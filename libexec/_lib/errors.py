"""统一错误码、退出码与 CLI JSON 输出（PRD 8.5）。

错误码唯一来源是 PRD 8.5；本模块额外定义两个 M1 内部扩展码，PRD 未列通用 IO/兜底码：
- ``E_IO``         文件系统/权限类失败（退出码 5）
- ``E_UNEXPECTED`` 未分类的兜底失败（退出码 1）
二者已在 ``docs/acceptance/M1/backend-selftest.md`` 记为遗留项，待 PRD 8.5 增补后替换，
脚本输出中不新增其他错误码。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 退出码（PRD 8.5，唯一来源）
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_SCHEMA = 4
EXIT_IO = 5
EXIT_TIMEOUT = 6
EXIT_LOCK = 7
EXIT_DEP = 8
EXIT_DECISION = 9

# ---------------------------------------------------------------------------
# 错误码（PRD 8.5）
# ---------------------------------------------------------------------------
E_USAGE = "E_USAGE"
E_NOT_FOUND = "E_NOT_FOUND"
E_SCHEMA_INVALID = "E_SCHEMA_INVALID"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_ILLEGAL_TRANSITION = "E_ILLEGAL_TRANSITION"
E_ALREADY_IN_STAGE = "E_ALREADY_IN_STAGE"
E_GUARD_FAILED = "E_GUARD_FAILED"
E_LOCKED = "E_LOCKED"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_TIMEOUT = "E_TIMEOUT"
E_DEP_MISSING = "E_DEP_MISSING"
E_CONVERT_FAILED = "E_CONVERT_FAILED"
E_VALIDATE_FAILED = "E_VALIDATE_FAILED"
E_USER_DECISION_REQUIRED = "E_USER_DECISION_REQUIRED"
E_NETWORK = "E_NETWORK"
E_EXPORT_FAILED = "E_EXPORT_FAILED"

# M1 内部扩展（见模块 docstring）
E_IO = "E_IO"
E_UNEXPECTED = "E_UNEXPECTED"

#: 错误码 -> 退出码（PRD 8.5）
ERROR_EXIT: dict[str, int] = {
    E_USAGE: EXIT_USAGE,
    E_NOT_FOUND: EXIT_PRECONDITION,
    E_SCHEMA_INVALID: EXIT_SCHEMA,
    E_STATE_CORRUPT: EXIT_SCHEMA,
    E_ILLEGAL_TRANSITION: EXIT_PRECONDITION,
    E_ALREADY_IN_STAGE: EXIT_PRECONDITION,
    E_GUARD_FAILED: EXIT_PRECONDITION,
    E_LOCKED: EXIT_PRECONDITION,
    E_LOCK_TIMEOUT: EXIT_LOCK,
    E_TIMEOUT: EXIT_TIMEOUT,
    E_DEP_MISSING: EXIT_DEP,
    E_CONVERT_FAILED: EXIT_GENERIC,
    E_VALIDATE_FAILED: EXIT_SCHEMA,
    E_USER_DECISION_REQUIRED: EXIT_DECISION,
    E_NETWORK: EXIT_GENERIC,
    E_EXPORT_FAILED: EXIT_GENERIC,
    E_IO: EXIT_IO,
    E_UNEXPECTED: EXIT_GENERIC,
}


class BgError(Exception):
    """可映射为失败 JSON 与进程退出码的业务异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        detail: Mapping[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.detail: dict[str, Any] = dict(detail) if detail else {}
        self.exit_code = ERROR_EXIT[code] if exit_code is None else exit_code

    def to_dict(self) -> dict[str, Any]:
        """转为 PRD H.0 规定的失败 JSON 对象。"""
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.detail:
            payload["detail"] = self.detail
        return payload


def fail(
    code: str,
    message: str,
    *,
    hint: str = "",
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造失败 JSON（不抛出）。"""
    return BgError(code, message, hint=hint, detail=detail).to_dict()


def ensure(
    condition: bool,
    code: str,
    message: str,
    *,
    hint: str = "",
    detail: Mapping[str, Any] | None = None,
) -> None:
    """条件不满足时抛出 BgError，减少 if-raise 样板。"""
    if not condition:
        raise BgError(code, message, hint=hint, detail=detail)


# ---------------------------------------------------------------------------
# CLI 基础设施
# ---------------------------------------------------------------------------
class CliParser(argparse.ArgumentParser):
    """参数错误改为抛 BgError，保证 --json 模式下也能输出结构化失败 JSON。"""

    def error(self, message: str) -> None:  # pragma: no cover - argparse 回调
        raise BgError(E_USAGE, message, hint="使用 --help 查看命令用法")


def add_common_args(parser: argparse.ArgumentParser, *, subparser: bool = False) -> None:
    """注册公共参数：--json / --timeout（PRD H.0）。

    ``subparser=True`` 时默认值用 SUPPRESS，避免子命令解析器覆盖父解析器已设置的值。
    """
    default_json: Any = argparse.SUPPRESS if subparser else False
    parser.add_argument(
        "--json",
        action="store_true",
        default=default_json,
        help="stdout 只输出单个 JSON 对象（人类日志走 stderr）",
    )
    default_timeout: Any = argparse.SUPPRESS if subparser else None
    parser.add_argument(
        "--timeout",
        type=float,
        default=default_timeout,
        metavar="SEC",
        help="操作超时秒数（M1 用于文件锁等待上限，默认 15s）",
    )


def timeout_value(args: argparse.Namespace, default: float = 15.0) -> float:
    """从参数中取 --timeout，限制在 (0, 15] 秒。"""
    raw = getattr(args, "timeout", None)
    if raw is None:
        return default
    return max(0.1, min(float(raw), default))


def _human_text(payload: Mapping[str, Any]) -> str:
    """非 --json 模式下的人类可读摘要。"""
    if payload.get("ok"):
        command = str(payload.get("command") or "")
        if not command and isinstance(payload.get("selftest"), Mapping):
            command = str(payload["selftest"].get("script") or "selftest")
        parts = [f"[OK] {command}".strip()]
        changed = payload.get("changed")
        if isinstance(changed, list) and changed:
            parts.append(f"changed={len(changed)}")
        bidder_id = payload.get("bidderId")
        if bidder_id:
            parts.append(f"bidderId={bidder_id}")
        if payload.get("alreadyExists"):
            parts.append("alreadyExists=true")
        return " ".join(parts)
    parts = [f"[FAIL] {payload.get('error', E_UNEXPECTED)}: {payload.get('message', '')}"]
    if payload.get("hint"):
        parts.append(f"hint: {payload['hint']}")
    return " ".join(parts)


def emit(payload: Mapping[str, Any], json_mode: bool) -> None:
    """按模式输出结果：--json 为单个 JSON 对象，否则人类可读一行/段。"""
    if json_mode:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = _human_text(payload)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _os_error_to_bg(exc: OSError) -> BgError:
    """把 OSError 映射为 PRD 8.5 错误码与退出码。"""
    detail = {"reason": exc.strerror or type(exc).__name__}
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        detail["winerror"] = winerror
    if isinstance(exc, FileNotFoundError):
        if exc.filename:
            detail["path"] = str(exc.filename)
        return BgError(
            E_NOT_FOUND,
            "文件或目录不存在",
            hint="确认路径存在；工作区路径必须是 DSH 已添加的绝对路径",
            detail=detail,
        )
    if isinstance(exc, PermissionError):
        if exc.filename:
            detail["path"] = str(exc.filename)
        return BgError(
            E_IO,
            "权限不足或文件正被占用",
            hint="关闭占用该文件的程序，或检查工作区是否可写",
            detail=detail,
        )
    return BgError(
        E_IO,
        "文件系统操作失败",
        hint="检查磁盘空间与工作区写权限后重试",
        detail=detail,
    )


def _log_script_error(
    args: argparse.Namespace | None,
    error: BgError,
    script_name: str,
    raw_argv: Sequence[str],
) -> None:
    """失败时尽力追加 ``script.error`` 事件（PRD 7.9）；日志失败不得影响主错误。"""
    workspace = getattr(args, "workspace", None) if args is not None else None
    if not workspace:
        return
    try:
        ws = Path(str(workspace))
        if not (ws / "dsh" / "logs").is_dir():
            return
        from . import logging as event_log  # 延迟导入，避免循环依赖

        command = str(getattr(args, "command", "") or (raw_argv[0] if raw_argv else ""))
        event_log.log_event(
            ws,
            "script.error",
            actor="script",
            detail={
                "script": script_name,
                "command": command,
                "error": error.code,
                "exitCode": error.exit_code,
                "message": error.message,
                "findings": (error.detail or {}).get("findings") if isinstance(error.detail, Mapping) else None,
                "detail": error.detail,
            },
            result="error",
        )
    except Exception:  # noqa: BLE001 - 审计日志为尽力而为，绝不覆盖原始错误
        pass


def run_cli(
    argv: Sequence[str] | None,
    *,
    parser: argparse.ArgumentParser,
    handler: Callable[[argparse.Namespace], Mapping[str, Any]],
    script_name: str = "",
) -> int:
    """标准 CLI 入口：解析参数 -> handler -> 统一输出 / 错误映射 / 退出码。"""
    raw: list[str] = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(raw)
        json_mode = bool(getattr(args, "json", json_mode))
        result = handler(args)
        if "ok" not in result:
            result = {"ok": True, **result}
        emit(result, json_mode)
        return EXIT_OK if result.get("ok") else EXIT_GENERIC
    except BgError as exc:
        _log_script_error(args, exc, script_name, raw)
        emit(exc.to_dict(), json_mode)
        return exc.exit_code
    except SystemExit:  # argparse --help / --version
        raise
    except KeyboardInterrupt:
        err = BgError(
            E_UNEXPECTED,
            "操作被中断",
            hint="重跑命令；JSON 写入为原子替换，不会留下半截文件",
            exit_code=EXIT_GENERIC,
        )
        emit(err.to_dict(), json_mode)
        return EXIT_GENERIC
    except OSError as exc:
        err = _os_error_to_bg(exc)
        _log_script_error(args, err, script_name, raw)
        emit(err.to_dict(), json_mode)
        return err.exit_code
    except Exception as exc:  # noqa: BLE001 - CLI 兜底，避免 Python traceback 污染 stdout
        traceback.print_exc(file=sys.stderr)
        err = BgError(
            E_UNEXPECTED,
            f"未预期错误：{type(exc).__name__}",
            hint="把 stderr 的 traceback 反馈给开发者；不要重复执行破坏现场",
            detail={"type": type(exc).__name__},
            exit_code=EXIT_GENERIC,
        )
        _log_script_error(args, err, script_name, raw)
        emit(err.to_dict(), json_mode)
        return EXIT_GENERIC
