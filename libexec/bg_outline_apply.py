#!/usr/bin/env python3
"""BidGenie F5-1 大纲落盘（PRD F5.1.2–F5.1.9 / 7.4 / 4.3 T4）。

Agent 只写 `<packageDir>/chapters/_outline/outline.draft.json`；本脚本负责：
1) 读取草稿（或 --input），调用 `bg_outline_validate` 校验；
2) 计算全局 `seq`、`kind`、`dirPath`/`filePath`，按 F5.1.5 分配/校正 `wordTarget`
   （floor=800、单章上限 min(8000, W/2)、Σ=W），回写 `wordTargetBasis`；
3) 汇总 `figureIndex`/`stats`（图表配额对账）、补 `completion` 默认值、重算 `coveredBy`；
4) 备份旧 `outline.json` 后原子写；`--materialize` 建章节树/文件头；`--check-tree` 只读校验；
6) `--migrate-storage` 把 M5.1 前散落在包根的 outline 文件迁入 `chapters/_outline/`（保留备份，不改 state）。
5) 事件 `outline.generated` / `outline.materialized`。
"""

from __future__ import annotations

import argparse
import copy
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import errors, jsonio, paths, schema, state as state_lib
import bg_outline_validate as outline_validate

SCHEMA_VERSION = outline_validate.SCHEMA_VERSION
FLOOR_WORD = outline_validate.FLOOR_WORD
MAX_WORD = outline_validate.MAX_WORD
FIGURE_TYPES = outline_validate.FIGURE_TYPES
OUTLINE_FILE = outline_validate.OUTLINE_FILE
DRAFT_FILE = outline_validate.DRAFT_FILE
OUTLINE_DIR = outline_validate.OUTLINE_DIR


def outline_dir(package_dir: Path, *, create: bool = False) -> Path:
    return outline_validate.outline_dir(package_dir, create=create)


def outline_path(package_dir: Path) -> Path:
    return outline_validate.outline_path(package_dir)


def draft_path(package_dir: Path) -> Path:
    return outline_validate.draft_path(package_dir)


# ---------------------------------------------------------------------------
# 归一化 + 字数分配
# ---------------------------------------------------------------------------
def _default_completion() -> dict[str, Any]:
    return {
        "status": "pending",
        "wordCount": 0,
        "updatedAt": "",
        "summary": "",
        "skipReason": "",
        "blockReason": "",
    }


def _default_review_state() -> dict[str, Any]:
    return {"coverage": "unreviewed", "lastRound": 0, "issueIds": []}


