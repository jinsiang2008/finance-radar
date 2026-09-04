"""
SQLite layer for KOL dashboard.

Schema:
  events(id, dedup_key UNIQUE, url_hash, url, canonical_url, title, snippet, source,
         kol_key, kol_name, kol_name_cn,
         impact, has_market_kw, source_count, fetched_at, last_seen_at, published_at)

  event_enrichments(event_id PRIMARY KEY, input_hash, prompt_version, status,
                    headline_zh, summary_zh, why_it_matters_zh, impact_level,
                    impact_path_json, tags_json, assets_json, cluster_key,
                    language, confidence, evidence_basis, model, generated_at)

  macro_event_enrichments(event_key PRIMARY KEY, input_hash, prompt_version,
                          status, headline_zh, summary_zh, why_it_matters_zh,
                          impact_level, impact_path_json, tags_json,
                          assets_json, cluster_key, language, confidence,
                          evidence_basis, model, generated_at)

  event_sightings(event_id, title, snippet, tickers, kol_key, kol_name,
                  kol_name_cn, source, source_url, published_at, first_seen_at,
                  last_seen_at, source_count)

  relations(source_type, source_id, topic_key, asset_key, relation_type, direction,
            strength, confidence, horizon, method, rationale, evidence_json, created_at)

  macro_snapshots(id, created_at, composite_score, composite_level, payload)

  decision_snapshots(id, schema_version, engine_version, source_hash,
                     source_as_of, generated_at, summary_json, full_json)

  meta(key, value)  — small KV store for last-sent-id state etc.

Dedup: news aggregators hand out a fresh tracking URL for the same article on
every fetch, so url_hash alone lets the same story in hundreds of times. The
authoritative key is a normalized-title fingerprint; repeat sightings bump
source_count instead of creating rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from kol_dashboard.content_quality import is_event_content_eligible
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    from content_quality import is_event_content_eligible

try:
    from kol_dashboard.event_relevance import (
        KOL_DIRECTORY,
        assess_event_relevance,
        is_owned_direct_source,
    )
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    from event_relevance import (  # type: ignore
        KOL_DIRECTORY,
        assess_event_relevance,
        is_owned_direct_source,
    )

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = os.environ.get(
    "KOL_DASHBOARD_DB", str(_DEFAULT_DATA_DIR / "kol_dashboard.db")
)

# ``PRAGMA user_version`` is the durable, transactionally updated schema marker.
# Bump this whenever ``init`` gains a new migration that existing databases must
# execute.  A current database takes the read-only fast path instead of scanning
# and rewriting the event tables on every process start.
_DB_SCHEMA_VERSION = 3
_BEGIN_RETRY_ENV = "KOL_DB_BEGIN_RETRY_SECONDS"
_DEFAULT_BEGIN_RETRY_SECONDS = 30.0
_MAX_BEGIN_RETRY_SECONDS = 120.0
_BEGIN_RETRY_INITIAL_DELAY_SECONDS = 0.025
_BEGIN_RETRY_MAX_DELAY_SECONDS = 0.5

_AI_REQUEST_COOLDOWN_ENV = "KOL_AI_REQUEST_COOLDOWN_SECONDS"
_AI_REQUEST_HOURLY_ENV = "KOL_AI_REQUEST_HOURLY_LIMIT"
_AI_REQUEST_DAILY_ENV = "KOL_AI_REQUEST_DAILY_LIMIT"
_AI_REQUEST_PENDING_ENV = "KOL_AI_REQUEST_PENDING_LIMIT"
_AI_REQUEST_RETENTION_ENV = "KOL_AI_REQUEST_RETENTION_DAYS"

RETENTION_DAYS = 14
MACRO_RETENTION_DAYS = 90
MAX_EVENT_KOL_FILTERS = 20


def _event_relevance_args_sql(sighting_alias: str) -> str:
    """Build a relevance call from the exact sighting's evidence text."""
    identity = sighting_alias
    return (
        f"{identity}.title, {identity}.snippet, {identity}.source, "
        f"{identity}.source_url, "
        f"{identity}.kol_key, {identity}.kol_name, {identity}.kol_name_cn"
    )


def _event_intelligence_sql(sighting_alias: str) -> str:
    return (
        "event_intelligence_eligible("
        f"{_event_relevance_args_sql(sighting_alias)})=1"
    )


def _event_finance_sql(sighting_alias: str) -> str:
    return (
        "event_finance_relevant("
        f"{_event_relevance_args_sql(sighting_alias)})"
    )


def _event_rule_impact_sql(sighting_alias: str) -> str:
    return (
        "event_rule_impact("
        f"{_event_relevance_args_sql(sighting_alias)})"
    )


def _event_owned_direct_sql(sighting_alias: str) -> str:
    return (
        "event_owned_direct_source("
        f"{sighting_alias}.source, {sighting_alias}.source_url, "
        f"{sighting_alias}.kol_key)"
    )


def _event_eligible_source_count_sql(alias: str = "eligible_source") -> str:
    return (
        f"(SELECT COUNT(DISTINCT NULLIF(TRIM({alias}.source_url), '')) "
        f"FROM event_sightings {alias} "
        f"WHERE {alias}.event_id=e.id "
        f"AND {_event_intelligence_sql(alias)})"
    )


def _event_attribution_basis_sql(sighting_alias: str) -> str:
    return (
        "event_attribution_basis("
        f"{_event_relevance_args_sql(sighting_alias)})"
    )


def _event_matched_alias_sql(sighting_alias: str) -> str:
    return (
        "event_matched_alias("
        f"{_event_relevance_args_sql(sighting_alias)})"
    )


def _preferred_sighting_order_sql(alias: str = "s") -> str:
    """Stable best-evidence order shared by feed and enrichment paths."""
    return (
        f"CASE WHEN {alias}.published_at_status='verified' THEN 0 ELSE 1 END, "
        f"CASE WHEN {_event_owned_direct_sql(alias)}=1 THEN 0 ELSE 1 END, "
        f"CASE WHEN {alias}.published_at_status='verified' "
        f"THEN {alias}.published_at_epoch END DESC, "
        f"CASE WHEN {alias}.published_at_status<>'verified' "
        f"THEN {alias}.first_seen_at END DESC, "
        f"{alias}.last_seen_at DESC, {alias}.id DESC"
    )


def _preferred_event_sighting_id_sql(alias: str = "s") -> str:
    return (
        f"(SELECT {alias}.id FROM event_sightings {alias} "
        f"WHERE {alias}.event_id=e.id AND {_event_intelligence_sql(alias)} "
        f"ORDER BY {_preferred_sighting_order_sql(alias)} LIMIT 1)"
    )

_MARKET_REACTION_STATUSES = {
    "pending",
    "preliminary",
    "complete",
    "unavailable",
}
_MARKET_SERIES_STATUSES = {"available", "unavailable", "unsupported"}
_MARKET_REASON_CODES = {
    "asset_unavailable",
    "bad_payload",
    "baseline_unavailable",
    "benchmark_unavailable",
    "follow_up_unavailable",
    "insufficient_follow_up",
    "invalid_event_time",
    "invalid_interval",
    "invalid_range",
    "invalid_timeout",
    "no_common_trading_dates",
    "provider_error",
    "request_failed",
    "same_proxy_as_benchmark",
    "unknown",
    "unsupported_asset",
    "unsupported_benchmark",
    "window_not_due",
}

# Tracking params that differ per fetch and must not affect identity.
_TRACKING_PARAMS = {
    "ref", "aid", "tid", "c", "mkt", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content", "spm", "from", "src",
}


def _event_ai_cache_current_sql(
    input_hash: Any,
    prompt_version: Any,
    model: Any,
    title: Any,
    snippet: Any,
    source: Any,
    kol_name_cn: Any,
    kol_name: Any,
    tickers: Any,
    url: Any,
) -> int:
    try:
        from kol_dashboard import llm_enrichment as enrichment_domain
    except ModuleNotFoundError:  # Flat production bundle.
        import llm_enrichment as enrichment_domain

    _, current_hash = enrichment_domain.build_event_input(
        {
            "title": title,
            "snippet": snippet,
            "source": source,
            "kol_name_cn": kol_name_cn,
            "kol_name": kol_name,
            "tickers": tickers,
            "url": url,
        }
    )
    return int(
        str(input_hash or "") == current_hash
        and str(prompt_version or "") == enrichment_domain.PROMPT_VERSION
        and enrichment_domain.is_supported_model(model)
    )


def _begin_retry_budget_seconds() -> float:
    raw = os.environ.get(_BEGIN_RETRY_ENV)
    if raw is None:
        return _DEFAULT_BEGIN_RETRY_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BEGIN_RETRY_SECONDS
    if not math.isfinite(value):
        return _DEFAULT_BEGIN_RETRY_SECONDS
    return min(max(value, 0.0), _MAX_BEGIN_RETRY_SECONDS)


def _is_sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        # Extended result codes preserve the primary code in the low byte.
        if error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return True
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _begin_immediate(c: sqlite3.Connection) -> None:
    """Acquire the SQLite writer slot with bounded begin-only retries.

    The context body is entered only after this succeeds.  Operational errors
    raised by user SQL or commit are deliberately propagated and never replayed.
    """

    budget = _begin_retry_budget_seconds()
    deadline = time.monotonic() + budget
    delay = _BEGIN_RETRY_INITIAL_DELAY_SECONDS
    while True:
        try:
            c.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _BEGIN_RETRY_MAX_DELAY_SECONDS)


@contextmanager
def conn(*, immediate: bool = False):
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.create_function(
        "event_content_eligible",
        4,
        lambda title, snippet, source, url: int(
            is_event_content_eligible(
                {
                    "title": title,
                    "snippet": snippet,
                    "source": source,
                    "url": url,
                }
            )
        ),
        deterministic=True,
    )
    @lru_cache(maxsize=16_384)
    def relevance_assessment(
        title: Any,
        snippet: Any,
        source: Any,
        url: Any,
        kol_key: Any,
        kol_name: Any,
        kol_name_cn: Any,
    ) -> Mapping[str, Any]:
        return assess_event_relevance(
            {
                "title": title,
                "snippet": snippet,
                "source": source,
                "url": url,
                "kol_key": kol_key,
                "kol_name": kol_name,
                "kol_name_cn": kol_name_cn,
            }
        )

    c.create_function(
        "event_intelligence_eligible",
        7,
        lambda *values: int(
            bool(relevance_assessment(*values)["intelligence_eligible"])
        ),
        deterministic=True,
    )
    c.create_function(
        "event_finance_relevant",
        7,
        lambda *values: int(
            bool(relevance_assessment(*values)["finance_relevant"])
        ),
        deterministic=True,
    )
    c.create_function(
        "event_rule_impact",
        7,
        lambda *values: str(relevance_assessment(*values)["rule_impact"]),
        deterministic=True,
    )
    c.create_function(
        "event_owned_direct_source",
        3,
        lambda source, source_url, kol_key: int(
            is_owned_direct_source(
                {
                    "source": source,
                    "source_url": source_url,
                    "kol_key": kol_key,
                }
            )
        ),
        deterministic=True,
    )
    c.create_function(
        "event_attribution_basis",
        7,
        lambda *values: str(
            relevance_assessment(*values)["attribution_basis"]
        ),
        deterministic=True,
    )
    c.create_function(
        "event_matched_alias",
        7,
        lambda *values: str(
            relevance_assessment(*values)["matched_alias"] or ""
        ),
        deterministic=True,
    )
    c.create_function(
        "event_ai_cache_current",
        10,
        _event_ai_cache_current_sql,
    )
    c.execute("PRAGMA foreign_keys=ON")
    # Reissuing ``journal_mode=WAL`` can itself contend with a live writer.
    # Current databases therefore take a read-only mode check; the transition
    # is only needed once for a new or legacy non-WAL database.
    journal_mode = str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode != "wal":
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        if immediate:
            # Disable the driver's separate opaque busy wait so the configured
            # retry budget applies to BEGIN as a whole, not to every attempt.
            c.execute("PRAGMA busy_timeout=0")
            _begin_immediate(c)
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ─── Identity helpers ──────────────────────────────────

def canonical_url(url: str) -> str:
    """Unwrap aggregator redirects and drop per-fetch tracking params."""
    if not url:
        return ""
    u = url.strip()

    # Bing News hands back apiclick.aspx?...&url=<percent-encoded real url>
    for _ in range(3):
        try:
            parsed = urllib.parse.urlsplit(u)
        except ValueError:
            return u
        qs = urllib.parse.parse_qs(parsed.query)
        inner = qs.get("url") or qs.get("u")
        if inner and inner[0].startswith(("http://", "https://")):
            u = inner[0]
            continue
        break

    try:
        parsed = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            urllib.parse.urlencode(kept),
            "",
        )
    )


def _collapse_self_repeat(s: str) -> str:
    """Some feeds emit "TITLE" + a truncated repeat of it; keep the first copy.

    The repeat is shorter than the original, so the split point always sits in
    the second half of the string.
    """
    n = len(s)
    for size in range((n + 1) // 2, n):
        rest = s[size:]
        if len(rest) >= 6 and s[:size].startswith(rest):
            return s[:size]
    return s


# CJK typography treats these as separators/brackets, but Unicode classes some
# of them as letters, so they survive punctuation stripping and split otherwise
# identical headlines into different fingerprints.
_CJK_SEPARATORS = "丨｜·・‧∣│―─—–「」『』【】〈〉《》"
_SEP_TABLE = {ord(ch): None for ch in _CJK_SEPARATORS}


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = t.translate(_SEP_TABLE)
    # Strip punctuation/symbols; keep letters, digits and CJK.
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] in ("L", "N"))
    return _collapse_self_repeat(t)


_ELLIPSIS_RE = re.compile(r"(?:\s*(?:\.{3}|…|\u2026))+\s*$")


def clean_display_title(title: str) -> str:
    """Trim the duplicated-then-truncated titles some feeds emit."""
    t = re.sub(r"\s+", " ", (title or "").strip())
    stripped = _ELLIPSIS_RE.sub("", t)
    n = len(stripped)
    for size in range((n + 1) // 2, n):
        rest = stripped[size:]
        if len(rest) >= 6 and stripped[:size].startswith(rest):
            return stripped[:size].strip()
    return t


def dedup_key(title: str, url: str = "") -> str:
    base = normalize_title(title)
    if not base:
        base = canonical_url(url)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


# Aggregators emit both the truncated and the full form of a headline. They
# share a leading run of characters, so bucket on that and confirm with a
# prefix test — cheap, and indexable.
PREFIX_BUCKET_LEN = 12
PREFIX_MIN_MATCH = 16


def prefix_key(title: str) -> str | None:
    """Bucket key for near-duplicate lookup; None when the title is too short."""
    base = normalize_title(title)
    if len(base) < PREFIX_BUCKET_LEN:
        return None
    return hashlib.sha1(base[:PREFIX_BUCKET_LEN].encode("utf-8")).hexdigest()[:16]


def is_prefix_dupe(a: str, b: str) -> bool:
    """True when one normalized headline is a truncation of the other."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= PREFIX_MIN_MATCH and long_.startswith(short)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# ─── Schema ────────────────────────────────────────────

def _columns(c, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _required_text(value: Any, field: str, *, maximum: int = 200) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValueError(f"{field} must be non-empty and at most {maximum} characters")
    return text


def _optional_text(value: Any, field: str, *, maximum: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > maximum or "\x00" in text:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


def _finite_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    optional: bool = False,
) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return number


def _bounded_integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = 1_000_000_000,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if isinstance(value, float) and value != number:
        raise ValueError(f"{field} must be an integer")
    if number < minimum or number > maximum:
        raise ValueError(f"{field} is outside the supported range")
    return number


def _safe_json(value: Any, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be JSON-safe") from exc


def _reaction_timestamps_json(value: Any) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("data_timestamps must be an object")
    allowed = {"start", "end", "benchmark_start", "benchmark_end"}
    if not set(value).issubset(allowed):
        raise ValueError("data_timestamps contains unsupported fields")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            clean[key] = None
        elif isinstance(item, bool):
            raise ValueError("data_timestamps values must be timestamps")
        elif isinstance(item, (int, float)):
            clean[key] = _finite_number(
                item, f"data_timestamps.{key}", minimum=0
            )
        elif isinstance(item, str):
            text = _required_text(
                item, f"data_timestamps.{key}", maximum=64
            )
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"data_timestamps.{key} must be a timestamp"
                ) from exc
            if parsed.tzinfo is None:
                raise ValueError(
                    f"data_timestamps.{key} must include a timezone"
                )
            clean[key] = (
                parsed.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )
        else:
            raise ValueError("data_timestamps values must be timestamps")
    return _safe_json(clean, "data_timestamps")


def _observed_at(value: Any) -> str:
    if value is None:
        return _now_iso()
    text = _required_text(value, "observed_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _optional_utc_at(value: Any, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = _required_text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _market_reason_code(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    candidate = str(value).strip().lower().split(":", 1)[0]
    return candidate if candidate in _MARKET_REASON_CODES else "unknown"


def _create_market_reactions_table(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS market_reactions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_type TEXT NOT NULL,
          source_id TEXT NOT NULL,
          asset_key TEXT NOT NULL,
          window TEXT NOT NULL,
          benchmark_asset_key TEXT,
          asset_return REAL,
          benchmark_return REAL,
          abnormal_return REAL,
          expected_direction TEXT
            CHECK(expected_direction IS NULL OR expected_direction IN ('positive', 'negative')),
          observed_direction TEXT
            CHECK(observed_direction IS NULL OR observed_direction IN ('positive', 'negative', 'neutral')),
          direction_confirmed INTEGER
            CHECK(direction_confirmed IS NULL OR direction_confirmed IN (0, 1)),
          status TEXT NOT NULL
            CHECK(status IN ('pending', 'preliminary', 'complete', 'unavailable')),
          sample_count INTEGER NOT NULL DEFAULT 0 CHECK(sample_count >= 0),
          data_timestamps_json TEXT NOT NULL DEFAULT '{}',
          method_version TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          provider TEXT,
          provider_symbol TEXT,
          proxy_for TEXT,
          asset_status TEXT
            CHECK(asset_status IS NULL OR asset_status IN ('available', 'unavailable', 'unsupported')),
          benchmark_status TEXT
            CHECK(benchmark_status IS NULL OR benchmark_status IN ('available', 'unavailable', 'unsupported')),
          reason_code TEXT CHECK(
            reason_code IS NULL OR reason_code IN (
              'asset_unavailable', 'bad_payload', 'baseline_unavailable',
              'benchmark_unavailable', 'follow_up_unavailable',
              'insufficient_follow_up', 'invalid_event_time',
              'invalid_interval', 'invalid_range', 'invalid_timeout',
              'no_common_trading_dates', 'provider_error', 'request_failed',
              'same_proxy_as_benchmark', 'unknown', 'unsupported_asset',
              'unsupported_benchmark', 'window_not_due'
            )
          ),
          next_due_at TEXT,
          UNIQUE(source_type, source_id, asset_key, window, method_version)
        )
        """
    )


