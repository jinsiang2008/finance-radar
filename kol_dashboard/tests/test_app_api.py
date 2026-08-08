from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import db  # noqa: E402
import llm_enrichment  # noqa: E402
import portfolio  # noqa: E402
import app as dashboard_app  # noqa: E402


def api_enrichment_result(**overrides) -> dict:
    result = {
        "headline_zh": "人工智能需求推动英伟达信号升温",
        "summary_zh": "公开来源显示人工智能需求仍受关注，但订单与收入影响仍需等待公司披露确认。",
        "why_it_matters_zh": "该信号可能通过算力需求影响美国半导体股票及相关供应链。",
        "impact_level": "high",
        "impact_path": ["人工智能需求 → 算力投入 → 半导体股票"],
        "tags": ["人工智能", "半导体"],
        "assets": [
            {
                "asset_key": "US:NVDA",
                "name_zh": "英伟达",
                "direction": "positive",
                "horizon": "medium",
                "reason_zh": "需求延续可能改善收入预期。",
                "confidence": 0.8,
            }
        ],
        "cluster_key": "nvidia-ai-demand-signal",
        "language": "en",
        "confidence": 0.77,
        "schema_version": 1,
    }
    result.update(overrides)
    return result


class DashboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "api.db"
        passcode_hash = auth.hash_passcode("open-sesame", iterations=1_000)
        self.environment = patch.dict(
            os.environ,
            {
                "KOL_DASHBOARD_PASSCODE_HASH": passcode_hash,
                "KOL_DASHBOARD_SESSION_SECRET": "s" * 32,
                "KOL_DASHBOARD_SESSION_TTL_SECONDS": "3600",
                "KOL_DASHBOARD_COOKIE_SECURE": "false",
                "KOL_DASHBOARD_COOKIE_PATH": "/",
            },
        )
        self.environment.start()
        dashboard_app.LOGIN_LIMITER.reset("127.0.0.1")
        db.init()
        transport = httpx.ASGITransport(app=dashboard_app.app)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.environment.stop()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    async def _login(self) -> httpx.Response:
        return await self.client.post(
            "/api/auth/login",
            json={"passcode": "open-sesame"},
        )

    def _seed_decision_and_portfolio(self) -> None:
        published_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(hours=1)
        ).isoformat()
        db.insert_events(
            [
                {
                    "title": "AI demand",
                    "url": "https://example.com/ai-demand",
                    "snippet": "NVIDIA AI demand",
                    "source": "Test News",
                    "kol_key": "tester",
                    "kol_name": "Tester",
                    "kol_name_cn": "测试者",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": published_at,
                }
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
                    "rationale": "Public mechanism evidence.",
                    "evidence": {
                        "title": "AI demand",
                        "account": "must-never-be-public",
                        "quantity": 999,
                        "published_at": published_at,
                    },
                    "created_at": published_at,
                }
            ],
        )
        markdown = f"""
## 美股持仓 — 股票
> 数据日期：{date.today().isoformat()}
| 代码 | 名称 | 持有数量 | 平均成本 |
|---|---|---:|---:|
| NVDA | NVIDIA | 10 | 100 |
"""
        db.save_portfolio_snapshot(portfolio.parse_holdings_markdown(markdown))

    async def test_login_status_logout_and_cookie_security_contract(self) -> None:
        status = await self.client.get("/api/auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(
            status.json(),
            {"configured": True, "authenticated": False},
        )
        self.assertEqual(status.headers["cache-control"], "no-store")

        denied = await self.client.post(
            "/api/auth/login", json={"passcode": "wrong"}
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["detail"], "invalid_credentials")
        self.assertNotIn("open-sesame", denied.text)

        logged_in = await self._login()
        self.assertEqual(logged_in.status_code, 200)
        cookie = logged_in.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("max-age=3600", cookie)
        self.assertNotIn("open-sesame", cookie)

        status = await self.client.get("/api/auth/status")
        self.assertTrue(status.json()["authenticated"])

        logged_out = await self.client.post("/api/auth/logout")
        self.assertEqual(logged_out.status_code, 200)
        status = await self.client.get("/api/auth/status")
        self.assertFalse(status.json()["authenticated"])

    async def test_private_routes_require_valid_session_and_never_cache(
        self,
    ) -> None:
        for path in (
            "/api/private/decisions",
            "/api/private/portfolio-impact",
        ):
            denied = await self.client.get(path)
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers["cache-control"], "no-store")
            self.assertNotIn("position", denied.text.lower())

        prune = await self.client.post("/api/prune")
        self.assertEqual(prune.status_code, 401)

    async def test_missing_config_and_tampered_cookie_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            status = await self.client.get("/api/auth/status")
            self.assertEqual(
                status.json(),
                {"configured": False, "authenticated": False},
            )
            login = await self.client.post(
                "/api/auth/login", json={"passcode": "anything"}
            )
            self.assertEqual(login.status_code, 503)
            private = await self.client.get("/api/private/decisions")
            self.assertEqual(private.status_code, 503)

        await self._login()
        self.client.cookies.set(
            "kol_private_session",
            "tampered.value",
            domain="testserver.local",
            path="/",
        )
        denied = await self.client.get("/api/private/decisions")
        self.assertEqual(denied.status_code, 401)

    async def test_public_and_private_decision_apis_are_isolated(self) -> None:
        self._seed_decision_and_portfolio()

        public = await self.client.get("/api/decisions")
        self.assertEqual(public.status_code, 200)
        public_text = public.text.lower()
        self.assertIn("ai_semiconductors", public_text)
        for private_value in (
            "must-never-be-public",
            "robinhood",
            "quantity",
            "matched_positions",
        ):
            self.assertNotIn(private_value, public_text)

        relations = await self.client.get("/api/relations")
        self.assertEqual(relations.status_code, 200)
        self.assertNotIn("must-never-be-public", relations.text)
        reactions = await self.client.get("/api/market/reactions")
        self.assertEqual(reactions.status_code, 200)
        self.assertEqual(reactions.json(), {"items": [], "count": 0})

        await self._login()
        private = await self.client.get("/api/private/decisions")
        self.assertEqual(private.status_code, 200)
        self.assertEqual(private.headers["cache-control"], "no-store")
        card = private.json()["decisions"][0]
        self.assertEqual(card["matched_positions"][0]["asset_key"], "US:NVDA")
        self.assertEqual(card["matched_positions"][0]["quantity"], 10.0)

        impact = await self.client.get("/api/private/portfolio-impact")
        self.assertEqual(impact.status_code, 200)
        body = impact.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["snapshot"]["position_count"], 1)
        self.assertEqual(body["impacts"][0]["asset_key"], "US:NVDA")
        self.assertTrue(body["human_review_required"])

    async def test_event_api_defaults_to_recent_verified_publications(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def event(title: str, published_at: str | None) -> dict:
            return {
                "title": title,
                "url": f"https://example.com/{title.replace(' ', '-').lower()}",
                "snippet": "",
                "source": "Test News",
                "kol_key": "tester",
                "kol_name": "Tester",
                "kol_name_cn": "测试者",
                "impact": "medium",
                "has_market_kw": True,
                "published_at": published_at,
            }

        db.insert_events(
            [
                event(
                    "Recent verified story",
                    (now - timedelta(hours=1)).isoformat(),
                ),
                event(
                    "Old story observed again",
                    (now - timedelta(days=400)).isoformat(),
                ),
                event("Unknown publication time", None),
            ]
        )

        default_feed = await self.client.get("/api/events")
        quarantine = await self.client.get(
            "/api/events", params={"time_status": "unverified"}
        )
        invalid = await self.client.get(
            "/api/events", params={"time_status": "anything"}
        )

        self.assertEqual(default_feed.status_code, 200)
        self.assertEqual(
            [item["title"] for item in default_feed.json()["items"]],
            ["Recent verified story"],
        )
        self.assertEqual(
            default_feed.json()["items"][0]["time_status"], "verified"
        )
        self.assertEqual(quarantine.status_code, 200)
        self.assertEqual(
            [item["title"] for item in quarantine.json()["items"]],
            ["Unknown publication time"],
        )
        self.assertEqual(
            quarantine.json()["items"][0]["time_status"], "unknown"
        )
        self.assertEqual(invalid.status_code, 422)

    async def test_noncontent_truth_repost_is_hidden_from_public_intelligence(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_events(
            [
                {
                    "title": "RT https://truthsocial.com/@realDonaldTrump",
                    "url": (
                        "https://truthsocial.com/@realDonaldTrump/"
                        "117051398671535118"
                    ),
                    "snippet": "RT https://truthsocial.com/@realDonaldTrump",
                    "source": "Truth Social @realDonaldTrump",
                    "kol_key": "trump",
                    "kol_name": "Donald Trump",
                    "kol_name_cn": "特朗普",
                    "impact": "high",
                    "has_market_kw": True,
                    "published_at": (now - timedelta(hours=1)).isoformat(),
                }
            ]
        )
        with db.conn() as connection:
            event_id = connection.execute("SELECT id FROM events").fetchone()["id"]

        listing = await self.client.get("/api/events")
        detail = await self.client.get(f"/api/events/{event_id}")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), {"items": [], "count": 0})
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(detail.json()["detail"], "event_not_available")

    async def test_kol_feed_filters_noncontent_sighting_before_pagination(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        newest = (now - timedelta(minutes=5)).isoformat()
        older = (now - timedelta(minutes=10)).isoformat()
        db.insert_events(
            [
                {
                    "title": "AI",
                    "url": "https://example.com/brief-ai-item",
                    "snippet": "AI",
                    "source": "Bing News",
                    "kol_key": "reporter",
                    "published_at": newest,
                },
                {
                    "title": "AI",
                    "url": (
                        "https://truthsocial.com/@realDonaldTrump/"
                        "117051398671535118"
                    ),
                    "snippet": "RT https://truthsocial.com/@realDonaldTrump",
                    "source": "Truth Social @realDonaldTrump",
                    "kol_key": "trump",
                    "published_at": newest,
                },
                {
                    "title": "Tariff review enters final stage",
                    "url": (
                        "https://truthsocial.com/@realDonaldTrump/"
                        "117051398671535119"
                    ),
                    "snippet": (
                        "The semiconductor tariff review enters its final stage."
                    ),
                    "source": "Truth Social @realDonaldTrump",
                    "kol_key": "trump",
                    "published_at": older,
                },
            ]
        )

        response = await self.client.get(
            "/api/events",
            params={"kol": "trump", "limit": 1, "hours": 24},
        )
        with db.conn() as connection:
            merged_event_id = connection.execute(
                "SELECT id FROM events WHERE title='AI'"
            ).fetchone()["id"]
        detail = await self.client.get(
            f"/api/events/{merged_event_id}",
            params={"kol": "trump"},
        )
        kols = await self.client.get("/api/kols")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["title"] for item in response.json()["items"]],
            ["Tariff review enters final stage"],
        )
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(detail.json()["detail"], "event_not_available")
        trump = next(item for item in kols.json() if item["kol_key"] == "trump")
        self.assertEqual(trump["total"], 1)
        self.assertEqual(trump["total_24h"], 1)

    async def test_event_list_and_integer_detail_expose_nested_enrichment(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        event = {
            "title": "Current NVIDIA AI demand signal",
            "url": "https://example.com/current-nvidia-ai-demand",
            "snippet": "NVIDIA AI demand remains in focus.",
            "source": "Test News",
            "kol_key": "tester",
            "kol_name": "Tester",
            "kol_name_cn": "测试者",
            "impact": "medium",
            "has_market_kw": True,
            "tickers": ["NVDA"],
            "published_at": (now - timedelta(hours=1)).isoformat(),
        }
        db.insert_events([event])
        db.insert_events(
            [
                {
                    **event,
                    "url": "https://x.com/elonmusk/status/123",
                    "source": "X @elonmusk",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                }
            ]
        )
        db.insert_events(
            [
                {
                    **event,
                    "url": "https://example.com/musk-ai-interview",
                    "source": "Interview transcript",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                    "published_at": (now - timedelta(minutes=30)).isoformat(),
                }
            ]
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
                result=api_enrichment_result(),
                generated_at=now.isoformat(),
            )
        )
        db.replace_relations(
            "event",
            str(candidate["id"]),
            [
                {
                    "topic_key": "ai_semiconductors",
                    "asset_key": "US:NVDA",
                    "relation_type": "view",
                    "direction": "positive",
                    "strength": 0.8,
                    "confidence": 0.8,
                    "horizon": "medium",
                    "method": "deterministic_rules:test",
                    "rationale": "Public mechanism evidence.",
                    "evidence": {"title": event["title"]},
                }
            ],
        )

        listing = await self.client.get("/api/events")
        detail = await self.client.get(f"/api/events/{candidate['id']}")
        detail_for_kol = await self.client.get(
            f"/api/events/{candidate['id']}",
            params={"kol": "musk"},
        )
        detail_for_source = await self.client.get(
            f"/api/events/{candidate['id']}",
            params={
                "kol": "musk",
                "source_url": "https://example.com/musk-ai-interview",
            },
        )
        missing = await self.client.get("/api/events/999999")
        oversized = await self.client.get(f"/api/events/{10**40}")

        self.assertEqual(listing.status_code, 200)
        listed = listing.json()["items"][0]
        self.assertEqual(listed["id"], candidate["id"])
        self.assertEqual(listed["tickers"], ["NVDA"])
        self.assertEqual(listed["rule_impact"], "medium")
        self.assertEqual(listed["impact"], "high")
        self.assertEqual(listed["ai_status"], "ready")
        self.assertEqual(
            listed["ai_enrichment"]["headline_zh"],
            "人工智能需求推动英伟达信号升温",
        )
        self.assertEqual(
            listed["ai_enrichment"]["assets"][0]["asset_key"],
            "US:NVDA",
        )
        self.assertNotIn("ai_assets_json", listed)

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.headers["cache-control"], "public, max-age=30")
        body = detail.json()
        self.assertEqual(body["event"]["id"], candidate["id"])
        self.assertEqual(body["event"]["tickers"], ["NVDA"])
        self.assertEqual(body["event"]["ai_enrichment"]["status"], "ready")
        self.assertEqual(len(body["sightings"]), 3)
        self.assertEqual(
            {sighting["source_url"] for sighting in body["sightings"]},
            {
                "https://example.com/current-nvidia-ai-demand",
                "https://example.com/musk-ai-interview",
                "https://x.com/elonmusk/status/123",
            },
        )
        self.assertEqual(body["relations"][0]["asset_key"], "US:NVDA")
        self.assertEqual(body["market_reactions"], [])

        self.assertEqual(detail_for_kol.status_code, 200)
        selected = detail_for_kol.json()["event"]
        self.assertEqual(selected["kol_key"], "musk")
        self.assertEqual(selected["kol_name_cn"], "马斯克")
        self.assertEqual(selected["source"], "X @elonmusk")
        self.assertEqual(
            selected["source_url"],
            "https://x.com/elonmusk/status/123",
        )
        self.assertEqual(detail_for_source.status_code, 200)
        exact = detail_for_source.json()["event"]
        self.assertEqual(exact["kol_key"], "musk")
        self.assertEqual(exact["source"], "Interview transcript")
        self.assertEqual(
            exact["source_url"],
            "https://example.com/musk-ai-interview",
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "event_not_found")
        self.assertEqual(oversized.status_code, 422)

    async def test_unsafe_incremental_event_endpoint_is_not_exposed(self) -> None:
        response = await self.client.get("/api/events/new")

        self.assertEqual(response.status_code, 404)

    async def test_stale_relations_cannot_hide_a_current_decision(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_events(
            [
                {
                    "title": "Current NVIDIA signal",
                    "url": "https://example.com/current-nvidia",
                    "snippet": "NVIDIA AI",
                    "source": "Test",
                    "kol_key": "tester",
                    "kol_name": "Tester",
                    "kol_name_cn": "测试者",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": (now - timedelta(hours=1)).isoformat(),
                }
            ]
        )
        with db.conn() as connection:
            current_event_id = str(
                connection.execute("SELECT id FROM events").fetchone()["id"]
            )
        current_evidence = json.dumps(
            {
                "title": "Current NVIDIA signal",
                "published_at": (now - timedelta(hours=1)).isoformat(),
            }
        )
        stale_evidence = json.dumps(
            {
                "title": "Stale signal",
                "published_at": (now - timedelta(days=30)).isoformat(),
            }
        )
        base = (
            "event",
            "",
            "ai_semiconductors",
            "US:NVDA",
            "view",
            "positive",
            0.8,
            0.8,
            "medium",
            "deterministic_rules:test",
            "public rationale",
        )
        with db.conn() as connection:
            connection.execute(
                """
                INSERT INTO relations (
                  source_type, source_id, topic_key, asset_key, relation_type,
                  direction, strength, confidence, horizon, method, rationale,
                  evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *base[:1],
                    current_event_id,
                    *base[2:],
                    current_evidence,
                    (now - timedelta(hours=2)).isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO relations (
                  source_type, source_id, topic_key, asset_key, relation_type,
                  direction, strength, confidence, horizon, method, rationale,
                  evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        *base[:1],
                        f"stale-{index}",
                        *base[2:],
                        stale_evidence,
                        now.isoformat(),
                    )
                    for index in range(1_001)
                ],
            )

        response = await self.client.get("/api/decisions")

        self.assertEqual(response.status_code, 200)
        decisions = response.json()["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0]["evidence"][0]["source_id"],
            current_event_id,
        )

    async def test_decisions_require_a_current_verified_event_row(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_events(
            [
                {
                    "title": "Future event",
                    "url": "https://example.com/future-event",
                    "snippet": "NVIDIA AI",
                    "source": "Test",
                    "kol_key": "tester",
                    "kol_name": "Tester",
                    "kol_name_cn": "测试者",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": (now + timedelta(hours=2)).isoformat(),
                }
            ]
        )
        with db.conn() as connection:
            event_id = connection.execute(
                "SELECT id FROM events"
            ).fetchone()["id"]

        def relation(source_id: str) -> dict:
            return {
                "source_type": "event",
                "source_id": source_id,
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
                    "title": "Forged recent evidence time",
                    "published_at": (now - timedelta(hours=1)).isoformat(),
                },
            }

        db.replace_relations("event", str(event_id), [relation(str(event_id))])
        db.replace_relations("event", "orphan", [relation("orphan")])

        response = await self.client.get("/api/decisions")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decisions"], [])

    async def test_macro_coverage_uses_collector_accounting(self) -> None:
        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": "2026-08-03T00:00:00+00:00",
                "composite_risk": {"score": 50, "level": "medium"},
                "data_coverage": {
                    "available": 0,
                    "total": 6,
                    "pct": 0,
                    "sources": [],
                },
                "market_data": {
                    "vix": {"value": None, "status": "unknown"},
                    "treasury": {"10Y": None, "status": "unknown"},
                },
            }
        )
        self.assertEqual(dashboard_app._macro_coverage(), 0.0)

        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": "2026-08-03T01:00:00+00:00",
                "composite_risk": {"score": 50, "level": "medium"},
                "data_coverage": {
                    "available": 3,
                    "total": 6,
                    "pct": 50,
                    "sources": [],
                },
                "market_data": {},
            }
        )
        self.assertEqual(dashboard_app._macro_coverage(), 0.5)

    async def test_macro_api_exposes_only_current_whitelisted_ai_analysis(
        self,
    ) -> None:
        ready_event = {
            "id": "ind_ready",
            "kind": "indicator",
            "title": "VIX 跳升 12 点",
            "source": "风险雷达指标监控",
            "published_at": "2026-08-06T04:00:00+00:00",
            "time_status": "verified",
            "severity": "high",
            "previous_value": 15.0,
            "current_value": 27.0,
            "unit": "point",
            "note": "恐慌指数快速变化通常先于风险资产重定价",
            "tickers": ["VXX", "SPY"],
            "sectors": ["波动率", "美股大盘"],
        }
        stale_original = {
            **ready_event,
            "id": "ind_stale",
            "title": "OFR FSI 上升",
            "previous_value": 1.0,
            "current_value": 2.5,
            "unit": "index",
        }
        stale_current = {**stale_original, "current_value": 4.0}
        retry_event = {
            "id": "pol_retry",
            "kind": "policy",
            "title": "Federal Reserve issues a policy statement",
            "url": "https://federalreserve.gov/policy/retry.htm",
            "source": "Federal Reserve",
            "published_at": "2026-08-06T03:00:00+00:00",
            "time_status": "verified",
            "severity": "medium",
            "tickers": ["SPY"],
            "sectors": ["美股大盘"],
        }
        pending_event = {
            **retry_event,
            "id": "pol_pending",
            "title": "PBOC issues an open-market notice",
            "url": "https://pbc.gov.cn/policy/pending.htm",
            "source": "中国人民银行",
            "ai_status": "ready",
            "ai_enrichment": {
                "headline_zh": "PRIVATE-INJECTED-SNAPSHOT-AI",
                "summary_zh": "不能信任快照中自带的 AI 字段",
            },
        }

        def claim_params(event: dict) -> tuple[dict, str]:
            event_input, input_hash = llm_enrichment.build_macro_event_input(event)
            params = {
                "event_key": llm_enrichment.macro_event_key(event),
                "input_hash": input_hash,
                "prompt_version": llm_enrichment.MACRO_PROMPT_VERSION,
                "model": llm_enrichment.DEFAULT_MODEL,
                "evidence_basis": event_input["evidence_basis"],
            }
            claimed = db.claim_macro_event_enrichment(**params)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            return params, claimed[0]

        ready_params, ready_token = claim_params(ready_event)
        self.assertTrue(
            db.save_macro_event_enrichment(
                **ready_params,
                claim_token=ready_token,
                result=api_enrichment_result(),
            )
        )
        with db.conn() as connection:
            connection.execute(
                "UPDATE macro_event_enrichments "
                "SET assets_json=?, claim_token=?, error_code=? "
                "WHERE event_key=?",
                (
                    json.dumps(
                        [
                            {
                                "asset_key": "US:NVDA",
                                "name_zh": "英伟达",
                                "direction": "positive",
                                "horizon": "medium",
                                "reason_zh": "波动率变化可能影响风险偏好。",
                                "confidence": 0.8,
                                "account": "PRIVATE-ASSET-ACCOUNT",
                                "quantity": 999,
                            },
                            {
                                "asset_key": "THEME:NOT-TRADEABLE",
                                "name_zh": "不得公开",
                                "direction": "positive",
                                "horizon": "medium",
                                "reason_zh": "无效资产键",
                                "confidence": 1,
                            },
                        ],
                        ensure_ascii=False,
                    ),
                    "PRIVATE-CLAIM-TOKEN",
                    "PRIVATE-READY-ERROR",
                    ready_params["event_key"],
                ),
            )

        stale_params, stale_token = claim_params(stale_original)
        self.assertTrue(
            db.save_macro_event_enrichment(
                **stale_params,
                claim_token=stale_token,
                result=api_enrichment_result(
                    headline_zh="不得跟随新指标值展示的旧结论"
                ),
            )
        )

        retry_params, retry_token = claim_params(retry_event)
        self.assertTrue(
            db.fail_macro_event_enrichment(
                retry_params["event_key"],
                input_hash=retry_params["input_hash"],
                prompt_version=retry_params["prompt_version"],
                model=retry_params["model"],
                claim_token=retry_token,
                error_code="Bearer-private-provider-detail",
                retry_after_seconds=900,
            )
        )

        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": "2026-08-06T04:00:00+00:00",
                "composite_risk": {"score": 65, "level": "high"},
                "monitored_events": [
                    ready_event,
                    stale_current,
                    retry_event,
                    pending_event,
                ],
                "market_data": {},
                "sub_scores": {},
                "black_swan_scenarios": [],
                "gray_rhinos": [],
                "opportunities": [],
            }
        )

        response = await self.client.get("/api/macro")

        self.assertEqual(response.status_code, 200)
        events = {
            item["id"]: item for item in response.json()["monitored_events"]
        }
        ready = events["ind_ready"]
        self.assertEqual(ready["severity"], "high")
        self.assertEqual(ready["ai_status"], "ready")
        enrichment = ready["ai_enrichment"]
        self.assertEqual(
            set(enrichment),
            {
                "status",
                "headline_zh",
                "summary_zh",
                "why_it_matters_zh",
                "impact_level",
                "impact_path",
                "tags",
                "assets",
                "cluster_key",
                "language",
                "confidence",
                "evidence_basis",
                "model",
                "generated_at",
            },
        )
        self.assertEqual(enrichment["headline_zh"], api_enrichment_result()["headline_zh"])
        self.assertEqual(len(enrichment["assets"]), 1)
        self.assertEqual(
            set(enrichment["assets"][0]),
            {
                "asset_key",
                "name_zh",
                "direction",
                "horizon",
                "reason_zh",
                "confidence",
            },
        )

        self.assertEqual(events["ind_stale"]["ai_status"], "pending")
        self.assertIsNone(events["ind_stale"]["ai_enrichment"])
        self.assertEqual(events["pol_retry"]["ai_status"], "retry")
        self.assertIsNone(events["pol_retry"]["ai_enrichment"])
        self.assertEqual(events["pol_pending"]["ai_status"], "pending")
        self.assertIsNone(events["pol_pending"]["ai_enrichment"])

        encoded = response.text.lower()
        for forbidden in (
            "input_hash",
            "prompt_version",
            "attempt_count",
            "next_attempt_at",
            "claim_token",
            "error_code",
            "raw_response",
            "private-asset-account",
            "private-claim-token",
            "private-ready-error",
            "bearer-private-provider-detail",
            "private-injected-snapshot-ai",
            "quantity",
        ):
            self.assertNotIn(forbidden, encoded)

    async def test_macro_api_publishes_official_body_with_evidence_bound_ai(
        self,
    ) -> None:
        source_url = (
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "monetary20260729a.htm"
        )
        event = {
            "id": "pol_fomc_official_body",
            "kind": "policy",
            "title": "Federal Reserve issues FOMC statement",
            "url": source_url,
            "source": "FOMC",
            "published_at": "2026-07-29T18:00:00+00:00",
            "time_status": "verified",
            "severity": "high",
            "category": "fomc_statement",
            "content_status": "ready",
            "content_excerpt": (
                "The Committee decided to maintain the target range at "
                "3-1/2 to 3-3/4 percent. <b>The vote was 9-3.</b>"
            ),
            "content_source_url": source_url,
            "evidence_sections": [
                {
                    "kind": "paragraph",
                    "text": "The vote was 9-3; three members preferred a 25 basis point increase.",
                }
            ],
            "tickers": ["TLT", "SPY"],
            "sectors": ["美债", "利率敏感板块"],
            "raw_html": "PRIVATE-RAW-ARTICLE",
        }
        event_input, input_hash = llm_enrichment.build_macro_event_input(event)
        event_key = llm_enrichment.macro_event_key(event)
        claimed = db.claim_macro_event_enrichment(
            event_key,
            input_hash=input_hash,
            prompt_version=llm_enrichment.MACRO_PROMPT_VERSION,
            model=llm_enrichment.DEFAULT_MODEL,
            evidence_basis=event_input["evidence_basis"],
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertTrue(
            db.save_macro_event_enrichment(
                event_key,
                input_hash=input_hash,
                prompt_version=llm_enrichment.MACRO_PROMPT_VERSION,
                model=llm_enrichment.DEFAULT_MODEL,
                claim_token=claimed[0],
                evidence_basis=event_input["evidence_basis"],
                result=api_enrichment_result(
                    headline_zh="美联储以9比3维持利率区间不变",
                    summary_zh=(
                        "FOMC以9比3决定维持3.5%至3.75%的目标区间，"
                        "三名委员倾向加息25个基点。"
                    ),
                    why_it_matters_zh="分歧偏鹰，可能推高美债收益率并压制高估值股票。",
                    confidence=0.86,
                ),
            )
        )
        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": "2026-07-29T18:05:00+00:00",
                "composite_risk": {"score": 55, "level": "high"},
                "monitored_events": [event],
                "market_data": {},
                "sub_scores": {},
                "black_swan_scenarios": [],
                "gray_rhinos": [],
                "opportunities": [],
            }
        )

        response = await self.client.get("/api/macro")

        self.assertEqual(response.status_code, 200)
        public_event = response.json()["monitored_events"][0]
        self.assertEqual(public_event["category"], "fomc_statement")
        self.assertEqual(public_event["content_status"], "ready")
        self.assertIn("3-1/2 to 3-3/4", public_event["content_excerpt"])
        self.assertNotIn("<b>", public_event["content_excerpt"])
        self.assertEqual(public_event["content_source_url"], source_url)
        self.assertEqual(
            public_event["evidence_sections"][0]["kind"], "paragraph"
        )
        self.assertEqual(public_event["ai_status"], "ready")
        self.assertEqual(
            public_event["ai_enrichment"]["evidence_basis"], "official_body"
        )
        self.assertIn("9比3", public_event["ai_enrichment"]["summary_zh"])
        self.assertNotIn("private-raw-article", response.text.lower())

    async def test_legacy_macro_portfolio_fields_never_reach_public_apis(
        self,
    ) -> None:
        db.save_macro_snapshot(
            {
                "timestamp": "2026-08-02T00:00:00+08:00",
                "composite_risk": {"score": 50, "level": "medium"},
                "sub_scores": {
                    "recession": {
                        "score": 40,
                        "signals": ["Public signal"],
                        "holdings": ["NESTED-HOLDING"],
                    }
                },
                "market_data": {
                    "vix": {
                        "value": 20,
                        "positions": ["NESTED-POSITION"],
                    }
                },
                "search_queries": [
                    "public query",
                    {"账户": "NESTED-QUERY"},
                ],
                "black_swan_scenarios": [
                    {
                        "id": "legacy",
                        "name": "Legacy scenario",
                        "affected_positions": ["PRIVATE-TICKER"],
                        "affected_assets": ["SPY"],
                    }
                ],
                "gray_rhinos": [
                    {
                        "id": "legacy-rhino",
                        "name": "Legacy rhino",
                        "portfolio_impact": "PRIVATE-IMPACT",
                        "market_impact": "Public market impact",
                    }
                ],
                "opportunities": [],
            }
        )
        db.replace_relations(
            "macro_snapshot",
            "legacy:gray_rhino",
            [
                {
                    "source_type": "macro_snapshot",
                    "source_id": "legacy:gray_rhino",
                    "topic_key": "legacy_private",
                    "asset_key": "US:PRIVATE",
                    "relation_type": "structural_risk",
                    "direction": "negative",
                    "strength": 0.8,
                    "confidence": 0.8,
                    "horizon": "medium",
                    "method": "deterministic_rules:test",
                    "rationale": "legacy",
                    "evidence": {
                        "matched_fields": {
                            "portfolio_impact": "PRIVATE-IMPACT"
                        }
                    },
                }
            ],
        )
        db.replace_relations(
            "macro_snapshot",
            "legacy:fallback",
            [
                {
                    "source_type": "macro_snapshot",
                    "source_id": "legacy:fallback",
                    "topic_key": "legacy_fallback",
                    "asset_key": "US:FALLBACK",
                    "relation_type": "structural_risk",
                    "direction": "negative",
                    "strength": 0.7,
                    "confidence": 0.7,
                    "horizon": "medium",
                    "method": "deterministic_rules:1.1.0",
                    "rationale": "legacy fallback",
                    "evidence": {
                        "extractor_version": "1.1.0",
                        "matched_fields": {
                            "fallback_text": "FALLBACK-SECRET 持仓"
                        },
                    },
                }
            ],
        )

        macro = await self.client.get("/api/macro")
        decisions = await self.client.get("/api/decisions")
        relations = await self.client.get("/api/relations")
        encoded = (macro.text + decisions.text + relations.text).lower()

        self.assertEqual(
            macro.json()["black_swan_scenarios"],
            [],
        )
        self.assertEqual(macro.json()["gray_rhinos"], [])
        for private_value in (
            "private-ticker",
            "private-impact",
            "affected_positions",
            "portfolio_impact",
            "us:private",
            "nested-holding",
            "nested-position",
            "nested-query",
            "fallback-secret",
            "us:fallback",
        ):
            self.assertNotIn(private_value, encoded)

    async def test_login_rate_limit_returns_retry_after(self) -> None:
        client_key = "127.0.0.1"
        dashboard_app.LOGIN_LIMITER.reset(client_key)
        for _ in range(5):
            response = await self.client.post(
                "/api/auth/login", json={"passcode": "wrong"}
            )
            self.assertEqual(response.status_code, 401)

        limited = await self.client.post(
            "/api/auth/login", json={"passcode": "wrong"}
        )

        self.assertEqual(limited.status_code, 429)
        self.assertGreater(int(limited.headers["retry-after"]), 0)

    async def test_public_decision_summary_detail_and_etag_share_snapshot(self) -> None:
        self._seed_decision_and_portfolio()

        summary = await self.client.get("/api/decisions/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.json()
        self.assertTrue(payload["summary"])
        self.assertEqual(payload["total_decisions"], 1)
        self.assertNotIn("evidence", payload["decisions"][0])
        self.assertIn("etag", summary.headers)

        not_modified = await self.client.get(
            "/api/decisions/summary",
            headers={"If-None-Match": summary.headers["etag"]},
        )
        self.assertEqual(not_modified.status_code, 304)

        # nginx weakens strong ETags when it gzip-compresses a representation.
        # GET/HEAD revalidation uses weak comparison, so the transformed value
        # must still hit the same application snapshot.
        weak_not_modified = await self.client.get(
            "/api/decisions/summary",
            headers={"If-None-Match": f'W/{summary.headers["etag"]}'},
        )
        self.assertEqual(weak_not_modified.status_code, 304)

        card = payload["decisions"][0]
        detail = await self.client.get(
            "/api/decisions/detail",
            params={
                "topic_key": card["topic_key"],
                "asset_key": card["asset_key"],
                "snapshot_id": payload["snapshot_id"],
            },
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["snapshot_id"], payload["snapshot_id"])
        self.assertIn("evidence", detail.json()["decision"])
        self.assertNotIn("must-never-be-public", detail.text.lower())


if __name__ == "__main__":
    unittest.main()
