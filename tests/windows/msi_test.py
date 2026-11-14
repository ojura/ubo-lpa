"""
End-to-end test of the MSI on a fresh Windows runner with retail Chrome:

- a first install through every page of the setup wizard, with a click on
  Allow when Setup loads uBlock Origin into Chrome; the files, the Start menu,
  the entry in the list of installed apps, the scheduled task, and Chrome's
  record of both extensions;
- the maintenance page, opened from the Start menu: Change settings turns the
  daily update off, Update now keeps it off, and so does a silent upgrade,
  which also keeps the extension IDs;
- uninstall;
- an install without maintenance, of the bundled uBO release into another
  folder, which leaves nothing but that folder.

Usage: python tests/windows/msi_test.py FIRST.msi SECOND.msi
(SECOND has the higher version.) Needs pywinauto and Pillow. Screenshots of
every wizard page go to $UBO_OUT, with chrome.txt, the Chrome the wizard found.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
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
PORTABLE = LOCAL / "uBOLPA-portable"
USER_DATA = LOCAL / "Google" / "Chrome" / "User Data"
MENU = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/uBlock Origin for Chrome"
MAINTENANCE_LNK = MENU / "Update, change or remove uBlock Origin for Chrome.lnk"
NAME = "uBlock Origin for Chrome (ubo-lpa)"
TITLE = f"{NAME} Setup"
TASK = "ubo-lpa-update"


class Failure(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def arp_entry() -> dict | None:
    """The entry in the list of installed apps, from the per-user or machine uninstall keys."""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                code = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, code) as key:
                    try:
                        if winreg.QueryValueEx(key, "DisplayName")[0] == NAME:
                            entry = {n: winreg.QueryValueEx(key, n)[0]
                                     for n in ("DisplayName", "DisplayVersion", "Publisher")}
                            return {**entry, "ProductCode": code}
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


def chrome_exe() -> str:
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        candidate = Path(os.environ.get(base, "")) / "Google/Chrome/Application/chrome.exe"
        if os.environ.get(base) and candidate.is_file():
            return str(candidate)
    raise Failure("Google Chrome is not installed")


def seed_profile() -> None:
    """Run Chrome once, as a user has before installing, so it has created
    its Default profile and recorded it in Local State."""
    subprocess.Popen([chrome_exe(), "--no-first-run", "--no-default-browser-check", "about:blank"])
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            local_state = json.loads((USER_DATA / "Local State").read_text(encoding="utf-8"))
            if "Default" in local_state.get("profile", {}).get("info_cache", {}):
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    time.sleep(5)
    close_chrome()
    note("Chrome ran once and created its Default profile")


def close_chrome() -> None:
    subprocess.run(["taskkill", "/im", "chrome.exe"], capture_output=True)
    deadline = time.time() + 60
    while chrome_running() and time.time() < deadline:
        time.sleep(1)
    if chrome_running():
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
        time.sleep(3)


def state(root: Path = ROOT) -> dict:
    return json.loads((root / ".state.json").read_text())


# --- the wizard ------------------------------------------------------------


def wizard():
    from pywinauto import Application
    app = Application(backend="win32").connect(title=TITLE, timeout=120)
    window = app.window(title=TITLE)
    window.wait("visible ready", timeout=120)
    return window


def page(window, shot: str, wait: float = 2) -> list[str]:
    """Screenshot the page shown now; return the texts of its controls."""
    time.sleep(wait)
    window.capture_as_image().save(OUT / f"{shot}.png")
    texts = [c.window_text() for c in window.wrapper_object().children()]
    note(f"{shot}: {' | '.join(t for t in texts if t)[:400]}")
    return texts


def control(window, text, cls: str | None = "Button", timeout: int = 300):
    """
    The visible, enabled control of this class (any class for None) whose
    text is text (or matches it, a compiled pattern). Before the first page, and while a page's custom
    action runs, Windows Installer shows the buttons disabled. WiX's Ready page
    has two of some buttons, with and without the UAC shield, and shows one;
    pywinauto's own lookup counts hidden controls too. A radio button is a
    child of its group's control, so all descendants are searched.
    """
    deadline = time.time() + timeout
    while True:
        try:
            for child in window.wrapper_object().descendants():
                label = child.window_text()
                matches = text.fullmatch(label) if hasattr(text, "fullmatch") else label == text
                if ((cls is None or child.class_name() == cls) and matches
                        and child.is_visible() and child.is_enabled()):
                    return child
        except Exception:  # the page is being replaced
            pass
        if time.time() > deadline:
            seen = []
            try:
                seen = [f"{c.class_name()}:{c.window_text()[:40]}"
                        for c in window.wrapper_object().descendants() if c.is_visible()]
            except Exception:
                pass
            raise Failure(f"no visible, enabled {cls or 'control'} {text!r}; the page has {seen}")
        time.sleep(0.5)


def click(window, text, timeout: int = 300) -> None:
    control(window, text, timeout=timeout).click()


def toggle(window, text) -> None:
    """Flip a checkbox, or select a radio button, and wait for its new state.
    BM_CLICK is sent, not posted, and makes the control tell the dialog, which
    sets the property."""
    box = control(window, text)
    before = box.get_check_state()
    box.send_message(0x00F5)  # BM_CLICK
    deadline = time.time() + 10
    while box.get_check_state() == before and time.time() < deadline:
        time.sleep(0.25)
    check(box.get_check_state() != before, f"{text} did not change")


def first_page(window, button: str) -> None:
    """Wait for the first page: until then Windows Installer shows its
    preparing page, here while UboProbe runs ubo-lpa.py setup-info."""
    control(window, button)


def wait_for_finish(window, minutes: int = 15) -> None:
    control(window, "&Finish", timeout=minutes * 60)


def install_with_wizard(msi: Path) -> None:
    """A first install through every page, with a screenshot of each."""
    log = OUT / "install.log"
    process = subprocess.Popen(["msiexec", "/i", str(msi), "/l*v", str(log)])
    window = wizard()
    first_page(window, "&Next")
    texts = page(window, "wizard-1-welcome")
    check("ubo-lpa: uBlock Origin for Chrome" in texts, "the Welcome page's title does not name ubo-lpa")
    click(window, "&Next")
    texts = page(window, "wizard-2-release")
    check(any(re.search(r"uBlock Origin \d+\.\d+\.\d+ on GitHub", t) for t in texts),
          "the release page does not link a uBO release")
    click(window, "&Next")
    texts = page(window, "wizard-3-chrome")
    chrome = next((t for t in texts if t.startswith("Google Chrome ")), "")
    check(re.match(r"Google Chrome \d+\.\d+\.\d+\.\d+, installed for ", chrome),
          f"the Chrome page does not show Chrome's version: {texts}")
    (OUT / "chrome.txt").write_text(chrome, encoding="utf-8")
    check(any(t.endswith("folder Default") for t in texts),
          f"the Chrome page does not offer the Default profile: {texts}")
    click(window, "&Next")
    page(window, "wizard-4-options")
    click(window, "&Next")
    page(window, "wizard-5-ready")
    with AllowClicker(OUT) as clicker:
        click(window, "&Install")
        page(window, "wizard-6-progress", wait=4)
        wait_for_finish(window)
    check(clicker.prompts == 1,
          f"expected one remote debugging prompt during Setup, Chrome showed {clicker.prompts}")
    page(window, "wizard-7-finish")
    click(window, "&Finish")
    check(process.wait(timeout=120) == 0, f"msiexec /i exited with {process.returncode}")
    show_ubo_panel(OUT / "desktop-after-install.png")


def show_ubo_panel(path: Path) -> None:
    """
    Screenshot uBO at work, for the release page: an ad-block test page in
    the Chrome Setup loaded uBO into, with uBO's panel opened from its pinned
    button. A button UI Automation cannot find leaves the screenshot without
    the panel; it does not fail the test.
    """
    subprocess.Popen([chrome_exe(), "https://adblock-tester.com/"])
    time.sleep(15)
    try:
        from pywinauto import Desktop
        for window in Desktop(backend="uia").windows():
            if not window.element_info.class_name.startswith("Chrome_WidgetWin"):
                continue
            # Chrome names an action button after the extension's title.
            buttons = [b for b in window.descendants(control_type="Button")
                       if b.window_text().startswith("uBlock Origin")]
            if buttons:
                window.set_focus()
                # Chrome's toolbar buttons do not offer UI Automation's Invoke
                # pattern, so this is a mouse click.
                buttons[0].click_input()
                time.sleep(5)
                note(f"opened uBO's panel from its toolbar button {buttons[0].window_text()!r}")
                break
        else:
            note("did not find the uBlock Origin button in Chrome's toolbar")
    except Exception as exc:  # windows come and go while they are listed
        note(f"opening uBO's panel: {type(exc).__name__}: {exc}")
    screenshot(path)


def maintenance(action: str, shot: str, untick_updates: bool = False) -> None:
    """Open the maintenance page from the Start menu entry and run action on it."""
    os.startfile(str(MAINTENANCE_LNK))  # type: ignore[attr-defined]
    window = wizard()
    first_page(window, "&Update now")
    page(window, f"{shot}-1-maintenance")
    click(window, action)
    if action == "&Change settings":
        page(window, f"{shot}-2-release")
        click(window, "&Next")
        texts = page(window, f"{shot}-3-chrome")
        check(any("folder Default, already has uBlock Origin" in t for t in texts),
              "the Chrome page does not show that Default already has uBlock Origin")
        click(window, "&Next")
        if untick_updates:
            check(control(window, "&Update uBlock Origin daily").get_check_state() == 1,
                  "the daily update is not ticked before it is turned off")
            toggle(window, "&Update uBlock Origin daily")
        page(window, f"{shot}-4-options")
        click(window, "&Next")
    page(window, f"{shot}-5-ready")
    click(window, "&Change")
    wait_for_finish(window)
    page(window, f"{shot}-6-finish")
    click(window, "&Finish")
    window.wait_not("exists", timeout=120)
    # Windows Installer runs one installation at a time; let this one end
    # before the next starts.
    time.sleep(10)


def install_portable(msi: Path) -> str:
    """An install without maintenance, of the bundled release, into PORTABLE.
    Returns the bundled version the release page linked."""
    log = OUT / "portable.log"
    process = subprocess.Popen(["msiexec", "/i", str(msi), "/l*v", str(log)])
    window = wizard()
    first_page(window, "&Next")
    click(window, "&Next")
    toggle(window, "The release &bundled with this installer")
    texts = page(window, "portable-1-release")
    links = [m.group(1) for t in texts for m in [re.search(r"uBlock Origin (\d+\.\d+\.\d+) on GitHub", t)] if m]
    check(links, f"the release page links no release: {texts}")
    bundled = links[-1]
    click(window, "&Next")
    texts = page(window, "portable-2-chrome")
    check(any(t.endswith("folder Default") for t in texts),
          f"the Chrome page does not offer the Default profile: {texts}")
    click(window, "&Next")
    # Windows Installer's PathEdit control, which shows the folder.
    folder = control(window, re.compile(r".*\\uBOLPA\\?"), cls=None)
    note(f"the folder box is a {folder.class_name()}")
    # Windows Installer reads the box into the property when it loses focus.
    folder.set_focus()
    # A RichEdit20W, which pywinauto wraps as an edit box; its
    # set_window_text posts a pointer, set_edit_text sends it.
    folder.set_edit_text(str(PORTABLE))
    folder.type_keys("{TAB}")
    toggle(window, re.compile(r"Install &maintenance.*"))
    page(window, "portable-3-options")
    click(window, "&Next")
    page(window, "portable-4-ready")
    with AllowClicker(OUT / "portable") as clicker:
        click(window, "&Install")
        wait_for_finish(window)
    check(clicker.prompts == 1,
          f"expected one remote debugging prompt during the portable install, Chrome showed {clicker.prompts}")
    page(window, "portable-5-finish")
    click(window, "&Finish")
    check(process.wait(timeout=120) == 0, f"msiexec /i exited with {process.returncode}")
    return bundled


# --- checks ----------------------------------------------------------------


def verify_install(version: str) -> None:
    for path in (PROGRAM / "ubo-lpa.py", PROGRAM / "python" / "pythonw.exe",
                 PROGRAM / "assets", ROOT / "extension" / "manifest.json",
                 ROOT / "companion" / "manifest.json"):
        check(path.exists(), f"missing {path}")
    check(list((PROGRAM / "ubo").glob("uBlock0_*.chromium.zip")), "the bundled uBO release is missing")
    for name in (MAINTENANCE_LNK.name, "uBlock Origin update log.lnk"):
        check((MENU / name).exists(), f"missing Start menu entry {name}")
    entry = arp_entry()
    check(entry is not None and entry["DisplayVersion"] == version,
          f"entry in the list of installed apps: {entry}")
    note(f"installed {version}: files, Start menu and the entry in the list of installed apps are in place")


def verify_task(present: bool) -> None:
    if present:
        xml = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"],
                             capture_output=True, text=True).stdout
        check(str(PROGRAM / "python" / "pythonw.exe").lower() in xml.lower(),
              "the scheduled task does not run the installed pythonw.exe")
    else:
        check(not task_fields(), "the scheduled task is still there")
    check(state().get("autoupdate") is present, f"the state file records autoupdate {state().get('autoupdate')}")
    note(f"the daily update is {'on' if present else 'off'}")


def verify_chrome(ubo_id: str, comp_id: str, root: Path = ROOT) -> None:
    close_chrome()
    secure = json.loads((USER_DATA / "Default" / "Secure Preferences").read_text(encoding="utf-8"))
    prefs = json.loads((USER_DATA / "Default" / "Preferences").read_text(encoding="utf-8"))
    settings = {**prefs.get("extensions", {}).get("settings", {}),
                **secure.get("extensions", {}).get("settings", {})}
    for ext_id, folder in ((ubo_id, root / "extension"), (comp_id, root / "companion")):
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
    note(f"Chrome kept both extensions, loaded from {root} and enabled")


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


def verify_removed() -> None:
    check(not ROOT.exists(), f"uninstall left {ROOT}")
    check(not PROGRAM.exists(), f"uninstall left {PROGRAM}: {list(PROGRAM.rglob('*'))[:10]}")
    check(not MENU.exists(), "uninstall left the Start menu folder")
    check(arp_entry() is None, "uninstall left the entry in the list of installed apps")
    check(not task_fields(), "uninstall left the scheduled task")


def main() -> int:
    first, second = (Path(a).resolve() for a in sys.argv[1:3])
    version = lambda msi: msi.stem.split("-")[-1]  # noqa: E731
    OUT.mkdir(parents=True, exist_ok=True)
    USER_DATA.mkdir(parents=True, exist_ok=True)
    (USER_DATA / "First Run").touch()  # a user's Chrome has this marker
    try:
        seed_profile()
        install_with_wizard(first)
        verify_install(version(first))
        verify_task(present=True)
        ids = state()["ext_id"], state()["comp_id"]
        verify_chrome(*ids)
        run_task()

        note("maintenance: Change settings, without the daily update")
        maintenance("&Change settings", "maintenance-change", untick_updates=True)
        verify_task(present=False)
        note("maintenance: Update now")
        maintenance("&Update now", "maintenance-update")
        verify_task(present=False)

        note(f"upgrading to {version(second)}")
        result = subprocess.run(["msiexec", "/i", str(second), "/qn", "/l*v",
                                 str(OUT / "upgrade.log")], timeout=900)
        check(result.returncode == 0, f"the upgrade exited with {result.returncode}")
        verify_install(version(second))
        verify_task(present=False)
        check((state()["ext_id"], state()["comp_id"]) == ids, "the upgrade changed the extension IDs")
        verify_chrome(*ids)

        result = subprocess.run(["msiexec", "/x", str(second), "/qn", "/l*v",
                                 str(OUT / "uninstall.log")], timeout=900)
        check(result.returncode == 0, f"the uninstall exited with {result.returncode}")
        verify_removed()
        note("uninstall removed the program, the extension folder, the menu, the entry and the task")

        bundled = install_portable(first)
        check(arp_entry() is None, "the install without maintenance registered itself")
        check(not PROGRAM.exists(), f"the install without maintenance installed {PROGRAM}")
        check(not MENU.exists(), "the install without maintenance created Start menu entries")
        check(not task_fields(), "the install without maintenance scheduled the update")
        check(not ROOT.exists(), f"the install without maintenance used {ROOT}")
        portable = state(PORTABLE)
        check(portable.get("version") == bundled,
              f"the install without maintenance has uBO {portable.get('version')}, not the bundled {bundled}")
        verify_chrome(portable["ext_id"], portable["comp_id"], PORTABLE)
        note(f"the install without maintenance left only {PORTABLE}, with the bundled uBO {bundled}")
    except Failure as exc:
        note(f"FAIL: {exc}")
        screenshot(OUT / "failure.png")
        return 1
    note("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
