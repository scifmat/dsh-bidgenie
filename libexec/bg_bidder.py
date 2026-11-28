#!/usr/bin/env python3
"""bg_bidder.py：投标人 CRUD（M1，F1）。

用法（CONTRACT §4 冻结 CLI）：
    python bg_bidder.py list         --workspace <abs> [--json]
    python bg_bidder.py create       --workspace <abs> --data <json-file> [--json]
    python bg_bidder.py update       --workspace <abs> --bidder <id> --data <json-file> [--json]
    python bg_bidder.py delete       --workspace <abs> --bidder <id> --confirm [--json]
    python bg_bidder.py set-current  --workspace <abs> --bidder <id> [--json]
    python bg_bidder.py validate     --workspace <abs> [--bidder <id>] [--json]
    python bg_bidder.py --selftest [--json]

兼容别名（PRD 附录 H.1 / 开发文档 M1-01）：``--from-json`` = ``--data``（来源记为 agent），
``delete --trash`` = ``--confirm``。

约束：只写工作区；共享 JSON 全部经 ``_lib/jsonio.py`` 原子写 + 文件锁；
事件日志敏感键掩码；软删除到 ``dsh/bidders/.trash/<dir>-<ts14>/``。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import bg_workspace  # noqa: E402  - 自检复用 init_workspace
from _lib import errors, jsonio, logging as event_log, paths, schema, tempdir  # noqa: E402

#: profile.md 空骨架（PRD 7.7.5 建议结构；仅用户/Agent 阅读补充）
PROFILE_SKELETON = """# 投标人简介素材

## 公司简介

## 核心优势

## 类似业绩

## 可复用叙述素材
"""

#: 最小 bidder 字段（创建时若缺 displayName / bidderType 直接失败）
REQUIRED_CREATE_FIELDS: tuple[str, ...] = ("displayName", "bidderType")

_BACKUP_DIR_RE = re.compile(r"^\d{14}-[a-z0-9]{4}$")


# ---------------------------------------------------------------------------
# 通用结果 / 读取工具
# ---------------------------------------------------------------------------
def _ok(command: str, *, changed: list[str] | None = None, skipped: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """构造成功 JSON（PRD H.0：至少 ok/command/changed/skipped）。"""
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
        "changed": list(changed or []),
        "skipped": list(skipped or []),
    }
    payload.update(extra)
    return payload


def _load_workspace(raw: str) -> Path:
    """校验工作区并确认已初始化（registry.json 存在）。"""
    workspace = paths.normalize_workspace(raw)
    if not paths.registry_path(workspace).is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "工作区尚未初始化（缺少 dsh/registry.json）",
            hint="先运行 python libexec/bg_workspace.py init --workspace <ws> --json",
            detail={"workspace": str(workspace)},
        )
    return workspace


def _read_registry(workspace: Path) -> dict[str, Any]:
    """读取并校验 registry.json。"""
    document = jsonio.read_json(paths.registry_path(workspace))
    if not isinstance(document, dict):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "dsh/registry.json 必须是 JSON 对象",
            hint="从 dsh/backups/ 恢复 registry.json",
            detail={"path": "dsh/registry.json"},
        )
    schema.raise_if_errors(schema.validate_registry(document), filename="dsh/registry.json")
    document.setdefault("currentBidderId", "")
    return document


def _read_settings(workspace: Path) -> dict[str, Any]:
    """读取并校验 user-settings.json。"""
    document = jsonio.read_json(paths.user_settings_path(workspace))
    if not isinstance(document, dict):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "dsh/config/user-settings.json 必须是 JSON 对象",
            hint="从 dsh/backups/ 恢复 user-settings.json",
            detail={"path": "dsh/config/user-settings.json"},
        )
    schema.raise_if_errors(schema.validate_user_settings(document), filename="dsh/config/user-settings.json")
    return document


def _read_doc(
    path: Path,
    validator: Any,
    filename: str,
) -> dict[str, Any]:
    """读取并校验单个 JSON 文档（不存在视为 E_NOT_FOUND）。"""
    document = jsonio.read_json(path)
    if not isinstance(document, dict):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"{filename} 必须是 JSON 对象",
            hint="从 dsh/backups/ 或 *.bak 恢复该文件",
            detail={"path": filename},
        )
    schema.raise_if_errors(validator(document), filename=filename)
    return document


def _read_doc_or_empty(
    path: Path,
    *,
    normalizer: Any,
    validator: Any,
    filename: str,
    now_iso: str,
) -> dict[str, Any]:
    """读取可选 JSON 文档；不存在返回归一化空文档。"""
    if not path.is_file():
        return normalizer(None, now_iso=now_iso)
    return _read_doc(path, validator, filename)


def _read_json_tolerant(path: Path) -> Any:
    """读取 JSON，任何失败返回 None（只用于 list 展示与兜底扫描）。"""
    try:
        return jsonio.read_json(path)
    except errors.BgError:
        return None


def _canonical(document: Any) -> str:
    """稳定序列化，用于内容差异比较。"""
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def _without_updated_at(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "updatedAt"}


def _content_changed(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    """忽略 updatedAt 后比较内容是否变化。"""
    return _canonical(_without_updated_at(old or {})) != _canonical(_without_updated_at(new))


def _scan_bidder_dirs(workspace: Path) -> list[Path]:
    """列出 dsh/bidders/ 下真实投标人目录（排除 .trash 等隐藏目录）。"""
    root = paths.bidders_dir(workspace)
    if not root.is_dir():
        return []
    return sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=lambda entry: entry.name,
    )


def _entry_by_id(registry: Mapping[str, Any], bidder_id: str) -> dict[str, Any] | None:
    """从 registry 中按 bidderId 查找索引条目。"""
    for entry in registry.get("bidders") or []:
        if isinstance(entry, Mapping) and str(entry.get("bidderId") or "") == bidder_id:
            return dict(entry)
    return None


def _scan_for_bidder_id(workspace: Path, bidder_id: str) -> Path | None:
    """兜底：扫描目录内 bidder.json 匹配 bidderId（registry 漏登记时修复用）。"""
    for directory in _scan_bidder_dirs(workspace):
        bidder = _read_json_tolerant(directory / "bidder.json")
        if isinstance(bidder, Mapping) and str(bidder.get("bidderId") or "") == bidder_id:
            return directory
    return None


def _resolve_bidder(
    workspace: Path, registry: Mapping[str, Any], bidder_id: str
) -> tuple[dict[str, Any] | None, Path | None]:
    """解析投标人索引条目与目录路径（registry 优先，目录扫描兜底）。"""
    entry = _entry_by_id(registry, bidder_id)
    if entry is not None:
        directory = paths.bidders_dir(workspace) / str(entry.get("dirName") or "")
        return entry, (directory if directory.is_dir() else None)
    return None, _scan_for_bidder_id(workspace, bidder_id)


def _require_bidder_dir(workspace: Path, registry: Mapping[str, Any], bidder_id: str) -> tuple[dict[str, Any], Path]:
    """要求投标人存在，否则返回 E_NOT_FOUND。"""
    entry, directory = _resolve_bidder(workspace, registry, bidder_id)
    if entry is None or directory is None:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"未找到投标人：{bidder_id}",
            hint="运行 python libexec/bg_bidder.py list --workspace <ws> --json 查看 bidderId",
            detail={"bidderId": bidder_id},
        )
    return entry, directory


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def _prune_backups(workspace: Path, keep: int = 5) -> None:
    """保留最近 5 份 dsh/backups 快照（PRD 3.7.4）。"""
    root = paths.backups_dir(workspace)
    if not root.is_dir():
        return
    snapshots = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and _BACKUP_DIR_RE.match(entry.name)),
        key=lambda entry: entry.name,
        reverse=True,
    )
    for stale in snapshots[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _snapshot_backup(workspace: Path, files: list[Path]) -> str | None:
    """把即将被覆盖的文件复制到 dsh/backups/<ts14>-<rand4>/<相对路径>。"""
    existing = [path for path in files if path.is_file()]
    if not existing:
        return None
    root = paths.backups_dir(workspace) / f"{paths.timestamp14()}-{paths.rand4()}"
    try:
        for source in existing:
            relative = paths.rel_posix(workspace, source)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    except OSError as exc:
        raise errors.BgError(
            errors.E_IO,
            "写入 dsh/backups 快照失败，已中止更新（不写任何数据文件）",
            hint="检查工作区写权限与磁盘空间",
            detail={"reason": exc.strerror or type(exc).__name__},
        ) from exc
    _prune_backups(workspace, keep=5)
    return paths.rel_posix(workspace, root)


# ---------------------------------------------------------------------------
# 数据载荷解析
# ---------------------------------------------------------------------------
def _data_source(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (数据来源描述, 记录到 meta.source 的值：panel/agent)。"""
    data = getattr(args, "data", None)
    from_json = getattr(args, "from_json", None)
    if data and from_json:
        raise errors.BgError(
            errors.E_USAGE, "--data 与 --from-json 只能二选一", hint="推荐使用 --data <json-file>"
        )
    if data:
        return str(data), "panel"
    if from_json:
        return str(from_json), "agent"
    raise errors.BgError(
        errors.E_USAGE,
        "缺少 --data <json-file>",
        hint='传入 JSON 文件，结构 {"bidder":{...},"certificates?":{...}}',
    )


