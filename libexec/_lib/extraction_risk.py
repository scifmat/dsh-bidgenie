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

    返回去重后的风险列表；空列表表示无需确认。
    """
    root = Path(project_dir)
    risks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for risk in [*_manifest_fallback_risks(root), *_extraction_line_risks(root)]:
        key = (str(risk.get("code") or ""), str(risk.get("source") or ""), str(risk.get("excerpt") or ""))
        if key in seen:
            continue
        seen.add(key)
        risks.append(risk)
        if len(risks) >= MAX_RISKS:
            break
    return risks


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
