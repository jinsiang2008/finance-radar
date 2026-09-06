"""Strict, private underwriting-policy contract for the Options Lab.

The policy is user-confirmed research input.  It is not a broker permission,
funding record, live market signal, recommendation, or order instruction.
Validation is deliberately closed-world so persisted versions can be hashed,
compared and audited without silently accepting future fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


POLICY_SCHEMA_VERSION = 1
POLICY_STRATEGY = "cash_secured_put"
MAX_POLICY_UNDERLYINGS = 8
MAX_POLICY_VERSIONS = 32
POLICY_REVIEW_DAYS = 30

_MAX_SQLITE_REVISION = (1 << 63) - 2
_FIXED_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "expected_revision",
        "strategy",
        "limits",
        "assignment_plan",
        "underlyings",
        "acknowledgements",
    }
)
_LIMIT_KEYS = frozenset(
    {
        "assignment_budget_ceiling_usd",
        "max_total_reserved_bps",
        "max_single_underlying_bps",
        "minimum_cash_buffer_bps",
        "max_new_contracts_per_week",
    }
)
_ACKNOWLEDGEMENT_KEYS = frozenset(
    {"cash_secured_only", "assignment_risk_reviewed"}
)
_ASSIGNMENT_PLANS = frozenset(
    {
        "hold_for_review",
        "sell_after_assignment_review",
        "wheel_after_review",
    }
)

# Server-owned names prevent the client from persisting arbitrary labels or
# presenting an unknown/leveraged product as a familiar cash-secured-Put name.
UNDERLYING_DIRECTORY: dict[str, dict[str, str]] = {
    "US:SPY": {"symbol": "SPY", "name_zh": "标普 500 ETF", "tier": "核心 ETF"},
    "US:QQQ": {"symbol": "QQQ", "name_zh": "纳斯达克 100 ETF", "tier": "核心 ETF"},
    "US:MSFT": {"symbol": "MSFT", "name_zh": "微软", "tier": "核心大盘股"},
    "US:GOOGL": {"symbol": "GOOGL", "name_zh": "谷歌", "tier": "核心大盘股"},
    "US:META": {"symbol": "META", "name_zh": "Meta", "tier": "核心大盘股"},
    "US:NVDA": {"symbol": "NVDA", "name_zh": "英伟达", "tier": "核心大盘股"},
    "US:JPM": {"symbol": "JPM", "name_zh": "摩根大通", "tier": "核心大盘股"},
    "US:AMD": {"symbol": "AMD", "name_zh": "AMD", "tier": "半导体扩展"},
    "US:AVGO": {"symbol": "AVGO", "name_zh": "博通", "tier": "半导体扩展"},
    "US:QCOM": {"symbol": "QCOM", "name_zh": "高通", "tier": "半导体扩展"},
    "US:TSM": {"symbol": "TSM", "name_zh": "台积电", "tier": "半导体扩展"},
    "US:TSLA": {"symbol": "TSLA", "name_zh": "特斯拉", "tier": "高波动观察"},
    "US:HOOD": {"symbol": "HOOD", "name_zh": "Robinhood", "tier": "高波动观察"},
    "US:PLTR": {"symbol": "PLTR", "name_zh": "Palantir", "tier": "高波动观察"},
    "US:MU": {"symbol": "MU", "name_zh": "美光", "tier": "高波动观察"},
    "US:BABA": {"symbol": "BABA", "name_zh": "阿里巴巴", "tier": "高波动观察"},
}

_KNOWN_LEVERAGED_ASSET_KEYS = frozenset(
    {
        "US:DRAM",
        "US:GLWG",
        "US:MUU",
        "US:NVDL",
        "US:SOXL",
        "US:SPXS",
        "US:TSLL",
        "US:YINN",
    }
)


class PolicyValidationError(ValueError):
    """A stable validation failure that never contains the rejected value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise PolicyValidationError(code)


