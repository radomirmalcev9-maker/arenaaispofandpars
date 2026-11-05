#!/usr/bin/env python3
"""Patch an apktool-decoded copy of the Capri 1.22 APK for Arena.

The original APK has no configurable server URL. This small patch adds an
Arena provider using the existing Anthropic-compatible wire protocol. It never
modifies the original APK and refuses to patch an unrecognised build.

Usage: python tools/patch_capri.py /path/to/decoded-apk
"""
from __future__ import annotations

import argparse
from pathlib import Path


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Unexpected Capri version: cannot patch {what}")
    return text.replace(old, new, 1)


def patch_provider_list(source: str) -> str:
    source = _replace_once(
        source,
        "    const/16 v0, 0x8\n\n    .line 177",
        "    const/16 v0, 0x9\n\n    .line 177",
        "provider count",
    )
    return _replace_once(
        source,
        "    const/4 v2, 0x7\n\n    aput-object v1, v0, v2\n\n    .line 176",
        """    const/4 v2, 0x7

    aput-object v1, v0, v2

    # The actual URL comes from ArenaBridge.resolve at connection time.
    new-instance v1, Lapp/capri/Provider;
    const-string v2, "arena+"
    invoke-static {v2}, Ljava/util/Collections;->singletonList(Ljava/lang/Object;)Ljava/util/List;
    move-result-object v2
    const-string v3, "arena"
    const-string v4, "Arena"
    const-string v5, "https://invalid.ts.net/v1"
    invoke-direct {v1, v3, v4, v5, v2}, Lapp/capri/Provider;-><init>(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/util/List;)V
    const/16 v2, 0x8
    aput-object v1, v0, v2

    .line 176""",
        "Arena provider",
    )


def patch_cloud(source: str) -> str:
    conn_begin = source.index(".method private final conn(")
    chat_begin = source.index(".method public final chat(", conn_begin)
    connect_begin = source.index(".method public final connect(", chat_begin)
    head, conn, chat, connect = (
        source[:conn_begin],
        source[conn_begin:chat_begin],
        source[chat_begin:connect_begin],
        source[connect_begin:],
    )
    conn = _replace_once(
        conn,
        """    .line 185
    invoke-virtual {p3}, Lapp/capri/Provider;->getId()Ljava/lang/String;

    move-result-object v0

    const-string v1, "anthropic"

    invoke-static {v0, v1}, Lkotlin/jvm/internal/Intrinsics;->areEqual(Ljava/lang/Object;Ljava/lang/Object;)Z

    move-result v0""",
        """    .line 185
    invoke-virtual {p3}, Lapp/capri/Provider;->getId()Ljava/lang/String;
    move-result-object v0

    const-string v1, "arena"
    invoke-virtual {v1, v0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v1
    if-eqz v1, :arena_header_done
    invoke-static {p2}, Lapp/capri/ArenaBridge;->secret(Ljava/lang/String;)Ljava/lang/String;
    move-result-object p2
    # Never forward the pairing secret to a redirected host.
    const/4 v1, 0x0
    invoke-virtual {p1, v1}, Ljava/net/HttpURLConnection;->setInstanceFollowRedirects(Z)V
    # Tailscale may need to provision its certificate on first connection.
    const v1, 0xafc8
    invoke-virtual {p1, v1}, Ljava/net/HttpURLConnection;->setConnectTimeout(I)V
    # A cold Arena/browser start can take longer than the default 60 seconds.
    const v1, 0x2bf20
    invoke-virtual {p1, v1}, Ljava/net/HttpURLConnection;->setReadTimeout(I)V
    :arena_header_done
    invoke-static {v0}, Lapp/capri/ArenaBridge;->isAnthropic(Ljava/lang/String;)Z
    move-result v0""",
        "provider authentication",
    )
    chat = _replace_once(
        chat,
        """    .line 205
    invoke-virtual {p1}, Lapp/capri/Provider;->getId()Ljava/lang/String;""",
        """    .line 205
    invoke-static {p1, p2}, Lapp/capri/ArenaBridge;->resolve(Lapp/capri/Provider;Ljava/lang/String;)Lapp/capri/Provider;
    move-result-object p1
    invoke-virtual {p1}, Lapp/capri/Provider;->getId()Ljava/lang/String;""",
        "chat URL",
    )
    for register, label in (("v1", "chat URL protocol"), ("v3", "system message"), ("p5", "streaming response")):
        chat = _replace_once(
            chat,
            f"    invoke-static {{{register}, v2}}, Lkotlin/jvm/internal/Intrinsics;->areEqual(Ljava/lang/Object;Ljava/lang/Object;)Z",
            f"    invoke-static {{{register}}}, Lapp/capri/ArenaBridge;->isAnthropic(Ljava/lang/String;)Z",
            label,
        )
    connect = _replace_once(
        connect,
        """    invoke-static {v0, v3}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V

    .line 189""",
        """    invoke-static {v0, v3}, Lkotlin/jvm/internal/Intrinsics;->checkNotNullParameter(Ljava/lang/Object;Ljava/lang/String;)V
    invoke-static {v0}, Lapp/capri/ArenaBridge;->guard(Ljava/lang/String;)V

    .line 189""",
        "pairing fallback guard",
    )
    connect = _replace_once(
        connect,
        """    .line 191
    :try_start_0
    invoke-virtual {v4}, Lapp/capri/Provider;->getId()Ljava/lang/String;""",
        """    .line 191
    :try_start_0
    invoke-static {v4, v0}, Lapp/capri/ArenaBridge;->resolve(Lapp/capri/Provider;Ljava/lang/String;)Lapp/capri/Provider;
    move-result-object v4
    invoke-virtual {v4}, Lapp/capri/Provider;->getId()Ljava/lang/String;""",
        "model-list URL",
    )
    return head + conn + chat + connect


