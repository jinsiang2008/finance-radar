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
import briefing_import  # noqa: E402
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


class DashboardStartupTests(unittest.TestCase):
    def test_init_db_warms_relevance_cache_after_schema_init(self) -> None:
        calls: list[str] = []

        with (
            patch.object(
                dashboard_app.db,
                "init",
                side_effect=lambda: calls.append("init"),
            ) as init_db,
            patch.object(
                dashboard_app.db,
                "warm_event_relevance_cache",
                side_effect=lambda: calls.append("warm"),
            ) as warm_cache,
        ):
            dashboard_app._init_db()

        self.assertEqual(calls, ["init", "warm"])
        init_db.assert_called_once_with()
        warm_cache.assert_called_once_with()

    def test_init_db_propagates_relevance_cache_warmup_failure(self) -> None:
        failure = RuntimeError("relevance cache warmup failed")

        with (
            patch.object(dashboard_app.db, "init") as init_db,
            patch.object(
                dashboard_app.db,
                "warm_event_relevance_cache",
                side_effect=failure,
            ) as warm_cache,
        ):
            with self.assertRaises(RuntimeError) as raised:
                dashboard_app._init_db()

        self.assertIs(raised.exception, failure)
        init_db.assert_called_once_with()
        warm_cache.assert_called_once_with()


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
                "KOL_ENRICHMENT_PENDING_PATH": str(
                    Path(self.temp_dir.name) / "enrichment.pending"
                ),
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

    @staticmethod
    def _options_policy_body(**overrides) -> dict:
        body = {
            "schema_version": 1,
            "expected_revision": 0,
            "strategy": "cash_secured_put",
            "limits": {
                "assignment_budget_ceiling_usd": "50000.00",
                "max_total_reserved_bps": 3000,
                "max_single_underlying_bps": 1500,
                "minimum_cash_buffer_bps": 2000,
                "max_new_contracts_per_week": 2,
            },
            "assignment_plan": "hold_for_review",
            "underlyings": [
                {
                    "asset_key": "US:NVDA",
                    "decision": "willing",
                    "max_assignment_price_usd": "150.00",
                },
                {"asset_key": "US:TSLA", "decision": "exclude"},
            ],
            "acknowledgements": {
                "cash_secured_only": True,
                "assignment_risk_reviewed": True,
            },
        }
        body.update(overrides)
        return body

    def _seed_ai_event(
        self, *, title: str = "Jensen Huang says NVIDIA AI demand remains strong"
    ) -> int:
        db.insert_events(
            [
                {
                    "title": title,
                    "url": f"https://example.com/{title.replace(' ', '-').lower()}",
                    "snippet": (
                        "Jensen Huang discussed NVIDIA AI demand and investment "
                        "with enough public evidence for analysis."
                    ),
                    "source": "Test News",
                    "kol_key": "huangrenxun",
                    "kol_name": "Jensen Huang",
                    "kol_name_cn": "黄仁勋",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )
        with db.conn() as connection:
            return int(
                connection.execute(
                    "SELECT id FROM events ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
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
                    "kol_key": "huangrenxun",
                    "kol_name": "Jensen Huang",
                    "kol_name_cn": "黄仁勋",
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
            "/api/private/options/overview",
            "/api/private/options/policy",
        ):
            denied = await self.client.get(path)
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers["cache-control"], "no-store")
            self.assertNotIn("position", denied.text.lower())

        prune = await self.client.post("/api/prune")
        self.assertEqual(prune.status_code, 401)

    async def test_options_overview_is_private_no_store_and_research_only(
        self,
    ) -> None:
        private_snapshot = {
            "snapshot_id": 71,
            "source_hash": "private-options-source-hash",
            "as_of": "2026-01-01",
            "positions": [
                {
                    "account": "private-options-account",
                    "asset_key": "US:PRIVATE",
                    "symbol": "PRIVATE",
                    "name": "private-options-name",
                    "quantity": 987.0,
                    "avg_cost": 65.43,
                }
            ],
            "staleness": {
                "is_stale": True,
                "clock_skew": False,
                "age_seconds": 999999,
            },
        }
        public_macro = {
            "market_alerts": {
                "schema_version": 1,
                "method_version": "macro-de-risk-trial-v1",
                "generated_at": "2026-09-06T01:00:00+00:00",
                "mode": "trial",
                "human_review_required": True,
                "automatic_execution": False,
                "markets": [
                    {
                        "market": "US",
                        "action": "reduce_candidate",
                        "action_label": "减仓候选",
                        "risk_level": "high",
                        "abstain": False,
                        "data_status": "ok",
                        "data_as_of": "2026-09-05T20:00:00+00:00",
                    }
                ],
            }
        }
        await self._login()

        with (
            patch.object(
                dashboard_app.db,
                "latest_portfolio_snapshot",
                return_value=private_snapshot,
            ) as latest_portfolio,
            patch.object(
                dashboard_app,
                "_public_macro_snapshot",
                return_value=public_macro,
            ) as latest_macro,
        ):
            response = await self.client.get("/api/private/options/overview")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        body = response.json()
        self.assertEqual(body["schema_version"], 2)
        self.assertEqual(body["method_version"], "options-policy-readiness-v1")
        self.assertTrue(body["available"])
        self.assertEqual(body["mode"], "research_only")
        self.assertEqual(body["data_status"], "insufficient")
        self.assertEqual(body["decision_state"], "abstain")
        self.assertEqual(body["candidate_count"], 0)
        self.assertEqual(body["candidates"], [])
        self.assertEqual(body["market_gate"]["status"], "blocked")
        self.assertNotIn("familiar_universe", body)
        self.assertTrue(body["research_universe"])
        self.assertTrue(
            all(
                item["status"] == "needs_user_confirmation"
                for item in body["research_universe"]
            )
        )
        self.assertTrue(body["human_review_required"])
        self.assertFalse(body["automatic_execution"])
        self.assertFalse(body["trade_execution_available"])
        self.assertEqual(body["policy"]["status"], "not_configured")
        self.assertTrue(body["capabilities"]["policy_configuration"])
        self.assertFalse(body["capabilities"]["live_option_chain"])
        latest_portfolio.assert_called_once_with()
        latest_macro.assert_called_once_with()

        encoded = response.text.lower()
        for private_value in (
            "private-options-source-hash",
            "private-options-account",
            "us:private",
            "private-options-name",
            "987.0",
            "65.43",
        ):
            self.assertNotIn(private_value, encoded)
        for forbidden_field in (
            '"source_hash"',
            '"account"',
            '"quantity"',
            '"avg_cost"',
            '"positions"',
        ):
            self.assertNotIn(forbidden_field, encoded)

        public_route = await self.client.get("/api/options/overview")
        self.assertEqual(public_route.status_code, 404)

    async def test_options_policy_get_put_is_private_versioned_and_no_store(
        self,
    ) -> None:
        path = "/api/private/options/policy"
        action = {"X-Finance-Radar-Action": "update-options-policy"}
        denied = await self.client.put(
            path,
            json=self._options_policy_body(),
            headers=action,
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")

        await self._login()
        initial = await self.client.get(path)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.headers["cache-control"], "no-store")
        self.assertEqual(initial.json()["status"], "not_configured")
        self.assertEqual(initial.json()["revision"], 0)

        saved = await self.client.put(
            path,
            json=self._options_policy_body(),
            headers={**action, "Origin": "http://testserver"},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.headers["cache-control"], "no-store")
        policy = saved.json()
        self.assertEqual(policy["status"], "ready")
        self.assertEqual(policy["revision"], 1)
        self.assertEqual(
            policy["limits"]["assignment_budget_ceiling_usd"],
            "50000.00",
        )
        self.assertEqual(policy["confirmed_count"], 1)
        self.assertEqual(policy["excluded_count"], 1)
        self.assertEqual(policy["evidence_basis"], "user_confirmed")
        self.assertNotIn("payload_sha256", saved.text)

        # A response-lost retry may still carry revision 0; identical current
        # content is idempotent and must not create revision 2.
        retry = await self.client.put(
            path,
            json=self._options_policy_body(expected_revision=0),
            headers=action,
        )
        current = await self.client.get(path)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["revision"], 1)
        self.assertEqual(current.json()["revision"], 1)
        self.assertEqual(current.headers["cache-control"], "no-store")

        changed_limits = {
            **self._options_policy_body()["limits"],
            "assignment_budget_ceiling_usd": "60000.00",
        }
        conflict = await self.client.put(
            path,
            json=self._options_policy_body(
                expected_revision=0,
                limits=changed_limits,
            ),
            headers=action,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["detail"], "options_policy_revision_conflict"
        )
        self.assertEqual(conflict.headers["cache-control"], "no-store")

        public_get = await self.client.get("/api/options/policy")
        public_put = await self.client.put(
            "/api/options/policy",
            json=self._options_policy_body(),
            headers=action,
        )
        self.assertEqual(public_get.status_code, 404)
        self.assertEqual(public_put.status_code, 404)

    async def test_options_policy_put_enforces_csrf_media_size_and_exact_json(
        self,
    ) -> None:
        await self._login()
        path = "/api/private/options/policy"
        action = {"X-Finance-Radar-Action": "update-options-policy"}

        cases = [
            (
                await self.client.put(path, json=self._options_policy_body()),
                403,
                "options_policy_action_header_required",
            ),
            (
                await self.client.put(
                    path,
                    json=self._options_policy_body(),
                    headers={**action, "Origin": "https://attacker.example"},
                ),
                403,
                "options_policy_same_origin_required",
            ),
            (
                await self.client.put(
                    path,
                    content=b"{}",
                    headers={**action, "Content-Type": "text/plain"},
                ),
                415,
                "options_policy_json_body_required",
            ),
            (
                await self.client.put(
                    path,
                    content=b"{" + b" " * (16 * 1024) + b"}",
                    headers={**action, "Content-Type": "application/json"},
                ),
                413,
                "options_policy_request_too_large",
            ),
        ]
        for response, status, detail in cases:
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["detail"], detail)
            self.assertEqual(response.headers["cache-control"], "no-store")

        invalid_length = await self.client.put(
            path,
            content=b"{}",
            headers={
                **action,
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
        )
        self.assertEqual(invalid_length.status_code, 400)
        self.assertEqual(
            invalid_length.json()["detail"], "invalid_content_length"
        )
        self.assertEqual(invalid_length.headers["cache-control"], "no-store")

        hostile = {
            **self._options_policy_body(),
            "unknown": "PRIVATE-MALICIOUS-VALUE",
        }
        extra = await self.client.put(path, json=hostile, headers=action)
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(
            extra.json()["detail"], "options_policy_invalid_policy_fields"
        )
        self.assertNotIn("PRIVATE-MALICIOUS-VALUE", extra.text)
        self.assertEqual(extra.headers["cache-control"], "no-store")

        duplicate_json = json.dumps(self._options_policy_body()).replace(
            '"schema_version": 1',
            '"schema_version": 1, "schema_version": 1',
            1,
        )
        duplicate = await self.client.put(
            path,
            content=duplicate_json,
            headers={**action, "Content-Type": "application/json"},
        )
        self.assertEqual(duplicate.status_code, 422)
        self.assertEqual(
            duplicate.json()["detail"], "invalid_options_policy_json"
        )
        self.assertEqual(duplicate.headers["cache-control"], "no-store")

        leveraged = await self.client.put(
            path,
            json=self._options_policy_body(
                underlyings=[
                    {"asset_key": "US:NVDL", "decision": "exclude"}
                ]
            ),
            headers=action,
        )
        self.assertEqual(leveraged.status_code, 422)
        self.assertEqual(
            leveraged.json()["detail"],
            "options_policy_leveraged_underlying_not_allowed",
        )
        decimal = await self.client.put(
            path,
            json=self._options_policy_body(
                limits={
                    **self._options_policy_body()["limits"],
                    "assignment_budget_ceiling_usd": "5e4",
                }
            ),
            headers=action,
        )
        self.assertEqual(decimal.status_code, 422)
        self.assertEqual(
            decimal.json()["detail"],
            "options_policy_invalid_assignment_budget_ceiling_usd",
        )
        for response in (leveraged, decimal):
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_configured_policy_never_unblocks_live_or_broker_gates(self) -> None:
        await self._login()
        action = {"X-Finance-Radar-Action": "update-options-policy"}
        saved = await self.client.put(
            "/api/private/options/policy",
            json=self._options_policy_body(),
            headers=action,
        )
        self.assertEqual(saved.status_code, 200)

        overview = await self.client.get("/api/private/options/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.headers["cache-control"], "no-store")
        body = overview.json()
        readiness = {
            item["key"]: item for item in body["readiness"]["items"]
        }
        self.assertEqual(readiness["underwriting_policy"]["status"], "ready")
        self.assertEqual(readiness["familiar_universe"]["status"], "ready")
        for blocked in (
            "option_market_data",
            "funding_capacity",
            "options_permission",
            "event_calendar",
        ):
            self.assertTrue(readiness[blocked]["blocking"])
        self.assertEqual(body["data_status"], "insufficient")
        self.assertEqual(body["decision_state"], "abstain")
        self.assertEqual(body["candidate_count"], 0)
        self.assertEqual(body["candidates"], [])
        self.assertFalse(body["trade_execution_available"])

    async def test_corrupt_policy_storage_returns_private_503_without_cache(self) -> None:
        await self._login()
        await self.client.put(
            "/api/private/options/policy",
            json=self._options_policy_body(),
            headers={"X-Finance-Radar-Action": "update-options-policy"},
        )
        with db.conn(immediate=True) as connection:
            connection.execute("DROP TRIGGER option_policy_versions_no_update")
            connection.execute(
                "UPDATE option_policy_versions SET payload_sha256=?",
                ("0" * 64,),
            )

        for path in (
            "/api/private/options/policy",
            "/api/private/options/overview",
        ):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json()["detail"], "options_policy_storage_unavailable"
            )
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_manual_ai_request_requires_session_header_and_exact_body(
        self,
    ) -> None:
        event_id = self._seed_ai_event()
        body = {"subject_type": "event", "subject_id": str(event_id)}
        denied = await self.client.post(
            "/api/private/ai-requests",
            json=body,
            headers={
                "X-Finance-Radar-Action": "request-ai-enrichment"
            },
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")

        await self._login()
        missing_header = await self.client.post(
            "/api/private/ai-requests", json=body
        )
        self.assertEqual(missing_header.status_code, 403)
        self.assertEqual(missing_header.headers["cache-control"], "no-store")
        wrong_header = await self.client.post(
            "/api/private/ai-requests",
            json=body,
            headers={"X-Finance-Radar-Action": "refresh"},
        )
        self.assertEqual(wrong_header.status_code, 403)
        hostile_origin = await self.client.post(
            "/api/private/ai-requests",
            json=body,
            headers={
                "Origin": "https://attacker.example",
                "X-Finance-Radar-Action": "request-ai-enrichment",
            },
        )
        self.assertEqual(hostile_origin.status_code, 403)
        self.assertEqual(hostile_origin.headers["cache-control"], "no-store")
        preflight = await self.client.options(
            "/api/private/ai-requests",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-finance-radar-action",
            },
        )
        self.assertNotIn("access-control-allow-origin", preflight.headers)
        force = await self.client.post(
            "/api/private/ai-requests",
            json={**body, "force": True},
            headers={
                "X-Finance-Radar-Action": "request-ai-enrichment"
            },
        )
        self.assertEqual(force.status_code, 422)
        self.assertEqual(force.headers["cache-control"], "no-store")
        oversized_id = await self.client.post(
            "/api/private/ai-requests",
            json={
                "subject_type": "event",
                "subject_id": "9999999999999999999",
            },
            headers={
                "X-Finance-Radar-Action": "request-ai-enrichment"
            },
        )
        self.assertEqual(oversized_id.status_code, 422)
        self.assertEqual(oversized_id.headers["cache-control"], "no-store")

    async def test_manual_ai_request_queues_deduplicates_and_never_leaks_identity(
        self,
    ) -> None:
        event_id = self._seed_ai_event()
        await self._login()
        body = {"subject_type": "event", "subject_id": str(event_id)}
        headers = {
            "X-Finance-Radar-Action": "request-ai-enrichment"
        }
        queued = await self.client.post(
            "/api/private/ai-requests", json=body, headers=headers
        )
        duplicate = await self.client.post(
            "/api/private/ai-requests", json=body, headers=headers
        )
        status = await self.client.get(
            "/api/private/ai-requests/status", params=body
        )

        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["state"], "queued")
        self.assertEqual(queued.json()["wake_up"], "immediate")
        self.assertFalse(queued.json()["can_request"])
        self.assertTrue(
            (Path(self.temp_dir.name) / "enrichment.pending").exists()
        )
        self.assertEqual(duplicate.json()["state"], "already_queued")
        self.assertEqual(status.json()["state"], "queued")
        for response in (queued, duplicate, status):
            self.assertEqual(response.headers["cache-control"], "no-store")
            for private_name in (
                "input_hash",
                "prompt_version",
                "model",
                "claim_token",
                "request_id",
            ):
                self.assertNotIn(private_name, response.text)

    async def test_manual_ai_request_cached_and_latest_macro_subject(self) -> None:
        event_id = self._seed_ai_event()
        event = db.get_event_enrichment_subject(event_id)
        assert event is not None
        event_input, input_hash = llm_enrichment.build_event_input(event)
        token = db.claim_event_enrichment(
            event_id,
            input_hash=input_hash,
            prompt_version=llm_enrichment.PROMPT_VERSION,
            model=llm_enrichment.DEFAULT_MODEL,
            evidence_basis=event_input["evidence_basis"],
        )
        assert isinstance(token, str)
        db.save_event_enrichment(
            event_id,
            input_hash=input_hash,
            prompt_version=llm_enrichment.PROMPT_VERSION,
            model=llm_enrichment.DEFAULT_MODEL,
            claim_token=token,
            evidence_basis=event_input["evidence_basis"],
            result=api_enrichment_result(),
        )
        macro_event = {
            "id": "ind_policy_rate",
            "kind": "indicator",
            "title": "Policy rate changed",
            "source": "Official indicator",
            "previous_value": 4.0,
            "current_value": 4.25,
            "unit": "%",
            "severity": "high",
        }
        db.save_macro_snapshot(
            {
                "composite_risk": {"score": 50, "level": "medium"},
                "monitored_events": [macro_event],
            }
        )

        await self._login()
        headers = {
            "X-Finance-Radar-Action": "request-ai-enrichment"
        }
        event_body = {"subject_type": "event", "subject_id": str(event_id)}
        cached = await self.client.post(
            "/api/private/ai-requests", json=event_body, headers=headers
        )
        ready = await self.client.get(
            "/api/private/ai-requests/status", params=event_body
        )
        macro_body = {
            "subject_type": "macro_event",
            "subject_id": "ind_policy_rate",
        }
        macro = await self.client.post(
            "/api/private/ai-requests", json=macro_body, headers=headers
        )
        stale_macro = await self.client.get(
            "/api/private/ai-requests/status",
            params={
                "subject_type": "macro_event",
                "subject_id": "not-in-latest-snapshot",
            },
        )

        self.assertEqual(cached.json()["state"], "cached")
        self.assertEqual(ready.json()["state"], "ready")
        self.assertEqual(macro.json()["state"], "queued")
        self.assertEqual(stale_macro.status_code, 404)
        self.assertEqual(stale_macro.headers["cache-control"], "no-store")

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
        public_body = public.json()
        public_text = public.text.lower()
        self.assertIn("ai_semiconductors", public_text)
        summary = await self.client.get("/api/decisions/summary")
        self.assertEqual(summary.status_code, 200)
        self.assertIn("decision_overview", summary.json())
        public_card = public_body["decisions"][0]
        detail = await self.client.get(
            "/api/decisions/detail",
            params={
                "topic_key": public_card["topic_key"],
                "asset_key": public_card["asset_key"],
                "snapshot_id": public_body["snapshot_id"],
            },
        )
        self.assertEqual(detail.status_code, 200)
        public_payloads = (
            public.text + "\n" + summary.text + "\n" + detail.text
        ).lower()
        for private_value in (
            "must-never-be-public",
            "robinhood",
            "quantity",
            "matched_positions",
            "portfolio_overview",
            "portfolio_matched",
            "trade_execution_available",
        ):
            self.assertNotIn(private_value, public_payloads)

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
        private_body = private.json()
        card = private_body["decisions"][0]
        self.assertEqual(card["matched_positions"][0]["asset_key"], "US:NVDA")
        self.assertEqual(card["matched_positions"][0]["quantity"], 10.0)
        self.assertEqual(
            private_body["portfolio_overview"]["matched_position_count"],
            1,
        )
        self.assertEqual(
            private_body["decision_overview"]["portfolio_matched"],
            1,
        )

        impact = await self.client.get("/api/private/portfolio-impact")
        self.assertEqual(impact.status_code, 200)
        self.assertEqual(impact.headers["cache-control"], "no-store")
        body = impact.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertTrue(body["available"])
        self.assertEqual(
            body["decision_snapshot_id"],
            private_body["snapshot_id"],
        )
        self.assertEqual(body["snapshot"]["position_count"], 1)
        self.assertEqual(body["summary"]["position_count"], 1)
        self.assertEqual(body["summary"]["matched_position_count"], 1)
        self.assertEqual(body["summary"]["unmatched_position_count"], 0)
        self.assertEqual(body["matching_policy"], "exact_asset_key_v1")
        self.assertFalse(body["indirect_exposure_calculated"])
        self.assertFalse(body["trade_execution_available"])
        self.assertEqual(body["impacts"][0]["asset_key"], "US:NVDA")
        self.assertTrue(body["human_review_required"])

    async def test_event_api_defaults_to_recent_verified_publications(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def event(title: str, published_at: str | None) -> dict:
            return {
                "title": title,
                "url": f"https://example.com/{title.replace(' ', '-').lower()}",
                "snippet": "Jensen Huang discusses NVIDIA investment demand.",
                "source": "Test News",
                "kol_key": "huangrenxun",
                "kol_name": "Jensen Huang",
                "kol_name_cn": "黄仁勋",
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

    async def test_kol_api_includes_configured_people_without_sightings(self) -> None:
        response = await self.client.get("/api/kols")

        self.assertEqual(response.status_code, 200)
        by_key = {item["kol_key"]: item for item in response.json()}
        self.assertIn("serenity", by_key)
        self.assertTrue(by_key["serenity"]["configured"])
        self.assertEqual(by_key["serenity"]["total"], 0)
        self.assertEqual(by_key["serenity"]["total_24h"], 0)
        self.assertIsNone(by_key["serenity"]["last_published"])

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

    async def test_event_api_supports_persistent_multi_kol_filter_contract(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        shared = "Elon Musk and Donald Trump discuss AI investment"
        db.insert_events(
            [
                {
                    "title": shared,
                    "url": "https://x.com/elonmusk/status/shared",
                    "snippet": "Elon Musk discussed artificial intelligence investment.",
                    "source": "X @elonmusk",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": (now - timedelta(hours=2)).isoformat(),
                },
                {
                    "title": "Jensen Huang discusses NVIDIA AI demand",
                    "url": "https://example.com/jensen-ai-demand",
                    "snippet": "Jensen Huang discussed NVIDIA demand and investment.",
                    "source": "Test News",
                    "kol_key": "huangrenxun",
                    "kol_name": "Jensen Huang",
                    "kol_name_cn": "黄仁勋",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": (now - timedelta(minutes=30)).isoformat(),
                },
            ]
        )
        db.insert_events(
            [
                {
                    "title": shared,
                    "url": "https://truthsocial.com/@realDonaldTrump/shared",
                    "snippet": "Donald Trump discussed artificial intelligence investment.",
                    "source": "Truth Social @realDonaldTrump",
                    "kol_key": "trump",
                    "kol_name": "Donald Trump",
                    "kol_name_cn": "特朗普",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": (now - timedelta(hours=1)).isoformat(),
                }
            ]
        )

        legacy = await self.client.get("/api/events", params={"kol": "musk"})
        multi = await self.client.get(
            "/api/events", params={"kols": "musk,trump,musk"}
        )
        union = await self.client.get(
            "/api/events", params={"kol": "musk", "kols": "trump,musk"}
        )
        unfiltered = await self.client.get("/api/events", params={"kols": ""})
        unknown = await self.client.get(
            "/api/events", params={"kols": "unknown_but_valid"}
        )

        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["count"], 1)
        self.assertEqual(legacy.json()["items"][0]["kol_key"], "musk")
        self.assertEqual(multi.status_code, 200)
        self.assertEqual(multi.json()["count"], 1)
        selected = multi.json()["items"][0]
        self.assertEqual(selected["kol_key"], "trump")
        self.assertEqual(selected["kol_name"], "Donald Trump")
        self.assertEqual(selected["source"], "Truth Social @realDonaldTrump")
        self.assertEqual(
            selected["source_url"],
            "https://truthsocial.com/@realDonaldTrump/shared",
        )
        self.assertEqual(
            selected["published_at"], (now - timedelta(hours=1)).isoformat()
        )
        self.assertEqual(union.json(), multi.json())
        self.assertEqual(unfiltered.status_code, 200)
        self.assertEqual(unfiltered.json()["count"], 2)
        self.assertEqual(unknown.json(), {"items": [], "count": 0})

    async def test_event_api_rejects_invalid_or_excessive_multi_kol_filters(
        self,
    ) -> None:
        injected = await self.client.get(
            "/api/events", params={"kols": "musk') OR 1=1--"}
        )
        uppercase = await self.client.get(
            "/api/events", params={"kols": "Musk"}
        )
        too_many = await self.client.get(
            "/api/events",
            params={"kols": ",".join(f"kol_{index}" for index in range(21))},
        )
        too_long = await self.client.get(
            "/api/events", params={"kols": "a" * 1300}
        )
        invalid_legacy = await self.client.get(
            "/api/events", params={"kol": "musk OR 1=1"}
        )

        self.assertEqual(injected.status_code, 422)
        self.assertIn("invalid_kols", injected.json()["detail"])
        self.assertEqual(uppercase.status_code, 422)
        self.assertEqual(too_many.status_code, 422)
        self.assertIn("too_many_kols", too_many.json()["detail"])
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(invalid_legacy.status_code, 422)

    async def test_event_list_and_integer_detail_expose_nested_enrichment(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        event = {
            "title": (
                "Jensen Huang and Elon Musk discuss NVIDIA AI investment demand"
            ),
            "url": "https://example.com/current-nvidia-ai-demand",
            "snippet": (
                "Jensen Huang and Elon Musk said NVIDIA AI investment demand "
                "remains in focus."
            ),
            "source": "Test News",
            "kol_key": "huangrenxun",
            "kol_name": "Jensen Huang",
            "kol_name_cn": "黄仁勋",
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
                    "snippet": (
                        "Elon Musk said Tesla deliveries rose while NVIDIA AI "
                        "investment remained strong."
                    ),
                    "source": "Interview transcript",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                    "tickers": ["TSLA"],
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
        exact_body = detail_for_source.json()
        exact = exact_body["event"]
        self.assertEqual(exact["kol_key"], "musk")
        self.assertEqual(exact["source"], "Interview transcript")
        self.assertEqual(
            exact["source_url"],
            "https://example.com/musk-ai-interview",
        )
        self.assertEqual(
            exact["snippet"],
            "Elon Musk said Tesla deliveries rose while NVIDIA AI "
            "investment remained strong.",
        )
        self.assertEqual(exact["tickers"], ["TSLA"])
        self.assertEqual(exact["ai_status"], "ineligible")
        self.assertFalse(exact["ai_request_eligible"])
        self.assertIsNone(exact["ai_enrichment"])
        self.assertEqual(exact["rule_impact"], "medium")
        self.assertEqual(exact["impact"], "medium")
        self.assertEqual(exact_body["relations"], [])
        self.assertEqual(exact_body["market_reactions"], [])
        primary = exact_body["primary_ai_subject"]
        self.assertEqual(primary["source"], "X @elonmusk")
        self.assertEqual(primary["tickers"], ["NVDA"])
        self.assertEqual(primary["ai_status"], "ready")
        self.assertTrue(primary["ai_request_eligible"])
        self.assertEqual(primary["impact"], "high")
        self.assertEqual(primary["relations"][0]["asset_key"], "US:NVDA")
        self.assertEqual(primary["market_reactions"], [])
        self.assertEqual(
            primary["ai_enrichment"]["headline_zh"],
            "人工智能需求推动英伟达信号升温",
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
                    "title": "Jensen Huang says NVIDIA AI platform demand rises",
                    "url": "https://example.com/current-nvidia",
                    "snippet": (
                        "Jensen Huang described NVIDIA data center demand "
                        "and AI investment."
                    ),
                    "source": "Test",
                    "kol_key": "huangrenxun",
                    "kol_name": "Jensen Huang",
                    "kol_name_cn": "黄仁勋",
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
                "title": "Jensen Huang says NVIDIA AI platform demand rises",
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

    async def test_daily_briefing_is_read_only_cached_and_public_safe(self) -> None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        db.insert_events(
            [
                {
                    "title": "Elon Musk discusses Tesla shares and AI investment",
                    "url": "https://x.com/elonmusk/status/123456",
                    "snippet": (
                        "Elon Musk discusses Tesla shares and AI investment in "
                        "this sufficiently substantive original social post."
                    ),
                    "source": "X @elonmusk",
                    "kol_key": "musk",
                    "kol_name": "Elon Musk",
                    "kol_name_cn": "马斯克",
                    "impact": "medium",
                    "has_market_kw": True,
                    "published_at": generated_at,
                    "account": "PRIVATE-EVENT-ACCOUNT",
                }
            ]
        )
        official_url = (
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "monetary20260904a.htm"
        )
        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": generated_at,
                "composite_risk": {"score": 61, "level": "high"},
                "monitored_events": [
                    {
                        "id": "fed-daily",
                        "kind": "policy",
                        "title": "Federal Reserve issues FOMC statement",
                        "url": official_url,
                        "source": "Federal Reserve",
                        "published_at": generated_at,
                        "time_status": "verified",
                        "severity": "high",
                        "category": "fomc_statement",
                        "content_status": "ready",
                        "content_excerpt": "The Committee published its decision.",
                        "content_source_url": official_url,
                        "evidence_sections": [],
                        "account": "PRIVATE-MACRO-ACCOUNT",
                    }
                ],
                "data_coverage": {"available": 1, "total": 1, "pct": 100},
                "market_data": {},
                "sub_scores": {},
                "portfolio": {"positions": ["PRIVATE-MACRO-POSITION"]},
            }
        )
        db.save_decision_snapshot(
            schema_version=dashboard_app.decision_service.DECISION_SNAPSHOT_SCHEMA_VERSION,
            engine_version=dashboard_app.decision_service.DECISION_ENGINE_VERSION,
            source_hash="daily-public-safe",
            source_as_of=generated_at,
            generated_at=generated_at,
            summary={
                "decisions": [
                    {
                        "topic_key": "ai_semiconductors",
                        "asset_key": "US:NVDA",
                        "direction": "negative",
                        "action_stage": "verify",
                        "total_score": 0.7,
                        "source_count": 1,
                        "market_validation": {
                            "status": "pending",
                            "applicability_reason": "window_not_due",
                            "account": "PRIVATE-DECISION-ACCOUNT",
                        },
                        "positions": ["PRIVATE-DECISION-POSITION"],
                    }
                ]
            },
            full={
                "decisions": [],
                "portfolio": "PRIVATE-FULL-PORTFOLIO",
            },
        )

        with (
            patch.object(
                dashboard_app.decision_snapshot,
                "ensure_public_snapshot",
                side_effect=AssertionError("must not rebuild a decision snapshot"),
            ),
            patch.object(
                dashboard_app.db,
                "request_ai_enrichment",
                side_effect=AssertionError("must not queue AI work"),
            ),
            patch.object(
                dashboard_app.llm_enrichment,
                "load_config",
                side_effect=AssertionError("must not load an LLM provider"),
            ),
        ):
            response = await self.client.get("/api/briefings/latest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "public, max-age=60")
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "available",
                "date",
                "edition",
                "edition_label",
                "generated_at",
                "source_as_of",
                "content_as_of",
                "source_coverage_as_of",
                "source_coverage_stale",
                "stale",
                "coverage",
                "coverage_window_hours",
                "dedup_stats",
                "lead",
                "highlights",
                "firsthand",
                "next_refresh_at",
                "refresh_schedule_status",
                "sections",
                "watchpoints",
                "disclaimer",
            },
        )
        self.assertTrue(body["available"])
        self.assertTrue(body["firsthand"])
        self.assertEqual(body["firsthand"][0]["source_tier"], "official")
        self.assertTrue(
            any(item["source_tier"] == "first_party" for item in body["firsthand"])
        )
        self.assertLessEqual(len(body["highlights"]), 5)
        self.assertLessEqual(len(body["firsthand"]), 6)
        self.assertLessEqual(len(body["watchpoints"]), 5)
        encoded = response.text.lower()
        for forbidden in (
            "private-event-account",
            "private-macro-account",
            "private-macro-position",
            "private-decision-account",
            "private-decision-position",
            "private-full-portfolio",
        ):
            self.assertNotIn(forbidden, encoded)

    async def test_daily_briefing_passes_a_current_imported_snapshot(self) -> None:
        generated = datetime.now(timezone.utc).replace(microsecond=0)
        snapshot = {
            "schema_version": 1,
            "snapshot_date": (generated + timedelta(hours=8)).date().isoformat(),
            "generated_at": generated.isoformat(),
            "source_as_of": generated.isoformat(),
            "sections": {
                section: [] for section in briefing_import.SECTION_KEYS
            },
        }
        snapshot["sections"]["ai"].append(
            {
                "title": "AI lab publishes a new model system card",
                "source": "AI lab",
                "source_url": "https://example.com/ai/system-card",
                "published_at": (generated - timedelta(minutes=5)).isoformat(),
                "source_tier": "official",
                "summary": "The system card documents the new model release.",
                "why_it_matters": "Primary documentation limits rumor risk.",
                "assets": ["THEME:AI"],
            }
        )
        briefing_import.import_payload(snapshot, now=generated)

        with patch.dict(
            os.environ,
            {"KOL_DAILY_REFRESH_SCHEDULE": "hourly"},
        ), patch.object(
            dashboard_app.briefing_service,
            "build_latest_briefing",
            return_value={"available": True},
        ) as build:
            response = await self.client.get("/api/briefings/latest")

        self.assertEqual(response.status_code, 200)
        imported = build.call_args.kwargs["imported_snapshot"]
        self.assertIsNotNone(imported)
        self.assertEqual(build.call_args.kwargs["refresh_schedule"], "hourly")
        self.assertEqual(
            imported["sections"]["ai"][0]["title"],
            "AI lab publishes a new model system card",
        )

    async def test_daily_briefing_distinguishes_empty_scan_from_missing_job(
        self,
    ) -> None:
        generated = datetime.now(timezone.utc).replace(microsecond=0)
        snapshot = {
            "schema_version": 1,
            "snapshot_date": (generated + timedelta(hours=8)).date().isoformat(),
            "generated_at": generated.isoformat(),
            "source_as_of": generated.isoformat(),
            "sections": {
                section: [] for section in briefing_import.SECTION_KEYS
            },
        }
        briefing_import.import_payload(snapshot, now=generated)

        response = await self.client.get("/api/briefings/latest")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertIsNone(body["content_as_of"])
        self.assertIsNone(body["source_as_of"])
        self.assertEqual(body["source_coverage_as_of"], generated.isoformat())
        self.assertFalse(body["source_coverage_stale"])
        self.assertEqual(body["coverage"]["total"], 0)

    async def test_daily_briefing_preserves_investor_disclosure_dates_end_to_end(
        self,
    ) -> None:
        generated = datetime.now(timezone.utc).replace(microsecond=0)
        disclosed = generated - timedelta(minutes=8)
        snapshot = {
            "schema_version": 1,
            "snapshot_date": (generated + timedelta(hours=8)).date().isoformat(),
            "generated_at": generated.isoformat(),
            "source_as_of": generated.isoformat(),
            "sections": {
                section: [] for section in briefing_import.SECTION_KEYS
            },
        }
        snapshot["sections"]["investors"].append(
            {
                "title": "Investor files quarterly holdings",
                "source": "SEC",
                "source_url": "https://www.sec.gov/edgar/browse/example",
                "published_at": disclosed.isoformat(),
                "disclosed_at": disclosed.isoformat(),
                "period_end": "2026-06-30",
                "source_tier": "official",
                "summary": "The filing reports prior-quarter holdings.",
            }
        )
        briefing_import.import_payload(snapshot, now=generated)

        response = await self.client.get("/api/briefings/latest")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        investors = next(
            section
            for section in body["sections"]
            if section["key"] == "investors"
        )
        self.assertEqual(len(investors["items"]), 1)
        item = investors["items"][0]
        self.assertEqual(item["disclosed_at"], disclosed.isoformat())
        self.assertEqual(item["effective_at"], "2026-06-30")
        self.assertNotIn("period_end", response.text)
        self.assertNotIn("data_as_of", response.text)

    async def test_daily_briefing_ignores_a_source_stale_import(self) -> None:
        generated = (
            datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(hours=25)
        )
        snapshot = {
            "schema_version": 1,
            "snapshot_date": (generated + timedelta(hours=8)).date().isoformat(),
            "generated_at": generated.isoformat(),
            "source_as_of": generated.isoformat(),
            "sections": {
                section: [] for section in briefing_import.SECTION_KEYS
            },
        }
        snapshot["sections"]["world"].append(
            {
                "title": "Stale world news must not replace live fallback data",
                "source_url": "https://example.com/world/stale",
                "published_at": generated.isoformat(),
                "source_tier": "media",
            }
        )
        briefing_import.import_payload(
            snapshot,
            now=generated,
            imported_at=generated,
        )

        with patch.object(
            dashboard_app.briefing_service,
            "build_latest_briefing",
            return_value={"available": False},
        ) as build:
            response = await self.client.get("/api/briefings/latest")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(build.call_args.kwargs["imported_snapshot"])

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

    async def test_macro_api_exposes_market_alerts_without_private_state(self) -> None:
        generated_at = datetime.now(timezone.utc).isoformat()
        db.save_macro_snapshot(
            {
                "public_schema_version": 1,
                "timestamp": "2026-09-06T01:00:00+00:00",
                "composite_risk": {"score": 50, "level": "medium"},
                "market_alerts": {
                    "schema_version": 1,
                    "method_version": "macro-de-risk-trial-v1",
                    "generated_at": generated_at,
                    "mode": "trial",
                    "human_review_required": True,
                    "automatic_execution": False,
                    "markets": [
                        {
                            "market": "US",
                            "action": "prepare_reduce",
                            "risk_level": "medium",
                            "abstain": False,
                            "data_status": "ok",
                            "summary": "等待第二个收盘日确认",
                            "gates": [],
                            "account": "PRIVATE-ACCOUNT",
                            "confirmation": {"reduce_dates": ["2026-09-05"]},
                        },
                        {
                            "market": "CN",
                            "action": "observe",
                            "risk_level": "insufficient",
                            "abstain": True,
                            "data_status": "insufficient",
                            "summary": "数据不足",
                            "gates": [],
                        },
                    ],
                },
            }
        )

        response = await self.client.get("/api/macro")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(
            [item["market"] for item in payload["market_alerts"]["markets"]],
            ["US", "CN"],
        )
        self.assertEqual(
            payload["market_alerts"]["markets"][0]["action"],
            "prepare_reduce",
        )
        self.assertNotIn("confirmation", response.text)
        self.assertNotIn("PRIVATE-ACCOUNT", response.text)

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
        self.assertEqual(
            payload["business_health"]["market_validation"]["status"],
            "unavailable",
        )
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

        generated = datetime.fromisoformat(payload["generated_at"])
        fresh_time = generated + timedelta(
            seconds=dashboard_app.decision_snapshot.STALE_AFTER_SECONDS - 1
        )
        stale_time = generated + timedelta(
            seconds=dashboard_app.decision_snapshot.STALE_AFTER_SECONDS + 1
        )
        with patch.object(
            dashboard_app,
            "_decision_response_now",
            return_value=fresh_time,
        ):
            fresh = await self.client.get("/api/decisions/summary")
        self.assertFalse(fresh.json()["stale"])
        with patch.object(
            dashboard_app,
            "_decision_response_now",
            return_value=stale_time,
        ):
            crossed_threshold = await self.client.get(
                "/api/decisions/summary",
                headers={"If-None-Match": fresh.headers["etag"]},
            )
        self.assertEqual(crossed_threshold.status_code, 200)
        self.assertTrue(crossed_threshold.json()["stale"])
        self.assertNotEqual(
            crossed_threshold.headers["etag"],
            fresh.headers["etag"],
        )

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
        self.assertIn("market_validation", detail.json()["business_health"])
        self.assertNotIn("must-never-be-public", detail.text.lower())


if __name__ == "__main__":
    unittest.main()