def _migrate_market_reactions_in(c: sqlite3.Connection) -> None:
    """Atomically add pending/diagnostic support to an existing reaction table."""
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_reactions'"
    ).fetchone()
    if row is None:
        _create_market_reactions_table(c)
        return
    table_sql = str(row["sql"] or "").lower()
    if "'pending'" not in table_sql:
        legacy = "market_reactions_legacy_p0"
        c.execute(f"ALTER TABLE market_reactions RENAME TO {legacy}")
        _create_market_reactions_table(c)
        legacy_columns = _columns(c, legacy)
        target_columns = [
            "id",
            "source_type",
            "source_id",
            "asset_key",
            "window",
            "benchmark_asset_key",
            "asset_return",
            "benchmark_return",
            "abnormal_return",
            "expected_direction",
            "observed_direction",
            "direction_confirmed",
            "status",
            "sample_count",
            "data_timestamps_json",
            "method_version",
            "observed_at",
            "provider",
            "provider_symbol",
            "proxy_for",
            "asset_status",
            "benchmark_status",
            "reason_code",
            "next_due_at",
        ]
        select_columns = [
            column if column in legacy_columns else "NULL" for column in target_columns
        ]
        c.execute(
            f"INSERT INTO market_reactions ({', '.join(target_columns)}) "
            f"SELECT {', '.join(select_columns)} FROM {legacy}"
        )
        c.execute(f"DROP TABLE {legacy}")
        return
    columns = _columns(c, "market_reactions")
    additions = {
        "provider": "TEXT",
        "provider_symbol": "TEXT",
        "proxy_for": "TEXT",
        "asset_status": (
            "TEXT CHECK(asset_status IS NULL OR "
            "asset_status IN ('available', 'unavailable', 'unsupported'))"
        ),
        "benchmark_status": (
            "TEXT CHECK(benchmark_status IS NULL OR "
            "benchmark_status IN ('available', 'unavailable', 'unsupported'))"
        ),
        "reason_code": "TEXT",
        "next_due_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            c.execute(
                f"ALTER TABLE market_reactions ADD COLUMN {column} {definition}"
            )


_MAX_FUTURE_SKEW_SECONDS = 5 * 60
_PUBLICATION_STATUSES = {"verified", "unknown", "future"}


def _parse_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _reliable_published_at(value: Any) -> str | None:
    parsed = _parse_utc_datetime(value)
    if parsed is None:
        return None
    return parsed.replace(microsecond=0).isoformat()


def _publication_metadata(
    value: Any,
    *,
    observed_at: Any = None,
) -> tuple[str | None, str, int | None]:
    parsed = _parse_utc_datetime(value)
    if parsed is None:
        return None, "unknown", None
    observed = (
        _parse_utc_datetime(observed_at)
        if observed_at is not None
        else datetime.now(timezone.utc)
    )
    if observed is None:
        raise ValueError("observed_at must include a timezone")
    normalized = parsed.replace(microsecond=0)
    status = (
        "future"
        if normalized
        > observed + timedelta(seconds=_MAX_FUTURE_SKEW_SECONDS)
        else "verified"
    )
    return normalized.isoformat(), status, int(normalized.timestamp())


def _preferred_publication(
    current: tuple[Any, Any, Any],
    candidate: tuple[Any, Any, Any],
) -> tuple[str | None, str, int | None]:
    current_value, current_status, current_epoch = current
    candidate_value, candidate_status, candidate_epoch = candidate
    current_status = (
        current_status if current_status in _PUBLICATION_STATUSES else "unknown"
    )
    candidate_status = (
        candidate_status
        if candidate_status in _PUBLICATION_STATUSES
        else "unknown"
    )
    if current_status == "verified":
        return current_value, current_status, current_epoch
    if candidate_status == "verified":
        return candidate_value, candidate_status, candidate_epoch
    if current_status == "future":
        return current_value, current_status, current_epoch
    if candidate_status == "future":
        return candidate_value, candidate_status, candidate_epoch
    return None, "unknown", None


def _publication_status(value: Any, *, now: datetime | None = None) -> str:
    return _publication_metadata(value, observed_at=now)[1]


