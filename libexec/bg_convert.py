#!/usr/bin/env python3
"""bg_convert.py：招标文件 → Markdown 转换器（M3，CONTRACT §10.1 / PRD F3.2）。

用法：
    python bg_convert.py --workspace <ws> --project <dir|id> --files <a[,b...]> [--json]
    python bg_convert.py --workspace <ws> --project <dir|id> --file <a> [--file <b>] [--json]
    python bg_convert.py --workspace <ws> --project <dir|id> --list [--json]
    python bg_convert.py --workspace <ws> --project <dir|id> --files <...> --check [--json]
    python bg_convert.py --selftest [--json]

支持：`.docx`（python-docx → 标题/段落/表格/文本框/图片 → GFM；文本框 `w:txbxContent` 按文档序并入正文）、`.doc`（Word COM 转 docx 再解析；
无 COM 时尝试 soffice；都不可用 → 该文件标记 failed 并提示另存 `.docx`）、`.pdf`（pymupdf4llm，
pdfplumber 复核表格：≤60 页全量、更大文件回退页优先 + 均匀抽样分批限时复核；扫描件标记 `scanned_unsupported`）、`.md`/`.txt`（直转/复制）。

PDF 逐页覆盖（P0-2 / P2-11）：pymupdf4llm 整页丢失时按行回退补齐，并把回退页写入
manifest（`fallbackPages/fallbackPageCount/fallbackDetail/keyDataFromFallbackOnly/fallbackNote`
+ warning `pdf_pages_fallback:<首页>` / `key_data_from_fallback_only:<首页>` /
`pdf_pages_fallback_count:<页数>` / `pdf_pages_fallback_list:<全部页码>`），供 F3 汇报显式提示
「关键数据仅来自回退文本」。冒号后是页码，不是回退页数量。

输出（CONTRACT §10.1）：
- `<project>/sources/converted/<原文件名>.md`
- `<project>/sources/conversion-manifest.json`
  `{schemaVersion, generatedAt, converter, files:[{sourcePath, sourceName, sourceType, sizeBytes,
    convertedPath, mdChars, headings, tables, warnings, status}]}`（附加 `hash/quality/images/mtime/
    pageCoverage/fallbackPages/fallbackPageCount/keyDataFromFallbackOnly/fallbackNote/tableRecheck`）

约束：不联网、不改源文件；失败不写半成品（临时文件 + os.replace）；重复转换幂等
（未变化 → skipped 不改 mtime；变化 → 先备份到 `sources/.backup-<ts14>/` 再覆盖）；单文件 300s 预算。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import deps, errors, jsonio, logging as event_log, paths, state as state_lib, tempdir  # noqa: E402

SCHEMA_VERSION = "1.0"
MANIFEST_REL = "sources/conversion-manifest.json"
CONVERTED_DIR_REL = "sources/converted"
ASSETS_DIR_REL = "sources/assets"
BACKUP_DIR_PREFIX = ".backup-"

SUPPORTED_TYPES = ("docx", "doc", "pdf", "md", "txt")
PER_FILE_TIMEOUT_SEC = 300.0
DOC_COM_TIMEOUT_SEC = 120.0
PDF_MIN_TEXT_CHARS = 50
MAX_SOURCE_BYTES = 200 * 1024 * 1024
BACKUP_KEEP = 2
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp"}

# PDF 表格复核策略（P2-11）：≤PDF_TABLE_RECHECK_FULL_MAX_PAGES 全量复核；更大文件改为
# 回退页优先 + 均匀抽样的分批复核（限页数 + 限时预算），超时/失败只降级记录，不中断转换。
PDF_TABLE_RECHECK_FULL_MAX_PAGES = 60
PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES = 60
PDF_TABLE_RECHECK_BATCH_SIZE = 10
PDF_TABLE_RECHECK_BUDGET_SEC = 60.0
PDF_LARGE_PDF_PAGES = 200
#: manifest 中回退页名单最多保留条数（完整覆盖数据仍在 pageCoverage 里）
PDF_FALLBACK_LIST_LIMIT = 50

_HEADING_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)
_BULLET_STYLES = ("list bullet", "list paragraph", "项目符号", "列表段落")
_CAPTION_HINTS = ("caption", "题注")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _norm_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _safe_stem(name: str) -> str:
    return paths.safe_component(Path(name).stem)[:80] or "converted"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except Exception:  # noqa: BLE001 - 版本仅用于诊断
        return "unknown"


def _count_headings(md: str) -> int:
    return sum(1 for line in md.splitlines() if re.match(r"^#{1,6}\s+\S", line))


def _count_tables(md: str) -> int:
    """统计 GFM 表格块（表头行 + 分隔行）。"""
    lines = md.splitlines()
    count = 0
    for index in range(len(lines) - 1):
        line = lines[index].strip()
        nxt = lines[index + 1].strip()
        if line.startswith("|") and line.count("|") >= 2 and re.match(r"^\|?[\s:|-]+\|?$", nxt) and "-" in nxt:
            count += 1
    return count


def _garbled_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = 0
    for char in text:
        code = ord(char)
        if char == "\ufffd" or (code < 0x20 and char not in "\n\t"):
            bad += 1
    return bad / len(text)


def _quality_for(md: str) -> tuple[str, list[str]]:
    chars = len(md.strip())
    warnings: list[str] = []
    ratio = _garbled_ratio(md)
    if chars == 0:
        return "poor", ["empty_output"]
    if ratio > 0.01:
        warnings.append("garbled_text")
        return "poor", warnings
    if chars >= 2000:
        return "good", warnings
    if chars >= 200:
        return "fair", warnings
    warnings.append("text_short")
    return "poor", warnings


def _silence_stdout() -> contextlib.AbstractContextManager[io.StringIO]:
    """转换库可能向 stdout 打印进度；--json 模式必须保证 stdout 只有 JSON。"""
    return contextlib.redirect_stdout(io.StringIO())


def _run_with_timeout(func: Callable[..., Any], timeout: float, *args: Any, **kwargs: Any) -> Any:
    """在守护线程中执行转换并限时（超时抛 TimeoutError；线程无法强杀，仅作预算控制）。"""
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 转交给调用线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="bg-convert", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"转换超过 {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _read_text_source(path: Path) -> tuple[str, list[str]]:
    """读取 md/txt 源（UTF-8 优先，兼容 GB18030；返回归一化文本与警告）。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    warnings: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
            warnings.append("source_encoding_gb18030")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            warnings.append("decode_replaced")
    return _norm_newlines(text), warnings


def _txt_to_markdown(text: str) -> str:
    """txt 直转：按空行分段；段内中文行直接拼接，ASCII 词之间补空格。"""
    blocks: list[str] = []
    buffer: list[str] = []
    for line in _norm_newlines(text).split("\n"):
        if line.strip():
            buffer.append(line.strip())
            continue
        if buffer:
            blocks.append(_join_lines(buffer))
            buffer = []
    if buffer:
        blocks.append(_join_lines(buffer))
    return "\n\n".join(block for block in blocks if block.strip()) + "\n"


def _join_lines(lines: list[str]) -> str:
    result = lines[0]
    for line in lines[1:]:
        prev = result[-1] if result else ""
        if prev and (ord(prev) > 0x2E80 or ord(line[0]) > 0x2E80 if line else False):
            result += line
        else:
            result += " " + line
    return result


# ---------------------------------------------------------------------------
# docx → markdown
# ---------------------------------------------------------------------------
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
#: w:sym 字符映射（Wingdings/Symbol 常见复选框/勾叉；未知字符输出占位并告警）
_SYMBOL_MAP: dict[str, str] = {
    "f0fe": "☑",
    "f0fd": "☐",
    "f0fc": "✓",
    "f0fb": "✗",
    "f0a8": "□",
    "f0a3": "☑",
    "f06f": "□",
    "f071": "☑",
    "f072": "□",
    "f0d8": "√",
    "f0d7": "×",
    "221a": "√",
    "00d7": "×",
    "2713": "✓",
    "2717": "✗",
    "2611": "☑",
    "2610": "☐",
}
_CHECKBOX_GLYPHS = set("☑☐☒□✓√×✗◻◼")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _run_bold(run_element: Any) -> bool:
    from docx.oxml.ns import qn

    element = run_element.find(qn("w:rPr"))
    if element is None:
        return False
    bold = element.find(qn("w:b"))
    if bold is None:
        return False
    return (bold.get(qn("w:val")) or "1") not in ("0", "false")


def _run_italic(run_element: Any) -> bool:
    from docx.oxml.ns import qn

    element = run_element.find(qn("w:rPr"))
    if element is None:
        return False
    italic = element.find(qn("w:i"))
    if italic is None:
        return False
    return (italic.get(qn("w:val")) or "1") not in ("0", "false")


def _paragraph_text(paragraph: Any, *, markdown_emphasis: bool = False, stats: dict[str, Any] | None = None) -> str:
    """按文档序提取段落文本：保留符号/复选框状态，跳过文本框内容（由 textbox 逻辑处理）。"""
    from docx.oxml.ns import qn

    stats = stats if stats is not None else {}
    stats.setdefault("symbolsMapped", 0)
    stats.setdefault("symbolsUnmapped", 0)
    stats.setdefault("checkboxesPreserved", 0)
    field_stack: list[dict[str, Any]] = []
    pending_skip_glyph = [False]

    def checkbox_checked(element: Any) -> bool:
        checked = element.find(qn("w:checked"))
        if checked is not None:
            return (checked.get(qn("w:val")) or "1") not in ("0", "false")
        default = element.find(qn("w:default"))
        if default is not None:
            return (default.get(qn("w:val")) or "1") not in ("0", "false")
        return False

    def walk(node: Any) -> str:
        pieces: list[str] = []
        for child in list(node):
            local = _local_name(child.tag)
            if local == "txbxContent":
                continue
            if local == "r":
                inner = walk(child)
                if inner:
                    if markdown_emphasis:
                        bold = _run_bold(child)
                        italic = _run_italic(child)
                        if bold and italic:
                            inner = f"***{inner}***"
                        elif bold:
                            inner = f"**{inner}**"
                        elif italic:
                            inner = f"*{inner}*"
                    pieces.append(inner)
                continue
            if local == "sym":
                code = (child.get(qn("w:char")) or "").lower()
                mapped = _SYMBOL_MAP.get(code) or _SYMBOL_MAP.get(code[-2:] if len(code) >= 2 else code)
                if mapped:
                    stats["symbolsMapped"] += 1
                    pieces.append(mapped)
                else:
                    stats["symbolsUnmapped"] += 1
                    pieces.append(f"[符号:{code or '?'}]")
                pending_skip_glyph[0] = True
                continue
            if local == "checkbox" and child.tag.startswith("{" + _W14_NS + "}"):
                checked_element = child.find("{" + _W14_NS + "}checked")
                checked = checked_element is not None and (checked_element.get("{" + _W14_NS + "}val") or "1") not in (
                    "0",
                    "false",
                )
                stats["checkboxesPreserved"] += 1
                pieces.append("☑" if checked else "☐")
                pending_skip_glyph[0] = True
                continue
            if local == "checkBox":
                stats["checkboxesPreserved"] += 1
                pieces.append("☑" if checkbox_checked(child) else "☐")
                if field_stack:
                    field_stack[-1]["has_checkbox"] = True
                pending_skip_glyph[0] = True
                continue
            if local == "fldChar":
                field_type = child.get(qn("w:fldCharType"))
                if field_type == "begin":
                    field_stack.append({"has_checkbox": False, "skip_result": False})
                elif field_type == "separate" and field_stack:
                    field_stack[-1]["skip_result"] = bool(field_stack[-1].get("has_checkbox"))
                elif field_type == "end" and field_stack:
                    field_stack.pop()
                continue
            if local == "t":
                text = child.text or ""
                if field_stack and field_stack[-1].get("skip_result"):
                    continue
                stripped = text.strip()
                if pending_skip_glyph[0] and stripped and set(stripped) <= _CHECKBOX_GLYPHS:
                    pending_skip_glyph[0] = False
                    continue
                pending_skip_glyph[0] = False
                if text:
                    pieces.append(text)
                continue
            if local in ("drawing", "pict", "object", "oleObject"):
                # 图片/文本框/嵌入对象由文档级扫描与其他分支处理，不在此递归
                continue
            pieces.append(walk(child))
        return "".join(pieces)

    return walk(paragraph._p).strip()


