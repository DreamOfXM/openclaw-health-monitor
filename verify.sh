#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"

discover_dashboard_url() {
    if [ -n "${DASHBOARD_URL:-}" ]; then
        echo "$DASHBOARD_URL"
        return 0
    fi
    local port
    for port in $(seq 8080 8089); do
        if curl -fsS "http://127.0.0.1:${port}/api/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "gateway_healthy" in d and "incident_summary" in d and "memory_summary" in d' >/dev/null 2>&1; then
            echo "http://127.0.0.1:${port}/api/status"
            return 0
        fi
    done
    return 1
}

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtualenv not found. Run ./install.sh first." >&2
    exit 1
fi

TMP_JSON="$(mktemp)"
cleanup() {
    rm -f "$TMP_JSON"
}
trap cleanup EXIT

DASHBOARD_URL="$(discover_dashboard_url || true)"
if [ -z "$DASHBOARD_URL" ]; then
    echo "Dashboard API not reachable on ports 8080-8089." >&2
    exit 1
fi

echo "1. Checking dashboard API..."
if ! curl -fsS "$DASHBOARD_URL" -o "$TMP_JSON"; then
    echo "Dashboard API not reachable: $DASHBOARD_URL" >&2
    exit 1
fi

echo "2. Validating runtime status..."
"$VENV_PYTHON" - "$TMP_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failures: list[str] = []

metrics = payload.get("metrics") or {}
memory_summary = payload.get("memory_summary") or {}
incident = payload.get("incident_summary") or {}
recent_events = payload.get("recent_events") or []

def require(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)

require("cpu" in metrics, "missing metrics.cpu")
require("mem_used" in metrics, "missing metrics.mem_used")
require("mem_total" in metrics, "missing metrics.mem_total")
require("summary" in memory_summary, "missing memory_summary.summary")
require("items" in memory_summary, "missing memory_summary.items")
require("process_coverage_percent" in memory_summary, "missing memory_summary.process_coverage_percent")
require("headline" in incident, "missing incident_summary.headline")
require("action" in incident, "missing incident_summary.action")
require("gateway_healthy" in payload, "missing gateway_healthy")

print(f"Gateway healthy: {payload.get('gateway_healthy')}")
print(f"CPU: {metrics.get('cpu')}%")
print(f"Memory: {metrics.get('mem_used')}G / {metrics.get('mem_total')}G")
print(f"Memory attribution: {memory_summary.get('summary', '-')}")
print(f"Incident headline: {incident.get('headline', '-')}")
print(f"Recent events: {len(recent_events)}")

items = memory_summary.get("items") or []
for item in items[:4]:
    print(f"  - {item.get('name')}: {item.get('value_gb')}G")

if recent_events:
    latest = recent_events[0]
    print(f"Latest event: {latest.get('type')} | {latest.get('message')}")

if failures:
    print("")
    print("Verification failed:")
    for failure in failures:
        print(f" - {failure}")
    sys.exit(1)

print("")
print("Verification passed.")
PY

echo "3. Checking local processes..."
pgrep -fl "guardian.py|openclaw.*gateway" || true

echo ""
echo "Dashboard API: $DASHBOARD_URL"
echo "Done. If you just restarted, also open ${DASHBOARD_URL%/api/status} and confirm:"
echo "- '问题定位' has content"
echo "- '最近异常 / 进度' renders"
echo "- '内存归因' shows Top 15 plus system items"
