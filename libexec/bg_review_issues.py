#!/usr/bin/env python3
"""BidGenie F6 问题台账（bg_review_issues.py，PRD F6.3–F6.5 / 7.5 / 4.6 / 4.3 T6–T8）。

命令：
    merge   --round <k> --input <json> [--layer coverage|graphic]  合并 B/C 层问题（校验/ID/去重/快照）
    close   --issue <RV-...> --status fixing|fixed|verified|wontfix [--note] [--user-confirmed] [--review]
    verify  [--round <k>]                                          计算 4.6 通过判据（只读）
    status                                                         台账摘要（只读）
    carry-forward --round <k> [--dry-run]                          把上轮未 verified 问题补进当前轮台账（修复路径）
所有写操作只改 reviews/**；state.json 仅由 bg_state.py 写。

P1-3 carry-forward 约定（与 bg_review_script.py 共用同一实现）：
- 新一轮 `bg_review_script.py --round k` 会自动把上一轮台账里未 verified 的条目带入
  `reviews/issues.json`（详情见 `carry_forward_issues`）；
- 台账始终是单一审计链：带入条目保留原 `issueId(RV-<上轮>-<seq>)`，新增
  `carriedFrom/lastRound/recheck{round,previousStatus,result,note}`；
- 每轮快照 `reviews/rounds/round-<k>.issues.json` 保留当轮时点状态；
- `carry-forward` 子命令用于修复：ledger 被旧版本脚本整体重写后，从
  `rounds/round-<k-1>.issues.json` 把丢失条目补回当前轮（幂等）。
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
# P1-3 跨轮 carry-forward：上一轮未 verified 条目带入新一轮并复检
# ---------------------------------------------------------------------------
#: recheck.result 取值（写入 issue.recheck.result，报告与台账共同口径）。
RECHECK_VERIFIED = "verified"                 # 脚本复检未复现 → 自动 verified
RECHECK_PREVIOUS = "verified-previously"      # 上轮已 verified，继续保留
RECHECK_RECURRED = "recurred"                 # 已修复项再次检出 → 复开 open
RECHECK_STILL_OPEN = "still-open"             # 上轮未关闭且仍检出 → 保持 open
RECHECK_REGRESSED = "regressed"               # 已验证项再次检出（回归）→ 复开 open
RECHECK_PENDING_SEMANTIC = "pending-semantic"  # B/C 语义项待复审 close --review
RECHECK_CARRIED_OPEN = "carried-open"         # B/C 语义项上轮未关闭，保持原状态
RECHECK_WONTFIX = "wontfix-carried"           # 用户已确认不修，跨轮保留


def _issue_condition_key(issue: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """问题“条件键”：层 + 类型 + 章节 + 锚点（不含证据文本，用于跨轮同一问题匹配）。"""
    return (
        str(issue.get("layer") or ""),
        str(issue.get("type") or ""),
        str(issue.get("chapterId") or ""),
        str(issue.get("anchorId") or ""),
    )


def _issue_dedupe_key(issue: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """去重键（与 merge 一致）：条件键 + 证据前 120 字。"""
    return (*_issue_condition_key(issue), str(issue.get("evidence") or "")[:120])


def _issue_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _issue_sort_key(issue: Mapping[str, Any]) -> tuple[int, int, int]:
    rank = {"blocker": 0, "major": 1, "minor": 2}.get(str(issue.get("severity") or ""), 3)
    match = ISSUE_ID_RE.match(str(issue.get("issueId") or ""))
    if match:
        return (rank, int(match.group(1)), int(match.group(2)))
    return (rank, 10 ** 6, 0)


def _issue_round(issue: Mapping[str, Any]) -> int | None:
    match = ISSUE_ID_RE.match(str(issue.get("issueId") or ""))
    return int(match.group(1)) if match else None


def sort_issues(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 级别 → 轮次 → 序号 稳定排序（跨轮台账展示顺序）。"""
    rows = [dict(item) for item in issues if isinstance(item, Mapping)]
    rows.sort(key=_issue_sort_key)
    return rows


