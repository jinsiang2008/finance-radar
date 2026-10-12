"""Bounded, publisher-direct news collection, independent of the KOL pipeline.

Only the collector writes these tables. Public snapshots never fetch a URL,
request inference, expose HTTP headers, or pretend feed excerpts are full text.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET

try:
    import db
except ModuleNotFoundError:  # Source-tree package invocation.
    from kol_dashboard import db

POLLING_INTERVAL_SECONDS = 300
SOURCE_STALE_SECONDS = 20 * 60
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 300
MAX_INITIAL_ITEMS = 60
INITIAL_WINDOW_HOURS = 48
RETENTION_DAYS = 14
DISPLAY_WINDOW_DAYS = 7
HTTP_TIMEOUT_SECONDS = 20
HTTP_DEADLINE_SECONDS = 40
EVIDENCE_SCOPE = "title_and_snippet"


@dataclass(frozen=True)
class NewsSource:
    id: str
    name: str
    publisher: str
    feed_url: str
    article_hosts: tuple[str, ...]


SOURCES = (
    NewsSource("bloomberg_markets", "Bloomberg · Markets", "Bloomberg",
               "https://www.bloomberg.com/feeds/markets/news.rss",
               ("bloomberg.com", "www.bloomberg.com")),
    NewsSource("wsj_markets", "WSJ · Markets", "WSJ",
               "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
               ("wsj.com", "www.wsj.com")),
    NewsSource("wsj_business", "WSJ · Business", "WSJ",
               "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
               ("wsj.com", "www.wsj.com")),
)
SOURCE_BY_ID = {source.id: source for source in SOURCES}
COMMODITY_PATTERNS = {
    "copper": re.compile(r"\bcopper\b|铜", re.I),
    "gold": re.compile(r"\bgold\b|黄金|金价|贵金属", re.I),
    "silver": re.compile(r"\bsilver\b|白银|银价", re.I),
    "aluminum": re.compile(r"\balumin(?:um|ium)\b|铝", re.I),
    "oil": re.compile(r"\b(?:oil|petroleum|brent|wti)\b|\bcrude\b|原油|油价|石油", re.I),
    "gas": re.compile(r"\bnatural\s+gas\b|\blng\b|\blpg\b|天然气|液化气", re.I),
}
_TRACKING_KEYS = {"mod", "reflink", "ref", "referrer", "gclid", "fbclid", "msclkid",
                  "srnd", "cmpid", "campaign_id", "mc_cid", "mc_eid", "tesla", "embedded-checkout",
                  "leadsource", "sref", "siteid"}


class NewsFeedError(ValueError):
    """A fixed public error code, safe to return without upstream diagnostics."""


def utc_datetime(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return parsed.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        else:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, limit: int) -> str:
    parser = _PlainText()
    # One decoding pass before parsing catches escaped markup as well as CDATA.
    parser.feed(html.unescape(str(value or ""))[:32_768])
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:limit]


def canonical_article_url(value: str, source: NewsSource) -> str | None:
    """Keep only HTTPS links to this publisher; remove attribution parameters."""
    value = html.unescape(str(value or "")).strip()
    if len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in source.article_hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443}):
            return None
        query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in _TRACKING_KEYS and not key.lower().startswith("utm_")]
        host = parsed.hostname.removeprefix("www.")
        return urlunsplit(("https", host, parsed.path or "/", urlencode(sorted(query)), ""))
    except (ValueError, TypeError):
        return None


def commodity_tags(title: str, snippet: str = "") -> list[str]:
    text = title + " " + snippet
    return [key for key, pattern in COMMODITY_PATTERNS.items() if pattern.search(text)]


def _publication(value: str, now: datetime) -> tuple[str | None, str, str | None]:
    value = str(value or "").strip()
    if not value:
        return None, "missing", None
    try:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, "invalid", None
        parsed = parsed.astimezone(timezone.utc)
        reported = timestamp(parsed)
        if parsed > now:
            return None, "future", reported
        return reported, "provided", reported
    except (TypeError, ValueError, OverflowError):
        return None, "invalid", None


def _child_text(node, names: set[str]) -> str:
    for child in node:
        if child.tag.rsplit("}", 1)[-1] in names:
            return "".join(child.itertext())
    return ""


def parse_feed(body: bytes, source: NewsSource, *, now: Any = None) -> list[dict]:
    current = utc_datetime(now)
    if len(body) > MAX_FEED_BYTES:
        raise NewsFeedError("feed_too_large")
    try:
        # These configured feeds are UTF-8. Reject DTD/entity declarations before
        # XML parsing, including expansion attacks and external-entity attempts.
        xml = body.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
            raise NewsFeedError("unsafe_xml")
        root = ET.fromstring(xml)
    except (UnicodeError, ET.ParseError):
        raise NewsFeedError("invalid_xml") from None
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"rss", "feed", "RDF"}:
        raise NewsFeedError("not_a_feed")
    if kind == "rss" and root.find("channel") is None:
        raise NewsFeedError("not_a_feed")
    rows = []
    seen = set()
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    for node in entries[:MAX_FEED_ITEMS]:
        title = plain_text(_child_text(node, {"title"}), 500)
        link = _child_text(node, {"link"})
        if not link:
            link = next((child.get("href", "") for child in node
                         if child.tag.rsplit("}", 1)[-1] == "link"
                         and child.get("rel", "alternate") == "alternate"), "")
        url = canonical_article_url(link, source)
        if not title or not url or url in seen:
            continue
        seen.add(url)
        snippet = plain_text(_child_text(node, {"description", "summary"}), 1600)
        published_at, status, reported = _publication(_child_text(node, {"pubDate", "published", "date"}), current)
        rows.append({"id": hashlib.sha256(url.encode()).hexdigest()[:32], "title": title,
                     "snippet": snippet, "url": url, "publisher": source.publisher,
                     "published_at": published_at, "published_at_status": status,
                     "reported_published_at": reported, "commodity_tags": commodity_tags(title, snippet),
                     "evidence_scope": EVIDENCE_SCOPE if snippet else "title_only"})
    if entries and not rows:
        raise NewsFeedError("no_usable_items")
    return rows


class _FeedRedirect(HTTPRedirectHandler):
    def __init__(self, allowed_url: str):
        self.allowed_url = allowed_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl != self.allowed_url:
            raise NewsFeedError("unapproved_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(source: NewsSource, *, etag: str | None = None,
               last_modified: str | None = None) -> dict:
    """Fetch only the immutable source registry, bounded by socket/size/deadline."""
    if SOURCE_BY_ID.get(source.id) != source:
        raise NewsFeedError("unapproved_source")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KOLNewsReader/1.0)",
               "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
               "Accept-Encoding": "identity"}
    for name, value in (("If-None-Match", etag), ("If-Modified-Since", last_modified)):
        if value and len(value) <= 1024 and not re.search(r"[\r\n\x00]", value):
            headers[name] = value
    opener = build_opener(ProxyHandler({}), _FeedRedirect(source.feed_url))
    started = time.monotonic()
    try:
        with opener.open(Request(source.feed_url, headers=headers), timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise NewsFeedError("http_" + str(int(response.status)))
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_FEED_BYTES:
                raise NewsFeedError("feed_too_large")
            chunks, size = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() - started > HTTP_DEADLINE_SECONDS:
                    raise NewsFeedError("request_deadline")
                chunk = read(min(64 * 1024, MAX_FEED_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise NewsFeedError("feed_too_large")
            return {"status": 200, "body": b"".join(chunks),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified")}
    except HTTPError as exc:
        if exc.code == 304:
            return {"status": 304, "body": b"", "etag": exc.headers.get("ETag"),
                    "last_modified": exc.headers.get("Last-Modified")}
        raise NewsFeedError("http_" + str(int(exc.code))) from None
    except (TimeoutError, URLError, OSError):
        raise NewsFeedError("network_error") from None


def database_path(db_path=None) -> Path:
    return Path(db_path or db.DB_PATH)


@contextmanager
def _connection(db_path=None, *, readonly=False):
    path = database_path(db_path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) if readonly else sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        if not readonly:
            connection.commit()
    except Exception:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


def init_news_db(db_path=None) -> None:
    database_path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connection(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS news_sources (
            id TEXT PRIMARY KEY, last_checked_at TEXT, last_success_at TEXT,
            last_error TEXT, latest_published_at TEXT, etag TEXT, last_modified TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_feed_item_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS news_items (
            id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, publisher TEXT NOT NULL,
            title TEXT NOT NULL, snippet TEXT NOT NULL, published_at TEXT,
            published_at_status TEXT NOT NULL, reported_published_at TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            commodity_tags_json TEXT NOT NULL, evidence_scope TEXT NOT NULL,
            initial_import INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL, analysis_status TEXT NOT NULL DEFAULT 'not_requested'
        );
        CREATE INDEX IF NOT EXISTS news_items_published ON news_items(published_at DESC);
        CREATE INDEX IF NOT EXISTS news_items_seen ON news_items(first_seen_at DESC);
        CREATE TABLE IF NOT EXISTS news_item_sources (
            item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES news_sources(id),
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            has_snippet INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (item_id, source_id)
        );
        """)
        # Older validation databases may predate per-feed excerpt provenance.
        # Unknown provenance defaults to no excerpt, rather than guessing which
        # of overlapping WSJ feeds supplied the currently retained text.
        columns = {row[1] for row in con.execute("PRAGMA table_info(news_item_sources)")}
        if "has_snippet" not in columns:
            con.execute("ALTER TABLE news_item_sources ADD COLUMN has_snippet INTEGER NOT NULL DEFAULT 0")
        con.executemany("INSERT OR IGNORE INTO news_sources(id) VALUES (?)", [(source.id,) for source in SOURCES])


