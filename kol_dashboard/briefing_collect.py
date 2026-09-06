#!/usr/bin/env python3
"""Collect bounded Daily Briefing inputs from public, auditable sources.

The collector deliberately has no scheduler or public HTTP endpoint.  It reads
Hacker News' official Firebase API and the two configured RSS feeds, produces a
strict Daily Briefing v1 document, and optionally hands that document to the
existing importer.  Collection failures are resolved before any output file or
database snapshot is replaced.
"""

from __future__ import annotations

import argparse
import contextlib
import email.utils
import html
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__:
    from . import briefing_import, daily_enrichment, db
else:  # Flat production bundle in /opt/kol-dashboard.
    import briefing_import  # type: ignore
    import daily_enrichment  # type: ignore
    import db  # type: ignore


HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_TOP_URL = f"{HN_API_BASE}/topstories.json"
HN_BEST_URL = f"{HN_API_BASE}/beststories.json"
HN_DISCUSSION_URL = "https://news.ycombinator.com/item?id={item_id}"
AI_DIGEST_FEED_URL = "https://ai-digest.liziran.com/zh/feed.xml"
AI_BRIEF_FEED_URL = "https://ai-brief.liziran.com/zh/feed.xml"

HN_WINDOW = timedelta(hours=24)
RSS_WINDOW = timedelta(hours=24)
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_COLLECTION_DEADLINE_SECONDS = 40.0
DEFAULT_HN_SCAN_LIMIT = 40
DEFAULT_HN_ITEM_LIMIT = 10
DEFAULT_RSS_SCAN_LIMIT = 4
DEFAULT_RSS_ITEM_LIMIT = 6
MAX_TIMEOUT_SECONDS = 10.0
MAX_COLLECTION_DEADLINE_SECONDS = 120.0
MAX_HN_SCAN_LIMIT = 60
MAX_HN_ITEM_LIMIT = 20
MAX_RSS_SCAN_LIMIT = 4
MAX_RSS_ITEM_LIMIT = 12
MAX_CURATED_PARAGRAPHS_PER_BLOCK = 8
MAX_CURATED_LINKS_PER_BLOCK = 16
MAX_CURATED_CAPTURE_CHARS = 4_096
MAX_REDIRECTS = 3
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 768 * 1024
MAX_ERROR_COUNT = 80

_BEIJING = ZoneInfo("Asia/Shanghai")
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www\.)?arxiv\.org/(?:abs|pdf)/|"
    r"huggingface\.co/papers/)"
    r"(?P<id>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?(?:[?#].*)?$",
    re.IGNORECASE,
)
_ARXIV_ID_RE = re.compile(
    r"^(?:arxiv:)?(?P<id>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?$",
    re.IGNORECASE,
)
_AI_TOKEN_RE = re.compile(
    r"(?:\b(?:ai|agi|llm|gpt|openai|anthropic|claude|gemini|deepmind|"
    r"deepseek|mistral|qwen|llama|transformer|diffusion|embedding|"
    r"inference|machine learning|neural network|computer vision|"
    r"reinforcement learning)\b|人工智能|大模型|机器学习|神经网络|"
    r"多模态|推理模型|生成式)",
    re.IGNORECASE,
)
_MODIFIED_METADATA_KEYS = {
    "article:modified_time",
    "date_modified",
    "datemodified",
    "last-modified",
    "last_modified",
    "og:updated_time",
}
_PUBLISHED_METADATA_KEYS = {
    "article:published_time",
    "date_published",
    "datepublished",
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
_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"


class CollectionError(RuntimeError):
    """A collection run could not complete its required source scans safely."""


class FetchError(CollectionError):
    """One bounded public HTTP request failed or returned invalid bytes."""


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only a short chain of same-scheme, same-host redirects."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(request.full_url, new_url)
        source_parts = urllib.parse.urlsplit(request.full_url)
        target_parts = urllib.parse.urlsplit(target)
        if target_parts.username is not None or target_parts.password is not None:
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "redirect userinfo rejected",
                headers,
                file_pointer,
            )

        def effective_port(parts: urllib.parse.SplitResult) -> int | None:
            if parts.port is not None:
                return parts.port
            return {"http": 80, "https": 443}.get(parts.scheme.casefold())

        source_origin = (
            source_parts.scheme.casefold(),
            (source_parts.hostname or "").casefold().rstrip("."),
            effective_port(source_parts),
        )
        target_origin = (
            target_parts.scheme.casefold(),
            (target_parts.hostname or "").casefold().rstrip("."),
            effective_port(target_parts),
        )
        if source_origin != target_origin:
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "cross-origin redirect rejected",
                headers,
                file_pointer,
            )
        redirect_count = int(getattr(request, "_briefing_redirect_count", 0))
        if redirect_count >= MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "redirect limit exceeded",
                headers,
                file_pointer,
            )
        redirected = super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            target,
        )
        if redirected is not None:
            redirected._briefing_redirect_count = redirect_count + 1
        return redirected


def _default_opener() -> Callable[..., Any]:
    return urllib.request.build_opener(SameOriginRedirectHandler()).open


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    fetched_at: datetime


