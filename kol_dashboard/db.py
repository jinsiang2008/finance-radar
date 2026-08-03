"""
SQLite layer for KOL dashboard.

Schema:
  events(id, dedup_key UNIQUE, url_hash, url, canonical_url, title, snippet, source,
         kol_key, kol_name, kol_name_cn,
         impact, has_market_kw, source_count, fetched_at, last_seen_at, published_at)

  event_sightings(event_id, kol_key, kol_name, kol_name_cn, source, source_url,
                  published_at, first_seen_at, last_seen_at, source_count)

  relations(source_type, source_id, topic_key, asset_key, relation_type, direction,
            strength, confidence, horizon, method, rationale, evidence_json, created_at)

  macro_snapshots(id, created_at, composite_score, composite_level, payload)

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
import sqlite3
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = os.environ.get(
    "KOL_DASHBOARD_DB", str(_DEFAULT_DATA_DIR / "kol_dashboard.db")
)

RETENTION_DAYS = 14

# Tracking params that differ per fetch and must not affect identity.
_TRACKING_PARAMS = {
    "ref", "aid", "tid", "c", "mkt", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content", "spm", "from", "src",
}


@contextmanager
def conn(*, immediate: bool = False):
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        if immediate:
            c.execute("BEGIN IMMEDIATE")
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
        SELECT id, kol_key, kol_name, kol_name_cn, source, url, canonical_url,
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
              event_id, kol_key, kol_name, kol_name_cn, source, source_url,
              published_at, published_at_status, published_at_epoch,
              first_seen_at, last_seen_at, source_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    published_at, published_status, published_epoch = _publication_metadata(
        item.get("published_at"),
        observed_at=publication_observed_at or seen_at,
    )
    c.execute(
        """
        INSERT INTO event_sightings (
          event_id, kol_key, kol_name, kol_name_cn, source, source_url,
          published_at, published_at_status, published_at_epoch,
          first_seen_at, last_seen_at, source_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(event_id, kol_key, source, source_url) DO UPDATE SET
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
            item.get("kol_key") or "unknown",
            item.get("kol_name") or "",
            item.get("kol_name_cn") or "",
            item.get("source") or "",
            source_url,
            published_at,
            published_status,
            published_epoch,
            seen_at,
            seen_at,
        ),
    )
    _sync_event_source_count(c, event_id)


def _move_sightings(c, keep_id: int, victim_id: int) -> None:
    """Move a duplicate event's sightings without losing per-KOL attribution."""
    rows = c.execute(
        """
        SELECT kol_key, kol_name, kol_name_cn, source, source_url, published_at,
               published_at_status, published_at_epoch,
               first_seen_at, last_seen_at, source_count
        FROM event_sightings WHERE event_id=?
        """,
        (victim_id,),
    ).fetchall()
    for row in rows:
        c.execute(
            """
            INSERT INTO event_sightings (
              event_id, kol_key, kol_name, kol_name_cn, source, source_url,
              published_at, published_at_status, published_at_epoch,
              first_seen_at, last_seen_at, source_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, kol_key, source, source_url) DO UPDATE SET
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
        SELECT id, event_id, kol_key, source, source_url, published_at,
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
            SELECT id, published_at, published_at_status, published_at_epoch,
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
        c.execute(
            """
            UPDATE event_sightings
            SET published_at=?, published_at_status=?, published_at_epoch=?,
                first_seen_at=MIN(first_seen_at, ?),
                last_seen_at=MAX(last_seen_at, ?),
                source_count=source_count+?
            WHERE id=?
            """,
            (
                *publication,
                row["first_seen_at"],
                row["last_seen_at"],
                row["source_count"],
                existing["id"],
            ),
        )
        c.execute("DELETE FROM event_sightings WHERE id=?", (row["id"],))


def init() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with conn(immediate=True) as c:
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
                CHECK(status IN ('preliminary', 'complete', 'unavailable')),
              sample_count INTEGER NOT NULL DEFAULT 0 CHECK(sample_count >= 0),
              data_timestamps_json TEXT NOT NULL DEFAULT '{}',
              method_version TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              UNIQUE(source_type, source_id, asset_key, window, method_version)
            )
            """
        )
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

    backfill_dedup()


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


