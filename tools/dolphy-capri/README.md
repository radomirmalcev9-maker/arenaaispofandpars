# Dolphy + Capri: implementation and rebuild

This is an **experimental binary integration**, not an Android Studio source project. No real Android installation, UI, device API, accessibility, Tailscale or Arena end-to-end test was available in the build environment. The original Capri Android source is not in this repository.

## What is changed

- Dolphy 2.6 is the base: its original three DEX files remain byte-identical. The main launcher is the existing `app.capri.MainActivity`; a lifecycle-installed bar switches between it and the existing Dolphy `MainActivity`. The two screens are still separate Activities, not one rewritten Compose screen.
- The existing Capri Arena Screen DEX files are added after relocating conflicting library classes into `capriembed`; `app.capri` and `com.google.ar.core` are retained for JNI. Conflicting synthetic Android platform stubs are removed. Direct resource IDs and the ARCore/accessibility resources are remapped. Original Capri assets and ARM64 shared libraries are retained. Other Dolphy native ABIs are removed because Capri JNI is ARM64-only.
- `app.capri.integration.Tabs` installs the tab bar and tracks the resumed Dolphy Activity. The five original own-package accessibility exclusions are replaced with checks that allow an own-package node/event only when its window ID matches the focused Dolphy Activity. All other own-app windows remain blocked from the accessibility tree. Screenshots are a separate mechanism and can include whatever is visible on screen. Device testing is needed to validate window ID behaviour on actual Android versions.
- `DolphyBridge` registers `dolphy` with Capri's existing tool parser and forwards enumerated public `PluginDeviceApis` / `PluginAndroidApis` methods using JSON-compatible arguments. Each direct call shows the method and full argument string in a per-call confirmation dialog, fails closed without a foreground Activity, completes at most once, and reports timeouts. Opening a whitelisted Dolphy route and existing accessibility tools do not use that confirmation dialog. Complex Java objects and long-lived multi-event APIs are not generally representable. Root, Shizuku, permissions and hardware are still separate requirements.
- The application ID and provider authorities are changed to `com.droid.dolphy.capri` so the combined build can coexist with the original apps. Existing app data is not imported. Capri's Arena/Tailscale and screenshot code is retained, but has not been tested on a phone.
- `pack.py` writes local ZIP headers without data descriptors and aligns stored entries (including 16 KiB alignment for stored native libraries). Sign **after** packing. The manifest retains `extractNativeLibs=true` and the original Dolphy `usesCleartextTraffic=true`.

## Pinned inputs

- Dolphy 2.6 `app-release.apk`: SHA-256 `d84799d121b3528c3419106905ef0b027aa70d4ee4f9718800dafa5fb3396462`; the user's file matches the [2.6 release](https://github.com/unvoiddd/Dolphy-App/releases/tag/2.6). The [Dolphy sources](https://github.com/unvoiddd/Dolphy-App/tree/3f6cbcc034454cd6a1dee395191572b641c96516) were consulted for navigation and bridge APIs, but source-to-binary reproducibility is not established.
- `Capri-Arena-Screen.apk`: SHA-256 `c382246ece2a1a5af5448cf931911edc33ae685e503a408d16188adbef475025`. Its Arena and screenshot modifications are documented in `CAPRI-SETUP.md`.

The script checks both hashes and fails on unexpected class/resource/patch patterns. Dolphy is GPL-3.0; see [the licence](LICENSE-DOLPHY-GPL-3.0.txt). Complete Capri sources and redistribution rights are not supplied here. Confirm licensing and source obligations before distributing a combined work publicly.

## Rebuild on Linux

Obtain a Java runtime, Apktool 2.9.3, Eclipse ECJ, Android API 30 `android.jar`, a desktop D8/R8 jar exposing `com.android.tools.r8.D8`, and `apksigner.jar`. Provide your own private PKCS12 signing key and a private file containing its password. Do **not** put either in this repository.

```bash
export JAVA_BIN=/path/to/java
export APKTOOL_JAR=/path/to/apktool.jar
export ECJ_JAR=/path/to/ecj.jar
export ANDROID_JAR=/path/to/android-30/android.jar
export D8_JAR=/path/to/r8.jar
export APKSIGNER_JAR=/path/to/apksigner.jar
export SIGNING_KEYSTORE=/private/path/signing-key.p12
export SIGNING_PASS_FILE=/private/path/signing-password.txt
export SIGNING_ALIAS=dolphycapri

tools/dolphy-capri/build.sh /path/to/dolphy-2.6.apk Capri-Arena-Screen.apk /path/to/output.apk
```

The script decodes disposable temporary copies, compiles the bridge with ECJ, builds with Apktool, packs and signs with v2/v3, then runs `apksigner verify`. A full clean rebuild with these inputs was exercised; APK byte hashes can vary between builds. To update the shipped copy on a device, sign with the **same** key. A different key cannot update an existing installation of the same package; uninstalling loses its local app data.

The private signing key and password used for the current experimental APK are stored outside the repository at `/home/user/Dolphy-Capri-private/` in this workspace. Preserve them privately if updates are wanted; never commit or publish them.

## Tests

```bash
python -m pytest -q tests/test_dolphy_capri.py tests/test_capri_apk.py
apksigner verify --verbose --print-certs Dolphy-Capri.apk
```

The Python tests check patch guards, manifest composition, focused-window filtering logic using JVM fakes, explicit direct-call confirmation/callback handling using JVM fakes, duplicate DEX classes, Dolphy DEX hashes, Capri assets/native library hashes, ZIP local headers and stored-entry alignment. The JVM tests use `java`/`javac`, or `DOLPHY_CAPRI_JAVA` and `DOLPHY_CAPRI_ECJ_JAR`, and skip if neither compiler is available. Fakes and structural tests are **not** Android device tests.
