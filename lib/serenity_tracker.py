#!/usr/bin/env python3
"""
Serenity (@aleabitoreddit) 动态跟踪脚本
从 x.com 抓取最新推文，输出格式化报告
"""

import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

HANDLE = "aleabitoreddit"
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "serenity"
CACHE_DIR = os.environ.get("SERENITY_CACHE_DIR", str(_DEFAULT_CACHE_DIR))
STATE_FILE = os.path.join(CACHE_DIR, "tracking_state.json")
MAX_TWEETS = 15
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TWEET_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
_TWITTER_EPOCH = datetime(2006, 3, 21, tzinfo=timezone.utc)
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class SourceParseError(RuntimeError):
    """A fetched source returned content but no trustworthy post records."""

    code = "source_parse_empty"


def _validated_handle(handle):
    candidate = str(handle or "").strip().lstrip("@")
    if not _HANDLE_RE.fullmatch(candidate):
        raise ValueError("invalid_x_handle")
    return candidate


def fetch_page(handle=HANDLE):
    handle = _validated_handle(handle)
    url = f"https://x.com/{handle}"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")

def _extract_nested_div(html_fragment, start_pos):
    """Extract content inside a div starting at start_pos, handling nesting."""
    depth = 1
    pos = start_pos
    while depth > 0 and pos < len(html_fragment):
        tag = re.search(r'</?(\w+)[^>]*>', html_fragment[pos:])
        if not tag:
            break
        tag_start = pos + tag.start()
        tag_end = pos + tag.end()
        tag_name = tag.group(1)
        is_closing = html_fragment[tag_start+1:tag_start+2] == '/'

        if tag_name == 'div':
            if is_closing:
                depth -= 1
                if depth == 0:
                    return html_fragment[start_pos:tag_start]
            else:
                depth += 1
        pos = tag_end
    return html_fragment[start_pos:]


def _parse_legacy_dom_tweets(html_content, handle):
    """Parse the older server-rendered ``data-tweet-id`` article shape."""
    tweets = []

    for m in re.finditer(r'<article[^>]*data-tweet-id="(\d+)"[^>]*>(.*?)</article>', html_content, re.DOTALL):
        tid = m.group(1)
        if not _TWEET_ID_RE.fullmatch(tid):
            continue
        article = m.group(2)

        # Extract tweet text from the precise div structure
        text = ""
        tm = re.search(
            r'<div dir="auto" class="font-chirp max-w-full whitespace-pre-wrap break-words text-text text-body font-normal">',
            article
        )
        if tm:
            inner = _extract_nested_div(article, tm.end())
            inner = re.sub(r'<[^>]+>', '', inner)
            inner = html_mod.unescape(inner)
            inner = re.sub(r'\s+', ' ', inner).strip()
            text = inner

        # Extract date
        date_match = re.search(
            r'<a[^>]*href="/' + re.escape(handle) + r'/status/' + tid + r'"[^>]*>([^<]+)</a>',
            article
        )
        if not date_match:
            continue
        date_str = date_match.group(1).strip() if date_match else ""

        # Clean up artifacts
        text = re.sub(r'\s*Show more\s*', ' ', text)
        text = re.sub(r'\s*This post is unavailable\.?\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if text and len(text) > 15:
            tweets.append({
                "tid": tid,
                "date": date_str,
                "text": text,
                "handle": handle,
                "url": f"https://x.com/{handle}/status/{tid}",
            })

    return tweets[:MAX_TWEETS]


_RSC_ENTITY_RE = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])*"|[A-Za-z0-9_+/=.-]+)'
    r':\$R\[\d+\]=\{\s*__id:(?P<entity_id>"(?:\\.|[^"\\])*")'
)


def _decode_js_string(literal):
    try:
        value = json.loads(literal)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) else None