def backfill_dedup() -> dict[str, int]:
    """Populate dedup_key/canonical_url, collapse duplicate rows, enforce uniqueness.

    Idempotent — safe to run on every startup. When the fingerprint algorithm
    changes, every row is re-keyed so newly-matching stories merge.
    """
    stats = {"rekeyed": 0, "merged": 0}
    with conn(immediate=True) as c:
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
                "SELECT id, impact, title, published_at, "
                "published_at_status, published_at_epoch, fetched_at "
                "FROM events WHERE dedup_key=?",
                (key,),
            ).fetchone()
            if not existing and pkey:
                # Same story, truncated differently by the aggregator.
                for cand in c.execute(
                    "SELECT id, impact, title, published_at, "
                    "published_at_status, published_at_epoch, fetched_at "
                    "FROM events WHERE prefix_key=?",
                    (pkey,),
                ).fetchall():
                    if is_prefix_dupe(cand["title"], title):
                        existing = cand
                        break

            if not existing:
                existing = c.execute(
                    "SELECT id, impact, title, published_at, "
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
                fuller = (
                    title
                    if len(title) > len(existing_row["title"])
                    else existing_row["title"]
                )
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
                    "impact=?, title=?, dedup_key=?, prefix_key=?, "
                    "has_market_kw=MAX(has_market_kw, ?), "
                    "tickers=COALESCE(tickers, ?), "
                    "published_at=?, published_at_status=?, "
                    "published_at_epoch=? WHERE id=?",
                    (
                        now,
                        best,
                        fuller,
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
                    "SELECT id, impact, title, published_at, "
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
    if status not in {"preliminary", "complete", "unavailable"}:
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
    )

    def execute(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO market_reactions (
              source_type, source_id, asset_key, window, benchmark_asset_key,
              asset_return, benchmark_return, abnormal_return,
              expected_direction, observed_direction, direction_confirmed,
              status, sample_count, data_timestamps_json, method_version,
              observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
              observed_at=excluded.observed_at
            WHERE
              CASE excluded.status
                WHEN 'complete' THEN 2
                WHEN 'preliminary' THEN 1
                ELSE 0
              END
              >=
              CASE market_reactions.status
                WHEN 'complete' THEN 2
                WHEN 'preliminary' THEN 1
                ELSE 0
              END
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
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("source_type", source_type),
        ("source_id", source_id),
        ("asset_key", asset_key),
        ("window", window),
    ):
        if value is not None:
            where.append(f"{column}=?")
            params.append(str(value))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 5000)))
    with conn() as c:
        rows = c.execute(
            "SELECT source_type, source_id, asset_key, window, "
            "benchmark_asset_key, asset_return, benchmark_return, "
            "abnormal_return, expected_direction, observed_direction, "
            "direction_confirmed, status, sample_count, "
            "data_timestamps_json, method_version, observed_at "
            f"FROM market_reactions{where_sql} "
            "ORDER BY observed_at DESC, id DESC LIMIT ?",
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
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("source_type", source_type),
        ("source_id", source_id),
        ("topic_key", topic_key),
        ("asset_key", asset_key),
        ("relation_type", relation_type),
    ):
        if value is not None:
            where.append(f"{column}=?")
            params.append(str(value))
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with conn() as c:
        rows = c.execute(
            "SELECT source_type, source_id, topic_key, asset_key, relation_type, "
            "direction, strength, confidence, horizon, method, rationale, "
            f"evidence_json, created_at FROM relations{where_sql} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


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
            """
            SELECT r.source_type, r.source_id, r.topic_key, r.asset_key,
                   r.relation_type, r.direction, r.strength, r.confidence,
                   r.horizon, r.method, r.rationale, r.evidence_json,
                   r.created_at
            FROM relations r
            LEFT JOIN events e
              ON LOWER(TRIM(r.source_type))='event'
             AND (
               r.source_id=CAST(e.id AS TEXT)
               OR r.source_id=e.dedup_key
             )
            WHERE (
                 LOWER(TRIM(r.source_type))='macro_snapshot'
                 AND (
                   r.source_id=CAST(
                     (SELECT MAX(id) FROM macro_snapshots) AS TEXT
                   )
                   OR r.source_id LIKE (
                     CAST((SELECT MAX(id) FROM macro_snapshots) AS TEXT)
                     || ':%'
                   )
                 )
               )
               OR (
                 LOWER(TRIM(r.source_type))='event'
                 AND e.id IS NOT NULL
                 AND e.published_at_status='verified'
                 AND e.published_at_epoch BETWEEN ? AND ?
               )
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (cutoff_epoch, current_epoch),
        ).fetchall()
    return [dict(row) for row in rows]


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
            """
            SELECT r.source_type, r.source_id, r.topic_key, r.asset_key,
                   r.relation_type, r.direction, r.strength, r.confidence,
                   r.horizon, r.method, r.rationale, r.evidence_json,
                   r.created_at
            FROM relations r
            JOIN events e
              ON LOWER(TRIM(r.source_type))='event'
             AND (
               r.source_id=CAST(e.id AS TEXT)
               OR r.source_id=e.dedup_key
             )
            WHERE e.published_at_status='verified'
              AND e.published_at_epoch BETWEEN ? AND ?
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (cutoff_epoch, current_epoch),
        ).fetchall()
    return [dict(row) for row in rows]


