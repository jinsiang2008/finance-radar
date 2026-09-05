"""Pure decision aggregation with an explicit opt-in private overlay.

Public decisions are built only from relation and market-reaction evidence.
They never load holdings.  KOL text and market statistics are treated as
signals for review, not as proof of causality or instructions to trade.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

try:
    from kol_dashboard import relation_engine
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    import relation_engine


EVIDENCE_POLICY = (
    "KOL 信息仅用于发现待验证线索；统计相关不等于因果。"
    "尚无到期样本、技术不可用或样本冲突时必须 abstain；"
    "未来窗口记为 pending，不能用来否定事件预期。所有候选均需人工复核。"
)
PUBLIC_MACRO_SCHEMA_VERSION = 1

SCORE_WEIGHTS = {
    "strength": 0.20,
    "confidence": 0.20,
    "freshness": 0.15,
    "corroboration": 0.15,
    "market_confirmation": 0.20,
    "coverage": 0.10,
}
EVENT_RELATION_INGEST_MAX_AGE_HOURS = 72
EVENT_RELATION_MAX_AGE_HOURS = 14 * 24
DECISION_SNAPSHOT_SCHEMA_VERSION = 2
DECISION_ENGINE_VERSION = "decision-v3"

_PUBLIC_FORBIDDEN_TOKENS = (
    "shares",
    "quantity",
    "account",
    "cost",
    "portfolio",
    "matched_positions",
)
_PUBLIC_FORBIDDEN_VALUE_TOKENS = tuple(
    token for token in _PUBLIC_FORBIDDEN_TOKENS if token != "cost"
)
_PUBLIC_IDENTITY_FIELDS = frozenset(
    {
        "affected_assets",
        "affected_markets",
        "asset_key",
        "benchmark_asset_key",
        "matched_asset",
        "provider_symbol",
        "proxy_for",
        "symbol",
        "ticker",
        "tickers",
        "topic_key",
    }
)
_PUBLIC_OPAQUE_ID_FIELDS = frozenset({"id", "item_id", "source_id"})
_PUBLIC_COST_MARKET_IDENTITY_FIELDS = frozenset(
    {
        "affected_assets",
        "asset_key",
        "benchmark_asset_key",
        "matched_asset",
        "provider_symbol",
        "proxy_for",
        "symbol",
        "ticker",
        "tickers",
    }
)
_PUBLIC_EVIDENCE_FIELDS = (
    "title",
    "snippet",
    "url",
    "published_at",
    "generated_at",
    "name",
    "category",
    "item_id",
    "matched_asset",
    "extractor",
    "extractor_version",
)
_PUBLIC_RELATION_FIELDS = (
    "source_type",
    "source_id",
    "topic_key",
    "asset_key",
    "relation_type",
    "direction",
    "strength",
    "confidence",
    "horizon",
    "method",
    "rationale",
    "created_at",
)
_PUBLIC_REACTION_FIELDS = (
    "source_type",
    "source_id",
    "asset_key",
    "window",
    "benchmark_asset_key",
    "asset_return",
    "benchmark_return",
    "abnormal_return",
    "expected_direction",
    "observed_direction",
    "direction_confirmed",
    "status",
    "sample_count",
    "data_timestamps",
    "method_version",
    "observed_at",
    "reason_code",
    "provider",
    "provider_symbol",
    "proxy_for",
    "asset_status",
    "benchmark_status",
    "next_due_at",
)
_PUBLIC_MACRO_SCENARIO_FIELDS = (
    "id",
    "name",
    "probability",
    "impact",
    "description",
    "trigger",
    "affected_assets",
    "affected_markets",
    "tickers",
    "sectors",
    "hedge",
    "timeframe",
)
_PUBLIC_MACRO_RHINO_FIELDS = (
    "id",
    "name",
    "description",
    "visibility",
    "urgency",
    "affected_markets",
    "tickers",
    "sectors",
    "catalyst",
    "market_impact",
)
_PUBLIC_MACRO_EVENT_FIELDS = (
    "id",
    "kind",
    "title",
    "url",
    "source",
    "published_at",
    "time_status",
    "severity",
    "previous_value",
    "current_value",
    "unit",
    "note",
    "tickers",
    "sectors",
)
_PUBLIC_MACRO_EVENT_CATEGORIES = {
    "fomc_statement",
    "fomc_minutes",
    "fed_press_release",
    "fed_speech",
    "pboc_announcement",
    "policy_update",
}
_PUBLIC_MACRO_CONTENT_STATUSES = {"ready", "unavailable", "unsupported"}
_PUBLIC_MACRO_CONTENT_HOSTS = {
    "federalreserve.gov",
    "www.federalreserve.gov",
    "pbc.gov.cn",
    "www.pbc.gov.cn",
}
_PUBLIC_FED_ARTICLE_PATH = re.compile(
    r"^/newsevents/(?:pressreleases|speech)/[a-z0-9._~-]+\.htm$",
    re.IGNORECASE,
)
_PUBLIC_PBOC_ARTICLE_PATH = re.compile(
    r"^/zhengcehuobisi/(?:[0-9]+/)+index\.html$",
    re.IGNORECASE,
)
_PUBLIC_MACRO_EVIDENCE_SECTION_KINDS = {"paragraph", "table_row"}
_PUBLIC_MACRO_AI_STATUSES = {"pending", "processing", "ready", "retry", "failed"}
_PUBLIC_MACRO_AI_IMPACTS = {"high", "medium", "low", "none"}
_PUBLIC_MACRO_AI_DIRECTIONS = {"positive", "negative", "mixed", "unclear"}
_PUBLIC_MACRO_AI_HORIZONS = {"intraday", "short", "medium", "long"}
_PUBLIC_MACRO_AI_LANGUAGES = {"zh", "en", "mixed", "other", "unknown"}
_PUBLIC_MACRO_AI_EVIDENCE = {
    "title",
    "title_only",
    "title_and_snippet",
    "post_text",
    "indicator_data",
    "official_body",
}
_PUBLIC_MACRO_OPPORTUNITY_FIELDS = (
    "id",
    "type",
    "name",
    "asset",
    "assets",
    "signal",
    "catalyst",
    "confidence",
    "timeframe",
    "risk",
)
_PUBLIC_MACRO_SCORE_KEYS = (
    "recession",
    "market_stress",
    "geopolitical",
    "china_risk",
)
_PUBLIC_MACRO_MARKET_KEYS = (
    "vix",
    "treasury",
    "yield_curve",
    "usd_cny",
    "dxy",
    "gold_oil",
    "financial_stress",
    "credit_spreads",
)
_PUBLIC_MACRO_MARKET_SCALARS = (
    "value",
    "rate",
    "price",
    "change",
    "change_pct",
    "status",
    "source",
    "source_url",
    "timestamp",
    "observed_at",
    "spread_2y10y",
    "2Y",
    "5Y",
    "10Y",
    "30Y",
    "hy_oas",
    "ig_oas",
    "ofr_fsi",
    "credit",
    "funding",
    "volatility",
    "equity_valuation",
    "safe_assets",
    "data_status",
    "stale",
    "is_stale",
    "note",
    "symbol",
    "unit",
)

_PUBLIC_MACRO_ALERT_INPUT_KEYS = (
    "us_equity",
    "cn_equity",
    "vix_daily",
    "usd_cny_daily",
)
_PUBLIC_MACRO_ALERT_INPUT_FIELDS = (
    "status",
    "data_status",
    "stale",
    "asset_key",
    "label",
    "provider",
    "source",
    "source_url",
    "symbol",
    "currency",
    "observed_at",
    "market_date",
    "timestamp_semantics",
    "bars_available",
    "reason",
    "close",
    "sma20",
    "sma60",
    "sma20_slope_5d_pct",
    "return_5d_pct",
    "drawdown_60d_pct",
    "data_hash",
)
_PUBLIC_MACRO_ALERT_ACTIONS = {
    "observe": "继续观察",
    "prepare_reduce": "减仓准备",
    "reduce_candidate": "减仓候选",
    "exit_candidate": "防御 / 清仓审查",
}
_PUBLIC_MACRO_ALERT_RISK_LEVELS = {
    "insufficient",
    "low",
    "medium",
    "high",
    "critical",
}
_PUBLIC_MACRO_ALERT_DATA_STATUSES = {"ok", "insufficient"}
_PUBLIC_MACRO_ALERT_GATE_STATUSES = {
    "met",
    "partial",
    "unmet",
    "unavailable",
}
_PUBLIC_MACRO_ALERT_SIGNAL_SEVERITIES = {"watch", "strong", "critical"}
_PUBLIC_MACRO_ALERT_PILLARS = {
    "trend",
    "volatility",
    "financial_stress",
    "fx_liquidity",
}
_PUBLIC_MACRO_ALERT_TIME_BASES = {
    "completed_market_close",
    "official_daily_observation",
}
_PUBLIC_MACRO_ALERT_METHOD_VERSION = "macro-de-risk-trial-v1"
_PUBLIC_MACRO_ALERT_MAX_AGE = timedelta(minutes=90)
_PUBLIC_MACRO_ALERT_SOURCE_HOSTS = {
    "finance.yahoo.com",
    "gu.qq.com",
    "www.financialresearch.gov",
}

_PUBLIC_MACRO_COVERAGE_SOURCE_FIELDS = (
    "key",
    "label",
    "available",
    "status",
    "data_status",
    "observed_at",
    "source_url",
    "stale",
    "is_stale",
    "note",
    "source",
    "provider",
    "market_date",
)
_PRIVATE_MACRO_RELATION_FIELDS = {
    "affected_positions",
    "portfolio_impact",
}
_DIRECTION_ALIASES = {
    "positive": "positive",
    "bullish": "positive",
    "up": "positive",
    "long": "positive",
    "negative": "negative",
    "bearish": "negative",
    "down": "negative",
    "short": "negative",
    "neutral": "neutral",
    "mixed": "neutral",
    "unknown": "neutral",
}
_WINDOW_MINIMUM_SAMPLES = {"1D": 2, "3D": 4, "5D": 6}
_MARKET_WINDOWS = ("1D", "3D", "5D")
_MARKET_WINDOW_SCORE = {"1D": 0.55, "3D": 0.80, "5D": 1.0}
_HORIZON_REQUIRED_WINDOW = {
    "intraday": "1D",
    "short": "1D",
    "medium": "3D",
    "long": "5D",
    "mixed": "5D",
    "unknown": "3D",
}
_HORIZON_MAX_AGE_HOURS = {
    "intraday": 72,
    "short": 72,
    "medium": 7 * 24,
    "unknown": 7 * 24,
    "long": 14 * 24,
    "mixed": 14 * 24,
}
_PUBLIC_REACTION_TEXT_LIMITS = {
    "reason_code": 64,
    "provider": 32,
    "provider_symbol": 80,
    "proxy_for": 80,
    "asset_status": 64,
    "benchmark_status": 64,
    "next_due_at": 48,
}
_SAFE_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _unit_interval(value: Any, default: float = 0.0) -> float:
    number = _finite_number(value)
    if number is None:
        number = default
    return max(0.0, min(1.0, number))


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        timestamp = _finite_number(value)
        if timestamp is None or timestamp < 0:
            return None
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return _utc_datetime(float(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _verified_event_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _event_time_is_eligible(
    value: Any,
    now: datetime,
    *,
    maximum_age_hours: int = EVENT_RELATION_MAX_AGE_HOURS,
) -> bool:
    published = _verified_event_time(value)
    if published is None:
        return False
    age_seconds = (now - published).total_seconds()
    return (
        age_seconds >= 0
        and age_seconds <= max(1, int(maximum_age_hours)) * 3600
    )


def _relation_is_decision_eligible(
    relation: Mapping[str, Any], now: datetime
) -> bool:
    source_type = str(relation.get("source_type") or "").strip().lower()
    if source_type == "macro_snapshot":
        return True
    if source_type != "event":
        return False
    evidence = _relation_evidence(relation)
    published_at = (
        evidence.get("published_at")
        if isinstance(evidence, Mapping)
        else None
    )
    horizon = str(relation.get("horizon") or "unknown").strip().lower()
    maximum_age_hours = _HORIZON_MAX_AGE_HOURS.get(horizon, 7 * 24)
    return _event_time_is_eligible(
        published_at,
        now,
        maximum_age_hours=maximum_age_hours,
    )


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        candidates: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        candidates = value
    else:
        return []
    return [
        deepcopy(dict(item))
        for item in candidates
        if isinstance(item, Mapping)
    ]


def _contains_forbidden_token(value: Any) -> bool:
    lowered = str(value).lower()
    return any(token in lowered for token in _PUBLIC_FORBIDDEN_TOKENS)


def _contains_forbidden_value_token(value: Any) -> bool:
    return _contains_bounded_token(value, _PUBLIC_FORBIDDEN_VALUE_TOKENS)


def _contains_bounded_token(value: Any, tokens: Iterable[str]) -> bool:
    text = str(value)
    return any(
        re.search(
            rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
            text,
            flags=re.IGNORECASE,
        )
        is not None
        for token in tokens
    )


def _contains_forbidden_opaque_token(value: Any) -> bool:
    return _contains_bounded_token(value, _PUBLIC_FORBIDDEN_TOKENS)


def _contains_forbidden_identity_token(
    value: Any,
    field_name: str | None,
) -> bool:
    text = str(value).strip()
    lowered_field = str(field_name or "").lower()
    tokens: Iterable[str] = _PUBLIC_FORBIDDEN_TOKENS
    if (
        lowered_field in _PUBLIC_COST_MARKET_IDENTITY_FIELDS
        and text.upper() in {"COST", "US:COST"}
    ):
        tokens = _PUBLIC_FORBIDDEN_VALUE_TOKENS
    return _contains_bounded_token(text, tokens)


def _sanitize_public_string(value: str) -> str:
    result = value
    # ``cost`` is a legitimate finance word and the NASDAQ ticker COST. It is
    # forbidden in field names (for example avg_cost), but not in public text.
    for token in _PUBLIC_FORBIDDEN_VALUE_TOKENS:
        result = re.sub(
            rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
            "[redacted]",
            result,
            flags=re.IGNORECASE,
        )
    return result


def _redacted_public_identity(value: str, field_name: str) -> str:
    digest = hashlib.sha256(
        f"{field_name.lower()}\0{value}".encode("utf-8")
    ).hexdigest()[:16]
    return f"[redacted]-{digest}"


def _is_public_identity_field(field_name: str | None) -> bool:
    if not field_name:
        return False
    lowered = field_name.lower()
    return (
        lowered in _PUBLIC_IDENTITY_FIELDS
        or lowered.endswith("_key")
    )


def _public_sanitize(value: Any, *, field_name: str | None = None) -> Any:
    """Return JSON-safe public data with private vocabulary removed recursively."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = str(key)
            if _contains_forbidden_token(clean_key):
                continue
            result[clean_key] = _public_sanitize(item, field_name=clean_key)
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _public_sanitize(item, field_name=field_name)
            for item in value
        ]
    if isinstance(value, str):
        if _is_public_identity_field(field_name):
            if _contains_forbidden_identity_token(value, field_name):
                return _redacted_public_identity(
                    value,
                    str(field_name or "identity"),
                )
            return value
        lowered_field = str(field_name or "").lower()
        if (
            lowered_field in _PUBLIC_OPAQUE_ID_FIELDS
            or lowered_field.endswith("_url")
            or lowered_field == "url"
        ) and _contains_forbidden_opaque_token(value):
            return _redacted_public_identity(value, lowered_field)
        return _sanitize_public_string(value)
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = str(value)
    return text if _is_public_identity_field(field_name) else _sanitize_public_string(text)


