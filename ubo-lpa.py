#!/usr/bin/env python3
"""
ubo-lpa: run uBlock Origin in Chrome 151 through 157 as a legacy packaged app.

One implementation for Linux and Windows. The patching, companion build and
consistency checks are identical on both. Only the delivery to Chrome differs,
and the two Installer subclasses at the bottom of this file implement it.

    ubo-lpa.py install     download, patch and build, then add both extensions
                           to every Chrome profile (Linux) or print the steps
                           for loading them by hand (Windows)
    ubo-lpa.py update      alias for install; rebuilds a current, consistent
                           installation only after a confirmation prompt
    ubo-lpa.py check       verify required files, IDs, cross-references and
                           patch markers
    ubo-lpa.py status      recorded version and IDs, the install directory,
                           and which Chrome profiles have the extensions
    ubo-lpa.py uninstall   remove both extensions and undo the profile changes
    ubo-lpa.py timer       run install daily: a systemd user timer on Linux, a
                           scheduled task on Windows; --off removes it, --keep
                           sets it up unless it was switched off
    ubo-lpa.py load        Windows: load both extensions into Chrome over its
                           DevTools connection, after a click on Allow in
                           Chrome; --profiles names the profile folders
    ubo-lpa.py setup-info  print, as JSON, what the Windows setup wizard shows
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import html
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import contextlib
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

GITHUB_REPO = "gorhill/uBlock"
# Releases that all eight edits apply to. Edit 8 patches js/vapi-scripting.js,
# which uBO has had since 1.76.0.
KNOWN_GOOD = ["1.76.2", "1.76.0"]
RESIZE_MSG = "ubo-resize"
STOCK_UBO_ID = "cjpalhdlnbpafiamejdnhcphjbkeiagm"
TIMER_NAME = "ubo-lpa-update"

IS_WINDOWS = platform.system() == "Windows"
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
ASSETS = Path(__file__).resolve().parent / "assets"

# --- output ---------------------------------------------------------------

# pythonw.exe, which the Windows scheduled task runs, starts without a console,
# so sys.stdout is None until main() points it at the log file.
_COLOR = sys.stdout is not None and sys.stdout.isatty() and not IS_WINDOWS


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_c('1;34', '::')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('1;32', 'OK')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33', '!')}  {msg}")


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"{_c('1;31', 'error:')} {msg}", file=sys.stderr)
    raise SystemExit(1)


def ask(prompt: str, default_yes: bool) -> bool:
    """Prompt, or take the default silently when there is no TTY (cron, CI)."""
    if sys.stdin is None or not sys.stdin.isatty():
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        reply = input(f"{_c('1;34', '::')} {prompt} {suffix} ").strip().lower()
    except EOFError:
        return default_yes
    if not reply:
        return default_yes
    return reply.startswith("y")


# --- identity -------------------------------------------------------------


def id_from_bytes(data: bytes) -> str:
    """Chrome's extension-ID encoding: sha256, first 16 bytes, nibbles to a-p."""
    digest = hashlib.sha256(data).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def _generate_key(pem: Path) -> None:
    """Write a fresh 2048-bit RSA private key, via cryptography or openssl."""
    pem.parent.mkdir(parents=True, exist_ok=True)
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem.write_bytes(
            priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    except ImportError:
        if not shutil.which("openssl"):
            die(
                "need either the python 'cryptography' package or the openssl\n"
                "         command to generate an extension key.\n"
                "         pip install cryptography"
            )
        subprocess.run(
            ["openssl", "genrsa", "-out", str(pem), "2048"],
            check=True,
            capture_output=True,
        )
    try:
        pem.chmod(0o600)
    except OSError:
        pass


def _public_der(pem: Path) -> bytes:
    """SubjectPublicKeyInfo DER for a private key on disk."""
    try:
        from cryptography.hazmat.primitives import serialization

        priv = serialization.load_pem_private_key(pem.read_bytes(), password=None)
        return priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except ImportError:
        result = subprocess.run(
            ["openssl", "rsa", "-in", str(pem), "-pubout", "-outform", "DER"],
            check=True,
            capture_output=True,
        )
        return result.stdout


def ensure_key(pem: Path) -> tuple[str, str]:
    """
    Return (base64 SPKI for the manifest "key" field, extension id).

    The key is stored beside the extensions and reused, so the id survives
    reinstalls and directory moves. Chrome derives the id from this key as it
    does for a packed extension: sha256 of the SPKI DER, first 16 bytes,
    nibbles mapped to a-p.

    Without a key Chrome would derive the id from the directory path
    (GenerateIdForPath), which ties identity to a location the user can change.
    On Windows the user picks that location in the Load unpacked dialog.
    """
    if not pem.exists():
        _generate_key(pem)
    der = _public_der(pem)
    return base64.b64encode(der).decode(), id_from_bytes(der)


# --- download -------------------------------------------------------------


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ubo-lpa"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def version_tuple(tag: str) -> tuple:
    """(1, 74, 0) from "1.74.0"; unparseable parts sort lowest."""
    parts = []
    for chunk in str(tag).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def resolve_latest_tag() -> tuple[str | None, bool]:
    """
    (tag, from_github): the newest release, or, when GitHub cannot say, the
    first version in KNOWN_GOOD that a HEAD request finds. That list is fixed
    in this file, so the fallback cannot find a release newer than its first
    entry.
    """
    tag = latest_release()
    if tag:
        return tag, True
    for version in KNOWN_GOOD:
        url = (
            f"https://github.com/{GITHUB_REPO}/releases/download/"
            f"{version}/uBlock0_{version}.chromium.zip"
        )
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "ubo-lpa"}
            )
            urllib.request.urlopen(req, timeout=15)
            return version, False
        except Exception:
            continue
    return None, False


def latest_release() -> str | None:
    """
    The newest uBO release, or None. GitHub's release API answers 60
    unauthenticated requests an hour per IP address, and machines that share
    an address can use them up. The /releases/latest page on github.com still
    redirects to the newest release's tag when that happens.
    """
    try:
        data = json.loads(
            _get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", 20)
        )
        if data.get("tag_name"):
            return data["tag_name"]
    except Exception:
        pass
    try:
        request = urllib.request.Request(
            f"https://github.com/{GITHUB_REPO}/releases/latest", method="HEAD",
            headers={"User-Agent": "ubo-lpa"})
        with urllib.request.urlopen(request, timeout=20) as response:
            final = response.geturl()
    except Exception:
        return None
    match = re.search(r"/releases/tag/([^/?#]+)$", final)
    return urllib.parse.unquote(match.group(1)) if match else None


def release_url(tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"


def ubo_zip_version(archive: Path) -> str:
    """The uBO version of a Chromium release zip, from its manifest."""
    try:
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read("uBlock0.chromium/manifest.json"))
        return str(manifest["version"])
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        die(f"{archive} is not a uBlock Origin Chromium release ({exc})")


def download_ubo(tag: str, dest: Path, archive: Path | None = None) -> None:
    """Extract uBO into dest, from archive when given, else from its release."""
    if archive is not None:
        info(f"Using uBO {tag} from {archive.name}")
        blob = archive.read_bytes()
    else:
        url = (
            f"https://github.com/{GITHUB_REPO}/releases/download/"
            f"{tag}/uBlock0_{tag}.chromium.zip"
        )
        info(f"Downloading uBO {tag}")
        try:
            blob = _get(url, 300)
        except urllib.error.URLError as exc:
            die(f"download failed: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ubo.zip"
        archive.write_bytes(blob)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        root = Path(tmp) / "uBlock0.chromium"
        if not (root / "manifest.json").is_file():
            die(f"unexpected archive layout in uBlock0_{tag}.chromium.zip")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(root), str(dest))
    ok(f"Extracted uBO {tag}")


# --- patching -------------------------------------------------------------


def asset(name: str, **subs: str) -> str:
    text = (ASSETS / name).read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(f"__{key}__", value)
    return text


