#!/usr/bin/env python3
"""Patch an apktool-decoded copy of the Capri 1.22 APK for Arena.

The original APK has no configurable server URL. This small patch adds an
Arena provider using the existing Anthropic-compatible wire protocol. It never
modifies the original APK and refuses to patch an unrecognised build.

Usage: python tools/patch_capri.py /path/to/decoded-apk [--screenshots]
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


def patch_cloud(source: str, *, screenshots: bool = False) -> str:
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
    if screenshots:
        chat = _replace_once(
            chat,
            """    const-string p4, "messages"

    invoke-virtual {p3, p4, p5}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;

    .line 212""",
            """    const-string p4, "messages"

    invoke-virtual {p3, p4, p5}, Lorg/json/JSONObject;->put(Ljava/lang/String;Ljava/lang/Object;)Lorg/json/JSONObject;
    # Only Arena's correlated user turn is changed to include an image block.
    invoke-static {p1, p5}, Lapp/capri/ArenaScreen;->inject(Lapp/capri/Provider;Lorg/json/JSONArray;)V

    .line 212""",
            "screenshot image blocks",
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


def patch_manifest(source: str, *, screenshots: bool = False) -> str:
    # A new ID allows this independently signed screenshot build to coexist
    # with both the original app and the previous Capri Arena, without deleting
    # either app's local data. Kotlin/JNI classes keep their original package.
    app_id = "app.capri.arena.screen" if screenshots else "app.capri.arena"
    label = "Capri Arena Screen" if screenshots else "Capri Arena"
    source = _replace_once(source, ' package="app.capri"', f' package="{app_id}"', "application ID")
    source = _replace_once(source, 'android:label="Capri" android:largeHeap', f'android:label="{label}" android:largeHeap', "application label")
    # Keep the accessibility service and quick-settings tile distinguishable
    # from the original app when both APKs are installed side by side.
    source = source.replace('android:label="Capri"', f'android:label="{label}"')
    import re
    source, count = re.subn(r'android:name="\.([A-Za-z0-9_$]+)"', r'android:name="app.capri.\1"', source)
    if count != 11:
        raise ValueError("Unexpected Capri version: cannot patch component class names")
    return source


def patch_screen_code(sources: dict[str, str]) -> dict[str, str]:
    """Wire screenshot capture into all model-driven phone-control paths."""
    sources = dict(sources)
    ui = sources["HandUi.smali"]
    ui = _replace_once(ui, ".field private post:Z", ".field private post:Z\n\n.field private arenaScreenshot:Z", "screen UI flag")
    ui += """
.method public final getArenaScreenshot()Z
    .locals 1
    iget-boolean v0, p0, Lapp/capri/HandUi;->arenaScreenshot:Z
    return v0
.end method

.method public final setArenaScreenshot(Z)V
    .locals 0
    iput-boolean p1, p0, Lapp/capri/HandUi;->arenaScreenshot:Z
    return-void
.end method
"""
    sources["HandUi.smali"] = ui
    watch = sources["TypeWatch.smali"]
    watch += """
.method public final arenaMarks()Ljava/util/ArrayList;
    .locals 1
    iget-object v0, p0, Lapp/capri/TypeWatch;->marks:Ljava/util/ArrayList;
    return-object v0
