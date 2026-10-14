from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
import json
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
import re
import shlex
import sqlite3


ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        cls.collect = (ROOT / "collect.sh").read_text(encoding="utf-8")

    def test_deploy_bundle_contains_all_runtime_modules(self) -> None:
        for filename in (
            "app.py",
            "auth.py",
            "briefing_collect.py",
            "briefing_import.py",
            "briefing_service.py",
            "briefing_topics.py",
            "daily_enrichment.py",
            "content_quality.py",
            "event_relevance.py",
            "db.py",
            "decision_collect.py",
            "decision_service.py",
            "decision_snapshot.py",
            "market_data.py",
            "portfolio.py",
            "relation_engine.py",
            "macro_alert_service.py",
            "macro_collect.py",
            "llm_enrichment.py",
            "enrichment_collect.py",
            "options_capacity.py",
            "options_policy.py",
            "options_research_service.py",
            "options_strategy.py",
            "reading_service.py",
            "reading_worker.py",
            "reading_import.py",
            "news_catalog.py",
            "news_service.py",
            "news_collect.py",
            "news_enrichment.py",
            "collect.sh",
        ):
            self.assertIn(filename, self.deploy)

    def test_news_workers_are_quiesced_backed_up_and_restored(self) -> None:
        for name in ("prepare_unit_state_rollback", "restore_unit_states", "cleanup_remote"):
            start = self.deploy.index(name + "() {")
            end = self.deploy.index("\n}\n", start)
            for unit in ("kol-collect-news.timer", "kol-enrich-news.timer"):
                self.assertIn(unit, self.deploy[start:end], (name, unit))
        for action in ("backup_path", "restore_path"):
            self.assertIn(action + ' /etc/systemd/system/kol-enrich-news.service', self.deploy)
            self.assertIn(action + ' /etc/systemd/system/kol-enrich-news.timer', self.deploy)
        for section in ("rollback_configuration() {", 'backup_path /etc/systemd/system/kol-dashboard.service'):
            start = self.deploy.index(section)
            self.assertIn("for job in kol macro decision daily enrich news; do", self.deploy[start:])
        start = self.deploy.index("systemctl stop kol-enrich-wakeup.path")
        end = self.deploy.index("SERVICES_STOPPED=1", start)
        for unit in ("kol-collect-news.service", "kol-enrich-news.service"):
            # Stop first, then independently verify that each writer stopped.
            self.assertEqual(self.deploy[start:end].count(unit), 2)
        record = self.deploy.index("record_unit_state \"$unit\"")
        self.assertIn("kol-enrich-news.timer", self.deploy[record - 350:record])

    def test_generated_news_collection_unit_has_no_model_dependency(self) -> None:
        start = self.deploy.index("for job in kol macro decision daily enrich news; do\n  EXTRA_ENVIRONMENT=")
        end = self.deploy.index("\n# Only this worker", start)
        with tempfile.TemporaryDirectory() as directory:
            script = "REMOTE_STAGE=" + shlex.quote(directory) + "\ninstall() { :; }\n" + self.deploy[start:end]
            result = subprocess.run(["bash"], input=script, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = (Path(directory) / "kol-collect-news.service").read_text()
            self.assertIn("ExecStart=/usr/bin/python3 /opt/kol-dashboard/current/news_collect.py", unit)
            self.assertIn("TimeoutStartSec=3min", unit)
            self.assertIn("User=kol-dashboard", unit)
            self.assertIn("Environment=KOL_DASHBOARD_DB=/opt/kol-dashboard/data/kol_dashboard.db", unit)
            self.assertNotIn("deepseek.env", unit)
            self.assertNotIn("reading_worker", unit)
            self.assertNotIn("collect.sh news", unit)
            old_unit = (Path(directory) / "kol-collect-enrich.service").read_text()
            self.assertIn("TimeoutStartSec=20min", old_unit)
            self.assertIn("ExecStart=/bin/bash /opt/kol-dashboard/current/collect.sh enrich", old_unit)

    def test_news_enrichment_has_a_separate_bounded_secret_bearing_unit(self) -> None:
        start = self.deploy.index('cat > "$REMOTE_STAGE/kol-enrich-news.service"')
        end = self.deploy.index("\nUNIT\n", start)
        unit = self.deploy[start:end]
        self.assertIn("EnvironmentFile=-/etc/kol-dashboard/deepseek.env", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/kol-dashboard/current/news_enrichment.py --limit 8", unit)
        for line in ("User=kol-dashboard", "UMask=0077", "LimitCORE=0", "NoNewPrivileges=true", "TimeoutStartSec=420s"):
            self.assertIn(line, unit)
        self.assertNotIn("/opt/kol-dashboard/private", unit)

    def test_news_timers_have_five_minute_polling_and_acceptance_before_commit(self) -> None:
        for unit in ("kol-collect-news", "kol-enrich-news"):
            start = self.deploy.index('cat > "$REMOTE_STAGE/' + unit + '.timer"')
            end = self.deploy.index("\nUNIT\n", start)
            timer = self.deploy[start:end]
            for line in ("OnCalendar=*-*-* *:00/5:00", "RandomizedDelaySec=15s", "AccuracySec=5s", "Persistent=true", "Unit=" + unit + ".service"):
                self.assertIn(line, timer)
        preflight = self.deploy.index("/usr/bin/python3 -c 'import news_catalog, news_service, news_collect, news_enrichment'")
        stop = self.deploy.index("systemctl stop kol-enrich-wakeup.path")
        self.assertLess(preflight, stop)
        candidate = self.deploy.index("\nsystemctl start kol-collect-news.service\n")
        acceptance = self.deploy.index('validate_news_api "$NEWS_ACCEPTANCE_NOT_BEFORE"', candidate)
        timers = self.deploy.index("systemctl start kol-collect-kol.timer", acceptance)
        committed = self.deploy.index("\nCOMMITTED=1\n", timers)
        self.assertLess(candidate, acceptance)
        self.assertLess(acceptance, timers)
        self.assertLess(timers, committed)
        enabled = self.deploy.index("systemctl enable -q kol-dashboard")
        for unit in ("kol-collect-news.timer", "kol-enrich-news.timer"):
            self.assertIn(unit, self.deploy[enabled:candidate])
            self.assertIn(unit, self.deploy[timers:committed])
        self.assertIn("https://zlstreet.xyz/kol/api/news", self.deploy)

    def test_news_contexts_are_accepted_before_timers_and_commit(self) -> None:
        acceptance = self.deploy.index('validate_news_api "$NEWS_ACCEPTANCE_NOT_BEFORE"')
        contexts = self.deploy.index("\nvalidate_news_context_api\n", acceptance)
        timers = self.deploy.index("systemctl start kol-collect-kol.timer", contexts)
        self.assertLess(acceptance, contexts)
        self.assertLess(contexts, timers)
        function = self.deploy.index("validate_news_context_api() {")
        start = self.deploy.index("<<'PY_CONTEXT'\n", function) + len("<<'PY_CONTEXT'\n")
        end = self.deploy.index("\nPY_CONTEXT\n", start)
        validator = compile(self.deploy[start:end], "news-context-acceptance", "exec")
        def response(request, timeout):
            from urllib.parse import parse_qs, urlsplit
            view = parse_qs(urlsplit(request.full_url).query)["view"][0]
            value = {"view": view, "items": [], "sources": [], "generated_at": "2026-10-13T00:00:00+00:00", "analysis_schedule": {}}
            stream = io.StringIO(json.dumps(value))
            stream.status = 200
            return stream
        with patch("urllib.request.urlopen", side_effect=response) as fetch, redirect_stdout(io.StringIO()):
            exec(validator, {})
        self.assertEqual(fetch.call_count, 4)
        self.assertIn("asset=US%3ANVDA", fetch.call_args_list[-1].args[0].full_url)
        bad = io.StringIO(json.dumps({"view": "home", "items": [], "sources": []}))
        bad.status = 200
        with patch("urllib.request.urlopen", return_value=bad), self.assertRaises(SystemExit):
            exec(validator, {})

    def test_news_acceptance_rejects_partial_stale_or_empty_successes(self) -> None:
        function = self.deploy.index("validate_news_api() {")
        start = self.deploy.index("<<'PY_NEWS'\n", function) + len("<<'PY_NEWS'\n")
        end = self.deploy.index("\nPY_NEWS\n", start)
        validator = compile(self.deploy[start:end], "news-release-acceptance", "exec")
        now = datetime.now(timezone.utc)
        source_ids = ("bloomberg_markets", "bloomberg_technology", "wsj_markets", "wsj_business", "wsj_technology")

        def payload() -> dict:
            return {"sources": [{"id": source_id, "status": "ok", "stale": False, "last_checked_at": now.isoformat(), "last_success_at": now.isoformat(), "item_count": 2} for source_id in source_ids], "items": [{"title": "Accepted news item"}]}

        def run(value: dict) -> None:
            response = io.StringIO(json.dumps(value))
            response.status = 200
            with patch("urllib.request.urlopen", return_value=response), patch.object(sys, "argv", ["acceptance", str(int(now.timestamp()) - 1)]), redirect_stdout(io.StringIO()):
                exec(validator, {})

        run(payload())
        mutations = {
            "missing source": lambda value: value["sources"].pop(),
            "failed source": lambda value: value["sources"][0].update(status="error"),
            "stale source": lambda value: value["sources"][0].update(stale=True),
            "no source entries": lambda value: value["sources"][0].update(item_count=0),
            "old run": lambda value: value["sources"][0].update(last_success_at=(now - timedelta(minutes=10)).isoformat()),
            "future run": lambda value: value["sources"][0].update(last_checked_at=(now + timedelta(minutes=10)).isoformat()),
            "naive time": lambda value: value["sources"][0].update(last_checked_at=now.replace(tzinfo=None).isoformat()),
            "empty api": lambda value: value.update(items=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = payload()
                mutate(value)
                with self.assertRaises(SystemExit):
                    run(value)

    def test_deploy_bundle_omits_macos_appledouble_metadata(self) -> None:
        self.assertIn("COPYFILE_DISABLE=1 tar --no-xattrs -czf", self.deploy)
        self.assertNotIn("tar --no-xattrs czf", self.deploy)

    def test_deploy_defaults_to_a_production_sized_remote_timeout(self) -> None:
        self.assertIn(
            'export RSH_TIMEOUT="${RSH_TIMEOUT:-1200}"',
            self.deploy,
        )

    def test_deploy_resolves_the_stable_vps_helper_with_explicit_override(self) -> None:
        self.assertIn('VPS="${VPS_HELPER:-}"', self.deploy)
        self.assertIn('command -v zlstreet-vps', self.deploy)
        self.assertIn("请安装 zlstreet-vps 或设置 VPS_HELPER", self.deploy)
        self.assertNotIn(".cursor/skills/aliyun-ops", self.deploy)

    def test_systemd_uses_root_only_environment_file(self) -> None:
        self.assertIn("EnvironmentFile=-/etc/kol-dashboard.env", self.deploy)
        self.assertIn("chmod 600 /etc/kol-dashboard.env", self.deploy)
        self.assertIn("KOL_DASHBOARD_COOKIE_PATH=/kol", self.deploy)
        self.assertIn("KOL_DASHBOARD_COOKIE_SECURE=true", self.deploy)
        self.assertNotIn(
            "Environment=KOL_DASHBOARD_SESSION_SECRET",
            self.deploy,
        )
        self.assertNotIn(
            "Environment=KOL_DASHBOARD_PASSCODE_HASH",
            self.deploy,
        )
        self.assertIn("JOB_TIMEOUT=20min", self.deploy)
        self.assertIn("TimeoutStartSec=${JOB_TIMEOUT}", self.deploy)

    def test_decision_collection_is_scheduled_and_fail_fast(self) -> None:
        self.assertIn("set -euo pipefail", self.collect)
        self.assertIn('decision_collect.py" relations', self.collect)
        self.assertIn('decision_collect.py" all', self.collect)
        self.assertIn("kol-collect-decision.timer", self.deploy)
        self.assertIn("python3 -c 'import db; db.init()'", self.deploy)
        self.assertIn("KOL_DB_WRITE_REQUIRED=1", self.collect)

    def test_kol_collector_uses_one_tiered_writer_every_15_minutes(self) -> None:
        self.assertIn('"$TRACKER" collect-tiered 6', self.collect)
        self.assertIn('KOL_CADENCE_STATE_PATH="$KOL_CADENCE_STATE"', self.collect)
        self.assertIn('flock -n 9', self.collect)
        self.assertIn('skipped overlapping collection', self.collect)
        self.assertIn('|| KOL_RC=$?', self.collect)
        self.assertIn('hot-lane downstream work completed', self.collect)
        overlap_start = self.collect.index("if ! flock -n 9; then")
        overlap_end = self.collect.index("    fi", overlap_start)
        self.assertIn("exit 0", self.collect[overlap_start:overlap_end])

        timer_start = self.deploy.index(
            'cat > "$REMOTE_STAGE/kol-collect-kol.timer"'
        )
        timer_end = self.deploy.index(
            'cat > "$REMOTE_STAGE/kol-collect-macro.timer"', timer_start
        )
        timer = self.deploy[timer_start:timer_end]
        self.assertIn("OnCalendar=*-*-* *:00,15,30,45:00", timer)
        self.assertIn("RandomizedDelaySec=2min", timer)
        self.assertIn("AccuracySec=30s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=kol-collect-kol.service", timer)
        self.assertNotIn("OnUnitActiveSec=", timer)
        self.assertIn(
            "Environment=KOL_CADENCE_STATE_PATH=/opt/kol-dashboard/data/kol-cadence-state.json",
            self.deploy,
        )
        kol_start = self.collect.index("  kol)")
        macro_start = self.collect.index("  macro)", kol_start)
        kol_job = self.collect[kol_start:macro_start]
        self.assertLess(
            kol_job.index('decision_collect.py" relations'),
            kol_job.index('exit "$KOL_RC"'),
        )
        self.assertLess(
            kol_job.index("signal_enrichment"),
            kol_job.index('exit "$KOL_RC"'),
        )

    def test_daily_collector_is_packaged_and_scheduled_hourly(self) -> None:
        self.assertIn("briefing_collect.py", self.deploy)
        daily_start = self.collect.index("  daily)")
        enrich_start = self.collect.index("  enrich)", daily_start)
        daily_job = self.collect[daily_start:enrich_start]
        self.assertIn('briefing_collect.py"', daily_job)
        self.assertIn('--output "$DAILY_SNAPSHOT"', daily_job)
        self.assertIn('--import --db "$KOL_DASHBOARD_DB"', daily_job)

        timer_start = self.deploy.index(
            'cat > "$REMOTE_STAGE/kol-collect-daily.timer"'
        )
        timer_end = self.deploy.index(
            'cat > "$REMOTE_STAGE/kol-collect-enrich.timer"', timer_start
        )
        timer = self.deploy[timer_start:timer_end]
        self.assertIn("OnCalendar=*-*-* *:05:00", timer)
        self.assertIn("RandomizedDelaySec=90s", timer)
        self.assertIn("AccuracySec=30s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=kol-collect-daily.service", timer)
        self.assertNotIn("OnUnitActiveSec=", timer)

    def test_daily_worker_receives_schedule_and_deepseek_environment(self) -> None:
        self.assertEqual(
            self.deploy.count("Environment=KOL_DAILY_REFRESH_SCHEDULE=hourly"),
            2,
        )
        self.assertIn(
            'if [[ "$job" == "daily" || "$job" == "enrich" ]]; then',
            self.deploy,
        )
        self.assertIn(
            'EXTRA_ENVIRONMENT="EnvironmentFile=-/etc/kol-dashboard/deepseek.env"',
            self.deploy,
        )
        self.assertIn(
            'if [[ "$job" == "daily" ]]; then',
            self.deploy,
        )
        self.assertIn(
            'EXTRA_SCHEDULE="Environment=KOL_DAILY_REFRESH_SCHEDULE=hourly"',
            self.deploy,
        )
        self.assertNotIn("DEEPSEEK_API_KEY=", self.deploy)

    def test_daily_candidate_is_imported_and_accepted_before_commit(self) -> None:
        candidate_start = self.deploy.index(
            'systemctl start kol-collect-daily.service'
        )
        snapshot_acceptance = self.deploy.index(
            'validate_daily_snapshot "$DAILY_ACCEPTANCE_NOT_BEFORE"',
            candidate_start,
        )
        api_acceptance = self.deploy.index(
            "validate_daily_api", snapshot_acceptance
        )
        timer_start = self.deploy.index(
            "systemctl start kol-collect-kol.timer", api_acceptance
        )
        committed = self.deploy.index("COMMITTED=1", timer_start)

        self.assertLess(candidate_start, snapshot_acceptance)
        self.assertLess(snapshot_acceptance, api_acceptance)
        self.assertLess(api_acceptance, timer_start)
        self.assertLess(timer_start, committed)
        for required in (
            'payload.get("refresh_schedule_status") not in {"configured", "active"}',
            'payload.get("next_refresh_at")',
            "database_integrity \"$DB_PATH\"",
            "https://zlstreet.xyz/kol/api/briefings/latest",
        ):
            self.assertIn(required, self.deploy)

    def test_daily_snapshot_accepts_empty_curated_feeds_but_keeps_core_gates(
        self,
    ) -> None:
        contract_start = self.deploy.index("validate_daily_snapshot()")
        contract_end = self.deploy.index("validate_daily_api()", contract_start)
        contract = self.deploy[contract_start:contract_end]

        self.assertIn('required = {("Hacker News", "hn_story")}', contract)
        self.assertNotIn('("AI Digest", "ai_digest")', contract)
        self.assertNotIn('("AI Brief", "paper_digest")', contract)
        self.assertIn(
            'expected_sections = {"macro", "world", "finance", '
            '"technology", "ai", "investors"}',
            contract,
        )
        self.assertIn("coverage_time < not_before - timedelta(minutes=5)", contract)
        self.assertIn("coverage_time > now + timedelta(minutes=5)", contract)

    def test_daily_units_are_quiesced_enabled_and_rollback_safe(self) -> None:
        self.assertGreaterEqual(
            self.deploy.count("for job in kol macro decision daily enrich news; do"),
            3,
        )
        self.assertIn(
            'backup_path "/etc/systemd/system/kol-collect-${job}.service"',
            self.deploy,
        )
        self.assertIn(
            'restore_path "/etc/systemd/system/kol-collect-${job}.service"',
            self.deploy,
        )
        self.assertIn(
            'DAILY_SNAPSHOT_PATH="$DATA_DIR/daily-briefing-latest.json"',
            self.deploy,
        )
        self.assertIn(
            'backup_path "$DAILY_SNAPSHOT_PATH" daily-briefing-latest.json',
            self.deploy,
        )
        self.assertIn("rollback_daily_snapshot()", self.deploy)
        self.assertIn(
            "rollback_daily_snapshot || rollback_failed=1",
            self.deploy,
        )
        self.assertIn(
            '[[ -f "$DAILY_SNAPSHOT_PATH" && ! -L "$DAILY_SNAPSHOT_PATH" ]]',
            self.deploy,
        )

        rollback_start = self.deploy.index("cleanup_remote()")
        rollback_end = self.deploy.index("\n}\ntrap cleanup_remote", rollback_start)
        rollback = self.deploy[rollback_start:rollback_end]
        self.assertIn("kol-collect-daily.timer", rollback)
        self.assertIn("kol-collect-daily.service", rollback)

        quiesce_start = self.deploy.index("systemctl stop kol-enrich-wakeup.path")
        quiesce_end = self.deploy.index("SERVICES_STOPPED=1", quiesce_start)
        quiesce = self.deploy[quiesce_start:quiesce_end]
        self.assertIn("kol-collect-daily.timer", quiesce)
        self.assertIn("kol-collect-daily.service", quiesce)

        enable_start = self.deploy.index("systemctl enable -q kol-dashboard")
        enable_end = self.deploy.index("nginx -t", enable_start)
        self.assertIn(
            "kol-collect-daily.timer",
            self.deploy[enable_start:enable_end],
        )
        timer_start = self.deploy.index(
            "systemctl start kol-collect-kol.timer", enable_end
        )
        timer_acceptance = self.deploy.index(
            'systemctl is-enabled --quiet "$unit"', timer_start
        )
        start_block = self.deploy[timer_start:timer_acceptance]
        self.assertIn("kol-collect-daily.timer", start_block)
        self.assertIn(
            "for unit in kol-collect-kol.timer kol-collect-macro.timer",
            self.deploy[timer_start:],
        )

    def test_decision_snapshot_is_prewarmed_before_release_switch(self) -> None:
        prewarm = self.deploy.index(
            '"$RELEASE_DIR/decision_collect.py" snapshot'
        )
        release_switch = self.deploy.index(
            'ln -s "$RELEASE_DIR" "$CURRENT_NEXT"', prewarm
        )
        self.assertLess(prewarm, release_switch)
        self.assertIn("runuser -u kol-dashboard -- /usr/bin/env", self.deploy)
        self.assertIn('KOL_DASHBOARD_DB="$DB_PATH"', self.deploy)
        self.assertIn("database_integrity \"$DB_PATH\"", self.deploy[prewarm:])

    def test_enrichment_has_an_isolated_secret_file_and_hardened_timer(self) -> None:
        self.assertIn('enrich)', self.collect)
        self.assertIn('enrichment_collect.py")', self.collect)
        self.assertIn("kol-collect-enrich.timer", self.deploy)
        self.assertIn(
            'cat > "$REMOTE_STAGE/kol-collect-enrich.timer"',
            self.deploy,
        )
        self.assertIn(
            'if [[ "$job" == "daily" || "$job" == "enrich" ]]; then',
            self.deploy,
        )
        self.assertIn(
            'EXTRA_ENVIRONMENT="EnvironmentFile=-/etc/kol-dashboard/deepseek.env"',
            self.deploy,
        )
        self.assertEqual(
            self.deploy.count(
                "EnvironmentFile=-/etc/kol-dashboard/deepseek.env"
            ),
            3,
        )
        self.assertIn('EXTRA_HARDENING="LimitCORE=0"', self.deploy)
        self.assertNotIn("DEEPSEEK_API_KEY=", self.deploy)
        self.assertNotIn("DEEPSEEK_API_KEY=", self.collect)
        # The externally provisioned root-only file must never be created,
        # copied, rewritten or bundled by this deployment script.
        self.assertNotIn("put-secret deepseek", self.deploy)
        self.assertNotIn("deepseek.env\" <<", self.deploy)

    def test_successful_source_collection_wakes_enrichment(self) -> None:
        self.assertIn(
            'ENRICHMENT_MARKER="${KOL_ENRICH_WAKE_PATH:-$(dirname '
            '"$KOL_DASHBOARD_DB")/enrichment.pending}"',
            self.collect,
        )
        self.assertEqual(
            self.collect.count("\n    signal_enrichment\n"),
            2,
        )
        kol_start = self.collect.index("  kol)")
        macro_start = self.collect.index("  macro)", kol_start)
        decision_start = self.collect.index("  decision)", macro_start)
        kol_job = self.collect[kol_start:macro_start]
        macro_job = self.collect[macro_start:decision_start]
        self.assertGreater(
            kol_job.index("signal_enrichment"),
            kol_job.index('decision_collect.py" relations'),
        )
        self.assertGreater(
            macro_job.index("signal_enrichment"),
            macro_job.index('decision_collect.py" relations'),
        )

    def test_enrichment_path_unit_is_atomic_and_rollback_safe(self) -> None:
        self.assertIn(
            'cat > "$REMOTE_STAGE/kol-enrich-wakeup.path"',
            self.deploy,
        )
        self.assertIn(
            "PathExists=/opt/kol-dashboard/data/enrichment.pending",
            self.deploy,
        )
        self.assertIn("Unit=kol-collect-enrich.service", self.deploy)
        self.assertIn(
            "ExecStartPre=/usr/bin/rm -f "
            "/opt/kol-dashboard/data/enrichment.pending",
            self.deploy,
        )
        self.assertIn(
            "backup_path /etc/systemd/system/kol-enrich-wakeup.path",
            self.deploy,
        )
        self.assertIn(
            "restore_path /etc/systemd/system/kol-enrich-wakeup.path",
            self.deploy,
        )
        self.assertIn("systemctl stop kol-enrich-wakeup.path", self.deploy)
        self.assertIn("kol-collect-enrich.timer", self.deploy)
        self.assertIn("kol-enrich-wakeup.path kol-reading-worker.service; do", self.deploy)

        enable = self.deploy.index("systemctl enable -q kol-dashboard")
        nginx_test = self.deploy.index("nginx -t", enable)
        self.assertIn(
            "kol-enrich-wakeup.path",
            self.deploy[enable:nginx_test],
        )
        start = self.deploy.index(
            "systemctl start kol-collect-kol.timer", nginx_test
        )
        acceptance = self.deploy.index(
            "systemctl is-enabled --quiet kol-enrich-wakeup.path", start
        )
        self.assertIn(
            "kol-enrich-wakeup.path",
            self.deploy[start:acceptance],
        )
        self.assertIn(
            "systemctl is-active --quiet kol-enrich-wakeup.path",
            self.deploy[acceptance:],
        )

    def test_nginx_gzips_dynamic_payloads_and_caches_versioned_assets(self) -> None:
        static_start = self.deploy.index("location ^~ /kol/static/ {")
        dynamic_start = self.deploy.index("location /kol/ {", static_start)
        nginx_end = self.deploy.index("\nNGINX", dynamic_start)
        static_location = self.deploy[static_start:dynamic_start]
        dynamic_location = self.deploy[dynamic_start:nginx_end]

        self.assertIn(
            "proxy_pass http://127.0.0.1:8088/static/;",
            static_location,
        )
        self.assertIn("gzip on;", static_location)
        self.assertIn("gzip_vary on;", static_location)
        self.assertIn("gzip_comp_level 5;", static_location)
        self.assertIn(
            "gzip_types application/json application/javascript "
            "text/javascript text/css image/svg+xml;",
            static_location,
        )
        self.assertIn("proxy_hide_header Cache-Control;", static_location)
        self.assertIn(
            'add_header Cache-Control "public, max-age=604800, immutable" always;',
            static_location,
        )
        self.assertIn("gzip on;", dynamic_location)
        self.assertIn("gzip_vary on;", dynamic_location)
        self.assertIn("gzip_comp_level 5;", dynamic_location)
        self.assertIn(
            "gzip_types application/json application/javascript "
            "text/javascript text/css image/svg+xml;",
            dynamic_location,
        )

    def test_nginx_preflight_rejects_legacy_include_before_service_stop(
        self,
    ) -> None:
        preflight = self.deploy.index(
            "NGINX_SITE=/etc/nginx/sites-enabled/aidao"
        )
        rollback_ready = self.deploy.index("\nROLLBACK_READY=1\n", preflight)
        service_stop = self.deploy.index(
            "systemctl stop kol-enrich-wakeup.path", preflight
        )
        gate = self.deploy[preflight:rollback_ready]

        self.assertIn(
            "/root/kol-dashboard/deployment/nginx/kol.locations.conf",
            gate,
        )
        self.assertIn("LEGACY_INCLUDE_STATUS", gate)
        self.assertIn("nginx -t", gate)
        self.assertLess(preflight, rollback_ready)
        self.assertLess(preflight, service_stop)

    def test_nginx_activation_starts_inactive_service_and_is_verified(
        self,
    ) -> None:
        helper_start = self.deploy.index("activate_nginx()")
        helper_end = self.deploy.index("\n}\n", helper_start) + 3
        helper = self.deploy[helper_start:helper_end]
        self.assertIn("systemctl reload nginx", helper)
        self.assertIn("systemctl start nginx", helper)
        self.assertGreaterEqual(
            helper.count("systemctl is-active --quiet nginx"),
            2,
        )

        direct_health = self.deploy.index('[[ "$DIRECT_HEALTH" == ok ]]')
        activation = self.deploy.index("\nactivate_nginx\n", direct_health)
        proxy_health = self.deploy.index("PROXY_HEALTH=FAILED", activation)
        self.assertLess(direct_health, activation)
        self.assertLess(activation, proxy_health)

        rollback = self.deploy.index("cleanup_remote()")
        restore = self.deploy.index(
            "rollback_configuration || rollback_failed=1", rollback
        )
        rollback_activation = self.deploy.index(
            "activate_nginx >/dev/null 2>&1 || rollback_failed=1",
            restore,
        )
        self.assertGreater(rollback_activation, restore)

    def test_existing_deepseek_secret_file_must_be_root_only(self) -> None:
        self.assertIn(
            "stat -c '%U:%G:%a' /etc/kol-dashboard/deepseek.env",
            self.deploy,
        )
        self.assertIn(
            '"$DEEPSEEK_SECRET_STAT" == "root:root:600"',
            self.deploy,
        )
        self.assertNotIn("source /etc/kol-dashboard/deepseek.env", self.deploy)
        self.assertNotIn("cat /etc/kol-dashboard/deepseek.env", self.deploy)
        self.assertNotIn("chmod 600 /etc/kol-dashboard/deepseek.env", self.deploy)
        self.assertNotIn("chown root:root /etc/kol-dashboard/deepseek.env", self.deploy)

    def test_rollback_restores_original_unit_enablement_and_activity(self) -> None:
        self.assertIn("record_unit_state()", self.deploy)
        self.assertIn("prepare_unit_state_rollback()", self.deploy)
        self.assertIn("restore_unit_states()", self.deploy)
        self.assertIn('> "$ROLLBACK_DIR/config/$unit.state"', self.deploy)
        self.assertIn('systemctl disable -q "$unit"', self.deploy)
        self.assertIn('systemctl enable -q "$unit"', self.deploy)
        self.assertIn('systemctl start "$unit"', self.deploy)
        self.assertIn(
            "prepare_unit_state_rollback || rollback_failed=1",
            self.deploy,
        )
        self.assertIn("restore_unit_states || rollback_failed=1", self.deploy)
        self.assertNotIn("start_available_units", self.deploy)

    def test_all_worker_logs_are_precreated_private(self) -> None:
        self.assertIn(
            "for log_file in out.log err.log collect.log collect.err.log",
            self.deploy,
        )
        self.assertIn("-m 600 /dev/null", self.deploy)
        self.assertIn(
            'chmod 600 "/var/log/kol-dashboard/$log_file"',
            self.deploy,
        )

    def test_auth_rotation_requires_explicit_flag_and_secure_rewrite(self) -> None:
        self.assertIn("if [[ $CONFIGURE_AUTH == 1 ]]; then", self.deploy)
        self.assertNotIn(
            "CONFIGURE_AUTH == 1 || -n",
            self.deploy,
        )
        self.assertIn("KOL_DASHBOARD_COOKIE_PATH=/kol", self.deploy)
        self.assertIn("KOL_DASHBOARD_COOKIE_SECURE=true", self.deploy)
        self.assertIn("os.replace", self.deploy)
        self.assertIn(
            '"$VPS" put-secret "$WORK/auth.env"',
            self.deploy,
        )
        self.assertNotIn(
            '"$VPS" put "$WORK/auth.env"',
            self.deploy,
        )

    def test_default_auth_rewrite_preserves_cookie_name(self) -> None:
        rewrite_marker = (
            'python3 - "$REMOTE_STAGE/auth.env" '
            "/etc/kol-dashboard.env <<'PY'\n"
        )
        rewrite_start = self.deploy.index(rewrite_marker) + len(rewrite_marker)
        rewrite_end = self.deploy.index(
            "\nPY\nchmod 600 /etc/kol-dashboard.env",
            rewrite_start,
        )
        rewrite_program = self.deploy[rewrite_start:rewrite_end]

        # COOKIE_NAME must be admitted while reading and emitted in a stable
        # position; the functional assertions below exercise both contracts.
        self.assertEqual(
            rewrite_program.count('"KOL_DASHBOARD_COOKIE_NAME",'),
            2,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            missing_incoming = directory / "auth.env"
            target = directory / "kol-dashboard.env"
            target.write_text(
                "\n".join(
                    (
                        "KOL_DASHBOARD_COOKIE_SECURE=false",
                        "KOL_DASHBOARD_COOKIE_NAME=zlstreet_private_session",
                        "KOL_DASHBOARD_SESSION_TTL_SECONDS=3600",
                        "KOL_DASHBOARD_SESSION_SECRET=session-secret",
                        "KOL_DASHBOARD_PASSCODE_HASH=passcode-hash",
                        "KOL_DASHBOARD_COOKIE_PATH=/legacy",
                        "KOL_DASHBOARD_HOLDINGS_FILE=/legacy/holdings.md",
                        "UNRELATED_SECRET=must-not-survive",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(missing_incoming),
                    str(target),
                ],
                input=rewrite_program,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rewritten_lines = target.read_text(encoding="utf-8").splitlines()

            self.assertEqual(
                rewritten_lines,
                [
                    "KOL_DASHBOARD_PASSCODE_HASH=passcode-hash",
                    "KOL_DASHBOARD_SESSION_SECRET=session-secret",
                    "KOL_DASHBOARD_SESSION_TTL_SECONDS=3600",
                    "KOL_DASHBOARD_COOKIE_NAME=zlstreet_private_session",
                    "KOL_DASHBOARD_COOKIE_PATH=/kol",
                    "KOL_DASHBOARD_COOKIE_SECURE=true",
                    "KOL_DASHBOARD_HOLDINGS_FILE="
                    "/opt/kol-dashboard/private/holdings.md",
                ],
            )
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_noninteractive_passcode_is_unexported_before_child_processes(
        self,
    ) -> None:
        capture = self.deploy.index(
            'CAPTURED_PASSCODE="${KOL_DASHBOARD_PASSCODE-}"'
        )
        unexport = self.deploy.index("export -n CAPTURED_PASSCODE", capture)
        remove_environment = self.deploy.index(
            "unset KOL_DASHBOARD_PASSCODE", unexport
        )
        early_cleanup_trap = self.deploy.index(
            "trap cleanup_auth_only EXIT INT TERM",
            remove_environment,
        )
        first_external_command = min(
            self.deploy.index('LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"'),
            self.deploy.index('WORK="$(mktemp -d)"'),
            self.deploy.index('"$VPS" run'),
        )

        self.assertLess(capture, unexport)
        self.assertLess(unexport, remove_environment)
        self.assertLess(remove_environment, early_cleanup_trap)
        self.assertLess(early_cleanup_trap, first_external_command)
        self.assertNotIn(
            "${KOL_DASHBOARD_PASSCODE",
            self.deploy[remove_environment:],
        )

        auth_start = self.deploy.index(
            "if [[ $CONFIGURE_AUTH == 1 ]]; then"
        )
        auth_end = self.deploy.index("\nelse\n", auth_start)
        auth_block = self.deploy[auth_start:auth_end]
        self.assertIn('PASSCODE="$CAPTURED_PASSCODE"', auth_block)
        self.assertIn('if [[ -z "$PASSCODE" ]]; then', auth_block)
        self.assertIn(
            'read -r -s -p "私人模式新口令: " PASSCODE',
            auth_block,
        )
        self.assertIn(
            'read -r -s -p "再次输入口令: " PASSCODE_CONFIRM',
            auth_block,
        )

        secret_cleanup = self.deploy.index("clear_auth_material()")
        secret_cleanup_end = self.deploy.index("\n}", secret_cleanup)
        self.assertIn(
            "unset CAPTURED_PASSCODE PASSCODE PASSCODE_CONFIRM "
            "PASSCODE_HASH SESSION_SECRET",
            self.deploy[secret_cleanup:secret_cleanup_end],
        )
        cleanup_auth_only = self.deploy.index("cleanup_auth_only()")
        cleanup_auth_only_end = self.deploy.index("\n}", cleanup_auth_only)
        self.assertIn(
            "clear_auth_material",
            self.deploy[cleanup_auth_only:cleanup_auth_only_end],
        )
        cleanup_local = self.deploy.index("cleanup_local()")
        cleanup_local_end = self.deploy.index("\n}", cleanup_local)
        self.assertIn(
            "clear_auth_material",
            self.deploy[cleanup_local:cleanup_local_end],
        )

    def test_remote_staging_and_release_switch_are_atomic(self) -> None:
        self.assertIn("REMOTE_STAGE=", self.deploy)
        self.assertIn("install -d -m 700", self.deploy)
        self.assertNotIn("/tmp/kol-auth.env", self.deploy)
        self.assertNotIn("/tmp/kol-db.gz", self.deploy)
        self.assertNotIn("/tmp/kol-app.tgz", self.deploy)
        self.assertIn("--no-same-owner", self.deploy)
        self.assertIn("releases", self.deploy)
        self.assertIn("current.next", self.deploy)
        self.assertIn("chown -R root:root", self.deploy)

    def test_generated_remote_script_has_valid_bash_syntax(self) -> None:
        remote_start = self.deploy.index("cat <<'REMOTE'\n") + len(
            "cat <<'REMOTE'\n"
        )
        remote_end = self.deploy.index("\nREMOTE\n", remote_start)
        remote_script = self.deploy[remote_start:remote_end]
        result = subprocess.run(
            ["bash", "-n"],
            input=remote_script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_database_transfer_uses_sqlite_backup_and_private_permissions(
        self,
    ) -> None:
        self.assertIn("source.backup(destination)", self.deploy)
        self.assertIn("PRAGMA integrity_check", self.deploy)
        self.assertIn('chmod 700 "$DATA_DIR"', self.deploy)
        self.assertIn('chmod 600 "$DB_PATH"', self.deploy)

    def test_remote_deploy_serializes_and_stops_every_database_writer(
        self,
    ) -> None:
        self.assertIn("flock -n 9", self.deploy)
        for unit in (
            "kol-dashboard.service",
            "kol-collect-kol.service",
            "kol-collect-macro.service",
            "kol-collect-decision.service",
            "kol-collect-daily.service",
            "kol-collect-enrich.service",
            "kol-collect-news.service",
            "kol-enrich-news.service",
        ):
            self.assertIn(unit, self.deploy)
        self.assertIn("PRAGMA wal_checkpoint(TRUNCATE)", self.deploy)
        self.assertIn("active database writer remains", self.deploy)

    def test_failed_release_can_restore_legacy_code_data_and_config(
        self,
    ) -> None:
        self.assertIn("legacy-$RELEASE_ID", self.deploy)
        self.assertIn("ROLLBACK_READY=1", self.deploy)
        self.assertIn("rollback_database", self.deploy)
        self.assertIn("rollback_configuration", self.deploy)
        self.assertIn("database.before-release", self.deploy)
        self.assertIn("database.before-release.next", self.deploy)
        self.assertIn("DB_ROLLBACK_READY=1", self.deploy)
        self.assertIn("ROLLBACK INCOMPLETE", self.deploy)
        self.assertIn("PRESERVE", self.deploy)

    def test_rollback_never_replaces_database_while_a_writer_can_run(
        self,
    ) -> None:
        cleanup_start = self.deploy.index("cleanup_remote()")
        cleanup_end = self.deploy.index("\n}\ntrap cleanup_remote", cleanup_start)
        cleanup = self.deploy[cleanup_start:cleanup_end]
        stop_guard = cleanup.index('if [[ $rollback_safe == 1 ]]; then')
        database_restore = cleanup.index("rollback_database", stop_guard)
        self.assertLess(stop_guard, database_restore)
        guard_setup = cleanup[:stop_guard]
        for unit in (
            "kol-enrich-wakeup.path",
            "kol-collect-kol.timer",
            "kol-collect-macro.timer",
            "kol-collect-decision.timer",
            "kol-collect-daily.timer",
            "kol-collect-enrich.timer",
            "kol-collect-news.timer",
            "kol-enrich-news.timer",
            "kol-collect-kol.service",
            "kol-collect-macro.service",
            "kol-collect-decision.service",
            "kol-collect-daily.service",
            "kol-collect-enrich.service",
            "kol-collect-news.service",
            "kol-enrich-news.service",
            "kol-dashboard.service",
        ):
            self.assertIn(unit, guard_setup)
        self.assertIn('systemctl stop "${rollback_units[@]}"', guard_setup)
        self.assertIn('for unit in "${rollback_units[@]}"', guard_setup)
        self.assertIn(
            "systemctl show --property=LoadState --value", guard_setup
        )
        self.assertIn(
            "systemctl show --property=ActiveState --value", guard_setup
        )
        self.assertIn("unit_query_rc=$?", guard_setup)
        self.assertIn("unit_query_rc != 0", guard_setup)
        self.assertIn('-z "$unit_load_state"', guard_setup)
        self.assertIn('"$unit_load_state" == not-found', guard_setup)
        self.assertIn('-z "$unit_state"', guard_setup)
        self.assertIn('"$unit_state" != inactive', guard_setup)
        self.assertIn('"$unit_state" != failed', guard_setup)
        self.assertIn("rollback_safe=0", guard_setup)
        self.assertNotIn("systemctl cat", guard_setup)
        abort = cleanup.index("ROLLBACK ABORTED", database_restore)
        unsafe_start = cleanup.rindex("    else\n", database_restore, abort)
        unsafe_branch = cleanup[unsafe_start:]
        self.assertIn('> "$REMOTE_STAGE/PRESERVE"', unsafe_branch)
        for forbidden in (
            "rollback_database",
            "rollback_configuration",
            "restore_unit_states",
            'mv -Tf "$CURRENT_NEXT" "$CURRENT_LINK"',
        ):
            self.assertNotIn(forbidden, unsafe_branch)

    def test_successful_release_keeps_a_private_pre_migration_database(
        self,
    ) -> None:
        self.assertIn('BACKUPS_DIR="$BASE_DIR/backups"', self.deploy)
        self.assertIn(
            'install -d -m 700 "$STAGING_DIR" "$BACKUPS_DIR"',
            self.deploy,
        )
        backup = self.deploy.index(
            'DURABLE_DB_BACKUP="$BACKUPS_DIR/database.before-'
            '$RELEASE_ID.sqlite3"'
        )
        committed = self.deploy.index("COMMITTED=1", backup)
        self.assertLess(backup, committed)
        contract = self.deploy[backup:committed]
        source_backup = '"$ROLLBACK_DIR/database.before-release"'
        self.assertIn(source_backup, contract)
        self.assertNotIn('"$DB_PATH"', contract)
        self.assertIn("install -o root -g root -m 600", contract)
        self.assertIn('database_integrity "$DURABLE_DB_BACKUP"', contract)
        self.assertIn("os.fsync(handle.fileno())", contract)
        self.assertIn("os.fsync(directory_fd)", contract)
        source_created = self.deploy.index(
            '"$ROLLBACK_DIR/database.before-release"',
            self.deploy.index('if [[ -f "$DB_PATH" ]]'),
        )
        schema_migration = self.deploy.index(
            'python3 -c \'import db; db.init()\''
        )
        proxy_healthy = self.deploy.index('[[ "$PROXY_HEALTH" == ok ]]')
        wakeup_active = self.deploy.index(
            "systemctl is-active --quiet kol-enrich-wakeup.path",
            proxy_healthy,
        )
        self.assertLess(source_created, schema_migration)
        self.assertLess(proxy_healthy, wakeup_active)
        self.assertLess(wakeup_active, backup)

    def test_service_is_unprivileged_and_proxy_is_verified_before_timers(
        self,
    ) -> None:
        self.assertIn("User=kol-dashboard", self.deploy)
        self.assertIn("Group=kol-dashboard", self.deploy)
        self.assertIn("chgrp -R kol-dashboard", self.deploy)
        self.assertIn("chmod -R u=rwX,g=rX,o=", self.deploy)
        self.assertIn("runuser -u kol-dashboard -- test -r", self.deploy)
        self.assertNotIn("pip3 install", self.deploy)
        proxy_check = self.deploy.index(
            "--resolve zlstreet.xyz:443:127.0.0.1"
        )
        timer_start = self.deploy.index(
            "systemctl start kol-collect-kol.timer",
            proxy_check,
        )
        self.assertGreater(timer_start, proxy_check)

    def test_reading_bundle_is_an_explicit_runtime_allowlist(self) -> None:
        runtime = re.search(r'cp "\$READING_SOURCE"/\{([^}]+)\}', self.deploy)
        assets = re.search(r'cp "\$READING_SOURCE/web"/\{([^}]+)\}', self.deploy)
        self.assertIsNotNone(runtime)
        self.assertIsNotNone(assets)
        self.assertEqual(set(runtime.group(1).split(",")), {
            "poc.py", "wechat.py", "reading_jobs.py", "schedule_policy.py",
            "inference.py", "official_transcripts.py", "detailed_summary.py", "wechat_sources.json",
        })
        self.assertEqual(set(assets.group(1).split(",")), {
            "index.html", "wechat.html", "app.js", "wechat.js", "style.css",
            "wechat.css", "reader-auth.js", "reader-auth.css",
        })
        bundle = self.deploy[self.deploy.index('READING_SOURCE='):self.deploy.index('COPYFILE_DISABLE=1 tar')]
        self.assertNotIn('cp -R', bundle)
        self.assertNotIn('configure_deepseek.py', bundle)
        self.assertNotIn('reader_control.py', bundle)
        self.assertNotIn('deepseek-credentials.json', bundle)
        for name in assets.group(1).split(","):
            self.assertTrue((ROOT.parent / "research/podcast_poc/web" / name).is_file(), name)

    def test_reading_worker_has_private_data_and_secrets_only_in_background_units(self) -> None:
        start = self.deploy.index('cat > "$REMOTE_STAGE/kol-reading-worker.service"')
        end = self.deploy.index('\nUNIT\n', start)
        worker = self.deploy[start:end]
        for required in (
            'User=kol-dashboard', 'Group=kol-dashboard',
            'WorkingDirectory=/opt/kol-dashboard/current',
            'ExecStart=/usr/bin/python3 /opt/kol-dashboard/current/reading_worker.py',
            'Environment=KOL_READING_DATA_DIR=/opt/kol-dashboard/private/reading',
            'Environment=KOL_READING_ENABLED=1',
            'Environment=KOL_DASHBOARD_DB=/opt/kol-dashboard/data/kol_dashboard.db',
            'EnvironmentFile=-/etc/kol-dashboard/deepseek.env',
            'LimitCORE=0', 'UMask=0077', 'ProtectSystem=strict',
            'ReadWritePaths=/opt/kol-dashboard/private/reading /var/log/kol-dashboard',
        ):
            self.assertIn(required, worker)
        self.assertNotIn('EnvironmentFile=-/etc/kol-dashboard.env', worker)
        self.assertNotIn('kol-reading-worker.timer', self.deploy)
        without_worker = self.deploy[:start] + self.deploy[end:]
        news_start = without_worker.index('cat > "$REMOTE_STAGE/kol-enrich-news.service"')
        news_end = without_worker.index('\nUNIT\n', news_start)
        without_worker = without_worker[:news_start] + without_worker[news_end:]
        without_workers = without_worker.replace(
            'EXTRA_ENVIRONMENT="EnvironmentFile=-/etc/kol-dashboard/deepseek.env"', '')
        self.assertNotIn('EnvironmentFile=-/etc/kol-dashboard/deepseek.env', without_workers)
        web_start = self.deploy.index('cat > "$REMOTE_STAGE/kol-dashboard.service"')
        web_end = self.deploy.index('\nUNIT\n', web_start)
        web = self.deploy[web_start:web_end]
        self.assertIn('Environment=KOL_READING_DATA_DIR=/opt/kol-dashboard/private/reading', web)
        self.assertNotIn('deepseek.env', web)

    def test_reading_worker_participates_in_every_stop_and_restore_boundary(self) -> None:
        for name in ('prepare_unit_state_rollback', 'restore_unit_states', 'rollback_configuration', 'cleanup_remote'):
            start = self.deploy.index(name + '() {')
            end = self.deploy.index('\n}\n', start)
            self.assertIn('kol-reading-worker.service', self.deploy[start:end], name)
        record = self.deploy.index('backup_path /etc/systemd/system/kol-reading-worker.service')
        ready = self.deploy.index('\nROLLBACK_READY=1\n', record)
        self.assertIn('kol-reading-worker.service; do', self.deploy[record:ready])
        stopping = self.deploy.index('systemctl stop kol-enrich-wakeup.path', ready)
        stopped = self.deploy.index('\nSERVICES_STOPPED=1\n', stopping)
        stop = self.deploy[stopping:stopped]
        self.assertEqual(stop.count('kol-dashboard.service kol-reading-worker.service; do'), 2)
        self.assertIn('systemctl show --property=LoadState --value', stop)
        self.assertIn('systemctl show --property=ActiveState --value', stop)
        self.assertIn('"$unit_state" != inactive && "$unit_state" != failed', stop)
        self.assertIn('"$unit_load_state" == not-found', stop)
        backup = self.deploy.index('\nbackup_reading_queue\n', stopped)
        self.assertGreater(backup, stopped)
        rollback = self.deploy.index('cleanup_remote() {')
        guard = self.deploy.index('if [[ $rollback_safe == 1 ]]; then', rollback)
        restore = self.deploy.index('rollback_reading_queue || rollback_failed=1', guard)
        self.assertGreater(restore, guard)
        self.assertIn('systemctl is-active --quiet kol-reading-worker.service || rollback_failed=1', self.deploy)

    def test_reading_preflight_happens_before_stopping_existing_services(self) -> None:
        preflight = self.deploy.index("/usr/bin/python3 -c 'import bs4, reading_service'")
        stop = self.deploy.index('systemctl stop kol-enrich-wakeup.path')
        self.assertLess(preflight, stop)
        self.assertIn('beautifulsoup4>=4.12,<5', (ROOT.parent / 'requirements.txt').read_text())
        self.assertNotIn('pip install', self.deploy)
        self.assertNotIn('pip3 install', self.deploy)
        self.assertIn('install -d -o kol-dashboard -g kol-dashboard -m 700 "$reading_path"', self.deploy)

    def test_optional_reading_seed_is_imported_only_inside_stopped_release_transaction(self) -> None:
        self.assertIn('READING_SEED_ARCHIVE="${KOL_READING_SEED_ARCHIVE:-}"', self.deploy)
        self.assertIn('"$VPS" put "$READING_SEED_ARCHIVE" "$REMOTE_STAGE/reading-seed.tgz"', self.deploy)
        self.assertIn('! -L "$READING_SEED_ARCHIVE"', self.deploy)
        bundle = self.deploy[self.deploy.index('mkdir -p "$WORK/pkg/lib"'):self.deploy.index('echo "→ 创建远端私有暂存区"')]
        self.assertNotIn('reading-seed.tgz', bundle)
        self.assertNotIn('"$READING_SEED_ARCHIVE"', bundle)
        stopped = self.deploy.index('\nSERVICES_STOPPED=1\n')
        backup = self.deploy.index('\nbackup_reading_queue\n', stopped)
        importer = self.deploy.index('PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "$RELEASE_DIR/reading_import.py"', backup)
        switch = self.deploy.index('ln -s "$RELEASE_DIR" "$CURRENT_NEXT"', importer)
        start_worker = self.deploy.index('\nsystemctl start kol-reading-worker.service\n', switch)
        self.assertLess(stopped, backup)
        self.assertLess(backup, importer)
        self.assertLess(importer, switch)
        self.assertLess(switch, start_worker)
        self.assertIn('--archive "$REMOTE_STAGE/reading-seed.tgz"', self.deploy[importer:switch])
        self.assertIn('--data-dir "$READING_DATA_DIR" --owner kol-dashboard', self.deploy[importer:switch])

    def test_reading_queue_backup_captures_committed_wal_rows(self) -> None:
        function = self.deploy.index('backup_reading_queue() {')
        start = self.deploy.index("<<'PY'\n", function) + len("<<'PY'\n")
        end = self.deploy.index('\nPY\n', start)
        program = self.deploy[start:end]
        maintenance = self.deploy.index('reading_database_integrity() {')
        check_start = self.deploy.index("<<'PY'\n", maintenance) + len("<<'PY'\n")
        check_end = self.deploy.index('\nPY\n', check_start)
        checkpoint = self.deploy[check_start:check_end]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'reading source.sqlite3'
            target = Path(directory) / 'before-release.sqlite3'
            connection = sqlite3.connect(source)
            self.addCleanup(connection.close)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)')
            connection.execute("INSERT INTO jobs VALUES ('keep-me', 'queued')")
            connection.commit()
            self.assertTrue(Path(str(source) + '-wal').exists())
            self.assertGreater(Path(str(source) + '-wal').stat().st_size, 0)
            result = subprocess.run([sys.executable, '-', str(source), 'checkpoint'],
                                    input=checkpoint, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            connection.close()
            # All writers are stopped and checkpointed. Root's snapshot read
            # must not recreate sidecars under its different service identity.
            for suffix in ('-wal', '-shm'):
                Path(str(source) + suffix).unlink(missing_ok=True)
            result = subprocess.run([sys.executable, '-', str(source), str(target)],
                                    input=program, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(target) as saved:
                self.assertEqual(saved.execute('SELECT * FROM jobs').fetchall(), [('keep-me', 'queued')])
                self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                self.assertEqual(saved.execute('PRAGMA journal_mode').fetchone(), ('delete',))
            for path in (source, target):
                for suffix in ('-wal', '-shm'):
                    self.assertFalse(Path(str(path) + suffix).exists())
            self.assertFalse(target.with_name(target.name + '.next').exists())
            connection.close()

    def test_reading_live_integrity_and_checkpoint_use_service_identity(self) -> None:
        start = self.deploy.index('reading_database_integrity() {')
        end = self.deploy.index('\n}\n', start)
        integrity = self.deploy[start:end]
        self.assertIn('runuser -u kol-dashboard -- /usr/bin/python3', integrity)
        self.assertIn('info.st_uid != os.geteuid()', integrity)
        self.assertNotIn('immutable=1', integrity)
        backup_start = self.deploy.index('backup_reading_queue() {')
        backup_end = self.deploy.index('\n}\n', backup_start)
        backup = self.deploy[backup_start:backup_end]
        self.assertLess(backup.index('reading_database_integrity "$READING_DB_PATH" checkpoint'),
                        backup.index('source = sqlite3.connect'))
        self.assertIn('mode=ro&immutable=1', backup)
        self.assertNotRegex(self.deploy, r'(?m)^\s+database_integrity "\$READING_DB_(?:NEXT|PATH)"')
        self.assertIn('\nreading_database_integrity "$READING_DB_PATH"\n', self.deploy)

    def test_reading_checkpoint_fails_closed_while_a_writer_is_active(self) -> None:
        function = self.deploy.index('reading_database_integrity() {')
        start = self.deploy.index("<<'PY'\n", function) + len("<<'PY'\n")
        end = self.deploy.index('\nPY\n', start)
        # Only shorten SQLite's busy wait in this contention fixture.
        program = self.deploy[start:end].replace('timeout=30', 'timeout=0.05')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'blocked.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('PRAGMA journal_mode=WAL').fetchone()
                connection.execute('CREATE TABLE jobs (id TEXT)')
                connection.commit()
                connection.execute('BEGIN IMMEDIATE')
                connection.execute("INSERT INTO jobs VALUES ('uncommitted')")
                result = subprocess.run([sys.executable, '-', str(path), 'checkpoint'],
                    input=program, text=True, capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('checkpoint did not finish', result.stderr)
                connection.rollback()

    def test_reading_queue_rollback_restores_old_queue_and_keeps_first_install_data(self) -> None:
        start = self.deploy.index('rollback_reading_queue() {')
        end = self.deploy.index('\n}\n', start) + 3
        function = self.deploy[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollback = root / 'rollback'; rollback.mkdir()
            live = root / 'live queue.sqlite3'
            saved = rollback / 'reading-queue.before-release'
            with sqlite3.connect(saved) as connection:
                connection.execute('CREATE TABLE jobs (id TEXT)')
                connection.execute("INSERT INTO jobs VALUES ('before-release')")
            live.write_bytes(b'changed')
            Path(str(live) + '-wal').write_bytes(b'changed-wal')
            Path(str(live) + '-shm').write_bytes(b'changed-shm')
            articles = root / 'article.json'; articles.write_text('preserved')
            setup = '\n'.join([
                'set -euo pipefail', 'READING_DB_ROLLBACK_READY=1',
                'ROLLBACK_DIR=' + shlex.quote(str(rollback)),
                'READING_DB_PATH=' + shlex.quote(str(live)),
                'READING_DB_NEXT=' + shlex.quote(str(live) + '.next'),
                'chown() { :; }', 'database_integrity() { test -s "$1"; }',
                'reading_database_integrity() { test -s "$1"; }',
                function, 'rollback_reading_queue',
            ])
            result = subprocess.run(['bash'], input=setup, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(live) as restored:
                self.assertEqual(restored.execute('SELECT id FROM jobs').fetchall(), [('before-release',)])
            self.assertEqual(live.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(str(live) + '-wal').exists())
            self.assertFalse(Path(str(live) + '-shm').exists())
            saved.unlink()
            live.write_bytes(b'newly-created-queue-must-remain')
            result = subprocess.run(['bash'], input=setup, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(live.read_bytes(), b'newly-created-queue-must-remain')
            self.assertEqual(articles.read_text(), 'preserved')

    def test_reading_acceptance_rejects_stale_or_wrong_process_heartbeat(self) -> None:
        function = self.deploy.index('validate_reading_worker() {')
        start = self.deploy.index("<<'PY'\n", function) + len("<<'PY'\n")
        end = self.deploy.index('\nPY\n', start)
        program = self.deploy[start:end]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'heartbeat.json'
            valid = {'state': 'running', 'pid': 123,
                     'updated_at': datetime.now(timezone.utc).isoformat()}
            for changes, expected in (({}, 0), ({'state': 'stopped'}, 1), ({'pid': 999}, 1),
                                      ({'updated_at': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}, 1),
                                      ({'updated_at': 'not a timestamp'}, 1)):
                with self.subTest(changes=changes):
                    path.write_text(json.dumps({**valid, **changes}))
                    result = subprocess.run([sys.executable, '-', str(path), '123'],
                        input=program, text=True, capture_output=True, check=False)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_reading_worker_acceptance_and_durable_backup_precede_commit(self) -> None:
        start = self.deploy.index('\nsystemctl start kol-reading-worker.service\n')
        acceptance = self.deploy.index('[[ "$READING_WORKER_HEALTH" == ok ]]', start)
        integrity = self.deploy.index('reading_database_integrity "$READING_DB_PATH"', acceptance)
        backup = self.deploy.index('DURABLE_READING_BACKUP=', integrity)
        committed = self.deploy.index('COMMITTED=1', backup)
        self.assertLess(start, acceptance)
        self.assertLess(acceptance, integrity)
        self.assertLess(integrity, backup)
        self.assertLess(backup, committed)
        block = self.deploy[backup:committed]
        self.assertIn('"$ROLLBACK_DIR/reading-queue.before-release"', block)
        self.assertIn('install -o root -g root -m 600', block)
        self.assertIn('database_integrity "$DURABLE_READING_BACKUP"', block)
        self.assertIn('os.fsync(directory_fd)', block)


if __name__ == "__main__":
    unittest.main()
