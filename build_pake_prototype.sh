#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="${APP_NAME:-OpenClaw Health Monitor}"
OUTPUT_DIR="$BASE_DIR/dist/pake"
ICON_PATH="${ICON_PATH:-$BASE_DIR/assets/icons/openclaw_lobster_armor.png}"
WINDOW_WIDTH="${WINDOW_WIDTH:-1480}"
WINDOW_HEIGHT="${WINDOW_HEIGHT:-960}"
HIDE_TITLE_BAR="${HIDE_TITLE_BAR:-1}"
ENTRY_URL="${ENTRY_URL:-http://127.0.0.1:8080}"
STARTED_DASHBOARD=0
STARTED_DASHBOARD_PID=""

cleanup() {
    if [ "$STARTED_DASHBOARD" = "1" ] && [ -n "$STARTED_DASHBOARD_PID" ]; then
        kill "$STARTED_DASHBOARD_PID" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

discover_dashboard_url() {
    if [ -n "${DASHBOARD_URL:-}" ]; then
        echo "$DASHBOARD_URL"
        return 0
    fi
    local port
    for port in $(seq 8080 8089); do
        if curl -fsS "http://127.0.0.1:${port}/api/status" >/dev/null 2>&1; then
            echo "http://127.0.0.1:${port}"
            return 0
        fi
    done
    return 1
}

if ! command -v pnpm >/dev/null 2>&1; then
    echo "pnpm not found. Install pnpm first." >&2
    exit 1
fi

if [ ! -f "$ICON_PATH" ]; then
    echo "Icon not found: $ICON_PATH" >&2
    exit 1
fi

if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
    echo "Rust toolchain not found." >&2
    echo "Install Rust first, for example:" >&2
    echo "  brew install rust" >&2
    echo "Then rerun ./build_pake_prototype.sh" >&2
    exit 1
fi

copy_latest_artifacts() {
    mkdir -p "$OUTPUT_DIR"

    local root_dmg="$BASE_DIR/${APP_NAME}.dmg"
    if [ -f "$root_dmg" ]; then
        cp -f "$root_dmg" "$OUTPUT_DIR/"
        echo "Copied project dmg to: $OUTPUT_DIR/$(basename "$root_dmg")"
    fi

    local app_path=""
    app_path="$(find "$HOME/Library/Caches/pnpm/dlx" -path "*${APP_NAME}.app" -type d 2>/dev/null | tail -n 1 || true)"
    if [ -n "$app_path" ]; then
        rm -rf "$OUTPUT_DIR/${APP_NAME}.app"
        ditto "$app_path" "$OUTPUT_DIR/${APP_NAME}.app"
        echo "Copied app bundle to: $OUTPUT_DIR/${APP_NAME}.app"
    fi

    local dmg_path=""
    dmg_path="$(find "$HOME/Library/Caches/pnpm/dlx" -path "*${APP_NAME}_*.dmg" ! -name "rw.*" -type f 2>/dev/null | tail -n 1 || true)"
    if [ -n "$dmg_path" ] && [ ! -f "$OUTPUT_DIR/$(basename "$root_dmg")" ]; then
        cp -f "$dmg_path" "$OUTPUT_DIR/"
        echo "Copied dmg to: $OUTPUT_DIR/$(basename "$dmg_path")"
    fi

    [ -n "$app_path" ] || [ -n "$dmg_path" ] || [ -f "$root_dmg" ]
}

sync_app_from_output_dmg() {
    local dmg_path="$OUTPUT_DIR/${APP_NAME}.dmg"
    local mount_dir
    mount_dir="$(mktemp -d /tmp/openclaw-pake-mount.XXXXXX)"
    if hdiutil attach "$dmg_path" -nobrowse -readonly -mountpoint "$mount_dir" >/dev/null 2>&1; then
        if [ -d "$mount_dir/${APP_NAME}.app" ]; then
            rm -rf "$OUTPUT_DIR/${APP_NAME}.app"
            ditto "$mount_dir/${APP_NAME}.app" "$OUTPUT_DIR/${APP_NAME}.app"
            echo "Synced app bundle from dmg to: $OUTPUT_DIR/${APP_NAME}.app"
        fi
        hdiutil detach "$mount_dir" >/dev/null 2>&1 || true
    fi
    rmdir "$mount_dir" 2>/dev/null || true
}

create_fallback_dmg() {
    local app_bundle="$OUTPUT_DIR/${APP_NAME}.app"
    local dmg_path="$OUTPUT_DIR/${APP_NAME}.dmg"
    if [ ! -d "$app_bundle" ]; then
        return 1
    fi
    if ! command -v hdiutil >/dev/null 2>&1; then
        return 1
    fi
    echo "Creating fallback dmg from copied app bundle..."
    rm -f "$dmg_path"
    hdiutil create -volname "$APP_NAME" -srcfolder "$app_bundle" -ov -format UDZO "$dmg_path"
    echo "Created fallback dmg: $dmg_path"
}

install_desktop_wrapper() {
    local app_bundle="$1"
    local plist="$app_bundle/Contents/Info.plist"
    if [ ! -f "$plist" ]; then
        return 1
    fi
    local exec_name
    exec_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$plist" 2>/dev/null || true)"
    if [ -z "$exec_name" ]; then
        return 1
    fi
    local macos_dir="$app_bundle/Contents/MacOS"
    local original_exec="$macos_dir/$exec_name"
    local wrapped_exec="$macos_dir/${exec_name}-bin"
    if [ ! -f "$original_exec" ]; then
        return 1
    fi
    if [ ! -f "$wrapped_exec" ]; then
        mv "$original_exec" "$wrapped_exec"
    fi
    cat > "$original_exec" <<EOF
#!/bin/bash
set -euo pipefail

APP_DIR="\$(cd "\$(dirname "\$0")/../.." && pwd)"
REPO_DIR="\${OPENCLAW_MONITOR_DIR:-\$HOME/openclaw-health-monitor}"
RUNTIME="\$REPO_DIR/desktop_runtime.sh"
NATIVE_BIN="\$APP_DIR/Contents/MacOS/${exec_name}-bin"
WAIT_URL="http://127.0.0.1:8080/api/status"

cleanup() {
    if [ -x "\$RUNTIME" ]; then
        "\$RUNTIME" stop all >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

if [ ! -x "\$RUNTIME" ]; then
    osascript -e 'display alert "OpenClaw Health Monitor" message "Missing ~/openclaw-health-monitor/desktop_runtime.sh. Install the monitor repository first." as critical'
    exit 1
fi

if ! "\$RUNTIME" start all >/tmp/openclaw-health-monitor-app-start.log 2>&1; then
    osascript -e 'display alert "OpenClaw Health Monitor" message "Failed to start Gateway / Guardian / Dashboard. Check ~/openclaw-health-monitor/logs and /tmp/openclaw-health-monitor-app-start.log." as critical'
    exit 1
fi

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

for _ in {1..20}; do
    if env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost curl --noproxy '*' -fsS "\$WAIT_URL" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost curl --noproxy '*' -fsS "\$WAIT_URL" >/dev/null 2>&1; then
    osascript -e 'display alert "OpenClaw Health Monitor" message "Local dashboard did not become reachable on http://127.0.0.1:8080." as critical'
    exit 1
fi

"\$NATIVE_BIN" "\$@"
exit \$?
EOF
    chmod +x "$original_exec"
    echo "Installed desktop lifecycle wrapper into: $app_bundle"
}

echo "Building desktop app for local dashboard URL"
echo "App name: $APP_NAME"
echo "Icon path: $ICON_PATH"
echo "Entry URL: $ENTRY_URL"
echo "Window: ${WINDOW_WIDTH}x${WINDOW_HEIGHT}"
echo "Output dir: $OUTPUT_DIR"
echo
echo "Command:"
BASE_CMD=(pnpm dlx pake-cli "$ENTRY_URL" --name "$APP_NAME" --icon "$ICON_PATH" --width "$WINDOW_WIDTH" --height "$WINDOW_HEIGHT")
if [ "$HIDE_TITLE_BAR" = "1" ]; then
    BASE_CMD+=(--hide-title-bar)
fi
if [ -n "${PAKE_ARGS:-}" ]; then
    echo "${BASE_CMD[*]} ${PAKE_ARGS}"
else
    echo "${BASE_CMD[*]}"
fi
echo

cd "$BASE_DIR"
build_status=0
"${BASE_CMD[@]}" ${PAKE_ARGS:-} || build_status=$?

if copy_latest_artifacts; then
    if [ -f "$OUTPUT_DIR/${APP_NAME}.dmg" ]; then
        sync_app_from_output_dmg || true
    fi
    if [ -d "$OUTPUT_DIR/${APP_NAME}.app" ]; then
        install_desktop_wrapper "$OUTPUT_DIR/${APP_NAME}.app" || true
        rm -f "$OUTPUT_DIR/${APP_NAME}.dmg"
        create_fallback_dmg || true
    elif [ ! -f "$OUTPUT_DIR/${APP_NAME}.dmg" ]; then
        create_fallback_dmg || true
    fi
    if [ "$build_status" -ne 0 ]; then
        echo "Pake build reported an error, but usable artifacts were copied to $OUTPUT_DIR." >&2
    else
        echo "Artifacts copied to $OUTPUT_DIR."
    fi
    exit 0
fi

exit "$build_status"