def write_prefs(path: Path, data: dict) -> None:
    """Replace a Chrome Preferences file, keeping its mode and flushing to disk."""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_suffix(path.suffix + ".ubo-lpa.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def patch_ubo(ext: Path, companion_id: str, embed_key: str | None) -> None:
    """Apply the eight edits in WRITEUP.md to a freshly extracted copy."""
    manifest_path = ext / "manifest.json"
    manifest = read_json(manifest_path)

    # 1. app key -> kLegacyPackagedApp. IsExtensionAffected() only tests
    #    kExtension, kLoginScreenExtension and kUserScript, so a legacy
    #    packaged app is never considered for the MV2 shutdown.
    manifest["app"] = {"launch": {"local_path": "dashboard.html"}}
    if embed_key:
        manifest["key"] = embed_key

    #    Chrome does not resolve __MSG_*__ for this install path, so the
    #    extensions page would show a literal __MSG_extShortDesc__.
    locale = manifest.get("default_locale", "en")
    messages_path = ext / "_locales" / locale / "messages.json"
    if messages_path.exists():
        messages = read_json(messages_path)

        def resolve(node):
            # Tokens appear nested too, e.g. commands.*.description.
            if isinstance(node, str):
                if node.startswith("__MSG_") and node.endswith("__"):
                    token = node[6:-2]
                    if token in messages:
                        return messages[token]["message"]
                return node
            if isinstance(node, dict):
                return {k: resolve(v) for k, v in node.items()}
            if isinstance(node, list):
                return [resolve(v) for v in node]
            return node

        manifest = resolve(manifest)

    # 3. expose the panel so the companion can iframe it
    # Only popup-fenix.html, the document the companion iframes, has to be
    # reachable from another origin. Its dependencies (css/, js/, img/) load
    # from the same origin once the document is running. Listing those
    # dependencies would unnecessarily expose more resources to websites.
    war = list(manifest.get("web_accessible_resources", []))
    if "popup-fenix.html" not in war:
        war.append("popup-fenix.html")
    manifest["web_accessible_resources"] = war

    # 5. let the companion message uBO for the badge count
    manifest["externally_connectable"] = {"ids": [companion_id]}

    # 4. frame-ancestors in the CSP
    # A web-accessible resource can be framed by any page, and uBO's panel has
    # a working power switch in it, so an ordinary site could frame it and
    # induce a click that disables filtering. frame-ancestors limits framing
    # to uBO itself and the companion.
    csp = manifest.get("content_security_policy", "script-src 'self'; object-src 'self'")
    directives = [d.strip() for d in csp.split(";") if d.strip()]
    kept = [d for d in directives if d.split()[0].lower() != "frame-ancestors"]
    kept.append(f"frame-ancestors 'self' chrome-extension://{companion_id}")
    manifest["content_security_policy"] = "; ".join(kept)

    # Retain browser_action and commands. Chrome warns about them for a legacy
    # packaged app, but vapi-background.js reads
    # getManifest().browser_action.default_title.
    write_json(manifest_path, manifest)

    # 2. browserAction shim, inside webext.js so every uBO page is covered
    webext = ext / "js" / "webext.js"
    source = webext.read_text(encoding="utf-8")
    if "ubo-lpa-shim" not in source:
        anchor = source.find("const promisifyNoFail")
        if anchor < 0:
            die("webext.js: promisifyNoFail not found; uBO's layout changed")
        shim = asset("webext-shim.js")
        webext.write_text(source[:anchor] + shim + "\n" + source[anchor:], encoding="utf-8")

    # 6. badge bridge
    vapi = ext / "js" / "vapi-background.js"
    source = vapi.read_text(encoding="utf-8")
    if "uboBadge" not in source:
        with vapi.open("a", encoding="utf-8") as handle:
            handle.write(asset("badge-bridge.js"))

    # 7. resize notifier, external file because uBO's CSP is script-src 'self'
    (ext / "js" / "popup-resize.js").write_text(
        asset("popup-resize.js", RESIZE_MSG=RESIZE_MSG, COMP_ID=companion_id),
        encoding="utf-8",
    )
    popup = ext / "popup-fenix.html"
    html = popup.read_text(encoding="utf-8")
    if "popup-resize.js" not in html:
        html = html.replace("</body>", '<script src="js/popup-resize.js"></script>\n</body>')
        popup.write_text(html, encoding="utf-8")

    # 8. hand each site's main-world scriptlets to the companion, which
    #    registers them with chrome.userScripts so they run before the page's
    #    own scripts
    patch_scriptlet_injection(ext / "js" / "vapi-scripting.js", companion_id)

    ok("uBO patched")


def patch_scriptlet_injection(path: Path, companion_id: str) -> None:
    """
    Edit 8. Each replacement must match text in uBO's vapi-scripting.js
    exactly once; when uBO changes that text, the build stops and does not
    write a partly edited file.
    """
    if not path.exists():
        die(f"{path.name} is missing; uBO 1.76.0 or newer is required")
    source = path.read_text(encoding="utf-8")
    if "ubolpaUserScripts" in source:
        return
    edits = [
        (
            "/* global browser */\n",
            "/* global browser */\n"
            + asset("userscripts-bridge.js", COMP_ID=companion_id),
        ),
        (
            "    if ( tabId < 0 ) { return; }\n"
            "    rememberFrame(details);\n"
            "    let { url } = details;\n"
            "    if ( frameId !== 0 && reIsHTTP.test(url) === false ) {\n"
            "        url = getEffectiveURL(tabId, frameId);\n"
            "        if ( url === undefined ) { return; }\n"
            "    }\n"
            "    if ( reIsHTTP.test(url) === false ) { return; }\n"
            "    registerContentScript(url);\n",
            "    if ( tabId < 0 ) { return; }\n"
            "    rememberFrame(details);\n"
            "    ubolpaUserScripts.navStarted(details);\n"
            "    let { url } = details;\n"
            "    if ( frameId !== 0 && reIsHTTP.test(url) === false ) {\n"
            "        url = getEffectiveURL(tabId, frameId);\n"
            "        if ( url === undefined ) { return; }\n"
            "    }\n"
            "    if ( reIsHTTP.test(url) === false ) { return; }\n"
            "    registerContentScript(url);\n",
        ),
        (
            "    const code = uBO_assembleIsolatedCode(result) ?? null;\n"
            "    contentScriptDict.set(hostname, { code, t: Date.now() });\n",
            "    const code = uBO_assembleIsolatedCode(result) ?? null;\n"
            "    const entry = { code, t: Date.now() };\n"
            "    contentScriptDict.set(hostname, entry);\n"
            "    ubolpaUserScripts.register(hostname, entry, result);\n",
        ),
        (
            "    const entry = contentScriptDict.get(hostname);\n"
            "    if ( Boolean(entry?.code) === false ) { return; }\n"
            "    webext.tabs.executeScript(tabId, {\n"
            "        frameId,\n"
            "        matchAboutBlank: true,\n"
            "        runAt: 'document_start',\n"
            "        code: entry.code,\n"
            "    });\n"
            "    entry.t = Date.now();\n",
            "    const entry = contentScriptDict.get(hostname);\n"
            "    const code = ubolpaUserScripts.codeFor(entry, details);\n"
            "    if ( Boolean(code) === false ) { return; }\n"
            "    webext.tabs.executeScript(tabId, {\n"
            "        frameId,\n"
            "        matchAboutBlank: true,\n"
            "        runAt: 'document_start',\n"
            "        code,\n"
            "    });\n",
        ),
        (
            "    for ( const key of sortedKeys ) {\n"
            "        contentScriptDict.delete(key);\n"
            "    }\n",
            "    for ( const key of sortedKeys ) {\n"
            "        contentScriptDict.delete(key);\n"
            "        ubolpaUserScripts.forget(key);\n"
            "    }\n",
        ),
        (
            "    if ( initialized ) { return; }\n",
            "    if ( initialized ) { return; }\n"
            "    // Registered user scripts outlive a restart, but the secrets in\n"
            "    // their code do not.\n"
            "    ubolpaUserScripts.reset();\n",
        ),
        (
            "export function reset() {\n"
            "    contentScriptDict.clear();\n",
            "export function reset() {\n"
            "    contentScriptDict.clear();\n"
            "    ubolpaUserScripts.reset();\n",
        ),
    ]
    for old, new in edits:
        if source.count(old) != 1:
            first = old.strip().splitlines()[0]
            die(f"vapi-scripting.js: expected exactly one {first!r}; uBO's layout changed")
        source = source.replace(old, new)
    path.write_text(source, encoding="utf-8")


def build_companion(comp: Path, ubo_id: str, ubo_dir: Path, embed_key: str | None) -> None:
    if comp.exists():
        shutil.rmtree(comp)
    (comp / "img").mkdir(parents=True)

    icons = {}
    for size in (16, 32, 64):
        src = ubo_dir / "img" / f"icon_{size}.png"
        if src.exists():
            shutil.copy2(src, comp / "img" / f"icon_{size}.png")
            icons[str(size)] = f"img/icon_{size}.png"

    manifest = {
        "manifest_version": 3,
        "name": "uBlock Origin toolbar button",
        "version": "1.0",
        "description": (
            "Provides the toolbar button for uBlock Origin, which runs as a "
            "legacy packaged app so Chrome's Manifest V2 shutdown does not "
            "disable it."
        ),
        # userScripts and host access let it register uBO's main-world
        # scriptlets so they run before the page's own scripts (edit 8). Only
        # uBO may message it.
        "permissions": ["tabs", "userScripts"],
        "host_permissions": ["<all_urls>"],
        "externally_connectable": {"ids": [ubo_id]},
        "action": {
            "default_popup": "popup.html",
            "default_title": "uBlock Origin",
            "default_icon": icons,
        },
        "background": {"service_worker": "sw.js"},
    }
    if embed_key:
        manifest["key"] = embed_key
    write_json(comp / "manifest.json", manifest)

    (comp / "popup.html").write_text(asset("companion/popup.html"), encoding="utf-8")
    (comp / "popup.js").write_text(
        asset("companion/popup.js", UBO_ID=ubo_id, RESIZE_MSG=RESIZE_MSG), encoding="utf-8"
    )
    (comp / "sw.js").write_text(
        asset("companion/sw.js", UBO_ID=ubo_id), encoding="utf-8"
    )
    ok("Companion built")


# --- check ----------------------------------------------------------------


def check(
    root: Path,
    ubo_id: str,
    comp_id: str,
    profiles: bool = True,
    keys_root: Path | None = None,
) -> int:
    """
    Return a count of problems; 0 means consistent. `profiles` is off when
    verifying a staged build, which has not been delivered to a browser yet.
    """
    ext, comp = root / "extension", root / "companion"
    missing = [
        str(f.relative_to(root))
        for f in (
            ext / "manifest.json",
            ext / "js" / "webext.js",
            ext / "js" / "vapi-background.js",
            ext / "popup-fenix.html",
            comp / "manifest.json",
            comp / "popup.js",
            comp / "sw.js",
        )
        if not f.exists()
    ]
    if len(missing) == 7:
        info("Not installed.")
        return 1
    if missing:
        for name in missing:
            print(f"  {'missing file':<26} {name}")
        print()
        print("PROBLEMS FOUND")
        return len(missing)

    problems = 0

    def row(label: str, value, expected=None) -> None:
        nonlocal problems
        if expected is None:
            print(f"  {label:<26} {value}")
            return
        good = value == expected
        if not good:
            problems += 1
        mark = "OK" if good else f"MISMATCH, expected {expected}"
        print(f"  {label:<26} {value}   {mark}")

    try:
        manifest = read_json(ext / "manifest.json")
        comp_manifest = read_json(comp / "manifest.json")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"  {'manifest unreadable':<26} {exc}")
        print()
        print("PROBLEMS FOUND")
        return 1
    if not isinstance(manifest, dict) or not isinstance(comp_manifest, dict):
        print(f"  {'manifest wrong type':<26} expected a JSON object")
        print()
        print("PROBLEMS FOUND")
        return 1
    popup_js = (comp / "popup.js").read_text(encoding="utf-8")
    sw_js = (comp / "sw.js").read_text(encoding="utf-8")

    print("identity")
    row("uBO id", ubo_id)
    row("companion id", comp_id)
    print("cross-references")
    row("companion popup.js", set(re.findall(r"chrome-extension://([a-p]{32})", popup_js)), {ubo_id})
    row("companion sw.js", set(re.findall(r"'([a-p]{32})'", sw_js)), {ubo_id})
    row("uBO externally_connectable", manifest.get("externally_connectable", {}).get("ids"), [comp_id])
    print("patches")
    launch = (manifest.get("app") or {}).get("launch") or {}
    local_path = str(launch.get("local_path") or "")
    inside = False
    if local_path and not os.path.isabs(local_path):
        target = (ext / local_path).resolve()
        inside = target.is_file() and str(target).startswith(str(ext.resolve()) + os.sep)
    row("app key (legacy packaged app)", inside, True)
    webext = (ext / "js" / "webext.js").read_text(encoding="utf-8")
    row(
        "browserAction shim",
        "ubo-lpa-shim" in webext and "chrome.browserAction = {" in webext,
        True,
    )
    vapi = (ext / "js" / "vapi-background.js").read_text(encoding="utf-8")
    row(
        "badge bridge",
        "uboBadge" in vapi and "onMessageExternal" in vapi,
        True,
    )
    resize = ext / "js" / "popup-resize.js"
    row(
        "resize notifier",
        resize.exists() and "postMessage" in resize.read_text(encoding="utf-8"),
        True,
    )
    scripting = ext / "js" / "vapi-scripting.js"
    row(
        "user scripts bridge",
        scripting.exists()
        and "ubolpaUserScripts.codeFor(" in scripting.read_text(encoding="utf-8"),
        True,
    )
    row("companion userScripts", "userScripts" in comp_manifest.get("permissions", []), True)
    row("popup-fenix web-accessible", "popup-fenix.html" in manifest.get("web_accessible_resources", []), True)
    row("popup-resize tag", "popup-resize.js" in (ext / "popup-fenix.html").read_text(encoding="utf-8"), True)
    row("browser_action kept", "browser_action" in manifest, True)
    # Checked inside the frame-ancestors directive, since listing the companion
    # origin in any other directive would not permit the framing.
    ancestors = ""
    for directive in manifest.get("content_security_policy", "").split(";"):
        parts = directive.split()
        if parts and parts[0].lower() == "frame-ancestors":
            ancestors = " ".join(parts[1:])
    row(
        "frame-ancestors CSP",
        f"chrome-extension://{comp_id}" in ancestors.split(),
        True,
    )

    # Marker strings only prove a patch was attempted. These check the results.
    scanned = [
        ext / "js" / "popup-resize.js",
        ext / "js" / "vapi-scripting.js",
        ext / "manifest.json",
        comp / "popup.js",
        comp / "sw.js",
        comp / "popup.html",
        comp / "manifest.json",
    ]
    unresolved = []
    for f in scanned:
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            unresolved.append(f"{f.name} (not utf-8)")
            continue
        if re.search(r"__[A-Z][A-Z0-9_]*__", text) or "__MSG_" in text:
            unresolved.append(f.name)
    unresolved = sorted(unresolved)
    row("placeholders substituted", unresolved or "yes", "yes")
    row("uBO manifest key", bool(manifest.get("key")), True)
    row("companion manifest key", bool(comp_manifest.get("key")), True)
    # Without these files, the next rebuild generates new IDs and Chrome
    # registers different extensions, so uBO's settings are lost.
    keys = keys_root or root
    for label, pem, want in (
        ("uBO", keys / "ubo.pem", ubo_id),
        ("companion", keys / "comp.pem", comp_id),
    ):
        # Existence is not enough: the next rebuild derives the ID from this
        # key, so a different key would change the ID, and an unreadable one
        # would make the rebuild fail.
        if not pem.is_file():
            row(f"{label} private key", "missing", "matches")
            continue
        try:
            derived = id_from_bytes(_public_der(pem))
        except Exception:
            derived = "unreadable"
        row(f"{label} private key", "matches" if derived == want else derived, "matches")
    for label, mf, want in (("uBO", manifest, ubo_id), ("companion", comp_manifest, comp_id)):
        derived = ""
        if mf.get("key"):
            try:
                derived = id_from_bytes(base64.b64decode(mf["key"]))
            except Exception:
                derived = "unreadable"
        row(f"{label} id matches key", derived, want)
    for name in ("popup.html", "popup.js", "sw.js", "manifest.json"):
        row(f"companion {name}", (comp / name).is_file(), True)
    row("popup origin guard", "e.origin !== UBO_ORIGIN" in popup_js, True)
    row("companion name", comp_manifest.get("name"))

    if profiles and not IS_WINDOWS:
        print("profiles")
        found = False
        for profile in LinuxInstaller(root).profiles():
            found = True
            try:
                prefs = json.loads((profile / "Preferences").read_text())
            except Exception:
                row(f"{profile.name}", "unreadable", "ok")
                continue
            entries = prefs.get("extensions", {}).get("settings", {})
            label = f"{profile.parent.name}/{profile.name}"
            for name, eid, want_dir in (
                ("uBO", ubo_id, ext),
                ("companion", comp_id, comp),
            ):
                row(f"{label} {name}", entry_state(entries.get(eid), want_dir), "enabled")
            dev = prefs.get("extensions", {}).get("ui", {}).get("developer_mode")
            row(f"{label} developer mode", dev, True)
            user_scripts = (entries.get(comp_id) or {}).get("user_scripts_enabled")
            row(f"{label} user scripts", user_scripts is True, True)
        if not found:
            print("  (no Chrome or Chromium profile found)")

    print()
    print("PROBLEMS FOUND" if problems else "all consistent")
    return problems


