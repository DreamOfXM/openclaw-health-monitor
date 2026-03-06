#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$BASE_DIR/desktop_runtime.sh"

if [ ! -x "$RUNTIME" ]; then
    echo "Missing runtime controller: $RUNTIME" >&2
    exit 1
fi

echo "Stopping OpenClaw Health Monitor..."
"$RUNTIME" stop all
echo "Gateway, Guardian, and Dashboard stopped."
