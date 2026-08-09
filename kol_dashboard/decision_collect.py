#!/usr/bin/env python3
"""Populate relations, market validation, and private portfolio snapshots."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from kol_dashboard import (
        db,
        decision_snapshot,
        decision_service,
        market_data,
        portfolio,
    )
except ModuleNotFoundError:  # Flat production bundle in /opt/kol-dashboard.
    import db
    import decision_snapshot
    import decision_service
    import market_data
    import portfolio


MARKET_REACTION_MAX_AGE_DAYS = 14
MARKET_REACTION_RETRY_HOURS = 6
MARKET_REACTION_TRANSIENT_RETRY_HOURS = 1
MARKET_REACTION_UNAVAILABLE_RESERVE_RATIO = 0.33
MARKET_DEGRADED_EXIT_CODE = 3
_WINDOW_DUE_DAYS = {"1D": 1, "3D": 3, "5D": 5}
_TERMINAL_MARKET_REASONS = {
    "invalid_event_time",
    "same_proxy_as_benchmark",
    "unsupported_asset",
    "unsupported_benchmark",
}
_TRANSIENT_MARKET_REASONS = {
    "bad_payload",
    "invalid_range",
    "invalid_timeout",
    "provider_error",
    "request_failed",
}
_PENDING_MARKET_REASONS = {
    "follow_up_unavailable",
    "insufficient_follow_up",
    "window_not_due",
}


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
        hours=decision_service.EVENT_RELATION_INGEST_MAX_AGE_HOURS,
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


def _optional_utc_datetime(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return _utc_datetime(value)
    except (TypeError, ValueError):
        return None


def _window_due_at(event_time: datetime, window: str) -> datetime:
    return event_time + timedelta(days=_WINDOW_DUE_DAYS[window])


def _best_reactions_by_window(
    stored: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rank = {"complete": 3, "preliminary": 2, "pending": 1, "unavailable": 1}
    selected: dict[str, dict[str, Any]] = {}
    for item in stored:
        window = str(item.get("window") or "")
        if window not in _WINDOW_DUE_DAYS:
            continue
        current = selected.get(window)
        candidate_key = (
            rank.get(str(item.get("status") or ""), -1),
            str(item.get("observed_at") or ""),
        )
        current_key = (
            rank.get(str(current.get("status") or ""), -1),
            str(current.get("observed_at") or ""),
        ) if current else (-1, "")
        if candidate_key > current_key:
            selected[window] = item
    return selected


def _edge_due_state(
    stored: list[dict[str, Any]],
    event_time: datetime | None,
    current: datetime,
) -> tuple[datetime | None, bool]:
    """Return the next edge due time and whether it is a failed retry."""
    best = _best_reactions_by_window(stored)
    incomplete = [
        window
        for window in _WINDOW_DUE_DAYS
        if str(best.get(window, {}).get("status") or "") != "complete"
    ]
    if not incomplete:
        return None, False
    failed_retry = any(
        str(best.get(window, {}).get("status") or "") == "unavailable"
        for window in incomplete
    )
    scheduled: list[datetime] = []
    for window in incomplete:
        row = best.get(window, {})
        next_due = _optional_utc_datetime(row.get("next_due_at"))
        if next_due is not None:
            scheduled.append(next_due)
            continue
        reason = market_data.normalize_reason_code(row.get("reason_code"))
        if row and reason in _TERMINAL_MARKET_REASONS:
            continue
        if event_time is None:
            scheduled.append(current)
        else:
            scheduled.append(_window_due_at(event_time, window))
    return (min(scheduled), failed_retry) if scheduled else (None, failed_retry)


def collect_market_reactions(
    *,
    repository: Any = db,
    history_fetcher: Callable[..., dict[str, Any]] = market_data.fetch_yahoo_history,
    reaction_computer: Callable[..., dict[str, Any]] = market_data.compute_event_reaction,
    now: Any = None,
    max_edges: int | None = None,
) -> dict[str, Any]:
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
        limit=5_000,
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

    candidates: list[dict[str, Any]] = []
    future_new_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str]] = set()
    skipped_not_due = 0
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
            or identity in seen_candidates
        ):
            continue
        seen_candidates.add(identity)
        event_time = _optional_utc_datetime(event.get("published_at"))
        stored = reactions_by_edge.get(identity, [])
        due_at, failed_retry = _edge_due_state(
            stored,
            event_time,
            current,
        )
        if due_at is None:
            continue
        if due_at > current:
            skipped_not_due += 1
            if not stored:
                future_new_candidates.append(
                    {
                        "relation": relation,
                        "event": event,
                        "identity": identity,
                        "event_time": event_time,
                        "due_at": due_at,
                    }
                )
            continue
        candidates.append(
            {
                "relation": relation,
                "event": event,
                "identity": identity,
                "event_time": event_time,
                "due_at": due_at,
                "failed_retry": failed_retry,
            }
        )

    candidates.sort(key=lambda item: (item["due_at"], item["identity"]))
    retry_candidates = [item for item in candidates if item["failed_retry"]]
    other_candidates = [item for item in candidates if not item["failed_retry"]]
    retry_reserve = min(
        len(retry_candidates),
        max(1, round(edge_limit * MARKET_REACTION_UNAVAILABLE_RESERVE_RATIO)),
    )
    selected = retry_candidates[:retry_reserve]
    selected.extend(other_candidates[: max(0, edge_limit - len(selected))])
    selected_ids = {item["identity"] for item in selected}
    if len(selected) < edge_limit:
        remaining = [
            item for item in candidates if item["identity"] not in selected_ids
        ]
        selected.extend(remaining[: edge_limit - len(selected)])

    history_cache: dict[str, dict[str, Any]] = {}
    reaction_rows = 0
    unavailable = 0
    processed = 0
    pending_attempts = 0
    successful_price_series: set[str] = set()
    price_rows = 0
    reason_counts: dict[str, int] = {}

    def history(asset_key: str) -> dict[str, Any]:
        nonlocal price_rows
        if asset_key not in history_cache:
            resolution = market_data.resolve_provider_asset(asset_key, "yahoo")
            try:
                fetched = history_fetcher(asset_key, range_="3mo", interval="1d")
            except Exception:
                fetched = {
                    "status": "unavailable",
                    "provider": "yahoo",
                    "reason_code": "request_failed",
                    "reason": "request_failed",
                    "bars": [],
                }
            result = dict(fetched) if isinstance(fetched, dict) else {}
            result.setdefault("provider", resolution.get("provider") or "yahoo")
            result.setdefault("symbol", resolution.get("provider_symbol"))
            result.setdefault("price_asset_key", resolution.get("price_asset_key"))
            result.setdefault("proxy_for", resolution.get("proxy_for"))
            if not result.get("reason_code") and result.get("reason"):
                result["reason_code"] = market_data.normalize_reason_code(
                    result.get("reason")
                )
            history_cache[asset_key] = result
            bars = _history_bars(history_cache[asset_key])
            if bars:
                price_asset_key = str(
                    history_cache[asset_key].get("price_asset_key") or asset_key
                )
                price_rows += repository.upsert_market_prices(
                    price_asset_key,
                    str(history_cache[asset_key].get("provider") or "yahoo"),
                    bars,
                    provider_symbol=history_cache[asset_key].get("symbol"),
                    observed_at=current.isoformat(),
                )
                successful_price_series.add(price_asset_key)
        return history_cache[asset_key]

    def series_status(payload: dict[str, Any]) -> str:
        if _history_bars(payload):
            return "available"
        reason = market_data.normalize_reason_code(payload.get("reason_code"))
        return "unsupported" if reason == "unsupported_asset" else "unavailable"

    def schedule_result(
        result: dict[str, Any],
        *,
        event_time: datetime | None,
        provider: str,
        provider_symbol: str | None,
        proxy_for: str | None,
        asset_status: str | None,
        benchmark_status: str | None,
        fallback_reason: str,
    ) -> dict[str, Any]:
        scheduled = dict(result)
        windows = result.get("windows") if isinstance(result, dict) else None
        scheduled_windows: dict[str, dict[str, Any]] = {}
        root_reason = market_data.normalize_reason_code(
            result.get("reason_code") or result.get("reason") or fallback_reason
        )
        for label in _WINDOW_DUE_DAYS:
            raw = windows.get(label) if isinstance(windows, dict) else None
            item = dict(raw) if isinstance(raw, dict) else {
                "window": label,
                "status": "unavailable",
                "sample_count": 0,
                "data_timestamps": {},
            }
            target_due = (
                _window_due_at(event_time, label)
                if event_time is not None
                else current
            )
            status = str(item.get("status") or "unavailable")
            if status == "complete":
                item["reason_code"] = None
                item["next_due_at"] = None
            elif target_due > current:
                item["status"] = "pending"
                item["reason_code"] = "window_not_due"
                item["next_due_at"] = target_due.replace(microsecond=0).isoformat()
            else:
                raw_reason = (
                    item.get("reason_code")
                    or item.get("reason")
                    or (
                        "insufficient_follow_up"
                        if status == "unavailable"
                        and str(result.get("status") or "") == "preliminary"
                        else root_reason
                    )
                )
                reason = market_data.normalize_reason_code(raw_reason)
                if reason in _PENDING_MARKET_REASONS:
                    item["status"] = "pending"
                elif status not in {"preliminary", "pending"}:
                    item["status"] = "unavailable"
                item["reason_code"] = reason
                if reason in _TERMINAL_MARKET_REASONS:
                    item["next_due_at"] = None
                else:
                    retry_hours = (
                        MARKET_REACTION_TRANSIENT_RETRY_HOURS
                        if reason in _TRANSIENT_MARKET_REASONS
                        else MARKET_REACTION_RETRY_HOURS
                    )
                    item["next_due_at"] = (
                        current + timedelta(hours=retry_hours)
                    ).replace(microsecond=0).isoformat()
            item["provider"] = provider
            item["provider_symbol"] = provider_symbol
            item["proxy_for"] = proxy_for
            item["asset_status"] = asset_status
            item["benchmark_status"] = benchmark_status
            scheduled_windows[label] = item
        scheduled["windows"] = scheduled_windows
        scheduled["provider"] = provider
        scheduled["provider_symbol"] = provider_symbol
        scheduled["proxy_for"] = proxy_for
        scheduled["asset_status"] = asset_status
        scheduled["benchmark_status"] = benchmark_status
        scheduled["reason_code"] = root_reason
        return scheduled

    scheduled_pending_edges = 0
    for candidate in future_new_candidates:
        relation = candidate["relation"]
        event_time = candidate["event_time"]
        source_id, asset_key = candidate["identity"]
        benchmark = market_data.benchmark_for(
            asset_key,
            relation.get("topic_key"),
        )
        resolution = market_data.resolve_provider_asset(asset_key, "yahoo")
        result = market_data.unavailable_event_reaction("window_not_due")
        expected_direction = str(
            relation.get("direction") or ""
        ).strip().lower()
        result["expected_direction"] = (
            expected_direction
            if expected_direction in {"positive", "negative"}
            else None
        )
        result = schedule_result(
            result,
            event_time=event_time,
            provider="yahoo",
            provider_symbol=resolution.get("provider_symbol"),
            proxy_for=resolution.get("proxy_for"),
            asset_status=None,
            benchmark_status=None,
            fallback_reason="window_not_due",
        )
        reaction_rows += repository.upsert_market_reactions(
            "event",
            source_id,
            asset_key,
            result,
            benchmark_asset_key=benchmark,
            observed_at=current.isoformat(),
        )
        scheduled_pending_edges += 1

    for candidate in selected:
        relation = candidate["relation"]
        event = candidate["event"]
        event_time = candidate["event_time"]
        source_id, asset_key = candidate["identity"]
        benchmark = market_data.benchmark_for(
            asset_key,
            relation.get("topic_key"),
        )
        resolution = market_data.resolve_provider_asset(asset_key, "yahoo")
        if benchmark is None:
            result = market_data.unavailable_event_reaction(
                "unsupported_benchmark"
            )
            result = schedule_result(
                result,
                event_time=event_time,
                provider="yahoo",
                provider_symbol=resolution.get("provider_symbol"),
                proxy_for=resolution.get("proxy_for"),
                asset_status="unavailable",
                benchmark_status="unsupported",
                fallback_reason="unsupported_benchmark",
            )
            unavailable += 1
            reason_counts["unsupported_benchmark"] = (
                reason_counts.get("unsupported_benchmark", 0) + 1
            )
            reaction_rows += repository.upsert_market_reactions(
                "event",
                source_id,
                asset_key,
                result,
                benchmark_asset_key=None,
                observed_at=current.isoformat(),
            )
            continue
        asset_history = history(asset_key)
        benchmark_history = history(benchmark)
        asset_state = series_status(asset_history)
        benchmark_state = series_status(benchmark_history)
        asset_price_key = str(
            asset_history.get("price_asset_key") or asset_key
        )
        benchmark_price_key = str(
            benchmark_history.get("price_asset_key") or benchmark
        )
        if asset_price_key == benchmark_price_key:
            reason_code = "same_proxy_as_benchmark"
            result = market_data.unavailable_event_reaction(reason_code)
        elif asset_state != "available":
            reason_code = market_data.normalize_reason_code(
                asset_history.get("reason_code") or "asset_unavailable"
            )
            result = market_data.unavailable_event_reaction(reason_code)
        elif benchmark_state != "available":
            reason_code = market_data.normalize_reason_code(
                benchmark_history.get("reason_code") or "benchmark_unavailable"
            )
            result = market_data.unavailable_event_reaction(reason_code)
        else:
            result = reaction_computer(
                _history_bars(asset_history),
                _history_bars(benchmark_history),
                event.get("published_at"),
                expected_direction=relation.get("direction"),
            )
            reason_code = market_data.normalize_reason_code(
                result.get("reason_code") or result.get("reason")
            )
        result = schedule_result(
            result,
            event_time=event_time,
            provider=str(asset_history.get("provider") or "yahoo"),
            provider_symbol=asset_history.get("symbol"),
            proxy_for=asset_history.get("proxy_for"),
            asset_status=asset_state,
            benchmark_status=benchmark_state,
            fallback_reason=reason_code,
        )
        window_statuses = {
            str(item.get("status") or "")
            for item in result.get("windows", {}).values()
            if isinstance(item, dict)
        }
        if window_statuses.intersection({"complete", "preliminary"}):
            processed += 1
        elif "unavailable" in window_statuses:
            unavailable += 1
            reason_counts[reason_code] = reason_counts.get(reason_code, 0) + 1
        else:
            pending_attempts += 1
        reaction_rows += repository.upsert_market_reactions(
            "event",
            source_id,
            asset_key,
            result,
            benchmark_asset_key=benchmark,
            observed_at=current.isoformat(),
        )

    degraded = bool(selected and processed == 0 and unavailable > 0)
    business_status = (
        "degraded"
        if degraded
        else "ok"
        if processed
        else "pending"
        if scheduled_pending_edges or pending_attempts
        else "idle"
    )
    return {
        "eligible": len(candidates),
        "attempted": len(selected),
        "processed": processed,
        "unavailable": unavailable,
        "reaction_rows": reaction_rows,
        "price_series": len(successful_price_series),
        "price_rows": price_rows,
        "history_requests": len(history_cache),
        "skipped_not_due": skipped_not_due,
        "pending": scheduled_pending_edges + pending_attempts,
        "pending_scheduled": scheduled_pending_edges,
        "reserved_unavailable_retries": retry_reserve,
        "reason_counts": reason_counts,
        "business_status": business_status,
        "degraded": degraded,
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


def collect_snapshot(
    *,
    repository: Any = db,
    now: Any = None,
) -> dict[str, Any]:
    """Materialize the public decision graph after its inputs are current."""
    record = decision_snapshot.refresh_public_snapshot(
        repository=repository,
        now=now,
    )
    summary = record.get("summary") or {}
    return {
        "snapshot_id": record.get("snapshot_id"),
        "generated_at": record.get("generated_at"),
        "decisions": summary.get("total_decisions", 0),
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
    if command in {"all", "relations", "market", "snapshot"}:
        summary["snapshot"] = collect_snapshot(repository=repository)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        choices=("all", "relations", "market", "portfolio", "snapshot"),
        default="all",
    )
    args = parser.parse_args()
    summary = run(args.command)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    market = summary.get("market") if isinstance(summary, dict) else None
    if args.command in {"all", "market"} and isinstance(market, dict):
        if (
            market.get("degraded") is True
            and int(market.get("eligible") or 0) > 0
            and int(market.get("attempted") or 0) > 0
            and int(market.get("processed") or 0) == 0
            and int(market.get("unavailable") or 0) > 0
        ):
            return MARKET_DEGRADED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
