"""工作区路径解析、目录命名与 slug 化（PRD 3.2 / 3.6 / 7.0）。

约定：
- 工作区路径由调用方传入绝对路径，统一 ``resolve()`` 后使用；
- 目录/文件命名规则唯一来源 PRD 3.6；Windows 非法字符与冒号必须替换（NTFS ADS 坑）；
- JSON 内相对路径一律 POSIX 风格（``/`` 分隔）。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import string
import unicodedata
from datetime import date, datetime
from pathlib import Path

from .errors import E_NOT_FOUND, E_USAGE, BgError

#: Windows 非法文件名字符（PRD 3.2；冒号必须替换）
ILLEGAL_CHARS = frozenset('<>:"/\\|?*')
#: Windows 保留设备名（PRD 3.2）
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
#: 单级目录/文件名最大字符数（PRD 3.2 要求 <= 80）
MAX_COMPONENT_LEN = 80
#: 投标人 slug 最大长度（PRD 3.6）
SLUG_MAX_LEN = 24
#: 投标人目录名：<slug>-<ts14>-<rand4>
DIR_NAME_RE = re.compile(r"^.+-\d{14}-[a-z0-9]{4}$")
#: 投标人 ID：bdr-<ts14>-<rand4>（PRD 7.0）；也接受调用方显式传入的安全 ID
BIDDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_RAND_ALPHABET = string.ascii_lowercase + string.digits


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now_local(now: datetime | None = None) -> datetime:
    """返回带本地时区偏移的当前时间（无入参时）。"""
    if now is None:
        return datetime.now().astimezone()
    return now


def timestamp14(now: datetime | None = None) -> str:
    """目录/文件名时间戳：本地时区 ``YYYYMMDDHHmmss``（PRD 3.2）。"""
    return now_local(now).strftime("%Y%m%d%H%M%S")


def date8(now: datetime | None = None) -> str:
    """日志文件日期段 ``YYYYMMDD``（PRD 7.9）。"""
    return now_local(now).strftime("%Y%m%d")


def iso_now(now: datetime | None = None) -> str:
    """JSON 时间字段：ISO 8601 带时区偏移（PRD 7.0，精确到秒）。"""
    return now_local(now).isoformat(timespec="seconds")


def today(now: datetime | None = None) -> date:
    """本地日期，用于证书有效期计算。"""
    return now_local(now).date()


def parse_iso_date(text: str) -> date | None:
    """解析 ``YYYY-MM-DD``；空值/非法值返回 None（不抛异常）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 随机与命名
# ---------------------------------------------------------------------------
def rand4() -> str:
    """4 位 ``[a-z0-9]`` 安全随机后缀（PRD 3.2）。"""
    return "".join(secrets.choice(_RAND_ALPHABET) for _ in range(4))


