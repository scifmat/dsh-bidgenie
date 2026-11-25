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


def _seq_key(node: Mapping[str, Any]) -> tuple[int, int, str]:
    """按大纲先序 seq 排序；草稿/旧大纲没有 seq 时退回 order。"""
    seq = node.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return (0, seq, str(node.get("id") or ""))
    order = node.get("order")
    if isinstance(order, int) and not isinstance(order, bool):
        return (1, order, str(node.get("id") or ""))
    return (2, 0, str(node.get("id") or ""))


def plan_batches(leaves: Sequence[Mapping[str, Any]], batch_size: int = DEFAULT_BATCH_SIZE) -> list[list[dict[str, Any]]]:
    """按大纲顺序（seq）连续切批，批内按 priority 排序。

    P2-6（2026-11-23 冒烟）：旧实现按 dependsOn 分波次，第一批会跨章跳到
    `1-1/6-1/7-1/9-1`，写作连续性差。dependsOn 在连续 batch 中自然满足
    （依赖只指向更早章节），批内仍按 seq 顺序书写即可。尾批不足 4 时并入前批。
    """
    size = max(1, int(batch_size))
    pending = [dict(node) for node in leaves if _completion_status(node) not in ("done", "skipped")]
    pending.sort(key=_seq_key)
    if not pending:
        return []
    # N5：先算批数再均分（16 章 / 5 → 4/4/4/4），避免 5/5/5/1 的尾批 1 章。
    batch_count = (len(pending) + size - 1) // size
    base, extra = divmod(len(pending), batch_count)
    batch_sizes = [base + 1] * extra + [base] * (batch_count - extra)
    batches: list[list[dict[str, Any]]] = []
    cursor = 0
    for count in batch_sizes:
        batches.append(pending[cursor : cursor + count])
        cursor += count
    for batch in batches:
        batch.sort(key=lambda node: (_prio_rank(node), _seq_key(node)))
    return batches


def _prio_rank(node: Mapping[str, Any]) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(str(node.get("priority") or "P1"), 9)


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
        level = int(node.get("level") or 1)
        title = str(node.get("title") or "")
        figure_plan: list[dict[str, Any]] = []
        for plan in node.get("figurePlan") or []:
            if not isinstance(plan, Mapping):
                continue
            item = dict(plan)
            if str(item.get("type") or "") == "mermaid":
                # F-06：纵向 flowchart 节点过多会渲染成超高图（高宽比失败）；
                # 在任务书阶段就给出可执行约束，而不是等 bg_render 失败后返工。
                item["layoutHint"] = "flowchart 节点 >=8 时用 `flowchart LR` 或拆成两张图；避免纵向 >6 层（高宽比过大）"
            figure_plan.append(item)
        chapters.append(
            {
                "chapterId": chapter_id,
                "title": title,
                "level": level,
                "seq": node.get("seq"),
                "filePath": node.get("filePath") or "",
                # F-01（2026-11-25 轻量冒烟）：任务书直接给出可复制的首行/第二行，
                # 避免写作子智能体自由选择 H1/H2 或漏写 BG:CHAPTER meta。
                "heading": {
                    "firstLine": f"{'#' * level} {title}",
                    "metaLine": (
                        f"<!-- BG:CHAPTER id={chapter_id} level={level} "
                        f"wordTarget={node.get('wordTarget') or 0} seq={node.get('seq') or 0} -->"
                    ),
                },
                "wordTarget": node.get("wordTarget") or 0,
                "priority": node.get("priority") or "",
                "scoreWeight": node.get("scoreWeight") or 0,
                "dependsOn": _dependency_ids(node),
                "maxBodyHeadingLevel": (int(node.get("level") or 1) + 1),
                "criterionRefs": node.get("criterionRefs") or [],
                "contentRequirements": node.get("contentRequirements") or [],
                "figurePlan": figure_plan,
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
            # P2-8：A 层按 outline.level+1 判 heading_depth_exceeded，任务书必须显式约束
            "maxBodyHeadingLevel": "outline.level + 1（末级 level=2 时正文只能用 ###，禁止 ####）",
        },
        "writingRules": [
            "每章前两行必须逐字使用 heading.firstLine / heading.metaLine（首行 `#`×level，第二行 BG:CHAPTER）；不要改成 H1 或省略 meta。",
            "加载 bg-writing-style；正文不写跨章引用（如“详见第 3 章”）。",
            "图表按 figurePlan 生成；table 不写 BG:FIGURE，mermaid/terminal 才写标记。flowchart 节点 >=8 时用 `flowchart LR` 或拆图（避免纵向超高）。",
            "特殊章节 handling 不为空时按 bg-special-chapters 模板写，不产图/表。",
            "只写本章 md 与 resources 源文件；不改 outline.json，不渲染 png。",
            "正文小节标题最多下探到章节层级+1（末级 level=2 时只用 ###，不要 ####/#####）。",
            "chapter-result.figures[].status 只能是 produced / failed / skipped（不要写 generated/done）；tables[] 按实际表登记。",
            "技术正文不写商务/报价内容：预算/限价金额、付款比例/节点、价格评审公式/报价得分、资质证书编号。",
        ],
    }


