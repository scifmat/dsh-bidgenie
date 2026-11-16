#!/usr/bin/env python3
"""bg_state.py：状态机 CLI（M2；``state.json`` 唯一写者 = ``_lib/state.py``）。

用法（PRD 4.4 / CONTRACT §8.3）：
    python bg_state.py status     --project <dir|id> [--workspace <ws>] [--json]
    python bg_state.py check      --project <dir|id> --to <stage> [--package <id>] [--json]
    python bg_state.py transition --project <dir|id> --to <stage> [--package <id>]
                                  [--reason <text>] [--actor agent|user|script] [--idempotent] [--json]
    python bg_state.py set-package --project <dir|id> --package <package-N> [--json]
    python bg_state.py abort      --project <dir|id> --reason <text> [--json]
    python bg_state.py resume     --project <dir|id> [--json]
    python bg_state.py --selftest [--json]

M2 范围：T1（project_init→info_extract）、T2（info_extract→package_prepare）完整 guard/effects；
T11 abort / T12 resume 可用；T3–T13、R1–R5 在迁移表中登记（``allowedNext`` 正确），
guard 返回「待后续里程碑实现」，非法迁移返回 ``E_ILLEGAL_TRANSITION`` + ``allowedNext``。
"""

from __future__ import annotations

import argparse
import copy
import json
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import bg_bidder  # noqa: E402  - --selftest 用（创建投标人）
import bg_workspace  # noqa: E402  - --selftest 用（初始化工作区/项目）
from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib  # noqa: E402

ACTORS = ("agent", "user", "script")


# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------
def _log_transition(
    workspace: Path,
    *,
    from_stage: str,
    to_stage: str,
    scope: str | None,
    actor: str,
    revision: int,
    transition_id: str,
    reason: str,
    project_id: str = "",
) -> None:
    event_log.log_event(
        workspace,
        "state.transition",
        actor="script",
        project=project_id,
        package=str(scope or ""),
        detail={
            "from": from_stage,
            "to": to_stage,
            "scope": scope,
            "actor": actor,
            "revision": revision,
            "transitionId": transition_id,
            "reason": reason,
            "projectId": project_id,
            "package": str(scope or ""),
        },
    )


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def command_status(args: argparse.Namespace) -> dict[str, Any]:
    """status：只读输出状态摘要 + allowedNext。"""
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return state_lib.status_summary(workspace, project_dir)


def command_check(args: argparse.Namespace) -> dict[str, Any]:
    """check：只读校验一次迁移（guard 失败仍 exit 0，pass=false）。"""
    if args.to not in schema.STAGES:
        raise errors.BgError(
            errors.E_USAGE,
            f"--to 阶段非法：{args.to}",
            hint=f"可用阶段：{', '.join(schema.STAGES)}",
            detail={"to": args.to, "stages": list(schema.STAGES)},
        )
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return state_lib.check_transition(
        workspace,
        project_dir,
        to_stage=args.to,
        package=getattr(args, "package", None),
        actor=getattr(args, "actor", "agent"),
        reason=getattr(args, "reason", "") or "",
    )


def command_transition(args: argparse.Namespace) -> dict[str, Any]:
    """transition：执行一次合法迁移（写 state.json + registry 镜像 + history/revision）。"""
    if args.to not in schema.STAGES:
        raise errors.BgError(
            errors.E_USAGE,
            f"--to 阶段非法：{args.to}",
            hint=f"可用阶段：{', '.join(schema.STAGES)}",
            detail={"to": args.to, "stages": list(schema.STAGES)},
        )
    actor = str(getattr(args, "actor", "agent") or "agent")
    if actor not in ACTORS:
        raise errors.BgError(errors.E_USAGE, f"--actor 非法：{actor}", hint="可用：agent|user|script")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    result = state_lib.perform_transition(
        workspace,
        project_dir,
        to_stage=args.to,
        package=getattr(args, "package", None),
        actor=actor,
        reason=str(getattr(args, "reason", "") or ""),
        idempotent=bool(getattr(args, "idempotent", False)),
        lock_timeout=errors.timeout_value(args),
    )
    if not result.get("alreadyInStage"):
        state_after = state_lib.load_state(project_dir)
        _log_transition(
            workspace,
            from_stage=str(result.get("from") or ""),
            to_stage=str(result.get("to") or ""),
            scope=result.get("scope"),
            actor=actor,
            revision=int(result.get("revision") or 0),
            transition_id=str(result.get("transitionId") or ""),
            reason=str(getattr(args, "reason", "") or ""),
            project_id=str(state_after.get("projectId") or ""),
        )
    return result


