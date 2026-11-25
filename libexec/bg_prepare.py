#!/usr/bin/env python3
"""bg_prepare.py：F4 撰写前准备（PRD F4 / 7.3 / 4.3 T3；M4-P1）。

用法：
    python bg_prepare.py --workspace <ws> --project <dir|id> --answers <answers.json> [--package package-<N>] [--json]
    python bg_prepare.py --workspace <ws> --project <dir|id> --check  [--json]   # 只读预检（供 Agent 调 T3 前）
    python bg_prepare.py --workspace <ws> --project <dir|id> --verify [--json]   # 只读校验已生成包（F4.3 必存清单）
    python bg_prepare.py --selftest [--json]

输入：project.json（分包结论/packagesToWrite/bidderRef）、state.json（package_prepare）、extraction/
（common 01–05 + packages/package-<N>/06–09）、user-settings（默认字数/图表配额/联网配图/导出样式）、
answers（7 项提问 + exportStyleProfile 服务默认；缺省按 PRD F4.2 默认值推导）。

输出（幂等）：
    packages/package-<N>-<ts14>/{chapters,chapters/_results,reviews,reviews/rounds,
        resources/{mermaid,network,terminal,tables},export}/
    packages/package-<N>-<ts14>/package-meta.json
      PRD 7.3 瘦字段 + 7 项提问/exportStyleProfile（含 valueSource）；提取内容不复制（材料索引按需 CLI 生成）
      + assumptions（默认值推导说明）+ status（prepared）+ answers 镜像；
    project.json 更新 packagesToWrite/status/updatedAt（extensions.prepare 记录包目录）；
    state.json set-package（不迁移阶段；history 记 kind=event/action=set_package 审计事件）；事件 `package.prepared`；T3 前置 check 结果随输出返回。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib  # noqa: E402

SCHEMA_VERSION = "1.0"
PACKAGE_ID_RE = re.compile(r"^package-(\d+)$")
PACKAGE_DIR_RE = re.compile(r"^package-(\d+)-(\d{14})$")
WORD_COUNT_TIERS = (50000, 100000, 150000)
DEFAULT_FIGURE_QUOTA = {"table": 5, "mermaid": 5, "network": 0, "terminal": 5}
FIGURE_KEYS = ("table", "mermaid", "network", "terminal")
VALUE_SOURCE_VALUES = ("user", "agent-default", "extracted")
REQUIRED_PACKAGE_DIRS = (
    "chapters",
    "chapters/_results",
    "reviews",
    "reviews/rounds",
    "resources/mermaid",
    "resources/terminal",
    "export",
)
ANSI_WORD_RE = re.compile(r"(?:不少于|不低于|至少|不少于)\s*(\d+(?:\.\d+)?)\s*万字")
CN_WORD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万字(?:以上|及以上)?")
CHAR_WORD_RE = re.compile(r"(?:不少于|不低于|至少)\s*(\d+(?:\.\d+)?)\s*字")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: P2-9：合同履行期限（服务期）与施工/供货工期语义分开，避免 constructionPlan 语义错位
SERVICE_PERIOD_LABEL_RE = re.compile(r"合同履行期限|履约期限|服务期限|服务期|履约期|履行期限")
CONSTRUCTION_LABEL_RE = re.compile(r"工期|供货期|交付期|建设期")
#: package-meta 瘦身字段（PRD 7.3；M4.3 只存决策/配置，不复制提取内容）
PRD_META_KEYS: tuple[str, ...] = (
    "schemaVersion",
    "packageId",
    "packageNo",
    "packageName",
    "expectedWordCount",
    "personnelPlan",
    "equipmentPlan",
    "schedulePlan",
    "figureQuota",
    "valueSource",
    "exportStyleProfile",
    "exportPath",
    "createdAt",
    "updatedAt",
    "extensions",
)
#: M4.3 之前误加的字段（--compact 时删除、T3 guard 不再要求）
LEGACY_META_FIELDS: tuple[str, ...] = (
    "mustSaveMaterials",
    "answers",
    "assumptions",
    "personnelPlanSource",
    "equipmentPlanSource",
    "extractedRequirementSummary",
    "status",
)

EXTRACTION_FILES = {
    "01": ("extraction/common/01-Basic-Information.md", "基础信息"),
    "02": ("extraction/common/02-Eligibility-Review.md", "资格审查"),
    "03": ("extraction/common/03-Invalid_Bid-Item.md", "无效投标项"),
    "04": ("extraction/common/04-Compilation-Requirements.md", "投标文件编制要求"),
    "05": ("extraction/common/05-Substantive-Response.md", "实质性应标资料"),
    "06": ("extraction/packages/{package}/06-Procurement-Content.md", "采购内容"),
    "07": ("extraction/packages/{package}/07-Evaluation-Criteria.md", "评审标准"),
    "08": ("extraction/packages/{package}/08-Business-Requirements.md", "商务要求"),
    "09": ("extraction/packages/{package}/09-Technical-Requirements.md", "技术服务要求"),
}


def _slim_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """把 package-meta 规整为 PRD 7.3 瘦字段：删除提取内容副本，assumptions 收进 extensions。"""
    slim: dict[str, Any] = {key: meta[key] for key in PRD_META_KEYS if key in meta}
    old_extensions = meta.get("extensions") if isinstance(meta.get("extensions"), Mapping) else {}
    extensions: dict[str, Any] = {}
    # M4.4：extensions.allowNetworkImages 已废弃，不再写入/保留（联网开关只在 user-settings）
    assumptions = meta.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = old_extensions.get("assumptions")
    if isinstance(assumptions, list) and assumptions:
        extensions["assumptions"] = [str(item)[:500] for item in assumptions[:30]]
    slim["extensions"] = extensions
    return slim


# ---------------------------------------------------------------------------
# 通用读取
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return ""


def _strip_legacy_user_settings(workspace: Path) -> dict[str, Any]:
    """M4.4：compact 时迁移 user-settings，删除已废弃的 userInputTimeoutSec（其他设置不动）。"""
    path = paths.user_settings_path(workspace)
    if not path.is_file():
        return {"changed": False, "removed": [], "path": ""}
    document = jsonio.read_json(path)
    if not isinstance(document, Mapping):
        return {"changed": False, "removed": [], "path": ""}
    if "userInputTimeoutSec" not in document:
        return {"changed": False, "removed": [], "path": paths.rel_posix(workspace, path)}
    updated = dict(document)
    updated.pop("userInputTimeoutSec", None)
    updated["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(path, updated)
    return {
        "changed": True,
        "removed": ["userInputTimeoutSec"],
        "path": paths.rel_posix(workspace, path),
    }


def _read_user_settings(workspace: Path) -> dict[str, Any]:
    """读取 user-settings 并剥离已废弃字段（M4.4：userInputTimeoutSec 不再读写）。"""
    settings = dict(_read_json_optional(paths.user_settings_path(workspace)))
    settings.pop("userInputTimeoutSec", None)
    return settings


def _read_json_optional(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = jsonio.read_json(path)
    except errors.BgError:
        return {}
    return document if isinstance(document, Mapping) else {}


def _split_row(line: str) -> list[str]:
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


def _is_separator(cells: list[str]) -> bool:
    values = [cell for cell in cells if cell.strip()]
    return bool(values) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in values) and any(
        "-" in cell for cell in values
    )


def _section_tables(text: str) -> list[dict[str, Any]]:
    """解析 md 中按 H2 分节的 GFM 表格：``[{section, header, rows}]``。"""
    tables: list[dict[str, Any]] = []
    lines = text.split("\n")
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
        if heading:
            if len(heading.group(1)) == 2:
                section = heading.group(2).strip()
            index += 1
            continue
        if line.strip().startswith("|"):
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index].strip())
                index += 1
            if len(block) >= 2:
                header = _split_row(block[0])
                separator = _split_row(block[1])
                if _is_separator(separator):
                    rows = [_split_row(row) for row in block[2:] if row.strip()]
                    tables.append({"section": section, "header": header, "rows": rows})
            continue
        index += 1
    return tables


def _bullets_under_sections(text: str) -> list[dict[str, str]]:
    """提取 H2 分节下的列表项（用于资格/商务要求兜底）。"""
    items: list[dict[str, str]] = []
    section = ""
    for line in text.split("\n"):
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading:
            section = heading.group(1).strip()
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", stripped)
        if match:
            text_value = match.group(1).strip()
            if len(text_value) >= 4:
                items.append({"section": section, "text": text_value})
    return items


def resolve_packages(project: Mapping[str, Any], *, package_count: int | None = None) -> list[str]:
    """解析要撰写的包列表：优先 project.writingPackages（用户一次性选包），否则 packagesToWrite/分包结论。"""
    raw = project.get("writingPackages")
    if not isinstance(raw, list) or not raw:
        raw = project.get("packagesToWrite")
    packages = [entry for entry in raw if isinstance(entry, str)] if isinstance(raw, list) else []
    if packages:
        return packages
    count = package_count
    if count is None:
        value = project.get("packageCount")
        count = value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1
    return [f"package-{index}" for index in range(1, count + 1)]


# ---------------------------------------------------------------------------
# 提取稿读取与默认值推导
# ---------------------------------------------------------------------------
def load_extraction(project_dir: Path, package_id: str) -> tuple[dict[str, str], list[str]]:
    """读取 9 类提取稿；返回 {key: text} 与缺失文件列表。"""
    texts: dict[str, str] = {}
    missing: list[str] = []
    for key, (rel, _title) in EXTRACTION_FILES.items():
        rel_path = rel.format(package=package_id)
        path = project_dir / rel_path
        if not path.is_file():
            missing.append(rel_path)
            continue
        texts[key] = _read_text(path)
    return texts, missing


def derive_word_count_requirement(texts: Mapping[str, str]) -> int | None:
    """从提取稿推导「不少于 X 万字」等字数下限（PRD F4.2 #1）。"""
    candidates: list[int] = []
    combined = "\n".join(texts.values())
    for match in ANSI_WORD_RE.finditer(combined):
        candidates.append(int(float(match.group(1)) * 10000))
    for match in CN_WORD_RE.finditer(combined):
        candidates.append(int(float(match.group(1)) * 10000))
    for match in CHAR_WORD_RE.finditer(combined):
        value = int(float(match.group(1)))
        if value >= 10000:
            candidates.append(value)
    return max(candidates) if candidates else None


def _pick_label_cell(row: list[str], *, skip: set[str] | None = None) -> tuple[str, str]:
    """从表格行选「人读标签」与「其余内容」（跳过序号/数字列）。"""
    skip = skip or set()
    cells = [cell.strip() for cell in row if cell and cell.strip()]
    for cell in cells:
        if cell in skip:
            continue
        if re.fullmatch(r"[\d\s.、()（）\-—/]+", cell):
            continue
        if len(cell) <= 1:
            continue
        detail = " / ".join(
            other for other in cells if other != cell and not re.fullmatch(r"[\d.\s%]+", other)
        )
        return cell, detail
    return (cells[0] if cells else ""), " / ".join(cells[1:])


def _column_index(header: Sequence[str], tokens: Sequence[str]) -> int:
    for index, name in enumerate(header):
        normalized = re.sub(r"\s+", "", str(name))
        if any(token in normalized for token in tokens):
            return index
    return -1


