"""Pure deterministic extraction of explainable topic/asset relations.

The rules only describe textual association and stated direction. They do not
claim that a KOL statement or macro scenario causes an asset move.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


EXTRACTOR_NAME = "kol-relation-rules"
EXTRACTOR_VERSION = "1.2.0"
METHOD = f"deterministic_rules:{EXTRACTOR_VERSION}"

_CANONICAL_PREFIXES = {
    "US",
    "CN",
    "CRYPTO",
    "COMMODITY",
    "FX",
    "INDEX",
    "BOND",
    "THEME",
}

_SPECIAL_SYMBOLS = {
    "BTC": "CRYPTO:BTC",
    "BTCUSD": "CRYPTO:BTC",
    "BTC-USD": "CRYPTO:BTC",
    "BITCOIN": "CRYPTO:BTC",
    "ETH": "CRYPTO:ETH",
    "ETHUSD": "CRYPTO:ETH",
    "ETH-USD": "CRYPTO:ETH",
    "ETHEREUM": "CRYPTO:ETH",
    "DOGE": "CRYPTO:DOGE",
    "DOGEUSD": "CRYPTO:DOGE",
    "DOGE-USD": "CRYPTO:DOGE",
    "SOL": "CRYPTO:SOL",
    "SOLUSD": "CRYPTO:SOL",
    "SOL-USD": "CRYPTO:SOL",
    "ADA": "CRYPTO:ADA",
    "XRP": "CRYPTO:XRP",
    "BNB": "CRYPTO:BNB",
    "GOLD": "COMMODITY:GOLD",
    "GLD": "COMMODITY:GOLD",
    "IAU": "COMMODITY:GOLD",
    "WTI": "COMMODITY:OIL",
    "OIL": "COMMODITY:OIL",
    "USO": "COMMODITY:OIL",
    "DXY": "FX:DXY",
    "UUP": "US:UUP",
    "CNY": "FX:USD/CNY",
    "USDCNY": "FX:USD/CNY",
    "USD/CNY": "FX:USD/CNY",
    "VIX": "INDEX:VIX",
    "TLT": "BOND:UST_LONG",
    "IEF": "BOND:UST_INTERMEDIATE",
}

# Ordered so extraction is stable across runs and Python versions.
_ASSET_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("US:NVDA", ("nvidia", "英伟达", "辉达")),
    ("US:TSM", ("tsmc", "台积电")),
    ("US:TSLA", ("tesla", "特斯拉")),
    ("US:AAPL", ("apple inc", "苹果公司")),
    ("US:MSFT", ("microsoft", "微软")),
    ("US:META", ("meta platforms", "facebook", "脸书", "元宇宙公司meta")),
    ("US:GOOGL", ("alphabet", "google", "谷歌")),
    ("US:AMZN", ("amazon", "亚马逊")),
    ("US:AMD", ("advanced micro devices", "超威半导体")),
    ("US:AVGO", ("broadcom", "博通")),
    ("US:BABA", ("alibaba", "阿里巴巴")),
    ("CRYPTO:BTC", ("bitcoin", "比特币")),
    ("CRYPTO:ETH", ("ethereum", "以太坊")),
    ("COMMODITY:GOLD", ("gold", "黄金", "金价")),
    ("COMMODITY:OIL", ("crude oil", "wti", "原油", "石油")),
    ("FX:DXY", ("dollar index", "美元指数", "dxy")),
    (
        "FX:USD/CNY",
        ("usd/cny", "美元兑人民币", "人民币汇率", "出口型企业", "美元计价资产"),
    ),
    ("US:UUP", ("dollar assets", "美元资产", "美元计价资产", "出口型企业")),
    ("INDEX:VIX", ("vix", "恐慌指数")),
    ("US:SPY", ("s&p 500", "s&p500", "标普500", "美股大盘")),
    ("US:QQQ", ("nasdaq 100", "nasdaq-100", "纳斯达克100")),
    ("BOND:UST_LONG", ("us treasuries", "us treasury", "美国国债", "长期美债")),
    ("FX:JPY", ("japanese yen", "yen", "日元")),
    ("INDEX:NIKKEI", ("nikkei", "日经")),
    ("THEME:EMERGING_MARKETS", ("emerging markets", "新兴市场")),
    (
        "THEME:GLOBAL_RISK_ASSETS",
        ("global risk assets", "全球风险资产", "所有风险资产", "所有资产", "风险资产"),
    ),
    ("THEME:AI", ("artificial intelligence", "人工智能", "ai")),
    ("US:XLF", ("financial stocks", "金融股")),
    ("US:XLI", ("cyclical stocks", "周期股")),
    (
        "THEME:SEMICONDUCTOR",
        ("semiconductor", "semiconductors", "半导体", "芯片"),
    ),
    ("THEME:FINANCIALS", ("financial stocks", "金融股", "银行股", "区域银行")),
    ("THEME:CHINA_EQUITY", ("a股", "中国股票", "中概股")),
    ("THEME:CRYPTO", ("crypto market", "加密货币市场", "加密资产")),
)

_TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ai_semiconductors",
        ("ai", "人工智能", "半导体", "芯片", "gpu", "算力", "data center"),
    ),
    (
        "monetary_policy",
        ("fed", "federal reserve", "美联储", "央行", "降息", "加息", "利率"),
    ),
    (
        "recession_growth",
        ("recession", "hard landing", "衰退", "硬着陆", "经济增长", "通缩"),
    ),
    (
        "inflation",
        ("inflation", "cpi", "通胀", "物价"),
    ),
    (
        "geopolitics_trade",
        ("war", "conflict", "tariff", "sanction", "战争", "冲突", "关税", "制裁"),
    ),
    (
        "crypto",
        ("bitcoin", "ethereum", "crypto", "比特币", "以太坊", "加密货币"),
    ),
    (
        "financial_system",
        ("credit", "liquidity", "bank", "debt", "信用", "流动性", "银行", "债务"),
    ),
    (
        "china_markets",
        ("china", "chinese", "中国", "人民币", "a股", "中概股"),
    ),
    (
        "market_risk",
        ("market", "equity", "stock", "vix", "市场", "股票", "美股", "风险资产"),
    ),
)

_POSITIVE_WORDS = (
    "bullish",
    "buy",
    "upside",
    "outperform",
    "benefit",
    "opportunity",
    "positive",
    "看好",
    "买入",
    "增持",
    "利好",
    "受益",
    "上涨",
    "机会",
    "逢低配置",
)
_NEGATIVE_WORDS = (
    "bearish",
    "sell",
    "downside",
    "underperform",
    "avoid",
    "warning",
    "risk",
    "crash",
    "collapse",
    "negative",
    "看空",
    "卖出",
    "减持",
    "利空",
    "下跌",
    "警告",
    "风险",
    "危机",
    "崩盘",
    "破裂",
)

_DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9.-]{0,9})\b")
_CN_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_FIELD_TICKER_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2,6})(?![A-Za-z])")
_FIELD_TICKER_STOPWORDS = {
    "AI",
    "ETF",
    "REIT",
    "REITS",
    "GDP",
    "CPI",
    "CFTC",
    "SEC",
    "USD",
    "CEO",
}
_PROXY_ASSET_KEYS = {"US:XLF", "US:XLI", "FX:USD/CNY", "US:UUP"}
_CLAUSE_SPLIT_RE = re.compile(
    r"[,，;；。.!?\n]+|\b(?:but|while|whereas)\b|但是|不过|然而|而",
    re.IGNORECASE,
)


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def normalize_asset_key(value: Any) -> str | None:
    """Normalize supported symbols and proxies to a stable namespaced key."""
    if value is None:
        return None
    raw = str(value).strip().strip("()[]{}，,;；")
    if not raw:
        return None
    upper = raw.upper().replace(" ", "")
    if ":" in upper:
        prefix, body = upper.split(":", 1)
        if prefix in _CANONICAL_PREFIXES and body:
            return f"{prefix}:{body}"

    upper = upper.lstrip("$")
    special = _SPECIAL_SYMBOLS.get(upper)
    if special:
        return special

    cn_match = re.fullmatch(
        r"(?:(?:SH|SZ|BJ)[.:]?)?(\d{6})(?:\.(?:SH|SZ|BJ))?",
        upper,
    )
    if cn_match:
        return f"CN:{cn_match.group(1)}"

    if re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z])?", upper):
        return f"US:{upper}"
    return None


def _contains_alias(text: str, alias: str) -> bool:
    if re.search(r"[A-Za-z0-9]", alias):
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                text,
                re.IGNORECASE,
            )
        )
    return alias in text


def _explicit_values(values: Iterable[Any] | str | None) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, str):
        return [part for part in re.split(r"[\s,，;；]+", values) if part]
    return list(values)


def extract_assets(
    text: str,
    explicit_tickers: Iterable[Any] | str | None = None,
    limit: int = 32,
) -> list[str]:
    """Extract assets in a deterministic precedence order without network/LLM use."""
    found: list[str] = []
    for value in _explicit_values(explicit_tickers):
        _append_unique(found, normalize_asset_key(value))

    source = text or ""
    for match in _DOLLAR_TICKER_RE.finditer(source):
        _append_unique(found, normalize_asset_key(match.group(1)))
    for match in _CN_CODE_RE.finditer(source):
        _append_unique(found, normalize_asset_key(match.group(1)))
    for asset_key, aliases in _ASSET_ALIASES:
        if any(_contains_alias(source, alias) for alias in aliases):
            _append_unique(found, asset_key)
        if len(found) >= limit:
            break
    return found[: max(0, limit)]


def classify_topic(text: str) -> str:
    source = text or ""
    for topic_key, words in _TOPIC_RULES:
        if any(_contains_alias(source, word) for word in words):
            return topic_key
    return "general"


def classify_stance(text: str) -> dict[str, Any]:
    source = text or ""
    positive = [
        word
        for word in _POSITIVE_WORDS
        if _contains_alias(source, word) and not _word_is_negated(source, word)
    ]
    negative = [
        word
        for word in _NEGATIVE_WORDS
        if _contains_alias(source, word) and not _word_is_negated(source, word)
    ]
    if len(positive) > len(negative):
        stance, direction = "bullish", "positive"
    elif len(negative) > len(positive):
        stance, direction = "bearish", "negative"
    else:
        stance, direction = "unknown", "neutral"
    return {
        "stance": stance,
        "direction": direction,
        "positive_hits": positive,
        "negative_hits": negative,
    }


def _word_is_negated(text: str, word: str) -> bool:
    if re.search(r"[A-Za-z]", word):
        return bool(
            re.search(
                rf"\b(?:not|never|no)\s+(?:\w+\s+){{0,2}}{re.escape(word)}\b",
                text,
                re.IGNORECASE,
            )
        )
    return any(
        negated in text
        for negated in (
            f"不{word}",
            f"并不{word}",
            f"不再{word}",
            f"不要{word}",
            f"别{word}",
        )
    )


def _clause_mentions_asset(clause: str, asset_key: str) -> bool:
    prefix, _, body = asset_key.partition(":")
    if prefix in {"US", "CRYPTO", "CN"} and body:
        if re.search(
            rf"(?<![A-Za-z0-9])\$?{re.escape(body)}(?![A-Za-z0-9])",
            clause,
            re.IGNORECASE,
        ):
            return True
    for key, aliases in _ASSET_ALIASES:
        if key == asset_key and any(_contains_alias(clause, alias) for alias in aliases):
            return True
    return False


def classify_asset_stance(
    text: str,
    asset_key: str,
    *,
    asset_count: int = 1,
) -> dict[str, Any]:
    """Classify direction only from clauses that explicitly mention the asset."""
    clauses = [
        clause.strip()
        for clause in _CLAUSE_SPLIT_RE.split(text or "")
        if clause.strip()
    ]
    local = [clause for clause in clauses if _clause_mentions_asset(clause, asset_key)]
    if local:
        return classify_stance(" ".join(local))
    if asset_count == 1:
        return classify_stance(text)
    return classify_stance("")


def normalize_horizon(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw or any(word in raw for word in ("不确定", "unknown", "随时")):
        return "unknown"
    month_range = re.search(r"(\d+)\s*[-–—至到]\s*(\d+)\s*个?月", raw)
    if month_range:
        maximum = int(month_range.group(2))
        return "short" if maximum <= 3 else "medium" if maximum <= 6 else "long"
    if any(word in raw for word in ("中长期", "长期", "year", "年度")):
        return "long"
    if any(word in raw for word in ("中期", "quarter", "季度")):
        return "medium"
    if any(word in raw for word in ("短期", "day", "week", "天", "周")):
        return "short"
    return "unknown"


def score_confidence(
    *,
    has_asset: bool,
    stance: str,
    topic_key: str,
    source_kind: str,
) -> float:
    """Deterministic confidence formula; unknown stance is explicitly capped."""
    score = 0.30
    score += 0.25 if has_asset else 0.0
    score += 0.20 if stance != "unknown" else 0.0
    score += 0.10 if topic_key != "general" else 0.0
    score += 0.05 if source_kind == "event" else 0.0
    if stance == "unknown":
        score = min(score, 0.45)
    return round(min(score, 0.95), 2)


def score_strength(impact: Any, direction: str, source_kind: str = "event") -> float:
    base = 0.30 if source_kind == "event" else 0.45
    impact_score = {
        "low": 0.05,
        "medium": 0.15,
        "high": 0.30,
        "severe": 0.40,
        "catastrophic": 0.50,
    }.get(str(impact or "").lower(), 0.0)
    direction_score = 0.15 if direction != "neutral" else 0.0
    return round(min(base + impact_score + direction_score, 1.0), 2)


def _event_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(field) or "") for field in ("title", "snippet", "text")
    ).strip()


def event_relations(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one KOL event to non-causal, explainable asset edges."""
    text = _event_text(event)
    assets = extract_assets(text, event.get("tickers"))
    if not assets:
        return []
    topic_key = classify_topic(text)
    horizon = normalize_horizon(event.get("horizon") or text)
    source_id = str(
        event.get("id")
        or event.get("dedup_key")
        or hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    )
    relations: list[dict[str, Any]] = []
    for asset_key in assets:
        stance = classify_asset_stance(
            text, asset_key, asset_count=len(assets)
        )
        confidence = score_confidence(
            has_asset=True,
            stance=stance["stance"],
            topic_key=topic_key,
            source_kind="event",
        )
        strength = score_strength(event.get("impact"), stance["direction"])
        rationale = (
            f"规则命中主题 {topic_key}；"
            + (
                f"资产附近子句立场识别为 {stance['stance']}。"
                if stance["stance"] != "unknown"
                else "资产附近未出现足够明确的多空措辞，方向降级为 neutral。"
            )
            + " 此边仅表示文本关联，不表示已证实的因果关系。"
        )
        relations.append({
            "source_type": "event",
            "source_id": source_id,
            "topic_key": topic_key,
            "asset_key": asset_key,
            "relation_type": (
                "view" if stance["stance"] != "unknown" else "mention"
            ),
            "stance": stance["stance"],
            "direction": stance["direction"],
            "strength": strength,
            "confidence": confidence,
            "horizon": horizon,
            "method": METHOD,
            "rationale": rationale,
            "evidence": {
                "extractor": EXTRACTOR_NAME,
                "extractor_version": EXTRACTOR_VERSION,
                "title": str(event.get("title") or "")[:500],
                "snippet": str(event.get("snippet") or "")[:500],
                "url": (
                    event.get("source_url")
                    or event.get("canonical_url")
                    or event.get("url")
                ),
                "published_at": event.get("published_at"),
                "positive_hits": stance["positive_hits"],
                "negative_hits": stance["negative_hits"],
                "matched_asset": asset_key,
            },
        })
    return relations


