#!/usr/bin/env python3
"""bg_env_check.py：M2 环境自检（PRD 附录 D / F2.2 / 8.4 / CONTRACT §8.2）。

用法：
    python bg_env_check.py --report --workspace <abs> [--json]     # 只读，读 env-check.json
    python bg_env_check.py --apply  --workspace <abs> [--item <id> ...] [--json]
    python bg_env_check.py --selftest [--json]

规则：
- 基线唯一来源 ``libexec/checklist.json``（15 项 / 必需 8，PRD 附录 D）；
- 版本化跳过（PRD 8.4）：``checklistVersion`` + ``itemVersion`` + ``status=passed`` → 跳过；
- ``--item`` 指定检查项时强制重跑该项（绕过跳过规则，M2-02「只重跑该项」）；
- 每项单项预算 120s、整体 10 分钟（PRD 8.1）；超时项记 ``timeout`` 并视为失败；
- **不自动安装任何依赖**：缺失项只给 remediation 指引；
- 结果原子写 ``dsh/config/env-check.json``（items 为 object map，CONTRACT §8.2）；
- ``--report`` 严格只读：不建目录、不写文件、不写事件日志；
- 退出码：0 检查完成（失败项体现在报告里）；3 workspace 不存在；4 checklist/env-check 损坏；8 checklist 缺失。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import deps, errors, jsonio, logging as event_log, paths, runtime as runtime_env, tempdir  # noqa: E402

CHECKLIST_PATH = _SCRIPT_DIR / "checklist.json"
ENV_CHECK_SCHEMA_VERSION = "1.0"
ENV_CHECK_FILE = "env-check.json"

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"
STATUS_WAIVED = "waived"
STATUS_NOT_RUN = "not_run"
ALL_STATUSES = (STATUS_PASSED, STATUS_FAILED, STATUS_TIMEOUT, STATUS_SKIPPED, STATUS_WAIVED, STATUS_NOT_RUN)
DEGRADED_STATUSES = (STATUS_FAILED, STATUS_TIMEOUT)

PER_ITEM_TIMEOUT_SEC = 120.0
OVERALL_TIMEOUT_SEC = 600.0
SCRIPT_SELFTEST_TIMEOUT_SEC = 60.0

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: D1（第三轮冒烟）：沙箱/文件权限被拒的典型报错（Windows AppContainer + 0700 DACL）。
_PERMISSION_ERROR_RE = re.compile(r"WinError 5|拒绝访问|Access is denied|PermissionError|Permission denied", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 基线加载与校验
# ---------------------------------------------------------------------------
def validate_checklist(document: Any) -> list[str]:
    """校验 checklist.json 结构，返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    if not isinstance(document, Mapping):
        return ["checklist.json 顶层必须是 JSON 对象"]
    if not isinstance(document.get("schemaVersion"), str) or not document.get("schemaVersion"):
        problems.append("缺少 schemaVersion")
    if not isinstance(document.get("checklistVersion"), str) or not document.get("checklistVersion"):
        problems.append("缺少 checklistVersion")
    items = document.get("items")
    if not isinstance(items, list):
        return problems + ["items 必须是数组"]
    seen: set[str] = set()
    for index, item in enumerate(items):
        pointer = f"/items/{index}"
        if not isinstance(item, Mapping):
            problems.append(f"{pointer} 必须是对象")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            problems.append(f"{pointer}/id 必须是非空字符串")
        elif item_id in seen:
            problems.append(f"{pointer}/id 重复：{item_id}")
        else:
            seen.add(item_id)
        for key in ("label", "description", "itemVersion", "check", "remediation"):
            value = item.get(key)
            if not isinstance(value, str) or (key != "check" and not value.strip()):
                problems.append(f"{pointer}/{key} 必须是非空字符串")
        if not isinstance(item.get("required"), bool):
            problems.append(f"{pointer}/required 必须是布尔值")
        if "degradeImpact" in item and not isinstance(item["degradeImpact"], str):
            problems.append(f"{pointer}/degradeImpact 必须是字符串")
        if "deferredUntil" in item and not isinstance(item["deferredUntil"], str):
            problems.append(f"{pointer}/deferredUntil 必须是字符串")
        if "config" in item and not isinstance(item["config"], Mapping):
            problems.append(f"{pointer}/config 必须是对象")
    return problems


def load_checklist(path: Path = CHECKLIST_PATH) -> dict[str, Any]:
    """加载并校验检查清单基线（插件包内只读文件）。"""
    if not path.is_file():
        raise errors.BgError(
            errors.E_DEP_MISSING,
            "缺少环境检查基线 libexec/checklist.json",
            hint="插件包不完整，请重新安装/更新插件",
            detail={"path": "libexec/checklist.json"},
            exit_code=errors.EXIT_DEP,
        )
    document = jsonio.read_json(path)
    problems = validate_checklist(document)
    if problems:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "libexec/checklist.json 校验失败",
            hint="插件包损坏，请重新安装/更新插件",
            detail={"file": "libexec/checklist.json", "problems": problems},
        )
    return dict(document)


