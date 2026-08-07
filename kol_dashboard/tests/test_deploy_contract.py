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
