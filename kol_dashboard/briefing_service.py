"""Read-only Daily Briefing aggregation over persisted public records.

This module deliberately has no collector or provider imports.  Building a
briefing is a bounded projection of rows that have already been written by the
KOL, macro and decision collectors; an HTTP request must never start a scan or
an LLM call.
"""

from __future__ import annotations

import math
import re
import hashlib
import ipaddress
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
STALE_AFTER_SECONDS = 90 * 60
MAX_HIGHLIGHTS = 5
MAX_FIRSTHAND = 6
MAX_WATCHPOINTS = 5
MAX_SECTION_ITEMS = 6
EVENT_LOOKBACK_HOURS = 24
EVENT_QUERY_LIMIT = 240
MACRO_HISTORY_LIMIT = 72

SECTION_KEYS = ("macro", "world", "finance", "technology", "ai", "investors")
SECTION_DEFINITIONS: dict[str, tuple[str, str]] = {
    "macro": ("宏观信息", "央行、通胀、就业、增长、财政与关键经济指标"),
    "world": ("全球要闻", "地缘政治、政策、贸易与全球供应链变化"),
    "finance": ("金融要闻", "市场异动、财报、监管、并购与金融体系动态"),
    "technology": ("科技前沿", "芯片、云、机器人、生物科技与前沿工程进展"),
    "ai": ("AI 前沿", "模型、算力、AI 产品、研究、融资与治理进展"),
    "investors": ("投资大师动态", "投资人公开披露、持仓、信件、演讲与访谈"),
}

# Ninety minutes describes whether a populated rail is currently fresh.  It is
# deliberately shorter than the 24-hour inclusion window: useful context can
# remain visible while the UI still tells the reader that this particular rail
# has not received a recent verified update.
SECTION_STALE_AFTER_SECONDS = STALE_AFTER_SECONDS

