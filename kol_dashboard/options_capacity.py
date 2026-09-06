"""Fail-closed capacity derivation for cash-secured Put research.

The adapter accepts one immutable policy version, one complete single-account
snapshot, and one bounded US macro projection.  It performs no I/O and does
not evaluate contracts.  A successful result is only an effective risk-limit
envelope; candidate generation and execution remain unavailable.

Hash contracts
--------------
``account_snapshot.payload_sha256`` is the SHA-256 of the canonical JSON for
the account mapping with that hash field removed.  ``macro_snapshot`` follows
the same rule for ``projection_sha256``.  Canonical JSON sorts keys, emits no
insignificant whitespace, and preserves Unicode.  Policy hashes use the
canonical policy serializer owned by :mod:`options_policy`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from typing import Any

try:
    from kol_dashboard import options_policy
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    import options_policy  # type: ignore


SCHEMA_VERSION = 1
METHOD_VERSION = "options-capacity-v1"
MACRO_METHOD_VERSION = "macro-de-risk-trial-v1"
ACCOUNT_MAX_AGE = timedelta(seconds=60)
MACRO_MAX_AGE = timedelta(minutes=90)

_CENT = Decimal("0.01")
_BPS_DENOMINATOR = Decimal("10000")
_MAX_MONEY = Decimal("999999999999999.99")
_MAX_COUNT = 1_000_000
_MAX_REVISION = (1 << 63) - 2
_MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]{0,14})\.[0-9]{2}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_POLICY_KEYS = frozenset(
    {"revision", "payload_sha256", "updated_at", "review_due_at", "payload"}
)
_ACCOUNT_KEYS = frozenset(
    {
        "account_scope_id",
        "account_scope_type",
        "collateral_pool_id",
        "snapshot_id",
        "payload_sha256",
        "status",
        "as_of",
        "received_at",
        "currency",
        "cash_secured_put_permission",
        "available_cash_usd",
        "buying_power_usd",
        "buying_power_semantics",
        "net_liquidation_value_usd",
        "new_contracts_opened_this_week",
        "positions_complete",
        "existing_options_complete",
        "positions",
        "existing_options",
    }
)
_POSITION_KEYS = frozenset({"market_value_usd"})
_EXISTING_OPTION_KEYS = frozenset(
    {"underlying", "position_type", "gross_cash_reserved_usd"}
)
_MACRO_KEYS = frozenset(
    {
        "snapshot_id",
        "projection_sha256",
        "method_version",
        "generated_at",
        "markets",
    }
)
_MACRO_MARKET_KEYS = frozenset(
    {
        "market",
        "status",
        "action",
        "data_status",
        "abstain",
        "abstain_reason",
    }
)
_MACRO_ACTIONS = frozenset(
    {"observe", "prepare_reduce", "reduce_candidate", "exit_candidate"}
)
_MACRO_STATUS_BY_ACTION = {
    "observe": "ready",
    "prepare_reduce": "constrained",
    "reduce_candidate": "blocked",
    "exit_candidate": "blocked",
}
_MACRO_DATA_STATUSES = frozenset({"ok", "insufficient", "stale", "unavailable"})
_AMBIGUOUS_SCOPE_KEYS = frozenset(
    {
        "accounts",
        "account_scope_ids",
        "collateral_pools",
        "collateral_pool_ids",
        "is_aggregated",
    }
)
_AMBIGUOUS_SCOPE_TOKENS = frozenset(
    {
        "aggregate",
        "aggregated",
        "all-accounts",
        "combined",
        "consolidated",
        "household",
        "multi-account",
    }
)


class _CapacityInputError(ValueError):
    """Internal stable-code failure; never includes rejected input values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise _CapacityInputError(code)


