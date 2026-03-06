#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$BASE_DIR/desktop_runtime.sh"
LOCAL_CURL=(env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost curl --noproxy '*')

if [ ! -x "$RUNTIME" ]; then
    echo "Missing runtime controller: $RUNTIME" >&2
    exit 1
fi

echo "Starting OpenClaw Health Monitor..."
"$RUNTIME" start all

dashboard_url=""
if "${LOCAL_CURL[@]}" -fsS "http://127.0.0.1:8080/api/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "gateway_healthy" in d and "incident_summary" in d and "memory_summary" in d' >/dev/null 2>&1; then
    dashboard_url="http://127.0.0.1:8080"
fi

echo
echo "Stack started."
"$BASE_DIR/status.sh"

if [ -n "$dashboard_url" ]; then
    echo
    echo "Opening $dashboard_url"
    if [ "${NO_OPEN_BROWSER:-0}" != "1" ]; then
        open "$dashboard_url" >/dev/null 2>&1 || true
    fi
fi