_INVESTOR_KOL_KEYS = {
    "buffett",
    "dalio",
    "duanyongping",
    "danbin",
    "dimon",
    "burry",
    "howardmarks",
    "cathiewood",
    "serenity",
}
_MACRO_KOL_KEYS = {"powell", "pangongsheng", "renzeping"}
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "ocid",
    "ref",
    "ref_src",
    "spm",
}
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "credential",
    "key",
    "password",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-token",
}

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
_EVIDENCE_RANK = {
    "official_body": 3,
    "indicator_data": 3,
    "post_text": 3,
    "title_and_snippet": 2,
    "title": 1,
    "title_only": 1,
}
_REVISION_PROVENANCE_RANK = {
    "kol_event": 1,
    "macro_event": 1,
    "imported_event": 0,
}
_TIER_LABELS = {
    "official": "官方正文",
    "first_party": "一手原文",
    "reporting": "媒体报道",
    "discovery": "聚合线索",
}
_DISCOVERY_KINDS = {"hn_story", "ai_digest", "paper_digest"}
_DISCOVERY_CHANNELS = {
    "hacker_news_top",
    "hacker_news_best",
    "ai_digest_rss",
    "ai_brief_rss",
}
_DISCOVERY_KIND_CHANNELS = {
    "hn_story": {"hacker_news_top", "hacker_news_best"},
    "ai_digest": {"ai_digest_rss"},
    "paper_digest": {"ai_brief_rss"},
}
_ARTICLE_FILE_SUFFIXES = {".asp", ".aspx", ".htm", ".html", ".pdf", ".php"}
_GENERIC_URL_TERMINALS = {
    "about",
    "ai",
    "all",
    "announcement",
    "announcements",
    "archive",
    "archives",
    "article",
    "articles",
    "blog",
    "blogs",
    "category",
    "categories",
    "company-announcements",
    "home",
    "index",
    "latest",
    "news",
    "news-releases",
    "newsroom",
    "paper",
    "papers",
    "press-release",
    "press-releases",
    "publication",
    "publications",
    "research",
    "technology",
    "topics",
}
_GENERIC_URL_SUFFIX_TOKENS = {
    "announcement",
    "announcements",
    "archive",
    "archives",
    "category",
    "categories",
    "newsroom",
}
_IDENTITY_QUERY_KEYS = {"article", "document", "id", "paper", "post", "story"}
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www\.)?arxiv\.org/(?:abs|pdf)/|"
    r"huggingface\.co/papers/)"
    r"(?P<id>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?(?:[?#].*)?$",
    re.IGNORECASE,
)
_IMPACT_LABELS = {"high": "高影响", "medium": "中影响", "low": "低影响"}
_AGGREGATOR_SOURCE_RE = re.compile(
    r"(?:\bbing\b|\bbaidu\b|\bgoogle\s+news\b|\bhacker\s+news\b|"
    r"\bai\s+(?:digest|brief)\b|百度(?:新闻|资讯)|必应(?:新闻|资讯))",
    re.IGNORECASE,
)
_AGGREGATOR_HOSTS = {
    "ai-brief.liziran.com",
    "ai-digest.liziran.com",
    "bing.com",
    "www.bing.com",
    "cn.bing.com",
    "baidu.com",
    "www.baidu.com",
    "news.baidu.com",
    "news.google.com",
    "hacker-news.firebaseio.com",
    "news.ycombinator.com",
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

_AI_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])"
    r"(?:ai|agi|llm|chatgpt|openai|anthropic|deepmind|gemini|claude)"
    r"(?![A-Za-z0-9_])|"
    r"人工智能|生成式\s*AI|大模型|基础模型|推理模型|多模态|智能体|"
    r"机器学习|神经网络|模型训练|模型推理|算力集群)",
    re.IGNORECASE,
)
_TECHNOLOGY_RE = re.compile(
    r"(?:半导体|芯片|晶圆|光刻|机器人|自动驾驶|量子|云计算|数据中心|"
    r"网络安全|生物科技|航天|火箭|卫星|软件|开源|开发者|"
    r"(?<![A-Za-z0-9_])(?:semiconductor|chip|robotics?|quantum|cloud|"
    r"cybersecurity|biotech|spacex|software|hardware|gpu|cuda)"
    r"(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_MACRO_RE = re.compile(
    r"(?:央行|美联储|人民银行|货币政策|财政政策|利率决议|加息|降息|"
    r"通胀|消费者价格|生产者价格|非农|失业率|就业数据|国内生产总值|"
    r"经济增长|衰退|国债收益率|收益率曲线|"
    r"(?<![A-Za-z0-9_])(?:pmi|gdp|cpi|ppi|fed|fomc|central bank|"
    r"inflation|payrolls?|unemployment|interest rates?|rate cut|rate hike|"
    r"recession|treasury yields?)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_WORLD_RE = re.compile(
    r"(?:地缘|战争|冲突|军事行动|停火|制裁|关税|贸易战|外交|大选|"
    r"政府|白宫|国会|欧盟|北约|供应链|伊朗|以色列|乌克兰|俄罗斯|"
    r"(?<![A-Za-z0-9_])(?:war|conflict|ceasefire|sanctions?|tariffs?|"
    r"geopolitic|white house|congress|election|nato|iran|israel|ukraine|"
    r"russia)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_FINANCE_RE = re.compile(
    r"(?:股市|股票|债券|外汇|汇率|商品|黄金|原油|比特币|加密资产|"
    r"财报|营收|利润|监管|并购|收购|上市|IPO|基金|银行|保险|"
    r"(?<![A-Za-z0-9_])(?:stocks?|shares?|bonds?|forex|earnings|revenue|"
    r"profit|merger|acquisition|ipo|fund|bank|bitcoin|crypto)"
    r"(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_INVESTOR_ACTION_RE = re.compile(
    r"(?:持仓|加仓|增持|买入|减持|卖出|清仓|建仓|披露|股东信|致股东|"
    r"投资组合|公开信|访谈|演讲|观点|看好|警告|押注|13F|"
    r"(?<![A-Za-z0-9_])(?:buys?|bought|adds?|increases?|trims?|cuts?|"
    r"sells?|sold|stake|portfolio|shareholder letter|filing|interview|"
    r"speech|bet)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_KNOWN_ENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "vance",
        re.compile(
            r"(?:万斯|(?<![A-Za-z0-9_])(?:jd|j\.d\.)\s+vance"
            r"(?![A-Za-z0-9_]))",
            re.I,
        ),
    ),
    ("trump", re.compile(r"(?:特朗普|川普|(?<![A-Za-z0-9_])trump(?![A-Za-z0-9_]))", re.I)),
    ("iran", re.compile(r"(?:伊朗|对伊|(?<![A-Za-z0-9_])iran(?![A-Za-z0-9_]))", re.I)),
    ("israel", re.compile(r"(?:以色列|(?<![A-Za-z0-9_])israel(?![A-Za-z0-9_]))", re.I)),
    ("fed", re.compile(r"(?:美联储|(?<![A-Za-z0-9_])(?:fed|fomc)(?![A-Za-z0-9_]))", re.I)),
    ("pboc", re.compile(r"(?:中国人民银行|(?<![A-Za-z0-9_])pboc(?![A-Za-z0-9_]))", re.I)),
    ("openai", re.compile(r"(?:OpenAI|开放人工智能)", re.I)),
    ("nvidia", re.compile(r"(?:英伟达|(?<![A-Za-z0-9_])nvidia(?![A-Za-z0-9_]))", re.I)),
    ("tesla", re.compile(r"(?:特斯拉|(?<![A-Za-z0-9_])tesla(?![A-Za-z0-9_]))", re.I)),
    ("apple", re.compile(r"(?:苹果公司|(?<![A-Za-z0-9_])(?:apple|aapl)(?![A-Za-z0-9_]))", re.I)),
    ("meta", re.compile(r"(?:Meta\s+Platforms|(?<![A-Za-z0-9_])meta(?![A-Za-z0-9_])|脸书)", re.I)),
    ("amd", re.compile(r"(?:(?<![A-Za-z0-9_])amd(?![A-Za-z0-9_])|超威半导体)", re.I)),
    ("bitcoin", re.compile(r"(?:比特币|(?<![A-Za-z0-9_])bitcoin(?![A-Za-z0-9_]))", re.I)),
)
_TITLE_SYNONYMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:美国|美)副总统", re.I), ""),
    (re.compile(r"(?:j\.d\.|jd)\s*vance", re.I), "vance"),
    (re.compile(r"对伊朗", re.I), "对伊"),
    (re.compile(r"(?:伊朗军事行动|对伊军事行动)", re.I), "对伊冲突"),
    (re.compile(r"(?:并不(?:是|算)|并非|不是|不算)", re.I), "非"),
    (re.compile(r"(?:表示|声称|宣称|说道|说)[:：]?", re.I), "称"),
    (re.compile(r"(?:最新|快讯|突发)[:：]?", re.I), ""),
)
_SEMANTIC_MODAL_RE = re.compile(
    r"(?:可能|或将|预计|预期|计划|拟于?|有望|据悉|传闻|将于|即将|"
    r"如果|假如|若(?:非)?|是否|考虑|讨论|研究|评估|"
    r"明天|明年|下周|下月|下季度|下次会议|未来|"
    r"将(?:会)?(?=.{0,8}(?:降息|加息|发布|推出|召回|增持|减持|买入|卖出))|"
    r"(?<![A-Za-z0-9_])(?:may|might|could|would|will|expects?|expected|"
    r"plans?|planned|reportedly|rumou?red|set\s+to|scheduled\s+to|"
    r"forecast(?:s|ed)?|likely\s+to|if|"
    r"next\s+(?:week|month|quarter|year|meeting)|"
    r"consider(?:s|ed|ing)?|debate(?:s|d|ing)?|weigh(?:s|ed|ing)?|"
    r"discuss(?:es|ed|ing)?|eye(?:s|d|ing)?|explor(?:e|es|ed|ing)|"
    r"assess(?:es|ed|ing)?|evaluat(?:e|es|ed|ing)|"
    r"(?:leave(?:s)?|left)\s+(?:the\s+)?door\s+open)(?![A-Za-z0-9_])|"
    r"[?？])",
    re.IGNORECASE,
)
_SEMANTIC_HISTORICAL_CONTEXT_RE = re.compile(
    r"(?:此前|曾经?|去年|上月|上季度|上次会议|前次会议|"
    r"[0-9一二三四五六七八九十]{1,3}月|"
    r"(?<![A-Za-z0-9_])(?:after|following|previously|earlier|last\s+year|"
    r"(?:at\s+(?:its\s+)?|in\s+(?:its\s+)?)previous\s+meeting|"
    r"in\s+(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december|jan|feb|mar|apr|jun|jul|"
    r"aug|sep|sept|oct|nov|dec))"
    r"(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_SEMANTIC_POSITIVE_ACTION_PATTERNS: tuple[
    tuple[str, re.Pattern[str]], ...
] = (
    (
        "rate_cut",
        re.compile(
            r"(?:降息|下调(?:基准|政策)?利率|\b(?:cut|cuts|cutting|lowered|lowers)\s+"
            r"(?:the\s+)?(?:policy\s+)?rates?\b)",
            re.I,
        ),
    ),
    (
        "rate_hike",
        re.compile(
            r"(?:加息|上调(?:基准|政策)?利率|\b(?:hike|hikes|hiked|raise|raises|raised)\s+"
            r"(?:the\s+)?(?:policy\s+)?rates?\b)",
            re.I,
        ),
    ),
    (
        "rate_hold",
        re.compile(
            r"(?:维持(?:政策)?利率不变|按兵不动|"
            r"\b(?:keep|keeps|kept|hold|holds|held|leave|leaves|left)\s+"
            r"(?:the\s+)?(?:policy\s+)?rates?\s+(?:steady|unchanged)\b)",
            re.I,
        ),
    ),
    (
        "product_recall",
        re.compile(r"(?:召回|撤回产品|\brecall(?:s|ed|ing)?\b)", re.I),
    ),
    (
        "holding_decrease",
        re.compile(
            r"(?:减持|卖出|清仓|削减持仓|\b(?:trim|trims|trimmed|sell|sells|sold|"
            r"cuts?)\s+(?:its\s+)?(?:stake|holding|position|shares?)\b)",
            re.I,
        ),
    ),
    (
        "holding_increase",
        re.compile(
            r"(?:增持|加仓|买入|建仓|\b(?:buy|buys|bought|add|adds|added|"
            r"increase|increases|increased)\s+(?:its\s+)?(?:stake|holding|position|shares?)\b)",
            re.I,
        ),
    ),
    (
        "product_release",
        re.compile(
            r"(?:发布|推出|揭晓|\b(?:release|releases|released|launch|launches|"
            r"launched|unveil|unveils|unveiled)\b)",
            re.I,
        ),
    ),
)
_NARROW_SEMANTIC_CLAIM_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "rate_cut": (
        re.compile(
            r"^(?:the\s+)?(?:fed|fomc|pboc)\s+"
            r"(?:cuts?|cut|lowers?|lowered)\s+(?:the\s+)?"
            r"(?:(?:policy|benchmark|key)\s+)?rates?\s+(?:by\s+)?"
            r"\d+(?:\.\d+)?\s*(?:bps?|basis\s+points?)\.?$",
            re.I,
        ),
        re.compile(
            r"^(?:美联储|中国人民银行|央行)\s*(?:宣布|决定)?\s*"
            r"(?:降息|下调(?:基准|政策)?利率)\s*\d+(?:\.\d+)?\s*"
            r"个?基点[。.!！]?$",
            re.I,
        ),
    ),
    "rate_hike": (
        re.compile(
            r"^(?:the\s+)?(?:fed|fomc|pboc)\s+"
            r"(?:hikes?|hiked|raises?|raised)\s+(?:the\s+)?"
            r"(?:(?:policy|benchmark|key)\s+)?rates?\s+(?:by\s+)?"
            r"\d+(?:\.\d+)?\s*(?:bps?|basis\s+points?)\.?$",
            re.I,
        ),
        re.compile(
            r"^(?:美联储|中国人民银行|央行)\s*(?:宣布|决定)?\s*"
            r"(?:加息|上调(?:基准|政策)?利率)\s*\d+(?:\.\d+)?\s*"
            r"个?基点[。.!！]?$",
            re.I,
        ),
    ),
    "war_denial": (
        re.compile(
            r"^(?:美国\s*)?(?:副总统\s*)?万斯\s*(?:表示|声称|宣称|称)?\s*"
            r"(?:对伊朗?|伊朗)\s*(?:军事行动|冲突)?\s*"
            r"(?:并非|不是|不算|并不算)\s*(?:一场)?战争[。.!！]?$",
            re.I,
        ),
        re.compile(
            r"^(?:(?:u\.?s\.?)\s+vice\s+president\s+)?"
            r"(?:(?:j\.?d\.?)\s+)?vance\s+(?:says?|said|calls?)\s+"
            r"(?:the\s+)?iran(?:ian)?\s+(?:operation|conflict)\s+"
            r"(?:is|was)\s+not\s+(?:a\s+)?war\.?$",
            re.I,
        ),
    ),
    "product_release": (
        re.compile(
            r"^openai\s+(?:releases?|released|launches?|launched|"
            r"unveils?|unveiled)\s+(?:a\s+|an\s+|the\s+)?"
            r"(?:(?:gpt[- ]?[a-z0-9.]+)(?:\s+(?:ai\s+)?model)?|"
            r"(?:new\s+)?(?:reasoning|ai|foundation|multimodal)\s+model)"
            r"(?:\s+(?:for|aimed\s+at)\s+(?:enterprise\s+)?"
            r"(?:developers?|customers?|users?|businesses?))?[.!]?$",
            re.I,
        ),
        re.compile(
            r"^(?:openai|开放人工智能)\s*(?:发布|推出|揭晓)\s*(?:新)?"
            r"(?:gpt[- ]?[a-z0-9.]+(?:\s*模型)?|推理模型|大模型|"
            r"基础模型|多模态模型)[。.!！]?$",
            re.I,
        ),
        re.compile(
            r"^(?:新)?(?:推理模型|大模型|基础模型|多模态模型)\s*由\s*"
            r"(?:openai|开放人工智能)\s*(?:正式)?\s*(?:发布|推出|揭晓)"
            r"[。.!！]?$",
            re.I,
        ),
    ),
}
_PUBLIC_ITEM_FIELDS = (
    "id",
    "kind",
    "title",
    "summary",
    "why_it_matters",
    "impact",
    "source_tier",
    "source_label",
    "source_url",
    "published_at",
    "disclosed_at",
    "effective_at",
    "fetched_at",
    "last_updated_at",
    "time_status",
    "source_count",
    "related_records",
    "kol_name",
    "ai_status",
    "ai_summary_used",
    "evidence_basis",
    "assets",
    "rank_reason",
    "story_key",
    "primary_section",
    "cross_tags",
    "featured_at",
    "original_url",
    "discussion_url",
    "discovered_via",
    "publication_time_verified",
    "hn_id",
    "hn_score",
    "hn_comments",
    "hn_rank",
    "heat_score",
)


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


