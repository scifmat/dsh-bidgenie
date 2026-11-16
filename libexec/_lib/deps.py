"""插件依赖目录（pylibs）的 sys.path / DLL 配置。

搜索顺序（task-156）：
1. ``BIDGENIE_PYLIBS`` 环境变量（用户/运维显式指定）；
2. ``<DSH_HOME>/data/bidgenie/pylibs``（推荐：用户数据目录，不受 Git/发布包影响）；
3. ``<plugin>/pylibs``（旧发布包兜底；GitHub 仓库已不再跟踪）。

约定：
- 只读环境、幂等，不下载/安装任何东西；
- pywin32 的 DLL（``pywin32_system32``）与含 `.pyd`/`.dll` 的子目录通过 ``os.add_dll_directory`` 加入；
- 诊断输出实际目录、来源、候选目录与 key imports，便于 env-check rootCause。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: ``_lib/deps.py`` -> parents[0]=_lib, [1]=libexec, [2]=plugin
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PYLIBS_DIRNAME = "pylibs"

_PYLIBS_SUBPATHS: tuple[str, ...] = (
    "",  # pylibs 自身
    "win32",
    "win32/lib",
    "pythonwin",
)


def dsh_home() -> Path:
    """DSH 用户目录：``DSH_HOME`` 或 ``~/.dsh``。"""
    raw = os.environ.get("DSH_HOME")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser().resolve()
    return (Path.home() / ".dsh").resolve()


def data_pylibs_dir() -> Path:
    """推荐依赖目录：``<DSH_HOME>/data/bidgenie/pylibs``。"""
    return dsh_home() / "data" / "bidgenie" / PYLIBS_DIRNAME


def plugin_pylibs_dir() -> Path:
    """插件包内兜底目录：``<plugin>/pylibs``。"""
    return PLUGIN_ROOT / PYLIBS_DIRNAME


def candidate_pylibs_dirs() -> list[dict[str, str]]:
    """按优先级返回候选目录与来源（不保证存在）。"""
    candidates: list[dict[str, str]] = []
    override = os.environ.get("BIDGENIE_PYLIBS")
    if isinstance(override, str) and override.strip():
        candidates.append({"source": "env:BIDGENIE_PYLIBS", "path": str(Path(override.strip()).expanduser().resolve())})
    candidates.append({"source": "data", "path": str(data_pylibs_dir())})
    candidates.append({"source": "plugin", "path": str(plugin_pylibs_dir())})
    return candidates


def pylibs_dir() -> Path:
    """旧 API：返回插件内 pylibs 路径（保留兼容）。"""
    return plugin_pylibs_dir()


def active_pylibs() -> dict[str, Any]:
    """返回实际使用的 pylibs 目录/来源（优先已存在目录；env 显式目录也保留）。"""
    candidates = candidate_pylibs_dirs()
    for candidate in candidates:
        path = Path(candidate["path"])
        if path.is_dir():
            return {"path": str(path), "source": candidate["source"], "exists": True, "candidates": candidates}
    return {"path": "", "source": "missing", "exists": False, "candidates": candidates}


def _remove_existing(entry: str) -> None:
    while entry in sys.path:
        sys.path.remove(entry)


def _add_dll_dir(directory: Path, dll_dirs: list[str]) -> None:
    text = str(directory)
    add_dll = getattr(os, "add_dll_directory", None)
    if add_dll is not None:
        try:
            add_dll(text)
        except OSError:
            pass
    if text not in dll_dirs:
        dll_dirs.append(text)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if text not in path_entries:
        os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")


def ensure_dependency_path(*, prepend: bool = True, base: str | Path | None = None) -> dict[str, Any]:
    """把生效的 pylibs（及 pywin32 子目录/DLL 目录）加入当前进程；幂等。"""
    active = active_pylibs()
    selected = Path(base) if base is not None else Path(active["path"]) if active["path"] else None
    if selected is None or not selected.is_dir():
        return {"pylibs": str(selected) if selected else "", "source": active["source"], "exists": False, "added": [], "dllDirs": [], "candidates": active["candidates"]}
    added: list[str] = []
    for relative in _PYLIBS_SUBPATHS:
        directory = selected / relative if relative else selected
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
        _add_dll_dir(selected, dll_dirs)
        system32 = selected / "pywin32_system32"
        if system32.is_dir():
            _add_dll_dir(system32, dll_dirs)
        for child in sorted(selected.iterdir()):
            if child.is_dir() and any(item.suffix.lower() in (".pyd", ".dll") for item in child.iterdir() if item.is_file()):
                _add_dll_dir(child, dll_dirs)
    return {"pylibs": str(selected), "source": active["source"], "exists": True, "added": added, "dllDirs": dll_dirs, "candidates": active["candidates"]}


def binary_integrity() -> dict[str, Any]:
    """检查生效目录的 .pyd/.so 数量与 RECORD 中声明的二进制是否缺失。"""
    active = active_pylibs()
    base = Path(active["path"]) if active["path"] else None
    result: dict[str, Any] = {"pylibs": str(base) if base else "", "source": active["source"], "binaryCount": 0, "declared": 0, "missing": [], "ok": False}
    if base is None or not base.is_dir():
        result["reason"] = "pylibs 目录不存在"
        return result
    binaries = [path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in (".pyd", ".so")]
    result["binaryCount"] = len(binaries)
    declared: list[str] = []
    for record in base.glob("*.dist-info/RECORD"):
        try:
            for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
                rel = line.split(",", 1)[0]
                if rel.lower().endswith((".pyd", ".so")):
                    declared.append(rel)
        except OSError:
            continue
    result["declared"] = len(declared)
    missing = [rel for rel in declared if not (base / rel).is_file()]
    result["missing"] = missing[:50]
    result["ok"] = bool(binaries) and not missing
    if not binaries:
        result["reason"] = "pylibs 内没有任何 .pyd/.so（GitHub 发布包可能因 .gitignore *.py[cod] 误伤 .pyd）"
    elif missing:
        result["reason"] = f"RECORD 声明 {len(missing)} 个二进制缺失"
    return result


def dependency_report() -> dict[str, Any]:
    """诊断用：实际目录/来源、候选、sys.path、关键依赖深导入与二进制完整性。"""
    active = active_pylibs()
    entries = []
    if active["path"]:
        base = Path(active["path"])
        entries = [str(base)] + [str(base / relative) for relative in _PYLIBS_SUBPATHS[1:]]
    on_path = [entry for entry in entries if entry in sys.path]
    imports: dict[str, Any] = {}
    probes = {
        "PIL.Image": "PIL",
        "lxml.etree": "lxml",
        "docx.api": "docx",
        "fitz": None,
        "pymupdf4llm": None,
        "pdfplumber": None,
    }
    for module_name in probes:
        try:
            __import__(module_name)
            imports[module_name] = {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - 诊断只记录结果
            imports[module_name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "module": str(getattr(exc, "name", "") or "")}
    return {
        "pylibs": active["path"],
        "source": active["source"],
        "exists": active["exists"],
        "candidates": active["candidates"],
        "sysPathEntries": on_path,
        "imports": imports,
        "binaryIntegrity": binary_integrity(),
    }
