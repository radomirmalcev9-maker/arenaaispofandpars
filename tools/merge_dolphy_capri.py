#!/usr/bin/env python3
"""Integrate the decoded, version-pinned Dolphy 2.6 and Capri Arena Screen APKs.

This operates ONLY on disposable apktool decode directories. It does not download,
modify, sign, or overwrite either input APK. See tools/dolphy-capri/README.md.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID = "http://schemas.android.com/apk/res/android"
A = "{" + ANDROID + "}"
ET.register_namespace("android", ANDROID)
ROOT = Path(__file__).resolve().parent.parent
APP_ID = "com.droid.dolphy.capri"


def dex_classes(path: Path) -> set[str]:
    data = path.read_bytes()
    if not data.startswith(b"dex\n"):
        raise ValueError(f"Not a dex: {path}")
    def u32(offset: int) -> int:
        return struct.unpack_from("<I", data, offset)[0]
    strings_off, types_off = u32(0x3c), u32(0x44)
    count, classes_off = u32(0x60), u32(0x64)
    result = set()
    for index in range(count):
        type_idx = u32(classes_off + index * 32)
        str_idx = u32(types_off + type_idx * 4)
        pos = u32(strings_off + str_idx * 4)
        while data[pos] & 0x80:  # skip string_data_item's UTF-16 length (ULEB128)
            pos += 1
        pos += 1
        end = data.index(b"\0", pos)
        result.add(data[pos:end].decode("utf-8"))
    return result


def smali_classes(path: Path) -> dict[str, Path]:
    classes: dict[str, Path] = {}
    for directory in sorted(path.glob("smali*")):
        if not directory.is_dir():
            continue
        for file in directory.rglob("*.smali"):
            with file.open(encoding="utf-8") as stream:
                line = stream.readline()
            found = re.search(r"(L[^;]+;)", line)
            if not found or not line.startswith(".class "):
                raise ValueError(f"Unexpected smali class header: {file}")
            name = found.group(1)
            if name in classes:
                raise ValueError(f"Duplicate Capri class: {name}")
            classes[name] = file
    return classes


def require_replace(text: str, old: str, new: str, count: int = 1) -> str:
    if text.count(old) != count:
        raise ValueError(f"Expected {count} occurrence(s) of {old[:80]!r}, got {text.count(old)}")
    return text.replace(old, new)


def patch_capri_tools(capri: Path) -> None:
    hand_text = capri / "smali/app/capri/HandText.smali"
    text = hand_text.read_text(encoding="utf-8")
    anchor = "    sput-object v0, Lapp/capri/HandText;->NAMES:Ljava/util/HashSet;"
    text = require_replace(text, anchor, anchor + "\n\n    const-string v1, \"dolphy\"\n\n"
                           "    invoke-virtual {v0, v1}, Ljava/util/HashSet;->add(Ljava/lang/Object;)Z")
    for field in ("GATE", "DEVICE"):
        expression = re.compile(r'(    const-string v1, ")([^"\n]*)("\n\n'
                                r'    sget-object v2, Lkotlin/text/RegexOption;->IGNORE_CASE:Lkotlin/text/RegexOption;\n\n'
                                r'    invoke-direct \{v0, v1, v2\}, Lkotlin/text/Regex;-><init>\(Ljava/lang/String;Lkotlin/text/RegexOption;\)V\n\n'
                                r'    sput-object v0, Lapp/capri/HandText;->' + field + r':)')
        text, n = expression.subn(lambda match: match.group(1) + r'dolphy|\u0434\u043e\u043b\u0444\u0438|'
                                   + match.group(2) + match.group(3), text)
        if n != 1:
            raise ValueError(f"Could not patch {field} intent filter: {n}")
    hand_text.write_text(text, encoding="utf-8")

    hands = capri / "smali/app/capri/Hands.smali"
    text = hands.read_text(encoding="utf-8")
    marker = "    .line 46\n    :try_start_0\n    invoke-direct {p0, p1, p2, p3}, Lapp/capri/Hands;->go("
    replacement = ("    invoke-static {p1, p2, p3}, Lapp/capri/integration/DolphyBridge;"
                   "->intercept(Ljava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)Z\n\n"
                   "    move-result v0\n\n    if-eqz v0, :capri_original_tool\n\n"
                   "    return-void\n\n    :capri_original_tool\n\n" + marker)
    text = require_replace(text, marker, replacement)
    hands.write_text(text, encoding="utf-8")

    persona = capri / "smali/app/capri/Persona.smali"
    text = persona.read_text(encoding="utf-8")
    anchor = r"open|app\nclose|app"
    replacement = (r"open|app (for Dolphy use open|dolphy)\n"
                   r"dolphy|routes lists available screens\n"
                   r"dolphy|open|route opens a listed screen\n"
                   r"dolphy|list|device|filter or dolphy|list|android|filter lists APIs and argument types\n"
                   r"dolphy|device|method|[JSON arguments] or dolphy|android|method|[JSON arguments] "
                   r"calls a Dolphy API; the phone user must confirm every call. "
                   r"Omit the callback argument from the JSON array. For example dolphy|device|irStatusJson|[]\n"
                   r"close|app")
    text = require_replace(text, anchor, replacement)
    persona.write_text(text, encoding="utf-8")

    watch = capri / "smali/app/capri/TypeWatch.smali"
    text = watch.read_text(encoding="utf-8")
    pattern = re.compile(r"    invoke-virtual \{p0\}, Lapp/capri/TypeWatch;->getPackageName\(\)Ljava/lang/String;"
                         r"\n\n    move-result-object (v\d+)\n\n"
                         r"    invoke-static \{(v\d+), \1\}, Lkotlin/jvm/internal/Intrinsics;"
                         r"->areEqual\(Ljava/lang/Object;Ljava/lang/Object;\)Z")
    matches = list(pattern.finditer(text))
    node = "Landroid/view/accessibility/AccessibilityNodeInfo;"
    event = "Landroid/view/accessibility/AccessibilityEvent;"
    expected = [
        ("v4", "v5", "isOwnNodeBlocked", node),
        ("v1", "v0", "isOwnNodeBlocked", node),
        ("v4", "p1", "isOwnEventBlocked", event),
        ("v1", "v0, p1", "isOwnRootOrEventBlocked", node + event),
        ("v3", "v2", "isOwnNodeBlocked", node),
    ]
    if len(matches) != len(expected):
        raise ValueError(f"Expected 5 own-package accessibility checks, got {len(matches)}")
    for match, (package_reg, window_regs, method, signature) in reversed(list(zip(matches, expected))):
        if match.group(2) != package_reg:
            raise ValueError(f"Unexpected accessibility register: {match.group(2)}, expected {package_reg}")
        replacement = ("    invoke-static {p0, " + package_reg + ", " + window_regs
                       + "}, Lapp/capri/integration/Tabs;->" + method
                       + "(Landroid/content/Context;Ljava/lang/Object;" + signature + ")Z")
        text = text[:match.start()] + replacement + text[match.end():]
    watch.write_text(text, encoding="utf-8")


def merge_resources(dolphy: Path, capri_res: Path) -> dict[str, str]:
    public_path = dolphy / "res/values/public.xml"
    tree = ET.parse(public_path)
    target = tree.getroot()
    maximum: dict[str, int] = {}
    existing: dict[tuple[str, str], str] = {}
    for item in target:
        kind, name, number = item.get("type"), item.get("name"), item.get("id")
        if kind is None or name is None or number is None:
            continue
        existing[(kind, name)] = number
        maximum[kind] = max(maximum.get(kind, 0), int(number, 16))
    remap = {}
    for item in ET.parse(capri_res / "res/values/public.xml").getroot():
        kind, name, old = item.get("type"), item.get("name"), item.get("id")
        if kind is None or name is None or old is None:
            raise ValueError("Incomplete Capri public resource")
        if (kind, name) == ("mipmap", "ic_launcher"):
            new = existing[(kind, name)]
        else:
            if (kind, name) in existing:
                raise ValueError(f"Unexpected resource name collision: {kind}/{name}")
            if kind not in maximum:
                raise ValueError(f"Unknown Dolphy resource type: {kind}")
            maximum[kind] += 1
            if (maximum[kind] & 0xffff) == 0:
                raise ValueError(f"Resource type {kind} exhausted")
            new = f"0x{maximum[kind]:08x}"
            ET.SubElement(target, "public", {"type": kind, "name": name, "id": new})
        remap[old] = new
    ET.indent(tree, space="    ")
    tree.write(public_path, encoding="utf-8", xml_declaration=True)

    resources = [
        ("values/ids.xml", "values/capri_ids.xml"),
        ("values/integers.xml", "values/capri_integers.xml"),
        ("values/strings.xml", "values/capri_strings.xml"),
        ("values/colors.xml", "values/capri_colors.xml"),
        ("values/styles.xml", "values/capri_styles.xml"),
        ("values-v29/styles.xml", "values-v29/capri_styles.xml"),
        ("layout/__arcore_education.xml", "layout/__arcore_education.xml"),
        ("xml/typewatch.xml", "xml/typewatch.xml"),
        ("mipmap-xxxhdpi/ic_fg.png", "mipmap-xxxhdpi/ic_fg.png"),
    ]
    for source, destination in resources:
        src = capri_res / "res" / source
        dst = dolphy / "res" / destination
        if dst.exists():
            raise ValueError(f"Would overwrite Dolphy resource: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    # Explain the screen-sharing implications without promising password secrecy.
    strings = dolphy / "res/values/capri_strings.xml"
    string_tree = ET.parse(strings)
    desc = string_tree.getroot().find("string[@name='watch_desc']")
    if desc is None:
        raise ValueError("Accessibility description missing")
    desc.text = ("По вашему запросу Capri может читать экран и нажимать элементы, в том числе в Dolphy. "
                 "Снимки экрана могут передаваться подключённому ИИ; защищённые экраны ограничены Android.")
    ET.indent(string_tree, space="    ")
    string_tree.write(strings, encoding="utf-8", xml_declaration=True)
    return remap


def merge_manifest(dolphy: Path, capri_res: Path) -> None:
    path = dolphy / "AndroidManifest.xml"
    target_tree = ET.parse(path)
    root = target_tree.getroot()
    if root.get("package") != "com.droid.dolphy":
        raise ValueError("Unexpected Dolphy application id")
    capri = ET.parse(capri_res / "AndroidManifest.xml").getroot()
    root.set("package", APP_ID)
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key in (A + "authorities", A + "name") and value in {
                "com.droid.dolphy.shizuku", "com.droid.dolphy.provider",
                "com.droid.dolphy.androidx-startup", "com.droid.dolphy.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"
            }:
                node.set(key, value.replace("com.droid.dolphy.", APP_ID + ".", 1))
    app = root.find("application")
    capri_app = capri.find("application")
    if app is None or capri_app is None:
        raise ValueError("Application manifest missing")
    app.set(A + "label", "Dolphy + Capri")
    app.set(A + "largeHeap", "true")
    app.set(A + "hardwareAccelerated", "true")

    # Deduplicate permissions by name. Preserve Dolphy's existing restrictions.
    permissions = {element.get(A + "name") for element in root.findall("uses-permission")}
    features = {(element.get(A + "name"), element.get(A + "glEsVersion")) for element in root.findall("uses-feature")}
    app_index = list(root).index(app)
    for element in capri:
        if element.tag == "uses-permission" and element.get(A + "name") not in permissions:
            root.insert(app_index, copy.deepcopy(element)); app_index += 1
            permissions.add(element.get(A + "name"))
        elif element.tag == "uses-feature":
            key = (element.get(A + "name"), element.get(A + "glEsVersion"))
            if key not in features:
                root.insert(app_index, copy.deepcopy(element)); app_index += 1
                features.add(key)
    queries = root.find("queries")
    capri_queries = capri.find("queries")
    if queries is None or capri_queries is None:
        raise ValueError("Expected both query sections")
    for element in capri_queries:
        queries.append(copy.deepcopy(element))

    dolphy_activity = app.find("activity[@" + A + "name='com.droid.dolphy.MainActivity']")
    if dolphy_activity is None:
        raise ValueError("Dolphy main activity missing")
    launch_filters = [element for element in dolphy_activity.findall("intent-filter")
                      if any(x.get(A + "name") == "android.intent.category.LAUNCHER"
                             for x in element.findall("category"))]
    if len(launch_filters) != 1:
        raise ValueError("Expected one Dolphy launcher intent")
    dolphy_activity.remove(launch_filters[0])  # NFC/deep links remain intact.
    capri_activity = None
    for element in capri_app:
        node = copy.deepcopy(element)
        if node.tag == "activity" and node.get(A + "name") == "app.capri.MainActivity":
            node.set(A + "launchMode", "singleTop")
            node.set(A + "label", "Dolphy + Capri")
            node.set(A + "theme", "@style/CapriTheme")
            capri_activity = node
        elif node.tag == "activity" and node.get(A + "name") == "com.google.ar.core.InstallActivity":
            pass
        elif node.tag == "meta-data":
            pass
        elif node.tag in ("service", "receiver", "activity"):
            pass
        else:
            raise ValueError(f"Unexpected Capri application component: {node.tag}")
        app.append(node)
    if capri_activity is None:
        raise ValueError("Capri main activity missing")
    ET.SubElement(app, "provider", {
        A + "name": "app.capri.integration.BootstrapProvider",
        A + "authorities": APP_ID + ".capri.integration",
        A + "exported": "false",
        A + "initOrder": "100",
    })
    ET.indent(target_tree, space="    ")
    target_tree.write(path, encoding="utf-8", xml_declaration=True)


def copy_assets_and_libs(dolphy: Path, capri: Path) -> None:
    for source in (capri / "assets").rglob("*"):
        if source.is_file():
            dest = dolphy / "assets" / source.relative_to(capri / "assets")
            if dest.exists():
                raise ValueError(f"Asset collision: {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
    shutil.copyfile(ROOT / "tools/dolphy-capri/routes.json", dolphy / "assets/capri_dolphy_routes.json")
    libs = dolphy / "lib"
    for abi in libs.iterdir():
        if abi.is_dir() and abi.name != "arm64-v8a":
            shutil.rmtree(abi)  # Capri JNI is ARM64-only; make incompatible phones fail installation.
    for source in (capri / "lib/arm64-v8a").glob("*.so"):
        dest = libs / "arm64-v8a" / source.name
        if dest.exists():
            raise ValueError(f"Native library collision: {source.name}")
        shutil.copyfile(source, dest)


def relocate_and_remap(capri: Path, dolphy_classes: set[str], resource_map: dict[str, str]) -> dict[str, int]:
    classes = smali_classes(capri)
    embedded = {name for name in classes if not name.startswith(("Lapp/capri/", "Lcom/google/ar/core/"))
                and not (name.startswith("Landroid/") and not name.startswith("Landroid/support/"))}
    roots = {name[1:].split("/", 1)[0] for name in embedded
             if re.fullmatch(r"[A-Za-z][0-9]?", name[1:].split("/", 1)[0])}
    roots.update({"kotlin", "androidx", "okhttp3"})
    keep = {name for name in classes if name.startswith(("Lapp/capri/", "Lcom/google/ar/core/"))}
    if keep & dolphy_classes:
        raise ValueError(f"JNI/Capri class collision: {sorted(keep & dolphy_classes)[:3]}")
    renamed = {"Lcapriembed/" + name[1:] for name in embedded}
    if renamed & dolphy_classes or renamed & keep:
        raise ValueError("Class collision after relocation")

    # Framework stubs in Capri are synthetic NoClassDefFoundError shims. Keeping
    # them alongside Dolphy's identical framework class names breaks multidex.
    stubs = {name for name in classes if name.startswith("Landroid/") and not name.startswith("Landroid/support/")}
    for name in stubs:
        classes[name].unlink()
    descriptor = re.compile(r"L([A-Za-z0-9_$-]+(?:/[A-Za-z0-9_$-]+)+)(?=[;<])")
    dotted = re.compile(r'("(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*")')
    const = re.compile(r"\bconst(?P<high>/high16)?(?P<reg>\s+[vp]\d+,\s+)(?P<value>0x7f[0-9a-fA-F]{6})\b")
    changed = 0
    resources = 0
    for name, file in classes.items():
        if name in stubs:
            continue
        text = file.read_text(encoding="utf-8")
        def desc(match: re.Match) -> str:
            internal = match.group(1)
            full = "L" + internal + ";"
            root = internal.split("/", 1)[0]
            if full in embedded or root in roots or internal.startswith("android/support/"):
                return "Lcapriembed/" + internal
            return match.group(0)
        text, c = descriptor.subn(desc, text)
        changed += c
        def dot(match: re.Match) -> str:
            value = match.group(1)[1:-1]
            internal = value.replace(".", "/")
            if ("L" + internal + ";" in embedded or value.startswith(("kotlin.", "androidx.", "android.support.", "okhttp3."))):
                return '"capriembed.' + value + '"'
            return match.group(0)
        text = dotted.sub(dot, text)
        def resource(match: re.Match) -> str:
            nonlocal resources
            value = match.group("value").lower()
            if value not in resource_map:
                return match.group(0)
            resources += 1
            new = resource_map[value]
            # high16 cannot encode an ID whose low 16 bits are nonzero.
            high = match.group("high") if int(new, 16) & 0xffff == 0 else ""
            return "const" + (high or "") + match.group("reg") + new
        text = const.sub(resource, text)
        file.write_text(text, encoding="utf-8")
    return {"capri_classes": len(classes), "relocated_classes": len(embedded),
            "removed_platform_stubs": len(stubs), "descriptor_occurrences": changed,
            "remapped_resource_constants": resources}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dolphy", required=True, type=Path, help="apktool d -s Dolphy directory (modified in place)")
    parser.add_argument("--capri", required=True, type=Path, help="apktool d -r Capri directory (modified in place)")
    parser.add_argument("--capri-res", required=True, type=Path, help="apktool d -s Capri resource directory")
    args = parser.parse_args()
    dolphy_classes: set[str] = set()
    for dex in sorted(args.dolphy.glob("classes*.dex")):
        before = len(dolphy_classes)
        found = dex_classes(dex)
        if dolphy_classes & found:
            raise ValueError(f"Duplicate class in Dolphy input: {dex}")
        dolphy_classes.update(found)
        print(f"{dex.name}: {len(dolphy_classes)-before} classes")
    if not dolphy_classes:
        raise ValueError("No Dolphy dex files")
    patch_capri_tools(args.capri)
    resource_map = merge_resources(args.dolphy, args.capri_res)
    merge_manifest(args.dolphy, args.capri_res)
    copy_assets_and_libs(args.dolphy, args.capri)
    stats = relocate_and_remap(args.capri, dolphy_classes, resource_map)
    print(json.dumps({"application_id": APP_ID, "dolphy_classes": len(dolphy_classes),
                      "resources": resource_map, "capri": stats}, indent=2))


if __name__ == "__main__":
    main()
