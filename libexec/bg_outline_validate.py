#!/usr/bin/env python3
"""BidGenie F5-1 大纲校验（PRD F5.1.2–F5.1.9 / 7.4 / 4.3 T4）。

职责（只读）：
- 读取 `<packageDir>/outline.json`（或 Agent 草稿 `outline.draft.json` / `--input`）；
- 校验 schema、层级（parent≤4/leaf≤5、level=路径深度、id=路径）、结构、评审覆盖、
  contentRequirements、字数（≤±1%）、图表配额/anchorId、依赖（无环且 seq 更小）、路径；
- `--check-tree` 校验 `chapters/**` 文件树与 outline 严格一致（materialize 后）；
- `--strict` 把非 blocker 发现也视为失败；blocker 时退出码 4（E_VALIDATE_FAILED）；
- `--report` 可写 `<packageDir>/outline-validation-report.json`（只读校验的报告例外）。

本模块同时被 `bg_outline_apply.py` 与 `_lib/state.py`（T4 guard）复用：
`validate_document()` / `compute_layout()` / `build_figure_index()` / `build_stats()`。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, paths, state as state_lib

SCHEMA_VERSION = "1.0"
FIGURE_TYPES: tuple[str, ...] = ("table", "mermaid", "network", "terminal")
FIGURE_QUOTA_KEYS = FIGURE_TYPES
PRIORITIES = ("P0", "P1", "P2")
COMPLETION_STATUS = ("pending", "writing", "done", "skipped", "blocked")
REVIEW_COVERAGE = ("unreviewed", "pass", "weak", "missing")
FLOOR_WORD = 800
MAX_WORD = 8000
MAX_CHAPTERS = 500
WORD_DELTA_TOL = 0.01
PATH_MAX_LEN = 240
ID_RE = re.compile(r"^[1-9]\d*(?:-[1-9]\d*)*$")
ANCHOR_RE = re.compile(r"^fig-([1-9]\d*(?:-[1-9]\d*)*)-(\d{2})$")
ILLEGAL_PATH_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
META_COMMENT_RE = re.compile(
    r"^<!-- BG:CHAPTER id=(?P<id>\S+) level=(?P<level>\d+) wordTarget=(?P<word>\d+) seq=(?P<seq>\d+) -->$"
)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,7})\s+(?P<title>\S.*)$")
DRAFT_SUFFIX = ".draft"
OUTLINE_DIR = "chapters/_outline"
OUTLINE_FILE = "outline.json"
DRAFT_FILE = "outline.draft.json"
REPORT_FILE = "outline-validation-report.json"


def outline_dir(package_dir: Path, *, create: bool = False) -> Path:
    """M5.1：大纲元数据目录 `<packageDir>/chapters/_outline/`（不计入章节树）。"""
    target = package_dir / OUTLINE_DIR
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def outline_path(package_dir: Path) -> Path:
    return outline_dir(package_dir) / OUTLINE_FILE


def draft_path(package_dir: Path) -> Path:
    return outline_dir(package_dir) / DRAFT_FILE


def legacy_outline_path(package_dir: Path) -> Path:
    """M5.1 前的包根路径（仅用于迁移识别/兼容读取）。"""
    return package_dir / OUTLINE_FILE


def legacy_draft_path(package_dir: Path) -> Path:
    return package_dir / DRAFT_FILE


# ---------------------------------------------------------------------------
# 路径/加载辅助
# ---------------------------------------------------------------------------
def safe_name(title: str, *, fallback: str = "chapter") -> str:
    """把标题转为文件名安全组件（保留中文/字母/数字/空格/连字符）。"""
    name = ILLEGAL_PATH_RE.sub("-", str(title or "")).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"-{2,}", "-", name).strip(" -")
    if not name:
        name = fallback
    return name[:80].rstrip(". ")


def find_package_dir(project_dir: Path, package_id: str) -> Path:
    """定位最新 `packages/package-<N>-<ts14>/` 目录。"""
    root = project_dir / "packages"
    dirs = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and entry.name.startswith(package_id + "-")),
        key=lambda entry: entry.name,
    ) if root.is_dir() else []
    if not dirs:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"未找到 {package_id} 的包目录",
            hint="先执行 bg_prepare.py 创建包目录",
            detail={"packageId": package_id, "packagesDir": "packages/"},
        )
    return dirs[-1]


def load_package_meta(package_dir: Path) -> dict[str, Any]:
    meta_path = package_dir / "package-meta.json"
    if not meta_path.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少 package-meta.json",
            hint="先执行 bg_prepare.py",
            detail={"path": str(meta_path)},
        )
    try:
        document = jsonio.read_json(meta_path)
    except errors.BgError as exc:
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"package-meta.json 不可解析：{exc.message}") from exc
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "package-meta.json 必须是 JSON 对象")
    return dict(document)


def resolve_package_context(
    workspace: str | Path | None,
    project: str | Path | None,
    package_id: str | None = None,
) -> tuple[Path, Path, str, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """解析 (workspace, project_dir, package_id, package_dir, package_meta, state, project_doc)。"""
    workspace_path, project_dir = state_lib.resolve_project_context(project, workspace)
    state = state_lib.load_state(project_dir)
    project_doc = state_lib.load_project(project_dir)
    packages = [entry for entry in project_doc.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = package_id or str(state.get("currentPackage") or "") or (packages[0] if packages else "")
    if not target or not re.match(r"^package-[1-9]\d*$", target):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"无法确定目标包：{target!r}",
            hint="传入 --package package-<N>",
        )
    if packages and target not in packages:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"目标包不在 packagesToWrite：{target}",
            detail={"target": target, "packagesToWrite": packages},
        )
    package_dir = find_package_dir(project_dir, target)
    package_meta = load_package_meta(package_dir)
    return workspace_path, project_dir, target, package_dir, package_meta, state, project_doc


def resolve_input_path(package_dir: Path, input_path: str | None, *, prefer_outline: bool = False) -> tuple[Path, bool]:
    """返回 (输入文件, 是否草稿模式)。"""
    if input_path:
        path = Path(input_path).expanduser()
        path = path if path.is_absolute() else (Path.cwd() / path)
        if not path.is_file():
            raise errors.BgError(errors.E_NOT_FOUND, f"大纲输入文件不存在：{input_path}", detail={"path": str(path)})
        return path, DRAFT_SUFFIX in path.name or path.name == DRAFT_FILE
    outline = outline_path(package_dir)
    draft = draft_path(package_dir)
    legacy_outline = legacy_outline_path(package_dir)
    legacy_draft = legacy_draft_path(package_dir)
    if prefer_outline and outline.is_file():
        return outline, False
    if draft.is_file():
        return draft, True
    if outline.is_file():
        return outline, False
    # 兼容 M5.1 前旧项目：读取包根旧文件，但 apply 会提示先迁移（不静默写旧位置）
    if prefer_outline and legacy_outline.is_file():
        return legacy_outline, False
    if legacy_draft.is_file():
        return legacy_draft, True
    if legacy_outline.is_file():
        return legacy_outline, False
    raise errors.BgError(
        errors.E_NOT_FOUND,
        "缺少大纲文件（chapters/_outline/outline.json / outline.draft.json）",
        hint="Agent 先写 chapters/_outline/outline.draft.json；旧项目先运行 bg_outline_apply.py --migrate-storage",
        detail={"packageDir": package_dir.name, "outlineDir": OUTLINE_DIR},
    )


# ---------------------------------------------------------------------------
# 计算布局（seq / dirPath / filePath / kind）
# ---------------------------------------------------------------------------
def _walk_nodes(nodes: Sequence[Any], parent: Mapping[str, Any] | None, depth: int, out: list[dict[str, Any]]) -> None:
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        node["_depth"] = depth
        node["_index"] = index + 1
        out.append(node)
        children = node.get("children")
        if isinstance(children, list) and children:
            _walk_nodes(children, node, depth + 1, out)


def compute_layout(document: dict[str, Any]) -> dict[str, Any]:
    """计算/回写 seq、kind、dirPath、filePath；返回结构元信息（不写文件）。"""
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        chapters = []
        document["chapters"] = chapters
    nodes: list[dict[str, Any]] = []
    _walk_nodes(chapters, None, 1, nodes)
    for index, node in enumerate(nodes, start=1):
        node["seq"] = index
    for node in nodes:
        node["kind"] = "parent" if isinstance(node.get("children"), list) and node["children"] else "leaf"

    def assign(nodes_list: Sequence[dict[str, Any]], parent_path: str) -> None:
        for node in nodes_list:
            node_id = str(node.get("id") or "")
            node_title = safe_name(str(node.get("title") or ""))
            name = f"{node_id}-{node_title}" if node_id else node_title
            if node.get("kind") == "parent":
                node["dirPath"] = f"{parent_path}/{name}/"
                node["filePath"] = None
                node["wordTarget"] = 0
                assign(node.get("children") or [], node["dirPath"].rstrip("/"))
            else:
                node["dirPath"] = None
                node["filePath"] = f"{parent_path}/{name}.md"
                node.pop("children", None)

    assign(chapters, "chapters")
    parents = [node for node in nodes if node.get("kind") == "parent"]
    leaves = [node for node in nodes if node.get("kind") == "leaf"]
    preorder = [str(node.get("id") or "") for node in nodes]
    return {"nodes": nodes, "parents": parents, "leaves": leaves, "preorder": preorder}


def iter_preorder(chapters: Sequence[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    _walk_nodes(chapters, None, 1, nodes)
    return nodes


def build_figure_index(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从所有 figurePlan 汇总 figureIndex（章节先序）。"""
    result: list[dict[str, Any]] = []
    for node in iter_preorder(document.get("chapters") or []):
        plans = node.get("figurePlan")
        if not isinstance(plans, list):
            continue
        for plan in plans:
            if not isinstance(plan, Mapping):
                continue
            result.append(
                {
                    "anchorId": plan.get("anchorId"),
                    "type": plan.get("type"),
                    "chapterId": node.get("id"),
                    "caption": plan.get("caption"),
                    "status": plan.get("status") or "planned",
                    "resourcePath": plan.get("resourcePath") or "",
                    "sourcePath": plan.get("sourcePath") or "",
                }
            )
    return result