# ─── Reads ─────────────────────────────────────────────

def query_events(
    *,
    kol: str | None = None,
    hours: int | None = None,
    impact: str | None = None,
    q: str | None = None,
    time_status: str = "verified",
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
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
    now_epoch = int(current.timestamp())
    where: list[str] = []
    params: list[Any] = []
    select_sql = (
        "e.id, e.url, e.canonical_url, e.title, e.snippet, e.source, "
        "e.kol_key, e.kol_name, e.kol_name_cn, e.impact, e.has_market_kw, "
        "e.tickers, e.source_count, e.fetched_at, "
        "e.fetched_at AS first_seen_at, e.last_seen_at, e.published_at, "
        "e.published_at_status AS time_status, e.published_at_epoch"
    )
    from_sql = "events e"
    if kol:
        select_sql = (
            "e.id, e.url, e.canonical_url, e.title, e.snippet, m.source, "
            "m.kol_key, m.kol_name, m.kol_name_cn, e.impact, e.has_market_kw, "
            "e.tickers, e.source_count, e.fetched_at, m.first_seen_at, "
            "m.last_seen_at, m.published_at, "
            "m.published_at_status AS time_status, m.published_at_epoch, "
            "m.source_url"
        )
        sighting_status = (
            "s.published_at_status='verified'"
            if time_status == "verified"
            else "s.published_at_status IN ('unknown', 'future')"
        )
        sighting_order = (
            "s.published_at_epoch DESC"
            if time_status == "verified"
            else "s.first_seen_at DESC"
        )
        from_sql = (
            "events e JOIN event_sightings m ON m.id=("
            "SELECT s.id FROM event_sightings s "
            f"WHERE s.event_id=e.id AND s.kol_key=? AND {sighting_status} "
            f"ORDER BY {sighting_order}, s.last_seen_at DESC, "
            "s.id DESC LIMIT 1)"
        )
        params.append(kol)

    published_status_sql = (
        "m.published_at_status" if kol else "e.published_at_status"
    )
    published_epoch_sql = (
        "m.published_at_epoch" if kol else "e.published_at_epoch"
    )
    collected_sql = "m.first_seen_at" if kol else "e.fetched_at"
    collected_epoch_sql = f"CAST(strftime('%s', {collected_sql}) AS INTEGER)"

    if time_status == "verified":
        where.append(f"{published_status_sql}='verified'")
    else:
        where.append(f"{published_status_sql} IN ('unknown', 'future')")

    if hours:
        cutoff = now_epoch - int(hours) * 3600
        where.append(
            f"{published_epoch_sql if time_status == 'verified' else collected_epoch_sql} >= ?"
        )
        params.append(cutoff)
    if impact:
        if impact == "high+":
            where.append("e.impact IN ('high', 'medium')")
        else:
            where.append("e.impact = ?")
            params.append(impact)
    if q:
        where.append("(e.title LIKE ? OR IFNULL(e.snippet,'') LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

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
    return [dict(row) for row in rows]


def list_kols() -> list[dict[str, Any]]:
    """Per-KOL summary: distinct stories, last sighting, high/medium counts in 24h."""
    with conn() as c:
        rows = c.execute(
            """
            WITH per_event AS (
              SELECT event_id,
                     kol_key,
                     MAX(kol_name) AS kol_name,
                     MAX(kol_name_cn) AS kol_name_cn,
                     MAX(last_seen_at) AS last_seen_at,
                     MAX(
                       CASE WHEN published_at_status='verified'
                         THEN published_at END
                     ) AS published_at,
                     MAX(
                       CASE WHEN published_at_status='verified'
                         THEN published_at_epoch END
                     ) AS published_at_epoch
              FROM event_sightings
              GROUP BY event_id, kol_key
            )
            SELECT p.kol_key,
                   MAX(p.kol_name) AS kol_name,
                   MAX(p.kol_name_cn) AS kol_name_cn,
                   COUNT(*) AS total,
                   MAX(e.id) AS last_id,
                   MAX(p.last_seen_at) AS last_fetched,
                   MAX(p.published_at) AS last_published,
                   SUM(CASE WHEN e.impact='high' THEN 1 ELSE 0 END) AS high_total,
                   SUM(CASE WHEN e.impact='medium' THEN 1 ELSE 0 END) AS medium_total,
                   SUM(CASE WHEN e.impact='high' AND p.published_at_epoch >= CAST(strftime('%s','now','-24 hours') AS INTEGER) THEN 1 ELSE 0 END) AS high_24h,
                   SUM(CASE WHEN p.published_at_epoch >= CAST(strftime('%s','now','-24 hours') AS INTEGER) THEN 1 ELSE 0 END) AS total_24h
            FROM per_event p
            JOIN events e ON e.id=p.event_id
            GROUP BY p.kol_key
            ORDER BY total_24h DESC, total DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def stats(hours: int = 24) -> dict[str, Any]:
    hours = int(hours)
    if hours < 0:
        raise ValueError("hours must be non-negative")
    modifier = f"-{hours} hours"
    win = (
        "AND published_at_status='verified' "
        "AND published_at_epoch >= CAST(strftime('%s','now',?) AS INTEGER)"
    )
    with conn() as c:
        def count(cond: str = "") -> int:
            return c.execute(
                f"SELECT COUNT(*) n FROM events WHERE 1=1 {cond} {win}",
                (modifier,),
            ).fetchone()["n"]

        total = count()
        high = count("AND impact='high'")
        med = count("AND impact='medium'")
        with_market = count("AND has_market_kw=1")
        active_kols = c.execute(
            "SELECT COUNT(DISTINCT s.kol_key) n FROM event_sightings s "
            "WHERE s.published_at_status='verified' "
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


def prune_old() -> int:
    with conn() as c:
        cur = c.execute(
            "DELETE FROM events WHERE strftime('%s', substr(COALESCE(last_seen_at, fetched_at),1,19)) < "
            f"strftime('%s','now','-{RETENTION_DAYS} days')"
        )
        c.execute(
            "DELETE FROM macro_snapshots WHERE strftime('%s', substr(created_at,1,19)) < "
            "strftime('%s','now','-90 days')"
        )
    return cur.rowcount


if __name__ == "__main__":
    init()
    print("DB initialized at", DB_PATH)
    print("dedup:", backfill_dedup())
