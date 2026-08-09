from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kol_dashboard import db, llm_enrichment, relation_engine


def ready_enrichment(**overrides):
    result = {
        "headline_zh": "英伟达发布新一代人工智能平台",
        "summary_zh": "英伟达发布新的人工智能平台，但具体产品参数仍需核对原始来源。",
        "why_it_matters_zh": "若客户采用增加，可能影响美国半导体板块与相关供应链。",
        "impact_level": "high",
        "impact_path": ["产品发布 → 算力需求 → 半导体股票"],
        "tags": ["人工智能", "半导体"],
        "assets": [
            {
                "asset_key": "US:NVDA",
                "name_zh": "英伟达",
                "direction": "positive",
                "horizon": "medium",
                "reason_zh": "采用增加可能改善收入预期。",
                "confidence": 0.74,
            }
        ],
        "cluster_key": "nvidia-launches-ai-platform",
        "language": "en",
        "confidence": 0.72,
        "schema_version": 1,
    }
    result.update(overrides)
    return result


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "kol-test.sqlite3")
        self.db_path_patch = mock.patch.object(db, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        db.init()

    def tearDown(self) -> None:
        self.db_path_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def event(**overrides):
        item = {
            "title": "NVIDIA launches a new AI platform",
            "url": "https://example.com/nvidia-platform?utm_source=test",
            "snippet": "NVIDIA described its new platform.",
            "source": "Bing News",
            "kol_key": "huangrenxun",
            "kol_name": "Jensen Huang",
            "kol_name_cn": "黄仁勋",
            "impact": "medium",
            "has_market_kw": True,
            "tickers": ["NVDA"],
        }
        item.update(overrides)
        return item

    def test_insert_events_writes_published_at(self) -> None:
        published_at = "2026-07-31T10:34:56+00:00"

        db.insert_events([self.event(published_at=published_at)])

        with db.conn() as connection:
            row = connection.execute(
                "SELECT published_at FROM events"
            ).fetchone()
        self.assertEqual(row["published_at"], published_at)

    def test_market_reaction_diagnostics_are_bounded_and_public_safe(self) -> None:
        db.upsert_market_reaction(
            "event",
            "diagnostic",
            "THEME:AI",
            {
                "window": "1D",
                "status": "unavailable",
                "sample_count": 0,
                "data_timestamps": {},
                "method_version": "common_trading_days:test",
                "provider": "Yahoo",
                "provider_symbol": "SOXX",
                "proxy_for": "THEME:AI",
                "asset_status": "unavailable",
                "benchmark_status": "available",
                "reason_code": "request_failed:TimeoutError secret detail",
                "next_due_at": "2026-08-09T09:00:00+08:00",
            },
            benchmark_asset_key="US:SPY",
            observed_at="2026-08-09T00:00:00+00:00",
        )

        row = db.query_market_reactions(source_id="diagnostic")[0]

        self.assertEqual(row["provider"], "yahoo")
        self.assertEqual(row["provider_symbol"], "SOXX")
        self.assertEqual(row["proxy_for"], "THEME:AI")
        self.assertEqual(row["asset_status"], "unavailable")
        self.assertEqual(row["benchmark_status"], "available")
        self.assertEqual(row["reason_code"], "request_failed")
        self.assertEqual(row["next_due_at"], "2026-08-09T01:00:00+00:00")
        self.assertNotIn("TimeoutError", json.dumps(row))

    def test_pending_market_reaction_requires_a_due_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "require next_due_at"):
            db.upsert_market_reaction(
                "event",
                "pending",
                "US:SPY",
                {
                    "window": "3D",
                    "status": "pending",
                    "sample_count": 0,
                    "data_timestamps": {},
                    "method_version": "common_trading_days:test",
                    "reason_code": "window_not_due",
                },
            )

    def test_equal_rank_market_reaction_rejects_stale_writer(self) -> None:
        reaction = {
            "window": "1D",
            "status": "complete",
            "asset_return": 0.1,
            "benchmark_return": 0.03,
            "abnormal_return": 0.07,
            "sample_count": 2,
            "data_timestamps": {"start": 1, "end": 2},
            "method_version": "common_trading_days:test",
        }
        db.upsert_market_reaction(
            "event",
            "ordered-writer",
            "US:SPY",
            {**reaction, "abnormal_return": 0.08},
            observed_at="2026-08-09T10:00:00+08:00",
        )
        db.upsert_market_reaction(
            "event",
            "ordered-writer",
            "US:SPY",
            {**reaction, "abnormal_return": 0.01},
            observed_at="2026-08-09T01:00:00+00:00",
        )

        after_stale = db.query_market_reactions(
            source_id="ordered-writer"
        )[0]

        self.assertEqual(after_stale["abnormal_return"], 0.08)
        self.assertEqual(
            after_stale["observed_at"], "2026-08-09T02:00:00+00:00"
        )

        db.upsert_market_reaction(
            "event",
            "ordered-writer",
            "US:SPY",
            {**reaction, "abnormal_return": 0.09},
            observed_at="2026-08-09T03:00:00+00:00",
        )
        after_newer = db.query_market_reactions(
            source_id="ordered-writer"
        )[0]

        self.assertEqual(after_newer["abnormal_return"], 0.09)
        self.assertEqual(
            after_newer["observed_at"], "2026-08-09T03:00:00+00:00"
        )

    def test_macro_event_enrichment_cache_uses_an_independent_text_key(self) -> None:
        with db.conn() as connection:
            columns = {
                row["name"]: dict(row)
                for row in connection.execute(
                    "PRAGMA table_info(macro_event_enrichments)"
                ).fetchall()
            }
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(macro_event_enrichments)"
            ).fetchall()

        self.assertEqual(columns["event_key"]["type"], "TEXT")
        self.assertEqual(columns["event_key"]["pk"], 1)
        self.assertIn("input_hash", columns)
        self.assertIn("prompt_version", columns)
        self.assertIn("claim_token", columns)
        self.assertEqual(foreign_keys, [])

    def test_llm_call_attempt_lifecycle_is_bounded_and_idempotent(self) -> None:
        call_id = db.begin_llm_call(
            subject_type="event",
            subject_key="42",
            input_hash="a" * 64,
            prompt_version="event-intelligence-v1",
            model="deepseek-v4-flash",
            attempt_count=2,
            started_at="2026-08-08T18:00:00+08:00",
        )

        self.assertTrue(
            db.finish_llm_call(
                call_id,
                outcome="ready",
                latency_ms=321,
                http_status=200,
                usage={
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                    "completion_tokens": 15,
                    "reasoning_tokens": 4,
                    "total_tokens": 115,
                },
                completed_at="2026-08-08T10:00:01+00:00",
            )
        )
        self.assertFalse(
            db.finish_llm_call(
                call_id,
                outcome="failed",
                latency_ms=999,
                error_code="late_writer",
                completed_at="2026-08-08T10:00:02+00:00",
            )
        )

        with db.conn() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(llm_call_attempts)"
                ).fetchall()
            }
            row = dict(
                connection.execute(
                    "SELECT * FROM llm_call_attempts WHERE id=?",
                    (call_id,),
                ).fetchone()
            )

        self.assertTrue(
            {"prompt", "response", "claim_token", "api_key"}.isdisjoint(columns)
        )
        self.assertEqual(row["provider"], "deepseek")
        self.assertEqual(row["outcome"], "ready")
        self.assertEqual(row["started_at"], "2026-08-08T10:00:00+00:00")
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(row["latency_ms"], 321)
        self.assertEqual(row["prompt_cache_hit_tokens"], 80)
        self.assertEqual(row["total_tokens"], 115)

    def test_llm_usage_summary_uses_a_numeric_utc_cutoff(self) -> None:
        reference = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        recent = db.begin_llm_call(
            subject_type="event",
            subject_key="recent",
            input_hash="b" * 64,
            prompt_version="event-intelligence-v1",
            model="deepseek-v4-flash",
            started_at="2026-08-08T19:30:00+08:00",
        )
        old = db.begin_llm_call(
            subject_type="macro_event",
            subject_key="old",
            input_hash="c" * 64,
            prompt_version="macro-intelligence-v1",
            model="deepseek-v4-pro",
            started_at="2026-08-08T08:00:00+00:00",
        )
        db.finish_llm_call(
            recent,
            outcome="ready",
            latency_ms=250,
            usage={
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 75,
                "prompt_cache_miss_tokens": 25,
                "completion_tokens": 12,
                "reasoning_tokens": 3,
                "total_tokens": 112,
            },
            completed_at=reference.isoformat(),
        )
        db.finish_llm_call(
            old,
            outcome="failed",
            latency_ms=800,
            usage={"prompt_tokens": 900, "total_tokens": 990},
            completed_at=reference.isoformat(),
        )

        summary = db.query_llm_usage_summary(hours=1, now=reference)

        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["prompt_tokens"], 100)
        self.assertEqual(summary["cache_hit_tokens"], 75)
        self.assertEqual(summary["cache_miss_tokens"], 25)
        self.assertEqual(summary["completion_tokens"], 12)
        self.assertEqual(summary["total_tokens"], 112)
        self.assertEqual(summary["average_latency_ms"], 250.0)
        with self.assertRaisesRegex(ValueError, "timezone"):
            db.query_llm_usage_summary(hours=1, now=reference.replace(tzinfo=None))

    def test_stale_llm_calls_are_abandoned_and_old_telemetry_is_pruned(self) -> None:
        current = datetime.now(timezone.utc).replace(microsecond=0)
        stale = db.begin_llm_call(
            subject_type="event",
            subject_key="stale",
            input_hash="d" * 64,
            prompt_version="event-intelligence-v1",
            model="deepseek-v4-flash",
            started_at=(current - timedelta(minutes=21)).isoformat(),
        )
        live = db.begin_llm_call(
            subject_type="event",
            subject_key="live",
            input_hash="e" * 64,
            prompt_version="event-intelligence-v1",
            model="deepseek-v4-flash",
            started_at=(current - timedelta(minutes=19)).isoformat(),
        )
        expired = db.begin_llm_call(
            subject_type="macro_event",
            subject_key="expired-ledger-row",
            input_hash="f" * 64,
            prompt_version="macro-intelligence-v1",
            model="deepseek-v4-pro",
            started_at=(current - timedelta(days=91)).isoformat(),
        )

        self.assertEqual(
            db.abandon_stale_llm_calls(
                older_than_seconds=20 * 60,
                now=current,
            ),
            2,
        )
        db.prune_old()

        with db.conn() as connection:
            rows = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT id, outcome, error_code FROM llm_call_attempts"
                ).fetchall()
            }
        self.assertEqual(rows[stale]["outcome"], "abandoned")
        self.assertEqual(rows[stale]["error_code"], "worker_lease_expired")
        self.assertEqual(rows[live]["outcome"], "started")
        self.assertNotIn(expired, rows)

    def test_merge_fills_missing_published_at_then_preserves_it(self) -> None:
        reliable = "2026-07-31T10:34:56+00:00"
        later_claim = "2026-08-01T10:34:56+00:00"

        db.insert_events([self.event(published_at=None)])
        db.insert_events([self.event(published_at=reliable)])
        db.insert_events([self.event(published_at=later_claim)])

        with db.conn() as connection:
            row = connection.execute(
                "SELECT published_at FROM events"
            ).fetchone()
        self.assertEqual(row["published_at"], reliable)

    def test_merge_treats_blank_published_at_as_missing(self) -> None:
        reliable = "2026-07-31T10:34:56+00:00"

        db.insert_events([self.event(published_at="")])
        db.insert_events([self.event(published_at=reliable)])

        with db.conn() as connection:
            event = connection.execute(
                "SELECT published_at FROM events"
            ).fetchone()
            sighting = connection.execute(
                "SELECT published_at FROM event_sightings"
            ).fetchone()
        self.assertEqual(event["published_at"], reliable)
        self.assertEqual(sighting["published_at"], reliable)

    def test_same_event_from_two_kols_has_one_event_and_two_sightings(self) -> None:
        db.insert_events([self.event()])
        db.insert_events(
            [
                self.event(
                    kol_key="musk",
                    kol_name="Elon Musk",
                    kol_name_cn="马斯克",
                )
            ]
        )

        with db.conn() as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) AS n FROM events"
            ).fetchone()["n"]
            sightings = connection.execute(
                "SELECT kol_key FROM event_sightings ORDER BY kol_key"
            ).fetchall()

        self.assertEqual(event_count, 1)
        self.assertEqual([row["kol_key"] for row in sightings], ["huangrenxun", "musk"])

    def test_query_events_for_second_kol_returns_matching_sighting_fields(self) -> None:
        db.insert_events([self.event(published_at="2026-07-30T10:00:00+00:00")])
        db.insert_events(
            [
                self.event(
                    url="https://x.com/elonmusk/status/123",
                    source="X @elonmusk",
                    kol_key="musk",
                    kol_name="Elon Musk",
                    kol_name_cn="马斯克",
                    published_at="2026-07-31T08:00:00+00:00",
                )
            ]
        )

        items = db.query_events(kol="musk")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kol_key"], "musk")
        self.assertEqual(items[0]["kol_name"], "Elon Musk")
        self.assertEqual(items[0]["kol_name_cn"], "马斯克")
        self.assertEqual(items[0]["source"], "X @elonmusk")
        self.assertEqual(
            items[0]["source_url"], "https://x.com/elonmusk/status/123"
        )
        self.assertEqual(
            items[0]["published_at"], "2026-07-31T08:00:00+00:00"
        )

    def test_query_events_uses_each_kol_sighting_time_for_filter_and_order(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
            microsecond=0
        ).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).replace(
            microsecond=0
        ).isoformat()
        db.insert_events(
            [
                self.event(
                    title="Shared story",
                    url="https://example.com/shared",
                    kol_key="huangrenxun",
                    published_at=recent,
                )
            ]
        )
        db.insert_events(
            [
                self.event(
                    title="Shared story",
                    url="https://x.com/elonmusk/status/old",
                    source="X @elonmusk",
                    kol_key="musk",
                    kol_name="Elon Musk",
                    kol_name_cn="马斯克",
                    published_at=old,
                ),
                self.event(
                    title="Musk recent story",
                    url="https://x.com/elonmusk/status/recent",
                    source="X @elonmusk",
                    kol_key="musk",
                    kol_name="Elon Musk",
                    kol_name_cn="马斯克",
                    published_at=recent,
                ),
            ]
        )
        with db.conn() as connection:
            connection.execute(
                "UPDATE event_sightings SET first_seen_at=?, last_seen_at=? "
                "WHERE kol_key='musk' AND source_url LIKE '%/old'",
                (old, old),
            )

        musk_recent = db.query_events(kol="musk", hours=24)
        musk_all = db.query_events(kol="musk")
        huang_recent = db.query_events(kol="huangrenxun", hours=24)

        self.assertEqual(
            [item["title"] for item in musk_recent], ["Musk recent story"]
        )
        self.assertEqual(
            [item["title"] for item in musk_all],
            ["Musk recent story", "Shared story"],
        )
        self.assertEqual(
            [item["title"] for item in huang_recent], ["Shared story"]
        )
        self.assertEqual(musk_all[1]["first_seen_at"], old)
        self.assertEqual(musk_all[1]["last_seen_at"], old)
        self.assertEqual(musk_all[1]["published_at"], old)

    def test_kol_pagination_filters_noncontent_sighting_before_limit(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        newest = (now - timedelta(minutes=5)).isoformat()
        older = (now - timedelta(minutes=10)).isoformat()
        db.insert_events(
            [
                self.event(
                    title="AI",
                    snippet="AI",
                    url="https://example.com/brief-ai-item",
                    source="Bing News",
                    kol_key="reporter",
                    published_at=newest,
                )
            ]
        )
        db.insert_events(
            [
                self.event(
                    title="AI",
                    snippet="RT https://truthsocial.com/@realDonaldTrump",
                    url=(
                        "https://truthsocial.com/@realDonaldTrump/"
                        "117051398671535118"
                    ),
                    source="Truth Social @realDonaldTrump",
                    kol_key="trump",
                    kol_name="Donald Trump",
                    kol_name_cn="特朗普",
                    published_at=newest,
                ),
                self.event(
                    title="Tariff review enters final stage",
                    snippet="The semiconductor tariff review enters its final stage.",
                    url=(
                        "https://truthsocial.com/@realDonaldTrump/"
                        "117051398671535119"
                    ),
                    source="Truth Social @realDonaldTrump",
                    kol_key="trump",
                    kol_name="Donald Trump",
                    kol_name_cn="特朗普",
                    published_at=older,
                ),
            ]
        )
        # The same KOL can sight a merged story through multiple sources. When
        # publication times tie, both the feed and KOL counters must choose the
        # source with the newest last_seen_at (not the newest first_seen_at).
        db.insert_events(
            [
                self.event(
                    title="AI",
                    snippet="AI",
                    url="https://example.com/brief-ai-item",
                    source="Bing News",
                    kol_key="trump",
                    kol_name="Donald Trump",
                    kol_name_cn="特朗普",
                    published_at=newest,
                )
            ]
        )
        with db.conn() as connection:
            connection.execute(
                "UPDATE event_sightings SET first_seen_at=?, last_seen_at=? "
                "WHERE kol_key='trump' AND source='Bing News'",
                (
                    (now - timedelta(minutes=1)).isoformat(),
                    (now - timedelta(minutes=2)).isoformat(),
                ),
            )
            connection.execute(
                "UPDATE event_sightings SET first_seen_at=?, last_seen_at=? "
                "WHERE kol_key='trump' AND source LIKE 'Truth Social%' "
                "AND source_url LIKE '%117051398671535118'",
                (
                    (now - timedelta(minutes=30)).isoformat(),
                    now.isoformat(),
                ),
            )

        items = db.query_events(kol="trump", limit=1, now=now)

        self.assertEqual(
            [item["title"] for item in items],
            ["Tariff review enters final stage"],
        )
        trump = next(item for item in db.list_kols() if item["kol_key"] == "trump")
        self.assertEqual(trump["total"], 1)
        self.assertEqual(trump["total_24h"], 1)
        self.assertEqual(db.stats(hours=24)["active_kols"], 2)

    def test_recent_window_uses_publication_not_last_sighting(self) -> None:
        now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=2)).isoformat()
        old = (now - timedelta(days=400)).isoformat()
        db.insert_events(
            [
                self.event(
                    title="Actually recent",
                    url="https://example.com/recent",
                    published_at=recent,
                ),
                self.event(
                    title="Old story observed again",
                    url="https://example.com/old",
                    published_at=old,
                ),
            ]
        )
        with db.conn() as connection:
            connection.execute(
                "UPDATE events SET last_seen_at=? WHERE title=?",
                (now.isoformat(), "Old story observed again"),
            )

        items = db.query_events(hours=24, now=now)

        self.assertEqual([item["title"] for item in items], ["Actually recent"])
        self.assertEqual(items[0]["time_status"], "verified")

    def test_unverified_time_records_are_quarantined(self) -> None:
        now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(db, "_now_iso", return_value=now.isoformat()):
            db.insert_events(
                [
                    self.event(
                        title="Unknown publication time",
                        url="https://example.com/unknown",
                        published_at=None,
                    ),
                    self.event(
                        title="Implausible future time",
                        url="https://example.com/future",
                        published_at=(now + timedelta(hours=2)).isoformat(),
                    ),
                    self.event(
                        title="Verified publication time",
                        url="https://example.com/verified",
                        published_at=(now - timedelta(hours=1)).isoformat(),
                    ),
                ]
            )

        verified = db.query_events(hours=24, time_status="verified", now=now)
        quarantined = db.query_events(
            hours=24, time_status="unverified", now=now
        )

        self.assertEqual(
            [item["title"] for item in verified],
            ["Verified publication time"],
        )
        self.assertEqual(
            {item["title"] for item in quarantined},
            {"Unknown publication time", "Implausible future time"},
        )
        self.assertEqual(
            {item["time_status"] for item in quarantined},
            {"unknown", "future"},
        )

    def test_future_time_never_becomes_verified_just_because_clock_passes(
        self,
    ) -> None:
        observed = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        claimed = observed + timedelta(hours=2)
        with mock.patch.object(
            db, "_now_iso", return_value=observed.isoformat()
        ):
            db.insert_events(
                [
                    self.event(
                        title="Future timestamp",
                        url="https://example.com/future-persistent",
                        published_at=claimed.isoformat(),
                    )
                ]
            )

        much_later = observed + timedelta(days=2)
        verified = db.query_events(
            time_status="verified", hours=168, now=much_later
        )
        quarantined = db.query_events(
            time_status="unverified", hours=168, now=much_later
        )

        self.assertEqual(verified, [])
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0]["time_status"], "future")

    def test_verified_timestamp_replaces_an_earlier_future_claim(self) -> None:
        observed = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(
            db, "_now_iso", return_value=observed.isoformat()
        ):
            db.insert_events(
                [
                    self.event(
                        title="Timestamp corrected",
                        url="https://example.com/time-corrected",
                        published_at=(observed + timedelta(hours=2)).isoformat(),
                    )
                ]
            )
            db.insert_events(
                [
                    self.event(
                        title="Timestamp corrected",
                        url="https://example.com/time-corrected",
                        published_at=(observed - timedelta(hours=1)).isoformat(),
                    )
                ]
            )

        items = db.query_events(hours=24, now=observed)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["time_status"], "verified")
        self.assertEqual(
            items[0]["published_at"],
            "2026-08-02T23:00:00+00:00",
        )

    def test_publication_status_is_stable_across_restart(self) -> None:
        first_seen = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(
            db, "_now_iso", return_value=first_seen.isoformat()
        ):
            db.insert_events(
                [
                    self.event(
                        title="Late timestamp claim",
                        url="https://example.com/late-claim",
                        published_at=None,
                    )
                ]
            )
        with mock.patch.object(
            db,
            "_now_iso",
            return_value=(first_seen + timedelta(hours=2)).isoformat(),
        ):
            db.insert_events(
                [
                    self.event(
                        title="Late timestamp claim",
                        url="https://another.example.com/late-claim",
                        published_at=(
                            first_seen + timedelta(hours=1, minutes=30)
                        ).isoformat(),
                    )
                ]
            )

        before_restart = db.query_events(
            time_status="unverified",
            hours=24,
            now=first_seen + timedelta(hours=2),
        )
        db.init()
        after_restart = db.query_events(
            time_status="unverified",
            hours=24,
            now=first_seen + timedelta(hours=2),
        )

        self.assertEqual(len(before_restart), 1)
        self.assertEqual(before_restart[0]["time_status"], "future")
        self.assertEqual(
            after_restart[0]["time_status"],
            before_restart[0]["time_status"],
        )

    def test_kol_filter_selects_verified_sighting_before_newer_undated_one(
        self,
    ) -> None:
        observed = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(
            db, "_now_iso", return_value=observed.isoformat()
        ):
            db.insert_events(
                [
                    self.event(
                        title="Shared timestamped report",
                        url="https://example.com/reliable",
                        published_at=(observed - timedelta(hours=1)).isoformat(),
                    )
                ]
            )
        with mock.patch.object(
            db,
            "_now_iso",
            return_value=(observed + timedelta(minutes=10)).isoformat(),
        ):
            db.insert_events(
                [
                    self.event(
                        title="Shared timestamped report",
                        url="https://another.example.com/undated",
                        published_at=None,
                    )
                ]
            )

        verified = db.query_events(
            kol="huangrenxun",
            hours=24,
            time_status="verified",
            now=observed + timedelta(minutes=10),
        )

        self.assertEqual(len(verified), 1)
        self.assertEqual(
            verified[0]["source_url"], "https://example.com/reliable"
        )
        self.assertEqual(verified[0]["time_status"], "verified")

    def test_repeated_sighting_updates_count_and_last_seen(self) -> None:
        db.insert_events([self.event()])
        with db.conn() as connection:
            connection.execute(
                "UPDATE event_sightings SET last_seen_at='2000-01-01T00:00:00+00:00'"
            )

        db.insert_events([self.event()])

        with db.conn() as connection:
            row = connection.execute(
                "SELECT source_count, last_seen_at FROM event_sightings"
            ).fetchone()
        self.assertEqual(row["source_count"], 2)
        self.assertNotEqual(row["last_seen_at"], "2000-01-01T00:00:00+00:00")

    def test_event_source_count_tracks_distinct_urls_not_repeated_scans(self) -> None:
        db.insert_events([self.event()])
        db.insert_events([self.event()])
        db.insert_events([self.event()])

        with db.conn() as connection:
            event_count = connection.execute(
                "SELECT source_count FROM events"
            ).fetchone()["source_count"]
            observation_count = connection.execute(
                "SELECT source_count FROM event_sightings"
            ).fetchone()["source_count"]
        self.assertEqual(event_count, 1)
        self.assertEqual(observation_count, 3)

        db.insert_events(
            [
                self.event(
                    url="https://another.example.com/nvidia-platform",
                    source="Another News",
                )
            ]
        )
        with db.conn() as connection:
            event_count = connection.execute(
                "SELECT source_count FROM events"
            ).fetchone()["source_count"]
        self.assertEqual(event_count, 2)

    def test_init_repairs_inflated_event_source_counts(self) -> None:
        db.insert_events([self.event()])
        with db.conn() as connection:
            connection.execute("UPDATE events SET source_count=99")

        db.init()

        with db.conn() as connection:
            repaired = connection.execute(
                "SELECT source_count FROM events"
            ).fetchone()["source_count"]
        self.assertEqual(repaired, 1)

    def test_init_canonicalizes_and_merges_legacy_sighting_urls(self) -> None:
        db.insert_events([self.event()])
        with db.conn() as connection:
            event_id = connection.execute(
                "SELECT id FROM events"
            ).fetchone()["id"]
            connection.execute("DELETE FROM event_sightings")
            for suffix in ("first", "second"):
                connection.execute(
                    """
                    INSERT INTO event_sightings (
                      event_id, kol_key, kol_name, kol_name_cn, source,
                      source_url, published_at, first_seen_at, last_seen_at,
                      source_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        event_id,
                        "huangrenxun",
                        "Jensen Huang",
                        "黄仁勋",
                        "Bing News",
                        "https://example.com/nvidia-platform"
                        f"?utm_source={suffix}",
                        "2026-08-02T00:00:00+00:00",
                        "2026-08-02T01:00:00+00:00",
                        "2026-08-02T01:00:00+00:00",
                    ),
                )
            connection.execute("UPDATE events SET source_count=99")

        db.init()

        with db.conn() as connection:
            sightings = connection.execute(
                "SELECT source_url FROM event_sightings WHERE event_id=?",
                (event_id,),
            ).fetchall()
            source_count = connection.execute(
                "SELECT source_count FROM events WHERE id=?",
                (event_id,),
            ).fetchone()["source_count"]
        self.assertEqual(
            [row["source_url"] for row in sightings],
            ["https://example.com/nvidia-platform"],
        )
        self.assertEqual(source_count, 1)

    def test_existing_events_backfill_sightings_idempotently(self) -> None:
        db.insert_events([self.event()])
        with db.conn() as connection:
            connection.execute("DELETE FROM event_sightings")

        db.backfill_sightings()
        db.backfill_sightings()

        with db.conn() as connection:
            rows = connection.execute(
                "SELECT source_count FROM event_sightings"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_count"], 1)

    def test_backfill_treats_blank_sighting_published_at_as_missing(self) -> None:
        reliable = "2026-07-31T10:34:56+00:00"
        db.insert_events([self.event(published_at=reliable)])
        with db.conn() as connection:
            connection.execute("UPDATE event_sightings SET published_at=''")

        db.backfill_sightings()

        with db.conn() as connection:
            row = connection.execute(
                "SELECT published_at FROM event_sightings"
            ).fetchone()
        self.assertEqual(row["published_at"], reliable)

    def test_merge_recovers_truth_placeholder_when_real_text_arrives(self) -> None:
        url = "https://truthsocial.com/@realDonaldTrump/117051398671535118"
        shell = self.event(
            title="RT https://truthsocial.com/@realDonaldTrump",
            snippet="RT https://truthsocial.com/@realDonaldTrump",
            source="Truth Social @realDonaldTrump",
            url=url,
        )
        recovered = self.event(
            title="Tariff policy update",
            snippet=(
                "The administration will publish a semiconductor tariff "
                "decision after the review."
            ),
            source="Truth Social @realDonaldTrump",
            url=url,
        )

        db.insert_events([shell])
        db.insert_events([recovered])
        db.insert_events([shell])

        with db.conn() as connection:
            row = dict(
                connection.execute(
                    "SELECT title, snippet, source, canonical_url, url FROM events"
                ).fetchone()
            )

        self.assertEqual(row["title"], recovered["title"])
        self.assertEqual(row["snippet"], recovered["snippet"])
        self.assertTrue(llm_enrichment.is_event_enrichment_eligible(row))

    def test_merge_title_only_recovery_clears_stale_shell_snippet(self) -> None:
        url = "https://truthsocial.com/@realDonaldTrump/117051398671535119"
        db.insert_events(
            [
                self.event(
                    title="RT https://truthsocial.com/@realDonaldTrump",
                    snippet="RT https://truthsocial.com/@realDonaldTrump",
                    source="Truth Social @realDonaldTrump",
                    url=url,
                )
            ]
        )
        db.insert_events(
            [
                self.event(
                    title="WAR TARIFF ANNOUNCEMENT",
                    snippet="",
                    source="Truth Social @realDonaldTrump",
                    url=url,
                )
            ]
        )

        with db.conn() as connection:
            row = dict(
                connection.execute(
                    "SELECT title, snippet, source, canonical_url, url FROM events"
                ).fetchone()
            )

        self.assertEqual(row["title"], "WAR TARIFF ANNOUNCEMENT")
        self.assertEqual(row["snippet"], "")
        self.assertTrue(llm_enrichment.is_event_enrichment_eligible(row))

    def test_enrichment_claim_cache_input_change_and_retry_backoff(self) -> None:
        now = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
        db.insert_events(
            [self.event(published_at=(now - timedelta(hours=1)).isoformat())]
        )
        with db.conn() as connection:
            event_id = connection.execute("SELECT id FROM events").fetchone()["id"]

        claim = {
            "event_id": event_id,
            "input_hash": "a" * 64,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": "title_and_snippet",
        }
        first_token = db.claim_event_enrichment(**claim, now=now)
        self.assertIsInstance(first_token, str)
        self.assertIsNone(
            db.claim_event_enrichment(
                **claim,
                now=now + timedelta(minutes=1),
            )
        )
        self.assertTrue(
            db.save_event_enrichment(
                event_id,
                input_hash=claim["input_hash"],
                prompt_version=claim["prompt_version"],
                model=claim["model"],
                claim_token=first_token,
                evidence_basis=claim["evidence_basis"],
                result=ready_enrichment(),
                generated_at=(now + timedelta(minutes=2)).isoformat(),
            )
        )
        self.assertIsNone(
            db.claim_event_enrichment(
                **claim,
                now=now + timedelta(minutes=3),
            )
        )

        changed_claim = {**claim, "input_hash": "b" * 64}
        changed_token = db.claim_event_enrichment(
            **changed_claim,
            now=now + timedelta(minutes=3),
        )
        self.assertIsInstance(changed_token, str)
        rate_limit = llm_enrichment._response_error(429)
        self.assertTrue(
            db.fail_event_enrichment(
                event_id,
                input_hash=changed_claim["input_hash"],
                prompt_version=changed_claim["prompt_version"],
                model=changed_claim["model"],
                claim_token=changed_token,
                error_code=rate_limit.code,
                retry_after_seconds=rate_limit.retry_after_seconds,
                now=now + timedelta(minutes=3),
            )
        )
        self.assertIsNone(
            db.claim_event_enrichment(
                **changed_claim,
                now=now + timedelta(minutes=17),
            )
        )
        retry_token = db.claim_event_enrichment(
            **changed_claim,
            now=now + timedelta(minutes=19),
        )
        self.assertIsInstance(retry_token, str)

        authentication = llm_enrichment._response_error(401)
        self.assertTrue(
            db.fail_event_enrichment(
                event_id,
                input_hash=changed_claim["input_hash"],
                prompt_version=changed_claim["prompt_version"],
                model=changed_claim["model"],
                claim_token=retry_token,
                error_code=authentication.code,
                retry_after_seconds=authentication.retry_after_seconds,
                now=now + timedelta(minutes=19),
            )
        )
        self.assertIsNone(
            db.claim_event_enrichment(
                **changed_claim,
                now=now + timedelta(minutes=60),
            )
        )
        with db.conn() as connection:
            persisted = dict(
                connection.execute(
                    "SELECT * FROM event_enrichments WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            )
        serialized = json.dumps(persisted, sort_keys=True)
        self.assertEqual(persisted["status"], "retry")
        self.assertEqual(persisted["error_code"], "authentication")
        self.assertEqual(persisted["attempt_count"], 2)
        self.assertEqual(persisted["claim_token"], "")
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("deepseek-secret", serialized)

        # Authentication errors become retryable after configuration is fixed.
        recovered_token = db.claim_event_enrichment(
            **changed_claim,
            now=now + timedelta(minutes=80),
        )
        self.assertIsInstance(recovered_token, str)

    def test_macro_enrichment_claim_is_cache_aware_and_token_scoped(self) -> None:
        now = datetime(2026, 8, 6, 6, 0, tzinfo=timezone.utc)
        event_key = "indicator:0123456789abcdef01234567"
        base = {
            "event_key": event_key,
            "input_hash": "a" * 64,
            "prompt_version": llm_enrichment.MACRO_PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": "indicator_data",
        }

        first = db.claim_macro_event_enrichment(**base, now=now)
        self.assertIsInstance(first, tuple)
        assert first is not None
        first_token, first_attempt = first
        self.assertEqual(first_attempt, 1)
        self.assertIsNone(
            db.claim_macro_event_enrichment(
                **base,
                now=now + timedelta(minutes=1),
            )
        )

        changed = {**base, "input_hash": "b" * 64}
        replacement = db.claim_macro_event_enrichment(
            **changed,
            now=now + timedelta(minutes=1),
        )
        self.assertIsInstance(replacement, tuple)
        assert replacement is not None
        replacement_token, replacement_attempt = replacement
        self.assertEqual(replacement_attempt, 1)
        self.assertNotEqual(first_token, replacement_token)

        self.assertFalse(
            db.save_macro_event_enrichment(
                **base,
                claim_token=first_token,
                result=ready_enrichment(headline_zh="旧 worker 结果"),
            )
        )
        self.assertFalse(
            db.fail_macro_event_enrichment(
                event_key,
                input_hash=base["input_hash"],
                prompt_version=base["prompt_version"],
                model=base["model"],
                claim_token=first_token,
                error_code="provider secret must not overwrite",
                retry_after_seconds=60,
                now=now + timedelta(minutes=2),
            )
        )
        self.assertTrue(
            db.save_macro_event_enrichment(
                **changed,
                claim_token=replacement_token,
                result=ready_enrichment(headline_zh="当前指标解读"),
                generated_at=(now + timedelta(minutes=2)).isoformat(),
            )
        )

        exact = db.get_macro_event_enrichment(
            event_key,
            input_hash=changed["input_hash"],
            prompt_version=changed["prompt_version"],
            model=changed["model"],
        )
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact["status"], "ready")
        self.assertEqual(
            exact["ai_enrichment"]["headline_zh"],
            "当前指标解读",
        )
        self.assertIsNone(
            db.get_macro_event_enrichment(
                event_key,
                input_hash=base["input_hash"],
                prompt_version=changed["prompt_version"],
                model=changed["model"],
            )
        )
        self.assertIsNone(
            db.claim_macro_event_enrichment(
                **changed,
                now=now + timedelta(minutes=3),
            )
        )

    def test_macro_enrichment_prompt_or_model_change_invalidates_ready_cache(
        self,
    ) -> None:
        now = datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)
        event_key = "policy:0123456789abcdef01234567"
        base = {
            "event_key": event_key,
            "input_hash": "c" * 64,
            "prompt_version": llm_enrichment.MACRO_PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": "title_only",
        }
        claimed = db.claim_macro_event_enrichment(**base, now=now)
        assert claimed is not None
        self.assertTrue(
            db.save_macro_event_enrichment(
                **base,
                claim_token=claimed[0],
                result=ready_enrichment(),
                generated_at=now.isoformat(),
            )
        )

        next_prompt_version = f"{llm_enrichment.MACRO_PROMPT_VERSION}-next"
        new_prompt = db.claim_macro_event_enrichment(
            **{**base, "prompt_version": next_prompt_version},
            now=now + timedelta(seconds=1),
        )
        self.assertIsNotNone(new_prompt)
        assert new_prompt is not None
        self.assertEqual(new_prompt[1], 1)

        new_model = db.claim_macro_event_enrichment(
            **{
                **base,
                "prompt_version": next_prompt_version,
                "model": "deepseek-v4-pro",
            },
            now=now + timedelta(seconds=2),
        )
        self.assertIsNotNone(new_model)
        assert new_model is not None
        self.assertEqual(new_model[1], 1)

    def test_stale_claim_cannot_save_or_fail_a_new_owner(self) -> None:
        now = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
        db.insert_events(
            [self.event(published_at=(now - timedelta(hours=1)).isoformat())]
        )
        with db.conn() as connection:
            event_id = connection.execute("SELECT id FROM events").fetchone()["id"]
        base_claim = {
            "event_id": event_id,
            "input_hash": "a" * 64,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "evidence_basis": "title_and_snippet",
        }
        old_token = db.claim_event_enrichment(
            **base_claim,
            model="deepseek-v4-flash",
            now=now,
        )
        new_token = db.claim_event_enrichment(
            **base_claim,
            model="deepseek-v4-pro",
            now=now + timedelta(seconds=1),
        )
        self.assertIsInstance(old_token, str)
        self.assertIsInstance(new_token, str)
        self.assertNotEqual(old_token, new_token)

        self.assertFalse(
            db.save_event_enrichment(
                **base_claim,
                model="deepseek-v4-flash",
                claim_token=old_token,
                result=ready_enrichment(headline_zh="旧 worker 的结果"),
            )
        )
        self.assertFalse(
            db.fail_event_enrichment(
                event_id,
                input_hash=base_claim["input_hash"],
                prompt_version=base_claim["prompt_version"],
                model="deepseek-v4-flash",
                claim_token=old_token,
                error_code="provider_unavailable",
                retry_after_seconds=1200,
                now=now + timedelta(seconds=2),
            )
        )
        self.assertFalse(
            db.save_event_enrichment(
                **base_claim,
                model="deepseek-v4-flash",
                claim_token=new_token,
                result=ready_enrichment(headline_zh="错误模型的结果"),
            )
        )
        self.assertTrue(
            db.save_event_enrichment(
                **base_claim,
                model="deepseek-v4-pro",
                claim_token=new_token,
                result=ready_enrichment(headline_zh="新 owner 的结果"),
                generated_at=(now + timedelta(seconds=3)).isoformat(),
            )
        )
        with db.conn() as connection:
            row = connection.execute(
                "SELECT status, model, headline_zh, claim_token "
                "FROM event_enrichments WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["model"], "deepseek-v4-pro")
        self.assertEqual(row["headline_zh"], "新 owner 的结果")
        self.assertEqual(row["claim_token"], "")

    def test_event_input_change_revokes_live_claim_and_is_immediately_pending(
        self,
    ) -> None:
        now = datetime(2026, 8, 8, 4, 0, tzinfo=timezone.utc)
        original = self.event(
            title="NVIDIA announces an AI platform",
            snippet="NVIDIA announced a platform.",
            tickers=[],
            published_at=(now - timedelta(hours=1)).isoformat(),
        )
        db.insert_events([original])
        candidate = db.query_enrichment_candidates(now=now)[0]
        event_input, old_hash = llm_enrichment.build_event_input(candidate)
        claim = {
            "event_id": candidate["id"],
            "input_hash": old_hash,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": event_input["evidence_basis"],
        }
        old_token = db.claim_event_enrichment(**claim, now=now)
        self.assertIsInstance(old_token, str)

        db.insert_events(
            [
                {
                    **original,
                    "title": (
                        "NVIDIA announces an AI platform for enterprise "
                        "customers worldwide"
                    ),
                    "snippet": (
                        "NVIDIA announced the product, initial availability, "
                        "supported deployments, and enterprise rollout timing."
                    ),
                    "tickers": ["NVDA"],
                }
            ]
        )

        with db.conn() as connection:
            cache = dict(
                connection.execute(
                    "SELECT status, claim_token, next_attempt_at, error_code "
                    "FROM event_enrichments WHERE event_id=?",
                    (candidate["id"],),
                ).fetchone()
            )
        pending = db.query_enrichment_candidates(now=now)

        self.assertEqual(cache["status"], "pending")
        self.assertEqual(cache["claim_token"], "")
        self.assertIsNone(cache["next_attempt_at"])
        self.assertEqual(cache["error_code"], "")
        self.assertEqual([row["id"] for row in pending], [candidate["id"]])
        self.assertEqual(pending[0]["ai_status"], "pending")
        self.assertFalse(
            db.save_event_enrichment(
                **claim,
                claim_token=old_token,
                result=ready_enrichment(headline_zh="旧输入的迟到结果"),
            )
        )
        self.assertFalse(
            db.fail_event_enrichment(
                candidate["id"],
                input_hash=old_hash,
                prompt_version=llm_enrichment.PROMPT_VERSION,
                model=llm_enrichment.DEFAULT_MODEL,
                claim_token=old_token,
                error_code="provider_unavailable",
                retry_after_seconds=1200,
                now=now + timedelta(minutes=1),
            )
        )

        new_input, new_hash = llm_enrichment.build_event_input(pending[0])
        self.assertNotEqual(new_hash, old_hash)
        new_token = db.claim_event_enrichment(
            candidate["id"],
            input_hash=new_hash,
            prompt_version=llm_enrichment.PROMPT_VERSION,
            model=llm_enrichment.DEFAULT_MODEL,
            evidence_basis=new_input["evidence_basis"],
            now=now + timedelta(minutes=1),
        )
        self.assertIsInstance(new_token, str)
        with db.conn() as connection:
            attempt_count = connection.execute(
                "SELECT attempt_count FROM event_enrichments WHERE event_id=?",
                (candidate["id"],),
            ).fetchone()["attempt_count"]
        self.assertEqual(attempt_count, 1)

    def test_non_input_event_merge_preserves_live_enrichment_claim(self) -> None:
        now = datetime(2026, 8, 8, 5, 0, tzinfo=timezone.utc)
        original = self.event(
            snippet=None,
            published_at=(now - timedelta(hours=1)).isoformat()
        )
        db.insert_events([original])
        candidate = db.query_enrichment_candidates(now=now)[0]
        event_input, input_hash = llm_enrichment.build_event_input(candidate)
        token = db.claim_event_enrichment(
            candidate["id"],
            input_hash=input_hash,
            prompt_version=llm_enrichment.PROMPT_VERSION,
            model=llm_enrichment.DEFAULT_MODEL,
            evidence_basis=event_input["evidence_basis"],
            now=now,
        )

        db.insert_events([{**original, "snippet": "", "impact": "high"}])

        with db.conn() as connection:
            cache = connection.execute(
                "SELECT status, claim_token FROM event_enrichments WHERE event_id=?",
                (candidate["id"],),
            ).fetchone()
        self.assertEqual(cache["status"], "processing")
        self.assertEqual(cache["claim_token"], token)

    def test_candidate_pool_skips_live_backoff_without_starving_ready_rows(self) -> None:
        now = datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc)
        titles = (
            "Unclaimed enrichment candidate",
            "Ready enrichment candidate",
            "Future retry enrichment candidate",
            "Live processing enrichment candidate",
            "Failed enrichment candidate",
        )
        for index, title in enumerate(titles):
            db.insert_events(
                [
                    self.event(
                        title=title,
                        url=f"https://example.com/enrichment-{index}",
                        published_at=(now - timedelta(hours=1)).isoformat(),
                    )
                ]
            )
        with db.conn() as connection:
            event_ids = {
                row["title"]: row["id"]
                for row in connection.execute("SELECT id, title FROM events")
            }

        def claim(title: str) -> tuple[dict[str, object], str]:
            params: dict[str, object] = {
                "event_id": event_ids[title],
                "input_hash": str(event_ids[title]).zfill(64),
                "prompt_version": llm_enrichment.PROMPT_VERSION,
                "model": llm_enrichment.DEFAULT_MODEL,
                "evidence_basis": "title_only",
            }
            token = db.claim_event_enrichment(**params, now=now)
            self.assertIsInstance(token, str)
            return params, token

        ready_claim, ready_token = claim("Ready enrichment candidate")
        self.assertTrue(
            db.save_event_enrichment(
                **ready_claim,
                claim_token=ready_token,
                result=ready_enrichment(),
                generated_at=now.isoformat(),
            )
        )
        retry_claim, retry_token = claim("Future retry enrichment candidate")
        self.assertTrue(
            db.fail_event_enrichment(
                retry_claim["event_id"],
                input_hash=retry_claim["input_hash"],
                prompt_version=retry_claim["prompt_version"],
                model=retry_claim["model"],
                claim_token=retry_token,
                error_code="rate_limit",
                retry_after_seconds=3600,
                now=now,
            )
        )
        claim("Live processing enrichment candidate")
        failed_claim, failed_token = claim("Failed enrichment candidate")
        self.assertTrue(
            db.fail_event_enrichment(
                failed_claim["event_id"],
                input_hash=failed_claim["input_hash"],
                prompt_version=failed_claim["prompt_version"],
                model=failed_claim["model"],
                claim_token=failed_token,
                error_code="invalid_output",
                retry_after_seconds=None,
                now=now,
            )
        )

        first_page = db.query_enrichment_candidates(now=now, limit=2)
        due_later = db.query_enrichment_candidates(
            now=now + timedelta(hours=2),
            limit=10,
        )

        self.assertEqual(
            [row["title"] for row in first_page],
            ["Unclaimed enrichment candidate", "Ready enrichment candidate"],
        )
        self.assertEqual(
            {row["title"] for row in due_later},
            set(titles),
        )
        self.assertEqual(due_later[-1]["title"], "Failed enrichment candidate")

    def test_ready_enrichment_is_nested_in_event_queries_and_searchable(self) -> None:
        now = datetime(2026, 8, 6, 5, 0, tzinfo=timezone.utc)
        db.insert_events(
            [self.event(published_at=(now - timedelta(hours=1)).isoformat())]
        )
        candidate = db.query_enrichment_candidates(now=now)[0]
        event_input, input_hash = llm_enrichment.build_event_input(candidate)
        claim = {
            "event_id": candidate["id"],
            "input_hash": input_hash,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": event_input["evidence_basis"],
        }

        claim_token = db.claim_event_enrichment(**claim, now=now)
        self.assertIsInstance(claim_token, str)
        self.assertTrue(
            db.save_event_enrichment(
                **claim,
                claim_token=claim_token,
                result=ready_enrichment(),
                generated_at=now.isoformat(),
            )
        )
        db.insert_events(
            [
                {
                    **self.event(
                        published_at=(now - timedelta(minutes=30)).isoformat()
                    ),
                    "url": "https://x.com/elonmusk/status/987",
                    "source": "X @elonmusk",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                }
            ]
        )

        deterministic_items = db.query_events(hours=24, now=now)
        public_items = db.query_events(hours=24, now=now, use_ai_impact=True)
        searched = db.query_events(q="人工智能", hours=24, now=now)
        rule_high = db.query_events(impact="high", hours=24, now=now)
        ai_high = db.query_events(
            impact="high",
            hours=24,
            now=now,
            use_ai_impact=True,
        )
        musk_high = db.query_events(
            kol="musk",
            impact="high",
            hours=24,
            now=now,
            use_ai_impact=True,
        )

        self.assertEqual(len(deterministic_items), 1)
        deterministic = deterministic_items[0]
        public = public_items[0]
        self.assertEqual(deterministic["rule_impact"], "medium")
        self.assertEqual(deterministic["impact"], "medium")
        self.assertEqual(public["impact"], "high")
        self.assertEqual(public["ai_status"], "ready")
        self.assertEqual(public["ai_enrichment"]["status"], "ready")
        self.assertEqual(public["ai_enrichment"]["tags"], ["人工智能", "半导体"])
        self.assertEqual(
            public["ai_enrichment"]["assets"][0]["asset_key"],
            "US:NVDA",
        )
        self.assertEqual(public["ai_enrichment"]["model"], "deepseek-v4-flash")
        self.assertNotIn("ai_summary_zh", public)
        self.assertNotIn("ai_tags_json", public)
        self.assertEqual([row["id"] for row in searched], [candidate["id"]])
        self.assertEqual(rule_high, [])
        self.assertEqual([row["id"] for row in ai_high], [candidate["id"]])
        self.assertEqual(len(musk_high), 1)
        self.assertEqual(musk_high[0]["impact"], "high")
        self.assertEqual(musk_high[0]["ai_status"], "ready")
        self.assertEqual(musk_high[0]["kol_key"], "musk")

    def test_stale_enrichment_is_fail_closed_after_event_evidence_changes(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        original = self.event(
            published_at=(now - timedelta(hours=1)).isoformat()
        )
        db.insert_events([original])
        candidate = db.query_enrichment_candidates(now=now)[0]
        event_input, input_hash = llm_enrichment.build_event_input(candidate)
        claim = {
            "event_id": candidate["id"],
            "input_hash": input_hash,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": llm_enrichment.DEFAULT_MODEL,
            "evidence_basis": event_input["evidence_basis"],
        }
        token = db.claim_event_enrichment(**claim, now=now)
        self.assertIsInstance(token, str)
        self.assertTrue(
            db.save_event_enrichment(
                **claim,
                claim_token=token,
                result=ready_enrichment(),
                generated_at=now.isoformat(),
            )
        )
        ready_summary = db.stats(hours=24)
        ready_kols = db.list_kols()
        self.assertEqual(ready_summary["high"], 1)
        self.assertEqual(ready_summary["medium"], 0)
        self.assertEqual(ready_kols[0]["high_24h"], 1)

        db.insert_events(
            [
                {
                    **original,
                    "title": (
                        "NVIDIA launches a new AI platform with expanded "
                        "enterprise availability"
                    ),
                    "snippet": (
                        "NVIDIA described the platform, its enterprise rollout, "
                        "customer eligibility, timing, and supported deployment "
                        "options in a substantially fuller source update."
                    ),
                }
            ]
        )

        public = db.query_events(hours=24, now=now, use_ai_impact=True)
        stale_search = db.query_events(q="人工智能", hours=24, now=now)
        stale_high = db.query_events(
            impact="high",
            hours=24,
            now=now,
            use_ai_impact=True,
        )
        summary = db.stats(hours=24)
        kols = db.list_kols()

        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]["ai_status"], "pending")
        self.assertIsNone(public[0]["ai_enrichment"])
        self.assertEqual(public[0]["impact"], "medium")
        self.assertEqual(stale_search, [])
        self.assertEqual(stale_high, [])
        self.assertEqual(summary["high"], 0)
        self.assertEqual(summary["medium"], 1)
        self.assertEqual(kols[0]["high_24h"], 0)

    def test_supported_worker_model_is_valid_without_web_model_env(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_events(
            [self.event(published_at=(now - timedelta(hours=1)).isoformat())]
        )
        candidate = db.query_enrichment_candidates(now=now)[0]
        event_input, input_hash = llm_enrichment.build_event_input(candidate)
        claim = {
            "event_id": candidate["id"],
            "input_hash": input_hash,
            "prompt_version": llm_enrichment.PROMPT_VERSION,
            "model": "deepseek-v4-pro",
            "evidence_basis": event_input["evidence_basis"],
        }
        token = db.claim_event_enrichment(**claim, now=now)
        self.assertIsInstance(token, str)
        self.assertTrue(
            db.save_event_enrichment(
                **claim,
                claim_token=token,
                result=ready_enrichment(),
                generated_at=now.isoformat(),
            )
        )

        with mock.patch.dict(
            llm_enrichment.os.environ,
            {"DEEPSEEK_MODEL": "deepseek-v4-flash"},
        ):
            public = db.query_events(hours=24, now=now, use_ai_impact=True)

        self.assertEqual(public[0]["ai_status"], "ready")
        self.assertEqual(public[0]["impact"], "high")
        self.assertEqual(
            public[0]["ai_enrichment"]["model"],
            "deepseek-v4-pro",
        )

    def test_ai_none_only_downgrades_confident_medium_rule_impact(self) -> None:
        now = datetime(2026, 8, 6, 5, 30, tzinfo=timezone.utc)
        cases = (
            ("Rule high stays high", "high", 0.99),
            ("Low confidence medium stays medium", "medium", 0.64),
            ("Confident medium becomes low", "medium", 0.65),
        )
        for index, (title, impact, _confidence) in enumerate(cases):
            db.insert_events(
                [
                    self.event(
                        title=title,
                        url=f"https://example.com/ai-none-{index}",
                        impact=impact,
                        published_at=(now - timedelta(hours=1)).isoformat(),
                    )
                ]
            )
        with db.conn() as connection:
            event_ids = {
                row["title"]: row["id"]
                for row in connection.execute("SELECT id, title FROM events")
            }
        candidates_by_id = {
            item["id"]: item
            for item in db.query_enrichment_candidates(now=now)
        }

        for title, _impact, confidence in cases:
            event_id = event_ids[title]
            event_input, input_hash = llm_enrichment.build_event_input(
                candidates_by_id[event_id]
            )
            claim = {
                "event_id": event_id,
                "input_hash": input_hash,
                "prompt_version": llm_enrichment.PROMPT_VERSION,
                "model": llm_enrichment.DEFAULT_MODEL,
                "evidence_basis": event_input["evidence_basis"],
            }
            claim_token = db.claim_event_enrichment(**claim, now=now)
            self.assertIsInstance(claim_token, str)
            self.assertTrue(
                db.save_event_enrichment(
                    **claim,
                    claim_token=claim_token,
                    result=ready_enrichment(
                        impact_level="none",
                        confidence=confidence,
                        cluster_key=f"ai-none-{event_id}",
                    ),
                    generated_at=now.isoformat(),
                )
            )

        deterministic = {
            row["title"]: row
            for row in db.query_events(hours=24, now=now)
        }
        public = {
            row["title"]: row
            for row in db.query_events(
                hours=24,
                now=now,
                use_ai_impact=True,
            )
        }

        self.assertEqual(deterministic["Rule high stays high"]["impact"], "high")
        self.assertEqual(public["Rule high stays high"]["impact"], "high")
        self.assertEqual(
            public["Low confidence medium stays medium"]["impact"],
            "medium",
        )
        self.assertEqual(public["Confident medium becomes low"]["impact"], "low")
        self.assertEqual(
            public["Confident medium becomes low"]["ai_enrichment"]["impact_level"],
            "none",
        )
        self.assertEqual(
            {
                row["title"]
                for row in db.query_events(
                    impact="low",
                    hours=24,
                    now=now,
                    use_ai_impact=True,
                )
            },
            {"Confident medium becomes low"},
        )
        high_detail = db.get_event_detail(event_ids["Rule high stays high"])
        low_detail = db.get_event_detail(event_ids["Confident medium becomes low"])
        self.assertIsNotNone(high_detail)
        self.assertIsNotNone(low_detail)
        assert high_detail is not None
        assert low_detail is not None
        self.assertEqual(high_detail["event"]["impact"], "high")
        self.assertEqual(low_detail["event"]["impact"], "low")

    def test_event_detail_includes_every_sighting_and_related_cluster(self) -> None:
        now = datetime(2026, 8, 6, 6, 0, tzinfo=timezone.utc)
        db.insert_events(
            [
                self.event(
                    title="NVIDIA launches enterprise AI platform",
                    url="https://example.com/nvidia-enterprise-platform",
                    published_at=(now - timedelta(hours=2)).isoformat(),
                )
            ]
        )
        db.insert_events(
            [
                self.event(
                    title="NVIDIA launches enterprise AI platform",
                    url="https://x.com/elonmusk/status/123",
                    source="X @elonmusk",
                    kol_key="musk",
                    kol_name="Elon Musk",
                    kol_name_cn="马斯克",
                    published_at=(now - timedelta(hours=1)).isoformat(),
                )
            ]
        )
        db.insert_events(
            [
                self.event(
                    title="Partners adopt NVIDIA platform worldwide",
                    url="https://example.com/nvidia-partners",
                    source="Reuters",
                    kol_key="analyst",
                    kol_name="Analyst",
                    kol_name_cn="分析师",
                    published_at=(now - timedelta(minutes=30)).isoformat(),
                )
            ]
        )
        candidates = db.query_enrichment_candidates(now=now)
        ids_by_title = {item["title"]: item["id"] for item in candidates}
        first_id = ids_by_title["NVIDIA launches enterprise AI platform"]
        related_id = ids_by_title["Partners adopt NVIDIA platform worldwide"]

        for candidate in candidates:
            event_input, input_hash = llm_enrichment.build_event_input(candidate)
            claim = {
                "event_id": candidate["id"],
                "input_hash": input_hash,
                "prompt_version": llm_enrichment.PROMPT_VERSION,
                "model": llm_enrichment.DEFAULT_MODEL,
                "evidence_basis": event_input["evidence_basis"],
            }
            claim_token = db.claim_event_enrichment(**claim, now=now)
            self.assertIsInstance(claim_token, str)
            self.assertTrue(
                db.save_event_enrichment(
                    **claim,
                    claim_token=claim_token,
                    result=ready_enrichment(
                        headline_zh=f"事件 {candidate['id']}",
                        cluster_key="nvidia-enterprise-ai-platform",
                    ),
                    generated_at=now.isoformat(),
                )
            )

        detail = db.get_event_detail(first_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["event"]["id"], first_id)
        self.assertEqual(detail["event"]["ai_status"], "ready")
        self.assertEqual(len(detail["sightings"]), 2)
        self.assertEqual(detail["sightings"][0]["kol_key"], "musk")
        self.assertEqual(
            {sighting["source_url"] for sighting in detail["sightings"]},
            {
                "https://example.com/nvidia-enterprise-platform",
                "https://x.com/elonmusk/status/123",
            },
        )
        self.assertEqual([item["id"] for item in detail["related"]], [related_id])
        self.assertEqual(detail["related"][0]["headline_zh"], f"事件 {related_id}")
        self.assertIsNone(db.get_event_detail(999_999))

    def test_replace_relations_is_idempotent_and_updates_payload(self) -> None:
        edge = {
            "topic_key": "ai_semiconductors",
            "asset_key": "US:NVDA",
            "relation_type": "view",
            "direction": "positive",
            "strength": 0.7,
            "confidence": 0.8,
            "horizon": "medium",
            "method": "deterministic_rules:test",
            "rationale": "initial",
            "evidence": {"text": "bullish on NVIDIA"},
        }
        db.replace_relations("event", "42", [edge])
        db.replace_relations(
            "event",
            "42",
            [{**edge, "rationale": "updated", "evidence": {"text": "buy $NVDA"}}],
        )

        rows = db.query_relations(source_type="event", source_id="42")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rationale"], "updated")
        self.assertEqual(json.loads(rows[0]["evidence_json"])["text"], "buy $NVDA")

    def test_replace_relations_parses_json_evidence_strings_safely(self) -> None:
        base = {
            "topic_key": "ai_semiconductors",
            "asset_key": "US:NVDA",
            "relation_type": "view",
            "direction": "positive",
            "strength": 0.7,
            "confidence": 0.8,
            "horizon": "medium",
            "method": "deterministic_rules:test",
        }
        db.replace_relations(
            "event", "valid-json", [{**base, "evidence": '{"claim":"buy"}'}]
        )
        db.replace_relations(
            "event", "invalid-json", [{**base, "evidence": "{not json"}]
        )

        valid = db.query_relations(
            source_type="event", source_id="valid-json"
        )[0]
        invalid = db.query_relations(
            source_type="event", source_id="invalid-json"
        )[0]
        self.assertEqual(json.loads(valid["evidence_json"]), {"claim": "buy"})
        self.assertEqual(json.loads(invalid["evidence_json"]), "{not json")

    def test_replace_relations_preserves_ids_and_deletes_only_stale_edges(self) -> None:
        def edge(asset_key: str) -> dict:
            return {
                "topic_key": "ai_semiconductors",
                "asset_key": asset_key,
                "relation_type": "view",
                "direction": "positive",
                "strength": 0.7,
                "confidence": 0.8,
                "horizon": "medium",
                "method": "deterministic_rules:test",
                "evidence": {"asset": asset_key},
            }

        db.replace_relations("event", "stable", [edge("US:NVDA"), edge("US:AMD")])
        with db.conn() as connection:
            before = {
                row["asset_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id, asset_key FROM relations WHERE source_id='stable'"
                )
            }

        db.replace_relations(
            "event",
            "stable",
            [
                {**edge("US:NVDA"), "rationale": "updated"},
                edge("US:TSM"),
            ],
        )

        with db.conn() as connection:
            after = {
                row["asset_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id, asset_key FROM relations WHERE source_id='stable'"
                )
            }
        self.assertEqual(after["US:NVDA"], before["US:NVDA"])
        self.assertNotIn("US:AMD", after)
        self.assertIn("US:TSM", after)

    def test_market_validation_keeps_event_relations_for_fourteen_days(
        self,
    ) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        published_at = (now - timedelta(days=10)).isoformat()
        db.insert_events(
            [
                self.event(
                    title="Ten day old event",
                    url="https://example.com/ten-day-event",
                    published_at=published_at,
                )
            ]
        )
        with db.conn() as connection:
            event_id = str(
                connection.execute("SELECT id FROM events").fetchone()["id"]
            )
        db.replace_relations(
            "event",
            event_id,
            [
                {
                    "source_type": "event",
                    "source_id": event_id,
                    "topic_key": "ai_semiconductors",
                    "asset_key": "US:NVDA",
                    "relation_type": "view",
                    "direction": "positive",
                    "strength": 0.8,
                    "confidence": 0.8,
                    "horizon": "medium",
                    "method": "deterministic_rules:test",
                    "rationale": "public rationale",
                    "evidence": {
                        "title": "Ten day old event",
                        "published_at": published_at,
                    },
                }
            ],
        )

        decisions = db.query_decision_relations(now=now)
        validation = db.query_market_validation_relations(
            now=now,
            event_max_age_hours=14 * 24,
        )

        self.assertEqual(decisions, [])
        self.assertEqual(len(validation), 1)
        self.assertEqual(validation[0]["source_id"], event_id)

    def test_macro_relations_round_trip_preserves_each_edge_source_group(self) -> None:
        payload = {
            "snapshot_id": 21,
            "black_swan_scenarios": [
                {
                    "id": "demand",
                    "name": "AI demand shock",
                    "affected_assets": ["NVDA"],
                    "probability": "medium",
                    "impact": "high",
                },
                {
                    "id": "supply",
                    "name": "AI supply shock",
                    "affected_assets": ["NVDA"],
                    "probability": "medium",
                    "impact": "high",
                },
            ],
        }
        edges = relation_engine.macro_relations(payload)

        db.replace_relations("macro_snapshot", "21", edges)
        db.replace_relations("macro_snapshot", "21", [edges[0]])

        rows = db.query_relations(source_type="macro_snapshot")
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["source_id"] for row in rows},
            {edge["source_id"] for edge in edges},
        )
        self.assertEqual(
            {
                json.loads(row["evidence_json"])["item_id"]
                for row in rows
            },
            {"demand", "supply"},
        )

    def test_decisions_use_only_the_latest_macro_snapshot_relations(self) -> None:
        def report(timestamp: str) -> dict:
            return {
                "public_schema_version": 1,
                "timestamp": timestamp,
                "composite_risk": {"score": 50, "level": "medium"},
            }

        first_id = db.save_macro_snapshot(report("2026-08-02T00:00:00+00:00"))
        second_id = db.save_macro_snapshot(report("2026-08-03T00:00:00+00:00"))

        def edge(snapshot_id: int) -> dict:
            return {
                "source_type": "macro_snapshot",
                "source_id": f"{snapshot_id}:gray_rhino:credit",
                "topic_key": "credit_stress",
                "asset_key": "US:HYG",
                "relation_type": "structural_risk",
                "direction": "negative",
                "strength": 0.8,
                "confidence": 0.8,
                "horizon": "medium",
                "method": "deterministic_rules:test",
                "rationale": "public rationale",
                "evidence": {"name": "Credit stress"},
            }

        db.replace_relations("macro_snapshot", str(first_id), [edge(first_id)])
        db.replace_relations("macro_snapshot", str(second_id), [edge(second_id)])

        rows = db.query_decision_relations()

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["source_id"],
            f"{second_id}:gray_rhino:credit",
        )

    def test_stats_coerces_hours_to_int_and_rejects_invalid_input(self) -> None:
        self.assertEqual(db.stats("24")["hours"], 24)
        with self.assertRaises((TypeError, ValueError)):
            db.stats("24 hours') OR 1=1 --")

    def test_stats_and_kol_activity_use_verified_publication_time(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_events(
            [
                self.event(
                    title="Fresh verified signal",
                    url="https://example.com/fresh-signal",
                    kol_key="fresh",
                    kol_name="Fresh",
                    kol_name_cn="新鲜",
                    impact="high",
                    published_at=(now - timedelta(hours=1)).isoformat(),
                ),
                self.event(
                    title="Old signal observed today",
                    url="https://example.com/old-signal",
                    kol_key="old",
                    kol_name="Old",
                    kol_name_cn="旧闻",
                    published_at=(now - timedelta(days=400)).isoformat(),
                ),
                self.event(
                    title="Undated signal",
                    url="https://example.com/undated-signal",
                    kol_key="undated",
                    kol_name="Undated",
                    kol_name_cn="时间未知",
                    published_at=None,
                ),
            ]
        )

        stats = db.stats(24)
        by_kol = {row["kol_key"]: row for row in db.list_kols()}

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["high"], 1)
        self.assertEqual(stats["active_kols"], 1)
        self.assertEqual(by_kol["fresh"]["total_24h"], 1)
        self.assertEqual(by_kol["old"]["total_24h"], 0)
        self.assertEqual(by_kol["undated"]["total_24h"], 0)


class LegacyMigrationTests(unittest.TestCase):
    def test_market_reaction_pending_migration_is_atomic_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy-market.sqlite3")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE market_reactions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  asset_key TEXT NOT NULL,
                  window TEXT NOT NULL,
                  benchmark_asset_key TEXT,
                  asset_return REAL,
                  benchmark_return REAL,
                  abnormal_return REAL,
                  expected_direction TEXT,
                  observed_direction TEXT,
                  direction_confirmed INTEGER,
                  status TEXT NOT NULL CHECK(
                    status IN ('preliminary', 'complete', 'unavailable')
                  ),
                  sample_count INTEGER NOT NULL DEFAULT 0,
                  data_timestamps_json TEXT NOT NULL DEFAULT '{}',
                  method_version TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  UNIQUE(
                    source_type, source_id, asset_key, window, method_version
                  )
                )
                """
            )
            connection.execute(
                "CREATE INDEX idx_market_reaction_source "
                "ON market_reactions(source_type, source_id)"
            )
            connection.execute(
                """
                INSERT INTO market_reactions (
                  source_type, source_id, asset_key, window, status,
                  sample_count, method_version, observed_at
                ) VALUES (
                  'event', 'legacy', 'US:SPY', '3D', 'unavailable',
                  0, 'common_trading_days:test', '2026-08-08T00:00:00+00:00'
                )
                """
            )
            connection.commit()
            connection.close()

            with mock.patch.object(db, "DB_PATH", path):
                db.init()
                db.init()
                db.upsert_market_reaction(
                    "event",
                    "legacy",
                    "US:SPY",
                    {
                        "window": "3D",
                        "status": "pending",
                        "sample_count": 0,
                        "data_timestamps": {},
                        "method_version": "common_trading_days:test",
                        "provider": "yahoo",
                        "provider_symbol": "SPY",
                        "asset_status": "available",
                        "benchmark_status": "available",
                        "reason_code": "window_not_due",
                        "next_due_at": "2026-08-10T00:00:00+00:00",
                    },
                )
                with db.conn() as migrated:
                    table_sql = migrated.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name='market_reactions'"
                    ).fetchone()["sql"]
                    columns = {
                        row["name"]
                        for row in migrated.execute(
                            "PRAGMA table_info(market_reactions)"
                        ).fetchall()
                    }
                    integrity = migrated.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                row = db.query_market_reactions(source_id="legacy")[0]

            self.assertIn("'pending'", table_sql)
            self.assertTrue(
                {
                    "provider",
                    "provider_symbol",
                    "proxy_for",
                    "asset_status",
                    "benchmark_status",
                    "reason_code",
                    "next_due_at",
                }.issubset(columns)
            )
            self.assertEqual(integrity, "ok")
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["reason_code"], "window_not_due")

    def test_duplicate_legacy_events_keep_both_kols_and_reliable_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE events (
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
                  published_at TEXT
                )
                """
            )
            rows = [
                (
                    "hash-one",
                    "https://one.example/story",
                    "The same market-moving story",
                    "Bing News",
                    "musk",
                    "Elon Musk",
                    "马斯克",
                    "",
                ),
                (
                    "hash-two",
                    "https://two.example/story",
                    "The same market-moving story",
                    "Bing News",
                    "huangrenxun",
                    "Jensen Huang",
                    "黄仁勋",
                    "2026-07-31T10:34:56+00:00",
                ),
            ]
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO events (
                      url_hash, url, title, source, kol_key, kol_name, kol_name_cn,
                      fetched_at, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-01T00:00:00+00:00', ?)
                    """,
                    row,
                )
            connection.commit()
            connection.close()

            with mock.patch.object(db, "DB_PATH", path):
                db.init()
                db.init()
                with db.conn() as migrated:
                    events = migrated.execute(
                        "SELECT source_count, published_at FROM events"
                    ).fetchall()
                    sightings = migrated.execute(
                        "SELECT kol_key FROM event_sightings ORDER BY kol_key"
                    ).fetchall()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source_count"], 2)
            self.assertEqual(
                events[0]["published_at"], "2026-07-31T10:34:56+00:00"
            )
            self.assertEqual(
                [row["kol_key"] for row in sightings],
                ["huangrenxun", "musk"],
            )

    def test_failed_rekey_restores_unique_dedup_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "migration-failure.sqlite3")
            with mock.patch.object(db, "DB_PATH", path):
                db.init()
                db.insert_events(
                    [
                        DatabaseTests.event(
                            title="Atomic migration test",
                            url="https://example.com/atomic",
                        )
                    ]
                )
                with db.conn() as connection:
                    db.set_meta_in(connection, "dedup_algo_version", "old")

                with mock.patch.object(
                    db, "_merge_prefix_dupes", side_effect=RuntimeError("injected")
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        db.backfill_dedup()

                with db.conn() as connection:
                    index = connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='index' AND name='idx_dedup'"
                    ).fetchone()
                self.assertIsNotNone(index)

    def test_init_uses_immediate_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "locked-migration.sqlite3")
            statements: list[str] = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection

            with mock.patch.object(db, "DB_PATH", path):
                with mock.patch.object(
                    db.sqlite3, "connect", side_effect=traced_connect
                ):
                    db.init()

            self.assertTrue(
                any(
                    statement.strip().upper() == "BEGIN IMMEDIATE"
                    for statement in statements
                )
            )

    def test_decision_snapshot_round_trip_versioning_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "decision-snapshot.sqlite3")
            with mock.patch.object(db, "DB_PATH", path):
                db.init()
                for index in range(3):
                    snapshot_id = db.save_decision_snapshot(
                        schema_version=1,
                        engine_version="decision-test-v1",
                        source_hash=f"source-{index}",
                        source_as_of=f"2026-08-08T0{index}:00:00+00:00",
                        generated_at=f"2026-08-08T0{index}:00:00+00:00",
                        summary={"decisions": [], "index": index},
                        full={
                            "decisions": [],
                            "index": index,
                            "evidence_policy": "test",
                        },
                        keep=2,
                    )

                latest = db.latest_decision_snapshot(
                    schema_version=1,
                    engine_version="decision-test-v1",
                )
                self.assertEqual(latest["snapshot_id"], snapshot_id)
                self.assertEqual(latest["summary"]["index"], 2)
                self.assertEqual(latest["full"]["index"], 2)
                self.assertIsNone(
                    db.latest_decision_snapshot(
                        schema_version=2,
                        engine_version="decision-test-v1",
                    )
                )
                with db.conn() as connection:
                    rows = connection.execute(
                        "SELECT id FROM decision_snapshots ORDER BY id"
                    ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertIsNone(
                    db.get_decision_snapshot(
                        rows[0]["id"] - 1,
                        schema_version=1,
                        engine_version="decision-test-v1",
                    )
                )


if __name__ == "__main__":
    unittest.main()