@dataclass(frozen=True)
class Story:
    section: str
    title: str
    source: str
    source_url: str
    fetched_at: datetime
    kind: str
    discovered_via: tuple[str, ...]
    publication_time_verified: bool
    published_at: datetime | None = None
    featured_at: datetime | None = None
    original_url: str | None = None
    discussion_url: str | None = None
    summary: str = ""
    why_it_matters: str = ""
    assets: tuple[str, ...] = ()
    # Private producer evidence.  It is passed to the background localizer but
    # is deliberately not serialized into the public Daily snapshot.
    evidence_excerpt: str = field(default="", repr=False, compare=False)
    summary_basis: str = "title_only"
    hn_id: int | None = None
    hn_rank: int | None = None
    hn_score: int | None = None
    hn_comments: int | None = None
    heat_score: float | None = None
    aliases: frozenset[str] = field(default_factory=frozenset)

    def to_v1_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "title": self.title,
            "source": self.source,
            "source_url": self.source_url,
            "fetched_at": _iso(self.fetched_at),
            "source_tier": "discovery",
            "summary": self.summary,
            "why_it_matters": self.why_it_matters,
            "assets": list(self.assets),
            "kind": self.kind,
            "discovered_via": list(self.discovered_via),
            "publication_time_verified": self.publication_time_verified,
        }
        optional: dict[str, Any] = {
            "published_at": (
                _iso(self.published_at)
                if self.publication_time_verified and self.published_at
                else None
            ),
            "featured_at": _iso(self.featured_at) if self.featured_at else None,
            "original_url": self.original_url,
            "discussion_url": self.discussion_url,
            "hn_id": self.hn_id,
            "hn_rank": self.hn_rank,
            "hn_score": self.hn_score,
            "hn_comments": self.hn_comments,
            "heat_score": self.heat_score,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
        return item


@dataclass(frozen=True)
class CollectionResult:
    payload: dict[str, Any]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _FeedSpec:
    feed_url: str
    expected_host: str
    source: str
    kind: str
    discovered_via: str


_FEEDS = (
    _FeedSpec(
        feed_url=AI_DIGEST_FEED_URL,
        expected_host="ai-digest.liziran.com",
        source="AI Digest",
        kind="ai_digest",
        discovered_via="ai_digest_rss",
    ),
    _FeedSpec(
        feed_url=AI_BRIEF_FEED_URL,
        expected_host="ai-brief.liziran.com",
        source="AI Brief",
        kind="paper_digest",
        discovered_via="ai_brief_rss",
    ),
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _clean_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(html.unescape(value).split())
    return cleaned[:maximum].rstrip()


def _safe_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return briefing_import.canonicalize_source_url(value.strip())
    except (briefing_import.BriefingValidationError, TypeError, ValueError):
        return None


def _arxiv_identity(value: str | None) -> str | None:
    if not value:
        return None
    candidate = urllib.parse.unquote(value.strip())
    match = _ARXIV_URL_RE.fullmatch(candidate) or _ARXIV_ID_RE.fullmatch(candidate)
    return f"arxiv:{match.group('id').lower()}" if match else None


def _specific_original_url(value: str | None) -> str | None:
    """Return a conservative, article-like canonical URL or ``None``."""
    canonical = _safe_url(value)
    if canonical is None:
        return None
    if _arxiv_identity(canonical) is not None:
        return canonical
    parsed = urllib.parse.urlsplit(canonical)
    segments = [
        urllib.parse.unquote(segment).casefold()
        for segment in parsed.path.split("/")
        if segment
    ]
    while segments and _LOCALE_SEGMENT_RE.fullmatch(segments[0]):
        segments.pop(0)
    if not segments:
        return None

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
        return None

    query_has_identity = any(
        key.casefold() in _IDENTITY_QUERY_KEYS and bool(query_value)
        for key, query_value in urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=False,
        )
    )
    if suffix or query_has_identity or re.search(r"\d", stem):
        return canonical
    # A single opaque slug is commonly a product, company, or rolling landing
    # page. A nested non-generic path is the minimum safe structural evidence
    # when no document suffix or explicit identifier is present.
    if len(segments) < 2:
        return None
    return canonical


def _url_alias(value: str | None) -> str | None:
    canonical = _specific_original_url(value)
    if canonical is None:
        return None
    return f"url:{canonical}"


def _parse_datetime(value: Any) -> datetime | None:
    text = _clean_text(value, 128)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


class _ArticleMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.modified: list[str] = []
        self.published: list[str] = []
        self.links: list[str] = []
        self._json_ld_depth = 0
        self._json_ld_chunks: list[str] = []
        self.json_ld_documents: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            key.casefold(): value
            for key, value in attrs
            if isinstance(value, str)
        }
        if tag.casefold() == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).casefold()
            content = attributes.get("content", "")
            if key in _MODIFIED_METADATA_KEYS and content:
                self.modified.append(content)
            elif key in _PUBLISHED_METADATA_KEYS and content:
                self.published.append(content)
        elif tag.casefold() == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        elif (
            tag.casefold() == "script"
            and attributes.get("type", "").casefold() == "application/ld+json"
        ):
            self._json_ld_depth = 1
            self._json_ld_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_ld_depth:
            self.json_ld_documents.append("".join(self._json_ld_chunks))
            self._json_ld_depth = 0
            self._json_ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_chunks.append(data)


def _json_ld_dates(value: Any) -> tuple[list[str], list[str]]:
    modified: list[str] = []
    published: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded == "datemodified" and isinstance(item, str):
                modified.append(item)
            elif folded == "datepublished" and isinstance(item, str):
                published.append(item)
            else:
                child_modified, child_published = _json_ld_dates(item)
                modified.extend(child_modified)
                published.extend(child_published)
    elif isinstance(value, list):
        for item in value:
            child_modified, child_published = _json_ld_dates(item)
            modified.extend(child_modified)
            published.extend(child_published)
    return modified, published


def _page_evidence(
    body: bytes,
    *,
    page_url: str,
    current: datetime,
) -> tuple[datetime | None, list[str]]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    parser = _ArticleMetadataParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        return None, []

    modified = list(parser.modified)
    published = list(parser.published)
    for document in parser.json_ld_documents:
        try:
            json_value = json.loads(document)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        json_modified, json_published = _json_ld_dates(json_value)
        modified.extend(json_modified)
        published.extend(json_published)

    # A page-owned modified time is preferred to the RSS pubDate.  Future and
    # timezone-free metadata is never used as a publication timestamp.
    for values in (modified, published):
        valid = [
            parsed
            for parsed in (_parse_datetime(value) for value in values)
            if parsed is not None and parsed <= current
        ]
        if valid:
            return max(valid), parser.links
    return None, parser.links


@dataclass
class _CuratedBlock:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


class _CuratedBlockLimitReached(Exception):
    pass


