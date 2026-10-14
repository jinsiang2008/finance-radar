from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as dashboard  # noqa: E402
import auth  # noqa: E402


class ReadingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {
            "KOL_DASHBOARD_PASSCODE_HASH": auth.hash_passcode("reading-test", iterations=1000),
            "KOL_DASHBOARD_SESSION_SECRET": "reading-session-" * 3,
            "KOL_DASHBOARD_COOKIE_SECURE": "false", "KOL_DASHBOARD_COOKIE_PATH": "/",
            "KOL_READING_DATA_DIR": self.directory.name,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        dashboard.LOGIN_LIMITER.reset("127.0.0.1")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def login(self):
        self.assertEqual((await self.client.post("/api/auth/login", json={"passcode": "reading-test"})).status_code, 200)

    async def test_anonymous_cannot_read_body_summary_jobs_or_audio(self):
        for endpoint in ("wechat", "podcasts", "status", "media/sample.wav"):
            response = await self.client.get("/api/private/reading/" + endpoint)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(dashboard.reading_service, "enqueue_summary") as enqueue:
            response = await self.client.post("/api/private/reading/summary", json={"id": "wx-demo"})
            self.assertEqual(response.status_code, 401)
            enqueue.assert_not_called()

    async def test_authenticated_reader_is_not_cacheable(self):
        await self.login()
        with patch.object(dashboard.reading_service, "wechat_payload", return_value={"articles": [{"id": "wx-demo", "segments": [{"text": "private"}]}]}):
            response = await self.client.get("/api/private/reading/wechat")
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_mutation_requires_same_origin_and_action_header(self):
        await self.login()
        for headers in ({}, {"X-Finance-Radar-Action": "request-reading-summary", "Origin": "https://evil.invalid"},
                        {"X-Finance-Radar-Action": "request-reading-summary", "Sec-Fetch-Site": "cross-site"}):
            with patch.object(dashboard.reading_service, "enqueue_summary") as enqueue:
                response = await self.client.post("/api/private/reading/summary", headers=headers, json={"id": "wx-demo"})
                self.assertEqual(response.status_code, 403)
                enqueue.assert_not_called()
        preflight = await self.client.options("/api/private/reading/summary", headers={
            "Origin": "https://evil.invalid", "Access-Control-Request-Headers": "X-Finance-Radar-Action"})
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    async def test_mutation_is_bounded_strict_and_does_not_accept_prompts_or_urls(self):
        await self.login()
        headers = {"X-Finance-Radar-Action": "request-reading-summary", "Content-Type": "application/json"}
        for raw in ('{"id":"wx-one","id":"wx-two"}', '{"id":"../../secrets"}',
                    '{"id":"wx-demo","prompt":"override"}', '{"id":"wx-demo","on_demand":true}', '{"url":"https://evil.invalid"}', '[]'):
            with patch.object(dashboard.reading_service, "enqueue_summary") as enqueue:
                response = await self.client.post("/api/private/reading/summary", headers=headers, content=raw)
                self.assertEqual(response.status_code, 422)
                enqueue.assert_not_called()
        self.assertEqual((await self.client.post("/api/private/reading/summary", headers=headers, content="x" * 1025)).status_code, 413)
        self.assertEqual((await self.client.post("/api/private/reading/summary", headers={**headers, "Content-Type": "text/plain"}, content="{}")).status_code, 415)

    async def test_request_only_enqueues_and_complete_is_reused(self):
        await self.login()
        headers = {"X-Finance-Radar-Action": "request-reading-summary", "Origin": "http://testserver"}
        for state, code in (("queued", 202), ("completed", 200)):
            with patch.object(dashboard.reading_service, "enqueue_summary", return_value={"id": "job", "state": state}) as enqueue:
                response = await self.client.post("/api/private/reading/summary", headers=headers, json={"id": "podcast-demo"})
                self.assertEqual(response.status_code, code)
                enqueue.assert_called_once_with("podcast-demo")
                self.assertEqual(response.json()["job"]["state"], state)