def slugify(name: str, *, max_len: int = SLUG_MAX_LEN) -> str:
    """投标人 slug：CJK 保留、非法字符与空白替换为 ``-``、合并连续 ``-``、去首尾、超长截断。

    截断时追加名称 hash 的前 4 位（PRD 3.6）。
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    chars: list[str] = []
    for ch in text:
        if ch in ILLEGAL_CHARS or ord(ch) < 0x20:
            chars.append("-")
        elif ch.isspace():
            chars.append("-")
        else:
            chars.append(ch)
    slug = re.sub(r"-{2,}", "-", "".join(chars))
    slug = slug.strip("- .")
    if not slug:
        slug = "bidder"
    if len(slug) > max_len:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:4]
        prefix = slug[: max(1, max_len - 5)].rstrip("- .")
        slug = f"{prefix}-{digest}"
    return slug[:max_len]


def safe_component(name: str) -> str:
    """把任意字符串转为安全的单级文件名/目录名（PRD 3.2）。"""
    text = unicodedata.normalize("NFC", str(name or ""))
    chars: list[str] = []
    for ch in text:
        if ch in ILLEGAL_CHARS or ord(ch) < 0x20:
            chars.append("-")
        else:
            chars.append(ch)
    cleaned = re.sub(r"-{2,}", "-", "".join(chars)).strip("- .")
    if not cleaned:
        cleaned = "item"
    if cleaned.split(".")[0].upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:MAX_COMPONENT_LEN]


def new_bidder_dir_name(display_name: str, *, now: datetime | None = None) -> str:
    """``<bidderSlug>-<ts14>-<rand4>``（PRD 3.6）。"""
    return f"{slugify(display_name)}-{timestamp14(now)}-{rand4()}"


def new_bidder_id(now: datetime | None = None) -> str:
    """``bdr-<ts14>-<rand4>``（PRD 7.0 ID 规则）。"""
    return f"bdr-{timestamp14(now)}-{rand4()}"


def new_project_id(now: datetime | None = None) -> str:
    """``prj-<ts14>-<rand4>``（PRD 7.0 ID 规则）。"""
    return f"prj-{timestamp14(now)}-{rand4()}"


def is_bidder_dir_name(name: str) -> bool:
    """判断目录名是否符合 PRD 3.6 投标人目录规则。"""
    return bool(DIR_NAME_RE.match(name))


def is_valid_bidder_id(bidder_id: str) -> bool:
    """判断 bidderId 是否安全可落盘（非空、无路径分隔符/非法字符）。"""
    return bool(BIDDER_ID_RE.match(str(bidder_id or "")))


# ---------------------------------------------------------------------------
# 工作区与 dsh 路径
# ---------------------------------------------------------------------------
def normalize_workspace(raw: str | os.PathLike[str]) -> Path:
    """校验并规范化工作区绝对路径（必须已存在且是目录）。"""
    text = str(raw or "").strip().strip('"')
    if not text:
        raise BgError(E_USAGE, "--workspace 不能为空", hint="传入 DSH 工作区根的绝对路径")
    path = Path(text)
    if not path.is_absolute():
        raise BgError(
            E_USAGE,
            "workspace 必须是绝对路径",
            hint="传入工作区根目录的绝对路径，例如 D:\\projects\\demo",
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BgError(
            E_NOT_FOUND,
            f"工作区不存在：{text}",
            hint="先在 DSH 中添加该工作区，或检查路径拼写",
        ) from exc
    if not resolved.is_dir():
        raise BgError(E_NOT_FOUND, f"workspace 不是目录：{text}", hint="传入工作区根目录")
    return resolved


def dsh_root(workspace: Path) -> Path:
    """``<workspace>/dsh``。"""
    return workspace / "dsh"


def config_dir(workspace: Path) -> Path:
    return dsh_root(workspace) / "config"


def registry_path(workspace: Path) -> Path:
    return dsh_root(workspace) / "registry.json"


def user_settings_path(workspace: Path) -> Path:
    return config_dir(workspace) / "user-settings.json"


def bidders_dir(workspace: Path) -> Path:
    return dsh_root(workspace) / "bidders"


def trash_dir(workspace: Path) -> Path:
    return bidders_dir(workspace) / ".trash"


def backups_dir(workspace: Path) -> Path:
    return dsh_root(workspace) / "backups"


def logs_dir(workspace: Path) -> Path:
    return dsh_root(workspace) / "logs"


def locks_dir(workspace: Path) -> Path:
    return dsh_root(workspace) / "locks"


def config_file(workspace: Path, name: str) -> Path:
    """``dsh/config/<name>``（单级安全文件名）。"""
    return config_dir(workspace) / safe_component(name)


#: 项目工作目录创建/读取相关路径（F2 起使用；M1 只解析、不创建）
def project_dir(workspace: Path, dir_name: str) -> Path:
    """项目工作目录：``<workspace>/<projectDir>``（与 dsh/ 兄弟，PRD 3.4.1）。"""
    name = str(dir_name or "")
    if name in ("", ".", "..") or "/" in name or "\\" in name or ":" in name:
        raise BgError(E_USAGE, "项目目录名必须是单级目录名", detail={"dirName": name})
    return join_under(workspace, name)


def project_meta_path(workspace: Path, dir_name: str) -> Path:
    """``<projectDir>/project.json``。"""
    return project_dir(workspace, dir_name) / "project.json"


def workspace_fingerprint(workspace: Path) -> str:
    """工作区根指纹（PRD 7.6 registry.workspaceRootFingerprint；sha256 前 16 位）。"""
    normalized = os.path.normcase(str(workspace.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def rel_posix(root: Path, target: Path) -> str:
    """返回 target 相对 root 的 POSIX 风格相对路径。"""
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(target)


def join_under(root: Path, *parts: str) -> Path:
    """在 root 下安全拼接路径，拒绝绝对路径、``..`` 与 symlink 逃逸（PRD 8.9）。"""
    if not parts:
        return root
    rel = Path(*[str(p) for p in parts])
    if rel.is_absolute() or rel.drive:
        raise BgError(E_USAGE, "只允许工作区相对路径", hint="传入相对 dsh/ 的路径，不允许绝对路径")
    rel_text = rel.as_posix()
    if ":" in rel_text or rel_text.startswith("..") or "/../" in f"/{rel_text}/":
        raise BgError(E_USAGE, "路径越界（禁止 ..、盘符或冒号）", detail={"path": rel_text})
    try:
        resolved = (root / rel).resolve()
    except OSError as exc:
        raise BgError(E_USAGE, "路径无法解析", detail={"path": rel_text}) from exc
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BgError(E_USAGE, "路径越界（工作区之外）", detail={"path": rel_text}) from exc
    return resolved


def sanitize_rel_path(rel: str) -> str:
    """规范化记录进 JSON 的 POSIX 相对路径，拒绝逃逸写法。"""
    text = str(rel or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise BgError(E_USAGE, "相对路径中禁止 ..", detail={"path": text})
    return "/".join(parts)
