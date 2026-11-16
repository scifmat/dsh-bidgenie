"""PRD 第 7.7 节最小字段校验与默认值归一化（M1）。

规则来源：``plugin/CONTRACT.md`` 第 5 节（字段已冻结）+ PRD 7.0 / 7.7。
- 校验函数返回 ``Finding`` 列表，不抛异常；调用方聚合成 ``E_SCHEMA_INVALID``；
- 归一化函数补齐 ``schemaVersion``、空串、默认值，保留未知字段（PRD 7.0 round-trip）；
- 枚举一律小写 snake_case，空串表示「未填」，不强制非空（草稿友好）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import paths as bgpaths
from .errors import E_SCHEMA_INVALID, BgError

SCHEMA_VERSION = "1.0"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)$")

#: 枚举（PRD 7.7）
BIDDER_TYPES: tuple[str, ...] = ("legal_entity", "natural_person")
CERTIFICATE_CATEGORIES: tuple[str, ...] = (
    "qualification",
    "license",
    "management",
    "personnel",
    "other",
)
CERTIFICATE_STATUSES: tuple[str, ...] = ("valid", "expiring", "expired", "unknown")
PERFORMANCE_ROLES: tuple[str, ...] = ("main", "sub", "similar")
AWARD_LEVELS: tuple[str, ...] = ("national", "provincial", "municipal", "industry", "other")
PENALTY_TYPES: tuple[str, ...] = (
    "administrative_penalty",
    "litigation",
    "dishonesty",
    "other",
)
PENALTY_STATUSES: tuple[str, ...] = ("settled", "pending", "unknown")
INTERACTION_MODES: tuple[str, ...] = ("guided", "auto")
LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")
META_SOURCES: tuple[str, ...] = ("panel", "agent")
#: 阶段枚举（PRD 4.2，v0.1 冻结）
STAGES: tuple[str, ...] = (
    "project_init",
    "info_extract",
    "package_prepare",
    "outline",
    "draft",
    "review",
    "optimize",
    "finalize",
    "export",
    "completed",
    "aborted",
)
#: 项目级阶段（含 aborted 终态）
PROJECT_SCOPED_STAGES: tuple[str, ...] = ("project_init", "info_extract", "aborted")
#: 包级阶段（含 aborted 终态）
PACKAGE_SCOPED_STAGES: tuple[str, ...] = (
    "package_prepare",
    "outline",
    "draft",
    "review",
    "optimize",
    "finalize",
    "export",
    "completed",
    "aborted",
)
#: 活动阶段（可 abort / 可推进；终态 completed/aborted 除外）
ACTIVE_STAGES: tuple[str, ...] = tuple(
    stage for stage in STAGES if stage not in ("completed", "aborted")
)
PROJECT_ID_RE = re.compile(r"^prj-\d{14}-[a-z0-9]{4}$")
PACKAGE_ID_RE = re.compile(r"^package-\d+$")
SCOPE_RE = re.compile(r"^(project|package-\d+)$")
#: 证书 90 天内到期视为 expiring（PRD 7.7.2）
EXPIRING_WINDOW_DAYS = 90

#: 默认脱敏字段路径（PRD 7.7.1 示例）
DEFAULT_SENSITIVE_FIELDS: tuple[str, ...] = (
    "unifiedSocialCreditCode",
    "idNumber",
    "bank.accountNo",
    "contact.phone",
)

# ---------------------------------------------------------------------------
# 字段顺序（写盘键顺序稳定，便于 diff；PRD 3.2 / 7.0）
# ---------------------------------------------------------------------------
BIDDER_FIELD_ORDER: tuple[str, ...] = (
    "schemaVersion",
    "bidderId",
    "bidderType",
    "displayName",
    "legalName",
    "unifiedSocialCreditCode",
    "legalRepresentative",
    "idType",
    "idNumber",
    "registeredAddress",
    "officeAddress",
    "contact",
    "bank",
    "capital",
    "establishedDate",
    "businessScope",
    "writable",
    "sensitiveFields",
    "tags",
    "updatedAt",
)
BIDDER_OPTIONAL_STRINGS: tuple[str, ...] = (
    "legalName",
    "unifiedSocialCreditCode",
    "legalRepresentative",
    "idType",
    "idNumber",
    "registeredAddress",
    "officeAddress",
    "capital",
    "establishedDate",
    "businessScope",
)
#: 归一化写盘顺序：contact/bank 位于 officeAddress 与 capital 之间（PRD 7.7.1）
BIDDER_OPTIONAL_STRINGS_BEFORE_CONTACT: tuple[str, ...] = (
    "legalName",
    "unifiedSocialCreditCode",
    "legalRepresentative",
    "idType",
    "idNumber",
    "registeredAddress",
    "officeAddress",
)
BIDDER_OPTIONAL_STRINGS_AFTER_BANK: tuple[str, ...] = (
    "capital",
    "establishedDate",
    "businessScope",
)
CONTACT_KEYS: tuple[str, ...] = ("name", "title", "phone", "email")
BANK_KEYS: tuple[str, ...] = ("accountName", "bankName", "accountNo")

CERTIFICATE_ITEM_ORDER: tuple[str, ...] = (
    "certificateId",
    "name",
    "category",
    "issuer",
    "certificateNo",
    "level",
    "validFrom",
    "validTo",
    "status",
    "holder",
    "requiredFor",
    "attachmentRef",
    "notes",
)
PERFORMANCE_ITEM_ORDER: tuple[str, ...] = (
    "performanceId",
    "projectName",
    "clientName",
    "contractAmount",
    "period",
    "role",
    "scope",
    "attachmentRef",
    "notes",
)
PERFORMANCE_PERIOD_KEYS: tuple[str, ...] = ("start", "end")
AWARD_ITEM_ORDER: tuple[str, ...] = (
    "awardId",
    "name",
    "issuer",
    "awardedAt",
    "level",
    "description",
    "attachmentRef",
)
PENALTY_ITEM_ORDER: tuple[str, ...] = (
    "penaltyId",
    "type",
    "description",
    "authority",
    "occurredAt",
    "status",
    "attachmentRef",
)
META_ORDER: tuple[str, ...] = (
    "schemaVersion",
    "bidderId",
    "dirName",
    "completeness",
    "source",
    "createdAt",
    "updatedAt",
)


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    """一条校验发现；``severity=error`` 才影响 ``valid``。"""

    pointer: str
    message: str
    severity: str = "error"
    expected: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pointer": self.pointer,
            "message": self.message,
            "severity": self.severity,
        }
        if self.expected:
            payload["expected"] = self.expected
        return payload


def _err(
    findings: list[Finding], pointer: str, message: str, expected: str | None = None
) -> None:
    findings.append(Finding(pointer, message, "error", expected))


def _warn(
    findings: list[Finding], pointer: str, message: str, expected: str | None = None
) -> None:
    findings.append(Finding(pointer, message, "warning", expected))


def split_findings(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """拆分为 (errors, warnings)。"""
    errors = [item for item in findings if item.severity == "error"]
    warnings = [item for item in findings if item.severity != "error"]
    return errors, warnings


def raise_if_errors(findings: list[Finding], *, filename: str, hint: str = "") -> None:
    """有 error 级发现时抛 ``E_SCHEMA_INVALID``（附完整 findings）。"""
    errors, warnings = split_findings(findings)
    if not errors:
        return
    default_hint = "修正字段后重试；运行 python libexec/bg_bidder.py validate --json 查看完整 findings"
    raise BgError(
        E_SCHEMA_INVALID,
        f"{filename} 校验失败（{len(errors)} 项错误）",
        hint=hint or default_hint,
        detail={
            "file": filename,
            "findings": [item.to_dict() for item in errors],
            "warnings": [item.to_dict() for item in warnings],
        },
    )


# ---------------------------------------------------------------------------
# 基础校验工具
# ---------------------------------------------------------------------------
def _missing(value: Any) -> bool:
    return value is None


def _check_object(
    value: Any, pointer: str, findings: list[Finding], *, label: str, required: bool = True
) -> bool:
    if _missing(value):
        if required:
            _err(findings, pointer, f"缺少必填对象 {label}", "object")
        return False
    if not isinstance(value, Mapping):
        _err(findings, pointer, f"{label} 必须是 JSON 对象", "object")
        return False
    return True


def _check_str(
    value: Any,
    pointer: str,
    findings: list[Finding],
    *,
    required: bool = False,
    allow_empty: bool = True,
) -> bool:
    label = pointer.rsplit("/", 1)[-1]
    if _missing(value):
        if required:
            _err(findings, pointer, f"缺少必填字段 {label}", "string")
        return False
    if not isinstance(value, str):
        _err(findings, pointer, f"{label} 必须是字符串", "string")
        return False
    if not allow_empty and not value.strip():
        _err(findings, pointer, f"{label} 不能为空", "非空字符串")
        return False
    return True


def _check_bool(value: Any, pointer: str, findings: list[Finding]) -> None:
    if not _missing(value) and not isinstance(value, bool):
        _err(findings, pointer, f"{pointer.rsplit('/', 1)[-1]} 必须是布尔值", "boolean")


def _check_enum(
    value: Any,
    pointer: str,
    allowed: tuple[str, ...],
    findings: list[Finding],
    *,
    allow_empty: bool = True,
) -> None:
    if _missing(value):
        return
    if not isinstance(value, str):
        _err(findings, pointer, f"{pointer.rsplit('/', 1)[-1]} 必须是字符串枚举", "string")
        return
    if value == "" and allow_empty:
        return
    if value not in allowed:
        _err(
            findings,
            pointer,
            f"{pointer.rsplit('/', 1)[-1]} 取值非法：{value!r}",
            "|".join(allowed),
        )


def _check_list_of_str(value: Any, pointer: str, findings: list[Finding]) -> None:
    if _missing(value):
        return
    if not isinstance(value, list):
        _err(findings, pointer, f"{pointer.rsplit('/', 1)[-1]} 必须是数组", "array")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _err(findings, f"{pointer}/{index}", "数组元素必须是字符串", "string")


def _check_date_str(value: Any, pointer: str, findings: list[Finding]) -> None:
    if _missing(value) or value == "":
        return
    if not isinstance(value, str):
        _err(findings, pointer, f"{pointer.rsplit('/', 1)[-1]} 必须是 YYYY-MM-DD 字符串", "date")
        return
    if not _DATE_ONLY_RE.match(value):
        _err(
            findings,
            pointer,
            f"{pointer.rsplit('/', 1)[-1]} 必须是 YYYY-MM-DD 格式",
            "YYYY-MM-DD",
        )


def _check_updated_at(value: Any, pointer: str, findings: list[Finding], *, required: bool) -> None:
    if _missing(value):
        if required:
            _err(findings, pointer, f"缺少必填字段 {pointer.rsplit('/', 1)[-1]}", "ISO 8601")
        return
    if not isinstance(value, str) or not _ISO_RE.match(value):
        _err(
            findings,
            pointer,
            "时间字段必须为 ISO 8601 带时区偏移（如 2026-11-08T20:00:00+08:00）",
            "ISO 8601",
        )


def _check_schema_version(value: Any, findings: list[Finding]) -> None:
    if not _check_str(value, "/schemaVersion", findings, required=True):
        return
    if value != SCHEMA_VERSION:
        _err(
            findings,
            "/schemaVersion",
            f"schemaVersion 必须为 {SCHEMA_VERSION}（M1；读到更高版本应拒绝写入）",
            SCHEMA_VERSION,
        )


# ---------------------------------------------------------------------------
# bidder.json
# ---------------------------------------------------------------------------
def validate_bidder(data: Any) -> list[Finding]:
    """校验 ``bidder.json``（CONTRACT 第 5 节 / PRD 7.7.1）。"""
    findings: list[Finding] = []
    if not isinstance(data, Mapping):
        return [Finding("", "bidder.json 必须是 JSON 对象", "error", "object")]
    _check_schema_version(data.get("schemaVersion"), findings)

    bidder_id = data.get("bidderId")
    if _check_str(bidder_id, "/bidderId", findings, required=True, allow_empty=False):
        if not bgpaths.is_valid_bidder_id(str(bidder_id)):
            _err(
                findings,
                "/bidderId",
                "bidderId 只能包含字母/数字/下划线/连字符，长度 3–64",
                "bdr-<ts14>-<rand4>",
            )
    bidder_type = data.get("bidderType")
    if _check_str(bidder_type, "/bidderType", findings, required=True, allow_empty=False):
        _check_enum(bidder_type, "/bidderType", BIDDER_TYPES, findings, allow_empty=False)
    _check_str(data.get("displayName"), "/displayName", findings, required=True, allow_empty=False)

    for key in BIDDER_OPTIONAL_STRINGS:
        _check_str(data.get(key), f"/{key}", findings)
    _check_date_str(data.get("establishedDate"), "/establishedDate", findings)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)

    contact = data.get("contact")
    if _check_object(contact, "/contact", findings, label="contact", required=False):
        assert isinstance(contact, Mapping)
        for key in CONTACT_KEYS:
            _check_str(contact.get(key), f"/contact/{key}", findings)
    bank = data.get("bank")
    if _check_object(bank, "/bank", findings, label="bank", required=False):
        assert isinstance(bank, Mapping)
        for key in BANK_KEYS:
            _check_str(bank.get(key), f"/bank/{key}", findings)
    _check_bool(data.get("writable"), "/writable", findings)
    _check_list_of_str(data.get("sensitiveFields"), "/sensitiveFields", findings)
    _check_list_of_str(data.get("tags"), "/tags", findings)
    return findings


def normalize_bidder(
    raw: Mapping[str, Any], *, bidder_id: str, now_iso: str
) -> dict[str, Any]:
    """按 PRD 7.7.1 键顺序补齐默认值；保留未知字段。"""
    out: dict[str, Any] = {}
    out["schemaVersion"] = SCHEMA_VERSION
    out["bidderId"] = bidder_id
    out["bidderType"] = _plain(raw.get("bidderType"), "")
    display_name = _plain(raw.get("displayName"), "")
    out["displayName"] = display_name.strip() if isinstance(display_name, str) else display_name
    for key in BIDDER_OPTIONAL_STRINGS_BEFORE_CONTACT:
        out[key] = _plain(raw.get(key), "")
    out["contact"] = _normalize_sub_object(raw.get("contact"), CONTACT_KEYS)
    out["bank"] = _normalize_sub_object(raw.get("bank"), BANK_KEYS)
    for key in BIDDER_OPTIONAL_STRINGS_AFTER_BANK:
        out[key] = _plain(raw.get(key), "")
    if "writable" in raw:
        out["writable"] = raw["writable"]  # 非法类型保留，交给 validate 报错（不静默修复）
    else:
        out["writable"] = True
    if "sensitiveFields" in raw:
        sensitive = raw["sensitiveFields"]
        out["sensitiveFields"] = (
            [str(item) for item in sensitive] if isinstance(sensitive, list) else sensitive
        )
    else:
        out["sensitiveFields"] = list(DEFAULT_SENSITIVE_FIELDS)
    if "tags" in raw:
        tags = raw["tags"]
        out["tags"] = [str(item) for item in tags] if isinstance(tags, list) else tags
    else:
        out["tags"] = []
    out["updatedAt"] = now_iso
    _append_unknown(out, raw)
    return out


def merge_bidder_document(existing: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """把 patch 合并进既有 bidder 文档（contact/bank 逐键深合并，其余覆盖）。"""
    merged: dict[str, Any] = dict(existing)
    for key, value in patch.items():
        if key in ("contact", "bank") and isinstance(value, Mapping):
            base = merged.get(key)
            base_dict = dict(base) if isinstance(base, Mapping) else {}
            base_dict.update(value)
            merged[key] = base_dict
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# certificates.json
# ---------------------------------------------------------------------------
def validate_certificates(data: Any) -> list[Finding]:
    """校验 ``certificates.json``（PRD 7.7.2）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="certificates.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    items = data.get("items")
    if items is None:
        _err(findings, "/items", "缺少必填数组 items", "array")
        return findings
    if not isinstance(items, list):
        _err(findings, "/items", "items 必须是数组", "array")
        return findings
    for index, item in enumerate(items):
        pointer = f"/items/{index}"
        if not isinstance(item, Mapping):
            _err(findings, pointer, "证书条目必须是 JSON 对象", "object")
            continue
        _check_str(item.get("certificateId"), f"{pointer}/certificateId", findings)
        _check_str(item.get("name"), f"{pointer}/name", findings)
        _check_enum(item.get("category"), f"{pointer}/category", CERTIFICATE_CATEGORIES, findings)
        for key in ("issuer", "level", "holder", "attachmentRef", "notes", "certificateNo"):
            _check_str(item.get(key), f"{pointer}/{key}", findings)
        _check_date_str(item.get("validFrom"), f"{pointer}/validFrom", findings)
        _check_date_str(item.get("validTo"), f"{pointer}/validTo", findings)
        _check_enum(item.get("status"), f"{pointer}/status", CERTIFICATE_STATUSES, findings)
        _check_list_of_str(item.get("requiredFor"), f"{pointer}/requiredFor", findings)
    return findings