def patch_manifest(source: str) -> str:
    # A new application ID allows the re-signed build to coexist with the
    # original. The actual Kotlin/JNI classes keep their original package.
    source = _replace_once(source, ' package="app.capri"', ' package="app.capri.arena"', "application ID")
    source = _replace_once(source, 'android:label="Capri" android:largeHeap', 'android:label="Capri Arena" android:largeHeap', "application label")
    # Keep the accessibility service and quick-settings tile distinguishable
    # from the original app when both APKs are installed side by side.
    source = source.replace('android:label="Capri"', 'android:label="Capri Arena"')
    import re
    source, count = re.subn(r'android:name="\.([A-Za-z0-9_$]+)"', r'android:name="app.capri.\1"', source)
    if count != 11:
        raise ValueError("Unexpected Capri version: cannot patch component class names")
    return source


def patch(decoded: Path) -> None:
    smali = decoded / "smali" / "app" / "capri"
    brain_path, cloud_path = smali / "BrainKt.smali", smali / "Cloud.smali"
    manifest_path = decoded / "AndroidManifest.xml"
    helper_path = smali / "ArenaBridge.smali"
    if helper_path.exists():
        raise ValueError("This APK is already patched")
    # Prepare every string before writing, so unexpected input cannot leave
    # the decoded tree half-patched.
    brain = patch_provider_list(brain_path.read_text(encoding="utf-8"))
    cloud = patch_cloud(cloud_path.read_text(encoding="utf-8"))
    manifest = patch_manifest(manifest_path.read_text(encoding="utf-8"))
    helper = (Path(__file__).parent / "capri" / "ArenaBridge.smali").read_text(encoding="utf-8")
    brain_path.write_text(brain, encoding="utf-8", newline="\n")
    cloud_path.write_text(cloud, encoding="utf-8", newline="\n")
    manifest_path.write_text(manifest, encoding="utf-8", newline="\n")
    helper_path.write_text(helper, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decoded", type=Path, help="directory produced by apktool d")
    args = parser.parse_args()
    patch(args.decoded)
    print("Capri Arena adapter patched successfully.")


if __name__ == "__main__":
    main()
