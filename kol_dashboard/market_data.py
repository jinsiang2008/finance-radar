"""Defensive market-data adapters and deterministic event-reaction math.

Provider data is evidence of price association only.  It does not establish
that an event caused a market move.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time as datetime_time, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


REACTION_METHOD_VERSION = "common_trading_days:v1"
DEFAULT_TIMEOUT_SECONDS = 8.0

_YAHOO_EXPLICIT = {
    "COMMODITY:GOLD": "GC=F",
    "COMMODITY:OIL": "CL=F",
    "FX:DXY": "DX-Y.NYB",
    "FX:USD/CNY": "CNY=X",
    "FX:JPY": "JPY=X",
    "FX:USD/JPY": "JPY=X",
    "FX:EUR/USD": "EURUSD=X",
    "INDEX:VIX": "^VIX",
    "INDEX:NIKKEI": "^N225",
    "INDEX:CSI300": "000300.SS",
    "BOND:UST_LONG": "TLT",
    "BOND:UST_INTERMEDIATE": "IEF",
    "BOND:UST_SHORT": "SHY",
    "BOND:UST_10Y_YIELD": "^TNX",
}

# Themes are analytical concepts rather than directly traded instruments.  Keep
# their price proxies separate from provider_symbol() so a bare symbol lookup can
# never silently turn a theme into an ETF.  resolve_provider_asset() carries the
# explicit ``proxy_for`` audit trail used by the collector and public API.
_THEME_PRICE_PROXIES = {
    "THEME:AI": "US:SOXX",
    "THEME:SEMICONDUCTOR": "US:SOXX",
    "THEME:CHINA_EQUITY": "US:MCHI",
    "THEME:EMERGING_MARKETS": "US:EEM",
    "THEME:GLOBAL_RISK_ASSETS": "US:ACWI",
    "THEME:FINANCIALS": "US:XLF",
    "THEME:CRYPTO": "CRYPTO:BTC",
}

_THEME_BENCHMARKS = {
    "THEME:AI": "US:SPY",
    "THEME:SEMICONDUCTOR": "US:SPY",
    "THEME:CHINA_EQUITY": "US:SPY",
    "THEME:EMERGING_MARKETS": "US:SPY",
    "THEME:GLOBAL_RISK_ASSETS": "US:SPY",
    "THEME:FINANCIALS": "US:SPY",
    "THEME:CRYPTO": "US:SPY",
}

MARKET_REASON_CODES = frozenset(
    {
        "asset_unavailable",
        "bad_payload",
        "baseline_unavailable",
        "benchmark_unavailable",
        "follow_up_unavailable",
        "insufficient_follow_up",
        "invalid_event_time",
        "invalid_interval",
        "invalid_range",
        "invalid_timeout",
        "no_common_trading_dates",
        "provider_error",
        "request_failed",
        "same_proxy_as_benchmark",
        "unknown",
        "unsupported_asset",
        "unsupported_benchmark",
        "window_not_due",
    }
)

_BENCHMARK_PROXIES = {
    "COMMODITY:GOLD": "US:GLD",
    "COMMODITY:OIL": "US:USO",
    "FX:DXY": "US:UUP",
    "FX:USD/CNY": "FX:DXY",
    "FX:JPY": "FX:DXY",
    "FX:USD/JPY": "FX:DXY",
    "FX:EUR/USD": "FX:DXY",
    "INDEX:VIX": "US:VXX",
    "INDEX:NIKKEI": "US:EWJ",
    "INDEX:CSI300": "CN:510300",
    "BOND:UST_LONG": "US:IEF",
    "BOND:UST_INTERMEDIATE": "US:SHY",
    "BOND:UST_10Y_YIELD": "US:IEF",
}

_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_CN_SYMBOL_RE = re.compile(r"^\d{6}$")
_CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_TENCENT_VALUE_RE = re.compile(
    r"""v_([a-z]{2}\d{6})\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)
_MARKET_CLOSE_LOCAL = {
    "America/New_York": (16, 0),
    "America/Toronto": (16, 0),
    "Asia/Shanghai": (15, 0),
    "Asia/Hong_Kong": (16, 0),
    "Asia/Tokyo": (15, 0),
    "Europe/London": (16, 30),
}
_US_CLOSING_INDEXES = {
    "INDEX:DOW",
    "INDEX:NASDAQ",
    "INDEX:SPX",
    "INDEX:VIX",
}


