# Technical design and implementation

This document describes the workaround, the edits the installer makes, why the
Windows install has to be completed manually, and how to troubleshoot problems.

## What the installer changes

Symbols below refer to uBlock Origin 1.76.2. Line numbers vary between
releases, so they are omitted. If the code a patch is anchored to has moved,
the installer discards the staged build and leaves the previous installation
unchanged.

`check` verifies required files, the two IDs and their cross-references, and
the marker strings and manifest entries the patches add. It is not a
byte-for-byte or semantic comparison.

The installer applies eight edits to uBlock Origin and generates a companion
extension. Each rebuild extracts a fresh copy of uBO and applies all eight
edits, so a given upstream release and key pair always produce the same result.
Running `install` again rebuilds a current, consistent installation only after
a confirmation prompt. `ubo-lpa.py check` verifies the result.

An update replaces the extension and companion directories in place and leaves
the profile entries as they are: those written by the Linux installer, and
those Load unpacked created on Windows. The install path and the keys stay the
same, so the IDs and paths in Chrome's preferences remain valid. Chrome reads
an unpacked extension's manifest from disk at every start
(`ShouldReloadExtensionManifest()` in `installed_loader.cc`), and it does not
disable an unpacked extension whose new version asks for more permissions
(`CheckPermissionsIncrease()` in `chrome_extension_registrar_delegate.cc`).
Updates therefore run while Chrome is open, and on Windows the manual steps are
needed only for the first install. Until Chrome restarts, the running uBO
background page keeps the old code, while pages opened in the meantime, such as
the popup and the dashboard, load the new files.

Windows refuses to rename a directory while a file in it is open, and a virus
scanner checking the new files, or a Chrome read, can hold one open for a
moment. The installer retries each rename for up to ten seconds. If a rename
still fails, it moves the previous directories back and exits with status 1,
and the next run tries again.

The installer edits a profile only when its entries need changing, and only
while its browser is closed. It decides that from the `SingletonLock` symlink
Chrome keeps in its user data directory while it runs, which points to
`<hostname>-<pid>`.

The payloads live in `assets/` as files, with `__UBO_ID__`, `__COMP_ID__` and
`__RESIZE_MSG__` substituted at install time.

---

### 1. `app` key in `manifest.json`

```json
"app": { "launch": { "local_path": "dashboard.html" } }
```

This changes the extension's type to `kLegacyPackagedApp`.

`manifest_v2_util::IsExtensionAffected()` only tests `kExtension`,
`kLoginScreenExtension` and `kUserScript`, so a legacy packaged app (LPA) is
never considered for the MV2 shutdown.
`ManifestV2Handler::ShouldBlockExtensionEnable()` consequently returns false and
the extension loads with `disable_reasons: []`.

The same pass resolves `__MSG_*__` tokens in the manifest from
`_locales/<default_locale>/messages.json`. Chrome does not resolve them for this
install path, so without it the extensions page shows a literal
`__MSG_extShortDesc__` as the description.

### 2. `browserAction` shim in `js/webext.js`

An LPA cannot declare `browser_action`:

```jsonc
// extensions/common/api/_manifest_features.json
"browser_action": { "extension_types": ["extension"], "max_manifest_version": 2 }
```

and the API is only available when that key is present:

```jsonc
// chrome/common/extensions/api/_api_features.json
"browserAction": { "dependencies": ["manifest:browser_action"] }
```

Consequently, `chrome.browserAction` is `undefined`. uBO's `webext.js` builds an object
literal containing
`promisifyNoFail(chrome.browserAction, 'setBadgeBackgroundColor')`, and
`promisifyNoFail` does `const fn = thisArg[fnName]`, a property read on
`undefined`, which throws during module evaluation. This prevents `webext.js`
and every module that imports it from loading.

The shim is inserted above `promisifyNoFail`. It defines `chrome.browserAction`
with stub methods that do nothing except call a callback passed as the last
argument, so the promises that `promisifyNoFail` builds around them still
resolve. It is placed in `webext.js` because the dashboard, logger, settings and
whitelist all import that file. A change limited to `background.html` would not
prevent module-evaluation errors in those pages.

