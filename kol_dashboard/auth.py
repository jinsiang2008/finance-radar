"""Passcode authentication with signed, expiring private-mode cookies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping


PASSCODE_HASH_ITERATIONS = 600_000
MAX_PASSCODE_BYTES = 256
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
DEFAULT_COOKIE_NAME = "kol_private_session"
DEFAULT_COOKIE_PATH = "/kol"
_HASH_NAME = "pbkdf2_sha256"
_HASH_RE = re.compile(
    r"^pbkdf2_sha256\$(\d{1,7})\$([A-Za-z0-9_-]+)\$([A-Za-z0-9_-]+)$"
)
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or not _TOKEN_PART_RE.fullmatch(value):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _passcode_bytes(passcode: Any) -> bytes | None:
    if not isinstance(passcode, str):
        return None
    encoded = passcode.encode("utf-8")
    if not encoded or len(encoded) > MAX_PASSCODE_BYTES:
        return None
    return encoded


def hash_passcode(
    passcode: str,
    *,
    iterations: int = PASSCODE_HASH_ITERATIONS,
    salt: bytes | None = None,
) -> str:
    """Create a self-contained PBKDF2-SHA256 passcode verifier."""
    encoded = _passcode_bytes(passcode)
    if encoded is None:
        raise ValueError("passcode must be 1-256 UTF-8 bytes")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 1_000
        or iterations > 2_000_000
    ):
        raise ValueError("iterations must be between 1000 and 2000000")
    clean_salt = secrets.token_bytes(16) if salt is None else salt
    if not isinstance(clean_salt, bytes) or not 16 <= len(clean_salt) <= 64:
        raise ValueError("salt must contain 16-64 bytes")
    digest = hashlib.pbkdf2_hmac(
        "sha256", encoded, clean_salt, iterations, dklen=32
    )
    return (
        f"{_HASH_NAME}${iterations}${_b64encode(clean_salt)}"
        f"${_b64encode(digest)}"
    )


def _parse_passcode_hash(encoded_hash: Any) -> tuple[int, bytes, bytes] | None:
    if not isinstance(encoded_hash, str) or len(encoded_hash) > 512:
        return None
    match = _HASH_RE.fullmatch(encoded_hash)
    if not match:
        return None
    try:
        iterations = int(match.group(1))
        salt = _b64decode(match.group(2))
        digest = _b64decode(match.group(3))
    except (ValueError, TypeError):
        return None
    if (
        not 1_000 <= iterations <= 2_000_000
        or not 16 <= len(salt) <= 64
        or len(digest) != 32
    ):
        return None
    return iterations, salt, digest


def verify_passcode(passcode: Any, encoded_hash: Any) -> bool:
    parsed = _parse_passcode_hash(encoded_hash)
    candidate = _passcode_bytes(passcode)
    if parsed is None or candidate is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac(
        "sha256", candidate, salt, iterations, dklen=32
    )
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class AuthConfig:
    passcode_hash: str | None
    session_secret: bytes | None
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    cookie_name: str = DEFAULT_COOKIE_NAME
    cookie_path: str = DEFAULT_COOKIE_PATH
    cookie_secure: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.passcode_hash and self.session_secret)


def _config_int(
    value: Any, default: int, *, minimum: int, maximum: int
) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer authentication setting") from exc
    if not minimum <= number <= maximum:
        raise ValueError("authentication setting is outside supported range")
    return number


def _config_bool(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid boolean authentication setting")


def load_config(environment: Mapping[str, str] | None = None) -> AuthConfig:
    env = os.environ if environment is None else environment
    passcode_hash = str(env.get("KOL_DASHBOARD_PASSCODE_HASH") or "").strip()
    secret_text = str(env.get("KOL_DASHBOARD_SESSION_SECRET") or "")
    if passcode_hash and _parse_passcode_hash(passcode_hash) is None:
        raise ValueError("KOL_DASHBOARD_PASSCODE_HASH is malformed")
    session_secret = secret_text.encode("utf-8") if secret_text else None
    if session_secret is not None and len(session_secret) < 32:
        raise ValueError(
            "KOL_DASHBOARD_SESSION_SECRET must contain at least 32 bytes"
        )
    cookie_name = str(
        env.get("KOL_DASHBOARD_COOKIE_NAME") or DEFAULT_COOKIE_NAME
    ).strip()
    if not _COOKIE_NAME_RE.fullmatch(cookie_name):
        raise ValueError("KOL_DASHBOARD_COOKIE_NAME is invalid")
    cookie_path = str(
        env.get("KOL_DASHBOARD_COOKIE_PATH") or DEFAULT_COOKIE_PATH
    ).strip()
    if (
        not cookie_path.startswith("/")
        or any(character in cookie_path for character in "\r\n;")
    ):
        raise ValueError("KOL_DASHBOARD_COOKIE_PATH is invalid")
    return AuthConfig(
        passcode_hash=passcode_hash or None,
        session_secret=session_secret,
        session_ttl_seconds=_config_int(
            env.get("KOL_DASHBOARD_SESSION_TTL_SECONDS"),
            DEFAULT_SESSION_TTL_SECONDS,
            minimum=300,
            maximum=7 * 24 * 60 * 60,
        ),
        cookie_name=cookie_name,
        cookie_path=cookie_path,
        cookie_secure=_config_bool(
            env.get("KOL_DASHBOARD_COOKIE_SECURE"), True
        ),
    )


class SessionSigner:
    """Issue and verify stateless HMAC-SHA256 session tokens."""

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("session secret must contain at least 32 bytes")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 300 <= ttl_seconds <= 7 * 24 * 60 * 60
        ):
            raise ValueError("session TTL is outside supported range")
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, *, now: int | float | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "v": 1,
            "iat": issued_at,
            "exp": issued_at + self._ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
        }
        encoded = _b64encode(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def verify(
        self, token: Any, *, now: int | float | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(token, str) or not 16 <= len(token) <= 2_048:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        encoded, provided_signature = parts
        try:
            expected_signature = _b64encode(
                hmac.new(
                    self._secret, encoded.encode("ascii"), hashlib.sha256
                ).digest()
            )
            if not hmac.compare_digest(provided_signature, expected_signature):
                return None
            payload = json.loads(_b64decode(encoded).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "iat",
            "exp",
            "nonce",
        }:
            return None
        version = payload.get("v")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        nonce = payload.get("nonce")
        if (
            version != 1
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or not isinstance(nonce, str)
            or not 8 <= len(nonce) <= 64
            or expires_at <= issued_at
            or expires_at - issued_at > self._ttl_seconds
        ):
            return None
        current = int(time.time() if now is None else now)
        if issued_at > current + 300 or current >= expires_at:
            return None
        return payload


class LoginRateLimiter:
    """Small in-memory fixed-window limiter for passcode failures."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds < 1:
            raise ValueError("rate limit settings must be positive")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _purge(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        cutoff = now - self._window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            return deque()
        return failures

    def allow(self, key: str) -> bool:
        clean_key = str(key or "unknown")[:256]
        with self._lock:
            failures = self._purge(clean_key, self._clock())
            return len(failures) < self._max_attempts

    def acquire(self, key: str) -> tuple[bool, int]:
        """Atomically reserve one authentication attempt for a client."""
        clean_key = str(key or "unknown")[:256]
        with self._lock:
            now = self._clock()
            failures = self._purge(clean_key, now)
            if len(failures) >= self._max_attempts:
                retry_after = max(
                    1,
                    math.ceil(
                        self._window_seconds - (now - failures[0])
                    ),
                )
                return False, retry_after
            self._failures[clean_key].append(now)
            return True, 0

    def record_failure(self, key: str) -> None:
        clean_key = str(key or "unknown")[:256]
        with self._lock:
            now = self._clock()
            self._purge(clean_key, now)
            self._failures[clean_key].append(now)

    def retry_after(self, key: str) -> int:
        clean_key = str(key or "unknown")[:256]
        with self._lock:
            now = self._clock()
            failures = self._purge(clean_key, now)
            if len(failures) < self._max_attempts:
                return 0
            return max(
                1,
                math.ceil(
                    self._window_seconds - (now - failures[0])
                ),
            )

    def reset(self, key: str) -> None:
        clean_key = str(key or "unknown")[:256]
        with self._lock:
            self._failures.pop(clean_key, None)