class _CuratedContentParser(HTMLParser):
    """Extract numbered, source-backed sections from one RSS content body."""

    def __init__(
        self,
        *,
        maximum: int,
        check_deadline: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        if maximum <= 0:
            raise ValueError("maximum must be positive")
        self.maximum = maximum
        self.check_deadline = check_deadline
        self.blocks: list[_CuratedBlock] = []
        self._current: _CuratedBlock | None = None
        self._capture_tag: str | None = None
        self._chunks: list[str] = []
        self._captured_chars = 0
        self._focus_mode = False

    def _check_deadline(self) -> None:
        if self.check_deadline is not None:
            self.check_deadline()

    def _finish_current(self) -> None:
        if self._current is not None:
            self.blocks.append(self._current)
        self._current = None
        if len(self.blocks) >= self.maximum:
            raise _CuratedBlockLimitReached

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._check_deadline()
        folded = tag.casefold()
        if folded in {"h2", "h3", "p"}:
            self._capture_tag = folded
            self._chunks = []
            self._captured_chars = 0
        if folded == "a" and self._current is not None:
            href = dict(attrs).get("href")
            if href and len(self._current.links) < MAX_CURATED_LINKS_PER_BLOCK:
                self._current.links.append(href)
        if folded == "hr":
            self._finish_current()

    def handle_endtag(self, tag: str) -> None:
        self._check_deadline()
        folded = tag.casefold()
        if folded != self._capture_tag:
            return
        value = _clean_text(" ".join(self._chunks), 1_200)
        if folded == "h2":
            self._finish_current()
            match = re.match(r"^\s*0*([0-9]+)[.、:：]?\s*(.+)$", value)
            if match and match.group(2).strip() and "快讯" not in match.group(2):
                self._current = _CuratedBlock(
                    title=_clean_text(match.group(2), 300)
                )
                self._focus_mode = False
            else:
                self._focus_mode = value == "重点关注"
        elif folded == "h3":
            if self._focus_mode and value:
                self._finish_current()
                self._current = _CuratedBlock(title=_clean_text(value, 300))
        elif (
            folded == "p"
            and self._current is not None
            and value
            and len(self._current.paragraphs) < MAX_CURATED_PARAGRAPHS_PER_BLOCK
        ):
            self._current.paragraphs.append(value)
        self._capture_tag = None
        self._chunks = []
        self._captured_chars = 0

    def handle_data(self, data: str) -> None:
        self._check_deadline()
        if self._capture_tag:
            remaining = MAX_CURATED_CAPTURE_CHARS - self._captured_chars
            if remaining > 0:
                chunk = data[:remaining]
                self._chunks.append(chunk)
                self._captured_chars += len(chunk)

    def close(self) -> None:
        super().close()
        self._finish_current()


def _curated_blocks(
    value: str,
    *,
    maximum: int,
    check_deadline: Callable[[], None] | None = None,
) -> list[_CuratedBlock]:
    parser = _CuratedContentParser(
        maximum=maximum,
        check_deadline=check_deadline,
    )
    try:
        parser.feed(value)
        parser.close()
    except _CuratedBlockLimitReached:
        pass
    except (AssertionError, ValueError):
        return []
    return parser.blocks


class BriefingCollector:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        deadline_seconds: float = DEFAULT_COLLECTION_DEADLINE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        hn_scan_limit: int = DEFAULT_HN_SCAN_LIMIT,
        hn_item_limit: int = DEFAULT_HN_ITEM_LIMIT,
        rss_scan_limit: int = DEFAULT_RSS_SCAN_LIMIT,
        rss_item_limit: int = DEFAULT_RSS_ITEM_LIMIT,
        enable_ai_enrichment: bool = False,
        enrichment_config: Any | None = None,
        enrichment_transport: Any | None = None,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
            or timeout > MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g} seconds"
            )
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(float(deadline_seconds))
            or deadline_seconds <= 0
            or deadline_seconds > MAX_COLLECTION_DEADLINE_SECONDS
        ):
            raise ValueError(
                "deadline_seconds must be greater than 0 and at most "
                f"{MAX_COLLECTION_DEADLINE_SECONDS:g} seconds"
            )
        for label, value in (
            ("hn_scan_limit", hn_scan_limit),
            ("hn_item_limit", hn_item_limit),
            ("rss_scan_limit", rss_scan_limit),
            ("rss_item_limit", rss_item_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        self.opener = opener or _default_opener()
        self.timeout = timeout
        self.deadline_seconds = float(deadline_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.deadline_at: float | None = None
        self.hn_scan_limit = min(hn_scan_limit, MAX_HN_SCAN_LIMIT)
        self.hn_item_limit = min(hn_item_limit, MAX_HN_ITEM_LIMIT)
        self.rss_scan_limit = min(rss_scan_limit, MAX_RSS_SCAN_LIMIT)
        self.rss_item_limit = min(rss_item_limit, MAX_RSS_ITEM_LIMIT)
        self.enable_ai_enrichment = bool(
            enable_ai_enrichment
            or enrichment_config is not None
            or enrichment_transport is not None
        )
        self.enrichment_config = enrichment_config
        self.enrichment_transport = enrichment_transport
        self.errors: list[str] = []
        self.coverage_times: list[datetime] = []

    def _now(self) -> datetime:
        return _aware_utc(self.clock())

    def _error(self, message: str) -> None:
        if len(self.errors) < MAX_ERROR_COUNT:
            self.errors.append(_clean_text(message, 240))

    def _start_deadline(self) -> None:
        self.deadline_at = self.monotonic() + self.deadline_seconds

    def _request_timeout(self) -> float:
        if self.deadline_at is None:
            self._start_deadline()
        assert self.deadline_at is not None
        remaining = self.deadline_at - self.monotonic()
        if remaining <= 0:
            raise FetchError("collection deadline exceeded")
        return min(self.timeout, remaining)

    def _ensure_deadline(self) -> None:
        if self.deadline_at is None:
            self._start_deadline()
        assert self.deadline_at is not None
        if self.deadline_at - self.monotonic() <= 0:
            raise FetchError("collection deadline exceeded while parsing")

    def _fetch(self, url: str, *, maximum: int) -> FetchResult:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/rss+xml, "
                "application/atom+xml, application/xml, text/html;q=0.8",
                "User-Agent": "zlstreet-daily-briefing/1.0",
            },
            method="GET",
        )
        request_timeout = self._request_timeout()
        completed = threading.Event()
        cancelled = threading.Event()
        state_lock = threading.Lock()
        state: dict[str, Any] = {}

        def close_response_once(response: Any) -> None:
            # Closing a hostile or broken response can itself block. Claim the
            # cleanup under the state lock, then perform it outside that lock
            # so the deadline path can always return independently.
            with state_lock:
                if state.get("close_started"):
                    return
                state["close_started"] = True
            with contextlib.suppress(Exception):
                response.close()

        def fetch_in_daemon() -> None:
            response: Any = None
            try:
                response = self.opener(request, timeout=request_timeout)
                with state_lock:
                    state["response"] = response
                if cancelled.is_set():
                    return
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if status is not None and not 200 <= int(status) < 300:
                    raise FetchError(f"HTTP {status}")
                body = response.read(maximum + 1)
                if not isinstance(body, bytes):
                    raise FetchError("response body is not bytes")
                if len(body) > maximum:
                    raise FetchError("response exceeds the configured byte limit")
                with state_lock:
                    state["body"] = body
            except BaseException as exc:
                with state_lock:
                    state["error"] = exc
            finally:
                if response is not None:
                    close_response_once(response)
                completed.set()

        worker = threading.Thread(
            target=fetch_in_daemon,
            name="briefing-fetch",
            daemon=True,
        )
        worker.start()
        if not completed.wait(request_timeout):
            cancelled.set()
            with state_lock:
                response = state.get("response")
            if response is not None:
                # Cleanup is best effort and must not extend the user-visible
                # wall-clock bound if a response object's close() is broken.
                threading.Thread(
                    target=close_response_once,
                    args=(response,),
                    name="briefing-fetch-close",
                    daemon=True,
                ).start()
            raise FetchError("request wall-clock deadline exceeded")

        with state_lock:
            error = state.get("error")
            body = state.get("body")
        if error is not None:
            if isinstance(error, FetchError):
                raise error
            if isinstance(
                error,
                (
                    OSError,
                    TimeoutError,
                    ValueError,
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                ),
            ):
                raise FetchError(
                    str(error) or error.__class__.__name__
                ) from error
            raise error
        if not isinstance(body, bytes):
            raise FetchError("request completed without a response body")
        fetched_at = self._now()
        self.coverage_times.append(fetched_at)
        return FetchResult(body=body, fetched_at=fetched_at)

    def _fetch_json(self, url: str) -> tuple[Any, datetime]:
        result = self._fetch(url, maximum=MAX_JSON_BYTES)
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError("response is not valid UTF-8 JSON") from exc
        return payload, result.fetched_at

    def _ranked_hn_ids(self) -> tuple[dict[int, dict[str, int]], int]:
        ranks: dict[int, dict[str, int]] = {}
        successful_lists = 0
        for label, url in (("top", HN_TOP_URL), ("best", HN_BEST_URL)):
            try:
                payload, _ = self._fetch_json(url)
                if not isinstance(payload, list):
                    raise FetchError("root payload is not an array")
            except FetchError as exc:
                self._error(f"Hacker News {label} list failed: {exc}")
                continue
            successful_lists += 1
            seen: set[int] = set()
            for rank, raw_id in enumerate(payload[: self.hn_scan_limit], start=1):
                item_id = _safe_int(raw_id, minimum=1)
                if item_id is None or item_id in seen:
                    continue
                seen.add(item_id)
                ranks.setdefault(item_id, {})[label] = rank
        return ranks, successful_lists

    def collect_hacker_news(self, *, current: datetime) -> tuple[list[Story], int]:
        ranks, successful_lists = self._ranked_hn_ids()
        stories: list[Story] = []

        def fetch_story(
            entry: tuple[int, dict[str, int]],
        ) -> tuple[int, Story | None, str | None]:
            item_id, item_ranks = entry
            try:
                payload, fetched_at = self._fetch_json(
                    f"{HN_API_BASE}/item/{item_id}.json"
                )
            except FetchError as exc:
                return item_id, None, str(exc)
            story = self._hn_story(
                payload,
                item_id=item_id,
                ranks=item_ranks,
                fetched_at=fetched_at,
                current=current,
            )
            return item_id, story, None

        # The limit is a global request budget, not a per-list budget.  Dual
        # list membership and the best observed rank determine the bounded,
        # reproducible candidate set before any item fan-out begins.
        entries = sorted(
            ranks.items(),
            key=lambda entry: (
                min(entry[1].values()),
                -len(entry[1]),
                entry[0],
            ),
        )[: self.hn_scan_limit]
        with ThreadPoolExecutor(
            max_workers=min(8, max(1, len(entries))),
            thread_name_prefix="briefing-hn",
        ) as executor:
            fetched = executor.map(fetch_story, entries)
            for item_id, story, error in fetched:
                if error:
                    self._error(f"Hacker News item {item_id} failed: {error}")
                    continue
                if story is not None:
                    stories.append(story)
        stories.sort(
            key=lambda item: (
                item.heat_score or 0.0,
                -(item.hn_rank or 10_000),
                item.published_at or item.fetched_at,
                item.hn_id or 0,
            ),
            reverse=True,
        )
        return stories[: self.hn_item_limit], successful_lists

    def _hn_story(
        self,
        payload: Any,
        *,
        item_id: int,
        ranks: Mapping[str, int],
        fetched_at: datetime,
        current: datetime,
    ) -> Story | None:
        if not isinstance(payload, Mapping):
            return None
        if payload.get("deleted") is True or payload.get("dead") is True:
            return None
        if payload.get("type") != "story":
            # Firebase's `job` objects have story-like fields but no useful
            # discussion signal; comments and ranking are not comparable.
            return None
        actual_id = _safe_int(payload.get("id"), minimum=1)
        if actual_id != item_id:
            return None
        title = _clean_text(payload.get("title"), 300)
        timestamp = _safe_int(payload.get("time"), minimum=1)
        score = _safe_int(payload.get("score"))
        # Fresh HN stories may not have a descendants field until the first
        # comment exists.  Absence means zero; malformed explicit values still
        # fail closed instead of being coerced.
        comments = _safe_int(payload.get("descendants", 0))
        if not title or timestamp is None or score is None or comments is None:
            return None
        try:
            published_at = datetime.fromtimestamp(timestamp, timezone.utc).replace(
                microsecond=0
            )
        except (OSError, OverflowError, ValueError):
            return None
        age = current - published_at
        if age < timedelta(0) or age > HN_WINDOW:
            return None

        discussion_url = HN_DISCUSSION_URL.format(item_id=item_id)
        submitted_url = _safe_url(payload.get("url"))
        raw_original = _specific_original_url(submitted_url)
        # A reusable home/category URL is not a story identity. When HN only
        # supplies such a link, keep the auditable HN discussion as the public
        # record URL instead of claiming the landing page is the original.
        source_url = raw_original or discussion_url
        rank = min(ranks.values())
        heat = hn_heat_score(
            rank=rank,
            score=score,
            comments=comments,
            age_hours=age.total_seconds() / 3600,
            appears_in_both=len(ranks) > 1,
        )
        rank_labels = []
        if "top" in ranks:
            rank_labels.append(f"Top #{ranks['top']}")
        if "best" in ranks:
            rank_labels.append(f"Best #{ranks['best']}")
        rank_text = " / ".join(rank_labels)
        link_note = (
            "主链接保留原始文章，讨论链接单独保留。"
            if raw_original
            else (
                "HN 提交链接是泛化落地页，已忽略该链接；主链接改为 HN 讨论。"
                if submitted_url
                else "该站内讨论没有外链，主链接指向 Hacker News 讨论页。"
            )
        )
        summary = (
            f"Hacker News 采集快照：{rank_text}，{score} points，"
            f"{comments} comments。"
        )
        why = (
            "采集时榜单名次、积分、评论数和 HN 提交时间的指数衰减共同得到"
            f"可审计热度分 {heat:.1f}/100；{link_note}"
        )
        section = "ai" if _AI_TOKEN_RE.search(title) else "technology"
        raw_self_post = payload.get("text")
        self_post_excerpt = (
            _html_text(raw_self_post, 4_000)
            if raw_original is None and isinstance(raw_self_post, str)
            else ""
        )
        aliases = {f"hn:{item_id}"}
        if submitted_url:
            url_alias = _url_alias(raw_original)
            if url_alias:
                aliases.add(url_alias)
            arxiv = _arxiv_identity(raw_original)
            if arxiv:
                aliases.add(arxiv)
        return Story(
            section=section,
            title=title,
            source="Hacker News",
            source_url=source_url,
            fetched_at=fetched_at,
            kind="hn_story",
            discovered_via=tuple(
                name
                for name in ("hacker_news_top", "hacker_news_best")
                if name.rsplit("_", 1)[-1] in ranks
            ),
            publication_time_verified=True,
            published_at=published_at,
            featured_at=fetched_at,
            original_url=raw_original,
            discussion_url=discussion_url,
            summary=summary,
            why_it_matters=why,
            assets=("THEME:AI",) if section == "ai" else ("THEME:TECH",),
            evidence_excerpt=self_post_excerpt,
            summary_basis="self_post" if self_post_excerpt else "title_only",
            hn_id=item_id,
            hn_rank=rank,
            hn_score=score,
            hn_comments=comments,
            heat_score=heat,
            aliases=frozenset(aliases),
        )

    def collect_feed(
        self,
        spec: _FeedSpec,
        *,
        current: datetime,
    ) -> tuple[list[Story], bool]:
        try:
            result = self._fetch(spec.feed_url, maximum=MAX_XML_BYTES)
            items = _rss_items(result.body)
        except (FetchError, ET.ParseError, ValueError) as exc:
            self._error(f"{spec.source} feed failed: {exc}")
            return [], False

        candidates = items[: self.rss_scan_limit]

        def parse_item(raw: Mapping[str, str]) -> list[Story]:
            return self._feed_stories(
                raw,
                spec=spec,
                feed_fetched_at=result.fetched_at,
                current=current,
            )

        stories: list[Story] = []
        with ThreadPoolExecutor(
            max_workers=min(4, max(1, len(candidates))),
            thread_name_prefix="briefing-rss",
        ) as executor:
            for item_stories in executor.map(parse_item, candidates):
                stories.extend(item_stories[: self.rss_item_limit - len(stories)])
                if len(stories) >= self.rss_item_limit:
                    break
        return stories, True

    def _feed_stories(
        self,
        raw: Mapping[str, str],
        *,
        spec: _FeedSpec,
        feed_fetched_at: datetime,
        current: datetime,
    ) -> list[Story]:
        issue_title = _clean_text(raw.get("title"), 300)
        if not issue_title:
            return []
        link = _resolve_feed_url(raw.get("link"), spec.feed_url)
        guid_url = _resolve_feed_url(raw.get("guid"), spec.feed_url)
        source_url = link or guid_url
        if source_url is None:
            return []

        feed_declared_at = _parse_datetime(raw.get("pubDate"))
        if feed_declared_at is not None and feed_declared_at <= current:
            if current - feed_declared_at > RSS_WINDOW:
                return []

        page_time: datetime | None = None
        page_links: list[str] = []
        fetched_at = feed_fetched_at
        if _host_is(source_url, spec.expected_host):
            try:
                page = self._fetch(source_url, maximum=MAX_HTML_BYTES)
                fetched_at = max(fetched_at, page.fetched_at)
                page_time, page_links = _page_evidence(
                    page.body,
                    page_url=source_url,
                    current=current,
                )
            except FetchError as exc:
                self._error(f"{spec.source} page metadata failed: {exc}")

        if page_time is not None and current - page_time > RSS_WINDOW:
            return []
        if page_time is None and (
            feed_declared_at is None or feed_declared_at > current
        ):
            # A fetch observation alone cannot establish when a curator
            # featured the item.  Future/fixed-noon and unparseable feed times
            # therefore need a non-future page-owned timestamp or are skipped.
            return []
        # The page timestamp belongs to the curator, not to any underlying
        # article or paper.  It is useful as `featured_at`, but cannot verify
        # the original source's publication time.
        featured_at = page_time
        if featured_at is None and feed_declared_at is not None:
            if feed_declared_at <= current:
                featured_at = feed_declared_at
        assert featured_at is not None
        future_note = (
            "RSS 声明时间位于未来，已忽略该时间；"
            if feed_declared_at is not None and feed_declared_at > current
            else ""
        )
        time_note = (
            future_note
            + "策展时间仅写入 featured_at；未直接核验原始来源时间，"
            "因此不写 published_at。"
        )
        self._ensure_deadline()
        blocks = _curated_blocks(
            raw.get("content", ""),
            maximum=self.rss_item_limit,
            check_deadline=self._ensure_deadline,
        )
        if not blocks:
            blocks = [
                _CuratedBlock(
                    title=issue_title,
                    paragraphs=[_html_text(raw.get("description", ""), 520)],
                    links=page_links[:MAX_CURATED_LINKS_PER_BLOCK],
                )
            ]

        stories: list[Story] = []
        for block in blocks:
            self._ensure_deadline()
            original_urls = _unique_external_links(
                block.links,
                base_url=source_url,
                excluded_host=spec.expected_host,
            )
            guid_external = (
                guid_url
                if guid_url is not None
                and not _host_is(guid_url, spec.expected_host)
                else None
            )
            if guid_external and guid_external not in original_urls:
                original_urls.insert(0, guid_external)
            arxiv_urls = [
                candidate
                for candidate in original_urls
                if _arxiv_identity(candidate) is not None
            ]
            # Multiple links may support one editorial synthesis. AI Brief is
            # paper-focused, so one unambiguous arXiv target wins; otherwise an
            # arbitrary link must never be labelled as the unique original.
            if spec.kind == "paper_digest" and len(arxiv_urls) == 1:
                original_url = arxiv_urls[0]
            else:
                original_url = (
                    original_urls[0] if len(original_urls) == 1 else None
                )
            original_url = _specific_original_url(original_url)
            title_key = re.sub(r"\W+", " ", block.title.casefold()).strip()
            aliases = {f"curated:{source_url}\x1f{title_key}"}
            if original_url:
                original_alias = _url_alias(original_url)
                if original_alias:
                    aliases.add(original_alias)
                arxiv = _arxiv_identity(original_url)
                if arxiv:
                    aliases.add(arxiv)

            narrative = [
                paragraph
                for paragraph in block.paragraphs
                if not paragraph.startswith("为什么重要")
            ]
            why_paragraphs = [
                paragraph
                for paragraph in block.paragraphs
                if paragraph.startswith("为什么重要")
            ]
            summary = _clean_text(narrative[0] if narrative else "", 520)
            editorial_why = _clean_text(
                why_paragraphs[0].removeprefix("为什么重要：")
                if why_paragraphs
                else "",
                360,
            )
            why = _append_sentence(
                editorial_why,
                "该来源用于发现 AI 一手报道或论文，不自动升级为一手证据。"
                + time_note,
                600,
            )
            stories.append(
                Story(
                    section="ai",
                    title=block.title,
                    source=spec.source,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    kind=spec.kind,
                    discovered_via=(spec.discovered_via,),
                    publication_time_verified=False,
                    published_at=None,
                    featured_at=featured_at,
                    original_url=original_url,
                    summary=summary,
                    why_it_matters=why,
                    assets=(
                        ("THEME:AI", "KIND:PAPER_DIGEST")
                        if spec.kind == "paper_digest"
                        else ("THEME:AI",)
                    ),
                    evidence_excerpt=summary,
                    summary_basis="curated_excerpt",
                    aliases=frozenset(aliases),
                )
            )
        return stories

    def collect(self) -> CollectionResult:
        self._start_deadline()
        started_at = self._now()
        with ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="briefing-source",
        ) as executor:
            hn_future = executor.submit(self.collect_hacker_news, current=started_at)
            feed_futures = {
                spec: executor.submit(self.collect_feed, spec, current=started_at)
                for spec in _FEEDS
            }
            hn_stories, hn_successes = hn_future.result()
            feed_results = {
                spec: future.result() for spec, future in feed_futures.items()
            }

        if hn_successes != 2:
            raise CollectionError("Hacker News top and best lists are both required")
        if not hn_stories:
            raise CollectionError(
                "Hacker News must provide at least one current valid story"
            )
        all_stories = list(hn_stories)
        for spec in _FEEDS:
            feed_stories, succeeded = feed_results[spec]
            if not succeeded:
                raise CollectionError(
                    f"{spec.source} feed root must fetch and parse successfully"
                )
            all_stories.extend(feed_stories)

        deduplicated = deduplicate_stories(all_stories)
        packets = [
            {
                "hn_id": story.hn_id,
                "title": story.title,
                "source": story.source,
                "source_url": story.source_url,
                "summary_basis": story.summary_basis,
                "evidence_excerpt": story.evidence_excerpt,
            }
            for story in deduplicated
        ]
        try:
            if self.enable_ai_enrichment:
                remaining_budget = (
                    max(0.0, self.deadline_at - self.monotonic())
                    if self.deadline_at is not None
                    else 0.0
                )
                localization = daily_enrichment.enrich_batch(
                    packets,
                    config=self.enrichment_config,
                    transport=self.enrichment_transport,
                    budget_seconds=remaining_budget,
                )
            else:
                localization = {
                    "configured": False,
                    "results": {
                        daily_enrichment.story_identity(packet):
                        daily_enrichment.local_projection(
                            packet,
                            status=(
                                "source_zh"
                                if not daily_enrichment.should_enrich(packet)
                                else "unavailable"
                            ),
                        )
                        for packet in packets
                    },
                    "errors": {},
                }
        except Exception:
            # Chinese projection is an assistive layer.  A local/provider bug
            # must never replace a fresh source snapshot with an older one.
            localization = {
                "configured": False,
                "results": {
                    daily_enrichment.story_identity(packet):
                    daily_enrichment.local_projection(
                        packet,
                        status="unavailable",
                    )
                    for packet in packets
                },
                "errors": {"batch": "worker_exception"},
            }
        localized_items = localization.get("results")
        if not isinstance(localized_items, Mapping):
            localized_items = {}
        localization_errors = localization.get("errors")
        if isinstance(localization_errors, Mapping) and localization_errors:
            codes = sorted(
                {
                    _clean_text(code, 48)
                    for code in localization_errors.values()
                    if _clean_text(code, 48) not in {"", "provider_unconfigured"}
                }
            )
            if codes:
                self._error(
                    "Daily Chinese projection partial: "
                    + ", ".join(codes[:4])
                )

        generated_at = max(self._now(), *(self.coverage_times or [started_at]))
        source_as_of = min(
            generated_at,
            max(self.coverage_times or [generated_at]),
        )
        sections: dict[str, list[dict[str, Any]]] = {
            section: [] for section in briefing_import.SECTION_KEYS
        }
        for story, packet in zip(deduplicated, packets):
            # Defensive clamp: injected clocks and responses may not move
            # monotonically, but the importer requires every fetch to be at or
            # before the actual coverage instant.
            safe_story = replace(
                story,
                fetched_at=min(story.fetched_at, source_as_of),
                featured_at=(
                    min(story.featured_at, source_as_of)
                    if story.featured_at
                    else None
                ),
            )
            item = safe_story.to_v1_item()
            projection = localized_items.get(
                daily_enrichment.story_identity(packet)
            )
            if isinstance(projection, Mapping):
                item.update(projection)
            sections[safe_story.section].append(item)
        for section in sections:
            sections[section].sort(key=_payload_story_sort_key, reverse=True)

        payload = {
            "schema_version": briefing_import.SCHEMA_VERSION,
            "snapshot_date": generated_at.astimezone(_BEIJING).date().isoformat(),
            "generated_at": _iso(generated_at),
            "source_as_of": _iso(source_as_of),
            "sections": sections,
        }
        # Validation happens before callers can replace a file or a database
        # snapshot.  The normalized projection is intentionally discarded:
        # only strict producer fields may be handed back to the importer.
        briefing_import.validate_payload(payload, now=generated_at)
        return CollectionResult(payload=payload, errors=tuple(self.errors))


