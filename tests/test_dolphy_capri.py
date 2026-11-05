"""Host-side tests for the experimental Dolphy + Capri APK integration.

These tests do not replace installation or device/permission tests on Android.
"""
from __future__ import annotations

import importlib.util
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dolphy_capri_merge", ROOT / "tools/merge_dolphy_capri.py")
assert spec and spec.loader
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)
ANDROID = "{http://schemas.android.com/apk/res/android}"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_dex_class_reader_and_invalid_header(tmp_path):
    # One class_def -> type_id -> string_id -> length-prefixed descriptor.
    data = bytearray(160)
    data[:8] = b"dex\n039\0"
    struct.pack_into("<I", data, 0x3C, 112)
    struct.pack_into("<I", data, 0x44, 116)
    struct.pack_into("<II", data, 0x60, 1, 120)
    struct.pack_into("<I", data, 112, 152)
    struct.pack_into("<I", data, 116, 0)
    struct.pack_into("<I", data, 120, 0)
    data[152:] = b"\x05Lx/A;\0\0"
    path = tmp_path / "classes.dex"
    path.write_bytes(data)
    assert merge.dex_classes(path) == {"Lx/A;"}
    path.write_bytes(b"not dex")
    with pytest.raises(ValueError):
        merge.dex_classes(path)


def test_relocation_avoids_framework_and_remaps_high16(tmp_path):
    embedded = write(tmp_path / "smali/x/Test.smali", """.class public Lx/Test;
.super Ljava/lang/Object;
.field a:Landroid/view/View;
.field b:Landroid/support/v4/Foo;
.field c:Lapp/capri/MainActivity;
.method public f()V
    const/high16 v0, 0x7f070000
    const-string v1, "x.Test"
    return-void
.end method
""")
    keep = write(tmp_path / "smali/app/capri/MainActivity.smali", ".class public Lapp/capri/MainActivity;\n.super Ljava/lang/Object;\n")
    support = write(tmp_path / "smali/android/support/v4/Foo.smali", ".class public Landroid/support/v4/Foo;\n.super Ljava/lang/Object;\n")
    stub = write(tmp_path / "smali/android/app/AppComponentFactory.smali", ".class public synthetic Landroid/app/AppComponentFactory;\n.super Ljava/lang/Object;\n")
    stats = merge.relocate_and_remap(tmp_path, {"Lx/Test;", "Landroid/app/AppComponentFactory;"}, {"0x7f070000": "0x7f0a0281"})
    value = embedded.read_text(encoding="utf-8")
    assert ".class public Lcapriembed/x/Test;" in value
    assert "Landroid/view/View;" in value and "Lapp/capri/MainActivity;" in value
    assert "Lcapriembed/android/support/v4/Foo;" in value
    assert '"capriembed.x.Test"' in value
    assert "const v0, 0x7f0a0281" in value and "const/high16 v0, 0x7f0a0281" not in value
    assert not stub.exists() and keep.exists() and support.exists()
    assert stats["removed_platform_stubs"] == 1
    assert stats["remapped_resource_constants"] == 1


def test_relocation_rejects_jni_class_collision(tmp_path):
    write(tmp_path / "smali/app/capri/MainActivity.smali", ".class public Lapp/capri/MainActivity;\n")
    with pytest.raises(ValueError, match="JNI/Capri class collision"):
        merge.relocate_and_remap(tmp_path, {"Lapp/capri/MainActivity;"}, {})


