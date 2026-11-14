# ubo-lpa

Keeps uBlock Origin working in Chrome 151 and 152, with its Manifest V2
blocking intact.

[![Linux Chrome](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/linux.yml)
[![Windows Chrome under Wine](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml/badge.svg)](https://github.com/ojura/ubo-lpa/actions/workflows/wine.yml)

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

`uninstall` reverses the first two and removes both extensions.

## Install

`ubo-lpa.py` runs on both platforms and requires Python 3.9+. It generates an RSA
key per extension, using the `cryptography` package or the `openssl` command
when that package is absent. Linux systems normally have `openssl`, so pip is
not needed there. On Windows, install the package with
`python -m pip install cryptography`.

Tested against Chrome 151.0.7922.108 and 152.0.7977.64 on Linux. The Windows
steps (install, **Load unpacked** and a browser restart) were tested only in
Windows Chrome under Wine 11, not on Windows itself.

### Linux

Clone or update a persistent checkout and install:

```bash
src="${XDG_DATA_HOME:-$HOME/.local/share}/ubo-lpa-source"; if [ -d "$src/.git" ]; then git -C "$src" pull --ff-only; else git clone https://github.com/ojura/ubo-lpa.git "$src"; fi && python3 "$src/ubo-lpa.py" install
```

From an existing checkout:

```bash
python3 ubo-lpa.py install
```

Close Chrome first; it rewrites `Preferences` on exit and will overwrite the
injection. Root access is not required. The extensions and their keys are
stored in `~/.local/share/ublock-origin-lpa/` (under `$XDG_DATA_HOME` if set),
or in the directory given with `--dir`. `timer` also writes a systemd service
and timer to `~/.config/systemd/user/`. In each profile, `install` keeps a copy
of the original `Preferences` as `Preferences.ubo-lpa.bak` until `uninstall`
deletes it.

| command | |
|---|---|
| `install` | download and patch uBO, build the companion, inject both into every profile |
| `update` | alias for `install`; rebuilds a current, consistent installation only after a confirmation prompt |
| `check` | verify required files, IDs, cross-references and patch markers |
| `status` | report installed versions, IDs and paths |
| `uninstall` | remove both extensions and undo the profile changes [listed above](#what-the-installer-changes) |
| `timer` | install a systemd user timer that runs `install` daily |

The timer runs `install` once a day. If Chrome is running at that moment,
`install` exits before changing anything, and the timer tries again the next
day. Running `install` manually with Chrome closed is therefore the recommended
way to update. The unit records the script's absolute path, so moving the
checkout requires re-running `timer`.

### Windows

Clone or update a persistent checkout and stage the extensions:

```powershell
$src = Join-Path $env:LOCALAPPDATA 'uBOLPA-source'; if (Test-Path (Join-Path $src '.git')) { git -C $src pull --ff-only } else { git clone https://github.com/ojura/ubo-lpa.git $src }; if ($LASTEXITCODE -eq 0) { python -m pip install cryptography; if ($LASTEXITCODE -eq 0) { python (Join-Path $src 'ubo-lpa.py') install } }
```

From an existing checkout:

```powershell
python -m pip install cryptography
python ubo-lpa.py install
```

Then in Chrome:

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\extension`
4. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\companion`

Of the automated routes tested on Windows, only enterprise policy installs an
off-store extension that Chrome leaves enabled. That route needs administrator
rights and marks the browser as managed. [WRITEUP.md](WRITEUP.md#windows)
compares all the routes that were tested.

## Layout

```
ubo-lpa.py          the implementation, both platforms
assets/             the .js and .html files added to uBO and the companion
linux/ubo-lpa.sh    launcher
windows/ubo-lpa.ps1 launcher
tests/e2e/          browser harness, diagnostics tests, AF_UNIX shim for Wine
WRITEUP.md          technical design, and why Windows needs a manual install
```

## End-to-end tests

The browser harness tests the native Linux install, and the Windows install
under Wine 11 or newer, each in a fresh Chrome profile. Both jobs check that uBO
blocks requests, that the companion's popup shows uBO's panel and that an
ordinary web page cannot frame the panel. They upload screenshots and logs even
when a run fails. The Linux job also checks `uninstall`. The Wine job restarts
Chrome to check that both extensions are still enabled.

The Windows job installs the current WineHQ development build and loads both
directories through **Load unpacked** and Chrome's Win32 folder picker, which is
the manual route from the Windows instructions above.

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
