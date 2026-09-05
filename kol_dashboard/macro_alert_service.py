"""Deterministic, fail-closed market de-risk alerts for the macro page.

The engine deliberately separates evidence collection from action labelling:
news and LLM text never change an action.  A candidate requires independently
observed market closes and pressure indicators, and every action remains a
manual review prompt rather than an executable trade instruction.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
METHOD_VERSION = "macro-de-risk-trial-v1"
MODE = "trial"
MIN_DAILY_BARS = 60
MAX_MARKET_AGE_DAYS = 5
MAX_OFR_AGE_DAYS = 6

_ACTION_LABELS = {
    "observe": "继续观察",
    "prepare_reduce": "减仓准备",
    "reduce_candidate": "减仓候选",
    "exit_candidate": "防御 / 清仓审查",
}
_ACTION_RANK = {
    "observe": 0,
    "prepare_reduce": 1,
    "reduce_candidate": 2,
    "exit_candidate": 3,
}
_SERIES_SPECS = {
    "us_equity": {
        "asset_key": "US:SPY",
        "label": "标普 500（SPY）",
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com/quote/SPY/history/",
    },
    "cn_equity": {
        "asset_key": "INDEX:CSI300",
        "label": "沪深 300",
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com/quote/000300.SS/history/",
    },
    "vix_daily": {
        "asset_key": "INDEX:VIX",
        "label": "VIX 日线",
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com/quote/%5EVIX/history/",
    },
    "usd_cny_daily": {
        "asset_key": "FX:USD/CNY",
        "label": "美元 / 人民币日线",
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com/quote/CNY%3DX/history/",
    },
}
_TENCENT_CSI300_SOURCE = {
    "provider": "tencent",
    "source": "腾讯行情",
    "source_url": "https://gu.qq.com/sh000300",
}
_MARKET_SPECS = {
    "US": {
        "label": "美股",
        "equity_key": "us_equity",
        "drawdown_threshold": -10.0,
        "required": ("us_equity", "vix_daily", "financial_stress"),
    },
    "CN": {
        "label": "A股",
        "equity_key": "cn_equity",
        "drawdown_threshold": -12.0,
        "required": (
            "cn_equity",
            "usd_cny_daily",
            "vix_daily",
            "financial_stress",
        ),
    },
}


def series_specs() -> dict[str, dict[str, str]]:
    """Return a copy of the fixed, auditable history-source specification."""
    return {key: dict(value) for key, value in _SERIES_SPECS.items()}


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return current.astimezone(timezone.utc)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if len(raw) == 10:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time())
            return parsed.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _market_date(timestamp: int, timezone_name: Any) -> str:
    try:
        zone = ZoneInfo(str(timezone_name or "UTC"))
    except (KeyError, ValueError):
        zone = timezone.utc
    return datetime.fromtimestamp(timestamp, tz=zone).date().isoformat()


def _unavailable_summary(
    key: str,
    *,
    reason: str,
    bars_available: int = 0,
) -> dict[str, Any]:
    spec = _SERIES_SPECS[key]
    return {
        "status": "unavailable",
        "data_status": "unavailable",
        "stale": False,
        "asset_key": spec["asset_key"],
        "label": spec["label"],
        "provider": "yahoo",
        "source": spec["source"],
        "source_url": spec["source_url"],
        "observed_at": None,
        "market_date": None,
        "bars_available": max(0, int(bars_available)),
        "reason": reason,
    }


def summarize_daily_history(
    history: Any,
    *,
    series_key: str,
    now: datetime | None = None,
    minimum_bars: int = MIN_DAILY_BARS,
    stale_after_days: int = MAX_MARKET_AGE_DAYS,
) -> dict[str, Any]:
    """Reduce raw daily bars to a bounded, auditable trend observation.

    Future/unfinished bars are excluded.  Raw bars never enter the macro
    snapshot, which keeps the public API small and avoids accidental data
    redistribution beyond the derived facts needed by the rule engine.
    """
    if series_key not in _SERIES_SPECS:
        raise ValueError("unsupported series_key")
    current = _utc_now(now)
    if not isinstance(history, Mapping) or history.get("status") != "available":
        reason = (
            str(history.get("reason_code") or history.get("reason") or "source_unavailable")
            if isinstance(history, Mapping)
            else "source_unavailable"
        )
        return _unavailable_summary(series_key, reason=reason[:64])
    raw_bars = history.get("bars")
    if not isinstance(raw_bars, list):
        return _unavailable_summary(series_key, reason="invalid_bars")

    latest_allowed = current.timestamp() + 300
    by_timestamp: dict[int, float] = {}
    for raw_bar in raw_bars:
        if not isinstance(raw_bar, Mapping):
            continue
        timestamp_value = _finite(raw_bar.get("timestamp"))
        close = _finite(raw_bar.get("close"))
        if timestamp_value is None or close is None or close <= 0:
            continue
        timestamp = int(timestamp_value)
        if timestamp <= 0 or timestamp > latest_allowed:
            continue
        by_timestamp[timestamp] = close

    points = sorted(by_timestamp.items())
    minimum = max(MIN_DAILY_BARS, int(minimum_bars))
    if len(points) < minimum:
        return _unavailable_summary(
            series_key,
            reason="insufficient_completed_bars",
            bars_available=len(points),
        )

    timestamps = [item[0] for item in points]
    closes = [item[1] for item in points]
    latest_timestamp = timestamps[-1]
    latest_at = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc)
    age_seconds = max(0.0, (current - latest_at).total_seconds())
    stale = age_seconds > max(1, int(stale_after_days)) * 86_400
    latest = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma60 = sum(closes[-60:]) / 60
    prior_sma20 = sum(closes[-25:-5]) / 20
    prior_5d = closes[-6]
    peak_60d = max(closes[-60:])
    return_5d = (latest / prior_5d - 1) * 100
    drawdown_60d = (latest / peak_60d - 1) * 100
    slope_5d = (sma20 / prior_sma20 - 1) * 100
    fingerprint = hashlib.sha256(
        json.dumps(points[-60:], separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    spec = _SERIES_SPECS[series_key]
    provider = str(history.get("provider") or "yahoo").strip().lower()
    source = spec["source"]
    source_url = spec["source_url"]
    if series_key == "cn_equity" and provider == "tencent":
        source = _TENCENT_CSI300_SOURCE["source"]
        source_url = _TENCENT_CSI300_SOURCE["source_url"]
    return {
        "status": "available",
        "data_status": "stale" if stale else "ok",
        "stale": stale,
        "asset_key": spec["asset_key"],
        "label": spec["label"],
        "provider": provider,
        "source": source,
        "source_url": source_url,
        "symbol": history.get("symbol"),
        "currency": history.get("currency"),
        "observed_at": _iso(latest_at),
        "market_date": _market_date(
            latest_timestamp,
            history.get("exchange_timezone"),
        ),
        "timestamp_semantics": history.get("timestamp_semantics") or "provider",
        "bars_available": len(points),
        "close": round(latest, 6),
        "sma20": round(sma20, 6),
        "sma60": round(sma60, 6),
        "sma20_slope_5d_pct": round(slope_5d, 3),
        "return_5d_pct": round(return_5d, 3),
        "drawdown_60d_pct": round(drawdown_60d, 3),
        "data_hash": fingerprint,
    }


def _history_input(market_data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    inputs = market_data.get("alert_inputs")
    if not isinstance(inputs, Mapping):
        return {}
    item = inputs.get(key)
    return item if isinstance(item, Mapping) else {}


def _usable_history(item: Mapping[str, Any]) -> bool:
    return (
        item.get("status") == "available"
        and item.get("data_status") == "ok"
        and item.get("stale") is not True
        and _parse_time(item.get("observed_at")) is not None
    )


def _usable_ofr(item: Mapping[str, Any], now: datetime) -> bool:
    observed = _parse_time(item.get("observed_at"))
    if (
        item.get("data_status") != "ok"
        or item.get("stale") is True
        or _finite(item.get("ofr_fsi")) is None
        or observed is None
    ):
        return False
    age = (now.date() - observed.date()).days
    return 0 <= age <= MAX_OFR_AGE_DAYS


def _signal(
    *,
    key: str,
    pillar: str,
    label: str,
    severity: str,
    value: float,
    unit: str,
    threshold: str,
    source_item: Mapping[str, Any],
    detail: str,
    time_basis: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "pillar": pillar,
        "label": label,
        "severity": severity,
        "value": round(value, 3),
        "unit": unit,
        "threshold": threshold,
        "observed_at": source_item.get("observed_at"),
        "time_basis": time_basis,
        "source": source_item.get("source"),
        "source_url": source_item.get("source_url"),
        "detail": detail,
    }


def _append_date(values: Any, market_date: str, *, maximum: int = 3) -> list[str]:
    output = [
        value
        for value in values
        if isinstance(value, str) and len(value) == 10
    ] if isinstance(values, list) else []
    if market_date not in output:
        output.append(market_date)
    return output[-maximum:]


def _previous_market(previous_snapshot: Any, market: str) -> Mapping[str, Any]:
    if not isinstance(previous_snapshot, Mapping):
        return {}
    alerts = previous_snapshot.get("market_alerts")
    if not isinstance(alerts, Mapping) or alerts.get("method_version") != METHOD_VERSION:
        return {}
    markets = alerts.get("markets")
    if not isinstance(markets, list):
        return {}
    for item in markets:
        if isinstance(item, Mapping) and item.get("market") == market:
            return item
    return {}


def _oldest_iso(values: list[Any]) -> str | None:
    parsed = [value for value in (_parse_time(item) for item in values) if value]
    return _iso(min(parsed)) if parsed else None


def _next_evaluation(now: datetime) -> str:
    return _iso((now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))


def _build_market_alert(
    report: Mapping[str, Any],
    *,
    market: str,
    previous_snapshot: Any,
    now: datetime,
) -> dict[str, Any]:
    spec = _MARKET_SPECS[market]
    market_data = report.get("market_data")
    market_data = market_data if isinstance(market_data, Mapping) else {}
    equity = _history_input(market_data, spec["equity_key"])
    vix = _history_input(market_data, "vix_daily")
    fx = _history_input(market_data, "usd_cny_daily")
    ofr_raw = market_data.get("financial_stress")
    ofr = ofr_raw if isinstance(ofr_raw, Mapping) else {}

    availability = {
        spec["equity_key"]: _usable_history(equity),
        "vix_daily": _usable_history(vix),
        "usd_cny_daily": _usable_history(fx),
        "financial_stress": _usable_ofr(ofr, now),
    }
    labels = {
        "us_equity": "SPY 完成收盘日线",
        "cn_equity": "沪深 300 完成收盘日线",
        "vix_daily": "VIX 完成收盘日线",
        "usd_cny_daily": "美元 / 人民币完成日线",
        "financial_stress": "OFR FSI 官方日度数据",
    }
    missing_sources = [
        labels[key]
        for key in spec["required"]
        if not availability.get(key, False)
    ]
    abstain = bool(missing_sources)

    triggered: list[dict[str, Any]] = []
    counter: list[dict[str, Any]] = []
    price_watch = price_confirmed = price_severe = False
    equity_close = _finite(equity.get("close"))
    equity_sma20 = _finite(equity.get("sma20"))
    equity_sma60 = _finite(equity.get("sma60"))
    equity_slope = _finite(equity.get("sma20_slope_5d_pct"))
    equity_drawdown = _finite(equity.get("drawdown_60d_pct"))
    if all(
        value is not None
        for value in (
            equity_close,
            equity_sma20,
            equity_sma60,
            equity_slope,
            equity_drawdown,
        )
    ):
        price_watch = bool(equity_close < equity_sma20 or equity_slope < 0)
        price_confirmed = bool(equity_close < equity_sma20 and equity_slope < 0)
        price_severe = bool(
            equity_close < equity_sma60
            and equity_sma20 < equity_sma60
            and equity_drawdown <= spec["drawdown_threshold"]
        )
        if price_confirmed:
            triggered.append(
                _signal(
                    key=f"{market.lower()}_trend_warning",
                    pillar="trend",
                    label=f"{equity.get('label') or spec['label']} 趋势确认转弱",
                    severity="critical" if price_severe else "strong",
                    value=equity_close,
                    unit=str(equity.get("currency") or "index"),
                    threshold="收盘 < SMA20 且 SMA20 五日斜率 < 0（试运行）",
                    source_item=equity,
                    detail=(
                        f"SMA20={equity_sma20:.2f}，SMA60={equity_sma60:.2f}，"
                        f"60日回撤={equity_drawdown:.2f}%"
                    ),
                    time_basis="completed_market_close",
                )
            )
        elif price_watch:
            triggered.append(
                _signal(
                    key=f"{market.lower()}_trend_watch",
                    pillar="trend",
                    label=f"{equity.get('label') or spec['label']} 趋势接近确认线",
                    severity="watch",
                    value=equity_close,
                    unit=str(equity.get("currency") or "index"),
                    threshold="收盘 < SMA20 或 SMA20 五日斜率 < 0（试运行）",
                    source_item=equity,
                    detail=f"SMA20={equity_sma20:.2f}，五日斜率={equity_slope:.2f}%",
                    time_basis="completed_market_close",
                )
            )
        else:
            counter.append(
                _signal(
                    key=f"{market.lower()}_trend_counter",
                    pillar="trend",
                    label=f"{equity.get('label') or spec['label']} 趋势尚未转弱",
                    severity="watch",
                    value=equity_close,
                    unit=str(equity.get("currency") or "index"),
                    threshold="收盘 ≥ SMA20 且 SMA20 五日斜率 ≥ 0（试运行）",
                    source_item=equity,
                    detail=f"SMA20={equity_sma20:.2f}，五日斜率={equity_slope:.2f}%",
                    time_basis="completed_market_close",
                )
            )

    strong_pressure: set[str] = set()
    critical_pressure: set[str] = set()
    vix_value = _finite(vix.get("close"))
    if vix_value is not None:
        if vix_value >= 35:
            strong_pressure.add("volatility")
            critical_pressure.add("volatility")
            triggered.append(
                _signal(
                    key="vix_critical",
                    pillar="volatility",
                    label="VIX 进入极端压力区",
                    severity="critical",
                    value=vix_value,
                    unit="index",
                    threshold="VIX ≥ 35（试运行阈值）",
                    source_item=vix,
                    detail="VIX 是未来约30天的隐含波动率，不代表市场方向。",
                    time_basis="completed_market_close",
                )
            )
        elif vix_value >= 28:
            strong_pressure.add("volatility")
            triggered.append(
                _signal(
                    key="vix_strong",
                    pillar="volatility",
                    label="VIX 波动压力抬升",
                    severity="strong",
                    value=vix_value,
                    unit="index",
                    threshold="VIX ≥ 28（试运行阈值）",
                    source_item=vix,
                    detail="仅作为独立压力支柱，不能单独决定减仓。",
                    time_basis="completed_market_close",
                )
            )
        else:
            counter.append(
                _signal(
                    key="vix_counter",
                    pillar="volatility",
                    label="VIX 未达试运行压力线",
                    severity="watch",
                    value=vix_value,
                    unit="index",
                    threshold="VIX < 28（试运行阈值）",
                    source_item=vix,
                    detail="这是反向证据，不等同于市场安全。",
                    time_basis="completed_market_close",
                )
            )

    ofr_value = _finite(ofr.get("ofr_fsi"))
    if ofr_value is not None:
        if ofr_value > 5:
            strong_pressure.add("financial_stress")
            critical_pressure.add("financial_stress")
            triggered.append(
                _signal(
                    key="ofr_fsi_critical",
                    pillar="financial_stress",
                    label="OFR 系统性金融压力极高",
                    severity="critical",
                    value=ofr_value,
                    unit="index",
                    threshold="OFR FSI > 5（试运行阈值）",
                    source_item=ofr,
                    detail="OFR FSI 为日度全球金融压力指标，通常滞后约两个工作日。",
                    time_basis="official_daily_observation",
                )
            )
        elif ofr_value > 2:
            strong_pressure.add("financial_stress")
            triggered.append(
                _signal(
                    key="ofr_fsi_strong",
                    pillar="financial_stress",
                    label="OFR 系统性金融压力显著抬升",
                    severity="strong",
                    value=ofr_value,
                    unit="index",
                    threshold="OFR FSI > 2（试运行阈值）",
                    source_item=ofr,
                    detail="正值表示高于历史平均；本阈值尚待走步回测校准。",
                    time_basis="official_daily_observation",
                )
            )
        else:
            counter.append(
                _signal(
                    key="ofr_fsi_counter",
                    pillar="financial_stress",
                    label="OFR FSI 未达试运行压力线",
                    severity="watch",
                    value=ofr_value,
                    unit="index",
                    threshold="OFR FSI ≤ 2（试运行阈值）",
                    source_item=ofr,
                    detail="该项未形成系统性压力确认。",
                    time_basis="official_daily_observation",
                )
            )

    fx_warning = fx_severe = False
    fx_rate = _finite(fx.get("close"))
    fx_return = _finite(fx.get("return_5d_pct"))
    if market == "CN" and fx_rate is not None and fx_return is not None:
        fx_warning = fx_rate >= 7.20 and fx_return >= 0.5
        fx_severe = fx_rate >= 7.30 and fx_return >= 1.0
        if fx_warning:
            strong_pressure.add("fx_liquidity")
            if fx_severe:
                critical_pressure.add("fx_liquidity")
            triggered.append(
                _signal(
                    key="usd_cny_severe" if fx_severe else "usd_cny_strong",
                    pillar="fx_liquidity",
                    label="人民币汇率压力连续抬升",
                    severity="critical" if fx_severe else "strong",
                    value=fx_rate,
                    unit="CNY per USD",
                    threshold=(
                        "USD/CNY ≥ 7.30 且五日升幅 ≥ 1%（试运行）"
                        if fx_severe
                        else "USD/CNY ≥ 7.20 且五日升幅 ≥ 0.5%（试运行）"
                    ),
                    source_item=fx,
                    detail=f"五日变化={fx_return:.2f}%；只与A股趋势共同判断。",
                    time_basis="completed_market_close",
                )
            )
        else:
            counter.append(
                _signal(
                    key="usd_cny_counter",
                    pillar="fx_liquidity",
                    label="人民币汇率未形成双条件压力",
                    severity="watch",
                    value=fx_rate,
                    unit="CNY per USD",
                    threshold="汇率水平与五日变化未同时越线（试运行）",
                    source_item=fx,
                    detail=f"五日变化={fx_return:.2f}%",
                    time_basis="completed_market_close",
                )
            )

    raw_reduce = price_confirmed and bool(strong_pressure)
    raw_exit = (
        price_severe
        and len(critical_pressure) >= 2
        and (market != "CN" or "fx_liquidity" in critical_pressure)
    )
    market_date = str(equity.get("market_date") or "")
    previous = _previous_market(previous_snapshot, market)
    previous_confirmation = previous.get("confirmation")
    previous_confirmation = (
        previous_confirmation if isinstance(previous_confirmation, Mapping) else {}
    )
    reduce_dates = (
        _append_date(previous_confirmation.get("reduce_dates"), market_date)
        if (raw_reduce or raw_exit) and market_date
        else []
    )
    exit_dates = (
        _append_date(previous_confirmation.get("exit_dates"), market_date)
        if raw_exit and market_date
        else []
    )

    action = "observe"
    if raw_reduce or raw_exit:
        action = "prepare_reduce"
    elif price_watch or strong_pressure:
        action = "prepare_reduce"
    if len(reduce_dates) >= 2:
        action = "reduce_candidate"
    previous_action = str(previous.get("action") or "observe")
    if (
        len(exit_dates) >= 2
        and previous_action in {"reduce_candidate", "exit_candidate"}
    ):
        action = "exit_candidate"

    recovery_dates: list[str] = []
    recovery_pending = False
    previous_condition_still_active = (
        previous_action == "reduce_candidate" and (raw_reduce or raw_exit)
    ) or (
        previous_action == "exit_candidate" and raw_exit
    )
    if (
        not abstain
        and previous_condition_still_active
        and _ACTION_RANK.get(previous_action, 0) > _ACTION_RANK[action]
    ):
        action = previous_action
    elif not abstain and _ACTION_RANK.get(previous_action, 0) > _ACTION_RANK[action]:
        recovery_dates = _append_date(
            previous_confirmation.get("recovery_dates"),
            market_date,
        ) if market_date else []
        if len(recovery_dates) < 2:
            action = previous_action
            recovery_pending = True
    if abstain:
        action = "observe"
        reduce_dates = []
        exit_dates = []
        recovery_dates = []
        recovery_pending = False

    required_times: list[Any] = []
    for required in spec["required"]:
        if required == "financial_stress":
            required_times.append(ofr.get("observed_at"))
        else:
            required_times.append(_history_input(market_data, required).get("observed_at"))

    if abstain:
        summary = "核心数据缺失或陈旧，系统停止升级仓位动作；这不是低风险结论。"
    elif recovery_pending:
        summary = "风险条件首次解除，等待第二个不同收盘日确认后再降级，避免预警来回跳变。"
    elif action == "exit_candidate":
        summary = "极端趋势与多个压力支柱连续共振，进入防御 / 清仓审查；必须人工确认。"
    elif action == "reduce_candidate":
        summary = "趋势与独立压力支柱已连续两个不同收盘日共振，进入减仓候选复核。"
    elif action == "prepare_reduce":
        summary = "风险条件接近或首次触发，等待独立收盘确认；当前不构成减仓指令。"
    else:
        summary = "尚未形成跨支柱风险共振，继续观察已列出的触发条件与反向证据。"

    pressure_detail = (
        f"已触发 {len(strong_pressure)} 个压力支柱；"
        f"其中 {len(critical_pressure)} 个达到极端线"
    )
    gates = [
        {
            "key": "data_freshness",
            "label": "核心数据新鲜度",
            "status": "unavailable" if abstain else "met",
            "detail": (
                "缺少：" + "、".join(missing_sources)
                if missing_sources
                else "全部必需输入均来自允许时效内的完成日线或官方观测"
            ),
        },
        {
            "key": "price_confirmation",
            "label": "价格趋势确认",
            "status": "met" if price_confirmed else "partial" if price_watch else "unmet",
            "detail": (
                "短趋势已确认转弱" if price_confirmed else
                "仅一个趋势条件转弱" if price_watch else
                "短趋势尚未确认转弱"
            ),
        },
        {
            "key": "independent_pressure",
            "label": "独立压力共振",
            "status": "met" if strong_pressure else "unmet",
            "detail": pressure_detail,
        },
        {
            "key": "close_confirmation",
            "label": "跨收盘日确认",
            "status": (
                "met" if len(reduce_dates) >= 2 else
                "partial" if len(reduce_dates) == 1 else
                "unmet"
            ),
            "detail": f"减仓条件已由 {len(reduce_dates)}/2 个不同收盘日确认",
        },
    ]
    gate_progress = {
        "met": sum(1 for gate in gates if gate["status"] == "met"),
        "total": len(gates),
    }
    risk_level = {
        "observe": "insufficient" if abstain else "low",
        "prepare_reduce": "medium",
        "reduce_candidate": "high",
        "exit_candidate": "critical",
    }[action]

    if market == "US":
        upgrade_conditions = [
            "减仓候选：SPY 收盘低于 SMA20、SMA20 五日斜率为负，并与 VIX≥28 或 OFR FSI>2 连续两个不同收盘日共振。",
            "防御 / 清仓审查：SPY 跌破 SMA60、60日回撤≤-10%，且 VIX≥35 与 OFR FSI>5 连续确认；此前至少已处于减仓候选。",
        ]
    else:
        upgrade_conditions = [
            "减仓候选：沪深300趋势转弱，并与汇率、VIX 或 OFR 中至少一个压力支柱连续两个不同收盘日共振。",
            "防御 / 清仓审查：沪深300跌破 SMA60、60日回撤≤-12%，人民币汇率达到极端线且另一个全球压力支柱极端化；此前至少已处于减仓候选。",
        ]

    return {
        "market": market,
        "label": spec["label"],
        "action": action,
        "action_label": _ACTION_LABELS[action],
        "risk_level": risk_level,
        "abstain": abstain,
        "data_status": "insufficient" if abstain else "ok",
        "data_as_of": _oldest_iso(required_times),
        "next_evaluation_at": _next_evaluation(now),
        "summary": summary,
        "gate_progress": gate_progress,
        "gates": gates,
        "triggered_signals": triggered,
        "counter_signals": counter,
        "upgrade_conditions": upgrade_conditions,
        "invalidation_conditions": [
            "风险条件连续两个不同收盘日解除后才降级，避免单日反弹造成误判。",
            "任一核心数据缺失、陈旧或出现未来时间戳时，系统停止升级并显示证据不足。",
            "新闻、LLM 解读和静态场景不会单独改变仓位动作。",
        ],
        "missing_sources": missing_sources,
        "rule_version": METHOD_VERSION,
        "confirmation": {
            "raw_reduce": raw_reduce,
            "raw_exit": raw_exit,
            "reduce_dates": reduce_dates,
            "exit_dates": exit_dates,
            "recovery_dates": recovery_dates,
            "recovery_pending": recovery_pending,
        },
    }


def build_market_alerts(
    report: Any,
    *,
    previous_snapshot: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build market-level review candidates without reading any holdings."""
    current = _utc_now(now)
    safe_report = report if isinstance(report, Mapping) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "generated_at": _iso(current),
        "mode": MODE,
        "human_review_required": True,
        "automatic_execution": False,
        "markets": [
            _build_market_alert(
                safe_report,
                market=market,
                previous_snapshot=previous_snapshot,
                now=current,
            )
            for market in ("US", "CN")
        ],
    }
