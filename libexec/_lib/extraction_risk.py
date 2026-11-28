"""F3 转换回退/勾选不确定风险识别（P1-2 / P1-3，2026-11-25 冒烟）。

背景：大 PDF 的「关键数据仅来自回退文本」、跨页表格、复选框符号丢失，过去只写在
warnings/「需用户确认」里，状态机仍会推进。这里把风险汇总成结构化清单，供
`bg_extract_validate.py` 做阻塞式用户确认（`--confirm-fallback`）与 T2 guard 复核。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from . import jsonio, paths

MAX_RISKS = 12
CRITICAL_FIELDS: tuple[str, ...] = (
    "项目编号",
    "项目名称",
    "预算",
    "最高限价",
    "限价",
    "分包",
    "包数",
    "截止时间",
    "开标时间",
    "评分",
    "分值",
    "满分",
    "技术分",
    "工期",
    "服务期",
    "合同履行期限",
)
REVIEW_MARKERS: tuple[str, ...] = (
    "回退文本",
    "回退页",
    "需核对 PDF 原件",
    "需核对原件",
    "需核对",
    "需确认",
    "转换不可辨",
    "无法辨识",
    "不可辨",
)
AMBIGUOUS_TOKENS: tuple[str, ...] = ("复选框", "勾选", "方框", "符号丢失", "符号不可辨")
TABLE_TOKENS: tuple[str, ...] = ("跨页", "合并单元格", "断行", "行列错位", "表格可能丢失")
MANIFEST_REL = "sources/conversion-manifest.json"
#: BUG-07（VM 冒烟）：风险来源区分「转换清单新发现」与「提取稿按 §5.1 已标注」，
#: 便于 Agent 给用户一份简明摘要，而不是把两大段原文直接甩给用户。
ORIGIN_MANIFEST = "conversion_manifest"
ORIGIN_ANNOTATION = "extraction_annotation"
RISK_LABELS: dict[str, str] = {
    "fallback_page": "回退页关键字段",
    "ambiguous_symbol": "复选框/符号不可辨",
    "table_structure": "跨页/合并单元格表格",
}
RISK_CHECKPOINTS: dict[str, str] = {
    "fallback_page": "核对回退页的关键字段（项目编号/预算/分包/截止时间/工期等）是否与 PDF 原件一致",
    "ambiguous_symbol": "核对复选框/勾选符号对应的选项是否识别正确（是否响应/是否满足）",
    "table_structure": "核对跨页或合并单元格表格的行列、数量/金额是否完整",
}


def _excerpt(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _manifest_entries(project_dir: Path) -> list[Mapping[str, Any]]:
    path = project_dir / MANIFEST_REL
    if not path.is_file():
        return []
    try:
        manifest = jsonio.read_json(path)
    except Exception:  # noqa: BLE001 - 风险识别不应让校验崩溃
        return []
    if not isinstance(manifest, Mapping):
        return []
    entries: list[Mapping[str, Any]] = []
    for key in ("files", "sources"):
        value = manifest.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, Mapping))
    return entries


def _manifest_fallback_risks(project_dir: Path) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for entry in _manifest_entries(project_dir):
        pages = entry.get("fallbackPages")
        page_list = [int(item) for item in pages if isinstance(item, int)] if isinstance(pages, list) else []
        key_data = bool(entry.get("keyDataFromFallbackOnly"))
        note = str(entry.get("fallbackNote") or "")
        if not key_data and not page_list:
            continue
        if not key_data and "关键数据" not in note:
            continue
        risks.append(
            {
                "code": "fallback_page",
                "origin": ORIGIN_MANIFEST,
                "source": MANIFEST_REL,
                "pages": page_list,
                "fields": [],
                "excerpt": _excerpt(note or f"第 {page_list} 页关键数据来自回退文本，表格/版式结构可能丢失"),
            }
        )
    return risks


def _extraction_line_risks(project_dir: Path) -> list[dict[str, Any]]:
    extraction_dir = project_dir / "extraction"
    if not extraction_dir.is_dir():
        return []
    risks: list[dict[str, Any]] = []
    for path in sorted(extraction_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        source = paths.rel_posix(project_dir, path)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not any(marker in stripped for marker in REVIEW_MARKERS):
                continue
            fields = [field for field in CRITICAL_FIELDS if field in stripped]
            if any(token in stripped for token in AMBIGUOUS_TOKENS):
                code = "ambiguous_symbol"
            elif any(token in stripped for token in TABLE_TOKENS):
                code = "table_structure"
            elif fields:
                code = "fallback_page"
            else:
                continue
            risks.append(
                {
                    "code": code,
                    "origin": ORIGIN_ANNOTATION,
                    "source": source,
                    "pages": [],
                    "fields": fields,
                    "excerpt": _excerpt(stripped),
                }
            )
            if len(risks) >= MAX_RISKS:
                return risks
    return risks


def detect_fallback_review_risks(project_dir: str | Path) -> list[dict[str, Any]]:
    """汇总需要用户核对的风险：回退页关键字段、跨页表格、复选框/符号不可辨。

    同一风险类型（code）在多个提取文件/清单里重复出现时合并为一条：保留首条
    source/excerpt，同时累积 sources/fields/pages/excerpts/occurrences。这样用户
    看到的是“3 类风险”而不是同一条提示重复 9 次，也不会丢核对范围。
    """
    root = Path(project_dir)
    grouped: dict[str, dict[str, Any]] = {}
    for risk in [*_manifest_fallback_risks(root), *_extraction_line_risks(root)]:
        code = str(risk.get("code") or "")
        if not code:
            continue
        entry = grouped.get(code)
        if entry is None:
            grouped[code] = {
                **risk,
                "origins": [str(risk.get("origin") or ORIGIN_ANNOTATION)],
                "sources": [str(risk.get("source") or "")],
                "fields": list(risk.get("fields") or []),
                "pages": list(risk.get("pages") or []),
                "excerpts": [str(risk.get("excerpt") or "")],
                "occurrences": 1,
            }
            continue
        entry["occurrences"] = int(entry.get("occurrences") or 1) + 1
        origin = str(risk.get("origin") or ORIGIN_ANNOTATION)
        if origin not in entry.setdefault("origins", []):
            entry["origins"].append(origin)
        for key in ("sources", "fields", "pages", "excerpts"):
            if key == "sources":
                values = [str(risk.get("source") or "")]
            elif key in ("fields", "pages"):
                values = list(risk.get(key) or [])
            else:
                values = [str(risk.get("excerpt") or "")]
            for item in values:
                if item not in entry[key]:
                    entry[key].append(item)
    risks: list[dict[str, Any]] = []
    for entry in grouped.values():
        entry["pages"] = sorted({int(item) for item in entry.get("pages") or [] if isinstance(item, int)})
        entry["sources"] = [item for item in entry.get("sources") or [] if item]
        entry["excerpts"] = [item for item in entry.get("excerpts") or [] if item][:10]
        # N-06：fields 必须与展示给用户的 excerpts 对得上；旧实现取所有命中行的并集，
        # excerpts 只保留前 5 条时会出现「fields 有、摘录里看不到」的误读。
        visible = " ".join(entry["excerpts"])
        entry["fields"] = sorted({field for field in CRITICAL_FIELDS if field in visible})
        origins = [str(item) for item in entry.get("origins") or []]
        entry["origin"] = origins[0] if origins else ORIGIN_ANNOTATION
        entry["kind"] = (
            "mixed"
            if ORIGIN_MANIFEST in origins and ORIGIN_ANNOTATION in origins
            else ("new" if ORIGIN_MANIFEST in origins else "annotated")
        )
        entry["alreadyAnnotated"] = entry["kind"] == "annotated"
        risks.append(entry)
        if len(risks) >= MAX_RISKS:
            break
    return risks


def risk_summary(risks: list[dict[str, Any]]) -> dict[str, Any]:
    """BUG-07：把合并后的风险压成用户可读的简明摘要（新风险 / 已标注 / 核对点）。"""
    items: list[dict[str, Any]] = []
    new_count = 0
    annotated_count = 0
    for risk in risks:
        code = str(risk.get("code") or "")
        kind = str(risk.get("kind") or ("annotated" if risk.get("alreadyAnnotated") else "new"))
        if ORIGIN_MANIFEST in (risk.get("origins") or []) or kind == "new":
            new_count += 1
        if ORIGIN_ANNOTATION in (risk.get("origins") or []) or kind == "annotated":
            annotated_count += 1
        items.append(
            {
                "code": code,
                "label": RISK_LABELS.get(code, code),
                "kind": kind,
                "occurrences": int(risk.get("occurrences") or 1),
                "pages": list(risk.get("pages") or [])[:10],
                "fields": list(risk.get("fields") or [])[:10],
                "sourcesCount": len(risk.get("sources") or []),
                "checkpoint": RISK_CHECKPOINTS.get(code, "核对相关原文与 PDF 原件是否一致"),
            }
        )
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        pages = f"第 {'、'.join(str(page) for page in item['pages'])} 页" if item["pages"] else ""
        fields = f"字段：{'、'.join(item['fields'])}" if item["fields"] else ""
        scope = "；".join(part for part in (pages, fields) if part)
        suffix = f"（{scope}）" if scope else ""
        tagged = "（提取稿已按 §5.1 标注）" if item["kind"] == "annotated" else ""
        lines.append(f"{index}. {item['label']}{tagged}：{item['occurrences']} 处{suffix}；{item['checkpoint']}")
    prompt = (
        f"检测到 {len(risks)} 类转换风险（转换清单新发现 {new_count} 类 / 提取稿已标注 {annotated_count} 类）：\n"
        + "\n".join(lines)
        + "\n已标注的风险只需核对，不代表新增缺陷；请用户确认后运行 --confirm-fallback --note \"<核对结论>\"。"
    )
    return {
        "riskCount": len(risks),
        "newRiskCount": new_count,
        "annotatedRiskCount": annotated_count,
        "items": items,
        "userPrompt": prompt,
    }


def review_from_project(project: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = project.get("extensions") if isinstance(project.get("extensions"), Mapping) else {}
    review = extensions.get("extractionFallbackReview") if isinstance(extensions, Mapping) else None
    return review if isinstance(review, Mapping) else {}


def write_review(
    project_dir: str | Path,
    risks: list[dict[str, Any]],
    *,
    confirmed_at: str = "",
    note: str = "",
) -> str:
    """把风险与确认状态写入 project.extensions.extractionFallbackReview；返回项目文件相对路径。"""
    root = Path(project_dir)
    project_file = root / "project.json"
    if not project_file.is_file():
        return ""
    project = jsonio.read_json(project_file)
    if not isinstance(project, dict):
        return ""
    extensions = dict(project.get("extensions") or {})
    previous = extensions.get("extractionFallbackReview") if isinstance(extensions.get("extractionFallbackReview"), Mapping) else {}
    review = {
        "required": bool(risks),
        "risks": risks,
        "confirmedAt": confirmed_at or str(previous.get("confirmedAt") or ""),
        "confirmedBy": "user" if confirmed_at else str(previous.get("confirmedBy") or ""),
        "note": note or str(previous.get("note") or ""),
        "checkedAt": paths.iso_now(),
    }
    if previous == review:
        return paths.rel_posix(root, project_file) + ".extensions.extractionFallbackReview"
    extensions["extractionFallbackReview"] = review
    project["extensions"] = extensions
    project["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(project_file, project)
    return paths.rel_posix(root, project_file) + ".extensions.extractionFallbackReview"
