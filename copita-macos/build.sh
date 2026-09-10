#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
APP_BUNDLE="$SCRIPT_DIR/Copita.app"

echo "==> Building Copita Downloader for macOS..."

# 1. Compile Swift sources
echo "--> Compiling Swift sources..."
swiftc -O -target arm64-apple-macos13.0 \
  "$SCRIPT_DIR"/Sources/*.swift \
  -o "$SCRIPT_DIR/Copita_bin" \
  -framework Cocoa \
  -framework SwiftUI \
  -framework UserNotifications

# 2. Assemble .app bundle
echo "--> Assembling Copita.app bundle..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

mv "$SCRIPT_DIR/Copita_bin" "$APP_BUNDLE/Contents/MacOS/Copita"
chmod +x "$APP_BUNDLE/Contents/MacOS/Copita"

cp "$SCRIPT_DIR/Resources/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
cp "$SCRIPT_DIR/Resources/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
cp "$SCRIPT_DIR/Resources"/MenuBarIcon*.png "$APP_BUNDLE/Contents/Resources/"

# Copy copita engine into Resources so app is standalone. rsync (not cp -R)
# so dev-machine cruft -- __pycache__ bytecode caches, .DS_Store -- never
# ships in a distributed app; it's stale/machine-specific junk, not
# something the bundled copy needs (Python regenerates its own cache at
# runtime regardless).
echo "--> Bundling copita package into Resources..."
rsync -a --exclude='__pycache__' --exclude='.DS_Store' "$WORKSPACE_DIR/copita/" "$APP_BUNDLE/Contents/Resources/copita/"

# Copy the browser extension into Resources too, so the Settings > Install
# Extension flow has something to point at even when workspacePath doesn't
# resolve to the live project directory (e.g. after real distribution).
echo "--> Bundling browser extension into Resources..."
rsync -a --exclude='.DS_Store' "$WORKSPACE_DIR/copita-extension/" "$APP_BUNDLE/Contents/Resources/copita-extension/"

# 3. Ad-hoc codesign
echo "--> Signing Copita.app..."
codesign --force --deep --sign - "$APP_BUNDLE"

echo "==> Successfully built: $APP_BUNDLE"
echo "    You can run it with: open \"$APP_BUNDLE\""