def _allocate_words(leaves: Sequence[dict[str, Any]], total: int) -> dict[str, list[Any]]:
    """F5.1.5 字数分配：floor/max 保护、按 scoreWeight share、Σ==total（尾差到最大权重章）。"""
    if not leaves:
        raise errors.BgError(errors.E_SCHEMA_INVALID, "大纲没有末级章节，无法分配字数")
    floor = FLOOR_WORD
    max_word = min(MAX_WORD, max(floor, int(total * 0.5)))
    if total < floor * len(leaves):
        raise errors.BgError(
            errors.E_SCHEMA_INVALID,
            f"总字数 {total} 不足以覆盖 {len(leaves)} 个末级章节的 floor={floor}",
            hint="减少章节或提高 expectedWordCount",
        )
    weights: list[float] = []
    for leaf in leaves:
        value = leaf.get("scoreWeight")
        weights.append(max(0.0, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0)
    weight_sum = sum(weights)
    if weight_sum <= 0:
        shares = [1.0 / len(leaves)] * len(leaves)
    else:
        shares = [weight / weight_sum for weight in weights]
    targets = [min(max_word, max(floor, total * share)) for share in shares]
    # 分配剩余（sum < total）给未触顶章节，按 share 比例
    for _ in range(100):
        remainder = total - sum(targets)
        if remainder <= 0:
            break
        candidates = [index for index, value in enumerate(targets) if value < max_word]
        if not candidates:
            break
        share_sum = sum(shares[index] for index in candidates)
        moved = 0
        for index in candidates:
            capacity = max_word - targets[index]
            portion = remainder * (shares[index] / share_sum) if share_sum else remainder / len(candidates)
            add = min(capacity, portion)
            targets[index] += add
            moved += add
        if moved <= 1e-9:
            break
    # 若仍 sum > total（floor 过大/超上限导致），从大章节扣减
    for _ in range(100):
        excess = sum(targets) - total
        if excess <= 1e-9:
            break
        candidates = sorted(range(len(targets)), key=lambda index: targets[index], reverse=True)
        moved = 0
        for index in candidates:
            if targets[index] <= floor:
                continue
            reduce = min(excess, targets[index] - floor)
            targets[index] -= reduce
            excess -= reduce
            moved += reduce
            if excess <= 1e-9:
                break
        if moved <= 1e-9:
            break
    # 取整到 100 的倍数，尾差调到 scoreWeight 最大的章节，保证 Σ==total
    int_targets = [max(floor, min(max_word, int(round(value / 100.0)) * 100)) for value in targets]
    # P1-2：高 scoreWeight 章节不得低于低权重章节；按权重排序后单调分配同组值
    weight_order = sorted(range(len(int_targets)), key=lambda index: (weights[index], index))
    ordered_values = sorted(int_targets[index] for index in weight_order)
    monotonic_targets = [0] * len(int_targets)
    for position, index in enumerate(weight_order):
        monotonic_targets[index] = ordered_values[position]
    int_targets = monotonic_targets
    diff = total - sum(int_targets)
    order = sorted(
        range(len(int_targets)),
        key=lambda index: (weights[index], int_targets[index]),
        reverse=True,
    )
    while diff != 0:
        moved = False
        for index in order:
            if diff > 0 and int_targets[index] < max_word:
                add = min(100, diff, max_word - int_targets[index])
                int_targets[index] += add
                diff -= add
                moved = True
            elif diff < 0 and int_targets[index] > floor:
                reduce = min(100, -diff, int_targets[index] - floor)
                int_targets[index] -= reduce
                diff += reduce
                moved = True
            if diff == 0:
                break
        if not moved:
            break
    return {
        "targets": int_targets,
        "shares": [round(share, 6) for share in shares],
        "weights": weights,
        "sum": int(sum(int_targets)),
        "floor": floor,
        "maxWord": max_word,
    }


def normalize_outline(
    document: Mapping[str, Any],
    *,
    package_meta: Mapping[str, Any],
    project_name: str = "",
    package_id: str = "",
) -> dict[str, Any]:
    """把草稿归一化为可落盘 outline（seq/路径/字数/figureIndex/stats/completion/coveredBy）。"""
    if not isinstance(document, Mapping):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "outline 草稿必须是 JSON 对象")
    doc = copy.deepcopy(dict(document))
    target = package_id or str(package_meta.get("packageId") or "")
    expected_word = package_meta.get("expectedWordCount")
    doc.setdefault("schemaVersion", SCHEMA_VERSION)
    doc.setdefault("packageId", target)
    doc.setdefault("title", f"{project_name}技术方案" if project_name else "技术方案")
    doc.setdefault("generatedAt", paths.iso_now())
    doc["totalWordTarget"] = int(expected_word) if isinstance(expected_word, int) and not isinstance(expected_word, bool) else int(doc.get("totalWordTarget") or 0)
    doc.setdefault("criterionIndex", [])
    chapters = doc.get("chapters")
    if not isinstance(chapters, list):
        raise errors.BgError(errors.E_SCHEMA_INVALID, "chapters 必须是数组")
    doc["chapters"] = chapters
    doc.setdefault("extensions", {})
    layout = outline_validate.compute_layout(doc)
    leaves = layout["leaves"]
    total = int(doc.get("totalWordTarget") or 0)
    allocation = _allocate_words(leaves, total)
    for leaf, word, share, weight in zip(leaves, allocation["targets"], allocation["shares"], allocation["weights"]):
        leaf["wordTarget"] = int(word)
        leaf["wordTargetBasis"] = {
            "formula": "W*share floor=800 step=100",
            "share": share,
            "weight": round(float(weight), 6),
            "adjusted": int(word),
        }
    for node in layout["nodes"]:
        node.setdefault("priority", "P1")
        node["completion"] = node.get("completion") if isinstance(node.get("completion"), Mapping) else _default_completion()
        if "reviewState" not in node:
            node["reviewState"] = _default_review_state()
        if node.get("kind") == "parent":
            node["wordTarget"] = 0
            node["dirPath"] = node.get("dirPath")
            node["filePath"] = None
            node.setdefault("contentRequirements", [])
    # coveredBy 由末级引用重算
    covered: dict[str, list[str]] = {}
    for leaf in leaves:
        for ref in leaf.get("criterionRefs") or []:
            if isinstance(ref, Mapping) and isinstance(ref.get("criterionId"), str):
                covered.setdefault(str(ref["criterionId"]), []).append(str(leaf.get("id") or ""))
    for criterion in doc.get("criterionIndex") or []:
        if isinstance(criterion, dict):
            criterion["coveredBy"] = sorted(set(covered.get(str(criterion.get("criterionId")), [])))
    # 清理 compute_layout 的内部遍历键（含 _parent 引用，避免循环引用）
    for node in outline_validate.iter_preorder(doc.get("chapters") or []):
        for key in list(node.keys()):
            if key.startswith("_"):
                node.pop(key, None)
    doc["figureIndex"] = outline_validate.build_figure_index(doc)
    stats = outline_validate.build_stats(doc, package_meta)
    doc["stats"] = {
        "parentCount": stats["parentCount"],
        "leafCount": stats["leafCount"],
        "plannedWordTotal": stats["plannedWordTotal"],
        "figurePlanned": stats["figurePlanned"],
        "figureQuota": stats["figureQuota"],
    }
    return doc


