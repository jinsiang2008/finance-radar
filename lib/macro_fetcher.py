#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Macro Monitor — 宏观数据抓取核心库

数据源（2026-07-07 更新）：
  - VIX: CNBC quotes (Yahoo Finance blocked)
  - 美债: Investing.com (CNBC timeout)
  - 汇率/黄金/原油: 新浪财经 (Yahoo Finance blocked)
  - 美联储: RSS Feed (页面结构变化)
  - 美联储讲话: Fed Speeches RSS（鲍威尔/理事讲话）
  - FOMC声明: Fed Monetary Policy RSS（FOMC声明/纪要/点阵图）
  - 人行: 直接解析公告列表 (UTF-8)
  - 人行利率政策: 利率政策页面
  - 人行行长讲话: Bing News 搜索
  - 新闻联播: CCTV 页面
  - BLS/统计局/经济日历: 数据源受限，静默降级

子命令：
    pboc              # 中国人民银行 — 最新货币政策/利率公告
    pboc_rate         # 中国人民银行 — 最新利率政策
    pboc_speech       # 中国人民银行行长 — 最新讲话
    nbs               # 国家统计局 — 最新经济数据
    cctv_news         # 新闻联播文字版 — 头条
    fed               # 美联储 — 最新声明/讲话（原RSS）
    fed_speeches      # 美联储官员 — 最新讲话（Speeches RSS）
    fomc              # FOMC — 最新声明/纪要/点阵图
    bls               # BLS — 最新CPI/非农
    vix               # CBOE VIX 指数
    treasury_yield    # 美债收益率 (2Y/10Y/30Y)
    usd_cny           # 美元/人民币汇率
    gold_oil          # 黄金/原油价格
    calendar          # 经济数据日历（未来7天）
    score             # 信号评分 (JSON格式)
    help              # 帮助信息
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 15
SEP = "|||"
CN_TZ = timezone(timedelta(hours=8))


def http_get(url: str, headers: dict | None = None, timeout: int = TIMEOUT, retries: int = 2) -> str:
    merged = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(enc, errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                import time
                time.sleep(2 ** attempt * 2)
                continue
            return ""
        except Exception:
            return ""
    return ""


def http_get_raw(url: str, headers: dict | None = None, timeout: int = TIMEOUT) -> bytes:
    """获取原始字节（用于编码不确定的页面）"""
    merged = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return b""


def now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


# ════════════════════════════════════════════
# 🇨🇳 中国政策/新闻源
# ════════════════════════════════════════════

def fetch_pboc() -> list[dict[str, str]]:
    """中国人民银行 — 最新货币政策/利率公告"""
    items = []
    try:
        raw = http_get_raw("http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html")
        # 页面是 UTF-8 编码
        html = raw.decode("utf-8", errors="ignore")
        # 提取公告列表：<a href="..." title="..."> 或 <a href="...">公告标题</a>
        # 优先找包含日期ID的链接（实际公告）
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
            url, text = m.group(1), m.group(2).strip()
            # 过滤导航链接，只保留实际公告（URL 含日期数字）
            if re.search(r'/2026\d{10,}', url) and len(text) > 5:
                full_url = url if url.startswith("http") else "http://www.pbc.gov.cn" + url
                items.append({"title": text, "url": full_url, "source": "中国人民银行"})
    except Exception:
        pass
    return items[:8]


def fetch_pboc_rate() -> list[dict[str, str]]:
    """中国人民银行 — 最新利率政策（含LPR/存贷款基准利率）"""
    items = []
    try:
        raw = http_get_raw("http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/index.html")
        html = raw.decode("utf-8", errors="ignore")
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
            url, text = m.group(1), m.group(2).strip()
            if re.search(r'/2026\d{10,}', url) and len(text) > 5:
                full_url = url if url.startswith("http") else "http://www.pbc.gov.cn" + url
                items.append({"title": text, "url": full_url, "source": "中国人民银行-利率政策"})
    except Exception:
        pass
    return items[:8]


def fetch_pboc_speech() -> list[dict[str, str]]:
    """中国人民银行行长 — 最新讲话/表态（通过Bing News搜索）"""
    items = []
    try:
        raw = http_get(
            "https://www.bing.com/news/search?q=%E6%BD%98%E5%8A%9F%E8%83%9C+%E8%AE%B2%E8%AF%9D+%E5%A4%AE%E8%A1%8C&format=rss",
            timeout=12
        )
        for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
            raw, re.DOTALL
        ):
            title = m.group(1).strip()
            link = m.group(2).strip()
            pub_date = m.group(3).strip()
            title = unescape(title)
            items.append({
                "title": title,
                "url": link,
                "source": "央行行长讲话",
                "date": pub_date,
            })
    except Exception:
        pass
    return items[:8]