def hn_heat_score(
    *,
    rank: int,
    score: int,
    comments: int,
    age_hours: float,
    appears_in_both: bool,
) -> float:
    """Return a bounded, auditable HN hotness score in the range 0..100."""
    if rank <= 0 or score < 0 or comments < 0 or age_hours < 0:
        raise ValueError("HN heat inputs must be non-negative and rank must be positive")
    rank_component = 35.0 / (1.0 + 0.08 * (rank - 1))
    score_component = 20.0 * min(1.0, math.log1p(score) / math.log1p(500))
    comment_component = 15.0 * min(
        1.0,
        math.log1p(comments) / math.log1p(250),
    )
    overlap_component = 10.0 if appears_in_both else 0.0
    time_decay = math.exp(-math.log(2.0) * age_hours / 8.0)
    engagement = (
        rank_component + score_component + comment_component + overlap_component
    )
    # Eight-hour half-life keeps fast-rising discussions visible without a
    # linear cliff. A small floor preserves relative engagement within the
    # same 24-hour display window while still materially penalizing old posts.
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


def _rss_items(body: bytes) -> list[dict[str, str]]:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not supported")
    root = ET.fromstring(body)

    root_namespace, root_name = _xml_name(root.tag)
    if (root_namespace, root_name) == ("", "rss"):
        if root.attrib.get("version", "").strip() != "2.0":
            raise ValueError("RSS root must declare version 2.0")
        channels = [
            child
            for child in list(root)
            if _xml_name(child.tag) == ("", "channel")
        ]
        if len(channels) != 1:
            raise ValueError("RSS root must contain exactly one channel")
        channel = channels[0]
        for required_name in ("title", "link", "description"):
            if not _direct_xml_text(channel, "", required_name):
                raise ValueError(
                    f"RSS channel is missing required {required_name} metadata"
                )
        nodes = [
            child
            for child in list(channel)
            if _xml_name(child.tag) == ("", "item")
        ]
        return [_rss_item_fields(node) for node in nodes]

    if root_name == "feed":
        if root_namespace != _ATOM_NAMESPACE:
            raise ValueError("Atom feed root must use the Atom namespace")
        for required_name in ("title", "id", "updated"):
            value = _direct_xml_text(root, _ATOM_NAMESPACE, required_name)
            if not value:
                raise ValueError(
                    f"Atom feed is missing required {required_name} metadata"
                )
            if required_name == "updated" and _parse_datetime(value) is None:
                raise ValueError("Atom feed updated metadata is invalid")
        nodes = [
            child
            for child in list(root)
            if _xml_name(child.tag) == (_ATOM_NAMESPACE, "entry")
        ]
        return [_atom_entry_fields(node) for node in nodes]

    raise ValueError("document root is not a supported RSS or Atom feed")