def _read_payload(spec: str) -> dict[str, Any]:
    """读取 --data 文件（也接受内联 JSON 对象字符串）。"""
    text = str(spec).strip()
    candidate = Path(text)
    if candidate.is_file():
        document = jsonio.read_json(candidate)
    elif text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise errors.BgError(
                errors.E_USAGE,
                f"内联 JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）",
                hint="改用 --data <json-file> 传入文件，避免命令行转义问题",
            ) from exc
    else:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"--data 文件不存在：{text}",
            hint="传入存在的 JSON 文件路径（绝对路径）",
        )
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "--data 顶层必须是 JSON 对象", detail={"pointer": ""})
    return dict(document)


def _extract_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    """从 update 载荷提取 bidder patch：支持 {patch}/{bidder}/{直接字段}。"""
    patch = payload.get("patch")
    if isinstance(patch, Mapping):
        return dict(patch)
    bidder = payload.get("bidder")
    if isinstance(bidder, Mapping):
        return dict(bidder)
    return {
        key: value
        for key, value in payload.items()
        if key not in ("certificates", "performance", "awardsPenalties")
    }


def _reject_schema_version(payload: Mapping[str, Any]) -> None:
    """拒绝显式写入非 1.0 schemaVersion（PRD 8.8：读到更高版本拒绝写入）。"""
    provided = payload.get("schemaVersion")
    if provided is not None and provided != schema.SCHEMA_VERSION:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"不支持的 schemaVersion：{provided!r}（M1 仅支持 {schema.SCHEMA_VERSION}）",
            hint="升级插件后再写入该数据，不要手工降级字段",
            detail={"pointer": "/schemaVersion", "expected": schema.SCHEMA_VERSION},
        )


# ---------------------------------------------------------------------------
# 落盘辅助
# ---------------------------------------------------------------------------
def _write_json(path: Path, document: Any, *, lock_timeout: float, lock: bool = True) -> None:
    jsonio.write_json_atomic(path, document, lock=lock, lock_timeout=lock_timeout)


