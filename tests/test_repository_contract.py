from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_collectors_are_vendored(self) -> None:
        for name in (
            "kol_tracker.py",
            "macro_fetcher.py",
            "risk_radar.py",
            "serenity_tracker.py",
        ):
            self.assertTrue((ROOT / "lib" / name).is_file(), name)

    def test_runtime_files_use_no_clawd_or_user_specific_default(self) -> None:
        for relative_path in (
            "kol_dashboard/collect.sh",
            "kol_dashboard/run.sh",
            "kol_dashboard/deploy.sh",
            "kol_dashboard/db.py",
            "kol_dashboard/portfolio.py",
            "kol_dashboard/macro_collect.py",
            "lib/kol_tracker.py",
            "lib/serenity_tracker.py",
            "kol_dashboard/tests/test_kol_tracker.py",
            "kol_dashboard/tests/test_risk_radar.py",
        ):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertNotIn("/clawd/", content)
                self.assertNotIn("/Users/", content)

    def test_private_runtime_artifacts_are_absent(self) -> None:
        forbidden_suffixes = (".db", ".db-wal", ".db-shm", ".env")
        for path in ROOT.rglob("*"):
            if ".git" in path.parts:
                continue
            with self.subTest(path=path):
                self.assertFalse(path.name.endswith(forbidden_suffixes))


if __name__ == "__main__":
    unittest.main()
