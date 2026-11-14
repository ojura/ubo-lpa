# Browser end-to-end harness

`setup-ubo-wine-harness.sh` runs the installer against a fresh Chrome profile,
in a fresh Wine prefix for the Windows mode, and then checks the result in the
browser. It has two modes:

- `--linux` runs the native Linux installer against a temporary Chrome
  profile, then checks the install, request blocking, the popup, the CSP that
  keeps other pages from framing uBO's panel, and uninstall.
- the default mode runs the Windows installer under Wine 11 or newer, loads
  both unpacked directories through Chrome's Win32 folder picker, restarts
  Chrome, and runs the same browser checks except uninstall.

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
