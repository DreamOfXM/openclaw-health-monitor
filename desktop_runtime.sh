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

mkdir -p "$LOG_DIR"

find_pid() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | head -n 1 || true
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
    read_pid_file "$GUARDIAN_PID_FILE" || find_pid "$BASE_DIR/guardian.py"
}

dashboard_pid() {
    read_pid_file "$DASHBOARD_PID_FILE" || find_pid "$BASE_DIR/dashboard.py"
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

gateway_workdir() {
    local value
    value="$(config_value OPENCLAW_CODE)"
    printf '%s\n' "${value:-$HOME/openclaw-workspace/openclaw}"
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
        all)
            [ -n "$(gateway_pid)" ] && [ -n "$(guardian_pid)" ] && [ -n "$(dashboard_pid)" ]
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
    local workdir
    workdir="$(gateway_workdir)"
    if [ ! -d "$workdir" ]; then
        echo "Gateway workspace not found: $workdir" >&2
        return 1
    fi
    (
        cd "$workdir"
        openclaw gateway run >> "$LOG_DIR/gateway.log" 2>&1
    ) &
    pid=$!
    echo "$pid" > "$GATEWAY_PID_FILE"
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Gateway failed to start" >&2
        return 1
    fi
    echo "$pid"
}

start_guardian() {
    local pid
    pid="$(guardian_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "Virtualenv not found: $VENV_PYTHON" >&2
        return 1
    fi
    "$VENV_PYTHON" "$BASE_DIR/guardian.py" >> "$LOG_DIR/guardian.log" 2>&1 &
    pid=$!
    echo "$pid" > "$GUARDIAN_PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Guardian failed to start" >&2
        return 1
    fi
    echo "$pid"
}

start_dashboard() {
    local pid
    pid="$(dashboard_pid || true)"
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "Virtualenv not found: $VENV_PYTHON" >&2
        return 1
    fi
    "$VENV_PYTHON" "$BASE_DIR/dashboard.py" >> "$LOG_DIR/dashboard.stdout.log" 2>&1 &
    pid=$!
    echo "$pid" > "$DASHBOARD_PID_FILE"
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Dashboard failed to start" >&2
        return 1
    fi
    echo "$pid"
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
    pid="$(guardian_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    rm -f "$GUARDIAN_PID_FILE"
}

stop_dashboard() {
    local pid
    pid="$(dashboard_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    rm -f "$DASHBOARD_PID_FILE"
}

stop_gateway() {
    local pid
    pid="$(gateway_pid || true)"
    if [ -n "$pid" ]; then
        stop_pid "$pid"
    fi
    rm -f "$GATEWAY_PID_FILE"
}

start_all() {
    start_gateway >/dev/null
    start_guardian >/dev/null
    start_dashboard >/dev/null
}

stop_all() {
    stop_dashboard || true
    stop_guardian || true
    stop_gateway || true
}

status_json() {
    local guardian dashboard gateway
    gateway="$(gateway_pid || true)"
    guardian="$(guardian_pid || true)"
    dashboard="$(dashboard_pid || true)"
    printf '{"gateway":"%s","guardian":"%s","dashboard":"%s"}\n' "${gateway:-}" "${guardian:-}" "${dashboard:-}"
}

case "${1:-}" in
    is-running)
        is_running "${2:-}"
        ;;
    start)
        case "${2:-}" in
            gateway) start_gateway ;;
            guardian) start_guardian ;;
            dashboard) start_dashboard ;;
            all) start_all ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    stop)
        case "${2:-}" in
            gateway) stop_gateway ;;
            guardian) stop_guardian ;;
            dashboard) stop_dashboard ;;
            all) stop_all ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    status-json)
        status_json
        ;;
    *)
        echo "Usage: $0 {is-running|start|stop|status-json} <gateway|guardian|dashboard|all>" >&2
        exit 2
        ;;
esac
