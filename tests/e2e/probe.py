#!/usr/bin/env python3
"""
Browser test for ubo-lpa. setup-ubo-wine-harness.sh runs it against Windows
Chrome under Wine and against Linux Chrome.

Uses CDP directly through a minimal WebSocket client built on the standard
library, without Node, npm or a browser-automation package. Requires Python 3.9
or newer.

  PROBE_MODE=install   install both extensions through the Load unpacked dialog
  PROBE_MODE=test      exercise the installed pair

The timing of the steps and the way the checks observe Chrome and uBO follow
from these points:

  * Chrome reloads chrome://extensions after the native picker closes, so
    navigating again at that moment can remain pending indefinitely.
  * uBO readiness is queried through chrome.runtime.getBackgroundPage from an
    extension page. A background_page target is not a reliable readiness signal
    on a constrained host.
  * uBO's panel waits on requestAnimationFrame, which Chrome suspends in a
    hidden tab, so the companion's tab must be in front for uBO's panel to
    finish rendering.
  * Blocking is verified by a canary that must never reach the local server,
    rather than by reading the page, because uBO redirects to an empty response.
  * Clicking Load unpacked opens a modal dialog that stalls the renderer's CDP
    reply, so the command is sent without waiting for a response.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import traceback
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


class ProbeError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


# --- websocket client -------------------------------------------------------
#
# CDP needs client text frames, server text frames and ping handling. The
# client does not send an Origin header, because Chrome refuses DevTools
# connections that have one unless --remote-allow-origins allows that origin.
# Most client libraries send it, which is why they need the flag.


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port or 80
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self._buffer = b""
        header = self._read_until(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise ProbeError(f"websocket upgrade refused: {header.splitlines()[0]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ProbeError("connection closed during handshake")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head + marker

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self._buffer)))
            if not chunk:
                raise ProbeError("connection closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        self.sock.sendall(
            bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def readable(self) -> bool:
        """True if part of a message has arrived, so recv() returns soon."""
        return bool(self._buffer) or bool(select.select([self.sock], [], [], 0)[0])

    def recv(self) -> str:
        chunks: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            fin, opcode, length = first & 0x80, first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b""
            if opcode == 0x9:
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0x8:
                raise ProbeError("websocket closed by the browser")
            if opcode in (0x0, 0x1, 0x2):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8", "replace")


# --- CDP --------------------------------------------------------------------


def result_value(result: dict):
    return (result.get("result") or {}).get("value")


class Cdp:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.version = self.wait_for_endpoint()
        self.ws = WebSocket(self.version["webSocketDebuggerUrl"])
        self.next_id = 1
        self.sessions: dict[str, str] = {}
        self.events: list[dict] = []

    def wait_for_endpoint(self) -> dict:
        # The second browser starts with uBO already installed. While uBO
        # compiles its filter lists under Wine, the browser can be so busy that
        # the DevTools HTTP server accepts connections and does not answer them.
        # Wait up to CDP_WAIT_SECONDS (420 s by default) and report the last
        # connection error.
        last = None
        deadline = time.time() + float(os.environ.get("CDP_WAIT_SECONDS", "420"))
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            try:
                with urllib.request.urlopen(f"{self.endpoint}/json/version", timeout=5) as r:
                    if attempts > 1:
                        log(f"CDP_READY after {attempts} attempts")
                    return json.loads(r.read())
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise ProbeError(
            f"CDP endpoint unavailable after {attempts} attempts over "
            f"{int(time.time() - (deadline - float(os.environ.get('CDP_WAIT_SECONDS', '420'))))}s: {last}")

    def targets(self) -> list[dict]:
        with urllib.request.urlopen(f"{self.endpoint}/json/list", timeout=20) as r:
            return json.loads(r.read())

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 20.0) -> dict:
        message_id = self.next_id
        self.next_id += 1
        payload = {"id": message_id, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                # During startup, Chrome under Wine can take longer than one
                # socket timeout to reply, so keep reading until this
                # command's deadline.
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise ProbeError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
        raise ProbeError(f"timed out waiting for {method}")

    def drain(self) -> None:
        """
        Keep the events Chrome has already sent, without waiting for more.
        Only whole messages are read: a shorter socket timeout could stop
        recv() between a frame's header and its payload.
        """
        while self.ws.readable():
            message = json.loads(self.ws.recv())
            if "method" in message:
                self.events.append(message)

    def send_nowait(self, method: str, params: dict | None = None,
                    session: str | None = None) -> None:
        payload = {"id": self.next_id, "method": method, "params": params or {}}
        self.next_id += 1
        if session:
            payload["sessionId"] = session
        self.ws.send(json.dumps(payload))

    def attach(self, target_id: str) -> str:
        if target_id not in self.sessions:
            result = self.send("Target.attachToTarget",
                               {"targetId": target_id, "flatten": True}, timeout=60)
            session = result["sessionId"]
            self.sessions[target_id] = session
            self.wait_for_context(session)
        return self.sessions[target_id]

    def wait_for_context(self, session: str, timeout: float = 60.0) -> None:
        """
        Runtime.evaluate blocks while a page is mid-navigation, so evaluating
        before the JS context exists looks like a hang rather than an error.
        Runtime.enable replays executionContextCreated for contexts that already
        exist, which is the signal that evaluating is safe.
        """
        self.send("Runtime.enable", {}, session, timeout=30)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for event in self.events:
                if (event.get("method") == "Runtime.executionContextCreated"
                        and event.get("sessionId") == session):
                    return
            self.drain()
            time.sleep(0.25)
        raise ProbeError("the page did not get an execution context")

    def target_for(self, session: str) -> str | None:
        return next((t for t, s in self.sessions.items() if s == session), None)

    def new_page(self, url: str = "about:blank") -> str:
        return self.attach(self.send("Target.createTarget", {"url": url})["targetId"])

    def close_page(self, session: str) -> None:
        target_id = self.target_for(session)
        if not target_id:
            return
        try:
            self.send("Target.closeTarget", {"targetId": target_id}, timeout=10)
        except ProbeError:
            pass
        self.sessions.pop(target_id, None)

    def evaluate(self, session: str, expression: str, await_promise: bool = True):
        result = self.send("Runtime.evaluate",
                           {"expression": expression, "returnByValue": True,
                            "awaitPromise": await_promise}, session)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise ProbeError(f"evaluate failed: {text}")
        return result.get("result", {}).get("value")

    def url_of(self, session: str) -> str:
        try:
            return self.evaluate(session, "location.href") or ""
        except ProbeError:
            return ""

    def navigate(self, session: str, url: str, timeout: float = 120.0) -> None:
        self.send("Page.enable", {}, session, timeout=60)
        self.send("Page.navigate", {"url": url}, session, timeout=60)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.send("Runtime.evaluate",
                                  {"expression": "document.readyState",
                                   "returnByValue": True}, session, timeout=20)
            except ProbeError:
                # A WebUI renderer under Wine can become temporarily
                # unresponsive during navigation; retry until the navigation
                # deadline.
                continue
            if result_value(state) in ("interactive", "complete"):
                return
            time.sleep(0.25)
        raise ProbeError(f"page did not load: {url}")

    def bring_to_front(self, session: str) -> None:
        try:
            self.send("Page.bringToFront", {}, session, timeout=15)
        except ProbeError:
            pass

    def screenshot(self, session: str, name: str) -> None:
        data = self.send("Page.captureScreenshot",
                         {"format": "png", "captureBeyondViewport": True}, session)["data"]
        path = OUT_DIR / f"{name}.png"
        path.write_bytes(base64.b64decode(data))
        log(f"SCREENSHOT {path}")


def desktop_shot(name: str, quiet: bool = False) -> bool:
    """Capture the whole screen, which in a windowed run also shows Chrome's
    window frame and native dialogs such as the folder picker."""
    path = OUT_DIR / f"{name}.png"
    if not os.environ.get("DISPLAY"):
        return False
    try:
        subprocess.run([IMPORT_BIN, "-window", "root", str(path)],
                       check=True, capture_output=True, timeout=60)
        log(f"SCREENSHOT {path}")
        return True
    except Exception:  # noqa: BLE001
        if not quiet:
            log(f"SCREENSHOT-FAILED {path}")
        return False


def capture(cdp: Cdp, session: str, name: str, pause: float = 1.4) -> None:
    """
    Prefers the desktop capture, which shows the browser frame itself. Without
    a display, or when the screen capture fails, it falls back to a CDP
    screenshot of the page.
    """
    cdp.bring_to_front(session)
    time.sleep(pause)
    if desktop_shot(name, quiet=True):
        return
    try:
        cdp.screenshot(session, name)
    except ProbeError:
        log(f"SCREENSHOT-FAILED {OUT_DIR / (name + '.png')}")


# --- fixture server ---------------------------------------------------------


class Fixtures(http.server.BaseHTTPRequestHandler):
    """
    Serves the test pages and stands in for the advertising host, which Chrome
    resolves here via --host-resolver-rules. The blocking check asserts that
    canary_hits stays at 0, since a blocked request never arrives.
    """

    canary_hits = 0

    def log_message(self, *args):
        pass

    def _send(self, body: str) -> None:
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        port = self.server.server_address[1]
        if path == "/block":
            self._send(
                "<!doctype html><meta charset=utf-8>"
                "<title>uBO blocking probe</title>"
                "<style>body{font:20px system-ui;margin:40px}</style>"
                "<h1>uBO blocking probe</h1>"
                "<p id=status>Requesting a known advertising URL...</p>"
                f"<script>fetch('http://pagead2.googlesyndication.com:{port}"
                "/canary?ubo-wine=' + Date.now())"
                ".then(()=>status.textContent='request completed')"
                ".catch(()=>status.textContent='request failed');</script>")
        elif path == "/early":
            # The document's first script records whether uBO's main-world
            # scriptlet has already run. An anti-adblock loader in <head> runs
            # as early, and a scriptlet that runs after it comes too late. The
            # remove-node-text scriptlet has to empty the second script before
            # it runs. The third script creates an empty iframe and at once
            # reads a value from the iframe's window, where a page would look
            # for functions that uBO has not wrapped. It then writes the
            # iframe's document, as advertising code does. Calling
            # document.open() removes the listeners on the frame's window,
            # among them the one that cancels the presence event. uBO's own
            # injection arrives later and still has to detect that the
            # scriptlets ran in the frame.
            self._send(
                "<!doctype html><script>window.__early = typeof window.uboLpaEarly;"
                "</script><script>/*UBOLPA_REMOVE*/window.__removedRan = true;</script>"
                "<meta charset=utf-8><title>scriptlet timing</title>"
                "<p>Scriptlet timing fixture</p>"
                "<script>const f = document.createElement('iframe');"
                "document.body.appendChild(f);"
                "window.__frame = typeof f.contentWindow.uboLpaEarly;"
                "f.contentDocument.open();f.contentDocument.write('<p>written</p>');"
                "f.contentDocument.close();</script>")
        elif path == "/canary":
            Fixtures.canary_hits += 1
            self.send_response(204)
            self.end_headers()
        elif path == "/hostile":
            self._send(
                "<!doctype html><meta charset=utf-8>"
                "<title>frame-ancestors test</title>"
                "<style>body{font:18px system-ui;margin:32px;background:#f4f6fb}"
                "iframe{width:300px;height:500px;border:5px solid #b3261e;background:#fff}"
                "#result{font-weight:700;padding:12px;background:#fff}</style>"
                "<h1>Can an ordinary http:// page frame uBO's popup?</h1>"
                "<p>The red box attempts to embed uBO's popup with an explicit tab ID.</p>"
                "<p id=result>Checking frame-ancestors...</p>"
                "<iframe id=victim src=about:blank></iframe>")
        else:
            self.send_response(404)
            self.end_headers()


def start_fixtures() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


# --- shadow-piercing query --------------------------------------------------

DEEP = """
  function deepAll(selector, root) {
    root = root || document;
    const out = [];
    const walk = node => {
      if (!node.querySelectorAll) { return; }
      node.querySelectorAll(selector).forEach(el => out.push(el));
      node.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
    };
    walk(root);
    return out;
  }
  function deepOne(selector, root) { return deepAll(selector, root)[0] || null; }