# --- installers -----------------------------------------------------------


class Installer:
    """Shared build steps. Subclasses supply the platform delivery."""

    def __init__(self, root: Path, ubo_zip: Path | None = None) -> None:
        self.root = root
        self.ext = root / "extension"
        self.comp = root / "companion"
        self.state_file = root / ".state.json"
        # A uBO release zip to install instead of the newest release, as the
        # Windows MSI does with the release it bundles.
        self.ubo_zip = ubo_zip

    # -- state
    def state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:
            die(f"{self.state_file} is unreadable ({exc}); move or remove it before retrying")
        if not isinstance(data, dict):
            die(f"{self.state_file} does not contain an object; move or remove it before retrying")
        return data

    MAGIC = "ubo-lpa-install"

    def owns_root(self) -> bool:
        """True if the state file has the marker this installer writes."""
        try:
            data = json.loads(self.state_file.read_text())
        except Exception:
            return False
        return isinstance(data, dict) and data.get("marker") == self.MAGIC

    def save_state(self, **kw) -> None:
        data = self.state()
        data["marker"] = self.MAGIC
        data.update(kw)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.state_file)

    # -- identity: one mechanism on both platforms. The key is stored beside
    #    the extensions and reused, so ids are stable across reinstalls.
    def ids(self) -> tuple[str, str, str, str]:
        # The marker is written before the keys, so a failed first install
        # still leaves a directory that uninstall recognises as its own. A
        # directory that already contains other files is refused, because the
        # install would overwrite anything in it named extension, companion,
        # ubo.pem and so on.
        if not self.owns_root():
            existing = [p.name for p in self.root.iterdir()] if self.root.is_dir() else []
            if existing:
                die(
                    f"{self.root} is not empty "
                    f"({', '.join(sorted(existing)[:4])}) and does not have "
                    "the ubo-lpa marker. Choose an empty or new directory."
                )
            self.save_state()
        ubo_key, ubo_id = ensure_key(self.root / "ubo.pem")
        comp_key, comp_id = ensure_key(self.root / "comp.pem")
        return ubo_id, comp_id, ubo_key, comp_key

    # -- platform-specific delivery
    def deliver(self, ubo_id: str, comp_id: str, version: str, rebuilt: bool) -> int:
        """
        Add the built extensions to Chrome's profiles (Linux) or say how to
        load them (Windows). Returns the exit status.
        """
        raise NotImplementedError

    def remove(self) -> None:
        raise NotImplementedError

    def timer(self) -> None:
        raise NotImplementedError

    def timer_off(self) -> None:
        raise NotImplementedError

    def timer_active(self) -> bool:
        raise NotImplementedError

    def apply_timer(self, mode: str) -> None:
        """
        "on" schedules the daily update and "off" removes it. "keep" schedules
        it unless it was switched off before. The setup wizard passes "keep"
        when its options page is skipped, as it is after the Update button on
        the maintenance page. The choice is kept in the state file.
        """
        if mode == "keep" and self.state().get("autoupdate") is False:
            info("Daily updates stay off.")
            return
        if mode == "off":
            self.timer_off()
            ok("Daily updates are off.")
        else:
            self.timer()
        if self.owns_root():
            self.save_state(autoupdate=mode != "off")

    # -- shared build
    def install(self, quiet: bool = False) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock():
            return self._install(quiet)

    @staticmethod
    @contextlib.contextmanager
    def lock():
        """
        Serialise every mutating command. The file lives outside the install
        directory, since uninstall deletes that. The name is fixed rather than
        per-directory, because runs with different --dir values still edit the
        same Chrome profiles.
        """
        # Use a per-user path rather than $TMPDIR, which a caller can change to
        # bypass the lock.
        base = Path(
            os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("LOCALAPPDATA")
            or Path.home()
        )
        base.mkdir(parents=True, exist_ok=True)
        path = base / ".ubo-lpa.lock"
        handle = path.open("a+")
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    die("another ubo-lpa run is in progress")
            elif msvcrt is not None:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    die("another ubo-lpa run is in progress")
            yield
        finally:
            handle.close()

    def _install(self, quiet: bool = False) -> int:
        ubo_id, comp_id, ubo_key, comp_key = self.ids()

        if self.ubo_zip is not None:
            tag, from_api = ubo_zip_version(self.ubo_zip), False
        else:
            info("Checking for latest uBO release...")
            tag, from_api = resolve_latest_tag()
            if not tag:
                die("Could not reach any uBO release.")
        installed = self.state().get("version")

        present = self.state_file.exists() and self.ext.is_dir() and self.comp.is_dir()
        # Only the files decide whether to rebuild. A profile without the
        # extensions is fixed by deliver() below, which runs on every install.
        broken = present and check_quiet(self.root, ubo_id, comp_id, profiles=False) > 0
        outdated = present and installed != tag

        if installed and version_tuple(tag) < version_tuple(installed):
            # KNOWN_GOOD is fixed in this file, so when latest_release() finds
            # nothing, the fallback can offer a version older than the installed
            # one, and --ubo-zip can name an older release. That must not cause
            # a downgrade. A broken install is still rebuilt from the version
            # already recorded.
            source = ("GitHub" if from_api
                      else f"{self.ubo_zip.name}" if self.ubo_zip is not None
                      else "The built-in release list")
            info(f"{source} offers {tag}, older than the installed {installed}.")
            tag = installed
            outdated = False
            # A rebuild then downloads the installed release; the zip holds
            # the older one.
            self.ubo_zip = None

        rebuild = not present or broken or outdated
        if present:
            if broken:
                warn("Installed but inconsistent:")
                check(self.root, ubo_id, comp_id, profiles=False)
                print()
                if not quiet and not ask("Repair it (re-download and rebuild)?", True):
                    info("No changes made.")
                    return 0
            elif outdated:
                ok(f"Installed: uBO {installed}; {tag} is available.")
            else:
                ok(f"Installed: uBO {installed} (current)")
                rebuild = not quiet and ask("Installation is already current. Reinstall?", False)
            print()

        # IDs are recorded before any profile is touched, so if delivery fails
        # part way through, uninstall can still find the entries already
        # written.
        self.save_state(ext_id=ubo_id, comp_id=comp_id)
        if rebuild:
            self._rebuild(tag, ubo_id, comp_id, ubo_key, comp_key)
            self.save_state(version=tag)
        return self.deliver(ubo_id, comp_id, tag, rebuild)

    def _rebuild(self, tag: str, ubo_id: str, comp_id: str, ubo_key: str,
                 comp_key: str) -> None:
        # Build and verify the complete staged copy before modifying the working
        # installation. A changed upstream layout or failed patch therefore
        # leaves the existing installation operational.
        staging = self.root / ".staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            download_ubo(tag, staging / "extension", self.ubo_zip)
            patch_ubo(staging / "extension", comp_id, ubo_key)
            build_companion(staging / "companion", ubo_id, staging / "extension", comp_key)
            if check_quiet(staging, ubo_id, comp_id, profiles=False,
                           keys_root=self.root) > 0:
                check(staging, ubo_id, comp_id, profiles=False, keys_root=self.root)
                die("built copy failed verification; existing installation unchanged")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # A running Chrome keeps working through the swap. It reads the
        # extension files from disk as it needs them and the manifest at its
        # next start, so the new version runs fully after a restart.
        try:
            swap_pair([
                (staging / "extension", self.ext),
                (staging / "companion", self.comp),
            ])
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            die(f"could not replace the installed files ({exc}); "
                "the previous installation is unchanged")
        shutil.rmtree(staging, ignore_errors=True)

    OWNED = (
        "extension",
        "companion",
        ".staging",
        ".state.json",
        "ubo.pem",
        "comp.pem",
        ".lock",
        "update.log",
        "load-by-hand.html",
    )

    def remove_install_dir(self) -> None:
        """
        Delete only what this installer creates, and only from a directory it
        recognises as its own. `uninstall --dir` otherwise points rmtree at an
        arbitrary path.
        """
        if not self.root.exists():
            return
        if not self.owns_root():
            die(
                f"{self.root} does not have the ubo-lpa marker; refusing to "
                "delete a directory this installer did not create"
            )
        failed = []
        # Remove the state file last, so a partial failure leaves the directory
        # recognisable and a retry can complete the operation.
        ordered = [n for n in self.OWNED if n != ".state.json"] + [".state.json"]
        for name in ordered:
            target = self.root / name
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except OSError as exc:
                failed.append(f"{name}: {exc.strerror}")
                if name != ".state.json":
                    # Stop before the marker, so the directory remains
                    # recognisable and uninstall can be run again.
                    break
        if failed:
            for line in failed:
                warn(f"could not remove {line}")
            die("uninstall incomplete; resolve the reported errors and run uninstall again")
        leftovers = list(self.root.iterdir())
        if leftovers:
            info(f"Left {len(leftovers)} unrecognised item(s) in {self.root}")
        else:
            self.root.rmdir()


