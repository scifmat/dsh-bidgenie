#!/usr/bin/env python3
"""BidGenie F5-2 章节结果合并（单写者，PRD F5.2.3–F5.2.6 / 7.4 / 4.3 T5）。

子智能体只写自己的章节 md + `chapters/_results/<chapterId>.chapter-result.json`；
本脚本串行读取结果、校验契约、更新 `chapters/_outline/outline.json` 的
`completion`（status/wordCount/summary/skipReason/blockReason/updatedAt）与 `reviewState`，
重算 stats（含实际字数/完成数），备份后原子写，并记 `chapter.completed/skipped/blocked` 事件。

CLI：
    --status                     只读进度（leaf 状态/结果文件/blocked/pending）
    --verify-all                 只读校验全部 leaf 完成（done/skipped、无 blocked、summary/wordCount）
    --chapter <id>               合并指定章节结果
    --batch <k>                  合并第 k 批（state.extensions.draftBatches[k-1]；无批次记录时合并全部可用结果）
    --result <file>              合并指定结果文件
    （无选择器 = 合并 _results/ 下全部可用结果）
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
SUMMARY_MIN = 200
SUMMARY_MAX = 400
RESULT_SUFFIX = ".chapter-result.json"
RESULTS_DIR = "chapters/_results"
LOCK_MARKER = "chapters/.locked"
STATUS_MAP = {"done": "done", "skipped": "skipped", "blocked": "blocked", "partial": "blocked"}
FIGURE_STATUS = ("produced", "failed", "skipped")


# ---------------------------------------------------------------------------
# 读取/定位
# ---------------------------------------------------------------------------
def resolve_context(
    workspace: str | Path | None, project: str | Path | None, package_id: str | None
) -> tuple[Path, Path, str, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """返回 (workspace, project_dir, package_id, package_dir, package_meta, state, project_doc)。"""
    return outline_validate.resolve_package_context(workspace, project, package_id)


def outline_path(package_dir: Path) -> Path:
    return outline_validate.outline_path(package_dir)


def load_outline(package_dir: Path) -> dict[str, Any]:
    path = outline_path(package_dir)
    if not path.is_file():
        legacy = outline_validate.legacy_outline_path(package_dir)
        if legacy.is_file():
            raise errors.BgError(
                errors.E_NOT_FOUND,
                "outline.json 仍在包根旧位置（M5.1 前布局）",
                hint="先运行 bg_outline_apply.py --migrate-storage",
                detail={"legacyPath": legacy.name},
            )
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少 chapters/_outline/outline.json",
            hint="先完成 M5 大纲落盘（bg_outline_apply.py）",
            detail={"packageDir": package_dir.name},
        )
    document = jsonio.read_json(path)
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "outline.json 必须是 JSON 对象")
    return dict(document)


def leaf_nodes(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = outline_validate.iter_preorder(document.get("chapters") or [])
    return [node for node in nodes if not (isinstance(node.get("children"), list) and node.get("children"))]


def results_dir(package_dir: Path) -> Path:
    return package_dir / RESULTS_DIR


def result_file(package_dir: Path, chapter_id: str) -> Path:
    return results_dir(package_dir) / f"{chapter_id}{RESULT_SUFFIX}"


def available_results(package_dir: Path) -> list[Path]:
    directory = results_dir(package_dir)
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.glob(f"*{RESULT_SUFFIX}") if path.is_file() and not path.name.startswith(".")
    )


def _default_completion() -> dict[str, Any]:
    return {
        "status": "pending",
        "wordCount": 0,
        "updatedAt": "",
        "summary": "",
        "skipReason": "",
        "blockReason": "",
    }


# ---------------------------------------------------------------------------
# 结果校验
# ---------------------------------------------------------------------------
def validate_result(result: Any, node: Mapping[str, Any], package_dir: Path) -> list[dict[str, Any]]:
    """校验 chapter-result.json（F5.2.3）；返回 findings（severity=blocker）。"""
    findings: list[dict[str, Any]] = []

    def add(code: str, message: str) -> None:
        findings.append({"code": code, "severity": "blocker", "message": message, "chapterId": node.get("id")})

    if not isinstance(result, Mapping):
        add("result_not_object", "chapter-result.json 必须是 JSON 对象")
        return findings
    chapter_id = result.get("chapterId")
    if chapter_id != node.get("id"):
        add("chapter_id_mismatch", f"chapterId 必须为 {node.get('id')!r}，实际 {chapter_id!r}")
    status = result.get("status")
    if status not in STATUS_MAP:
        add("status_invalid", f"status 必须是 done/skipped/blocked/partial，实际 {status!r}")
        return findings
    mapped = STATUS_MAP[str(status)]
    word_count = result.get("wordCount")
    if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count < 0:
        add("word_count_invalid", "wordCount 必须是非负整数")
    elif mapped == "done" and word_count <= 0:
        add("word_count_zero", "status=done 时 wordCount 必须 >0")
    summary = result.get("summary")
    if mapped == "done":
        if not isinstance(summary, str) or not summary.strip():
            add("summary_missing", "status=done 时 summary 必填（200–400 字重点总结）")
        elif not (SUMMARY_MIN <= len(summary.strip()) <= SUMMARY_MAX):
            add("summary_length", f"status=done 时 summary 必须 200–400 字（实际 {len(summary.strip())}）")
    elif summary is not None and not isinstance(summary, str):
        add("summary_invalid", "summary 必须是字符串")
    if status == "skipped":
        reason = result.get("skipReason") or result.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            add("skip_reason_missing", "status=skipped 必须填写 skipReason（用户确认跳过理由）")
    if mapped == "blocked":
        reason = result.get("blockReason") or result.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            add("block_reason_missing", "status=blocked/partial 必须填写 blockReason")
        questions = result.get("questions")
        if questions is not None and not isinstance(questions, list):
            add("questions_invalid", "questions 必须是数组（子智能体问题回传主 Agent）")
    figures = result.get("figures")
    if figures is None:
        figures = result.get("figureResults")
    if figures is not None:
        if not isinstance(figures, list):
            add("figures_invalid", "figures 必须是数组")
        else:
            plan_anchors = {
                str(plan.get("anchorId")): str(plan.get("type"))
                for plan in node.get("figurePlan") or []
                if isinstance(plan, Mapping)
            }
            seen: set[str] = set()
            for index, figure in enumerate(figures):
                if not isinstance(figure, Mapping):
                    add("figure_invalid", f"figures[{index}] 必须是对象")
                    continue
                anchor = figure.get("anchorId")
                if not isinstance(anchor, str) or not anchor:
                    add("figure_anchor_missing", f"figures[{index}].anchorId 不能为空")
                else:
                    if anchor in seen:
                        add("figure_anchor_duplicate", f"figures anchorId 重复：{anchor}")
                    seen.add(anchor)
                    if plan_anchors and anchor not in plan_anchors:
                        add("figure_anchor_unknown", f"figures anchorId 不属于本章 figurePlan：{anchor}")
                fig_status = figure.get("status")
                if fig_status not in FIGURE_STATUS:
                    add("figure_status_invalid", f"figures[{index}].status 必须是 {FIGURE_STATUS}")
                for key in ("resourcePath", "sourcePath"):
                    value = figure.get(key)
                    if value is not None and not isinstance(value, str):
                        add("figure_path_invalid", f"figures[{index}].{key} 必须是字符串")
    for key in ("citations", "issues"):
        value = result.get(key)
        if value is not None and not isinstance(value, list):
            add(f"{key}_invalid", f"{key} 必须是数组")
    headings = result.get("headingLevelsUsed")
    if headings is not None and (not isinstance(headings, list) or any(not isinstance(item, int) for item in headings)):
        add("heading_levels_invalid", "headingLevelsUsed 必须是整数数组")
    return findings


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def _select_results(
    package_dir: Path,
    state: Mapping[str, Any],
    *,
    chapter: str | None = None,
    result_path: str | None = None,
    batch: int | None = None,
) -> list[Path]:
    if result_path:
        path = Path(result_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise errors.BgError(errors.E_NOT_FOUND, f"结果文件不存在：{result_path}", detail={"path": str(path)})
        return [path]
    if chapter:
        path = result_file(package_dir, chapter)
        if not path.is_file():
            raise errors.BgError(
                errors.E_NOT_FOUND,
                f"缺少章节结果：{path.name}",
                hint="子智能体写 chapters/_results/<chapterId>.chapter-result.json 后再合并",
                detail={"chapterId": chapter},
            )
        return [path]
    all_results = available_results(package_dir)
    if batch is not None:
        batches = (state.get("extensions") or {}).get("draftBatches") if isinstance(state.get("extensions"), Mapping) else None
        if isinstance(batches, list) and 1 <= batch <= len(batches):
            ids = [str(item) for item in batches[batch - 1] if isinstance(item, (str, int))]
            selected = [result_file(package_dir, chapter_id) for chapter_id in ids]
            return [path for path in selected if path.is_file()]
        return all_results
    return all_results


def sync_figure_index(outline: dict[str, Any], package_dir: Path, project_dir: Path) -> None:
    """按当前资源文件同步 figureIndex（可选；不复制 figurePlan，不改正文）。"""
    index: list[dict[str, Any]] = []
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        for plan in node.get("figurePlan") or []:
            if not isinstance(plan, Mapping):
                continue
            anchor_id = str(plan.get("anchorId") or "")
            figure_type = str(plan.get("type") or "")
            if not anchor_id or not figure_type:
                continue
            resource_path = ""
            source_path = ""
            status = "planned"
            if figure_type != "table":
                png = package_dir / "resources" / figure_type / f"{anchor_id}.png"
                if png.is_file():
                    status = "produced"
                    resource_path = paths.rel_posix(project_dir, png)
                source_suffix = ".mmd" if figure_type == "mermaid" else (".txt" if figure_type == "terminal" else "")
                if source_suffix:
                    source = package_dir / "resources" / figure_type / f"{anchor_id}{source_suffix}"
                    if source.is_file():
                        source_path = paths.rel_posix(project_dir, source)
            # figureIndex 与 figurePlan 同源：figurePlan 合法状态 planned/deferred/done；
            # 资源已产出时 plan=done、index=done（index 不再使用 produced，validator 才一致）。
            plan_status = "done" if status == "produced" else "planned"
            index.append(
                {
                    "anchorId": anchor_id,
                    "type": figure_type,
                    "chapterId": str(node.get("id") or ""),
                    "caption": str(plan.get("caption") or ""),
                    "status": plan_status,
                    "resourcePath": resource_path,
                    "sourcePath": source_path,
                }
            )
            try:
                plan["status"] = plan_status
                plan["resourcePath"] = resource_path
                plan["sourcePath"] = source_path
            except (TypeError, AttributeError):
                pass
    outline["figureIndex"] = index


def merge_selected(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
    chapter: str | None = None,
    result_path: str | None = None,
    batch: int | None = None,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = resolve_context(
        workspace, project_dir, package_id
    )
    lock_path = project_dir_path / package_dir / LOCK_MARKER
    if lock_path.exists():
        raise errors.BgError(
            errors.E_LOCKED,
            "正文目录已锁定（chapters/.locked），不允许合并",
            hint="先解除定稿锁定（bg_finalize.py --unlock）后再合并",
            detail={"lock": LOCK_MARKER},
        )
    outline = load_outline(package_dir)
    nodes = leaf_nodes(outline)
    by_id = {str(node.get("id")): node for node in nodes}
    selected = _select_results(package_dir, state, chapter=chapter, result_path=result_path, batch=batch)
    if not selected:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "没有可合并的 chapter-result.json",
            hint="子智能体先写 chapters/_results/<chapterId>.chapter-result.json",
            detail={"resultsDir": RESULTS_DIR},
        )
    parsed: list[tuple[Path, dict[str, Any], dict[str, Any], str]] = []
    findings: list[dict[str, Any]] = []
    for path in selected:
        try:
            document = jsonio.read_json(path)
        except errors.BgError as exc:
            findings.append({"code": "result_unreadable", "severity": "blocker", "message": exc.message, "file": str(path)})
            continue
        chapter_id = str(document.get("chapterId") or path.name[: -len(RESULT_SUFFIX)]) if isinstance(document, Mapping) else ""
        node = by_id.get(chapter_id)
        if node is None:
            findings.append(
                {
                    "code": "chapter_not_found",
                    "severity": "blocker",
                    "message": f"结果对应章节不存在或不是末级：{chapter_id!r}",
                    "file": path.name,
                }
            )
            continue
        summary_text = document.get("summary") if isinstance(document, Mapping) else None
        if isinstance(summary_text, str) and len(summary_text.strip()) > SUMMARY_MAX:
            original_length = len(summary_text.strip())
            document["summary"] = summary_text.strip()[:SUMMARY_MAX]
            document["summaryTruncated"] = {"originalLength": original_length, "max": SUMMARY_MAX, "at": paths.iso_now()}
            jsonio.write_json_atomic(path, document)
        result_findings = validate_result(document, node, package_dir)
        findings.extend({**item, "file": path.name} for item in result_findings)
        if not result_findings:
            parsed.append((path, dict(document), node, chapter_id))
    if findings:
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"章节结果校验未通过：{len(findings)} 项 blocker",
            hint="按 findings 修正 chapter-result.json 后重试；本批未写入 outline",
            detail={"command": "chapter_merge", "packageId": target, "findings": findings, "results": [p.name for p in selected]},
            exit_code=errors.EXIT_SCHEMA,
        )
    now = paths.iso_now()
    updated_outline = copy.deepcopy(outline)
    updated_nodes = {str(node.get("id")): node for node in leaf_nodes(updated_outline)}
    merged: list[dict[str, Any]] = []
    unchanged: list[str] = []
    supplement_tables: list[dict[str, Any]] = []
    for _path, document, _node, chapter_id in parsed:
        # 同步章节目录 figurePlan 的 caption 到 chapter-result（wordCount 由结果自身记录，合并后保留）
        plans = {
            str(plan.get("anchorId")): str(plan.get("caption") or "").strip()
            for plan in _node.get("figurePlan") or []
            if isinstance(plan, Mapping) and plan.get("anchorId")
        }
        figures = document.get("figures") if isinstance(document.get("figures"), list) else document.get("figureResults")
        if isinstance(figures, list):
            changed = False
            for figure in figures:
                if not isinstance(figure, dict):
                    continue
                caption = plans.get(str(figure.get("anchorId") or ""))
                if caption and str(figure.get("caption") or "").strip() != caption:
                    figure["caption"] = caption
                    changed = True
            if changed:
                jsonio.write_json_atomic(Path(_path), document)
        tables = document.get("tables")
        if isinstance(tables, list):
            chapter_supplements: list[dict[str, Any]] = []
            for index, table in enumerate(tables):
                if not isinstance(table, Mapping):
                    continue
                anchor = str(table.get("anchorId") or "")
                if anchor and anchor in plans:
                    continue
                caption = str(table.get("caption") or "").strip()
                entry = {
                    "anchorId": anchor or f"sup-{chapter_id}-{index + 1}",
                    "chapterId": chapter_id,
                    "caption": caption,
                    "source": paths.rel_posix(project_dir, Path(_path)),
                    "kind": "supplement",
                }
                chapter_supplements.append(entry)
                supplement_tables.append(entry)
            if chapter_supplements:
                document["supplementTables"] = chapter_supplements
                jsonio.write_json_atomic(Path(_path), document)
        status = STATUS_MAP[str(document.get("status"))]
        node = updated_nodes[chapter_id]
        completion = dict(node.get("completion") or _default_completion())
        new_completion = {
            **completion,
            "status": status,
            "wordCount": int(document.get("wordCount") or 0),
            "updatedAt": now,
        }
        summary = document.get("summary")
        if isinstance(summary, str):
            new_completion["summary"] = summary.strip()
        if status == "skipped":
            new_completion["skipReason"] = str(document.get("skipReason") or document.get("reason") or "")
            new_completion["blockReason"] = ""
        elif status == "blocked":
            new_completion["blockReason"] = str(document.get("blockReason") or document.get("reason") or "")
        else:
            new_completion["skipReason"] = ""
            new_completion["blockReason"] = ""
        # 幂等判定忽略 updatedAt（重复合并同一结果不重复写/备份/计数）
        semantic_keys = ("status", "wordCount", "summary", "skipReason", "blockReason")
        if all(completion.get(key) == new_completion.get(key) for key in semantic_keys):
            unchanged.append(chapter_id)
            continue
        node["completion"] = new_completion
        node["reviewState"] = dict(node.get("reviewState") or {"coverage": "unreviewed", "lastRound": 0, "issueIds": []})
        merged.append(
            {
                "chapterId": chapter_id,
                "status": status,
                "wordCount": new_completion["wordCount"],
                "summaryLength": len(str(new_completion.get("summary") or "")),
                "skipReason": new_completion.get("skipReason", ""),
                "blockReason": new_completion.get("blockReason", ""),
            }
        )
    selected_chapters = {chapter_id for _path, _document, _node, chapter_id in parsed}
    existing_supplements = [item for item in updated_outline.get("supplementTables") or [] if isinstance(item, Mapping) and str(item.get("chapterId") or "") not in selected_chapters]
    merged_supplements = {(str(item.get("chapterId") or ""), str(item.get("anchorId") or ""), str(item.get("caption") or "")): dict(item) for item in existing_supplements}
    for item in supplement_tables:
        merged_supplements[(str(item.get("chapterId") or ""), str(item.get("anchorId") or ""), str(item.get("caption") or ""))] = dict(item)
    updated_outline["supplementTables"] = sorted(merged_supplements.values(), key=lambda item: (str(item.get("chapterId") or ""), str(item.get("anchorId") or "")))
    # 可选：按实际资源同步 figureIndex（figurePlan 合法状态 planned/deferred/done）
    before_figures = json.dumps(updated_outline.get("figureIndex") or [], ensure_ascii=False, sort_keys=True)
    before_plan = json.dumps(
        [plan for node in outline_validate.iter_preorder(updated_outline.get("chapters") or []) for plan in node.get("figurePlan") or []],
        ensure_ascii=False,
        sort_keys=True,
    )
    sync_figure_index(updated_outline, package_dir, project_dir)
    after_figures = json.dumps(updated_outline.get("figureIndex") or [], ensure_ascii=False, sort_keys=True)
    after_plan = json.dumps(
        [plan for node in outline_validate.iter_preorder(updated_outline.get("chapters") or []) for plan in node.get("figurePlan") or []],
        ensure_ascii=False,
        sort_keys=True,
    )
    if before_figures != after_figures or before_plan != after_plan:
        outline_file = outline_validate.outline_path(package_dir)
        if outline_file.is_file():
            backup = outline_file.with_name(f"{outline_file.name}.bak-{paths.timestamp14()}")
            try:
                shutil.copy2(outline_file, backup)
            except OSError:
                pass
        try:
            event_log.log_event(
                workspace,
                "outline.repaired",
                actor="script",
                project=str(project_doc.get("projectId") or ""),
                package=target,
                detail={"packageId": target, "reason": "figureIndex/figurePlan 状态规范化", "figureCount": len(updated_outline.get("figureIndex") or [])},
            )
        except ValueError:
            pass
    # 重算 stats（保留 7.4 必需键，额外记录进度）
    base_stats = outline_validate.build_stats(updated_outline, package_meta)
    statuses = [str(node.get("completion", {}).get("status") or "pending") for node in updated_nodes.values()]
    written = sum(
        int(node.get("completion", {}).get("wordCount") or 0)
        for node in updated_nodes.values()
        if node.get("completion", {}).get("status") == "done"
    )
    done = statuses.count("done")
    skipped = statuses.count("skipped")
    blocked = statuses.count("blocked")
    pending = len(statuses) - done - skipped - blocked
    updated_outline["stats"] = {
        **base_stats,
        "doneCount": done,
        "skippedCount": skipped,
        "blockedCount": blocked,
        "pendingCount": pending,
        "writtenWordTotal": written,
        "completionRate": round((done + skipped) / len(statuses), 6) if statuses else 0.0,
    }
    updated_outline["stats"].setdefault("plannedWordTotal", base_stats.get("plannedWordTotal"))
    updated_outline["stats"].setdefault("parentCount", base_stats.get("parentCount"))
    updated_outline["stats"].setdefault("leafCount", base_stats.get("leafCount"))
    idempotent = not merged
    backup = ""
    if not idempotent:
        path = outline_path(package_dir)
        target_dir = path.parent
        stamp = paths.timestamp14()
        candidate = target_dir / f"outline.json.bak-{stamp}"
        suffix = 1
        while candidate.exists():
            candidate = target_dir / f"outline.json.bak-{stamp}-{suffix}"
            suffix += 1
        shutil.copy2(path, candidate)
        backup = f"chapters/_outline/{candidate.name}"
        jsonio.write_json_atomic(path, updated_outline)

        for item in merged:
            event = {"done": "chapter.completed", "skipped": "chapter.skipped", "blocked": "chapter.blocked"}[item["status"]]
            event_log.log_event(
                workspace_path,
                event,
                project=str(project_doc.get("projectId") or ""),
                package=target,
                detail={
                    "chapterId": item["chapterId"],
                    "status": item["status"],
                    "wordCount": item["wordCount"],
                    "summaryLength": item["summaryLength"],
                },
            )
    return {
        "ok": True,
        "command": "chapter_merge",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "outlinePath": paths.rel_posix(project_dir_path, outline_path(package_dir)),
        "merged": merged,
        "unchanged": unchanged,
        "idempotent": idempotent,
        "backup": backup,
        "stats": updated_outline.get("stats"),
        "currentStage": state.get("currentStage"),
        "revision": state.get("revision"),
        "note": "只更新 outline.completion/reviewState/summary/wordCount 与 stats；state.json 不变（单写者）",
    }


# ---------------------------------------------------------------------------
# status / verify-all
# ---------------------------------------------------------------------------
def status_payload(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = resolve_context(
        workspace, project_dir, package_id
    )
    outline = load_outline(package_dir)
    nodes = leaf_nodes(outline)
    statuses: dict[str, int] = {}
    per_chapter: list[dict[str, Any]] = []
    for node in nodes:
        completion = node.get("completion") or {}
        status = str(completion.get("status") or "pending")
        statuses[status] = statuses.get(status, 0) + 1
        per_chapter.append(
            {
                "chapterId": node.get("id"),
                "title": node.get("title"),
                "status": status,
                "wordCount": completion.get("wordCount", 0),
                "hasSummary": bool(str(completion.get("summary") or "").strip()),
                "hasResult": result_file(package_dir, str(node.get("id"))).is_file(),
            }
        )
    return {
        "ok": True,
        "command": "chapter_merge",
        "mode": "status",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "currentStage": state.get("currentStage"),
        "reviewLoop": state.get("reviewLoop"),
        "leafCount": len(nodes),
        "statusCounts": statuses,
        "resultFiles": len(available_results(package_dir)),
        "chapters": per_chapter,
        "pending": [item["chapterId"] for item in per_chapter if item["status"] in ("pending", "writing")],
        "blocked": [item["chapterId"] for item in per_chapter if item["status"] == "blocked"],
    }


def verify_all_payload(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = resolve_context(
        workspace, project_dir, package_id
    )
    outline = load_outline(package_dir)
    nodes = leaf_nodes(outline)
    findings: list[dict[str, Any]] = []
    for node in nodes:
        chapter_id = str(node.get("id"))
        completion = node.get("completion") or {}
        status = str(completion.get("status") or "pending")
        if status in ("pending", "writing"):
            findings.append({"code": "chapter_pending", "chapterId": chapter_id, "message": f"章节尚未完成：{status}"})
        elif status == "blocked":
            findings.append(
                {
                    "code": "chapter_blocked",
                    "chapterId": chapter_id,
                    "message": "存在 blocked 章节，T5 前必须清零",
                    "blockReason": completion.get("blockReason", ""),
                }
            )
        elif status == "done":
            if not str(completion.get("summary") or "").strip():
                findings.append({"code": "summary_missing", "chapterId": chapter_id, "message": "done 章节 summary 不能为空"})
            word_count = completion.get("wordCount")
            if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count <= 0:
                findings.append({"code": "word_count_invalid", "chapterId": chapter_id, "message": "done 章节 wordCount 必须 >0"})
        elif status == "skipped":
            if not str(completion.get("skipReason") or "").strip():
                findings.append({"code": "skip_reason_missing", "chapterId": chapter_id, "message": "skipped 章节必须有 skipReason"})
        else:
            findings.append({"code": "status_invalid", "chapterId": chapter_id, "message": f"非法 completion.status：{status}"})
    stats = outline_validate.build_stats(outline, package_meta)
    return {
        "ok": True,
        "command": "chapter_merge",
        "mode": "verify_all",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "currentStage": state.get("currentStage"),
        "allDone": not findings,
        "validForT5": not findings,
        "leafCount": len(nodes),
        "findings": findings,
        "stats": stats,
        "reviewLoop": state.get("reviewLoop"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_chapter_merge.py",
        description="BidGenie F5-2 章节结果合并（单写者）：chapter-result.json → outline completion/stats；T5 前校验",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>（缺省 state.currentPackage / packagesToWrite 首个）")
    parser.add_argument("--batch", type=int, help="合并第 k 批（读取 state.extensions.draftBatches[k-1]；无批次记录则合并全部可用结果）")
    parser.add_argument("--chapter", help="只合并指定末级章节 id（如 1-1）")
    parser.add_argument("--result", help="合并指定 chapter-result.json 文件")
    parser.add_argument("--verify-all", action="store_true", help="只读校验全部 leaf 完成（done/skipped、无 blocked、summary/wordCount）")
    parser.add_argument("--status", action="store_true", help="只读进度（状态计数/结果文件/blocked/pending）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if args.status:
        return status_payload(workspace=workspace, project_dir=project_dir, package_id=args.package)
    if args.verify_all:
        return verify_all_payload(workspace=workspace, project_dir=project_dir, package_id=args.package)
    return merge_selected(
        workspace=workspace,
        project_dir=project_dir,
        package_id=args.package,
        chapter=args.chapter,
        result_path=args.result,
        batch=args.batch if args.batch is not None and args.batch > 0 else None,
    )


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _summary_text(chapter_id: str) -> str:
    body = (
        f"本章（{chapter_id}）响应评审因素的技术要求，围绕服务范围、实施方法与保障措施展开说明。"
        "明确关键承诺与量化指标，结合项目实际给出可执行步骤和验收标准，并与相关章节保持自然衔接。"
        "内容不复制其他章节，不写证明材料与无关商务信息。"
    )
    text = body * 3
    return text[:350]


def _build_selftest_project(base: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    workspace = base / "ws"
    for rel in ("dsh/logs", "dsh/config", "dsh/bidders"):
        (workspace / rel).mkdir(parents=True, exist_ok=True)
    now = paths.iso_now()
    jsonio.write_json_atomic(
        workspace / "dsh" / "registry.json",
        {
            "schemaVersion": "1.0",
            "workspaceRootFingerprint": paths.workspace_fingerprint(workspace),
            "currentBidderId": "",
            "bidders": [],
            "projects": [],
            "updatedAt": now,
        },
    )
    project_id = "prj-20261111100000-m6aa"
    project_dir = workspace / f"合并自检项目-{paths.timestamp14()}-m6aa"
    project_dir.mkdir()
    project = schema.initialize_project_document(
        project_id=project_id,
        bidder_ref={"bidderId": "bdr-20261111100000-m6aa", "dirName": "示例", "displayName": "示例", "snapshotAt": now},
        workspace_fingerprint=paths.workspace_fingerprint(workspace),
        now_iso=now,
        project_name="M6 合并自检项目",
    )
    project["projectNo"] = "M6-TEST-001"
    project["isSubcontracted"] = False
    project["packageCount"] = 1
    project["packagesToWrite"] = ["package-1"]
    jsonio.write_json_atomic(project_dir / "project.json", project)
    state = schema.initialize_state_document(project_id=project_id, now_iso=now)
    state["currentStage"] = "draft"
    state["currentScope"] = "package-1"
    state["currentPackage"] = "package-1"
    state["completedStages"] = [
        {"stage": "project_init", "scope": "project", "completedAt": now, "note": ""},
        {"stage": "info_extract", "scope": "project", "completedAt": now, "note": ""},
        {"stage": "package_prepare", "scope": "package-1", "completedAt": now, "note": ""},
        {"stage": "outline", "scope": "package-1", "completedAt": now, "note": ""},
    ]
    state["revision"] = 6
    state["reviewLoop"] = {"round": 0, "maxRounds": 5, "closedRound": None}
    state_lib.write_state(project_dir, state)
    package_dir = project_dir / "packages" / f"package-1-{paths.timestamp14()}"
    package_dir.mkdir(parents=True)
    for rel in (
        "chapters",
        "chapters/_results",
        "chapters/_outline",
        "reviews",
        "reviews/rounds",
        "resources/mermaid",
        "resources/terminal",
        "export",
    ):
        (package_dir / rel).mkdir(parents=True, exist_ok=True)
    package_meta = {
        "schemaVersion": "1.0",
        "packageId": "package-1",
        "packageNo": 1,
        "packageName": "M6 合并自检项目",
        "expectedWordCount": 50000,
        "figureQuota": {"table": 5, "mermaid": 5, "network": 0, "terminal": 5},
    }
    jsonio.write_json_atomic(package_dir / "package-meta.json", package_meta)
    import bg_outline_apply as outline_apply

    outline = outline_apply.normalize_outline(
        copy.deepcopy(outline_validate._golden_document()),  # noqa: SLF001 - 自检复用 golden 夹具
        package_meta=package_meta,
        project_name="M6 合并自检项目",
        package_id="package-1",
    )
    outline_apply.materialize_tree(outline, package_dir)
    jsonio.write_json_atomic(outline_validate.outline_path(package_dir), outline)
    return workspace, project_dir, package_dir, outline


def run_selftest() -> dict[str, Any]:
    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-chapter-merge-selftest-"))
    try:
        workspace, project_dir, package_dir, outline = _build_selftest_project(tmp)
        leaves = leaf_nodes(outline)
        first, second = leaves[0], leaves[1]
        results = results_dir(package_dir)
        results.mkdir(parents=True, exist_ok=True)

        def write_result(node: Mapping[str, Any], status: str, **extra: Any) -> Path:
            document = {
                "schemaVersion": "1.0",
                "chapterId": node.get("id"),
                "status": status,
                **extra,
            }
            path = result_file(package_dir, str(node.get("id")))
            jsonio.write_json_atomic(path, document)
            return path

        write_result(first, "done", wordCount=1800, summary=_summary_text(str(first.get("id"))))
        merged = merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(first.get("id")))
        updated = jsonio.read_json(outline_validate.outline_path(package_dir))
        node = next(item for item in leaf_nodes(updated) if item.get("id") == first.get("id"))
        if not merged.get("merged") or node["completion"]["status"] != "done" or node["completion"]["wordCount"] != 1800:
            raise AssertionError(f"合并结果不正确：{merged} / {node.get('completion')}")
        if not merged.get("backup"):
            raise AssertionError("合并未备份 outline")
        checks.append("merge_done")
        backups_before = len(list((package_dir / "chapters/_outline").glob("outline.json.bak-*")))
        again = merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(first.get("id")))
        backups_after = len(list((package_dir / "chapters/_outline").glob("outline.json.bak-*")))
        if not again.get("idempotent") or backups_after != backups_before:
            raise AssertionError(f"重复合并非幂等：{again.get('idempotent')} backups {backups_before}->{backups_after}")
        checks.append("merge_idempotent")
        # blocked 结果映射 + verify-all 失败
        blocked_path = write_result(second, "blocked", wordCount=0, blockReason="缺少用户补充信息", questions=["请补充人员计划"])
        blocked = merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(second.get("id")))
        verify_blocked = verify_all_payload(workspace=workspace, project_dir=project_dir)
        codes = {item["code"] for item in verify_blocked["findings"]}
        if blocked["merged"][0]["status"] != "blocked" or verify_blocked["allDone"] or "chapter_blocked" not in codes:
            raise AssertionError(f"blocked 合并/verify 不正确：{blocked} / {verify_blocked}")
        checks.append("merge_blocked")
        # 非法 done（summary 缺失/wordCount 0）拒绝且不写 outline
        bad_path = write_result(second, "done", wordCount=0, summary="太短")
        outline_before = jsonio.read_json(outline_validate.outline_path(package_dir))
        try:
            merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(second.get("id")))
        except errors.BgError as exc:
            if exc.code != errors.E_VALIDATE_FAILED:
                raise AssertionError(f"非法结果错误码不正确：{exc.code}") from exc
        else:
            raise AssertionError("非法结果未被拒绝")
        if jsonio.read_json(outline_validate.outline_path(package_dir)) != outline_before:
            raise AssertionError("非法结果合并不应写 outline")
        checks.append("invalid_result_rejected")
        # 其余章节 done/skipped 后 verify-all 通过
        for node in leaves:
            if node.get("id") == first.get("id"):
                continue
            if node.get("id") == second.get("id"):
                write_result(node, "skipped", wordCount=0, skipReason="用户确认跳过")
            else:
                write_result(node, "done", wordCount=1500, summary=_summary_text(str(node.get("id"))))
        for node in leaves:
            merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(node.get("id")))
        verify_ok = verify_all_payload(workspace=workspace, project_dir=project_dir)
        if not verify_ok["allDone"]:
            raise AssertionError(f"全部完成后 verify-all 仍失败：{verify_ok['findings']}")
        checks.append("verify_all")
        status = status_payload(workspace=workspace, project_dir=project_dir)
        if status["statusCounts"].get("done", 0) != len(leaves) - 1 or status["statusCounts"].get("skipped", 0) != 1:
            raise AssertionError(f"status 计数不正确：{status['statusCounts']}")
        checks.append("status")
        # T5 guard/transition（state 层）
        state_before = state_lib.load_state(project_dir)
        t5 = state_lib.perform_transition(
            workspace, project_dir, to_stage="review", package="package-1", reason="M6 自检 T5"
        )
        state_after = state_lib.load_state(project_dir)
        if t5.get("transitionId") != "T5" or state_after.get("currentStage") != "review":
            raise AssertionError(f"T5 迁移失败：{t5}")
        if state_after.get("reviewLoop", {}).get("round") != 1:
            raise AssertionError(f"reviewLoop.round 首轮应为 1：{state_after.get('reviewLoop')}")
        checks.append("t5_transition")
        return {"ok": True, "selftest": {"script": "bg_chapter_merge.py", "checks": checks}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_chapter_merge.py",
    )


if __name__ == "__main__":
    sys.exit(main())
