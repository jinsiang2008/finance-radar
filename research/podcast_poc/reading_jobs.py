"""Durable, single-executor jobs for the local article reader.

The SQLite transaction deduplicates requests; the separate process lock covers
both RSS writes and inference. No network or model work occurs during enqueue.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import traceback
import uuid

try:
    from research.podcast_poc import schedule_policy
except ModuleNotFoundError:
    import schedule_policy


PUBLIC_FAILURE = "处理失败，可以稍后重试。详细诊断已保存在本机。"
INTERRUPTED_FAILURE = "任务多次中断，已停止自动重试；可以手动重试。"
REFRESH_FAILURE = "本次未能取得订阅源，已保留已有文章。"
MAX_INTERRUPTION_RECOVERIES = 3
PRIVATE_RUNTIME_KEYS = {"automatic_source_baselines", "automatic_baselines_initialized", "tracked_article_bodies"}
PROVIDER_ERRORS = {
    "authentication": "DeepSeek 认证失败，请检查本机配置的密钥后重试。",
    "balance": "DeepSeek 账户余额不足，请补充余额后重试。",
    "rate_limit": "DeepSeek 请求受到限流，请稍后重试。",
    "timeout": "DeepSeek 连接超时，请稍后重试。",
    "network": "暂时无法连接 DeepSeek，请检查网络后重试。",
    "missing_key": "本机尚未配置 DeepSeek 密钥，配置后可以重试。",
    "invalid_key": "本机 DeepSeek 密钥格式不正确，请检查配置。",
    "redirect": "DeepSeek 请求发生异常跳转，已停止本次处理。",
    "incomplete_output": "DeepSeek 返回的内容不完整，请重试。",
    "invalid_response": "DeepSeek 返回的内容格式无效，请重试。",
    "empty_output": "DeepSeek 未返回摘要内容，请重试。",
    "provider_unavailable": "DeepSeek 服务暂时不可用，请稍后重试。",
    "provider_error": "DeepSeek 服务返回错误，请稍后重试。",
    "invalid_request": "摘要请求暂时无法处理，请检查服务配置。",
    "invalid_output": "DeepSeek 返回的摘要格式不完整，请重试。",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, poc_module):
        self.poc = poc_module
        self.runtime = Path(poc_module.DATA) / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.database = self.runtime / "reading-jobs.sqlite3"
        self.lock_path = self.runtime / "reading-worker.lock"
        with self._connection() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL CHECK(type IN ('summary', 'refresh')),
                    article_id TEXT,
                    input_version TEXT,
                    state TEXT NOT NULL,
                    manual INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    interruptions INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    reused INTEGER NOT NULL DEFAULT 0,
                    not_before TEXT,
                    deferred_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS summary_input
                    ON jobs(article_id, input_version) WHERE type = 'summary';
                CREATE UNIQUE INDEX IF NOT EXISTS active_refresh
                    ON jobs(type) WHERE type = 'refresh' AND state IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                for column in ("not_before", "deferred_reason"):
                    if column not in columns:
                        connection.execute("ALTER TABLE jobs ADD COLUMN " + column + " TEXT")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _schedule(self):
        policy = getattr(self.poc, "reading_schedule", None)
        return policy() if callable(policy) else {"execution_allowed": True}

    @staticmethod
    def _deferral_values(policy):
        if policy.get("execution_allowed", True):
            return None, None
        value = datetime.fromisoformat(policy["next_run_at"]).astimezone(timezone.utc).isoformat()
        return value, "off_peak_only"

    def _apply_queue_schedule(self, connection, job_id, policy):
        not_before, reason = self._deferral_values(policy)
        connection.execute("UPDATE jobs SET not_before=?,deferred_reason=? WHERE id=? AND state='queued'",
                           (not_before, reason, job_id))

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _public(row):
        result = dict(row)
        result["manual"] = bool(result["manual"])
        result["reused"] = bool(result["reused"])
        result.pop("interruptions", None)
        return result

    @staticmethod
    def _has_body(article):
        segments = article.get("segments")
        coverage = article.get("coverage")
        return (
            article.get("content_kind") == "wechat_article"
            and article.get("source_kind") == "external_rss_body"
            and isinstance(coverage, dict) and coverage.get("scope") == "source_text"
            and isinstance(segments, list) and bool(segments)
            and all(isinstance(segment, dict) and isinstance(segment.get("text"), str)
                    and isinstance(segment.get("id"), str) and segment["id"] for segment in segments)
            and sum(len(re.sub(r"\s+", "", segment["text"])) for segment in segments) >= 200
        )

    def _has_input(self, article):
        if article.get("content_kind") == "wechat_article" or article.get("id", "").startswith("wx-"):
            return self._has_body(article)
        if not getattr(self.poc, "ALLOW_PODCAST_JOBS", False):
            return False
        segments, coverage = article.get("segments"), article.get("coverage")
        accepted_scope = (isinstance(coverage, dict) and (coverage.get("scope") in {"full", "sample"}
                          or (coverage.get("scope") == "partial" and article.get("source_kind") == "third_party_transcript")))
        return (accepted_scope
                and article.get("source_kind") in {"provided_transcript", "official_transcript", "official_edited_transcript", "third_party_transcript", "local_asr_sample"}
                and isinstance(segments, list) and bool(segments)
                and all(isinstance(segment, dict) and isinstance(segment.get("id"), str) and segment["id"]
                        and isinstance(segment.get("text"), str) and segment["text"].strip() for segment in segments))

    def _summary_ready(self, article):
        if article.get("status") != "summarized" or not self._has_input(article):
            return False
        summary = article.get("summary")
        if not isinstance(summary, dict) or not isinstance(summary.get("overview"), str) or not summary["overview"].strip():
            return False
        points = summary.get("points")
        ids = {segment["id"] for segment in article["segments"]}
        return isinstance(points, list) and bool(points) and all(
            isinstance(point, dict) and isinstance(point.get("text"), str) and bool(point["text"].strip())
            and isinstance(point.get("segment_ids"), list) and bool(point["segment_ids"])
            and all(isinstance(ref, str) and ref in ids for ref in point["segment_ids"])
            for point in points
        )

    def _version(self, article):
        signature = getattr(self.poc, "summary_signature", None)
        if signature is not None:
            return signature(article)
        identity = [article["segments"], self.poc.MODEL, "wechat-source-evidence-v1",
                    article.get("body_hash"), article.get("title"), article.get("account")]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def enqueue(self, article_id, manual=True):
        """Queue a validated body once; only an explicit manual request retries failure."""
        pattern = r"[a-zA-Z0-9_-]{1,100}" if getattr(self.poc, "ALLOW_PODCAST_JOBS", False) else r"wx-[a-zA-Z0-9_-]{1,97}"
        if not isinstance(article_id, str) or not re.fullmatch(pattern, article_id):
            raise ValueError("仅支持已有的文章或节目。")
        article = self.poc.load_episode(article_id)
        if article.get("id") != article_id:
            raise ValueError("文章记录与请求不一致。")
        if not self._has_input(article):
            raise ValueError("尚未取得可用正文，不能生成摘要。")
        version, ready, moment = self._version(article), self._summary_ready(article), _now()
        policy = self._schedule()
        with self._transaction() as connection:
            # An old queued input must never overwrite a newer body or reviewed result.
            connection.execute("""UPDATE jobs SET state='superseded', updated_at=?, completed_at=?,not_before=NULL,deferred_reason=NULL
                WHERE type='summary' AND article_id=? AND input_version<>? AND state='queued'""",
                (moment, moment, article_id, version))
            row = connection.execute("SELECT * FROM jobs WHERE type='summary' AND article_id=? AND input_version=?",
                                     (article_id, version)).fetchone()
            if row is None:
                job_id = uuid.uuid4().hex
                connection.execute("""INSERT INTO jobs
                    (id,type,article_id,input_version,state,manual,created_at,updated_at,completed_at,reused)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, "summary", article_id, version, "completed" if ready else "queued", int(manual),
                     moment, moment, moment if ready else None, int(ready)))
            else:
                job_id = row["id"]
                if ready and row["state"] != "running":
                    connection.execute("""UPDATE jobs SET state='completed', error=NULL, reused=1,not_before=NULL,deferred_reason=NULL,
                        completed_at=?, updated_at=? WHERE id=?""", (moment, moment, job_id))
                elif not ready and (row["state"] in {"completed", "superseded"} or (manual and row["state"] == "failed")):
                    connection.execute("""UPDATE jobs SET state='queued', error=NULL, reused=0, manual=?,
                        interruptions=0, started_at=NULL, completed_at=NULL, updated_at=? WHERE id=?""",
                        (int(manual), moment, job_id))
                elif manual and row["state"] == "queued" and not row["manual"]:
                    connection.execute("UPDATE jobs SET manual=1, updated_at=? WHERE id=?", (moment, job_id))
            # Remember bodies explicitly selected for reading. A later edit of an
            # old article that was read manually is eligible, not the entire archive.
            tracked_row = connection.execute("SELECT value FROM runtime WHERE key='tracked_article_bodies'").fetchone()
            tracked = json.loads(tracked_row["value"]) if tracked_row else {}
            tracked[article_id] = article.get("body_hash")
            connection.execute("INSERT OR REPLACE INTO runtime (key,value) VALUES ('tracked_article_bodies',?)",
                               (json.dumps(tracked),))
            self._apply_queue_schedule(connection, job_id, policy)
            return self._public(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue_refresh(self, manual=False):
        """Deduplicate refreshes while one is queued or running."""
        policy = self._schedule()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE type='refresh' AND state IN ('queued','running')").fetchone()
            if row:
                if manual and row["state"] == "queued" and not row["manual"]:
                    connection.execute("UPDATE jobs SET manual=1, updated_at=? WHERE id=?", (_now(), row["id"]))
                self._apply_queue_schedule(connection, row["id"], policy)
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                return self._public(row)
            job_id, moment = uuid.uuid4().hex, _now()
            connection.execute("INSERT INTO jobs (id,type,state,manual,created_at,updated_at) VALUES (?, 'refresh', 'queued', ?, ?, ?)",
                               (job_id, int(manual), moment, moment))
            self._apply_queue_schedule(connection, job_id, policy)
            return self._public(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def snapshot(self):
        with self._connection() as connection:
            rows = connection.execute("""SELECT * FROM jobs ORDER BY
                CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                updated_at DESC LIMIT 250""").fetchall()
            return [self._public(row) for row in rows]

    def runtime_snapshot(self):
        with self._connection() as connection:
            result = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT * FROM runtime")
                      if row["key"] not in PRIVATE_RUNTIME_KEYS}
            result["counts"] = {row["state"]: row["total"] for row in connection.execute(
                "SELECT state, COUNT(*) AS total FROM jobs GROUP BY state")}
        return result

    def _runtime_value(self, key, default=None):
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def _runtime_update(self, **values):
        with self._transaction() as connection:
            connection.executemany("INSERT OR REPLACE INTO runtime (key,value) VALUES (?,?)",
                                   [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()])

    def _finish(self, job_id, state, *, error=None, reused=False):
        moment = _now()
        with self._transaction() as connection:
            connection.execute("UPDATE jobs SET state=?, error=?, reused=?, updated_at=?, completed_at=?,not_before=NULL,deferred_reason=NULL WHERE id=?",
                               (state, error, int(reused), moment, moment, job_id))

    def _defer(self, job, exception):
        with self._transaction() as connection:
            connection.execute("""UPDATE jobs SET state='queued',error=NULL,not_before=?,deferred_reason='off_peak_only',
                started_at=NULL,completed_at=NULL,updated_at=? WHERE id=?""", (exception.not_before, _now(), job["id"]))
        if job["type"] == "summary":
            article = self.poc.load_episode(job["article_id"])
            if self._version(article) == job["input_version"] and not self._summary_ready(article):
                article["status"] = "body_ready" if article.get("content_kind") == "wechat_article" else "transcribed"
                article.pop("error", None)
                self.poc.save_episode(article)
        else:
            # A partial refresh must be eligible as soon as the next window opens.
            self._runtime_update(last_refresh_state="queued", last_error=None)

    def _article_failure(self, job, message):
        try:
            article = self.poc.load_episode(job["article_id"])
            if self._version(article) == job["input_version"] and not self._summary_ready(article):
                article.update(status="failed", error=message)
                self.poc.save_episode(article)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _recover(self):
        """Called only after owning the global worker lock, never during enqueue."""
        stopped = []
        moment = _now()
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
            for row in rows:
                if row["interruptions"] >= MAX_INTERRUPTION_RECOVERIES:
                    connection.execute("UPDATE jobs SET state='failed', error=?, updated_at=?, completed_at=? WHERE id=?",
                                       (INTERRUPTED_FAILURE, moment, moment, row["id"]))
                    stopped.append(dict(row))
                else:
                    connection.execute("""UPDATE jobs SET state='queued', interruptions=interruptions+1,
                        started_at=NULL, updated_at=? WHERE id=?""", (moment, row["id"]))
        for job in stopped:
            if job["type"] == "summary":
                self._article_failure(job, INTERRUPTED_FAILURE)

    def _diagnostic(self, job, exc):
        # Do not retain str(exc): HTTP/provider exceptions may contain credentials.
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)]
        entry = {"at": _now(), "job_id": job["id"], "type": job["type"],
                 "article_id": job["article_id"], "exception_type": type(exc).__name__, "frames": frames}
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in PROVIDER_ERRORS:
            entry["code"] = code
            status_code = getattr(exc, "http_status", None)
        else:
            status_code = code
        if type(status_code) is int and 100 <= status_code <= 599:
            entry["http_status"] = status_code
        try:
            with (self.runtime / "reading-errors.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # A log permission/storage issue must not trigger a model retry loop.
            pass

    def _articles(self):
        articles = []
        for path in sorted((Path(self.poc.DATA) / "articles").glob("wx-*.json")):
            try:
                value = self.poc.load_episode(path.stem)
                if isinstance(value, dict) and value.get("id") == path.stem:
                    articles.append(value)
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return articles

    @staticmethod
    def _published(article):
        value = article.get("published_at")
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return moment.astimezone(timezone.utc).isoformat() if moment.tzinfo else None
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _source_id(article):
        return article.get("source_id") or "__unknown__"

    def _seed_baselines(self, baselines, articles, *, first_import):
        """Freeze a source's activation waterline; never advance it from new imports."""
        by_source = {}
        for article in articles:
            by_source.setdefault(self._source_id(article), []).append(article)
        for source_id, group in by_source.items():
            if source_id in baselines:
                continue
            group.sort(key=lambda article: (self._published(article) or "", -(article.get("feed_rank") or 0)), reverse=True)
            selected = group[:2] if first_import else group
            baselines[source_id] = {
                "published_after": max((self._published(article) for article in group if self._published(article)), default=_now()),
                "initial_article_ids": [article["id"] for article in selected],
            }

    def _refresh(self, manual=False):
        baselines = self._runtime_value("automatic_source_baselines", {})
        # Capture the current POC's articles before importing deeper archives.
        # Persist before network work, so an interrupted refresh cannot promote
        # newly imported historical articles into the initial set on restart.
        initialized = self._runtime_value("automatic_baselines_initialized", False)
        self._seed_baselines(baselines, self._articles(), first_import=initialized)
        self._runtime_update(automatic_source_baselines=baselines, automatic_baselines_initialized=True)
        self._runtime_update(last_check_at=_now(), last_refresh_state="running", last_error=None)
        sources = self.poc.wechat_module().refresh(per_source=20)
        successful = sum(source.get("status") not in {"failed", "pending"} for source in sources)
        articles = self._articles()
        self._seed_baselines(baselines, articles, first_import=True)
        self._runtime_update(automatic_source_baselines=baselines)
        tracked = self._runtime_value("tracked_article_bodies", {})
        for article in articles:
            try:
                baseline = baselines[self._source_id(article)]
                published = self._published(article)
                known_changed = (article["id"] in tracked and bool(article.get("body_hash"))
                                 and tracked[article["id"]] != article["body_hash"])
                eligible = (article["id"] in baseline["initial_article_ids"]
                            or bool(published and published > baseline["published_after"]) or known_changed)
                if eligible and self._has_body(article) and not self._summary_ready(article):
                    self.enqueue(article["id"], manual=manual)
            except (ValueError, OSError, KeyError, TypeError):
                continue
        values = {"last_refresh_state": "completed" if successful else "failed",
                  "source_count": len(sources), "successful_sources": successful,
                  "failed_sources": len(sources) - successful, "last_error": None if successful else REFRESH_FAILURE}
        if successful:
            values["last_success_at"] = _now()
        self._runtime_update(**values)
        podcast_refresh = getattr(self.poc, "refresh_podcasts", None)
        if callable(podcast_refresh):
            podcast_refresh()
        if not successful:
            raise RuntimeError(REFRESH_FAILURE)

    def run_one(self, allow_automatic=True):
        """Run one job; manual priority never bypasses a reading cost window."""
        with self.lock_path.open("a") as worker_lock:
            try:
                fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                self._recover()
                policy = self._schedule()
                if not policy.get("execution_allowed", True):
                    not_before, reason = self._deferral_values(policy)
                    with self._transaction() as connection:
                        connection.execute("""UPDATE jobs SET not_before=?,deferred_reason=? WHERE state='queued'
                            AND (not_before IS NOT ? OR deferred_reason IS NOT ?)""", (not_before, reason, not_before, reason))
                    return False
                with self._transaction() as connection:
                    connection.execute("UPDATE jobs SET not_before=NULL,deferred_reason=NULL WHERE state='queued' AND deferred_reason='off_peak_only'")
                    row = connection.execute("""SELECT * FROM jobs WHERE state='queued'
                        AND (? OR manual=1)
                        ORDER BY manual DESC, CASE type WHEN 'refresh' THEN 0 ELSE 1 END, created_at, id LIMIT 1""",
                        (int(allow_automatic),)).fetchone()
                    if row is None:
                        return False
                    job = dict(row)
                    moment = _now()
                    connection.execute("""UPDATE jobs SET state='running', attempts=attempts+1,
                        started_at=?, updated_at=?, error=NULL,not_before=NULL,deferred_reason=NULL WHERE id=?""", (moment, moment, job["id"]))
                try:
                    if job["type"] == "refresh":
                        self._refresh(manual=bool(job["manual"]))
                    else:
                        article = self.poc.load_episode(job["article_id"])
                        if not self._has_input(article) or self._version(article) != job["input_version"]:
                            self._finish(job["id"], "superseded")
                            if self._has_input(article):
                                self.enqueue(article["id"], manual=bool(job["manual"]))
                            return True
                        if self._summary_ready(article):
                            self._finish(job["id"], "completed", reused=True)
                            return True
                        self.poc.summarize(job["article_id"])
                        completed = self.poc.load_episode(job["article_id"])
                        if self._version(completed) != job["input_version"]:
                            self._finish(job["id"], "superseded")
                            if self._has_input(completed):
                                self.enqueue(completed["id"], manual=bool(job["manual"]))
                            return True
                        if not self._summary_ready(completed):
                            raise ValueError("Summary generation did not persist a complete result")
                        self._runtime_update(provider_blocked=None, provider_blocked_at=None)
                    self._finish(job["id"], "completed")
                except schedule_policy.OffPeakDeferred as exc:
                    self._defer(job, exc)
                    return False
                except Exception as exc:
                    self._diagnostic(job, exc)
                    message = REFRESH_FAILURE if job["type"] == "refresh" else PUBLIC_FAILURE
                    code = getattr(exc, "code", None)
                    if job["type"] == "summary" and isinstance(code, str):
                        message = PROVIDER_ERRORS.get(code, message)
                        if code in {"authentication", "balance", "missing_key", "invalid_key"}:
                            self._runtime_update(provider_blocked=code, provider_blocked_at=_now())
                    self._finish(job["id"], "failed", error=message)
                    if job["type"] == "summary":
                        self._article_failure(job, message)
                    else:
                        self._runtime_update(last_refresh_state="failed", last_error=message)
                return True
            finally:
                fcntl.flock(worker_lock, fcntl.LOCK_UN)