uBO accesses the API in several ways: an `instanceof Object` check in
`vapi-background.js`, direct calls such as `setIcon`, and
`onClicked` listener registration. An object satisfies all of these access
patterns.

### 3. `web_accessible_resources`

The installer adds `popup-fenix.html`, allowing the companion to embed uBO's
panel from its own origin. No other entry is added; uBO already ships
`/web_accessible_resources/*` of its own. Its stylesheets, scripts and images
load from the same origin once the panel is running, so they require no
additional entry. Under MV2 every listed resource is reachable by any website,
so this entry also lets any website embed the panel. Edit 4 blocks that.

### 4. `frame-ancestors` in `content_security_policy`

`popup-fenix.html` must be web-accessible for the companion to embed it,
which also permits any web page to frame it. uBO's panel contains a functional
power switch, so a hostile page could frame it and induce a click on the switch.
The installer appends:

```
frame-ancestors 'self' chrome-extension://<companion id>
```

Testing against Chrome 152 confirmed that, without the directive, an ordinary
HTTP page can frame the panel and access `#switch`. With the directive, the
frame resolves to `chrome-error://`, while the companion popup remains
functional.

### 5. `externally_connectable`

```json
"externally_connectable": { "ids": ["<companion id>"] }
```

The companion uses this to ask uBO for the badge count (edit 6). LPAs may
declare it: `_manifest_features.json` lists `legacy_packaged_app` among
the allowed `extension_types`, and `runtime.onMessageExternal` requires only
`contexts: ["privileged_extension"]`, with no restriction by type.

### 6. Badge bridge appended to `js/vapi-background.js`

The companion has a static icon and title, and refreshes its badge when a
tab is activated or finishes loading. uBO's dynamic `setIcon` and `setTitle`
calls are handled by the shim's no-ops and are not reflected in the companion.
Requests that uBO blocks after the tab has finished loading only show up in the
count at the next refresh.

A `runtime.onMessageExternal` listener responds to `{what:'uboBadge', tabId}`
with `µb.requestStats.blockedCount` and `pageStore.counts.blocked.any`, and
whether filtering is on for the page, which is `pageStore.isTrusted() === false`.
The companion's service worker requests these values on `tabs.onActivated` and
`onUpdated`, then sets its own badge for that tab.

### 7. Resize notifier: `js/popup-resize.js` plus a tag in `popup-fenix.html`

The companion's popup shows uBO's panel in an iframe, so Chrome cannot size the
popup to uBO's content automatically. This script reports uBO's dimensions to
the parent, which resizes the iframe.

It must be an **external file**: uBO ships
`content_security_policy: "script-src 'self'"`, which blocks inline `<script>`.

Two constraints on the measurement:

- **Width comes from CSS.** In a narrow viewport uBO adds a `portrait` class and
  the panel becomes fluid, so measuring it reports the iframe's current width
  and causes oscillation. A probe element resolves uBO's own
  `--popup-main-min-width` (`18em` at `--font-size: 14px` = 252px), which does
  not depend on the iframe.
- **Height comes from `#panes`**, which is content-sized. `scrollHeight` is
  `max(content, viewport)` and so depends on the viewport too.

The 10ms debounce coalesces uBO's mutation burst because `post()` forces layout
twice per call. The panel reaches the same final height with longer delays, so a
short delay avoids unnecessary latency.

### 8. Scriptlets through the companion's user scripts: `js/vapi-scripting.js`

uBO injects each site's scriptlets (`+js(...)` filters) with
`tabs.executeScript` when `webNavigation.onCommitted` fires. On Chromium that
arrives a few milliseconds after the page has started parsing, so an inline
script early in `<head>` runs first, and anti-adblock loaders depend on that.
Ad-Shield's bootstrap on index.hr saves the native `fetch`, `setAttribute` and
`addEventListener` when it starts, so a scriptlet that arrives later cannot stop
it, and `rmnt` empties the script only after it has run. Measured on index.hr
with Chrome 157, uBO's scriptlets arrived 3 to 7 ms after the bootstrap on every
load.

The companion is an MV3 extension, so it can use `chrome.userScripts`, which
injects registered code into the page's main world at `document_start`, before
any page script. The edit inserts `assets/userscripts-bridge.js` into
`vapi-scripting.js` and anchors six small changes on uBO's code:

