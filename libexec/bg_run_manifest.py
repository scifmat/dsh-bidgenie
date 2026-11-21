#!/usr/bin/env python3
"""bg_run_manifest.py：验收运行记录生成器（task-106 M-7 / P1-6 生命周期修复）。

只读采集：state / 导出 docx / 插件文件 SHA256 / env-check hostVersion，输出 JSON + Markdown 运行清单；
不写项目 state、不重新导出。

生命周期（P1-6：单次运行只有一个 runId）：
- `--start` 写 `dsh/run-manifests/<runId>.start.json`，并原子维护 `_latest.json`（kind=start）；
- 记录导出（不带 `--start/--finalize`）**复用最近一次 start 的 runId**，产物命名
  `<runId>.snapshot-<ts14>.json/.md`，不另造 runId、不覆盖 `<runId>.json`；
- `--finalize` 仍写 `<runId>.json/.md`，并把 `_latest.json` 更新为 kind=finalize；
- `_latest.json` 为实体文件（runId/kind/updatedAt/产物路径），历史遗留的无实体 `_latest.json.lock` 在下次写入前清理。
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

from _lib import errors, jsonio, paths, schema, state as state_lib  # noqa: E402

PLUGIN_ROOT = _SCRIPT_DIR.parent
SCHEMA_VERSION = "1.0"
LATEST_FILE_NAME = "_latest.json"
#: 记录导出（非 start/finalize）产物的 runId 前缀外的快照后缀
SNAPSHOT_SUFFIX = "snapshot"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _rel(workspace: Path, target: Path | None) -> str:
    """workspace 相对 POSIX 路径；不在 workspace 下时退回绝对字符串。"""
    if target is None:
        return ""
    try:
        return paths.rel_posix(workspace, target)
    except ValueError:
        return str(target)


def run_manifests_dir(workspace: Path) -> Path:
    """`dsh/run-manifests` 目录（不存在则创建）。"""
    run_dir = workspace / "dsh" / "run-manifests"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def latest_pointer_path(workspace: Path) -> Path:
    return run_manifests_dir(workspace) / LATEST_FILE_NAME


def clean_stale_latest_lock(workspace: Path) -> bool:
    """清理历史遗留 `_latest.json.lock`（仅在 `_latest.json` 实体不存在时）；返回是否删除。"""
    run_dir = run_manifests_dir(workspace)
    entity = run_dir / LATEST_FILE_NAME
    lock = entity.with_name(entity.name + ".lock")
    if entity.exists() or not lock.exists():
        return False
    try:
        lock.unlink()
        return True
    except OSError:
        return False


def read_latest_pointer(workspace: Path) -> dict[str, Any]:
    """读取 `_latest.json`（不存在/非法时返回空对象）。"""
    target = latest_pointer_path(workspace)
    if not target.is_file():
        return {}
    try:
        document = jsonio.read_json(target)
    except Exception:  # noqa: BLE001 - 指针只用于查询，损坏不阻断
        return {}
    return document if isinstance(document, dict) else {}


def write_latest_pointer(
    workspace: Path,
    *,
    run_id: str,
    kind: str,
    start_path: Path | None = None,
    manifest_path: Path | None = None,
    markdown_path: Path | None = None,
    snapshot_path: Path | None = None,
    project_id: str = "",
    project_dir: str = "",
) -> dict[str, Any]:
    """原子维护 `<workspace>/dsh/run-manifests/_latest.json`（指向最新 runId/时间/产物）。"""
    stale_lock_removed = clean_stale_latest_lock(workspace)
    target = latest_pointer_path(workspace)
    pointer: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "kind": kind,
        "updatedAt": paths.iso_now(),
        "startPath": _rel(workspace, start_path),
        "manifestPath": _rel(workspace, manifest_path),
        "markdownPath": _rel(workspace, markdown_path),
        "snapshotPath": _rel(workspace, snapshot_path),
        "projectId": project_id,
        "projectDir": project_dir,
    }
    jsonio.write_json_atomic(target, pointer)
    pointer["path"] = _rel(workspace, target)
    pointer["staleLockRemoved"] = stale_lock_removed
    return pointer


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
    run_dir = run_manifests_dir(workspace)
    start_path = out_path or (run_dir / f"{run_id}.start.json")
    jsonio.write_json_atomic(start_path, start)
    latest = write_latest_pointer(
        workspace,
        run_id=run_id,
        kind="start",
        start_path=start_path,
        project_id=str(start.get("projectId") or ""),
        project_dir=str(start.get("projectDir") or ""),
    )
    return {
        "ok": True,
        "command": "run_manifest_start",
        "runId": run_id,
        "start": start,
        "startPath": str(start_path),
        "latest": latest,
        "latestPath": latest.get("path", ""),
        "staleLockRemoved": bool(latest.get("staleLockRemoved")),
    }


def _latest_start(workspace: Path, project_dir: Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    """取最近一次 start；给定 project_dir 时按 projectId/目录名过滤，避免多项目工作区串号。"""
    run_dir = workspace / "dsh" / "run-manifests"
    files = sorted(run_dir.glob("RUN-*.start.json"), key=lambda path: path.stat().st_mtime, reverse=True) if run_dir.is_dir() else []
    if not files:
        return None, {}
    if project_dir is not None:
        target_id = ""
        target_name = Path(project_dir).name
        try:
            target_id = str(state_lib.load_project(project_dir).get("projectId") or "")
        except Exception:  # noqa: BLE001 - 项目文档缺失时只按目录名匹配
            target_id = ""
        for path in files:
            try:
                document = jsonio.read_json(path)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(document, Mapping):
                continue
            start_id = str(document.get("projectId") or "")
            start_dir = str(document.get("projectDir") or "")
            if (target_id and start_id == target_id) or (target_name and start_dir == target_name):
                return path, document
        # 兼容旧用法：--start 未带 --project（start 无 projectId/projectDir）且工作区只有这一份 start，
        # 视为当前项目的 start（多项目工作区不猜，避免串号）。
        if len(files) == 1:
            try:
                document = jsonio.read_json(files[0])
            except Exception:  # noqa: BLE001
                document = {}
            if isinstance(document, Mapping) and not str(document.get("projectId") or "") and not str(document.get("projectDir") or ""):
                return files[0], document
        return None, {}
    path = files[0]
    try:
        return path, jsonio.read_json(path)
    except Exception:  # noqa: BLE001
        return path, {}


def record_export(*, workspace: Path, project_dir: Path, package_id: str, kind: str, docx_path: Path) -> None:
    """每包导出后向最近一次 start 记录 export（路径/哈希/包/类型）。"""
    start_path, start = _latest_start(workspace, project_dir)
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


def generate(
    *,
    workspace: Path,
    project_dir: Path,
    out_path: Path,
    keep: int = 20,
    run_id: str | None = None,
    snapshot_of: Path | None = None,
    run_file: Path | None = None,
) -> dict[str, Any]:
    """生成运行清单（runId 可复用 start；`snapshot_of` 非空时按 `<runId>.snapshot-<ts>` 归档）。"""
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
    if snapshot_of is not None:
        recorded_exports: list[Any] = []
        try:
            start_document = jsonio.read_json(snapshot_of)
            if isinstance(start_document, dict):
                recorded_exports = list(start_document.get("exports") or [])
        except Exception:  # noqa: BLE001 - start 记录不可读不影响快照
            recorded_exports = []
        manifest["snapshot"] = {
            "of": _rel(workspace, snapshot_of),
            "snapshotAt": paths.iso_now(),
            "recordedExports": recorded_exports,
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = run_manifests_dir(workspace)
    target_run_file = Path(run_file) if run_file is not None else (run_dir / f"{manifest['runId']}.json")
    manifest["runManifestPath"] = _rel(workspace, target_run_file)
    jsonio.write_json_atomic(target_run_file, manifest)
    if out_path.resolve() != target_run_file.resolve():
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
    md_path = out_path.with_suffix(".md")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    latest = write_latest_pointer(
        workspace,
        run_id=str(manifest["runId"]),
        kind="snapshot" if snapshot_of is not None else "manifest",
        start_path=snapshot_of,
        manifest_path=target_run_file,
        markdown_path=md_path,
        snapshot_path=out_path if snapshot_of is not None else None,
        project_id=str(manifest.get("projectId") or ""),
        project_dir=str(manifest.get("projectDir") or ""),
    )
    return {
        "ok": True,
        "command": "run_manifest",
        "runId": str(manifest["runId"]),
        "runIdSource": "start" if snapshot_of is not None else "generated",
        "manifestPath": str(out_path),
        "runManifestPath": str(target_run_file),
        "markdownPath": str(md_path),
        "snapshot": manifest.get("snapshot"),
        "latest": latest,
        "latestPath": latest.get("path", ""),
        "staleLockRemoved": bool(latest.get("staleLockRemoved")),
        "manifest": manifest,
    }


def record_snapshot(*, workspace: Path, project_dir: Path, out_path: Path | None = None) -> dict[str, Any]:
    """记录导出步骤（P1-6）：复用最近一次 start 的 runId，写 `<runId>.snapshot-<ts14>.json/.md`。

    没有 start 记录时退回旧行为（生成新 runId 的 `<runId>.json`），保证脚本仍可用。
    """
    start_path, start = _latest_start(workspace, project_dir)
    start_run_id = str((start or {}).get("runId") or "") if isinstance(start, dict) else ""
    run_dir = run_manifests_dir(workspace)
    if start_run_id:
        target = Path(out_path) if out_path is not None else (run_dir / f"{start_run_id}.{SNAPSHOT_SUFFIX}-{paths.timestamp14()}.json")
        result = generate(
            workspace=workspace,
            project_dir=project_dir,
            out_path=target,
            run_id=start_run_id,
            snapshot_of=start_path,
            run_file=target,
        )
        result["snapshotPath"] = str(target)
        return result
    target = Path(out_path) if out_path is not None else (run_dir / f"RUN-{paths.timestamp14()}.json")
    if out_path is not None:
        result = generate(workspace=workspace, project_dir=project_dir, out_path=target)
    else:
        # 缺省路径与 runId 对齐，避免同一份 manifest 落两份不同 ts 的文件
        result = generate(workspace=workspace, project_dir=project_dir, out_path=target, run_id=target.stem)
    result["snapshotPath"] = ""
    result["warning"] = "缺少 RUN-*.start.json：记录步骤生成新 runId（建议先 --start 以保持单次运行 runId 一致）"
    return result


def finalize_run(*, workspace: Path, project_dir: Path, out_path: Path) -> dict[str, Any]:
    """结束时写完整 manifest，并与 start 的 plugin/scripts hash 对比（漂移则 versionDrift）。"""
    start_path, start = _latest_start(workspace, project_dir)
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
    run_dir = run_manifests_dir(workspace)
    run_file = run_dir / f"{run_id}.json"
    manifest["runManifestPath"] = _rel(workspace, run_file)
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
    latest = write_latest_pointer(
        workspace,
        run_id=run_id,
        kind="finalize",
        start_path=start_path,
        manifest_path=run_file,
        markdown_path=md_path,
        project_id=str(manifest.get("projectId") or ""),
        project_dir=str(manifest.get("projectDir") or ""),
    )
    return {
        "ok": not manifest["versionDrift"],
        "command": "run_manifest_finalize",
        "runId": run_id,
        "versionDrift": manifest["versionDrift"],
        "envRefreshedAfterStart": env_refreshed_after_start,
        "envSnapshotAfterStart": env_snapshot_after_start,
        "conclusion": manifest["conclusion"],
        "driftDetail": drift,
        "manifestPath": str(out_path),
        "runManifestPath": str(run_file),
        "latest": latest,
        "latestPath": latest.get("path", ""),
        "manifest": manifest,
        "exitCode": exit_code,
    }


def run_selftest() -> dict[str, Any]:
    """脚本自检：start→record→finalize 同一 runId、`_latest.json` 实体指针、遗留 lock 清理。"""
    import platform
    import re as _re
    import shutil
    import tempfile

    global _plugin_files, _scripts_hash

    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-run-manifest-selftest-"))
    # 插件哈希改为固定桩：自检不依赖「并行开发时 plugin/ 目录内容是否稳定」
    original_plugin_files = _plugin_files
    original_scripts_hash = _scripts_hash
    _plugin_files = lambda: [{"path": "selftest-stub.py", "sha256": "sha256:" + "0" * 64, "size": 1}]
    _scripts_hash = lambda: "0" * 64
    try:
        workspace = tmp / "ws"
        (workspace / "dsh" / "logs").mkdir(parents=True)
        project_dir = workspace / "示例项目-20261121120000-abcd"
        project_dir.mkdir()
        now = paths.iso_now()
        project = schema.initialize_project_document(
            project_id="prj-20261121120000-abcd",
            bidder_ref={"bidderId": "bdr-20261121120000-abcd", "dirName": "示例投标人", "displayName": "示例投标人", "snapshotAt": now},
            workspace_fingerprint=paths.workspace_fingerprint(workspace),
            now_iso=now,
        )
        jsonio.write_json_atomic(project_dir / "project.json", project)
        state_lib.write_state(project_dir, schema.initialize_state_document(project_id="prj-20261121120000-abcd", now_iso=now))
        run_dir = run_manifests_dir(workspace)
        # 历史遗留（P3-14）：只有 `_latest.json.lock`、没有实体文件 → 下次写入前清理
        legacy_lock = run_dir / f"{LATEST_FILE_NAME}.lock"
        legacy_lock.write_bytes(b"\0")
        started = start_run(workspace=workspace, project_dir=project_dir)
        run_id = str(started.get("runId") or "")
        start_path = Path(str(started.get("startPath") or ""))
        if not run_id.startswith("RUN-") or not start_path.is_file():
            raise AssertionError(f"start 结果不正确：runId={run_id!r} startPath={start_path}")
        if started.get("staleLockRemoved") is not True:
            raise AssertionError(f"遗留 _latest.json.lock 未清理：{started.get('staleLockRemoved')}")
        checks.append("start")
        pointer_start = read_latest_pointer(workspace)
        if pointer_start.get("kind") != "start" or pointer_start.get("runId") != run_id or not pointer_start.get("updatedAt"):
            raise AssertionError(f"_latest.json（start）不正确：{pointer_start}")
        checks.append("latest_pointer_start")
        # 记录导出步骤（P1-6）：复用 start 的 runId，写 <runId>.snapshot-<ts14>.json
        snapshot = record_snapshot(workspace=workspace, project_dir=project_dir)
        snapshot_path = Path(str(snapshot.get("snapshotPath") or ""))
        if (
            snapshot.get("runId") != run_id
            or snapshot.get("runIdSource") != "start"
            or not snapshot_path.is_file()
            or _re.fullmatch(rf"{_re.escape(run_id)}\.snapshot-\d{{14}}\.json", snapshot_path.name) is None
        ):
            raise AssertionError(f"记录步骤未复用 start 的 runId：runId={snapshot.get('runId')!r} path={snapshot_path.name!r}")
        if (run_dir / f"{run_id}.json").exists():
            raise AssertionError("记录步骤不应写 <runId>.json（finalize 才写）")
        if (snapshot.get("manifest") or {}).get("runId") != run_id:
            raise AssertionError(f"snapshot manifest runId 不一致：{(snapshot.get('manifest') or {}).get('runId')}")
        pointer_snapshot = read_latest_pointer(workspace)
        if pointer_snapshot.get("kind") != "snapshot" or pointer_snapshot.get("runId") != run_id or pointer_snapshot.get("snapshotPath") != _rel(workspace, snapshot_path):
            raise AssertionError(f"_latest.json（snapshot）不正确：{pointer_snapshot}")
        checks.append("snapshot_same_run_id")
        # finalize 仍写 <runId>.json/.md，并把指针更新为 kind=finalize
        finalized = finalize_run(workspace=workspace, project_dir=project_dir, out_path=run_dir / f"{run_id}.json")
        if finalized.get("runId") != run_id or finalized.get("versionDrift") is not False:
            raise AssertionError(f"finalize 不正确：runId={finalized.get('runId')!r} drift={finalized.get('versionDrift')}")
        if not (run_dir / f"{run_id}.json").is_file() or not (run_dir / f"{run_id}.md").is_file():
            raise AssertionError("finalize 未写 <runId>.json/.md")
        pointer_finalize = read_latest_pointer(workspace)
        if (
            pointer_finalize.get("kind") != "finalize"
            or pointer_finalize.get("runId") != run_id
            or pointer_finalize.get("manifestPath") != _rel(workspace, run_dir / f"{run_id}.json")
            or pointer_finalize.get("startPath") != _rel(workspace, start_path)
        ):
            raise AssertionError(f"_latest.json（finalize）不正确：{pointer_finalize}")
        checks.append("single_run_id")
        checks.append("latest_pointer_finalize")
        # 再次记录：仍复用同一 runId，快照文件名可区分（不同 ts14）
        time.sleep(1.05)
        second = record_snapshot(workspace=workspace, project_dir=project_dir)
        if second.get("runId") != run_id or second.get("snapshotPath") == snapshot.get("snapshotPath"):
            raise AssertionError(f"第二次记录未复用 runId 或快照名重复：{second.get('runId')!r} / {second.get('snapshotPath')}")
        if read_latest_pointer(workspace).get("runId") != run_id:
            raise AssertionError("_latest.json 未指向同一 runId")
        checks.append("snapshot_repeat_same_run_id")
        # 多项目工作区：另一个项目更晚 start 时，本项目记录/finalize 不得串用它的 runId
        other_dir = workspace / "另一个项目-20261121120500-efgh"
        other_dir.mkdir()
        other_now = paths.iso_now()
        other_project = schema.initialize_project_document(
            project_id="prj-20261121120500-efgh",
            bidder_ref={"bidderId": "bdr-20261121120500-efgh", "dirName": "另一投标人", "displayName": "另一投标人", "snapshotAt": other_now},
            workspace_fingerprint=paths.workspace_fingerprint(workspace),
            now_iso=other_now,
        )
        jsonio.write_json_atomic(other_dir / "project.json", other_project)
        state_lib.write_state(other_dir, schema.initialize_state_document(project_id="prj-20261121120500-efgh", now_iso=other_now))
        other_started = start_run(workspace=workspace, project_dir=other_dir)
        other_run_id = str(other_started.get("runId") or "")
        if not other_run_id or other_run_id == run_id:
            raise AssertionError(f"第二个项目 start runId 异常：{other_run_id!r}")
        _scoped_path, scoped_doc = _latest_start(workspace, project_dir)
        if str((scoped_doc or {}).get("runId") or "") != run_id:
            raise AssertionError(f"按项目过滤 start 失败，取到了 {scoped_doc.get('runId')!r}")
        third = record_snapshot(workspace=workspace, project_dir=project_dir)
        if third.get("runId") != run_id:
            raise AssertionError(f"多项目工作区记录串号：{third.get('runId')!r} != {run_id!r}")
        other_final = finalize_run(workspace=workspace, project_dir=other_dir, out_path=run_dir / f"{other_run_id}.json")
        if other_final.get("runId") != other_run_id or other_final.get("ok") is False:
            raise AssertionError(f"第二个项目 finalize 失败：{other_final.get('runId')!r}")
        checks.append("project_scoped_start")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_run_manifest.py",
                "python": platform.python_version(),
                "checks": checks,
                "import": True,
                "sha256": sha256_file(run_dir / f"{run_id}.json").startswith("sha256:"),
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_run_manifest.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        _plugin_files = original_plugin_files
        _scripts_hash = original_scripts_hash
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = errors.CliParser(prog="bg_run_manifest.py", description="生成验收运行记录（只读）")
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径（生成时必填）")
    parser.add_argument("--project", help="项目目录或 projectId（生成时必填）")
    parser.add_argument("--out", help="输出 JSON 路径（缺省 finalize=<runId>.json、记录步骤=<runId>.snapshot-<ts>.json）")
    parser.add_argument("--keep", type=int, default=20, help="run-manifests 目录保留最新份数（默认 20）")
    parser.add_argument("--start", action="store_true", help="开跑前写 RUN-*.start.json（插件/脚本/env hash）")
    parser.add_argument("--finalize", action="store_true", help="结束时写完整 manifest 并校验相对 start 是否版本漂移")
    parser.add_argument("--selftest", action="store_true", help="脚本自检")
    args = parser.parse_args(argv)
    if args.selftest:
        result = run_selftest()
        errors.emit(result, bool(getattr(args, "json", False)))
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
    if args.finalize:
        out_path = Path(args.out).expanduser() if args.out else None
        if out_path is None:
            _start_path, start_doc = _latest_start(workspace, project_dir)
            start_run_id = str((start_doc or {}).get("runId") or "")
            out_path = run_manifests_dir(workspace) / (f"{start_run_id}.json" if start_run_id else f"RUN-{paths.timestamp14()}.json")
        result = finalize_run(workspace=workspace, project_dir=project_dir, out_path=out_path.resolve())
        errors.emit(result, bool(getattr(args, "json", False)))
        return int(result.get("exitCode") or 0)
    # 记录导出步骤（P1-6）：复用 start 的 runId，产物写 <runId>.snapshot-<ts>.json，不新造 runId
    out_path = Path(args.out).expanduser() if args.out else None
    result = record_snapshot(workspace=workspace, project_dir=project_dir, out_path=out_path)
    errors.emit(result, bool(getattr(args, "json", False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