def _cell(row: Sequence[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _row_is_header(row: Sequence[str], header: Sequence[str]) -> bool:
    cells = [str(cell or "").strip() for cell in row]
    header_cells = {str(cell or "").strip() for cell in header}
    return bool(cells) and all(cell in header_cells for cell in cells if cell)


def derive_personnel_plan(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """从 08/09 人员相关表格按列名推导 personnelPlan（跳过表头/合计，避免「人数 1 人/是」）。"""
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, text in texts.items():
        if key not in ("08", "09"):
            continue
        for table in _section_tables(text):
            header = table["header"]
            header_text = " ".join(header)
            if not any(token in header_text for token in ("人员", "岗位", "角色", "团队", "人员类别")):
                if not any(token in table["section"] for token in ("人员", "团队", "岗位")):
                    continue
            role_index = _column_index(header, ("岗位", "角色", "人员类别", "职务", "职责"))
            count_index = _column_index(header, ("人数", "数量", "编制", "配置人数"))
            note_index = _column_index(header, ("要求", "备注", "说明", "资格"))
            for row in table["rows"]:
                if not row or _row_is_header(row, header):
                    continue
                if role_index >= 0:
                    role = _cell(row, role_index)
                else:
                    role, _detail = _pick_label_cell(row, skip={"序号", "岗位", "人员类别", "角色", "人数", "数量"})
                role = re.sub(r"\s+", " ", role).strip()
                if not role or role in seen or len(role) > 40 or role in ("合计", "小计"):
                    continue
                if re.fullmatch(r"[\d.\s%人]+", role):
                    continue
                count = 0
                if count_index >= 0:
                    match = re.search(r"(\d+)", _cell(row, count_index))
                    count = int(match.group(1)) if match else 0
                if count <= 0:
                    detail_text = " ".join(str(cell) for cell in row[1:])
                    match = re.search(r"(\d+)\s*人", detail_text)
                    count = int(match.group(1)) if match else 1
                note = _cell(row, note_index)
                if not note:
                    note = " / ".join(
                        str(cell).strip()
                        for index, cell in enumerate(row)
                        if str(cell).strip() and index not in {0, role_index, count_index}
                    )
                seen.add(role)
                plan.append({"role": role, "count": count, "note": note[:160]})
                if len(plan) >= 12:
                    return plan
    return plan


def derive_personnel_total(texts: Mapping[str, str], *, multi_package: bool = False) -> int:
    """从 08/09 人员表的「合计/小计」行取总人数（只作汇总镜像，不当作岗位条目）。

    P2-8/C06：表头/合计行不作为岗位计入 personnelPlan，但「招标文件声明的合计人数」
    是投标人承诺的总量口径，应在 auto 描述里以「，合计N人」镜像出来（整行原文优先）。
    """
    labels = ("合计", "小计", "总计", "总人数", "合 计")
    for key in ("09", "08"):
        text = texts.get(key, "")
        for table in _section_tables(text):
            header = table["header"]
            header_text = " ".join(header)
            if not any(token in header_text for token in ("人员", "岗位", "角色", "团队", "人员类别")):
                if not any(token in table["section"] for token in ("人员", "团队", "岗位")):
                    continue
            role_index = _column_index(header, ("岗位", "角色", "人员类别", "职务", "职责"))
            count_index = _column_index(header, ("人数", "数量", "编制", "配置人数"))
            for row in table["rows"]:
                if not row or _row_is_header(row, header):
                    continue
                row_text = " ".join(str(cell) for cell in row)
                if multi_package and CROSS_PACKAGE_RE.search(row_text):
                    continue
                if role_index >= 0:
                    label = _cell(row, role_index)
                else:
                    label, _detail = _pick_label_cell(row, skip={"序号", "岗位", "人员类别", "角色", "人数", "数量"})
                if re.sub(r"\s+", "", label) not in labels:
                    continue
                if count_index >= 0:
                    match = re.search(r"(\d+)", _cell(row, count_index))
                else:
                    match = re.search(r"(\d+)\s*(?:人|名)", row_text)
                if match and int(match.group(1)) > 0:
                    return int(match.group(1))
    return 0


def derive_equipment_plan(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """从 08/09 的拟投入设备/车辆/备件表按列名推导；数量取数量列，单位列不写入 purpose。"""
    combined = texts.get("08", "") + "\n" + texts.get("09", "")
    if not re.search(r"拟投入|投入设备|自有设备|施工机具|作业车|升降车|高空作业|巡检车|专用车辆|备品备件|备件", combined):
        return []
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("08", "09"):
        text = texts.get(key, "")
        for table in _section_tables(text):
            header = table["header"]
            header_text = " ".join(header)
            if not any(token in header_text for token in ("设备", "车辆", "工具", "机具", "名称", "备件")):
                continue
            if any(token in table["section"] for token in ("采购清单", "采购内容", "评分")):
                continue
            section_text = table["section"]
            if not any(token in section_text for token in ("车辆", "备件", "设备", "机具", "工具", "投入")) and not any(
                token in header_text for token in ("车辆类别", "车辆名称", "设备名称", "备品备件")
            ):
                continue
            if any(token in section_text for token in ("据实", "单价", "采购", "评分", "技术", "服务内容")):
                continue
            name_index = _column_index(header, ("设备名称", "车辆类别", "车辆名称", "设备", "名称"))
            count_index = _column_index(header, ("数量", "最低数量", "台数", "配置数量"))
            unit_index = _column_index(header, ("单位",))
            purpose_index = _column_index(header, ("用途", "备注", "要求", "说明"))
            for row in table["rows"]:
                if not row or _row_is_header(row, header):
                    continue
                if name_index >= 0:
                    name = _cell(row, name_index)
                else:
                    name, _detail = _pick_label_cell(row, skip={"序号", "名称", "设备名称", "车辆名称", "单位"})
                name = re.sub(r"\s+", " ", name).strip()
                if not name or name in seen or len(name) > 50 or name in ("合计", "小计"):
                    continue
                if re.fullmatch(r"[\d.\s%台套个辆项]+", name):
                    continue
                count = 0
                if count_index >= 0:
                    match = re.search(r"(\d+)", _cell(row, count_index))
                    count = int(match.group(1)) if match else 0
                if count <= 0:
                    match = re.search(r"(\d+)\s*(?:台|套|个|辆|项)", " ".join(str(cell) for cell in row))
                    count = int(match.group(1)) if match else 1
                unit = _cell(row, unit_index) if unit_index >= 0 else ""
                purpose = _cell(row, purpose_index)
                if not purpose and unit:
                    purpose = ""
                seen.add(name)
                plan.append({"name": name, "count": count, **({"unit": unit} if unit else {}), "purpose": purpose[:160]})
                if len(plan) >= 12:
                    return plan
    return plan


def _clean_period_candidates(candidates: Sequence[str]) -> str:
    """清洗期限候选（去前缀/引号/尾标点、限长、去重），返回首个合法值。"""
    cleaned: list[str] = []
    for candidate in candidates:
        value = re.sub(r"^[\s:：*·\-]+", "", str(candidate or ""))
        value = re.sub(r"[「」『』*`]", "", value).strip(" ；;，,。、/")
        if not value or len(value) > 60:
            continue
        if not re.search(r"\d+\s*(?:个)?\s*(?:日历天|日历日|工作日|天|年|个月|月|日|周)", value):
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned[0] if cleaned else ""


def derive_schedule_plan(texts: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """从 01/08/09 期限字段推导计划；不编造具体日期。

    P2-9 语义拆分：
    - 合同履行期限/履约期限/服务期 → ``schedulePlan.servicePeriod``（服务期）；
    - 工期/供货期/交付期 → ``schedulePlan.constructionPlan``（建设/供货计划）；
    两者都不编造：未推导到就留空字符串，由调用方按 agent-default 记录依据。
    """
    service_candidates: list[str] = []
    construction_candidates: list[str] = []
    for key in ("01", "08", "09"):
        text = texts.get(key, "")
        for table in _section_tables(text):
            for row in table["rows"]:
                if len(row) < 2:
                    continue
                label = str(row[0]).strip()
                value = str(row[1]).strip()
                if not value:
                    continue
                if SERVICE_PERIOD_LABEL_RE.search(label):
                    service_candidates.append(value)
                elif CONSTRUCTION_LABEL_RE.search(label):
                    construction_candidates.append(value)
        for line in text.split("\n"):
            if "|" in line:
                continue
            service_match = re.search(r"(?:合同履行期限|履约期限|服务期限|服务期|履约期|履行期限)[：:]\s*([^\n]{2,60})", line)
            if service_match:
                service_candidates.append(service_match.group(1).strip())
            construction_match = re.search(r"(?:工期|供货期|交付期|建设期)[：:]\s*([^\n]{2,60})", line)
            if construction_match:
                construction_candidates.append(construction_match.group(1).strip())
    service_period = _clean_period_candidates(service_candidates)
    construction_plan = _clean_period_candidates(construction_candidates)
    derived = bool(service_period or construction_plan)
    return {
        "startDate": "",
        "deliveryDate": "",
        "servicePeriod": service_period,
        "constructionPlan": construction_plan,
    }, derived


def derive_key_chapters(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """从 07 评分表推导重点章节/评分点（按分值降序）。"""
    items: list[dict[str, Any]] = []
    text = texts.get("07", "")
    for table in _section_tables(text):
        header = [cell.strip() for cell in table["header"]]
        if not any("评审因素" in cell for cell in header) or not any("分值" in cell for cell in header):
            continue
        factor_index = next(index for index, cell in enumerate(header) if "评审因素" in cell)
        score_index = next(index for index, cell in enumerate(header) if "分值" in cell)
        for row in table["rows"]:
            factor = row[factor_index].strip() if factor_index < len(row) else ""
            if not factor or "合计" in factor:
                continue
            try:
                score = float(re.search(r"\d+(?:\.\d+)?", row[score_index]).group(0)) if score_index < len(row) else 0.0
            except (AttributeError, ValueError):
                score = 0.0
            items.append({"criterion": factor, "score": score, "section": table["section"]})
    items.sort(key=lambda item: item["score"], reverse=True)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        if item["criterion"] in seen:
            continue
        seen.add(item["criterion"])
        result.append(item)
    return result


def build_must_save_materials(project_dir: Path, package_id: str, texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """生成必存清单：评分点（07）/资格要求（02）/商务要求（08）/实质性资料（05），均带来源。"""
    materials: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(material_type: str, title: str, detail: str, source_rel: str, section: str, score: float | None = None) -> None:
        clean_title = re.sub(r"\s+", " ", title).strip()[:120]
        clean_detail = re.sub(r"\s+", " ", detail).strip()[:300]
        if not clean_title or clean_title in ("序号", "评审因素", "合计", "项", "事项"):
            return
        if re.fullmatch(r"[\d\s.、()（）\-/]+", clean_title):
            return
        key = (material_type, clean_title, clean_detail[:80])
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {
            "id": f"MS-{len(materials) + 1:03d}",
            "type": material_type,
            "title": clean_title,
            "detail": clean_detail,
            "source": source_rel,
            "section": section,
        }
        if score is not None:
            item["score"] = score
        materials.append(item)

    def scan_tables(key: str, material_type: str, *, score_column: bool = False) -> None:
        text = texts.get(key, "")
        source_rel = EXTRACTION_FILES[key][0].format(package=package_id)
        for table in _section_tables(text):
            header = [cell.strip() for cell in table["header"]]
            if not header:
                continue
            if score_column:
                if not any("评审因素" in cell for cell in header) or not any("分值" in cell for cell in header):
                    continue
            factor_index = next((index for index, cell in enumerate(header) if "评审因素" in cell), None)
            score_index = next((index for index, cell in enumerate(header) if "分值" in cell), None)
            for row in table["rows"]:
                if not row or not any(cell.strip() for cell in row):
                    continue
                if factor_index is not None and factor_index < len(row) and row[factor_index].strip():
                    title = row[factor_index].strip()
                    detail = " / ".join(
                        cell for cell in row if cell and cell != title and not re.fullmatch(r"[\d.\s%]+", cell)
                    )
                else:
                    title, detail = _pick_label_cell(row, skip={"序号", "号", "项"})
                score: float | None = None
                if score_column and score_index is not None and score_index < len(row):
                    match = re.search(r"\d+(?:\.\d+)?", row[score_index])
                    if match:
                        score = float(match.group(0))
                add(material_type, title, detail, source_rel, table["section"], score)

    scan_tables("07", "scoring_criterion", score_column=True)
    for key, material_type in (("02", "qualification"), ("08", "business_requirement"), ("05", "substantive_material")):
        text = texts.get(key, "")
        source_rel = EXTRACTION_FILES[key][0].format(package=package_id)
        scan_tables(key, material_type)
        for bullet in _bullets_under_sections(text):
            add(material_type, bullet["text"], "", source_rel, bullet["section"])
    return materials


def load_bidder_summary(workspace: Path, project: Mapping[str, Any]) -> dict[str, Any]:
    """读取当前投标人摘要：资质证书/业绩/简介（用于素材与优势默认值）。"""
    ref = project.get("bidderRef") if isinstance(project.get("bidderRef"), Mapping) else {}
    dir_name = str(ref.get("dirName") or "")
    bidder_dir = paths.bidders_dir(workspace) / dir_name if dir_name else Path()
    summary: dict[str, Any] = {"dir": str(bidder_dir) if dir_name else "", "certificates": [], "performance": [], "profile": "", "bidder": {}}
    if not dir_name or not bidder_dir.is_dir():
        return summary
    bidder = _read_json_optional(bidder_dir / "bidder.json")
    summary["bidder"] = dict(bidder)
    certificates = _read_json_optional(bidder_dir / "certificates.json")
    for item in certificates.get("items") or []:
        if isinstance(item, Mapping) and item.get("name"):
            summary["certificates"].append(
                {"name": str(item.get("name")), "issuer": str(item.get("issuer") or ""), "validUntil": str(item.get("validUntil") or "")}
            )
    performance = _read_json_optional(bidder_dir / "performance.json")
    for item in performance.get("items") or []:
        if isinstance(item, Mapping) and item.get("projectName"):
            summary["performance"].append(
                {
                    "name": str(item.get("projectName")),
                    "client": str(item.get("client") or ""),
                    "amount": item.get("amount"),
                    "year": str(item.get("year") or ""),
                }
            )
    profile_path = bidder_dir / "profile.md"
    if profile_path.is_file():
        summary["profile"] = _read_text(profile_path)[:2000]
    return summary


def derive_materials_and_advantages(summary: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """从投标人资料推导可用素材/案例与优势（无数据返回空，不编造）。"""
    materials: list[dict[str, Any]] = []
    advantages: list[str] = []
    for item in summary.get("performance") or []:
        materials.append({"type": "case", "name": item["name"], "source": "performance.json", "note": item.get("client") or ""})
        if item.get("client"):
            advantages.append(f"具备{item.get('client')}类似项目业绩（{item['name']}）")
    for item in summary.get("certificates") or []:
        materials.append({"type": "certificate", "name": item["name"], "source": "certificates.json", "note": item.get("issuer") or ""})
        advantages.append(f"具备{item['name']}资质/证书")
    bidder = summary.get("bidder") or {}
    business_scope = str(bidder.get("businessScope") or "").strip()
    if business_scope:
        advantages.append(f"经营范围覆盖：{business_scope[:80]}")
    # 去重 + 限制条数
    unique_advantages: list[str] = []
    for text in advantages:
        if text not in unique_advantages:
            unique_advantages.append(text)
    return materials[:30], unique_advantages[:15]


# ---------------------------------------------------------------------------
# answers 解析/校验
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    "wordCount": "expectedWordCount",
    "word_count": "expectedWordCount",
    "expected_word_count": "expectedWordCount",
    "personnel": "personnelPlan",
    "personnel_plan": "personnelPlan",
    "equipment": "equipmentPlan",
    "equipment_plan": "equipmentPlan",
    "schedule": "schedulePlan",
    "schedule_plan": "schedulePlan",
    "quota": "figureQuota",
    "figure_quota": "figureQuota",
    "figures": "figureQuota",
    "exportStyle": "exportStyleProfile",
    "export_style_profile": "exportStyleProfile",
    "styleProfile": "exportStyleProfile",
}


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field} 必须是整数", detail={"field": field, "value": value})
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"{field} 必须 {'>=' if allow_zero else '>'} {minimum}",
            detail={"field": field, "value": value},
        )
    return value


def normalize_answers(raw: Any, *, allow_network_images: bool) -> dict[str, Any]:
    """规范化 8 项 answers（支持别名/部分回答）；返回 ``{field: value}``（仅用户提供项）。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise errors.BgError(errors.E_USAGE, "answers 必须是 JSON 对象", hint='形如 {"expectedWordCount": 50000, ...}')
    if isinstance(raw.get("answers"), Mapping):
        raw = raw["answers"]
    provided: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = ALIASES.get(str(key), str(key))
        if canonical in (
            "expectedWordCount",
            "personnelPlan",
            "equipmentPlan",
            "schedulePlan",
            "figureQuota",
            "exportStyleProfile",
            "valueSource",
            "allowNetworkImages",
        ):
            provided[canonical] = value
        elif str(key) in FIGURE_KEYS:
            provided.setdefault("figureQuota", {})[str(key)] = value
        elif str(key) in ("tableQuota", "mermaidQuota", "networkQuota", "terminalQuota"):
            provided.setdefault("figureQuota", {})[str(key).replace("Quota", "")] = value
        # 其他未知键忽略（不阻塞 Agent 扩展）
    result: dict[str, Any] = {}
    if "expectedWordCount" in provided:
        result["expectedWordCount"] = _positive_int(provided["expectedWordCount"], "expectedWordCount")
    for field in ("personnelPlan", "equipmentPlan"):
        if field not in provided:
            continue
        result[field] = _plan_value(provided[field], field)
    if "schedulePlan" in provided:
        value = provided["schedulePlan"]
        if not isinstance(value, Mapping):
            raise errors.BgError(errors.E_SCHEMA_INVALID, "schedulePlan 必须是对象", detail={"field": "schedulePlan"})
        schedule = {
            "startDate": str(value.get("startDate") or ""),
            "deliveryDate": str(value.get("deliveryDate") or ""),
            "constructionPlan": str(value.get("constructionPlan") or "")[:300],
            "servicePeriod": str(value.get("servicePeriod") or "")[:300],
        }
        for key in ("startDate", "deliveryDate"):
            if schedule[key] and not DATE_RE.match(schedule[key]):
                raise errors.BgError(
                    errors.E_SCHEMA_INVALID,
                    f"schedulePlan.{key} 必须是 YYYY-MM-DD 或空字符串",
                    detail={"field": f"schedulePlan.{key}", "value": schedule[key]},
                )
        result["schedulePlan"] = schedule
    if "figureQuota" in provided:
        value = provided["figureQuota"]
        if not isinstance(value, Mapping):
            raise errors.BgError(errors.E_SCHEMA_INVALID, "figureQuota 必须是对象", detail={"field": "figureQuota"})
        quota: dict[str, int] = {}
        for key in FIGURE_KEYS:
            if key in value and value[key] is not None:
                quota[key] = _positive_int(value[key], f"figureQuota.{key}", allow_zero=True)
        result["figureQuota"] = quota
    if "allowNetworkImages" in provided:
        if not isinstance(provided["allowNetworkImages"], bool):
            raise errors.BgError(errors.E_SCHEMA_INVALID, "allowNetworkImages 必须是布尔值", detail={"value": provided["allowNetworkImages"]})
        result["allowNetworkImages"] = bool(provided["allowNetworkImages"])
    if "exportStyleProfile" in provided:
        result["exportStyleProfile"] = str(provided["exportStyleProfile"] or "").strip()
    if "valueSource" in provided:
        value = provided["valueSource"]
        if not isinstance(value, Mapping):
            raise errors.BgError(errors.E_SCHEMA_INVALID, "valueSource 必须是对象", detail={"field": "valueSource"})
        sources: dict[str, str] = {}
        for key, source in value.items():
            if str(key) in ("expectedWordCount", "personnelPlan", "equipmentPlan", "schedulePlan") + FIGURE_KEYS:
                if str(source) not in VALUE_SOURCE_VALUES:
                    raise errors.BgError(errors.E_SCHEMA_INVALID, f"valueSource.{key} 非法", detail={"value": source})
                sources[str(key)] = str(source)
        result["valueSource"] = sources
    return result


# ---------------------------------------------------------------------------
# package-meta 构建
# ---------------------------------------------------------------------------
def _plan_value(value: Any, field: str) -> Any:
    """人员/设备计划：允许用户原话字符串或数组（M4.1）；字符串直接原样保存。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field} 不能为空字符串", detail={"field": field})
        if len(text) > 2000:
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field} 原话过长（>2000 字）", detail={"field": field})
        return text
    if not isinstance(value, list):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"{field} 必须是字符串（用户原话）或数组（结构化）",
            detail={"field": field, "type": type(value).__name__},
        )
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        if isinstance(entry, str):
            entry = {"role": entry, "count": 1, "note": ""} if field == "personnelPlan" else {"name": entry, "count": 1, "purpose": ""}
        if not isinstance(entry, Mapping):
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field}[{index}] 必须是对象或字符串", detail={"field": field})
        name_key = "role" if field == "personnelPlan" else "name"
        name = str(entry.get(name_key) or "").strip()
        if not name:
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field}[{index}].{name_key} 不能为空", detail={"field": field})
        count = _positive_int(entry.get("count", 0), f"{field}[{index}].count", allow_zero=True)
        if field == "personnelPlan":
            entries.append({"role": name, "count": count, "note": str(entry.get("note") or "")[:200]})
        else:
            entries.append({"name": name, "count": count, "purpose": str(entry.get("purpose") or "")[:200]})
    return entries


def _requirement_source(keys: tuple[str, ...], package_id: str) -> list[str]:
    return [EXTRACTION_FILES[key][0].format(package=package_id) for key in keys]


