#!/usr/bin/env python3
"""BidGenie F7.3 样式三层合并/校验（M8-P1）。

官方样式：`plugin/templates/styles/{tech,keyinfo,overview}/official-*.json`
用户样式：`dsh/styles/{tech,keyinfo,overview}/*.json`（自定义 profile）、
          `dsh/config/style-override.json`（用户覆盖，可含 profiles 或直接 params）
项目样式：`<projectDir>/style.json`、`<packageDir>/style.json`（可选，package 优先）

优先级（高→低）：用户自定义/覆盖 > 项目级 > 官方；深合并（对象递归、数组替换、标量覆盖）。
非法字段（字体/颜色/数值/枚举）回退低优先级并 warning，不中断；输出不含封面/TOC。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, paths, state as state_lib

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STYLE_ROOT = PLUGIN_ROOT / "templates" / "styles"
DOC_TYPES = ("tech", "keyinfo", "overview")
DOC_TYPE_ALIASES = {"tech": "tech", "keyinfo": "keyInfo", "keyInfo": "keyInfo", "overview": "overview"}
ALLOWED_FONTS = {"宋体", "黑体", "Arial", "Times New Roman", "Consolas"}
COLOR_KEYS = {"textColor", "headingColor", "captionColor", "tableColor", "color"}
ALIGN_VALUES = {"left", "center", "right", "justify"}
FONT_KEYS = {
    "fontFamily",
    "headingFontFamily",
    "fontLatin",
    "headingFontLatin",
    "captionFontFamily",
    "captionFontLatin",
    "tableFontFamily",
    "tableFontLatin",
}
SIZE_KEYS = {"fontSizePt", "tableFontSizePt"}
DEFAULT_STYLE = "official-tech"
RESOLVED_REL = "export/.resolved-style.json"


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        merged = {key: copy_value(value) for key, value in base.items()}
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value) if key in merged else copy_value(value)
        return merged
    return copy_value(override)


def copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_value(item) for item in value]
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_value(path: tuple[str, ...], value: Any) -> list[str]:
    """按字段路径校验单个值；未知路径不报（保留扩展字段）。"""
    key = path[-1]
    findings: list[str] = []
    if key in COLOR_KEYS or key.endswith("Color") or key == "color":
        if value != "auto":
            findings.append(f"颜色必须为 auto，实际 {value!r}")
        return findings
    if key in FONT_KEYS:
        if not isinstance(value, str) or value not in ALLOWED_FONTS:
            findings.append(f"字体不在白名单 {sorted(ALLOWED_FONTS)}：{value!r}")
        return findings
    if key in SIZE_KEYS:
        if not _is_number(value) or not (8 <= float(value) <= 30):
            findings.append(f"字号必须在 8–30pt：{value!r}")
        return findings
    if key == "lineSpacing":
        if not _is_number(value) or not (1.0 <= float(value) <= 3.0):
            findings.append(f"行距必须在 1.0–3.0：{value!r}")
        return findings
    if key == "firstLineIndentChars":
        if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= 5):
            findings.append(f"首行缩进字符数必须在 0–5：{value!r}")
    elif key == "bodyAlignment":
        if value not in ("justify", "left", "center", "right"):
            findings.append(f"正文对齐枚举非法：{value!r}")
    elif key == "align":
        if value not in ALIGN_VALUES:
            findings.append(f"对齐枚举非法：{value!r}")
    elif key == "enabled":
        if not isinstance(value, bool):
            findings.append(f"enabled 必须是布尔：{value!r}")
    elif key == "pageMarginMm":
        if not isinstance(value, Mapping):
            findings.append("pageMarginMm 必须是对象")
        else:
            for side in ("top", "bottom", "left", "right"):
                item = value.get(side)
                if item is not None and (not _is_number(item) or not (5 <= float(item) <= 50)):
                    findings.append(f"页边距 {side} 必须在 5–50mm：{item!r}")
    elif key == "headingSizesPt":
        if not isinstance(value, Mapping):
            findings.append("headingSizesPt 必须是对象")
        else:
            for level in range(1, 7):
                item = value.get(f"h{level}")
                if item is not None and (not _is_number(item) or not (8 <= float(item) <= 40)):
                    findings.append(f"标题字号 h{level} 必须在 8–40pt：{item!r}")
    elif key == "headingNumbering":
        if not isinstance(value, bool):
            findings.append(f"headingNumbering 必须是布尔：{value!r}")
    return findings


def sanitize_layer(merged: dict[str, Any], overlay: Mapping[str, Any], baseline: Mapping[str, Any], layer_name: str, warnings: list[str]) -> dict[str, Any]:
    """校验 overlay 中的字段；非法值回退 baseline/merged 低优先级值 + warning。"""
    for key, value in overlay.items():
        if key in ("cover", "toc"):
            warnings.append(f"[{layer_name}] 忽略 {key}（导出不生成封面/TOC）")
            continue
        if isinstance(value, Mapping):
            base_value = baseline.get(key) if isinstance(baseline.get(key), Mapping) else {}
            merged_value = merged.get(key) if isinstance(merged.get(key), Mapping) else {}
            merged[key] = sanitize_layer(dict(merged_value), value, base_value, f"{layer_name}.{key}", warnings)
            continue
        problems = validate_value((str(key),), value)
        if problems:
            fallback = baseline.get(key, merged.get(key))
            if fallback is not None:
                merged[key] = copy_value(fallback)
            else:
                merged.pop(key, None)
            for problem in problems:
                warnings.append(f"[{layer_name}] {key} 非法，已回退：{problem}")
        else:
            merged[key] = copy_value(value)
    return merged


# ---------------------------------------------------------------------------
# profile 发现/加载
# ---------------------------------------------------------------------------
def official_profiles() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for doc_dir in DOC_TYPES:
        directory = STYLE_ROOT / doc_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                document = jsonio.read_json(path)
            except errors.BgError:
                continue
            if not isinstance(document, Mapping) or not document.get("id"):
                continue
            result.append(
                {
                    "id": str(document.get("id")),
                    "docType": str(document.get("docType") or DOC_TYPE_ALIASES.get(doc_dir, doc_dir)),
                    "label": str(document.get("label") or document.get("name") or path.stem),
                    "layer": "official",
                    "path": paths.rel_posix(PLUGIN_ROOT, path) if path.is_relative_to(PLUGIN_ROOT) else str(path),
                    "absolutePath": str(path),
                }
            )
    return result


def custom_profiles(workspace: Path | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if workspace is None:
        return result
    for doc_dir in DOC_TYPES:
        directory = workspace / "dsh" / "styles" / doc_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                document = jsonio.read_json(path)
            except errors.BgError:
                result.append({"id": path.stem, "docType": DOC_TYPE_ALIASES.get(doc_dir, doc_dir), "layer": "user", "path": str(path), "error": "JSON 不可解析"})
                continue
            if not isinstance(document, Mapping) or not document.get("id"):
                continue
            result.append(
                {
                    "id": str(document.get("id")),
                    "docType": str(document.get("docType") or DOC_TYPE_ALIASES.get(doc_dir, doc_dir)),
                    "label": str(document.get("label") or document.get("name") or path.stem),
                    "layer": "user",
                    "path": str(path),
                    "absolutePath": str(path),
                    "basedOn": document.get("basedOn"),
                }
            )
    return result


def list_profiles(workspace: Path | None) -> list[dict[str, Any]]:
    profiles = official_profiles() + custom_profiles(workspace)
    deduped: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        key = profile["id"]
        if key not in deduped or profile["layer"] == "user":
            deduped[key] = profile
    profiles = sorted(deduped.values(), key=lambda item: (item["docType"], item["id"]))
    return profiles


def load_document(path: Path) -> dict[str, Any]:
    document = jsonio.read_json(path)
    if not isinstance(document, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"样式文件必须是 JSON 对象：{path}")
    return document


def find_profile(profile_id: str, workspace: Path | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    profiles = list_profiles(workspace)
    match = next((item for item in profiles if item["id"] == profile_id), None)
    if match is None:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"样式 profile 不存在：{profile_id}",
            hint="运行 --list-profiles 查看官方/自定义样式",
            detail={"profile": profile_id, "available": [item["id"] for item in profiles]},
        )
    document = load_document(Path(match.get("absolutePath") or match["path"]))
    return match, document


def resolve_project_style(
    workspace: Path | None, project_dir: Path | None, package_id: str | None
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """返回 (project_style, package_style, warnings)。"""
    project_style: dict[str, Any] = {}
    package_style: dict[str, Any] = {}
    warnings: list[str] = []
    if project_dir is None:
        return project_style, package_style, warnings
    project_file = project_dir / "style.json"
    if project_file.is_file():
        try:
            project_style = load_document(project_file)
        except errors.BgError as exc:
            warnings.append(f"项目 style.json 非法已忽略：{exc.message}")
    package_dir = None
    if package_id:
        candidate = next((path for path in (project_dir / "packages").glob(f"{package_id}-*") if path.is_dir()), None)
        if candidate is not None:
            package_dir = candidate / "style.json"
    if package_dir is None or not package_dir.is_file():
        latest = sorted((path for path in (project_dir / "packages").glob("package-*") if path.is_dir()), key=lambda item: item.name)
        if latest:
            candidate = latest[0] / "style.json"
            if candidate.is_file():
                package_dir = candidate
    if package_dir is not None and package_dir.is_file():
        try:
            package_style = load_document(package_dir)
        except errors.BgError as exc:
            warnings.append(f"包级 style.json 非法已忽略：{exc.message}")
    return project_style, package_style, warnings


def load_user_override(workspace: Path | None, profile_id: str, doc_type: str) -> dict[str, Any]:
    if workspace is None:
        return {}
    path = workspace / "dsh" / "config" / "style-override.json"
    if not path.is_file():
        return {}
    try:
        document = load_document(path)
    except errors.BgError:
        return {}
    profiles = document.get("profiles")
    if isinstance(profiles, Mapping) and isinstance(profiles.get(profile_id), Mapping):
        return dict(profiles[profile_id])
    doc_overrides = document.get("docTypes")
    if isinstance(doc_overrides, Mapping) and isinstance(doc_overrides.get(doc_type), Mapping):
        return dict(doc_overrides[doc_type])
    if isinstance(document.get("params"), Mapping) or isinstance(document.get("id"), str):
        return document
    return {}


# ---------------------------------------------------------------------------
# 校验/合并
# ---------------------------------------------------------------------------
def validate_document(document: Mapping[str, Any], *, expected_doc_type: str | None = None) -> dict[str, Any]:
    errors_list: list[str] = []
    warnings: list[str] = []
    if document.get("schemaVersion") != "1.0":
        errors_list.append(f"schemaVersion 必须为 1.0：{document.get('schemaVersion')!r}")
    profile_id = document.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        errors_list.append("id 必须是非空字符串")
    doc_type = str(document.get("docType") or "")
    if doc_type not in ("tech", "keyInfo", "overview"):
        errors_list.append(f"docType 非法：{doc_type!r}")
    if expected_doc_type and doc_type and DOC_TYPE_ALIASES.get(expected_doc_type, expected_doc_type) != doc_type:
        warnings.append(f"profile docType={doc_type} 与目录 {expected_doc_type} 不一致")
    params = document.get("params")
    if not isinstance(params, Mapping):
        errors_list.append("params 必须是对象")
        return {"ok": False, "errors": errors_list, "warnings": warnings}
    for key in FONT_KEYS:
        if key in params:
            errors_list.extend(validate_value((key,), params[key]))
    for key in ("textColor", "headingColor", "captionColor", "tableColor"):
        if key in params:
            errors_list.extend(validate_value((key,), params[key]))
    for key in ("fontSizePt", "tableFontSizePt", "lineSpacing", "firstLineIndentChars", "pageMarginMm", "headingSizesPt", "bodyAlignment", "headingNumbering"):
        if key in params:
            errors_list.extend(validate_value((key,), params[key]))
    for nested in ("title", "table", "captions"):
        if isinstance(params.get(nested), Mapping):
            for key, value in params[nested].items():  # type: ignore[union-attr]
                if key in FONT_KEYS or key in COLOR_KEYS or key in SIZE_KEYS or key in ("align", "enabled"):
                    errors_list.extend(validate_value((nested, str(key)), value))
    if isinstance(params.get("cover"), Mapping) and params["cover"].get("enabled") not in (False, None):
        warnings.append("cover.enabled 将被强制 false（不生成封面）")
    if isinstance(params.get("toc"), Mapping) and params["toc"].get("enabled") not in (False, None):
        warnings.append("toc.enabled 将被强制 false（不生成目录）")
    return {"ok": not errors_list, "errors": errors_list, "warnings": warnings}


def merge_profile(
    profile_id: str,
    *,
    workspace: Path | None,
    project_dir: Path | None = None,
    package_id: str | None = None,
) -> dict[str, Any]:
    profile, official = find_profile(profile_id, workspace)
    custom_document: dict[str, Any] = {}
    base_profile = profile
    base_document = official
    if profile.get("layer") == "user":
        custom_document = official
        based_on = custom_document.get("basedOn")
        if isinstance(based_on, str) and based_on and based_on != profile_id:
            base_profile, base_document = find_profile(based_on, workspace)
    doc_type = str(base_document.get("docType") or custom_document.get("docType") or "")
    warnings: list[str] = []
    base_validation = validate_document(base_document, expected_doc_type=doc_type)
    if not base_validation["ok"]:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"官方样式 {profile_id} 非法",
            detail={"errors": base_validation["errors"]},
        )
    project_style, package_style, style_warnings = resolve_project_style(workspace, project_dir, package_id)
    warnings.extend(style_warnings)
    user_override = load_user_override(workspace, profile_id, doc_type)
    user_custom = custom_document if profile["layer"] == "user" else {}
    merged = copy_value(base_document)
    # 官方 baseline；项目层级：project < package；用户层：config override < dsh styles custom
    for layer_name, override in (
        ("project", project_style),
        ("package", package_style),
        ("userOverride", user_override),
        ("userCustom", user_custom),
    ):
        if not isinstance(override, Mapping) or not override:
            continue
        if layer_name == "userCustom":
            override = {
                key: value
                for key, value in override.items()
                if key
                not in (
                    "id",
                    "docType",
                    "schemaVersion",
                    "name",
                    "label",
                    "description",
                    "basedOn",
                    "createdAt",
                    "updatedAt",
                    "resolvedFrom",
                )
            }
        baseline = copy_value(merged)
        merged = sanitize_layer(merged, override, baseline, layer_name, warnings)
    # 输出不含封面/TOC
    merged.pop("cover", None)
    merged.pop("toc", None)
    params = merged.get("params")
    if isinstance(params, dict):
        removed = [key for key in ("cover", "toc") if key in params]
        for key in removed:
            params.pop(key, None)
        if removed:
            warnings.append(f"输出已移除 cover/toc：{removed}")
        for nested in ("title", "table", "captions"):
            value = params.get(nested)
            if isinstance(value, dict):
                params[nested] = {key: item for key, item in value.items() if key not in ("cover", "toc")}
    merged["id"] = profile_id
    merged["docType"] = str(custom_document.get("docType") or doc_type)
    merged["resolvedFrom"] = {
        "profile": profile_id,
        "official": base_profile.get("path"),
        "project": bool(project_style),
        "package": bool(package_style),
        "userOverride": bool(user_override),
        "userCustom": profile.get("layer") == "user",
    }
    validation = validate_document(merged, expected_doc_type=doc_type)
    warnings.extend(validation.get("warnings") or [])
    return {"document": merged, "warnings": warnings, "profile": profile, "validation": validation}


def resolve_output_path(
    workspace: Path | None, project_dir: Path | None, package_id: str | None, out: str | None
) -> Path:
    if out:
        path = Path(out).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path.suffix.lower() != ".json":
            raise errors.BgError(errors.E_USAGE, "--out 必须是 .json 路径", detail={"out": str(path)})
        return path
    if project_dir is None:
        raise errors.BgError(errors.E_USAGE, "缺少 --project，无法确定默认输出 export/.resolved-style.json")
    package_dir = None
    if package_id:
        package_dir = next((path for path in (project_dir / "packages").glob(f"{package_id}-*") if path.is_dir()), None)
    if package_dir is None:
        candidates = sorted((path for path in (project_dir / "packages").glob("package-*") if path.is_dir()), key=lambda item: item.name)
        package_dir = candidates[0] if candidates else project_dir
    return package_dir / RESOLVED_REL


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def command_list(workspace: Path | None) -> dict[str, Any]:
    profiles = list_profiles(workspace)
    return {
        "ok": True,
        "command": "style_merge_list",
        "count": len(profiles),
        "profiles": profiles,
    }


def command_validate(workspace: Path | None, project_dir: Path | None, package_id: str | None, profile_id: str) -> dict[str, Any]:
    result = merge_profile(profile_id, workspace=workspace, project_dir=project_dir, package_id=package_id)
    validation = result["validation"]
    return {
        "ok": True,
        "command": "style_merge_validate",
        "profile": profile_id,
        "valid": bool(validation.get("ok")),
        "errors": validation.get("errors") or [],
        "warnings": result["warnings"],
        "resolvedFrom": result["document"].get("resolvedFrom"),
    }


def command_resolve(
    workspace: Path | None, project_dir: Path | None, package_id: str | None, profile_id: str, out: str | None
) -> dict[str, Any]:
    result = merge_profile(profile_id, workspace=workspace, project_dir=project_dir, package_id=package_id)
    path = resolve_output_path(workspace, project_dir, package_id, out)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(path, result["document"])
    effective = path
    if project_dir is not None and path.is_relative_to(project_dir):
        effective = Path(paths.rel_posix(project_dir, path))
    return {
        "ok": True,
        "command": "style_merge_resolve",
        "profile": profile_id,
        "out": str(effective),
        "absolutePath": str(path),
        "valid": bool(result["validation"].get("ok")),
        "warnings": result["warnings"],
        "resolvedFrom": result["document"].get("resolvedFrom"),
        "params": {
            "fontFamily": (result["document"].get("params") or {}).get("fontFamily"),
            "headingFontFamily": (result["document"].get("params") or {}).get("headingFontFamily"),
            "colors": {
                key: (result["document"].get("params") or {}).get(key)
                for key in ("textColor", "headingColor", "captionColor", "tableColor")
            },
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    base = {"a": {"b": 1, "c": [1, 2]}, "x": "old"}
    merged = deep_merge(base, {"a": {"c": [3]}, "x": "new"})
    if merged != {"a": {"b": 1, "c": [3]}, "x": "new"}:
        raise AssertionError(f"deep_merge 错误：{merged}")
    checks = ["deep_merge"]
    merged_values = sanitize_layer(
        {"fontFamily": "宋体", "textColor": "auto", "fontSizePt": 12},
        {"fontFamily": "Comic Sans", "textColor": "000000", "fontSizePt": 99},
        {"fontFamily": "宋体", "textColor": "auto", "fontSizePt": 12},
        "user",
        [],
    )
    if merged_values["fontFamily"] != "宋体" or merged_values["textColor"] != "auto" or merged_values["fontSizePt"] != 12:
        raise AssertionError(f"非法回退错误：{merged_values}")
    checks.append("fallback")
    if validate_document({"schemaVersion": "1.0", "id": "x", "docType": "tech", "params": {}})["ok"] is not True:
        raise AssertionError("合法样式被误判")
    checks.append("validate")
    return {"ok": True, "selftest": {"script": "bg_style_merge.py", "checks": checks}}


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_style_merge.py",
        description="BidGenie F7.3 样式三层合并/校验（official < project/package < user；字体/颜色白名单）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>")
    parser.add_argument("--list-profiles", action="store_true", help="列出官方/自定义样式 profile")
    parser.add_argument("--validate", action="store_true", help="校验合并结果")
    parser.add_argument("--resolve", action="store_true", help="三层合并并输出 resolved style")
    parser.add_argument("--profile", help="样式 profile id（如 official-tech）")
    parser.add_argument("--out", help="resolved style 输出路径（默认 export/.resolved-style.json）")
    parser.add_argument("--selftest", action="store_true", help="脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if args.selftest:
        return run_selftest()
    workspace: Path | None = None
    project_dir: Path | None = None
    if args.project:
        workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    elif args.workspace:
        workspace = paths.normalize_workspace(args.workspace)
    if args.list_profiles:
        return command_list(workspace)
    if not args.profile:
        raise errors.BgError(errors.E_USAGE, "缺少 --profile", hint="--profile official-tech / --list-profiles")
    if args.validate:
        return command_validate(workspace, project_dir, args.package, args.profile)
    if args.resolve:
        return command_resolve(workspace, project_dir, args.package, args.profile, args.out)
    raise errors.BgError(errors.E_USAGE, "缺少操作：--list-profiles / --validate / --resolve", hint="使用 --help 查看用法")


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_style_merge.py",
    )


if __name__ == "__main__":
    sys.exit(main())
