#!/usr/bin/env bash
# Build an experimental combined APK from the exact, separately supplied inputs.
# Never keep a signing key or password in the repository.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 DOLPHY_2_6.apk CAPRI_ARENA_SCREEN.apk OUTPUT.apk" >&2
  exit 2
fi
: "${JAVA_BIN:?Set JAVA_BIN to a Java runtime executable}"
: "${APKTOOL_JAR:?Set APKTOOL_JAR to apktool 2.9.3}"
: "${ECJ_JAR:?Set ECJ_JAR to the Eclipse Java compiler jar}"
: "${ANDROID_JAR:?Set ANDROID_JAR to an Android API 30 android.jar}"
: "${D8_JAR:?Set D8_JAR to desktop R8/D8 jar}"
: "${APKSIGNER_JAR:?Set APKSIGNER_JAR to apksigner.jar}"
: "${SIGNING_KEYSTORE:?Set SIGNING_KEYSTORE to a PRIVATE PKCS12 keystore}"
: "${SIGNING_PASS_FILE:?Set SIGNING_PASS_FILE to a PRIVATE file containing its password}"
: "${SIGNING_ALIAS:=dolphycapri}"

repo="$(cd "$(dirname "$0")/../.." && pwd)"
dolphy="$(realpath "$1")"
capri="$(realpath "$2")"
out="$(realpath -m "$3")"
if [[ "$out" == "$dolphy" || "$out" == "$capri" || "$out" == "$(realpath "$SIGNING_KEYSTORE")" ]]; then
  echo "Output must not overwrite an input or the signing key" >&2; exit 2
fi
printf '%s  %s\n' d84799d121b3528c3419106905ef0b027aa70d4ee4f9718800dafa5fb3396462 "$dolphy" | sha256sum -c -
printf '%s  %s\n' c382246ece2a1a5af5448cf931911edc33ae685e503a408d16188adbef475025 "$capri" | sha256sum -c -

export PATH="$(dirname "$JAVA_BIN"):$PATH"
work="$(mktemp -d "${TMPDIR:-/tmp}/dolphy-capri-build.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/helper/classes" "$work/helper/dex" "$(dirname "$out")"

"$JAVA_BIN" -Xmx2g -jar "$APKTOOL_JAR" d -f -s "$dolphy" -o "$work/dolphy"
"$JAVA_BIN" -Xmx2g -jar "$APKTOOL_JAR" d -f -r "$capri" -o "$work/capri"
"$JAVA_BIN" -Xmx2g -jar "$APKTOOL_JAR" d -f -s "$capri" -o "$work/capri-res"
python3 "$repo/tools/merge_dolphy_capri.py" --dolphy "$work/dolphy" --capri "$work/capri" --capri-res "$work/capri-res"

"$JAVA_BIN" -jar "$ECJ_JAR" -1.8 -encoding UTF-8 -bootclasspath "$ANDROID_JAR" \
  -classpath "$ANDROID_JAR" -d "$work/helper/classes" "$repo"/tools/dolphy-capri/src/app/capri/integration/*.java
mapfile -d '' classes < <(find "$work/helper/classes" -name '*.class' -print0)
"$JAVA_BIN" -cp "$D8_JAR" com.android.tools.r8.D8 --min-api 26 --output "$work/helper/dex" "${classes[@]}"

"$JAVA_BIN" -Xmx2g -jar "$APKTOOL_JAR" b -f "$work/dolphy" -o "$work/dolphy-res.apk"
"$JAVA_BIN" -Xmx2g -jar "$APKTOOL_JAR" b -f "$work/capri" -o "$work/capri-dex.apk"
python3 "$repo/tools/dolphy-capri/pack.py" --dolphy "$work/dolphy-res.apk" --capri "$work/capri-dex.apk" \
  --bridge "$work/helper/dex/classes.dex" --out "$work/unsigned.apk"
"$JAVA_BIN" -jar "$APKSIGNER_JAR" sign --ks "$SIGNING_KEYSTORE" --ks-key-alias "$SIGNING_ALIAS" \
  --ks-pass "file:$SIGNING_PASS_FILE" --v1-signing-enabled false --v2-signing-enabled true \
  --v3-signing-enabled true --v4-signing-enabled false --out "$out" "$work/unsigned.apk"
"$JAVA_BIN" -jar "$APKSIGNER_JAR" verify --verbose --print-certs "$out"
sha256sum "$out"