def render_batch_markdown(payload: Mapping[str, Any]) -> str:
    facts = payload.get("projectFacts") or {}
    lines = [
        f"# 第 {payload.get('batch')} 批写作任务书（{payload.get('packageId')}）",
        "",
        f"- 本批章节：{payload.get('batchSize')} 章（全包 {payload.get('totalLeaves')} 章）；共 {payload.get('totalBatches')} 批",
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
                "- **首行（逐字复制，不要改层级/标题）**：",
                f"  `{(chapter.get('heading') or {}).get('firstLine') or ''}`",
                "- **第二行（逐字复制）**：",
                f"  `{(chapter.get('heading') or {}).get('metaLine') or ''}`",
                f"- 正文标题上限：`{'#' * int(chapter.get('maxBodyHeadingLevel') or 3)}`（不要写更深层级）",
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
                    + (f"；{plan.get('layoutHint')}" if plan.get("layoutHint") else "")
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
            "- `figures[].status` 只能是 `produced` / `failed` / `skipped`；渲染前源文件已写就填 `produced`，不要写 `generated`/`done`。",
            "- 只写本批文件；不要运行 bg_render.py；不要改 outline.json。",
            "- 特殊章节不写图/表，不代填材料，不承诺已具备。",
            "- 商务/报价/资质证书内容不进技术正文（不写预算/限价金额、付款比例/节点、价格评审公式/报价得分）。",
            "",
        ]
    )
    return "\n".join(lines)


def _batch_state_path(package_dir: Path) -> Path:
    return package_dir / BATCH_STATE


def update_batch_state(package_dir: Path, payload: Mapping[str, Any], *, configured_batch_size: int | None = None) -> bool:
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
    # P2-7：台账批大小必须回写本次实际配置，不能永远写 DEFAULT_BATCH_SIZE。
    batch_size = int(configured_batch_size or payload.get("batchSize") or DEFAULT_BATCH_SIZE)
    document.update(
        {
            "schemaVersion": "1.0",
            "packageId": payload.get("packageId"),
            "updatedAt": paths.iso_now(),
            "batchSize": batch_size,
            "batchSizes": {str(key): value.get("batchSize") for key, value in batches.items() if isinstance(value, Mapping)},
            "batches": batches,
            "lastBatch": payload.get("batch"),
            "nextBatch": min(int(payload.get("batch") or 1) + 1, int(payload.get("totalBatches") or 1) + 1),
        }
    )
    jsonio.write_json_atomic(path, document)
    return True


def summarize_brief(payload: Mapping[str, Any]) -> dict[str, Any]:
    """P2-6：`--all --brief` 的轻量返回，避免几十 KB 的 chapters/projectFacts 回显。"""
    batch = int(payload.get("batch") or 1)
    return {
        "batch": batch,
        "batchSize": payload.get("batchSize"),
        "totalBatches": payload.get("totalBatches"),
        "draftJsonPath": f"{BATCH_DIR}/batch-{batch}.json",
        "draftMarkdownPath": f"{BATCH_DIR}/batch-{batch}.md",
    }


