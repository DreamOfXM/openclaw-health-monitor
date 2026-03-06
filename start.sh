#!/bin/bash
set -euo pipefail

# OpenClaw 健康监控中心 - 启动脚本
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"

cd "$BASE_DIR"
mkdir -p logs

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtualenv not found. Run ./install.sh first." >&2
    exit 1
fi

echo "启动 Guardian 守护进程..."
"$VENV_PYTHON" "$BASE_DIR/guardian.py" &
GUARDIAN_PID=$!
echo "Guardian PID: $GUARDIAN_PID"

echo "启动健康监控仪表盘..."
"$VENV_PYTHON" "$BASE_DIR/dashboard.py"
