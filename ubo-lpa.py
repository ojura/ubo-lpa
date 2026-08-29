#!/usr/bin/env python3
"""
ubo-lpa: run uBlock Origin in Chrome 151 and 152 as a legacy packaged app.

One implementation for Linux and Windows. The patching, companion build and
consistency checks are identical on both. Only the delivery to Chrome differs,
in the two Installer subclasses at the bottom of this file.

    ubo-lpa.py install     download, patch, build, and perform the supported
                           platform-specific installation steps
    ubo-lpa.py update      alias for install; makes no changes to a current,
                           consistent installation
    ubo-lpa.py check       verify required files, IDs, cross-references and
                           patch sentinels
    ubo-lpa.py status      recorded version, IDs and where they are installed
    ubo-lpa.py uninstall   remove both extensions and undo the profile changes
    ubo-lpa.py timer       systemd user timer that attempts an update daily
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import contextlib
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

GITHUB_REPO = "gorhill/uBlock"
KNOWN_GOOD = ["1.74.0", "1.73.0", "1.72.0", "1.71.0"]
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

_COLOR = sys.stdout.isatty() and not IS_WINDOWS


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
    if not sys.stdin.isatty():
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
    (tag, from_api). When the release API is unreachable, each candidate below
    is probed with a HEAD request; that list is fixed, so a candidate result is
    never authoritative about what the latest release is.
    """
    try:
        data = json.loads(
            _get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", 20)
        )
        if data.get("tag_name"):
            return data["tag_name"], True
    except Exception:
        pass
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


def download_ubo(tag: str, dest: Path) -> None:
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
    """Apply the seven edits to a freshly extracted copy."""
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
    # Only the document the companion iframes has to be reachable from another
    # origin. Its dependencies (css/, js/, img/) load from the same origin once
    # the document is running. Listing those dependencies would unnecessarily
    # increase the resources exposed to websites.
    war = list(manifest.get("web_accessible_resources", []))
    if "popup-fenix.html" not in war:
        war.append("popup-fenix.html")
    manifest["web_accessible_resources"] = war

    # 4. let the companion message uBO for the badge count
    manifest["externally_connectable"] = {"ids": [companion_id]}

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

    # 5. badge bridge
    vapi = ext / "js" / "vapi-background.js"
    source = vapi.read_text(encoding="utf-8")
    if "uboBadge" not in source:
        with vapi.open("a", encoding="utf-8") as handle:
            handle.write(asset("badge-bridge.js"))

    # 6. resize notifier, external file because uBO ships script-src 'self'
    (ext / "js" / "popup-resize.js").write_text(
        asset("popup-resize.js", RESIZE_MSG=RESIZE_MSG, COMP_ID=companion_id),
        encoding="utf-8",
    )
    popup = ext / "popup-fenix.html"
    html = popup.read_text(encoding="utf-8")
    if "popup-resize.js" not in html:
        html = html.replace("</body>", '<script src="js/popup-resize.js"></script>\n</body>')
        popup.write_text(html, encoding="utf-8")

    ok("uBO patched")


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
        "permissions": ["tabs"],
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
        print(f"  {'manifest wrong shape':<26} expected a JSON object")
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
        "resize shim",
        resize.exists() and "postMessage" in resize.read_text(encoding="utf-8"),
        True,
    )
    row("popup-fenix in WAR", "popup-fenix.html" in manifest.get("web_accessible_resources", []), True)
    row("popup-resize tag", "popup-resize.js" in (ext / "popup-fenix.html").read_text(encoding="utf-8"), True)
    row("browser_action kept", "browser_action" in manifest, True)
    # Checked inside the frame-ancestors directive, since the companion origin
    # appearing in some other directive would not permit the framing.
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
        # key, so an unreadable one silently changes identity later.
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
                entry = entries.get(eid)
                if entry is None:
                    state = "absent"
                elif entry.get("disable_reasons"):
                    state = "disabled"
                elif entry.get("state") not in (1, None):
                    state = f"state={entry.get('state')}"
                elif Path(str(entry.get("path", ""))) != want_dir:
                    state = "wrong path"
                else:
                    state = "enabled"
                row(f"{label} {name}", state, "enabled")
            dev = prefs.get("extensions", {}).get("ui", {}).get("developer_mode")
            row(f"{label} developer mode", dev, True)
        if not found:
            print("  (no Chrome or Chromium profile found)")

    print()
    print("PROBLEMS FOUND" if problems else "all consistent")
    return problems


# --- installers -----------------------------------------------------------


