"""
End-to-end test of the MSI on a fresh Windows runner with retail Chrome:
install through the setup wizard, with a click on Allow when Setup loads
uBlock Origin into Chrome; the files, Start menu, Apps & features entry and
scheduled task; Chrome's record of both extensions; an upgrade that keeps the
extension IDs; and uninstall.

Usage: python tests/windows/msi_test.py FIRST.msi SECOND.msi
(SECOND has the higher version.) Needs pywinauto and Pillow. Screenshots of
every wizard page and the logs go to $UBO_OUT.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from uia import AllowClicker, note, screenshot  # noqa: E402

OUT = Path(os.environ.get("UBO_OUT") or Path(tempfile.gettempdir()) / "ubo-lpa-msi")
LOCAL = Path(os.environ["LOCALAPPDATA"])
PROGRAM = LOCAL / "Programs" / "ubo-lpa"
ROOT = LOCAL / "uBOLPA"
USER_DATA = LOCAL / "Google" / "Chrome" / "User Data"
MENU = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/uBlock Origin for Chrome"
NAME = "uBlock Origin for Chrome (ubo-lpa)"
TASK = "ubo-lpa-update"


class Failure(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def arp_entry() -> dict | None:
    """The Apps & features entry, from the per-user or machine uninstall keys."""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                    try:
                        if winreg.QueryValueEx(key, "DisplayName")[0] == NAME:
                            return {n: winreg.QueryValueEx(key, n)[0]
                                    for n in ("DisplayName", "DisplayVersion", "Publisher")}
                    except OSError:
                        continue
    return None


def task_fields() -> dict:
    output = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "CSV"],
                            capture_output=True, text=True).stdout
    rows = list(csv.DictReader(io.StringIO(output)))
    return rows[0] if rows else {}


def chrome_running() -> bool:
    return "chrome.exe" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                                          capture_output=True, text=True).stdout


def close_chrome() -> None:
    subprocess.run(["taskkill", "/im", "chrome.exe"], capture_output=True)
    deadline = time.time() + 60
    while chrome_running() and time.time() < deadline:
        time.sleep(1)
    if chrome_running():
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
        time.sleep(3)


def state() -> dict:
    return json.loads((ROOT / ".state.json").read_text())


def install_with_wizard(msi: Path) -> None:
    """Click through the wizard, with a screenshot of every page."""
    from pywinauto import Application
    log = OUT / "install.log"
    process = subprocess.Popen(["msiexec", "/i", str(msi), "/l*v", str(log)])
    app = Application(backend="win32").connect(title=f"{NAME} Setup", timeout=120)
    wizard = app.window(title=f"{NAME} Setup")
    wizard.wait("visible ready", timeout=120)
    time.sleep(2)
    wizard.capture_as_image().save(OUT / "wizard-1-welcome.png")
    wizard["Next"].click()
    time.sleep(2)
    wizard.capture_as_image().save(OUT / "wizard-2-ready.png")
    with AllowClicker(OUT):
        wizard["Install"].click()
        time.sleep(4)
        wizard.capture_as_image().save(OUT / "wizard-3-progress.png")
        deadline = time.time() + 900
        while time.time() < deadline:
            finish = wizard.child_window(title="&Finish", class_name="Button")
            if finish.exists() and finish.is_enabled():
                break
            time.sleep(2)
        else:
            raise Failure("the wizard did not reach its Finish page")
    time.sleep(2)
    wizard.capture_as_image().save(OUT / "wizard-4-finish.png")
    screenshot(OUT / "desktop-after-install.png")
    wizard["Finish"].click()
    check(process.wait(timeout=120) == 0, f"msiexec /i exited with {process.returncode}")


def verify_install(version: str) -> None:
    for path in (PROGRAM / "ubo-lpa.py", PROGRAM / "python" / "pythonw.exe",
                 PROGRAM / "assets", ROOT / "extension" / "manifest.json",
                 ROOT / "companion" / "manifest.json"):
        check(path.exists(), f"missing {path}")
    for name in ("Load uBlock Origin into Chrome.lnk", "Update uBlock Origin now.lnk",
                 "uBlock Origin update log.lnk"):
        check((MENU / name).exists(), f"missing Start menu entry {name}")
    entry = arp_entry()
    check(entry is not None and entry["DisplayVersion"] == version,
          f"Apps & features entry: {entry}")
    xml = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"],
                         capture_output=True, text=True).stdout
    check(str(PROGRAM / "python" / "pythonw.exe").lower() in xml.lower(),
          "the scheduled task does not run the installed pythonw.exe")
    note(f"installed {version}: files, Start menu, Apps & features and the task are in place")


def verify_chrome(ubo_id: str, comp_id: str) -> None:
    close_chrome()
    secure = json.loads((USER_DATA / "Default" / "Secure Preferences").read_text(encoding="utf-8"))
    prefs = json.loads((USER_DATA / "Default" / "Preferences").read_text(encoding="utf-8"))
    settings = {**prefs.get("extensions", {}).get("settings", {}),
                **secure.get("extensions", {}).get("settings", {})}
    for ext_id, folder in ((ubo_id, ROOT / "extension"), (comp_id, ROOT / "companion")):
        entry = settings.get(ext_id) or {}
        check(os.path.normcase(entry.get("path", "")) == os.path.normcase(str(folder)),
              f"{ext_id} is not loaded from {folder}: {entry.get('path')}")
        check(entry.get("location") == 4 and not entry.get("disable_reasons"),
              f"{ext_id}: location {entry.get('location')}, disable_reasons {entry.get('disable_reasons')}")
    check(comp_id in prefs.get("extensions", {}).get("pinned_extensions", []),
          "the toolbar button is not pinned")
    remote = json.loads((USER_DATA / "Local State").read_text(encoding="utf-8"))
    check(not remote.get("devtools", {}).get("remote_debugging", {}).get("user-enabled"),
          "remote debugging is still on")
    note("Chrome kept both extensions, loaded from the install and enabled")


def run_task() -> None:
    log = ROOT / "update.log"
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    subprocess.run(["schtasks", "/Run", "/TN", TASK], check=True, capture_output=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        fields = task_fields()
        after = log.read_text(encoding="utf-8") if log.exists() else ""
        if len(after) > len(before) and fields.get("Status") == "Ready":
            break
        time.sleep(3)
    check(task_fields().get("Last Result") == "0", f"the scheduled run: {task_fields()}")
    note("the scheduled update ran with Last Result 0")


def main() -> int:
    first, second = (Path(a).resolve() for a in sys.argv[1:3])
    version = lambda msi: msi.stem.split("-")[-1]  # noqa: E731
    OUT.mkdir(parents=True, exist_ok=True)
    USER_DATA.mkdir(parents=True, exist_ok=True)
    (USER_DATA / "First Run").touch()  # a user's Chrome has this marker
    try:
        install_with_wizard(first)
        verify_install(version(first))
        ids = state()["ext_id"], state()["comp_id"]
        verify_chrome(*ids)
        run_task()

        note(f"upgrading to {version(second)}")
        result = subprocess.run(["msiexec", "/i", str(second), "/qn", "/l*v",
                                 str(OUT / "upgrade.log")], timeout=900)
        check(result.returncode == 0, f"the upgrade exited with {result.returncode}")
        verify_install(version(second))
        check((state()["ext_id"], state()["comp_id"]) == ids, "the upgrade changed the extension IDs")
        verify_chrome(*ids)

        result = subprocess.run(["msiexec", "/x", str(second), "/qn", "/l*v",
                                 str(OUT / "uninstall.log")], timeout=900)
        check(result.returncode == 0, f"the uninstall exited with {result.returncode}")
        check(not ROOT.exists(), f"uninstall left {ROOT}")
        check(not PROGRAM.exists(), f"uninstall left {PROGRAM}: {list(PROGRAM.rglob('*'))[:10]}")
        check(not MENU.exists(), "uninstall left the Start menu folder")
        check(arp_entry() is None, "uninstall left the Apps & features entry")
        check(not task_fields(), "uninstall left the scheduled task")
        note("uninstall removed the program, the extension folders, the menu, the entry and the task")
    except Failure as exc:
        note(f"FAIL: {exc}")
        screenshot(OUT / "failure.png")
        return 1
    note("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