def command_set_package(args: argparse.Namespace) -> dict[str, Any]:
    """set-package：设置 currentPackage/currentScope（不迁移阶段，幂等）。"""
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    result = state_lib.set_package(
        workspace,
        project_dir,
        package=str(args.package),
        lock_timeout=errors.timeout_value(args),
    )
    from _lib import logging as event_log

    state_after = state_lib.load_state(project_dir)
    event_log.log_event(
        workspace,
        "state.set_package",
        actor="script",
        project=str(state_after.get("projectId") or ""),
        package=str(result.get("currentPackage") or ""),
        detail={"package": result.get("currentPackage"), "revision": result.get("revision"), "changed": result.get("changed")},
    )
    return result


def command_abort(args: argparse.Namespace) -> dict[str, Any]:
    """abort：T11 任意活动阶段 → aborted。"""
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    result = state_lib.abort_project(
        workspace,
        project_dir,
        reason=str(args.reason or ""),
        lock_timeout=errors.timeout_value(args),
    )
    _log_transition(
        workspace,
        from_stage=str(result.get("from") or ""),
        to_stage="aborted",
        scope=result.get("scope"),
        actor="user",
        revision=int(result.get("revision") or 0),
        transition_id="T11",
        reason=str(args.reason or ""),
    )
    return result


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    """resume：T12 aborted → 原阶段（保留 abort 记录）。"""
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    result = state_lib.resume_project(
        workspace,
        project_dir,
        lock_timeout=errors.timeout_value(args),
    )
    _log_transition(
        workspace,
        from_stage="aborted",
        to_stage=str(result.get("to") or ""),
        scope=result.get("scope"),
        actor="user",
        revision=int(result.get("revision") or 0),
        transition_id="T12",
        reason="用户恢复执行",
    )
    return result


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
REQUIRED_ENV_ITEMS = (
    "python_runtime",
    "python_packages",
    "scripts_integrity",
    "templates_integrity",
    "workspace_writable",
    "disk_space",
    "skill_service",
    "state_service",
)


def _write_passing_env_check(workspace: Path) -> None:
    """selftest 用：构造必需项全 passed（templates 延后）的 env-check.json。"""
    checklist = jsonio.read_json(_SCRIPT_DIR / "checklist.json")
    items: dict[str, Any] = {}
    for item in checklist.get("items") or []:
        item_id = str(item.get("id") or "")
        deferred = item.get("deferredUntil")
        status = "skipped" if deferred else "passed"
        items[item_id] = {
            "itemVersion": str(item.get("itemVersion") or ""),
            "required": bool(item.get("required")),
            "status": status if item.get("required") else "passed",
            "evidence": "selftest",
            "checkedAt": paths.iso_now(),
            "detail": ({"pendingMilestone": deferred} if deferred else {}),
        }
    document = {
        "schemaVersion": "1.0",
        "checklistVersion": str(checklist.get("checklistVersion") or ""),
        "hostVersion": "",
        "checkedAt": paths.iso_now(),
        "items": items,
        "waivers": [],
        "obsoleteItems": [],
        "degraded": [],
        "updatedAt": paths.iso_now(),
    }
    target = paths.config_dir(workspace) / "env-check.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(target, document, lock=False)


def _fill_t2_fixture(project_dir: Path) -> None:
    """selftest 用：回填 project.json 分包字段 + 九类提取文件 + conversion-manifest。"""
    project = state_lib.load_project(project_dir)
    project["isSubcontracted"] = False
    project["packageCount"] = 1
    project["packagesToWrite"] = ["package-1"]
    project["updatedAt"] = paths.iso_now()
    schema.raise_if_errors(schema.validate_project(project), filename="project.json")
    jsonio.write_json_atomic(project_dir / "project.json", project)
    common = project_dir / "extraction" / "common"
    common.mkdir(parents=True, exist_ok=True)
    for name in state_lib.COMMON_EXTRACT_FILES:
        (common / name).write_text(f"# {name}\n", encoding="utf-8", newline="\n")
    package_dir = project_dir / "extraction" / "packages" / "package-1"
    package_dir.mkdir(parents=True, exist_ok=True)
    for name in state_lib.PACKAGE_EXTRACT_FILES:
        (package_dir / name).write_text(f"# {name}\n", encoding="utf-8", newline="\n")
    sources = project_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": "1.0",
        "converters": {},
        "sources": [
            {
                "originalName": "selftest.docx",
                "rawPath": "sources/raw/selftest.docx",
                "convertedPath": "sources/01-bidding-documents.md",
                "kind": "bidding_document",
                "hash": "sha256:selftest",
            }
        ],
        "updatedAt": paths.iso_now(),
    }
    jsonio.write_json_atomic(sources / "conversion-manifest.json", manifest)


