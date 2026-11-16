#!/usr/bin/env python3
"""BidGenie F7.5 导出后自检（bg_export_verify.py）。

CLI：
    bg_export_verify.py --docx <path> --mode tech|keyinfo|overview [--report] [--strict]
                        [--workspace <ws>] [--project <prj>] [--package <pkg>] [--json]

检查：docx 可打开；outline 标题全部以 Heading 1–6 输出且编号样式挂接 numbering.xml；
图片数=produced 图；表格数=▼表题注±1；正文总字数 vs outline.stats ±20%；抽查 3 章首段非空；
无 TOC/目录；颜色 auto、标题/正文字体；缺失图/占位 major（strict 下失败）。
通过写 export/_last-export-report.json（含自检结果）；失败 exit4。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
REPORT_REL = "export/_last-export-report.json"
HEADING_RE = re.compile(r"^Heading ([1-6])$")
CAPTION_PREFIXES = ("▼表", "▲图", "表", "图")
ALLOWED_FONTS = {"宋体", "黑体", "Arial", "Times New Roman", "Consolas"}


def _document_text(document: Any) -> str:
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", text))
    return cjk + words


def _outline_titles(outline: Mapping[str, Any]) -> list[str]:
    titles: list[str] = []
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        title = str(node.get("title") or "").strip()
        if title:
            titles.append(title)
    return titles


def _leaf_nodes(outline: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    leaves: list[Mapping[str, Any]] = []
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        if isinstance(node.get("children"), list) and node.get("children"):
            continue
        leaves.append(node)
    return leaves


def _produced_figure_count(package_dir: Path, outline: Mapping[str, Any]) -> int:
    planned: list[dict[str, Any]] = []
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        for plan in node.get("figurePlan") or []:
            if not isinstance(plan, Mapping):
                continue
            figure_type = str(plan.get("type") or "")
            if figure_type and figure_type != "table":
                planned.append(dict(plan))
    if planned:
        # figurePlan 是图表计划的唯一来源；非 table 图应逐张插入，资源缺失由导出报告/实际图片数体现。
        return len(planned)
    manifest_path = package_dir / "resources" / "render-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = jsonio.read_json(manifest_path)
        except errors.BgError:
            manifest = {}
        anchors = manifest.get("anchors") if isinstance(manifest, Mapping) else None
        if isinstance(anchors, Mapping):
            return sum(1 for item in anchors.values() if isinstance(item, Mapping) and item.get("status") == "produced")
    count = 0
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        for plan in node.get("figurePlan") or []:
            if not isinstance(plan, Mapping):
                continue
            figure_type = str(plan.get("type") or "")
            if figure_type in ("mermaid", "terminal", "network"):
                png = package_dir / "resources" / figure_type / f"{plan.get('anchorId')}.png"
                if png.is_file():
                    count += 1
    return count


def _heading_numbering_ok(document: Any, expected_count: int) -> tuple[bool, str]:
    headings = [paragraph for paragraph in document.paragraphs if HEADING_RE.match(paragraph.style.name or "")]
    if len(headings) < expected_count:
        return False, f"Heading 段落 {len(headings)} < 期望 {expected_count}"
    numbering_ok = False
    try:
        numbering = document.part.numbering_part.element
        abstracts = numbering.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}abstractNum")
        for abstract in abstracts:
            levels = abstract.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}lvl")
            if len(levels) >= 3:
                numbering_ok = True
                break
    except Exception:  # noqa: BLE001
        numbering_ok = False
    if not numbering_ok:
        return False, "numbering.xml 缺少多级 abstractNum"
    return True, f"headingCount={len(headings)}"


def _docx_appearance(docx_path: Path) -> tuple[set[str], set[str]]:
    try:
        with zipfile.ZipFile(docx_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        return set(re.findall(r'<w:color[^>]*w:val="([^"]+)"', xml)), set(re.findall(r'w:(?:ascii|eastAsia)="([^"]+)"', xml))
    except Exception:  # noqa: BLE001
        return set(), set()


def verify_docx(
    *,
    docx_path: Path,
    mode: str,
    package_dir: Path,
    outline: Mapping[str, Any],
    package_meta: Mapping[str, Any],
    report_data: Mapping[str, Any] | None = None,
    strict: bool = False,
    docx_root: Path | None = None,
    expected_keyinfo_headings: int | None = None,
) -> dict[str, Any]:
    from docx import Document
    from docx.oxml.ns import qn

    findings: list[dict[str, Any]] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    try:
        document = Document(str(docx_path))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "findings": [{"code": "docx_open_failed", "message": str(exc)}], "checks": {}, "warnings": []}
    text = _document_text(document)
    if mode in ("keyinfo", "overview"):
        checks: dict[str, Any] = {}
        if mode == "keyinfo":
            expected_keyinfo = int(expected_keyinfo_headings or 9)
            heading1 = [paragraph for paragraph in document.paragraphs if (paragraph.style.name or "") == "Heading 1"]
            checks["keyinfoSections"] = {"heading1": len(heading1), "expected": expected_keyinfo, "packageCount": max(1, (expected_keyinfo - 5) // 4)}
            if len(heading1) != expected_keyinfo:
                findings.append({"code": "keyinfo_heading_count", "message": f"关键信息一级章节 {len(heading1)} != {expected_keyinfo}（5+4×包数）"})
            if not document.paragraphs:
                findings.append({"code": "keyinfo_empty", "message": "关键信息 docx 无段落"})
        else:
            required_sections = ("输入摘要", "大纲与正文", "阶段与状态迁移", "审查与优化", "导出信息", "AI 参与说明", "数据缺口")
            missing_sections = [section for section in required_sections if section not in text]
            checks["overviewSections"] = {"required": list(required_sections), "missing": missing_sections}
            if missing_sections:
                findings.append({"code": "overview_sections_missing", "message": f"工作概览缺少栏目：{missing_sections}"})
            if not document.paragraphs:
                findings.append({"code": "overview_empty", "message": "工作概览 docx 无段落"})
        toc_present = False
        try:
            toc_present = any("TOC" in (node.text or "").upper() for node in document.element.xpath(".//w:instrText"))
        except Exception:  # noqa: BLE001
            toc_present = False
        checks["tocFieldPresent"] = toc_present
        if toc_present:
            findings.append({"code": "toc_field_present", "message": "文档存在 TOC 域（不允许）"})
        color_values, font_attrs = _docx_appearance(docx_path)
        checks["colors"] = sorted(color_values)
        checks["fontAttrs"] = sorted(font_attrs)
        if any(value != "auto" for value in color_values):
            findings.append({"code": "colors_not_auto", "message": f"存在非 auto 颜色：{sorted(color_values)[:5]}"})
        bad_fonts = [font for font in font_attrs if font not in ALLOWED_FONTS and font not in ("Consolas",)]
        if bad_fonts:
            findings.append({"code": "fonts_not_allowed", "message": f"字体不在白名单：{bad_fonts[:5]}"})
        heading_paragraphs = [paragraph for paragraph in document.paragraphs if HEADING_RE.match(paragraph.style.name or "")]
        return {
            "ok": not findings,
            "findings": findings,
            "warnings": warnings,
            "checks": checks,
            "expectedHeadings": expected_keyinfo if mode == "keyinfo" else 0,
            "headingParagraphs": len(heading_paragraphs),
            "images": len(document.inline_shapes),
            "tables": len(document.tables),
            "wordCount": count_words(text),
        }
    expected_titles = _outline_titles(outline)
    heading_paragraphs = [paragraph for paragraph in document.paragraphs if HEADING_RE.match(paragraph.style.name or "")]
    heading_texts = {paragraph.text.strip() for paragraph in heading_paragraphs}
    missing_titles = [title for title in expected_titles if title not in heading_texts]
    checks["headings"] = {"expected": len(expected_titles), "matched": len(expected_titles) - len(missing_titles), "totalHeadingParagraphs": len(heading_paragraphs), "missing": missing_titles[:10]}
    if missing_titles:
        findings.append({"code": "headings_missing", "message": f"outline 标题未以 Heading 输出：{missing_titles[:5]}"})
    numbering_ok, numbering_detail = _heading_numbering_ok(document, len(expected_titles))
    checks["numbering"] = {"ok": numbering_ok, "detail": numbering_detail}
    if not numbering_ok:
        findings.append({"code": "heading_numbering_missing", "message": numbering_detail})

    expected_images = _produced_figure_count(package_dir, outline)
    actual_images = len(document.inline_shapes)
    checks["images"] = {"expected": expected_images, "actual": actual_images}
    if actual_images != expected_images:
        findings.append({"code": "images_mismatch", "message": f"图片数 {actual_images} != produced {expected_images}"})

    captions = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip().startswith("▼表")]
    expected_tables = len(document.tables)
    checks["tables"] = {"docxTables": expected_tables, "captions": len(captions)}
    if captions and abs(expected_tables - len(captions)) > 1:
        findings.append({"code": "tables_mismatch", "message": f"表格数 {expected_tables} vs ▼表题注 {len(captions)}（容差±1）"})
    if "▼表" in text and not captions:
        findings.append({"code": "table_caption_missing", "message": "正文含 ▼表 文本但无题注段落"})
    if isinstance(report_data, Mapping) and report_data.get("plannedTables") is not None and "supplementTables" in outline:
        supplements = [item for item in report_data.get("supplementTables") or [] if isinstance(item, Mapping)]
        planned = int(report_data.get("plannedTables") or 0)
        expected_registered = planned + len(supplements)
        checks["tableRegistration"] = {"planned": planned, "supplements": len(supplements), "expected": expected_registered, "docxTables": expected_tables}
        if expected_tables < planned:
            findings.append({"code": "table_registration_mismatch", "message": f"docx 表格 {expected_tables} < 计划 table {planned}（计划表缺失）"})
        elif expected_tables > expected_registered:
            warnings.append(f"docx 额外表格 {expected_tables - expected_registered} 个未登记到 supplementTables（上不封顶，仅提示）")

    bad_captions: list[dict[str, Any]] = []
    for paragraph in document.paragraphs:
        caption_text = paragraph.text.strip()
        if not caption_text.startswith(("▼表", "▲图")):
            continue
        prefix_ok = bool(re.match(r"^[▼▲][表图]-", caption_text))
        bold_kind = any((run.bold is True) and run.text.strip() in ("表", "图") for run in paragraph.runs)
        if not prefix_ok or not bold_kind:
            bad_captions.append({"text": caption_text[:40], "prefixOk": prefix_ok, "boldKind": bold_kind})
    checks["captions"] = {"count": len(captions) + sum(1 for p in document.paragraphs if p.text.strip().startswith("▲图")), "bad": bad_captions[:10]}
    if bad_captions:
        findings.append({"code": "caption_format_mismatch", "message": f"题注格式/加粗不符合要求：{bad_captions[:3]}"})

    section = document.sections[0]
    content_height = float(section.page_height.cm) - float(section.top_margin.cm) - float(section.bottom_margin.cm)
    content_width = float(section.page_width.cm) - float(section.left_margin.cm) - float(section.right_margin.cm)
    overflowing = [
        {"index": index, "heightCm": round(shape.height.cm, 2), "widthCm": round(shape.width.cm, 2)}
        for index, shape in enumerate(document.inline_shapes)
        if shape.height.cm > content_height + 0.01 or shape.width.cm > content_width + 0.01 or shape.height.cm < 1.19
    ]
    checks["imageBounds"] = {"contentHeightCm": round(content_height, 2), "contentWidthCm": round(content_width, 2), "overflowing": overflowing}
    if overflowing:
        findings.append({"code": "images_overflow", "message": f"图片超出可用页面/小于1.2cm：{overflowing[:5]}"})

    stats = outline.get("stats") if isinstance(outline.get("stats"), Mapping) else {}
    expected_words = int(stats.get("writtenWordTotal") or stats.get("writtenWordCount") or stats.get("actualWordCount") or stats.get("totalWordCount") or package_meta.get("expectedWordCount") or 0)
    actual_words = count_words(text)
    checks["wordCount"] = {"expected": expected_words, "actual": actual_words}
    if expected_words > 0 and abs(actual_words - expected_words) / expected_words > 0.20:
        findings.append({"code": "word_count_out_of_range", "message": f"正文 {actual_words} vs 大纲 {expected_words}（>±20%）"})

    samples: list[dict[str, Any]] = []
    leaves = _leaf_nodes(outline)
    for leaf in leaves[:3]:
        title = str(leaf.get("title") or "").strip()
        matched_index = next((index for index, paragraph in enumerate(document.paragraphs) if paragraph.text.strip() == title), None)
        if matched_index is None:
            samples.append({"chapterId": leaf.get("id"), "ok": False, "reason": "heading_not_found"})
            continue
        next_text = ""
        for paragraph in document.paragraphs[matched_index + 1 :]:
            if paragraph.text.strip():
                next_text = paragraph.text.strip()
                break
        samples.append({"chapterId": leaf.get("id"), "ok": bool(next_text), "firstParagraph": next_text[:60]})
    checks["samples"] = samples
    if any(not item["ok"] for item in samples):
        findings.append({"code": "sample_first_paragraph_empty", "message": f"抽查首段为空：{[item for item in samples if not item['ok']]}"})

    toc_present = False
    try:
        instructions = document.element.xpath(".//w:instrText")
        toc_present = any("TOC" in (node.text or "").upper() for node in instructions)
    except Exception:  # noqa: BLE001
        toc_present = False
    checks["tocFieldPresent"] = toc_present
    if toc_present:
        findings.append({"code": "toc_field_present", "message": "文档存在 TOC 域（不允许）"})

    color_values: set[str] = set()
    font_attrs: set[str] = set()
    try:
        with zipfile.ZipFile(docx_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        color_values = set(re.findall(r'<w:color[^>]*w:val="([^"]+)"', xml))
        font_attrs = set(re.findall(r'w:(?:ascii|eastAsia)="([^"]+)"', xml))
    except Exception:  # noqa: BLE001
        warnings.append("无法读取 docx XML 做颜色/字体抽检")
    checks["colors"] = sorted(color_values)
    checks["fontAttrs"] = sorted(font_attrs)
    if any(value != "auto" for value in color_values):
        findings.append({"code": "colors_not_auto", "message": f"存在非 auto 颜色：{sorted(color_values)[:5]}"})
    bad_fonts = [font for font in font_attrs if font not in ALLOWED_FONTS and font not in ("Consolas",)]
    if bad_fonts:
        findings.append({"code": "fonts_not_allowed", "message": f"字体不在白名单：{bad_fonts[:5]}"})

    missing_figures = list((report_data or {}).get("missingFigures") or [])
    if missing_figures:
        message = f"缺图 {len(missing_figures)} 个：{[item.get('anchorId') for item in missing_figures][:5]}"
        warnings.append(message)
        if strict:
            findings.append({"code": "missing_figures", "message": message})
    table_markers = list((report_data or {}).get("tableMarkers") or [])
    checks["tableMarkers"] = {"count": len(table_markers), "items": table_markers[:10]}
    if table_markers:
        warnings.append(f"table 类型 figurePlan 不应有 BG:FIGURE 标记：{[item.get('anchorId') for item in table_markers][:5]}")
    return {
        "ok": not findings,
        "findings": findings,
        "warnings": warnings,
        "checks": checks,
        "expectedHeadings": len(expected_titles),
        "headingParagraphs": len(heading_paragraphs),
        "images": actual_images,
        "tables": expected_tables,
        "wordCount": actual_words,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_export_verify.py",
        description="BidGenie F7.5 导出后自检（docx 标题/图片/表格/字数/样式/无 TOC）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--docx", help="待校验 docx（缺省读 export/_last-export-report.json 的 docxPath）")
    parser.add_argument("--mode", choices=("tech", "keyinfo", "overview"), default="tech")
    parser.add_argument("--report", action="store_true", help="写 export/_last-export-report.json（含 verify 结果）")
    parser.add_argument("--strict", action="store_true", help="缺失图/警告升级为失败")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    _, _, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, args.package
    )
    outline_path = outline_validate.outline_path(package_dir)
    if not outline_path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 chapters/_outline/outline.json")
    outline = jsonio.read_json(outline_path)
    report_path = package_dir / REPORT_REL if args.mode == "tech" else package_dir / "export" / f"_last-export-report-{args.mode}.json"
    report_data: dict[str, Any] = {}
    if report_path.is_file():
        try:
            loaded = jsonio.read_json(report_path)
            report_data = loaded if isinstance(loaded, dict) else {}
        except errors.BgError:
            report_data = {}
    elif args.mode == "tech" and (package_dir / REPORT_REL).is_file():
        try:
            loaded = jsonio.read_json(package_dir / REPORT_REL)
            report_data = loaded if isinstance(loaded, dict) else {}
        except errors.BgError:
            report_data = {}
    docx_path: Path | None = None
    if args.docx:
        candidate = Path(args.docx)
        docx_path = candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    elif report_data.get("docxPath"):
        candidate = Path(str(report_data["docxPath"]))
        docx_path = candidate if candidate.is_absolute() else (project_dir / candidate)
    if docx_path is None and args.mode == "keyinfo":
        exports_dir = project_dir / "exports"
        candidates = sorted((path for path in exports_dir.glob("*.docx") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
        preferred = [path for path in candidates if "关键信息" in path.name] or candidates
        if preferred:
            docx_path = preferred[0]
            report_data.setdefault("docxPath", paths.rel_posix(project_dir, docx_path))
    if docx_path is None and args.mode == "overview":
        overview_report = package_dir / "export" / "_last-overview-report.json"
        if overview_report.is_file():
            try:
                overview_doc = jsonio.read_json(overview_report)
                if isinstance(overview_doc, Mapping) and overview_doc.get("overviewDocxPath"):
                    candidate = Path(str(overview_doc["overviewDocxPath"]))
                    docx_path = candidate if candidate.is_absolute() else (project_dir / candidate)
            except errors.BgError:
                pass
    if docx_path is None or not docx_path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "找不到待校验 docx", hint="--docx <path> 或先运行 bg_export_docx.py", detail={"docx": str(docx_path) if docx_path else None})
    package_count = project_doc.get("packageCount") if isinstance(project_doc, Mapping) else None
    expected_keyinfo_headings = 5 + 4 * int(package_count) if isinstance(package_count, int) and not isinstance(package_count, bool) and package_count >= 1 else None
    result = verify_docx(
        docx_path=docx_path,
        mode=args.mode,
        package_dir=package_dir,
        outline=outline,
        package_meta=package_meta,
        report_data=report_data,
        strict=bool(args.strict),
        expected_keyinfo_headings=expected_keyinfo_headings,
    )
    verify_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "mode": args.mode,
        "docxPath": paths.rel_posix(project_dir, docx_path) if docx_path.is_relative_to(project_dir) else str(docx_path),
        "sha256": "sha256:" + hashlib.sha256(docx_path.read_bytes()).hexdigest(),
        "modeChecked": args.mode,
        "verifiedAt": paths.iso_now(),
        "strict": bool(args.strict),
        "ok": result["ok"],
        "findings": result["findings"],
        "warnings": result["warnings"],
        "checks": result["checks"],
    }
    event_log.log_event(
        workspace,
        "package.verified",
        actor="script",
        project=str((project_doc or {}).get("projectId") or ""),
        package=target,
        detail={"packageId": target, "mode": args.mode, "docxPath": verify_payload["docxPath"], "ok": result["ok"], "findings": len(result["findings"])},
    )
    if args.report:
        merged = dict(report_data)
        merged.update(
            {
                "schemaVersion": SCHEMA_VERSION,
                "mode": args.mode,
                "docxPath": verify_payload["docxPath"],
                "sha256": verify_payload["sha256"],
                "verifiedAt": verify_payload["verifiedAt"],
                "verifyOk": result["ok"],
                "verifyFindings": result["findings"],
                "verifyWarnings": result["warnings"],
                "verifyChecks": result["checks"],
                "blocks": result["findings"],
                "blockerCount": sum(1 for item in result["findings"] if item.get("code") != "missing_figures"),
            }
        )
        jsonio.write_json_atomic(report_path, merged)
    if not result["ok"]:
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"导出后自检失败：{len(result['findings'])} 项",
            hint="保留 docx，修正问题后重新导出/自检；--strict 会把缺图升级为失败",
            detail={"findings": result["findings"], "checks": result["checks"], "report": paths.rel_posix(project_dir, report_path)},
        )
    return {
        "ok": True,
        "command": "export_verify",
        "projectId": str(project_doc.get("projectId") or ""),
        "packageId": target,
        "mode": args.mode,
        "docxPath": verify_payload["docxPath"],
        "reportPath": paths.rel_posix(project_dir, report_path) if args.report else "",
        "expectedHeadings": result["expectedHeadings"],
        "headingParagraphs": result["headingParagraphs"],
        "images": result["images"],
        "tables": result["tables"],
        "wordCount": result["wordCount"],
        "findings": [],
        "warnings": result["warnings"],
        "checks": result["checks"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_export_verify.py",
    )


if __name__ == "__main__":
    sys.exit(main())
