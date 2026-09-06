"""Fail-closed research gates for one cash-secured short put contract.

The evaluator is deliberately a pure function: it performs no network, file,
database, or broker I/O and never mutates the supplied mappings.  It accepts a
single mapping with these required mapping sections::

    decision:   {at}
    provenance: {provider, dataset, license_mode, permitted_use, data_mode,
                 snapshot_id, as_of, received_at, source_hash}
    contract:   {underlying, option_type, standard, adjusted, multiplier,
                 strike, expiration, contract_key}
    quote:      {option: {...}, underlying: {...}, derived: {...}}
    events:     {underlying, calendar_status, known_at, coverage_through,
                 earnings, corporate_action, occ_adjustment}
    macro:      {status, data_status, abstain, action}
    policy:     {status, underlyings, risk_limits}; risk_limits are effective
                limits already adjusted upstream for the declared macro action
    account:    {status, as_of, received_at, currency,
                 cash_secured_put_permission, available_cash_usd,
                 buying_power_usd, net_liquidation_value_usd,
                 new_contracts_opened_this_week,
                 positions_complete, existing_options_complete, positions,
                 existing_options}

Only a contract that clears every gate receives ``one_contract_metrics``.
Delayed and end-of-day inputs may clear the research gates, but are always
labelled ``non_current_research``.  A passing result is still research-only:
human review is required and execution is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
METHOD_VERSION = "cash-secured-put-gate-v1"

MIN_DTE = 21
MAX_DTE = 60
MAX_SPREAD_RATIO = Decimal("0.08")
MIN_OPEN_INTEREST = 500
MIN_VOLUME = 50
MIN_ABS_DELTA = Decimal("0.10")
MAX_ABS_DELTA = Decimal("0.30")
CONTRACT_MULTIPLIER = Decimal("100")
ACCOUNT_MAX_AGE_SECONDS = Decimal("60")
EVENT_CALENDAR_MAX_AGE_SECONDS = Decimal("345600")
QUOTE_RECEIPT_MAX_AGE_SECONDS = Decimal("15")
QUOTE_EVENT_MAX_AGE_SECONDS = {
    "realtime": Decimal("15"),
    "delayed": Decimal("1800"),
    "eod": Decimal("345600"),
}

_ALLOWED_LICENSE_MODES = frozenset({"licensed", "public", "first_party"})
_ALLOWED_PERMITTED_USES = frozenset({"internal_research", "research_only"})
_ALLOWED_DATA_MODES = frozenset(QUOTE_EVENT_MAX_AGE_SECONDS)
_MACRO_STATUS_BY_ACTION = {
    "observe": "ready",
    "prepare_reduce": "constrained",
    "reduce_candidate": "blocked",
    "exit_candidate": "blocked",
}
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,79}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DECIMAL_TEXT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_MAX_ABS_NUMERIC = Decimal("1000000000000000")
US_OPTIONS_TIMEZONE = ZoneInfo("America/New_York")

_CURRENTNESS_REJECTIONS = frozenset(
    {
        "decision_time_invalid",
        "data_mode_invalid",
        "market_snapshot_identity_invalid",
        "provenance_time_invalid",
        "provenance_time_order",
        "provenance_future_time",
        "contract_identity_invalid",
        "option_quote_invalid",
        "option_quote_contract_mismatch",
        "underlying_quote_invalid",
        "underlying_quote_symbol_mismatch",
        "market_snapshot_mismatch",
        "quote_time_invalid",
        "quote_time_order",
        "quote_future_time",
        "quote_stale",
        "derived_identity_mismatch",
    }
)

# These known products are rejected even if a supplied policy profile is
# accidentally misclassified.  The verified policy flags remain mandatory, so
# this list is a guardrail rather than an exhaustive product database.
_KNOWN_PROHIBITED_ETPS = frozenset(
    {
        "DRAM",
        "GLWG",
        "MUU",
        "NVDL",
        "SOXL",
        "SPXS",
        "SQQQ",
        "SVXY",
        "TQQQ",
        "TSLL",
        "UVXY",
        "VIXY",
        "VXX",
        "YINN",
    }
)

_LABELS = {
    "input_not_mapping": "输入结构不可用",
    "section_missing": "输入分区不完整",
    "decision_time_invalid": "决策时间不可核验",
    "provenance_fields_missing": "数据来源字段不完整",
    "provenance_identity_invalid": "数据来源标识不可核验",
    "market_snapshot_identity_invalid": "市场快照标识不可核验",
    "provenance_license_invalid": "数据许可不可核验",
    "provenance_use_not_permitted": "数据用途未获许可",
    "data_mode_invalid": "数据模式不可核验",
    "source_hash_invalid": "数据指纹不可核验",
    "provenance_time_invalid": "来源时间不可核验",
    "provenance_time_order": "来源时间顺序异常",
    "provenance_future_time": "来源时间晚于决策时点",
    "underlying_invalid": "标的代码不可核验",
    "option_type_not_put": "合约不是 Put",
    "non_standard_contract": "合约不是标准合约",
    "adjusted_contract": "调整后合约不可研究",
    "multiplier_not_100": "合约乘数不符合规则",
    "strike_invalid": "行权价不可核验",
    "expiration_invalid": "到期日不可核验",
    "contract_identity_invalid": "期权合约标识不可核验",
    "dte_out_of_range": "到期天数不符合规则",
    "policy_not_ready": "研究政策不可用",
    "underlying_not_whitelisted": "标的不在白名单",
    "underlying_profile_invalid": "标的属性不可核验",
    "underlying_product_prohibited": "标的产品类型被排除",
    "asset_policy_excluded": "标的已被明确排除",
    "asset_policy_unconfirmed": "接货意愿尚未确认",
    "max_assignment_price_invalid": "最高接货价不可核验",
    "strike_above_assignment_price": "行权价高于接货上限",
    "risk_limits_invalid": "风险预算规则不可核验",
    "risk_limits_macro_mode_mismatch": "风险预算未匹配宏观状态",
    "option_quote_invalid": "期权报价不可核验",
    "option_quote_contract_mismatch": "期权报价与合约不匹配",
    "underlying_quote_invalid": "标的报价不可核验",
    "underlying_quote_symbol_mismatch": "标的报价代码不匹配",
    "market_snapshot_mismatch": "报价快照不一致",
    "bid_not_positive": "Bid 必须大于零",
    "ask_invalid": "Ask 不符合规则",
    "crossed_quote": "报价发生交叉",
    "bid_size_not_positive": "Bid Size 必须大于零",
    "ask_size_not_positive": "Ask Size 必须大于零",
    "spread_too_wide": "买卖价差超过上限",
    "put_premium_out_of_bounds": "Put 权利金超出合理边界",
    "open_interest_too_low": "未平仓量低于门槛",
    "volume_too_low": "成交量低于门槛",
    "underlying_price_invalid": "标的价格不可核验",
    "quote_time_invalid": "报价时间不可核验",
    "quote_time_order": "报价时间顺序异常",
    "quote_future_time": "报价时间晚于决策时点",
    "quote_stale": "报价超过模式时效上限",
    "delta_invalid": "Delta 不可核验",
    "derived_identity_mismatch": "派生指标身份不匹配",
    "delta_out_of_range": "Delta 不在研究区间",
    "event_calendar_not_ready": "事件日历不可用",
    "event_calendar_time_invalid": "事件日历时间不可核验",
    "event_calendar_lookahead": "事件日历包含前视信息",
    "event_calendar_stale": "事件日历已过期",
    "event_calendar_coverage_invalid": "事件日历覆盖日期不可核验",
    "event_calendar_coverage_insufficient": "事件日历未覆盖至到期日",
    "event_underlying_mismatch": "事件日历标的不匹配",
    "earnings_status_unknown": "财报事件状态不可核验",
    "earnings_time_invalid": "财报时间不可核验",
    "earnings_before_expiration": "到期前存在财报事件",
    "corporate_action_unresolved": "公司行动尚未解析",
    "occ_adjustment_present": "存在 OCC 合约调整",
    "macro_not_ready": "宏观闸门不可用",
    "macro_abstain": "宏观闸门要求弃权",
    "macro_action_unknown": "宏观动作不可识别",
    "macro_risk_blocked": "宏观风险阻止新增研究候选",
    "macro_prepare_reduce_symbol": "收紧状态下标的不符合范围",
    "account_not_ready": "账户快照不可用",
    "account_time_invalid": "账户时间不可核验",
    "account_time_order": "账户时间顺序异常",
    "account_future_time": "账户时间晚于决策时点",
    "account_stale": "账户快照已过期",
    "account_currency_not_usd": "账户币种不符合规则",
    "options_permission_missing": "现金担保卖 Put 权限未确认",
    "account_balances_invalid": "账户资金字段不可核验",
    "weekly_new_contract_count_invalid": "本周新增合约计数不可核验",
    "positions_incomplete": "现货持仓语义不完整",
    "existing_options_incomplete": "现有期权语义不完整",
    "available_cash_insufficient": "可用现金不足",
    "buying_power_insufficient": "购买力不足",
    "cash_buffer_breached": "现金缓冲不足",
    "total_collateral_limit_breached": "总担保超过上限",
    "weekly_new_contract_limit_breached": "本周新增合约达到上限",
    "post_assignment_concentration_breached": "接货后集中度超过上限",
}

_DETAILS = {
    "input_not_mapping": "根输入必须是只读可解析的映射结构。",
    "section_missing": "一个或多个必需输入分区缺失或类型错误。",
    "decision_time_invalid": "决策时点必须是带时区的 ISO 8601 时间。",
    "provenance_fields_missing": "来源、数据集、许可、用途、模式、快照、时间与指纹均为必填。",
    "provenance_identity_invalid": "来源与数据集必须使用受限长度的可审计标识。",
    "market_snapshot_identity_invalid": "市场快照必须提供受限长度的可审计标识。",
    "provenance_license_invalid": "许可模式不在允许的研究许可集合中。",
    "provenance_use_not_permitted": "用途必须明确允许内部或只读研究。",
    "data_mode_invalid": "数据模式只能是 realtime、delayed 或 eod。",
    "source_hash_invalid": "来源指纹必须是完整 SHA-256。",
    "provenance_time_invalid": "来源时间必须是带时区的 ISO 8601 时间。",
    "provenance_time_order": "来源观测时间不得晚于接收时间。",
    "provenance_future_time": "来源观测或接收时间不得晚于决策时点。",
    "underlying_invalid": "标的必须是规范化的美股代码。",
    "option_type_not_put": "该评估器只接受 Put 合约。",
    "non_standard_contract": "只研究标准化合约。",
    "adjusted_contract": "拆股或公司行动调整后的合约失败关闭。",
    "multiplier_not_100": "只接受乘数恰好为 100 的标准合约。",
    "strike_invalid": "行权价必须是有限且大于零的金额。",
    "expiration_invalid": "到期日必须是 YYYY-MM-DD。",
    "contract_identity_invalid": "合约标识必须与标的、到期日、Put、行权价和乘数的规范键完全一致。",
    "dte_out_of_range": "日历 DTE 必须在 21 至 60 天之间，含边界。",
    "policy_not_ready": "研究政策必须明确处于 ready 状态。",
    "underlying_not_whitelisted": "只有经过显式白名单审核的标的可继续。",
    "underlying_profile_invalid": "白名单必须完整声明产品类型及杠杆、反向和波动率属性。",
    "underlying_product_prohibited": "杠杆、反向或波动率相关 ETP 被排除。",
    "asset_policy_excluded": "用户政策已明确排除该标的。",
    "asset_policy_unconfirmed": "必须显式确认愿意按规则价格接货。",
    "max_assignment_price_invalid": "最高接货价必须是有限且大于零的金额。",
    "strike_above_assignment_price": "合约行权价不得高于用户确认的最高接货价。",
    "risk_limits_invalid": "现金缓冲、总担保和集中度上限必须完整且有效。",
    "risk_limits_macro_mode_mismatch": "有效风险限额必须显式对应本次宏观动作；prepare_reduce 需由上游先收紧。",
    "option_quote_invalid": "期权报价分区或数值字段不完整。",
    "option_quote_contract_mismatch": "期权报价必须携带并匹配本次评估的规范合约标识。",
    "underlying_quote_invalid": "标的报价分区或数值字段不完整。",
    "underlying_quote_symbol_mismatch": "标的报价必须携带并匹配本次评估的标的代码。",
    "market_snapshot_mismatch": "期权与标的报价必须来自 provenance 声明的同一市场快照。",
    "bid_not_positive": "研究卖价采用 Bid，且 Bid 必须大于零。",
    "ask_invalid": "Ask 必须是有限且大于零的价格。",
    "crossed_quote": "Ask 必须大于或等于 Bid。",
    "bid_size_not_positive": "Bid Size 必须是正整数。",
    "ask_size_not_positive": "Ask Size 必须是正整数。",
    "spread_too_wide": "价差比按 (Ask-Bid)/Bid 计算，不得超过 8%，含边界。",
    "put_premium_out_of_bounds": "Put 的 Bid 必须低于行权价，且 Ask 不得高于行权价。",
    "open_interest_too_low": "未平仓量必须至少为 500。",
    "volume_too_low": "当日成交量必须至少为 50。",
    "underlying_price_invalid": "标的价格必须是有限且大于零的金额。",
    "quote_time_invalid": "期权与标的报价均需 event_ts 和 recv_ts。",
    "quote_time_order": "报价事件时间不得晚于接收时间。",
    "quote_future_time": "报价事件或接收时间不得晚于决策时点。",
    "quote_stale": "报价必须符合声明数据模式的事件和接收时效。",
    "delta_invalid": "派生 Delta 必须存在且是有限数字。",
    "derived_identity_mismatch": "派生指标必须携带并匹配本次合约标识与市场快照标识。",
    "delta_out_of_range": "Put Delta 必须为负数，且在 -0.30 至 -0.10 之间，含边界。",
    "event_calendar_not_ready": "事件日历、财报、公司行动与 OCC 状态必须完整。",
    "event_calendar_time_invalid": "事件日历已知时间必须是带时区的 ISO 8601 时间。",
    "event_calendar_lookahead": "只允许使用决策时点前已经获知的事件信息。",
    "event_calendar_stale": "事件日历已知时间距决策时点不得超过四天。",
    "event_calendar_coverage_invalid": "事件日历必须提供 YYYY-MM-DD 格式的 coverage_through。",
    "event_calendar_coverage_insufficient": "事件日历必须至少覆盖至期权到期日。",
    "event_underlying_mismatch": "事件日历必须明确绑定本次评估的标的代码。",
    "earnings_status_unknown": "财报状态必须明确为 none 或 scheduled。",
    "earnings_time_invalid": "scheduled 财报必须提供可核验的带时区时间。",
    "earnings_before_expiration": "到期日结束前存在已知财报，候选失败关闭。",
    "corporate_action_unresolved": "未解析的公司行动会改变交割或风险语义。",
    "occ_adjustment_present": "OCC 调整或其状态不明时不研究该合约。",
    "macro_not_ready": "宏观状态必须与动作精确匹配，且数据状态必须明确可用。",
    "macro_abstain": "宏观系统弃权时不得继续生成研究候选。",
    "macro_action_unknown": "宏观动作必须属于允许的动作集合。",
    "macro_risk_blocked": "reduce_candidate 或 exit_candidate 阻止新增短 Put 研究。",
    "macro_prepare_reduce_symbol": "prepare_reduce 期间只允许 SPY 或 QQQ 继续受限研究。",
    "account_not_ready": "账户快照必须明确处于 ready 状态。",
    "account_time_invalid": "账户 as_of 与 received_at 必须是带时区时间。",
    "account_time_order": "账户 as_of 不得晚于 received_at。",
    "account_future_time": "账户时间不得晚于决策时点。",
    "account_stale": "账户 as_of 距决策时点不得超过 60 秒。",
    "account_currency_not_usd": "该版本只核算美元现金担保。",
    "options_permission_missing": "必须显式确认现金担保卖 Put 权限为真。",
    "account_balances_invalid": "可用现金、购买力和净清算价值必须完整且有效。",
    "weekly_new_contract_count_invalid": "账户必须提供完整且非负的本周新增合约计数。",
    "positions_incomplete": "持仓必须完整同步为标的到市值的映射。",
    "existing_options_incomplete": "现有期权必须完整同步标的、正数担保额与现金担保短 Put 语义；缺少类型时按担保额保守计作潜在接货。",
    "available_cash_insufficient": "账户可用现金不足以全额覆盖一张的行权价乘数。",
    "buying_power_insufficient": "账户购买力不足以覆盖一张的全额担保。",
    "cash_buffer_breached": "预留一张全额担保后将低于政策现金缓冲。",
    "total_collateral_limit_breached": "现有担保加一张全额担保超过政策上限。",
    "weekly_new_contract_limit_breached": "计入本次研究合约后将超过本周新增上限。",
    "post_assignment_concentration_breached": "按行权价接货后该标的占净清算价值比例超过政策上限。",
}


def _rejection(code: str) -> dict[str, str]:
    """Return stable copy only; never interpolate untrusted input."""

    return {"code": code, "label": _LABELS[code], "detail": _DETAILS[code]}


def _add(rejections: list[dict[str, str]], code: str) -> None:
    if not any(item["code"] == code for item in rejections):
        rejections.append(_rejection(code))


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = payload.get(key)
    return value if isinstance(value, Mapping) else None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str):
        text = value.strip()
        if (
            not text
            or len(text) > 64
            or _DECIMAL_TEXT_RE.fullmatch(text) is None
        ):
            return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or abs(parsed) > _MAX_ABS_NUMERIC:
        return None
    return parsed


def _integer(value: Any) -> int | None:
    parsed = _decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _bps(value: Decimal) -> str:
    return _money(value * Decimal("10000"))


def _canonical_contract_key(
    *,
    symbol: str | None,
    expiration: date | None,
    strike: Decimal | None,
) -> str | None:
    if symbol is None or expiration is None or strike is None or strike <= 0:
        return None
    return f"US:{symbol}:{expiration.isoformat()}:P:{_money(strike)}:100"


def _base_result(
    *,
    status: str,
    rejections: list[dict[str, str]],
    data_mode: str | None,
) -> dict[str, Any]:
    rejection_codes = {item["code"] for item in rejections}
    realtime_data_is_usable = not (rejection_codes & _CURRENTNESS_REJECTIONS)
    research_context = (
        "current_research"
        if data_mode == "realtime" and realtime_data_is_usable
        else "non_current_research"
        if data_mode in {"delayed", "eod"}
        else "unavailable_research"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "status": status,
        "mode": "research_only",
        "research_context": research_context,
        "data_mode": data_mode if data_mode in _ALLOWED_DATA_MODES else "unknown",
        "is_current_data": data_mode == "realtime" and realtime_data_is_usable,
        "rejections": rejections,
        "human_review_required": True,
        "automatic_execution": False,
        "trade_execution_available": False,
        "risk_notice": "只读研究候选，不构成投资建议；不提交或执行交易。",
    }


def _validate_timed_quote(
    item: Mapping[str, Any],
    *,
    decision_at: datetime | None,
    data_mode: str | None,
    rejections: list[dict[str, str]],
) -> None:
    event_ts = _timestamp(item.get("event_ts"))
    recv_ts = _timestamp(item.get("recv_ts"))
    if event_ts is None or recv_ts is None:
        _add(rejections, "quote_time_invalid")
        return
    if event_ts > recv_ts:
        _add(rejections, "quote_time_order")
    if decision_at is None:
        return
    if event_ts > decision_at or recv_ts > decision_at:
        _add(rejections, "quote_future_time")
        return
    if data_mode not in _ALLOWED_DATA_MODES:
        return
    event_age = Decimal(str((decision_at - event_ts).total_seconds()))
    receive_age = Decimal(str((decision_at - recv_ts).total_seconds()))
    if (
        event_age > QUOTE_EVENT_MAX_AGE_SECONDS[data_mode]
        or receive_age > QUOTE_RECEIPT_MAX_AGE_SECONDS
    ):
        _add(rejections, "quote_stale")


def _validate_positions(
    positions: Any,
    *,
    target: str | None,
    rejections: list[dict[str, str]],
) -> tuple[Decimal | None, bool]:
    if not isinstance(positions, Mapping):
        _add(rejections, "positions_incomplete")
        return None, False
    target_value = Decimal("0")
    valid = True
    for symbol, item in positions.items():
        if (
            not isinstance(symbol, str)
            or _SYMBOL_RE.fullmatch(symbol) is None
            or not isinstance(item, Mapping)
        ):
            valid = False
            continue
        market_value = _decimal(item.get("market_value_usd"))
        if market_value is None or market_value < 0:
            valid = False
            continue
        if symbol == target:
            target_value += market_value
    if not valid:
        _add(rejections, "positions_incomplete")
        return None, False
    return target_value, True


def _validate_existing_options(
    existing_options: Any,
    *,
    target: str | None,
    rejections: list[dict[str, str]],
) -> tuple[Decimal | None, Decimal | None, bool]:
    if not isinstance(existing_options, Mapping):
        _add(rejections, "existing_options_incomplete")
        return None, None, False
    total = Decimal("0")
    target_assignment_exposure = Decimal("0")
    valid = True
    for item in existing_options.values():
        if not isinstance(item, Mapping):
            valid = False
            continue
        underlying = item.get("underlying")
        reserve = _decimal(item.get("gross_cash_reserved_usd"))
        explicit_semantics = [
            item[key]
            for key in ("strategy", "position_type")
            if key in item
        ]
        if (
            not isinstance(underlying, str)
            or _SYMBOL_RE.fullmatch(underlying) is None
            or reserve is None
            or reserve <= 0
            or any(
                semantic != "cash_secured_short_put"
                for semantic in explicit_semantics
            )
        ):
            valid = False
            continue
        total += reserve
        # When no explicit type is supplied, gross reserved cash is treated as
        # conservative short-Put assignment exposure rather than ignored.
        if underlying == target:
            target_assignment_exposure += reserve
    if not valid:
        _add(rejections, "existing_options_incomplete")
        return None, None, False
    return total, target_assignment_exposure, True


def evaluate_cash_secured_put(inputs: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Evaluate one contract without I/O, side effects, or execution advice."""

    rejections: list[dict[str, str]] = []
    if not isinstance(inputs, Mapping):
        return _base_result(
            status="rejected",
            rejections=[_rejection("input_not_mapping")],
            data_mode=None,
        )

    sections = {
        key: _section(inputs, key)
        for key in (
            "decision",
            "provenance",
            "contract",
            "quote",
            "events",
            "macro",
            "policy",
            "account",
        )
    }
    if any(section is None for section in sections.values()):
        _add(rejections, "section_missing")

    decision = sections["decision"] or {}
    decision_at = _timestamp(decision.get("at"))
    if decision_at is None:
        _add(rejections, "decision_time_invalid")

    provenance = sections["provenance"] or {}
    required_provenance = {
        "provider",
        "dataset",
        "license_mode",
        "permitted_use",
        "data_mode",
        "snapshot_id",
        "as_of",
        "received_at",
        "source_hash",
    }
    if not required_provenance.issubset(provenance):
        _add(rejections, "provenance_fields_missing")
    provider = provenance.get("provider")
    dataset = provenance.get("dataset")
    if (
        not isinstance(provider, str)
        or _IDENTITY_RE.fullmatch(provider) is None
        or not isinstance(dataset, str)
        or _IDENTITY_RE.fullmatch(dataset) is None
    ):
        _add(rejections, "provenance_identity_invalid")
    raw_snapshot_id = provenance.get("snapshot_id")
    snapshot_id = (
        raw_snapshot_id
        if isinstance(raw_snapshot_id, str)
        and _IDENTITY_RE.fullmatch(raw_snapshot_id) is not None
        else None
    )
    if snapshot_id is None:
        _add(rejections, "market_snapshot_identity_invalid")
    license_mode = provenance.get("license_mode")
    if (
        not isinstance(license_mode, str)
        or license_mode not in _ALLOWED_LICENSE_MODES
    ):
        _add(rejections, "provenance_license_invalid")
    permitted_use = provenance.get("permitted_use")
    if (
        not isinstance(permitted_use, str)
        or permitted_use not in _ALLOWED_PERMITTED_USES
    ):
        _add(rejections, "provenance_use_not_permitted")
    raw_data_mode = provenance.get("data_mode")
    data_mode = (
        raw_data_mode
        if isinstance(raw_data_mode, str) and raw_data_mode in _ALLOWED_DATA_MODES
        else None
    )
    if data_mode is None:
        _add(rejections, "data_mode_invalid")
    source_hash = provenance.get("source_hash")
    if not isinstance(source_hash, str) or _SHA256_RE.fullmatch(source_hash) is None:
        _add(rejections, "source_hash_invalid")
    provenance_as_of = _timestamp(provenance.get("as_of"))
    provenance_received = _timestamp(provenance.get("received_at"))
    if provenance_as_of is None or provenance_received is None:
        _add(rejections, "provenance_time_invalid")
    else:
        if provenance_as_of > provenance_received:
            _add(rejections, "provenance_time_order")
        if decision_at is not None and (
            provenance_as_of > decision_at or provenance_received > decision_at
        ):
            _add(rejections, "provenance_future_time")

    contract = sections["contract"] or {}
    underlying = contract.get("underlying")
    symbol = (
        underlying
        if isinstance(underlying, str) and _SYMBOL_RE.fullmatch(underlying)
        else None
    )
    if symbol is None:
        _add(rejections, "underlying_invalid")
    if contract.get("option_type") != "put":
        _add(rejections, "option_type_not_put")
    if contract.get("standard") is not True:
        _add(rejections, "non_standard_contract")
    if contract.get("adjusted") is not False:
        _add(rejections, "adjusted_contract")
    multiplier = _decimal(contract.get("multiplier"))
    if multiplier != CONTRACT_MULTIPLIER:
        _add(rejections, "multiplier_not_100")
    strike = _decimal(contract.get("strike"))
    if strike is None or strike <= 0:
        _add(rejections, "strike_invalid")
    expiration = _date(contract.get("expiration"))
    if expiration is None:
        _add(rejections, "expiration_invalid")
    expected_contract_key = _canonical_contract_key(
        symbol=symbol,
        expiration=expiration,
        strike=strike,
    )
    raw_contract_key = contract.get("contract_key")
    contract_key = (
        raw_contract_key
        if isinstance(raw_contract_key, str)
        and _IDENTITY_RE.fullmatch(raw_contract_key) is not None
        else None
    )
    if expected_contract_key is None or contract_key != expected_contract_key:
        _add(rejections, "contract_identity_invalid")
    dte: int | None = None
    if expiration is not None and decision_at is not None:
        dte = (
            expiration - decision_at.astimezone(US_OPTIONS_TIMEZONE).date()
        ).days
        if not MIN_DTE <= dte <= MAX_DTE:
            _add(rejections, "dte_out_of_range")

    policy = sections["policy"] or {}
    if policy.get("status") != "ready":
        _add(rejections, "policy_not_ready")
    underlyings = policy.get("underlyings")
    asset_policy: Mapping[str, Any] | None = None
    if isinstance(underlyings, Mapping) and symbol is not None:
        candidate_policy = underlyings.get(symbol)
        if isinstance(candidate_policy, Mapping):
            asset_policy = candidate_policy
    if asset_policy is None or asset_policy.get("whitelisted") is not True:
        _add(rejections, "underlying_not_whitelisted")
    else:
        product_type = asset_policy.get("product_type")
        flags_are_explicit = all(
            asset_policy.get(key) is False
            for key in ("leveraged", "inverse", "volatility_linked")
        )
        if (
            not isinstance(product_type, str)
            or product_type not in {"stock", "etf"}
            or not flags_are_explicit
        ):
            _add(rejections, "underlying_profile_invalid")
        if (
            symbol in _KNOWN_PROHIBITED_ETPS
            or asset_policy.get("leveraged") is True
            or asset_policy.get("inverse") is True
            or asset_policy.get("volatility_linked") is True
        ):
            _add(rejections, "underlying_product_prohibited")
        asset_decision = asset_policy.get("decision")
        if asset_decision == "exclude":
            _add(rejections, "asset_policy_excluded")
        elif asset_decision != "willing":
            _add(rejections, "asset_policy_unconfirmed")
        max_assignment_price = _decimal(
            asset_policy.get("max_assignment_price_usd")
        )
        if max_assignment_price is None or max_assignment_price <= 0:
            _add(rejections, "max_assignment_price_invalid")
        elif strike is not None and strike > max_assignment_price:
            _add(rejections, "strike_above_assignment_price")

    risk_limits = policy.get("risk_limits")
    minimum_cash_buffer: Decimal | None = None
    max_total_collateral: Decimal | None = None
    max_concentration: Decimal | None = None
    max_new_contracts_per_week: int | None = None
    if isinstance(risk_limits, Mapping):
        minimum_cash_buffer = _decimal(
            risk_limits.get("minimum_cash_buffer_usd")
        )
        max_total_collateral = _decimal(
            risk_limits.get("max_total_cash_secured_usd")
        )
        max_concentration = _decimal(
            risk_limits.get("max_post_assignment_underlying_ratio")
        )
        max_new_contracts_per_week = _integer(
            risk_limits.get("max_new_contracts_per_week")
        )
    if (
        minimum_cash_buffer is None
        or minimum_cash_buffer < 0
        or max_total_collateral is None
        or max_total_collateral <= 0
        or max_concentration is None
        or not Decimal("0") < max_concentration <= Decimal("1")
        or max_new_contracts_per_week is None
        or max_new_contracts_per_week < 0
    ):
        _add(rejections, "risk_limits_invalid")

    quote = sections["quote"] or {}
    option_quote = quote.get("option")
    underlying_quote = quote.get("underlying")
    option_quote = option_quote if isinstance(option_quote, Mapping) else None
    underlying_quote = (
        underlying_quote if isinstance(underlying_quote, Mapping) else None
    )
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread_ratio: Decimal | None = None
    if option_quote is None:
        _add(rejections, "option_quote_invalid")
    else:
        option_contract_key = option_quote.get("contract_key")
        if (
            expected_contract_key is None
            or not isinstance(option_contract_key, str)
            or _IDENTITY_RE.fullmatch(option_contract_key) is None
            or option_contract_key != expected_contract_key
        ):
            _add(rejections, "option_quote_contract_mismatch")
        option_snapshot_id = option_quote.get("snapshot_id")
        if (
            snapshot_id is None
            or not isinstance(option_snapshot_id, str)
            or _IDENTITY_RE.fullmatch(option_snapshot_id) is None
            or option_snapshot_id != snapshot_id
        ):
            _add(rejections, "market_snapshot_mismatch")
        bid = _decimal(option_quote.get("bid"))
        ask = _decimal(option_quote.get("ask"))
        bid_size = _integer(option_quote.get("bid_size"))
        ask_size = _integer(option_quote.get("ask_size"))
        open_interest = _integer(option_quote.get("open_interest"))
        volume = _integer(option_quote.get("volume"))
        if bid is None:
            _add(rejections, "option_quote_invalid")
        elif bid <= 0:
            _add(rejections, "bid_not_positive")
        if ask is None:
            _add(rejections, "ask_invalid")
        elif ask <= 0:
            _add(rejections, "ask_invalid")
        if bid is not None and ask is not None and ask < bid:
            _add(rejections, "crossed_quote")
        if (
            strike is not None
            and strike > 0
            and (
                (bid is not None and bid >= strike)
                or (ask is not None and ask > strike)
            )
        ):
            _add(rejections, "put_premium_out_of_bounds")
        if bid_size is None or bid_size <= 0:
            _add(rejections, "bid_size_not_positive")
        if ask_size is None or ask_size <= 0:
            _add(rejections, "ask_size_not_positive")
        if bid is not None and bid > 0 and ask is not None and ask >= bid:
            spread_ratio = (ask - bid) / bid
            if spread_ratio > MAX_SPREAD_RATIO:
                _add(rejections, "spread_too_wide")
        if open_interest is None or open_interest < MIN_OPEN_INTEREST:
            _add(rejections, "open_interest_too_low")
        if volume is None or volume < MIN_VOLUME:
            _add(rejections, "volume_too_low")
        _validate_timed_quote(
            option_quote,
            decision_at=decision_at,
            data_mode=data_mode,
            rejections=rejections,
        )

    underlying_price: Decimal | None = None
    if underlying_quote is None:
        _add(rejections, "underlying_quote_invalid")
    else:
        quote_symbol = underlying_quote.get("symbol")
        if symbol is None or quote_symbol != symbol:
            _add(rejections, "underlying_quote_symbol_mismatch")
        underlying_snapshot_id = underlying_quote.get("snapshot_id")
        if (
            snapshot_id is None
            or not isinstance(underlying_snapshot_id, str)
            or _IDENTITY_RE.fullmatch(underlying_snapshot_id) is None
            or underlying_snapshot_id != snapshot_id
        ):
            _add(rejections, "market_snapshot_mismatch")
        underlying_price = _decimal(underlying_quote.get("price"))
        if underlying_price is None or underlying_price <= 0:
            _add(rejections, "underlying_price_invalid")
        _validate_timed_quote(
            underlying_quote,
            decision_at=decision_at,
            data_mode=data_mode,
            rejections=rejections,
        )

    derived = quote.get("derived")
    delta: Decimal | None = None
    if not isinstance(derived, Mapping):
        _add(rejections, "derived_identity_mismatch")
        _add(rejections, "delta_invalid")
    else:
        derived_contract_key = derived.get("contract_key")
        derived_snapshot_id = derived.get("snapshot_id")
        if (
            expected_contract_key is None
            or not isinstance(derived_contract_key, str)
            or _IDENTITY_RE.fullmatch(derived_contract_key) is None
            or derived_contract_key != expected_contract_key
            or snapshot_id is None
            or not isinstance(derived_snapshot_id, str)
            or _IDENTITY_RE.fullmatch(derived_snapshot_id) is None
            or derived_snapshot_id != snapshot_id
        ):
            _add(rejections, "derived_identity_mismatch")
        delta = _decimal(derived.get("delta"))
        if delta is None:
            _add(rejections, "delta_invalid")
        elif not -MAX_ABS_DELTA <= delta <= -MIN_ABS_DELTA:
            _add(rejections, "delta_out_of_range")

    events = sections["events"] or {}
    earnings = events.get("earnings")
    corporate_action = events.get("corporate_action")
    occ_adjustment = events.get("occ_adjustment")
    event_sections_ready = all(
        isinstance(item, Mapping)
        for item in (earnings, corporate_action, occ_adjustment)
    )
    if events.get("calendar_status") != "ready" or not event_sections_ready:
        _add(rejections, "event_calendar_not_ready")
    if symbol is None or events.get("underlying") != symbol:
        _add(rejections, "event_underlying_mismatch")
    known_at = _timestamp(events.get("known_at"))
    if known_at is None:
        _add(rejections, "event_calendar_time_invalid")
    elif decision_at is not None:
        if known_at > decision_at:
            _add(rejections, "event_calendar_lookahead")
        elif Decimal(str((decision_at - known_at).total_seconds())) > (
            EVENT_CALENDAR_MAX_AGE_SECONDS
        ):
            _add(rejections, "event_calendar_stale")
    coverage_through = _date(events.get("coverage_through"))
    if coverage_through is None:
        _add(rejections, "event_calendar_coverage_invalid")
    elif expiration is not None and coverage_through < expiration:
        _add(rejections, "event_calendar_coverage_insufficient")
    if isinstance(earnings, Mapping):
        earnings_status = earnings.get("status")
        if earnings_status not in {"none", "scheduled"}:
            _add(rejections, "earnings_status_unknown")
        elif earnings_status == "scheduled":
            earnings_at = _timestamp(earnings.get("at"))
            if earnings_at is None:
                _add(rejections, "earnings_time_invalid")
            elif expiration is not None:
                expiration_end = datetime.combine(
                    expiration,
                    time.max,
                    tzinfo=US_OPTIONS_TIMEZONE,
                ).astimezone(timezone.utc)
                if earnings_at <= expiration_end:
                    _add(rejections, "earnings_before_expiration")
    if (
        isinstance(corporate_action, Mapping)
        and corporate_action.get("status") != "clear"
    ):
        _add(rejections, "corporate_action_unresolved")
    if (
        isinstance(occ_adjustment, Mapping)
        and occ_adjustment.get("status") != "none"
    ):
        _add(rejections, "occ_adjustment_present")

    macro = sections["macro"] or {}
    macro_action = macro.get("action")
    if macro.get("data_status") != "ok":
        _add(rejections, "macro_not_ready")
    if macro.get("abstain") is not False:
        _add(rejections, "macro_abstain")
    if not isinstance(macro_action, str) or macro_action not in (
        _MACRO_STATUS_BY_ACTION
    ):
        _add(rejections, "macro_action_unknown")
    else:
        if macro.get("status") != _MACRO_STATUS_BY_ACTION[macro_action]:
            _add(rejections, "macro_not_ready")
        if macro_action in {"reduce_candidate", "exit_candidate"}:
            _add(rejections, "macro_risk_blocked")
        elif macro_action == "prepare_reduce" and symbol not in {"SPY", "QQQ"}:
            _add(rejections, "macro_prepare_reduce_symbol")
    risk_budget = "constrained" if macro_action == "prepare_reduce" else "standard"
    if (
        isinstance(risk_limits, Mapping)
        and risk_limits.get("effective_for_macro_action") != macro_action
    ):
        _add(rejections, "risk_limits_macro_mode_mismatch")

    account = sections["account"] or {}
    if account.get("status") != "ready":
        _add(rejections, "account_not_ready")
    account_as_of = _timestamp(account.get("as_of"))
    account_received = _timestamp(account.get("received_at"))
    if account_as_of is None or account_received is None:
        _add(rejections, "account_time_invalid")
    else:
        if account_as_of > account_received:
            _add(rejections, "account_time_order")
        if decision_at is not None:
            if account_as_of > decision_at or account_received > decision_at:
                _add(rejections, "account_future_time")
            elif Decimal(str((decision_at - account_as_of).total_seconds())) > (
                ACCOUNT_MAX_AGE_SECONDS
            ):
                _add(rejections, "account_stale")
    if account.get("currency") != "USD":
        _add(rejections, "account_currency_not_usd")
    if account.get("cash_secured_put_permission") is not True:
        _add(rejections, "options_permission_missing")

    available_cash = _decimal(account.get("available_cash_usd"))
    buying_power = _decimal(account.get("buying_power_usd"))
    net_liquidation = _decimal(account.get("net_liquidation_value_usd"))
    new_contracts_this_week = _integer(
        account.get("new_contracts_opened_this_week")
    )
    if (
        available_cash is None
        or available_cash < 0
        or buying_power is None
        or buying_power < 0
        or net_liquidation is None
        or net_liquidation <= 0
    ):
        _add(rejections, "account_balances_invalid")
    if new_contracts_this_week is None or new_contracts_this_week < 0:
        _add(rejections, "weekly_new_contract_count_invalid")
    if account.get("positions_complete") is not True:
        _add(rejections, "positions_incomplete")
    if account.get("existing_options_complete") is not True:
        _add(rejections, "existing_options_incomplete")
    target_market_value, positions_valid = _validate_positions(
        account.get("positions"),
        target=symbol,
        rejections=rejections,
    )
    (
        existing_collateral,
        target_existing_assignment_exposure,
        options_valid,
    ) = _validate_existing_options(
        account.get("existing_options"),
        target=symbol,
        rejections=rejections,
    )

    gross_cash_reserved = (
        strike * CONTRACT_MULTIPLIER if strike is not None and strike > 0 else None
    )
    if gross_cash_reserved is not None:
        if available_cash is not None and available_cash < gross_cash_reserved:
            _add(rejections, "available_cash_insufficient")
        if buying_power is not None and buying_power < gross_cash_reserved:
            _add(rejections, "buying_power_insufficient")
        if (
            available_cash is not None
            and minimum_cash_buffer is not None
            and available_cash - gross_cash_reserved < minimum_cash_buffer
        ):
            _add(rejections, "cash_buffer_breached")
        if (
            options_valid
            and existing_collateral is not None
            and max_total_collateral is not None
            and existing_collateral + gross_cash_reserved > max_total_collateral
        ):
            _add(rejections, "total_collateral_limit_breached")
        if (
            new_contracts_this_week is not None
            and new_contracts_this_week >= 0
            and max_new_contracts_per_week is not None
            and new_contracts_this_week + 1 > max_new_contracts_per_week
        ):
            _add(rejections, "weekly_new_contract_limit_breached")
        if (
            positions_valid
            and target_market_value is not None
            and options_valid
            and target_existing_assignment_exposure is not None
            and net_liquidation is not None
            and net_liquidation > 0
            and max_concentration is not None
            and (
                target_market_value
                + target_existing_assignment_exposure
                + gross_cash_reserved
            )
            / net_liquidation
            > max_concentration
        ):
            _add(rejections, "post_assignment_concentration_breached")

    if rejections:
        return _base_result(
            status="rejected",
            rejections=rejections,
            data_mode=data_mode,
        )

    # Every operand is validated above.  Metrics are intentionally computed
    # only after all gates pass; Bid is the sole premium assumption.
    assert symbol is not None
    assert expiration is not None
    assert contract_key is not None
    assert snapshot_id is not None
    assert dte is not None
    assert strike is not None
    assert bid is not None
    assert ask is not None
    assert spread_ratio is not None
    assert underlying_price is not None
    assert gross_cash_reserved is not None
    assert target_market_value is not None
    assert target_existing_assignment_exposure is not None
    assert provenance_as_of is not None
    assert provenance_received is not None
    assert account_as_of is not None
    assert account_received is not None
    assert known_at is not None
    assert coverage_through is not None
    assert option_quote is not None
    assert underlying_quote is not None
    option_event_ts = _timestamp(option_quote.get("event_ts"))
    option_recv_ts = _timestamp(option_quote.get("recv_ts"))
    underlying_event_ts = _timestamp(underlying_quote.get("event_ts"))
    underlying_recv_ts = _timestamp(underlying_quote.get("recv_ts"))
    assert option_event_ts is not None
    assert option_recv_ts is not None
    assert underlying_event_ts is not None
    assert underlying_recv_ts is not None
    premium_at_bid = bid * CONTRACT_MULTIPLIER
    breakeven = strike - bid
    stock_zero_loss = gross_cash_reserved - premium_at_bid
    breakeven_cushion = (underlying_price - breakeven) / underlying_price
    simple_annualized_yield = (
        premium_at_bid
        / gross_cash_reserved
        * Decimal("365")
        / Decimal(dte)
    )
    post_assignment_concentration = (
        (
            target_market_value
            + target_existing_assignment_exposure
            + gross_cash_reserved
        )
        / net_liquidation
    )

    result = _base_result(
        status="research_candidate",
        rejections=[],
        data_mode=data_mode,
    )
    result.update(
        {
            "risk_budget": risk_budget,
            "evidence": {
                "decision_at": decision_at.isoformat(),
                "market_data": {
                    "provider": provider,
                    "dataset": dataset,
                    "license_mode": license_mode,
                    "permitted_use": permitted_use,
                    "data_mode": data_mode,
                    "snapshot_id": snapshot_id,
                    "as_of": provenance_as_of.isoformat(),
                    "received_at": provenance_received.isoformat(),
                    "source_hash": source_hash.lower(),
                },
                "option_quote": {
                    "event_ts": option_event_ts.isoformat(),
                    "recv_ts": option_recv_ts.isoformat(),
                },
                "underlying_quote": {
                    "event_ts": underlying_event_ts.isoformat(),
                    "recv_ts": underlying_recv_ts.isoformat(),
                },
                "event_calendar_known_at": known_at.isoformat(),
                "event_calendar_coverage_through": coverage_through.isoformat(),
                "account_as_of": account_as_of.isoformat(),
                "account_received_at": account_received.isoformat(),
            },
            "contract": {
                "underlying": symbol,
                "contract_key": contract_key,
                "expiration": expiration.isoformat(),
                "dte_calendar_days": dte,
                "strike_usd": _money(strike),
                "multiplier": 100,
            },
            "one_contract_metrics": {
                "gross_cash_reserved_usd": _money(gross_cash_reserved),
                "premium_at_bid_usd": _money(premium_at_bid),
                "breakeven_usd_per_share": _money(breakeven),
                "max_profit_usd": _money(premium_at_bid),
                "stock_zero_loss_usd": _money(stock_zero_loss),
                "breakeven_cushion_ratio": _ratio(breakeven_cushion),
                "breakeven_cushion_bps": _bps(breakeven_cushion),
                "simple_annualized_premium_yield_ratio": _ratio(
                    simple_annualized_yield
                ),
                "simple_annualized_premium_yield_bps": _bps(
                    simple_annualized_yield
                ),
                "spread_ratio": _ratio(spread_ratio),
                "spread_bps": _bps(spread_ratio),
                "post_assignment_underlying_ratio": _ratio(
                    post_assignment_concentration
                ),
                "post_assignment_underlying_bps": _bps(
                    post_assignment_concentration
                ),
            },
            "metric_notes": [
                "premium_at_bid 使用 Bid 作为研究卖价假设，不假设更优成交。",
                "simple_annualized_premium_yield 是单利年化，不是 CAGR。",
                "spread_ratio 按 (Ask-Bid)/Bid 计算，8% 为含边界上限。",
                "Delta 只用于区间筛选；Delta 不是到期获利概率。",
                "gross_cash_reserved 按行权价乘 100 全额计提，不扣除权利金。",
                "接货后集中度包含同标的现货、已有现金担保短 Put 的潜在接货口径与本次一张毛担保。",
                "prepare_reduce 时 policy.risk_limits 必须是上游收紧后的有效限额，本内核不推导缩放比例。",
            ],
        }
    )
    if delta is not None:
        result["derived"] = {
            "contract_key": contract_key,
            "snapshot_id": snapshot_id,
            "delta": _ratio(delta),
            "delta_is_probability": False,
        }
    return result


__all__ = [
    "ACCOUNT_MAX_AGE_SECONDS",
    "EVENT_CALENDAR_MAX_AGE_SECONDS",
    "MAX_DTE",
    "MAX_SPREAD_RATIO",
    "METHOD_VERSION",
    "MIN_DTE",
    "MIN_OPEN_INTEREST",
    "MIN_VOLUME",
    "SCHEMA_VERSION",
    "evaluate_cash_secured_put",
]