def _exact_mapping(value: Any, keys: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return value


def _strict_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    code: str,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _identity(value: Any, code: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _sha256_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        _fail(code)
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (OverflowError, ValueError):
        _fail(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(code)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        _fail(code)


def _decision_time(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail("decision_time_invalid")
    try:
        if value.utcoffset() is None:
            _fail("decision_time_invalid")
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        _fail("decision_time_invalid")


def _money(value: Any, *, positive: bool, code: str) -> Decimal:
    if not isinstance(value, str) or _MONEY_RE.fullmatch(value) is None:
        _fail(code)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _fail(code)
    if (
        not parsed.is_finite()
        or parsed > _MAX_MONEY
        or (parsed <= 0 if positive else parsed < 0)
    ):
        _fail(code)
    return parsed


def _canonical_sha256(payload: Mapping[str, Any], code: str) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _fail(code)


def _verify_embedded_hash(
    payload: Mapping[str, Any],
    *,
    hash_key: str,
    invalid_code: str,
    mismatch_code: str,
) -> str:
    supplied = _sha256_text(payload.get(hash_key), invalid_code)
    unhashed = {key: value for key, value in payload.items() if key != hash_key}
    if _canonical_sha256(unhashed, invalid_code) != supplied:
        _fail(mismatch_code)
    return supplied


def _validate_policy(
    raw: Any,
    *,
    decision_at: datetime,
) -> dict[str, Any]:
    record = _exact_mapping(raw, _POLICY_KEYS, "invalid_policy_fields")
    revision = _strict_int(
        record.get("revision"),
        minimum=1,
        maximum=_MAX_REVISION,
        code="policy_revision_invalid",
    )
    supplied_hash = _sha256_text(
        record.get("payload_sha256"), "policy_hash_invalid"
    )
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        _fail("policy_payload_invalid")
    try:
        _, canonical = options_policy.normalize_policy_request(
            {**dict(payload), "expected_revision": 0}
        )
    except (TypeError, options_policy.PolicyValidationError):
        _fail("policy_payload_invalid")
    if dict(payload) != canonical:
        _fail("policy_payload_not_canonical")
    canonical_json = options_policy.canonical_policy_json(canonical)
    if options_policy.policy_hash(canonical_json) != supplied_hash:
        _fail("policy_hash_mismatch")

    updated_at = _timestamp(record.get("updated_at"), "policy_time_invalid")
    review_due_at = _timestamp(
        record.get("review_due_at"), "policy_time_invalid"
    )
    try:
        expected_review_due = updated_at + timedelta(
            days=options_policy.POLICY_REVIEW_DAYS
        )
    except OverflowError:
        _fail("policy_review_invalid")
    if review_due_at != expected_review_due:
        _fail("policy_review_invalid")
    if updated_at > decision_at:
        _fail("policy_future_time")
    if decision_at >= review_due_at:
        _fail("policy_review_due")

    acknowledgements = canonical["acknowledgements"]
    willing_count = sum(
        item["decision"] == "willing" for item in canonical["underlyings"]
    )
    if (
        acknowledgements["cash_secured_only"] is not True
        or acknowledgements["assignment_risk_reviewed"] is not True
        or willing_count == 0
    ):
        _fail("policy_not_ready")
    return {
        "revision": revision,
        "payload_sha256": supplied_hash,
        "updated_at": updated_at.isoformat(),
        "review_due_at": review_due_at.isoformat(),
        "limits": canonical["limits"],
    }


def _validate_positions(raw: Any) -> None:
    if not isinstance(raw, Mapping) or len(raw) > _MAX_COUNT:
        _fail("positions_incomplete")
    for symbol, raw_position in raw.items():
        if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
            _fail("positions_incomplete")
        position = _exact_mapping(
            raw_position, _POSITION_KEYS, "positions_incomplete"
        )
        _money(
            position.get("market_value_usd"),
            positive=False,
            code="positions_incomplete",
        )


def _validate_existing_options(raw: Any) -> Decimal:
    if not isinstance(raw, Mapping) or len(raw) > _MAX_COUNT:
        _fail("existing_options_incomplete")
    existing_collateral = Decimal("0")
    for contract_key, raw_option in raw.items():
        if (
            not isinstance(contract_key, str)
            or _IDENTITY_RE.fullmatch(contract_key) is None
        ):
            _fail("existing_options_incomplete")
        option = _exact_mapping(
            raw_option,
            _EXISTING_OPTION_KEYS,
            "existing_options_incomplete",
        )
        underlying = option.get("underlying")
        if (
            not isinstance(underlying, str)
            or _SYMBOL_RE.fullmatch(underlying) is None
            or option.get("position_type") != "cash_secured_short_put"
        ):
            _fail("existing_options_incomplete")
        existing_collateral += _money(
            option.get("gross_cash_reserved_usd"),
            positive=True,
            code="existing_options_incomplete",
        )
    return existing_collateral


def _validate_account(
    raw: Any,
    *,
    decision_at: datetime,
) -> dict[str, Any]:
    if isinstance(raw, (list, tuple)):
        _fail("account_scope_ambiguous")
    if isinstance(raw, Mapping):
        if set(raw) & _AMBIGUOUS_SCOPE_KEYS:
            _fail("account_scope_ambiguous")
        raw_scope = raw.get("account_scope_id")
        raw_scope_type = raw.get("account_scope_type")
        raw_pool = raw.get("collateral_pool_id")
        if isinstance(raw_scope, (list, tuple, set, frozenset, Mapping)):
            _fail("account_scope_ambiguous")
        if isinstance(raw_pool, (list, tuple, set, frozenset, Mapping)):
            _fail("account_scope_ambiguous")
        if raw_scope_type != "single_account":
            _fail("account_scope_ambiguous")
        for raw_identity in (raw_scope, raw_pool):
            if isinstance(raw_identity, str) and any(
                token in raw_identity.lower() for token in _AMBIGUOUS_SCOPE_TOKENS
            ):
                _fail("account_scope_ambiguous")
    account = _exact_mapping(raw, _ACCOUNT_KEYS, "invalid_account_fields")
    supplied_hash = _verify_embedded_hash(
        account,
        hash_key="payload_sha256",
        invalid_code="account_hash_invalid",
        mismatch_code="account_hash_mismatch",
    )
    account_scope_id = _identity(
        account.get("account_scope_id"), "account_identity_invalid"
    )
    collateral_pool_id = _identity(
        account.get("collateral_pool_id"), "account_identity_invalid"
    )
    snapshot_id = _identity(
        account.get("snapshot_id"), "account_identity_invalid"
    )
    if account.get("status") != "ready":
        _fail("account_not_ready")

    as_of = _timestamp(account.get("as_of"), "account_time_invalid")
    received_at = _timestamp(
        account.get("received_at"), "account_time_invalid"
    )
    if as_of > received_at:
        _fail("account_time_order")
    if as_of > decision_at or received_at > decision_at:
        _fail("account_future_time")
    if decision_at - as_of > ACCOUNT_MAX_AGE:
        _fail("account_stale")
    if account.get("currency") != "USD":
        _fail("account_currency_not_usd")
    if account.get("cash_secured_put_permission") is not True:
        _fail("options_permission_missing")
    if account.get("buying_power_semantics") != "cash_secured_put":
        _fail("buying_power_semantics_invalid")

    available_cash = _money(
        account.get("available_cash_usd"),
        positive=False,
        code="account_balances_invalid",
    )
    buying_power = _money(
        account.get("buying_power_usd"),
        positive=False,
        code="account_balances_invalid",
    )
    net_liquidation = _money(
        account.get("net_liquidation_value_usd"),
        positive=True,
        code="account_balances_invalid",
    )
    opened_this_week = _strict_int(
        account.get("new_contracts_opened_this_week"),
        minimum=0,
        maximum=_MAX_COUNT,
        code="weekly_new_contract_count_invalid",
    )
    if account.get("positions_complete") is not True:
        _fail("positions_incomplete")
    if account.get("existing_options_complete") is not True:
        _fail("existing_options_incomplete")
    _validate_positions(account.get("positions"))
    existing_collateral = _validate_existing_options(
        account.get("existing_options")
    )
    return {
        "account_scope_id": account_scope_id,
        "account_scope_type": "single_account",
        "collateral_pool_id": collateral_pool_id,
        "snapshot_id": snapshot_id,
        "payload_sha256": supplied_hash,
        "available_cash": available_cash,
        "buying_power": buying_power,
        "net_liquidation": net_liquidation,
        "opened_this_week": opened_this_week,
        "existing_collateral": existing_collateral,
    }


def _validate_macro(
    raw: Any,
    *,
    decision_at: datetime,
) -> dict[str, str]:
    macro = _exact_mapping(raw, _MACRO_KEYS, "invalid_macro_fields")
    supplied_hash = _verify_embedded_hash(
        macro,
        hash_key="projection_sha256",
        invalid_code="macro_hash_invalid",
        mismatch_code="macro_hash_mismatch",
    )
    snapshot_id = _identity(
        macro.get("snapshot_id"), "macro_identity_invalid"
    )
    if macro.get("method_version") != MACRO_METHOD_VERSION:
        _fail("macro_method_invalid")
    generated_at = _timestamp(
        macro.get("generated_at"), "macro_time_invalid"
    )
    if generated_at > decision_at:
        _fail("macro_future_time")
    if decision_at - generated_at > MACRO_MAX_AGE:
        _fail("macro_stale")

    markets = macro.get("markets")
    if not isinstance(markets, list) or not markets or len(markets) > 8:
        _fail("macro_market_invalid")
    validated_markets: list[Mapping[str, Any]] = []
    seen_markets: set[str] = set()
    for raw_market in markets:
        market = _exact_mapping(
            raw_market, _MACRO_MARKET_KEYS, "invalid_macro_market_fields"
        )
        market_key = market.get("market")
        if market_key not in {"US", "CN"} or market_key in seen_markets:
            _fail("macro_market_invalid")
        seen_markets.add(market_key)
        action = market.get("action")
        if not isinstance(action, str) or action not in _MACRO_ACTIONS:
            _fail("macro_action_invalid")
        data_status = market.get("data_status")
        abstain = market.get("abstain")
        status = market.get("status")
        reason = market.get("abstain_reason")
        if data_status not in _MACRO_DATA_STATUSES or type(abstain) is not bool:
            _fail("macro_state_inconsistent")
        if abstain:
            if (
                action != "observe"
                or status != "unavailable"
                or data_status == "ok"
                or not isinstance(reason, str)
                or _IDENTITY_RE.fullmatch(reason) is None
            ):
                _fail("macro_state_inconsistent")
        elif (
            status != _MACRO_STATUS_BY_ACTION[action]
            or data_status != "ok"
            or reason is not None
        ):
            _fail("macro_state_inconsistent")
        validated_markets.append(market)
    us_markets = [item for item in validated_markets if item.get("market") == "US"]
    if len(us_markets) != 1:
        _fail("macro_market_invalid")
    market = us_markets[0]
    action = market["action"]
    if market.get("data_status") != "ok" or market.get("abstain") is not False:
        _fail("macro_not_ready")
    return {
        "snapshot_id": snapshot_id,
        "projection_sha256": supplied_hash,
        "action": action,
    }


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_CENT), "f")


def _floor_cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_FLOOR)


def _ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_CEILING)


