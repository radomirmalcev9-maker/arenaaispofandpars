"""Static regression tests for the repackaged Capri adapter.

A real Android device/emulator is still needed for an end-to-end install test.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct
import zipfile

import pytest

from arena_api.compat.capri_server import pairing_string
from tools import patch_capri

ROOT = Path(__file__).resolve().parents[1]


def test_pairing_format_matches_the_android_allowlist() -> None:
    smali = (ROOT / "tools/capri/ArenaBridge.smali").read_text(encoding="utf-8")
    line = next(line for line in smali.splitlines() if "const-string v0" in line and "https://" in line)
    raw = line.split('"', 2)[1]
    # Smali uses two backslashes to encode one in the Java regex.
    pattern = raw.replace("\\\\", "\\")
    token = "a" * 43
    good = pairing_string("phone.tail123.ts.net", 8443, token)
    assert re.fullmatch(pattern, good)
    for value in (
        good.replace("https://", "http://"),
        good.replace("phone.tail123.ts.net", "attacker.example.com"),
        good.replace("phone.tail123.ts.net", "phone.tail123.ts.net@attacker.example.com"),
        good.replace(":8443#", ":8443/elsewhere#"),
        good.replace(token, "short"),
    ):
        assert not re.fullmatch(pattern, value)


def test_pairing_guard_blocks_mistyped_arena_strings_before_provider_fallback() -> None:
    smali = (ROOT / "tools/capri/ArenaBridge.smali").read_text(encoding="utf-8")
    guard = smali.split(".method public static guard(", 1)[1].split(".end method", 1)[0]
    # The original app falls back to Mistral for an unknown key. Obvious
    # misspellings of our pairing string must not be sent to that provider.
    assert 'const-string v0, "arena+"' in guard
    assert 'const-string v1, "arena"' in guard
    assert 'const-string v1, ".ts.net"' in guard
    assert "Invalid Arena pairing string" in guard
    assert "ArenaBridge;->guard" in (ROOT / "tools/patch_capri.py").read_text(encoding="utf-8")


def test_manifest_patch_uses_a_separate_application_id() -> None:
    components = "".join(f'<activity android:name=".C{i}"/>' for i in range(11))
    source = (
        '<manifest package="app.capri"><application android:label="Capri" '
        f'android:largeHeap="true">{components}'
        '<service android:name="app.capri.Existing" android:label="Capri"/>'
        '</application></manifest>'
    )
    out = patch_capri.patch_manifest(source)
    assert 'package="app.capri.arena"' in out
    assert out.count('android:label="Capri Arena"') == 2
    assert out.count('android:name="app.capri.C') == 11
    with pytest.raises(ValueError):
        patch_capri.patch_manifest(source.replace('.C10', 'C10'))


def test_provider_patch_refuses_unrecognised_versions() -> None:
    old = "    const/16 v0, 0x8\n\n    .line 177\n"
    old += "    const/4 v2, 0x7\n\n    aput-object v1, v0, v2\n\n    .line 176"
    out = patch_capri.patch_provider_list(old)
    assert "const/16 v0, 0x9" in out
    assert 'const-string v3, "arena"' in out
    assert 'const-string v2, "arena+"' in out
    with pytest.raises(ValueError):
        patch_capri.patch_provider_list(old.replace("0x8", "0x9"))


def test_built_apk_keeps_assets_and_has_an_arena_adapter() -> None:
    original = ROOT / "Capri.apk"
    patched = ROOT / "Capri-Arena.apk"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(patched) as app:
        manifest = app.read("AndroidManifest.xml")
        assert "app.capri.arena".encode("utf-16-le") in manifest
        assert "app.capri.MainActivity".encode("utf-16-le") in manifest
        assert "Capri Arena".encode("utf-16-le") in manifest
        # The adapter uses HTTPS; it does not turn on Android cleartext traffic.
        assert "usesCleartextTraffic".encode("utf-16-le") not in manifest
        dex = app.read("classes.dex")
        for marker in (b"Lapp/capri/ArenaBridge;", b"Invalid Arena pairing string", b"arena+"):
            assert marker in dex
        assert source.read("assets/strings.json") == app.read("assets/strings.json")
        # Every native library must be byte-for-byte identical to the original.
        source_libs = {name for name in source.namelist() if name.startswith("lib/") and name.endswith(".so")}
        app_libs = {name for name in app.namelist() if name.startswith("lib/") and name.endswith(".so")}
        assert source_libs and source_libs == app_libs
        for filename in source_libs:
            assert hashlib.sha256(source.read(filename)).digest() == hashlib.sha256(app.read(filename)).digest(), filename
    # A v2/v3 APK signature block exists; apksigner performs full verification.
    assert b"APK Sig Block 42" in patched.read_bytes()


def test_built_apk_local_zip_headers_and_descriptors_match_directory() -> None:
    """Android checks data descriptors that Python's ZipFile.testzip ignores.

    The previous APK had valid CRCs in the central directory but invalid
    descriptors for compressed entries, including its native libraries.
    """
    patched = ROOT / "Capri-Arena.apk"
    with zipfile.ZipFile(patched) as app, patched.open("rb") as raw:
        assert app.testzip() is None
        for info in app.infolist():
            raw.seek(info.header_offset)
            header = raw.read(30)
            assert len(header) == 30, info.filename
            signature, _version, flags, method, _time, _date, crc, size, usize, name_len, extra_len = (
                struct.unpack("<IHHHHHIIIHH", header)
            )
            assert signature == 0x04034B50, info.filename
            assert flags == info.flag_bits and method == info.compress_type, info.filename
            name = raw.read(name_len).decode("utf-8" if flags & 0x800 else "cp437")
            assert name == info.orig_filename, info.filename
            if flags & 0x08:
                # ZIP data descriptors can optionally start with 0x08074b50.
                raw.seek(info.header_offset + 30 + name_len + extra_len + info.compress_size)
                descriptor = raw.read(4)
                if descriptor == b"PK\x07\x08":
                    descriptor = raw.read(4)
                descriptor += raw.read(8)
                assert len(descriptor) == 12, info.filename
                assert struct.unpack("<III", descriptor) == (
                    info.CRC, info.compress_size, info.file_size
                ), info.filename
            else:
                assert (crc, size, usize) == (info.CRC, info.compress_size, info.file_size), info.filename


def test_built_apk_stored_entries_are_zip_aligned() -> None:
    patched = ROOT / "Capri-Arena.apk"
    with zipfile.ZipFile(patched) as app, patched.open("rb") as raw:
        for info in app.infolist():
            if info.compress_type != zipfile.ZIP_STORED:
                continue
            raw.seek(info.header_offset)
            header = raw.read(30)
            name_len, extra_len = struct.unpack_from("<HH", header, 26)
            assert (info.header_offset + 30 + name_len + extra_len) % 4 == 0, info.filename
