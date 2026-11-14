"""
Test the Windows installer on a real Windows machine (the GitHub Actions
Windows runner): install, the scheduled task that updates it, an update while
Chrome has both extensions loaded, and uninstall.

Chrome for Testing loads the extensions with --load-extension, which retail
Chrome ignores. That is enough here: the update only has to replace the files
under a Chrome that holds them, and Windows refuses to rename a directory while
a file in it is open.

Usage: python tests/windows/run.py   (needs the cryptography package)
Logs go to $UBO_OUT, or to ubo-lpa-windows/out in the temp directory.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ubo-lpa.py"
WORK = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ubo-lpa-windows"
OUT = Path(os.environ.get("UBO_OUT") or WORK / "out")
ROOT = WORK / "install"
CFT_INDEX = ("https://googlechromelabs.github.io/chrome-for-testing/"
             "last-known-good-versions-with-downloads.json")


class Failure(RuntimeError):
    pass


def note(msg: str) -> None:
    print(f"[windows] {msg}", flush=True)


def run(name: str, argv: list[str], expect: int | None = 0, timeout: int = 600) -> str:
    """Run a command, keep its output in OUT/<name>.log, check its exit status."""
    note(f"$ {subprocess.list2cmdline(argv)}")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    output = result.stdout + result.stderr
    (OUT / f"{name}.log").write_text(output, encoding="utf-8")
    print(output, flush=True)
    if expect is not None and result.returncode != expect:
        raise Failure(f"{name}: exit {result.returncode}, expected {expect}")
    return output


def ubo_lpa(name: str, *args: str, expect: int | None = 0) -> str:
    return run(name, [sys.executable, str(SCRIPT), *args, "--dir", str(ROOT)], expect)


def installer_module():
    spec = importlib.util.spec_from_file_location("ubo_lpa", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_fields(task: str) -> dict:
    """The verbose schtasks /Query row for the task, as a dict."""
    output = subprocess.run(["schtasks", "/Query", "/TN", task, "/V", "/FO", "CSV"],
                            capture_output=True, text=True).stdout
    rows = list(csv.DictReader(io.StringIO(output)))
    return rows[0] if rows else {}


def test_install() -> None:
    output = ubo_lpa("install", "install", "--non-interactive")
    if "has to be completed manually" not in output:
        raise Failure("a first install did not print the manual steps")
    ubo_lpa("check", "check")


def test_timer(task: str) -> None:
    ubo_lpa("timer", "timer")
    xml = run("task-definition", ["schtasks", "/Query", "/TN", task, "/XML"])
    command = re.search(r"<Command>(.*?)</Command>", xml, re.S)
    arguments = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S)
    if not command or not command.group(1).lower().endswith("pythonw.exe"):
        raise Failure(f"the task does not run pythonw.exe: {command and command.group(1)}")
    if not arguments or f'--dir {ROOT}' not in arguments.group(1).replace('"', ""):
        raise Failure(f"the task does not name the install directory: {arguments and arguments.group(1)}")
    for setting in ("<CalendarTrigger>", "<StartWhenAvailable>true</StartWhenAvailable>",
                    "<LogonType>InteractiveToken</LogonType>"):
        if setting not in xml:
            raise Failure(f"the task definition lacks {setting}")

    # Run the task as Task Scheduler would, and wait for it to finish.
    log = ROOT / "update.log"
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    run("task-run", ["schtasks", "/Run", "/TN", task])
    deadline = time.time() + 300
    while time.time() < deadline:
        fields = task_fields(task)
        after = log.read_text(encoding="utf-8") if log.exists() else ""
        if len(after) > len(before) and fields.get("Status") == "Ready":
            break
        time.sleep(3)
    else:
        raise Failure(f"the scheduled run did not finish: {task_fields(task)}")
    (OUT / "task-fields.json").write_text(json.dumps(task_fields(task), indent=1))
    (OUT / "update.log").write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
    new = log.read_text(encoding="utf-8")[len(before):]
    note(f"update.log from the scheduled run:\n{new}")
    if fields.get("Last Result") != "0":
        raise Failure(f"the scheduled run ended with Last Result {fields.get('Last Result')}")
    if "No changes made." not in new:
        raise Failure("the scheduled run did not log a completed install")


def chrome_for_testing() -> Path:
    with urllib.request.urlopen(CFT_INDEX, timeout=60) as response:
        index = json.load(response)
    stable = index["channels"]["Stable"]
    url = next(d["url"] for d in stable["downloads"]["chrome"] if d["platform"] == "win64")
    note(f"Chrome for Testing {stable['version']}")
    archive = WORK / "chrome-win64.zip"
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(WORK / "cft")
    return WORK / "cft" / "chrome-win64" / "chrome.exe"


def targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
        return json.load(r)


def test_update_while_chrome_runs(ubo_id: str, comp_id: str) -> None:
    chrome = chrome_for_testing()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    log = (OUT / "chrome.log").open("w")
    # The default user data directory, so the installer's check for loaded
    # extensions finds this profile as it finds a user's.
    user_data = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
    process = subprocess.Popen([
        str(chrome), "--headless=new", f"--user-data-dir={user_data}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        f"--remote-debugging-port={port}",
        f"--load-extension={ROOT / 'extension'},{ROOT / 'companion'}",
        "about:blank",
    ], stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 120
        seen: list[dict] = []
        while time.time() < deadline:
            try:
                seen = targets(port)
            except OSError:
                seen = []
            urls = [t.get("url", "") for t in seen]
            if (any(u.startswith(f"chrome-extension://{ubo_id}/") for u in urls)
                    and any(u.startswith(f"chrome-extension://{comp_id}/") for u in urls)):
                break
            time.sleep(1)
        else:
            raise Failure(f"Chrome did not load both extensions: {[t.get('url') for t in seen]}")
        note("Chrome has both extensions running: "
             + ", ".join(t["url"] for t in seen if t.get("url", "").startswith("chrome-extension://")))
        # Let uBO finish reading its bundled filter lists, so the update meets
        # the files Chrome keeps open, not only the ones it is reading. Chrome
        # also writes its preferences to disk within this time.
        time.sleep(20)

        marker = ROOT / "extension" / "replaced-by-update"
        marker.write_text("present before the update")
        state_file = ROOT / ".state.json"
        state = json.loads(state_file.read_text())
        state["version"] = "0.0.0"
        state_file.write_text(json.dumps(state))
        output = ubo_lpa("update-while-running", "install", "--non-interactive")
        if "at its next start" not in output:
            raise Failure("the update did not see that Chrome has both extensions loaded")
        if marker.exists():
            raise Failure("install exited 0 but did not replace the extension directory")
        if process.poll() is not None:
            raise Failure(f"Chrome exited during the update with {process.returncode}")
        urls = [t.get("url", "") for t in targets(port)]
        if not any(u.startswith(f"chrome-extension://{ubo_id}/") for u in urls):
            raise Failure(f"uBO stopped running during the update: {urls}")
        note("The update replaced the files while Chrome kept running uBO.")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        process.wait(timeout=60)
        log.close()


def test_uninstall(task: str) -> None:
    ubo_lpa("uninstall", "uninstall")
    if subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True).returncode == 0:
        raise Failure(f"uninstall left the scheduled task {task}")
    if ROOT.exists():
        raise Failure(f"uninstall left {ROOT}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    module = installer_module()
    if not module.IS_WINDOWS:
        raise SystemExit("run this on Windows")
    task = module.WindowsInstaller(ROOT).task_name
    note(f"install directory {ROOT}, scheduled task {task}")
    try:
        test_install()
        state = json.loads((ROOT / ".state.json").read_text())
        test_timer(task)
        test_update_while_chrome_runs(state["ext_id"], state["comp_id"])
        test_uninstall(task)
    except Failure as exc:
        note(f"FAIL: {exc}")
        return 1
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"], capture_output=True)
    note("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
