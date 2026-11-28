#!/usr/bin/env python3
"""BidGenie 环境修复：--plan / --apply / --verify / --rollback / --selftest。

红线：
- 只执行 env-actions.json 中 mode=auto 的白名单动作；
- pip 以参数列表调用（不拼接 shell），先装到 ``<target>.new``，逐 import 验证，
  原子替换并把旧目录备份 ``.broken-<ts>``；
- 多个动作一起安装时按动作分别验证：只要有一个动作导入通过就提交 staging，
  失败动作写入 ``partial``/``failedActions``，避免某个可选组件（如 pywin32）
  把已装好的 PDF 组件一起丢掉；
- 写入仅 data pylibs、``dsh/logs``、``dsh/locks``、env-check.json（通过 bg_env_check）；
- 不修改 DSH bundled python site-packages。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

_VENDOR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _VENDOR.parent
for _entry in (str(_VENDOR), str(_PLUGIN_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from _lib import deps, errors, jsonio, paths, tempdir  # noqa: E402

SCHEMA_VERSION = "1.0"
ACTIONS_FILE = _VENDOR / "env-actions.json"
ENV_CHECK_SCRIPT = _VENDOR / "bg_env_check.py"
LOCK_REL = "dsh/locks/env-repair.lock"
LOG_DIR_REL = "dsh/logs"
ENV_CHECK_REL = "dsh/config/env-check.json"


def load_actions() -> dict[str, Any]:
    document = jsonio.read_json(ACTIONS_FILE)
    if not isinstance(document, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "env-actions.json 必须是对象")
    return document


def repair_target() -> Path:
    raw = os.environ.get("BIDGENIE_PYLIBS")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser().resolve()
    return deps.data_pylibs_dir()


def _current_report(workspace: Path) -> dict[str, Any]:
    env_path = workspace / ENV_CHECK_REL
    if not env_path.is_file():
        return {"items": [], "status": "not-run"}
    document = jsonio.read_json(env_path)
    import bg_env_check

    checklist = bg_env_check.load_checklist()
    return bg_env_check.build_report(workspace, checklist, document, command="env_repair_plan")


def _failed_items(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in report.get("items") or []:
        if isinstance(item, Mapping) and item.get("status") == "failed":
            result[str(item.get("id"))] = dict(item.get("detail") or {})
    return result


def plan_repair(workspace: str | Path, *, report: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ws = paths.normalize_workspace(workspace)
    actions_doc = load_actions()
    current = dict(report) if report is not None else _current_report(ws)
    failed = _failed_items(current)
    groups: dict[str, list[dict[str, Any]]] = {"auto": [], "assisted": [], "manual": [], "pluginDefect": []}
    claimed: set[str] = set()
    for action in actions_doc.get("actions") or []:
        if not isinstance(action, Mapping) or str(action.get("mode") or "") != "auto":
            continue
        matched = [item for item in action.get("items") or [] if str(item) in failed]
        if not matched:
            continue
        claimed.update(str(item) for item in matched)
        groups["auto"].append(
            {
                "id": action.get("id"),
                "mode": "auto",
                "items": matched,
                "why": action.get("why"),
                "downloadMB": action.get("downloadMB", 0),
                "writes": action.get("writes") or [],
                "packages": action.get("packages") or [],
                "verifyImports": action.get("verifyImports") or [],
            }
        )
    for item_id, detail in failed.items():
        if item_id in claimed:
            continue
        remediable = str(detail.get("remediable") or "")
        entry = {"id": item_id, "cause": detail.get("cause"), "causeDetail": detail.get("causeDetail"), "why": detail.get("why") or "见 causeDetail"}
        if remediable == "assisted":
            groups["assisted"].append(entry)
        elif remediable == "plugin_defect":
            groups["pluginDefect"].append(entry)
        else:
            groups["manual"].append(entry)
    return {
        "ok": True,
        "command": "env_repair_plan",
        "workspace": str(ws),
        "target": str(repair_target()),
        "status": "ready" if failed else "nothing-to-repair",
        "failedItems": sorted(failed),
        "groups": groups,
        "counts": {key: len(value) for key, value in groups.items()},
        "actionsFile": paths.rel_posix(_PLUGIN_ROOT, ACTIONS_FILE),
        "rootCause": current.get("rootCause"),
        "installHint": current.get("installHint") or "",
    }


def _resolve_actions(actions_doc: Mapping[str, Any], item_ids: Sequence[str]) -> list[dict[str, Any]]:
    requested = {str(item) for item in item_ids}
    actions = [dict(action) for action in actions_doc.get("actions") or [] if isinstance(action, Mapping)]
    matched = [action for action in actions if requested.intersection(str(item) for item in action.get("items") or [])]
    if not matched:
        raise errors.BgError(errors.E_USAGE, f"没有可自动修复的动作覆盖：{', '.join(sorted(requested))}", hint="先运行 --plan 查看 auto/manual/pluginDefect 分组", detail={"items": sorted(requested)})
    non_auto = [action for action in matched if str(action.get("mode") or "") != "auto"]
    if non_auto:
        raise errors.BgError(errors.E_USAGE, "只允许白名单 auto 动作", detail={"actions": [action.get("id") for action in non_auto]})
    return matched


def _log_path(workspace: Path, job_id: str) -> Path:
    directory = workspace / LOG_DIR_REL
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"env-repair-{job_id}.jsonl"


def _append_log(log_path: Path, job_id: str, event: str, **detail: Any) -> None:
    payload = {"at": paths.iso_now(), "jobId": job_id, "event": event, **detail}
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


#: 自检子进程必须按运行时同一套规则挂载 pylibs：只设 ``PYTHONPATH`` 时
#: ``win32com`` 能找到包，但 ``pywintypes*.dll`` 不在 DLL 搜索路径，pywin32 会被
#: 误判为导入失败（见 env-repair 日志 2026-11-19）。
_PROBE_CODE = """\
import sys
sys.path.insert(0, sys.argv[1])
from _lib import deps
deps.ensure_dependency_path(base=sys.argv[2])
import importlib
for name in sys.argv[3:]:
    try:
        importlib.import_module(name)
        print(name + "=ok")
    except Exception as exc:
        print(name + "=" + type(exc).__name__ + ": " + str(exc).replace(chr(10), chr(32)))
