#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOL Tracker v2 — 关键意见领袖动态追踪

数据源：
1. Bing News RSS — 主要英文源（稳定、无需 Key）
2. Baidu News — 中文源
3. DuckDuckGo Lite — 备用（可能被 CAPTCHA 拦截）

子命令：
    search <kol_name> <keyword>           # 搜索 KOL 近期言论
    scan                                   # 扫描所有 KOL 近期重大动态
    trump                                  # 专门搜索 Trump 近期言论
    musk                                   # 专门搜索 Musk 近期言论
    digest                                 # 生成今日 KOL 摘要（供日报使用）
    emergency                              # 紧急扫描（供 macro-emergency-check 使用）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

# 让 cmd_collect / cmd_emergency 能写入 dashboard 数据库。
# 部署到服务器时 lib 目录位于 <dashboard>/lib，故同时探测上级目录。
_HERE = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD_CANDIDATES = [
    os.environ.get("KOL_DASHBOARD_DIR", ""),
    os.path.dirname(_HERE),
    os.path.join(os.path.dirname(_HERE), "kol_dashboard"),
]
for _cand in _DASHBOARD_CANDIDATES:
    if _cand and os.path.isfile(os.path.join(_cand, "db.py")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from content_quality import has_substantive_social_text  # noqa: E402

try:
    from kol_dashboard.event_relevance import (
        KOL_DIRECTORY,
        assess_event_relevance,
        classify_rule_impact,
    )
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    from event_relevance import (  # type: ignore
        KOL_DIRECTORY,
        assess_event_relevance,
        classify_rule_impact,
    )


def _write_to_db(items: list[dict[str, Any]]) -> tuple[int, int]:
    """Best-effort write to dashboard DB; returns (inserted, skipped).
    Silent failure — scanner keeps running even if dashboard DB is unreachable."""
    try:
        import db  # type: ignore
        db.init()
        return db.insert_events(items)
    except Exception as e:
        sys.stderr.write(f"[kol_tracker] db write skipped: {e}\n")
        if os.environ.get("KOL_DB_WRITE_REQUIRED") == "1":
            raise
        return 0, 0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 6
CST = timezone(timedelta(hours=8))

# ─── KOL 配置 ──────────────────────────────────────────

KOLS = {
    "trump": {
        "name": "Donald Trump",
        "name_cn": "特朗普",
        "search_terms": ["Trump"],
        "impact": "high",
        # Primary-source posts, ahead of any news coverage of them.
        "truth_handle": "realDonaldTrump",
    },
    "musk": {
        "name": "Elon Musk",
        "name_cn": "马斯克",
        "search_terms": ["Elon Musk"],
        "impact": "high",
    },
    "buffett": {
        "name": "Warren Buffett",
        "name_cn": "巴菲特",
        "search_terms": ["Warren Buffett"],
        "impact": "medium",
    },
    "dalio": {
        "name": "Ray Dalio",
        "name_cn": "瑞达利欧",
        "search_terms": ["Ray Dalio"],
        "impact": "medium",
    },
    "duanyongping": {
        "name": "Duan Yongping",
        "name_cn": "段永平",
        "search_terms": ["段永平"],
        "impact": "medium",
    },
    "danbin": {
        "name": "但斌",
        "name_cn": "但斌",
        "search_terms": ["但斌"],
        "impact": "low",
    },
    "renzeping": {
        "name": "任泽平",
        "name_cn": "任泽平",
        "search_terms": ["任泽平"],
        "impact": "low",
    },
    "huangrenxun": {
        "name": "Jensen Huang",
        "name_cn": "黄仁勋",
        "search_terms": ["Jensen Huang", "黄仁勋", "NVIDIA CEO"],
        "impact": "high",
    },
    "suzifeng": {
        "name": "Lisa Su",
        "name_cn": "苏姿丰",
        "search_terms": ["Lisa Su", "苏姿丰", "AMD CEO"],
        "impact": "high",
    },
    "altman": {
        "name": "Sam Altman",
        "name_cn": "Sam Altman",
        "search_terms": ["Sam Altman", "OpenAI CEO"],
        "impact": "high",
    },
    "zuckerberg": {
        "name": "Mark Zuckerberg",
        "name_cn": "扎克伯格",
        "search_terms": ["Mark Zuckerberg", "扎克伯格", "Meta CEO"],
        "impact": "high",
    },
    # ─── 宏观政策制定者 — 黑天鹅/灰犀牛一手信号 ───
    "powell": {
        "name": "Jerome Powell",
        "name_cn": "鲍威尔",
        "search_terms": ["Jerome Powell", "鲍威尔", "Fed Chair"],
        "impact": "high",
        "category": "macro",
    },
    "pangongsheng": {
        "name": "Pan Gongsheng",
        "name_cn": "潘功胜",
        "search_terms": ["潘功胜", "央行行长"],
        "impact": "high",
        "category": "macro",
    },
    # ─── 风险预警型投资人 ───
    "dimon": {
        "name": "Jamie Dimon",
        "name_cn": "杰米·戴蒙",
        "search_terms": ["Jamie Dimon", "戴蒙", "JPMorgan CEO"],
        "impact": "high",
        "category": "risk",
    },
    "burry": {
        "name": "Michael Burry",
        "name_cn": "迈克尔·伯里",
        "search_terms": ["Michael Burry", "迈克尔·伯里"],
        "impact": "medium",
        "category": "risk",
    },
    "howardmarks": {
        "name": "Howard Marks",
        "name_cn": "霍华德·马克斯",
        "search_terms": ["Howard Marks", "霍华德·马克斯", "Oaktree"],
        "impact": "medium",
        "category": "risk",
    },
    "cathiewood": {
        "name": "Cathie Wood",
        "name_cn": "木头姐",
        "search_terms": ["Cathie Wood", "木头姐", "ARK Invest"],
        "impact": "medium",
        "category": "growth",
    },
    # ─── X / 社交源 ───
    "serenity": {
        "name": "Serenity",
        "name_cn": "Serenity",
        "search_terms": [],
        "impact": "medium",
        "category": "trader",
        "source_type": "x",
        "handle": "aleabitoreddit",
    },
}

# The shared directory is authoritative for public display metadata.  Keeping
# retrieval-only search/source settings here avoids a backend -> collector
# dependency while ensuring `/api/kols` and collected rows use the same names.
for _kol_key, _public_profile in KOL_DIRECTORY.items():
    if _kol_key in KOLS:
        KOLS[_kol_key].update(_public_profile)

# ─── 网络请求 ──────────────────────────────────────────

def http_get(url: str, headers: dict | None = None, timeout: int = TIMEOUT) -> str:
    merged = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, errors="ignore")
    except Exception:
        return ""


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_published_at(
    value: Any,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return a trustworthy absolute timestamp in UTC ISO-8601 form.

    Relative or timezone-free display strings (for example ``2h`` or
    ``Jul 31`` from X) are deliberately rejected rather than inferred.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        )
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if normalized > current.astimezone(timezone.utc) + timedelta(minutes=5):
        return None
    return normalized.isoformat()


