# ubo-lpa

Keeps uBlock Origin working in Chrome 151 through 157, with its Manifest V2
blocking intact.

[![Linux Chrome](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml)
[![Windows Chrome under Wine](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml)
[![Windows](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml)

Chrome disables MV2 extensions, but it still supports legacy packaged apps
(LPAs). This installer takes advantage of that to keep running the MV2 version of
uBlock Origin. Adding the `app` entry to its manifest is enough to make Chrome
treat it as an LPA.

The installer downloads uBlock Origin from its own releases, adds the `app` key,
and patches the code paths where uBO assumes it is an extension. Chrome does not
allow an LPA to provide a toolbar button, so the installer also builds a small
companion extension that provides the button and displays uBO's panel.

[WRITEUP.md](WRITEUP.md) describes the technical details and lists all edits
performed by the installer.

## What the installer changes

On both platforms the patched uBO is a separate extension with its own ID, so it
starts from uBlock Origin's defaults. An existing uBO's filter lists, custom
rules and settings remain in that extension's storage and are not carried over.
Export them from the stock uBO's dashboard first and import them afterwards.

On Windows, the installer writes the patched uBO and companion directories. Both
must then be loaded manually from `chrome://extensions`.

On Linux the installer adds both extensions to each Chrome profile it finds
under `$XDG_CONFIG_HOME` (default `~/.config`) in `google-chrome`, `-beta`,
`-unstable` and `chromium`, covering `Default` and `Profile *`. In each profile
it also:

- disables the Web Store uBO, so the two do not both filter
- turns on `chrome://extensions` Developer mode, which the MV3 companion requires
- marks both extensions as allowed in incognito
- grants the permissions declared in each manifest, without a prompt
- turns on the companion's **Allow user scripts**, which lets uBO's scriptlets
  run before a page's own scripts (edit 8 in [WRITEUP.md](WRITEUP.md))

`uninstall` reverses the first two and removes both extensions.

## Install

Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh
```

or with wget:

```bash
wget -qO- https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh
```

Windows: **[download the latest MSI](https://github.com/ojura/ubo-lpa/releases/latest/download/ubo-lpa.msi)**,
or run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.ps1 | iex
```

Both set up the daily update. On Linux, close Chrome first. The sections below
describe what each route does.

From a checkout, `ubo-lpa.py` runs on both platforms and requires Python 3.9+.
It generates an RSA key per extension, using the `cryptography` package or the
`openssl` command when that package is absent. Linux systems normally have
`openssl`, so pip is not needed there. On Windows, install the package with
`python -m pip install cryptography`.

CI tests every push with the current stable Chrome on Linux, on GitHub's
Windows runners, and as Windows Chrome under Wine 11; see
[End-to-end tests](#end-to-end-tests).

### Linux

[install.sh](install.sh) keeps the repository in
`~/.local/share/ubo-lpa-source` (under `$XDG_DATA_HOME` if set): a git checkout
when git is installed, otherwise the `main` branch's archive. It runs `install`
and `timer` from there, so the daily update keeps working as long as that
directory stays. Running the one-liner again updates ubo-lpa, then uBO.

From a checkout:

```bash
python3 ubo-lpa.py install
python3 ubo-lpa.py timer
```

Close Chrome for the first install. Chrome keeps each profile's `Preferences` in
memory and writes it back on exit, so the installer only edits a profile while
its browser is closed. With Chrome open, `install` builds the files, leaves the
profiles alone and exits with status 1. Updates keep the same paths and IDs and
only replace the files, so they run while Chrome is open, and Chrome loads the
new version at its next start. Adding the extensions to a new profile and
`uninstall` need Chrome closed again. Root access is not required. The
extensions and their keys are stored in `~/.local/share/ublock-origin-lpa/`
(under `$XDG_DATA_HOME` if set), or in the directory given with `--dir`. `timer`
also writes a systemd service and timer to `~/.config/systemd/user/`. In each
profile, `install` keeps a copy of the original `Preferences` as
`Preferences.ubo-lpa.bak` until `uninstall` deletes it.

| command | |
|---|---|
| `install` | download and patch uBO, build the companion, inject both into every profile |
| `update` | alias for `install`; rebuilds a current, consistent installation only after a confirmation prompt |
| `check` | verify required files, IDs, cross-references and patch markers |
| `status` | report installed versions, IDs and paths |
| `uninstall` | remove both extensions and undo the profile changes [listed above](#what-the-installer-changes) |
| `timer` | run `install` daily: a systemd user timer on Linux, a scheduled task on Windows |

The timer runs `install` once a day, whether or not Chrome is open. If a
profile still needs the extensions added, such as a profile created since the
last install, the run exits with status 1 until `install` runs once with Chrome
closed. The unit records the script's absolute path and the install directory,
so moving the checkout requires re-running `timer`.

### Windows

The [latest MSI](https://github.com/ojura/ubo-lpa/releases/latest/download/ubo-lpa.msi)
is the one the [Windows workflow](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml)
built and tested from the newest commit on `main`, published as the
[`latest` release](https://github.com/ojura/ubo-lpa/releases/tag/latest).
[install.ps1](install.ps1) downloads it and starts its setup wizard. The MSI is
not code-signed, so Windows SmartScreen warns about it. The release page shows
each step of the setup, from screenshots the msi job takes on a Windows runner.

The MSI installs everything for the current user, without administrator rights.
It brings its own Python, downloads and patches uBO into
`%LOCALAPPDATA%\uBOLPA`, schedules the daily update, and loads both extensions
into Chrome. Chrome asks once whether to allow remote debugging; a click on
**Allow** is the only step Setup leaves to the user. If Chrome is running at
that moment, Setup asks to restart it first, and Chrome reopens its tabs.

From a checkout, with Python and `cryptography`:

```powershell
python -m pip install cryptography
python ubo-lpa.py install
python ubo-lpa.py load
python ubo-lpa.py timer
```

`load` does what Load unpacked does by hand, over Chrome's DevTools connection:
it turns on Developer mode, loads both folders through the extensions page,
turns on the button's **Allow user scripts** and pins the button. It switches
remote debugging on only for this, and off again afterwards. Loading by hand
works as well, once:

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\extension`
4. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\companion`
5. Open **Details** of "uBlock Origin toolbar button" and turn on **Allow user
   scripts**, so uBO's scriptlets run before the page's own scripts

Updates replace both folders in place, Chrome keeps the extensions loaded, and
it loads the new version at its next start. `timer` registers a scheduled task,
`ubo-lpa-update`, that runs `install` once a day with `pythonw.exe`, without a
console window. It runs while the user is logged on, and a day missed while the
PC was off is caught up at the next logon. Each run appends its output to
`update.log` in the install directory. `uninstall` removes the task. With
`--dir`, the task name ends in a hash of the directory, so each install
directory has its own task.

Of the automated routes tested on Windows, only enterprise policy installs an
off-store extension that Chrome leaves enabled, and only Load unpacked does so
without managing the browser. [WRITEUP.md](WRITEUP.md#windows) compares all the
routes that were tested, and describes how `load` drives Load unpacked.

## Layout

```
ubo-lpa.py          the implementation, both platforms
install.sh          Linux one-liner: fetch the repository and install
install.ps1         Windows one-liner: download the latest MSI and run it
assets/             the .js and .html files added to uBO and the companion
linux/ubo-lpa.sh    launcher
windows/ubo-lpa.ps1 launcher
tests/e2e/          browser harness, diagnostics tests, AF_UNIX shim for Wine
windows/msi/        the MSI: WiX sources, setup art and build script
tests/windows/      Windows runner tests of load, the scheduled task, updates
                    and the MSI
WRITEUP.md          technical design, and why Windows needs a manual install
```

## End-to-end tests

The browser harness tests the native Linux install, and the Windows install
under Wine 11 or newer, each in a fresh Chrome profile. Both jobs check that uBO
blocks requests, that the companion's popup shows uBO's panel and that an
ordinary web page cannot frame the panel. They upload screenshots and logs even
when a run fails. The Linux job also checks `uninstall`. The Wine job restarts
Chrome to check that both extensions are still enabled.

The Wine job installs the current WineHQ development build and loads both
directories through **Load unpacked** and Chrome's Win32 folder picker, which is
the manual route from the Windows instructions above.

The Windows workflow runs on GitHub Actions Windows runners, with the retail
Chrome installed there. [tests/windows/run.py](tests/windows/run.py) installs,
runs the scheduled task through Task Scheduler, runs `load` while UI Automation
clicks Allow in Chrome's prompt, checks what Chrome saved about both
extensions, updates the install while Chrome runs with them loaded, and
uninstalls. [tests/windows/msi_test.py](tests/windows/msi_test.py) builds the
MSI, installs it through the wizard with a screenshot of every page, upgrades
it and uninstalls it.

See [tests/e2e/README.md](tests/e2e/README.md) for local commands and safety
notes.

## Caveats

- **Compatibility is limited to tested Chrome releases.** A future release may
  extend MV2 enforcement to LPAs and make this approach inoperable.
- **uBO appears under "Chrome Apps"**, not "Extensions".
- **uBO is patched at install time.** The edits are listed in
  [WRITEUP.md](WRITEUP.md); `check` reports when one is missing.
- **Keep the install directory.** It holds the private keys that determine
  the extension IDs. Without them a rebuild generates new IDs, Chrome registers
  different extensions, and uBO starts from defaults again. They are
  unencrypted private keys, so keep any copy private.
- **Not affiliated with uBlock Origin.** Reproduce issues on a stock install
  before reporting them to the upstream project.

## Licence

GPL-3.0, matching uBlock Origin. uBlock Origin is © Raymond Hill and is
downloaded from its own releases at install time. This repository does not
contain any uBO code.