def _rsc_object_end(value, start):
    """Return the end of one RSC object without trusting braces in strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
    return None


def _rsc_entities(payload):
    """Index small RSC entity objects by their self-declared ``__id``."""
    entities = {}
    for match in _RSC_ENTITY_RE.finditer(payload):
        raw_key = match.group("key")
        key = _decode_js_string(raw_key) if raw_key.startswith('"') else raw_key
        entity_id = _decode_js_string(match.group("entity_id"))
        if not key or key != entity_id:
            continue
        object_start = payload.find("{", match.start(), match.end())
        if object_start < 0:
            continue
        object_end = _rsc_object_end(payload, object_start)
        if object_end is None:
            continue
        body = payload[object_start:object_end]
        if len(body) > len(entities.get(entity_id, "")):
            entities[entity_id] = body
    return entities


def _rsc_ref(body, field):
    match = re.search(
        rf'(?<![A-Za-z0-9_]){re.escape(field)}:\$R\[\d+\]='
        r'\{__ref:("(?:\\.|[^"\\])*")\}',
        body,
    )
    return _decode_js_string(match.group(1)) if match else None


def _rsc_string(body, field):
    match = re.search(
        rf'(?<![A-Za-z0-9_]){re.escape(field)}:'
        r'("(?:\\.|[^"\\])*")',
        body,
    )
    return _decode_js_string(match.group(1)) if match else None


def _rsc_integer(body, field):
    match = re.search(
        rf'(?<![A-Za-z0-9_]){re.escape(field)}:(\d+)(?!\d)',
        body,
    )
    return int(match.group(1)) if match else None


def _entity_has_type(body, expected):
    return _rsc_string(body, "__typename") == expected


def _profile_status_link_present(payload, handle, tid):
    path = rf'/{re.escape(handle)}/status/{re.escape(tid)}'
    return bool(
        re.search(
            rf'(?:data-href|href)="(?:https://(?:www\.)?x\.com)?{path}'
            r'(?=["/?#])',
            payload,
            re.IGNORECASE,
        )
    )


def _timestamp_from_ms(value, now):
    if value is None or value < 0:
        return None
    try:
        stamp = datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if stamp < _TWITTER_EPOCH or stamp > now + _MAX_FUTURE_SKEW:
        return None
    return stamp.replace(microsecond=0).isoformat()


def _parse_rsc_tweets(payload, handle, now):
    """Resolve current X TweetResults/RSC records through their author graph."""
    entities = _rsc_entities(payload)
    root_ids = list(dict.fromkeys(re.findall(
        r'entry_id:"tweet-([1-9]\d{0,19})"', payload
    )))
    tweets = []

    for tid in root_ids:
        if not _TWEET_ID_RE.fullmatch(tid):
            continue
        if not _profile_status_link_present(payload, handle, tid):
            continue

        results = entities.get(f"TweetResults:{tid}", "")
        if (
            not _entity_has_type(results, "TweetResults")
            or _rsc_string(results, "rest_id") != tid
        ):
            continue
        tweet_ref = _rsc_ref(results, "result")
        tweet = entities.get(tweet_ref or "", "")
        if (
            not _entity_has_type(tweet, "Tweet")
            or _rsc_string(tweet, "rest_id") != tid
        ):
            continue

        core = entities.get(_rsc_ref(tweet, "core") or "", "")
        details = entities.get(_rsc_ref(tweet, "details") or "", "")
        if not _entity_has_type(core, "TweetCore"):
            continue
        user_results = entities.get(_rsc_ref(core, "user_results") or "", "")
        if not _entity_has_type(user_results, "UserResults"):
            continue
        user = entities.get(_rsc_ref(user_results, "result") or "", "")
        if not _entity_has_type(user, "User"):
            continue
        user_core = entities.get(_rsc_ref(user, "core") or "", "")
        author = _rsc_string(user_core, "screen_name")
        if (
            not _entity_has_type(user_core, "UserCore")
            or not author
            or author.casefold() != handle.casefold()
        ):
            continue

        if not _entity_has_type(details, "TBirdData"):
            continue
        text = _rsc_string(details, "full_text")
        published_at = _timestamp_from_ms(
            _rsc_integer(details, "created_at_ms"), now
        )
        if not text or not published_at:
            continue
        text = re.sub(r"\s+", " ", html_mod.unescape(text)).strip()
        if len(text) <= 15:
            continue
        tweets.append({
            "tid": tid,
            "published_at": published_at,
            "date": published_at,
            "text": text,
            "handle": handle,
            "url": f"https://x.com/{handle}/status/{tid}",
        })
        if len(tweets) >= MAX_TWEETS:
            break
    return tweets


def parse_tweets(html_content, handle=HANDLE, now=None):
    """Parse both legacy X DOM and current TweetResults/RSC responses."""
    handle = _validated_handle(handle)
    if not isinstance(html_content, str) or not html_content:
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(timezone.utc)

    combined = _parse_legacy_dom_tweets(html_content, handle)
    combined.extend(_parse_rsc_tweets(html_content, handle, current))
    unique = []
    seen = set()
    for tweet in combined:
        tid = tweet.get("tid")
        if tid in seen:
            continue
        seen.add(tid)
        unique.append(tweet)
    return unique[:MAX_TWEETS]


def fetch_tweets(handle=HANDLE):
    """Fetch one X profile and fail observably when no parser recognizes it."""
    handle = _validated_handle(handle)
    payload = fetch_page(handle)
    tweets = parse_tweets(payload, handle=handle)
    if not tweets:
        raise SourceParseError("source_parse_empty")
    return tweets

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_tid": None, "last_checked": None}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"last_tid": None, "last_checked": None}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def extract_tickers(tweets):
    """提取所有 $TICKER"""
    us_stocks = set()

    known_tickers = {
        "AAOI", "AAPL", "AMZN", "APLD", "AXTI", "BITF", "CIFR", "CLSK",
        "COHR", "CRWV", "FI", "GOOGL", "GRRR", "HIVE", "HOOD", "HUT",
        "IQE", "IREN", "LGN", "LITE", "LYC", "META", "MSFT", "MU",
        "NBIS", "NVDA", "ORCL", "RIOT", "SIVE", "SOI", "TSEM", "TSLA",
        "WULF", "WYFI", "XFAB", "INTC"
    }

    for t in tweets:
        for m in re.finditer(r'\$([A-Z]{2,5})(?:\b|(?=[^A-Za-z]))', t["text"]):
            ticker = m.group(1)
            if ticker in known_tickers or (len(ticker) >= 2 and len(ticker) <= 5):
                us_stocks.add(ticker)

    return us_stocks


def format_report(tweets, is_new=False):
    lines = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if is_new:
        lines.append(f"📡 *Serenity 新动态* ({now})")
        lines.append("X: https://x.com/aleabitoreddit")
        lines.append("")
    else:
        lines.append(f"📡 *Serenity 动态* — 最近 {len(tweets)} 条")
        lines.append("X: https://x.com/aleabitoreddit")
        lines.append("")

    for t in tweets:
        text = t["text"][:1000]
        text = re.sub(r'\$([A-Z]{2,5})', r'*\1*', text)
        link = f"https://x.com/aleabitoreddit/status/{t['tid']}"
        lines.append(f"*{t['date']}*")
        lines.append(text)
        lines.append(f"<{link}|🔗 查看原文>")
        lines.append("───")
        lines.append("")

    if is_new and tweets:
        us_stocks = extract_tickers(tweets)
        lines.append("*📊 归纳总结*")
        lines.append("")

        all_text = " ".join(t["text"] for t in tweets).lower()
        themes = []
        if "photonics" in all_text or "cpo" in all_text or "silicon photonic" in all_text:
            themes.append("CPO/硅光子")
        if "power semi" in all_text or "800v" in all_text or "800 vdc" in all_text:
            themes.append("800V 功率半导体")
        if "memory" in all_text or "hbm" in all_text or "dram" in all_text:
            themes.append("存储/HBM")
        if "neocloud" in all_text or "nebius" in all_text:
            themes.append("Neocloud")
        if "inp" in all_text or "substrate" in all_text:
            themes.append("InP 衬底/光子学供应链")
        if "robot" in all_text or "humanoid" in all_text:
            themes.append("人形机器人")
        if "glass" in all_text or "substrate" in all_text:
            themes.append("玻璃基板")

        if themes:
            lines.append(f"*核心主题:* {' | '.join(themes)}")
            lines.append("")

        if us_stocks:
            sorted_us = sorted(us_stocks)
            lines.append(f"*美股:* ${', $'.join(sorted_us)}")
            lines.append("")

        lines.append("*核心观点:*")
        main_tweet = max(tweets, key=lambda t: len(t["text"]))
        summary = main_tweet["text"][:300]
        summary = re.sub(r'\$([A-Z]{2,5})', r'*\1*', summary)
        lines.append(summary)
        lines.append("")
        lines.append("<https://x.com/aleabitoreddit|查看 Serenity X 主页>")

    return "\n".join(lines)

def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    state = load_state()

    try:
        tweets = fetch_tweets(HANDLE)
    except Exception as e:
        print(f"抓取失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not tweets:
        print("未找到推文", file=sys.stderr)
        sys.exit(1)

    last_tid = state.get("last_tid")
    new_tweets = [t for t in tweets if t["tid"] != last_tid] if last_tid else tweets[:5]

    state["last_tid"] = tweets[0]["tid"]
    save_state(state)

    if new_tweets:
        print(format_report(new_tweets, is_new=True))
    else:
        print("ℹ️ Serenity 状态更新 — 上次检查后无新推文")
        print(f"   最近帖子: {tweets[0]['date']}")
        print(format_report(tweets[:3], is_new=False))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = os.path.join(CACHE_DIR, f"raw_{today}.json")
    with open(log_file, "w") as f:
        json.dump(tweets, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
