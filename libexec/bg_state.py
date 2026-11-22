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
    python bg_state.py projects   [--workspace <ws>] [--json]
    python bg_state.py doctor     --project <dir|id> [--workspace <ws>] [--json]
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
        result["packageSummary"] = _package_summary(project_dir, str(result.get("scope") or args.package or ""))
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
    result["packageSummary"] = _package_summary(project_dir, str(result.get("currentPackage") or args.package or ""))
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


def _package_summary(project_dir: Path, package_id: str) -> dict[str, Any]:
    """切包后摘要：让 Agent 不必再读 package-meta（T13/多包衔接）。"""
    if not package_id:
        return {}
    dirs = state_lib._package_dirs(project_dir, package_id)  # noqa: SLF001
    if not dirs:
        return {"packageId": package_id, "packageDir": "", "note": "包目录尚未 prepare"}
    package_dir = dirs[-1]
    summary: dict[str, Any] = {"packageId": package_id, "packageDir": str(package_dir.relative_to(project_dir).as_posix())}
    meta_path = package_dir / "package-meta.json"
    if not meta_path.is_file():
        summary["note"] = "package-meta.json 缺失"
        return summary
    try:
        meta = jsonio.read_json(meta_path)
    except errors.BgError as exc:
        summary["note"] = f"package-meta 不可解析：{exc.message}"
        return summary
    for key in ("packageName", "expectedWordCount", "figureQuota", "personnelPlan", "equipmentPlan", "schedulePlan"):
        if key in meta:
            summary[key] = meta.get(key)
    summary["valueSource"] = meta.get("valueSource") or {}
    return summary


def command_projects(args: argparse.Namespace) -> dict[str, Any]:
    """projects：列出工作区注册项目（解决 status 必须传 --project 的入口问题）。"""
    workspace = paths.normalize_workspace(getattr(args, "workspace", None) or Path.cwd())
    registry = state_lib.read_registry(workspace)
    rows: list[dict[str, Any]] = []
    for entry in registry.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        dir_name = str(entry.get("dirName") or "")
        project_dir = workspace / dir_name
        rows.append(
            {
                "projectId": entry.get("projectId"),
                "dirName": dir_name,
                "projectName": entry.get("projectName"),
                "currentStage": entry.get("currentStage"),
                "currentPackage": entry.get("currentPackage"),
                "exists": bool(project_dir.is_dir()),
                "updatedAt": entry.get("updatedAt"),
            }
        )
    return {
        "ok": True,
        "command": "state_projects",
        "workspace": str(workspace),
        "count": len(rows),
        "projects": rows,
    }


