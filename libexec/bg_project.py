#!/usr/bin/env python3
"""bg_project.py：F3 `project.json` 字段回填 CLI（M3.1 G1，PRD F3.4 / 4.5）。

用法：
    python bg_project.py fields --workspace <ws> --project <dir|id> --data <fields.json> [--json]
    python bg_project.py --selftest [--json]

`fields` 子命令只允许回填 F3 字段白名单，经 `_lib/schema.validate_project` 校验后
**原子写 + 文件锁**更新 `project.json`；只改 project.json，绝不改写 `state.json`；成功后
追加事件 `project.fields_filled`（PRD 7.9 白名单）。

白名单：`projectNo` / `projectName` / `isSubcontracted` / `packageCount` /
`packagesToWrite` / `sourceFiles`。`sourceFiles` 条目仅允许
`originalName` / `rawPath` / `convertedPath` / `kind` / `hash`（PRD 4.5）；路径一律是
**项目目录相对 POSIX 路径**（如 `sources/raw/x.pdf`，不要带项目目录名前缀）；
缺少 `hash` 且源文件存在时自动补 `sha256:<hex>`。

data 文件格式（二选一，其余键一律拒绝）：
    {"projectNo": "...", "projectName": "...", ...}
    {"fields": {"projectNo": "...", ...}}
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lib import errors, jsonio, logging as event_log, paths, schema, state as state_lib  # noqa: E402

#: F3 允许回填的 project.json 顶层字段（PRD F3.4 / 4.5）
FIELD_WHITELIST: tuple[str, ...] = (
    "projectNo",
    "projectName",
    "isSubcontracted",
    "packageCount",
    "packagesToWrite",
    "sourceFiles",
)

#: sourceFiles 条目允许键（PRD 4.5 / `_lib.schema.PROJECT_SOURCE_FILE_KEYS`）
SOURCE_FILE_KEYS: tuple[str, ...] = schema.PROJECT_SOURCE_FILE_KEYS
SOURCE_FILE_REQUIRED: tuple[str, ...] = ("originalName", "rawPath", "convertedPath")

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PACKAGE_ID_RE = re.compile(r"^package-\d+$")


# ---------------------------------------------------------------------------
# 字段规范化/校验
# ---------------------------------------------------------------------------
def _normalize_rel_path(value: Any, *, field: str) -> str:
    """规范化项目目录相对路径（POSIX 分隔、禁止绝对路径/`..`）。"""
    if not isinstance(value, str) or not value.strip():
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"{field} 必须是非空字符串",
            hint="使用项目目录相对路径（如 sources/raw/x.pdf，POSIX 分隔，不要带项目目录名）",
            detail={"field": field},
        )
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"{field} 不得是绝对路径：{value!r}",
            hint="sourceFiles 只记录项目目录内相对路径（如 sources/raw/x.pdf）",
            detail={"field": field, "value": value},
        )
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"{field} 不得包含 `..`：{value!r}",
                hint="路径必须位于项目目录内",
                detail={"field": field, "value": value},
            )
        parts.append(part)
    normalized = "/".join(parts)
    if not normalized:
        raise errors.BgError(errors.E_SCHEMA_INVALID, f"{field} 为空路径", detail={"field": field})
    return normalized


def normalize_source_files(project_dir: Path, entries: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """校验/规范化 sourceFiles 条目；返回 (新条目, warnings)。"""
    if not isinstance(entries, list):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "sourceFiles 必须是数组",
            hint="每项为 {originalName, rawPath, convertedPath, kind?, hash?}",
            detail={"field": "sourceFiles"},
        )
    warnings: list[str] = []
    normalized: list[dict[str, Any]] = []
    seen_raw: set[str] = set()
    for index, entry in enumerate(entries):
        pointer = f"sourceFiles[{index}]"
        if not isinstance(entry, Mapping):
            raise errors.BgError(errors.E_SCHEMA_INVALID, f"{pointer} 必须是 JSON 对象", detail={"field": pointer})
        unknown = [key for key in entry if key not in SOURCE_FILE_KEYS]
        if unknown:
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"{pointer} 含未知键：{unknown}",
                hint=f"sourceFiles 条目仅允许 {list(SOURCE_FILE_KEYS)}",
                detail={"field": pointer, "unknown": unknown},
            )
        record: dict[str, Any] = {}
        original_name = entry.get("originalName")
        if not isinstance(original_name, str) or not original_name.strip():
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"{pointer}.originalName 必须是非空字符串",
                detail={"field": f"{pointer}.originalName"},
            )
        record["originalName"] = original_name.strip()
        raw_path = _normalize_rel_path(entry.get("rawPath"), field=f"{pointer}.rawPath")
        converted_path = _normalize_rel_path(entry.get("convertedPath"), field=f"{pointer}.convertedPath")
        if raw_path in seen_raw:
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                f"{pointer}.rawPath 重复：{raw_path}",
                detail={"field": f"{pointer}.rawPath"},
            )
        seen_raw.add(raw_path)
        if not raw_path.startswith("sources/raw/"):
            if raw_path.startswith(project_dir.name + "/"):
                warnings.append(
                    f"{pointer}.rawPath 带了项目目录名前缀（应去掉 {project_dir.name}/）：{raw_path}"
                )
            else:
                warnings.append(
                    f"{pointer}.rawPath 必须是项目目录相对路径并以 sources/raw/ 开头（如 sources/raw/x.pdf）：{raw_path}"
                )
        if not converted_path.startswith("sources/"):
            warnings.append(
                f"{pointer}.convertedPath 必须是项目目录相对路径并以 sources/ 开头（如 sources/01-bidding-documents.md）：{converted_path}"
            )
        record["rawPath"] = raw_path
        record["convertedPath"] = converted_path
        kind = entry.get("kind")
        if kind is not None:
            if not isinstance(kind, str) or not kind.strip():
                raise errors.BgError(
                    errors.E_SCHEMA_INVALID,
                    f"{pointer}.kind 必须是非空字符串或省略",
                    detail={"field": f"{pointer}.kind"},
                )
            record["kind"] = kind.strip()
        digest = entry.get("hash")
        raw_abs = project_dir / raw_path
        if digest is not None:
            if not isinstance(digest, str) or not HASH_RE.match(digest.strip()):
                raise errors.BgError(
                    errors.E_SCHEMA_INVALID,
                    f"{pointer}.hash 必须形如 sha256:<64 位小写十六进制>",
                    detail={"field": f"{pointer}.hash"},
                )
            record["hash"] = digest.strip()
            if raw_abs.is_file():
                actual = "sha256:" + hashlib.sha256(raw_abs.read_bytes()).hexdigest()
                if actual != record["hash"]:
                    raise errors.BgError(
                        errors.E_SCHEMA_INVALID,
                        f"{pointer}.hash 与源文件实际内容不一致",
                        hint="重新计算 sha256 或检查 rawPath 是否指向同一文件",
                        detail={"field": f"{pointer}.hash", "declared": record["hash"], "actual": actual},
                    )
        elif raw_abs.is_file():
            record["hash"] = "sha256:" + hashlib.sha256(raw_abs.read_bytes()).hexdigest()
            warnings.append(f"{pointer}.hash 缺失，已按源文件自动补全")
        # 其余允许键（PROJECT_SOURCE_FILE_KEYS 未来扩展）原样保留
        for key in SOURCE_FILE_KEYS:
            if key in entry and key not in record:
                record[key] = entry[key]
        normalized.append(record)
    return normalized, warnings


def _derive_packages(package_count: int) -> list[str]:
    return [f"package-{index}" for index in range(1, package_count + 1)]


def _validate_packages(candidate: Mapping[str, Any], package_count: Any) -> list[str]:
    """校验 `packagesToWrite`（P3-14）：允许显式空数组与 packageCount 内子集，交 F4 选包覆盖。

    - 不传 `packagesToWrite` 时仍由 `packageCount` 自动生成（`apply_fields` 内处理）；
    - 显式 `[]` 合法：表示「撰写包待 F4 确认」，返回 warning 提示由 F4 覆盖（不再报 schema 错）；
    - 子集（升序、packageCount 内）合法：F4 `select-packages` 会写入所选子集；
    - 越界/重复/非法标识/降序仍按 `E_SCHEMA_INVALID` 拒绝。
    """
    packages = candidate.get("packagesToWrite")
    if packages is None:
        return []
    if not isinstance(packages, list) or not all(isinstance(item, str) and PACKAGE_ID_RE.match(item) for item in packages):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            "packagesToWrite 必须是 package-<N> 字符串数组",
            detail={"field": "packagesToWrite"},
        )
    if len(set(packages)) != len(packages):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"packagesToWrite 不得重复：{packages}",
            hint="每个包只出现一次；撰写包由 F4 选包确认",
            detail={"field": "packagesToWrite", "actual": packages},
        )
    warnings: list[str] = []
    if not isinstance(package_count, int) or isinstance(package_count, bool) or package_count < 1:
        return warnings
    expected = _derive_packages(package_count)
    out_of_range = [item for item in packages if item not in expected]
    if out_of_range:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"packagesToWrite 超出 packageCount={package_count}：{out_of_range}",
            hint=f"packageCount={package_count} 时只能填 {expected} 内的包",
            detail={"field": "packagesToWrite", "packageCount": package_count, "actual": packages},
        )
    if not packages:
        warnings.append("packagesToWrite=[] 已接受：撰写包待 F4 确认，将由 F4（bg_project.py select-packages）覆盖")
        return warnings
    ordered_subset = [item for item in expected if item in set(packages)]
    if packages != ordered_subset:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"packagesToWrite 需按 package-<N> 升序：{packages}",
            hint=f"按 {ordered_subset} 顺序填写",
            detail={"field": "packagesToWrite", "actual": packages, "expected": ordered_subset},
        )
    if packages != expected:
        warnings.append(
            f"packagesToWrite 为 packageCount={package_count} 的子集（{packages}）：未选包不会撰写；"
            "最终撰写包以 F4 选包（bg_project.py select-packages）为准"
        )
    return warnings


def _normalize_selected_packages(raw: Any, package_count: int) -> list[str]:
    if isinstance(raw, str):
        parts = [part.strip() for part in re.split(r"[,，、\s]+", raw) if part.strip()]
        values: list[str] = []
        for part in parts:
            match = re.fullmatch(r"(?:package-)?(\d+)", part)
            if not match:
                raise errors.BgError(errors.E_USAGE, f"包选择非法：{part!r}", detail={"packages": raw})
            values.append(f"package-{int(match.group(1))}")
    elif isinstance(raw, list):
        values = []
        for item in raw:
            text = str(item).strip()
            match = re.fullmatch(r"(?:package-)?(\d+)", text)
            if not match:
                raise errors.BgError(errors.E_USAGE, f"包选择非法：{item!r}", detail={"packages": raw})
            values.append(f"package-{int(match.group(1))}")
    else:
        raise errors.BgError(errors.E_USAGE, "--packages 必须是逗号分隔的数字或 package-N 数组", detail={"packages": raw})
    selected = sorted(set(values), key=lambda item: int(item.split("-")[1]))
    if not selected:
        raise errors.BgError(errors.E_USAGE, "至少选择 1 个撰写包", detail={"packages": raw})
    invalid = [item for item in selected if not 1 <= int(item.split("-")[1]) <= package_count]
    if invalid:
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"选择的包超出 packageCount=1..{package_count}：{invalid}",
            detail={"selected": selected, "packageCount": package_count},
        )
    return selected


def select_packages(workspace: Path, project_dir: Path, packages: Any) -> dict[str, Any]:
    """一次性确认撰写包：写 writingPackages/packageSelection，事件 package.confirmed（actor=user）。"""
    workspace = Path(workspace).resolve()
    project_dir = Path(project_dir).resolve()
    project_file = project_dir / "project.json"
    if not project_file.is_file():
        raise errors.BgError(errors.E_NOT_FOUND, "缺少 project.json", detail={"project": str(project_dir)})
    project = jsonio.read_json(project_file)
    if not isinstance(project, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "project.json 必须是 JSON 对象")
    package_count = project.get("packageCount")
    if not isinstance(package_count, int) or isinstance(package_count, bool) or package_count < 1:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "packageCount 未回填，无法确认撰写包",
            hint="先完成 F3 分包识别并回填 packageCount（bg_project.py fields）",
            detail={"packageCount": package_count},
        )
    selected = _normalize_selected_packages(packages, package_count)
    now = paths.iso_now()
    project["writingPackages"] = selected
    project["packageSelection"] = {
        "selected": selected,
        "candidates": _derive_packages(package_count),
        "confirmedAt": now,
        "confirmedBy": "user",
    }
    project["packagesToWrite"] = selected
    project["status"] = "packages_selected"
    project["updatedAt"] = now
    schema.raise_if_errors(schema.validate_project(project), filename="project.json")
    jsonio.write_json_atomic(project_file, project)
    try:
        event_log.log_event(
            workspace,
            "package.confirmed",
            actor="user",
            project=str(project.get("projectId") or ""),
            detail={"selected": selected, "candidates": _derive_packages(package_count), "confirmedBy": "user"},
        )
    except ValueError as exc:
        raise errors.BgError(errors.E_UNEXPECTED, f"事件类型未登记：{exc}") from exc
    return {
        "ok": True,
        "command": "project_select_packages",
        "projectId": str(project.get("projectId") or ""),
        "writingPackages": selected,
        "packageSelection": project["packageSelection"],
        "packagesToWrite": selected,
        "status": project["status"],
        "updatedAt": now,
        "changed": ["writingPackages", "packageSelection", "packagesToWrite", "status"],
        "event": "package.confirmed",
    }


def apply_fields(workspace: Path, project_dir: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """回填 project.json F3 字段（内部实现，供 CLI 与 --selftest 复用）。"""
    workspace = Path(workspace).resolve()
    project_dir = Path(project_dir).resolve()
    project_file = project_dir / "project.json"
    if not project_file.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            f"项目缺少 project.json：{project_dir.name}",
            hint="先执行 bg_workspace.py init-project 创建项目",
            detail={"project": str(project_dir)},
        )
    project = jsonio.read_json(project_file)
    if not isinstance(project, dict):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "project.json 必须是 JSON 对象")
    schema.raise_if_errors(schema.validate_project(project), filename="project.json")

    if not isinstance(payload, Mapping):
        raise errors.BgError(errors.E_USAGE, "data 必须是 JSON 对象", hint='形如 {"projectNo": "...", ...}')
    if isinstance(payload.get("fields"), Mapping):
        fields = dict(payload["fields"])
    else:
        fields = dict(payload)
    if not fields:
        raise errors.BgError(errors.E_USAGE, "没有要回填的字段", hint=f"允许字段：{list(FIELD_WHITELIST)}")
    unknown = [key for key in fields if key not in FIELD_WHITELIST]
    if unknown:
        raise errors.BgError(
            errors.E_USAGE,
            f"含不允许回填的字段：{unknown}",
            hint=f"F3 只允许回填 {list(FIELD_WHITELIST)}；projectId/bidderRef/state 等由其他脚本维护",
            detail={"unknown": unknown, "allowed": list(FIELD_WHITELIST)},
        )

    candidate = copy.deepcopy(project)
    warnings: list[str] = []
    changed: list[str] = []
    for key in FIELD_WHITELIST:
        if key not in fields:
            continue
        value = fields[key]
        if key == "sourceFiles":
            normalized, source_warnings = normalize_source_files(project_dir, value)
            candidate[key] = normalized
            warnings.extend(source_warnings)
        else:
            candidate[key] = value
        changed.append(key)

    # 分包含义一致性
    packages_note = ""
    if isinstance(candidate.get("packageCount"), int) and not isinstance(candidate.get("packageCount"), bool):
        package_count = candidate["packageCount"]
        if package_count < 1:
            raise errors.BgError(errors.E_SCHEMA_INVALID, "packageCount 必须 >=1", detail={"field": "packageCount"})
        if "packagesToWrite" not in fields:
            derived = _derive_packages(package_count)
            if candidate.get("packagesToWrite") != derived:
                candidate["packagesToWrite"] = derived
                warnings.append(f"packagesToWrite 已按 packageCount 自动生成：{derived}")
            packages_note = f"packagesToWrite 未传入，已按 packageCount={package_count} 自动生成（F4 选包会覆盖）"
    if "packagesToWrite" in fields:
        explicit = candidate.get("packagesToWrite")
        if not explicit:
            packages_note = "packagesToWrite 显式为空：撰写包待 F4 确认，由 F4（bg_project.py select-packages）覆盖"
        else:
            packages_note = "packagesToWrite 为显式传入值；F4 选包后会被覆盖为所选子集"
    if candidate.get("isSubcontracted") is False:
        package_count = candidate.get("packageCount")
        if package_count not in (None, 1):
            raise errors.BgError(
                errors.E_SCHEMA_INVALID,
                "isSubcontracted=false 时 packageCount 必须为 1",
                hint="不分包项目只能有 1 个包；请用户确认分包结论",
                detail={"isSubcontracted": False, "packageCount": package_count},
            )
    package_warnings = _validate_packages(candidate, candidate.get("packageCount"))

    now = paths.iso_now()
    candidate["updatedAt"] = now
    schema.raise_if_errors(schema.validate_project(candidate), filename="project.json")
    jsonio.write_json_atomic(project_file, candidate)
    warnings.extend(package_warnings)
    if packages_note:
        warnings.append(packages_note)

    event_name = "project.fields_filled"
    try:
        event_log.log_event(
            workspace,
            event_name,
            actor="script",
            project=str(candidate.get("projectId") or ""),
            detail={
                "fields": changed,
                "projectDir": project_dir.name,
                "sourceFiles": len(candidate.get("sourceFiles") or []),
            },
        )
    except ValueError as exc:  # 事件白名单未登记时给出明确错误（不静默）
        raise errors.BgError(errors.E_UNEXPECTED, f"事件类型未登记：{exc}") from exc

    project_relative = (
        paths.rel_posix(workspace, project_file) if _is_within(workspace, project_file) else str(project_file)
    )
    return {
        "ok": True,
        "command": "project_fields",
        "projectId": str(candidate.get("projectId") or ""),
        "projectPath": project_relative,
        "changed": changed,
        "fields": {key: candidate.get(key) for key in FIELD_WHITELIST},
        "warnings": warnings,
        "packagesToWriteNote": packages_note,
        "updatedAt": now,
        "event": event_name,
    }


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# --selftest
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    """脚本自检：回填成功/自动 packages/hash、未知字段、类型错误、state 不被写、事件写入。"""
    import platform
    import shutil
    import tempfile

    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-project-selftest-"))
    try:
        workspace = tmp / "ws"
        (workspace / "dsh" / "logs").mkdir(parents=True)
        project_dir = workspace / "示例项目-20261109230000-abcd"
        project_dir.mkdir()
        now = paths.iso_now()
        bidder_ref = {
            "bidderId": "bdr-20261109230000-abcd",
            "dirName": "示例投标人-20261109230000-abcd",
            "displayName": "示例投标人",
            "snapshotAt": now,
        }
        project = schema.initialize_project_document(
            project_id="prj-20261109230000-abcd",
            bidder_ref=bidder_ref,
            workspace_fingerprint=paths.workspace_fingerprint(workspace),
            now_iso=now,
        )
        project_file = project_dir / "project.json"
        jsonio.write_json_atomic(project_file, project)
        state_snapshot = b'{"sentinel": "must not be touched"}\n'
        state_file = project_dir / "state.json"
        state_file.write_bytes(state_snapshot)
        raw_dir = project_dir / "sources" / "raw"
        raw_dir.mkdir(parents=True)
        raw_file = raw_dir / "招标文件.docx"
        raw_file.write_bytes(b"fake-docx-content")
        result = apply_fields(
            workspace,
            project_dir,
            {
                "fields": {
                    "projectNo": "TEST-2026-001",
                    "projectName": "示例项目",
                    "isSubcontracted": False,
                    "packageCount": 1,
                    "sourceFiles": [
                        {
                            "originalName": "招标文件.docx",
                            "rawPath": "sources\\raw\\招标文件.docx",
                            "convertedPath": "sources/01-bidding-documents.md",
                            "kind": "bidding_document",
                        }
                    ],
                }
            },
        )
        updated = jsonio.read_json(project_file)
        expected_hash = "sha256:" + hashlib.sha256(b"fake-docx-content").hexdigest()
        checks.append("fill")
        if (
            updated.get("projectNo") != "TEST-2026-001"
            or updated.get("projectName") != "示例项目"
            or updated.get("isSubcontracted") is not False
            or updated.get("packageCount") != 1
            or updated.get("packagesToWrite") != ["package-1"]
        ):
            raise AssertionError(f"字段回填不正确：{updated}")
        if updated.get("sourceFiles") != [
            {
                "originalName": "招标文件.docx",
                "rawPath": "sources/raw/招标文件.docx",
                "convertedPath": "sources/01-bidding-documents.md",
                "kind": "bidding_document",
                "hash": expected_hash,
            }
        ]:
            raise AssertionError(f"sourceFiles 规范化/自动 hash 不正确：{updated.get('sourceFiles')}")
        checks.append("source_files_hash")
        # F-03：路径基准是项目目录（不是工作区）；带项目目录名前缀时 warning 要给出可操作提示
        _, prefix_warnings = normalize_source_files(
            project_dir,
            [
                {
                    "originalName": "招标文件.docx",
                    "rawPath": f"{project_dir.name}/sources/raw/招标文件.docx",
                    "convertedPath": f"{project_dir.name}/sources/01-bidding-documents.md",
                }
            ],
        )
        if not any("项目目录名前缀" in str(item) for item in prefix_warnings):
            raise AssertionError(f"带项目目录名前缀时未给出提示：{prefix_warnings}")
        _, valid_warnings = normalize_source_files(
            project_dir,
            [
                {
                    "originalName": "招标文件.docx",
                    "rawPath": "sources/raw/招标文件.docx",
                    "convertedPath": "sources/01-bidding-documents.md",
                }
            ],
        )
        if any("rawPath" in str(item) or "convertedPath" in str(item) for item in valid_warnings):
            raise AssertionError(f"合法项目目录相对路径不应有路径 warning：{valid_warnings}")
        checks.append("source_path_scope")
        if state_file.read_bytes() != state_snapshot:
            raise AssertionError("state.json 被改写（必须只写 project.json）")
        checks.append("state_untouched")
        events = event_log.read_events(workspace)
        if not any(record.get("event") == "project.fields_filled" for record in events):
            raise AssertionError("缺少 project.fields_filled 事件")
        if sorted(result.get("changed") or []) != ["isSubcontracted", "packageCount", "projectName", "projectNo", "sourceFiles"]:
            raise AssertionError(f"changed 不正确：{result.get('changed')}")
        checks.append("event")

        def expect_error(code: str, fields: dict[str, Any]) -> None:
            try:
                apply_fields(workspace, project_dir, fields)
            except errors.BgError as exc:
                if exc.code != code:
                    raise AssertionError(f"期望 {code}，实际 {exc.code}：{exc.message}") from exc
                return
            raise AssertionError(f"期望 {code}，实际未报错：{fields}")

        expect_error(errors.E_USAGE, {"projectId": "prj-20261109230000-abcd"})
        expect_error(errors.E_SCHEMA_INVALID, {"packageCount": "1"})
        expect_error(errors.E_SCHEMA_INVALID, {"isSubcontracted": False, "packageCount": 2})
        expect_error(
            errors.E_SCHEMA_INVALID,
            {"sourceFiles": [{"originalName": "a.docx", "rawPath": "C:/x/a.docx", "convertedPath": "sources/a.md"}]},
        )
        expect_error(errors.E_SCHEMA_INVALID, {"sourceFiles": [{"rawPath": "sources/raw/a.docx", "convertedPath": "sources/a.md"}]})
        checks.append("negative")

        # P3-14：显式 packagesToWrite=[] 允许（由 F4 覆盖）；子集允许；不传仍自动生成；越界/重复仍拒绝
        empty_result = apply_fields(workspace, project_dir, {"fields": {"isSubcontracted": True, "packageCount": 5, "packagesToWrite": []}})
        updated_empty = jsonio.read_json(project_file)
        if (
            updated_empty.get("packageCount") != 5
            or updated_empty.get("packagesToWrite") != []
            or not any("F4" in str(item) and "覆盖" in str(item) for item in empty_result.get("warnings") or [])
            or "F4" not in str(empty_result.get("packagesToWriteNote") or "")
        ):
            raise AssertionError(f"packagesToWrite=[] 未被接受：{empty_result.get('warnings')} / {updated_empty.get('packagesToWrite')}")
        checks.append("packages_empty_allowed")
        subset_result = apply_fields(workspace, project_dir, {"fields": {"packagesToWrite": ["package-2", "package-4"]}})
        updated_subset = jsonio.read_json(project_file)
        if updated_subset.get("packagesToWrite") != ["package-2", "package-4"] or not any(
            "子集" in str(item) for item in subset_result.get("warnings") or []
        ):
            raise AssertionError(f"packagesToWrite 子集未被接受：{subset_result.get('warnings')}")
        checks.append("packages_subset_allowed")
        derived_result = apply_fields(workspace, project_dir, {"fields": {"packageCount": 2}})
        updated_derived = jsonio.read_json(project_file)
        if updated_derived.get("packagesToWrite") != ["package-1", "package-2"] or not any(
            "自动生成" in str(item) for item in derived_result.get("warnings") or []
        ):
            raise AssertionError(f"不传 packagesToWrite 未自动生成：{updated_derived.get('packagesToWrite')}")
        checks.append("packages_auto_derived")
        expect_error(errors.E_SCHEMA_INVALID, {"packagesToWrite": ["package-9"]})
        expect_error(errors.E_SCHEMA_INVALID, {"packagesToWrite": ["package-1", "package-1"]})
        expect_error(errors.E_SCHEMA_INVALID, {"packagesToWrite": ["package-2", "package-1"]})
        checks.append("packages_negative")
        if state_file.read_bytes() != state_snapshot:
            raise AssertionError("state.json 在失败路径被改写")
        return {
            "ok": True,
            "selftest": {
                "script": "bg_project.py",
                "python": platform.python_version(),
                "checks": checks,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 自检失败统一退出码 1
        raise errors.BgError(
            errors.E_UNEXPECTED,
            f"bg_project.py 自检失败：{exc}",
            hint="查看 checks 判断失败环节；自检使用临时目录，不影响真实项目",
            detail={"checks": checks},
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造 CLI（M3.1 G1：`fields` 子命令）。"""
    parser = errors.CliParser(
        prog="bg_project.py",
        description="BidGenie F3 project.json 字段回填（白名单 + schema 校验 + 原子写 + 事件）",
    )
    errors.add_common_args(parser)
    parser.add_argument("action", nargs="?", choices=("fields", "select-packages"), default="fields", help="子命令：fields / select-packages")
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--data", help="字段 JSON 文件路径（{...} 或 {fields:{...}}）")
    parser.add_argument("--packages", help="select-packages：逗号分隔的包号或 package-N（如 2,4）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    """按参数分发。"""
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    if getattr(args, "action", "fields") == "select-packages":
        if not args.packages:
            raise errors.BgError(errors.E_USAGE, "缺少 --packages", hint="--packages 2,4")
        workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
        return select_packages(workspace, project_dir, args.packages)
    if not args.data:
        raise errors.BgError(
            errors.E_USAGE,
            "缺少 --data",
            hint='传入字段 JSON 文件；允许字段：' + ", ".join(FIELD_WHITELIST),
        )
    data_path = Path(str(args.data))
    if not data_path.is_absolute():
        data_path = (Path.cwd() / data_path).resolve()
    payload = jsonio.read_json(data_path)
    if not isinstance(payload, Mapping):
        raise errors.BgError(errors.E_USAGE, f"data 必须是 JSON 对象：{data_path}")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    return apply_fields(workspace, project_dir, payload)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（返回退出码）。"""
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_project.py",
    )


if __name__ == "__main__":
    sys.exit(main())
