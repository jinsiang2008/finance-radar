"""Pure decision aggregation with an explicit opt-in private overlay.

Public decisions are built only from relation and market-reaction evidence.
They never load holdings.  KOL text and market statistics are treated as
signals for review, not as proof of causality or instructions to trade.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

try:
    from kol_dashboard import relation_engine
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    import relation_engine


EVIDENCE_POLICY = (
    "KOL 信息仅用于发现待验证线索；统计相关不等于因果。"
    "市场样本不完整或相互冲突时必须 abstain，所有结论均需人工复核。"
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
EVENT_RELATION_MAX_AGE_HOURS = 72

_PUBLIC_FORBIDDEN_TOKENS = (
    "shares",
    "quantity",
    "account",
    "cost",
    "portfolio",
    "matched_positions",
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


def _event_time_is_eligible(value: Any, now: datetime) -> bool:
    published = _verified_event_time(value)
    if published is None:
        return False
    age_seconds = (now - published).total_seconds()
    return (
        age_seconds >= 0
        and age_seconds <= EVENT_RELATION_MAX_AGE_HOURS * 3600
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
    return _event_time_is_eligible(published_at, now)


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


def _sanitize_public_string(value: str) -> str:
    result = value
    for token in _PUBLIC_FORBIDDEN_TOKENS:
        result = re.sub(re.escape(token), "[redacted]", result, flags=re.IGNORECASE)
    return result


def _public_sanitize(value: Any) -> Any:
    """Return JSON-safe public data with private vocabulary removed recursively."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = str(key)
            if _contains_forbidden_token(clean_key):
                continue
            result[clean_key] = _public_sanitize(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_public_sanitize(item) for item in value]
    if isinstance(value, str):
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
    return _sanitize_public_string(str(value))


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
            result[field] = _public_sanitize(value)
    return result


def _public_relation(relation: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for field in _PUBLIC_RELATION_FIELDS:
        if field in relation and _is_public_scalar(relation[field]):
            result[field] = _public_sanitize(relation[field])
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
                key: _public_sanitize(value[key])
                for key in (
                    "start",
                    "end",
                    "benchmark_start",
                    "benchmark_end",
                )
                if key in value
                and _is_public_scalar(value[key])
            }
        elif _is_public_scalar(value):
            result[field] = _public_sanitize(value)
    return result


def project_public_relations(relations: Any) -> list[dict[str, Any]]:
    """Project stored relations onto the strict public API schema."""
    output: list[dict[str, Any]] = []
    for relation in _records(relations):
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