def _asset_field_assets(values: Any) -> list[str]:
    if values is None:
        return []
    raw_values = values if isinstance(values, list) else [values]
    found: list[str] = []
    for raw in raw_values:
        text = str(raw or "")
        for key in extract_assets(text):
            _append_unique(found, key)
        for symbol in _FIELD_TICKER_RE.findall(text):
            if symbol not in _FIELD_TICKER_STOPWORDS:
                _append_unique(found, normalize_asset_key(symbol))
    return found


def _macro_confidence(label: Any, category: str) -> float:
    normalized = str(label or "").lower()
    if category == "opportunity":
        return {
            "low": 0.45,
            "medium": 0.62,
            "high": 0.78,
        }.get(normalized, 0.40)
    return {
        "low": 0.42,
        "low_to_medium": 0.50,
        "medium": 0.60,
        "medium_to_high": 0.70,
        "high": 0.78,
        "gradual": 0.52,
        "approaching": 0.64,
        "immediate": 0.76,
        "highly_visible": 0.68,
        "moderate": 0.56,
    }.get(normalized, 0.40)


def _macro_source_id(payload: dict[str, Any]) -> str:
    explicit = (
        payload.get("snapshot_id")
        or payload.get("id")
        or payload.get("created_at")
        or payload.get("timestamp")
    )
    if explicit is not None:
        return str(explicit)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:16]


