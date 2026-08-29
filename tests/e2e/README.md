# Browser end-to-end harness

`setup-ubo-wine-harness.sh` runs the installer and its browser-level checks in
fresh temporary state. It has two modes:

- `--linux` runs the native Linux installer against a temporary Chrome
  profile, then verifies install, blocking, popup, CSP and uninstall.
- the default mode runs the Windows installer under Wine 11 or newer, loads
  both unpacked directories through Chrome's Win32 folder picker, restarts
  Chrome, and performs the same functional checks.

Both modes take whole-desktop screenshots and retain logs in the directory
passed with `--out`. The Windows mode also records the downloaded Chrome MSI,
Chrome executable and harness hashes. Only run it against source you trust:
Wine exposes the host filesystem as `Z:` and is not a security boundary.

Run the diagnostics contract tests before a full browser pass:

```bash
python3 tests/e2e/test_probe_diagnostics.py
```

Run Linux Chrome:

```bash
tests/e2e/setup-ubo-wine-harness.sh --linux --out /tmp/ubo-linux-results .
```

Run Windows Chrome under Wine:

```bash
tests/e2e/setup-ubo-wine-harness.sh --out /tmp/ubo-wine-results .
```

The harness installs missing Debian/Ubuntu host packages by default. Set
`SKIP_HOST_PACKAGES=1` only when all required tools are already present. See
`--help` for cache inputs, Wine selection, low-memory mode and the AF_UNIX
preload bridge controls.

## Wine AF_UNIX bridge

The Windows harness builds the bridge from `wine-tcp-preload/` in a disposable
work-directory copy. Build and run its integration test directly with:

```bash
make -C tests/e2e/wine-tcp-preload check
```

`WINE_TCP_SHIM_SOURCE_DIR` selects a different source directory for bridge
development. The default is the source committed beside the harness.
