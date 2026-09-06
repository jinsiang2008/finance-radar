#!/usr/bin/env bash
# Deploy the dashboard to zlstreet.xyz/kol.
#
#   ./deploy.sh          — atomic code release; preserve remote data and auth
#   ./deploy.sh --db     — also replace remote DB from a consistent local backup
#   ./deploy.sh --auth   — explicitly rotate private-mode passcode and sessions
#
# Transfer goes through scripts/vps.sh because this host requires PTY-backed SSH
# and rejects scp/rsync. Every payload is uploaded into a root-only unique stage.
set -euo pipefail
umask 077

# Non-interactive auth may supply the passcode through this shell's environment.
# Capture it before any child process can inherit it, make the copy explicitly
# non-exported, then remove the original environment entry immediately.
CAPTURED_PASSCODE="${KOL_DASHBOARD_PASSCODE-}"
export -n CAPTURED_PASSCODE
unset KOL_DASHBOARD_PASSCODE

clear_auth_material() {
  unset CAPTURED_PASSCODE PASSCODE PASSCODE_CONFIRM PASSCODE_HASH SESSION_SECRET
}

cleanup_auth_only() {
  local rc=$?
  trap - EXIT INT TERM
  clear_auth_material
  exit "$rc"
}
trap cleanup_auth_only EXIT INT TERM

# A production-size SQLite backup plus decision snapshot prewarm can exceed the
# helper's generic 60-second SSH ceiling. Callers may raise this further.
export RSH_TIMEOUT="${RSH_TIMEOUT:-1200}"

VPS="${VPS_HELPER:-}"
if [[ -z "$VPS" ]]; then
  VPS="$(command -v zlstreet-vps || true)"
fi
[[ -x "$VPS" ]] || {
  echo "缺少远程操作 helper；请安装 zlstreet-vps 或设置 VPS_HELPER" >&2
  exit 1
}

LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$LOCAL_DIR/.." && pwd)"
LIB_DIR="${KOL_LIB_DIR:-$REPO_DIR/lib}"
DB="${KOL_DASHBOARD_DB:-$REPO_DIR/data/kol_dashboard.db}"
SEND_DB=0
CONFIGURE_AUTH=0
for arg in "$@"; do
  case "$arg" in
    --db) SEND_DB=1 ;;
    --auth) CONFIGURE_AUTH=1 ;;
    *)
      echo "usage: deploy.sh [--db] [--auth]" >&2
      exit 2
      ;;
  esac
done

WORK="$(mktemp -d)"
DEPLOY_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
REMOTE_STAGE="/opt/kol-dashboard/.staging/$DEPLOY_ID"
REMOTE_STAGE_CREATED=0

cleanup_local() {
  local rc=$?
  trap - EXIT INT TERM
  clear_auth_material
  rm -rf "$WORK"
  if [[ $REMOTE_STAGE_CREATED == 1 ]]; then
    "$VPS" run \
      "if [ -f '$REMOTE_STAGE/PRESERVE' ]; then printf '远端恢复材料已保留: %s\\n' '$REMOTE_STAGE' >&2; else rm -rf '$REMOTE_STAGE'; fi" \
      >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup_local EXIT INT TERM

echo "→ 打包应用与采集器"
mkdir -p "$WORK/pkg/lib"
cp "$LOCAL_DIR"/{app.py,auth.py,briefing_collect.py,briefing_import.py,briefing_service.py,briefing_topics.py,daily_enrichment.py,content_quality.py,event_relevance.py,db.py,decision_collect.py,decision_service.py,decision_snapshot.py,market_data.py,portfolio.py,relation_engine.py,macro_alert_service.py,macro_collect.py,llm_enrichment.py,enrichment_collect.py,options_policy.py,options_research_service.py,options_strategy.py,collect.sh} "$WORK/pkg/"
cp -R "$LOCAL_DIR"/templates "$LOCAL_DIR"/static "$WORK/pkg/"
cp "$LIB_DIR"/{kol_tracker.py,macro_fetcher.py,risk_radar.py} "$WORK/pkg/lib/"
cp "$LIB_DIR/serenity_tracker.py" "$WORK/pkg/lib/"
# macOS tar otherwise serializes extended attributes as AppleDouble `._*`
# files, which Linux compileall mistakes for Python source files.
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$WORK/app.tgz" \
  --exclude='__pycache__' --exclude='*.pyc' -C "$WORK/pkg" .

echo "→ 创建远端私有暂存区"
"$VPS" run "install -d -m 700 '$REMOTE_STAGE'"
REMOTE_STAGE_CREATED=1
"$VPS" put "$WORK/app.tgz" "$REMOTE_STAGE/app.tgz"

if [[ $CONFIGURE_AUTH == 0 && -n "$CAPTURED_PASSCODE" ]]; then
  echo "⚠ 已忽略 KOL_DASHBOARD_PASSCODE；轮换认证必须显式传入 --auth" >&2
fi

if [[ $CONFIGURE_AUTH == 1 ]]; then
  PASSCODE="$CAPTURED_PASSCODE"
  if [[ -z "$PASSCODE" ]]; then
    read -r -s -p "私人模式新口令: " PASSCODE
    echo
    read -r -s -p "再次输入口令: " PASSCODE_CONFIRM
    echo
    [[ "$PASSCODE" == "$PASSCODE_CONFIRM" ]] || {
      echo "两次口令不一致" >&2
      exit 1
    }
  fi
  PASSCODE_HASH="$(
    printf '%s' "$PASSCODE" |
      PYTHONPATH="$LOCAL_DIR" python3 -c \
        'import sys, auth; print(auth.hash_passcode(sys.stdin.read()))'
  )"
  unset CAPTURED_PASSCODE PASSCODE PASSCODE_CONFIRM
  SESSION_SECRET="${KOL_DASHBOARD_SESSION_SECRET:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')}"
  [[ "$SESSION_SECRET" =~ ^[A-Za-z0-9_-]{32,}$ ]] || {
    echo "KOL_DASHBOARD_SESSION_SECRET 必须是至少 32 位 URL-safe 字符" >&2
    exit 1
  }
  cat > "$WORK/auth.env" <<AUTH
