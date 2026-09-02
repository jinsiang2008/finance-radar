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
import time
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

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


def _default_concurrency() -> int:
    try:
        value = int(os.environ.get("KOL_ENRICHMENT_CONCURRENCY", "6"))
    except ValueError:
        value = 6
    return max(1, min(value, 8))


_PROVIDER_WIDE_ERRORS = {
    "authentication",
    "balance",
    "invalid_request",
    "network",
    "provider_error",
    "provider_unavailable",
    "rate_limit",
}


def _event_attempt_count(
    event: Mapping[str, Any],
    *,
    input_hash: str,
    model: str,
) -> int:
    same_cache = (
        event.get("ai_input_hash") == input_hash
        and event.get("ai_prompt_version") == llm_enrichment.PROMPT_VERSION
        and event.get("ai_model") == model
    )
    return int(event.get("ai_attempt_count") or 0) + 1 if same_cache else 1


def _provider_call(
    event_input: Mapping[str, Any],
    input_hash: str,
    config: llm_enrichment.DeepSeekConfig,
) -> Any:
    try:
        return llm_enrichment.enrich_event(
            event_input,
            input_hash=input_hash,
            config=config,
            return_response=True,
        )
    except llm_enrichment.EnrichmentError:
        raise
    except Exception:
        raise llm_enrichment.EnrichmentError(
            "worker_exception", retry_after_seconds=10 * 60
        ) from None


def _response_parts(value: Any) -> tuple[dict[str, Any], Any, int | None]:
    if isinstance(value, llm_enrichment.EnrichmentResponse):
        return value.result, value.usage, value.http_status
    # Existing test doubles and downstream wrappers may still return a dict.
    if isinstance(value, dict):
        return value, llm_enrichment.ProviderUsage(), 200
    raise llm_enrichment.EnrichmentError(
        "invalid_output", retry_after_seconds=15 * 60
    )


