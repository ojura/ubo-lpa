"""
Test ubo-lpa.py on a real Windows machine (the GitHub Actions Windows runner),
with the retail Chrome installed there: install, the scheduled task that
updates the install, load with a click on Allow in Chrome's prompt, an update
while Chrome has both extensions loaded, and uninstall.

UI Automation clicks Allow, as a user would (tests/windows/uia.py). Chrome
runs on its default user data directory, which a first run of Chrome on the
runner creates.

Usage: python tests/windows/run.py   (needs cryptography, pywinauto, Pillow)
Logs and screenshots go to $UBO_OUT, or to ubo-lpa-windows/out in the temp
directory.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from uia import AllowClicker, note, screenshot  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ubo-lpa.py"
WORK = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ubo-lpa-windows"
OUT = Path(os.environ.get("UBO_OUT") or WORK / "out")
ROOT = WORK / "install"
USER_DATA = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


class Failure(RuntimeError):
    pass


def run(name: str, argv: list[str], expect: int | None = 0, timeout: int = 600) -> str:
    """Run a command, keep its output in OUT/<name>.log, check its exit status."""
    note(f"$ {subprocess.list2cmdline(argv)}")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
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


def chrome_running() -> bool:
    output = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                            capture_output=True, text=True).stdout
    return "chrome.exe" in output


def close_chrome() -> None:
    """Close Chrome as its window's close button does, so it writes its settings."""
    subprocess.run(["taskkill", "/im", "chrome.exe"], capture_output=True)
    deadline = time.time() + 60
    while chrome_running() and time.time() < deadline:
        time.sleep(1)
    if chrome_running():
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
        time.sleep(3)


def first_run_done() -> None:
    """Create the First Run marker, which a user's Chrome has; without it,
    Chrome shows its first-run pages."""
    USER_DATA.mkdir(parents=True, exist_ok=True)
    (USER_DATA / "First Run").touch()


def test_install() -> None:
    output = ubo_lpa("install", "install", "--non-interactive")
    if "ubo-lpa.py load" not in output:
        raise Failure("a first install did not point at ubo-lpa.py load")
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
        raise Failure('the scheduled run did not log "No changes made."')


def test_load(module, ubo_id: str, comp_id: str) -> None:
    """load with Chrome closed: it switches remote debugging on, starts Chrome,
    loads both folders after the click on Allow, and switches it off again."""
    close_chrome()
    first_run_done()
    with AllowClicker(OUT) as clicker:
        output = ubo_lpa("load", "load")
    screenshot(OUT / "after-load.png")
    if clicker.prompts != 1:
        raise Failure(f"expected one remote debugging prompt, Chrome showed {clicker.prompts}")
    for expected in ("running in Chrome", "Turned remote debugging off again"):
        if expected not in output:
            raise Failure(f"load did not report: {expected}")

    # Chrome writes its settings when it closes; read what it kept.
    close_chrome()
    loaded = module.WindowsInstaller(ROOT).loaded_in(ubo_id, comp_id)
    if not loaded:
        raise Failure("Chrome's preferences do not list both extensions from the install directory")
    secure = json.loads((USER_DATA / "Default" / "Secure Preferences").read_text(encoding="utf-8"))
    prefs = json.loads((USER_DATA / "Default" / "Preferences").read_text(encoding="utf-8"))
    settings = {**prefs.get("extensions", {}).get("settings", {}),
                **secure.get("extensions", {}).get("settings", {})}
    for ext_id in (ubo_id, comp_id):
        entry = settings[ext_id]
        if entry.get("location") != 4 or entry.get("disable_reasons"):
            raise Failure(f"{ext_id}: location {entry.get('location')}, "
                          f"disable_reasons {entry.get('disable_reasons')}")
    pinned = prefs.get("extensions", {}).get("pinned_extensions", [])
    if comp_id not in pinned:
        raise Failure(f"the toolbar button is not pinned: {pinned}")
    # Chrome on Windows keeps protected settings, developer mode among them,
    # in Secure Preferences.
    developer_mode = [name for name, data in (("Secure Preferences", secure), ("Preferences", prefs))
                      if data.get("extensions", {}).get("ui", {}).get("developer_mode") is True]
    if not developer_mode:
        raise Failure("developer mode is off in both Secure Preferences and Preferences")
    note(f"developer mode is on, in {developer_mode[0]}")
    local_state = json.loads((USER_DATA / "Local State").read_text(encoding="utf-8"))
    if local_state.get("devtools", {}).get("remote_debugging", {}).get("user-enabled"):
        raise Failure("remote debugging is still on")
    note(f"Chrome kept both extensions, loaded and enabled, in {', '.join(loaded)}")

    output = ubo_lpa("load-again", "load")
    if "already loaded" not in output:
        raise Failure("a second load did not find both loaded")


def test_update_while_chrome_runs() -> None:
    """Replace the files while retail Chrome runs with both extensions loaded.
    Windows refuses to rename a directory while a file in it is open."""
    chrome = subprocess.Popen([r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                               "https://example.com/"])
    try:
        time.sleep(20)
        screenshot(OUT / "chrome-before-update.png")
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
        if not chrome_running():
            raise Failure("Chrome exited during the update")
        note("The update replaced the files while Chrome ran with both extensions loaded.")
    finally:
        close_chrome()
        chrome.wait(timeout=60)


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
        test_load(module, state["ext_id"], state["comp_id"])
        test_update_while_chrome_runs()
        test_uninstall(task)
    except Failure as exc:
        note(f"FAIL: {exc}")
        screenshot(OUT / "failure.png")
        return 1
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"], capture_output=True)
    note("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
