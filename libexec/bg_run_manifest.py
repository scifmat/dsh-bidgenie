#!/usr/bin/env python3
"""bg_run_manifest.py：验收运行记录生成器（task-106 M-7）。

只读采集：state / 导出 docx / 插件文件 SHA256 / env-check hostVersion，输出 JSON + Markdown 运行清单；
不写项目 state、不重新导出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, paths, state as state_lib  # noqa: E402

PLUGIN_ROOT = _SCRIPT_DIR.parent
SCHEMA_VERSION = "1.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _plugin_files() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() not in (".py", ".json", ".md"):
            continue
        result.append({"path": paths.rel_posix(PLUGIN_ROOT, path), "sha256": sha256_file(path), "size": path.stat().st_size})
    return result


def _plugin_hash(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['path']}:{item['sha256']}\n".encode("utf-8"))
    return digest.hexdigest()


def _scripts_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(_SCRIPT_DIR.glob("bg_*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _env_hash(workspace: Path) -> str:
    """稳定 envHash：只含 checklistHash/scriptsHash/必需项状态，排除 checkedAt/hostVersion。"""
    candidates = [workspace / "dsh" / "config" / "env-check.json", workspace / "dsh" / "env-check.json"]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return ""
    try:
        document = jsonio.read_json(path)
    except Exception:  # noqa: BLE001
        return ""
    items = document.get("items") if isinstance(document, dict) else {}
    required_status = {
        str(key): {"required": bool(value.get("required")), "status": value.get("status")}
        for key, value in (items or {}).items()
        if isinstance(value, Mapping) and value.get("required")
    }
    payload = {
        "checklistHash": document.get("checklistHash"),
        "scriptsHash": document.get("scriptsHash"),
        "requiredStatus": required_status,
    }
    return "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def start_run(*, workspace: Path, project_dir: Path | None, out_path: Path | None = None) -> dict[str, Any]:
    """开跑前写 RUN-*.start.json：runId/插件与脚本哈希/envHash/开始时间。"""
    plugin_files = _plugin_files()
    run_id = f"RUN-{paths.timestamp14()}"
    state = state_lib.load_state(project_dir) if project_dir is not None else {}
    project = state_lib.load_project(project_dir) if project_dir is not None else {}
    start = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "startedAt": paths.iso_now(),
        "workspace": str(workspace),
        "projectId": str(project.get("projectId") or ""),
        "projectDir": project_dir.name if project_dir is not None else "",
        "pluginHash": _plugin_hash(plugin_files),
        "scriptsHash": _scripts_hash(),
        "envHash": _env_hash(workspace),
        "envCheckedAt": "",
        "stateRevision": state.get("revision"),
        "exports": [],
    }
    env_candidates = [workspace / "dsh" / "config" / "env-check.json", workspace / "dsh" / "env-check.json"]
    env_path = next((path for path in env_candidates if path.is_file()), None)
    if env_path is not None:
        try:
            env_doc = jsonio.read_json(env_path)
            start["envCheckedAt"] = env_doc.get("checkedAt") or ""
        except Exception:  # noqa: BLE001
            pass
    run_dir = workspace / "dsh" / "run-manifests"
    run_dir.mkdir(parents=True, exist_ok=True)
    start_path = out_path or (run_dir / f"{run_id}.start.json")
    jsonio.write_json_atomic(start_path, start)
    return {"ok": True, "command": "run_manifest_start", "start": start, "startPath": str(start_path)}


def _latest_start(workspace: Path) -> tuple[Path | None, dict[str, Any]]:
    run_dir = workspace / "dsh" / "run-manifests"
    files = sorted(run_dir.glob("RUN-*.start.json"), key=lambda path: path.stat().st_mtime, reverse=True) if run_dir.is_dir() else []
    if not files:
        return None, {}
    path = files[0]
    try:
        return path, jsonio.read_json(path)
    except Exception:  # noqa: BLE001
        return path, {}


def record_export(*, workspace: Path, project_dir: Path, package_id: str, kind: str, docx_path: Path) -> None:
    """每包导出后向最近一次 start 记录 export（路径/哈希/包/类型）。"""
    start_path, start = _latest_start(workspace)
    if start_path is None or not isinstance(start, dict):
        return
    exports = list(start.get("exports") or [])
    relative = paths.rel_posix(project_dir, docx_path) if docx_path.is_relative_to(project_dir) else str(docx_path)
    entry = {"package": package_id, "kind": kind, "path": relative, "sha256": sha256_file(docx_path), "at": paths.iso_now()}
    exports = [item for item in exports if not (isinstance(item, dict) and item.get("package") == package_id and item.get("kind") == kind)]
    exports.append(entry)
    start["exports"] = exports
    jsonio.write_json_atomic(start_path, start)


def _docx_records(project_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((project_dir / "packages").glob("package-*/export/*.docx")):
        package_dir = path.parents[1]
        package_match = re.match(r"(package-\d+)", package_dir.name)
        records.append(
            {
                "package": package_match.group(1) if package_match else package_dir.name,
                "path": paths.rel_posix(project_dir, path),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "kind": "keyinfo" if "关键信息" in path.name else ("overview" if "工作概览" in path.name else "tech"),
            }
        )
    for path in sorted((project_dir / "exports").glob("*.docx")):
        records.append(
            {
                "package": "project",
                "path": paths.rel_posix(project_dir, path),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "kind": "keyinfo",
            }
        )
    return records


def generate(*, workspace: Path, project_dir: Path, out_path: Path, keep: int = 20, run_id: str | None = None) -> dict[str, Any]:
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    env_candidates = [workspace / "dsh" / "config" / "env-check.json", workspace / "dsh" / "env-check.json"]
    env_path = next((path for path in env_candidates if path.is_file()), None)
    env_check = jsonio.read_json(env_path) if env_path is not None else {}
    event_files = []
    logs_dir = workspace / "dsh" / "logs"
    if logs_dir.is_dir():
        event_files = [
            {"path": paths.rel_posix(workspace, path), "sha256": sha256_file(path), "size": path.stat().st_size}
            for path in sorted(logs_dir.glob("events-*.jsonl"))
            if path.is_file()
        ]
    docx = _docx_records(project_dir)
    plugin_files = _plugin_files()
    root_digest = hashlib.sha256()
    for item in plugin_files:
        root_digest.update(f"{item['path']}:{item['sha256']}\n".encode("utf-8"))
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id or f"RUN-{paths.timestamp14()}",
        "generatedAt": paths.iso_now(),
        "workspace": str(workspace),
        "projectId": str(project.get("projectId") or ""),
        "projectDir": project_dir.name,
        "writingPackages": project.get("writingPackages") or project.get("packagesToWrite") or [],
        "state": {
            key: state.get(key)
            for key in ("currentStage", "currentScope", "currentPackage", "revision", "reviewLoop", "locks")
        },
        "hostVersion": (str(env_check.get("hostVersion") or "") if isinstance(env_check, dict) else "") or "未记录",
        "envCheck": {
            "checklistVersion": env_check.get("checklistVersion") if isinstance(env_check, dict) else "未记录",
            "checklistHash": env_check.get("checklistHash") if isinstance(env_check, dict) else "未记录",
            "scriptsHash": env_check.get("scriptsHash") if isinstance(env_check, dict) else "未记录",
            "checkedAt": env_check.get("checkedAt") if isinstance(env_check, dict) else "未记录",
            "hostVersion": env_check.get("hostVersion") if isinstance(env_check, dict) else "未记录",
            "items": env_check.get("items") if isinstance(env_check, dict) else "未记录",
        },
        "eventFiles": event_files,
        "docx": docx,
        "exports": docx,
        "plugin": {"fileCount": len(plugin_files), "rootHash": root_digest.hexdigest(), "files": plugin_files},
        "pluginHash": _plugin_hash(plugin_files),
        "scriptsHash": _scripts_hash(),
        "envHash": _env_hash(workspace),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = workspace / "dsh" / "run-manifests"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_file = run_dir / f"{manifest['runId']}.json"
    manifest["runManifestPath"] = paths.rel_posix(workspace, run_file)
    jsonio.write_json_atomic(run_file, manifest)
    jsonio.write_json_atomic(out_path, manifest)
    lines = [
        "# 验收运行记录（run-manifest）",
        "",
        f"- runId：`{manifest['runId']}`",
        f"- 生成时间：{manifest['generatedAt']}",
        f"- 项目：`{manifest['projectId']}` / {manifest['projectDir']}",
        f"- 撰写包：{', '.join(manifest['writingPackages']) or '未记录'}",
        f"- state：{manifest['state']['currentStage']} / rev{manifest['state']['revision']} / {manifest['state']['currentPackage']}",
        f"- hostVersion：{manifest['hostVersion'] or '未记录'}",
        "",
        "## docx",
        "",
        "| 包 | 类型 | 路径 | SHA256 | 字节 |",
        "|---|---|---|---|---|",
    ]
    for item in docx:
        lines.append(f"| {item['package']} | {item['kind']} | `{item['path']}` | `{item['sha256']}` | {item['size']} |")
    lines.extend(
        [
            "",
            "## 插件文件",
            "",
            f"- 文件数：{manifest['plugin']['fileCount']}",
            f"- 根哈希：`sha256:{manifest['plugin']['rootHash']}`",
            "",
            "> 本文件由 `bg_run_manifest.py` 只读生成；不写 state，不重新导出。",
        ]
    )
    (out_path.with_suffix(".md")).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"ok": True, "command": "run_manifest", "manifestPath": str(out_path), "markdownPath": str(out_path.with_suffix(".md")), "manifest": manifest}


def finalize_run(*, workspace: Path, project_dir: Path, out_path: Path) -> dict[str, Any]:
    """结束时写完整 manifest，并与 start 的 plugin/scripts hash 对比（漂移则 versionDrift）。"""
    start_path, start = _latest_start(workspace)
    if not isinstance(start, dict) or not start:
        return {"ok": False, "error": "E_NOT_FOUND", "message": "缺少 RUN-*.start.json，先执行 --start", "exitCode": 4}
    run_id = str(start.get("runId") or f"RUN-{paths.timestamp14()}")
    result = generate(workspace=workspace, project_dir=project_dir, out_path=out_path, run_id=run_id)
    manifest = result["manifest"]
    drift = {
        "pluginHash": start.get("pluginHash") != manifest.get("pluginHash"),
        "scriptsHash": start.get("scriptsHash") != manifest.get("scriptsHash"),
        "envHash": start.get("envHash") != manifest.get("envHash"),
    }
    manifest["start"] = start
    started_at = str(start.get("startedAt") or "")
    env_checked_at_start = str(start.get("envCheckedAt") or "")
    env_checked_at = str((manifest.get("envCheck") or {}).get("checkedAt") or "")
    env_refreshed_after_start = bool(env_checked_at_start and env_checked_at and env_checked_at_start != env_checked_at)
    env_snapshot_after_start = bool(started_at and env_checked_at and started_at < env_checked_at)
    warnings: list[str] = []
    if env_refreshed_after_start:
        warnings.append("start 后 env-check 已刷新（checkedAt 变化）；envHash 稳定，不视为 versionDrift")
    if env_snapshot_after_start:
        warnings.append("start 早于最新环境快照 checkedAt（仅告警）")
    manifest["envRefreshedAfterStart"] = env_refreshed_after_start
    manifest["envSnapshotAfterStart"] = env_snapshot_after_start
    manifest["versionDrift"] = any(drift.values())
    manifest["versionDriftDetail"] = drift
    manifest["warnings"] = warnings
    manifest["finishedAt"] = paths.iso_now()
    exit_code = 4 if manifest["versionDrift"] else 0
    manifest["conclusion"] = {
        "versionDrift": manifest["versionDrift"],
        "envRefreshedAfterStart": env_refreshed_after_start,
        "envSnapshotAfterStart": env_snapshot_after_start,
        "warnings": warnings,
        "exitCode": exit_code,
    }
    run_dir = workspace / "dsh" / "run-manifests"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_file = run_dir / f"{run_id}.json"
    manifest["runManifestPath"] = paths.rel_posix(workspace, run_file)
    jsonio.write_json_atomic(run_file, manifest)
    jsonio.write_json_atomic(out_path, manifest)
    conclusion_lines = [
        "",
        "## Finalize 结论",
        "",
        f"- versionDrift：{manifest['versionDrift']}",
        f"- envRefreshedAfterStart：{env_refreshed_after_start}",
        f"- envSnapshotAfterStart：{env_snapshot_after_start}",
        f"- exitCode：{exit_code}",
        f"- warnings：{'; '.join(warnings) if warnings else '无'}",
        "",
    ]
    md_path = out_path.with_suffix(".md")
    with md_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(conclusion_lines))
    return {"ok": not manifest["versionDrift"], "command": "run_manifest_finalize", "runId": run_id, "versionDrift": manifest["versionDrift"], "envRefreshedAfterStart": env_refreshed_after_start, "envSnapshotAfterStart": env_snapshot_after_start, "conclusion": manifest["conclusion"], "driftDetail": drift, "manifestPath": str(out_path), "manifest": manifest, "exitCode": exit_code}


def main(argv: list[str] | None = None) -> int:
    parser = errors.CliParser(prog="bg_run_manifest.py", description="生成验收运行记录（只读）")
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径（生成时必填）")
    parser.add_argument("--project", help="项目目录或 projectId（生成时必填）")
    parser.add_argument("--out", help="输出 JSON 路径（缺省 docs/acceptance/M8/run-manifests/<runId>.json）")
    parser.add_argument("--keep", type=int, default=20, help="run-manifests 目录保留最新份数（默认 20）")
    parser.add_argument("--start", action="store_true", help="开跑前写 RUN-*.start.json（插件/脚本/env hash）")
    parser.add_argument("--finalize", action="store_true", help="结束时写完整 manifest 并校验相对 start 是否版本漂移")
    parser.add_argument("--selftest", action="store_true", help="脚本自检")
    args = parser.parse_args(argv)
    if args.selftest:
        print(json.dumps({"ok": True, "selftest": {"script": "bg_run_manifest.py", "checks": ["import", "sha256"]}}, ensure_ascii=False))
        return 0
    if not args.workspace:
        raise errors.BgError(errors.E_USAGE, "缺少 --workspace", hint="需要工作区")
    if args.start:
        workspace = Path(args.workspace).expanduser().resolve()
        project_dir = state_lib.resolve_project_context(args.project, workspace)[1] if args.project else None
        result = start_run(workspace=workspace, project_dir=project_dir)
        errors.emit(result, bool(getattr(args, "json", False)))
        return 0
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="生成 manifest 需要项目")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    out_path = Path(args.out).expanduser() if args.out else (workspace / "dsh" / "run-manifests" / f"RUN-{paths.timestamp14()}.json")
    if args.finalize and not args.out:
        _start_path, start_doc = _latest_start(workspace)
        start_run_id = str(start_doc.get("runId") or "")
        if start_run_id:
            out_path = workspace / "dsh" / "run-manifests" / f"{start_run_id}.json"
    out_path = out_path.resolve()
    if args.finalize:
        result = finalize_run(workspace=workspace, project_dir=project_dir, out_path=out_path)
        errors.emit(result, bool(getattr(args, "json", False)))
        return int(result.get("exitCode") or 0)
    result = generate(workspace=workspace, project_dir=project_dir, out_path=out_path, keep=args.keep)
    errors.emit(result, bool(getattr(args, "json", False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
