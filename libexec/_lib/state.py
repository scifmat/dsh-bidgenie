"""状态机唯一写者模块（PRD 4.2/4.3/4.4/4.8；``state.json`` 由本模块写入，PRD 7.2）。

设计：
- 迁移表 ``TRANSITIONS`` 为**数据驱动**（stage/from/to/guard/effects/milestone），M3+ 只增行；
- M2 完整实现 T1（project_init→info_extract）与 T2（info_extract→package_prepare）的 guard/effects；
  其余迁移在表中登记（保证 ``allowedNext`` 正确），guard 返回 `pending milestone` 检查项（M3+ 落地）；
- ``abort``/``resume`` 由 CLI 命令处理（T11/T12），不走普通 transition；
- ``state.json`` 写入前必须通过 ``schema.validate_state``（含 4.8 不变量）；
- 迁移成功同步 ``dsh/registry.json.projects[]`` 镜像（currentStage/currentPackage/updatedAt）。
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import jsonio, paths, schema, text as text_lib
from .errors import (
    E_ALREADY_IN_STAGE,
    E_GUARD_FAILED,
    E_ILLEGAL_TRANSITION,
    E_NOT_FOUND,
    E_SCHEMA_INVALID,
    E_STATE_CORRUPT,
    E_USAGE,
    BgError,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CHECKLIST_PATH = PLUGIN_ROOT / "libexec" / "checklist.json"
PACKAGE_ID_RE = re.compile(r"^package-(\d+)$")
DEFAULT_PACKAGE = "package-1"

#: F3 公共类提取文件（PRD 3.4.1）
COMMON_EXTRACT_FILES: tuple[str, ...] = (
    "01-Basic-Information.md",
    "02-Eligibility-Review.md",
    "03-Invalid_Bid-Item.md",
    "04-Compilation-Requirements.md",
    "05-Substantive-Response.md",
)
#: 每包包类提取文件（PRD 3.4.1）
PACKAGE_EXTRACT_FILES: tuple[str, ...] = (
    "06-Procurement-Content.md",
    "07-Evaluation-Criteria.md",
    "08-Business-Requirements.md",
    "09-Technical-Requirements.md",
)

#: 包目录必需子目录（PRD F4.2 Step 2 / F4.3）
PACKAGE_REQUIRED_DIRS: tuple[str, ...] = (
    "chapters",
    "chapters/_results",
    "reviews",
    "reviews/rounds",
    "resources/mermaid",
    "resources/terminal",
    "export",
)
#: package-meta 8 项 answer 的 valueSource 键（PRD 7.3）
PACKAGE_VALUE_SOURCE_KEYS: tuple[str, ...] = (
    "expectedWordCount",
    "personnelPlan",
    "equipmentPlan",
    "schedulePlan",
    "table",
    "mermaid",
    "network",
    "terminal",
)
PACKAGE_VALUE_SOURCE_VALUES: tuple[str, ...] = ("user", "agent-default", "extracted")
PACKAGE_META_REL = "package-meta.json"
#: 图表类型（与 bg_outline_validate.FIGURE_TYPES 同序；T4 配额对账用）
FIGURE_TYPES: tuple[str, ...] = ("table", "mermaid", "network", "terminal")
#: history 事件动作（自迁移类动作：不改变阶段，只进入 history 审计，不按 transition 统计）
HISTORY_EVENT_ACTIONS: tuple[str, ...] = ("set_package", "lock")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class TransitionContext:
    """一次迁移/校验的上下文。"""

    workspace: Path
    project_dir: Path
    state: dict[str, Any]
    project: dict[str, Any]
    package: str | None = None
    actor: str = "agent"
    reason: str = ""
    transition: "Transition | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    """迁移表的一行（数据驱动）。"""

    id: str
    from_stage: str
    to_stage: str
    scope: str
    title: str
    effects_note: str = ""
    guard: Callable[[TransitionContext], list[dict[str, Any]]] | None = None
    effects: Callable[[TransitionContext, dict[str, Any]], None] | None = None
    from_any_active: bool = False
    handled_by_command: str = ""
    milestone: str = "M2"


# ---------------------------------------------------------------------------
# history 记录类型（PRD 4.8.6）：transition=阶段推进/回退；event=自迁移审计动作
# ---------------------------------------------------------------------------
def is_history_event(entry: Any) -> bool:
    """判断 history 条目是**事件**而非阶段迁移（set-package/lock 等自迁移动作）。

    新记录显式带 ``kind``：``transition``=阶段迁移，``event``=事件；
    旧记录（M5.1 前）无 ``kind``：``from == to`` 的一律视为事件——正常迁移必然改变阶段，
    旧版 set-package（无 action）与 lock_draft（action=lock_draft）都满足该特征。
    """
    if not isinstance(entry, Mapping):
        return False
    kind = entry.get("kind")
    if kind == "event":
        return True
    if kind == "transition":
        return False
    return str(entry.get("from") or "") == str(entry.get("to") or "")


def history_transitions(history: Any) -> list[dict[str, Any]]:
    """只取阶段迁移记录（过滤事件；兼容无 kind 的旧记录）。"""
    if not isinstance(history, list):
        return []
    return [dict(entry) for entry in history if isinstance(entry, Mapping) and not is_history_event(entry)]


def history_events(history: Any) -> list[dict[str, Any]]:
    """只取事件记录（set_package/lock 等自迁移审计动作）。"""
    if not isinstance(history, list):
        return []
    return [dict(entry) for entry in history if isinstance(entry, Mapping) and is_history_event(entry)]


def append_history_event(
    state: dict[str, Any],
    *,
    action: str,
    reason: str,
    actor: str,
    revision: int,
    scope: str | None = None,
    at: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把一个自迁移动作写入 history（``kind=event`` + ``action``），不伪装成 X→X 迁移。

    ``from``/``to`` 仍写当前阶段（schema 要求 stage 枚举合法），但读取方用
    :func:`is_history_event` / :func:`history_transitions` 区分，不再误计为迁移。
    """
    if action not in HISTORY_EVENT_ACTIONS:
        raise BgError(
            E_USAGE,
            f"未登记的历史事件动作：{action}",
            hint=f"可用：{', '.join(HISTORY_EVENT_ACTIONS)}",
            detail={"action": action},
        )
    stage = str(state.get("currentStage") or "")
    entry: dict[str, Any] = {
        "at": at or paths.iso_now(),
        "from": stage,
        "to": stage,
        "scope": str(scope or state.get("currentScope") or "project"),
        "actor": actor,
        "reason": reason,
        "action": action,
        "kind": "event",
        "revision": revision,
    }
    if detail:
        entry["detail"] = dict(detail)
    state.setdefault("history", []).append(entry)
    return entry


# ---------------------------------------------------------------------------
# 状态文件读写（state.json 唯一写者）
# ---------------------------------------------------------------------------
def load_state(project_dir: str | Path) -> dict[str, Any]:
    """读取并校验 state.json；损坏 → E_STATE_CORRUPT（不猜修复，PRD 8.2 / M2-09）。"""
    path = Path(project_dir) / "state.json"
    if not path.is_file():
        raise BgError(
            E_NOT_FOUND,
            "项目缺少 state.json（不是有效项目目录）",
            hint="确认 --project 指向项目目录，或先运行 bg_workspace.py init-project",
            detail={"project": str(project_dir)},
        )
    document = jsonio.read_json(path, error_code=E_STATE_CORRUPT)
    if not isinstance(document, dict):
        raise BgError(
            E_STATE_CORRUPT,
            "state.json 必须是 JSON 对象",
            hint="从 dsh/backups/ 或项目备份恢复，不要手工猜测修复",
            detail={"path": "state.json"},
        )
    findings = schema.validate_state(document)
    errors, warnings = schema.split_findings(findings)
    if errors:
        raise BgError(
            E_STATE_CORRUPT,
            f"state.json 损坏或违反状态机不变量（{len(errors)} 项）",
            hint="从 dsh/backups/ 恢复；不要手工改 state.json",
            detail={
                "findings": [item.to_dict() for item in errors],
                "warnings": [item.to_dict() for item in warnings],
            },
        )
    return document


def write_state(
    project_dir: str | Path,
    state: Mapping[str, Any],
    *,
    lock: bool = True,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> None:
    """校验后原子写 state.json（本模块是唯一写者）。"""
    document = dict(state)
    if isinstance(document.get("completedStages"), list):
        document["completedStages"] = sorted(
            document["completedStages"],
            key=lambda item: (str(item.get("completedAt") or "") if isinstance(item, Mapping) else "", int(item.get("seq") or 0) if isinstance(item, Mapping) else 0),
        )
    findings = schema.validate_state(document)
    schema.raise_if_errors(findings, filename="state.json")
    jsonio.write_json_atomic(Path(project_dir) / "state.json", document, lock=lock, lock_timeout=lock_timeout)


def load_project(project_dir: str | Path) -> dict[str, Any]:
    """读取并校验 project.json。"""
    path = Path(project_dir) / "project.json"
    if not path.is_file():
        raise BgError(
            E_NOT_FOUND,
            "项目缺少 project.json（不是有效项目目录）",
            hint="确认 --project 指向项目目录，或先运行 bg_workspace.py init-project",
            detail={"project": str(project_dir)},
        )
    document = jsonio.read_json(path)
    if not isinstance(document, dict):
        raise BgError(E_SCHEMA_INVALID, "project.json 必须是 JSON 对象", detail={"path": "project.json"})
    schema.raise_if_errors(schema.validate_project(document), filename="project.json")
    return document


def read_registry(workspace: str | Path) -> dict[str, Any]:
    """读取并校验 registry.json。"""
    ws = Path(workspace)
    document = jsonio.read_json(paths.registry_path(ws))
    if not isinstance(document, dict):
        raise BgError(E_SCHEMA_INVALID, "dsh/registry.json 必须是 JSON 对象", detail={"path": "dsh/registry.json"})
    schema.raise_if_errors(schema.validate_registry(document), filename="dsh/registry.json")
    document.setdefault("projects", [])
    document.setdefault("bidders", [])
    return document


def _lookup_registry_project(workspace: Path, key: str) -> dict[str, Any] | None:
    """按 projectId 或 dirName 在 registry.projects[] 中查项目条目（宽容读取）。"""
    try:
        registry = jsonio.read_json(paths.registry_path(workspace))
    except BgError:
        return None
    if not isinstance(registry, dict):
        return None
    entries = registry.get("projects")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if key in (str(entry.get("projectId") or ""), str(entry.get("dirName") or "")):
            return dict(entry)
    return None


def resolve_project_context(
    project_arg: str,
    workspace_arg: str | None = None,
) -> tuple[Path, Path]:
    """解析 (workspace, projectDir)。

    ``--project`` 解析顺序（与进程 cwd 无关，Host 固定 cwd=插件目录）：
    ① 绝对路径直接使用；② 相对路径相对 ``--workspace`` 解析；③ ``registry.projects[]``
    按 projectId 或 dirName 匹配；④（兼容）相对当前进程 cwd。
    """
    raw = str(project_arg or "").strip()
    if not raw:
        raise BgError(E_USAGE, "缺少 --project", hint="传入项目目录、dirName 或 projectId（PRD 7.0）")
    workspace: Path | None = paths.normalize_workspace(workspace_arg) if workspace_arg else None
    candidate = Path(raw)
    project_dir: Path | None = None
    if candidate.is_absolute():
        if candidate.is_dir():
            project_dir = candidate.resolve()
    elif workspace is not None:
        joined = workspace / raw
        if joined.is_dir():
            project_dir = joined.resolve()
    if project_dir is None and workspace is not None:
        entry = _lookup_registry_project(workspace, raw)
        if entry is not None:
            dir_name = str(entry.get("dirName") or "")
            maybe = workspace / dir_name
            if dir_name and maybe.is_dir():
                project_dir = maybe.resolve()
    if project_dir is None and not candidate.is_absolute() and candidate.is_dir():
        # 兼容旧调用：相对 cwd（不推荐；Host 固定 cwd=插件目录，必须传 --workspace）
        project_dir = candidate.resolve()
    if project_dir is None:
        raise BgError(
            E_NOT_FOUND,
            f"未找到项目：{raw}",
            hint="传入项目目录绝对路径，或加 --workspace <ws> 后传 dirName / projectId",
            detail={"project": raw},
        )
    if workspace is None:
        workspace = project_dir.parent
    if not (workspace / "dsh").is_dir():
        raise BgError(
            E_NOT_FOUND,
            "工作区尚未初始化（缺少 dsh/）",
            hint="先运行 bg_workspace.py init --workspace <ws>",
            detail={"workspace": str(workspace)},
        )
    safe = paths.join_under(workspace, project_dir.name)
    if safe.resolve() != project_dir.resolve():
        raise BgError(E_USAGE, "项目目录不在工作区内", detail={"project": str(project_dir)})
    return workspace, project_dir


# ---------------------------------------------------------------------------
# 迁移表（数据驱动；PRD 4.3）
# ---------------------------------------------------------------------------
def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    label: str,
    passed: bool,
    detail: Mapping[str, Any] | None = None,
) -> None:
    checks.append({"id": check_id, "label": label, "pass": bool(passed), "detail": dict(detail or {})})


def _pending_input_check(state: Mapping[str, Any]) -> dict[str, Any]:
    pending = state.get("pendingInput")
    return {
        "id": "pending_input",
        "label": "无等待用户回答的问题（PRD 4.8.8）",
        "pass": pending is None,
        "detail": {"pendingInput": pending} if pending is not None else {},
    }


