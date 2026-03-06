#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARDIAN_PID_FILE="$BASE_DIR/logs/guardian.pid"

find_pid() {
    local pattern="$1"
    pgrep -f "$pattern" 2>/dev/null | head -n 1 || true
}

discover_dashboard_url() {
    local port
    for port in $(seq 8080 8089); do
        if curl -fsS "http://127.0.0.1:${port}/api/status" >/dev/null 2>&1; then
            echo "http://127.0.0.1:${port}"
            return 0
        fi
    done
    return 1
}

guardian_pid=""
if [ -f "$GUARDIAN_PID_FILE" ]; then
    guardian_pid="$(cat "$GUARDIAN_PID_FILE" 2>/dev/null || true)"
    if [ -n "$guardian_pid" ] && ! kill -0 "$guardian_pid" 2>/dev/null; then
        guardian_pid=""
    fi
fi
if [ -z "$guardian_pid" ]; then
    guardian_pid="$(find_pid "$BASE_DIR/guardian.py")"
fi

dashboard_pid="$(find_pid "$BASE_DIR/dashboard.py")"
gateway_pid="$(pgrep -f "openclaw.*gateway" 2>/dev/null | head -n 1 || true)"
dashboard_url="$(discover_dashboard_url || true)"

echo "OpenClaw Health Monitor status"
echo "Project dir: $BASE_DIR"
echo
echo "Guardian : ${guardian_pid:-not running}"
echo "Dashboard: ${dashboard_pid:-not running}"
echo "Gateway  : ${gateway_pid:-not running}"
echo "URL      : ${dashboard_url:-not reachable}"
echo

if [ -n "$dashboard_url" ]; then
    tmp_json="$(mktemp)"
    trap 'rm -f "$tmp_json"' EXIT
    if curl -fsS "$dashboard_url/api/status" -o "$tmp_json"; then
        python3 - "$tmp_json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
metrics = payload.get("metrics") or {}
incident = payload.get("incident_summary") or {}
memory = payload.get("memory_summary") or {}

print(f"Gateway healthy : {payload.get('gateway_healthy')}")
print(f"CPU / Memory    : {metrics.get('cpu')}% / {metrics.get('mem_used')}G / {metrics.get('mem_total')}G")
print(f"Incident        : {incident.get('headline', '-')}")
print(f"Action          : {incident.get('action', '-')}")
print(f"Memory summary  : {memory.get('summary', '-')}")
PY
    fi
fi
