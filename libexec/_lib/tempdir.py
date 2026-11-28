"""沙箱友好的临时目录（D1，2026-11-28 第三轮冒烟）。

背景：Python 3.12 的 ``tempfile.mkdtemp``（以及 ``TemporaryDirectory``）在 Windows
上会用 ``os.mkdir(path, 0o700)`` 创建**受保护 DACL**（仅 SYSTEM / Administrators /
OWNER RIGHTS），DSH 的 ``workspace-write`` 沙箱以受限令牌（AppContainer）运行子进程时
不在该 DACL 中，导致连自己刚建的目录都写不进去：

    [WinError 5] 拒绝访问: %TEMP%\\dsh-xxxx\\bidgenie-*-selftest-*\\ws

``os.makedirs(path)``（不带 mode）会让目录**继承父目录 ACL**，受限进程可以正常写入。
本模块只做这一件事：生成唯一目录名 + ``os.makedirs``，并提供与 ``TemporaryDirectory``
等价的上下文管理器。所有脚本自检/渲染/转换的临时目录统一走这里。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def mkdtemp(prefix: str = "bidgenie-", dir: str | Path | None = None) -> Path:
    """创建唯一临时目录并返回 Path；不使用 0700 受保护 DACL。

    可用环境变量 ``BIDGENIE_TEMP_DIR`` 指定可写根目录（沙箱/受限环境兜底）。
    """
    base = Path(dir) if dir is not None else Path(os.environ.get("BIDGENIE_TEMP_DIR") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        candidate = base / f"{prefix}{uuid.uuid4().hex[:10]}"
        try:
            os.makedirs(candidate)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"无法创建临时目录：{base / (prefix + '*')}")


@contextmanager
def temporary_directory(prefix: str = "bidgenie-", dir: str | Path | None = None) -> Iterator[Path]:
    """``tempfile.TemporaryDirectory`` 的等价实现（退出时尽力删除）。"""
    path = mkdtemp(prefix=prefix, dir=dir)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
