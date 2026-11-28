#!/usr/bin/env python3
"""bg_extract_validate.py：九大类提取结果校验（M3.2 结构校验版，CONTRACT §10.3 / Traecode 9 模板）。

M3.2 起提取文件改为**人类可读结构**（Traecode 9 模板输出示例）：
    # <标题>                 ← H1，与模板 manifest 的 title 一致，全文件唯一
    ## 一、<章节名>          ← H2，必须覆盖模板 expectedSections（源编号由导出脚本剥离）
    ... 正文/表格 ...          ← 禁止「字段键/字段值/字段名/证据（来源…」机器表、禁止 frontmatter 字段表

用法：
    python bg_extract_validate.py --workspace <ws> --project <dir|id> [--json]
    python bg_extract_validate.py --workspace <ws> --project <dir|id> --scope all|common|packages \
        [--package package-<N>] [--apply|--report|--check] [--strict] [--json]
    python bg_extract_validate.py --selftest [--json]

校验维度：
1. 9 文件存在（common 01–05、每包 06–09）、可读、非空；
2. 首 `#` 标题（H1）唯一且与 manifest `title` 一致；
3. `## expectedSections` 齐全（manifest 缺失时回退模板「输出示例」→ 内置 Traecode 快照），
   顺序错/多余章节 → warning；
4. **禁止机器表头**：字段键/字段值/字段名/证据（来源…；禁止 frontmatter 字段表；
5. 07：`技术评分标准` 章节必须有三列表（评审因素/分值/评分标准）+ 行合计与声明满分一致（容差 0.5）；
6. 表格/正文非空；
7. 包目录与 `project.json.packageCount` 一致。

输出 `_extraction-report.json`（`extraction/common/` 全局报告 + 每包报告）；blocker → exit 4。

`project.extensions.extractionCaveats`（P2-12）：按「需用户确认/转换说明/疑似缺失/…」标记行抽取后
归一化去重——跳过纯标题行（`**需用户确认：**` 空条目）、同包内按文本相似度合并重复项（履约担保/
备件表/付款方式等），每条保留 `source/sources/packages/duplicateCount` 并按包合并展示。

`report.coverage`（P3-14）：`criteriaCount` 为**当前校验范围**的评分项数；`all` 范围且多包时
`criteriaCountScope=all_packages_total` 并在 `criteriaCountNote/criteriaByPackage` 注明「全包合计」
口径，单包 outline/审查不要按全包合计核对。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib, tempdir  # noqa: E402
from _lib import extraction_risk  # noqa: E402

SCHEMA_VERSION = "1.0"
REPORT_FILE_NAME = "_extraction-report.json"
TEMPLATE_MANIFEST_REL = Path("templates") / "extraction" / "_template-manifest.json"
TEMPLATE_MANIFEST = _PLUGIN_ROOT / TEMPLATE_MANIFEST_REL

COMMON_TEMPLATE_IDS = ("01", "02", "03", "04", "05")
PACKAGE_TEMPLATE_IDS = ("06", "07", "08", "09")
SCORE_TOLERANCE = 0.5
MIN_CONTENT_CHARS = 10
MIN_SCORING_ROWS = 1

#: 禁止出现的机器表头/旧字段表标记（发现即 blocker）
FORBIDDEN_MARKERS: tuple[str, ...] = (
    "字段键",
    "字段值",
    "字段名",
    "证据（来源",
    "证据(来源",
)

#: 模板 manifest 未提供 expectedSections、模板文件无「输出示例」时的兜底（来自 Traecode 9 模板输出示例）
DEFAULT_EXPECTED_SECTIONS: dict[str, tuple[str, ...]] = {
    "01": ("项目信息", "资金与限价", "保证金相关", "投标方式与说明", "时间与地点", "组织单位与联系"),
    "02": ("投标人的资格要求", "符合性审查", "资格性审查"),
    "03": ("废标情形", "无效投标情形", "不得存在的情形"),
    "04": ("投标文件格式", "投标文件编制要求"),
    "05": (
        "企业证书证明",
        "拟派人员要求",
        "业绩与车辆要求",
        "书面承诺/声明",
        "各信用平台查询截图",
        "产品/服务厂商授权、第三方检测报告",
        "其它实质性应标资料",
    ),
    "06": ("标段信息", "采购内容"),
    "07": ("评标办法", "商务评分标准", "技术评分标准", "报价评分标准", "分值合计"),
    "08": ("企业资质要求", "人员资质要求", "业绩要求", "财务要求", "服务要求", "材料要求", "付款方式"),
    "09": (),
}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBER_PREFIX_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百零〇两\d]+(?:部分|[章节篇])\s*[、.．:：]?\s*"
    r"|[一二三四五六七八九十]+\s*[、.．:：]\s*"
    r"|\d+(?:\.\d+)+\s+"
    r"|\d+(?:\.\d+)*\s*[、.．:：)]\s*"
    r"|[（(]\d+[)）]\s*"
    r"|[①-⑳]\s*"
    r")"
)
_SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
_DECLARED_TOTAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"技术(?:评分|部分)?(?:分)?(?:满分|合计|总分)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?"),
    re.compile(r"技术分满分\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?"),
)


# ---------------------------------------------------------------------------
# 模板 manifest / expectedSections
# ---------------------------------------------------------------------------
def load_template_manifest() -> dict[str, Any]:
    """读取并校验模板 manifest（9 模板）。"""
    if not TEMPLATE_MANIFEST.is_file():
        raise errors.BgError(
            errors.E_DEP_MISSING,
            "缺少提取模板清单 templates/extraction/_template-manifest.json",
            hint="插件包不完整，请重新安装/更新插件",
            detail={"path": TEMPLATE_MANIFEST_REL.as_posix()},
            exit_code=errors.EXIT_DEP,
        )
    document = jsonio.read_json(TEMPLATE_MANIFEST)
    if not isinstance(document, dict) or not isinstance(document.get("templates"), list):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "模板清单结构非法（缺少 templates 数组）",
            detail={"path": TEMPLATE_MANIFEST_REL.as_posix()},
        )
    templates = [item for item in document["templates"] if isinstance(item, dict)]
    document["templates"] = templates
    ids = [str(item.get("id") or "") for item in templates]
    if sorted(ids) != sorted(COMMON_TEMPLATE_IDS + PACKAGE_TEMPLATE_IDS):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "模板清单与 9 类基线不一致",
            hint="检查 templates/extraction/_template-manifest.json（应为 01–09）",
            detail={"ids": ids},
        )
    document["_by_id"] = {str(item.get("id")): item for item in templates}
    return document


def template_conventions(manifest: dict[str, Any]) -> dict[str, Any]:
    conventions = manifest.get("conventions")
    return dict(conventions) if isinstance(conventions, dict) else {}


def template_version(manifest: dict[str, Any]) -> str:
    return str(manifest.get("templateVersion") or manifest.get("schemaVersion") or "1.0.0")


def strip_heading_numbering(title: str) -> str:
    """剥离标题前缀编号（第一章 / 一、 / 1.1 / （1） / ① 等），导出与校验共用语义。"""
    text = (title or "").strip().strip("#").strip()
    for _ in range(3):
        match = _NUMBER_PREFIX_RE.match(text)
        if not match:
            break
        text = text[match.end() :].strip()
    return text.strip()


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", strip_heading_numbering(title))


def _sections_from_output_example(template_path: Path) -> list[str]:
    """从模板文件「输出示例」代码块提取 H2 章节名（reviewer-a v2 模板/兜底用）。"""
    if not template_path.is_file():
        return []
    try:
        text = template_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    start = -1
    for index, line in enumerate(lines):
        if "输出示例" in line:
            start = index
            break
    if start < 0:
        return []
    in_fence = False
    sections: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                break
            in_fence = True
            continue
        if not in_fence:
            continue
        match = re.match(r"^##\s+(.+?)\s*$", stripped)
        if match:
            section = strip_heading_numbering(match.group(1))
            if section and section not in sections:
                sections.append(section)
    return sections


def expected_sections_for(template: dict[str, Any], template_path: Path | None = None) -> list[str]:
    """解析模板的 expectedSections：manifest → 模板输出示例 → 内置 Traecode 快照。"""
    for key in ("expectedSections", "expected_sections", "sections"):
        raw = template.get(key)
        if isinstance(raw, list):
            sections: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    title = item
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("label") or item.get("heading") or "")
                else:
                    title = ""
                title = strip_heading_numbering(title)
                if title and title not in sections:
                    sections.append(title)
            if sections:
                return sections
    if template_path is not None:
        derived = _sections_from_output_example(template_path)
        if derived:
            return derived
    return list(DEFAULT_EXPECTED_SECTIONS.get(str(template.get("id")), ()))


def template_path_for(template: dict[str, Any]) -> Path | None:
    """模板文件路径（模板目录仅用于读取「输出示例」，不修改）。"""
    file_name = template.get("fileName")
    doc_class = template.get("docClass")
    if not file_name:
        return None
    subdir = "common" if doc_class == "common" else "packages"
    candidate = TEMPLATE_MANIFEST.parent / subdir / str(file_name)
    return candidate


# ---------------------------------------------------------------------------
# Markdown 解析（结构校验用）
# ---------------------------------------------------------------------------
def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    return cells


def _is_separator_row(cells: list[str]) -> bool:
    non_empty = [cell for cell in cells if cell.strip()]
    return bool(non_empty) and all(_SEPARATOR_CELL_RE.match(cell.strip()) for cell in non_empty) and any(
        "-" in cell for cell in non_empty
    )


def has_frontmatter(text: str) -> bool:
    """检测 frontmatter 字段表（M3.2 禁止）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1 : min(len(lines), 40)]:
        if line.strip() == "---":
            return True
        if ":" in line:
            continue
    return False


