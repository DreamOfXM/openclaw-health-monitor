#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"
LOG_DIR="$BASE_DIR/logs"
GUARDIAN_PID_FILE="$LOG_DIR/guardian.pid"
DASHBOARD_PID_FILE="$LOG_DIR/dashboard.pid"
GATEWAY_PID_FILE="$LOG_DIR/gateway.pid"
TRACKED_CONFIG="$BASE_DIR/config.conf"
LOCAL_CONFIG="$BASE_DIR/config.local.conf"
LAUNCH_DOMAIN="gui/$(id -u)"
GUARDIAN_LABEL="ai.openclaw.guardian"
DASHBOARD_LABEL="ai.openclaw.dashboard"
LEGACY_MONITOR_LABEL="ai.openclaw.health-monitor"
WATCHER_LABEL="ai.openclaw.task-closure-watcher"
EVOLUTION_LABEL="ai.openclaw.self-evolution"
GUARDIAN_PLIST="$HOME/Library/LaunchAgents/${GUARDIAN_LABEL}.plist"
DASHBOARD_PLIST="$HOME/Library/LaunchAgents/${DASHBOARD_LABEL}.plist"
WATCHER_PLIST="$HOME/Library/LaunchAgents/${WATCHER_LABEL}.plist"
EVOLUTION_PLIST="$HOME/Library/LaunchAgents/${EVOLUTION_LABEL}.plist"
LEGACY_MONITOR_PLIST="$HOME/Library/LaunchAgents/${LEGACY_MONITOR_LABEL}.plist"
PYTHON_BIN=""
OPENCLAW_BIN=""
NODE_BIN=""

mkdir -p "$LOG_DIR"

find_pid() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | head -n 1 || true
}