def build_requirement_summary(texts: Mapping[str, str], package_id: str) -> dict[str, Any]:
    """提取到的人员/设备/工期等要求摘要（供 Agent 展示给用户后收集用户原话计划）。"""
    personnel: list[str] = []
    equipment: list[str] = []
    personnel_keys = ("05", "08", "09")
    equipment_keys = ("06", "07", "08", "09")
    for key in personnel_keys:
        text = texts.get(key, "")
        for table in _section_tables(text):
            header_text = " ".join(table["header"]) + " " + table["section"]
            if not any(token in header_text for token in ("人员", "团队", "岗位", "职称", "社保", "证书")):
                continue
            for row in table["rows"]:
                if not row:
                    continue
                label, _detail = _pick_label_cell(row, skip={"序号", "岗位", "人员类别", "角色"})
                if not label:
                    continue
                detail = " / ".join(cell for cell in row if cell and cell != label)
                item = f"{label}：{detail}" if detail else label
                if not any(
                    token in item
                    for token in ("人员", "团队", "岗位", "职称", "社保", "项目经理", "技术负责人", "驻场", "工程师", "安全员", "负责人")
                ):
                    continue
                if item not in personnel:
                    personnel.append(item[:240])
        for bullet in _bullets_under_sections(text):
            if any(token in bullet["section"] + bullet["text"] for token in ("人员", "团队", "职称", "社保")):
                item = f"{bullet['section']}：{bullet['text']}" if bullet["section"] else bullet["text"]
                if item not in personnel:
                    personnel.append(item[:240])
    for key in equipment_keys:
        text = texts.get(key, "")
        for table in _section_tables(text):
            header_text = " ".join(table["header"]) + " " + table["section"]
            if not any(token in header_text for token in ("设备", "车辆", "工具", "机具", "作业车", "升降车", "终端", "名称")):
                continue
            for row in table["rows"]:
                if not row:
                    continue
                label, _detail = _pick_label_cell(row, skip={"序号", "名称", "设备名称", "车辆名称"})
                if not label:
                    continue
                detail = " / ".join(cell for cell in row if cell and cell != label)
                item = f"{label}：{detail}" if detail else label
                if any(token in item for token in ("设备", "车辆", "工具", "机具", "作业车", "升降车", "终端")) and item not in equipment:
                    equipment.append(item[:240])
        for bullet in _bullets_under_sections(text):
            if any(token in bullet["text"] for token in ("设备", "车辆", "作业车", "升降车", "机具", "工具")):
                item = f"{bullet['section']}：{bullet['text']}" if bullet["section"] else bullet["text"]
                if item not in equipment:
                    equipment.append(item[:240])
    schedule_plan, schedule_derived = derive_schedule_plan(texts)
    word_requirement = derive_word_count_requirement(texts)
    schedule_kind = "servicePeriod" if schedule_plan.get("servicePeriod") else ("constructionPlan" if schedule_plan.get("constructionPlan") else "")
    return {
        "personnel": personnel[:12],
        "equipment": equipment[:12],
        "schedule": (schedule_plan.get("servicePeriod") or schedule_plan.get("constructionPlan") or "") if schedule_derived else "",
        "scheduleKind": schedule_kind,
        "wordCount": word_requirement,
        "sources": {
            "personnel": _requirement_source(personnel_keys, package_id),
            "equipment": _requirement_source(equipment_keys, package_id),
        },
        "note": "以下为提取稿中的相关要求摘要，仅供用户回答「计划投入的人员/设备」时参考；本阶段只记录用户偏好/计划，不讨论正文写法。",
    }


AUTO_BAD_WORDS = ("评分", "得分", "证明材料", "复印件", "证书编号", "身份证", "社保", "姓名", "车牌", "账号")
AUTO_UNITS = ("人", "名", "台", "套", "辆", "个", "项", "部")
CROSS_PACKAGE_RE = re.compile(r"第\s*\d+\s*包|package-\d+")


def _auto_clean(text: str, *, limit: int = 80) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" ；;，,。、/：:")
    value = re.sub(r"\b\d{17}[\dXx]\b", "", value)
    value = re.sub(r"\b1[3-9]\d{9}\b", "", value)
    value = re.sub(r"\b\d{16,19}\b", "", value)
    return value[:limit].strip()


def _auto_is_business_material(text: str) -> bool:
    return any(token in text for token in AUTO_BAD_WORDS)


def _auto_equipment_requirements(texts: Mapping[str, str], *, multi_package: bool = False) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """只从「拟投入/配置设备」语境提取拟投入设备；排除采购清单/维护任务/评分与材料明细。"""
    explicit: list[dict[str, Any]] = []
    roles: list[str] = []
    sources: list[str] = []
    person_tokens = ("班组", "人员", "团队", "岗位", "队伍", "小组", "工程师", "项目经理", "负责人")
    unit_re = r"(台|套|辆|个|项|部|块)"
    keys = ("06", "07", "08", "09") if multi_package else ("05", "06", "07", "08", "09")
    for key in keys:
        text = texts.get(key, "")
        if not text:
            continue
        found = False
        for table in _section_tables(text):
            context = table.get("section", "") + " " + " ".join(table.get("header") or [])
            if not re.search(r"拟投入|投入设备|设备配置|车辆配置|机具配置|自有设备", context):
                continue
            if re.search(r"采购清单|采购内容|维护|保养|巡检|评分|材料|证明|证书", context):
                continue
            for row in table.get("rows") or []:
                if not row:
                    continue
                row_text = " ".join(str(cell) for cell in row)
                if CROSS_PACKAGE_RE.search(row_text):
                    continue
                label, detail = _pick_label_cell(row, skip={"序号", "名称", "设备名称", "车辆名称", "数量"})
                name = _auto_clean(re.sub(r"[（(]\s*\d+\s*" + unit_re + r"\s*[)）]", "", label), limit=40)
                if not name or _auto_is_business_material(name + " " + detail):
                    continue
                if any(token in name for token in person_tokens):
                    continue
                match = re.search(r"(\d+)\s*" + unit_re, name + " " + detail)
                if match:
                    explicit.append({"name": name, "count": int(match.group(1)), "unit": match.group(2)})
                    found = True
                else:
                    roles.append(name)
                    found = True
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or "|" in stripped or _auto_is_business_material(stripped):
                continue
            if CROSS_PACKAGE_RE.search(stripped):
                continue
            if not re.search(r"拟投入|投入|配备|配置|自有", stripped):
                continue
            if not re.search(r"设备|车辆|作业车|升降车|机具|工具|终端|工程车|巡检车", stripped):
                continue
            pairs = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,24}?)\s*(\d+)\s*" + unit_re, stripped)
            if pairs:
                for name, count, unit in pairs:
                    clean = _auto_clean(re.sub(r"^(?:拟投入|投入|配备|配置|自有|提供|计划)", "", name))
                    if clean and not _auto_is_business_material(clean) and not any(token in clean for token in person_tokens):
                        explicit.append({"name": clean, "count": int(count), "unit": unit})
                        found = True
            else:
                match = re.search(r"(?:拟投入|配备|配置)([^。；;，,]{2,60})", stripped)
                if match:
                    phrase = _auto_clean(match.group(1), limit=60)
                    for part in re.split(r"[、和及与]", phrase):
                        part = _auto_clean(re.sub(r"等.*$", "", part), limit=24)
                        if part and not _auto_is_business_material(part) and not any(token in part for token in person_tokens):
                            roles.append(part)
                            found = True
        if found:
            sources.append(f"{key} {EXTRACTION_FILES[key][1]}")
    # 去重
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in explicit:
        key_tuple = (item["name"], item["count"], item["unit"])
        if key_tuple not in seen:
            seen.add(key_tuple)
            deduped.append(item)
    roles = list(dict.fromkeys(role for role in roles if role))
    return deduped, roles, sources


def _auto_plan_description(
    texts: Mapping[str, str],
    *,
    field: str,
    package_id: str,
    bidder_summary: Mapping[str, Any],
    multi_package: bool = False,
) -> dict[str, Any]:
    """三档生成人员/设备合规描述：显式数量镜像 / 能力最低表述 / 无要求履约承诺。"""
    summary_texts = {key: texts[key] for key in ("06", "07", "08", "09") if key in texts} if multi_package else texts
    summary = build_requirement_summary(summary_texts, package_id)
    source_texts = summary.get("personnel" if field == "personnelPlan" else "equipment") or []
    if multi_package:
        source_texts = [item for item in source_texts if not CROSS_PACKAGE_RE.search(item)]
    sources = (summary.get("sources") or {}).get("personnel" if field == "personnelPlan" else "equipment") or []
    explicit: list[dict[str, Any]] = []
    roles: list[str] = []
    if field == "personnelPlan":
        for entry in derive_personnel_plan(texts):
            role = _auto_clean(entry.get("role") or "")
            note_text = str(entry.get("note") or "")
            if not role or _auto_is_business_material(role + " " + note_text):
                continue
            if multi_package and CROSS_PACKAGE_RE.search(role + " " + note_text):
                continue
            count_match = re.search(r"(\d+)\s*(?:人|名)", str(entry.get("note") or "") + " " + role)
            count = int(count_match.group(1)) if count_match else (int(entry.get("count") or 0) if int(entry.get("count") or 0) > 1 else 0)
            if count:
                explicit.append({"name": role, "count": count, "unit": "人"})
            else:
                roles.append(role)
        for item in source_texts:
            if "：" not in item:
                continue
            if multi_package and CROSS_PACKAGE_RE.search(item):
                continue
            label, detail = item.split("：", 1)
            role = _auto_clean(label)
            generic_label = role in ("人员要求", "人员", "岗位要求", "人员配置") or (
                ("人员" in role or "场地" in role or "车辆" in role) and ("要求" in role or "★" in role or "配置" in role)
            )
            if generic_label:
                role_match = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,20}?)\s*\d+\s*(?:人|名)", detail)
                if role_match:
                    role = _auto_clean(role_match.group(1))
            match = re.search(r"(\d+)\s*(?:人|名)", detail)
            if role and match and not _auto_is_business_material(item):
                explicit.append({"name": role, "count": int(match.group(1)), "unit": "人"})
                roles.append(role)
            elif role and not _auto_is_business_material(item):
                roles.append(role)
        text_prefix = "拟投入"
        capability_word = "岗位人员"
    else:
        explicit, roles, equipment_sources = _auto_equipment_requirements(texts, multi_package=multi_package)
        sources = equipment_sources or sources
        json_text = str(bidder_summary)
        text_prefix = "自有" if any(token in json_text for token in ("设备", "车辆", "机具", "自有")) else "拟投入"
        capability_word = "设备"

    # 去重、去重名
    deduped: list[dict[str, Any]] = []
    seen_explicit: set[tuple[str, int, str]] = set()
    for item in explicit:
        key = (item["name"], item["count"], item["unit"])
        if key not in seen_explicit and not CROSS_PACKAGE_RE.search(item["name"]):
            seen_explicit.add(key)
            deduped.append(item)
    roles = [role for role in dict.fromkeys(roles) if role and not CROSS_PACKAGE_RE.search(role)][:8]

    if deduped:
        mirrored = "、".join(f"{item['name']}{item['count']}{item['unit']}" for item in deduped[:8])
        # C06：合计行不并入岗位列表，但把招标文件声明的总人数作为汇总镜像出来（「，合计N人」）
        total = derive_personnel_total(texts, multi_package=multi_package) if field == "personnelPlan" else 0
        total_suffix = f"，合计{total}人" if total > 0 else ""
        value = f"{text_prefix}{mirrored}{total_suffix}，满足招标文件要求"
        basis = "、".join(sources) or "提取稿要求"
        return {"value": value, "basis": basis, "valueSource": "extracted", "tier": "explicit", "counts": deduped[:8]}
    if roles:
        value = f"按招标文件要求配置{'、'.join(roles[:6])}等{capability_word}，满足招标文件最低要求"
        basis = "、".join(sources) or "提取稿要求"
        return {"value": value, "basis": basis, "valueSource": "extracted", "tier": "capability", "counts": []}
    if field == "personnelPlan":
        value = "按招标文件要求和履约需要配置人员，具体清单进场前报采购人确认"
    else:
        value = "按招标文件要求和履约需要配置设备，具体清单进场前报采购人确认"
    return {"value": value, "basis": "无明确要求", "valueSource": "agent-default", "tier": "commitment", "counts": []}


def _auto_schedule_basis(plan: Mapping[str, Any]) -> str:
    """P2-9：auto 推导依据写清「哪个字段来自哪类期限」，避免 constructionPlan 语义错位。"""
    service = str(plan.get("servicePeriod") or "")
    construction = str(plan.get("constructionPlan") or "")
    parts: list[str] = []
    if service:
        parts.append(f"提取稿合同履行期限→schedulePlan.servicePeriod=「{service}」")
    if construction:
        parts.append(f"提取稿工期→schedulePlan.constructionPlan=「{construction}」")
    if not parts:
        return "无明确期限要求，schedulePlan 留空（agent-default，不编造日期/工期）"
    return "；".join(parts) + "（extracted，不编造具体日期）"


def build_auto_fill(
    *,
    texts: Mapping[str, str],
    settings: Mapping[str, Any],
    bidder_summary: Mapping[str, Any],
    package_id: str,
    multi_package: bool = False,
) -> dict[str, Any]:
    """自动模式 8 项预填值 + valueSource + autoBasis（不提问）。"""
    personnel = _auto_plan_description(texts, field="personnelPlan", package_id=package_id, bidder_summary=bidder_summary, multi_package=multi_package)
    equipment = _auto_plan_description(texts, field="equipmentPlan", package_id=package_id, bidder_summary=bidder_summary, multi_package=multi_package)
    default_tier = settings.get("defaultWordCountTier")
    word_count = default_tier if isinstance(default_tier, int) and not isinstance(default_tier, bool) and default_tier > 0 else 50000
    schedule_default, schedule_derived = derive_schedule_plan(texts)
    quota = dict(DEFAULT_FIGURE_QUOTA)
    default_quota = settings.get("defaultFigureQuota")
    if isinstance(default_quota, Mapping):
        for key in FIGURE_KEYS:
            value = default_quota.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                quota[key] = value
    quota["network"] = 0
    style = str((settings.get("styleProfiles") or {}).get("tech") or settings.get("styleProfile") or "official-tech")
    values = {
        "expectedWordCount": word_count,
        "personnelPlan": personnel["value"],
        "equipmentPlan": equipment["value"],
        "schedulePlan": dict(schedule_default),
        "figureQuota": quota,
        "exportStyleProfile": style,
    }
    value_source = {
        "expectedWordCount": "agent-default",
        "personnelPlan": personnel["valueSource"],
        "equipmentPlan": equipment["valueSource"],
        "schedulePlan": "extracted" if schedule_derived else "agent-default",
        "exportStyleProfile": "agent-default",
    }
    for key in FIGURE_KEYS:
        value_source[key] = "agent-default"
    bases = {
        "expectedWordCount": f"user-settings.defaultWordCountTier={word_count}",
        "personnelPlan": personnel["basis"],
        "equipmentPlan": equipment["basis"],
        "schedulePlan": _auto_schedule_basis(schedule_default),
        "exportStyleProfile": "user-settings.styleProfiles.tech",
    }
    for key in FIGURE_KEYS:
        bases[key] = "user-settings.defaultFigureQuota"
    bases["network"] = "联网配图功能已移除（固定 0）"
    return {
        "mode": "auto",
        "values": values,
        "valueSource": value_source,
        "autoBasis": bases,
        "personnelBasis": personnel["basis"],
        "equipmentBasis": equipment["basis"],
        "personnelTier": personnel["tier"],
        "equipmentTier": equipment["tier"],
        "personnelCounts": personnel["counts"],
        "equipmentCounts": equipment["counts"],
        "tiers": {"personnelPlan": personnel["tier"], "equipmentPlan": equipment["tier"]},
    }