def build_stats(document: Mapping[str, Any], package_meta: Mapping[str, Any]) -> dict[str, Any]:
    nodes = iter_preorder(document.get("chapters") or [])
    parents = [node for node in nodes if node.get("kind") == "parent" or isinstance(node.get("children"), list) and node.get("children")]
    leaves = [node for node in nodes if node not in parents]
    planned: dict[str, int] = {key: 0 for key in FIGURE_TYPES}
    for node in nodes:
        for plan in node.get("figurePlan") or []:
            if isinstance(plan, Mapping) and plan.get("type") in planned:
                planned[str(plan["type"])] += 1
    quota = package_meta.get("figureQuota") if isinstance(package_meta.get("figureQuota"), Mapping) else {}
    figure_quota = {key: int(quota.get(key) or 0) for key in FIGURE_TYPES}
    word_total = 0
    for leaf in leaves:
        value = leaf.get("wordTarget")
        if isinstance(value, int) and not isinstance(value, bool):
            word_total += value
    total_target = document.get("totalWordTarget")
    total_target = total_target if isinstance(total_target, int) and not isinstance(total_target, bool) else 0
    delta_pct = round(abs(word_total - total_target) / total_target * 100, 4) if total_target else 0.0
    return {
        "parentCount": len(parents),
        "leafCount": len(leaves),
        "plannedWordTotal": word_total,
        "wordDeltaPct": delta_pct,
        "figurePlanned": planned,
        "figureQuota": figure_quota,
    }