def fetch_nbs() -> list[dict[str, str]]:
    """国家统计局 — 最新经济数据发布"""
    # 统计局页面访问受限，返回空
    return []


def fetch_cctv_news() -> list[dict[str, str]]:
    """新闻联播文字版 — 头条"""
    items = []
    try:
        html = http_get("https://tv.cctv.com/lm/xwlb/")
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*title="([^"]*)"', html):
            url, title = m.group(1), m.group(2).strip()
            if not title or not url or "javascript" in url:
                continue
            full_url = url if url.startswith("http") else "https://tv.cctv.com" + url
            items.append({"title": title, "url": full_url, "source": "新闻联播"})
        # 如果 title 属性没抓到，试试 a 标签文本
        if not items:
            for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
                url, text = m.group(1), m.group(2).strip()
                if "javascript" in url or len(text) < 8:
                    continue
                full_url = url if url.startswith("http") else "https://tv.cctv.com" + url
                items.append({"title": text, "url": full_url, "source": "新闻联播"})
    except Exception:
        pass
    return items[:5]


# ════════════════════════════════════════════
# 🇺🇸 美国宏观/政策源
# ════════════════════════════════════════════

def fetch_fed() -> list[dict[str, str]]:
    """美联储 — 最新声明/讲话（通过 RSS Feed）"""
    items = []
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/press_all.xml")
        for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
            raw, re.DOTALL
        ):
            title_raw, link_raw, date_raw = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_raw).strip()
            link = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", link_raw).strip()
            pub_date = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", date_raw).strip()
            title = unescape(title)
            items.append({
                "title": title,
                "url": link,
                "source": "Federal Reserve",
                "date": pub_date,
            })
    except Exception:
        pass
    return items[:8]


def fetch_fed_speeches() -> list[dict[str, str]]:
    """美联储官员 — 最新讲话（鲍威尔/理事/行长，通过 Speeches RSS）"""
    items = []
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/speeches.xml")
        for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
            raw, re.DOTALL
        ):
            title_raw, link_raw, date_raw = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_raw).strip()
            link = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", link_raw).strip()
            pub_date = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", date_raw).strip()
            title = unescape(title)
            items.append({
                "title": title,
                "url": link,
                "source": "Fed Speech",
                "date": pub_date,
            })
    except Exception:
        pass
    return items[:8]


def fetch_fomc() -> list[dict[str, str]]:
    """FOMC — 最新声明/纪要/点阵图（通过 Monetary Policy RSS）"""
    items = []
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/press_monetary.xml")
        for m in re.finditer(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
            raw, re.DOTALL
        ):
            title_raw, link_raw, date_raw = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_raw).strip()
            link = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", link_raw).strip()
            pub_date = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", date_raw).strip()
            title = unescape(title)
            items.append({
                "title": title,
                "url": link,
                "source": "FOMC",
                "date": pub_date,
            })
    except Exception:
        pass
    return items[:8]


def fetch_bls() -> list[dict[str, str]]:
    """BLS — 最新经济数据发布"""
    # BLS 官网被 CDN 封锁，返回空
    return []


# ════════════════════════════════════════════
# 📊 市场指标
# ════════════════════════════════════════════