def _mtime_iso(path: Path) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _doctor_package(project_dir: Path, package_id: str) -> dict[str, Any]:
    """单包续跑诊断：章节完成度 / stale chapter-result / 未关闭问题 / 图表状态。"""
    from _lib import text as text_lib

    dirs = state_lib._package_dirs(project_dir, package_id)  # noqa: SLF001 - 复用状态机同口径定位
    result: dict[str, Any] = {
        "packageId": package_id,
        "packageDir": "",
        "hasMeta": False,
        "hasOutline": False,
        "leafCount": 0,
        "done": 0,
        "pending": [],
        "blocked": [],
        "skipped": [],
        "staleResults": [],
        "nextBatch": [],
        "review": {},
        "figures": {},
    }
    if not dirs:
        result["note"] = "包目录不存在（尚未 F4 prepare）"
        return result
    package_dir = dirs[-1]
    result["packageDir"] = str(package_dir.relative_to(project_dir).as_posix())
    result["hasMeta"] = (package_dir / "package-meta.json").is_file()
    outline_path = package_dir / "chapters" / "_outline" / "outline.json"
    result["hasOutline"] = outline_path.is_file()
    if outline_path.is_file():
        try:
            outline = jsonio.read_json(outline_path)
        except errors.BgError:
            outline = {}
        def walk(nodes: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for node in nodes if isinstance(nodes, list) else []:
                if not isinstance(node, dict):
                    continue
                children = node.get("children")
                if isinstance(children, list) and children:
                    out.extend(walk(children))
                else:
                    out.append(node)
            return out

        leaves = walk(outline.get("chapters") or [])
        result["leafCount"] = len(leaves)
        for node in leaves:
            chapter_id = str(node.get("id") or "")
            status = str((node.get("completion") or {}).get("status") or "pending")
            if status == "done":
                result["done"] += 1
            elif status == "blocked":
                result["blocked"].append(chapter_id)
            elif status == "skipped":
                result["skipped"].append(chapter_id)
            else:
                result["pending"].append(chapter_id)
            file_path = node.get("filePath")
            md_path = package_dir / str(file_path) if isinstance(file_path, str) and file_path else None
            result_path = package_dir / "chapters" / "_results" / f"{chapter_id}.chapter-result.json"
            if status == "done" and md_path is not None and md_path.is_file() and result_path.is_file():
                try:
                    measured = text_lib.count_body_words(md_path.read_text(encoding="utf-8-sig", errors="replace"))
                    doc = jsonio.read_json(result_path)
                    reported = int(doc.get("wordCount") or 0) if isinstance(doc, dict) else 0
                except (OSError, errors.BgError, ValueError):
                    continue
                md_newer = md_path.stat().st_mtime > result_path.stat().st_mtime + 1.0
                deviation = abs(reported - measured) / measured * 100 if measured > 0 else 0.0
                if reported and (md_newer or deviation > 5.0):
                    result["staleResults"].append(
                        {
                            "chapterId": chapter_id,
                            "reportedWordCount": reported,
                            "measuredWordCount": measured,
                            "deviationPct": round(deviation, 2),
                            "mdMtime": _mtime_iso(md_path),
                            "resultMtime": _mtime_iso(result_path),
                            "mdNewerThanResult": md_newer,
                        }
                    )
        result["nextBatch"] = result["pending"][:6]
        figure_index = [item for item in outline.get("figureIndex") or [] if isinstance(item, dict)]
        result["figures"] = {
            "total": len(figure_index),
            "done": sum(1 for item in figure_index if str(item.get("status")) == "done"),
            "planned": sum(1 for item in figure_index if str(item.get("status")) == "planned"),
            "deferred": sum(1 for item in figure_index if str(item.get("status")) == "deferred"),
        }
    issues_path = package_dir / "reviews" / "issues.json"
    if issues_path.is_file():
        try:
            issues_doc = jsonio.read_json(issues_path)
            issues = [item for item in issues_doc.get("issues") or [] if isinstance(item, dict)]
            by_status: dict[str, int] = {}
            for issue in issues:
                by_status[str(issue.get("status") or "open")] = by_status.get(str(issue.get("status") or "open"), 0) + 1
            result["review"] = {
                "round": issues_doc.get("round"),
                "total": len(issues),
                "byStatus": by_status,
                "openIds": [item.get("issueId") for item in issues if str(item.get("status")) in ("open", "fixing")],
            }
        except errors.BgError:
            result["review"] = {"error": "issues.json 不可解析"}
    return result


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    """doctor：断点续跑诊断（只读）——当前阶段、待做章节、stale 结果、未关闭问题与建议命令。"""
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    status = state_lib.status_summary(workspace, project_dir)
    state = state_lib.load_state(project_dir)
    project = state_lib.load_project(project_dir)
    current_package = str(getattr(args, "package", None) or state.get("currentPackage") or (project.get("packagesToWrite") or ["package-1"])[0])
    package_info = _doctor_package(project_dir, current_package)
    history = state.get("history") or []
    last_transition = state_lib.history_transitions(history)[-1] if state_lib.history_transitions(history) else None
    last_event = state_lib.history_events(history)[-1] if state_lib.history_events(history) else None
    stage = str(state.get("currentStage") or "")
    script = "python -X utf8 <scriptsDir>/"
    recommended: list[str] = []
    if stage == "outline":
        recommended = [
            f"{script}bg_outline_validate.py --strict --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_outline_apply.py --materialize --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_state.py check --to draft --workspace <ws> --project <prj> --package {current_package} --json",
        ]
    elif stage == "draft":
        recommended = [
            f"{script}bg_chapter_merge.py --verify-all --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_render.py --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_state.py check --to review --workspace <ws> --project <prj> --package {current_package} --json",
        ]
    elif stage == "review":
        recommended = [
            f"{script}bg_review_script.py --round <k> --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_review_issues.py status --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_review_issues.py verify --workspace <ws> --project <prj> --package {current_package} --json",
        ]
    elif stage == "optimize":
        recommended = [
            f"{script}bg_chapter_merge.py --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_render.py --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_state.py check --to review --workspace <ws> --project <prj> --package {current_package} --json",
        ]
    elif stage in ("finalize", "export"):
        recommended = [
            f"{script}bg_finalize.py --lock --workspace <ws> --project <prj> --package {current_package} --json",
            f"{script}bg_state.py check --to export --workspace <ws> --project <prj> --package {current_package} --json",
        ]
    warnings: list[str] = []
    if package_info.get("staleResults"):
        warnings.append(f"{len(package_info['staleResults'])} 个章节的 chapter-result 与 md 字数/mtime 不一致，见 staleResults")
    if package_info.get("review", {}).get("openIds"):
        warnings.append(f"{len(package_info['review']['openIds'])} 个问题未关闭")
    if package_info.get("blocked"):
        warnings.append(f"{len(package_info['blocked'])} 个章节 blocked")
    return {
        "ok": True,
        "command": "state_doctor",
        "projectId": state.get("projectId"),
        "projectName": project.get("projectName"),
        "currentStage": stage,
        "currentPackage": current_package,
        "revision": state.get("revision"),
        "allowedNext": status.get("allowedNext"),
        "lastTransition": last_transition,
        "lastStateEvent": last_event,
        "eventLogCount": status.get("eventLogCount"),
        "openIssueCount": len((package_info.get("review") or {}).get("openIds") or []),
        "package": package_info,
        "warnings": warnings,
        "recommendedCommands": recommended,
        "note": "只读诊断；不写 state/outline/issues。staleResults 表示 md 已改但 chapter-result 未同步，建议重写该章 chapter-result 或重跑合并前先回写字数。",
    }


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
        projects = command_projects(argparse.Namespace(workspace=str(workspace)))
        doctor = command_doctor(argparse.Namespace(project=str(project_dir), workspace=str(workspace), package=None))
        if (
            projects.get("count") != 1
            or projects.get("projects", [{}])[0].get("projectId") != project_result.get("projectId")
            or doctor.get("command") != "state_doctor"
            or doctor.get("currentStage") != "project_init"
            or "recommendedCommands" not in doctor
        ):
            raise AssertionError(f"projects/doctor 自检失败：projects={projects} doctor={doctor}")
        checks.append("projects_doctor")

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
    projects = subparsers.add_parser("projects", help="列出工作区已注册项目（不需要 --project）")
    errors.add_common_args(projects, subparser=True)
    projects.add_argument("--workspace", help="工作区根绝对路径（缺省为当前目录）")
    doctor = add_sub("doctor", "断点续跑诊断（只读）：阶段/待做章节/stale 结果/issues/建议命令")
    doctor.add_argument("--package", help="诊断指定包（缺省 state.currentPackage）")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按子命令分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if args.command is None:
        raise errors.BgError(
            errors.E_USAGE,
            "缺少子命令",
            hint="可用子命令：status/check/transition/set-package/abort/resume/projects/doctor；或 --selftest",
        )
    dispatch = {
        "status": command_status,
        "check": command_check,
        "transition": command_transition,
        "set-package": command_set_package,
        "abort": command_abort,
        "resume": command_resume,
        "projects": command_projects,
        "doctor": command_doctor,
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