def _exact_mapping(value: Any, keys: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return value


def _strict_int(value: Any, *, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _strict_bool(value: Any, code: str) -> bool:
    if type(value) is not bool:
        _fail(code)
    return value


def _fixed_decimal(
    value: Any,
    *,
    minimum: Decimal,
    maximum: Decimal,
    code: str,
) -> str:
    if not isinstance(value, str) or not _FIXED_DECIMAL.fullmatch(value):
        _fail(code)
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(code)
    if not number.is_finite() or not minimum <= number <= maximum:
        _fail(code)
    return format(number.quantize(Decimal("0.01")), "f")


def normalize_policy_request(payload: Any) -> tuple[int, dict[str, Any]]:
    """Validate a policy request and return concurrency token + canonical body."""

    request = _exact_mapping(payload, _TOP_LEVEL_KEYS, "invalid_policy_fields")
    schema_version = _strict_int(
        request.get("schema_version"),
        minimum=POLICY_SCHEMA_VERSION,
        maximum=POLICY_SCHEMA_VERSION,
        code="invalid_schema_version",
    )
    expected_revision = _strict_int(
        request.get("expected_revision"),
        minimum=0,
        maximum=_MAX_SQLITE_REVISION,
        code="invalid_expected_revision",
    )
    if request.get("strategy") != POLICY_STRATEGY:
        _fail("invalid_strategy")

    raw_limits = _exact_mapping(
        request.get("limits"), _LIMIT_KEYS, "invalid_limits_fields"
    )
    total_reserved = _strict_int(
        raw_limits.get("max_total_reserved_bps"),
        minimum=1,
        maximum=10_000,
        code="invalid_max_total_reserved_bps",
    )
    single_underlying = _strict_int(
        raw_limits.get("max_single_underlying_bps"),
        minimum=1,
        maximum=10_000,
        code="invalid_max_single_underlying_bps",
    )
    cash_buffer = _strict_int(
        raw_limits.get("minimum_cash_buffer_bps"),
        minimum=0,
        maximum=9_999,
        code="invalid_minimum_cash_buffer_bps",
    )
    if single_underlying > total_reserved:
        _fail("single_underlying_exceeds_total_reserved")
    if total_reserved + cash_buffer > 10_000:
        _fail("reserved_plus_buffer_exceeds_available_cash")
    limits = {
        "assignment_budget_ceiling_usd": _fixed_decimal(
            raw_limits.get("assignment_budget_ceiling_usd"),
            minimum=Decimal("0.01"),
            maximum=Decimal("100000000.00"),
            code="invalid_assignment_budget_ceiling_usd",
        ),
        "max_total_reserved_bps": total_reserved,
        "max_single_underlying_bps": single_underlying,
        "minimum_cash_buffer_bps": cash_buffer,
        "max_new_contracts_per_week": _strict_int(
            raw_limits.get("max_new_contracts_per_week"),
            minimum=1,
            maximum=100,
            code="invalid_max_new_contracts_per_week",
        ),
    }

    assignment_plan = request.get("assignment_plan")
    if not isinstance(assignment_plan, str) or assignment_plan not in _ASSIGNMENT_PLANS:
        _fail("invalid_assignment_plan")

    raw_underlyings = request.get("underlyings")
    if not isinstance(raw_underlyings, list):
        _fail("invalid_underlyings")
    if len(raw_underlyings) > MAX_POLICY_UNDERLYINGS:
        _fail("too_many_underlyings")
    underlyings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_underlying in raw_underlyings:
        if not isinstance(raw_underlying, Mapping):
            _fail("invalid_underlying")
        decision = raw_underlying.get("decision")
        expected_keys = (
            frozenset({"asset_key", "decision", "max_assignment_price_usd"})
            if decision == "willing"
            else frozenset({"asset_key", "decision"})
        )
        if set(raw_underlying) != expected_keys:
            _fail("invalid_underlying_fields")
        asset_key = raw_underlying.get("asset_key")
        if not isinstance(asset_key, str):
            _fail("invalid_underlying_asset_key")
        if asset_key in _KNOWN_LEVERAGED_ASSET_KEYS:
            _fail("leveraged_underlying_not_allowed")
        if asset_key not in UNDERLYING_DIRECTORY:
            _fail("unknown_underlying")
        if asset_key in seen:
            _fail("duplicate_underlying")
        seen.add(asset_key)
        if decision not in {"willing", "exclude"}:
            _fail("invalid_underlying_decision")
        normalized: dict[str, Any] = {
            "asset_key": asset_key,
            "decision": decision,
        }
        if decision == "willing":
            normalized["max_assignment_price_usd"] = _fixed_decimal(
                raw_underlying.get("max_assignment_price_usd"),
                minimum=Decimal("0.01"),
                maximum=Decimal("1000000.00"),
                code="invalid_max_assignment_price_usd",
            )
        underlyings.append(normalized)
    underlyings.sort(key=lambda item: item["asset_key"])

    raw_acknowledgements = _exact_mapping(
        request.get("acknowledgements"),
        _ACKNOWLEDGEMENT_KEYS,
        "invalid_acknowledgement_fields",
    )
    acknowledgements = {
        "cash_secured_only": _strict_bool(
            raw_acknowledgements.get("cash_secured_only"),
            "invalid_cash_secured_only",
        ),
        "assignment_risk_reviewed": _strict_bool(
            raw_acknowledgements.get("assignment_risk_reviewed"),
            "invalid_assignment_risk_reviewed",
        ),
    }

    canonical = {
        "schema_version": schema_version,
        "strategy": POLICY_STRATEGY,
        "limits": limits,
        "assignment_plan": assignment_plan,
        "underlyings": underlyings,
        "acknowledgements": acknowledgements,
    }
    return expected_revision, canonical


def canonical_policy_json(policy: Mapping[str, Any]) -> str:
    return json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def policy_hash(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _utc_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def empty_policy_projection() -> dict[str, Any]:
    return {
        "status": "not_configured",
        "ready": False,
        "revision": 0,
        "updated_at": None,
        "review_due_at": None,
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategy": POLICY_STRATEGY,
        "limits": None,
        "assignment_plan": None,
        "underlyings": [],
        "acknowledgements": {
            "cash_secured_only": False,
            "assignment_risk_reviewed": False,
        },
        "confirmed_count": 0,
        "excluded_count": 0,
        "evidence_basis": "user_confirmed",
    }


def project_policy_record(
    record: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the bounded private API projection of one validated DB version."""

    if not isinstance(record, Mapping):
        return empty_policy_projection()
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return empty_policy_projection()

    projected_underlyings: list[dict[str, Any]] = []
    for item in payload.get("underlyings", []):
        if not isinstance(item, Mapping):
            continue
        asset_key = item.get("asset_key")
        directory = UNDERLYING_DIRECTORY.get(str(asset_key))
        decision = item.get("decision")
        if directory is None or decision not in {"willing", "exclude"}:
            continue
        projected = {
            "asset_key": asset_key,
            **directory,
            "decision": decision,
        }
        if decision == "willing" and isinstance(
            item.get("max_assignment_price_usd"), str
        ):
            projected["max_assignment_price_usd"] = item[
                "max_assignment_price_usd"
            ]
        projected_underlyings.append(projected)

    acknowledgements = payload.get("acknowledgements")
    acknowledgements = (
        dict(acknowledgements)
        if isinstance(acknowledgements, Mapping)
        else {
            "cash_secured_only": False,
            "assignment_risk_reviewed": False,
        }
    )
    confirmed_count = sum(
        item["decision"] == "willing" for item in projected_underlyings
    )
    excluded_count = sum(
        item["decision"] == "exclude" for item in projected_underlyings
    )
    review_due_at = record.get("review_due_at")
    review_due = _parse_utc(review_due_at)
    expired = review_due is None or _utc_now(now) >= review_due
    acknowledged = (
        acknowledgements.get("cash_secured_only") is True
        and acknowledgements.get("assignment_risk_reviewed") is True
    )
    if expired:
        status = "review_due"
    elif not acknowledged:
        status = "acknowledgement_required"
    elif confirmed_count == 0:
        status = "no_willing_underlyings"
    else:
        status = "ready"

    return {
        "status": status,
        "ready": status == "ready",
        "revision": int(record.get("revision") or 0),
        "updated_at": record.get("updated_at"),
        "review_due_at": review_due_at,
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategy": POLICY_STRATEGY,
        "limits": dict(payload.get("limits") or {}),
        "assignment_plan": payload.get("assignment_plan"),
        "underlyings": projected_underlyings,
        "acknowledgements": acknowledgements,
        "confirmed_count": confirmed_count,
        "excluded_count": excluded_count,
        "evidence_basis": "user_confirmed",
    }