def generate_briefs(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    batch_no: int = 1,
    all_batches: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    write: bool = True,
    brief: bool = False,
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
            update_batch_state(package_dir, payload, configured_batch_size=batch_size)
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
        "batchSizes": [payload.get("batchSize") for payload in payloads],
        "batchStatePath": BATCH_STATE,
        "written": written,
        "brief": brief,
    }
    if brief:
        result["batches"] = [summarize_brief(payload) for payload in payloads]
    else:
        result["batches"] = payloads
        if len(payloads) == 1:
            result["batch"] = payloads[0]
            result["markdown"] = render_batch_markdown(payloads[0])
    result["currentStage"] = state.get("currentStage")
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
    if [len(batch) for batch in batches] != [3, 2]:
        raise AssertionError(f"批次切分不正确（应均衡 3/2）：{[len(batch) for batch in batches]}")
    if [node["id"] for node in batches[0]] != ["1-1", "1-2", "1-3"]:
        raise AssertionError(f"批次未按大纲顺序连续切分：{[node['id'] for node in batches[0]]}")
    checks.append("plan_batches_seq_contiguous")
    shuffled = [leaves[2], leaves[0], leaves[4], leaves[1], leaves[3]]
    shuffled_batches = plan_batches(shuffled, 2)
    if [node["id"] for batch in shuffled_batches for node in batch] != ["1-1", "1-2", "1-3", "1-4", "1-5"]:
        raise AssertionError(f"批次未按 seq 排序：{[[node['id'] for node in batch] for batch in shuffled_batches]}")
    checks.append("plan_batches_sorts_by_seq")
    payload = build_batch_payload(
        project={"projectName": "自检项目", "projectNo": "T-1"},
        package_meta={"packageId": "package-1", "packageName": "一包", "expectedWordCount": 5000, "figureQuota": {"table": 1, "mermaid": 1}},
        outline=outline,
        batch_no=1,
        batch_size=4,
    )
    markdown = render_batch_markdown(payload)
    if payload.get("batchSize") != 3 or "## 本批章节" not in markdown or "1-1" not in markdown:
        raise AssertionError("任务书 payload/markdown 不正确")
    if "本批章节：3 章（全包 5 章）；共 2 批" not in markdown:
        raise AssertionError(f"任务书头部口径不正确：{markdown.splitlines()[:5]}")
    first_chapter = payload["chapters"][0]
    heading = first_chapter.get("heading") or {}
    if heading.get("firstLine") != "## 现状":
        raise AssertionError(f"任务书首行模板不正确：{heading}")
    if heading.get("metaLine") != "<!-- BG:CHAPTER id=1-1 level=2 wordTarget=1000 seq=0 -->":
        raise AssertionError(f"任务书 meta 模板不正确：{heading}")
    if f"`{heading['firstLine']}`" not in markdown or f"`{heading['metaLine']}`" not in markdown:
        raise AssertionError("任务书未内联可复制的首行/meta 模板")
    checks.append("payload_markdown")
    brief_item = summarize_brief(payload)
    if (
        brief_item.get("batch") != 1
        or brief_item.get("batchSize") != 3
        or brief_item.get("draftJsonPath") != "chapters/_batches/batch-1.json"
        or "chapters" in brief_item
        or "projectFacts" in brief_item
    ):
        raise AssertionError(f"--brief 摘要不正确：{brief_item}")
    checks.append("brief_summary")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bidgenie-write-brief-") as tmp:
        package_dir = Path(tmp)
        update_batch_state(package_dir, payload, configured_batch_size=4)
        state_path = _batch_state_path(package_dir)
        state_doc = json.loads(state_path.read_text(encoding="utf-8"))
        if state_doc.get("batches", {}).get("1", {}).get("status") != "briefed":
            raise AssertionError("批次台账未写入")
        if state_doc.get("batchSize") != 4:
            raise AssertionError(f"批次台账 batchSize 未回写实际值：{state_doc.get('batchSize')}")
        update_batch_state(package_dir, payload, configured_batch_size=5)
        state_doc = json.loads(state_path.read_text(encoding="utf-8"))
        if state_doc.get("batchSize") != 5:
            raise AssertionError(f"批次台账 batchSize 未按 --batch-size 回写：{state_doc.get('batchSize')}")
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
    parser.add_argument("--full", action="store_true", help="--all 时仍返回完整 chapters/projectFacts（默认 --all 精简输出）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not getattr(args, "project", None):
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    all_batches = bool(getattr(args, "all", False))
    brief = bool(getattr(args, "brief", False)) or (all_batches and not bool(getattr(args, "full", False)))
    return generate_briefs(
        workspace=workspace,
        project_dir=project_dir,
        package_id=getattr(args, "package", None),
        batch_no=int(getattr(args, "batch", 1) or 1),
        all_batches=all_batches,
        batch_size=int(getattr(args, "batch_size", DEFAULT_BATCH_SIZE) or DEFAULT_BATCH_SIZE),
        write=not bool(getattr(args, "no_write", False)),
        brief=brief,
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