- When uBO works out a site's scriptlets, it also sends the main-world part,
  wrapped in uBO's own trusted-site check, to the companion, which registers it
  for that host with `runAt: document_start` and `world: MAIN`.
- Once the companion confirms a site, navigations that start later get only the
  isolated-world part from `executeScript`, so the scriptlets run once. A frame
  whose own URL is not http(s), such as `about:blank`, still gets everything
  from `executeScript`, because a registered user script does not match it.
- When uBO drops a site from its cache it unregisters it, and when it resets
  its scriptlets (after a filter list update, a change to a site's switches, a
  logger toggle) or restarts, it unregisters all of them: the code embeds the
  trusted-site list and a secret that changes at those points.

The first navigation to a site after a reset still uses `executeScript` alone,
which is uBO's normal behaviour. The companion needs the `userScripts`
permission, host access to all sites, and its "Allow user scripts" toggle, which
Chrome stores as `user_scripts_enabled` in its Preferences entry. On Linux the
installer sets that pref the next time it edits the profile with Chrome closed;
on Windows `load` turns it on, and by hand it is one more click on the
companion's details page. When `chrome.userScripts` is unavailable, the
companion answers that it cannot help and uBO keeps injecting on its own.

Chrome applies a changed manifest of an unpacked extension only when it starts:
a reload from `chrome://extensions` reloads the scripts but keeps the stored
manifest. After an update that adds this edit, the companion has the new
permissions from the next Chrome start.

### `browser_action` is retained

It remains in uBO's manifest even though Chrome logs a warning ("only allowed for
extensions, but this is a legacy packaged app"), because uBO reads it:
`vapi-background.js` uses `getManifest().browser_action.default_title`, so
removing it prevents the background page from loading. The installer also
retains `commands`, for which Chrome emits the corresponding LPA warning.

### Extension identity

Both platforms use the same mechanism: an RSA key per extension is generated on
first install and stored in the install directory as `ubo.pem` and `comp.pem`,
with the public half embedded in each manifest as the `key` field. Chrome then
derives the extension ID from that key, exactly as it does for a packed
extension: sha256 of the SubjectPublicKeyInfo DER, first 16 bytes, nibbles
mapped onto `a`-`p`. The installer computes the same value, so each extension
can be given the other's ID before Chrome loads either manifest.

The key is generated with the `cryptography` package if present, otherwise by
invoking `openssl` as a subprocess. This avoids a pip dependency on Linux.
Windows uses `pip install cryptography`.

**Path-derived IDs are unsuitable.** Chrome derives an ID from the path for an
unpacked extension with no `key`:

```cpp
// extensions/common/extension.cc  InitExtensionID
if (!Manifest::IsUnpackedLocation(location) || creation_flags & REQUIRE_KEY)
  ...key path...
else
  return GenerateIdForPath(path);   // sha256(path)[:16] mapped onto a-p
```

This ties identity to the directory: moving or renaming it changes every ID,
but each extension still refers to the other by its old ID, and Chrome does not
report an error. The companion tries to embed uBO's panel under an ID that no
longer exists, and the popup stays empty. On Windows, the selected path comes
from the Load unpacked dialog.

With a stored key, identity is tied to the key file instead. An installed copy
keeps working without the key because the public half is already in its
manifest. Losing the key causes the next rebuild to generate a new identity, and
the rebuilt uBO starts with default settings. The `.pem` files preserve
extension identity, not uBO's configuration. They are unencrypted private keys,
so any copy must remain private.

## Windows

The manifest type change is platform-independent, but extension delivery differs
on Windows. Each automated route that was tested either did not install the
extensions, left them disabled, or put the browser under enterprise management.
Load unpacked gives an unmanaged, enabled installation, and `ubo-lpa.py load`
performs it over Chrome's DevTools connection after one click on Allow.

### Preference protection

On Linux, extension settings are stored without integrity protection in
`Preferences`, and the installer writes them directly. On Windows, they are
stored in `Secure Preferences`:

```
plain Preferences  has extensions.settings: False
Secure Preferences has extensions.settings: True
super_mac: 8F98F1FF6C75ECDC4866EDDD...
```

