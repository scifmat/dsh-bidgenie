#!/usr/bin/env python3
"""BidGenie F6 审查脚本层（A 层，PRD F6.2–F6.7 / 7.5 / 4.6）。

职责（确定性）：
- 读取 `chapters/_outline/outline.json`、`chapters/**/*.md`、`chapters/_results/*.chapter-result.json`、
  `resources/**`、`package-meta.json`；
- A 层检查：结构/文件树一致性、文档完整性、标题层级、字数（8.7.1 阈值）、图表标记/文件/题注/表格、
  图表配额对账、引用完整性、JSON 合法性、禁用内容；特殊章节（handling）免字数/图表检查，只要求存在/非空/done；
- 产出 `reviews/issues.json`（7.5 schema）、`reviews/rounds/round-<k>.issues.json`、
  `reviews/review-report.md`（含 criterionId×chapterId 覆盖率矩阵、字数/图表清单）；
- A 层 blocker 时**短路** B/C（报告标记 skipped）；无 blocker 时标记 B/C 待语义审查（占位，不生成假问题）。
- 文风/自指/跨章一致性留给语义审查，本脚本不做硬门禁。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib, text as text_lib, tempdir
import bg_outline_validate as outline_validate
import bg_review_issues as review_issues

SCHEMA_VERSION = "1.0"
ISSUES_FILE = "reviews/issues.json"
REPORT_FILE = "reviews/review-report.md"
SUMMARY_FILE = "reviews/review-summary.json"
RENDER_MANIFEST_FILE = "resources/render-manifest.json"
ISSUE_ID_RE = re.compile(r"^RV-(\d+)-(\d{4})$")
HEADING_RE = re.compile(r"^(#{1,7})\s+(.+?)\s*$")
META_COMMENT_RE = re.compile(r"^<!--\s*BG:CHAPTER\b.*-->$")
FIGURE_MARKER_RE = re.compile(r"<!--\s*BG:FIGURE:([A-Za-z0-9-]+)\s*-->")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
FORBIDDEN_PATTERNS = (
    (r"待补充", "placeholder_left"),
    (r"TODO", "placeholder_left"),
    (r"占位符", "placeholder_left"),
    (r"[A-Za-z]:\\\\", "forbidden_content"),
    (r"file://", "forbidden_content"),
    (r"详见第\s*[0-9一二三四五六七八九十]+\s*章", "forbidden_content"),
    (r"见商务部分|见资格部分|证明材料见", "forbidden_content"),
)
#: P0-3：题注只拦真正的“编号形态”；「1包」「2 包」「2026年」「1.5mm」等业务数字放行。
CAPTION_NUMBERING_RE = re.compile(
    r"^[▼▲][表图]\s*(?:"
    r"第\s*\d+\s*[章节部分篇条]"
    r"|[（(]\s*\d+\s*[)）]"
    r"|\d{1,2}\s*[、.．)）](?!\d)"
    r"|[一二三四五六七八九十]{1,2}\s*[、.．]"
    r"|[表图]\s*\d"
    r")"
)
#: P2-4：技术正文混入商务/报价内容（A 层机检，写作任务书已提前约束）
BUSINESS_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"评标基准价|投标报价得分|报价得分|价格分(?:计算|评审|权重)",
        "business_content_mixed",
        "技术正文删除价格评审/报价得分/评标基准价，相关内容只在商务/报价文件",
    ),
    (
        r"(?:最高限价|预算金额|采购预算|限价)\s*[：:]?\s*[0-9０-９]",
        "business_content_mixed",
        "技术正文不写预算/最高限价金额，删除后改为定性描述",
    ),
    (
        r"付款(?:比例|节点|方式为|方式：)|货款支付|预付(?:款|比例)\s*[0-9０-９]",
        "business_content_mixed",
        "技术正文不写付款比例/付款节点/货款支付，相关内容只在商务文件",
    ),
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
#: P2-10：字数口径唯一实现在 `_lib/text.py`（CJK 字符 + ASCII 词，剥离注释与标题行）。
#: 这里直接引用其正文口径函数，禁止再写本地正则副本；run_selftest 依赖本名字。
count_words = text_lib.count_body_words


def caption_like_number(caption: str) -> bool:
    """R-13：题注去掉 ▼表/▲图 前缀后以「数字+量词」开头（会读成「表3」式编号）。"""
    body = re.sub(r"^[▼▲][表图]\s*", "", str(caption or ""))
    return bool(text_lib.caption_number_prefix(body))


#: D7（第三轮冒烟）：跨章数量索引用的「名词 + 数量 + 单位」模式（只做索引，不判问题）。
QUANTITY_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,10})\s*[^\d\n。；;，,、]{0,6}?(\d+(?:\.\d+)?)\s*"
    r"(套|根|个|台|辆|项|块|处|段|人|班|夜|天|个日历日|㎡|m²|平方米|米|公里|千米|吨|件|份)"
)


def build_quantity_index(chapter_texts: Mapping[str, str], *, limit: int = 30) -> list[dict[str, Any]]:
    """把各章「同类指标」的数值并列出来供 B 层核对（D7；不生成问题，避免误报）。"""
    seen: dict[tuple[str, str], dict[str, set[str]]] = {}
    for chapter_id, text in chapter_texts.items():
        body = body_text(text)
        for match in QUANTITY_RE.finditer(body):
            noun = match.group(1).strip()
            value = match.group(2)
            unit = match.group(3)
            if len(noun) < 2 or noun in ("合计", "共计", "其中", "上述"):
                continue
            seen.setdefault((noun, unit), {}).setdefault(chapter_id, set()).add(value)
    rows: list[dict[str, Any]] = []
    for (noun, unit), by_chapter in seen.items():
        if len(by_chapter) < 2:
            continue
        values = {value for chapter_values in by_chapter.values() for value in chapter_values}
        if len(values) < 2:
            continue
        rows.append(
            {
                "label": f"{noun}（{unit}）",
                "chapterCount": len(by_chapter),
                "chapters": {chapter_id: sorted(chapter_values) for chapter_id, chapter_values in by_chapter.items()},
            }
        )
    rows.sort(key=lambda item: (-int(item.get("chapterCount") or 0), str(item.get("label") or "")))
    return rows[:limit]


def markdown_tables(text: str) -> list[dict[str, Any]]:
    """提取 GFM 表格：返回 {caption, line, rows, header}。"""
    lines = text.splitlines()
    tables: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(lines):
        line = lines[index]
        separator = lines[index + 1]
        if TABLE_ROW_RE.match(line) and TABLE_SEPARATOR_RE.match(separator):
            header = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows: list[list[str]] = []
            cursor = index + 2
            while cursor < len(lines) and TABLE_ROW_RE.match(lines[cursor]):
                rows.append([cell.strip() for cell in lines[cursor].strip().strip("|").split("|")])
                cursor += 1
            caption = ""
            for back in range(index - 1, max(-1, index - 4), -1):
                candidate = lines[back].strip()
                if not candidate:
                    continue
                if candidate.startswith("▼表"):
                    caption = candidate
                break
            tables.append({"caption": caption, "line": index + 1, "header": header, "rows": rows})
            index = cursor
            continue
        index += 1
    return tables


def heading_info(text: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    in_code = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = HEADING_RE.match(line.strip())
        if match:
            headings.append({"level": len(match.group(1)), "title": match.group(2), "line": line_number})
    return headings


def body_text(text: str) -> str:
    """去掉首行标题、第二行 BG:CHAPTER meta、HTML 注释后的正文。"""
    lines = text.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == 0 and re.match(r"^#{1,6}\s+", stripped):
            continue
        if index == 1 and META_COMMENT_RE.match(stripped):
            continue
        output.append(line)
    return re.sub(r"<!--.*?-->", "", "\n".join(output), flags=re.DOTALL)


def png_ok(path: Path) -> tuple[bool, str]:
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            stat = ImageStat.Stat(image.convert("RGB"))
            variance = max(stat.var)
        if width < 200 or height < 150:
            return False, f"尺寸 {width}×{height} < 200×150"
        if variance <= 1.0:
            return False, f"近似纯色（variance={variance:.2f}）"
        return True, f"{width}×{height}"
    except Exception as exc:  # noqa: BLE001
        return False, f"不可解析：{type(exc).__name__}"


def severity_rank(severity: str) -> int:
    return {"blocker": 0, "major": 1, "minor": 2}.get(severity, 3)


class IssueCollector:
    def __init__(self) -> None:
        self._issues: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()

    def add(
        self,
        layer: str,
        issue_type: str,
        severity: str,
        *,
        chapter_id: str = "",
        file_path: str = "",
        anchor_id: str | None = None,
        evidence: str = "",
        suggestion: str = "",
    ) -> None:
        key = (layer, issue_type, chapter_id, anchor_id, evidence[:120])
        if key in self._issues:
            return
        now = paths.iso_now()
        self._issues[key] = {
            "issueId": "",
            "layer": layer,
            "type": issue_type,
            "severity": severity,
            "chapterId": chapter_id,
            "filePath": file_path,
            "anchorId": anchor_id,
            "evidence": evidence[:500],
            "suggestion": suggestion[:500],
            "status": "open",
            "userConfirmed": False,
            "createdAt": now,
            "updatedAt": now,
            "resolvedAt": None,
            "resolutionNote": "",
        }

    def raw_items(self) -> list[dict[str, Any]]:
        """本轮新检出问题（未分配 issueId，供 carry-forward 去重后再编号）。"""
        issues = list(self._issues.values())
        issues.sort(
            key=lambda item: (
                severity_rank(item["severity"]),
                str(item.get("chapterId") or ""),
                str(item.get("type") or ""),
                str(item.get("anchorId") or ""),
                str(item.get("filePath") or ""),
            )
        )
        return issues

    def items(self, round_no: int) -> list[dict[str, Any]]:
        issues = self.raw_items()
        for index, item in enumerate(issues, start=1):
            item["issueId"] = f"RV-{round_no}-{index:04d}"
        return issues


# ---------------------------------------------------------------------------
# A 层检查
# ---------------------------------------------------------------------------
def load_inputs(
    workspace: Path, project_dir: Path, package_id: str | None
) -> tuple[str, Path, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    outline_path = outline_validate.outline_path(package_dir)
    if not outline_path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 chapters/_outline/outline.json", hint="先完成 M5 大纲落盘")
    try:
        outline = jsonio.read_json(outline_path)
    except errors.BgError as exc:
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"outline.json 不可解析：{exc.message}") from exc
    return target, package_dir, outline, package_meta, state, project_doc


def collect_chapter_tree(package_dir: Path, chapters_dir: Path) -> tuple[set[str], set[str]]:
    """收集章节树中应参与对账的目录/文件（排除元数据目录）。

    P0-1（2026-11-23 冒烟）：`bg_write_brief.py` 会写 `chapters/_batches/`；旧实现只排除
    `_outline/_results/_frozen/resources`，导致 F6 必现 12 条 `extra_chapter` blocker。
    约定：章节树中以 `_` 开头的目录一律视为元数据目录（`_outline/_results/_frozen/_batches`），
    不做章节对账；`resources` 为历史遗留章节内资源目录，继续跳过。
    """
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    if not chapters_dir.is_dir():
        return actual_dirs, actual_files
    for entry in sorted(chapters_dir.rglob("*")):
        rel = entry.relative_to(package_dir).as_posix()
        parts = entry.relative_to(chapters_dir).parts
        if any(paths.is_metadata_dir(part) for part in parts) or "resources" in parts:
            continue
        if entry.name.startswith(".") or entry.name.endswith(".lock"):
            continue
        if entry.is_dir():
            actual_dirs.add(rel.rstrip("/"))
        elif entry.is_file():
            actual_files.add(rel)
    return actual_dirs, actual_files


def mermaid_fallback_reason(anchor_meta: Any) -> str:
    """渲染清单中 mermaid 图是否降级为 Pillow 占位图；是则返回原因。

    P0-2：node 缺失时 `bg_render.py` 会静默写占位 PNG（status=fallback、
    engine=pillow-fallback）。A 层必须把它升级为 blocker，避免用户拿到全占位图
    方案还以为审查通过。terminal 图正常就是 Pillow 绘制，不在本函数范围。
    """
    if not isinstance(anchor_meta, Mapping):
        return ""
    status = str(anchor_meta.get("status") or "")
    engine = str(anchor_meta.get("engine") or "")
    if status != "fallback" and engine != "pillow-fallback":
        return ""
    reason = str(anchor_meta.get("fallbackReason") or "").strip()
    return reason or "node/浏览器不可用"


def review_a_layer(
    *,
    package_dir: Path,
    outline: Mapping[str, Any],
    package_meta: Mapping[str, Any],
    collector: IssueCollector,
) -> dict[str, Any]:
    nodes = list(outline_validate.iter_preorder(outline.get("chapters") or []))
    leaves = [node for node in nodes if not (isinstance(node.get("children"), list) and node.get("children"))]
    render_manifest: dict[str, Any] = {}
    render_manifest_path = package_dir / RENDER_MANIFEST_FILE
    if render_manifest_path.is_file():
        try:
            loaded_manifest = jsonio.read_json(render_manifest_path)
            if isinstance(loaded_manifest, Mapping):
                render_manifest = dict(loaded_manifest)
        except errors.BgError:
            render_manifest = {}
    render_anchors = render_manifest.get("anchors") if isinstance(render_manifest.get("anchors"), Mapping) else {}
    chapter_rows: list[dict[str, Any]] = []
    pending_chapters: list[dict[str, Any]] = []
    chapter_texts: dict[str, str] = {}
    special_rows: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    figure_rows: list[dict[str, Any]] = []
    word_total = 0
    expected_word = int(package_meta.get("expectedWordCount") or 0)
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    chapters_dir = package_dir / "chapters"
    if chapters_dir.is_dir():
        actual_dirs, actual_files = collect_chapter_tree(package_dir, chapters_dir)
    expected_dirs: set[str] = set()
    expected_files: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node.get("children"), list) and node.get("children"):
            dir_path = node.get("dirPath")
            if isinstance(dir_path, str) and dir_path:
                expected_dirs.add(dir_path.rstrip("/"))
        else:
            file_path = node.get("filePath")
            if isinstance(file_path, str) and file_path:
                expected_files[file_path] = node
    for missing_dir in sorted(expected_dirs - actual_dirs):
        collector.add("script", "missing_chapter", "blocker", evidence=f"缺少章节目录：{missing_dir}", suggestion="运行 bg_outline_apply.py --materialize 或补齐目录")
    for extra_dir in sorted(actual_dirs - expected_dirs):
        collector.add("script", "extra_chapter", "blocker", evidence=f"多余章节目录：{extra_dir}", suggestion="删除或回写大纲后重新 materialize")
    for missing_file in sorted(set(expected_files) - actual_files):
        node = expected_files[missing_file]
        collector.add(
            "script",
            "missing_chapter",
            "blocker",
            chapter_id=str(node.get("id") or ""),
            file_path=missing_file,
            evidence=f"缺少章节文件：{missing_file}",
            suggestion="运行 bg_outline_apply.py --materialize 补齐空文件后写作",
        )
    for extra_file in sorted(actual_files - set(expected_files)):
        collector.add("script", "extra_chapter", "blocker", file_path=extra_file, evidence=f"多余文件：{extra_file}", suggestion="删除或加入大纲后重新生成")
    # 标题/完整性/字数/禁用内容
    for node in leaves:
        chapter_id = str(node.get("id") or "")
        file_path = str(node.get("filePath") or "")
        path = package_dir / file_path
        target_word = node.get("wordTarget")
        target_word = target_word if isinstance(target_word, int) else 0
        special = outline_validate.is_special_chapter(node)
        handling = node.get("handling") if isinstance(node.get("handling"), Mapping) else {}
        if special:
            special_rows.append(
                {
                    "chapterId": chapter_id,
                    "title": str(node.get("title") or ""),
                    "materialType": str(handling.get("materialType") or ""),
                    "requirementCount": len(handling.get("requirements") or []) if isinstance(handling.get("requirements"), list) else 0,
                    "userAction": str(handling.get("userAction") or ""),
                }
            )
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        completion_status = str(completion.get("status") or "pending")
        # R-14：写作中途 dry-run 不应把 pending 章节按空文档/字数不足报 blocker；
        # 这些章节单列 pendingChapters，仅审已完成章节（写完后重跑审查）。
        pending_chapter = completion_status not in ("done", "skipped")
        if not path.is_file():
            if pending_chapter:
                pending_chapters.append(
                    {"chapterId": chapter_id, "status": completion_status, "title": str(node.get("title") or ""), "filePath": file_path, "wordCount": 0}
                )
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        word_count = count_words(text)
        word_total += word_count
        if pending_chapter:
            pending_chapters.append(
                {
                    "chapterId": chapter_id,
                    "status": completion_status,
                    "title": str(node.get("title") or ""),
                    "filePath": file_path,
                    "wordCount": word_count,
                }
            )
            chapter_rows.append(
                {
                    "chapterId": chapter_id,
                    "filePath": file_path,
                    "wordCount": word_count,
                    "wordTarget": target_word,
                    "ratio": None,
                    "judgement": "未完成（未审）",
                    "special": special,
                    "wordCheck": "skipped",
                }
            )
            continue
        chapter_texts[chapter_id] = text
        headings = heading_info(text)
        if headings:
            first = headings[0]
            expected_level = node.get("level")
            if expected_level is not None and first["level"] != expected_level:
                collector.add(
                    "script",
                    "heading_level_mismatch",
                    "blocker",
                    chapter_id=chapter_id,
                    file_path=file_path,
                    evidence=f"首行标题层级 {first['level']} != outline.level {expected_level}",
                    suggestion="修正首行标题为 outline 指定层级",
                )
            if first["title"].strip() != str(node.get("title") or "").strip():
                collector.add(
                    "script",
                    "heading_level_mismatch",
                    "major",
                    chapter_id=chapter_id,
                    file_path=file_path,
                    evidence=f"首行标题「{first['title']}」与 outline.title「{node.get('title')}」不一致",
                    suggestion="保持首行标题与 outline 一致",
                )
            base_level = int(node.get("level") or first["level"])
            for heading in headings:
                if heading["level"] > 6:
                    collector.add("script", "heading_depth_exceeded", "blocker", chapter_id=chapter_id, file_path=file_path, evidence=f"第 {heading['line']} 行标题深度 {heading['level']} > 6", suggestion="改用加粗段落/列表承载更深层级")
                elif heading["level"] > base_level + 1:
                    collector.add(
                        "script",
                        "heading_depth_exceeded",
                        "blocker",
                        chapter_id=chapter_id,
                        file_path=file_path,
                        evidence=f"第 {heading['line']} 行标题深度 {heading['level']} > 基准{base_level}+1",
                        suggestion=f"正文只允许相对章节层级下探 1 级（本章 level={base_level}，最多 {'#' * (base_level + 1)}）",
                    )
        else:
            collector.add("script", "heading_level_mismatch", "blocker", chapter_id=chapter_id, file_path=file_path, evidence="缺少首行 Markdown 标题", suggestion="首行补 `#`×level + 标题")
        body = body_text(text)
        compact = re.sub(r"\s+", "", body)
        min_body = 50 if special else 200
        if len(compact) < min_body:
            collector.add(
                "script",
                "empty_document",
                "blocker",
                chapter_id=chapter_id,
                file_path=file_path,
                evidence=f"正文长度 {len(compact)} 字符 < {min_body}（空/占位文档）",
                suggestion="按 bg-special-chapters 模板补「要求 + 处理方式 + 请用户补充」" if special else "按 contentRequirements 补写正文",
            )
        if special:
            ratio = None
            judgement = "special"
        else:
            ratio = (word_count / target_word) if target_word else None
            judgement = "pass"
            if ratio is not None:
                if ratio < 0.60:
                    judgement = "blocker_low"
                    collector.add("script", "word_count_low", "blocker", chapter_id=chapter_id, file_path=file_path, evidence=f"实际 {word_count} 字 / 目标 {target_word}（{ratio:.1%}）<60%", suggestion="扩写至目标 90–120%")
                elif ratio < 0.90:
                    judgement = "major_low"
                    collector.add("script", "word_count_low", "major", chapter_id=chapter_id, file_path=file_path, evidence=f"实际 {word_count} 字 / 目标 {target_word}（{ratio:.1%}）低于 90%", suggestion="扩写至目标 90–120%")
                elif ratio <= 1.20:
                    judgement = "pass"
                elif ratio <= 1.40:
                    judgement = "major_high"
                    collector.add("script", "word_count_high", "major", chapter_id=chapter_id, file_path=file_path, evidence=f"实际 {word_count} 字 / 目标 {target_word}（{ratio:.1%}）高于 120%", suggestion="精简至目标 90–120%")
                else:
                    judgement = "blocker_high"
                    collector.add("script", "word_count_high", "blocker", chapter_id=chapter_id, file_path=file_path, evidence=f"实际 {word_count} 字 / 目标 {target_word}（{ratio:.1%}）>140%", suggestion="精简至目标 90–120%")
        chapter_rows.append(
            {
                "chapterId": chapter_id,
                "filePath": file_path,
                "wordCount": word_count,
                "wordTarget": target_word,
                "ratio": ratio,
                "judgement": judgement,
                "special": special,
                "wordCheck": "skipped" if special else "checked",
            }
        )
        for pattern, issue_type in FORBIDDEN_PATTERNS:
            match = re.search(pattern, text)
            if match:
                collector.add("script", issue_type, "major", chapter_id=chapter_id, file_path=file_path, evidence=f"命中禁用内容「{match.group(0)[:40]}」", suggestion="删除占位/绝对路径/证明材料提示")
        for pattern, issue_type, suggestion in BUSINESS_PATTERNS:
            match = re.search(pattern, text)
            if match:
                collector.add(
                    "script",
                    issue_type,
                    "major",
                    chapter_id=chapter_id,
                    file_path=file_path,
                    evidence=f"技术正文混入商务/报价内容「{match.group(0)[:40]}」",
                    suggestion=suggestion,
                )
        if special:
            # 特殊章节不规划图、免图表检查；正文只写要求/处理方式/请用户补充。
            continue
        # 表格题注（所有 GFM 表格）
        for table in markdown_tables(text):
            caption = table["caption"]
            table_rows.append({"chapterId": chapter_id, "line": table["line"], "caption": caption, "rows": len(table["rows"])})
            if not caption:
                collector.add("script", "table_missing", "major", chapter_id=chapter_id, file_path=file_path, evidence=f"第 {table['line']} 行表格上方缺少「▼表」题注", suggestion="表格上方补独立题注行 `▼表<题名>`（不带编号）")
            elif CAPTION_NUMBERING_RE.match(caption):
                collector.add("script", "caption_format", "major", chapter_id=chapter_id, file_path=file_path, evidence=f"表题带编号：{caption[:60]}", suggestion="使用 `▼表<题名>`，不带编号（「1包」「2026年」等业务数字开头不算编号）")
            elif caption_like_number(caption):
                collector.add(
                    "script",
                    "caption_format",
                    "minor",
                    chapter_id=chapter_id,
                    file_path=file_path,
                    evidence=f"表题以数字+量词开头（读作表号）：{caption[:60]}",
                    suggestion="改成「各条路段…」「各类…」等概括说法，避免「表3」歧义（R-13）",
                )
            if not table["header"]:
                collector.add("script", "table_mismatch", "major", chapter_id=chapter_id, file_path=file_path, evidence=f"第 {table['line']} 行表格无表头", suggestion="补齐可读表头")
        # 图表规划
        for plan in node.get("figurePlan") or []:
            if not isinstance(plan, Mapping):
                continue
            figure_type = str(plan.get("type") or "")
            anchor_id = str(plan.get("anchorId") or "")
            caption = str(plan.get("caption") or "")
            marker_found = bool(re.search(r"<!--\s*BG:FIGURE:" + re.escape(anchor_id) + r"\s*-->", text))
            figure_row = {"chapterId": chapter_id, "anchorId": anchor_id, "type": figure_type, "subtype": plan.get("subtype"), "marker": marker_found, "status": "planned", "detail": ""}
            if figure_type == "table":
                # bg-write 硬规则：table 只写 `▼表` 题注，不得写 BG:FIGURE 标记（P1-4 配套：A 层拦截）
                if marker_found:
                    figure_row["detail"] = "误加 BG:FIGURE 标记"
                    collector.add(
                        "script",
                        "marker_format",
                        "major",
                        chapter_id=chapter_id,
                        file_path=file_path,
                        anchor_id=anchor_id,
                        evidence=f"table 计划图表不得写 BG:FIGURE 标记（bg-write 硬规则），却检出 `<!-- BG:FIGURE:{anchor_id} -->`",
                        suggestion=f"删除 `<!-- BG:FIGURE:{anchor_id} -->`；表格只用独立 `▼表` 题注，锚点由 outline.figurePlan 定位",
                    )
                table_row = next((row for row in markdown_tables(text) if row["caption"]), None)
                if table_row is None:
                    figure_row["status"] = "missing"
                    collector.add("script", "table_missing", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"章节无带 ▼表 题注的表格（计划表图：{caption}）", suggestion="按 figurePlan 补充表格与表题")
                else:
                    figure_row["status"] = "produced"
                figure_rows.append(figure_row)
                continue
            resource_dir = package_dir / "resources" / figure_type
            png_path = resource_dir / f"{anchor_id}.png"
            suffix = ".mmd" if figure_type == "mermaid" else (".txt" if figure_type == "terminal" else None)
            source_path = resource_dir / f"{anchor_id}{suffix}" if suffix else None
            if not marker_found:
                collector.add("script", "marker_format", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"正文缺少插入标记 `<!-- BG:FIGURE:{anchor_id} -->`", suggestion="按 figurePlan 补插入标记与图题")
            else:
                position = text.find(f"<!-- BG:FIGURE:{anchor_id} -->")
                following = text[position + len(f"<!-- BG:FIGURE:{anchor_id} -->") :].splitlines()
                next_line = next((line.strip() for line in following if line.strip()), "")
                if not next_line.startswith("▲图"):
                    collector.add("script", "caption_format", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"标记下一行缺少「▲图」题注：{next_line[:60]}", suggestion="标记下一行写独立 `▲图<题名>`（不带编号）")
                elif CAPTION_NUMBERING_RE.match(next_line):
                    collector.add("script", "caption_format", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"图题带编号：{next_line[:60]}", suggestion="题注不带编号（「1包」「2026年」等业务数字开头不算编号）")
                elif caption_like_number(next_line):
                    collector.add(
                        "script",
                        "caption_format",
                        "minor",
                        chapter_id=chapter_id,
                        file_path=file_path,
                        anchor_id=anchor_id,
                        evidence=f"图题以数字+量词开头（读作编号）：{next_line[:60]}",
                        suggestion="改成「各…」等概括说法，避免读成「图3」（R-13）",
                    )
            anchor_meta = render_anchors.get(anchor_id) if isinstance(render_anchors, Mapping) else None
            render_fallback = ""
            if figure_type == "mermaid":
                render_fallback = mermaid_fallback_reason(anchor_meta)
                if render_fallback:
                    figure_row["status"] = "fallback"
                    collector.add(
                        "script",
                        "figure_render_fallback",
                        "blocker",
                        chapter_id=chapter_id,
                        file_path=file_path,
                        anchor_id=anchor_id,
                        evidence=f"mermaid 图渲染降级为占位图：{render_fallback}",
                        suggestion="按 env-check 的 browser_renderer 修复 node/浏览器后，用 bg_render.py --force --anchor <anchorId> 重渲染",
                    )
            if not png_path.is_file():
                figure_row["status"] = "missing"
                collector.add("script", "figure_missing", "blocker", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"缺少资源文件：resources/{figure_type}/{anchor_id}.png", suggestion="运行 bg_render.py 生成 PNG")
            else:
                ok, detail = png_ok(png_path)
                figure_row["detail"] = detail
                if not ok:
                    figure_row["status"] = "invalid"
                    collector.add("script", "figure_quality", "blocker", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"PNG 不合格：{detail}", suggestion="重新渲染/更换资源")
                else:
                    figure_row["status"] = "fallback" if render_fallback else "produced"
                    layout = anchor_meta.get("layout") if isinstance(anchor_meta, Mapping) else None
                    if isinstance(layout, Mapping):
                        figure_row["layout"] = {
                            key: layout.get(key)
                            for key in ("diagramType", "fontPtAtDisplay", "displayWidthCm", "aspectTall", "aspectWide", "lineCount", "maxColumns")
                            if layout.get(key) is not None
                        }
                        for layout_warning in layout.get("warnings") or []:
                            if not isinstance(layout_warning, Mapping):
                                continue
                            severity = str(layout_warning.get("severity") or "")
                            if severity not in ("major", "minor"):
                                continue
                            collector.add(
                                "script",
                                "figure_quality",
                                severity,
                                chapter_id=chapter_id,
                                file_path=file_path,
                                anchor_id=anchor_id,
                                evidence=f"图表机检：{layout_warning.get('message')}",
                                suggestion="按 bg-figures 调整布局/拆图，或重渲染",
                            )
            if source_path is not None and not source_path.is_file():
                collector.add("script", "figure_missing", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence=f"缺少源文件：resources/{figure_type}/{anchor_id}{suffix}", suggestion="补 .mmd/.txt 源文件")
            if figure_type == "network":
                sources = package_dir / "resources" / "network" / "sources.json"
                if not sources.is_file():
                    collector.add("script", "source_mismatch", "major", chapter_id=chapter_id, file_path=file_path, anchor_id=anchor_id, evidence="网络图缺少 sources.json 来源记录", suggestion="补来源 URL/许可/检索词")
            figure_rows.append(figure_row)
    # 总字数：有未完成章节时为中间态，不做全包偏差判定（R-14）
    if expected_word > 0 and not pending_chapters:
        delta = abs(word_total - expected_word) / expected_word
        if delta > 0.20:
            collector.add("script", "total_word_low" if word_total < expected_word else "total_word_high", "blocker", evidence=f"全包实际 {word_total} 字 vs expectedWordCount {expected_word}（偏差 {delta:.1%}）>±20%", suggestion="调整章节字数后重算")
        elif delta > 0.10:
            collector.add("script", "total_word_low" if word_total < expected_word else "total_word_high", "major", evidence=f"全包实际 {word_total} 字 vs expectedWordCount {expected_word}（偏差 {delta:.1%}）>±10%", suggestion="调整章节字数")
    # 引用完整性 + 配额
    criteria = {str(item.get("criterionId")) for item in outline.get("criterionIndex") or [] if isinstance(item, Mapping)}
    for node in nodes:
        for ref in node.get("criterionRefs") or []:
            if isinstance(ref, Mapping) and str(ref.get("criterionId") or "") not in criteria:
                collector.add("script", "criterion_mismatch", "major", chapter_id=str(node.get("id") or ""), evidence=f"criterionId 不在 criterionIndex：{ref.get('criterionId')}", suggestion="修正 criterionRefs")
    stats = outline.get("stats") if isinstance(outline.get("stats"), Mapping) else {}
    planned = {key: int((stats.get("figurePlanned") or {}).get(key) or 0) for key in outline_validate.FIGURE_TYPES}
    quota = package_meta.get("figureQuota") if isinstance(package_meta.get("figureQuota"), Mapping) else {}
    quota_norm = {key: int(quota.get(key) or 0) for key in outline_validate.FIGURE_TYPES}
    hard_quota_short = any(
        planned[key] < quota_norm[key] for key in outline_validate.FIGURE_TYPES if key != "terminal"
    )
    terminal_disabled_violation = quota_norm["terminal"] == 0 and planned["terminal"] > 0
    if hard_quota_short or terminal_disabled_violation:
        collector.add(
            "script",
            "figure_mismatch",
            "blocker",
            evidence=f"图表配额不足/禁用类型被规划：stats.figurePlanned={planned} vs package-meta.figureQuota={quota_norm}（table/mermaid 为下限；terminal=0 时不得规划）",
            suggestion="按配额补齐 table/mermaid 的 figurePlan，或删除 terminal 规划/回写 package-meta（经用户确认）",
        )
    produced = {key: 0 for key in outline_validate.FIGURE_TYPES}
    for row in figure_rows:
        if row.get("status") == "produced" and row.get("type") in produced:
            produced[str(row["type"])] += 1
    chapter_rows.sort(key=lambda item: str(item.get("chapterId")))
    return {
        "nodes": nodes,
        "leaves": leaves,
        "chapterRows": chapter_rows,
        "pendingChapters": pending_chapters,
        "quantityIndex": build_quantity_index(chapter_texts),
        "totalWordChecked": bool(expected_word > 0 and not pending_chapters),
        "specialRows": special_rows,
        "tableRows": table_rows,
        "figureRows": figure_rows,
        "wordTotal": word_total,
        "expectedWord": expected_word,
        "quota": quota_norm,
        "planned": planned,
        "produced": produced,
        "coverage": build_coverage_matrix(outline, leaves),
    }


def build_coverage_matrix(outline: Mapping[str, Any], leaves: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    criteria = [item for item in outline.get("criterionIndex") or [] if isinstance(item, Mapping)]
    rows: list[dict[str, Any]] = []
    for criterion in criteria:
        criterion_id = str(criterion.get("criterionId") or "")
        cells: dict[str, str] = {}
        for leaf in leaves:
            chapter_id = str(leaf.get("id") or "")
            refs = {str(ref.get("criterionId")) for ref in leaf.get("criterionRefs") or [] if isinstance(ref, Mapping)}
            cells[chapter_id] = "✓" if criterion_id in refs else "-"
        rows.append({"criterionId": criterion_id, "factor": criterion.get("factor", ""), "score": criterion.get("score", ""), "cells": cells})
    return {"leaves": [str(leaf.get("id") or "") for leaf in leaves], "rows": rows}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def write_outputs(
    *,
    package_dir: Path,
    target: str,
    round_no: int,
    issues: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    layers_run: Sequence[str],
    pending_layers: Sequence[str],
    layer_status: Mapping[str, str],
    dry_run: bool,
    carry_forward: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    summary = review_issues.recalc_summary(issues)
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "packageId": target,
        "round": round_no,
        "generatedAt": paths.iso_now(),
        "layersRun": list(layers_run),
        "layerStatus": dict(layer_status),
        "pendingLayers": list(pending_layers),
        "summary": summary,
        "issues": list(issues),
    }
    if carry_forward:
        payload["carryForward"] = dict(carry_forward)
    paths_out = {
        "issues": ISSUES_FILE,
        "snapshot": f"reviews/rounds/round-{round_no}.issues.json",
        "report": REPORT_FILE,
    }
    if dry_run:
        return paths_out
    issues_path = package_dir / ISSUES_FILE
    snapshot_path = package_dir / f"reviews/rounds/round-{round_no}.issues.json"
    report_path = package_dir / REPORT_FILE
    issues_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(issues_path, payload)
    jsonio.write_json_atomic(snapshot_path, payload)
    report_path.write_text(
        render_report(target, round_no, summary, issues, result, layers_run, pending_layers, layer_status, carry_forward),
        encoding="utf-8",
        newline="\n",
    )
    return paths_out


def render_report(
    target: str,
    round_no: int,
    summary: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    layers_run: Sequence[str],
    pending_layers: Sequence[str],
    layer_status: Mapping[str, str],
    carry_forward: Mapping[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 审查报告（第 {round_no} 轮）")
    lines.append("")
    lines.append(f"- packageId：`{target}`")
    lines.append(f"- 生成时间：{paths.iso_now()}")
    lines.append(f"- A 层：**{layer_status.get('script', '?')}**；B 层：**{layer_status.get('coverage', 'skipped')}**；C 层：**{layer_status.get('graphic', 'skipped')}**")
    supplements = result.get("supplementTables") if isinstance(result.get("supplementTables"), list) else []
    lines.append(f"- 补充表登记（supplementTables）：{len(supplements)} 个" + (f"（{', '.join(str(item.get('caption') or item.get('anchorId')) for item in supplements[:5])}）" if supplements else ""))
    lines.append(f"- 问题统计：blocker **{summary.get('blocker', 0)}** / major **{summary.get('major', 0)}** / minor **{summary.get('minor', 0)}** / open **{summary.get('openCount', 0)}**")
    if carry_forward:
        if carry_forward.get("sameRound"):
            scope_label = f"**同轮复检**（`{carry_forward.get('source', '')}`）"
        else:
            scope_label = f"上轮第 **{carry_forward.get('previousRound', '?')}** 轮带入"
        lines.append(
            f"- carry-forward：{scope_label} **{carry_forward.get('carriedCount', 0)}** 项"
            f"（verified {carry_forward.get('verifiedCount', 0)} / 复开 {carry_forward.get('reopenedCount', 0)}"
            f" / 待语义复审 {carry_forward.get('pendingSemanticCount', 0)} / wontfix {carry_forward.get('wontfixKeptCount', 0)}"
            f" / 已 verified {carry_forward.get('keptVerifiedCount', 0)}），本轮新检出 {carry_forward.get('detectedCount', 0)} 项，合并去重 {carry_forward.get('suppressedCount', 0)} 项")
    lines.append("")
    if "script" in layer_status and layer_status["script"] == "blocked":
        lines.append("> A 层存在 blocker：按 F6.2 短路 B/C，先完成结构/字数/图表修复后再审查。")
    else:
        lines.append("> A 层未发现 blocker；B/C 层需由语义审查子智能体执行（本报告暂为待审占位）。")
    lines.append("")
    lines.append("## A 层问题")
    lines.append("")
    if issues:
        lines.append("| ID | 级别 | 类型 | 章节 | 证据 | 建议 |")
        lines.append("|---|---|---|---|---|---|")
        for issue in issues:
            lines.append(
                f"| {issue.get('issueId')} | {issue.get('severity')} | {issue.get('type')} | {issue.get('chapterId') or '-'} | {str(issue.get('evidence') or '').replace('|', '/')[:120]} | {str(issue.get('suggestion') or '').replace('|', '/')[:80]} |"
            )
    else:
        lines.append("（无）")
    lines.append("")
    carried_rows = [item for item in issues if isinstance(item, Mapping) and item.get("carriedFrom") is not None]
    if carried_rows and carry_forward:
        lines.append("## 上一轮问题复检（carry-forward）")
        lines.append("")
        if carry_forward.get("sameRound"):
            lines.append(f"- 数据来源：`{carry_forward.get('source', '')}`（第 {carry_forward.get('round', '?')} 轮重跑：同轮复检，不整体重写台账）")
        else:
            lines.append(f"- 数据来源：`{carry_forward.get('source', '')}`（上轮第 {carry_forward.get('previousRound', '?')} 轮）")
        lines.append("- 复检口径：A 层（script）由本脚本逐轮确定性复检并自动写 verified；B/C 语义项保持 fixed 待复审，由 `bg_review_issues.py close --status verified --review` 写 verified。")
        lines.append("- 表中「复检结果」为带入时点结论；后续复审结论见括号内「→ 状态」，当前状态以 `reviews/issues.json` 的 `status` 为准。")
        lines.append("")
        lines.append("| ID | 级别 | 层 | 类型 | 章节 | 原状态 | 复检结果 | 说明 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for issue in carried_rows:
            recheck = issue.get("recheck") if isinstance(issue.get("recheck"), Mapping) else {}
            result_label = str(recheck.get("result") or "-")
            if recheck.get("resolvedStatus"):
                result_label += f"（→ {recheck.get('resolvedStatus')}）"
            lines.append(
                f"| {issue.get('issueId')} | {issue.get('severity')} | {issue.get('layer')} | {issue.get('type')} | {issue.get('chapterId') or '-'} |"
                f" {recheck.get('previousStatus', '-')} | {result_label} | {str(recheck.get('note') or '').replace('|', '/')[:100]} |"
            )
        lines.append("")
    chapter_rows = result.get("chapterRows") or []
    lines.append("## 字数核对")
    lines.append("")
    lines.append("| 章节 | 实际 | 目标 | 比例 | 判定 |")
    lines.append("|---|---|---|---|---|")
    for row in chapter_rows:
        ratio = f"{row['ratio']:.1%}" if row.get("ratio") is not None else "-"
        judgement = "特殊章节（免字数检查）" if row.get("judgement") == "special" else row.get("judgement")
        lines.append(f"| {row.get('chapterId')} | {row.get('wordCount')} | {row.get('wordTarget')} | {ratio} | {judgement} |")
    lines.append(f"\n全包实际 {result.get('wordTotal')} 字 / expectedWordCount {result.get('expectedWord')}。")
    pending_rows = result.get("pendingChapters") or []
    if pending_rows:
        ids = "、".join(str(item.get("chapterId")) for item in pending_rows[:12])
        suffix = "…" if len(pending_rows) > 12 else ""
        lines.append("")
        lines.append(
            f"> 未完成章节 **{len(pending_rows)}** 个（{ids}{suffix}）：本次只审已完成章节，"
            "这些章节不计问题、全包字数不做偏差判定；写完后重跑审查。"
        )
    special_rows = result.get("specialRows") or []
    if special_rows:
        lines.append("")
        lines.append(f"特殊章节（免字数检查）：{len(special_rows)} 个")
        lines.append("")
        lines.append("| 章节 | 材料类型 | 要求数 | 请用户处理 |")
        lines.append("|---|---|---|---|")
        for row in special_rows:
            lines.append(
                f"| {row.get('chapterId')} {str(row.get('title') or '').replace('|', '/')} | {row.get('materialType') or '-'} |"
                f" {row.get('requirementCount', 0)} | {str(row.get('userAction') or '-').replace('|', '/')[:80]} |"
            )
    lines.append("")
    lines.append("## 图表核对")
    lines.append("")
    lines.append("| 章节 | anchorId | 类型 | 标记 | 状态 | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for row in result.get("figureRows") or []:
        lines.append(f"| {row.get('chapterId')} | {row.get('anchorId')} | {row.get('type')} | {'✓' if row.get('marker') else '-'} | {row.get('status')} | {row.get('detail') or ''} |")
    lines.append(f"\n计划：{result.get('planned')}；产出：{result.get('produced')}；配额：{result.get('quota')}。")
    quantity_rows = result.get("quantityIndex") or []
    if quantity_rows:
        lines.append("")
        lines.append("## 跨章数量索引（B 层核对用，不判问题）")
        lines.append("")
        lines.append("同一指标在 ≥2 章出现且数值不同时列出（可能包含分项/合计的正常差异，需 B 层结合上下文判断）。")
        lines.append("")
        lines.append("| 指标 | 出现章节 | 各章数值 |")
        lines.append("|---|---|---|")
        for row in quantity_rows:
            chapters = "；".join(
                f"{chapter_id}: {'/'.join(values)}" for chapter_id, values in (row.get("chapters") or {}).items()
            )
            lines.append(f"| {str(row.get('label') or '').replace('|', '/')} | {row.get('chapterCount')} | {chapters.replace('|', '/')} |")
    lines.append("")
    lines.append("## 覆盖率矩阵（criterionId × chapterId，A 层计划引用）")
    lines.append("")
    coverage = result.get("coverage") or {"leaves": [], "rows": []}
    leaf_ids = coverage.get("leaves") or []
    lines.append("| criterionId | " + " | ".join(leaf_ids) + " |")
    lines.append("|---|" + "|".join("---" for _ in leaf_ids) + "|")
    for row in coverage.get("rows") or []:
        cells = [str((row.get("cells") or {}).get(leaf_id, "-")) for leaf_id in leaf_ids]
        lines.append(f"| {row.get('criterionId')} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## B/C 层（语义审查）")
    lines.append("")
    if "coverage" in pending_layers:
        lines.append("- B 层评分项响应覆盖度：**待语义审查**（按章 summary + criterionRefs/contentRequirements 判定 pass/weak/missing，输出 coverage_gap/weak_response/missing_criterion）。")
    if "graphic" in pending_layers:
        lines.append("- C 层图表契合度：**待语义审查**（脚本已校验存在性/尺寸/可解析性；图文一致性、表头支撑、终端命令场景需语义判断）。")
    if not pending_layers:
        lines.append("- B/C 层本轮被短路（A 层 blocker 优先修复）。")
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append("- 本报告由 `bg_review_script.py` 生成；A 层为确定性脚本检查，B/C 层语义问题由 `bg_review_issues.py merge` 合并。")
    lines.append("- 文风/自引用/跨章一致性属语义审查范围，本脚本不做硬门禁。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """纯函数自检：字数口径（P2-10）/表格/正文/标题/去重/ID/carry-forward（不读写项目）。"""
    checks: list[str] = []
    # P2-10：审查字数口径必须来自 _lib.text，不得有本地副本
    if count_words is not text_lib.count_body_words:
        raise AssertionError("count_words 必须直接引用 _lib.text.count_body_words")
    sample = "# 一级标题\n<!-- BG:CHAPTER id=1 -->\n正文 2026-11-21 PLC_01 运维服务。\n"
    expected_words = 6 + 2  # 正文(2)+运维服务(4) 个汉字 + “2026-11-21”“PLC_01” 2 个词
    if count_words(sample) != expected_words or count_words(sample) != text_lib.count_words(sample):
        raise AssertionError(f"字数口径不一致：{count_words(sample)} != {expected_words}")
    checks.append("word_count_lib_text")
    # 表格解析
    table_text = "▼表运维范围\n\n| 项目 | 内容 |\n|---|---|\n| 范围 | 全域 |\n"
    tables = markdown_tables(table_text)
    if len(tables) != 1 or tables[0]["caption"] != "▼表运维范围" or len(tables[0]["rows"]) != 1:
        raise AssertionError(f"表格解析不正确：{tables}")
    checks.append("markdown_tables")
    # P0-3：业务数字开头（1包/2 包/2026年）不是编号；真正编号才拦
    for caption in ("▼表1包采购设施规格与预估量对照表", "▼表2 包采购量清单", "▼表2026年设施清单", "▼表1.5mm标线参数"):
        if CAPTION_NUMBERING_RE.match(caption):
            raise AssertionError(f"业务数字题注被误判为编号：{caption}")
    for caption in ("▼表1、设施清单", "▼表第1章设施", "▼表（1）设施", "▼表一、设施", "▼表表1设施"):
        if not CAPTION_NUMBERING_RE.match(caption):
            raise AssertionError(f"真正的题注编号未拦截：{caption}")
    checks.append("caption_numbering")
    # R-13：题注「数字+量词」开头虽不是编号，但会和 ▼表 连读成「表3」，审查记 minor
    for caption in ("▼表3 条路段设施供货清单与进场计划表", "▲图5 类设施布置示意"):
        if not caption_like_number(caption):
            raise AssertionError(f"数字+量词题注未识别：{caption}")
    for caption in ("▼表1包采购设施规格与预估量对照表", "▼表2026年设施清单", "▼表1.5mm标线参数"):
        if caption_like_number(caption):
            raise AssertionError(f"业务数字题注被误判为数字+量词：{caption}")
    if text_lib.rewrite_caption_number_prefix("3 条路段设施供货清单") != "各条路段设施供货清单":
        raise AssertionError("数字+量词题注改写不正确")
    checks.append("caption_number_like")
    # D7：跨章数量索引（只列不同值，不判问题）
    quantity_rows = build_quantity_index({"1-1": "隔离柱 103 根，护栏 1863 套", "2-1": "隔离柱 245 根，护栏 1863 套"})
    labels = {str(row.get("label")) for row in quantity_rows}
    if not any(label.startswith("隔离柱") for label in labels):
        raise AssertionError(f"跨章数量索引未列出不同值：{quantity_rows}")
    if any(label.startswith("护栏") for label in labels):
        raise AssertionError(f"跨章数量索引误列同值指标：{quantity_rows}")
    checks.append("quantity_index")
    # P2-4：技术正文商务/报价混入机检
    business_hits = [bool(re.search(pattern, "价格评审按低价优先法，评标基准价…最高限价 130.86 万元，货款支付分两段 97%")) for pattern, _, _ in BUSINESS_PATTERNS]
    if not all(business_hits):
        raise AssertionError(f"商务/报价混入未全部命中：{business_hits}")
    if any(re.search(pattern, "作业人员按统一制服上岗，巡查频次每季度一次。") for pattern, _, _ in BUSINESS_PATTERNS):
        raise AssertionError("普通技术正文被误判商务/报价内容")
    checks.append("business_content_patterns")
    # 正文剥离（首行标题 / BG:CHAPTER / HTML 注释）
    body = body_text("## 标题\n<!-- BG:CHAPTER id=1-1 -->\n\n正文 <!-- BG:FIGURE:fig-1 -->内容\n")
    if "标题" in body or "BG:CHAPTER" in body or "BG:FIGURE" in body or "正文" not in body:
        raise AssertionError(f"body_text 剥离不正确：{body!r}")
    headings = heading_info("# 标题\n```\n# 代码内不算\n```\n## 二级\n")
    if [item["title"] for item in headings] != ["标题", "二级"]:
        raise AssertionError(f"heading_info 不正确：{headings}")
    checks.append("body_and_headings")
    # 去重 + ID 分配
    collector = IssueCollector()
    collector.add("script", "word_count_low", "major", chapter_id="1-1", evidence="证据A")
    collector.add("script", "word_count_low", "major", chapter_id="1-1", evidence="证据A")
    collector.add("script", "table_missing", "minor", chapter_id="1-2", evidence="证据B")
    collector.add("script", "word_count_low", "blocker", chapter_id="1-1", evidence="证据C")
    raw = collector.raw_items()
    if len(raw) != 3 or raw[0]["severity"] != "blocker" or raw[0]["issueId"] != "":
        raise AssertionError(f"raw_items 去重/排序不正确：{raw}")
    assigned = review_issues.assign_issue_ids(raw, 3)
    if [item["issueId"] for item in assigned] != ["RV-3-0001", "RV-3-0002", "RV-3-0003"]:
        raise AssertionError(f"issueId 分配不正确：{assigned}")
    checks.append("collector")
    # carry-forward：script fixed 未复现 → verified；复现 → 复开；语义层 → 待复审
    previous = [
        {
            "issueId": "RV-1-0001",
            "layer": "script",
            "type": "word_count_low",
            "severity": "major",
            "chapterId": "1-1",
            "anchorId": None,
            "evidence": "实际 800 字 / 目标 1000（80.0%）",
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
    carried = review_issues.carry_forward_issues(previous, detected, 2, previous_round=1)
    by_id = {item["issueId"]: item for item in carried["carried"]}
    if by_id["RV-1-0001"]["status"] != "verified" or by_id["RV-1-0001"]["recheck"]["result"] != review_issues.RECHECK_VERIFIED:
        raise AssertionError(f"未复现的 fixed 应自动 verified：{by_id['RV-1-0001']}")
    if by_id["RV-1-0002"]["status"] != "open" or by_id["RV-1-0002"]["recheck"]["result"] != review_issues.RECHECK_RECURRED:
        raise AssertionError(f"复现的 fixed 应复开：{by_id['RV-1-0002']}")
    if by_id["RV-1-0003"]["recheck"]["result"] != review_issues.RECHECK_PENDING_SEMANTIC:
        raise AssertionError(f"语义层应待复审：{by_id['RV-1-0003']}")
    if carried["detected"] or carried["summary"]["suppressedCount"] != 1:
        raise AssertionError(f"复现问题应去重：{carried['summary']}")
    sorted_ids = [item["issueId"] for item in review_issues.sort_issues(carried["carried"])]
    if sorted_ids[0] != "RV-1-0001" or sorted_ids[-1] != "RV-1-0003":
        raise AssertionError(f"跨轮排序不正确：{sorted_ids}")
    checks.append("carry_forward")
    # P0-1：chapters/_batches 等元数据目录不得参与章节树对账
    with tempdir.temporary_directory(prefix="bidgenie-review-tree-") as tmp:
        package_dir = Path(tmp)
        chapters_dir = package_dir / "chapters"
        (chapters_dir / "1-项目理解").mkdir(parents=True)
        (chapters_dir / "1-项目理解" / "1-1-现状.md").write_text("# 现状\n", encoding="utf-8")
        for meta_dir in ("_outline", "_results", "_frozen", "_batches"):
            (chapters_dir / meta_dir).mkdir()
            (chapters_dir / meta_dir / f"{meta_dir.strip('_')}.json").write_text("{}", encoding="utf-8")
        actual_dirs, actual_files = collect_chapter_tree(package_dir, chapters_dir)
        if any("_batches" in item or "_outline" in item or "_results" in item for item in actual_dirs | actual_files):
            raise AssertionError(f"元数据目录未排除：dirs={actual_dirs} files={actual_files}")
        if "chapters/1-项目理解" not in actual_dirs or "chapters/1-项目理解/1-1-现状.md" not in actual_files:
            raise AssertionError(f"正常章节树被误排除：dirs={actual_dirs} files={actual_files}")
    checks.append("chapter_tree_ignores_metadata")
    # P0-2：mermaid fallback 必须是 blocker；terminal 的 pillow 引擎不得误报
    if not mermaid_fallback_reason({"status": "fallback", "engine": "pillow-fallback", "fallbackReason": "node 不可用"}):
        raise AssertionError("mermaid fallback 未识别")
    if mermaid_fallback_reason({"status": "produced", "engine": "msedge.exe"}):
        raise AssertionError("正常 mermaid 被误判 fallback")
    if mermaid_fallback_reason({"status": "produced", "engine": "pillow"}):
        raise AssertionError("terminal 的 pillow 引擎被误判 fallback")
    checks.append("render_fallback_detection")
    return {"ok": True, "selftest": {"script": "bg_review_script.py", "checks": checks}}


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_review_script.py",
        description="BidGenie F6 脚本审查层：结构/完整性/字数/图表/题注/树/配额 → issues.json + review-report.md",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--round", type=int, help="审查轮次（缺省 state.reviewLoop.round）")
    parser.add_argument("--layer", choices=("a", "all"), default="all", help="a=仅 A 层；all=按规则执行 A 并标记 B/C（M7 阶段 B/C 占位）")
    parser.add_argument("--no-carry-forward", action="store_true", help="不读上一轮台账（纯本轮重生成；默认开启 carry-forward）")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写 issues/report（真实项目试跑用）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检（纯函数，不读写项目）")
    return parser


def apply_carry_forward(
    package_dir: Path,
    round_no: int,
    detected: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """P1-3：把上一轮未 verified 条目带入本轮并与本轮新检出合并。

    返回 ``(issues, carry_forward_info)``；无历史或无可带入条目时退化为纯本轮结果
    （``carry_forward_info is None``，不写 carryForward 块，保持第 1 轮输出兼容）。
    """
    previous = review_issues.load_previous_ledger(package_dir, round_no)
    if previous is None:
        return review_issues.assign_issue_ids(detected, round_no), None
    carried = review_issues.carry_forward_issues(
        previous["issues"],
        detected,
        round_no,
        previous_round=previous["round"],
        same_round=bool(previous.get("sameRound")),
    )
    new_issues = review_issues.assign_issue_ids(carried["detected"], round_no, reserved=carried["carried"])
    issues = review_issues.sort_issues(list(carried["carried"]) + new_issues)
    summary = carried["summary"]
    if not summary["carriedCount"] and not summary["suppressedCount"]:
        return issues, None
    info: dict[str, Any] = dict(summary)
    info["source"] = previous["source"]
    info["at"] = paths.iso_now()
    info["verifiedIds"] = list(carried["stats"]["verified"])
    info["reopenedIds"] = list(carried["stats"]["reopened"])
    info["pendingSemanticIds"] = list(carried["stats"]["pendingSemantic"])
    info["carriedOpenIds"] = list(carried["stats"]["carriedOpen"])
    info["wontfixKeptIds"] = list(carried["stats"]["wontfixKept"])
    info["keptVerifiedIds"] = list(carried["stats"]["keptVerified"])
    info["suppressed"] = list(carried["suppressed"])
    return issues, info


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    target, package_dir, outline, package_meta, state, project_doc = load_inputs(workspace, project_dir, args.package)
    stage = str(state.get("currentStage") or "")
    if stage not in ("review", "optimize") and not bool(args.dry_run):
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"审查脚本只能在 review/optimize 阶段运行，当前 {stage}",
            hint="越阶检查仅允许 --dry-run（不写 reviews/events）",
            detail={"currentStage": stage, "dryRun": False},
        )
    round_no = args.round if args.round is not None else int((state.get("reviewLoop") or {}).get("round") or 1)
    if not isinstance(round_no, int) or round_no < 1:
        raise errors.BgError(errors.E_USAGE, "--round 必须是正整数", detail={"round": args.round})
    collector = IssueCollector()
    if not bool(args.dry_run):
        event_log.log_event(
            workspace,
            "review.round_start",
            actor="script",
            project=str(project_doc.get("projectId") or ""),
            package=target,
            detail={"round": round_no, "packageId": target, "layer": args.layer},
        )
    # JSON/结构合法性（outline schema）
    validation = outline_validate.validate_document(outline, package_meta, require_layout=True)
    for blocker in validation.get("blockers") or []:
        collector.add("script", "script_error", "blocker", evidence=f"outline 校验失败：{blocker.get('code')} {blocker.get('message')}", suggestion="修复 outline 后重跑")
    result = review_a_layer(package_dir=package_dir, outline=outline, package_meta=package_meta, collector=collector)
    result["supplementTables"] = [item for item in outline.get("supplementTables") or [] if isinstance(item, Mapping)]
    detected = collector.raw_items()
    if bool(getattr(args, "no_carry_forward", False)):
        issues = review_issues.assign_issue_ids(detected, round_no)
        carry_forward_info: dict[str, Any] | None = None
    else:
        issues, carry_forward_info = apply_carry_forward(package_dir, round_no, detected)
    # 短路判定只看“仍未关闭”的 blocker：带入的 blocker 若已 verified 不再短路 B/C
    blocker_count = sum(
        1
        for issue in issues
        if issue["severity"] == "blocker" and str(issue.get("status") or "open") in ("open", "fixing")
    )
    if blocker_count:
        layers_run = ["script"]
        pending_layers: list[str] = []
        layer_status = {"script": "blocked", "coverage": "skipped", "graphic": "skipped"}
    else:
        layers_run = ["script"]
        pending_layers = ["coverage", "graphic"] if args.layer == "all" else []
        layer_status = {"script": "pass", "coverage": "pending" if args.layer == "all" else "skipped", "graphic": "pending" if args.layer == "all" else "skipped"}
    paths_out = write_outputs(
        package_dir=package_dir,
        target=target,
        round_no=round_no,
        issues=issues,
        result=result,
        layers_run=layers_run,
        pending_layers=pending_layers,
        layer_status=layer_status,
        dry_run=bool(args.dry_run),
        carry_forward=carry_forward_info,
    )
    summary = review_issues.recalc_summary(issues)
    if not bool(args.dry_run):
        detail: dict[str, Any] = {"round": round_no, "packageId": target, "summary": summary, "layerStatus": layer_status}
        if carry_forward_info:
            detail["carryForward"] = carry_forward_info
        event_log.log_event(
            workspace,
            "review.round_end",
            actor="script",
            project=str(project_doc.get("projectId") or ""),
            package=target,
            detail=detail,
        )
    return {
        "ok": True,
        "command": "review_script",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "round": round_no,
        "currentStage": state.get("currentStage"),
        "dryRun": bool(args.dry_run),
        "layersRun": layers_run,
        "pendingLayers": pending_layers,
        "layerStatus": layer_status,
        "summary": summary,
        "blockerCount": blocker_count,
        "carryForward": carry_forward_info,
        "pendingChapters": result.get("pendingChapters") or [],
        "totalWordChecked": bool(result.get("totalWordChecked")),
        "issueIds": [issue["issueId"] for issue in issues],
        "issues": issues,
        "paths": paths_out,
        "stats": {
            "leafCount": len(result["leaves"]),
            "pendingChapterCount": len(result.get("pendingChapters") or []),
            "totalWordChecked": bool(result.get("totalWordChecked")),
            "wordTotal": result["wordTotal"],
            "expectedWordCount": result["expectedWord"],
            "figurePlanned": result["planned"],
            "figureProduced": result["produced"],
            "figureQuota": result["quota"],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_review_script.py",
    )


if __name__ == "__main__":
    sys.exit(main())
