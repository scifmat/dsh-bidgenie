#!/usr/bin/env python3
"""BidGenie F5-2 图表资源渲染（PRD F5.1.6 / F5.2.3–F5.2.5、决策 9.8/9.9）。

- 读取 `<packageDir>/chapters/_outline/outline.json` 的 figurePlan 与章节 md 插入标记
  （`<!-- BG:FIGURE:<anchorId> -->`）；**table 类型按 bg-write 硬规则不写 BG:FIGURE
  （用 `▼表` 题注定位），因此不做缺失标记检查**（P1-4；误加的标记由导出自检
  `tableMarkers` 记 warning / A 层审查约束，渲染侧不再提示“补标记”）；
- mermaid：源码 `<anchorId>.mmd`（缺失时从章节 md 的 ```mermaid 代码块提取）→ Node 脚本
  `libexec/node/render_mermaid.js`（系统 Chrome/Edge headless + 内置 `assets/mermaid/mermaid.min.js`）
  → `<anchorId>.png`；浏览器/库失败时降级 Pillow 简化占位图（warning，不中断正文）；
- terminal：文本 `<anchorId>.txt`（缺失时从 ```text/bash/console 代码块提取）→ Pillow 终端窗口 PNG；
- network：quota=0 时全部跳过（`networkDisabled`），保留接口；>0 时仅校验既有 `<anchorId>.png` + `sources.json`，缺素材记 warning；
- 资源命名 `<anchorId>.png/.mmd/.txt`；正文 md 只保留插入标记（本脚本不改 md、不内嵌 base64）；
- 幂等：`resources/render-manifest.json` 记录 source/png hash，已存在且 hash 一致跳过（`--force` 重渲染）；
- 临时目录：每次 node/浏览器渲染使用私有 TEMP 根目录，结束后整体删除（含浏览器自身
  的 `scoped_dir*`/`HeadlessChrome*` 兄弟目录），避免在系统 TEMP 或会话 TEMP
  （如 `<workspace>/dsh/tmp`）留下残留；浏览器子进程仍占用导致删不掉时记
  `render_temp_residue` warning 与 `tempResidue` 字段（跨进程无法强杀，系统会自行清理）；
- `--anchor` 单图重渲染、`--selftest` 离线自检。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, paths, schema, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MERMAID_LIB = PLUGIN_ROOT / "assets" / "mermaid" / "mermaid.min.js"
NODE_SCRIPT = Path(__file__).resolve().parent / "node" / "render_mermaid.js"
MANIFEST_FILE = "resources/render-manifest.json"
CROP_PADDING = 28
# R1 审计：写 DPI 120，并保证物理尺寸 ≤ A4 可用区 15.9×24.7cm（120dpi 对应 751×1166px）
TARGET_DPI = 120
MAX_PHYSICAL_WIDTH_CM = 15.9
MAX_PHYSICAL_HEIGHT_CM = 23.0
CROP_MAX_WIDTH = int(MAX_PHYSICAL_WIDTH_CM / 2.54 * TARGET_DPI)  # 751px
CROP_MAX_HEIGHT = int(MAX_PHYSICAL_HEIGHT_CM / 2.54 * TARGET_DPI)  # 1166px
CROP_WHITE_THRESHOLD = 245
# R3：渲染/裁切参数版本；变更时必须使 manifest 缓存失效重渲染
RENDER_PROFILE_VERSION = "m6.1-crop-dpi-a4safe-2"
FIGURE_MARKER_RE = r"<!--\s*BG:FIGURE:{anchor}\s*-->"
FENCE_RE = re.compile(r"```(?P<lang>[A-Za-z0-9_-]*)\s*\n(?P<code>.*?)\n```", re.DOTALL)
MERMAID_LANGS = ("mermaid", "")
TERMINAL_LANGS = ("text", "txt", "bash", "shell", "sh", "console", "powershell", "ps1", "cmd")
FONT_CANDIDATES_ASCII = (
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)
FONT_CANDIDATES_CJK = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/nsimsun.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


class RenderFallback(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(project_dir: Path, path: Path) -> str:
    return paths.rel_posix(project_dir, path)


def find_node() -> str | None:
    candidate = os.environ.get("BG_NODE") or shutil.which("node")
    return candidate if candidate and Path(candidate).is_file() else None


#: 单次 node/浏览器渲染的私有临时根目录前缀（P1-4：渲染不留 dsh/tmp / 系统 TEMP 残留）。
RENDER_SCRATCH_PREFIX = "bidgenie-render-scratch-"


def make_render_scratch() -> Path:
    """为一次 node+浏览器渲染创建私有临时根目录。

    把 `TEMP`/`TMP`/`TMPDIR` 指向该目录后：node 自己的 `bidgenie-mermaid-*` 目录，
    以及 Chrome/Edge headless 自己创建的 `scoped_dir*` / `HeadlessChrome*` 兄弟目录
    都会落在本目录内，渲染结束可整体删除，不会污染系统 TEMP 或会话 TEMP
    （例如 `<workspace>/dsh/tmp`）。
    """
    return Path(tempfile.mkdtemp(prefix=RENDER_SCRATCH_PREFIX))


def cleanup_render_scratch(scratch: Path, *, attempts: int = 10) -> list[dict[str, str]]:
    """删除渲染私有临时根目录；返回未能删除的残留项（path/error）。

    Windows 下浏览器的 crashpad/GPU 子进程可能在截图后短暂占用文件，这里做短重试；
    仍失败说明是浏览器自身进程持有句柄，跨进程无法可靠删除（本脚本不 kill 用户浏览器），
    交由系统临时目录清理，同时以 warning + `tempResidue` 字段暴露。
    """
    if not scratch.is_dir():
        return []
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            shutil.rmtree(scratch)
            return []
        except OSError as exc:  # pragma: no cover - 依赖浏览器进程时序，回归只断言成功路径
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.05 * (attempt + 1))
    if scratch.exists():
        return [{"path": str(scratch), "error": last_error}]
    return []



def _font(size: int, *, cjk: bool = False) -> Any:
    from PIL import ImageFont

    candidates = FONT_CANDIDATES_CJK if cjk else FONT_CANDIDATES_ASCII
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _read_chapter_md(package_dir: Path, node: Mapping[str, Any]) -> tuple[Path | None, str]:
    file_path = node.get("filePath")
    if not isinstance(file_path, str) or not file_path:
        return None, ""
    path = package_dir / file_path
    if not path.is_file():
        return path, ""
    try:
        return path, path.read_text(encoding="utf-8")
    except OSError:
        return path, ""


def extract_code_block(
    markdown: str, anchor_id: str, *, kind: str
) -> tuple[str, bool, bool]:
    """从章节 md 提取锚点附近代码块；返回 (code, markerFound, extracted)。"""
    anchor = re.escape(anchor_id)
    marker_match = re.search(FIGURE_MARKER_RE.format(anchor=anchor), markdown)
    marker_found = marker_match is not None
    search_text = markdown[marker_match.start() : marker_match.start() + 6000] if marker_match else markdown
    langs = MERMAID_LANGS if kind == "mermaid" else TERMINAL_LANGS
    candidates: list[re.Match[str]] = list(FENCE_RE.finditer(search_text))
    for match in candidates:
        lang = (match.group("lang") or "").strip().lower()
        if lang in langs:
            return match.group("code").strip("\n"), marker_found, True
    return "", marker_found, False


# ---------------------------------------------------------------------------
# Pillow 渲染/降级
# ---------------------------------------------------------------------------
def render_terminal_png(lines: Sequence[str], caption: str, output: Path) -> dict[str, Any]:
    from PIL import Image, ImageDraw

    text_lines = [str(line).rstrip("\n") for line in lines] or ["(空终端输出)"]
    char_width = 18
    line_height = 30
    max_chars = max(40, max((len(line) for line in text_lines), default=40))
    width = min(3000, max(900, 64 + max_chars * char_width))
    height = min(4000, max(300, 100 + len(text_lines) * line_height + 40))
    image = Image.new("RGB", (width, height), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    font_ascii = _font(18, cjk=False)
    font_cjk = _font(18, cjk=True)
    title_font = _font(20, cjk=True)
    draw.rectangle([0, 0, width, 44], fill=(58, 58, 58))
    for index, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        draw.ellipse([16 + index * 28, 14, 32 + index * 28, 30], fill=color)
    draw.text((110, 11), f"终端 — {caption}", font=title_font, fill=(230, 230, 230))
    y = 64
    for line in text_lines:
        cjk = any(ord(char) > 127 for char in line)
        draw.text((28, y), line, font=font_cjk if cjk else font_ascii, fill=(220, 220, 220))
        y += line_height
    if image.width > CROP_MAX_WIDTH or image.height > CROP_MAX_HEIGHT:
        image.thumbnail((CROP_MAX_WIDTH, CROP_MAX_HEIGHT), Image.LANCZOS)
    width, height = image.size
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", dpi=(TARGET_DPI, TARGET_DPI))
    return {
        "width": width,
        "height": height,
        "engine": "pillow",
        "lines": len(text_lines),
        "dpi": TARGET_DPI,
        "physicalWidthCm": round(width / TARGET_DPI * 2.54, 2),
        "physicalHeightCm": round(height / TARGET_DPI * 2.54, 2),
    }


def render_placeholder_png(caption: str, source: str, output: Path, reason: str) -> dict[str, Any]:
    """mermaid 渲染降级：Pillow 简化占位图（保留源文件，warning 不中断）。"""
    from PIL import Image, ImageDraw

    width, height = 1200, 800
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    title_font = _font(30, cjk=True)
    body_font = _font(18, cjk=True)
    small_font = _font(15, cjk=True)
    draw.rectangle([0, 0, width, height], outline=(180, 180, 180), width=3)
    draw.rectangle([0, 0, width, 70], fill=(240, 240, 240))
    draw.text((30, 20), f"▲图 {caption}", font=title_font, fill=(30, 30, 30))
    draw.text((30, 100), "mermaid 图（占位：渲染环境不可用，需在审查阶段重试）", font=body_font, fill=(120, 60, 0))
    draw.text((30, 140), f"降级原因：{reason}", font=small_font, fill=(130, 130, 130))
    lines = [line for line in source.splitlines() if line.strip()][:22]
    y = 200
    for line in lines:
        draw.text((40, y), line[:100], font=body_font, fill=(80, 80, 80))
        y += 26
    if image.width > CROP_MAX_WIDTH or image.height > CROP_MAX_HEIGHT:
        image.thumbnail((CROP_MAX_WIDTH, CROP_MAX_HEIGHT), Image.LANCZOS)
    width, height = image.size
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", dpi=(TARGET_DPI, TARGET_DPI))
    return {
        "width": width,
        "height": height,
        "engine": "pillow-fallback",
        "reason": reason,
        "dpi": TARGET_DPI,
        "physicalWidthCm": round(width / TARGET_DPI * 2.54, 2),
        "physicalHeightCm": round(height / TARGET_DPI * 2.54, 2),
    }


def validate_png(path: Path) -> dict[str, Any]:
    """PNG 基本校验：可解析、尺寸 ≥200×150、非纯色。"""
    from PIL import Image, ImageStat

    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        raw_dpi = image.info.get("dpi")
        rgb = image.convert("RGB")
        stat = ImageStat.Stat(rgb)
        variance = max(stat.var)
    dpi = TARGET_DPI
    if isinstance(raw_dpi, tuple) and raw_dpi and isinstance(raw_dpi[0], (int, float)):
        dpi = int(round(raw_dpi[0]))
    elif isinstance(raw_dpi, (int, float)):
        dpi = int(round(raw_dpi))
    physical_width_cm = round(width / dpi * 2.54, 2) if dpi else None
    physical_height_cm = round(height / dpi * 2.54, 2) if dpi else None
    return {
        "width": width,
        "height": height,
        "variance": round(float(variance), 3),
        "dpi": dpi,
        "physicalWidthCm": physical_width_cm,
        "physicalHeightCm": physical_height_cm,
        "ok": width >= 200 and height >= 150 and variance > 1.0,
    }


def write_png_dpi(path: Path, dpi: int = TARGET_DPI) -> None:
    """确保 PNG 带 DPI 元数据（--no-crop 时也保留 DPI 处理）。"""
    from PIL import Image

    with Image.open(path) as image:
        image.save(path, "PNG", dpi=(dpi, dpi))


def image_content_bbox(path: Path, *, white_threshold: int = CROP_WHITE_THRESHOLD) -> tuple[int, int, int, int] | None:
    """非白内容 bbox（RGBA 先按 alpha 过滤；白底截图用于裁切）。"""
    from PIL import Image, ImageChops

    with Image.open(path) as source:
        image = source.convert("RGB")
        white = Image.new("RGB", image.size, (255, 255, 255))
        diff = ImageChops.difference(image, white).convert("L")
        # 接近白色的像素视为空白：diff <= 255-threshold 丢弃
        cutoff = max(1, 255 - white_threshold)
        mask = diff.point(lambda value: 255 if value >= cutoff else 0)
        return mask.getbbox()


def crop_mermaid_png(
    path: Path,
    *,
    padding: int = CROP_PADDING,
    max_width: int = CROP_MAX_WIDTH,
    max_height: int = CROP_MAX_HEIGHT,
    white_threshold: int = CROP_WHITE_THRESHOLD,
) -> dict[str, Any]:
    """mermaid PNG 按内容 bbox 裁切 + padding + 最大宽高限制；返回裁剪信息（含原图尺寸）。"""
    from PIL import Image

    with Image.open(path) as source:
        original_width, original_height = source.size
    bbox = image_content_bbox(path, white_threshold=white_threshold)
    if bbox is None:
        return {
            "cropped": False,
            "originalWidth": original_width,
            "originalHeight": original_height,
            "width": original_width,
            "height": original_height,
            "padding": padding,
            "bbox": None,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "dpi": TARGET_DPI,
            "physicalWidthCm": round(original_width / TARGET_DPI * 2.54, 2),
            "physicalHeightCm": round(original_height / TARGET_DPI * 2.54, 2),
            "reason": "no_content_bbox",
        }
    with Image.open(path) as source:
        image = source.convert("RGB")
        left = max(0, bbox[0] - padding)
        top = max(0, bbox[1] - padding)
        right = min(original_width, bbox[2] + padding)
        bottom = min(original_height, bbox[3] + padding)
        cropped = image.crop((left, top, right, bottom))
        resized = False
        # R1：按 A4 可用区物理尺寸限制像素（120dpi 下 751×1166），保持清晰度等比缩放
        limit_width = min(max_width, int(MAX_PHYSICAL_WIDTH_CM / 2.54 * TARGET_DPI))
        limit_height = min(max_height, int(MAX_PHYSICAL_HEIGHT_CM / 2.54 * TARGET_DPI))
        if cropped.width > limit_width or cropped.height > limit_height:
            cropped.thumbnail((limit_width, limit_height), Image.LANCZOS)
            resized = True
        cropped.save(path, "PNG", optimize=True, dpi=(TARGET_DPI, TARGET_DPI))
        width, height = cropped.size
    return {
        "cropped": True,
        "originalWidth": original_width,
        "originalHeight": original_height,
        "width": width,
        "height": height,
        "padding": padding,
        "bbox": list(bbox),
        "cropBox": [left, top, right, bottom],
        "maxWidth": limit_width,
        "maxHeight": limit_height,
        "dpi": TARGET_DPI,
        "physicalWidthCm": round(width / TARGET_DPI * 2.54, 2),
        "physicalHeightCm": round(height / TARGET_DPI * 2.54, 2),
        "resizedToMax": resized,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_mermaid_via_node(
    source: Path,
    output: Path,
    *,
    browser: str,
    scale: int = 2,
    timeout: int = 120,
) -> dict[str, Any]:
    node = find_node()
    if not node:
        raise RenderFallback("node 不可用")
    if not NODE_SCRIPT.is_file():
        raise RenderFallback(f"缺少 node/render_mermaid.js：{NODE_SCRIPT}")
    if not MERMAID_LIB.is_file():
        raise RenderFallback(f"缺少内置 mermaid 库：{MERMAID_LIB}")
    command = [
        node,
        str(NODE_SCRIPT),
        str(source),
        str(output),
        "--browser",
        browser,
        "--lib",
        str(MERMAID_LIB),
        "--scale",
        str(scale),
    ]
    # P1-4：node 的 os.tmpdir() 与浏览器临时目录都跟随 TEMP/TMP，指向本次私有根目录，
    # 渲染结束（成功/失败/超时）后整体删除，避免残留。
    scratch = make_render_scratch()
    env = {**os.environ, "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch)}
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    finally:
        residue = cleanup_render_scratch(scratch)
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    if completed.returncode != 0 or not isinstance(payload, Mapping) or not payload.get("ok"):
        detail = payload or {"stdout": completed.stdout[-500:], "stderr": completed.stderr[-500:]}
        raise RenderFallback(f"Node 渲染失败：{detail}")
    result = dict(payload)
    result["tempResidue"] = residue
    return result


def collect_figures(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    for node in outline_validate.iter_preorder(document.get("chapters") or []):
        if isinstance(node.get("children"), list) and node.get("children"):
            continue
        for plan in node.get("figurePlan") or []:
            if isinstance(plan, Mapping):
                figures.append({"node": node, "plan": dict(plan)})
    return figures


def render_resources(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
    anchor: str | None = None,
    force: bool = False,
    browser: str = "auto",
    fallback: str = "pillow",
    kind_filter: str | None = None,
    crop: bool = True,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    outline_path = outline_validate.outline_path(package_dir)
    if not outline_path.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 chapters/_outline/outline.json", hint="先完成 M5 大纲落盘")
    document = jsonio.read_json(outline_path)
    figures = collect_figures(document)
    if anchor:
        figures = [item for item in figures if str(item["plan"].get("anchorId")) == anchor]
        if not figures:
            raise errors.BgError(errors.E_NOT_FOUND, f"outline 中不存在图表锚点：{anchor}", detail={"anchor": anchor})
    if kind_filter:
        figures = [item for item in figures if item["plan"].get("type") == kind_filter]
    quota = package_meta.get("figureQuota") if isinstance(package_meta.get("figureQuota"), Mapping) else {}
    network_quota = int(quota.get("network") or 0)
    manifest_path = package_dir / MANIFEST_FILE
    manifest: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "packageId": target, "anchors": {}}
    if manifest_path.is_file():
        try:
            # 读侧也加锁，避免读到另一个进程的中间状态（写为原子替换）
            with jsonio.FileLock(manifest_path, timeout=jsonio.LOCK_TIMEOUT_SEC):
                old = jsonio.read_json(manifest_path)
            if isinstance(old, Mapping):
                manifest.update({key: old[key] for key in ("schemaVersion", "packageId") if key in old})
                manifest["anchors"] = dict(old.get("anchors") or {})
        except errors.BgError:
            pass
    manifest_updates: dict[str, dict[str, Any]] = {}
    rendered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    temp_residues: list[dict[str, str]] = []
    marker_checks = {"required": 0, "found": 0, "missing": 0, "tableSkipped": 0}
    changed = False

    def item_result(plan: Mapping[str, Any], node: Mapping[str, Any], status: str, **extra: Any) -> dict[str, Any]:
        item = {
            "anchorId": plan.get("anchorId"),
            "type": plan.get("type"),
            "chapterId": node.get("id"),
            "caption": plan.get("caption"),
            "status": status,
        }
        item.update(extra)
        return item

    for item in figures:
        node = item["node"]
        plan = item["plan"]
        figure_type = str(plan.get("type") or "")
        anchor_id = str(plan.get("anchorId") or "")
        chapter_path, markdown = _read_chapter_md(package_dir, node)
        marker_found = bool(
            search_marker := re.search(FIGURE_MARKER_RE.format(anchor=re.escape(anchor_id)), markdown)
        ) if markdown else False
        if chapter_path is None or not markdown:
            warnings.append({"anchorId": anchor_id, "code": "chapter_file_missing", "message": f"章节文件不存在：{node.get('filePath')}"})
        elif figure_type == "table":
            # P1-4：table 按 bg-write 硬规则不写 BG:FIGURE（用 ▼表 题注定位），
            # 不参与“缺插入标记”检查；误加的标记由 A 层审查（bg_review_script）拦截。
            marker_checks["tableSkipped"] += 1
        else:
            marker_checks["required"] += 1
            if marker_found:
                marker_checks["found"] += 1
            else:
                marker_checks["missing"] += 1
                warnings.append({"anchorId": anchor_id, "code": "figure_marker_missing", "message": "章节 md 缺少 BG:FIGURE 插入标记"})
        if figure_type == "table":
            skipped.append(
                item_result(
                    plan,
                    node,
                    "skipped",
                    reason="table 无需 png（table 无需 BG:FIGURE 插入标记，按 ▼表 题注定位）",
                    markerExpected=False,
                )
            )
            continue
        if figure_type == "network":
            if network_quota == 0:
                skipped.append(item_result(plan, node, "network_disabled", reason="network quota=0（决策 9.9 默认关闭）"))
                continue
            png_path = package_dir / "resources" / "network" / f"{anchor_id}.png"
            sources_path = package_dir / "resources" / "network" / "sources.json"
            if png_path.is_file() and sources_path.is_file():
                rendered.append(item_result(plan, node, "produced", resourcePath=rel(project_dir_path, png_path), engine="existing"))
            else:
                skipped.append(
                    item_result(plan, node, "skipped", reason="网络图素材缺失（需素材脚本下载/用户同意联网，M6-P3 不生成）")
                )
                warnings.append({"anchorId": anchor_id, "code": "network_source_missing", "message": "网络图缺少 png/sources.json"})
            continue
        if figure_type not in ("mermaid", "terminal"):
            skipped.append(item_result(plan, node, "skipped", reason=f"未知图表类型：{figure_type}"))
            continue
        resource_dir = package_dir / "resources" / figure_type
        resource_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".mmd" if figure_type == "mermaid" else ".txt"
        source_path = resource_dir / f"{anchor_id}{suffix}"
        png_path = resource_dir / f"{anchor_id}.png"
        if not source_path.is_file():
            code, extracted_marker, extracted = extract_code_block(markdown, anchor_id, kind=figure_type)
            if not extracted:
                failed.append(
                    item_result(plan, node, "failed", reason=f"缺少 {suffix} 源码且章节 md 无对应代码块")
                )
                warnings.append({"anchorId": anchor_id, "code": "source_missing", "message": "缺少图表源码/代码块"})
                continue
            candidate = code
            if figure_type == "mermaid" and not re.search(r"^\s*\w+", candidate):
                candidate = f"flowchart TD\n  A[\"{plan.get('caption')}\"]\n"
            source_path.write_text(candidate.rstrip() + "\n", encoding="utf-8", newline="\n")
            if not extracted_marker:
                warnings.append({"anchorId": anchor_id, "code": "figure_marker_missing", "message": "源码从 md 代码块提取，但缺少插入标记"})
        source_sha = sha256_file(source_path)
        cached = manifest["anchors"].get(anchor_id) if isinstance(manifest["anchors"], Mapping) else None
        current_profile = {
            "version": RENDER_PROFILE_VERSION,
            "crop": bool(crop),
            "padding": CROP_PADDING,
            "dpi": TARGET_DPI,
            "maxPhysicalWidthCm": MAX_PHYSICAL_WIDTH_CM,
            "maxPhysicalHeightCm": MAX_PHYSICAL_HEIGHT_CM,
        }
        if (
            not force
            and isinstance(cached, Mapping)
            and cached.get("sourceSha256") == source_sha
            and cached.get("status") != "fallback"
            and cached.get("renderProfile") == current_profile
            and png_path.is_file()
            and cached.get("pngSha256") == sha256_file(png_path)
        ):
            skipped.append(
                item_result(
                    plan,
                    node,
                    "skipped",
                    reason="hash 一致",
                    resourcePath=rel(project_dir_path, png_path),
                    sourcePath=rel(project_dir_path, source_path),
                )
            )
            continue
        render_meta: dict[str, Any] = {}
        status = "produced"
        engine = ""
        fallback_reason = ""
        if figure_type == "mermaid":
            try:
                if browser == "none":
                    raise RenderFallback("--browser none（强制降级）")
                render_meta = render_mermaid_via_node(source_path, png_path, browser=browser)
                temp_residues.extend(render_meta.pop("tempResidue", []) or [])
                engine = str(render_meta.get("engine") or "chrome")
                status = "produced"
                if not crop:
                    write_png_dpi(png_path)
            except (RenderFallback, subprocess.TimeoutExpired, OSError) as exc:
                fallback_reason = str(exc)[:300]
                if fallback != "pillow":
                    failed.append(item_result(plan, node, "failed", reason=fallback_reason))
                    warnings.append({"anchorId": anchor_id, "code": "render_failed", "message": fallback_reason})
                    continue
                render_meta = render_placeholder_png(
                    str(plan.get("caption") or anchor_id),
                    source_path.read_text(encoding="utf-8"),
                    png_path,
                    fallback_reason,
                )
                engine = "pillow-fallback"
                status = "fallback"
                warnings.append(
                    {
                        "anchorId": anchor_id,
                        "code": "mermaid_fallback",
                        "message": f"mermaid 渲染降级 Pillow 占位图：{fallback_reason}",
                    }
                )
        else:
            text = source_path.read_text(encoding="utf-8")
            lines = text.splitlines()
            render_meta = render_terminal_png(lines, str(plan.get("caption") or anchor_id), png_path)
            engine = "pillow"
            status = "produced"
        crop_info: dict[str, Any] | None = None
        # R2：Pillow fallback 占位图不做 bbox 裁切（不标记 cropped），仅浏览器 produced 图裁切
        if figure_type == "mermaid" and crop and status == "produced":
            crop_info = crop_mermaid_png(png_path)
        validation = validate_png(png_path)
        if not validation["ok"]:
            failed.append(item_result(plan, node, "failed", reason=f"PNG 校验失败：{validation}"))
            warnings.append({"anchorId": anchor_id, "code": "png_invalid", "message": f"PNG 不满足最小尺寸/非纯色：{validation}"})
            continue
        png_sha = sha256_file(png_path)
        original_width = (crop_info or {}).get("originalWidth", validation["width"])
        original_height = (crop_info or {}).get("originalHeight", validation["height"])
        manifest_updates[anchor_id] = {
            "type": figure_type,
            "chapterId": node.get("id"),
            "sourcePath": rel(project_dir_path, source_path),
            "sourceSha256": source_sha,
            "pngPath": rel(project_dir_path, png_path),
            "pngSha256": png_sha,
            "status": status,
            "engine": engine,
            "fallbackReason": fallback_reason,
            "renderedAt": paths.iso_now(),
            "width": validation["width"],
            "height": validation["height"],
            "originalWidth": original_width,
            "originalHeight": original_height,
            "cropped": bool((crop_info or {}).get("cropped")),
            "crop": crop_info or {},
            "dpi": validation.get("dpi"),
            "physicalWidthCm": validation.get("physicalWidthCm"),
            "physicalHeightCm": validation.get("physicalHeightCm"),
            "renderProfile": current_profile,
        }
        changed = True
        result = item_result(
            plan,
            node,
            status,
            resourcePath=rel(project_dir_path, png_path),
            sourcePath=rel(project_dir_path, source_path),
            engine=engine,
            width=validation["width"],
            height=validation["height"],
            originalWidth=original_width,
            originalHeight=original_height,
            cropped=bool((crop_info or {}).get("cropped")),
            dpi=validation.get("dpi"),
            physicalWidthCm=validation.get("physicalWidthCm"),
            physicalHeightCm=validation.get("physicalHeightCm"),
        )
        if fallback_reason:
            result["fallbackReason"] = fallback_reason
        rendered.append(result)
    if manifest_updates:
        # GAP：read-modify-write 加锁 + 合并最新 manifest，避免并行 writer 丢 anchor
        with jsonio.FileLock(manifest_path, timeout=jsonio.LOCK_TIMEOUT_SEC):
            latest: dict[str, Any] = {}
            if manifest_path.is_file():
                try:
                    loaded = jsonio.read_json(manifest_path)
                    if isinstance(loaded, Mapping):
                        latest = dict(loaded)
                except errors.BgError:
                    latest = {}
            anchors = dict(latest.get("anchors") or {}) if isinstance(latest.get("anchors"), Mapping) else {}
            anchors.update(manifest_updates)
            merged = {
                "schemaVersion": SCHEMA_VERSION,
                "packageId": target,
                "anchors": anchors,
                "generatedAt": paths.iso_now(),
            }
            # 测试钩子：在锁内放大临界区，验证并发写不丢 anchor
            delay_ms = os.environ.get("BG_RENDER_MANIFEST_DELAY_MS")
            if delay_ms:
                try:
                    time.sleep(min(float(delay_ms), 5000) / 1000.0)
                except (TypeError, ValueError):
                    pass
            jsonio.write_json_atomic(manifest_path, merged, lock=False)
            manifest = merged
        changed = True
    fallback_items = [item for item in rendered if isinstance(item, Mapping) and item.get("status") == "fallback"]
    render_fallback = bool(fallback_items)
    if render_fallback:
        warnings.append({"code": "render_fallback_summary", "message": f"渲染降级：{len(fallback_items)} 个图使用 Pillow 占位（属于降级产物，不是真实渲染）"})
    for residue in temp_residues:
        warnings.append(
            {
                "code": "render_temp_residue",
                "message": f"渲染临时目录未能删除（多为浏览器子进程仍占用文件，退出后由系统清理）：{residue.get('path')}",
                "path": residue.get("path"),
                "error": residue.get("error"),
            }
        )
    return {
        "ok": True,
        "command": "render_resources",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "packageDir": rel(project_dir_path, package_dir),
        "network": {"quota": network_quota, "disabled": network_quota == 0},
        "renderFallback": render_fallback,
        "fallbackCount": len(fallback_items),
        "markerChecks": marker_checks,
        "tempResidue": temp_residues,
        "rendered": rendered,
        "skipped": skipped,
        "failed": failed,
        "warnings": warnings,
        "manifestPath": rel(project_dir_path, manifest_path),
        "manifestUpdated": changed,
        "crop": {
            "enabled": crop,
            "padding": CROP_PADDING,
            "dpi": TARGET_DPI,
            "maxWidth": CROP_MAX_WIDTH,
            "maxHeight": CROP_MAX_HEIGHT,
            "maxPhysicalWidthCm": MAX_PHYSICAL_WIDTH_CM,
            "maxPhysicalHeightCm": MAX_PHYSICAL_HEIGHT_CM,
            "whiteThreshold": CROP_WHITE_THRESHOLD,
            "renderProfileVersion": RENDER_PROFILE_VERSION,
        },
        "engine": {"node": find_node() or "", "mermaidLib": str(MERMAID_LIB) if MERMAID_LIB.is_file() else ""},
        "currentStage": state.get("currentStage"),
        "revision": state.get("revision"),
        "note": "正文 md 插入标记保持原样（不改 md/不内嵌 base64）；资源文件在 resources/<类型>/<anchorId>.*",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_render.py",
        description="BidGenie F5-2 图表资源渲染：mermaid（Chrome/Edge headless+Pillow 降级）、terminal（Pillow）、network=0 跳过",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--anchor", help="只渲染指定 anchorId")
    parser.add_argument("--kind", choices=("mermaid", "terminal", "network", "table"), help="只渲染指定图表类型")
    parser.add_argument("--browser", default="auto", choices=("auto", "chrome", "edge", "none"), help="浏览器选择；none 强制 Pillow 降级")
    parser.add_argument("--fallback", default="pillow", choices=("pillow", "none"), help="mermaid 渲染失败降级策略")
    parser.add_argument("--force", action="store_true", help="忽略 manifest hash 重新渲染")
    parser.add_argument("--no-crop", action="store_true", help="关闭 mermaid PNG 自动裁切（默认裁切）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return render_resources(
        workspace=workspace,
        project_dir=project_dir,
        package_id=args.package,
        anchor=args.anchor,
        force=bool(args.force),
        browser=str(args.browser),
        fallback=str(args.fallback),
        kind_filter=args.kind,
        crop=not bool(getattr(args, "no_crop", False)),
    )


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _build_selftest_project(base: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    workspace = base / "ws"
    for rel_dir in ("dsh/logs", "dsh/config", "dsh/bidders"):
        (workspace / rel_dir).mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(
        workspace / "dsh" / "registry.json",
        {
            "schemaVersion": "1.0",
            "workspaceRootFingerprint": paths.workspace_fingerprint(workspace),
            "currentBidderId": "",
            "bidders": [],
            "projects": [],
            "updatedAt": paths.iso_now(),
        },
    )
    project_dir = workspace / f"渲染自检项目-{paths.timestamp14()}-m6rr"
    project_dir.mkdir()
    now = paths.iso_now()
    project_id = "prj-20261111100000-m6rr"
    project = schema.initialize_project_document(
        project_id=project_id,
        bidder_ref={"bidderId": "bdr-20261111100000-m6rr", "dirName": "示例", "displayName": "示例", "snapshotAt": now},
        workspace_fingerprint=paths.workspace_fingerprint(workspace),
        now_iso=now,
        project_name="M6 渲染自检项目",
    )
    project["projectNo"] = "M6-RENDER-001"
    project["isSubcontracted"] = False
    project["packageCount"] = 1
    project["packagesToWrite"] = ["package-1"]
    jsonio.write_json_atomic(project_dir / "project.json", project)
    state = schema.initialize_state_document(project_id=project_id, now_iso=now)
    state["currentStage"] = "draft"
    state["currentScope"] = "package-1"
    state["currentPackage"] = "package-1"
    state["revision"] = 6
    state["completedStages"] = [{"stage": "draft", "scope": "package-1", "completedAt": now, "note": ""}]
    state_lib.write_state(project_dir, state)
    package_dir = project_dir / "packages" / f"package-1-{paths.timestamp14()}"
    package_dir.mkdir(parents=True)
    for rel_dir in (
        "chapters",
        "chapters/_results",
        "chapters/_outline",
        "reviews",
        "reviews/rounds",
        "resources/mermaid",
        "resources/terminal",
        "export",
    ):
        (package_dir / rel_dir).mkdir(parents=True, exist_ok=True)
    package_meta = {
        "schemaVersion": "1.0",
        "packageId": "package-1",
        "packageNo": 1,
        "packageName": "M6 渲染自检项目",
        "expectedWordCount": 50000,
        "figureQuota": {"table": 1, "mermaid": 1, "network": 0, "terminal": 1},
    }
    jsonio.write_json_atomic(package_dir / "package-meta.json", package_meta)
    import bg_outline_apply as outline_apply

    outline = outline_apply.normalize_outline(
        copy.deepcopy(outline_validate._golden_document()),  # noqa: SLF001 - 自检复用 golden 夹具
        package_meta={"expectedWordCount": 50000, "packageId": "package-1"},
        project_name="M6 渲染自检项目",
        package_id="package-1",
    )
    # 覆盖为三种图表 + 一张 table 的小规模图
    leaves = [node for node in outline_validate.iter_preorder(outline["chapters"]) if not node.get("children")]
    leaf = leaves[0]
    leaf["figurePlan"] = [
        {"anchorId": "fig-1-1-01", "type": "mermaid", "purpose": "流程", "caption": "渲染流程", "status": "planned"},
        {"anchorId": "fig-1-1-02", "type": "terminal", "purpose": "命令", "caption": "巡检命令", "status": "planned"},
        {"anchorId": "fig-1-1-03", "type": "network", "purpose": "实景", "caption": "现场示意", "status": "planned"},
        {"anchorId": "fig-1-1-04", "type": "table", "purpose": "清单", "caption": "清单表", "status": "planned"},
        {"anchorId": "fig-1-1-05", "type": "mermaid", "purpose": "并发测试", "caption": "并发渲染流程", "status": "planned"},
    ]
    leaf["insertAnchors"] = [
        {"anchorId": "fig-1-1-01", "positionHint": "章节末尾", "type": "mermaid"},
        {"anchorId": "fig-1-1-02", "positionHint": "章节末尾", "type": "terminal"},
        {"anchorId": "fig-1-1-05", "positionHint": "章节末尾", "type": "mermaid"},
    ]
    for node in leaves:
        if node is not leaf:
            node["figurePlan"] = []
            node["insertAnchors"] = []
    outline["stats"]["figurePlanned"] = {"table": 1, "mermaid": 1, "network": 1, "terminal": 1}
    outline["stats"]["figureQuota"] = {"table": 1, "mermaid": 1, "network": 0, "terminal": 1}
    outline["figureIndex"] = outline_validate.build_figure_index(outline)
    outline_apply.materialize_tree(outline, package_dir)
    jsonio.write_json_atomic(outline_validate.outline_path(package_dir), outline)
    chapter_path = package_dir / leaf["filePath"]
    chapter_path.write_text(
        chapter_path.read_text(encoding="utf-8")
        + "\n<!-- BG:FIGURE:fig-1-1-01 -->\n\n```mermaid\nflowchart TD\n  A[开始] --> B{检查}\n  B -->|通过| C[结束]\n  B -->|失败| D[处理]\n  D --> C\n```\n\n"
        + "<!-- BG:FIGURE:fig-1-1-02 -->\n\n```text\n$ systemctl status nginx\n● nginx.service - A high performance web server\n     Active: active (running)\n$ df -h\n/dev/sda1  100G  42G  58G  42% /\n```\n\n"
        + "<!-- BG:FIGURE:fig-1-1-05 -->\n\n```mermaid\nflowchart LR\n  A[并发A] --> B[并发B]\n  B --> C[完成]\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    return workspace, project_dir, package_dir, outline


def run_selftest() -> dict[str, Any]:
    from PIL import Image

    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-render-selftest-"))
    try:
        workspace, project_dir, package_dir, outline = _build_selftest_project(tmp)
        first = render_resources(workspace=workspace, project_dir=project_dir, browser="auto")
        rendered = {item["anchorId"]: item for item in first["rendered"]}
        skipped = {item["anchorId"]: item for item in first["skipped"]}
        # P1-4：table 不写 BG:FIGURE，渲染不得因缺标记报 warning；table 应被跳过（无需 png）。
        table_warnings = [item for item in first["warnings"] if item.get("anchorId") == "fig-1-1-04"]
        if table_warnings:
            raise AssertionError(f"table 不应产生渲染 warning：{table_warnings}")
        if first.get("markerChecks", {}).get("tableSkipped") != 1:
            raise AssertionError(f"markerChecks 未统计 table 跳过：{first.get('markerChecks')}")
        if skipped.get("fig-1-1-04", {}).get("markerExpected") is not False:
            raise AssertionError(f"table 跳过原因未标注 markerExpected=false：{skipped.get('fig-1-1-04')}")
        # P1-4：本次渲染的私有临时目录（含浏览器 scoped_dir*/HeadlessChrome*）必须清理干净。
        if first.get("tempResidue"):
            raise AssertionError(f"渲染留下临时目录残留：{first['tempResidue']}")
        checks.append("table_no_marker_warning")
        checks.append("temp_cleanup")
        if "fig-1-1-01" not in rendered or rendered["fig-1-1-01"]["status"] not in ("produced", "fallback"):
            raise AssertionError(f"mermaid 未生成：{first['rendered']} {first['warnings']}")
        mermaid_png = project_dir / rendered["fig-1-1-01"]["resourcePath"]
        if not mermaid_png.is_file() or not validate_png(mermaid_png)["ok"]:
            raise AssertionError("mermaid png 无效")
        mermaid_mmd = project_dir / rendered["fig-1-1-01"]["sourcePath"]
        if not mermaid_mmd.is_file():
            raise AssertionError("mermaid .mmd 未落盘")
        if "fig-1-1-02" not in rendered or not (project_dir / rendered["fig-1-1-02"]["resourcePath"]).is_file():
            raise AssertionError("terminal 未生成")
        mermaid_info = rendered["fig-1-1-01"]
        mermaid_fallback = mermaid_info.get("status") == "fallback"
        if mermaid_fallback:
            # 浏览器属于可选组件：没有 Chrome/Edge，或 headless 截图不可用时，
            # mermaid 会降级为 Pillow 占位图（由 env-check 的 browser_renderer 单独报告）。
            # 必过项 scripts_integrity 不应因此失败。
            if mermaid_info.get("cropped") is not False:
                raise AssertionError(f"fallback 不应标记 cropped：{mermaid_info}")
            if not any(item.get("code") == "mermaid_fallback" for item in first["warnings"]):
                raise AssertionError(f"fallback 未产生 warning：{first['warnings']}")
        else:
            if not mermaid_info.get("cropped") or mermaid_info.get("originalWidth", 0) <= mermaid_info.get("width", 0):
                raise AssertionError(f"mermaid 未按内容裁切：{mermaid_info}")
            if mermaid_info.get("width", 0) > CROP_MAX_WIDTH or mermaid_info.get("height", 0) > CROP_MAX_HEIGHT:
                raise AssertionError(f"mermaid 裁切后超出最大宽高：{mermaid_info}")
            with Image.open(mermaid_png) as final_image:
                content_bbox = image_content_bbox(mermaid_png)
                if content_bbox is None:
                    raise AssertionError("裁切后无内容 bbox")
                left_margin, top_margin = content_bbox[0], content_bbox[1]
                right_margin = final_image.size[0] - content_bbox[2]
                bottom_margin = final_image.size[1] - content_bbox[3]
                if min(left_margin, top_margin, right_margin, bottom_margin) > CROP_PADDING + 2:
                    raise AssertionError(f"裁切 padding 过大：{left_margin}/{top_margin}/{right_margin}/{bottom_margin}")
        manifest = jsonio.read_json(package_dir / MANIFEST_FILE)
        anchor_meta = (manifest.get("anchors") or {}).get("fig-1-1-01") or {}
        if anchor_meta.get("originalWidth") != mermaid_info.get("originalWidth") or anchor_meta.get("width") != mermaid_info.get("width"):
            raise AssertionError(f"manifest 未记录原/裁尺寸：{anchor_meta}")
        if anchor_meta.get("dpi") != TARGET_DPI or anchor_meta.get("physicalWidthCm", 999) > MAX_PHYSICAL_WIDTH_CM or anchor_meta.get("physicalHeightCm", 999) > MAX_PHYSICAL_HEIGHT_CM:
            raise AssertionError(f"manifest DPI/物理尺寸不正确：{anchor_meta}")
        if anchor_meta.get("renderProfile", {}).get("version") != RENDER_PROFILE_VERSION:
            raise AssertionError(f"manifest 缺少 renderProfile 缓存版本：{anchor_meta.get('renderProfile')}")
        checks.append("mermaid_fallback" if mermaid_fallback else "crop")
        # R1：超高图按 A4 物理高度限高（120dpi 下 1166px），并写 DPI 120
        tall_path = tmp / "tall-mermaid.png"
        from PIL import ImageDraw as _ImageDraw

        tall = Image.new("RGB", (2400, 5000), (255, 255, 255))
        tall_draw = _ImageDraw.Draw(tall)
        tall_draw.rectangle([100, 100, 900, 4900], outline=(0, 0, 0), width=4)
        tall.save(tall_path)
        tall_info = crop_mermaid_png(tall_path)
        if tall_info["height"] > CROP_MAX_HEIGHT or tall_info["physicalHeightCm"] > MAX_PHYSICAL_HEIGHT_CM or not tall_info["resizedToMax"]:
            raise AssertionError(f"超高图未按物理高度限高：{tall_info}")
        with Image.open(tall_path) as tall_image:
            dpi = tall_image.info.get("dpi")
            if not isinstance(dpi, tuple) or round(float(dpi[0])) != TARGET_DPI:
                raise AssertionError(f"裁切后 PNG 未写 DPI {TARGET_DPI}：{dpi}")
        checks.append("dpi_physical_limit")
        terminal_meta = (jsonio.read_json(package_dir / MANIFEST_FILE).get("anchors") or {}).get("fig-1-1-02") or {}
        if terminal_meta.get("cropped") or terminal_meta.get("width") != rendered["fig-1-1-02"]["width"]:
            raise AssertionError(f"terminal 不应裁切：{terminal_meta}")
        terminal_txt = project_dir / rendered["fig-1-1-02"]["sourcePath"]
        if not terminal_txt.is_file() or "systemctl" not in terminal_txt.read_text(encoding="utf-8"):
            raise AssertionError("terminal .txt 未提取")
        if "fig-1-1-03" in rendered or skipped.get("fig-1-1-03", {}).get("status") != "network_disabled":
            raise AssertionError(f"network=0 应跳过：{first['skipped']}")
        checks.append("render")
        manifest_path = package_dir / MANIFEST_FILE
        if not manifest_path.is_file():
            raise AssertionError("render-manifest.json 未写")
        chapter_before = (package_dir / outline["chapters"][0]["children"][0]["filePath"]).read_text(encoding="utf-8")
        if "data:image" in chapter_before or "![" in chapter_before:
            raise AssertionError("正文 md 不应内嵌 base64/图片链接")
        checks.append("markers_preserved")
        second = render_resources(workspace=workspace, project_dir=project_dir, browser="auto")
        second_skipped = {item["anchorId"] for item in second["skipped"]}
        second_rendered = {item["anchorId"] for item in second["rendered"]}
        if mermaid_fallback:
            # fallback 图不做缓存命中（每次都重新降级），terminal 图仍按 hash 跳过
            if "fig-1-1-02" not in second_skipped or "fig-1-1-01" not in second_rendered:
                raise AssertionError(f"fallback 二次渲染不正确：{second['skipped']} {second['rendered']}")
        elif not {"fig-1-1-01", "fig-1-1-02"} <= second_skipped:
            raise AssertionError(f"二次渲染未按 hash 跳过：{second['skipped']} {second['rendered']}")
        checks.append("idempotent")
        fallback = render_resources(
            workspace=workspace, project_dir=project_dir, browser="none", fallback="pillow", force=True
        )
        fallback_item = next((item for item in fallback["rendered"] if item["anchorId"] == "fig-1-1-01"), None)
        if not fallback_item or fallback_item["status"] != "fallback" or not (project_dir / fallback_item["resourcePath"]).is_file():
            raise AssertionError(f"缺浏览器未降级 Pillow：{fallback}")
        if not any(item.get("code") == "mermaid_fallback" for item in fallback["warnings"]):
            raise AssertionError("降级未产生 warning")
        # R2：fallback 占位图不标记 cropped
        if fallback_item.get("cropped") is not False:
            raise AssertionError(f"fallback 不应标记 cropped：{fallback_item}")
        fallback_manifest = jsonio.read_json(package_dir / MANIFEST_FILE)
        fallback_meta = (fallback_manifest.get("anchors") or {}).get("fig-1-1-01") or {}
        if fallback_meta.get("cropped") is not False:
            raise AssertionError(f"fallback manifest 不应标记 cropped：{fallback_meta}")
        checks.append("fallback")
        single = render_resources(
            workspace=workspace, project_dir=project_dir, anchor="fig-1-1-02", force=True, browser="auto"
        )
        if [item["anchorId"] for item in single["rendered"]] != ["fig-1-1-02"]:
            raise AssertionError(f"--anchor 过滤不正确：{single['rendered']}")
        checks.append("anchor_filter")
        # 反向检查：P1-4 只对 table 豁免；mermaid 缺 BG:FIGURE 仍必须报 warning。
        marker_leaf = next(
            node
            for node in outline_validate.iter_preorder(outline.get("chapters") or [])
            if any(
                isinstance(plan, Mapping) and plan.get("anchorId") == "fig-1-1-01"
                for plan in node.get("figurePlan") or []
            )
        )
        marker_chapter = package_dir / str(marker_leaf.get("filePath"))
        original_markdown = marker_chapter.read_text(encoding="utf-8")
        marker_chapter.write_text(
            original_markdown.replace("<!-- BG:FIGURE:fig-1-1-01 -->", ""),
            encoding="utf-8",
            newline="\n",
        )
        try:
            missing_marker = render_resources(
                workspace=workspace, project_dir=project_dir, anchor="fig-1-1-01", force=True, browser="auto"
            )
        finally:
            marker_chapter.write_text(original_markdown, encoding="utf-8", newline="\n")
        if not any(
            item.get("code") == "figure_marker_missing" and item.get("anchorId") == "fig-1-1-01"
            for item in missing_marker["warnings"]
        ):
            raise AssertionError(f"mermaid 缺标记未报 warning：{missing_marker['warnings']}")
        checks.append("non_table_marker_required")
        return {"ok": True, "selftest": {"script": "bg_render.py", "checks": checks}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# copy 在函数内导入，避免顶部污染
import copy  # noqa: E402

def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_render.py",
    )


if __name__ == "__main__":
    sys.exit(main())
