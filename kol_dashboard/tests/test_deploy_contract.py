from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
            "collect.sh",
        ):
            self.assertIn(filename, self.deploy)

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
        self.assertIn("TimeoutStartSec=20min", self.deploy)

    def test_decision_collection_is_scheduled_and_fail_fast(self) -> None:
        self.assertIn("set -euo pipefail", self.collect)
        self.assertIn('decision_collect.py" relations', self.collect)
        self.assertIn('decision_collect.py" all', self.collect)
        self.assertIn("kol-collect-decision.timer", self.deploy)
        self.assertIn("python3 -c 'import db; db.init()'", self.deploy)
        self.assertIn("KOL_DB_WRITE_REQUIRED=1", self.collect)

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
            '("Hacker News", "hn_story")',
            '("AI Digest", "ai_digest")',
            '("AI Brief", "paper_digest")',
            'payload.get("refresh_schedule_status") not in {"configured", "active"}',
            'payload.get("next_refresh_at")',
            "database_integrity \"$DB_PATH\"",
            "https://zlstreet.xyz/kol/api/briefings/latest",
        ):
            self.assertIn(required, self.deploy)

    def test_daily_units_are_quiesced_enabled_and_rollback_safe(self) -> None:
        self.assertGreaterEqual(
            self.deploy.count("for job in kol macro decision daily enrich; do"),
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
            1,
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
        self.assertIn("kol-enrich-wakeup.path; do", self.deploy)

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
            "kol-collect-kol.service",
            "kol-collect-macro.service",
            "kol-collect-decision.service",
            "kol-collect-daily.service",
            "kol-collect-enrich.service",
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


if __name__ == "__main__":
    unittest.main()