def normalize_certificates(
    data: Any, *, now_iso: str, reference_date: date | None = None
) -> dict[str, Any]:
    """归一化证书文件；``status`` 一律由脚本按 validTo 计算（PRD 7.7.2）。"""
    source = data if isinstance(data, Mapping) else {}
    raw_items = source.get("items")
    items: list[Any] = []
    if raw_items is not None and not isinstance(raw_items, list):
        out_invalid: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "items": raw_items,  # 非法类型保留 -> validate 报「items 必须是数组」
            "updatedAt": now_iso,
        }
        _append_unknown(out_invalid, source)
        return out_invalid
    for index, raw in enumerate(raw_items if isinstance(raw_items, list) else []):
        if not isinstance(raw, Mapping):
            items.append(raw)  # 非法条目保留 -> validate 报「必须是 JSON 对象」
            continue
        item_source = raw
        item: dict[str, Any] = {}
        certificate_id = _plain(item_source.get("certificateId"), "")
        item["certificateId"] = certificate_id or f"cert-{index + 1:03d}"
        for key in ("name", "category", "issuer", "certificateNo", "level"):
            item[key] = _plain(item_source.get(key), "")
        item["validFrom"] = _plain(item_source.get("validFrom"), "")
        item["validTo"] = _plain(item_source.get("validTo"), "")
        item["status"] = compute_certificate_status(item["validTo"], reference_date)
        holder = _plain(item_source.get("holder"), "")
        item["holder"] = holder or "company"
        required_for = item_source.get("requiredFor")
        if isinstance(required_for, list):
            item["requiredFor"] = [str(entry) for entry in required_for]
        elif required_for is not None:
            item["requiredFor"] = required_for  # 非法类型保留
        else:
            item["requiredFor"] = []
        item["attachmentRef"] = _plain(item_source.get("attachmentRef"), "")
        item["notes"] = _plain(item_source.get("notes"), "")
        _append_unknown(item, item_source)
        items.append(item)
    out: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "items": items,
        "updatedAt": now_iso,
    }
    _append_unknown(out, source)
    return out