def _manual_request_task(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve a queued identity against current canonical public evidence."""
    kind = str(request.get("subject_type") or "")
    subject_key = str(request.get("subject_key") or "")
    event: Mapping[str, Any] | None = None
    if kind == "event":
        try:
            event = db.get_event_enrichment_subject(int(subject_key))
        except (TypeError, ValueError):
            event = None
        if event is None or not llm_enrichment.is_event_enrichment_eligible(event):
            db.complete_ai_enrichment_request(
                int(request["id"]),
                subject_type=kind,
                subject_key=subject_key,
                input_hash=str(request.get("input_hash") or ""),
                prompt_version=str(request.get("prompt_version") or ""),
                superseded=True,
            )
            return None
        event_input, input_hash = llm_enrichment.build_event_input(event)
        prompt_version = llm_enrichment.PROMPT_VERSION
    elif kind == "macro_event":
        snapshot = db.latest_macro()
        monitored = (
            snapshot.get("monitored_events")
            if isinstance(snapshot, Mapping)
            else None
        )
        if isinstance(monitored, list):
            for candidate in monitored[:24]:
                if (
                    isinstance(candidate, Mapping)
                    and llm_enrichment.macro_event_key(candidate) == subject_key
                ):
                    event = candidate
                    break
        if event is None:
            db.complete_ai_enrichment_request(
                int(request["id"]),
                subject_type=kind,
                subject_key=subject_key,
                input_hash=str(request.get("input_hash") or ""),
                prompt_version=str(request.get("prompt_version") or ""),
                superseded=True,
            )
            return None
        event_input, input_hash = llm_enrichment.build_macro_event_input(event)
        prompt_version = llm_enrichment.MACRO_PROMPT_VERSION
    else:
        return None

    if (
        input_hash != request.get("input_hash")
        or prompt_version != request.get("prompt_version")
    ):
        db.complete_ai_enrichment_request(
            int(request["id"]),
            subject_type=kind,
            subject_key=subject_key,
            input_hash=str(request.get("input_hash") or ""),
            prompt_version=str(request.get("prompt_version") or ""),
            superseded=True,
        )
        return None
    return {
        "kind": kind,
        "subject_key": subject_key,
        "input": event_input,
        "input_hash": input_hash,
        "prompt_version": prompt_version,
        "event": event,
        "manual_request_id": int(request["id"]),
    }


def _event_task_is_current(
    event: Mapping[str, Any],
    task: Mapping[str, Any],
) -> bool:
    if not llm_enrichment.is_event_enrichment_eligible(event):
        return False
    _, input_hash = llm_enrichment.build_event_input(event)
    return (
        input_hash == task.get("input_hash")
        and task.get("prompt_version") == llm_enrichment.PROMPT_VERSION
    )


def _macro_task_is_current(
    snapshot: Mapping[str, Any],
    task: Mapping[str, Any],
) -> bool:
    monitored = snapshot.get("monitored_events")
    if not isinstance(monitored, list):
        return False
    for candidate in monitored[:24]:
        if not isinstance(candidate, Mapping):
            continue
        if llm_enrichment.macro_event_key(candidate) != task.get("subject_key"):
            continue
        _, input_hash = llm_enrichment.build_macro_event_input(candidate)
        return (
            input_hash == task.get("input_hash")
            and task.get("prompt_version") == llm_enrichment.MACRO_PROMPT_VERSION
        )
    return False


def run(
    *,
    limit: int,
    max_age_hours: int,
    macro_limit: int | None = None,
    concurrency: int | None = None,
) -> tuple[dict[str, int | bool], bool]:
    db.init()
    db.abandon_stale_llm_calls()
    safe_macro_limit = (
        _default_macro_limit()
        if macro_limit is None
        else max(0, min(int(macro_limit), 24))
    )
    safe_concurrency = (
        _default_concurrency()
        if concurrency is None
        else max(1, min(int(concurrency), 8))
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
            "superseded": 0,
            "concurrency": safe_concurrency,
        }, False

    counts: Counter[str] = Counter()
    stopped = False

    manual_tasks: list[dict[str, Any]] = []
    for request in db.query_pending_ai_enrichment_requests(limit=200):
        task = _manual_request_task(request)
        if task is not None:
            manual_tasks.append(task)

    snapshot = db.latest_macro()
    monitored_events = (
        snapshot.get("monitored_events")
        if isinstance(snapshot, Mapping)
        else None
    )
    macro_tasks: list[dict[str, Any]] = []
    if isinstance(monitored_events, list):
        for event in monitored_events[:24]:
            if not isinstance(event, Mapping):
                continue
            event_key = llm_enrichment.macro_event_key(event)
            event_input, input_hash = llm_enrichment.build_macro_event_input(event)
            macro_tasks.append(
                {
                    "kind": "macro_event",
                    "subject_key": event_key,
                    "input": event_input,
                    "input_hash": input_hash,
                    "prompt_version": llm_enrichment.MACRO_PROMPT_VERSION,
                    "event": event,
                }
            )

    event_tasks: list[dict[str, Any]] = []
    candidates = db.query_enrichment_candidates(
        max_age_hours=max_age_hours,
        limit=5_000,
    )
    for event in candidates:
        if not llm_enrichment.is_event_enrichment_eligible(event):
            counts["skipped_noncontent"] += 1
            continue
        event_input, input_hash = llm_enrichment.build_event_input(event)
        event_tasks.append(
            {
                "kind": "event",
                "subject_key": str(int(event["id"])),
                "input": event_input,
                "input_hash": input_hash,
                "prompt_version": llm_enrichment.PROMPT_VERSION,
                "event": event,
            }
        )

    manual_subjects = {
        (task["kind"], task["subject_key"]) for task in manual_tasks
    }
    macro_tasks = [
        task
        for task in macro_tasks
        if (task["kind"], task["subject_key"]) not in manual_subjects
    ]
    event_tasks = [
        task
        for task in event_tasks
        if (task["kind"], task["subject_key"]) not in manual_subjects
    ]

    # Alternate task kinds so a full macro batch cannot block a time-sensitive
    # KOL event. Warm cache rows do not consume either processing quota.
    pending: list[dict[str, Any]] = list(manual_tasks)
    for index in range(max(len(macro_tasks), len(event_tasks))):
        if index < len(macro_tasks):
            pending.append(macro_tasks[index])
        if index < len(event_tasks):
            pending.append(event_tasks[index])
    pending_index = 0
    active: dict[Future[Any], dict[str, Any]] = {}

    def complete_manual(task: Mapping[str, Any], *, superseded: bool = False) -> None:
        request_id = task.get("manual_request_id")
        if request_id is None:
            return
        db.complete_ai_enrichment_request(
            int(request_id),
            subject_type=str(task["kind"]),
            subject_key=str(task["subject_key"]),
            input_hash=str(task["input_hash"]),
            prompt_version=str(task["prompt_version"]),
            superseded=superseded,
        )

    def settle_manual_without_claim(task: Mapping[str, Any]) -> None:
        if task.get("manual_request_id") is None:
            return
        status = db.get_ai_enrichment_request_status(
            subject_type=str(task["kind"]),
            subject_key=str(task["subject_key"]),
            input_hash=str(task["input_hash"]),
            prompt_version=str(task["prompt_version"]),
            model=config.model,
        )
        # A live claim or retry keeps an old, manually selected event reachable
        # even when it is outside the normal 72-hour candidate window.
        if status.get("state") in {"ready", "failed"}:
            complete_manual(task)

    def claim_and_submit(executor: ThreadPoolExecutor, task: dict[str, Any]) -> None:
        nonlocal stopped
        kind = task["kind"]
        identity_current = [True]
        if kind == "macro_event":

            def current_snapshot_check(snapshot: Mapping[str, Any]) -> bool:
                identity_current[0] = _macro_task_is_current(snapshot, task)
                return identity_current[0]

            claim = db.claim_macro_event_enrichment(
                task["subject_key"],
                input_hash=task["input_hash"],
                prompt_version=task["prompt_version"],
                model=config.model,
                evidence_basis=task["input"]["evidence_basis"],
                current_snapshot_check=current_snapshot_check,
            )
            if claim is None:
                if identity_current[0]:
                    settle_manual_without_claim(task)
                else:
                    complete_manual(task, superseded=True)
                return
            claim_token, attempt_count = claim
            counts["macro_processed"] += 1
        else:
            event = task["event"]

            def current_event_check(current_event: Mapping[str, Any]) -> bool:
                identity_current[0] = _event_task_is_current(current_event, task)
                return identity_current[0]

            claim = db.claim_event_enrichment(
                int(event["id"]),
                input_hash=task["input_hash"],
                prompt_version=task["prompt_version"],
                model=config.model,
                evidence_basis=task["input"]["evidence_basis"],
                current_event_check=current_event_check,
                return_attempt_count=True,
            )
            if claim is None:
                if identity_current[0]:
                    settle_manual_without_claim(task)
                else:
                    complete_manual(task, superseded=True)
                return
            if isinstance(claim, tuple):
                claim_token, attempt_count = claim
            else:
                # Preserve compatibility with simple test doubles and wrappers.
                claim_token = claim
                attempt_count = _event_attempt_count(
                    event,
                    input_hash=task["input_hash"],
                    model=config.model,
                )
            counts["processed"] += 1
        task["claim_token"] = claim_token
        task["attempt_count"] = attempt_count
        try:
            task["call_id"] = db.begin_llm_call(
                subject_type=kind,
                subject_key=task["subject_key"],
                input_hash=task["input_hash"],
                prompt_version=task["prompt_version"],
                model=config.model,
                attempt_count=attempt_count,
            )
            task["started_ns"] = time.monotonic_ns()
            future = executor.submit(
                _provider_call,
                task["input"],
                task["input_hash"],
                config,
            )
            active[future] = task
        except Exception:
            if kind == "macro_event":
                db.fail_macro_event_enrichment(
                    task["subject_key"],
                    input_hash=task["input_hash"],
                    prompt_version=task["prompt_version"],
                    model=config.model,
                    claim_token=claim_token,
                    error_code="worker_submit",
                    retry_after_seconds=60,
                )
            else:
                db.fail_event_enrichment(
                    int(task["event"]["id"]),
                    input_hash=task["input_hash"],
                    prompt_version=task["prompt_version"],
                    model=config.model,
                    claim_token=claim_token,
                    error_code="worker_submit",
                    retry_after_seconds=60,
                )
            if task.get("call_id"):
                db.finish_llm_call(
                    task["call_id"],
                    outcome="cancelled",
                    error_code="worker_submit",
                    latency_ms=0,
                )
            stopped = True

    def finish_task(future: Future[Any], task: dict[str, Any]) -> None:
        nonlocal stopped
        latency_ms = min(
            3_600_000,
            max(0, int((time.monotonic_ns() - task["started_ns"]) / 1_000_000)),
        )
        usage: Any = None
        http_status: int | None = None
        try:
            result, usage, http_status = _response_parts(future.result())
        except llm_enrichment.EnrichmentError as exc:
            retry_after = exc.retry_after_seconds
            if exc.code == "invalid_output" and task["attempt_count"] >= 3:
                retry_after = None
            if task["kind"] == "macro_event":
                persisted = db.fail_macro_event_enrichment(
                    task["subject_key"],
                    input_hash=task["input_hash"],
                    prompt_version=task["prompt_version"],
                    model=config.model,
                    claim_token=task["claim_token"],
                    error_code=exc.code,
                    retry_after_seconds=retry_after,
                )
                counter = (
                    "macro_superseded"
                    if not persisted
                    else "macro_retry" if retry_after is not None else "macro_failed"
                )
            else:
                persisted = db.fail_event_enrichment(
                    int(task["event"]["id"]),
                    input_hash=task["input_hash"],
                    prompt_version=task["prompt_version"],
                    model=config.model,
                    claim_token=task["claim_token"],
                    error_code=exc.code,
                    retry_after_seconds=retry_after,
                )
                counter = (
                    "superseded"
                    if not persisted
                    else "retry" if retry_after is not None else "failed"
                )
            counts[counter] += 1
            db.finish_llm_call(
                task["call_id"],
                outcome=(
                    "superseded"
                    if not persisted
                    else "retry" if retry_after is not None else "failed"
                ),
                error_code=exc.code,
                http_status=exc.http_status,
                latency_ms=latency_ms,
                usage=exc.usage,
            )
            if not persisted:
                complete_manual(task, superseded=True)
            elif retry_after is None:
                complete_manual(task)
            if exc.code in _PROVIDER_WIDE_ERRORS:
                stopped = True
            return

        if task["kind"] == "macro_event":
            saved = db.save_macro_event_enrichment(
                task["subject_key"],
                input_hash=task["input_hash"],
                prompt_version=task["prompt_version"],
                model=config.model,
                claim_token=task["claim_token"],
                evidence_basis=task["input"]["evidence_basis"],
                result=result,
            )
            counts["macro_ready" if saved else "macro_superseded"] += 1
        else:
            saved = db.save_event_enrichment(
                int(task["event"]["id"]),
                input_hash=task["input_hash"],
                prompt_version=task["prompt_version"],
                model=config.model,
                claim_token=task["claim_token"],
                evidence_basis=task["input"]["evidence_basis"],
                result=result,
            )
            counts["ready" if saved else "superseded"] += 1
        db.finish_llm_call(
            task["call_id"],
            outcome="ready" if saved else "superseded",
            http_status=http_status,
            latency_ms=latency_ms,
            usage=usage,
        )
        complete_manual(task, superseded=not saved)

    with ThreadPoolExecutor(
        max_workers=safe_concurrency,
        thread_name_prefix="deepseek-enrich",
    ) as executor:
        while active or (pending_index < len(pending) and not stopped):
            while (
                not stopped
                and len(active) < safe_concurrency
                and pending_index < len(pending)
            ):
                task = pending[pending_index]
                pending_index += 1
                if task["kind"] == "macro_event":
                    if counts["macro_processed"] >= safe_macro_limit:
                        continue
                elif counts["processed"] >= limit:
                    continue
                claim_and_submit(executor, task)
            if not active:
                continue
            completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
            for future in completed:
                task = active.pop(future)
                finish_task(future, task)

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
        "manual_requests": len(manual_tasks),
        "concurrency": safe_concurrency,
    }, stopped


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich recent KOL events")
    parser.add_argument("--limit", type=int, default=_default_limit())
    parser.add_argument("--macro-limit", type=int, default=_default_macro_limit())
    parser.add_argument("--max-age-hours", type=int, default=72)
    parser.add_argument("--concurrency", type=int, default=_default_concurrency())
    args = parser.parse_args()
    limit = max(1, min(int(args.limit), 200))
    max_age_hours = max(1, min(int(args.max_age_hours), 14 * 24))
    macro_limit = max(0, min(int(args.macro_limit), 24))
    concurrency = max(1, min(int(args.concurrency), 8))
    result, stopped = run(
        limit=limit,
        max_age_hours=max_age_hours,
        macro_limit=macro_limit,
        concurrency=concurrency,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
