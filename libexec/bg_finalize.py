#!/usr/bin/env python3
"""BidGenie F7.2 定稿锁定（M8-P2）。

--lock   校验 finalize 阶段/outline 后，计算 chapters md + outline.json + resources/** 的 SHA-256，
         写 `export/_frozen/manifest.json`、`_frozen/snapshot/` 快照与 `chapters/.locked`；
         通过 `_lib/state.py` 写 `locks.draftLocked/lockedAt/lockReason`。
--verify 比对当前文件哈希与 manifest；被改动/缺失 → E_VALIDATE_FAILED（exit4）。
--unlock 仅允许 `--force --reason`：归档 _frozen、删除 .locked、解除锁定并回到 optimize（R4）。
--status 只读报告锁定/冻结清单状态。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, logging as event_log, paths, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = "1.0"
FROZEN_DIR = "export/_frozen"
MANIFEST_FILE = "export/_frozen/manifest.json"
LOCK_MARKER = "chapters/.locked"
SNAPSHOT_DIR = "export/_frozen/snapshot"
EXCLUDED_PARTS = {"_outline", "_results", "_frozen", "resources", "export"}


# ---------------------------------------------------------------------------
# 哈希/清单
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_clean_freeze_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith((".lock", ".tmp", ".bak")) or name.startswith("~$") or name.endswith("~"):
        return False
    if any(part in {"_results", "_frozen"} for part in path.parts):
        return False
    return True


def collect_files(package_dir: Path) -> list[Path]:
    """章节 md + outline.json + resources/**（不含 _results/_frozen/export、*.lock/.tmp/.bak）。"""
    collected: list[Path] = []
    chapters_dir = package_dir / "chapters"
    if chapters_dir.is_dir():
        for path in sorted(chapters_dir.rglob("*.md")):
            if path.is_file() and _is_clean_freeze_file(path.relative_to(package_dir)):
                collected.append(path)
        outline_path = chapters_dir / "_outline" / "outline.json"
        if outline_path.is_file():
            collected.append(outline_path)
    resources_dir = package_dir / "resources"
    if resources_dir.is_dir():
        for path in sorted(resources_dir.rglob("*")):
            if path.is_file() and _is_clean_freeze_file(path.relative_to(package_dir)):
                collected.append(path)
    return collected


def build_manifest(package_dir: Path, package_id: str | None = None) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in collect_files(package_dir):
        relative = paths.rel_posix(package_dir, path)
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "type": "chapter" if relative.startswith("chapters/") and relative.endswith(".md") else ("outline" if relative.endswith("outline.json") else "resource"),
            }
        )
    files.sort(key=lambda item: item["path"])
    root_digest = hashlib.sha256()
    for item in files:
        root_digest.update(f"{item['path']}:{item['sha256']}\n".encode("utf-8"))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "packageId": package_id or package_dir.parent.name,
        "lockedAt": paths.iso_now(),
        "fileCount": len(files),
        "chapterCount": sum(1 for item in files if item["type"] == "chapter"),
        "resourceCount": sum(1 for item in files if item["type"] == "resource"),
        "rootHash": root_digest.hexdigest(),
        "files": files,
    }


def verify_manifest(package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "缺少定稿冻结清单 export/_frozen/manifest.json",
            hint="先运行 bg_finalize.py --lock",
            detail={"path": MANIFEST_FILE},
        )
    manifest = jsonio.read_json(manifest_path)
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "manifest.json 结构非法", detail={"path": MANIFEST_FILE})
    findings: list[dict[str, Any]] = []
    checked = 0
    for item in manifest["files"]:
        if not isinstance(item, Mapping):
            continue
        relative = str(item.get("path") or "")
        path = package_dir / relative
        expected = str(item.get("sha256") or "")
        if not path.is_file():
            findings.append({"path": relative, "error": "missing", "expectedSha256": expected})
            continue
        actual = sha256_file(path)
        checked += 1
        if actual != expected:
            findings.append({"path": relative, "error": "modified", "expectedSha256": expected, "actualSha256": actual})
    return {
        "ok": not findings,
        "manifestPath": MANIFEST_FILE,
        "fileCount": len(manifest["files"]),
        "checked": checked,
        "rootHash": manifest.get("rootHash"),
        "lockedAt": manifest.get("lockedAt"),
        "findings": findings,
    }


def _snapshot(package_dir: Path, manifest: Mapping[str, Any], snapshot_dir: Path) -> int:
    copied = 0
    for item in manifest.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        relative = str(item.get("path") or "")
        if not (relative.startswith("chapters/") and (relative.endswith(".md") or relative.endswith("outline.json"))):
            continue
        source = package_dir / relative
        target = snapshot_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


# ---------------------------------------------------------------------------
# 锁定前置校验
# ---------------------------------------------------------------------------
def _outline_issues(package_dir: Path, package_meta: Mapping[str, Any]) -> list[str]:
    outline_path = outline_validate.outline_path(package_dir)
    if not outline_path.is_file():
        return ["缺少 chapters/_outline/outline.json"]
    try:
        outline = jsonio.read_json(outline_path)
    except errors.BgError as exc:
        return [f"outline.json 不可解析：{exc.message}"]
    validation = outline_validate.validate_document(outline, package_meta, require_layout=True)
    issues = [f"{item.get('code')}: {item.get('message')}" for item in validation.get("blockers") or []]
    if validation.get("warnings") and not issues:
        # 警告不阻止锁定，但保留在调用结果中
        pass
    for node in outline_validate.iter_preorder(outline.get("chapters") or []):
        if isinstance(node.get("children"), list) and node.get("children"):
            continue
        completion = node.get("completion") if isinstance(node.get("completion"), Mapping) else {}
        status = str(completion.get("status") or "")
        if status not in ("done", "skipped"):
            issues.append(f"章节 {node.get('id')} completion.status={status or '未设置'}（须 done/skipped）")
    return issues


def cleanup_exports(*, workspace: Path, project_dir: Path, package_id: str | None, keep: int = 5) -> dict[str, Any]:
    """清理旧导出/lock/tmp：保留最近 keep 份 docx 与 _frozen 归档，不动当前 manifest/docx。"""
    _workspace_path, _project_dir_path, target, package_dir, _meta, _state, _project = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    export_dir = package_dir / "export"
    removed: list[str] = []
    kept: list[str] = []
    if export_dir.is_dir():
        docx_files = sorted((path for path in export_dir.glob("*.docx") if path.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in docx_files[keep:]:
            removed.append(paths.rel_posix(package_dir, path))
            path.unlink()
        kept = [paths.rel_posix(package_dir, path) for path in docx_files[:keep]]
        for path in sorted(export_dir.rglob("*")):
            if path.is_file() and (path.name.endswith((".lock", ".tmp", ".bak")) or path.name.startswith("~$")):
                removed.append(paths.rel_posix(package_dir, path))
                path.unlink()
    archives = sorted(package_dir.glob("export/_frozen-archive-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in archives[keep:]:
        removed.append(paths.rel_posix(package_dir, path))
        shutil.rmtree(path, ignore_errors=True)
    outline_dir = package_dir / "chapters" / "_outline"
    if outline_dir.is_dir():
        backups = sorted(outline_dir.glob("*.bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in backups[keep:]:
            removed.append(paths.rel_posix(package_dir, path))
            path.unlink(missing_ok=True)
        for path in outline_dir.glob("*"):
            if path.is_file() and (path.name.endswith((".lock", ".tmp")) or path.name.startswith("~$")):
                removed.append(paths.rel_posix(package_dir, path))
                path.unlink(missing_ok=True)
    return {"ok": True, "command": "finalize_cleanup", "packageId": target, "keep": keep, "removed": removed, "kept": kept}


def lock_project(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    stage = str(state.get("currentStage") or "")
    if stage != "finalize":
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            f"仅 finalize 阶段可定稿锁定，当前 {stage}",
            hint="先完成 T7 审查通过并迁移到 finalize",
            detail={"currentStage": stage},
        )
    lock_marker = package_dir / LOCK_MARKER
    manifest_path = package_dir / MANIFEST_FILE
    locks = state.get("locks") if isinstance(state.get("locks"), Mapping) else {}
    if lock_marker.exists() or manifest_path.is_file() or locks.get("draftLocked"):
        verification = verify_manifest(package_dir)
        if not verification.get("ok"):
            raise errors.BgError(
                errors.E_LOCKED,
                "项目已锁定但冻结清单与当前文件不一致",
                hint="运行 --verify 查看被改动文件；必要时 --unlock --force --reason 后重新锁定",
                detail={"findings": verification.get("findings")},
            )
        if not locks.get("draftLocked"):
            state_lib.lock_draft(project_dir_path, reason=str(locks.get("lockReason") or reason), manifest_path=paths.rel_posix(project_dir_path, package_dir / MANIFEST_FILE), root_hash=str(verification.get("rootHash") or ""), actor=actor, package_id=target)
        return {
            "ok": True,
            "command": "finalize_lock",
            "alreadyLocked": True,
            "packageId": target,
            "currentStage": stage,
            "manifestPath": MANIFEST_FILE,
            "fileCount": verification.get("fileCount"),
            "rootHash": verification.get("rootHash"),
            "lockedAt": verification.get("lockedAt"),
        }
    issues = _outline_issues(package_dir, package_meta)
    if issues:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "定稿前 outline/章节完整性校验失败",
            hint="修复 blockers 后再锁定；--verify 用于锁定后校验",
            detail={"findings": issues[:30]},
        )
    manifest = build_manifest(package_dir, target)
    manifest["lockReason"] = reason
    manifest["lockedBy"] = actor
    frozen_dir = package_dir / FROZEN_DIR
    frozen_dir.mkdir(parents=True, exist_ok=True)
    jsonio.write_json_atomic(frozen_dir / "manifest.json", manifest)
    snapshot_count = _snapshot(package_dir, manifest, package_dir / SNAPSHOT_DIR)
    marker_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "lockedAt": manifest["lockedAt"],
        "lockReason": reason,
        "manifest": MANIFEST_FILE,
        "rootHash": manifest["rootHash"],
    }
    jsonio.write_json_atomic(lock_marker, marker_payload)
    new_state = state_lib.lock_draft(
        project_dir_path,
        reason=reason,
        manifest_path=paths.rel_posix(project_dir_path, package_dir / MANIFEST_FILE),
        root_hash=manifest["rootHash"],
        actor=actor,
        package_id=target,
        locked_at=manifest["lockedAt"],
    )
    project_id = str(new_state.get("projectId") or "")
    try:
        event_log.log_event(
            workspace_path,
            "export.frozen",
            actor="script",
            project=project_id,
            package=target,
            detail={"packageId": target, "manifestPath": MANIFEST_FILE, "rootHash": manifest["rootHash"], "fileCount": manifest["fileCount"]},
        )
        event_log.log_event(
            workspace_path,
            "lock.locked",
            actor="script",
            project=project_id,
            package=target,
            detail={"packageId": target, "lockedAt": manifest["lockedAt"], "lockReason": reason},
        )
    except ValueError:
        pass
    return {
        "ok": True,
        "command": "finalize_lock",
        "alreadyLocked": False,
        "packageId": target,
        "currentStage": new_state.get("currentStage"),
        "manifestPath": MANIFEST_FILE,
        "fileCount": manifest["fileCount"],
        "chapterCount": manifest["chapterCount"],
        "resourceCount": manifest["resourceCount"],
        "rootHash": manifest["rootHash"],
        "lockedAt": manifest["lockedAt"],
        "snapshotCount": snapshot_count,
        "lockMarker": LOCK_MARKER,
        "revision": new_state.get("revision"),
    }


def verify_project(*, workspace: Path, project_dir: Path, package_id: str | None) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    verification = verify_manifest(package_dir)
    if not verification.get("ok"):
        raise errors.BgError(
            errors.E_VALIDATE_FAILED,
            f"冻结清单校验失败：{len(verification['findings'])} 个文件不一致",
            hint="被改动文件见 findings；锁定态禁止写正文，确认后 --unlock --force --reason 再修改",
            detail={"findings": verification["findings"], "manifestPath": MANIFEST_FILE},
        )
    try:
        event_log.log_event(
            workspace,
            "export.verified",
            actor="script",
            project=str(state.get("projectId") or ""),
            package=target,
            detail={"packageId": target, "fileCount": verification.get("fileCount"), "rootHash": verification.get("rootHash")},
        )
    except ValueError:
        pass
    return {
        "ok": True,
        "command": "finalize_verify",
        "packageId": target,
        "currentStage": state.get("currentStage"),
        "locked": bool((state.get("locks") or {}).get("draftLocked")),
        "manifestPath": MANIFEST_FILE,
        "fileCount": verification.get("fileCount"),
        "rootHash": verification.get("rootHash"),
        "lockedAt": verification.get("lockedAt"),
        "findings": [],
    }


def unlock_project(
    *, workspace: Path, project_dir: Path, package_id: str | None, reason: str, force: bool, actor: str
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    if not force:
        raise errors.BgError(
            errors.E_GUARD_FAILED,
            "解除定稿锁定必须显式 --force（R4，用户二次确认）",
            hint="--unlock --force --reason \"<原因>\"",
            detail={"locked": bool((state.get("locks") or {}).get("draftLocked"))},
        )
    if not reason.strip():
        raise errors.BgError(errors.E_USAGE, "解除锁定必须填写 --reason", hint="--unlock --force --reason \"<原因>\"")
    frozen_dir = package_dir / FROZEN_DIR
    archived = None
    if frozen_dir.exists():
        stamp = paths.timestamp14()
        archived_path = package_dir / "export" / f"_frozen-archive-{stamp}"
        suffix = 1
        while archived_path.exists():
            archived_path = package_dir / "export" / f"_frozen-archive-{stamp}-{suffix}"
            suffix += 1
        frozen_dir.rename(archived_path)
        archived = paths.rel_posix(package_dir, archived_path)
    lock_marker = package_dir / LOCK_MARKER
    if lock_marker.exists():
        lock_marker.unlink()
    new_state = state_lib.unlock_draft(project_dir_path, reason=reason, actor=actor)
    return {
        "ok": True,
        "command": "finalize_unlock",
        "packageId": target,
        "archivedFrozen": archived,
        "lockMarkerRemoved": not lock_marker.exists(),
        "currentStage": new_state.get("currentStage"),
        "currentScope": new_state.get("currentScope"),
        "locked": bool((new_state.get("locks") or {}).get("draftLocked")),
        "revision": new_state.get("revision"),
        "reason": reason,
    }


def status_project(*, workspace: Path, project_dir: Path, package_id: str | None) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    locks = state.get("locks") if isinstance(state.get("locks"), Mapping) else {}
    manifest_path = package_dir / MANIFEST_FILE
    manifest: Mapping[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = jsonio.read_json(manifest_path)
        except errors.BgError:
            manifest = {}
    verification: dict[str, Any] = {"ok": False, "findings": []}
    if manifest_path.is_file():
        try:
            verification = verify_manifest(package_dir)
        except errors.BgError:
            verification = {"ok": False, "findings": [{"error": "manifest_invalid"}]}
    return {
        "ok": True,
        "command": "finalize_status",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "currentStage": state.get("currentStage"),
        "currentScope": state.get("currentScope"),
        "locked": bool(locks.get("draftLocked")),
        "lockedAt": locks.get("lockedAt"),
        "lockReason": locks.get("lockReason") or "",
        "lockMarker": LOCK_MARKER,
        "lockMarkerExists": (package_dir / LOCK_MARKER).is_file(),
        "frozenExists": manifest_path.is_file(),
        "fileCount": manifest.get("fileCount"),
        "rootHash": manifest.get("rootHash"),
        "verifyOk": bool(verification.get("ok")),
        "verifyFindings": verification.get("findings") or [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_selftest() -> dict[str, Any]:
    import tempfile

    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bidgenie-finalize-selftest-") as tmp:
        package_dir = Path(tmp) / "package-1-20261111120000"
        (package_dir / "chapters" / "_outline").mkdir(parents=True)
        (package_dir / "chapters" / "1-1").mkdir(parents=True)
        (package_dir / "resources" / "mermaid").mkdir(parents=True)
        (package_dir / "chapters" / "1-1" / "1-1-01.md").write_text("# 章节\n\n正文\n", encoding="utf-8", newline="\n")
        (package_dir / "chapters" / "_outline" / "outline.json").write_text('{"schemaVersion":"1.0","chapters":[]}', encoding="utf-8", newline="\n")
        (package_dir / "resources" / "mermaid" / "fig-1-1-01.png").write_bytes(b"png-bytes")
        manifest = build_manifest(package_dir)
        if manifest["chapterCount"] != 1 or manifest["resourceCount"] != 1 or manifest["fileCount"] != 3:
            raise AssertionError(f"清单数量错误：{manifest}")
        checks.append("manifest")
        manifest_path = package_dir / MANIFEST_FILE
        manifest_path.parent.mkdir(parents=True)
        jsonio.write_json_atomic(manifest_path, manifest)
        if not verify_manifest(package_dir)["ok"]:
            raise AssertionError("初次 verify 失败")
        checks.append("verify_ok")
        (package_dir / "chapters" / "1-1" / "1-1-01.md").write_text("# 章节\n\n被篡改\n", encoding="utf-8", newline="\n")
        if verify_manifest(package_dir)["ok"]:
            raise AssertionError("篡改未被发现")
        checks.append("verify_tamper")
    return {"ok": True, "selftest": {"script": "bg_finalize.py", "checks": checks}}


def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_finalize.py",
        description="BidGenie F7.2 定稿锁定：lock/verify/unlock/status（冻结清单 SHA256 + chapters/.locked）",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>")
    action = parser.add_mutually_exclusive_group(required=False)
    action.add_argument("--lock", action="store_true", help="定稿锁定（写 manifest/.locked/state.locks）")
    action.add_argument("--verify", action="store_true", help="校验冻结清单（篡改 exit4）")
    action.add_argument("--unlock", action="store_true", help="解除锁定（需 --force --reason，归档 _frozen 并回 optimize）")
    action.add_argument("--status", action="store_true", help="只读锁定/清单状态")
    action.add_argument("--cleanup", action="store_true", help="清理旧导出/.lock/.tmp/_frozen 归档（保留 --keep 份）")
    parser.add_argument("--keep", type=int, default=5, help="cleanup 保留份数（默认 5）")
    parser.add_argument("--reason", default="", help="锁定/解锁原因（审计）")
    parser.add_argument("--force", action="store_true", help="解锁时必须显式确认（R4）")
    parser.add_argument("--actor", default="script:bg_finalize.py", help="操作者标识")
    parser.add_argument("--selftest", action="store_true", help="脚本自检")
    return parser


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if args.selftest:
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if args.lock:
        return lock_project(workspace=workspace, project_dir=project_dir, package_id=args.package, reason=args.reason or "用户确认定稿", actor=args.actor)
    if args.verify:
        return verify_project(workspace=workspace, project_dir=project_dir, package_id=args.package)
    if args.unlock:
        return unlock_project(workspace=workspace, project_dir=project_dir, package_id=args.package, reason=args.reason, force=bool(args.force), actor=args.actor)
    if args.status:
        return status_project(workspace=workspace, project_dir=project_dir, package_id=args.package)
    if args.cleanup:
        return cleanup_exports(workspace=workspace, project_dir=project_dir, package_id=args.package, keep=max(1, int(args.keep or 5)))
    raise errors.BgError(errors.E_USAGE, "缺少操作：--lock / --verify / --unlock / --status / --cleanup", hint="使用 --help 查看用法")


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_finalize.py",
    )


if __name__ == "__main__":
    sys.exit(main())
