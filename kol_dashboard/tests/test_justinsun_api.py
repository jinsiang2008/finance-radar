from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import app
import db


class JustinSunApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_and_media_context_keep_person_and_company_distinct(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(db, "DB_PATH", Path(directory) / "test.db"):
            db.init()
            app.news_service.init_news_db(db.DB_PATH)
            published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            titles = ("Justin Sun discusses stablecoin settlement growth",
                      "TRON DAO reports stablecoin settlement growth",
                      "Bitcoin investment grows across global markets",
                      "Tron film earns record revenue")
            items = [f"<item><title>{escape(title)}</title>"
                     f"<link>https://www.bloomberg.com/news/articles/sun-{i}</link>"
                     f"<pubDate>{published}</pubDate></item>" for i, title in enumerate(titles)]
            app.news_service.store_feed(
                app.news_service.SOURCES[0],
                {"status": 200, "body": ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()},
                db_path=db.DB_PATH,
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://testserver") as client:
                directory_response = await client.get("/api/kols")
                self.assertEqual(directory_response.status_code, 200)
                profiles = {entry["kol_key"]: entry for entry in directory_response.json()}
                self.assertEqual(profiles["justinsun"]["kol_name_cn"], "孙宇晨")
                with patch.object(app.news_enrichment.llm_enrichment, "complete_json_with_usage", side_effect=AssertionError("read only")):
                    response = await client.get("/api/news/context?view=kol&kols=justinsun")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["total"], 2)
                self.assertEqual({item["publisher"] for item in data["items"]}, {"Bloomberg"})
                self.assertEqual({item["kol_mentions"][0]["attribution_basis"] for item in data["items"]},
                                 {"person_mention", "company_mention"})
                self.assertTrue(all(item["kol_mentions"][0]["key"] == "justinsun" for item in data["items"]))
                self.assertIn("不代表本人发言", data["notice"])


if __name__ == "__main__":
    unittest.main()
