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
        self.assertEqual(len(response.json()["sources"]), 3)
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
        for query in ("limit=0", "limit=201", "commodity=all", "publisher=untrusted", "commodity=%27%20OR%201%3D1"):
            with self.subTest(query=query):
                response = await self.client.get("/api/news?" + query)
                self.assertEqual(response.status_code, 422)

    async def test_valid_filters_forwarded_and_no_private_data(self):
        with patch.object(app.news_service, "get_news_snapshot", return_value={"items": [], "sources": []}) as snapshot:
            db.init()
            response = await self.client.get("/api/news?commodity=copper&publisher=Bloomberg&limit=12")
        self.assertEqual(response.status_code, 200)
        snapshot.assert_called_once_with(limit=12, commodity="copper", publisher="Bloomberg", db_path=db.DB_PATH)
        self.assertNotIn("holdings", response.text)


if __name__ == "__main__":
    unittest.main()