# ---------------------------------------------------------------------------
# 校验核心
# ---------------------------------------------------------------------------
def _add(
    findings: list[dict[str, Any]],
    code: str,
    severity: str,
    pointer: str,
    message: str,
    suggestion: str = "",
) -> None:
    item: dict[str, Any] = {"code": code, "severity": severity, "pointer": pointer, "message": message}
    if suggestion:
        item["suggestion"] = suggestion
    findings.append(item)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_criterion_index(
    document: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    criteria = document.get("criterionIndex")
    if not isinstance(criteria, list):
        _add(findings, "criterion_index_invalid", "blocker", "/criterionIndex", "criterionIndex 必须是数组")
        return index
    if not criteria:
        _add(findings, "criterion_index_empty", "blocker", "/criterionIndex", "criterionIndex 不能为空（技术评审因素缺失走 F5.1.9 回退）")
    for position, criterion in enumerate(criteria):
        pointer = f"/criterionIndex/{position}"
        if not isinstance(criterion, Mapping):
            _add(findings, "criterion_invalid", "blocker", pointer, "评审因素必须是对象")
            continue
        criterion_id = criterion.get("criterionId")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            _add(findings, "criterion_id_missing", "blocker", pointer, "criterionId 不能为空")
            continue
        if criterion_id in index:
            _add(findings, "criterion_id_duplicate", "blocker", pointer, f"criterionId 重复：{criterion_id}")
            continue
        for key in ("factor", "itemText", "sourceRef"):
            if not isinstance(criterion.get(key), str) or not str(criterion.get(key)).strip():
                _add(findings, f"criterion_{key}_missing", "blocker", f"{pointer}/{key}", f"{key} 不能为空")
        score = criterion.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or float(score) < 0:
            _add(findings, "criterion_score_invalid", "blocker", f"{pointer}/score", "score 必须是非负数字")
        covered_by = criterion.get("coveredBy")
        if covered_by is not None and not isinstance(covered_by, list):
            _add(findings, "criterion_covered_by_invalid", "blocker", f"{pointer}/coveredBy", "coveredBy 必须是数组")
        index[criterion_id] = dict(criterion)
    return index


def _validate_node_refs(
    node: Mapping[str, Any],
    pointer: str,
    criterion_index: Mapping[str, Mapping[str, Any]],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs = node.get("criterionRefs")
    if not isinstance(refs, list):
        _add(findings, "criterion_refs_missing", "blocker", f"{pointer}/criterionRefs", "criterionRefs 必须是数组（至少 1 条）")
        return []
    if not refs:
        _add(findings, "criterion_refs_empty", "blocker", f"{pointer}/criterionRefs", "criterionRefs 至少 1 条（一级标题/末级均必填）")
    normalized: list[dict[str, Any]] = []
    for ref_position, ref in enumerate(refs):
        ref_pointer = f"{pointer}/criterionRefs/{ref_position}"
        if not isinstance(ref, Mapping):
            _add(findings, "criterion_ref_invalid", "blocker", ref_pointer, "criterionRefs 元素必须是对象")
            continue
        criterion_id = ref.get("criterionId")
        if not isinstance(criterion_id, str) or criterion_id not in criterion_index:
            _add(
                findings,
                "criterion_ref_unknown",
                "blocker",
                f"{ref_pointer}/criterionId",
                f"未知 criterionId：{criterion_id!r}（必须来自 criterionIndex）",
            )
        score = ref.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or float(score) < 0:
            _add(findings, "criterion_ref_score_invalid", "blocker", f"{ref_pointer}/score", "criterionRefs.score 必须是非负数字")
        normalized.append(dict(ref))
    return normalized


def _validate_figures(
    node: Mapping[str, Any],
    pointer: str,
    anchor_seen: set[str],
    figure_counts: dict[str, int],
    findings: list[dict[str, Any]],
    *,
    is_leaf: bool,
) -> None:
    plans = node.get("figurePlan")
    if plans is not None:
        if not isinstance(plans, list):
            _add(findings, "figure_plan_invalid", "blocker", f"{pointer}/figurePlan", "figurePlan 必须是数组")
        elif not is_leaf and plans:
            _add(findings, "figure_plan_on_parent", "blocker", f"{pointer}/figurePlan", "图表规划只允许出现在末级章节")
        else:
            for position, plan in enumerate(plans):
                plan_pointer = f"{pointer}/figurePlan/{position}"
                if not isinstance(plan, Mapping):
                    _add(findings, "figure_plan_invalid", "blocker", plan_pointer, "figurePlan 元素必须是对象")
                    continue
                figure_type = plan.get("type")
                if figure_type not in FIGURE_TYPES:
                    _add(findings, "figure_type_invalid", "blocker", f"{plan_pointer}/type", f"图表类型必须是 {FIGURE_TYPES}")
                else:
                    figure_counts[str(figure_type)] += 1
                anchor = plan.get("anchorId")
                if not isinstance(anchor, str) or not anchor:
                    _add(findings, "figure_anchor_missing", "blocker", f"{plan_pointer}/anchorId", "anchorId 不能为空")
                else:
                    if anchor in anchor_seen:
                        _add(findings, "figure_anchor_duplicate", "blocker", f"{plan_pointer}/anchorId", f"anchorId 重复：{anchor}")
                    anchor_seen.add(anchor)
                    match = ANCHOR_RE.match(anchor)
                    if not match or match.group(1) != str(node.get("id") or ""):
                        _add(
                            findings,
                            "figure_anchor_format",
                            "blocker",
                            f"{plan_pointer}/anchorId",
                            f"anchorId 必须为 fig-<chapterId>-<NN> 且属于本章：{anchor!r}",
                        )
                caption = plan.get("caption")
                if not isinstance(caption, str) or not caption.strip():
                    _add(findings, "figure_caption_missing", "blocker", f"{plan_pointer}/caption", "caption 不能为空")
                elif caption.strip().startswith(("▼", "▲")):
                    _add(findings, "figure_caption_symbol", "blocker", f"{plan_pointer}/caption", "caption 必须是纯题名，不含 ▼/▲ 符号")
                if not isinstance(plan.get("purpose"), str) or not str(plan.get("purpose")).strip():
                    _add(findings, "figure_purpose_missing", "blocker", f"{plan_pointer}/purpose", "purpose 不能为空")
                status = plan.get("status", "planned")
                if status not in ("planned", "deferred", "done"):
                    _add(findings, "figure_status_invalid", "blocker", f"{plan_pointer}/status", "status 必须是 planned/deferred/done")
    anchors = node.get("insertAnchors")
    if anchors is not None:
        if not isinstance(anchors, list):
            _add(findings, "insert_anchors_invalid", "blocker", f"{pointer}/insertAnchors", "insertAnchors 必须是数组")
        elif anchors and not is_leaf:
            _add(findings, "insert_anchors_on_parent", "blocker", f"{pointer}/insertAnchors", "插入锚点只允许出现在末级章节")


def _validate_node(
    node: dict[str, Any],
    pointer: str,
    *,
    parent: Mapping[str, Any] | None,
    depth: int,
    order: int,
    expected_id: str,
    criterion_index: Mapping[str, Mapping[str, Any]],
    findings: list[dict[str, Any]],
    anchor_seen: set[str],
    figure_counts: dict[str, int],
    child_refs_seen: dict[str, list[str]],
    require_layout: bool,
    max_word: int,
) -> None:
    node_id = node.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        _add(findings, "id_missing", "blocker", f"{pointer}/id", "id 不能为空")
        node_id = ""
    elif not ID_RE.match(node_id):
        _add(findings, "id_invalid", "blocker", f"{pointer}/id", f"id 非法：{node_id!r}（形如 1 / 1-1 / 1-3-1）")
    if node_id and node_id != expected_id:
        _add(findings, "id_path_mismatch", "blocker", f"{pointer}/id", f"id 与路径不一致：期望 {expected_id!r}，实际 {node_id!r}")
    level = node.get("level")
    if not _is_int(level) or level != depth:
        _add(findings, "level_mismatch", "blocker", f"{pointer}/level", f"level 必须等于路径深度 {depth}，实际 {level!r}")
    children = node.get("children")
    is_leaf = not (isinstance(children, list) and children)
    expected_kind = "leaf" if is_leaf else "parent"
    if node.get("kind") != expected_kind:
        _add(findings, "kind_mismatch", "blocker", f"{pointer}/kind", f"kind 应为 {expected_kind}（有子节点=parent，否则=leaf）")
    if node.get("order") != order:
        _add(findings, "order_mismatch", "blocker", f"{pointer}/order", f"order 必须为同级序号 {order}，实际 {node.get('order')!r}")
    title = node.get("title")
    if not isinstance(title, str) or not title.strip():
        _add(findings, "title_missing", "blocker", f"{pointer}/title", "title 不能为空")
    if is_leaf and _is_int(level) and level > 5:
        _add(findings, "leaf_level_exceeded", "blocker", f"{pointer}/level", "末级章节 level ≤5（H3）")
    if not is_leaf and _is_int(level) and level > 4:
        _add(findings, "parent_level_exceeded", "blocker", f"{pointer}/level", "父级章节 level ≤4（H3）")
    if not is_leaf and (not isinstance(children, list) or not children):
        _add(findings, "parent_without_children", "blocker", pointer, "parent 必须有子节点")
    if require_layout:
        for key in ("seq", "dirPath", "filePath", "wordTarget", "contentRequirements", "priority"):
            if key not in node:
                _add(findings, "missing_field", "blocker", f"{pointer}/{key}", f"缺少必填字段 {key}")
        if "seq" in node and (not _is_int(node.get("seq")) or node.get("seq", 0) <= 0):
            _add(findings, "seq_invalid", "blocker", f"{pointer}/seq", "seq 必须是正整数（由脚本计算）")
        if is_leaf:
            if node.get("filePath") is None:
                _add(findings, "file_path_missing", "blocker", f"{pointer}/filePath", "末级 filePath 不能为空")
            if node.get("dirPath") is not None:
                _add(findings, "leaf_dir_path_invalid", "blocker", f"{pointer}/dirPath", "末级 dirPath 必须为 null")
        else:
            if node.get("dirPath") is None:
                _add(findings, "dir_path_missing", "blocker", f"{pointer}/dirPath", "父级 dirPath 不能为空")
            if node.get("filePath") is not None:
                _add(findings, "parent_file_path_invalid", "blocker", f"{pointer}/filePath", "父级 filePath 必须为 null")
    refs = _validate_node_refs(node, pointer, criterion_index, findings)
    if is_leaf:
        for ref in refs:
            if isinstance(ref.get("criterionId"), str):
                child_refs_seen.setdefault(str(ref["criterionId"]), []).append(node_id)
    priority = node.get("priority")
    if priority not in PRIORITIES:
        _add(findings, "priority_invalid", "blocker", f"{pointer}/priority", f"priority 必须是 {PRIORITIES}")
    score_weight = node.get("scoreWeight")
    if not isinstance(score_weight, (int, float)) or isinstance(score_weight, bool) or float(score_weight) < 0:
        _add(findings, "score_weight_invalid", "blocker", f"{pointer}/scoreWeight", "scoreWeight 必须是非负数字")
    elif not is_leaf:
        children_weight = sum(
            float(child.get("scoreWeight") or 0) for child in children if isinstance(child, Mapping)
        )
        if abs(float(score_weight) - children_weight) > 1e-6:
            _add(
                findings,
                "parent_score_mismatch",
                "blocker",
                f"{pointer}/scoreWeight",
                f"父级 scoreWeight 必须等于子节点之和：{score_weight} != {children_weight}",
            )
    if is_leaf:
        requirements = node.get("contentRequirements")
        if not isinstance(requirements, list) or not requirements:
            _add(findings, "content_requirements_missing", "blocker", f"{pointer}/contentRequirements", "末级章节必须至少 1 条 contentRequirements（H5）")
        else:
            for index, requirement in enumerate(requirements):
                if not isinstance(requirement, str) or not requirement.strip():
                    _add(findings, "content_requirement_invalid", "blocker", f"{pointer}/contentRequirements/{index}", "写作要求不能为空字符串")
        if require_layout:
            word_target = node.get("wordTarget")
            if not _is_int(word_target) or value_lt(word_target, FLOOR_WORD):
                _add(findings, "word_target_below_floor", "blocker", f"{pointer}/wordTarget", f"wordTarget 必须 ≥{FLOOR_WORD}（实际 {word_target!r}）")
            elif word_target > max_word:
                _add(
                    findings,
                    "word_target_above_max",
                    "warning",
                    f"{pointer}/wordTarget",
                    f"wordTarget 超出单章上限 {max_word}（实际 {word_target}），apply 会 clamp",
                )
    else:
        if node.get("wordTarget") not in (0, None) and node.get("wordTarget") != 0:
            _add(findings, "parent_word_target_nonzero", "blocker", f"{pointer}/wordTarget", "父级 wordTarget 必须为 0")
    _validate_figures(node, pointer, anchor_seen, figure_counts, findings, is_leaf=is_leaf)
    if "completion" in node:
        completion = node.get("completion")
        _validate_completion(completion, f"{pointer}/completion", findings, required=False)
    elif require_layout:
        _add(findings, "completion_missing", "blocker", f"{pointer}/completion", "缺少 completion 对象（apply 会补默认值）")
    if "reviewState" in node:
        review_state = node.get("reviewState")
        if not isinstance(review_state, Mapping):
            _add(findings, "review_state_invalid", "blocker", f"{pointer}/reviewState", "reviewState 必须是对象")
        else:
            if review_state.get("coverage") not in REVIEW_COVERAGE:
                _add(findings, "review_coverage_invalid", "blocker", f"{pointer}/reviewState/coverage", f"coverage 必须是 {REVIEW_COVERAGE}")
            if not _is_int(review_state.get("lastRound")) or review_state.get("lastRound", 0) < 0:
                _add(findings, "review_round_invalid", "blocker", f"{pointer}/reviewState/lastRound", "lastRound 必须是非负整数")
            if not isinstance(review_state.get("issueIds"), list):
                _add(findings, "review_issue_ids_invalid", "blocker", f"{pointer}/reviewState/issueIds", "issueIds 必须是数组")
    for key in ("dependsOn", "relatedChapters"):
        value = node.get(key)
        if value is not None and not isinstance(value, list):
            _add(findings, f"{key}_invalid", "blocker", f"{pointer}/{key}", f"{key} 必须是数组")
        elif isinstance(value, list):
            for index, ref in enumerate(value):
                if not isinstance(ref, str) or not ref:
                    _add(findings, f"{key}_item_invalid", "blocker", f"{pointer}/{key}/{index}", "章节 id 不能为空")
    if not is_leaf:
        for child_index, child in enumerate(children):
            if not isinstance(child, dict):
                _add(findings, "child_not_object", "blocker", f"{pointer}/children/{child_index}", "子节点必须是对象")
                continue
            _validate_node(
                child,
                f"{pointer}/children/{child_index}",
                parent=node,
                depth=depth + 1,
                order=child_index + 1,
                expected_id=f"{node_id}-{child_index + 1}" if node_id else "",
                criterion_index=criterion_index,
                findings=findings,
                anchor_seen=anchor_seen,
                figure_counts=figure_counts,
                child_refs_seen=child_refs_seen,
                require_layout=require_layout,
                max_word=max_word,
            )


def value_lt(value: Any, limit: int) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value < limit


def _validate_completion(completion: Any, pointer: str, findings: list[dict[str, Any]], *, required: bool) -> None:
    if not isinstance(completion, Mapping):
        if required:
            _add(findings, "completion_invalid", "blocker", pointer, "completion 必须是对象")
        return
    if completion.get("status") not in COMPLETION_STATUS:
        _add(findings, "completion_status_invalid", "blocker", f"{pointer}/status", f"status 必须是 {COMPLETION_STATUS}")
    if not _is_int(completion.get("wordCount")) or completion.get("wordCount", 0) < 0:
        _add(findings, "completion_word_count_invalid", "blocker", f"{pointer}/wordCount", "wordCount 必须是非负整数")
    if not isinstance(completion.get("updatedAt"), str):
        _add(findings, "completion_updated_at_invalid", "blocker", f"{pointer}/updatedAt", "updatedAt 必须是字符串")
    summary = completion.get("summary", "")
    if not isinstance(summary, str):
        _add(findings, "completion_summary_invalid", "blocker", f"{pointer}/summary", "summary 必须是字符串")
    elif len(summary) > 500:
        _add(findings, "completion_summary_too_long", "warning", f"{pointer}/summary", "summary 超过 500 字（PRD F5.2.4）")


def _check_dependencies(
    nodes: Sequence[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> None:
    by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    preorder = [str(node.get("id")) for node in nodes]
    order_index = {node_id: index for index, node_id in enumerate(preorder)}
    graph: dict[str, list[str]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        deps = node.get("dependsOn") or []
        if not isinstance(deps, list):
            continue
        graph[node_id] = []
        for dep in deps:
            if not isinstance(dep, str):
                continue
            if dep == node_id:
                _add(findings, "depends_on_self", "blocker", f"/chapters#id={node_id}/dependsOn", "dependsOn 不得自引用")
                continue
            if dep not in by_id:
                _add(findings, "depends_on_missing", "blocker", f"/chapters#id={node_id}/dependsOn", f"dependsOn 指向不存在的章节：{dep}")
                continue
            if order_index.get(dep, 10**9) >= order_index.get(node_id, -1):
                _add(
                    findings,
                    "depends_on_order",
                    "blocker",
                    f"/chapters#id={node_id}/dependsOn",
                    f"dependsOn 只能指向 seq 更小的章节：{dep}（seq {order_index.get(dep)} vs {order_index.get(node_id)}）",
                )
            graph[node_id].append(dep)
    # 环检测（仅依赖边；DFS 三色）
    state_color: dict[str, int] = {}

    def visit(node_id: str, stack: list[str]) -> bool:
        color = state_color.get(node_id, 0)
        if color == 1:
            cycle = " -> ".join(stack + [node_id])
            _add(findings, "dependency_cycle", "blocker", "/chapters/dependsOn", f"dependsOn 存在环：{cycle}")
            return True
        if color == 2:
            return False
        state_color[node_id] = 1
        for dep in graph.get(node_id, []):
            if visit(dep, stack + [node_id]):
                return True
        state_color[node_id] = 2
        return False

    for node_id in graph:
        if visit(node_id, []):
            break


def _validate_paths(
    nodes: Sequence[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> None:
    seen_dirs: set[str] = set()
    seen_files: set[str] = set()
    illegal_component = re.compile(r'[<>:"\\\\|?*\x00-\x1f]')
    for node in nodes:
        for key in ("dirPath", "filePath"):
            value = node.get(key)
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("/") or "\\" in value or ".." in Path(value).parts:
                _add(findings, "path_invalid", "blocker", f"/chapters#id={node.get('id')}/{key}", f"path 必须是包目录内相对 POSIX 路径：{value!r}")
            if len(value) > PATH_MAX_LEN:
                _add(findings, "path_too_long", "blocker", f"/chapters#id={node.get('id')}/{key}", f"路径长度超过 {PATH_MAX_LEN}")
            for part in value.split("/"):
                if not part:
                    continue
                if illegal_component.search(part) or part in (".", ".."):
                    _add(findings, "path_illegal_char", "blocker", f"/chapters#id={node.get('id')}/{key}", f"路径组件含 Windows 非法字符：{part!r}")
                    break
            if key == "dirPath":
                if value in seen_dirs:
                    _add(findings, "path_duplicate", "blocker", f"/chapters#id={node.get('id')}/{key}", f"目录路径重复：{value}")
                seen_dirs.add(value)
            else:
                if value in seen_files:
                    _add(findings, "path_duplicate", "blocker", f"/chapters#id={node.get('id')}/{key}", f"文件路径重复：{value}")
                seen_files.add(value)

def validate_document(
    document: Any,
    package_meta: Mapping[str, Any],
    *,
    require_layout: bool = True,
    strict: bool = False,
    check_tree: bool = False,
    package_dir: Path | None = None,
    source: str = "",
) -> dict[str, Any]:
    """校验大纲文档，返回 findings/stats/valid（不抛异常）。"""
    findings: list[dict[str, Any]] = []
    if not isinstance(document, Mapping):
        _add(findings, "outline_not_object", "blocker", "/", "outline 顶层必须是 JSON 对象")
        return {"valid": False, "findings": findings, "blockers": findings, "warnings": [], "stats": {}, "source": source}
    doc = dict(document)
    for key, expected_type, required in (
        ("schemaVersion", str, True),
        ("packageId", str, True),
        ("title", str, True),
        ("totalWordTarget", int, True),
        ("criterionIndex", list, True),
        ("chapters", list, True),
    ):
        value = doc.get(key)
        if key not in doc:
            if required:
                _add(findings, f"missing_{key}", "blocker", f"/{key}", f"缺少顶层字段 {key}")
            continue
        if expected_type is int:
            if not _is_int(value):
                _add(findings, f"{key}_invalid", "blocker", f"/{key}", f"{key} 必须是整数")
        elif not isinstance(value, expected_type):
            _add(findings, f"{key}_invalid", "blocker", f"/{key}", f"{key} 必须{'是字符串' if expected_type is str else '是数组'}")
    if doc.get("schemaVersion") not in (None, SCHEMA_VERSION):
        _add(findings, "schema_version_invalid", "blocker", "/schemaVersion", f"schemaVersion 必须为 {SCHEMA_VERSION!r}")
    package_id = str(package_meta.get("packageId") or "")
    if isinstance(doc.get("packageId"), str) and package_id and doc.get("packageId") != package_id:
        _add(findings, "package_id_mismatch", "blocker", "/packageId", f"packageId 必须为 {package_id!r}")
    total_target = doc.get("totalWordTarget")
    expected_word = package_meta.get("expectedWordCount")
    if _is_int(total_target) and _is_int(expected_word) and total_target != expected_word:
        _add(
            findings,
            "total_word_target_mismatch",
            "blocker",
            "/totalWordTarget",
            f"totalWordTarget 必须等于 package-meta.expectedWordCount（{expected_word}），实际 {total_target}",
        )
    if require_layout and not isinstance(doc.get("generatedAt"), str):
        _add(findings, "generated_at_missing", "blocker", "/generatedAt", "缺少 generatedAt（由 apply 写入）")
    # 草稿模式：generatedAt 由 apply 补，不提示（避免 --strict 因脚本生成字段失败）
    criterion_index = _check_criterion_index(doc, findings)
    chapters = doc.get("chapters")
    if not isinstance(chapters, list):
        _add(findings, "chapters_invalid", "blocker", "/chapters", "chapters 必须是数组")
        chapters = []
    elif not chapters:
        _add(findings, "chapters_empty", "blocker", "/chapters", "chapters 不能为空")
    anchor_seen: set[str] = set()
    figure_counts: dict[str, int] = {key: 0 for key in FIGURE_TYPES}
    child_refs_seen: dict[str, list[str]] = {}
    max_word = min(MAX_WORD, int(expected_word * 0.5)) if _is_int(expected_word) else MAX_WORD
    for index, root in enumerate(chapters):
        if not isinstance(root, dict):
            _add(findings, "root_not_object", "blocker", f"/chapters/{index}", "一级章节必须是对象")
            continue
        _validate_node(
            root,
            f"/chapters/{index}",
            parent=None,
            depth=1,
            order=index + 1,
            expected_id=str(index + 1),
            criterion_index=criterion_index,
            findings=findings,
            anchor_seen=anchor_seen,
            figure_counts=figure_counts,
            child_refs_seen=child_refs_seen,
            require_layout=require_layout,
            max_word=max_word,
        )
    nodes = iter_preorder(chapters)
    if len(nodes) > MAX_CHAPTERS:
        _add(findings, "node_count_exceeded", "warning", "/chapters", f"节点数 {len(nodes)} 超过 {MAX_CHAPTERS}（建议拆分/合并）")
    _check_dependencies(nodes, findings)
    if require_layout:
        _validate_paths(nodes, findings)
    # 覆盖检查（leaf 覆盖 criterionIndex 全部 key）
    covered = {criterion_id for criterion_id, ids in child_refs_seen.items() if ids}
    leaf_ids = {str(node.get("id")) for node in nodes if not (isinstance(node.get("children"), list) and node.get("children"))}
    leaf_covered = {
        ref.get("criterionId")
        for node in nodes
        if str(node.get("id")) in leaf_ids
        for ref in (node.get("criterionRefs") or [])
        if isinstance(ref, Mapping)
    }
    uncovered = [criterion_id for criterion_id in criterion_index if criterion_id not in leaf_covered]
    coverage = round((len(criterion_index) - len(uncovered)) / len(criterion_index), 6) if criterion_index else 0.0
    if uncovered:
        _add(
            findings,
            "coverage_gap",
            "blocker",
            "/criterionIndex",
            f"评审因素未被末级章节覆盖：{uncovered}（coverage={coverage:.2%}）",
            "补章节或经用户确认在 criterionIndex 标 notApplicable",
        )
    # coveredBy 回写核对（final）
    for criterion_id, criterion in criterion_index.items():
        expected_covered = child_refs_seen.get(criterion_id) or []
        actual_covered = criterion.get("coveredBy") if isinstance(criterion.get("coveredBy"), list) else None
        if require_layout and actual_covered is not None and sorted(str(x) for x in actual_covered) != sorted(expected_covered):
            _add(
                findings,
                "covered_by_mismatch",
                "warning",
                f"/criterionIndex#{criterion_id}/coveredBy",
                f"coveredBy 与实际引用不一致（apply 会重算）：{actual_covered} vs {expected_covered}",
            )
    # 字数合计
    stats = build_stats(doc, package_meta)
    if require_layout and _is_int(total_target) and total_target > 0:
        if stats["wordDeltaPct"] > WORD_DELTA_TOL * 100:
            _add(
                findings,
                "word_total_mismatch",
                "blocker",
                "/chapters",
                f"ΣwordTarget={stats['plannedWordTotal']} 与 totalWordTarget={total_target} 偏差 {stats['wordDeltaPct']}%（容差 ±1%）",
                "重调 wordTarget（apply 会按 F5.1.5 重新分配）",
            )
    # 图表配额对账
    quota = stats["figureQuota"]
    for key in FIGURE_TYPES:
        planned = stats["figurePlanned"][key]
        if planned < quota[key]:
            _add(
                findings,
                "figure_quota_mismatch",
                "blocker",
                "/chapters",
                f"图表配额不足：{key} planned={planned} quota={quota[key]}（允许 ≥ 配额）",
            )
    # 计算后字段（final 必须由脚本回写）
    if require_layout:
        expected_figures = build_figure_index(doc)
        figure_index = doc.get("figureIndex")
        if not isinstance(figure_index, list):
            _add(findings, "figure_index_missing", "blocker", "/figureIndex", "缺少 figureIndex（apply 生成）")
        else:
            core_keys = ("anchorId", "type", "chapterId", "caption", "status")
            expected_core = [{key: item.get(key) for key in core_keys} for item in expected_figures]
            actual_core = [
                {key: item.get(key) for key in core_keys} for item in figure_index if isinstance(item, Mapping)
            ]
            if actual_core != expected_core:
                _add(findings, "figure_index_mismatch", "blocker", "/figureIndex", "figureIndex 与 chapters.figurePlan 汇总不一致")
        expected_stats = {
            "parentCount": stats["parentCount"],
            "leafCount": stats["leafCount"],
            "plannedWordTotal": stats["plannedWordTotal"],
            "figurePlanned": stats["figurePlanned"],
            "figureQuota": stats["figureQuota"],
        }
        actual_stats = doc.get("stats")
        if not isinstance(actual_stats, Mapping):
            _add(findings, "stats_missing", "blocker", "/stats", "缺少 stats（apply 生成）")
        else:
            for key, expected in expected_stats.items():
                if actual_stats.get(key) != expected:
                    _add(
                        findings,
                        "stats_mismatch",
                        "blocker",
                        f"/stats/{key}",
                        f"stats.{key} 与计算值不一致：{actual_stats.get(key)!r} != {expected!r}",
                    )
    # 草稿模式：figureIndex/stats 由 apply 重算，静默忽略
    # 文件树
    if check_tree:
        if package_dir is None:
            _add(findings, "check_tree_no_package_dir", "blocker", "/", "check_tree 需要 package_dir")
        else:
            tree_findings = validate_tree_files(doc, package_dir)
            findings.extend(tree_findings)
    blockers = [item for item in findings if item.get("severity") == "blocker"]
    warnings = [item for item in findings if item.get("severity") != "blocker"]
    valid = not blockers and (not strict or not warnings)
    return {
        "valid": valid,
        "strict": strict,
        "findings": findings,
        "blockers": blockers,
        "warnings": warnings,
        "stats": stats,
        "source": source,
    }


# ---------------------------------------------------------------------------
# 文件树校验
# ---------------------------------------------------------------------------
def _expected_tree(document: Mapping[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    dirs: set[str] = set()
    files: dict[str, dict[str, Any]] = {}
    for node in iter_preorder(document.get("chapters") or []):
        is_leaf = not (isinstance(node.get("children"), list) and node.get("children"))
        if is_leaf:
            file_path = node.get("filePath")
            if isinstance(file_path, str) and file_path:
                files[file_path] = node
        else:
            dir_path = node.get("dirPath")
            if isinstance(dir_path, str) and dir_path:
                dirs.add(dir_path.rstrip("/"))
    return dirs, files


def validate_tree_files(document: Mapping[str, Any], package_dir: Path) -> list[dict[str, Any]]:
    """校验 `chapters/**` 与 outline 树一致（H2 第二事实源）。"""
    findings: list[dict[str, Any]] = []
    chapters_dir = package_dir / "chapters"
    expected_dirs, expected_files = _expected_tree(document)
    if not chapters_dir.is_dir():
        _add(findings, "tree_missing_dir", "blocker", "/chapters", "chapters/ 目录不存在（先 --materialize）")
        return findings
    actual_dirs: set[str] = set()
    actual_files: set[str] = set()
    for entry in sorted(chapters_dir.rglob("*")):
        rel = entry.relative_to(package_dir).as_posix()
        parts = entry.relative_to(chapters_dir).parts
        if any(part in ("_results", "_frozen", "resources", "_outline") for part in parts):
            continue
        if entry.name.startswith(".") or entry.name.endswith(".lock"):
            continue
        if entry.is_dir():
            actual_dirs.add(rel.rstrip("/"))
        elif entry.is_file():
            if entry.suffix == ".md":
                actual_files.add(rel)
            else:
                actual_files.add(rel)
    for missing_dir in sorted(expected_dirs - actual_dirs):
        _add(findings, "tree_missing_dir", "blocker", f"/chapters/{missing_dir}", f"缺少章节文件夹：{missing_dir}")
    for extra_dir in sorted(actual_dirs - expected_dirs):
        _add(findings, "tree_extra_dir", "blocker", f"/chapters/{extra_dir}", f"多余文件夹：{extra_dir}")
    for missing_file in sorted(set(expected_files) - actual_files):
        _add(findings, "tree_missing_file", "blocker", f"/chapters/{missing_file}", f"缺少章节文件：{missing_file}")
    for extra_file in sorted(actual_files - set(expected_files)):
        _add(findings, "tree_extra_file", "blocker", f"/chapters/{extra_file}", f"多余文件：{extra_file}")
    for rel, node in sorted(expected_files.items()):
        path = package_dir / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _add(findings, "tree_file_unreadable", "blocker", f"/chapters/{rel}", f"文件不可读：{exc}")
            continue
        lines = text.splitlines()
        if not lines:
            _add(findings, "tree_empty_file", "blocker", f"/chapters/{rel}", "章节文件为空（materialize 必须写标题+meta）")
            continue
        heading = HEADING_RE.match(lines[0].strip())
        if not heading:
            _add(findings, "tree_heading_missing", "blocker", f"/chapters/{rel}", "首行必须是 Markdown 标题")
        else:
            level = int(node.get("level") or 0)
            hashes = heading.group("hashes")
            if len(hashes) != level:
                _add(
                    findings,
                    "tree_heading_level_mismatch",
                    "blocker",
                    f"/chapters/{rel}",
                    f"标题层级 {len(hashes)} 与章节 level {level} 不一致",
                )
            if len(hashes) > 6:
                _add(findings, "tree_heading_depth_exceeded", "blocker", f"/chapters/{rel}", "标题深度超过 6 级（H1）")
            if heading.group("title").strip() != str(node.get("title") or "").strip():
                _add(findings, "tree_heading_text_mismatch", "blocker", f"/chapters/{rel}", "首行标题与 outline.title 不一致")
        if len(lines) < 2:
            _add(findings, "tree_meta_missing", "blocker", f"/chapters/{rel}", "缺少第二行 BG:CHAPTER meta 注释")
            continue
        meta = META_COMMENT_RE.match(lines[1].strip())
        if not meta:
            _add(findings, "tree_meta_missing", "blocker", f"/chapters/{rel}", "第二行必须是 <!-- BG:CHAPTER ... --> meta 注释")
            continue
        expected = {
            "id": str(node.get("id") or ""),
            "level": str(node.get("level") or ""),
            "word": str(node.get("wordTarget") or ""),
            "seq": str(node.get("seq") or ""),
        }
        actual = {
            "id": meta.group("id"),
            "level": meta.group("level"),
            "word": meta.group("word"),
            "seq": meta.group("seq"),
        }
        if actual != expected:
            _add(
                findings,
                "tree_meta_mismatch",
                "blocker",
                f"/chapters/{rel}",
                f"BG:CHAPTER meta 与 outline 不一致：{actual} != {expected}",
            )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_outline_validate.py",
        description="BidGenie F5-1 大纲校验：schema/层级/结构/覆盖率/字数/图表/依赖/路径/文件树（只读）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>（缺省 state.currentPackage / packagesToWrite 首个）")
    parser.add_argument("--input", help="指定大纲文件（缺省：outline.json 优先，否则 outline.draft.json）")
    parser.add_argument("--strict", action="store_true", help="把 warning/major/minor 也视为失败（H.0 --strict 语义）")
    parser.add_argument("--check-tree", action="store_true", help="同时校验 chapters/** 文件树与 outline 一致（materialize 后）")
    parser.add_argument("--report", action="store_true", help="把校验报告写入 <packageDir>/outline-validation-report.json")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def _payload(
    *,
    workspace: Path,
    project_dir: Path,
    target: str,
    package_dir: Path,
    source_path: Path,
    require_layout: bool,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "outline_validate",
        "projectId": None,
        "packageId": target,
        "source": paths.rel_posix(project_dir, source_path),
        "draftMode": not require_layout,
        "valid": bool(result.get("valid")),
        "strict": bool(result.get("strict")),
        "blockers": result.get("blockers") or [],
        "warnings": result.get("warnings") or [],
        "findings": result.get("findings") or [],
        "stats": result.get("stats") or {},
        "packageDir": paths.rel_posix(project_dir, package_dir),
    }


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir, target, package_dir, package_meta, state, project_doc = resolve_package_context(
        args.workspace, args.project, args.package
    )
    input_path, draft_mode = resolve_input_path(package_dir, args.input, prefer_outline=True)
    try:
        document = jsonio.read_json(input_path)
    except errors.BgError as exc:
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"大纲文件不可解析：{exc.message}", detail={"path": str(input_path)}) from exc
    require_layout = not draft_mode
    result = validate_document(
        document,
        package_meta,
        require_layout=require_layout,
        strict=bool(args.strict),
        check_tree=bool(args.check_tree),
        package_dir=package_dir,
        source=paths.rel_posix(project_dir, input_path),
    )
    payload = _payload(
        workspace=workspace,
        project_dir=project_dir,
        target=target,
        package_dir=package_dir,
        source_path=input_path,
        require_layout=require_layout,
        result=result,
    )
    payload["projectId"] = project_doc.get("projectId")
    payload["currentStage"] = state.get("currentStage")
    if args.report:
        report_path = package_dir / REPORT_FILE
        jsonio.write_json_atomic(
            report_path,
            {
                "schemaVersion": SCHEMA_VERSION,
                "packageId": target,
                "generatedAt": paths.iso_now(),
                "source": payload["source"],
                **{key: payload[key] for key in ("valid", "strict", "blockers", "warnings", "findings", "stats")},
            },
        )
        payload["reportPath"] = paths.rel_posix(project_dir, report_path)
    if not payload["valid"]:
        failed = payload["blockers"] + (payload["warnings"] if args.strict else [])
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"大纲校验未通过：{len(payload['blockers'])} 项 blocker"
            + (f"、{len(payload['warnings'])} 项 warning（--strict 计为失败）" if args.strict and payload["warnings"] else ""),
            hint="按 blockers/findings 修正后重试（Agent 只改 outline.draft.json，再 apply）",
            detail=payload,
            exit_code=errors.EXIT_SCHEMA,
        )
    return payload


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def build_selftest_package(base: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """构造临时工作区/项目/包目录/package-meta（isolated extras: extraction 不写入）。"""
    workspace = base / "ws"
    project_dir = workspace / "示例项目-20261111100000-abcd"
    package_dir = project_dir / "packages" / "package-1-20261111100000-abcd"
    package_dir.mkdir(parents=True)
    for rel in ("chapters", "reviews", "resources", "export"):
        (package_dir / rel).mkdir(exist_ok=True)
    package_meta = {
        "schemaVersion": "1.0",
        "packageId": "package-1",
        "expectedWordCount": 50000,
        "figureQuota": {"table": 5, "mermaid": 5, "network": 0, "terminal": 5},
        "personnelPlan": "",
        "equipmentPlan": "",
        "valueSource": {key: "user" for key in (
            "expectedWordCount", "personnelPlan", "equipmentPlan", "schedulePlan",
            "table", "mermaid", "network", "terminal",
        )},
        "schedulePlan": {"startDate": "", "deliveryDate": "", "constructionPlan": ""},
    }
    jsonio.write_json_atomic(package_dir / "package-meta.json", package_meta)
    return workspace, project_dir, package_dir, package_meta


def _golden_document() -> dict[str, Any]:
    def leaf(node_id: str, title: str, order: int, criterion_id: str, weight: float, word: int, figures: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": node_id,
            "title": title,
            "level": 2,
            "kind": "leaf",
            "order": order,
            "criterionRefs": [
                {
                    "criterionId": criterion_id,
                    "factor": "技术评分",
                    "itemText": title,
                    "score": weight,
                    "sourceRef": "extraction/packages/package-1/07-Evaluation-Criteria.md#技术评分标准",
                }
            ],
            "scoreWeight": weight,
            "contentRequirements": [f"必须包含{title}的背景、目标、措施与验收标准", "不得照抄招标文件原文超过 30%"],
            "wordTarget": word,
            "wordTargetBasis": {"formula": "W*share", "share": 0.1, "weight": weight, "adjusted": word},
            "priority": "P0",
            "dependsOn": [],
            "relatedChapters": [],
            "figurePlan": figures,
            "insertAnchors": [
                {"anchorId": figure["anchorId"], "positionHint": "章节末尾", "type": figure["type"]}
                for figure in figures
            ],
            "writingNotes": [],
            "completion": {"status": "pending", "wordCount": 0, "updatedAt": "", "summary": "", "skipReason": "", "blockReason": ""},
            "reviewState": {"coverage": "unreviewed", "lastRound": 0, "issueIds": []},
            "extensions": {},
        }

    criteria = [
        {"criterionId": "CR-001", "factor": "需求分析及理解", "itemText": "技术评审因素一", "score": 9, "sourceRef": "extraction/packages/package-1/07-Evaluation-Criteria.md#技术评分标准", "coveredBy": []},
        {"criterionId": "CR-002", "factor": "巡检服务", "itemText": "技术评审因素二", "score": 6, "sourceRef": "extraction/packages/package-1/07-Evaluation-Criteria.md#技术评分标准", "coveredBy": []},
        {"criterionId": "CR-003", "factor": "应急处理", "itemText": "技术评审因素三", "score": 8, "sourceRef": "extraction/packages/package-1/07-Evaluation-Criteria.md#技术评分标准", "coveredBy": []},
    ]
    return {
        "schemaVersion": "1.0",
        "packageId": "package-1",
        "title": "示例项目技术方案",
        "totalWordTarget": 50000,
        "criterionIndex": criteria,
        "chapters": [
            {
                "id": "1",
                "title": "需求分析及理解",
                "level": 1,
                "kind": "parent",
                "order": 1,
                "criterionRefs": [{"criterionId": "CR-001", "factor": "需求分析及理解", "itemText": "技术评审因素一", "score": 9, "sourceRef": "…"}],
                "scoreWeight": 9,
                "priority": "P0",
                "completion": {"status": "pending", "wordCount": 0, "updatedAt": "", "summary": "", "skipReason": "", "blockReason": ""},
                "children": [
                    leaf("1-1", "项目背景分析", 1, "CR-001", 3, 10000,
                         [{"anchorId": "fig-1-1-01", "type": "table", "purpose": "对比现状", "caption": "项目现状对比", "status": "planned"},
                          {"anchorId": "fig-1-1-02", "type": "table", "purpose": "需求清单", "caption": "需求清单", "status": "planned"}]),
                    leaf("1-2", "项目目标分析", 2, "CR-001", 3, 10000,
                         [{"anchorId": "fig-1-2-01", "type": "table", "purpose": "目标指标", "caption": "目标指标表", "status": "planned"},
                          {"anchorId": "fig-1-2-02", "type": "mermaid", "purpose": "目标关系", "caption": "项目目标关系", "status": "planned"}]),
                    leaf("1-3", "运维重点分析", 3, "CR-001", 3, 10000,
                         [{"anchorId": "fig-1-3-01", "type": "terminal", "purpose": "巡检命令", "caption": "巡检命令示例", "status": "planned"},
                          {"anchorId": "fig-1-3-02", "type": "terminal", "purpose": "诊断命令", "caption": "故障诊断命令示例", "status": "planned"}]),
                ],
            },
            {
                "id": "2",
                "title": "巡检服务",
                "level": 1,
                "kind": "parent",
                "order": 2,
                "criterionRefs": [{"criterionId": "CR-002", "factor": "巡检服务", "itemText": "技术评审因素二", "score": 6, "sourceRef": "…"}],
                "scoreWeight": 6,
                "priority": "P1",
                "completion": {"status": "pending", "wordCount": 0, "updatedAt": "", "summary": "", "skipReason": "", "blockReason": ""},
                "children": [
                    leaf("2-1", "巡检内容", 1, "CR-002", 2, 8000,
                         [{"anchorId": "fig-2-1-01", "type": "table", "purpose": "巡检清单", "caption": "巡检内容清单", "status": "planned"}]),
                    leaf("2-2", "巡检方式", 2, "CR-002", 2, 8000,
                         [{"anchorId": "fig-2-2-01", "type": "mermaid", "purpose": "巡检流程", "caption": "巡检流程", "status": "planned"},
                          {"anchorId": "fig-2-2-02", "type": "mermaid", "purpose": "组织关系", "caption": "巡检组织关系", "status": "planned"}]),
                    leaf("2-3", "巡检周期", 3, "CR-002", 2, 8000,
                         [{"anchorId": "fig-2-3-01", "type": "terminal", "purpose": "巡检终端", "caption": "巡检终端示例", "status": "planned"},
                          {"anchorId": "fig-2-3-02", "type": "terminal", "purpose": "巡检记录", "caption": "巡检记录输出", "status": "planned"}]),
                ],
            },
            {
                "id": "3",
                "title": "应急处理",
                "level": 1,
                "kind": "parent",
                "order": 3,
                "criterionRefs": [{"criterionId": "CR-003", "factor": "应急处理", "itemText": "技术评审因素三", "score": 8, "sourceRef": "…"}],
                "scoreWeight": 8,
                "priority": "P0",
                "completion": {"status": "pending", "wordCount": 0, "updatedAt": "", "summary": "", "skipReason": "", "blockReason": ""},
                "children": [
                    leaf("3-1", "应急响应", 1, "CR-003", 4, 10000,
                         [{"anchorId": "fig-3-1-01", "type": "table", "purpose": "响应分级", "caption": "应急响应分级", "status": "planned"},
                          {"anchorId": "fig-3-1-02", "type": "terminal", "purpose": "响应命令", "caption": "应急响应命令", "status": "planned"}]),
                    leaf("3-2", "重大故障处理", 2, "CR-003", 4, 10000,
                         [{"anchorId": "fig-3-2-01", "type": "mermaid", "purpose": "故障流程", "caption": "重大故障处理流程", "status": "planned"},
                          {"anchorId": "fig-3-2-02", "type": "mermaid", "purpose": "故障树", "caption": "故障定位路径", "status": "planned"}]),
                ],
            },
        ],
        "extensions": {},
    }


def _build_document_and_expected(require_layout: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    document = copy.deepcopy(_golden_document())
    from bg_outline_apply import normalize_outline  # 自检时复用 apply 的归一化（避免重复实现）
    normalized = normalize_outline(document, package_meta={"expectedWordCount": 50000, "packageId": "package-1"})
    return normalized, normalized


def run_selftest() -> dict[str, Any]:
    """大纲校验自检：golden 通过 + 层级/覆盖/字数/图表/anchor/依赖/路径/文件树负例。"""
    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-outline-validate-selftest-"))
    try:
        workspace, project_dir, package_dir, package_meta = build_selftest_package(tmp)
        from bg_outline_apply import normalize_outline
        golden = normalize_outline(copy.deepcopy(_golden_document()), package_meta=package_meta)
        outline_dir(package_dir, create=True)
        jsonio.write_json_atomic(outline_path(package_dir), golden)
        base_result = validate_document(golden, package_meta, require_layout=True)
        if not base_result["valid"]:
            raise AssertionError(f"golden 大纲校验失败：{base_result['blockers']}")
        checks.append("golden_valid")

        def block(mutator: Any, expected_code: str) -> None:
            document = copy.deepcopy(golden)
            mutator(document)
            result = validate_document(document, package_meta, require_layout=True)
            codes = {item["code"] for item in result["blockers"]}
            if expected_code not in codes:
                raise AssertionError(f"负例未命中 {expected_code}：{sorted(codes)}")

        block(lambda doc: doc["chapters"][0]["children"][0].__setitem__("level", 6), "level_mismatch")
        block(lambda doc: doc["chapters"].pop(0), "coverage_gap")
        block(lambda doc: doc["chapters"][0]["children"][0].__setitem__("wordTarget", 0), "word_target_below_floor")
        block(lambda doc: doc["chapters"][0]["children"][0]["figurePlan"].pop(), "figure_quota_mismatch")
        block(lambda doc: doc["chapters"][0]["children"][1]["figurePlan"][0].__setitem__("anchorId", "fig-9-9-01"), "figure_anchor_format")
        block(lambda doc: doc["chapters"][0]["children"][0].__setitem__("dependsOn", ["3-2"]), "depends_on_order")
        block(lambda doc: doc["chapters"][0]["children"][0].__setitem__("id", "1-9"), "id_path_mismatch")
        block(lambda doc: doc["criterionIndex"][0].__setitem__("criterionId", "CR-999"), "criterion_ref_unknown")

        # 文件树：materialize 后通过，缺文件/内容不符失败
        from bg_outline_apply import materialize_tree
        materialize_tree(golden, package_dir, force=True)
        tree_result = validate_document(golden, package_meta, require_layout=True, check_tree=True, package_dir=package_dir)
        if not tree_result["valid"]:
            raise AssertionError(f"materialize 后文件树校验失败：{tree_result['blockers']}")
        checks.append("check_tree_pass")
        target_file = package_dir / golden["chapters"][0]["children"][0]["filePath"]
        target_file.unlink()
        tree_bad = validate_document(golden, package_meta, require_layout=True, check_tree=True, package_dir=package_dir)
        if "tree_missing_file" not in {item["code"] for item in tree_bad["blockers"]}:
            raise AssertionError("文件树缺文件未被发现")
        checks.append("check_tree_negative")

        # 草稿模式：缺 generatedAt/figureIndex/stats 允许（apply 会补）
        draft = copy.deepcopy(golden)
        for key in ("generatedAt", "figureIndex", "stats"):
            draft.pop(key, None)
        draft_result = validate_document(draft, package_meta, require_layout=False)
        if not draft_result["valid"]:
            raise AssertionError(f"草稿模式不应要求生成字段：{draft_result['blockers']}")
        checks.append("draft_mode")
        return {"ok": True, "selftest": {"script": "bg_outline_validate.py", "checks": checks}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_outline_validate.py",
    )


if __name__ == "__main__":
    sys.exit(main())
