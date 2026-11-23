"""DSH 运行时外部命令发现（node 等）。

背景（2026-11-23 干净 VM 冒烟）：`bg_render.py` 只查 `BG_NODE` 与 PATH。打包版 DSH
把 node 放在 `<DSH_HOME>/dsh-runtimes/<runtime>/dependencies/node/bin/node.exe`，
不在 PATH，导致 mermaid 静默降级为 Pillow 占位图；env-check 的 browser_renderer
又只查浏览器，出现「环境检查通过、交付全是占位图」的假通过。

本模块统一发现顺序，render 与 env-check 共用：
1. `BG_NODE`（显式覆盖，便于排障/离线包）；
2. PATH 中的 `node`；
3. DSH 运行时：当前 Python 解释器的兄弟目录 `dependencies/node/bin/node[.exe]`；
4. 同目录 `node/node[.exe]` 兼容旧布局。

只做只读探测，不启动 node，不改 PATH。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

NODE_ENV = "BG_NODE"


def _node_binary_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def _is_file(path: Path | None) -> bool:
    return bool(path) and Path(path).is_file()


def candidate_nodes() -> list[tuple[str, Path]]:
    """按优先级返回候选 ``(source, path)``；不检查可执行性，只判断文件存在。"""
    candidates: list[tuple[str, Path]] = []
    env_value = os.environ.get(NODE_ENV)
    if env_value:
        env_path = Path(env_value).expanduser()
        if _is_file(env_path):
            candidates.append((NODE_ENV, env_path))
    which_value = shutil.which("node")
    if which_value:
        which_path = Path(which_value)
        if _is_file(which_path):
            candidates.append(("PATH", which_path))
    try:
        runtime_deps = Path(sys.executable).resolve().parent.parent
    except OSError:  # pragma: no cover - 解释器路径异常时直接跳过运行时探测
        runtime_deps = None
    if runtime_deps is not None:
        node_name = _node_binary_name()
        for relative in (Path("node") / "bin" / node_name, Path("node") / node_name):
            candidate = runtime_deps / relative
            if _is_file(candidate):
                candidates.append((f"DSH runtime:{runtime_deps}", candidate))
    return candidates


def find_node() -> tuple[Path | None, str]:
    """返回 ``(node 路径, 来源)``；找不到时 ``(None, "")``。"""
    candidates = candidate_nodes()
    if not candidates:
        return None, ""
    source, path = candidates[0]
    return path, source
