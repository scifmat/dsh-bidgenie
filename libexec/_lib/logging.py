"""事件日志 append-only JSONL 与敏感值掩码（PRD 7.9 / 8.9 / 8.10）。

- 路径：``dsh/logs/events-<YYYYMMDD>.jsonl``，一行一事件；
- 事件名白名单唯一来源 PRD 7.9（新增事件必须先改 PRD）；
- 日志只追加、不改写；敏感键（电话/邮箱/证件号/账号/证书编号等）一律掩码；
- 写日志失败默认不阻塞主流程（``strict=True`` 时才抛出）。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths as bgpaths
from .errors import BgError
from .jsonio import append_line, iter_lines

#: 事件类型白名单（PRD 7.9 v0.1）
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "workspace.initialized",
        "workspace.repaired",
        "bidder.created",
        "bidder.updated",
        "bidder.deleted",
        "bidder.current_changed",
        "bidder.incomplete",
        "env.checked",
        "env.degraded",
        "state.transition",
        "state.set_package",
        "project.initialized",
        "project.fields_filled",
        "project.resumed",
        "project.aborted",
        "convert.started",
        "convert.file",
        "convert.failed",
        "extract.validated",
        "extract.failed",
        "package.prepared",
        "package.confirmed",
        "package.answers_updated",
        "package.verified",
        "package.meta_compacted",
        "package.value_override",
        "outline.generated",
        "outline.validated",
        "outline.materialized",
        "chapter.started",
        "chapter.completed",
        "chapter.blocked",
        "chapter.failed",
        "chapter.skipped",
        "chapter.rework",
        "review.round_start",
        "review.round_end",
        "review.not_converged",
        "optimize.round_end",
        "export.frozen",
        "export.started",
        "export.verified",
        "export.failed",
        "lock.locked",
        "lock.unlocked",
        "state.transition",
        "state.migrate",
        "state.rework",
        "recover.started",
        "recover.resumed",
        "recover.failed",
        "script.error",
        "script.timeout",
    }
)

#: 需要掩码的键名（PRD 8.9：证件号/银行账号/联系人电话/邮箱/证书编号）
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "phone",
        "email",
        "idNumber",
        "idType",
        "unifiedSocialCreditCode",
        "accountNo",
        "certificateNo",
        "legalRepresentative",
    }
)
MASK = "***"


def mask_value(key: str, value: Any) -> Any:
    """单值掩码：敏感键的非空值替换为 ``***``。"""
    if key in SENSITIVE_KEYS and value not in (None, ""):
        return MASK
    if isinstance(value, Mapping):
        return mask_detail(value)
    if isinstance(value, (list, tuple)):
        return [mask_value(key, item) for item in value]
    return value


def mask_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """递归掩码事件 detail，避免敏感值落日志（PRD 8.9）。"""
    if not detail:
        return {}
    masked: dict[str, Any] = {}
    for key, value in detail.items():
        masked[str(key)] = mask_value(str(key), value)
    return masked


def events_path(workspace: str | Path, when: datetime | None = None) -> Path:
    """``dsh/logs/events-<YYYYMMDD>.jsonl``。"""
    return bgpaths.logs_dir(Path(workspace)) / f"events-{bgpaths.date8(when)}.jsonl"


def log_event(
    workspace: str | Path,
    event: str,
    *,
    actor: str = "script",
    source: str = "",
    project: str = "",
    package: str = "",
    detail: Mapping[str, Any] | None = None,
    result: str = "ok",
    strict: bool = False,
    when: datetime | None = None,
) -> Path | None:
    """追加一条事件日志；返回日志文件路径，失败时返回 None（strict 时抛出）。

    面板/宿主可通过环境变量注入身份：BIDGENIE_ACTOR / BIDGENIE_SOURCE；
    面板动作应为 actor=user、source=panel，脚本内部默认 script。
    """
    if event not in EVENT_TYPES:
        raise ValueError(f"未登记的事件类型：{event}（白名单见 PRD 7.9 / _lib/logging.py）")
    actor = os.environ.get("BIDGENIE_ACTOR") or actor
    source = os.environ.get("BIDGENIE_SOURCE") or source or "script"
    moment = bgpaths.now_local(when)
    record: dict[str, Any] = {
        "ts": bgpaths.iso_now(moment),
        "event": event,
        "actor": actor,
        "source": source,
        "project": project,
        "package": package,
        "detail": mask_detail(detail),
        "result": result,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path = events_path(workspace, moment)
    try:
        append_line(path, line)
        return path
    except (BgError, OSError, ValueError) as exc:
        if strict:
            raise
        print(f"[warn] 事件日志写入失败：{type(exc).__name__}", file=sys.stderr)
        return None


def read_events(workspace: str | Path) -> list[dict[str, Any]]:
    """读取全部事件日志（按文件名字典序，逐行解析，损坏行跳过）。"""
    directory = bgpaths.logs_dir(Path(workspace))
    records: list[dict[str, Any]] = []
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("events-*.jsonl")):
        for line in iter_lines(path):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def iter_events(workspace: str | Path) -> Iterator[dict[str, Any]]:
    """逐条迭代事件（生成器版 read_events）。"""
    yield from read_events(workspace)


def event_names(workspace: str | Path) -> Sequence[str]:
    """返回已记录事件名列表（便于自检/诊断）。"""
    return [str(record.get("event", "")) for record in read_events(workspace)]
