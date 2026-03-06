#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
GUARDIAN_PID_FILE="$LOG_DIR/guardian.pid"
DASHBOARD_PID_FILE="$LOG_DIR/dashboard.pid"

stop_pid() {
    local pid="$1"
    local label="$2"
    if [ -z "$pid" ]; then
        return 1
    fi
    if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping $label (PID: $pid)..."
        kill "$pid"
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            echo "$label still running, sending SIGKILL..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "$label stopped."
        return 0
    fi
    return 1
}

find_first_pid() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | head -n 1 || true
}

guardian_stopped=0
dashboard_stopped=0

if [ -f "$GUARDIAN_PID_FILE" ]; then
    guardian_pid="$(cat "$GUARDIAN_PID_FILE" 2>/dev/null || true)"
    if stop_pid "$guardian_pid" "Guardian"; then
        guardian_stopped=1
    fi
    rm -f "$GUARDIAN_PID_FILE"
fi

if [ "$guardian_stopped" -eq 0 ]; then
    guardian_pid="$(find_first_pid "$BASE_DIR/guardian.py")"
    if stop_pid "$guardian_pid" "Guardian"; then
        guardian_stopped=1
    fi
fi

dashboard_pid=""
if [ -f "$DASHBOARD_PID_FILE" ]; then
    dashboard_pid="$(cat "$DASHBOARD_PID_FILE" 2>/dev/null || true)"
fi
if [ -z "$dashboard_pid" ]; then
    dashboard_pid="$(find_first_pid "$BASE_DIR/dashboard.py")"
fi
if stop_pid "$dashboard_pid" "Dashboard"; then
    dashboard_stopped=1
fi
rm -f "$DASHBOARD_PID_FILE"

if [ "$guardian_stopped" -eq 0 ]; then
    echo "Guardian not running."
fi

if [ "$dashboard_stopped" -eq 0 ]; then
    echo "Dashboard not running."
fi

echo "Gateway not touched."
