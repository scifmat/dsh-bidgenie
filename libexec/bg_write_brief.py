#!/usr/bin/env python3
"""bg_write_brief.py：F5-2 写作任务书生成器（P1-5）。

从 outline.json + package-meta + project.json 生成批次 JSON/Markdown，避免主 Agent
手拼 3 批 prompt 时口径漂移。只读状态/大纲，写 `<packageDir>/chapters/_batches/`。

用法：
    python bg_write_brief.py --workspace <ws> --project <dir|id> --package package-1 \
        [--batch 1] [--all] [--batch-size 6] [--no-write] [--json] [--brief]

输出：
    - `_batches/batch-<n>.json`：批次结构（chapters[].criterionRefs/contentRequirements/
      figurePlan/context + constraints）
    - `_batches/batch-<n>.md`：可直接交给子智能体的任务书
    - `_batches/_batch-state.json`：批次台账（chapterIds/status/生成时间）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, state as state_lib  # noqa: E402
import bg_outline_validate as outline_validate  # noqa: E402

BATCH_DIR = "chapters/_batches"
BATCH_STATE = f"{BATCH_DIR}/_batch-state.json"
DEFAULT_BATCH_SIZE = 6


def _leaf_nodes(outline: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        node
        for node in outline_validate.iter_preorder(outline.get("chapters") or [])
        if not (isinstance(node.get("children"), list) and node.get("children"))
    ]


def _completion_status(node: Mapping[str, Any]) -> str:
    completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
    return str(completion.get("status") or "pending")


def _dependency_ids(node: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in node.get("dependsOn") or [] if str(item)]


def plan_batches(leaves: Sequence[Mapping[str, Any]], batch_size: int = DEFAULT_BATCH_SIZE) -> list[list[dict[str, Any]]]:
    """按 dependsOn 分波次切批；批内无硬依赖，批间按前批完成推进。"""
    size = max(1, int(batch_size))
    pending = [dict(node) for node in leaves if _completion_status(node) not in ("done", "skipped")]
    completed = {str(node.get("id")) for node in leaves if _completion_status(node) in ("done", "skipped")}
    batches: list[list[dict[str, Any]]] = []
    while pending:
        ready = [
            node
            for node in pending
            if all(dep in completed for dep in _dependency_ids(node))
        ]
        if not ready:
            # 依赖环/大纲错误：不静默死循环，按原始顺序切一批，交给 Agent/审查发现
            ready = list(pending)
        batch = ready[:size]
        batches.append(batch)
        batch_ids = {str(node.get("id")) for node in batch}
        pending = [node for node in pending if str(node.get("id")) not in batch_ids]
        completed.update(batch_ids)
    # 尾批不足 4 时尽量并入前一批（仍不超过 6）
    if len(batches) >= 2 and len(batches[-1]) < 4 and len(batches[-2]) + len(batches[-1]) <= size:
        batches[-2].extend(batches[-1])
        batches.pop()
    return batches


def _project_facts(project: Mapping[str, Any], package_meta: Mapping[str, Any]) -> dict[str, Any]:
    schedule = package_meta.get("schedulePlan") if isinstance(package_meta.get("schedulePlan"), Mapping) else {}
    return {
        "projectName": project.get("projectName") or "",
        "projectNo": project.get("projectNo") or "",
        "packageId": package_meta.get("packageId") or "",
        "packageName": package_meta.get("packageName") or "",
        "expectedWordCount": package_meta.get("expectedWordCount") or 0,
        "figureQuota": package_meta.get("figureQuota") or {},
        "personnelPlan": package_meta.get("personnelPlan") or "",
        "equipmentPlan": package_meta.get("equipmentPlan") or "",
        "schedulePlan": schedule,
        "valueSource": package_meta.get("valueSource") or {},
        "extractionRefs": {
            "procurement": f"extraction/packages/{package_meta.get('packageId') or ''}/06-Procurement-Content.md",
            "criteria": f"extraction/packages/{package_meta.get('packageId') or ''}/07-Evaluation-Criteria.md",
            "technical": f"extraction/packages/{package_meta.get('packageId') or ''}/09-Technical-Requirements.md",
        },
    }


def build_batch_payload(
    *,
    project: Mapping[str, Any],
    package_meta: Mapping[str, Any],
    outline: Mapping[str, Any],
    batch_no: int,
    batch_size: int,
) -> dict[str, Any]:
    leaves = _leaf_nodes(outline)
    batches = plan_batches(leaves, batch_size)
    batch_index = max(1, int(batch_no)) - 1
    selected = batches[batch_index] if 0 <= batch_index < len(batches) else []
    by_id = {str(node.get("id")): node for node in leaves}
    facts = _project_facts(project, package_meta)
    chapters: list[dict[str, Any]] = []
    for node in selected:
        chapter_id = str(node.get("id") or "")
        dependency_summaries: dict[str, str] = {}
        for dep in _dependency_ids(node):
            dep_node = by_id.get(dep)
            if dep_node is not None:
                summary = (dep_node.get("completion") or {}).get("summary") if isinstance(dep_node.get("completion"), Mapping) else ""
                dependency_summaries[dep] = str(summary or "")
        chapters.append(
            {
                "chapterId": chapter_id,
                "title": node.get("title") or "",
                "level": node.get("level"),
                "filePath": node.get("filePath") or "",
                "wordTarget": node.get("wordTarget") or 0,
                "priority": node.get("priority") or "",
                "scoreWeight": node.get("scoreWeight") or 0,
                "criterionRefs": node.get("criterionRefs") or [],
                "contentRequirements": node.get("contentRequirements") or [],
                "figurePlan": node.get("figurePlan") or [],
                "insertAnchors": node.get("insertAnchors") or [],
                "writingNotes": node.get("writingNotes") or [],
                "handling": node.get("handling"),
                "context": {
                    "dependencySummaries": dependency_summaries,
                    "projectFacts": facts,
                },
            }
        )
    return {
        "schemaVersion": "1.0",
        "packageId": package_meta.get("packageId") or "",
        "batch": int(batch_no),
        "batchSize": len(chapters),
        "totalBatches": len(batches),
        "totalLeaves": len(leaves),
        "chapters": chapters,
        "projectFacts": facts,
        "constraints": {
            "noSelfReference": True,
            "noSensitive": True,
            "noBusinessMaterial": True,
            "writeOnlyCurrentBatch": True,
        },
        "writingRules": [
            "加载 bg-writing-style；正文不写跨章引用（如“详见第 3 章”）。",
            "图表按 figurePlan 生成；table 不写 BG:FIGURE，mermaid/terminal 才写标记。",
            "特殊章节 handling 不为空时按 bg-special-chapters 模板写，不产图/表。",
            "只写本章 md 与 resources 源文件；不改 outline.json，不渲染 png。",
        ],
    }


def render_batch_markdown(payload: Mapping[str, Any]) -> str:
    facts = payload.get("projectFacts") or {}
    lines = [
        f"# 第 {payload.get('batch')} 批写作任务书（{payload.get('packageId')}）",
        "",
        f"- 本批章节：{payload.get('batchSize')} / 共 {payload.get('totalBatches')} 批；末级总数 {payload.get('totalLeaves')}",
        f"- 项目：{facts.get('projectName') or '未记录'} / {facts.get('packageName') or ''}",
        f"- 全包目标字数：{facts.get('expectedWordCount') or 0}",
        f"- 图表配额：{json.dumps(facts.get('figureQuota') or {}, ensure_ascii=False)}",
        f"- 人员计划：{facts.get('personnelPlan') or '未记录'}",
        f"- 设备计划：{facts.get('equipmentPlan') or '未记录'}",
        f"- 工期/服务期：{json.dumps(facts.get('schedulePlan') or {}, ensure_ascii=False)}",
        "",
        "## 本批章节",
        "",
    ]
    for chapter in payload.get("chapters") or []:
        lines.extend(
            [
                f"### {chapter.get('chapterId')} {chapter.get('title')}",
                "",
                f"- 文件：`{chapter.get('filePath') or ''}`",
                f"- 目标字数：{chapter.get('wordTarget') or 0}（审查口径，目标区间 90%–120%）",
                f"- 优先级/分值：{chapter.get('priority') or '-'} / {chapter.get('scoreWeight') or 0}",
                "- 评审因素：",
            ]
        )
        for ref in chapter.get("criterionRefs") or []:
            lines.append(
                f"  - {ref.get('criterionId') or ''} {ref.get('factor') or ''}"
                f"（{ref.get('score') or 0} 分）：{str(ref.get('itemText') or '')[:300]}"
            )
        lines.append("- 内容要求：")
        for requirement in chapter.get("contentRequirements") or []:
            lines.append(f"  - {requirement}")
        handling = chapter.get("handling")
        if isinstance(handling, Mapping):
            lines.extend(
                [
                    "- **特殊章节**：",
                    f"  - 材料类型：{handling.get('materialType') or ''}",
                    f"  - 用户动作：{handling.get('userAction') or ''}",
                ]
            )
        figures = chapter.get("figurePlan") or []
        if figures:
            lines.append("- 图表计划：")
            for plan in figures:
                lines.append(
                    f"  - `{plan.get('anchorId')}` {plan.get('type')}"
                    + (f"/{plan.get('subtype')}" if plan.get("subtype") else "")
                    + f"：{plan.get('caption') or ''}（{plan.get('purpose') or ''}）"
                )
        dep_summaries = (chapter.get("context") or {}).get("dependencySummaries") or {}
        if dep_summaries:
            lines.append("- 依赖章节摘要：")
            for dep_id, summary in dep_summaries.items():
                lines.append(f"  - {dep_id}：{str(summary)[:300] or '（无摘要）'}")
        lines.append("")
    lines.extend(
        [
            "## 交付要求",
            "",
            "- 每章写 `chapters/_results/<chapterId>.chapter-result.json`（status/wordCount/summary/criterionRefs/figures/tables）。",
            "- 只写本批文件；不要运行 bg_render.py；不要改 outline.json。",
            "- 特殊章节不写图/表，不代填材料，不承诺已具备。",
            "- 商务/报价/资质证书内容不进技术正文。",
            "",
        ]
    )
    return "\n".join(lines)


def _batch_state_path(package_dir: Path) -> Path:
    return package_dir / BATCH_STATE


def update_batch_state(package_dir: Path, payload: Mapping[str, Any]) -> bool:
    path = _batch_state_path(package_dir)
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = jsonio.read_json(path)
            document = loaded if isinstance(loaded, dict) else {}
        except errors.BgError:
            document = {}
    batches = document.get("batches") if isinstance(document.get("batches"), Mapping) else {}
    batches[str(payload.get("batch"))] = {
        "chapterIds": [chapter.get("chapterId") for chapter in payload.get("chapters") or []],
        "status": "briefed",
        "generatedAt": paths.iso_now(),
        "batchSize": payload.get("batchSize"),
    }
    document.update(
        {
            "schemaVersion": "1.0",
            "packageId": payload.get("packageId"),
            "updatedAt": paths.iso_now(),
            "batchSize": DEFAULT_BATCH_SIZE,
            "batches": batches,
            "lastBatch": payload.get("batch"),
            "nextBatch": min(int(payload.get("batch") or 1) + 1, int(payload.get("totalBatches") or 1) + 1),
        }
    )
    jsonio.write_json_atomic(path, document)
    return True


def generate_briefs(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    batch_no: int = 1,
    all_batches: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    write: bool = True,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    outline_path = outline_validate.outline_path(package_dir)
    if not outline_path.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少 outline.json，无法生成写作任务书",
            hint="先完成 F5-1 大纲并 --materialize",
            detail={"path": str(outline_path)},
        )
    outline = jsonio.read_json(outline_path)
    leaves = _leaf_nodes(outline)
    batches = plan_batches(leaves, batch_size)
    if not batches:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "没有待写章节（全部 done/skipped）",
            hint="如仍要重写，先重置对应章节 completion 或直接给子智能体指定章节",
            detail={"leafCount": len(leaves)},
        )
    targets = list(range(1, len(batches) + 1)) if all_batches else [max(1, int(batch_no))]
    payloads: list[dict[str, Any]] = []
    written: list[str] = []
    for number in targets:
        if number > len(batches):
            continue
        payload = build_batch_payload(
            project=project_doc,
            package_meta=package_meta,
            outline=outline,
            batch_no=number,
            batch_size=batch_size,
        )
        payloads.append(payload)
        if write:
            out_dir = package_dir / BATCH_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            json_path = out_dir / f"batch-{number}.json"
            md_path = out_dir / f"batch-{number}.md"
            jsonio.write_json_atomic(json_path, payload)
            md_path.write_text(render_batch_markdown(payload), encoding="utf-8", newline="\n")
            written.extend([paths.rel_posix(project_dir_path, json_path), paths.rel_posix(project_dir_path, md_path)])
            update_batch_state(package_dir, payload)
    try:
        event_log.log_event(
            workspace_path,
            "write.brief_generated",
            actor="script",
            project=str(project_doc.get("projectId") or ""),
            package=target,
            detail={"batches": [payload.get("batch") for payload in payloads], "written": len(written), "batchSize": batch_size},
        )
    except (errors.BgError, ValueError):
        pass
    result: dict[str, Any] = {
        "ok": True,
        "command": "write_brief",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "totalLeaves": len(leaves),
        "totalBatches": len(batches),
        "batchSize": batch_size,
        "batchStatePath": BATCH_STATE,
        "written": written,
        "batches": payloads,
        "currentStage": state.get("currentStage"),
    }
    if len(payloads) == 1:
        result["batch"] = payloads[0]
        result["markdown"] = render_batch_markdown(payloads[0])
    return result


# ---------------------------------------------------------------------------
# CLI / selftest
# ---------------------------------------------------------------------------
def _selftest_outline() -> dict[str, Any]:
    def leaf(node_id: str, title: str, order: int, deps: list[str], status: str = "pending") -> dict[str, Any]:
        return {
            "id": node_id,
            "title": title,
            "level": 2,
            "kind": "leaf",
            "order": order,
            "dependsOn": deps,
            "wordTarget": 1000,
            "scoreWeight": 2,
            "criterionRefs": [{"criterionId": "CR-01", "factor": "项目理解", "itemText": title, "score": 2, "sourceRef": "x"}],
            "contentRequirements": ["要求 A"],
            "figurePlan": [],
            "insertAnchors": [],
            "completion": {"status": status, "wordCount": 1000 if status == "done" else 0, "summary": "摘要" if status == "done" else ""},
        }

    return {
        "schemaVersion": "1.0",
        "packageId": "package-1",
        "chapters": [
            {
                "id": "1",
                "title": "项目理解",
                "level": 1,
                "kind": "parent",
                "order": 1,
                "children": [
                    leaf("1-1", "现状", 1, []),
                    leaf("1-2", "目标", 2, []),
                    leaf("1-3", "重难点", 3, []),
                    leaf("1-4", "方案", 4, []),
                    leaf("1-5", "保障", 5, []),
                ],
            }
        ],
    }


def run_selftest() -> dict[str, Any]:
    checks: list[str] = []
    outline = _selftest_outline()
    leaves = _leaf_nodes(outline)
    batches = plan_batches(leaves, 4)
    if [len(batch) for batch in batches] != [4, 1]:
        raise AssertionError(f"批次切分不正确：{[len(batch) for batch in batches]}")
    checks.append("plan_batches")
    first = batches[0]
    first_ids = {node["id"] for node in first}
    if any(dep in first_ids for node in first for dep in node.get("dependsOn") or []):
        raise AssertionError("批内出现硬依赖")
    checks.append("batch_no_internal_dependency")
    payload = build_batch_payload(
        project={"projectName": "自检项目", "projectNo": "T-1"},
        package_meta={"packageId": "package-1", "packageName": "一包", "expectedWordCount": 5000, "figureQuota": {"table": 1, "mermaid": 1}},
        outline=outline,
        batch_no=1,
        batch_size=4,
    )
    markdown = render_batch_markdown(payload)
    if payload.get("batchSize") != 4 or "## 本批章节" not in markdown or "1-1" not in markdown:
        raise AssertionError("任务书 payload/markdown 不正确")
    checks.append("payload_markdown")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bidgenie-write-brief-") as tmp:
        package_dir = Path(tmp)
        update_batch_state(package_dir, payload)
        state_path = _batch_state_path(package_dir)
        state_doc = json.loads(state_path.read_text(encoding="utf-8"))
        if state_doc.get("batches", {}).get("1", {}).get("status") != "briefed":
            raise AssertionError("批次台账未写入")
        checks.append("batch_state")
    return {"ok": True, "selftest": {"script": "bg_write_brief.py", "checks": checks}}


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_write_brief.py",
        description="BidGenie F5-2 写作任务书生成器：按 outline + package-meta 生成批次 JSON/Markdown（只写 chapters/_batches/）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>（缺省 state.currentPackage）")
    parser.add_argument("--batch", type=int, default=1, help="生成第几批（1-based，默认 1）")
    parser.add_argument("--all", action="store_true", help="生成全部批次")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"每批章节数（默认 {DEFAULT_BATCH_SIZE}，建议 4–6）")
    parser.add_argument("--no-write", action="store_true", help="只输出，不写 _batches/ 文件")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not getattr(args, "project", None):
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return generate_briefs(
        workspace=workspace,
        project_dir=project_dir,
        package_id=getattr(args, "package", None),
        batch_no=int(getattr(args, "batch", 1) or 1),
        all_batches=bool(getattr(args, "all", False)),
        batch_size=int(getattr(args, "batch_size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE),
        write=not bool(getattr(args, "no_write", False)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_write_brief.py",
    )


if __name__ == "__main__":
    sys.exit(main())
