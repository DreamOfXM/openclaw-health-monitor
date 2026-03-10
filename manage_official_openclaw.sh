#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TRACKED_CONFIG="$BASE_DIR/config.conf"
LOCAL_CONFIG="$BASE_DIR/config.local.conf"
LOG_DIR="$BASE_DIR/logs"
OFFICIAL_PID_FILE="$LOG_DIR/openclaw-official.pid"
OFFICIAL_LOG_FILE="$LOG_DIR/openclaw-official.log"
OFFICIAL_LABEL="ai.openclaw.gateway.official"
OFFICIAL_PLIST="$HOME/Library/LaunchAgents/${OFFICIAL_LABEL}.plist"
SCHEDULE_LABEL="ai.openclaw.official-update"
SCHEDULE_PLIST="$HOME/Library/LaunchAgents/${SCHEDULE_LABEL}.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

mkdir -p "$LOG_DIR"

config_value() {
    local key="$1"
    local value=""
    if [ -f "$TRACKED_CONFIG" ]; then
        value="$(awk -F= -v key="$key" '$1==key{print substr($0, index($0,$2))}' "$TRACKED_CONFIG" | tail -n 1 | tr -d '"' | tr -d "'")"
    fi
    if [ -f "$LOCAL_CONFIG" ]; then
        local local_value
        local_value="$(awk -F= -v key="$key" '$1==key{print substr($0, index($0,$2))}' "$LOCAL_CONFIG" | tail -n 1 | tr -d '"' | tr -d "'")"
        if [ -n "$local_value" ]; then
            value="$local_value"
        fi
    fi
    value="${value//\$HOME/$HOME}"
    printf '%s\n' "$value"
}

openclaw_code() {
    local value
    value="$(config_value OPENCLAW_CODE)"
    printf '%s\n' "${value:-$HOME/openclaw-workspace/openclaw}"
}

official_code() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_CODE)"
    printf '%s\n' "${value:-$HOME/openclaw-workspace/openclaw-official}"
}

openclaw_home() {
    local value
    value="$(config_value OPENCLAW_HOME)"
    printf '%s\n' "${value:-$HOME/.openclaw}"
}

official_state() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_STATE)"
    printf '%s\n' "${value:-$HOME/.openclaw-official}"
}

official_port() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_PORT)"
    printf '%s\n' "${value:-19001}"
}

official_ref() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_REF)"
    printf '%s\n' "${value:-origin/main}"
}

official_update_hour() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_UPDATE_HOUR)"
    printf '%s\n' "${value:-4}"
}

official_update_minute() {
    local value
    value="$(config_value OPENCLAW_OFFICIAL_UPDATE_MINUTE)"
    printf '%s\n' "${value:-30}"
}

resolve_login_path() {
    /bin/zsh -lc 'printf %s "$PATH"' 2>/dev/null || true
}

bootstrap_env() {
    local login_path
    login_path="$(resolve_login_path)"
    if [ -n "$login_path" ]; then
        PATH="$login_path:$PATH"
        export PATH
    fi
    export NO_PROXY=127.0.0.1,localhost
    export no_proxy=127.0.0.1,localhost
}

resolve_login_cmd() {
    local name="$1"
    /bin/zsh -lc "command -v $name" 2>/dev/null | head -n 1 || true
}

launchd_bootout() {
    local label="$1"
    local plist="$2"
    launchctl bootout "${LAUNCH_DOMAIN}/${label}" 2>/dev/null || launchctl bootout "$LAUNCH_DOMAIN" "$plist" 2>/dev/null || true
}

launchd_bootstrap() {
    local plist="$1"
    launchctl bootstrap "$LAUNCH_DOMAIN" "$plist"
}

launchd_kickstart() {
    local label="$1"
    launchctl kickstart -k "${LAUNCH_DOMAIN}/${label}"
}

launchd_pid() {
    local label="$1"
    launchctl print "${LAUNCH_DOMAIN}/${label}" 2>/dev/null | awk -F'= ' '/pid = / {print $2; exit}' | tr -d ';' || true
}

require_cmd() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Error: missing required command: $name" >&2
        exit 1
    fi
}

ensure_worktree() {
    local current_repo target_repo target_ref
    current_repo="$(openclaw_code)"
    target_repo="$(official_code)"
    target_ref="$(official_ref)"

    require_cmd git
    if [ ! -d "$current_repo/.git" ] && [ ! -f "$current_repo/.git" ]; then
        echo "Error: OpenClaw repo not found at $current_repo" >&2
        exit 1
    fi

    git -C "$current_repo" fetch origin

    if [ ! -e "$target_repo" ]; then
        mkdir -p "$(dirname "$target_repo")"
        git -C "$current_repo" worktree add "$target_repo" "$target_ref"
    else
        git -C "$target_repo" fetch origin
        git -C "$target_repo" reset --hard "$target_ref"
        git -C "$target_repo" clean -fd
    fi
}