def assign_issue_ids(
    issues: Sequence[Mapping[str, Any]],
    round_no: int,
    reserved: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """给本轮新检出问题分配 `RV-<round>-<seq4>`。

    ``reserved`` 是本轮已带入台账的条目（carry-forward），它们的旧 issueId 不重排；
    新问题从 ``reserved`` 中同轮号的最大序号继续编号，避免“同轮重跑 + 新检出”撞号。
    """
    rows = [dict(item) for item in issues if isinstance(item, Mapping)]
    max_seq = 0
    for item in list(reserved) + rows:
        match = ISSUE_ID_RE.match(str(item.get("issueId") or ""))
        if match and int(match.group(1)) == round_no:
            max_seq = max(max_seq, int(match.group(2)))
    for index, item in enumerate(rows, start=max_seq + 1):
        item["issueId"] = f"RV-{round_no}-{index:04d}"
    return rows


def load_previous_ledger(package_dir: Path, round_no: int) -> dict[str, Any] | None:
    """读取可带入第 `round_no` 轮的历史台账（carry-forward 数据源）。

    优先级：

    1. ``reviews/issues.json`` 且台账轮次 < round_no → 整表（正常多轮路径）；
    2. ``reviews/issues.json`` 轮次 == round_no → 整表作为“同轮复检”数据源（同一轮重跑不
       整体重写台账：已关闭状态与 B/C 语义问题都不会被冲掉，A 层新检出按条件去重）；
    3. ``reviews/rounds/round-<round_no-1>.issues.json`` 快照（ledger 缺失/损坏/
       被旧版本脚本整体重写后的修复路径）。

    无可用历史（例如第 1 轮首次运行）时返回 None。
    """
    ledger_path = issues_path(package_dir)
    if ledger_path.is_file():
        try:
            document = jsonio.read_json(ledger_path)
        except errors.BgError:
            document = None
        if isinstance(document, Mapping):
            ledger_round = document.get("round")
            if isinstance(ledger_round, int) and not isinstance(ledger_round, bool):
                if ledger_round < round_no:
                    return {
                        "round": ledger_round,
                        "source": f"{ISSUES_REL}@{ledger_round}",
                        "issues": _issue_list(document.get("issues")),
                        "sameRound": False,
                    }
                if ledger_round == round_no:
                    issues = _issue_list(document.get("issues"))
                    if issues:
                        return {
                            "round": round_no,
                            "source": f"{ISSUES_REL}@{round_no}（同轮复检）",
                            "issues": issues,
                            "sameRound": True,
                        }
    if round_no > 1:
        snapshot_rel = f"reviews/rounds/round-{round_no - 1}.issues.json"
        snapshot_path = package_dir / snapshot_rel
        if snapshot_path.is_file():
            try:
                document = jsonio.read_json(snapshot_path)
            except errors.BgError:
                document = None
            if isinstance(document, Mapping):
                snapshot_round = document.get("round")
                return {
                    "round": (
                        int(snapshot_round)
                        if isinstance(snapshot_round, int) and not isinstance(snapshot_round, bool)
                        else round_no - 1
                    ),
                    "source": snapshot_rel,
                    "issues": _issue_list(document.get("issues")),
                    "sameRound": False,
                }
    return None


def carry_forward_issues(
    previous_issues: Sequence[Mapping[str, Any]],
    detected_issues: Sequence[Mapping[str, Any]],
    round_no: int,
    *,
    previous_round: int | None = None,
    same_round: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """把上一轮未 verified 的问题带入第 `round_no` 轮，并按本轮 A 层复检结果更新状态。

    复检规则（确定性、可审计）：

    - ``wontfix``：用户已确认不修，跨轮保留；本轮再次检出也不复开；
    - ``script`` 层（A 层确定性检查）``fixed/fixing/open``：本轮未再检出同一条件 →
      ``verified``（脚本即审查方，逐轮复检等价于“下一轮审查写 verified”）；
      再次检出 → ``open``（复开等待优化再修）；
    - ``script`` 层 ``verified``：再次检出 → ``open``（回归）；否则继续 ``verified``；
    - ``coverage/graphic`` 语义层：脚本无法复检。``fixed`` 保持 ``fixed`` 并标
      ``pending-semantic``，由 B/C 复审 ``close --status verified --review`` 写 verified；
      ``open/fixing`` 保持原状态并标 ``carried-open``；
    - 本轮新检出若与带入条目同条件（层/类型/章节/锚点相同），不再新增副本：保留上轮
      ``issueId`` 作为审计主键，计入 ``suppressed``。

    返回 ``{"carried", "detected", "suppressed", "stats", "summary"}``；``detected``
    为剔除重复后的本轮新问题（调用方随后用 `assign_issue_ids` 分配 issueId）。
    """
    now = now or paths.iso_now()
    detected = [dict(item) for item in detected_issues if isinstance(item, Mapping)]
    detected_exact = {_issue_dedupe_key(item) for item in detected}
    detected_conditions = {_issue_condition_key(item) for item in detected}
    fallback_round = (
        int(previous_round)
        if isinstance(previous_round, int) and not isinstance(previous_round, bool) and previous_round >= 1
        else max(round_no - 1, 1)
    )
    carried: list[dict[str, Any]] = []
    suppressed_conditions: set[tuple[str, str, str, str]] = set()
    matched_issue_id: dict[tuple[str, str, str, str], str] = {}
    stats: dict[str, list[str]] = {
        "verified": [],
        "reopened": [],
        "pendingSemantic": [],
        "carriedOpen": [],
        "wontfixKept": [],
        "keptVerified": [],
    }
    for raw in previous_issues:
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        issue_id = str(entry.get("issueId") or "")
        status = str(entry.get("status") or "open")
        layer = str(entry.get("layer") or "")
        condition = _issue_condition_key(entry)
        rediscovered = _issue_dedupe_key(entry) in detected_exact or condition in detected_conditions
        if rediscovered:
            suppressed_conditions.add(condition)
        carried_from = entry.get("carriedFrom")
        if isinstance(carried_from, int) and not isinstance(carried_from, bool):
            entry["carriedFrom"] = carried_from
        else:
            # 首次带入：carriedFrom 取该 issue 的检出轮次（RV-<round>-<seq4>），
            # 保证“同轮保留”的条目 carriedFrom == 本轮，不误标成更早轮次。
            entry["carriedFrom"] = _issue_round(entry) or fallback_round
        entry["lastRound"] = round_no
        entry["updatedAt"] = now
        recheck: dict[str, Any] = {"round": round_no, "at": now, "previousStatus": status, "result": "", "note": ""}
        if status == "wontfix":
            recheck["result"] = RECHECK_WONTFIX
            recheck["note"] = "用户已确认不修，跨轮保留；本轮再次检出也不复开"
            stats["wontfixKept"].append(issue_id)
        elif status == "verified":
            if rediscovered:
                entry["status"] = "open"
                entry["resolvedAt"] = None
                recheck["result"] = RECHECK_REGRESSED
                recheck["note"] = "上轮已验证的问题本轮再次检出（回归），重新打开"
                stats["reopened"].append(issue_id)
            else:
                recheck["result"] = RECHECK_PREVIOUS
                recheck["note"] = "上轮已 verified，继续保留"
                stats["keptVerified"].append(issue_id)
        elif layer == "script":
            if rediscovered:
                if status in ("fixing", "fixed"):
                    entry["status"] = "open"
                    entry["resolvedAt"] = None
                    recheck["result"] = RECHECK_RECURRED
                    recheck["note"] = f"本轮脚本复检再次检出同一问题，复开（原状态 {status}）"
                    stats["reopened"].append(issue_id)
                else:
                    recheck["result"] = RECHECK_STILL_OPEN
                    recheck["note"] = "上轮未关闭且本轮仍检出，保持 open 等待修复"
                    stats["carriedOpen"].append(issue_id)
            else:
                entry["status"] = "verified"
                entry["resolvedAt"] = now
                recheck["result"] = RECHECK_VERIFIED
                recheck["note"] = "本轮脚本复检未复现同类问题，脚本侧自动 verified"
                stats["verified"].append(issue_id)
        else:
            # coverage / graphic 语义层：脚本不能复检，状态保留到 B/C 复审
            if status in ("open", "fixing"):
                recheck["result"] = RECHECK_CARRIED_OPEN
                recheck["note"] = "上轮未关闭，保留原状态；需优化修复后 close fixed"
                stats["carriedOpen"].append(issue_id)
            else:
                recheck["result"] = RECHECK_PENDING_SEMANTIC
                recheck["note"] = "已修复待复审：B/C 复审通过后 close --status verified --review"
                stats["pendingSemantic"].append(issue_id)
        entry["recheck"] = recheck
        matched_issue_id.setdefault(condition, issue_id)
        carried.append(entry)
    suppressed: list[dict[str, Any]] = []
    kept_detected: list[dict[str, Any]] = []
    for item in detected:
        condition = _issue_condition_key(item)
        if condition in suppressed_conditions:
            suppressed.append(
                {
                    "condition": {
                        "layer": condition[0],
                        "type": condition[1],
                        "chapterId": condition[2],
                        "anchorId": condition[3],
                    },
                    "matchedIssueId": matched_issue_id.get(condition, ""),
                }
            )
            continue
        kept_detected.append(item)
    by_recheck: dict[str, int] = {}
    for entry in carried:
        result_name = str((entry.get("recheck") or {}).get("result") or "unknown")
        by_recheck[result_name] = by_recheck.get(result_name, 0) + 1
    summary = {
        "round": round_no,
        "previousRound": fallback_round,
        "sameRound": bool(same_round),
        "carriedCount": len(carried),
        "detectedCount": len(detected),
        "suppressedCount": len(suppressed),
        "verifiedCount": len(stats["verified"]),
        "reopenedCount": len(stats["reopened"]),
        "pendingSemanticCount": len(stats["pendingSemantic"]),
        "carriedOpenCount": len(stats["carriedOpen"]),
        "wontfixKeptCount": len(stats["wontfixKept"]),
        "keptVerifiedCount": len(stats["keptVerified"]),
        "byRecheck": by_recheck,
    }
    return {"carried": carried, "detected": kept_detected, "suppressed": suppressed, "stats": stats, "summary": summary}


def carry_forward_command(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    round_no: int,
    dry_run: bool,
) -> dict[str, Any]:
    """`carry-forward --round <k>`：把上轮快照/台账中未 verified 条目补进当前轮台账。

    幂等：已存在于台账的 issueId 不重复带入；`--dry-run` 只计算不写盘。
    典型用途：修复旧版本脚本“第 k 轮整体重写 issues.json”造成的台账丢失。
    """
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    document = load_issues(package_dir)
    document_round = document.get("round")
    if int(document_round or 0) != round_no:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"issues.json 当前轮次 {document_round} != --round {round_no}",
            hint="先对当前轮跑 bg_review_script.py --round <k>，或用相同 --round",
            detail={"documentRound": document_round, "requestedRound": round_no},
        )
    # 修复子命令的数据源：优先上一轮快照（旧脚本整体重写台账后，快照仍是唯一完整上一轮台账），
    # 避免同级台账非空时 load_previous_ledger 走“同轮复检”而看不到被冲掉的历史条目。
    previous: dict[str, Any] | None = None
    if round_no > 1:
        snapshot_rel = f"reviews/rounds/round-{round_no - 1}.issues.json"
        snapshot_path = package_dir / snapshot_rel
        if snapshot_path.is_file():
            try:
                snapshot_document = jsonio.read_json(snapshot_path)
            except errors.BgError:
                snapshot_document = None
            if isinstance(snapshot_document, Mapping):
                previous = {
                    "round": round_no - 1,
                    "source": snapshot_rel,
                    "issues": _issue_list(snapshot_document.get("issues")),
                    "sameRound": False,
                }
    if previous is None:
        previous = load_previous_ledger(package_dir, round_no)
    if previous is None:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少可 carry-forward 的上轮台账",
            hint="需要 reviews/rounds/round-<k-1>.issues.json 或轮次更早的 reviews/issues.json",
            detail={"round": round_no},
        )
    existing = _issue_list(document.get("issues"))
    existing_ids = {str(item.get("issueId") or "") for item in existing}
    missing = [item for item in previous["issues"] if str(item.get("issueId") or "") not in existing_ids]
    result = carry_forward_issues(
        missing,
        existing,
        round_no,
        previous_round=previous["round"],
        same_round=bool(previous.get("sameRound")),
    )
    combined = sort_issues(result["carried"] + result["detected"])
    carry_summary = dict(result["summary"])
    carry_summary["source"] = previous["source"]
    carry_summary["at"] = paths.iso_now()
    changed = bool(missing) or bool(result["suppressed"]) or combined != sort_issues(existing)
    if changed:
        document["issues"] = combined
        document["carryForward"] = carry_summary
    else:
        carry_summary["alreadyCarried"] = True
    report_updated = False
    if changed and not dry_run:
        write_issues(package_dir, document)
        report_updated = refresh_report_stats(package_dir, document)
    return {
        "ok": True,
        "command": "review_issues_carry_forward",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "round": round_no,
        "currentStage": state.get("currentStage"),
        "dryRun": bool(dry_run),
        "source": previous["source"],
        "previousRound": previous["round"],
        "carried": result["carried"],
        "carriedIds": [str(item.get("issueId") or "") for item in result["carried"]],
        "suppressed": result["suppressed"],
        "carryForward": carry_summary,
        "summary": document.get("summary") if changed else recalc_summary(existing),
        "changed": changed,
        "reportUpdated": report_updated,
        "issuesPath": ISSUES_REL,
        "snapshotPath": f"reviews/rounds/round-{round_no}.issues.json",
    }


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
    if status not in ("open", "fixing", "fixed", "verified", "wontfix"):
        raise errors.BgError(
            errors.E_USAGE,
            f"--status 非法：{status}",
            detail={"allowed": ["open", "fixing", "fixed", "verified", "wontfix"]},
        )
    allowed = {
        "open": ("fixing", "fixed", "wontfix"),
        "fixing": ("fixed", "wontfix"),
        # fixed/verified 允许复审不通过时复开（--review），避免语义层修复后无法回到 open 的死锁
        "fixed": ("verified", "open"),
        "verified": ("open",),
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
    if status == "open":
        # 复审不通过：把已 fixed/verified 的语义问题复开，等待优化再修；只有复审方能操作
        if not from_review:
            raise errors.BgError(
                errors.E_GUARD_FAILED,
                "复开问题只能由复审方写入（加 --review）",
                hint="复审确认修复不成立时使用：close --status open --review --note 理由",
                detail={"issueId": issue_id, "from": previous, "currentStage": state.get("currentStage")},
            )
        if not note.strip():
            raise errors.BgError(errors.E_USAGE, "复开问题必须填写 --note 理由（复审不成立的依据）", detail={"issueId": issue_id})
    now = paths.iso_now()
    match["status"] = status
    match["updatedAt"] = now
    match["resolutionNote"] = note.strip()
    if status in ("fixed", "verified", "wontfix"):
        match["resolvedAt"] = now
    elif status == "open":
        match["resolvedAt"] = None
    if status == "wontfix":
        match["userConfirmed"] = True
    # 带入条目：保留 carry 时点的 recheck 结果，并记录后续复核结论（台账可审计）
    if match.get("carriedFrom") is not None and isinstance(match.get("recheck"), Mapping):
        recheck = dict(match["recheck"])
        recheck["resolvedStatus"] = status
        recheck["resolvedAt"] = now
        match["recheck"] = recheck
    document["issues"] = issues
    write_issues(package_dir, document)
    optimization_updated = (
        False if status == "open" else record_optimization_round(package_dir, state, target, match, previous, status, note)
    )
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
    """纯函数自检：summary/ID/pass criteria/issue 校验/carry-forward（不写文件）。"""
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
    # P1-3 carry-forward：script 层 fixed 未复现 → verified；复现 → 复开；语义层 → 待复审
    previous = [
        {
            "issueId": "RV-1-0001",
            "layer": "script",
            "type": "word_count_low",
            "severity": "major",
            "chapterId": "1-1",
            "anchorId": None,
            "evidence": "实际 800 字 / 目标 1000（80.0%）低于 90%",
            "status": "fixed",
            "userConfirmed": False,
        },
        {
            "issueId": "RV-1-0002",
            "layer": "script",
            "type": "word_count_low",
            "severity": "major",
            "chapterId": "2-1",
            "anchorId": None,
            "evidence": "实际 900 字 / 目标 1000（90.0%）",
            "status": "fixed",
            "userConfirmed": False,
        },
        {
            "issueId": "RV-1-0003",
            "layer": "graphic",
            "type": "figure_mismatch",
            "severity": "minor",
            "chapterId": "2-3",
            "anchorId": "fig-2-3-02",
            "evidence": "图与正文脱节",
            "status": "fixed",
            "userConfirmed": False,
        },
        {
            "issueId": "RV-1-0004",
            "layer": "script",
            "type": "table_missing",
            "severity": "minor",
            "chapterId": "3-1",
            "anchorId": None,
            "evidence": "缺 ▼表题注",
            "status": "wontfix",
            "userConfirmed": True,
        },
    ]
    detected = [
        {
            "issueId": "",
            "layer": "script",
            "type": "word_count_low",
            "severity": "major",
            "chapterId": "2-1",
            "anchorId": None,
            "evidence": "实际 905 字 / 目标 1000（90.5%）",
        }
    ]
    carried = carry_forward_issues(previous, detected, 2, previous_round=1)
    by_id = {item["issueId"]: item for item in carried["carried"]}
    if by_id["RV-1-0001"]["status"] != "verified" or by_id["RV-1-0001"]["recheck"]["result"] != RECHECK_VERIFIED:
        raise AssertionError(f"未复现的 script fixed 应自动 verified：{by_id['RV-1-0001']}")
    if by_id["RV-1-0002"]["status"] != "open" or by_id["RV-1-0002"]["recheck"]["result"] != RECHECK_RECURRED:
        raise AssertionError(f"复现的 script fixed 应复开：{by_id['RV-1-0002']}")
    if by_id["RV-1-0003"]["status"] != "fixed" or by_id["RV-1-0003"]["recheck"]["result"] != RECHECK_PENDING_SEMANTIC:
        raise AssertionError(f"语义层 fixed 应保持 fixed 待复审：{by_id['RV-1-0003']}")
    if by_id["RV-1-0004"]["status"] != "wontfix" or by_id["RV-1-0004"]["recheck"]["result"] != RECHECK_WONTFIX:
        raise AssertionError(f"wontfix 应跨轮保留：{by_id['RV-1-0004']}")
    if carried["detected"]:
        raise AssertionError(f"复现问题应去重而不是新增副本：{carried['detected']}")
    if carried["summary"]["suppressedCount"] != 1 or carried["summary"]["carriedCount"] != 4:
        raise AssertionError(f"carry-forward 统计不正确：{carried['summary']}")
    checks.append("carry_forward")
    # 同一轮重跑：上一轮数据源只取已 carry 条目（load_previous_ledger 的 round==k 分支由集成测试覆盖）
    rerun = carry_forward_issues(carried["carried"], [], 2, previous_round=1)
    rerun_by_id = {item["issueId"]: item for item in rerun["carried"]}
    if (
        rerun_by_id["RV-1-0001"]["status"] != "verified"
        or rerun_by_id["RV-1-0001"]["carriedFrom"] != 1
        or rerun_by_id["RV-1-0001"]["recheck"]["result"] != RECHECK_PREVIOUS
    ):
        raise AssertionError(f"carry 重跑不稳定：{rerun_by_id['RV-1-0001']}")
    if sort_issues(carried["carried"])[0]["severity"] != "major":
        raise AssertionError("跨轮排序应按级别优先")
    checks.append("carry_rerun")
    # 分支：open+复现 → still-open；verified+复现 → regressed（回归复开）
    branch = carry_forward_issues(
        [
            {"issueId": "RV-1-0005", "layer": "script", "type": "table_missing", "severity": "minor", "chapterId": "3-1", "anchorId": None, "evidence": "缺题注", "status": "open", "userConfirmed": False},
            {"issueId": "RV-1-0006", "layer": "script", "type": "caption_format", "severity": "minor", "chapterId": "3-2", "anchorId": None, "evidence": "题注格式", "status": "verified", "userConfirmed": False},
        ],
        [
            {"layer": "script", "type": "table_missing", "severity": "minor", "chapterId": "3-1", "anchorId": None, "evidence": "缺题注（本轮再次检出）"},
            {"layer": "script", "type": "caption_format", "severity": "minor", "chapterId": "3-2", "anchorId": None, "evidence": "题注格式（本轮再次检出）"},
        ],
        2,
        previous_round=1,
    )
    branch_by_id = {item["issueId"]: item for item in branch["carried"]}
    if branch_by_id["RV-1-0005"]["status"] != "open" or branch_by_id["RV-1-0005"]["recheck"]["result"] != RECHECK_STILL_OPEN:
        raise AssertionError(f"open+复现应保持 open：{branch_by_id['RV-1-0005']}")
    if branch_by_id["RV-1-0006"]["status"] != "open" or branch_by_id["RV-1-0006"]["recheck"]["result"] != RECHECK_REGRESSED:
        raise AssertionError(f"verified+复现应回归复开：{branch_by_id['RV-1-0006']}")
    if branch["summary"]["suppressedCount"] != 2:
        raise AssertionError(f"分支去重计数不正确：{branch['summary']}")
    checks.append("carry_forward_branches")
    # 同轮重跑 + 新检出：新问题 ID 必须从已带入条目的最大序号续号，不能撞 RV-2-0001
    same_round_prev = [
        {
            "issueId": "RV-2-0001",
            "layer": "script",
            "type": "word_count_low",
            "severity": "minor",
            "chapterId": "1-1",
            "anchorId": None,
            "evidence": "同轮已带入的旧问题",
            "status": "fixed",
            "userConfirmed": False,
        }
    ]
    same_round = carry_forward_issues(
        same_round_prev,
        [{"layer": "script", "type": "table_missing", "severity": "minor", "chapterId": "3-1", "anchorId": None, "evidence": "本轮新检出问题"}],
        2,
        previous_round=2,
        same_round=True,
    )
    assigned = assign_issue_ids(same_round["detected"], 2, reserved=same_round["carried"])
    if same_round["carried"][0]["issueId"] != "RV-2-0001" or assigned[0]["issueId"] != "RV-2-0002":
        raise AssertionError(f"同轮重跑 ID 续号不正确：carried={same_round['carried'][0]['issueId']} new={assigned[0]['issueId']}")
    if assigned[0]["issueId"] == same_round["carried"][0]["issueId"]:
        raise AssertionError("同轮重跑出现重复 issueId")
    checks.append("carry_same_round_ids")
    return {"ok": True, "selftest": {"script": "bg_review_issues.py", "checks": checks}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_review_issues.py",
        description="BidGenie F6 问题台账：merge/close/verify/status/carry-forward（issues.json + 轮次快照；只写 reviews/**）",
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
    close.add_argument("--status", required=True, choices=("open", "fixing", "fixed", "verified", "wontfix"))
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
    carry = sub.add_parser("carry-forward", help="把上轮未 verified 问题补进当前轮台账（P1-3 修复路径，幂等）")
    errors.add_common_args(carry, subparser=True)
    carry.add_argument("--workspace", default=argparse.SUPPRESS)
    carry.add_argument("--project", default=argparse.SUPPRESS)
    carry.add_argument("--package", default=argparse.SUPPRESS)
    carry.add_argument("--round", type=int, required=True)
    carry.add_argument("--dry-run", action="store_true", help="只计算不写 issues.json")
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
    if command == "carry-forward":
        return carry_forward_command(
            workspace=workspace,
            project_dir=project_dir,
            package_id=getattr(args, "package", None),
            round_no=int(args.round),
            dry_run=bool(args.dry_run),
        )
    raise errors.BgError(errors.E_USAGE, "缺少子命令：merge / close / verify / status / carry-forward", hint="使用 --help 查看用法")


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_review_issues.py",
    )


if __name__ == "__main__":
    sys.exit(main())