def fetch_vix() -> dict[str, Any]:
    """CBOE VIX 指数 — 数据源: CNBC"""
    result = {"value": None, "change": None, "status": "unknown"}
    try:
        html = http_get("https://www.cnbc.com/quotes/.VIX")
        # 提取 last 和 change
        last_m = re.search(r'"last":\s*"([0-9.]+)"', html)
        if last_m:
            val = float(last_m.group(1))
            result["value"] = round(val, 2)
            # 状态判断
            if val > 35:
                result["status"] = "critical"
            elif val > 28:
                result["status"] = "elevated"
            elif val > 20:
                result["status"] = "normal"
            else:
                result["status"] = "low"
        # change 字段可能有多个匹配，取第一个合理的
        change_m = re.search(r'"change":\s*"(-?[0-9.]+)"', html)
        if change_m:
            chg = float(change_m.group(1))
            # CNBC 的 change 可能是绝对变化也可能是相对值
            # 只保留合理范围（-10 到 +10）
            if -10 <= chg <= 10:
                result["change"] = round(chg, 2)
    except Exception:
        pass
    return result


def _fetch_treasury_yields_official() -> dict[str, float | None]:
    """美债收益率 — 数据源: 美国财政部官方每日收益率曲线 XML。

    该源按交易日发布，最后一条 entry 即最新一个交易日。
    """
    result: dict[str, float | None] = {"2Y": None, "10Y": None, "30Y": None}
    year = datetime.now(CN_TZ).year
    xml = http_get(
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml?data=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}",
        timeout=15,
    )
    if not xml:
        return result

    fields = {"2Y": "BC_2YEAR", "10Y": "BC_10YEAR", "30Y": "BC_30YEAR"}
    # 取文档中最后一次出现的值 = 最新交易日
    for key, tag in fields.items():
        matches = re.findall(rf"<d:{tag}[^>]*>([0-9.]+)</d:{tag}>", xml)
        if matches:
            result[key] = round(float(matches[-1]), 3)

    date_matches = re.findall(r"<d:NEW_DATE[^>]*>([0-9-]+)T", xml)
    if date_matches:
        result["as_of"] = date_matches[-1]
    return result


def fetch_treasury_yields() -> dict[str, float | None]:
    """美债收益率 (2Y/10Y/30Y)。

    Investing.com 常因反爬返回空，故以美国财政部官方 XML 兜底。
    """
    result: dict[str, float | None] = {"2Y": None, "10Y": None, "30Y": None}
    bonds = {
        "2Y": "https://www.investing.com/rates-bonds/u.s.-2-year-bond-yield",
        "10Y": "https://www.investing.com/rates-bonds/u.s.-10-year-bond-yield",
        "30Y": "https://www.investing.com/rates-bonds/u.s.-30-year-bond-yield",
    }
    for key, url in bonds.items():
        try:
            html = http_get(url, timeout=12)
            m = re.search(r'data-test="instrument-price-last">([^<]+)', html)
            if m:
                val = m.group(1).strip().replace(",", "")
                result[key] = round(float(val), 3)
        except Exception:
            pass

    if all(result.get(k) is None for k in ("2Y", "10Y", "30Y")):
        try:
            return _fetch_treasury_yields_official()
        except Exception:
            pass
    return result


