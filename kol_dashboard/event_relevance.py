"""Deterministic KOL attribution, finance relevance and impact rules.

The collector and read paths use the same pure functions so a search-engine
hit is never confused with proof that a person (or their company) appears in
the result.  This module intentionally has no dependency on ``kol_tracker`` or
the database, which keeps it safe to reuse for historical rows and before an
LLM call.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

try:
    from kol_dashboard.content_quality import (
        has_substantive_social_text,
        is_event_content_eligible,
    )
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    from content_quality import (  # type: ignore
        has_substantive_social_text,
        is_event_content_eligible,
    )


CLASSIFIER_VERSION = "kol-event-relevance-v1"

# Public, dependency-free directory used by both the collector and API.  Search
# terms and source credentials remain private to the collector configuration.
KOL_DIRECTORY: dict[str, dict[str, str]] = {
    "trump": {"name": "Donald Trump", "name_cn": "特朗普", "category": "policy"},
    "musk": {"name": "Elon Musk", "name_cn": "马斯克", "category": "technology"},
    "buffett": {"name": "Warren Buffett", "name_cn": "巴菲特", "category": "investor"},
    "dalio": {"name": "Ray Dalio", "name_cn": "瑞达利欧", "category": "investor"},
    "duanyongping": {"name": "Duan Yongping", "name_cn": "段永平", "category": "investor"},
    "danbin": {"name": "但斌", "name_cn": "但斌", "category": "investor"},
    "renzeping": {"name": "任泽平", "name_cn": "任泽平", "category": "macro"},
    "huangrenxun": {"name": "Jensen Huang", "name_cn": "黄仁勋", "category": "technology"},
    "suzifeng": {"name": "Lisa Su", "name_cn": "苏姿丰", "category": "technology"},
    "altman": {"name": "Sam Altman", "name_cn": "Sam Altman", "category": "technology"},
    "zuckerberg": {"name": "Mark Zuckerberg", "name_cn": "扎克伯格", "category": "technology"},
    "powell": {"name": "Jerome Powell", "name_cn": "鲍威尔", "category": "macro"},
    "pangongsheng": {"name": "Pan Gongsheng", "name_cn": "潘功胜", "category": "macro"},
    "dimon": {"name": "Jamie Dimon", "name_cn": "杰米·戴蒙", "category": "risk"},
    "burry": {"name": "Michael Burry", "name_cn": "迈克尔·伯里", "category": "risk"},
    "howardmarks": {"name": "Howard Marks", "name_cn": "霍华德·马克斯", "category": "risk"},
    "cathiewood": {"name": "Cathie Wood", "name_cn": "木头姐", "category": "growth"},
    "serenity": {"name": "Serenity", "name_cn": "Serenity", "category": "trader"},
}

# Only aliases that unambiguously identify the person are listed here.  A
# company/institution match is useful intelligence, but is deliberately
# reported as ``company_mention`` rather than implying that the KOL spoke.
_PERSON_ALIASES: dict[str, tuple[str, ...]] = {
    "trump": (
        "Donald Trump", "President Trump", "Trump", "特朗普", "川普",
    ),
    "musk": ("Elon Musk", "Musk", "马斯克"),
    "buffett": ("Warren Buffett", "Buffett", "巴菲特"),
    "dalio": ("Ray Dalio", "Dalio", "瑞达利欧"),
    "duanyongping": ("Duan Yongping", "段永平"),
    "danbin": ("但斌",),
    "renzeping": ("任泽平",),
    "huangrenxun": (
        "Jensen Huang", "Huang Renxun", "黄仁勋", "NVIDIA CEO",
    ),
    "suzifeng": ("Lisa Su", "苏姿丰", "AMD CEO"),
    "altman": ("Sam Altman", "Altman", "奥尔特曼", "OpenAI CEO"),
    "zuckerberg": (
        "Mark Zuckerberg", "Zuckerberg", "扎克伯格", "Meta CEO",
    ),
    "powell": (
        "Jerome Powell", "Powell", "鲍威尔", "Fed Chair",
        "Federal Reserve Chair",
    ),
    "pangongsheng": ("Pan Gongsheng", "潘功胜", "中国人民银行行长"),
    "dimon": ("Jamie Dimon", "Dimon", "杰米·戴蒙", "JPMorgan CEO"),
    "burry": ("Michael Burry", "迈克尔·伯里"),
    "howardmarks": ("Howard Marks", "霍华德·马克斯"),
    "cathiewood": ("Cathie Wood", "木头姐"),
    "serenity": ("Serenity", "aleabitoreddit", "@aleabitoreddit"),
}

_AFFILIATED_COMPANIES: dict[str, tuple[str, ...]] = {
    "trump": ("Trump Media", "TMTG", "$DJT"),
    "musk": ("Tesla", "特斯拉", "SpaceX", "xAI"),
    "buffett": ("Berkshire Hathaway", "Berkshire", "伯克希尔"),
    "dalio": ("Bridgewater Associates", "Bridgewater", "桥水基金", "桥水"),
    "duanyongping": ("BBK", "步步高", "OPPO", "vivo"),
    "danbin": ("东方港湾", "Oriental Harbor"),
    "huangrenxun": ("NVIDIA", "英伟达"),
    "suzifeng": ("AMD", "Advanced Micro Devices", "超威半导体"),
    "altman": ("OpenAI", "开放人工智能"),
    "zuckerberg": ("Meta Platforms", "Facebook", "脸书", "Meta"),
    "powell": ("Federal Reserve", "the Fed", "美联储"),
    "pangongsheng": (
        "People's Bank of China", "Peoples Bank of China", "PBOC",
        "中国人民银行", "央行",
    ),
    "dimon": ("JPMorgan Chase", "JPMorgan", "摩根大通"),
    "burry": ("Scion Asset Management", "Scion"),
    "howardmarks": ("Oaktree Capital", "Oaktree", "橡树资本"),
    "cathiewood": ("ARK Invest", "ARK Investment", "方舟投资"),
}

_BASELINE_IMPACT = {
    "trump": "high",
    "musk": "high",
    "buffett": "medium",
    "dalio": "medium",
    "duanyongping": "medium",
    "danbin": "low",
    "renzeping": "low",
    "huangrenxun": "high",
    "suzifeng": "high",
    "altman": "high",
    "zuckerberg": "high",
    "powell": "high",
    "pangongsheng": "high",
    "dimon": "high",
    "burry": "medium",
    "howardmarks": "medium",
    "cathiewood": "medium",
    "serenity": "medium",
}

_DIRECT_HANDLES = {
    "trump": frozenset({"realdonaldtrump"}),
    "musk": frozenset({"elonmusk"}),
    "serenity": frozenset({"aleabitoreddit"}),
}

_DIRECT_SOURCE_RE = re.compile(r"^(?:truth\s+social|x)\s+@", re.I)
_TICKER_RE = re.compile(r"(?<![\w$])\$[A-Z]{1,5}\b")
_META_COMPANY_RE = re.compile(
    r"(?<![A-Za-z0-9_])meta(?![A-Za-z0-9_]|[-\s]?(?:analysis|analytic|"
    r"review|study|data|model|learning)\b)",
    re.I,
)


def _text(event: Mapping[str, Any]) -> str:
    return unicodedata.normalize(
        "NFKC",
        " ".join(
            str(event.get(field) or "")
            for field in ("title", "snippet")
        ),
    ).strip()


@lru_cache(maxsize=256)
def _term_pattern(term: str) -> re.Pattern[str]:
    normalized = unicodedata.normalize("NFKC", term).strip()
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    if re.fullmatch(r"[A-Za-z0-9@$.'’&\- ]+", normalized):
        left = r"(?<![A-Za-z0-9_])" if normalized[0].isalnum() else ""
        right = r"(?![A-Za-z0-9_])" if normalized[-1].isalnum() else ""
        return re.compile(left + escaped + right, re.I)
    return re.compile(escaped, re.I)


def _first_match(text: str, terms: Sequence[str]) -> str | None:
    for term in sorted(set(terms), key=len, reverse=True):
        pattern = _META_COMPANY_RE if term.casefold() == "meta" else _term_pattern(term)
        if pattern.search(text):
            return term
    return None


def _first_person_match(
    text: str,
    kol_key: str,
    terms: Sequence[str],
) -> str | None:
    """Match person aliases while rejecting common surname homonyms."""
    for term in sorted(set(terms), key=len, reverse=True):
        pattern = _term_pattern(term)
        for match in pattern.finditer(text):
            matched = match.group(0)
            if (
                kol_key == "trump"
                and term.casefold() == "trump"
            ):
                window = text[max(0, match.start() - 32): match.end() + 32]
                verb_context = re.search(
                    r"\b(?:stocks?|equities|shares?|markets?|returns?|earnings|"
                    r"profits?|cash|gold|oil|growth|value|performance|"
                    r"fundamentals)"
                    r"(?:\s+(?:may|might|can|could|will|would|should|still|"
                    r"easily|clearly|consistently|often|usually|historically|"
                    r"probably|likely|continue(?:s|d)?|are|is|set|poised|"
                    r"expected|to)){0,4}\s+trump\s+"
                    r"(?:bonds?|treasur(?:y|ies)|"
                    r"peers?|cash|gold|oil|growth|value|returns?|risk|quality|"
                    r"politics|polls?|fundamentals|fixed\s+income|other\s+assets?)\b",
                    window,
                    re.I,
                )
                if matched not in {"Trump", "TRUMP"} or verb_context:
                    # ``trump`` also means outperform. Headline/title casing
                    # is not evidence of a person, so use the noun context.
                    continue
            if kol_key == "powell" and term.casefold() == "powell":
                suffix = text[match.end(): match.end() + 32]
                if re.match(
                    r"\s+(?:Industries|Company|Corp(?:oration)?|Inc\.?|LLC)\b",
                    suffix,
                    re.I,
                ):
                    continue
            return term
    return None


def _identity_terms(event: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    key = str(event.get("kol_key") or "").strip().casefold()
    people = list(_PERSON_ALIASES.get(key, ()))
    companies = list(_AFFILIATED_COMPANIES.get(key, ()))

    # Unknown/new KOLs still get a conservative full-name match without
    # guessing that an arbitrary surname or company belongs to them.
    for field in ("kol_name", "kol_name_cn"):
        value = str(event.get(field) or "").strip()
        if len(value) >= 2 and value.casefold() not in {
            item.casefold() for item in people
        }:
            people.append(value)
    return people, companies


def is_direct_source(source: Any, url: Any) -> bool:
    """Return whether source and URL together identify a first-party post."""
    source_text = str(source or "").strip()
    if not _DIRECT_SOURCE_RE.match(source_text):
        return False
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if source_text.casefold().startswith("truth social @"):
        return host in {"truthsocial.com", "www.truthsocial.com"} and "/@" in path
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and (
        "/status/" in path
    )


def _direct_handle(source: Any, url: Any) -> str:
    source_match = re.search(r"@([A-Za-z0-9_]{1,64})", str(source or ""))
    if source_match:
        return source_match.group(1).casefold()
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return ""
    url_match = re.search(r"/@?([A-Za-z0-9_]{1,64})/", path)
    return url_match.group(1).casefold() if url_match else ""


def _is_owned_direct_source(event: Mapping[str, Any]) -> bool:
    source = event.get("source")
    url = (
        event.get("source_url")
        or event.get("canonical_url")
        or event.get("url")
    )
    if not is_direct_source(source, url):
        return False
    key = str(event.get("kol_key") or "").strip().casefold()
    expected = set(_DIRECT_HANDLES.get(key, ()))
    configured = str(event.get("handle") or "").strip().lstrip("@").casefold()
    if configured:
        expected.add(configured)
    return bool(expected) and _direct_handle(source, url) in expected


def is_owned_direct_source(event: Mapping[str, Any]) -> bool:
    """Return whether a sighting is a verified first-party KOL source."""
    return _is_owned_direct_source(event)


_FINANCE_RE = re.compile(
    r"(?:"
    r"\b(?:stocks?|equities|bonds?|treasur(?:y|ies)|"
    r"invest(?:or|ors|ing|ment|ments)?|portfolio|ETF|IPO|"
    r"earnings|revenue|profits?|guidance|valuation|dividend|buyback|"
    r"capex|capital expenditure|cash flow|fiscal|monetary|GDP|econom(?:y|ic)|"
    r"recession|inflation|deflation|interest rates?|rate cut|rate hike|"
    r"central bank|tariffs?|trade war|export controls?|sanctions?|"
    r"commodit(?:y|ies)|bitcoin|ethereum|crypto|forex|currency|"
    r"AI spending|AI investment|supply chain)\b|"
    r"A股|港股|美股|股票|股份|股价|市场|债券|国债|收益率|基金|投资|"
    r"大宗商品|比特币|加密货币|营收|利润|业绩|指引|"
    r"估值|分红|回购|资本开支|经济|财政|货币政策|央行|美联储|国内生产总值|"
    r"衰退|通胀|通缩|降息|加息|利率|关税|贸易战|出口管制|制裁|"
    r"供应链"
    r")",
    re.I,
)

_FINANCE_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"(?:stock|company|public|listed|common|preferred)\s+shares?|"
    r"shares?\s+(?:rise|fall|jump|drop|plunge|rally|trade|trading|price|"
    r"buyback|offering)|"
    r"(?:financial|capital|stock|bond|equity|crypto|commodity|global)\s+markets?|"
    r"markets?\s+(?:rally|sell[ -]?off|outlook|volatility|liquidity|risk)|"
    r"(?:mutual|hedge|index|pension|sovereign|investment|money market)\s+funds?|"
    r"(?:asset allocation|asset management|assets under management|"
    r"financial assets?|asset classes?|asset prices?)|"
    r"(?:crude\s+oil|Brent|WTI)|"
    r"(?:oil|gold)\s+(?:prices?|futures?|markets?|demand|production|reserves?)|"
    r"(?:customer|chip|semiconductor|vehicle|server|export)\s+orders?|"
    r"orders?\s+(?:backlog|growth|decline|guidance)|"
    r"(?:AI|chip|semiconductor|GPU|data center|vehicle|consumer|energy|oil)\s+demand|"
    r"demand\s+(?:forecast|outlook|growth|slowdown)|"
    r"(?:chip|semiconductor|vehicle|factory|oil|gas|industrial)\s+production|"
    r"(?:semiconductor|chip|GPU|data center)\s+(?:sales|revenue|demand|"
    r"shipments?|exports?|shortage|investment|capex)|"
    r"(?:sales|revenue|demand|shipments?|exports?|investment|capex)\s+"
    r"(?:for\s+)?(?:semiconductors?|chips?|GPUs?|data centers?)"
    r")\b|"
    r"股票份额|股份价格|资本市场|金融市场|股票市场|资产配置|资产管理|"
    r"资产价格|基金产品|对冲基金|原油价格|黄金价格|原油期货|黄金期货|"
    r"共同基金|客户订单|订单积压|芯片需求|算力需求|汽车产量|芯片产量",
    re.I,
)

_GEO_CONTEXT_RE = re.compile(
    r"\b(?:Iran|Israel|Ukraine|Russia|Taiwan|China|Middle East|NATO|"
    r"military|nuclear|missile|invasion|geopolitic(?:al|s)?|armed conflict)\b|"
    r"伊朗|以色列|乌克兰|俄罗斯|台海|台湾|中东|北约|军事|核武|导弹|入侵|地缘",
    re.I,
)
_WAR_RE = re.compile(r"\bwar\b|战争|战事|冲突", re.I)
_NON_GEO_WAR_RE = re.compile(
    r"\b(?:culture|console|streaming|browser|format|talent|price)\s+war\b|"
    r"文化战争|价格战",
    re.I,
)
_PHYSICAL_CRASH_RE = re.compile(
    r"\b(?:train|tractor|car|vehicle|truck|bus|plane|aircraft|helicopter|"
    r"road|highway|rollover|collision|traffic)\b.{0,45}\b(?:crash|accident|"
    r"collision|strikes?|hit)\b|"
    r"\b(?:crash|accident|collision)\b.{0,45}\b(?:train|tractor|car|vehicle|"
    r"truck|bus|plane|aircraft|helicopter|road|highway|traffic)\b|"
    r"(?:火车|拖拉机|汽车|车辆|卡车|公交车|飞机|直升机|道路|公路).{0,20}"
    r"(?:事故|相撞|撞击|撞上|车祸|空难)|"
    r"(?:事故|相撞|撞击|车祸|空难).{0,20}"
    r"(?:火车|拖拉机|汽车|车辆|卡车|公交车|飞机|直升机|道路|公路)",
    re.I,
)

_MARKET_SHOCK_RE = re.compile(
    r"(?:\b(?:stocks?|shares?|equities|markets?|crypto|bitcoin|bonds?|"
    r"prices?|assets?)\b.{0,35}\b(?:crash(?:es|ed|ing)?|collapse(?:s|d)?|"
    r"plunge(?:s|d)?|sell[ -]?off|panic)\b|"
    r"\b(?:crash(?:es|ed|ing)?|collapse(?:s|d)?|plunge(?:s|d)?|sell[ -]?off|"
    r"panic)\b.{0,35}\b(?:stocks?|shares?|equities|markets?|crypto|bitcoin|"
    r"bonds?|prices?|assets?)\b|"
    r"(?:股市|股票|股价|市场|加密货币|比特币|债券|资产).{0,20}"
    r"(?:崩盘|暴跌|崩溃|恐慌|抛售)|"
    r"(?:崩盘|暴跌|崩溃|恐慌|抛售).{0,20}(?:股市|股票|股价|市场|"
    r"加密货币|比特币|债券|资产))",
    re.I,
)
_BUBBLE_CRISIS_RE = re.compile(
    r"(?:\b(?:financial|banking|debt|credit|liquidity|economic|sovereign|"
    r"housing|property|stock|market|crypto|AI|tech)\b.{0,30}\b"
    r"(?:crisis|bubble|panic|collapse)\b|"
    r"\b(?:crisis|bubble|panic|collapse)\b.{0,30}\b(?:financial|banking|"
    r"debt|credit|liquidity|economic|sovereign|housing|property|stock|market|"
    r"crypto|AI|tech)\b|"
    r"(?:金融|银行|债务|信贷|流动性|经济|主权|房地产|楼市|股市|市场|加密|AI|"
    r"科技).{0,20}(?:危机|泡沫|恐慌|崩溃)|"
    r"(?:危机|泡沫|恐慌|崩溃).{0,20}(?:金融|银行|债务|信贷|流动性|经济|"
    r"主权|房地产|楼市|股市|市场|加密|AI|科技))",
    re.I,
)
_RATE_DECISION_RE = re.compile(
    r"\b(?:Fed|Federal Reserve|central bank)\b.{0,45}\b(?:cuts?|raises?|"
    r"hikes?|reduces?)\b.{0,20}\b(?:rates?|basis points?|bps)\b|"
    r"\b(?:cuts?|raises?|hikes?|reduces?)\b.{0,20}\b(?:interest rates?|"
    r"basis points?|bps)\b|"
    r"(?:美联储|央行).{0,30}(?:降息|加息)|(?:降息|加息).{0,30}(?:基点|利率)",
    re.I,
)
_TARIFF_ACTION_RE = re.compile(
    r"\b(?:impose|announce|raise|increase|double|slash|remove|delay)s?\b"
    r".{0,40}\btariffs?\b|\b\d{1,3}%\s+tariffs?\b|"
    r"(?:宣布|加征|提高|取消|暂缓).{0,25}关税|\d{1,3}%[^。；,，]{0,12}关税",
    re.I,
)
_SYSTEMIC_RE = re.compile(
    r"\b(?:recession|depression|sovereign default|bank run|systemic risk)\b|"
    r"经济衰退|经济萧条|主权违约|银行挤兑|系统性风险",
    re.I,
)
_MEDIUM_RE = re.compile(
    r"\b(?:warning|warns?|alert|caution|risk|slowdown|inflation|deflation|"
    r"tariffs?|trade war|shortage|layoffs?)\b|"
    r"预警|警告|风险|放缓|减速|通胀|通缩|关税|贸易战|短缺|裁员",
    re.I,
)
_CORPORATE_ACTION_RE = re.compile(
    r"\b(?:launch(?:es|ed|ing)?|unveil(?:s|ed)?|announce(?:s|d)?|"
    r"release(?:s|d)?|describ(?:e|es|ed|ing)|present(?:s|ed|ing)?|"
    r"demonstrat(?:e|es|ed|ing)|partner(?:s|ed|ship)?|"
    r"acquire(?:s|d)?|acquisition|"
    r"merge(?:s|d|r)?|merger|"
    r"(?:sign(?:s|ed)?|win(?:s)?|won)\b.{0,30}\b(?:a\s+)?contract|"
    r"expand(?:s|ed|ing)?|expansion|restructur(?:e|es|ed|ing)|"
    r"delay(?:s|ed|ing)?|recall(?:s|ed|ing)?|"
    r"layoffs?|lays?\s+off|appoint(?:s|ed)?|"
    r"faces?\s+(?:a\s+)?(?:lawsuit|probe|investigation)|"
    r"sued|investigat(?:e|es|ed|ing))\b|"
    r"发布|推出|宣布|介绍|展示|上市|合作|收购|并购|"
    r"(?:签署|赢得|获得).{0,8}合同|扩产|扩张|推迟|延期|召回|降价|"
    r"重组|裁员|任命|遭到.{0,8}(?:诉讼|调查)|被(?:起诉|调查)",
    re.I,
)
_TRADE_ACTION_RE = re.compile(
    r"\b(?:buy|buys|bought|sell|sells|sold|add|adds|added|trim|trims|"
    r"trimmed|reduce|reduces|reduced|increase|increases|increased|"
    r"short|shorts|shorted|long|longs|longed|exit|exits|exited)\b|"
    r"买入|卖出|增持|加仓|减持|减仓|做多|做空|清仓|退出",
    re.I,
)
_BUSINESS_OBJECT_RE = re.compile(
    r"\b(?:product|platform|model|processor|chip|GPU|semiconductor|factory|"
    r"data center|software|service|startup|company|partnership|acquisition|"
    r"merger|contract|stake|position|holding|exposure|shares?|stock|"
    r"OpenAI|GPT[- ]?\d*|Grok(?:\s+\d+)?|NVIDIA|AMD|Meta|Tesla|SpaceX|xAI|"
    r"Apple|NVDA|TSLA|AAPL|MSFT|GOOGL|GOOG|AMZN|DJT|SPY|QQQ|BTC|ETH)\b|"
    r"产品|平台|模型|处理器|芯片|算力|半导体|工厂|数据中心|软件|服务|"
    r"公司|合作|收购|并购|合同|股份|持仓|仓位|敞口|英伟达|特斯拉|苹果",
    re.I,
)
_FINANCIAL_POSITION_OBJECT_RE = re.compile(
    r"\b(?:stake|position|holding|exposure|shares?|stocks?|portfolio|"
    r"NVDA|TSLA|AMD|META|AAPL|MSFT|GOOGL|GOOG|AMZN|DJT|SPY|QQQ|BTC|ETH)\b|"
    r"股份|股票|持仓|仓位|敞口|投资组合",
    re.I,
)
_CORPORATE_EVENT_RE = re.compile(
    r"\b(?:funding|financing|capital raising|fundraising)\s+(?:round|deal)|"
    r"\b(?:raises?|raised|secures?|secured)\b.{0,18}\b(?:funding|financing|capital)\b|"
    r"融资轮|融资交易|完成.{0,8}融资|获得.{0,8}融资|融资",
    re.I,
)
_BUSINESS_METRIC_RE = re.compile(
    r"\b(?:shipments?|deliveries|delivery estimates?|margins?|backlog|bookings|"
    r"(?:vehicle|company|product|unit)\s+sales|market share|prices?)\b.{0,24}\b"
    r"(?:ramp(?:s|ed|ing)?|rise|rises|rose|grow|grows|grew|gain|gains|gained|"
    r"improve|improves|improved|fall|falls|fell|decline|declines|declined|"
    r"slow|slows|slowed|miss|misses|missed|cut|cuts|cutting)\b|"
    r"\b(?:ramp(?:s|ed|ing)?|rise|rises|rose|grow|grows|grew|improve|improves|"
    r"improved|gain|gains|gained|fall|falls|fell|decline|declines|declined|"
    r"miss|misses|missed|cut|cuts|cutting)\b.{0,24}\b"
    r"(?:shipments?|deliveries|delivery estimates?|margins?|backlog|bookings|"
    r"(?:vehicle|company|product|unit)\s+sales|market share|prices?)\b|"
    r"(?:出货|交付|交付预期|毛利率|利润率|订单积压|预订量|销量|市占率|价格)"
    r".{0,12}(?:增长|上升|提升|改善|加速|下滑|下降|放缓|不及|未达|降价)|"
    r"(?:增长|上升|提升|改善|加速|下滑|下降|放缓|不及|未达|降价).{0,12}"
    r"(?:出货|交付|交付预期|毛利率|利润率|订单积压|预订量|销量|市占率|价格)",
    re.I,
)
_RATE_HOLD_RE = re.compile(
    r"\b(?:hold|holds|held|keep|keeps|kept|leave|leaves|left)\b.{0,20}"
    r"\b(?:interest\s+)?rates?\b.{0,12}\b(?:steady|unchanged)\b|"
    r"\b(?:interest\s+)?rates?\b.{0,12}\b(?:remain|remains|stay|stays)\b"
    r".{0,8}\b(?:steady|unchanged)\b|"
    r"维持.{0,10}利率.{0,6}不变|利率.{0,10}(?:维持不变|保持不变|按兵不动)",
    re.I,
)
_NON_BUSINESS_CONTEXT_RE = re.compile(
    r"\b(?:oil\s+painting|personal\s+assets?|book\s+sales?|charity\s+platform|"
    r"museum|exhibition|baby|newborn|custody|divorce|toy|pie|lunch|"
    r"daughter|son|birthday|wedding|school|teaching|heart\s+rate|"
    r"(?:buys?|bought|wears?|gifts?)\s+(?:an?\s+)?Apple\s+Watch|"
    r"holiday\s+(?:party|dinner|video)|family\s+(?:photo|photos|dinner))\b|"
    r"油画|个人资产|离婚|监护权|图书销量|慈善平台|博物馆|展览|婴儿|"
    r"新生儿|玩具|馅饼|午餐|女儿|儿子|生日|婚礼|学校|教学|心率|"
    r"节日视频|家庭照片|家庭聚餐",
    re.I,
)
_ACCIDENT_BUSINESS_CONSEQUENCE_RE = re.compile(
    r"\b(?:stocks?|shares?|markets?|earnings|revenue|production|operations?|"
    r"supply chain|factory|plant|shipment|sales)\b|"
    r"股票|股价|市场|业绩|营收|生产|运营|供应链|工厂|出货|销量",
    re.I,
)
_AMBIGUOUS_FINANCE_USAGE_RE = re.compile(
    r"\bbonds?\s+with\b|"
    r"\bstocks?\s+(?:up\s+on|(?:the\s+)?(?:pantry|shelves?|store|warehouse))\b|"
    r"\bportfolio\s+of\s+(?:watercolou?r|oil|art|paintings?|photos?|photographs?)\b|"
    r"\beconomy\s+class\b|"
    r"菜市场|人才市场|农贸市场|跳蚤市场|经济舱|基金会",
    re.I,
)


def _finance_semantic_text(text: str) -> str:
    """Remove compound phrases whose finance-looking token has another sense."""
    return _AMBIGUOUS_FINANCE_USAGE_RE.sub(" ", text)


def _has_business_signal(
    text: str,
    attribution_basis: str,
    kol_key: str,
) -> bool:
    """Require a real action/metric, not merely a business-looking noun."""
    if _NON_BUSINESS_CONTEXT_RE.search(text):
        return False
    metric = bool(_BUSINESS_METRIC_RE.search(text))
    corporate_action = bool(_CORPORATE_ACTION_RE.search(text))
    corporate_event = bool(_CORPORATE_EVENT_RE.search(text))
    trade_action = bool(_TRADE_ACTION_RE.search(text))
    business_object = bool(_BUSINESS_OBJECT_RE.search(text))
    financial_object = bool(_FINANCIAL_POSITION_OBJECT_RE.search(text))
    if metric or corporate_event:
        return True
    if attribution_basis == "company_mention":
        return corporate_action or (trade_action and business_object)
    if attribution_basis == "direct_source":
        return (corporate_action or trade_action) and business_object
    if attribution_basis == "person_mention":
        if corporate_action and business_object:
            return True
        category = KOL_DIRECTORY.get(kol_key, {}).get("category", "")
        return trade_action and (
            financial_object
            or (
                category in {"investor", "risk", "growth", "trader"}
                and business_object
            )
        )
    return False


def matches_kol_entity(event: Mapping[str, Any]) -> tuple[bool, str | None, str]:
    """Return match, alias and attribution basis for a KOL news item."""
    text = _text(event)
    people, companies = _identity_terms(event)
    key = str(event.get("kol_key") or "").strip().casefold()
    person = _first_person_match(text, key, people)
    if person:
        return True, person, "person_mention"
    company = _first_match(text, companies)
    if company:
        return True, company, "company_mention"
    return False, None, "missing"


def is_finance_relevant(
    event: Mapping[str, Any], *, attribution_basis: str = "missing"
) -> bool:
    """Classify asset, business, macro or geopolitical relevance."""
    text = _text(event)
    if not text:
        return False
    finance_text = _finance_semantic_text(text)
    if (
        _PHYSICAL_CRASH_RE.search(text)
        and not _ACCIDENT_BUSINESS_CONSEQUENCE_RE.search(text)
    ):
        return False
    if (
        _TICKER_RE.search(finance_text)
        or _FINANCE_RE.search(finance_text)
        or _FINANCE_CONTEXT_RE.search(finance_text)
        or _MARKET_SHOCK_RE.search(finance_text)
    ):
        return True
    if _BUBBLE_CRISIS_RE.search(finance_text):
        return True
    key = str(event.get("kol_key") or "").strip().casefold()
    if (
        not _NON_BUSINESS_CONTEXT_RE.search(text)
        and _RATE_HOLD_RE.search(text)
    ) or _has_business_signal(text, attribution_basis, key):
        return True
    # Company identity establishes attribution, not finance relevance by
    # itself.  Requiring a product/business/policy action keeps social trivia
    # such as a holiday party out of intelligence and LLM pipelines.
    if (
        key == "trump"
        and _is_owned_direct_source(event)
        and _WAR_RE.search(text)
        and not _NON_GEO_WAR_RE.search(text)
    ):
        # A terse first-party signal from a sitting policy figure supplies the
        # geopolitical context that the one-word post itself omits.
        return True
    return bool(
        _WAR_RE.search(text)
        and _GEO_CONTEXT_RE.search(text)
        and not _NON_GEO_WAR_RE.search(text)
    )


def classify_rule_impact(
    event: Mapping[str, Any], *, finance_relevant: bool | None = None
) -> str:
    """Return a contextual impact level without trusting stored old labels."""
    text = _text(event)
    relevant = (
        is_finance_relevant(event)
        if finance_relevant is None
        else bool(finance_relevant)
    )
    if not relevant:
        return "low"

    physical_crash = bool(_PHYSICAL_CRASH_RE.search(text))
    geo_war = bool(
        _WAR_RE.search(text)
        and _GEO_CONTEXT_RE.search(text)
        and not _NON_GEO_WAR_RE.search(text)
    )
    key = str(event.get("kol_key") or "").strip().casefold()
    policy_direct_war = bool(
        key == "trump"
        and _is_owned_direct_source(event)
        and _WAR_RE.search(text)
        and not _NON_GEO_WAR_RE.search(text)
    )
    if (
        (not physical_crash and _MARKET_SHOCK_RE.search(text))
        or _BUBBLE_CRISIS_RE.search(text)
        or _RATE_DECISION_RE.search(text)
        or _RATE_HOLD_RE.search(text)
        or _TARIFF_ACTION_RE.search(text)
        or _SYSTEMIC_RE.search(text)
        or geo_war
        or policy_direct_war
    ):
        return "high"
    if _MEDIUM_RE.search(text):
        return "medium"

    baseline = str(
        event.get("kol_baseline_impact") or _BASELINE_IMPACT.get(key, "low")
    ).strip().casefold()
    return "medium" if baseline in {"high", "medium"} else "low"


def assess_event_relevance(
    event: Mapping[str, Any], *, require_entity: bool = True
) -> dict[str, Any]:
    """Assess attribution, finance scope and contextual rule impact.

    ``eligible`` means the item has substantive content and can safely be
    attributed to the requested KOL or an affiliated company.  It does not
    mean the KOL personally spoke.  ``intelligence_eligible`` additionally
    requires finance relevance and is the gate intended for LLM/relations.
    """
    text = _text(event)
    content_ok = bool(text) and is_event_content_eligible(event)
    direct = _is_owned_direct_source(event)

    if direct:
        entity_match, matched_alias, attribution_basis = (
            True,
            "@" + _direct_handle(
                event.get("source"),
                event.get("source_url")
                or event.get("canonical_url")
                or event.get("url"),
            ),
            "direct_source",
        )
        if not has_substantive_social_text(
            event.get("snippet") or event.get("title")
        ):
            content_ok = False
    else:
        entity_match, matched_alias, attribution_basis = matches_kol_entity(event)

    attribution_ok = entity_match or not require_entity
    eligible = bool(content_ok and attribution_ok)
    finance_relevant = is_finance_relevant(
        event, attribution_basis=attribution_basis
    )
    rule_impact = classify_rule_impact(
        event, finance_relevant=finance_relevant
    )

    if not content_ok:
        reason = "insubstantial_content"
    elif require_entity and not entity_match:
        reason = "entity_not_found"
    elif not finance_relevant:
        reason = "non_finance"
    elif attribution_basis == "direct_source":
        reason = "direct_source_finance"
    elif attribution_basis == "company_mention":
        reason = "affiliated_company_finance"
    else:
        reason = "person_mention_finance"

    return {
        "eligible": eligible,
        "intelligence_eligible": bool(eligible and finance_relevant),
        "entity_match": entity_match,
        "finance_relevant": finance_relevant,
        "reason": reason,
        "rule_impact": rule_impact,
        "matched_alias": matched_alias,
        "attribution_basis": attribution_basis,
        "classifier_version": CLASSIFIER_VERSION,
    }


__all__ = [
    "CLASSIFIER_VERSION",
    "KOL_DIRECTORY",
    "assess_event_relevance",
    "classify_rule_impact",
    "is_direct_source",
    "is_owned_direct_source",
    "is_finance_relevant",
    "matches_kol_entity",
]
