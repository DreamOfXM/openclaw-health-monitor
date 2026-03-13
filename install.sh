#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/.venv"
LOCAL_CONFIG="$BASE_DIR/config.local.conf"

echo "==> OpenClaw Health Monitor installer"
echo "Project dir: $BASE_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required." >&2
  exit 1
fi

if ! command -v openclaw >/dev/null 2>&1; then
  echo "Warning: openclaw command not found in PATH."
  echo "Install OpenClaw first, then rerun ./install.sh if needed."
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"

mkdir -p "$BASE_DIR/logs" "$BASE_DIR/change-logs" "$BASE_DIR/data" "$BASE_DIR/config"

if [ ! -f "$LOCAL_CONFIG" ]; then
  cat > "$LOCAL_CONFIG" <<'EOF'
# Local-only overrides. This file is gitignored.
DINGTALK_WEBHOOK=""
FEISHU_WEBHOOK=""
ENABLE_DESTRUCTIVE_RECOVERY=false
EOF
  echo "Created $LOCAL_CONFIG"
fi

echo
echo "Install complete."
echo "Start stack:     ./start.sh"
echo "Check status:    ./status.sh"
echo "Stop stack:      ./stop.sh"
echo "Dashboard URL:   http://127.0.0.1:8080 (or the first free port in 8080-8089)"