def _coverage_value(value: Any) -> float:
    raw = value
    if isinstance(value, Mapping):
        raw = value.get("coverage", value.get("value", 0.0))
    return round(_unit_interval(raw), 4)


def _direction(value: Any) -> str:
    return _DIRECTION_ALIASES.get(str(value or "").strip().lower(), "neutral")


def _relation_evidence(relation: Mapping[str, Any]) -> Any:
    evidence = relation.get("evidence")
    if evidence is None:
        evidence = relation.get("evidence_json", {})
    if isinstance(evidence, str):
        try:
            return json.loads(evidence)
        except json.JSONDecodeError:
            return {"text": evidence}
    return evidence if evidence is not None else {}


def _relation_freshness(relation: Mapping[str, Any], now: datetime) -> float:
    evidence = _relation_evidence(relation)
    evidence_time = evidence.get("published_at") if isinstance(evidence, Mapping) else None
    observed = _utc_datetime(
        evidence_time
        or relation.get("published_at")
        or relation.get("created_at")
    )
    if observed is None:
        return 0.35
    age_seconds = (now - observed).total_seconds()
    if age_seconds < -300:
        return 0.0
    age_days = max(0.0, age_seconds) / 86_400
    if age_days <= 1:
        return 1.0
    if age_days <= 3:
        return 0.9
    if age_days <= 7:
        return 0.75
    if age_days <= 30:
        return 0.5
    if age_days <= 90:
        return 0.25
    return 0.1


def _average(values: Iterable[float], default: float = 0.0) -> float:
    collected = list(values)
    if not collected:
        return default
    return sum(collected) / len(collected)


def _corroboration(source_count: int) -> float:
    if source_count <= 0:
        return 0.0
    if source_count == 1:
        return 0.35
    if source_count == 2:
        return 0.65
    if source_count == 3:
        return 0.85
    return 1.0


def _group_horizon(relations: list[dict[str, Any]]) -> str:
    values = [
        str(relation.get("horizon") or "unknown").strip().lower()
        for relation in relations
    ]
    counts = Counter(values)
    if not counts:
        return "unknown"
    highest = max(counts.values())
    winners = sorted(value for value, count in counts.items() if count == highest)
    return winners[0] if len(winners) == 1 else "mixed"


def _classification(relations: list[dict[str, Any]]) -> tuple[str, str]:
    directions = {_direction(relation.get("direction")) for relation in relations}
    if "positive" in directions and "negative" in directions:
        return "conflict", "mixed"
    if "negative" in directions:
        return "risk", "negative"
    if "positive" in directions:
        return "opportunity", "positive"
    return "conflict", "mixed"


def _market_applicability(
    relations: Iterable[Mapping[str, Any]],
    expected_direction: str,
) -> tuple[str, str | None, str]:
    records = list(relations)
    source_types = {
        str(relation.get("source_type") or "").strip().lower()
        for relation in records
    }
    has_event = "event" in source_types
    source_scope = (
        "mixed"
        if has_event and "macro_snapshot" in source_types
        else "event_only"
        if has_event
        else "macro_only"
    )
    if not has_event:
        return "not_applicable", "no_event_anchor", source_scope
    if expected_direction not in {"positive", "negative"}:
        return "not_applicable", "direction_missing", source_scope
    has_directional_event = any(
        str(relation.get("source_type") or "").strip().lower() == "event"
        and _direction(relation.get("direction")) == expected_direction
        for relation in records
    )
    if not has_directional_event:
        return "not_applicable", "direction_missing", source_scope
    return "applicable", None, source_scope


def _directional_event_source_pairs(
    relations: Iterable[Mapping[str, Any]],
    expected_direction: str,
) -> set[tuple[str, str]]:
    return {
        identity
        for index, relation in enumerate(relations)
        if str(relation.get("source_type") or "").strip().lower() == "event"
        and _direction(relation.get("direction")) == expected_direction
        and (identity := _source_identity(relation, index)) is not None
    }


def _source_identity(
    relation: Mapping[str, Any], index: int
) -> tuple[str, str] | None:
    source_type = relation.get("source_type")
    source_id = relation.get("source_id")
    if (
        not isinstance(source_type, str)
        or not source_type.strip()
        or not isinstance(source_id, str)
        or not source_id.strip()
    ):
        return None
    return source_type.strip(), source_id.strip()


def _matches_reaction(
    reaction: Mapping[str, Any],
    asset_key: str,
    source_pairs: set[tuple[str, str]],
) -> bool:
    if str(reaction.get("asset_key") or "") != asset_key:
        return False
    source_id = reaction.get("source_id")
    reaction_type = reaction.get("source_type")
    if (
        not isinstance(source_id, str)
        or not source_id.strip()
        or not isinstance(reaction_type, str)
        or not reaction_type.strip()
    ):
        return False
    pair = (reaction_type.strip(), source_id.strip())
    return pair in source_pairs


def _is_public_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _public_evidence_detail(relation: Mapping[str, Any]) -> dict[str, Any]:
    raw = _relation_evidence(relation)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in _PUBLIC_EVIDENCE_FIELDS:
        if field not in raw:
            continue
        value = raw.get(field)
        if _is_public_scalar(value):
            result[field] = _public_sanitize(value, field_name=field)
    return result