def _load_checklist_for_guard() -> dict[str, Any] | None:
    if not CHECKLIST_PATH.is_file():
        return None
    try:
        document = jsonio.read_json(CHECKLIST_PATH)
    except BgError:
        return None
    return document if isinstance(document, dict) else None


def env_check_summary(
    workspace: str | Path,
    checklist: Mapping[str, Any],
) -> dict[str, Any]:
    """按 checklist 计算必需项通过/延后情况（T1 guard 数据源：dsh/config/env-check.json）。"""
    ws = Path(workspace)
    env_path = paths.config_dir(ws) / "env-check.json"
    summary: dict[str, Any] = {
        "present": False,
        "versionMatch": False,
        "requiredPassed": 0,
        "requiredTotal": 0,
        "requiredDeferred": 0,
        "failed": [],
        "deferred": [],
        "checklistVersion": str(checklist.get("checklistVersion") or ""),
        "envChecklistVersion": "",
    }
    if not env_path.is_file():
        return summary
    try:
        document = jsonio.read_json(env_path)
    except BgError:
        return summary
    if not isinstance(document, dict) or not isinstance(document.get("items"), Mapping):
        return summary
    summary["present"] = True
    env_version = str(document.get("checklistVersion") or "")
    summary["envChecklistVersion"] = env_version
    summary["versionMatch"] = env_version == summary["checklistVersion"]
    entries = document["items"]
    for item in checklist.get("items") or []:
        if not isinstance(item, Mapping) or item.get("required") is not True:
            continue
        item_id = str(item.get("id") or "")
        summary["requiredTotal"] = int(summary["requiredTotal"]) + 1
        entry = entries.get(item_id) if isinstance(entries, Mapping) else None
        status = str((entry or {}).get("status") or "") if isinstance(entry, Mapping) else ""
        deferred_until = item.get("deferredUntil")
        if status == "passed":
            summary["requiredPassed"] = int(summary["requiredPassed"]) + 1
        elif status == "skipped" and deferred_until:
            summary["requiredDeferred"] = int(summary["requiredDeferred"]) + 1
            summary["deferred"].append(item_id)
        else:
            summary["failed"].append(item_id)
    return summary


def _current_scripts_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((PLUGIN_ROOT / "libexec").glob("bg_*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _env_check_freshness(workspace: Path, min_checked_at: datetime | None = None) -> dict[str, Any]:
    path = Path(workspace) / "dsh" / "config" / "env-check.json"
    if not path.is_file():
        return {"present": False, "fresh": False, "reason": "env-check.json 缺失"}
    try:
        document = jsonio.read_json(path)
    except BgError:
        return {"present": True, "fresh": False, "reason": "env-check.json 不可解析"}
    current_hash = _current_scripts_hash()
    recorded_hash = str(document.get("scriptsHash") or "")
    checked_at = _parse_iso(document.get("checkedAt"))
    scripts = sorted((PLUGIN_ROOT / "libexec").glob("bg_*.py"))
    latest_mtime = max((datetime.fromtimestamp(path.stat().st_mtime).astimezone() for path in scripts), default=None)
    fresh = bool(recorded_hash) and recorded_hash == current_hash
    if fresh and checked_at is not None and latest_mtime is not None:
        fresh = checked_at >= latest_mtime
    if fresh and checked_at is not None and min_checked_at is not None:
        fresh = checked_at >= min_checked_at
    return {
        "present": True,
        "fresh": fresh,
        "reason": "ok" if fresh else "scriptsHash 或 checkedAt 过期",
        "recordedScriptsHash": recorded_hash,
        "currentScriptsHash": current_hash,
        "checkedAt": document.get("checkedAt"),
    }


def _ensure_env_check_fresh(workspace: Path, min_checked_at: datetime | None = None) -> dict[str, Any]:
    """T1 前校验 env-check 新鲜度；过期则自动 bg_env_check --apply（返回刷新结果）。"""
    before = _env_check_freshness(workspace, min_checked_at)
    if before.get("fresh"):
        return {**before, "refreshed": False}
    if os.environ.get("BIDGENIE_SKIP_ENV_REFRESH") == "1":
        return {**before, "fresh": True, "refreshed": False, "skipped": "env-check 内部 selftest，禁止递归刷新"}
    script = PLUGIN_ROOT / "libexec" / "bg_env_check.py"
    command = [sys.executable, "-X", "utf8", str(script), "--apply", "--workspace", str(workspace), "--json"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=str(PLUGIN_ROOT))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**before, "refreshed": False, "refreshError": type(exc).__name__}
    after = _env_check_freshness(workspace, min_checked_at)
    return {
        **after,
        "refreshed": True,
        "refreshExitCode": completed.returncode,
        "refreshStderr": (completed.stderr or "")[-500:],
        "before": before,
    }


def _context_package(state: Mapping[str, Any], project: Mapping[str, Any], package: str | None) -> str | None:
    """目标包缺省：state.currentPackage 优先，其次 packagesToWrite/writingPackages 首个。"""
    if package:
        return str(package)
    current = str(state.get("currentPackage") or "")
    if current:
        return current
    entries = project.get("writingPackages") or project.get("packagesToWrite")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str) and entry:
                return entry
    return None


