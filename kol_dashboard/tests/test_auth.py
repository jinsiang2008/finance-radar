from __future__ import annotations

import base64
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auth  # noqa: E402


class PasscodeTests(unittest.TestCase):
    def test_hash_round_trip_is_constant_format_and_wrong_value_fails(self) -> None:
        encoded = auth.hash_passcode(
            "correct horse battery staple",
            iterations=1_000,
            salt=b"\x01" * 16,
        )

        self.assertTrue(
            auth.verify_passcode("correct horse battery staple", encoded)
        )
        self.assertFalse(auth.verify_passcode("wrong", encoded))
        self.assertEqual(encoded.split("$", 1)[0], "pbkdf2_sha256")

    def test_malformed_hash_and_oversized_passcode_fail_closed(self) -> None:
        for encoded in (
            "",
            "sha256$1000$bad$bad",
            "pbkdf2_sha256$0$bad$bad",
            "pbkdf2_sha256$1000$%%%$%%%",
        ):
            self.assertFalse(auth.verify_passcode("value", encoded))
        self.assertFalse(
            auth.verify_passcode(
                "x" * 300,
                auth.hash_passcode("valid", iterations=1_000),
            )
        )


class SessionSignerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = auth.SessionSigner(
            b"s" * 32,
            ttl_seconds=600,
        )

    def test_signed_session_round_trips_and_expires(self) -> None:
        token = self.signer.issue(now=1_000)

        claims = self.signer.verify(token, now=1_001)

        self.assertIsNotNone(claims)
        self.assertEqual(claims["iat"], 1_000)
        self.assertEqual(claims["exp"], 1_600)
        self.assertIsNone(self.signer.verify(token, now=1_601))

    def test_tampered_or_future_session_fails_closed(self) -> None:
        token = self.signer.issue(now=1_000)
        payload, signature = token.split(".")
        tampered_payload = base64.urlsafe_b64encode(
            b'{"v":1,"iat":1000,"exp":999999999,"nonce":"x"}'
        ).decode("ascii").rstrip("=")

        self.assertIsNone(
            self.signer.verify(f"{tampered_payload}.{signature}", now=1_001)
        )
        future = self.signer.issue(now=2_000)
        self.assertIsNone(self.signer.verify(future, now=1_000))
        self.assertIsNone(self.signer.verify("not-a-token", now=1_000))
        self.assertIsNone(self.signer.verify("令牌.签名", now=1_000))


class LoginRateLimiterTests(unittest.TestCase):
    def test_failures_are_limited_then_expire_or_reset(self) -> None:
        now = [100.0]
        limiter = auth.LoginRateLimiter(
            max_attempts=3,
            window_seconds=60,
            clock=lambda: now[0],
        )

        self.assertTrue(limiter.allow("client"))
        limiter.record_failure("client")
        limiter.record_failure("client")
        limiter.record_failure("client")
        self.assertFalse(limiter.allow("client"))
        self.assertGreater(limiter.retry_after("client"), 0)

        now[0] = 161.0
        self.assertTrue(limiter.allow("client"))
        limiter.record_failure("client")
        limiter.reset("client")
        self.assertTrue(limiter.allow("client"))

    def test_attempt_reservation_is_atomic_under_concurrency(self) -> None:
        limiter = auth.LoginRateLimiter(
            max_attempts=5,
            window_seconds=60,
            clock=lambda: 100.0,
        )

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(
                pool.map(lambda _: limiter.acquire("client"), range(20))
            )

        self.assertEqual(sum(1 for allowed, _ in results if allowed), 5)
        self.assertTrue(
            all(retry_after > 0 for allowed, retry_after in results if not allowed)
        )


class ConfigTests(unittest.TestCase):
    def test_environment_config_is_fail_closed_and_validated(self) -> None:
        missing = auth.load_config({})
        self.assertFalse(missing.configured)
        self.assertEqual(missing.cookie_path, "/kol")
        self.assertTrue(missing.cookie_secure)

        passcode_hash = auth.hash_passcode("pass", iterations=1_000)
        with self.assertRaises(ValueError):
            auth.load_config(
                {
                    "KOL_DASHBOARD_PASSCODE_HASH": passcode_hash,
                    "KOL_DASHBOARD_SESSION_SECRET": "short",
                }
            )

        config = auth.load_config(
            {
                "KOL_DASHBOARD_PASSCODE_HASH": passcode_hash,
                "KOL_DASHBOARD_SESSION_SECRET": "x" * 32,
                "KOL_DASHBOARD_SESSION_TTL_SECONDS": "3600",
                "KOL_DASHBOARD_COOKIE_PATH": "/kol",
                "KOL_DASHBOARD_COOKIE_SECURE": "true",
            }
        )
        self.assertTrue(config.configured)
        self.assertEqual(config.session_ttl_seconds, 3600)
        self.assertEqual(config.cookie_path, "/kol")
        self.assertTrue(config.cookie_secure)


if __name__ == "__main__":
    unittest.main()
