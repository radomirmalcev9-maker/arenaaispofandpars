#!/usr/bin/env python3
"""Pack the merged resources and dex files without ZIP data descriptors.

The APK remains unsigned. Sign the output with apksigner *after* packing.
"""
from __future__ import annotations

import argparse
import copy
import shutil
import struct
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


def aligned_info(info: ZipInfo, position: int, name: str | None = None) -> ZipInfo:
    result = copy.copy(info)
    if name is not None:
        result.filename = name
        result.orig_filename = name
    result.flag_bits = 0
    result.extra = b""
    if result.compress_type == ZIP_STORED:
        alignment = 16384 if result.filename.startswith("lib/") and result.filename.endswith(".so") else 4
        filename_bytes, _ = result._encodeFilenameFlags()
        data_offset = position + 30 + len(filename_bytes)
        if data_offset % alignment:
            padding = (-(data_offset + 4)) % alignment
            result.extra = struct.pack("<HH", 0xD935, padding) + bytes(padding)
    return result


def stream_copy(source, out: ZipFile, info: ZipInfo, name: str | None = None) -> None:
    if out.fp is None:
        raise ValueError("Closed output ZIP")
    item = aligned_info(info, out.fp.tell(), name)
    with source as input_stream, out.open(item, "w") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def signature(name: str) -> bool:
    upper = name.upper()
    return upper.startswith("META-INF/") and (
        upper.endswith((".SF", ".RSA", ".DSA", ".EC")) or upper == "META-INF/MANIFEST.MF"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dolphy", type=Path, required=True, help="rebuilt Dolphy resources APK")
    parser.add_argument("--capri", type=Path, required=True, help="rebuilt Capri smali APK")
    parser.add_argument("--bridge", type=Path, required=True, help="D8-generated integration classes.dex")
    parser.add_argument("--out", type=Path, required=True, help="unsigned output APK")
    args = parser.parse_args()
    if args.out.resolve() in {args.dolphy.resolve(), args.capri.resolve(), args.bridge.resolve()}:
        raise ValueError("Output must not overwrite an input")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.dolphy) as dolphy, ZipFile(args.capri) as capri, \
            ZipFile(args.out, "w", allowZip64=True, compression=ZIP_DEFLATED) as out:
        names = set()
        for info in dolphy.infolist():
            if info.is_dir() or signature(info.filename):
                continue
            if info.filename in names:
                raise ValueError(f"Duplicate base ZIP entry: {info.filename}")
            names.add(info.filename)
            stream_copy(dolphy.open(info), out, info)
        for old, new in (("classes.dex", "classes4.dex"), ("classes2.dex", "classes5.dex")):
            if new in names:
                raise ValueError(f"Base already contains {new}")
            stream_copy(capri.open(old), out, capri.getinfo(old), new)
            names.add(new)
        if "classes6.dex" in names:
            raise ValueError("Base already contains classes6.dex")
        bridge_info = ZipInfo("classes6.dex", (2026, 1, 1, 0, 0, 0))
        bridge_info.compress_type = ZIP_DEFLATED
        with args.bridge.open("rb") as source:
            stream_copy(source, out, bridge_info)
    print(f"Wrote unsigned {args.out} ({args.out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
