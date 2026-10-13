#!/usr/bin/env python3
"""Collect configured publisher RSS feeds; safe for a five-minute systemd timer."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
from pathlib import Path

try:
    import news_service
except ModuleNotFoundError:  # Source-tree package invocation.
    from kol_dashboard import news_service


def collect_news(*, db_path=None, now=None, fetcher=None) -> dict:
    # A production request may take seconds. Timestamp the completed observation,
    # not the start of the batch, so freshly published entries are not "future".
    current = news_service.utc_datetime(now) if now is not None else None
    news_service.init_news_db(db_path)
    fetcher = fetcher or news_service.fetch_feed
    states = {source.id: news_service.source_state(source.id, db_path) for source in news_service.SOURCES}
    results = []
    # Network requests are independent; SQLite commits stay serialized here.
    with ThreadPoolExecutor(max_workers=min(5, len(news_service.SOURCES)), thread_name_prefix="publisher-news") as pool:
        pending = {pool.submit(fetcher, source, etag=states[source.id].get("etag"),
                               last_modified=states[source.id].get("last_modified")): source
                   for source in news_service.SOURCES}
        for future in as_completed(pending):
            source = pending[future]
            try:
                result = news_service.store_feed(source, future.result(), db_path=db_path,
                                                 now=current or news_service.utc_datetime())
            except Exception as exc:
                # Only our fixed codes are public. Never expose upstream exception
                # messages, request headers, credentials, or a filesystem path.
                code = str(exc) if isinstance(exc, news_service.NewsFeedError) else "collection_error"
                news_service.record_source_failure(source, code, db_path=db_path,
                                                   now=current or news_service.utc_datetime())
                result = {"id": source.id, "status": "error", "error": code, "inserted": 0, "updated": 0}
            results.append(result)
    results.sort(key=lambda row: row["id"])
    success = sum(row["status"] != "error" for row in results)
    return {"status": "ok" if success == len(results) else "degraded" if success else "failed",
            "checked_sources": len(results), "successful_sources": success,
            "inserted": sum(row["inserted"] for row in results), "updated": sum(row["updated"] for row in results),
            "pruned": news_service.prune_news(db_path=db_path, now=current), "sources": results}


def run_once(*, db_path=None, now=None, fetcher=None, lock_path=None) -> dict:
    path = news_service.database_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(lock_path) if lock_path else Path(str(path) + ".news-collect.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "checked_sources": 0, "successful_sources": 0,
                    "inserted": 0, "updated": 0, "sources": []}
        try:
            return collect_news(db_path=db_path, now=now, fetcher=fetcher)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override KOL_DASHBOARD_DB for an isolated validation database")
    args = parser.parse_args(argv)
    try:
        result = run_once(db_path=args.db)
    except Exception:
        print(json.dumps({"status": "failed", "error": "collector_failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