sync_private_state() {
    local source_state target_state source_code target_code
    source_state="$(openclaw_home)"
    target_state="$(official_state)"
    source_code="$(openclaw_code)"
    target_code="$(official_code)"

    require_cmd rsync
    mkdir -p "$target_state"

    rsync -a \
        --delete \
        --exclude 'logs/' \
        --exclude 'sessions/' \
        --exclude 'memory/' \
        --exclude 'tmp/' \
        --exclude 'cron/runs/' \
        "$source_state/" "$target_state/"

    python3 - <<PY
from pathlib import Path

source_state = "$source_state"
target_state = "$target_state"
source_code = "$source_code"
target_code = "$target_code"
root = Path(target_state)

for path in root.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    if rel.startswith("logs/") or "/logs/" in rel:
        continue
    if rel.startswith("sessions/") or "/sessions/" in rel:
        continue
    if rel.startswith("memory/") or "/memory/" in rel:
        continue
    if path.suffix in {".db", ".sqlite", ".sqlite3"}:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    replaced = text.replace(source_state, target_state).replace(source_code, target_code)
    if replaced != text:
        path.write_text(replaced, encoding="utf-8")
PY

    python3 - <<PY
import json
import secrets
from pathlib import Path

path = Path("$target_state") / "openclaw.json"
data = json.loads(path.read_text(encoding="utf-8"))
gateway = data.setdefault("gateway", {})
auth = gateway.setdefault("auth", {})
auth["token"] = secrets.token_hex(24)
control_ui = gateway.setdefault("controlUi", {})
control_ui["dangerouslyDisableDeviceAuth"] = True
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY

    rm -f "$target_state/identity/device.json" "$target_state/identity/device-auth.json"
    rm -f "$target_state/devices/paired.json" "$target_state/devices/pending.json"
    rm -rf "$target_state/browser/openclaw/user-data"
    rm -f "$target_state/workspace/data/browserwing.db"
}

build_official_repo() {
    local repo
    repo="$(official_code)"

    require_cmd pnpm
    (cd "$repo" && pnpm install --frozen-lockfile)
    (cd "$repo" && pnpm build)
}

dashboard_url() {
    local state port token gateway_url gateway_url_encoded
    state="$(official_state)"
    port="$(official_port)"
    token="$(python3 - <<PY
import json
from pathlib import Path
path = Path("$state") / "openclaw.json"
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print(data.get("gateway", {}).get("auth", {}).get("token", ""))
except Exception:
    print("")
PY
)"
    gateway_url="ws://127.0.0.1:$port"
    gateway_url_encoded="$(python3 - <<PY
import urllib.parse
print(urllib.parse.quote("$gateway_url", safe=""))
PY
)"
    if [ -n "$token" ]; then
        printf 'http://127.0.0.1:%s/#token=%s&gatewayUrl=%s\n' "$port" "$token" "$gateway_url_encoded"
    else
        printf 'http://127.0.0.1:%s/#gatewayUrl=%s\n' "$port" "$gateway_url_encoded"
    fi
}

read_pid_file() {
    if [ ! -f "$OFFICIAL_PID_FILE" ]; then
        return 1
    fi
    local pid
    pid="$(cat "$OFFICIAL_PID_FILE" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    printf '%s\n' "$pid"
}

listener_pid() {
    lsof -ti "tcp:$(official_port)" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

kill_listener_pid() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return 0
    fi
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
}

stop_listener() {
    local pid
    pid="$(listener_pid)"
    kill_listener_pid "$pid"
}

official_pid() {
    launchd_pid "$OFFICIAL_LABEL" || read_pid_file || listener_pid
}

health_check() {
    curl --noproxy '*' -fsS "http://127.0.0.1:$(official_port)/health" >/dev/null 2>&1
}

prepare_official() {
    bootstrap_env
    ensure_worktree
    sync_private_state
    build_official_repo
    echo "Official OpenClaw prepared:"
    echo "  code : $(official_code)"
    echo "  state: $(official_state)"
    echo "  ref  : $(official_ref)"
}