def _base_result() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "status": "abstain",
        "mode": "research_only",
        "decision_state": "abstain",
        "candidate_count": 0,
        "candidates": [],
        "identity": None,
        "input_sha256": None,
        "risk_limits": None,
        "rejections": [],
        "human_review_required": True,
        "automatic_execution": False,
        "trade_execution_available": False,
        "risk_notice": "只读容量边界，不构成投资建议；不生成、提交或执行交易。",
    }


def _abstain(
    code: str,
    *,
    identity: Mapping[str, Any] | None = None,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    result = _base_result()
    result["identity"] = dict(identity) if identity is not None else None
    result["input_sha256"] = input_sha256
    result["rejections"] = [{"code": code}]
    return result


def _build_identity(
    *,
    decision_at: datetime,
    policy: Mapping[str, Any],
    account: Mapping[str, Any],
    macro: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    identity_input = {
        "method_version": METHOD_VERSION,
        "decision_at": decision_at.isoformat(),
        "policy": {
            "revision": policy["revision"],
            "payload_sha256": policy["payload_sha256"],
            "updated_at": policy["updated_at"],
            "review_due_at": policy["review_due_at"],
        },
        "account": {
            "account_scope_type": account["account_scope_type"],
            "account_scope_id": account["account_scope_id"],
            "collateral_pool_id": account["collateral_pool_id"],
            "snapshot_id": account["snapshot_id"],
            "payload_sha256": account["payload_sha256"],
        },
        "macro": {
            "snapshot_id": macro["snapshot_id"],
            "projection_sha256": macro["projection_sha256"],
            "method_version": MACRO_METHOD_VERSION,
        },
    }
    input_sha256 = _canonical_sha256(identity_input, "identity_hash_unavailable")
    identity = {
        "capacity_snapshot_id": f"options-cap:{input_sha256}",
        "decision_at": decision_at.isoformat(),
    }
    return identity, input_sha256


def _derive_risk_limits(
    *,
    policy: Mapping[str, Any],
    account: Mapping[str, Any],
) -> dict[str, Any]:
    raw_limits = policy["limits"]
    net_liquidation = account["net_liquidation"]
    total_ratio = Decimal(raw_limits["max_total_reserved_bps"]) / _BPS_DENOMINATOR
    single_ratio = (
        Decimal(raw_limits["max_single_underlying_bps"]) / _BPS_DENOMINATOR
    )
    buffer_ratio = (
        Decimal(raw_limits["minimum_cash_buffer_bps"]) / _BPS_DENOMINATOR
    )
    policy_total = _floor_cent(net_liquidation * total_ratio)
    policy_single = _floor_cent(net_liquidation * single_ratio)
    minimum_buffer = _ceil_cent(net_liquidation * buffer_ratio)

    available_after_buffer = max(
        Decimal("0"), account["available_cash"] - minimum_buffer
    )
    new_room = max(
        Decimal("0"),
        min(account["buying_power"], available_after_buffer),
    )
    existing_collateral = account["existing_collateral"]
    account_total = existing_collateral + new_room
    absolute_ceiling = Decimal(raw_limits["assignment_budget_ceiling_usd"])
    effective_total = min(absolute_ceiling, policy_total, account_total)
    remaining_total = max(Decimal("0"), effective_total - existing_collateral)

    weekly_limit = raw_limits["max_new_contracts_per_week"]
    if account["available_cash"] < minimum_buffer:
        _fail("cash_buffer_breached")
    if existing_collateral > effective_total:
        _fail("existing_collateral_limit_breached")
    if account["opened_this_week"] >= weekly_limit:
        _fail("weekly_new_contract_limit_reached")
    if remaining_total <= 0:
        _fail("new_capacity_unavailable")
    weekly_remaining = max(0, weekly_limit - account["opened_this_week"])
    # The single-underlying figure is the policy's cumulative concentration
    # ceiling, not incremental room.  Consumers must enforce it together with
    # both total-capacity fields; silently shrinking it would erase which
    # independent policy constraint produced the boundary.
    return {
        "minimum_cash_buffer_usd": _money_text(minimum_buffer),
        "max_total_cash_secured_usd": _money_text(effective_total),
        "remaining_total_cash_secured_usd": _money_text(remaining_total),
        "max_single_underlying_assignment_usd": _money_text(policy_single),
        "max_single_underlying_assignment_semantics": (
            "cumulative_post_assignment_exposure_ceiling"
        ),
        "max_post_assignment_underlying_ratio": format(
            single_ratio.quantize(Decimal("0.000001")), "f"
        ),
        "max_new_contracts_per_week": weekly_limit,
        "remaining_new_contracts_this_week": weekly_remaining,
        "effective_for_macro_action": "observe",
    }


def derive_effective_limits(
    *,
    decision_at: datetime,
    policy_record: Mapping[str, Any] | Any,
    account_snapshot: Mapping[str, Any] | Any,
    macro_snapshot: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Return a fail-closed, deterministic effective capacity envelope.

    ``available_cash_usd`` must mean unencumbered cash available for a new
    cash-secured Put.  The account snapshot must cover exactly one account
    scope and one collateral pool.  Version 1 can derive limits only for the
    macro ``observe`` action; every more defensive action abstains.
    """

    # Decimal arithmetic must not inherit precision or rounding changes from
    # an embedding worker.  Sixty-four digits comfortably covers every
    # bounded input and aggregate accepted by this schema.
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)):
        return _derive_effective_limits(
            decision_at=decision_at,
            policy_record=policy_record,
            account_snapshot=account_snapshot,
            macro_snapshot=macro_snapshot,
        )


def _derive_effective_limits(
    *,
    decision_at: datetime,
    policy_record: Mapping[str, Any] | Any,
    account_snapshot: Mapping[str, Any] | Any,
    macro_snapshot: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    try:
        normalized_decision = _decision_time(decision_at)
        policy = _validate_policy(policy_record, decision_at=normalized_decision)
        account = _validate_account(
            account_snapshot, decision_at=normalized_decision
        )
        macro = _validate_macro(macro_snapshot, decision_at=normalized_decision)
        identity, input_sha256 = _build_identity(
            decision_at=normalized_decision,
            policy=policy,
            account=account,
            macro=macro,
        )
    except _CapacityInputError as exc:
        return _abstain(exc.code)

    action = macro["action"]
    if action == "prepare_reduce":
        return _abstain(
            "macro_constrained_limits_missing",
            identity=identity,
            input_sha256=input_sha256,
        )
    if action in {"reduce_candidate", "exit_candidate"}:
        return _abstain(
            "macro_risk_blocked",
            identity=identity,
            input_sha256=input_sha256,
        )

    try:
        risk_limits = _derive_risk_limits(policy=policy, account=account)
    except _CapacityInputError as exc:
        return _abstain(
            exc.code,
            identity=identity,
            input_sha256=input_sha256,
        )

    result = _base_result()
    result.update(
        {
            "status": "ready",
            "identity": identity,
            "input_sha256": input_sha256,
            "risk_limits": risk_limits,
        }
    )
    return result


__all__ = [
    "ACCOUNT_MAX_AGE",
    "MACRO_MAX_AGE",
    "METHOD_VERSION",
    "SCHEMA_VERSION",
    "derive_effective_limits",
]