"""


# --- extensions page --------------------------------------------------------


def first_page(cdp: Cdp) -> str:
    """
    A tab opened directly at chrome://extensions. Under Wine, the renderer of
    the about:blank tab that Chrome opens at startup frequently terminates, and
    once the probe has attached to it, later commands run into their timeouts.
    Creating the tab at chrome://extensions avoids that renderer and saves a
    separate navigation.
    """
    return cdp.new_page("chrome://extensions/")


def open_extensions(cdp: Cdp, session: str) -> str:
    """
    Chrome reloads this WebUI itself once a native picker closes, so navigating
    at that moment can hang. The current URL is read from the browser's target
    list instead of by evaluating in the page, because under Wine the page's
    renderer can already have terminated, and an evaluate would then wait out
    the full timeout.
    """
    target_id = cdp.target_for(session)
    url = next((t.get("url", "") for t in cdp.targets() if t["id"] == target_id), "")
    if not url.startswith("chrome://extensions"):
        cdp.navigate(session, "chrome://extensions/")
    deadline = time.time() + 60
    while time.time() < deadline:
        if cdp.evaluate(session, DEEP + " !!deepOne('extensions-manager')"):
            time.sleep(0.7)
            return session
        time.sleep(0.5)
    raise ProbeError("chrome://extensions did not render")


def extension_cards(cdp: Cdp, session: str) -> list[dict]:
    """Assumes the caller has chrome://extensions open. Navigating there again
    for every read would reload the WebUI each time."""
    return json.loads(cdp.evaluate(session, DEEP + """
      JSON.stringify(deepAll('extensions-item').map(item => {
        const shadow = item.shadowRoot;
        const idNode = shadow ? shadow.querySelector('#extension-id') : null;
        const raw = (idNode ? idNode.textContent : '') || item.id || '';
        const match = raw.match(/[a-p]{32}/);
        const name = shadow ? shadow.querySelector('#name') : null;
        const version = shadow ? shadow.querySelector('#version') : null;
        const toggle = shadow ? shadow.querySelector('#enableToggle') : null;
        return {
          id: match ? match[0] : raw.trim(),
          name: name ? name.textContent.trim() : '',
          version: version ? version.textContent.trim() : '',
          enabled: toggle ? !!toggle.checked : null,
        };
      }))"""))


def card_warnings(cdp: Cdp, session: str) -> dict:
    """Return per-card warning text, including why an extension is disabled."""
    return json.loads(cdp.evaluate(session, DEEP + """
      JSON.stringify(Object.fromEntries(deepAll('extensions-item').map(item => {
        const shadow = item.shadowRoot;
        const texts = [];
        if (shadow) {
          shadow.querySelectorAll('#warnings-container, .warning-message, #a11yAssociation')
            .forEach(node => { if (node.textContent) texts.push(node.textContent.trim()); });
        }
        return [item.id, texts.join(' | ').slice(0, 400)];
      })))"""))


def extension_diagnostic_info(cdp: Cdp, session: str) -> dict:
    """Return Chrome's manifest, install, and runtime diagnostics per item.

    Chrome 152 puts the two expected LPA manifest warnings in
    ``manifestErrors`` after Load unpacked while leaving ``installWarnings``
    empty. Older builds and other loading routes can use ``installWarnings``,
    so both sources are retained and checked. Runtime errors are kept apart
    from these warnings and are never allowed.
    """
    return json.loads(cdp.evaluate(session, """
      chrome.developerPrivate.getExtensionsInfo().then(list =>
        JSON.stringify(Object.fromEntries(
          list.map(e => [e.id, {
            installWarnings: e.installWarnings || [],
            manifestErrors: (e.manifestErrors || []).map(error =>
              typeof error === 'string' ? error
                : (error.message || error.error || JSON.stringify(error))),
            runtimeErrors: (e.runtimeErrors || []).map(error =>
              typeof error === 'string' ? error
                : (error.message || error.error || JSON.stringify(error))),
          }]))))"""))


EXPECTED_UBO_MANIFEST_WARNINGS = [
    "'browser_action' is only allowed for extensions, but this is a legacy packaged app.",
    "'commands' is only allowed for extensions and packaged apps, but this is a legacy packaged app.",
]


def unexpected_extension_diagnostics(info: dict, ubo_id: str, companion_id: str,
                                     require_expected_warnings: bool) -> dict:
    """Return the unexpected diagnostics, keyed "<id>.warnings" and
    "<id>.runtimeErrors". An empty dict means the check passes.

    On Windows, where uBO is installed through Load unpacked, it must show
    exactly the two known manifest warnings. On Linux, where Chrome loads uBO
    at startup from the entry the installer wrote into Preferences, uBO may
    show that exact pair or no warnings at all. The companion must not have
    any warnings, and a runtime error in either extension always fails the
    check. Diagnostics for unrelated built-in extensions are deliberately
    ignored.
    """
    unexpected: dict = {}
    records = {
        ubo_id: info.get(ubo_id, {}),
        companion_id: info.get(companion_id, {}),
    }
    warning_sets: dict[str, list[str]] = {}
    for ext_id, record in records.items():
        warnings = list(dict.fromkeys(
            list(record.get("installWarnings", []))
            + list(record.get("manifestErrors", []))))
        warning_sets[ext_id] = warnings
        runtime_errors = list(record.get("runtimeErrors", []))
        if runtime_errors:
            unexpected[f"{ext_id}.runtimeErrors"] = runtime_errors

    ubo_warnings = warning_sets[ubo_id]
    exact_pair = sorted(ubo_warnings) == sorted(EXPECTED_UBO_MANIFEST_WARNINGS)
    if (require_expected_warnings and not exact_pair) or (
            not require_expected_warnings and ubo_warnings and not exact_pair):
        unexpected[f"{ubo_id}.warnings"] = {
            "expected": EXPECTED_UBO_MANIFEST_WARNINGS,
            "actual": ubo_warnings,
            "required": require_expected_warnings,
        }
    if warning_sets[companion_id]:
        unexpected[f"{companion_id}.warnings"] = warning_sets[companion_id]
    return unexpected


def check_extension_diagnostics(cdp: Cdp, session: str,
                                require_expected_warnings: bool) -> None:
    """Log uBO's and the companion's diagnostics, and raise ProbeError if any
    of them are unexpected."""
    info = extension_diagnostic_info(cdp, session)
    ours = {ext_id: info.get(ext_id, []) for ext_id in (UBO_ID, COMPANION_ID)}
    log(f"EXTENSION_DIAGNOSTICS {json.dumps(ours)}")
    unexpected = unexpected_extension_diagnostics(
        info, UBO_ID, COMPANION_ID, require_expected_warnings)
    if unexpected:
        raise ProbeError(
            f"unexpected extension diagnostics: {json.dumps(unexpected)}")


def set_dev_mode(cdp: Cdp, session: str) -> str:
    # On a cold start, extensions-toolbar can render after extensions-manager,
    # and reading deepOne('extensions-toolbar').shadowRoot right away throws a
    # TypeError on null. Wait for the toolbar's #devMode toggle, as
    # open_extensions waits for extensions-manager.
    for _ in range(60):
        if cdp.evaluate(session, DEEP + """
          (() => { const t = deepOne('extensions-toolbar');
                   return !!(t && t.shadowRoot
                             && t.shadowRoot.querySelector('#devMode')); })()"""):
            break
        time.sleep(0.5)
    else:
        raise ProbeError("extensions toolbar never rendered")
    # #devMode is a cr-toggle. Its checked property holds the state; Chrome
    # 157 marks it with role=switch and aria-checked, where Chrome 152 used
    # aria-pressed.
    cdp.evaluate(session, DEEP + """
      (() => {
        const toggle = deepOne('extensions-toolbar').shadowRoot.querySelector('#devMode');
        if (toggle.checked !== true) { toggle.click(); }
      })()""")
    time.sleep(1.0)
    settled = cdp.evaluate(session, DEEP + """
      (() => {
        const toggle = deepOne('extensions-toolbar').shadowRoot.querySelector('#devMode');
        return toggle.checked === true;
      })()""")
    log(f"DEV_MODE {settled}")
    if settled is not True:
        raise ProbeError("developer mode did not turn on")
    return session


# --- the native folder picker ----------------------------------------------


def run_picker_helper(argument: str) -> str:
    """
    The Win32 helper finds the dialog by control ID rather than by title,
    focuses the filename edit and verifies that it received focus, ensuring that
    the path is entered in the intended control.
    """
    env = dict(os.environ)
    if os.environ.get("USE_SHIM") == "1" and os.environ.get("SHIM"):
        preload = os.environ["SHIM"]
        if os.environ.get("LD_PRELOAD"):
            preload += ":" + os.environ["LD_PRELOAD"]
        env["LD_PRELOAD"] = preload
    result = subprocess.run([WINE_BIN, PYTHON_EXE, PICKER_HELPER_WIN, argument],
                            capture_output=True, text=True, timeout=90, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"status {result.returncode}").strip()
        raise ProbeError(f"Win32 folder helper failed: {detail}")
    return result.stdout.strip()


def xdo(*args: str) -> str:
    result = subprocess.run([XDOTOOL, *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ProbeError(f"xdotool {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def select_folder(windows_path: str) -> None:
    log(f"PICKER {run_picker_helper(windows_path)}")
    # --clearmodifiers is required: a stuck modifier otherwise drops Shift on
    # capitals and the typed path silently becomes lowercase.
    xdo("key", "--clearmodifiers", "ctrl+a")
    xdo("type", "--clearmodifiers", "--delay", "1", windows_path)
    xdo("key", "--clearmodifiers", "Return")
    log(f"PICKER {run_picker_helper('--wait-closed')}")


def ext_running(cdp: Cdp, ext_id: str) -> list[dict]:
    """Extension processes visible at browser level, independent of the WebUI."""
    return [t for t in cdp.targets()
            if t.get("type") in ("background_page", "service_worker")
            and ext_id in t.get("url", "")]


def load_unpacked(cdp: Cdp, session: str, windows_path: str, expected: str, label: str) -> None:
    # The click is sent without awaiting a response because the modal dialog
    # blocks the renderer's reply. A click may also be dropped while the WebUI
    # is re-rendering, so retry until the dialog appears.
    last: ProbeError | None = None
    for attempt in range(3):
        # The previous load re-renders the list, and a click dispatched during
        # that update is dropped. Wait for the button to be present, then one
        # more second for the rendering to stabilize, before dispatching,
        # because the click gets no reply that would show whether it worked.
        for _ in range(40):
            if cdp.evaluate(session, DEEP + """
                  !!(deepOne('extensions-toolbar')
                     && deepOne('extensions-toolbar').shadowRoot.querySelector('#loadUnpacked'))"""):
                break
            time.sleep(0.5)
        time.sleep(1.0)
        cdp.send_nowait("Runtime.evaluate", {
            "expression": DEEP + """
              deepOne('extensions-toolbar').shadowRoot.querySelector('#loadUnpacked').click()""",
            "returnByValue": True,
        }, session)
        try:
            select_folder(windows_path)
            break
        except ProbeError as exc:
            last = exc
            log(f"PICKER_RETRY {label} attempt {attempt + 1}: {exc}")
            time.sleep(2)
    else:
        desktop_shot(f"00-{label}-picker-failed")
        raise last or ProbeError(f"{label}: the picker never appeared")
    # Confirm through the browser-level target list first. On a constrained
    # host, the extensions WebUI renderer can be killed while uBO compiles its
    # filter lists, so the install check must not depend on the WebUI.
    deadline = time.time() + 120
    while time.time() < deadline:
        if any(ext_running(cdp, expected)):
            log(f"LOAD_UNPACKED {label} id={expected} (extension process running)")
            return
        try:
            cards = extension_cards(cdp, session)
        except ProbeError:
            time.sleep(1)
            continue
        if any(c["id"] == expected and c["enabled"] is not False for c in cards):
            log(f"LOAD_UNPACKED {label} id={expected}")
            return
        time.sleep(0.5)
    desktop_shot(f"00-{label}-load-failed")
    raise ProbeError(f"{label}: {expected} never started")


# --- uBO helpers ------------------------------------------------------------

MU = "\u00b5"

BACKGROUND_JS = """
  new Promise((resolve, reject) => {
    const deadline = Date.now() + 45000;
    const poll = () => chrome.runtime.getBackgroundPage(background => {
      if (chrome.runtime.lastError || !background || !background.MU_Block) {
        if (Date.now() >= deadline) {
          return reject(new Error((chrome.runtime.lastError || {}).message
            || 'uBO background missing'));
        }
        return setTimeout(poll, 200);
      }
      BODY
    });
    poll();
  })