def _rss_item_fields(node: ET.Element) -> dict[str, str]:
    item: dict[str, str] = {}
    for child in list(node):
        name = _local_name(child.tag)
        output_name = "content" if name == "encoded" else name
        if output_name == "date":
            output_name = "pubDate"
        if output_name not in {
            "title",
            "link",
            "guid",
            "pubDate",
            "description",
            "content",
        }:
            continue
        value = "".join(child.itertext()).strip()
        if output_name == "link" and not value:
            value = child.attrib.get("href", "").strip()
        if value and output_name not in item:
            item[output_name] = value
    return item


def _atom_entry_fields(node: ET.Element) -> dict[str, str]:
    item: dict[str, str] = {}
    fallback_link = ""
    updated = ""
    for child in list(node):
        namespace, name = _xml_name(child.tag)
        if namespace != _ATOM_NAMESPACE:
            continue
        value = "".join(child.itertext()).strip()
        if name == "title" and value:
            item.setdefault("title", value)
        elif name == "id" and value:
            item.setdefault("guid", value)
        elif name == "link":
            href = child.attrib.get("href", "").strip()
            if href and child.attrib.get("rel", "alternate") == "alternate":
                item.setdefault("link", href)
            elif href and not fallback_link:
                fallback_link = href
        elif name == "published" and value:
            item.setdefault("pubDate", value)
        elif name == "updated" and value and not updated:
            updated = value
        elif name == "summary" and value:
            item.setdefault("description", value)
        elif name == "content" and value:
            item.setdefault("content", value)
    if "link" not in item and fallback_link:
        item["link"] = fallback_link
    if "pubDate" not in item and updated:
        item["pubDate"] = updated
    return item