def compute_certificate_status(valid_to: Any, reference_date: date | None = None) -> str:
    """按 validTo 与参考日期计算证书状态（PRD 7.7.2：90 天内到期为 expiring）。"""
    if not isinstance(valid_to, str) or not valid_to.strip():
        return "unknown"
    try:
        expiry = date.fromisoformat(valid_to.strip())
    except ValueError:
        return "unknown"
    ref = reference_date or bgpaths.today()
    delta = (expiry - ref).days
    if delta < 0:
        return "expired"
    if delta <= EXPIRING_WINDOW_DAYS:
        return "expiring"
    return "valid"


# ---------------------------------------------------------------------------
# performance.json
# ---------------------------------------------------------------------------
def validate_performance(data: Any) -> list[Finding]:
    """校验 ``performance.json``（PRD 7.7.3）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="performance.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    items = data.get("items")
    if items is None:
        _err(findings, "/items", "缺少必填数组 items", "array")
        return findings
    if not isinstance(items, list):
        _err(findings, "/items", "items 必须是数组", "array")
        return findings
    for index, item in enumerate(items):
        pointer = f"/items/{index}"
        if not isinstance(item, Mapping):
            _err(findings, pointer, "业绩条目必须是 JSON 对象", "object")
            continue
        _check_str(item.get("performanceId"), f"{pointer}/performanceId", findings)
        _check_str(item.get("projectName"), f"{pointer}/projectName", findings)
        for key in ("clientName", "contractAmount", "scope", "attachmentRef", "notes"):
            _check_str(item.get(key), f"{pointer}/{key}", findings)
        _check_enum(item.get("role"), f"{pointer}/role", PERFORMANCE_ROLES, findings)
        period = item.get("period")
        if _check_object(period, f"{pointer}/period", findings, label="period", required=False):
            assert isinstance(period, Mapping)
            _check_date_str(period.get("start"), f"{pointer}/period/start", findings)
            _check_date_str(period.get("end"), f"{pointer}/period/end", findings)
    return findings


def normalize_performance(data: Any, *, now_iso: str) -> dict[str, Any]:
    """归一化业绩文件。"""
    source = data if isinstance(data, Mapping) else {}
    raw_items = source.get("items")
    items: list[Any] = []
    if raw_items is not None and not isinstance(raw_items, list):
        out_invalid: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "items": raw_items,
            "updatedAt": now_iso,
        }
        _append_unknown(out_invalid, source)
        return out_invalid
    for index, raw in enumerate(raw_items if isinstance(raw_items, list) else []):
        if not isinstance(raw, Mapping):
            items.append(raw)
            continue
        item_source = raw
        item: dict[str, Any] = {}
        performance_id = _plain(item_source.get("performanceId"), "")
        item["performanceId"] = performance_id or f"perf-{index + 1:03d}"
        item["projectName"] = _plain(item_source.get("projectName"), "")
        item["clientName"] = _plain(item_source.get("clientName"), "")
        item["contractAmount"] = _plain(item_source.get("contractAmount"), "")
        item["period"] = _normalize_sub_object(item_source.get("period"), PERFORMANCE_PERIOD_KEYS)
        item["role"] = _plain(item_source.get("role"), "")
        item["scope"] = _plain(item_source.get("scope"), "")
        item["attachmentRef"] = _plain(item_source.get("attachmentRef"), "")
        item["notes"] = _plain(item_source.get("notes"), "")
        _append_unknown(item, item_source)
        items.append(item)
    out: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "items": items,
        "updatedAt": now_iso,
    }
    _append_unknown(out, source)
    return out


# ---------------------------------------------------------------------------
# awards-penalties.json
# ---------------------------------------------------------------------------
def validate_awards_penalties(data: Any) -> list[Finding]:
    """校验 ``awards-penalties.json``（PRD 7.7.4）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="awards-penalties.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    for section in ("awards", "penalties"):
        entries = data.get(section)
        if entries is None:
            _err(findings, f"/{section}", f"缺少必填数组 {section}", "array")
            continue
        if not isinstance(entries, list):
            _err(findings, f"/{section}", f"{section} 必须是数组", "array")
            continue
        for index, item in enumerate(entries):
            pointer = f"/{section}/{index}"
            if not isinstance(item, Mapping):
                _err(findings, pointer, "条目必须是 JSON 对象", "object")
                continue
            if section == "awards":
                _check_str(item.get("awardId"), f"{pointer}/awardId", findings)
                _check_str(item.get("name"), f"{pointer}/name", findings)
                _check_str(item.get("issuer"), f"{pointer}/issuer", findings)
                _check_date_str(item.get("awardedAt"), f"{pointer}/awardedAt", findings)
                _check_enum(item.get("level"), f"{pointer}/level", AWARD_LEVELS, findings)
                _check_str(item.get("description"), f"{pointer}/description", findings)
                _check_str(item.get("attachmentRef"), f"{pointer}/attachmentRef", findings)
            else:
                _check_str(item.get("penaltyId"), f"{pointer}/penaltyId", findings)
                _check_enum(item.get("type"), f"{pointer}/type", PENALTY_TYPES, findings)
                _check_str(item.get("description"), f"{pointer}/description", findings)
                _check_str(item.get("authority"), f"{pointer}/authority", findings)
                _check_date_str(item.get("occurredAt"), f"{pointer}/occurredAt", findings)
                _check_enum(item.get("status"), f"{pointer}/status", PENALTY_STATUSES, findings)
                _check_str(item.get("attachmentRef"), f"{pointer}/attachmentRef", findings)
    return findings