Each entry is individually MAC'd under `macs.extensions.settings`, keyed by
extension ID, with a `super_mac` over the whole protection block. Windows Chrome
also mirrors these into the registry at
`HKCU\Software\Google\Chrome\PreferenceMACs\Default`.

The difference comes from `GetSettingsEnforcementGroup()` in
`chrome_pref_service_factory.cc`:

```cpp
SettingsEnforcementGroup GetSettingsEnforcementGroup() {
  ...
#if BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC)
  return GROUP_ENFORCE_DEFAULT;
#else
  return GROUP_NO_ENFORCEMENT;
#endif
}
```

`GROUP_ENFORCE_DEFAULT` outranks `GROUP_ENFORCE_ALWAYS_WITH_EXTENSIONS_AND_DSE`,
so `extensions::pref_names::kExtensions` gets `ENFORCE_ON_LOAD`. macOS is in the
same branch. Computing valid MACs is possible; this project does not do so
because the mechanism prevents software from adding extensions without user
authorization.

### Route comparison

| route | installs | enabled | notes |
|---|---|---|---|
| `Preferences` injection | no | n/a | non-authoritative file; settings are MAC-enforced |
| `--load-extension` | no | n/a | ignored by branded Chrome |
| registry + local CRX | yes | **no** | `DISABLE_NOT_VERIFIED`, toggle greyed |
| policy forcelist | yes | yes | managed banner, non-removable, admin |
| **Load unpacked** | yes | yes | manual; requires Developer mode |
| DevTools `Extensions.loadUnpacked` | yes | yes | gone at Chrome's next start |
| **`load`: Load unpacked over DevTools** | yes | yes | one click on Allow; persists |

### Load unpacked over DevTools: `load`

Chrome 136 and later accept a DevTools connection to the default profile when
"Allow remote debugging for this browser instance" is on in
`chrome://inspect/#remote-debugging`, and then ask in an "Allow remote
debugging?" prompt before each connection. The setting is
`devtools.remote_debugging.user-enabled` in `Local State`, which Chrome reads
when it starts and writes back when it exits. `load` therefore:

1. Connects directly if Chrome already listens (`DevToolsActivePort` in the
   user data directory names a port that accepts connections).
2. Otherwise, with Chrome closed, sets the value in `Local State` and starts
   Chrome. With Chrome running, it asks once to restart it, closes it as its
   window's close button does, sets the value and starts it with
   `--restore-last-session`. It cannot open `chrome://inspect` in the running
   Chrome instead: Chrome ignores `chrome://` addresses on its command line,
   both at start and when handed to a running instance.
3. Waits for the user's click on Allow: Chrome answers the WebSocket upgrade
   with 101 for Allow and 403 for Cancel.

On the connection, `load` opens `chrome://extensions` and calls the page's own
`chrome.developerPrivate` API: `updateProfileConfiguration({inDeveloperMode:
true})`, then `loadUnpacked()` for each folder, and
`updateExtensionConfiguration({pinnedToToolbar: true})` for the button.
`loadUnpacked()` opens Chrome's folder picker, a Win32 common dialog; `load`
finds it among Chrome's windows, sets the folder in its file name box (control
`0x480` in Wine's dialog, the `Edit` in the `ComboBoxEx32` `0x47C` in Windows')
and sends `IDOK`. Then it waits until `getExtensionsInfo()` lists both as
enabled: Chrome lists an extension as disabled, with the reason `reloading`,
while it loads it again. Finally, if it switched remote debugging on, it turns
it off from `chrome://inspect` as the page's checkbox does,
`chrome.send('set-remote-debugging-enabled', [false])`, and Chrome closes the
connection.

DevTools has a direct `Extensions.loadUnpacked(path)`, and retail Chrome 157
accepts it on a consent connection without `--enable-unsafe-extension-debugging`.
Chrome loads extensions that way for the current session only: after a restart
they are gone, as with `--load-extension`. A drop of the folder onto
`chrome://extensions` simulated with `Input.dispatchDragEvent` reaches the page
as a directory, but the browser answers `loadUnpacked({useDraggedPath: true})`
with "No dragged path": it records the path only for a drag from the operating
system.

