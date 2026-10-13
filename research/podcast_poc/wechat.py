"""Public third-party RSS ingestion for the local, free WeChat reading POC."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

# Support both `python poc.py` and module-based tests, sharing the same data root.
try:
    from . import poc
except ImportError:
    import poc

CONFIG = Path(__file__).with_name("wechat_sources.json")
BODY_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"
STALE_DAYS = 14


def source_config():
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    return value["sources"]


def normalize_url(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Article must have a public HTTP(S) original URL")
    if parsed.hostname.lower() == "mp.weixin.qq.com":
        # Keep the stable identity of long-form WeChat URLs; discard sharing tags.
        stable = {k: v for k, v in parse_qsl(parsed.query) if k in {"__biz", "mid", "idx", "sn"}}
        query = urlencode(sorted(stable.items()))
        return urlunsplit(("https", "mp.weixin.qq.com", parsed.path, query, ""))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def publish_time(value):
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        return None  # Do not invent a timezone or substitute the fetch time.
    return moment.astimezone(timezone.utc).isoformat()


def body_text(html):
    """Strip active HTML, preserve all visible text; never load remote images."""
    soup = BeautifulSoup(html or "", "html.parser")
    images = len(soup.find_all("img"))
    for tag in soup.find_all(["script", "style", "iframe", "noscript", "svg"]):
        tag.decompose()
    for tag in soup.find_all(["td", "th"]):
        tag.insert_after(" | ")
    for tag in soup.find_all(["br"]):
        tag.replace_with("\n")
    for tag in soup.find_all(["p", "div", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = [re.sub(r"[\t \u00a0]+", " ", text).strip() for text in soup.get_text("", strip=False).splitlines()]
    return [line for line in lines if line], images


def article_from_item(item, source):
    original_url = item.findtext("link") or ""
    canonical = normalize_url(original_url)
    eid = "wx-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
    content = item.findtext(BODY_TAG) or ""
    lines, image_count = body_text(content)
    description, _ = body_text(item.findtext("description") or "")
    plain = "\n".join(lines)
    body_chars = len(re.sub(r"\s+", "", plain))
    verification_page = any(marker in plain[:500] for marker in ("环境异常", "访问过于频繁", "请完成下方验证"))
    has_body = bool(content.strip()) and body_chars >= 200 and not verification_page
    note = "已取得第三方 RSS 提供的正文文本，未逐段对照微信原文，不能据此保证无删节。"
    if not has_body:
        note = "正文缺失、过短或为验证页面；保留源简介，不自动生成全文摘要。"
    if image_count:
        note += f" 来源含 {image_count} 张图片，未识别图片和图表内容。"
    segments = [{"id": f"p{i+1:04d}", "text": line} for i, line in enumerate(lines)] if has_body else []
    title = (item.findtext("title") or "未命名文章").strip()
    # Context and image count matter too: a changed title/coverage invalidates the draft.
    body_hash = hashlib.sha256(json.dumps([title, source["name"], lines, image_count, has_body], ensure_ascii=False).encode()).hexdigest()
    return {"id": eid, "content_kind": "wechat_article", "account": source["name"],
        "source_id": source["id"], "category": source["category"], "title": title,
        "published_at": publish_time(item.findtext("pubDate")), "published_at_raw": item.findtext("pubDate"),
        "source_url": original_url, "canonical_url": canonical, "feed_url": source["feed_url"],
        "provider": source["provider"], "source_kind": "external_rss_body" if has_body else "rss_metadata",
        "status": "body_ready" if has_body else "metadata_only", "language": "zh",
        "source_excerpt": "\n".join(description)[:500], "body_chars": body_chars, "image_count": image_count,
        "segments": segments, "body_hash": body_hash, "fetched_at": poc.now(),
        "coverage": {"scope": "source_text" if has_body else "metadata_only", "segments_total": len(segments),
                     "segments_processed": 0, "note": note}, "processing": {"paid_api_calls": 0}}


def upsert_article(article):
    path = poc.episode_path(article["id"])
    incoming_has_body = article.get("coverage", {}).get("scope") == "source_text" and bool(article.get("segments"))
    article["source_body_available"] = incoming_has_body
    article["source_checked_at"] = article.get("fetched_at")
    if path.is_file():
        old = poc.load_episode(article["id"])
        old_has_body = old.get("coverage", {}).get("scope") == "source_text" and bool(old.get("segments"))
        if old_has_body and not incoming_has_body:
            # An incomplete feed refresh is not a new article revision. Keep the
            # archived body and its review, with the body's real retrieval time.
            old.update(source_body_available=False, source_checked_at=article.get("fetched_at"),
                       source_body_note="本次订阅源未提供正文；显示上次保存的正文及对应摘要。")
            for key in ("source_excerpt", "source_url", "feed_rank"):
                old[key] = article.get(key)
            article = old
        elif old.get("body_hash") == article["body_hash"]:
            # Preserve processing and review on unchanged content, update retrieval evidence.
            for key in ("fetched_at", "published_at", "published_at_raw", "source_excerpt", "source_url", "feed_rank",
                        "source_body_available", "source_checked_at"):
                old[key] = article.get(key)
            old.pop("source_body_note", None)
            article = old
    poc.save_episode(article)
    return article


def ingest_feed(source, body, per_source=2):
    if not 1 <= per_source <= 20:
        raise ValueError("POC per-source limit must be 1–20")
    root = ET.fromstring(body)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Expected RSS channel; HTML verification pages are not article feeds")
    items = channel.findall("item")
    items.sort(key=lambda item: publish_time(item.findtext("pubDate")) or "", reverse=True)
    if not items:
        raise ValueError("RSS contains no articles")
    records, errors = [], []
    for rank, item in enumerate(items[:per_source]):
        try:
            article = article_from_item(item, source)
            article["feed_rank"] = rank
            records.append(upsert_article(article))
        except ValueError as exc:
            errors.append(str(exc))
    if not records:
        raise ValueError("No valid article identities: " + "; ".join(errors))
    latest = max((a["published_at"] for a in records if a["published_at"]), default=None)
    age_days = None
    if latest:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(latest)).total_seconds() / 86400
    status = "ok" if all(a.get("source_body_available") for a in records) else "partial"
    if age_days is not None and age_days > STALE_DAYS:
        status = "stale"
    return {**source, "status": status, "last_checked_at": poc.now(), "last_success_at": poc.now(),
        "latest_published_at": latest, "age_days": round(age_days, 2) if age_days is not None else None,
        "article_count": len(records), "feed_item_count": len(items), "body_count": sum(bool(a.get("source_body_available")) for a in records),
        "error": "; ".join(errors) or None,
        "freshness_note": "发布时间来自 RSS；超过14天提示源较旧，不据此断言公众号停更。"}


def refresh(per_source=2, fixture_dir=None):
    config = source_config()
    state_path = poc.DATA / "wechat-sources.json"
    old = json.loads(state_path.read_text()) if state_path.exists() else {"sources": []}
    previous = {source["id"]: source for source in old["sources"]}
    states = []
    for source in config:
        try:
            # Fixture import is an explicit local debugging facility, not a remote fallback.
            if fixture_dir:
                body = (Path(fixture_dir) / (source["id"] + ".xml")).read_bytes()
            else:
                body = poc.fetch(source["feed_url"], limit=16_000_000)
            raw_path = poc.DATA / "wechat-raw" / (poc.safe_id(source["id"]) + ".xml")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(body)
            state = ingest_feed(source, body, per_source)
            state["retrieval_mode"] = "fixture" if fixture_dir else "live_public_rss"
        except poc.local_module("schedule_policy").OffPeakDeferred:
            # Preserve the last source state and stop the batch. A cost-window
            # pause is neither a broken RSS source nor a completed refresh.
            raise
        except Exception as exc:
            state = {**previous.get(source["id"], {}), **source, "status": "failed", "last_checked_at": poc.now(), "error": str(exc)}
        states.append(state)
        poc.write_json(state_path, {"checked_at": poc.now(), "sources": states + [previous.get(s["id"], {**s, "status": "pending"}) for s in config[len(states):]]})
        print(json.dumps({"source": source["name"], "status": state["status"], "articles": state.get("article_count"), "error": state.get("error")}, ensure_ascii=False), flush=True)
    return states


def payload():
    state = poc.DATA / "wechat-sources.json"
    sources = json.loads(state.read_text())["sources"] if state.exists() else [{**s, "status": "pending"} for s in source_config()]
    articles = [json.loads(path.read_text()) for path in (poc.DATA / "articles").glob("*.json")]
    articles.sort(key=lambda a: (a.get("published_at") or "", -(a.get("feed_rank") or 0)), reverse=True)
    return {"generated_at": poc.now(), "mode": "local_reading_poc", "sources": sources, "articles": articles}


def summarize_batch(max_articles=6):
    if not 1 <= max_articles <= 20:
        raise ValueError("POC batch must be 1–20 articles")
    articles = payload()["articles"]
    selected, seen = [], set()
    for article in articles:
        if article["coverage"]["scope"] != "source_text" or article.get("body_chars", 0) < 500 or article["status"] == "summarized" or article["source_id"] in seen:
            continue
        selected.append(article)
        seen.add(article["source_id"])
        if len(selected) >= max_articles:
            break
    result = []
    for article in selected:
        try:
            completed = poc.summarize(article["id"])
            result.append({"id": article["id"], "status": completed["status"]})
        except poc.local_module("schedule_policy").OffPeakDeferred:
            raise
        except Exception as exc:
            failed = poc.load_episode(article["id"])
            failed.update({"status": "failed", "error": str(exc)})
            poc.save_episode(failed)
            result.append({"id": article["id"], "status": "failed", "error": str(exc)})
    poc.write_json(poc.DATA / "wechat-summary-run.json", {"finished_at": poc.now(), "results": result,
        "provider": poc.summary_config()["provider"]})
    return result
