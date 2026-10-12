"""Only mock HTTP and fake keys in temporary directories; no real credentials."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from research.podcast_poc import configure_deepseek as configure, inference


FAKE_KEY = "fake-interactive-test-secret"


class ConfigureDeepSeekTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.path = self.data / "runtime" / "deepseek-credentials.json"
        self.patch_data = patch.object(configure.poc, "DATA", self.data)
        self.patch_data.start()
        self.addCleanup(self.patch_data.stop)
        self.builder = patch.object(configure, "build_opener")
        self.build = self.builder.start()
        self.addCleanup(self.builder.stop)
        self.opener = self.build.return_value
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.status = 200
        self.response.read.return_value = json.dumps({"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "98765.43"}]}).encode()
        self.opener.open.return_value = self.response

    def run_main(self, *, interactive=True):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch.object(configure.sys.stdin, "isatty", return_value=interactive), \
                patch.object(configure.getpass, "getpass", return_value=FAKE_KEY) as prompt:
            result = configure.main()
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertNotIn("98765.43", output.getvalue())
        return result, output.getvalue(), prompt

    def test_one_read_only_validation_then_private_atomic_save(self):
        result, output, prompt = self.run_main()
        self.assertEqual(result, 0)
        prompt.assert_called_once()
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/user/balance")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertIsInstance(self.build.call_args.args[0], inference._NoRedirect)
        self.assertEqual(json.loads(self.path.read_text()), {"api_key": FAKE_KEY})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertIn("Git 已忽略", output)
        settings = json.loads((self.path.parent / "automation.json").read_text())
        self.assertEqual(settings["credentials_status"], "verified")
        self.assertFalse(settings["enabled"])
        self.assertIn("请回到当前聊天完成启用", output)

    def test_replacement_of_existing_permissive_file_is_private(self):
        self.path.parent.mkdir()
        self.path.write_text('{"api_key":"old-fake-key"}')
        self.path.chmod(0o644)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_validation_preserves_existing_file_and_never_prints_provider_body(self):
        self.path.parent.mkdir()
        original = '{"api_key":"old-fake-key"}'
        self.path.write_text(original)
        self.path.chmod(0o600)
        for status in (401, 402, 429, 302, 500):
            with self.subTest(status=status):
                body = MagicMock(wraps=io.BytesIO(FAKE_KEY.encode()))
                self.opener.open.reset_mock()
                self.opener.open.side_effect = HTTPError("https://unsafe.invalid/?key=" + FAKE_KEY,
                    status, FAKE_KEY, {"Secret": FAKE_KEY}, body)
                result, output, _ = self.run_main()
                self.assertEqual(result, 1)
                self.assertIn(f"HTTP {status}", output)
                self.assertEqual(self.path.read_text(), original)
                self.opener.open.assert_called_once()
                body.read.assert_not_called()

    def test_network_failure_or_invalid_account_response_never_saves(self):
        for error in (TimeoutError(FAKE_KEY), URLError(FAKE_KEY), OSError(FAKE_KEY)):
            self.opener.open.side_effect = error
            self.assertEqual(self.run_main()[0], 1)
            self.assertFalse(self.path.exists())
        self.opener.open.side_effect = None
        for raw in (FAKE_KEY.encode(), b"[]", b"{}", b'{"is_available":"yes","balance_infos":[]}',
                    b"x" * (configure.MAX_BALANCE_BYTES + 1)):
            self.response.read.return_value = raw
            self.assertEqual(self.run_main()[0], 1)
            self.assertFalse(self.path.exists())

    def test_insufficient_balance_is_still_valid_authentication_without_model_call(self):
        self.response.read.return_value = b'{"is_available":false,"balance_infos":[]}'
        self.assertEqual(self.run_main()[0], 0)
        self.opener.open.assert_called_once()
        settings = json.loads((self.path.parent / "automation.json").read_text())
        self.assertEqual(settings["credentials_status"], "balance_insufficient")
        self.assertFalse(settings["enabled"])

    def test_automation_settings_are_preserved_without_enabling_jobs(self):
        self.path.parent.mkdir()
        path = self.path.parent / "automation.json"
        settings = {"enabled": False, "provider": "deepseek", "automation_id": "automation",
                    "peak_minutes": 240, "off_peak_minutes": 60, "credentials_status": "invalid"}
        path.write_text(json.dumps(settings))
        self.assertEqual(self.run_main()[0], 0)
        settings["credentials_status"] = "verified"
        self.assertEqual(json.loads(path.read_text()), settings)

    def test_noninteractive_or_echo_fallback_never_reads_or_saves_key(self):
        result, _, prompt = self.run_main(interactive=False)
        self.assertEqual(result, 1)
        prompt.assert_not_called()
        self.opener.open.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch.object(configure.sys.stdin, "isatty", return_value=True), \
                patch.object(configure.getpass, "getpass", side_effect=configure.getpass.GetPassWarning()):
            self.assertEqual(configure.main(), 1)
        self.assertFalse(self.path.exists())
        self.opener.open.assert_not_called()

    def test_storage_failure_is_sanitized(self):
        with patch.object(configure.poc, "write_json", side_effect=OSError(FAKE_KEY)):
            self.assertEqual(self.run_main()[0], 1)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