launchd_pid() {
    local label="$1"
    launchctl print "${LAUNCH_DOMAIN}/${label}" 2>/dev/null | awk -F'= ' '/pid = / {print $2; exit}' | tr -d ';' || true
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

launchd_loaded() {
    local label="$1"
    launchctl print "${LAUNCH_DOMAIN}/${label}" >/dev/null 2>&1
}

resolve_cmd_from_login_shell() {
    local cmd_name="$1"
    /bin/zsh -lc "command -v $cmd_name" 2>/dev/null | head -n 1 || true
}

resolve_python_bin() {
    local candidate=""
    if [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c "import flask, requests" >/dev/null 2>&1; then
        echo "$VENV_PYTHON"
        return 0
    fi

    candidate="$(resolve_cmd_from_login_shell python3)"
    if [ -n "$candidate" ] && "$candidate" -c "import flask, requests" >/dev/null 2>&1; then
        echo "$candidate"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1 && python3 -c "import flask, requests" >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    return 1
}

resolve_openclaw_bin() {
    local candidate=""
    candidate="$(resolve_cmd_from_login_shell openclaw)"
    if [ -n "$candidate" ]; then
        echo "$candidate"
        return 0
    fi
    if command -v openclaw >/dev/null 2>&1; then
        command -v openclaw
        return 0
    fi
    return 1
}

run_gateway_service_cmd() {
    local subcmd="$1"
    bootstrap_env
    if [ -z "$OPENCLAW_BIN" ]; then
        return 1
    fi
    "$OPENCLAW_BIN" gateway "$subcmd"
}

bootstrap_env() {
    local login_path=""
    login_path="$(/bin/zsh -lc 'printf %s "$PATH"' 2>/dev/null || true)"
    if [ -n "$login_path" ]; then
        PATH="$login_path:$PATH"
        export PATH
    fi
    if [ -z "$PYTHON_BIN" ]; then
        PYTHON_BIN="$(resolve_python_bin || true)"
    fi
    if [ -z "$OPENCLAW_BIN" ]; then
        OPENCLAW_BIN="$(resolve_openclaw_bin || true)"
    fi
    if [ -z "$NODE_BIN" ]; then
        NODE_BIN="$(resolve_cmd_from_login_shell node || true)"
        if [ -z "$NODE_BIN" ] && command -v node >/dev/null 2>&1; then
            NODE_BIN="$(command -v node)"
        fi
    fi
    if [ -n "$NODE_BIN" ]; then
        PATH="$(dirname "$NODE_BIN"):$PATH"
        export PATH
    fi
}

plist_python_bin() {
    bootstrap_env
    if [ -n "${PYTHON_BIN:-}" ]; then
        printf '%s\n' "$PYTHON_BIN"
        return 0
    fi
    if [ -x "$VENV_PYTHON" ]; then
        printf '%s\n' "$VENV_PYTHON"
        return 0
    fi
    printf '%s\n' "python3"
}

raise_nofile_limit() {
    local current hard target
    current="$(ulimit -Sn 2>/dev/null || true)"
    hard="$(ulimit -Hn 2>/dev/null || true)"
    target=65536
    if [ -z "$current" ] || [ -z "$hard" ]; then
        return 0
    fi
    if ! [[ "$current" =~ ^[0-9]+$ ]]; then
        return 0
    fi
    if [[ "$hard" =~ ^[0-9]+$ ]] && [ "$hard" -lt "$target" ]; then
        target="$hard"
    fi
    if [ "$current" -lt "$target" ]; then
        ulimit -Sn "$target" 2>/dev/null || true
    fi
}

read_pid_file() {
    local pid_file="$1"
    if [ ! -f "$pid_file" ]; then
        return 1
    fi
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    printf '%s\n' "$pid"
}

guardian_pid() {
    launchd_pid "$GUARDIAN_LABEL" || read_pid_file "$GUARDIAN_PID_FILE" || find_pid "$BASE_DIR/guardian.py"
}

dashboard_pid() {
    launchd_pid "$DASHBOARD_LABEL" || read_pid_file "$DASHBOARD_PID_FILE" || listener_pid "$(dashboard_port)" || find_pid "$BASE_DIR/dashboard_v2/app.py|$BASE_DIR/dashboard_backend.py"
}

watcher_pid() {
    launchd_pid "$WATCHER_LABEL" || find_pid "$BASE_DIR/scripts/task_closure_watcher.py"
}

dashboard_reachable() {
    env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost curl --noproxy '*' -fsS "http://127.0.0.1:$(dashboard_port)/api/status" >/dev/null 2>&1
}

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

gateway_port() {
    local value
    value="$(config_value GATEWAY_PORT)"
    printf '%s\n' "${value:-18789}"
}

dashboard_port() {
    printf '%s\n' "${DASHBOARD_PORT:-8080}"
}

gateway_workdir() {
    local value
    value="$(config_value OPENCLAW_CODE)"
    printf '%s\n' "${value:-$HOME/openclaw-workspace/openclaw}"
}

active_openclaw_env() {
    local db_path value env_id
    db_path="$BASE_DIR/data/monitor.db"
    if command -v sqlite3 >/dev/null 2>&1 && [ -f "$db_path" ]; then
        value="$(sqlite3 "$db_path" "SELECT value_json FROM kv_state WHERE namespace='runtime' AND key='active_openclaw_env' LIMIT 1;" 2>/dev/null || true)"
        env_id="$(printf '%s' "$value" | sed -n 's/.*"env_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
        if [ -n "$env_id" ]; then
            printf '%s\n' "$env_id"
            return 0
        fi
    fi
    printf 'primary\n'
}

purity_gate_status() {
    local env_id db_path value ok reasons
    env_id="$(active_openclaw_env)"
    db_path="$BASE_DIR/data/monitor.db"
    if ! command -v sqlite3 >/dev/null 2>&1 || [ ! -f "$db_path" ]; then
        printf 'unknown\n'
        return 0
    fi
    value="$(sqlite3 "$db_path" "SELECT value_json FROM kv_state WHERE namespace='runtime' AND key='main_closure_purity_gate:${env_id}' LIMIT 1;" 2>/dev/null || true)"
    if [ -z "$value" ]; then
        printf 'unknown\n'
        return 0
    fi
    ok="$(sqlite3 "$db_path" "SELECT CASE json_extract(value_json, '$.ok') WHEN 1 THEN 'true' WHEN 0 THEN 'false' ELSE '' END FROM kv_state WHERE namespace='runtime' AND key='main_closure_purity_gate:${env_id}' LIMIT 1;" 2>/dev/null || true)"
    if [ "$ok" = "true" ]; then
        printf 'ok\n'
        return 0
    fi
    reasons="$(sqlite3 "$db_path" "SELECT COALESCE(group_concat(value, ' '), '') FROM kv_state, json_each(value_json, '$.reasons') WHERE namespace='runtime' AND key='main_closure_purity_gate:${env_id}';" 2>/dev/null || true)"
    printf 'failed:%s\n' "${reasons:-main_closure_purity_gate_failed}"
}

listener_pid() {
    local port="$1"
    lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

gateway_pid() {
    local pid
    pid="$(read_pid_file "$GATEWAY_PID_FILE" || true)"
    if [ -n "$pid" ]; then
        printf '%s\n' "$pid"
        return 0
    fi
    listener_pid "$(gateway_port)"
}

active_gateway_pid() {
    gateway_pid
}

ensure_launch_agents_dir() {
    mkdir -p "$HOME/Library/LaunchAgents"
}

install_guardian_launch_agent() {
    local python_bin escaped_path escaped_base
    ensure_launch_agents_dir
    python_bin="$(plist_python_bin)"
    cat > "$GUARDIAN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${GUARDIAN_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${BASE_DIR}/guardian.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>15</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/guardian.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/guardian.launchd.err.log</string>
</dict>
</plist>
EOF
}

install_dashboard_launch_agent() {
    local python_bin
    ensure_launch_agents_dir
    python_bin="$(plist_python_bin)"
    cat > "$DASHBOARD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${DASHBOARD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${BASE_DIR}/dashboard_v2/app.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>DASHBOARD_PORT</key>
    <string>$(dashboard_port)</string>
    <key>DASHBOARD_HOST</key>
    <string>127.0.0.1</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>15</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/dashboard.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/dashboard.launchd.err.log</string>
</dict>
</plist>
EOF
}

install_watcher_launch_agent() {
    local python_bin
    ensure_launch_agents_dir
    python_bin="$(plist_python_bin)"
    cat > "$WATCHER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${WATCHER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${BASE_DIR}/scripts/task_closure_watcher.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>45</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/task-closure-watcher.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/task-closure-watcher.launchd.err.log</string>
</dict>
</plist>
EOF
}

install_evolution_launch_agent() {
    local python_bin
    ensure_launch_agents_dir
    python_bin="$(plist_python_bin)"
    cat > "$EVOLUTION_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${EVOLUTION_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${BASE_DIR}/scripts/self_evolution_engine.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
    <key>no_proxy</key>
    <string>127.0.0.1,localhost</string>
    <key>OPENCLAW_MODEL_DECIDER_CMD</key>
    <string>${python_bin} ${BASE_DIR}/scripts/external_model_decider.py</string>
    <key>OPENCLAW_MODEL_DECIDER_REQUIRED</key>
    <string>1</string>
    <key>OPENCLAW_SELF_EVOLUTION_AUTO_COMMIT</key>
    <string>1</string>
    <key>OPENCLAW_SELF_EVOLUTION_AUTO_PUSH_PR</key>
    <string>1</string>
    <key>OPENCLAW_SELF_EVOLUTION_REMOTE</key>
    <string>origin</string>
    <key>OPENCLAW_SELF_EVOLUTION_BASE</key>
    <string>main</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>86400</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/self-evolution.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/self-evolution.launchd.err.log</string>
</dict>
</plist>
EOF
}

install_watcher_launch_agent() {
    local python_bin
    ensure_launch_agents_dir
    python_bin="$(plist_python_bin)"
    cat > "$WATCHER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${WATCHER_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${python_bin}</string>
    <string>${BASE_DIR}/scripts/task_closure_watcher.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${BASE_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/task-closure-watcher.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/task-closure-watcher.launchd.err.log</string>
</dict>
</plist>
EOF
}

is_running() {
    case "${1:-}" in
        gateway)
            [ -n "$(gateway_pid)" ]
            ;;
        guardian)
            [ -n "$(guardian_pid)" ]
            ;;
        dashboard)
            [ -n "$(dashboard_pid)" ]
            ;;
        watcher)
            [ -n "$(watcher_pid)" ] || launchd_loaded "$WATCHER_LABEL"
            ;;
        all)
            [ -n "$(gateway_pid)" ] && [ -n "$(guardian_pid)" ] && [ -n "$(dashboard_pid)" ] && [ -n "$(watcher_pid)" ]
            ;;
        *)
            echo "Unknown service: ${1:-}" >&2
            return 2
            ;;
    esac
}

