#!/usr/bin/env python3
"""BidGenie F5-2 章节结果合并（单写者，PRD F5.2.3–F5.2.6 / 7.4 / 4.3 T5）。

子智能体只写自己的章节 md + `chapters/_results/<chapterId>.chapter-result.json`；
本脚本串行读取结果、校验契约、更新 `chapters/_outline/outline.json` 的
`completion`（status/wordCount/summary/skipReason/blockReason/updatedAt）与 `reviewState`，
重算 stats（含实际字数/完成数），备份后原子写，并记 `chapter.completed/skipped/blocked` 事件。

口径（P1-5 / P2-10）：

- **字数**：`stats.writtenWordTotal` 按 `_lib/text.count_body_words`（审查口径：CJK 字符 +
  ASCII 连续词，去掉标题行/HTML 注释）复算章节 md 得到，与 review / export 同口径；
  子智能体在 chapter-result 里报的 `wordCount` 保留进 `completion.wordCount`（审计用），
  并在 stats 记 `reportedWordTotal` 对照；
- **表格注册**：`chapter-result.tables[]` 里属于本章 figurePlan 的“计划表”（按 anchorId、
  题注或 `plannedFigure=true` 判定）不再登记为 `outline.supplementTables`；只有真正的
  补充表才登记（按 anchorId/题注去重），并清理历史遗留的“计划表重复登记”；
- **summary 200–400 是字符长度 `len(str)`（含标点）**，不是字数口径，保持不变。

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
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib
from _lib import text as text_lib
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


def _normalize_caption(value: Any) -> str:
    """题注归一化：删除所有空白（含全角空格），用于 caption 级匹配。

    写作子智能体常只填 `tables[].caption` 不填 anchorId（真实冒烟数据即如此），
    因此计划表识别必须同时支持“按 anchorId”和“按本章 figurePlan 题注”两种匹配。
    """
    return re.sub(r"[\s\u3000]+", "", str(value or ""))


def _figure_plan_maps(node: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """返回本章 (anchorId→type, 归一化题注→anchorId) 的 figurePlan 映射。"""
    anchors: dict[str, str] = {}
    captions: dict[str, str] = {}
    for plan in node.get("figurePlan") or []:
        if not isinstance(plan, Mapping):
            continue
        anchor_id = str(plan.get("anchorId") or "")
        if not anchor_id:
            continue
        anchors[anchor_id] = str(plan.get("type") or "")
        caption = _normalize_caption(plan.get("caption"))
        if caption:
            captions[caption] = anchor_id
    return anchors, captions


def _planned_table_refs(node: Mapping[str, Any], document: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """返回本章“计划表”的 anchorId 集合与归一化题注集合（P1-5）。

    来源：figurePlan 中 type=table 的计划；再补充 chapter-result.figures[] 里声明为 table
    且 anchorId 属于本章 figurePlan 的条目（兼容子智能体只在 figures[] 标 type）。
    """
    all_anchors, plan_captions = _figure_plan_maps(node)
    planned_anchors = {anchor for anchor, figure_type in all_anchors.items() if figure_type == "table"}
    planned_captions = {
        caption for caption, anchor in plan_captions.items() if all_anchors.get(anchor) == "table"
    }
    figures = document.get("figures") if isinstance(document, Mapping) else None
    if not isinstance(figures, list):
        figures = document.get("figureResults") if isinstance(document, Mapping) else None
    if isinstance(figures, list):
        for figure in figures:
            if not isinstance(figure, Mapping):
                continue
            anchor_id = str(figure.get("anchorId") or "")
            if anchor_id and all_anchors.get(anchor_id) == "table":
                planned_anchors.add(anchor_id)
            if str(figure.get("type") or "") == "table" and anchor_id in all_anchors:
                planned_anchors.add(anchor_id)
                caption = _normalize_caption(figure.get("caption"))
                if caption:
                    planned_captions.add(caption)
    return planned_anchors, planned_captions


def table_is_planned(
    table: Mapping[str, Any],
    node: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    """判断 `chapter-result.tables[]` 的一条记录是否属于本章 figurePlan 的计划表。"""
    explicit = table.get("plannedFigure")
    if explicit is True:
        return True
    if explicit is False:
        return False
    anchor_id = str(table.get("anchorId") or "").strip()
    planned_anchors, planned_captions = _planned_table_refs(node, document)
    if anchor_id and anchor_id in planned_anchors:
        return True
    caption = _normalize_caption(table.get("caption"))
    return bool(caption) and caption in planned_captions


def _chapter_body_word_count(package_dir: Path, node: Mapping[str, Any]) -> int | None:
    """按审查口径（_lib/text.count_body_words）读取章节 md 的字数；文件缺失返回 None。"""
    file_path = node.get("filePath")
    if not isinstance(file_path, str) or not file_path:
        return None
    path = package_dir / file_path
    if not path.is_file():
        return None
    try:
        return text_lib.count_body_words(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None


def _supplement_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    """补充表去重键：优先 anchorId，其次归一化题注（同章内唯一）。"""
    chapter_id = str(entry.get("chapterId") or "")
    anchor_id = str(entry.get("anchorId") or "")
    if anchor_id:
        return chapter_id, anchor_id
    return chapter_id, _normalize_caption(entry.get("caption"))


def supplement_is_planned_table(entry: Mapping[str, Any], outline: Mapping[str, Any]) -> bool:
    """判断一条 outline.supplementTables 记录是否是“计划表重复登记”（P1-5 历史数据清理）。"""
    chapter_id = str(entry.get("chapterId") or "")
    if not chapter_id:
        return False
    target_node = None
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        if str(node.get("id")) == chapter_id:
            target_node = node
            break
    if target_node is None:
        return False
    anchor_id = str(entry.get("anchorId") or "").strip()
    planned_anchors, planned_captions = _planned_table_refs(target_node, {})
    if anchor_id and anchor_id in planned_anchors:
        return True
    return _normalize_caption(entry.get("caption")) in planned_captions


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
            add("summary_missing", "status=done 时 summary 必填（200–400 字符长度 len(str)，不是字数口径）")
        elif not (SUMMARY_MIN <= len(summary.strip()) <= SUMMARY_MAX):
            add(
                "summary_length",
                f"status=done 时 summary 必须 200–400 字符长度（len，含标点；实际 {len(summary.strip())}）",
            )
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
                    "subtype": plan.get("subtype"),
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
    supplement_seen: set[tuple[str, str]] = set()
    planned_table_skips = 0
    duplicate_supplements = 0
    word_count_warnings: list[dict[str, Any]] = []
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
                anchor = str(table.get("anchorId") or "").strip()
                caption = str(table.get("caption") or "").strip()
                # P1-5：chapter-result.tables[] 会登记本章所有 Markdown 表（含 figurePlan 里的
                # 计划表）。计划表不再写入 outline.supplementTables，避免 sup-* 与 fig-* 重复。
                if table_is_planned(table, _node, document):
                    planned_table_skips += 1
                    continue
                entry = {
                    "anchorId": anchor or f"sup-{chapter_id}-{index + 1}",
                    "chapterId": chapter_id,
                    "caption": caption,
                    "source": paths.rel_posix(project_dir, Path(_path)),
                    "kind": "supplement",
                }
                key = _supplement_key(entry)
                if key in supplement_seen:
                    duplicate_supplements += 1
                    continue
                supplement_seen.add(key)
                chapter_supplements.append(entry)
                supplement_tables.append(entry)
            previous_supplements = document.get("supplementTables")
            if previous_supplements != chapter_supplements and (chapter_supplements or previous_supplements):
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
                "bodyWordCount": _chapter_body_word_count(package_dir, node),
                "summaryLength": len(str(new_completion.get("summary") or "")),
                "skipReason": new_completion.get("skipReason", ""),
                "blockReason": new_completion.get("blockReason", ""),
            }
        )
    selected_chapters = {chapter_id for _path, _document, _node, chapter_id in parsed}
    # P1-5：清理历史遗留的“计划表重复登记”（旧版本把 figurePlan.table 也写进
    # outline.supplementTables，产生 sup-1-2-1 与 fig-1-2-01 并存的重复）。
    stale_supplements = [
        item
        for item in updated_outline.get("supplementTables") or []
        if isinstance(item, Mapping)
        and str(item.get("chapterId") or "") not in selected_chapters
        and not supplement_is_planned_table(item, updated_outline)
    ]
    merged_supplements: dict[tuple[str, str], dict[str, Any]] = {}
    for item in stale_supplements:
        merged_supplements[_supplement_key(item)] = dict(item)
    for item in supplement_tables:
        merged_supplements[_supplement_key(item)] = dict(item)
    updated_outline["supplementTables"] = sorted(
        merged_supplements.values(),
        key=lambda item: (str(item.get("chapterId") or ""), str(item.get("anchorId") or "")),
    )
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
    # P2-10：正文字数统一走 _lib/text.count_body_words（与 review/export 同口径），
    # 子智能体上报的 wordCount 仅保留在 completion 与 reportedWordTotal 中用于审计。
    reported_total = 0
    written = 0
    measured_chapters = 0
    measured_missing: list[str] = []
    for node in updated_nodes.values():
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        if completion.get("status") != "done":
            continue
        reported = int(completion.get("wordCount") or 0)
        reported_total += reported
        measured = _chapter_body_word_count(package_dir, node)
        if measured is None:
            measured_missing.append(str(node.get("id") or ""))
            written += reported
            continue
        measured_chapters += 1
        written += measured
        if measured > 0 and abs(reported - measured) / measured > 0.20:
            word_count_warnings.append(
                {
                    "code": "word_count_deviation",
                    "chapterId": str(node.get("id") or ""),
                    "message": f"chapter-result.wordCount={reported} 与章节 md 审查口径 {measured} 偏差 >20%（stats 采用审查口径）",
                }
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
        "reportedWordTotal": reported_total,
        "wordCountBasis": "review" if not measured_missing else "review_with_fallback",
        "measuredChapterCount": measured_chapters,
        "completionRate": round((done + skipped) / len(statuses), 6) if statuses else 0.0,
    }
    if measured_missing:
        word_count_warnings.append(
            {
                "code": "word_count_fallback",
                "message": f"章节 md 缺失，字数回退 chapter-result.wordCount：{measured_missing[:5]}",
            }
        )
    updated_outline["stats"].setdefault("plannedWordTotal", base_stats.get("plannedWordTotal"))
    updated_outline["stats"].setdefault("parentCount", base_stats.get("parentCount"))
    updated_outline["stats"].setdefault("leafCount", base_stats.get("leafCount"))

    # 字数总量诊断（VM 复测观察 1/2）：合并后立即给出全包偏差告警 + 逐章超/欠目标清单，
    # 让 Agent 在 T5 前就能发现“每章都顶 120% 导致全包 +16%”并定点精简，少走一轮 optimize。
    expected_total = int(package_meta.get("expectedWordCount") or 0) or int(base_stats.get("plannedWordTotal") or 0)
    deviation_pct = round((written - expected_total) / expected_total * 100, 2) if expected_total and written else 0.0
    word_total_warning = ""
    if expected_total and written and abs(deviation_pct) > 10.0:
        word_total_warning = (
            f"全包实测字数 {written} vs 计划 {expected_total}（{deviation_pct:+.1f}%），超出 ±10% 容差；"
            "建议按 wordCountDiagnostics.chaptersOverTarget/UnderTarget 定点调整后再 T5"
        )
        word_count_warnings.append(
            {
                "code": "word_total_deviation",
                "expected": expected_total,
                "actual": written,
                "deviationPct": deviation_pct,
                "message": word_total_warning,
            }
        )
    chapter_word_diagnostics: list[dict[str, Any]] = []
    for node in leaf_nodes(updated_outline):
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        if str(completion.get("status") or "") != "done":
            continue
        target = int(node.get("wordTarget") or 0)
        measured = _chapter_body_word_count(package_dir, node)
        if not target or measured is None:
            continue
        delta = measured - target
        chapter_word_diagnostics.append(
            {
                "chapterId": str(node.get("id") or ""),
                "title": str(node.get("title") or ""),
                "wordTarget": target,
                "actual": measured,
                "delta": delta,
                "pct": round(delta / target * 100, 1),
            }
        )
    chapters_over = sorted((item for item in chapter_word_diagnostics if item["delta"] > 0), key=lambda item: item["delta"], reverse=True)[:10]
    chapters_under = sorted((item for item in chapter_word_diagnostics if item["delta"] < 0), key=lambda item: item["delta"])[:10]
    word_count_diagnostics = {
        "expectedTotal": expected_total,
        "actualTotal": written,
        "deviationPct": deviation_pct,
        "withinTolerance": bool(expected_total) and abs(deviation_pct) <= 10.0,
        "warning": word_total_warning,
        "chaptersMeasured": len(chapter_word_diagnostics),
        "overTargetCount": sum(1 for item in chapter_word_diagnostics if item["delta"] > 0),
        "underTargetCount": sum(1 for item in chapter_word_diagnostics if item["delta"] < 0),
        "chaptersOverTarget": chapters_over,
        "chaptersUnderTarget": chapters_under,
    }

    idempotent = not merged
    # 除 completion 变化外，supplementTables 清理/字数口径复算也可能需要落盘；
    # 幂等语义 = 本次没有需要落盘的语义变化（重复合并同一结果必须不写、不备份）。
    supplements_changed = (outline.get("supplementTables") or []) != (updated_outline.get("supplementTables") or [])
    stats_changed = updated_outline.get("stats") != (outline.get("stats") or {})
    idempotent = idempotent and not supplements_changed and not stats_changed
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
        "supplementTables": updated_outline.get("supplementTables") or [],
        "supplementTableCount": len(updated_outline.get("supplementTables") or []),
        "supplementTablesDropped": {"plannedTableEntries": planned_table_skips, "duplicates": duplicate_supplements},
        "wordCountWarnings": word_count_warnings,
        "wordCountDiagnostics": word_count_diagnostics,
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
    expected_total = int(package_meta.get("expectedWordCount") or 0) or int(stats.get("plannedWordTotal") or 0)
    written_total = int(((outline.get("stats") or {}).get("writtenWordTotal")) or 0)
    deviation_pct = round((written_total - expected_total) / expected_total * 100, 2) if expected_total and written_total else 0.0
    total_warnings: list[str] = []
    if expected_total and written_total and abs(deviation_pct) > 10.0:
        total_warnings.append(f"全包实测字数 {written_total} vs 计划 {expected_total}（{deviation_pct:+.1f}%），超出 ±10% 容差")
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
        "warnings": total_warnings,
        "wordCountTotal": {
            "expectedTotal": expected_total,
            "actualTotal": written_total,
            "deviationPct": deviation_pct,
            "withinTolerance": bool(expected_total) and abs(deviation_pct) <= 10.0,
        },
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
        # 自检章节 md 补一段正文，用于验证 stats 字数按审查口径（CJK+ASCII 词）复算
        first_chapter_path = package_dir / str(first.get("filePath"))
        first_chapter_path.write_text(
            first_chapter_path.read_text(encoding="utf-8")
            + "\n\n运维服务人员 8 人，按季度开展巡检、应急演练与性能评估，保障系统稳定运行。\n",
            encoding="utf-8",
            newline="\n",
        )
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

        # P1-5 / P2-10 回归：计划表不得重复登记为 supplementTables；stats 字数走审查口径。
        outline_file = outline_validate.outline_path(package_dir)
        seeded_outline = jsonio.read_json(outline_file)
        seeded_outline["supplementTables"] = [
            {"anchorId": "sup-1-1-1", "chapterId": str(first.get("id")), "caption": "项目现状对比", "kind": "supplement"},
        ]
        jsonio.write_json_atomic(outline_file, seeded_outline)
        first_tables = [
            {"caption": "项目现状对比", "rows": 4},  # 计划表：只有题注，无 anchorId
            {"anchorId": "fig-1-1-02", "caption": "需求清单", "rows": 3},  # 计划表：有 anchorId
            {"caption": "真实补充表", "rows": 2},  # 真补充表：应登记
        ]
        write_result(
            first,
            "done",
            wordCount=1800,
            summary=_summary_text(str(first.get("id"))),
            tables=first_tables,
        )
        merged = merge_selected(workspace=workspace, project_dir=project_dir, chapter=str(first.get("id")))
        updated = jsonio.read_json(outline_file)
        node = next(item for item in leaf_nodes(updated) if item.get("id") == first.get("id"))
        if not merged.get("merged") or node["completion"]["status"] != "done" or node["completion"]["wordCount"] != 1800:
            raise AssertionError(f"合并结果不正确：{merged} / {node.get('completion')}")
        if not merged.get("backup"):
            raise AssertionError("合并未备份 outline")
        supplements = [item for item in updated.get("supplementTables") or []]
        if [item.get("caption") for item in supplements] != ["真实补充表"]:
            raise AssertionError(f"计划表被重复登记为 supplementTables：{supplements}")
        if (merged.get("supplementTablesDropped") or {}).get("plannedTableEntries") != 2:
            raise AssertionError(f"计划表剔除计数不正确：{merged.get('supplementTablesDropped')}")
        first_result = jsonio.read_json(result_file(package_dir, str(first.get("id"))))
        if [item.get("caption") for item in first_result.get("supplementTables") or []] != ["真实补充表"]:
            raise AssertionError(f"chapter-result.supplementTables 未按补充表回写：{first_result.get('supplementTables')}")
        measured_first = _chapter_body_word_count(package_dir, node)
        if not measured_first or updated["stats"].get("writtenWordTotal") != measured_first:
            raise AssertionError(f"stats.writtenWordTotal 未用审查口径复算：{updated['stats']} measured={measured_first}")
        if updated["stats"].get("reportedWordTotal") != 1800 or updated["stats"].get("wordCountBasis") != "review":
            raise AssertionError(f"stats 字数口径元信息不正确：{updated['stats']}")
        checks.append("merge_done")
        diagnostics = merged.get("wordCountDiagnostics") or {}
        if (
            not diagnostics
            or int(diagnostics.get("expectedTotal") or 0) <= 0
            or int(diagnostics.get("actualTotal") or 0) != measured_first
            or int(diagnostics.get("chaptersMeasured") or 0) < 1
            or not isinstance(diagnostics.get("chaptersOverTarget"), list)
            or not isinstance(diagnostics.get("chaptersUnderTarget"), list)
        ):
            raise AssertionError(f"字数总量诊断缺失或不正确：{diagnostics}")
        checks.append("wordcount_diagnostics")
        checks.append("supplement_planned_excluded")
        checks.append("wordcount_review_basis")
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
        total_info = verify_ok.get("wordCountTotal") or {}
        if int(total_info.get("expectedTotal") or 0) <= 0 or "withinTolerance" not in total_info:
            raise AssertionError(f"verify-all 缺少全包字数偏差信息：{total_info}")
        checks.append("verify_all")
        final_supplements = [item.get("caption") for item in jsonio.read_json(outline_file).get("supplementTables") or []]
        if final_supplements != ["真实补充表"]:
            raise AssertionError(f"后续合并丢失/污染 supplementTables：{final_supplements}")
        checks.append("supplement_tables_stable")
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
