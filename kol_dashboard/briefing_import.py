"""Validate and import structured Daily Briefing snapshots.

This module is deliberately a one-way, JSON-only bridge. It never executes an
OpenClaw/Hermes job, fetches a URL, calls an LLM, or exposes a public write API.
The producer owns collection; this boundary validates provenance and persists a
bounded snapshot for the read-only dashboard projection.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import sys
import unicodedata
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__:
    from . import briefing_topics, db
else:  # Flat production bundle in /opt/kol-dashboard.
    import briefing_topics  # type: ignore
    import db  # type: ignore


SCHEMA_VERSION = 1
SECTION_KEYS = ("macro", "world", "finance", "technology", "ai", "investors")
SOURCE_TIERS = ("official", "first_party", "media", "discovery")

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_NORMALIZED_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_ITEMS_PER_SECTION = 80
MAX_TOTAL_ITEMS = 300
MAX_TITLE_LENGTH = 300
MAX_URL_LENGTH = 2048
MAX_SOURCE_LENGTH = 120
MAX_SUMMARY_LENGTH = 800
MAX_WHY_LENGTH = 600
MAX_TITLE_ZH_LENGTH = 180
MAX_SUMMARY_ZH_LENGTH = 420
MAX_ASSETS = 16
MAX_ASSET_LENGTH = 64
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_HN_ID = 9_007_199_254_740_991
MAX_HN_ENGAGEMENT = 1_000_000
MAX_HN_RANK = 500

DISCOVERY_KINDS = ("hn_story", "ai_digest", "paper_digest")
SUMMARY_BASES = ("title_only", "curated_excerpt", "self_post")
TRANSLATION_STATUSES = ("translated", "source_zh", "unavailable")
DISCOVERY_CHANNELS = (
    "hacker_news_top",
    "hacker_news_best",
    "ai_digest_rss",
    "ai_brief_rss",
)
_KIND_CHANNELS = {
    "hn_story": {"hacker_news_top", "hacker_news_best"},
    "ai_digest": {"ai_digest_rss"},
    "paper_digest": {"ai_brief_rss"},
}
_KIND_SOURCE_LABELS = {
    "hn_story": "hacker news",
    "ai_digest": "ai digest",
    "paper_digest": "ai brief",
}
_KIND_SOURCE_HOSTS = {
    "ai_digest": "ai-digest.liziran.com",
    "paper_digest": "ai-brief.liziran.com",
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
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www\.)?arxiv\.org/(?:abs|pdf)/|"
    r"huggingface\.co/papers/)"
    r"(?P<id>[0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?(?:\.pdf)?(?:[?#].*)?$",
    re.IGNORECASE,
)

_BEIJING = ZoneInfo("Asia/Shanghai")
_TRACKING_PARAMS = {
    "_hsenc",
    "_hsmi",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "spm",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_SENSITIVE_QUERY_PARAMS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "code",
    "credential",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-token",
}
_OFFICIAL_EXCHANGE_HOSTS = {
    "hkexnews.hk",
    "sse.com.cn",
    "szse.cn",
}
_SOCIAL_HOSTS = {
    "truthsocial.com",
    "twitter.com",
    "x.com",
}
_AGGREGATOR_HOSTS = {
    "ai-brief.liziran.com",
    "ai-digest.liziran.com",
    "baidu.com",
    "bing.com",
    "hacker-news.firebaseio.com",
    "news.baidu.com",
    "news.google.com",
    "news.ycombinator.com",
}
_AGGREGATOR_SOURCE_RE = re.compile(
    r"(?:\bbing\b|\bbaidu\b|\bgoogle\s+news\b|\bhacker\s+news\b|"
    r"\bai\s+(?:digest|brief)\b|百度(?:新闻|资讯)|必应(?:新闻|资讯))",
    re.IGNORECASE,
)
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "snapshot_date",
    "generated_at",
    "source_as_of",
    "sections",
}
_ITEM_FIELDS = {
    "title",
    "source",
    "source_url",
    "published_at",
    "fetched_at",
    "disclosed_at",
    "effective_at",
    "period_end",
    "data_as_of",
    "summary",
    "why",
    "why_it_matters",
    "assets",
    "source_tier",
    "kind",
    "discovered_via",
    "publication_time_verified",
    "featured_at",
    "original_url",
    "discussion_url",
    "hn_id",
    "hn_score",
    "hn_comments",
    "hn_rank",
    "heat_score",
    "title_zh",
    "summary_zh",
    "summary_basis",
    "content_category",
    "content_tags",
    "taxonomy_version",
    "translation_status",
}

_CONTENT_ENHANCEMENT_FIELDS = (
    "title_zh",
    "summary_zh",
    "summary_basis",
    "content_category",
    "content_tags",
    "taxonomy_version",
    "translation_status",
)
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class BriefingValidationError(ValueError):
    """The supplied v1 snapshot is unsafe, ambiguous, or out of contract."""


def _query_key_is_sensitive(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = normalized.casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    segments = tuple(part for part in normalized.split("_") if part)
    sensitive_segments = {
        "auth",
        "authorization",
        "apikey",
        "bearer",
        "credential",
        "jwt",
        "password",
        "passwd",
        "secret",
        "session",
        "sessionid",
        "signature",
        "token",
    }
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_QUERY_PARAMS
        or any(part in sensitive_segments for part in segments)
        or any(
            segments[index : index + 2] in {("api", "key"), ("access", "code")}
            for index in range(max(0, len(segments) - 1))
        )
        or compact.startswith(("clientsecret", "secretkey"))
        or compact.endswith(
            (
                "apikey",
                "token",
                "secret",
                "password",
                "credential",
                "signature",
                "sessionid",
            )
        )
    )


def _text(
    value: Any,
    field: str,
    *,
    maximum: int,
    required: bool = True,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise BriefingValidationError(f"{field} must be a string")
    if "\x00" in value or any(
        unicodedata.category(char) == "Cc" and char not in "\n\r\t"
        for char in value
    ):
        raise BriefingValidationError(f"{field} contains unsupported control characters")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise BriefingValidationError(f"{field} must not be empty")
    if len(cleaned) > maximum:
        raise BriefingValidationError(
            f"{field} must be at most {maximum} characters"
        )
    return cleaned


def _timestamp(
    value: Any,
    field: str,
    *,
    now: datetime,
    required: bool = True,
) -> tuple[str | None, datetime | None]:
    if value is None and not required:
        return None, None
    text = _text(value, field, maximum=64, required=required)
    if not text and not required:
        return None, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BriefingValidationError(
            f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BriefingValidationError(f"{field} must include a timezone")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    if parsed > now + MAX_FUTURE_SKEW:
        raise BriefingValidationError(f"{field} is too far in the future")
    return parsed.isoformat(), parsed


def _current_time(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, str):
        text = _text(value, "now", maximum=64)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BriefingValidationError(
                "now must be an ISO-8601 timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BriefingValidationError("now must include a timezone")
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BriefingValidationError("now must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _effective_date_or_timestamp(
    value: Any,
    field: str,
    *,
    now: datetime,
) -> str | None:
    """Normalize a filing period date or a timezone-aware effective instant."""
    if value is None:
        return None
    text = _text(value, field, maximum=64)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as exc:
            raise BriefingValidationError(
                f"{field} must be a valid ISO-8601 date or timestamp"
            ) from exc
        if parsed_date.isoformat() != text:
            raise BriefingValidationError(
                f"{field} must use YYYY-MM-DD for a date"
            )
        if parsed_date > now.astimezone(_BEIJING).date():
            raise BriefingValidationError(f"{field} is too far in the future")
        return text
    normalized, _ = _timestamp(value, field, now=now)
    return normalized


def canonicalize_source_url(value: Any, field: str = "source_url") -> str:
    """Return a stable public HTTP(S) URL and reject executable/local schemes."""
    raw = _text(value, field, maximum=MAX_URL_LENGTH)
    if "\\" in raw or any(char.isspace() for char in raw):
        raise BriefingValidationError(f"{field} is not a safe URL")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise BriefingValidationError(f"{field} is not a valid URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise BriefingValidationError(f"{field} must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise BriefingValidationError(f"{field} must contain a public host without userinfo")
    if not parsed.hostname.isascii() or "%" in parsed.hostname:
        raise BriefingValidationError(f"{field} contains an unsafe encoded host")
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        raise BriefingValidationError(f"{field} must use the default http or https port")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise BriefingValidationError(f"{field} contains an invalid host") from exc
    if (
        not hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        raise BriefingValidationError(f"{field} must contain a public host")
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        address = None
    if address is None and "." not in hostname:
        raise BriefingValidationError(f"{field} must contain a public host")
    if address is None and re.fullmatch(
        r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
        hostname,
        re.IGNORECASE,
    ):
        raise BriefingValidationError(
            f"{field} must not contain an ambiguous numeric IP address"
        )
    if address is not None and (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise BriefingValidationError(f"{field} must not contain a non-public IP address")

    query = urllib.parse.parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=False,
    )
    if any(_query_key_is_sensitive(key) for key, _ in query):
        raise BriefingValidationError(
            f"{field} must not contain sensitive query parameters"
        )

    public_hostname = f"[{hostname}]" if ":" in hostname else hostname
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    public_query = [
        (key, item)
        for key, item in query
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith("utm_")
    ]
    public_query.sort()
    canonical = urllib.parse.urlunsplit(
        (
            scheme,
            public_hostname,
            path,
            urllib.parse.urlencode(public_query, doseq=True),
            "",
        )
    )
    if len(canonical) > MAX_URL_LENGTH:
        raise BriefingValidationError(f"{field} canonical form is too long")
    return canonical


def _host_matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _official_host(host: str) -> bool:
    return bool(
        host.endswith(".gov")
        or host == "gov.cn"
        or host.endswith(".gov.cn")
        or _host_matches(host, _OFFICIAL_EXCHANGE_HOSTS)
    )


def _original_social_post(source: str, source_url: str) -> bool:
    parsed = urllib.parse.urlsplit(source_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = urllib.parse.unquote(parsed.path)
    source_text = source.strip().casefold()
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        match = re.fullmatch(
            r"/([A-Za-z0-9_]{1,15})/status/([0-9]+)(?:/.*)?",
            path,
        )
        if match is None:
            return False
        prefix = "x @" if host in {"x.com", "www.x.com"} else "twitter @"
        return source_text == f"{prefix}{match.group(1).casefold()}"
    if host in {"truthsocial.com", "www.truthsocial.com"}:
        match = re.fullmatch(r"/@([^/]+)/(?:posts/)?([0-9]+)(?:/.*)?", path)
        return bool(
            match is not None
            and source_text == f"truth social @{match.group(1).casefold()}"
        )
    return False


def _bounded_integer(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BriefingValidationError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise BriefingValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BriefingValidationError(f"{field} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise BriefingValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise BriefingValidationError(
            f"{field} must be a finite number between {minimum:g} and {maximum:g}"
        )
    return round(number, 1)


def _discovery_channels(value: Any, field: str, *, kind: str) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BriefingValidationError(f"{field} must be an array of strings")
    if not value or len(value) > len(DISCOVERY_CHANNELS):
        raise BriefingValidationError(
            f"{field} must contain between 1 and {len(DISCOVERY_CHANNELS)} values"
        )
    output: list[str] = []
    for index, raw in enumerate(value):
        channel = _text(raw, f"{field}[{index}]", maximum=40)
        if channel not in DISCOVERY_CHANNELS:
            raise BriefingValidationError(
                f"{field}[{index}] must be a supported discovery channel"
            )
        if channel in output:
            raise BriefingValidationError(f"{field} must not contain duplicates")
        output.append(channel)
    allowed = _KIND_CHANNELS[kind]
    if not set(output).intersection(allowed):
        raise BriefingValidationError(
            f"{field} must include a primary channel for kind={kind}"
        )
    return output


def _optional_public_url(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return canonicalize_source_url(value, field)


def _specific_original_url(value: str) -> bool:
    """Accept only a conservative, article-like canonical URL."""
    if _ARXIV_URL_RE.fullmatch(urllib.parse.unquote(value.strip())):
        return True
    parsed = urllib.parse.urlsplit(value)
    segments = [
        urllib.parse.unquote(segment).casefold()
        for segment in parsed.path.split("/")
        if segment
    ]
    while segments and _LOCALE_SEGMENT_RE.fullmatch(segments[0]):
        segments.pop(0)
    if not segments:
        return False

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
        return False
    query_has_identity = any(
        key.casefold() in _IDENTITY_QUERY_KEYS and bool(query_value)
        for key, query_value in urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=False,
        )
    )
    if suffix or query_has_identity or re.search(r"\d", stem):
        return True
    return len(segments) >= 2


def _validate_discovery_metadata(
    value: Mapping[str, Any],
    *,
    field: str,
    source: str,
    source_url: str,
    published_time: datetime | None,
    fetched_time: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """Validate optional discovery provenance without trusting producer labels."""
    raw_verified = value.get("publication_time_verified")
    if raw_verified is None:
        publication_time_verified = published_time is not None
    elif not isinstance(raw_verified, bool):
        raise BriefingValidationError(
            f"{field}.publication_time_verified must be a boolean"
        )
    else:
        publication_time_verified = raw_verified
    if publication_time_verified != (published_time is not None):
        raise BriefingValidationError(
            f"{field}.publication_time_verified must match published_at"
        )

    raw_kind = value.get("kind")
    if raw_kind is None:
        special_fields = {
            "discovered_via",
            "featured_at",
            "original_url",
            "discussion_url",
            "hn_id",
            "hn_score",
            "hn_comments",
            "hn_rank",
            "heat_score",
        }
        if any(name in value and value.get(name) is not None for name in special_fields):
            raise BriefingValidationError(
                f"{field}.kind is required for discovery metadata"
            )
        return {"publication_time_verified": publication_time_verified}

    kind = _text(raw_kind, f"{field}.kind", maximum=32)
    if kind not in DISCOVERY_KINDS:
        raise BriefingValidationError(
            f"{field}.kind must be one of {', '.join(DISCOVERY_KINDS)}"
        )
    if fetched_time is None:
        raise BriefingValidationError(f"{field}.fetched_at is required for {kind}")
    if raw_verified is None:
        raise BriefingValidationError(
            f"{field}.publication_time_verified is required for {kind}"
        )
    channels = _discovery_channels(
        value.get("discovered_via"),
        f"{field}.discovered_via",
        kind=kind,
    )
    expected_source = _KIND_SOURCE_LABELS[kind]
    if source.casefold() != expected_source:
        raise BriefingValidationError(
            f"{field}.source must be {_KIND_SOURCE_LABELS[kind]!r} for {kind}"
        )
    featured_at, featured_time = _timestamp(
        value.get("featured_at"),
        f"{field}.featured_at",
        now=now,
    )
    assert featured_at is not None and featured_time is not None
    if featured_time > fetched_time + MAX_FUTURE_SKEW:
        raise BriefingValidationError(
            f"{field}.featured_at cannot be after fetched_at"
        )
    if published_time is not None and published_time > featured_time + MAX_FUTURE_SKEW:
        raise BriefingValidationError(
            f"{field}.featured_at cannot be before published_at"
        )

    original_url = _optional_public_url(
        value.get("original_url"), f"{field}.original_url"
    )
    discussion_url = _optional_public_url(
        value.get("discussion_url"), f"{field}.discussion_url"
    )
    output: dict[str, Any] = {
        "kind": kind,
        "discovered_via": channels,
        "publication_time_verified": publication_time_verified,
        "featured_at": featured_at,
        "original_url": original_url,
        "discussion_url": discussion_url,
    }

    has_hn_channel = bool(
        set(channels).intersection({"hacker_news_top", "hacker_news_best"})
    )
    if has_hn_channel:
        hn_id = _bounded_integer(
            value.get("hn_id"),
            f"{field}.hn_id",
            minimum=1,
            maximum=MAX_HN_ID,
        )
        if discussion_url != f"https://news.ycombinator.com/item?id={hn_id}":
            raise BriefingValidationError(
                f"{field}.discussion_url must match hn_id on news.ycombinator.com"
            )
        output.update(
            {
                "hn_id": hn_id,
                "hn_score": _bounded_integer(
                    value.get("hn_score"),
                    f"{field}.hn_score",
                    minimum=0,
                    maximum=MAX_HN_ENGAGEMENT,
                ),
                "hn_comments": _bounded_integer(
                    value.get("hn_comments"),
                    f"{field}.hn_comments",
                    minimum=0,
                    maximum=MAX_HN_ENGAGEMENT,
                ),
                "hn_rank": _bounded_integer(
                    value.get("hn_rank"),
                    f"{field}.hn_rank",
                    minimum=1,
                    maximum=MAX_HN_RANK,
                ),
                "heat_score": _bounded_number(
                    value.get("heat_score"),
                    f"{field}.heat_score",
                    minimum=0,
                    maximum=100,
                ),
            }
        )
    else:
        if discussion_url is not None:
            raise BriefingValidationError(
                f"{field}.discussion_url requires a Hacker News discovery channel"
            )
        for hn_field in ("hn_id", "hn_score", "hn_comments", "hn_rank", "heat_score"):
            if value.get(hn_field) is not None:
                raise BriefingValidationError(
                    f"{field}.{hn_field} requires a Hacker News discovery channel"
                )

    if kind == "hn_story":
        if not publication_time_verified or published_time is None:
            raise BriefingValidationError(
                f"{field}.published_at must contain the verified HN submission time"
            )
        if original_url is None:
            if source_url != discussion_url:
                raise BriefingValidationError(
                    f"{field}.source_url must be the HN discussion when original_url is absent"
                )
        elif source_url != original_url:
            raise BriefingValidationError(
                f"{field}.source_url must match original_url for an HN external story"
            )
    else:
        expected_host = _KIND_SOURCE_HOSTS[kind]
        if urllib.parse.urlsplit(source_url).hostname != expected_host:
            raise BriefingValidationError(
                f"{field}.source_url must use {expected_host} for {kind}"
            )
        if original_url is not None and (
            original_url == source_url
            or urllib.parse.urlsplit(original_url).hostname == expected_host
            or not _specific_original_url(original_url)
        ):
            raise BriefingValidationError(
                f"{field}.original_url must be a distinct underlying source for {kind}"
            )
    return output


def _validated_source_tier(
    claimed: str,
    source: str,
    source_url: str,
    *,
    force_discovery: bool = False,
) -> str:
    """Return only a source tier established by the supplied public evidence.

    The producer's tier is a claim, not authority.  A failed official,
    first-party or media claim falls all the way back to discovery instead of
    being silently reinterpreted into a more favourable tier.
    """
    if force_discovery:
        return "discovery"
    parsed = urllib.parse.urlsplit(source_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if claimed == "official":
        return "official" if _official_host(host) else "discovery"
    if claimed == "first_party":
        return (
            "first_party"
            if _original_social_post(source, source_url)
            else "discovery"
        )
    if claimed == "media":
        is_article = parsed.path not in {"", "/"}
        is_aggregator = _host_matches(host, _AGGREGATOR_HOSTS) or bool(
            _AGGREGATOR_SOURCE_RE.search(source)
        )
        if (
            is_article
            and not is_aggregator
            and not _official_host(host)
            and not _host_matches(host, _SOCIAL_HOSTS)
        ):
            return "media"
        return "discovery"
    return "discovery"


def _story_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = []
    for char in normalized:
        category = unicodedata.category(char)
        characters.append(" " if category[0] in {"P", "S", "Z", "C"} else char)
    return " ".join("".join(characters).split())


def _assets(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BriefingValidationError(f"{field} must be an array of strings")
    if len(value) > MAX_ASSETS:
        raise BriefingValidationError(f"{field} contains too many assets")
    output: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        asset = _text(
            item,
            f"{field}[{index}]",
            maximum=MAX_ASSET_LENGTH,
        )
        identity = asset.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        output.append(asset)
    return output


def _same_normalized_text(left: str, right: str) -> bool:
    return unicodedata.normalize("NFKC", left).casefold() == unicodedata.normalize(
        "NFKC", right
    ).casefold()


def _content_enhancement(
    value: Mapping[str, Any],
    *,
    field: str,
    title: str,
    kind: str | None,
    original_url: str | None,
) -> dict[str, Any]:
    """Validate one optional Chinese-summary and topic bundle.

    The original title remains the event identity.  These fields are a bounded
    display enhancement and therefore have no authority over source tier,
    section placement, deduplication or ordering.
    """
    if not any(name in value for name in _CONTENT_ENHANCEMENT_FIELDS):
        # Preserve compatibility with already-imported v1 snapshots. New
        # discovery producers write the complete bundle, including an explicit
        # summary basis even when translation is unavailable.
        return {}

    title_zh = _text(
        value.get("title_zh"),
        f"{field}.title_zh",
        maximum=MAX_TITLE_ZH_LENGTH,
        required=False,
    )
    summary_zh = _text(
        value.get("summary_zh"),
        f"{field}.summary_zh",
        maximum=MAX_SUMMARY_ZH_LENGTH,
        required=False,
    )
    for name, text in (("title_zh", title_zh), ("summary_zh", summary_zh)):
        if text and _HAN_RE.search(text) is None:
            raise BriefingValidationError(
                f"{field}.{name} must contain Chinese text"
            )

    summary_basis = _text(
        value.get("summary_basis"),
        f"{field}.summary_basis",
        maximum=32,
    )
    if summary_basis not in SUMMARY_BASES:
        raise BriefingValidationError(
            f"{field}.summary_basis must be one of {', '.join(SUMMARY_BASES)}"
        )
    translation_status = _text(
        value.get("translation_status"),
        f"{field}.translation_status",
        maximum=24,
    )
    if translation_status not in TRANSLATION_STATUSES:
        raise BriefingValidationError(
            f"{field}.translation_status must be one of "
            f"{', '.join(TRANSLATION_STATUSES)}"
        )

    if summary_basis == "title_only" and summary_zh:
        raise BriefingValidationError(
            f"{field}.summary_zh is not allowed for title_only evidence"
        )
    if summary_basis == "curated_excerpt" and kind not in {
        "ai_digest",
        "paper_digest",
    }:
        raise BriefingValidationError(
            f"{field}.summary_basis=curated_excerpt requires a curated kind"
        )
    if summary_basis == "self_post" and (
        kind != "hn_story" or original_url is not None
    ):
        raise BriefingValidationError(
            f"{field}.summary_basis=self_post requires an HN self-post "
            "without original_url"
        )

    if translation_status == "translated":
        if not title_zh:
            raise BriefingValidationError(
                f"{field}.title_zh is required when translation_status=translated"
            )
        if summary_basis != "title_only" and not summary_zh:
            raise BriefingValidationError(
                f"{field}.summary_zh is required for translated excerpt evidence"
            )
    elif translation_status == "source_zh":
        if title_zh:
            if not _same_normalized_text(title_zh, title):
                raise BriefingValidationError(
                    f"{field}.title_zh must match the Chinese source title when "
                    "translation_status=source_zh"
                )
            # Do not persist a duplicate display title.
            title_zh = ""
        if _HAN_RE.search(title) is None and not summary_zh:
            raise BriefingValidationError(
                f"{field}.translation_status=source_zh requires Chinese source "
                "content"
            )
    elif title_zh or summary_zh:
        raise BriefingValidationError(
            f"{field} cannot include Chinese enhancement text when "
            "translation_status=unavailable"
        )

    taxonomy_version = _text(
        value.get("taxonomy_version"),
        f"{field}.taxonomy_version",
        maximum=32,
    )
    if taxonomy_version != briefing_topics.TAXONOMY_VERSION:
        raise BriefingValidationError(
            f"{field}.taxonomy_version must be {briefing_topics.TAXONOMY_VERSION}"
        )
    content_category = _text(
        value.get("content_category"),
        f"{field}.content_category",
        maximum=40,
    )
    raw_tags = value.get("content_tags")
    try:
        topic_assignment = briefing_topics.validate_topic_assignment(
            content_category,
            raw_tags,
            taxonomy_version=taxonomy_version,
        )
    except (TypeError, ValueError) as exc:
        raise BriefingValidationError(
            f"{field} contains an invalid content topic assignment"
        ) from exc

    output: dict[str, Any] = {
        "summary_basis": summary_basis,
        "content_category": topic_assignment.primary,
        "content_tags": list(topic_assignment.tags),
        "taxonomy_version": topic_assignment.version,
        "translation_status": translation_status,
    }
    if title_zh:
        output["title_zh"] = title_zh
    if summary_zh:
        output["summary_zh"] = summary_zh
    return output


def _validate_item(
    value: Any,
    *,
    section: str,
    index: int,
    order: int,
    now: datetime,
) -> dict[str, Any]:
    field = f"sections.{section}[{index}]"
    if not isinstance(value, Mapping):
        raise BriefingValidationError(f"{field} must be an object")
    unknown = set(value) - _ITEM_FIELDS
    if unknown:
        raise BriefingValidationError(
            f"{field} contains unsupported fields: {', '.join(sorted(map(str, unknown)))}"
        )
    title = _text(value.get("title"), f"{field}.title", maximum=MAX_TITLE_LENGTH)
    raw_source_url = _text(
        value.get("source_url"),
        f"{field}.source_url",
        maximum=MAX_URL_LENGTH,
    )
    canonical_url = canonicalize_source_url(raw_source_url, f"{field}.source_url")
    source = _text(
        value.get("source"),
        f"{field}.source",
        maximum=MAX_SOURCE_LENGTH,
        required=False,
    )
    tier = _text(
        value.get("source_tier"),
        f"{field}.source_tier",
        maximum=32,
    )
    if tier not in SOURCE_TIERS:
        raise BriefingValidationError(
            f"{field}.source_tier must be one of {', '.join(SOURCE_TIERS)}"
        )
    published_at, published_time = _timestamp(
        value.get("published_at"),
        f"{field}.published_at",
        now=now,
        required=False,
    )
    fetched_at, fetched_time = _timestamp(
        value.get("fetched_at"),
        f"{field}.fetched_at",
        now=now,
        required=False,
    )
    if published_time is None and fetched_time is None:
        raise BriefingValidationError(
            f"{field} requires published_at or fetched_at"
        )
    discovery_metadata = _validate_discovery_metadata(
        value,
        field=field,
        source=source,
        source_url=canonical_url,
        published_time=published_time,
        fetched_time=fetched_time,
        now=now,
    )
    enhancement = _content_enhancement(
        value,
        field=field,
        title=title,
        kind=discovery_metadata.get("kind"),
        original_url=discovery_metadata.get("original_url"),
    )
    canonical_identity_url = (
        discovery_metadata.get("original_url") or canonical_url
    )
    disclosed_at, disclosed_time = _timestamp(
        value.get("disclosed_at"),
        f"{field}.disclosed_at",
        now=now,
        required=False,
    )
    effective_values: list[str] = []
    for alias in ("effective_at", "period_end", "data_as_of"):
        normalized = _effective_date_or_timestamp(
            value.get(alias),
            f"{field}.{alias}",
            now=now,
        )
        if normalized is not None:
            effective_values.append(normalized)
    if len(set(effective_values)) > 1:
        raise BriefingValidationError(
            f"{field} contains conflicting effective_at, period_end or data_as_of values"
        )
    effective_at = effective_values[0] if effective_values else None
    if section == "investors" and disclosed_time is not None and effective_at:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective_at):
            effective_day = date.fromisoformat(effective_at)
        else:
            effective_day = datetime.fromisoformat(effective_at).astimezone(
                _BEIJING
            ).date()
        disclosed_day = disclosed_time.astimezone(_BEIJING).date()
        if effective_day > disclosed_day:
            raise BriefingValidationError(
                f"{field}.effective_at cannot be after disclosed_at in "
                "Asia/Shanghai"
            )
    why = value.get("why_it_matters")
    legacy_why = value.get("why")
    if why is not None and legacy_why is not None and why != legacy_why:
        raise BriefingValidationError(
            f"{field} cannot provide conflicting why and why_it_matters values"
        )
    normalized_featured_at = discovery_metadata.get("featured_at")
    featured_time = (
        datetime.fromisoformat(normalized_featured_at)
        if isinstance(normalized_featured_at, str)
        else None
    )
    updated = (
        published_time
        if published_time is not None
        else featured_time
        if featured_time is not None
        else fetched_time
    )
    assert updated is not None
    normalized_title = _story_title(title)
    if not normalized_title:
        raise BriefingValidationError(f"{field}.title has no searchable content")
    output = {
        "section": section,
        "title": title,
        "source": source,
        "source_url": canonical_url,
        "canonical_url": canonical_identity_url,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "disclosed_at": disclosed_at,
        "effective_at": effective_at,
        "time_status": (
            "verified"
            if published_at is not None
            else "featured_only"
            if featured_time is not None
            else "fetched_only"
        ),
        "source_tier": _validated_source_tier(
            tier,
            source,
            canonical_url,
            force_discovery=bool(discovery_metadata.get("kind")),
        ),
        "summary": _text(
            value.get("summary"),
            f"{field}.summary",
            maximum=MAX_SUMMARY_LENGTH,
            required=False,
        ),
        "why_it_matters": _text(
            why if why is not None else legacy_why,
            f"{field}.why_it_matters",
            maximum=MAX_WHY_LENGTH,
            required=False,
        ),
        "assets": _assets(value.get("assets"), f"{field}.assets"),
        "last_updated_at": updated.isoformat(),
        "_normalized_title": normalized_title,
        "_order": order,
    }
    output.update(discovery_metadata)
    output.update(enhancement)
    return output


def _deduplicate(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    # A landing-page URL is not an event identifier: publishers commonly reuse
    # one page for unrelated notices.  Pre-import merging is URL scoped even
    # for a named label such as "OpenAI Update"; the read service owns the much
    # narrower, action-aware cross-source semantic pass.  Opposite actions
    # remain distinct because the complete title is part of every signature.
    groups_by_signature: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        event_day = (
            datetime.fromisoformat(
                item["published_at"]
                or item.get("featured_at")
                or item["fetched_at"]
            )
            .astimezone(_BEIJING)
            .date()
            .isoformat()
        )
        groups_by_signature.setdefault(
            (item["_normalized_title"], event_day, item["canonical_url"]),
            [],
        ).append(item)
    groups = list(groups_by_signature.values())

    # Preserve the canonical-URL story key for the normal one-event-per-URL
    # case.  When a reused URL occurs in multiple retained groups, add the
    # event signature so those distinct stories cannot expose the same key.
    url_group_counts: dict[str, int] = {}
    for group in groups:
        for canonical_url in {item["canonical_url"] for item in group}:
            url_group_counts[canonical_url] = (
                url_group_counts.get(canonical_url, 0) + 1
            )

    primary_section_rank = {
        section: index
        for index, section in enumerate(
            ("investors", "ai", "technology", "macro", "world", "finance")
        )
    }
    tier_rank = {tier: index for index, tier in enumerate(SOURCE_TIERS)}
    output = {section: [] for section in SECTION_KEYS}
    for group in groups:
        primary = min(
            group,
            key=lambda item: primary_section_rank[item["section"]],
        )["section"]
        ranked_group = sorted(
            group,
            key=lambda item: (
                0 if item["published_at"] is not None else 1,
                tier_rank[item["source_tier"]],
                0 if item["disclosed_at"] is not None else 1,
                0 if item["effective_at"] is not None else 1,
                0 if item["summary"] else 1,
                0 if item["why_it_matters"] else 1,
                -datetime.fromisoformat(item["last_updated_at"]).timestamp(),
                item["_order"],
            ),
        )
        representative = ranked_group[0]
        cross_tags = [
            section
            for section in SECTION_KEYS
            if section != primary and any(item["section"] == section for item in group)
        ]
        unique_urls = {item["canonical_url"] for item in group}
        merged_assets: list[str] = []
        seen_assets: set[str] = set()
        for item in sorted(group, key=lambda entry: entry["_order"]):
            for asset in item["assets"]:
                identity = asset.casefold()
                if identity in seen_assets or len(merged_assets) >= MAX_ASSETS:
                    continue
                seen_assets.add(identity)
                merged_assets.append(asset)
        story_identity = min(item["canonical_url"] for item in group)
        if url_group_counts[story_identity] > 1:
            event_day = (
                datetime.fromisoformat(
                    representative["published_at"]
                    or representative.get("featured_at")
                    or representative["fetched_at"]
                )
                .astimezone(_BEIJING)
                .date()
                .isoformat()
            )
            story_identity = "\x1f".join(
                (
                    story_identity,
                    event_day,
                    representative["_normalized_title"],
                )
            )
        clean = {
            key: value
            for key, value in representative.items()
            if not key.startswith("_")
        }
        # A localization/topic assignment is one evidence-bound bundle. If a
        # stronger factual representative has no usable Chinese projection,
        # it may inherit one only from the same validated tier, time semantics
        # and discovery kind. Never splice a translated title from one record
        # together with another record's summary basis or taxonomy.
        if clean.get("translation_status") not in {"translated", "source_zh"}:
            matching_enhancement = next(
                (
                    item
                    for item in ranked_group
                    if item.get("translation_status")
                    in {"translated", "source_zh"}
                    and item["source_tier"] == representative["source_tier"]
                    and item["time_status"] == representative["time_status"]
                    and item.get("kind") == representative.get("kind")
                ),
                None,
            )
            if matching_enhancement is not None:
                for name in _CONTENT_ENHANCEMENT_FIELDS:
                    clean.pop(name, None)
                clean.update(
                    {
                        name: matching_enhancement[name]
                        for name in _CONTENT_ENHANCEMENT_FIELDS
                        if name in matching_enhancement
                    }
                )
        for disclosure_field in ("disclosed_at", "effective_at"):
            if clean.get(disclosure_field) is None:
                clean[disclosure_field] = next(
                    (
                        item[disclosure_field]
                        for item in ranked_group
                        if item[disclosure_field] is not None
                    ),
                    None,
                )
        clean.update(
            {
                "section": primary,
                "story_key": hashlib.sha256(
                    story_identity.encode("utf-8")
                ).hexdigest(),
                "source_count": len(unique_urls),
                "last_updated_at": max(
                    item["published_at"]
                    for item in group
                    if item["published_at"] is not None
                )
                if any(item["published_at"] is not None for item in group)
                else max(item["last_updated_at"] for item in group),
                "cross_tags": cross_tags,
                "assets": merged_assets,
            }
        )
        output[primary].append(clean)

    for section in SECTION_KEYS:
        output[section].sort(
            key=lambda item: (
                item["last_updated_at"],
                item["title"].casefold(),
                item["story_key"],
            ),
            reverse=True,
        )
    return output


def validate_payload(
    payload: Any,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Validate, normalize, and deduplicate one v1 snapshot."""
    current = _current_time(now)
    if not isinstance(payload, Mapping):
        raise BriefingValidationError("payload must be an object")
    unknown = set(payload) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(payload)
    if unknown:
        raise BriefingValidationError(
            f"payload contains unsupported fields: {', '.join(sorted(map(str, unknown)))}"
        )
    if missing:
        raise BriefingValidationError(
            f"payload is missing required fields: {', '.join(sorted(missing))}"
        )
    schema = payload.get("schema_version")
    if isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise BriefingValidationError(
            f"schema_version must be {SCHEMA_VERSION}"
        )
    day_text = _text(payload.get("snapshot_date"), "snapshot_date", maximum=10)
    try:
        parsed_day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise BriefingValidationError(
            "snapshot_date must be an ISO-8601 date"
        ) from exc
    if parsed_day.isoformat() != day_text:
        raise BriefingValidationError("snapshot_date must use YYYY-MM-DD")
    generated_at, generated_time = _timestamp(
        payload.get("generated_at"),
        "generated_at",
        now=current,
    )
    source_as_of, source_time = _timestamp(
        payload.get("source_as_of"),
        "source_as_of",
        now=current,
    )
    assert generated_at is not None and generated_time is not None
    assert source_as_of is not None and source_time is not None
    if parsed_day != generated_time.astimezone(_BEIJING).date():
        raise BriefingValidationError(
            "snapshot_date must match generated_at in Asia/Shanghai"
        )
    if source_time > generated_time + MAX_FUTURE_SKEW:
        raise BriefingValidationError("source_as_of cannot be after generated_at")

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, Mapping):
        raise BriefingValidationError("sections must be an object")
    section_names = set(raw_sections)
    expected_sections = set(SECTION_KEYS)
    if section_names != expected_sections:
        unknown_sections = section_names - expected_sections
        missing_sections = expected_sections - section_names
        details = []
        if unknown_sections:
            details.append(
                "unknown: " + ", ".join(sorted(map(str, unknown_sections)))
            )
        if missing_sections:
            details.append("missing: " + ", ".join(sorted(missing_sections)))
        raise BriefingValidationError("invalid sections (" + "; ".join(details) + ")")

    items: list[dict[str, Any]] = []
    order = 0
    for section in SECTION_KEYS:
        values = raw_sections[section]
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise BriefingValidationError(f"sections.{section} must be an array")
        if len(values) > MAX_ITEMS_PER_SECTION:
            raise BriefingValidationError(
                f"sections.{section} contains too many items"
            )
        for index, value in enumerate(values):
            items.append(
                _validate_item(
                    value,
                    section=section,
                    index=index,
                    order=order,
                    now=current,
                )
            )
            order += 1
            if order > MAX_TOTAL_ITEMS:
                raise BriefingValidationError("payload contains too many items")
    for item in items:
        for timestamp_field in (
            "published_at",
            "fetched_at",
            "featured_at",
            "last_updated_at",
            "disclosed_at",
        ):
            raw_timestamp = item.get(timestamp_field)
            if raw_timestamp is None:
                continue
            item_time = datetime.fromisoformat(raw_timestamp)
            latest_allowed = (
                source_time
                if timestamp_field == "fetched_at"
                else source_time + MAX_FUTURE_SKEW
            )
            if item_time > latest_allowed:
                raise BriefingValidationError(
                    f"item {timestamp_field} cannot be after source_as_of"
                )
        effective_at = item.get("effective_at")
        if isinstance(effective_at, str):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective_at):
                if date.fromisoformat(effective_at) > source_time.astimezone(
                    _BEIJING
                ).date():
                    raise BriefingValidationError(
                        "item effective_at cannot be after source_as_of"
                    )
            elif datetime.fromisoformat(effective_at) > source_time + MAX_FUTURE_SKEW:
                raise BriefingValidationError(
                    "item effective_at cannot be after source_as_of"
                )

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_date": day_text,
        "generated_at": generated_at,
        "source_as_of": source_as_of,
        "sections": _deduplicate(items),
    }
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BriefingValidationError("payload must contain JSON-safe values") from exc
    if len(encoded) > MAX_NORMALIZED_PAYLOAD_BYTES:
        raise BriefingValidationError("normalized payload exceeds the supported size")
    return normalized


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise BriefingValidationError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def load_json_file(path: str | Path) -> Any:
    """Load one bounded, regular UTF-8 JSON file without following symlinks."""
    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise BriefingValidationError(f"cannot read briefing file: {source}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BriefingValidationError(
                "briefing file must be a regular non-symlink file"
            )
        if metadata.st_size > MAX_FILE_BYTES:
            raise BriefingValidationError("briefing file exceeds the supported size")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_FILE_BYTES:
            raise BriefingValidationError("briefing file exceeds the supported size")
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError as exc:
        raise BriefingValidationError("briefing file must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise BriefingValidationError("briefing file must contain valid JSON") from exc
    except OSError as exc:
        raise BriefingValidationError(f"cannot read briefing file: {source}") from exc
    finally:
        os.close(descriptor)


def import_payload(
    payload: Any,
    *,
    repository: Any = db,
    now: datetime | str | None = None,
    imported_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Validate and persist a snapshot, returning only non-sensitive metadata."""
    normalized = validate_payload(payload, now=now)
    imported_time = _current_time(imported_at or now).isoformat()
    repository.init()
    snapshot_id = repository.upsert_daily_briefing_snapshot(
        snapshot_date=normalized["snapshot_date"],
        schema_version=normalized["schema_version"],
        generated_at=normalized["generated_at"],
        source_as_of=normalized["source_as_of"],
        payload=normalized,
        imported_at=imported_time,
    )
    counts = {
        section: len(normalized["sections"][section])
        for section in SECTION_KEYS
    }
    return {
        "snapshot_id": snapshot_id,
        "snapshot_date": normalized["snapshot_date"],
        "schema_version": normalized["schema_version"],
        "generated_at": normalized["generated_at"],
        "source_as_of": normalized["source_as_of"],
        "section_counts": counts,
        "item_count": sum(counts.values()),
    }


def import_file(
    path: str | Path,
    *,
    repository: Any = db,
    now: datetime | str | None = None,
    imported_at: datetime | str | None = None,
) -> dict[str, Any]:
    return import_payload(
        load_json_file(path),
        repository=repository,
        now=now,
        imported_at=imported_at,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and import one Daily Briefing v1 JSON snapshot.",
    )
    parser.add_argument("json_file", help="Path to a regular UTF-8 JSON file")
    parser.add_argument(
        "--db",
        dest="database_path",
        help="SQLite database path (defaults to KOL_DASHBOARD_DB)",
    )
    args = parser.parse_args(argv)
    if args.database_path:
        db.DB_PATH = str(Path(args.database_path).expanduser())
    try:
        result = import_file(args.json_file)
    except (BriefingValidationError, OSError, ValueError) as exc:
        print(f"briefing import rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BriefingValidationError",
    "SCHEMA_VERSION",
    "SECTION_KEYS",
    "canonicalize_source_url",
    "import_file",
    "import_payload",
    "load_json_file",
    "main",
    "validate_payload",
]
