"""No-network tests for DeepSeek transport, redaction, and accounting."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request

from research.podcast_poc import inference


MESSAGES = [{"role": "system", "content": '仅据资料输出 JSON，例如 {"overview":"中文"}。'},
            {"role": "user", "content": "需要归纳的公开文字。"}]


def response(content='{"overview":"中文"}', *, usage=None, finish_reason="stop"):
    result = {"choices": [{"finish_reason": finish_reason,
                            "message": {"content": content, "reasoning_content": "must not return"}}],
              "usage": usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.status = 200
    mock.read.return_value = json.dumps(result, ensure_ascii=False).encode()
    return mock


class DeepSeekClientTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-test-only-key"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.builder = patch.object(inference, "build_opener")
        self.build = self.builder.start()
        self.addCleanup(self.builder.stop)
        self.opener = self.build.return_value
        self.opener.open.return_value = response()

    def test_local_queue_provider_checks_permission_before_every_paid_attempt(self):
        policy = inference.schedule_policy
        client = inference.Client()
        selected = [False]
        with patch.object(policy, "period_at", return_value="peak"), policy.summary_request_context(lambda: selected[0]):
            with self.assertRaises(policy.OffPeakDeferred):
                client.generate(MESSAGES, 1100)
            self.assertEqual(client.stats()["paid_api_calls"], 0)
            self.opener.open.assert_not_called()
            selected[0] = True
            self.assertEqual(client.generate(MESSAGES, 1100), '{"overview":"中文"}')
            selected[0] = False
            with self.assertRaises(policy.OffPeakDeferred):
                client.generate(MESSAGES, 1100)
        self.assertEqual(client.stats()["paid_api_calls"], 1)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_public_config_and_model_allowlist(self):
        self.assertEqual(inference.config(), {"provider": "deepseek", "model": "deepseek-v4-flash"})
        for model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"):
            with patch.dict(os.environ, {"DEEPSEEK_MODEL": model}):
                self.assertEqual(inference.Client().model, model)
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "not-a-supported-model"}):
            self.assertEqual(inference.config()["model"], "deepseek-v4-flash")
        self.assertNotIn("fake-test-only-key", repr(inference.config()))

    def test_missing_or_malformed_key_never_opens_network(self):
        for key in ("", " \n", "fake\nInjected: value", "secret\tpart", "非ASCII"):
            with self.subTest(key_kind=len(key)), patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
                with self.assertRaises(inference.DeepSeekError) as caught:
                    inference.Client()
                if key.strip():
                    self.assertNotIn(key, str(caught.exception))
        self.opener.open.assert_not_called()

    def test_fixed_endpoint_json_contract_and_text_only_result(self):
        client = inference.Client()
        self.assertIsInstance(self.build.call_args.args[0], inference._NoRedirect)
        with patch.dict(os.environ, {"DEEPSEEK_BASE_URL": "https://untrusted.invalid/path"}):
            text = client.generate(MESSAGES, 1100)
        self.assertEqual(text, '{"overview":"中文"}')
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-only-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 1100)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"], MESSAGES)
        self.assertNotIn("fake-test-only-key", request.data.decode())
        self.assertEqual(client.stats(), {"paid_api_calls": 1,
                         "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                         "usage_unreported_calls": 0})

    def test_timeout_is_finite_and_bounded(self):
        for raw, expected in (("999", 120), ("1", 10), ("bad", 45), ("nan", 45), ("inf", 45), ("60", 60)):
            with self.subTest(raw=raw), patch.dict(os.environ, {"DEEPSEEK_TIMEOUT_SECONDS": raw}):
                client = inference.Client()
                client.generate(MESSAGES, 20)
                self.assertEqual(self.opener.open.call_args.kwargs["timeout"], expected)

    def test_every_redirect_is_blocked_before_a_second_request(self):
        handler = inference._NoRedirect()
        request = Request(inference.ENDPOINT, data=b"{}", headers={"Authorization": "Bearer fake-test-only-key"})
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(request, None, code, "redirect", {}, "https://elsewhere.invalid/"))

    def test_default_proxy_handler_is_preserved_without_network(self):
        # Constructing a real opener does no I/O; the client calls remain mocked.
        from urllib.request import build_opener
        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:19999"}):
            opener = build_opener(inference._NoRedirect())
        self.assertTrue(any(isinstance(h, ProxyHandler) for h in opener.handlers))
        redirects = [h for h in opener.handlers if isinstance(h, HTTPRedirectHandler)]
        self.assertEqual(len(redirects), 1)
        self.assertIsInstance(redirects[0], inference._NoRedirect)

    def test_http_failures_do_not_read_or_leak_provider_details_or_retry(self):
        for code, diagnostic in ((401, "authentication"), (402, "balance"), (429, "rate_limit"), (500, "http_error"), (302, "redirect")):
            with self.subTest(code=code):
                body = MagicMock(wraps=io.BytesIO(b"secret provider body fake-test-only-key"))
                error = HTTPError("https://user:password@host.invalid/?token=secret", code,
                                  "unsafe reason fake-test-only-key", {"Secret": "fake-test-only-key"}, body)
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                client = inference.Client()
                with self.assertRaises(inference.DeepSeekError) as caught:
                    client.generate(MESSAGES, 20)
                self.assertEqual(caught.exception.code, diagnostic)
                self.assertEqual(caught.exception.http_status, code)
                self.assertNotIn("fake-test-only-key", str(caught.exception))
                self.assertNotIn("password", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                body.read.assert_not_called()
                self.opener.open.assert_called_once()
                self.assertEqual(client.stats()["paid_api_calls"], 1)
                self.assertEqual(client.stats()["usage_unreported_calls"], 1)

    def test_network_and_timeout_errors_are_sanitized(self):
        for error, expected in ((TimeoutError("fake-test-only-key"), "timeout"),
                                (URLError(TimeoutError("unsafe")), "timeout"),
                                (URLError("https://user:password@proxy.invalid"), "network"),
                                (OSError("fake-test-only-key"), "network")):
            with self.subTest(expected=expected):
                self.opener.open.side_effect = error
                client = inference.Client()
                with self.assertRaises(inference.DeepSeekError) as caught:
                    client.generate(MESSAGES, 20)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("fake-test-only-key", str(caught.exception))
                self.assertNotIn("password", str(caught.exception))
                self.assertEqual(client.stats()["paid_api_calls"], 1)

    def test_usage_is_cumulative_and_snapshots_cannot_mutate_counters(self):
        self.opener.open.side_effect = [response(), response(usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})]
        client = inference.Client()
        client.generate(MESSAGES, 20)
        snapshot = client.stats()
        snapshot["usage"]["prompt_tokens"] = 999
        client.generate(MESSAGES, 20)
        self.assertEqual(client.stats()["usage"], {"prompt_tokens": 13, "completion_tokens": 6, "total_tokens": 19})
        self.assertEqual(client.stats()["paid_api_calls"], 2)

    def test_missing_or_invalid_usage_does_not_fabricate_known_zero_cost(self):
        self.opener.open.return_value = response(usage={"prompt_tokens": True, "completion_tokens": -2, "total_tokens": "14"})
        client = inference.Client()
        client.generate(MESSAGES, 20)
        self.assertEqual(client.stats()["usage"], {field: 0 for field in inference.USAGE_FIELDS})
        self.assertEqual(client.stats()["usage_unreported_calls"], 1)

    def test_incomplete_or_empty_answer_is_not_delivered_but_usage_is_recorded(self):
        for mock, code in ((response(finish_reason="length"), "incomplete_output"),
                           (response(content=""), "empty_output"),
                           (response(content=None), "empty_output")):
            with self.subTest(code=code):
                self.opener.open.return_value = mock
                client = inference.Client()
                with self.assertRaises(inference.DeepSeekError) as caught:
                    client.generate(MESSAGES, 20)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(client.stats()["usage"]["total_tokens"], 14)

    def test_bad_response_envelopes_and_oversized_bodies_fail_safely(self):
        for raw, code in ((b"secret: fake-test-only-key", "invalid_response"), (b"[]", "invalid_response"),
                          (b'{"choices": []}', "invalid_response"),
                          (b"x" * (inference.MAX_RESPONSE_BYTES + 1), "response_too_large")):
            with self.subTest(code=code):
                mock = response(); mock.read.return_value = raw
                self.opener.open.return_value = mock
                client = inference.Client()
                with self.assertRaises(inference.DeepSeekError) as caught:
                    client.generate(MESSAGES, 20)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("fake-test-only-key", str(caught.exception))

    def test_invalid_input_is_rejected_before_attempt_count_or_network(self):
        client = inference.Client()
        for tokens in (True, 0, -1, 393217, 2.5, "20"):
            with self.assertRaises(ValueError):
                client.generate(MESSAGES, tokens)
        for messages in ([], [None], [{"role": "tool", "content": "JSON"}],
                         [{"role": [], "content": "JSON"}], [{"role": "user", "content": 1}],
                         [{"role": "user", "content": "no format instruction"}]):
            with self.assertRaises(ValueError):
                client.generate(messages, 20)
        self.opener.open.assert_not_called()
        self.assertEqual(client.stats()["paid_api_calls"], 0)

    def test_explicit_private_file_precedes_environment_and_missing_file_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"api_key": "fake-local-key"}))
            path.chmod(0o600)
            inference.Client(credentials_path=path).generate(MESSAGES, 20)
            self.assertEqual(self.opener.open.call_args.args[0].get_header("Authorization"), "Bearer fake-local-key")
            path.unlink()
            inference.Client(credentials_path=path).generate(MESSAGES, 20)
            self.assertEqual(self.opener.open.call_args.args[0].get_header("Authorization"), "Bearer fake-test-only-key")

    def test_default_client_does_not_open_any_credential_file(self):
        with patch.object(inference.os, "open") as open_file:
            inference.Client()
        open_file.assert_not_called()

    def test_malformed_private_file_fails_closed_without_network_or_key_disclosure(self):
        values = ['fake-local-secret', '[]', '{}', '{"api_key": null}',
                  '{"api_key":"fake-local-secret","extra":"bad"}', '{"api_key":"bad\\nsecret"}',
                  'x' * (inference.MAX_CREDENTIAL_BYTES + 1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            for value in values:
                path.write_text(value)
                path.chmod(0o600)
                with self.assertRaises(inference.DeepSeekError) as caught:
                    inference.Client(credentials_path=path)
                self.assertNotIn("fake-local-secret", str(caught.exception))
                self.assertNotIn("fake-test-only-key", str(caught.exception))
        self.opener.open.assert_not_called()

    def test_insecure_or_symlinked_credential_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"api_key":"fake-local-key"}')
            path.chmod(0o644)
            with self.assertRaises(inference.DeepSeekError) as caught:
                inference.Client(credentials_path=path)
            self.assertEqual(caught.exception.code, "credentials_permissions")
            path.chmod(0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(inference.DeepSeekError):
                inference.Client(credentials_path=link)
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