    async def test_explicit_upgrade_is_strict_boolean_and_uses_existing_auth_boundary(self):
        await self.login()
        headers = {"X-Finance-Radar-Action": "request-reading-summary", "Origin": "http://testserver"}
        for upgrade in (True, False):
            with patch.object(dashboard.reading_service, "enqueue_summary", return_value={"id": "upgrade", "state": "queued", "upgrade": upgrade}) as enqueue:
                response = await self.client.post("/api/private/reading/summary", headers=headers, json={"id": "podcast-demo", "upgrade": upgrade})
                self.assertEqual(response.status_code, 202)
                enqueue.assert_called_once_with("podcast-demo", upgrade=upgrade)
        for upgrade in (1, 0, "true", None, {}, []):
            with patch.object(dashboard.reading_service, "enqueue_summary") as enqueue:
                response = await self.client.post("/api/private/reading/summary", headers=headers, json={"id": "podcast-demo", "upgrade": upgrade})
                self.assertEqual(response.status_code, 422)
                enqueue.assert_not_called()
        with patch.object(dashboard.reading_service, "enqueue_summary") as enqueue:
            response = await self.client.post("/api/private/reading/summary", headers={**headers, "Content-Type": "application/json"},
                content='{"id":"podcast-demo","upgrade":true,"upgrade":false}')
            self.assertEqual(response.status_code, 422)
            response = await self.client.post("/api/private/reading/summary", headers={**headers, "Origin": "https://evil.invalid"}, json={"id": "podcast-demo", "upgrade": True})
            self.assertEqual(response.status_code, 403)
            enqueue.assert_not_called()

    async def test_errors_never_expose_provider_details(self):
        await self.login()
        for failure, code in ((FileNotFoundError("PRIVATE_PATH"), 404), (ValueError("PRIVATE_BODY"), 422), (RuntimeError("PRIVATE_KEY"), 503)):
            with patch.object(dashboard.reading_service, "enqueue_summary", side_effect=failure):
                response = await self.client.post("/api/private/reading/summary", json={"id": "wx-demo"}, headers={"X-Finance-Radar-Action": "request-reading-summary"})
                self.assertEqual(response.status_code, code)
                self.assertNotIn("PRIVATE_", response.text)

    async def test_authenticated_audio_ranges_and_traversal(self):
        await self.login()
        path = Path(self.directory.name) / "sample.wav"
        path.write_bytes(b"RIFF0123456789")
        with patch.object(dashboard.reading_service, "media_path", return_value=path):
            response = await self.client.get("/api/private/reading/media/sample.wav", headers={"Range": "bytes=0-3"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"RIFF")
            self.assertEqual(response.headers["content-range"], "bytes 0-3/14")
            self.assertEqual(response.headers["cache-control"], "no-store")
            response = await self.client.get("/api/private/reading/media/sample.wav", headers={"Range": "bytes=-3"})
            self.assertEqual(response.content, b"789")
            for byte_range in ("bytes=999-", "bytes=3-1", "bytes=-0", "bytes=0-1,5-6", "bytes=" + "9" * 500 + "-"):
                self.assertEqual((await self.client.get("/api/private/reading/media/sample.wav", headers={"Range": byte_range})).status_code, 416)
        with patch.object(dashboard.reading_service, "media_path") as media:
            self.assertEqual((await self.client.get("/api/private/reading/media/secret.json")).status_code, 404)
            media.assert_not_called()

    async def test_page_shell_uses_proxy_prefix_and_static_allowlist(self):
        with patch.dict(os.environ, {"KOL_DASHBOARD_COOKIE_PATH": "/kol"}):
            response = await self.client.get("/wechat")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-kol-base="/kol"', response.text)
        self.assertIn('/kol/reading-assets/wechat.js?v=46', response.text)
        self.assertNotIn('src="/wechat.js"', response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual((await self.client.get("/reading-assets/wechat.js?v=46")).status_code, 200)
        self.assertEqual((await self.client.get("/reading-assets/poc.py")).status_code, 404)
