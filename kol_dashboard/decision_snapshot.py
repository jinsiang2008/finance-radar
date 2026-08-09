"""Versioned, last-good public decision snapshots.

The decision graph is deliberately computed by the collector, not by every
HTTP request.  Private portfolio overlays read the same public full snapshot
and remain request-local; no account data is ever written here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

if __package__:
    from kol_dashboard import db, decision_service
else:  # Flat production bundle and app.py test imports.
    import db
    import decision_service


STALE_AFTER_SECONDS = 90 * 60


def _utc_datetime(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("now must be an ISO timestamp or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return parsed.astimezone(timezone.utc)


def macro_coverage(repository: Any) -> float:
    snapshot = repository.latest_macro()
    if not isinstance(snapshot, Mapping):
        return 0.0
    coverage = snapshot.get("data_coverage")
    if not isinstance(coverage, Mapping):
        return 0.0
    available = coverage.get("available")
    total = coverage.get("total")
    if (
        isinstance(available, (int, float))
        and not isinstance(available, bool)
        and isinstance(total, (int, float))
        and not isinstance(total, bool)
        and total > 0
    ):
        return round(max(0.0, min(1.0, float(available) / float(total))), 4)
    return 0.0


def _source_hash(
    relations: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    coverage: float,
) -> str:
    stable = json.dumps(
        {
            "engine_version": decision_service.DECISION_ENGINE_VERSION,
            "coverage": coverage,
            "relations": relations,
            "reactions": reactions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def refresh_public_snapshot(
    *,
    repository: Any = db,
    now: Any = None,
) -> dict[str, Any]:
    """Compute and atomically persist one last-good public decision graph."""
    current = _utc_datetime(now)
    relations = repository.query_decision_relations(
        now=current,
        event_max_age_hours=decision_service.EVENT_RELATION_MAX_AGE_HOURS,
    )
    reactions = repository.query_market_reactions(
        limit=5_000,
        eligible_events_only=True,
    )
    coverage = macro_coverage(repository)
    full = decision_service.build_public_decisions(
        relations,
        reactions,
        coverage,
        now=current,
    )
    summary = decision_service.project_decision_summary(full)
    generated_at = current.isoformat()
    snapshot_id = repository.save_decision_snapshot(
        schema_version=decision_service.DECISION_SNAPSHOT_SCHEMA_VERSION,
        engine_version=decision_service.DECISION_ENGINE_VERSION,
        source_hash=_source_hash(relations, reactions, coverage),
        source_as_of=generated_at,
        generated_at=generated_at,
        summary=summary,
        full=full,
    )
    record = repository.get_decision_snapshot(
        snapshot_id,
        schema_version=decision_service.DECISION_SNAPSHOT_SCHEMA_VERSION,
        engine_version=decision_service.DECISION_ENGINE_VERSION,
    )
    if record is None:
        raise RuntimeError("saved decision snapshot could not be read")
    return record


def load_public_snapshot(
    *,
    repository: Any = db,
    snapshot_id: int | None = None,
) -> dict[str, Any] | None:
    arguments = {
        "schema_version": decision_service.DECISION_SNAPSHOT_SCHEMA_VERSION,
        "engine_version": decision_service.DECISION_ENGINE_VERSION,
    }
    if snapshot_id is None:
        return repository.latest_decision_snapshot(**arguments)
    return repository.get_decision_snapshot(snapshot_id, **arguments)


def ensure_public_snapshot(
    *,
    repository: Any = db,
    now: Any = None,
) -> dict[str, Any]:
    """Return the current snapshot, computing only when none exists."""
    current = load_public_snapshot(repository=repository)
    return current or refresh_public_snapshot(repository=repository, now=now)


def response_payload(
    record: Mapping[str, Any],
    *,
    kind: str,
    now: Any = None,
) -> dict[str, Any]:
    if kind not in {"summary", "full"}:
        raise ValueError("kind must be summary or full")
    payload = record.get(kind)
    if not isinstance(payload, Mapping):
        raise ValueError("decision snapshot payload is unavailable")
    current = _utc_datetime(now)
    generated = _utc_datetime(record.get("generated_at"))
    exact_age_seconds = max(0.0, (current - generated).total_seconds())
    age_seconds = int(exact_age_seconds)
    return {
        **dict(payload),
        "snapshot_id": int(record["snapshot_id"]),
        "generated_at": record.get("generated_at"),
        "source_as_of": record.get("source_as_of"),
        "age_seconds": age_seconds,
        "stale": exact_age_seconds > STALE_AFTER_SECONDS,
    }


def freshness_phase(record: Mapping[str, Any], *, now: Any = None) -> str:
    current = _utc_datetime(now)
    generated = _utc_datetime(record.get("generated_at"))
    age_seconds = max(0.0, (current - generated).total_seconds())
    return "stale" if age_seconds > STALE_AFTER_SECONDS else "fresh"


def cache_max_age(record: Mapping[str, Any], *, now: Any = None) -> int:
    """Bound public caching so a fresh response cannot cross into stale."""
    current = _utc_datetime(now)
    generated = _utc_datetime(record.get("generated_at"))
    age_seconds = max(0.0, (current - generated).total_seconds())
    if age_seconds > STALE_AFTER_SECONDS:
        return 30
    return max(0, min(30, int(STALE_AFTER_SECONDS - age_seconds)))


def etag(record: Mapping[str, Any], kind: str, *, now: Any = None) -> str:
    phase = freshness_phase(record, now=now)
    return (
        f'"decision-{int(record["snapshot_id"])}-'
        f'{str(record.get("source_hash") or "")[:16]}-{kind}-{phase}"'
    )