def _backfill_sightings_in(c) -> int:
    """Create one recoverable sighting for every existing canonical event."""
    rows = c.execute(
        """
        SELECT id, title, snippet, tickers, kol_key, kol_name, kol_name_cn, source,
               url, canonical_url,
               published_at, published_at_status, published_at_epoch,
               fetched_at, last_seen_at, source_count
        FROM events
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        first_seen = row["fetched_at"] or row["last_seen_at"] or _now_iso()
        last_seen = row["last_seen_at"] or first_seen
        source_url = row["canonical_url"] or canonical_url(row["url"]) or row["url"]
        cur = c.execute(
            """
            INSERT INTO event_sightings (
              event_id, title, snippet, tickers, kol_key, kol_name, kol_name_cn,
              source, source_url,
              published_at, published_at_status, published_at_epoch,
              first_seen_at, last_seen_at, source_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, kol_key, source, source_url) DO UPDATE SET
              kol_name=CASE
                WHEN event_sightings.kol_name='' THEN excluded.kol_name
                ELSE event_sightings.kol_name
              END,
              kol_name_cn=CASE
                WHEN event_sightings.kol_name_cn='' THEN excluded.kol_name_cn
                ELSE event_sightings.kol_name_cn
              END,
              published_at=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN event_sightings.published_at
                WHEN excluded.published_at_status='verified'
                  THEN excluded.published_at
                WHEN event_sightings.published_at_status='future'
                  THEN event_sightings.published_at
                ELSE excluded.published_at
              END,
              published_at_status=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN 'verified'
                WHEN excluded.published_at_status='verified'
                  THEN 'verified'
                WHEN event_sightings.published_at_status='future'
                  THEN 'future'
                ELSE excluded.published_at_status
              END,
              published_at_epoch=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN event_sightings.published_at_epoch
                WHEN excluded.published_at_status='verified'
                  THEN excluded.published_at_epoch
                WHEN event_sightings.published_at_status='future'
                  THEN event_sightings.published_at_epoch
                ELSE excluded.published_at_epoch
              END,
              first_seen_at=MIN(
                event_sightings.first_seen_at, excluded.first_seen_at
              ),
              last_seen_at=MAX(
                event_sightings.last_seen_at, excluded.last_seen_at
              )
            """,
            (
                row["id"],
                row["title"] or "",
                row["snippet"] or "",
                row["tickers"] or "",
                row["kol_key"] or "unknown",
                row["kol_name"] or "",
                row["kol_name_cn"] or "",
                row["source"] or "",
                source_url or "",
                _reliable_published_at(row["published_at"]),
                row["published_at_status"] or "unknown",
                row["published_at_epoch"],
                first_seen,
                last_seen,
                max(1, row["source_count"] or 1),
            ),
        )
        inserted += max(cur.rowcount, 0)
    return inserted


def backfill_sightings() -> int:
    """Idempotently backfill sightings from the legacy event attribution fields."""
    with conn(immediate=True) as c:
        _backfill_publication_metadata_in(c)
        _normalize_sighting_urls_in(c)
        return _backfill_sightings_in(c)


def _sync_event_source_count(c, event_id: int) -> None:
    c.execute(
        """
        UPDATE events
        SET source_count=MAX(
          1,
          (
            SELECT COUNT(DISTINCT NULLIF(TRIM(source_url), ''))
            FROM event_sightings
            WHERE event_id=?
          )
        )
        WHERE id=?
        """,
        (event_id, event_id),
    )


def _recount_event_source_counts_in(c) -> None:
    c.execute(
        """
        UPDATE events
        SET source_count=MAX(
          1,
          (
            SELECT COUNT(DISTINCT NULLIF(TRIM(source_url), ''))
            FROM event_sightings
            WHERE event_sightings.event_id=events.id
          )
        )
        """
    )


def _sighting_ticker_set(value: Any) -> set[str]:
    raw_values = value.split(",") if isinstance(value, str) else value or ()
    return {
        re.sub(r"[^A-Z0-9.^_-]", "", str(item).strip().upper())[:20]
        for item in raw_values
        if str(item).strip()
    } - {""}


def _merge_sighting_content(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
    *,
    source: str,
    source_url: str,
) -> tuple[str, str, str]:
    """Merge observations only within one exact source identity."""
    incoming_title = clean_display_title(
        str(incoming.get("title") or "").strip()
    )
    incoming_snippet = str(incoming.get("snippet") or "").strip()
    incoming_tickers = _sighting_ticker_set(incoming.get("tickers"))
    if existing is None:
        return (
            incoming_title,
            incoming_snippet,
            ",".join(sorted(incoming_tickers)),
        )

    existing_title = str(existing.get("title") or "").strip()
    existing_snippet = str(existing.get("snippet") or "").strip()
    existing_tickers = _sighting_ticker_set(existing.get("tickers"))

    def substantive(title: str, snippet: str) -> bool:
        return bool(title or snippet) and is_event_content_eligible(
            {
                "title": title,
                "snippet": snippet,
                "source": source,
                "source_url": source_url,
            }
        )

    existing_substantive = substantive(existing_title, existing_snippet)
    incoming_substantive = substantive(incoming_title, incoming_snippet)
    if existing_substantive and not incoming_substantive:
        selected_title, selected_snippet = existing_title, existing_snippet
        selected_tickers = existing_tickers
    elif incoming_substantive and not existing_substantive:
        selected_title, selected_snippet = incoming_title, incoming_snippet
        selected_tickers = incoming_tickers
    elif (
        normalize_title(existing_title) != normalize_title(incoming_title)
        and not is_prefix_dupe(existing_title, incoming_title)
    ):
        # An exact URL can be corrected or, occasionally, reused.  Unrelated
        # text is a replacement observation rather than an extension of the
        # old story; keeping old tickers here would contaminate the new AI
        # subject and asset links.
        selected_title, selected_snippet = incoming_title, incoming_snippet
        selected_tickers = incoming_tickers
    else:
        selected_title = (
            incoming_title
            if len(incoming_title) >= len(existing_title)
            else existing_title
        )
        selected_snippet = (
            incoming_snippet
            if len(incoming_snippet) >= len(existing_snippet)
            else existing_snippet
        )
        selected_tickers = existing_tickers | incoming_tickers
    return selected_title, selected_snippet, ",".join(sorted(selected_tickers))


def _upsert_sighting(
    c,
    event_id: int,
    item: dict[str, Any],
    seen_at: str,
    *,
    publication_observed_at: str | None = None,
) -> None:
    raw_url = (item.get("url") or "").strip()
    source_url = canonical_url(raw_url) or raw_url
    kol_key = item.get("kol_key") or "unknown"
    source = item.get("source") or ""
    existing_content = c.execute(
        "SELECT title, snippet, tickers FROM event_sightings "
        "WHERE event_id=? AND kol_key=? AND source=? AND source_url=?",
        (event_id, kol_key, source, source_url),
    ).fetchone()
    incoming_title, incoming_snippet, incoming_tickers = (
        _merge_sighting_content(
            dict(existing_content) if existing_content is not None else None,
            item,
            source=source,
            source_url=source_url,
        )
    )
    published_at, published_status, published_epoch = _publication_metadata(
        item.get("published_at"),
        observed_at=publication_observed_at or seen_at,
    )
    c.execute(
        """
        INSERT INTO event_sightings (
          event_id, title, snippet, tickers, kol_key, kol_name, kol_name_cn,
          source, source_url,
          published_at, published_at_status, published_at_epoch,
          first_seen_at, last_seen_at, source_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(event_id, kol_key, source, source_url) DO UPDATE SET
          title=excluded.title,
          snippet=excluded.snippet,
          tickers=excluded.tickers,
          kol_name=CASE
            WHEN excluded.kol_name<>'' THEN excluded.kol_name
            ELSE event_sightings.kol_name
          END,
          kol_name_cn=CASE
            WHEN excluded.kol_name_cn<>'' THEN excluded.kol_name_cn
            ELSE event_sightings.kol_name_cn
          END,
          published_at=CASE
            WHEN event_sightings.published_at_status='verified'
              THEN event_sightings.published_at
            WHEN excluded.published_at_status='verified'
              THEN excluded.published_at
            WHEN event_sightings.published_at_status='future'
              THEN event_sightings.published_at
            ELSE excluded.published_at
          END,
          published_at_status=CASE
            WHEN event_sightings.published_at_status='verified' THEN 'verified'
            WHEN excluded.published_at_status='verified' THEN 'verified'
            WHEN event_sightings.published_at_status='future' THEN 'future'
            ELSE excluded.published_at_status
          END,
          published_at_epoch=CASE
            WHEN event_sightings.published_at_status='verified'
              THEN event_sightings.published_at_epoch
            WHEN excluded.published_at_status='verified'
              THEN excluded.published_at_epoch
            WHEN event_sightings.published_at_status='future'
              THEN event_sightings.published_at_epoch
            ELSE excluded.published_at_epoch
          END,
          last_seen_at=excluded.last_seen_at,
          source_count=event_sightings.source_count+1
        """,
        (
            event_id,
            incoming_title,
            incoming_snippet,
            incoming_tickers,
            kol_key,
            item.get("kol_name") or "",
            item.get("kol_name_cn") or "",
            source,
            source_url,
            published_at,
            published_status,
            published_epoch,
            seen_at,
            seen_at,
        ),
    )
    _sync_event_source_count(c, event_id)


def _revoke_stale_event_enrichment_in(
    c: sqlite3.Connection,
    event_id: int,
    updated_at: str,
) -> bool:
    """Revoke a lease/cache when the preferred sighting changes AI identity."""
    enrichment = c.execute(
        "SELECT input_hash FROM event_enrichments WHERE event_id=?",
        (int(event_id),),
    ).fetchone()
    if enrichment is None:
        return False
    subject = c.execute(
        f"""
        SELECT m.title, m.snippet, m.source, e.url, e.canonical_url,
               m.source_url, m.kol_key, m.kol_name, m.kol_name_cn,
               m.tickers
        FROM events e
        JOIN event_sightings m ON m.id=(
          SELECT s.id FROM event_sightings s
          WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
          ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1
        )
        WHERE e.id=?
        """,
        (int(event_id),),
    ).fetchone()
    if subject is None:
        # The last eligible sighting may have been corrected into irrelevant
        # content while an LLM worker still owns a lease. Revoke that lease so
        # its late result cannot be persisted or consume follow-up work.
        c.execute(
            """
            UPDATE event_enrichments
            SET status='pending', updated_at=?, next_attempt_at=NULL,
                error_code='', claim_token=''
            WHERE event_id=?
            """,
            (updated_at, int(event_id)),
        )
        return True
    try:
        from kol_dashboard import llm_enrichment as enrichment_domain
    except ModuleNotFoundError:  # Flat production bundle.
        import llm_enrichment as enrichment_domain
    _, current_hash = enrichment_domain.build_event_input(dict(subject))
    if str(enrichment["input_hash"] or "") == current_hash:
        return False
    c.execute(
        """
        UPDATE event_enrichments
        SET status='pending', updated_at=?, next_attempt_at=NULL,
            error_code='', claim_token=''
        WHERE event_id=?
        """,
        (updated_at, int(event_id)),
    )
    return True


def _move_sightings(c, keep_id: int, victim_id: int) -> None:
    """Move a duplicate event's sightings without losing per-KOL attribution."""
    rows = c.execute(
        """
        SELECT title, snippet, tickers, kol_key, kol_name, kol_name_cn, source,
               source_url, published_at, published_at_status, published_at_epoch,
               first_seen_at, last_seen_at, source_count
        FROM event_sightings WHERE event_id=?
        """,
        (victim_id,),
    ).fetchall()
    for row in rows:
        existing_content = c.execute(
            "SELECT title, snippet, tickers FROM event_sightings "
            "WHERE event_id=? AND kol_key=? AND source=? AND source_url=?",
            (
                keep_id,
                row["kol_key"],
                row["source"],
                row["source_url"],
            ),
        ).fetchone()
        merged_title, merged_snippet, merged_tickers = _merge_sighting_content(
            dict(existing_content) if existing_content is not None else None,
            dict(row),
            source=str(row["source"] or ""),
            source_url=str(row["source_url"] or ""),
        )
        c.execute(
            """
            INSERT INTO event_sightings (
              event_id, title, snippet, tickers, kol_key, kol_name, kol_name_cn,
              source, source_url,
              published_at, published_at_status, published_at_epoch,
              first_seen_at, last_seen_at, source_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, kol_key, source, source_url) DO UPDATE SET
              title=excluded.title,
              snippet=excluded.snippet,
              tickers=excluded.tickers,
              published_at=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN event_sightings.published_at
                WHEN excluded.published_at_status='verified'
                  THEN excluded.published_at
                WHEN event_sightings.published_at_status='future'
                  THEN event_sightings.published_at
                ELSE excluded.published_at
              END,
              published_at_status=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN 'verified'
                WHEN excluded.published_at_status='verified'
                  THEN 'verified'
                WHEN event_sightings.published_at_status='future'
                  THEN 'future'
                ELSE excluded.published_at_status
              END,
              published_at_epoch=CASE
                WHEN event_sightings.published_at_status='verified'
                  THEN event_sightings.published_at_epoch
                WHEN excluded.published_at_status='verified'
                  THEN excluded.published_at_epoch
                WHEN event_sightings.published_at_status='future'
                  THEN event_sightings.published_at_epoch
                ELSE excluded.published_at_epoch
              END,
              first_seen_at=MIN(
                event_sightings.first_seen_at, excluded.first_seen_at
              ),
              last_seen_at=MAX(
                event_sightings.last_seen_at, excluded.last_seen_at
              ),
              source_count=event_sightings.source_count+excluded.source_count
            """,
            (
                keep_id,
                merged_title,
                merged_snippet,
                merged_tickers,
                row["kol_key"],
                row["kol_name"],
                row["kol_name_cn"],
                row["source"],
                row["source_url"],
                _reliable_published_at(row["published_at"]),
                row["published_at_status"] or "unknown",
                row["published_at_epoch"],
                row["first_seen_at"],
                row["last_seen_at"],
                row["source_count"],
            ),
        )


def _backfill_publication_metadata_in(c) -> None:
    for row in c.execute(
        "SELECT id, published_at, published_at_status, published_at_epoch, "
        "fetched_at FROM events"
    ).fetchall():
        status = row["published_at_status"]
        parsed = _parse_utc_datetime(row["published_at"])
        if status in _PUBLICATION_STATUSES:
            if status == "unknown" or parsed is None:
                value, status, epoch = None, "unknown", None
            else:
                value = parsed.replace(microsecond=0).isoformat()
                epoch = int(parsed.timestamp())
        else:
            value, status, epoch = _publication_metadata(
                row["published_at"],
                observed_at=row["fetched_at"],
            )
        c.execute(
            "UPDATE events SET published_at=?, published_at_status=?, "
            "published_at_epoch=? WHERE id=?",
            (value, status, epoch, row["id"]),
        )
    for row in c.execute(
        "SELECT id, published_at, published_at_status, published_at_epoch, "
        "first_seen_at FROM event_sightings"
    ).fetchall():
        status = row["published_at_status"]
        parsed = _parse_utc_datetime(row["published_at"])
        if status in _PUBLICATION_STATUSES:
            if status == "unknown" or parsed is None:
                value, status, epoch = None, "unknown", None
            else:
                value = parsed.replace(microsecond=0).isoformat()
                epoch = int(parsed.timestamp())
        else:
            value, status, epoch = _publication_metadata(
                row["published_at"],
                observed_at=row["first_seen_at"],
            )
        c.execute(
            "UPDATE event_sightings SET published_at=?, "
            "published_at_status=?, published_at_epoch=? WHERE id=?",
            (value, status, epoch, row["id"]),
        )


def _normalize_sighting_urls_in(c) -> None:
    rows = c.execute(
        """
        SELECT id, event_id, title, snippet, tickers, kol_key, source,
               source_url, published_at,
               published_at_status, published_at_epoch,
               first_seen_at, last_seen_at, source_count
        FROM event_sightings
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        source_url = canonical_url(row["source_url"]) or row["source_url"]
        if not source_url:
            continue
        existing = c.execute(
            """
            SELECT id, title, snippet, tickers, published_at,
                   published_at_status, published_at_epoch,
                   first_seen_at, last_seen_at, source_count
            FROM event_sightings
            WHERE event_id=? AND kol_key=? AND source=? AND source_url=?
              AND id<>?
            ORDER BY id
            LIMIT 1
            """,
            (
                row["event_id"],
                row["kol_key"],
                row["source"],
                source_url,
                row["id"],
            ),
        ).fetchone()
        if existing is None:
            if source_url != row["source_url"]:
                c.execute(
                    "UPDATE event_sightings SET source_url=? WHERE id=?",
                    (source_url, row["id"]),
                )
            continue
        publication = _preferred_publication(
            (
                existing["published_at"],
                existing["published_at_status"],
                existing["published_at_epoch"],
            ),
            (
                row["published_at"],
                row["published_at_status"],
                row["published_at_epoch"],
            ),
        )
        merged_title, merged_snippet, merged_tickers = _merge_sighting_content(
            dict(existing),
            dict(row),
            source=str(row["source"] or ""),
            source_url=str(source_url),
        )
        c.execute(
            """
            UPDATE event_sightings
            SET title=?, snippet=?,
                tickers=?,
                published_at=?, published_at_status=?, published_at_epoch=?,
                first_seen_at=MIN(first_seen_at, ?),
                last_seen_at=MAX(last_seen_at, ?),
                source_count=source_count+?
            WHERE id=?
            """,
            (
                merged_title,
                merged_snippet,
                merged_tickers,
                *publication,
                row["first_seen_at"],
                row["last_seen_at"],
                row["source_count"],
                existing["id"],
            ),
        )
        c.execute("DELETE FROM event_sightings WHERE id=?", (row["id"],))


def _schema_version_in(c: sqlite3.Connection) -> int:
    return int(c.execute("PRAGMA user_version").fetchone()[0])


def _ensure_supported_schema_version(version: int) -> None:
    if version > _DB_SCHEMA_VERSION:
        raise RuntimeError(
            "database schema is newer than this application "
            f"({version} > {_DB_SCHEMA_VERSION})"
        )


def init() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # The overwhelmingly common startup path performs one version read and no
    # write transaction.  This also avoids repeating the publication, sighting
    # and dedup full-table backfills while collectors are active.
    with conn() as c:
        version = _schema_version_in(c)
        _ensure_supported_schema_version(version)
        if version == _DB_SCHEMA_VERSION:
            return

    with conn(immediate=True) as c:
        # Another process may have completed the migration while this process
        # was backing off for the writer slot.
        version = _schema_version_in(c)
        _ensure_supported_schema_version(version)
        if version == _DB_SCHEMA_VERSION:
            return

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              url_hash TEXT UNIQUE NOT NULL,
              url TEXT NOT NULL,
              title TEXT NOT NULL,
              snippet TEXT,
              source TEXT,
              kol_key TEXT NOT NULL,
              kol_name TEXT NOT NULL,
              kol_name_cn TEXT NOT NULL,
              impact TEXT NOT NULL DEFAULT 'low',
              has_market_kw INTEGER NOT NULL DEFAULT 0,
              fetched_at TEXT NOT NULL,
              published_at TEXT,
              published_at_status TEXT NOT NULL DEFAULT 'unknown',
              published_at_epoch INTEGER
            )
            """
        )
        cols = _columns(c, "events")
        if "dedup_key" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN dedup_key TEXT")
        if "canonical_url" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN canonical_url TEXT")
        if "source_count" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN source_count INTEGER NOT NULL DEFAULT 1")
        if "last_seen_at" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN last_seen_at TEXT")
        if "tickers" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN tickers TEXT")
        if "prefix_key" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN prefix_key TEXT")
        if "published_at" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN published_at TEXT")
        if "published_at_status" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN published_at_status TEXT")
        if "published_at_epoch" not in cols:
            c.execute("ALTER TABLE events ADD COLUMN published_at_epoch INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prefix ON events(prefix_key)")

        c.execute("CREATE INDEX IF NOT EXISTS idx_fetched ON events(fetched_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kol ON events(kol_key, fetched_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_impact ON events(impact, fetched_at DESC)")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_publication "
            "ON events(published_at_status, published_at_epoch DESC)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS event_sightings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              title TEXT NOT NULL DEFAULT '',
              snippet TEXT NOT NULL DEFAULT '',
              tickers TEXT NOT NULL DEFAULT '',
              kol_key TEXT NOT NULL,
              kol_name TEXT NOT NULL DEFAULT '',
              kol_name_cn TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              published_at TEXT,
              published_at_status TEXT NOT NULL DEFAULT 'unknown',
              published_at_epoch INTEGER,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              source_count INTEGER NOT NULL DEFAULT 1 CHECK(source_count > 0),
              UNIQUE(event_id, kol_key, source, source_url)
            )
            """
        )
        sighting_columns = _columns(c, "event_sightings")
        title_column_added = False
        snippet_column_added = False
        tickers_column_added = False
        if "title" not in sighting_columns:
            # Existing multi-source rows cannot safely inherit the merged
            # canonical headline. Keep them blank until that exact source is
            # observed again; precision is preferable to fabricated lineage.
            c.execute(
                "ALTER TABLE event_sightings "
                "ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
            title_column_added = True
        if "snippet" not in sighting_columns:
            c.execute(
                "ALTER TABLE event_sightings "
                "ADD COLUMN snippet TEXT NOT NULL DEFAULT ''"
            )
            snippet_column_added = True
        if "tickers" not in sighting_columns:
            c.execute(
                "ALTER TABLE event_sightings "
                "ADD COLUMN tickers TEXT NOT NULL DEFAULT ''"
            )
            tickers_column_added = True
        if "published_at_status" not in sighting_columns:
            c.execute(
                "ALTER TABLE event_sightings ADD COLUMN published_at_status TEXT"
            )
        if "published_at_epoch" not in sighting_columns:
            c.execute(
                "ALTER TABLE event_sightings ADD COLUMN published_at_epoch INTEGER"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sighting_event "
            "ON event_sightings(event_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sighting_kol "
            "ON event_sightings(kol_key, last_seen_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sighting_publication "
            "ON event_sightings("
            "kol_key, published_at_status, published_at_epoch DESC)"
        )

        # LLM output is an auxiliary, replaceable cache.  The canonical event,
        # original evidence and deterministic relation graph remain untouched.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS event_enrichments (
              event_id INTEGER PRIMARY KEY
                REFERENCES events(id) ON DELETE CASCADE,
              input_hash TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              status TEXT NOT NULL
                CHECK(status IN ('pending', 'processing', 'ready', 'retry', 'failed')),
              headline_zh TEXT NOT NULL DEFAULT '',
              summary_zh TEXT NOT NULL DEFAULT '',
              why_it_matters_zh TEXT NOT NULL DEFAULT '',
              impact_level TEXT NOT NULL DEFAULT 'unknown'
                CHECK(impact_level IN ('high', 'medium', 'low', 'none', 'unknown')),
              impact_path_json TEXT NOT NULL DEFAULT '[]',
              tags_json TEXT NOT NULL DEFAULT '[]',
              assets_json TEXT NOT NULL DEFAULT '[]',
              cluster_key TEXT NOT NULL DEFAULT '',
              language TEXT NOT NULL DEFAULT 'unknown',
              confidence REAL
                CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
              evidence_basis TEXT NOT NULL DEFAULT 'title',
              model TEXT NOT NULL DEFAULT '',
              generated_at TEXT,
              updated_at TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
              next_attempt_at TEXT,
              error_code TEXT NOT NULL DEFAULT '',
              claim_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        enrichment_columns = _columns(c, "event_enrichments")
        if "claim_token" not in enrichment_columns:
            c.execute(
                "ALTER TABLE event_enrichments ADD COLUMN claim_token TEXT "
                "NOT NULL DEFAULT ''"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_status "
            "ON event_enrichments(status, next_attempt_at, updated_at)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_cluster "
            "ON event_enrichments(cluster_key) WHERE status='ready'"
        )

        # Risk-radar events live inside immutable macro snapshot JSON rather
        # than the canonical KOL events table, so their replaceable AI cache
        # uses a stable opaque event key and deliberately has no foreign key.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS macro_event_enrichments (
              event_key TEXT PRIMARY KEY,
              input_hash TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              status TEXT NOT NULL
                CHECK(status IN ('pending', 'processing', 'ready', 'retry', 'failed')),
              headline_zh TEXT NOT NULL DEFAULT '',
              summary_zh TEXT NOT NULL DEFAULT '',
              why_it_matters_zh TEXT NOT NULL DEFAULT '',
              impact_level TEXT NOT NULL DEFAULT 'unknown'
                CHECK(impact_level IN ('high', 'medium', 'low', 'none', 'unknown')),
              impact_path_json TEXT NOT NULL DEFAULT '[]',
              tags_json TEXT NOT NULL DEFAULT '[]',
              assets_json TEXT NOT NULL DEFAULT '[]',
              cluster_key TEXT NOT NULL DEFAULT '',
              language TEXT NOT NULL DEFAULT 'unknown',
              confidence REAL
                CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
              evidence_basis TEXT NOT NULL DEFAULT 'title_only',
              model TEXT NOT NULL DEFAULT '',
              generated_at TEXT,
              updated_at TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
              next_attempt_at TEXT,
              error_code TEXT NOT NULL DEFAULT '',
              claim_token TEXT NOT NULL DEFAULT ''
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_macro_event_enrichment_status "
            "ON macro_event_enrichments(status, next_attempt_at, updated_at)"
        )

        # Provider-call telemetry deliberately stores only counters and
        # bounded categories.  Prompts, responses, credentials, claim tokens
        # and account data never enter this ledger.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_call_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT NOT NULL CHECK(provider='deepseek'),
              subject_type TEXT NOT NULL
                CHECK(subject_type IN ('event', 'macro_event')),
              subject_key TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              model TEXT NOT NULL,
              attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
              outcome TEXT NOT NULL
                CHECK(outcome IN (
                  'started', 'ready', 'retry', 'failed',
                  'superseded', 'cancelled', 'abandoned'
                )),
              error_code TEXT NOT NULL DEFAULT '',
              http_status INTEGER
                CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
              started_at TEXT NOT NULL,
              completed_at TEXT,
              latency_ms INTEGER
                CHECK(latency_ms IS NULL OR latency_ms BETWEEN 0 AND 3600000),
              prompt_tokens INTEGER
                CHECK(prompt_tokens IS NULL OR prompt_tokens BETWEEN 0 AND 2000000),
              prompt_cache_hit_tokens INTEGER
                CHECK(prompt_cache_hit_tokens IS NULL OR prompt_cache_hit_tokens BETWEEN 0 AND 2000000),
              prompt_cache_miss_tokens INTEGER
                CHECK(prompt_cache_miss_tokens IS NULL OR prompt_cache_miss_tokens BETWEEN 0 AND 2000000),
              completion_tokens INTEGER
                CHECK(completion_tokens IS NULL OR completion_tokens BETWEEN 0 AND 1000000),
              reasoning_tokens INTEGER
                CHECK(reasoning_tokens IS NULL OR reasoning_tokens BETWEEN 0 AND 1000000),
              total_tokens INTEGER
                CHECK(total_tokens IS NULL OR total_tokens BETWEEN 0 AND 3000000)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_time "
            "ON llm_call_attempts(started_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_subject "
            "ON llm_call_attempts(subject_type, subject_key, started_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_model_outcome "
            "ON llm_call_attempts(model, outcome, started_at DESC)"
        )

        # Authenticated users may ask the background worker to prioritize one
        # current public subject.  This queue stores only a bounded content
        # identity and lifecycle timestamps: never session/account data,
        # prompts, source bodies, provider responses, or credentials.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_enrichment_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subject_type TEXT NOT NULL
                CHECK(subject_type IN ('event', 'macro_event')),
              subject_key TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              model TEXT NOT NULL,
              status TEXT NOT NULL
                CHECK(status IN ('pending', 'completed', 'superseded')),
              accepted_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_request_pending_identity "
            "ON ai_enrichment_requests("
            "subject_type, subject_key, input_hash, prompt_version"
            ") WHERE status='pending'"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_request_queue "
            "ON ai_enrichment_requests(status, id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_request_accepted "
            "ON ai_enrichment_requests(accepted_at DESC)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS relations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_type TEXT NOT NULL,
              source_id TEXT NOT NULL,
              topic_key TEXT NOT NULL,
              asset_key TEXT NOT NULL,
              relation_type TEXT NOT NULL,
              direction TEXT NOT NULL,
              strength REAL NOT NULL,
              confidence REAL NOT NULL,
              horizon TEXT NOT NULL,
              method TEXT NOT NULL,
              rationale TEXT NOT NULL DEFAULT '',
              evidence_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              UNIQUE(
                source_type, source_id, topic_key, asset_key, relation_type, method
              )
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_relation_source "
            "ON relations(source_type, source_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_relation_asset "
            "ON relations(asset_key, created_at DESC)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_relation_topic "
            "ON relations(topic_key, created_at DESC)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS market_prices (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              asset_key TEXT NOT NULL,
              provider TEXT NOT NULL,
              provider_symbol TEXT,
              timestamp INTEGER NOT NULL CHECK(timestamp >= 0),
              close REAL NOT NULL CHECK(close > 0),
              volume REAL CHECK(volume IS NULL OR volume >= 0),
              currency TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              observed_at TEXT NOT NULL,
              UNIQUE(asset_key, provider, timestamp)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_price_lookup "
            "ON market_prices(asset_key, provider, timestamp)"
        )
        _migrate_market_reactions_in(c)
        reaction_columns = _columns(c, "market_reactions")
        if "expected_direction" not in reaction_columns:
            c.execute(
                "ALTER TABLE market_reactions ADD COLUMN expected_direction TEXT "
                "CHECK(expected_direction IS NULL OR "
                "expected_direction IN ('positive', 'negative'))"
            )
        if "observed_direction" not in reaction_columns:
            c.execute(
                "ALTER TABLE market_reactions ADD COLUMN observed_direction TEXT "
                "CHECK(observed_direction IS NULL OR "
                "observed_direction IN ('positive', 'negative', 'neutral'))"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_reaction_source "
            "ON market_reactions(source_type, source_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_reaction_asset "
            "ON market_reactions(asset_key, observed_at DESC)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_hash TEXT NOT NULL UNIQUE,
              schema_version INTEGER NOT NULL,
              source_as_of TEXT,
              position_count INTEGER NOT NULL CHECK(position_count >= 0),
              created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot_created "
            "ON portfolio_snapshots(created_at DESC, id DESC)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_positions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              snapshot_id INTEGER NOT NULL
                REFERENCES portfolio_snapshots(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
              account TEXT NOT NULL,
              asset_key TEXT NOT NULL,
              symbol TEXT NOT NULL,
              name TEXT NOT NULL,
              quantity REAL NOT NULL CHECK(quantity > 0),
              avg_cost REAL CHECK(avg_cost IS NULL OR avg_cost >= 0),
              currency TEXT NOT NULL,
              asset_class TEXT NOT NULL,
              is_leveraged INTEGER NOT NULL CHECK(is_leveraged IN (0, 1)),
              as_of TEXT,
              UNIQUE(snapshot_id, ordinal)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_position_asset "
            "ON portfolio_positions(asset_key, snapshot_id)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS macro_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              composite_score INTEGER,
              composite_level TEXT,
              payload TEXT NOT NULL
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_macro_created ON macro_snapshots(created_at DESC)"
        )

        # Public decision output is expensive to aggregate but contains no
        # account data.  Persist last-good, versioned snapshots so public and
        # private requests can share the same evidence baseline without
        # recomputing the entire graph on every page view.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              schema_version INTEGER NOT NULL,
              engine_version TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              source_as_of TEXT,
              generated_at TEXT NOT NULL,
              summary_json TEXT NOT NULL,
              full_json TEXT NOT NULL
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_decision_snapshot_current "
            "ON decision_snapshots(schema_version, engine_version, id DESC)"
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )

        _backfill_publication_metadata_in(c)
        _normalize_sighting_urls_in(c)
        _backfill_sightings_in(c)
        # Only a one-source event has an unambiguous legacy content owner.
        # This runs after URL normalization so tracking-URL duplicates that
        # collapse to one row can be recovered. Multi-source events remain
        # blank/fail-closed until each exact source is recollected.
        single_source = (
            "event_id IN (SELECT event_id FROM event_sightings "
            "GROUP BY event_id HAVING COUNT(*)=1)"
        )
        if title_column_added:
            c.execute(
                "UPDATE event_sightings SET title=COALESCE((SELECT e.title "
                "FROM events e WHERE e.id=event_sightings.event_id), '') "
                f"WHERE {single_source}"
            )
        if snippet_column_added:
            c.execute(
                "UPDATE event_sightings SET snippet=COALESCE((SELECT e.snippet "
                "FROM events e WHERE e.id=event_sightings.event_id), '') "
                f"WHERE {single_source}"
            )
        if tickers_column_added:
            c.execute(
                "UPDATE event_sightings SET tickers=COALESCE((SELECT e.tickers "
                "FROM events e WHERE e.id=event_sightings.event_id), '') "
                f"WHERE {single_source}"
            )
        backfill_dedup(connection=c)
        # The marker is deliberately the final statement in the same migration
        # transaction.  Any schema/backfill failure rolls it back as one unit.
        c.execute(f"PRAGMA user_version={_DB_SCHEMA_VERSION}")


def get_meta_in(c, key: str) -> str | None:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta_in(c, key: str, value: str) -> None:
    c.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# Bump when normalize_title / clean_display_title change, to force a re-key.
_DEDUP_ALGO_VERSION = "3"


def backfill_dedup(
    *, connection: sqlite3.Connection | None = None
) -> dict[str, int]:
    """Populate dedup_key/canonical_url, collapse duplicate rows, enforce uniqueness.

    Idempotent and available as an explicit repair operation. ``init`` passes
    its migration transaction so the first schema version marker is atomic.
    When the fingerprint algorithm changes, every row is re-keyed so
    newly-matching stories merge.
    """
    stats = {"rekeyed": 0, "merged": 0}
    transaction = (
        nullcontext(connection)
        if connection is not None
        else conn(immediate=True)
    )
    with transaction as c:
        rekey_all = get_meta_in(c, "dedup_algo_version") != _DEDUP_ALGO_VERSION
        # A re-key moves rows onto colliding keys, so uniqueness can't hold mid-flight.
        if rekey_all:
            c.execute("DROP INDEX IF EXISTS idx_dedup")
            rows = c.execute("SELECT id, title, url FROM events").fetchall()
        else:
            rows = c.execute(
                "SELECT id, title, url FROM events "
                "WHERE dedup_key IS NULL OR canonical_url IS NULL"
            ).fetchall()

        for r in rows:
            cleaned = clean_display_title(r["title"])
            c.execute(
                "UPDATE events SET dedup_key=?, prefix_key=?, canonical_url=?, title=?, "
                "last_seen_at=COALESCE(last_seen_at, fetched_at) WHERE id=?",
                (
                    dedup_key(cleaned, r["url"]),
                    prefix_key(cleaned),
                    canonical_url(r["url"]),
                    cleaned,
                    r["id"],
                ),
            )
        stats["rekeyed"] = len(rows)

        # Collapse duplicates: keep the earliest row, carry over sighting count.
        dupes = c.execute(
            "SELECT dedup_key, COUNT(*) n, MIN(id) keep_id, "
            "MAX(COALESCE(last_seen_at, fetched_at)) last_at "
            "FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY dedup_key HAVING n > 1"
        ).fetchall()
        for d in dupes:
            agg = c.execute(
                "SELECT COALESCE(SUM(source_count),0) s, "
                "MAX(CASE impact WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END) imp, "
                "MAX(has_market_kw) kw FROM events WHERE dedup_key=?",
                (d["dedup_key"],),
            ).fetchone()
            group_rows = c.execute(
                "SELECT id, published_at, published_at_status, "
                "published_at_epoch FROM events WHERE dedup_key=? "
                "ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END, id",
                (d["dedup_key"], d["keep_id"]),
            ).fetchall()
            publication: tuple[Any, Any, Any] = (None, "unknown", None)
            for row in group_rows:
                publication = _preferred_publication(
                    publication,
                    (
                        row["published_at"],
                        row["published_at_status"],
                        row["published_at_epoch"],
                    ),
                )
            c.execute(
                "UPDATE events SET source_count=?, last_seen_at=?, impact=?, "
                "has_market_kw=?, published_at=?, published_at_status=?, "
                "published_at_epoch=? WHERE id=?",
                (
                    agg["s"],
                    d["last_at"],
                    {2: "high", 1: "medium"}.get(agg["imp"], "low"),
                    agg["kw"],
                    *publication,
                    d["keep_id"],
                ),
            )
            for row in group_rows:
                if row["id"] != d["keep_id"]:
                    _move_sightings(c, d["keep_id"], row["id"])
            c.execute(
                "DELETE FROM events WHERE dedup_key=? AND id<>?",
                (d["dedup_key"], d["keep_id"]),
            )
        stats["merged"] = sum(d["n"] - 1 for d in dupes)
        stats["prefix_merged"] = _merge_prefix_dupes(c)

        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup ON events(dedup_key)")
        set_meta_in(c, "dedup_algo_version", _DEDUP_ALGO_VERSION)
        _recount_event_source_counts_in(c)
    return stats


def _merge_prefix_dupes(c) -> int:
    """Fold truncated headlines into their full-length twin, bucket by bucket."""
    merged = 0
    buckets = c.execute(
        "SELECT prefix_key FROM events WHERE prefix_key IS NOT NULL "
        "GROUP BY prefix_key HAVING COUNT(*) > 1"
    ).fetchall()

    for b in buckets:
        rows = c.execute(
            "SELECT id, title, source_count, impact, has_market_kw, "
            "published_at, published_at_status, published_at_epoch, "
            "COALESCE(last_seen_at, fetched_at) seen FROM events "
            "WHERE prefix_key=? ORDER BY length(title) DESC",
            (b["prefix_key"],),
        ).fetchall()

        consumed: set[int] = set()
        for i, keep in enumerate(rows):
            if keep["id"] in consumed:
                continue
            victims = [
                r
                for r in rows[i + 1 :]
                if r["id"] not in consumed and is_prefix_dupe(keep["title"], r["title"])
            ]
            if not victims:
                continue

            rank = {"low": 0, "medium": 1, "high": 2}
            group = [keep, *victims]
            publication = (None, "unknown", None)
            for row in group:
                publication = _preferred_publication(
                    publication,
                    (
                        row["published_at"],
                        row["published_at_status"],
                        row["published_at_epoch"],
                    ),
                )
            c.execute(
                "UPDATE events SET source_count=?, last_seen_at=?, impact=?, "
                "has_market_kw=?, published_at=?, published_at_status=?, "
                "published_at_epoch=? WHERE id=?",
                (
                    sum(r["source_count"] or 1 for r in group),
                    max(r["seen"] or "" for r in group),
                    max((r["impact"] for r in group), key=lambda x: rank.get(x, 0)),
                    max(r["has_market_kw"] or 0 for r in group),
                    *publication,
                    keep["id"],
                ),
            )
            for v in victims:
                _move_sightings(c, keep["id"], v["id"])
                c.execute("DELETE FROM events WHERE id=?", (v["id"],))
                consumed.add(v["id"])
            merged += len(victims)
    return merged


# ─── Writes ────────────────────────────────────────────

def insert_events(items: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Insert events, dedup by title fingerprint.

    ``events`` remains the canonical story table. Every observation is also
    recorded against its KOL/source in ``event_sightings``. Returns
    ``(inserted, merged)`` for compatibility with existing callers.
    """
    inserted = 0
    merged = 0
    now = _now_iso()
    with conn() as c:
        for it in items:
            url = (it.get("url") or "").strip()
            title = (it.get("title") or "").strip()
            if not url or not title:
                continue
            title = clean_display_title(title)
            key = dedup_key(title, url)
            pkey = prefix_key(title)
            canon = canonical_url(url)
            digest = url_hash(canon or url)
            rank = {"low": 0, "medium": 1, "high": 2}
            new_impact = it.get("impact", "low")
            publication = _publication_metadata(
                it.get("published_at"),
                observed_at=now,
            )
            tickers = it.get("tickers") or []
            if isinstance(tickers, str):
                tickers_text = tickers or None
            else:
                tickers_text = ",".join(str(t) for t in tickers) or None

            existing = c.execute(
                "SELECT id, impact, title, snippet, source, url, canonical_url, "
                "tickers, "
                "published_at, "
                "published_at_status, published_at_epoch, fetched_at "
                "FROM events WHERE dedup_key=?",
                (key,),
            ).fetchone()
            if not existing and pkey:
                # Same story, truncated differently by the aggregator.
                for cand in c.execute(
                    "SELECT id, impact, title, snippet, source, url, canonical_url, "
                    "tickers, "
                    "published_at, "
                    "published_at_status, published_at_epoch, fetched_at "
                    "FROM events WHERE prefix_key=?",
                    (pkey,),
                ).fetchall():
                    if is_prefix_dupe(cand["title"], title):
                        existing = cand
                        break

            if not existing:
                existing = c.execute(
                    "SELECT id, impact, title, snippet, source, url, canonical_url, "
                    "tickers, "
                    "published_at, "
                    "published_at_status, published_at_epoch, fetched_at "
                    "FROM events WHERE url_hash=?",
                    (digest,),
                ).fetchone()

            def merge_into(existing_row) -> None:
                # Keep the strongest impact and the most complete headline.
                best = (
                    new_impact
                    if rank.get(new_impact, 0) > rank.get(existing_row["impact"], 0)
                    else existing_row["impact"]
                )
                incoming_snippet = str(it.get("snippet") or "").strip()
                existing_snippet = str(existing_row["snippet"] or "").strip()
                existing_eligible = is_event_content_eligible(
                    {
                        "title": existing_row["title"],
                        "snippet": existing_snippet,
                        "source": existing_row["source"],
                        "canonical_url": existing_row["canonical_url"],
                        "url": existing_row["url"],
                    }
                )
                incoming_eligible = is_event_content_eligible(
                    {
                        "title": title,
                        "snippet": incoming_snippet,
                        "source": it.get("source"),
                        "canonical_url": canon,
                        "url": url,
                    }
                )
                richer_recovery = incoming_eligible and not existing_eligible
                richer_snippet = (
                    incoming_eligible
                    and len(incoming_snippet) >= len(existing_snippet) + 20
                )
                if existing_eligible and not incoming_eligible:
                    fuller = existing_row["title"]
                elif richer_recovery or len(title) > len(existing_row["title"]):
                    fuller = title
                else:
                    fuller = existing_row["title"]
                if richer_recovery:
                    # A substantive title with no snippet must clear a stale
                    # link-shell snippet so eligibility can recover on title.
                    best_snippet = incoming_snippet
                elif incoming_snippet and (
                    not existing_snippet or richer_snippet
                ):
                    best_snippet = incoming_snippet
                else:
                    best_snippet = existing_snippet
                candidate_publication = _publication_metadata(
                    it.get("published_at"),
                    observed_at=existing_row["fetched_at"],
                )
                selected_publication = _preferred_publication(
                    (
                        existing_row["published_at"],
                        existing_row["published_at_status"],
                        existing_row["published_at_epoch"],
                    ),
                    candidate_publication,
                )
                c.execute(
                    "UPDATE events SET last_seen_at=?, "
                    "impact=?, title=?, snippet=?, dedup_key=?, prefix_key=?, "
                    "has_market_kw=MAX(has_market_kw, ?), "
                    "tickers=COALESCE(tickers, ?), "
                    "published_at=?, published_at_status=?, "
                    "published_at_epoch=? WHERE id=?",
                    (
                        now,
                        best,
                        fuller,
                        best_snippet,
                        dedup_key(fuller, url),
                        prefix_key(fuller),
                        1 if it.get("has_market_kw") else 0,
                        tickers_text,
                        *selected_publication,
                        existing_row["id"],
                    ),
                )
                _upsert_sighting(
                    c,
                    existing_row["id"],
                    it,
                    now,
                    publication_observed_at=existing_row["fetched_at"],
                )
                _revoke_stale_event_enrichment_in(
                    c,
                    existing_row["id"],
                    now,
                )

            if existing:
                merge_into(existing)
                merged += 1
                continue

            try:
                cur = c.execute(
                    """
                    INSERT INTO events (
                      dedup_key, prefix_key, url_hash, url, canonical_url, title, snippet, source,
                      kol_key, kol_name, kol_name_cn,
                      impact, has_market_kw, tickers, source_count, fetched_at, last_seen_at,
                      published_at, published_at_status, published_at_epoch
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        key,
                        pkey,
                        digest,
                        url,
                        canon,
                        title,
                        it.get("snippet", ""),
                        it.get("source", ""),
                        it.get("kol_key", "unknown"),
                        it.get("kol_name", ""),
                        it.get("kol_name_cn", ""),
                        it.get("impact", "low"),
                        1 if it.get("has_market_kw") else 0,
                        tickers_text,
                        now,
                        now,
                        *publication,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = c.execute(
                    "SELECT id, impact, title, snippet, source, url, canonical_url, "
                    "tickers, "
                    "published_at, "
                    "published_at_status, published_at_epoch, fetched_at "
                    "FROM events "
                    "WHERE dedup_key=? OR url_hash=? ORDER BY id LIMIT 1",
                    (key, digest),
                ).fetchone()
                if not existing:
                    raise
                merge_into(existing)
                merged += 1
            else:
                event_id = cur.lastrowid
                if event_id is None:
                    raise sqlite3.IntegrityError("event insert returned no id")
                _upsert_sighting(c, event_id, it, now)
                inserted += 1
    return inserted, merged


# ─── Event intelligence cache ─────────────────────────

def query_enrichment_candidates(
    *,
    max_age_hours: int = 72,
    limit: int = 500,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return recent events and their cache metadata for the LLM worker.

    Selection intentionally includes ready rows: the worker owns the stable
    input hash and prompt version, so it can invalidate a cache when a merged
    event later acquires a fuller title or when the output contract changes.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    cutoff = int(current.timestamp()) - max(1, int(max_age_hours)) * 3600
    safe_limit = max(1, min(int(limit), 5_000))
    with conn() as c:
        rows = c.execute(
            f"""
            WITH recent_events AS MATERIALIZED (
              SELECT s.event_id,
                     MAX(
                       CASE WHEN s.published_at_status='verified'
                         THEN s.published_at_epoch
                         ELSE CAST(strftime('%s', s.first_seen_at) AS INTEGER)
                       END
                     ) AS activity_epoch
              FROM event_sightings s
              JOIN events e ON e.id=s.event_id
              WHERE {_event_intelligence_sql('s')}
                AND (
                  (s.published_at_status='verified'
                    AND s.published_at_epoch >= ?)
                  OR (
                    s.published_at_status IN ('unknown', 'future')
                    AND CAST(strftime('%s', s.first_seen_at) AS INTEGER) >= ?
                  )
                )
              GROUP BY s.event_id
            )
            SELECT e.id, m.title, m.snippet, m.source, e.url, e.canonical_url,
                   m.source_url,
                   m.kol_key, m.kol_name, m.kol_name_cn,
                   {_event_attribution_basis_sql('m')} AS attribution_basis,
                   {_event_matched_alias_sql('m')} AS matched_alias,
                   {_event_rule_impact_sql('m')} AS impact,
                   {_event_finance_sql('m')} AS has_market_kw,
                   m.tickers,
                   {_event_eligible_source_count_sql()} AS source_count,
                   e.fetched_at, m.published_at,
                   m.published_at_status,
                   ai.input_hash AS ai_input_hash,
                   ai.prompt_version AS ai_prompt_version,
                   ai.status AS ai_status,
                   ai.model AS ai_model,
                   ai.updated_at AS ai_updated_at,
                   ai.next_attempt_at AS ai_next_attempt_at,
                   ai.attempt_count AS ai_attempt_count
            FROM recent_events recent
            JOIN events e ON e.id=recent.event_id
            JOIN event_sightings m ON m.id=(
              SELECT s.id FROM event_sightings s
              WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
              ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1
            )
            LEFT JOIN event_enrichments ai ON ai.event_id=e.id
            WHERE {_event_intelligence_sql('m')} AND (
                ai.event_id IS NULL
                OR ai.status='pending'
                OR ai.status='ready'
                OR (
                    ai.status='retry'
                    AND (ai.next_attempt_at IS NULL OR ai.next_attempt_at <= ?)
                )
                OR (
                    ai.status='processing'
                    AND ai.updated_at <= ?
                )
                OR ai.status='failed'
            )
            ORDER BY
              CASE
                WHEN ai.event_id IS NULL OR ai.status='pending' THEN 0
                WHEN ai.status IN ('retry', 'processing') THEN 1
                WHEN ai.status='ready' THEN 2
                ELSE 3
              END,
              CASE {_event_rule_impact_sql('m')}
                WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
              {_event_finance_sql('m')} DESC,
              recent.activity_epoch DESC,
              e.id DESC
            LIMIT ?
            """,
            (
                cutoff,
                cutoff,
                current.replace(microsecond=0).isoformat(),
                (current - timedelta(minutes=20)).replace(microsecond=0).isoformat(),
                safe_limit,
            ),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_event_enrichment(
    event_id: int,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    evidence_basis: str,
    now: datetime | None = None,
    processing_lease_seconds: int = 20 * 60,
    current_event_check: Callable[[Mapping[str, Any]], bool] | None = None,
    return_attempt_count: bool = False,
) -> str | tuple[str, int] | None:
    """Atomically claim an event unless its cache is fresh or lease is live."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = current.replace(microsecond=0).isoformat()
    with conn(immediate=True) as c:
        if current_event_check is not None:
            event_row = c.execute(
                f"SELECT e.id, m.title, m.snippet, m.source, e.url, "
                "e.canonical_url, m.source_url, m.kol_key, m.kol_name, "
                "m.kol_name_cn, m.tickers "
                "FROM events e JOIN event_sightings m ON m.id=("
                "SELECT s.id FROM event_sightings s WHERE s.event_id=e.id "
                f"AND {_event_intelligence_sql('s')} "
                f"ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1) "
                "WHERE e.id=?",
                (int(event_id),),
            ).fetchone()
            if event_row is None or not current_event_check(dict(event_row)):
                return None
        row = c.execute(
            "SELECT input_hash, prompt_version, status, model, updated_at, "
            "next_attempt_at, attempt_count FROM event_enrichments "
            "WHERE event_id=?",
            (int(event_id),),
        ).fetchone()
        same_cache = bool(
            row
            and row["input_hash"] == input_hash
            and row["prompt_version"] == prompt_version
            and row["model"] == model
        )
        if same_cache and row["status"] == "ready":
            return None
        if same_cache and row["status"] == "failed":
            return None
        if same_cache and row["status"] == "retry":
            retry_at = _parse_utc_datetime(row["next_attempt_at"])
            if retry_at is not None and retry_at > current:
                return None
        if same_cache and row["status"] == "processing":
            updated = _parse_utc_datetime(row["updated_at"])
            if updated is not None and (
                current - updated
            ).total_seconds() < max(60, int(processing_lease_seconds)):
                return None

        attempts = int(row["attempt_count"] or 0) + 1 if same_cache else 1
        claim_token = secrets.token_urlsafe(24)
        c.execute(
            """
            INSERT INTO event_enrichments (
              event_id, input_hash, prompt_version, status, headline_zh,
              summary_zh, why_it_matters_zh, impact_level,
              impact_path_json, tags_json, assets_json, cluster_key, language,
              confidence, evidence_basis, model, generated_at, updated_at,
              attempt_count, next_attempt_at, error_code, claim_token
            ) VALUES (?, ?, ?, 'processing', '', '', '', 'unknown',
                      '[]', '[]', '[]', '', 'unknown', NULL, ?, ?, NULL, ?, ?, NULL, '', ?)
            ON CONFLICT(event_id) DO UPDATE SET
              input_hash=excluded.input_hash,
              prompt_version=excluded.prompt_version,
              status='processing',
              headline_zh='', summary_zh='', why_it_matters_zh='',
              impact_level='unknown', impact_path_json='[]', tags_json='[]',
              assets_json='[]', cluster_key='', language='unknown',
              confidence=NULL, evidence_basis=excluded.evidence_basis,
              model=excluded.model, generated_at=NULL,
              updated_at=excluded.updated_at,
              attempt_count=excluded.attempt_count,
              next_attempt_at=NULL, error_code='',
              claim_token=excluded.claim_token
            """,
            (
                int(event_id),
                input_hash,
                prompt_version,
                evidence_basis,
                model,
                now_iso,
                attempts,
                claim_token,
            ),
        )
    return (claim_token, attempts) if return_attempt_count else claim_token


def save_event_enrichment(
    event_id: int,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    claim_token: str,
    evidence_basis: str,
    result: dict[str, Any],
    generated_at: str | None = None,
) -> bool:
    """Store a validated result only if it still owns the active claim."""
    completed = generated_at or _now_iso()
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE event_enrichments
            SET status='ready', headline_zh=?, summary_zh=?,
                why_it_matters_zh=?, impact_level=?, impact_path_json=?,
                tags_json=?, assets_json=?, cluster_key=?, language=?,
                confidence=?, evidence_basis=?, model=?, generated_at=?,
                updated_at=?, next_attempt_at=NULL, error_code='', claim_token=''
            WHERE event_id=? AND input_hash=? AND prompt_version=?
              AND model=? AND claim_token=? AND status='processing'
            """,
            (
                result["headline_zh"],
                result["summary_zh"],
                result["why_it_matters_zh"],
                result["impact_level"],
                json.dumps(result["impact_path"], ensure_ascii=False),
                json.dumps(result["tags"], ensure_ascii=False),
                json.dumps(result["assets"], ensure_ascii=False),
                result["cluster_key"],
                result["language"],
                result["confidence"],
                evidence_basis,
                model,
                completed,
                completed,
                int(event_id),
                input_hash,
                prompt_version,
                model,
                claim_token,
            ),
        )
    return cur.rowcount == 1


def fail_event_enrichment(
    event_id: int,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    claim_token: str,
    error_code: str,
    retry_after_seconds: int | None,
    now: datetime | None = None,
) -> bool:
    """Record only a bounded error category; never persist provider text."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated = current.replace(microsecond=0).isoformat()
    next_attempt = None
    status = "failed"
    if retry_after_seconds is not None:
        status = "retry"
        next_attempt = (
            current + timedelta(seconds=max(60, int(retry_after_seconds)))
        ).replace(microsecond=0).isoformat()
    safe_code = re.sub(r"[^a-z0-9_:-]", "", str(error_code).lower())[:48]
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE event_enrichments
            SET status=?, updated_at=?, next_attempt_at=?, error_code=?,
                claim_token=''
            WHERE event_id=? AND input_hash=? AND prompt_version=?
              AND model=? AND claim_token=? AND status='processing'
            """,
            (
                status,
                updated,
                next_attempt,
                safe_code or "unknown",
                int(event_id),
                input_hash,
                prompt_version,
                model,
                claim_token,
            ),
        )
    return cur.rowcount == 1


# ─── Macro monitored-event intelligence cache ─────────

def _macro_enrichment_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    status = str(item.get("status") or "pending")
    output: dict[str, Any] = {
        "event_key": str(item.get("event_key") or ""),
        "input_hash": str(item.get("input_hash") or ""),
        "prompt_version": str(item.get("prompt_version") or ""),
        "status": status,
        "model": str(item.get("model") or ""),
        "attempt_count": int(item.get("attempt_count") or 0),
        "updated_at": item.get("updated_at"),
        "next_attempt_at": item.get("next_attempt_at"),
        "ai_enrichment": None,
    }
    if status != "ready":
        return output

    def decode_list(key: str) -> list[Any]:
        raw = item.get(key, "[]") or "[]"
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    output["ai_enrichment"] = {
        "status": "ready",
        "headline_zh": item.get("headline_zh") or "",
        "summary_zh": item.get("summary_zh") or "",
        "why_it_matters_zh": item.get("why_it_matters_zh") or "",
        "impact_level": item.get("impact_level") or "unknown",
        "impact_path": decode_list("impact_path_json"),
        "tags": decode_list("tags_json"),
        "assets": decode_list("assets_json"),
        "cluster_key": item.get("cluster_key") or "",
        "language": item.get("language") or "unknown",
        "confidence": item.get("confidence"),
        "evidence_basis": item.get("evidence_basis") or "title_only",
        "model": item.get("model") or "",
        "generated_at": item.get("generated_at"),
    }
    return output


def query_macro_event_enrichments(
    event_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Load a bounded set of macro-event cache rows in one query."""
    keys: list[str] = []
    seen: set[str] = set()
    for value in event_keys:
        key = str(value or "").strip()[:160]
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
        if len(keys) >= 100:
            break
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT event_key, input_hash, prompt_version, status,
                   headline_zh, summary_zh, why_it_matters_zh, impact_level,
                   impact_path_json, tags_json, assets_json, cluster_key,
                   language, confidence, evidence_basis, model, generated_at,
                   updated_at, attempt_count, next_attempt_at
            FROM macro_event_enrichments
            WHERE event_key IN ({placeholders})
            """,
            keys,
        ).fetchall()
    decoded = (_macro_enrichment_row(row) for row in rows)
    return {
        item["event_key"]: item
        for item in decoded
        if isinstance(item, dict) and item.get("event_key")
    }


def get_macro_event_enrichment(
    event_key: str,
    *,
    input_hash: str | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Return one cache row, optionally requiring an exact cache identity."""
    item = query_macro_event_enrichments([event_key]).get(str(event_key or ""))
    if item is None:
        return None
    for field, expected in (
        ("input_hash", input_hash),
        ("prompt_version", prompt_version),
        ("model", model),
    ):
        if expected is not None and item.get(field) != expected:
            return None
    return item


def claim_macro_event_enrichment(
    event_key: str,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    evidence_basis: str,
    now: datetime | None = None,
    processing_lease_seconds: int = 20 * 60,
    current_snapshot_check: Callable[[Mapping[str, Any]], bool] | None = None,
) -> tuple[str, int] | None:
    """Atomically claim one current macro-event cache and return its attempt."""
    safe_key = str(event_key or "").strip()[:160]
    if not safe_key:
        raise ValueError("event_key is required")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = current.replace(microsecond=0).isoformat()
    with conn(immediate=True) as c:
        if current_snapshot_check is not None:
            snapshot_row = c.execute(
                "SELECT payload FROM macro_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            try:
                snapshot = (
                    json.loads(snapshot_row["payload"])
                    if snapshot_row is not None
                    else None
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = None
            if not isinstance(snapshot, Mapping) or not current_snapshot_check(snapshot):
                return None
        row = c.execute(
            "SELECT input_hash, prompt_version, status, model, updated_at, "
            "next_attempt_at, attempt_count FROM macro_event_enrichments "
            "WHERE event_key=?",
            (safe_key,),
        ).fetchone()
        same_cache = bool(
            row
            and row["input_hash"] == input_hash
            and row["prompt_version"] == prompt_version
            and row["model"] == model
        )
        if same_cache and row["status"] in {"ready", "failed"}:
            return None
        if same_cache and row["status"] == "retry":
            retry_at = _parse_utc_datetime(row["next_attempt_at"])
            if retry_at is not None and retry_at > current:
                return None
        if same_cache and row["status"] == "processing":
            updated = _parse_utc_datetime(row["updated_at"])
            if updated is not None and (
                current - updated
            ).total_seconds() < max(60, int(processing_lease_seconds)):
                return None

        attempts = int(row["attempt_count"] or 0) + 1 if same_cache else 1
        claim_token = secrets.token_urlsafe(24)
        c.execute(
            """
            INSERT INTO macro_event_enrichments (
              event_key, input_hash, prompt_version, status, headline_zh,
              summary_zh, why_it_matters_zh, impact_level,
              impact_path_json, tags_json, assets_json, cluster_key, language,
              confidence, evidence_basis, model, generated_at, updated_at,
              attempt_count, next_attempt_at, error_code, claim_token
            ) VALUES (?, ?, ?, 'processing', '', '', '', 'unknown',
                      '[]', '[]', '[]', '', 'unknown', NULL, ?, ?, NULL, ?, ?, NULL, '', ?)
            ON CONFLICT(event_key) DO UPDATE SET
              input_hash=excluded.input_hash,
              prompt_version=excluded.prompt_version,
              status='processing',
              headline_zh='', summary_zh='', why_it_matters_zh='',
              impact_level='unknown', impact_path_json='[]', tags_json='[]',
              assets_json='[]', cluster_key='', language='unknown',
              confidence=NULL, evidence_basis=excluded.evidence_basis,
              model=excluded.model, generated_at=NULL,
              updated_at=excluded.updated_at,
              attempt_count=excluded.attempt_count,
              next_attempt_at=NULL, error_code='',
              claim_token=excluded.claim_token
            """,
            (
                safe_key,
                input_hash,
                prompt_version,
                evidence_basis,
                model,
                now_iso,
                attempts,
                claim_token,
            ),
        )
    return claim_token, attempts


def save_macro_event_enrichment(
    event_key: str,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    claim_token: str,
    evidence_basis: str,
    result: dict[str, Any],
    generated_at: str | None = None,
) -> bool:
    """Store validated macro-event output only for the active claim."""
    completed = generated_at or _now_iso()
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE macro_event_enrichments
            SET status='ready', headline_zh=?, summary_zh=?,
                why_it_matters_zh=?, impact_level=?, impact_path_json=?,
                tags_json=?, assets_json=?, cluster_key=?, language=?,
                confidence=?, evidence_basis=?, model=?, generated_at=?,
                updated_at=?, next_attempt_at=NULL, error_code='', claim_token=''
            WHERE event_key=? AND input_hash=? AND prompt_version=?
              AND model=? AND claim_token=? AND status='processing'
            """,
            (
                result["headline_zh"],
                result["summary_zh"],
                result["why_it_matters_zh"],
                result["impact_level"],
                json.dumps(result["impact_path"], ensure_ascii=False),
                json.dumps(result["tags"], ensure_ascii=False),
                json.dumps(result["assets"], ensure_ascii=False),
                result["cluster_key"],
                result["language"],
                result["confidence"],
                evidence_basis,
                model,
                completed,
                completed,
                str(event_key or "").strip()[:160],
                input_hash,
                prompt_version,
                model,
                claim_token,
            ),
        )
    return cur.rowcount == 1


def fail_macro_event_enrichment(
    event_key: str,
    *,
    input_hash: str,
    prompt_version: str,
    model: str,
    claim_token: str,
    error_code: str,
    retry_after_seconds: int | None,
    now: datetime | None = None,
) -> bool:
    """Record a bounded macro-event error category without provider text."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated = current.replace(microsecond=0).isoformat()
    status = "failed"
    next_attempt = None
    if retry_after_seconds is not None:
        status = "retry"
        next_attempt = (
            current + timedelta(seconds=max(60, int(retry_after_seconds)))
        ).replace(microsecond=0).isoformat()
    safe_code = re.sub(r"[^a-z0-9_:-]", "", str(error_code).lower())[:48]
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE macro_event_enrichments
            SET status=?, updated_at=?, next_attempt_at=?, error_code=?,
                claim_token=''
            WHERE event_key=? AND input_hash=? AND prompt_version=?
              AND model=? AND claim_token=? AND status='processing'
            """,
            (
                status,
                updated,
                next_attempt,
                safe_code or "unknown",
                str(event_key or "").strip()[:160],
                input_hash,
                prompt_version,
                model,
                claim_token,
            ),
        )
    return cur.rowcount == 1


# ─── Authenticated manual enrichment priority queue ───

def _bounded_env_integer(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _ai_request_limits() -> dict[str, int]:
    hourly = _bounded_env_integer(
        _AI_REQUEST_HOURLY_ENV, 20, minimum=1, maximum=200
    )
    daily = _bounded_env_integer(
        _AI_REQUEST_DAILY_ENV, 50, minimum=hourly, maximum=500
    )
    return {
        "cooldown": _bounded_env_integer(
            _AI_REQUEST_COOLDOWN_ENV, 10, minimum=1, maximum=300
        ),
        "hourly": hourly,
        "daily": daily,
        "pending": _bounded_env_integer(
            _AI_REQUEST_PENDING_ENV, 50, minimum=1, maximum=200
        ),
        "retention_days": _bounded_env_integer(
            _AI_REQUEST_RETENTION_ENV, 7, minimum=1, maximum=90
        ),
    }


def _ai_request_identity(
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    model: str,
) -> tuple[str, str, str, str, str]:
    kind = str(subject_type or "").strip()
    if kind not in {"event", "macro_event"}:
        raise ValueError("unsupported ai request subject_type")
    key = _required_text(subject_key, "subject_key", maximum=160)
    digest = _required_text(input_hash, "input_hash", maximum=128)
    prompt = _required_text(prompt_version, "prompt_version", maximum=100)
    clean_model = _required_text(model, "model", maximum=100)
    return kind, key, digest, prompt, clean_model


def _ai_cache_request_state_in(
    c: sqlite3.Connection,
    *,
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    model: str,
    now: datetime,
    ready_state: str,
    processing_lease_seconds: int = 20 * 60,
) -> dict[str, Any] | None:
    if subject_type == "event":
        row = c.execute(
            "SELECT input_hash, prompt_version, model, status, updated_at, "
            "next_attempt_at, generated_at FROM event_enrichments "
            "WHERE event_id=?",
            (int(subject_key),),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT input_hash, prompt_version, model, status, updated_at, "
            "next_attempt_at, generated_at FROM macro_event_enrichments "
            "WHERE event_key=?",
            (subject_key,),
        ).fetchone()
    if (
        row is None
        or row["input_hash"] != input_hash
        or row["prompt_version"] != prompt_version
        or row["model"] != model
    ):
        return None

    status = str(row["status"] or "")
    if status == "ready":
        return {
            "state": ready_state,
            "can_request": False,
            "generated_at": row["generated_at"],
        }
    if status == "failed":
        return {"state": "failed", "can_request": False}
    if status == "retry":
        retry_at = _parse_utc_datetime(row["next_attempt_at"])
        if retry_at is not None and retry_at > now:
            retry_after = max(1, math.ceil((retry_at - now).total_seconds()))
            return {
                "state": "retry_wait",
                "can_request": False,
                "retry_after_seconds": retry_after,
                "next_attempt_at": retry_at.replace(microsecond=0).isoformat(),
            }
        return None
    if status == "processing":
        updated = _parse_utc_datetime(row["updated_at"])
        if updated is not None and (
            now - updated
        ).total_seconds() < max(60, int(processing_lease_seconds)):
            return {"state": "processing", "can_request": False}
        return None
    return None


def _prune_ai_requests_in(
    c: sqlite3.Connection,
    *,
    now: datetime,
    retention_days: int,
) -> None:
    cutoff = (now - timedelta(days=retention_days)).replace(
        microsecond=0
    ).isoformat()
    c.execute(
        "DELETE FROM ai_enrichment_requests "
        "WHERE status<>'pending' AND accepted_at<?",
        (cutoff,),
    )
    # Keep storage bounded even when a custom retention window is generous.
    c.execute(
        "DELETE FROM ai_enrichment_requests "
        "WHERE status<>'pending' AND id NOT IN ("
        "SELECT id FROM ai_enrichment_requests WHERE status<>'pending' "
        "ORDER BY id DESC LIMIT 2000)"
    )


def request_ai_enrichment(
    *,
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    model: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cache-aware, quota-bound enqueue for one current public identity."""
    kind, key, digest, prompt, clean_model = _ai_request_identity(
        subject_type, subject_key, input_hash, prompt_version, model
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = current.replace(microsecond=0).isoformat()
    limits = _ai_request_limits()
    with conn(immediate=True) as c:
        cache_state = _ai_cache_request_state_in(
            c,
            subject_type=kind,
            subject_key=key,
            input_hash=digest,
            prompt_version=prompt,
            model=clean_model,
            now=current,
            ready_state="cached",
        )
        if cache_state is not None:
            return cache_state

        duplicate = c.execute(
            "SELECT id, accepted_at FROM ai_enrichment_requests "
            "WHERE subject_type=? AND subject_key=? AND input_hash=? "
            "AND prompt_version=? AND model=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (kind, key, digest, prompt, clean_model),
        ).fetchone()
        if duplicate is not None:
            return {
                "state": "already_queued",
                "can_request": False,
                "accepted_at": duplicate["accepted_at"],
            }

        _prune_ai_requests_in(
            c,
            now=current,
            retention_days=limits["retention_days"],
        )
        latest = c.execute(
            "SELECT accepted_at FROM ai_enrichment_requests "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is not None:
            latest_at = _parse_utc_datetime(latest["accepted_at"])
            if latest_at is not None:
                elapsed = (current - latest_at).total_seconds()
                if elapsed < limits["cooldown"]:
                    return {
                        "state": "rate_limited",
                        "can_request": False,
                        "retry_after_seconds": max(
                            1, math.ceil(limits["cooldown"] - elapsed)
                        ),
                    }

        for window_seconds, limit in (
            (3600, limits["hourly"]),
            (24 * 3600, limits["daily"]),
        ):
            cutoff = (current - timedelta(seconds=window_seconds)).replace(
                microsecond=0
            ).isoformat()
            rows = c.execute(
                "SELECT accepted_at FROM ai_enrichment_requests "
                "WHERE accepted_at>? ORDER BY accepted_at",
                (cutoff,),
            ).fetchall()
            if len(rows) >= limit:
                oldest = _parse_utc_datetime(rows[0]["accepted_at"])
                retry_after = window_seconds
                if oldest is not None:
                    retry_after = max(
                        1,
                        math.ceil(
                            (oldest + timedelta(seconds=window_seconds) - current)
                            .total_seconds()
                        ),
                    )
                return {
                    "state": "rate_limited",
                    "can_request": False,
                    "retry_after_seconds": retry_after,
                }

        replaceable = int(
            c.execute(
                "SELECT COUNT(*) FROM ai_enrichment_requests "
                "WHERE status='pending' AND subject_type=? AND subject_key=?",
                (kind, key),
            ).fetchone()[0]
        )
        pending = int(
            c.execute(
                "SELECT COUNT(*) FROM ai_enrichment_requests "
                "WHERE status='pending'"
            ).fetchone()[0]
        )
        if pending - replaceable >= limits["pending"]:
            return {
                "state": "rate_limited",
                "can_request": False,
                "retry_after_seconds": 60,
            }

        # A changed source/prompt supersedes an older unclaimed request for the
        # same subject.  A worker completing that older row cannot touch this
        # new identity because completion is id-and-identity scoped below.
        c.execute(
            "UPDATE ai_enrichment_requests "
            "SET status='superseded', updated_at=?, completed_at=? "
            "WHERE status='pending' AND subject_type=? AND subject_key=?",
            (now_iso, now_iso, kind, key),
        )
        cur = c.execute(
            """
            INSERT INTO ai_enrichment_requests (
              subject_type, subject_key, input_hash, prompt_version, model,
              status, accepted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (kind, key, digest, prompt, clean_model, now_iso, now_iso),
        )
        request_id = cur.lastrowid
    if request_id is None:
        raise sqlite3.IntegrityError("ai request insert returned no id")
    return {
        "state": "queued",
        "can_request": False,
        "accepted_at": now_iso,
        "request_id": int(request_id),
    }


def get_ai_enrichment_request_status(
    *,
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    model: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    kind, key, digest, prompt, clean_model = _ai_request_identity(
        subject_type, subject_key, input_hash, prompt_version, model
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with conn() as c:
        cache_state = _ai_cache_request_state_in(
            c,
            subject_type=kind,
            subject_key=key,
            input_hash=digest,
            prompt_version=prompt,
            model=clean_model,
            now=current,
            ready_state="ready",
        )
        if cache_state is not None:
            return cache_state
        queued = c.execute(
            "SELECT accepted_at FROM ai_enrichment_requests "
            "WHERE subject_type=? AND subject_key=? AND input_hash=? "
            "AND prompt_version=? AND model=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (kind, key, digest, prompt, clean_model),
        ).fetchone()
    if queued is not None:
        return {
            "state": "queued",
            "can_request": False,
            "accepted_at": queued["accepted_at"],
        }
    return {"state": "pending", "can_request": True}


def query_pending_ai_enrichment_requests(
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 200))
    with conn() as c:
        rows = c.execute(
            "SELECT id, subject_type, subject_key, input_hash, prompt_version, "
            "model, accepted_at FROM ai_enrichment_requests "
            "WHERE status='pending' ORDER BY id LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def complete_ai_enrichment_request(
    request_id: int,
    *,
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    superseded: bool = False,
    now: datetime | None = None,
) -> bool:
    kind, key, digest, prompt, _ = _ai_request_identity(
        subject_type,
        subject_key,
        input_hash,
        prompt_version,
        "internal",
    )
    completed = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).replace(microsecond=0).isoformat()
    with conn(immediate=True) as c:
        cur = c.execute(
            "UPDATE ai_enrichment_requests SET status=?, updated_at=?, "
            "completed_at=? WHERE id=? AND subject_type=? AND subject_key=? "
            "AND input_hash=? AND prompt_version=? AND status='pending'",
            (
                "superseded" if superseded else "completed",
                completed,
                completed,
                int(request_id),
                kind,
                key,
                digest,
                prompt,
            ),
        )
    return cur.rowcount == 1


def get_event_enrichment_subject(event_id: int) -> dict[str, Any] | None:
    """Load one event for a manual request, without the normal age window."""
    with conn() as c:
        row = c.execute(
            f"SELECT e.id, m.title, m.snippet, m.source, e.url, "
            "e.canonical_url, m.source_url, m.kol_key, m.kol_name, "
            "m.kol_name_cn, m.tickers, "
            f"{_event_rule_impact_sql('m')} AS impact, "
            f"{_event_finance_sql('m')} AS has_market_kw, "
            f"{_event_eligible_source_count_sql()} AS source_count, "
            "e.fetched_at, m.published_at, "
            "m.published_at_status "
            "FROM events e JOIN event_sightings m ON m.id=("
            "SELECT s.id FROM event_sightings s WHERE s.event_id=e.id "
            f"AND {_event_intelligence_sql('s')} "
            f"ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1) "
            "WHERE e.id=?",
            (int(event_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def save_macro_snapshot(report: dict[str, Any]) -> int:
    composite = report.get("composite_risk", {})
    with conn() as c:
        cur = c.execute(
            "INSERT INTO macro_snapshots (created_at, composite_score, composite_level, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                _now_iso(),
                composite.get("score"),
                composite.get("level"),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        return cur.lastrowid or 0


def upsert_market_prices(
    asset_key: str,
    provider: str,
    prices: Iterable[dict[str, Any]],
    *,
    provider_symbol: str | None = None,
    observed_at: str | None = None,
) -> int:
    """Upsert validated bars by stable asset/provider/timestamp identity."""
    clean_asset = _required_text(asset_key, "asset_key", maximum=100)
    clean_provider = _required_text(provider, "provider", maximum=32).lower()
    clean_symbol = _optional_text(
        provider_symbol, "provider_symbol", maximum=100
    )
    default_observed = _observed_at(observed_at)
    keyed: dict[int, tuple[Any, ...]] = {}
    for raw in prices:
        if not isinstance(raw, dict):
            raise ValueError("each market price must be an object")
        timestamp = _bounded_integer(
            raw.get("timestamp"),
            "timestamp",
            maximum=32_503_680_000,
        )
        close = _finite_number(
            raw.get("close"),
            "close",
            minimum=0.000000000001,
            maximum=1e15,
        )
        volume = _finite_number(
            raw.get("volume"),
            "volume",
            minimum=0,
            maximum=1e20,
            optional=True,
        )
        row_symbol = _optional_text(
            raw.get("provider_symbol", clean_symbol),
            "provider_symbol",
            maximum=100,
        )
        currency = _optional_text(raw.get("currency"), "currency", maximum=16)
        row_observed = _observed_at(raw.get("observed_at", default_observed))
        metadata = raw.get("metadata") if "metadata" in raw else {}
        keyed[timestamp] = (
            clean_asset,
            clean_provider,
            row_symbol,
            timestamp,
            close,
            volume,
            currency,
            _safe_json(metadata if metadata is not None else {}, "metadata"),
            row_observed,
        )

    with conn(immediate=True) as c:
        for values in keyed.values():
            c.execute(
                """
                INSERT INTO market_prices (
                  asset_key, provider, provider_symbol, timestamp, close, volume,
                  currency, metadata_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_key, provider, timestamp) DO UPDATE SET
                  provider_symbol=excluded.provider_symbol,
                  close=excluded.close,
                  volume=excluded.volume,
                  currency=excluded.currency,
                  metadata_json=excluded.metadata_json,
                  observed_at=excluded.observed_at
                """,
                values,
            )
    return len(keyed)


def query_market_prices(
    *,
    asset_key: str | None = None,
    provider: str | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if asset_key is not None:
        where.append("asset_key=?")
        params.append(str(asset_key))
    if provider is not None:
        where.append("provider=?")
        params.append(str(provider).lower())
    if start_timestamp is not None:
        where.append("timestamp>=?")
        params.append(
            _bounded_integer(
                start_timestamp, "start_timestamp", maximum=32_503_680_000
            )
        )
    if end_timestamp is not None:
        where.append("timestamp<=?")
        params.append(
            _bounded_integer(
                end_timestamp, "end_timestamp", maximum=32_503_680_000
            )
        )
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 10_000)))
    with conn() as c:
        rows = c.execute(
            "SELECT asset_key, provider, provider_symbol, timestamp, close, "
            "volume, currency, metadata_json, observed_at "
            f"FROM market_prices{where_sql} "
            "ORDER BY timestamp ASC, id ASC LIMIT ?",
            params,
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            item["metadata"] = {}
        output.append(item)
    return output


def upsert_market_reaction(
    source_type: str,
    source_id: str | int,
    asset_key: str,
    reaction: dict[str, Any],
    *,
    benchmark_asset_key: str | None = None,
    observed_at: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> int:
    """Upsert one event-study window after validating all numeric/JSON fields."""
    if not isinstance(reaction, dict):
        raise ValueError("reaction must be an object")
    clean_source_type = _required_text(
        source_type, "source_type", maximum=50
    )
    clean_source_id = _required_text(source_id, "source_id", maximum=200)
    clean_asset = _required_text(asset_key, "asset_key", maximum=100)
    window = _required_text(reaction.get("window"), "window", maximum=16)
    if window not in {"1D", "3D", "5D"}:
        raise ValueError("window must be one of 1D, 3D, or 5D")
    status = _required_text(reaction.get("status"), "status", maximum=20)
    if status not in _MARKET_REACTION_STATUSES:
        raise ValueError("invalid reaction status")
    method_version = _required_text(
        reaction.get("method_version"), "method_version", maximum=100
    )
    asset_return = _finite_number(
        reaction.get("asset_return"),
        "asset_return",
        minimum=-1.0,
        maximum=1e6,
        optional=True,
    )
    benchmark_return = _finite_number(
        reaction.get("benchmark_return"),
        "benchmark_return",
        minimum=-1.0,
        maximum=1e6,
        optional=True,
    )
    abnormal_return = _finite_number(
        reaction.get("abnormal_return"),
        "abnormal_return",
        minimum=-1e6,
        maximum=1e6,
        optional=True,
    )
    if status == "complete" and None in (
        asset_return,
        benchmark_return,
        abnormal_return,
    ):
        raise ValueError("complete reactions require all return values")
    direction = reaction.get("direction_confirmed")
    if direction is not None and not isinstance(direction, bool):
        raise ValueError("direction_confirmed must be boolean or null")
    expected_direction = _optional_text(
        reaction.get("expected_direction"),
        "expected_direction",
        maximum=16,
    )
    if expected_direction is not None:
        expected_direction = expected_direction.lower()
        if expected_direction not in {"positive", "negative"}:
            raise ValueError("expected_direction must be positive, negative, or null")
    observed_direction = _optional_text(
        reaction.get("observed_direction"),
        "observed_direction",
        maximum=16,
    )
    if observed_direction is not None:
        observed_direction = observed_direction.lower()
        if observed_direction not in {"positive", "negative", "neutral"}:
            raise ValueError(
                "observed_direction must be positive, negative, neutral, or null"
            )
    if expected_direction and observed_direction:
        calculated_direction = (
            observed_direction == expected_direction
            if observed_direction != "neutral"
            else None
        )
        if direction is not None and direction != calculated_direction:
            raise ValueError("direction_confirmed conflicts with stored directions")
        direction = calculated_direction
    sample_count = _bounded_integer(
        reaction.get("sample_count", 0),
        "sample_count",
        maximum=1_000_000,
    )
    timestamps_json = _reaction_timestamps_json(
        reaction.get("data_timestamps") or {}
    )
    clean_benchmark = _optional_text(
        benchmark_asset_key
        if benchmark_asset_key is not None
        else reaction.get("benchmark_asset_key"),
        "benchmark_asset_key",
        maximum=100,
    )
    clean_observed = _observed_at(
        observed_at
        if observed_at is not None
        else reaction.get("observed_at")
    )
    provider = _optional_text(
        reaction.get("provider"), "provider", maximum=32
    )
    if provider is not None:
        provider = provider.lower()
    provider_symbol = _optional_text(
        reaction.get("provider_symbol"), "provider_symbol", maximum=100
    )
    proxy_for = _optional_text(
        reaction.get("proxy_for"), "proxy_for", maximum=100
    )
    asset_status = _optional_text(
        reaction.get("asset_status"), "asset_status", maximum=20
    )
    if asset_status is not None:
        asset_status = asset_status.lower()
        if asset_status not in _MARKET_SERIES_STATUSES:
            raise ValueError("invalid asset_status")
    benchmark_status = _optional_text(
        reaction.get("benchmark_status"), "benchmark_status", maximum=20
    )
    if benchmark_status is not None:
        benchmark_status = benchmark_status.lower()
        if benchmark_status not in _MARKET_SERIES_STATUSES:
            raise ValueError("invalid benchmark_status")
    reason_code = _market_reason_code(
        reaction.get("reason_code", reaction.get("reason"))
    )
    next_due_at = _optional_utc_at(reaction.get("next_due_at"), "next_due_at")
    if status == "pending" and next_due_at is None:
        raise ValueError("pending reactions require next_due_at")
    if status == "complete":
        reason_code = None
        next_due_at = None
    values = (
        clean_source_type,
        clean_source_id,
        clean_asset,
        window,
        clean_benchmark,
        asset_return,
        benchmark_return,
        abnormal_return,
        expected_direction,
        observed_direction,
        None if direction is None else int(direction),
        status,
        sample_count,
        timestamps_json,
        method_version,
        clean_observed,
        provider,
        provider_symbol,
        proxy_for,
        asset_status,
        benchmark_status,
        reason_code,
        next_due_at,
    )

    def execute(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO market_reactions (
              source_type, source_id, asset_key, window, benchmark_asset_key,
              asset_return, benchmark_return, abnormal_return,
              expected_direction, observed_direction, direction_confirmed,
              status, sample_count, data_timestamps_json, method_version,
              observed_at, provider, provider_symbol, proxy_for, asset_status,
              benchmark_status, reason_code, next_due_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?
            )
            ON CONFLICT(
              source_type, source_id, asset_key, window, method_version
            ) DO UPDATE SET
              benchmark_asset_key=excluded.benchmark_asset_key,
              asset_return=excluded.asset_return,
              benchmark_return=excluded.benchmark_return,
              abnormal_return=excluded.abnormal_return,
              expected_direction=excluded.expected_direction,
              observed_direction=excluded.observed_direction,
              direction_confirmed=excluded.direction_confirmed,
              status=excluded.status,
              sample_count=excluded.sample_count,
              data_timestamps_json=excluded.data_timestamps_json,
              provider=excluded.provider,
              provider_symbol=excluded.provider_symbol,
              proxy_for=excluded.proxy_for,
              asset_status=excluded.asset_status,
              benchmark_status=excluded.benchmark_status,
              reason_code=excluded.reason_code,
              next_due_at=excluded.next_due_at,
              observed_at=excluded.observed_at
            WHERE
              CASE excluded.status
                WHEN 'complete' THEN 3
                WHEN 'preliminary' THEN 2
                ELSE 1
              END
              >
              CASE market_reactions.status
                WHEN 'complete' THEN 3
                WHEN 'preliminary' THEN 2
                ELSE 1
              END
              OR (
                CASE excluded.status
                  WHEN 'complete' THEN 3
                  WHEN 'preliminary' THEN 2
                  ELSE 1
                END
                =
                CASE market_reactions.status
                  WHEN 'complete' THEN 3
                  WHEN 'preliminary' THEN 2
                  ELSE 1
                END
                AND excluded.observed_at >= market_reactions.observed_at
              )
            """,
            values,
        )
    if _connection is None:
        with conn(immediate=True) as c:
            execute(c)
    else:
        execute(_connection)
    return 1


def upsert_market_reactions(
    source_type: str,
    source_id: str | int,
    asset_key: str,
    reaction_result: dict[str, Any],
    *,
    benchmark_asset_key: str | None = None,
    observed_at: str | None = None,
) -> int:
    """Expand a compute_event_reaction result into idempotent window rows."""
    if not isinstance(reaction_result, dict):
        raise ValueError("reaction_result must be an object")
    windows = reaction_result.get("windows")
    if not isinstance(windows, dict):
        raise ValueError("reaction_result.windows must be an object")
    method_version = reaction_result.get("method_version")
    count = 0
    with conn(immediate=True) as c:
        for label in ("1D", "3D", "5D"):
            raw = windows.get(label)
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["window"] = label
            item["method_version"] = item.get("method_version") or method_version
            item["expected_direction"] = (
                item.get("expected_direction")
                or reaction_result.get("expected_direction")
            )
            for field in (
                "provider",
                "provider_symbol",
                "proxy_for",
                "asset_status",
                "benchmark_status",
                "reason_code",
                "next_due_at",
            ):
                if field not in item:
                    item[field] = reaction_result.get(field)
            count += upsert_market_reaction(
                source_type,
                source_id,
                asset_key,
                item,
                benchmark_asset_key=benchmark_asset_key,
                observed_at=observed_at,
                _connection=c,
            )
    return count


def query_market_reactions(
    *,
    source_type: str | None = None,
    source_id: str | int | None = None,
    asset_key: str | None = None,
    window: str | None = None,
    limit: int = 500,
    eligible_events_only: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    eligible_sources_sql = ""
    if eligible_events_only:
        eligible_sources_sql = f"""
            WITH eligible_events AS MATERIALIZED (
              SELECT e.id, e.dedup_key
              FROM events e
              WHERE EXISTS (
                SELECT 1 FROM event_sightings s
                WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
              )
            ),
            event_source_ids(source_id) AS MATERIALIZED (
              SELECT CAST(id AS TEXT) FROM eligible_events
              UNION
              SELECT dedup_key FROM eligible_events
              WHERE NULLIF(TRIM(dedup_key), '') IS NOT NULL
            )
        """
        where.append(
            "(LOWER(TRIM(mr.source_type))<>'event' OR mr.source_id IN ("
            "SELECT source_id FROM event_source_ids))"
        )
    params: list[Any] = []
    for column, value in (
        ("source_type", source_type),
        ("source_id", source_id),
        ("asset_key", asset_key),
        ("window", window),
    ):
        if value is not None:
            where.append(f"mr.{column}=?")
            params.append(str(value))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 5000)))
    with conn() as c:
        rows = c.execute(
            eligible_sources_sql
            + "SELECT mr.source_type, mr.source_id, mr.asset_key, mr.window, "
            "mr.benchmark_asset_key, mr.asset_return, mr.benchmark_return, "
            "mr.abnormal_return, mr.expected_direction, mr.observed_direction, "
            "mr.direction_confirmed, mr.status, mr.sample_count, "
            "mr.data_timestamps_json, mr.method_version, mr.observed_at, "
            "mr.provider, mr.provider_symbol, mr.proxy_for, mr.asset_status, "
            "mr.benchmark_status, mr.reason_code, mr.next_due_at "
            f"FROM market_reactions mr{where_sql} "
            "ORDER BY mr.observed_at DESC, mr.id DESC LIMIT ?",
            params,
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item["direction_confirmed"] is not None:
            item["direction_confirmed"] = bool(item["direction_confirmed"])
        try:
            item["data_timestamps"] = json.loads(
                item["data_timestamps_json"]
            )
        except (TypeError, json.JSONDecodeError):
            item["data_timestamps"] = {}
        output.append(item)
    return output


def save_portfolio_snapshot(snapshot: dict[str, Any]) -> int:
    """Persist one sanitized snapshot; identical source hashes reuse one row."""
    try:
        from kol_dashboard import portfolio as portfolio_domain
    except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
        import portfolio as portfolio_domain

    clean = portfolio_domain.validate_snapshot(snapshot)
    with conn(immediate=True) as c:
        existing = c.execute(
            "SELECT id FROM portfolio_snapshots WHERE source_hash=?",
            (clean["source_hash"],),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = c.execute(
            """
            INSERT INTO portfolio_snapshots (
              source_hash, schema_version, source_as_of, position_count, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                clean["source_hash"],
                clean["schema_version"],
                clean["as_of"],
                len(clean["positions"]),
                _now_iso(),
            ),
        )
        snapshot_id = cur.lastrowid
        if snapshot_id is None:
            raise sqlite3.IntegrityError("portfolio snapshot insert returned no id")
        for ordinal, position in enumerate(clean["positions"]):
            c.execute(
                """
                INSERT INTO portfolio_positions (
                  snapshot_id, ordinal, account, asset_key, symbol, name,
                  quantity, avg_cost, currency, asset_class, is_leveraged, as_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    ordinal,
                    position["account"],
                    position["asset_key"],
                    position["symbol"],
                    position["name"],
                    position["quantity"],
                    position["avg_cost"],
                    position["currency"],
                    position["asset_class"],
                    int(position["is_leveraged"]),
                    position["as_of"],
                ),
            )
    return int(snapshot_id)


def _utc_datetime(value: Any, field: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_portfolio_snapshot(
    *,
    now: datetime | str | None = None,
    stale_after_seconds: int = 7 * 24 * 60 * 60,
) -> dict[str, Any] | None:
    """Return the newest snapshot plus a staleness calculation."""
    stale_threshold = _bounded_integer(
        stale_after_seconds,
        "stale_after_seconds",
        maximum=10 * 365 * 24 * 60 * 60,
    )
    with conn() as c:
        snapshot = c.execute(
            """
            SELECT id, source_hash, schema_version, source_as_of,
                   position_count, created_at
            FROM portfolio_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not snapshot:
            return None
        rows = c.execute(
            """
            SELECT account, asset_key, symbol, name, quantity, avg_cost,
                   currency, asset_class, is_leveraged, as_of
            FROM portfolio_positions
            WHERE snapshot_id=?
            ORDER BY ordinal ASC
            """,
            (snapshot["id"],),
        ).fetchall()

    positions: list[dict[str, Any]] = []
    for row in rows:
        position = dict(row)
        position["is_leveraged"] = bool(position["is_leveraged"])
        positions.append(position)
    reference_text = snapshot["source_as_of"] or snapshot["created_at"]
    if len(reference_text) == 10:
        reference_text += "T00:00:00+00:00"
    reference = _utc_datetime(reference_text, "source_as_of")
    current = _utc_datetime(now, "now")
    raw_age_seconds = int((current - reference).total_seconds())
    clock_skew = raw_age_seconds < -300
    age_seconds = max(0, raw_age_seconds)
    return {
        "snapshot_id": int(snapshot["id"]),
        "schema_version": int(snapshot["schema_version"]),
        "source_hash": snapshot["source_hash"],
        "as_of": snapshot["source_as_of"],
        "created_at": snapshot["created_at"],
        "positions": positions,
        "staleness": {
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_threshold,
            "is_stale": clock_skew or age_seconds > stale_threshold,
            "clock_skew": clock_skew,
        },
    }


save_snapshot = save_portfolio_snapshot
latest_snapshot = latest_portfolio_snapshot


def _json_evidence(edge: dict[str, Any]) -> str:
    evidence: Any = (
        edge["evidence"] if "evidence" in edge else edge.get("evidence_json", {})
    )
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            pass
    return json.dumps(
        evidence if evidence is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def replace_relations(
    source_type: str,
    source_id: str | int,
    edges: Iterable[dict[str, Any]],
) -> int:
    """Atomically replace all explainable edges for one source.

    Stable unique keys and preserved ``created_at`` values make repeated writes
    of the same deterministic extraction idempotent.
    """
    source_type = str(source_type).strip()
    source_id = str(source_id).strip()
    if not source_type or not source_id:
        raise ValueError("source_type and source_id are required")

    grouped: dict[
        tuple[str, str],
        dict[tuple[str, str, str, str], dict[str, Any]],
    ] = {}
    required = (
        "topic_key",
        "asset_key",
        "relation_type",
        "direction",
        "strength",
        "confidence",
        "horizon",
        "method",
    )
    saw_edge = False
    for raw in edges:
        saw_edge = True
        missing = [field for field in required if raw.get(field) in (None, "")]
        if missing:
            raise ValueError(f"relation missing required fields: {', '.join(missing)}")
        edge = dict(raw)
        edge_source_type = str(edge.get("source_type") or source_type).strip()
        edge_source_id = str(edge.get("source_id") or source_id).strip()
        if not edge_source_type or not edge_source_id:
            raise ValueError("relation source_type and source_id are required")
        key = (
            str(edge["topic_key"]),
            str(edge["asset_key"]),
            str(edge["relation_type"]),
            str(edge["method"]),
        )
        grouped.setdefault((edge_source_type, edge_source_id), {})[key] = edge
    if not saw_edge:
        grouped[(source_type, source_id)] = {}

    now = _now_iso()
    with conn(immediate=True) as c:
        for (group_type, group_id), keyed in grouped.items():
            existing_keys = {
                (
                    row["topic_key"],
                    row["asset_key"],
                    row["relation_type"],
                    row["method"],
                )
                for row in c.execute(
                    """
                    SELECT topic_key, asset_key, relation_type, method
                    FROM relations WHERE source_type=? AND source_id=?
                    """,
                    (group_type, group_id),
                ).fetchall()
            }
            for key, edge in keyed.items():
                strength = max(0.0, min(1.0, float(edge["strength"])))
                confidence = max(0.0, min(1.0, float(edge["confidence"])))
                c.execute(
                    """
                    INSERT INTO relations (
                      source_type, source_id, topic_key, asset_key, relation_type,
                      direction, strength, confidence, horizon, method, rationale,
                      evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                      source_type, source_id, topic_key, asset_key,
                      relation_type, method
                    ) DO UPDATE SET
                      direction=excluded.direction,
                      strength=excluded.strength,
                      confidence=excluded.confidence,
                      horizon=excluded.horizon,
                      rationale=excluded.rationale,
                      evidence_json=excluded.evidence_json
                    """,
                    (
                        group_type,
                        group_id,
                        key[0],
                        key[1],
                        key[2],
                        str(edge["direction"]),
                        strength,
                        confidence,
                        str(edge["horizon"]),
                        key[3],
                        str(edge.get("rationale") or ""),
                        _json_evidence(edge),
                        str(edge.get("created_at") or now),
                    ),
                )
            for stale in existing_keys - set(keyed):
                c.execute(
                    """
                    DELETE FROM relations
                    WHERE source_type=? AND source_id=? AND topic_key=?
                      AND asset_key=? AND relation_type=? AND method=?
                    """,
                    (group_type, group_id, *stale),
                )
    return sum(len(keyed) for keyed in grouped.values())


def query_relations(
    *,
    source_type: str | None = None,
    source_id: str | int | None = None,
    topic_key: str | None = None,
    asset_key: str | None = None,
    relation_type: str | None = None,
    limit: int = 200,
    eligible_events_only: bool = False,
) -> list[dict[str, Any]]:
    where: list[str] = []
    eligible_sources_sql = ""
    if eligible_events_only:
        eligible_sources_sql = f"""
            WITH eligible_events AS MATERIALIZED (
              SELECT e.id, e.dedup_key
              FROM events e
              WHERE EXISTS (
                SELECT 1 FROM event_sightings s
                WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
              )
            ),
            event_source_ids(source_id) AS MATERIALIZED (
              SELECT CAST(id AS TEXT) FROM eligible_events
              UNION
              SELECT dedup_key FROM eligible_events
              WHERE NULLIF(TRIM(dedup_key), '') IS NOT NULL
            )
        """
        where.append(
            "(LOWER(TRIM(r.source_type))<>'event' OR r.source_id IN ("
            "SELECT source_id FROM event_source_ids))"
        )
    params: list[Any] = []
    for column, value in (
        ("source_type", source_type),
        ("source_id", source_id),
        ("topic_key", topic_key),
        ("asset_key", asset_key),
        ("relation_type", relation_type),
    ):
        if value is not None:
            where.append(f"r.{column}=?")
            params.append(str(value))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with conn() as c:
        rows = c.execute(
            eligible_sources_sql
            + "SELECT r.source_type, r.source_id, r.topic_key, r.asset_key, "
            "r.relation_type, r.direction, r.strength, r.confidence, "
            "r.horizon, r.method, r.rationale, r.evidence_json, r.created_at "
            f"FROM relations r{where_sql} "
            "ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


_DECISION_RELATIONS_SQL = f"""
    WITH eligible_events AS MATERIALIZED (
      SELECT e.id, e.dedup_key
      FROM events e
      WHERE EXISTS (
        SELECT 1 FROM event_sightings s
        WHERE s.event_id=e.id
          AND s.published_at_status='verified'
          AND s.published_at_epoch BETWEEN ? AND ?
          AND {_event_intelligence_sql('s')}
      )
    ),
    event_source_ids(source_id) AS MATERIALIZED (
      SELECT CAST(id AS TEXT) FROM eligible_events
      UNION
      SELECT dedup_key FROM eligible_events
      WHERE NULLIF(TRIM(dedup_key), '') IS NOT NULL
    ),
    event_relation_types(source_type) AS MATERIALIZED (
      SELECT DISTINCT r.source_type
      FROM relations r
      WHERE LOWER(TRIM(r.source_type))='event'
    ),
    latest_macro(source_id) AS MATERIALIZED (
      SELECT CAST(MAX(id) AS TEXT) FROM macro_snapshots
    )
    SELECT source_type, source_id, topic_key, asset_key, relation_type,
           direction, strength, confidence, horizon, method, rationale,
           evidence_json, created_at
    FROM (
      SELECT r.id AS relation_id, r.source_type, r.source_id, r.topic_key,
             r.asset_key, r.relation_type, r.direction, r.strength,
             r.confidence, r.horizon, r.method, r.rationale,
             r.evidence_json, r.created_at
      FROM event_relation_types rt
      CROSS JOIN event_source_ids esi
      CROSS JOIN relations AS r INDEXED BY idx_relation_source
      WHERE r.source_type=rt.source_type AND r.source_id=esi.source_id

      UNION ALL

      SELECT r.id AS relation_id, r.source_type, r.source_id, r.topic_key,
             r.asset_key, r.relation_type, r.direction, r.strength,
             r.confidence, r.horizon, r.method, r.rationale,
             r.evidence_json, r.created_at
      FROM latest_macro lm
      CROSS JOIN relations r
      WHERE LOWER(TRIM(r.source_type))='macro_snapshot'
        AND (
          r.source_id=lm.source_id
          OR r.source_id LIKE (lm.source_id || ':%')
        )
    ) selected
    ORDER BY created_at DESC, relation_id DESC
"""


def query_decision_relations(
    *,
    now: datetime | str | None = None,
    event_max_age_hours: int = 72,
) -> list[dict[str, Any]]:
    current = (
        _parse_utc_datetime(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        raise ValueError("now must include a timezone")
    maximum_age = max(1, min(int(event_max_age_hours), 24 * 365))
    current_epoch = int(current.timestamp())
    cutoff_epoch = current_epoch - maximum_age * 3600
    with conn() as c:
        rows = c.execute(
            _DECISION_RELATIONS_SQL, (cutoff_epoch, current_epoch)
        ).fetchall()
    return [dict(row) for row in rows]


_MARKET_VALIDATION_RELATIONS_SQL = f"""
    WITH eligible_events AS MATERIALIZED (
      SELECT e.id, e.dedup_key
      FROM events e
      WHERE EXISTS (
        SELECT 1 FROM event_sightings s
        WHERE s.event_id=e.id
          AND s.published_at_status='verified'
          AND s.published_at_epoch BETWEEN ? AND ?
          AND {_event_intelligence_sql('s')}
      )
    ),
    event_source_ids(source_id) AS MATERIALIZED (
      SELECT CAST(id AS TEXT) FROM eligible_events
      UNION
      SELECT dedup_key FROM eligible_events
      WHERE NULLIF(TRIM(dedup_key), '') IS NOT NULL
    ),
    event_relation_types(source_type) AS MATERIALIZED (
      SELECT DISTINCT r.source_type
      FROM relations r
      WHERE LOWER(TRIM(r.source_type))='event'
    )
    SELECT r.source_type, r.source_id, r.topic_key, r.asset_key,
           r.relation_type, r.direction, r.strength, r.confidence,
           r.horizon, r.method, r.rationale, r.evidence_json,
           r.created_at
    FROM event_relation_types rt
    CROSS JOIN event_source_ids esi
    CROSS JOIN relations AS r INDEXED BY idx_relation_source
    WHERE r.source_type=rt.source_type AND r.source_id=esi.source_id
    ORDER BY r.created_at DESC, r.id DESC
"""


def query_market_validation_relations(
    *,
    now: datetime | str | None = None,
    event_max_age_hours: int = 14 * 24,
) -> list[dict[str, Any]]:
    current = (
        _parse_utc_datetime(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        raise ValueError("now must include a timezone")
    maximum_age = max(1, min(int(event_max_age_hours), 24 * 365))
    current_epoch = int(current.timestamp())
    cutoff_epoch = current_epoch - maximum_age * 3600
    with conn() as c:
        rows = c.execute(
            _MARKET_VALIDATION_RELATIONS_SQL,
            (cutoff_epoch, current_epoch),
        ).fetchall()
    return [dict(row) for row in rows]


# ─── Reads ─────────────────────────────────────────────

def _current_event_ai_sql(sighting_alias: str) -> str:
    identity = sighting_alias
    return (
        "ai.status='ready' AND event_ai_cache_current("
        "ai.input_hash, ai.prompt_version, ai.model, "
        f"{identity}.title, {identity}.snippet, {identity}.source, "
        f"{identity}.kol_name_cn, {identity}.kol_name, {identity}.tickers, "
        f"{identity}.source_url"
        ")=1"
    )


def _event_ai_select(sighting_alias: str) -> str:
    current = _current_event_ai_sql(sighting_alias)
    return (
        "ai.status AS ai_status, ai.input_hash AS ai_input_hash, "
        "ai.prompt_version AS ai_prompt_version, "
        f"CASE WHEN {current} THEN 1 ELSE 0 END AS ai_cache_current, "
        "ai.headline_zh AS ai_headline_zh, "
        "ai.summary_zh AS ai_summary_zh, "
        "ai.why_it_matters_zh AS ai_why_it_matters_zh, "
        "ai.impact_level AS ai_impact_level, "
        "ai.impact_path_json AS ai_impact_path_json, "
        "ai.tags_json AS ai_tags_json, ai.assets_json AS ai_assets_json, "
        "ai.cluster_key AS ai_cluster_key, ai.language AS ai_language, "
        "ai.confidence AS ai_confidence, "
        "ai.evidence_basis AS ai_evidence_basis, ai.model AS ai_model, "
        "ai.generated_at AS ai_generated_at"
    )


# The public score combines deterministic rules with the model assessment.
# Rules retain veto power over high-risk events, while the model may promote a
# signal or demote a medium rule hit only when it has enough evidence.  Stored
# historical impact is deliberately ignored: the contextual classifier repairs
# old ambiguous-word false positives without rewriting rows or invalidating AI
# caches. A title-only enrichment is capped below this threshold upstream.
def _public_impact_sql(sighting_alias: str) -> str:
    rule = _event_rule_impact_sql(sighting_alias)
    current_ai = _current_event_ai_sql(sighting_alias)
    return (
        "CASE "
        f"WHEN {rule}='high' THEN 'high' "
        f"WHEN {current_ai} AND ai.impact_level='high' "
        "AND ai.confidence>=0.65 THEN 'high' "
        f"WHEN {rule}='medium' AND {current_ai} "
        "AND ai.impact_level='none' AND ai.confidence>=0.65 THEN 'low' "
        f"WHEN {rule}='medium' THEN 'medium' "
        f"WHEN {current_ai} AND ai.impact_level='medium' "
        "AND ai.confidence>=0.65 THEN 'medium' "
        "ELSE 'low' END"
    )


def _event_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    status = str(item.pop("ai_status", "") or "")
    cache_current = bool(item.pop("ai_cache_current", 0))
    if status == "ready" and not cache_current:
        status = "pending"
        if "rule_impact" in item:
            item["impact"] = item["rule_impact"]
    if status != "ready":
        item["ai_enrichment"] = None
        item["ai_status"] = status or "pending"
        for key in tuple(item):
            if key.startswith("ai_") and key not in {
                "ai_status",
                "ai_enrichment",
                "ai_request_eligible",
            }:
                item.pop(key, None)
        return item

    def decode_list(key: str) -> list[Any]:
        raw = item.pop(key, "[]") or "[]"
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    enrichment = {
        "status": "ready",
        "headline_zh": item.pop("ai_headline_zh", "") or "",
        "summary_zh": item.pop("ai_summary_zh", "") or "",
        "why_it_matters_zh": item.pop("ai_why_it_matters_zh", "") or "",
        "impact_level": item.pop("ai_impact_level", "unknown") or "unknown",
        "impact_path": decode_list("ai_impact_path_json"),
        "tags": decode_list("ai_tags_json"),
        "assets": decode_list("ai_assets_json"),
        "cluster_key": item.pop("ai_cluster_key", "") or "",
        "language": item.pop("ai_language", "unknown") or "unknown",
        "confidence": item.pop("ai_confidence", None),
        "evidence_basis": item.pop("ai_evidence_basis", "title") or "title",
        "model": item.pop("ai_model", "") or "",
        "generated_at": item.pop("ai_generated_at", None),
    }
    item.pop("ai_input_hash", None)
    item.pop("ai_prompt_version", None)
    item["ai_status"] = "ready"
    item["ai_enrichment"] = enrichment
    return item

def query_events(
    *,
    kol: str | None = None,
    kols: Iterable[str] | None = None,
    hours: int | None = None,
    impact: str | None = None,
    q: str | None = None,
    time_status: str = "verified",
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
    use_ai_impact: bool = False,
) -> list[dict[str, Any]]:
    if time_status not in {"verified", "unverified"}:
        raise ValueError("time_status must be 'verified' or 'unverified'")
    current = (
        _parse_utc_datetime(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        raise ValueError("now must include a timezone")
    selected_kols: list[str] = []
    selected_kol_set: set[str] = set()
    raw_kols: Iterable[str]
    if isinstance(kols, str):
        raw_kols = (kols,)
    else:
        raw_kols = kols or ()
    for raw_key in ((kol,) if kol else ()):
        if raw_key not in selected_kol_set:
            selected_kol_set.add(raw_key)
            selected_kols.append(raw_key)
    for raw_key in raw_kols:
        key = str(raw_key)
        if not key or key in selected_kol_set:
            continue
        selected_kol_set.add(key)
        selected_kols.append(key)
    if len(selected_kols) > MAX_EVENT_KOL_FILTERS:
        raise ValueError(
            f"query_events accepts at most {MAX_EVENT_KOL_FILTERS} KOL filters"
        )
    now_epoch = int(current.timestamp())
    where: list[str] = []
    params: list[Any] = []
    rule_impact_sql = _event_rule_impact_sql("m")
    effective_impact_sql = rule_impact_sql
    if use_ai_impact:
        effective_impact_sql = _public_impact_sql("m")
    select_sql = (
        "e.id, e.url, e.canonical_url, m.title, m.snippet, m.source, "
        "m.kol_key, m.kol_name, m.kol_name_cn, "
        f"{_event_attribution_basis_sql('m')} AS attribution_basis, "
        f"{_event_matched_alias_sql('m')} AS matched_alias, "
        f"{rule_impact_sql} AS rule_impact, "
        f"{effective_impact_sql} AS impact, "
        f"{_event_finance_sql('m')} AS has_market_kw, "
        f"m.tickers, {_event_eligible_source_count_sql()} AS source_count, "
        "e.fetched_at, m.first_seen_at, "
        "m.last_seen_at, m.published_at, "
        "m.published_at_status AS time_status, m.published_at_epoch, "
        "m.source_url, m.id AS sighting_id, "
        f"CASE WHEN m.id={_preferred_event_sighting_id_sql('preferred_ai')} "
        "THEN 1 ELSE 0 END AS ai_request_eligible, "
        f"{_event_ai_select('m')}"
    )
    sighting_status = (
        "s.published_at_status='verified'"
        if time_status == "verified"
        else "s.published_at_status IN ('unknown', 'future')"
    )
    sighting_order = _preferred_sighting_order_sql("s")
    selected_sighting_filter = ""
    if selected_kols:
        selected_sighting_filter = (
            "AND s.kol_key IN ("
            + ",".join("?" for _ in selected_kols)
            + ") "
        )
        params.extend(selected_kols)
    sighting_window_filter = ""
    if hours:
        cutoff = now_epoch - int(hours) * 3600
        sighting_time_sql = (
            "s.published_at_epoch"
            if time_status == "verified"
            else "CAST(strftime('%s', s.first_seen_at) AS INTEGER)"
        )
        sighting_window_filter = f"AND {sighting_time_sql} >= ? "
        params.append(cutoff)
    from_sql = (
        "events e JOIN event_sightings m ON m.id=("
        "SELECT s.id FROM event_sightings s "
        f"WHERE s.event_id=e.id {selected_sighting_filter}"
        f"AND {sighting_status} {sighting_window_filter}"
        f"AND {_event_intelligence_sql('s')} "
        f"ORDER BY {sighting_order} LIMIT 1) "
        "LEFT JOIN event_enrichments ai ON ai.event_id=e.id"
    )

    # Match the exact source/URL semantics returned to the caller. In a KOL
    # view the source comes from the selected sighting, while the canonical URL
    # and content still belong to the merged event. Keeping this predicate in
    # SQL prevents an ineligible sighting from consuming a pagination slot.
    where.append(_event_intelligence_sql("m"))

    published_status_sql = "m.published_at_status"
    published_epoch_sql = "m.published_at_epoch"
    collected_sql = "m.first_seen_at"
    collected_epoch_sql = f"CAST(strftime('%s', {collected_sql}) AS INTEGER)"

    if time_status == "verified":
        where.append(f"{published_status_sql}='verified'")
    else:
        where.append(f"{published_status_sql} IN ('unknown', 'future')")

    if impact:
        if impact == "high+":
            where.append(f"{effective_impact_sql} IN ('high', 'medium')")
        else:
            where.append(f"{effective_impact_sql} = ?")
            params.append(impact)
    if q:
        where.append(
            "(m.title LIKE ? OR IFNULL(m.snippet,'') LIKE ? "
            f"OR (({_current_event_ai_sql('m')}) AND ("
            "IFNULL(ai.headline_zh,'') LIKE ? "
            "OR IFNULL(ai.summary_zh,'') LIKE ? "
            "OR IFNULL(ai.tags_json,'') LIKE ?)))"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
      SELECT {select_sql}
      FROM {from_sql}
      {where_sql}
      ORDER BY {
        published_epoch_sql
        if time_status == "verified"
        else collected_epoch_sql
      } DESC, e.id DESC
      LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [item for row in rows if (item := _event_row(row)) is not None]


def get_event_detail(event_id: int) -> dict[str, Any] | None:
    """Return a canonical event, eligible sightings and same-cluster stories."""
    with conn() as c:
        row = c.execute(
            f"""
            SELECT e.id, e.url, e.canonical_url, m.title, m.snippet, m.source,
                   m.kol_key, m.kol_name, m.kol_name_cn,
                   {_event_attribution_basis_sql('m')} AS attribution_basis,
                   {_event_matched_alias_sql('m')} AS matched_alias,
                   {_event_rule_impact_sql('m')} AS rule_impact,
                   {_public_impact_sql('m')} AS impact,
                   {_event_finance_sql('m')} AS has_market_kw,
                   m.tickers,
                   {_event_eligible_source_count_sql()} AS source_count,
                   e.fetched_at,
                   m.first_seen_at, m.last_seen_at, m.published_at,
                   m.published_at_status AS time_status,
                   m.published_at_epoch, m.source_url,
                   m.id AS sighting_id,
                   1 AS ai_request_eligible,
                   {_event_ai_select('m')}
            FROM events e
            JOIN event_sightings m ON m.id=(
              SELECT s.id FROM event_sightings s
              WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
              ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1
            )
            LEFT JOIN event_enrichments ai ON ai.event_id=e.id
            WHERE e.id=?
            """,
            (int(event_id),),
        ).fetchone()
        event = _event_row(row)
        if event is None:
            return None
        sightings = [
            dict(item)
            for item in c.execute(
                f"""
                SELECT s.id AS sighting_id, s.title, s.snippet,
                       s.kol_key, s.kol_name,
                       s.kol_name_cn, s.source, s.source_url,
                       {_event_attribution_basis_sql('s')} AS attribution_basis,
                       {_event_matched_alias_sql('s')} AS matched_alias,
                       {_event_rule_impact_sql('s')} AS rule_impact,
                       {_public_impact_sql('s')} AS impact,
                       {_event_finance_sql('s')} AS has_market_kw, s.tickers,
                       s.published_at,
                       s.published_at_status AS time_status,
                       s.first_seen_at, s.last_seen_at, s.source_count
                FROM event_sightings s
                JOIN events e ON e.id=s.event_id
                LEFT JOIN event_enrichments ai ON ai.event_id=e.id
                WHERE s.event_id=? AND {_event_intelligence_sql('s')}
                ORDER BY {_preferred_sighting_order_sql('s')}
                """,
                (int(event_id),),
            ).fetchall()
        ]
        cluster_key = str(
            (event.get("ai_enrichment") or {}).get("cluster_key") or ""
        )
        related: list[dict[str, Any]] = []
        if cluster_key:
            related = [
                dict(item)
                for item in c.execute(
                    f"""
                    SELECT e.id, m.title, m.source, m.kol_name_cn,
                           m.source_url, e.canonical_url, e.url, m.published_at,
                           ai.headline_zh, ai.summary_zh
                    FROM event_enrichments ai
                    JOIN events e ON e.id=ai.event_id
                    JOIN event_sightings m ON m.id=(
                      SELECT s.id FROM event_sightings s
                      WHERE s.event_id=e.id AND {_event_intelligence_sql('s')}
                      ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1
                    )
                    WHERE {_current_event_ai_sql('m')}
                      AND ai.cluster_key=? AND e.id<>?
                      AND m.published_at_status='verified'
                    ORDER BY COALESCE(m.published_at_epoch,
                      CAST(strftime('%s', m.first_seen_at) AS INTEGER)) DESC
                    LIMIT 12
                    """,
                    (cluster_key, int(event_id)),
                ).fetchall()
            ]
    return {"event": event, "sightings": sightings, "related": related}


def event_exists(event_id: int) -> bool:
    """Return whether an event row exists, independent of public eligibility."""
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM events WHERE id=? LIMIT 1",
            (int(event_id),),
        ).fetchone()
    return row is not None


def list_kols() -> list[dict[str, Any]]:
    """Return the configured KOL directory overlaid with observed summaries."""
    with conn() as c:
        rows = c.execute(
            f"""
            WITH ranked_sightings AS (
              SELECT s.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY s.event_id, s.kol_key
                       ORDER BY {_preferred_sighting_order_sql('s')}
                     ) AS source_rank
              FROM event_sightings s
              JOIN events e ON e.id=s.event_id
              WHERE {_event_intelligence_sql('s')}
            ), per_event AS (
              SELECT event_id,
                     title,
                     snippet,
                     tickers,
                     kol_key,
                     kol_name,
                     kol_name_cn,
                     source,
                     source_url,
                     last_seen_at,
                     published_at,
                     published_at_epoch
              FROM ranked_sightings
              WHERE source_rank=1
            )
            SELECT p.kol_key,
                   MAX(p.kol_name) AS kol_name,
                   MAX(p.kol_name_cn) AS kol_name_cn,
                   COUNT(*) AS total,
                   MAX(e.id) AS last_id,
                   MAX(p.last_seen_at) AS last_fetched,
                   MAX(p.published_at) AS last_published,
                   SUM(CASE WHEN {_public_impact_sql('p')}='high'
                     THEN 1 ELSE 0 END) AS high_total,
                   SUM(CASE WHEN {_public_impact_sql('p')}='medium'
                     THEN 1 ELSE 0 END) AS medium_total,
                   0 AS high_24h,
                   0 AS total_24h
            FROM per_event p
            JOIN events e ON e.id=p.event_id
            LEFT JOIN event_enrichments ai ON ai.event_id=e.id
            WHERE {_event_intelligence_sql('p')}
            GROUP BY p.kol_key
            ORDER BY total_24h DESC, total DESC
            """
        ).fetchall()
        observed_rows = c.execute(
            """
            WITH observed AS (
              SELECT s.kol_key, s.kol_name, s.kol_name_cn,
                     ROW_NUMBER() OVER (
                       PARTITION BY s.kol_key
                       ORDER BY s.last_seen_at DESC, s.id DESC
                     ) AS observed_rank
              FROM event_sightings s
              WHERE NULLIF(TRIM(s.kol_key), '') IS NOT NULL
            )
            SELECT kol_key, kol_name, kol_name_cn
            FROM observed
            WHERE observed_rank=1
            """
        ).fetchall()
        recent_rows = c.execute(
            f"""
            WITH ranked_recent AS (
              SELECT s.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY s.event_id, s.kol_key
                       ORDER BY {_preferred_sighting_order_sql('s')}
                     ) AS source_rank
              FROM event_sightings s
              JOIN events e ON e.id=s.event_id
              WHERE s.published_at_status='verified'
                AND s.published_at_epoch >= CAST(
                  strftime('%s','now','-24 hours') AS INTEGER
                )
                AND {_event_intelligence_sql('s')}
            ), per_recent_event AS (
              SELECT * FROM ranked_recent WHERE source_rank=1
            )
            SELECT p.kol_key,
                   COUNT(*) AS total_24h,
                   SUM(CASE WHEN {_public_impact_sql('p')}='high'
                     THEN 1 ELSE 0 END) AS high_24h
            FROM per_recent_event p
            JOIN events e ON e.id=p.event_id
            LEFT JOIN event_enrichments ai ON ai.event_id=e.id
            GROUP BY p.kol_key
            """
        ).fetchall()
    recent_by_key = {
        str(row["kol_key"] or ""): {
            "total_24h": int(row["total_24h"] or 0),
            "high_24h": int(row["high_24h"] or 0),
        }
        for row in recent_rows
    }
    summaries: list[dict[str, Any]] = []
    observed_keys: set[str] = set()
    for row in rows:
        item = dict(row)
        key = str(item.get("kol_key") or "")
        observed_keys.add(key)
        configured = KOL_DIRECTORY.get(key)
        item["configured"] = configured is not None
        if configured is not None:
            item["kol_name"] = configured["name"]
            item["kol_name_cn"] = configured["name_cn"]
            item["category"] = configured["category"]
        else:
            item["category"] = "other"
        item.update(recent_by_key.get(key, {}))
        summaries.append(item)

    empty_summary = {
        "total": 0,
        "last_id": None,
        "last_fetched": None,
        "last_published": None,
        "high_total": 0,
        "medium_total": 0,
        "high_24h": 0,
        "total_24h": 0,
    }
    for row in observed_rows:
        key = str(row["kol_key"] or "")
        if not key or key in observed_keys or key in KOL_DIRECTORY:
            continue
        summaries.append(
            {
                "kol_key": key,
                "kol_name": str(row["kol_name"] or key),
                "kol_name_cn": str(row["kol_name_cn"] or row["kol_name"] or key),
                "category": "other",
                "configured": False,
                **empty_summary,
            }
        )
        observed_keys.add(key)
    for key, configured in KOL_DIRECTORY.items():
        if key in observed_keys:
            continue
        summaries.append(
            {
                "kol_key": key,
                "kol_name": configured["name"],
                "kol_name_cn": configured["name_cn"],
                "category": configured["category"],
                "configured": True,
                **empty_summary,
            }
        )
    return summaries


def stats(hours: int = 24) -> dict[str, Any]:
    hours = int(hours)
    if hours < 0:
        raise ValueError("hours must be non-negative")
    modifier = f"-{hours} hours"
    with conn() as c:
        def count(cond: str = "") -> int:
            return c.execute(
                "SELECT COUNT(*) n FROM events e "
                "JOIN event_sightings m ON m.id=("
                "SELECT s.id FROM event_sightings s "
                "WHERE s.event_id=e.id AND s.published_at_status='verified' "
                "AND s.published_at_epoch >= "
                "CAST(strftime('%s','now',?) AS INTEGER) "
                f"AND {_event_intelligence_sql('s')} "
                f"ORDER BY {_preferred_sighting_order_sql('s')} LIMIT 1) "
                "LEFT JOIN event_enrichments ai ON ai.event_id=e.id "
                f"WHERE {_event_intelligence_sql('m')} "
                f"{cond}",
                (modifier,),
            ).fetchone()["n"]

        total = count()
        high = count(f"AND {_public_impact_sql('m')}='high'")
        med = count(f"AND {_public_impact_sql('m')}='medium'")
        with_market = count(f"AND {_event_finance_sql('m')}=1")
        active_kols = c.execute(
            "SELECT COUNT(DISTINCT s.kol_key) n FROM event_sightings s "
            "JOIN events e ON e.id=s.event_id "
            "WHERE s.published_at_status='verified' "
            f"AND {_event_intelligence_sql('s')} "
            "AND s.published_at_epoch >= "
            "CAST(strftime('%s','now',?) AS INTEGER)",
            (modifier,),
        ).fetchone()["n"]
    return {
        "hours": hours,
        "total": total,
        "high": high,
        "medium": med,
        "low": max(0, total - high - med),
        "with_market_kw": with_market,
        "active_kols": active_kols,
    }


def latest_macro() -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            "SELECT id, created_at, payload FROM macro_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    payload["snapshot_id"] = row["id"]
    payload["created_at"] = row["created_at"]
    return payload


def save_decision_snapshot(
    *,
    schema_version: int,
    engine_version: str,
    source_hash: str,
    source_as_of: str | None,
    generated_at: str,
    summary: dict[str, Any],
    full: dict[str, Any],
    keep: int = 48,
) -> int:
    """Atomically save a public decision snapshot and prune old versions."""
    schema = _bounded_integer(
        schema_version,
        "schema_version",
        maximum=1_000_000,
    )
    engine = _required_text(engine_version, "engine_version", maximum=80)
    digest = _required_text(source_hash, "source_hash", maximum=128)
    created = _observed_at(generated_at)
    source_time = (
        _observed_at(source_as_of) if source_as_of is not None else None
    )
    if not isinstance(summary, dict) or not isinstance(full, dict):
        raise ValueError("decision snapshot payloads must be objects")
    summary_json = json.dumps(
        summary,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    full_json = json.dumps(
        full,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    retain = max(2, min(int(keep), 720))
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            INSERT INTO decision_snapshots (
              schema_version, engine_version, source_hash, source_as_of,
              generated_at, summary_json, full_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schema,
                engine,
                digest,
                source_time,
                created,
                summary_json,
                full_json,
            ),
        )
        snapshot_id = cur.lastrowid
        if snapshot_id is None:
            raise sqlite3.IntegrityError("decision snapshot insert returned no id")
        c.execute(
            """
            DELETE FROM decision_snapshots
            WHERE id IN (
              SELECT id FROM decision_snapshots
              ORDER BY id DESC LIMIT -1 OFFSET ?
            )
            """,
            (retain,),
        )
    return int(snapshot_id)


def _decision_snapshot_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        summary = json.loads(row["summary_json"])
        full = json.loads(row["full_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(summary, dict) or not isinstance(full, dict):
        return None
    return {
        "snapshot_id": int(row["id"]),
        "schema_version": int(row["schema_version"]),
        "engine_version": row["engine_version"],
        "source_hash": row["source_hash"],
        "source_as_of": row["source_as_of"],
        "generated_at": row["generated_at"],
        "summary": summary,
        "full": full,
    }


def latest_decision_snapshot(
    *,
    schema_version: int,
    engine_version: str,
) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            """
            SELECT id, schema_version, engine_version, source_hash,
                   source_as_of, generated_at, summary_json, full_json
            FROM decision_snapshots
            WHERE schema_version=? AND engine_version=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(schema_version), str(engine_version)),
        ).fetchone()
    return _decision_snapshot_row(row)


def get_decision_snapshot(
    snapshot_id: int,
    *,
    schema_version: int,
    engine_version: str,
) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            """
            SELECT id, schema_version, engine_version, source_hash,
                   source_as_of, generated_at, summary_json, full_json
            FROM decision_snapshots
            WHERE id=? AND schema_version=? AND engine_version=?
            LIMIT 1
            """,
            (int(snapshot_id), int(schema_version), str(engine_version)),
        ).fetchone()
    return _decision_snapshot_row(row)


def macro_history(limit: int = 60) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT created_at, composite_score, composite_level "
            "FROM macro_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_meta(key: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


_LLM_OUTCOMES = {
    "started",
    "ready",
    "retry",
    "failed",
    "superseded",
    "cancelled",
    "abandoned",
}


def _telemetry_integer(value: Any, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def begin_llm_call(
    *,
    subject_type: str,
    subject_key: str,
    input_hash: str,
    prompt_version: str,
    model: str,
    attempt_count: int = 1,
    started_at: str | None = None,
) -> int:
    kind = str(subject_type or "").strip()
    if kind not in {"event", "macro_event"}:
        raise ValueError("unsupported llm subject_type")
    key = _required_text(subject_key, "subject_key", maximum=160)
    digest = _required_text(input_hash, "input_hash", maximum=128)
    prompt = _required_text(prompt_version, "prompt_version", maximum=100)
    clean_model = _required_text(model, "model", maximum=100)
    attempt = _bounded_integer(
        attempt_count,
        "attempt_count",
        minimum=1,
        maximum=1_000_000,
    )
    started = _observed_at(started_at)
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            INSERT INTO llm_call_attempts (
              provider, subject_type, subject_key, input_hash,
              prompt_version, model, attempt_count, outcome, started_at
            ) VALUES ('deepseek', ?, ?, ?, ?, ?, ?, 'started', ?)
            """,
            (kind, key, digest, prompt, clean_model, attempt, started),
        )
        call_id = cur.lastrowid
    if call_id is None:
        raise sqlite3.IntegrityError("llm telemetry insert returned no id")
    return int(call_id)


def finish_llm_call(
    call_id: int,
    *,
    outcome: str,
    latency_ms: int | None,
    usage: Any = None,
    error_code: str = "",
    http_status: int | None = None,
    completed_at: str | None = None,
) -> bool:
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in _LLM_OUTCOMES - {"started"}:
        raise ValueError("unsupported llm outcome")
    values = usage.as_dict() if hasattr(usage, "as_dict") else usage
    values = values if isinstance(values, Mapping) else {}
    prompt_tokens = _telemetry_integer(values.get("prompt_tokens"), 2_000_000)
    cache_hit = _telemetry_integer(
        values.get("prompt_cache_hit_tokens"), 2_000_000
    )
    cache_miss = _telemetry_integer(
        values.get("prompt_cache_miss_tokens"), 2_000_000
    )
    if (
        prompt_tokens is not None
        and cache_hit is not None
        and cache_miss is not None
        and cache_hit + cache_miss > prompt_tokens
    ):
        cache_hit = None
        cache_miss = None
    safe_latency = _telemetry_integer(latency_ms, 3_600_000)
    safe_status = _telemetry_integer(http_status, 599)
    if safe_status is not None and safe_status < 100:
        safe_status = None
    safe_error = re.sub(r"[^a-z0-9_:-]", "", str(error_code).lower())[:48]
    completed = _observed_at(completed_at)
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE llm_call_attempts
            SET outcome=?, error_code=?, http_status=?, completed_at=?,
                latency_ms=?, prompt_tokens=?, prompt_cache_hit_tokens=?,
                prompt_cache_miss_tokens=?, completion_tokens=?,
                reasoning_tokens=?, total_tokens=?
            WHERE id=? AND outcome='started'
            """,
            (
                clean_outcome,
                safe_error,
                safe_status,
                completed,
                safe_latency,
                prompt_tokens,
                cache_hit,
                cache_miss,
                _telemetry_integer(values.get("completion_tokens"), 1_000_000),
                _telemetry_integer(values.get("reasoning_tokens"), 1_000_000),
                _telemetry_integer(values.get("total_tokens"), 3_000_000),
                int(call_id),
            ),
        )
    return cur.rowcount == 1


def abandon_stale_llm_calls(
    *,
    older_than_seconds: int = 20 * 60,
    now: datetime | None = None,
) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (
        current - timedelta(seconds=max(60, int(older_than_seconds)))
    ).replace(microsecond=0).isoformat()
    with conn(immediate=True) as c:
        cur = c.execute(
            """
            UPDATE llm_call_attempts
            SET outcome='abandoned', error_code='worker_lease_expired',
                completed_at=?
            WHERE outcome='started' AND started_at<=?
            """,
            (current.replace(microsecond=0).isoformat(), cutoff),
        )
    return cur.rowcount


def query_llm_usage_summary(
    *,
    hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    window = max(1, min(int(hours), 24 * 365))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    cutoff_epoch = int(current.astimezone(timezone.utc).timestamp()) - window * 3600
    with conn() as c:
        row = c.execute(
            """
            SELECT COUNT(*) AS calls,
                   SUM(CASE WHEN outcome='ready' THEN 1 ELSE 0 END) AS ready,
                   SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                   SUM(COALESCE(prompt_cache_hit_tokens, 0)) AS cache_hit_tokens,
                   SUM(COALESCE(prompt_cache_miss_tokens, 0)) AS cache_miss_tokens,
                   SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                   SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                   AVG(latency_ms) AS average_latency_ms
            FROM llm_call_attempts
            WHERE CAST(strftime('%s', started_at) AS INTEGER) >= ?
            """,
            (cutoff_epoch,),
        ).fetchone()
    return {
        "hours": window,
        "calls": int(row["calls"] or 0),
        "ready": int(row["ready"] or 0),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
        "cache_miss_tokens": int(row["cache_miss_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "average_latency_ms": (
            round(float(row["average_latency_ms"]), 1)
            if row["average_latency_ms"] is not None
            else None
        ),
    }


def _prune_macro_history_in(
    c: sqlite3.Connection,
    *,
    cutoff_epoch: int,
) -> dict[str, int]:
    expired = int(
        c.execute(
            """
            SELECT COUNT(*)
            FROM macro_snapshots
            WHERE CAST(strftime('%s', created_at) AS INTEGER) < ?
            """,
            (cutoff_epoch,),
        ).fetchone()[0]
    )
    if not expired:
        return {"snapshots": 0, "relations": 0}

    # ``relations`` has a polymorphic source key rather than a foreign key.
    # Remove every edge owned by an expiring macro snapshot before deleting
    # its parent so retention cannot create permanent orphans.  Extracting the
    # prefix handles both the direct id and ``<snapshot-id>:...`` source forms
    # without confusing snapshot 12 with 120.
    c.execute(
        """
        WITH expired_macro_sources(source_id) AS MATERIALIZED (
          SELECT CAST(id AS TEXT)
          FROM macro_snapshots
          WHERE CAST(strftime('%s', created_at) AS INTEGER) < ?
        )
        DELETE FROM relations
        WHERE LOWER(TRIM(source_type))='macro_snapshot'
          AND CASE
                WHEN INSTR(source_id, ':') > 0
                THEN SUBSTR(source_id, 1, INSTR(source_id, ':') - 1)
                ELSE source_id
              END IN (SELECT source_id FROM expired_macro_sources)
        """,
        (cutoff_epoch,),
    )
    relations_deleted = int(c.execute("SELECT changes()").fetchone()[0])
    snapshots = c.execute(
        """
        DELETE FROM macro_snapshots
        WHERE CAST(strftime('%s', created_at) AS INTEGER) < ?
        """,
        (cutoff_epoch,),
    ).rowcount
    return {
        "snapshots": max(0, int(snapshots)),
        "relations": relations_deleted,
    }


def prune_macro_history(
    *,
    now: datetime | str | None = None,
    retention_days: int = MACRO_RETENTION_DAYS,
) -> dict[str, int]:
    current = (
        _parse_utc_datetime(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        raise ValueError("now must include a timezone")
    days = max(1, min(int(retention_days), 24 * 365))
    cutoff_epoch = int((current - timedelta(days=days)).timestamp())
    with conn(immediate=True) as c:
        return _prune_macro_history_in(c, cutoff_epoch=cutoff_epoch)


def prune_old() -> int:
    current = datetime.now(timezone.utc)
    with conn(immediate=True) as c:
        cur = c.execute(
            "DELETE FROM events WHERE strftime('%s', substr(COALESCE(last_seen_at, fetched_at),1,19)) < "
            f"strftime('%s','now','-{RETENTION_DAYS} days')"
        )
        _prune_macro_history_in(
            c,
            cutoff_epoch=int(
                (current - timedelta(days=MACRO_RETENTION_DAYS)).timestamp()
            ),
        )
        c.execute(
            "DELETE FROM llm_call_attempts "
            "WHERE CAST(strftime('%s', started_at) AS INTEGER) < "
            "CAST(strftime('%s','now','-90 days') AS INTEGER)"
        )
    return cur.rowcount


if __name__ == "__main__":
    init()
    print("DB initialized at", DB_PATH)
    print("dedup:", backfill_dedup())
