"""插件自有依赖目录（``plugin/pylibs``）的 sys.path / DLL 配置（PRD 附录 B.3）。

约定（PRD B.3）：
- 第三方依赖装到插件自有目录 ``plugin/pylibs``，**不修改 DSH 自带 runtime 的 site-packages**；
- 脚本入口调用 :func:`ensure_dependency_path` 后即可 ``import fitz`` / ``import pdfplumber`` / ``import win32com.client``；
- pywin32 的 DLL（``pywin32_system32``）在 Windows 上需要 ``os.add_dll_directory`` 才能被加载。

本模块只读环境、幂等；不下载/安装任何东西（安装由 ``pip --target`` 人工/构建步骤完成）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: ``_lib/deps.py`` -> parents[0]=_lib, [1]=libexec, [2]=plugin
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
#: 插件自有依赖目录名（相对插件根）
PYLIBS_DIRNAME = "pylibs"

#: 需要加入 sys.path 的子目录（pywin32 的 win32 / win32/lib / pythonwin；顺序保留）
_PYLIBS_SUBPATHS: tuple[str, ...] = (
    "",  # pylibs 自身（PDF 库、pymupdf4llm、pdfplumber 等）
    "win32",
    "win32/lib",
    "pythonwin",
)


def pylibs_dir() -> Path:
    """返回 ``plugin/pylibs`` 绝对路径（可能不存在）。"""
    return PLUGIN_ROOT / PYLIBS_DIRNAME


def _remove_existing(entry: str) -> None:
    """移除 sys.path 中相同路径的旧条目，保证顺序稳定。"""
    while entry in sys.path:
        sys.path.remove(entry)


def ensure_dependency_path(*, prepend: bool = True) -> dict[str, Any]:
    """把 ``plugin/pylibs``（及 pywin32 子目录/DLL 目录）加入当前进程；幂等。

    Returns:
        ``{"pylibs": abs, "exists": bool, "added": [相对目录], "dllDirs": [abs]|None}``
        便于脚本在 ``--json`` 输出/诊断中附带依赖路径信息。
    """
    base = pylibs_dir()
    added: list[str] = []
    existed = base.is_dir()
    if existed:
        for relative in _PYLIBS_SUBPATHS:
            directory = base / relative if relative else base
            if not directory.is_dir():
                continue
            text = str(directory)
            if text in sys.path:
                continue
            _remove_existing(text)
            if prepend:
                sys.path.insert(0, text)
            else:
                sys.path.append(text)
            added.append(relative or ".")
    dll_dirs: list[str] = []
    if os.name == "nt":
        system32 = base / "pywin32_system32"
        if system32.is_dir():
            text = str(system32)
            add_dll = getattr(os, "add_dll_directory", None)
            if add_dll is not None:
                try:
                    add_dll(text)
                    dll_dirs.append(text)
                except OSError:
                    pass
            # 兼容老式依赖 PATH 的加载路径（子进程亦可继承）
            path_entries = os.environ.get("PATH", "").split(os.pathsep)
            if text not in path_entries:
                os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")
    return {"pylibs": str(base), "exists": existed, "added": added, "dllDirs": dll_dirs}


def dependency_report() -> dict[str, Any]:
    """诊断用：pylibs 是否存在、已加入 sys.path 的条目、关键依赖是否可导入（不抛异常）。"""
    base = pylibs_dir()
    entries = [str(base)] + [str(base / relative) for relative in _PYLIBS_SUBPATHS[1:]]
    on_path = [entry for entry in entries if entry in sys.path]
    imports: dict[str, str] = {}
    for module_name in ("fitz", "pymupdf", "pymupdf4llm", "pdfplumber", "win32com.client"):
        try:
            __import__(module_name)
            imports[module_name] = "ok"
        except Exception as exc:  # noqa: BLE001 - 诊断只记录结果
            imports[module_name] = f"{type(exc).__name__}"
    return {
        "pylibs": str(base),
        "exists": base.is_dir(),
        "sysPathEntries": on_path,
        "imports": imports,
    }