def _public_relation(relation: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for field in _PUBLIC_RELATION_FIELDS:
        if field in relation and _is_public_scalar(relation[field]):
            result[field] = _public_sanitize(
                relation[field],
                field_name=field,
            )
    result["evidence"] = _public_evidence_detail(relation)
    return result


def _public_reaction(reaction: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in _PUBLIC_REACTION_FIELDS:
        if field not in reaction:
            continue
        value = reaction[field]
        if field == "data_timestamps":
            if not isinstance(value, Mapping):
                continue
            result[field] = {
                key: _public_sanitize(value[key], field_name=key)
                for key in (
                    "start",
                    "end",
                    "benchmark_start",
                    "benchmark_end",
                )
                if key in value
                and _is_public_scalar(value[key])
            }
        elif field in _PUBLIC_REACTION_TEXT_LIMITS:
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > _PUBLIC_REACTION_TEXT_LIMITS[field]:
                continue
            if field == "reason_code":
                text = text.lower()
                if _SAFE_REASON_CODE.fullmatch(text) is None:
                    continue
            result[field] = _public_sanitize(text, field_name=field)
        elif _is_public_scalar(value):
            result[field] = _public_sanitize(value, field_name=field)
    return result


def project_public_relations(relations: Any) -> list[dict[str, Any]]:
    """Project stored relations onto the strict public API schema."""
    output: list[dict[str, Any]] = []
    for relation in _records(relations):
        # Topic and asset identities drive public grouping and portfolio
        # matching. If upstream storage is polluted with private vocabulary,
        # dropping the edge is safer than exposing it or creating an invalid
        # redacted asset card. ``cost`` is intentionally not a forbidden value
        # token, so the legitimate US:COST identity remains valid.
        if _contains_forbidden_identity_token(
            relation.get("topic_key") or "",
            "topic_key",
        ) or _contains_forbidden_identity_token(
            relation.get("asset_key") or "",
            "asset_key",
        ):
            continue
        source_type = str(relation.get("source_type") or "").strip().lower()
        if source_type not in {"event", "macro_snapshot"}:
            continue
        relation["source_type"] = source_type
        if source_type == "macro_snapshot":
            evidence = _relation_evidence(relation)
            matched_fields = evidence.get("matched_fields")
            if isinstance(matched_fields, Mapping) and (
                set(matched_fields).intersection(_PRIVATE_MACRO_RELATION_FIELDS)
            ):
                continue
            if isinstance(matched_fields, Mapping) and "fallback_text" in matched_fields:
                fallback_text = str(matched_fields.get("fallback_text") or "")
                extractor_version = str(evidence.get("extractor_version") or "")
                private_tokens = (
                    "持仓",
                    "账户",
                    "数量",
                    "成本",
                    "portfolio",
                    "position",
                    "holding",
                    "broker",
                    "qty",
                )
                if (
                    extractor_version != relation_engine.EXTRACTOR_VERSION
                    or any(
                        token in fallback_text.lower()
                        for token in private_tokens
                    )
                ):
                    continue
        projected = _public_relation(relation)
        if (
            isinstance(projected.get("topic_key"), str)
            and projected["topic_key"].strip()
            and isinstance(projected.get("asset_key"), str)
            and projected["asset_key"].strip()
        ):
            output.append(projected)
    return output


def _public_macro_value(value: Any, field_name: str) -> Any:
    if _is_public_scalar(value):
        return _public_sanitize(value, field_name=field_name)
    if isinstance(value, list):
        return [
            _public_sanitize(item, field_name=field_name)
            for item in value
            if isinstance(item, str)
        ]
    return None


def _project_macro_items(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return output
    for item in value:
        if not isinstance(item, Mapping):
            continue
        projected: dict[str, Any] = {}
        for field in fields:
            if field not in item:
                continue
            clean = _public_macro_value(item[field], field)
            if clean is not None:
                projected[field] = clean
        output.append(projected)
    return output


def _macro_ai_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return _sanitize_public_string(re.sub(r"\s+", " ", value).strip())[:maximum]


def _macro_ai_text_list(
    value: Any,
    *,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _macro_ai_text(item, maximum_length)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
        if len(output) >= maximum_items:
            break
    return output


def _macro_event_plain_text(value: Any, maximum: int) -> str:
    """Return bounded plain text; never pass source HTML through the public API."""
    if not isinstance(value, str):
        return ""
    text = unescape(value)
    text = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _sanitize_public_string(text)[:maximum]


def _public_macro_content_identity(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or host not in _PUBLIC_MACRO_CONTENT_HOSTS
    ):
        return None
    if host.endswith("federalreserve.gov"):
        return (
            ("fed", parsed.path)
            if _PUBLIC_FED_ARTICLE_PATH.fullmatch(parsed.path)
            else None
        )
    return (
        ("pboc", parsed.path)
        if _PUBLIC_PBOC_ARTICLE_PATH.fullmatch(parsed.path)
        else None
    )


def _public_macro_content_url(value: Any) -> str:
    if _public_macro_content_identity(value) is None:
        return ""
    return _sanitize_public_string(str(value).strip())[:2048]


def _project_macro_evidence_sections(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        text = _macro_event_plain_text(item.get("text"), 700)
        if kind not in _PUBLIC_MACRO_EVIDENCE_SECTION_KINDS or not text:
            continue
        output.append({"kind": kind, "text": text})
        if len(output) >= 8:
            break
    return output


def _project_macro_event_content(
    source: Mapping[str, Any],
    event: dict[str, Any],
) -> None:
    category = str(source.get("category") or "").strip().lower()
    if category in _PUBLIC_MACRO_EVENT_CATEGORIES:
        event["category"] = category

    status = str(source.get("content_status") or "").strip().lower()
    if status not in _PUBLIC_MACRO_CONTENT_STATUSES:
        return
    excerpt = _macro_event_plain_text(source.get("content_excerpt"), 4000)
    source_url = _public_macro_content_url(source.get("content_source_url"))
    event_identity = _public_macro_content_identity(source.get("url"))
    source_identity = _public_macro_content_identity(source_url)
    if status == "ready" and (
        not excerpt
        or not source_url
        or event_identity is None
        or event_identity != source_identity
    ):
        status = "unavailable"
    event["content_status"] = status
    if status != "ready":
        return

    event["content_excerpt"] = excerpt
    event["content_source_url"] = source_url
    sections = _project_macro_evidence_sections(source.get("evidence_sections"))
    if sections:
        event["evidence_sections"] = sections


def _project_macro_event_ai(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    headline = _macro_ai_text(value.get("headline_zh"), 72)
    summary = _macro_ai_text(value.get("summary_zh"), 280)
    why = _macro_ai_text(value.get("why_it_matters_zh"), 220)
    if not headline or not summary or not why:
        return None

    impact = str(value.get("impact_level") or "").strip().lower()
    if impact not in _PUBLIC_MACRO_AI_IMPACTS:
        impact = "low"
    language = str(value.get("language") or "unknown").strip().lower()
    if language not in _PUBLIC_MACRO_AI_LANGUAGES:
        language = "unknown"
    evidence = str(value.get("evidence_basis") or "title_only").strip().lower()
    if evidence not in _PUBLIC_MACRO_AI_EVIDENCE:
        evidence = "title_only"
    confidence = _finite_number(value.get("confidence"))
    confidence = (
        round(max(0.0, min(1.0, confidence)), 2)
        if confidence is not None
        else 0.0
    )

    assets: list[dict[str, Any]] = []
    seen_assets: set[str] = set()
    raw_assets = value.get("assets")
    if isinstance(raw_assets, list):
        for raw in raw_assets:
            if not isinstance(raw, Mapping):
                continue
            asset_key = _macro_ai_text(raw.get("asset_key"), 32).upper()
            if (
                not re.fullmatch(
                    r"(?:US|CN|HK|INDEX|ETF|BOND|FX|COMMODITY|CRYPTO):[A-Z0-9.^_-]{1,20}",
                    asset_key,
                )
                or asset_key in seen_assets
            ):
                continue
            direction = str(raw.get("direction") or "unclear").strip().lower()
            horizon = str(raw.get("horizon") or "short").strip().lower()
            asset_confidence = _finite_number(raw.get("confidence"))
            seen_assets.add(asset_key)
            assets.append(
                {
                    "asset_key": asset_key,
                    "name_zh": _macro_ai_text(raw.get("name_zh"), 30),
                    "direction": (
                        direction
                        if direction in _PUBLIC_MACRO_AI_DIRECTIONS
                        else "unclear"
                    ),
                    "horizon": (
                        horizon
                        if horizon in _PUBLIC_MACRO_AI_HORIZONS
                        else "short"
                    ),
                    "reason_zh": _macro_ai_text(raw.get("reason_zh"), 90),
                    "confidence": round(
                        max(0.0, min(1.0, asset_confidence or 0.0)), 2
                    ),
                }
            )
            if len(assets) >= 6:
                break

    return {
        "status": "ready",
        "headline_zh": headline,
        "summary_zh": summary,
        "why_it_matters_zh": why,
        "impact_level": impact,
        "impact_path": _macro_ai_text_list(
            value.get("impact_path"), maximum_items=3, maximum_length=150
        ),
        "tags": _macro_ai_text_list(
            value.get("tags"), maximum_items=6, maximum_length=16
        ),
        "assets": assets,
        "cluster_key": _macro_ai_text(value.get("cluster_key"), 96),
        "language": language,
        "confidence": confidence,
        "evidence_basis": evidence,
        "model": _macro_ai_text(value.get("model"), 80),
        "generated_at": _macro_ai_text(value.get("generated_at"), 64),
    }


def _project_macro_events(
    value: Any,
    enrichment_map: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    raw_events = value if isinstance(value, list) else []
    events: list[dict[str, Any]] = []
    for source in raw_events:
        if not isinstance(source, Mapping):
            continue
        projected = _project_macro_items([source], _PUBLIC_MACRO_EVENT_FIELDS)
        if not projected:
            continue
        event = projected[0]
        _project_macro_event_content(source, event)
        events.append(event)
    records = enrichment_map if isinstance(enrichment_map, Mapping) else {}
    for event in events:
        event_id = str(event.get("id") or "")
        cache = records.get(event_id)
        status = (
            str(cache.get("status") or "pending").strip().lower()
            if isinstance(cache, Mapping)
            else "pending"
        )
        if status not in _PUBLIC_MACRO_AI_STATUSES:
            status = "pending"
        enrichment = (
            _project_macro_event_ai(cache.get("ai_enrichment"))
            if status == "ready" and isinstance(cache, Mapping)
            else None
        )
        if status == "ready" and enrichment is None:
            status = "failed"
        event["ai_status"] = status
        event["ai_enrichment"] = enrichment
    return events


def _project_macro_sub_scores(
    value: Any,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key in _PUBLIC_MACRO_SCORE_KEYS:
        item = value.get(key)
        if not isinstance(item, Mapping):
            continue
        projected: dict[str, Any] = {}
        fields = (
            ("score", "level", "interpretation")
            if include_text
            else ("score", "level")
        )
        for field in fields:
            if field in item and _is_public_scalar(item[field]):
                if (
                    not include_text
                    and field == "level"
                    and str(item[field]).lower()
                    not in {"critical", "high", "medium", "low"}
                ):
                    continue
                projected[field] = _public_sanitize(
                    item[field],
                    field_name=field,
                )
        signals = item.get("signals")
        if include_text and isinstance(signals, list):
            projected["signals"] = [
                signal for signal in signals if isinstance(signal, str)
            ]
        output[key] = projected
    return output


def _project_market_point(
    value: Any,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        field: _public_sanitize(value[field], field_name=field)
        for field in _PUBLIC_MACRO_MARKET_SCALARS
        if field in value
        and _is_public_scalar(value[field])
        and (include_text or not isinstance(value[field], str))
    }


def _project_macro_market_data(
    value: Any,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key in _PUBLIC_MACRO_MARKET_KEYS:
        item = value.get(key)
        if not isinstance(item, Mapping):
            continue
        projected = _project_market_point(item, include_text=include_text)
        if key == "gold_oil":
            for nested_key in ("gold", "oil"):
                nested = _project_market_point(
                    item.get(nested_key),
                    include_text=include_text,
                )
                if nested:
                    projected[nested_key] = nested
        output[key] = projected
    alert_inputs = value.get("alert_inputs")
    if include_text and isinstance(alert_inputs, Mapping):
        projected_inputs: dict[str, Any] = {}
        for key in _PUBLIC_MACRO_ALERT_INPUT_KEYS:
            item = alert_inputs.get(key)
            if not isinstance(item, Mapping):
                continue
            projected: dict[str, Any] = {}
            for field in _PUBLIC_MACRO_ALERT_INPUT_FIELDS:
                if field not in item or not _is_public_scalar(item[field]):
                    continue
                if field == "source_url":
                    safe_url = _project_macro_alert_source_url(item[field])
                    if safe_url:
                        projected[field] = safe_url
                    continue
                projected[field] = _public_sanitize(
                    item[field],
                    field_name=field,
                )
            projected_inputs[key] = projected
        output["alert_inputs"] = projected_inputs
    return output


def _project_macro_alert_source_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or host not in _PUBLIC_MACRO_ALERT_SOURCE_HOSTS
    ):
        return None
    return candidate[:500]


def _project_macro_alert_time(value: Any, *, allow_date: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if allow_date and len(candidate) == 10:
        try:
            date.fromisoformat(candidate)
        except ValueError:
            return None
        return candidate
    parsed = _verified_event_time(candidate)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_macro_alert_signal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    key = _macro_event_plain_text(value.get("key"), 80)
    pillar = str(value.get("pillar") or "").strip().lower()
    severity = str(value.get("severity") or "").strip().lower()
    label = _macro_event_plain_text(value.get("label"), 160)
    if (
        not key
        or pillar not in _PUBLIC_MACRO_ALERT_PILLARS
        or severity not in _PUBLIC_MACRO_ALERT_SIGNAL_SEVERITIES
        or not label
    ):
        return None
    output: dict[str, Any] = {
        "key": key,
        "pillar": pillar,
        "label": label,
        "severity": severity,
    }
    number = _finite_number(value.get("value"))
    if number is not None:
        output["value"] = number
    for field, maximum in (
        ("unit", 40),
        ("threshold", 240),
        ("source", 100),
        ("detail", 500),
    ):
        text = _macro_event_plain_text(value.get(field), maximum)
        if text:
            output[field] = text
    observed_at = _project_macro_alert_time(
        value.get("observed_at"),
        allow_date=True,
    )
    if observed_at:
        output["observed_at"] = observed_at
    time_basis = str(value.get("time_basis") or "").strip().lower()
    if time_basis in _PUBLIC_MACRO_ALERT_TIME_BASES:
        output["time_basis"] = time_basis
    source_url = _project_macro_alert_source_url(value.get("source_url"))
    if source_url:
        output["source_url"] = source_url
    return output


def _project_macro_alert_gates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value[:8]:
        if not isinstance(item, Mapping):
            continue
        key = _macro_event_plain_text(item.get("key"), 80)
        label = _macro_event_plain_text(item.get("label"), 160)
        status = str(item.get("status") or "").strip().lower()
        if (
            not key
            or key in seen
            or not label
            or status not in _PUBLIC_MACRO_ALERT_GATE_STATUSES
        ):
            continue
        seen.add(key)
        projected = {"key": key, "label": label, "status": status}
        detail = _macro_event_plain_text(item.get("detail"), 500)
        if detail:
            projected["detail"] = detail
        output.append(projected)
    return output


def _project_macro_alert_text_list(
    value: Any,
    *,
    maximum_items: int = 8,
    maximum_length: int = 500,
) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _macro_event_plain_text(item, maximum_length)
        folded = text.casefold()
        if text and folded not in seen:
            seen.add(folded)
            output.append(text)
        if len(output) >= maximum_items:
            break
    return output


def _project_macro_market_alert(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    market = str(value.get("market") or "").strip().upper()
    if market not in {"US", "CN"}:
        return None
    action = str(value.get("action") or "").strip().lower()
    data_status = str(value.get("data_status") or "").strip().lower()
    if (
        action not in _PUBLIC_MACRO_ALERT_ACTIONS
        or data_status not in _PUBLIC_MACRO_ALERT_DATA_STATUSES
    ):
        return None
    abstain = value.get("abstain") is True or data_status != "ok"
    if abstain:
        action = "observe"
        data_status = "insufficient"
    risk_level = str(value.get("risk_level") or "").strip().lower()
    if risk_level not in _PUBLIC_MACRO_ALERT_RISK_LEVELS:
        risk_level = "insufficient" if abstain else "low"
    if abstain:
        risk_level = "insufficient"
    gates = _project_macro_alert_gates(value.get("gates"))
    signals: list[dict[str, Any]] = []
    for item in value.get("triggered_signals", [])[:12] if isinstance(value.get("triggered_signals"), list) else []:
        projected = _project_macro_alert_signal(item)
        if projected:
            signals.append(projected)
    counters: list[dict[str, Any]] = []
    for item in value.get("counter_signals", [])[:12] if isinstance(value.get("counter_signals"), list) else []:
        projected = _project_macro_alert_signal(item)
        if projected:
            counters.append(projected)
    output: dict[str, Any] = {
        "market": market,
        "label": "美股" if market == "US" else "A股",
        "action": action,
        "action_label": _PUBLIC_MACRO_ALERT_ACTIONS[action],
        "risk_level": risk_level,
        "abstain": abstain,
        "data_status": data_status,
        "summary": _macro_event_plain_text(value.get("summary"), 600),
        "gate_progress": {
            "met": sum(1 for gate in gates if gate["status"] == "met"),
            "total": len(gates),
        },
        "gates": gates,
        "triggered_signals": signals,
        "counter_signals": counters,
        "upgrade_conditions": _project_macro_alert_text_list(
            value.get("upgrade_conditions")
        ),
        "invalidation_conditions": _project_macro_alert_text_list(
            value.get("invalidation_conditions")
        ),
        "missing_sources": _project_macro_alert_text_list(
            value.get("missing_sources"),
            maximum_items=8,
            maximum_length=160,
        ),
        "rule_version": _PUBLIC_MACRO_ALERT_METHOD_VERSION,
    }
    for field in ("data_as_of", "next_evaluation_at"):
        projected_time = _project_macro_alert_time(value.get(field))
        if projected_time:
            output[field] = projected_time
    return output


def _project_macro_market_alerts(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if (
        value.get("schema_version") != 1
        or isinstance(value.get("schema_version"), bool)
        or value.get("method_version") != _PUBLIC_MACRO_ALERT_METHOD_VERSION
        or value.get("mode") != "trial"
        or value.get("human_review_required") is not True
        or value.get("automatic_execution") is not False
    ):
        return {}
    generated_at = _project_macro_alert_time(value.get("generated_at"))
    if not generated_at:
        return {}
    raw_markets = value.get("markets")
    if not isinstance(raw_markets, list):
        return {}
    by_market: dict[str, dict[str, Any]] = {}
    for item in raw_markets:
        projected = _project_macro_market_alert(item)
        if projected and projected["market"] not in by_market:
            by_market[projected["market"]] = projected
    return {
        "schema_version": 1,
        "method_version": _PUBLIC_MACRO_ALERT_METHOD_VERSION,
        "generated_at": generated_at,
        "mode": "trial",
        "human_review_required": True,
        "automatic_execution": False,
        "markets": [
            by_market[market]
            for market in ("US", "CN")
            if market in by_market
        ],
    }


def _expire_stale_macro_alerts(
    alerts: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Fail closed when collection stops but an old action remains cached."""
    generated = _verified_event_time(alerts.get("generated_at"))
    if generated is None:
        return {}
    current = now.astimezone(timezone.utc)
    age = current - generated
    if -timedelta(minutes=5) <= age <= _PUBLIC_MACRO_ALERT_MAX_AGE:
        return alerts

    expired = deepcopy(alerts)
    reason = (
        "宏观快照时间异常，系统停止升级仓位动作。"
        if age < -timedelta(minutes=5)
        else "宏观快照已超过90分钟，系统停止升级仓位动作。"
    )
    for market in expired.get("markets", []):
        if not isinstance(market, dict):
            continue
        market["action"] = "observe"
        market["action_label"] = _PUBLIC_MACRO_ALERT_ACTIONS["observe"]
        market["risk_level"] = "insufficient"
        market["abstain"] = True
        market["data_status"] = "insufficient"
        market["summary"] = reason + "这不是低风险结论。"
        market.pop("next_evaluation_at", None)
        missing = market.get("missing_sources")
        missing = missing if isinstance(missing, list) else []
        if "宏观采集快照已过期" not in missing:
            missing.append("宏观采集快照已过期")
        market["missing_sources"] = missing
        gates = market.get("gates")
        gates = gates if isinstance(gates, list) else []
        freshness = next(
            (
                gate
                for gate in gates
                if isinstance(gate, dict) and gate.get("key") == "data_freshness"
            ),
            None,
        )
        if freshness is None:
            gates.insert(
                0,
                {
                    "key": "data_freshness",
                    "label": "核心数据新鲜度",
                    "status": "unavailable",
                    "detail": reason,
                },
            )
        else:
            freshness["status"] = "unavailable"
            freshness["detail"] = reason
        market["gates"] = gates
        market["gate_progress"] = {
            "met": sum(
                1
                for gate in gates
                if isinstance(gate, Mapping) and gate.get("status") == "met"
            ),
            "total": len(gates),
        }
    return expired


def project_public_macro(
    snapshot: Any,
    macro_event_enrichments: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project a macro snapshot without legacy portfolio-specific fields."""
    if not isinstance(snapshot, Mapping):
        return {}
    trusted = (
        snapshot.get("public_schema_version") == PUBLIC_MACRO_SCHEMA_VERSION
        and not isinstance(snapshot.get("public_schema_version"), bool)
    )
    output: dict[str, Any] = {}
    for field in ("timestamp", "snapshot_id", "created_at"):
        if field in snapshot and _is_public_scalar(snapshot[field]):
            output[field] = _public_sanitize(
                snapshot[field],
                field_name=field,
            )
    if trusted:
        output["public_schema_version"] = PUBLIC_MACRO_SCHEMA_VERSION
    composite = snapshot.get("composite_risk")
    if isinstance(composite, Mapping):
        composite_fields = ("score", "level", "label") if trusted else ("score", "level")
        output["composite_risk"] = {
            field: _public_sanitize(composite[field], field_name=field)
            for field in composite_fields
            if field in composite and _is_public_scalar(composite[field])
            and (
                trusted
                or field != "level"
                or str(composite[field]).lower()
                in {"critical", "high", "medium", "low"}
            )
        }
    output["sub_scores"] = _project_macro_sub_scores(
        snapshot.get("sub_scores"),
        include_text=trusted,
    )
    output["market_data"] = _project_macro_market_data(
        snapshot.get("market_data"),
        include_text=trusted,
    )
    if trusted:
        projected_alerts = _project_macro_market_alerts(
            snapshot.get("market_alerts")
        )
        if projected_alerts:
            if now is not None:
                current = _utc_datetime(now)
                if current is not None:
                    projected_alerts = _expire_stale_macro_alerts(
                        projected_alerts,
                        now=current,
                    )
            output["market_alerts"] = projected_alerts
    coverage = snapshot.get("data_coverage")
    if isinstance(coverage, Mapping):
        projected_coverage = {
            field: _public_sanitize(coverage[field], field_name=field)
            for field in ("available", "total", "pct")
            if field in coverage and _is_public_scalar(coverage[field])
        }
        sources = coverage.get("sources")
        projected_coverage["sources"] = (
            [
                {
                    field: _public_sanitize(source[field], field_name=field)
                    for field in _PUBLIC_MACRO_COVERAGE_SOURCE_FIELDS
                    if field in source and _is_public_scalar(source[field])
                }
                for source in sources
                if isinstance(source, Mapping)
            ]
            if trusted and isinstance(sources, list)
            else []
        )
        output["data_coverage"] = projected_coverage
    queries = snapshot.get("search_queries")
    output["search_queries"] = (
        [query for query in queries if isinstance(query, str)]
        if trusted and isinstance(queries, list)
        else []
    )
    output["black_swan_scenarios"] = _project_macro_items(
        snapshot.get("black_swan_scenarios") if trusted else None,
        _PUBLIC_MACRO_SCENARIO_FIELDS,
    )
    output["gray_rhinos"] = _project_macro_items(
        snapshot.get("gray_rhinos") if trusted else None,
        _PUBLIC_MACRO_RHINO_FIELDS,
    )
    output["opportunities"] = _project_macro_items(
        snapshot.get("opportunities") if trusted else None,
        _PUBLIC_MACRO_OPPORTUNITY_FIELDS,
    )
    output["monitored_events"] = _project_macro_events(
        snapshot.get("monitored_events") if trusted else None,
        macro_event_enrichments,
    )
    return output


def project_public_reactions(reactions: Any) -> list[dict[str, Any]]:
    """Project market reactions onto the strict public API schema."""
    output: list[dict[str, Any]] = []
    for reaction in _records(reactions):
        if _contains_forbidden_identity_token(
            reaction.get("asset_key") or "",
            "asset_key",
        ):
            continue
        projected = _public_reaction(reaction)
        if (
            isinstance(projected.get("asset_key"), str)
            and projected["asset_key"].strip()
        ):
            output.append(projected)
    return output


def _effective_market_confirmation(
    reaction: Mapping[str, Any],
    expected_direction: str,
) -> bool | None:
    if expected_direction not in {"positive", "negative"}:
        return None
    observed_direction = str(
        reaction.get("observed_direction") or ""
    ).strip().lower()
    if observed_direction not in {"positive", "negative", "neutral"}:
        abnormal = _finite_number(reaction.get("abnormal_return"))
        observed_direction = (
            "positive"
            if abnormal is not None and abnormal > 0
            else "negative"
            if abnormal is not None and abnormal < 0
            else "neutral"
        )
    if observed_direction == "neutral":
        return None
    if observed_direction in {"positive", "negative"}:
        return observed_direction == expected_direction
    stored_expected = str(
        reaction.get("expected_direction") or ""
    ).strip().lower()
    stored_confirmation = reaction.get("direction_confirmed")
    if (
        stored_expected == expected_direction
        and isinstance(stored_confirmation, bool)
    ):
        return stored_confirmation
    return None


def _required_market_window(horizon: str) -> str:
    return _HORIZON_REQUIRED_WINDOW.get(
        str(horizon or "unknown").strip().lower(),
        "3D",
    )


def _reaction_is_pending(reaction: Mapping[str, Any]) -> bool:
    status = str(reaction.get("status") or "").strip().lower()
    if status == "pending":
        return True
    # Compatibility for snapshots written just before the pending status was
    # introduced. A bounded reason code plus a due time identifies a future
    # window; technical/data failures remain unavailable and conservative.
    reason_code = str(reaction.get("reason_code") or "").strip().lower()
    return (
        status == "unavailable"
        and reason_code == "window_not_due"
        and isinstance(reaction.get("next_due_at"), str)
        and bool(str(reaction.get("next_due_at") or "").strip())
    )


def _market_reason_counts(
    reactions: Iterable[Mapping[str, Any]],
    *,
    unavailable_only: bool = False,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for reaction in reactions:
        if unavailable_only and (
            str(reaction.get("status") or "").strip().lower()
            != "unavailable"
            or _reaction_is_pending(reaction)
        ):
            continue
        code = str(reaction.get("reason_code") or "").strip().lower()
        if _SAFE_REASON_CODE.fullmatch(code) is not None:
            counts[code] += 1
        elif unavailable_only:
            counts["unspecified"] += 1
    return dict(sorted(counts.items()))


def _market_business_health(
    reactions: list[dict[str, Any]],
    decisions: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    total = len(reactions)
    complete = 0
    preliminary = 0
    pending = 0
    unavailable = 0
    observed: list[datetime] = []
    assets: set[str] = set()
    for reaction in reactions:
        status = str(reaction.get("status") or "").strip().lower()
        if _reaction_is_pending(reaction):
            pending += 1
        elif status == "complete":
            complete += 1
        elif status == "preliminary":
            preliminary += 1
        else:
            unavailable += 1
        asset_key = reaction.get("asset_key")
        if isinstance(asset_key, str) and asset_key.strip():
            assets.add(asset_key.strip())
        timestamp = _utc_datetime(reaction.get("observed_at"))
        if timestamp is not None:
            observed.append(timestamp)

    available = complete + preliminary
    if total == 0:
        status = "unavailable"
        note = "尚无市场验证记录，决策仅可作为待核验候选。"
    elif unavailable:
        status = "degraded" if available or pending else "unavailable"
        note = "部分市场验证因数据或技术原因不可用，相关候选必须降级复核。"
    elif available == 0 and pending:
        status = "pending"
        note = "市场窗口尚未到期；未来窗口不会被当作事件预期失效。"
    else:
        status = "healthy"
        note = "市场验证链路有可用样本；统计相关仍不表示因果。"

    decision_records = [
        decision for decision in decisions if isinstance(decision, Mapping)
    ]
    covered_decisions = 0
    pending_decisions = 0
    unavailable_decisions = 0
    not_applicable_decisions = 0
    scenario_monitoring_decisions = 0
    direction_missing_decisions = 0
    for decision in decision_records:
        validation = decision.get("market_validation")
        validation_status = (
            str(validation.get("status") or "").strip().lower()
            if isinstance(validation, Mapping)
            else "unavailable"
        )
        applicability = (
            str(validation.get("applicability") or "").strip().lower()
            if isinstance(validation, Mapping)
            else ""
        )
        applicability_reason = (
            str(validation.get("applicability_reason") or "").strip().lower()
            if isinstance(validation, Mapping)
            else ""
        )
        degraded = (
            bool(validation.get("degraded"))
            if isinstance(validation, Mapping)
            else True
        )
        if applicability == "not_applicable":
            not_applicable_decisions += 1
            if applicability_reason == "no_event_anchor":
                scenario_monitoring_decisions += 1
            elif applicability_reason == "direction_missing":
                direction_missing_decisions += 1
        elif degraded or validation_status in {
            "unavailable",
            "degraded",
            "error",
            "failed",
        }:
            unavailable_decisions += 1
        elif validation_status == "pending":
            pending_decisions += 1
        elif validation_status in {"complete", "preliminary"}:
            covered_decisions += 1
        else:
            unavailable_decisions += 1

    total_decisions = len(decision_records)
    applicable_decisions = total_decisions - not_applicable_decisions
    if decision_records:
        if (
            applicable_decisions
            and unavailable_decisions == applicable_decisions
        ):
            status = "unavailable"
            note = "当前适用事件窗口的决策均缺少可用市场验证，只能作为待核验候选。"
        elif unavailable_decisions:
            status = "degraded"
            note = "部分适用事件窗口的决策缺少市场验证，不得以其他卡片样本掩盖。"
        elif applicable_decisions and covered_decisions == 0 and pending_decisions:
            status = "pending"
            note = "当前市场窗口均尚未到期；pending 不构成数据故障。"
        elif applicable_decisions == 0 and not_applicable_decisions:
            status = "not_applicable"
            note = "当前决策需采用情景触发或先明确事件预期，不适用事件窗口确认。"
        else:
            status = "healthy"
            note = "适用事件窗口已有市场样本；统计相关仍不表示因果。"
    return {
        "semantics_version": 2,
        "status": status,
        "degraded": status in {"degraded", "unavailable"},
        "total_records": total,
        "available_records": available,
        "complete_records": complete,
        "preliminary_records": preliminary,
        "pending_records": pending,
        "unavailable_records": unavailable,
        "coverage": round(available / total, 4) if total else 0.0,
        "asset_count": len(assets),
        "total_decisions": total_decisions,
        "applicable_decisions": applicable_decisions,
        "covered_decisions": covered_decisions,
        "pending_decisions": pending_decisions,
        "unavailable_decisions": unavailable_decisions,
        "data_failure_decisions": unavailable_decisions,
        "not_applicable_decisions": not_applicable_decisions,
        "no_event_anchor_decisions": scenario_monitoring_decisions,
        "scenario_monitoring_decisions": scenario_monitoring_decisions,
        "direction_missing_decisions": direction_missing_decisions,
        "decision_coverage": (
            round(covered_decisions / applicable_decisions, 4)
            if applicable_decisions
            else None
        ),
        "last_observed_at": (
            max(observed).isoformat() if observed else None
        ),
        "reason_counts": _market_reason_counts(
            reactions,
            unavailable_only=True,
        ),
        "note": note,
    }


def _market_validation(
    reactions: list[dict[str, Any]],
    expected_direction: str,
    horizon: str,
    *,
    applicability: str = "applicable",
    applicability_reason: str | None = None,
    source_scope: str = "event_only",
) -> tuple[dict[str, Any], float]:
    required_window = _required_market_window(horizon)
    if applicability_reason == "no_event_anchor":
        return (
            {
                "status": "unavailable",
                "phase": "unavailable",
                "applicability": applicability,
                "applicability_reason": applicability_reason,
                "source_scope": source_scope,
                "abstain": True,
                "veto": True,
                "degraded": False,
                "direction_confirmed": None,
                "sample_count": 0,
                "required_window": required_window,
                "required_window_complete": False,
                "completed_windows": [],
                "pending_windows": [],
                "unavailable_windows": [],
                "next_review_at": None,
                "reason_counts": {"no_event_anchor": 1},
                "note": (
                    "宏观情景尚无单一事件锚点；改用触发指标监控，"
                    "不以事件后收益验证。"
                ),
                "records": [],
            },
            0.0,
        )
    if not reactions:
        if applicability_reason == "direction_missing":
            return (
                {
                    "status": "unavailable",
                    "phase": "direction_unavailable",
                    "applicability": applicability,
                    "applicability_reason": applicability_reason,
                    "source_scope": source_scope,
                    "abstain": True,
                    "veto": True,
                    "degraded": False,
                    "direction_confirmed": None,
                    "sample_count": 0,
                    "required_window": required_window,
                    "required_window_complete": False,
                    "completed_windows": [],
                    "pending_windows": [],
                    "unavailable_windows": [],
                    "next_review_at": None,
                    "reason_counts": {"direction_missing": 1},
                    "note": (
                        "事件预期尚未明确，无法判断资产应跑赢还是跑输基准；"
                        "先补充方向证据。"
                    ),
                    "records": [],
                },
                0.0,
            )
        return (
            {
                "status": "unavailable",
                "phase": "unavailable",
                "applicability": applicability,
                "applicability_reason": applicability_reason,
                "source_scope": source_scope,
                "abstain": True,
                "veto": True,
                "degraded": True,
                "direction_confirmed": None,
                "sample_count": 0,
                "required_window": required_window,
                "required_window_complete": False,
                "completed_windows": [],
                "pending_windows": [],
                "unavailable_windows": [],
                "next_review_at": None,
                "reason_counts": {"no_records": 1},
                "note": "暂无可用市场样本，必须 abstain 并等待共同交易日数据。",
                "records": [],
            },
            0.0,
        )

    confirmations_by_window: dict[str, list[bool | None]] = defaultdict(list)
    sample_counts: list[int] = []
    public_records: list[dict[str, Any]] = []
    completed_windows: set[str] = set()
    pending_windows: set[str] = set()
    unavailable_windows: set[str] = set()
    next_review_times: list[datetime] = []
    for reaction in reactions:
        status = str(reaction.get("status") or "").strip().lower()
        window = str(reaction.get("window") or "").strip().upper()
        sample_number = _finite_number(reaction.get("sample_count"))
        sample_count = max(0, int(sample_number or 0))
        sample_counts.append(sample_count)
        minimum = _WINDOW_MINIMUM_SAMPLES.get(
            window,
            2,
        )
        complete = (
            window in _MARKET_WINDOWS
            and status == "complete"
            and sample_count >= minimum
        )
        projected = _public_reaction(reaction)
        effective_confirmation = (
            _effective_market_confirmation(reaction, expected_direction)
            if complete
            else None
        )
        projected["direction_confirmed"] = effective_confirmation
        projected["evaluated_direction"] = expected_direction
        public_records.append(projected)
        next_due = _utc_datetime(reaction.get("next_due_at"))
        if next_due is not None:
            next_review_times.append(next_due)
        if complete:
            completed_windows.add(window)
            confirmations_by_window[window].append(effective_confirmation)
        elif window in _MARKET_WINDOWS and _reaction_is_pending(reaction):
            pending_windows.add(window)
        elif window in _MARKET_WINDOWS:
            unavailable_windows.add(window)

    confirmations = [
        value
        for window in _MARKET_WINDOWS
        for value in confirmations_by_window.get(window, [])
    ]
    contrary = any(value is False for value in confirmations)
    inconclusive = bool(confirmations) and any(
        value is None for value in confirmations
    )
    confirming_windows = [
        window
        for window in _MARKET_WINDOWS
        if confirmations_by_window.get(window)
        and all(
            value is True for value in confirmations_by_window[window]
        )
    ]
    highest_confirmed = confirming_windows[-1] if confirming_windows else None
    required_index = _MARKET_WINDOWS.index(required_window)
    required_done = (
        required_window in confirming_windows
        and required_window not in pending_windows
        and required_window not in unavailable_windows
    )
    unavailable_due = any(
        _MARKET_WINDOWS.index(window) <= required_index
        for window in unavailable_windows
    )
    invalid_direction = (
        applicability_reason == "direction_missing"
        or expected_direction not in {"positive", "negative"}
    )
    veto = contrary or inconclusive or unavailable_due or invalid_direction

    if invalid_direction:
        status = "unavailable"
        phase = "direction_unavailable"
        note = "事件预期尚未明确，现有市场样本无法完成验证。"
    elif contrary:
        status = "complete" if required_done else "preliminary"
        phase = "contrary"
        note = "已有共同交易日样本未支持事件预期，候选必须停止并复核。"
    elif inconclusive:
        status = "complete" if required_done else "preliminary"
        phase = "inconclusive"
        note = "已有样本方向中性或不一致，候选必须继续复核。"
    elif highest_confirmed is not None:
        status = "complete" if required_done else "preliminary"
        phase = f"confirmed_{highest_confirmed.lower()}"
        if unavailable_due:
            note = "已有早期确认，但所需窗口因数据或技术原因不可用，必须降级复核。"
        elif required_done:
            note = "对应期限所需窗口已确认；更长窗口只用于增强，仍需人工复核。"
        else:
            note = "1D/3D 样本仅形成渐进评分；必需窗口到期前不得形成行动候选。"
    elif pending_windows and not unavailable_due:
        status = "pending"
        phase = "awaiting_1d"
        note = "共同交易日窗口尚未到期；pending 不能用来否定事件预期。"
    else:
        status = "unavailable"
        phase = "unavailable"
        note = "市场样本因数据或技术原因不可用，必须等待重新验证。"

    abstain = veto or highest_confirmed is None or not required_done
    if contrary:
        direction_confirmed: bool | None = False
    elif highest_confirmed is not None and not inconclusive:
        direction_confirmed = True
    else:
        direction_confirmed = None
    score = (
        _MARKET_WINDOW_SCORE.get(highest_confirmed, 0.0)
        if highest_confirmed is not None and not veto
        else 0.0
    )
    return (
        {
            "status": status,
            "phase": phase,
            "applicability": applicability,
            "applicability_reason": applicability_reason,
            "source_scope": source_scope,
            "abstain": abstain,
            "veto": veto,
            "degraded": unavailable_due and not invalid_direction,
            "direction_confirmed": direction_confirmed,
            "sample_count": max(sample_counts, default=0),
            "required_window": required_window,
            "required_window_complete": required_done,
            "completed_windows": sorted(
                completed_windows,
                key=_MARKET_WINDOWS.index,
            ),
            "pending_windows": sorted(
                pending_windows,
                key=_MARKET_WINDOWS.index,
            ),
            "unavailable_windows": sorted(
                unavailable_windows,
                key=_MARKET_WINDOWS.index,
            ),
            "next_review_at": (
                min(next_review_times).isoformat()
                if next_review_times
                else None
            ),
            "reason_counts": _market_reason_counts(reactions),
            "note": note,
            "records": public_records,
        },
        round(score, 4),
    )


def _total_score(components: Mapping[str, Any]) -> float:
    total = sum(
        SCORE_WEIGHTS[name] * _unit_interval(components.get(name))
        for name in SCORE_WEIGHTS
    )
    return round(total, 4)


def _action_stage(
    classification: str,
    total_score: float,
    market_validation: Mapping[str, Any],
) -> str:
    market_ready = (
        market_validation.get("abstain", True) is False
        and market_validation.get("veto", True) is False
        and market_validation.get("required_window_complete") is True
        and market_validation.get("direction_confirmed") is True
    )
    if classification == "conflict" or not market_ready:
        return "verify" if total_score >= 0.45 else "observe"
    if total_score < 0.70:
        return "verify" if total_score >= 0.45 else "observe"
    return (
        "candidate_scale_in"
        if classification == "opportunity"
        else "candidate_reduce_or_hedge"
    )


def _trigger_and_invalidation(classification: str) -> tuple[str, str]:
    if classification == "opportunity":
        trigger = (
            "机制证据获独立来源支持，且对应期限的共同交易日窗口分阶段确认正向方向。"
        )
    elif classification == "risk":
        trigger = (
            "风险机制获独立来源支持，且对应期限的共同交易日窗口分阶段确认负向方向。"
        )
    else:
        trigger = "待冲突来源经新增机制证据和到期市场窗口消解后再形成候选行动。"
    invalidation = (
        "若新增机制证据反转，或后续到期市场窗口不再支持当前方向，则候选失效。"
    )
    return trigger, invalidation


def _public_evidence(relation: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("source_type", "source_id", "relation_type", "rationale"):
        value = relation.get(field)
        if _is_public_scalar(value):
            result[field] = _public_sanitize(value, field_name=field)
    direction = relation.get("direction")
    result["direction"] = (
        _direction(direction) if _is_public_scalar(direction) else "neutral"
    )
    result["detail"] = _public_evidence_detail(relation)
    return result


def _data_as_of(
    relations: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    now: datetime,
) -> str | None:
    candidates: list[datetime] = []

    def acceptable(value: Any) -> datetime | None:
        parsed = _utc_datetime(value)
        if parsed is None or parsed > now + timedelta(minutes=5):
            return None
        return parsed

    for relation in relations:
        evidence = _relation_evidence(relation)
        evidence_times = [
            parsed
            for value in (
                evidence.get("published_at"),
                evidence.get("generated_at"),
                evidence.get("timestamp"),
            )
            if (parsed := acceptable(value)) is not None
        ]
        if evidence_times:
            candidates.extend(evidence_times)
        else:
            created_at = acceptable(relation.get("created_at"))
            if created_at is not None:
                candidates.append(created_at)
    for reaction in reactions:
        timestamps = reaction.get("data_timestamps")
        reaction_times: list[datetime] = []
        if isinstance(timestamps, Mapping):
            reaction_times = [
                parsed
                for key in ("end", "benchmark_end")
                if (parsed := acceptable(timestamps.get(key))) is not None
            ]
        if reaction_times:
            candidates.extend(reaction_times)
        else:
            observed_at = acceptable(reaction.get("observed_at"))
            if observed_at is not None:
                candidates.append(observed_at)
    return max(candidates).isoformat() if candidates else None


def _contrary_evidence(
    relations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    classification: str,
    reactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if classification == "conflict":
        contrary = [
            deepcopy(item)
            for relation, item in zip(relations, evidence)
            if _direction(relation.get("direction")) != "neutral"
        ]
    else:
        primary = "positive" if classification == "opportunity" else "negative"
        contrary = [
            deepcopy(item)
            for relation, item in zip(relations, evidence)
            if _direction(relation.get("direction")) != primary
        ]
    for reaction in reactions:
        if (
            reaction.get("status") != "complete"
            or reaction.get("direction_confirmed") is not False
        ):
            continue
        item: dict[str, Any] = {
            "direction": "contrary",
            "detail": "完整观察窗口内，资产相对基准的表现未支持事件预期。",
        }
        for field in ("source_type", "source_id"):
            if _is_public_scalar(reaction.get(field)):
                item[field] = _public_sanitize(
                    reaction[field],
                    field_name=field,
                )
        contrary.append(item)
    return contrary


def _build_impact_matrix(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    topics = sorted({str(card["topic_key"]) for card in decisions})
    assets = sorted({str(card["asset_key"]) for card in decisions})
    indexed = {
        (str(card["topic_key"]), str(card["asset_key"])): card
        for card in decisions
    }
    rows: list[dict[str, Any]] = []
    for topic in topics:
        cells: list[dict[str, Any] | None] = []
        for asset in assets:
            card = indexed.get((topic, asset))
            cells.append(
                None
                if card is None
                else {
                    "classification": card["classification"],
                    "direction": card["direction"],
                    "total_score": card["total_score"],
                    "action_stage": card["action_stage"],
                    "source_count": card["source_count"],
                    "human_review_required": card["human_review_required"],
                }
            )
        rows.append({"topic_key": topic, "cells": cells})
    return {"columns": assets, "rows": rows}


def _decision_overview(
    decisions: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    cards = [card for card in decisions if isinstance(card, Mapping)]
    overview = {
        "total": len(cards),
        "candidate": 0,
        "market_confirmed": 0,
        "contrary": 0,
        "pending_window": 0,
        "scenario_monitoring": 0,
        "direction_missing": 0,
        "data_unavailable": 0,
    }
    for card in cards:
        stage = str(card.get("action_stage") or "").strip().lower()
        if stage in {
            "candidate_reduce_or_hedge",
            "candidate_scale_in",
            "reduce_or_hedge",
            "scale_in",
        }:
            overview["candidate"] += 1
        market = card.get("market_validation")
        if not isinstance(market, Mapping):
            overview["data_unavailable"] += 1
            continue
        status = str(market.get("status") or "").strip().lower()
        phase = str(market.get("phase") or "").strip().lower()
        applicability = str(
            market.get("applicability") or "applicable"
        ).strip().lower()
        applicability_reason = str(
            market.get("applicability_reason") or ""
        ).strip().lower()
        if (
            status == "complete"
            and phase.startswith("confirmed_")
            and market.get("required_window_complete") is True
            and market.get("direction_confirmed") is True
            and market.get("abstain") is False
            and market.get("veto") is False
        ):
            overview["market_confirmed"] += 1
        if phase == "contrary":
            overview["contrary"] += 1
        if applicability == "applicable" and (
            market.get("pending_windows") or status == "pending"
        ):
            overview["pending_window"] += 1
        if applicability_reason == "no_event_anchor":
            overview["scenario_monitoring"] += 1
        elif applicability_reason == "direction_missing":
            overview["direction_missing"] += 1
        elif applicability == "applicable" and (
            market.get("degraded") is True
            or status
            in {
                "unavailable",
                "degraded",
                "error",
                "failed",
            }
        ):
            overview["data_unavailable"] += 1
    return overview


def build_public_decisions(
    relations: Any,
    market_reactions: Any = None,
    coverage: Any = 1.0,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Aggregate public topic/asset cards without loading private positions."""
    current = _utc_datetime(now) or datetime.now(timezone.utc)
    clean_relations = [
        relation
        for relation in project_public_relations(relations)
        if _relation_is_decision_eligible(relation, current)
    ]
    clean_reactions = project_public_reactions(market_reactions)
    coverage_score = _coverage_value(coverage)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for relation in clean_relations:
        topic_key = str(relation.get("topic_key") or "").strip()
        asset_key = str(relation.get("asset_key") or "").strip()
        if topic_key and asset_key:
            grouped[(topic_key, asset_key)].append(relation)

    decisions: list[dict[str, Any]] = []
    used_reactions: list[dict[str, Any]] = []
    used_reaction_keys: set[tuple[str, str, str, str]] = set()
    for topic_key, asset_key in sorted(grouped):
        group = grouped[(topic_key, asset_key)]
        classification, direction = _classification(group)
        horizon = _group_horizon(group)
        applicability, applicability_reason, source_scope = (
            _market_applicability(group, direction)
        )
        evidence_source_pairs = {
            identity
            for index, relation in enumerate(group)
            if (identity := _source_identity(relation, index)) is not None
        }
        market_source_pairs = (
            _directional_event_source_pairs(group, direction)
            if applicability == "applicable"
            else set()
        )
        matched_reactions = [
            reaction
            for reaction in clean_reactions
            if _matches_reaction(reaction, asset_key, market_source_pairs)
        ]
        for reaction in matched_reactions:
            reaction_key = (
                str(reaction.get("source_type") or ""),
                str(reaction.get("source_id") or ""),
                str(reaction.get("asset_key") or ""),
                str(reaction.get("window") or "").upper(),
            )
            if reaction_key in used_reaction_keys:
                continue
            used_reaction_keys.add(reaction_key)
            used_reactions.append(reaction)
        market_validation, market_score = _market_validation(
            matched_reactions,
            direction,
            horizon,
            applicability=applicability,
            applicability_reason=applicability_reason,
            source_scope=source_scope,
        )
        confidence_values: list[float] = []
        for relation in group:
            confidence = _unit_interval(relation.get("confidence"))
            if str(relation.get("source_type") or "") == "macro_snapshot":
                confidence *= 0.5 + (0.5 * coverage_score)
            confidence_values.append(confidence)
        evidence = [_public_evidence(relation) for relation in group]
        components = {
            "strength": round(
                _average(_unit_interval(item.get("strength")) for item in group),
                4,
            ),
            "confidence": round(_average(confidence_values), 4),
            "freshness": round(
                _average(_relation_freshness(item, current) for item in group),
                4,
            ),
            "corroboration": _corroboration(len(evidence_source_pairs)),
            "market_confirmation": market_score,
            "coverage": coverage_score,
        }
        total_score = _total_score(components)
        trigger, invalidation = _trigger_and_invalidation(classification)
        card = {
            "topic_key": topic_key,
            "asset_key": asset_key,
            "classification": classification,
            "direction": direction,
            "horizon": horizon,
            "data_as_of": _data_as_of(group, matched_reactions, current),
            "evidence": evidence,
            "source_count": len(evidence_source_pairs),
            "mechanism_relations": [_public_relation(item) for item in group],
            "market_validation": market_validation,
            "score_components": components,
            "confidence": components["confidence"],
            "total_score": total_score,
            "action_stage": _action_stage(
                classification,
                total_score,
                market_validation,
            ),
            "trigger": trigger,
            "invalidation": invalidation,
            "contrary_evidence": _contrary_evidence(
                group,
                evidence,
                classification,
                market_validation["records"],
            ),
            "human_review_required": True,
            "decision_status": "candidate",
        }
        decisions.append(card)

    result = {
        "decisions": decisions,
        "impact_matrix": _build_impact_matrix(decisions),
        "decision_overview": _decision_overview(decisions),
        "evidence_policy": EVIDENCE_POLICY,
        "business_health": {
            "market_validation": _market_business_health(
                used_reactions,
                decisions,
            ),
        },
    }
    return _public_sanitize(result)


_SUMMARY_CARD_FIELDS = (
    "topic_key",
    "asset_key",
    "classification",
    "direction",
    "horizon",
    "data_as_of",
    "source_count",
    "score_components",
    "confidence",
    "total_score",
    "action_stage",
    "trigger",
    "invalidation",
    "human_review_required",
    "decision_status",
)


def _decision_sort_key(
    card: Mapping[str, Any],
) -> tuple[int, int, float, str, str]:
    stage_rank = {
        "candidate_reduce_or_hedge": 0,
        "candidate_scale_in": 1,
        # Legacy snapshots remain sortable during a rolling deployment, but
        # the v2 engine never emits these imperative stage names.
        "reduce_or_hedge": 0,
        "scale_in": 1,
        "verify": 2,
        "observe": 3,
    }
    stage = str(card.get("action_stage") or "")
    try:
        score = float(card.get("total_score") or 0.0)
    except (TypeError, ValueError, OverflowError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    market = card.get("market_validation")
    status = (
        str(market.get("status") or "").strip().lower()
        if isinstance(market, Mapping)
        else "unavailable"
    )
    applicability = (
        str(market.get("applicability") or "applicable").strip().lower()
        if isinstance(market, Mapping)
        else "applicable"
    )
    if status in {"complete", "preliminary"}:
        market_rank = 0
    elif status == "pending":
        market_rank = 1
    elif applicability == "applicable":
        market_rank = 2
    else:
        market_rank = 3
    return (
        stage_rank.get(stage, 9),
        market_rank,
        -score,
        str(card.get("topic_key") or ""),
        str(card.get("asset_key") or ""),
    )


def _select_summary_cards(
    cards: list[Mapping[str, Any]],
    limit: int,
) -> tuple[list[Mapping[str, Any]], dict[str, int | str]]:
    """Keep global leaders while preventing one theme or asset taking over."""
    if not cards:
        return [], {
            "name": "diversified_top_score_v1",
            "global_reserve": 0,
            "topic_quota": 0,
            "asset_quota": 0,
        }

    global_reserve = min(2, limit, len(cards))
    topic_quota = max(2, math.ceil(limit / 4))
    asset_quota = max(1, math.ceil(limit / 6))
    selected = list(cards[:global_reserve])
    selected_ids = {id(card) for card in selected}
    topic_counts = Counter(
        str(card.get("topic_key") or "") for card in selected
    )
    asset_counts = Counter(
        str(card.get("asset_key") or "") for card in selected
    )

    for card in cards[global_reserve:]:
        if len(selected) >= limit:
            break
        topic = str(card.get("topic_key") or "")
        asset = str(card.get("asset_key") or "")
        if topic_counts[topic] >= topic_quota:
            continue
        if asset_counts[asset] >= asset_quota:
            continue
        selected.append(card)
        selected_ids.add(id(card))
        topic_counts[topic] += 1
        asset_counts[asset] += 1

    # Sparse datasets should still fill the screen. Quotas define the first
    # pass, not a reason to hide otherwise valid candidates.
    for card in cards:
        if len(selected) >= limit:
            break
        if id(card) in selected_ids:
            continue
        selected.append(card)
        selected_ids.add(id(card))

    selected.sort(key=_decision_sort_key)
    return selected, {
        "name": "diversified_top_score_v1",
        "global_reserve": global_reserve,
        "topic_quota": topic_quota,
        "asset_quota": asset_quota,
    }


def project_decision_summary(
    payload: Any,
    *,
    decision_limit: int = 12,
) -> dict[str, Any]:
    """Return a small, public allow-list projection for the first screen.

    Evidence, mechanism relations and raw market records stay in the matching
    detail response.  The summary matrix is built only from the projected
    cards so every interactive cell has a matching decision card.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("decision payload must be an object")
    raw_cards = payload.get("decisions")
    cards = [card for card in raw_cards or [] if isinstance(card, Mapping)]
    cards.sort(key=_decision_sort_key)
    limit = max(1, min(int(decision_limit), 50))
    selected_cards, selection_policy = _select_summary_cards(cards, limit)

    projected: list[dict[str, Any]] = []
    for card in selected_cards:
        item = {
            field: deepcopy(card[field])
            for field in _SUMMARY_CARD_FIELDS
            if field in card
        }
        market = card.get("market_validation")
        if isinstance(market, Mapping):
            item["market_validation"] = {
                key: deepcopy(value)
                for key, value in market.items()
                if key != "records"
            }
        item["detail_available"] = True
        projected.append(item)

    full_matrix = payload.get("impact_matrix")
    total_assets = 0
    if isinstance(full_matrix, Mapping) and isinstance(
        full_matrix.get("columns"), list
    ):
        total_assets = len(full_matrix["columns"])

    return _public_sanitize(
        {
            "decisions": projected,
            "impact_matrix": _build_impact_matrix(projected),
            "evidence_policy": payload.get("evidence_policy", EVIDENCE_POLICY),
            "business_health": deepcopy(payload.get("business_health", {})),
            "decision_overview": deepcopy(
                payload.get("decision_overview", _decision_overview(cards))
            ),
            "summary": True,
            "total_decisions": len(cards),
            "total_assets": total_assets,
            "selection_policy": selection_policy,
        }
    )


def find_decision(
    payload: Any,
    topic_key: str,
    asset_key: str,
) -> dict[str, Any] | None:
    """Find one exact decision card without exposing unrelated evidence."""
    if not isinstance(payload, Mapping):
        return None
    for card in payload.get("decisions") or []:
        if not isinstance(card, Mapping):
            continue
        if (
            str(card.get("topic_key") or "") == str(topic_key or "")
            and str(card.get("asset_key") or "") == str(asset_key or "")
        ):
            return _public_sanitize(deepcopy(dict(card)))
    return None


def ingest_sources(
    events: Any,
    macro_snapshot: Any = None,
    *,
    persist: bool = False,
    repository: Any = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    """Extract relations and optionally persist them through replace_relations."""
    current = _utc_datetime(now) or datetime.now(timezone.utc)
    event_records = _records(events)
    macro_copy = (
        deepcopy(dict(macro_snapshot))
        if isinstance(macro_snapshot, Mapping)
        else None
    )
    batches: list[tuple[str, str, list[dict[str, Any]]]] = []
    relations: list[dict[str, Any]] = []

    for event in event_records:
        source_id = str(event.get("id") or event.get("dedup_key") or "")
        if not _event_time_is_eligible(event.get("published_at"), current):
            if source_id:
                batches.append(("event", source_id, []))
            continue
        generated = _public_sanitize(
            relation_engine.event_relations(deepcopy(event))
        )
        event_edges = [
            item for item in generated if isinstance(item, dict)
        ]
        source_id = str(
            source_id
            or (event_edges[0].get("source_id") if event_edges else "")
        )
        if event_edges or source_id:
            batches.append(("event", source_id, event_edges))
        relations.extend(event_edges)

    if macro_copy is not None:
        generated = _public_sanitize(
            relation_engine.macro_relations(deepcopy(macro_copy))
        )
        macro_edges = [
            item for item in generated if isinstance(item, dict)
        ]
        source_id = str(
            macro_copy.get("snapshot_id")
            or macro_copy.get("id")
            or macro_copy.get("created_at")
            or (macro_edges[0].get("source_id") if macro_edges else "")
        )
        if macro_edges or source_id:
            batches.append(("macro_snapshot", source_id, macro_edges))
        relations.extend(macro_edges)

    if persist:
        if repository is None:
            try:
                from kol_dashboard import db as repository
            except ModuleNotFoundError:
                import db as repository
        replace = getattr(repository, "replace_relations", None)
        if not callable(replace):
            raise TypeError("repository must provide replace_relations")
        for source_type, source_id, edges in batches:
            if not source_id:
                raise ValueError("persisted relation source requires an id")
            replace(source_type, source_id, deepcopy(edges))
    return relations


def _is_older_than(
    value: Any,
    now: datetime,
    maximum_age_seconds: float,
) -> bool:
    observed = _utc_datetime(value)
    if observed is None:
        return True
    age = (now - observed).total_seconds()
    if age < -300:
        return True
    age = max(0.0, age)
    return age > maximum_age_seconds


def _estimated_exposure(
    matched: list[dict[str, Any]],
    quote: Mapping[str, Any] | None,
    *,
    quote_is_stale: bool,
) -> dict[str, Any] | None:
    if not matched or quote is None or quote_is_stale:
        return None
    price = _finite_number(quote.get("price"))
    currency = str(quote.get("currency") or "").strip()
    if price is None or price <= 0 or not currency:
        return None
    quantities = [_finite_number(item.get("quantity")) for item in matched]
    if any(value is None or value <= 0 for value in quantities):
        return None
    position_currencies = {
        str(item.get("currency") or "").strip() for item in matched
    }
    if position_currencies != {currency}:
        return None
    value = price * sum(value for value in quantities if value is not None)
    if not math.isfinite(value):
        return None
    return {
        "value": round(value, 8),
        "currency": currency,
        "quote_observed_at": quote.get("observed_at") or quote.get("timestamp"),
    }


def build_private_overlay(
    public_decisions: Any,
    positions: Any,
    quotes: Any = None,
    *,
    now: Any = None,
    position_stale_days: float = 7.0,
    quote_stale_seconds: float = 24 * 60 * 60,
) -> dict[str, Any]:
    """Deep-copy public cards and add position context only when explicitly called."""
    if not isinstance(public_decisions, Mapping):
        raise TypeError("public_decisions must be an object")
    result = deepcopy(dict(public_decisions))
    if isinstance(positions, Mapping):
        position_records = _records(positions.get("positions"))
    else:
        position_records = _records(positions)
    quote_map = deepcopy(dict(quotes)) if isinstance(quotes, Mapping) else {}
    current = _utc_datetime(now) or datetime.now(timezone.utc)
    position_days = _finite_number(position_stale_days)
    quote_seconds = _finite_number(quote_stale_seconds)
    if position_days is None or position_days < 0:
        raise ValueError("position_stale_days must be non-negative")
    if quote_seconds is None or quote_seconds < 0:
        raise ValueError("quote_stale_seconds must be non-negative")

    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for position in position_records:
        asset_key = str(position.get("asset_key") or "").strip()
        if asset_key:
            by_asset[asset_key].append(position)

    decisions = result.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("public_decisions.decisions must be a list")
    for card in decisions:
        if not isinstance(card, dict):
            continue
        asset_key = str(card.get("asset_key") or "")
        matched = deepcopy(by_asset.get(asset_key, []))
        raw_quote = quote_map.get(asset_key)
        quote = raw_quote if isinstance(raw_quote, Mapping) else None
        position_is_stale = bool(matched) and any(
            _is_older_than(
                item.get("as_of"),
                current,
                position_days * 86_400,
            )
            for item in matched
        )
        quote_timestamp = (
            quote.get("observed_at") or quote.get("timestamp")
            if quote is not None
            else None
        )
        price = _finite_number(quote.get("price")) if quote is not None else None
        quote_is_stale = bool(matched) and (
            quote is None
            or str(quote.get("status") or "available") == "unavailable"
            or price is None
            or price <= 0
            or _is_older_than(quote_timestamp, current, quote_seconds)
        )
        stale = position_is_stale or quote_is_stale
        leverage_flag = any(bool(item.get("is_leveraged")) for item in matched)

        card["matched_positions"] = matched
        card["estimated_exposure"] = _estimated_exposure(
            matched,
            quote,
            quote_is_stale=quote_is_stale,
        )
        card["stale"] = stale
        card["leverage_flag"] = leverage_flag

        components = card.get("score_components")
        if isinstance(components, dict) and matched and stale:
            confidence = _unit_interval(components.get("confidence"))
            if position_is_stale:
                confidence *= 0.8
            if quote_is_stale:
                confidence *= 0.75
            components["confidence"] = round(confidence, 4)
            card["confidence"] = components["confidence"]
            card["total_score"] = _total_score(components)
            card["action_stage"] = _action_stage(
                str(card.get("classification") or "conflict"),
                card["total_score"],
                card.get("market_validation") or {},
            )

    result["impact_matrix"] = _build_impact_matrix(
        [card for card in decisions if isinstance(card, dict)]
    )
    matched_asset_keys = {
        str(card.get("asset_key") or "")
        for card in decisions
        if isinstance(card, Mapping) and card.get("matched_positions")
    }
    matched_positions = [
        position
        for position in position_records
        if str(position.get("asset_key") or "") in matched_asset_keys
    ]
    position_dates = [
        parsed
        for position in position_records
        if (parsed := _utc_datetime(position.get("as_of"))) is not None
    ]
    stale_position_count = sum(
        1
        for position in position_records
        if _is_older_than(
            position.get("as_of"),
            current,
            position_days * 86_400,
        )
    )
    candidate_stages = {
        "candidate_reduce_or_hedge",
        "candidate_scale_in",
        "reduce_or_hedge",
        "scale_in",
    }
    result["portfolio_overview"] = {
        "position_count": len(position_records),
        "matched_position_count": len(matched_positions),
        "unmatched_position_count": len(position_records) - len(matched_positions),
        "impacted_asset_count": len(matched_asset_keys),
        "leveraged_match_count": sum(
            1 for position in matched_positions if position.get("is_leveraged")
        ),
        "stale_position_count": stale_position_count,
        "oldest_as_of": (
            min(position_dates).date().isoformat() if position_dates else None
        ),
        "candidate_matched_decisions": sum(
            1
            for card in decisions
            if isinstance(card, Mapping)
            and card.get("matched_positions")
            and str(card.get("action_stage") or "") in candidate_stages
        ),
        "matching_policy": "exact_asset_key_v1",
        "indirect_exposure_calculated": False,
        "trade_execution_available": False,
    }
    private_overview = _decision_overview(
        card for card in decisions if isinstance(card, Mapping)
    )
    private_overview["portfolio_matched"] = sum(
        1
        for card in decisions
        if isinstance(card, Mapping) and card.get("matched_positions")
    )
    result["decision_overview"] = private_overview
    return result
