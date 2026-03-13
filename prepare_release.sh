#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-check}"

cd "$BASE_DIR"

echo "==> OpenClaw Health Monitor release prep ($MODE)"

echo
echo "[1/5] Python cache tracked by git"
TRACKED_PYC="$(git ls-files '__pycache__/*' '*.pyc' || true)"
if [ -n "$TRACKED_PYC" ]; then
  echo "$TRACKED_PYC"
  if [ "$MODE" = "fix" ]; then
    git rm --cached $TRACKED_PYC
  fi
else
  echo "OK"
fi

echo
echo "[2/5] Runtime artifacts"
find "$BASE_DIR" -maxdepth 2 \( -path "$BASE_DIR/logs/*" -o -path "$BASE_DIR/change-logs/*" -o -path "$BASE_DIR/snapshots/*" -o -path "$BASE_DIR/data/*.db" -o -path "$BASE_DIR/data/*.db-shm" -o -path "$BASE_DIR/data/*.db-wal" -o -path "$BASE_DIR/data/current-task-facts.json" -o -path "$BASE_DIR/data/task-registry-summary.json" -o -path "$BASE_DIR/data/shared-state/*.json" -o -path "$BASE_DIR/.learnings/*.md" -o -path "$BASE_DIR/memory/*.md" -o -path "$BASE_DIR/MEMORY.md" \) -print 2>/dev/null || true

echo
echo "[3/5] Secret placeholders in tracked config"
grep -n 'WEBHOOK=' "$BASE_DIR/config.conf" || true

echo
echo "[4/5] Test suite"
"$BASE_DIR/.venv/bin/python" -m pytest dashboard_v2/tests tests -q

echo
echo "[5/5] Git status"
git status --short

echo
if [ "$MODE" = "fix" ]; then
  echo "Release prep fix-up finished."
else
  echo "Release prep check finished. Run ./prepare_release.sh fix to apply safe git cleanup."
fi