def source_state(source_id: str, db_path=None) -> dict:
    with _connection(db_path, readonly=True) as con:
        row = con.execute("SELECT * FROM news_sources WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else {}


def record_source_failure(source: NewsSource, code: str, *, db_path=None, now=None) -> None:
    with _connection(db_path) as con:
        con.execute("UPDATE news_sources SET last_checked_at=?,last_error=?,consecutive_failures=consecutive_failures+1 WHERE id=?",
                    (timestamp(utc_datetime(now)), code, source.id))


def store_feed(source: NewsSource, response: dict, *, db_path=None, now=None) -> dict:
    current = utc_datetime(now)
    stamp = timestamp(current)
    previous = source_state(source.id, db_path)
    not_modified = response.get("status") == 304
    if not_modified and not previous.get("last_success_at"):
        raise NewsFeedError("unexpected_not_modified")
    if response.get("status") not in {200, 304}:
        raise NewsFeedError("invalid_response")
    parsed = [] if not_modified else parse_feed(response.get("body", b""), source, now=current)
    bootstrap = not previous.get("last_success_at")
    # Use the bounded discovery window on subsequent runs as well; otherwise a
    # second poll would silently import the old archive skipped during bootstrap.
    cutoff = timestamp(current - timedelta(hours=INITIAL_WINDOW_HOURS))
    items = [item for item in parsed if not item["published_at"] or item["published_at"] >= cutoff]
    items.sort(key=lambda item: item["published_at"] or "", reverse=True)
    if bootstrap:
        items = items[:MAX_INITIAL_ITEMS]
    latest = max((item["published_at"] for item in parsed if item["published_at"]), default=None)
    inserted, updated = 0, 0
    with _connection(db_path) as con:
        for item in items:
            existing = con.execute("SELECT content_hash,snippet FROM news_items WHERE id=?", (item["id"],)).fetchone()
            incoming_has_snippet = bool(item["snippet"])
            prior_sighting = con.execute("SELECT has_snippet FROM news_item_sources WHERE item_id=? AND source_id=?",
                                         (item["id"], source.id)).fetchone()
            if (existing and existing["snippet"] and not incoming_has_snippet
                    and not (prior_sighting and prior_sighting["has_snippet"])):
                # A headline-only sibling feed must not erase an excerpt from
                # another feed. A feed that actually removes its own earlier
                # excerpt still updates normally, without retaining stale text.
                item["snippet"] = existing["snippet"]
                item["commodity_tags"] = commodity_tags(item["title"], item["snippet"])
                item["evidence_scope"] = EVIDENCE_SCOPE
            content_hash = hashlib.sha256((item["title"] + "\n" + item["snippet"]).encode()).hexdigest()
            con.execute("""INSERT INTO news_items
                (id,url,publisher,title,snippet,published_at,published_at_status,reported_published_at,
                 first_seen_at,last_seen_at,commodity_tags_json,evidence_scope,initial_import,content_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,
                    published_at=COALESCE(excluded.published_at,news_items.published_at),
                    published_at_status=CASE WHEN excluded.published_at IS NULL AND news_items.published_at IS NOT NULL
                        THEN news_items.published_at_status ELSE excluded.published_at_status END,
                    reported_published_at=CASE WHEN excluded.published_at IS NULL AND news_items.published_at IS NOT NULL
                        THEN news_items.reported_published_at ELSE excluded.reported_published_at END,
                    last_seen_at=excluded.last_seen_at,evidence_scope=excluded.evidence_scope,
                    commodity_tags_json=excluded.commodity_tags_json,content_hash=excluded.content_hash,
                    analysis_status=CASE WHEN news_items.content_hash=excluded.content_hash
                        THEN news_items.analysis_status ELSE 'not_requested' END""",
                (item["id"], item["url"], item["publisher"], item["title"], item["snippet"],
                 item["published_at"], item["published_at_status"], item["reported_published_at"],
                 stamp, stamp, json.dumps(item["commodity_tags"]), item["evidence_scope"], int(bootstrap), content_hash))
            con.execute("""INSERT INTO news_item_sources(item_id,source_id,first_seen_at,last_seen_at,has_snippet)
                VALUES (?,?,?,?,?) ON CONFLICT(item_id,source_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,has_snippet=excluded.has_snippet""",
                        (item["id"], source.id, stamp, stamp, int(incoming_has_snippet)))
            inserted += existing is None
            updated += existing is not None and existing["content_hash"] != content_hash
        if not_modified:
            # A cached representation can contain an upstream clock-skewed date.
            # Once that date has arrived, 304 responses must not leave it stuck
            # as "future" forever. No observation or first-seen time is invented.
            con.execute("""UPDATE news_items SET published_at=reported_published_at,
                    published_at_status='provided'
                WHERE published_at_status='future' AND reported_published_at<=?
                  AND id IN (SELECT item_id FROM news_item_sources WHERE source_id=?)""", (stamp, source.id))
            stored_latest = con.execute("""SELECT MAX(published_at) FROM news_items
                WHERE id IN (SELECT item_id FROM news_item_sources WHERE source_id=?)""", (source.id,)).fetchone()[0]
            latest = max((value for value in (previous.get("latest_published_at"), stored_latest) if value), default=None)
        # Cache validators are updated only after a successful parse and commit.
        # A 304 has no replacement representation and must preserve old validators.
        etag = response.get("etag") or (previous.get("etag") if not_modified else None)
        modified = response.get("last_modified") or (previous.get("last_modified") if not_modified else None)
        con.execute("""UPDATE news_sources SET last_checked_at=?,last_success_at=?,last_error=NULL,
            consecutive_failures=0,latest_published_at=?,etag=?,last_modified=?,last_feed_item_count=? WHERE id=?""",
            (stamp, stamp, latest,
             etag, modified, previous.get("last_feed_item_count", 0) if not_modified else len(parsed), source.id))
    return {"id": source.id, "status": "not_modified" if not_modified else "ok",
            "inserted": inserted, "updated": updated, "initial_import": bootstrap,
            "feed_items": len(parsed) if not not_modified else previous.get("last_feed_item_count", 0)}


def prune_news(*, db_path=None, now=None) -> int:
    cutoff = timestamp(utc_datetime(now) - timedelta(days=RETENTION_DAYS))
    with _connection(db_path) as con:
        return con.execute("DELETE FROM news_items WHERE COALESCE(published_at,first_seen_at) < ?", (cutoff,)).rowcount


def _public_source(source: NewsSource, row: dict, count: int, current: datetime) -> dict:
    last_success = row.get("last_success_at")
    age = (current - utc_datetime(last_success)).total_seconds() if last_success else None
    stale = age is None or age < 0 or age > SOURCE_STALE_SECONDS
    return {"id": source.id, "name": source.name, "publisher": source.publisher,
            "feed_url": source.feed_url, "status": "error" if row.get("last_error") else "ok" if last_success else "not_checked",
            "last_checked_at": row.get("last_checked_at"), "last_success_at": last_success,
            "last_error": row.get("last_error"), "latest_published_at": row.get("latest_published_at"),
            "item_count": count, "last_feed_item_count": row.get("last_feed_item_count", 0),
            "consecutive_failures": row.get("consecutive_failures", 0), "stale": stale,
            "check_age_seconds": int(age) if age is not None and age >= 0 else None,
            "evidence_scope": EVIDENCE_SCOPE}


def get_news_snapshot(*, limit=40, commodity=None, publisher=None, db_path=None, now=None) -> dict:
    """Read a public, bounded projection. Missing collector state is explicit."""
    current = utc_datetime(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    commodity = str(commodity or "").strip().lower() or None
    if commodity and commodity not in COMMODITY_PATTERNS:
        raise ValueError("unsupported commodity")
    publisher = str(publisher or "").strip().lower() or None
    if publisher and publisher not in {"bloomberg", "wsj"}:
        raise ValueError("unsupported publisher")
    states, counts, items, total = {}, {}, [], 0
    path = database_path(db_path)
    if path.is_file():
        with _connection(db_path, readonly=True) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"news_items", "news_sources", "news_item_sources"} <= tables:
                states = {row["id"]: dict(row) for row in con.execute("SELECT * FROM news_sources")}
                counts = {row[0]: row[1] for row in con.execute("SELECT source_id,COUNT(*) FROM news_item_sources GROUP BY source_id")}
                clauses, params = ["COALESCE(published_at,first_seen_at)>=?"], [timestamp(current - timedelta(days=DISPLAY_WINDOW_DAYS))]
                if commodity:
                    clauses.append("commodity_tags_json LIKE ?")
                    params.append('%"' + commodity + '"%')
                if publisher:
                    clauses.append("LOWER(publisher)=?")
                    params.append(publisher)
                where = " AND ".join(clauses)
                total = con.execute("SELECT COUNT(*) FROM news_items WHERE " + where, params).fetchone()[0]
                rows = con.execute("SELECT * FROM news_items WHERE " + where + " ORDER BY COALESCE(published_at,first_seen_at) DESC,first_seen_at DESC,id LIMIT ?", [*params, limit]).fetchall()
                for row in rows:
                    source_ids = [entry[0] for entry in con.execute("SELECT source_id FROM news_item_sources WHERE item_id=? ORDER BY source_id", (row["id"],))]
                    delay = None
                    if row["published_at"] and row["published_at_status"] == "provided":
                        delta = (utc_datetime(row["first_seen_at"]) - utc_datetime(row["published_at"])).total_seconds()
                        if delta >= 0:
                            delay = int(delta)
                    item = {key: row[key] for key in ("id", "title", "snippet", "url", "publisher", "published_at",
                        "published_at_status", "reported_published_at", "first_seen_at", "last_seen_at", "evidence_scope", "analysis_status")}
                    item.update(source_ids=source_ids, commodity_tags=json.loads(row["commodity_tags_json"]),
                                discovery_delay_seconds=delay, initial_import=bool(row["initial_import"]))
                    items.append(item)
    return {"schema_version": 1, "generated_at": timestamp(current),
            "polling_interval_seconds": POLLING_INTERVAL_SECONDS, "evidence_scope": EVIDENCE_SCOPE,
            "sources": [_public_source(source, states.get(source.id, {}), counts.get(source.id, 0), current) for source in SOURCES],
            "items": items, "total": total, "filters": {"commodity": commodity, "publisher": publisher},
            "notice": "官方 RSS 标题与简介；不代表全文或实时通讯社服务。每 5 分钟检查，实际到达速度取决于上游。"}
