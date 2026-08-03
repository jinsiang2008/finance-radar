#!/usr/bin/env python3
"""Populate relations, market validation, and private portfolio snapshots."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from kol_dashboard import (
        db,
        decision_service,
        market_data,
        portfolio,
    )
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    import db
    import decision_service
    import market_data
    import portfolio


MARKET_REACTION_MAX_AGE_DAYS = 14


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


def collect_relations(
    *,
    repository: Any = db,
    now: Any = None,
) -> dict[str, int]:
    """Extract current event and latest macro relations idempotently."""
    current = _utc_datetime(now)
    events = repository.query_events(
        hours=decision_service.EVENT_RELATION_MAX_AGE_HOURS,
        time_status="verified",
        limit=1_000,
        now=current,
    )
    event_relations = decision_service.ingest_sources(
        events,
        persist=True,
        repository=repository,
        now=current,
    )
    macro = repository.latest_macro()
    macro_relations = (
        decision_service.ingest_sources(
            [],
            macro,
            persist=True,
            repository=repository,
            now=current,
        )
        if isinstance(macro, dict)
        else []
    )
    return {
        "events": len(events),
        "event_relations": len(event_relations),
        "macro_relations": len(macro_relations),
    }


def _history_bars(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    bars = payload.get("bars")
    return bars if isinstance(bars, list) else []


def collect_market_reactions(
    *,
    repository: Any = db,
    history_fetcher: Callable[..., dict[str, Any]] = market_data.fetch_yahoo_history,
    reaction_computer: Callable[..., dict[str, Any]] = market_data.compute_event_reaction,
    now: Any = None,
    max_edges: int | None = None,
) -> dict[str, int]:
    """Refresh bounded market-adjusted reactions for current event relations."""
    current = _utc_datetime(now)
    edge_limit = (
        int(os.environ.get("KOL_MARKET_MAX_EDGES", "20"))
        if max_edges is None
        else int(max_edges)
    )
    edge_limit = max(1, min(edge_limit, 200))
    events = repository.query_events(
        hours=MARKET_REACTION_MAX_AGE_DAYS * 24,
        time_status="verified",
        limit=1_000,
        now=current,
    )
    event_by_source: dict[str, dict[str, Any]] = {}
    for event in events:
        event_by_source[str(event.get("id") or "")] = event
        dedup_key = str(event.get("dedup_key") or "")
        if dedup_key:
            event_by_source[dedup_key] = event

    relations = decision_service.project_public_relations(
        repository.query_market_validation_relations(
            now=current,
            event_max_age_hours=MARKET_REACTION_MAX_AGE_DAYS * 24,
        )
    )
    existing_reactions = repository.query_market_reactions(limit=5_000)
    reactions_by_edge: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for reaction in existing_reactions:
        identity = (
            str(reaction.get("source_id") or ""),
            str(reaction.get("asset_key") or ""),
        )
        reactions_by_edge.setdefault(identity, []).append(reaction)

    def refresh_priority(relation: dict[str, Any]) -> tuple[int, str]:
        identity = (
            str(relation.get("source_id") or ""),
            str(relation.get("asset_key") or ""),
        )
        stored = reactions_by_edge.get(identity, [])
        statuses = {str(item.get("status") or "") for item in stored}
        if len(stored) >= 3 and statuses == {"complete"}:
            rank = 3
        elif "preliminary" in statuses:
            rank = 2
        elif stored:
            rank = 1
        else:
            rank = 0
        observed = min(
            (str(item.get("observed_at") or "") for item in stored),
            default="",
        )
        return rank, observed

    relations.sort(key=refresh_priority)
    history_cache: dict[str, dict[str, Any]] = {}
    processed: set[tuple[str, str]] = set()
    reaction_rows = 0
    unavailable = 0

    def history(asset_key: str) -> dict[str, Any]:
        if asset_key not in history_cache:
            result = history_fetcher(asset_key, range_="3mo", interval="1d")
            history_cache[asset_key] = result if isinstance(result, dict) else {}
            bars = _history_bars(history_cache[asset_key])
            if bars:
                repository.upsert_market_prices(
                    asset_key,
                    "yahoo",
                    bars,
                    provider_symbol=history_cache[asset_key].get("symbol"),
                    observed_at=current.isoformat(),
                )
        return history_cache[asset_key]

    for relation in relations:
        if relation.get("source_type") != "event":
            continue
        source_id = str(relation.get("source_id") or "")
        asset_key = str(relation.get("asset_key") or "")
        event = event_by_source.get(source_id)
        identity = (source_id, asset_key)
        if (
            not source_id
            or not asset_key
            or event is None
            or identity in processed
        ):
            continue
        if len(processed) >= edge_limit:
            break
        processed.add(identity)
        benchmark = market_data.benchmark_for(
            asset_key,
            relation.get("topic_key"),
        )
        if benchmark is None:
            unavailable += 1
            continue
        asset_history = history(asset_key)
        benchmark_history = history(benchmark)
        result = reaction_computer(
            _history_bars(asset_history),
            _history_bars(benchmark_history),
            event.get("published_at"),
            expected_direction=relation.get("direction"),
        )
        if str(result.get("status") or "") == "unavailable":
            unavailable += 1
        reaction_rows += repository.upsert_market_reactions(
            "event",
            source_id,
            asset_key,
            result,
            benchmark_asset_key=benchmark,
            observed_at=current.isoformat(),
        )

    return {
        "eligible": len(processed),
        "processed": len(processed) - unavailable,
        "unavailable": unavailable,
        "reaction_rows": reaction_rows,
        "price_series": len(history_cache),
    }


def collect_portfolio(
    *,
    repository: Any = db,
    holdings_loader: Callable[[], dict[str, Any]] = portfolio.load_holdings,
) -> dict[str, Any]:
    """Store the configured private ledger without exposing it publicly."""
    try:
        snapshot = holdings_loader()
    except FileNotFoundError:
        return {"available": False, "reason": "missing"}
    snapshot_id = repository.save_portfolio_snapshot(snapshot)
    return {
        "available": True,
        "snapshot_id": snapshot_id,
        "positions": len(snapshot.get("positions") or []),
    }


def run(command: str, *, repository: Any = db) -> dict[str, Any]:
    repository.init()
    summary: dict[str, Any] = {}
    if command in {"all", "relations"}:
        summary["relations"] = collect_relations(repository=repository)
    if command in {"all", "market"}:
        summary["market"] = collect_market_reactions(repository=repository)
    if command in {"all", "portfolio"}:
        summary["portfolio"] = collect_portfolio(repository=repository)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        choices=("all", "relations", "market", "portfolio"),
        default="all",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.command), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