def test_tool_patch_is_specific_and_keeps_own_screen_guard(tmp_path):
    root = tmp_path / "smali/app/capri"
    regex = lambda field: (f'    const-string v1, "foo"\n\n'
                           '    sget-object v2, Lkotlin/text/RegexOption;->IGNORE_CASE:Lkotlin/text/RegexOption;\n\n'
                           '    invoke-direct {v0, v1, v2}, Lkotlin/text/Regex;-><init>(Ljava/lang/String;Lkotlin/text/RegexOption;)V\n\n'
                           f'    sput-object v0, Lapp/capri/HandText;->{field}:Lkotlin/text/Regex;\n')
    hand = write(root / "HandText.smali", "    sput-object v0, Lapp/capri/HandText;->NAMES:Ljava/util/HashSet;\n" + regex("GATE") + regex("DEVICE"))
    hands = write(root / "Hands.smali", "    .line 46\n    :try_start_0\n    invoke-direct {p0, p1, p2, p3}, Lapp/capri/Hands;->go(\n")
    persona = write(root / "Persona.smali", r'const-string v1, "open|app\nclose|app"')
    def check(package_reg, self_reg):
        return ("    invoke-virtual {p0}, Lapp/capri/TypeWatch;->getPackageName()Ljava/lang/String;\n\n"
                f"    move-result-object {self_reg}\n\n"
                f"    invoke-static {{{package_reg}, {self_reg}}}, Lkotlin/jvm/internal/Intrinsics;->areEqual(Ljava/lang/Object;Ljava/lang/Object;)Z\n")
    watch = write(root / "TypeWatch.smali", "".join(check(a, b) for a, b in [
        ("v4", "v5"), ("v1", "v2"), ("v4", "v7"), ("v1", "v2"), ("v3", "v4")]))
    merge.patch_capri_tools(tmp_path)
    assert hand.read_text().count('const-string v1, "dolphy"') == 1
    assert hand.read_text().count(r'dolphy|\u0434') == 2
    assert "DolphyBridge;->intercept" in hands.read_text()
    assert "dolphy|device|method|[JSON arguments]" in persona.read_text()
    assert watch.read_text().count("Tabs;->isOwnNodeBlocked") == 3
    assert watch.read_text().count("Tabs;->isOwnEventBlocked") == 1
    assert watch.read_text().count("Tabs;->isOwnRootOrEventBlocked") == 1
    assert "{p0, v4, v5}" in watch.read_text() and "{p0, v4, p1}" in watch.read_text()
    assert "{p0, v1, v0, p1}" in watch.read_text()
    with pytest.raises(ValueError):
        merge.patch_capri_tools(tmp_path)  # fail closed on unexpected inputs / repeat patch


def test_manifest_preserves_nfc_and_isolates_application_id(tmp_path):
    dolphy = tmp_path / "dolphy"
    capri = tmp_path / "capri"
    ns = 'xmlns:android="http://schemas.android.com/apk/res/android"'
    write(dolphy / "AndroidManifest.xml", f'''<manifest {ns} package="com.droid.dolphy">
      <uses-permission android:name="android.permission.INTERNET"/>
      <queries><package android:name="moe.shizuku.privileged.api"/></queries>
      <permission android:name="com.droid.dolphy.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"/>
      <application android:name="com.droid.dolphy.DolphyApplication">
        <activity android:name="com.droid.dolphy.MainActivity" android:exported="true">
          <intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter>
          <intent-filter><action android:name="android.nfc.action.TAG_DISCOVERED"/></intent-filter>
        </activity>
        <provider android:name="androidx.core.content.FileProvider" android:authorities="com.droid.dolphy.provider"/>
      </application>
    </manifest>''')
    write(capri / "AndroidManifest.xml", f'''<manifest {ns} package="app.capri.arena.screen">
      <uses-permission android:name="android.permission.INTERNET"/>
      <uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW"/>
      <uses-feature android:name="android.hardware.camera.ar" android:required="false"/>
      <queries><package android:name="com.google.ar.core"/></queries>
      <application><activity android:name="app.capri.MainActivity" android:exported="true">
        <intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter>
      </activity><service android:name="app.capri.TypeWatch" android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"/></application>
    </manifest>''')
    merge.merge_manifest(dolphy, capri)
    root = ET.parse(dolphy / "AndroidManifest.xml").getroot()
    assert root.get("package") == "com.droid.dolphy.capri"
    assert len([x for x in root.findall("uses-permission") if x.get(ANDROID + "name") == "android.permission.INTERNET"]) == 1
    app = root.find("application")
    main = next(x for x in app.findall("activity") if x.get(ANDROID + "name") == "com.droid.dolphy.MainActivity")
    assert [x.get(ANDROID + "name") for x in main.findall("intent-filter/action")] == ["android.nfc.action.TAG_DISCOVERED"]
    launcher = next(x for x in app.findall("activity") if x.get(ANDROID + "name") == "app.capri.MainActivity")
    assert launcher.find("intent-filter/category").get(ANDROID + "name") == "android.intent.category.LAUNCHER"
    provider = next(x for x in app.findall("provider") if x.get(ANDROID + "name") == "app.capri.integration.BootstrapProvider")
    assert provider.get(ANDROID + "exported") == "false"
    assert next(x for x in app.findall("provider") if x.get(ANDROID + "name") == "androidx.core.content.FileProvider").get(ANDROID + "authorities") == "com.droid.dolphy.capri.provider"


