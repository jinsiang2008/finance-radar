#!/bin/bash
# Scheduled collection for the dashboard.
#   collect.sh kol     — scan every KOL into the events table
#   collect.sh macro   — store one macro risk snapshot
#   collect.sh decision — refresh relations, market checks, and portfolio
#   collect.sh enrich  — add cached Chinese intelligence with DeepSeek
#   collect.sh daily   — collect HN/AI discovery feeds and import one Daily snapshot
#
# Jobs are idempotent; the DB dedups repeat sightings.

set -euo pipefail
umask 077

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DIR/.." && pwd)"
DATA_DIR="${KOL_DATA_DIR:-$REPO_DIR/data}"
LIB_DIR="${KOL_LIB_DIR:-$REPO_DIR/lib}"
if [[ -d "$DIR/lib" ]]; then
  LIB_DIR="$DIR/lib"
fi
export KOL_DASHBOARD_DB="${KOL_DASHBOARD_DB:-$DATA_DIR/kol_dashboard.db}"
export KOL_DB_WRITE_REQUIRED=1
export KOL_DASHBOARD_DIR="$DIR"
export KOL_LIB_DIR="$LIB_DIR"
export KOL_SCRIPTS_DIR="$LIB_DIR"
export SERENITY_CACHE_DIR="${SERENITY_CACHE_DIR:-$DATA_DIR/serenity}"
LOG_DIR="${KOL_LOG_DIR:-$REPO_DIR/logs}"
mkdir -p "$(dirname "$KOL_DASHBOARD_DB")" "$LOG_DIR"
ERROR_LOG="$LOG_DIR/collect.err.log"
ENRICHMENT_MARKER="${KOL_ENRICH_WAKE_PATH:-$(dirname "$KOL_DASHBOARD_DB")/enrichment.pending}"
DAILY_SNAPSHOT="${KOL_DAILY_SNAPSHOT_PATH:-$DATA_DIR/daily-briefing-latest.json}"

TRACKER="$LIB_DIR/kol_tracker.py"
[[ -f "$TRACKER" ]] || {
  echo "missing collector: $TRACKER" >&2
  exit 1
}

stamp() { TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S'; }

signal_enrichment() {
  touch "$ENRICHMENT_MARKER"
}

on_error() {
  local rc=$?
  trap - ERR
  printf '[%s] %s collection failed (exit=%s)\n' \
    "$(stamp)" "${1:-unknown}" "$rc" | tee -a "$ERROR_LOG" >&2
  exit "$rc"
}
trap 'on_error "${1:-unknown}"' ERR

run_capture() {
  local tmp rc output
  tmp="$(mktemp)"
  if "$@" >"$tmp" 2> >(tee -a "$ERROR_LOG" >&2); then
    output="$(<"$tmp")"
    rm -f "$tmp"
    printf '%s' "$output"
    return 0
  else
    rc=$?
  fi
  output="$(<"$tmp")"
  rm -f "$tmp"
  [[ -z "$output" ]] || printf '%s\n' "$output" | tee -a "$ERROR_LOG" >&2
  return "$rc"
}

case "${1:-kol}" in
  kol)
    OUT=$(run_capture python3 "$TRACKER" collect 6)
    RELATIONS=$(run_capture python3 "$DIR/decision_collect.py" relations)
    signal_enrichment
    OUT="$OUT; $RELATIONS"
    echo "[$(stamp)] kol: $OUT" >> "$LOG_DIR/collect.log"
    ;;
  macro)
    OUT=$(run_capture python3 "$DIR/macro_collect.py")
    RELATIONS=$(run_capture python3 "$DIR/decision_collect.py" relations)
    signal_enrichment
    OUT="$OUT; $RELATIONS"
    echo "[$(stamp)] macro: $OUT" >> "$LOG_DIR/collect.log"
    ;;
  decision)
    OUT=$(run_capture python3 "$DIR/decision_collect.py" all)
    echo "[$(stamp)] decision: $OUT" >> "$LOG_DIR/collect.log"
    ;;
  daily)
    OUT=$(run_capture python3 "$DIR/briefing_collect.py" \
      --output "$DAILY_SNAPSHOT" --import --db "$KOL_DASHBOARD_DB")
    [[ -n "$OUT" ]] || OUT="Daily snapshot imported"
    echo "[$(stamp)] daily: $OUT" >> "$LOG_DIR/collect.log"
    ;;
  enrich)
    OUT=$(run_capture python3 "$DIR/enrichment_collect.py")
    echo "[$(stamp)] enrich: $OUT" >> "$LOG_DIR/collect.log"
    ;;
  *)
    echo "usage: collect.sh {kol|macro|decision|daily|enrich}" >&2
    exit 2
    ;;
esac

# Keep the log from growing without bound.
tail -n 2000 "$LOG_DIR/collect.log" > "$LOG_DIR/collect.log.tmp" 2>/dev/null &&
  mv "$LOG_DIR/collect.log.tmp" "$LOG_DIR/collect.log"