def parse_markdown_structure(text: str) -> dict[str, Any]:
    """解析标题/表格/正文（不做 frontmatter 剥离，违规由校验判定）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    headings: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    section_of_line: dict[int, str] = {}
    current_section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings.append({"level": level, "title": title, "line": index + 1})
            if level == 2:
                current_section = strip_heading_numbering(title)
            elif level == 1:
                current_section = ""
            index += 1
            continue
        if stripped.startswith("|"):
            block_start = index
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            if len(block) >= 2:
                header = _split_markdown_row(block[0])
                separator = _split_markdown_row(block[1])
                if _is_separator_row(separator):
                    rows = [_split_markdown_row(row) for row in block[2:] if row.strip()]
                    tables.append(
                        {
                            "section": current_section,
                            "line": block_start + 1,
                            "header": header,
                            "rows": rows,
                        }
                    )
            continue
        index += 1
    # 正文文本（排除标题/表格/代码围栏/空行/HTML 注释）
    body_lines: list[str] = []
    in_fence = False
    in_comment = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue
        if not stripped or stripped.startswith("|") or _HEADING_RE.match(stripped):
            continue
        if stripped in ("---", "***", "___"):
            continue
        body_lines.append(stripped)
    return {
        "headings": headings,
        "tables": tables,
        "body_lines": body_lines,
        "body_chars": sum(len(line) for line in body_lines),
        "line_count": len(lines),
    }


# ---------------------------------------------------------------------------
# 07 评分表校验
# ---------------------------------------------------------------------------
def _extract_declared_total(text: str) -> float | None:
    """从「评标办法/分值合计」文本提取技术分声明满分（优先技术评分合计/技术分满分）。

    N10：旧实现对全文任意「满分 N 分」兜底，07 漏写技术分声明时会命中评分表里的
    「每项满分 4 分」，把 58 的分差报成「声明满分 4」。这里只认“技术分/技术评分/
    技术部分/技术总分 + 满分/合计/总分”的明确声明；找不到就让调用方提示缺声明。
    """
    for pattern in _DECLARED_TOTAL_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def _parse_number(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover
        return None


def validate_scoring(
    template: dict[str, Any],
    structure: dict[str, Any],
    text: str,
    report_path: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """07：技术评分标准必须三列 + 行合计与声明满分一致（容差 0.5）。"""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    target_section = None
    for heading in structure["headings"]:
        if heading["level"] == 2 and "技术评分标准" in strip_heading_numbering(heading["title"]):
            target_section = heading
            break
    if target_section is None:
        blockers.append(_blocker("scoring_section_missing", report_path, "", "缺少「技术评分标准」章节"))
        return None, blockers, warnings
    candidate_tables = [table for table in structure["tables"] if table.get("section") == strip_heading_numbering(
        target_section["title"]
    )]
    if not candidate_tables:
        blockers.append(_blocker("scoring_table_missing", report_path, "", "技术评分标准章节缺少评分表"))
        return None, blockers, warnings
    target_table = None
    for table in candidate_tables:
        header = [cell.strip() for cell in table["header"]]
        if (
            any("评审因素" in cell for cell in header)
            and any(cell.startswith("分值") or "分值" in cell for cell in header)
            and any("评分标准" in cell for cell in header)
        ):
            target_table = table
            break
    if target_table is None:
        blockers.append(
            _blocker("scoring_columns_missing", report_path, "技术评分标准", "评分表缺少「评审因素/分值/评分标准」三列")
        )
        return None, blockers, warnings
    header = [cell.strip() for cell in target_table["header"]]
    factor_index = next(index for index, cell in enumerate(header) if "评审因素" in cell)
    score_index = next(index for index, cell in enumerate(header) if "分值" in cell)
    standard_index = next(index for index, cell in enumerate(header) if "评分标准" in cell)
    total = 0.0
    rows = [row for row in target_table["rows"] if any(cell.strip() for cell in row)]
    total_row_value: float | None = None
    for row_index, row in enumerate(rows, start=1):
        factor = row[factor_index].strip() if factor_index < len(row) else ""
        score_text = row[score_index].strip() if score_index < len(row) else ""
        standard = row[standard_index].strip() if standard_index < len(row) else ""
        if any(marker in f"{factor}{score_text}{standard}" for marker in ("（其余", "（评审因素", "（分值", "（评分标准")):
            warnings.append(_warning("scoring_placeholder_row", report_path, f"row{row_index}", "评分表占位行已跳过，需替换为原文"))
            continue
        if "合计" in factor:
            total_row_value = _parse_number(score_text)
            continue
        if not factor:
            blockers.append(_blocker("scoring_factor_empty", report_path, f"row{row_index}", "评审因素为空"))
        if not standard:
            blockers.append(_blocker("scoring_standard_empty", report_path, f"row{row_index}", "评分标准为空"))
        score = _parse_number(score_text)
        if score is None:
            blockers.append(
                _blocker("scoring_value_unparsable", report_path, f"row{row_index}", f"分值无法解析为数字：{score_text!r}")
            )
        else:
            total += score
    if len([row for row in rows if "合计" not in (row[factor_index].strip() if factor_index < len(row) else "")]) < MIN_SCORING_ROWS:
        blockers.append(_blocker("scoring_rows_empty", report_path, "技术评分标准", "技术评分表没有数据行"))
    declared: float | None = _extract_declared_total(text)
    if declared is None and total_row_value is None:
        blockers.append(
            _blocker(
                "scoring_total_missing",
                report_path,
                "技术评分标准/分值合计",
                "缺少技术分满分声明（可写「技术分满分N分」），评分表也没有「合计」行，无法对账",
            )
        )
    elif declared is None:
        warnings.append(
            _warning(
                "scoring_declared_total_missing",
                report_path,
                f"未找到技术分满分声明，已按评分表合计 {total_row_value:g} 分对账；建议在「技术评分标准」补写「技术分满分{total_row_value:g}分」",
            )
        )
    if declared is not None and abs(total - declared) > SCORE_TOLERANCE:
        blockers.append(
            _blocker(
                "scoring_sum_mismatch",
                report_path,
                "技术评分标准",
                f"评分行合计 {total:g} 与声明满分 {declared:g} 差额 {total - declared:+g}（容差 ±{SCORE_TOLERANCE}）",
            )
        )
    if total_row_value is not None and abs(total_row_value - total) > SCORE_TOLERANCE:
        blockers.append(
            _blocker(
                "scoring_row_total_mismatch",
                report_path,
                "技术评分标准",
                f"表内合计 {total_row_value:g} 与各行分值和 {total:g} 不一致",
            )
        )
    scoring = {
        "file": report_path,
        "rows": len([row for row in rows if "合计" not in (row[factor_index].strip() if factor_index < len(row) else "")]),
        "totalScore": round(total, 2),
        "declaredTotal": declared if declared is not None else total_row_value,
        "issues": [item["code"] for item in blockers],
    }
    return scoring, blockers, warnings


# ---------------------------------------------------------------------------
# 单文件校验
# ---------------------------------------------------------------------------
def _blocker(code: str, file: str, pointer: str, message: str) -> dict[str, Any]:
    return {"code": code, "file": file, "pointer": pointer, "message": message}


def _warning(code: str, file: str, message: str) -> dict[str, Any]:
    return {"code": code, "file": file, "message": message}


def validate_file(
    file_path: Path,
    template: dict[str, Any],
    *,
    expected_package_id: str,
    manifest: dict[str, Any],
    project_dir: Path,
) -> dict[str, Any]:
    """校验单个提取文件（结构校验）。"""
    report_path = paths.rel_posix(project_dir, file_path)
    template_id = str(template.get("id") or "")
    expected_title = strip_heading_numbering(str(template.get("title") or ""))
    expected_sections = expected_sections_for(template, template_path_for(template))
    result: dict[str, Any] = {
        "path": report_path,
        "templateId": template_id,
        "ok": False,
        "missingSections": [],
        "extraSections": [],
        "warnings": [],
        "blockers": [],
        "expectedSections": expected_sections,
        "foundSections": [],
    }
    blockers: list[dict[str, Any]] = result["blockers"]
    warnings: list[dict[str, Any]] = result["warnings"]
    if not file_path.is_file():
        blockers.append(_blocker("file_missing", report_path, "", "提取文件不存在"))
        return result
    try:
        raw_text = file_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        blockers.append(_blocker("file_unreadable", report_path, "", f"文件不可读：{type(exc).__name__}"))
        return result
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    conventions = template_conventions(manifest)
    min_chars = int(conventions.get("minChars") or 200)
    if len(text.strip()) < min_chars:
        warnings.append(_warning("file_short", report_path, f"文件仅 {len(text.strip())} 字符（模板建议 ≥{min_chars}）"))

    # 1) 禁止 frontmatter 字段表 / 机器表头
    if has_frontmatter(text):
        blockers.append(_blocker("frontmatter_forbidden", report_path, "", "禁止 frontmatter 字段表（M3.2 输出为人类可读结构）"))
    for line_no, line in enumerate(text.split("\n"), start=1):
        for marker in FORBIDDEN_MARKERS:
            if marker in line:
                blockers.append(
                    _blocker(
                        "machine_table_forbidden",
                        report_path,
                        f"L{line_no}",
                        f"出现机器表头标记「{marker}」：{line.strip()[:80]}",
                    )
                )
                break

    structure = parse_markdown_structure(text)
    headings = structure["headings"]

    # 2) H1 唯一且与 manifest title 一致
    h1_headings = [heading for heading in headings if heading["level"] == 1]
    if not h1_headings:
        blockers.append(_blocker("h1_missing", report_path, "", "缺少 H1 标题（# <标题>）"))
    elif len(h1_headings) > 1:
        blockers.append(_blocker("h1_duplicate", report_path, "", f"存在 {len(h1_headings)} 个 H1（应仅有 1 个）"))
    else:
        actual_title = strip_heading_numbering(h1_headings[0]["title"])
        if normalize_title(actual_title) != normalize_title(expected_title):
            blockers.append(
                _blocker(
                    "h1_mismatch",
                    report_path,
                    "H1",
                    f"H1 标题 {actual_title!r} 与模板 title {expected_title!r} 不一致",
                )
            )

    # 3) expectedSections 齐全
    found_sections: list[str] = []
    for heading in headings:
        if heading["level"] != 2:
            continue
        section = strip_heading_numbering(heading["title"])
        if section:
            found_sections.append(section)
    result["foundSections"] = found_sections
    normalized_found = [normalize_title(section) for section in found_sections]
    missing: list[str] = []
    positions: list[int] = []
    for expected in expected_sections:
        normalized_expected = normalize_title(expected)
        try:
            position = normalized_found.index(normalized_expected)
        except ValueError:
            missing.append(expected)
            continue
        positions.append(position)
    if missing:
        result["missingSections"] = missing
        blockers.append(
            _blocker("section_missing", report_path, "H2", f"缺少 expectedSections：{missing}")
        )
    expected_normalized = {normalize_title(section) for section in expected_sections}
    # P2-5：`需用户确认` 是提取模板允许的附加章节（转换不可辨/矛盾时记录），
    # 不应作为 section_extra warning 污染校验结果；其他非预期章节仍提示。
    optional_normalized = {normalize_title("需用户确认")}
    extra = [
        section
        for section in found_sections
        if normalize_title(section) not in expected_normalized and normalize_title(section) not in optional_normalized
    ]
    if extra and expected_sections:
        # expectedSections=[]（如 09）表示模板不限定章节，自然分节不算异常
        result["extraSections"] = extra
        warnings.append(_warning("section_extra", report_path, f"存在非预期章节：{extra}"))
    if positions and positions != sorted(positions):
        warnings.append(_warning("section_order", report_path, "章节顺序与模板 expectedSections 不一致"))

    # 4) 表格/正文非空
    if structure["body_chars"] < MIN_CONTENT_CHARS and not structure["tables"]:
        blockers.append(_blocker("content_empty", report_path, "", "正文与表格均为空（至少需要正文或表格内容）"))

    # 5) 07 评分表
    scoring: dict[str, Any] | None = None
    if template_id == "07":
        scoring, scoring_blockers, scoring_warnings = validate_scoring(template, structure, text, report_path)
        blockers.extend(scoring_blockers)
        warnings.extend(scoring_warnings)
        if scoring is not None:
            result["scoring"] = scoring
    result["headings"] = [
        {"level": heading["level"], "title": heading["title"]} for heading in headings
    ]
    result["tables"] = len(structure["tables"])
    result["ok"] = not blockers
    return result


# ---------------------------------------------------------------------------
# 整项目校验
# ---------------------------------------------------------------------------
def _extraction_paths(project_dir: Path, template: dict[str, Any], package_id: str) -> Path:
    filename = str(template.get("fileName") or "")
    if template.get("docClass") == "common":
        return project_dir / "extraction" / "common" / filename
    return project_dir / "extraction" / "packages" / package_id / filename


def _load_project_for_validation(project_dir: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    path = project_dir / "project.json"
    blockers: list[dict[str, Any]] = []
    if not path.is_file():
        blockers.append(_blocker("project_missing", "project.json", "", "项目缺少 project.json"))
        return None, blockers
    try:
        document = jsonio.read_json(path)
    except errors.BgError as exc:
        blockers.append(_blocker("project_unreadable", "project.json", "", exc.message))
        return None, blockers
    if not isinstance(document, dict):
        blockers.append(_blocker("project_invalid", "project.json", "", "project.json 必须是 JSON 对象"))
        return None, blockers
    return document, blockers


def _package_dirs(project_dir: Path) -> list[str]:
    root = project_dir / "extraction" / "packages"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir() and re.match(r"^package-\d+$", entry.name))


def validate_project_extraction(
    project_dir: Path,
    *,
    scope: str = "all",
    only_package: str | None = None,
) -> dict[str, Any]:
    """执行整项目校验（不写文件）；返回 report 文档与汇总。"""
    project_dir = project_dir.resolve()
    manifest = load_template_manifest()
    by_id: dict[str, dict[str, Any]] = manifest["_by_id"]
    project, project_blockers = _load_project_for_validation(project_dir)
    blockers: list[dict[str, Any]] = list(project_blockers)
    warnings: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    scoring_tables: list[dict[str, Any]] = []
    package_count: int | None = None
    if project is not None:
        if project.get("packageCount") is None:
            blockers.append(_blocker("package_count_unknown", "project.json", "packageCount", "packageCount 未回填（T2 前必须确定分包数）"))
        elif not isinstance(project.get("packageCount"), int) or isinstance(project.get("packageCount"), bool) or project.get("packageCount") < 1:
            blockers.append(_blocker("package_count_invalid", "project.json", "packageCount", "packageCount 必须是 >=1 的整数"))
        else:
            package_count = int(project["packageCount"])
        if project.get("isSubcontracted") is None:
            warnings.append(_warning("subcontracted_unknown", "project.json", "isSubcontracted 未回填"))
    validate_common = scope in ("all", "common")
    validate_packages = scope in ("all", "packages")
    expected_packages: list[str] = []
    if validate_packages:
        if only_package:
            if not re.match(r"^package-\d+$", only_package):
                raise errors.BgError(errors.E_USAGE, "package 必须形如 package-<N>", detail={"package": only_package})
            expected_packages = [only_package]
            if package_count is not None and int(only_package.split("-")[1]) > package_count:
                blockers.append(_blocker("package_out_of_range", "project.json", "packageCount", f"{only_package} 超出 packageCount={package_count}"))
        elif package_count is not None:
            expected_packages = [f"package-{index}" for index in range(1, package_count + 1)]
        else:
            expected_packages = _package_dirs(project_dir)
        actual_packages = _package_dirs(project_dir)
        for expected in expected_packages:
            if expected not in actual_packages:
                blockers.append(_blocker("package_dir_missing", f"extraction/packages/{expected}", "", f"缺少包目录 {expected}"))
        if package_count is not None and not only_package:
            for actual in actual_packages:
                if actual not in expected_packages:
                    blockers.append(_blocker("orphan_package_dir", f"extraction/packages/{actual}", "", f"孤儿包目录（packageCount={package_count}）"))
    if validate_common:
        for template_id in COMMON_TEMPLATE_IDS:
            template = by_id[template_id]
            file_path = _extraction_paths(project_dir, template, "")
            result = validate_file(file_path, template, expected_package_id="", manifest=manifest, project_dir=project_dir)
            files.append(result)
        for package_id in expected_packages:
            for template_id in PACKAGE_TEMPLATE_IDS:
                template = by_id[template_id]
                file_path = _extraction_paths(project_dir, template, package_id)
                result = validate_file(
                    file_path, template, expected_package_id=package_id, manifest=manifest, project_dir=project_dir
                )
                files.append(result)
                if result.get("scoring"):
                    scoring_tables.append(result["scoring"])
    elif validate_packages:
        for package_id in expected_packages:
            for template_id in PACKAGE_TEMPLATE_IDS:
                template = by_id[template_id]
                file_path = _extraction_paths(project_dir, template, package_id)
                result = validate_file(
                    file_path, template, expected_package_id=package_id, manifest=manifest, project_dir=project_dir
                )
                files.append(result)
                if result.get("scoring"):
                    scoring_tables.append(result["scoring"])
    for result in files:
        blockers.extend(result.get("blockers") or [])
        warnings.extend(result.get("warnings") or [])
    criteria_count = sum((table.get("rows") or 0) for table in scoring_tables)
    criteria_by_package: dict[str, int] = {}
    for table in scoring_tables:
        package_key = str(table.get("package") or table.get("packageId") or "")
        if not package_key:
            package_id_match = re.search(r"extraction/packages/(package-\d+)/", str(table.get("path") or table.get("file") or ""))
            package_key = package_id_match.group(1) if package_id_match else "unknown"
        criteria_by_package[package_key] = criteria_by_package.get(package_key, 0) + int(table.get("rows") or 0)
    report_scope = scope if scope in ("common", "packages") else "all"
    if only_package:
        report_scope = only_package
    if report_scope == "all" and len(expected_packages) > 1:
        criteria_scope = "all_packages_total"
        criteria_note = (
            f"全包合计口径：{len(expected_packages)} 个包的评分项合计；单包大纲/审查只取本包评分项"
            "（见 criteriaByPackage），不要按全包合计核对单包覆盖"
        )
    else:
        criteria_scope = str(report_scope)
        criteria_note = "当前校验范围口径（criteriaCount 只统计本次校验文件的评分项）"
    summary = {
        "filesTotal": len(files),
        "filesOk": sum(1 for result in files if result.get("ok")),
        "blockerCount": len(blockers),
        "warningCount": len(warnings),
        "packages": len(expected_packages),
        "packageCount": package_count,
    }
    passed = not blockers
    document: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "scope": report_scope,
        "validatedAt": paths.iso_now(),
        "ok": passed,
        "passed": passed,
        "files": files,
        "scoringTable": scoring_tables[0] if scoring_tables else None,
        "scoringTables": scoring_tables,
        "coverage": {
            "criteriaCount": criteria_count,
            "criteriaCountScope": criteria_scope,
            "criteriaCountNote": criteria_note,
            "criteriaByPackage": criteria_by_package,
            "criteriaWithSource": criteria_count,
        },
        "blockers": blockers,
        "warnings": warnings,
        "summary": summary,
    }
    return {
        "report": document,
        "project_dir": project_dir,
        "expected_packages": expected_packages,
        "package_count": package_count,
    }


# ---------------------------------------------------------------------------
# 报告写盘
# ---------------------------------------------------------------------------
def write_reports(project_dir: Path, report: dict[str, Any]) -> list[str]:
    """写 `extraction/common/_extraction-report.json`（全局）与包级报告；返回相对路径列表。"""
    written: list[str] = []
    common_dir = project_dir / "extraction" / "common"
    common_dir.mkdir(parents=True, exist_ok=True)
    common_report = dict(report)
    common_report["scope"] = "all"
    target = common_dir / REPORT_FILE_NAME
    jsonio.write_json_atomic(target, common_report)
    written.append(paths.rel_posix(project_dir, target))
    packages = sorted(
        {
            str((item.get("path") or "").split("/")[2])
            for item in report.get("files") or []
            if str(item.get("path") or "").startswith("extraction/packages/package-")
        }
    )
    for package in packages:
        package_dir = project_dir / "extraction" / "packages" / package
        if not package_dir.is_dir():
            continue
        package_files = [
            item
            for item in report.get("files") or []
            if str(item.get("path") or "").startswith(f"extraction/packages/{package}/")
        ]
        package_report = {
            "schemaVersion": SCHEMA_VERSION,
            "scope": package,
            "validatedAt": report.get("validatedAt"),
            "ok": all(item.get("ok") for item in package_files),
            "passed": all(item.get("ok") for item in package_files),
            "files": package_files,
            "scoringTable": next((item.get("scoring") for item in package_files if item.get("scoring")), None),
            "blockers": [
                item
                for item in report.get("blockers") or []
                if str(item.get("file") or "").startswith(f"extraction/packages/{package}/")
                or str(item.get("file") or "") == "project.json"
            ],
            "warnings": [
                item
                for item in report.get("warnings") or []
                if str(item.get("file") or "").startswith(f"extraction/packages/{package}/")
            ],
            "summary": {
                "filesTotal": len(package_files),
                "filesOk": sum(1 for item in package_files if item.get("ok")),
            },
        }
        package_target = package_dir / REPORT_FILE_NAME
        jsonio.write_json_atomic(package_target, package_report)
        written.append(paths.rel_posix(project_dir, package_target))
    return written


# ---------------------------------------------------------------------------
# 自检辅助：生成合法提取样例（M3.2 人类可读结构；供 --selftest 与 tests/m3 使用）
# ---------------------------------------------------------------------------
_CN_NUMBERS = "一二三四五六七八九十"


def _cn_section(index: int) -> str:
    return _CN_NUMBERS[index] if 0 <= index < len(_CN_NUMBERS) else str(index + 1)


def _file_content(template: dict[str, Any], sections: list[str]) -> str:
    """按模板 id 生成一段合法、带真实感的中文内容（供自检/测试）。"""
    template_id = str(template.get("id"))
    lines: list[str] = []
    if template_id == "01":
        contact_rows = [
            "| 采购单位 | XX市城市管理局 | 张主任 | 0571-12345678 | XX市XX区XX路123号 |",
            "| 代理机构 | XX招标代理有限公司 | 李经理 | 0571-87654321 | XX市XX区XX大厦15楼 |",
        ]
        for index, section in enumerate(sections):
            lines.append(f"## {_cn_section(index)}、{section}")
            lines.append("")
            if section == "项目信息":
                lines.append("| 项目属性 | 属性值 | 备注 |")
                lines.append("|----------|--------|------|")
                lines.append("| 项目编号 | TEST-2026-001 | |")
                lines.append("| 项目名称 | 智慧交通运维服务项目 | |")
                lines.append("| 采购方式 | 公开招标 | |")
            elif section == "资金与限价":
                lines.append("| 资金类型 | 金额 | 备注 |")
                lines.append("|----------|------|------|")
                lines.append("| 预算金额 | 5000万元 | 财政性资金 |")
                lines.append("| 最高限价 | 4800万元 | 不得超过 |")
            elif section == "保证金相关":
                lines.append("| 事项 | 要求 | 备注 |")
                lines.append("|------|------|------|")
                lines.append("| 是否提交 | 是 | |")
                lines.append("| 提交金额 | 96万元 | 最高限价的2% |")
            elif section == "投标方式与说明":
                lines.append("本项目采用线上投标方式，投标人需在政府采购平台上传电子投标文件，文件格式为 PDF。")
            elif section == "时间与地点":
                lines.append("| 类型 | 具体内容 |")
                lines.append("|------|----------|")
                lines.append("| 投标截止时间 | 2026年4月1日 9:00 |")
                lines.append("| 开标时间 | 2026年4月1日 9:30 |")
                lines.append("| 开标地点 | XX市公共资源交易中心3号开标室 |")
            elif section == "组织单位与联系":
                lines.append("| 单位类型 | 名称 | 联系人 | 电话 | 地址 |")
                lines.append("|----------|------|--------|------|------|")
                lines.extend(contact_rows)
                lines.append("")
                lines.append("项目联系邮箱：zb@example.com。")
            lines.append("")
    elif template_id == "07":
        for index, section in enumerate(sections):
            lines.append(f"## {_cn_section(index)}、{section}")
            lines.append("")
            if section == "评标办法":
                lines.append("本项目采用综合评分法，技术分满分 40 分，商务分 10 分，报价分 50 分。")
            elif section == "商务评分标准":
                lines.append("| 评审因素 | 分值 | 评分标准 |")
                lines.append("|----------|------|----------|")
                lines.append("| 商务响应 | 10 | 商务条款全部响应的得满分 |")
            elif section == "技术评分标准":
                lines.append("| 评审因素 | 分值 | 评分标准（原文） | 证据 |")
                lines.append("|----------|------|------------------|------|")
                lines.append("| 技术方案 | 20 | 方案完整可行：①完整 ②可行 | 技术评分表 |")
                lines.append("| 服务承诺 | 10 | 响应承诺：①2小时 ②4小时 | 技术评分表 |")
                lines.append("| 人员配置 | 10 | 人员数量：①不少于5人 ②不少于3人 | 技术评分表 |")
            elif section == "报价评分标准":
                lines.append("| 评审因素 | 分值 | 评分标准 |")
                lines.append("|----------|------|----------|")
                lines.append("| 投标报价 | 50 | 满足招标文件要求且投标价格最低的得满分 |")
            elif section == "分值合计":
                lines.append("- 技术评分合计：40 分（与招标文件声明的技术分满分一致）")
                lines.append("- 商务评分合计：10 分")
                lines.append("- 报价评分合计：50 分")
                lines.append("- 总分：100 分")
            lines.append("")
    elif template_id == "04":
        for index, section in enumerate(sections):
            lines.append(f"## {_cn_section(index)}、{section}")
            lines.append("")
            if section == "投标文件格式":
                lines.append("投标文件应包含以下内容，按顺序编排：")
                lines.append("")
                lines.append("1. 投标函及投标函附录；")
                lines.append("2. 法定代表人身份证明或授权委托书；")
                lines.append("3. 技术方案（含技术响应表）；")
                lines.append("4. 资格证明文件。")
            else:
                lines.append("### 2.1 排版要求")
                lines.append("")
                lines.append("纸张大小：A4；正文宋体小四，标题黑体四号；行距 1.5 倍；页面右下角标注页码。")
                lines.append("")
                lines.append("### 2.2 封装与签章要求")
                lines.append("")
                lines.append("投标文件分技术、商务、报价三部分单独密封，封口处加盖投标人公章；每页加盖公章。")
            lines.append("")
    elif template_id == "09":
        lines.append("技术服务要求详见采购需求附件：包括系统运行维护、7×24 小时响应、故障处理时限与人员配置要求。")
        lines.append("")
        lines.append("投标人须提供完整的技术服务方案与响应承诺，并附相关证明材料。")
        lines.append("")
        lines.append("服务范围覆盖前端设备、平台、网络与安全设备的日常巡检、故障处理、备品备件管理与运行报告，具体要求以采购需求附件原文为准。")
        lines.append("")
        lines.append("服务团队须配备项目负责人、技术负责人与驻场工程师，并提供人员资质、社保与劳动合同等证明材料。")
    else:
        for index, section in enumerate(sections):
            lines.append(f"## {_cn_section(index)}、{section}")
            lines.append("")
            lines.append("| 序号 | 事项 | 具体要求 |")
            lines.append("|------|------|----------|")
            lines.append(f"| 1 | {section} | 按招标文件要求执行，详见原文条款 |")
            lines.append("")
            lines.append("以上内容按招标文件原文提取，数量、参数与交付要求以采购需求附件为准；未分标段时直接对应项目整体采购内容。")
            lines.append("")
    return "\n".join(lines)


def build_selftest_extractions(project_dir: Path, *, package_count: int = 1) -> None:
    """按真实模板 manifest 生成合法 M3.2 提取样例（供自检/测试，不写生产项目）。"""
    manifest = load_template_manifest()
    by_id = manifest["_by_id"]

    def write_file(template: dict[str, Any], target: Path) -> None:
        sections = expected_sections_for(template, template_path_for(template))
        title = strip_heading_numbering(str(template.get("title") or ""))
        body = _file_content(template, sections)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {title}\n\n{body}\n", encoding="utf-8", newline="\n")

    common_dir = project_dir / "extraction" / "common"
    for template_id in COMMON_TEMPLATE_IDS:
        template = by_id[template_id]
        write_file(template, common_dir / str(template.get("fileName")))
    for number in range(1, package_count + 1):
        package_dir = project_dir / "extraction" / "packages" / f"package-{number}"
        for template_id in PACKAGE_TEMPLATE_IDS:
            template = by_id[template_id]
            write_file(template, package_dir / str(template.get("fileName")))


def build_selftest_project(project_dir: Path, *, package_count: int = 1, project_name: str = "M3.2校验自检项目") -> dict[str, Any]:
    """生成自检用项目骨架（project.json + extraction 样例），返回 project 文档。"""
    now = paths.iso_now()
    project_id = "prj-20261110100000-a1b2"
    bidder_ref = {
        "bidderId": "bdr-20261110100000-a1b2",
        "dirName": "M3.2校验自检科技有限公司-20261110100000-a1b2",
        "displayName": "M3.2校验自检科技有限公司",
        "snapshotAt": now,
    }
    project = schema.initialize_project_document(
        project_id=project_id,
        bidder_ref=bidder_ref,
        workspace_fingerprint="selftest",
        now_iso=now,
        project_name=project_name,
    )
    project["projectNo"] = "TEST-2026-032"
    project["isSubcontracted"] = package_count > 1
    project["packageCount"] = package_count
    project["packagesToWrite"] = [f"package-{index}" for index in range(1, package_count + 1)]
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(project_dir / "project.json", project)
    build_selftest_extractions(project_dir, package_count=package_count)
    return project


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """脚本自检：人类可读 golden 通过；机器表/frontmatter/H1 不符/缺章节/评分表错误/缺文件/包数均 blocker。"""
    import platform
    import shutil
    import tempfile

    checks: list[str] = []
    tmp = tempdir.mkdtemp(prefix="bidgenie-validate-v2-selftest-")
    try:
        project_dir = tmp / "project"
        build_selftest_project(project_dir)
        report = validate_project_extraction(project_dir)["report"]
        if not report["passed"] or report["blockers"]:
            raise AssertionError(f"合法样例未通过：{report['blockers'][:3]}")
        ids = [item["templateId"] for item in report["files"]]
        if report["summary"]["filesTotal"] != 9 or ids != list(COMMON_TEMPLATE_IDS + PACKAGE_TEMPLATE_IDS):
            raise AssertionError(f"应有 9 个文件，实际 {report['summary']} ids={ids}")
        if not report["scoringTable"] or report["scoringTable"]["rows"] != 3:
            raise AssertionError(f"评分表解析不正确：{report.get('scoringTable')}")
        checks.append("golden_pass")
        # P1-6：DEFAULT_EXPECTED_SECTIONS 必须与 manifest v3 对齐，避免模板缺失时按旧口径误判
        manifest = load_template_manifest()
        for template in manifest["templates"]:
            template_id = str(template.get("id") or "")
            expected = tuple(template.get("expectedSections") or ())
            fallback = DEFAULT_EXPECTED_SECTIONS.get(template_id, ())
            if expected != fallback:
                raise AssertionError(f"template {template_id} 默认章节与 manifest 不一致：{fallback} != {expected}")
        # P1-5：「第一部分：交通标线」不能把“部”当编号剥掉
        if strip_heading_numbering("第一部分：交通标线") != "交通标线":
            raise AssertionError(f"第N部分编号剥离不正确：{strip_heading_numbering('第一部分：交通标线')!r}")
        if strip_heading_numbering("第一章 项目概况") != "项目概况" or strip_heading_numbering("1.1 项目背景") != "项目背景":
            raise AssertionError("常规章节编号剥离回归")
        checks.append("default_sections_and_heading_numbering")
        # P1-2/P1-3：回退页/表格/复选框风险汇总与确认标记
        review_dir = tmp / "project-fallback-review"
        (review_dir / "sources").mkdir(parents=True)
        (review_dir / "extraction" / "packages" / "package-1").mkdir(parents=True)
        jsonio.write_json_atomic(
            review_dir / "project.json",
            {"schemaVersion": "1.0", "projectId": "prj-fallback-review", "extensions": {}},
        )
        jsonio.write_json_atomic(
            review_dir / "sources" / "conversion-manifest.json",
            {
                "schemaVersion": "1.0",
                "files": [
                    {
                        "sourceName": "a.pdf",
                        "fallbackPages": [7],
                        "fallbackPageCount": 1,
                        "keyDataFromFallbackOnly": True,
                        "fallbackNote": "关键数据仅来自回退文本：第 7 页由纯文本回退补齐",
                    }
                ],
            },
        )
        (review_dir / "extraction" / "packages" / "package-1" / "08-Business-Requirements.md").write_text(
            "# 商务要求\n\n"
            "## 需用户确认\n\n"
            "- 前附表复选框符号（□√）转换后不可辨，需核对 PDF 原件。\n"
            "- 采购清单跨页合并单元格断行错位，需核对 PDF 原件。\n"
            "- 第一章第 7 页关键字段（项目编号/预算/限价/分包数）来自回退文本，需核对 PDF 原件。\n",
            encoding="utf-8",
        )
        risks = extraction_risk.detect_fallback_review_risks(review_dir)
        risk_codes = {str(item.get("code")) for item in risks}
        if not {"fallback_page", "ambiguous_symbol", "table_structure"} <= risk_codes:
            raise AssertionError(f"回退/勾选风险识别不完整：{risks}")
        fallback_risk = next(item for item in risks if item.get("code") == "fallback_page")
        if int(fallback_risk.get("occurrences") or 0) < 2 or not any(
            str(item).startswith("extraction/") for item in fallback_risk.get("sources") or []
        ):
            raise AssertionError(f"同类型风险未合并 sources/occurrences：{fallback_risk}")
        if len(risks) != len({str(item.get('code')) for item in risks}):
            raise AssertionError(f"同类风险未去重：{[item.get('code') for item in risks]}")
        checks.append("fallback_risk_grouped")
        extraction_risk.write_review(review_dir, risks, confirmed_at="2026-01-01T00:00:00+08:00", note="用户已核对")
        marker = extraction_risk.review_from_project(jsonio.read_json(review_dir / "project.json"))
        if not marker.get("required") or not marker.get("confirmedAt") or len(marker.get("risks") or []) != len(risks):
            raise AssertionError(f"风险确认标记写入不正确：{marker}")
        checks.append("fallback_review_risks")

        # P3-14：coverage.criteriaCount 口径（单包 / 全包合计）+ criteriaByPackage
        coverage = report.get("coverage") or {}
        if (
            coverage.get("criteriaCountScope") != "all"
            or not str(coverage.get("criteriaCountNote") or "")
            or set(coverage.get("criteriaByPackage") or {}) != {"package-1"}
        ):
            raise AssertionError(f"单包 coverage 口径缺失：{coverage}")
        multi_dir = tmp / "project-multi"
        build_selftest_project(multi_dir, package_count=2)
        multi_report = validate_project_extraction(multi_dir)["report"]
        multi_coverage = multi_report.get("coverage") or {}
        if (
            multi_report.get("passed") is not True
            or multi_coverage.get("criteriaCountScope") != "all_packages_total"
            or "全包合计" not in str(multi_coverage.get("criteriaCountNote") or "")
            or multi_coverage.get("criteriaCount") != 6
            or set(multi_coverage.get("criteriaByPackage") or {}) != {"package-1", "package-2"}
            or any(value != 3 for value in (multi_coverage.get("criteriaByPackage") or {}).values())
        ):
            raise AssertionError(f"多包 coverage 全包合计口径不正确：{multi_coverage}")
        single_coverage = (validate_project_extraction(multi_dir, only_package="package-2")["report"].get("coverage") or {})
        if single_coverage.get("criteriaCountScope") != "package-2" or single_coverage.get("criteriaCount") != 3:
            raise AssertionError(f"单包校验 coverage 口径不正确：{single_coverage}")
        checks.append("coverage_criteria_scope")

        # P2-12：caveats 去空条目/去重/按包合并，且不丢真实不同项
        caveat_lines = [
            ("extraction/common/02-Eligibility-Review.md", "需用户确认：条款 A 与条款 B 的表述不一致，需核对原件。"),
            ("extraction/common/05-Substantive-Response.md", "8. 条款 A 与条款 B 的表述不一致，需核对原件（需确认）。"),
            (
                "extraction/packages/package-1/08-Business-Requirements.md",
                "需用户确认：2. **复选框状态不可辨**：表格中的勾选符号转换后丢失，已按语义暂记，需核对原件。",
            ),
            (
                "extraction/packages/package-1/09-Technical-Requirements.md",
                "需用户确认：6. **复选框状态不可辨**：表格中的勾选符号转换后丢失，已按语义暂记，需核对原件。",
            ),
            (
                "extraction/packages/package-1/08-Business-Requirements.md",
                "需用户确认：1. **条款冲突**：同一要求在前后两节表述不同，需核对原件。",
            ),
            ("extraction/packages/package-1/06-Procurement-Content.md", "**需用户确认：**"),
            ("extraction/packages/package-4/09-Technical-Requirements.md", "**需用户确认：** 条款 E 在两处表述不同，需核对原件。"),
            ("extraction/packages/package-5/09-Technical-Requirements.md", "**需用户确认：** 条款 F 在两处表述不同，需核对原件。"),
        ]
        entries = build_caveat_entries(caveat_lines)
        duplicate_items = [item for item in entries if int(item.get("duplicateCount") or 1) > 1]
        if (
            len(entries) != 5
            or [str(item.get("scope")) for item in entries] != ["common", "package-1", "package-1", "package-4", "package-5"]
            or any(not str(item.get("text") or "").strip() for item in entries)
            or len(duplicate_items) != 2
            or not all(len(item.get("sources") or []) == int(item.get("duplicateCount") or 0) for item in entries)
            or "条款 F" not in " ".join(str(item.get("text") or "") for item in entries if item.get("scope") == "package-5")
            or "条款 E" not in " ".join(str(item.get("text") or "") for item in entries if item.get("scope") == "package-4")
        ):
            raise AssertionError(f"caveats 去重/按包合并结果不正确：{entries}")
        if caveats_by_package(entries) != {"common": 1, "package-1": 2, "package-4": 1, "package-5": 1}:
            raise AssertionError(f"caveats 按包统计不正确：{caveats_by_package(entries)}")
        # 端到端：写提取稿 → collect（跳过纯标题、去空条目）→ 写 project.extensions
        demo_dir = tmp / "project-caveats"
        build_selftest_project(demo_dir)
        demo_09 = demo_dir / "extraction" / "packages" / "package-1" / "09-Technical-Requirements.md"
        demo_09.write_text(
            demo_09.read_text(encoding="utf-8")
            + "\n**需用户确认：**\n\n需用户确认：设备数量仅存在于回退文本第 2 页，需与 PDF 原件核对。\n"
            + "\n## 需用户确认\n\n- 采购清单跨页表格需对照 PDF 原件核对。\n",
            encoding="utf-8",
            newline="\n",
        )
        collected = collect_extraction_caveats(demo_dir)
        if (
            not collected
            or any(not str(item.get("text") or "").strip() for item in collected)
            or any("需用户确认" == _caveat_meaningful_text(str(item.get("text") or "")) for item in collected)
            or not all(item.get("packages") and item.get("sources") and "source" in item for item in collected)
            or not any("跨页表格" in str(item.get("text") or "") for item in collected)
        ):
            raise AssertionError(f"collect_extraction_caveats 结果不正确：{collected}")
        written_caveats = write_extraction_caveats(demo_dir, collected)
        saved_caveats = (jsonio.read_json(demo_dir / "project.json").get("extensions") or {}).get("extractionCaveats") or []
        if not written_caveats or len(saved_caveats) != len(collected):
            raise AssertionError(f"extractionCaveats 落盘不正确：{saved_caveats}")
        checks.append("caveats_dedupe")

        target01 = project_dir / "extraction" / "common" / "01-Basic-Information.md"
        original01 = target01.read_text(encoding="utf-8")

        def expect(code: str, label: str, mutate) -> None:
            target01.write_text(mutate(original01), encoding="utf-8", newline="\n")
            bad = validate_project_extraction(project_dir)["report"]
            if not any(item["code"] == code for item in bad["blockers"]):
                target01.write_text(original01, encoding="utf-8", newline="\n")
                raise AssertionError(f"{label} 未判 blocker（codes={[item['code'] for item in bad['blockers']][:6]}）")
            target01.write_text(original01, encoding="utf-8", newline="\n")
            checks.append(label)

        expect(
            "machine_table_forbidden",
            "machine_table_forbidden",
            lambda text: text + "\n| 字段键 | 字段名 | 值 | 证据（来源 · 章节） |\n|---|---|---|---|\n| projectNo | 项目编号 | X | Y |\n",
        )
        expect(
            "frontmatter_forbidden",
            "frontmatter_forbidden",
            lambda text: "---\ntemplateId: \"01\"\ntemplateVersion: \"1.0\"\n---\n\n" + text,
        )
        expect("h1_mismatch", "h1_mismatch", lambda text: text.replace("# 基础信息", "# 错误标题", 1))
        expect(
            "section_missing",
            "section_missing",
            lambda text: text.replace("## 二、资金与限价", "## 二、被删除章节", 1),
        )

        # 07 负例
        target07 = project_dir / "extraction" / "packages" / "package-1" / "07-Evaluation-Criteria.md"
        original07 = target07.read_text(encoding="utf-8")
        target07.write_text(original07.replace("| 评审因素 | 分值 | 评分标准（原文）", "| 评审因素 | 得分 | 评分标准（原文）", 1), encoding="utf-8", newline="\n")
        bad07 = validate_project_extraction(project_dir)["report"]
        if not any(item["code"] == "scoring_columns_missing" for item in bad07["blockers"]):
            raise AssertionError("评分表缺列未判 blocker")
        checks.append("scoring_columns_missing")
        target07.write_text(original07.replace("| 技术方案 | 20 |", "| 技术方案 | 25 |", 1), encoding="utf-8", newline="\n")
        bad07 = validate_project_extraction(project_dir)["report"]
        if not any(item["code"] == "scoring_sum_mismatch" for item in bad07["blockers"]):
            raise AssertionError("评分合计不一致未判 blocker")
        target07.write_text(original07, encoding="utf-8", newline="\n")
        checks.append("scoring_sum_mismatch")
        target07.write_text(
            original07.replace("技术分满分 40 分", "技术分见招标文件附件").replace("技术评分合计：40 分", "技术评分合计：见附件"),
            encoding="utf-8",
            newline="\n",
        )
        bad_total = validate_project_extraction(project_dir)["report"]
        if not any(item["code"] == "scoring_total_missing" for item in bad_total["blockers"]):
            raise AssertionError("评分合计缺失未判 blocker")
        target07.write_text(original07, encoding="utf-8", newline="\n")
        checks.append("scoring_total_missing")
        # N10：07 漏写技术分声明、正文只有「每项满分 4 分」时，不能把 4 当声明满分报
        # 「合计 58 与声明满分 4 差额 +54」；应明确报缺技术分满分声明。
        target07.write_text(
            original07.replace("技术分满分 40 分", "技术分见招标文件附件")
            .replace("技术评分合计：40 分", "技术评分合计：见附件")
            .replace("方案完整可行：①完整 ②可行", "每项满分 4 分：①完整 ②可行"),
            encoding="utf-8",
            newline="\n",
        )
        no_decl = validate_project_extraction(project_dir)["report"]
        no_decl_codes = {item["code"] for item in no_decl["blockers"]}
        if "scoring_total_missing" not in no_decl_codes or "scoring_sum_mismatch" in no_decl_codes:
            raise AssertionError(f"N10 缺技术分声明时诊断不正确：{sorted(no_decl_codes)}")
        checks.append("scoring_declared_total_missing")
        # N10：只有表内合计行、没有技术分声明时，按合计对账并给出 warning，不误报差额
        target07.write_text(
            original07.replace("技术分满分 40 分", "技术分见招标文件附件")
            .replace("技术评分合计：40 分", "技术评分合计：见附件")
            .replace("方案完整可行：①完整 ②可行", "每项满分 4 分：①完整 ②可行")
            .replace(
                "| 人员配置 | 10 | 人员数量：①不少于5人 ②不少于3人 | 技术评分表 |",
                "| 人员配置 | 10 | 人员数量：①不少于5人 ②不少于3人 | 技术评分表 |\n| 合计 | 40 | 与技术总分一致 |",
            ),
            encoding="utf-8",
            newline="\n",
        )
        table_total = validate_project_extraction(project_dir)["report"]
        table_codes = {item["code"] for item in table_total["blockers"]}
        warning_codes = {item["code"] for item in table_total["warnings"]}
        table_scoring = table_total.get("scoringTable") or {}
        if (
            table_codes
            or "scoring_declared_total_missing" not in warning_codes
            or abs(float(table_scoring.get("declaredTotal") or 0) - 40) > 0.01
        ):
            raise AssertionError(f"N10 表内合计对账不正确：blockers={sorted(table_codes)} warnings={sorted(warning_codes)} scoring={table_scoring}")
        target07.write_text(original07, encoding="utf-8", newline="\n")
        checks.append("scoring_total_from_table_only")

        # 缺文件
        missing = project_dir / "extraction" / "common" / "05-Substantive-Response.md"
        backup = missing.read_bytes()
        missing.unlink()
        bad_file = validate_project_extraction(project_dir)["report"]
        if not any(item["code"] == "file_missing" for item in bad_file["blockers"]):
            raise AssertionError("缺文件未判 blocker")
        missing.write_bytes(backup)
        checks.append("file_missing")

        # 包数不一致
        project = jsonio.read_json(project_dir / "project.json")
        project["packageCount"] = 2
        project["packagesToWrite"] = ["package-1", "package-2"]
        jsonio.write_json_atomic(project_dir / "project.json", project)
        bad_package = validate_project_extraction(project_dir)["report"]
        if not any(item["code"] == "package_dir_missing" for item in bad_package["blockers"]):
            raise AssertionError("包数不一致未判 blocker")
        checks.append("package_count_mismatch")

        written = write_reports(project_dir, bad_package)
        if not written or not (project_dir / "extraction" / "common" / REPORT_FILE_NAME).is_file():
            raise AssertionError("报告写盘失败")
        checks.append("report_write")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_extract_validate.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_extract_validate.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（CONTRACT §10.3 + H.1 #7 兼容参数）。"""
    parser = errors.CliParser(
        prog="bg_extract_validate.py",
        description="BidGenie M3.2 九大类提取结构校验（9 文件/H1/title/expectedSections/无机器表/07 评分表）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    parser.add_argument("--scope", choices=["all", "common", "packages"], default="all", help="校验范围（默认 all）")
    parser.add_argument("--package", help="只校验指定 package-<N>")
    parser.add_argument("--strict", action="store_true", help="warning 升级为 blocker")
    parser.add_argument("--confirm-fallback", action="store_true", help="用户已核对回退页/表格/复选框风险后登记确认")
    parser.add_argument("--note", default="", help="--confirm-fallback 的用户确认说明（写 project.extensions）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="校验并写报告（默认）")
    action.add_argument("--report", action="store_true", help="同 --apply（写报告）")
    action.add_argument("--check", action="store_true", help="只读校验，不写报告")
    return parser


#: caveat 识别标记（行内出现任一即视为待确认/转换不确定项）
CAVEAT_MARKERS: tuple[str, ...] = ("转换说明", "需用户确认", "疑似缺失", "无法辨识", "原文缺失", "需确认")
#: 归一化后去掉标记/标点仍不足该字数 → 视为「纯标题行」丢弃（P2-12：`**需用户确认：**` 空条目）
CAVEAT_MIN_MEANINGFUL_CHARS = 3
#: 同包内文本相似度阈值（字符二元组重叠率）：≥ 阈值视为重复（履约担保/备件表/付款方式等）
CAVEAT_SIMILARITY_THRESHOLD = 0.42
#: 去重后 caveat 上限（防止噪声淹没 project.extensions）
CAVEAT_MAX_ITEMS = 30
_CAVEAT_EMPHASIS_RE = re.compile(r"[*_]{1,3}")
_CAVEAT_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\d+[.、．]|[（(]\d+[)）])\s*")
_CAVEAT_PUNCT_RE = re.compile(r"[：:；;，,。.、（）()\[\]【】「」『』《》\s\-—…]+")
_CAVEAT_PACKAGE_RE = re.compile(r"extraction/packages/(package-\d+)/")


def _caveat_scope(source: str) -> str:
    """caveat 所属包：`extraction/packages/package-N/**` → package-N，其余（common）→ common。"""
    match = _CAVEAT_PACKAGE_RE.search(source.replace("\\", "/"))
    return match.group(1) if match else "common"


def _caveat_meaningful_text(text: str) -> str:
    """去掉粗体标记/序号前缀/识别标记/标点后的“有效文字”，用于空条目判定与相似度比较。"""
    stripped = _CAVEAT_EMPHASIS_RE.sub("", text)
    stripped = _CAVEAT_NUMBER_PREFIX_RE.sub("", stripped)
    for marker in CAVEAT_MARKERS:
        stripped = stripped.replace(marker, "")
    return _CAVEAT_PUNCT_RE.sub("", stripped)


def _caveat_bigrams(text: str) -> set[str]:
    normalized = _caveat_meaningful_text(text)
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def _caveat_similarity(left: str, right: str) -> float:
    a, b = _caveat_bigrams(left), _caveat_bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def build_caveat_entries(lines: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """caveats 归一化/去重/按包合并（P2-12）。

    - 跳过纯标题行（归一化后有效文字 < ``CAVEAT_MIN_MEANINGFUL_CHARS``，如 ``**需用户确认：**``）；
    - 完全相同的文本跨包/跨文件合并为一条，保留全部来源（`sources`）与所属包（`packages`）；
    - 同 scope（common / 同一 package）内按文本相似度聚类去重（履约担保、备件表、付款方式等跨文件重复）；
    - 每簇取最长文本为展示文本，保留全部来源（`sources`）与所属包（`packages`），`duplicateCount` 记录合并条数；
    - 不同 scope 的近重复不合并（各包含包内独有条款，避免丢真实不同项），按包顺序合并展示。
    """
    candidates: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for source, raw_text in lines:
        stripped = str(raw_text or "").strip().lstrip("->").strip()
        if not stripped or not any(marker in stripped for marker in CAVEAT_MARKERS):
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            continue
        if len(_caveat_meaningful_text(stripped)) < CAVEAT_MIN_MEANINGFUL_CHARS:
            continue
        normalized = _caveat_meaningful_text(stripped)
        existing = seen.get(normalized)
        if existing is not None:
            # 完全相同文本（跨包/跨文件）：合并来源与包，而不是丢弃
            if source not in existing["sources"]:
                existing["sources"].append(source)
            existing["scopes"].add(_caveat_scope(source))
            continue
        candidate = {
            "text": stripped[:500],
            "source": source,
            "scope": _caveat_scope(source),
            "scopes": {_caveat_scope(source)},
            "sources": [source],
            "order": len(candidates),
        }
        seen[normalized] = candidate
        candidates.append(candidate)
    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        target = None
        for cluster in clusters:
            if cluster["scope"] != candidate["scope"]:
                continue
            if any(_caveat_similarity(candidate["text"], member["text"]) >= CAVEAT_SIMILARITY_THRESHOLD for member in cluster["members"]):
                target = cluster
                break
        if target is None:
            clusters.append({"scope": candidate["scope"], "members": [candidate]})
        else:
            target["members"].append(candidate)
    entries: list[dict[str, Any]] = []
    for cluster in clusters:
        members = list(cluster["members"])
        representative = max(members, key=lambda item: (len(item["text"]), -item["order"]))
        packages: list[str] = []
        sources: list[str] = []
        for member in members:
            for scope in sorted(member.get("scopes") or {member.get("scope") or "common"}):
                if scope not in packages:
                    packages.append(scope)
            for source in member.get("sources") or [member.get("source")]:
                if source and source not in sources:
                    sources.append(str(source))
        packages.sort(key=lambda item: (0 if item == "common" else 1, item))
        entries.append(
            {
                "text": representative["text"],
                "source": representative["source"],
                "packages": packages,
                "sources": sources,
                "duplicateCount": len(sources),
                "scope": packages[0] if packages else str(cluster["scope"]),
                "order": min(int(item["order"]) for item in members),
            }
        )
    entries.sort(key=lambda item: (0 if "common" in (item.get("packages") or []) else 1, item.get("packages") or [""], item["order"]))
    return entries[:CAVEAT_MAX_ITEMS]


def caveats_by_package(caveats: list[dict[str, Any]]) -> dict[str, int]:
    """按包统计 caveat 数量（合并展示口径：common 在前，其后 package-N）。"""
    stats: dict[str, int] = {}
    for item in caveats:
        for package in item.get("packages") or ["common"]:
            stats[str(package)] = stats.get(str(package), 0) + 1
    return stats


def _caveat_source_lines(text: str) -> list[str]:
    """按「需用户确认」章节收集 caveat 行（P2-7）。

    旧实现只认行内标记，章节下的「- 请对照原件复核…」没有「需用户确认」字样就会被跳过；
    这里以 H2/H3 标题为界，把章节内的列表项也纳入，并补上可识别的确认前缀。
    """
    lines: list[str] = []
    in_caveat_section = False
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        heading = re.match(r"^#{2,6}\s+(.+?)\s*$", stripped)
        if heading:
            in_caveat_section = any(marker in heading.group(1) for marker in CAVEAT_MARKERS)
            continue
        if in_caveat_section:
            if not stripped or stripped.startswith("|") or stripped.startswith("```"):
                continue
            if not any(marker in stripped for marker in CAVEAT_MARKERS):
                stripped = "需用户确认：" + re.sub(r"^[-*+]\s*", "", stripped)
            lines.append(stripped)
        else:
            lines.append(raw)
    return lines


def split_resolved_caveats(caveats: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """N-06：把「已由其他章节明确/无需处理」类 caveat 折叠到 resolved，减少 27 条噪声。

    只折叠明确的“已解决/无需”措辞；不确定的一律留在 actionable，避免丢失真实待确认项。
    """
    resolved: list[dict[str, Any]] = []
    actionable: list[dict[str, Any]] = []
    markers = ("已由第一章明确", "已另见第一章", "已由其他章节明确", "已在其他章节明确", "无需处理", "无需提供", "不涉及", "已明确表述")
    for item in caveats:
        text = str(item.get("text") or "")
        if any(marker in text for marker in markers):
            resolved.append({**item, "resolvedByOtherSection": True})
        else:
            actionable.append(item)
    return actionable, resolved


def collect_extraction_caveats(project_dir: Path) -> list[dict[str, Any]]:
    """从提取稿收集 caveats（转换说明/需用户确认/疑似缺失/原文缺失），去重合并后机读写 project.extensions。"""
    extraction_dir = project_dir / "extraction"
    paths_to_scan = sorted(extraction_dir.rglob("*.md")) if extraction_dir.is_dir() else []
    lines: list[tuple[str, str]] = []
    for path in paths_to_scan:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        source = paths.rel_posix(project_dir, path)
        lines.extend((source, line) for line in _caveat_source_lines(text))
    return build_caveat_entries(lines)


def write_extraction_caveats(project_dir: Path, caveats: list[dict[str, Any]]) -> str:
    project_file = project_dir / "project.json"
    if not project_file.is_file():
        return ""
    project = jsonio.read_json(project_file)
    if not isinstance(project, dict):
        return ""
    extensions = dict(project.get("extensions") or {})
    current = extensions.get("extractionCaveats")
    if current == caveats:
        return paths.rel_posix(project_dir, project_file) + ".extensions.extractionCaveats"
    extensions["extractionCaveats"] = caveats
    project["extensions"] = extensions
    project["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(project_file, project)
    return paths.rel_posix(project_dir, project_file) + ".extensions.extractionCaveats"


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    # P1-2/P1-3：把回退页/表格/复选框风险汇总为阻塞式核对项
    risks = extraction_risk.detect_fallback_review_risks(project_dir)
    project_doc = state_lib.load_project(project_dir)
    existing_review = extraction_risk.review_from_project(project_doc)
    confirmed_at = str(existing_review.get("confirmedAt") or "")
    if getattr(args, "confirm_fallback", False):
        confirmed_at = paths.iso_now()
    # F-08：确认动作必须带可追溯结论，不能空 note / “OK” 一句话带过。
    effective_note = str(getattr(args, "note", "") or existing_review.get("note") or "").strip()
    if getattr(args, "confirm_fallback", False) and len(effective_note) < 8:
        raise errors.BgError(
            errors.E_USAGE,
            "确认回退风险时 --note 至少 8 个字符，需写明核对了哪些字段/页码",
            hint='例：--note "已核对第7页回退文本与采购清单跨页表格，截止时间/工期按PDF原件确认"',
            detail={"noteLength": len(effective_note)},
        )
    review_written = ""
    if not getattr(args, "check", False):
        review_written = extraction_risk.write_review(
            project_dir,
            risks,
            confirmed_at=confirmed_at,
            note=effective_note,
        )
    result = validate_project_extraction(
        project_dir,
        scope=str(args.scope),
        only_package=getattr(args, "package", None),
    )
    report = result["report"]
    blockers = list(report.get("blockers") or [])
    warnings = list(report.get("warnings") or [])
    if getattr(args, "strict", False) and warnings:
        for item in warnings:
            blockers.append(
                {
                    "code": "strict_" + str(item.get("code") or "warning"),
                    "file": item.get("file"),
                    "pointer": item.get("pointer", ""),
                    "message": str(item.get("message") or ""),
                }
            )
        warnings = []
    report["fallbackReview"] = {"required": bool(risks), "risks": risks, "confirmedAt": confirmed_at}
    if risks and not confirmed_at:
        blockers.append(
            {
                "code": "fallback_review_required",
                "file": "sources/conversion-manifest.json",
                "pointer": "",
                "message": (
                    f"检测到 {len(risks)} 项需用户核对（回退页关键字段/跨页表格/复选框符号不可辨）；"
                    "请先向用户确认受影响字段，再运行 bg_extract_validate.py --confirm-fallback --note \"<用户确认结论>\""
                ),
            }
        )
    report["blockers"] = blockers
    report["warnings"] = warnings
    report["passed"] = not blockers
    report["ok"] = not blockers
    written: list[str] = []
    if not getattr(args, "check", False):
        written = write_reports(project_dir, report)
        if review_written:
            written.append(review_written)
    summary = report.get("summary") or {}
    if blockers:
        event_log.log_event(
            workspace,
            "extract.failed",
            actor="script",
            detail={"blockerCount": len(blockers), "warningCount": len(warnings)},
            result="error",
        )
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"提取校验失败：{len(blockers)} 项 blocker",
            hint="按 blockers 修复提取文件后重跑；报告见 extraction/common/_extraction-report.json",
            detail={
                "reportPath": paths.rel_posix(project_dir, project_dir / "extraction" / "common" / REPORT_FILE_NAME),
                "blockers": blockers[:50],
                "warnings": warnings[:50],
                "summary": summary,
                # N-08：异常 detail 只回摘要（完整 risks/excerpts 已写 reportPath），
                # 避免 Host script.error 事件写进十几 KB。
                "fallbackReview": {
                    "required": bool((report.get("fallbackReview") or {}).get("required")),
                    "riskCount": len((report.get("fallbackReview") or {}).get("risks") or []),
                    "riskCodes": sorted({str(item.get("code") or "") for item in (report.get("fallbackReview") or {}).get("risks") or []}),
                },
                "written": written,
            },
        )
    caveats_all = collect_extraction_caveats(project_dir)
    caveats, resolved_caveats = split_resolved_caveats(caveats_all)
    caveat_stats = caveats_by_package(caveats)
    caveats_written = ""
    if not getattr(args, "check", False):
        caveats_written = write_extraction_caveats(project_dir, caveats)
    event_log.log_event(
        workspace,
        "extract.validated",
        actor="script",
        detail={
            "files": summary.get("filesTotal"),
            "packages": summary.get("packages"),
            "warningCount": len(warnings),
            "caveatCount": len(caveats),
            "caveatsResolved": len(resolved_caveats),
            "caveatsByPackage": caveat_stats,
        },
    )
    return {
        "ok": True,
        "command": "extract_validate",
        "passed": True,
        "scope": report.get("scope"),
        "validatedAt": report.get("validatedAt"),
        "files": report.get("files"),
        "scoringTable": report.get("scoringTable"),
        "coverage": report.get("coverage"),
        "warnings": warnings,
        "extractionCaveats": caveats,
        "caveatCount": len(caveats),
        "resolvedCaveats": resolved_caveats,
        "resolvedCaveatCount": len(resolved_caveats),
        "caveatsByPackage": caveat_stats,
        "caveatsWritten": caveats_written,
        "fallbackReview": report.get("fallbackReview"),
        "blockers": [],
        "summary": summary,
        "reportPath": paths.rel_posix(project_dir, project_dir / "extraction" / "common" / REPORT_FILE_NAME),
        "written": written,
        "changed": written,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_extract_validate.py",
    )


if __name__ == "__main__":
    sys.exit(main())
