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

import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 15
SEP = "|||"
CN_TZ = timezone(timedelta(hours=8))

OFFICIAL_CONTENT_MAX_BYTES = 1_500_000
OFFICIAL_CONTENT_MAX_CHARS = 4_000
OFFICIAL_SECTION_MAX_CHARS = 700
OFFICIAL_SECTION_LIMIT = 8

_OFFICIAL_CONTENT_HOSTS = {
    "federalreserve.gov",
    "www.federalreserve.gov",
    "pbc.gov.cn",
    "www.pbc.gov.cn",
}
_FED_CONTENT_PATH_RE = re.compile(
    r"^/newsevents/(?:pressreleases|speech)/[a-z0-9._~-]+\.htm$",
    re.IGNORECASE,
)
_PBOC_CONTENT_PATH_RE = re.compile(
    r"^/zhengcehuobisi/(?:[0-9]+/)+index\.html$",
    re.IGNORECASE,
)


def _normalized_official_policy_url(url: Any) -> str:
    """Return a canonical allowlisted policy-page URL, or an empty string.

    The official-page fetcher is intentionally much narrower than the generic
    HTTP helpers in this module.  It accepts only known article paths, rejects
    credentials and custom ports, and is also applied to every redirect.
    """
    if not isinstance(url, str) or len(url) > 2_048:
        return ""
    candidate = unescape(url).strip()
    if not candidate or "\\" in candidate:
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return ""

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        host not in _OFFICIAL_CONTENT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        return ""

    path = parsed.path or "/"
    decoded_path = path
    for _ in range(3):
        decoded_path = urllib.parse.unquote(decoded_path)
    if ".." in decoded_path.split("/") or "\\" in decoded_path:
        return ""

    if host.endswith("federalreserve.gov"):
        if scheme != "https" or not _FED_CONTENT_PATH_RE.fullmatch(path):
            return ""
    elif host.endswith("pbc.gov.cn"):
        if scheme not in {"http", "https"} or not _PBOC_CONTENT_PATH_RE.fullmatch(path):
            return ""
        # The historical listing exposed http links, but the official site
        # supports TLS.  Upgrade before any DNS/connect step so plaintext
        # content can never be labelled as verified official evidence.
        scheme = "https"
    else:
        return ""

    return urllib.parse.urlunsplit((scheme, host, path, parsed.query, ""))


def is_supported_official_policy_url(url: Any) -> bool:
    """Whether *url* is an allowlisted Fed/PBoC HTML policy article."""
    return bool(_normalized_official_policy_url(url))


def _official_policy_url_identity(url: Any) -> tuple[str, str] | None:
    normalized = _normalized_official_policy_url(url)
    if not normalized:
        return None
    parsed = urllib.parse.urlsplit(normalized)
    host = parsed.hostname or ""
    family = "fed" if host.endswith("federalreserve.gov") else "pboc"
    return family, parsed.path


def same_official_policy_article(first: Any, second: Any) -> bool:
    """Require two evidence URLs to identify the same institution and path."""
    first_identity = _official_policy_url_identity(first)
    return first_identity is not None and first_identity == _official_policy_url_identity(second)


def _host_resolves_publicly(host: str, port: int) -> bool:
    """Reject local, private, reserved and otherwise non-public DNS results."""
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (OSError, socket.gaierror):
        return False
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        return False


class _OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep redirects inside the same narrow official-page allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        normalized = _normalized_official_policy_url(newurl)
        if not normalized or not same_official_policy_article(req.full_url, normalized):
            raise urllib.error.HTTPError(
                newurl,
                code,
                "Refused redirect outside the original official policy article",
                headers,
                fp,
            )
        parsed = urllib.parse.urlsplit(normalized)
        if not _host_resolves_publicly(parsed.hostname or "", 443 if parsed.scheme == "https" else 80):
            raise urllib.error.URLError("Official host did not resolve to a public address")
        return super().redirect_request(req, fp, code, msg, headers, normalized)