"""


def _import_probe(imports: Sequence[str], staging: Path) -> dict[str, dict[str, str]]:
    env = {**os.environ, "BIDGENIE_PYLIBS": str(staging), "PYTHONPATH": str(staging)}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _PROBE_CODE, str(_VENDOR), str(staging), *imports],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
        cwd=str(_PLUGIN_ROOT),
    )
    result: dict[str, dict[str, str]] = {}
    for line in (completed.stdout or "").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        result[name] = {"status": "ok" if value == "ok" else "failed", "detail": value}
    return result


def _run_env_check(workspace: Path, item_ids: Sequence[str]) -> dict[str, Any]:
    command = [sys.executable, "-X", "utf8", str(ENV_CHECK_SCRIPT), "--apply", "--workspace", str(workspace), "--json"]
    for item_id in item_ids:
        command.extend(["--item", item_id])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, cwd=str(_PLUGIN_ROOT), env={**os.environ, "BIDGENIE_SKIP_ENV_REFRESH": "1"})
    payload = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    return {"exitCode": completed.returncode, "payload": payload, "stdoutTail": (completed.stdout or "")[-1000:], "stderrTail": (completed.stderr or "")[-1000:]}


def apply_repair(
    workspace: str | Path,
    *,
    item_ids: Sequence[str],
    yes: bool = False,
    pip_command: Sequence[str] | None = None,
    timeout_seconds: float = 1800,
) -> dict[str, Any]:
    if not yes:
        raise errors.BgError(errors.E_USAGE, "环境修复会写入 data pylibs，必须加 --yes", hint="先 --plan 查看动作")
    ws = paths.normalize_workspace(workspace)
    actions_doc = load_actions()
    actions = _resolve_actions(actions_doc, item_ids)
    index_url = str(actions_doc.get("defaultIndexUrl") or "https://pypi.tuna.tsinghua.edu.cn/simple")
    target = repair_target()
    staging = target.with_name(target.name + ".new")
    job_id = f"{paths.timestamp14()}-{paths.rand4()}"
    log_path = _log_path(ws, job_id)
    lock_path = ws / LOCK_REL
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pip = list(pip_command or [sys.executable, "-m", "pip"])
    _append_log(log_path, job_id, "start", items=sorted(set(item_ids)), actions=[action.get("id") for action in actions], target=str(target))
    backup: Path | None = None
    replaced = False
    result: dict[str, Any] = {"ok": False, "command": "env_repair_apply", "jobId": job_id, "logPath": paths.rel_posix(ws, log_path), "target": str(target), "itemIds": sorted(set(item_ids))}
    try:
        with jsonio.FileLock(lock_path, timeout=jsonio.LOCK_TIMEOUT_SEC):
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            packages: list[str] = []
            imports: list[str] = []
            for action in actions:
                packages.extend(str(package) for package in action.get("packages") or [])
                imports.extend(str(module) for module in action.get("verifyImports") or [])
            command = [*pip, "install", "--disable-pip-version-check", "--no-input", "--upgrade", "--target", str(staging), "--index-url", index_url, *packages]
            _append_log(log_path, job_id, "pip-start", command=command, packages=packages)
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds, cwd=str(_PLUGIN_ROOT))
            _append_log(log_path, job_id, "pip-done", exitCode=completed.returncode, stdoutTail=(completed.stdout or "")[-2000:], stderrTail=(completed.stderr or "")[-2000:])
            if completed.returncode != 0:
                raise errors.BgError("E_REPAIR_FAILED", "依赖安装失败", hint="查看 env-repair 日志；已回滚未改动旧目录", detail={"jobId": job_id, "exitCode": completed.returncode}, exit_code=errors.EXIT_PRECONDITION)
            probes = _import_probe(sorted(set(imports)), staging)
            _append_log(log_path, job_id, "probe", probes=probes)
            # 按动作分别判定：一个可选动作导入失败（例如 pywin32 的 DLL 目录问题）
            # 不应把同一批已经装好的其它组件一起回滚。
            actions_report: list[dict[str, Any]] = []
            for action in actions:
                action_imports = [str(module) for module in action.get("verifyImports") or []]
                failed_imports = {
                    name: probes.get(name, {"status": "failed", "detail": "probe missing"})
                    for name in action_imports
                    if probes.get(name, {}).get("status") != "ok"
                }
                actions_report.append(
                    {
                        "id": action.get("id"),
                        "items": [str(item) for item in action.get("items") or []],
                        "packages": [str(package) for package in action.get("packages") or []],
                        "status": "ok" if not failed_imports else "failed",
                        "failedImports": failed_imports,
                    }
                )
            ok_actions = [entry for entry in actions_report if entry["status"] == "ok"]
            failed_actions = [entry for entry in actions_report if entry["status"] != "ok"]
            _append_log(log_path, job_id, "probe-groups", actions=actions_report)
            if not ok_actions:
                raise errors.BgError(
                    "E_REPAIR_FAILED",
                    "新依赖导入验证失败",
                    hint="保留旧目录；检查 pylibs 二进制",
                    detail={
                        "jobId": job_id,
                        "actions": failed_actions,
                        "failedImports": {name: info for name, info in probes.items() if info.get("status") != "ok"},
                    },
                    exit_code=errors.EXIT_PRECONDITION,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.with_name(target.name + f".broken-{paths.timestamp14()}")
                target.rename(backup)
            staging.rename(target)
            replaced = True
            _append_log(log_path, job_id, "replaced", backup=str(backup) if backup else "", target=str(target))
            # 只复验“导入验证已通过”的动作；失败动作保留在 partial 里，等下次重试。
            verified_items = sorted({str(item) for entry in ok_actions for item in entry.get("items") or []})
            verification = _run_env_check(ws, verified_items)
            _append_log(log_path, job_id, "env-check", exitCode=verification.get("exitCode"), payload=verification.get("payload"))
            failed_after = [
                item
                for item in (verification.get("payload") or {}).get("items") or []
                if item.get("id") in set(verified_items) and item.get("status") == "failed"
            ]
            # 导入已验证通过，但 env-check 仍失败时要区分责任：例如 pywin32 已装好、
            # 只是本机没有 Word，属于人工处理项，不应把装好的 PDF 组件一起回滚。
            blocking_after = [item for item in failed_after if str((item.get("detail") or {}).get("remediable") or "") != "manual"]
            manual_after = [item for item in failed_after if item not in blocking_after]
            if blocking_after or (verification.get("exitCode") != 0 and not failed_after):
                raise errors.BgError(
                    "E_REPAIR_FAILED",
                    "修复后 env-check 复验仍失败",
                    hint="已回滚到旧 pylibs",
                    detail={"jobId": job_id, "items": [item.get("id") for item in blocking_after]},
                    exit_code=errors.EXIT_PRECONDITION,
                )
            warnings: list[str] = []
            if failed_actions:
                warnings.append("部分依赖已安装，但个别组件导入验证失败；其余组件可正常使用，失败项见修复日志。")
            if manual_after:
                warnings.append("以下检查项仍需手动处理：" + "、".join(str(item.get("id")) for item in manual_after))
            result.update(
                {
                    "ok": True,
                    "backup": str(backup) if backup else "",
                    "probes": probes,
                    "verification": verification,
                    "actions": [entry["id"] for entry in ok_actions],
                    "actionsReport": actions_report,
                }
            )
            if failed_actions:
                result.update({"partial": True, "failedActions": failed_actions})
            if manual_after:
                result["manualRemaining"] = [str(item.get("id")) for item in manual_after]
            if warnings:
                result["warnings"] = warnings
            if failed_actions:
                _append_log(log_path, job_id, "partial", result=result)
                return result
            _append_log(log_path, job_id, "done", result=result)
            return result
    except errors.BgError:
        if replaced and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup is not None and backup.exists() and not target.exists():
            backup.rename(target)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _append_log(log_path, job_id, "failed", target=str(target), backup=str(backup) if backup else "")
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_repair(workspace: str | Path) -> dict[str, Any]:
    ws = paths.normalize_workspace(workspace)
    log_dir = ws / LOG_DIR_REL
    logs = sorted(log_dir.glob("env-repair-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True) if log_dir.is_dir() else []
    if not logs:
        return {"ok": False, "command": "env_repair_verify", "error": "E_NOT_FOUND", "message": "没有 env-repair 日志，先 --apply", "exitCode": 4}
    events = [json.loads(line) for line in logs[0].read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    report = _current_report(ws)
    failed = _failed_items(report)
    return {"ok": True, "command": "env_repair_verify", "jobLog": paths.rel_posix(ws, logs[0]), "events": events[-20:], "failedItems": sorted(failed), "status": "repaired" if not failed else "still-failing", "report": report}


def rollback_repair(workspace: str | Path) -> dict[str, Any]:
    ws = paths.normalize_workspace(workspace)
    target = repair_target()
    backups = sorted(target.parent.glob(target.name + ".broken-*"), key=lambda path: path.stat().st_mtime, reverse=True) if target.parent.is_dir() else []
    if not backups:
        raise errors.BgError("E_REPAIR_FAILED", "没有可回滚的备份", hint="--apply 成功时会保留 .broken-<ts>", exit_code=errors.EXIT_PRECONDITION)
    backup = backups[0]
    job_id = f"rollback-{paths.timestamp14()}-{paths.rand4()}"
    log_path = _log_path(ws, job_id)
    lock_path = ws / LOCK_REL
    with jsonio.FileLock(lock_path, timeout=jsonio.LOCK_TIMEOUT_SEC):
        rolled = target.with_name(target.name + f".rolledback-{paths.timestamp14()}")
        if target.exists():
            target.rename(rolled)
        backup.rename(target)
        _append_log(log_path, job_id, "rollback", backup=str(backup), target=str(target), previous=str(rolled))
    return {"ok": True, "command": "env_repair_rollback", "jobId": job_id, "restoredFrom": str(backup), "target": str(target), "logPath": paths.rel_posix(ws, log_path)}


def run_selftest() -> dict[str, Any]:
    tmp = tempdir.mkdtemp(prefix="bidgenie-env-repair-selftest-")
    checks: list[dict[str, Any]] = []
    try:
        workspace = paths.normalize_workspace(tmp)
        fake_report = {
            "items": [
                {"id": "python_packages", "status": "failed", "detail": {"cause": "version_mismatch", "remediable": "auto", "mismatched": ["Pillow>=12.3"]}},
                {"id": "pdf_libraries", "status": "failed", "detail": {"cause": "import_error", "remediable": "auto", "errors": {"fitz": {"type": "ImportError"}}}},
                {"id": "scripts_integrity", "status": "failed", "detail": {"cause": "selftest_json_broken", "remediable": "plugin_defect"}},
            ]
        }
        plan = plan_repair(workspace, report=fake_report)
        checks.append({"id": "plan_groups", "ok": plan["counts"]["auto"] == 2 and plan["counts"]["pluginDefect"] == 1, "detail": plan["counts"]})
        actions = load_actions()
        checks.append({"id": "actions_whitelist", "ok": all(action.get("mode") == "auto" for action in actions.get("actions") or []), "detail": [action.get("id") for action in actions.get("actions") or []]})
        checks.append({"id": "target_data_default", "ok": ".new" not in str(repair_target()) and "pylibs" in str(repair_target()), "detail": str(repair_target())})
        failed = [item for item in checks if not item["ok"]]
        return {"ok": not failed, "command": "env_repair_selftest", "checks": checks, "failed": len(failed)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(prog="bg_env_repair.py", description="BidGenie 环境修复：plan/apply/verify/rollback/--selftest")
    errors.add_common_args(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="分组输出可修复动作（auto/assisted/manual/pluginDefect）")
    group.add_argument("--apply", action="store_true", help="执行白名单 auto 修复（必须 --yes）")
    group.add_argument("--verify", action="store_true", help="对比最近修复日志与当前 env-check")
    group.add_argument("--rollback", action="store_true", help="恢复最近 .broken-<ts> 备份")
    group.add_argument("--selftest", action="store_true", help="不联网自检")
    parser.add_argument("--workspace", help="工作区根的绝对路径")
    parser.add_argument("--item", action="append", dest="items", metavar="ID", help="目标检查项（可重复）")
    parser.add_argument("--yes", action="store_true", help="确认执行修复（写 data pylibs）")
    parser.add_argument("--pip", help="pip 可执行路径（测试可注入 fake pip）")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.workspace:
        raise errors.BgError(errors.E_USAGE, "缺少 --workspace", hint="传入工作区根的绝对路径")
    if args.plan:
        return plan_repair(args.workspace)
    if args.apply:
        item_ids = list(getattr(args, "items", None) or [])
        if not item_ids:
            raise errors.BgError(errors.E_USAGE, "--apply 需要 --item（可重复）", hint="先 --plan 看 auto 组")
        pip_command = [getattr(args, "pip")] if getattr(args, "pip", None) else None
        return apply_repair(args.workspace, item_ids=item_ids, yes=bool(args.yes), pip_command=pip_command, timeout_seconds=float(getattr(args, "timeout", 1800) or 1800))
    if args.verify:
        return verify_repair(args.workspace)
    if args.rollback:
        return rollback_repair(args.workspace)
    raise errors.BgError(errors.E_USAGE, "缺少动作", hint="--plan / --apply / --verify / --rollback / --selftest")


def main(argv: list[str] | None = None) -> int:
    deps.ensure_dependency_path()
    return errors.run_cli(argv, parser=build_parser(), handler=handle, script_name="bg_env_repair.py")


if __name__ == "__main__":
    sys.exit(main())
