#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$BASE_DIR/.venv/bin/python"
LOCAL_CONFIG="$BASE_DIR/config.local.conf"
TRACKED_CONFIG="$BASE_DIR/config.conf"

failures=0
db_path="$BASE_DIR/data/monitor.db"

ok() {
    echo "[OK] $1"
}

warn() {
    echo "[WARN] $1"
}

fail() {
    echo "[FAIL] $1"
    failures=$((failures + 1))
}

check_cmd() {
    local cmd="$1"
    local label="$2"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$label"
    else
        fail "$label"
    fi
}

active_openclaw_env() {
    local value env_id
    if command -v sqlite3 >/dev/null 2>&1 && [ -f "$db_path" ]; then
        value="$(sqlite3 "$db_path" "SELECT value_json FROM kv_state WHERE namespace='runtime' AND key='active_openclaw_env' LIMIT 1;" 2>/dev/null || true)"
        env_id="$(printf '%s' "$value" | sed -n 's/.*"env_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
        if [ -n "$env_id" ]; then
            printf '%s\n' "$env_id"
            return 0
        fi
    fi
    printf 'primary\n'
}

purity_gate_status() {
    local env_id value ok reasons
    env_id="$(active_openclaw_env)"
    if ! command -v sqlite3 >/dev/null 2>&1 || [ ! -f "$db_path" ]; then
        printf 'unknown\n'
        return 0
    fi
    value="$(sqlite3 "$db_path" "SELECT value_json FROM kv_state WHERE namespace='runtime' AND key='main_closure_purity_gate:${env_id}' LIMIT 1;" 2>/dev/null || true)"
    if [ -z "$value" ]; then
        printf 'unknown\n'
        return 0
    fi
    ok="$(printf '%s' "$value" | sed -n 's/.*"ok"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')"
    if [ "$ok" = "true" ]; then
        printf 'ok\n'
        return 0
    fi
    reasons="$(printf '%s' "$value" | sed -n 's/.*"reasons"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' | tr -d '"' | tr ',' ' ' | xargs 2>/dev/null || true)"
    printf 'failed:%s\n' "${reasons:-main_closure_purity_gate_failed}"
}

check_file() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ]; then
        ok "$label"
    else
        fail "$label"
    fi
}

check_listen_port() {
    local port="$1"
    local label="$2"
    if lsof -ti "tcp:$port" -sTCP:LISTEN >/dev/null 2>&1; then
        warn "$label already in use on tcp:$port"
    else
        ok "$label free on tcp:$port"
    fi
}

echo "==> OpenClaw Health Monitor preflight"
echo "Project dir: $BASE_DIR"
echo

check_cmd python3 "python3 available"
check_cmd curl "curl available"
check_cmd lsof "lsof available"
check_cmd openclaw "openclaw available"

check_file "$TRACKED_CONFIG" "tracked config present"
check_file "$LOCAL_CONFIG" "local config present"
check_file "$BASE_DIR/guardian.py" "guardian.py present"
check_file "$BASE_DIR/dashboard_backend.py" "dashboard_backend.py present"
check_file "$BASE_DIR/dashboard_v2/app.py" "dashboard_v2/app.py present"
check_file "$BASE_DIR/requirements.txt" "requirements.txt present"

if [ -x "$VENV_PYTHON" ]; then
    ok "virtualenv python present"
else
    fail "virtualenv python missing (.venv/bin/python)"
fi

echo
echo "==> Static checks"
if [ -x "$VENV_PYTHON" ]; then
    if "$VENV_PYTHON" -m py_compile "$BASE_DIR/guardian.py" "$BASE_DIR/dashboard_backend.py" "$BASE_DIR/dashboard_v2/app.py"; then
        ok "guardian.py, dashboard_backend.py, and dashboard_v2/app.py compile"
    else
        fail "python compile failed"
    fi
fi

echo
echo "==> Port checks"
check_listen_port 8080 "dashboard default port"
check_listen_port 8081 "dashboard fallback port"
check_listen_port 8082 "dashboard fallback port"

gateway_port="$(awk -F= '/^GATEWAY_PORT=/{print $2}' "$TRACKED_CONFIG" | tail -n 1 | tr -d '[:space:]')"
gateway_port="${gateway_port:-18789}"
if lsof -ti "tcp:$gateway_port" -sTCP:LISTEN >/dev/null 2>&1; then
    ok "gateway port already listening on tcp:$gateway_port"
else
    warn "gateway port not listening on tcp:$gateway_port"
fi

echo
echo "==> Health probe"
if openclaw gateway health >/tmp/openclaw-health-monitor-preflight.out 2>/tmp/openclaw-health-monitor-preflight.err; then
    ok "openclaw gateway health succeeded"
else
    warn "openclaw gateway health failed; inspect /tmp/openclaw-health-monitor-preflight.err"
fi

echo
echo "==> Main closure purity gate"
purity_status="$(purity_gate_status)"
case "$purity_status" in
    ok)
        ok "main closure purity gate passed"
        ;;
    unknown)
        warn "main closure purity gate status unavailable"
        ;;
    failed:*)
        fail "main closure purity gate failed (${purity_status#failed:})"
        ;;
    *)
        warn "main closure purity gate status unknown: $purity_status"
        ;;
esac

echo
echo "==> Process snapshot"
pgrep -fl "guardian.py|dashboard_v2/app.py|dashboard_backend.py|openclaw.*gateway" || warn "no guardian/dashboard/gateway processes matched"

echo
if [ "$failures" -gt 0 ]; then
    echo "Preflight completed with $failures failure(s)."
    exit 1
fi

echo "Preflight passed."
echo "Next:"
echo "1. Start or restart manually when ready."
echo "2. Run ./verify.sh after the switch."
