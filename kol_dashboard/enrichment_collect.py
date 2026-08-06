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

import db
import llm_enrichment


def _default_limit() -> int:
    try:
        value = int(os.environ.get("KOL_ENRICHMENT_BATCH_LIMIT", "18"))
    except ValueError:
        value = 18
    return max(1, min(value, 200))


def run(*, limit: int, max_age_hours: int) -> tuple[dict[str, int | bool], bool]:
    db.init()
    config = llm_enrichment.load_config()
    if config is None:
        return {
            "configured": False,
            "processed": 0,
            "ready": 0,
            "retry": 0,
            "failed": 0,
        }, False

    # Scan the bounded retention set so warm ready rows cannot hide a newly
    # invalidated cache behind a fixed first page.
    pool_limit = 5_000
    candidates = db.query_enrichment_candidates(
        max_age_hours=max_age_hours,
        limit=pool_limit,
    )
    counts: Counter[str] = Counter()
    stopped = False
    for event in candidates:
        if counts["processed"] >= limit:
            break
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
            if exc.code in {
                "authentication",
                "balance",
                "invalid_request",
                "network",
                "provider_error",
                "provider_unavailable",
                "rate_limit",
            }:
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
        "superseded": counts["superseded"],
    }, stopped


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich recent KOL events")
    parser.add_argument("--limit", type=int, default=_default_limit())
    parser.add_argument("--max-age-hours", type=int, default=72)
    args = parser.parse_args()
    limit = max(1, min(int(args.limit), 200))
    max_age_hours = max(1, min(int(args.max_age_hours), 14 * 24))
    result, stopped = run(limit=limit, max_age_hours=max_age_hours)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