def run_selftest() -> dict[str, Any]:
    """脚本自检：建项目 → status → T1 → 重复/非法/T2 guard → T2 → abort/resume → 损坏 state。"""
    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-state-selftest-"))
    try:
        workspace = tmp / "ws"
        workspace.mkdir()
        bg_workspace.init_workspace(workspace)
        bidder_payload = {
            "bidder": {
                "bidderId": "bdr-20261108120000-a1b2",
                "bidderType": "legal_entity",
                "displayName": "状态机自检公司",
            }
        }
        bg_bidder.create_bidder(workspace, bidder_payload, source="panel")
        _write_passing_env_check(workspace)
        project_result = bg_workspace.init_project(
            workspace,
            bidder_id="bdr-20261108120000-a1b2",
            project_name="状态机自检项目",
        )
        project_dir = Path(project_result["projectRoot"])
        checks.append("project_init")

        status = state_lib.status_summary(workspace, project_dir)
        if status["currentStage"] != "project_init" or status["allowedNext"] != ["info_extract"]:
            raise AssertionError(f"初始状态不正确：{status['currentStage']} {status['allowedNext']}")
        checks.append("status")

        check_t1 = state_lib.check_transition(workspace, project_dir, to_stage="info_extract")
        if not check_t1["pass"]:
            raise AssertionError(f"T1 guard 未通过：{check_t1['guard']['checks']}")
        checks.append("check_t1")

        t1 = state_lib.perform_transition(workspace, project_dir, to_stage="info_extract", actor="agent")
        if t1["to"] != "info_extract" or t1["revision"] != 2:
            raise AssertionError(f"T1 迁移结果不正确：{t1}")
        registry = state_lib.read_registry(workspace)
        mirror = (registry.get("projects") or [{}])[0].get("currentStage")
        if mirror != "info_extract":
            raise AssertionError(f"registry 镜像未同步：{mirror}")
        checks.append("transition_t1")

        try:
            state_lib.perform_transition(workspace, project_dir, to_stage="info_extract")
            raise AssertionError("重复迁移未报错")
        except errors.BgError as exc:
            if exc.code != errors.E_ALREADY_IN_STAGE:
                raise AssertionError(f"重复迁移错误码不正确：{exc.code}") from exc
        idempotent = state_lib.perform_transition(
            workspace, project_dir, to_stage="info_extract", idempotent=True
        )
        if not idempotent.get("alreadyInStage"):
            raise AssertionError("--idempotent 未返回已在该阶段")
        checks.append("already_in_stage")

        try:
            state_lib.check_transition(workspace, project_dir, to_stage="outline")
            raise AssertionError("非法迁移未报错")
        except errors.BgError as exc:
            if exc.code != errors.E_ILLEGAL_TRANSITION or exc.detail.get("allowedNext") != ["package_prepare"]:
                raise AssertionError(f"非法迁移 detail 不正确：{exc.to_dict()}") from exc
        checks.append("illegal_transition")

        try:
            state_lib.check_transition(workspace, project_dir, to_stage="package_prepare")
            state_lib.perform_transition(workspace, project_dir, to_stage="package_prepare")
            raise AssertionError("T2 guard 在未回填数据时应失败")
        except errors.BgError as exc:
            if exc.code != errors.E_GUARD_FAILED:
                raise AssertionError(f"T2 guard 失败错误码不正确：{exc.code}") from exc
        _fill_t2_fixture(project_dir)
        check_t2 = state_lib.check_transition(workspace, project_dir, to_stage="package_prepare")
        if not check_t2["pass"]:
            raise AssertionError(f"T2 guard 未通过：{check_t2['guard']['checks']}")
        t2 = state_lib.perform_transition(workspace, project_dir, to_stage="package_prepare", actor="script")
        if t2["to"] != "package_prepare" or t2["to"] != "package_prepare":
            raise AssertionError(f"T2 迁移结果不正确：{t2}")
        status = state_lib.status_summary(workspace, project_dir)
        if status["currentScope"] != "package-1" or status["currentPackage"] != "package-1":
            raise AssertionError(f"T2 未切到 package-1：{status['currentScope']}")
        checks.append("transition_t2")

        set_pkg = state_lib.set_package(workspace, project_dir, package="package-1")
        if set_pkg["changed"] != []:
            raise AssertionError("set-package 幂等失败")
        checks.append("set_package")

        abort = state_lib.abort_project(workspace, project_dir, reason="自检取消")
        if abort["to"] != "aborted":
            raise AssertionError("abort 失败")
        resume = state_lib.resume_project(workspace, project_dir)
        if resume["to"] != "package_prepare" or resume["scope"] != "package-1":
            raise AssertionError(f"resume 未回到原阶段：{resume}")
        checks.append("abort_resume")

        state_file = project_dir / "state.json"
        backup = state_file.read_bytes()
        state_file.write_bytes(b'{"broken": ')
        try:
            state_lib.load_state(project_dir)
            raise AssertionError("损坏 state.json 未报 E_STATE_CORRUPT")
        except errors.BgError as exc:
            if exc.code != errors.E_STATE_CORRUPT:
                raise AssertionError(f"损坏 state 错误码不正确：{exc.code}") from exc
        finally:
            state_file.write_bytes(backup)
        checks.append("corrupt_state")

        return {
            "ok": True,
            "selftest": {
                "script": "bg_state.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_state.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 子命令（PRD 4.4 / CONTRACT §8.3）。"""
    parser = errors.CliParser(
        prog="bg_state.py",
        description="BidGenie M2 状态机：status/check/transition/set-package/abort/resume",
    )
    errors.add_common_args(parser)
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    def add_sub(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        errors.add_common_args(sub, subparser=True)
        sub.add_argument("--project", required=True, help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
        sub.add_argument("--workspace", help="工作区根绝对路径（--project 传 projectId 时必填）")
        return sub

    add_sub("status", "只读输出状态摘要与 allowedNext")
    check = add_sub("check", "只读校验一次迁移（guard 失败仍 exit 0，pass=false）")
    check.add_argument("--to", required=True, help="目标阶段")
    check.add_argument("--package", help="包标识 package-<N>（包级迁移用）")
    check.add_argument("--actor", default="agent", choices=ACTORS)
    check.add_argument("--reason", default="", help="迁移原因（写入 history）")
    transition = add_sub("transition", "执行一次合法迁移（写 state.json + history + registry 镜像）")
    transition.add_argument("--to", required=True, help="目标阶段")
    transition.add_argument("--package", help="包标识 package-<N>")
    transition.add_argument("--actor", default="agent", choices=ACTORS)
    transition.add_argument("--reason", default="", help="迁移原因（写入 history）")
    transition.add_argument("--idempotent", action="store_true", help="已在目标阶段时返回成功且不写 history")
    set_package = add_sub("set-package", "设置当前包（不迁移阶段，幂等）")
    set_package.add_argument("--package", required=True, help="包标识 package-<N>")
    abort = add_sub("abort", "T11：任意活动阶段 → aborted（保留现场）")
    abort.add_argument("--reason", required=True, help="取消原因（写入 abort.reason 与 history）")
    add_sub("resume", "T12：aborted → 原阶段（从 history 推导）")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按子命令分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if args.command is None:
        raise errors.BgError(
            errors.E_USAGE,
            "缺少子命令",
            hint="可用子命令：status/check/transition/set-package/abort/resume；或 --selftest",
        )
    dispatch = {
        "status": command_status,
        "check": command_check,
        "transition": command_transition,
        "set-package": command_set_package,
        "abort": command_abort,
        "resume": command_resume,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        raise errors.BgError(errors.E_USAGE, f"未知子命令：{args.command}")
    return handler(args)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_state.py",
    )


if __name__ == "__main__":
    sys.exit(main())
