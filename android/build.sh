#!/usr/bin/env bash
# Build the PATREOAIX Android app (APK) without Android Studio.
#
#   TOOLS=/path/to/tools  KEYSTORE=/path/to/patreoaix-release.p12  APK_KEY_PASS=...  ./android/build.sh
#
# TOOLS must hold (all from Maven Central):
#   aapt2, android-framework.jar   from org.apktool:apktool-lib:3.0.3 (prebuilt/linux/aapt2, prebuilt/android-framework.jar)
#   android-all.jar                org.robolectric:android-all:14-robolectric-10818077 (Android 14 classes to compile against)
#   dx.jar                         com.jakewharton.android.repackaged:dalvik-dx:16.0.1
#   apksig.jar                     com.android.tools.build:apksig:2.3.0
# Output: store/static/store/downloads/patreoaix.apk (served at /static/store/downloads/patreoaix.apk)
# KEEP THE KEYSTORE AND ITS PASSWORD SAFE: every update must be signed with the same key, or phones refuse it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
: "${TOOLS:?set TOOLS}"; : "${KEYSTORE:?set KEYSTORE}"; : "${APK_KEY_PASS:?set APK_KEY_PASS}"
B="$HERE/build"; rm -rf "$B"; mkdir -p "$B"/{res,gen,classes,compiled}
cp -r "$HERE/res/." "$B/res/"

# launcher icons from the site icon
python3 - "$ROOT/store/static/store/icons/icon-512.png" "$B/res" <<'PY'
import sys, os
from PIL import Image
src, out = sys.argv[1], sys.argv[2]
im = Image.open(src).convert('RGBA')
for d, px in (('mdpi', 48), ('hdpi', 72), ('xhdpi', 96), ('xxhdpi', 144), ('xxxhdpi', 192)):
    os.makedirs(f'{out}/mipmap-{d}', exist_ok=True)
    im.resize((px, px), Image.LANCZOS).save(f'{out}/mipmap-{d}/ic_launcher.png', optimize=True)
PY

"$TOOLS/aapt2" compile --dir "$B/res" -o "$B/compiled/res.zip"
"$TOOLS/aapt2" link -I "$TOOLS/android-framework.jar" --manifest "$HERE/AndroidManifest.xml" \
    --min-sdk-version 24 --target-sdk-version 34 --java "$B/gen" --auto-add-overlay \
    -o "$B/base.apk" "$B/compiled/res.zip"

javac --release 8 -nowarn -encoding UTF-8 -cp "$TOOLS/android-all.jar" -d "$B/classes" \
    $(find "$HERE/src" "$B/gen" -name '*.java') 2>&1 | grep -v "warning: \[options\]" || true
test -f "$B/classes/com/patreoaix/app/MainActivity.class"
java -cp "$TOOLS/dx.jar" com.android.dx.command.Main --dex --min-sdk-version=24 --output="$B/classes.dex" "$B/classes"

cp "$B/base.apk" "$B/unsigned.apk"
(cd "$B" && zip -q -X -0 unsigned.apk classes.dex)

javac -nowarn -cp "$TOOLS/apksig.jar" -d "$B" "$HERE/tools/SignApk.java"
OUT="$ROOT/store/static/store/downloads"; mkdir -p "$OUT"
java --add-exports java.base/sun.security.x509=ALL-UNNAMED --add-exports java.base/sun.security.pkcs=ALL-UNNAMED --add-exports java.base/sun.security.util=ALL-UNNAMED -cp "$TOOLS/apksig.jar:$B" SignApk "$B/unsigned.apk" "$OUT/patreoaix.apk" "$KEYSTORE" patreoaix
ls -l "$OUT/patreoaix.apk"; sha256sum "$OUT/patreoaix.apk"