def _public_macro_value(value: Any) -> Any:
    if _is_public_scalar(value):
        return _public_sanitize(value)
    if isinstance(value, list):
        return [
            _public_sanitize(item)
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
            clean = _public_macro_value(item[field])
            if clean is not None:
                projected[field] = clean
        output.append(projected)
    return output


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
                projected[field] = _public_sanitize(item[field])
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
        field: _public_sanitize(value[field])
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
    return output


def project_public_macro(snapshot: Any) -> dict[str, Any]:
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
            output[field] = _public_sanitize(snapshot[field])
    if trusted:
        output["public_schema_version"] = PUBLIC_MACRO_SCHEMA_VERSION
    composite = snapshot.get("composite_risk")
    if isinstance(composite, Mapping):
        composite_fields = ("score", "level", "label") if trusted else ("score", "level")
        output["composite_risk"] = {
            field: _public_sanitize(composite[field])
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
    coverage = snapshot.get("data_coverage")
    if isinstance(coverage, Mapping):
        projected_coverage = {
            field: _public_sanitize(coverage[field])
            for field in ("available", "total", "pct")
            if field in coverage and _is_public_scalar(coverage[field])
        }
        sources = coverage.get("sources")
        projected_coverage["sources"] = (
            [
                {
                    field: _public_sanitize(source[field])
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
    output["monitored_events"] = _project_macro_items(
        snapshot.get("monitored_events") if trusted else None,
        _PUBLIC_MACRO_EVENT_FIELDS,
    )
    return output


def project_public_reactions(reactions: Any) -> list[dict[str, Any]]:
    """Project market reactions onto the strict public API schema."""
    output: list[dict[str, Any]] = []
    for reaction in _records(reactions):
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


def _market_validation(
    reactions: list[dict[str, Any]],
    expected_direction: str,
) -> tuple[dict[str, Any], float]:
    if not reactions:
        return (
            {
                "status": "unavailable",
                "abstain": True,
                "direction_confirmed": None,
                "sample_count": 0,
                "note": "暂无可用市场样本，必须 abstain 并等待共同交易日数据。",
                "records": [],
            },
            0.0,
        )

    enough: list[bool] = []
    confirmations: list[bool | None] = []
    sample_counts: list[int] = []
    public_records: list[dict[str, Any]] = []
    for reaction in reactions:
        status = str(reaction.get("status") or "").lower()
        sample_number = _finite_number(reaction.get("sample_count"))
        sample_count = max(0, int(sample_number or 0))
        sample_counts.append(sample_count)
        minimum = _WINDOW_MINIMUM_SAMPLES.get(
            str(reaction.get("window") or "").upper(),
            2,
        )
        complete = status == "complete" and sample_count >= minimum
        enough.append(complete)
        projected = _public_reaction(reaction)
        effective_confirmation = (
            _effective_market_confirmation(reaction, expected_direction)
            if complete
            else None
        )
        projected["direction_confirmed"] = effective_confirmation
        projected["evaluated_direction"] = expected_direction
        public_records.append(projected)
        if complete:
            confirmations.append(effective_confirmation)

    abstain = (
        not enough
        or not all(enough)
        or expected_direction not in {"positive", "negative"}
        or len(confirmations) != len(reactions)
        or not confirmations
        or not all(value is True for value in confirmations)
    )
    if not abstain:
        status = "complete"
        note = "完整共同交易日样本可用于方向核验；统计相关仍不表示因果。"
    elif enough and all(enough):
        status = "complete"
        note = "完整市场样本未确认机制方向，必须 abstain 并复核相反证据。"
    elif any(str(item.get("status") or "").lower() in {"complete", "preliminary"} for item in reactions):
        status = "preliminary"
        note = "市场样本尚不完整，必须 abstain，不能据此放大行动。"
    else:
        status = "unavailable"
        note = "市场样本不可用，必须 abstain 并等待重新验证。"

    direction_confirmed: bool | None
    if confirmations and all(value is True for value in confirmations):
        direction_confirmed = True
    elif any(value is False for value in confirmations):
        direction_confirmed = False
    else:
        direction_confirmed = None
    score = 0.0 if abstain else 1.0
    return (
        {
            "status": status,
            "abstain": abstain,
            "direction_confirmed": direction_confirmed,
            "sample_count": max(sample_counts, default=0),
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
    if classification == "conflict" or market_validation.get("abstain", True):
        return "verify" if total_score >= 0.45 else "observe"
    if total_score < 0.70:
        return "verify" if total_score >= 0.45 else "observe"
    return "scale_in" if classification == "opportunity" else "reduce_or_hedge"


def _trigger_and_invalidation(classification: str) -> tuple[str, str]:
    if classification == "opportunity":
        trigger = (
            "机制证据获独立来源支持，且完整共同交易日样本继续确认正向方向。"
        )
    elif classification == "risk":
        trigger = (
            "风险机制获独立来源支持，且完整共同交易日样本继续确认负向方向。"
        )
    else:
        trigger = "待冲突来源经新增机制证据和完整市场样本消解后再进入行动阶段。"
    invalidation = (
        "若新增机制证据反转，或完整共同交易日样本不再支持当前方向，则结论失效。"
    )
    return trigger, invalidation


def _public_evidence(relation: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("source_type", "source_id", "relation_type", "rationale"):
        value = relation.get(field)
        if _is_public_scalar(value):
            result[field] = _public_sanitize(value)
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
            "detail": "完整市场样本未确认机制方向。",
        }
        for field in ("source_type", "source_id"):
            if _is_public_scalar(reaction.get(field)):
                item[field] = _public_sanitize(reaction[field])
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
    for topic_key, asset_key in sorted(grouped):
        group = grouped[(topic_key, asset_key)]
        source_pairs = {
            identity
            for index, relation in enumerate(group)
            if (identity := _source_identity(relation, index)) is not None
        }
        matched_reactions = [
            reaction
            for reaction in clean_reactions
            if _matches_reaction(reaction, asset_key, source_pairs)
        ]
        classification, direction = _classification(group)
        market_validation, market_score = _market_validation(
            matched_reactions, direction
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
            "corroboration": _corroboration(len(source_pairs)),
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
            "horizon": _group_horizon(group),
            "data_as_of": _data_as_of(group, matched_reactions, current),
            "evidence": evidence,
            "source_count": len(source_pairs),
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
        }
        decisions.append(card)

    result = {
        "decisions": decisions,
        "impact_matrix": _build_impact_matrix(decisions),
        "evidence_policy": EVIDENCE_POLICY,
    }
    return _public_sanitize(result)


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
    return result
