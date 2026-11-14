# Browser end-to-end harness

`setup-ubo-wine-harness.sh` runs the installer against a fresh Chrome profile,
in a fresh Wine prefix for the Windows mode, and then checks the result in the
browser. It has two modes:

- `--linux` runs the native Linux installer against a temporary Chrome
  profile and starts Chrome. It then updates the install twice while Chrome is
  open, first to a changed companion worker and then back to the first one,
  and restarts Chrome after each update. After each restart Chrome has to run
  the worker that is on disk. Then it runs the browser checks below, and
  uninstall.
- the default mode runs the Windows installer under Wine 11 or newer, loads
  both unpacked directories through Chrome's Win32 folder picker, turns on the
  companion's "Allow user scripts", restarts Chrome, and runs the same browser
  checks.

`probe.py` runs these browser checks:

- both extensions are enabled without runtime errors, and the only warnings
  allowed are the two manifest warnings that Chrome gives uBO as a legacy
  packaged app;
- uBO blocks a request to an advertising host. Chrome resolves that host to
  the probe's own server, where the request would arrive if uBO let it
  through;
- the popup shows uBO's panel, and Chrome runs the build that is on disk. In
  the Linux run this check comes after the two updates, so it fails when
  Chrome still runs a worker from an earlier build. The Linux run also starts
  Chrome with the popup page open in a tab. That page loads before uBO's
  background page exists, and uBO's panel has to load in it all the same;
- the button's badge and title show uBO's count for the tab;
- uBO runs a keyboard command that a companion page sends it. The Linux run
  also assigns a shortcut to the command and presses it, so that the command
  goes through the companion's worker;
- an ordinary web page cannot frame uBO's panel;
- the companion registers a site's main-world and isolated-world scriptlets
  as user scripts. The main-world scriptlet runs before the page's first
  script, and the isolated-world remove-node-text scriptlet empties the page's
  second script before it runs. A new empty iframe has the main-world
  scriptlet before the script that created it continues. uBO's main-world
  code runs exactly once in the page and once in the iframe, also after the
  registrations are removed without uBO's knowledge;
- the companion keeps the launch guard's rule only while uBO's setting
  "Suspend network activity until all filter lists are loaded" is on. A
  browser restart drops the session rule that switches the guard off. The
  probe removes that rule in the same way, and a page then has to pass
  through the companion's wait page and load;
- in the Linux run, the button is pinned to the toolbar, and starting uBO's
  incognito instance leaves the registered scriptlets in place.

Both modes take whole-desktop screenshots and retain logs in the directory
passed with `--out`, and record the SHA-256 hashes of the input and the harness
in `PROVENANCE.txt`. The Windows mode also records the hashes of the downloaded
Chrome MSI and of the Chrome executable. Only run the harness against trusted
source: Wine exposes the host filesystem as `Z:` and is not a security boundary.

Before a browser run, test the code in `probe.py` that decides which Chrome
extension warnings and errors fail a run. The same tests check that `probe.py`
matches the copy embedded in the harness:

```bash
python3 tests/e2e/test_probe_diagnostics.py
```

Test with Linux Chrome:

```bash
tests/e2e/setup-ubo-wine-harness.sh --linux --out /tmp/ubo-linux-results .
```

Test with Windows Chrome under Wine:

```bash
tests/e2e/setup-ubo-wine-harness.sh --out /tmp/ubo-wine-results .
```

The harness installs missing Debian/Ubuntu host packages by default. Set
`SKIP_HOST_PACKAGES=1` only when all required tools are already present.
`--help` lists the environment variables for cached downloads, the Wine binary,
low-memory mode and the AF_UNIX preload shim.

## AF_UNIX preload shim for Wine

Some sandboxes block pathname AF_UNIX sockets, and then wineserver cannot
create its master socket. `wine-tcp-preload/` is an `LD_PRELOAD` library that
sets up each of Wine's pathname AF_UNIX connections over loopback TCP and then
swaps in a native AF_UNIX socket pair, so Wine's traffic keeps its AF_UNIX
semantics. With the default `WINE_TCP_SHIM=auto`, the harness uses the shim
only when pathname AF_UNIX sockets are blocked.

The Windows mode builds the shim from a copy of `wine-tcp-preload/` in its
temporary work directory. Build it and run its integration test directly with:

```bash
make -C tests/e2e/wine-tcp-preload check
```

The build emits x86-64 and i386 libraries. The harness passes a single absolute
preload path containing glibc's `$LIB` token, which selects the matching
library for each Wine process.

`WINE_TCP_SHIM_SOURCE_DIR` makes the harness build the shim from another source
directory, which helps when changing the shim. The default is the source
committed beside the harness. The Wine job in GitHub Actions sets
`WINE_TCP_SHIM=always`, so the browser run uses the shim even when the runner
supports pathname AF_UNIX sockets.