# X labels anything under a day as an offset ("5h") and older posts as a bare
# date. Only the offset form pins down a time of day.
_RELATIVE_OFFSET_RE = re.compile(r"^(\d{1,2})\s*([smh])$", re.I)
_OFFSET_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}
_OFFSET_LIMITS = {"s": 59, "m": 59, "h": 23}


def resolve_relative_time(
    value: Any,
    *,
    now: datetime | None = None,
) -> str | None:
    """Anchor a relative offset label to the moment it was observed.

    The label is floor-rounded, so "6h" means at least six hours but under
    seven. This resolves to the older edge of that window: the newer edge can
    land after a post was already observed, which is provably impossible and
    gets the record quarantined. A bare date such as ``Aug 4`` pins down no
    time of day at all and stays unresolved.
    """
    if not isinstance(value, str):
        return None
    match = _RELATIVE_OFFSET_RE.match(value.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount > _OFFSET_LIMITS[unit]:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    observed = current.astimezone(timezone.utc) - timedelta(
        **{_OFFSET_UNITS[unit]: amount + 1}
    )
    return observed.replace(microsecond=0).isoformat()


# ─── Bing News RSS 搜索 ────────────────────────────────

_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.S | re.I)


def _tagged_text(fragment: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", fragment, re.S | re.I)
    return match.group(1) if match else ""


def _parse_bing_items_with_regex(xml: str) -> list[dict[str, Any]]:
    """Recover items from a feed that is not well-formed XML.

    Bing intermittently emits unescaped markup, which makes the whole
    document unparseable and would otherwise drop that KOL's entire feed.
    """
    recovered: list[dict[str, Any]] = []
    for fragment in _ITEM_RE.findall(xml):
        title = strip_html(_tagged_text(fragment, "title"))
        link = strip_html(_tagged_text(fragment, "link"))
        if not title or not link:
            continue
        recovered.append({
            "title": title,
            "snippet": strip_html(_tagged_text(fragment, "description")),
            "url": link,
            "source": "Bing News",
            "published_at": normalize_published_at(
                strip_html(_tagged_text(fragment, "pubDate"))
            ),
        })
    return recovered


def search_bing_rss(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """使用 Bing News RSS feed 搜索。

    必须显式指定市场并按时间排序：默认的相关度排序会把几个月前的旧闻排在
    最前，经过严格时效过滤后前台会长期显示不出新内容。
    """
    q = urllib.parse.quote(query)
    url = (
        f"https://www.bing.com/news/search?q={q}&format=rss"
        "&setmkt=en-US&setlang=en-US"
        '&qft=sortbydate%3d"1"'
    )

    html = http_get(url, headers={"User-Agent": UA})
    if not html:
        return []

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(html)
    except Exception:
        return _parse_bing_items_with_regex(html)[:max_results]

    results: list[dict[str, Any]] = []
    for item in root.iter():
        tag = item.tag.lower().rsplit("}", 1)[-1]
        if tag != "item":
            continue
        title = ""
        snippet = ""
        link = ""
        pub_date = None
        for child in item:
            ctag = child.tag.lower().rsplit("}", 1)[-1]
            if ctag == "title":
                title = strip_html(child.text)
            elif ctag == "description":
                snippet = strip_html(child.text)
            elif ctag == "link":
                link = (child.text or "").strip()
            elif ctag == "pubdate":
                pub_date = normalize_published_at(child.text)
        if title and link:
            results.append({
                "title": title,
                "snippet": snippet,
                "url": link,
                "source": "Bing News",
                "published_at": pub_date,
            })
            if len(results) >= max_results:
                break

    return results


# ─── Truth Social ─────────────────────────────────────

# truthsocial.com puts its own API and RSS behind Cloudflare, which rejects
# server-side requests outright. This mirror republishes the same posts with
# their original timestamps and permalinks.
TRUTH_MIRROR_FEED = "https://trumpstruth.org/feed"
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)


def _feed_field(fragment: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", fragment, re.S | re.I)
    if not match:
        return ""
    return _CDATA_RE.sub(r"\1", match.group(1)).strip()


_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.S | re.I,
)
_TRUTH_PROFILE_LINK_RE = re.compile(
    r"https?://(?:www\.)?truthsocial\.com/@([A-Za-z0-9_]{1,64})/?",
    re.I,
)


def _post_body_text(markup: str) -> str:
    """Flatten post HTML into readable text.

    Anchors are reduced to their href because Mastodon renders long URLs as
    several spans, so the visible text alone reassembles into a broken link.
    Remaining tags become spaces to keep paragraphs from running together.
    """
    if not markup:
        return ""
    def flatten_anchor(match: re.Match[str]) -> str:
        href = unescape(match.group(1)).strip()
        profile = _TRUTH_PROFILE_LINK_RE.fullmatch(href)
        if profile:
            return f" @{profile.group(1)} "
        return f" {href} "

    text = _ANCHOR_RE.sub(flatten_anchor, markup)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def search_truth_social(
    handle: str,
    max_results: int = 10,
    *,
    feed_url: str = TRUTH_MIRROR_FEED,
) -> list[dict[str, Any]]:
    """抓取 Truth Social 帖子（经镜像站，带原始时间与永久链接）。

    只保留有正文的帖子：纯转帖和纯图片帖没有可分析的文本。
    """
    xml = http_get(feed_url, headers={"User-Agent": UA}, timeout=15)
    if not xml:
        return []

    results: list[dict[str, Any]] = []
    for fragment in _ITEM_RE.findall(xml):
        body = _post_body_text(_feed_field(fragment, "description"))
        if not has_substantive_social_text(body):
            continue
        url = (
            _feed_field(fragment, "truth:originalUrl")
            or _feed_field(fragment, "link")
        )
        if not url:
            continue
        title = body if len(body) <= 90 else body[:90].rstrip() + "…"
        results.append({
            "title": title,
            "snippet": body,
            "url": url,
            "source": f"Truth Social @{handle}",
            "published_at": normalize_published_at(
                _feed_field(fragment, "pubDate")
            ),
        })
        if len(results) >= max_results:
            break

    return results


# ─── 百度新闻搜索 ──────────────────────────────────────

_BAIDU_DATE_RE = re.compile(
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?"
)
# Baidu renders the timestamp in a sibling node shortly after the headline.
_BAIDU_DATE_WINDOW = 400


def _baidu_published_at(html: str, offset: int) -> str | None:
    """Read the absolute timestamp Baidu prints next to a headline.

    Baidu reports Beijing time without an offset, and also uses relative
    labels such as 3小时前 which are deliberately not inferred.
    """
    match = _BAIDU_DATE_RE.search(html, offset, offset + _BAIDU_DATE_WINDOW)
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        stamp = datetime(
            int(year), int(month), int(day),
            int(hour or 0), int(minute or 0),
            tzinfo=CST,
        )
    except ValueError:
        return None
    return normalize_published_at(stamp.isoformat())


def search_baidu(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """百度新闻搜索"""
    results = []
    q = urllib.parse.quote(query)
    url = f"https://news.baidu.com/ns?word={q}&pn=0&rn=10&cl=2&ct=1&tn=news&ie=utf-8"

    html = http_get(url, headers={
        "User-Agent": UA,
        "Referer": "https://news.baidu.com/",
    })
    if not html:
        return results

    for m in re.finditer(
        r'<h3[^>]*>.*?<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    ):
        link = m.group(1)
        title = strip_html(m.group(2))
        if not title or not link:
            continue
        results.append({
            "title": title,
            "url": link,
            "source": "Baidu News",
            "published_at": _baidu_published_at(html, m.end()),
        })
        if len(results) >= max_results:
            break

    return results


# ─── 搜索单个 KOL ─────────────────────────────────────

def search_kol(key: str, query: str, max_results: int = 5) -> list[dict[str, str]]:
    """搜索某个 KOL 的近期动态，并拒绝搜索引擎的错误归因。

    The query string is retrieval input, never entity evidence.  A result must
    name either the person or an affiliated company in its own title/snippet.
    """
    kol = KOLS.get(key)
    if not kol:
        return []

    def is_attributable(item: dict[str, Any]) -> bool:
        candidate = {
            **item,
            "kol_key": key,
            "kol_name": kol["name"],
            "kol_name_cn": kol.get("name_cn", kol["name"]),
            "kol_baseline_impact": kol.get("impact", "low"),
        }
        return bool(
            assess_event_relevance(candidate)["intelligence_eligible"]
        )

    results = []
    seen = set()
    for item in search_bing_rss(query, max_results):
        if item["url"] not in seen and is_attributable(item):
            seen.add(item["url"])
            item["query_term"] = query
            results.append(item)
    for item in search_baidu(query, max_results):
        if item["url"] not in seen and is_attributable(item):
            seen.add(item["url"])
            item["query_term"] = query
            results.append(item)
    return results


_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
_CONTEXT_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9.-]{0,5})\s+"
    r"(?:(?:class\s+[A-Z]\s+|common\s+)?stock|shares?)\b"
)
_CONTEXT_TICKER_STOPWORDS = {
    "A",
    "AI",
    "CEO",
    "CFO",
    "ETF",
    "GDP",
    "IPO",
    "SEC",
    "THE",
    "US",
    "USD",
}


