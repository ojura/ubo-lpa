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
and patches the few places uBO assumes it is an extension. Chrome does not allow
a toolbar button on an LPA, so it also builds a small companion extension to
carry one, with uBO's panel inside its popup.

[WRITEUP.md](WRITEUP.md) describes the technical details and lists all edits
performed by the installer.

## What installing changes

On both platforms the patched uBO is a separate extension with its own ID, so it
starts from uBlock Origin's defaults. An existing uBO's filter lists, custom
rules and settings stay in that extension's storage and are not carried over.
Export them from the stock uBO's dashboard first and import them afterwards.

On Windows that is the whole of it: the installer writes two directories and you
load them yourself.

On Linux the installer edits each Chrome profile. Profiles are searched under
`$XDG_CONFIG_HOME` (default `~/.config`) in `google-chrome`, `-beta`,
`-unstable` and `chromium`, covering `Default` and `Profile *`. In each one it
also:

- disables the Web Store uBO, so the two do not both filter
- turns on `chrome://extensions` Developer mode, which the MV3 companion needs
- marks both extensions as allowed in incognito
- grants the permissions declared in each manifest, without a prompt

`uninstall` reverses the first two and removes both extensions.

## Install

`ubo-lpa.py` runs on both platforms and needs Python 3.9+. It generates an RSA
key per extension, using the `cryptography` package or the `openssl` command
when that package is absent, so Linux normally needs nothing further. On
Windows, install it with `python -m pip install cryptography`.

Tested against Chrome 151.0.7922.108 and 152.0.7977.64 on Linux. The Windows
installer lifecycle was exercised under Wine 11, which is a compatibility layer
rather than Windows itself.

### Linux

```bash
python3 ubo-lpa.py install
```

Close Chrome first; it rewrites `Preferences` on exit and will overwrite the
injection. No root required. By default the extensions and their keys live in
`~/.local/share/ublock-origin-lpa/`, which `--dir` changes; `timer` also writes
a systemd unit under `~/.config/systemd/user/`, and install leaves a
`Preferences.ubo-lpa.bak` in each profile until uninstall.

| command | |
|---|---|
| `install` | download, patch, build companion, inject into every profile |
| `update` | alias for `install`; a current, consistent install does nothing |
| `check` | verify required files, IDs, cross-references and patch sentinels |
| `status` | what is installed where |
| `uninstall` | remove both extensions and undo the profile changes below |
| `timer` | systemd user timer that attempts an update daily |

The timer runs `install` once a day. If Chrome is running it exits without
installing and the next day's activation tries again, so running `install` by
hand with Chrome closed is the reliable path. The unit calls this script where
it currently sits, so moving the checkout requires re-running `timer`.

### Windows

```powershell
python -m pip install cryptography
python ubo-lpa.py install
```

Then in Chrome:

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\extension`
4. **Load unpacked**, pick `%LOCALAPPDATA%\uBOLPA\companion`

Chrome refuses scripted installs of off-store extensions on Windows. Enterprise
policy can deploy them instead, but Google documents that route as needing a
domain-joined or Entra-joined machine, or Chrome Enterprise Core enrolment, and
it marks the browser as managed and makes both extensions non-removable.
[WRITEUP.md](WRITEUP.md) covers it.

## Layout

```
ubo-lpa.py          the implementation, both platforms
assets/             the patch payloads as .js and .html files
linux/ubo-lpa.sh    launcher
windows/ubo-lpa.ps1 launcher
WRITEUP.md          technical details, and what was tried on Windows
```

## End-to-end tests

The browser harness exercises the native Linux path and the Windows path under
Wine 11 or newer. Both jobs use fresh Chrome state, verify filtering and the
companion popup, exercise the CSP boundary, and publish screenshots and logs
even when a run fails. Linux also checks uninstall; Wine restarts Windows
Chrome to check persistence.

The Windows job installs the current WineHQ development build and loads both
directories through Chrome's real Win32 folder picker; it does not bypass the
manual Windows installation route with `--load-extension`.

See [tests/e2e/README.md](tests/e2e/README.md) for local commands and safety
notes.

## Caveats

- **It can break at any Chrome release**, since it depends on LPAs being left out
  of a check they could be added to at any time.
- **uBO appears under "Chrome Apps"**, not "Extensions".
- **uBO is patched at install time.** The edits are listed in
  [WRITEUP.md](WRITEUP.md); `check` reports when one is missing.
- **Keep the install directory.** It holds the private keys that fix the
  extension IDs. Without them a rebuild mints new IDs, Chrome sees different
  extensions, and uBO starts from defaults again. They are unencrypted private
  keys, so keep any copy private.
- **Not affiliated with uBlock Origin.** Reproduce bugs on a stock install before
  taking them to its tracker.

## Licence

GPL-3.0, matching uBlock Origin. uBlock Origin is © Raymond Hill and is
downloaded from its own releases at install time; no uBO code is in this
repository.