def fetch_usd_cny() -> dict[str, float | None]:
    """美元/人民币汇率 — 数据源: 新浪财经"""
    result = {"rate": None, "change_pct": None}
    try:
        raw = http_get(
            "https://hq.sinajs.cn/list=fx_susdcny",
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        # var hq_str_fx_susdcny="time,price,open,high,low,volume,bid,ask,prev_close,...,change,change_pct,..."
        m = re.search(r'"([^"]+)"', raw)
        if m:
            fields = m.group(1).split(",")
            if len(fields) >= 10:
                rate_str = fields[1].strip()
                result["rate"] = round(float(rate_str), 4)
                # change_pct 通常在 fields[11] 或 fields[12]
                for f in fields[10:15]:
                    f = f.strip()
                    if f and abs(float(f)) < 10:
                        result["change_pct"] = round(float(f), 2)
                        break
    except Exception:
        pass

    if result["rate"] is None:
        # 新浪对机房 IP 返回 403；er-api 免费、无需 Key。
        try:
            data = json.loads(http_get("https://open.er-api.com/v6/latest/USD", timeout=12))
            cny = data.get("rates", {}).get("CNY")
            if cny:
                result["rate"] = round(float(cny), 4)
        except Exception:
            pass
    return result


def fetch_cnbc_quote(symbol: str) -> dict[str, float | None]:
    """CNBC 行情兜底。价格带千分位逗号，需一并处理。"""
    out: dict[str, float | None] = {"price": None, "change_pct": None}
    html = http_get(f"https://www.cnbc.com/quotes/{symbol}", timeout=12)
    if not html:
        return out
    m = re.search(r'"last":\s*"([0-9,.]+)"', html)
    if m:
        out["price"] = round(float(m.group(1).replace(",", "")), 2)
    m = re.search(r'"change_pct":\s*"(-?[0-9.]+)%?"', html) or re.search(
        r'"changePct":\s*"?(-?[0-9.]+)%?"?', html
    )
    if m:
        out["change_pct"] = round(float(m.group(1)), 2)
    elif out["price"]:
        chg = re.search(r'"change":\s*"(-?[0-9,.]+)"', html)
        if chg:
            delta = float(chg.group(1).replace(",", ""))
            prev = out["price"] - delta
            if prev:
                out["change_pct"] = round(delta / prev * 100, 2)
    return out


def fetch_gold_oil() -> dict[str, dict[str, float | None]]:
    """黄金/原油价格 — 新浪财经，失败时回落 CNBC 期货报价。

    新浪对机房 IP 返回 403，所以部署在云主机上时兜底源是必需的。
    """
    result = {"gold": {"price": None, "change_pct": None}, "oil": {"price": None, "change_pct": None}}
    try:
        raw = http_get(
            "https://hq.sinajs.cn/list=hf_GC,hf_CL",
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        # 解析黄金
        gc_m = re.search(r'hf_GC="([^"]+)"', raw)
        if gc_m:
            fields = gc_m.group(1).split(",")
            if len(fields) >= 9:
                price = fields[0].strip()
                # 注意：fields[5] 是当日最低价，fields[7] 才是昨收（2026-07-31 确认的 bug）
                prev_close = fields[7].strip()
                if price and prev_close:
                    p = float(price)
                    pc = float(prev_close)
                    result["gold"]["price"] = round(p, 2)
                    if pc != 0:
                        result["gold"]["change_pct"] = round((p - pc) / pc * 100, 2)
        # 解析原油
        cl_m = re.search(r'hf_CL="([^"]+)"', raw)
        if cl_m:
            fields = cl_m.group(1).split(",")
            if len(fields) >= 9:
                price = fields[0].strip()
                # 注意：fields[5] 是当日最低价，fields[7] 才是昨收（2026-07-31 确认的 bug）
                prev_close = fields[7].strip()
                if price and prev_close:
                    p = float(price)
                    pc = float(prev_close)
                    result["oil"]["price"] = round(p, 2)
                    if pc != 0:
                        result["oil"]["change_pct"] = round((p - pc) / pc * 100, 2)
    except Exception:
        pass

    for key, symbol in (("gold", "@GC.1"), ("oil", "@CL.1")):
        if result[key]["price"] is None:
            try:
                result[key] = fetch_cnbc_quote(symbol)
            except Exception:
                pass
    return result


# ════════════════════════════════════════════
# 📅 经济数据日历
# ════════════════════════════════════════════

def fetch_economic_calendar(days: int = 7) -> list[dict[str, str]]:
    """经济数据日历（未来 days 天）"""
    # ForexFactory 页面结构已变，暂时返回空
    return []


# ════════════════════════════════════════════
# 🔍 信号评分引擎
# ════════════════════════════════════════════

def calculate_signal_score(
    portfolio_relevance: int = 50,
    market_impact: int = 50,
    timeliness: int = 50,
    source_credibility: int = 50,
    uniqueness: int = 50,
) -> dict[str, Any]:
    """计算信号强度评分 (0-100)"""
    score = (
        portfolio_relevance * 0.35 +
        market_impact * 0.25 +
        timeliness * 0.20 +
        source_credibility * 0.10 +
        uniqueness * 0.10
    )
    score = round(score, 1)

    if score >= 85:
        level = "CRITICAL"
        emoji = "🚨"
    elif score >= 70:
        level = "HIGH"
        emoji = "⚠️"
    elif score >= 50:
        level = "MEDIUM"
        emoji = "📌"
    else:
        level = "LOW"
        emoji = "💡"

    return {
        "score": score,
        "level": level,
        "emoji": emoji,
        "details": {
            "portfolio_relevance": portfolio_relevance,
            "market_impact": market_impact,
            "timeliness": timeliness,
            "source_credibility": source_credibility,
            "uniqueness": uniqueness,
        },
    }


# ════════════════════════════════════════════
# 🧰 工具函数
# ════════════════════════════════════════════

def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ════════════════════════════════════════════
# 📋 CLI 命令
# ════════════════════════════════════════════

def cmd_pboc() -> None:
    items = fetch_pboc()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_pboc_rate() -> None:
    items = fetch_pboc_rate()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_pboc_speech() -> None:
    items = fetch_pboc_speech()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}{SEP}{item.get('date','')}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_nbs() -> None:
    items = fetch_nbs()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_cctv() -> None:
    items = fetch_cctv_news()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_fed() -> None:
    items = fetch_fed()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}{SEP}{item.get('date','')}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_fed_speeches() -> None:
    items = fetch_fed_speeches()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}{SEP}{item.get('date','')}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_fomc() -> None:
    items = fetch_fomc()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}{SEP}{item.get('date','')}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_bls() -> None:
    items = fetch_bls()
    for item in items:
        print(f"{item['title']}{SEP}{item['url']}{SEP}{item['source']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_vix() -> None:
    result = fetch_vix()
    print(json.dumps(result, ensure_ascii=False))


def cmd_treasury() -> None:
    result = fetch_treasury_yields()
    print(json.dumps(result, ensure_ascii=False))


def cmd_usdcny() -> None:
    result = fetch_usd_cny()
    print(json.dumps(result, ensure_ascii=False))


def cmd_gold_oil() -> None:
    result = fetch_gold_oil()
    print(json.dumps(result, ensure_ascii=False))


def cmd_calendar() -> None:
    items = fetch_economic_calendar()
    for item in items:
        print(f"{item['datetime']}{SEP}{item['title']}{SEP}{item['impact']}")
    if not items:
        print(f"暂无数据{SEP}{SEP}")


def cmd_score() -> None:
    if len(sys.argv) > 2:
        try:
            params = json.loads(sys.argv[2])
            result = calculate_signal_score(
                params.get("relevance", 50),
                params.get("impact", 50),
                params.get("timeliness", 50),
                params.get("credibility", 50),
                params.get("uniqueness", 50),
            )
        except (json.JSONDecodeError, KeyError):
            result = {"error": "Invalid JSON params. Usage: score '{\"relevance\":80,\"impact\":70,...}'"}
    else:
        result = calculate_signal_score()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_all() -> None:
    """一键获取所有宏观指标（用于紧急检查）"""
    result: dict[str, Any] = {
        "timestamp": now_cn(),
        "vix": fetch_vix(),
        "treasury": fetch_treasury_yields(),
        "usd_cny": fetch_usd_cny(),
        "gold_oil": fetch_gold_oil(),
        "pboc": fetch_pboc()[:3],
        "pboc_rate": fetch_pboc_rate()[:3],
        "pboc_speech": fetch_pboc_speech()[:3],
        "fed": fetch_fed()[:3],
        "fed_speeches": fetch_fed_speeches()[:3],
        "fomc": fetch_fomc()[:3],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_help() -> None:
    print(__doc__)


COMMANDS = {
    "pboc": cmd_pboc,
    "pboc_rate": cmd_pboc_rate,
    "pboc_speech": cmd_pboc_speech,
    "nbs": cmd_nbs,
    "cctv": cmd_cctv,
    "fed": cmd_fed,
    "fed_speeches": cmd_fed_speeches,
    "fomc": cmd_fomc,
    "bls": cmd_bls,
    "vix": cmd_vix,
    "treasury": cmd_treasury,
    "usdcny": cmd_usdcny,
    "gold_oil": cmd_gold_oil,
    "calendar": cmd_calendar,
    "score": cmd_score,
    "all": cmd_all,
    "help": cmd_help,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write(__doc__ or "")
        return 1
    try:
        COMMANDS[sys.argv[1]]()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