KOL_DASHBOARD_PASSCODE_HASH=$PASSCODE_HASH
KOL_DASHBOARD_SESSION_SECRET=$SESSION_SECRET
KOL_DASHBOARD_SESSION_TTL_SECONDS=28800
KOL_DASHBOARD_COOKIE_PATH=/kol
KOL_DASHBOARD_COOKIE_SECURE=true
KOL_DASHBOARD_HOLDINGS_FILE=/opt/kol-dashboard/private/holdings.md
AUTH
  chmod 600 "$WORK/auth.env"
  unset PASSCODE_HASH SESSION_SECRET
  "$VPS" put-secret "$WORK/auth.env" "$REMOTE_STAGE/auth.env"
else
  unset CAPTURED_PASSCODE
fi

if [[ $SEND_DB == 1 ]]; then
  [[ -f "$DB" ]] || { echo "本地数据库不存在: $DB" >&2; exit 1; }
  echo "→ 创建 SQLite 一致性备份并上传（将覆盖远端数据）"
  python3 - "$DB" "$WORK/kol_dashboard.db" <<'PY'
from pathlib import Path
from urllib.parse import quote
import sqlite3
import sys

source_path, destination_path = map(Path, sys.argv[1:])
source_uri = "file:" + quote(str(source_path.resolve()), safe="/") + "?mode=ro"
source = sqlite3.connect(source_uri, uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit(f"SQLite backup integrity check failed: {result!r}")
finally:
    destination.close()
    source.close()
PY
  chmod 600 "$WORK/kol_dashboard.db"
  "$VPS" put "$WORK/kol_dashboard.db" "$REMOTE_STAGE/kol_dashboard.db"
fi

{
  printf 'REMOTE_STAGE=%q\n' "$REMOTE_STAGE"
  printf 'RELEASE_ID=%q\n' "$DEPLOY_ID"
  cat <<'REMOTE'
#!/usr/bin/env bash
set -euo pipefail
umask 077

BASE_DIR=/opt/kol-dashboard
STAGING_DIR="$BASE_DIR/.staging"
RELEASES_DIR="$BASE_DIR/releases"
DATA_DIR="$BASE_DIR/data"
PRIVATE_DIR="$BASE_DIR/private"
BACKUPS_DIR="$BASE_DIR/backups"
DB_PATH="$DATA_DIR/kol_dashboard.db"
DAILY_SNAPSHOT_PATH="$DATA_DIR/daily-briefing-latest.json"
CURRENT_LINK="$BASE_DIR/current"
CURRENT_NEXT="$BASE_DIR/current.next.$RELEASE_ID"
DB_NEXT="$DATA_DIR/kol_dashboard.db.next.$RELEASE_ID"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
ROLLBACK_DIR="$REMOTE_STAGE/rollback"
PREVIOUS_TARGET=""
SERVICES_STOPPED=0
ROLLBACK_READY=0
DB_ROLLBACK_READY=0
COMMITTED=0

case "$REMOTE_STAGE" in
  "$STAGING_DIR"/*) ;;
  *) echo "非法暂存路径: $REMOTE_STAGE" >&2; exit 1 ;;
esac

backup_path() {
  local source_path=$1
  local backup_name=$2
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -a "$source_path" "$ROLLBACK_DIR/config/$backup_name"
  else
    : > "$ROLLBACK_DIR/config/$backup_name.absent"
  fi
}

restore_path() {
  local target_path=$1
  local backup_name=$2
  if [[ -f "$ROLLBACK_DIR/config/$backup_name.absent" ]]; then
    rm -f "$target_path" || return 1
  elif [[ -e "$ROLLBACK_DIR/config/$backup_name" ]]; then
    cp -a "$ROLLBACK_DIR/config/$backup_name" "$target_path" || return 1
  fi
  return 0
}

record_unit_state() {
  local unit=$1 enabled=disabled active=inactive
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    enabled=enabled
  fi
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    active=active
  fi
  printf '%s %s\n' "$enabled" "$active" \
    > "$ROLLBACK_DIR/config/$unit.state"
}

prepare_unit_state_rollback() {
  local unit enabled active failed=0
  for unit in kol-dashboard.service kol-collect-kol.timer \
    kol-collect-macro.timer kol-collect-decision.timer \
    kol-collect-daily.timer kol-collect-enrich.timer \
    kol-enrich-wakeup.path; do
    read -r enabled active < "$ROLLBACK_DIR/config/$unit.state" || {
      failed=1
      continue
    }
    if [[ "$enabled" == disabled ]] && \
       systemctl cat "$unit" >/dev/null 2>&1; then
      systemctl disable -q "$unit" >/dev/null 2>&1 || failed=1
    fi
  done
  return "$failed"
}

restore_unit_states() {
  local unit enabled active failed=0
  for unit in kol-dashboard.service kol-collect-kol.timer \
    kol-collect-macro.timer kol-collect-decision.timer \
    kol-collect-daily.timer kol-collect-enrich.timer \
    kol-enrich-wakeup.path; do
    read -r enabled active < "$ROLLBACK_DIR/config/$unit.state" || {
      failed=1
      continue
    }
    if [[ "$enabled" == enabled ]]; then
      systemctl enable -q "$unit" >/dev/null 2>&1 || failed=1
    fi
    if [[ "$active" == active ]]; then
      systemctl start "$unit" >/dev/null 2>&1 || failed=1
    fi
  done
  return "$failed"
}

unit_was_active() {
  local unit=$1 enabled active
  read -r enabled active < "$ROLLBACK_DIR/config/$unit.state" || return 1
  [[ "$active" == active ]]
}

database_integrity() {
  python3 - "$1" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit(f"Database integrity check failed: {result!r}")
finally:
    connection.close()
PY
}

validate_daily_snapshot() {
  local acceptance_not_before=$1
  python3 - "$DB_PATH" "$acceptance_not_before" <<'PY'
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys

database_path = sys.argv[1]
not_before = datetime.fromtimestamp(int(sys.argv[2]), timezone.utc)
now = datetime.now(timezone.utc)
connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
try:
    row = connection.execute(
        """
        SELECT generated_at, source_as_of, payload_json
        FROM daily_briefing_snapshots
        WHERE schema_version=1
        ORDER BY source_as_of DESC, generated_at DESC, imported_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
finally:
    connection.close()

if row is None:
    raise SystemExit("Daily candidate did not import a snapshot")
generated_at, source_as_of, encoded = row
try:
    coverage_time = datetime.fromisoformat(source_as_of)
    payload = json.loads(encoded)
except (TypeError, ValueError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Daily candidate snapshot is invalid: {exc}") from exc
if coverage_time.tzinfo is None or coverage_time.utcoffset() is None:
    raise SystemExit("Daily candidate source_as_of must be timezone-aware")
coverage_time = coverage_time.astimezone(timezone.utc)
if coverage_time < not_before - timedelta(minutes=5):
    raise SystemExit("Daily candidate snapshot did not advance during deployment")
if coverage_time > now + timedelta(minutes=5):
    raise SystemExit("Daily candidate source_as_of is in the future")

sections = payload.get("sections")
expected_sections = {"macro", "world", "finance", "technology", "ai", "investors"}
if not isinstance(sections, dict) or set(sections) != expected_sections:
    raise SystemExit("Daily candidate snapshot does not contain all six sections")
observed = {
    (item.get("source"), item.get("kind"))
    for values in sections.values()
    if isinstance(values, list)
    for item in values
    if isinstance(item, dict)
}
required = {("Hacker News", "hn_story")}
missing = sorted(required - observed)
if missing:
    raise SystemExit(f"Daily candidate is missing required discovery sources: {missing}")
print(
    "daily candidate: accepted "
    f"generated_at={generated_at} source_as_of={source_as_of}"
)
PY
}

validate_daily_api() {
  python3 - <<'PY'
import json
from urllib.request import Request, urlopen

request = Request(
    "http://127.0.0.1:8088/api/briefings/latest",
    headers={"Accept": "application/json"},
)
with urlopen(request, timeout=8) as response:
    if response.status != 200:
        raise SystemExit(f"Daily API returned HTTP {response.status}")
    payload = json.load(response)

if not payload.get("source_coverage_as_of"):
    raise SystemExit("Daily API did not expose imported source coverage")
if payload.get("refresh_schedule_status") not in {"configured", "active"}:
    raise SystemExit("Daily API did not expose the configured hourly schedule")
if not payload.get("next_refresh_at"):
    raise SystemExit("Daily API did not expose the next hourly refresh")
print(
    "daily api: accepted "
    f"status={payload['refresh_schedule_status']} "
    f"next={payload['next_refresh_at']}"
)
PY
}

activate_nginx() {
  nginx -t || return 1
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx || return 1
  else
    systemctl start nginx || return 1
  fi
  systemctl is-active --quiet nginx
}

rollback_database() {
  [[ $DB_ROLLBACK_READY == 1 ]] || return 0
  if [[ -f "$ROLLBACK_DIR/database.before-release" ]]; then
    database_integrity "$ROLLBACK_DIR/database.before-release" || return 1
    install -m 600 "$ROLLBACK_DIR/database.before-release" "$DB_NEXT" ||
      return 1
    chown kol-dashboard:kol-dashboard "$DB_NEXT" || return 1
    database_integrity "$DB_NEXT" || return 1
    rm -f "$DB_PATH-wal" "$DB_PATH-shm" || return 1
    mv -f "$DB_NEXT" "$DB_PATH" || return 1
    chmod 600 "$DB_PATH" || return 1
    database_integrity "$DB_PATH" || return 1
  elif [[ -f "$ROLLBACK_DIR/database.absent" ]]; then
    rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$DB_NEXT" ||
      return 1
  fi
  return 0
}

rollback_daily_snapshot() {
  restore_path "$DAILY_SNAPSHOT_PATH" daily-briefing-latest.json || return 1
  if [[ -f "$DAILY_SNAPSHOT_PATH" ]]; then
    chown kol-dashboard:kol-dashboard "$DAILY_SNAPSHOT_PATH" || return 1
    chmod 600 "$DAILY_SNAPSHOT_PATH" || return 1
  fi
  return 0
}

rollback_configuration() {
  local failed=0
  restore_path /etc/kol-dashboard.env kol-dashboard.env || failed=1
  restore_path /etc/systemd/system/kol-dashboard.service \
    kol-dashboard.service || failed=1
  for job in kol macro decision daily enrich; do
    restore_path "/etc/systemd/system/kol-collect-${job}.service" \
      "kol-collect-${job}.service" || failed=1
    restore_path "/etc/systemd/system/kol-collect-${job}.timer" \
      "kol-collect-${job}.timer" || failed=1
  done
  restore_path /etc/systemd/system/kol-enrich-wakeup.path \
    kol-enrich-wakeup.path || failed=1
  restore_path /etc/nginx/snippets/kol-dashboard.conf \
    kol-dashboard.conf || failed=1
  restore_path /etc/nginx/snippets/aidao.locations.conf \
    aidao.locations.conf || failed=1
  return "$failed"
}

cleanup_remote() {
  local rc=$?
  trap - EXIT
  set +e
  rm -f "$CURRENT_NEXT" "$DB_NEXT"
  if [[ $rc != 0 && $ROLLBACK_READY == 1 && $COMMITTED == 0 ]]; then
    local rollback_failed=0 rollback_safe=1 unit unit_load_state unit_state
    local unit_query_rc active_target rollback_health
    local -a rollback_units=(
      kol-enrich-wakeup.path
      kol-collect-kol.timer kol-collect-macro.timer
      kol-collect-decision.timer kol-collect-daily.timer
      kol-collect-enrich.timer
      kol-collect-kol.service kol-collect-macro.service
      kol-collect-decision.service kol-collect-daily.service
      kol-collect-enrich.service
      kol-dashboard.service
    )
    echo "部署失败，恢复上一版本、数据库和配置" >&2
    systemctl stop "${rollback_units[@]}" >/dev/null 2>&1 || true
    for unit in "${rollback_units[@]}"; do
      unit_load_state="$(systemctl show --property=LoadState --value \
        "$unit" 2>/dev/null)"
      unit_query_rc=$?
      if [[ $unit_query_rc != 0 || -z "$unit_load_state" ]]; then
        echo "无法确认回滚单元是否存在: $unit" >&2
        rollback_safe=0
        continue
      fi
      if [[ "$unit_load_state" == not-found ]]; then
        continue
      fi
      unit_state="$(systemctl show --property=ActiveState --value \
        "$unit" 2>/dev/null)"
      unit_query_rc=$?
      if [[ $unit_query_rc != 0 || -z "$unit_state" ||
            ( "$unit_state" != inactive && "$unit_state" != failed ) ]]; then
        echo "回滚前数据库写入进程或触发器未停净: $unit ($unit_state)" >&2
        rollback_safe=0
      fi
    done

    if [[ $rollback_safe == 1 ]]; then
      rollback_database || rollback_failed=1
      rollback_daily_snapshot || rollback_failed=1
      prepare_unit_state_rollback || rollback_failed=1
      rollback_configuration || rollback_failed=1
      if [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]]; then
        ln -s "$PREVIOUS_TARGET" "$CURRENT_NEXT" || rollback_failed=1
        mv -Tf "$CURRENT_NEXT" "$CURRENT_LINK" || rollback_failed=1
        active_target="$(readlink -f "$CURRENT_LINK" 2>/dev/null)"
        [[ "$active_target" == "$PREVIOUS_TARGET" ]] || rollback_failed=1
      else
        rm -f "$CURRENT_LINK" || rollback_failed=1
      fi
      systemctl daemon-reload || rollback_failed=1
      restore_unit_states || rollback_failed=1
      activate_nginx >/dev/null 2>&1 || rollback_failed=1
      if unit_was_active kol-dashboard.service; then
        rollback_health=FAILED
        for _ in $(seq 1 15); do
          if curl -sf --max-time 4 \
            http://127.0.0.1:8088/health >/dev/null; then
            rollback_health=ok
            break
          fi
          sleep 2
        done
        [[ "$rollback_health" == ok ]] || rollback_failed=1
      fi
      if [[ $rollback_failed != 0 ]]; then
        : > "$REMOTE_STAGE/PRESERVE"
        chmod 600 "$REMOTE_STAGE/PRESERVE"
        echo "ROLLBACK INCOMPLETE: 恢复材料保留在 $REMOTE_STAGE" >&2
      else
        echo "回滚验证完成，旧服务健康" >&2
      fi
    else
      : > "$REMOTE_STAGE/PRESERVE"
      chmod 600 "$REMOTE_STAGE/PRESERVE"
      echo "ROLLBACK ABORTED: 写入进程或触发器未停止；数据库、代码和配置保持失败现场，恢复材料保留在 $REMOTE_STAGE" >&2
    fi
  fi
  active_target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  if [[ ! -f "$REMOTE_STAGE/PRESERVE" &&
        "$active_target" != "$RELEASE_DIR" ]]; then
    rm -rf "$RELEASE_DIR"
  fi
  if [[ ! -f "$REMOTE_STAGE/PRESERVE" ]]; then
    rm -rf "$REMOTE_STAGE"
  fi
  exit "$rc"
}
trap cleanup_remote EXIT

[[ -f "$REMOTE_STAGE/app.tgz" ]] || {
  echo "部署包缺失: $REMOTE_STAGE/app.tgz" >&2
  exit 1
}

install -d -m 755 "$BASE_DIR" "$RELEASES_DIR"
install -d -m 700 "$STAGING_DIR" "$BACKUPS_DIR"
chmod 700 "$REMOTE_STAGE"
exec 9>"$BASE_DIR/.deploy.lock"
flock -n 9 || {
  echo "已有 KOL Dashboard 部署正在执行" >&2
  exit 1
}

# Fail before stopping any service when an old site-level include would load
# the KOL locations a second time. The canonical route is aggregated through
# /etc/nginx/snippets/aidao.locations.conf; this deploy intentionally never
# edits the shared AiDao server block.
NGINX_SITE=/etc/nginx/sites-enabled/aidao
[[ -r "$NGINX_SITE" ]] || {
  echo "nginx 站点配置不可读: $NGINX_SITE" >&2
  exit 1
}
LEGACY_NGINX_INCLUDE=/root/kol-dashboard/deployment/nginx/kol.locations.conf
LEGACY_INCLUDE_STATUS=0
grep -Eq \
  "^[[:space:]]*include[[:space:]]+['\"]?/root/kol-dashboard/deployment/nginx/kol\\.locations\\.conf['\"]?[[:space:]]*;([[:space:]]*#.*)?$" \
  "$NGINX_SITE" || LEGACY_INCLUDE_STATUS=$?
case "$LEGACY_INCLUDE_STATUS" in
  0)
    echo "检测到活动旧 KOL nginx include: $LEGACY_NGINX_INCLUDE" >&2
    echo "请先迁移到 /etc/nginx/snippets/aidao.locations.conf" >&2
    exit 1
    ;;
  1) ;;
  *)
    echo "无法核验 nginx 站点配置: $NGINX_SITE" >&2
    exit 1
    ;;
esac
nginx -t || {
  echo "当前 nginx 配置无效，拒绝在停止服务前继续部署" >&2
  exit 1
}

if ! getent group kol-dashboard >/dev/null; then
  groupadd --system kol-dashboard
fi
if ! id -u kol-dashboard >/dev/null 2>&1; then
  useradd --system --gid kol-dashboard --home-dir /nonexistent \
    --shell /usr/sbin/nologin kol-dashboard
fi
install -d -o kol-dashboard -g kol-dashboard -m 700 \
  "$DATA_DIR" "$PRIVATE_DIR"
install -d -o kol-dashboard -g kol-dashboard -m 750 \
  /var/log/kol-dashboard
chown -R kol-dashboard:kol-dashboard /var/log/kol-dashboard
for log_file in out.log err.log collect.log collect.err.log; do
  if [[ ! -e "/var/log/kol-dashboard/$log_file" ]]; then
    install -o kol-dashboard -g kol-dashboard -m 600 /dev/null \
      "/var/log/kol-dashboard/$log_file"
  fi
  chown kol-dashboard:kol-dashboard "/var/log/kol-dashboard/$log_file"
  chmod 600 "/var/log/kol-dashboard/$log_file"
done
chmod 700 "$DATA_DIR"
chmod 700 "$PRIVATE_DIR"
if [[ -f "$PRIVATE_DIR/holdings.md" ]]; then
  chown kol-dashboard:kol-dashboard "$PRIVATE_DIR/holdings.md"
  chmod 600 "$PRIVATE_DIR/holdings.md"
fi

if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_TARGET="$(readlink -f "$CURRENT_LINK")"
  case "$PREVIOUS_TARGET" in
    "$RELEASES_DIR"/*) ;;
    *) echo "current 指向发布目录之外，拒绝部署" >&2; exit 1 ;;
  esac
elif [[ -e "$CURRENT_LINK" ]]; then
  echo "current 必须是符号链接，拒绝覆盖" >&2
  exit 1
elif [[ -f "$BASE_DIR/app.py" ]]; then
  LEGACY_RELEASE="$RELEASES_DIR/legacy-$RELEASE_ID"
  install -d -m 755 "$LEGACY_RELEASE"
  tar -C "$BASE_DIR" \
    --exclude='./data' --exclude='./private' --exclude='./releases' \
    --exclude='./.staging' --exclude='./current*' \
    --exclude='./.deploy.lock' -cf - . |
    tar --no-same-owner -xf - -C "$LEGACY_RELEASE"
  chown -R root:root "$LEGACY_RELEASE"
  PREVIOUS_TARGET="$LEGACY_RELEASE"
fi

[[ ! -e "$RELEASE_DIR" ]] || {
  echo "发布目录已存在: $RELEASE_DIR" >&2
  exit 1
}
install -d -m 755 "$RELEASE_DIR"
tar xzf "$REMOTE_STAGE/app.tgz" --no-same-owner --no-same-permissions \
  -C "$RELEASE_DIR"
chown -R root:root "$RELEASE_DIR"
chgrp -R kol-dashboard "$RELEASE_DIR"
chmod -R u=rwX,g=rX,o= "$RELEASE_DIR"
chmod 750 "$RELEASE_DIR/collect.sh" "$RELEASE_DIR/decision_collect.py" \
  "$RELEASE_DIR/enrichment_collect.py"
runuser -u kol-dashboard -- test -r "$RELEASE_DIR/app.py"
runuser -u kol-dashboard -- test -x "$RELEASE_DIR"

python3 -c 'import fastapi, uvicorn' 2>/dev/null || {
  echo "服务器缺少 fastapi/uvicorn；请先维护运行环境" >&2
  exit 1
}
python3 -m compileall -q "$RELEASE_DIR"

if [[ ! -f /etc/kol-dashboard.env &&
      ! -f "$REMOTE_STAGE/auth.env" &&
      -f /etc/systemd/system/kol-dashboard.service ]] &&
   grep -Eq '^Environment=KOL_DASHBOARD_(PASSCODE_HASH|SESSION_SECRET)=' \
     /etc/systemd/system/kol-dashboard.service; then
  echo "旧服务内嵌认证密钥；请使用 --auth 显式迁移" >&2
  exit 1
fi

install -d -m 700 "$ROLLBACK_DIR/config"
backup_path /etc/kol-dashboard.env kol-dashboard.env
if [[ -e "$DAILY_SNAPSHOT_PATH" || -L "$DAILY_SNAPSHOT_PATH" ]]; then
  [[ -f "$DAILY_SNAPSHOT_PATH" && ! -L "$DAILY_SNAPSHOT_PATH" ]] || {
    echo "Daily 快照必须是普通文件，不能是符号链接" >&2
    exit 1
  }
fi
backup_path "$DAILY_SNAPSHOT_PATH" daily-briefing-latest.json
backup_path /etc/systemd/system/kol-dashboard.service \
  kol-dashboard.service
for job in kol macro decision daily enrich; do
  backup_path "/etc/systemd/system/kol-collect-${job}.service" \
    "kol-collect-${job}.service"
  backup_path "/etc/systemd/system/kol-collect-${job}.timer" \
    "kol-collect-${job}.timer"
done
backup_path /etc/systemd/system/kol-enrich-wakeup.path \
  kol-enrich-wakeup.path
for unit in kol-dashboard.service kol-collect-kol.timer \
  kol-collect-macro.timer kol-collect-decision.timer \
  kol-collect-daily.timer kol-collect-enrich.timer \
  kol-enrich-wakeup.path; do
  record_unit_state "$unit"
done
backup_path /etc/nginx/snippets/kol-dashboard.conf \
  kol-dashboard.conf
backup_path /etc/nginx/snippets/aidao.locations.conf \
  aidao.locations.conf
ROLLBACK_READY=1

systemctl stop kol-enrich-wakeup.path \
  kol-collect-kol.timer kol-collect-macro.timer \
  kol-collect-decision.timer kol-collect-daily.timer \
  kol-collect-enrich.timer 2>/dev/null || true
for unit in kol-collect-kol.service kol-collect-macro.service \
  kol-collect-decision.service kol-collect-daily.service \
  kol-collect-enrich.service \
  kol-dashboard.service; do
  systemctl stop "$unit" 2>/dev/null || true
done
for unit in kol-enrich-wakeup.path \
  kol-collect-kol.timer kol-collect-macro.timer \
  kol-collect-decision.timer kol-collect-daily.timer \
  kol-collect-kol.service \
  kol-collect-macro.service kol-collect-decision.service \
  kol-collect-daily.service kol-collect-enrich.timer \
  kol-collect-enrich.service \
  kol-dashboard.service; do
  if systemctl is-active --quiet "$unit"; then
    echo "active database writer remains: $unit" >&2
    exit 1
  fi
done
SERVICES_STOPPED=1

if [[ -f "$DB_PATH" ]]; then
  python3 - "$DB_PATH" \
    "$ROLLBACK_DIR/database.before-release" \
    "$ROLLBACK_DIR/database.before-release.next" <<'PY'
from pathlib import Path
import os
import sqlite3
import sys

source_path = Path(sys.argv[1])
final_path = Path(sys.argv[2])
next_path = Path(sys.argv[3])
next_path.unlink(missing_ok=True)
source = sqlite3.connect(source_path, timeout=30)
destination = sqlite3.connect(next_path)
try:
    source.execute("PRAGMA busy_timeout=30000")
    source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit(f"Remote database backup failed integrity check: {result!r}")
finally:
    destination.close()
    source.close()

with next_path.open("rb") as handle:
    os.fsync(handle.fileno())
os.replace(next_path, final_path)
directory_fd = os.open(final_path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  chmod 600 "$ROLLBACK_DIR/database.before-release"
  DB_ROLLBACK_READY=1
else
  : > "$ROLLBACK_DIR/database.absent"
  DB_ROLLBACK_READY=1
fi

# Preserve existing secrets unless an explicit --auth payload was uploaded.
# The rewrite is always atomic and forces the cookie/holdings security policy.
python3 - "$REMOTE_STAGE/auth.env" /etc/kol-dashboard.env <<'PY'
from pathlib import Path
import os
import re
import secrets
import sys
import tempfile

incoming_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
allowed = {
    "KOL_DASHBOARD_PASSCODE_HASH",
    "KOL_DASHBOARD_SESSION_SECRET",
    "KOL_DASHBOARD_SESSION_TTL_SECONDS",
    "KOL_DASHBOARD_COOKIE_NAME",
    "KOL_DASHBOARD_COOKIE_PATH",
    "KOL_DASHBOARD_COOKIE_SECURE",
    "KOL_DASHBOARD_HOLDINGS_FILE",
}
key_pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key_pattern.fullmatch(key) and key in allowed and "\n" not in value:
            values[key] = value
    return values


values = read_environment(target_path)
if incoming_path.is_file():
    incoming = read_environment(incoming_path)
    required = {
        "KOL_DASHBOARD_PASSCODE_HASH",
        "KOL_DASHBOARD_SESSION_SECRET",
    }
    if not required.issubset(incoming):
        raise SystemExit("认证更新文件不完整")
    values.update(incoming)

values.setdefault(
    "KOL_DASHBOARD_SESSION_SECRET",
    secrets.token_urlsafe(48),
)
ttl = values.get("KOL_DASHBOARD_SESSION_TTL_SECONDS", "")
if not ttl.isdigit() or not 300 <= int(ttl) <= 86400:
    values["KOL_DASHBOARD_SESSION_TTL_SECONDS"] = "28800"
values["KOL_DASHBOARD_COOKIE_PATH"] = "/kol"
values["KOL_DASHBOARD_COOKIE_SECURE"] = "true"
values["KOL_DASHBOARD_HOLDINGS_FILE"] = (
    "/opt/kol-dashboard/private/holdings.md"
)

order = (
    "KOL_DASHBOARD_PASSCODE_HASH",
    "KOL_DASHBOARD_SESSION_SECRET",
    "KOL_DASHBOARD_SESSION_TTL_SECONDS",
    "KOL_DASHBOARD_COOKIE_NAME",
    "KOL_DASHBOARD_COOKIE_PATH",
    "KOL_DASHBOARD_COOKIE_SECURE",
    "KOL_DASHBOARD_HOLDINGS_FILE",
)
fd, temporary_name = tempfile.mkstemp(
    prefix=".kol-dashboard.env.",
    dir=str(target_path.parent),
    text=True,
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key in order:
            if key in values:
                handle.write(f"{key}={values[key]}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, target_path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
chmod 600 /etc/kol-dashboard.env

if [[ -f "$REMOTE_STAGE/kol_dashboard.db" ]]; then
  python3 - "$REMOTE_STAGE/kol_dashboard.db" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit(f"Uploaded database integrity check failed: {result!r}")
finally:
    connection.close()
PY
  install -m 600 "$REMOTE_STAGE/kol_dashboard.db" "$DB_NEXT"
  chown kol-dashboard:kol-dashboard "$DB_NEXT"
  rm -f "$DB_PATH-wal" "$DB_PATH-shm"
  mv -f "$DB_NEXT" "$DB_PATH"
fi

cd "$RELEASE_DIR"
KOL_DASHBOARD_DB="$DB_PATH" python3 -c 'import db; db.init()'
chown kol-dashboard:kol-dashboard "$DB_PATH"
chmod 600 "$DB_PATH"
rm -f "$DB_PATH-wal" "$DB_PATH-shm"

echo "→ 预热公共决策快照"
runuser -u kol-dashboard -- /usr/bin/env \
  KOL_DASHBOARD_DB="$DB_PATH" \
  KOL_DB_WRITE_REQUIRED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 "$RELEASE_DIR/decision_collect.py" snapshot
database_integrity "$DB_PATH"

ln -s "$RELEASE_DIR" "$CURRENT_NEXT"
mv -Tf "$CURRENT_NEXT" "$CURRENT_LINK"

cat > "$REMOTE_STAGE/kol-dashboard.service" <<'UNIT'
[Unit]
Description=KOL Dashboard + Macro Risk Radar
After=network.target

[Service]
Type=simple
User=kol-dashboard
Group=kol-dashboard
WorkingDirectory=/opt/kol-dashboard/current
EnvironmentFile=-/etc/kol-dashboard.env
Environment=KOL_DASHBOARD_PORT=8088
Environment=KOL_DASHBOARD_HOST=127.0.0.1
Environment=KOL_DASHBOARD_DB=/opt/kol-dashboard/data/kol_dashboard.db
Environment=KOL_DAILY_REFRESH_SCHEDULE=hourly
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/kol-dashboard/current/app.py
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/opt/kol-dashboard/data /opt/kol-dashboard/private /var/log/kol-dashboard
StandardOutput=append:/var/log/kol-dashboard/out.log
StandardError=append:/var/log/kol-dashboard/err.log

[Install]
WantedBy=multi-user.target
UNIT
install -m 644 "$REMOTE_STAGE/kol-dashboard.service" \
  /etc/systemd/system/kol-dashboard.service

if [[ -e /etc/kol-dashboard/deepseek.env || \
      -L /etc/kol-dashboard/deepseek.env ]]; then
  [[ -f /etc/kol-dashboard/deepseek.env && \
     ! -L /etc/kol-dashboard/deepseek.env ]] || {
    echo "DeepSeek 环境文件必须是普通文件，不能是符号链接" >&2
    exit 1
  }
  DEEPSEEK_SECRET_STAT=$(stat -c '%U:%G:%a' /etc/kol-dashboard/deepseek.env)
  [[ "$DEEPSEEK_SECRET_STAT" == "root:root:600" ]] || {
    echo "DeepSeek 环境文件权限必须为 root:root 0600" >&2
    exit 1
  }
fi

for job in kol macro decision daily enrich; do
  EXTRA_ENVIRONMENT=""
  EXTRA_SCHEDULE=""
  EXTRA_HARDENING=""
  EXTRA_EXEC_START_PRE=""
  if [[ "$job" == "daily" || "$job" == "enrich" ]]; then
    EXTRA_ENVIRONMENT="EnvironmentFile=-/etc/kol-dashboard/deepseek.env"
    EXTRA_HARDENING="LimitCORE=0"
  fi
  if [[ "$job" == "daily" ]]; then
    EXTRA_SCHEDULE="Environment=KOL_DAILY_REFRESH_SCHEDULE=hourly"
  elif [[ "$job" == "enrich" ]]; then
    EXTRA_EXEC_START_PRE="ExecStartPre=/usr/bin/rm -f /opt/kol-dashboard/data/enrichment.pending"
  fi
  cat > "$REMOTE_STAGE/kol-collect-${job}.service" <<UNIT
[Unit]
Description=KOL dashboard ${job} collection
After=network-online.target

[Service]
Type=oneshot
User=kol-dashboard
Group=kol-dashboard
WorkingDirectory=/opt/kol-dashboard/current
EnvironmentFile=-/etc/kol-dashboard.env
${EXTRA_ENVIRONMENT}
${EXTRA_SCHEDULE}
Environment=KOL_DASHBOARD_DB=/opt/kol-dashboard/data/kol_dashboard.db
Environment=KOL_LOG_DIR=/var/log/kol-dashboard
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=strict
${EXTRA_HARDENING}
ReadWritePaths=/opt/kol-dashboard/data /opt/kol-dashboard/private /var/log/kol-dashboard
TimeoutStartSec=20min
${EXTRA_EXEC_START_PRE}
ExecStart=/bin/bash /opt/kol-dashboard/current/collect.sh ${job}
UNIT
  install -m 644 "$REMOTE_STAGE/kol-collect-${job}.service" \
    "/etc/systemd/system/kol-collect-${job}.service"
done

cat > "$REMOTE_STAGE/kol-enrich-wakeup.path" <<'UNIT'
[Unit]
Description=Wake KOL enrichment when new source data arrives

[Path]
PathExists=/opt/kol-dashboard/data/enrichment.pending
Unit=kol-collect-enrich.service

[Install]
WantedBy=multi-user.target
UNIT
install -m 644 "$REMOTE_STAGE/kol-enrich-wakeup.path" \
  /etc/systemd/system/kol-enrich-wakeup.path

cat > "$REMOTE_STAGE/kol-collect-kol.timer" <<'UNIT'
[Unit]
Description=Scan KOL news every 30 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > "$REMOTE_STAGE/kol-collect-macro.timer" <<'UNIT'
[Unit]
Description=Snapshot macro risk radar hourly

[Timer]
OnBootSec=6min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > "$REMOTE_STAGE/kol-collect-decision.timer" <<'UNIT'
[Unit]
Description=Refresh decision relations and market validation hourly

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > "$REMOTE_STAGE/kol-collect-daily.timer" <<'UNIT'
[Unit]
Description=Refresh Hacker News and curated AI Daily briefing hourly

[Timer]
OnCalendar=*-*-* *:05:00
RandomizedDelaySec=90s
AccuracySec=30s
Persistent=true
Unit=kol-collect-daily.service

[Install]
WantedBy=timers.target
UNIT

cat > "$REMOTE_STAGE/kol-collect-enrich.timer" <<'UNIT'
[Unit]
Description=Enrich recent KOL events with Chinese intelligence

[Timer]
OnBootSec=12min
OnUnitActiveSec=15min
RandomizedDelaySec=90s
Persistent=true

[Install]
WantedBy=timers.target
UNIT

for job in kol macro decision daily enrich; do
  install -m 644 "$REMOTE_STAGE/kol-collect-${job}.timer" \
    "/etc/systemd/system/kol-collect-${job}.timer"
done

cat > "$REMOTE_STAGE/kol-dashboard.conf" <<'NGINX'
# KOL Dashboard — reverse proxy at /kol/
location = /kol { return 301 /kol/; }
location ^~ /kol/static/ {
    proxy_pass http://127.0.0.1:8088/static/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;
    gzip_min_length 512;
    gzip_types application/json application/javascript text/javascript text/css image/svg+xml;

    proxy_hide_header Cache-Control;
    add_header Cache-Control "public, max-age=604800, immutable" always;
}
location /kol/ {
    proxy_pass http://127.0.0.1:8088/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;
    gzip_min_length 512;
    gzip_types application/json application/javascript text/javascript text/css image/svg+xml;
}
NGINX
install -m 644 "$REMOTE_STAGE/kol-dashboard.conf" \
  /etc/nginx/snippets/kol-dashboard.conf

SITE=/etc/nginx/snippets/aidao.locations.conf
[[ -f "$SITE" ]] || {
  echo "nginx 路由汇总文件不存在: $SITE" >&2
  exit 1
}
if ! grep -Eq \
  '^[[:space:]]*include[[:space:]]+/etc/nginx/snippets/kol-dashboard\.conf;[[:space:]]*$' \
  "$SITE"; then
  cp "$SITE" "$REMOTE_STAGE/aidao.locations.conf.next"
  printf '\ninclude /etc/nginx/snippets/kol-dashboard.conf;\n' \
    >> "$REMOTE_STAGE/aidao.locations.conf.next"
  install -m 644 "$REMOTE_STAGE/aidao.locations.conf.next" "$SITE"
fi

systemctl daemon-reload
systemctl enable -q kol-dashboard kol-collect-kol.timer \
  kol-collect-macro.timer kol-collect-decision.timer \
  kol-collect-daily.timer kol-collect-enrich.timer \
  kol-enrich-wakeup.path
nginx -t
systemctl restart kol-dashboard.service

DIRECT_HEALTH=FAILED
for _ in $(seq 1 30); do
  if curl -sf --max-time 4 http://127.0.0.1:8088/health >/dev/null; then
    DIRECT_HEALTH=ok
    break
  fi
  sleep 2
done

[[ "$DIRECT_HEALTH" == ok ]] || {
  echo "新版本直连健康检查失败" >&2
  exit 1
}

activate_nginx
PROXY_HEALTH=FAILED
for _ in $(seq 1 10); do
  if curl -sf --max-time 5 --noproxy '*' \
    --resolve zlstreet.xyz:443:127.0.0.1 \
    https://zlstreet.xyz/kol/health >/dev/null; then
    PROXY_HEALTH=ok
    break
  fi
  sleep 2
done
[[ "$PROXY_HEALTH" == ok ]] || {
  echo "nginx /kol/ 代理健康检查失败" >&2
  exit 1
}

echo "→ 验收 Daily 候选采集"
DAILY_ACCEPTANCE_NOT_BEFORE=$(date -u +%s)
systemctl start kol-collect-daily.service
database_integrity "$DB_PATH"
validate_daily_snapshot "$DAILY_ACCEPTANCE_NOT_BEFORE"
validate_daily_api

systemctl start kol-collect-kol.timer kol-collect-macro.timer \
  kol-collect-decision.timer kol-collect-daily.timer \
  kol-collect-enrich.timer kol-enrich-wakeup.path
for unit in kol-collect-kol.timer kol-collect-macro.timer \
  kol-collect-decision.timer kol-collect-daily.timer \
  kol-collect-enrich.timer; do
  systemctl is-enabled --quiet "$unit" || {
    echo "采集定时器未启用: $unit" >&2
    exit 1
  }
  systemctl is-active --quiet "$unit" || {
    echo "采集定时器未运行: $unit" >&2
    exit 1
  }
done
systemctl is-enabled --quiet kol-enrich-wakeup.path || {
  echo "enrichment 唤醒 path 未启用" >&2
  exit 1
}
systemctl is-active --quiet kol-enrich-wakeup.path || {
  echo "enrichment 唤醒 path 未运行" >&2
  exit 1
}

# Schema migrations are forward-only. Keep the verified pre-release database
# outside the disposable staging tree so an operator can pair an old binary
# rollback with its compatible database instead of only switching `current`.
if [[ -f "$ROLLBACK_DIR/database.before-release" ]]; then
  DURABLE_DB_BACKUP="$BACKUPS_DIR/database.before-$RELEASE_ID.sqlite3"
  install -o root -g root -m 600 \
    "$ROLLBACK_DIR/database.before-release" "$DURABLE_DB_BACKUP"
  database_integrity "$DURABLE_DB_BACKUP"
  python3 - "$DURABLE_DB_BACKUP" <<'PY'
from pathlib import Path
import os
import sys

backup_path = Path(sys.argv[1])
with backup_path.open("rb") as handle:
    os.fsync(handle.fileno())
directory_fd = os.open(backup_path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
fi
SERVICES_STOPPED=0
COMMITTED=1
echo "service: $(systemctl is-active kol-dashboard)  health: ok"
REMOTE
} > "$WORK/remote.sh"
chmod 700 "$WORK/remote.sh"

echo "→ 原子切换远端版本"
"$VPS" script "$WORK/remote.sh"

echo "→ 公网验证"
curl -sf --max-time 12 https://zlstreet.xyz/kol/health && echo "  ← /kol/health"
curl -sf --max-time 12 -o /dev/null \
  -w "  /kol/ → HTTP %{http_code}\n" https://zlstreet.xyz/kol/
curl -sf --max-time 12 -o /dev/null \
  -w "  /kol/api/briefings/latest → HTTP %{http_code}\n" \
  https://zlstreet.xyz/kol/api/briefings/latest
echo "✓ 完成 — https://zlstreet.xyz/kol/"