.end method
"""
    sources["TypeWatch.smali"] = watch
    hands = sources["Hands.smali"]
    begin = hands.index(".method private final look(")
    end = hands.index(".end method", begin) + len(".end method")
    method = hands[begin:end]
    method = _replace_once(method, """    :cond_0
    sget-object p2, Lapp/capri/App;->INSTANCE:Lapp/capri/App;""", """    :cond_0
    invoke-virtual {p2}, Lapp/capri/HandUi;->getArenaScreenshot()Z
    move-result v1
    if-eqz v1, :arena_original_look
    invoke-static {v0, p1, p3}, Lapp/capri/ArenaScreen;->schedule(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;)V
    return-void

    :arena_original_look
    sget-object p2, Lapp/capri/App;->INSTANCE:Lapp/capri/App;""", "screen dispatch")
    sources["Hands.smali"] = hands[:begin] + method + hands[end:]
    for filename, ui_reg, model_reg, run in (
        ("PhoneView.smali", "v1", "v8", "{v0, p3, v1, v2}"),
        ("TalkBox.smali", "v4", "v2", "{v3, p1, v4, v0}"),
        ("Solo$run$step$act$1.smali", "v8", "v4", "{v0, p1, v8, v9}"),
    ):
        call = f"    invoke-virtual {run}, Lapp/capri/Hands;->run(Lapp/capri/HandCall;Lapp/capri/HandUi;Lkotlin/jvm/functions/Function1;)V"
        replacement = f"    invoke-static {{{ui_reg}, {model_reg}}}, Lapp/capri/ArenaScreen;->configure(Lapp/capri/HandUi;Lapp/capri/ModelEntry;)V\n\n" + call
        sources[filename] = _replace_once(sources[filename], call, replacement, f"screen provider in {filename}")
    return sources


def patch(decoded: Path, *, screenshots: bool = False) -> None:
    smali = decoded / "smali" / "app" / "capri"
    brain_path, cloud_path = smali / "BrainKt.smali", smali / "Cloud.smali"
    manifest_path = decoded / "AndroidManifest.xml"
    helper_path = smali / "ArenaBridge.smali"
    if helper_path.exists():
        raise ValueError("This APK is already patched")
    # Prepare every string before writing, so unexpected input cannot leave
    # the decoded tree half-patched.
    brain = patch_provider_list(brain_path.read_text(encoding="utf-8"))
    cloud = patch_cloud(cloud_path.read_text(encoding="utf-8"), screenshots=screenshots)
    manifest = patch_manifest(manifest_path.read_text(encoding="utf-8"), screenshots=screenshots)
    helper = (Path(__file__).parent / "capri" / "ArenaBridge.smali").read_text(encoding="utf-8")
    extra: dict[str, str] = {}
    screen_files: dict[str, str] = {}
    xml_path = decoded / "res" / "xml" / "typewatch.xml"
    xml = ""
    if screenshots:
        names = ("HandUi.smali", "TypeWatch.smali", "Hands.smali", "PhoneView.smali", "TalkBox.smali", "Solo$run$step$act$1.smali")
        extra = patch_screen_code({name: (smali / name).read_text(encoding="utf-8") for name in names})
        xml = _replace_once(xml_path.read_text(encoding="utf-8"),
                            'android:canRetrieveWindowContent="true"',
                            'android:canRetrieveWindowContent="true" android:canTakeScreenshot="true"',
                            "Android screenshot capability")
        screen_dir = Path(__file__).parent / "capri" / "screen"
        screen_files = {p.name: p.read_text(encoding="utf-8") for p in screen_dir.glob("ArenaScreen*.smali")}
        if len(screen_files) != 9 or any((smali / name).exists() for name in screen_files):
            raise ValueError("Missing or conflicting screenshot helper classes")
    brain_path.write_text(brain, encoding="utf-8", newline="\n")
    cloud_path.write_text(cloud, encoding="utf-8", newline="\n")
    manifest_path.write_text(manifest, encoding="utf-8", newline="\n")
    helper_path.write_text(helper, encoding="utf-8", newline="\n")
    for name, contents in {**extra, **screen_files}.items():
        (smali / name).write_text(contents, encoding="utf-8", newline="\n")
    if screenshots:
        xml_path.write_text(xml, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decoded", type=Path, help="directory produced by apktool d")
    parser.add_argument("--screenshots", action="store_true", help="build the separate screenshot-enabled app")
    args = parser.parse_args()
    patch(args.decoded, screenshots=args.screenshots)
    print("Capri Arena adapter patched successfully.")


if __name__ == "__main__":
    main()
