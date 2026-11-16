"""原子写、旁路文件锁与 JSON 读写重试（PRD 3.2 / 3.7 / 8.2 / 8.6）。

核心保证：
- 写：``.<name>.tmp-<rand4>`` -> flush + fsync -> ``os.replace``；
  对 ``PermissionError / EPERM``（Windows 并发读者）指数退避重试，上限约 2 秒，失败删除 tmp；
- 锁：旁路文件 ``<target>.lock``，Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``，
  超时 15 秒抛 ``E_LOCK_TIMEOUT``（退出码 7）；同进程同线程可重入；
- 读：容忍瞬时占用失败，重试 1–3 次；UTF-8 无 BOM，读取时容忍（剥离）BOM，写出时禁止 BOM。
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import paths as bgpaths
from .errors import (
    E_IO,
    E_LOCK_TIMEOUT,
    E_NOT_FOUND,
    E_SCHEMA_INVALID,
    BgError,
)

#: 文件锁等待上限（PRD 3.7 / 8.6：15 秒）
LOCK_TIMEOUT_SEC = 15.0
#: 原子替换重试总预算（PRD 3.7：上限约 2 秒）
REPLACE_TIMEOUT_SEC = 2.0
#: 读侧重试次数（PRD 3.7：1–3 次）
READ_ATTEMPTS = 3
#: Windows 并发读导致的瞬时错误码：ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
_TRANSIENT_ERRNOS = frozenset(
    {errno.EACCES, errno.EPERM, errno.EBUSY, errno.EAGAIN, getattr(errno, "EDEADLK", -1)}
)


def _is_transient(exc: OSError) -> bool:
    """判断 OSError 是否为并发占用类瞬时错误。"""
    winerror = getattr(exc, "winerror", None)
    if winerror in _TRANSIENT_WINERRORS:
        return True
    return exc.errno in _TRANSIENT_ERRNOS


def _io_error(message: str, target: Path, exc: OSError, *, hint: str) -> BgError:
    detail: dict[str, Any] = {"path": str(target), "reason": exc.strerror or type(exc).__name__}
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        detail["winerror"] = winerror
    return BgError(E_IO, message, hint=hint, detail=detail)


# ---------------------------------------------------------------------------
# 文件锁
# ---------------------------------------------------------------------------
_LOCK_STATE_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
#: key -> (thread_ident, fd, depth)
_FILE_HOLDERS: dict[str, tuple[int, int, int]] = {}


def lock_path_for(target: str | os.PathLike[str]) -> Path:
    """旁路锁文件路径：``<target>.lock``（PRD 3.7）。"""
    path = Path(target)
    return path.with_name(path.name + ".lock")


class FileLock:
    """对目标 JSON 的旁路文件锁（跨进程互斥 + 同线程可重入）。"""

    def __init__(self, target: str | os.PathLike[str], *, timeout: float = LOCK_TIMEOUT_SEC) -> None:
        self.target = Path(target)
        self.lock_path = lock_path_for(self.target)
        self.timeout = max(0.0, float(timeout))
        self._key = os.path.normcase(os.path.abspath(str(self.lock_path)))
        self._thread_lock: threading.RLock | None = None
        self._fd: int | None = None
        self._nested = False

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.release()
        return False

    # -- 获取 / 释放 -------------------------------------------------------
    def acquire(self) -> None:
        """获取锁；超时抛 E_LOCK_TIMEOUT。"""
        deadline = time.monotonic() + self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK_STATE_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(self._key, threading.RLock())
        remaining = max(0.0, deadline - time.monotonic())
        if not thread_lock.acquire(timeout=remaining):
            raise self._timeout_error()
        self._thread_lock = thread_lock
        ident = threading.get_ident()
        with _LOCK_STATE_GUARD:
            holder = _FILE_HOLDERS.get(self._key)
            if holder is not None and holder[0] == ident:
                _FILE_HOLDERS[self._key] = (ident, holder[1], holder[2] + 1)
                self._nested = True
                return
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        try:
            self._lock_fd(fd, deadline)
        except BaseException:
            os.close(fd)
            self._thread_lock = None
            thread_lock.release()
            raise
        with _LOCK_STATE_GUARD:
            _FILE_HOLDERS[self._key] = (ident, fd, 1)
        self._fd = fd

    def release(self) -> None:
        """释放锁（嵌套获取时仅减少重入计数）。"""
        thread_lock = self._thread_lock
        if thread_lock is None:
            return
        try:
            if self._nested:
                with _LOCK_STATE_GUARD:
                    holder = _FILE_HOLDERS.get(self._key)
                    if holder is not None:
                        _FILE_HOLDERS[self._key] = (holder[0], holder[1], holder[2] - 1)
            else:
                with _LOCK_STATE_GUARD:
                    _FILE_HOLDERS.pop(self._key, None)
                fd = self._fd
                if fd is not None:
                    try:
                        self._unlock_fd(fd)
                    finally:
                        os.close(fd)
        finally:
            self._fd = None
            self._nested = False
            self._thread_lock = None
            thread_lock.release()

    # -- 平台加锁实现 ------------------------------------------------------
    def _timeout_error(self) -> BgError:
        return BgError(
            E_LOCK_TIMEOUT,
            f"获取文件锁超时（{self.timeout:.0f}s）：{self.target.name}",
            hint="另一个操作正在进行，请稍后重试；不要并发编辑同一文件",
            detail={"lock": str(self.lock_path)},
        )

    def _lock_fd(self, fd: int, deadline: float) -> None:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise self._timeout_error() from exc
                    time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise self._timeout_error() from exc
                    time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))

    def _unlock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _read_bytes_with_retry(target: Path, attempts: int) -> bytes:
    """读文件字节，容忍 Windows 并发占用导致的瞬时失败。"""
    last_exc: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise BgError(
                E_NOT_FOUND,
                f"文件不存在：{target.name}",
                hint="确认路径正确；工作区可能尚未初始化",
                detail={"path": str(target)},
            ) from exc
        except OSError as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt >= attempts - 1:
                raise _io_error(
                    f"读取文件失败：{target.name}",
                    target,
                    exc,
                    hint="关闭占用该文件的程序后重试",
                ) from exc
            time.sleep(0.05 * (attempt + 1))
    raise _io_error("读取文件失败", target, last_exc or OSError("unknown"), hint="稍后重试")


def read_json(
    target: str | os.PathLike[str],
    *,
    attempts: int = READ_ATTEMPTS,
    error_code: str = E_SCHEMA_INVALID,
    missing_ok: bool = False,
    default: Any = None,
) -> Any:
    """读取 UTF-8 无 BOM JSON；瞬时 IO 失败重试；解析失败抛指定错误码。"""
    path = Path(target)
    try:
        raw = _read_bytes_with_retry(path, attempts)
    except BgError as exc:
        if exc.code == E_NOT_FOUND and missing_ok:
            return default
        raise
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BgError(
            error_code,
            f"{path.name} 不是合法 UTF-8（禁止 GBK/ANSI）",
            hint="以 UTF-8 无 BOM 重新保存该文件",
            detail={"path": str(path)},
        ) from exc
    if not text.strip():
        raise BgError(
            error_code,
            f"{path.name} 为空文件",
            hint="从 dsh/backups/ 或 *.bak 恢复，或重新生成",
            detail={"path": str(path)},
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BgError(
            error_code,
            f"{path.name} 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）",
            hint="从 dsh/backups/ 或 *.bak 恢复，或重新生成该文件",
            detail={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def read_json_optional(target: str | os.PathLike[str], **kwargs: Any) -> Any:
    """读取可缺省 JSON：文件不存在返回 None。"""
    return read_json(target, missing_ok=True, default=None, **kwargs)


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - 防御性分支
            raise OSError(errno.EIO, "os.write 返回 0")
        view = view[written:]


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """``os.replace``，对 PermissionError/EPERM 指数退避重试 ≤ 约 2 秒（PRD 3.7）。"""
    deadline = time.monotonic() + REPLACE_TIMEOUT_SEC
    delay = 0.02
    attempts = 0
    while True:
        attempts += 1
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if not _is_transient(exc) or time.monotonic() >= deadline:
                raise _io_error(
                    f"原子替换失败：{target.name}",
                    target,
                    exc,
                    hint="关闭占用该文件的程序（如编辑器/预览）后重试",
                ) from exc
            time.sleep(min(delay, max(0.005, deadline - time.monotonic())))
            delay = min(delay * 2, 0.25)


def _write_text_unlocked(target: Path, text: str) -> None:
    """在 target 同目录写临时文件并原子替换（不加文件锁）。"""
    data = text.encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        raise BgError(E_SCHEMA_INVALID, "写入内容不允许带 UTF-8 BOM", detail={"path": str(target)})
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{bgpaths.rand4()}")
    fd: int | None = None
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _replace_with_retry(tmp, target)
    except BgError:
        _safe_unlink(tmp)
        raise
    except OSError as exc:
        _safe_unlink(tmp)
        raise _io_error(
            f"写入文件失败：{target.name}",
            target,
            exc,
            hint="检查磁盘空间与工作区写权限后重试",
        ) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_text_atomic(
    target: str | os.PathLike[str],
    text: str,
    *,
    lock: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SEC,
) -> None:
    """原子写文本文件（UTF-8 无 BOM，调用方负责 ``\\n`` 换行）。"""
    path = Path(target)
    if lock:
        with FileLock(path, timeout=lock_timeout):
            _write_text_unlocked(path, text)
    else:
        _write_text_unlocked(path, text)


def write_json_atomic(
    target: str | os.PathLike[str],
    payload: Any,
    *,
    lock: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SEC,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """原子写 JSON（2 空格缩进、UTF-8 无 BOM、末尾 LF、键顺序即传入顺序）。"""
    text = json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent) + "\n"
    write_text_atomic(target, text, lock=lock, lock_timeout=lock_timeout)


def append_line(target: str | os.PathLike[str], line: str) -> None:
    """追加一行文本（日志用；单次 ``os.write``，UTF-8、``\\n`` 结尾）。"""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line if line.endswith("\n") else line + "\n").encode("utf-8")
    fd: int | None = None
    try:
        fd = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o666
        )
        _write_all(fd, data)
    except OSError as exc:
        raise _io_error(
            f"追加日志失败：{path.name}", path, exc, hint="检查 dsh/logs/ 写权限与磁盘空间"
        ) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def iter_lines(target: str | os.PathLike[str]) -> Iterator[str]:
    """逐行读取文本（UTF-8，剥离 BOM）；文件不存在返回空迭代。"""
    path = Path(target)
    if not path.is_file():
        return
    raw = _read_bytes_with_retry(path, READ_ATTEMPTS)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            yield line
