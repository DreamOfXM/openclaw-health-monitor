#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$BASE_DIR/desktop_runtime.sh"

discover_dashboard_url() {
    local port
    for port in $(seq 8080 8089); do
        if curl -fsS "http://127.0.0.1:${port}/api/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "gateway_healthy" in d and "incident_summary" in d and "memory_summary" in d' >/dev/null 2>&1; then
            echo "http://127.0.0.1:${port}"
            return 0
        fi
    done
    return 1
}

json="$("$RUNTIME" status-json)"
gateway_pid="$(python3 - <<'PY' "$json"
import json, sys
print((json.loads(sys.argv[1]).get("gateway") or "").strip())
PY
)"
guardian_pid="$(python3 - <<'PY' "$json"
import json, sys
print((json.loads(sys.argv[1]).get("guardian") or "").strip())
PY
)"
dashboard_pid="$(python3 - <<'PY' "$json"
import json, sys
print((json.loads(sys.argv[1]).get("dashboard") or "").strip())
PY
)"
dashboard_url="$(discover_dashboard_url || true)"

echo "OpenClaw Health Monitor status"
echo "Project dir: $BASE_DIR"
echo
echo "Gateway  : ${gateway_pid:-not running}"
echo "Guardian : ${guardian_pid:-not running}"
echo "Dashboard: ${dashboard_pid:-not running}"
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
