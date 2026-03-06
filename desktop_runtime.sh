#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"
LOG_DIR="$BASE_DIR/logs"
GUARDIAN_PID_FILE="$LOG_DIR/guardian.pid"
DASHBOARD_PID_FILE="$LOG_DIR/dashboard.pid"

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

is_running() {
    case "${1:-}" in
        guardian)
            [ -n "$(guardian_pid)" ]
            ;;
        dashboard)
            [ -n "$(dashboard_pid)" ]
            ;;
        *)
            echo "Unknown service: ${1:-}" >&2
            return 2
            ;;
    esac
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

status_json() {
    local guardian dashboard
    guardian="$(guardian_pid || true)"
    dashboard="$(dashboard_pid || true)"
    printf '{"guardian":"%s","dashboard":"%s"}\n' "${guardian:-}" "${dashboard:-}"
}

case "${1:-}" in
    is-running)
        is_running "${2:-}"
        ;;
    start)
        case "${2:-}" in
            guardian) start_guardian ;;
            dashboard) start_dashboard ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    stop)
        case "${2:-}" in
            guardian) stop_guardian ;;
            dashboard) stop_dashboard ;;
            *) echo "Unknown service: ${2:-}" >&2; exit 2 ;;
        esac
        ;;
    status-json)
        status_json
        ;;
    *)
        echo "Usage: $0 {is-running|start|stop|status-json} <guardian|dashboard>" >&2
        exit 2
        ;;
esac
