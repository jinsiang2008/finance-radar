from __future__ import annotations

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
            "content_quality.py",
            "db.py",
            "decision_collect.py",
            "decision_service.py",
            "decision_snapshot.py",
            "market_data.py",
            "portfolio.py",
            "relation_engine.py",
            "macro_collect.py",
            "llm_enrichment.py",
            "enrichment_collect.py",
            "collect.sh",
        ):
            self.assertIn(filename, self.deploy)

    def test_deploy_bundle_omits_macos_appledouble_metadata(self) -> None:
        self.assertIn("COPYFILE_DISABLE=1 tar czf", self.deploy)

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
        self.assertIn('if [[ "$job" == "enrich" ]]; then', self.deploy)
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
        self.assertIn(
            "kol-collect-enrich.timer kol-enrich-wakeup.path; do",
            self.deploy,
        )

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