def normalize_awards_penalties(data: Any, *, now_iso: str) -> dict[str, Any]:
    """归一化奖惩文件。"""
    source = data if isinstance(data, Mapping) else {}
    award_docs: list[Any] = []
    raw_awards = source.get("awards")
    awards_value: Any = raw_awards if raw_awards is not None else []
    if isinstance(raw_awards, list):
        for index, raw in enumerate(raw_awards):
            if not isinstance(raw, Mapping):
                award_docs.append(raw)
                continue
            item_source = raw
            item: dict[str, Any] = {
                "awardId": _plain(item_source.get("awardId"), "") or f"awd-{index + 1:03d}",
                "name": _plain(item_source.get("name"), ""),
                "issuer": _plain(item_source.get("issuer"), ""),
                "awardedAt": _plain(item_source.get("awardedAt"), ""),
                "level": _plain(item_source.get("level"), ""),
                "description": _plain(item_source.get("description"), ""),
                "attachmentRef": _plain(item_source.get("attachmentRef"), ""),
            }
            _append_unknown(item, item_source)
            award_docs.append(item)
        awards_value = award_docs
    penalty_docs: list[Any] = []
    raw_penalties = source.get("penalties")
    penalties_value: Any = raw_penalties if raw_penalties is not None else []
    if isinstance(raw_penalties, list):
        for index, raw in enumerate(raw_penalties):
            if not isinstance(raw, Mapping):
                penalty_docs.append(raw)
                continue
            item_source = raw
            item = {
                "penaltyId": _plain(item_source.get("penaltyId"), "") or f"pen-{index + 1:03d}",
                "type": _plain(item_source.get("type"), ""),
                "description": _plain(item_source.get("description"), ""),
                "authority": _plain(item_source.get("authority"), ""),
                "occurredAt": _plain(item_source.get("occurredAt"), ""),
                "status": _plain(item_source.get("status"), ""),
                "attachmentRef": _plain(item_source.get("attachmentRef"), ""),
            }
            _append_unknown(item, item_source)
            penalty_docs.append(item)
        penalties_value = penalty_docs
    out: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "awards": awards_value,
        "penalties": penalties_value,
        "updatedAt": now_iso,
    }
    _append_unknown(out, source)
    return out