def guard_t1(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T1 guard：环境必需项 passed（带 deferredUntil 的可为 skipped）+ 项目文件存在 + 无 pendingInput。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    project_file = ctx.project_dir / "project.json"
    state_file = ctx.project_dir / "state.json"
    _check(
        checks,
        "project_files",
        "project.json / state.json 已创建且可解析",
        project_file.is_file() and state_file.is_file(),
        {"projectExists": project_file.is_file(), "stateExists": state_file.is_file()},
    )
    checklist = _load_checklist_for_guard()
    if checklist is None:
        _check(checks, "env_check_baseline", "可读取环境检查基线 checklist.json", False, {"path": "libexec/checklist.json"})
        return checks
    freshness = _ensure_env_check_fresh(ctx.workspace, _parse_iso(ctx.project.get("createdAt")))
    ctx.extra["env_freshness"] = freshness
    _check(
        checks,
        "env_check_fresh",
        "env-check 新鲜（scriptsHash/checkedAt 与当前插件一致；过期已自动 --apply）",
        bool(freshness.get("fresh")),
        freshness,
    )
    summary = env_check_summary(ctx.workspace, checklist)
    ctx.extra["env_summary"] = summary
    _check(
        checks,
        "env_check_present",
        "已生成 dsh/config/env-check.json",
        bool(summary["present"]),
        {"path": "dsh/config/env-check.json"},
    )
    _check(
        checks,
        "env_check_version",
        "环境检查清单版本与基线一致（PRD 8.4）",
        bool(summary["versionMatch"]),
        {"baseline": summary["checklistVersion"], "envCheck": summary["envChecklistVersion"]},
    )
    failed = list(summary["failed"])
    _check(
        checks,
        "env_check_required",
        "环境必需项全部 passed（带 deferredUntil 的项允许延后）",
        summary["present"] and bool(summary["versionMatch"]) and not failed,
        {
            "requiredPassed": summary["requiredPassed"],
            "requiredTotal": summary["requiredTotal"],
            "requiredDeferred": summary["requiredDeferred"],
            "deferred": summary["deferred"],
            "failed": failed,
        },
    )
    return checks


def _first_package(project: Mapping[str, Any]) -> str:
    packages = project.get("packagesToWrite")
    if isinstance(packages, list):
        for entry in packages:
            if isinstance(entry, str) and PACKAGE_ID_RE.match(entry):
                return entry
    return DEFAULT_PACKAGE


def guard_t2(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T2 guard：分包字段已回填 + 九类提取文件存在 + conversion-manifest 完整。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    project = ctx.project
    package_count = project.get("packageCount")
    fields_ok = isinstance(project.get("isSubcontracted"), bool) and isinstance(
        package_count, int
    ) and not isinstance(package_count, bool) and package_count >= 1
    _check(
        checks,
        "project_fields",
        "分包结论已回填（isSubcontracted / packageCount）",
        fields_ok,
        {"isSubcontracted": project.get("isSubcontracted"), "packageCount": package_count},
    )
    common_dir = ctx.project_dir / "extraction" / "common"
    missing_common = [name for name in COMMON_EXTRACT_FILES if not (common_dir / name).is_file()]
    _check(
        checks,
        "extract_common",
        "5 个公共类提取文件存在",
        not missing_common,
        {"dir": "extraction/common", "missing": missing_common},
    )
    count = int(package_count) if fields_ok else 1
    missing_packages: dict[str, list[str]] = {}
    for number in range(1, count + 1):
        package_dir = ctx.project_dir / "extraction" / "packages" / f"package-{number}"
        missing = [name for name in PACKAGE_EXTRACT_FILES if not (package_dir / name).is_file()]
        if missing:
            missing_packages[f"package-{number}"] = missing
    _check(
        checks,
        "extract_packages",
        "每包包类提取文件存在",
        not missing_packages,
        {"packages": count, "missing": missing_packages},
    )
    manifest_path = ctx.project_dir / "sources" / "conversion-manifest.json"
    manifest_ok = False
    manifest_detail: dict[str, Any] = {"path": "sources/conversion-manifest.json"}
    if manifest_path.is_file():
        try:
            manifest = jsonio.read_json(manifest_path)
        except BgError:
            manifest = None
        if isinstance(manifest, Mapping):
            sources = manifest.get("sources")
            manifest_ok = isinstance(sources, list) and len(sources) > 0
            manifest_detail["sources"] = len(sources) if isinstance(sources, list) else 0
        else:
            manifest_detail["reason"] = "不可解析"
    else:
        manifest_detail["reason"] = "文件缺失"
    _check(
        checks,
        "conversion_manifest",
        "conversion-manifest.json 存在且包含源文件记录",
        manifest_ok,
        manifest_detail,
    )
    return checks


def _package_dirs(project_dir: Path, package_id: str) -> list[Path]:
    """按 ``packages/package-<N>-<ts14>`` 前缀查找包目录（升序=旧→新）。"""
    root = project_dir / "packages"
    if not root.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(package_id)}-\d{{14}}$")
    return sorted(entry for entry in root.iterdir() if entry.is_dir() and pattern.match(entry.name))


def _package_meta_issues(meta: Any, package_id: str) -> list[str]:
    """校验 package-meta（PRD 7.3 / F4.3 必存清单）；返回问题列表。"""
    issues: list[str] = []
    if not isinstance(meta, Mapping):
        return ["package-meta 必须是 JSON 对象"]
    if str(meta.get("schemaVersion") or "") != "1.0":
        issues.append("schemaVersion 必须为 1.0")
    if str(meta.get("packageId") or "") != package_id:
        issues.append(f"packageId 必须为 {package_id}")
    if not isinstance(meta.get("packageNo"), int) or isinstance(meta.get("packageNo"), bool):
        issues.append("packageNo 必须是整数")
    if not isinstance(meta.get("packageName"), str):
        issues.append("packageName 必须是字符串")
    word_count = meta.get("expectedWordCount")
    if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count <= 0:
        issues.append("expectedWordCount 必须是正整数")
    for key in ("personnelPlan", "equipmentPlan"):
        if not isinstance(meta.get(key), (list, str)):
            issues.append(f"{key} 必须是字符串（用户原话）或数组")
    # M4.3：PRD 7.3 瘦字段；mustSaveMaterials/answers/extractedRequirementSummary 等旧字段不再要求
    schedule = meta.get("schedulePlan")
    if not isinstance(schedule, Mapping):
        issues.append("schedulePlan 必须是对象")
    else:
        for key in ("startDate", "deliveryDate", "constructionPlan"):
            if key not in schedule:
                issues.append(f"schedulePlan.{key} 缺失")
        # P2-9：servicePeriod=合同履行期限（“1 年/3 个月”），与 constructionPlan（工期/供货期）语义分开；
        # 旧 package-meta 无该键仍兼容（不强制），有新键时必须是字符串。
        if "servicePeriod" in schedule and not isinstance(schedule.get("servicePeriod"), str):
            issues.append("schedulePlan.servicePeriod 必须是字符串")
    quota = meta.get("figureQuota")
    if not isinstance(quota, Mapping):
        issues.append("figureQuota 必须是对象")
    else:
        for key in ("table", "mermaid", "network", "terminal"):
            value = quota.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                issues.append(f"figureQuota.{key} 必须是 >=0 整数")
    sources = meta.get("valueSource")
    if not isinstance(sources, Mapping):
        issues.append("valueSource 必须是对象")
    else:
        for key in PACKAGE_VALUE_SOURCE_KEYS:
            if str(sources.get(key) or "") not in PACKAGE_VALUE_SOURCE_VALUES:
                issues.append(f"valueSource.{key} 必须是 {'/'.join(PACKAGE_VALUE_SOURCE_VALUES)}")
    for key in ("exportStyleProfile", "exportPath"):
        if not isinstance(meta.get(key), str):
            issues.append(f"{key} 必须是字符串")
    for key in ("createdAt", "updatedAt"):
        value = str(meta.get(key) or "")
        if not value:
            issues.append(f"{key} 缺失")
    return issues


def _package_tree_status(project_dir: Path, package_id: str) -> dict[str, Any]:
    """单个包的目录/package-meta 状态（guard 与 bg_prepare --check 共用语义）。"""
    dirs = _package_dirs(project_dir, package_id)
    status: dict[str, Any] = {
        "packageId": package_id,
        "dirs": [entry.name for entry in dirs],
        "packageDir": "",
        "missingDirs": [],
        "metaPath": "",
        "metaIssues": ["缺少 packages/package-<N>-<ts14>/ 目录"],
        "ok": False,
    }
    if not dirs:
        return status
    package_dir = dirs[-1]
    status["packageDir"] = str(package_dir.relative_to(project_dir).as_posix())
    missing = [rel for rel in PACKAGE_REQUIRED_DIRS if not (package_dir / rel).is_dir()]
    status["missingDirs"] = missing
    meta_path = package_dir / PACKAGE_META_REL
    if not meta_path.is_file():
        status["metaIssues"] = ["package-meta.json 缺失"]
    else:
        status["metaPath"] = str(meta_path.relative_to(project_dir).as_posix())
        try:
            meta = jsonio.read_json(meta_path)
        except BgError as exc:
            status["metaIssues"] = [f"package-meta 不可解析：{exc.message}"]
        else:
            status["metaIssues"] = _package_meta_issues(meta, package_id)
    status["ok"] = not missing and not status["metaIssues"]
    return status


def guard_t3(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T3 guard：packagesToWrite 与包目录一致、9 类提取存在、package-meta/目录/必存清单完整。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    project = ctx.project
    packages = project.get("writingPackages")
    if not isinstance(packages, list) or not packages:
        packages = project.get("packagesToWrite")
    package_list = [entry for entry in packages if isinstance(entry, str)] if isinstance(packages, list) else []
    package_count = project.get("packageCount")
    fields_ok = (
        isinstance(project.get("isSubcontracted"), bool)
        and isinstance(package_count, int)
        and not isinstance(package_count, bool)
        and package_count >= 1
        and len(package_list) >= 1
        and all(PACKAGE_ID_RE.match(entry) for entry in package_list)
        and len(set(package_list)) == len(package_list)
        and all(1 <= int(entry.split("-")[1]) <= package_count for entry in package_list)
    )
    _check(
        checks,
        "project_fields",
        "writingPackages（或 packagesToWrite）是 packageCount 内的非空子集",
        fields_ok,
        {"isSubcontracted": project.get("isSubcontracted"), "packageCount": package_count, "writingPackages": package_list},
    )
    # 九类提取文件（F4 入口前置；T2 已校验，这里防手工删除）
    common_dir = ctx.project_dir / "extraction" / "common"
    missing_common = [name for name in COMMON_EXTRACT_FILES if not (common_dir / name).is_file()]
    missing_packages: dict[str, list[str]] = {}
    for package_id in package_list:
        package_dir = ctx.project_dir / "extraction" / "packages" / package_id
        missing = [name for name in PACKAGE_EXTRACT_FILES if not (package_dir / name).is_file()]
        if missing:
            missing_packages[package_id] = missing
    _check(
        checks,
        "extract_files",
        "9 类提取文件存在",
        not missing_common and not missing_packages,
        {"missingCommon": missing_common, "missingPackages": missing_packages},
    )
    target = ctx.package or _first_unprepared_package(ctx.project_dir, project) or (package_list[0] if package_list else "")
    _check(
        checks,
        "target_package",
        "目标包属于 packagesToWrite",
        bool(target) and target in package_list,
        {"target": target, "packagesToWrite": package_list},
    )
    missing_prepared: list[str] = []
    for package_id in package_list:
        dirs = _package_dirs(ctx.project_dir, package_id)
        if not dirs or not (dirs[-1] / "package-meta.json").is_file():
            missing_prepared.append(package_id)
    _check(
        checks,
        "all_selected_prepared",
        "全部 writingPackages 已 F4 prepare（目录+package-meta）后再执行 T3",
        not missing_prepared,
        {"missingPackages": missing_prepared, "writingPackages": package_list},
    )
    tree_status = {package_id: _package_tree_status(ctx.project_dir, package_id) for package_id in package_list}
    tree_ok = all(item.get("ok") for item in tree_status.values())
    _check(
        checks,
        "package_tree",
        "每包目录完整（chapters/_results、reviews/rounds、resources×4、export）",
        tree_ok,
        {"packages": {key: {"missingDirs": value["missingDirs"], "packageDir": value["packageDir"]} for key, value in tree_status.items()}},
    )
    meta_ok = all(not item.get("metaIssues") for item in tree_status.values())
    meta_detail = {
        "packages": {
            key: {"metaPath": value["metaPath"], "issues": value["metaIssues"]}
            for key, value in tree_status.items()
            if value.get("metaIssues")
        },
        "required": "package-meta.json（PRD 7.3：8 项答案/valueSource/exportStyleProfile 等，不含提取内容副本）",
    }
    _check(checks, "package_meta", "package-meta.json（PRD 7.3 瘦字段）schema 合法且 8 项答案齐全", meta_ok, meta_detail)
    target_dir = (tree_status.get(target) or {}).get("packageDir") if target else ""
    _check(
        checks,
        "target_package_dir",
        "目标包目录存在且含 package-meta",
        bool(target_dir) and bool((tree_status.get(target) or {}).get("metaPath")),
        {"target": target, "packageDir": target_dir},
    )
    return checks


def effect_t3(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T3 effects：completedStages += package_prepare(scope=package-N)；进入 outline。"""
    project = ctx.project
    packages = _writing_packages(project)
    target = ctx.package or _first_unprepared_package(ctx.project_dir, project) or (packages[0] if packages else DEFAULT_PACKAGE)
    if target not in packages:
        raise BgError(
            E_GUARD_FAILED,
            f"目标包不在 packagesToWrite 中：{target}",
            hint="先由 bg_project.py fields 回填 packagesToWrite",
            detail={"target": target, "packagesToWrite": packages},
        )
    _append_completed(state, "package_prepare", target, "包级撰写准备完成（目录/package-meta/必存清单）")
    state["currentStage"] = "outline"
    state["currentScope"] = target
    state["currentPackage"] = target


def _outline_validator() -> Any:
    """延迟导入 bg_outline_validate（同 libexec 目录；避免 _lib 反向硬依赖）。"""
    import importlib
    import sys

    try:
        return importlib.import_module("bg_outline_validate")
    except ImportError:
        libexec_dir = Path(__file__).resolve().parent.parent
        if str(libexec_dir) not in sys.path:
            sys.path.insert(0, str(libexec_dir))
        return importlib.import_module("bg_outline_validate")


def _figure_quota_check(stats: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    """T4 图表配额对账：table/mermaid planned >= quota；terminal 配额是参考值。

    - table/mermaid：配额是**下限**、上不封顶；
    - terminal：配额是参考值，planned 可少于参考（不强制凑数）；quota=0 时 planned 必须为 0；
    - quota=0 的类型 planned 必须为 0，避免规划禁用类型；
    - 返回 (是否通过, 明细)。
    """
    planned_raw = stats.get("figurePlanned") if isinstance(stats.get("figurePlanned"), Mapping) else {}
    quota_raw = stats.get("figureQuota") if isinstance(stats.get("figureQuota"), Mapping) else {}
    planned = {key: int(planned_raw.get(key) or 0) for key in FIGURE_TYPES}
    quota = {key: int(quota_raw.get(key) or 0) for key in FIGURE_TYPES}
    deficits = {
        key: {"planned": planned[key], "quota": quota[key]}
        for key in FIGURE_TYPES
        if key != "terminal" and planned[key] < quota[key]
    }
    over_disabled = {key: planned[key] for key in FIGURE_TYPES if quota[key] == 0 and planned[key] > 0}
    terminal_reference = {
        "planned": planned["terminal"],
        "reference": quota["terminal"],
        "note": "terminal 配额为参考值，少于参考不报错",
    } if quota["terminal"] > 0 and planned["terminal"] < quota["terminal"] else {}
    ok = bool(quota_raw) and not deficits and not over_disabled
    detail: dict[str, Any] = {
        "figurePlanned": planned,
        "figureQuota": quota,
        "allowed": "table/mermaid planned >= quota（下限）；terminal 为参考值；quota=0 时 planned=0",
    }
    if deficits:
        detail["deficits"] = deficits
    if terminal_reference:
        detail["terminalReference"] = terminal_reference
    if over_disabled:
        detail["overDisabledQuota"] = over_disabled
        detail["overDisabledHint"] = "quota=0 的图表类型不得规划；deferred 仍计入 figurePlanned，需删除或改为其它类型"
    return ok, detail


def guard_t4(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T4 guard：outline.json strict 校验 + chapters 文件树一致 + 图表配额对账（PRD 4.3/F5.1.8）。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    project = ctx.project
    packages = [entry for entry in project.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = ctx.package or str(ctx.state.get("currentPackage") or "") or (packages[0] if packages else "")
    _check(
        checks,
        "target_package",
        "目标包属于 packagesToWrite",
        bool(target) and target in packages,
        {"target": target, "packagesToWrite": packages},
    )
    package_dir = None
    dirs = _package_dirs(ctx.project_dir, target) if target else []
    if dirs:
        package_dir = dirs[-1]
    outline_path = (package_dir / "chapters" / "_outline" / "outline.json") if package_dir is not None else None
    _check(
        checks,
        "outline_exists",
        "目标包 chapters/_outline/outline.json 已由 bg_outline_apply.py 落盘（M5.1 新位置）",
        outline_path is not None and outline_path.is_file(),
        {
            "path": (str(outline_path.relative_to(ctx.project_dir).as_posix()) if outline_path is not None else ""),
            "hint": "旧项目直接运行 bg_outline_apply.py --materialize（自动迁移到 chapters/_outline/ 并告警）；仍可用 --migrate-storage 显式迁移",
        },
    )
    if outline_path is None or not outline_path.is_file() or package_dir is None:
        return checks
    meta_path = package_dir / "package-meta.json"
    try:
        package_meta = jsonio.read_json(meta_path) if meta_path.is_file() else {}
        document = jsonio.read_json(outline_path)
    except BgError as exc:
        _check(checks, "outline_schema", "outline.json 可解析", False, {"error": exc.message})
        return checks
    validator = _outline_validator()
    result = validator.validate_document(
        document,
        package_meta,
        require_layout=True,
        strict=True,
        check_tree=True,
        package_dir=package_dir,
        source=str(outline_path.relative_to(ctx.project_dir).as_posix()),
    )
    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    schema_blockers = [item for item in blockers if not str(item.get("code") or "").startswith("tree_")]
    tree_blockers = [item for item in blockers if str(item.get("code") or "").startswith("tree_")]
    _check(
        checks,
        "outline_schema",
        "outline strict 校验通过（schema/层级/覆盖/字数/图表/依赖/路径）",
        not schema_blockers and not warnings,
        {"blockers": schema_blockers, "warnings": warnings, "stats": result.get("stats") or {}},
    )
    _check(
        checks,
        "outline_tree",
        "chapters 文件树与 outline.json 一致（materialize 后）",
        not tree_blockers,
        {"blockers": tree_blockers},
    )
    stats = result.get("stats") or {}
    quota_ok, quota_detail = _figure_quota_check(stats)
    _check(
        checks,
        "figure_quota",
        "图表配额：table/mermaid 不低于 package-meta.figureQuota（下限）；terminal 为参考值；quota=0 时 planned 必须为 0",
        quota_ok,
        quota_detail,
    )
    _check(
        checks,
        "chapter_counts",
        "章节结构有效（parent/leaf 数量 >0，coverage=100%）",
        bool(stats.get("parentCount")) and bool(stats.get("leafCount")) and float(stats.get("wordDeltaPct") or 0) <= 1.0,
        {
            "parentCount": stats.get("parentCount"),
            "leafCount": stats.get("leafCount"),
            "wordDeltaPct": stats.get("wordDeltaPct"),
        },
    )
    return checks


def effect_t4(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T4 effects：completedStages += outline(scope=package-N)；进入 draft。"""
    project = ctx.project
    packages = [entry for entry in project.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = ctx.package or str(state.get("currentPackage") or "") or (packages[0] if packages else DEFAULT_PACKAGE)
    if target not in packages:
        raise BgError(
            E_GUARD_FAILED,
            f"目标包不在 packagesToWrite 中：{target}",
            detail={"target": target, "packagesToWrite": packages},
        )
    _append_completed(state, "outline", target, "大纲校验通过（strict/文件树/图表配额对账）")
    state["currentStage"] = "draft"
    state["currentScope"] = target
    state["currentPackage"] = target


def _outline_document_for_package(project_dir: Path, package_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    """读取新位置 chapters/_outline/outline.json（兼容旧的包根只读）。"""
    dirs = _package_dirs(project_dir, package_id) if package_id else []
    if not dirs:
        return None, None
    package_dir = dirs[-1]
    path = package_dir / "chapters" / "_outline" / "outline.json"
    if not path.is_file():
        legacy = package_dir / "outline.json"
        if legacy.is_file():
            path = legacy
        else:
            return path, None
    try:
        document = jsonio.read_json(path)
    except BgError:
        return path, None
    return path, document if isinstance(document, Mapping) else None


def guard_t5(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T5 guard：全部末级完成（done/skipped）、无 blocked；done summary/wordCount 完整（PRD 4.3/F5.2.6）。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    project = ctx.project
    packages = [entry for entry in project.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = ctx.package or str(ctx.state.get("currentPackage") or "") or (packages[0] if packages else "")
    _check(
        checks,
        "target_package",
        "目标包属于 packagesToWrite",
        bool(target) and target in packages,
        {"target": target, "packagesToWrite": packages},
    )
    outline_path, document = _outline_document_for_package(ctx.project_dir, target) if target else (None, None)
    _check(
        checks,
        "outline_exists",
        "chapters/_outline/outline.json 存在且可解析",
        outline_path is not None and document is not None,
        {"path": (str(outline_path.relative_to(ctx.project_dir).as_posix()) if outline_path is not None else "")},
    )
    if document is None:
        return checks
    nodes = [
        node
        for node in _iter_outline_nodes(document.get("chapters") or [])
        if not (isinstance(node.get("children"), list) and node.get("children"))
    ]
    blocked: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    missing_meta: list[dict[str, Any]] = []
    skipped_missing_reason: list[dict[str, Any]] = []
    done_count = 0
    skipped_count = 0
    for node in nodes:
        chapter_id = str(node.get("id") or "")
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        status = str(completion.get("status") or "pending")
        if status == "blocked":
            blocked.append({"chapterId": chapter_id, "blockReason": completion.get("blockReason", "")})
            continue
        if status not in ("done", "skipped"):
            incomplete.append({"chapterId": chapter_id, "status": status})
            continue
        if status == "done":
            done_count += 1
            summary = completion.get("summary")
            word_count = completion.get("wordCount")
            if not isinstance(summary, str) or not summary.strip():
                missing_meta.append({"chapterId": chapter_id, "issue": "summary 为空"})
            if not isinstance(word_count, int) or isinstance(word_count, bool) or word_count <= 0:
                missing_meta.append({"chapterId": chapter_id, "issue": "wordCount 必须 >0"})
        else:
            skipped_count += 1
            if not str(completion.get("skipReason") or "").strip():
                skipped_missing_reason.append({"chapterId": chapter_id, "issue": "skipped 缺少 skipReason"})
    _check(
        checks,
        "leaf_count",
        "大纲存在末级章节",
        bool(nodes),
        {"leafCount": len(nodes)},
    )
    _check(
        checks,
        "no_blocked",
        "不存在 blocked 章节（T5 前必须清零）",
        not blocked,
        {"blocked": blocked},
    )
    _check(
        checks,
        "all_completed",
        "所有末级 completion.status ∈ {done, skipped}",
        bool(nodes) and not incomplete,
        {"incomplete": incomplete, "done": done_count, "skipped": skipped_count},
    )
    _check(
        checks,
        "done_meta",
        "done 章节 summary 非空且 wordCount >0",
        not missing_meta,
        {"issues": missing_meta},
    )
    _check(
        checks,
        "skipped_reason",
        "skipped 章节均有 skipReason",
        not skipped_missing_reason,
        {"issues": skipped_missing_reason},
    )
    return checks


def _iter_outline_nodes(chapters: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if not isinstance(chapters, list):
        return nodes
    for node in chapters:
        if not isinstance(node, dict):
            continue
        nodes.append(node)
        children = node.get("children")
        if isinstance(children, list):
            nodes.extend(_iter_outline_nodes(children))
    return nodes


def effect_t5(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T5 effects：completedStages += draft(scope=package-N)；进入 review；reviewLoop.round 首轮置 1。"""
    project = ctx.project
    packages = [entry for entry in project.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = ctx.package or str(state.get("currentPackage") or "") or (packages[0] if packages else DEFAULT_PACKAGE)
    if target not in packages:
        raise BgError(
            E_GUARD_FAILED,
            f"目标包不在 packagesToWrite 中：{target}",
            detail={"target": target, "packagesToWrite": packages},
        )
    _append_completed(state, "draft", target, "正文全部章节完成（done/skipped），无 blocked")
    state["currentStage"] = "review"
    state["currentScope"] = target
    state["currentPackage"] = target
    review_loop = dict(state.get("reviewLoop") or {})
    packages_rounds = dict(review_loop.get("packages") or {}) if isinstance(review_loop.get("packages"), Mapping) else {}
    entry = dict(packages_rounds.get(target) or {})
    previous_round = entry.get("round")
    previous_round = previous_round if isinstance(previous_round, int) and not isinstance(previous_round, bool) else 0
    entry["round"] = 1 if previous_round == 0 else previous_round + 1
    entry.setdefault("closedRound", None)
    max_rounds = _review_max_rounds_from_workspace(ctx.workspace)
    packages_rounds[target] = entry
    review_loop["maxRounds"] = max_rounds
    review_loop["packages"] = packages_rounds
    review_loop["round"] = entry["round"]
    review_loop["closedRound"] = entry.get("closedRound")
    state["reviewLoop"] = review_loop


def _review_issues_module() -> Any:
    """延迟导入 bg_review_issues（同 libexec 目录，避免 _lib 反向硬依赖）。"""
    import importlib
    import sys

    try:
        return importlib.import_module("bg_review_issues")
    except ImportError:
        libexec_dir = Path(__file__).resolve().parent.parent
        if str(libexec_dir) not in sys.path:
            sys.path.insert(0, str(libexec_dir))
        return importlib.import_module("bg_review_issues")


def _load_review_context(
    ctx: TransitionContext,
) -> tuple[str, list[dict[str, Any]], Path | None, dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """返回 (target, checks, package_dir, issues_doc, outline_doc, extra_checks)。"""
    checks: list[dict[str, Any]] = []
    project = ctx.project
    packages = [entry for entry in project.get("packagesToWrite") or [] if isinstance(entry, str)]
    target = ctx.package or str(ctx.state.get("currentPackage") or "") or (packages[0] if packages else "")
    checks.append(
        {
            "id": "target_package",
            "label": "目标包属于 packagesToWrite",
            "pass": bool(target) and target in packages,
            "detail": {"target": target, "packagesToWrite": packages},
        }
    )
    dirs = _package_dirs(ctx.project_dir, target) if target else []
    package_dir = dirs[-1] if dirs else None
    issues_doc: dict[str, Any] | None = None
    outline_doc: dict[str, Any] | None = None
    if package_dir is None:
        checks.append({"id": "package_dir", "label": "目标包目录存在", "pass": False, "detail": {"target": target}})
        return target, checks, package_dir, issues_doc, outline_doc, []
    try:
        module = _review_issues_module()
        issues_doc = module.load_issues(package_dir)
    except BgError as exc:
        checks.append({"id": "issues_json", "label": "reviews/issues.json 合法可解析", "pass": False, "detail": {"error": exc.message}})
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "issues_json", "label": "reviews/issues.json 合法可解析", "pass": False, "detail": {"error": str(exc)}})
    if issues_doc is not None:
        checks.append(
            {
                "id": "issues_json",
                "label": "reviews/issues.json 合法可解析",
                "pass": True,
                "detail": {"round": issues_doc.get("round"), "layersRun": issues_doc.get("layersRun"), "summary": issues_doc.get("summary")},
            }
        )
    outline_path = package_dir / "chapters" / "_outline" / "outline.json"
    if outline_path.is_file():
        try:
            outline_doc = jsonio.read_json(outline_path)
        except BgError:
            outline_doc = None
    return target, checks, package_dir, issues_doc, outline_doc, []


def _review_max_rounds_from_workspace(workspace: Path) -> int:
    settings_path = Path(workspace) / "dsh" / "config" / "user-settings.json"
    if not settings_path.is_file():
        return 5
    try:
        settings = jsonio.read_json(settings_path)
    except BgError:
        return 5
    if not isinstance(settings, Mapping):
        return 5
    value = settings.get("maxReviewRounds")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        review = settings.get("review") if isinstance(settings.get("review"), Mapping) else {}
        value = review.get("maxRounds")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 5


def _review_package_rounds(review_loop: Mapping[str, Any], package: str) -> dict[str, Any] | None:
    packages = review_loop.get("packages") if isinstance(review_loop.get("packages"), Mapping) else {}
    entry = packages.get(package)
    return dict(entry) if isinstance(entry, Mapping) else None


def _review_round(state: Mapping[str, Any]) -> tuple[int, int]:
    review_loop = state.get("reviewLoop") if isinstance(state.get("reviewLoop"), Mapping) else {}
    package = str(state.get("currentPackage") or "")
    entry = _review_package_rounds(review_loop, package) if package else None
    round_value = entry.get("round") if entry is not None else review_loop.get("round")
    round_value = round_value if isinstance(round_value, int) and not isinstance(round_value, bool) else 0
    max_rounds = review_loop.get("maxRounds")
    max_rounds = max_rounds if isinstance(max_rounds, int) and not isinstance(max_rounds, bool) and max_rounds > 0 else 5
    return round_value, max_rounds


def guard_t6(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T6 review→optimize：issues 合法且 openCount>0；轮次上限内。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    target, extra, package_dir, issues_doc, outline_doc, _ = _load_review_context(ctx)
    checks.extend(extra)
    round_value, max_rounds = _review_round(ctx.state)
    if issues_doc is None:
        return checks
    summary = issues_doc.get("summary") if isinstance(issues_doc.get("summary"), Mapping) else {}
    open_count = summary.get("openCount")
    open_count = open_count if isinstance(open_count, int) else 0
    _check(
        checks,
        "open_issues",
        "issues.summary.openCount > 0（需要进入优化）",
        open_count > 0,
        {"openCount": open_count, "summary": summary},
    )
    _check(checks, "review_round", "reviewLoop.round 在 (0, maxRounds] 内", 0 < round_value < max_rounds, {"round": round_value, "maxRounds": max_rounds})
    if round_value >= max_rounds:
        checks.append(
            {
                "id": "max_rounds",
                "label": f"未收敛保护：round={round_value} >= maxRounds={max_rounds}",
                "pass": False,
                "detail": {"hint": "输出未收敛报告，等待用户决定继续/放宽/人工接管；不得自动通过"},
            }
        )
    return checks


def effect_t6(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T6 effects：completedStages += review(本轮问题数)；进入 optimize。"""
    target, _, _, issues_doc, _, _ = _load_review_context(ctx)
    summary = (issues_doc or {}).get("summary") if isinstance((issues_doc or {}).get("summary"), Mapping) else {}
    round_value, _ = _review_round(state)
    _append_completed(state, "review", target, f"第 {round_value} 轮审查发现开放问题 {summary.get('openCount', 0)} 个，进入优化")
    state["currentStage"] = "optimize"
    state["currentScope"] = target
    state["currentPackage"] = target


def guard_t7(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T7 review→finalize：4.6 通过判据成立。

    coverage 判据由 bg_review_issues.evaluate_pass 统一执行：只要求末级 leaf coverage ∈ pass/notApplicable，
    父级跳过（M7 修复：父级不参与 coverageUpdates/T7 判定）。
    """
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    target, extra, package_dir, issues_doc, outline_doc, _ = _load_review_context(ctx)
    checks.extend(extra)
    if issues_doc is None:
        return checks
    module = _review_issues_module()
    try:
        result = module.evaluate_pass(issues_doc, outline_doc)
    except Exception as exc:  # noqa: BLE001
        checks.append({"id": "pass_criteria", "label": "4.6 通过判据可计算", "pass": False, "detail": {"error": str(exc)}})
        return checks
    _check(checks, "pass_criteria", "4.6 通过判据成立（blocker/major/minor 全部关闭/verified/wontfix+确认；layersRun/coverage）", bool(result.get("pass")), {"reasons": result.get("reasons"), "summary": result.get("summary")})
    if package_dir is not None:
        report_path = package_dir / "reviews" / "review-report.md"
        layer_status = issues_doc.get("layerStatus") if isinstance(issues_doc.get("layerStatus"), Mapping) else {}
        report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        match = re.search(r"- A 层：\*\*(.*?)\*\*；B 层：\*\*(.*?)\*\*；C 层：\*\*(.*?)\*\*", report_text)
        expected = {
            "script": str(layer_status.get("script") or "skipped"),
            "coverage": str(layer_status.get("coverage") or "skipped"),
            "graphic": str(layer_status.get("graphic") or "skipped"),
        }
        actual = {"script": match.group(1), "coverage": match.group(2), "graphic": match.group(3)} if match else {}
        _check(
            checks,
            "report_layer_status",
            "review-report.md 的 A/B/C 层状态与 issues.layerStatus 一致",
            (not match) or actual == expected,
            {"report": actual, "issues": expected, "skipped": not bool(match)},
        )
        pending_layers = issues_doc.get("pendingLayers") if isinstance(issues_doc.get("pendingLayers"), list) else []
        _check(
            checks,
            "report_pending_consistent",
            "无 pendingLayers 时 review-report 不再保留“待审占位”矛盾句",
            (not match) or bool(pending_layers) or ("待审占位" not in report_text),
            {"pendingLayers": pending_layers, "skipped": not bool(match)},
        )
    return checks


def effect_t7(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T7 effects：completedStages += review(通过)；closedRound=round；进入 finalize。"""
    target, _, _, issues_doc, _, _ = _load_review_context(ctx)
    round_value, _ = _review_round(state)
    note = f"第 {round_value} 轮审查通过（4.6 判据）"
    existing = next(
        (
            entry
            for entry in state.get("completedStages") or []
            if isinstance(entry, Mapping) and entry.get("stage") == "review" and entry.get("scope") == target
        ),
        None,
    )
    if isinstance(existing, dict):
        existing["note"] = note
        existing["completedAt"] = paths.iso_now()
    else:
        _append_completed(state, "review", target, note)
    review_loop = dict(state.get("reviewLoop") or {})
    packages_rounds = dict(review_loop.get("packages") or {}) if isinstance(review_loop.get("packages"), Mapping) else {}
    entry = dict(packages_rounds.get(target) or {"round": round_value})
    entry["closedRound"] = round_value
    packages_rounds[target] = entry
    review_loop["packages"] = packages_rounds
    review_loop["closedRound"] = round_value
    review_loop.setdefault("maxRounds", 5)
    state["reviewLoop"] = review_loop
    state["currentStage"] = "finalize"
    state["currentScope"] = target
    state["currentPackage"] = target


def guard_t8(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T8 optimize→review：openCount==0、blocker/major fixed/verified 或 wontfix+确认、optimization 记录存在、轮次上限内。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    target, extra, package_dir, issues_doc, outline_doc, _ = _load_review_context(ctx)
    checks.extend(extra)
    round_value, max_rounds = _review_round(ctx.state)
    if issues_doc is None or package_dir is None:
        return checks
    module = _review_issues_module()
    issues = [item for item in issues_doc.get("issues") or [] if isinstance(item, Mapping)]
    summary = module.recalc_summary(issues)
    _check(checks, "open_zero", "issues.summary.openCount == 0", int(summary.get("openCount") or 0) == 0, {"summary": summary})
    unverified: list[dict[str, Any]] = []
    for issue in issues:
        severity = str(issue.get("severity") or "")
        status = str(issue.get("status") or "open")
        if severity in ("blocker", "major"):
            if status not in ("fixed", "verified") and not (status == "wontfix" and issue.get("userConfirmed")):
                unverified.append({"issueId": issue.get("issueId"), "severity": severity, "status": status})
        elif severity == "minor":
            if status not in ("fixed", "verified") and not (status == "wontfix" and issue.get("userConfirmed")):
                unverified.append({"issueId": issue.get("issueId"), "severity": severity, "status": status})
    _check(
        checks,
        "verified_issues",
        "blocker/major 已 fixed/verified（或 wontfix+userConfirmed）；minor 已 fixed/verified/wontfix+确认",
        not unverified,
        {"unverified": unverified[:20]},
    )
    optimization_path = package_dir / "reviews" / "rounds" / f"round-{round_value}.optimization.json"
    optimization_ok = False
    if optimization_path.is_file():
        try:
            document = jsonio.read_json(optimization_path)
            optimization_ok = isinstance(document, (Mapping, list))
        except BgError:
            optimization_ok = False
    _check(
        checks,
        "optimization_record",
        f"本轮优化记录存在：reviews/rounds/round-{round_value}.optimization.json",
        optimization_ok,
        {"path": str(optimization_path.relative_to(ctx.project_dir).as_posix()) if package_dir is not None else ""},
    )
    _check(checks, "round_increment", "reviewLoop.round < maxRounds（可进入下一轮审查）", round_value < max_rounds, {"round": round_value, "maxRounds": max_rounds})
    return checks


def _count_cjk_words(text: str) -> int:
    """统一字数口径（P2-10）：转发 _lib.text.count_body_words，避免多份实现漂移。"""
    return text_lib.count_body_words(text)


def _recompute_outline_wordcounts(ctx: TransitionContext, target: str) -> int:
    """优化后统一复算：按章 md 的 CJK 口径回写 completion/wordCount、chapter-result、stats。"""
    dirs = _package_dirs(ctx.project_dir, target)
    if not dirs:
        return 0
    package_dir = dirs[-1]
    outline_path = package_dir / "chapters" / "_outline" / "outline.json"
    if not outline_path.is_file():
        return 0
    try:
        outline = jsonio.read_json(outline_path)
    except BgError:
        return 0
    total = 0
    for node in _iter_outline_nodes(outline.get("chapters") or []):
        children = node.get("children")
        if isinstance(children, list) and children:
            continue
        file_path = package_dir / str(node.get("filePath") or "")
        if not file_path.is_file():
            continue
        counted = _count_cjk_words(file_path.read_text(encoding="utf-8-sig"))
        total += counted
        completion = node.get("completion")
        if isinstance(completion, Mapping):
            completion = dict(completion)
            completion["wordCount"] = counted
            node["completion"] = completion
    stats = dict(outline.get("stats") or {})
    stats["writtenWordTotal"] = total
    stats["actualWordCount"] = total
    outline["stats"] = stats
    outline["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(outline_path, outline)
    for node in _iter_outline_nodes(outline.get("chapters") or []):
        children = node.get("children")
        if isinstance(children, list) and children:
            continue
        chapter_id = str(node.get("id") or "")
        result_path = package_dir / "chapters" / "_results" / f"{chapter_id}.chapter-result.json"
        if not result_path.is_file():
            continue
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        try:
            result = jsonio.read_json(result_path)
        except BgError:
            continue
        if isinstance(result, dict):
            result["wordCount"] = int(completion.get("wordCount") or 0)
            jsonio.write_json_atomic(result_path, result)
    return total


def effect_t8(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T8 effects：completedStages += optimize；进入 review；reviewLoop.round +=1（单调）。"""
    target, _, _, issues_doc, _, _ = _load_review_context(ctx)
    round_value, _ = _review_round(state)
    summary = (issues_doc or {}).get("summary") if isinstance((issues_doc or {}).get("summary"), Mapping) else {}
    _append_completed(state, "optimize", target, f"第 {round_value} 轮优化完成（open=0，修复 {summary.get('fixedCount', 0) + summary.get('verifiedCount', 0)} 项）")
    state["currentStage"] = "review"
    state["currentScope"] = target
    state["currentPackage"] = target
    review_loop = dict(state.get("reviewLoop") or {})
    packages_rounds = dict(review_loop.get("packages") or {}) if isinstance(review_loop.get("packages"), Mapping) else {}
    entry = dict(packages_rounds.get(target) or {"round": round_value})
    entry["round"] = round_value + 1
    packages_rounds[target] = entry
    review_loop["packages"] = packages_rounds
    review_loop["round"] = round_value + 1
    review_loop.setdefault("maxRounds", 5)
    state["reviewLoop"] = review_loop
    _recompute_outline_wordcounts(ctx, target)


def _finalize_module() -> Any:
    import importlib
    import sys

    try:
        return importlib.import_module("bg_finalize")
    except ImportError:
        libexec_dir = Path(__file__).resolve().parent.parent
        if str(libexec_dir) not in sys.path:
            sys.path.insert(0, str(libexec_dir))
        return importlib.import_module("bg_finalize")


def _finalize_context(ctx: TransitionContext) -> tuple[str, Path | None, dict[str, Any]]:
    target = ctx.package or str(ctx.state.get("currentPackage") or "")
    packages = [entry for entry in ctx.project.get("packagesToWrite") or [] if isinstance(entry, str)]
    if not target and packages:
        target = packages[0]
    dirs = _package_dirs(ctx.project_dir, target) if target else []
    package_dir = dirs[-1] if dirs else None
    checks = [
        {"id": "target_package", "label": "目标包属于 packagesToWrite", "pass": bool(target) and target in packages, "detail": {"target": target, "packagesToWrite": packages}},
    ]
    return target, package_dir, {"checks": checks}


def guard_t9(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T9 finalize→export：锁定标记 + state.locks.draftLocked + 冻结清单 verify 通过。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    target, package_dir, context = _finalize_context(ctx)
    checks.extend(context["checks"])
    if package_dir is None:
        checks.append({"id": "package_dir", "label": "目标包目录存在", "pass": False, "detail": {"target": target}})
        return checks
    locks = ctx.state.get("locks") if isinstance(ctx.state.get("locks"), Mapping) else {}
    _check(checks, "draft_locked", "state.locks.draftLocked == true", bool(locks.get("draftLocked")), {"locks": locks})
    lock_marker = package_dir / "chapters" / ".locked"
    _check(checks, "lock_marker", "chapters/.locked 存在", lock_marker.is_file(), {"path": "chapters/.locked"})
    module = _finalize_module()
    verification: Mapping[str, Any] = {"ok": False, "findings": []}
    try:
        verification = module.verify_manifest(package_dir)
    except BgError as exc:
        verification = {"ok": False, "findings": [{"error": exc.message}]}
    except Exception as exc:  # noqa: BLE001
        verification = {"ok": False, "findings": [{"error": str(exc)}]}
    _check(
        checks,
        "frozen_manifest",
        "export/_frozen/manifest.json 与当前文件哈希一致",
        bool(verification.get("ok")),
        {"fileCount": verification.get("fileCount"), "findings": (verification.get("findings") or [])[:10]},
    )
    return checks


def effect_t9(ctx: TransitionContext, state: dict[str, Any]) -> None:
    target, package_dir, _ = _finalize_context(ctx)
    manifest_locked_at = None
    if package_dir is not None:
        try:
            manifest = jsonio.read_json(package_dir / "export" / "_frozen" / "manifest.json")
            manifest_locked_at = manifest.get("lockedAt") if isinstance(manifest, Mapping) else None
        except BgError:
            manifest_locked_at = None
    _append_completed(state, "finalize", target, "定稿锁定完成：冻结清单生成并通过 verify，正文进入禁写")
    locks = dict(state.get("locks") or {})
    locks["draftLocked"] = True
    locks.setdefault("lockedAt", manifest_locked_at or paths.iso_now())
    locks.setdefault("lockReason", "定稿锁定")
    state["locks"] = locks
    state["currentStage"] = "export"
    state["currentScope"] = target
    state["currentPackage"] = target


def guard_t10(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T10 export→completed：docx 已生成且导出报告自检通过，package-meta.exportPath 已回填。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    target, package_dir, context = _finalize_context(ctx)
    checks.extend(context["checks"])
    if package_dir is None:
        checks.append({"id": "package_dir", "label": "目标包目录存在", "pass": False, "detail": {"target": target}})
        return checks
    meta_path = package_dir / "package-meta.json"
    meta: Mapping[str, Any] = {}
    try:
        meta = jsonio.read_json(meta_path)
    except BgError as exc:
        checks.append({"id": "package_meta", "label": "package-meta.json 可解析", "pass": False, "detail": {"error": exc.message}})
    export_path = meta.get("exportPath") if isinstance(meta, Mapping) else None
    docx_path: Path | None = None
    if isinstance(export_path, str) and export_path.strip():
        candidate = Path(export_path)
        options = [candidate] if candidate.is_absolute() else [ctx.project_dir / candidate, package_dir / candidate]
        docx_path = next((path for path in options if path.is_file()), None)
    _check(
        checks,
        "export_path",
        "package-meta.exportPath 已回填且 docx 文件存在",
        docx_path is not None,
        {"exportPath": export_path, "packageId": target},
    )
    report_path = package_dir / "export" / "_last-export-report.json"
    report: Mapping[str, Any] = {}
    if report_path.is_file():
        try:
            loaded = jsonio.read_json(report_path)
            report = loaded if isinstance(loaded, Mapping) else {}
        except BgError:
            report = {}
    report_ok = bool(report) and report.get("mode") == "tech" and report.get("verifyOk") is True
    if isinstance(report.get("findings"), list) and report.get("findings"):
        report_ok = False
    _check(checks, "export_report", "export/_last-export-report.json 为 tech 模式且 verifyOk=true", report_ok, {"report": {"mode": report.get("mode"), "verifyOk": report.get("verifyOk"), "verifyFindings": report.get("verifyFindings")}})
    # 多包/完整导出：若已生成 keyinfo/overview docx，则对应 mode 报告必须 verifyOk=true
    for mode_name, pattern in (("keyinfo", "*关键信息*.docx"), ("overview", "*工作概览*.docx")):
        mode_docx = list((package_dir / "export").glob(pattern)) if (package_dir / "export").is_dir() else []
        if not mode_docx:
            continue
        mode_report_path = package_dir / "export" / f"_last-export-report-{mode_name}.json"
        mode_report: Mapping[str, Any] = {}
        if mode_report_path.is_file():
            try:
                loaded = jsonio.read_json(mode_report_path)
                mode_report = loaded if isinstance(loaded, Mapping) else {}
            except BgError:
                mode_report = {}
        mode_ok = mode_report.get("mode") == mode_name and mode_report.get("verifyOk") is True
        _check(
            checks,
            f"{mode_name}_report",
            f"{mode_name} 导出报告存在且 verifyOk=true",
            mode_ok,
            {"report": str(mode_report_path.name), "mode": mode_report.get("mode"), "verifyOk": mode_report.get("verifyOk")},
        )
    overview_report_path = package_dir / "export" / "_last-overview-report.json"
    if overview_report_path.is_file():
        overview_report = {}
        try:
            loaded = jsonio.read_json(overview_report_path)
            overview_report = loaded if isinstance(loaded, Mapping) else {}
        except BgError:
            overview_report = {}
        tech_docx_path = report.get("docxPath")
        tech_docx = None
        if tech_docx_path:
            rel = str(tech_docx_path).replace("\\", "/")
            base = package_dir.parent.parent if rel.startswith("packages/") else package_dir
            tech_docx = base / rel
        tech_sha = ""
        if tech_docx is not None and tech_docx.is_file():
            tech_sha = "sha256:" + hashlib.sha256(tech_docx.read_bytes()).hexdigest()
        freshness_ok = (
            report.get("mode") == "tech"
            and overview_report.get("techDocxPath") == report.get("docxPath")
            and bool(overview_report.get("techDocxSha256"))
            and overview_report.get("techDocxSha256") == tech_sha
            and overview_report.get("techVerifyOk") is True
            and str(overview_report.get("techExportedAt") or "") == str(report.get("exportedAt") or "")
            and overview_report.get("techVerifyFindings") == report.get("verifyFindings")
        )
        _check(
            checks,
            "overview_tech_freshness",
            "overview 引用的 tech docx/sha/verifyOk/findings/时间与最新 tech 报告一致",
            freshness_ok,
            {"overview": {key: overview_report.get(key) for key in ("techDocxPath", "techDocxSha256", "techVerifyOk", "techVerifyFindings", "techExportedAt")}, "tech": {"docxPath": report.get("docxPath"), "sha256": tech_sha, "exportedAt": report.get("exportedAt"), "verifyOk": report.get("verifyOk")}},
        )
    tmp_files = sorted(path.name for path in (package_dir / "export").glob("*.tmp*")) if (package_dir / "export").is_dir() else []
    _check(checks, "no_tmp_files", "export 目录无 .tmp 残留", not tmp_files, {"tmpFiles": tmp_files[:10]})
    return checks


def effect_t10(ctx: TransitionContext, state: dict[str, Any]) -> None:
    target, _package_dir, _ = _finalize_context(ctx)
    _append_completed(state, "export", target, "导出完成且自检通过")
    state["currentStage"] = "completed"
    state["currentScope"] = target
    state["currentPackage"] = target
    state["completedAt"] = paths.iso_now()
    selected = _writing_packages(ctx.project)
    _update_project_progress(ctx, state, status="package_completed", package=target, package_status="completed")
    project_after = load_project(ctx.project_dir)
    completed = _completed_packages(project_after, {**state, "currentStage": "completed", "currentPackage": target})
    all_done = bool(selected) and all(package in completed for package in selected)
    _update_project_progress(ctx, state, status="completed" if all_done else "package_completed", package=target, package_status="completed")


def lock_draft(
    project_dir: str | Path,
    *,
    reason: str,
    manifest_path: str,
    root_hash: str,
    actor: str = "script:bg_finalize.py",
    package_id: str | None = None,
    locked_at: str | None = None,
) -> dict[str, Any]:
    """经 state 模块写 locks（唯一写者），并把 lock 记为 history **事件**（P3-13）。"""
    project_dir_path = Path(project_dir)
    state = load_state(project_dir_path)
    now = paths.iso_now()
    locks = dict(state.get("locks") or {})
    locks.update({"draftLocked": True, "lockedAt": locked_at or now, "lockReason": reason, "manifestPath": manifest_path, "rootHash": root_hash})
    if package_id:
        locks["package"] = package_id
        if not str(manifest_path).startswith("packages/"):
            locks["manifestPath"] = f"packages/{package_id}/{manifest_path}"
    state["locks"] = locks
    next_revision = int(state.get("revision") or 0) + 1
    # P3-13：lock 是自迁移动作，记为 event（不是 finalize→finalize 迁移）
    append_history_event(
        state,
        action="lock",
        reason=reason,
        actor=actor,
        revision=next_revision,
        scope=package_id or str(state.get("currentPackage") or state.get("currentScope") or "package-1"),
        at=now,
        detail={"rootHash": root_hash, "manifestPath": locks.get("manifestPath")},
    )
    state["revision"] = next_revision
    state["updatedAt"] = now
    state["updatedBy"] = actor
    write_state(project_dir_path, state)
    return state


def unlock_draft(
    project_dir: str | Path,
    *,
    reason: str,
    actor: str = "script:bg_finalize.py",
) -> dict[str, Any]:
    """R4：解除锁定并回到 optimize（不删除历史；_frozen 归档由调用方完成）。"""
    project_dir_path = Path(project_dir)
    state = load_state(project_dir_path)
    now = paths.iso_now()
    locks = dict(state.get("locks") or {})
    previous = dict(locks)
    locks["draftLocked"] = False
    locks["lockedAt"] = None
    locks["lockReason"] = ""
    state["locks"] = locks
    previous_stage = state.get("currentStage")
    state["currentStage"] = "optimize"
    state["currentScope"] = str(state.get("currentPackage") or state.get("currentScope") or "package-1")
    next_revision = int(state.get("revision") or 0) + 1
    state.setdefault("history", []).append(
        {
            "at": now,
            "from": previous_stage,
            "to": "optimize",
            "scope": state["currentScope"],
            "actor": actor,
            "reason": reason,
            "action": "unlock_draft",
            "kind": "transition",
            "revision": next_revision,
            "detail": {"previousLock": previous},
        }
    )
    state["revision"] = next_revision
    state["updatedAt"] = now
    state["updatedBy"] = actor
    write_state(project_dir_path, state)
    return state


def _writing_packages(project: Mapping[str, Any]) -> list[str]:
    raw = project.get("writingPackages")
    if not isinstance(raw, list) or not raw:
        raw = project.get("packagesToWrite")
    return [str(item) for item in raw or [] if isinstance(item, str)]


def _package_progress(project: Mapping[str, Any]) -> dict[str, Any]:
    extensions = project.get("extensions") if isinstance(project.get("extensions"), Mapping) else {}
    progress = extensions.get("packageProgress") if isinstance(extensions.get("packageProgress"), Mapping) else {}
    return {str(key): dict(value) if isinstance(value, Mapping) else value for key, value in progress.items()}


def _completed_packages(project: Mapping[str, Any], state: Mapping[str, Any]) -> set[str]:
    completed: set[str] = set()
    for package, entry in _package_progress(project).items():
        if isinstance(entry, Mapping) and str(entry.get("status") or "") == "completed":
            completed.add(package)
    history_completed: set[str] = set()
    for entry in history_transitions(state.get("history")):
        if str(entry.get("to") or "") == "completed" and str(entry.get("scope") or "").startswith("package-"):
            history_completed.add(str(entry["scope"]))
    completed |= history_completed
    # P1-2：currentStage=completed 且 currentPackage 存在时，旧实现无条件把当前包算作已完成；
    # 但用户可能已先 set-package 到下一包，导致 T13 把“下一包”误判为已完成而失败。
    # 仅在没有 completed 历史（旧状态/手工状态）时才按 currentPackage 兜底。
    if state.get("currentStage") == "completed" and str(state.get("currentPackage") or "") and not history_completed:
        completed.add(str(state["currentPackage"]))
    return completed


def _first_unprepared_package(project_dir: Path, project: Mapping[str, Any]) -> str:
    selected = _writing_packages(project)
    for package_id in selected:
        dirs = _package_dirs(project_dir, package_id)
        if not dirs or not (dirs[-1] / "package-meta.json").is_file():
            return package_id
    return selected[0] if selected else DEFAULT_PACKAGE


def _t13_progress(project: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[str | None, list[str], str, str, bool]:
    """T13 推进上下文：``(下一包, selected, current, effectiveCurrent, recovered)``。

    P1-2：若用户已先 `set-package` 到下一包（current 未完成、紧邻的前一包已完成），
    T13 仍按“上一已完成包 → current”推进，并标记 recovered=True；guard 会输出提示，
    effect 用 effectiveCurrent 记录前包锁信息。
    """
    selected = _writing_packages(project)
    current = str(state.get("currentPackage") or "")
    completed = _completed_packages(project, state)
    effective = current
    recovered = False
    if current in selected and current not in completed:
        index = selected.index(current)
        if index > 0 and selected[index - 1] in completed:
            effective = selected[index - 1]
            recovered = True
    if not selected or effective not in selected:
        return None, selected, current, effective, recovered
    index = selected.index(effective)
    if index + 1 >= len(selected):
        return None, selected, current, effective, recovered
    nxt = selected[index + 1]
    if nxt in completed:
        return None, selected, current, effective, recovered
    return nxt, selected, current, effective, recovered


def _next_writing_package(project: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[str | None, list[str], str]:
    nxt, selected, current, _effective, _recovered = _t13_progress(project, state)
    return nxt, selected, current


def _update_project_progress(
    ctx: TransitionContext,
    state: Mapping[str, Any],
    *,
    status: str,
    package: str,
    package_status: str,
) -> None:
    project = dict(ctx.project)
    extensions = dict(project.get("extensions") or {})
    progress = _package_progress(project)
    progress[package] = {"status": package_status, "updatedAt": paths.iso_now(), "stage": state.get("currentStage")}
    extensions["packageProgress"] = progress
    project["extensions"] = extensions
    project["status"] = status
    project["updatedAt"] = paths.iso_now()
    jsonio.write_json_atomic(ctx.project_dir / "project.json", project)


def guard_t13(ctx: TransitionContext) -> list[dict[str, Any]]:
    """T13 completed→package_prepare：存在下一个已选撰写包，顺序推进、不跳包/不重复。"""
    checks: list[dict[str, Any]] = [_pending_input_check(ctx.state)]
    nxt, selected, current, effective, recovered = _t13_progress(ctx.project, ctx.state)
    completed = _completed_packages(ctx.project, ctx.state)
    _check(checks, "writing_packages", "writingPackages/packagesToWrite 非空", bool(selected), {"selected": selected})
    _check(checks, "current_selected", "当前包属于 selected", bool(current) and current in selected, {"current": current, "selected": selected})
    if current in selected:
        index = selected.index(effective)
        expected = selected[index + 1] if index + 1 < len(selected) else None
        _check(
            checks,
            "next_package",
            "存在下一个未完成撰写包",
            bool(nxt),
            {"next": nxt, "expected": expected, "effectiveCurrent": effective},
        )
        _check(checks, "sequential", "下一包紧跟当前包（不可跳包/重复）", nxt == expected, {"next": nxt, "expected": expected})
        _check(checks, "current_completed", "当前包已完成", effective in completed, {"current": current, "effectiveCurrent": effective})
        if recovered:
            _check(
                checks,
                "pre_switched_auto_recover",
                "currentPackage 已提前切到下一包，T13 按上一已完成包自动识别",
                True,
                {"current": current, "effectiveCurrent": effective},
            )
    return checks


def effect_t13(ctx: TransitionContext, state: dict[str, Any]) -> None:
    nxt, selected, current, effective, recovered = _t13_progress(ctx.project, state)
    if not nxt:
        raise BgError(E_GUARD_FAILED, "没有下一个撰写包，不能执行 T13")
    previous = effective
    # 前一包已定稿锁定；切下一包前释放项目级 draftLocked（前一包 .locked/_frozen 文件保留）
    locks = dict(state.get("locks") or {})
    if locks.get("draftLocked") is True:
        previous_lock = {
            "lockedAt": locks.get("lockedAt"),
            "lockReason": locks.get("lockReason"),
            "manifestPath": locks.get("manifestPath"),
            "rootHash": locks.get("rootHash"),
            "package": previous,
        }
        locks["draftLocked"] = False
        locks["lockedAt"] = None
        locks["lockReason"] = ""
        locks["previousLock"] = previous_lock
    state["locks"] = locks
    _update_project_progress(ctx, state, status="package_prepared", package=previous, package_status="completed")
    _update_project_progress(ctx, state, status="package_prepared", package=nxt, package_status="package_prepare")
    state["currentStage"] = "package_prepare"
    state["currentScope"] = nxt
    state["currentPackage"] = nxt
    state.pop("completedAt", None)
    if recovered:
        # 可追溯：记录一次事件，不额外写 history transition
        extensions = dict(state.get("extensions") or {})
        extensions["t13PreSwitchRecovered"] = {"at": paths.iso_now(), "from": current, "effectiveCurrent": previous, "to": nxt}
        state["extensions"] = extensions


def _guard_pending_milestone(milestone: str) -> Callable[[TransitionContext], list[dict[str, Any]]]:
    """未实现迁移的占位 guard：失败并标注里程碑（M3+ 替换为真实 guard）。"""

    def guard(ctx: TransitionContext) -> list[dict[str, Any]]:
        transition = ctx.transition
        return [
            {
                "id": "milestone_guard",
                "label": f"{transition.id if transition else '该迁移'} guard 待 {milestone} 实现",
                "pass": False,
                "detail": {
                    "milestone": milestone,
                    "message": "M2 只实现 T1/T2 的 guard/effects；该迁移由后续里程碑落地",
                },
            }
        ]

    return guard


def _append_completed(state: dict[str, Any], stage: str, scope: str, note: str) -> None:
    """upsert completedStages：同 stage/scope 更新不追加；seq 保持首次顺序。"""
    entries = state.setdefault("completedStages", [])
    now = paths.iso_now()
    next_seq = max((int(item.get("seq") or 0) for item in entries if isinstance(item, Mapping)), default=0) + 1
    for item in entries:
        if isinstance(item, dict) and item.get("stage") == stage and item.get("scope") == scope:
            item["note"] = note
            item["completedAt"] = now
            item.setdefault("seq", next_seq)
            return
    entries.append({"stage": stage, "scope": scope, "completedAt": now, "note": note, "seq": next_seq})


def effect_t1(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T1 effects：completedStages += project_init；进入 info_extract。"""
    summary = ctx.extra.get("env_summary") or {}
    note = f"环境检查 {summary.get('requiredPassed', 0)}/{summary.get('requiredTotal', 0)} 必需项通过"
    if summary.get("requiredDeferred"):
        note += f"，{summary['requiredDeferred']} 项延后（M3 转强校验）"
    _append_completed(state, "project_init", "project", note)
    state["envCheckSummary"] = {
        "requiredPassed": summary.get("requiredPassed", 0),
        "requiredTotal": summary.get("requiredTotal", 0),
        "requiredDeferred": summary.get("requiredDeferred", 0),
        "failed": list(summary.get("failed") or []),
        "checklistVersion": summary.get("checklistVersion"),
        "checkedAt": paths.iso_now(),
    }
    env_path = Path(ctx.workspace) / "dsh" / "config" / "env-check.json"
    if env_path.is_file():
        try:
            env_doc = jsonio.read_json(env_path)
        except BgError:
            env_doc = {}
        state["envCheckSummary"].update(
            {
                "source": "dsh/config/env-check.json",
                "checkedAt": env_doc.get("checkedAt") or paths.iso_now(),
                "scriptsHash": env_doc.get("scriptsHash") or "",
                "hostVersion": env_doc.get("hostVersion") or "未记录",
                "items": env_doc.get("items") or {},
            }
        )
    bidder_ref = ctx.project.get("bidderRef") if isinstance(ctx.project.get("bidderRef"), Mapping) else {}
    bidder_completeness = {
        "overall": bidder_ref.get("completeness"),
        "threshold": 0.7,
        "warning": isinstance(bidder_ref.get("completeness"), (int, float)) and bidder_ref.get("completeness") < 0.7,
        "snapshotHash": bidder_ref.get("snapshotHash", ""),
        "certificatesHash": bidder_ref.get("certificatesHash", ""),
    }
    if bidder_completeness.get("overall") is not None:
        state["bidderCompleteness"] = bidder_completeness
    state["currentStage"] = "info_extract"
    state["currentScope"] = "project"
    state["currentPackage"] = None


def effect_t2(ctx: TransitionContext, state: dict[str, Any]) -> None:
    """T2 effects：completedStages += info_extract；切到首个包 package_prepare。"""
    package = _first_package(ctx.project)
    _append_completed(state, "info_extract", "project", "九类提取校验通过，分包结论已确定")
    state["currentStage"] = "package_prepare"
    state["currentScope"] = package
    state["currentPackage"] = package


TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        "T1",
        "project_init",
        "info_extract",
        "project",
        "F2 完成 → 关键信息提取",
        effects_note="完成 project_init；进入 info_extract",
        guard=guard_t1,
        effects=effect_t1,
        milestone="M2",
    ),
    Transition(
        "T2",
        "info_extract",
        "package_prepare",
        "project",
        "F3 提取完成 → 撰写前准备",
        effects_note="完成 info_extract；进入首个包 package_prepare",
        guard=guard_t2,
        effects=effect_t2,
        milestone="M2",
    ),
    Transition(
        "T3",
        "package_prepare",
        "outline",
        "package",
        "F4 完成 → 大纲编写",
        effects_note="完成 package_prepare；进入 outline（effects 同事务写 currentPackage/currentScope）",
        guard=guard_t3,
        effects=effect_t3,
        milestone="M4",
    ),
    Transition(
        "T4",
        "outline",
        "draft",
        "package",
        "大纲校验通过 → 正文撰写",
        effects_note="完成 outline；进入 draft",
        guard=guard_t4,
        effects=effect_t4,
        milestone="M5",
    ),
    Transition(
        "T5",
        "draft",
        "review",
        "package",
        "正文合并完成 → 审查",
        effects_note="完成 draft；进入 review；reviewLoop.round 首轮置 1",
        guard=guard_t5,
        effects=effect_t5,
        milestone="M6",
    ),
    Transition(
        "T6",
        "review",
        "optimize",
        "package",
        "审查完成且有问题 → 优化",
        effects_note="完成 review；进入 optimize",
        guard=guard_t6,
        effects=effect_t6,
        milestone="M7",
    ),
    Transition(
        "T7",
        "review",
        "finalize",
        "package",
        "审查通过 → 定稿锁定",
        effects_note="完成 review；记录 closedRound",
        guard=guard_t7,
        effects=effect_t7,
        milestone="M7",
    ),
    Transition(
        "T8",
        "optimize",
        "review",
        "package",
        "优化完成 → 下一轮审查",
        effects_note="完成 optimize；reviewLoop.round 自增",
        guard=guard_t8,
        effects=effect_t8,
        milestone="M7",
    ),
    Transition(
        "T9",
        "finalize",
        "export",
        "package",
        "定稿完成 → 导出",
        effects_note="完成 finalize；写 locks.lockedAt",
        guard=guard_t9,
        effects=effect_t9,
        milestone="M8",
    ),
    Transition(
        "T10",
        "export",
        "completed",
        "package",
        "导出校验通过 → 已完成",
        effects_note="完成 export；写 completedAt",
        guard=guard_t10,
        effects=effect_t10,
        milestone="M8",
    ),
    Transition(
        "T11",
        "*",
        "aborted",
        "any",
        "用户取消 → 已中止",
        effects_note="写 abort{reason,at}；保留现场",
        from_any_active=True,
        handled_by_command="abort",
        milestone="M2",
    ),
    Transition(
        "T12",
        "aborted",
        "*",
        "any",
        "用户恢复 → 原阶段",
        effects_note="从 history 推出原阶段；保留 abort 记录",
        handled_by_command="resume",
        milestone="M2",
    ),
    Transition(
        "T13",
        "completed",
        "package_prepare",
        "package",
        "前一包完成 → 下一包",
        effects_note="切换 currentPackage/currentScope；写 history",
        guard=guard_t13,
        effects=effect_t13,
        milestone="M3",
    ),
    Transition(
        "R1",
        "review",
        "draft",
        "package",
        "结构返工 → 重写正文",
        effects_note="重开章节；记 rework 事件",
        guard=_guard_pending_milestone("M7"),
        milestone="M7",
    ),
    Transition(
        "R2",
        "optimize",
        "draft",
        "package",
        "结构返工 → 重写正文",
        effects_note="重开章节；记 rework 事件",
        guard=_guard_pending_milestone("M7"),
        milestone="M7",
    ),
    Transition(
        "R3",
        "outline",
        "package_prepare",
        "package",
        "修改包级参数 → 重做准备",
        effects_note="备份 outline.json；回到 package_prepare",
        guard=_guard_pending_milestone("M6"),
        milestone="M6",
    ),
    Transition(
        "R4",
        "finalize",
        "optimize",
        "package",
        "解除定稿锁定 → 优化",
        effects_note="draftLocked=false；归档 _frozen",
        guard=_guard_pending_milestone("M8"),
        milestone="M8",
    ),
    Transition(
        "R5",
        "*",
        "package_prepare",
        "package",
        "恢复历史包 → 原阶段",
        effects_note="切换 currentPackage/currentScope/currentStage",
        from_any_active=True,
        handled_by_command="resume-package",
        milestone="M3",
    ),
)


# ---------------------------------------------------------------------------
# 迁移解析 / allowedNext
# ---------------------------------------------------------------------------
def resolve_transition(state: Mapping[str, Any], to_stage: str) -> Transition | None:
    """按当前阶段与目标阶段查迁移表（普通推进类，不含 abort/resume）。"""
    current_stage = str(state.get("currentStage") or "")
    for transition in TRANSITIONS:
        if transition.handled_by_command:
            continue
        if transition.to_stage != to_stage:
            continue
        if transition.from_any_active:
            if current_stage in schema.ACTIVE_STAGES:
                return transition
            continue
        if transition.from_stage == current_stage:
            return transition
    return None


def allowed_next(state: Mapping[str, Any], project: Mapping[str, Any] | None = None) -> list[str]:
    """当前阶段允许的下一步（正常推进类；不含 abort/rework 等需用户确认的迁移）。"""
    current_stage = str(state.get("currentStage") or "")
    result: list[str] = []
    for transition in TRANSITIONS:
        if transition.handled_by_command or transition.from_any_active:
            continue
        if transition.from_stage != current_stage:
            continue
        if transition.id == "T13" and project is not None:
            nxt, _, _ = _next_writing_package(project, state)
            if not nxt:
                continue
        if transition.to_stage in result:
            continue
        result.append(transition.to_stage)
    return result


def evaluate_guard(ctx: TransitionContext) -> dict[str, Any]:
    """执行 guard，返回 ``{pass, checks}``；guard 异常转失败检查项。"""
    transition = ctx.transition
    if transition is None:
        return {"pass": False, "checks": [{"id": "transition", "label": "迁移存在", "pass": False, "detail": {}}]}
    if transition.guard is None:
        checks = [{"id": "guard_missing", "label": "guard 未实现", "pass": False, "detail": {"milestone": transition.milestone}}]
    else:
        try:
            checks = transition.guard(ctx)
        except Exception as exc:  # noqa: BLE001 - guard 异常不得让进程崩溃
            checks = [
                {
                    "id": "guard_exception",
                    "label": "guard 执行异常",
                    "pass": False,
                    "detail": {"type": type(exc).__name__, "message": str(exc)[:300]},
                }
            ]
    passed = bool(checks) and all(bool(check.get("pass")) for check in checks)
    return {"pass": passed, "checks": checks}


# ---------------------------------------------------------------------------
# 迁移 / set-package / abort / resume
# ---------------------------------------------------------------------------
def _transition_scope(state: Mapping[str, Any], transition: Transition, ctx: TransitionContext | None = None) -> str:
    if transition.id == "T3" and ctx is not None:
        return ctx.package or str(state.get("currentPackage") or state.get("currentScope") or "package-1")
    if transition.id == "T13" and ctx is not None:
        nxt, _, _ = _next_writing_package(ctx.project, state)
        if nxt:
            return nxt
    return str(state.get("currentScope") or "project")


def sync_registry_mirror(
    workspace: Path,
    state: Mapping[str, Any],
    project: Mapping[str, Any],
    *,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> bool:
    """同步 registry.projects[] 镜像（PRD 4.4 每次迁移成功）。"""
    registry_file = paths.registry_path(workspace)
    project_id = str(state.get("projectId") or "")
    try:
        with jsonio.FileLock(registry_file, timeout=lock_timeout):
            registry = read_registry(workspace)
            entries = [dict(entry) for entry in registry.get("projects") or [] if isinstance(entry, Mapping)]
            found = False
            for entry in entries:
                if str(entry.get("projectId") or "") != project_id:
                    continue
                entry["currentStage"] = state.get("currentStage")
                entry["currentPackage"] = state.get("currentPackage")
                entry["updatedAt"] = paths.iso_now()
                entry["projectName"] = project.get("projectName", entry.get("projectName", ""))
                entry["bidderId"] = str((project.get("bidderRef") or {}).get("bidderId") or entry.get("bidderId", ""))
                found = True
                break
            if not found:
                entries.append(
                    {
                        "projectId": project_id,
                        "dirName": str(project.get("dirName") or ""),
                        "projectName": str(project.get("projectName") or ""),
                        "bidderId": str((project.get("bidderRef") or {}).get("bidderId") or ""),
                        "currentStage": state.get("currentStage"),
                        "currentPackage": state.get("currentPackage"),
                        "createdAt": str(project.get("createdAt") or ""),
                        "updatedAt": paths.iso_now(),
                    }
                )
            registry["projects"] = entries
            registry["updatedAt"] = paths.iso_now()
            jsonio.write_json_atomic(registry_file, registry, lock=False, lock_timeout=lock_timeout)
    except BgError:
        raise
    except OSError as exc:
        raise BgError(
            E_SCHEMA_INVALID,
            "同步 registry.json 镜像失败",
            hint="重跑同名迁移（--idempotent）或手工核对 registry.json",
            detail={"reason": exc.strerror or type(exc).__name__},
        ) from exc
    return True


def perform_transition(
    workspace: Path,
    project_dir: Path,
    *,
    to_stage: str,
    package: str | None = None,
    actor: str = "agent",
    reason: str = "",
    idempotent: bool = False,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """执行一次合法迁移（含 guard/effects/history/revision/registry 镜像）。"""
    if to_stage == "aborted":
        return abort_project(workspace, project_dir, reason=reason, actor=actor, lock_timeout=lock_timeout)
    state = load_state(project_dir)
    project = load_project(project_dir)
    current_stage = str(state.get("currentStage") or "")
    if current_stage == to_stage:
        if idempotent:
            return {
                "ok": True,
                "command": "state_transition",
                "transitionId": "",
                "alreadyInStage": True,
                "from": current_stage,
                "to": to_stage,
                "scope": state.get("currentScope"),
                "revision": state.get("revision"),
                "changed": [],
                "allowedNext": allowed_next(state, project),
            }
        raise BgError(
            E_ALREADY_IN_STAGE,
            f"已处于目标阶段：{to_stage}",
            hint="重复迁移默认报错；确认无副作用可加 --idempotent",
            detail={"current": current_stage, "to": to_stage, "allowedNext": allowed_next(state, project)},
        )
    transition = resolve_transition(state, to_stage)
    if transition is None:
        raise BgError(
            E_ILLEGAL_TRANSITION,
            f"非法迁移：{current_stage} → {to_stage}",
            hint="只允许 PRD 4.3 迁移表中的迁移；查看 allowedNext",
            detail={
                "current": current_stage,
                "to": to_stage,
                "allowedNext": allowed_next(state, project),
            },
        )
    context = TransitionContext(
        workspace=workspace,
        project_dir=project_dir,
        state=state,
        project=project,
        package=_context_package(state, project, package),
        actor=actor,
        reason=reason,
        transition=transition,
    )
    guard = evaluate_guard(context)
    if not guard["pass"]:
        hint = "按 guard.checks 中的失败项修复后重试（check 命令可只读预检）"
        if transition.id == "T13":
            hint = (
                "T13 必须在上一包 completed 且 currentPackage 仍为上一包时执行；"
                "若已提前 set-package 到下一包，请先 set-package 回上一包再执行 T13（新版会自动识别提前切包）"
            )
        raise BgError(
            E_GUARD_FAILED,
            f"迁移前置条件不满足：{current_stage} → {to_stage}",
            hint=hint,
            detail={
                "current": current_stage,
                "to": to_stage,
                "transitionId": transition.id,
                "currentPackage": state.get("currentPackage"),
                "guard": guard,
                "allowedNext": allowed_next(state, project),
            },
        )
    new_state = copy.deepcopy(state)
    if transition.effects is not None:
        transition.effects(context, new_state)
    new_revision = int(state.get("revision") or 1) + 1
    now = paths.iso_now()
    scope = _transition_scope(state, transition, context)
    history_entry = {
        "at": now,
        "from": current_stage,
        "to": to_stage,
        "scope": scope,
        "actor": actor,
        "reason": reason or transition.title,
        "kind": "transition",
        "revision": new_revision,
    }
    new_state["history"] = list(new_state.get("history") or []) + [history_entry]
    new_state["revision"] = new_revision
    new_state["updatedAt"] = now
    new_state["updatedBy"] = "script:bg_state.py"
    write_state(project_dir, new_state, lock_timeout=lock_timeout)
    if transition.id in ("T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T13"):
        # PRD F4.4/F5.1.8 post-check：迁移后状态必须为目标阶段 + 目标包；否则回滚并报 E_STATE_CORRUPT
        try:
            verified = load_state(project_dir)
        except BgError:
            verified = None
        target_package = str(new_state.get("currentPackage") or "")
        expected_stage = {
            "T3": "outline",
            "T4": "draft",
            "T5": "review",
            "T6": "optimize",
            "T7": "finalize",
            "T8": "review",
            "T9": "export",
            "T10": "completed",
            "T13": "package_prepare",
        }[transition.id]
        t9_locks_ok = True
        t10_completed_ok = True
        if transition.id == "T9" and isinstance(verified, Mapping):
            t9_locks_ok = bool((verified.get("locks") or {}).get("draftLocked"))
        if transition.id == "T10" and isinstance(verified, Mapping):
            t10_completed_ok = bool(verified.get("completedAt"))
        review_loop_ok = True
        if transition.id in ("T5", "T7", "T8") and isinstance(verified, Mapping):
            review_loop = verified.get("reviewLoop") if isinstance(verified.get("reviewLoop"), Mapping) else {}
            if transition.id == "T5":
                review_loop_ok = isinstance(review_loop.get("round"), int) and review_loop.get("round") >= 1
            elif transition.id == "T7":
                round_value, _ = _review_round(state)
                review_loop_ok = review_loop.get("closedRound") == round_value
            elif transition.id == "T8":
                round_value, _ = _review_round(state)
                review_loop_ok = isinstance(review_loop.get("round"), int) and review_loop.get("round") == round_value + 1
        if (
            not isinstance(verified, Mapping)
            or verified.get("currentStage") != expected_stage
            or verified.get("currentScope") != target_package
            or not review_loop_ok
            or not t9_locks_ok
            or not t10_completed_ok
        ):
            write_state(project_dir, state, lock_timeout=lock_timeout)
            raise BgError(
                E_STATE_CORRUPT,
                f"{transition.id} 迁移后状态校验失败，已回滚到 {current_stage}",
                hint="检查 state.json 写入/权限；不要手工修复状态",
                detail={
                    "expected": {"currentStage": expected_stage, "currentScope": target_package},
                    "actual": verified if isinstance(verified, Mapping) else None,
                },
            )
    mirror_ok = sync_registry_mirror(workspace, new_state, project, lock_timeout=lock_timeout)
    return {
        "ok": True,
        "command": "state_transition",
        "transitionId": transition.id,
        "alreadyInStage": False,
        "from": current_stage,
        "to": new_state.get("currentStage"),
        "scope": scope,
        "revision": new_revision,
        "historyEntry": history_entry,
        "changed": [f"{project_dir.name}/state.json", "dsh/registry.json"] if mirror_ok else [f"{project_dir.name}/state.json"],
        "allowedNext": allowed_next(new_state, project),
    }


def check_transition(
    workspace: Path,
    project_dir: Path,
    *,
    to_stage: str,
    package: str | None = None,
    actor: str = "agent",
    reason: str = "",
) -> dict[str, Any]:
    """只读校验一次迁移（不写任何文件）。"""
    state = load_state(project_dir)
    project = load_project(project_dir)
    transition = resolve_transition(state, to_stage)
    if transition is None:
        raise BgError(
            E_ILLEGAL_TRANSITION,
            f"非法迁移：{state.get('currentStage')} → {to_stage}",
            hint="只允许 PRD 4.3 迁移表中的迁移；查看 allowedNext",
            detail={
                "current": state.get("currentStage"),
                "to": to_stage,
                "allowedNext": allowed_next(state, project),
            },
        )
    context = TransitionContext(
        workspace=workspace,
        project_dir=project_dir,
        state=state,
        project=project,
        package=_context_package(state, project, package),
        actor=actor,
        reason=reason,
        transition=transition,
    )
    guard = evaluate_guard(context)
    next_writing, _selected, _current, effective, recovered = _t13_progress(project, state)
    return {
        "ok": True,
        "command": "state_check",
        "transitionId": transition.id,
        "from": state.get("currentStage"),
        "to": to_stage,
        "scope": state.get("currentScope"),
        "currentPackage": state.get("currentPackage"),
        "nextWritingPackage": next_writing,
        "t13EffectiveCurrent": effective if transition.id == "T13" else None,
        "t13PreSwitchRecovered": recovered if transition.id == "T13" else False,
        "pass": guard["pass"],
        "guard": guard,
        "effects": transition.effects_note,
        "allowedNext": allowed_next(state, project),
    }


def set_package(
    workspace: Path,
    project_dir: Path,
    *,
    package: str,
    actor: str = "agent",
    record_history: bool = True,
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """设置当前包（不迁移阶段，幂等；PRD 4.4）。"""
    if not isinstance(package, str) or not PACKAGE_ID_RE.match(package):
        raise BgError(E_USAGE, "package 必须形如 package-<N>", detail={"package": package})
    state = load_state(project_dir)
    project = load_project(project_dir)
    stage = str(state.get("currentStage") or "")
    if stage == "aborted":
        raise BgError(
            E_GUARD_FAILED,
            "已中止的项目不能切换包",
            hint="先 resume 恢复项目",
            detail={"current": stage},
        )
    if stage not in schema.PACKAGE_SCOPED_STAGES:
        raise BgError(
            E_GUARD_FAILED,
            f"当前阶段（{stage}）不是包级阶段，不能设置当前包",
            hint="T2 迁移进入 package_prepare 后再设置包",
            detail={"current": stage, "allowedNext": allowed_next(state, project)},
        )
    if state.get("currentPackage") == package and state.get("currentScope") == package:
        return {
            "ok": True,
            "command": "state_set_package",
            "currentPackage": package,
            "currentScope": state.get("currentScope"),
            "revision": state.get("revision"),
            "changed": [],
            "allowedNext": allowed_next(state, project),
        }
    new_state = copy.deepcopy(state)
    new_state["currentPackage"] = package
    new_state["currentScope"] = package
    new_state["revision"] = int(state.get("revision") or 1) + 1
    now = paths.iso_now()
    new_state["updatedAt"] = now
    new_state["updatedBy"] = "script:bg_state.py"
    history_entry: dict[str, Any] | None = None
    if record_history:
        # P3-13：set-package 是自迁移动作，记为 event（不是 package_prepare→package_prepare 迁移）
        history_entry = append_history_event(
            new_state,
            action="set_package",
            reason=f"切换当前包：{state.get('currentPackage') or '-'} → {package}",
            actor=actor,
            revision=new_state["revision"],
            scope=package,
            at=now,
            detail={"from": state.get("currentPackage"), "to": package, "stage": stage},
        )
    write_state(project_dir, new_state, lock_timeout=lock_timeout)
    sync_registry_mirror(workspace, new_state, project, lock_timeout=lock_timeout)
    return {
        "ok": True,
        "command": "state_set_package",
        "currentPackage": package,
        "currentScope": package,
        "revision": new_state["revision"],
        "changed": [f"{project_dir.name}/state.json", "dsh/registry.json"],
        "historyKind": "event",
        "historyEntry": history_entry,
        "allowedNext": allowed_next(new_state, project),
    }


def abort_project(
    workspace: Path,
    project_dir: Path,
    *,
    reason: str,
    actor: str = "user",
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """T11：任意活动阶段 → aborted（保留现场，PRD 4.3）。"""
    if not str(reason or "").strip():
        raise BgError(E_USAGE, "abort 需要 --reason <文本>", hint="说明取消原因，便于审计与恢复")
    state = load_state(project_dir)
    project = load_project(project_dir)
    stage = str(state.get("currentStage") or "")
    if stage == "aborted":
        raise BgError(
            E_ALREADY_IN_STAGE,
            "项目已处于 aborted 状态",
            hint="用 resume 恢复执行",
            detail={"current": stage},
        )
    if stage == "completed":
        raise BgError(
            E_ILLEGAL_TRANSITION,
            "项目已完成，不能再中止",
            hint="已完成项目如需修改需走 R4 解锁定流程（后续里程碑）",
            detail={"current": stage, "allowedNext": []},
        )
    new_state = copy.deepcopy(state)
    new_revision = int(state.get("revision") or 1) + 1
    now = paths.iso_now()
    new_state["abort"] = {"reason": str(reason).strip(), "at": now}
    new_state["currentStage"] = "aborted"
    history_entry = {
        "at": now,
        "from": stage,
        "to": "aborted",
        "scope": state.get("currentScope"),
        "actor": actor,
        "reason": str(reason).strip(),
        "kind": "transition",
        "revision": new_revision,
    }
    new_state["history"] = list(new_state.get("history") or []) + [history_entry]
    new_state["revision"] = new_revision
    new_state["updatedAt"] = now
    new_state["updatedBy"] = "script:bg_state.py"
    write_state(project_dir, new_state, lock_timeout=lock_timeout)
    sync_registry_mirror(workspace, new_state, project, lock_timeout=lock_timeout)
    return {
        "ok": True,
        "command": "state_abort",
        "from": stage,
        "to": "aborted",
        "scope": state.get("currentScope"),
        "revision": new_revision,
        "abort": new_state["abort"],
        "historyEntry": history_entry,
        "changed": [f"{project_dir.name}/state.json", "dsh/registry.json"],
        "allowedNext": [],
    }


def resume_project(
    workspace: Path,
    project_dir: Path,
    *,
    actor: str = "user",
    reason: str = "用户恢复执行",
    lock_timeout: float = jsonio.LOCK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """T12：aborted → 原阶段（从 history 推导；保留 abort 记录）。"""
    state = load_state(project_dir)
    project = load_project(project_dir)
    if state.get("currentStage") != "aborted":
        raise BgError(
            E_ILLEGAL_TRANSITION,
            "只有 aborted 状态可以恢复",
            hint="resume 仅用于 T12；查看 allowedNext",
            detail={"current": state.get("currentStage"), "allowedNext": allowed_next(state, project)},
        )
    previous_stage = ""
    previous_scope = ""
    for entry in reversed(history_transitions(state.get("history"))):
        if entry.get("to") == "aborted":
            previous_stage = str(entry.get("from") or "")
            previous_scope = str(entry.get("scope") or "")
            break
    if not previous_stage or previous_stage not in schema.STAGES:
        raise BgError(
            E_STATE_CORRUPT,
            "无法从 history 推导恢复目标阶段",
            hint="history 缺少 to=aborted 的迁移记录；从备份恢复 state.json",
            detail={"historyCount": len(state.get("history") or [])},
        )
    if previous_stage == "aborted":
        raise BgError(E_STATE_CORRUPT, "history 中 aborted 的前一阶段非法", detail={"stage": previous_stage})
    new_state = copy.deepcopy(state)
    new_revision = int(state.get("revision") or 1) + 1
    now = paths.iso_now()
    new_state["currentStage"] = previous_stage
    if previous_scope == "project":
        new_state["currentScope"] = "project"
        new_state["currentPackage"] = None
    elif previous_scope.startswith("package-"):
        new_state["currentScope"] = previous_scope
        new_state["currentPackage"] = previous_scope
    history_entry = {
        "at": now,
        "from": "aborted",
        "to": previous_stage,
        "scope": previous_scope or state.get("currentScope"),
        "actor": actor,
        "reason": reason,
        "kind": "transition",
        "revision": new_revision,
    }
    new_state["history"] = list(new_state.get("history") or []) + [history_entry]
    new_state["revision"] = new_revision
    new_state["updatedAt"] = now
    new_state["updatedBy"] = "script:bg_state.py"
    write_state(project_dir, new_state, lock_timeout=lock_timeout)
    sync_registry_mirror(workspace, new_state, project, lock_timeout=lock_timeout)
    return {
        "ok": True,
        "command": "state_resume",
        "from": "aborted",
        "to": previous_stage,
        "scope": previous_scope or state.get("currentScope"),
        "revision": new_revision,
        "historyEntry": history_entry,
        "abort": new_state.get("abort"),
        "changed": [f"{project_dir.name}/state.json", "dsh/registry.json"],
        "allowedNext": allowed_next(new_state, project),
    }


def status_summary(workspace: Path, project_dir: Path) -> dict[str, Any]:
    """status 命令输出（Host 的 GET /projects/state 可直接透传）。"""
    state = load_state(project_dir)
    project = load_project(project_dir)
    event_log_path = ""
    event_log_count = 0
    try:
        from . import logging as event_log  # 延迟导入，避免 state <-> logging 循环依赖

        path = event_log.events_path(workspace)
        if path.is_file():
            event_log_path = str(path)
            event_log_count = sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    except (OSError, ValueError):
        pass
    return {
        "ok": True,
        "command": "state_status",
        "projectId": state.get("projectId"),
        "projectRoot": str(project_dir),
        "projectDirName": project_dir.name,
        "projectName": project.get("projectName", ""),
        "bidderRef": project.get("bidderRef"),
        "currentStage": state.get("currentStage"),
        "currentScope": state.get("currentScope"),
        "currentPackage": state.get("currentPackage"),
        "revision": state.get("revision"),
        "completedStages": state.get("completedStages"),
        "locks": state.get("locks"),
        "reviewLoop": state.get("reviewLoop"),
        "pendingInput": state.get("pendingInput"),
        "abort": state.get("abort"),
        "envCheckSummary": state.get("envCheckSummary"),
        "allowedNext": allowed_next(state, project),
        "historyCount": len(state.get("history") or []),
        "transitionCount": len(history_transitions(state.get("history"))),
        # 事件日志只写 dsh/logs/events-*.jsonl，不在 state.history 中；明确命名避免
        # 把“state 内事件数=0”误读成“没有任何事件”。
        "stateEventCount": len(history_events(state.get("history"))),
        "eventLogPath": event_log_path,
        "eventLogCount": event_log_count,
        "updatedAt": state.get("updatedAt"),
        "updatedBy": state.get("updatedBy"),
        "workspace": str(workspace),
    }