def extract_tickers(text: str, limit: int = 8) -> list[str]:
    """提取明确的 $TICKER 或 ``TICKER stock/shares``，按出现顺序去重。"""
    source = text or ""
    candidates = [
        (match.start(), match.group(1), False)
        for match in _TICKER_RE.finditer(source)
    ] + [
        (match.start(), match.group(1), True)
        for match in _CONTEXT_TICKER_RE.finditer(source)
    ]
    seen: list[str] = []
    for _, ticker, is_contextual in sorted(candidates, key=lambda item: item[0]):
        if is_contextual and ticker in _CONTEXT_TICKER_STOPWORDS:
            continue
        if ticker not in seen:
            seen.append(ticker)
        if len(seen) >= limit:
            break
    return seen


def classify_kol_impact(item: dict[str, Any], kol: dict[str, Any]) -> str:
    """判断单条动态的影响力等级。

    由共享相关性模块做上下文组合判断：物理事故中的 ``crash`` 不会
    升级，市场崩盘、利率决议、关税行动和地缘升级仍可判为 high。
    """
    candidate = {**item, "kol_baseline_impact": kol.get("impact", "low")}
    return classify_rule_impact(candidate)


_SCRIPTS_DIRS = [
    os.environ.get("KOL_SCRIPTS_DIR", ""),
    _HERE,
]