# ---------------------------------------------------------------------------
# meta.json / profile.md
# ---------------------------------------------------------------------------
def validate_meta(data: Any) -> list[Finding]:
    """校验 ``meta.json``（PRD 7.7.5）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="meta.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    _check_str(data.get("bidderId"), "/bidderId", findings, required=True, allow_empty=False)
    _check_str(data.get("dirName"), "/dirName", findings, required=True, allow_empty=False)
    completeness = data.get("completeness")
    if _check_object(completeness, "/completeness", findings, label="completeness"):
        assert isinstance(completeness, Mapping)
        for key in ("basic", "certificates", "awards", "overall"):
            value = completeness.get(key)
            if value is None:
                _err(findings, f"/completeness/{key}", f"缺少完整度字段 {key}", "number")
            elif not isinstance(value, (int, float)) or isinstance(value, bool):
                _err(findings, f"/completeness/{key}", f"{key} 必须是数字", "number")
            elif not 0.0 <= float(value) <= 1.0:
                _warn(findings, f"/completeness/{key}", f"{key} 超出 0–1 范围", "0.0-1.0")
    _check_enum(data.get("source"), "/source", META_SOURCES, findings, allow_empty=False)
    _check_updated_at(data.get("createdAt"), "/createdAt", findings, required=True)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)
    return findings


def normalize_meta(
    *,
    bidder_id: str,
    dir_name: str,
    completeness: Mapping[str, float],
    source: str,
    created_at: str,
    now_iso: str,
) -> dict[str, Any]:
    """构造 ``meta.json``（键顺序固定）。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "bidderId": bidder_id,
        "dirName": dir_name,
        "completeness": {
            "basic": float(completeness.get("basic", 0.0)),
            "certificates": float(completeness.get("certificates", 0.0)),
            "awards": float(completeness.get("awards", 0.0)),
            "overall": float(completeness.get("overall", 0.0)),
        },
        "source": source,
        "createdAt": created_at,
        "updatedAt": now_iso,
    }


# ---------------------------------------------------------------------------
# registry.json / user-settings.json
# ---------------------------------------------------------------------------
def validate_registry(data: Any) -> list[Finding]:
    """校验 ``registry.json``（CONTRACT 第 5 节 / PRD 7.6）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="registry.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    for section, required in (("bidders", True), ("projects", True)):
        entries = data.get(section)
        if entries is None:
            if required:
                _err(findings, f"/{section}", f"缺少必填数组 {section}", "array")
            continue
        if not isinstance(entries, list):
            _err(findings, f"/{section}", f"{section} 必须是数组", "array")
            continue
        if section == "bidders":
            for index, entry in enumerate(entries):
                pointer = f"/bidders/{index}"
                if not isinstance(entry, Mapping):
                    _err(findings, pointer, "投标人索引条目必须是 JSON 对象", "object")
                    continue
                _check_str(entry.get("bidderId"), f"{pointer}/bidderId", findings, required=True, allow_empty=False)
                dir_name = entry.get("dirName")
                if _check_str(dir_name, f"{pointer}/dirName", findings, required=True, allow_empty=False):
                    if not bgpaths.is_bidder_dir_name(str(dir_name)):
                        _warn(
                            findings,
                            f"{pointer}/dirName",
                            "目录名不符合 PRD 3.6 规则 <slug>-<ts14>-<rand4>",
                        )
                _check_str(entry.get("displayName"), f"{pointer}/displayName", findings, required=True, allow_empty=False)
                _check_enum(entry.get("bidderType"), f"{pointer}/bidderType", BIDDER_TYPES, findings)
                _check_updated_at(entry.get("updatedAt"), f"{pointer}/updatedAt", findings, required=True)
        else:  # projects（M2 起使用；字段按 CONTRACT §3 / PRD 7.6）
            for index, entry in enumerate(entries):
                pointer = f"/projects/{index}"
                if not isinstance(entry, Mapping):
                    _err(findings, pointer, "项目索引条目必须是 JSON 对象", "object")
                    continue
                _check_str(entry.get("projectId"), f"{pointer}/projectId", findings, required=True, allow_empty=False)
                dir_name = entry.get("dirName")
                if _check_str(dir_name, f"{pointer}/dirName", findings, required=True, allow_empty=False):
                    if not bgpaths.is_bidder_dir_name(str(dir_name)):
                        _warn(
                            findings,
                            f"{pointer}/dirName",
                            "项目目录名不符合 PRD 3.6 规则 <slug>-<ts14>-<rand4>",
                        )
                _check_str(entry.get("projectName"), f"{pointer}/projectName", findings)
                _check_str(entry.get("bidderId"), f"{pointer}/bidderId", findings)
                _check_enum(entry.get("currentStage"), f"{pointer}/currentStage", STAGES, findings)
                package = entry.get("currentPackage")
                if package is not None:
                    if not isinstance(package, str) or not PACKAGE_ID_RE.match(package):
                        _err(findings, f"{pointer}/currentPackage", "currentPackage 必须是 package-<N> 或 null", "package-<N>")
                _check_updated_at(entry.get("updatedAt"), f"{pointer}/updatedAt", findings, required=False)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)
    _check_str(data.get("workspaceRootFingerprint"), "/workspaceRootFingerprint", findings)
    _check_str(data.get("currentBidderId"), "/currentBidderId", findings)
    return findings


def validate_user_settings(data: Any) -> list[Finding]:
    """校验 ``user-settings.json``（CONTRACT 第 5 节 / PRD 7.6 S1–S15）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="user-settings.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    _check_str(data.get("currentBidderId"), "/currentBidderId", findings, required=True)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)

    tier = data.get("defaultWordCountTier")
    if tier is not None and (not isinstance(tier, int) or isinstance(tier, bool)):
        _err(findings, "/defaultWordCountTier", "defaultWordCountTier 必须是整数", "integer")
    quota = data.get("defaultFigureQuota")
    if quota is not None:
        if not isinstance(quota, Mapping):
            _err(findings, "/defaultFigureQuota", "defaultFigureQuota 必须是对象", "object")
        else:
            for key in ("table", "mermaid", "network", "terminal"):
                value = quota.get(key)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                    _err(findings, f"/defaultFigureQuota/{key}", f"{key} 必须是整数", "integer")
    _check_str(data.get("styleProfile"), "/styleProfile", findings)
    profiles = data.get("styleProfiles")
    if profiles is not None:
        if not isinstance(profiles, Mapping):
            _err(findings, "/styleProfiles", "styleProfiles 必须是对象", "object")
        else:
            for name in ("tech", "keyInfo", "overview"):
                value = profiles.get(name)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    _err(findings, f"/styleProfiles/{name}", f"{name} 必须是非空字符串", "non-empty string")
    _check_enum(data.get("interactionMode"), "/interactionMode", INTERACTION_MODES, findings)
    for key in ("maxReviewRounds", "maxParallelWriters"):
        value = data.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            _err(findings, f"/{key}", f"{key} 必须是整数", "integer")
    for key in ("allowNetworkImages", "allowAutoDefaults", "maskSensitive"):
        _check_bool(data.get(key), f"/{key}", findings)
    _check_enum(data.get("logLevel"), "/logLevel", LOG_LEVELS, findings)
    return findings


