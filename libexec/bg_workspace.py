#!/usr/bin/env python3
"""bg_workspace.py：工作区 ``dsh/`` 目录树初始化 + F2 项目骨架创建。

用法（M1/M2）：
    python bg_workspace.py init --workspace <abs> [--json] [--dry-run]
    python bg_workspace.py init --workspace <abs> [--bidder <id>] [--project-name <name>] [--json]
    python bg_workspace.py init-project --workspace <abs> [--bidder <id>] [--project-name <name>] [--json]
    python bg_workspace.py --selftest [--json]

行为：
- 幂等创建 ``<ws>/dsh/{config,bidders,bidders/.trash,backups,logs,locks}``、
  ``registry.json``、``config/user-settings.json`` 骨架；已存在不重建、不清空（PRD 8.11）；
- ``init --bidder`` / ``init-project`` 额外创建 F2 项目骨架：项目目录、
  ``project.json``（只建键、值置空）、``state.json``（初始态，唯一写者 = ``_lib/state.py``）、
  ``registry.projects[]`` 登记、事件 ``project.initialized``；重复执行幂等复用未完成项目；
- 不触发 Agent；只写工作区。
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, schema, tempdir  # noqa: E402

#: dsh/ 下必须存在的目录（PRD 3.4.1 / CONTRACT §4 / §9.2/§9.3 v2：官方样式隔离目录）
DSH_SUBDIRS: tuple[str, ...] = (
    "config",
    "styles",
    "styles/tech",
    "styles/keyinfo",
        "styles/overview",
    "bidders",
    "bidders/.trash",
    "backups",
    "logs",
    "locks",
)
#: registry.json 键顺序（PRD 7.6 + M1 镜像字段 currentBidderId）
REGISTRY_KEY_ORDER: tuple[str, ...] = (
    "schemaVersion",
    "workspaceRootFingerprint",
    "currentBidderId",
    "bidders",
    "projects",
    "updatedAt",
)


def default_registry(workspace: Path, now_iso: str) -> dict[str, Any]:
    """新工作区 registry.json 骨架。"""
    return {
        "schemaVersion": schema.SCHEMA_VERSION,
        "workspaceRootFingerprint": paths.workspace_fingerprint(workspace),
        "currentBidderId": "",
        "bidders": [],
        "projects": [],
        "updatedAt": now_iso,
    }


def default_user_settings(now_iso: str) -> dict[str, Any]:
    """user-settings.json 骨架（S1–S15 默认值，PRD 7.6）。"""
    return {
        "schemaVersion": schema.SCHEMA_VERSION,
        "currentBidderId": "",
        "defaultWordCountTier": 50000,
        "defaultFigureQuota": {"table": 5, "mermaid": 5, "terminal": 5, "network": 0},
        "styleProfile": "official-default",
        "styleProfiles": {
            "tech": "official-tech",
            "keyInfo": "official-keyinfo",
            "overview": "official-overview",
        },
        "interactionMode": "guided",
        "maxReviewRounds": 5,
        "maxParallelWriters": 3,
        "allowNetworkImages": False,  # 兼容旧字段；网络配图功能已移除，恒 false
        "allowAutoDefaults": True,
        "maskSensitive": True,
        "logLevel": "info",
        "updatedAt": now_iso,
    }


def _rebuild_known_first(
    source: dict[str, Any], order: tuple[str, ...]
) -> dict[str, Any]:
    """按 schema 顺序重排已知键，未知键保持原顺序追加（PRD 7.0 round-trip）。"""
    result: dict[str, Any] = {}
    for key in order:
        if key in source:
            result[key] = source[key]
    for key, value in source.items():
        if key not in result:
            result[key] = value
    return result


def _repair_registry(workspace: Path, document: dict[str, Any], now_iso: str) -> list[str]:
    """补齐 registry 的可选字段（缺失才补），返回补充的键名列表。"""
    added: list[str] = []
    if "workspaceRootFingerprint" not in document:
        document["workspaceRootFingerprint"] = paths.workspace_fingerprint(workspace)
        added.append("workspaceRootFingerprint")
    if "currentBidderId" not in document:
        document["currentBidderId"] = ""
        added.append("currentBidderId")
    if added:
        document.update({"updatedAt": now_iso})
    return added


def _repair_user_settings(document: dict[str, Any], now_iso: str) -> list[str]:
    """补齐 user-settings 的 S1–S15 默认键（缺失才补）；迁移时剥离废弃字段。"""
    defaults = default_user_settings(now_iso)
    added: list[str] = []
    for key, value in defaults.items():
        if key in ("schemaVersion", "currentBidderId", "updatedAt"):
            continue
        if key not in document:
            document[key] = value
            added.append(key)
    # M8：styleProfiles 必须包含 tech/keyInfo/overview；旧设置只补缺失项（保留用户自定义值）
    expected_profiles = defaults["styleProfiles"]
    profiles = document.get("styleProfiles")
    if not isinstance(profiles, dict):
        document["styleProfiles"] = dict(expected_profiles)
        added.append("styleProfiles")
    else:
        missing = [name for name in expected_profiles if name not in profiles]
        if missing:
            profiles.update({name: expected_profiles[name] for name in missing})
            document["styleProfiles"] = profiles
            added.append("styleProfiles(" + ",".join(missing) + ")")
    # M4.4：userInputTimeoutSec 已废弃（PRD v1.0.3 注 / CONTRACT §5），旧文件迁移时删除
    if "userInputTimeoutSec" in document:
        document.pop("userInputTimeoutSec", None)
        added.append("userInputTimeoutSec(removed)")
    if added:
        document["updatedAt"] = now_iso
    return added


def _ensure_dir(path: Path, workspace: Path, created: list[str], skipped: list[str], dry_run: bool) -> None:
    rel = paths.rel_posix(workspace, path)
    if path.is_dir():
        skipped.append(rel)
        return
    created.append(rel)
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)


def init_workspace(
    workspace: str | Path,
    *,
    dry_run: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """初始化工作区 dsh/ 树；返回统一成功 JSON。"""
    ws = paths.normalize_workspace(workspace)
    now_iso = paths.iso_now()
    created: list[str] = []
    skipped: list[str] = []
    changed: list[str] = []
    repaired: list[str] = []
    already_initialized = paths.registry_path(ws).is_file()

    dsh = paths.dsh_root(ws)
    _ensure_dir(dsh, ws, created, skipped, dry_run)
    for sub in DSH_SUBDIRS:
        _ensure_dir(dsh / sub, ws, created, skipped, dry_run)

    # registry.json
    registry_file = paths.registry_path(ws)
    if registry_file.is_file():
        document = jsonio.read_json(registry_file)
        if not isinstance(document, dict):
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                "dsh/registry.json 必须是 JSON 对象",
                hint="从 dsh/backups/ 恢复 registry.json",
                detail={"path": "dsh/registry.json"},
            )
        schema.raise_if_errors(schema.validate_registry(document), filename="dsh/registry.json")
        added = _repair_registry(ws, document, now_iso)
        if added:
            document = _rebuild_known_first(document, REGISTRY_KEY_ORDER)
            repaired.extend(f"dsh/registry.json#{key}" for key in added)
            changed.append("dsh/registry.json")
            if not dry_run:
                jsonio.write_json_atomic(registry_file, document, lock_timeout=lock_timeout)
        skipped.append("dsh/registry.json")
    else:
        document = default_registry(ws, now_iso)
        created.append("dsh/registry.json")
        changed.append("dsh/registry.json")
        if not dry_run:
            jsonio.write_json_atomic(registry_file, document, lock_timeout=lock_timeout)

    # config/user-settings.json
    settings_file = paths.user_settings_path(ws)
    if settings_file.is_file():
        settings = jsonio.read_json(settings_file)
        if not isinstance(settings, dict):
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                "dsh/config/user-settings.json 必须是 JSON 对象",
                hint="从 dsh/backups/ 恢复 user-settings.json",
                detail={"path": "dsh/config/user-settings.json"},
            )
        schema.raise_if_errors(
            schema.validate_user_settings(settings), filename="dsh/config/user-settings.json"
        )
        added = _repair_user_settings(settings, now_iso)
        if added:
            settings = _rebuild_known_first(settings, tuple(default_user_settings(now_iso)))
            repaired.extend(f"dsh/config/user-settings.json#{key}" for key in added)
            changed.append("dsh/config/user-settings.json")
            if not dry_run:
                jsonio.write_json_atomic(settings_file, settings, lock_timeout=lock_timeout)
        skipped.append("dsh/config/user-settings.json")
    else:
        settings = default_user_settings(now_iso)
        created.append("dsh/config/user-settings.json")
        changed.append("dsh/config/user-settings.json")
        if not dry_run:
            jsonio.write_json_atomic(settings_file, settings, lock_timeout=lock_timeout)

    if not dry_run:
        event_log.log_event(
            ws,
            "workspace.initialized",
            actor="script",
            detail={
                "alreadyInitialized": already_initialized,
                "created": created,
                "repaired": repaired,
                "workspaceName": ws.name,
            },
        )

    return {
        "ok": True,
        "command": "workspace_init",
        "alreadyInitialized": already_initialized,
        "created": created,
        "skipped": skipped,
        "changed": changed,
        "repaired": repaired,
        "dryRun": dry_run,
        "workspace": str(ws),
        "registryPath": "dsh/registry.json",
        "settingsPath": "dsh/config/user-settings.json",
    }


def _read_registry(workspace: Path) -> dict[str, Any]:
    """读取并校验 registry.json（M2 起含 projects 索引）。"""
    document = jsonio.read_json(paths.registry_path(workspace))
    if not isinstance(document, dict):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "dsh/registry.json 必须是 JSON 对象",
            hint="从 dsh/backups/ 恢复 registry.json",
            detail={"path": "dsh/registry.json"},
        )
    schema.raise_if_errors(schema.validate_registry(document), filename="dsh/registry.json")
    document.setdefault("bidders", [])
    document.setdefault("projects", [])
    return document


def _read_user_settings(workspace: Path) -> dict[str, Any]:
    """读取并校验 user-settings.json。"""
    document = jsonio.read_json(paths.user_settings_path(workspace))
    if not isinstance(document, dict):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "dsh/config/user-settings.json 必须是 JSON 对象",
            hint="从 dsh/backups/ 恢复 user-settings.json",
            detail={"path": "dsh/config/user-settings.json"},
        )
    schema.raise_if_errors(
        schema.validate_user_settings(document), filename="dsh/config/user-settings.json"
    )
    # M4.4：旧 settings 读取时兼容剥离（不写盘；写盘迁移由 init_workspace 完成）
    document.pop("userInputTimeoutSec", None)
    return document


def _find_bidder_entry(registry: dict[str, Any], bidder_id: str) -> dict[str, Any] | None:
    """按 bidderId 查 registry.bidders[]。"""
    for entry in registry.get("bidders") or []:
        if isinstance(entry, dict) and str(entry.get("bidderId") or "") == bidder_id:
            return dict(entry)
    return None


def _read_project_state(workspace: Path, dir_name: str) -> dict[str, Any] | None:
    """宽容读取项目 state.json（仅用于幂等判定/返回摘要，损坏返回 None）。"""
    path = workspace / dir_name / "state.json"
    try:
        document = jsonio.read_json(path)
    except errors.BgError:
        return None
    return document if isinstance(document, dict) else None


def _find_active_project(
    workspace: Path,
    registry: dict[str, Any],
    bidder_id: str,
    project_name: str,
) -> dict[str, Any] | None:
    """查找可复用的未完成项目：同 projectName 优先，否则同投标人的活动项目。"""
    candidates = [entry for entry in registry.get("projects") or [] if isinstance(entry, dict)]
    for entry in candidates:
        entry_name = str(entry.get("projectName") or "")
        if project_name:
            if entry_name != project_name:
                continue
        elif str(entry.get("bidderId") or "") != bidder_id:
            continue
        state = _read_project_state(workspace, str(entry.get("dirName") or ""))
        stage = str((state or {}).get("currentStage") or entry.get("currentStage") or "")
        if stage in ("completed", "aborted"):
            continue
        return {"entry": entry, "state": state, "stage": stage}
    return None


def _project_payload(
    workspace: Path,
    *,
    project_id: str,
    dir_name: str,
    bidder_id: str,
    project_name: str,
    already_exists: bool,
    project_document: dict[str, Any] | None,
    state_document: dict[str, Any] | None,
    created: list[str],
    changed: list[str],
    skipped: list[str],
    dry_run: bool,
    workspace_init: dict[str, Any],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """组装 init-project 成功 JSON（Host/面板/Agent 共用）。"""
    project_root = workspace / dir_name
    project_document = project_document or {}
    state_document = state_document or {}
    return {
        "ok": True,
        "command": "workspace_init_project",
        "projectId": project_id,
        "dirName": dir_name,
        "projectDirRel": dir_name,
        "projectRoot": str(project_root),
        "projectFile": str(project_root / "project.json"),
        "stateFile": str(project_root / "state.json"),
        "bidderId": bidder_id,
        "projectName": project_name,
        "alreadyExists": already_exists,
        "dryRun": dry_run,
        "created": created,
        "changed": changed,
        "skipped": skipped,
        "warnings": list(warnings or []),
        "project": {
            "projectId": project_id,
            "dirName": dir_name,
            "projectName": project_name or str(project_document.get("projectName") or ""),
            "currentStage": str(state_document.get("currentStage") or "project_init"),
            "currentPackage": state_document.get("currentPackage"),
            "bidderRef": project_document.get("bidderRef"),
            "updatedAt": str(project_document.get("updatedAt") or ""),
        },
        "state": {
            "currentStage": state_document.get("currentStage"),
            "currentScope": state_document.get("currentScope"),
            "currentPackage": state_document.get("currentPackage"),
            "revision": state_document.get("revision"),
            "updatedAt": state_document.get("updatedAt"),
        },
        "workspaceInit": {
            "created": workspace_init.get("created"),
            "skipped": workspace_init.get("skipped"),
            "changed": workspace_init.get("changed"),
            "alreadyInitialized": workspace_init.get("alreadyInitialized"),
        },
    }


def _hash_json_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _bidder_integrity(ws: Path, bidder_ref: Mapping[str, Any]) -> dict[str, Any]:
    """读取投标人快照，补 completeness/snapshotHash/certificatesHash（项目初始化门禁依据）。"""
    dir_name = str(bidder_ref.get("dirName") or "")
    if not dir_name:
        return {}
    bidder_dir = ws / "dsh" / "bidders" / dir_name
    bidder_file = bidder_dir / "bidder.json"
    cert_file = bidder_dir / "certificates.json"
    awards_file = bidder_dir / "awards-penalties.json"
    try:
        bidder = jsonio.read_json(bidder_file) if bidder_file.is_file() else {}
        certificates = jsonio.read_json(cert_file) if cert_file.is_file() else {}
        awards = jsonio.read_json(awards_file) if awards_file.is_file() else {}
    except Exception:  # noqa: BLE001
        bidder, certificates, awards = {}, {}, {}
    completeness = schema.compute_completeness(bidder, certificates, awards)
    return {
        "completeness": completeness.get("overall"),
        "completenessDetail": completeness,
        "snapshotHash": _hash_json_file(bidder_file),
        "certificatesHash": _hash_json_file(cert_file),
    }


def init_project(
    workspace: str | Path,
    *,
    bidder_id: str | None = None,
    project_name: str = "",
    dry_run: bool = False,
    allow_new: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """F2：创建项目目录 + project.json（仅键）+ state.json（初始态）+ registry.projects 登记。

    幂等（PRD 8.11 / F2.4）：同 bidderId 已有未完成项目，或同 projectName 已存在且未完成 → 复用不重建。
    ``allow_new=True``（CLI `--new`）时跳过复用，强制新建（用户想重跑同一项目时使用）。
    """
    ws = paths.normalize_workspace(str(workspace))
    workspace_init = init_workspace(ws, lock_timeout=lock_timeout)
    registry = _read_registry(ws)
    settings = _read_user_settings(ws)
    resolved_id = str(bidder_id or settings.get("currentBidderId") or "").strip()
    if not resolved_id:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "尚未设置当前投标人（currentBidderId 为空）",
            hint="先运行 python libexec/bg_bidder.py set-current --workspace <ws> --bidder <id> --json",
            detail={"currentBidderId": ""},
        )
    entry = _find_bidder_entry(registry, resolved_id)
    if entry is None:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"投标人不存在：{resolved_id}",
            hint="运行 bg_bidder.py list 查看有效 bidderId",
            detail={"bidderId": resolved_id},
        )
    bidder_dir = paths.bidders_dir(ws) / str(entry.get("dirName") or "")
    bidder_file = bidder_dir / "bidder.json"
    if not bidder_file.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "投标人目录缺少 bidder.json",
            hint="运行 bg_bidder.py validate 检查投标人数据",
            detail={"bidderId": resolved_id},
        )
    bidder_document = jsonio.read_json(bidder_file)
    if not isinstance(bidder_document, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "bidder.json 必须是 JSON 对象")
    schema.raise_if_errors(schema.validate_bidder(bidder_document), filename="bidder.json")
    display_name = str(bidder_document.get("displayName") or entry.get("displayName") or "").strip()
    if not display_name:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "当前投标人缺少 displayName，无法生成项目目录名",
            hint="先补全投标人基本信息",
            detail={"bidderId": resolved_id},
        )
    project_name_value = str(project_name or "").strip()

    existing = None if allow_new else _find_active_project(ws, registry, resolved_id, project_name_value)
    if existing is not None:
        existing_entry = existing["entry"]
        dir_name = str(existing_entry.get("dirName") or "")
        project_document = None
        try:
            raw_project = jsonio.read_json(ws / dir_name / "project.json")
            project_document = dict(raw_project) if isinstance(raw_project, dict) else None
        except errors.BgError:
            project_document = None
        return _project_payload(
            ws,
            project_id=str(existing_entry.get("projectId") or ""),
            dir_name=dir_name,
            bidder_id=resolved_id,
            project_name=project_name_value or str(existing_entry.get("projectName") or ""),
            already_exists=True,
            project_document=project_document,
            state_document=existing["state"],
            created=[],
            changed=[],
            skipped=[dir_name],
            dry_run=dry_run,
            workspace_init=workspace_init,
            warnings=[
                f"已存在未完成项目（currentStage={existing['stage']}），复用不重建；"
                "如需重跑同一项目，加 --new 强制新建（旧项目保留，可 T11 abort 或 resume）"
            ],
        )

    if dry_run:
        planned_name = paths.new_bidder_dir_name(display_name)
        planned = [
            planned_name,
            f"{planned_name}/project.json",
            f"{planned_name}/state.json",
            "dsh/registry.json",
        ]
        return _project_payload(
            ws,
            project_id="<planned>",
            dir_name=planned_name,
            bidder_id=resolved_id,
            project_name=project_name_value,
            already_exists=False,
            project_document=None,
            state_document=None,
            created=planned,
            changed=planned,
            skipped=[],
            dry_run=True,
            workspace_init=workspace_init,
        )

    base_name = paths.new_bidder_dir_name(display_name)
    directory: Path | None = None
    for attempt in range(5):
        candidate = ws / (
            base_name if directory is None and attempt == 0 else paths.new_bidder_dir_name(display_name)
        )
        if candidate.exists():
            continue
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        directory = candidate
        break
    if directory is None:
        raise errors.BgError(
            errors.E_IO,
            "无法创建唯一的项目目录（连续 5 次命名冲突）",
            hint="稍后重试；该错误通常由并发创建同名投标人的项目触发",
        )
    dir_name = directory.name
    project_id = paths.new_project_id()
    now_iso = paths.iso_now()
    bidder_ref = {
        "bidderId": resolved_id,
        "dirName": str(entry.get("dirName") or ""),
        "displayName": display_name,
        "snapshotAt": now_iso,
    }
    bidder_ref.update(_bidder_integrity(ws, bidder_ref))
    project_document = schema.initialize_project_document(
        project_id=project_id,
        bidder_ref=bidder_ref,
        workspace_fingerprint=paths.workspace_fingerprint(ws),
        now_iso=now_iso,
        project_name=project_name_value,
    )
    state_document = schema.initialize_state_document(project_id=project_id, now_iso=now_iso)
    schema.raise_if_errors(schema.validate_project(project_document), filename="project.json")
    schema.raise_if_errors(schema.validate_state(state_document), filename="state.json")
    created: list[str] = [dir_name]
    try:
        jsonio.write_json_atomic(directory / "project.json", project_document, lock=False, lock_timeout=lock_timeout)
        created.append(f"{dir_name}/project.json")
        jsonio.write_json_atomic(directory / "state.json", state_document, lock=False, lock_timeout=lock_timeout)
        created.append(f"{dir_name}/state.json")
        registry_file = paths.registry_path(ws)
        with jsonio.FileLock(registry_file, timeout=lock_timeout):
            latest = _read_registry(ws)
            duplicate = None if allow_new else _find_active_project(ws, latest, resolved_id, project_name_value)
            if duplicate is not None:
                duplicate_entry = duplicate["entry"]
                raise _ProjectExists(duplicate_entry, duplicate["state"])
            latest["projects"] = list(latest.get("projects") or []) + [
                {
                    "projectId": project_id,
                    "dirName": dir_name,
                    "projectName": project_name_value,
                    "bidderId": resolved_id,
                    "currentStage": "project_init",
                    "currentPackage": None,
                    "createdAt": now_iso,
                    "updatedAt": now_iso,
                }
            ]
            latest["updatedAt"] = now_iso
            jsonio.write_json_atomic(registry_file, latest, lock=False, lock_timeout=lock_timeout)
        created.append("dsh/registry.json")
    except _ProjectExists as duplicate_exc:  # 并发创建：回滚本目录，返回已存在
        shutil.rmtree(directory, ignore_errors=True)
        duplicate_entry = duplicate_exc.entry
        duplicate_dir = str(duplicate_entry.get("dirName") or "")
        duplicate_state = duplicate_exc.state
        return _project_payload(
            ws,
            project_id=str(duplicate_entry.get("projectId") or ""),
            dir_name=duplicate_dir,
            bidder_id=resolved_id,
            project_name=str(duplicate_entry.get("projectName") or ""),
            already_exists=True,
            project_document=None,
            state_document=duplicate_state,
            created=[],
            changed=[],
            skipped=[duplicate_dir],
            dry_run=False,
            workspace_init=workspace_init,
            warnings=["并发初始化检测到已存在未完成项目，复用已有项目"],
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    event_log.log_event(
        ws,
        "project.initialized",
        actor="script",
        detail={
            "projectId": project_id,
            "dirName": dir_name,
            "bidderId": resolved_id,
            "projectName": project_name_value,
            "currentStage": "project_init",
        },
    )
    return _project_payload(
        ws,
        project_id=project_id,
        dir_name=dir_name,
        bidder_id=resolved_id,
        project_name=project_name_value,
        already_exists=False,
        project_document=project_document,
        state_document=state_document,
        created=created,
        changed=list(created),
        skipped=[],
        dry_run=False,
        workspace_init=workspace_init,
    )


class _ProjectExists(Exception):
    """内部信号：registry 锁内检测到并发创建的同项目。"""

    def __init__(self, entry: dict[str, Any], state: dict[str, Any] | None) -> None:
        super().__init__("project already exists")
        self.entry = entry
        self.state = state


def run_selftest() -> dict[str, Any]:
    """脚本自检：目录树、幂等、读写、命名与事件日志（临时目录，不碰真实工作区）。"""
    checks: list[str] = []
    tmp = tempdir.mkdtemp(prefix="bidgenie-ws-selftest-")
    try:
        ws = tmp / "ws"
        ws.mkdir()

        first = init_workspace(ws)
        checks.append("init")
        for rel in DSH_SUBDIRS + ("registry.json", "config/user-settings.json"):
            target = ws / "dsh" / rel
            if not target.exists():
                raise AssertionError(f"缺少 {rel}")
        checks.append("layout")

        registry = jsonio.read_json(paths.registry_path(ws))
        schema.raise_if_errors(schema.validate_registry(registry), filename="registry.json")
        settings = jsonio.read_json(paths.user_settings_path(ws))
        schema.raise_if_errors(schema.validate_user_settings(settings), filename="user-settings.json")
        if "userInputTimeoutSec" in settings:
            raise AssertionError("user-settings 初始化不应写 userInputTimeoutSec")
        checks.append("registry+settings")

        # M4.4：旧 user-settings 迁移（注入废弃键 → init 剥离，其他设置不变）
        legacy_settings = jsonio.read_json(paths.user_settings_path(ws))
        legacy_settings["userInputTimeoutSec"] = 600
        jsonio.write_json_atomic(paths.user_settings_path(ws), legacy_settings)
        second = init_workspace(ws)
        cleaned_settings = jsonio.read_json(paths.user_settings_path(ws))
        if "userInputTimeoutSec" in cleaned_settings:
            raise AssertionError("init 未迁移剥离 userInputTimeoutSec")
        if cleaned_settings.get("allowNetworkImages") != settings.get("allowNetworkImages"):
            raise AssertionError("迁移不应修改其他 user-settings 字段")
        if not any("userInputTimeoutSec" in item for item in second.get("repaired") or []):
            raise AssertionError("迁移未记录 repaired 标记")
        checks.append("settings_cleanup")
        if not second["alreadyInitialized"] or second["created"]:
            raise AssertionError("重复 init 不幂等")
        checks.append("idempotent")

        names = event_log.event_names(ws)
        if "workspace.initialized" not in names:
            raise AssertionError("缺少 workspace.initialized 事件")
        checks.append("event_log")

        slug = paths.slugify("XX科技/有限:公司")
        if ":" in slug or "/" in slug or "\\" in slug:
            raise AssertionError("slug 未替换非法字符")
        if not paths.is_bidder_dir_name("测试公司-20261108120000-a1b2"):
            raise AssertionError("目录名规则不匹配")
        checks.append("paths")

        probe = ws / "dsh" / "logs" / "selftest.json"
        with jsonio.FileLock(probe):
            with jsonio.FileLock(probe):
                jsonio.write_json_atomic(probe, {"ok": True}, lock=False)
        if jsonio.read_json(probe) != {"ok": True}:
            raise AssertionError("原子读写失败")
        checks.append("io+lock")

        # M2：init-project（手动构造投标人，避免脚本间 import 依赖）
        bidder_id = "bdr-20261108120000-a1b2"
        bidder_dir_name = "自检公司-20261108120000-a1b2"
        bidder_dir = ws / "dsh" / "bidders" / bidder_dir_name
        bidder_dir.mkdir()
        bidder_doc = schema.normalize_bidder(
            {"displayName": "自检公司", "bidderType": "legal_entity"},
            bidder_id=bidder_id,
            now_iso=paths.iso_now(),
        )
        schema.raise_if_errors(schema.validate_bidder(bidder_doc), filename="bidder.json")
        jsonio.write_json_atomic(bidder_dir / "bidder.json", bidder_doc, lock=False)
        registry = jsonio.read_json(paths.registry_path(ws))
        registry["bidders"].append(
            {
                "bidderId": bidder_id,
                "dirName": bidder_dir_name,
                "displayName": "自检公司",
                "bidderType": "legal_entity",
                "updatedAt": paths.iso_now(),
            }
        )
        jsonio.write_json_atomic(paths.registry_path(ws), registry)
        settings = jsonio.read_json(paths.user_settings_path(ws))
        settings["currentBidderId"] = bidder_id
        settings["updatedAt"] = paths.iso_now()
        jsonio.write_json_atomic(paths.user_settings_path(ws), settings)
        project = init_project(ws, project_name="自检项目")
        project_dir = ws / str(project["dirName"])
        if not (project_dir / "project.json").is_file() or not (project_dir / "state.json").is_file():
            raise AssertionError("init-project 未生成 project.json/state.json")
        project_state = jsonio.read_json(project_dir / "state.json")
        if project_state.get("currentStage") != "project_init" or project_state.get("revision") != 1:
            raise AssertionError(f"state.json 初始态不正确：{project_state.get('currentStage')}")
        project_registry = jsonio.read_json(paths.registry_path(ws))
        if len(project_registry.get("projects") or []) != 1:
            raise AssertionError("registry.projects 未登记")
        again = init_project(ws)
        if not again.get("alreadyExists") or again.get("dirName") != project["dirName"]:
            raise AssertionError("重复 init-project 不幂等")
        if not any("--new" in str(item) for item in again.get("warnings") or []):
            raise AssertionError(f"复用提示未给出 --new 入口：{again.get('warnings')}")
        # N-07：--new 强制新建（同投标人已有未完成项目时不再复用）
        new_project = init_project(ws, project_name="自检项目", allow_new=True)
        if new_project.get("alreadyExists") or new_project.get("dirName") == project.get("dirName"):
            raise AssertionError(f"--new 未强制新建：{new_project}")
        if len((jsonio.read_json(paths.registry_path(ws)).get("projects") or [])) != 2:
            raise AssertionError("--new 未登记新项目")
        checks.append("init_project")
        checks.append("init_project_new")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_workspace.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一转退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_workspace.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实工作区",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prune_registry(workspace: str | Path, *, dry_run: bool = False, lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC) -> dict[str, Any]:
    """清理 registry.projects 指向不存在目录的幽灵条目；保留 archive/软删条目。"""
    ws = paths.normalize_workspace(workspace)
    registry_file = paths.registry_path(ws)
    if not registry_file.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 dsh/registry.json", hint="先运行 bg_workspace.py init")
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    with jsonio.FileLock(registry_file, timeout=lock_timeout):
        registry = _read_registry(ws)
        projects = []
        for entry in registry.get("projects") or []:
            if not isinstance(entry, dict):
                continue
            dir_name = str(entry.get("dirName") or "")
            project_dir = ws / dir_name if dir_name else None
            archived = bool(entry.get("archived") or entry.get("deletedAt") or entry.get("softDeleted"))
            if archived or (project_dir is not None and (project_dir / "project.json").is_file()):
                kept.append(entry)
            else:
                removed.append({"projectId": entry.get("projectId"), "dirName": dir_name, "reason": "目录或 project.json 不存在"})
        registry["projects"] = kept
        if removed:
            registry["updatedAt"] = paths.iso_now()
        if removed and not dry_run:
            jsonio.write_json_atomic(registry_file, registry, lock=False, lock_timeout=lock_timeout)
    return {
        "ok": True,
        "command": "workspace_prune",
        "workspace": str(ws),
        "dryRun": dry_run,
        "removed": removed,
        "removedCount": len(removed),
        "keptCount": len(kept),
        "registryPath": paths.rel_posix(ws, registry_file),
        "changed": bool(removed) and not dry_run,
    }


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 子命令（M1 init + M2 init / init-project）。"""
    parser = errors.CliParser(
        prog="bg_workspace.py",
        description="BidGenie 工作区/项目初始化：幂等创建 dsh/ 目录树；F2 创建项目骨架",
    )
    errors.add_common_args(parser)
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检；返回 selftest JSON")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser(
        "init",
        parents=[],
        help="幂等创建 <workspace>/dsh/ 树 + registry.json + config/user-settings.json；带 --bidder 时同时创建项目",
    )
    errors.add_common_args(init, subparser=True)
    init.add_argument("--workspace", required=True, help="工作区根的绝对路径")
    init.add_argument("--bidder", help="投标人 bidderId（缺省读 user-settings.currentBidderId）")
    init.add_argument("--project-name", dest="project_name", help="项目名称（写入 project.json.projectName）")
    init.add_argument("--new", action="store_true", help="强制新建项目（不复用同投标人未完成项目）")
    init.add_argument("--dry-run", action="store_true", help="只输出计划，不写任何文件")

    init_project_parser = subparsers.add_parser(
        "init-project",
        help="F2：创建项目目录 + project.json（仅键）+ state.json（初始态）+ registry.projects 登记",
    )
    errors.add_common_args(init_project_parser, subparser=True)
    init_project_parser.add_argument("--workspace", required=True, help="工作区根的绝对路径")
    init_project_parser.add_argument("--bidder", help="投标人 bidderId（缺省读 user-settings.currentBidderId）")
    init_project_parser.add_argument("--project-name", dest="project_name", help="项目名称（写入 project.json.projectName）")
    init_project_parser.add_argument("--new", action="store_true", help="强制新建项目（不复用同投标人未完成项目）")
    init_project_parser.add_argument("--dry-run", action="store_true", help="只输出计划，不写任何文件")

    prune = subparsers.add_parser("prune", help="清理 registry 中目录已不存在的幽灵项目（保留 archive/软删）")
    errors.add_common_args(prune, subparser=True)
    prune.add_argument("--workspace", required=True, help="工作区根的绝对路径")
    prune.add_argument("--dry-run", action="store_true", help="只报告将删除条目，不写 registry")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按子命令分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if args.command is None:
        raise errors.BgError(
            errors.E_USAGE,
            "缺少子命令",
            hint="可用子命令：init、init-project、prune；或运行 --selftest",
        )
    lock_timeout = errors.timeout_value(args)
    if args.command == "init":
        bidder = getattr(args, "bidder", None)
        project_name = getattr(args, "project_name", None)
        if bidder or project_name:
            return init_project(
                args.workspace,
                bidder_id=bidder,
                project_name=project_name or "",
                dry_run=bool(getattr(args, "dry_run", False)),
                allow_new=bool(getattr(args, "new", False)),
                lock_timeout=lock_timeout,
            )
        return init_workspace(
            args.workspace,
            dry_run=bool(getattr(args, "dry_run", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "init-project":
        return init_project(
            args.workspace,
            bidder_id=getattr(args, "bidder", None),
            project_name=getattr(args, "project_name", "") or "",
            dry_run=bool(getattr(args, "dry_run", False)),
            allow_new=bool(getattr(args, "new", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "prune":
        return prune_registry(args.workspace, dry_run=bool(getattr(args, "dry_run", False)), lock_timeout=lock_timeout)
    raise errors.BgError(errors.E_USAGE, f"未知子命令：{args.command}", hint="可用子命令：init、init-project、prune")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_workspace.py",
    )


if __name__ == "__main__":
    sys.exit(main())