def _macro_item_key(category: str, item: dict[str, Any]) -> str:
    explicit = item.get("id")
    if explicit is not None:
        return str(explicit)
    encoded = json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{category}-{hashlib.sha1(encoded).hexdigest()[:16]}"


def _macro_edges(
    *,
    item: dict[str, Any],
    category: str,
    relation_type: str,
    direction: str,
    asset_fields: tuple[str, ...],
    source_id: str,
    confidence_label: Any,
    impact: Any,
) -> list[dict[str, Any]]:
    text = " ".join(
        str(item.get(field) or "")
        for field in (
            "name",
            "description",
            "signal",
            "trigger",
            "catalyst",
            "market_impact",
        )
    )
    assets: list[str] = []
    matched_fields: dict[str, Any] = {}
    for field in asset_fields:
        if item.get(field):
            matched_fields[field] = item[field]
            for asset in _asset_field_assets(item[field]):
                _append_unique(assets, asset)
    if not assets:
        assets = extract_assets(text)
        matched_fields["fallback_text"] = text[:600]
    if not assets:
        return []

    topic_key = classify_topic(text + " " + json.dumps(matched_fields, ensure_ascii=False))
    confidence = _macro_confidence(confidence_label, category)
    strength = score_strength(impact, direction, source_kind="macro")
    horizon = normalize_horizon(item.get("timeframe") or item.get("horizon"))
    item_id = _macro_item_key(category, item)
    return [
        {
            "source_type": "macro_snapshot",
            "source_id": source_id,
            "topic_key": topic_key,
            "asset_key": asset,
            "relation_type": relation_type,
            "stance": "scenario",
            "direction": direction,
            "strength": strength,
            "confidence": round(confidence, 2),
            "horizon": horizon,
            "method": METHOD,
            "rationale": _macro_rationale(category, direction, asset),
            "evidence": {
                "extractor": EXTRACTOR_NAME,
                "extractor_version": EXTRACTOR_VERSION,
                "category": category,
                "item_id": str(item_id),
                "name": item.get("name"),
                "matched_fields": matched_fields,
                "matched_asset": asset,
            },
        }
        for asset in assets
    ]


