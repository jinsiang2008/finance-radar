from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kol_dashboard import db, relation_engine


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


if __name__ == "__main__":
    unittest.main()
