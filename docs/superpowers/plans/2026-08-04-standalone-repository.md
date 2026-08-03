# Finance Radar Standalone Repository Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create and push a self-contained, secret-free `finance-radar` repository whose tests and deployment bundle no longer depend on an external workspace.

**Architecture:** Keep the application as the `kol_dashboard` Python package and vendor the four collectors under root-level `lib/`. Runtime state lives in ignored root-level `data/`, `logs/`, `private/`, and `.cache/` directories; environment variables continue to override every production path.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, SQLite, vanilla JavaScript/CSS, Bash, unittest, GitHub.

## Global Constraints

- Never copy the production SQLite database, logs, real holdings, Serenity cache, `.env`, passcode hash, session secret, SSH key, or deployment payload.
- Preserve strict publication-time filtering and public/private API isolation.
- Do not add a license without an explicit user choice.
- Push only to `https://github.com/jinsiang2008/finance-radar.git` on `main`.

---

### Task 1: Create the isolated repository and dependency contract

**Files:**
- Create: `<workspace>/finance-radar/`
- Create: `<workspace>/finance-radar/tests/test_repository_contract.py`
- Copy: `<source-workspace>/kol_dashboard/**`
- Copy: `<source-workspace>/cron/lib/{kol_tracker,macro_fetcher,risk_radar}.py`
- Copy: `<source-workspace>/scripts/serenity_tracker.py`

**Interfaces:**
- Produces: root package `kol_dashboard` and collector directory `lib`.
- Consumes: only the source paths listed above.

- [ ] **Step 1: Write the failing repository contract**

```python
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_collectors_are_vendored(self):
        for name in (
            "kol_tracker.py",
            "macro_fetcher.py",
            "risk_radar.py",
            "serenity_tracker.py",
        ):
            self.assertTrue((ROOT / "lib" / name).is_file())

    def test_runtime_files_use_no_clawd_default(self):
        for path in (
            ROOT / "kol_dashboard" / "collect.sh",
            ROOT / "kol_dashboard" / "run.sh",
            ROOT / "kol_dashboard" / "deploy.sh",
            ROOT / "kol_dashboard" / "portfolio.py",
        ):
            self.assertNotIn("/clawd/", path.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the contract before copying collectors**

Run: `cd <workspace>/finance-radar && python3 -m unittest tests.test_repository_contract -v`

Expected: FAIL because `lib/` files are absent and paths still reference `clawd`.

- [ ] **Step 3: Copy only approved source files**

Create the package, `lib/`, template, static, tests, and docs directories. Do not copy `data`, `logs`, `memory`, `.env`, caches, or database files.

- [ ] **Step 4: Re-run the repository contract**

Expected: collector assertions pass; relative-path assertions remain failing until Task 2.

### Task 2: Make local runtime and tests repository-relative

**Files:**
- Modify: `kol_dashboard/db.py`
- Modify: `kol_dashboard/portfolio.py`
- Modify: `kol_dashboard/collect.sh`
- Modify: `kol_dashboard/run.sh`
- Modify: `kol_dashboard/deploy.sh`
- Modify: `kol_dashboard/macro_collect.py`
- Modify: `lib/kol_tracker.py`
- Modify: `lib/serenity_tracker.py`
- Modify: `kol_dashboard/tests/test_kol_tracker.py`
- Modify: `kol_dashboard/tests/test_risk_radar.py`

**Interfaces:**
- Consumes: repository root and environment overrides.
- Produces: defaults under `data/`, `logs/`, `private/`, `.cache/serenity`.

- [ ] **Step 1: Set shell paths from the repository**

Use:

```bash
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$APP_DIR/.." && pwd)"
LIB_DIR="${KOL_LIB_DIR:-$REPO_DIR/lib}"
DATA_DIR="${KOL_DATA_DIR:-$REPO_DIR/data}"
LOG_DIR="${KOL_LOG_DIR:-$REPO_DIR/logs}"
```

The deployed bundle still resolves `$APP_DIR/lib` first.

- [ ] **Step 2: Set Python defaults from `__file__`**

Use `KOL_DASHBOARD_DB` and `KOL_DASHBOARD_HOLDINGS_FILE` when set; otherwise resolve under the repository package’s local runtime directories. No absolute user path is allowed.

- [ ] **Step 3: Point collector imports and tests at root `lib/`**

Tests must use:

```python
ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "lib"
```

Collector fallback paths must use their own file location or explicit environment variables.

- [ ] **Step 4: Run focused path and collector tests**

Run:

```bash
python3 -m unittest \
  tests.test_repository_contract \
  kol_dashboard.tests.test_kol_tracker \
  kol_dashboard.tests.test_risk_radar -v
```

Expected: all pass.

### Task 3: Add repository hygiene and operating documentation

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `requirements.txt`
- Create: `private/holdings.example.md`
- Create: `kol_dashboard/__init__.py`
- Preserve: `docs/superpowers/specs/2026-08-04-standalone-repository-design.md`
- Preserve: `docs/superpowers/plans/2026-08-04-standalone-repository.md`

**Interfaces:**
- Produces: documented install, test, run, collection, authentication, and deployment commands.

- [ ] **Step 1: Ignore all runtime and secret material**

`.gitignore` must cover:

```gitignore
__pycache__/
*.py[cod]
.env
.env.*
*.db
*.db-wal
*.db-shm
data/*
logs/*
private/*
!private/holdings.example.md
.cache/
```

- [ ] **Step 2: Document setup and runtime**

README commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s kol_dashboard/tests -v
./kol_dashboard/run.sh
```

Document strict publication-time quarantine, public/private separation, `--auth`, and the rule that `--db` is opt-in.

- [ ] **Step 3: Add dependencies**

`requirements.txt` contains `fastapi`, `uvicorn`, and `httpx`.

- [ ] **Step 4: Add a sanitized holdings example**

Use fictional accounts and positions only; do not derive values from the real holdings file.

### Task 4: Verify, commit, and push

**Files:**
- Verify: all tracked files
- Create: Git metadata and initial commits

**Interfaces:**
- Produces: public GitHub `main` branch.

- [ ] **Step 1: Run complete verification**

```bash
python3 -m unittest discover -s kol_dashboard/tests -v
python3 -m unittest tests.test_repository_contract -v
python3 -m compileall -q kol_dashboard lib
node --check kol_dashboard/static/app.js
bash -n kol_dashboard/collect.sh
bash -n kol_dashboard/run.sh
bash -n kol_dashboard/deploy.sh
```

Expected: zero failures and zero syntax errors.

- [ ] **Step 2: Scan staged content for secrets**

Search tracked candidates for `SESSION_SECRET=`, `PASSCODE_HASH=`, private key headers, user-specific absolute paths, real holdings paths, `.db`, and `.env`. Only variable names and documentation placeholders may remain.

- [ ] **Step 3: Initialize and inspect Git**

Run `git init`, set branch to `main`, add only repository files, inspect `git status`, `git diff --cached`, and recent log format.

- [ ] **Step 4: Commit with conventional messages**

Create focused commits for repository extraction and documentation without bypassing hooks.

- [ ] **Step 5: Add and verify origin**

```bash
git remote add origin https://github.com/jinsiang2008/finance-radar.git
git branch -M main
git remote -v
```

- [ ] **Step 6: Push**

Run: `git push -u origin main`

Expected: GitHub reports `main` tracking `origin/main`; verify with `gh repo view` and `git status`.