def _create_bidder_files(
    workspace: Path,
    *,
    bidder: dict[str, Any],
    certificates: dict[str, Any],
    performance: dict[str, Any],
    awards: dict[str, Any],
    profile_text: str,
    source: str,
    lock_timeout: float,
) -> tuple[str, list[str]]:
    """创建投标人目录并写入 6 个 JSON/MD 文件 + attachments/（全新建，无需加锁）。"""
    display_name = str(bidder.get("displayName") or "")
    base_name = paths.new_bidder_dir_name(display_name)
    directory: Path | None = None
    for _attempt in range(5):
        candidate = paths.bidders_dir(workspace) / (
            base_name if directory is None else paths.new_bidder_dir_name(display_name)
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
            "无法创建唯一的投标人目录（连续 5 次命名冲突）",
            hint="稍后重试；该错误通常由并发创建同名投标人触发",
        )
    dir_name = directory.name
    created: list[str] = []

    def write_doc(name: str, document: Any) -> None:
        path = directory / name
        jsonio.write_json_atomic(path, document, lock=False, lock_timeout=lock_timeout)
        created.append(paths.rel_posix(workspace, path))

    try:
        write_doc("bidder.json", bidder)
        write_doc("certificates.json", certificates)
        write_doc("performance.json", performance)
        write_doc("awards-penalties.json", awards)
        meta = schema.normalize_meta(
            bidder_id=str(bidder["bidderId"]),
            dir_name=dir_name,
            completeness=schema.compute_completeness(bidder, certificates, awards),
            source=source if source in schema.META_SOURCES else "panel",
            created_at=str(bidder.get("updatedAt") or paths.iso_now()),
            now_iso=str(bidder.get("updatedAt") or paths.iso_now()),
        )
        write_doc("meta.json", meta)
        profile_path = directory / "profile.md"
        jsonio.write_text_atomic(profile_path, profile_text, lock=False, lock_timeout=lock_timeout)
        created.append(paths.rel_posix(workspace, profile_path))
        attachments = directory / "attachments"
        attachments.mkdir()
        created.append(paths.rel_posix(workspace, attachments))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return dir_name, created


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def list_bidders(workspace: str | Path, *, lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC) -> dict[str, Any]:
    """列出投标人（registry 为索引；坏文件标 invalid，不中断列表）。"""
    ws = _load_workspace(str(workspace))
    registry = _read_registry(ws)
    settings = _read_settings(ws)
    current_id = str(settings.get("currentBidderId") or "")
    bidders: list[dict[str, Any]] = []
    for entry in registry.get("bidders") or []:
        if not isinstance(entry, Mapping):
            continue
        dir_name = str(entry.get("dirName") or "")
        bidder_id = str(entry.get("bidderId") or "")
        directory = paths.bidders_dir(ws) / dir_name
        status = "ok"
        completeness: float | None = None
        counts = {"certificates": 0, "performances": 0, "awards": 0, "penalties": 0}
        display_name = str(entry.get("displayName") or "")
        bidder_type = str(entry.get("bidderType") or "")
        updated_at = str(entry.get("updatedAt") or "")
        if not directory.is_dir():
            status = "missing"
        else:
            bidder = _read_json_tolerant(directory / "bidder.json")
            if isinstance(bidder, Mapping):
                display_name = str(bidder.get("displayName") or display_name)
                bidder_type = str(bidder.get("bidderType") or bidder_type)
                updated_at = str(bidder.get("updatedAt") or updated_at)
            else:
                status = "invalid"
            meta = _read_json_tolerant(directory / "meta.json")
            if isinstance(meta, Mapping) and isinstance(meta.get("completeness"), Mapping):
                overall = meta["completeness"].get("overall")
                if isinstance(overall, (int, float)) and not isinstance(overall, bool):
                    completeness = float(overall)
            for key, file_name, field in (
                ("certificates", "certificates.json", "items"),
                ("performances", "performance.json", "items"),
                ("awards", "awards-penalties.json", "awards"),
                ("penalties", "awards-penalties.json", "penalties"),
            ):
                document = _read_json_tolerant(directory / file_name)
                if isinstance(document, Mapping) and isinstance(document.get(field), list):
                    counts[key] = len(document[field])
        bidders.append(
            {
                "bidderId": bidder_id,
                "dirName": dir_name,
                "displayName": display_name,
                "bidderType": bidder_type,
                "updatedAt": updated_at,
                "isCurrent": bool(current_id) and current_id == bidder_id,
                "completeness": completeness,
                "counts": counts,
                "status": status,
            }
        )
    bidders.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    bidders.sort(key=lambda item: not item.get("isCurrent"))
    return _ok(
        "bidder_list",
        bidders=bidders,
        count=len(bidders),
        currentBidderId=current_id,
        changed=[],
        skipped=[],
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def create_bidder(
    workspace: str | Path,
    payload: Mapping[str, Any],
    *,
    source: str = "panel",
    dry_run: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """创建投标人；以 bidderId 去重（幂等，PRD 8.3）。"""
    ws = _load_workspace(str(workspace))
    now_iso = paths.iso_now()

    raw_bidder = payload.get("bidder")
    if not isinstance(raw_bidder, Mapping):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "--data 缺少 bidder 对象",
            hint='结构应为 {"bidder":{"displayName":...,"bidderType":...}}',
            detail={"pointer": "/bidder", "expected": "object"},
        )
    raw_bidder = dict(raw_bidder)
    _reject_schema_version(raw_bidder)
    missing = [field for field in REQUIRED_CREATE_FIELDS if not str(raw_bidder.get(field) or "").strip()]
    if missing:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"bidder 缺少必填字段：{', '.join(missing)}",
            hint="新建投标人至少需要 displayName（名称）与 bidderType（legal_entity/natural_person）",
            detail={"findings": [{"pointer": f"/{field}", "message": "必填非空字符串"} for field in missing]},
        )

    bidder_id_raw = raw_bidder.get("bidderId")
    if bidder_id_raw in (None, ""):
        bidder_id = paths.new_bidder_id()
    else:
        bidder_id = str(bidder_id_raw).strip()
        if not paths.is_valid_bidder_id(bidder_id):
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                "bidderId 非法",
                hint="bidderId 只允许字母/数字/下划线/连字符（3–64）；留空则自动生成 bdr-<ts14>-<rand4>",
                detail={"pointer": "/bidderId"},
            )

    bidder = schema.normalize_bidder(raw_bidder, bidder_id=bidder_id, now_iso=now_iso)
    bidder.pop("_source", None)
    schema.raise_if_errors(schema.validate_bidder(bidder), filename="bidder.json")
    certificates = schema.normalize_certificates(payload.get("certificates"), now_iso=now_iso)
    performance = schema.normalize_performance(payload.get("performance"), now_iso=now_iso)
    awards = schema.normalize_awards_penalties(payload.get("awardsPenalties"), now_iso=now_iso)
    schema.raise_if_errors(schema.validate_certificates(certificates), filename="certificates.json")
    schema.raise_if_errors(schema.validate_performance(performance), filename="performance.json")
    schema.raise_if_errors(schema.validate_awards_penalties(awards), filename="awards-penalties.json")

    completeness = schema.compute_completeness(bidder, certificates, awards)

    registry_file = paths.registry_path(ws)
    with jsonio.FileLock(registry_file, timeout=lock_timeout):
        registry = _read_registry(ws)
        duplicate = _entry_by_id(registry, bidder_id)
        if duplicate is not None:
            dir_name = str(duplicate.get("dirName") or "")
            return _ok(
                "bidder_create",
                bidderId=bidder_id,
                dirName=dir_name,
                createdAt=str(duplicate.get("updatedAt") or ""),
                source=source,
                alreadyExists=True,
                completeness=completeness,
                changed=[],
                skipped=[f"dsh/bidders/{dir_name}"] if dir_name else [],
                dryRun=dry_run,
            )
        existing_dir = _scan_for_bidder_id(ws, bidder_id)
        if existing_dir is not None:
            dir_name = existing_dir.name
            entry = {
                "bidderId": bidder_id,
                "dirName": dir_name,
                "displayName": str(bidder.get("displayName") or ""),
                "bidderType": str(bidder.get("bidderType") or ""),
                "updatedAt": now_iso,
            }
            registry["bidders"] = list(registry.get("bidders") or []) + [entry]
            registry["updatedAt"] = now_iso
            _write_json(registry_file, registry, lock_timeout=lock_timeout, lock=False)
            return _ok(
                "bidder_create",
                bidderId=bidder_id,
                dirName=dir_name,
                createdAt=now_iso,
                source=source,
                alreadyExists=True,
                repairedRegistry=True,
                completeness=completeness,
                changed=["dsh/registry.json"],
                skipped=[],
                dryRun=dry_run,
            )

        if dry_run:
            planned_name = paths.new_bidder_dir_name(str(bidder.get("displayName") or ""))
            planned = [
                f"dsh/bidders/{planned_name}",
                f"dsh/bidders/{planned_name}/bidder.json",
                f"dsh/bidders/{planned_name}/certificates.json",
                f"dsh/bidders/{planned_name}/performance.json",
                f"dsh/bidders/{planned_name}/awards-penalties.json",
                f"dsh/bidders/{planned_name}/meta.json",
                f"dsh/bidders/{planned_name}/profile.md",
                f"dsh/bidders/{planned_name}/attachments",
                "dsh/registry.json",
            ]
            return _ok(
                "bidder_create",
                bidderId=bidder_id,
                dirName=planned_name,
                createdAt=now_iso,
                source=source,
                alreadyExists=False,
                completeness=completeness,
                changed=planned,
                skipped=[],
                dryRun=True,
            )

        dir_name, created = _create_bidder_files(
            ws,
            bidder=bidder,
            certificates=certificates,
            performance=performance,
            awards=awards,
            profile_text=PROFILE_SKELETON,
            source=source,
            lock_timeout=lock_timeout,
        )
        entry = {
            "bidderId": bidder_id,
            "dirName": dir_name,
            "displayName": str(bidder.get("displayName") or ""),
            "bidderType": str(bidder.get("bidderType") or ""),
            "updatedAt": now_iso,
        }
        registry["bidders"] = list(registry.get("bidders") or []) + [entry]
        registry["updatedAt"] = now_iso
        _write_json(registry_file, registry, lock_timeout=lock_timeout, lock=False)
        created.append("dsh/registry.json")

    event_log.log_event(
        ws,
        "bidder.created",
        actor="script",
        detail={
            "dirName": dir_name,
            "bidderType": str(bidder.get("bidderType") or ""),
            "source": source,
            "completeness": completeness.get("overall"),
        },
    )
    missing_parts = schema.incomplete_reasons(bidder, certificates, performance)
    if missing_parts:
        event_log.log_event(
            ws,
            "bidder.incomplete",
            actor="script",
            detail={"dirName": dir_name, "missing": missing_parts},
            result="warning",
        )
    return _ok(
        "bidder_create",
        bidderId=bidder_id,
        dirName=dir_name,
        createdAt=now_iso,
        source=source,
        alreadyExists=False,
        completeness=completeness,
        changed=created,
        skipped=[],
        dryRun=False,
    )


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------
def update_bidder(
    workspace: str | Path,
    bidder_id: str,
    payload: Mapping[str, Any],
    *,
    dry_run: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """更新投标人（bidder 深合并；证书/业绩/奖惩整组替换）。"""
    ws = _load_workspace(str(workspace))
    now_iso = paths.iso_now()
    patch = _extract_patch(payload)
    _reject_schema_version(patch)
    if "bidderId" in patch and str(patch.get("bidderId")) != bidder_id:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "bidderId 不可修改",
            hint="创建后 bidderId 是稳定标识；如需换人请新建投标人",
            detail={"pointer": "/bidderId"},
        )

    registry = _read_registry(ws)
    entry, directory = _require_bidder_dir(ws, registry, bidder_id)
    dir_name = directory.name
    bidder_file = directory / "bidder.json"
    certificates_file = directory / "certificates.json"
    performance_file = directory / "performance.json"
    awards_file = directory / "awards-penalties.json"
    meta_file = directory / "meta.json"
    for required_file in (bidder_file,):
        if not required_file.is_file():
            raise errors.BgError(
                errors.E_NOT_FOUND,
                f"投标人目录缺少 {required_file.name}",
                hint="目录可能被手工删除；从 dsh/backups/ 恢复或重新创建投标人",
                detail={"dirName": dir_name},
            )

    # 1) bidder.json 读改写全程持锁，避免多进程丢更新（PRD 3.7 / 8.6）
    planned_writes: list[tuple[Path, dict[str, Any]]] = []
    with jsonio.FileLock(bidder_file, timeout=lock_timeout):
        old_bidder = _read_doc(bidder_file, schema.validate_bidder, "bidder.json")
        merged = schema.merge_bidder_document(old_bidder, patch)
        new_bidder = schema.normalize_bidder(merged, bidder_id=bidder_id, now_iso=now_iso)
        schema.raise_if_errors(schema.validate_bidder(new_bidder), filename="bidder.json")
        if not dry_run:
            _snapshot_backup(ws, [bidder_file])
            _write_json(bidder_file, new_bidder, lock_timeout=lock_timeout, lock=False)

    # 2) 证书：可选替换；未提供时保留内容但按 validTo 重算 status
    old_certs = _read_doc_or_empty(
        certificates_file,
        normalizer=schema.normalize_certificates,
        validator=schema.validate_certificates,
        filename="certificates.json",
        now_iso=now_iso,
    )
    if "certificates" in payload:
        new_certs = schema.normalize_certificates(payload.get("certificates"), now_iso=now_iso)
    else:
        new_certs = schema.normalize_certificates(old_certs, now_iso=now_iso)
    schema.raise_if_errors(schema.validate_certificates(new_certs), filename="certificates.json")

    # 3) 业绩：仅当显式提供时替换
    old_perf = _read_doc_or_empty(
        performance_file,
        normalizer=lambda data, *, now_iso: schema.normalize_performance(data, now_iso=now_iso),
        validator=schema.validate_performance,
        filename="performance.json",
        now_iso=now_iso,
    )
    if "performance" in payload:
        new_perf = schema.normalize_performance(payload.get("performance"), now_iso=now_iso)
        new_perf["updatedAt"] = now_iso
    else:
        new_perf = old_perf
    schema.raise_if_errors(schema.validate_performance(new_perf), filename="performance.json")

    # 4) 奖惩：仅当显式提供时替换
    old_awards = _read_doc_or_empty(
        awards_file,
        normalizer=lambda data, *, now_iso: schema.normalize_awards_penalties(data, now_iso=now_iso),
        validator=schema.validate_awards_penalties,
        filename="awards-penalties.json",
        now_iso=now_iso,
    )
    if "awardsPenalties" in payload:
        new_awards = schema.normalize_awards_penalties(payload.get("awardsPenalties"), now_iso=now_iso)
    else:
        new_awards = old_awards
    schema.raise_if_errors(schema.validate_awards_penalties(new_awards), filename="awards-penalties.json")

    # 5) meta 完整度 + registry 镜像
    old_meta = (
        _read_doc(meta_file, schema.validate_meta, "meta.json") if meta_file.is_file() else None
    )
    completeness = schema.compute_completeness(new_bidder, new_certs, new_awards)
    created_at = str((old_meta or {}).get("createdAt") or new_bidder.get("updatedAt") or now_iso)
    source = str((old_meta or {}).get("source") or "panel")
    new_meta = schema.normalize_meta(
        bidder_id=bidder_id,
        dir_name=dir_name,
        completeness=completeness,
        source=source if source in schema.META_SOURCES else "panel",
        created_at=created_at,
        now_iso=now_iso,
    )

    changed: list[str] = [paths.rel_posix(ws, bidder_file)]
    planned: list[tuple[Path, dict[str, Any]]] = []
    old_cert_document = _read_json_tolerant(certificates_file) if certificates_file.is_file() else None
    if not (
        isinstance(old_cert_document, Mapping) and not _content_changed(old_cert_document, new_certs)
    ):
        planned.append((certificates_file, new_certs))
    if "performance" in payload:
        planned.append((performance_file, new_perf))
    if "awardsPenalties" in payload:
        planned.append((awards_file, new_awards))
    planned.append((meta_file, new_meta))
    if not dry_run:
        _snapshot_backup(ws, [path for path, _document in planned])
        for path, document in planned:
            _write_json(path, document, lock_timeout=lock_timeout)
    changed.extend(paths.rel_posix(ws, path) for path, _document in planned)

    # 6) registry 索引（持锁读改写）
    registry_file = paths.registry_path(ws)
    with jsonio.FileLock(registry_file, timeout=lock_timeout):
        latest_registry = _read_registry(ws)
        new_entry = {
            "bidderId": bidder_id,
            "dirName": dir_name,
            "displayName": str(new_bidder.get("displayName") or ""),
            "bidderType": str(new_bidder.get("bidderType") or ""),
            "updatedAt": now_iso,
        }
        entries = [
            dict(candidate)
            for candidate in (latest_registry.get("bidders") or [])
            if not (isinstance(candidate, Mapping) and str(candidate.get("bidderId") or "") == bidder_id)
        ]
        entries.append(new_entry)
        latest_registry["bidders"] = entries
        latest_registry["updatedAt"] = now_iso
        if not dry_run:
            _write_json(registry_file, latest_registry, lock_timeout=lock_timeout, lock=False)
        changed.append("dsh/registry.json")

    if not dry_run:
        event_log.log_event(
            ws,
            "bidder.updated",
            actor="script",
            detail={
                "dirName": dir_name,
                "fields": sorted(str(key) for key in patch.keys()),
                "completeness": completeness.get("overall"),
            },
        )
        missing_parts = schema.incomplete_reasons(new_bidder, new_certs, new_perf)
        if missing_parts:
            event_log.log_event(
                ws,
                "bidder.incomplete",
                actor="script",
                detail={"dirName": dir_name, "missing": missing_parts},
                result="warning",
            )
    return _ok(
        "bidder_update",
        bidderId=bidder_id,
        dirName=dir_name,
        updatedAt=now_iso,
        completeness=completeness,
        fields=sorted(str(key) for key in patch.keys()),
        changed=changed,
        skipped=[],
        dryRun=dry_run,
    )


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def delete_bidder(
    workspace: str | Path,
    bidder_id: str,
    *,
    confirm: bool = False,
    trash: bool = False,
    dry_run: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """软删除投标人：目录移入 ``dsh/bidders/.trash/<dir>-<ts14>/``（PRD F1.2 C）。"""
    if not (confirm or trash):
        raise errors.BgError(
            errors.E_USAGE,
            "删除需要二次确认",
            hint="在确认弹窗后重跑并加 --confirm（--trash 为兼容别名）",
            detail={"bidderId": bidder_id},
        )
    ws = _load_workspace(str(workspace))
    registry = _read_registry(ws)
    entry, directory = _resolve_bidder(ws, registry, bidder_id)
    if entry is None and directory is None:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"未找到投标人：{bidder_id}",
            hint="运行 list 查看有效 bidderId",
            detail={"bidderId": bidder_id},
        )
    dir_name = str((entry or {}).get("dirName") or (directory.name if directory else ""))
    warnings: list[str] = []
    referenced = [
        str(project.get("projectId") or project.get("dirName") or "")
        for project in (registry.get("projects") or [])
        if isinstance(project, Mapping) and str(project.get("bidderId") or "") == bidder_id
    ]
    if referenced:
        warnings.append("该投标人已被项目引用；删除后历史项目仍保留快照（PRD F1.6）")

    trashed_to = ""
    changed: list[str] = []
    if directory is not None and directory.is_dir():
        target_name = f"{dir_name}-{paths.timestamp14()}"
        trash_root = paths.trash_dir(ws)
        planned_target = trash_root / target_name
        if planned_target.exists():
            planned_target = trash_root / f"{target_name}-{paths.rand4()}"
        trashed_to = f".trash/{planned_target.name}"
        if not dry_run:
            trash_root.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(str(directory), str(planned_target))
            except OSError as exc:
                raise errors.BgError(
                    errors.E_IO,
                    "移动投标人目录到 .trash 失败（文件可能被占用）",
                    hint="关闭占用该目录文件的程序后重试",
                    detail={"dirName": dir_name, "reason": exc.strerror or type(exc).__name__},
                ) from exc
            changed.append(f"dsh/bidders/{trashed_to}")
    else:
        warnings.append("目录已不存在，仅清理 registry 索引（目录本身不需移动）")

    registry_file = paths.registry_path(ws)
    with jsonio.FileLock(registry_file, timeout=lock_timeout):
        latest_registry = _read_registry(ws)
        latest_registry["bidders"] = [
            dict(candidate)
            for candidate in (latest_registry.get("bidders") or [])
            if not (isinstance(candidate, Mapping) and str(candidate.get("bidderId") or "") == bidder_id)
        ]
        if str(latest_registry.get("currentBidderId") or "") == bidder_id:
            latest_registry["currentBidderId"] = ""
        latest_registry["updatedAt"] = paths.iso_now()
        if not dry_run:
            _write_json(registry_file, latest_registry, lock_timeout=lock_timeout, lock=False)
        changed.append("dsh/registry.json")

    cleared_current = False
    settings_file = paths.user_settings_path(ws)
    with jsonio.FileLock(settings_file, timeout=lock_timeout):
        settings = _read_settings(ws)
        if str(settings.get("currentBidderId") or "") == bidder_id:
            settings["currentBidderId"] = ""
            settings["updatedAt"] = paths.iso_now()
            if not dry_run:
                _write_json(settings_file, settings, lock_timeout=lock_timeout, lock=False)
            cleared_current = True
            changed.append("dsh/config/user-settings.json")

    if not dry_run:
        event_log.log_event(
            ws,
            "bidder.deleted",
            actor="script",
            detail={
                "dirName": dir_name,
                "trashedTo": trashed_to,
                "clearedCurrent": cleared_current,
            },
        )
    return _ok(
        "bidder_delete",
        bidderId=bidder_id,
        dirName=dir_name,
        trashedTo=trashed_to,
        trashDir=f"dsh/bidders/{trashed_to}" if trashed_to else "",
        clearedCurrent=cleared_current,
        warnings=warnings,
        changed=changed,
        skipped=[],
        dryRun=dry_run,
    )


