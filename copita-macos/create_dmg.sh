#!/bin/bash
# Builds Copita.app (ad-hoc signed, same as build.sh) and packages it into a
# distributable .dmg: the app itself, a drag-to-Applications shortcut, and
# the end-user-facing APP_README.md (ships as "Read Me.txt" inside the DMG --
# not this repo's own README, which is for building from source, not for
# someone who just downloaded the app).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_BUNDLE="$SCRIPT_DIR/Copita.app"
VOL_NAME="Copita Downloader"
DMG_PATH="$SCRIPT_DIR/Copita-Downloader.dmg"
STAGING_DIR="$SCRIPT_DIR/.dmg_staging"

echo "==> Building Copita.app..."
bash "$SCRIPT_DIR/build.sh"

echo "==> Preparing DMG contents..."
rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"
cp -R "$APP_BUNDLE" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
cp "$SCRIPT_DIR/APP_README.md" "$STAGING_DIR/Read Me.txt"

echo "==> Creating disk image..."
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH" -quiet

echo "==> Signing disk image..."
codesign --force --sign - "$DMG_PATH"

rm -rf "$STAGING_DIR"

echo "==> Done: $DMG_PATH"
echo "    Note: this is ad-hoc signed, not notarized by Apple -- anyone"
echo "    opening it for the first time will need to right-click > Open"
echo "    (covered in Read Me.txt / APP_README.md)."
