#!/bin/bash
# KOL Dashboard launcher
# - binds 127.0.0.1:8088 (exposed via tailscale serve :8444)
# - stores local runtime state under the repository root

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DIR/.." && pwd)"
cd "$DIR"

export KOL_DASHBOARD_DB="${KOL_DASHBOARD_DB:-$REPO_DIR/data/kol_dashboard.db}"
export KOL_DASHBOARD_HOLDINGS_FILE="${KOL_DASHBOARD_HOLDINGS_FILE:-$REPO_DIR/private/holdings.md}"
export KOL_DASHBOARD_PORT="${KOL_DASHBOARD_PORT:-8088}"
export KOL_DASHBOARD_HOST="${KOL_DASHBOARD_HOST:-127.0.0.1}"

LOG_DIR="${KOL_LOG_DIR:-$REPO_DIR/logs}"
mkdir -p "$(dirname "$KOL_DASHBOARD_DB")" "$LOG_DIR" "$REPO_DIR/private"

# kill any stale instance on the same port
STALE=$(lsof -ti :"$KOL_DASHBOARD_PORT" 2>/dev/null || true)
if [[ -n "$STALE" ]]; then
  echo "[run.sh] killing stale pid(s) on :$KOL_DASHBOARD_PORT: $STALE"
  kill -TERM $STALE 2>/dev/null || true
  sleep 1
fi

echo "[run.sh] starting KOL dashboard on 127.0.0.1:$KOL_DASHBOARD_PORT"
exec python3 "$DIR/app.py"
