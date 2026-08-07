#!/usr/bin/env python3
"""Background-only event enrichment worker.

The worker claims SQLite rows before network I/O, calls DeepSeek outside the
transaction, and stores only validated structured output or a bounded error
code.  It is safe to run repeatedly and inexpensive when the cache is warm.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Mapping

import db
import llm_enrichment


def _default_limit() -> int:
    try:
        value = int(os.environ.get("KOL_ENRICHMENT_BATCH_LIMIT", "18"))
    except ValueError:
        value = 18
    return max(1, min(value, 200))


def _default_macro_limit() -> int:
    try:
        value = int(os.environ.get("KOL_MACRO_ENRICHMENT_BATCH_LIMIT", "24"))
    except ValueError:
        value = 24
    return max(0, min(value, 24))


_PROVIDER_WIDE_ERRORS = {
    "authentication",
    "balance",
    "invalid_request",
    "network",
    "provider_error",
    "provider_unavailable",
    "rate_limit",
}


def run(
    *,
    limit: int,
    max_age_hours: int,
    macro_limit: int | None = None,
) -> tuple[dict[str, int | bool], bool]:
    db.init()
    safe_macro_limit = (
        _default_macro_limit()
        if macro_limit is None
        else max(0, min(int(macro_limit), 24))
    )
    config = llm_enrichment.load_config()
    if config is None:
        return {
            "configured": False,
            "processed": 0,
            "ready": 0,
            "retry": 0,
            "failed": 0,
            "skipped_noncontent": 0,
            "macro_processed": 0,
            "macro_ready": 0,
            "macro_retry": 0,
            "macro_failed": 0,
            "macro_superseded": 0,
        }, False

    counts: Counter[str] = Counter()
    stopped = False

    snapshot = db.latest_macro()
    monitored_events = (
        snapshot.get("monitored_events")
        if isinstance(snapshot, Mapping)
        else None
    )
    if isinstance(monitored_events, list):
        for event in monitored_events[:24]:
            if counts["macro_processed"] >= safe_macro_limit:
                break
            if not isinstance(event, Mapping):
                continue
            event_key = llm_enrichment.macro_event_key(event)
            event_input, input_hash = llm_enrichment.build_macro_event_input(event)
            claim = db.claim_macro_event_enrichment(
                event_key,
                input_hash=input_hash,
                prompt_version=llm_enrichment.MACRO_PROMPT_VERSION,
                model=config.model,
                evidence_basis=event_input["evidence_basis"],
            )
            if claim is None:
                continue
            claim_token, attempt_count = claim
            counts["macro_processed"] += 1
            try:
                result = llm_enrichment.enrich_event(
                    event_input,
                    input_hash=input_hash,
                    config=config,
                )
            except llm_enrichment.EnrichmentError as exc:
                retry_after = exc.retry_after_seconds
                if exc.code == "invalid_output" and attempt_count >= 3:
                    retry_after = None
                db.fail_macro_event_enrichment(
                    event_key,
                    input_hash=input_hash,
                    prompt_version=llm_enrichment.MACRO_PROMPT_VERSION,
                    model=config.model,
                    claim_token=claim_token,
                    error_code=exc.code,
                    retry_after_seconds=retry_after,
                )
                counts[
                    "macro_retry" if retry_after is not None else "macro_failed"
                ] += 1
                if exc.code in _PROVIDER_WIDE_ERRORS:
                    stopped = True
                    break
            else:
                saved = db.save_macro_event_enrichment(
                    event_key,
                    input_hash=input_hash,
                    prompt_version=llm_enrichment.MACRO_PROMPT_VERSION,
                    model=config.model,
                    claim_token=claim_token,
                    evidence_basis=event_input["evidence_basis"],
                    result=result,
                )
                counts["macro_ready" if saved else "macro_superseded"] += 1

    # Scan the bounded retention set so warm ready rows cannot hide a newly
    # invalidated cache behind a fixed first page.
    pool_limit = 5_000
    if not stopped:
        candidates = db.query_enrichment_candidates(
            max_age_hours=max_age_hours,
            limit=pool_limit,
        )
        for event in candidates:
            if counts["processed"] >= limit:
                break
            if not llm_enrichment.is_event_enrichment_eligible(event):
                counts["skipped_noncontent"] += 1
                continue
            event_input, input_hash = llm_enrichment.build_event_input(event)
            claim_token = db.claim_event_enrichment(
                int(event["id"]),
                input_hash=input_hash,
                prompt_version=llm_enrichment.PROMPT_VERSION,
                model=config.model,
                evidence_basis=event_input["evidence_basis"],
            )
            if claim_token is None:
                continue
            counts["processed"] += 1
            try:
                result = llm_enrichment.enrich_event(
                    event_input,
                    input_hash=input_hash,
                    config=config,
                )
            except llm_enrichment.EnrichmentError as exc:
                same_cache = (
                    event.get("ai_input_hash") == input_hash
                    and event.get("ai_prompt_version")
                    == llm_enrichment.PROMPT_VERSION
                    and event.get("ai_model") == config.model
                )
                previous_attempts = (
                    int(event.get("ai_attempt_count") or 0) if same_cache else 0
                )
                retry_after = exc.retry_after_seconds
                if exc.code == "invalid_output" and previous_attempts >= 2:
                    retry_after = None
                db.fail_event_enrichment(
                    int(event["id"]),
                    input_hash=input_hash,
                    prompt_version=llm_enrichment.PROMPT_VERSION,
                    model=config.model,
                    claim_token=claim_token,
                    error_code=exc.code,
                    retry_after_seconds=retry_after,
                )
                counts["retry" if retry_after is not None else "failed"] += 1
                if exc.code in _PROVIDER_WIDE_ERRORS:
                    stopped = True
                    break
            else:
                saved = db.save_event_enrichment(
                    int(event["id"]),
                    input_hash=input_hash,
                    prompt_version=llm_enrichment.PROMPT_VERSION,
                    model=config.model,
                    claim_token=claim_token,
                    evidence_basis=event_input["evidence_basis"],
                    result=result,
                )
                counts["ready" if saved else "superseded"] += 1

    return {
        "configured": True,
        "processed": counts["processed"],
        "ready": counts["ready"],
        "retry": counts["retry"],
        "failed": counts["failed"],
        "skipped_noncontent": counts["skipped_noncontent"],
        "superseded": counts["superseded"],
        "macro_processed": counts["macro_processed"],
        "macro_ready": counts["macro_ready"],
        "macro_retry": counts["macro_retry"],
        "macro_failed": counts["macro_failed"],
        "macro_superseded": counts["macro_superseded"],
    }, stopped


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich recent KOL events")
    parser.add_argument("--limit", type=int, default=_default_limit())
    parser.add_argument("--macro-limit", type=int, default=_default_macro_limit())
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()
    limit = max(1, min(int(args.limit), 200))
    max_age_hours = max(1, min(int(args.max_age_hours), 14 * 24))
    macro_limit = max(0, min(int(args.macro_limit), 24))
    result, stopped = run(
        limit=limit,
        max_age_hours=max_age_hours,
        macro_limit=macro_limit,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