start_gateway() {
    local pid
    pid="$(gateway_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    if [ "${ALLOW_PURITY_GATE_BYPASS:-0}" != "1" ]; then
        local purity_status
        purity_status="$(purity_gate_status)"
        if [ "${purity_status#failed:}" != "$purity_status" ]; then
            echo "Refusing to start Gateway: main closure purity gate failed (${purity_status#failed:})" >&2
            return 1
        fi
    fi
    local workdir
    workdir="$(gateway_workdir)"
    if [ ! -d "$workdir" ]; then
        echo "Gateway workspace not found: $workdir" >&2
        return 1
    fi
    bootstrap_env
    if [ -z "$OPENCLAW_BIN" ]; then
        echo "openclaw command not found" >&2
        return 1
    fi
    run_gateway_service_cmd install >> "$LOG_DIR/gateway.log" 2>&1 || true
    local gateway_plist
    gateway_plist="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
    if [ ! -f "$gateway_plist" ]; then
        echo "Gateway launch agent not found" >&2
        return 1
    fi
    launchd_bootout "ai.openclaw.gateway" "$gateway_plist"
    launchd_bootstrap "$gateway_plist" >> "$LOG_DIR/gateway.log" 2>&1 || true
    launchd_kickstart "ai.openclaw.gateway" >> "$LOG_DIR/gateway.log" 2>&1 || true
    sleep 3
    pid="$(gateway_pid || true)"
    if [ -z "$pid" ]; then
        run_gateway_service_cmd start >> "$LOG_DIR/gateway.log" 2>&1 || true
        sleep 3
        pid="$(gateway_pid || true)"
    fi
    if [ -z "$pid" ]; then
        echo "Gateway failed to start" >&2
        return 1
    fi
    echo "$pid" > "$GATEWAY_PID_FILE"
    echo "$pid"
}