def requirement_questions(summary: Mapping[str, Any], current_meta: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """供 Agent 展示给用户的提问卡（先展示提取要求，再请用户用自己的话简短回答）。"""
    meta = current_meta or {}
    current_value_source = meta.get("valueSource") or {}
    return [
        {
            "id": "expectedWordCount",
            "label": "技术方案字数",
            "ask": "您希望技术方案写多少字？可选 5 万/10 万/15 万，或直接给正整数。",
            "default": 50000,
            "currentValue": meta.get("expectedWordCount"),
            "valueSource": current_value_source.get("expectedWordCount"),
        },
        {
            "id": "personnelPlan",
            "label": "计划投入人员（用户原话）",
            "inputType": "text",
            "ask": "根据上面提取到的人员要求，请用一句话说明你计划投入的人员（原话记录，不必结构化）。",
            "extractedRequirements": summary.get("personnel") or [],
            "currentValue": meta.get("personnelPlan"),
            "valueSource": meta.get("personnelPlanSource") or current_value_source.get("personnelPlan"),
        },
        {
            "id": "equipmentPlan",
            "label": "计划投入设备（用户原话）",
            "inputType": "text",
            "ask": "根据上面提取到的设备/车辆要求，请用一句话说明你计划投入的设备（原话记录，不必结构化）。",
            "extractedRequirements": summary.get("equipment") or [],
            "currentValue": meta.get("equipmentPlan"),
            "valueSource": meta.get("equipmentPlanSource") or current_value_source.get("equipmentPlan"),
        },
        {
            "id": "schedulePlan",
            "label": "工期/计划",
            "ask": "根据提取到的工期要求，请确认开工/交付/服务计划（可只写一句计划）。",
            "extractedRequirements": [summary.get("schedule")] if summary.get("schedule") else [],
            "currentValue": meta.get("schedulePlan"),
            "valueSource": current_value_source.get("schedulePlan"),
        },
        {
            "id": "figureQuota.table",
            "label": "表格数量",
            "ask": "技术方案中预计放多少个表格？默认 5。",
            "currentValue": (meta.get("figureQuota") or {}).get("table"),
        },
        {
            "id": "figureQuota.mermaid",
            "label": "示意图数量",
            "ask": "预计画多少张示意图（Mermaid：流程图/时序图/甘特图等）？默认 5。",
            "currentValue": (meta.get("figureQuota") or {}).get("mermaid"),
        },
        {
            "id": "figureQuota.terminal",
            "label": "终端图参考数量",
            "ask": "预计需要多少张模拟终端图？参考值（0=不生成；实际按适合章节规划，可多张，不强制凑数）。默认 5。",
            "currentValue": (meta.get("figureQuota") or {}).get("terminal"),
        },
    ]


def load_package_meta_for_update(project_dir: Path, package_id: str) -> tuple[Path, dict[str, Any]]:
    """定位已有包目录与 package-meta（update-answers 不新建目录）。"""
    dirs = sorted(
        (entry for entry in (project_dir / "packages").iterdir() if entry.is_dir() and entry.name.startswith(package_id + "-"))
        if (project_dir / "packages").is_dir()
        else []
    )
    for package_dir in reversed(dirs):
        meta_path = package_dir / "package-meta.json"
        if meta_path.is_file():
            try:
                meta = jsonio.read_json(meta_path)
            except errors.BgError:
                continue
            if isinstance(meta, Mapping):
                return package_dir, dict(meta)
    raise errors.BgError(
        errors.E_NOT_FOUND,
        f"未找到 {package_id} 的 package-meta.json",
        hint="先执行 bg_prepare.py 创建包目录与 package-meta",
        detail={"packageId": package_id, "packagesDir": "packages/"},
    )


def _style_ids(kind: str) -> list[str]:
    """可用导出样式 id（templates/styles/<kind>/*.json 的 id）。"""
    directory = _PLUGIN_ROOT / "templates" / "styles" / kind
    ids: list[str] = []
    if not directory.is_dir():
        return ids
    for path in sorted(directory.glob("*.json")):
        try:
            document = jsonio.read_json(path)
        except errors.BgError:
            continue
        if isinstance(document, Mapping) and document.get("id"):
            ids.append(str(document["id"]))
    return ids


def build_package_meta(
    *,
    project_dir: Path,
    project: Mapping[str, Any],
    package_id: str,
    answers: Mapping[str, Any],
    settings: Mapping[str, Any],
    texts: Mapping[str, str],
    bidder_summary: Mapping[str, Any],
    existing_meta: Mapping[str, Any] | None = None,
    auto_fill: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建/合并 package-meta（PRD 7.3 瘦字段 + 8 项 answers/valueSource；assumptions 收进 extensions）。"""
    now = paths.iso_now()
    auto_values = auto_fill.get("values") if isinstance(auto_fill, Mapping) and isinstance(auto_fill.get("values"), Mapping) else {}
    auto_sources = auto_fill.get("valueSource") if isinstance(auto_fill, Mapping) and isinstance(auto_fill.get("valueSource"), Mapping) else {}
    package_no = int(PACKAGE_ID_RE.match(package_id).group(1))
    project_name = str(project.get("projectName") or "").strip() or "未命名项目"
    package_name = project_name if int(project.get("packageCount") or 1) <= 1 else f"{project_name}（{package_id}）"
    assumptions: list[str] = []
    value_source: dict[str, str] = {key: "agent-default" for key in state_lib.PACKAGE_VALUE_SOURCE_KEYS}
    for key, source in (answers.get("valueSource") or {}).items():
        if key in value_source:
            value_source[key] = source

    # 1) expectedWordCount
    default_tier = settings.get("defaultWordCountTier")
    default_tier = default_tier if isinstance(default_tier, int) and not isinstance(default_tier, bool) and default_tier > 0 else 50000
    required = derive_word_count_requirement(texts)
    existing_source = str((existing_meta or {}).get("valueSource", {}).get("expectedWordCount") or "") if existing_meta else ""
    existing_count = (existing_meta or {}).get("expectedWordCount") if existing_meta else None
    if "expectedWordCount" in answers:
        expected_word_count = int(answers["expectedWordCount"])
        value_source["expectedWordCount"] = "user"
    elif existing_meta and existing_source == "user" and isinstance(existing_count, int):
        expected_word_count = int(existing_count)
        value_source["expectedWordCount"] = "user"
    elif auto_values.get("expectedWordCount") is not None:
        expected_word_count = int(auto_values["expectedWordCount"])
        value_source["expectedWordCount"] = str(auto_sources.get("expectedWordCount") or "agent-default")
        assumptions.append("auto 模式：字数采用 user-settings 默认档（agent-default）")
    elif required is not None:
        expected_word_count = max(required, default_tier)
        value_source["expectedWordCount"] = "extracted"
        assumptions.append(
            f"字数档：提取稿要求下限 {required} 字，user-settings 默认 {default_tier} 字，取 max → {expected_word_count}（extracted）"
        )
    else:
        expected_word_count = default_tier
        assumptions.append(f"字数档：提取稿未识别字数要求，采用 user-settings 默认 {default_tier} 字（agent-default）")

    # 2) personnelPlan / equipmentPlan / schedulePlan
    def existing_or(value: Any, field: str) -> Any:
        if existing_meta and str((existing_meta.get("valueSource") or {}).get(field) or "") == "user":
            return existing_meta.get(field)
        return value

    personnel = answers.get("personnelPlan")
    if personnel is not None:
        value_source["personnelPlan"] = "user"
        personnel_source = "user"
        if isinstance(personnel, str):
            assumptions.append("personnelPlan：用户原话记录（user，原样保存供正文使用）")
    elif auto_values.get("personnelPlan") and not (
        existing_meta and str((existing_meta.get("valueSource") or {}).get("personnelPlan") or "") == "user"
    ):
        personnel = auto_values["personnelPlan"]
        personnel_source = str(auto_sources.get("personnelPlan") or "agent-default")
        value_source["personnelPlan"] = personnel_source
        assumptions.append(f"personnelPlan：auto 模式按提取要求生成（{personnel_source}，{auto_fill.get('personnelBasis') if isinstance(auto_fill, Mapping) else ''}）")
    else:
        derived = derive_personnel_plan(texts)
        existing_user = bool(existing_meta) and str((existing_meta.get("valueSource") or {}).get("personnelPlan") or "") == "user"
        personnel = existing_or(derived, "personnelPlan")
        personnel_source = "user" if existing_user else ("extracted" if derived else "agent-default")
        value_source["personnelPlan"] = personnel_source
        if not derived and not existing_user:
            assumptions.append("personnelPlan：提取稿未识别具体人员要求，留空（agent-default）")
        elif derived:
            assumptions.append(f"personnelPlan：从 08/09 人员要求推导 {len(derived)} 项（extracted）")

    equipment = answers.get("equipmentPlan")
    if equipment is not None:
        value_source["equipmentPlan"] = "user"
        equipment_source = "user"
        if isinstance(equipment, str):
            assumptions.append("equipmentPlan：用户原话记录（user，原样保存供正文使用）")
    elif auto_values.get("equipmentPlan") and not (
        existing_meta and str((existing_meta.get("valueSource") or {}).get("equipmentPlan") or "") == "user"
    ):
        equipment = auto_values["equipmentPlan"]
        equipment_source = str(auto_sources.get("equipmentPlan") or "agent-default")
        value_source["equipmentPlan"] = equipment_source
        assumptions.append(f"equipmentPlan：auto 模式按提取要求生成（{equipment_source}，{auto_fill.get('equipmentBasis') if isinstance(auto_fill, Mapping) else ''}）")
    else:
        derived = derive_equipment_plan(texts)
        existing_user = bool(existing_meta) and str((existing_meta.get("valueSource") or {}).get("equipmentPlan") or "") == "user"
        equipment = existing_or(derived, "equipmentPlan")
        equipment_source = "user" if existing_user else ("extracted" if derived else "agent-default")
        value_source["equipmentPlan"] = equipment_source
        if not derived and not existing_user:
            assumptions.append("equipmentPlan：提取稿未识别拟投入设备要求，留空（agent-default）")
        elif derived:
            assumptions.append(f"equipmentPlan：从 06/09 采购/技术要求推导 {len(derived)} 项（extracted）")

    schedule_default, schedule_derived = derive_schedule_plan(texts)
    if answers.get("schedulePlan") is not None:
        schedule = {
            "startDate": str(answers["schedulePlan"].get("startDate") or ""),
            "deliveryDate": str(answers["schedulePlan"].get("deliveryDate") or ""),
            "constructionPlan": str(answers["schedulePlan"].get("constructionPlan") or ""),
            "servicePeriod": str(answers["schedulePlan"].get("servicePeriod") or ""),
        }
        value_source["schedulePlan"] = "user"
    elif existing_meta and str((existing_meta.get("valueSource") or {}).get("schedulePlan") or "") == "user":
        schedule = dict(existing_meta.get("schedulePlan") or schedule_default)
        schedule.setdefault("servicePeriod", "")
        value_source["schedulePlan"] = "user"
    elif auto_values.get("schedulePlan") is not None and not (
        existing_meta and str((existing_meta.get("valueSource") or {}).get("schedulePlan") or "") == "user"
    ):
        schedule = dict(auto_values["schedulePlan"])
        schedule.setdefault("servicePeriod", "")
        value_source["schedulePlan"] = str(auto_sources.get("schedulePlan") or "agent-default")
        assumptions.append(f"auto 模式：schedulePlan 采用 {value_source['schedulePlan']} 推导（servicePeriod/constructionPlan 分字段记录）")
    else:
        schedule = dict(schedule_default)
        value_source["schedulePlan"] = "extracted" if schedule_derived else "agent-default"
        if schedule.get("servicePeriod"):
            assumptions.append(f"schedulePlan：从提取稿合同履行期限推导 servicePeriod「{schedule['servicePeriod']}」（extracted，不编造日期）")
        if schedule.get("constructionPlan"):
            assumptions.append(f"schedulePlan：从提取稿工期要求推导 constructionPlan「{schedule['constructionPlan']}」（extracted，不编造日期）")
        if not schedule_derived:
            assumptions.append("schedulePlan：未推导出明确期限，servicePeriod/constructionPlan 留空（agent-default，不编造日期或工期）")
    schedule.setdefault("servicePeriod", "")

    # 3) figureQuota
    allow_network = bool(answers.get("allowNetworkImages", settings.get("allowNetworkImages")))
    default_quota = settings.get("defaultFigureQuota")
    quota = dict(DEFAULT_FIGURE_QUOTA)
    if isinstance(default_quota, Mapping):
        for key in FIGURE_KEYS:
            value = default_quota.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                quota[key] = value
    auto_quota = auto_values.get("figureQuota") if isinstance(auto_values.get("figureQuota"), Mapping) else None
    if auto_quota and not existing_meta and not answers.get("figureQuota"):
        for key in FIGURE_KEYS:
            value = auto_quota.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                quota[key] = value
                value_source[key] = str(auto_sources.get(key) or "agent-default")
    user_quota = answers.get("figureQuota") or {}
    for key in FIGURE_KEYS:
        if key in user_quota:
            quota[key] = int(user_quota[key])
            value_source[key] = "user"
    if not user_quota and existing_meta and isinstance(existing_meta.get("figureQuota"), Mapping):
        existing_sources = existing_meta.get("valueSource") or {}
        for key in FIGURE_KEYS:
            if str(existing_sources.get(key) or "") == "user":
                quota[key] = int((existing_meta.get("figureQuota") or {}).get(key, quota[key]))
                value_source[key] = "user"
    if quota.get("network", 0) != 0:
        quota["network"] = 0
        value_source["network"] = "agent-default"
        assumptions.append("figureQuota.network：联网配图功能已移除，网络图配额固定为 0（旧配置兼容忽略）")
    if not user_quota:
        assumptions.append(
            "figureQuota：表 {} / 示意图 {} / 网络图 {} / 终端图参考 {}（默认值来自 user-settings，agent-default）".format(
                quota["table"], quota["mermaid"], quota["network"], quota["terminal"]
            )
        )
    else:
        assumptions.append(
            "figureQuota：用户提供 {}；其余采用默认值".format("、".join(f"{key}={value}" for key, value in user_quota.items()))
        )
    for key in FIGURE_KEYS:
        value = quota.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"figureQuota.{key} 必须是 >=0 整数", detail={"value": value})

    # 4) exportStyleProfile
    tech_styles = _style_ids("tech") or ["official-tech"]
    default_style = str((settings.get("styleProfiles") or {}).get("tech") or (settings.get("styleProfile") or "") or "official-tech")
    if default_style not in tech_styles:
        default_style = tech_styles[0]
    if "exportStyleProfile" in answers:
        export_style = str(answers["exportStyleProfile"])
        if export_style not in tech_styles:
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"exportStyleProfile 必须是可用 tech 样式：{tech_styles}",
                detail={"value": export_style, "available": tech_styles},
            )
    elif auto_values.get("exportStyleProfile"):
        export_style = str(auto_values["exportStyleProfile"])
        value_source["exportStyleProfile"] = str(auto_sources.get("exportStyleProfile") or "agent-default")
    elif existing_meta and str(existing_meta.get("exportStyleProfile") or "") in tech_styles:
        export_style = str(existing_meta["exportStyleProfile"])
    else:
        export_style = default_style

    created_at = str((existing_meta or {}).get("createdAt") or now)
    extensions: dict[str, Any] = {}
    if assumptions:
        extensions["assumptions"] = assumptions
    if isinstance(auto_fill, Mapping):
        extensions["autoFill"] = {
            "mode": str(auto_fill.get("mode") or "auto"),
            "personnelBasis": str(auto_fill.get("personnelBasis") or ""),
            "equipmentBasis": str(auto_fill.get("equipmentBasis") or ""),
            "tiers": auto_fill.get("tiers") or {},
            "autoBasis": {key: str(value) for key, value in (auto_fill.get("autoBasis") or {}).items() if isinstance(value, (str, int, float))},
            "generatedAt": now,
        }
    meta: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "packageId": package_id,
        "packageNo": package_no,
        "packageName": package_name,
        "expectedWordCount": expected_word_count,
        "personnelPlan": personnel,
        "equipmentPlan": equipment,
        "schedulePlan": schedule,
        "figureQuota": quota,
        "valueSource": value_source,
        "exportStyleProfile": export_style,
        "exportPath": "",
        "createdAt": created_at,
        "updatedAt": now,
        "extensions": extensions,
    }
    return meta


# ---------------------------------------------------------------------------
# 目录 / 校验 / 准备
# ---------------------------------------------------------------------------
def find_or_create_package_dir(project_dir: Path, package_id: str) -> tuple[Path, bool]:
    """幂等定位/创建 ``packages/package-<N>-<ts14>`` 包目录。"""
    root = project_dir / "packages"
    root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(package_id)}-(\d{{14}})$")
    existing = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and pattern.match(entry.name)),
        key=lambda entry: entry.name,
    )
    if existing:
        return existing[-1], False
    package_dir = root / f"{package_id}-{paths.timestamp14()}"
    package_dir.mkdir(parents=True, exist_ok=False)
    return package_dir, True


def ensure_package_dirs(package_dir: Path) -> list[str]:
    created: list[str] = []
    for rel in REQUIRED_PACKAGE_DIRS:
        target = package_dir / rel
        if not target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            created.append(rel)
    return created


def package_tree_status(project_dir: Path, package_id: str) -> dict[str, Any]:
    """复用 state 模块的包目录/package-meta 校验语义（只读）。"""
    return state_lib._package_tree_status(project_dir, package_id)  # noqa: SLF001 - 同阶段脚本复用 guard 语义


def check_ready(workspace: Path, project_dir: Path, *, package_id: str | None = None) -> dict[str, Any]:
    """只读预检：prepare 前置 + T3 readiness；package_id 可只验目标包。"""
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    stage = str(state.get("currentStage") or "")
    packages = [package_id] if package_id else resolve_packages(project)
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "id": "stage",
            "pass": stage == "package_prepare",
            "detail": {"currentStage": stage, "expected": "package_prepare"},
        }
    )
    package_count = project.get("packageCount")
    fields_ok = (
        isinstance(project.get("isSubcontracted"), bool)
        and isinstance(package_count, int)
        and not isinstance(package_count, bool)
        and package_count >= 1
        and len(packages) >= 1
        and len(set(packages)) == len(packages)
        and all(1 <= int(entry.split("-")[1]) <= package_count for entry in packages)
    )
    checks.append(
        {
            "id": "project_fields",
            "pass": fields_ok,
            "detail": {"isSubcontracted": project.get("isSubcontracted"), "packageCount": package_count, "packagesToWrite": packages},
        }
    )
    missing_files: list[str] = []
    for package_id in packages:
        _texts, missing = load_extraction(project_dir, package_id)
        missing_files.extend(missing)
    checks.append({"id": "extraction_files", "pass": not missing_files, "detail": {"missing": missing_files}})
    tree_status = {package_id: package_tree_status(project_dir, package_id) for package_id in packages}
    checks.append(
        {
            "id": "package_tree",
            "pass": all(item.get("ok") for item in tree_status.values()),
            "detail": {
                package_id: {"packageDir": item.get("packageDir"), "missingDirs": item.get("missingDirs"), "metaIssues": item.get("metaIssues")}
                for package_id, item in tree_status.items()
            },
        }
    )
    t3: dict[str, Any] = {"ok": False, "pass": False, "guard": None}
    if stage == "package_prepare":
        try:
            t3 = state_lib.check_transition(workspace, project_dir, to_stage="outline", package=packages[0] if packages else None)
        except errors.BgError as exc:
            t3 = {"ok": False, "pass": False, "error": exc.code, "message": exc.message, "detail": exc.detail}
    ready = bool(checks and all(item["pass"] for item in checks) and t3.get("pass"))
    return {
        "ok": True,
        "command": "prepare_check",
        "projectId": state.get("projectId"),
        "currentStage": stage,
        "currentPackage": state.get("currentPackage"),
        "revision": state.get("revision"),
        "packages": packages,
        "checks": checks,
        "readyForPrepare": bool(checks[0]["pass"] and checks[1]["pass"] and checks[2]["pass"]),
        "readyForT3": ready,
        "t3Check": t3,
        "packageTree": tree_status,
    }


def verify_prepared(workspace: Path, project_dir: Path, *, package_id: str | None = None) -> dict[str, Any]:
    """只读校验已生成包（F4.3 必存清单）；package_id 可只验目标包；不通过抛 E_VALIDATE_FAILED。"""
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    packages = [package_id] if package_id else resolve_packages(project)
    if not packages:
        raise errors.BgError(errors.E_VALIDATE_FAILED, "packagesToWrite 为空", detail={"packagesToWrite": packages})
    tree_status = {package_id: package_tree_status(project_dir, package_id) for package_id in packages}
    missing = {
        package_id: {"missingDirs": item.get("missingDirs"), "metaIssues": item.get("metaIssues")}
        for package_id, item in tree_status.items()
        if not item.get("ok")
    }
    materials_index_status: dict[str, Any] = {}
    for package_id in packages:
        package_dir = None
        dirs = sorted(
            (entry for entry in (project_dir / "packages").iterdir() if entry.is_dir() and entry.name.startswith(package_id + "-"))
            if (project_dir / "packages").is_dir()
            else []
        )
        if dirs:
            package_dir = dirs[-1]
        index_path = package_dir / "materials-index.json" if package_dir is not None else None
        if index_path is not None and index_path.is_file():
            try:
                index_doc = jsonio.read_json(index_path)
            except errors.BgError:
                materials_index_status[package_id] = "invalid"
                continue
            items = index_doc.get("items") if isinstance(index_doc, Mapping) else None
            materials_index_status[package_id] = "ok" if isinstance(items, list) else "invalid"
    stage = str(state.get("currentStage") or "")
    t3: dict[str, Any] | None = None
    t3_ok = True
    if stage == "package_prepare":
        t3 = state_lib.check_transition(workspace, project_dir, to_stage="outline", package=packages[0])
        t3_ok = bool(t3.get("pass"))
    invalid_index = [key for key, value in materials_index_status.items() if value == "invalid"]
    payload = {
        "ok": True,
        "command": "prepare_verify",
        "projectId": state.get("projectId"),
        "currentStage": stage,
        "revision": state.get("revision"),
        "packages": packages,
        "packageTree": tree_status,
        "materialsIndex": materials_index_status,
        "t3Check": t3,
        "verified": not missing and t3_ok and not invalid_index,
        # P1-3：与其他 prepare 输出统一契约；单包时显式给 packageId。
        "packageId": packages[0] if len(packages) == 1 else None,
        "readyForT3": not missing and t3_ok and not invalid_index,
        "metaKeysChecked": list(PRD_META_KEYS),
    }
    if missing or not t3_ok or invalid_index:
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            "撰写前准备必存清单校验未通过",
            hint="按 detail 补齐后重跑 bg_prepare.py（幂等）",
            detail=payload,
        )
    return payload


#: P2-8：资格/证书要求提示关键词（只认资格/证书类硬项，正文性描述不算）
QUALIFICATION_KEYWORDS = ("资质", "证书", "等级", "监理", "建造师", "安全许可", "认证", "许可证", "项目管理师", "职称")
QUALIFICATION_CLAUSE_RE = re.compile(r"[，,；;。、]")
QUALIFICATION_NOUN_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,16}(?:资质等级|资质证书|认证证书|安全生产许可证|许可证|建造师|监理工程师|项目管理师|职称|证书|资质|认证)"
)
QUALIFICATION_MODAL_RE = re.compile(r"具备|具有|须|应|需|提供|持有|要求")
QUALIFICATION_MODAL_PREFIX_RE = re.compile(
    r"^(?:投标人|供应商|响应人|申请人|本项目|项目|须|应|需|必须|具备|具有|提供|持有|要求|有效的|有效|相应的|相关|国家|行业|省级|及以上)+"
)
QUALIFICATION_MAX_GAPS = 5
QUALIFICATION_MAX_TOKEN_CHARS = 120
#: 多包范围标记：第4包 / 第1—3包 / 第4、5包（用于资格提示按包过滤）
PACKAGE_SCOPE_RE = re.compile(r"第\s*(\d+(?:\s*(?:[—\-~～至到、,，和及])\s*\d+)*)\s*包")
PACKAGE_SCOPE_NEGATION_RE = re.compile(r"不适用于本包|本包未要求|本包未提及|本包不适用|不适用本包")
#: 程序性/条件性文本（不是"要求投标人具备什么"），不计入缺口
QUALIFICATION_NOISE_RE = re.compile(r"审查|由采购人|招标代理|按照招标文件|未提供|不符|评标|评审程序|材料清单")
#: 产品/技术标准条款（GB/T、产品规格、材料性能等）不是投标人资格，排除
QUALIFICATION_PRODUCT_STANDARD_RE = re.compile(
    r"GB\s*/?\s*T?\s*\d|国家标准|行业标准|产品|材料|设备|规格|型号|技术参数|技术指标|性能|检验|检测|说明书"
)
#: 资格/证书硬名词：出现时才允许保留“产品标准”句（如“ISO9001 认证证书”）
QUALIFICATION_CREDENTIAL_RE = re.compile(r"资质|证书|许可证|建造师|监理|职称|安全生产许可")
#: 过于泛化的词，不作为可缺口名词
QUALIFICATION_GENERIC_TERMS = frozenset({"资质", "证书", "等级", "认证", "许可证", "投标人资质", "投标人证书", "资质要求", "资格证书"})


def load_bidder_cert_text(workspace: Path, project: Mapping[str, Any]) -> tuple[str, bool]:
    """读取投标人证书清单文本；返回 (文本, 是否有可比对资料)。"""
    bidder_ref = project.get("bidderRef") if isinstance(project.get("bidderRef"), Mapping) else {}
    dir_name = str(bidder_ref.get("dirName") or "")
    cert_file = workspace / "dsh" / "bidders" / dir_name / "certificates.json" if dir_name else None
    if cert_file is not None and cert_file.is_file():
        try:
            return json.dumps(jsonio.read_json(cert_file), ensure_ascii=False), True
        except errors.BgError:
            return "", False
    return "", False


def _package_numbers(text: str) -> list[int]:
    """从“第1包/第1—3包/第4、5包”文本提取包号（无标记返回空列表）。"""
    numbers: list[int] = []
    for match in PACKAGE_SCOPE_RE.finditer(str(text or "")):
        for part in re.split(r"[、,，和及]", match.group(1)):
            bounds = [item for item in re.split(r"[—\-~～至到]", part) if item.strip().isdigit()]
            if len(bounds) == 2:
                start, end = int(bounds[0]), int(bounds[1])
                if start <= end:
                    numbers.extend(range(start, end + 1))
                else:
                    numbers.extend((start, end))
            elif len(bounds) == 1:
                numbers.append(int(bounds[0]))
    return sorted(set(numbers))


def _target_package_number(package_id: str) -> int:
    match = re.search(r"(\d+)$", str(package_id or ""))
    return int(match.group(1)) if match else 0


def _qualification_clauses(text: str) -> list[tuple[str, str]]:
    """把资格类文本切成**完整短语**，并带上标题/表格行上下文（用于按包过滤）。

    返回 `(clause, context)`；context 含当前标题与所在表格行，便于识别“第N包/不适用于本包”。
    """
    clauses: list[tuple[str, str]] = []
    seen: set[str] = set()
    heading_context = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading_context = stripped if _package_numbers(stripped) else ""
            continue
        if stripped.startswith("|"):
            # Markdown 表格行：单元格各自作为一个短语；跳过表头/分隔行
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not cells or all(not cell or set(cell) <= set("-: ") for cell in cells):
                continue
            candidates = cells
            row_context = " ".join(cells)
        else:
            candidates = [stripped.lstrip("-*0123456789. ()").strip()]
            row_context = ""
        for candidate in candidates:
            for clause in QUALIFICATION_CLAUSE_RE.split(candidate):
                clause = re.sub(r"\s+", " ", clause).strip(" ：:；;，,、*·「」『』")
                if len(clause) < 6:
                    continue
                if not any(keyword in clause for keyword in QUALIFICATION_KEYWORDS):
                    continue
                if not QUALIFICATION_MODAL_RE.search(clause):
                    continue
                if QUALIFICATION_NOISE_RE.search(clause):
                    continue
                if QUALIFICATION_PRODUCT_STANDARD_RE.search(clause) and not QUALIFICATION_CREDENTIAL_RE.search(clause):
                    continue
                if clause in seen:
                    continue
                seen.add(clause)
                clauses.append((clause, " ".join(part for part in (heading_context, row_context, clause) if part)))
    return clauses


def _qualification_terms(clause: str) -> list[str]:
    """从完整短语抽资格名词（去掉「投标人/须/具备」等前缀、括号说明与「证书」类后缀），用于和投标人资料比对。"""
    terms: list[str] = []
    for match in QUALIFICATION_NOUN_RE.finditer(clause):
        noun = QUALIFICATION_MODAL_PREFIX_RE.sub("", match.group(0)).strip()
        variants = [noun, re.sub(r"[（(][^）)]*[）)]", "", noun).strip()]
        for variant in list(variants):
            base = re.sub(r"(?:资质等级|资质证书|认证证书|安全生产许可证|许可证|证书|资质|认证)$", "", variant).strip()
            variants.append(base)
        for candidate in variants:
            if len(candidate) >= 3 and candidate not in QUALIFICATION_GENERIC_TERMS and candidate not in terms:
                terms.append(candidate)
    return terms


def find_qualification_gaps(workspace: Path, project_dir: Path, project: Mapping[str, Any], package_id: str) -> list[dict[str, Any]]:
    """F4 前资格/证书要求提示（P2-8 去噪）：只对 02/08/09 的资格硬项做比对，每包 ≤5 条 info。

    - 按标点切出**完整短语**，missingTokens 就是整条短语（不再把中文切成 2–8 字碎片）；
    - 只取资格硬性项（资质/证书/等级/许可证/建造师/监理/认证等 + 具备/须/应/需/提供/持有）；
    - **按包过滤**：有“第N包/第1—3包/第4、5包”且不含当前包 → 跳过；本包写“不适用于本包/本包未要求” → 跳过；
    - 仅作 info 提示（blocking=false）：资格/证书要求属商务部分，供用户核对；技术方案不据此判定，不阻断 F4/T3。
    """
    cert_text, _cert_available = load_bidder_cert_text(workspace, project)
    target_no = _target_package_number(package_id)
    sources = [project_dir / "extraction" / "common" / "02-Eligibility-Review.md"]
    sources.extend(
        project_dir / "extraction" / "packages" / package_id / name
        for name in ("08-Business-Requirements.md", "09-Technical-Requirements.md")
    )
    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sources:
        if not path.is_file():
            continue
        source = paths.rel_posix(project_dir, path)
        for clause, context in _qualification_clauses(path.read_text(encoding="utf-8-sig")):
            if PACKAGE_SCOPE_NEGATION_RE.search(context):
                continue
            package_numbers = _package_numbers(context)
            if package_numbers and target_no not in package_numbers:
                continue
            terms = _qualification_terms(clause)
            if not terms:
                continue
            if any(term in cert_text for term in terms):
                continue
            key = clause[:80]
            if key in seen:
                continue
            seen.add(key)
            gaps.append(
                {
                    "packageId": package_id,
                    "requirement": clause[:300],
                    "source": source,
                    "missingTokens": [clause[:QUALIFICATION_MAX_TOKEN_CHARS]],
                    "severity": "info",
                    "blocking": False,
                    "note": "资格/证书要求提示（商务部分，供用户核对）；info 不阻断 F4/T3，技术方案不据此判定",
                }
            )
            if len(gaps) >= QUALIFICATION_MAX_GAPS:
                return gaps
    return gaps


def prepare_project(
    workspace: Path,
    project_dir: Path,
    *,
    package_id: str | None = None,
    answers_path: Path | None = None,
    answers_document: Mapping[str, Any] | None = None,
    auto: bool = False,
    switch_package: bool = True,
) -> dict[str, Any]:
    """执行 F4 准备（幂等）：包目录 + package-meta + project 更新 + set-package + 事件 + T3 预检。

    ``switch_package=False`` 供 ``--all-selected`` 批量准备使用：中间包不写 set-package
    事件，避免历史里出现“来回切包”；批量结束由调用方把当前包设回首包。
    """
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    if str(state.get("currentStage") or "") != "package_prepare":
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"当前阶段（{state.get('currentStage')}）不是 package_prepare，不能执行撰写前准备",
            hint="先完成 F3 提取并通过 T2 迁移（bg_state.py transition --to package_prepare）",
            detail={"currentStage": state.get("currentStage"), "allowedNext": state_lib.allowed_next(state, project)},
        )
    settings = _read_user_settings(workspace)
    if answers_document is None:
        answers_document = {}
        if answers_path is not None:
            if not answers_path.is_file():
                raise errors.BgError(errors.E_NOT_FOUND, f"answers 文件不存在：{answers_path}", detail={"path": str(answers_path)})
            document = jsonio.read_json(answers_path)
            if not isinstance(document, Mapping):
                raise errors.BgError(errors.E_USAGE, "answers 必须是 JSON 对象", detail={"path": str(answers_path)})
            answers_document = document
    raw_answers = answers_document.get("answers") if isinstance(answers_document.get("answers"), Mapping) else answers_document
    allow_network = bool(settings.get("allowNetworkImages"))
    if isinstance(raw_answers, Mapping) and "allowNetworkImages" in raw_answers:
        allow_network = bool(raw_answers["allowNetworkImages"])
    answers = normalize_answers(raw_answers, allow_network_images=allow_network)

    packages = resolve_packages(project)
    if not packages:
        raise errors.BgError(errors.E_GUARD_FAILED, "无法确定要撰写的包（packagesToWrite 为空）")
    target = package_id or str(state.get("currentPackage") or "") or packages[0]
    if not PACKAGE_ID_RE.match(target):
        raise errors.BgError(errors.E_USAGE, "package 必须形如 package-<N>", detail={"package": target})
    if target not in packages:
        selected_by_user = bool(project.get("writingPackages"))
        if selected_by_user:
            raise errors.BgError(
                errors.E_GUARD_FAILED,
                f"目标包 {target} 不属于用户选定的 writingPackages",
                hint="F4 只能 prepare 一次性选包中的包；先运行 bg_project.py select-packages",
                detail={"package": target, "writingPackages": packages},
            )
        # 兼容旧项目：允许按 packageCount 扩展包列表（例如 packagesToWrite 尚未回填完整）
        if int(PACKAGE_ID_RE.match(target).group(1)) <= int(project.get("packageCount") or len(packages)):
            packages = sorted(set(packages + [target]), key=lambda item: int(PACKAGE_ID_RE.match(item).group(1)))
        else:
            raise errors.BgError(
                errors.E_GUARD_FAILED,
                f"目标包 {target} 不在 packagesToWrite 中",
                detail={"package": target, "packagesToWrite": packages},
            )

    texts, missing = load_extraction(project_dir, target)
    if missing:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"缺少提取稿 {len(missing)} 个，无法生成必存清单",
            hint="先运行 bg_extract_validate.py 校验/修复提取稿",
            detail={"missing": missing, "packageId": target},
        )
    bidder_summary = load_bidder_summary(workspace, project)
    auto_mode = bool(auto) or str(settings.get("interactionMode") or "") == "auto"
    multi_package = int(project.get("packageCount") or len(packages) or 1) > 1
    auto_fill = build_auto_fill(texts=texts, settings=settings, bidder_summary=bidder_summary, package_id=target, multi_package=multi_package) if auto_mode else None

    package_dir, created_dir = find_or_create_package_dir(project_dir, target)
    created_dirs = ensure_package_dirs(package_dir)
    meta_path = package_dir / "package-meta.json"
    existing_meta: Mapping[str, Any] | None = None
    if meta_path.is_file():
        try:
            loaded = jsonio.read_json(meta_path)
        except errors.BgError:
            loaded = None
        if isinstance(loaded, Mapping):
            existing_meta = loaded
    meta = build_package_meta(
        project_dir=project_dir,
        project=project,
        package_id=target,
        answers=answers,
        settings=settings,
        texts=texts,
        bidder_summary=bidder_summary,
        existing_meta=existing_meta,
        auto_fill=auto_fill,
    )
    jsonio.write_json_atomic(meta_path, meta)

    # project.json：回填 packagesToWrite/status/extensions.prepare
    now = paths.iso_now()
    updated_project = dict(project)
    updated_project["packagesToWrite"] = packages
    updated_project["status"] = "package_prepared"
    extensions = dict(updated_project.get("extensions") or {})
    prepare_extension = dict(extensions.get("prepare") or {})
    package_dirs = dict(prepare_extension.get("packageDirs") or {})
    package_dirs[target] = paths.rel_posix(project_dir, package_dir)
    package_prepared_at = dict(prepare_extension.get("packagePreparedAt") or {})
    package_prepared_at.setdefault(target, now)
    prepare_extension.update(
        {
            "preparedAt": prepare_extension.get("preparedAt") or now,
            "packages": packages,
            "packageDirs": package_dirs,
            "packagePreparedAt": package_prepared_at,
            "status": "prepared",
        }
    )
    extensions["prepare"] = prepare_extension
    qualification_gaps = find_qualification_gaps(workspace, project_dir, project, target)
    _cert_text, cert_available = load_bidder_cert_text(workspace, project)
    gaps_by_package = dict(extensions.get("qualificationGaps") or {}) if isinstance(extensions.get("qualificationGaps"), dict) else {}
    gaps_by_package[target] = qualification_gaps
    extensions["qualificationGaps"] = gaps_by_package
    updated_project["extensions"] = extensions
    updated_project["updatedAt"] = now
    schema.raise_if_errors(schema.validate_project(updated_project), filename="project.json")
    jsonio.write_json_atomic(project_dir / "project.json", updated_project)

    if switch_package:
        set_package_result = state_lib.set_package(workspace, project_dir, package=target)
    else:
        unchanged_state = state_lib.load_state(project_dir)
        set_package_result = {
            "revision": unchanged_state.get("revision"),
            "changed": [],
            "historyKind": "skipped",
            "currentPackage": unchanged_state.get("currentPackage"),
            "note": "F4 批量准备：中间包不切换当前包，避免历史出现来回切包",
        }
    event_log.log_event(
        workspace,
        "package.prepared",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=target,
        detail={
            "packageDir": paths.rel_posix(project_dir, package_dir),
            "expectedWordCount": meta["expectedWordCount"],
            "figureQuota": meta["figureQuota"],
            "valueSource": meta["valueSource"],
        },
    )
    t3_check = state_lib.check_transition(workspace, project_dir, to_stage="outline", package=target)
    refreshed_state = state_lib.load_state(project_dir)
    return {
        "ok": True,
        "command": "prepare",
        "projectId": str(project.get("projectId") or ""),
        "packageId": target,
        "packagesToWrite": packages,
        "packageDir": paths.rel_posix(project_dir, package_dir),
        "packageMetaPath": paths.rel_posix(project_dir, meta_path),
        "createdPackageDir": created_dir,
        "createdDirs": created_dirs,
        "reused": bool(existing_meta is not None),
        "expectedWordCount": meta["expectedWordCount"],
        "figureQuota": meta["figureQuota"],
        "valueSource": meta["valueSource"],
        "qualificationGaps": qualification_gaps,
        "qualificationGapsInfo": {
            "count": len(qualification_gaps),
            "severity": "info",
            "blocking": False,
            "certDataAvailable": cert_available,
            "label": "资格/证书要求提示（商务部分，供用户核对）",
            "note": "资格/证书要求属商务部分，仅供用户核对；info 不阻断 F4/T3，技术方案不据此判定、不要求 Agent 补材料",
        },
        "assumptions": (meta.get("extensions") or {}).get("assumptions") or [],
        "packageMetaBytes": meta_path.stat().st_size,
        "status": "prepared",
        "projectChanged": ["project.json"],
        "state": {
            "revision": refreshed_state.get("revision"),
            "currentStage": refreshed_state.get("currentStage"),
            "currentScope": refreshed_state.get("currentScope"),
            "currentPackage": refreshed_state.get("currentPackage"),
        },
        "setPackage": {
            "revision": set_package_result.get("revision"),
            "changed": set_package_result.get("changed") or [],
            "historyKind": set_package_result.get("historyKind") or "event",
            "note": set_package_result.get("note", ""),
        },
        "t3Check": t3_check,
        "readyForT3": bool(t3_check.get("pass")),
        "autoMode": auto_mode,
        "autoFill": (
            {
                "mode": "auto",
                "personnelBasis": auto_fill.get("personnelBasis"),
                "equipmentBasis": auto_fill.get("equipmentBasis"),
                "tiers": auto_fill.get("tiers"),
                "valueSource": auto_fill.get("valueSource"),
            }
            if auto_fill
            else None
        ),
        "questions": [] if auto_mode else None,
        "changed": ["project.json", "state.json", paths.rel_posix(project_dir, meta_path)],
    }


# ---------------------------------------------------------------------------
# M4.1：requirements / update-answers
# ---------------------------------------------------------------------------
def _filter_cross_package_texts(texts: Mapping[str, str], target: str) -> dict[str, str]:
    """多包 requirements：过滤明显属于其他包的“第N包”行，避免 common 05 串包。"""
    target_no = target.split("-")[-1]
    filtered: dict[str, str] = {}
    for key, text in texts.items():
        lines = []
        for line in str(text).splitlines():
            match = re.search(r"第\s*(\d+)\s*包", line)
            if match and match.group(1) != target_no:
                continue
            lines.append(line)
        filtered[key] = "\n".join(lines)
    return filtered


def requirements_summary(workspace: Path, project_dir: Path, *, package_id: str | None = None, auto: bool = False) -> dict[str, Any]:
    """只读：返回提取到的人员/设备/工期要求摘要 + 7 项提问卡（exportStyleProfile 服务默认，不提问）。"""
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    settings = _read_user_settings(workspace)
    packages = resolve_packages(project)
    target = package_id or str(state.get("currentPackage") or "") or (packages[0] if packages else "package-1")
    if not PACKAGE_ID_RE.match(target):
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"无法确定目标包：{target!r}")
    texts, missing = load_extraction(project_dir, target)
    if missing:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"缺少提取稿 {len(missing)} 个",
            hint="先运行 bg_extract_validate.py 校验/修复提取稿",
            detail={"missing": missing, "packageId": target},
        )
    if int(project.get("packageCount") or 1) > 1:
        texts = _filter_cross_package_texts(texts, target)
    summary = build_requirement_summary(texts, target)
    existing_meta: dict[str, Any] | None = None
    existing_dir = ""
    try:
        package_dir, existing_meta = load_package_meta_for_update(project_dir, target)
        existing_dir = paths.rel_posix(project_dir, package_dir)
    except errors.BgError:
        existing_meta = None
    current_plans: dict[str, Any] = {}
    if existing_meta:
        sources = existing_meta.get("valueSource") or {}
        current_plans = {
            "expectedWordCount": existing_meta.get("expectedWordCount"),
            "personnelPlan": existing_meta.get("personnelPlan"),
            "equipmentPlan": existing_meta.get("equipmentPlan"),
            "schedulePlan": existing_meta.get("schedulePlan"),
            "figureQuota": existing_meta.get("figureQuota"),
            "personnelPlanSource": sources.get("personnelPlan"),
            "equipmentPlanSource": sources.get("equipmentPlan"),
            "status": "prepared" if (existing_meta.get("packageId") and existing_meta.get("expectedWordCount")) else None,
        }
    auto_mode = bool(auto) or str(settings.get("interactionMode") or "") == "auto"
    multi_package = int(project.get("packageCount") or len(packages) or 1) > 1
    auto_fill = build_auto_fill(texts=texts, settings=settings, bidder_summary=load_bidder_summary(workspace, project), package_id=target, multi_package=multi_package) if auto_mode else None
    questions = [] if auto_mode else requirement_questions(summary, existing_meta)
    if questions:
        default_tier = settings.get("defaultWordCountTier") if isinstance(settings, Mapping) else None
        default_words = int(default_tier) if isinstance(default_tier, int) and not isinstance(default_tier, bool) and default_tier > 0 else 50000
        questions[0]["default"] = default_words
        questions[0]["ask"] = f"您希望技术方案写多少字？默认 {default_words} 字（user-settings.defaultWordCountTier），可写其他正整数。"
    auto_values: dict[str, Any] = {}
    if auto_fill:
        values = auto_fill["values"]
        sources = auto_fill["valueSource"]
        bases = auto_fill["autoBasis"]
        auto_values = {
            "expectedWordCount": {"autoValue": values["expectedWordCount"], "autoBasis": bases["expectedWordCount"], "valueSource": sources["expectedWordCount"]},
            "personnelPlan": {"autoValue": values["personnelPlan"], "autoBasis": bases["personnelPlan"], "valueSource": sources["personnelPlan"]},
            "equipmentPlan": {"autoValue": values["equipmentPlan"], "autoBasis": bases["equipmentPlan"], "valueSource": sources["equipmentPlan"]},
            "schedulePlan": {"autoValue": values["schedulePlan"], "autoBasis": bases["schedulePlan"], "valueSource": sources["schedulePlan"]},
            "exportStyleProfile": {"autoValue": values["exportStyleProfile"], "autoBasis": bases["exportStyleProfile"], "valueSource": sources["exportStyleProfile"]},
        }
        for key in FIGURE_KEYS:
            if key == "network":
                continue
            auto_values[f"figureQuota.{key}"] = {
                "autoValue": values["figureQuota"].get(key),
                "autoBasis": bases.get(key, ""),
                "valueSource": sources.get(key, "agent-default"),
            }
    return {
        "ok": True,
        "command": "prepare_requirements",
        "projectId": state.get("projectId"),
        "currentStage": state.get("currentStage"),
        "currentPackage": state.get("currentPackage"),
        "revision": state.get("revision"),
        "packageId": target,
        "packageDir": existing_dir,
        "requirements": summary,
        "currentPlans": current_plans,
        "questions": questions,
        "autoMode": auto_mode,
        "packageConfirmationRequired": True,
        "autoValues": auto_values,
        "autoFill": (
            {
                "mode": "auto",
                "personnelBasis": auto_fill.get("personnelBasis"),
                "equipmentBasis": auto_fill.get("equipmentBasis"),
                "tiers": auto_fill.get("tiers"),
                "valueSource": auto_fill.get("valueSource"),
            }
            if auto_fill
            else None
        ),
        "note": "auto 模式：除需撰写包确认外，7 项提问 + exportStyleProfile 自动预填（人员/设备三档合规描述，valueSource 记录 extracted/agent-default）；"
        "guided 模式：先向用户展示 requirements，再收集用户原话。",
    }


def _backup_meta(meta_path: Path) -> str:
    if not meta_path.is_file():
        return ""
    backup = meta_path.with_name(f"{meta_path.name}.bak-{paths.timestamp14()}")
    try:
        shutil.copy2(meta_path, backup)
    except OSError:
        return ""
    return paths.rel_posix(meta_path.parents[2], backup)


def record_override(
    workspace: Path,
    project_dir: Path,
    *,
    package_id: str | None,
    field: str,
    value: str,
    note: str = "",
) -> dict[str, Any]:
    """人工改写 package-meta 单字段：备份、valueSource=user、发 package.value_override 事件。"""
    allowed = {"personnelPlan", "equipmentPlan", "expectedWordCount", "schedulePlan", "figureQuota", "exportStyleProfile"}
    if field not in allowed:
        raise errors.BgError(errors.E_USAGE, f"不允许的 field：{field}", hint=f"可用：{sorted(allowed)}")
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    target = package_id or str(state.get("currentPackage") or "")
    if not target:
        raise errors.BgError(errors.E_GUARD_FAILED, "未指定 package，且 state.currentPackage 为空")
    package_dir, meta = load_package_meta_for_update(project_dir, target)
    meta_path = package_dir / "package-meta.json"
    old_value = meta.get(field)
    parsed: Any = value
    if field in ("expectedWordCount",):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise errors.BgError(errors.E_USAGE, "expectedWordCount 必须是整数") from exc
    elif field in ("schedulePlan", "figureQuota"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise errors.BgError(errors.E_USAGE, f"{field} 必须是 JSON 对象") from exc
        if field == "schedulePlan":
            # P2-9：统一 shape（servicePeriod 与 constructionPlan 分开），缺省键补空串
            normalized = normalize_answers({"schedulePlan": parsed}, allow_network_images=False).get("schedulePlan")
            if not isinstance(normalized, Mapping):
                raise errors.BgError(errors.E_USAGE, "schedulePlan 必须是 JSON 对象（startDate/deliveryDate/servicePeriod/constructionPlan）")
            parsed = dict(normalized)
    candidate = dict(meta)
    backup_path = _backup_meta(meta_path)
    candidate[field] = parsed
    value_source = dict(candidate.get("valueSource") or {})
    value_source[field] = "user"
    candidate["valueSource"] = value_source
    candidate["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(meta_path, _slim_meta(candidate))
    event_log.log_event(
        workspace,
        "package.value_override",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=target,
        detail={"field": field, "oldValue": old_value, "newValue": parsed, "note": note, "backupPath": backup_path, "source": "manual"},
    )
    return {
        "ok": True,
        "command": "prepare_record_override",
        "projectId": str(project.get("projectId") or ""),
        "packageId": target,
        "field": field,
        "oldValue": old_value,
        "newValue": parsed,
        "valueSource": value_source.get(field),
        "backupPath": backup_path,
        "event": "package.value_override",
    }


def update_answers(
    workspace: Path,
    project_dir: Path,
    *,
    answers_path: Path,
    package_id: str | None = None,
) -> dict[str, Any]:
    """M4.1：只更新已有 package-meta 的答案字段（不迁移状态、不重建包，幂等）。"""
    if answers_path is None or not answers_path.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少 --update-answers 的 answers JSON 文件",
            hint="传入用户回答 JSON（personnelPlan/equipmentPlan 可为原话字符串）",
            detail={"path": str(answers_path) if answers_path else ""},
        )
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    stage = str(state.get("currentStage") or "")
    if stage in ("project_init", "info_extract"):
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"当前阶段（{stage}）尚未完成撰写前准备，不能更新 answers",
            hint="先执行 bg_prepare.py 创建包目录/package-meta",
            detail={"currentStage": stage, "allowedNext": state_lib.allowed_next(state, project)},
        )
    if stage == "aborted":
        raise errors.BgError(errors.E_GUARD_FAILED, "已中止的项目不能更新 answers", hint="先 resume 恢复项目")
    settings = _read_user_settings(workspace)
    packages = resolve_packages(project)
    target = package_id or str(state.get("currentPackage") or "") or (packages[0] if packages else "package-1")
    package_dir, meta = load_package_meta_for_update(project_dir, target)
    meta_path = package_dir / "package-meta.json"
    document = jsonio.read_json(answers_path)
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_USAGE, "answers 必须是 JSON 对象", detail={"path": str(answers_path)})
    raw_answers = document.get("answers") if isinstance(document.get("answers"), Mapping) else document

    texts, _missing = load_extraction(project_dir, target)
    allow_network = bool(settings.get("allowNetworkImages"))
    answers = normalize_answers(raw_answers, allow_network_images=allow_network)
    base_slim = _slim_meta(meta)
    legacy_removed = sorted(set(meta.keys()) - set(base_slim.keys()))
    candidate = dict(base_slim)
    changed: list[str] = []
    value_source = dict(base_slim.get("valueSource") or {})
    for key in state_lib.PACKAGE_VALUE_SOURCE_KEYS:
        value_source.setdefault(key, "agent-default")

    if "expectedWordCount" in answers and answers["expectedWordCount"] != meta.get("expectedWordCount"):
        candidate["expectedWordCount"] = answers["expectedWordCount"]
        value_source["expectedWordCount"] = "user"
        changed.append("expectedWordCount")
    for field in ("personnelPlan", "equipmentPlan"):
        if field in answers and answers[field] != base_slim.get(field):
            candidate[field] = answers[field]
            value_source[field] = "user"
            changed.append(field)
    raw_schedule = raw_answers.get("schedulePlan") if isinstance(raw_answers, Mapping) else None
    if isinstance(raw_schedule, Mapping) and "schedulePlan" in answers:
        merged_schedule = dict(base_slim.get("schedulePlan") or {})
        merged_schedule.setdefault("servicePeriod", "")
        for key in ("startDate", "deliveryDate", "constructionPlan", "servicePeriod"):
            if key in raw_schedule:
                merged_schedule[key] = str(answers["schedulePlan"].get(key) or "")
        if merged_schedule != base_slim.get("schedulePlan"):
            candidate["schedulePlan"] = merged_schedule
            value_source["schedulePlan"] = "user"
            changed.append("schedulePlan")
    raw_quota = raw_answers.get("figureQuota") if isinstance(raw_answers, Mapping) else None
    if isinstance(raw_quota, Mapping) and "figureQuota" in answers:
        merged_quota = dict(base_slim.get("figureQuota") or DEFAULT_FIGURE_QUOTA)
        for key in FIGURE_KEYS:
            if key in raw_quota:
                merged_quota[key] = int(answers["figureQuota"].get(key, merged_quota.get(key, 0)))
                value_source[key] = "user"
        if merged_quota != base_slim.get("figureQuota"):
            candidate["figureQuota"] = merged_quota
            changed.append("figureQuota")
    if "exportStyleProfile" in answers:
        style = str(answers["exportStyleProfile"])
        tech_styles = _style_ids("tech") or ["official-tech"]
        if style not in tech_styles:
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"exportStyleProfile 必须是可用 tech 样式：{tech_styles}",
                detail={"value": style, "available": tech_styles},
            )
        if style != base_slim.get("exportStyleProfile"):
            candidate["exportStyleProfile"] = style
            changed.append("exportStyleProfile")
    candidate["extensions"] = dict(base_slim.get("extensions") or {})
    if value_source != base_slim.get("valueSource"):
        candidate["valueSource"] = value_source
        if "valueSource" not in changed:
            changed.append("valueSource")
    # 若 answers 同一值重复提交 → changed 为空，不写盘、不发事件（幂等）
    if not changed and not legacy_removed:
        return {
            "ok": True,
            "command": "prepare_update_answers",
            "idempotent": True,
            "updated": False,
            "changed": [],
            "removedLegacyFields": [],
            "projectId": state.get("projectId"),
            "packageId": target,
            "packageDir": paths.rel_posix(project_dir, package_dir),
            "packageMetaPath": paths.rel_posix(project_dir, meta_path),
            "state": {
                "revision": state.get("revision"),
                "currentStage": state.get("currentStage"),
                "currentScope": state.get("currentScope"),
                "currentPackage": state.get("currentPackage"),
            },
            "personnelPlan": candidate.get("personnelPlan"),
            "equipmentPlan": candidate.get("equipmentPlan"),
        }
    now = paths.iso_now()
    backup_path = _backup_meta(meta_path)
    candidate["updatedAt"] = now
    candidate = _slim_meta(candidate)
    jsonio.write_json_atomic(meta_path, candidate)
    event_log.log_event(
        workspace,
        "package.answers_updated",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=target,
        detail={
            "fields": sorted(set(changed)),
            "personnelPlanSource": (candidate.get("valueSource") or {}).get("personnelPlan"),
            "equipmentPlanSource": (candidate.get("valueSource") or {}).get("equipmentPlan"),
            "stage": state.get("currentStage"),
        },
    )
    refreshed_state = state_lib.load_state(project_dir)
    return {
        "ok": True,
        "command": "prepare_update_answers",
        "idempotent": False,
        "updated": True,
        "changed": sorted(set(changed)),
        "removedLegacyFields": legacy_removed,
        "projectId": state.get("projectId"),
        "packageId": target,
        "packageDir": paths.rel_posix(project_dir, package_dir),
        "packageMetaPath": paths.rel_posix(project_dir, meta_path),
        "personnelPlan": candidate.get("personnelPlan"),
        "equipmentPlan": candidate.get("equipmentPlan"),
        "personnelPlanSource": (candidate.get("valueSource") or {}).get("personnelPlan"),
        "equipmentPlanSource": (candidate.get("valueSource") or {}).get("equipmentPlan"),
        "updatedAt": now,
        "state": {
            "revision": refreshed_state.get("revision"),
            "currentStage": refreshed_state.get("currentStage"),
            "currentScope": refreshed_state.get("currentScope"),
            "currentPackage": refreshed_state.get("currentPackage"),
        },
        "note": "只更新 package-meta 答案字段；state.json/包目录未变（M4.3：旧字段会被顺带瘦身）",
    }


# ---------------------------------------------------------------------------
# M4.3：materials-index / --compact
# ---------------------------------------------------------------------------
def materials_index(
    workspace: Path,
    project_dir: Path,
    *,
    package_id: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """按需生成材料索引（只 id/type/title/source/section/score，不含 detail；不写 package-meta）。"""
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    packages = resolve_packages(project)
    target = package_id or str(state.get("currentPackage") or "") or (packages[0] if packages else "package-1")
    texts, missing = load_extraction(project_dir, target)
    if missing:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"缺少提取稿 {len(missing)} 个",
            hint="先运行 bg_extract_validate.py 校验/修复提取稿",
            detail={"missing": missing, "packageId": target},
        )
    materials = build_must_save_materials(project_dir, target, texts)
    items: list[dict[str, Any]] = []
    for material in materials:
        item: dict[str, Any] = {
            "id": material.get("id"),
            "type": material.get("type"),
            "title": material.get("title"),
            "source": material.get("source"),
            "section": material.get("section"),
        }
        if "score" in material:
            item["score"] = material["score"]
        items.append(item)
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "packageId": target,
        "generatedAt": paths.iso_now(),
        "items": items,
    }
    written_path = ""
    if write:
        package_dir, _meta = load_package_meta_for_update(project_dir, target)
        index_path = package_dir / "materials-index.json"
        jsonio.write_json_atomic(index_path, document)
        written_path = paths.rel_posix(project_dir, index_path)
    return {
        "ok": True,
        "command": "prepare_materials",
        "projectId": state.get("projectId"),
        "currentStage": state.get("currentStage"),
        "packageId": target,
        "count": len(items),
        "items": items,
        "materialsIndexPath": written_path,
        "written": bool(write),
        "note": "材料索引按需生成，仅含 id/type/title/source/section/score；提取内容不复制进 package-meta。",
    }


def compact_meta(workspace: Path, project_dir: Path, *, package_id: str | None = None) -> dict[str, Any]:
    """M4.3：把旧 package-meta 迁移为 PRD 7.3 瘦字段（幂等，不改 state/包目录）。"""
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    packages = resolve_packages(project)
    target = package_id or str(state.get("currentPackage") or "") or (packages[0] if packages else "package-1")
    package_dir, meta = load_package_meta_for_update(project_dir, target)
    meta_path = package_dir / "package-meta.json"
    before_bytes = meta_path.stat().st_size
    settings_migration = _strip_legacy_user_settings(workspace)
    slim = _slim_meta(meta)
    missing_keys = [key for key in PRD_META_KEYS if key not in slim]
    if missing_keys:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"package-meta 缺少 PRD 7.3 字段，不能 compact：{missing_keys}",
            hint="检查 package-meta.json 完整性（缺失字段需重跑 prepare）",
            detail={"missingKeys": missing_keys, "path": paths.rel_posix(project_dir, meta_path)},
        )
    issues = state_lib._package_meta_issues(slim, target)  # noqa: SLF001 - 与 T3 guard 同语义
    if issues:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "package-meta 字段类型不合法，不能 compact",
            hint="先修复 package-meta 字段类型",
            detail={"issues": issues, "path": paths.rel_posix(project_dir, meta_path)},
        )
    old_extensions = meta.get("extensions") if isinstance(meta.get("extensions"), Mapping) else {}
    removed_top = sorted(key for key in meta if key not in slim)
    removed_extensions = sorted(key for key in old_extensions if key not in (slim.get("extensions") or {}))
    if slim == meta or (not removed_top and not removed_extensions and slim == meta):
        return {
            "ok": True,
            "command": "prepare_compact",
            "idempotent": not settings_migration["changed"],
            "updated": bool(settings_migration["changed"]),
            "changed": ["user-settings.json"] if settings_migration["changed"] else [],
            "removedTopFields": [],
            "removedExtensionKeys": [],
            "settingsMigration": settings_migration,
            "beforeBytes": before_bytes,
            "afterBytes": before_bytes,
            "packageId": target,
            "packageDir": paths.rel_posix(project_dir, package_dir),
            "packageMetaPath": paths.rel_posix(project_dir, meta_path),
            "state": {"revision": state.get("revision"), "currentStage": state.get("currentStage")},
        }
    slim["updatedAt"] = meta.get("updatedAt") or paths.iso_now()
    jsonio.write_json_atomic(meta_path, slim)
    after_bytes = meta_path.stat().st_size
    event_log.log_event(
        workspace,
        "package.meta_compacted",
        actor="script",
        project=str(project.get("projectId") or ""),
        package=target,
        detail={
            "beforeBytes": before_bytes,
            "afterBytes": after_bytes,
            "removedTopFields": removed_top,
            "removedExtensionKeys": removed_extensions,
            "settingsRemoved": settings_migration.get("removed") or [],
        },
    )
    refreshed = state_lib.load_state(project_dir)
    return {
        "ok": True,
        "command": "prepare_compact",
        "idempotent": False,
        "updated": True,
        "changed": ["package-meta.json"],
        "removedTopFields": removed_top,
        "removedExtensionKeys": removed_extensions,
        "settingsMigration": settings_migration,
        "beforeBytes": before_bytes,
        "afterBytes": after_bytes,
        "packageId": target,
        "packageDir": paths.rel_posix(project_dir, package_dir),
        "packageMetaPath": paths.rel_posix(project_dir, meta_path),
        "state": {
            "revision": refreshed.get("revision"),
            "currentStage": refreshed.get("currentStage"),
            "currentScope": refreshed.get("currentScope"),
            "currentPackage": refreshed.get("currentPackage"),
        },
    }


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def _build_selftest_project(base: Path) -> tuple[Path, Path]:
    """构造临时 workspace/project（package_prepare 状态 + v2 提取稿）。"""
    import bg_extract_validate as extract_validate

    workspace = base / "ws"
    (workspace / "dsh" / "logs").mkdir(parents=True)
    (workspace / "dsh" / "config").mkdir(parents=True)
    (workspace / "dsh" / "bidders").mkdir(parents=True)
    project_dir = workspace / "示例项目-20261111100000-abcd"
    project_dir.mkdir(parents=True)
    now = paths.iso_now()
    bidder_ref = {
        "bidderId": "bdr-20261111100000-abcd",
        "dirName": "示例投标人-20261111100000-abcd",
        "displayName": "示例投标人",
        "snapshotAt": now,
    }
    project = schema.initialize_project_document(
        project_id="prj-20261111100000-abcd",
        bidder_ref=bidder_ref,
        workspace_fingerprint=paths.workspace_fingerprint(workspace),
        now_iso=now,
        project_name="M4 撰写前准备自检项目",
    )
    project["projectNo"] = "M4-TEST-001"
    project["isSubcontracted"] = False
    project["packageCount"] = 1
    project["packagesToWrite"] = ["package-1"]
    jsonio.write_json_atomic(project_dir / "project.json", project)
    state = schema.initialize_state_document(project_id=project["projectId"], now_iso=now)
    state["currentStage"] = "package_prepare"
    state["currentScope"] = "package-1"
    state["currentPackage"] = "package-1"
    state["completedStages"] = [
        {"stage": "project_init", "scope": "project", "completedAt": now, "note": "示例"},
        {"stage": "info_extract", "scope": "project", "completedAt": now, "note": "示例"},
    ]
    state["revision"] = 3
    state["updatedAt"] = now
    state["updatedBy"] = "script:bg_state.py"
    state_lib.write_state(project_dir, state)
    extract_validate.build_selftest_extractions(project_dir, package_count=1)
    jsonio.write_json_atomic(
        paths.user_settings_path(workspace),
        {
            "schemaVersion": "1.0",
            "currentBidderId": bidder_ref["bidderId"],
            "defaultWordCountTier": 50000,
            "defaultFigureQuota": {"table": 5, "mermaid": 5, "network": 5, "terminal": 5},
            "allowNetworkImages": False,
            "userInputTimeoutSec": 600,
            "styleProfiles": {"tech": "official-tech", "keyInfo": "official-keyinfo"},
            "updatedAt": now,
        },
    )
    return workspace, project_dir


def run_selftest() -> dict[str, Any]:
    """脚本自检：prepare 幂等/目录/PRD 瘦 meta/T3 预检/requirements/update-answers/materials/compact/非法 answers。"""
    import platform
    import shutil
    import tempfile

    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-prepare-selftest-"))
    try:
        workspace, project_dir = _build_selftest_project(tmp)
        result = prepare_project(workspace, project_dir)
        if not result.get("packageDir") or not (project_dir / result["packageDir"]).is_dir():
            raise AssertionError("包目录未创建")
        checks.append("prepare")
        meta = jsonio.read_json(project_dir / result["packageMetaPath"])
        for key in PRD_META_KEYS:
            if key not in meta:
                raise AssertionError(f"package-meta 缺少 PRD 字段：{key}")
        for legacy in LEGACY_META_FIELDS:
            if legacy in meta:
                raise AssertionError(f"package-meta 不应再包含旧字段：{legacy}")
        if meta.get("expectedWordCount") != 50000 or meta.get("figureQuota", {}).get("network") != 0:
            raise AssertionError(f"默认值推导不正确：word={meta.get('expectedWordCount')} quota={meta.get('figureQuota')}")
        # P2-9：schedulePlan 必须含 servicePeriod（合同履行期限语义），无期限时留空且不把承诺写进 constructionPlan
        schedule_meta = meta.get("schedulePlan") if isinstance(meta.get("schedulePlan"), Mapping) else {}
        if not isinstance(schedule_meta.get("servicePeriod"), str):
            raise AssertionError(f"schedulePlan.servicePeriod 缺失/非字符串：{schedule_meta}")
        if schedule_meta.get("constructionPlan"):
            raise AssertionError(f"无工期要求时 constructionPlan 不应有值：{schedule_meta}")
        if not isinstance((meta.get("extensions") or {}).get("assumptions"), list):
            raise AssertionError("assumptions 应放入 extensions")
        if "allowNetworkImages" in (meta.get("extensions") or {}):
            raise AssertionError("extensions.allowNetworkImages 已废弃，不应写入")
        if (project_dir / result["packageMetaPath"]).stat().st_size > 8000:
            raise AssertionError(f"package-meta 未瘦身：{(project_dir / result['packageMetaPath']).stat().st_size} B")
        checks.append("package_meta")
        # P2-1：合同履行期限「10 个日历日内」要能推导 servicePeriod（旧正则只认 10 天/10 日历天）
        schedule_text = (
            "| 事项 | 内容 |\n|---|---|\n| 合同履行期限 | 合同签订后 10 个日历日内完成供货及安装 |\n"
        )
        derived_plan, derived = derive_schedule_plan({"01": schedule_text})
        if not derived or "10 个日历日" not in str(derived_plan.get("servicePeriod") or ""):
            raise AssertionError(f"合同履行期限未推导：{derived_plan} derived={derived}")
        # P2-2：产品执行标准（GB/T…并通过认证）不是投标人资格缺口
        qualification_clauses = _qualification_clauses(
            "投标人应具备公路交通工程专业承包（公路安全设施分项）二级及以上资质；"
            "产品应符合 GB/T 24972-2010《弹性交通柱》标准并通过认证"
        )
        if len(qualification_clauses) != 1 or "资质" not in qualification_clauses[0][0]:
            raise AssertionError(f"资格缺口过滤不正确：{qualification_clauses}")
        checks.append("schedule_and_qualification_filter")
        package_dir = project_dir / result["packageDir"]
        for rel in REQUIRED_PACKAGE_DIRS:
            if not (package_dir / rel).is_dir():
                raise AssertionError(f"缺少目录：{rel}")
        checks.append("package_dirs")
        if not result.get("readyForT3"):
            raise AssertionError(f"T3 预检未通过：{result.get('t3Check')}")
        checks.append("t3_ready")
        state = state_lib.load_state(project_dir)
        if state.get("currentStage") != "package_prepare" or state.get("currentPackage") != "package-1":
            raise AssertionError(f"set-package 后状态不正确：{state.get('currentStage')}/{state.get('currentPackage')}")
        revision_after_first = state.get("revision")
        second = prepare_project(workspace, project_dir)
        state_second = state_lib.load_state(project_dir)
        package_dirs = [entry.name for entry in (project_dir / "packages").iterdir() if entry.is_dir()]
        if len(package_dirs) != 1 or not second.get("reused"):
            raise AssertionError(f"幂等失败：dirs={package_dirs} reused={second.get('reused')}")
        if state_second.get("revision") != revision_after_first:
            raise AssertionError("幂等重跑不应增加 state revision")
        checks.append("idempotent")
        # M4.1：requirements（提取要求摘要 + 7 项提问卡）
        requirements = requirements_summary(workspace, project_dir)
        summary = requirements.get("requirements") or {}
        if not all(key in summary for key in ("personnel", "equipment", "schedule", "wordCount")):
            raise AssertionError(f"requirements 缺少字段：{sorted(summary.keys())}")
        question_list = requirements.get("questions") or []
        if len(question_list) != 7:
            raise AssertionError("requirements 提问卡不是 7 项")
        plan_questions = {item.get("id"): item for item in question_list}
        for question_id in ("personnelPlan", "equipmentPlan"):
            question = plan_questions.get(question_id) or {}
            if question.get("inputType") != "text":
                raise AssertionError(f"{question_id} 缺少 inputType=text：{question}")
            if "options" in question or "choices" in question:
                raise AssertionError(f"{question_id} 不应带 options/choices：{question}")
            if "一句话" not in str(question.get("ask") or ""):
                raise AssertionError(f"{question_id} ask 文案应含「一句话」：{question.get('ask')}")
        checks.append("requirements")
        # M4.1：纯文本人员/设备 answers + update-answers 幂等（T3 前）
        update_file = tmp / "update-answers.json"
        jsonio.write_json_atomic(
            update_file,
            {
                "personnelPlan": "我计划投入项目经理 1 名、驻场工程师 3 名，均有相关证书。",
                "equipmentPlan": "计划投入巡检车 2 辆、笔记本电脑 5 台。",
            },
        )
        revision_before_update = int(state_lib.load_state(project_dir).get("revision") or 0)
        updated_payload = update_answers(workspace, project_dir, answers_path=update_file)
        meta_after = jsonio.read_json(package_dir / "package-meta.json")
        if meta_after.get("personnelPlan") != "我计划投入项目经理 1 名、驻场工程师 3 名，均有相关证书。":
            raise AssertionError(f"personnelPlan 原话未原样保存：{meta_after.get('personnelPlan')!r}")
        if meta_after.get("equipmentPlan") != "计划投入巡检车 2 辆、笔记本电脑 5 台。":
            raise AssertionError(f"equipmentPlan 原话未原样保存：{meta_after.get('equipmentPlan')!r}")
        if (meta_after.get("valueSource") or {}).get("personnelPlan") != "user" or (
            meta_after.get("valueSource") or {}
        ).get("equipmentPlan") != "user":
            raise AssertionError(f"计划来源不正确：{meta_after.get('valueSource')}")
        for legacy in LEGACY_META_FIELDS:
            if legacy in meta_after:
                raise AssertionError(f"update-answers 不应写旧字段：{legacy}")
        if int(state_lib.load_state(project_dir).get("revision") or 0) != revision_before_update:
            raise AssertionError("update-answers 不应修改 state revision")
        if not updated_payload.get("changed"):
            raise AssertionError("update-answers 未报告变更字段")
        second_update = update_answers(workspace, project_dir, answers_path=update_file)
        if second_update.get("changed") or not second_update.get("idempotent"):
            raise AssertionError(f"update-answers 幂等失败：{second_update.get('changed')}")
        checks.append("update_answers")
        checks.append("update_answers_idempotent")
        bad_update = tmp / "update-answers-bad.json"
        jsonio.write_json_atomic(bad_update, {"personnelPlan": 123})
        try:
            update_answers(workspace, project_dir, answers_path=bad_update)
        except errors.BgError as exc:
            if exc.code != errors.E_SCHEMA_INVALID:
                raise AssertionError(f"非法 plan 错误码不正确：{exc.code}") from exc
        else:
            raise AssertionError("非法 plan 未被拒绝")
        checks.append("update_answers_invalid")
        # M4.3：materials 按需索引（不复制 detail 进 package-meta）
        material_index = materials_index(workspace, project_dir)
        if material_index.get("count", 0) <= 0 or not material_index.get("items"):
            raise AssertionError("materials 索引为空")
        allowed_keys = {"id", "type", "title", "source", "section", "score"}
        if any(not set(item) <= allowed_keys for item in material_index["items"]):
            raise AssertionError("materials 索引含 detail 等不允许字段")
        meta_bytes_before_index = (package_dir / "package-meta.json").stat().st_size
        written_index = materials_index(workspace, project_dir, write=True)
        index_path = project_dir / written_index["materialsIndexPath"]
        if not index_path.is_file():
            raise AssertionError("materials-index.json 未写入")
        if (package_dir / "package-meta.json").stat().st_size != meta_bytes_before_index:
            raise AssertionError("materials 索引不应修改 package-meta")
        checks.append("materials_index")
        # M4.3：--compact 旧 meta 迁移（幂等、不改 state/目录）
        legacy_meta = dict(meta_after)
        legacy_meta["mustSaveMaterials"] = [{"id": "MS-1", "type": "x", "title": "t", "detail": "d"}]
        legacy_meta["answers"] = {"expectedWordCount": 50000}
        legacy_meta["assumptions"] = ["旧 assumption"]
        legacy_meta["personnelPlanSource"] = "user"
        legacy_meta["equipmentPlanSource"] = "user"
        legacy_meta["extractedRequirementSummary"] = {"personnel": ["x"]}
        legacy_meta["status"] = "prepared"
        legacy_meta["extensions"] = dict(
            (meta_after.get("extensions") or {}), keyChapters=[1, 2], allowNetworkImages=False
        )
        jsonio.write_json_atomic(package_dir / "package-meta.json", legacy_meta)
        legacy_bytes = (package_dir / "package-meta.json").stat().st_size
        revision_before_compact = int(state_lib.load_state(project_dir).get("revision") or 0)
        dirs_before_compact = sorted(entry.name for entry in (project_dir / "packages").iterdir() if entry.is_dir())
        compacted = compact_meta(workspace, project_dir)
        meta_compacted = jsonio.read_json(package_dir / "package-meta.json")
        if compacted.get("updated") is not True or not compacted.get("removedTopFields"):
            raise AssertionError(f"compact 未报告删除字段：{compacted}")
        for legacy in LEGACY_META_FIELDS:
            if legacy in meta_compacted:
                raise AssertionError(f"compact 后仍含旧字段：{legacy}")
        if int(state_lib.load_state(project_dir).get("revision") or 0) != revision_before_compact:
            raise AssertionError("compact 不应修改 state revision")
        if sorted(entry.name for entry in (project_dir / "packages").iterdir() if entry.is_dir()) != dirs_before_compact:
            raise AssertionError("compact 不应修改包目录")
        if compacted.get("afterBytes", 0) >= legacy_bytes:
            raise AssertionError(f"compact 未减小体积：{legacy_bytes} -> {compacted.get('afterBytes')}")
        if (meta_compacted.get("extensions") or {}).get("assumptions") != ["旧 assumption"]:
            raise AssertionError("compact 应把 assumptions 保留进 extensions")
        if "allowNetworkImages" in (meta_compacted.get("extensions") or {}):
            raise AssertionError("compact 应删除 extensions.allowNetworkImages")
        migrated_settings = jsonio.read_json(paths.user_settings_path(workspace))
        if "userInputTimeoutSec" in migrated_settings:
            raise AssertionError("compact 应迁移剥离 user-settings.userInputTimeoutSec")
        if migrated_settings.get("allowNetworkImages") is not False:
            raise AssertionError("compact 不应修改其他 user-settings 字段")
        compacted_again = compact_meta(workspace, project_dir)
        if not compacted_again.get("idempotent"):
            raise AssertionError("compact 幂等失败")
        checks.append("compact")
        t3 = state_lib.check_transition(workspace, project_dir, to_stage="outline", package="package-1")
        if not t3.get("pass"):
            raise AssertionError(f"T3 guard 未通过：{t3.get('guard')}")
        checks.append("t3_guard")
        # 非法 answers
        for bad in (
            {"expectedWordCount": 0},
            {"figureQuota": {"table": -1}},
            {"schedulePlan": {"startDate": "2026/01/01"}},
            {"expectedWordCount": "5万"},
        ):
            try:
                prepare_project(workspace, project_dir, answers_document=bad)
            except errors.BgError as exc:
                if exc.code != errors.E_SCHEMA_INVALID:
                    raise AssertionError(f"非法 answers 错误码不正确：{exc.code}") from exc
            else:
                raise AssertionError(f"非法 answers 未被拒绝：{bad}")
        checks.append("invalid_answers")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_prepare.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_prepare.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（F4 / H.1）。"""
    parser = errors.CliParser(
        prog="bg_prepare.py",
        description="BidGenie F4 撰写前准备：包目录/package-meta/必存清单 + T3 前置校验",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>（缺省 state.currentPackage / packagesToWrite 首个）")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("requirements", "materials", "record-override"),
        default=None,
        help="子命令：requirements=提取要求摘要；materials=材料索引；record-override=人工改写单字段（需 --field/--value）",
    )
    parser.add_argument("--answers", help="8 项问题答案 JSON 文件（可选；缺省按默认值推导）")
    parser.add_argument("--auto", action="store_true", help="auto 模式：除需撰写包确认外，8 项自动预填（也读取 settings.interactionMode=auto）")
    parser.add_argument("--all-selected", action="store_true", help="按 writingPackages 顺序逐个 prepare 全部选中包（再执行 T3）")
    parser.add_argument("--update-answers", dest="update_answers", help="M4.1：项目已在 outline 时只更新 package-meta 答案字段（JSON 文件）")
    parser.add_argument("--field", help="record-override：personnelPlan/equipmentPlan/expectedWordCount/schedulePlan(含 servicePeriod/constructionPlan)/figureQuota/exportStyleProfile")
    parser.add_argument("--value", help="record-override：新值（schedulePlan/figureQuota 用 JSON）")
    parser.add_argument("--note", help="record-override：改写来源/原因备注")
    parser.add_argument("--write", action="store_true", help="materials 子命令：把材料索引写入 <packageDir>/materials-index.json")
    parser.add_argument("--compact", action="store_true", help="M4.3：把旧 package-meta 迁移为 PRD 7.3 瘦字段（幂等，不改 state/目录）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="只读预检（含 T3 readiness），供 Agent 调用")
    action.add_argument("--verify", action="store_true", help="只读校验已生成的包目录/package-meta（F4.3 文件存在性）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if getattr(args, "check", False):
        return check_ready(workspace, project_dir, package_id=getattr(args, "package", None))
    if getattr(args, "verify", False):
        return verify_prepared(workspace, project_dir, package_id=getattr(args, "package", None))
    if getattr(args, "compact", False):
        return compact_meta(workspace, project_dir, package_id=getattr(args, "package", None))
    if getattr(args, "update_answers", None):
        update_path = Path(str(args.update_answers)).expanduser().resolve()
        return update_answers(
            workspace,
            project_dir,
            answers_path=update_path,
            package_id=getattr(args, "package", None),
        )
    if getattr(args, "action", None) == "record-override":
        if not args.field or args.value is None:
            raise errors.BgError(errors.E_USAGE, "record-override 需要 --field 和 --value")
        return record_override(workspace, project_dir, package_id=getattr(args, "package", None), field=str(args.field), value=str(args.value), note=str(getattr(args, "note", "") or ""))
    if getattr(args, "action", None) == "requirements":
        return requirements_summary(workspace, project_dir, package_id=getattr(args, "package", None), auto=bool(getattr(args, "auto", False)))
    if getattr(args, "action", None) == "materials":
        return materials_index(
            workspace,
            project_dir,
            package_id=getattr(args, "package", None),
            write=bool(getattr(args, "write", False)),
        )
    answers_path = Path(str(args.answers)).expanduser().resolve() if getattr(args, "answers", None) else None
    if getattr(args, "all_selected", False):
        project = state_lib.load_project(project_dir)
        packages = resolve_packages(project)
        results: list[dict[str, Any]] = []
        pending_packages: list[str] = []
        for index, package_id in enumerate(packages):
            result = prepare_project(
                workspace,
                project_dir,
                package_id=package_id,
                answers_path=answers_path,
                auto=bool(getattr(args, "auto", False)),
                switch_package=(index == 0),
            )
            # P2-7：--all-selected 中间包 readyForT3=false 是正常顺序中间态（后续包尚未 prepare），
            # 显式标注 pendingReason，避免 Agent/用户误判 F4 失败；但只有“失败项都由其它包未 prepare
            # 引起”时才这样标注，本包自身仍有失败项时保持原样要求处理。
            t3_check = result.get("t3Check") if isinstance(result.get("t3Check"), Mapping) else {}
            t3_guard = t3_check.get("guard") if isinstance(t3_check.get("guard"), Mapping) else {}
            other_packages = set(packages) - {package_id}
            only_sequence_reason = True
            failed_checks = [
                check
                for check in (t3_guard.get("checks") or [])
                if isinstance(check, Mapping) and check.get("pass") is False and check.get("id") != "pending_input"
            ]
            for check in failed_checks:
                check_id = str(check.get("id") or "")
                detail = check.get("detail") if isinstance(check.get("detail"), Mapping) else {}
                if check_id == "all_selected_prepared":
                    failing_packages = set(detail.get("missingPackages") or [])
                elif check_id == "package_tree":
                    # package_tree.detail 列全部包；只取真正缺目录/目录为空的包
                    failing_packages = {
                        key
                        for key, value in (detail.get("packages") or {}).items()
                        if isinstance(value, Mapping) and (not value.get("packageDir") or value.get("missingDirs"))
                    }
                elif check_id == "package_meta":
                    failing_packages = set((detail.get("packages") or {}).keys())
                else:
                    only_sequence_reason = False
                    break
                if not failing_packages or not failing_packages <= other_packages:
                    only_sequence_reason = False
                    break
            only_sequence_reason = bool(failed_checks) and only_sequence_reason
            if index < len(packages) - 1 and not result.get("readyForT3") and only_sequence_reason:
                result["pendingReason"] = "sequence-in-progress"
                result["t3Pending"] = True
                result["note"] = "中间包 readyForT3=false 属正常顺序中间态（后续包尚未 prepare）；以汇总/最后一包 readyForT3 为准"
                pending_packages.append(package_id)
            results.append(result)
        if packages:
            state_lib.set_package(workspace, project_dir, package=packages[0])
        state_after = state_lib.load_state(project_dir)
        last = results[-1] if results else {}
        ready = bool(results) and bool(last.get("readyForT3"))
        return {
            "ok": ready,
            "command": "prepare_all_selected",
            "projectId": state_after.get("projectId"),
            "packagesToWrite": packages,
            "prepared": [item.get("packageId") for item in results],
            "readyForT3": bool(last.get("readyForT3")),
            "pendingPackages": pending_packages,
            "summary": {
                "readyForT3": bool(last.get("readyForT3")),
                "finalPackage": last.get("packageId"),
                "pendingReason": "sequence-in-progress" if pending_packages else "",
                "note": (
                    "中间包 readyForT3=false 为顺序中间态（后续包未 prepare），非失败；最终以 readyForT3 为准"
                    if pending_packages
                    else "全部选中包 prepare 完成，最终 readyForT3 以最后一包为准"
                ),
                "historyNote": "批量准备只在首包切换一次 currentPackage；中间包不再写 set-package 事件，历史里的切包记录不代表来回切换。",
            },
            "t3Check": last.get("t3Check"),
            "results": results,
            "state": {"revision": state_after.get("revision"), "currentStage": state_after.get("currentStage"), "currentPackage": state_after.get("currentPackage")},
        }
    result = prepare_project(workspace, project_dir, package_id=getattr(args, "package", None), answers_path=answers_path, auto=bool(getattr(args, "auto", False)))
    if not result.get("readyForT3"):
        # 准备本身成功，但 T3 guard 未通过（如多包只准备了一个）→ 不抛错，交由 Agent 继续/补齐
        result["warning"] = "T3 guard 尚未通过：按 t3Check.guard.checks 补齐（多包需逐个 prepare）"
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_prepare.py",
    )


if __name__ == "__main__":
    sys.exit(main())
