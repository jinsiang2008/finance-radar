"""Authenticated KOL reader domain API; inference belongs to reading_worker only.

Importing this module is side-effect free. The deployment must explicitly set
KOL_READING_DATA_DIR; there is no fallback to a developer's POC data or keys.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from research.podcast_poc import poc as poc_template, reading_jobs, schedule_policy


SUPPORTED_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
_runtime = None
_runtime_lock = threading.Lock()


class ReadingUnavailable(RuntimeError):
    pass


def _web_inference_disabled():
    raise ReadingUnavailable("Only the background worker may run reading inference")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadingRuntime:
    def __init__(self, data_dir, *, client_factory=None, clock=None):
        self.data = Path(data_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not self.data.is_absolute():
            raise ReadingUnavailable("KOL_READING_DATA_DIR must be an absolute directory")
        # Separate function globals preserve local POC callers and isolated tests.
        name = "research.podcast_poc._kol_reading_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, poc_template.__file__)
        self.poc = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.poc
        spec.loader.exec_module(self.poc)
        self.poc.configure_runtime(self.data, settings_provider=self.settings,
                                   client_factory=client_factory or _web_inference_disabled,
                                   fetcher=self.fetch_feed)
        self.poc.ALLOW_PODCAST_JOBS = True
        self.poc.refresh_podcasts = self.refresh_podcasts
        self.engine = reading_jobs.Engine(self.poc)
        self.poc.reading_schedule = self.schedule_status
        self.allowed_feeds = {value[2] for value in self.poc.FEEDS.values()}
        self.allowed_feeds.update(source["feed_url"] for source in self.poc.wechat_module().source_config())

    def settings(self):
        fallback = {"enabled": False, "provider": "unconfigured", "model": "unconfigured",
                    "peak_minutes": 240, "off_peak_minutes": 60, "off_peak_only": True}
        try:
            value = json.loads((self.data / "runtime" / "production-config.json").read_text())
        except (OSError, ValueError):
            return fallback
        if (not isinstance(value, dict) or value.get("provider") != "deepseek"
                or not isinstance(value.get("model"), str) or value.get("model") not in SUPPORTED_MODELS
                or value.get("configured") is not True):
            return fallback
        return {"enabled": value.get("enabled") is True, "provider": "deepseek", "model": value["model"],
                "peak_minutes": 240, "off_peak_minutes": 60, "off_peak_only": True}

    def publish_configuration(self, *, model, enabled):
        if model not in SUPPORTED_MODELS:
            raise ReadingUnavailable("Unsupported reading model")
        self.poc.write_json(self.data / "runtime" / "production-config.json",
                            {"provider": "deepseek", "model": model, "configured": True,
                             "enabled": bool(enabled), "peak_minutes": 240, "off_peak_minutes": 60,
                             "off_peak_only": True,
                             "updated_at": self.poc.now()})

    def fetch_feed(self, url, *, limit):
        # The worker discovers metadata from fixed configuration only. It never
        # fetches a supplied article/audio URL or follows a feed's arbitrary redirect.
        if url not in self.allowed_feeds or not url.startswith("https://"):
            raise ValueError("Only configured HTTPS reading feeds may be fetched")
        schedule_policy.require_off_peak(at=self.clock())
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = Request(url, headers={"User-Agent": "KOLReading/1", "Accept": "application/rss+xml, application/xml"})
        with opener.open(request, timeout=25) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("Reading feed exceeds the size limit")
        return body

    def refresh_podcasts(self):
        schedule_policy.require_off_peak(at=self.clock())
        results = self.poc.discover(list(self.poc.FEEDS), limit=3)
        self.engine._runtime_update(podcast_last_check_at=self.poc.now(),
                                    podcast_successful_sources=sum(bool(row.get("success")) for row in results),
                                    podcast_source_count=len(results))

    def schedule_status(self):
        runtime = self.engine.runtime_snapshot()
        return schedule_policy.snapshot(runtime.get("last_check_at"), at=self.clock(),
                                        peak_minutes=240, off_peak_minutes=60, off_peak_only=True)

    def status(self):
        settings, runtime = self.settings(), self.engine.runtime_snapshot()
        state = self.schedule_status()
        state.update(runtime)
        state.update(settings)
        if state.get("provider_blocked"):
            state["automatic_paused_reason"] = reading_jobs.PROVIDER_ERRORS.get(state["provider_blocked"], reading_jobs.PUBLIC_FAILURE)
        state.update(worker_alive=False, scheduler="systemd_worker", jobs=self.engine.snapshot(),
                     transcript_policy="existing_transcripts_only", automatic_audio_transcription=False,
                     deepseek_audio_transcription=False, paid_call_boundary="before_each_request")
        try:
            heartbeat = json.loads((self.data / "runtime" / "worker-heartbeat.json").read_text())
            stamp = datetime.fromisoformat(heartbeat["updated_at"])
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            state["worker_alive"] = heartbeat.get("state") == "running" and 0 <= age <= 120
            state["worker_last_seen_at"] = heartbeat["updated_at"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not state["enabled"]:
            state["next_check_at"] = None
        return state

    def wechat_payload(self):
        result = self.poc.wechat_module().payload()
        result.update(mode="server_deepseek", automation=self.status())
        for source in result["sources"]:
            if source.get("error"):
                source["error"] = "本次来源读取异常，已保留已有内容。"
        return result

    def podcast_payload(self):
        result = self.poc.payload()
        result.update(mode="server_deepseek", automation=self.status())
        return result

    def enqueue_summary(self, content_id):
        if self.settings()["provider"] != "deepseek":
            raise ReadingUnavailable("阅读后台尚未配置完成，请稍后重试。")
        return self.engine.enqueue(content_id, manual=True)

    def enqueue_check(self, manual=True):
        if self.settings()["provider"] != "deepseek":
            raise ReadingUnavailable("阅读后台尚未配置完成，请稍后重试。")
        return self.engine.enqueue_refresh(manual=manual)

    def media_path(self, content_id):
        if not isinstance(content_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", content_id):
            return None
        root = (self.data / "media").resolve()
        path = root / (content_id + ".wav")
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            return None
        return path


def get_runtime():
    global _runtime
    directory = os.environ.get("KOL_READING_DATA_DIR", "").strip()
    if not directory:
        raise ReadingUnavailable("KOL_READING_DATA_DIR is not configured")
    with _runtime_lock:
        if _runtime is None or _runtime.data != Path(directory):
            _runtime = ReadingRuntime(directory)
        return _runtime


def wechat_payload():
    return get_runtime().wechat_payload()


def podcast_payload():
    return get_runtime().podcast_payload()


def runtime_status():
    return get_runtime().status()


def enqueue_summary(content_id):
    return get_runtime().enqueue_summary(content_id)


def enqueue_check(manual=True):
    return get_runtime().enqueue_check(manual)


def media_path(content_id):
    return get_runtime().media_path(content_id)