class Installer:
    """Shared build steps. Subclasses supply the platform delivery."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ext = root / "extension"
        self.comp = root / "companion"
        self.state_file = root / ".state.json"

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
        """Require the state markers written by this installer."""
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
        # Written before the keys, so a failed first install still leaves a
        # directory uninstall recognises as its own. A directory that already
        # holds something else is never adopted, because installing would
        # replace fixed child names inside it.
        if not self.owns_root():
            existing = [p.name for p in self.root.iterdir()] if self.root.is_dir() else []
            if existing:
                die(
                    f"{self.root} is not empty and carries no ubo-lpa marker "
                    f"({', '.join(sorted(existing)[:4])}). Choose an empty or "
                    "new directory."
                )
            self.save_state()
        ubo_key, ubo_id = ensure_key(self.root / "ubo.pem")
        comp_key, comp_id = ensure_key(self.root / "comp.pem")
        return ubo_id, comp_id, ubo_key, comp_key

    # -- platform-specific delivery
    def deliver(self, ubo_id: str, comp_id: str, version: str) -> None:
        raise NotImplementedError

    def remove(self) -> None:
        raise NotImplementedError

    # -- shared build
    def install(self, quiet: bool = False) -> int:
        self.preflight()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock():
            return self._install(quiet)

    @staticmethod
    @contextlib.contextmanager
    def lock():
        """
        Serialise every mutating command. The file lives outside the install
        directory, since uninstall deletes that, and the name is fixed rather
        than per-directory because two different --dir runs still edit the same
        Chrome profiles.
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

        info("Checking for latest uBO release...")
        tag, from_api = resolve_latest_tag()
        if not tag:
            die("Could not reach any uBO release.")
        installed = self.state().get("version")

        present = self.state_file.exists() and self.ext.is_dir() and self.comp.is_dir()
        inconsistent = present and check_quiet(self.root, ubo_id, comp_id) > 0
        outdated = present and installed != tag

        if installed and version_tuple(tag) < version_tuple(installed):
            # The candidate list cannot see releases newer than itself, so an
            # unreachable API must never turn into a downgrade. A broken install
            # is still rebuilt from the version already recorded.
            source = "the release API" if from_api else "the fallback candidates"
            info(f"{source} offers {tag}, older than the installed {installed}.")
            if present and not inconsistent:
                info("Keeping the installed version.")
                return 0
            info(f"Installation is incomplete, so rebuilding {installed}.")
            tag = installed
            present = False

        if present:
            if inconsistent:
                warn("Installed but inconsistent:")
                check(self.root, ubo_id, comp_id)
                print()
                if not quiet and not ask("Repair it (re-download and rebuild)?", True):
                    info("No changes made.")
                    return 0
            elif outdated:
                ok(f"Installed: uBO {installed}; {tag} is available.")
            else:
                ok(f"Installed: uBO {installed} (current)")
                if quiet:
                    info("No changes made.")
                    return 0
                if not ask("Installation is already current. Reinstall?", False):
                    info("No changes made.")
                    return 0
            print()

        # Build and verify the complete staged copy before modifying the working
        # installation. A changed upstream layout or failed patch therefore
        # leaves the existing installation operational.
        staging = self.root / ".staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            download_ubo(tag, staging / "extension")
            patch_ubo(staging / "extension", comp_id, ubo_key)
            build_companion(staging / "companion", ubo_id, staging / "extension", comp_key)
            if check_quiet(staging, ubo_id, comp_id, profiles=False,
                           keys_root=self.root) > 0:
                check(staging, ubo_id, comp_id, profiles=False, keys_root=self.root)
                die("built copy failed verification; existing installation unchanged")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        swap_pair([
            (staging / "extension", self.ext),
            (staging / "companion", self.comp),
        ])
        shutil.rmtree(staging, ignore_errors=True)

        # IDs are recorded before any profile is touched, so a delivery that
        # fails part way through still leaves uninstall able to find the entries
        # it wrote. The version follows once delivery has succeeded.
        self.save_state(ext_id=ubo_id, comp_id=comp_id)
        self.deliver(ubo_id, comp_id, tag)
        self.save_state(version=tag)
        return 0

    def preflight(self) -> None:
        pass

    OWNED = (
        "extension",
        "companion",
        ".staging",
        ".state.json",
        "ubo.pem",
        "comp.pem",
        ".lock",
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
                f"{self.root} carries no ubo-lpa marker; refusing to delete a "
                "directory this installer did not create"
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
                dest.rename(backup)
            else:
                backup = None
            # Record the directory before moving it so a failure restores this
            # directory as well as those already completed.
            done.append((dest, backup))
            new_dir.rename(dest)
    except OSError:
        for dest, backup in reversed(done):
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            if backup is not None and backup.exists():
                backup.rename(dest)
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
    """Injects directly into each profile's Preferences. Chrome must be closed."""

    def preflight(self) -> None:
        if chrome_running():
            die("Chrome is running. Close it first.")

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

    def deliver(self, ubo_id: str, comp_id: str, version: str) -> None:
        targets = self.profiles()
        if not targets:
            die("No Chrome/Chromium profile found.")
        for profile in targets:
            self._inject_profile(profile, ubo_id, comp_id)
            ok(f"Injected into {profile.parent.name}/{profile.name}")
        print()
        ok(f"uBlock Origin {version} installed as a legacy packaged app")
        info("Start Chrome to activate.")

    def _inject_profile(self, profile: Path, ubo_id: str, comp_id: str) -> None:
        prefs_path = profile / "Preferences"
        prefs = json.loads(prefs_path.read_text())
        settings = prefs.setdefault("extensions", {}).setdefault("settings", {})

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

        # Chrome disables unpacked MV3 extensions with
        # DISABLE_UNSUPPORTED_DEVELOPER_EXTENSION (1<<24) unless this is set.
        # uBO is exempt as a legacy packaged app; the companion is not. Without
        # this setting, request blocking functions but the toolbar button is
        # absent.
        ui = prefs["extensions"].setdefault("ui", {})
        touched_dev_mode = ui.get("developer_mode") is not True
        ui["developer_mode"] = True

        backup = prefs_path.with_suffix(prefs_path.suffix + ".ubo-lpa.bak")
        if not backup.exists():
            shutil.copy2(prefs_path, backup)

        for ext_id, ext_dir in ((ubo_id, self.ext), (comp_id, self.comp)):
            settings[ext_id] = self._entry(ext_id, ext_dir)

        stock = settings.get(STOCK_UBO_ID)
        touched_stock = bool(stock) and stock.get("state") == 1
        if touched_stock:
            stock["state"] = 0
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

    @staticmethod
    def _entry(ext_id: str, ext_dir: Path) -> dict:
        manifest = read_json(ext_dir / "manifest.json")
        perms = manifest.get("permissions", [])
        hosts = [p for p in perms if p.startswith("<") or "://" in p]
        apis = [p for p in perms if p not in hosts]
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
                f"{self.root} carries no ubo-lpa marker; refusing to touch "
                "profiles or files for a directory this installer did not create"
            )
        if chrome_running():
            die("Chrome is running. Close it first.")
        state = self.state()
        ids = {state.get("ext_id"), state.get("comp_id")} - {None}
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
            if changed:
                write_prefs(prefs_path, prefs)
                ok(f"Cleaned {profile.name}")
            backup = prefs_path.with_suffix(prefs_path.suffix + ".ubo-lpa.bak")
            backup.unlink(missing_ok=True)
        # Disable before unlinking, or systemd keeps a dangling symlink in
        # timers.target.wants.
        unit_dir = Path.home() / ".config/systemd/user"
        if (unit_dir / f"{TIMER_NAME}.timer").exists():
            result = systemctl("disable", "--now", f"{TIMER_NAME}.timer")
            if result is None:
                info("systemctl not found; leaving the timer units in place")
            elif result.returncode != 0:
                warn(f"could not disable the timer: {result.stderr.strip()}")
        for unit in (f"{TIMER_NAME}.service", f"{TIMER_NAME}.timer"):
            (unit_dir / unit).unlink(missing_ok=True)
        systemctl("daemon-reload")
        self.remove_install_dir()
        ok("Uninstalled.")

    def timer(self) -> None:
        unit_dir = Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        script = Path(__file__).resolve()
        # systemd splits ExecStart on whitespace and treats % specially, so both
        # paths are quoted and any % doubled.
        def esc(value: str) -> str:
            return '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"'

        argv = [esc(sys.executable), esc(script), "install", "--non-interactive"]
        if self.root != default_root():
            argv += ["--dir", esc(self.root)]
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
    Stages files only. Chrome on Windows rejects the tested automated routes for
    an off-store extension (see WRITEUP.md), so the final step is manual.
    """

    def deliver(self, ubo_id: str, comp_id: str, version: str) -> None:
        print()
        ok(f"Staged uBlock Origin {version} in {self.root}")
        print()
        warn("Chrome requires manual completion of this installation:")
        print()
        print("  1. Open  chrome://extensions")
        print("  2. Turn on  Developer mode  (top right)")
        print(f"  3. Load unpacked  ->  {self.ext}")
        print(f"  4. Load unpacked  ->  {self.comp}")
        print()
        info("Both extensions persist across browser restarts. See WRITEUP.md for details.")

    def remove(self) -> None:
        self.remove_install_dir()
        ok("Removed staged files.")
        info("Remove both entries from chrome://extensions as well; this script\n   cannot unload them.")

    def timer(self) -> None:
        info("No timer on Windows; re-run install to update.")


def systemctl(*args: str) -> subprocess.CompletedProcess | None:
    """Run systemctl --user, or return None when it is not available."""
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True
        )
    except FileNotFoundError:
        return None


def chrome_running() -> bool:
    """True if any browser whose profiles this installer touches is running."""
    for name in ("chrome", "chromium", "chromium-browser", "google-chrome"):
        try:
            if subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0:
                return True
        except FileNotFoundError:
            return False
    return False


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
        choices=["install", "update", "check", "status", "uninstall", "timer"],
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
    args = parser.parse_args(argv)

    # Resolved because the path is written verbatim into Chrome's Preferences,
    # where a relative one would depend on the browser's working directory.
    root = (args.dir.expanduser().resolve() if args.dir else default_root())
    installer: Installer = (WindowsInstaller if IS_WINDOWS else LinuxInstaller)(root)

    if args.command in ("install", "update"):
        return installer.install(quiet=args.non_interactive)
    if args.command == "uninstall":
        with Installer.lock():
            installer.remove()
        return 0
    if args.command == "timer":
        with Installer.lock():
            installer.timer()
        return 0

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