def _xml_name(value: Any) -> tuple[str, str]:
    text = str(value)
    if text.startswith("{") and "}" in text:
        namespace, local = text[1:].split("}", 1)
        return namespace, local
    return "", text


def _direct_xml_text(node: ET.Element, namespace: str, name: str) -> str:
    for child in list(node):
        if _xml_name(child.tag) == (namespace, name):
            return "".join(child.itertext()).strip()
    return ""


def _local_name(value: Any) -> str:
    return str(value).rsplit("}", 1)[-1]


def _resolve_feed_url(value: Any, feed_url: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _safe_url(urllib.parse.urljoin(feed_url, value.strip()))


def _host_is(url: str, expected_host: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")
    return host == expected_host or host == f"www.{expected_host}"


def _unique_external_links(
    links: Iterable[str],
    *,
    base_url: str,
    excluded_host: str,
) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in links:
        candidate = _safe_url(urllib.parse.urljoin(base_url, raw))
        if (
            candidate is None
            or _host_is(candidate, excluded_host)
            or candidate in seen
        ):
            continue
        seen.add(candidate)
        output.append(candidate)
    return output


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


def _html_text(value: str, maximum: int) -> str:
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        return _clean_text(value, maximum)
    return _clean_text(" ".join(parser.chunks), maximum)


def _story_preference(story: Story) -> tuple[int, int, datetime, str]:
    kind_rank = {"paper_digest": 3, "ai_digest": 2, "hn_story": 1}
    return (
        kind_rank.get(story.kind, 0),
        int(story.publication_time_verified),
        story.published_at or story.fetched_at,
        story.source_url,
    )


def _append_sentence(value: str, addition: str, maximum: int) -> str:
    combined = " ".join(part for part in (value, addition) if part).strip()
    return combined[:maximum].rstrip()


def _merge_story_group(group: list[Story]) -> Story:
    representative = max(group, key=_story_preference)
    discovered = tuple(
        dict.fromkeys(
            source
            for story in group
            for source in story.discovered_via
        )
    )
    assets = tuple(dict.fromkeys(asset for story in group for asset in story.assets))
    aliases = frozenset(alias for story in group for alias in story.aliases)
    hn_candidates = [story for story in group if story.kind == "hn_story"]
    discussion_url = representative.discussion_url
    summary = representative.summary
    why = representative.why_it_matters
    hn_evidence: Story | None = None
    if hn_candidates and representative.kind != "hn_story":
        hottest = max(hn_candidates, key=lambda item: item.heat_score or 0.0)
        hn_evidence = hottest
        summary = _append_sentence(
            summary,
            "同一原链亦进入 Hacker News："
            f"#{hottest.hn_rank}，{hottest.hn_score} points，"
            f"{hottest.hn_comments} comments。",
            800,
        )
        why = _append_sentence(
            why,
            f"跨源去重后保留 HN 可审计热度 {hottest.heat_score:.1f}/100。",
            600,
        )
        discussion_url = hottest.discussion_url
    elif representative.kind == "hn_story":
        hn_evidence = representative
    original_url = representative.original_url
    if original_url is None:
        originals = {story.original_url for story in group if story.original_url}
        if len(originals) == 1:
            original_url = originals.pop()
    representative_featured_times = [
        story.featured_at
        for story in group
        if story.featured_at is not None
        and story.kind == representative.kind
        and story.source_url == representative.source_url
    ]
    # `featured_at` describes the representative discovery surface.  HN's
    # observation time is useful provenance, but it must never make an older
    # Digest/Brief issue look newly featured on every collection run.
    featured_at = max(
        representative_featured_times,
        default=representative.featured_at,
    )
    return replace(
        representative,
        discovered_via=discovered,
        assets=assets,
        aliases=aliases,
        fetched_at=max(story.fetched_at for story in group),
        featured_at=featured_at,
        original_url=original_url,
        discussion_url=discussion_url,
        hn_id=hn_evidence.hn_id if hn_evidence else representative.hn_id,
        hn_rank=hn_evidence.hn_rank if hn_evidence else representative.hn_rank,
        hn_score=hn_evidence.hn_score if hn_evidence else representative.hn_score,
        hn_comments=(
            hn_evidence.hn_comments if hn_evidence else representative.hn_comments
        ),
        heat_score=(
            hn_evidence.heat_score if hn_evidence else representative.heat_score
        ),
        summary=summary,
        why_it_matters=why,
    )


def deduplicate_stories(stories: Sequence[Story]) -> list[Story]:
    """Deduplicate by any shared canonical URL, arXiv id, or HN id."""
    if not stories:
        return []
    parents = list(range(len(stories)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    alias_owner: dict[str, int] = {}
    for index, story in enumerate(stories):
        aliases = set(story.aliases)
        if not aliases:
            title_key = re.sub(r"\W+", " ", story.title.casefold()).strip()
            aliases.add(f"fallback:{story.source_url}\x1f{title_key}")
        if story.hn_id is not None:
            aliases.add(f"hn:{story.hn_id}")
        for alias in aliases:
            previous = alias_owner.setdefault(alias, index)
            union(index, previous)

    groups: dict[int, list[Story]] = {}
    for index, story in enumerate(stories):
        groups.setdefault(find(index), []).append(story)
    return [_merge_story_group(group) for group in groups.values()]


def _payload_story_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    timestamp = item.get("published_at") or item.get("featured_at") or item["fetched_at"]
    return (
        int(bool(item.get("publication_time_verified"))),
        float(item.get("heat_score") or 0.0),
        str(timestamp),
        str(item.get("title") or "").casefold(),
    )


def collect_briefing(
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_COLLECTION_DEADLINE_SECONDS,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    hn_scan_limit: int = DEFAULT_HN_SCAN_LIMIT,
    hn_item_limit: int = DEFAULT_HN_ITEM_LIMIT,
    rss_scan_limit: int = DEFAULT_RSS_SCAN_LIMIT,
    rss_item_limit: int = DEFAULT_RSS_ITEM_LIMIT,
    enable_ai_enrichment: bool = False,
    enrichment_config: Any | None = None,
    enrichment_transport: Any | None = None,
) -> CollectionResult:
    clock = (lambda: now) if now is not None else None
    return BriefingCollector(
        opener=opener,
        timeout=timeout,
        deadline_seconds=deadline_seconds,
        clock=clock,
        monotonic=monotonic,
        hn_scan_limit=hn_scan_limit,
        hn_item_limit=hn_item_limit,
        rss_scan_limit=rss_scan_limit,
        rss_item_limit=rss_item_limit,
        enable_ai_enrichment=enable_ai_enrichment,
        enrichment_config=enrichment_config,
        enrichment_transport=enrichment_transport,
    ).collect()


def write_payload_atomic(payload: Mapping[str, Any], path: str | Path) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        directory_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def produce_briefing(
    *,
    output_path: str | Path | None = None,
    import_snapshot: bool = False,
    repository: Any = db,
    opener: Callable[..., Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_COLLECTION_DEADLINE_SECONDS,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    hn_scan_limit: int = DEFAULT_HN_SCAN_LIMIT,
    hn_item_limit: int = DEFAULT_HN_ITEM_LIMIT,
    rss_scan_limit: int = DEFAULT_RSS_SCAN_LIMIT,
    rss_item_limit: int = DEFAULT_RSS_ITEM_LIMIT,
    enable_ai_enrichment: bool = False,
    enrichment_config: Any | None = None,
    enrichment_transport: Any | None = None,
) -> tuple[CollectionResult, dict[str, Any] | None]:
    result = collect_briefing(
        opener=opener,
        timeout=timeout,
        deadline_seconds=deadline_seconds,
        now=now,
        monotonic=monotonic,
        hn_scan_limit=hn_scan_limit,
        hn_item_limit=hn_item_limit,
        rss_scan_limit=rss_scan_limit,
        rss_item_limit=rss_item_limit,
        enable_ai_enrichment=enable_ai_enrichment,
        enrichment_config=enrichment_config,
        enrichment_transport=enrichment_transport,
    )
    if output_path is not None:
        write_payload_atomic(result.payload, output_path)
    imported = (
        briefing_import.import_payload(
            result.payload,
            repository=repository,
            now=result.payload["generated_at"],
        )
        if import_snapshot
        else None
    )
    return result, imported


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Hacker News, AI Digest, and AI Brief into one strict "
            "Daily Briefing v1 snapshot."
        )
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Atomic JSON output path, or - for stdout (default: -)",
    )
    parser.add_argument(
        "--import",
        dest="import_snapshot",
        action="store_true",
        help="Import the validated payload with the existing v1 importer",
    )
    parser.add_argument("--db", dest="database_path", help="SQLite database path")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--deadline",
        type=float,
        default=DEFAULT_COLLECTION_DEADLINE_SECONDS,
        help="Whole collection deadline in seconds",
    )
    parser.add_argument("--hn-scan-limit", type=int, default=DEFAULT_HN_SCAN_LIMIT)
    parser.add_argument("--hn-limit", type=int, default=DEFAULT_HN_ITEM_LIMIT)
    parser.add_argument("--rss-scan-limit", type=int, default=DEFAULT_RSS_SCAN_LIMIT)
    parser.add_argument("--rss-limit", type=int, default=DEFAULT_RSS_ITEM_LIMIT)
    parser.add_argument(
        "--no-ai-enrichment",
        action="store_true",
        help=(
            "Skip background Chinese title/summary generation; deterministic "
            "content classification still runs"
        ),
    )
    args = parser.parse_args(argv)
    if args.database_path:
        db.DB_PATH = str(Path(args.database_path).expanduser())
    try:
        result, imported = produce_briefing(
            output_path=None if args.output == "-" else args.output,
            import_snapshot=args.import_snapshot,
            timeout=args.timeout,
            deadline_seconds=args.deadline,
            hn_scan_limit=args.hn_scan_limit,
            hn_item_limit=args.hn_limit,
            rss_scan_limit=args.rss_scan_limit,
            rss_item_limit=args.rss_limit,
            enable_ai_enrichment=not args.no_ai_enrichment,
        )
    except (
        CollectionError,
        briefing_import.BriefingValidationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"briefing collection failed: {exc}", file=sys.stderr)
        return 2
    for warning in result.errors:
        print(f"briefing collection warning: {warning}", file=sys.stderr)
    if args.output == "-":
        print(json.dumps(result.payload, ensure_ascii=False, sort_keys=True, indent=2))
    if imported is not None:
        print(
            "briefing import: " + json.dumps(imported, ensure_ascii=False, sort_keys=True),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AI_BRIEF_FEED_URL",
    "AI_DIGEST_FEED_URL",
    "BriefingCollector",
    "CollectionError",
    "CollectionResult",
    "HN_BEST_URL",
    "HN_TOP_URL",
    "Story",
    "collect_briefing",
    "deduplicate_stories",
    "hn_heat_score",
    "main",
    "produce_briefing",
    "write_payload_atomic",
]