start_guardian() {
    local pid
    pid="$(guardian_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    bootstrap_env
    if [ -z "$PYTHON_BIN" ]; then
        echo "Python with flask+requests not found" >&2
        return 1
    fi
    install_guardian_launch_agent
    [ -f "$LEGACY_MONITOR_PLIST" ] && launchd_bootout "$LEGACY_MONITOR_LABEL" "$LEGACY_MONITOR_PLIST"
    launchd_bootout "$GUARDIAN_LABEL" "$GUARDIAN_PLIST"
    launchd_bootstrap "$GUARDIAN_PLIST"
    launchd_kickstart "$GUARDIAN_LABEL"
    sleep 2
    pid="$(guardian_pid || true)"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "Guardian failed to start" >&2
        return 1
    fi
    echo "$pid" > "$GUARDIAN_PID_FILE"
    echo "$pid"
}

start_dashboard() {
    local pid
    pid="$(dashboard_pid || true)"
    if [ -n "$pid" ]; then
        if dashboard_reachable; then
            echo "$pid"
            return 0
        fi
        stop_pid "$pid" || true
        rm -f "$DASHBOARD_PID_FILE"
    fi
    bootstrap_env
    if [ -z "$PYTHON_BIN" ]; then
        echo "Python with flask+requests not found" >&2
        return 1
    fi
    install_dashboard_launch_agent
    launchd_bootout "$DASHBOARD_LABEL" "$DASHBOARD_PLIST"
    launchd_bootstrap "$DASHBOARD_PLIST"
    launchd_kickstart "$DASHBOARD_LABEL"
    sleep 3
    pid="$(dashboard_pid || true)"
    if [ -z "$pid" ] || ! dashboard_reachable; then
        echo "Dashboard failed to start" >&2
        return 1
    fi
    echo "$pid" > "$DASHBOARD_PID_FILE"
    echo "$pid"
}

start_watcher() {
    local pid
    pid="$(watcher_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    bootstrap_env
    if [ -z "$PYTHON_BIN" ]; then
        echo "Python with flask+requests not found" >&2
        return 1
    fi
    install_watcher_launch_agent
    launchd_bootout "$WATCHER_LABEL" "$WATCHER_PLIST"
    launchd_bootstrap "$WATCHER_PLIST"
    launchd_kickstart "$WATCHER_LABEL" || true
    sleep 2
    pid="$(watcher_pid || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
    fi
    if launchd_loaded "$WATCHER_LABEL"; then
        echo "launchd:${WATCHER_LABEL}"
        return 0
    fi
    echo "Watcher failed to start" >&2
    return 1
}

evolution_pid() {
    launchd_pid "$EVOLUTION_LABEL" || find_pid "$BASE_DIR/scripts/self_evolution_engine.py"
}