def rename(src: Path, dest: Path) -> None:
    """
    Path.rename, retried for up to ten seconds on Windows. Windows refuses to
    rename a directory while a file in it is open, and a virus scanner checking
    the new files, or Chrome reading one, can hold a file open for a moment.
    """
    deadline = time.monotonic() + (10 if IS_WINDOWS else 0)
    while True:
        try:
            src.rename(dest)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def swap_pair(pairs: list[tuple[Path, Path]]) -> None:
    """
    Replace several directories together. Every old copy is kept until all
    moves are complete, so a failure part way through restores the whole set
    rather than leaving a new extension beside an old companion.
    """
    done: list[tuple[Path, Path | None]] = []
    try:
        for new_dir, dest in pairs:
            backup: Path | None = dest.with_name(dest.name + ".old")
            if backup.exists():
                shutil.rmtree(backup)
            if dest.exists():
                rename(dest, backup)
            else:
                backup = None
            # Record dest before moving the new copy into it, so a failure
            # restores this directory as well as those already moved.
            done.append((dest, backup))
            rename(new_dir, dest)
    except OSError:
        for dest, backup in reversed(done):
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            if backup is not None and backup.exists():
                rename(backup, dest)
        raise
    for _, backup in done:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def check_quiet(root: Path, ubo_id: str, comp_id: str, profiles: bool = True,
                keys_root: Path | None = None) -> int:
    """check() without the output."""
    buf, sys.stdout = sys.stdout, open(os.devnull, "w")
    try:
        return check(root, ubo_id, comp_id, profiles, keys_root)
    finally:
        sys.stdout.close()
        sys.stdout = buf