`load` was tested with retail Windows Chrome 157 under Wine 11.22: from a fresh
profile, from a running Chrome with and without remote debugging, across a
restart, and with a second run that finds both loaded and does not prompt. On
the GitHub Actions Windows runner, `tests/windows/run.py` and
`tests/windows/msi_test.py` run it with the runner's retail Chrome, with UI
Automation clicking Allow, and check that Chrome showed exactly one prompt and
kept both extensions after closing.

### `--load-extension`

Google Chrome ignores this option and emits the following warning when logging
is enabled:

```
WARNING:chrome\browser\extensions\extension_service.cc:419]
  --load-extension is not allowed in Google Chrome, ignoring.
```

The behavior of other Chromium-based browsers was not tested.
Without `--enable-logging=stderr`, Chrome does not expose this warning, so the
option appears to be ignored without explanation.

### Registry + local CRX

Writing `HKCU\Software\Google\Chrome\Extensions\<ID>` with `path` and `version`
installs the CRX at `location: 3` (`kExternalRegistry`), unpacked into the
profile. It remains disabled with `disable_reasons: [256]`
(`DISABLE_NOT_VERIFIED`, `1<<8`) and the enable toggle rendered
`disabled=true aria-disabled=true`.

The user cannot enable it from the UI. `InstallVerifier::MustRemainDisabled`
defines four exemptions:

```cpp
if (!CanUseExtensionApis(*extension))        return false;
if (Manifest::IsUnpackedLocation(location))  return false;
if (location == kComponent)                  return false;
if (AllowedByEnterprisePolicy(id) && ...)    return false;
```

A registry CRX does not match any of them, and the check cannot be turned off
from the command line:
`GetStatus()` is `max(GetExperimentStatus(), GetCommandLineStatus())`, and

```cpp
#if BUILDFLAG(GOOGLE_CHROME_BRANDING) && (BUILDFLAG(IS_WIN) || BUILDFLAG(IS_MAC))
  return VerifyStatus::ENFORCE;
#else
  return VerifyStatus::NONE;
#endif
```

means `--extensions-install-verification` can only raise the level, and Linux
returns `NONE` throughout.

Developer mode does not change this result. It governs
`DISABLE_UNSUPPORTED_DEVELOPER_EXTENSION` (`1<<24`), a different bit for a
different check. With Developer mode enabled, `disable_reasons` stayed at
`[256]`, both when the extensions page first rendered and after a browser
restart.

### Enterprise policy route

Enterprise policy can install both extensions without UI interaction. This
route requires administrator access, an update manifest served over HTTP, and
packed and signed CRX files.

```
HKLM\SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist
  1 = <ubo-id>;http://127.0.0.1:8600/ubo_update.xml
  2 = <comp-id>;http://127.0.0.1:8600/comp_update.xml
```

with one manifest per extension, each listing that extension's own version
(uBO `1.74.0`, the companion `1.0`). Each URL must be reachable; HTTPS is also
supported:

```xml
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='ID'>
    <updatecheck codebase='http://127.0.0.1:8600/x.crx' version='VERSION' />
  </app>
</gupdate>
```

Verified under Wine: both extensions were installed at `location: 7`
(`kExternalPolicyDownload`) with `disable_reasons: []` and were enabled. That
is all this test checked. The browser was a Chrome for Testing build with its
own banners, so the result does not show what the retail Stable interface looks
like.

Two implementation details allow this route to succeed where the registry route
fails. Plain HTTP is accepted because `extension_downloader.cc` only requires
SSL for the Web Store update URL. Locally signed CRX3 files are also accepted:
`GetPolicyVerifierFormat()` returns plain `CRX3`, not the
`CRX3_WITH_PUBLISHER_PROOF` that webstore installs require.

Google's documented requirements extend beyond administrator access: forced
installation of an off-store extension on Windows requires a domain-joined or
Entra-joined machine, or Chrome Enterprise Core enrolment. Direct registry
configuration succeeded in testing; production deployment should follow
Google's policy documentation.

Trade-offs include the "Your browser is managed by your organization" notice,
the absence of a **Remove** button, and automatic reinstallation after deletion.

A `HKCU\...\Chrome\Extensions\<ID>` entry for the same ID overrides the policy
provider, producing the disabled registry install instead. The log reports it:

