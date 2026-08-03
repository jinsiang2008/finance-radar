#!/usr/bin/env python3
"""
Serenity (@aleabitoreddit) 动态跟踪脚本
从 x.com 抓取最新推文，输出格式化报告
"""

import sys, re, html as html_mod, json, os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

HANDLE = "aleabitoreddit"
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "serenity"
CACHE_DIR = os.environ.get("SERENITY_CACHE_DIR", str(_DEFAULT_CACHE_DIR))
STATE_FILE = os.path.join(CACHE_DIR, "tracking_state.json")
MAX_TWEETS = 15

def fetch_page():
    url = f"https://x.com/{HANDLE}"
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


def parse_tweets(html_content):
    """Parse tweets from x.com's React-rendered HTML."""
    tweets = []

    for m in re.finditer(r'<article[^>]*data-tweet-id="(\d+)"[^>]*>(.*?)</article>', html_content, re.DOTALL):
        tid = m.group(1)
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
            r'<a[^>]*href="/' + HANDLE + r'/status/' + tid + r'"[^>]*>([^<]+)</a>',
            article
        )
        date_str = date_match.group(1).strip() if date_match else ""

        # Clean up artifacts
        text = re.sub(r'\s*Show more\s*', ' ', text)
        text = re.sub(r'\s*This post is unavailable\.?\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if text and len(text) > 15:
            tweets.append({"tid": tid, "date": date_str, "text": text})

    return tweets[:MAX_TWEETS]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_tid": None, "last_checked": None}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
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
        lines.append(f"X: https://x.com/aleabitoreddit")
        lines.append("")
    else:
        lines.append(f"📡 *Serenity 动态* — 最近 {len(tweets)} 条")
        lines.append(f"X: https://x.com/aleabitoreddit")
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
        lines.append(f"<https://x.com/aleabitoreddit|查看 Serenity X 主页>")

    return "\n".join(lines)

def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    state = load_state()

    try:
        html_content = fetch_page()
        tweets = parse_tweets(html_content)
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
        print(f"ℹ️ Serenity 状态更新 — 上次检查后无新推文")
        print(f"   最近帖子: {tweets[0]['date']}")
        print(format_report(tweets[:3], is_new=False))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = os.path.join(CACHE_DIR, f"raw_{today}.json")
    with open(log_file, "w") as f:
        json.dump(tweets, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