def test_route_allowlist_has_no_dynamic_or_outer_routes():
    import json
    routes = json.loads((ROOT / "tools/dolphy-capri/routes.json").read_text())
    assert routes == sorted(set(routes)) and len(routes) >= 75
    assert {"bluetooth", "other", "settings", "ir_tv_home"} <= set(routes)
    assert not {"main_scaffold", "plugin_preview", "theme_mode", "accent_color"} & set(routes)
    assert all(re.fullmatch(r"[A-Za-z0-9_/]+", route) for route in routes)


def test_pack_removes_descriptors_and_aligns_stored_files(tmp_path):
    base, capri, bridge, out = (tmp_path / name for name in ("base.apk", "capri.apk", "bridge.dex", "out.apk"))
    with ZipFile(base, "w") as z:
        z.writestr("resources.arsc", b"abc", compress_type=ZIP_STORED)
        z.writestr("classes.dex", b"dolphy1", compress_type=ZIP_DEFLATED)
        z.writestr("classes2.dex", b"dolphy2", compress_type=ZIP_DEFLATED)
        z.writestr("classes3.dex", b"dolphy3", compress_type=ZIP_DEFLATED)
        z.writestr("lib/arm64-v8a/unaligned.so", b"native", compress_type=ZIP_STORED)
        z.writestr("META-INF/OLD.RSA", b"bad signature")
        z.writestr("META-INF/services/some.service", b"keep me")
    with ZipFile(capri, "w") as z:
        z.writestr("classes.dex", b"capri1")
        z.writestr("classes2.dex", b"capri2")
    bridge.write_bytes(b"bridge")
    subprocess.run([sys.executable, str(ROOT / "tools/dolphy-capri/pack.py"), "--dolphy", str(base), "--capri", str(capri), "--bridge", str(bridge), "--out", str(out)], check=True)
    with out.open("rb") as raw, ZipFile(out) as z:
        assert z.testzip() is None
        assert z.read("classes4.dex") == b"capri1" and z.read("classes5.dex") == b"capri2" and z.read("classes6.dex") == b"bridge"
        assert "META-INF/OLD.RSA" not in z.namelist() and "META-INF/services/some.service" in z.namelist()
        for entry in z.infolist():
            raw.seek(entry.header_offset)
            header = struct.unpack("<IHHHHHIIIHH", raw.read(30))
            _, _, flags, _, _, _, crc, compressed, size, name_len, extra_len = header
            assert not flags & 8 and (crc, compressed, size) == (entry.CRC, entry.compress_size, entry.file_size)
            if entry.compress_type == ZIP_STORED:
                alignment = 16384 if entry.filename.endswith(".so") else 4
                assert (entry.header_offset + 30 + name_len + extra_len) % alignment == 0


def test_bridge_confirmation_and_callbacks_on_host(tmp_path):
    """Exercise real bridge logic with JVM Android/Dolphy fakes, not a device."""
    java = os.getenv("DOLPHY_CAPRI_JAVA") or shutil.which("java")
    javac = shutil.which("javac")
    ecj = os.getenv("DOLPHY_CAPRI_ECJ_JAR")
    if not java or (not javac and not ecj):
        pytest.skip("requires a JDK, or DOLPHY_CAPRI_JAVA and DOLPHY_CAPRI_ECJ_JAR")
    sources = sorted(str(path) for path in (ROOT / "tools/dolphy-capri/test").rglob("*.java") if path.name != "TabsHarness.java")
    sources.append(str(ROOT / "tools/dolphy-capri/src/app/capri/integration/DolphyBridge.java"))
    compiler = [javac, "-encoding", "UTF-8", "-d", str(tmp_path)] if javac else [java, "-jar", ecj, "-11", "-encoding", "UTF-8", "-d", str(tmp_path)]
    subprocess.run(compiler + sources, check=True, timeout=60, capture_output=True, text=True)
    result = subprocess.run([java, "-cp", str(tmp_path), "BridgeHarness"], check=True, timeout=20, capture_output=True, text=True)
    assert "all checks passed" in result.stdout