# ---------------------------------------------------------------------------
# 备份 / 写入 / materialize
# ---------------------------------------------------------------------------
def backup_outline(package_dir: Path) -> str:
    outline = outline_path(package_dir)
    if not outline.is_file():
        return ""
    stamp = paths.timestamp14()
    target_dir = outline.parent
    candidate = target_dir / f"{OUTLINE_FILE}.bak-{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = target_dir / f"{OUTLINE_FILE}.bak-{stamp}-{suffix}"
        suffix += 1
    shutil.copy2(outline, candidate)
    return f"{OUTLINE_DIR}/{candidate.name}"


def migrate_outline_storage(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
) -> dict[str, Any]:
    """M5.1：把包根 legacy outline 文件移入 chapters/_outline/（保留迁移前 backup，不改 state/包目录）。"""
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    out_dir = outline_dir(package_dir, create=True)
    legacy_files: list[Path] = []
    for name in (OUTLINE_FILE, DRAFT_FILE, f"{OUTLINE_FILE}.lock"):
        candidate = package_dir / name
        if candidate.is_file():
            legacy_files.append(candidate)
    legacy_files.extend(sorted(package_dir.glob(f"{OUTLINE_FILE}.bak-*")))
    backup = ""
    legacy_outline = package_dir / OUTLINE_FILE
    if legacy_outline.is_file():
        stamp = paths.timestamp14()
        backup_path = out_dir / f"{OUTLINE_FILE}.bak-{stamp}-storage-migration"
        suffix = 1
        while backup_path.exists():
            backup_path = out_dir / f"{OUTLINE_FILE}.bak-{stamp}-storage-migration-{suffix}"
            suffix += 1
        shutil.copy2(legacy_outline, backup_path)
        backup = f"{OUTLINE_DIR}/{backup_path.name}"
    moved: list[str] = []
    skipped: list[str] = []
    for source in legacy_files:
        target_path = out_dir / source.name
        if target_path.exists():
            try:
                same = target_path.read_bytes() == source.read_bytes()
            except OSError:
                same = False
            if same:
                source.unlink()
                skipped.append(source.name)
                continue
            alt = out_dir / f"{source.name}.pre-migration-{paths.timestamp14()}"
            suffix = 1
            while alt.exists():
                alt = out_dir / f"{source.name}.pre-migration-{paths.timestamp14()}-{suffix}"
                suffix += 1
            shutil.move(str(source), str(alt))
            moved.append(f"{OUTLINE_DIR}/{alt.name}")
            continue
        shutil.move(str(source), str(target_path))
        moved.append(f"{OUTLINE_DIR}/{source.name}")
    outline_valid: bool | None = None
    if outline_path(package_dir).is_file():
        document = jsonio.read_json(outline_path(package_dir))
        result = outline_validate.validate_document(
            document,
            package_meta,
            require_layout=True,
            strict=True,
            check_tree=True,
            package_dir=package_dir,
            source=f"{OUTLINE_DIR}/{OUTLINE_FILE}",
        )
        outline_valid = bool(result["valid"])
    return {
        "ok": True,
        "command": "outline_migrate_storage",
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "packageDir": paths.rel_posix(project_dir_path, package_dir),
        "outlineDir": f"{paths.rel_posix(project_dir_path, package_dir)}/{OUTLINE_DIR}",
        "migrated": moved,
        "skippedSameContent": skipped,
        "backup": backup,
        "remainingLegacy": sorted(
            str(path.relative_to(package_dir).as_posix())
            for path in package_dir.glob(f"{OUTLINE_FILE}*")
            if path.is_file()
        ),
        "validAfterMigration": outline_valid,
        "currentStage": state.get("currentStage"),
        "revision": state.get("revision"),
        "note": "只移动 outline 元数据文件；state.json/包目录/章节树未变（M5.1）",
    }