X_FETCH_ATTEMPTS = 3
X_RETRY_BACKOFF_SECONDS = 2
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_X_TWEET_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
X_SOURCE_WARNINGS: dict[str, dict[str, str]] = {}


class XSourceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _set_x_source_warning(handle: str, code: str) -> None:
    X_SOURCE_WARNINGS[handle.casefold()] = {
        "source": f"X @{handle}",
        "code": code,
    }


def _x_error_code(error: Exception | None) -> str:
    code = str(getattr(error, "code", "") or "").strip()
    if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code):
        return code
    if str(error or "").strip() == "source_parse_empty":
        return "source_parse_empty"
    return "source_fetch_failed"


def search_x(handle: str, max_results: int = 10) -> list[dict[str, Any]]:
    """抓取 X 账号最新推文。复用 serenity_tracker 的解析逻辑。"""
    handle = str(handle or "").strip().lstrip("@")
    if not _X_HANDLE_RE.fullmatch(handle):
        sys.stderr.write("[kol_tracker] x source warning code=invalid_x_handle\n")
        return []
    X_SOURCE_WARNINGS.pop(handle.casefold(), None)
    for d in _SCRIPTS_DIRS:
        if d and os.path.isfile(os.path.join(d, "serenity_tracker.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            break
    try:
        import serenity_tracker as st  # type: ignore
    except Exception:
        _set_x_source_warning(handle, "source_unavailable")
        sys.stderr.write(
            f"[kol_tracker] x source warning @{handle} "
            "code=source_unavailable\n"
        )
        return []

    # x.com returns intermittent 5xx responses; a couple of retries recovers
    # the fetch far more often than it fails.
    tweets = None
    last_error: Exception | None = None
    for attempt in range(X_FETCH_ATTEMPTS):
        try:
            fetch_tweets = getattr(st, "fetch_tweets", None)
            if callable(fetch_tweets):
                tweets = fetch_tweets(handle)
            else:
                tweets = st.parse_tweets(st.fetch_page())
            if not isinstance(tweets, list):
                raise XSourceError("source_bad_payload")
            if not tweets:
                raise XSourceError("source_parse_empty")
            break
        except Exception as e:
            last_error = e
            tweets = None
            if attempt < X_FETCH_ATTEMPTS - 1:
                time.sleep(X_RETRY_BACKOFF_SECONDS * (attempt + 1))
    if not tweets:
        code = _x_error_code(last_error)
        _set_x_source_warning(handle, code)
        sys.stderr.write(
            f"[kol_tracker] x source warning @{handle} code={code} after "
            f"{X_FETCH_ATTEMPTS} attempts\n"
        )
        return []

    results = []
    for t in tweets[:max_results]:
        tid = str(t.get("tid") or "").strip()
        if not _X_TWEET_ID_RE.fullmatch(tid):
            continue
        observed_handle = str(t.get("handle") or "").strip().lstrip("@")
        if observed_handle and observed_handle.casefold() != handle.casefold():
            continue
        expected_url = f"https://x.com/{handle}/status/{tid}"
        source_url = str(t.get("url") or expected_url).strip()
        if source_url != expected_url:
            continue
        text = re.sub(r"\s+", " ", (t.get("text") or "").strip())
        if not text:
            continue
        # 推文没有标题，取首句作标题，全文进摘要
        title = text if len(text) <= 90 else text[:90].rstrip() + "…"
        published_at = None
        candidates = (
            t.get("published_at"),
            t.get("created_at"),
            t.get("date"),
        )
        for candidate in candidates:
            published_at = normalize_published_at(candidate)
            if published_at:
                break
        if not published_at:
            for candidate in candidates:
                published_at = resolve_relative_time(candidate)
                if published_at:
                    break
        results.append(
            {
                "title": title,
                "snippet": text,
                "url": expected_url,
                "source": f"X @{handle}",
                "published_at": published_at,
            }
        )
    if tweets and not results:
        _set_x_source_warning(handle, "source_items_invalid")
        sys.stderr.write(
            f"[kol_tracker] x source warning @{handle} "
            "code=source_items_invalid\n"
        )
    return results


def scan_kol(kol_key: str, max_results: int = 5) -> list[dict[str, Any]]:
    """扫描单个 KOL 的近期动态"""
    kol = KOLS.get(kol_key)
    if not kol:
        return []

    def annotate(item: dict[str, Any]) -> dict[str, Any] | None:
        item["kol_key"] = kol_key
        item["kol_name"] = kol["name"]
        item["kol_name_cn"] = kol.get("name_cn", kol["name"])
        item["kol_baseline_impact"] = kol.get("impact", "low")
        assessment = assess_event_relevance(item)
        if not assessment["intelligence_eligible"]:
            return None
        item["impact"] = assessment["rule_impact"]
        blob = item.get("title", "") + " " + item.get("snippet", "")
        item["tickers"] = extract_tickers(blob)
        item["has_market_kw"] = assessment["finance_relevant"]
        item["intelligence_eligible"] = assessment["intelligence_eligible"]
        item["relevance_reason"] = assessment["reason"]
        item["matched_alias"] = assessment["matched_alias"]
        item["attribution_basis"] = assessment["attribution_basis"]
        item["classifier_version"] = assessment["classifier_version"]
        return item

    if kol.get("source_type") == "x":
        annotated = []
        for item in search_x(kol.get("handle", ""), max_results):
            result = annotate(item)
            if result is not None:
                annotated.append(result)
        return annotated

    results = []
    seen = set()

    # Own-platform posts come first: they are the primary source for anything
    # the news feeds will only report on later. They take at most half the
    # budget so news coverage is never crowded out entirely.
    truth_handle = kol.get("truth_handle")
    if truth_handle:
        for item in search_truth_social(
            truth_handle, max(1, max_results // 2)
        ):
            if item["url"] not in seen:
                seen.add(item["url"])
                annotated = annotate(item)
                if annotated is not None:
                    results.append(annotated)

    for term in kol["search_terms"]:
        if len(results) >= max_results:
            break
        for item in search_kol(kol_key, term, max_results):
            if item["url"] not in seen:
                seen.add(item["url"])
                annotated = annotate(item)
                if annotated is not None:
                    results.append(annotated)

    return results[:max_results]


def scan_all(max_results: int = 5) -> dict[str, list[Any]]:
    """扫描所有 KOL 的近期动态"""
    X_SOURCE_WARNINGS.clear()
    data = {}
    for kol_key in KOLS:
        items = scan_kol(kol_key, max_results)
        if items:
            data[kol_key] = items
    return data


# ─── CLI 命令 ──────────────────────────────────────────

def cmd_search(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: kol_tracker.py search <kol_name> <keyword>")
        return
    kol_name = args[0]
    keyword = args[1]
    max_results = int(args[2]) if len(args) > 2 else 5

    results = []
    seen = set()
    for item in search_bing_rss(f"{kol_name} {keyword}", max_results):
        if item["url"] not in seen:
            seen.add(item["url"])
            results.append(item)
    for item in search_baidu(f"{kol_name} {keyword}", max_results):
        if item["url"] not in seen:
            seen.add(item["url"])
            results.append(item)

    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_scan(args: list[str]) -> None:
    max_results = int(args[0]) if args else 5
    data = scan_all(max_results)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_trump(args: list[str]) -> None:
    max_results = int(args[0]) if args else 10
    data = scan_kol("trump", max_results)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_musk(args: list[str]) -> None:
    max_results = int(args[0]) if args else 10
    data = scan_kol("musk", max_results)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_digest(args: list[str]) -> None:
    """生成今日 KOL 摘要（供日报使用）"""
    max_results = int(args[0]) if args else 5
    data = scan_all(max_results)

    output = []
    for kol_key, items in data.items():
        kol = KOLS.get(kol_key, {})
        kol_name_cn = kol.get("name_cn", kol_key)

        significant = [
            it for it in items
            if it["impact"] in ("high", "medium") or it["has_market_kw"]
        ]

        if not significant:
            continue

        section = {
            "kol": kol_name_cn,
            "kol_key": kol_key,
            "items": significant[:5],
        }
        output.append(section)

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_emergency(args: list[str]) -> None:
    """紧急扫描 — 只检查高影响 KOL（Trump/Musk/Zuckerberg/Huang/Altman）。
    同时把所有扫描到的事件写入 dashboard 数据库（去重）。"""
    max_results = int(args[0]) if args else 10
    alerts = []
    all_scanned: list[dict[str, Any]] = []

    for kol_key in ("trump", "musk", "zuckerberg", "huangrenxun", "altman"):
        items = scan_kol(kol_key, max_results)
        all_scanned.extend(items)
        for item in items:
            if item["impact"] == "high" or (
                item["impact"] == "medium" and item["has_market_kw"]
            ):
                alerts.append(item)

    # 写入 dashboard 数据库
    if all_scanned:
        ins, skip = _write_to_db(all_scanned)
        sys.stderr.write(f"[emergency] db: +{ins} new, {skip} dup\n")

    seen = set()
    unique_alerts = []
    for a in alerts:
        t = a["title"][:50]
        if t not in seen:
            seen.add(t)
            unique_alerts.append(a)

    if unique_alerts:
        print(json.dumps(unique_alerts, ensure_ascii=False, indent=2))


def cmd_collect(args: list[str]) -> None:
    """全量扫描所有 KOL 并写入 dashboard 数据库（静默，不输出到 stdout 用于 Slack）。"""
    max_results = int(args[0]) if args else 5
    data = scan_all(max_results)
    all_items: list[dict[str, Any]] = []
    for items in data.values():
        all_items.extend(items)
    result = {
        "kols": len(data),
        "scanned": len(all_items),
        "inserted": 0,
        "skipped": 0,
    }
    if all_items:
        ins, skip = _write_to_db(all_items)
        result.update({"inserted": ins, "skipped": skip})
    if X_SOURCE_WARNINGS:
        result["source_warnings"] = [
            X_SOURCE_WARNINGS[key] for key in sorted(X_SOURCE_WARNINGS)
        ]
    print(json.dumps(result, ensure_ascii=False))


# ─── Dispatcher ────────────────────────────────────────

COMMANDS = {
    "search": cmd_search,
    "scan": cmd_scan,
    "trump": cmd_trump,
    "musk": cmd_musk,
    "digest": cmd_digest,
    "emergency": cmd_emergency,
    "collect": cmd_collect,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write(__doc__ or "")
        return 1
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except BrokenPipeError:
        return 0
    except Exception as e:
        sys.stderr.write(json.dumps({"error": str(e)}, ensure_ascii=False) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
