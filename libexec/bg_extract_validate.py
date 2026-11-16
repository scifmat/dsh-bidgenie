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

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib  # noqa: E402

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
        "拟派人员证书证明",
        "书面承诺/声明",
        "各信用平台查询截图",
        "产品/服务厂商授权、第三方检测报告、其它资质证明资料等",
        "其它实质性应标资料",
    ),
    "06": ("标段信息", "采购内容"),
    "07": ("评标办法", "商务评分标准", "技术评分标准", "报价评分标准"),
    "08": ("企业资质要求", "人员资质要求", "业绩要求", "财务要求", "服务要求", "付款方式"),
    "09": (),
}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
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
_SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
_DECLARED_TOTAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"技术(?:评分|部分)?(?:分)?(?:满分|合计)\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?"),
    re.compile(r"技术分满分\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?"),
    re.compile(r"满分\s*[：:]?\s*(\d+(?:\.\d+)?)\s*分?"),
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
    """从「评标办法/分值合计」文本提取技术分声明满分（优先技术评分合计/技术分满分）。"""
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
            _blocker("scoring_total_missing", report_path, "技术评分标准/分值合计", "未找到技术分满分或合计数值，无法对账")
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
    extra = [section for section in found_sections if normalize_title(section) not in expected_normalized]
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
    report_scope = scope if scope in ("common", "packages") else "all"
    if only_package:
        report_scope = only_package
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
        "coverage": {"criteriaCount": criteria_count, "criteriaWithSource": criteria_count},
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
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-validate-v2-selftest-"))
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
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="校验并写报告（默认）")
    action.add_argument("--report", action="store_true", help="同 --apply（写报告）")
    action.add_argument("--check", action="store_true", help="只读校验，不写报告")
    return parser


def collect_extraction_caveats(project_dir: Path) -> list[dict[str, str]]:
    """从提取稿收集 caveats（转换说明/需用户确认/疑似缺失/原文缺失），机读写 project.extensions。"""
    markers = ("转换说明", "需用户确认", "疑似缺失", "无法辨识", "原文缺失", "需确认")
    caveats: list[dict[str, str]] = []
    seen: set[str] = set()
    extraction_dir = project_dir / "extraction"
    paths_to_scan = sorted(extraction_dir.rglob("*.md")) if extraction_dir.is_dir() else []
    for path in paths_to_scan:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        source = paths.rel_posix(project_dir, path)
        for line in text.splitlines():
            stripped = line.strip().lstrip("->").strip()
            if not stripped or not any(marker in stripped for marker in markers):
                continue
            if stripped.startswith("#") or stripped.startswith("|"):
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            caveats.append({"text": stripped[:500], "source": source})
            if len(caveats) >= 30:
                return caveats
    return caveats


def write_extraction_caveats(project_dir: Path, caveats: list[dict[str, str]]) -> str:
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
        report["blockers"] = blockers
        report["warnings"] = warnings
        report["passed"] = not blockers
        report["ok"] = not blockers
    written: list[str] = []
    if not getattr(args, "check", False):
        written = write_reports(project_dir, report)
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
                "written": written,
            },
        )
    caveats = collect_extraction_caveats(project_dir)
    caveats_written = ""
    if not getattr(args, "check", False):
        caveats_written = write_extraction_caveats(project_dir, caveats)
    event_log.log_event(
        workspace,
        "extract.validated",
        actor="script",
        detail={"files": summary.get("filesTotal"), "packages": summary.get("packages"), "warningCount": len(warnings), "caveatCount": len(caveats)},
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
        "caveatsWritten": caveats_written,
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