start_official() {
    local repo state port pid node_bin shell_cmd
    bootstrap_env
    repo="$(official_code)"
    state="$(official_state)"
    port="$(official_port)"
    node_bin="$(resolve_login_cmd node)"

    if [ ! -d "$repo" ]; then
        prepare_official
    fi

    pid="$(official_pid || true)"
    if [ -n "$pid" ] && health_check; then
        echo "Official OpenClaw already running on http://127.0.0.1:${port}"
        echo "PID: $pid"
        echo "Dashboard: $(dashboard_url)"
        return 0
    fi

    if [ -n "$pid" ]; then
        stop_official >/dev/null 2>&1 || true
    fi
    stop_listener

    : > "$OFFICIAL_LOG_FILE"
    mkdir -p "$HOME/Library/LaunchAgents"
    shell_cmd="set -a; if [ -f '${state}/.env' ]; then . '${state}/.env'; fi; set +a; exec '${node_bin:-node}' '${repo}/openclaw.mjs' gateway --bind loopback --port '${port}'"
    cat > "$OFFICIAL_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${OFFICIAL_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>${shell_cmd}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${repo}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>$(resolve_login_path)</string>
    <key>OPENCLAW_CONFIG_PATH</key>
    <string>${state}/openclaw.json</string>
    <key>OPENCLAW_STATE_DIR</key>
    <string>${state}</string>
    <key>OPENCLAW_GATEWAY_PORT</key>
    <string>${port}</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>StandardOutPath</key>
  <string>${OFFICIAL_LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${OFFICIAL_LOG_FILE}</string>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
EOF
    launchd_bootout "$OFFICIAL_LABEL" "$OFFICIAL_PLIST"
    launchd_bootstrap "$OFFICIAL_PLIST"
    launchd_kickstart "$OFFICIAL_LABEL"

    for _ in $(seq 1 40); do
        if health_check; then
            pid="$(listener_pid || true)"
            if [ -z "$pid" ]; then
                pid="$(official_pid || true)"
            fi
            if [ -n "$pid" ]; then
                echo "$pid" > "$OFFICIAL_PID_FILE"
            fi
            echo "Official OpenClaw validation gateway started."
            echo "  pid      : ${pid:-unknown}"
            echo "  health   : http://127.0.0.1:${port}/health"
            echo "  dashboard: $(dashboard_url)"
            return 0
        fi
        sleep 1
    done

    echo "Official OpenClaw validation gateway failed to start." >&2
    echo "Log: $OFFICIAL_LOG_FILE" >&2
    tail -n 120 "$OFFICIAL_LOG_FILE" >&2 || true
    exit 1
}

stop_official() {
    local pid listener
    launchd_bootout "$OFFICIAL_LABEL" "$OFFICIAL_PLIST"
    pid="$(official_pid || true)"
    kill_listener_pid "$pid"
    listener="$(listener_pid)"
    if [ -n "$listener" ] && [ "$listener" != "$pid" ]; then
        kill_listener_pid "$listener"
    fi
    rm -f "$OFFICIAL_PID_FILE"
    echo "Stopped official OpenClaw validation gateway."
}

status_official() {
    local repo pid remote_head local_head
    bootstrap_env
    repo="$(official_code)"
    pid="$(official_pid || true)"
    if [ -d "$repo/.git" ] || [ -f "$repo/.git" ]; then
        local_head="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || true)"
        remote_head="$(git -C "$repo" rev-parse --short "$(official_ref)" 2>/dev/null || true)"
    fi
    echo "Official OpenClaw"
    echo "  code      : $repo"
    echo "  state     : $(official_state)"
    echo "  port      : $(official_port)"
    echo "  head      : ${local_head:-unknown}"
    echo "  target    : ${remote_head:-unknown}"
    if [ -n "$pid" ]; then
        echo "  pid       : $pid"
    else
        echo "  pid       : not running"
    fi
    if health_check; then
        echo "  health    : ok"
        echo "  dashboard : $(dashboard_url)"
    else
        echo "  health    : unavailable"
    fi
}

install_schedule() {
    local hour minute script_path
    bootstrap_env
    mkdir -p "$HOME/Library/LaunchAgents"
    hour="$(official_update_hour)"
    minute="$(official_update_minute)"
    script_path="$BASE_DIR/manage_official_openclaw.sh"
    cat > "$SCHEDULE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SCHEDULE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${script_path}</string>
    <string>update</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>$(resolve_login_path)</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/openclaw-official-update.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/openclaw-official-update.stderr.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
EOF
    launchctl bootout "${LAUNCH_DOMAIN}/${SCHEDULE_LABEL}" 2>/dev/null || true
    launchctl bootstrap "$LAUNCH_DOMAIN" "$SCHEDULE_PLIST"
    echo "Installed official OpenClaw update schedule at ${hour}:$(printf '%02d' "$minute")."
}

schedule_status() {
    if [ ! -f "$SCHEDULE_PLIST" ]; then
        echo "Official OpenClaw update schedule not installed."
        return 0
    fi
    launchctl print "${LAUNCH_DOMAIN}/${SCHEDULE_LABEL}" 2>/dev/null | sed -n '1,200p' || cat "$SCHEDULE_PLIST"
}

update_official() {
    prepare_official
}

usage() {
    cat <<EOF
Usage: ./manage_official_openclaw.sh <command>

Commands:
  prepare           Prepare official latest worktree, sync config, install deps, build
  start             Start isolated official validation gateway
  stop              Stop isolated official validation gateway
  status            Show isolated official validation status
  update            Refresh official latest worktree and rebuilt validation env
  install-schedule  Install daily auto-update launchd job
  schedule-status   Show auto-update launchd status
EOF
}

case "${1:-}" in
    prepare) prepare_official ;;
    start) start_official ;;
    stop) stop_official ;;
    status) status_official ;;
    update) update_official ;;
    install-schedule) install_schedule ;;
    schedule-status) schedule_status ;;
    *) usage; exit 1 ;;
esac
