# ubo-lpa

Keeps uBlock Origin working in Chrome 151 through 157, with its Manifest V2
blocking intact.

[![Linux Chrome](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml)
[![Windows Chrome under Wine](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml)
[![Windows](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml)

## Install

Linux, with Chrome closed:

```bash
curl -fsSL https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh
```

or `wget -qO- https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.sh | sh`.

Windows: **[download the latest MSI](https://github.com/ojura/ubo-lpa/releases/latest/download/ubo-lpa.msi)**,
or run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/ojura/ubo-lpa/main/install.ps1 | iex
```

<p align="center">
  <a href="#setup-on-windows"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-3-chrome.png" width="560" alt="The setup wizard's Chrome page, with the Chrome version it found and the profiles that get uBlock Origin"></a><br>
  <sub>The Windows setup on a current Chrome, in which Manifest V2 extensions no longer run</sub>
</p>

Both set up a daily update. The Windows setup can also leave the daily update
out, or install without maintenance and leave nothing but the uBlock Origin
folder. The patched uBO starts with uBlock Origin's default settings; to keep
the current ones, back them up from the stock uBO's dashboard first and restore
them afterwards. [Installation details](#installation-details) describes each
route.

## How it works

Chrome disables MV2 extensions, but it still supports legacy packaged apps
(LPAs). This installer takes advantage of that to keep running the MV2 version of
uBlock Origin. Adding the `app` entry to its manifest is enough to make Chrome
treat it as an LPA.

The installer downloads uBlock Origin from its own releases, adds the `app` key,
and patches the code paths where uBO assumes it is an extension. Chrome does
not allow an LPA to have a toolbar button or keyboard shortcuts, so the
installer also builds a small companion extension that has both. The
companion's button shows uBO's count and state and opens uBO's panel. The
companion also passes the shortcuts on to uBO.

The companion is a Manifest V3 extension, so it can use `chrome.userScripts`,
which a legacy packaged app cannot. For a site that has scriptlet filters of
its own, the companion registers uBO's scriptlets as user scripts, so they run
before the page's first script. On Chromium, uBO's own injection arrives after
that script has run. That is too late for a scriptlet that has to empty an
inline script before it runs, or wrap a function before the page saves a
reference to it. uBO's filter lists use scriptlets of both kinds against
Ad-Shield. On index.hr, a filter removes Ad-Shield's bootstrap script. That
script still ran in 5 of 6 loads with uBO's own injection, and in 0 of 8 loads
with the registered scripts (edit 8 in [WRITEUP.md](WRITEUP.md)).

[WRITEUP.md](WRITEUP.md) describes the technical details and lists all edits
performed by the installer.

## What the installer does

On both platforms the patched uBO is a separate extension with its own ID, so it
starts from uBlock Origin's defaults. An existing uBO's filter lists, custom
rules and settings remain in that extension's storage and are not carried over.
Export them from the stock uBO's dashboard first and import them afterwards.

On Windows, the installer writes the patched uBO and companion folders, and
`load` loads them into the chosen Chrome profiles through Chrome's own Load
unpacked, as a user would by hand. Chrome then keeps them like any other
unpacked extension.

On Linux the installer adds both extensions to each Chrome profile it finds
under `$XDG_CONFIG_HOME` (default `~/.config`) in `google-chrome`, `-beta`,
`-unstable` and `chromium`, covering `Default` and `Profile *`. In each profile
it also:

- disables the Web Store uBO, so the two do not both filter
- turns on `chrome://extensions` Developer mode, which is required for installing
  the companion extension
- marks both extensions as allowed in incognito
- grants the permissions declared in each manifest, without a prompt
- turns on the companion's **Allow user scripts**, which lets uBO's scriptlets
  run before a page's own scripts (edit 8 in [WRITEUP.md](WRITEUP.md))
- pins the companion's button to the toolbar

`uninstall` reverses the first two and removes both extensions.

uBO's keyboard shortcuts are assigned at `chrome://extensions/shortcuts`, under
"uBlock Origin toolbar button". Chrome's side panel menu has an entry of the
same name, which shows uBO's logger beside the page.

Chrome loads the tabs it restores at launch before uBO has started, so their
first requests are not filtered. The button marks such a tab with "!". To have
those tabs wait for uBO instead, turn on **Suspend network activity until all
filter lists are loaded** in uBO's Filter lists pane. The companion then holds
the restored tabs until uBO filters requests, for at most ten seconds. It
holds most other pages that open before then in the same way.
[WRITEUP.md](WRITEUP.md#the-companion-extension) describes this launch guard
and lists the pages it covers.

## Installation details

From a checkout, `ubo-lpa.py` runs on both platforms; Python 3.9 or newer is
required. It generates an RSA key per extension, using the `cryptography`
package or the `openssl` command when that package is absent. Linux systems
normally have `openssl`, so pip is not needed there. On Windows, install the
package with `python -m pip install cryptography`.

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
its browser is closed. If Chrome is open at the first install, `install` builds
the files, leaves the profiles alone and exits with status 1. Updates keep the
same paths and IDs and only replace the files, so they run while Chrome is open,
and Chrome loads the new version at its next start. Adding the extensions to a
new profile and `uninstall` need Chrome closed again. Root access is not
required. The extensions and their keys are stored in
`~/.local/share/ublock-origin-lpa/` (under `$XDG_DATA_HOME` if set), or in the
directory given with `--dir`. `timer` also writes a systemd service and timer to
`~/.config/systemd/user/`. In each profile, `install` keeps a copy of the
original `Preferences` as `Preferences.ubo-lpa.bak` until `uninstall` deletes
it.

| command | |
|---|---|
| `install` | download and patch uBO, build the companion, inject both into every profile; `--ubo-zip` installs a given uBO release zip instead of the newest release |
| `update` | alias for `install`; rebuilds a current, consistent installation only after a confirmation prompt |
| `check` | verify required files, IDs, cross-references and patch markers |
| `status` | report installed versions, IDs and paths |
| `uninstall` | remove both extensions and undo the profile changes [listed above](#what-the-installer-does) |
| `timer` | run `install` daily: a systemd user timer on Linux, a scheduled task on Windows; `--off` removes it, `--keep` sets it up unless it was switched off |
| `load` | Windows: load both extensions into Chrome; `--profiles` names the profile folders, separated by `\|` |
| `setup-info` | print, as JSON, what the Windows setup wizard shows: Chrome, its profiles and the uBO releases |

The timer runs `install` once a day, whether or not Chrome is open. If a
profile still needs the extensions added, such as a profile created since the
last install, the run exits with status 1 until `install` runs once with Chrome
closed. The unit records the script's absolute path and the install directory,
so moving the checkout requires re-running `timer`.

### Windows

The [latest MSI](https://github.com/ojura/ubo-lpa/releases/latest/download/ubo-lpa.msi)
comes from the newest commit on `main` that passed the
[Windows workflow](https://github.com/ojura/ubo-lpa/actions/workflows/windows.yml),
which builds and tests the MSI and publishes it as the
[`latest` release](https://github.com/ojura/ubo-lpa/releases/tag/latest).
[install.ps1](install.ps1) downloads it and starts its setup wizard. The MSI is
not code-signed, so Windows SmartScreen warns about it. The release page shows
each step of the setup, from screenshots the msi job takes on a Windows runner.

The MSI installs everything for the current user, without administrator rights,
and contains its own Python and the uBO release that was newest when it was
built. Its wizard asks:

- which uBO release to install: the newest one, downloaded from GitHub, or the
  bundled one, each with a link to its release notes;
- which Chrome profiles get it, on a page that shows the Chrome it found, its
  version and whether it is installed for this user or for all users;
- which folder to keep it in, `%LOCALAPPDATA%\uBOLPA` by default;
- whether to install maintenance and whether to update daily, both on by
  default.

Setup then patches uBO into the folder and loads both extensions into the chosen
profiles. Chrome asks once whether to allow remote debugging. With Chrome
closed, a click on **Allow** is the only step Setup leaves to the user; with
Chrome running, Setup first asks to restart it, and Chrome reopens its tabs.

Maintenance is the updater in `%LOCALAPPDATA%\Programs\ubo-lpa`, the daily
update, an entry in the list of installed apps and a Start menu entry, "Update,
change or remove uBlock Origin for Chrome". That entry reopens Setup at its
maintenance page: **Update now**, **Change settings** (release, profiles, daily
update) or **Remove**. Without maintenance, Setup works from its own temporary
copy and leaves nothing but the uBlock Origin folder, as a portable install.
Running the MSI again with the same folder updates it, and keeps uBO's settings.

#### Setup on Windows

These screenshots come from the msi job's test on a Windows runner, and are
replaced whenever a new MSI is published.

<table>
<tr>
<td align="center" valign="top" width="33%"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-1-welcome.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-1-welcome.png" width="260" alt="Welcome page"></a><br><sub>Welcome</sub></td>
<td align="center" valign="top" width="33%"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-2-release.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-2-release.png" width="260" alt="Release page"></a><br><sub>The newest uBO release, or the bundled one</sub></td>
<td align="center" valign="top" width="33%"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-3-chrome.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-3-chrome.png" width="260" alt="Chrome page"></a><br><sub>A current Chrome, without Manifest V2, and its profiles</sub></td>
</tr>
<tr>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-4-options.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-4-options.png" width="260" alt="Options page"></a><br><sub>Folder, maintenance and daily update</sub></td>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-5-ready.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-5-ready.png" width="260" alt="Ready page"></a><br><sub>Ready to install</sub></td>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-6-progress.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-6-progress.png" width="260" alt="Progress page"></a><br><sub>Installing</sub></td>
</tr>
<tr>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-7-chrome-prompt.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-7-chrome-prompt.png" width="260" alt="Chrome's remote debugging prompt"></a><br><sub>Allow in Chrome's remote debugging prompt</sub></td>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-8-finish.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-8-finish.png" width="260" alt="Finish page"></a><br><sub>Finished</sub></td>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-9-chrome.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-9-chrome.png" width="260" alt="uBlock Origin's panel in Chrome, on an ad-block test page"></a><br><sub>uBO's panel, from its pinned button</sub></td>
</tr>
<tr>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-10-maintenance.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-10-maintenance.png" width="260" alt="Maintenance page"></a><br><sub>Maintenance, from the Start menu</sub></td>
<td align="center" valign="top"><a href="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-11-portable.png"><img src="https://github.com/ojura/ubo-lpa/releases/download/screenshots/setup-11-portable.png" width="260" alt="Options page without maintenance"></a><br><sub>Without maintenance: a portable install</sub></td>
<td></td>
</tr>
</table>

From a checkout, with Python and `cryptography`:

```powershell
python -m pip install cryptography
python ubo-lpa.py install
python ubo-lpa.py load
python ubo-lpa.py timer
```

`load` performs the steps below over Chrome's DevTools connection: it turns on
Developer mode, loads both folders through the extensions page, turns on the
companion's **Allow user scripts** and pins its button. If remote debugging was
off, it turns it on for this and off again afterwards. Loading by hand works as
well, and is needed only once:

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\extension`, or `extension`
   in the folder chosen in Setup
4. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\companion`, or `companion`
   in that folder
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

Of the routes tested on Windows, only enterprise policy and Load unpacked
install an off-store extension that Chrome keeps enabled across restarts, and
only Load unpacked does so without putting the browser under enterprise
management. [WRITEUP.md](WRITEUP.md#windows) compares all the routes that were
tested, and describes how `load` drives Load unpacked.

## Layout

```
ubo-lpa.py          the implementation, both platforms
install.sh          Linux one-liner: fetch the repository and install
install.ps1         Windows one-liner: download the latest MSI and run it
assets/             the .js and .html files added to uBO and the companion
linux/ubo-lpa.sh    launcher
windows/ubo-lpa.ps1 launcher
tests/e2e/          browser harness, diagnostics tests, AF_UNIX shim for Wine
windows/msi/        the MSI: WiX sources, setup art, the wizard's custom
                    actions and the build script
tests/windows/      Windows runner tests of load, the scheduled task, updates
                    and the MSI
WRITEUP.md          technical design, and why Windows needs Load unpacked
```

## End-to-end tests

The browser harness tests the native Linux install, and the Windows install
under Wine 11 or newer, each in a fresh Chrome profile. Both jobs check that
uBO blocks requests, that the companion's popup shows uBO's panel, that the
companion's button shows uBO's count, and that an ordinary web page cannot
frame the panel. They also check that uBO runs a keyboard command sent from
the companion, that a site's scriptlets run before the page scripts they act
on, and that uBO's main-world scriptlet code runs exactly once in each
document. They upload screenshots and logs even when a run fails. The Linux
job also updates the install twice while Chrome runs, first to a changed
companion and then back to the first one. After each update it restarts Chrome
and checks that Chrome then runs the companion that is on disk. It also checks
`uninstall`. The Wine job restarts Chrome to check that both extensions are
still enabled.

The Wine job installs the current WineHQ development build and loads both
directories through **Load unpacked** and Chrome's Win32 folder picker, which is
the manual route from the Windows instructions above.

The Windows workflow runs on GitHub Actions Windows runners, with the retail
Chrome installed there. [tests/windows/run.py](tests/windows/run.py) installs,
runs the scheduled task through Task Scheduler, runs `load` while UI Automation
clicks Allow in Chrome's prompt, checks what Chrome saved about both
extensions, updates the install while Chrome runs with them loaded, and
uninstalls. The msi job builds the MSI with
[windows/msi/build.py](windows/msi/build.py), and
[tests/windows/msi_test.py](tests/windows/msi_test.py) installs it through the
wizard with a screenshot of every page, changes its settings and updates it
from the maintenance page, upgrades it, uninstalls it, and installs it once
more without maintenance.

See [tests/e2e/README.md](tests/e2e/README.md) for local commands and safety
notes.

## Caveats

- **Compatibility is limited to tested Chrome releases.** A future release may
  extend MV2 enforcement to LPAs and make this approach inoperable.
- **uBO appears under "Chrome Apps"**, not "Extensions".
- **`chrome://apps` offers to remove it.** That page lists uBO as an unsupported
  app, with a "Remove unsupported app" link. The link's dialog uninstalls uBO,
  and the uninstall deletes uBO's settings.
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
