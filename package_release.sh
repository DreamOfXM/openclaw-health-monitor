#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$BASE_DIR/dist/pake"
RELEASE_DIR="$BASE_DIR/release"
APP_NAME="OpenClaw Health Monitor"

read_version() {
    local version
    version="$(awk '/^## \[/{gsub(/^## \[/,""); gsub(/\].*/,""); print; exit}' "$BASE_DIR/CHANGELOG.md")"
    echo "${version:-0.1.0}"
}

VERSION="${APP_VERSION:-$(read_version)}"
ARCH="${APP_ARCH:-arm64}"
SLUG="openclaw-health-monitor"

APP_BUNDLE="$DIST_DIR/${APP_NAME}.app"
DMG_FILE="$DIST_DIR/${APP_NAME}.dmg"
RELEASE_DMG="$RELEASE_DIR/${SLUG}-${VERSION}-macos-${ARCH}.dmg"
RELEASE_ZIP="$RELEASE_DIR/${SLUG}-${VERSION}-macos-${ARCH}.app.zip"

if [ ! -d "$APP_BUNDLE" ] || [ ! -f "$DMG_FILE" ]; then
    echo "Missing packaged app artifacts in $DIST_DIR" >&2
    echo "Run ./build_pake_prototype.sh first." >&2
    exit 1
fi

mkdir -p "$RELEASE_DIR"
cp -f "$DMG_FILE" "$RELEASE_DMG"
ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$RELEASE_ZIP"

echo "Release artifacts ready:"
echo "  $RELEASE_DMG"
echo "  $RELEASE_ZIP"
