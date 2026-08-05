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

# 市场关键词 — 扩展了 AI/半导体/算力相关关键词
MARKET_KEYWORDS = [
    "buy", "sell", "invest", "stock", "share", "price", "market",
    "tariff", "trade", "war", "crisis", "bubble", "crash",
    "AI", "crypto", "bitcoin", "ethereum", "DOGE",
    "Tesla", "Apple", "NVIDIA", "Microsoft", "Amazon", "Google",
    "Dell", "HP", "Intel", "AMD", "TSMC", "Samsung",
    "算力", "AI泡沫", "AI bubble", "半导体", "semiconductor",
    "chip", "数据中心", "data center", "GPU", "capital expenditure",
    "AI算力", "AI资本开支", "大模型", "LLM",
    "AI spending", "AI capex", "AI investment",
    "Meta", "OpenAI", "Anthropic", "Google AI",
    "中国", "A股", "港股", "美股", "牛市", "熊市",
    "买入", "卖出", "加仓", "减仓", "看好", "看空",
    "关税", "贸易战", "降息", "加息", "通胀",
]


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
        link = ""
        pub_date = None
        for child in item:
            ctag = child.tag.lower().rsplit("}", 1)[-1]
            if ctag == "title":
                title = strip_html(child.text)
            elif ctag == "link":
                link = (child.text or "").strip()
            elif ctag == "pubdate":
                pub_date = normalize_published_at(child.text)
        if title and link:
            results.append({
                "title": title,
                "url": link,
                "source": "Bing News",
                "published_at": pub_date,
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
    """搜索某个 KOL 的近期言论"""
    results = []
    seen = set()
    for item in search_bing_rss(query, max_results):
        if item["url"] not in seen:
            seen.add(item["url"])
            results.append(item)
    for item in search_baidu(query, max_results):
        if item["url"] not in seen:
            seen.add(item["url"])
            results.append(item)
    return results


_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")


def extract_tickers(text: str, limit: int = 8) -> list[str]:
    """提取 $TICKER 形式的股票代码，保持出现顺序去重。"""
    seen: list[str] = []
    for t in _TICKER_RE.findall(text or ""):
        if t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    return seen


HIGH_WORDS = [
    "crisis", "crash", "war", "bubble", "panic", "sell-off",
    "recession", "depression", "collapse", "bankrupt",
    "危机", "崩溃", "崩盘", "泡沫", "恐慌", "战争",
    "降息", "加息", "关税", "核战", "制裁",
]

MEDIUM_WORDS = [
    "warning", "alert", "caution", "risk", "slowdown",
    "inflation", "deflation", "tariff", "trade war",
    "预警", "警告", "风险", "放缓", "减速",
    "通胀", "通缩", "贸易战",
]


def _compile_terms(words: list[str]) -> re.Pattern:
    """ASCII 词加词边界，避免 war 命中 Warsh；CJK 无词边界概念，直接子串匹配。"""
    parts = []
    for w in words:
        esc = re.escape(w.lower())
        if re.match(r"^[a-z0-9][a-z0-9\s\-]*$", w.lower()):
            parts.append(rf"\b{esc}\b")
        else:
            parts.append(esc)
    return re.compile("|".join(parts))


_HIGH_RE = _compile_terms(HIGH_WORDS)
_MEDIUM_RE = _compile_terms(MEDIUM_WORDS)
_MARKET_RE = _compile_terms(MARKET_KEYWORDS)


def classify_kol_impact(item: dict[str, Any], kol: dict[str, Any]) -> str:
    """判断单条动态的影响力等级。

    不直接套用 KOL 基线（否则 Trump/黄仁勋等所有新闻都会变成 high）。
    规则：
      - 危机/宏观冲击词 → high
      - 预警/风险词 → medium
      - 含市场关键词或股票代码，且 KOL 基线为 high/medium → medium
      - 其余 → low
    """
    base = kol.get("impact", "low")
    blob = (item.get("title", "") + " " + item.get("snippet", "")).lower()

    if _HIGH_RE.search(blob):
        return "high"
    if _MEDIUM_RE.search(blob):
        return "medium"

    raw = item.get("title", "") + " " + item.get("snippet", "")
    if (_MARKET_RE.search(blob) or _TICKER_RE.search(raw)) and base in ("high", "medium"):
        return "medium"

    return "low"


_SCRIPTS_DIRS = [
    os.environ.get("KOL_SCRIPTS_DIR", ""),
    _HERE,
]


def search_x(handle: str, max_results: int = 10) -> list[dict[str, Any]]:
    """抓取 X 账号最新推文。复用 serenity_tracker 的解析逻辑。"""
    for d in _SCRIPTS_DIRS:
        if d and os.path.isfile(os.path.join(d, "serenity_tracker.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            break
    try:
        import serenity_tracker as st  # type: ignore
    except Exception as e:
        sys.stderr.write(f"[kol_tracker] x source unavailable: {e}\n")
        return []

    try:
        tweets = st.parse_tweets(st.fetch_page())
    except Exception as e:
        sys.stderr.write(f"[kol_tracker] x fetch failed for @{handle}: {e}\n")
        return []

    results = []
    for t in tweets[:max_results]:
        text = re.sub(r"\s+", " ", (t.get("text") or "").strip())
        if not text:
            continue
        # 推文没有标题，取首句作标题，全文进摘要
        title = text if len(text) <= 90 else text[:90].rstrip() + "…"
        published_at = None
        for candidate in (
            t.get("published_at"),
            t.get("created_at"),
            t.get("date"),
        ):
            published_at = normalize_published_at(candidate)
            if published_at:
                break
        results.append(
            {
                "title": title,
                "snippet": text,
                "url": f"https://x.com/{handle}/status/{t['tid']}",
                "source": f"X @{handle}",
                "published_at": published_at,
            }
        )
    return results


def scan_kol(kol_key: str, max_results: int = 5) -> list[dict[str, Any]]:
    """扫描单个 KOL 的近期动态"""
    kol = KOLS.get(kol_key)
    if not kol:
        return []

    if kol.get("source_type") == "x":
        items = search_x(kol.get("handle", ""), max_results)
        for item in items:
            item["kol_key"] = kol_key
            item["kol_name"] = kol["name"]
            item["kol_name_cn"] = kol.get("name_cn", kol["name"])
            item["impact"] = classify_kol_impact(item, kol)
            item["tickers"] = extract_tickers(item["snippet"])
            item["has_market_kw"] = bool(item["tickers"]) or bool(
                _MARKET_RE.search((item["title"] + " " + item["snippet"]).lower())
            )
        return items

    results = []
    seen = set()

    for term in kol["search_terms"]:
        items = search_kol(kol_key, term, max_results)
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                item["kol_key"] = kol_key
                item["kol_name"] = kol["name"]
                item["kol_name_cn"] = kol.get("name_cn", kol["name"])
                item["impact"] = classify_kol_impact(item, kol)
                blob = item.get("title", "") + " " + item.get("snippet", "")
                item["tickers"] = extract_tickers(blob)
                item["has_market_kw"] = bool(item["tickers"]) or bool(
                    _MARKET_RE.search(blob.lower())
                )
                results.append(item)

        if len(results) >= max_results:
            break

    return results[:max_results]


def scan_all(max_results: int = 5) -> dict[str, list[Any]]:
    """扫描所有 KOL 的近期动态"""
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
    if all_items:
        ins, skip = _write_to_db(all_items)
        print(
            json.dumps(
                {
                    "kols": len(data),
                    "scanned": len(all_items),
                    "inserted": ins,
                    "skipped": skip,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps({"kols": 0, "scanned": 0, "inserted": 0, "skipped": 0}))


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
