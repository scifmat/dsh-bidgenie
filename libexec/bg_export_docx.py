#!/usr/bin/env python3
"""bg_export_docx.py：关键信息 docx 导出（M3.2 人性化版，CONTRACT §10.4 / Traecode 导出规则）。

用法：
    python bg_export_docx.py --workspace <ws> --project <dir|id> --mode keyinfo [--out <path>] [--json]
    python bg_export_docx.py --workspace <ws> --project <dir|id> --profile keyinfo --run [--json]  # H.1 兼容
    python bg_export_docx.py --selftest [--json]

M3.2 重写要点（参考 Traecode `md_to_docx.py` / `export_rules.md`，但 DSH 自包含）：
- 合并 9 个人读提取稿（01–05 公共 + 每包 06–09），**剥离源标题编号**后写入；
- **Word 原生多级自动编号**：numbering.xml 定义 6 级列表并绑定 Heading 1–6 样式
  （H1 `第%1章`、H2 `%1.%2`、H3 `%1.%2.%3`…），增删章节 Word 自动重编号；
- 标题黑体（H1 16pt / H2 14pt / H3+ 12pt），正文宋体小四 12pt、1.5 倍行距、首行缩进 2 字符；
- 表格：Table Grid、表头加粗 + 浅灰底（D9D9D9）、单元格宋体五号；
- **无封面、无目录、无页眉页脚**；标题行（项目名/项目编号/导出时间）作为正文段落；
- **公开标识/联系信息不脱敏**（项目编号/采购计划备案号/项目名称/采购人/代理机构/联系人/公开电话/邮箱/地址原样保留）；
  仅对身份证号、银行卡号/银行账号、个人证件号等私密号码按上下文掩码；
- 样式参数读取 `plugin/templates/styles/keyinfo/official-keyinfo.json` 的 `params`；
- 原子落盘 + 回填 `project.json.keyInfoExport`。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib  # noqa: E402
import bg_outline_validate as outline_validate  # noqa: E402

EXPORT_STYLE_PROFILE = "keyinfo"
STYLE_FILE_REL = Path("templates") / "styles" / "keyinfo" / "official-keyinfo.json"
COMMON_TEMPLATE_IDS = ("01", "02", "03", "04", "05")
PACKAGE_TEMPLATE_IDS = ("06", "07", "08", "09")

DEFAULT_PARAMS: dict[str, Any] = {
    "fontFamily": "宋体",
    "headingFontFamily": "黑体",
    "fontLatin": "Times New Roman",
    "headingFontLatin": "Arial",
    "captionFontFamily": "宋体",
    "captionFontLatin": "Times New Roman",
    "tableFontFamily": "宋体",
    "tableFontLatin": "Times New Roman",
    "textColor": "auto",
    "headingColor": "auto",
    "captionColor": "auto",
    "tableColor": "auto",
    "fontSizePt": 12,
    "headingSizesPt": {"h1": 16, "h2": 14, "h3": 12, "h4": 12, "h5": 12, "h6": 12},
    "lineSpacing": 1.5,
    "pageMarginMm": {"top": 25.4, "bottom": 25.4, "left": 31.8, "right": 31.8},
    "headingNumbering": True,
    "headingNumberingScheme": {
        "h1": "第%1章",
        "h2": "%1.%2",
        "h3": "%1.%2.%3",
        "h4": "%1.%2.%3.%4",
        "h5": "%1.%2.%3.%4.%5",
        "h6": "%1.%2.%3.%4.%5.%6",
    },
    "tableFontSizePt": 10.5,
    "tableHeaderFill": "D9D9D9",
    "tableHeaderBold": True,
    "tableStyle": "Table Grid",
    "firstLineIndentChars": 2,
    "title": {
        "fontFamily": "黑体",
        "fontSizePt": 18,
        "bold": True,
        "align": "center",
        "spaceAfterPt": 6,
    },
    "table": {
        "autoFit": True,
        "cellAlign": "center",
        "cellVerticalAlign": "center",
        "headerBold": True,
        "headerShading": "D9D9D9",
        "fontSizePt": 10.5,
        "style": "Table Grid",
        "minColumnWidthCm": 1.2,
        "maxColumnWeight": 40,
    },
    "captions": {
        "tablePrefix": "表",
        "figurePrefix": "图",
        "fontFamily": "宋体",
        "fontLatin": "Times New Roman",
        "color": "auto",
        "fontSizePt": 10.5,
        "align": "center",
        "spaceBeforePt": 3,
        "spaceAfterPt": 3,
    },
}

_NUMBER_PREFIX_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百零〇两\d]+[章节部分篇]\s*[、.．:：]?\s*"
    r"|[一二三四五六七八九十]+\s*[、.．:：]\s*"
    r"|\d+(?:\.\d+)+\s+"
    r"|\d+(?:\.\d+)*\s*[、.．:：)]\s*"
    r"|[（(]\d+[)）]\s*"
    r"|[①-⑳]\s*"
    r")"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_ORDERED_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.+)$")
_QUOTE_RE = re.compile(r"^>\s*(.*)$")
_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|\*([^*]+?)\*|`([^`]+?)`)")

#: 强身份证模式（18 位，末位可为 X）：无上下文时也掩码
_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
#: 银行卡/银行账号（16–19 位纯数字）：仅在隐私标签上下文中掩码，避免误伤 12/16 位项目编号
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
#: 隐私标签（出现才按私密号码处理）
_PRIVATE_LABEL_RE = re.compile(r"(身份证|证件号|证件号码|银行卡|银行账号|卡号|开户行|账号)")
#: 公开标识/联系信息标签（仅用于文档说明与自检，不做掩码）
_PUBLIC_LABEL_RE = re.compile(
    r"(项目编号|采购计划备案号|标段编号|项目名称|采购人|采购单位|代理机构|联系人|电话|邮箱|地址|联系方式)"
)
#: 过滤 JSON 片段/绝对路径（避免机器信息进入人读文档）
# P3-2：URL 里的 `https://` 会被旧的 `[A-Za-z]:[\\/]` 误判为盘符路径，整段丢弃；
# 这里要求盘符/Unix 路径前不是字母数字或 URL 标点，避免误伤公开网址。
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9+.\-])(?:[A-Za-z]:[\\/]|\\\\[^\\\s]|/Users/|/home/|/mnt/)")
_STYLE_LEVEL_KEYS = ("h1", "h2", "h3", "h4", "h5", "h6")


# ---------------------------------------------------------------------------
# 样式参数
# ---------------------------------------------------------------------------
def style_file_for_mode(mode: str) -> Path:
    return _PLUGIN_ROOT / "templates" / "styles" / mode / f"official-{mode}.json"


def load_style_params(mode: str = "keyinfo", resolved_path: Path | None = None) -> dict[str, Any]:
    """读取官方样式或 resolved style params（缺文件/字段时回退默认值）。"""
    params = dict(DEFAULT_PARAMS)
    style_file = resolved_path if resolved_path is not None and resolved_path.is_file() else style_file_for_mode(mode)
    if not style_file.is_file():
        return params
    try:
        document = jsonio.read_json(style_file)
    except errors.BgError:
        return params
    if not isinstance(document, dict):
        return params
    raw = document.get("params")
    if not isinstance(raw, dict):
        return params
    for key, value in raw.items():
        if key == "headingSizesPt" and isinstance(value, dict):
            merged = dict(DEFAULT_PARAMS["headingSizesPt"])
            merged.update({str(k).lower(): v for k, v in value.items() if isinstance(v, (int, float))})
            params[key] = merged
        elif key == "headingNumberingScheme" and isinstance(value, dict):
            merged = dict(DEFAULT_PARAMS["headingNumberingScheme"])
            merged.update({str(k).lower(): str(v) for k, v in value.items() if v})
            params[key] = merged
        elif key == "pageMarginMm" and isinstance(value, dict):
            merged = dict(DEFAULT_PARAMS["pageMarginMm"])
            merged.update({str(k): v for k, v in value.items() if isinstance(v, (int, float))})
            params[key] = merged
        elif key in ("title", "table", "captions") and isinstance(value, dict):
            merged = dict(DEFAULT_PARAMS.get(key) or {})
            merged.update({str(k): v for k, v in value.items() if v is not None})
            params[key] = merged
        else:
            params[key] = value
    return params


def table_config(params: dict[str, Any]) -> dict[str, Any]:
    """解析表格样式参数（优先 params.table，回退旧扁平字段）。"""
    config = dict(DEFAULT_PARAMS["table"])
    nested = params.get("table")
    if isinstance(nested, dict):
        config.update({str(k): v for k, v in nested.items() if v is not None})
    config.setdefault("headerShading", params.get("tableHeaderFill", "D9D9D9"))
    config.setdefault("headerBold", params.get("tableHeaderBold", True))
    config.setdefault("fontSizePt", params.get("tableFontSizePt", 10.5))
    config.setdefault("style", params.get("tableStyle", "Table Grid"))
    return config


def _merge_defaults(base: dict[str, Any], override: Any) -> dict[str, Any]:
    merged = dict(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def strip_heading_numbering(title: str) -> str:
    """剥离源标题自带编号（第一章 / 一、 / 1.1 / （1） / ① 等）。"""
    text = (title or "").strip().strip("#").strip()
    for _ in range(3):
        match = _NUMBER_PREFIX_RE.match(text)
        if not match:
            break
        text = text[match.end() :].strip()
    return text.strip()


def _sanitize_inline(text: str, counter: dict[str, Any], *, context: str = "") -> str:
    """防御性清洗：丢弃 JSON/绝对路径片段；仅按隐私上下文掩码身份证/银行卡/个人证件号。

    公开标识（项目编号/备案号/名称/采购人/代理机构/联系人/公开电话/邮箱/地址）一律不掩码；
    无隐私标签的 16–19 位纯数字（如项目编号）也不掩码，避免误伤。

    P3-2：丢弃时记录 `droppedSamples`（最多 20 条），导出结果可核对“到底丢了什么”。
    """
    match = _ABS_PATH_RE.search(text)
    if match:
        counter["dropped"] += 1
        samples = counter.setdefault("droppedSamples", [])
        if isinstance(samples, list) and len(samples) < 20:
            samples.append({"reason": "absolutePath", "preview": str(text).strip()[:160]})
        return ""
    stripped = text.strip()
    if stripped.startswith(("{", "}", "[", "]")) or '"schemaVersion"' in stripped:
        counter["dropped"] += 1
        samples = counter.setdefault("droppedSamples", [])
        if isinstance(samples, list) and len(samples) < 20:
            samples.append({"reason": "jsonFragment", "preview": stripped[:160]})
        return ""
    combined = f"{context}\n{text}"
    if _PRIVATE_LABEL_RE.search(combined):
        for pattern in (_ID_CARD_RE, _BANK_CARD_RE):
            text, count = pattern.subn("***", text)
            counter["masked"] += count
    else:
        # 无隐私标签时仅掩码强身份证模式（18 位、末位可为 X），其余公开数字原样保留
        text, count = _ID_CARD_RE.subn("***", text)
        counter["masked"] += count
    return text


# ---------------------------------------------------------------------------
# docx 构建器
# ---------------------------------------------------------------------------
class KeyInfoDocBuilder:
    """人读关键信息 docx 构建器（Word 原生多级自动编号 + 黑体标题/宋体正文）。"""

    _W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        from docx import Document

        self.params = params or load_style_params()
        self.doc = Document()
        self.counter: dict[str, Any] = {"paragraphs": 0, "headings": 0, "tables": 0, "dropped": 0, "masked": 0}
        self._num_id: int | None = None
        self._setup_styles()
        self._setup_page()
        if self.params.get("headingNumbering", True):
            self._setup_multilevel_numbering()

    # -- 基础样式 -------------------------------------------------------
    def _setup_styles(self) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.shared import Pt

        params = self.params
        normal = self.doc.styles["Normal"]
        normal.font.name = str(params["fontFamily"])
        normal.font.size = Pt(float(params["fontSizePt"]))
        normal_rpr = normal.element.get_or_add_rPr()
        self._apply_rfonts(normal_rpr, str(params["fontFamily"]), str(params.get("fontLatin", "Times New Roman")))
        self._set_color_auto(normal_rpr)
        normal.paragraph_format.line_spacing = float(params["lineSpacing"])
        normal.paragraph_format.space_before = Pt(0)
        normal.paragraph_format.space_after = Pt(0)
        normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        sizes = params["headingSizesPt"]
        for level in range(1, 7):
            style = self.doc.styles[f"Heading {level}"]
            style.font.name = str(params["headingFontFamily"])
            style.font.size = Pt(float(sizes.get(f"h{level}", 12)))
            style.font.bold = True
            heading_rpr = style.element.get_or_add_rPr()
            self._apply_rfonts(
                heading_rpr,
                str(params["headingFontFamily"]),
                str(params.get("headingFontLatin", "Arial")),
            )
            self._set_color_auto(heading_rpr)
            paragraph_format = style.paragraph_format
            paragraph_format.space_before = Pt(6)
            paragraph_format.space_after = Pt(6)
            paragraph_format.line_spacing = 1.0
            paragraph_format.keep_with_next = True

    def _setup_page(self) -> None:
        from docx.shared import Cm, Pt

        margins = self.params["pageMarginMm"]
        for section in self.doc.sections:
            section.page_width = Cm(21.0)
            section.page_height = Cm(29.7)
            section.top_margin = Cm(float(margins.get("top", 25.4)) / 10)
            section.bottom_margin = Cm(float(margins.get("bottom", 25.4)) / 10)
            section.left_margin = Cm(float(margins.get("left", 31.8)) / 10)
            section.right_margin = Cm(float(margins.get("right", 31.8)) / 10)
            section.header_distance = Cm(1.5)
            section.footer_distance = Cm(1.75)
        # 无页眉页脚：清空默认 header/footer 内容（python-docx 默认不创建段落，这里防御性清理）
        for section in self.doc.sections:
            for part in (section.header, section.footer):
                for paragraph in list(part.paragraphs):
                    paragraph._p.getparent().remove(paragraph._p)

    def _apply_rfonts(self, rpr: Any, cjk_font: str, latin_font: str) -> None:
        """rFonts 四属性：ascii/hAnsi/cs=西文、eastAsia=中文（M8 硬要求）。"""
        from docx.oxml.ns import qn

        rfonts = rpr.rFonts
        if rfonts is None:  # pragma: no cover - python-docx 会按需创建
            from docx.oxml import OxmlElement

            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs"):
            rfonts.set(qn(attr), latin_font)
        rfonts.set(qn("w:eastAsia"), cjk_font)

    def _set_color_auto(self, rpr: Any) -> None:
        """所有文字颜色写 w:color val=auto（不写 000000/RGB）。"""
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        for existing in rpr.findall(qn("w:color")):
            rpr.remove(existing)
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "auto")
        rpr.append(color)

    def _set_run_font(
        self,
        run: Any,
        *,
        font_name: str,
        size_pt: float,
        bold: bool = False,
        italic: bool = False,
        latin_font: str | None = None,
    ) -> None:
        from docx.shared import Pt

        run.font.name = font_name
        run.font.size = Pt(size_pt)
        run.font.bold = bold
        run.font.italic = italic
        rpr = run.element.get_or_add_rPr()
        self._apply_rfonts(rpr, font_name, latin_font or str(self.params.get("fontLatin", "Times New Roman")))
        self._set_color_auto(rpr)

    # -- Word 原生多级自动编号 -----------------------------------------
    def _get_or_create_numbering_element(self) -> Any:
        try:
            return self.doc.part.numbering_part.element
        except (KeyError, AttributeError):
            pass
        try:
            from docx.opc.constants import RELATIONSHIP_TYPE as RT
            from docx.opc.packuri import PackURI
            from docx.oxml import parse_xml
            from docx.parts.numbering import NumberingPart

            partname = PackURI("/word/numbering.xml")
            content_type = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
            )
            element = parse_xml(f'<w:numbering xmlns:w="{self._W_NS}"/>')
            part = NumberingPart.new(partname, content_type, element, self.doc.part.package)
            self.doc.part.relate_to(part, RT.NUMBERING)
            return part.element
        except Exception:  # noqa: BLE001 - 无法创建时降级为无编号
            return None

    def _setup_multilevel_numbering(self) -> None:
        from docx.oxml import parse_xml
        from docx.oxml.ns import qn

        numbering_elem = self._get_or_create_numbering_element()
        if numbering_elem is None:
            return
        abstract_existing = numbering_elem.findall(qn("w:abstractNum"))
        next_abstract = 0
        for element in abstract_existing:
            try:
                next_abstract = max(next_abstract, int(element.get(qn("w:abstractNumId"))) + 1)
            except (TypeError, ValueError):
                continue
        num_existing = numbering_elem.findall(qn("w:num"))
        next_num = 1
        for element in num_existing:
            try:
                next_num = max(next_num, int(element.get(qn("w:numId"))) + 1)
            except (TypeError, ValueError):
                continue
        scheme = self.params["headingNumberingScheme"]
        sizes = self.params["headingSizesPt"]
        heading_font = str(self.params["headingFontFamily"])
        heading_latin = str(self.params.get("headingFontLatin", "Arial"))
        parts = [
            f'<w:abstractNum xmlns:w="{self._W_NS}" w:abstractNumId="{next_abstract}">',
            '<w:multiLevelType w:val="multilevel"/>',
        ]
        for level in range(1, 7):
            lvl_text = str(scheme.get(f"h{level}") or "%" + ".".join(str(index) for index in range(1, level + 1)))
            size_half_pt = int(round(float(sizes.get(f"h{level}", 12)) * 2))
            parts.append(
                f'<w:lvl w:ilvl="{level - 1}">'
                f'<w:start w:val="1"/>'
                f'<w:numFmt w:val="decimal"/>'
                f'<w:lvlText w:val="{lvl_text}"/>'
                f'<w:lvlJc w:val="left"/>'
                f'<w:suff w:val="space"/>'
                f'<w:pPr><w:ind w:left="0" w:firstLine="0"/></w:pPr>'
                f'<w:rPr><w:rFonts w:ascii="{heading_font}" w:eastAsia="{heading_font}" '
                f'w:hAnsi="{heading_font}"/><w:b/><w:i w:val="false"/>'
                f'<w:color w:val="000000"/><w:sz w:val="{size_half_pt}"/>'
                f'<w:szCs w:val="{size_half_pt}"/></w:rPr></w:lvl>'
            )
        parts.append("</w:abstractNum>")
        abstract_element = parse_xml("".join(parts))
        first_num = numbering_elem.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(abstract_element)
        else:
            numbering_elem.append(abstract_element)
        num_element = parse_xml(
            f'<w:num xmlns:w="{self._W_NS}" w:numId="{next_num}">'
            f'<w:abstractNumId w:val="{next_abstract}"/></w:num>'
        )
        numbering_elem.append(num_element)
        for level in range(1, 7):
            self._bind_heading_style(level, next_num, level - 1)
        self._num_id = next_num

    def _bind_heading_style(self, level: int, num_id: int, ilvl: int) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        try:
            style = self.doc.styles[f"Heading {level}"]
        except KeyError:
            return
        style_element = style.element
        ppr = style_element.find(qn("w:pPr"))
        if ppr is None:
            ppr = OxmlElement("w:pPr")
            style_element.append(ppr)
        existing = ppr.find(qn("w:numPr"))
        if existing is not None:
            ppr.remove(existing)
        num_pr = OxmlElement("w:numPr")
        ilvl_element = OxmlElement("w:ilvl")
        ilvl_element.set(qn("w:val"), str(ilvl))
        num_id_element = OxmlElement("w:numId")
        num_id_element.set(qn("w:val"), str(num_id))
        num_pr.append(ilvl_element)
        num_pr.append(num_id_element)
        ppr.append(num_pr)

    # -- 标题行（正文段落，不参与 Heading 编号） ---------------------------
    def add_title_block(self, project_name: str, project_no: str, exported_at: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        title_config = _merge_defaults(DEFAULT_PARAMS["title"], self.params.get("title"))
        title = self.doc.add_paragraph()
        title.alignment = self._alignment(str(title_config.get("align", "center")))
        title.paragraph_format.space_after = Pt(float(title_config.get("spaceAfterPt", 6)))
        run = title.add_run(project_name)
        self._set_run_font(
            run,
            font_name=str(title_config.get("fontFamily", self.params["headingFontFamily"])),
            latin_font=str(title_config.get("fontLatin", self.params.get("headingFontLatin", "Arial"))),
            size_pt=float(title_config.get("fontSizePt", 18)),
            bold=bool(title_config.get("bold", True)),
        )
        self.counter["paragraphs"] += 1
        meta = self.doc.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
        meta.paragraph_format.space_after = Pt(12)
        meta_run = meta.add_run(f"项目编号：{project_no}    导出时间：{exported_at}")
        self._set_run_font(meta_run, font_name=str(self.params["fontFamily"]), size_pt=10.5)
        self.counter["paragraphs"] += 1

    def add_toc_field(self, title: str = "目录") -> None:
        """tech 模式默认插入 Word TOC 域，打开文档时提示更新域。

        P2-3（2026-11-25）：招标文件常要求技术文件含详细目录，默认插入；用户明确不要
        目录时用 `bg_export_docx.py --mode tech --no-toc`。导出后自检允许 TOC 域；
        用户打开 docx 后按 F9/“更新域”即可生成页码。
        """
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt

        heading = self.doc.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading.paragraph_format.space_before = Pt(12)
        heading.paragraph_format.space_after = Pt(6)
        heading_run = heading.add_run(title)
        self._set_run_font(
            heading_run,
            font_name=str(self.params["headingFontFamily"]),
            latin_font=str(self.params.get("headingFontLatin", "Arial")),
            size_pt=float(self.params["headingSizesPt"].get("h1", 16)),
            bold=True,
        )
        paragraph = self.doc.add_paragraph()
        begin_run = paragraph.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        begin.set(qn("w:dirty"), "true")
        begin_run._r.append(begin)
        instr_run = paragraph.add_run()
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = ' TOC \\o "1-3" \\h \\z \\u '
        instr_run._r.append(instr)
        separate_run = paragraph.add_run()
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        separate_run._r.append(separate)
        placeholder_run = paragraph.add_run("（目录域：打开 Word 后按 Ctrl+A、F9 更新；或右键选择“更新域”）")
        self._set_run_font(placeholder_run, font_name=str(self.params["fontFamily"]), size_pt=10.5)
        end_run = paragraph.add_run()
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        end_run._r.append(end)
        # 用户要求：目录与正文之间插入分页符，正文从新页开始。
        from docx.enum.text import WD_BREAK

        page_break = self.doc.add_paragraph()
        page_break.add_run().add_break(WD_BREAK.PAGE)
        self.counter["paragraphs"] += 1
        # 打开 Word 时自动更新域（w:updateFields），避免用户看到空目录
        settings = self.doc.settings.element
        if settings.find(qn("w:updateFields")) is None:
            update = OxmlElement("w:updateFields")
            update.set(qn("w:val"), "true")
            settings.append(update)
        self.counter["paragraphs"] += 1

    def _alignment(self, name: str) -> Any:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        mapping = {
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
        }
        return mapping.get(str(name or "center").lower(), WD_ALIGN_PARAGRAPH.CENTER)

    # -- 章节标题 -------------------------------------------------------
    def add_chapter(self, title: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        text = strip_heading_numbering(title) or "未命名章节"
        paragraph = self.doc.add_heading(text, level=1)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        size = float(self.params["headingSizesPt"].get("h1", 16))
        for run in paragraph.runs:
            self._set_run_font(
                run,
                font_name=str(self.params["headingFontFamily"]),
                latin_font=str(self.params.get("headingFontLatin", "Arial")),
                size_pt=size,
                bold=True,
                italic=False,
            )
        self.counter["headings"] += 1

    def _add_subheading(self, level: int, title: str) -> None:
        text = strip_heading_numbering(title)
        if not text:
            return
        paragraph = self.doc.add_heading(text, level=min(level, 6))
        size = float(self.params["headingSizesPt"].get(f"h{level}", 12))
        for run in paragraph.runs:
            self._set_run_font(
                run,
                font_name=str(self.params["headingFontFamily"]),
                latin_font=str(self.params.get("headingFontLatin", "Arial")),
                size_pt=size,
                bold=True,
                italic=False,
            )
        self.counter["headings"] += 1

    def _add_body_paragraph(self, text: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        text = _sanitize_inline(text, self.counter)
        if not text.strip():
            return
        caption_config = self.params.get("captions") or {}
        stripped = text.strip()
        if re.match(r"^(?:[▼▲][表图]|[表图][\s:：])", stripped):
            self.add_caption(stripped)
            return
        paragraph = self.doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph_format = paragraph.paragraph_format
        paragraph_format.line_spacing = float(self.params["lineSpacing"])
        paragraph_format.first_line_indent = Cm(0.74 * float(self.params.get("firstLineIndentChars", 2)) / 2)
        paragraph_format.space_before = Cm(0)
        paragraph_format.space_after = Cm(0)
        self._add_inline(paragraph, text)
        self.counter["paragraphs"] += 1

    def _add_inline(self, paragraph: Any, text: str) -> None:
        body_font = str(self.params["fontFamily"])
        body_latin = str(self.params.get("fontLatin", "Times New Roman"))
        body_size = float(self.params["fontSizePt"])
        last_end = 0
        for match in _INLINE_RE.finditer(text):
            if match.start() > last_end:
                run = paragraph.add_run(text[last_end : match.start()])
                self._set_run_font(run, font_name=body_font, latin_font=body_latin, size_pt=body_size)
            if match.group(2):
                run = paragraph.add_run(match.group(2))
                self._set_run_font(run, font_name=body_font, latin_font=body_latin, size_pt=body_size, bold=True)
            elif match.group(3):
                run = paragraph.add_run(match.group(3))
                self._set_run_font(run, font_name=body_font, latin_font=body_latin, size_pt=body_size, italic=True)
            elif match.group(4):
                run = paragraph.add_run(match.group(4))
                self._set_run_font(run, font_name="Consolas", latin_font="Consolas", size_pt=body_size - 1.5)
            last_end = match.end()
        if last_end < len(text):
            run = paragraph.add_run(text[last_end:])
            self._set_run_font(run, font_name=body_font, size_pt=body_size)

    def _add_quote(self, text: str) -> None:
        from docx.shared import Cm

        text = _sanitize_inline(text, self.counter)
        if not text:
            return
        paragraph = self.doc.add_paragraph()
        paragraph_format = paragraph.paragraph_format
        paragraph_format.left_indent = Cm(0.74)
        paragraph_format.line_spacing = float(self.params["lineSpacing"])
        self._add_inline(paragraph, text)
        for run in paragraph.runs:
            run.italic = True
        self.counter["paragraphs"] += 1

    def _add_list_item(self, text: str, *, ordered: bool, number: str = "") -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        text = _sanitize_inline(text, self.counter)
        if not text:
            return
        paragraph = self.doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph_format = paragraph.paragraph_format
        paragraph_format.line_spacing = float(self.params["lineSpacing"])
        paragraph_format.left_indent = Cm(0.74)
        paragraph_format.first_line_indent = Cm(-0.74)
        prefix = f"{number}. " if ordered and number else "• "
        prefix_run = paragraph.add_run(prefix)
        self._set_run_font(
            prefix_run,
            font_name=str(self.params["fontFamily"]),
            latin_font=str(self.params.get("fontLatin", "Times New Roman")),
            size_pt=float(self.params["fontSizePt"]),
        )
        self._add_inline(paragraph, text)
        self.counter["paragraphs"] += 1

    def _add_code_block(self, code: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        paragraph = self.doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph_format = paragraph.paragraph_format
        paragraph_format.left_indent = Cm(0.5)
        paragraph_format.line_spacing = 1.0
        run = paragraph.add_run(code)
        self._set_run_font(run, font_name="Consolas", latin_font="Consolas", size_pt=10.5)
        self._shade_paragraph(paragraph, "F5F5F5")
        self.counter["paragraphs"] += 1

    def _shade_paragraph(self, paragraph: Any, fill: str) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        ppr = paragraph._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        ppr.append(shd)

    @staticmethod
    def _display_width(text: str) -> int:
        """估算显示宽度（中日韩字符按 2 计，ASCII 按 1）。"""
        width = 0
        for char in text or "":
            width += 2 if ord(char) > 0x2E80 else 1
        return width

    def _column_widths_cm(self, header: list[str], rows: list[list[str]], config: dict[str, Any]) -> list[float]:
        """按内容长度估算各列宽度（短列窄、长内容列宽），总宽=页面可用宽度。"""
        column_count = max([len(header)] + [len(row) for row in rows]) if rows else len(header)
        max_weight = int(config.get("maxColumnWeight", 40) or 40)
        weights: list[float] = []
        for index in range(column_count):
            values = [header[index] if index < len(header) else ""]
            values.extend(row[index] if index < len(row) else "" for row in rows)
            content = max((self._display_width(str(value)) for value in values), default=2)
            weights.append(float(min(max(content, 2), max_weight)))
        margins = self.params.get("pageMarginMm") or {}
        available_cm = 21.0 - (float(margins.get("left", 31.8)) + float(margins.get("right", 31.8))) / 10.0
        available_cm = max(available_cm, 8.0)
        min_width = float(config.get("minColumnWidthCm", 1.2) or 1.2)
        if not config.get("autoFit", True):
            return [available_cm / column_count] * column_count
        total_weight = sum(weights) or 1.0
        widths = [max(min_width, available_cm * weight / total_weight) for weight in weights]
        # 最小宽度挤压后按比例回缩，保证总宽不超过可用宽度
        total_width = sum(widths)
        if total_width > available_cm:
            scale = available_cm / total_width
            widths = [max(min_width, width * scale) for width in widths]
            total_width = sum(widths)
            if total_width > available_cm:
                widths = [width * available_cm / total_width for width in widths]
        return [round(width, 2) for width in widths]

    def _add_table(self, header: list[str], rows: list[list[str]]) -> None:
        from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        if not header:
            return
        config = table_config(self.params)
        width = max([len(header)] + [len(row) for row in rows]) if rows else len(header)
        table = self.doc.add_table(rows=1, cols=width)
        try:
            table.style = str(config.get("style") or "Table Grid")
        except KeyError:  # pragma: no cover - 样式缺失时跳过
            pass
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        widths = self._column_widths_cm(header, rows, config)
        header_cells = table.rows[0].cells
        table_font = str(self.params.get("tableFontFamily", self.params["fontFamily"]))
        table_latin = str(self.params.get("tableFontLatin", "Times New Roman"))
        table_size = float(config.get("fontSizePt", 10.5))
        header_fill = str(config.get("headerShading", "D9D9D9"))
        horizontal = self._alignment(str(config.get("cellAlign", "center")))
        vertical_name = str(config.get("cellVerticalAlign", "center")).lower()
        vertical = {
            "center": WD_ALIGN_VERTICAL.CENTER,
            "top": WD_ALIGN_VERTICAL.TOP,
            "bottom": WD_ALIGN_VERTICAL.BOTTOM,
        }.get(vertical_name, WD_ALIGN_VERTICAL.CENTER)

        def apply_cell(cell: Any, text: str, *, bold: bool, width_cm: float, header_cell: bool) -> None:
            cell.width = Cm(width_cm)
            cell.vertical_alignment = vertical
            cell.text = ""
            paragraph = cell.paragraphs[0]
            for existing_run in list(paragraph.runs):
                existing_run._r.getparent().remove(existing_run._r)
            paragraph.alignment = horizontal
            run = paragraph.add_run(text)
            self._set_run_font(run, font_name=table_font, latin_font=table_latin, size_pt=table_size, bold=bold)
            if header_cell:
                self._set_cell_shading(cell, header_fill)

        for index in range(width):
            header_text = header[index] if index < len(header) else ""
            text = _sanitize_inline(header_text, self.counter, context=header_text)
            apply_cell(
                header_cells[index],
                text,
                bold=bool(config.get("headerBold", True)),
                width_cm=widths[index],
                header_cell=True,
            )
        for row in rows:
            cells = table.add_row().cells
            for index in range(width):
                row_label = row[0] if row else ""
                context = f"{header[index] if index < len(header) else ''} {row_label}"
                text = _sanitize_inline(row[index] if index < len(row) else "", self.counter, context=context)
                apply_cell(cells[index], text, bold=False, width_cm=widths[index], header_cell=False)
        self.counter["tables"] += 1
        self.counter.setdefault("tableWidths", []).append(widths)
        self.counter.setdefault("tableColumnCounts", []).append(width)
        # 表格后空一段，避免与后续正文粘连
        self.doc.add_paragraph()

    def _set_cell_shading(self, cell: Any, fill: str) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        tc_pr.append(shd)

    # -- 技术方案渲染（F7.4） ------------------------------------------
    def add_techn_heading(self, level: int, title: str) -> None:
        level = max(1, min(6, int(level)))
        if level == 1:
            self.add_chapter(title)
        else:
            self._add_subheading(level, title)

    def add_caption(self, text: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        caption_config = self.params.get("captions") or {}
        paragraph = self.doc.add_paragraph()
        align = str(caption_config.get("align", "center")).lower()
        paragraph.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "left": WD_ALIGN_PARAGRAPH.LEFT, "right": WD_ALIGN_PARAGRAPH.RIGHT}.get(align, WD_ALIGN_PARAGRAPH.CENTER)
        paragraph.paragraph_format.space_before = Pt(float(caption_config.get("spaceBeforePt", 3)))
        paragraph.paragraph_format.space_after = Pt(float(caption_config.get("spaceAfterPt", 3)))
        caption_font = str(caption_config.get("fontFamily", self.params.get("captionFontFamily", "宋体")))
        caption_latin = str(caption_config.get("fontLatin", self.params.get("captionFontLatin", "Times New Roman")))
        caption_size = float(caption_config.get("fontSizePt", 10.5))

        def add_run(segment: str, bold: bool = False) -> None:
            if not segment:
                return
            run = paragraph.add_run(segment)
            self._set_run_font(run, font_name=caption_font, latin_font=caption_latin, size_pt=caption_size, bold=bold)

        normalized = (text or "").strip()
        match = re.match(r"^([▼▲])\s*([表图])\s*[-—:：]?\s*(.*)$", normalized)
        if match:
            symbol, kind, rest = match.group(1), match.group(2), match.group(3).strip()
            add_run(symbol)
            add_run(kind, bold=True)
            if rest:
                add_run("-" + rest)
        else:
            add_run(normalized)
        self.counter["paragraphs"] += 1

    def add_tech_figure(self, anchor_id: str, caption: str, package_dir: Path, figure_type: str) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        png_path = package_dir / "resources" / figure_type / f"{anchor_id}.png"
        paragraph = self.doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if png_path.is_file():
            section = self.doc.sections[0]
            margins = self.params.get("pageMarginMm") or {}
            page_width = float(section.page_width.cm or 21.0)
            page_height = float(section.page_height.cm or 29.7)
            left_margin = float(section.left_margin.cm or float(margins.get("left", 19)) / 10.0)
            right_margin = float(section.right_margin.cm or float(margins.get("right", 19)) / 10.0)
            top_margin = float(section.top_margin.cm or float(margins.get("top", 20)) / 10.0)
            bottom_margin = float(section.bottom_margin.cm or float(margins.get("bottom", 20)) / 10.0)
            content_width = max(2.0, page_width - left_margin - right_margin)
            content_height = max(2.0, page_height - top_margin - bottom_margin)
            caption_config = self.params.get("captions") or {}
            caption_reserved = 0.0
            if caption:
                caption_reserved = float(caption_config.get("fontSizePt", 10.5)) / 72.0 * 2.54
                caption_reserved += (float(caption_config.get("spaceBeforePt", 3)) + float(caption_config.get("spaceAfterPt", 3))) / 72.0 * 2.54
                caption_reserved += 0.4
            max_width = max(2.0, content_width - 0.3)
            max_height = max(1.2, content_height - caption_reserved - 0.5)
            try:
                from PIL import Image

                with Image.open(png_path) as image:
                    aspect = float(image.width) / float(image.height) if image.height else 1.0
                height_cm = max_height
                width_cm = height_cm * aspect
                if width_cm > max_width:
                    width_cm = max_width
                    height_cm = width_cm / aspect
                if height_cm < 1.2:
                    height_cm = 1.2
                    width_cm = min(max_width, height_cm * aspect)
                paragraph.add_run().add_picture(str(png_path), width=Cm(width_cm))
                self.counter["images"] = int(self.counter.get("images", 0)) + 1
                self.counter.setdefault("imageSizesCm", []).append({"anchorId": anchor_id, "widthCm": round(width_cm, 3), "heightCm": round(height_cm, 3), "maxHeightCm": round(max_height, 3), "pageContentHeightCm": round(content_height, 3)})
            except Exception:  # noqa: BLE001 - 插图失败降级占位
                run = paragraph.add_run("[图片缺失]")
                self._set_run_font(run, font_name=str(self.params["fontFamily"]), size_pt=float(self.params["fontSizePt"]))
                self.missing_figures.append({"anchorId": anchor_id, "reason": "insert_failed"})
        else:
            run = paragraph.add_run("[图片缺失]")
            self._set_run_font(run, font_name=str(self.params["fontFamily"]), size_pt=float(self.params["fontSizePt"]))
            self.missing_figures.append({"anchorId": anchor_id, "reason": "png_missing", "path": paths.rel_posix(package_dir, png_path)})
        self.counter["paragraphs"] += 1
        if caption:
            self.add_caption(caption)

    def render_tech_markdown(
        self,
        markdown: str,
        *,
        chapter_id: str,
        base_level: int,
        package_dir: Path,
        figures: dict[str, dict[str, Any]],
    ) -> None:
        lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        first_heading = next((line for line in lines if _HEADING_RE.match(line.strip())), None)
        md_base = len(_HEADING_RE.match(first_heading.strip()).group(1)) if first_heading else base_level  # type: ignore[union-attr]
        removed_first = False
        index = 0
        in_fence = False
        code_lines: list[str] = []
        paragraph_buffer: list[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph_buffer
            text = " ".join(item.strip() for item in paragraph_buffer if item.strip()).strip()
            paragraph_buffer = []
            if text:
                self._add_body_paragraph(text)

        def consume_table(start: int) -> tuple[int, list[list[str]] | None]:
            cursor = start
            rows_raw: list[str] = []
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                rows_raw.append(lines[cursor].strip())
                cursor += 1
            filtered = [row for row in rows_raw if not _TABLE_SEPARATOR_RE.match(row)]
            if not filtered:
                return cursor, None
            parsed = [[cell.strip().replace("\\|", "|") for cell in row.strip("|").split("|")] for row in filtered]
            return cursor, parsed

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("```"):
                flush_paragraph()
                if in_fence:
                    self._add_code_block("\n".join(code_lines))
                    code_lines = []
                    in_fence = False
                else:
                    in_fence = True
                    code_lines = []
                index += 1
                continue
            if in_fence:
                code_lines.append(line)
                index += 1
                continue
            if not stripped or stripped.startswith("<!--"):
                if stripped.startswith("<!--"):
                    figure_match = _FIGURE_MARKER_RE.match(stripped)
                    if figure_match:
                        flush_paragraph()
                        anchor_id = figure_match.group(1)
                        plan = figures.get(anchor_id) or {}
                        figure_type = str(plan.get("type") or "mermaid")
                        caption = ""
                        cursor = index + 1
                        while cursor < len(lines) and not lines[cursor].strip():
                            cursor += 1
                        if cursor < len(lines) and lines[cursor].strip().startswith("▲图"):
                            caption = lines[cursor].strip()
                            cursor += 1
                        # 表格型 figurePlan 由正文 Markdown 表格承载，不应插图；
                        # 作者若误加 BG:FIGURE 标记，跳过标记并保留其后的 ▼表题注/表格。
                        if figure_type == "table":
                            self.table_marker_warnings.append({"anchorId": anchor_id, "chapterId": str(chapter_id)})
                            index += 1
                            continue
                        self.add_tech_figure(anchor_id, caption, package_dir, figure_type)
                        index = cursor
                        continue
                index += 1
                continue
            heading = _HEADING_RE.match(stripped)
            if heading:
                if not removed_first and first_heading is not None and stripped == first_heading.strip():
                    removed_first = True
                    index += 1
                    continue
                flush_paragraph()
                level = len(heading.group(1))
                word_level = base_level + (level - md_base)
                self.add_techn_heading(max(1, min(6, word_level)), heading.group(2))
                index += 1
                continue
            if stripped.startswith("▼表"):
                flush_paragraph()
                caption = stripped
                cursor = index + 1
                while cursor < len(lines) and not lines[cursor].strip():
                    cursor += 1
                cursor, parsed = consume_table(cursor)
                self.add_caption(caption)
                if parsed:
                    self._add_table(parsed[0], parsed[1:])
                index = cursor
                continue
            if stripped.startswith("|"):
                flush_paragraph()
                cursor, parsed = consume_table(index)
                if parsed:
                    self._add_table(parsed[0], parsed[1:])
                index = cursor
                continue
            image = _IMAGE_RE.fullmatch(stripped)
            if image:
                flush_paragraph()
                self._add_image(image.group(1), image.group(2), package_dir)
                index += 1
                continue
            quote = _QUOTE_RE.match(stripped)
            if quote:
                flush_paragraph()
                self._add_quote(quote.group(1))
                index += 1
                continue
            ordered = _ORDERED_RE.match(stripped)
            if ordered:
                flush_paragraph()
                self._add_list_item(ordered.group(2), ordered=True, number=ordered.group(1))
                index += 1
                continue
            bullet = _BULLET_RE.match(stripped)
            if bullet:
                flush_paragraph()
                self._add_list_item(bullet.group(1), ordered=False)
                index += 1
                continue
            paragraph_buffer.append(stripped)
            index += 1
        flush_paragraph()
        if in_fence and code_lines:
            self._add_code_block("\n".join(code_lines))

    # -- Markdown 正文渲染 ---------------------------------------------
    def render_markdown(self, markdown: str, *, base_dir: Path | None = None) -> None:
        lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        index = 0
        in_fence = False
        code_lines: list[str] = []
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_fence:
                    self._add_code_block("\n".join(code_lines))
                    code_lines = []
                    in_fence = False
                else:
                    in_fence = True
                    code_lines = []
                index += 1
                continue
            if in_fence:
                code_lines.append(line)
                index += 1
                continue
            if not stripped or stripped.startswith("<!--"):
                index += 1
                continue
            heading = _HEADING_RE.match(stripped)
            if heading:
                level = len(heading.group(1))
                if level >= 2:  # H1 由章节标题负责，跳过源 H1
                    self._add_subheading(level, heading.group(2))
                index += 1
                continue
            if stripped.startswith("|"):
                block: list[str] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    block.append(lines[index].strip())
                    index += 1
                rows_raw = [row for row in block if not _TABLE_SEPARATOR_RE.match(row)]
                if not rows_raw:
                    continue
                parsed = [
                    [cell.strip().replace("\\|", "|") for cell in row.strip("|").split("|")]
                    for row in rows_raw
                ]
                if parsed:
                    self._add_table(parsed[0], parsed[1:])
                continue
            image = _IMAGE_RE.fullmatch(stripped)
            if image:
                self._add_image(image.group(1), image.group(2), base_dir)
                index += 1
                continue
            quote = _QUOTE_RE.match(stripped)
            if quote:
                self._add_quote(quote.group(1))
                index += 1
                continue
            ordered = _ORDERED_RE.match(stripped)
            if ordered:
                self._add_list_item(ordered.group(2), ordered=True, number=ordered.group(1))
                index += 1
                continue
            bullet = _BULLET_RE.match(stripped)
            if bullet:
                self._add_list_item(bullet.group(1), ordered=False)
                index += 1
                continue
            self._add_body_paragraph(stripped)
            index += 1
        if in_fence and code_lines:
            self._add_code_block("\n".join(code_lines))

    def _add_image(self, alt: str, source: str, base_dir: Path | None) -> None:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        candidates: list[Path] = []
        source_path = Path(source)
        if source_path.is_absolute():
            candidates.append(source_path)
        elif base_dir is not None:
            candidates.append(base_dir / source_path)
        candidates.append(Path.cwd() / source_path)
        image_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        paragraph = self.doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if image_path is not None:
            try:
                paragraph.add_run().add_picture(str(image_path), width=Cm(15))
                self.counter["paragraphs"] += 1
                return
            except Exception:  # noqa: BLE001 - 插图失败降级为占位文字
                pass
        run = paragraph.add_run(f"[图片：{alt or source}]")
        self._set_run_font(run, font_name=str(self.params["fontFamily"]), size_pt=float(self.params["fontSizePt"]))
        self.counter["paragraphs"] += 1

    def save(self, target: Path) -> None:
        _atomic_save_document(self.doc, target)


# ---------------------------------------------------------------------------
# 技术方案导出（F7.4）
# ---------------------------------------------------------------------------
_FIGURE_MARKER_RE = re.compile(r"<!--\s*BG:FIGURE:([A-Za-z0-9-]+)\s*-->")


class TechDocBuilder(KeyInfoDocBuilder):
    """F7.4 技术方案：outline 深度优先 + Heading1-6/编号 + md 表格/图/题注。"""

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self.missing_figures: list[dict[str, Any]] = []
        self.table_marker_warnings: list[dict[str, Any]] = []
        self.counter.setdefault("images", 0)


def find_package_dir(project_dir: Path, package_id: str | None = None) -> Path:
    packages = sorted(path for path in (project_dir / "packages").glob("package-*") if path.is_dir())
    if not packages:
        raise errors.BgError(errors.E_NOT_FOUND, "项目下没有 package-* 目录", detail={"project": str(project_dir)})
    if package_id:
        match = next((path for path in packages if path.name == package_id or path.name.startswith(package_id + "-")), None)
        if match is None:
            raise errors.BgError(errors.E_NOT_FOUND, f"找不到包目录：{package_id}", detail={"available": [path.name for path in packages]})
        return match
    return packages[0]


def load_resolved_style(
    *,
    workspace: Path,
    project_dir: Path,
    package_dir: Path,
    package_id: str,
    profile_id: str,
) -> tuple[dict[str, Any], Path, list[str]]:
    import bg_style_merge

    resolved_path = package_dir / "export" / ".resolved-style.json"
    warnings: list[str] = []
    if resolved_path.is_file():
        document = jsonio.read_json(resolved_path)
        params = document.get("params") if isinstance(document, Mapping) else None
        if isinstance(params, dict) and str(document.get("id") or "") == profile_id:
            return dict(params), resolved_path, warnings
        warnings.append(f"resolved style id={document.get('id') if isinstance(document, Mapping) else None} 与 profile={profile_id} 不一致，重新 resolve")
    result = bg_style_merge.merge_profile(profile_id, workspace=workspace, project_dir=project_dir, package_id=package_id)
    document = result["document"]
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(resolved_path, document)
    warnings.extend(result.get("warnings") or [])
    return dict(document.get("params") or {}), resolved_path, warnings


def cleanup_export_family(directory: Path, patterns: Sequence[str], keep: int = 1) -> dict[str, Any]:
    """按 pattern 保留最新 keep 份文件（按 mtime，再按名称），返回 removed/kept。"""
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path for path in directory.glob(pattern) if path.is_file())
    unique = {path.resolve(): path for path in files}
    ordered = sorted(unique.values(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    keep = max(1, int(keep or 1))
    removed: list[str] = []
    for path in ordered[keep:]:
        removed.append(str(path))
        path.unlink(missing_ok=True)
    return {"removed": removed, "kept": [str(path) for path in ordered[:keep]]}


def _default_export_target(package_dir: Path, project_name: str, mode: str) -> Path:
    safe_name = paths.safe_component(project_name) or "项目"
    suffix = "技术方案" if mode == "tech" else ("工作概览" if mode == "overview" else "关键信息")
    export_dir = package_dir / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"{safe_name}-{suffix}-{paths.timestamp14()}.docx"
    if target.exists():
        target = export_dir / f"{safe_name}-{suffix}-{paths.timestamp14()}-{paths.rand4()}.docx"
    return target


def _tech_package_title(project: Mapping[str, Any], package_meta: Mapping[str, Any], package_id: str, package_count: int, project_dir: Path) -> str:
    project_name = str(project.get("projectName") or "未命名项目").strip() or "未命名项目"
    if package_count <= 1:
        return f"{project_name}技术方案"
    package_no = package_id.split("-")[-1]
    package_name = str(package_meta.get("packageName") or "").strip()
    if package_name and project_name in package_name:
        # package-meta 的 packageName 含项目名（旧自动值），改用 extraction 06 的标段名称
        package_name = ""
    if not package_name:
        extraction_06 = project_dir / "extraction" / "packages" / package_id / "06-Procurement-Content.md"
        if extraction_06.is_file():
            text = extraction_06.read_text(encoding="utf-8-sig")
            match = re.search(r"(?:标段|包)名称\s*[:：]\s*(.+)", text)
            if match:
                package_name = match.group(1).strip().splitlines()[0].strip()
    if package_name and package_name not in project_name:
        return f"{project_name}（第{package_no}包：{package_name}）技术方案"
    return f"{project_name}（第{package_no}包）技术方案"


def export_tech(
    workspace: Path,
    project_dir: Path,
    *,
    out_path: str | None = None,
    profile_id: str | None = None,
    package_id: str | None = None,
    keep_history: int = 1,
    with_toc: bool = True,
) -> dict[str, Any]:
    started_at = paths.iso_now()
    started = time.monotonic()
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    package_id = str(package_id or state.get("currentPackage") or "package-1")
    package_dir = find_package_dir(project_dir, package_id)
    package_meta = jsonio.read_json(package_dir / "package-meta.json")
    event_log.log_event(
        workspace,
        "export.started",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=package_id,
        detail={"mode": "tech", "packageId": package_id},
    )
    outline_path = package_dir / "chapters" / "_outline" / "outline.json"
    if not outline_path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 chapters/_outline/outline.json", detail={"path": str(outline_path)})
    outline = jsonio.read_json(outline_path)
    style_profile = profile_id or str(package_meta.get("exportStyleProfile") or "official-tech")
    params, resolved_path, style_warnings = load_resolved_style(
        workspace=workspace,
        project_dir=project_dir,
        package_dir=package_dir,
        package_id=package_id,
        profile_id=style_profile,
    )
    if out_path:
        target = Path(out_path)
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        if target.suffix.lower() != ".docx":
            raise errors.BgError(errors.E_USAGE, "--out 必须是 .docx 路径", detail={"out": str(target)})
    else:
        target = _default_export_target(package_dir, str(project.get("projectName") or "项目"), "tech")
    builder = TechDocBuilder(params)
    package_count = int(project.get("packageCount") or 1)
    tech_title = _tech_package_title(project, package_meta, package_id, package_count, project_dir)
    builder.add_title_block(tech_title, str(project.get("projectNo") or "未提及"), started_at)
    if with_toc:
        builder.add_toc_field()
    builder.doc.core_properties.title = tech_title
    figures: dict[str, dict[str, Any]] = {}
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        for plan in node.get("figurePlan") or []:
            if isinstance(plan, Mapping) and plan.get("anchorId"):
                figures[str(plan["anchorId"])] = dict(plan)
    planned_table_count = sum(1 for plan in figures.values() if str(plan.get("type") or "") == "table")
    supplement_tables = [item for item in outline.get("supplementTables") or [] if isinstance(item, dict)]
    chapters: list[str] = []
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        title = str(node.get("title") or "").strip()
        if not title:
            continue
        level = int(node.get("level") or 1)
        builder.add_techn_heading(level, title)
        chapters.append(title)
        children = node.get("children")
        if isinstance(children, list) and children:
            continue
        file_path = package_dir / str(node.get("filePath") or "")
        if not file_path.is_file():
            builder._add_body_paragraph(f"[正文缺失：{node.get('id')}]")
            continue
        markdown = file_path.read_text(encoding="utf-8-sig")
        builder.render_tech_markdown(markdown, chapter_id=str(node.get("id") or ""), base_level=level, package_dir=package_dir, figures=figures)
    builder.save(target)
    sanitized_xml = sanitize_docx_xml(target)
    cleanup = {"removed": [], "kept": []}
    if not out_path:
        cleanup = cleanup_export_family(target.parent, ["*技术方案*.docx"], keep_history)
    relative = paths.rel_posix(project_dir, target) if _is_within(project_dir, target) else str(target)
    report = {
        "schemaVersion": "1.0",
        "mode": "tech",
        "ok": not builder.missing_figures,
        "docxPath": relative,
        "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        "exportedAt": started_at,
        "styleProfile": style_profile,
        "resolvedStylePath": paths.rel_posix(project_dir, resolved_path),
        "paragraphs": builder.counter["paragraphs"],
        "headings": builder.counter["headings"],
        "tables": builder.counter["tables"],
        "plannedTables": planned_table_count,
        "supplementTables": supplement_tables,
        "images": builder.counter.get("images", 0),
        "missingFigures": builder.missing_figures,
        "tableMarkers": builder.table_marker_warnings,
        "warnings": style_warnings,
        "tocFieldPresent": bool(with_toc),
        "pageBreakAfterToc": bool(with_toc),
        "cover": False,
        "sanitizedXml": sanitized_xml,
        "cleanup": cleanup,
        "durationMs": int((time.monotonic() - started) * 1000),
    }
    report_path = package_dir / "export" / "_last-export-report.json"
    jsonio.write_json_atomic(report_path, report)
    event_log.log_event(
        workspace,
        "export.verified",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=package_id,
        detail={"mode": "tech", "packageId": package_id, "docxPath": relative, "images": report.get("images"), "cleanupRemoved": len(cleanup.get("removed") or [])},
    )
    package_meta["exportPath"] = relative
    package_meta["lastExportAt"] = started_at
    jsonio.write_json_atomic(package_dir / "package-meta.json", package_meta)
    try:
        import bg_run_manifest

        bg_run_manifest.record_export(workspace=workspace, project_dir=project_dir, package_id=package_id, kind="tech", docx_path=target)
    except Exception:  # noqa: BLE001 - run-manifest 记录尽力而为
        pass
    return {
        "ok": True,
        "command": "export_docx",
        "mode": "tech",
        "projectId": str(project.get("projectId") or ""),
        "packageId": package_id,
        "docxPath": relative,
        "absolutePath": str(target),
        "exportedAt": started_at,
        "cleanup": cleanup,
        "styleProfile": style_profile,
        "resolvedStylePath": report["resolvedStylePath"],
        "chapters": chapters,
        "paragraphs": report["paragraphs"],
        "headings": report["headings"],
        "tables": report["tables"],
        "plannedTables": report.get("plannedTables"),
        "supplementTables": report.get("supplementTables") or [],
        "images": report["images"],
        "missingFigures": report["missingFigures"],
        "tableMarkers": report.get("tableMarkers") or [],
        "warnings": report["warnings"],
        "tocFieldPresent": bool(report.get("tocFieldPresent")),
        "pageBreakAfterToc": bool(report.get("pageBreakAfterToc")),
        "durationMs": report["durationMs"],
        "reportPath": paths.rel_posix(project_dir, report_path),
    }


# ---------------------------------------------------------------------------
# 原子保存
# ---------------------------------------------------------------------------
def _atomic_save_document(document: Any, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{paths.rand4()}.docx")
    try:
        document.save(str(tmp))
        deadline = time.monotonic() + 2.0
        delay = 0.02
        while True:
            try:
                os.replace(tmp, target)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 提取文件收集
# ---------------------------------------------------------------------------
def _load_templates() -> dict[str, dict[str, Any]]:
    import bg_extract_validate as extract_validate

    manifest = extract_validate.load_template_manifest()
    return manifest["_by_id"]


def collect_sections(project_dir: Path, package_count: int) -> list[dict[str, Any]]:
    """按「公共 01–05 → 每包 06–09」收集提取文件与章节标题。"""
    by_id = _load_templates()
    sections: list[dict[str, Any]] = []
    for template_id in COMMON_TEMPLATE_IDS:
        template = by_id[template_id]
        file_path = project_dir / "extraction" / "common" / str(template.get("fileName"))
        sections.append(
            {
                "templateId": template_id,
                "title": str(template.get("title") or template_id),
                "path": file_path,
                "packageId": "",
            }
        )
    for number in range(1, package_count + 1):
        package_id = f"package-{number}"
        for template_id in PACKAGE_TEMPLATE_IDS:
            template = by_id[template_id]
            file_path = project_dir / "extraction" / "packages" / package_id / str(template.get("fileName"))
            sections.append(
                {
                    "templateId": template_id,
                    "title": str(template.get("title") or template_id),
                    "path": file_path,
                    "packageId": package_id,
                }
            )
    return sections


def _split_frontmatter(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip() == "---":
        for index in range(1, min(len(lines), 40)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1 :])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 文档 XML 终检：颜色 auto + 显式字体（M8）
# ---------------------------------------------------------------------------
_COLOR_RE = re.compile(r"<w:color\b[^>]*?/>")
_RFONTS_RE = re.compile(r"<w:rFonts\b[^>]*?/>")
_THEME_ATTR_RE = re.compile(r'\s+w:(?:asciiTheme|eastAsiaTheme|hAnsiTheme|cstheme)="[^"]*"')
_FORBIDDEN_COLOR_RE = re.compile(r'w:val="(?:000000|1F4E79|0070C0|1F497D|4F81BD)"')


def sanitize_docx_xml(path: Path) -> dict[str, int]:
    """重写 word/*.xml：所有 w:color→auto；显式字体 rFonts 去主题属性（避免 Word 用主题字体覆盖）。"""
    import zipfile

    stats = {"colorsRewritten": 0, "themefontsRemoved": 0, "forbiddenColors": 0}
    with zipfile.ZipFile(path, "r") as source:
        items = [(info, source.read(info.filename)) for info in source.infolist()]
    updated_items: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in items:
        name = info.filename
        if name.startswith("word/") and name.endswith(".xml"):
            text = data.decode("utf-8")
            stats["forbiddenColors"] += len(_FORBIDDEN_COLOR_RE.findall(text))

            def replace_color(_match: Any) -> str:
                stats["colorsRewritten"] += 1
                return '<w:color w:val="auto"/>'

            def replace_rfonts(match: Any) -> str:
                tag = match.group(0)
                if 'w:ascii="' in tag or 'w:eastAsia="' in tag:
                    cleaned, count = _THEME_ATTR_RE.subn("", tag)
                    stats["themefontsRemoved"] += count
                    return cleaned
                return tag

            text = _COLOR_RE.sub(replace_color, text)
            text = _RFONTS_RE.sub(replace_rfonts, text)
            data = text.encode("utf-8")
        updated_items.append((info, data))
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info, data in updated_items:
            target.writestr(info, data)
    temp_path.replace(path)
    return stats


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_keyinfo(workspace: Path, project_dir: Path, *, out_path: str | None = None, keep_history: int = 1) -> dict[str, Any]:
    """生成人读关键信息 docx 并回填 project.json.keyInfoExport。"""
    project_file = project_dir / "project.json"
    project = jsonio.read_json(project_file)
    if not isinstance(project, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "project.json 必须是 JSON 对象")
    schema.raise_if_errors(schema.validate_project(project), filename="project.json")
    package_count = project.get("packageCount")
    if not isinstance(package_count, int) or isinstance(package_count, bool) or package_count < 1:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "project.json.packageCount 未回填，无法合并九大类导出",
            hint="先完成 F3 提取校验并回填分包结论（packageCount ≥1）",
            detail={"packageCount": package_count},
        )
    sections = collect_sections(project_dir, package_count)
    missing = [paths.rel_posix(project_dir, section["path"]) for section in sections if not section["path"].is_file()]
    if missing:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"提取文件缺失 {len(missing)} 个，无法导出关键信息",
            hint="先运行 bg_extract_validate.py 修复缺失文件",
            detail={"missing": missing},
        )
    project_name = str(project.get("projectName") or "").strip() or "未命名项目"
    project_no = str(project.get("projectNo") or "").strip() or "未提及"
    now = paths.iso_now()
    keyinfo_profile = "official-keyinfo"
    resolved_path = None
    resolved_warnings: list[str] = []
    try:
        current_state = state_lib.load_state(project_dir)
        current_package = str(current_state.get("currentPackage") or "")
        first_package = find_package_dir(project_dir, current_package or None)
        package_meta = jsonio.read_json(first_package / "package-meta.json")
        user_settings = jsonio.read_json(workspace / "dsh" / "config" / "user-settings.json") if (workspace / "dsh" / "config" / "user-settings.json").is_file() else {}
        style_profiles = user_settings.get("styleProfiles") if isinstance(user_settings, Mapping) else {}
        keyinfo_profile = str((style_profiles or {}).get("keyInfo") or "official-keyinfo")
        params, resolved_path, resolved_warnings = load_resolved_style(
            workspace=workspace,
            project_dir=project_dir,
            package_dir=first_package,
            package_id=str(package_meta.get("packageId") or "package-1"),
            profile_id=keyinfo_profile,
        )
    except (errors.BgError, OSError):
        params = load_style_params()
    builder = KeyInfoDocBuilder(params)
    builder.add_title_block(project_name, project_no, now)

    chapters: list[str] = []
    for section in sections:
        title = strip_heading_numbering(str(section["title"]))
        suffix = f"（{section['packageId']}）" if package_count > 1 and section["packageId"] else ""
        chapter_title = title + suffix
        builder.add_chapter(chapter_title)
        chapters.append(chapter_title)
        raw_text = Path(section["path"]).read_text(encoding="utf-8-sig")
        body = _split_frontmatter(raw_text)
        builder.render_markdown(body, base_dir=Path(section["path"]).parent)

    if out_path:
        target = Path(out_path)
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        if target.suffix.lower() != ".docx":
            raise errors.BgError(errors.E_USAGE, "--out 必须是 .docx 路径", detail={"out": str(target)})
    else:
        exports_dir = project_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        safe_name = paths.safe_component(project_name) or "项目"
        target = exports_dir / f"{safe_name}-关键信息-{paths.timestamp14()}.docx"
        if target.exists():
            target = exports_dir / f"{safe_name}-关键信息-{paths.timestamp14()}-{paths.rand4()}.docx"
    builder.save(target)
    sanitized_xml = sanitize_docx_xml(target)
    cleanup = {"removed": [], "kept": []}
    cleanup = cleanup_export_family(target.parent, ["*关键信息*.docx"], keep_history)
    relative = paths.rel_posix(project_dir, target) if _is_within(project_dir, target) else str(target)

    key_info_export = {"path": relative, "exportedAt": now, "docxStyleProfile": EXPORT_STYLE_PROFILE}
    project["keyInfoExport"] = key_info_export
    project["updatedAt"] = now
    schema.raise_if_errors(schema.validate_project(project), filename="project.json")
    jsonio.write_json_atomic(project_file, project)
    chapters_by_class: dict[str, int] = {}
    for section in sections:
        key = str(section.get("packageId") or "common")
        chapters_by_class[key] = chapters_by_class.get(key, 0) + 1
    covered_templates = sorted({str(section.get("templateId") or "") for section in sections if section.get("templateId")})
    return {
        "ok": True,
        "command": "export_docx",
        "mode": EXPORT_STYLE_PROFILE,
        "projectId": str(project.get("projectId") or ""),
        "projectName": project_name,
        "projectNo": project_no,
        "docxPath": relative,
        "absolutePath": str(target),
        "exportedAt": now,
        "cleanup": cleanup,
        "chapterCount": len(chapters),
        "chaptersByClass": chapters_by_class,
        "templatesCovered": f"{len(covered_templates)}/9",
        "chapters": chapters,
        "paragraphs": builder.counter["paragraphs"],
        "headings": builder.counter["headings"],
        "tables": builder.counter["tables"],
        "numbering": {
            "enabled": bool(params.get("headingNumbering", True)),
            "numId": builder._num_id,
            "scheme": params.get("headingNumberingScheme"),
        },
        "fonts": {
            "body": params.get("fontFamily"),
            "bodySizePt": params.get("fontSizePt"),
            "lineSpacing": params.get("lineSpacing"),
            "heading": params.get("headingFontFamily"),
            "headingSizesPt": params.get("headingSizesPt"),
        },
        "tableStyle": table_config(params),
        "tableWidthsCm": builder.counter.get("tableWidths", [])[:10],
        "sanitized": {
            "dropped": builder.counter["dropped"],
            "masked": builder.counter["masked"],
            "droppedSamples": list(builder.counter.get("droppedSamples") or []),
        },
        "sanitizedXml": sanitized_xml,
        "keyInfoExport": key_info_export,
        "resolvedStylePath": paths.rel_posix(project_dir, resolved_path) if resolved_path is not None else "",
        "changed": [paths.rel_posix(project_dir, project_file)],
    }


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """脚本自检：人读 docx（Heading1=9/无机器表/原生多级编号/字体/表格/公开联系信息）+ 回填。"""
    import platform

    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-export-v2-selftest-"))
    try:
        import bg_extract_validate as extract_validate

        project_dir = tmp / "project"
        extract_validate.build_selftest_project(project_dir)
        workspace = tmp / "ws"
        (workspace / "dsh" / "logs").mkdir(parents=True)
        result = export_keyinfo(workspace, project_dir)
        checks.append("export")
        target = project_dir / result["docxPath"]
        if not target.is_file() or target.stat().st_size <= 0:
            raise AssertionError("docx 未生成")
        from docx import Document
        from docx.enum.table import WD_ALIGN_VERTICAL
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn

        document = Document(str(target))
        headings = [paragraph for paragraph in document.paragraphs if paragraph.style.name.startswith("Heading")]
        h1 = [paragraph for paragraph in headings if paragraph.style.name == "Heading 1"]
        if len(h1) != 9:
            raise AssertionError(f"Heading 1 数量应为 9，实际 {len(h1)}")
        checks.append("heading1_9")
        texts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    texts.append(cell.text)
        full_text = "\n".join(texts)
        for marker in ("字段键", "字段值", "字段名", "证据（来源"):
            if marker in full_text:
                raise AssertionError(f"docx 中出现机器表头：{marker}")
        checks.append("no_machine_table")
        for paragraph in headings:
            text = paragraph.text.strip()
            if re.match(r"^(?:[一二三四五六七八九十]+[、.．]|\d+(?:\.\d+)*[、.．]|第[一二三四五六七八九十\d]+[章节])", text):
                raise AssertionError(f"标题仍含硬编码编号：{text!r}")
        checks.append("no_hardcoded_numbering")
        # 原生多级编号：numbering.xml + Heading 1/2 样式 numPr
        numbering = document.part.numbering_part.element
        abstract = numbering.findall(qn("w:abstractNum"))
        target_abstract = None
        for candidate in abstract:
            candidate_levels = candidate.findall(qn("w:lvl"))
            candidate_texts = [
                level.find(qn("w:lvlText")).get(qn("w:val"))
                for level in candidate_levels
                if level.find(qn("w:lvlText")) is not None
            ]
            if candidate_texts and candidate_texts[0] == "第%1章":
                target_abstract = candidate
                break
        if target_abstract is None:
            raise AssertionError("numbering.xml 未找到 keyinfo 多级编号定义（abstractNum）")
        levels = target_abstract.findall(qn("w:lvl"))
        if len(levels) < 3 or not all(level.find(qn("w:lvlText")) is not None for level in levels):
            raise AssertionError("多级编号 lvl/lvlText 不完整")
        lvl_texts = [level.find(qn("w:lvlText")).get(qn("w:val")) for level in levels]
        if lvl_texts[0] != "第%1章" or lvl_texts[1] != "%1.%2":
            raise AssertionError(f"多级编号格式不正确：{lvl_texts[:3]}")
        for level in (1, 2, 3):
            style = document.styles[f"Heading {level}"]
            num_pr = style.element.find(qn("w:pPr")).find(qn("w:numPr"))
            if num_pr is None or num_pr.find(qn("w:numId")) is None:
                raise AssertionError(f"Heading {level} 未绑定多级编号")
        checks.append("native_multilevel_numbering")
        # 字体/字号/行距
        body_paragraph = next(
            (paragraph for paragraph in document.paragraphs if paragraph.text.startswith("本项目采用线上投标方式")),
            None,
        )
        if body_paragraph is None:
            raise AssertionError("未找到正文字体断言样本段落")
        run = body_paragraph.runs[0]
        rpr = run._r.find(qn("w:rPr"))
        rfonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
        if (
            rfonts is None
            or rfonts.get(qn("w:eastAsia")) != "宋体"
            or rfonts.get(qn("w:ascii")) != "Times New Roman"
            or abs(float(run.font.size.pt) - 12.0) > 0.01
        ):
            raise AssertionError(
                f"正文字体/字号不正确：eastAsia={rfonts.get(qn('w:eastAsia')) if rfonts is not None else None}/"
                f"ascii={rfonts.get(qn('w:ascii')) if rfonts is not None else None}/{run.font.size}"
            )
        if abs(float(body_paragraph.paragraph_format.line_spacing) - 1.5) > 0.01:
            raise AssertionError(f"正文行距不正确：{body_paragraph.paragraph_format.line_spacing}")
        checks.append("body_font")
        # 表格表头加粗 + 灰底
        if not document.tables:
            raise AssertionError("docx 无表格")
        header_cell = document.tables[0].rows[0].cells[0]
        if not header_cell.paragraphs[0].runs[0].bold:
            raise AssertionError("表头未加粗")
        shading = header_cell._tc.get_or_add_tcPr().find(qn("w:shd"))
        if shading is None or shading.get(qn("w:fill")) != "D9D9D9":
            raise AssertionError("表头未设置灰底 D9D9D9")
        checks.append("table_header_style")
        # M3.3：表格自适应列宽（短列窄）+ 单元格水平/垂直居中
        numbered_table = next(
            (
                table
                for table in document.tables
                if table.rows and table.rows[0].cells[0].text.strip() == "序号"
            ),
            None,
        )
        if numbered_table is None:
            raise AssertionError("未找到含「序号」列的表格")
        header_widths = [cell.width for cell in numbered_table.rows[0].cells]
        if not header_widths[0] or header_widths[0].pt >= header_widths[1].pt:
            raise AssertionError(f"序号列未明显变窄：{[width.pt if width else None for width in header_widths]}")
        sample_cells = [cell for table in document.tables[:5] for row in table.rows[:2] for cell in row.cells]
        if not all(paragraph.alignment == WD_ALIGN_PARAGRAPH.CENTER for cell in sample_cells for paragraph in cell.paragraphs):
            raise AssertionError("单元格文字未水平居中")
        if not all(cell.vertical_alignment == WD_ALIGN_VERTICAL.CENTER for cell in sample_cells):
            raise AssertionError("单元格未垂直居中")
        checks.append("table_autofit_center")
        # 公开联系信息不脱敏
        if "0571-12345678" not in full_text or "zb@example.com" not in full_text:
            raise AssertionError("公开联系信息被脱敏或丢失")
        if "TEST-2026-001" not in full_text or "***" in full_text:
            raise AssertionError("公开项目编号被误掩码（P1-1 回归）")
        checks.append("public_contact_preserved")
        counter = {"dropped": 0, "masked": 0}
        public_identifier = _sanitize_inline("项目编号：4210832026000678", counter)
        public_table_value = _sanitize_inline("4210832026000678", counter, context="项目属性 项目编号")
        private_id = _sanitize_inline("身份证号：110101199001011234", counter)
        private_bank = _sanitize_inline("4210832026000678", counter, context="银行账号")
        private_phone = _sanitize_inline("联系电话：0716-1234567", counter)
        if "***" in public_identifier or "***" in public_table_value or "***" in private_phone:
            raise AssertionError("公开标识/公开电话被误掩码")
        if "***" not in private_id or "***" not in private_bank:
            raise AssertionError("私密号码未被掩码")
        checks.append("masking_rules")
        # 无封面/目录/页眉页脚
        if any(paragraph.text.strip() == "目录" for paragraph in document.paragraphs):
            raise AssertionError("docx 出现目录")
        if any("TOC" in (node.text or "") for node in document.element.xpath(".//w:instrText")):
            raise AssertionError("docx 含 TOC 域")
        for section in document.sections:
            header_text = "\n".join(paragraph.text for paragraph in section.header.paragraphs).strip()
            footer_text = "\n".join(paragraph.text for paragraph in section.footer.paragraphs).strip()
            if header_text or footer_text:
                raise AssertionError("docx 存在页眉/页脚内容")
        checks.append("no_cover_toc_header_footer")
        project = jsonio.read_json(project_dir / "project.json")
        if not project.get("keyInfoExport", {}).get("path"):
            raise AssertionError("project.json.keyInfoExport 未回填")
        checks.append("backfill")
        try:
            export_keyinfo(workspace, project_dir, out_path=str(tmp / "x.pdf"))
            raise AssertionError("--out 非 docx 未报错")
        except errors.BgError as exc:
            if exc.code != errors.E_USAGE:
                raise AssertionError(f"--out 错误码不正确：{exc.code}")
        checks.append("out_validation")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_export_docx.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_export_docx.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（CONTRACT §10.4 + H.1 #15 兼容参数）。"""
    parser = errors.CliParser(
        prog="bg_export_docx.py",
        description="BidGenie M3.2 关键信息 docx 导出（人读九章、Word 原生多级编号、黑体标题/宋体正文）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mode", choices=["keyinfo", "tech"], help="导出模式（M3 仅 keyinfo）")
    mode.add_argument("--profile", dest="mode", choices=["keyinfo", "tech"], help="--mode 的兼容别名（H.1）")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--out", help="输出 docx 路径（缺省 exports/<项目名>-关键信息-<ts14>.docx）")
    parser.add_argument("--run", action="store_true", help="执行导出（默认动作，H.1 兼容）")
    parser.add_argument("--keep-history", type=int, default=1, help="默认导出目录保留历史份数（默认 1，只留最新）")
    parser.add_argument("--with-toc", action="store_true", help="tech 模式：插入 Word TOC 域（默认已插入，此参数保留兼容）")
    parser.add_argument("--no-toc", action="store_true", help="tech 模式：不插入 Word TOC 域（招标文件明确不要目录时使用）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    mode = str(getattr(args, "mode", None) or "keyinfo")
    if mode not in ("tech", "keyinfo"):
        raise errors.BgError(errors.E_USAGE, f"未知导出模式：{mode}", hint="可用：tech / keyinfo")
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if mode == "tech":
        return export_tech(workspace, project_dir, out_path=getattr(args, "out", None), profile_id=None, package_id=getattr(args, "package", None), keep_history=int(getattr(args, "keep_history", 1) or 1), with_toc=not bool(getattr(args, "no_toc", False)))
    return export_keyinfo(workspace, project_dir, out_path=getattr(args, "out", None), keep_history=int(getattr(args, "keep_history", 1) or 1))


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_export_docx.py",
    )


if __name__ == "__main__":
    sys.exit(main())