start_evolution() {
    local pid
    pid="$(evolution_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    bootstrap_env
    if [ -z "$PYTHON_BIN" ]; then
        echo "Python with flask+requests not found" >&2
        return 1
    fi
    install_evolution_launch_agent
    launchd_bootout "$EVOLUTION_LABEL" "$EVOLUTION_PLIST"
    launchd_bootstrap "$EVOLUTION_PLIST"
    launchd_kickstart "$EVOLUTION_LABEL" || true
    sleep 2
    if launchd_loaded "$EVOLUTION_LABEL"; then
        echo "launchd:${EVOLUTION_LABEL}"
        return 0
    fi
    echo "Evolution failed to start" >&2
    return 1
}

stop_evolution() {
    local pid
    launchd_bootout "$EVOLUTION_LABEL" "$EVOLUTION_PLIST"
    pid="$(evolution_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid" || true
    fi
    pkill -f "$BASE_DIR/scripts/self_evolution_engine.py" 2>/dev/null || true
}

stop_pid() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
}

stop_guardian() {
    local pid
    launchd_bootout "$GUARDIAN_LABEL" "$GUARDIAN_PLIST"
    pid="$(guardian_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    pkill -f "$BASE_DIR/guardian.py" 2>/dev/null || true
    rm -f "$GUARDIAN_PID_FILE"
}

stop_dashboard() {
    local pid listener
    launchd_bootout "$DASHBOARD_LABEL" "$DASHBOARD_PLIST"
    pid="$(dashboard_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    listener="$(listener_pid "$(dashboard_port)")"
    if [ -n "$listener" ]; then
        stop_pid "$listener" || true
    fi
    pkill -f "$BASE_DIR/dashboard_v2/app.py" 2>/dev/null || true
    pkill -f "$BASE_DIR/dashboard_backend.py" 2>/dev/null || true
    rm -f "$DASHBOARD_PID_FILE"
}

stop_watcher() {
    local pid
    launchd_bootout "$WATCHER_LABEL" "$WATCHER_PLIST"
    pid="$(watcher_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid" || true
    fi
    pkill -f "$BASE_DIR/scripts/task_closure_watcher.py" 2>/dev/null || true
}

stop_gateway() {
    local pid listener
    local gateway_plist
    gateway_plist="$HOME/Library/LaunchAgents/ai.openclaw.gateway.plist"
    launchd_bootout "ai.openclaw.gateway" "$gateway_plist"
    run_gateway_service_cmd stop >> "$LOG_DIR/gateway.log" 2>&1 || true
    sleep 1
    pid="$(gateway_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    listener="$(listener_pid "$(gateway_port)")"
    if [ -n "$listener" ]; then
        stop_pid "$listener" || true
    fi
    rm -f "$GATEWAY_PID_FILE"
}

start_active_gateway() {
    start_gateway >/dev/null
}

stop_all_gateways() {
    stop_gateway || true
}

start_all() {
    bootstrap_env
    raise_nofile_limit
    start_active_gateway
    start_guardian >/dev/null
    start_dashboard >/dev/null
    start_watcher >/dev/null || true
}

stop_all() {
    stop_watcher || true
    stop_dashboard || true
    stop_guardian || true
    stop_all_gateways
}

status_json() {
    local guardian dashboard gateway watcher
    gateway="$(active_gateway_pid || true)"
    guardian="$(guardian_pid || true)"
    dashboard="$(dashboard_pid || true)"
    watcher="$(watcher_pid || true)"
    printf '{"gateway":"%s","guardian":"%s","dashboard":"%s","watcher":"%s"}\n' "${gateway:-}" "${guardian:-}" "${dashboard:-}" "${watcher:-}"
}

case "${1:-}" in
    is-running)
        is_running "${2:-}"
        ;;
    start)
        case "${2:-}" in
            gateway) start_active_gateway ;;
            guardian) start_guardian ;;
            dashboard) start_dashboard ;;
            watcher) start_watcher ;;
            all) start_all ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    stop)
        case "${2:-}" in
            gateway) stop_gateway ;;
            guardian) stop_guardian ;;
            dashboard) stop_dashboard ;;
            watcher) stop_watcher ;;
            all) stop_all ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    status-json)
        status_json
        ;;
    *)
        echo "Usage: $0 {is-running|start|stop|status-json} <gateway|guardian|dashboard|watcher|all>" >&2
        exit 2
        ;;
esac