class LinuxInstaller(Installer):
    """
    Adds both extensions to each profile's Preferences. Chrome keeps that file
    in memory and writes it back, so a profile is only edited while its browser
    is closed. An update keeps the install path and IDs, so the entries already
    in Preferences stay valid and only the files change.
    """

    def profiles(self) -> list[Path]:
        found: list[Path] = []
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        for base in (
            config / "google-chrome",
            config / "google-chrome-beta",
            config / "google-chrome-unstable",
            config / "chromium",
        ):
            if (base / "Default/Preferences").exists():
                found.append(base / "Default")
            if base.is_dir():
                found.extend(
                    p.parent for p in base.glob("Profile */Preferences")
                )
        return sorted(set(found))

    def deliver(self, ubo_id: str, comp_id: str, version: str, rebuilt: bool) -> int:
        targets = self.profiles()
        if not targets:
            die("No Chrome/Chromium profile found.")
        injected, waiting, loaded_by_running = [], [], []
        for profile in targets:
            label = f"{profile.parent.name}/{profile.name}"
            result = self._inject_profile(profile, ubo_id, comp_id)
            if result == "injected":
                injected.append(label)
                ok(f"Injected into {label}")
            elif result == "waiting":
                waiting.append(label)
            elif browser_running(profile.parent):
                loaded_by_running.append(label)
        if rebuilt and (injected or loaded_by_running):
            print()
            ok(f"uBlock Origin {version} installed as a legacy packaged app")
            if loaded_by_running:
                info(f"Chrome is running and loads uBO {version} at its next start.")
            else:
                info("Start Chrome to activate.")
        elif not rebuilt and not injected and not waiting:
            info("No changes made.")
        if waiting:
            warn(
                f"Chrome is running, so uBO was not added to {', '.join(waiting)}. "
                "Close Chrome and run install again."
            )
            return 1
        return 0

    def _inject_profile(self, profile: Path, ubo_id: str, comp_id: str) -> str:
        """
        Add both extensions to one profile. Returns "current" when the profile
        already has correct entries, "waiting" when it needs changes but its
        browser is running, and "injected" after writing the changes.
        """
        prefs_path = profile / "Preferences"
        prefs = json.loads(prefs_path.read_text())
        settings = prefs.setdefault("extensions", {}).setdefault("settings", {})
        changed = False

        # Remove entries from earlier installations by this program. Limit the
        # operation to this installation directory so unrelated unpacked
        # extensions are never modified.
        def under_install_dir(path: str) -> bool:
            # Component-wise containment. A string prefix test would also match
            # a sibling like "<root>-backup/...".
            try:
                return os.path.commonpath([str(self.root), path]) == str(self.root)
            except ValueError:
                return False

        for key in [
            k
            for k, v in settings.items()
            if k not in (ubo_id, comp_id)
            and v.get("location") == 4
            and v.get("path")
            and under_install_dir(str(v["path"]))
        ]:
            del settings[key]
            changed = True

        # Chrome disables unpacked MV3 extensions with
        # DISABLE_UNSUPPORTED_DEVELOPER_EXTENSION (1<<24) unless this is set.
        # uBO is exempt as a legacy packaged app; the companion is not. Without
        # this setting, uBO still blocks requests but the toolbar button does
        # not appear.
        ui = prefs["extensions"].setdefault("ui", {})
        touched_dev_mode = ui.get("developer_mode") is not True
        ui["developer_mode"] = True
        changed |= touched_dev_mode

        # An entry that is already correct stays as it is, including the copy
        # of the manifest in it. Chrome reads an unpacked extension's manifest
        # from disk at every start, and does not disable an unpacked extension
        # whose new version asks for more permissions, so an update does not
        # need to touch the entry.
        for ext_id, ext_dir in ((ubo_id, self.ext), (comp_id, self.comp)):
            if entry_state(settings.get(ext_id), ext_dir) != "enabled":
                settings[ext_id] = self._entry(ext_id, ext_dir)
                changed = True

        stock = settings.get(STOCK_UBO_ID)
        touched_stock = bool(stock) and stock.get("state") == 1
        if touched_stock:
            stock["state"] = 0
            changed = True

        # Chrome gives the companion chrome.userScripts only while its "Allow
        # user scripts" toggle is on, and user_scripts_enabled is that toggle.
        # With the toggle off, uBO injects the scriptlets itself, as an
        # unpatched uBO does, so the pref alone is never a reason to report the
        # profile as waiting for Chrome to close.
        companion = settings[comp_id]
        user_scripts_off = companion.get("user_scripts_enabled") is not True
        companion["user_scripts_enabled"] = True
        running = browser_running(profile.parent)
        if user_scripts_off and not running:
            changed = True

        if not changed:
            return "current"
        if running:
            return "waiting"

        backup = prefs_path.with_suffix(prefs_path.suffix + ".ubo-lpa.bak")
        if not backup.exists():
            shutil.copy2(prefs_path, backup)
        if touched_stock:
            info(f"Disabled stock uBO in {profile.name}")
        write_prefs(prefs_path, prefs)
        # A later install reads the already-modified values and would otherwise
        # record False, losing the record of what the first install changed.
        journal = self.state().get("profiles", {})
        prior = journal.get(str(profile), {})
        journal[str(profile)] = {
            "disabled_stock": bool(prior.get("disabled_stock")) or touched_stock,
            "set_developer_mode": bool(prior.get("set_developer_mode")) or touched_dev_mode,
        }
        self.save_state(profiles=journal)
        return "injected"

    @staticmethod
    def _entry(ext_id: str, ext_dir: Path) -> dict:
        manifest = read_json(ext_dir / "manifest.json")
        perms = manifest.get("permissions", [])
        hosts = [p for p in perms if p.startswith("<") or "://" in p]
        apis = [p for p in perms if p not in hosts]
        # MV3 lists host access separately; MV2 has it in "permissions".
        hosts += manifest.get("host_permissions", [])
        grant = {
            "api": apis,
            "explicit_host": hosts,
            "manifest_permissions": [],
            "scriptable_host": hosts,
        }
        return {
            "active_permissions": grant,
            "granted_permissions": grant,
            "commands": {},
            "content_settings": [],
            "creation_flags": 4,
            "disable_reasons": [],
            "from_webstore": False,
            "incognito_content_settings": [],
            "incognito_preferences": {},
            "incognito": True,
            "location": 4,
            "manifest": manifest,
            "path": str(ext_dir),
            "preferences": {},
            "regular_only_preferences": {},
            "state": 1,
            "was_installed_by_default": False,
            "was_installed_by_oem": False,
        }

    def remove(self) -> None:
        if not self.owns_root():
            die(
                f"{self.root} does not have the ubo-lpa marker; refusing to "
                "touch profiles or files for a directory this installer did "
                "not create"
            )
        state = self.state()
        ids = {state.get("ext_id"), state.get("comp_id")} - {None}
        # Work out every edit first, so a running browser stops the uninstall
        # before any profile or file has changed.
        edits = []
        for profile in self.profiles():
            prefs_path = profile / "Preferences"
            prefs = json.loads(prefs_path.read_text())
            settings = prefs.get("extensions", {}).get("settings", {})
            removed = [settings.pop(i, None) is not None for i in ids]
            changed = any(removed)
            # Reverse the other two profile changes made during installation.
            journal = state.get("profiles", {}).get(str(profile), {})
            if journal.get("disabled_stock"):
                stock = settings.get(STOCK_UBO_ID)
                if stock is not None and stock.get("state") == 0:
                    stock["state"] = 1
                    changed = True
            if journal.get("set_developer_mode"):
                ui = prefs.get("extensions", {}).get("ui", {})
                if ui.get("developer_mode") is True:
                    ui["developer_mode"] = False
                    changed = True
            edits.append((profile, prefs, changed))
        running = sorted({str(p.parent) for p, _, changed in edits
                          if changed and browser_running(p.parent)})
        if running:
            die(f"Chrome is running with {', '.join(running)}. Close it first.")
        for profile, prefs, changed in edits:
            prefs_path = profile / "Preferences"
            if changed:
                write_prefs(prefs_path, prefs)
                ok(f"Cleaned {profile.name}")
            backup = prefs_path.with_suffix(prefs_path.suffix + ".ubo-lpa.bak")
            backup.unlink(missing_ok=True)
        self._remove_timer()
        self.remove_install_dir()
        ok("Uninstalled.")

    UNIT_DIR = Path.home() / ".config/systemd/user"

    def _timer_target(self) -> Path | None:
        """
        The install directory the timer units update, or None if the service
        unit does not exist.
        """
        try:
            text = (self.UNIT_DIR / f"{TIMER_NAME}.service").read_text()
        except OSError:
            return None
        match = re.search(r'--dir "((?:[^"\\]|\\.)*)"', text)
        return (
            Path(match.group(1).replace('\\"', '"').replace("%%", "%"))
            if match else default_root()
        )

    def timer_active(self) -> bool:
        return self._timer_target() == self.root

    def timer_off(self) -> None:
        self._remove_timer()

    def _remove_timer(self) -> None:
        """Remove the timer units, if they update this install directory."""
        target = self._timer_target()
        if target is None:
            return
        if target != self.root:
            info(f"Leaving the timer units in place; they update {target}.")
            return
        # Disable before unlinking, or systemd keeps a dangling symlink in
        # timers.target.wants.
        if (self.UNIT_DIR / f"{TIMER_NAME}.timer").exists():
            result = systemctl("disable", "--now", f"{TIMER_NAME}.timer")
            if result is None:
                info("systemctl not found; deleting the timer units without disabling them")
            elif result.returncode != 0:
                warn(f"could not disable the timer: {result.stderr.strip()}")
        for unit in (f"{TIMER_NAME}.service", f"{TIMER_NAME}.timer"):
            (self.UNIT_DIR / unit).unlink(missing_ok=True)
        systemctl("daemon-reload")

    def load(self, user_data: Path | None = None, profiles: list[str] | None = None) -> int:
        info("On Linux, install adds both extensions to Chrome's profiles itself.")
        return 0

    def timer(self) -> None:
        unit_dir = self.UNIT_DIR
        unit_dir.mkdir(parents=True, exist_ok=True)
        script = Path(__file__).resolve()
        # systemd splits ExecStart on whitespace and treats % specially, so each
        # path is quoted and any % doubled.
        def esc(value: str) -> str:
            return '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"'

        # The install directory is always given, so the unit does not depend
        # on the user manager's environment, and uninstall can tell which
        # install the timer updates.
        argv = [esc(sys.executable), esc(script), "install", "--non-interactive",
                "--dir", esc(self.root)]
        (unit_dir / f"{TIMER_NAME}.service").write_text(
            "[Unit]\nDescription=Update uBlock Origin (legacy packaged app)\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={' '.join(argv)}\n"
        )
        (unit_dir / f"{TIMER_NAME}.timer").write_text(
            "[Unit]\nDescription=Daily uBlock Origin update\n"
            "[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=3600\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        for args in (["daemon-reload"], ["enable", "--now", f"{TIMER_NAME}.timer"]):
            result = systemctl(*args)
            if result is None:
                info("systemctl not found; units written but not enabled.")
                return
            if result.returncode != 0:
                detail = result.stderr.strip() or f"exit {result.returncode}"
                info(f"systemctl {args[0]} failed: {detail}")
                info("Units written but not enabled.")
                return
        ok("Auto-update timer enabled (daily)")


class WindowsInstaller(Installer):
    """
    Stages the files. The load command then loads them into Chrome with Load
    unpacked, over Chrome's DevTools connection, after one click on Allow.
    Every other automated route tested for an off-store extension on Windows
    Chrome is ignored, leaves the extension disabled or needs enterprise
    policy (see WRITEUP.md).
    """

    def deliver(self, ubo_id: str, comp_id: str, version: str, rebuilt: bool) -> int:
        if not rebuilt:
            info("No changes made.")
            return 0
        print()
        ok(f"Staged uBlock Origin {version} in {self.root}")
        # Load unpacked records the folder, and Chrome reads an unpacked
        # extension's manifest from that folder at every start, so an update
        # needs the manual steps only until both extensions are loaded.
        loaded = self.loaded_in(ubo_id, comp_id)
        if loaded:
            info(f"Both extensions are loaded in {', '.join(loaded)}; "
                 f"Chrome loads uBO {version} at its next start.")
            return 0
        print()
        info("To load both into Chrome, run ubo-lpa.py load and click Allow when Chrome asks.")
        warn("Or load them by hand:")
        print()
        print("  1. Open  chrome://extensions")
        print("  2. Turn on  Developer mode  (top right)")
        print(f"  3. Load unpacked  ->  {self.ext}")
        print(f"  4. Load unpacked  ->  {self.comp}")
        print("  5. Details of \"uBlock Origin toolbar button\"  ->  Allow user scripts")
        print()
        info("Both extensions persist across browser restarts. See WRITEUP.md for details.")
        return 0



    def loaded_in(self, ubo_id: str, comp_id: str) -> list[str]:
        """The Chrome profiles that have both extensions loaded from this
        directory, as "<browser>/<profile folder>"."""
        found = []
        for user_data in chrome_user_data_dirs():
            for profile in chrome_profiles(user_data):
                if self.has_both(user_data / profile["dir"], ubo_id, comp_id):
                    found.append(f"{user_data.parent.name}/{profile['dir']}")
        return found

    def has_both(self, profile: Path, ubo_id: str, comp_id: str) -> bool:
        """True if this profile has both extensions loaded from this directory.
        Chrome keeps extension entries in Secure Preferences on Windows;
        Preferences is read as well."""
        settings = profile_extension_settings(profile)
        return all(
            same_path(str((settings.get(eid) or {}).get("path", "")), path)
            for eid, path in ((ubo_id, self.ext), (comp_id, self.comp))
        )

    @property
    def task_name(self) -> str:
        """
        The scheduled task's name. Each install directory gets its own task, so
        uninstall removes exactly the task that updates it. The default
        directory's task is plain TIMER_NAME.
        """
        if same_path(str(self.root), default_root()):
            return TIMER_NAME
        digest = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
        return f"{TIMER_NAME}-{digest[:8]}"

    def remove(self) -> None:
        # The task goes first, so an uninstall that stops part way does not
        # leave a task that installs everything again.
        result = run_quiet(["schtasks", "/Delete", "/TN", self.task_name, "/F"])
        if result is not None and result.returncode == 0:
            ok(f"Removed the scheduled task {self.task_name}.")
        self.remove_install_dir()
        ok("Removed staged files.")
        info("Remove both entries from chrome://extensions as well; this script\n   cannot unload them.")

    def timer_active(self) -> bool:
        result = run_quiet(["schtasks", "/Query", "/TN", self.task_name])
        return result is not None and result.returncode == 0

    def timer_off(self) -> None:
        if self.timer_active():
            result = run_quiet(["schtasks", "/Delete", "/TN", self.task_name, "/F"])
            if result is None or result.returncode != 0:
                detail = result and ((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
                die(f"schtasks /Delete failed: {detail}")
            ok(f"Removed the scheduled task {self.task_name}.")

    def timer(self) -> None:
        # pythonw.exe runs without a console window; main() then appends the
        # output to update.log in the install directory.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = pythonw if pythonw.is_file() else Path(sys.executable)
        arguments = subprocess.list2cmdline([
            str(Path(__file__).resolve()), "install", "--non-interactive",
            "--dir", str(self.root),
        ])
        task = TASK_XML.format(
            description=escape(f"Update uBlock Origin (legacy packaged app) in {self.root}"),
            command=escape(str(command)),
            arguments=escape(arguments),
        )
        with tempfile.TemporaryDirectory() as temporary:
            definition = Path(temporary) / "task.xml"
            definition.write_text(task, encoding="utf-16")
            result = run_quiet(["schtasks", "/Create", "/TN", self.task_name,
                                "/XML", str(definition), "/F"])
        if result is None:
            die("schtasks.exe not found; the scheduled task was not created")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            die(f"schtasks /Create failed: {detail}")
        ok(f"Scheduled task {self.task_name} created (daily)")

    def load(self, user_data: Path | None = None, profiles: list[str] | None = None) -> int:
        """
        Load both folders into Chrome over its DevTools connection: into the
        given profile folders or, when none are given, into the profile Chrome
        opens. When that fails, print the reason and open a page with the
        manual steps.
        """
        try:
            return self._load(user_data, profiles)
        except SystemExit as exc:
            if exc.code and user_data is None:
                guide = self.root / "load-by-hand.html"
                guide.write_text(GUIDE_HTML.replace("__EXT__", html.escape(str(self.ext)))
                                 .replace("__COMP__", html.escape(str(self.comp))),
                                 encoding="utf-8")
                info(f"The steps for loading both folders by hand: {guide}")
                try:
                    os.startfile(str(guide))  # type: ignore[attr-defined]
                except OSError as error:
                    warn(f"Could not open it ({error}).")
            raise

    def _load(self, user_data: Path | None, profiles: list[str] | None) -> int:
        state = self.state()
        ubo_id, comp_id = state.get("ext_id"), state.get("comp_id")
        if not ubo_id or not comp_id or not self.ext.is_dir() or not self.comp.is_dir():
            die("Not installed; run install first.")
        explicit = user_data is not None
        user_data = user_data or chrome_user_data()
        if profiles:
            done = [p for p in profiles if self.has_both(user_data / p, ubo_id, comp_id)]
            if done:
                ok(f"Already loaded in {', '.join(done)}.")
            pending: list[str | None] = [p for p in profiles if p not in done]
            if not pending:
                return 0
        else:
            if not explicit and self.loaded_in(ubo_id, comp_id):
                ok("Both extensions are already loaded in Chrome.")
                return 0
            pending = [None]
        chrome = chrome_exe()
        connection = devtools_port(user_data)
        switched_on = False
        if connection is None:
            if chrome is None:
                die("Google Chrome was not found.")
            flags = [f"--user-data-dir={user_data}"] if explicit else []
            if chrome_holds(user_data):
                # Chrome reads the remote debugging setting only when it starts,
                # writes Local State back when it exits, and ignores chrome://
                # addresses on its command line, so a running Chrome has to
                # restart once.
                if not confirm_restart():
                    die("Chrome was not restarted, so uBlock Origin was not loaded into it.")
                close_chrome(user_data)
                flags.append("--restore-last-session")
            set_remote_debugging_pref(user_data, True)
            launch_chrome(chrome, flags)
            switched_on = True
            deadline = time.monotonic() + 300
            while (connection := devtools_port(user_data)) is None:
                if time.monotonic() > deadline:
                    die("Chrome did not turn on remote debugging within five minutes.")
                time.sleep(1)
        info("Chrome asks whether to allow remote debugging: click Allow.")
        try:
            devtools = DevTools(*connection, consent_timeout=300)
        except (DevToolsRefused, OSError) as exc:
            if switched_on:
                warn("Remote debugging stays on in Chrome; it can be turned off at "
                     "chrome://inspect/#remote-debugging.")
            die(f"Chrome did not allow the connection ({exc}).")
        try:
            for profile in pending:
                if profile is None:
                    target, session = devtools.page("chrome://extensions/")
                else:
                    if chrome is None:
                        die("Google Chrome was not found.")
                    info(f"Opening Chrome's profile {profile}")
                    target, session = profile_page(
                        devtools, chrome, profile, user_data if explicit else None)
                self._load_with(devtools, session, ubo_id, comp_id)
                if profile is not None:
                    # The tab is alone in its window, so closing it closes
                    # the window.
                    devtools.call("Target.closeTarget", {"targetId": target})
        finally:
            if switched_on:
                switch_off_remote_debugging(devtools, user_data)
        return 0

    def _load_with(self, devtools: DevTools, session: str, ubo_id: str, comp_id: str) -> None:
        for _ in range(150):
            try:
                if devtools.evaluate(session, "typeof chrome.developerPrivate") == "object":
                    break
            except RuntimeError:
                pass
            time.sleep(0.2)
        # The companion is an unpacked MV3 extension, which Chrome only runs
        # with developer mode on.
        devtools.evaluate(session, "chrome.developerPrivate.updateProfileConfiguration("
                                   "{inDeveloperMode: true})")

        def entry(ext_id: str) -> dict:
            return next((e for e in devtools.evaluate(session, EXTENSIONS_INFO)
                         if e["id"] == ext_id), {})

        for ext_id, folder, label in ((ubo_id, self.ext, "uBlock Origin"),
                                      (comp_id, self.comp, "the toolbar button")):
            current = entry(ext_id)
            if current.get("location") != "UNPACKED":
                info(f"Loading {label} from {folder}")
                picker: dict = {}
                filler = threading.Thread(target=fill_folder_picker, args=(folder, picker),
                                          daemon=True)
                filler.start()
                error = devtools.evaluate(session, (
                    "chrome.developerPrivate.loadUnpacked({failQuietly: false, populateError: true})"
                    ".then(e => e ? (e.error || JSON.stringify(e)) : null, e => e.message)"))
                filler.join(5)
                if picker.get("error") or error:
                    die(f"Chrome did not load {folder}: {picker.get('error') or error}")
                current = entry(ext_id)
            if current.get("location") != "UNPACKED":
                die(f"Chrome did not load {folder}")
            ok(f"{label} is loaded in Chrome")
        # userScriptsAccess is the companion's "Allow user scripts" toggle,
        # which has to be on for edit 8 to take effect.
        devtools.evaluate(session, "chrome.developerPrivate.updateExtensionConfiguration("
                                   f"{{extensionId: '{comp_id}', pinnedToToolbar: true, "
                                   "userScriptsAccess: true})")

        def enabled(ext_id: str, seconds: float) -> dict:
            # Chrome lists an extension as disabled, with the reason
            # "reloading", while it loads it again, so wait for it to settle.
            deadline = time.monotonic() + seconds
            while True:
                current = entry(ext_id)
                if current.get("state") == "ENABLED" or time.monotonic() > deadline:
                    return current
                time.sleep(0.5)

        for ext_id in (ubo_id, comp_id):
            current = enabled(ext_id, 30)
            if current.get("state") != "ENABLED":
                devtools.evaluate(session, f"chrome.management.setEnabled('{ext_id}', true)")
                current = enabled(ext_id, 15)
            if current.get("state") != "ENABLED":
                die(f"Chrome lists {ext_id} as {current.get('state')} "
                    f"(disable reasons: {', '.join(current.get('reasons') or ['none'])})")
        ok("uBlock Origin is running in Chrome, with its button pinned to the toolbar.")


# Daily at midnight local time with up to an hour's random delay, like the
# Linux timer. StartWhenAvailable runs a missed update once the user is next
# logged on. InteractiveToken runs the task as the user who created it, and
# only while they are logged on, so Task Scheduler does not store a password.
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <RandomDelay>PT1H</RandomDelay>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def chrome_user_data_dirs() -> list[Path]:
    """Chrome's and Chromium's user data directories on Windows."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    return [
        chrome_user_data(),
        local / "Google/Chrome Beta/User Data",
        local / "Google/Chrome Dev/User Data",
        local / "Google/Chrome SxS/User Data",
        local / "Chromium/User Data",
    ]


def chrome_user_data() -> Path:
    """
    Google Chrome's user data directory, which holds its profiles. It is in
    the user's own folders whether Chrome is installed for this user or for
    all users; only the UserDataDir policy moves it.
    """
    if not IS_WINDOWS:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return config / "google-chrome"
    import winreg
    # A machine policy takes precedence over a user policy, as in Chrome.
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\Google\Chrome") as key:
                value = winreg.QueryValueEx(key, "UserDataDir")[0]
        except OSError:
            continue
        if value:
            return Path(expand_policy_path(str(value)))
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    return local / "Google/Chrome/User Data"


def expand_policy_path(value: str) -> str:
    """Expand the ${...} variables that Chrome's path policies accept, and
    %VARIABLES%."""
    env = os.environ
    for name, replacement in {
        "${local_app_data}": env.get("LOCALAPPDATA", ""),
        "${roaming_app_data}": env.get("APPDATA", ""),
        "${profile}": env.get("USERPROFILE", ""),
        "${documents}": str(Path(env.get("USERPROFILE", "")) / "Documents"),
        "${user_name}": env.get("USERNAME", ""),
        "${machine_name}": env.get("COMPUTERNAME", ""),
        "${program_files}": env.get("PROGRAMFILES", ""),
        "${windows}": env.get("WINDIR", ""),
    }.items():
        value = value.replace(name, replacement)
    return os.path.expandvars(value)


def chrome_profiles(user_data: Path) -> list[dict]:
    """
    The profiles in a user data directory, in the profile picker's order:
    each with its folder, the name Chrome shows for it and its account.
    """
    try:
        local_state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local_state = {}
    profile = local_state.get("profile", {}) if isinstance(local_state, dict) else {}
    cache = profile.get("info_cache") or {}
    order = [d for d in (profile.get("profiles_order") or []) if d in cache]

    def number(folder: str) -> tuple:
        tail = folder.rsplit(" ", 1)[-1]
        return (folder != "Default", int(tail) if tail.isdigit() else 0, folder)

    folders = order + sorted((d for d in cache if d not in order), key=number)
    if not folders and (user_data / "Default").is_dir():
        folders = ["Default"]
    return [
        {"dir": d, "name": (cache.get(d) or {}).get("name") or d,
         "account": (cache.get(d) or {}).get("user_name") or ""}
        for d in folders if (user_data / d).is_dir()
    ]


def profile_extension_settings(profile: Path) -> dict:
    """A profile's extension entries, from Preferences and Secure Preferences."""
    settings: dict = {}
    for name in ("Preferences", "Secure Preferences"):
        try:
            prefs = json.loads((profile / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        settings.update(prefs.get("extensions", {}).get("settings", {}))
    return settings


def profile_page(devtools: DevTools, chrome: str, profile: str,
                 user_data: Path | None = None) -> tuple[str, str]:
    """
    A chrome://extensions tab in one profile of the running Chrome; returns
    its target ID and an attached session. Target.createTarget opens tabs in
    the profile Chrome started with, so this starts chrome.exe again with
    --profile-directory: the running Chrome takes that command over and opens
    a window in the profile. That window opens at the New Tab page whatever
    address the command names, so its tab is found as the one page target
    that was not there before, and then navigated to chrome://extensions.
    """
    def pages() -> list[dict]:
        return [t for t in devtools.call("Target.getTargets").get("targetInfos", [])
                if t.get("type") == "page"]

    before = {t["targetId"] for t in pages()}
    flags = [f"--profile-directory={profile}"]
    if user_data is not None:
        flags.insert(0, f"--user-data-dir={user_data}")
    launch_chrome(chrome, flags)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        new = [t for t in pages() if t["targetId"] not in before]
        if new:
            target = new[0]["targetId"]
            session = devtools.call("Target.attachToTarget", {
                "targetId": target, "flatten": True})["sessionId"]
            devtools.call("Page.navigate", {"url": "chrome://extensions/"}, session)
            return target, session
        time.sleep(0.5)
    die(f"Chrome did not open a window for its profile {profile}.")


def chrome_info() -> dict:
    """The Chrome that the load command uses: its program, version, user data
    directory, and whether it is installed for this user or for all users."""
    exe = chrome_exe()
    if exe is None:
        return {"found": False, "user_data": str(chrome_user_data())}
    scope = "system"
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        if local and os.path.normcase(exe).startswith(os.path.normcase(local)):
            scope = "user"
    return {"found": True, "exe": exe, "version": chrome_version(exe), "scope": scope,
            "user_data": str(chrome_user_data())}


def chrome_version(exe: str) -> str | None:
    if IS_WINDOWS:
        # chrome.exe sits beside a folder named after its version, which holds
        # the rest of Chrome. After an update, the old and the new version's
        # folders are both there until Chrome restarts, so the highest version
        # is taken.
        try:
            names = [d.name for d in Path(exe).parent.iterdir() if d.is_dir()]
        except OSError:
            return None
        versions = [n for n in names if re.fullmatch(r"\d+(\.\d+){3}", n)]
        return max(versions, key=version_tuple) if versions else None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+(\.\d+){3}", out)
    return match.group(0) if match else None


def setup_info(installer: Installer) -> dict:
    """What the Windows setup wizard shows: the Chrome that the load command
    uses and its profiles, the uBO releases it can install, and this install's
    state."""
    chrome = chrome_info()
    state = installer.state() if installer.owns_root() else {}
    ubo_id, comp_id = state.get("ext_id"), state.get("comp_id")
    profiles = []
    for profile in chrome_profiles(Path(chrome["user_data"])):
        loaded = bool(ubo_id and comp_id and isinstance(installer, WindowsInstaller)
                      and installer.has_both(Path(chrome["user_data"]) / profile["dir"],
                                             ubo_id, comp_id))
        profiles.append({**profile, "loaded": loaded})
    latest = latest_release()
    bundled = ubo_zip_version(installer.ubo_zip) if installer.ubo_zip else None
    return {
        "chrome": chrome,
        "profiles": profiles,
        "ubo": {
            "latest": latest, "latest_url": release_url(latest) if latest else None,
            "bundled": bundled, "bundled_url": release_url(bundled) if bundled else None,
        },
        "install": {"dir": str(installer.root), "version": state.get("version"),
                    "autoupdate": installer.timer_active()},
    }


def same_path(a: str, b: Path) -> bool:
    return bool(a) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(str(b)))


def run_quiet(argv: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command and capture its output, or return None if it is missing."""
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return None


def systemctl(*args: str) -> subprocess.CompletedProcess | None:
    """Run systemctl --user, or return None when it is not available."""
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True
        )
    except FileNotFoundError:
        return None


def browser_running(user_data_dir: Path) -> bool:
    """
    True if a Chrome or Chromium process has this user data directory open.
    While it runs, Chrome keeps SingletonLock in the directory as a symlink to
    "<hostname>-<pid>". A lock whose process has exited is left over from a
    crash, and Chrome itself takes it over at its next start.
    """
    try:
        target = os.readlink(user_data_dir / "SingletonLock")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    host, _, pid = target.rpartition("-")
    if host != socket.gethostname() or not pid.isdigit():
        # Held from another machine sharing this home directory, or a lock
        # this code cannot read: treat the profile as open.
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def entry_state(entry: dict | None, ext_dir: Path) -> str:
    """The state of a profile's Preferences entry for one of the extensions:
    "enabled", or what is wrong with it."""
    if entry is None:
        return "absent"
    if entry.get("location") != 4:
        return f"location={entry.get('location')}"
    if entry.get("disable_reasons"):
        return "disabled"
    if entry.get("state") not in (1, None):
        return f"state={entry.get('state')}"
    if Path(str(entry.get("path", ""))) != ext_dir:
        return "wrong path"
    return "enabled"


# --- loading into Chrome (Windows) -----------------------------------------
#
# Chrome on Windows accepts an off-store extension only through Load unpacked
# (WRITEUP.md, "Windows"). The load command performs the manual steps over
# Chrome's DevTools connection, so the only step left to the user is a click on
# Allow in Chrome's prompt. Extensions.loadUnpacked over DevTools would skip the
# folder picker, but Chrome forgets extensions loaded that way at its next
# start, so the command calls the extensions page's own Load unpacked and fills
# in the folder picker it opens.


class DevToolsRefused(Exception):
    pass


class DevTools:
    """The parts of a DevTools WebSocket client that the load command uses."""

    def __init__(self, port: int, path: str, consent_timeout: float) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        # Chrome answers the upgrade once the user has decided in its "Allow
        # remote debugging?" prompt, with 101 for Allow and 403 for Cancel.
        self.sock.settimeout(consent_timeout)
        self.buffer = b""
        status = self._until(b"\r\n\r\n").split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in status:
            raise DevToolsRefused(status)
        self.sock.settimeout(120)
        self.next_id = 0

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise DevToolsRefused("Chrome closed the connection")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def _exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise OSError("Chrome closed the DevTools connection")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def _send(self, text: str) -> None:
        payload, mask = text.encode(), os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 1 << 16:
            header = bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x81, 0x80 | 127]) + length.to_bytes(8, "big")
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _receive(self) -> dict:
        chunks = []
        while True:
            first, second = self._exact(2)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._exact(8), "big")
            payload = self._exact(length)
            opcode = first & 0x0F
            if opcode == 0x9:  # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            elif opcode == 0x8:
                raise OSError("Chrome closed the DevTools connection")
            else:
                chunks.append(payload)
                if first & 0x80:
                    return json.loads(b"".join(chunks))

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self.next_id += 1
        message: dict = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._send(json.dumps(message))
        while True:
            reply = self._receive()
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result", {})

    def evaluate(self, session: str, expression: str):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
            "userGesture": True,
        }, session)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise RuntimeError((detail.get("exception") or {}).get("description") or detail.get("text"))
        return result.get("result", {}).get("value")

    def page(self, url: str) -> tuple[str, str]:
        """Open url in a new tab; return its target ID and an attached session."""
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        return target, session


EXTENSIONS_INFO = """chrome.developerPrivate.getExtensionsInfo({includeDisabled: true})
  .then(list => list.map(e => ({id: e.id, state: e.state, location: e.location,
                                path: e.prettifiedPath || "", pinned: e.pinnedToToolbar,
                                reasons: Object.keys(e.disableReasons || {})
                                               .filter(k => e.disableReasons[k])})))"""


def confirm_restart() -> bool:
    """Ask before restarting Chrome: in the console, or in a message box when
    there is none, as when Setup runs this."""
    text = ("To load uBlock Origin, Chrome restarts once and reopens its tabs. "
            "Chrome then asks whether to allow remote debugging: click Allow.")
    if sys.stdin is not None and sys.stdin.isatty():
        return ask(text + " Restart Chrome now?", True)
    import ctypes
    MB_OKCANCEL, MB_ICONINFORMATION, MB_TOPMOST, MB_SETFOREGROUND, IDOK = 1, 0x40, 0x40000, 0x10000, 1
    return ctypes.windll.user32.MessageBoxW(
        None, text, "uBlock Origin for Chrome",
        MB_OKCANCEL | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND) == IDOK


def close_chrome(user_data: Path) -> None:
    """Close Chrome as its window's close button does, so it saves its session;
    force it only if it keeps running, as it does with background apps."""
    subprocess.run(["taskkill", "/im", "chrome.exe"], capture_output=True)
    for force in (False, True):
        deadline = time.monotonic() + (15 if force else 30)
        while time.monotonic() < deadline:
            if not chrome_holds(user_data):
                return
            time.sleep(0.5)
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
    die("Chrome did not close.")


def launch_chrome(chrome: str, flags: list[str]) -> None:
    """Start Chrome so that it outlives this process."""
    if not IS_WINDOWS:
        subprocess.Popen([chrome, *flags], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP
    for breakaway in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB, if the job allows it
        try:
            subprocess.Popen([chrome, *flags], creationflags=detached | breakaway,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return
        except OSError:
            if not breakaway:
                raise


def switch_off_remote_debugging(devtools: DevTools, user_data: Path) -> None:
    """
    Untick "Allow remote debugging for this browser instance" on
    chrome://inspect, as the user would. The page shows the checkbox's state
    only after Chrome has sent it, and a click before that is lost, so this
    waits until the box shows as ticked. Chrome then stops listening and
    closes this connection, usually before it answers the evaluate call that
    clicked the box, so a Target.closeTarget sent after it does not arrive
    either. The page closes itself instead: a tab opened at an address has
    one history entry, so its own script may close it.
    """
    try:
        target, session = devtools.page("chrome://inspect/#remote-debugging")
        state = "missing"
        for _ in range(150):
            state = devtools.evaluate(session, """(() => {
                const box = document.getElementById("remote-debugging-enabled");
                return !box ? "missing" : box.disabled ? "disabled" : String(box.checked);
            })()""")
            if state == "true":
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"the switch on chrome://inspect stayed {state}")
        devtools.evaluate(session, 'document.getElementById("remote-debugging-enabled").click(); '
                                   'setTimeout(() => window.close(), 500); true')
        devtools.call("Target.closeTarget", {"targetId": target})
    except OSError:
        pass
    except RuntimeError as exc:
        warn(f"Could not turn remote debugging off ({exc}); it can be turned off at "
             "chrome://inspect/#remote-debugging.")
        return
    deadline = time.monotonic() + 15
    while devtools_port(user_data) is not None:
        if time.monotonic() > deadline:
            warn("Remote debugging is still on; it can be turned off at "
                 "chrome://inspect/#remote-debugging.")
            return
        time.sleep(0.5)
    ok("Turned remote debugging off again.")


GUIDE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Load uBlock Origin in Chrome</title>
<style>
  body { margin: 0; font: 15px/1.5 "Segoe UI", system-ui, sans-serif; color: #1f2328; background: #f6f8fa; }
  header { background: linear-gradient(135deg, #a3241a, #5e0f0a); color: #fff; padding: 26px 40px; }
  header h1 { margin: 0; font-size: 23px; font-weight: 600; }
  header p { margin: 6px 0 0; opacity: .9; }
  main { max-width: 760px; margin: 26px auto; padding: 0 24px; }
  ol { list-style: none; counter-reset: s; padding: 0; margin: 0; }
  li { counter-increment: s; position: relative; background: #fff; border: 1px solid #d8dee4;
       border-radius: 10px; padding: 15px 18px 15px 62px; margin-bottom: 11px; }
  li::before { content: counter(s); position: absolute; left: 18px; top: 14px; width: 30px; height: 30px;
               border-radius: 50%; background: #8c1c13; color: #fff; font-weight: 600;
               text-align: center; line-height: 30px; }
  .copy { display: flex; gap: 8px; margin-top: 8px; }
  code { flex: 1; font: 13px/1.4 Consolas, monospace; background: #f6f8fa; border: 1px solid #d8dee4;
         border-radius: 6px; padding: 7px 10px; overflow-wrap: anywhere; }
  button { font: inherit; font-size: 13px; border: 1px solid #8c1c13; color: #8c1c13; background: #fff;
           border-radius: 6px; padding: 0 14px; cursor: pointer; }
</style></head>
<body>
<header><h1>Load uBlock Origin in Chrome</h1>
<p>Setup could not load uBlock Origin into Chrome. Load it by hand with these steps; they are
   needed only once. The update log (update.log, in the same folder as this page) says what
   went wrong.</p></header>
<main><ol>
  <li>Open the extensions page: paste this into Chrome's address bar.
    <div class="copy"><code>chrome://extensions</code><button>Copy</button></div></li>
  <li>Turn on <b>Developer mode</b>, top right.</li>
  <li>Click <b>Load unpacked</b> and select this folder.
    <div class="copy"><code>__EXT__</code><button>Copy</button></div></li>
  <li>Click <b>Load unpacked</b> again and select the toolbar button's folder.
    <div class="copy"><code>__COMP__</code><button>Copy</button></div></li>
  <li>Click <b>Details</b> on <b>uBlock Origin toolbar button</b> and turn on <b>Allow user scripts</b>.</li>
  <li>Pin <b>uBlock Origin toolbar button</b> from the puzzle-piece menu next to the address bar.</li>
</ol></main>
<script>
for (const b of document.querySelectorAll(".copy button"))
  b.onclick = () => navigator.clipboard.writeText(b.previousElementSibling.textContent)
    .then(() => { b.textContent = "Copied"; setTimeout(() => b.textContent = "Copy", 1500); });
</script>
</body></html>
"""


def chrome_exe() -> str | None:
    """
    Google Chrome's chrome.exe, from App Paths or its usual folders. A Chrome
    installed for this user registers under HKEY_CURRENT_USER and one for all
    users under HKEY_LOCAL_MACHINE.
    """
    if not IS_WINDOWS:
        for name in ("google-chrome", "google-chrome-stable"):
            found = shutil.which(name)
            if found:
                return found
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                value = winreg.QueryValue(key, None)
            if value and Path(value).is_file():
                return value
        except OSError:
            pass
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        candidate = Path(os.environ.get(base, "")) / "Google/Chrome/Application/chrome.exe"
        if os.environ.get(base) and candidate.is_file():
            return str(candidate)
    return None


def chrome_holds(user_data: Path) -> bool:
    """True if a Chrome process has this user data directory open: it holds
    the directory's lockfile open without sharing it."""
    try:
        with (user_data / "lockfile").open("a"):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True


def devtools_port(user_data: Path) -> tuple[int, str] | None:
    """The port and browser path from DevToolsActivePort, if Chrome listens there."""
    try:
        lines = (user_data / "DevToolsActivePort").read_text().split()
        port = int(lines[0])
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return port, lines[1]
    except (OSError, ValueError, IndexError):
        return None


def set_remote_debugging_pref(user_data: Path, enabled: bool) -> None:
    """Set Chrome's "Allow remote debugging for this browser instance" setting
    in Local State. Call it only while Chrome is not running: a running Chrome
    writes its own copy back when it exits."""
    path = user_data / "Local State"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    user_data.mkdir(parents=True, exist_ok=True)
    data.setdefault("devtools", {}).setdefault("remote_debugging", {})["user-enabled"] = enabled
    path.write_text(json.dumps(data), encoding="utf-8")


def fill_folder_picker(folder: Path, result: dict, timeout: float = 60) -> None:
    """
    Wait for Chrome's folder picker, type folder into it and accept it. The
    picker is a Win32 common dialog: its file name box is control 0x480 in
    Wine's dialog and an Edit inside the ComboBoxEx32 0x47C in Windows' own.
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    WM_SETTEXT, WM_COMMAND, IDOK = 0x000C, 0x0111, 1

    def from_chrome(hwnd) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        name = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        found = kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size))
        kernel32.CloseHandle(process)
        return bool(found) and name.value.lower().endswith("chrome.exe")

    def picker() -> tuple | None:
        found = []

        @callback
        def visit(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetDlgItem(hwnd, IDOK):
                edit = user32.GetDlgItem(hwnd, 0x480)
                if not edit:
                    combo = user32.GetDlgItem(hwnd, 0x47C)
                    inner = user32.FindWindowExW(combo, None, "ComboBox", None) if combo else None
                    edit = user32.FindWindowExW(inner, None, "Edit", None) if inner else None
                if edit and from_chrome(hwnd):
                    found.append((hwnd, edit))
            return True

        user32.EnumWindows(visit, 0)
        return found[0] if found else None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialog = picker()
        if dialog:
            hwnd, edit = dialog
            user32.SendMessageW(edit, WM_SETTEXT, 0, str(folder))
            user32.PostMessageW(hwnd, WM_COMMAND, IDOK, 0)
            # A typed folder can make the dialog open it instead of returning
            # it; accepting again then returns the folder now shown.
            for _ in range(40):
                time.sleep(0.25)
                if not user32.IsWindow(hwnd):
                    result["ok"] = True
                    return
            user32.PostMessageW(hwnd, WM_COMMAND, IDOK, 0)
            result["ok"] = True
            return
        time.sleep(0.2)
    result["error"] = "Chrome's folder dialog did not appear"


def default_root() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "uBOLPA"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "ublock-origin-lpa"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ubo-lpa", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="install",
        choices=["install", "update", "check", "status", "uninstall", "timer", "load",
                 "setup-info"],
    )
    parser.add_argument(
        "--non-interactive",
        "--quiet",
        "-q",
        dest="non_interactive",
        action="store_true",
        help="never prompt; take the default answer (used by the timer)",
    )
    parser.add_argument("--dir", type=Path, default=None, help="install directory")
    parser.add_argument("--ubo-zip", type=Path, default=None,
                        help="install: this uBO release zip instead of the newest release")
    parser.add_argument("--profiles", default=None,
                        help='load: the Chrome profile folders to load into, separated by "|"')
    timer_mode = parser.add_mutually_exclusive_group()
    timer_mode.add_argument("--off", action="store_true",
                            help="timer: remove the daily update")
    timer_mode.add_argument("--keep", action="store_true",
                            help="timer: schedule the daily update unless it was switched off")
    # Tests use this to run load against a Chrome started with its own user
    # data directory.
    parser.add_argument("--user-data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # Resolved because the path is written verbatim into Chrome's Preferences,
    # where a relative one would depend on the browser's working directory.
    root = (args.dir.expanduser().resolve() if args.dir else default_root())
    ubo_zip = args.ubo_zip.expanduser().resolve() if args.ubo_zip else None
    installer: Installer = (WindowsInstaller if IS_WINDOWS else LinuxInstaller)(root, ubo_zip)

    if args.command == "setup-info":
        print(json.dumps(setup_info(installer), ensure_ascii=True))
        return 0

    if sys.stdout is None:
        # The Windows scheduled task and the MSI start this script with
        # pythonw.exe, which has no console, so the output is appended to a
        # log in the install directory. uninstall deletes that directory, and
        # Windows cannot delete a file this process holds open, so uninstall
        # logs to the temporary directory.
        if args.command == "uninstall":
            log_path = Path(tempfile.gettempdir()) / "ubo-lpa-uninstall.log"
        else:
            root.mkdir(parents=True, exist_ok=True)
            log_path = root / "update.log"
        log = log_path.open("a", encoding="utf-8")
        sys.stdout = sys.stderr = log
        print(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(sys.argv[1:])}")

    if args.command in ("install", "update"):
        return installer.install(quiet=args.non_interactive)
    if args.command == "uninstall":
        with Installer.lock():
            installer.remove()
        return 0
    if args.command == "timer":
        with Installer.lock():
            installer.apply_timer("off" if args.off else "keep" if args.keep else "on")
        return 0
    if args.command == "load":
        profiles = [p for p in args.profiles.split("|") if p] if args.profiles else None
        return installer.load(args.user_data_dir, profiles)

    state = installer.state()
    if not state.get("ext_id") or not state.get("comp_id"):
        info("Not installed.")
        return 1
    if args.command == "status":
        for label, path in (("extension", installer.ext), ("companion", installer.comp)):
            if not path.is_dir():
                warn(f"{label} directory missing: {path}")
    if args.command == "check":
        return 1 if check(root, state["ext_id"], state["comp_id"]) else 0

    print(f"  version:   {state.get('version', '?')}")
    print(f"  uBO ID:    {state.get('ext_id')}")
    print(f"  companion: {state.get('comp_id')}")
    print(f"  directory: {root}")
    if not IS_WINDOWS:
        for profile in LinuxInstaller(root).profiles():
            try:
                entries = json.loads((profile / "Preferences").read_text())
                entries = entries.get("extensions", {}).get("settings", {})
            except Exception:
                continue
            present = [
                n for n, i in (("uBO", state.get("ext_id")), ("companion", state.get("comp_id")))
                if i in entries
            ]
            label = f"{profile.parent.name}/{profile.name}"
            print(f"  profile:   {label}: {', '.join(present) if present else 'not installed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
