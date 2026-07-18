#!/bin/zsh
set -euo pipefail

REPOSITORY_ROOT="${0:A:h:h}"
APP_BUNDLE="$REPOSITORY_ROOT/dist/SchriftLotse.app"
CONTENTS="$APP_BUNDLE/Contents"

rm -rf "$APP_BUNDLE"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
xcrun clang \
  -fobjc-arc \
  -O2 \
  -framework AppKit \
  -framework WebKit \
  "$REPOSITORY_ROOT/macos/SchriftLotseApp.m" \
  -o "$CONTENTS/MacOS/SchriftLotse"
cp "$REPOSITORY_ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
printf '%s\n' "$REPOSITORY_ROOT" > "$CONTENTS/Resources/repository.txt"
codesign --force --deep --sign - "$APP_BUNDLE"
echo "Erstellt: $APP_BUNDLE"