def _public_temporal(value: Any) -> str:
    """Normalize an explicit public date/timestamp without inventing a zone."""
    if isinstance(value, str):
        raw = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                return ""
            return raw
    parsed = _datetime(value)
    return _iso(parsed) if parsed is not None else ""


def _query_key_is_sensitive(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = normalized.casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    segments = tuple(part for part in normalized.split("_") if part)
    sensitive_segments = {
        "auth",
        "authorization",
        "apikey",
        "bearer",
        "credential",
        "jwt",
        "password",
        "passwd",
        "secret",
        "session",
        "sessionid",
        "signature",
        "token",
    }
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_QUERY_KEYS
        or any(part in sensitive_segments for part in segments)
        or any(
            segments[index : index + 2] in {("api", "key"), ("access", "code")}
            for index in range(max(0, len(segments) - 1))
        )
        or compact.startswith(("clientsecret", "secretkey"))
        or compact.endswith("apikey")
        or compact.endswith(
            ("token", "secret", "password", "credential", "signature", "sessionid")
        )
    )


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
    # WHATWG URL parsers treat backslashes as authority/path separators for
    # special schemes.  urllib does not, so reject them (and embedded
    # whitespace) before parsing to keep the browser destination identical to
    # the server-side safety decision.
    if not raw or "\\" in raw or any(char.isspace() for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    scheme = parsed.scheme.lower()
    raw_host = parsed.hostname or ""
    # Browsers apply UTS46-style normalization to Unicode/fullwidth numeric
    # hosts and percent escapes inside an authority.  Reject both categories
    # instead of letting urllib validate one destination while the click opens
    # another (for example fullwidth 127.0.0.1 -> loopback).
    if not raw_host.isascii() or "%" in raw_host:
        return ""
    host = raw_host.lower().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (
            port is not None
            and not (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)
            )
        )
    ):
        return ""
    address = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    if address is None and re.fullmatch(
        r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
        host,
        re.IGNORECASE,
    ):
        return ""
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or (address is None and "." not in host)
        or "%" in host
    ):
        return ""
    if address is not None and (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return ""

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_query_key_is_sensitive(key) for key, _ in query_items):
        return ""
    safe_query = urlencode(
        sorted(
            (key, item_value)
            for key, item_value in query_items
            if not key.casefold().strip().startswith("utm_")
            and key.casefold().strip() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    display_host = f"[{host}]" if address is not None and address.version == 6 else host
    is_default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = display_host if port is None or is_default_port else f"{display_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, safe_query, ""))


def _host(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


def _canonical_identity_url(value: Any) -> str:
    """Return a comparison-only URL with tracking noise removed.

    The public link remains untouched.  Query parameters are retained unless
    they are known campaign/referral keys because some regulators and IR sites
    use a query parameter as the actual document identity.
    """
    raw = _public_url(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return ""
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    netloc = host if port in {None, 80, 443} else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _specific_original_url(value: str) -> bool:
    """Accept only a conservative, article-like canonical URL."""
    if _ARXIV_URL_RE.fullmatch(unquote(value.strip())):
        return True
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    segments = [
        unquote(segment).casefold()
        for segment in parsed.path.split("/")
        if segment
    ]
    while segments and _LOCALE_SEGMENT_RE.fullmatch(segments[0]):
        segments.pop(0)
    if not segments:
        return False

    last_segment = segments[-1]
    suffix = next(
        (
            candidate
            for candidate in _ARTICLE_FILE_SUFFIXES
            if last_segment.endswith(candidate)
        ),
        "",
    )
    stem = last_segment[: -len(suffix)] if suffix else last_segment
    terminal_tokens = [token for token in re.split(r"[-_.]+", stem) if token]
    if stem in _GENERIC_URL_TERMINALS or (
        terminal_tokens and terminal_tokens[-1] in _GENERIC_URL_SUFFIX_TOKENS
    ):
        return False
    query_has_identity = any(
        key.casefold() in _IDENTITY_QUERY_KEYS and bool(query_value)
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=False)
    )
    if suffix or query_has_identity or re.search(r"\d", stem):
        return True
    return len(segments) >= 2


def _observed_time(item: Mapping[str, Any]) -> datetime | None:
    for field in (
        "featured_at",
        "last_updated_at",
        "last_seen_at",
        "published_at",
        "fetched_at",
    ):
        parsed = _datetime(item.get(field))
        if parsed is not None:
            return parsed
    return None


def _publication_time(item: Mapping[str, Any]) -> datetime | None:
    if _text(item.get("time_status"), 24).lower() != "verified":
        return None
    return _datetime(item.get("published_at"))


def _featured_time(item: Mapping[str, Any]) -> datetime | None:
    """Return a curation time only for validated imported discovery records."""
    if _text(item.get("source_tier"), 24).lower() != "discovery":
        return None
    kind = _text(item.get("_content_kind"), 32).lower()
    # Hacker News `featured_at` is the collector observation time.  It must
    # never refresh an older submission; HN selection is anchored exclusively
    # to its verified API submission timestamp in `published_at`.
    if kind not in {"ai_digest", "paper_digest"}:
        return None
    return _datetime(item.get("featured_at"))


def _selection_time(item: Mapping[str, Any]) -> datetime | None:
    """Rank a curated lead by selection time without rewriting publication."""
    return _featured_time(item) or _publication_time(item)


def _latest_timestamp(source: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    values = [
        parsed
        for field in fields
        if (parsed := _datetime(source.get(field))) is not None
    ]
    return _iso(max(values)) if values else ""


def _normalized_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value, 240)).casefold()
    for pattern, replacement in _TITLE_SYNONYMS:
        text = pattern.sub(replacement, text)
    # Publisher suffixes and punctuation are presentation differences rather
    # than story identity.  Keep letters, numbers and CJK characters only.
    text = re.sub(r"(?:[-–—_|｜]\s*)?(?:路透|reuters|彭博|bloomberg)$", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _semantic_action(text: str) -> str:
    if re.search(r"(?:战争|\bwar\b)", text, re.I) and re.search(
        r"(?:非战争|不是战争|并非战争|not\s+(?:a\s+)?war)", text, re.I
    ):
        return "war_denial"
    negation = (
        r"(?:不会|可能不会|不太可能|不再|并未|并没有|未曾|未|没有|暂不|"
        r"无意|不打算|暂无计划|拒绝|避免|放弃|"
        r"传闻不实|消息不实|并不属实|系谣言|辟谣|否认(?:将|会)?|"
        r"\b(?:not|never|without|cannot|can't|unlikely\s+to|rules?\s+out|"
        r"against|refuses?\s+to|refrains?\s+from|avoids?|"
        r"has\s+no\s+intention\s+of|will\s+not|won't|would\s+not|"
        r"has\s+not|hasn't|had\s+not|hadn't|does\s+not|"
        r"doesn't|did\s+not|didn't|is\s+not|isn't|are\s+not|aren't|"
        r"was\s+not|wasn't|were\s+not|weren't|no\s+plans?\s+to|"
        r"not\s+(?:currently\s+)?expected\s+to|"
        r"den(?:y|ies|ied)\s+(?:it\s+)?"
        r"(?:will|would|has|had)?))"
    )
    negated_actions = (
        (
            "rate_cut_denial",
            r"(?:降息|下调(?:基准|政策)?利率|"
            r"(?:cut|cuts|cutting|lower|lowers|lowered|lowering)\s+"
            r"(?:the\s+)?(?:policy\s+)?rates?)",
        ),
        (
            "rate_hike_denial",
            r"(?:加息|上调(?:基准|政策)?利率|"
            r"(?:hike|hikes|hiked|hiking|raise|raises|raised|raising)\s+"
            r"(?:the\s+)?(?:policy\s+)?rates?)",
        ),
        (
            "holding_decrease_denial",
            r"(?:减持|卖出|清仓|削减持仓|"
            r"(?:trim|trims|trimmed|trimming|sell|sells|sold|selling|"
            r"cut|cuts|cutting)\s+"
            r"(?:its\s+)?(?:stake|holding|position|shares?))",
        ),
        (
            "holding_increase_denial",
            r"(?:增持|加仓|买入|建仓|"
            r"(?:buy|buys|bought|buying|add|adds|added|adding|increase|"
            r"increases|increased|increasing)\s+"
            r"(?:its\s+)?(?:stake|holding|position|shares?))",
        ),
        (
            "product_recall_denial",
            r"(?:召回|撤回产品|recall(?:s|ed|ing)?)",
        ),
        (
            "product_release_denial",
            r"(?:发布|推出|揭晓|(?:release|releases|released|releasing|"
            r"launch|launches|launched|launching|unveil|unveils|unveiled|"
            r"unveiling))",
        ),
    )
    for action, pattern in negated_actions:
        # Keep negated claims in a separate bucket even when qualifiers sit
        # between the negation and action ("not in September cutting", "未如
        # 预期降息").  A title-level negation is deliberately fail-closed for
        # deduplication: uncertain grammar should produce fewer merges, never
        # collapse a denial into an affirmative market-moving event.
        if re.search(negation, text, re.I) and re.search(pattern, text, re.I):
            return action
    for action, pattern in _SEMANTIC_POSITIVE_ACTION_PATTERNS:
        if action != "rate_hold" and pattern.search(text):
            return action
    return ""


def _has_ambiguous_action_context(text: str) -> bool:
    """Fail closed when a headline contains more than one temporal/action claim."""
    actions = {
        action
        for action, pattern in _SEMANTIC_POSITIVE_ACTION_PATTERNS
        if pattern.search(text)
    }
    return len(actions) > 1 or bool(
        actions and _SEMANTIC_HISTORICAL_CONTEXT_RE.search(text)
    )


def _is_narrow_semantic_claim(text: str, action: str) -> bool:
    """Allow cross-source semantics only for small, auditable fact grammars."""
    return any(
        pattern.fullmatch(text.strip())
        for pattern in _NARROW_SEMANTIC_CLAIM_PATTERNS.get(action, ())
    )


def _semantic_amounts(text: str) -> tuple[str, ...]:
    output: set[str] = set()
    for match in re.finditer(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:个?基点|bps?\b|basis\s+points?\b)",
        text,
        re.I,
    ):
        number = float(match.group(1))
        output.add(f"{number:g}bp")
    for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)\s*%", text):
        number = float(match.group(1))
        output.add(f"{number:g}pct")
    return tuple(sorted(output))


def _semantic_features(item: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Build a bounded deterministic signature for defensible near-duplicates.

    There is intentionally no general fuzzy match.  A candidate needs a known
    actor/entity, an explicit directional action, compatible object details and
    a verified publication-day bucket.  Exact equality prevents transitive
    cluster drift and keeps runtime linear in the number of candidates.
    """
    published = _publication_time(item)
    if published is None:
        return None
    raw_title = _text(item.get("_raw_title") or item.get("title"), 240)
    if not raw_title:
        return None
    normalized_text = unicodedata.normalize("NFKC", raw_title).casefold()
    action = _semantic_action(normalized_text)
    english_infinitive = re.search(
        r"(?<![A-Za-z0-9_])to\s+(?:cut|lower|hike|raise|release|launch|"
        r"unveil|recall|trim|sell|buy|add|increase)(?![A-Za-z0-9_])",
        normalized_text,
        re.I,
    )
    if action and (
        _SEMANTIC_MODAL_RE.search(normalized_text)
        or english_infinitive
        or _has_ambiguous_action_context(normalized_text)
    ):
        # Forecasts, plans and rumours must never collapse into an observed
        # event.  Multiple actions and historical clauses are equally unsafe:
        # a regex cannot reliably decide which clause is the current fact.
        # Exact/stable source identities can still deduplicate these records;
        # cross-source semantic merging deliberately abstains.
        return None
    if action.endswith("_denial") and action != "war_denial":
        # Negation scope is too easy to overstate with headline regexes (for
        # example "cuts rates, not ending QT").  Keep such records distinct
        # across sources unless their exact URL/title identity already proves
        # equivalence.  This intentionally trades recall for factual safety.
        return None
    if action and not _is_narrow_semantic_claim(normalized_text, action):
        # Entity/action extraction alone cannot safely distinguish a current
        # fact from advice, counterfactuals or subordinate clauses.  Unknown
        # wording stays separate instead of expanding an open-ended denylist.
        return None
    title_entities = {
        key
        for key, pattern in _KNOWN_ENTITY_PATTERNS
        if pattern.search(normalized_text)
    }
    day_bucket = published.astimezone(BEIJING).date().isoformat()
    if not action:
        # Even a named label such as "OpenAI Update" can describe several
        # unrelated stories on the same day.  Exact-title identity is therefore
        # URL/stable-ID scoped; cross-source merging requires an explicit action
        # that passes one of the narrow fact grammars above.
        return None
    kol_key = _text(item.get("_kol_key"), 80).lower()
    asset_entities = {
        f"asset:{key}"
        for asset in item.get("assets", [])
        if isinstance(asset, Mapping)
        if (key := _text(asset.get("asset_key"), 40).upper())
    }

    details: set[str] = set(_semantic_amounts(normalized_text))
    if action in {
        "rate_cut",
        "rate_hike",
        "rate_cut_denial",
        "rate_hike_denial",
    }:
        policy_entities = title_entities.intersection({"fed", "pboc"})
        if not policy_entities:
            return None
        entities = policy_entities
        details.add("policy_rate")
    elif action == "war_denial":
        entities = title_entities.intersection(
            {"vance", "trump", "iran", "israel"}
        )
        if "vance" not in entities or "iran" not in entities:
            return None
        details.add("iran_conflict")
    elif action in {
        "holding_increase",
        "holding_decrease",
        "holding_increase_denial",
        "holding_decrease_denial",
    }:
        entities = title_entities | asset_entities
        if kol_key:
            entities.add(f"kol:{kol_key}")
        # An actor alone is insufficient: two portfolio changes by the same
        # investor on the same day must not collapse into one story.
        if len(entities) < 2:
            return None
    else:
        entities = title_entities | asset_entities
        if not entities:
            return None
        if re.search(r"(?:推理模型|reasoning\s+model)", normalized_text, re.I):
            details.add("reasoning_model")
        elif re.search(r"(?:大模型|基础模型|\b(?:ai\s+)?model\b)", normalized_text, re.I):
            details.add("model")
        product_names = re.findall(
            r"(?<![A-Za-z0-9_])(?:gpt|claude|gemini|llama|iphone|cuda)"
            r"[- ]?[a-z0-9.]+(?![A-Za-z0-9_])",
            normalized_text,
            re.I,
        )
        details.update(name.replace(" ", "-").lower() for name in product_names)
        if not details:
            return None

    return (
        day_bucket,
        action,
        ",".join(sorted(entities)),
        ",".join(sorted(details)),
    )


def _ai_cluster_bucket(item: Mapping[str, Any]) -> str:
    cluster = _text(item.get("_ai_cluster_key"), 96)
    if not cluster:
        return ""
    # A model cluster is supporting evidence, not permission to erase time,
    # entity, object or action boundaries.  Reuse the deterministic signature
    # so a broad model label cannot collapse GPT-6/GPT-7 or assertion/denial.
    signature = _semantic_features(item)
    if signature is None:
        return ""
    return "\x1f".join((cluster, *signature))


def _stable_external_id(source: Mapping[str, Any], *, kind: str) -> str:
    for field in ("document_id", "accession_id", "official_id", "external_id"):
        value = _text(source.get(field), 160)
        if value:
            return f"{field}:{value.casefold()}"
    if kind == "macro_event":
        value = _text(source.get("id"), 160)
        if value:
            return f"macro:{value.casefold()}"
    return ""


def _ai_cluster_identity(
    enrichment: Mapping[str, Any] | None,
    *,
    evidence_basis: str,
) -> str:
    if enrichment is None or evidence_basis == "title_only":
        return ""
    confidence = _finite_number(enrichment.get("confidence"))
    cluster = _text(enrichment.get("cluster_key"), 96).lower()
    if confidence is None or confidence < 0.75:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,95}", cluster):
        return ""
    return cluster


def _section_text(item: Mapping[str, Any]) -> str:
    values = [
        item.get("_raw_title"),
        item.get("title"),
        item.get("summary"),
        item.get("why_it_matters"),
        item.get("kol_name"),
    ]
    tags = item.get("_tags")
    if isinstance(tags, list):
        values.extend(tags)
    assets = item.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, Mapping):
                values.extend((asset.get("asset_key"), asset.get("name_zh")))
            else:
                values.append(asset)
    return " ".join(_text(value, 500) for value in values if value is not None)


def _section_matches(
    text: str,
    *,
    kol_key: str,
    kol_name: str,
) -> set[str]:
    matches: set[str] = set()
    if _AI_RE.search(text) or "THEME:AI" in text.upper():
        matches.add("ai")
    if _TECHNOLOGY_RE.search(text) or "THEME:SEMICONDUCTOR" in text.upper():
        matches.add("technology")
    if _MACRO_RE.search(text) or kol_key in _MACRO_KOL_KEYS:
        matches.add("macro")
    if _WORLD_RE.search(text):
        matches.add("world")
    if _FINANCE_RE.search(text):
        matches.add("finance")
    if kol_key in _INVESTOR_KOL_KEYS or (
        _INVESTOR_ACTION_RE.search(text)
        and kol_name
        and kol_key not in _MACRO_KOL_KEYS
        and not _MACRO_RE.search(text)
    ):
        matches.add("investors")
    return matches


def _section_memberships(item: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Choose a fact-led primary section and context-derived cross tags."""
    kind = _text(item.get("kind"), 40).lower()
    kol_key = _text(item.get("_kol_key"), 80).lower()
    kol_name = _text(item.get("kol_name"), 80)
    section_hint = _text(item.get("_section_hint"), 24).lower()
    raw_cross = item.get("_cross_tags_hint")
    # Primary placement is a fact classification.  AI-generated headlines may
    # add cross-navigation context, but must never move the source event into a
    # different primary rail.
    title_text = _text(item.get("_raw_title"), 500) or _text(
        item.get("title"), 500
    )
    primary_matches = _section_matches(
        title_text,
        kol_key=kol_key,
        kol_name=kol_name,
    )
    matches = primary_matches | _section_matches(
        _section_text(item),
        kol_key=kol_key,
        kol_name=kol_name,
    )
    if section_hint in SECTION_KEYS:
        matches.add(section_hint)
    if isinstance(raw_cross, list):
        matches.update(
            key
            for value in raw_cross
            if (key := _text(value, 24).lower()) in SECTION_KEYS
        )

    if kind == "imported_event" and section_hint in SECTION_KEYS:
        # The importer has already validated the source rail.  Content-derived
        # matches enrich navigation only and must not silently move a persisted
        # item to a different primary section.
        primary = section_hint
    elif kind == "macro_event":
        primary = "macro"
    elif kol_key in _MACRO_KOL_KEYS:
        primary = "macro"
    elif kol_key in _INVESTOR_KOL_KEYS:
        primary = "investors"
    elif primary_matches:
        primary = next(
            key
            for key in (
                "macro",
                "world",
                "investors",
                "ai",
                "technology",
                "finance",
            )
            if key in primary_matches
        )
    elif section_hint in SECTION_KEYS:
        primary = section_hint
    else:
        # Persisted KOL rows have already passed financial-intelligence gates;
        # when no narrower classification is defensible, finance is the safe
        # home rather than inventing a topical claim.
        primary = "finance"
    cross_tags = [key for key in SECTION_KEYS if key in matches and key != primary]
    return primary, cross_tags


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
    source_text = _text(source, 120).casefold()
    try:
        parsed = urlsplit(str(url or ""))
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path)
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        match = re.fullmatch(
            r"/([A-Za-z0-9_]{1,15})/status/([0-9]+)(?:/.*)?",
            path,
        )
        if match is None:
            return False
        prefix = "x @" if host in {"x.com", "www.x.com"} else "twitter @"
        return source_text == f"{prefix}{match.group(1).casefold()}"
    if host in {"truthsocial.com", "www.truthsocial.com"}:
        match = re.fullmatch(r"/@([^/]+)/(?:posts/)?([0-9]+)(?:/.*)?", path)
        return bool(
            match is not None
            and source_text == f"truth social @{match.group(1).casefold()}"
        )
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
    content_kind = _text(item.get("_content_kind"), 32).lower()
    if _strict_integer(item.get("hn_id"), minimum=1, maximum=9_007_199_254_740_991):
        score = _strict_integer(
            item.get("hn_score"), minimum=0, maximum=1_000_000
        )
        comments = _strict_integer(
            item.get("hn_comments"), minimum=0, maximum=1_000_000
        )
        if score is not None and comments is not None:
            parts.append(f"HN {score} 分 / {comments} 评论")
    if impact in _IMPACT_LABELS:
        parts.append(_IMPACT_LABELS[impact])
    if tier in _TIER_LABELS:
        parts.append(_TIER_LABELS[tier])
    if item.get("time_status") == "verified":
        parts.append(
            "HN 提交时间已核验"
            if content_kind == "hn_story"
            else "发布时间已核验"
        )
    elif item.get("time_status") == "featured_only":
        parts.append("精选时间已核验")
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
        "_trusted_evidence_basis": trusted_basis,
        "_raw_title": original_title,
        "_canonical_url": _canonical_identity_url(
            source.get("canonical_url") or source_url
        ),
        "_stable_external_id": _stable_external_id(source, kind="kol_event"),
        "_kol_key": _text(source.get("kol_key"), 80).lower(),
        "_kind_hint": _text(source.get("kind") or source.get("event_type"), 40),
    }
    if enrichment is not None:
        why = _text(enrichment.get("why_it_matters_zh"), 320)
        assets = _assets(enrichment)
        if why:
            item["why_it_matters"] = why
        if assets:
            item["assets"] = assets
        tags = enrichment.get("tags")
        if isinstance(tags, list):
            item["_tags"] = [
                value
                for raw in tags[:8]
                if (value := _text(raw, 40))
            ]
        cluster = _ai_cluster_identity(
            enrichment,
            evidence_basis=trusted_basis,
        )
        if cluster:
            item["_ai_cluster_key"] = cluster
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
    if isinstance(related, int) and not isinstance(related, bool) and related >= 1:
        item["related_records"] = related
        item["source_count"] = related
    kol_name = _text(source.get("kol_name_cn") or source.get("kol_name"), 80)
    if kol_name:
        item["kol_name"] = kol_name
    # A re-fetch is collection metadata, not proof that the underlying report
    # changed.  Keep the public evidence timestamp anchored to publication.
    last_updated = _latest_timestamp(source, ("published_at",))
    if last_updated:
        item["last_updated_at"] = last_updated
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
        "_trusted_evidence_basis": trusted_basis,
        "_raw_title": original_title,
        "_canonical_url": _canonical_identity_url(
            source.get("content_source_url") or source.get("url")
        ),
        "_stable_external_id": _stable_external_id(source, kind="macro_event"),
        "_kol_key": "",
        "_kind_hint": _text(source.get("kind"), 40),
    }
    if enrichment is not None:
        why = _text(enrichment.get("why_it_matters_zh"), 320)
        assets = _assets(enrichment)
        if why:
            item["why_it_matters"] = why
        if assets:
            item["assets"] = assets
        tags = enrichment.get("tags")
        if isinstance(tags, list):
            item["_tags"] = [
                value
                for raw in tags[:8]
                if (value := _text(raw, 40))
            ]
        cluster = _ai_cluster_identity(
            enrichment,
            evidence_basis=trusted_basis,
        )
        if cluster:
            item["_ai_cluster_key"] = cluster
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
    # The macro snapshot generation time is a collector heartbeat, not a
    # revision time for the linked policy/indicator event.
    last_updated = _latest_timestamp(source, ("published_at",))
    if last_updated:
        item["last_updated_at"] = last_updated
    item["rank_reason"] = _rank_reason(item)
    return item