def checklist_items(checklist: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按基线顺序返回 item 列表。"""
    return [dict(item) for item in checklist.get("items") or [] if isinstance(item, Mapping)]


def find_item(checklist: Mapping[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in checklist_items(checklist):
        if item.get("id") == item_id:
            return item
    return None


def required_total(checklist: Mapping[str, Any]) -> int:
    return sum(1 for item in checklist_items(checklist) if item.get("required") is True)


# ---------------------------------------------------------------------------
# env-check.json 读写
# ---------------------------------------------------------------------------
def env_check_path(workspace: Path) -> Path:
    return paths.config_dir(workspace) / ENV_CHECK_FILE


def read_env_check(workspace: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    """读取 env-check.json；结构非法时抛 E_SCHEMA_INVALID（不静默修复，PRD 8.2）。"""
    path = env_check_path(workspace)
    if not path.is_file():
        if missing_ok:
            return None
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "尚未运行环境检查（缺少 dsh/config/env-check.json）",
            hint="先运行 python libexec/bg_env_check.py --apply --workspace <ws> --json",
            detail={"path": "dsh/config/env-check.json"},
        )
    document = jsonio.read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("items"), Mapping):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "dsh/config/env-check.json 结构非法",
            hint="删除该文件后重跑 --apply（或从 dsh/backups/ 恢复）",
            detail={"path": "dsh/config/env-check.json"},
        )
    return dict(document)


# ---------------------------------------------------------------------------
# 检查函数（15 项，id 与 PRD 附录 D 一致）
# ---------------------------------------------------------------------------
def _result(status: str, evidence: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"status": status, "evidence": evidence, "detail": dict(detail or {})}


def _module_version(module_name: str) -> str:
    module = importlib.import_module(module_name)
    version = getattr(module, "__version__", "")
    return str(version or "")


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(str(text or "").strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _word_exe_candidates() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
        Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE"),
        Path(r"C:\Program Files\Microsoft Office\Office16\WINWORD.EXE"),
        Path(r"C:\Program Files (x86)\Microsoft Office\Office16\WINWORD.EXE"),
    ]
    found = shutil.which("WINWORD.EXE") or shutil.which("winword")
    if found:
        candidates.insert(0, Path(found))
    return candidates


def _find_word_exe() -> Path | None:
    for candidate in _word_exe_candidates():
        if candidate.is_file():
            return candidate
    return None


def _find_soffice() -> Path | None:
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    candidates = [Path(found)] if found else []
    candidates.extend(
        [
            Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
            Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
            Path("/usr/bin/soffice"),
            Path("/usr/local/bin/soffice"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _find_browser(checklist_item: Mapping[str, Any]) -> Path | None:
    config = checklist_item.get("config") if isinstance(checklist_item.get("config"), Mapping) else {}
    candidates = [Path(str(entry)) for entry in config.get("browsers") or []]
    for name in ("chrome", "google-chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.insert(0, Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def check_python_runtime(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """python_runtime：sys.version_info ≥ 3.10。"""
    config = ctx["item"].get("config") or {}
    minimum = tuple(config.get("minVersion") or [3, 10])
    version = platform.python_version()
    ok = tuple(sys.version_info[: len(minimum)]) >= tuple(minimum)
    return _result(
        STATUS_PASSED if ok else STATUS_FAILED,
        f"Python {version}（{sys.executable}）",
        {"version": version, "executable": sys.executable, "minVersion": ".".join(map(str, minimum))},
    )


def check_python_packages(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """python_packages：核心库 import + 深导入 + 版本比较（python-docx / Pillow / lxml）。"""
    deps.ensure_dependency_path()
    config = ctx["item"].get("config") or {}
    found: dict[str, str] = {}
    missing: list[str] = []
    mismatched: list[str] = []
    errors: dict[str, dict[str, str]] = {}
    module_details: dict[str, dict[str, str]] = {}
    deep_imports = {"docx": "docx.api", "PIL": "PIL.Image", "lxml": "lxml.etree"}
    deep_checked: dict[str, str] = {}
    active = deps.active_pylibs()
    for module in config.get("modules") or []:
        if not isinstance(module, Mapping):
            continue
        import_name = str(module.get("import") or "")
        display = str(module.get("name") or import_name)
        expected = str(module.get("version") or "")
        policy = str(module.get("versionPolicy") or "min")
        try:
            imported = importlib.import_module(import_name)
            installed = str(getattr(imported, "__version__", "") or "")
        except Exception as exc:  # noqa: BLE001 - 任意导入失败都视为缺失
            missing.append(f"{display}（import {import_name}）")
            errors[import_name] = {"type": type(exc).__name__, "message": str(exc), "module": str(getattr(exc, "name", "") or import_name)}
            module_details[display] = {"import": import_name, "expected": expected, "policy": policy, "installed": "", "path": "", "source": "missing"}
            continue
        module_file = str(getattr(imported, "__file__", "") or "")
        module_source = "data-pylibs" if active["path"] and module_file.startswith(active["path"]) else "system"
        module_details[display] = {"import": import_name, "expected": expected, "policy": policy, "installed": installed, "path": module_file, "source": module_source}
        deep_name = deep_imports.get(import_name)
        if deep_name:
            try:
                importlib.import_module(deep_name)
                deep_checked[import_name] = "ok"
            except Exception as exc:  # noqa: BLE001 - 深导入失败（如 _extra 缺失）
                deep_checked[import_name] = f"{type(exc).__name__}: {exc}"
                errors[import_name] = {"type": type(exc).__name__, "message": str(exc), "module": str(getattr(exc, "name", "") or deep_name), "deepImport": deep_name}
        found[display] = installed
        if expected and installed:
            parsed_installed = _parse_version(installed)
            parsed_expected = _parse_version(expected)
            if parsed_installed is not None and parsed_expected is not None:
                if policy == "exact" and parsed_installed != parsed_expected:
                    mismatched.append(f"{display} 需要 exact {expected}，实际 {installed}")
                elif policy != "exact" and parsed_installed < parsed_expected:
                    mismatched.append(f"{display} 需要 >= {expected}，实际 {installed}")
    problems = missing + mismatched + [f"{key} 深导入失败" for key, value in deep_checked.items() if value != "ok"]
    if problems:
        hint = config.get("installHint")
        detail: dict[str, Any] = {"found": found, "missing": missing, "mismatched": mismatched, "errors": errors, "deepImports": deep_checked, "modules": module_details, "pylibs": active["path"], "pylibsSource": active["source"]}
        if hint:
            detail["installHint"] = hint
        return _result(STATUS_FAILED, "；".join(problems), detail)
    return _result(
        STATUS_PASSED,
        " / ".join(f"{name} {version}" for name, version in found.items()) + "（深导入 ok）",
        {"found": found, "missing": [], "mismatched": [], "errors": errors, "deepImports": deep_checked, "modules": module_details, "pylibs": active["path"], "pylibsSource": active["source"]},
    )


def _distribution_version(*candidates: str) -> str:
    """按 pip 发行名读取已安装版本（取第一个找到的）；找不到返回空串。"""
    for name in candidates:
        try:
            from importlib import metadata

            return str(metadata.version(name))
        except Exception:  # noqa: BLE001 - 诊断用途，任何失败都继续尝试下一个名字
            continue
    return ""


def check_pdf_libraries(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """pdf_libraries：PyMuPDF / pymupdf4llm / pdfplumber 可导入（版本取 pip 元数据）。"""
    config = ctx["item"].get("config") or {}
    modules = [str(name) for name in config.get("modules") or []]
    primary = str(config.get("primary") or (modules[0] if modules else "fitz"))
    distribution_names = {
        "fitz": ("PyMuPDF",),
        "pymupdf": ("PyMuPDF",),
        "pymupdf4llm": ("pymupdf4llm",),
        "pdfplumber": ("pdfplumber",),
    }
    found: list[str] = []
    missing: list[str] = []
    versions: dict[str, str] = {}
    errors: dict[str, dict[str, str]] = {}
    deps.ensure_dependency_path()
    binary = deps.binary_integrity()
    for name in modules:
        try:
            _module_version(name)
            found.append(name)
            version = _distribution_version(*distribution_names.get(name, (name,)))
            if version:
                versions[name] = version
        except Exception as exc:  # noqa: BLE001 - 导入失败即缺失，保留异常详情
            missing.append(name)
            errors[name] = {"type": type(exc).__name__, "message": str(exc), "module": str(getattr(exc, "name", "") or name)}
    active = deps.active_pylibs()
    if primary in found:
        version_text = " / ".join(
            f"{name} {versions[name]}" if name in versions else name for name in found
        )
        return _result(
            STATUS_PASSED,
            f"可导入：{version_text}",
            {"found": found, "missing": missing, "versions": versions, "errors": errors, "pylibs": active["path"], "pylibsSource": active["source"], "binaryIntegrity": binary},
        )
    return _result(
        STATUS_FAILED,
        f"缺少 PDF 读取组件：{', '.join(missing) or primary}",
        {"found": found, "missing": missing, "versions": versions, "errors": errors, "pylibs": active["path"], "pylibsSource": active["source"], "binaryIntegrity": binary, "installHint": config.get("installHint", "")},
    )


def check_word_com(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """word_com：Windows 下 pywin32 可导入 + 本机存在 Word（不启动 Word，避免副作用）。"""
    if os.name != "nt":
        return _result(STATUS_SKIPPED, "非 Windows 系统，Word COM 不适用", {"reason": "non_windows"})
    config = ctx["item"].get("config") or {}
    modules = [str(name) for name in config.get("modules") or ["win32com.client"]]
    missing: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 导入失败即缺失
            missing.append(name)
    word_exe = _find_word_exe()
    pywin32_version = _distribution_version("pywin32")
    if missing:
        return _result(
            STATUS_FAILED,
            f"缺少 pywin32（import {', '.join(missing)}）",
            {"missingModules": missing, "wordExe": str(word_exe) if word_exe else "", "pywin32": pywin32_version},
        )
    if word_exe is None:
        return _result(STATUS_FAILED, "未检测到本机 Word（WINWORD.EXE）", {"missingModules": [], "pywin32": pywin32_version})
    return _result(
        STATUS_PASSED,
        f"pywin32 {pywin32_version or '(版本未知)'} 可导入；Word：{word_exe}（未启动 Word 进程）",
        {"wordExe": str(word_exe), "launched": False, "pywin32": pywin32_version},
    )


def _scan_bom(path: Path) -> bool:
    try:
        return path.read_bytes().startswith(b"\xef\xbb\xbf")
    except OSError:
        return False


def _extract_last_json(text: str) -> Any:
    """容错提取 stdout 中最后一个完整 JSON 对象（第三方 import 可能打印横幅）。"""
    decoder = json.JSONDecoder()
    last: Any = None
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            last = value
    return last


def check_scripts_integrity(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """scripts_integrity：动态发现 libexec/bg_*.py + 编译/自检 + BOM 扫描（新增脚本自动纳入）。"""
    config = ctx["item"].get("config") or {}
    scripts = sorted(path.name for path in _SCRIPT_DIR.glob("bg_*.py") if path.is_file())
    plugin_hash = _plugin_scripts_hash()
    configured = [str(name) for name in config.get("scripts") or []]
    missing_configured = [name for name in configured if name not in scripts]
    run_selftest = bool(config.get("runSelftest", True))
    selftest_timeout = float(config.get("selftestTimeoutSec") or SCRIPT_SELFTEST_TIMEOUT_SEC)
    checked: list[str] = []
    problems: list[str] = []
    problem_details: list[dict[str, Any]] = []
    skipped_no_selftest: list[str] = []
    for name in scripts:
        script_path = _SCRIPT_DIR / name
        if _scan_bom(script_path):
            problems.append(f"{name} 含 UTF-8 BOM（应无 BOM）")
            continue
        try:
            source = script_path.read_text(encoding="utf-8")
            compile(source, str(script_path), "exec")
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{name} 语法/编码错误：{type(exc).__name__}")
            continue
        if run_selftest and "--selftest" in source:
            command = [sys.executable, "-X", "utf8", str(script_path), "--selftest", "--json"]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=selftest_timeout,
                    cwd=str(_PLUGIN_ROOT),
                    env={**os.environ, "BIDGENIE_SKIP_ENV_REFRESH": "1"},
                )
            except subprocess.TimeoutExpired:
                problems.append(f"{name} --selftest 超时（{selftest_timeout:.0f}s）")
                continue
            payload: Any = None
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                payload = _extract_last_json(completed.stdout)
            if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
                problems.append(f"{name} --selftest 未通过（exit={completed.returncode}）")
                combined_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
                permission_denied = bool(_PERMISSION_ERROR_RE.search(combined_output))
                problem_details.append(
                    {
                        "script": name,
                        "exitCode": completed.returncode,
                        "payload": payload if isinstance(payload, dict) else None,
                        "stdoutTail": (completed.stdout or "")[-800:],
                        "stderrTail": (completed.stderr or "")[-800:],
                        "permissionDenied": permission_denied,
                        "cause": (
                            "sandbox_permission"
                            if permission_denied
                            else ("selftest_json_broken" if payload is None and completed.stdout.strip() else "selftest_failed")
                        ),
                    }
                )
                continue
        elif run_selftest:
            skipped_no_selftest.append(name)
        checked.append(name)
    if problems:
        permission_denied = any(bool(item.get("permissionDenied")) for item in problem_details)
        cause = "sandbox_permission" if permission_denied else ("selftest_json_broken" if problem_details and all(item.get("cause") == "selftest_json_broken" for item in problem_details) else "script_selftest_failed")
        evidence = "；".join(problems)
        if permission_denied:
            evidence += "（疑似沙箱/文件权限限制：WinError 5；可将 DSH 文件策略切为「完全权限」后重跑）"
        return _result(
            STATUS_FAILED,
            evidence,
            {
                "checked": checked,
                "problems": problems,
                "problemDetails": problem_details,
                "discovered": scripts,
                "pluginHash": plugin_hash,
                "permissionDenied": permission_denied,
                "cause": cause,
            },
        )
    return _result(
        STATUS_PASSED,
        f"{len(checked)}/{len(scripts)} 脚本动态发现、无 BOM、可编译、自检通过",
        {"checked": checked, "problems": [], "discovered": scripts, "skippedNoSelftest": skipped_no_selftest, "missingConfigured": missing_configured, "pluginHash": plugin_hash},
    )


def check_templates_integrity(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """templates_integrity：模板清单 + 9 模板；M2 构建未含 templates/ 时按 deferredUntil 记 skipped。"""
    item = ctx["item"]
    config = item.get("config") or {}
    manifest_rel = str(config.get("manifest") or "templates/extraction/_template-manifest.json")
    manifest_path = _PLUGIN_ROOT / manifest_rel
    deferred_until = item.get("deferredUntil")
    if not manifest_path.is_file():
        if deferred_until:
            return _result(
                STATUS_SKIPPED,
                f"模板随 {deferred_until} 交付，本构建未包含 templates/（延后检查）",
                {"pendingMilestone": deferred_until, "manifest": manifest_rel},
            )
        return _result(STATUS_FAILED, f"缺少模板清单 {manifest_rel}", {"manifest": manifest_rel})
    try:
        manifest = jsonio.read_json(manifest_path)
    except errors.BgError:
        return _result(STATUS_FAILED, f"模板清单不可解析：{manifest_rel}", {"manifest": manifest_rel})
    templates_dir_rel = str(config.get("templatesDir") or "templates/extraction")
    templates_dir = _PLUGIN_ROOT / templates_dir_rel
    expected = int(config.get("expectedTemplates") or 9)
    entries: list[str] = []
    if isinstance(manifest, Mapping):
        raw_entries = manifest.get("templates") or manifest.get("files") or []
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                if isinstance(entry, str):
                    entries.append(entry)
                elif isinstance(entry, Mapping):
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    if isinstance(path, str):
                        entries.append(path)
    missing_files = [entry for entry in entries if not (templates_dir / entry).is_file()]
    problems: list[str] = []
    if not isinstance(manifest, Mapping):
        problems.append("模板清单顶层必须是对象")
    if len(entries) < expected:
        problems.append(f"模板清单登记 {len(entries)} 个模板，少于基线 {expected} 个")
    if missing_files:
        problems.append(f"模板文件缺失：{', '.join(missing_files[:5])}")
    if problems:
        return _result(
            STATUS_FAILED,
            "；".join(problems),
            {"manifest": manifest_rel, "registered": len(entries), "missingFiles": missing_files},
        )
    return _result(
        STATUS_PASSED,
        f"模板清单与 {len(entries)} 个模板完整",
        {"manifest": manifest_rel, "registered": len(entries)},
    )


def check_fonts(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """fonts：中文字体（宋体/黑体）与等宽字体至少各 1 个可用。"""
    config = ctx["item"].get("config") or {}
    chinese_names = [str(name).lower() for name in config.get("chinese") or []]
    mono_names = [str(name).lower() for name in config.get("monospace") or []]
    font_dirs: list[Path] = []
    if os.name == "nt":
        font_dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
    else:
        font_dirs.extend([Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"])
    available: set[str] = set()
    for directory in font_dirs:
        if not directory.is_dir():
            continue
        try:
            for name in os.listdir(directory):
                available.add(name.lower())
        except OSError:
            continue
    chinese_found = [name for name in chinese_names if name in available]
    mono_found = [name for name in mono_names if name in available]
    ok = bool(chinese_found) and bool(mono_found)
    detail = {"chinese": chinese_found, "monospace": mono_found, "searched": [str(d) for d in font_dirs]}
    evidence = f"中文字体：{', '.join(chinese_found) or '未找到'}；等宽字体：{', '.join(mono_found) or '未找到'}"
    return _result(STATUS_PASSED if ok else STATUS_FAILED, evidence, detail)


def check_external_converters(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """external_converters：Word COM / soffice / pandoc 至少一种可用。"""
    found: list[str] = []
    word_exe = _find_word_exe()
    if word_exe is not None:
        found.append(f"winword:{word_exe}")
    soffice = _find_soffice()
    if soffice is not None:
        found.append(f"soffice:{soffice}")
    pandoc = shutil.which("pandoc") or shutil.which("pandoc.exe")
    if pandoc:
        found.append(f"pandoc:{pandoc}")
    if found:
        return _result(STATUS_PASSED, "；".join(found), {"found": found})
    return _result(STATUS_FAILED, "未检测到 Word / soffice / pandoc（.doc 无法自动转换）", {"found": []})


def check_browser_renderer(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """browser_renderer：Chrome/Edge + 内置 mermaid.min.js + node。

    P0-2（2026-11-23 干净 VM）：打包版 DSH 的 node 不在 PATH，旧实现只查浏览器，
    导致 env-check 假通过、交付全占位图。node 发现顺序与 `bg_render.py` 共用
    `_lib.runtime`（BG_NODE → PATH → DSH 运行时解释器兄弟目录）。
    """
    item = ctx["item"]
    config = item.get("config") or {}
    browser = _find_browser(item)
    asset_rel = str(config.get("mermaidAsset") or "assets/mermaid/mermaid.min.js")
    asset_path = _PLUGIN_ROOT / asset_rel
    node_path, node_source = runtime_env.find_node()
    problems: list[str] = []
    if browser is None:
        problems.append("未检测到 Chrome/Edge")
    if not asset_path.is_file():
        problems.append(f"缺少 {asset_rel}")
    if node_path is None:
        problems.append("未检测到 node（mermaid 会降级为 Pillow 占位图）")
    detail = {
        "browser": str(browser) if browser else "",
        "mermaidAsset": asset_rel,
        "node": str(node_path) if node_path else "",
        "nodeSource": node_source,
        "problems": problems,
    }
    if problems:
        return _result(STATUS_FAILED, "；".join(problems), detail)
    return _result(STATUS_PASSED, f"{browser} + {asset_rel} + node({node_source})", detail)


def check_workspace_writable(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """workspace_writable：写临时文件并删除（优先 dsh/，不存在时工作区根）。"""
    workspace: Path = ctx["workspace"]
    target_dir = paths.dsh_root(workspace)
    label = "dsh/"
    if not target_dir.is_dir():
        target_dir = workspace
        label = "工作区根"
    probe = target_dir / f".bidgenie-write-test-{paths.rand4()}"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"bidgenie-env-check\n")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError as exc:
        return _result(
            STATUS_FAILED,
            f"{label}不可写：{exc.strerror or type(exc).__name__}",
            {"target": label, "reason": exc.strerror or type(exc).__name__},
        )
    temp_dir = paths.dsh_root(workspace) / "tmp"
    temp_probe = temp_dir / f".bidgenie-tmp-test-{paths.rand4()}"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_probe.write_text("bidgenie env tmp\n", encoding="utf-8")
        temp_probe.unlink()
    except OSError as exc:
        return _result(
            STATUS_FAILED,
            f"dsh/tmp 临时目录不可写：{exc.strerror or type(exc).__name__}",
            {"target": "dsh/tmp", "reason": exc.strerror or type(exc).__name__},
        )
    return _result(STATUS_PASSED, f"{label}可写；dsh/tmp 可写（临时文件已删除）", {"target": label, "tempDir": "dsh/tmp"})


def check_disk_space(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """disk_space：工作区所在磁盘可用空间 ≥ 500 MB。"""
    workspace: Path = ctx["workspace"]
    config = ctx["item"].get("config") or {}
    minimum_mb = int(config.get("minFreeMB") or 500)
    try:
        usage = shutil.disk_usage(str(workspace))
    except OSError as exc:
        return _result(STATUS_FAILED, f"无法读取磁盘空间：{exc.strerror or type(exc).__name__}", {})
    free_mb = usage.free // (1024 * 1024)
    ok = free_mb >= minimum_mb
    return _result(
        STATUS_PASSED if ok else STATUS_FAILED,
        f"可用空间 {free_mb} MB（要求 ≥ {minimum_mb} MB）",
        {"freeBytes": usage.free, "totalBytes": usage.total, "minFreeMB": minimum_mb},
    )


def check_path_length(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """path_length：工作区路径 + 预期最深路径 ≤ 240（Windows）。"""
    workspace: Path = ctx["workspace"]
    config = ctx["item"].get("config") or {}
    threshold = int(config.get("maxLenWindows") or 240)
    suffix = str(config.get("estimatedDeepestSuffix") or "")
    workspace_len = len(str(workspace).rstrip("\\/"))
    estimated = workspace_len + len(suffix)
    if os.name != "nt":
        return _result(STATUS_SKIPPED, "非 Windows 系统，240 字符限制不适用", {"estimatedMaxLen": estimated})
    if estimated <= threshold:
        return _result(
            STATUS_PASSED,
            f"预期最深路径 {estimated} 字符（上限 {threshold}）",
            {"workspaceLen": workspace_len, "estimatedMaxLen": estimated, "threshold": threshold},
        )
    return _result(
        STATUS_FAILED,
        f"预期最深路径 {estimated} 字符，超过上限 {threshold}",
        {"workspaceLen": workspace_len, "estimatedMaxLen": estimated, "threshold": threshold},
    )


def _parse_skill_frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def check_skill_service(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """skill_service：M2 三个 SKILL.md 存在且 frontmatter 可解析（Host 注册由 M2-P3 验收覆盖）。"""
    config = ctx["item"].get("config") or {}
    skills_dir = _PLUGIN_ROOT / "skills"
    skills = sorted(path.parent.name for path in skills_dir.glob("*/SKILL.md") if path.is_file()) if skills_dir.is_dir() else []
    configured = [str(name) for name in config.get("skills") or []]
    missing_configured = [name for name in configured if name not in skills]
    found: list[str] = []
    problems: list[str] = []
    for name in skills:
        skill_file = _PLUGIN_ROOT / "skills" / name / "SKILL.md"
        if not skill_file.is_file():
            problems.append(f"缺少 skills/{name}/SKILL.md")
            continue
        if _scan_bom(skill_file):
            problems.append(f"skills/{name}/SKILL.md 含 UTF-8 BOM")
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"skills/{name}/SKILL.md 不可读：{exc.strerror or type(exc).__name__}")
            continue
        frontmatter = _parse_skill_frontmatter(text)
        if frontmatter is None:
            problems.append(f"skills/{name}/SKILL.md 缺少 frontmatter")
            continue
        skill_name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if not _KEBAB_RE.match(skill_name or ""):
            problems.append(f"skills/{name} frontmatter.name 非 kebab-case：{skill_name!r}")
            continue
        if skill_name != name:
            problems.append(f"skills/{name} frontmatter.name（{skill_name}）与目录名不一致")
            continue
        if not description:
            problems.append(f"skills/{name} frontmatter.description 为空")
            continue
        found.append(name)
    detail = {"skills": found, "discovered": skills, "problems": problems, "hostRegistrationVerified": False, "missingConfigured": missing_configured}
    if problems:
        return _result(STATUS_FAILED, "；".join(problems), detail)
    return _result(STATUS_PASSED, f"{len(found)}/{len(skills)} 技能文件可解析", detail)


def check_state_service(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """state_service：bg_state.py --selftest 返回 JSON 成功。"""
    config = ctx["item"].get("config") or {}
    script_name = str(config.get("script") or "bg_state.py")
    timeout = float(config.get("selftestTimeoutSec") or SCRIPT_SELFTEST_TIMEOUT_SEC)
    script_path = _SCRIPT_DIR / script_name
    if not script_path.is_file():
        return _result(
            STATUS_FAILED,
            f"{script_name} 尚未交付（随 M2-P2 落地）",
            {"missingScript": script_name, "pendingMilestone": "M2-P2"},
        )
    command = [sys.executable, "-X", "utf8", str(script_path), "--selftest", "--json"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(_PLUGIN_ROOT),
            env={**os.environ, "BIDGENIE_SKIP_ENV_REFRESH": "1"},
        )
    except subprocess.TimeoutExpired:
        return _result(STATUS_TIMEOUT, f"{script_name} --selftest 超时（{timeout:.0f}s）", {})
    payload: Any = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = None
    if completed.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True:
        return _result(STATUS_PASSED, f"{script_name} --selftest 通过（exit=0）", {})
    return _result(
        STATUS_FAILED,
        f"{script_name} --selftest 未通过（exit={completed.returncode}）",
        {"exitCode": completed.returncode},
    )


def check_host_version(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """host_version：比较宿主版本与基线（host 通过 BIDGENIE_HOST_VERSION / DSH_HOST_VERSION 注入）。"""
    item = ctx["item"]
    config = item.get("config") or {}
    env_names = [str(name) for name in config.get("env") or ["BIDGENIE_HOST_VERSION", "DSH_HOST_VERSION"]]
    host_version = ""
    for name in env_names:
        value = os.environ.get(name)
        if value:
            host_version = value.strip()
            break
    baseline = str(ctx["checklist"].get("hostVersionBaseline") or "")
    if not host_version:
        return _result(
            STATUS_SKIPPED,
            "未获取 DSH 宿主版本：Host 插件会自动读取 DSH app.asar/package.json 并注入 BIDGENIE_HOST_VERSION；也可在启动 DSH 前设置 BIDGENIE_HOST_VERSION=<DSH 版本>（该项不阻塞）",
            {"baseline": baseline, "hostVersion": "", "configuration": "BIDGENIE_HOST_VERSION=<DSH version>"},
        )
    host_parsed = _parse_version(host_version)
    base_parsed = _parse_version(baseline)
    if host_parsed is None or base_parsed is None:
        return _result(
            STATUS_SKIPPED,
            f"版本号无法比较（host={host_version!r}, baseline={baseline!r}）",
            {"hostVersion": host_version, "baseline": baseline},
        )
    compatible = host_parsed[0] == base_parsed[0] and host_parsed[:2] >= base_parsed[:2]
    return _result(
        STATUS_PASSED if compatible else STATUS_FAILED,
        f"宿主 {host_version} / 基线 {baseline}" + ("" if compatible else "（兼容性未验证）"),
        {"hostVersion": host_version, "baseline": baseline},
    )


#: itemId -> 检查函数（与 checklist.json 的 id 一一对应）
CHECKS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "python_runtime": check_python_runtime,
    "python_packages": check_python_packages,
    "pdf_libraries": check_pdf_libraries,
    "word_com": check_word_com,
    "scripts_integrity": check_scripts_integrity,
    "templates_integrity": check_templates_integrity,
    "fonts": check_fonts,
    "external_converters": check_external_converters,
    "browser_renderer": check_browser_renderer,
    "workspace_writable": check_workspace_writable,
    "disk_space": check_disk_space,
    "path_length": check_path_length,
    "skill_service": check_skill_service,
    "state_service": check_state_service,
    "host_version": check_host_version,
}

REPORT_ITEM_KEYS = (
    "id",
    "label",
    "description",
    "required",
    "itemVersion",
    "status",
    "evidence",
    "remediation",
    "degradeImpact",
    "degraded",
    "deferred",
    "checkedAt",
    "detail",
)


# ---------------------------------------------------------------------------
# 执行引擎
# ---------------------------------------------------------------------------
def _run_check(func: Callable[[Mapping[str, Any]], dict[str, Any]], ctx: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    """带单项超时的检查执行；超时记 timeout（线程无法强杀，仅作预算控制）。"""
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = func(ctx)
        except Exception as exc:  # noqa: BLE001 - 单项异常不得拖垮整体检查
            box["result"] = _result(
                STATUS_FAILED,
                f"检查异常：{type(exc).__name__}",
                {"error": str(exc)[:300]},
            )

    thread = threading.Thread(target=worker, name=f"env-check-{ctx['item'].get('id')}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return _result(STATUS_TIMEOUT, f"检查超过 {timeout:.0f}s", {"timeoutSec": timeout})
    result = box.get("result")
    if not isinstance(result, Mapping):
        return _result(STATUS_FAILED, "检查未返回结果", {})
    status = str(result.get("status") or STATUS_FAILED)
    evidence = str(result.get("evidence") or "")
    detail = result.get("detail") if isinstance(result.get("detail"), Mapping) else {}
    if status not in ALL_STATUSES:
        status = STATUS_FAILED
    return {"status": status, "evidence": evidence, "detail": dict(detail)}


def _host_version_from_env(checklist: Mapping[str, Any]) -> str:
    item = find_item(checklist, "host_version") or {}
    config = item.get("config") if isinstance(item.get("config"), Mapping) else {}
    names = [str(name) for name in config.get("env") or ["BIDGENIE_HOST_VERSION", "DSH_HOST_VERSION"]]
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def _checklist_hash(checklist: Mapping[str, Any]) -> str:
    raw = json.dumps(checklist, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _plugin_scripts_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(_SCRIPT_DIR.glob("bg_*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _should_skip(item: Mapping[str, Any], previous: Mapping[str, Any] | None, checklist_version: str) -> bool:
    """PRD 8.4 版本化跳过：checklistVersion + itemVersion + status=passed。"""
    if not previous:
        return False
    if str(previous.get("checklistVersion") or "") != checklist_version:
        return False
    entries = previous.get("items")
    if not isinstance(entries, Mapping):
        return False
    entry = entries.get(str(item.get("id") or ""))
    if not isinstance(entry, Mapping):
        return False
    return entry.get("status") == STATUS_PASSED and str(entry.get("itemVersion") or "") == str(item.get("itemVersion") or "")


def _normalize_previous_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    normalized = {
        "itemVersion": str(entry.get("itemVersion") or ""),
        "required": bool(entry.get("required")),
        "status": str(entry.get("status") or STATUS_NOT_RUN),
        "evidence": str(entry.get("evidence") or ""),
        "checkedAt": str(entry.get("checkedAt") or ""),
        "detail": dict(entry.get("detail")) if isinstance(entry.get("detail"), Mapping) else {},
    }
    return normalized


def build_env_check_document(
    workspace: Path,
    checklist: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
    item_ids: list[str] | None = None,
    per_item_timeout: float = PER_ITEM_TIMEOUT_SEC,
    overall_timeout: float = OVERALL_TIMEOUT_SEC,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """逐项检查并构造 env-check.json 文档；返回 (document, run)。"""
    checklist_version = str(checklist.get("checklistVersion") or "")
    current_checklist_hash = _checklist_hash(checklist)
    current_scripts_hash = _plugin_scripts_hash()
    if isinstance(previous, Mapping) and previous and (
        previous.get("checklistHash") != current_checklist_hash
        or previous.get("scriptsHash") != current_scripts_hash
    ):
        previous = None
    started = time.monotonic()
    now_iso = paths.iso_now()
    selected = set(item_ids) if item_ids else None
    items_out: dict[str, Any] = {}
    run: dict[str, Any] = {"checked": [], "skipped": [], "failed": [], "timeout": []}
    previous_entries = previous.get("items") if isinstance(previous, Mapping) else None
    previous_entries = previous_entries if isinstance(previous_entries, Mapping) else {}

    for item in checklist_items(checklist):
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        if selected is not None and item_id not in selected:
            preserved = _normalize_previous_entry(previous_entries.get(item_id))
            items_out[item_id] = preserved or {
                "itemVersion": str(item.get("itemVersion") or ""),
                "required": bool(item.get("required")),
                "status": STATUS_NOT_RUN,
                "evidence": "本次未检查",
                "checkedAt": "",
                "detail": {},
            }
            continue
        context: dict[str, Any] = {"workspace": workspace, "item": item, "checklist": checklist}
        func = CHECKS.get(item_id)
        if func is None:
            entry = {
                "itemVersion": str(item.get("itemVersion") or ""),
                "required": bool(item.get("required")),
                "status": STATUS_FAILED,
                "evidence": "未实现的检查项（checklist 与脚本不一致）",
                "checkedAt": now_iso,
                "detail": {},
            }
            items_out[item_id] = entry
            run["failed"].append(item_id)
            continue
        if selected is None and _should_skip(item, previous, checklist_version):
            preserved = _normalize_previous_entry(previous_entries.get(item_id))
            if preserved is None:  # pragma: no cover - _should_skip 已保证存在
                preserved = _normalize_previous_entry({}) or {}
            items_out[item_id] = preserved
            run["skipped"].append(item_id)
            continue
        if time.monotonic() - started > overall_timeout:
            items_out[item_id] = {
                "itemVersion": str(item.get("itemVersion") or ""),
                "required": bool(item.get("required")),
                "status": STATUS_SKIPPED,
                "evidence": "整体检查超过 10 分钟，剩余项未检查",
                "checkedAt": now_iso,
                "detail": {"reason": "overall_timeout", "overallTimeoutSec": overall_timeout},
            }
            continue
        result = _run_check(func, context, per_item_timeout)
        status = result["status"]
        detail = dict(result.get("detail") or {})
        deferred_until = item.get("deferredUntil")
        if status == STATUS_SKIPPED and deferred_until:
            detail.setdefault("pendingMilestone", str(deferred_until))
        entry = {
            "itemVersion": str(item.get("itemVersion") or ""),
            "required": bool(item.get("required")),
            "status": status,
            "evidence": str(result.get("evidence") or ""),
            "checkedAt": now_iso,
            "detail": detail,
        }
        items_out[item_id] = entry
        run["checked"].append(item_id)
        if status == STATUS_TIMEOUT:
            run["timeout"].append(item_id)
        elif status == STATUS_FAILED:
            run["failed"].append(item_id)

    # 旧清单项归档 + 保留旧豁免记录（PRD 8.4 / 7.6）
    obsolete: list[Any] = list(previous.get("obsoleteItems")) if isinstance(previous, Mapping) and isinstance(previous.get("obsoleteItems"), list) else []
    baseline_ids = {str(item.get("id") or "") for item in checklist_items(checklist)}
    if isinstance(previous_entries, Mapping):
        for old_id, old_entry in previous_entries.items():
            if str(old_id) in baseline_ids:
                continue
            obsolete.append(
                {
                    "id": str(old_id),
                    "archivedAt": now_iso,
                    "itemVersion": str((old_entry or {}).get("itemVersion") or "") if isinstance(old_entry, Mapping) else "",
                    "status": str((old_entry or {}).get("status") or "") if isinstance(old_entry, Mapping) else "",
                }
            )
    waivers = list(previous.get("waivers")) if isinstance(previous, Mapping) and isinstance(previous.get("waivers"), list) else []
    degraded = [
        item_id
        for item_id, entry in items_out.items()
        if not entry.get("required") and entry.get("status") in DEGRADED_STATUSES
    ]
    document = {
        "schemaVersion": ENV_CHECK_SCHEMA_VERSION,
        "checklistVersion": checklist_version,
        "checklistHash": current_checklist_hash,
        "scriptsHash": current_scripts_hash,
        "hostVersion": _host_version_from_env(checklist),
        "checkedAt": now_iso,
        "items": items_out,
        "waivers": waivers,
        "obsoleteItems": obsolete,
        "degraded": degraded,
        "updatedAt": now_iso,
    }
    run["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return document, run


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
_REPAIR_ACTION_BY_ITEM = {
    "python_packages": "install-core-packages",
    "pdf_libraries": "install-pdf-libraries",
    "word_com": "install-pywin32",
}
_MANUAL_ITEMS = {"workspace_writable", "disk_space", "path_length", "fonts"}
_ASSISTED_ITEMS = {"browser_renderer"}
_PLUGIN_DEFECT_ITEMS = {"scripts_integrity", "templates_integrity", "skill_service", "state_service"}


def failure_diagnosis(item_id: str, detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """失败项 cause/causeDetail/remediable/repairAction 映射（env-check 与 repair plan 共用）。"""
    detail = detail or {}
    errors = detail.get("errors") if isinstance(detail.get("errors"), Mapping) else {}
    binary = detail.get("binaryIntegrity") if isinstance(detail.get("binaryIntegrity"), Mapping) else {}
    if item_id == "scripts_integrity":
        if detail.get("permissionDenied") or detail.get("cause") == "sandbox_permission":
            return {
                "cause": "sandbox_permission",
                "causeDetail": "脚本自检被文件权限拒绝（WinError 5/拒绝访问），疑似 DSH 沙箱或临时目录 ACL 限制",
                "remediable": "manual",
                "repairAction": "",
                "hint": "把 DSH 文件策略切为「完全权限」（danger-full-access）后重跑 bg_env_check --apply；如仍失败，设置 BIDGENIE_TEMP_DIR 指向可写目录",
            }
        cause = "selftest_json_broken" if detail.get("cause") == "selftest_json_broken" else "script_selftest_failed"
        problems = "；".join(str(problem) for problem in (detail.get("problems") or []))[:300]
        if cause == "selftest_json_broken":
            cause_detail = problems or "脚本自检输出不是合法的单个 JSON 对象"
        else:
            cause_detail = problems or "脚本自检未通过（未返回 problems 详情）"
        return {"cause": cause, "causeDetail": cause_detail, "remediable": "plugin_defect", "repairAction": ""}
    if item_id in _PLUGIN_DEFECT_ITEMS:
        return {"cause": "plugin_defect", "causeDetail": str(detail.get("problems") or detail.get("missing") or "")[:300], "remediable": "plugin_defect", "repairAction": ""}
    if item_id in _MANUAL_ITEMS:
        return {"cause": "environment_manual", "causeDetail": "需要用户/运维手动处理（目录权限/磁盘/字体/路径）", "remediable": "manual", "repairAction": ""}
    if item_id in _ASSISTED_ITEMS:
        return {"cause": "browser_missing_or_render_failed", "causeDetail": str(detail.get("problems") or "")[:300], "remediable": "assisted", "repairAction": ""}
    # word_com 有两种失败：pywin32 缺失（可自动装）与本机没装 Word（只能人工处理）。
    # 不区分的话，没装 Word 的机器会把“装 pywin32”当成可修复项，装完仍失败并回滚。
    if item_id == "word_com" and not detail.get("missingModules") and not detail.get("wordExe"):
        return {"cause": "word_missing", "causeDetail": "pywin32 可导入，但未检测到本机 Word（WINWORD.EXE）", "remediable": "manual", "repairAction": ""}
    if item_id in _REPAIR_ACTION_BY_ITEM:
        if binary.get("ok") is False or (binary and not binary.get("binaryCount")):
            cause = "pylibs_binary_missing"
        elif errors:
            cause = "import_error"
        elif detail.get("mismatched"):
            cause = "version_mismatch"
        else:
            cause = "dependency_missing"
        return {
            "cause": cause,
            "causeDetail": "；".join(str(item) for item in (detail.get("mismatched") or detail.get("missing") or [])[:5])[:500],
            "remediable": "auto",
            "repairAction": _REPAIR_ACTION_BY_ITEM[item_id],
            "installHint": detail.get("installHint") or "",
        }
    return {"cause": "unknown", "causeDetail": "", "remediable": "manual", "repairAction": ""}


def build_report(
    workspace: Path,
    checklist: Mapping[str, Any],
    env_document: Mapping[str, Any] | None,
    *,
    command: str,
    run: Mapping[str, Any] | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    """把基线 + env-check.json 合并为 Host/面板可用的报告形状（items 为数组）。"""
    checklist_version = str(checklist.get("checklistVersion") or "")
    entries = env_document.get("items") if isinstance(env_document, Mapping) else None
    entries = entries if isinstance(entries, Mapping) else {}
    items: list[dict[str, Any]] = []
    required_passed = 0
    required_deferred = 0
    optional_passed = 0
    required_count = 0
    optional_count = 0
    degraded: list[str] = []
    deferred: list[str] = []
    waived: list[str] = []
    for item in checklist_items(checklist):
        item_id = str(item.get("id") or "")
        required = bool(item.get("required"))
        if required:
            required_count += 1
        else:
            optional_count += 1
        entry = entries.get(item_id)
        status = str(entry.get("status") or STATUS_NOT_RUN) if isinstance(entry, Mapping) else STATUS_NOT_RUN
        evidence = str(entry.get("evidence") or "") if isinstance(entry, Mapping) else ""
        checked_at = str(entry.get("checkedAt") or "") if isinstance(entry, Mapping) else ""
        detail = dict(entry.get("detail")) if isinstance(entry, Mapping) and isinstance(entry.get("detail"), Mapping) else {}
        if status == STATUS_FAILED:
            detail.update(failure_diagnosis(item_id, detail))
        is_degraded = (not required) and status in DEGRADED_STATUSES
        is_deferred = bool(required and status == STATUS_SKIPPED and item.get("deferredUntil"))
        if status == STATUS_PASSED:
            if required:
                required_passed += 1
            else:
                optional_passed += 1
        if status == STATUS_WAIVED:
            waived.append(item_id)
        if is_degraded:
            degraded.append(item_id)
        if is_deferred:
            deferred.append(item_id)
            required_deferred += 1
        item_payload = {
            "id": item_id,
            "label": str(item.get("label") or ""),
            "description": str(item.get("description") or ""),
            "required": required,
            "itemVersion": str(item.get("itemVersion") or ""),
            "status": status,
            "evidence": evidence,
            "remediation": str(item.get("remediation") or ""),
            "degradeImpact": str(item.get("degradeImpact") or ""),
            "degraded": is_degraded,
            "deferred": is_deferred,
            "checkedAt": checked_at,
            "detail": detail,
        }
        items.append(item_payload)
    status = "ready" if env_document is not None else "not-run"
    blocked = (required_passed + required_deferred) < required_count
    failed_items = [item for item in items if item["status"] == STATUS_FAILED]
    diagnostic = deps.dependency_report()
    binary_integrity = diagnostic.get("binaryIntegrity") or {}
    root_cause: dict[str, Any] | None = None
    install_hint = ""
    if failed_items:
        error_text = " ".join(str(item.get("detail")) for item in failed_items)
        diagnoses = [item["detail"] for item in failed_items]
        sandbox_failed = [item for item in failed_items if item["detail"].get("cause") == "sandbox_permission"]
        if sandbox_failed:
            root_cause = {
                "code": "sandbox_permission",
                "message": "脚本自检被文件权限拒绝（WinError 5/拒绝访问），疑似 DSH 沙箱限制；请把文件策略切为「完全权限」后重跑环境检查",
                "items": [item["id"] for item in sandbox_failed],
                "hint": "DSH 会话设置 → 文件访问策略 → 完全权限；或在可写环境设置 BIDGENIE_TEMP_DIR 后重试",
                "diagnoses": [
                    {"id": item["id"], **{key: item["detail"].get(key) for key in ("cause", "causeDetail", "remediable", "hint")}}
                    for item in sandbox_failed
                ],
            }
        elif all(diagnosis.get("remediable") == "plugin_defect" for diagnosis in diagnoses):
            root_cause = {
                "code": "plugin_defect",
                "message": "插件自检/脚本输出问题，不是依赖缺失；请修脚本或重装插件",
                "items": [item["id"] for item in failed_items],
                "diagnoses": [{"id": item["id"], **{key: item["detail"].get(key) for key in ("cause", "causeDetail", "remediable")}} for item in failed_items],
            }
        elif not diagnostic.get("exists"):
            root_cause = {
                "code": "pylibs_not_installed",
                "message": "插件自有依赖尚未安装（首次使用需下载约 100 MB）；在面板点「修复环境」即可自动安装",
                "pylibs": diagnostic.get("pylibs") or "",
                "source": diagnostic.get("source") or "",
                "binaryIntegrity": binary_integrity,
                "candidates": diagnostic.get("candidates") or [],
            }
        elif binary_integrity.get("missing"):
            root_cause = {
                "code": "pylibs_binary_incomplete",
                "message": "pylibs 二进制不完整（常见根因：GitHub 仓库 .gitignore *.py[cod] 误伤 .pyd）",
                "pylibs": diagnostic.get("pylibs") or "",
                "source": diagnostic.get("source") or "",
                "binaryIntegrity": binary_integrity,
                "candidates": diagnostic.get("candidates") or [],
            }
        elif not binary_integrity.get("binaryCount"):
            root_cause = {
                "code": "pylibs_empty",
                "message": "pylibs 目录为空或缺少编译扩展；请在面板点「修复环境」重新安装依赖",
                "pylibs": diagnostic.get("pylibs") or "",
                "source": diagnostic.get("source") or "",
                "binaryIntegrity": binary_integrity,
                "candidates": diagnostic.get("candidates") or [],
            }
        elif binary_integrity.get("ok") is False or "pyd" in error_text or "_extra" in error_text:
            root_cause = {
                "code": "pylibs_binary_incomplete",
                "message": "pylibs 二进制不完整（常见根因：GitHub 仓库 .gitignore *.py[cod] 误伤 .pyd）",
                "pylibs": diagnostic.get("pylibs") or "",
                "source": diagnostic.get("source") or "",
                "binaryIntegrity": binary_integrity,
                "candidates": diagnostic.get("candidates") or [],
            }
        else:
            root_cause = {
                "code": "dependency_import_failed",
                "message": "依赖可导入性失败，见 items[].detail.errors",
                "pylibs": diagnostic.get("pylibs") or "",
                "source": diagnostic.get("source") or "",
                "candidates": diagnostic.get("candidates") or [],
            }
        if root_cause.get("code") != "sandbox_permission":
            install_hint = f"pip install --target <DSH_HOME>/data/bidgenie/pylibs -r <plugin>/libexec/requirements.txt（或设置 BIDGENIE_PYLIBS 指向完整 pylibs）"
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
        "status": status,
        "checklistVersion": checklist_version,
        "hostVersion": str(env_document.get("hostVersion") or "") if isinstance(env_document, Mapping) else _host_version_from_env(checklist),
        "checkedAt": str(env_document.get("checkedAt") or "") if isinstance(env_document, Mapping) else "",
        "items": items,
        "requiredPassed": required_passed,
        "requiredTotal": required_count,
        "optionalPassed": optional_passed,
        "optionalTotal": optional_count,
        "degraded": degraded,
        "deferred": deferred,
        "waived": waived,
        "summary": {
            "requiredPassed": required_passed,
            "requiredTotal": required_count,
            "optionalPassed": optional_passed,
            "optionalTotal": optional_count,
            "degradedCount": len(degraded),
            "waivedCount": len(waived),
            "deferredCount": len(deferred),
            "blocked": blocked,
        },
        "changed": list(changed or []),
        "skipped": [],
        "rootCause": root_cause,
        "installHint": install_hint,
        "sandboxHint": str((root_cause or {}).get("hint") or "") if (root_cause or {}).get("code") == "sandbox_permission" else "",
        "dependencyDiagnostics": diagnostic,
    }
    if run is not None:
        payload["run"] = dict(run)
    return payload


# ---------------------------------------------------------------------------
# --report / --apply
# ---------------------------------------------------------------------------
def report_env_check(workspace: str | Path) -> dict[str, Any]:
    """只读报告：不创建目录、不写文件（读 env-check.json 或返回 not-run）。"""
    ws = paths.normalize_workspace(workspace)
    checklist = load_checklist()
    document = read_env_check(ws, missing_ok=True)
    return build_report(ws, checklist, document, command="env_check_report")


def apply_env_check(
    workspace: str | Path,
    *,
    item_ids: list[str] | None = None,
    per_item_timeout: float = PER_ITEM_TIMEOUT_SEC,
    overall_timeout: float = OVERALL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """执行检查并原子写 dsh/config/env-check.json（幂等，版本化跳过）。"""
    ws = paths.normalize_workspace(workspace)
    checklist = load_checklist()
    if item_ids:
        unknown = [item_id for item_id in item_ids if find_item(checklist, item_id) is None]
        if unknown:
            raise errors.BgError(
                errors.E_USAGE,
                f"未知检查项：{', '.join(unknown)}",
                hint="检查项 id 见 libexec/checklist.json",
                detail={"unknown": unknown},
            )
    previous = read_env_check(ws, missing_ok=True)
    changed: list[str] = []
    config_dir = paths.config_dir(ws)
    if not config_dir.is_dir():
        config_dir.mkdir(parents=True, exist_ok=True)
        changed.append("dsh/config")
    document, run = build_env_check_document(
        ws,
        checklist,
        previous=previous,
        item_ids=item_ids,
        per_item_timeout=per_item_timeout,
        overall_timeout=overall_timeout,
    )
    target = env_check_path(ws)
    jsonio.write_json_atomic(target, document)
    changed.append("dsh/config/env-check.json")
    event_log.log_event(
        ws,
        "env.checked",
        actor="script",
        detail={
            "checklistVersion": str(checklist.get("checklistVersion") or ""),
            "requiredPassed": sum(
                1
                for entry in document["items"].values()
                if entry.get("required") and entry.get("status") == STATUS_PASSED
            ),
            "requiredTotal": required_total(checklist),
            "degraded": list(document.get("degraded") or []),
            "checked": len(run.get("checked") or []),
            "skipped": len(run.get("skipped") or []),
        },
    )
    if document.get("degraded"):
        event_log.log_event(
            ws,
            "env.degraded",
            actor="script",
            detail={"degraded": list(document.get("degraded") or [])},
            result="warning",
        )
    report = build_report(
        ws,
        checklist,
        document,
        command="env_check_apply",
        run=run,
        changed=changed,
    )
    report["skipped"] = list(run.get("skipped") or [])
    return report


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """脚本自检：基线、调度覆盖、跳过规则、env-check 读写（临时工作区）。"""
    checks: list[str] = []
    tmp = tempdir.mkdtemp(prefix="bidgenie-env-selftest-")
    try:
        checklist = load_checklist()
        items = checklist_items(checklist)
        if len(items) != 15:
            raise AssertionError(f"检查项应为 15 项，实际 {len(items)}")
        if required_total(checklist) != 8:
            raise AssertionError(f"必需项应为 8 项，实际 {required_total(checklist)}")
        if len({item.get("id") for item in items}) != len(items):
            raise AssertionError("检查项 id 重复")
        checks.append("checklist")

        missing_handlers = [str(item.get("id")) for item in items if item.get("id") not in CHECKS]
        if missing_handlers:
            raise AssertionError(f"缺少检查函数：{missing_handlers}")
        checks.append("dispatch")

        # 三个纯本地快速检查
        for item_id in ("python_runtime", "disk_space", "workspace_writable"):
            item = find_item(checklist, item_id)
            assert item is not None
            result = _run_check(CHECKS[item_id], {"workspace": tmp, "item": item, "checklist": checklist}, 30.0)
            if result["status"] != STATUS_PASSED:
                raise AssertionError(f"{item_id} 自检未通过：{result['evidence']}")
        checks.append("fast_checks")

        # 版本化跳过规则
        runtime_item = find_item(checklist, "python_runtime")
        passed_previous = {
            "checklistVersion": checklist.get("checklistVersion"),
            "items": {
                "python_runtime": {
                    "itemVersion": runtime_item.get("itemVersion"),
                    "required": True,
                    "status": STATUS_PASSED,
                    "evidence": "fake",
                    "checkedAt": "",
                    "detail": {},
                }
            },
        }
        if not _should_skip(runtime_item, passed_previous, str(checklist.get("checklistVersion"))):
            raise AssertionError("passed 项未按版本化规则跳过")
        if _should_skip(runtime_item, passed_previous, "999.0.0"):
            raise AssertionError("checklistVersion 变化时不应跳过")
        failed_previous = {
            "checklistVersion": checklist.get("checklistVersion"),
            "items": {"python_runtime": {"itemVersion": runtime_item.get("itemVersion"), "status": STATUS_FAILED}},
        }
        if _should_skip(runtime_item, failed_previous, str(checklist.get("checklistVersion"))):
            raise AssertionError("failed 项不应跳过")
        bumped_previous = {
            "checklistVersion": checklist.get("checklistVersion"),
            "items": {"python_runtime": {"itemVersion": "0.9", "status": STATUS_PASSED}},
        }
        if _should_skip(runtime_item, bumped_previous, str(checklist.get("checklistVersion"))):
            raise AssertionError("itemVersion 变化时不应跳过")
        checks.append("skip_logic")

        # env-check.json 结构读写
        workspace = tmp / "ws"
        workspace.mkdir()
        probe = env_check_path(workspace)
        probe.parent.mkdir(parents=True, exist_ok=True)
        fake_document = {
            "schemaVersion": ENV_CHECK_SCHEMA_VERSION,
            "checklistVersion": checklist.get("checklistVersion"),
            "hostVersion": "",
            "checkedAt": paths.iso_now(),
            "items": {"python_runtime": {"itemVersion": "1.0", "required": True, "status": STATUS_PASSED, "evidence": "fake", "checkedAt": "", "detail": {}}},
            "waivers": [],
            "obsoleteItems": [],
            "degraded": [],
            "updatedAt": paths.iso_now(),
        }
        jsonio.write_json_atomic(probe, fake_document, lock=False)
        loaded = read_env_check(workspace)
        if loaded is None or "python_runtime" not in loaded["items"]:
            raise AssertionError("env-check.json 读写失败")
        report = build_report(workspace, checklist, loaded, command="env_check_report")
        if len(report["items"]) != 15 or report["requiredTotal"] != 8:
            raise AssertionError("报告结构与基线不一致")
        checks.append("storage+report")

        return {
            "ok": True,
            "selftest": {
                "script": "bg_env_check.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_env_check.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实工作区",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（CONTRACT §8.2：--report / --apply / --selftest）。"""
    parser = errors.CliParser(
        prog="bg_env_check.py",
        description="BidGenie M2 环境自检（PRD 附录 D：15 项 / 必需 8）",
    )
    errors.add_common_args(parser)
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--report", action="store_true", help="只读报告（不写任何文件）")
    action_group.add_argument("--apply", action="store_true", help="逐项检查并写 dsh/config/env-check.json（默认）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    parser.add_argument("--workspace", help="工作区根的绝对路径（--report/--apply 必填）")
    parser.add_argument(
        "--item",
        action="append",
        dest="items",
        metavar="ID",
        help="只检查指定检查项（可重复；配合 --apply）",
    )
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.workspace:
        raise errors.BgError(errors.E_USAGE, "缺少 --workspace", hint="传入工作区根的绝对路径")
    if args.report and getattr(args, "items", None):
        raise errors.BgError(errors.E_USAGE, "--item 只能与 --apply 一起使用", hint="只读报告不支持单点重跑")
    per_item_timeout = min(max(errors.timeout_value(args, default=PER_ITEM_TIMEOUT_SEC), 1.0), PER_ITEM_TIMEOUT_SEC)
    if args.report:
        return report_env_check(args.workspace)
    return apply_env_check(
        args.workspace,
        item_ids=list(getattr(args, "items", None) or []),
        per_item_timeout=per_item_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）；先挂载 ``plugin/pylibs`` 依赖路径（PRD B.3）。"""
    deps.ensure_dependency_path()
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_env_check.py",
    )


if __name__ == "__main__":
    sys.exit(main())