def test_shipped_apk_preserves_both_apps_and_has_no_duplicate_classes(tmp_path):
    apk = ROOT / "Dolphy-Capri.apk"
    capri = ROOT / "Capri-Arena-Screen.apk"
    dolphy_hashes = {
        "classes.dex": "879722dfd64e991ee543d57eef21d2a375b9401fd996a299ca607e99cc7a07c2",
        "classes2.dex": "c0775e1b8b3a875ecb861f86c45044be5aa2ea4e7e24548ad0e563598598af3e",
        "classes3.dex": "3f2fbb9b86ee45d483d0bb09d4a0bf5a7a813cf906d8fafd2bbf014f07f2ad13",
    }
    with ZipFile(apk) as app, ZipFile(capri) as original, apk.open("rb") as raw:
        assert app.testzip() is None
        manifest = app.read("AndroidManifest.xml")
        for text in ("com.droid.dolphy.capri", "com.droid.dolphy.MainActivity", "app.capri.MainActivity",
                     "app.capri.TypeWatch", "app.capri.integration.BootstrapProvider"):
            assert text.encode("utf-16-le") in manifest
        for name, digest in dolphy_hashes.items():
            assert hashlib.sha256(app.read(name)).hexdigest() == digest
        classes = set()
        for name in ("classes.dex", "classes2.dex", "classes3.dex", "classes4.dex", "classes5.dex", "classes6.dex"):
            path = tmp_path / name
            path.write_bytes(app.read(name))
            found = merge.dex_classes(path)
            assert not classes & found, f"duplicate classes in {name}"
            classes.update(found)
        assert {"Lapp/capri/MainActivity;", "Lcom/droid/dolphy/MainActivity;", "Lapp/capri/ArenaBridge;",
                "Lapp/capri/ArenaScreen;", "Lapp/capri/integration/DolphyBridge;",
                "Lcapriembed/kotlin/jvm/functions/Function1;", "Lkotlin/jvm/functions/Function1;"} <= classes
        capri_dex, bridge_dex = app.read("classes4.dex"), app.read("classes6.dex")
        for guard in (b"isOwnNodeBlocked", b"isOwnEventBlocked", b"isOwnRootOrEventBlocked"):
            assert guard in capri_dex and guard in bridge_dex
        assert "assets/capri_dolphy_routes.json" in app.namelist()
        abi = {name.split("/")[1] for name in app.namelist() if name.startswith("lib/") and name.endswith(".so")}
        assert abi == {"arm64-v8a"}
        for name in original.namelist():
            if name.startswith("assets/") or name.startswith("lib/arm64-v8a/"):
                assert hashlib.sha256(original.read(name)).digest() == hashlib.sha256(app.read(name)).digest(), name
        for entry in app.infolist():
            raw.seek(entry.header_offset)
            _, _, flags, _, _, _, crc, compressed, size, name_len, extra_len = struct.unpack("<IHHHHHIIIHH", raw.read(30))
            assert not flags & 8 and (crc, compressed, size) == (entry.CRC, entry.compress_size, entry.file_size), entry.filename
            if entry.compress_type == ZIP_STORED:
                alignment = 16384 if entry.filename.startswith("lib/") and entry.filename.endswith(".so") else 4
                assert (entry.header_offset + 30 + name_len + extra_len) % alignment == 0, entry.filename
    # Signature marker is not a cryptographic verification: run apksigner verify too.
    with apk.open("rb") as raw:
        raw.seek(-2 * 1024 * 1024, 2)
        assert b"APK Sig Block 42" in raw.read()


def test_own_screen_guard_limits_access_to_focused_dolphy_window(tmp_path):
    """Test lifecycle/window checks against host fakes, not a real AccessibilityService."""
    java = os.getenv("DOLPHY_CAPRI_JAVA") or shutil.which("java")
    javac = shutil.which("javac")
    ecj = os.getenv("DOLPHY_CAPRI_ECJ_JAR")
    if not java or (not javac and not ecj):
        pytest.skip("requires a JDK, or DOLPHY_CAPRI_JAVA and DOLPHY_CAPRI_ECJ_JAR")
    test_root = ROOT / "tools/dolphy-capri/test"
    fake_tabs = test_root / "app/capri/integration/Tabs.java"
    sources = sorted(str(path) for path in test_root.rglob("*.java") if path != fake_tabs and path.name != "BridgeHarness.java")
    sources.append(str(ROOT / "tools/dolphy-capri/src/app/capri/integration/Tabs.java"))
    compiler = [javac, "-encoding", "UTF-8", "-d", str(tmp_path)] if javac else [java, "-jar", ecj, "-11", "-encoding", "UTF-8", "-d", str(tmp_path)]
    subprocess.run(compiler + sources, check=True, timeout=60, capture_output=True, text=True)
    result = subprocess.run([java, "-cp", str(tmp_path), "TabsHarness"], check=True, timeout=20, capture_output=True, text=True)
    assert "all checks passed" in result.stdout