def _strict_integer(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _imported_discovery_metadata(
    source: Mapping[str, Any],
    *,
    source_url: str,
    published: datetime | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Revalidate persisted producer metadata before public projection."""
    raw_kind = source.get("kind")
    if raw_kind is None:
        special_fields = {
            "discovered_via",
            "featured_at",
            "original_url",
            "discussion_url",
            "hn_id",
            "hn_score",
            "hn_comments",
            "hn_rank",
            "heat_score",
        }
        if any(source.get(field) is not None for field in special_fields):
            return None
        return {}
    if not isinstance(raw_kind, str) or raw_kind not in _DISCOVERY_KINDS:
        return None
    kind = raw_kind

    raw_channels = source.get("discovered_via")
    if (
        not isinstance(raw_channels, list)
        or not 1 <= len(raw_channels) <= len(_DISCOVERY_CHANNELS)
        or any(not isinstance(value, str) for value in raw_channels)
        or len(raw_channels) != len(set(raw_channels))
        or not set(raw_channels).issubset(_DISCOVERY_CHANNELS)
        or not set(raw_channels).intersection(_DISCOVERY_KIND_CHANNELS[kind])
    ):
        return None
    publication_verified = source.get("publication_time_verified")
    if not isinstance(publication_verified, bool):
        return None
    if publication_verified != (published is not None):
        return None
    expected_status = "verified" if publication_verified else "featured_only"
    if _text(source.get("time_status"), 24).lower() != expected_status:
        return None

    featured = _datetime(source.get("featured_at"))
    fetched = _datetime(source.get("fetched_at"))
    if (
        featured is None
        or fetched is None
        or featured > now
        or featured > fetched + timedelta(minutes=5)
        or (published is not None and published > featured + timedelta(minutes=5))
    ):
        return None
    original_raw = source.get("original_url")
    original_url = _public_url(original_raw) if original_raw is not None else ""
    if original_raw is not None and not original_url:
        return None
    discussion_raw = source.get("discussion_url")
    discussion_url = _public_url(discussion_raw) if discussion_raw is not None else ""
    if discussion_raw is not None and not discussion_url:
        return None

    output: dict[str, Any] = {
        "_content_kind": kind,
        "discovered_via": list(raw_channels),
        "publication_time_verified": publication_verified,
        "featured_at": _iso(featured),
    }
    if original_url:
        output["original_url"] = original_url
    if discussion_url:
        output["discussion_url"] = discussion_url

    has_hn_channel = bool(
        set(raw_channels).intersection({"hacker_news_top", "hacker_news_best"})
    )
    if has_hn_channel:
        hn_id = _strict_integer(
            source.get("hn_id"), minimum=1, maximum=9_007_199_254_740_991
        )
        hn_score = _strict_integer(
            source.get("hn_score"), minimum=0, maximum=1_000_000
        )
        hn_comments = _strict_integer(
            source.get("hn_comments"), minimum=0, maximum=1_000_000
        )
        hn_rank = _strict_integer(source.get("hn_rank"), minimum=1, maximum=500)
        heat_score = _finite_number(source.get("heat_score"))
        if (
            hn_id is None
            or hn_score is None
            or hn_comments is None
            or hn_rank is None
            or heat_score is None
            or not 0 <= heat_score <= 100
            or discussion_url
            != f"https://news.ycombinator.com/item?id={hn_id}"
        ):
            return None
        output.update(
            {
                "hn_id": hn_id,
                "hn_score": hn_score,
                "hn_comments": hn_comments,
                "hn_rank": hn_rank,
                "heat_score": round(heat_score, 1),
            }
        )
    elif discussion_url or any(
        source.get(field) is not None
        for field in ("hn_id", "hn_score", "hn_comments", "hn_rank", "heat_score")
    ):
        return None

    if kind == "hn_story":
        if _text(source.get("source"), 120).casefold() != "hacker news":
            return None
        if not publication_verified or published is None:
            return None
        if original_url:
            if source_url != original_url:
                return None
        elif source_url != discussion_url:
            return None
    else:
        expected = (
            ("ai digest", "ai-digest.liziran.com")
            if kind == "ai_digest"
            else ("ai brief", "ai-brief.liziran.com")
        )
        if (
            _text(source.get("source"), 120).casefold() != expected[0]
            or _host(source_url) != expected[1]
        ):
            return None
        if original_url and (
            original_url == source_url
            or _host(original_url) == expected[1]
            or not _specific_original_url(original_url)
        ):
            return None
    return output


def _imported_highlight(
    source: Mapping[str, Any],
    *,
    section_hint: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Project one already-validated imported snapshot item conservatively.

    A fetch timestamp proves discovery, not publication.  Ordinary rows still
    require a verified, in-window publication time.  The three explicitly
    validated discovery kinds may instead use an in-window ``featured_at`` for
    curation freshness while retaining (or omitting) the original publication
    timestamp exactly as supplied.
    """
    published = _datetime(source.get("published_at"))
    title = _text(source.get("title"), 180)
    if not title:
        return None
    source_url = _public_url(
        source.get("source_url") or source.get("canonical_url")
    )
    if not source_url:
        return None
    discovery_metadata = _imported_discovery_metadata(
        source,
        source_url=source_url,
        published=published,
        now=now,
    )
    if discovery_metadata is None:
        return None
    if discovery_metadata:
        content_kind = _text(discovery_metadata.get("_content_kind"), 32).lower()
        if content_kind == "hn_story":
            if (
                published is None
                or published > now
                or published < now - timedelta(hours=EVENT_LOOKBACK_HOURS)
            ):
                return None
        else:
            featured = _datetime(discovery_metadata.get("featured_at"))
            if (
                featured is None
                or featured > now
                or featured < now - timedelta(hours=EVENT_LOOKBACK_HOURS)
            ):
                return None
    elif (
        _text(source.get("time_status"), 24).lower() != "verified"
        or published is None
        or published > now
        or published < now - timedelta(hours=EVENT_LOOKBACK_HOURS)
    ):
        return None

    raw_tier = _text(source.get("source_tier"), 24).lower()
    host = _host(source_url)
    if discovery_metadata:
        tier = "discovery"
    elif raw_tier == "official" and _official_host(host):
        tier = "official"
    elif raw_tier == "first_party" and _original_social_post(
        source.get("source_label") or source.get("source"), source_url
    ):
        tier = "first_party"
    else:
        classified = classify_source(source, source_url=source_url)
        tier = (
            "reporting"
            if raw_tier in {"media", "reporting"}
            and classified == "reporting"
            else "discovery"
        )
    # Directness and evidence depth are separate claims.  A first-party or
    # official URL does not prove that the snapshot contains the body/post.
    evidence_basis = (
        "title_and_snippet"
        if _text(source.get("summary"), 420)
        else "title_only"
    )

    summary = _text(source.get("summary"), 420) or title
    item: dict[str, Any] = {
        "id": _text(source.get("story_key"), 80),
        "kind": "imported_event",
        "title": title,
        "summary": summary,
        # v1 intentionally has no producer-controlled importance score.
        # Imported priority is derived from validated source directness and
        # publication time instead of trusting an undeclared field.
        "impact": "unknown",
        "source_tier": tier,
        "source_label": _text(
            source.get("source_label") or source.get("source"), 120
        )
        or _host(source_url)
        or "导入来源",
        "source_url": source_url,
        "time_status": (
            "verified" if published is not None else "featured_only"
        ),
        "ai_status": "ineligible",
        "ai_summary_used": False,
        "evidence_basis": evidence_basis,
        "_trusted_evidence_basis": evidence_basis,
        "_raw_title": title,
        "_canonical_url": _canonical_identity_url(
            discovery_metadata.get("original_url")
            or source.get("canonical_url")
            or source_url
        ),
        "_stable_external_id": (
            f"hn:{discovery_metadata['hn_id']}"
            if discovery_metadata.get("hn_id") is not None
            else f"imported_story:{_text(source.get('story_key'), 120).casefold()}"
            if _text(source.get("story_key"), 120)
            else ""
        ),
        "_kol_key": _text(source.get("kol_key"), 80).lower(),
        "_kind_hint": _text(source.get("kind"), 40),
        "_section_hint": section_hint,
    }
    if published is not None:
        item["published_at"] = _iso(published)
    item.update(discovery_metadata)
    cross_tags = source.get("cross_tags")
    if isinstance(cross_tags, list):
        item["_cross_tags_hint"] = list(cross_tags[:6])
    why = _text(source.get("why_it_matters"), 320)
    if why:
        item["why_it_matters"] = why
    assets: list[dict[str, str]] = []
    raw_assets = source.get("assets")
    if isinstance(raw_assets, list):
        for raw in raw_assets:
            if isinstance(raw, Mapping):
                asset_key = _text(raw.get("asset_key"), 40).upper()
            else:
                asset_key = _text(raw, 40).upper()
            if _ASSET_KEY_RE.fullmatch(asset_key):
                assets.append({"asset_key": asset_key})
            if len(assets) >= 6:
                break
    if assets:
        item["assets"] = assets
    count = source.get("source_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
        item["source_count"] = count
        item["related_records"] = count
    # Re-fetching a persisted snapshot does not update the underlying news.
    # Until an upstream revision has its own verified publication timestamp,
    # the public update time is the verified publication time itself.
    evidence_time = published or _datetime(discovery_metadata.get("featured_at"))
    if evidence_time is not None:
        item["last_updated_at"] = _iso(evidence_time)
    disclosed_at = _public_temporal(source.get("disclosed_at"))
    if disclosed_at:
        item["disclosed_at"] = disclosed_at
    effective_at = ""
    for field in ("effective_at", "period_end", "data_as_of"):
        effective_at = _public_temporal(source.get(field))
        if effective_at:
            break
    if effective_at:
        item["effective_at"] = effective_at
    item["rank_reason"] = _rank_reason(item)
    return item


def _imported_candidates(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    if not _imported_snapshot_shape_is_valid(snapshot):
        return []
    assert isinstance(snapshot, Mapping)
    sections = snapshot.get("sections")
    assert isinstance(sections, Mapping)

    output: list[dict[str, Any]] = []
    for key in SECTION_KEYS:
        raw_items = sections.get(key)
        assert isinstance(raw_items, list)  # validated above
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item = _imported_highlight(raw, section_hint=key, now=now)
            if item is not None:
                output.append(item)
    return output


def _imported_snapshot_shape_is_valid(
    snapshot: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    version = snapshot.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return False
    if version != 1:
        return False
    sections = snapshot.get("sections")
    if not isinstance(sections, Mapping):
        return False
    if set(sections) != set(SECTION_KEYS):
        return False
    total = 0
    for key in SECTION_KEYS:
        raw_items = sections.get(key)
        if not isinstance(raw_items, list) or len(raw_items) > 80:
            return False
        total += len(raw_items)
    if total > 300:
        return False
    return True


def _imported_source_coverage_as_of(
    snapshot: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> datetime | None:
    if not _imported_snapshot_shape_is_valid(snapshot):
        return None
    assert isinstance(snapshot, Mapping)
    value = _datetime(snapshot.get("source_as_of"))
    return value if value is not None and value <= now else None


def _highlight_time(item: Mapping[str, Any]) -> datetime:
    return _selection_time(item) or datetime.min.replace(tzinfo=timezone.utc)


def _publication_day_bucket(item: Mapping[str, Any]) -> str:
    anchor = _publication_time(item) or _featured_time(item)
    return anchor.astimezone(BEIJING).date().isoformat() if anchor else ""


def _exact_compatibility_bucket(item: Mapping[str, Any]) -> str:
    signature = _semantic_features(item)
    if signature is not None:
        return "semantic\x1f" + "\x1f".join(signature[1:])
    normalized = _normalized_title(item.get("_raw_title") or item.get("title"))
    return "title\x1f" + normalized if normalized else ""


def _stable_id_bucket(item: Mapping[str, Any]) -> str:
    value = _text(item.get("_stable_external_id"), 200)
    day = _publication_day_bucket(item)
    compatibility = _exact_compatibility_bucket(item)
    return (
        "\x1f".join((value, day, compatibility))
        if value and day and compatibility
        else ""
    )


def _canonical_url_bucket(item: Mapping[str, Any]) -> str:
    value = _text(item.get("_canonical_url"), 2_048)
    day = _publication_day_bucket(item)
    compatibility = _exact_compatibility_bucket(item)
    return (
        "\x1f".join((value, day, compatibility))
        if value and day and compatibility
        else ""
    )


def _highlight_rank(item: Mapping[str, Any]) -> tuple[int, int, float, str, str]:
    return (
        -_TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0),
        -_IMPACT_RANK.get(_text(item.get("impact"), 24).lower(), 0),
        -_highlight_time(item).timestamp(),
        _text(item.get("kind"), 24),
        _text(item.get("id"), 80),
    )


def _primary_source_rank(item: Mapping[str, Any]) -> tuple[int, int, float, str]:
    return (
        -_TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0),
        -_IMPACT_RANK.get(_text(item.get("impact"), 24).lower(), 0),
        -_highlight_time(item).timestamp(),
        _text(item.get("source_url"), 2_048),
    )


def _revision_rank(
    item: Mapping[str, Any],
) -> tuple[int, int, float, int, float, int, str]:
    publication = _highlight_time(item)
    observed = _observed_time(item) or publication
    return (
        # A later low-trust import must never replace a validated native
        # record merely because both resolve to the same canonical URL.  Once
        # source trust and evidence depth are equal, the latest verified
        # publication is the representative revision; provenance only breaks
        # a true timestamp tie.
        -_TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0),
        -_EVIDENCE_RANK.get(
            _text(
                item.get("_trusted_evidence_basis") or item.get("evidence_basis"),
                40,
            ).lower(),
            0,
        ),
        -publication.timestamp(),
        -_REVISION_PROVENANCE_RANK.get(_text(item.get("kind"), 24), 0),
        -observed.timestamp(),
        -_IMPACT_RANK.get(_text(item.get("impact"), 24).lower(), 0),
        _text(item.get("id"), 80),
    )


def _revision_trust_key(item: Mapping[str, Any]) -> tuple[int, int]:
    return (
        _TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0),
        _EVIDENCE_RANK.get(
            _text(
                item.get("_trusted_evidence_basis") or item.get("evidence_basis"),
                40,
            ).lower(),
            0,
        ),
    )


def _story_key(group: list[Mapping[str, Any]]) -> str:
    stable = sorted(
        value
        for item in group
        if (value := _text(item.get("_stable_external_id"), 200))
    )
    urls = sorted(
        value
        for item in group
        if (value := _text(item.get("_canonical_url"), 2_048))
    )
    clusters = sorted(
        value
        for item in group
        if (value := _text(item.get("_ai_cluster_key"), 96))
    )
    if stable:
        day = _publication_day_bucket(group[0])
        compatibility = _exact_compatibility_bucket(group[0])
        identity = "stable\x1f" + stable[0] + "\x1f" + day + "\x1f" + compatibility
    elif urls:
        days = sorted(
            value for item in group if (value := _publication_day_bucket(item))
        )
        compatibility = _exact_compatibility_bucket(group[0])
        identity = (
            "url\x1f"
            + urls[0]
            + "\x1f"
            + (days[0] if days else "")
            + "\x1f"
            + compatibility
        )
    elif clusters:
        identity = "cluster\x1f" + clusters[0]
    else:
        normalized = sorted(
            value
            for item in group
            if (
                value := _normalized_title(
                    item.get("_raw_title") or item.get("title")
                )
            )
        )
        identity = "title\x1f" + (normalized[0] if normalized else "unknown")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"story_{digest[:24]}"


def _merge_duplicate_group(
    group: list[dict[str, Any]],
    *,
    prefer_latest_revision: bool = False,
) -> dict[str, Any]:
    base = dict(
        min(
            group,
            key=_revision_rank if prefer_latest_revision else _primary_source_rank,
        )
    )
    if not prefer_latest_revision:
        highest_impact = max(
            (_impact(item.get("impact")) for item in group),
            key=lambda value: _IMPACT_RANK.get(value, 0),
            default="unknown",
        )
        base["impact"] = highest_impact

    base_trust = _revision_trust_key(base)
    trusted_group = [
        item for item in group if _revision_trust_key(item) == base_trust
    ]
    if prefer_latest_revision and _impact(base.get("impact")) == "unknown":
        base["impact"] = max(
            (_impact(item.get("impact")) for item in trusted_group),
            key=lambda value: _IMPACT_RANK.get(value, 0),
            default="unknown",
        )
    observed = [
        value
        for item in trusted_group
        if (value := _observed_time(item))
    ]
    if observed:
        base["last_updated_at"] = _iso(max(observed))

    reported_counts = [
        count
        for item in group
        if isinstance((count := item.get("source_count")), int)
        and not isinstance(count, bool)
        and count >= 1
    ]
    source_urls = {
        value
        for item in group
        if (value := _text(item.get("_canonical_url"), 2_048))
    }
    if reported_counts or len(group) > 1:
        source_count = max(
            max(reported_counts, default=0),
            len(source_urls),
            len(group),
        )
        base["source_count"] = source_count
        base["related_records"] = source_count

    for field in ("disclosed_at", "effective_at"):
        if field in base:
            continue
        for item in sorted(group, key=_primary_source_rank):
            value = _text(item.get(field), 64)
            if value:
                base[field] = value
                break

    for field in ("_tags", "_cross_tags_hint"):
        merged: list[str] = []
        seen: set[str] = set()
        for item in group:
            values = item.get(field)
            if not isinstance(values, list):
                continue
            for raw in values:
                value = _text(raw, 40)
                if value and value not in seen:
                    seen.add(value)
                    merged.append(value)
        if merged:
            base[field] = merged[:12]
    section_hints = [
        value
        for item in group
        if (value := _text(item.get("_section_hint"), 24)) in SECTION_KEYS
    ]
    if section_hints and not _text(base.get("_section_hint"), 24):
        base["_section_hint"] = section_hints[0]

    base["story_key"] = _story_key(group)
    base["rank_reason"] = _rank_reason(base)
    return base


def _deduplicate_with_stats(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        parents[right_root] = left_root
        return True

    stats = {
        "input_count": len(items),
        "output_count": 0,
        "merged_count": 0,
        "stable_id_matches": 0,
        "canonical_url_matches": 0,
        "ai_cluster_matches": 0,
        "semantic_matches": 0,
    }
    indexes: dict[str, dict[str, int]] = {
        "stable": {},
        "url": {},
        "cluster": {},
    }
    for index, item in enumerate(items):
        exact_values = (
            (
                "stable",
                _stable_id_bucket(item),
                "stable_id_matches",
            ),
            (
                "url",
                _canonical_url_bucket(item),
                "canonical_url_matches",
            ),
            (
                "cluster",
                _ai_cluster_bucket(item),
                "ai_cluster_matches",
            ),
        )
        for namespace, value, stat_key in exact_values:
            if not value:
                continue
            previous = indexes[namespace].get(value)
            if previous is not None and union(index, previous):
                stats[stat_key] += 1
            indexes[namespace][value] = index

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    exact_output: list[dict[str, Any]] = []
    for group in groups.values():
        stable_values = [
            value for item in group if (value := _stable_id_bucket(item))
        ]
        url_values = [
            value for item in group if (value := _canonical_url_bucket(item))
        ]
        shared_revision_identity = (
            len(stable_values) != len(set(stable_values))
            or len(url_values) != len(set(url_values))
        )
        exact_output.append(
            _merge_duplicate_group(
                group,
                prefer_latest_revision=shared_revision_identity,
            )
        )

    # Semantic clustering is a second, linear pass over exact clusters.  Only
    # identical deterministic signatures share a bucket; no pairwise fuzzy
    # union means one marginal title cannot transitively join two stories.
    semantic_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    for item in exact_output:
        signature = _semantic_features(item)
        if signature is None:
            unmatched.append(item)
        else:
            semantic_groups.setdefault(signature, []).append(item)
    output = list(unmatched)
    for group in semantic_groups.values():
        output.append(_merge_duplicate_group(group))
        stats["semantic_matches"] += len(group) - 1
    output.sort(key=_highlight_rank)
    stats["output_count"] = len(output)
    stats["merged_count"] = len(items) - len(output)
    return output, stats


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need the clustered items."""
    return _deduplicate_with_stats(items)[0]


def _trusted_hn_heat(item: Mapping[str, Any], *, now: datetime) -> float:
    """Recompute standalone HN heat from validated primitives.

    ``heat_score`` remains useful display metadata, but is producer-controlled
    and therefore never enters ordering directly.  Curated cross-source
    representatives do not carry a separately verified HN submission instant,
    so their HN metrics are deliberately display-only.
    """
    if (
        _text(item.get("_content_kind"), 32).lower() != "hn_story"
        or item.get("publication_time_verified") is not True
        or _text(item.get("time_status"), 24).lower() != "verified"
    ):
        return 0.0
    published = _publication_time(item)
    rank = _strict_integer(item.get("hn_rank"), minimum=1, maximum=500)
    score = _strict_integer(item.get("hn_score"), minimum=0, maximum=1_000_000)
    comments = _strict_integer(
        item.get("hn_comments"), minimum=0, maximum=1_000_000
    )
    channels = item.get("discovered_via")
    if (
        published is None
        or published > now
        or rank is None
        or score is None
        or comments is None
        or not isinstance(channels, list)
        or any(not isinstance(value, str) for value in channels)
    ):
        return 0.0
    age_hours = (now - published).total_seconds() / 3600.0
    if age_hours < 0:
        return 0.0
    rank_component = 35.0 / (1.0 + 0.08 * (rank - 1))
    score_component = 20.0 * min(1.0, math.log1p(score) / math.log1p(500))
    comment_component = 15.0 * min(
        1.0,
        math.log1p(comments) / math.log1p(250),
    )
    overlap_component = (
        10.0
        if {"hacker_news_top", "hacker_news_best"}.issubset(set(channels))
        else 0.0
    )
    time_decay = math.exp(-math.log(2.0) * age_hours / 8.0)
    engagement = (
        rank_component + score_component + comment_component + overlap_component
    )
    decayed_engagement = engagement * (0.35 + 0.65 * time_decay)
    freshness_component = 20.0 * time_decay
    return round(
        min(
            100.0,
            max(
                0.0,
                decayed_engagement + freshness_component,
            ),
        ),
        1,
    )


def _section_rank(
    item: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[int, int, int, float, float, str]:
    selection_time = _selection_time(item)
    tier = _TIER_RANK.get(_text(item.get("source_tier"), 24).lower(), 0)
    is_fresh = (
        selection_time is not None
        and selection_time <= now
        and (now - selection_time).total_seconds() <= SECTION_STALE_AFTER_SECONDS
    )
    is_current_curated = (
        _text(item.get("_content_kind"), 32).lower() in _DISCOVERY_KINDS
        and selection_time is not None
        and selection_time <= now
        and (now - selection_time).total_seconds() <= EVENT_LOOKBACK_HOURS * 3600
    )
    # Fresh high-trust evidence leads.  A current, strictly validated HN or
    # curated lead comes next so six >90-minute reports cannot hide today's
    # live discovery signal.  Other discovery-only sources remain behind
    # verified reporting, and the section's stale badge still uses 90 minutes.
    freshness_quality = (
        0
        if is_fresh and tier >= 1
        else 1
        if is_current_curated
        else 2
        if tier >= 1
        else 3
        if is_fresh
        else 4
    )
    trusted_heat = _trusted_hn_heat(item, now=now)
    return (
        freshness_quality,
        -tier,
        -_IMPACT_RANK.get(_text(item.get("impact"), 24).lower(), 0),
        -trusted_heat,
        -_highlight_time(item).timestamp(),
        _text(item.get("story_key"), 80),
    )


def _public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    output = {
        key: item[key]
        for key in _PUBLIC_ITEM_FIELDS
        if key in item
    }
    content_kind = _text(item.get("_content_kind"), 32).lower()
    if content_kind in _DISCOVERY_KINDS:
        output["kind"] = content_kind
    return output


def _section_payloads(
    items: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_KEYS}
    for item in items:
        primary, cross_tags = _section_memberships(item)
        item["primary_section"] = primary
        item["cross_tags"] = cross_tags
        grouped[primary].append(item)

    sections: list[dict[str, Any]] = []
    selected: dict[str, list[dict[str, Any]]] = {}
    for key in SECTION_KEYS:
        ranked = sorted(grouped[key], key=lambda item: _section_rank(item, now=now))
        selected[key] = ranked[:MAX_SECTION_ITEMS]
        observed = [
            value
            for item in selected[key]
            if (value := _selection_time(item)) is not None and value <= now
        ]
        as_of = max(observed) if observed else None
        stale = (
            as_of is None
            or (now - as_of).total_seconds() > SECTION_STALE_AFTER_SECONDS
        )
        label, description = SECTION_DEFINITIONS[key]
        sections.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "source_as_of": _iso(as_of) if as_of is not None else None,
                "stale": stale,
                "status": "empty" if not ranked else "stale" if stale else "fresh",
                "verified_count": sum(
                    item.get("time_status") == "verified" for item in ranked
                ),
                "total_count": len(ranked),
                "items": [_public_item(item) for item in selected[key]],
            }
        )
    return sections, selected


def _top_highlights(
    selected: Mapping[str, list[dict[str, Any]]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Select Top 5 from section contents with a one-per-section first pass."""
    output: list[dict[str, Any]] = []
    used: set[str] = set()
    heads = [
        items[0]
        for key in SECTION_KEYS
        if (items := selected.get(key))
    ]
    for item in sorted(heads, key=lambda item: _section_rank(item, now=now)):
        story_key = _text(item.get("story_key"), 80)
        if story_key and story_key not in used:
            used.add(story_key)
            output.append(item)
        if len(output) >= MAX_HIGHLIGHTS:
            return output
    remainder = sorted(
        (
            item
            for key in SECTION_KEYS
            for item in selected.get(key, [])[1:]
        ),
        key=lambda item: _section_rank(item, now=now),
    )
    for item in remainder:
        story_key = _text(item.get("story_key"), 80)
        if story_key and story_key not in used:
            used.add(story_key)
            output.append(item)
        if len(output) >= MAX_HIGHLIGHTS:
            break
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
        data_as_of = _public_temporal(raw.get("data_as_of"))
        if data_as_of:
            item["data_as_of"] = data_as_of
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
    watchpoints: list[Mapping[str, Any]],
    *,
    now: datetime,
) -> datetime | None:
    values: list[datetime] = []

    def append_if_observed(value: Any) -> None:
        parsed = _datetime(value)
        if parsed is not None and parsed <= now:
            values.append(parsed)

    for item in items:
        selected_at = _selection_time(item)
        if selected_at is not None and selected_at <= now:
            values.append(selected_at)
    if isinstance(macro, Mapping):
        for field in ("timestamp", "created_at"):
            append_if_observed(macro.get(field))
    for item in watchpoints:
        # Date-only disclosures stay visible in the card, but are not coerced
        # into an invented instant for a global freshness promise.
        append_if_observed(item.get("data_as_of"))
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
    imported_snapshot: Mapping[str, Any] | None = None,
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
    candidates.extend(_imported_candidates(imported_snapshot, now=current))
    ranked, dedup_stats = _deduplicate_with_stats(candidates)
    sections, selected_by_section = _section_payloads(ranked, now=current)
    highlight_items = _top_highlights(selected_by_section, now=current)
    highlights = [_public_item(item) for item in highlight_items]
    public_ranked = [_public_item(item) for item in ranked]
    firsthand = sorted(
        (
            item
            for item in public_ranked
            if item.get("source_tier") in {"official", "first_party"}
        ),
        key=lambda item: _section_rank(item, now=current),
    )[:MAX_FIRSTHAND]
    watchpoints = _watchpoints(decision_record)
    source_coverage_as_of = _imported_source_coverage_as_of(
        imported_snapshot,
        now=current,
    )
    displayed_items_by_story: dict[str, Mapping[str, Any]] = {}
    for section_items in selected_by_section.values():
        for item in section_items:
            displayed_items_by_story[_text(item.get("story_key"), 80)] = item
    for item in firsthand:
        displayed_items_by_story[_text(item.get("story_key"), 80)] = item
    as_of = _source_as_of(
        list(displayed_items_by_story.values()),
        macro,
        watchpoints,
        now=current,
    )
    available = bool(ranked or macro or watchpoints or source_coverage_as_of)
    age_seconds = (
        max(0.0, (current - as_of).total_seconds()) if as_of is not None else None
    )
    coverage_age_seconds = (
        max(0.0, (current - source_coverage_as_of).total_seconds())
        if source_coverage_as_of is not None
        else None
    )
    public_as_of = _iso(as_of) if as_of is not None else None
    return {
        "available": available,
        "date": current.astimezone(BEIJING).date().isoformat(),
        "edition": edition,
        "edition_label": edition_label,
        "generated_at": _iso(current),
        "coverage_window_hours": EVENT_LOOKBACK_HOURS,
        # The historical OpenClaw 10:00 job is only a migrated contract, not
        # an active scheduler.  Do not present a guessed refresh promise.
        "next_refresh_at": None,
        "refresh_schedule_status": "unconfigured",
        # ``source_as_of`` remains as a compatibility alias for the latest
        # displayed evidence.  The imported batch coverage has a separate
        # field so an empty successful scan is not confused with cron failure.
        "source_as_of": public_as_of,
        "content_as_of": public_as_of,
        "source_coverage_as_of": (
            _iso(source_coverage_as_of)
            if source_coverage_as_of is not None
            else None
        ),
        "source_coverage_stale": (
            coverage_age_seconds > STALE_AFTER_SECONDS
            if coverage_age_seconds is not None
            else None
        ),
        "stale": age_seconds is None or age_seconds > STALE_AFTER_SECONDS,
        "coverage": _coverage(public_ranked),
        "dedup_stats": dedup_stats,
        "lead": _lead(highlights, macro, safe_history),
        "highlights": highlights,
        "firsthand": firsthand,
        "watchpoints": watchpoints,
        "sections": sections,
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "MAX_FIRSTHAND",
    "MAX_HIGHLIGHTS",
    "MAX_SECTION_ITEMS",
    "MAX_WATCHPOINTS",
    "SECTION_KEYS",
    "build_latest_briefing",
    "classify_source",
    "edition_for",
]