def _split_asset_key(asset_key: Any) -> tuple[str, str] | None:
    if not isinstance(asset_key, str):
        return None
    value = asset_key.strip()
    if value.count(":") != 1:
        return None
    prefix, body = value.split(":", 1)
    prefix = prefix.strip().upper()
    body = body.strip().upper()
    if not prefix or not body:
        return None
    return prefix, body


def _cn_exchange(code: str) -> str | None:
    if not _CN_SYMBOL_RE.fullmatch(code):
        return None
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("5", "6", "9", "110", "113", "118")):
        return "SS"
    if code.startswith(("0", "1", "2", "3")):
        return "SZ"
    return None


def provider_symbol(asset_key: Any, provider: str = "yahoo") -> str | None:
    """Map a canonical asset key to a provider symbol without fuzzy guessing."""
    parts = _split_asset_key(asset_key)
    provider_name = str(provider or "").strip().lower()
    if parts is None or provider_name not in {"yahoo", "tencent"}:
        return None
    prefix, body = parts

    if provider_name == "tencent":
        if prefix != "CN":
            return None
        exchange = _cn_exchange(body)
        tencent_exchange = {"SS": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
        return f"{tencent_exchange}{body}" if tencent_exchange else None

    explicit = _YAHOO_EXPLICIT.get(f"{prefix}:{body}")
    if explicit:
        return explicit
    if prefix == "US" and _US_SYMBOL_RE.fullmatch(body):
        return body.replace(".", "-")
    if prefix == "CN":
        exchange = _cn_exchange(body)
        return f"{body}.{exchange}" if exchange else None
    if prefix == "CRYPTO" and _CRYPTO_SYMBOL_RE.fullmatch(body):
        return f"{body}-USD"
    return None


def normalize_reason_code(value: Any) -> str:
    """Collapse provider details into a bounded, public-safe reason code."""
    candidate = str(value or "").strip().lower().split(":", 1)[0]
    return candidate if candidate in MARKET_REASON_CODES else "unknown"


def resolve_provider_asset(
    asset_key: Any,
    provider: str = "yahoo",
) -> dict[str, str | None]:
    """Resolve an asset to an explicit provider instrument with proxy lineage."""
    parts = _split_asset_key(asset_key)
    provider_name = str(provider or "").strip().lower()
    if parts is None:
        requested = str(asset_key or "").strip() or None
        return {
            "requested_asset_key": requested,
            "price_asset_key": None,
            "provider": provider_name or None,
            "provider_symbol": None,
            "proxy_for": None,
        }
    requested = f"{parts[0]}:{parts[1]}"
    price_asset = _THEME_PRICE_PROXIES.get(requested, requested)
    proxy_for = requested if price_asset != requested else None
    return {
        "requested_asset_key": requested,
        "price_asset_key": price_asset,
        "provider": provider_name,
        "provider_symbol": provider_symbol(price_asset, provider_name),
        "proxy_for": proxy_for,
    }


def yahoo_symbol(asset_key: Any) -> str | None:
    return provider_symbol(asset_key, "yahoo")


def tencent_symbol(asset_key: Any) -> str | None:
    return provider_symbol(asset_key, "tencent")


to_provider_symbol = provider_symbol


def benchmark_for(asset_key: Any, topic: Any = None) -> str | None:
    """Return an explicit comparison proxy, or ``None`` when none is justified."""
    parts = _split_asset_key(asset_key)
    if parts is None:
        return None
    prefix, body = parts
    canonical = f"{prefix}:{body}"
    topic_text = str(topic or "").strip().lower()

    if canonical in _THEME_BENCHMARKS:
        return _THEME_BENCHMARKS[canonical]

    if (
        any(token in topic_text for token in ("ai", "semiconductor", "chip", "半导体", "芯片"))
        and canonical != "US:SOXX"
    ):
        return "US:SOXX"
    if prefix == "US":
        return "US:SPY" if canonical != "US:SPY" else None
    if prefix == "CN":
        return "INDEX:CSI300"
    if prefix == "CRYPTO":
        return "CRYPTO:BTC" if body != "BTC" else None
    return _BENCHMARK_PROXIES.get(canonical)


def _safe_float(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def _safe_timestamp(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())
    if isinstance(value, date):
        return int(
            datetime(
                value.year,
                value.month,
                value.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )
    if isinstance(value, (int, float)):
        number = _safe_float(value, minimum=0)
        if number is None:
            return None
        if number > 10_000_000_000:
            number /= 1000.0
        return int(number)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _safe_timestamp(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _unavailable(
    provider: str,
    *,
    asset_key: Any = None,
    symbol: str | None = None,
    reason: Any = "unavailable",
    bars: bool = False,
) -> dict[str, Any]:
    reason_code = normalize_reason_code(reason)
    result: dict[str, Any] = {
        "status": "unavailable",
        "provider": provider,
        "asset_key": asset_key if isinstance(asset_key, str) else None,
        "symbol": symbol,
        "reason": reason_code,
        "reason_code": reason_code,
    }
    if bars:
        result["bars"] = []
    return result


def _decode_json_payload(payload: Any) -> Any:
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8")
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


def _daily_observation_timestamp(
    timestamp: int,
    asset_key: Any,
    interval: str,
    exchange_timezone: Any,
) -> tuple[int, str]:
    canonical_asset = str(asset_key or "").strip().upper()
    timezone_name = str(exchange_timezone or "").strip()
    is_known_equity_session = (
        (
            canonical_asset.startswith("US:")
            or canonical_asset in _US_CLOSING_INDEXES
        )
        and timezone_name == "America/New_York"
    ) or (
        canonical_asset.startswith("CN:")
        and timezone_name == "Asia/Shanghai"
    )
    if str(interval or "").strip().lower() != "1d" or not is_known_equity_session:
        return timestamp, "provider"
    close_parts = _MARKET_CLOSE_LOCAL.get(timezone_name)
    if not close_parts:
        return timestamp, "provider"
    try:
        zone = ZoneInfo(timezone_name)
        local_day = datetime.fromtimestamp(timestamp, tz=zone).date()
        close_at = datetime.combine(
            local_day,
            datetime_time(close_parts[0], close_parts[1]),
            tzinfo=zone,
        )
    except (ValueError, OverflowError, OSError):
        return timestamp, "provider"
    return int(close_at.astimezone(timezone.utc).timestamp()), "market_close"


def parse_yahoo_chart(
    payload: Any,
    *,
    asset_key: str | None = None,
    symbol: str | None = None,
    interval: str = "1d",
) -> dict[str, Any]:
    """Parse the Yahoo Chart v8 quote stream into validated daily bars."""
    resolved_symbol = symbol or (yahoo_symbol(asset_key) if asset_key else None)
    try:
        decoded = _decode_json_payload(payload)
        chart = decoded["chart"]
        if chart.get("error"):
            return _unavailable(
                "yahoo",
                asset_key=asset_key,
                symbol=resolved_symbol,
                reason="provider_error",
                bars=True,
            )
        result = chart["result"][0]
        timestamps = result["timestamp"]
        indicators = result["indicators"]
        quote = indicators["quote"][0]
        closes = quote["close"]
        adjusted_groups = indicators.get("adjclose") or []
        if adjusted_groups:
            adjusted = adjusted_groups[0].get("adjclose")
            if isinstance(adjusted, list):
                closes = adjusted
        volumes = quote.get("volume") or []
        if (
            not isinstance(timestamps, list)
            or not isinstance(closes, list)
            or not isinstance(volumes, list)
        ):
            raise ValueError("chart arrays are missing")
    except (
        AttributeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        return _unavailable(
            "yahoo",
            asset_key=asset_key,
            symbol=resolved_symbol,
            reason=f"bad_payload:{type(exc).__name__}",
            bars=True,
        )

    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    timestamp_semantics = "provider"
    bars: list[dict[str, Any]] = []
    for index, raw_timestamp in enumerate(timestamps):
        if index >= len(closes):
            break
        timestamp = _safe_timestamp(raw_timestamp)
        close = _safe_float(closes[index], minimum=0.000000000001, maximum=1e15)
        if timestamp is None or close is None:
            continue
        timestamp, semantics = _daily_observation_timestamp(
            timestamp,
            asset_key,
            interval,
            meta.get("exchangeTimezoneName"),
        )
        if semantics == "market_close":
            timestamp_semantics = semantics
        volume = (
            _safe_float(volumes[index], minimum=0, maximum=1e20)
            if index < len(volumes)
            else None
        )
        bars.append(
            {
                "timestamp": timestamp,
                "close": close,
                "volume": volume,
            }
        )
    bars.sort(key=lambda item: item["timestamp"])
    if not bars:
        return _unavailable(
            "yahoo",
            asset_key=asset_key,
            symbol=resolved_symbol,
            reason="no_valid_bars",
            bars=True,
        )
    return {
        "status": "available",
        "provider": "yahoo",
        "asset_key": asset_key,
        "symbol": resolved_symbol or meta.get("symbol"),
        "currency": meta.get("currency"),
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "timestamp_semantics": timestamp_semantics,
        "bars": bars,
    }


def _read_opened_response(response: Any) -> bytes:
    if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
        with response as entered:
            body = entered.read()
    else:
        try:
            body = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    if isinstance(body, str):
        return body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("opener response must provide bytes")
    return bytes(body)


def _call_opener(
    opener: Callable[..., Any] | Any,
    request: urllib.request.Request,
    timeout: float,
) -> bytes:
    operation = getattr(opener, "open", opener)
    response = operation(request, timeout=timeout)
    return _read_opened_response(response)


def fetch_yahoo_history(
    asset_key: str,
    *,
    period1: Any = None,
    period2: Any = None,
    interval: str = "1d",
    range_: str = "1y",
    opener: Callable[..., Any] | Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch Yahoo history through an injectable opener and fail closed."""
    resolution = resolve_provider_asset(asset_key, "yahoo")
    requested_asset = resolution.get("requested_asset_key")
    price_asset = resolution.get("price_asset_key")
    symbol = resolution.get("provider_symbol")

    def resolved(result: dict[str, Any]) -> dict[str, Any]:
        result["asset_key"] = requested_asset
        result["price_asset_key"] = price_asset
        result["proxy_for"] = resolution.get("proxy_for")
        return result

    if symbol is None:
        return resolved(
            _unavailable(
                "yahoo",
                asset_key=requested_asset,
                reason="unsupported_asset",
                bars=True,
            )
        )
    timeout_value = _safe_float(timeout, minimum=0.001, maximum=60.0)
    if timeout_value is None:
        return resolved(
            _unavailable(
                "yahoo",
                asset_key=requested_asset,
                symbol=symbol,
                reason="invalid_timeout",
                bars=True,
            )
        )
    if interval not in {"1d", "1wk", "1mo"}:
        return resolved(
            _unavailable(
                "yahoo",
                asset_key=requested_asset,
                symbol=symbol,
                reason="invalid_interval",
                bars=True,
            )
        )

    query: dict[str, Any] = {
        "interval": interval,
        "events": "history",
        "includeAdjustedClose": "true",
    }
    start = _safe_timestamp(period1)
    end = _safe_timestamp(period2)
    if start is not None and end is not None and start < end:
        query.update({"period1": start, "period2": end})
    else:
        if not re.fullmatch(r"(?:[1-9]\d*(?:d|mo|y)|ytd|max)", str(range_)):
            return resolved(
                _unavailable(
                    "yahoo",
                    asset_key=requested_asset,
                    symbol=symbol,
                    reason="invalid_range",
                    bars=True,
                )
            )
        query["range"] = range_
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol, safe="=^.-")
        + "?"
        + urllib.parse.urlencode(query)
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kol-dashboard-market-validation/1.0",
        },
    )
    try:
        body = _call_opener(opener or urllib.request.urlopen, request, timeout_value)
        return resolved(
            parse_yahoo_chart(
                body,
                asset_key=price_asset,
                symbol=symbol,
                interval=interval,
            )
        )
    except Exception as exc:
        return resolved(
            _unavailable(
                "yahoo",
                asset_key=requested_asset,
                symbol=symbol,
                reason=f"request_failed:{type(exc).__name__}",
                bars=True,
            )
        )


def parse_tencent_quote(
    payload: Any,
    *,
    asset_key: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Parse Tencent's A-share text quote format defensively."""
    resolved_symbol = symbol or (tencent_symbol(asset_key) if asset_key else None)
    try:
        if isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("gb18030")
        elif isinstance(payload, str):
            text = payload
        else:
            raise TypeError("quote payload must be text")
        match = _TENCENT_VALUE_RE.search(text)
        if match is None:
            raise ValueError("quote record missing")
        parsed_symbol = match.group(1).lower()
        if resolved_symbol and parsed_symbol != resolved_symbol.lower():
            raise ValueError("unexpected quote symbol")
        fields = match.group(2).split("~")
        if len(fields) < 6:
            raise ValueError("quote fields missing")
        price = _safe_float(fields[3], minimum=0.000000000001, maximum=1e15)
        if price is None:
            raise ValueError("price unavailable")
        timestamp_text = next(
            (field for field in fields if re.fullmatch(r"\d{14}", field or "")),
            None,
        )
        observed_at = None
        if timestamp_text:
            observed_at = (
                datetime.strptime(timestamp_text, "%Y%m%d%H%M%S")
                .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                .astimezone(timezone.utc)
                .isoformat()
            )
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        return _unavailable(
            "tencent",
            asset_key=asset_key,
            symbol=resolved_symbol,
            reason=f"bad_payload:{type(exc).__name__}",
        )
    return {
        "status": "available",
        "provider": "tencent",
        "asset_key": asset_key,
        "symbol": resolved_symbol or parsed_symbol,
        "name": fields[1],
        "price": price,
        "previous_close": _safe_float(fields[4], minimum=0, maximum=1e15),
        "open": _safe_float(fields[5], minimum=0, maximum=1e15),
        "currency": "CNY",
        "observed_at": observed_at,
    }


def fetch_tencent_quote(
    asset_key: str,
    *,
    opener: Callable[..., Any] | Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch one current A-share quote with an injectable transport."""
    symbol = tencent_symbol(asset_key)
    if symbol is None:
        return _unavailable(
            "tencent", asset_key=asset_key, reason="unsupported_asset"
        )
    timeout_value = _safe_float(timeout, minimum=0.001, maximum=60.0)
    if timeout_value is None:
        return _unavailable(
            "tencent",
            asset_key=asset_key,
            symbol=symbol,
            reason="invalid_timeout",
        )
    request = urllib.request.Request(
        f"https://qt.gtimg.cn/q={urllib.parse.quote(symbol)}",
        headers={"User-Agent": "kol-dashboard-market-validation/1.0"},
    )
    try:
        body = _call_opener(opener or urllib.request.urlopen, request, timeout_value)
        return parse_tencent_quote(body, asset_key=asset_key, symbol=symbol)
    except Exception as exc:
        return _unavailable(
            "tencent",
            asset_key=asset_key,
            symbol=symbol,
            reason=f"request_failed:{type(exc).__name__}",
        )


def _bars_iter(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        bars = value.get("bars")
        return bars if isinstance(bars, list) else []
    return value if isinstance(value, (list, tuple)) else []


def _bars_by_utc_date(value: Any) -> dict[date, dict[str, Any]]:
    selected: dict[date, dict[str, Any]] = {}
    for raw in _bars_iter(value):
        if not isinstance(raw, dict):
            continue
        timestamp = _safe_timestamp(raw.get("timestamp"))
        close = _safe_float(raw.get("close"), minimum=0.000000000001, maximum=1e15)
        if timestamp is None or close is None:
            continue
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        candidate = {
            "timestamp": timestamp,
            "close": close,
            "volume": _safe_float(raw.get("volume"), minimum=0, maximum=1e20),
        }
        current = selected.get(day)
        if current is None or timestamp > current["timestamp"]:
            selected[day] = candidate
    return selected


def _empty_window(window: str, sample_count: int = 0) -> dict[str, Any]:
    return {
        "window": window,
        "status": "unavailable",
        "asset_return": None,
        "benchmark_return": None,
        "abnormal_return": None,
        "direction_confirmed": None,
        "sample_count": max(0, int(sample_count)),
        "data_timestamps": {"start": None, "end": None},
    }


def _unavailable_reaction(reason: str, aligned_count: int = 0) -> dict[str, Any]:
    reason_code = normalize_reason_code(reason)
    return {
        "status": "unavailable",
        "method_version": REACTION_METHOD_VERSION,
        "reason": reason_code,
        "reason_code": reason_code,
        "abstain": True,
        "aligned_sample_count": max(0, int(aligned_count)),
        "data_timestamps": [],
        "windows": {
            window: _empty_window(window) for window in ("1D", "3D", "5D")
        },
    }


def unavailable_event_reaction(
    reason_code: str,
    aligned_count: int = 0,
) -> dict[str, Any]:
    """Build a bounded unavailable result without exposing provider errors."""
    return _unavailable_reaction(reason_code, aligned_count)


def compute_event_reaction(
    asset_bars: Any,
    benchmark_bars: Any,
    event_time: Any,
    *,
    expected_direction: str | None = None,
) -> dict[str, Any]:
    """Compute market-adjusted returns on dates shared by both price series.

    The baseline is the last common close strictly before the UTC event date;
    1D/3D/5D endpoints are the first, third and fifth common trading dates on
    or after that date. Missing dates are excluded rather than treated as zero.
    """
    event_timestamp = _safe_timestamp(event_time)
    if event_timestamp is None:
        return _unavailable_reaction("invalid_event_time")
    asset = _bars_by_utc_date(asset_bars)
    benchmark = _bars_by_utc_date(benchmark_bars)
    common_days = sorted(set(asset).intersection(benchmark))
    if not common_days:
        return _unavailable_reaction("no_common_trading_dates")

    prior_days = [
        day
        for day in common_days
        if asset[day]["timestamp"] < event_timestamp
        and benchmark[day]["timestamp"] < event_timestamp
    ]
    follow_days = [
        day
        for day in common_days
        if asset[day]["timestamp"] >= event_timestamp
        and benchmark[day]["timestamp"] >= event_timestamp
    ]
    if not prior_days:
        return _unavailable_reaction("baseline_unavailable", len(common_days))
    if not follow_days:
        return _unavailable_reaction("follow_up_unavailable", len(common_days))

    baseline_day = prior_days[-1]
    baseline_asset = asset[baseline_day]
    baseline_benchmark = benchmark[baseline_day]
    normalized_expected = str(expected_direction or "").strip().lower()
    if normalized_expected not in {"positive", "negative"}:
        normalized_expected = ""
    windows: dict[str, dict[str, Any]] = {}
    available = 0
    for label, offset in (("1D", 1), ("3D", 3), ("5D", 5)):
        if len(follow_days) < offset:
            windows[label] = _empty_window(
                label, sample_count=1 + len(follow_days)
            )
            windows[label]["data_timestamps"]["start"] = baseline_asset["timestamp"]
            continue
        end_day = follow_days[offset - 1]
        end_asset = asset[end_day]
        end_benchmark = benchmark[end_day]
        asset_return = end_asset["close"] / baseline_asset["close"] - 1.0
        benchmark_return = (
            end_benchmark["close"] / baseline_benchmark["close"] - 1.0
        )
        abnormal_return = asset_return - benchmark_return
        observed_direction = (
            "positive"
            if abnormal_return > 1e-12
            else "negative"
            if abnormal_return < -1e-12
            else "neutral"
        )
        direction_confirmed = (
            observed_direction == normalized_expected
            if normalized_expected in {"positive", "negative"}
            and observed_direction in {"positive", "negative"}
            else None
        )
        windows[label] = {
            "window": label,
            "status": "complete",
            "asset_return": round(asset_return, 10),
            "benchmark_return": round(benchmark_return, 10),
            "abnormal_return": round(abnormal_return, 10),
            "observed_direction": observed_direction,
            "direction_confirmed": direction_confirmed,
            "sample_count": offset + 1,
            "data_timestamps": {
                "start": baseline_asset["timestamp"],
                "end": end_asset["timestamp"],
                "benchmark_start": baseline_benchmark["timestamp"],
                "benchmark_end": end_benchmark["timestamp"],
            },
        }
        available += 1

    status = "complete" if available == 3 else "preliminary" if available else "unavailable"
    return {
        "status": status,
        "method_version": REACTION_METHOD_VERSION,
        "expected_direction": normalized_expected or None,
        "reason": None if available == 3 else "insufficient_follow_up",
        "reason_code": None if available == 3 else "insufficient_follow_up",
        "abstain": status != "complete",
        "event_time": datetime.fromtimestamp(
            event_timestamp, tz=timezone.utc
        ).isoformat(),
        "aligned_sample_count": len(prior_days) + len(follow_days),
        "data_timestamps": [
            asset[day]["timestamp"] for day in prior_days + follow_days
        ],
        "windows": windows,
    }