# ---------------------------------------------------------------------------
# set-current
# ---------------------------------------------------------------------------
def set_current(
    workspace: str | Path,
    bidder_id: str,
    *,
    dry_run: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """设置当前投标人：写 user-settings.currentBidderId 并同步 registry 镜像。"""
    ws = _load_workspace(str(workspace))
    registry = _read_registry(ws)
    entry, directory = _require_bidder_dir(ws, registry, bidder_id)
    bidder_file = directory / "bidder.json"
    _read_doc(bidder_file, schema.validate_bidder, "bidder.json")  # 当前投标人必须可解析（F1.7）

    settings_file = paths.user_settings_path(ws)
    changed: list[str] = []
    previous = ""
    with jsonio.FileLock(settings_file, timeout=lock_timeout):
        settings = _read_settings(ws)
        previous = str(settings.get("currentBidderId") or "")
        if previous != bidder_id:
            settings["currentBidderId"] = bidder_id
            settings["updatedAt"] = paths.iso_now()
            if not dry_run:
                _write_json(settings_file, settings, lock_timeout=lock_timeout, lock=False)
            changed.append("dsh/config/user-settings.json")

    if previous != bidder_id:
        registry_file = paths.registry_path(ws)
        with jsonio.FileLock(registry_file, timeout=lock_timeout):
            latest = _read_registry(ws)
            latest["currentBidderId"] = bidder_id
            latest["updatedAt"] = paths.iso_now()
            if not dry_run:
                _write_json(registry_file, latest, lock_timeout=lock_timeout, lock=False)
            changed.append("dsh/registry.json")
        if not dry_run:
            event_log.log_event(
                ws,
                "bidder.current_changed",
                actor="script",
                detail={"previous": previous, "currentBidderId": bidder_id},
            )
    return _ok(
        "bidder_set_current",
        bidderId=bidder_id,
        currentBidderId=bidder_id,
        previous=previous,
        changed=changed,
        skipped=[],
        dryRun=dry_run,
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def validate_workspace(
    workspace: str | Path,
    bidder_id: str | None = None,
    *,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """只读校验：registry / user-settings / 每个投标人目录五件套（PRD 7.7）。"""
    ws = _load_workspace(str(workspace))
    findings: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checked: dict[str, Any] = {"registry": False, "settings": False, "bidders": 0, "files": 0}

    def add(file_label: str, items: list[schema.Finding]) -> None:
        for finding in items:
            payload = finding.to_dict()
            payload["file"] = file_label
            (warnings if finding.severity != "error" else findings).append(payload)

    registry: dict[str, Any] | None = None
    try:
        raw_registry = jsonio.read_json(paths.registry_path(ws))
        if isinstance(raw_registry, dict):
            checked["registry"] = True
            registry = raw_registry
            add("dsh/registry.json", schema.validate_registry(raw_registry))
        else:
            findings.append({"file": "dsh/registry.json", "pointer": "", "message": "必须是 JSON 对象"})
    except errors.BgError as exc:
        findings.append({"file": "dsh/registry.json", "pointer": "", "message": exc.message})

    settings: dict[str, Any] | None = None
    try:
        raw_settings = jsonio.read_json(paths.user_settings_path(ws))
        if isinstance(raw_settings, dict):
            checked["settings"] = True
            settings = raw_settings
            add("dsh/config/user-settings.json", schema.validate_user_settings(raw_settings))
        else:
            findings.append(
                {"file": "dsh/config/user-settings.json", "pointer": "", "message": "必须是 JSON 对象"}
            )
    except errors.BgError as exc:
        findings.append({"file": "dsh/config/user-settings.json", "pointer": "", "message": exc.message})

    entries: list[Mapping[str, Any]] = []
    if registry is not None and isinstance(registry.get("bidders"), list):
        entries = [entry for entry in registry["bidders"] if isinstance(entry, Mapping)]
    if bidder_id is not None:
        entries = [entry for entry in entries if str(entry.get("bidderId") or "") == bidder_id]
        if not entries:
            fallback = _scan_for_bidder_id(ws, bidder_id)
            if fallback is None:
                findings.append(
                    {
                        "file": "dsh/registry.json",
                        "pointer": "/bidders",
                        "message": f"未找到投标人：{bidder_id}",
                    }
                )
            else:
                entries = [{"bidderId": bidder_id, "dirName": fallback.name}]
    checked_bids = 0
    checked_files = 0
    registered_dirs: set[str] = set()
    for entry in entries:
        bidder_entry_id = str(entry.get("bidderId") or "")
        dir_name = str(entry.get("dirName") or "")
        registered_dirs.add(dir_name)
        directory = paths.bidders_dir(ws) / dir_name
        checked_bids += 1
        if not dir_name:
            findings.append(
                {"file": "dsh/registry.json", "pointer": "/bidders", "message": "索引条目缺少 dirName"}
            )
            continue
        if not directory.is_dir():
            findings.append(
                {
                    "file": f"dsh/bidders/{dir_name}",
                    "pointer": "",
                    "message": "投标人目录不存在（registry 索引悬空）",
                }
            )
            continue
        for file_name, validator, label in (
            ("bidder.json", schema.validate_bidder, "bidder.json"),
            ("certificates.json", schema.validate_certificates, "certificates.json"),
            ("performance.json", schema.validate_performance, "performance.json"),
            ("awards-penalties.json", schema.validate_awards_penalties, "awards-penalties.json"),
            ("meta.json", schema.validate_meta, "meta.json"),
        ):
            path = directory / file_name
            relative = f"dsh/bidders/{dir_name}/{file_name}"
            checked_files += 1
            try:
                document = jsonio.read_json(path)
            except errors.BgError as exc:
                findings.append({"file": relative, "pointer": "", "message": exc.message, "error": exc.code})
                continue
            if not isinstance(document, dict):
                findings.append({"file": relative, "pointer": "", "message": "必须是 JSON 对象"})
                continue
            add(relative, validator(document))
            if file_name == "bidder.json":
                document_id = str(document.get("bidderId") or "")
                if bidder_entry_id and document_id != bidder_entry_id:
                    findings.append(
                        {
                            "file": relative,
                            "pointer": "/bidderId",
                            "message": f"bidder.json 的 bidderId（{document_id}）与 registry 索引（{bidder_entry_id}）不一致",
                        }
                    )
        if not (directory / "profile.md").is_file():
            warnings.append(
                {"file": f"dsh/bidders/{dir_name}/profile.md", "pointer": "", "message": "缺少 profile.md（可选但建议保留骨架）"}
            )
        if not (directory / "attachments").is_dir():
            warnings.append(
                {"file": f"dsh/bidders/{dir_name}/attachments", "pointer": "", "message": "缺少 attachments/ 目录"}
            )

    if bidder_id is None:
        for directory in _scan_bidder_dirs(ws):
            if directory.name not in registered_dirs:
                warnings.append(
                    {
                        "file": f"dsh/bidders/{directory.name}",
                        "pointer": "",
                        "message": "目录未登记在 registry.json（孤儿目录，可能由中断操作产生）",
                    }
                )

    if registry is not None and settings is not None:
        current_id = str(settings.get("currentBidderId") or "")
        registry_mirror = str(registry.get("currentBidderId") or "")
        if current_id != registry_mirror:
            warnings.append(
                {
                    "file": "dsh/registry.json",
                    "pointer": "/currentBidderId",
                    "message": "registry 镜像与 user-settings.currentBidderId 不一致（重跑 set-current 可修复）",
                }
            )
        if current_id:
            entry, directory = _resolve_bidder(ws, registry, current_id)
            if entry is None and directory is None:
                findings.append(
                    {
                        "file": "dsh/config/user-settings.json",
                        "pointer": "/currentBidderId",
                        "message": f"当前投标人不存在：{current_id}",
                    }
                )
            elif directory is not None:
                bidder = _read_json_tolerant(directory / "bidder.json")
                if bidder is None:
                    findings.append(
                        {
                            "file": f"dsh/bidders/{directory.name}/bidder.json",
                            "pointer": "",
                            "message": "当前投标人的 bidder.json 无法解析（F1.7 出口条件不满足）",
                        }
                    )
    checked["bidders"] = checked_bids
    checked["files"] = checked_files

    if findings:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"校验失败：{len(findings)} 项错误",
            hint="按 findings 修正字段/恢复备份后重跑 validate",
            detail={"findings": findings, "warnings": warnings, "checked": checked},
        )
    return _ok(
        "bidder_validate",
        valid=True,
        findings=[],
        warnings=warnings,
        checked=checked,
        changed=[],
        skipped=[],
    )


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """脚本自检：临时工作区跑 create→list→update→set-current→validate→delete。"""
    checks: list[str] = []
    tmp = tempdir.mkdtemp(prefix="bidgenie-bidder-selftest-")
    try:
        ws = tmp / "ws"
        ws.mkdir()
        bg_workspace.init_workspace(ws)
        checks.append("workspace_init")

        payload = {
            "bidder": {
                "bidderId": "bdr-20261108120000-a1b2",
                "bidderType": "legal_entity",
                "displayName": "自检科技有限公司",
                "legalName": "自检科技有限公司",
                "unifiedSocialCreditCode": "91330000MA27X0000Y",
                "contact": {"name": "张三", "phone": "13800001111", "email": "zhangsan@example.com"},
                "bank": {"accountName": "自检科技有限公司", "bankName": "某银行", "accountNo": "6222020200001234567"},
            },
            "certificates": {
                "items": [
                    {"name": "资质A", "category": "qualification", "validTo": "2999-12-31"},
                    {"name": "资质B", "category": "license", "validTo": "2020-01-01"},
                ]
            },
            "performance": {"items": [{"projectName": "业绩A", "role": "main"}]},
            "awardsPenalties": {"awards": [{"name": "奖项A", "level": "municipal"}], "penalties": []},
        }
        created = create_bidder(ws, payload, source="panel")
        bidder_id = str(created["bidderId"])
        directory = paths.bidders_dir(ws) / str(created["dirName"])
        for name in (
            "bidder.json",
            "certificates.json",
            "performance.json",
            "awards-penalties.json",
            "meta.json",
            "profile.md",
            "attachments",
        ):
            if not (directory / name).exists():
                raise AssertionError(f"缺少文件 {name}")
        certs = jsonio.read_json(directory / "certificates.json")
        statuses = [item.get("status") for item in certs.get("items", [])]
        if statuses != ["valid", "expired"]:
            raise AssertionError(f"证书状态计算错误：{statuses}")
        checks.append("create+status")

        duplicate = create_bidder(ws, payload, source="panel")
        if not duplicate.get("alreadyExists") or len(_scan_bidder_dirs(ws)) != 1:
            raise AssertionError("create 未按 bidderId 去重")
        checks.append("idempotent_create")

        listed = list_bidders(ws)
        if listed.get("count") != 1 or listed["bidders"][0]["bidderId"] != bidder_id:
            raise AssertionError("list 结果不正确")
        checks.append("list")

        updated = update_bidder(ws, bidder_id, {"bidder": {"displayName": "自检科技（更名）"}})
        bidder_doc = jsonio.read_json(directory / "bidder.json")
        if bidder_doc.get("displayName") != "自检科技（更名）":
            raise AssertionError("update 未生效")
        checks.append("update")

        current = set_current(ws, bidder_id)
        settings = jsonio.read_json(paths.user_settings_path(ws))
        registry = jsonio.read_json(paths.registry_path(ws))
        if (
            settings.get("currentBidderId") != bidder_id
            or registry.get("currentBidderId") != bidder_id
            or current.get("currentBidderId") != bidder_id
        ):
            raise AssertionError("set-current 未同步 user-settings / registry")
        checks.append("set_current")

        report = validate_workspace(ws)
        if not report.get("valid"):
            raise AssertionError("validate 未通过")
        checks.append("validate")

        deleted = delete_bidder(ws, bidder_id, confirm=True)
        if directory.exists() or not str(deleted.get("trashedTo", "")).startswith(".trash/"):
            raise AssertionError("软删除未生效")
        settings = jsonio.read_json(paths.user_settings_path(ws))
        if settings.get("currentBidderId") != "":
            raise AssertionError("删除当前投标人后未清空 currentBidderId")
        if list_bidders(ws).get("count") != 0:
            raise AssertionError("删除后 list 仍有记录")
        checks.append("delete")

        events_text = "".join(
            line for line in jsonio.iter_lines(paths.logs_dir(ws) / f"events-{paths.date8()}.jsonl")
        )
        if "13800001111" in events_text or "6222020200001234567" in events_text:
            raise AssertionError("事件日志出现明文敏感值")
        names = event_log.event_names(ws)
        for expected in ("bidder.created", "bidder.updated", "bidder.current_changed", "bidder.deleted"):
            if expected not in names:
                raise AssertionError(f"缺少事件 {expected}")
        checks.append("event_log+mask")

        return {
            "ok": True,
            "selftest": {
                "script": "bg_bidder.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一转退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_bidder.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实工作区",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 子命令（CONTRACT §4 冻结 CLI + H.1 兼容别名）。"""
    parser = errors.CliParser(
        prog="bg_bidder.py",
        description="BidGenie 投标人 CRUD（M1）：list/create/update/delete/set-current/validate",
    )
    errors.add_common_args(parser)
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检；返回 selftest JSON")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    def add_sub(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        errors.add_common_args(sub, subparser=True)
        sub.add_argument("--workspace", required=True, help="工作区根的绝对路径")
        return sub

    add_sub("list", "列出投标人（registry 索引 + 完整度/计数）")

    create = add_sub("create", "新建投标人（以 bidderId 去重）")
    create.add_argument("--data", help="数据文件：{bidder:{...}, certificates?, performance?, awardsPenalties?}")
    create.add_argument("--from-json", dest="from_json", help="--data 的兼容别名（来源记为 agent）")
    create.add_argument("--dry-run", action="store_true", help="只输出计划，不写任何文件")

    update = add_sub("update", "更新投标人（bidder 深合并；四组可选替换）")
    update.add_argument("--bidder", required=True, help="bidderId")
    update.add_argument("--data", help="数据文件：{bidder:{patch}} 或 {patch:{...}} + 可选四组")
    update.add_argument("--from-json", dest="from_json", help="--data 的兼容别名")
    update.add_argument("--dry-run", action="store_true", help="只输出计划，不写任何文件")

    delete = add_sub("delete", "软删除投标人（移入 .trash/，需二次确认）")
    delete.add_argument("--bidder", required=True, help="bidderId")
    delete.add_argument("--confirm", action="store_true", help="二次确认（必填其一）")
    delete.add_argument("--trash", action="store_true", help="--confirm 的兼容别名")
    delete.add_argument("--dry-run", action="store_true", help="只输出计划，不写任何文件")

    set_current_parser = add_sub("set-current", "设置当前投标人（user-settings + registry 镜像）")
    set_current_parser.add_argument("--bidder", required=True, help="bidderId")

    validate = add_sub("validate", "只读校验 registry / user-settings / 投标人 JSON")
    validate.add_argument("--bidder", help="只校验指定 bidderId（缺省校验全部）")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按子命令分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if args.command is None:
        raise errors.BgError(
            errors.E_USAGE,
            "缺少子命令",
            hint="可用子命令：list/create/update/delete/set-current/validate；或运行 --selftest",
        )
    lock_timeout = errors.timeout_value(args)
    if args.command == "list":
        return list_bidders(args.workspace, lock_timeout=lock_timeout)
    if args.command == "create":
        spec, source = _data_source(args)
        return create_bidder(
            args.workspace,
            _read_payload(spec),
            source=source,
            dry_run=bool(getattr(args, "dry_run", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "update":
        spec, _source = _data_source(args)
        return update_bidder(
            args.workspace,
            str(args.bidder),
            _read_payload(spec),
            dry_run=bool(getattr(args, "dry_run", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "delete":
        return delete_bidder(
            args.workspace,
            str(args.bidder),
            confirm=bool(getattr(args, "confirm", False)),
            trash=bool(getattr(args, "trash", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "set-current":
        return set_current(
            args.workspace,
            str(args.bidder),
            dry_run=bool(getattr(args, "dry_run", False)),
            lock_timeout=lock_timeout,
        )
    if args.command == "validate":
        return validate_workspace(
            args.workspace,
            getattr(args, "bidder", None),
            lock_timeout=lock_timeout,
        )
    raise errors.BgError(errors.E_USAGE, f"未知子命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_bidder.py",
    )


if __name__ == "__main__":
    sys.exit(main())