""".replace("MU_", MU)


def background_call(cdp: Cdp, session: str, body: str):
    return cdp.evaluate(session, BACKGROUND_JS.replace("BODY", body.replace("MU_", MU)))


def wait_for_ubo_ready(cdp: Cdp, session: str) -> None:
    background_call(cdp, session, """
      if (background.MU_Block.readyToFilter === true) { return resolve(true); }
      if (Date.now() >= deadline) { return reject(new Error('uBO not ready to filter')); }
      setTimeout(poll, 200);""")


def set_show_badge(cdp: Cdp, session: str, value):
    return background_call(cdp, session, f"""
      const previous = background.MU_Block.userSettings.showIconBadge;
      background.MU_Block.userSettings.showIconBadge = {json.dumps(value)};
      return resolve(previous);""")


def bridge_message(cdp: Cdp, companion: str, tab_id: int) -> dict:
    return json.loads(cdp.evaluate(companion, f"""
      new Promise(resolve => {{
        chrome.runtime.sendMessage('{UBO_ID}', {{what: 'uboBadge', tabId: {tab_id}}}, r => {{
          resolve(JSON.stringify(chrome.runtime.lastError
            ? {{error: chrome.runtime.lastError.message}} : r));
        }});
      }})"""))


def load_counting_main_runs(cdp: Cdp, page: str, url: str) -> dict:
    """
    Load the scriptlet fixture, and return what its scripts recorded and
    whether uBO's main-world scriptlet ran at all. The result also has
    mainRuns, the number of times uBO's main-world scriptlet code ran in each
    of the fixture's two documents: the page and the empty iframe that the
    page creates. The count comes from the debugger, which reports every
    script that Chrome compiles for the page. Three of those scripts can
    contain the main-world code: the registered user script, the inline
    script that uBO's content script inserts into the iframe, and the inline
    script that uBO appends when it injects the code itself.
    """
    cdp.drain()
    cdp.events.clear()
    cdp.send("Page.navigate", {"url": url}, page, timeout=60)
    deadline = time.time() + 20
    state: dict = {}
    while time.time() < deadline:
        cdp.drain()
        try:
            state = json.loads(cdp.evaluate(page, """
              JSON.stringify({early: window.__early, removed: window.__removedRan === true,
                              frame: window.__frame, later: typeof window.uboLpaEarly,
                              ready: document.readyState})"""))
        except ProbeError:
            state = {}
        if state.get("later") == "boolean" and state.get("ready") == "complete":
            break
        time.sleep(0.25)
    # uBO's own injection arrives a moment after the page has loaded.
    time.sleep(1.5)
    cdp.drain()
    runs: dict = {}
    for event in cdp.events:
        if event.get("method") != "Debugger.scriptParsed" or event.get("sessionId") != page:
            continue
        try:
            source = cdp.send("Debugger.getScriptSource",
                              {"scriptId": event["params"]["scriptId"]}, page,
                              timeout=10).get("scriptSource", "")
        except ProbeError:
            continue
        # The code uBO injects into its own isolated world holds the main-world
        # code as a string, without running it there.
        if "uBO_mainWorldScriptlets" in source and "uBO_isolatedWorldWrapper" not in source:
            context = event["params"].get("executionContextId")
            runs[context] = runs.get(context, 0) + 1
    state["mainRuns"] = sorted(runs.values())
    return state


def check_scriptlets(cdp: Cdp, port: int) -> None:
    """
    Check edits 8 and 9. The companion has to register the site's main-world
    and isolated-world scripts with chrome.userScripts. The main-world
    scriptlet then has to run before the page's first inline script, and the
    isolated-world remove-node-text scriptlet has to empty the second script
    before it runs. A new empty iframe has to have the main-world scriptlet
    before the script that created it continues. In every load, the
    main-world code has to run exactly once in each of the two documents, so
    uBO's own injection must not run it a second time. The first load
    registers the scriptlets. A filter list update in the fresh profile can
    reset uBO's scriptlets, and the next load registers them again, so the
    fixture is loaded at least twice and up to eight times.
    """
    # The earlier companion and dashboard tabs may be closed by now.
    companion = cdp.new_page()
    cdp.navigate(companion, f"chrome-extension://{COMPANION_ID}/popup.html")
    dashboard = cdp.new_page()
    cdp.navigate(dashboard, f"chrome-extension://{UBO_ID}/dashboard.html")
    wait_for_ubo_ready(cdp, dashboard)
    available = cdp.evaluate(companion, "typeof chrome.userScripts")
    log(f"COMPANION_USER_SCRIPTS {available}")
    if available != "object":
        raise ProbeError("the companion has no chrome.userScripts")
    background_call(cdp, dashboard, """
      const ub = background.MU_Block;
      ub.applyFilterListSelection({ toSelect: [ ub.userFiltersPath ], merge: true });
      ub.saveUserFilters('127.0.0.1##+js(set, uboLpaEarly, true)\\n' +
                         '127.0.0.1##+js(rmnt, script, UBOLPA_REMOVE)\\n')
        .then(() => ub.loadFilterLists())
        .then(() => resolve(true), reject);""")
    page = cdp.new_page()
    cdp.send("Page.enable", {}, page)
    cdp.send("Debugger.enable", {}, page)
    url = f"http://127.0.0.1:{port}/early"

    def registered() -> list:
        return cdp.evaluate(
            companion, "chrome.userScripts.getScripts().then(s => s.map(x => x.id))") or []

    def early(state: dict) -> bool:
        return (state.get("early") == "boolean" and state.get("removed") is False
                and state.get("frame") == "boolean")

    seen = []
    for _ in range(8):
        seen.append(load_counting_main_runs(cdp, page, url))
        if len(seen) > 1 and early(seen[-1]):
            break
        time.sleep(2)
    ids = registered()
    log(f"SCRIPTLETS {json.dumps(seen)}")
    log(f"USER_SCRIPTS {json.dumps(ids)}")
    if "main:127.0.0.1" not in ids or "isolated:127.0.0.1" not in ids:
        raise ProbeError("the companion did not register the site's scriptlets")
    if len(seen) < 2 or seen[-1].get("early") != "boolean":
        raise ProbeError("uBO's main-world scriptlet did not run before the page's first script")
    if seen[-1].get("removed") is not False:
        raise ProbeError("uBO's remove-node-text scriptlet did not empty the page's second "
                         "script before it ran")
    if seen[-1].get("frame") != "boolean":
        raise ProbeError("uBO's main-world scriptlet had not run in a new empty iframe when "
                         "the script that created it continued")
    if any(state.get("mainRuns") != [1, 1] for state in seen):
        raise ProbeError("in at least one load, uBO's main-world scriptlet code did not run "
                         "exactly once in the page and once in its iframe")

    # uBO runs a second instance for incognito windows. Starting it must
    # leave the regular windows' scriptlets registered. Only the Linux run
    # checks this, because the Windows run does not allow the extensions in
    # incognito.
    if os.environ.get("EXPECT_WINDOWS", "1") != "1":
        def instances() -> int:
            return sum(1 for t in cdp.send("Target.getTargets")["targetInfos"]
                       if t["type"] == "background_page" and UBO_ID in t["url"])
        before = {t["targetId"] for t in cdp.send("Target.getTargets")["targetInfos"]}
        background_call(cdp, dashboard, """
          background.chrome.windows.create({ incognito: true, url: 'about:blank' });
          resolve(true);""")
        for _ in range(60):
            if instances() == 2:
                break
            time.sleep(0.5)
        else:
            raise ProbeError("uBO's incognito instance did not start")
        time.sleep(3)
        state = load_counting_main_runs(cdp, page, url)
        ids = registered()
        log(f"SCRIPTLETS_AFTER_INCOGNITO {json.dumps(state)} {json.dumps(ids)}")
        if "main:127.0.0.1" not in ids or not early(state) or state.get("mainRuns") != [1, 1]:
            raise ProbeError("after an incognito window opened, the site's scriptlets were not "
                             "registered, did not run early, or did not run exactly once in "
                             "each document")
        for target in cdp.send("Target.getTargets")["targetInfos"]:
            if target["type"] == "page" and target["targetId"] not in before:
                try:
                    cdp.send("Target.closeTarget", {"targetId": target["targetId"]}, timeout=10)
                except ProbeError:
                    pass

    # uBO's injected code runs every part that a registered script did not
    # run. With the registrations removed without uBO's knowledge, uBO's own
    # injection has to run the main-world scriptlet once in each document,
    # after the page's first script.
    cdp.evaluate(companion, "chrome.userScripts.unregister().then(() => true)")
    state = load_counting_main_runs(cdp, page, url)
    log(f"SCRIPTLETS_UNREGISTERED {json.dumps(state)}")
    if state.get("later") != "boolean" or state.get("mainRuns") != [1, 1]:
        raise ProbeError("with the registrations removed, uBO's own injection did not run the "
                         "main-world scriptlet code exactly once in the page and once in its "
                         "iframe")
    cdp.close_page(page)
    cdp.close_page(dashboard)
    cdp.close_page(companion)


def check_launch_guard(cdp: Cdp, port: int) -> None:
    """
    Check the launch guard. While uBO's setting "Suspend network activity
    until all filter lists are loaded" is on, the companion keeps a rule that
    redirects every page to its wait.html. A session rule switches the
    redirect off once uBO's request listener is installed, or once wait.html
    stops waiting after ten seconds. Chrome drops session rules when it
    exits, and the probe removes the session rule in place of a restart. The
    next page then has to pass through wait.html and load.
    """
    companion = cdp.new_page()
    cdp.navigate(companion, f"chrome-extension://{COMPANION_ID}/popup.html")
    dashboard = cdp.new_page()
    cdp.navigate(dashboard, f"chrome-extension://{UBO_ID}/dashboard.html")
    wait_for_ubo_ready(cdp, dashboard)

    def rules() -> list:
        return json.loads(cdp.evaluate(companion, """
          Promise.all([chrome.declarativeNetRequest.getDynamicRules(),
                       chrome.declarativeNetRequest.getSessionRules()])
            .then(a => JSON.stringify(a.map(list => list.map(rule => rule.id))))"""))

    def setting(value: bool) -> None:
        background_call(cdp, dashboard, f"""
          background.MU_Block.changeUserSettings(
            'suspendUntilListsAreLoaded', {json.dumps(value)});
          resolve(true);""")

    def guard_becomes(wanted: list, page: str) -> None:
        # uBO includes the setting in each toolbar message, and loading a page
        # makes uBO send one.
        for attempt in range(20):
            cdp.navigate(page, f"http://127.0.0.1:{port}/block?guard={attempt}")
            for _ in range(8):
                if rules()[0] == wanted:
                    return
                time.sleep(0.5)
        raise ProbeError("the companion did not add the launch guard's rule" if wanted
                         else "the companion did not remove the launch guard's rule")

    if rules()[0]:
        raise ProbeError("the launch guard is on while uBO's setting is off")
    page = cdp.new_page()
    setting(True)
    guard_becomes([1], page)
    cdp.evaluate(companion, """
      chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: [2]}).then(() => true)""")
    cdp.send("Page.enable", {}, page)
    cdp.drain()
    cdp.events.clear()
    target = f"http://127.0.0.1:{port}/block?guarded=1"
    cdp.send("Page.navigate", {"url": target}, page, timeout=60)
    for _ in range(80):
        cdp.drain()
        if cdp.url_of(page) == target:
            break
        time.sleep(0.25)
    else:
        raise ProbeError("the page did not load while the launch guard was on")
    cdp.drain()
    visited = [event["params"]["frame"]["url"] for event in cdp.events
               if event.get("method") == "Page.frameNavigated"
               and not event["params"]["frame"].get("parentId")]
    after = rules()
    log(f"LAUNCH_GUARD visited={json.dumps(visited)} rules={json.dumps(after)}")
    if not any(f"{COMPANION_ID}/wait.html" in url for url in visited):
        raise ProbeError("the page did not pass through wait.html")
    if after != [[1], [2]]:
        raise ProbeError("after the wait page, the companion's rules are not exactly the "
                         "guard's rule and the session rule that switches the guard off")
    setting(False)
    guard_becomes([], page)
    cdp.close_page(page)
    cdp.close_page(dashboard)
    cdp.close_page(companion)


def check_side_panel(cdp: Cdp) -> None:
    """
    The companion's side panel shows uBO's logger in a frame. Chrome opens a
    side panel only in response to a user action, so the Runtime.evaluate call
    here sets userGesture.
    """
    companion = cdp.new_page()
    cdp.navigate(companion, f"chrome-extension://{COMPANION_ID}/popup.html")
    result = cdp.send("Runtime.evaluate", {
        "expression": """chrome.windows.getCurrent()
          .then(w => chrome.sidePanel.open({windowId: w.id}))
          .then(() => 'opened', e => 'failed: ' + e.message)""",
        "awaitPromise": True, "returnByValue": True, "userGesture": True}, companion)
    opened = result_value(result)
    log(f"SIDE_PANEL {opened}")
    if opened != "opened":
        raise ProbeError("Chrome did not open the companion's side panel")
    for _ in range(40):
        for target in cdp.send("Target.getTargets")["targetInfos"]:
            if target["type"] != "iframe" or f"{UBO_ID}/logger-ui.html" not in target["url"]:
                continue
            frame = cdp.attach(target["targetId"])
            try:
                if cdp.evaluate(frame, """location.protocol === 'chrome-extension:'
                                          && !!document.getElementById('vwContent')"""):
                    log("SIDE_PANEL_LOGGER ok")
                    desktop_shot("58-side-panel-logger")
                    cdp.close_page(companion)
                    return
            except ProbeError:
                pass
        time.sleep(0.5)
    desktop_shot("00-side-panel-empty")
    raise ProbeError("the side panel opened without uBO's logger")


def check_commands(cdp: Cdp, extensions: str, companion: str, site: str) -> None:
    """
    The companion declares uBO's keyboard commands and passes each one on to
    uBO. Here a companion page sends uBO the message that the companion's
    worker sends for a key press. The Linux run then assigns a shortcut with
    the call that chrome://extensions/shortcuts makes, and presses it, so that
    the command goes through the worker.
    """
    def logger_pages() -> list:
        return [t for t in cdp.targets() if f"{UBO_ID}/logger-ui.html" in t.get("url", "")]

    def expect_logger(how: str) -> None:
        for _ in range(40):
            pages = logger_pages()
            if pages:
                break
            time.sleep(0.5)
        else:
            raise ProbeError(f"uBO did not open its logger for the command {how}")
        for target in pages:
            try:
                cdp.send("Target.closeTarget", {"targetId": target["id"]}, timeout=10)
            except ProbeError:
                pass
        for _ in range(20):
            if not logger_pages():
                break
            time.sleep(0.25)

    commands = cdp.evaluate(companion, "chrome.commands.getAll().then(c => c.map(x => x.name))")
    log(f"COMMANDS {json.dumps(commands)}")
    if "launch-logger" not in (commands or []):
        raise ProbeError("the companion does not declare uBO's keyboard commands")
    cdp.evaluate(companion, f"""
      new Promise(resolve => chrome.runtime.sendMessage(
        '{UBO_ID}', {{what: 'command', command: 'launch-logger'}}, () => {{
          void chrome.runtime.lastError; resolve(true); }}))""")
    expect_logger("sent from a companion page")
    if os.environ.get("EXPECT_WINDOWS", "1") != "1":
        cdp.evaluate(extensions, f"""
          chrome.developerPrivate.updateExtensionCommand(
            {{extensionId: '{COMPANION_ID}', commandName: 'launch-logger',
              keybinding: 'Alt+Shift+L'}})""")
        cdp.bring_to_front(site)
        time.sleep(1)
        xdo("mousemove", "400", "500", "click", "1")
        time.sleep(0.5)
        xdo("key", "alt+shift+l")
        expect_logger("sent by a key press")
        log("COMMAND_KEY ok")
    cdp.bring_to_front(companion)


def tab_id_for(cdp: Cdp, companion: str, url_prefix: str) -> int:
    value = cdp.evaluate(companion, f"""
      new Promise(resolve => {{
        chrome.tabs.query({{}}, tabs => {{
          const hit = tabs.find(t => t.url && t.url.startsWith({json.dumps(url_prefix)}));
          resolve(hit ? hit.id : null);
        }});
      }})""")
    if not isinstance(value, int):
        raise ProbeError(f"companion could not identify the tab for {url_prefix}")
    return value


def frame_targets(cdp: Cdp, parent: str, url_fragment: str) -> list[dict]:
    """
    Return the out-of-process frames of one page whose address contains
    url_fragment. Chrome lists a frame it blocked under the address that was
    requested, so the frame of uBO's panel in the hostile page has the same
    address as the one in the companion's popup. Their parentId tells them
    apart.
    """
    parent_id = cdp.target_for(parent)
    return [t for t in cdp.targets()
            if t["type"] == "iframe" and url_fragment in t.get("url", "")
            and t.get("parentId", parent_id) == parent_id]


class Frame:
    """
    A child frame, addressed either as its own target or as an execution
    context inside the parent. The harness leaves site isolation on, so the uBO
    panel normally runs out of process and appears as an iframe target. With
    site isolation off, Chrome can keep the panel in its parent's process, where
    it exists only as an execution context.
    """

    def __init__(self, cdp: Cdp, session: str, context_id: int | None = None) -> None:
        self.cdp, self.session, self.context_id = cdp, session, context_id

    def evaluate(self, expression: str):
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if self.context_id is not None:
            params["contextId"] = self.context_id
        result = self.cdp.send("Runtime.evaluate", params, self.session)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise ProbeError(f"frame evaluate failed: {text}")
        return result.get("result", {}).get("value")


def find_frame(cdp: Cdp, parent: str, url_fragment: str, timeout: float = 45.0) -> Frame:
    deadline = time.time() + timeout
    cdp.send("Runtime.enable", {}, parent)
    while time.time() < deadline:
        # An out-of-process frame, which the panel is with site isolation on.
        for target in frame_targets(cdp, parent, url_fragment):
            if not target["url"].startswith("chrome-error://"):
                return Frame(cdp, cdp.attach(target["id"]))
        # Otherwise the frame is an execution context in the parent target.
        cdp.drain()
        for event in reversed(cdp.events):
            if event.get("method") != "Runtime.executionContextCreated":
                continue
            context = event["params"]["context"]
            if url_fragment in (context.get("origin", "") + context.get("name", "")):
                return Frame(cdp, parent, context["id"])
            aux = context.get("auxData") or {}
            if url_fragment in str(aux.get("frameId", "")):
                return Frame(cdp, parent, context["id"])
        # Fall back to the frame tree: find the child frame by URL, then the
        # context whose auxData.frameId matches that frame's ID.
        try:
            tree = cdp.send("Page.getFrameTree", {}, parent)["frameTree"]
            for child in tree.get("childFrames", []):
                if url_fragment in child["frame"].get("url", ""):
                    for event in reversed(cdp.events):
                        if event.get("method") != "Runtime.executionContextCreated":
                            continue
                        context = event["params"]["context"]
                        if (context.get("auxData") or {}).get("frameId") == child["frame"]["id"]:
                            return Frame(cdp, parent, context["id"])
        except ProbeError:
            pass
        time.sleep(0.5)
    raise ProbeError(f"frame {url_fragment} never appeared")


# --- phases -----------------------------------------------------------------


def install_phase(cdp: Cdp) -> None:
    log(f"CDP_VERSION {json.dumps(cdp.version)}")
    session = open_extensions(cdp, first_page(cdp))
    session = set_dev_mode(cdp, session)

    # Neither test extension should be present. The browser starts without
    # --load-extension, which retail Chrome ignores, so the extensions can only
    # be installed through the Load unpacked dialog, and that is what this
    # phase tests.
    preloaded = [c for c in extension_cards(cdp, session)
                 if c["id"] in (UBO_ID, COMPANION_ID)]
    if preloaded:
        desktop_shot("00-unexpected-preloaded")
        raise ProbeError(f"extensions already installed before Load unpacked: {preloaded}")

    # Companion first. uBO starts compiling filter lists the moment it loads,
    # and on a constrained host that work competes with the WebUI renderer
    # during a second picker interaction and gets the renderer killed
    # (RESULT_CODE_KILLED).
    load_unpacked(cdp, session, COMPANION_WIN, COMPANION_ID, "companion")
    load_unpacked(cdp, session, EXT_WIN, UBO_ID, "ubo")

    try:
        cards = extension_cards(cdp, session)
        log(f"EXTENSION_CARDS {json.dumps(cards)}")
        installed = {i for i in (UBO_ID, COMPANION_ID)
                     if any(c["id"] == i and c["enabled"] is not False for c in cards)}
        if len(installed) != 2:
            log(f"CARD_WARNINGS {json.dumps(card_warnings(cdp, session))}")
    except ProbeError as exc:
        log(f"EXTENSION_CARDS unavailable ({exc}); confirming at browser level")
        installed = {i for i in (UBO_ID, COMPANION_ID) if ext_running(cdp, i)}
    if len(installed) != 2:
        installed |= {i for i in (UBO_ID, COMPANION_ID) if ext_running(cdp, i)}
    if len(installed) != 2:
        desktop_shot("00-install-incomplete")
        raise ProbeError("Load unpacked did not install both extensions")
    check_extension_diagnostics(cdp, session, require_expected_warnings=True)
    # Loading by hand ends with turning on "Allow user scripts" on the
    # companion's details page. The toggle there makes this call.
    cdp.evaluate(session, f"""
      chrome.developerPrivate.updateExtensionConfiguration(
        {{extensionId: '{COMPANION_ID}', userScriptsAccess: true}})""")
    try:
        capture(cdp, session, "10-windows-load-unpacked-complete")
    except ProbeError:
        desktop_shot("10-windows-load-unpacked-complete")
    try:
        cdp.send("Browser.close", timeout=10)
    except ProbeError:
        pass


def test_phase(cdp: Cdp) -> None:
    log(f"CDP_VERSION {json.dumps(cdp.version)}")
    # The same assertions run against Linux Chrome, where the platform check
    # would be wrong, so the harness sets EXPECT_WINDOWS=0 for its Linux run.
    if os.environ.get("EXPECT_WINDOWS", "1") == "1":
        if "Windows NT" not in cdp.version.get("User-Agent", ""):
            raise ProbeError("Windows user agent missing")

    session = open_extensions(cdp, first_page(cdp))

    # The Linux run starts this Chrome with the companion's popup page open in
    # a tab. The updated worker has to take over even with that page open, and
    # the build comparison below checks that. The page loaded before uBO's
    # background page existed, and it still has to show uBO's panel once uBO
    # runs. A panel that loads too early shuts itself down and stays empty.
    # Afterwards the page is closed, because a later step looks for the toolbar
    # button's popup, which has the same address. The extensions page is open
    # in another tab by then, so the window stays open.
    left_open = [target for target in cdp.targets()
                 if target.get("type") == "page" and f"{COMPANION_ID}/" in target.get("url", "")]
    if os.environ.get("EXPECT_WINDOWS", "1") != "1":
        if len(left_open) != 1:
            raise ProbeError("Chrome does not have exactly one companion page open, the popup "
                             "page it was started with")
        early = cdp.attach(left_open[0]["id"])
        cdp.bring_to_front(early)
        panel = find_frame(cdp, early, "popup-fenix.html")
        for _ in range(120):
            if panel.evaluate("!!document.getElementById('switch') && typeof vAPI === 'object'"):
                break
            time.sleep(0.5)
        else:
            raise ProbeError("uBO's panel did not load in the page Chrome was started with")
        log("PANEL_IN_PAGE_OPEN_AT_START ok")
        cdp.bring_to_front(session)
    for target in left_open:
        try:
            cdp.send("Target.closeTarget", {"targetId": target["id"]}, timeout=10)
        except ProbeError:
            pass

    # One read is normally sufficient. On a resource-constrained host (one CPU,
    # swapping, and uBO recompiling filter lists at startup), the WebUI can
    # render before the extension system populates it, so the first read may be
    # empty. Poll for both IDs before checking that they survived the restart,
    # and keep the first read in the log so it shows whether it was empty.
    cards = extension_cards(cdp, session)
    log(f"STARTUP_EXTENSION_CARDS {json.dumps(cards)}")

    def have(ext_id: str) -> bool:
        return any(c["id"] == ext_id and c["enabled"] is not False for c in cards)

    deadline = time.monotonic() + 120
    settled = False
    while not (have(UBO_ID) and have(COMPANION_ID)) and time.monotonic() < deadline:
        time.sleep(2)
        cards = extension_cards(cdp, session)
        settled = True
    if settled:
        log(f"STARTUP_EXTENSION_CARDS_SETTLED {json.dumps(cards)}")
    for label, ext_id in (("uBO", UBO_ID), ("companion", COMPANION_ID)):
        if not have(ext_id):
            raise ProbeError(f"{label} is missing or disabled after the restart")
    capture(cdp, session, "20-extensions-page", 1.8)
    check_extension_diagnostics(
        cdp, session,
        require_expected_warnings=os.environ.get("EXPECT_WINDOWS", "1") == "1")
    # The Linux installer pins the button when it adds the companion to a
    # profile. Loading by hand, as the Windows run does, leaves the button in
    # the Extensions menu.
    if os.environ.get("EXPECT_WINDOWS", "1") != "1":
        pinned = cdp.evaluate(session, f"""
          chrome.developerPrivate.getExtensionsInfo().then(list =>
            (list.find(e => e.id === '{COMPANION_ID}') || {{}}).pinnedToToolbar)""")
        log(f"PINNED {json.dumps(pinned)}")
        if pinned is not True:
            raise ProbeError("the companion's button is not pinned to the toolbar")

    # The manifest's CSP must have exactly one frame-ancestors directive, which
    # allows 'self' and the companion.
    manifest = json.loads((Path(EXT_HOST) / "manifest.json").read_text(encoding="utf-8"))
    csp = manifest.get("content_security_policy")
    if isinstance(csp, dict):
        csp = csp.get("extension_pages", "")
    log(f"MANIFEST_CSP {json.dumps(csp)}")
    directives = [d.strip() for d in (csp or "").split(";")
                  if d.strip().startswith("frame-ancestors")]
    tokens = directives[0].split()[1:] if len(directives) == 1 else []
    if len(directives) != 1 or sorted(tokens) != sorted(
            ["'self'", f"chrome-extension://{COMPANION_ID}"]):
        raise ProbeError("manifest CSP lacks the expected frame-ancestors directive")

    # uBO's dashboard serves as the extension page used to query the background
    # page.
    time.sleep(12)
    dashboard = cdp.new_page()
    # CDP can report a navigation as complete when it ended on an error page.
    # Such a document has no chrome.runtime, so explicitly verify the extension
    # APIs.
    for attempt in range(8):
        try:
            cdp.navigate(dashboard, f"chrome-extension://{UBO_ID}/dashboard.html")
            landed = cdp.evaluate(dashboard, """
              JSON.stringify({url: location.href,
                              api: typeof chrome !== 'undefined' && !!chrome.runtime})""")
            state = json.loads(landed)
            if state["api"] and UBO_ID in state["url"]:
                break
            log(f"DASHBOARD_RETRY {attempt + 1}: {landed}")
        except ProbeError as exc:
            log(f"DASHBOARD_RETRY {attempt + 1}: {exc}")
        if attempt == 7:
            desktop_shot("00-dashboard-unreachable")
            raise ProbeError("uBO's dashboard never loaded with extension APIs")
        time.sleep(2)
    wait_for_ubo_ready(cdp, dashboard)
    title = cdp.evaluate(dashboard, "document.title")
    log(f"DASHBOARD title={json.dumps(title)}")
    if "Dashboard" not in title:
        raise ProbeError("uBO dashboard title is unexpected")
    capture(cdp, dashboard, "30-ubo-dashboard", 2.2)

    # Blocking: the canary must never reach the local server.
    port = start_fixtures()
    site = cdp.new_page()
    cdp.navigate(site, f"http://127.0.0.1:{port}/block")
    time.sleep(3.5)

    companion = cdp.new_page()
    cdp.navigate(companion, f"chrome-extension://{COMPANION_ID}/popup.html")
    site_tab = tab_id_for(cdp, companion, f"http://127.0.0.1:{port}/block")

    # uBO's panel waits on requestAnimationFrame, which Chrome suspends in a
    # hidden tab, so the companion's tab must be in front for uBO's panel to
    # finish rendering.
    cdp.bring_to_front(companion)
    panel = find_frame(cdp, companion, "popup-fenix.html")
    for _ in range(60):
        if panel.evaluate("!!document.getElementById('switch')"):
            break
        time.sleep(0.5)
    else:
        raise ProbeError("uBO's power switch never appeared in the companion popup")

    size = json.loads(cdp.evaluate(companion, """
      (s => JSON.stringify({width: s.width, height: s.height}))
        (getComputedStyle(document.getElementById('ubo')))"""))
    badge: dict = {}
    for _ in range(40):
        badge = bridge_message(cdp, companion, site_tab)
        if badge.get("ready") and badge.get("pageBlocked", 0) >= 1:
            break
        time.sleep(0.5)
    log(f"BLOCKING_AND_BADGE size={json.dumps(size)}"
        f" canaryHits={Fixtures.canary_hits} response={json.dumps(badge)}")
    if Fixtures.canary_hits != 0:
        raise ProbeError(f"advertising canary reached the server {Fixtures.canary_hits} time(s)")
    if not (badge.get("ok") and badge.get("ready") and badge.get("netFiltering")
            and badge.get("pageBlocked", 0) >= 1):
        raise ProbeError("uBO's companion bridge did not report the request as blocked")
    capture(cdp, companion, "40-companion-popup-document-in-tab", 1.5)

    # The worker and uBO's background page have to report the build that is
    # on disk. Chrome loaded both at its start, and it reads the popup's files
    # from disk when the popup opens. The popup compares the builds that the
    # two report with its own, and shows the notice "uBlock Origin was
    # updated" when one differs. After an update, the builds differ until
    # Chrome restarts.
    builds = json.loads(cdp.evaluate(companion, f"""
      Promise.all([
        chrome.runtime.sendMessage({{what: 'build'}}),
        new Promise(resolve => chrome.runtime.sendMessage(
          '{UBO_ID}', {{what: 'uboBadge'}},
          r => resolve(chrome.runtime.lastError ? null : r.build))),
      ]).then(a => JSON.stringify({{
        worker: a[0], ubo: a[1],
        notices: document.getElementById('notices').innerText}}))"""))
    log(f"BUILD {json.dumps(builds)}")
    if (not builds["worker"] or builds["worker"] != builds["ubo"]
            or "uBlock Origin was updated" in builds["notices"]):
        raise ProbeError("Chrome is not running the build that is on disk")
    if builds["notices"]:
        raise ProbeError(f"the popup shows an unexpected notice: {builds['notices']!r}")

    # The button has to show uBO's count of blocked requests for the tab, as
    # its badge and in its title.
    toolbar: list = []
    for _ in range(40):
        blocked = str(bridge_message(cdp, companion, site_tab).get("pageBlocked"))
        toolbar = json.loads(cdp.evaluate(companion, f"""
          Promise.all([chrome.action.getBadgeText({{tabId: {site_tab}}}),
                       chrome.action.getTitle({{tabId: {site_tab}}})])
            .then(a => JSON.stringify(a))"""))
        if toolbar == [blocked, f"uBlock Origin ({blocked})"]:
            break
        time.sleep(0.5)
    else:
        log(f"TOOLBAR_STATE {json.dumps(toolbar)}")
        raise ProbeError("the toolbar button does not show uBO's count for the tab")
    log(f"TOOLBAR_STATE {json.dumps(toolbar)}")

    check_commands(cdp, session, companion, site)

    # showIconBadge is honoured by the bridge.
    original = set_show_badge(cdp, dashboard, False)
    time.sleep(0.3)
    badge_off = bridge_message(cdp, companion, site_tab)
    set_show_badge(cdp, dashboard, original)
    log(f"SHOW_BADGE_FALSE response={json.dumps(badge_off)} restored={json.dumps(original)}")
    if badge_off.get("showBadge") is not False:
        raise ProbeError("the companion bridge ignored showIconBadge=false")

    # frame-ancestors: an ordinary HTTP origin must not be able to frame uBO's
    # popup.
    hostile = cdp.new_page()
    cdp.navigate(hostile, f"http://127.0.0.1:{port}/hostile")
    hostile_tab = tab_id_for(cdp, companion, f"http://127.0.0.1:{port}/hostile")
    cdp.evaluate(hostile, f"""
      document.getElementById('victim').src =
        'chrome-extension://{UBO_ID}/popup-fenix.html?tabId={hostile_tab}'""")
    time.sleep(4.5)
    switch_reachable = 0
    hostile_frames = []
    try:
        victim = find_frame(cdp, hostile, "popup-fenix.html", timeout=8)
        hostile_frames.append("frame present")
        if victim.evaluate("!!document.getElementById('switch')"):
            switch_reachable += 1
    except ProbeError:
        pass
    log(f"CSP_HOSTILE_FRAME framed={bool(hostile_frames)} switch={switch_reachable}")
    if switch_reachable:
        desktop_shot("00-clickjacking-possible")
        raise ProbeError("hostile parent can still reach the uBO power switch")
    cdp.evaluate(hostile, """
      document.getElementById('result').textContent =
        'PASS: Chrome blocked the extension document before it rendered.'""")
    capture(cdp, hostile, "50-csp-hostile-frame-blocked", 0.7)

    # Positive control: the allowlisted companion can still embed it afterwards.
    cdp.navigate(companion, f"chrome-extension://{COMPANION_ID}/popup.html")
    cdp.bring_to_front(companion)
    find_frame(cdp, companion, "popup-fenix.html")
    log("CSP_POSITIVE_CONTROL ok")

    # Next comes the action popup, which the browser opens itself. The
    # popup.html tab used above proves that the document works, but not that
    # the toolbar button opens it. That tab is closed first so the target
    # search below cannot match it instead.
    cdp.close_page(companion)
    time.sleep(1)
    worker = next((t for t in cdp.targets()
                   if t["type"] == "service_worker" and COMPANION_ID in t.get("url", "")), None)
    if worker is None:
        raise ProbeError("the companion service worker is not running")
    worker_session = cdp.attach(worker["id"])
    try:
        cdp.evaluate(worker_session, "chrome.action.openPopup()")
    except ProbeError as exc:
        log(f"TOOLBAR_POPUP openPopup rejected: {exc}")
    popup_target = None
    for _ in range(40):
        popup_target = next(
            (t for t in cdp.targets()
             if f"{COMPANION_ID}/popup.html" in t.get("url", "")), None)
        if popup_target:
            break
        time.sleep(0.5)
    if popup_target is None:
        desktop_shot("00-toolbar-popup-missing")
        raise ProbeError("the toolbar button did not open the companion action popup")
    log(f"TOOLBAR_POPUP {json.dumps({'type': popup_target['type'], 'url': popup_target['url'][:60]})}")
    toolbar_session = cdp.attach(popup_target["id"])
    toolbar_panel = find_frame(cdp, toolbar_session, "popup-fenix.html")
    # The frame exists before uBO's panel has rendered into it; under Wine
    # that takes a few seconds.
    for _ in range(40):
        if toolbar_panel.evaluate("!!document.getElementById('switch')"):
            break
        time.sleep(0.5)
    else:
        desktop_shot("00-toolbar-popup-empty")
        raise ProbeError("the toolbar popup opened without uBO's panel")
    log("TOOLBAR_POPUP_PANEL ok")
    time.sleep(2)
    desktop_shot("55-toolbar-action-popup")

    # The scriptlet check runs after the popup checks. It opens and closes
    # tabs, and that can leave Chrome without a focused window for
    # openPopup().
    check_scriptlets(cdp, port)
    check_launch_guard(cdp, port)
    # The side panel check runs last, because it opens uBO's logger, and an
    # open logger changes the code of uBO's scriptlets.
    check_side_panel(cdp)

    (OUT_DIR / "targets-final.json").write_text(
        json.dumps(cdp.targets(), indent=2), encoding="utf-8")
    desktop_shot("60-final-desktop")
    try:
        cdp.send("Browser.close", timeout=10)
    except ProbeError:
        pass


# --- main -------------------------------------------------------------------

ENDPOINT = os.environ.get("CDP_ENDPOINT", "")
OUT_DIR = Path(os.environ.get("OUT_DIR") or os.environ.get("SCREENSHOT_DIR") or ".")
XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
IMPORT_BIN = os.environ.get("IMPORT_BIN", "import")
UBO_ID = os.environ.get("UBO_ID", "")
COMPANION_ID = os.environ.get("COMPANION_ID", "")
EXT_WIN = os.environ.get("EXT_WIN", "")
COMPANION_WIN = os.environ.get("COMPANION_WIN", "")
EXT_HOST = os.environ.get("EXT_HOST", "")
WINE_BIN = os.environ.get("WINE_BIN", "wine")
PYTHON_EXE = os.environ.get("PYTHON_EXE", "")
PICKER_HELPER_WIN = os.environ.get("PICKER_HELPER_WIN", "")


def main() -> int:
    for name in ("CDP_ENDPOINT", "UBO_ID", "COMPANION_ID"):
        if not os.environ.get(name):
            print(f"error: {name} is required", file=sys.stderr)
            return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get("PROBE_MODE")
    try:
        cdp = Cdp(ENDPOINT)
        if mode == "install":
            install_phase(cdp)
        elif mode == "test":
            test_phase(cdp)
        else:
            print(f"error: unknown PROBE_MODE: {mode}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        # Several call sites raise the same timeout message, so the traceback is
        # printed too, to show which one failed.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        desktop_shot("00-failure-state")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
