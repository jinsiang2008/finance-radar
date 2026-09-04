"""Read-only Daily Briefing aggregation over persisted public records.

This module deliberately has no collector or provider imports.  Building a
briefing is a bounded projection of rows that have already been written by the
KOL, macro and decision collectors; an HTTP request must never start a scan or
an LLM call.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
STALE_AFTER_SECONDS = 90 * 60
MAX_HIGHLIGHTS = 5
MAX_FIRSTHAND = 6
MAX_WATCHPOINTS = 5
EVENT_LOOKBACK_HOURS = 24
EVENT_QUERY_LIMIT = 240
MACRO_HISTORY_LIMIT = 72

DISCLAIMER = (
    "仅供信息发现与风险监测，不构成投资建议；请打开原始来源核验，"
    "市场数据及自动摘要可能存在延迟。"
)

_AI_STATUSES = {"pending", "processing", "ready", "retry", "failed", "ineligible"}
_EVIDENCE_BASES = {
    "title",
    "title_only",
    "title_and_snippet",
    "post_text",
    "indicator_data",
    "official_body",
}
_IMPACT_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0, "unknown": 0}
_TIER_RANK = {"official": 3, "first_party": 3, "reporting": 1, "discovery": 0}
_TIER_LABELS = {
    "official": "官方正文",
    "first_party": "本人原文",
    "reporting": "媒体报道",
    "discovery": "聚合线索",
}
_IMPACT_LABELS = {"high": "高影响", "medium": "中影响", "low": "低影响"}
_AGGREGATOR_SOURCE_RE = re.compile(
    r"(?:\bbing\b|\bbaidu\b|\bgoogle\s+news\b|百度(?:新闻|资讯)|必应(?:新闻|资讯))",
    re.IGNORECASE,
)
_AGGREGATOR_HOSTS = {
    "bing.com",
    "www.bing.com",
    "cn.bing.com",
    "baidu.com",
    "www.baidu.com",
    "news.baidu.com",
    "news.google.com",
}
_OFFICIAL_EXCHANGE_HOSTS = {
    "sse.com.cn",
    "szse.cn",
    "hkexnews.hk",
}
_ASSET_KEY_RE = re.compile(
    r"(?:US|CN|HK|INDEX|ETF|BOND|FX|COMMODITY|CRYPTO|THEME):"
    r"[A-Z0-9.^_/-]{1,32}"
)
_TOPIC_LABELS = {
    "ai_semiconductors": "AI 与半导体",
    "monetary_policy": "货币政策",
    "recession_growth": "增长与衰退",
    "inflation": "通胀",
    "geopolitics_trade": "地缘与贸易",
    "crypto": "加密资产",
    "financial_system": "金融系统",
    "china_markets": "中国市场",
    "market_risk": "市场风险",
}
_ASSET_LABELS = {
    "US:NVDA": "英伟达",
    "US:TSM": "台积电",
    "US:TSLA": "特斯拉",
    "US:AAPL": "苹果",
    "US:MSFT": "微软",
    "US:META": "Meta",
    "US:GOOGL": "Alphabet",
    "US:AMZN": "亚马逊",
    "US:AMD": "AMD",
    "US:AVGO": "博通",
    "US:BABA": "阿里巴巴",
    "US:SPY": "标普 500 ETF",
    "US:QQQ": "纳斯达克 100 ETF",
    "CRYPTO:BTC": "比特币",
    "CRYPTO:ETH": "以太坊",
    "COMMODITY:GOLD": "黄金",
    "COMMODITY:OIL": "原油",
    "FX:DXY": "美元指数",
    "FX:USD/CNY": "美元兑人民币",
    "INDEX:VIX": "VIX",
    "BOND:UST_LONG": "长期美债",
    "THEME:AI": "人工智能主题",
    "THEME:SEMICONDUCTOR": "半导体主题",
    "THEME:CHINA_EQUITY": "中国股票",
    "THEME:GLOBAL_RISK_ASSETS": "全球风险资产",
}


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:maximum]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _datetime(value)
    if parsed is None:
        raise ValueError("now must be a timezone-aware datetime or ISO timestamp")
    return parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def edition_for(value: Any = None) -> tuple[str, str]:
    """Return the edition code and label using Beijing wall-clock cutoffs."""
    local = _now(value).astimezone(BEIJING)
    minutes = local.hour * 60 + local.minute
    if minutes < 9 * 60 + 30:
        return "morning", "晨间版"
    if minutes < 15 * 60:
        return "midday", "午间版"
    if minutes < 20 * 60 + 30:
        return "close", "收盘版"
    return "us_premarket", "美股盘前·夜间版"


def _public_url(value: Any) -> str:
    raw = _text(value, 2_048)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return ""
    return raw


def _host(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


def _official_host(host: str) -> bool:
    return bool(
        host.endswith(".gov")
        or host == "gov.cn"
        or host.endswith(".gov.cn")
        or any(
            host == base or host.endswith("." + base)
            for base in _OFFICIAL_EXCHANGE_HOSTS
        )
    )


def _original_social_post(source: Any, url: Any) -> bool:
    source_text = _text(source, 120).lower()
    try:
        parsed = urlsplit(str(url or ""))
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return (
            (source_text.startswith("x @") or source_text.startswith("twitter @"))
            and "/status/" in path
        )
    if host in {"truthsocial.com", "www.truthsocial.com"}:
        return source_text.startswith("truth social @") and "/@" in path
    return False


def classify_source(
    item: Mapping[str, Any],
    *,
    evidence_basis: str | None = None,
    source_url: str | None = None,
) -> str:
    """Classify source directness without inferring independent confirmation."""
    basis = _text(evidence_basis or item.get("evidence_basis"), 40).lower()
    url = _public_url(
        source_url
        or item.get("content_source_url")
        or item.get("source_url")
        or item.get("canonical_url")
        or item.get("url")
    )
    host = _host(url)
    if basis == "official_body" and _official_host(host):
        return "official"

    attribution = _text(
        item.get("attribution_basis") or item.get("post_type"), 40
    ).lower()
    if attribution in {"direct_source", "self_post"} and _original_social_post(
        item.get("source"), url
    ):
        return "first_party"

    source = _text(item.get("source"), 120)
    social_host = host in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "truthsocial.com",
        "www.truthsocial.com",
    }
    if (
        not url
        or social_host
        or _official_host(host)
        or _AGGREGATOR_SOURCE_RE.search(source)
        or host in _AGGREGATOR_HOSTS
    ):
        return "discovery"

    # A non-aggregator article URL is a reporting record.  It is intentionally
    # not promoted to official/first-party without the evidence checks above.
    return "reporting"


def _ai_projection(item: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any] | None]:
    status = _text(item.get("ai_status"), 24).lower() or "pending"
    if status not in _AI_STATUSES:
        status = "pending"
    raw = item.get("ai_enrichment")
    enrichment = raw if isinstance(raw, Mapping) else None
    raw_basis = (
        _text(enrichment.get("evidence_basis"), 40).lower()
        if enrichment is not None
        else ""
    )
    valid = bool(
        status == "ready"
        and enrichment is not None
        and _text(enrichment.get("status"), 24).lower() == "ready"
        and _text(enrichment.get("headline_zh"), 160)
        and _text(enrichment.get("summary_zh"), 400)
        and _text(enrichment.get("why_it_matters_zh"), 320)
    )
    return status, raw_basis, enrichment if valid else None


def _macro_ai_projection(
    item: Mapping[str, Any],
) -> tuple[str, str, Mapping[str, Any] | None]:
    """Apply the same evidence gate used by the public macro event view."""
    status, raw_basis, enrichment = _ai_projection(item)
    if enrichment is None:
        return status, raw_basis, None
    confidence = _finite_number(enrichment.get("confidence"))
    if raw_basis not in {
        "official_body",
        "indicator_data",
        "title_and_snippet",
        "post_text",
    } or confidence is None or confidence < 0.65:
        return status, raw_basis, None
    return status, raw_basis, enrichment


def _fallback_evidence_basis(
    item: Mapping[str, Any],
    *,
    kind: str,
) -> str:
    if (
        kind == "macro_event"
        and _text(item.get("content_status"), 24).lower() == "ready"
        and _public_url(item.get("content_source_url"))
    ):
        return "official_body"
    if kind == "macro_event" and _text(item.get("kind"), 24).lower() == "indicator":
        if item.get("previous_value") is not None or item.get("current_value") is not None:
            return "indicator_data"
    if _text(item.get("attribution_basis"), 40).lower() in {
        "direct_source",
        "self_post",
    }:
        return "post_text"
    if _text(item.get("snippet") or item.get("note"), 2_200):
        return "title_and_snippet"
    return "title_only"


def _impact(value: Any) -> str:
    normalized = _text(value, 24).lower()
    return normalized if normalized in _IMPACT_RANK else "unknown"


def _assets(enrichment: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if enrichment is None or not isinstance(enrichment.get("assets"), list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in enrichment["assets"]:
        if not isinstance(raw, Mapping):
            continue
        asset_key = _text(raw.get("asset_key"), 40).upper()
        if not _ASSET_KEY_RE.fullmatch(asset_key) or asset_key in seen:
            continue
        item = {"asset_key": asset_key}
        name = _text(raw.get("name_zh"), 40)
        direction = _text(raw.get("direction"), 24).lower()
        if name:
            item["name_zh"] = name
        if direction in {"positive", "negative", "mixed", "unclear"}:
            item["direction"] = direction
        output.append(item)
        seen.add(asset_key)
        if len(output) >= 6:
            break
    return output


def _rank_reason(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    impact = _text(item.get("impact"), 24).lower()
    tier = _text(item.get("source_tier"), 24).lower()
    if impact in _IMPACT_LABELS:
        parts.append(_IMPACT_LABELS[impact])
    if tier in _TIER_LABELS:
        parts.append(_TIER_LABELS[tier])
    if item.get("time_status") == "verified":
        parts.append("发布时间已核验")
    if item.get("ai_summary_used") is True:
        parts.append("AI 摘要已就绪")
    return " · ".join(parts[:3])


def _event_highlight(source: Mapping[str, Any]) -> dict[str, Any] | None:
    original_title = _text(source.get("title"), 180)
    if not original_title:
        return None
    ai_status, raw_basis, enrichment = _ai_projection(source)
    evidence_basis = (
        raw_basis
        if raw_basis in _EVIDENCE_BASES
        else _fallback_evidence_basis(source, kind="kol_event")
    )
    trusted_basis = evidence_basis if enrichment is not None else _fallback_evidence_basis(
        source, kind="kol_event"
    )
    source_url = _public_url(
        source.get("source_url") or source.get("canonical_url") or source.get("url")
    )
    # ``query_events(use_ai_impact=True)`` has already applied the public
    # confidence threshold and rule veto.  Re-reading the raw AI impact here
    # would let a low-confidence/title-only model result bypass that gate.
    impact = _impact(source.get("impact"))
    item: dict[str, Any] = {
        "id": _text(source.get("id"), 80),
        "kind": "kol_event",
        "title": _text(
            enrichment.get("headline_zh") if enrichment is not None else original_title,
            180,
        ),
        "summary": _text(
            enrichment.get("summary_zh")
            if enrichment is not None
            else source.get("snippet") or original_title,
            420,
        ),
        "impact": impact,
        "source_tier": classify_source(
            source,
            evidence_basis=trusted_basis,
            source_url=source_url,
        ),
        "source_label": _text(source.get("source"), 120) or "来源待核验",
        "ai_status": ai_status,
        "ai_summary_used": enrichment is not None,
        "evidence_basis": evidence_basis,
    }
    if enrichment is not None:
        why = _text(enrichment.get("why_it_matters_zh"), 320)
        assets = _assets(enrichment)
        if why:
            item["why_it_matters"] = why
        if assets:
            item["assets"] = assets
    if source_url:
        item["source_url"] = source_url
    for field in ("published_at", "fetched_at"):
        value = _text(source.get(field), 64)
        if value:
            item[field] = value
    time_status = _text(source.get("time_status"), 24).lower()
    if time_status:
        item["time_status"] = time_status
    related = source.get("source_count")
    if isinstance(related, int) and not isinstance(related, bool) and related >= 0:
        item["related_records"] = related
    kol_name = _text(source.get("kol_name_cn") or source.get("kol_name"), 80)
    if kol_name:
        item["kol_name"] = kol_name
    item["rank_reason"] = _rank_reason(item)
    return item


def _macro_highlight(source: Mapping[str, Any], macro: Mapping[str, Any]) -> dict[str, Any] | None:
    original_title = _text(source.get("title"), 180)
    if not original_title:
        return None
    ai_status, raw_basis, enrichment = _macro_ai_projection(source)
    fallback_basis = _fallback_evidence_basis(source, kind="macro_event")
    evidence_basis = raw_basis if raw_basis in _EVIDENCE_BASES else fallback_basis
    trusted_basis = evidence_basis if enrichment is not None else fallback_basis
    source_url = _public_url(source.get("content_source_url") or source.get("url"))
    fallback_summary = (
        source.get("content_excerpt")
        or source.get("snippet")
        or source.get("note")
        or original_title
    )
    item: dict[str, Any] = {
        "id": _text(source.get("id"), 80),
        "kind": "macro_event",
        "title": _text(
            enrichment.get("headline_zh") if enrichment is not None else original_title,
            180,
        ),
        "summary": _text(
            enrichment.get("summary_zh") if enrichment is not None else fallback_summary,
            420,
        ),
        # Macro severity is the collector's deterministic public gate.  AI is
        # used to compress evidence, never to promote Daily ranking severity.
        "impact": _impact(source.get("severity")),
        "source_tier": classify_source(
            source,
            evidence_basis=trusted_basis,
            source_url=source_url,
        ),
        "source_label": _text(source.get("source"), 120) or "宏观监控",
        "ai_status": ai_status,
        "ai_summary_used": enrichment is not None,
        "evidence_basis": evidence_basis,
    }
    if enrichment is not None:
        why = _text(enrichment.get("why_it_matters_zh"), 320)
        assets = _assets(enrichment)
        if why:
            item["why_it_matters"] = why
        if assets:
            item["assets"] = assets
    if source_url:
        item["source_url"] = source_url
    published = _text(source.get("published_at"), 64)
    if published:
        item["published_at"] = published
    fetched = _text(macro.get("created_at"), 64)
    if fetched:
        item["fetched_at"] = fetched
    time_status = _text(source.get("time_status"), 24).lower()
    if time_status:
        item["time_status"] = time_status
    item["rank_reason"] = _rank_reason(item)
    return item


def _highlight_time(item: Mapping[str, Any]) -> datetime:
    for field in ("published_at", "fetched_at"):
        parsed = _datetime(item.get(field))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _highlight_rank(item: Mapping[str, Any]) -> tuple[int, int, float, str, str]:
    return (
        -_IMPACT_RANK.get(_text(item.get("impact"), 24).lower(), 0),
        -_TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0),
        -_highlight_time(item).timestamp(),
        _text(item.get("kind"), 24),
        _text(item.get("id"), 80),
    )


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(items, key=_highlight_rank):
        url = _text(item.get("source_url"), 2_048).lower()
        identity = url or _text(item.get("title"), 180).casefold()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        output.append(item)
    return output


def _coverage(items: list[Mapping[str, Any]]) -> dict[str, int]:
    result = {
        "total": len(items),
        "official": 0,
        "first_party": 0,
        "reporting": 0,
        "discovery": 0,
        "ai_ready": 0,
        "time_verified": 0,
    }
    for item in items:
        tier = _text(item.get("source_tier"), 24).lower()
        if tier in _TIER_RANK:
            result[tier] += 1
        if item.get("ai_summary_used") is True:
            result["ai_ready"] += 1
        if item.get("time_status") == "verified":
            result["time_verified"] += 1
    return result


def _watchpoints(decision_record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    summary = (
        decision_record.get("summary") if isinstance(decision_record, Mapping) else None
    )
    cards = summary.get("decisions") if isinstance(summary, Mapping) else None
    if not isinstance(cards, list):
        return []
    ranked: list[tuple[tuple[int, float, str, str], dict[str, Any]]] = []
    stage_rank = {
        "candidate_reduce_or_hedge": 0,
        "candidate_scale_in": 1,
        "verify": 2,
        "observe": 3,
    }
    for raw in cards:
        if not isinstance(raw, Mapping):
            continue
        topic_key = _text(raw.get("topic_key"), 120)
        asset_key = _text(raw.get("asset_key"), 80)
        if not topic_key or not asset_key:
            continue
        status = _text(raw.get("action_stage") or raw.get("decision_status"), 40).lower()
        item: dict[str, Any] = {
            "topic_key": topic_key,
            "asset_key": asset_key,
            "status": status or "observe",
        }
        if topic_key in _TOPIC_LABELS:
            item["topic_label"] = _TOPIC_LABELS[topic_key]
        if asset_key in _ASSET_LABELS:
            item["asset_label"] = _ASSET_LABELS[asset_key]
        direction = _text(raw.get("direction"), 24).lower()
        if direction:
            item["direction"] = direction
        market = raw.get("market_validation")
        if isinstance(market, Mapping):
            next_review = _text(market.get("next_review_at"), 64)
            reason = _text(
                market.get("applicability_reason") or market.get("phase"), 80
            )
            if next_review:
                item["next_review_at"] = next_review
            if reason:
                item["reason"] = reason
        if "reason" not in item:
            reason = _text(raw.get("invalidation") or raw.get("trigger"), 160)
            if reason:
                item["reason"] = reason
        count = raw.get("source_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            item["source_count"] = count
        score = _finite_number(raw.get("total_score")) or 0.0
        ranked.append(
            (
                (
                    stage_rank.get(item["status"], 9),
                    -score,
                    topic_key,
                    asset_key,
                ),
                item,
            )
        )
    ranked.sort(key=lambda pair: pair[0])
    return [item for _, item in ranked[:MAX_WATCHPOINTS]]


def _risk_delta_24h(
    macro: Mapping[str, Any] | None,
    history: list[Mapping[str, Any]],
) -> float | None:
    composite = macro.get("composite_risk") if isinstance(macro, Mapping) else None
    if not isinstance(composite, Mapping):
        return None
    current_score = _finite_number(composite.get("score"))
    if current_score is None:
        return None
    anchor = None
    for field in ("created_at", "timestamp"):
        anchor = _datetime(macro.get(field))
        if anchor is not None:
            break
    if anchor is None:
        return None
    target = anchor - timedelta(hours=24)
    candidates: list[tuple[datetime, float]] = []
    for item in history:
        observed = _datetime(item.get("created_at"))
        score = _finite_number(item.get("composite_score"))
        if observed is not None and score is not None and observed <= target:
            candidates.append((observed, score))
    if not candidates:
        return None
    _, previous = max(candidates, key=lambda pair: pair[0])
    return round(current_score - previous, 2)


def _lead(
    highlights: list[Mapping[str, Any]],
    macro: Mapping[str, Any] | None,
    history: list[Mapping[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if highlights:
        first = highlights[0]
        for source, target in (
            ("title", "headline"),
            ("summary", "summary"),
            ("why_it_matters", "why_it_matters"),
        ):
            value = _text(first.get(source), 420)
            if value:
                output[target] = value
    composite = macro.get("composite_risk") if isinstance(macro, Mapping) else None
    if isinstance(composite, Mapping):
        score = _finite_number(composite.get("score"))
        level = _text(composite.get("level"), 24).lower()
        if score is not None:
            output["risk_score"] = round(score, 2)
        if level in {"critical", "high", "medium", "low"}:
            output["risk_level"] = level
        delta = _risk_delta_24h(macro, history)
        if delta is not None:
            output["risk_delta_24h"] = delta
    return output


def _source_as_of(
    items: list[Mapping[str, Any]],
    macro: Mapping[str, Any] | None,
    history: list[Mapping[str, Any]],
    decision_record: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> datetime | None:
    values: list[datetime] = []

    def append_if_observed(value: Any) -> None:
        parsed = _datetime(value)
        if parsed is not None and parsed <= now:
            values.append(parsed)

    for item in items:
        for field in ("published_at", "fetched_at"):
            append_if_observed(item.get(field))
    if isinstance(macro, Mapping):
        for field in ("timestamp", "created_at"):
            append_if_observed(macro.get(field))
    for item in history:
        append_if_observed(item.get("created_at"))
    if isinstance(decision_record, Mapping):
        summary = decision_record.get("summary")
        cards = summary.get("decisions") if isinstance(summary, Mapping) else None
        if isinstance(cards, list):
            for card in cards:
                if isinstance(card, Mapping):
                    append_if_observed(card.get("data_as_of"))
    return max(values) if values else None


def _is_current_verified_macro_event(
    item: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    if _text(item.get("time_status"), 24).lower() != "verified":
        return False
    published = _datetime(item.get("published_at"))
    if published is None:
        return False
    return now - timedelta(hours=EVENT_LOOKBACK_HOURS) <= published <= now


def build_latest_briefing(
    *,
    repository: Any,
    public_macro: Mapping[str, Any] | None,
    decision_record: Mapping[str, Any] | None,
    now: Any = None,
) -> dict[str, Any]:
    """Build one stable Daily payload from already-persisted public data."""
    current = _now(now)
    edition, edition_label = edition_for(current)
    raw_events = repository.query_events(
        hours=EVENT_LOOKBACK_HOURS,
        time_status="verified",
        limit=EVENT_QUERY_LIMIT,
        offset=0,
        now=current,
        use_ai_impact=True,
    )
    history = repository.macro_history(limit=MACRO_HISTORY_LIMIT)
    safe_history = [item for item in history if isinstance(item, Mapping)]

    candidates = [
        item
        for source in raw_events
        if isinstance(source, Mapping)
        if (item := _event_highlight(source)) is not None
    ]
    macro = public_macro if isinstance(public_macro, Mapping) else None
    monitored = macro.get("monitored_events") if macro is not None else None
    if isinstance(monitored, list):
        candidates.extend(
            item
            for source in monitored
            if isinstance(source, Mapping)
            if _is_current_verified_macro_event(source, now=current)
            if (item := _macro_highlight(source, macro)) is not None
        )
    ranked = _deduplicate(candidates)
    highlights = ranked[:MAX_HIGHLIGHTS]
    firsthand = [
        item
        for item in ranked
        if item.get("source_tier") in {"official", "first_party"}
    ][:MAX_FIRSTHAND]
    watchpoints = _watchpoints(decision_record)
    as_of = _source_as_of(
        ranked,
        macro,
        safe_history,
        decision_record,
        now=current,
    )
    available = bool(ranked or macro or watchpoints)
    age_seconds = (
        max(0.0, (current - as_of).total_seconds()) if as_of is not None else None
    )
    return {
        "available": available,
        "date": current.astimezone(BEIJING).date().isoformat(),
        "edition": edition,
        "edition_label": edition_label,
        "generated_at": _iso(current),
        "source_as_of": _iso(as_of) if as_of is not None else None,
        "stale": age_seconds is None or age_seconds > STALE_AFTER_SECONDS,
        "coverage": _coverage(ranked),
        "lead": _lead(highlights, macro, safe_history),
        "highlights": highlights,
        "firsthand": firsthand,
        "watchpoints": watchpoints,
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "MAX_FIRSTHAND",
    "MAX_HIGHLIGHTS",
    "MAX_WATCHPOINTS",
    "build_latest_briefing",
    "classify_source",
    "edition_for",
]