# ---------------------------------------------------------------------------
# 归一化辅助 / 完整度
# ---------------------------------------------------------------------------
def _plain(value: Any, default: Any) -> Any:
    """缺省值填充：None -> default，其余原样返回（类型错误交由校验报出）。"""
    return default if value is None else value


def _normalize_sub_object(raw: Any, keys: tuple[str, ...]) -> Any:
    """归一化固定子对象（contact/bank/period），保留未知键；非法类型原样保留交给校验。"""
    if raw is not None and not isinstance(raw, Mapping):
        return raw
    source = raw if isinstance(raw, Mapping) else {}
    out: dict[str, Any] = {}
    for key in keys:
        out[key] = _plain(source.get(key), "")
    _append_unknown(out, source)
    return out


def _append_unknown(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """把 source 中 target 未覆盖的键原样追加（PRD 7.0 未知字段 round-trip）。"""
    for key, value in source.items():
        if key not in target:
            target[key] = value


#: 基本信息完整度参与字段（路径形式，便于面板理解）
BASIC_COMPLETENESS_PATHS: tuple[str, ...] = (
    "displayName",
    "legalName",
    "unifiedSocialCreditCode",
    "legalRepresentative",
    "registeredAddress",
    "officeAddress",
    "contact.name",
    "contact.title",
    "contact.phone",
    "contact.email",
    "bank.accountName",
    "bank.bankName",
    "bank.accountNo",
    "capital",
    "establishedDate",
    "businessScope",
)


def compute_completeness(
    bidder: Mapping[str, Any],
    certificates: Mapping[str, Any],
    awards_penalties: Mapping[str, Any],
) -> dict[str, float]:
    """计算 meta.completeness（PRD 7.7.5）。

    公式（PRD 未给出数值，M1 固定为可解释的确定性权重，验收报告已记录）：
    - basic = 已填基本信息字段 / 16
    - certificates = min(1, 证书数 / 3)
    - awards = min(1, 获奖数 / 2)
    - overall = 0.5*basic + 0.3*certificates + 0.2*awards
    """
    filled = sum(1 for path in BASIC_COMPLETENESS_PATHS if _path_value(bidder, path))
    basic = round(filled / len(BASIC_COMPLETENESS_PATHS), 2)
    cert_items = certificates.get("items") if isinstance(certificates, Mapping) else None
    cert_count = len(cert_items) if isinstance(cert_items, list) else 0
    certificates_score = round(min(1.0, cert_count / 3.0), 2)
    award_items = awards_penalties.get("awards") if isinstance(awards_penalties, Mapping) else None
    award_count = len(award_items) if isinstance(award_items, list) else 0
    awards_score = round(min(1.0, award_count / 2.0), 2)
    overall = round(0.5 * basic + 0.3 * certificates_score + 0.2 * awards_score, 2)
    return {
        "basic": basic,
        "certificates": certificates_score,
        "awards": awards_score,
        "overall": overall,
    }


def incomplete_reasons(
    bidder: Mapping[str, Any],
    certificates: Mapping[str, Any],
    performance: Mapping[str, Any],
) -> list[str]:
    """返回资料缺口清单（用于 bidder.incomplete 事件；PRD F1.2 A.4）。"""
    missing: list[str] = []
    if not (certificates.get("items") if isinstance(certificates, Mapping) else None):
        missing.append("certificates")
    if not (performance.get("items") if isinstance(performance, Mapping) else None):
        missing.append("performance")
    if not str(bidder.get("businessScope") or "").strip():
        missing.append("profile")
    return missing


def _path_value(data: Mapping[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


# ---------------------------------------------------------------------------
# project.json（M2，PRD 7.1 / 4.5）
# ---------------------------------------------------------------------------
PROJECT_SOURCE_FILE_KEYS: tuple[str, ...] = (
    "originalName",
    "rawPath",
    "convertedPath",
    "kind",
    "hash",
)


def validate_project(data: Any) -> list[Finding]:
    """校验 ``project.json``（PRD 7.1 / 4.5；F2 只建键，F3/F4 回填）。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="project.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    project_id = data.get("projectId")
    if _check_str(project_id, "/projectId", findings, required=True, allow_empty=False):
        if not PROJECT_ID_RE.match(str(project_id)):
            _err(findings, "/projectId", "projectId 必须形如 prj-<ts14>-<rand4>", "prj-<ts14>-<rand4>")
    _check_str(data.get("projectNo"), "/projectNo", findings)
    _check_str(data.get("projectName"), "/projectName", findings)
    is_subcontracted = data.get("isSubcontracted")
    if is_subcontracted is not None and not isinstance(is_subcontracted, bool):
        _err(findings, "/isSubcontracted", "isSubcontracted 必须是 boolean 或 null", "boolean|null")
    package_count = data.get("packageCount")
    if package_count is not None:
        if not isinstance(package_count, int) or isinstance(package_count, bool) or package_count < 1:
            _err(findings, "/packageCount", "packageCount 必须是 >=1 的整数或 null", "integer|null")
    packages = data.get("packagesToWrite")
    if packages is not None:
        if not isinstance(packages, list):
            _err(findings, "/packagesToWrite", "packagesToWrite 必须是数组", "array")
        else:
            for index, package in enumerate(packages):
                if not isinstance(package, str) or not PACKAGE_ID_RE.match(package):
                    _err(findings, f"/packagesToWrite/{index}", "包标识必须形如 package-<N>", "package-<N>")
    writing_packages = data.get("writingPackages")
    if writing_packages is not None:
        if not isinstance(writing_packages, list):
            _err(findings, "/writingPackages", "writingPackages 必须是数组", "array")
        else:
            normalized: list[str] = []
            for index, package in enumerate(writing_packages):
                if not isinstance(package, str) or not PACKAGE_ID_RE.match(package):
                    _err(findings, f"/writingPackages/{index}", "包标识必须形如 package-<N>", "package-<N>")
                else:
                    normalized.append(package)
            if len(set(normalized)) != len(normalized):
                _err(findings, "/writingPackages", "writingPackages 不得重复", "unique")
            if isinstance(package_count, int) and not isinstance(package_count, bool):
                invalid = [package for package in normalized if int(package.split("-")[1]) > package_count]
                if invalid:
                    _err(findings, "/writingPackages", f"writingPackages 超出 packageCount={package_count}：{invalid}", "subset")
    selection = data.get("packageSelection")
    if selection is not None:
        if not isinstance(selection, Mapping):
            _err(findings, "/packageSelection", "packageSelection 必须是对象", "object")
        else:
            selected = selection.get("selected")
            if not isinstance(selected, list) or not selected:
                _err(findings, "/packageSelection/selected", "selected 必须是非空数组", "non-empty array")
            _check_str(selection.get("confirmedAt"), "/packageSelection/confirmedAt", findings)
            _check_str(selection.get("confirmedBy"), "/packageSelection/confirmedBy", findings)
    status = data.get("status")
    if status is not None and not isinstance(status, str):
        _err(findings, "/status", "status 必须是字符串", "string")
    bidder_ref = data.get("bidderRef")
    if _check_object(bidder_ref, "/bidderRef", findings, label="bidderRef"):
        assert isinstance(bidder_ref, Mapping)
        for key in ("bidderId", "dirName", "displayName"):
            _check_str(bidder_ref.get(key), f"/bidderRef/{key}", findings, required=True, allow_empty=False)
        _check_updated_at(bidder_ref.get("snapshotAt"), "/bidderRef/snapshotAt", findings, required=True)
    source_files = data.get("sourceFiles")
    if source_files is not None:
        if not isinstance(source_files, list):
            _err(findings, "/sourceFiles", "sourceFiles 必须是数组", "array")
        else:
            for index, entry in enumerate(source_files):
                if not isinstance(entry, Mapping):
                    _err(findings, f"/sourceFiles/{index}", "sourceFiles 条目必须是 JSON 对象", "object")
    key_info_export = data.get("keyInfoExport")
    if key_info_export is not None and not isinstance(key_info_export, Mapping):
        _err(findings, "/keyInfoExport", "keyInfoExport 必须是对象或 null", "object|null")
    _check_str(
        data.get("workspaceRootFingerprint"),
        "/workspaceRootFingerprint",
        findings,
        required=True,
        allow_empty=False,
    )
    _check_updated_at(data.get("createdAt"), "/createdAt", findings, required=True)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)
    extensions = data.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
        _err(findings, "/extensions", "extensions 必须是对象", "object")
    return findings


# ---------------------------------------------------------------------------
# state.json（M2，PRD 7.2 / 4.2 / 4.8 不变量）
# ---------------------------------------------------------------------------
def validate_state(data: Any) -> list[Finding]:
    """校验 ``state.json``（PRD 7.2）并检查 4.8 状态机不变量。"""
    findings: list[Finding] = []
    if not _check_object(data, "", findings, label="state.json"):
        return findings
    assert isinstance(data, Mapping)
    _check_schema_version(data.get("schemaVersion"), findings)
    _check_str(data.get("projectId"), "/projectId", findings, required=True, allow_empty=False)
    scope = data.get("currentScope")
    if _check_str(scope, "/currentScope", findings, required=True, allow_empty=False):
        if not SCOPE_RE.match(str(scope)):
            _err(findings, "/currentScope", "currentScope 必须是 project 或 package-<N>", "project|package-<N>")
    package = data.get("currentPackage")
    if package is not None:
        if not isinstance(package, str) or not PACKAGE_ID_RE.match(package):
            _err(findings, "/currentPackage", "currentPackage 必须是 package-<N> 或 null", "package-<N>|null")
    stage = data.get("currentStage")
    if _check_str(stage, "/currentStage", findings, required=True, allow_empty=False):
        _check_enum(stage, "/currentStage", STAGES, findings, allow_empty=False)
    completed = data.get("completedStages")
    seen: set[tuple[str, str]] = set()
    if completed is not None:
        if not isinstance(completed, list):
            _err(findings, "/completedStages", "completedStages 必须是数组", "array")
        else:
            for index, entry in enumerate(completed):
                pointer = f"/completedStages/{index}"
                if not isinstance(entry, Mapping):
                    _err(findings, pointer, "completedStages 条目必须是 JSON 对象", "object")
                    continue
                entry_stage = entry.get("stage")
                entry_scope = entry.get("scope")
                _check_enum(entry_stage, f"{pointer}/stage", STAGES, findings, allow_empty=False)
                _check_str(entry_scope, f"{pointer}/scope", findings, required=True, allow_empty=False)
                _check_updated_at(entry.get("completedAt"), f"{pointer}/completedAt", findings, required=True)
                _check_str(entry.get("note"), f"{pointer}/note", findings)
                if isinstance(entry_stage, str) and isinstance(entry_scope, str):
                    key = (entry_stage, entry_scope)
                    if key in seen:
                        _err(findings, pointer, "completedStages 内 (stage, scope) 组合重复（PRD 4.8.1）")
                    seen.add(key)
    pending = data.get("pendingInput")
    if pending is not None:
        if not isinstance(pending, Mapping):
            _err(findings, "/pendingInput", "pendingInput 必须是对象或 null", "object|null")
        else:
            _check_str(pending.get("kind"), "/pendingInput/kind", findings)
            _check_str(pending.get("callId"), "/pendingInput/callId", findings)
            question_ids = pending.get("questionIds")
            if question_ids is not None and not isinstance(question_ids, list):
                _err(findings, "/pendingInput/questionIds", "questionIds 必须是数组", "array")
            _check_updated_at(pending.get("askedAt"), "/pendingInput/askedAt", findings, required=False)
            _check_enum(pending.get("resumeStage"), "/pendingInput/resumeStage", STAGES, findings)
    abort = data.get("abort")
    if abort is not None:
        if not isinstance(abort, Mapping):
            _err(findings, "/abort", "abort 必须是对象或 null", "object|null")
        else:
            _check_str(abort.get("reason"), "/abort/reason", findings, required=True, allow_empty=False)
            _check_updated_at(abort.get("at"), "/abort/at", findings, required=True)
    locks = data.get("locks")
    if _check_object(locks, "/locks", findings, label="locks"):
        assert isinstance(locks, Mapping)
        _check_bool(locks.get("draftLocked"), "/locks/draftLocked", findings)
        if "draftLocked" not in locks:
            _err(findings, "/locks/draftLocked", "缺少 locks.draftLocked", "boolean")
        _check_updated_at(locks.get("lockedAt"), "/locks/lockedAt", findings, required=False)
        _check_str(locks.get("lockReason"), "/locks/lockReason", findings)
        if locks.get("draftLocked") is True and stage not in ("finalize", "export", "completed", "aborted"):
            _err(findings, "/locks/draftLocked", "draftLocked=true 时 currentStage 必须是 finalize/export/completed/aborted（PRD 4.8.4）")
    review = data.get("reviewLoop")
    if _check_object(review, "/reviewLoop", findings, label="reviewLoop"):
        assert isinstance(review, Mapping)
        round_value = review.get("round")
        if not isinstance(round_value, int) or isinstance(round_value, bool) or round_value < 0:
            _err(findings, "/reviewLoop/round", "reviewLoop.round 必须是 >=0 的整数", "integer")
        max_rounds = review.get("maxRounds")
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 1:
            _err(findings, "/reviewLoop/maxRounds", "reviewLoop.maxRounds 必须是 >=1 的整数", "integer")
        closed = review.get("closedRound")
        if closed is not None and (not isinstance(closed, int) or isinstance(closed, bool)):
            _err(findings, "/reviewLoop/closedRound", "closedRound 必须是整数或 null", "integer|null")
    summary = data.get("envCheckSummary")
    if summary is not None and not isinstance(summary, Mapping):
        _err(findings, "/envCheckSummary", "envCheckSummary 必须是对象或 null", "object|null")
    extensions = data.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
        _err(findings, "/extensions", "extensions 必须是对象", "object")
    history = data.get("history")
    if history is not None:
        if not isinstance(history, list):
            _err(findings, "/history", "history 必须是数组", "array")
        else:
            previous_revision = 0
            for index, entry in enumerate(history):
                pointer = f"/history/{index}"
                if not isinstance(entry, Mapping):
                    _err(findings, pointer, "history 条目必须是 JSON 对象", "object")
                    continue
                _check_updated_at(entry.get("at"), f"{pointer}/at", findings, required=True)
                _check_enum(entry.get("from"), f"{pointer}/from", STAGES, findings, allow_empty=False)
                _check_enum(entry.get("to"), f"{pointer}/to", STAGES, findings, allow_empty=False)
                _check_str(entry.get("scope"), f"{pointer}/scope", findings, required=True, allow_empty=False)
                _check_str(entry.get("actor"), f"{pointer}/actor", findings, required=True, allow_empty=False)
                _check_str(entry.get("reason"), f"{pointer}/reason", findings)
                entry_revision = entry.get("revision")
                if not isinstance(entry_revision, int) or isinstance(entry_revision, bool) or entry_revision < 1:
                    _err(findings, f"{pointer}/revision", "history.revision 必须是 >=1 的整数", "integer")
                elif entry_revision <= previous_revision:
                    _err(findings, f"{pointer}/revision", "history.revision 必须严格递增（PRD 4.8.6）")
                else:
                    previous_revision = entry_revision
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        _err(findings, "/revision", "revision 必须是 >=1 的整数", "integer")
    elif isinstance(history, list) and history:
        last = history[-1]
        if isinstance(last, Mapping) and isinstance(last.get("revision"), int) and last["revision"] > revision:
            _err(findings, "/revision", "revision 不能小于最后一条 history.revision", "integer")
    _check_updated_at(data.get("createdAt"), "/createdAt", findings, required=True)
    _check_updated_at(data.get("updatedAt"), "/updatedAt", findings, required=True)
    _check_str(data.get("updatedBy"), "/updatedBy", findings, required=True, allow_empty=False)

    # ---- PRD 4.8 不变量 ----
    if isinstance(scope, str) and isinstance(stage, str):
        if scope == "project" and stage not in PROJECT_SCOPED_STAGES:
            _err(
                findings,
                "/currentStage",
                f"currentScope=project 时 currentStage 只能是 {list(PROJECT_SCOPED_STAGES)}（PRD 4.8.3）",
            )
        if scope.startswith("package-") and stage not in PACKAGE_SCOPED_STAGES:
            _err(
                findings,
                "/currentStage",
                f"currentScope={scope} 时 currentStage 只能是包级阶段（PRD 4.8.3）",
            )
        if scope == "project" and package is not None:
            _err(findings, "/currentPackage", "currentScope=project 时 currentPackage 必须为 null")
        if scope.startswith("package-") and isinstance(package, str) and package != scope:
            _err(findings, "/currentPackage", "currentPackage 必须与 currentScope 一致")
    return findings


def initialize_project_document(
    *,
    project_id: str,
    bidder_ref: Mapping[str, Any],
    workspace_fingerprint: str,
    now_iso: str,
    project_name: str = "",
) -> dict[str, Any]:
    """构造 F2 初始 ``project.json``（只建键、值置空，PRD 4.5/7.1）。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "projectId": project_id,
        "projectNo": "",
        "projectName": project_name,
        "isSubcontracted": None,
        "packageCount": None,
        "packagesToWrite": [],
        "writingPackages": [],
        "packageSelection": None,
        "status": "",
        "bidderRef": {
            "bidderId": str(bidder_ref.get("bidderId") or ""),
            "dirName": str(bidder_ref.get("dirName") or ""),
            "displayName": str(bidder_ref.get("displayName") or ""),
            "snapshotAt": str(bidder_ref.get("snapshotAt") or now_iso),
            **{
                key: bidder_ref[key]
                for key in ("completeness", "completenessDetail", "snapshotHash", "certificatesHash")
                if key in bidder_ref
            },
        },
        "sourceFiles": [],
        "keyInfoExport": None,
        "workspaceRootFingerprint": workspace_fingerprint,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "extensions": {},
    }


def initialize_state_document(*, project_id: str, now_iso: str) -> dict[str, Any]:
    """构造 F2 初始 ``state.json``（CONTRACT §8.3 / PRD 7.2，revision=1）。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "projectId": project_id,
        "currentScope": "project",
        "currentPackage": None,
        "currentStage": "project_init",
        "completedStages": [],
        "pendingInput": None,
        "abort": None,
        "locks": {"draftLocked": False, "lockedAt": None, "lockReason": ""},
        "reviewLoop": {"round": 0, "maxRounds": 5, "closedRound": None},
        "envCheckSummary": None,
        "extensions": {},
        "history": [],
        "revision": 1,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "updatedBy": "script:bg_workspace.py",
    }