def materialize_tree(document: Mapping[str, Any], package_dir: Path, *, force: bool = False) -> dict[str, list[str]]:
    """按 outline 建章节树：父级目录 + 末级 .md（首行标题、第二行 BG:CHAPTER meta）。"""
    created_dirs: list[str] = []
    created_files: list[str] = []
    skipped_files: list[str] = []
    overwritten_files: list[str] = []
    for node in outline_validate.iter_preorder(document.get("chapters") or []):
        is_leaf = not (isinstance(node.get("children"), list) and node.get("children"))
        if is_leaf:
            file_path = node.get("filePath")
            if not isinstance(file_path, str) or not file_path:
                continue
            target = package_dir / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            rel = file_path
            if target.parent.is_dir() and target.parent.relative_to(package_dir).as_posix() not in created_dirs:
                pass
            level = int(node.get("level") or 1)
            content = (
                f"{'#' * level} {node.get('title') or ''}\n"
                f"<!-- BG:CHAPTER id={node.get('id')} level={level} wordTarget={node.get('wordTarget')} seq={node.get('seq')} -->\n"
            )
            if target.is_file() and not force:
                skipped_files.append(rel)
                continue
            if target.is_file():
                overwritten_files.append(rel)
            else:
                created_files.append(rel)
            target.write_text(content, encoding="utf-8", newline="\n")
        else:
            dir_path = node.get("dirPath")
            if isinstance(dir_path, str) and dir_path:
                target_dir = package_dir / dir_path.rstrip("/")
                if not target_dir.is_dir():
                    target_dir.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(dir_path.rstrip("/"))
    return {
        "createdDirs": sorted(set(created_dirs)),
        "createdFiles": sorted(created_files),
        "skippedFiles": sorted(skipped_files),
        "overwrittenFiles": sorted(overwritten_files),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _validation_detail(result: Mapping[str, Any], source: str) -> dict[str, Any]:
    return {
        "command": "outline_apply",
        "source": source,
        "valid": False,
        "blockers": result.get("blockers") or [],
        "warnings": result.get("warnings") or [],
        "findings": result.get("findings") or [],
        "stats": result.get("stats") or {},
    }


def apply_outline(
    *,
    workspace: Path,
    project_dir: Path,
    package_id: str | None = None,
    input_path: str | None = None,
    materialize: bool = False,
    force: bool = False,
    check_tree_only: bool = False,
) -> dict[str, Any]:
    workspace_path, project_dir_path, target, package_dir, package_meta, state, project_doc = outline_validate.resolve_package_context(
        workspace, project_dir, package_id
    )
    project_name = str(project_doc.get("projectName") or "")
    outline_file = outline_path(package_dir)
    if check_tree_only:
        if not outline_file.is_file():
            raise errors.BgError(
                errors.E_NOT_FOUND,
                "缺少 outline.json，无法 --check-tree",
                hint="先运行 bg_outline_apply.py --materialize；旧项目先 --migrate-storage",
                detail={"packageDir": paths.rel_posix(project_dir_path, package_dir)},
            )
        document = jsonio.read_json(outline_file)
        result = outline_validate.validate_document(
            document,
            package_meta,
            require_layout=True,
            strict=True,
            check_tree=True,
            package_dir=package_dir,
            source=paths.rel_posix(project_dir_path, outline_file),
        )
        if not result["valid"]:
            raise errors.BgError(
                errors.E_VALIDATE_FAILED,
                f"文件树校验未通过：{len(result['blockers'])} 项 blocker",
                hint="修复 outline 与 chapters 后重试（Agent 只改 draft，脚本重建树）",
                detail=_validation_detail(result, paths.rel_posix(project_dir_path, outline_file)),
                exit_code=errors.EXIT_SCHEMA,
            )
        return {
            "ok": True,
            "command": "outline_apply",
            "mode": "check_tree",
            "projectId": project_doc.get("projectId"),
            "packageId": target,
            "packageDir": paths.rel_posix(project_dir_path, package_dir),
            "valid": True,
            "stats": result["stats"],
            "outlinePath": paths.rel_posix(project_dir_path, outline_file),
            "tree": {"checked": True},
            "currentStage": state.get("currentStage"),
            "revision": state.get("revision"),
        }

    source_path, draft_mode = outline_validate.resolve_input_path(package_dir, input_path)
    if source_path.parent == package_dir:
        raise errors.BgError(
            errors.E_NOT_FOUND,
            "大纲文件仍在包根旧位置（M5.1 前布局）",
            hint="先运行 bg_outline_apply.py --migrate-storage 迁移到 chapters/_outline/",
            detail={"legacyPath": source_path.name, "outlineDir": OUTLINE_DIR},
        )
    applied = False
    normalized: dict[str, Any] | None = None
    backup_name = ""
    materialize_result: dict[str, list[str]] | None = None
    if draft_mode or source_path != outline_path:
        draft = jsonio.read_json(source_path)
        normalized = normalize_outline(
            draft,
            package_meta=package_meta,
            project_name=project_name,
            package_id=target,
        )
        result = outline_validate.validate_document(
            normalized,
            package_meta,
            require_layout=True,
            strict=True,
            check_tree=False,
            package_dir=package_dir,
            source=paths.rel_posix(project_dir_path, source_path),
        )
        if not result["valid"]:
            raise errors.BgError(
                errors.E_VALIDATE_FAILED,
                f"大纲校验未通过：{len(result['blockers'])} 项 blocker",
                hint="按 blockers/findings 修正 outline.draft.json 后重试；未写入 outline.json",
                detail=_validation_detail(result, paths.rel_posix(project_dir_path, source_path)),
                exit_code=errors.EXIT_SCHEMA,
            )
        outline_validate.outline_dir(package_dir, create=True)
        backup_name = backup_outline(package_dir)
        jsonio.write_json_atomic(outline_validate.outline_path(package_dir), normalized)
        applied = True
        event_log_event(
            workspace_path,
            "outline.generated",
            project=str(project_doc.get("projectId") or ""),
            package=target,
            detail={
                "source": paths.rel_posix(project_dir_path, source_path),
                "parentCount": normalized["stats"]["parentCount"],
                "leafCount": normalized["stats"]["leafCount"],
                "plannedWordTotal": normalized["stats"]["plannedWordTotal"],
                "figurePlanned": normalized["stats"]["figurePlanned"],
                "backup": backup_name,
            },
        )
    else:
        # 已有 outline.json，没有草稿：只做校验/落树
        normalized = jsonio.read_json(outline_file)
        result = outline_validate.validate_document(
            normalized,
            package_meta,
            require_layout=True,
            strict=True,
            check_tree=False,
            package_dir=package_dir,
            source=paths.rel_posix(project_dir_path, outline_file),
        )
        if not result["valid"]:
            raise errors.BgError(
                errors.E_VALIDATE_FAILED,
                f"已有 outline.json 校验未通过：{len(result['blockers'])} 项 blocker",
                detail=_validation_detail(result, paths.rel_posix(project_dir_path, outline_file)),
                exit_code=errors.EXIT_SCHEMA,
            )
    if materialize:
        materialize_result = materialize_tree(normalized, package_dir, force=force)
        event_log_event(
            workspace_path,
            "outline.materialized",
            project=str(project_doc.get("projectId") or ""),
            package=target,
            detail={
                "createdFiles": len(materialize_result["createdFiles"]),
                "skippedFiles": len(materialize_result["skippedFiles"]),
                "overwrittenFiles": len(materialize_result["overwrittenFiles"]),
            },
        )
    payload = {
        "ok": True,
        "command": "outline_apply",
        "mode": "apply+materialize" if (applied and materialize) else ("apply" if applied else ("materialize" if materialize else "validate")),
        "projectId": project_doc.get("projectId"),
        "packageId": target,
        "packageDir": paths.rel_posix(project_dir_path, package_dir),
        "source": paths.rel_posix(project_dir_path, source_path),
        "draftMode": draft_mode,
        "applied": applied,
        "backup": backup_name,
        "outlinePath": paths.rel_posix(project_dir_path, outline_file),
        "stats": (normalized or {}).get("stats", {}),
        "currentStage": state.get("currentStage"),
        "revision": state.get("revision"),
    }
    if materialize_result is not None:
        payload["materialize"] = materialize_result
    return payload


def event_log_event(workspace: Path, name: str, *, project: str, package: str, detail: Mapping[str, Any]) -> None:
    """写事件日志（logging 白名单已在 _lib/logging.py 注册）。"""
    from _lib import logging as event_log

    event_log.log_event(workspace, name, actor="script", project=project, package=package, detail=detail)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = errors.CliParser(
        prog="bg_outline_apply.py",
        description="BidGenie F5-1 大纲落盘：校验草稿、计算 seq、分配字数、汇总图表、备份并原子写 outline.json、materialize 章节树",
    )
    errors.add_common_args(parser)
    parser.add_argument("--workspace", help="工作区根绝对路径")
    parser.add_argument("--project", help="项目目录（绝对/相对）或 projectId（配合 --workspace）")
    parser.add_argument("--package", help="目标包 package-<N>（缺省 state.currentPackage / packagesToWrite 首个）")
    parser.add_argument("--input", help="指定草稿文件（缺省 <packageDir>/outline.draft.json）")
    parser.add_argument("--materialize", action="store_true", help="按 outline 创建 chapters 文件夹/末级 .md（已存在不覆盖，除非 --force）")
    parser.add_argument("--force", action="store_true", help="materialize 时覆盖已存在章节文件")
    parser.add_argument("--check-tree", action="store_true", help="只读校验 chapters/** 与 outline.json 一致（materialize 后）")
    parser.add_argument("--migrate-storage", action="store_true", help="M5.1：把包根 legacy outline 文件迁入 chapters/_outline/（保留备份，不改 state）")
    parser.add_argument("--cleanup", action="store_true", help="清理 outline 备份/.lock/.tmp（保留 --keep 份）")
    parser.add_argument("--keep", type=int, default=5, help="cleanup 保留备份份数（默认 5）")
    parser.add_argument("--selftest", action="store_true", help="运行脚本自检")
    return parser


def cleanup_outline(*, workspace: Path, project_dir: Path, package_id: str | None, keep: int = 5) -> dict[str, Any]:
    """清理 outline 备份/lock/tmp：按修改时间保留最近 keep 份 .bak。"""
    _workspace, _project, target, package_dir, _meta, _state, _project_doc = outline_validate.resolve_package_context(workspace, project_dir, package_id)
    outline_dir = package_dir / "chapters" / "_outline"
    removed: list[str] = []
    kept: list[str] = []
    if outline_dir.is_dir():
        backups = sorted(outline_dir.glob("*.bak*"), key=lambda p: p.stat().st_mtime, reverse=True)
        kept = [paths.rel_posix(package_dir, path) for path in backups[:keep]]
        for path in backups[keep:]:
            removed.append(paths.rel_posix(package_dir, path))
            path.unlink()
        for path in sorted(outline_dir.glob("*")):
            if path.is_file() and (path.name.endswith((".lock", ".tmp")) or path.name.startswith("~$")):
                removed.append(paths.rel_posix(package_dir, path))
                path.unlink()
    return {"ok": True, "command": "outline_cleanup", "packageId": target, "keep": keep, "removed": removed, "kept": kept}


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "selftest", False):
        return run_selftest()
    if not args.project:
        raise errors.BgError(errors.E_USAGE, "缺少 --project", hint="传入项目目录或 projectId")
    workspace, project_dir = state_lib.resolve_project_context(args.project, args.workspace)
    if getattr(args, "cleanup", False):
        return cleanup_outline(workspace=workspace, project_dir=project_dir, package_id=args.package, keep=max(1, int(getattr(args, "keep", 5) or 5)))
    if getattr(args, "migrate_storage", False):
        return migrate_outline_storage(
            workspace=workspace,
            project_dir=project_dir,
            package_id=args.package,
        )
    return apply_outline(
        workspace=workspace,
        project_dir=project_dir,
        package_id=args.package,
        input_path=args.input,
        materialize=bool(args.materialize),
        force=bool(args.force),
        check_tree_only=bool(args.check_tree),
    )


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest_golden() -> dict[str, Any]:
    from bg_outline_validate import _golden_document

    return _golden_document()


