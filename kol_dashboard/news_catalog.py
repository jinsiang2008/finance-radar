"""Shared news taxonomy and watchlist; deterministic associations, not advice.

Direct mentions require text evidence. Topic associations are explicitly marked
as discovery aids and never assert holdings, suppliers, beneficiaries or trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re


TOPICS = (
    ("ai_chips", "AI 与芯片", r"\b(?:AI|artificial intelligence|semiconductors?|chips?|GPU|LLM|OpenAI|Anthropic)\b|人工智能|芯片|半导体|大模型|算力"),
    ("technology", "科技", r"\b(?:technology|tech|software|cloud|internet|smartphones?|data cent(?:er|re)s?)\b|科技|技术|云计算|软件|互联网|数据中心"),
    ("space", "航天 / SpaceX", r"\b(?:SpaceX|Starlink|Starship|Falcon 9|Falcon Heavy|Blue Origin|Rocket Lab|NASA|rockets?|satellites?|spaceflight|aerospace)\b|航天|航空航天|火箭|卫星|星链|星舰|太空"),
    ("energy", "能源", r"\b(?:energy|oil|petroleum|brent|WTI|natural gas|LNG|OPEC|renewables?|solar|nuclear power)\b|能源|石油|原油|天然气|核电|光伏|电力"),
    ("macro", "宏观与政策", r"\b(?:inflation|interest rates?|Federal Reserve|Fed|central banks?|tariffs?|treasur(?:y|ies)|GDP|CPI|PPI|unemployment|monetary|fiscal)\b|宏观|通胀|利率|美联储|央行|关税|国债|货币政策|财政|就业数据"),
    ("commodities", "大宗商品", r"\b(?:commodit(?:y|ies)|copper|gold|silver|alumin(?:um|ium)|oil|petroleum|brent|WTI|natural gas|LNG|iron ore|lithium)\b|大宗|铜|黄金|金价|白银|银价|铝|原油|天然气|铁矿石|锂"),
    ("stocks_etfs", "股票与 ETF", r"\b(?:stocks?|equities|equity|shares?|ETFs?|IPO|earnings|dividends?|buybacks?|Nasdaq|S&P 500)\b|股票|股市|股份|上市|财报|业绩|分红|回购|指数基金|交易所交易基金"),
)
TOPIC_BY_KEY = {key: {"key": key, "label": label} for key, label, _pattern in TOPICS}
_TOPIC_PATTERNS = {key: re.compile(pattern, re.I) for key, _label, pattern in TOPICS}


@dataclass(frozen=True)
class Instrument:
    key: str
    label: str
    ticker: str
    market: str
    type: str
    topic_keys: tuple[str, ...]
    aliases: tuple[str, ...]
    related_pattern: str | None = None


INSTRUMENTS = (
    Instrument("US:NVDA", "英伟达", "NVDA", "US", "stock", ("ai_chips", "technology"), ("Nvidia", "英伟达", "辉达")),
    Instrument("US:AMD", "AMD", "AMD", "US", "stock", ("ai_chips", "technology"), ("Advanced Micro Devices", "超威半导体")),
    Instrument("US:TSM", "台积电 ADR", "TSM", "US", "stock", ("ai_chips", "technology"), ("TSMC", "Taiwan Semiconductor", "台积电")),
    Instrument("US:ASML", "阿斯麦", "ASML", "US", "stock", ("ai_chips", "technology"), ("ASML", "阿斯麦")),
    Instrument("US:AVGO", "博通", "AVGO", "US", "stock", ("ai_chips", "technology"), ("Broadcom", "博通")),
    Instrument("US:INTC", "英特尔", "INTC", "US", "stock", ("ai_chips", "technology"), ("Intel", "英特尔")),
    Instrument("US:MSFT", "微软", "MSFT", "US", "stock", ("technology",), ("Microsoft", "微软")),
    Instrument("US:GOOGL", "Alphabet A", "GOOGL", "US", "stock", ("technology",), ("Alphabet", "Google", "谷歌")),
    Instrument("US:AMZN", "亚马逊", "AMZN", "US", "stock", ("technology",), ("Amazon.com", "Amazon", "亚马逊")),
    Instrument("US:META", "Meta", "META", "US", "stock", ("technology",), ("Meta Platforms", "Facebook", "脸书")),
    Instrument("US:AAPL", "苹果", "AAPL", "US", "stock", ("technology",), ("Apple", "苹果公司")),
    Instrument("US:TSLA", "特斯拉", "TSLA", "US", "stock", ("technology", "energy"), ("Tesla", "特斯拉")),
    Instrument("US:SPCX", "SpaceX", "SPCX", "US", "stock", ("space",), ("SpaceX", "Space Exploration Technologies", "Starlink", "Starship", "Falcon 9", "Falcon Heavy", "星链", "星舰")),
    Instrument("US:RKLB", "Rocket Lab", "RKLB", "US", "stock", ("space",), ("Rocket Lab",)),
    Instrument("US:XOM", "埃克森美孚", "XOM", "US", "stock", ("energy",), ("Exxon", "ExxonMobil", "埃克森美孚")),
    Instrument("US:CVX", "雪佛龙", "CVX", "US", "stock", ("energy",), ("Chevron", "雪佛龙")),
    Instrument("US:FCX", "自由港麦克莫兰", "FCX", "US", "stock", ("commodities",), ("Freeport-McMoRan", "Freeport McMoRan", "自由港麦克莫兰"), r"\bcopper\b|铜"),
    Instrument("CN:601899", "紫金矿业", "601899", "CN", "stock", ("commodities",), ("Zijin Mining", "紫金矿业"), r"\bcopper\b|\bgold\b|铜|黄金|金价"),
    Instrument("HK:0700", "腾讯控股", "0700", "HK", "stock", ("technology",), ("Tencent", "腾讯")),
    Instrument("US:SPY", "SPDR 标普 500 ETF", "SPY", "US", "etf", ("stocks_etfs", "macro"), ("SPDR S&P 500 ETF", "SPDR S&P 500 ETF Trust")),
    Instrument("US:QQQ", "Invesco QQQ", "QQQ", "US", "etf", ("technology",), ("Invesco QQQ",)),
    Instrument("US:SMH", "VanEck 半导体 ETF", "SMH", "US", "etf", ("ai_chips",), ("VanEck Semiconductor ETF",)),
    Instrument("US:SOXX", "iShares 半导体 ETF", "SOXX", "US", "etf", ("ai_chips",), ("iShares Semiconductor ETF",)),
    Instrument("US:XLE", "能源精选板块 ETF", "XLE", "US", "etf", ("energy",), ("Energy Select Sector SPDR",)),
    Instrument("US:GLD", "SPDR 黄金 ETF", "GLD", "US", "etf", ("commodities",), ("SPDR Gold Shares",), r"\bgold\b|黄金|金价"),
    Instrument("US:IAU", "iShares 黄金信托", "IAU", "US", "etf", ("commodities",), ("iShares Gold Trust",), r"\bgold\b|黄金|金价"),
    Instrument("US:SLV", "iShares 白银信托", "SLV", "US", "etf", ("commodities",), ("iShares Silver Trust",), r"\bsilver\b|白银|银价"),
    Instrument("US:COPX", "Global X 铜矿 ETF", "COPX", "US", "etf", ("commodities",), ("Global X Copper Miners ETF",), r"\bcopper\b|铜"),
)
INSTRUMENT_BY_KEY = {item.key: item for item in INSTRUMENTS}

_SPACEX_VERIFICATION = {
    "verified_at": "2026-10-13",
    "verification_sources": [
        "https://www.sec.gov/Archives/edgar/data/1181412/000162828026045763/spcx-closing8xkjune2026.htm",
        "https://ir.nasdaq.com/node/110646/pdf",
    ],
}


def normalize_instrument(value) -> str | None:
    key = str(value or "").strip().upper() or None
    if key and key not in INSTRUMENT_BY_KEY:
        raise ValueError("unsupported instrument")
    return key


def _alias_pattern(alias: str):
    if re.search(r"[\u3400-\u9fff]", alias):
        return re.compile(re.escape(alias), re.I)
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.I)


_ALIASES = {item.key: [(alias, _alias_pattern(alias)) for alias in item.aliases] for item in INSTRUMENTS}
_TICKERS = {item.key: re.compile(r"(?<![A-Za-z0-9])(?:\$|" + re.escape(item.market) + r":)?" + re.escape(item.ticker) + r"(?![A-Za-z0-9])") for item in INSTRUMENTS}
_RELATED_PATTERNS = {item.key: re.compile(item.related_pattern, re.I) for item in INSTRUMENTS if item.related_pattern}
_META_COMPANY_CONTEXT = re.compile(
    r"\bMeta(?:['’]s)?\s+(?i:(?:(?:is|was|will|has|had|may|could)\s+)?"
    r"(?:reports?|announces?|considering|plans?|launch(?:es)?|cuts?|hires?|faces?|"
    r"invests?|acquires?|earnings|revenue|profits?|shares?|stock|layoffs|employees|CEO|CFO))\b"
)


@lru_cache(maxsize=8192)
def _classification(title: str, snippet: str):
    text = title + "\n" + snippet
    topics = {key for key, pattern in _TOPIC_PATTERNS.items() if pattern.search(text)}
    direct = []
    for instrument in INSTRUMENTS:
        matches = [alias for alias, pattern in _ALIASES[instrument.key] if pattern.search(text)]
        # The company commonly appears as TitleCase "Meta". Require nearby
        # business language instead of treating generic meta-analysis as a ticker.
        if instrument.key == "US:META" and _META_COMPANY_CONTEXT.search(text):
            matches.append("Meta")
        if _TICKERS[instrument.key].search(text):
            matches.append(instrument.ticker)
        if matches:
            direct.append((instrument.key, tuple(dict.fromkeys(matches))))
            topics.update(instrument.topic_keys)
            topics.add("stocks_etfs")
    direct_keys = {key for key, _matches in direct}
    related = []
    for instrument in INSTRUMENTS:
        if instrument.key in direct_keys:
            continue
        if instrument.key in _RELATED_PATTERNS:
            matches = bool(_RELATED_PATTERNS[instrument.key].search(text))
        else:
            matches = bool(topics.intersection(instrument.topic_keys))
        if matches:
            related.append(instrument.key)
    return tuple(key for key, _label, _pattern in TOPICS if key in topics), tuple(direct), tuple(related)


def classify(title: str, snippet: str = "") -> dict:
    """Return fresh JSON-safe objects; cached tuples cannot be mutated by callers."""
    topics, direct, related = _classification(str(title or "")[:500], str(snippet or "")[:1600])
    direct_terms = dict(direct)
    assets = []
    for key in [*(key for key, _terms in direct), *related]:
        instrument = INSTRUMENT_BY_KEY[key]
        is_direct = key in direct_terms
        assets.append({"asset_key": key, "name": instrument.label, "ticker": instrument.ticker,
                       "kind": instrument.type, "relation": "direct_mention" if is_direct else "topic_related",
                       "matched_terms": list(direct_terms.get(key, ())),
                       "topic_keys": [topic for topic in instrument.topic_keys if topic in topics],
                       "note": "标题或简介直接提及该公司、品牌或代码；不代表买卖建议。" if is_direct else
                               "同主题观察标的；不表示原文提及、持仓关系或确定受益。"})
    return {"topic_tags": list(topics), "instrument_keys": [key for key, _terms in direct], "assets": assets}


def matches_topic(title: str, snippet: str, topic: str) -> bool:
    return topic in _classification(str(title or "")[:500], str(snippet or "")[:1600])[0]


def matches_instrument(title: str, snippet: str, key: str, include_related=False) -> bool:
    _topics, direct, related = _classification(str(title or "")[:500], str(snippet or "")[:1600])
    return any(asset_key == key for asset_key, _terms in direct) or (bool(include_related) and key in related)


def catalog(rows=()) -> dict:
    """Coverage counts use the complete bounded time window, before UI filters."""
    topic_counts = dict.fromkeys(TOPIC_BY_KEY, 0)
    direct_counts = dict.fromkeys(INSTRUMENT_BY_KEY, 0)
    related_counts = dict.fromkeys(INSTRUMENT_BY_KEY, 0)
    for row in rows:
        topics, direct, related = _classification(str(row["title"] or "")[:500], str(row["snippet"] or "")[:1600])
        for key in topics:
            topic_counts[key] += 1
        for key, _terms in direct:
            direct_counts[key] += 1
        for key in related:
            related_counts[key] += 1
    instruments = []
    for item in INSTRUMENTS:
        row = {"key": item.key, "label": item.label, "ticker": item.ticker, "market": item.market,
               "type": item.type, "topic_keys": list(item.topic_keys),
               "direct_count": direct_counts[item.key], "related_count": related_counts[item.key]}
        if item.key == "US:SPCX":
            row.update(_SPACEX_VERIFICATION)
        instruments.append(row)
    return {"topics": [{**topic, "count": topic_counts[key]} for key, topic in TOPIC_BY_KEY.items()],
            "instruments": instruments, "counts_scope": "all_sources_last_7_days",
            "association_note": "直接提及与主题关联分开统计；主题关联不表示原文提及或确定投资收益。"}
