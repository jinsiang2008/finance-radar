from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import app
import db


class NewsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.directory.name) / "test.db")
        self.db_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.db_patch.stop()
        self.directory.cleanup()

    async def test_empty_public_api_is_honest_without_network(self):
        with patch.object(app.news_enrichment.llm_enrichment, "complete_json_with_usage", side_effect=AssertionError("HTTP must not call model")):
            response = await self.client.get("/api/news")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(len(response.json()["sources"]), 5)
        self.assertIn("public", response.headers["cache-control"])
        self.assertFalse(response.json()["analysis_schedule"]["configured"])
        self.assertFalse(Path(db.DB_PATH).exists(), "public reads must not create a database")

    async def test_api_reads_persisted_collector_rows_from_configured_database(self):
        app.news_service.init_news_db(db.DB_PATH)
        published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        feed = ("<rss><channel><item><title>Copper supply update</title>"
                "<link>https://www.bloomberg.com/news/articles/copper-test</link>"
                "<description>Production remains uncertain.</description><pubDate>" + published +
                "</pubDate></item></channel></rss>").encode()
        app.news_service.store_feed(app.news_service.SOURCES[0], {"status": 200, "body": feed}, db_path=db.DB_PATH)
        response = await self.client.get("/api/news?commodity=copper")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["title"], "Copper supply update")
        self.assertEqual(response.json()["items"][0]["analysis"]["status"], "unavailable")

    async def test_filters_are_validated_before_query(self):
        for query in ("limit=0", "limit=201", "commodity=all", "publisher=untrusted", "commodity=%27%20OR%201%3D1", "topic=untrusted", "instrument=FAKE:123"):
            with self.subTest(query=query):
                response = await self.client.get("/api/news?" + query)
                self.assertEqual(response.status_code, 422)

    async def test_valid_filters_forwarded_and_no_private_data(self):
        with patch.object(app.news_service, "get_news_snapshot", return_value={"items": [], "sources": []}) as snapshot:
            db.init()
            response = await self.client.get("/api/news?commodity=copper&publisher=Bloomberg&limit=12")
        self.assertEqual(response.status_code, 200)
        snapshot.assert_called_once_with(limit=12, commodity="copper", publisher="Bloomberg", topic=None, instrument=None, asset=None, db_path=db.DB_PATH)
        self.assertNotIn("holdings", response.text)

    async def test_context_reads_are_public_bounded_and_do_not_rebuild_decisions(self):
        with patch.object(app.decision_snapshot, "ensure_public_snapshot", side_effect=AssertionError("read only")):
            for view in ("home", "daily", "macro", "kol", "asset&asset=US:SPCX"):
                response = await self.client.get("/api/news/context?view=" + view)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"], [])
                self.assertIn("analysis_schedule", response.json())
                self.assertIn("public", response.headers["cache-control"])
        self.assertFalse(Path(db.DB_PATH).exists())

    async def test_context_requires_asset_and_rejects_unrecognized_people(self):
        for query in ("view=asset", "view=notreal", "view=kol&kols=notreal", "view=home&limit=21"):
            response = await self.client.get("/api/news/context?" + query)
            self.assertEqual(response.status_code, 422)
        response = await self.client.get("/api/news/context?view=asset&asset=US:NOTCONFIGURED")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)

    async def test_kol_context_keeps_media_attribution_and_filters_people(self):
        app.news_service.init_news_db(db.DB_PATH)
        published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        feed = ("<rss><channel><item><title>Elon Musk says SpaceX revenue is growing</title>"
                "<link>https://www.bloomberg.com/news/articles/musk-test</link>"
                "<description>SpaceX reported growing satellite revenue and investment.</description><pubDate>" + published +
                "</pubDate></item></channel></rss>").encode()
        app.news_service.store_feed(app.news_service.SOURCES[0], {"status": 200, "body": feed}, db_path=db.DB_PATH)
        response = await self.client.get("/api/news/context?view=kol&kols=musk")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["publisher"], "Bloomberg")
        self.assertEqual(item["kol_mentions"][0]["key"], "musk")
        self.assertEqual(item["kol_mentions"][0]["attribution_basis"], "person_mention")
        other = await self.client.get("/api/news/context?view=kol&kols=buffett")
        self.assertEqual(other.json()["items"], [])

    async def test_independent_media_failure_does_not_break_daily_adapter_input(self):
        import sqlite3
        with patch.object(app.news_service, "get_news_snapshot", side_effect=sqlite3.OperationalError("private diagnostic")):
            response = await self.client.get("/api/news/context")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private diagnostic", response.text)
            self.assertEqual(app._daily_publisher_news()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
