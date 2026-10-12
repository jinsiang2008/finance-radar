"""Bounded DeepSeek JSON inference for the local reading POC.

The API key is read from an explicitly supplied local file, or the process
environment when that file is absent. No file is read by default. This module never logs
requests, provider error bodies, headers, or credentials. Callers own any
format retry and persist only the returned text and aggregate statistics.

API contract checked 2026-10-08:
https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
https://api-docs.deepseek.com/zh-cn/guides/json_mode/
"""
from __future__ import annotations

from http.client import HTTPException
import json
import math
import os
import stat
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
SUPPORTED_MODELS = frozenset({"deepseek-v4-flash", "deepseek-flash", "deepseek-v4-pro"})
MAX_RESPONSE_BYTES = 4_000_000
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
MAX_CREDENTIAL_BYTES = 16_384


class DeepSeekError(RuntimeError):
    """A public diagnostic containing no provider-controlled error details."""

    def __init__(self, code: str, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        message = {
            "missing_key": "DEEPSEEK_API_KEY is not configured",
            "invalid_key": "DEEPSEEK_API_KEY has an invalid format",
            "credentials_file": "Local DeepSeek credential file is invalid or unreadable",
            "credentials_permissions": "Local DeepSeek credential file requires private owner permissions",
            "authentication": "DeepSeek authentication failed",
            "balance": "DeepSeek account balance is insufficient",
            "rate_limit": "DeepSeek rate limit reached",
            "redirect": "DeepSeek redirect was blocked",
            "http_error": "DeepSeek request failed",
            "timeout": "DeepSeek request timed out",
            "network": "DeepSeek network request failed",
            "response_too_large": "DeepSeek response exceeded the size limit",
            "invalid_response": "DeepSeek returned an invalid response",
            "incomplete_output": "DeepSeek output was not completed",
            "empty_output": "DeepSeek returned no answer text",
        }.get(code, "DeepSeek request failed")
        if http_status is not None:
            message += f" (HTTP {http_status})"
        super().__init__(message)


def config() -> dict[str, str]:
    """Return public provider/model metadata; never expose the API key."""
    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    return {"provider": "deepseek", "model": model if model in SUPPORTED_MODELS else DEFAULT_MODEL}


def _timeout() -> float:
    try:
        value = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "45"))
    except ValueError:
        value = 45.0
    if not math.isfinite(value):
        value = 45.0
    return min(120.0, max(10.0, value))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header to a Location supplied by a
        # response, including same-host redirects. The endpoint is fixed.
        return None


def _http_error(status: int) -> DeepSeekError:
    code = {401: "authentication", 402: "balance", 429: "rate_limit"}.get(status, "http_error")
    if 300 <= status < 400:
        code = "redirect"
    return DeepSeekError(code, status)


def _validate_key(value) -> str:
    if not isinstance(value, str):
        raise DeepSeekError("invalid_key")
    key = value.strip()
    if not key:
        raise DeepSeekError("missing_key")
    if len(key) > 8192 or any(not 33 <= ord(char) <= 126 for char in key):
        raise DeepSeekError("invalid_key")
    return key


def _load_key(credentials_path=None) -> str:
    """A present but invalid local file fails closed instead of using another key."""
    if credentials_path is not None:
        try:
            fd = os.open(credentials_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError):
            raise DeepSeekError("credentials_file") from None
        else:
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise DeepSeekError("credentials_permissions")
                with os.fdopen(fd, "rb") as stream:
                    fd = None  # The stream now owns and closes this descriptor.
                    raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
                if len(raw) > MAX_CREDENTIAL_BYTES:
                    raise DeepSeekError("credentials_file")
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {"api_key"}:
                    raise DeepSeekError("credentials_file")
                return _validate_key(value["api_key"])
            except (OSError, ValueError, UnicodeError):
                raise DeepSeekError("credentials_file") from None
            finally:
                if fd is not None:
                    os.close(fd)
    return _validate_key(os.environ.get("DEEPSEEK_API_KEY", ""))


class Client:
    """Single-threaded client. Every generate() makes at most one request."""

    def __init__(self, credentials_path=None):
        self._api_key = _load_key(credentials_path)
        self.model = config()["model"]
        self.timeout_seconds = _timeout()
        # Keep urllib's default system/environment proxy handling. Replacing
        # only the redirect handler still uses verified HTTPS for the endpoint.
        self._opener = build_opener(_NoRedirect())
        self._attempts = 0
        self._usage = {field: 0 for field in USAGE_FIELDS}
        self._usage_unreported_calls = 0

    def stats(self) -> dict:
        """Attempts are not confirmed charges; token totals are reported usage.

        A timeout or missing usage can still incur charges. The extra unknown
        counter prevents interpreting an absent token report as a free call.
        Returned dictionaries are detached from internal mutable counters.
        """
        return {"paid_api_calls": self._attempts, "usage": dict(self._usage),
                "usage_unreported_calls": self._usage_unreported_calls}

    def _record_usage(self, value) -> None:
        if not isinstance(value, dict):
            return
        complete = True
        for field in USAGE_FIELDS:
            count = value.get(field)
            if type(count) is int and 0 <= count <= 10_000_000:
                self._usage[field] += count
            else:
                complete = False
        if complete:
            self._usage_unreported_calls -= 1

    def generate(self, messages, max_tokens: int) -> str:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 393_216:
            raise ValueError("max_tokens must be an integer from 1 to 393216")
        if not isinstance(messages, (list, tuple)) or not messages:
            raise ValueError("messages must contain text messages")
        prepared = []
        for message in messages:
            if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                    or message["role"] not in {"system", "user", "assistant"}
                    or not isinstance(message.get("content"), str)):
                raise ValueError("messages must contain supported roles and text content")
            prepared.append({"role": message["role"], "content": message["content"]})
        if not any(m["role"] in {"system", "user"} and "json" in m["content"].lower() for m in prepared):
            raise ValueError("JSON mode requires a JSON instruction in a system or user message")
        payload = {"model": self.model, "messages": prepared, "stream": False,
                   "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                   "max_tokens": max_tokens, "temperature": 0}
        request = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key,
                                   "Content-Type": "application/json", "Accept": "application/json"},
                          method="POST")
        self._attempts += 1
        self._usage_unreported_calls += 1
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                if status != 200:
                    raise _http_error(status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Do not read or include its body, reason, URL, or response headers.
            status = int(exc.code)
            exc.close()
            raise _http_error(status) from None
        except TimeoutError:
            raise DeepSeekError("timeout") from None
        except URLError as exc:
            raise DeepSeekError("timeout" if isinstance(exc.reason, TimeoutError) else "network") from None
        except (OSError, HTTPException, ValueError):
            raise DeepSeekError("network") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeepSeekError("response_too_large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DeepSeekError("invalid_response") from None
        if not isinstance(result, dict):
            raise DeepSeekError("invalid_response")
        self._record_usage(result.get("usage"))
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise DeepSeekError("invalid_response")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise DeepSeekError("incomplete_output")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise DeepSeekError("invalid_response")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("empty_output")
        # Schema/evidence validation and any bounded format retry belong to the
        # caller. Never expose reasoning_content or the full response envelope.
        return content
