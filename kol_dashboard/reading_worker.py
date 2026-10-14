"""Single server-side reader: peak-aware discovery and serial evidence summaries."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time

from research.podcast_poc import schedule_policy

try:
    from kol_dashboard import llm_enrichment, reading_service
except ModuleNotFoundError:  # Flat production release.
    import llm_enrichment
    import reading_service


class SharedDeepSeekClient:
    """Adapts the reading prompts to the existing server's fixed HTTPS boundary."""
    def __init__(self, config, *, clock=None):
        if config is None:
            raise reading_service.ReadingUnavailable("Reading worker requires the existing DeepSeek configuration")
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls = 0
        self.unknown = 0
        self.usage = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}

    def _usage(self, usage):
        values = usage.as_dict() if usage is not None else {}
        complete = True
        for key in self.usage:
            value = values.get(key)
            if type(value) is int and value >= 0:
                self.usage[key] += value
            else:
                complete = False
        if complete:
            self.unknown -= 1

    @staticmethod
    def _object(value):
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def generate(self, messages, max_tokens):
        # Match the shared provider's local request bounds before counting an
        # attempted API call. A rejected local prompt cannot have incurred usage.
        system_prompt = messages[0]["content"]
        user_prompt = json.dumps(messages[1:], ensure_ascii=False)
        if (not isinstance(system_prompt, str) or not system_prompt.strip()
                or len(system_prompt) > 32_000 or not user_prompt.strip()
                or len(user_prompt) > 32_000):
            raise llm_enrichment.EnrichmentError("invalid_request", retry_after_seconds=None)
        # Each chunk, retry and merge passes this boundary independently. An
        # explicitly selected item may run in peak; other jobs recheck every call.
        schedule_policy.require_summary_window(at=self.clock())
        self.calls += 1
        self.unknown += 1
        try:
            result = llm_enrichment.complete_json_with_usage(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                config=replace(self.config, max_output_tokens=max_tokens),
                validator=self._object, user_id="kol-reading-summary")
        except llm_enrichment.EnrichmentError as exc:
            self._usage(exc.usage)
            raise
        self._usage(result.usage)
        return json.dumps(result.result, ensure_ascii=False)

    def stats(self):
        return {"paid_api_calls": self.calls, "usage_unreported_calls": self.unknown, "usage": dict(self.usage)}


class Worker:
    def __init__(self, data_dir, *, config, enabled=True, clock=None):
        if config is None or not llm_enrichment.is_supported_model(config.model):
            raise reading_service.ReadingUnavailable("Existing DeepSeek configuration is required")
        self.config, self.enabled = config, bool(enabled)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.runtime = reading_service.ReadingRuntime(data_dir,
            client_factory=lambda: SharedDeepSeekClient(config, clock=self.clock), clock=self.clock)
        self.stop = threading.Event()
        self.last_check_monotonic = None

    def heartbeat(self, state="running"):
        self.runtime.poc.write_json(self.runtime.data / "runtime" / "worker-heartbeat.json",
            {"state": state, "updated_at": self.runtime.poc.now(), "pid": os.getpid(),
             "provider": "deepseek", "model": self.config.model})

    def tick(self, *, monotonic=None):
        moment = time.monotonic() if monotonic is None else monotonic
        blocked = bool(self.runtime.engine.runtime_snapshot().get("provider_blocked"))
        allow_automatic = self.enabled and not blocked
        if self.last_check_monotonic is None or moment - self.last_check_monotonic >= 30:
            self.last_check_monotonic = moment
            state = self.runtime.status()
            if allow_automatic and state["due"]:
                self.runtime.engine.enqueue_refresh()
        return self.runtime.engine.run_one(allow_automatic=allow_automatic)

    def run(self):
        with (self.runtime.data / "runtime" / "reading-scheduler.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise reading_service.ReadingUnavailable("A reading scheduler is already active") from None
            self.runtime.publish_configuration(model=self.config.model, enabled=self.enabled)
            self.heartbeat()

            def pulse():
                while not self.stop.wait(10):
                    self.heartbeat()

            heartbeat_thread = threading.Thread(target=pulse, name="reading-heartbeat", daemon=True)
            heartbeat_thread.start()
            try:
                while not self.stop.is_set():
                    try:
                        if self.tick():
                            continue
                    except Exception as exc:
                        # Never print provider messages, headers or source URLs.
                        print("Reading worker check failed: " + type(exc).__name__, flush=True)
                    self.stop.wait(2)
            finally:
                self.stop.set()
                heartbeat_thread.join(timeout=2)
                self.heartbeat("stopped")
                fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    directory = os.environ.get("KOL_READING_DATA_DIR", "").strip()
    if not directory or not Path(directory).is_absolute():
        raise SystemExit("KOL_READING_DATA_DIR must name the persistent reading directory")
    config = llm_enrichment.load_config()
    if config is None:
        raise SystemExit("The reading worker requires the existing DeepSeek environment configuration")
    worker = Worker(directory, config=config, enabled=os.environ.get("KOL_READING_ENABLED", "1") == "1")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop.set())
    worker.run()


if __name__ == "__main__":
    main()
