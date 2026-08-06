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


def api_enrichment_result() -> dict:
    return {
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


if __name__ == "__main__":
    unittest.main()