def _heading_level(style_name: str) -> int:
    name = (style_name or "").strip().lower()
    if not name:
        return 0
    if name in ("title", "标题"):
        return 1
    match = _HEADING_RE.match(name)
    if match:
        return min(int(match.group(1)), 6)
    return 0


def _inline_text(paragraph: Any, *, stats: dict[str, Any] | None = None) -> str:
    return _paragraph_text(paragraph, markdown_emphasis=True, stats=stats)


def _textbox_content_list(element: Any) -> list[Any]:
    """收集元素下所有 `w:txbxContent`（文本框）内容节点。"""
    from docx.oxml.ns import qn

    try:
        return element.findall(".//" + qn("w:txbxContent"))
    except Exception:  # noqa: BLE001 - 非 XML 元素/解析异常时视为无文本框
        return []


def _textbox_text(content: Any) -> tuple[str, int]:
    """提取单个文本框文本（段落按行），返回 (文本, 原始 w:t 字符数)。"""
    from docx.oxml.ns import qn

    paragraphs: list[str] = []
    raw_chars = 0
    for paragraph in content.iter(qn("w:p")):
        runs = [node.text or "" for node in paragraph.iter(qn("w:t"))]
        raw_chars += sum(len(text) for text in runs)
        text = "".join(runs).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs), raw_chars


def _cell_text(cell: Any, textbox_sink: Any = None, stats: dict[str, Any] | None = None) -> str:
    parts = [
        _paragraph_text(paragraph, markdown_emphasis=False, stats=stats)
        for paragraph in cell.paragraphs
        if _paragraph_text(paragraph, markdown_emphasis=False, stats=stats).strip()
    ]
    if textbox_sink is not None:
        parts.extend(textbox_sink(cell._tc))
    text = " ".join(part for part in parts if part)
    return text.replace("|", "\\|").strip()