def _download_official_html(url: str, timeout: int = 10) -> tuple[bytes, str, str]:
    """Download one validated official HTML page with bounded memory use."""
    normalized = _normalized_official_policy_url(url)
    if not normalized:
        raise ValueError("unsupported official policy URL")
    parsed = urllib.parse.urlsplit(normalized)
    port = 443 if parsed.scheme == "https" else 80
    if not _host_resolves_publicly(parsed.hostname or "", port):
        raise urllib.error.URLError("Official host did not resolve to a public address")

    request = urllib.request.Request(
        normalized,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _OfficialRedirectHandler(),
    )
    with opener.open(request, timeout=timeout) as response:
        final_url = _normalized_official_policy_url(response.geturl())
        if not final_url:
            raise urllib.error.URLError("Unexpected final URL")
        content_type = (response.headers.get_content_type() or "").lower()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ValueError(f"unsupported content type: {content_type}")
        raw = response.read(OFFICIAL_CONTENT_MAX_BYTES + 1)
        if len(raw) > OFFICIAL_CONTENT_MAX_BYTES:
            raise ValueError("official policy page exceeded size limit")
        charset = response.headers.get_content_charset() or ""
    return raw, final_url, charset


def _decode_official_html(raw: bytes, declared_charset: str = "") -> str:
    """Decode official pages, preferring UTF-8 over occasionally wrong headers."""
    if not raw:
        return ""
    candidates = ["utf-8"]
    prefix = raw[:8_192].decode("ascii", errors="ignore")
    meta = re.search(
        r"charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)",
        prefix,
        flags=re.IGNORECASE,
    )
    for candidate in (meta.group(1) if meta else "", declared_charset, "gb18030"):
        normalized = candidate.strip().lower()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    for encoding in candidates:
        try:
            return raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", unescape(value)).strip()
    # Tables on the PBoC site split decimals and units across nested spans.
    # Rejoin only unambiguous numeric punctuation/units for readable evidence.
    text = re.sub(r"(?<=\d)\s*[.．]\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s+(?=[%％])", "", text)
    text = re.sub(
        r"(?<=\d)\s+(?=(?:亿元|万亿元|万元|基点|天|个月)(?:\s|$|\||，|。))",
        "",
        text,
    )
    return text