```
Extension id ... was entered for update more than once.
  old location: kExternalPolicyDownload  new location: kExternalRegistry
```

Delete the registry entries first.

### Test environment

The current browser harness uses retail Chrome 152.0.7977.65 under Wine 11.16.
It loads both extensions through the Win32 Load unpacked picker, restarts the
browser and checks that both are still enabled, then tests request blocking,
the badge, the popup, and that an ordinary page cannot frame uBO's panel. It
records screenshots along the way. Wine remains a compatibility layer; these
results do not replace validation on native Windows.

The separate enterprise-policy experiment used Chrome for Testing
151.0.7922.138 under Wine 11. It only checked that the extensions were installed
and enabled, not how the retail browser's interface behaves.

## Troubleshooting

Start with `check`. It compares both extensions with each other and with the
expected patches, and reports each inconsistency.

```bash
python3 ubo-lpa.py check
```

---

### uBO is installed but does not block requests

**The profile was left out because Chrome was running (Linux).** Chrome writes
`Preferences` back on exit, so the installer only adds the extensions to a
profile while its browser is closed, and names the profiles it left out. Chrome
started during an install can still overwrite the file. Close Chrome and run
`install` again.

**Filter lists are still downloading.** A new profile fetches uBO's initial
filter-list data. Wait a minute and reload the page.

**uBO is switched off for that site.** Open the popup and check the power button.

### The toolbar button is missing

**Linux:** the companion is MV3 and unpacked, so Chrome disables it with
`DISABLE_UNSUPPORTED_DEVELOPER_EXTENSION` (`1<<24`) unless developer mode is on,
and the installer sets `extensions.ui.developer_mode` for that reason. uBO is
exempt as a legacy packaged app, so with developer mode off, uBO still blocks
requests but the toolbar button does not appear.

**Either platform:** the button appears in the puzzle-piece menu until pinned.
Click the puzzle icon, then the pin.

### The popup is empty or the wrong size

The popup embeds uBO's panel in an iframe. An empty popup indicates that the
iframe did not load.

- `check` reports whether `popup-fenix.html` is missing from uBO's
  `web_accessible_resources`, or whether the companion references the wrong uBO
  ID.
- A gap around the panel means the resize notifier is not running. Confirm
  `js/popup-resize.js` exists in the uBO directory and that `popup-fenix.html`
  references it. It must be an external file because uBO ships
  `content_security_policy: "script-src 'self'"`, so an inline script is silently
  blocked.

### Extensions are disabled after a Chrome update

Inspect the error reported by Chrome:

```
chrome://extensions  ->  Details  ->  (any error text)
```

If uBO shows an MV2 deprecation message instead of loading, the relevant type
check may now include `kLegacyPackagedApp`. The current technique is
incompatible with that change.

A missing extension on Linux may indicate that the profile was reset. Run
`install` again.

### Windows: "This extension is not listed in the Chrome Web Store"

This is the registry install route, which ends in `DISABLE_NOT_VERIFIED` with the
enable toggle greyed out, and developer mode does not clear it. Remove the
entries under `HKCU\Software\Google\Chrome\Extensions\` and use **Load
unpacked**. See [Windows](#windows).

### Windows: `--load-extension` is ignored

Branded Google Chrome ignores the option and logs:

```
--load-extension is not allowed in Google Chrome, ignoring.
```

Chrome logs it only with `--enable-logging=stderr`, so otherwise the flag appears
to be ignored silently. Other Chromium-based browsers were not tested here.

### Windows: extensions are removed after restart

Force-installed extensions are uninstalled on the next start once their policy is
removed. Load unpacked installs persist without a policy.

### uBO pages report errors

Open the dashboard or logger and check the console. This error

```
Uncaught TypeError: Cannot read properties of undefined (reading 'setBadgeBackgroundColor')
    js/webext.js:26
```

means that the `browserAction` shim is missing from `js/webext.js`. Run
`install` again; `check` reports this as `browserAction shim`.

### Reporting bugs

The installed uBO is patched by this project, not released by the uBlock Origin
project. First check whether the problem also occurs with an unmodified uBO. If
it does, report it to uBlock Origin. If it occurs only with the patched uBO,
report it in this repository with the output of `check`.