def _table_to_markdown(
    table: Any,
    *,
    textbox_sink: Any = None,
    stats: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """表格→GFM；合并单元格（gridSpan/vMerge）只保留首格内容并记录位置 warning（结构已展平）。"""
    from docx.oxml.ns import qn

    warnings: list[str] = []
    stats = stats if stats is not None else {}
    stats.setdefault("tableSeq", 0)
    stats.setdefault("mergedCellsFlattened", 0)
    table_number = int(stats["tableSeq"]) + 1
    stats["tableSeq"] = table_number
    rows: list[list[str]] = []
    merge_details: list[str] = []
    previous_texts: list[str] = []
    for row_index, row in enumerate(table.rows):
        seen_tcs: set[int] = set()
        cells: list[str] = []
        row_texts: list[str] = []
        for column_index, cell in enumerate(row.cells):
            tc = cell._tc
            tc_id = id(tc)
            tc_pr = tc.find(qn("w:tcPr"))
            grid_span = 1
            v_merge = None
            if tc_pr is not None:
                grid_span_element = tc_pr.find(qn("w:gridSpan"))
                if grid_span_element is not None:
                    grid_span = int(grid_span_element.get(qn("w:val")) or 1)
                v_merge_element = tc_pr.find(qn("w:vMerge"))
                if v_merge_element is not None:
                    v_merge = v_merge_element.get(qn("w:val")) or "continue"
            if tc_id in seen_tcs:
                # 同一 tc 在 gridSpan 范围内重复出现：内容已在首格保留，展平为空
                stats["mergedCellsFlattened"] += 1
                if len(merge_details) < 8:
                    merge_details.append(
                        f"merged_cells_flattened:table{table_number}:row{row_index + 1}:col{column_index + 1}:gridSpan={grid_span}"
                    )
                cells.append("")
                row_texts.append("")
                continue
            seen_tcs.add(tc_id)
            text = _cell_text(cell, textbox_sink, stats)
            if v_merge == "continue":
                stats["mergedCellsFlattened"] += 1
                if len(merge_details) < 8:
                    merge_details.append(
                        f"merged_cells_flattened:table{table_number}:row{row_index + 1}:col{column_index + 1}:vMerge=continue"
                    )
                above = previous_texts[column_index] if column_index < len(previous_texts) else ""
                if not text or text == above:
                    text = ""
            cells.append(text)
            row_texts.append(text)
        rows.append(cells)
        previous_texts = row_texts
    if stats["mergedCellsFlattened"]:
        warnings.append("merged_cells_flattened")
        warnings.extend(merge_details)
    if not rows:
        return "", warnings
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(normalized[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n", warnings


class _NumberingResolver:
    """解析 docx numbering.xml，提供有序/无序列表标记（PRD F3.2 Step 3：不能全输出 1.）。"""

    def __init__(self, document: Any) -> None:
        self._levels: dict[int, dict[int, str]] = {}
        self._counters: dict[int, dict[int, int]] = {}
        try:
            numbering_part = document.part.numbering_part
        except Exception:  # noqa: BLE001 - 文档无 numbering part
            return
        try:
            from docx.oxml.ns import qn

            root = numbering_part.element
            abstract: dict[str, dict[int, str]] = {}
            for element in root.findall(qn("w:abstractNum")):
                abstract_id = element.get(qn("w:abstractNumId")) or ""
                levels: dict[int, str] = {}
                for level in element.findall(qn("w:lvl")):
                    ilvl = int(level.get(qn("w:ilvl")) or 0)
                    num_fmt = level.find(qn("w:numFmt"))
                    levels[ilvl] = (num_fmt.get(qn("w:val")) if num_fmt is not None else "decimal") or "decimal"
                abstract[abstract_id] = levels
            for element in root.findall(qn("w:num")):
                num_id = element.get(qn("w:numId"))
                abstract_ref = element.find(qn("w:abstractNumId"))
                abstract_id = abstract_ref.get(qn("w:val")) if abstract_ref is not None else None
                if num_id is not None and abstract_id is not None:
                    self._levels[int(num_id)] = abstract.get(abstract_id, {})
        except Exception:  # noqa: BLE001 - numbering 解析失败时降级为纯文本
            self._levels = {}

    def marker(self, paragraph: Any, style_name: str) -> tuple[str | None, int]:
        try:
            p_pr = paragraph._p.pPr
            num_pr = p_pr.numPr if p_pr is not None else None
        except Exception:  # noqa: BLE001
            num_pr = None
        if num_pr is None:
            return None, 0
        try:
            num_id = int(num_pr.numId.val) if num_pr.numId is not None else None
            ilvl = int(num_pr.ilvl.val) if num_pr.ilvl is not None else 0
        except Exception:  # noqa: BLE001
            return None, 0
        if num_id is None:
            return None, ilvl
        fmt = self._levels.get(num_id, {}).get(ilvl, "decimal")
        style_lower = (style_name or "").lower()
        if fmt in ("bullet", "none") or any(hint in style_lower for hint in _BULLET_STYLES):
            return "-", ilvl
        counters = self._counters.setdefault(num_id, {})
        counters[ilvl] = counters.get(ilvl, 0) + 1
        for key in [key for key in counters if key > ilvl]:
            del counters[key]
        return f"{counters[ilvl]}.", ilvl


def _extract_docx_images(document: Any, assets_dir: Path, stem: str) -> tuple[dict[str, str], list[str]]:
    """按关系 id 提取图片到 sources/assets/；返回 {rId: markdown 相对路径} 与警告。"""
    mapping: dict[str, str] = {}
    warnings: list[str] = []
    assets_dir.mkdir(parents=True, exist_ok=True)
    try:
        from docx.oxml.ns import qn

        blips = document.element.body.findall(".//" + qn("a:blip"))
    except Exception:  # noqa: BLE001 - python-docx 内部结构变化时跳过图片
        return mapping, ["image_extraction_skipped"]
    sequence = 0
    for blip in blips:
        rid = blip.get(qn("r:embed"))
        if not rid or rid in mapping:
            continue
        relation = document.part.rels.get(rid)
        if relation is None:
            continue
        try:
            image_part = relation.target_part
            extension = (image_part.partname.ext or "png").lower()
            blob = image_part.blob
        except Exception:  # noqa: BLE001
            warnings.append("image_relation_unreadable")
            continue
        if extension not in IMAGE_EXTS:
            try:
                from PIL import Image

                image = Image.open(io.BytesIO(blob))
                output = assets_dir / f"{stem}-img{sequence + 1:02d}.png"
                image.save(output, format="PNG")
                sequence += 1
                mapping[rid] = f"../assets/{output.name}"
                continue
            except Exception:  # noqa: BLE001
                warnings.append(f"image_format_unsupported:{extension}")
                continue
        sequence += 1
        output = assets_dir / f"{stem}-img{sequence:02d}.{extension}"
        try:
            output.write_bytes(blob)
        except OSError:
            warnings.append("image_write_failed")
            continue
        mapping[rid] = f"../assets/{output.name}"
    return mapping, warnings


def _docx_to_markdown(path: Path, project_dir: Path) -> dict[str, Any]:
    from docx import Document
    from docx.oxml.ns import qn

    document = Document(str(path))
    stem = _safe_stem(path.name)
    image_mapping, image_warnings = _extract_docx_images(document, project_dir / ASSETS_DIR_REL, stem)
    numbering = _NumberingResolver(document)
    lines: list[str] = []
    warnings: list[str] = list(image_warnings)
    headings = 0
    tables = 0
    images = 0
    textbox_found = 0
    textbox_count = 0
    textbox_chars = 0
    textbox_seen: set[str] = set()
    textbox_duplicates = 0
    textbox_empty = 0
    # 保真统计（符号/复选框/合并单元格/形状）
    stats: dict[str, Any] = {
        "symbolsMapped": 0,
        "symbolsUnmapped": 0,
        "checkboxesPreserved": 0,
        "mergedCellsFlattened": 0,
        "tableSeq": 0,
    }

    def collect_textboxes(element: Any) -> list[str]:
        """按文档序收集文本框文本（去重 mc:AlternateContent 双分支副本）。"""
        nonlocal textbox_found, textbox_count, textbox_chars, textbox_duplicates, textbox_empty
        collected: list[str] = []
        for content in _textbox_content_list(element):
            textbox_found += 1
            text, raw_chars = _textbox_text(content)
            if not text:
                textbox_empty += 1
                continue
            if text in textbox_seen:
                textbox_duplicates += 1
                continue
            textbox_seen.add(text)
            textbox_count += 1
            textbox_chars += raw_chars
            collected.append(text)
        return collected

    def append_textbox_texts(texts: list[str]) -> None:
        for box_text in texts:
            for box_line in box_text.split("\n"):
                append_block(box_line)

    def append_block(text: str) -> None:
        if not text.strip():
            return
        lines.append(text)
        lines.append("")

    for item in document.iter_inner_content():
        class_name = type(item).__name__
        if class_name == "Paragraph":
            box_texts = collect_textboxes(item._p)
            style_name = item.style.name if item.style is not None else ""
            text = _inline_text(item, stats=stats)
            level = _heading_level(style_name)
            if level and text:
                append_block("#" * level + " " + text)
                headings += 1
                append_textbox_texts(box_texts)
                continue
            marker, indent = numbering.marker(item, style_name)
            if marker and text:
                append_block("  " * indent + f"{marker} {text}")
            elif text:
                append_block(text)
            # 段落内嵌图片按顺序引用（PRD F3.2 Step 3：md 中以相对路径引用）
            try:
                for blip in item._p.findall(".//" + qn("a:blip")):
                    rid = blip.get(qn("r:embed"))
                    if rid in image_mapping:
                        append_block(f"![图{images + 1}]({image_mapping[rid]})")
                        images += 1
            except Exception:  # noqa: BLE001
                pass
            append_textbox_texts(box_texts)
            caption_hit = any(hint in (style_name or "").lower() for hint in _CAPTION_HINTS)
            if not text and caption_hit:
                warnings.append("empty_caption_paragraph")
        elif class_name == "Table":
            table_md, table_warnings = _table_to_markdown(item, textbox_sink=collect_textboxes, stats=stats)
            warnings.extend(table_warnings)
            if table_md:
                lines.append(table_md.rstrip("\n"))
                lines.append("")
                tables += 1

    # 未在段落中定位到的图片（页眉页脚/浮动图形）统一追加引用，避免丢失
    if image_mapping and images == 0:
        for rel in image_mapping.values():
            append_block(f"![图{images + 1}]({rel})")
            images += 1

    if textbox_found:
        if textbox_count == 0:
            warnings.append(f"textbox_content_lost:{textbox_found}")
        else:
            warnings.append(f"textboxes_extracted:{textbox_count}")
        if textbox_duplicates:
            warnings.append(f"textbox_duplicates_skipped:{textbox_duplicates}")
        if textbox_empty:
            warnings.append(f"textbox_empty:{textbox_empty}")

    # 版式保真扫描：形状/嵌入对象/符号/复选框/合并单元格 → manifest 明确标记（不静默丢）
    body = document.element.body
    shapes_detected = 0
    unsupported_shapes = 0
    for tag_name in ("w:drawing", "w:pict"):
        for element in body.iter(qn(tag_name)):
            shapes_detected += 1
            has_image = bool(element.findall(".//" + qn("a:blip")))
            if not has_image:
                try:
                    has_image = bool(element.findall(".//" + qn("v:imagedata")))
                except (KeyError, ValueError):
                    has_image = False
            has_textbox = bool(element.findall(".//" + qn("w:txbxContent")))
            if not has_image and not has_textbox:
                unsupported_shapes += 1
    embedded_objects = len(body.findall(".//" + qn("w:object"))) + len(
        body.findall(".//" + qn("w:oleObject"))
    )
    if stats["symbolsMapped"]:
        warnings.append(f"symbols_mapped:{stats['symbolsMapped']}")
    if stats["symbolsUnmapped"]:
        warnings.append(f"symbols_unmapped:{stats['symbolsUnmapped']}")
    if stats["checkboxesPreserved"]:
        warnings.append(f"checkboxes_preserved:{stats['checkboxesPreserved']}")
    if stats["mergedCellsFlattened"]:
        warnings.append(f"merged_cells_flattened_total:{stats['mergedCellsFlattened']}")
    if unsupported_shapes:
        warnings.append(f"unsupported_shape:{unsupported_shapes}")
    if embedded_objects:
        warnings.append(f"embedded_object:{embedded_objects}")
    # 合并单元格位置明细限长（保留前 20 条 + 截断计数，避免 manifest 过大）
    merged_details = [item for item in warnings if item.startswith("merged_cells_flattened:")]
    if len(merged_details) > 20:
        keep = set(merged_details[:20])
        warnings = [
            item
            for item in warnings
            if not item.startswith("merged_cells_flattened:") or item in keep
        ]
        warnings.append(f"merged_cells_flattened_details_truncated:{len(merged_details) - 20}")
    markdown = "\n".join(lines).strip("\n") + "\n"
    return {
        "markdown": markdown,
        "warnings": sorted(set(warnings)),
        "headings": headings,
        "tables": tables,
        "images": images,
        "textboxCount": textbox_count,
        "textboxChars": textbox_chars,
        "symbolsMapped": stats["symbolsMapped"],
        "symbolsUnmapped": stats["symbolsUnmapped"],
        "checkboxesPreserved": stats["checkboxesPreserved"],
        "mergedCellsFlattened": stats["mergedCellsFlattened"],
        "unsupportedShapes": unsupported_shapes,
        "shapesDetected": shapes_detected,
        "embeddedObjects": embedded_objects,
    }


# ---------------------------------------------------------------------------
# .doc → docx（Word COM 独立进程 + 硬超时；soffice 兜底）
# ---------------------------------------------------------------------------
_WORD_COM_CHILD = r'''
import json
import os
import subprocess
import sys


def winword_pids():
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except Exception:
        return set()
    pids = set()
    for line in completed.stdout.splitlines():
        line = line.strip().strip('"')
        if not line.lower().startswith("winword.exe"):
            continue
        parts = [part.strip().strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def emit(payload, code):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def main():
    libexec, source, target, pid_file = sys.argv[1:5]
    sys.path.insert(0, libexec)
    from _lib import deps
    deps.ensure_dependency_path()
    try:
        import win32com.client
    except Exception as exc:
        emit({"ok": False, "code": "E_DEP_MISSING", "message": f"pywin32 不可用：{type(exc).__name__}"}, 3)
    before = winword_pids()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        after = winword_pids()
        new_pids = sorted(after - before)
        with open(pid_file, "w", encoding="utf-8") as handle:
            json.dump({"wordPids": new_pids, "pyPid": os.getpid()}, handle)
        document = word.Documents.Open(source, ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
        document.SaveAs2(target, FileFormat=16)
        document.Close(False)
        emit({"ok": True, "wordPids": new_pids}, 0)
    except Exception as exc:
        emit({"ok": False, "code": "E_CONVERT_FAILED", "message": f"{type(exc).__name__}: {exc}"}, 4)
    finally:
        try:
            if word is not None:
                word.Quit(SaveChanges=0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
'''


def _taskkill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def _find_soffice() -> Path | None:
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    candidates = [Path(found)] if found else []
    candidates.extend(
        [
            Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
            Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
            Path("/usr/bin/soffice"),
            Path("/usr/local/bin/soffice"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _convert_doc_to_docx(source: Path, work_dir: Path, timeout: float) -> tuple[Path, list[str]]:
    """`.doc` → `.docx`：优先 Word COM（独立进程 + 120s 硬超时 + 只杀本插件新起的 WINWORD），
    失败再尝试 LibreOffice soffice；均不可用抛 BgError(E_DEP_MISSING)。"""
    warnings: list[str] = []
    target = work_dir / (source.stem + ".docx")
    child_script = work_dir / "_word_com_convert.py"
    child_script.write_text(_WORD_COM_CHILD, encoding="utf-8", newline="\n")
    pid_file = work_dir / "word-child-pids.json"
    command = [
        sys.executable,
        "-X",
        "utf8",
        str(child_script),
        str(_SCRIPT_DIR),
        str(source),
        str(target),
        str(pid_file),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(timeout, DOC_COM_TIMEOUT_SEC),
        )
        payload: Any = None
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1]) if completed.stdout.strip() else None
        except (json.JSONDecodeError, IndexError):
            payload = None
        if completed.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True:
            warnings.append("doc_converted_by_word_com")
            return target, warnings
        if isinstance(payload, dict) and payload.get("code") == "E_DEP_MISSING":
            warnings.append("word_com_unavailable")
        else:
            reason = str((payload or {}).get("message") or "Word COM 转换失败")[:200]
            warnings.append(f"word_com_failed:{reason}")
    except subprocess.TimeoutExpired:
        warnings.append(f"word_com_timeout:{int(min(timeout, DOC_COM_TIMEOUT_SEC))}s")
        try:
            pids_payload = json.loads(pid_file.read_text(encoding="utf-8"))
            _taskkill_pids([int(pid) for pid in pids_payload.get("wordPids") or []])
        except (OSError, ValueError, TypeError):
            pass
    soffice = _find_soffice()
    if soffice is not None:
        try:
            completed = subprocess.run(
                [
                    str(soffice),
                    "--headless",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    str(work_dir),
                    str(source),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(timeout, DOC_COM_TIMEOUT_SEC),
            )
            if completed.returncode == 0 and target.is_file():
                warnings.append("doc_converted_by_soffice")
                return target, warnings
            warnings.append("soffice_conversion_failed")
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("soffice_timeout_or_error")
    raise errors.BgError(
        errors.E_DEP_MISSING,
        f".doc 无法自动转换：{source.name}",
        hint="请安装 Word（COM）或 LibreOffice，或让用户把文件另存为 .docx 后重试；无转换器时不得编造内容",
        detail={"sourcePath": str(source), "warnings": warnings},
        exit_code=errors.EXIT_DEP,
    )


# ---------------------------------------------------------------------------
# pdf → markdown
# ---------------------------------------------------------------------------
def _evenly_pick(items: list[int], count: int) -> list[int]:
    """从有序序列中均匀抽取 count 项（count 不足时全取），用于大 PDF 抽样复核。"""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    step = len(items) / count
    picked: list[int] = []
    for index in range(count):
        picked.append(items[min(len(items) - 1, int(index * step))])
    return sorted(set(picked))


def _table_recheck_plan(page_count: int, fallback_pages: list[int]) -> dict[str, Any]:
    """表格复核计划（P2-11）：小文件全量复核；大文件回退页优先 + 均匀抽样的分批复核。

    - `page_count <= PDF_TABLE_RECHECK_FULL_MAX_PAGES` → mode=full，逐页全量（历史行为）；
    - 更大文件 → mode=sampled，总样本 ≤ `PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES`；
      回退页优先全覆盖（预算内），其余页均匀抽样补足，避免 200+ 页 PDF 复核无限拖慢转换。
    """
    total = max(0, int(page_count))
    if total <= PDF_TABLE_RECHECK_FULL_MAX_PAGES:
        return {"mode": "full", "pages": list(range(1, total + 1)), "totalPages": total, "reason": "all_pages"}
    valid_fallback = sorted({int(page) for page in fallback_pages if 1 <= int(page) <= total})
    # 回退页优先全覆盖（它们最可能丢表格/关键数据），其余均匀抽样补足总预算；
    # 回退页很多（>60）时按预算均匀抽样，避免逐个复核拖慢转换。
    fallback_budget = min(len(valid_fallback), PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES)
    sampled_fallback = _evenly_pick(valid_fallback, fallback_budget)
    remaining = max(0, PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES - len(sampled_fallback))
    sampled_rest: list[int] = []
    if remaining:
        candidates = _evenly_pick(list(range(1, total + 1)), min(total, PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES + len(sampled_fallback)))
        sampled_rest = [page for page in candidates if page not in set(sampled_fallback)][:remaining]
    pages = sorted(set(sampled_fallback) | set(sampled_rest))
    return {
        "mode": "sampled",
        "pages": pages,
        "totalPages": total,
        "fallbackSampled": len(sampled_fallback),
        "reason": "too_many_pages",
    }


def _recheck_tables(path: Path, page_count: int, fallback_pages: list[int], markdown_tables: int) -> tuple[int, dict[str, Any], list[str]]:
    """pdfplumber 表格复核：小文件全量、大文件抽样分批（限页数 + 限时预算，失败降级只记录）。

    返回 (tables, tableRecheck 元数据, warnings)；复核失败/超时不影响主转换（降级并记录）。
    """
    plan = _table_recheck_plan(page_count, fallback_pages)
    warnings: list[str] = []
    info: dict[str, Any] = {
        "mode": plan["mode"],
        "totalPages": plan["totalPages"],
        "plannedPages": len(plan["pages"]),
        "sampledPages": 0,
        "pdfplumberTables": 0,
        "batches": 0,
        "batchSize": PDF_TABLE_RECHECK_BATCH_SIZE if plan["mode"] == "sampled" else 0,
        "budgetSec": PDF_TABLE_RECHECK_BUDGET_SEC if plan["mode"] == "sampled" else None,
        "fallbackSampled": plan.get("fallbackSampled", 0),
        "timedOut": False,
        "error": "",
    }
    sampled_tables = 0
    deadline = time.monotonic() + PDF_TABLE_RECHECK_BUDGET_SEC
    try:
        import pdfplumber

        with _silence_stdout():
            with pdfplumber.open(str(path)) as pdf:
                pages = plan["pages"]
                for start in range(0, len(pages), PDF_TABLE_RECHECK_BATCH_SIZE):
                    if plan["mode"] == "sampled" and time.monotonic() > deadline:
                        info["timedOut"] = True
                        break
                    batch = pages[start:start + PDF_TABLE_RECHECK_BATCH_SIZE]
                    for page_no in batch:
                        sampled_tables += len(pdf.pages[page_no - 1].extract_tables() or [])
                    info["batches"] += 1
                    info["sampledPages"] += len(batch)
                    info["pdfplumberTables"] = sampled_tables
    except Exception:  # noqa: BLE001 - 复核失败不影响主转换
        warnings.append("pdfplumber_recheck_failed")
        info["error"] = "recheck_failed"
        return markdown_tables, info, warnings
    if plan["mode"] == "full":
        tables = markdown_tables
        if info["pdfplumberTables"] > tables:
            warnings.append(f"tables_maybe_lost:pymupdf4llm={tables},pdfplumber={info['pdfplumberTables']}")
            tables = info["pdfplumberTables"]
        return tables, info, warnings
    # 抽样模式：不覆盖全量表格计数，只按抽样页给出量级提示
    warnings.append(
        f"pdf_table_recheck_mode:sampled,sampled={info['sampledPages']},"
        f"total={info['totalPages']},pdfplumberTables={info['pdfplumberTables']}"
    )
    if info["totalPages"] > PDF_LARGE_PDF_PAGES:
        warnings.append(
            f"pdf_table_recheck_large_pdf:pages={info['totalPages']}>{PDF_LARGE_PDF_PAGES},"
            f"sampled={info['sampledPages']}（大文件按批抽样复核，表格数仅供量级参考）"
        )
    if info["timedOut"]:
        warnings.append(f"pdf_table_recheck_partial:batched={info['sampledPages']},planned={len(plan['pages'])}")
    if info["sampledPages"]:
        estimate = round(info["pdfplumberTables"] / info["sampledPages"] * info["totalPages"])
        info["estimatedTables"] = estimate
        if estimate > markdown_tables * 1.5 and estimate - markdown_tables >= 5:
            warnings.append(f"tables_maybe_lost_estimated:pymupdf4llm={markdown_tables},pdfplumber_est={estimate}")
    return markdown_tables, info, warnings


#: 逐页覆盖匹配时忽略的 Markdown/空白标记（避免 `**页码**`、表格管线影响行匹配）
_PAGE_MATCH_DROP_RE = re.compile(r"[\s*#|_`>~\-—–·]+")


def _compact_for_page_match(text: str) -> str:
    return _PAGE_MATCH_DROP_RE.sub("", str(text or ""))


def _insert_fallback_block(markdown: str, page_no: int, block: str) -> str | None:
    """尽量把回退块就地插入到该页页标之后；找不到唯一页标时返回 None（退回文末附录）。

    pymupdf4llm 会保留 PDF 页眉页脚（如 `武汉市公安局交通管理局 **49**`）。页标后紧跟
    分页线 `-----` 时，把补录块插到分页线之后，正文顺序更接近原 PDF，也避免文末重复。
    """
    pattern = re.compile(rf"(?m)^.*\*\*{page_no}\*\*\s*$")
    matches = list(pattern.finditer(markdown))
    if len(matches) != 1:
        return None
    tail = markdown[matches[0].end() : matches[0].end() + 500]
    separator = re.search(r"(?m)^\s*-{3,}\s*$", tail)
    if separator is None:
        return None
    insert_at = matches[0].end() + separator.end()
    return f"{markdown[:insert_at]}\n\n{block}\n{markdown[insert_at:]}"


def _pdf_page_coverage(markdown: str, page_texts: list[str]) -> dict[str, Any]:
    """逐页覆盖校验（P0-2/P2-4）：pymupdf4llm 整页丢失时回退补齐，但避免整页重复。

    2026-11-23 冒烟修复：旧实现用“20 字符采样块”判断覆盖，PDF 页眉/换行会让
    **内容其实已在正文**的页面被判丢失，然后整页追加到文末（261 页样本 49 页全部
    重复追加，仅第 49 页真的缺内容）。改为按行匹配：
    - 一行只要在已生成 Markdown 中出现即视为已覆盖（先去掉空白/Markdown 标记）；
    - 页面行覆盖率 >= 85% 视为覆盖；否则只追加**缺失行**，并在标题下标注缺失行数；
    - 页眉页码等短行不影响判定，也不会让文末出现整页重复。

    返回 `markdown/pageCoverage/fallbackPages/fallbackPageCount/keyDataFromFallbackOnly/fallbackNote/fallbackDetail/warnings`。
    """
    fallback_pages: list[int] = []
    warnings: list[str] = []
    fallback_detail: list[dict[str, Any]] = []
    page_coverage: list[dict[str, Any]] = []
    result_markdown = markdown
    matched_markdown = _compact_for_page_match(markdown)
    for page_no, page_text in enumerate(page_texts, start=1):
        raw_lines = [line.strip() for line in str(page_text or "").splitlines() if line.strip()]
        compact_lines = [(_compact_for_page_match(line), line) for line in raw_lines]
        compact_lines = [(compact, line) for compact, line in compact_lines if len(compact) >= 2]
        total_chars = sum(len(compact) for compact, _line in compact_lines)
        if total_chars < 20:
            page_coverage.append({"page": page_no, "chars": total_chars, "covered": True, "fallback": False, "lineCoverage": 1.0})
            continue
        missing: list[tuple[str, str]] = []
        present_chars = 0
        for compact, line in compact_lines:
            if compact in matched_markdown:
                present_chars += len(compact)
            else:
                missing.append((compact, line))
        line_coverage = present_chars / total_chars if total_chars else 1.0
        covered = line_coverage >= 0.85
        page_coverage.append(
            {
                "page": page_no,
                "chars": total_chars,
                "covered": covered,
                "fallback": not covered,
                "lineCoverage": round(line_coverage, 3),
                "missingLineCount": len(missing),
                "missingChars": sum(len(compact) for compact, _line in missing),
            }
        )
        if covered:
            continue
        fallback_pages.append(page_no)
        missing_chars = sum(len(compact) for compact, _line in missing)
        fallback_detail.append(
            {
                "page": page_no,
                "lineCoverage": round(line_coverage, 3),
                "missingLineCount": len(missing),
                "missingChars": missing_chars,
            }
        )
        missing_text = "\n".join(line for _compact, line in missing)
        marker = (
            f"<!-- BG:FALLBACK-PAGE:{page_no} missing={len(missing)}/{len(compact_lines)} "
            f"lines,{missing_chars} chars（仅补齐缺失行，避免整页重复） -->"
        )
        inline_block = f"**第 {page_no} 页（转换回退文本，仅缺失行）**\n\n{marker}\n\n{missing_text}"
        inserted = _insert_fallback_block(result_markdown, page_no, inline_block)
        if inserted is not None:
            result_markdown = inserted
            fallback_detail[-1]["placement"] = "inline"
        else:
            result_markdown += f"\n\n## 第 {page_no} 页（转换回退文本）\n\n{marker}\n\n{missing_text}\n"
            fallback_detail[-1]["placement"] = "appendix"
        # 追加后同步匹配串，避免后续页面把同一缺失行再判一次
        matched_markdown += "".join(compact for compact, _line in missing)
    fallback_note = ""
    if fallback_pages:
        # N3：冒号后是页码（skill 约定 `:<n>` 为页），数量另起 `*_count`。
        warnings.append(f"pdf_pages_fallback:{fallback_pages[0]}")
        warnings.append(f"pdf_pages_fallback_count:{len(fallback_pages)}")
        warnings.append("pdf_pages_fallback_list:" + ",".join(str(item) for item in fallback_pages[:20]))
        warnings.append(f"key_data_from_fallback_only:{fallback_pages[0]}")
        pages_text = "、".join(str(item) for item in fallback_pages[:20])
        fallback_note = (
            f"关键数据仅来自回退文本：第 {pages_text} 页存在缺失行，已按行就地补录（找不到唯一页标时追加到文末附录；"
            "仅缺失内容，不整页重复）；表格/版式结构可能已丢失，F3 提取与写作引用这些页面的数字/条款前必须核对招标文件 PDF 原件"
        )
    return {
        "markdown": result_markdown,
        "pageCoverage": page_coverage,
        "fallbackPages": fallback_pages[:PDF_FALLBACK_LIST_LIMIT],
        "fallbackPageCount": len(fallback_pages),
        "keyDataFromFallbackOnly": bool(fallback_pages),
        "fallbackDetail": fallback_detail,
        "fallbackNote": fallback_note,
        "warnings": warnings,
    }


def _pdf_to_markdown(path: Path) -> dict[str, Any]:
    import fitz  # PyMuPDF

    warnings: list[str] = []
    page_texts: list[str] = []
    with fitz.open(str(path)) as document:
        for page in document:
            page_texts.append(page.get_text("text") or "")
        text_chars = sum(len(text) for text in page_texts)
        page_count = document.page_count
    markdown = ""
    try:
        import pymupdf4llm

        with _silence_stdout():
            try:
                markdown = pymupdf4llm.to_markdown(str(path), show_progress=False)
            except TypeError:  # 旧版本无 show_progress
                markdown = pymupdf4llm.to_markdown(str(path))
    except Exception as exc:  # noqa: BLE001 - 转换失败按文件标记
        raise errors.BgError(
            errors.E_CONVERT_FAILED,
            f"PDF 转换失败：{path.name}",
            hint="检查 PDF 是否加密/损坏；可尝试另存或改用 docx",
            detail={"sourcePath": str(path), "reason": f"{type(exc).__name__}: {exc}"},
            exit_code=errors.EXIT_GENERIC,
        ) from exc
    markdown = _norm_newlines(markdown or "").strip() + "\n" if markdown else ""
    # P0-2 逐页覆盖校验 + P2-11 回退页标记：pymupdf4llm 偶发整页丢失时回退追加该页纯文本
    coverage = _pdf_page_coverage(markdown, page_texts)
    markdown = coverage["markdown"]
    warnings.extend(coverage["warnings"])
    tables = _count_tables(markdown)
    tables, table_recheck, recheck_warnings = _recheck_tables(path, page_count, coverage["fallbackPages"], tables)
    warnings.extend(recheck_warnings)
    quality = ""
    if text_chars < PDF_MIN_TEXT_CHARS:
        warnings.append("scanned_unsupported")
        quality = "scanned_unsupported"
    return {
        "markdown": markdown,
        "warnings": warnings,
        "headings": _count_headings(markdown),
        "tables": tables,
        "images": 0,
        "textChars": text_chars,
        "pages": page_count,
        "pageCoverage": coverage["pageCoverage"],
        "fallbackPages": coverage["fallbackPages"],
        "fallbackPageCount": coverage["fallbackPageCount"],
        "keyDataFromFallbackOnly": coverage["keyDataFromFallbackOnly"],
        "fallbackDetail": coverage.get("fallbackDetail") or [],
        "fallbackNote": coverage["fallbackNote"],
        "tableRecheck": table_recheck,
        "pdfQuality": quality,
    }



# ---------------------------------------------------------------------------
# 文件级转换
# ---------------------------------------------------------------------------
def _convert_one(project_dir: Path, source: Path, work_dir: Path, per_file_timeout: float) -> dict[str, Any]:
    """执行单个文件转换（返回 markdown/统计/warnings/converter），失败抛 BgError。"""
    suffix = source.suffix.lower()
    source_type = suffix.lstrip(".")
    if source_type == "docx":

        def run() -> dict[str, Any]:
            return _docx_to_markdown(source, project_dir)

        result = _run_with_timeout(run, per_file_timeout)
        result["converter"] = f"python-docx {_version('python-docx')}"
        return result
    if source_type == "doc":
        docx_path, warnings = _convert_doc_to_docx(source, work_dir, per_file_timeout)

        def run() -> dict[str, Any]:
            return _docx_to_markdown(docx_path, project_dir)

        result = _run_with_timeout(run, max(30.0, per_file_timeout - DOC_COM_TIMEOUT_SEC))
        result["warnings"] = sorted(set(list(result.get("warnings") or []) + warnings))
        result["converter"] = "Word COM → python-docx"
        return result
    if source_type == "pdf":
        result = _run_with_timeout(_pdf_to_markdown, per_file_timeout, source)
        result["converter"] = f"pymupdf4llm {_version('pymupdf4llm')} (PyMuPDF {_version('PyMuPDF')})"
        return result
    if source_type in ("md", "txt"):
        text, warnings = _read_text_source(source)
        markdown = text if source_type == "md" else _txt_to_markdown(text)
        if markdown and not markdown.endswith("\n"):
            markdown += "\n"
        return {
            "markdown": markdown,
            "warnings": warnings,
            "headings": _count_headings(markdown),
            "tables": _count_tables(markdown),
            "images": 0,
            "converter": "copy" if source_type == "md" else "text→markdown",
        }
    raise errors.BgError(errors.E_USAGE, f"不支持的文件类型：{source.name}", hint="支持 .docx/.doc/.pdf/.md/.txt")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def manifest_path(project_dir: Path) -> Path:
    return project_dir / MANIFEST_REL


def load_manifest(project_dir: Path) -> dict[str, Any]:
    path = manifest_path(project_dir)
    if not path.is_file():
        return {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": paths.iso_now(),
            "generatedAt": paths.iso_now(),
            "updatedAt": paths.iso_now(),
            "converter": {},
            "converters": {},
            "files": [],
            "sources": [],
        }
    document = jsonio.read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("files"), list):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "sources/conversion-manifest.json 结构非法",
            hint="删除该文件后重跑 --apply（或从 sources/.backup-* 恢复）",
            detail={"path": MANIFEST_REL},
        )
    document.setdefault("schemaVersion", SCHEMA_VERSION)
    document.setdefault("files", [])
    document.setdefault("sources", [])
    return document


def _converter_table(used: set[str]) -> dict[str, str]:
    table = {
        "docx": f"python-docx {_version('python-docx')}",
        "doc": "Word COM (pywin32) / soffice 兜底",
        "pdf": f"pymupdf4llm {_version('pymupdf4llm')} / PyMuPDF {_version('PyMuPDF')} / pdfplumber {_version('pdfplumber')}",
        "md": "copy",
        "txt": "text→markdown",
    }
    return {key: value for key, value in table.items() if key in used} or table


def _copy_raw_source(project_dir: Path, source: Path, source_hash: str) -> tuple[str, str]:
    """D3（第三轮冒烟）：把原件复制到 `sources/raw/`，返回 (rawCopyPath, warning)。

    项目要自包含：源盘（如 E:\\）不可用时仍能追溯/重转，且 `bg_project.py fields`
    要求 sourceFiles[].rawPath 是项目相对路径。复制失败不阻断转换，只给 warning。
    """
    raw_dir = project_dir / "sources" / "raw"
    try:
        resolved_source = source.resolve()
        try:
            relative = resolved_source.relative_to(project_dir.resolve())
            if relative.parts[:2] == ("sources", "raw"):
                return relative.as_posix(), ""
        except ValueError:
            pass
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / source.name
        if target.exists() and _sha256_file(target) != source_hash:
            digest = source_hash.split(":")[-1][:8]
            target = raw_dir / f"{source.stem}-{digest}{source.suffix}"
        shutil.copy2(resolved_source, target)
        return paths.rel_posix(project_dir, target), ""
    except OSError as exc:
        return "", f"raw_copy_failed:{exc.strerror or type(exc).__name__}"


def _allocate_converted_path(project_dir: Path, source: Path, manifest: Mapping[str, Any]) -> str:
    """为源文件分配 `sources/converted/<stem>.md`（已有条目优先保持原路径；冲突加 -2/-3）。"""
    source_key = str(source)
    stem = _safe_stem(source.name)
    entries = [entry for entry in manifest.get("files") or [] if isinstance(entry, Mapping)]
    for entry in entries:
        if str(entry.get("sourcePath") or "") == source_key and entry.get("convertedPath"):
            existing = str(entry["convertedPath"])
            if existing.startswith(CONVERTED_DIR_REL + "/"):
                return existing
    used = {
        str(entry.get("convertedPath") or "")
        for entry in entries
        if str(entry.get("sourcePath") or "") != source_key
    }
    candidate = f"{CONVERTED_DIR_REL}/{stem}.md"
    if candidate not in used:
        return candidate
    for index in range(2, 100):
        alternative = f"{CONVERTED_DIR_REL}/{stem}-{index}.md"
        if alternative not in used:
            return alternative
    return f"{CONVERTED_DIR_REL}/{stem}-{paths.rand4()}.md"


def _prune_backups(sources_dir: Path, keep: int = BACKUP_KEEP) -> None:
    backups = sorted(
        (entry for entry in sources_dir.iterdir() if entry.is_dir() and entry.name.startswith(BACKUP_DIR_PREFIX)),
        key=lambda entry: entry.name,
        reverse=True,
    )
    for stale in backups[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _backup_existing(project_dir: Path, target: Path, backup_dir: Path) -> None:
    if not target.is_file():
        return
    destination = backup_dir / target.relative_to(project_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, destination)


def _write_markdown(project_dir: Path, relative: str, markdown: str) -> Path:
    target = project_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text_atomic(target, markdown)
    return target


def _entry_changed(existing: Mapping[str, Any] | None, source_hash: str, project_dir: Path) -> bool:
    if not existing:
        return True
    if str(existing.get("hash") or "") != source_hash:
        return True
    converted = str(existing.get("convertedPath") or "")
    if not converted or not (project_dir / converted).is_file():
        return True
    return False


def _convert_workdir(workspace: Path | None, project_dir: Path) -> Any:
    """工作区 ACL 下的临时转换目录（<workspace>/dsh/tmp），用后自清理。"""
    import contextlib

    @contextlib.contextmanager
    def manager():
        base = (workspace / "dsh" / "tmp") if workspace is not None else (project_dir / "dsh" / "tmp")
        base.mkdir(parents=True, exist_ok=True)
        work = tempdir.mkdtemp(prefix="bg-convert-", dir=str(base))
        try:
            yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return manager()


def convert_files(
    project_dir: Path,
    files: list[Path],
    *,
    force: bool = False,
    no_backup: bool = False,
    copy_raw: bool = True,
    per_file_timeout: float = PER_FILE_TIMEOUT_SEC,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """转换一批文件并更新 manifest（内部实现，供 CLI 与 --selftest 复用）。"""
    project_dir = project_dir.resolve()
    files = [Path(str(item.resolve())) for item in files]
    sources_dir = project_dir / "sources"
    converted_dir = project_dir / CONVERTED_DIR_REL
    converted_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(project_dir)
    existing_map: dict[str, dict[str, Any]] = {
        str(entry.get("sourcePath") or ""): dict(entry)
        for entry in manifest.get("files") or []
        if isinstance(entry, Mapping)
    }
    backup_dir = sources_dir / f"{BACKUP_DIR_PREFIX}{paths.timestamp14()}" if not no_backup else None
    started = paths.iso_now()
    converted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    changed: list[str] = []
    quality_map: dict[str, str] = {}
    used_types: set[str] = set()
    results: list[dict[str, Any]] = []
    planned_paths: dict[str, str] = {}
    with _convert_workdir(workspace, project_dir) as work_dir:
        for source in files:
            source_key = str(source)
            source_type = source.suffix.lower().lstrip(".")
            used_types.add(source_type)
            source_hash = _sha256_file(source)
            relative = planned_paths.get(source_key) or _allocate_converted_path(project_dir, source, manifest)
            planned_paths[source_key] = relative
            target = project_dir / relative
            raw_copy_path, raw_copy_warning = _copy_raw_source(project_dir, source, source_hash) if copy_raw else ("", "")
            existing = existing_map.get(source_key)
            entry: dict[str, Any] = {
                "sourcePath": source_key,
                "sourceName": source.name,
                "sourceType": source_type,
                "sizeBytes": source.stat().st_size,
                "convertedPath": relative,
                "rawCopyPath": raw_copy_path or str((existing or {}).get("rawCopyPath") or ""),
                "mdChars": int(existing.get("mdChars") or 0) if existing else 0,
                "headings": int(existing.get("headings") or 0) if existing else 0,
                "tables": int(existing.get("tables") or 0) if existing else 0,
                "images": int(existing.get("images") or 0) if existing else 0,
                "textboxCount": int(existing.get("textboxCount") or 0) if existing else 0,
                "textboxChars": int(existing.get("textboxChars") or 0) if existing else 0,
                "symbolsMapped": int(existing.get("symbolsMapped") or 0) if existing else 0,
                "symbolsUnmapped": int(existing.get("symbolsUnmapped") or 0) if existing else 0,
                "checkboxesPreserved": int(existing.get("checkboxesPreserved") or 0) if existing else 0,
                "mergedCellsFlattened": int(existing.get("mergedCellsFlattened") or 0) if existing else 0,
                "unsupportedShapes": int(existing.get("unsupportedShapes") or 0) if existing else 0,
                "warnings": list(existing.get("warnings") or []) if existing else [],
                "status": "skipped",
                "hash": source_hash,
                "quality": str(existing.get("quality") or "") if existing else "",
                "mtime": source.stat().st_mtime,
                "convertedAt": str(existing.get("convertedAt") or "") if existing else "",
                "fallbackPages": list(existing.get("fallbackPages") or []) if existing and isinstance(existing.get("fallbackPages"), list) else [],
                "fallbackPageCount": int(existing.get("fallbackPageCount") or 0) if existing else 0,
                "keyDataFromFallbackOnly": bool(existing.get("keyDataFromFallbackOnly")) if existing else False,
                "fallbackDetail": list(existing.get("fallbackDetail") or []) if existing and isinstance(existing.get("fallbackDetail"), list) else [],
                "fallbackNote": str(existing.get("fallbackNote") or "") if existing else "",
                "tableRecheck": dict(existing.get("tableRecheck") or {}) if existing and isinstance(existing.get("tableRecheck"), Mapping) else {},
            }
            if raw_copy_warning:
                entry["warnings"] = sorted(set(list(entry.get("warnings") or []) + [raw_copy_warning]))
            if (
                not force
                and existing is not None
                and not _entry_changed(existing, source_hash, project_dir)
            ):
                entry["status"] = "skipped"
                entry["convertedPath"] = str(existing.get("convertedPath") or relative)
                skipped.append(entry["convertedPath"])
                results.append(entry)
                if workspace is not None:
                    event_log.log_event(
                        workspace,
                        "convert.file",
                        actor="script",
                        project=str(state_lib.load_project(project_dir).get("projectId") or ""),
                        detail={"source": source.name, "status": "skipped", "md": entry["convertedPath"]},
                    )
                continue
            try:
                outcome = _convert_one(project_dir, source, work_dir, per_file_timeout)
            except TimeoutError:
                entry["warnings"] = sorted(set(list(entry["warnings"]) + [f"timeout:{int(per_file_timeout)}s"]))
                entry["status"] = "failed"
                entry["convertedPath"] = ""
                failed.append(source_key)
                results.append(entry)
                if workspace is not None:
                    event_log.log_event(
                        workspace,
                        "convert.failed",
                        actor="script",
                        detail={"source": source.name, "reason": "timeout"},
                        result="error",
                    )
                continue
            except errors.BgError as exc:
                entry["warnings"] = sorted(
                    set(list(entry["warnings"]) + [f"{exc.code}:{exc.message[:120]}"] + list(exc.detail.get("warnings") or []))
                )
                entry["status"] = "failed"
                entry["convertedPath"] = ""
                failed.append(source_key)
                results.append(entry)
                if workspace is not None:
                    event_log.log_event(
                        workspace,
                        "convert.failed",
                        actor="script",
                        detail={"source": source.name, "error": exc.code},
                        result="error",
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其他文件
                entry["warnings"] = sorted(set(list(entry["warnings"]) + [f"convert_error:{type(exc).__name__}"]))
                entry["status"] = "failed"
                entry["convertedPath"] = ""
                failed.append(source_key)
                results.append(entry)
                if workspace is not None:
                    event_log.log_event(
                        workspace,
                        "convert.failed",
                        actor="script",
                        detail={"source": source.name, "error": type(exc).__name__},
                        result="error",
                    )
                continue
            markdown = str(outcome.get("markdown") or "")
            if str(outcome.get("pdfQuality") or "") == "scanned_unsupported":
                # 扫描件/图片型 PDF：不内置 OCR（决策 9.15）→ 标记失败并提示用户换文本版
                entry["warnings"] = sorted(set(list(entry["warnings"]) + ["scanned_unsupported"]))
                entry["status"] = "failed"
                entry["convertedPath"] = ""
                entry["quality"] = "scanned_unsupported"
                failed.append(source_key)
                results.append(entry)
                if workspace is not None:
                    event_log.log_event(
                        workspace,
                        "convert.failed",
                        actor="script",
                        detail={"source": source.name, "error": "scanned_unsupported"},
                        result="error",
                    )
                continue
            quality, quality_warnings = _quality_for(markdown)
            warnings = sorted(set(list(outcome.get("warnings") or []) + quality_warnings))
            warnings = sorted(set(warnings + quality_warnings))
            if outcome.get("pdfQuality"):
                quality = str(outcome["pdfQuality"])
            if backup_dir is not None:
                _backup_existing(project_dir, target, backup_dir)
            _write_markdown(project_dir, relative, markdown)
            entry.update(
                {
                    "mdChars": len(markdown.strip()),
                    "headings": int(outcome.get("headings") or 0),
                    "tables": int(outcome.get("tables") or 0),
                    "pageCoverage": outcome.get("pageCoverage") or [],
                    "images": int(outcome.get("images") or 0),
                    "textboxCount": int(outcome.get("textboxCount") or 0),
                    "textboxChars": int(outcome.get("textboxChars") or 0),
                    "symbolsMapped": int(outcome.get("symbolsMapped") or 0),
                    "symbolsUnmapped": int(outcome.get("symbolsUnmapped") or 0),
                    "checkboxesPreserved": int(outcome.get("checkboxesPreserved") or 0),
                    "mergedCellsFlattened": int(outcome.get("mergedCellsFlattened") or 0),
                    "unsupportedShapes": int(outcome.get("unsupportedShapes") or 0),
                    "warnings": warnings,
                    "status": "converted",
                    "quality": quality,
                    "convertedAt": paths.iso_now(),
                    "converter": str(outcome.get("converter") or ""),
                    "fallbackPages": list(outcome.get("fallbackPages") or []),
                    "fallbackPageCount": int(outcome.get("fallbackPageCount") or 0),
                    "keyDataFromFallbackOnly": bool(outcome.get("keyDataFromFallbackOnly")),
                    "fallbackDetail": list(outcome.get("fallbackDetail") or []),
                    "fallbackNote": str(outcome.get("fallbackNote") or ""),
                    "tableRecheck": dict(outcome.get("tableRecheck") or {}),
                }
            )
            converted.append(relative)
            quality_map[relative] = quality
            changed.append(relative)
            results.append(entry)
            if workspace is not None:
                try:
                    project_id = str(state_lib.load_project(project_dir).get("projectId") or "")
                except errors.BgError:
                    project_id = ""
                event_log.log_event(
                    workspace,
                    "convert.file",
                    actor="script",
                    project=project_id,
                    detail={
                        "source": source.name,
                        "status": "converted",
                        "md": relative,
                        "mdChars": entry["mdChars"],
                        "quality": quality,
                    },
                )
    # 合并 manifest：保留未参与本次转换的历史条目；本次文件按顺序更新
    merged: dict[str, dict[str, Any]] = dict(existing_map)
    for entry in results:
        merged[str(entry["sourcePath"])] = entry
    ordered = [merged[key] for key in merged]
    converters = _converter_table(used_types)
    generated_at = paths.iso_now()
    # PRD 7.6 / T2 guard 读取 `sources[]`；CONTRACT §10.1 以 `files[]` 为准。
    # 两者同数据同顺序（M3.1 G2：保持双数组，向 PRD 7.6 对齐）。
    file_entries = [dict(entry) for entry in ordered]
    manifest_document = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "updatedAt": generated_at,
        "converter": converters,
        "converters": converters,
        "files": file_entries,
        "sources": [dict(entry) for entry in file_entries],
    }
    manifest_target = manifest_path(project_dir)
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(manifest_target, manifest_document)
    changed.append(MANIFEST_REL)
    if backup_dir is not None and backup_dir.is_dir():
        _prune_backups(sources_dir)
    if workspace is not None:
        event_log.log_event(
            workspace,
            "convert.started",
            actor="script",
            detail={
                "project": str(project_dir.name),
                "requested": len(files),
                "converted": len(converted),
                "skipped": len(skipped),
                "failed": len(failed),
                "startedAt": started,
            },
        )
    return {
        "ok": True,
        "command": "convert",
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "files": results,
        "rawCopies": [str(entry.get("rawCopyPath") or "") for entry in results if entry.get("rawCopyPath")],
        "quality": quality_map,
        "manifest": MANIFEST_REL,
        "generatedAt": generated_at,
        "backupDir": paths.rel_posix(project_dir, backup_dir) if backup_dir is not None and backup_dir.is_dir() else "",
        "changed": changed,
        "skippedCount": len(skipped),
    }


def check_files(project_dir: Path, files: list[Path]) -> dict[str, Any]:
    """只读预检（--check）：判断每个文件将 convert / skip / fail，不写任何文件。"""
    project_dir = project_dir.resolve()
    manifest = load_manifest(project_dir)
    existing_map = {
        str(entry.get("sourcePath") or ""): dict(entry)
        for entry in manifest.get("files") or []
        if isinstance(entry, Mapping)
    }
    plan: list[dict[str, Any]] = []
    for source in files:
        entry = existing_map.get(str(source))
        try:
            source_hash = _sha256_file(source)
        except OSError as exc:
            plan.append({"sourcePath": str(source), "action": "fail", "reason": f"read_error:{exc.strerror}"})
            continue
        relative = str((entry or {}).get("convertedPath") or _allocate_converted_path(project_dir, source, manifest))
        unchanged = entry is not None and not _entry_changed(entry, source_hash, project_dir)
        plan.append(
            {
                "sourcePath": str(source),
                "action": "skip" if unchanged else "convert",
                "convertedPath": relative,
            }
        )
    return {
        "ok": True,
        "command": "convert_check",
        "manifest": MANIFEST_REL,
        "files": plan,
        "wouldConvert": [item["sourcePath"] for item in plan if item["action"] == "convert"],
        "wouldSkip": [item["sourcePath"] for item in plan if item["action"] == "skip"],
        "wouldFail": [item["sourcePath"] for item in plan if item["action"] == "fail"],
    }


def list_manifest(project_dir: Path) -> dict[str, Any]:
    """--list：只读返回现有 manifest。"""
    project_dir = project_dir.resolve()
    manifest = load_manifest(project_dir)
    return {
        "ok": True,
        "command": "convert_list",
        "manifest": MANIFEST_REL,
        "generatedAt": manifest.get("generatedAt", ""),
        "files": manifest.get("files") or [],
        "count": len(manifest.get("files") or []),
    }


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
def _normalize_files(raw_values: list[str] | None) -> list[Path]:
    if not raw_values:
        raise errors.BgError(errors.E_USAGE, "缺少 --files/--file", hint="传入一个或多个源文件绝对路径")
    tokens: list[str] = []
    for value in raw_values:
        for token in str(value).split(","):
            token = token.strip().strip('"')
            if token:
                tokens.append(token)
    if not tokens:
        raise errors.BgError(errors.E_USAGE, "--files 不能为空", hint="传入源文件绝对路径")
    files: list[Path] = []
    seen: set[str] = set()
    for token in tokens:
        path = Path(token)
        if not path.is_absolute():
            raise errors.BgError(
                errors.E_USAGE,
                f"--files 只接受绝对路径：{token}",
                hint="由 Agent 解析会话附件的绝对路径后传入（禁止猜测相对路径）",
                detail={"file": token},
            )
        if not path.is_file():
            raise errors.BgError(
                errors.E_NOT_FOUND,
                f"源文件不存在：{token}",
                hint="确认文件路径；必须由用户上传/提供，禁止猜测",
                detail={"file": token},
            )
        suffix = path.suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_TYPES:
            raise errors.BgError(
                errors.E_USAGE,
                f"不支持的格式：{path.name}",
                hint="支持 .docx/.doc/.pdf/.md/.txt；其他格式请用户另存后再试",
                detail={"file": token, "supported": list(SUPPORTED_TYPES)},
            )
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise errors.BgError(
                errors.E_USAGE,
                f"文件超过 200 MB：{path.name}",
                hint="请拆分或压缩源文件后重试（PRD F3.2 Step 1）",
                detail={"file": token, "sizeBytes": size},
            )
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        files.append(Path(key))
    return files


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def _build_test_docx(target: Path, *, variant: str = "first") -> None:
    from docx import Document

    document = Document()
    document.add_heading("第一章 项目概述", level=1)
    paragraph = document.add_paragraph("本项目为智慧交通运维服务采购，")
    run = paragraph.add_run("投标人须具备相关资质。")
    run.bold = True
    document.add_heading("1.1 项目背景", level=2)
    document.add_paragraph(variant + "：杭州市智慧交通系统运维项目，覆盖前端设备与平台。")
    document.add_paragraph("1. 前端设备运维", style="List Number")
    document.add_paragraph("2. 平台运行维护", style="List Number")
    table = document.add_table(rows=2, cols=3)
    headers = ["评审因素", "分值", "评分标准"]
    for index, text in enumerate(headers):
        table.cell(0, index).text = text
    values = ["技术方案", "40", "方案完整、可行"]
    for index, text in enumerate(values):
        table.cell(1, index).text = text
    document.add_paragraph("图1 系统架构示意（题注）", style="Caption")
    # 注入文本框（w:txbxContent）：正文段落 1 个 + 表格单元格 1 个（M3.2 P1 回归）
    from docx.oxml import parse_xml

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body_textbox = (
        f'<w:p xmlns:w="{w_ns}"><w:r><w:txbxContent><w:p><w:r>'
        '<w:t>文本框：前附表 2.2 采购人 洪湖市公安局</w:t></w:r></w:p></w:txbxContent></w:r></w:p>'
    )
    document.element.body.append(parse_xml(body_textbox))
    cell_paragraph = table.cell(1, 2).paragraphs[0]
    cell_textbox = (
        f'<w:r xmlns:w="{w_ns}"><w:txbxContent><w:p><w:r>'
        '<w:t>表格文本框：技术要求第1项</w:t></w:r></w:p></w:txbxContent></w:r>'
    )
    cell_paragraph._p.append(parse_xml(cell_textbox))

    # M3.3 保真夹具：w:sym 复选框 / fldChar+ffData 旧式复选框 / w14:checkbox / 合并单元格 / 未知形状
    w14_ns = "http://schemas.microsoft.com/office/word/2010/wordml"
    symbol_paragraph = (
        f'<w:p xmlns:w="{w_ns}"><w:r><w:t>是否允许分包：</w:t></w:r>'
        '<w:r><w:sym w:font="Wingdings" w:char="F0FE"/><w:t>不允许</w:t></w:r></w:p>'
    )
    legacy_checkbox = (
        f'<w:p xmlns:w="{w_ns}"><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:ffData><w:checkBox><w:checked/></w:checkBox></w:ffData></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>√</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    w14_checkbox = (
        f'<w:p xmlns:w="{w_ns}" xmlns:w14="{w14_ns}"><w:sdt><w:sdtPr>'
        '<w14:checkbox><w14:checked w14:val="0"/></w14:checkbox></w:sdtPr>'
        '<w:sdtContent><w:r><w:t>☐</w:t></w:r></w:sdtContent></w:sdt></w:p>'
    )
    document.element.body.append(parse_xml(symbol_paragraph))
    document.element.body.append(parse_xml(legacy_checkbox))
    document.element.body.append(parse_xml(w14_checkbox))
    merged = document.add_table(rows=2, cols=3)
    for index, text in enumerate(["合并标题", "跨列内容", "列3"]):
        merged.cell(0, index).text = text
    for index, text in enumerate(["横向合并", "被合并", "值"]):
        merged.cell(1, index).text = text
    merged.cell(0, 0).merge(merged.cell(0, 1))
    unsupported_shape = (
        f'<w:p xmlns:w="{w_ns}"><w:r><w:pict><w:shape id="shape1"/></w:pict></w:r></w:p>'
    )
    document.element.body.append(parse_xml(unsupported_shape))
    document.save(str(target))


def _build_test_pdf(target: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Bid Conversion PDF Fixture", fontsize=14)
    page.insert_text((72, 110), "This PDF fixture contains enough text for conversion.", fontsize=11)
    page.insert_text((72, 130), "Section 1 Procurement Requirements and Evaluation.", fontsize=11)
    document.save(str(target))
    document.close()


def _pdf_stack_available() -> bool:
    """PDF 读取组件是否齐全。

    PDF 依赖是可选组件（由 env-check 的 ``pdf_libraries`` 单独报告、可由
    环境修复安装）。缺失时自检应跳过 PDF 环节，否则代表“插件脚本完整性”的
    必过项会跟着失败，把可选依赖缺失误报成插件缺陷。
    """
    try:
        import fitz  # noqa: F401
        import pdfplumber  # noqa: F401
        import pymupdf4llm  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何缺失都视为不可用
        return False
    return True


def run_selftest() -> dict[str, Any]:
    """脚本自检：docx/pdf/md/txt 转换、manifest、幂等 skip、变更备份、负例。"""
    checks: list[str] = []
    pdf_available = _pdf_stack_available()
    tmp = tempdir.mkdtemp(prefix="bidgenie-convert-selftest-")
    try:
        project_dir = tmp / "project"
        (project_dir / "sources").mkdir(parents=True)
        docx_path = tmp / "示例招标文件.docx"
        _build_test_docx(docx_path)
        md_path = tmp / "说明.md"
        md_path.write_text("# 已有 Markdown\n\n正文内容。\n", encoding="utf-8", newline="\n")
        txt_path = tmp / "说明.txt"
        txt_path.write_text("第一段文本。\n\n第二段文本。\n", encoding="utf-8", newline="\n")
        pdf_path = tmp / "示例文件.pdf"
        requested = [docx_path, md_path, txt_path]
        if pdf_available:
            requested.append(pdf_path)
        import contextlib

        # 第三方 import 可能向 stdout 打印横幅；自检 --json 必须保持单对象，横幅重定向到 stderr
        with contextlib.redirect_stdout(sys.stderr):
            if pdf_available:
                _build_test_pdf(pdf_path)
            first = convert_files(project_dir, requested, per_file_timeout=60)
        checks.append("convert")
        if len(first["converted"]) != len(requested) or first["failed"]:
            raise AssertionError(f"首次转换结果不正确：converted={first['converted']} failed={first['failed']}")
        docx_md = project_dir / "sources" / "converted" / "示例招标文件.md"
        if not docx_md.is_file():
            raise AssertionError("docx 转换产物缺失")
        content = docx_md.read_text(encoding="utf-8")
        for expected in (
            "# 第一章 项目概述",
            "## 1.1 项目背景",
            "| 评审因素 | 分值 | 评分标准 |",
            "**投标人须具备相关资质。**",
            "文本框：前附表 2.2 采购人 洪湖市公安局",
            "表格文本框：技术要求第1项",
            "☑不允许",
            "☐",
        ):
            if expected not in content:
                raise AssertionError(f"docx markdown 缺少：{expected!r}")
        checks.append("docx_markdown")
        checks.append("checkbox_symbol_fidelity")
        if pdf_available:
            pdf_md = project_dir / "sources" / "converted" / "示例文件.md"
            if not pdf_md.is_file() or "Bid Conversion PDF Fixture" not in pdf_md.read_text(encoding="utf-8"):
                raise AssertionError("PDF 转换产物不正确")
            checks.append("pdf_markdown")
        else:
            # PDF 组件是可选依赖，缺失时不能让必过项 scripts_integrity 失败；
            # 缺失本身由 env-check 的 pdf_libraries 单独报告。
            checks.append("pdf_skipped_missing_deps")
        manifest = json.loads((project_dir / MANIFEST_REL).read_text(encoding="utf-8"))
        if len(manifest.get("files") or []) != len(requested) or not manifest.get("converter"):
            raise AssertionError(f"manifest 不正确：{manifest}")
        if not any(str(entry.get("hash") or "").startswith("sha256:") for entry in manifest["files"]):
            raise AssertionError("manifest 缺少 hash")
        docx_entry = next(entry for entry in manifest["files"] if entry.get("sourceName") == "示例招标文件.docx")
        if int(docx_entry.get("textboxCount") or 0) < 2 or int(docx_entry.get("textboxChars") or 0) <= 0:
            raise AssertionError(f"manifest 文本框质量标记不正确：{docx_entry}")
        if not any(str(warning).startswith("textboxes_extracted:") for warning in docx_entry.get("warnings") or []):
            raise AssertionError(f"manifest 缺少 textboxes_extracted warning：{docx_entry.get('warnings')}")
        checks.append("docx_textbox")
        warnings_text = " ".join(str(item) for item in docx_entry.get("warnings") or [])
        if int(docx_entry.get("symbolsMapped") or 0) < 1 or int(docx_entry.get("checkboxesPreserved") or 0) < 2:
            raise AssertionError(f"符号/复选框保真计数不正确：{docx_entry}")
        if int(docx_entry.get("mergedCellsFlattened") or 0) < 1 or int(docx_entry.get("unsupportedShapes") or 0) < 1:
            raise AssertionError(f"合并单元格/形状保真计数不正确：{docx_entry}")
        if "symbols_mapped:" not in warnings_text or "checkboxes_preserved:" not in warnings_text:
            raise AssertionError(f"manifest 缺少符号/复选框 warning：{warnings_text}")
        if "merged_cells_flattened:" not in warnings_text or "unsupported_shape:" not in warnings_text:
            raise AssertionError(f"manifest 缺少合并单元格/形状 warning：{warnings_text}")
        checks.append("fidelity_markers")
        sources = manifest.get("sources")
        if not isinstance(sources, list) or len(sources) != len(manifest["files"]):
            raise AssertionError(f"manifest 缺少 sources[] 或长度不一致：{len(sources or [])}")
        for file_entry, source_entry in zip(manifest["files"], sources):
            if not {"sourcePath", "sourceName", "sourceType", "convertedPath", "status"} <= set(source_entry):
                raise AssertionError(f"sources[] 条目字段不全：{source_entry}")
            if source_entry.get("sourcePath") != file_entry.get("sourcePath") or source_entry.get("convertedPath") != file_entry.get("convertedPath"):
                raise AssertionError("sources[] 与 files[] 数据不一致")
        if not (manifest.get("updatedAt") or ""):
            raise AssertionError("manifest 缺少 updatedAt")
        checks.append("manifest")
        checks.append("manifest_sources")
        # D3：原件复制进 sources/raw/，manifest 记录项目相对 rawCopyPath
        for entry in manifest["files"]:
            raw_rel = str(entry.get("rawCopyPath") or "")
            if not raw_rel.startswith("sources/raw/"):
                raise AssertionError(f"manifest 缺少 rawCopyPath：{entry.get('sourceName')}")
            if not (project_dir / raw_rel).is_file():
                raise AssertionError(f"rawCopyPath 指向的文件不存在：{raw_rel}")
        if not any((project_dir / "sources" / "raw").glob("*")):
            raise AssertionError("sources/raw 为空")
        checks.append("raw_copy")
        # P2-11：PDF 回退页标记 + 表格复核元数据（构造小样，不解析真实大 PDF）
        if pdf_available:
            pdf_entry = next(entry for entry in manifest["files"] if entry.get("sourceName") == "示例文件.pdf")
            recheck = pdf_entry.get("tableRecheck") or {}
            if (
                pdf_entry.get("keyDataFromFallbackOnly") is not False
                or int(pdf_entry.get("fallbackPageCount") or 0) != 0
                or pdf_entry.get("fallbackPages") != []
                or not isinstance(recheck, dict)
                or recheck.get("mode") != "full"
                or int(recheck.get("sampledPages") or 0) < 1
            ):
                raise AssertionError(f"PDF 无回退时 manifest 标记不正确：{pdf_entry}")
        coverage = _pdf_page_coverage(
            "# 首页\n\n第一页正文内容足够长足以覆盖抽样校验并且包含表格数据\n",
            ["第一页正文内容足够长足以覆盖抽样校验并且包含表格数据", "第2页关键数据 2334 1936 1691 81 909"],
        )
        coverage_markdown = str(coverage.get("markdown") or "")
        if (
            coverage.get("fallbackPages") != [2]
            or coverage.get("fallbackPageCount") != 1
            or coverage.get("keyDataFromFallbackOnly") is not True
            or "关键数据仅来自回退文本" not in str(coverage.get("fallbackNote") or "")
            or "## 第 2 页（转换回退文本）" not in coverage_markdown
            or "第2页关键数据 2334 1936 1691 81 909" not in coverage_markdown
            or "key_data_from_fallback_only:2" not in list(coverage.get("warnings") or [])
            or "pdf_pages_fallback:2" not in list(coverage.get("warnings") or [])
            or "pdf_pages_fallback_count:1" not in list(coverage.get("warnings") or [])
        ):
            raise AssertionError(f"PDF 回退页标记不正确：{coverage}")
        checks.append("pdf_fallback_markers")
        full_plan = _table_recheck_plan(PDF_TABLE_RECHECK_FULL_MAX_PAGES, [])
        sampled_plan = _table_recheck_plan(261, [49, 11])
        if (
            full_plan.get("mode") != "full"
            or len(full_plan.get("pages") or []) != PDF_TABLE_RECHECK_FULL_MAX_PAGES
            or sampled_plan.get("mode") != "sampled"
            or len(sampled_plan.get("pages") or []) > PDF_TABLE_RECHECK_SAMPLE_MAX_PAGES
            or 49 not in (sampled_plan.get("pages") or [])
            or 11 not in (sampled_plan.get("pages") or [])
            or int(sampled_plan.get("fallbackSampled") or 0) != 2
            or list(sampled_plan.get("pages") or []) != sorted(sampled_plan.get("pages") or [])
        ):
            raise AssertionError(f"大 PDF 表格复核抽样计划不正确：{sampled_plan}")
        checks.append("pdf_table_recheck_plan")

        # 幂等：未变化 → skipped，md mtime 不变
        before_mtime = docx_md.stat().st_mtime
        second = convert_files(project_dir, [docx_path, md_path], per_file_timeout=60)
        if second["converted"] or len(second["skipped"]) != 2:
            raise AssertionError(f"幂等 skip 不正确：{second['converted']} / {second['skipped']}")
        if docx_md.stat().st_mtime != before_mtime:
            raise AssertionError("skip 时 md mtime 变化")
        checks.append("idempotent_skip")

        # 修改源文件 → 重新转换 + 备份旧 md
        time.sleep(0.02)
        _build_test_docx(docx_path, variant="修改后")
        third = convert_files(project_dir, [docx_path], per_file_timeout=60)
        if len(third["converted"]) != 1:
            raise AssertionError(f"变更后未重新转换：{third}")
        backups = [entry for entry in (project_dir / "sources").iterdir() if entry.name.startswith(BACKUP_DIR_PREFIX)]
        if not backups:
            raise AssertionError("变更后未生成 sources/.backup-<ts>/")
        checks.append("backup_on_change")

        # --check 只读
        plan = check_files(project_dir, [docx_path.resolve()])
        if plan["wouldSkip"] != [str(docx_path.resolve())]:
            raise AssertionError(f"--check 预检不正确：{plan}")

        # 负例：缺失文件 / 不支持的格式
        try:
            _normalize_files([str(tmp / "不存在.docx")])
            raise AssertionError("缺失文件未报错")
        except errors.BgError as exc:
            if exc.code != errors.E_NOT_FOUND:
                raise AssertionError(f"缺失文件错误码不正确：{exc.code}")
        bad = tmp / "bad.xyz"
        bad.write_text("x", encoding="utf-8")
        try:
            _normalize_files([str(bad)])
            raise AssertionError("不支持格式未报错")
        except errors.BgError as exc:
            if exc.code != errors.E_USAGE:
                raise AssertionError(f"不支持格式错误码不正确：{exc.code}")
        checks.append("validation")

        return {
            "ok": True,
            "selftest": {
                "script": "bg_convert.py",
                "python": __import__("platform").python_version(),
                "pdfAvailable": pdf_available,
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_convert.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（CONTRACT §10.1 + H.1 #6 兼容参数）。"""
    parser = errors.CliParser(
        prog="bg_convert.py",
        description="BidGenie M3 文件转换器：docx/doc/pdf/md/txt → sources/converted/*.md + manifest",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--files", action="append", help="源文件绝对路径，逗号分隔或重复传入")
    parser.add_argument("--file", action="append", dest="files_alias", help="--files 的兼容别名（可重复）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="执行转换（默认）")
    action.add_argument("--check", action="store_true", help="只读预检：输出将 convert/skip 的文件，不写文件")
    action.add_argument("--list", dest="list_mode", action="store_true", help="只读列出现有 conversion-manifest")
    parser.add_argument("--force", action="store_true", help="忽略未变化判定，强制重新转换")
    parser.add_argument("--no-backup", dest="no_backup", action="store_true", help="不创建 sources/.backup-<ts>/")
    parser.add_argument("--no-copy-raw", dest="no_copy_raw", action="store_true", help="不把原件复制到 sources/raw/（默认复制，保证项目自包含）")
    return parser


def handle(args: Any) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if getattr(args, "list_mode", False):
        return list_manifest(project_dir)
    raw_files = list(getattr(args, "files", None) or []) + list(getattr(args, "files_alias", None) or [])
    files = _normalize_files(raw_files)
    if getattr(args, "check", False):
        return check_files(project_dir, files)
    per_file_timeout = errors.timeout_value(args, default=PER_FILE_TIMEOUT_SEC)
    per_file_timeout = min(max(per_file_timeout, 10.0), PER_FILE_TIMEOUT_SEC)
    return convert_files(
        project_dir,
        files,
        force=bool(getattr(args, "force", False)),
        no_backup=bool(getattr(args, "no_backup", False)),
        copy_raw=not bool(getattr(args, "no_copy_raw", False)),
        per_file_timeout=per_file_timeout,
        workspace=workspace,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）；先挂载 plugin/pylibs（PDF 依赖）。"""
    deps.ensure_dependency_path()
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_convert.py",
    )


if __name__ == "__main__":
    sys.exit(main())
