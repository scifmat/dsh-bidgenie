#!/usr/bin/env python3
"""BidGenie F6 问题台账（bg_review_issues.py，PRD F6.3–F6.5 / 7.5 / 4.6 / 4.3 T6–T8）。

命令：
    merge   --round <k> --input <json> [--layer coverage|graphic]  合并 B/C 层问题（校验/ID/去重/快照）
    close   --issue <RV-...> --status fixing|fixed|verified|wontfix [--note] [--user-confirmed] [--review]
    verify  [--round <k>]                                          计算 4.6 通过判据（只读）
    status                                                         台账摘要（只读）
所有写操作只改 reviews/**；state.json 仅由 bg_state.py 写。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
ISSUES_REL = "reviews/issues.json"
RESULT_SUFFIX = ".chapter-result.json"
ISSUE_ID_RE = re.compile(r"^RV-(\d+)-(\d{4})$")
LAYERS = ("script", "coverage", "graphic")
SEVERITIES = ("blocker", "major", "minor")
STATUSES = ("open", "fixing", "fixed", "verified", "wontfix")
ISSUE_TYPES = {
    "missing_chapter", "extra_chapter", "empty_document", "heading_depth_exceeded", "heading_level_mismatch",
    "word_count_low", "word_count_high", "total_word_low", "total_word_high",
    "coverage_gap", "weak_response", "missing_criterion", "criterion_mismatch",
    "figure_missing", "figure_quality", "figure_mismatch", "caption_mismatch", "table_missing", "table_mismatch",
    "citation_missing", "source_mismatch",
    "caption_format", "marker_format", "forbidden_content", "placeholder_left",
    "export_failed", "missing_figure_on_export", "toc_missing",
    "review_timeout", "script_error",
}
OPEN_STATUSES = ("open", "fixing")


# ---------------------------------------------------------------------------
# 读取/写工具
# ---------------------------------------------------------------------------
def issues_path(package_dir: Path) -> Path:
    return package_dir / ISSUES_REL


def load_issues(package_dir: Path, *, required: bool = True) -> dict[str, Any]:
    path = issues_path(package_dir)
    if not path.is_file():
        if not required:
            return {}
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少 reviews/issues.json（先跑 bg_review_script.py）",
            hint="运行 bg_review_script.py --round <k> 生成 A 层问题",
            detail={"path": ISSUES_REL},
        )
    document = jsonio.read_json(path)
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "issues.json 必须是 JSON 对象")
    return dict(document)


def recalc_summary(issues: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def count(status_set: Sequence[str]) -> int:
        return sum(1 for issue in issues if str(issue.get("status")) in status_set)

    return {
        "blocker": sum(1 for issue in issues if issue.get("severity") == "blocker"),
        "major": sum(1 for issue in issues if issue.get("severity") == "major"),
        "minor": sum(1 for issue in issues if issue.get("severity") == "minor"),
        "total": len(issues),
        "openCount": count(OPEN_STATUSES),
        "fixedCount": count(("fixed",)),
        "verifiedCount": count(("verified",)),
        "wontfixCount": count(("wontfix",)),
    }


def validate_issue(issue: Any, *, chapter_ids: set[str] | None = None) -> list[str]:
    findings: list[str] = []
    if not isinstance(issue, Mapping):
        return ["issue 必须是对象"]
    for key in ("type", "severity", "evidence"):
        if not str(issue.get(key) or "").strip():
            findings.append(f"缺少 {key}")
    layer = str(issue.get("layer") or "")
    if layer not in LAYERS:
        findings.append(f"layer 必须是 {LAYERS}")
    if str(issue.get("severity")) not in SEVERITIES:
        findings.append(f"severity 必须是 {SEVERITIES}")
    if str(issue.get("type")) not in ISSUE_TYPES:
        findings.append(f"type 不在 7.5.2 字典：{issue.get('type')}")
    status = str(issue.get("status") or "open")
    if status not in STATUSES:
        findings.append(f"status 必须是 {STATUSES}")
    chapter_id = str(issue.get("chapterId") or "")
    if chapter_id and chapter_ids is not None and chapter_id not in chapter_ids:
        findings.append(f"chapterId 不存在：{chapter_id}")
    if status == "wontfix" and not issue.get("userConfirmed"):
        findings.append("wontfix 必须 userConfirmed=true")
    return findings


def next_issue_id(issues: Sequence[Mapping[str, Any]], round_no: int) -> str:
    max_seq = 0
    for issue in issues:
        match = ISSUE_ID_RE.match(str(issue.get("issueId") or ""))
        if match and int(match.group(1)) == round_no:
            max_seq = max(max_seq, int(match.group(2)))
    return f"RV-{round_no}-{max_seq + 1:04d}"


def write_issues(package_dir: Path, document: dict[str, Any]) -> None:
    round_no = int(document.get("round") or 1)
    issues = list(document.get("issues") or [])
    document["issues"] = issues
    document["summary"] = recalc_summary(issues)
    document["generatedAt"] = paths.iso_now()
    path = issues_path(package_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(path, document)
    snapshot = package_dir / "reviews" / "rounds" / f"round-{round_no}.issues.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(snapshot, document)


# ---------------------------------------------------------------------------
# 通过判据（4.6/F6.5）
# ---------------------------------------------------------------------------
def evaluate_pass(document: Mapping[str, Any], outline: Mapping[str, Any] | None = None) -> dict[str, Any]:
    issues = [item for item in document.get("issues") or [] if isinstance(item, Mapping)]
    summary = recalc_summary(issues)
    blockers: list[dict[str, Any]] = []
    majors_open: list[dict[str, Any]] = []
    minors_open: list[dict[str, Any]] = []
    for issue in issues:
        status = str(issue.get("status") or "open")
        severity = str(issue.get("severity") or "")
        if severity == "blocker" and not (status == "verified" or (status == "wontfix" and issue.get("userConfirmed"))):
            blockers.append({"issueId": issue.get("issueId"), "type": issue.get("type"), "status": status})
        elif severity == "major" and not (status == "verified" or (status == "wontfix" and issue.get("userConfirmed"))):
            majors_open.append({"issueId": issue.get("issueId"), "type": issue.get("type"), "status": status})
        elif severity == "minor":
            if not (issue.get("userConfirmed") or status == "fixed" or status == "verified") and status != "wontfix":
                minors_open.append({"issueId": issue.get("issueId"), "type": issue.get("type"), "status": status})
            elif status == "wontfix" and not issue.get("userConfirmed"):
                minors_open.append({"issueId": issue.get("issueId"), "type": issue.get("type"), "status": status})
    layers_run = list(document.get("layersRun") or [])
    layers_ok = all(layer in layers_run for layer in LAYERS)
    coverage_ok = True
    coverage_gaps: list[str] = []
    if outline is not None:
        # M7 修复：coverage 判据只要求末级章节；父级（有无 children）跳过/视为 notApplicable
        for node in outline_validate.iter_preorder(outline.get("chapters") or []):
            if isinstance(node.get("children"), list) and node.get("children"):
                continue
            review_state = node.get("reviewState") if isinstance(node.get("reviewState"), Mapping) else {}
            coverage = str(review_state.get("coverage") or "unreviewed")
            if coverage in ("unreviewed", "weak", "missing"):
                coverage_ok = False
                coverage_gaps.append(f"{node.get('id')}:{coverage}")
    reasons: list[str] = []
    if blockers:
        reasons.append(f"blocker 未关闭：{blockers}")
    if majors_open:
        reasons.append(f"major 未 verified/wontfix+confirmed：{majors_open}")
    if minors_open:
        reasons.append(f"minor 未 fixed/verified/wontfix+confirmed：{minors_open}")
    if not layers_ok:
        reasons.append(f"本轮三层审查未全部执行：layersRun={layers_run}")
    if not coverage_ok:
        reasons.append(f"reviewState.coverage 非全部 pass/notApplicable：{coverage_gaps[:10]}")
    per_type = {key: summary[key] for key in ("blocker", "major", "minor")}
    return {
        "pass": not reasons,
        "reasons": reasons,
        "summary": summary,
        "layersRun": layers_run,
        "layersOk": layers_ok,
        "coverageOk": coverage_ok,
        "coverageGaps": coverage_gaps,
        "countsBySeverity": per_type,
    }


def refresh_report_stats(package_dir: Path, document: Mapping[str, Any]) -> bool:
    """低成本刷新 review-report.md 的合并后统计段（保留 A 层详细报告；幂等替换标记段）。"""
    report_path = package_dir / "reviews" / "review-report.md"
    if not report_path.is_file():
        return False
    summary = document.get("summary") if isinstance(document.get("summary"), Mapping) else recalc_summary(document.get("issues") or [])
    block = (
        "<!-- ISSUES_MERGE_STATS:start -->\n"
        f"- 合并时间：{paths.iso_now()}\n"
        f"- 问题统计：blocker **{summary.get('blocker', 0)}** / major **{summary.get('major', 0)}** / minor **{summary.get('minor', 0)}** / open **{summary.get('openCount', 0)}**\n"
        f"- layersRun：{document.get('layersRun')}；pendingLayers：{document.get('pendingLayers')}\n"
        "<!-- ISSUES_MERGE_STATS:end -->"
    )
    text = report_path.read_text(encoding="utf-8")
    pattern = re.compile(r"<!-- ISSUES_MERGE_STATS:start -->.*?<!-- ISSUES_MERGE_STATS:end -->", re.DOTALL)
    if pattern.search(text):
        updated = pattern.sub(block, text)
    else:
        updated = text.rstrip() + "\n\n## B/C 合并后统计（bg_review_issues merge）\n\n" + block + "\n"
    layer_status = document.get("layerStatus") if isinstance(document.get("layerStatus"), Mapping) else {}
    pending_layers = document.get("pendingLayers") if isinstance(document.get("pendingLayers"), list) else []
    header = (
        f"- A 层：**{layer_status.get('script', '?')}**；"
        f"B 层：**{layer_status.get('coverage', 'skipped')}**；"
        f"C 层：**{layer_status.get('graphic', 'skipped')}**"
    )
    updated = re.sub(r"- A 层：\*\*.*?\*\*；B 层：\*\*.*?\*\*；C 层：\*\*.*?\*\*", header, updated, count=1)
    if "coverage" not in pending_layers:
        updated = updated.replace(
            "B 层评分项响应覆盖度：**待语义审查**",
            "B 层评分项响应覆盖度：**已合并（done）**",
        )
    else:
        updated = updated.replace("B 层评分项响应覆盖度：**已合并（done）**", "B 层评分项响应覆盖度：**待语义审查**")
    if "graphic" not in pending_layers:
        updated = updated.replace(
            "C 层图表契合度：**待语义审查**",
            "C 层图表契合度：**已合并（done）**",
        )
    else:
        updated = updated.replace("C 层图表契合度：**已合并（done）**", "C 层图表契合度：**待语义审查**")
    if not pending_layers:
        updated = updated.replace("；本报告暂为待审占位", "").replace("（本报告暂为待审占位）", "").replace("本报告暂为待审占位", "")
        updated = updated.replace(
            "> A 层未发现 blocker；B/C 层需由语义审查子智能体执行（本报告暂为待审占位）。",
            f"> A 层未发现 blocker；B 层={layer_status.get('coverage', 'skipped')}，C 层={layer_status.get('graphic', 'skipped')}（已合并）。",
        )
    report_path.write_text(updated, encoding="utf-8", newline="\n")
    return True


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def merge_issues(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    round_no: int,
    input_path: str,
    layer: str | None,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, f"输入问题文件不存在：{input_path}", detail={"path": str(path)})
    incoming = jsonio.read_json(path)
    coverage_updates: list[dict[str, Any]] = []
    if isinstance(incoming, Mapping) and isinstance(incoming.get("issues"), list):
        incoming_issues = incoming["issues"]
        incoming_layer = incoming.get("layer") or layer
        raw_updates = incoming.get("coverageUpdates")
        if isinstance(raw_updates, list):
            coverage_updates = [dict(item) for item in raw_updates if isinstance(item, Mapping)]
    elif isinstance(incoming, list):
        incoming_issues = incoming
        incoming_layer = layer
    else:
        raise errors.BgError(errors.E_SCHEMA_INVALID, "输入必须是 issues 数组或 {issues:[...], coverageUpdates?:[...]}", detail={"path": str(path)})
    document = load_issues(package_dir, required=False)
    if not document:
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "packageId": target,
            "round": round_no,
            "generatedAt": paths.iso_now(),
            "layersRun": ["script"],
            "summary": {},
            "issues": [],
        }
    if int(document.get("round") or round_no) != round_no:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"issues.json 当前轮次 {document.get('round')} != --round {round_no}",
            hint="先对当前轮跑 bg_review_script.py，或使用相同 --round",
            detail={"documentRound": document.get("round"), "requestedRound": round_no},
        )
    outline = None
    outline_path = outline_validate.outline_path(package_dir)
    if outline_path.is_file():
        outline = jsonio.read_json(outline_path)
    leaves = [str(node.get("id")) for node in outline_validate.iter_preorder((outline or {}).get("chapters") or []) if not (isinstance(node.get("children"), list) and node.get("children"))]
    chapter_ids = set(leaves) if leaves else None
    existing = [item for item in document.get("issues") or [] if isinstance(item, Mapping)]
    existing_keys = {
        (item.get("layer"), item.get("type"), item.get("chapterId"), item.get("anchorId"), str(item.get("evidence") or "")[:120])
        for item in existing
    }
    findings: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    now = paths.iso_now()
    for index, raw in enumerate(incoming_issues):
        candidate = dict(raw) if isinstance(raw, Mapping) else {}
        if not candidate.get("layer"):
            candidate["layer"] = incoming_layer or "coverage"
        errors_list = validate_issue(candidate, chapter_ids=chapter_ids)
        if errors_list:
            findings.append({"index": index, "errors": errors_list})
            continue
        key = (candidate.get("layer"), candidate.get("type"), candidate.get("chapterId"), candidate.get("anchorId"), str(candidate.get("evidence") or "")[:120])
        if key in existing_keys:
            continue
        existing_keys.add(key)
        issue = {
            "issueId": next_issue_id(existing + merged, round_no),
            "layer": str(candidate["layer"]),
            "type": str(candidate["type"]),
            "severity": str(candidate["severity"]),
            "chapterId": str(candidate.get("chapterId") or ""),
            "filePath": str(candidate.get("filePath") or ""),
            "anchorId": candidate.get("anchorId") or None,
            "evidence": str(candidate.get("evidence") or "")[:500],
            "suggestion": str(candidate.get("suggestion") or "")[:500],
            "status": "open",
            "userConfirmed": False,
            "createdAt": now,
            "updatedAt": now,
            "resolvedAt": None,
            "resolutionNote": "",
        }
        merged.append(issue)
    if findings:
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"B/C 层问题校验未通过：{len(findings)} 项",
            hint="按 findings 修正输入后重试；未写入 issues.json",
            detail={"findings": findings, "path": str(path)},
            exit_code=errors.EXIT_SCHEMA,
        )
    document["issues"] = existing + merged
    layers = list(document.get("layersRun") or [])
    for item in merged:
        if item["layer"] not in layers:
            layers.append(item["layer"])
    if incoming_layer and incoming_layer not in layers:
        layers.append(incoming_layer)
    document["layersRun"] = [layer_name for layer_name in LAYERS if layer_name in layers]
    layer_status = dict(document.get("layerStatus") or {}) if isinstance(document.get("layerStatus"), Mapping) else {}
    for layer_name in document["layersRun"]:
        if layer_name == "script":
            layer_status.setdefault("script", "pass")
        else:
            layer_status[layer_name] = "done"
    for layer_name in LAYERS:
        layer_status.setdefault(layer_name, "skipped")
    document["layerStatus"] = layer_status
    document["pendingLayers"] = [layer_name for layer_name in ("coverage", "graphic") if layer_name not in document["layersRun"]]
    write_issues(package_dir, document)
    report_updated = refresh_report_stats(package_dir, document)
    event_log.log_event(
        workspace_path,
        "review.round_end",
        actor="script",
        project=str(project_doc.get("projectId") or ""),
        package=target,
        detail={"round": round_no, "packageId": target, "layerStatus": document.get("layerStatus"), "pendingLayers": document.get("pendingLayers"), "mergedCount": len(merged)},
    )
    coverage_updated: list[dict[str, Any]] = []
    if coverage_updates and outline is not None:
        valid_leaf_ids = {
            str(node.get("id"))
            for node in outline_validate.iter_preorder(outline.get("chapters") or [])
            if not (isinstance(node.get("children"), list) and node.get("children"))
        }
        coverage_map: dict[str, str] = {}
        for item in coverage_updates:
            chapter_id = str(item.get("chapterId") or "")
            coverage = str(item.get("coverage") or "")
            if chapter_id not in valid_leaf_ids or coverage not in ("pass", "weak", "missing", "notApplicable"):
                raise errors.BgError(
                    errors.E_SCHEMA_INVALID,
                    f"coverageUpdates 非法：{item}",
                    detail={"chapterId": chapter_id, "coverage": coverage},
                )
            coverage_map[chapter_id] = coverage
        if coverage_map:
            outline_path = outline_validate.outline_path(package_dir)
            if outline_path.is_file():
                stamp = paths.timestamp14()
                backup = outline_path.parent / f"outline.json.bak-{stamp}"
                suffix = 1
                while backup.exists():
                    backup = outline_path.parent / f"outline.json.bak-{stamp}-{suffix}"
                    suffix += 1
                import shutil as _shutil
                _shutil.copy2(outline_path, backup)
                updated_outline = dict(outline)
                for node in outline_validate.iter_preorder(updated_outline.get("chapters") or []):
                    chapter_id = str(node.get("id"))
                    if chapter_id in coverage_map:
                        review_state = dict(node.get("reviewState") or {})
                        review_state["coverage"] = coverage_map[chapter_id]
                        review_state["lastRound"] = round_no
                        node["reviewState"] = review_state
                        coverage_updated.append({"chapterId": chapter_id, "coverage": coverage_map[chapter_id]})
                jsonio.write_json_atomic(outline_path, updated_outline)
    return {
        "ok": True,
        "command": "review_issues_merge",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "round": round_no,
        "merged": merged,
        "mergedCount": len(merged),
        "skippedCount": len(incoming_issues) - len(merged) - len(findings),
        "layersRun": document["layersRun"],
        "summary": document["summary"],
        "coverageUpdated": coverage_updated,
        "reportUpdated": report_updated,
        "layerStatus": document.get("layerStatus"),
        "pendingLayers": document.get("pendingLayers"),
        "issuesPath": ISSUES_REL,
        "snapshotPath": f"reviews/rounds/round-{round_no}.issues.json",
        "currentStage": state.get("currentStage"),
    }


def record_optimization_round(package_dir: Path, state: Mapping[str, Any], target: str, issue: Mapping[str, Any], previous: str, status: str, note: str) -> bool:
    """P0-3：close/fixed/verified 时自动生成/更新 round-<k>.optimization.json。"""
    review_loop = state.get("reviewLoop") if isinstance(state.get("reviewLoop"), Mapping) else {}
    entry = (review_loop.get("packages") or {}).get(target) if isinstance(review_loop.get("packages"), Mapping) else None
    round_value = int(entry.get("round") or review_loop.get("round") or 1) if isinstance(entry, Mapping) else int(review_loop.get("round") or 1)
    path = package_dir / "reviews" / "rounds" / f"round-{round_value}.optimization.json"
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = jsonio.read_json(path)
            document = loaded if isinstance(loaded, dict) else {}
        except BgError:
            document = {}
    items = [dict(item) for item in document.get("items") or [] if isinstance(item, Mapping)]
    outline = None
    outline_path = package_dir / "chapters" / "_outline" / "outline.json"
    if outline_path.is_file():
        try:
            outline = jsonio.read_json(outline_path)
        except BgError:
            outline = None
    after_words = int(((outline or {}).get("stats") or {}).get("writtenWordTotal") or 0)
    before_words = int(document.get("wordCountAfter") or after_words)
    items = [item for item in items if item.get("issueId") != issue.get("issueId")]
    items.append(
        {
            "issueId": issue.get("issueId"),
            "from": previous,
            "to": status,
            "chapterId": issue.get("chapterId") or "",
            "filePath": ((issue.get("evidence") or {}).get("filePath") if isinstance(issue.get("evidence"), Mapping) else issue.get("filePath")) or "",
            "note": note,
            "at": paths.iso_now(),
        }
    )
    document.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "packageId": target,
            "round": round_value,
            "generatedAt": paths.iso_now(),
            "wordCountBefore": before_words,
            "wordCountAfter": after_words,
            "items": items,
            "issues": [{"issueId": item.get("issueId"), "result": item.get("to")} for item in items],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(path, document)
    return True


def close_issue(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    issue_id: str,
    status: str,
    note: str,
    user_confirmed: bool,
    from_review: bool,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    document = load_issues(package_dir)
    issues = [dict(item) for item in document.get("issues") or [] if isinstance(item, Mapping)]
    match = next((issue for issue in issues if issue.get("issueId") == issue_id), None)
    if match is None:
        raise errors.BgError(errors.E_NOT_FOUND, f"问题不存在：{issue_id}", detail={"issueId": issue_id})
    previous = str(match.get("status") or "open")
    if status not in ("fixing", "fixed", "verified", "wontfix"):
        raise errors.BgError(errors.E_USAGE, f"--status 非法：{status}", detail={"allowed": ["fixing", "fixed", "verified", "wontfix"]})
    allowed = {
        "open": ("fixing", "fixed", "wontfix"),
        "fixing": ("fixed", "wontfix"),
        "fixed": ("verified",),
        "verified": (),
        "wontfix": (),
    }
    if status not in allowed.get(previous, ()):
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"非法问题状态流转：{previous} → {status}",
            hint="按 open→fixing/fixed→verified；open/fixing→wontfix（需用户确认）",
            detail={"issueId": issue_id, "from": previous, "to": status, "allowed": allowed.get(previous, ())},
        )
    if status == "wontfix" and not user_confirmed:
        raise errors.BgError(errors.E_GUARD_FAILED, "wontfix 必须携带 --user-confirmed（用户确认）", detail={"issueId": issue_id})
    if status == "wontfix" and not note.strip():
        raise errors.BgError(errors.E_USAGE, "wontfix 必须填写 --note 理由", detail={"issueId": issue_id})
    if status == "verified":
        if str(match.get("severity")) not in ("blocker", "major", "minor"):
            raise errors.BgError(errors.E_SCHEMA_INVALID, "问题 severity 非法，无法 verified")
        if not (from_review or state.get("currentStage") == "review"):
            raise errors.BgError(
                errors.E_GUARD_FAILED,
                "verified 只能由下一轮审查/复核写入，优化方不得自证",
                hint="在 review 阶段运行，或加 --review（复审）",
                detail={"issueId": issue_id, "currentStage": state.get("currentStage")},
            )
    now = paths.iso_now()
    match["status"] = status
    match["updatedAt"] = now
    match["resolutionNote"] = note.strip()
    if status in ("fixed", "verified", "wontfix"):
        match["resolvedAt"] = now
    if status == "wontfix":
        match["userConfirmed"] = True
    document["issues"] = issues
    write_issues(package_dir, document)
    optimization_updated = record_optimization_round(package_dir, state, target, match, previous, status, note)
    report_updated = refresh_report_stats(package_dir, document)
    return {
        "ok": True,
        "command": "review_issues_close",
        "packageId": target,
        "issueId": issue_id,
        "from": previous,
        "to": status,
        "summary": document["summary"],
        "issuesPath": ISSUES_REL,
        "reportUpdated": report_updated,
        "optimizationUpdated": optimization_updated,
    }


def verify_issues(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    round_no: int | None,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    document = load_issues(package_dir)
    if round_no is not None and int(document.get("round") or 0) != round_no:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"issues.json 轮次 {document.get('round')} != --round {round_no}",
            detail={"documentRound": document.get("round"), "requestedRound": round_no},
        )
    outline = None
    outline_path = outline_validate.outline_path(package_dir)
    if outline_path.is_file():
        outline = jsonio.read_json(outline_path)
    result = evaluate_pass(document, outline)
    review_loop = state.get("reviewLoop") if isinstance(state.get("reviewLoop"), Mapping) else {}
    round_value = int(document.get("round") or 0)
    max_rounds = int(review_loop.get("maxRounds") or 5)
    if not result["pass"] and round_value >= max_rounds:
        try:
            from _lib import logging as event_log

            event_log.log_event(
                workspace_path,
                "review.not_converged",
                project=str(project_doc.get("projectId") or ""),
                package=target,
                detail={"round": round_value, "maxRounds": max_rounds, "reasons": result["reasons"][:5]},
            )
        except errors.BgError:
            pass
        result["notConverged"] = True
        result["hint"] = "达到 reviewLoop.maxRounds 仍未通过：输出未收敛报告，等待用户决定继续/放宽/人工接管，不得自动通过"
    return {
        "ok": True,
        "command": "review_issues_verify",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "round": round_value,
        "currentStage": state.get("currentStage"),
        "reviewLoop": review_loop,
        "pass": result["pass"],
        "reasons": result["reasons"],
        "layersRun": result["layersRun"],
        "layersOk": result["layersOk"],
        "coverageOk": result["coverageOk"],
        "coverageGaps": result["coverageGaps"][:20],
        "summary": result["summary"],
        "notConverged": bool(result.get("notConverged")),
        "hint": result.get("hint", ""),
    }


def status_issues(
    *, workspace: Path, project_dir: Path, package_id: str | None
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    document = load_issues(package_dir, required=False)
    if not document:
        return {
            "ok": True,
            "command": "review_issues_status",
            "packageId": target,
            "currentStage": state.get("currentStage"),
            "issuesExists": False,
            "message": "尚无 reviews/issues.json",
        }
    issues = [item for item in document.get("issues") or [] if isinstance(item, Mapping)]
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for issue in issues:
        by_status[str(issue.get("status"))] = by_status.get(str(issue.get("status")), 0) + 1
        by_severity[str(issue.get("severity"))] = by_severity.get(str(issue.get("severity")), 0) + 1
    return {
        "ok": True,
        "command": "review_issues_status",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "currentStage": state.get("currentStage"),
        "reviewLoop": state.get("reviewLoop"),
        "issuesExists": True,
        "round": document.get("round"),
        "layersRun": document.get("layersRun") or [],
        "summary": recalc_summary(issues),
        "byStatus": by_status,
        "bySeverity": by_severity,
        "open": [issue.get("issueId") for issue in issues if str(issue.get("status")) in OPEN_STATUSES],
        "verified": [issue.get("issueId") for issue in issues if issue.get("status") == "verified"],
        "wontfix": [issue.get("issueId") for issue in issues if issue.get("status") == "wontfix"],
    }


def run_selftest() -> dict[str, Any]:
    """纯函数自检：summary/ID/pass criteria/issue 校验（不写文件）。"""
    checks: list[str] = []
    issues = [
        {"issueId": "RV-2-0001", "layer": "coverage", "type": "coverage_gap", "severity": "major", "status": "open", "userConfirmed": False},
        {"issueId": "RV-2-0002", "layer": "graphic", "type": "caption_mismatch", "severity": "minor", "status": "fixed", "userConfirmed": False},
    ]
    summary = recalc_summary(issues)
    if summary["major"] != 1 or summary["minor"] != 1 or summary["openCount"] != 1:
        raise AssertionError(f"summary 计算不正确：{summary}")
    checks.append("summary")
    if next_issue_id(issues, 2) != "RV-2-0003":
        raise AssertionError("issueId 分配不正确")
    checks.append("issue_id")
    document = {"round": 2, "layersRun": ["script", "coverage", "graphic"], "issues": issues}
    result = evaluate_pass(document, outline=None)
    if result["pass"] or not result["reasons"]:
        raise AssertionError(f"未关闭 major 时不应通过：{result}")
    issues[0]["status"] = "verified"
    issues[1]["status"] = "verified"
    result2 = evaluate_pass({"round": 2, "layersRun": ["script", "coverage", "graphic"], "issues": issues}, outline=None)
    if not result2["pass"]:
        raise AssertionError(f"全部 verified 后应通过：{result2}")
    checks.append("pass_criteria")
    bad = validate_issue({"layer": "coverage", "type": "bad_type", "severity": "major", "evidence": "x"})
    if not bad:
        raise AssertionError("非法 type 未被发现")
    checks.append("validate")
    return {"ok": True, "selftest": {"script": "bg_review_issues.py", "checks": checks}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_review_issues.py",
        description="BidGenie F6 问题台账：merge/close/verify/status（issues.json + 轮次快照；只写 reviews/**）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    merge = sub.add_parser("merge", help="合并 B/C 层问题（校验/ID/去重/快照）")
    errors.add_common_args(merge, subparser=True)
    merge.add_argument("--workspace", default=argparse.SUPPRESS)
    merge.add_argument("--project", default=argparse.SUPPRESS)
    merge.add_argument("--package", default=argparse.SUPPRESS)
    merge.add_argument("--round", type=int, required=True)
    merge.add_argument("--input", required=True)
    merge.add_argument("--layer", choices=("coverage", "graphic"))
    close = sub.add_parser("close", help="关闭/流转问题状态")
    errors.add_common_args(close, subparser=True)
    close.add_argument("--workspace", default=argparse.SUPPRESS)
    close.add_argument("--project", default=argparse.SUPPRESS)
    close.add_argument("--package", default=argparse.SUPPRESS)
    close.add_argument("--issue", required=True)
    close.add_argument("--status", required=True, choices=("fixing", "fixed", "verified", "wontfix"))
    close.add_argument("--note", default="")
    close.add_argument("--user-confirmed", action="store_true")
    close.add_argument("--review", action="store_true", help="复审方写入 verified（下一轮审查/复核）")
    verify = sub.add_parser("verify", help="计算 4.6 通过判据（只读）")
    errors.add_common_args(verify, subparser=True)
    verify.add_argument("--workspace", default=argparse.SUPPRESS)
    verify.add_argument("--project", default=argparse.SUPPRESS)
    verify.add_argument("--package", default=argparse.SUPPRESS)
    verify.add_argument("--round", type=int)
    status = sub.add_parser("status", help="台账摘要（只读）")
    errors.add_common_args(status, subparser=True)
    status.add_argument("--workspace", default=argparse.SUPPRESS)
    status.add_argument("--project", default=argparse.SUPPRESS)
    status.add_argument("--package", default=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="等价 verify（供 bg_state/skill 调用）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not getattr(args, "project", None):
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, getattr(args, "workspace", None))
    command = getattr(args, "command", None)
    if getattr(args, "check", False) and not command:
        return verify_issues(workspace=workspace, project_dir=project_dir, package_id=getattr(args, "package", None), round_no=None)
    if command == "merge":
        return merge_issues(
            workspace=workspace,
            project_dir=project_dir,
            package_id=getattr(args, "package", None),
            round_no=int(args.round),
            input_path=args.input,
            layer=args.layer,
        )
    if command == "close":
        return close_issue(
            workspace=workspace,
            project_dir=project_dir,
            package_id=getattr(args, "package", None),
            issue_id=args.issue,
            status=args.status,
            note=args.note,
            user_confirmed=bool(args.user_confirmed),
            from_review=bool(args.review),
        )
    if command == "verify":
        return verify_issues(workspace=workspace, project_dir=project_dir, package_id=getattr(args, "package", None), round_no=args.round)
    if command == "status":
        return status_issues(workspace=workspace, project_dir=project_dir, package_id=getattr(args, "package", None))
    raise errors.BgError(errors.E_USAGE, "缺少子命令：merge / close / verify / status", hint="使用 --help 查看用法")


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_review_issues.py",
    )


if __name__ == "__main__":
    sys.exit(main())
