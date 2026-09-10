#!/bin/bash
# Builds Copita.app (ad-hoc signed, same as build.sh) and packages it into a
# distributable .dmg. Contents:
#   - Copita.app
#   - a drag-to-Applications shortcut
#   - "Read Me.txt"  (the end-user guide -- APP_README.md, not the repo's
#     own README, which is about building from source)
#   - LICENSE        (Copita is GPL-3.0 -- the license text has to travel
#     with any binary we hand out, and Read Me.txt links to the full source)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
APP_BUNDLE="$SCRIPT_DIR/Copita.app"
PLIST="$SCRIPT_DIR/Resources/Info.plist"

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST" 2>/dev/null || echo 0.0.0)"
VOL_NAME="Copita Downloader"
DMG_PATH="$SCRIPT_DIR/Copita-Downloader-${VERSION}.dmg"
STAGING_DIR="$SCRIPT_DIR/.dmg_staging"

echo "==> Building Copita.app (v${VERSION})..."
bash "$SCRIPT_DIR/build.sh"

echo "==> Preparing DMG contents..."
rm -rf "$STAGING_DIR" "$SCRIPT_DIR"/Copita-Downloader*.dmg
mkdir -p "$STAGING_DIR"
cp -R "$APP_BUNDLE" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
cp "$SCRIPT_DIR/APP_README.md" "$STAGING_DIR/Read Me.txt"

# GPL-3.0: ship the license alongside the binary.
if [ -f "$WORKSPACE_DIR/LICENSE" ]; then
  cp "$WORKSPACE_DIR/LICENSE" "$STAGING_DIR/LICENSE.txt"
else
  echo "!!  WARNING: $WORKSPACE_DIR/LICENSE not found -- DMG will ship without it."
fi

echo "==> Creating disk image..."
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH" -quiet

echo "==> Signing disk image..."
codesign --force --sign - "$DMG_PATH"

rm -rf "$STAGING_DIR"

echo "==> Done: $DMG_PATH"
echo "    Ad-hoc signed, not notarized by Apple. On macOS 15+ the first"
echo "    launch is blocked -- the user opens System Settings > Privacy &"
echo "    Security and clicks \"Open Anyway\" (covered in Read Me.txt)."