class _OfficialArticleParser(HTMLParser):
    """Extract bounded paragraphs and table rows from official policy pages."""

    _SKIP_TAGS = {
        "script", "style", "noscript", "svg", "nav", "header", "footer",
        "form", "button", "template",
    }
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
    _BLOCK_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4"}
    _NEGATIVE_TOKENS = {
        "breadcrumb", "footer", "header", "menu", "nav", "print", "search",
        "share", "social", "toolbar", "custom-banner",
    }
    _PRIORITY_TOKENS = {
        "article", "content", "detail", "main", "trs_editor", "zoom",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[tuple[str, str, bool]] = []
        self._stack: list[tuple[str, bool, bool]] = []
        self._skip_depth = 0
        self._priority_depth = 0
        self._blocks: list[tuple[str, list[str], bool]] = []
        self._row_cells: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self.priority_text_parts: list[str] = []
        self._priority_text_length = 0

    @staticmethod
    def _attribute_blob(attrs: list[tuple[str, str | None]]) -> str:
        return " ".join(
            value.lower()
            for key, value in attrs
            if key.lower() in {"id", "class", "role"} and value
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        blob = self._attribute_blob(attrs)
        skip_here = tag in self._SKIP_TAGS or any(token in blob for token in self._NEGATIVE_TOKENS)
        priority_here = not skip_here and any(token in blob for token in self._PRIORITY_TOKENS)
        is_void = tag in self._VOID_TAGS
        if skip_here and not is_void:
            self._skip_depth += 1
        if priority_here and not is_void:
            self._priority_depth += 1
        if not is_void:
            self._stack.append((tag, skip_here, priority_here))
        if self._skip_depth or (is_void and skip_here):
            return
        if tag in self._BLOCK_TAGS:
            self._blocks.append((tag, [], self._priority_depth > 0))
        elif tag == "tr":
            self._row_cells = []
            self._cell_parts = None
        elif tag in {"td", "th"} and self._row_cells is not None:
            self._cell_parts = []
        elif tag == "br":
            self._append_text(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def _append_text(self, data: str) -> None:
        if self._blocks:
            self._blocks[-1][1].append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._append_text(data)
            if self._priority_depth and self._priority_text_length < 20_000:
                remaining = 20_000 - self._priority_text_length
                chunk = data[:remaining]
                self.priority_text_parts.append(chunk)
                self._priority_text_length += len(chunk)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._skip_depth:
            if tag in {"td", "th"} and self._row_cells is not None and self._cell_parts is not None:
                cell = _plain_text(" ".join(self._cell_parts))
                if cell:
                    self._row_cells.append(cell)
                self._cell_parts = None
            elif tag == "tr" and self._row_cells is not None:
                row = _plain_text(" | ".join(self._row_cells))
                if row:
                    self.sections.append(("table_row", row, self._priority_depth > 0))
                self._row_cells = None
                self._cell_parts = None
            if tag in self._BLOCK_TAGS:
                for index in range(len(self._blocks) - 1, -1, -1):
                    block_tag, parts, priority = self._blocks[index]
                    if block_tag == tag:
                        del self._blocks[index]
                        text = _plain_text(" ".join(parts))
                        if text:
                            self.sections.append(("paragraph", text, priority))
                        break

        marker_index = next(
            (index for index in range(len(self._stack) - 1, -1, -1) if self._stack[index][0] == tag),
            None,
        )
        if marker_index is None:
            return
        removed = self._stack[marker_index:]
        del self._stack[marker_index:]
        self._skip_depth = max(0, self._skip_depth - sum(1 for _, skip, _ in removed if skip))
        self._priority_depth = max(
            0,
            self._priority_depth - sum(1 for _, _, priority in removed if priority),
        )


_OFFICIAL_BOILERPLATE = (
    "skip to main content",
    "board of governors of the federal reserve system",
    "for media inquiries",
    "website owner",
    "网站主办单位",
    "建议使用",
    "打印本页",
    "关闭窗口",
    "我的位置",
    "字号 大 中 小",
    "文章来源：",
)

_OFFICIAL_CHALLENGE_TOKENS = (
    "access denied",
    "request blocked",
    "service unavailable",
    "temporarily unavailable",
    "enable cookies",
    "enable javascript",
    "security check",
    "captcha",
    "验证码",
    "拒绝访问",
    "访问频繁",
    "页面不存在",
    "系统维护",
    "请稍后再试",
)


def _extract_official_article(html: str) -> tuple[str, list[dict[str, str]]]:
    parser = _OfficialArticleParser()
    parser.feed(html)
    parser.close()

    candidates = [entry for entry in parser.sections if entry[2]]
    has_priority_content = sum(len(text) for _, text, _ in candidates) >= 40
    priority_text = _plain_text(" ".join(parser.priority_text_parts))
    if sum(len(text) for _, text, _ in candidates) < 40 and len(priority_text) >= 40:
        candidates = [("paragraph", priority_text, True)]
        has_priority_content = True
    if sum(len(text) for _, text, _ in candidates) < 40:
        candidates = parser.sections

    cleaned: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for index, (kind, raw_text, _) in enumerate(candidates):
        text = _plain_text(raw_text)
        lowered = text.casefold()
        minimum = 8 if kind == "table_row" else 20
        if len(text) < minimum or any(token in lowered for token in _OFFICIAL_BOILERPLATE):
            continue
        dedup_key = re.sub(r"\W+", "", lowered)
        if not dedup_key or dedup_key in seen:
            continue
        seen.add(dedup_key)
        cleaned.append((index, kind, text[:OFFICIAL_SECTION_MAX_CHARS]))

    def section_score(entry: tuple[int, str, str]) -> tuple[int, int]:
        index, kind, text = entry
        blob = text.casefold()
        score = 2 if kind == "table_row" else 0
        if re.search(r"\d", text):
            score += 2
        if any(token in blob for token in ("voting", "voted", "dissent", "表决", "投票", "异议")):
            score += 10
        if any(token in blob for token in (
            "target range", "federal funds rate", "basis point", "percent",
            "利率", "降息", "加息", "逆回购", "中标量", "亿元", "%",
        )):
            score += 8
        if any(token in blob for token in (
            "inflation", "employment", "unemployment", "balance sheet",
            "通胀", "就业", "流动性", "准备金", "lpr", "mlf",
        )):
            score += 4
        # Preserve some opening context when scores otherwise tie.
        score += max(0, 2 - index // 3)
        return score, -index

    chosen = sorted(cleaned, key=section_score, reverse=True)[:OFFICIAL_SECTION_LIMIT]
    chosen.sort(key=lambda entry: entry[0])
    selected = [
        {"kind": kind, "text": text}
        for _, kind, text in chosen
    ]

    excerpt = "\n".join(section["text"] for section in selected)
    excerpt = excerpt[:OFFICIAL_CONTENT_MAX_CHARS].strip()
    challenge_blob = excerpt.casefold()
    if (
        len(excerpt) < 40
        or any(token in challenge_blob for token in _OFFICIAL_CHALLENGE_TOKENS)
        or (
            not has_priority_content
            and (len(selected) < 2 or len(excerpt) < 240)
        )
    ):
        return "", []
    return excerpt, selected


def fetch_official_policy_content(url: Any) -> dict[str, Any]:
    """Fetch and extract one allowlisted Fed/PBoC article.

    Failure is explicit and non-fatal.  ``unavailable`` means this collection
    attempt could not read the official body; it never means the publisher did
    not disclose details.
    """
    normalized = _normalized_official_policy_url(url)
    if not normalized:
        return {"content_status": "unsupported"}
    unavailable = {
        "content_status": "unavailable",
        "content_source_url": normalized,
    }
    try:
        raw, final_url, charset = _download_official_html(normalized)
        if not same_official_policy_article(normalized, final_url):
            return unavailable
        html = _decode_official_html(raw, charset)
        excerpt, sections = _extract_official_article(html)
    except Exception:
        return unavailable
    if not excerpt:
        return unavailable
    return {
        "content_status": "ready",
        "content_excerpt": excerpt,
        "content_source_url": final_url,
        "evidence_sections": sections,
    }


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

_POLICY_URL_STAMP_RE = re.compile(r"/((?:19|20)\d{12})\d*/")


def _sort_policy_items_newest(items: list[dict[str, str]]) -> list[dict[str, str]]:
    def key(item: dict[str, str]) -> str:
        match = _POLICY_URL_STAMP_RE.search(str(item.get("url") or ""))
        return match.group(1) if match else ""

    return sorted(items, key=key, reverse=True)

def fetch_pboc() -> list[dict[str, str]]:
    """中国人民银行 — 最新货币政策/利率公告"""
    items = []
    try:
        raw = http_get_raw("https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html")
        # 页面是 UTF-8 编码
        html = raw.decode("utf-8", errors="ignore")
        # 提取公告列表：<a href="..." title="..."> 或 <a href="...">公告标题</a>
        # 优先找包含日期ID的链接（实际公告）
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
            url, text = m.group(1), m.group(2).strip()
            # 过滤导航链接，只保留实际公告（URL 含日期数字）
            if re.search(r'/20\d{12,}', url) and len(text) > 5:
                full_url = (
                    "https://" + url.split("://", 1)[1]
                    if url.startswith("http://")
                    else url if url.startswith("https://")
                    else "https://www.pbc.gov.cn" + url
                )
                items.append({
                    "title": text,
                    "url": full_url,
                    "source": "中国人民银行",
                    "category": "pboc_announcement",
                })
    except Exception:
        pass
    return _sort_policy_items_newest(items)[:24]


def fetch_pboc_rate() -> list[dict[str, str]]:
    """中国人民银行 — 最新利率政策（含LPR/存贷款基准利率）"""
    items = []
    try:
        raw = http_get_raw("https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/index.html")
        html = raw.decode("utf-8", errors="ignore")
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
            url, text = m.group(1), m.group(2).strip()
            if re.search(r'/20\d{12,}', url) and len(text) > 5:
                full_url = (
                    "https://" + url.split("://", 1)[1]
                    if url.startswith("http://")
                    else url if url.startswith("https://")
                    else "https://www.pbc.gov.cn" + url
                )
                items.append({
                    "title": text,
                    "url": full_url,
                    "source": "中国人民银行-利率政策",
                    "category": "pboc_announcement",
                })
    except Exception:
        pass
    return _sort_policy_items_newest(items)[:24]


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

def _xml_local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _rss_child_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _xml_local_name(child.tag) == name:
            return _plain_text("".join(child.itertext()))
    return ""


def _strip_feed_markup(value: str) -> str:
    if not value:
        return ""
    parser = _OfficialArticleParser()
    try:
        parser.feed(f"<p>{value}</p>")
        parser.close()
    except Exception:
        return _plain_text(re.sub(r"<[^>]*>", " ", value))
    for kind, text, _ in parser.sections:
        if kind == "paragraph":
            return _plain_text(text)
    return _plain_text(re.sub(r"<[^>]*>", " ", value))


def _policy_category(title: str, source: str, url: str = "") -> str:
    blob = f"{title} {source} {url}".casefold()
    if "speech" in source.casefold() or "/newsevents/speech/" in url.casefold():
        return "fed_speech"
    if ("minutes" in blob or "纪要" in blob) and (
        "fomc" in blob
        or "federal open market committee" in blob
        or "fomc" in source.casefold()
    ):
        return "fomc_minutes"
    if "fomc" in blob and ("statement" in blob or "声明" in blob):
        return "fomc_statement"
    if "fomc" in source.casefold() or "monetary" in source.casefold():
        return "policy_update"
    if "federal reserve" in source.casefold():
        return "fed_press_release"
    if "中国人民银行" in source or "pbc.gov.cn" in url.casefold():
        return "pboc_announcement"
    return "policy_update"


def _parse_policy_rss(
    raw: str,
    *,
    source: str,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Parse RSS as XML so CDATA, entity escaping and element order are safe."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items: list[dict[str, str]] = []
    for node in root.iter():
        if _xml_local_name(node.tag) != "item":
            continue
        title = _rss_child_text(node, "title")
        link = _rss_child_text(node, "link")
        pub_date = _rss_child_text(node, "pubdate")
        if not title or not link:
            continue
        item = {
            "title": title,
            "url": link,
            "source": source,
            "date": pub_date,
            "category": _policy_category(title, source, link),
        }
        description = _strip_feed_markup(_rss_child_text(node, "description"))
        normalized_title = re.sub(r"\W+", "", title).casefold()
        normalized_description = re.sub(r"\W+", "", description).casefold()
        if (
            len(description) >= 20
            and normalized_description
            and normalized_description != normalized_title
        ):
            item["snippet"] = description[:2_200]
        items.append(item)
        if len(items) >= max(1, min(int(limit), 64)):
            break
    return items


_FED_RELEASE_NOISE_PHRASES = (
    "announces approval of the application",
    "issues enforcement action",
    "issues enforcement actions",
    "terminates enforcement action",
    "written agreement with",
    "consent order against",
)
_FED_MACRO_RELEASE_PHRASES = (
    "fomc",
    "monetary policy",
    "discount rate",
    "discount window",
    "federal funds",
    "interest rate",
    "inflation",
    "employment",
    "economic outlook",
    "financial stability",
    "stress test",
    "capital requirement",
    "liquidity requirement",
    "reserve requirement",
    "balance sheet",
    "senior loan officer",
    "beige book",
    "emergency facility",
    "credit facility",
)


def _is_macro_relevant_fed_release(item: dict[str, str]) -> bool:
    title = str(item.get("title") or "").casefold()
    return (
        bool(title)
        and not any(phrase in title for phrase in _FED_RELEASE_NOISE_PHRASES)
        and any(phrase in title for phrase in _FED_MACRO_RELEASE_PHRASES)
    )

def fetch_fed() -> list[dict[str, str]]:
    """美联储 — 最新声明/讲话（通过 RSS Feed）"""
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/press_all.xml")
        parsed = _parse_policy_rss(raw, source="Federal Reserve", limit=32)
        return [item for item in parsed if _is_macro_relevant_fed_release(item)][:8]
    except Exception:
        return []


def fetch_fed_speeches() -> list[dict[str, str]]:
    """美联储官员 — 最新讲话（鲍威尔/理事/行长，通过 Speeches RSS）"""
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/speeches.xml")
        return _parse_policy_rss(raw, source="Fed Speech")
    except Exception:
        return []


def fetch_fomc() -> list[dict[str, str]]:
    """FOMC — 最新声明/纪要/点阵图（通过 Monetary Policy RSS）"""
    try:
        raw = http_get("https://www.federalreserve.gov/feeds/press_monetary.xml")
        return _parse_policy_rss(raw, source="FOMC Monetary Policy")
    except Exception:
        return []


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