def run_selftest() -> dict[str, Any]:
    checks: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bidgenie-outline-apply-selftest-"))
    try:
        workspace = tmp / "ws"
        for rel in ("dsh/logs", "dsh/config", "dsh/bidders"):
            (workspace / rel).mkdir(parents=True, exist_ok=True)
        project_dir = workspace / "示例项目-20261111100000-abcd"
        package_dir = project_dir / "packages" / "package-1-20261111100000-abcd"
        package_dir.mkdir(parents=True)
        for rel in ("chapters", "reviews", "resources", "export"):
            (package_dir / rel).mkdir()
        package_meta = {
            "schemaVersion": "1.0",
            "packageId": "package-1",
            "expectedWordCount": 50000,
            "figureQuota": {"table": 5, "mermaid": 5, "network": 0, "terminal": 5},
            "personnelPlan": "",
            "equipmentPlan": "",
            "schedulePlan": {"startDate": "", "deliveryDate": "", "constructionPlan": ""},
        }
        jsonio.write_json_atomic(package_dir / "package-meta.json", package_meta)
        # project.json/state.json（apply 的 context 解析需要）
        project_doc = {
            "schemaVersion": "1.0",
            "projectId": "prj-20261111100000-abcd",
            "projectNo": "M5-TEST-001",
            "projectName": "M5 大纲自检项目",
            "isSubcontracted": False,
            "packageCount": 1,
            "packagesToWrite": ["package-1"],
            "bidderRef": {"bidderId": "bdr-20261111100000-abcd", "dirName": "示例", "displayName": "示例", "snapshotAt": paths.iso_now()},
            "workspaceRootFingerprint": paths.workspace_fingerprint(workspace),
            "createdAt": paths.iso_now(),
            "updatedAt": paths.iso_now(),
        }
        jsonio.write_json_atomic(project_dir / "project.json", project_doc)
        now = paths.iso_now()
        state_doc = schema.initialize_state_document(project_id=project_doc["projectId"], now_iso=now)
        state_doc["currentStage"] = "outline"
        state_doc["currentScope"] = "package-1"
        state_doc["currentPackage"] = "package-1"
        state_doc["completedStages"] = [
            {"stage": "project_init", "scope": "project", "completedAt": now, "note": ""},
            {"stage": "info_extract", "scope": "project", "completedAt": now, "note": ""},
            {"stage": "package_prepare", "scope": "package-1", "completedAt": now, "note": ""},
        ]
        state_doc["revision"] = 4
        state_doc["updatedAt"] = now
        state_doc["updatedBy"] = "script:bg_state.py"
        state_lib.write_state(project_dir, state_doc)
        draft = _selftest_golden()
        outline_dir(package_dir, create=True)
        jsonio.write_json_atomic(draft_path(package_dir), draft)
        first = apply_outline(workspace=workspace, project_dir=project_dir, materialize=True)
        outline_file = outline_path(package_dir)
        if not outline_file.is_file():
            raise AssertionError("apply 未写 outline.json")
        outline = jsonio.read_json(outline_file)
        checks.append("apply")
        stats = outline["stats"]
        if stats["plannedWordTotal"] != 50000:
            raise AssertionError(f"字数合计不正确：{stats['plannedWordTotal']}")
        if outline["figureIndex"] and len(outline["figureIndex"]) != sum(outline["stats"]["figureQuota"].values()):
            raise AssertionError("figureIndex 数量与配额不符")
        if any(node["kind"] == "leaf" and node["wordTarget"] < FLOOR_WORD for node in outline_validate.iter_preorder(outline["chapters"])):
            raise AssertionError("有末级章节 wordTarget < floor")
        seqs = [node["seq"] for node in outline_validate.iter_preorder(outline["chapters"])]
        if seqs != list(range(1, len(seqs) + 1)):
            raise AssertionError(f"seq 非连续先序：{seqs}")
        checks.append("seq_word_figures")
        validation = outline_validate.validate_document(outline, package_meta, require_layout=True)
        if not validation["valid"]:
            raise AssertionError(f"落盘 outline 未通过 strict：{validation['blockers']}")
        checks.append("strict_valid")
        tree = validate_tree = outline_validate.validate_document(
            outline, package_meta, require_layout=True, check_tree=True, package_dir=package_dir
        )
        if not validate_tree["valid"]:
            raise AssertionError(f"materialize 后文件树校验失败：{validate_tree['blockers']}")
        checks.append("materialize")
        leaf = next(node for node in outline_validate.iter_preorder(outline["chapters"]) if node["kind"] == "leaf")
        leaf_file = package_dir / leaf["filePath"]
        lines = leaf_file.read_text(encoding="utf-8").splitlines()
        if not lines[0].startswith("#" * leaf["level"] + " ") or "BG:CHAPTER" not in lines[1]:
            raise AssertionError("章节文件头格式不正确")
        checks.append("tree_header")
        # 再次 apply：备份 + 内容稳定
        second = apply_outline(workspace=workspace, project_dir=project_dir, materialize=True)
        backups = list(outline_dir(package_dir).glob(OUTLINE_FILE + ".bak-*"))
        if not second.get("backup") or not backups:
            raise AssertionError("重复 apply 未备份旧 outline")
        if first["stats"] != second["stats"]:
            raise AssertionError("重复 apply 统计不稳定")
        checks.append("backup_idempotent")
        # --check-tree
        tree_payload = apply_outline(workspace=workspace, project_dir=project_dir, check_tree_only=True)
        if not tree_payload.get("valid"):
            raise AssertionError("--check-tree 未通过")
        checks.append("check_tree")
        # 负例：删文件 → materialize 补齐；破坏一级标题 → 校验失败
        leaf_file.unlink()
        apply_outline(workspace=workspace, project_dir=project_dir, materialize=True)
        if not leaf_file.is_file():
            raise AssertionError("materialize 未补齐缺失文件")
        checks.append("materialize_repair")
        bad_draft = copy.deepcopy(draft)
        bad_draft["chapters"][0]["children"][0]["contentRequirements"] = []
        jsonio.write_json_atomic(draft_path(package_dir), bad_draft)
        try:
            apply_outline(workspace=workspace, project_dir=project_dir)
        except errors.BgError as exc:
            if exc.code != errors.E_VALIDATE_FAILED:
                raise AssertionError(f"非法草稿错误码不正确：{exc.code}") from exc
        else:
            raise AssertionError("非法草稿未被拒绝")
        checks.append("invalid_draft_rejected")
        # M5.1：legacy 包根文件迁移
        jsonio.write_json_atomic(package_dir / OUTLINE_FILE, outline)
        jsonio.write_json_atomic(package_dir / DRAFT_FILE, draft)
        (package_dir / f"{OUTLINE_FILE}.bak-20260101000000").write_text("{}\n", encoding="utf-8")
        (package_dir / f"{OUTLINE_FILE}.lock").write_text("", encoding="utf-8")
        migration = migrate_outline_storage(workspace=workspace, project_dir=project_dir)
        if not migration.get("migrated") or not migration.get("backup") or migration.get("remainingLegacy"):
            raise AssertionError(f"迁移结果不正确：{migration}")
        if not outline_path(package_dir).is_file() or (package_dir / OUTLINE_FILE).exists():
            raise AssertionError("迁移后包根 outline 未清理/新位置缺失")
        checks.append("migrate_storage")
        return {"ok": True, "selftest": {"script": "bg_outline_apply.py", "checks": checks}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    return errors.run_cli(
        argv,
        parser=build_parser(),
        handler=handle,
        script_name="bg_outline_apply.py",
    )


if __name__ == "__main__":
    sys.exit(main())