def _macro_rationale(category: str, direction: str, asset_key: str) -> str:
    rationale = (
        f"规则按 {category} 输入中明确列出的资产/代理建立 {direction} 关联。"
        " 此边是场景映射，不表示该场景会导致资产价格变化。"
    )
    if asset_key in _PROXY_ASSET_KEYS:
        rationale += " 该资产键是可交易或宏观代理，不代表个性化敞口。"
    return rationale


def _dedupe_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (
            edge["source_id"],
            edge["topic_key"],
            edge["asset_key"],
            edge["relation_type"],
        )
        current = keyed.get(key)
        if current is None or (
            edge["confidence"],
            edge["strength"],
            str(edge["evidence"].get("item_id")),
        ) > (
            current["confidence"],
            current["strength"],
            str(current["evidence"].get("item_id")),
        ):
            keyed[key] = edge
    return list(keyed.values())


def macro_relations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert risk-radar scenario lists to deterministic explainable edges."""
    snapshot_id = _macro_source_id(payload)
    edges: list[dict[str, Any]] = []

    for item in payload.get("black_swan_scenarios") or []:
        source_id = f"{snapshot_id}:black_swan:{_macro_item_key('black_swan', item)}"
        edges.extend(
            _macro_edges(
                item=item,
                category="black_swan",
                relation_type="risk_scenario",
                direction="negative",
                asset_fields=("affected_assets", "affected_markets"),
                source_id=source_id,
                confidence_label=item.get("probability"),
                impact=item.get("impact") or "high",
            )
        )
    for item in payload.get("gray_rhinos") or []:
        source_id = f"{snapshot_id}:gray_rhino:{_macro_item_key('gray_rhino', item)}"
        edges.extend(
            _macro_edges(
                item=item,
                category="gray_rhino",
                relation_type="structural_risk",
                direction="negative",
                asset_fields=("affected_markets", "affected_assets"),
                source_id=source_id,
                confidence_label=item.get("urgency") or item.get("visibility"),
                impact="high" if item.get("urgency") == "immediate" else "medium",
            )
        )
    for item in payload.get("opportunities") or []:
        source_id = f"{snapshot_id}:opportunity:{_macro_item_key('opportunity', item)}"
        edges.extend(
            _macro_edges(
                item=item,
                category="opportunity",
                relation_type="opportunity",
                direction="positive",
                asset_fields=("asset", "assets"),
                source_id=source_id,
                confidence_label=item.get("confidence"),
                impact="medium",
            )
        )
    return _dedupe_edges(edges)
