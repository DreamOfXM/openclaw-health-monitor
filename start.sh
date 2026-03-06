#!/bin/bash
set -euo pipefail

# OpenClaw 健康监控中心 - 启动脚本
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"
LOG_DIR="$BASE_DIR/logs"
GUARDIAN_PID_FILE="$LOG_DIR/guardian.pid"

find_running_pid() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | head -n 1 || true
}

cd "$BASE_DIR"
mkdir -p "$LOG_DIR"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtualenv not found. Run ./install.sh first." >&2
    exit 1
fi

EXISTING_GUARDIAN_PID="$(find_running_pid "$BASE_DIR/guardian.py")"
if [ -n "$EXISTING_GUARDIAN_PID" ]; then
    echo "Guardian 已在运行，PID: $EXISTING_GUARDIAN_PID"
    echo "$EXISTING_GUARDIAN_PID" > "$GUARDIAN_PID_FILE"
else
    echo "启动 Guardian 守护进程..."
    "$VENV_PYTHON" "$BASE_DIR/guardian.py" >> "$LOG_DIR/guardian.log" 2>&1 &
    GUARDIAN_PID=$!
    echo "$GUARDIAN_PID" > "$GUARDIAN_PID_FILE"
    echo "Guardian PID: $GUARDIAN_PID"
    sleep 1
    if ! kill -0 "$GUARDIAN_PID" 2>/dev/null; then
        echo "Guardian 启动失败，请检查 $LOG_DIR/guardian.log" >&2
        exit 1
    fi
fi

EXISTING_DASHBOARD_PID="$(find_running_pid "$BASE_DIR/dashboard.py")"
if [ -n "$EXISTING_DASHBOARD_PID" ]; then
    echo "检测到已有 Dashboard 进程，PID: $EXISTING_DASHBOARD_PID"
    echo "如需切换到当前版本，请先停止旧进程后再运行 ./start.sh" >&2
    exit 1
fi

echo "启动健康监控仪表盘..."
echo "日志目录: $LOG_DIR"
"$VENV_PYTHON" "$BASE_DIR/dashboard.py"
