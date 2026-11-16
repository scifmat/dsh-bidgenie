#!/usr/bin/env python3
"""BidGenie F7.11 工作概览导出（M8-P4，只读聚合）。

汇总：项目/包基础、state.history 阶段迁移、输入摘要、大纲/正文、审查优化、导出信息、
AI 参与与用户确认；缺失字段写「未记录」，输出脱敏。
产出：`<package>/export/工作概览-<项目名>-<ts>.md` + `.docx`（official-overview 样式）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
STAGE_LABELS = {
    "project_init": "F2 项目初始化",
    "info_extract": "F3 关键信息提取",
    "package_prepare": "F4 撰写前准备",
    "outline": "F5-1 大纲编写",
    "draft": "F5-2 正文撰写",
    "review": "F6 审查",
    "optimize": "F6 优化",
    "finalize": "F7 定稿锁定",
    "export": "F7 导出",
    "completed": "完成",
    "aborted": "已中止",
}
SENSITIVE_PATTERNS = (
    (re.compile(r"\b\d{17}[\dXx]\b"), "[身份证已脱敏]"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "[手机号已脱敏]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱已脱敏]"),
    (re.compile(r"(?:银行卡|银行账号|卡号|账号)\s*[:：]?\s*\d{12,}"), "银行卡/账号：[已脱敏]"),
)


def sanitize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def read_json_safe(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return jsonio.read_json(path)
    except errors.BgError:
        return None


def load_events(workspace: Path, project_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    logs_dir = workspace / "dsh" / "logs"
    if not logs_dir.is_dir():
        return events
    for path in sorted(logs_dir.glob("events-*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    continue
                if str(item.get("project") or "") != project_id:
                    continue
                events.append(item)
        except (OSError, json.JSONDecodeError):
            continue
    events.sort(key=lambda item: str(item.get("ts") or ""))
    return events


def aggregate(
    *,
    workspace: Path,
    project_dir: Path,
    package=None,
) -> dict[str, Any]:
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    package_id = str(getattr(package, "packageId", None) or state.get("currentPackage") or "package-1")
    package_dir = next(
        (path for path in sorted((project_dir / "packages").glob("package-*")) if path.is_dir() and path.name.startswith(package_id + "-")),
        None,
    )
    if package_dir is None:
        raise errors.BgError(errors.E_NOT_FOUND, f"找不到包目录：{package_id}", detail={"packageId": package_id})
    package_meta = read_json_safe(package_dir / "package-meta.json") or {}
    outline = read_json_safe(package_dir / "chapters" / "_outline" / "outline.json") or {}
    events = load_events(workspace, str(project.get("projectId") or ""))

    # 项目/包基础
    bidder = project.get("bidderRef") if isinstance(project.get("bidderRef"), Mapping) else {}
    source_files = [item for item in project.get("sourceFiles") or [] if isinstance(item, Mapping)]
    basic = {
        "projectName": sanitize_text(project.get("projectName") or "未记录"),
        "projectNo": sanitize_text(project.get("projectNo") or "未记录"),
        "projectId": sanitize_text(project.get("projectId") or "未记录"),
        "bidderName": sanitize_text(bidder.get("displayName") or "未记录"),
        "bidderCompleteness": {"overall": bidder.get("completeness"), "snapshotHash": bidder.get("snapshotHash", ""), "certificatesHash": bidder.get("certificatesHash", ""), "threshold": 0.7, "warning": isinstance(bidder.get("completeness"), (int, float)) and bidder.get("completeness") < 0.7} if bidder.get("completeness") is not None else "未记录",
        "qualificationGaps": ((project.get("extensions") or {}).get("qualificationGaps") or {}).get(package_id, []) if isinstance((project.get("extensions") or {}).get("qualificationGaps"), Mapping) else "未记录",
        "packageId": package_id,
        "packageName": sanitize_text(package_meta.get("packageName") or "未记录"),
        "packageCount": project.get("packageCount") if isinstance(project.get("packageCount"), int) else "未记录",
        "currentStage": state.get("currentStage") or "未记录",
        "currentScope": state.get("currentScope") or "未记录",
        "revision": state.get("revision") if isinstance(state.get("revision"), int) else "未记录",
        "reviewLoop": state.get("reviewLoop") or {},
        "locks": state.get("locks") or {},
        "createdAt": state.get("createdAt") or "未记录",
        "updatedAt": state.get("updatedAt") or "未记录",
    }

    # 输入摘要
    sources = [{"name": sanitize_text(item.get("originalName")), "hash": sanitize_text(item.get("hash") or "未记录"), "kind": sanitize_text(item.get("kind") or "未记录")} for item in source_files]
    conversion = read_json_safe(project_dir / "sources" / "conversion-manifest.json") or {}
    extraction_common = read_json_safe(project_dir / "extraction" / "common" / "_extraction-report.json") or {}
    extraction_package = read_json_safe(project_dir / "extraction" / "packages" / package_id / "_extraction-report.json") or {}
    conversion_files = conversion.get("files") if isinstance(conversion, Mapping) else None
    file_qualities = [
        str(item.get("quality"))
        for item in conversion_files or []
        if isinstance(item, Mapping) and str(item.get("quality") or "").strip()
    ]
    conversion_quality = conversion.get("quality") or ("、".join(dict.fromkeys(file_qualities)) if file_qualities else "未记录")
    extraction_summary = {
        "commonOk": extraction_common.get("ok", "未记录"),
        "packageOk": extraction_package.get("ok", "未记录"),
        "commonBlockers": len(extraction_common.get("blockers") or []) if isinstance(extraction_common, Mapping) else "未记录",
        "packageBlockers": len(extraction_package.get("blockers") or []) if isinstance(extraction_package, Mapping) else "未记录",
        "conversionQuality": conversion_quality,
        "conversionFiles": len(conversion_files or []) if isinstance(conversion, Mapping) else "未记录",
        "caveats": (project.get("extensions") or {}).get("extractionCaveats") if isinstance(project.get("extensions"), Mapping) else [],
    }
    # 用户回答/valueSource：package-meta 的计划类字段（M8 修复）
    user_answers: list[dict[str, Any]] = []
    value_sources = package_meta.get("valueSource") if isinstance(package_meta.get("valueSource"), Mapping) else {}
    plan_fields = ("expectedWordCount", "personnelPlan", "equipmentPlan", "schedulePlan")
    for field in plan_fields:
        if field not in package_meta:
            continue
        value = package_meta.get(field)
        if isinstance(value, Mapping):
            visible = "；".join(f"{key}：{sanitize_text(item) if str(item).strip() else '未记录'}" for key, item in value.items() if key in ("startDate", "deliveryDate", "constructionPlan"))
        else:
            visible = sanitize_text(value) if str(value).strip() else "未记录"
        user_answers.append({"key": field, "answer": visible or "未记录", "valueSource": sanitize_text(value_sources.get(field) or "未记录")})
    figure_quota = package_meta.get("figureQuota") if isinstance(package_meta.get("figureQuota"), Mapping) else {}
    for figure_type in ("table", "mermaid", "terminal"):
        if figure_type in figure_quota:
            user_answers.append(
                {
                    "key": f"figureQuota.{figure_type}",
                    "answer": sanitize_text(figure_quota.get(figure_type)),
                    "valueSource": sanitize_text(value_sources.get(figure_type) or value_sources.get("figureQuota") or "未记录"),
                }
            )
    # 既有落盘的用户回答
    for container in (project.get("extensions"), package_meta.get("extensions"), state.get("extensions")):
        if not isinstance(container, Mapping):
            continue
        for key, value in container.items():
            if isinstance(value, Mapping) and ("valueSource" in value or key in ("answers", "userAnswers", "prepareAnswers")):
                for question, answer in value.items():
                    if isinstance(answer, Mapping):
                        user_answers.append({"key": f"{key}.{question}", "valueSource": sanitize_text(answer.get("valueSource") or "未记录"), "answer": sanitize_text(answer.get("value") or answer.get("answer") or "未记录")})
    pending = state.get("pendingInput") if isinstance(state.get("pendingInput"), Mapping) else {}
    if pending.get("questions"):
        for question in pending.get("questions") or []:
            if isinstance(question, Mapping):
                user_answers.append({"key": sanitize_text(question.get("id") or question.get("question") or "pendingInput"), "valueSource": "待用户回答", "answer": "未记录"})

    # 大纲/正文
    nodes = list(outline_validate.iter_preorder(outline.get("chapters") or []))
    leaves = [node for node in nodes if not (isinstance(node.get("children"), list) and node.get("children"))]
    parents = [node for node in nodes if isinstance(node.get("children"), list) and node.get("children")]
    stats = outline.get("stats") if isinstance(outline.get("stats"), Mapping) else {}
    criteria = [item for item in outline.get("criterionIndex") or [] if isinstance(item, Mapping)]
    covered = 0
    for criterion in criteria:
        criterion_id = str(criterion.get("criterionId") or "")
        if any(any(isinstance(ref, Mapping) and str(ref.get("criterionId")) == criterion_id for ref in leaf.get("criterionRefs") or []) for leaf in leaves):
            covered += 1
    raw_figure_planned = stats.get("figurePlanned") or package_meta.get("figureQuota") or {}
    if isinstance(raw_figure_planned, Mapping):
        figure_planned = {key: value for key, value in raw_figure_planned.items() if key != "network"}
        network_removed = int(raw_figure_planned.get("network") or 0)
    else:
        figure_planned = raw_figure_planned
        network_removed = 0
    outline_summary = {
        "parentCount": len(parents),
        "leafCount": len(leaves),
        "plannedWordCount": stats.get("plannedWordTotal") or stats.get("plannedWordCount") or stats.get("totalWordTarget") or "未记录",
        "actualWordCount": stats.get("writtenWordTotal") or stats.get("actualWordCount") or "未记录",
        "figurePlanned": figure_planned,
        "networkRemoved": network_removed,
        "criterionCount": len(criteria),
        "criterionCovered": covered,
    }
    completions = [node.get("completion") for node in leaves if isinstance(node.get("completion"), Mapping)]
    body_summary = {
        "doneCount": sum(1 for item in completions if item.get("status") == "done"),
        "skippedCount": sum(1 for item in completions if item.get("status") == "skipped"),
        "blockedCount": sum(1 for item in completions if item.get("status") == "blocked"),
        "actualWordCount": sum(int(item.get("wordCount") or 0) for item in completions),
    }
    render_manifest = read_json_safe(package_dir / "resources" / "render-manifest.json") or {}
    anchors = render_manifest.get("anchors") if isinstance(render_manifest.get("anchors"), Mapping) else {}
    body_summary["figureProduced"] = sum(1 for item in anchors.values() if isinstance(item, Mapping) and item.get("status") == "produced") if anchors else "未记录"

    # 审查/优化
    issues = read_json_safe(package_dir / "reviews" / "issues.json") or {}
    issue_list = [item for item in issues.get("issues") or [] if isinstance(item, Mapping)]
    rounds: list[dict[str, Any]] = []
    for snapshot in sorted((package_dir / "reviews" / "rounds").glob("round-*.issues.json")):
        data = read_json_safe(snapshot) or {}
        rounds.append(
            {
                "round": int(data.get("round") or 0),
                "summary": data.get("summary") or {},
                "layersRun": data.get("layersRun") or [],
                "generatedAt": sanitize_text(data.get("generatedAt") or "未记录"),
            }
        )
    ai_changes: list[dict[str, Any]] = []
    user_confirmed: list[dict[str, Any]] = []
    seen_ai: set[tuple[str, str]] = set()
    all_issue_lists: list[list[Mapping[str, Any]]] = [issue_list]
    for snapshot in sorted((package_dir / "reviews" / "rounds").glob("round-*.issues.json")):
        data = read_json_safe(snapshot) or {}
        all_issue_lists.append([item for item in data.get("issues") or [] if isinstance(item, Mapping)])
    for issue_items in all_issue_lists:
        for issue in issue_items:
            issue_id = str(issue.get("issueId") or "")
            note = sanitize_text(issue.get("resolutionNote") or "")
            key = (issue_id, note)
            if note and key not in seen_ai:
                ai_changes.append({"issueId": issue_id, "type": issue.get("type"), "note": note})
                seen_ai.add(key)
            if issue.get("userConfirmed"):
                user_confirmed.append({"issueId": issue_id, "type": issue.get("type"), "status": issue.get("status")})
    decision_events = [
        item
        for item in events
        if str(item.get("actor") or "") in ("agent", "user", "script") and str(item.get("event") or "") in (
            "state.transition",
            "review.issues",
            "review.not_converged",
            "optimize.completed",
            "export.completed",
            "export.failed",
            "chapter.completed",
            "chapter.blocked",
            "outline.generated",
        )
    ]

    # 导出信息
    export_report = read_json_safe(package_dir / "export" / "_last-export-report.json") or {}
    frozen = read_json_safe(package_dir / "export" / "_frozen" / "manifest.json") or {}
    export_info = {
        "exportPath": sanitize_text(package_meta.get("exportPath") or "未记录"),
        "exportStyleProfile": sanitize_text(package_meta.get("exportStyleProfile") or "未记录"),
        "lastExportedAt": sanitize_text(export_report.get("exportedAt") or "未记录"),
        "verifyOk": export_report.get("verifyOk") if export_report.get("verifyOk") is not None else "未记录",
        "verifyFindings": len(export_report.get("verifyFindings") or []) if isinstance(export_report, Mapping) else "未记录",
        "frozenFileCount": frozen.get("fileCount") or "未记录",
        "frozenRootHash": sanitize_text(frozen.get("rootHash") or "未记录"),
        "locked": bool((state.get("locks") or {}).get("draftLocked")),
        "lockedAt": sanitize_text((state.get("locks") or {}).get("lockedAt") or "未记录"),
    }
    tech_docx_path = str(export_report.get("docxPath") or "")
    tech_docx_abs = project_dir / tech_docx_path if tech_docx_path else None
    tech_sha = ""
    if tech_docx_abs is not None and tech_docx_abs.is_file():
        tech_sha = "sha256:" + hashlib.sha256(tech_docx_abs.read_bytes()).hexdigest()
    export_info.update(
        {
            "techDocxPath": tech_docx_path or "未记录",
            "techDocxSha256": tech_sha or "未记录",
            "techExportedAt": sanitize_text(export_report.get("exportedAt") or "未记录"),
            "techVerifyOk": export_report.get("verifyOk") if export_report.get("mode") == "tech" else "未记录",
            "techVerifyFindings": export_report.get("verifyFindings") if export_report.get("mode") == "tech" else "未记录",
            "techMode": export_report.get("mode") or "未记录",
        }
    )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": paths.iso_now(),
        "basic": basic,
        "sources": sources,
        "conversion": extraction_summary,
        "userAnswers": user_answers[:50],
        "outline": outline_summary,
        "body": body_summary,
        "history": [item for item in state.get("history") or [] if isinstance(item, Mapping)],
        "completedStages": [item for item in state.get("completedStages") or [] if isinstance(item, Mapping)],
        "events": events,
        "decisionEvents": decision_events[-50:],
        "issues": {
            "summary": issues.get("summary") or {},
            "layersRun": issues.get("layersRun") or [],
            "rounds": rounds,
            "currentCount": len(issue_list),
        },
        "aiChanges": ai_changes[:100],
        "userConfirmed": user_confirmed[:100],
        "export": export_info,
    }


def render_markdown(aggregate_data: Mapping[str, Any]) -> str:
    basic = aggregate_data["basic"]
    lines: list[str] = []
    lines.append(f"# 工作概览：{basic['projectName']}")
    lines.append("")
    lines.append(f"- 生成时间：{aggregate_data['generatedAt']}")
    lines.append(f"- 项目编号：{basic['projectNo']}")
    lines.append(f"- 投标人：{basic['bidderName']}")
    completeness = basic.get("bidderCompleteness")
    if isinstance(completeness, Mapping):
        lines.append(f"- 投标人完整度：{completeness.get('overall', '未记录')}（阈值 {completeness.get('threshold', '未记录')}；warning={completeness.get('warning', '未记录')}）")
    else:
        lines.append("- 投标人完整度：未记录")
    gaps = basic.get("qualificationGaps") if isinstance(basic.get("qualificationGaps"), list) else []
    lines.append(f"- 资质缺口提示：{len(gaps)} 项" if gaps else "- 资质缺口提示：无记录")
    lines.append(f"- 包：{basic['packageId']} / {basic['packageName']}（共 {basic['packageCount']} 包）")
    lines.append(f"- 当前阶段：{STAGE_LABELS.get(str(basic['currentStage']), basic['currentStage'])}（{basic['currentStage']}），revision={basic['revision']}")
    lines.append(f"- 审查轮次：{basic['reviewLoop'].get('round', '未记录')}/{basic['reviewLoop'].get('maxRounds', '未记录')}；锁定：{'是' if basic['locks'].get('draftLocked') else '否'}")
    lines.append("")
    lines.append("## 1. 输入摘要")
    lines.append("")
    lines.append(f"- 招标文件：{len(aggregate_data['sources'])} 个")
    for source in aggregate_data["sources"]:
        lines.append(f"  - {source['name']}（{source['kind']}，hash={source['hash']}）")
    conversion = aggregate_data["conversion"]
    lines.append(f"- 转换质量：{conversion['conversionQuality']}；转换文件 {conversion['conversionFiles']} 个")
    caveats = conversion.get("caveats") if isinstance(conversion.get("caveats"), list) else []
    if caveats:
        lines.append(f"- 提取 caveats（{len(caveats)}）：")
        for item in caveats[:10]:
            if isinstance(item, Mapping):
                lines.append(f"  - {sanitize_text(item.get('text'))}（来源：{sanitize_text(item.get('source') or '未记录')}）")
    else:
        lines.append("- 提取 caveats：无（project.extensions.extractionCaveats 为空）")
    lines.append(f"- 提取校验：common ok={conversion['commonOk']} blockers={conversion['commonBlockers']}；package ok={conversion['packageOk']} blockers={conversion['packageBlockers']}")
    lines.append("")
    lines.append("### 用户回答与 valueSource")
    lines.append("")
    if aggregate_data["userAnswers"]:
        lines.append("| 问题 | 回答 | valueSource |")
        lines.append("|---|---|---|")
        for item in aggregate_data["userAnswers"]:
            lines.append(f"| {item['key']} | {item['answer']} | {item['valueSource']} |")
    else:
        lines.append("未记录（未发现落盘的用户回答/valueSource）")
    lines.append("")
    lines.append("## 2. 大纲与正文")
    lines.append("")
    outline = aggregate_data["outline"]
    body = aggregate_data["body"]
    lines.append(f"- 大纲：父级 {outline['parentCount']} 个，末级 {outline['leafCount']} 个；计划字数 {outline['plannedWordCount']}；实际字数 {outline['actualWordCount']}")
    lines.append(f"- 评分覆盖：criterion {outline['criterionCovered']}/{outline['criterionCount']}")
    lines.append(f"- 图表计划（不含已移除的网络配图）：{outline['figurePlanned']}" + (f"；旧数据 network 已移除 {outline['networkRemoved']} 张" if outline.get("networkRemoved") else ""))
    lines.append(f"- 正文：done {body['doneCount']}，skipped {body['skippedCount']}，blocked {body['blockedCount']}，字数 {body['actualWordCount']}，图表产出 {body['figureProduced']}")
    lines.append("")
    lines.append("## 3. 阶段与状态迁移（state.history）")
    lines.append("")
    history = aggregate_data["history"]
    if history:
        lines.append("| 时间 | 从 | 到 | actor | reason | revision |")
        lines.append("|---|---|---|---|---|---|")
        for item in history:
            lines.append(
                f"| {item.get('at', '未记录')} | {item.get('from', '')} | {item.get('to', '')} | {item.get('actor', '')} | {sanitize_text(item.get('reason', ''))} | {item.get('revision', '')} |"
            )
    else:
        lines.append("未记录")
    completed = aggregate_data["completedStages"]
    if completed:
        lines.append("")
        lines.append("已完成阶段：" + "；".join(f"{item.get('stage')}（{item.get('scope')}，{item.get('completedAt', '未记录')}）" for item in completed))
    lines.append("")
    lines.append("## 4. 审查与优化")
    lines.append("")
    issues = aggregate_data["issues"]
    summary = issues["summary"]
    lines.append(f"- issues 当前统计：blocker {summary.get('blocker', 0)} / major {summary.get('major', 0)} / minor {summary.get('minor', 0)} / open {summary.get('openCount', 0)} / verified {summary.get('verifiedCount', 0)} / wontfix {summary.get('wontfixCount', 0)}")
    lines.append(f"- layersRun：{issues['layersRun'] or '未记录'}")
    if issues["rounds"]:
        lines.append("")
        lines.append("| 轮次 | blocker | major | minor | open | 时间 |")
        lines.append("|---|---|---|---|---|---|")
        for item in issues["rounds"]:
            summary_item = item.get("summary") or {}
            lines.append(f"| {item['round']} | {summary_item.get('blocker', 0)} | {summary_item.get('major', 0)} | {summary_item.get('minor', 0)} | {summary_item.get('openCount', 0)} | {item['generatedAt']} |")
    else:
        lines.append("- 轮次快照：未记录")
    lines.append("")
    lines.append("### AI 修改与用户确认")
    lines.append("")
    if aggregate_data["aiChanges"]:
        for item in aggregate_data["aiChanges"][:20]:
            lines.append(f"- {item['issueId']}（{item['type']}）：{item['note']}")
    else:
        lines.append("- AI 自动修改记录：未记录")
    if aggregate_data["userConfirmed"]:
        lines.append("- 用户确认：")
        for item in aggregate_data["userConfirmed"][:20]:
            lines.append(f"  - {item['issueId']}（{item['type']}，status={item['status']}）")
    else:
        lines.append("- 用户确认记录：未记录")
    package_confirmations = [item for item in aggregate_data.get("events") or [] if isinstance(item, Mapping) and item.get("event") == "package.confirmed"]
    lines.append(f"- 用户选包确认（package.confirmed）：{len(package_confirmations)} 次" + ("（actor=user）" if any(item.get("actor") == "user" for item in package_confirmations) else ""))
    lines.append("")
    lines.append("## 5. 导出信息")
    lines.append("")
    export = aggregate_data["export"]
    lines.append(f"- 最近导出：{export['exportPath']}")
    lines.append(f"- 样式：{export['exportStyleProfile']}；导出时间：{export['lastExportedAt']}")
    lines.append(f"- 导出自检：verifyOk={export['verifyOk']}，findings={export['verifyFindings']}")
    lines.append(f"- 冻结清单：fileCount={export['frozenFileCount']}，rootHash={export['frozenRootHash']}；锁定={export['locked']}，lockedAt={export['lockedAt']}")
    lines.append("")
    lines.append("## 6. AI 参与说明")
    lines.append("")
    lines.append("- 本概览由 `bg_export_overview.py` 只读聚合 state/events/extraction/outline/reviews/export 数据生成；不修改状态机。")
    lines.append("- 大纲、正文、审查问题、优化修改由 AI 生成/建议，用户确认记录以 issues.userConfirmed 与事件 actor=user 为准；未确认内容不推断。")
    lines.append("- 缺失字段一律标注「未记录」，不写入敏感信息；身份证/手机号/邮箱等已脱敏。")
    lines.append("")
    lines.append("## 7. 数据缺口")
    lines.append("")
    missing: list[str] = []
    if not aggregate_data["userAnswers"]:
        missing.append("用户回答/valueSource")
    if not aggregate_data["issues"]["rounds"]:
        missing.append("审查轮次快照")
    if not aggregate_data["events"]:
        missing.append("events.jsonl 事件")
    if not aggregate_data["history"]:
        missing.append("state.history 迁移记录")
    if not aggregate_data["aiChanges"]:
        missing.append("AI 自动修改记录")
    if aggregate_data["conversion"].get("conversionQuality") == "未记录":
        missing.append("转换质量（conversion-manifest files[].quality 未记录）")
    if aggregate_data["conversion"].get("conversionFiles") in (0, "未记录"):
        missing.append("转换文件清单")
    lines.append("；".join(missing) if missing else "无（本概览所需字段均已记录）")
    lines.extend(["", "---", "", "> 本文件为工作概览，不并入技术方案正文；如发现内容与业务事实不符，请以项目原始记录为准。"])
    return "\n".join(lines) + "\n"


def resolved_overview_style(workspace: Path, project_dir: Path, package_dir: Path, package_id: str) -> tuple[dict[str, Any], list[str]]:
    import bg_style_merge

    result = bg_style_merge.merge_profile("official-overview", workspace=workspace, project_dir=project_dir, package_id=package_id)
    return dict(result["document"].get("params") or {}), list(result.get("warnings") or [])


def _write_text_atomic(target: Path, text: str) -> None:
    temp = target.with_name(f"{target.name}.tmp-{paths.rand4()}")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _save_docx_atomic(builder: Any, target: Path, sanitizer: Any) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        temp = target.with_name(f"{target.name}.tmp-{paths.rand4()}")
        try:
            builder.save(temp)
            sanitizer(temp)
            os.replace(temp, target)
            return
        except (OSError, PermissionError) as exc:
            last_error = exc
            temp.unlink(missing_ok=True)
            time.sleep(0.15 * (attempt + 1))
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    for leftover in target.parent.glob(f"{target.name}.tmp-*"):
        leftover.unlink(missing_ok=True)
    raise errors.BgError(
        errors.E_IO,
        f"工作概览 docx 原子写入失败：{target.name}",
        hint="检查文件占用/权限后重试；临时文件已清理",
        detail={"path": str(target), "error": type(last_error).__name__ if last_error else "unknown"},
    )


def export_overview(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
    out_dir: str | None = None,
    keep_history: int = 1,
) -> dict[str, Any]:
    package_id = str(package_id or state_lib.load_state(project_dir).get("currentPackage") or "package-1")
    package_dir = next(
        (path for path in sorted((project_dir / "packages").glob("package-*")) if path.is_dir() and path.name.startswith(package_id + "-")),
        None,
    )
    if package_dir is None:
        raise errors.BgError(errors.E_NOT_FOUND, f"找不到包目录：{package_id}", detail={"packageId": package_id})
    aggregate_data = aggregate(workspace=workspace, project_dir=project_dir, package=type("P", (), {"packageId": package_id})())
    state_doc = state_lib.load_state(project_dir)
    event_log.log_event(
        workspace,
        "export.started",
        actor="script",
        project=str(state_doc.get("projectId") or ""),
        package=package_id,
        detail={"mode": "overview", "packageId": package_id},
    )
    markdown = render_markdown(aggregate_data)
    target_dir = Path(out_dir).expanduser() if out_dir else package_dir / "export"
    if not target_dir.is_absolute():
        target_dir = (Path.cwd() / target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = paths.safe_component(aggregate_data["basic"]["projectName"]) or "项目"
    stamp = paths.timestamp14()
    md_path = target_dir / f"工作概览-{safe_name}-{stamp}.md"
    docx_path = target_dir / f"工作概览-{safe_name}-{stamp}.docx"
    _write_text_atomic(md_path, markdown)

    params, warnings = resolved_overview_style(workspace, project_dir, package_dir, package_id)
    import bg_export_docx as exporter

    builder = exporter.KeyInfoDocBuilder(params)
    builder.add_title_block(str(aggregate_data["basic"]["projectName"]), str(aggregate_data["basic"]["projectNo"]), str(aggregate_data["generatedAt"]))

    # 逐节渲染（避免把 md 的一级标题重复为标题）
    body_md = markdown
    first_heading_end = body_md.find("\n## ")
    if first_heading_end > 0:
        body_md = body_md[first_heading_end + 1 :]
    builder.render_markdown(body_md)
    _save_docx_atomic(builder, docx_path, exporter.sanitize_docx_xml)
    cleanup = {"removed": [], "kept": []}
    if not out_dir:
        md_cleanup = exporter.cleanup_export_family(target_dir, ["工作概览-*.md"], keep_history)
        docx_cleanup = exporter.cleanup_export_family(target_dir, ["工作概览-*.docx"], keep_history)
        cleanup = {"removed": [*md_cleanup["removed"], *docx_cleanup["removed"]], "kept": [*md_cleanup["kept"], *docx_cleanup["kept"]]}
    overview_report = {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "overview",
        "generatedAt": aggregate_data["generatedAt"],
        "overviewDocxPath": paths.rel_posix(project_dir, docx_path) if docx_path.is_relative_to(project_dir) else str(docx_path),
        "overviewMdPath": paths.rel_posix(project_dir, md_path) if md_path.is_relative_to(project_dir) else str(md_path),
        "techDocxPath": aggregate_data["export"].get("techDocxPath"),
        "techDocxSha256": aggregate_data["export"].get("techDocxSha256"),
        "techExportedAt": aggregate_data["export"].get("techExportedAt"),
        "techVerifyOk": aggregate_data["export"].get("techVerifyOk"),
        "techVerifyFindings": aggregate_data["export"].get("techVerifyFindings"),
    }
    jsonio.write_json_atomic(target_dir / "_last-overview-report.json", overview_report)
    event_log.log_event(
        workspace,
        "export.verified",
        actor="script",
        project=str(state_doc.get("projectId") or ""),
        package=package_id,
        detail={"mode": "overview", "packageId": package_id, "docxPath": str(docx_path), "cleanupRemoved": len(cleanup.get("removed") or [])},
    )
    try:
        import bg_run_manifest

        bg_run_manifest.record_export(workspace=workspace, project_dir=project_dir, package_id=package_id, kind="overview", docx_path=docx_path)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "command": "export_overview",
        "projectId": sanitize_text(aggregate_data["basic"]["projectId"]),
        "packageId": package_id,
        "mdPath": paths.rel_posix(project_dir, md_path) if md_path.is_relative_to(project_dir) else str(md_path),
        "docxPath": paths.rel_posix(project_dir, docx_path) if docx_path.is_relative_to(project_dir) else str(docx_path),
        "absoluteMdPath": str(md_path),
        "absoluteDocxPath": str(docx_path),
        "cleanup": cleanup,
        "generatedAt": aggregate_data["generatedAt"],
        "sections": ["输入摘要", "大纲与正文", "阶段与状态迁移", "审查与优化", "导出信息", "AI 参与说明", "数据缺口"],
        "stats": {
            "sources": len(aggregate_data["sources"]),
            "events": len(aggregate_data["events"]),
            "history": len(aggregate_data["history"]),
            "issues": aggregate_data["issues"]["currentCount"],
            "rounds": len(aggregate_data["issues"]["rounds"]),
            "aiChanges": len(aggregate_data["aiChanges"]),
            "userConfirmed": len(aggregate_data["userConfirmed"]),
            "userAnswers": len(aggregate_data["userAnswers"]),
        },
        "warnings": warnings,
        "styleProfile": "official-overview",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_export_overview.py",
        description="BidGenie F7.11 工作概览 md/docx 导出（只读聚合；缺失记「未记录」，脱敏）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--out-dir", help="输出目录（缺省 <package>/export）")
    parser.add_argument("--keep-history", type=int, default=1, help="默认输出目录保留历史份数（默认 1，只留最新）")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return export_overview(workspace=workspace, project_dir=project_dir, package_id=args.package, out_dir=args.out_dir, keep_history=int(getattr(args, "keep_history", 1) or 1))


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_export_overview.py",
    )


if __name__ == "__main__":
    sys.exit(main())
