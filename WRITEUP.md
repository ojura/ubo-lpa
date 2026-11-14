# Technical design and implementation

This document describes the workaround, the edits the installer makes, why the
Windows install goes through Load unpacked, and how to troubleshoot problems.

## What the installer changes

Symbols below refer to uBlock Origin 1.76.2. Line numbers vary between
releases, so they are omitted. If the code a patch is anchored to has moved,
the installer discards the staged build and leaves the previous installation
unchanged.

`check` verifies required files, the two IDs and their cross-references, and
the marker strings and manifest entries the patches add. It is not a
byte-for-byte or semantic comparison.

The installer applies nine edits to uBlock Origin and generates a companion
extension. Each rebuild extracts a fresh copy of uBO and applies all nine
edits. A given upstream release, key pair, token (edit 8) and installer version
therefore always produce the same result. The only exception is the companion's
manifest version, which is new at every build (see
[The companion extension](#the-companion-extension)). `ubo-lpa.py check`
verifies the result.

`install` rebuilds when a newer uBO release is available, when `check` finds a
problem, and when the installed copy was built by another version of the
installer. To detect the third case, the installer stores a hash of the files
in `assets/`, of the functions that patch uBO and build the companion, and of
the constants they read (`recipe()`). This document calls that hash the recipe.
`install` compares the stored recipe with its own and rebuilds when they
differ. This is how a change made in this repository reaches an installed copy.
Running `install` on a current, consistent installation rebuilds only after a
confirmation prompt.

An update replaces the extension and companion directories in place and leaves
the profile entries as they are: those written by the Linux installer, and
those Load unpacked created on Windows. The install path and the keys stay the
same, so the IDs and paths in Chrome's preferences remain valid. Chrome reads
an unpacked extension's manifest from disk at every start
(`ShouldReloadExtensionManifest()` in `installed_loader.cc`), and it does not
disable an unpacked extension whose new version asks for more permissions
(`CheckPermissionsIncrease()` in `chrome_extension_registrar_delegate.cc`).
Updates therefore run while Chrome is open, and on Windows Load unpacked, by
hand or through `load`, is needed only for the first install. Until Chrome
restarts, the running uBO background page and the companion's service worker
keep the old code, while pages opened in the meantime, such as the popup and
the dashboard, load the new files. The companion's popup compares the recipe
and uBO's version in its own file with the ones that uBO's background page
reports, and the recipe with the one that the worker reports. While they
differ, it shows "uBlock Origin was updated. Chrome runs the previous version
until it restarts." (see [The companion extension](#the-companion-extension)).

Windows refuses to rename a directory while a file in it is open, and a virus
scanner checking the new files, or Chrome reading them, can hold one open for
a moment. The installer retries each rename for up to ten seconds. If a rename
still fails, it moves the previous directories back and exits with status 1,
and the next run tries again.

The installer edits a profile only when its entries need changing, and only
while its browser is closed. To tell whether the browser is running, it reads
the `SingletonLock` symlink, which Chrome keeps in its user data directory
while it runs and which points to `<hostname>-<pid>`, and checks whether that
process still exists.

The payloads live in `assets/` as files, with placeholders such as
`__UBO_ID__`, `__COMP_ID__`, `__RESIZE_MSG__`, `__TOKEN__` and `__RECIPE__`
substituted at install time.

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

### 2. Shim for `browserAction` and `commands` in `js/webext.js`

An LPA cannot declare `browser_action` or `commands`:

```jsonc
// extensions/common/api/_manifest_features.json
"browser_action": { "extension_types": ["extension"], "max_manifest_version": 2 }
"commands":       { "extension_types": ["extension", "platform_app"] }
```

and each API is only available when its key is present:

```jsonc
// chrome/common/extensions/api/_api_features.json
"browserAction": { "dependencies": ["manifest:browser_action"] }
"commands":      { "dependencies": ["manifest:commands"] }
```

Consequently, `chrome.browserAction` and `chrome.commands` are `undefined`.
uBO's `webext.js` builds an object literal containing
`promisifyNoFail(chrome.browserAction, 'setBadgeBackgroundColor')`, and
`promisifyNoFail` does `const fn = thisArg[fnName]`, a property read on
`undefined`, which throws during module evaluation. This prevents `webext.js`
and every module that imports it from loading. Without `chrome.commands`,
`js/commands.js` returns before it registers its listener.

The shim is inserted above `promisifyNoFail`. It is placed in `webext.js`
because the dashboard, logger, settings and whitelist all import that file. A
change limited to `background.html` would not prevent module-evaluation errors
in those pages.

- `chrome.browserAction` gets methods that only call a callback passed as the
  last argument, so the promises that `promisifyNoFail` builds around them
  still resolve. uBO accesses the API in several ways: an `instanceof Object`
  check in `vapi-background.js`, direct calls such as `setIcon`, and
  `onClicked` listener registration. An object satisfies all of these. Edit 6
  sends the values that uBO would show on its button to the companion.
- `chrome.commands` gets an `onCommand` event, which stores the listener that
  `js/commands.js` registers. The companion declares the commands and passes
  each one on to uBO, where edit 6 calls that listener.

### 3. `web_accessible_resources`

The installer adds `popup-fenix.html` and `logger-ui.html`. The companion
embeds these two documents in its own pages: uBO's panel in its popup, and
uBO's logger in its side panel. uBO's manifest already lists
`/web_accessible_resources/*`, and the installer does not add any other entry.
Once one of the two documents is running, its stylesheets, scripts and images
load from the same origin, so they do not have to be listed. Under MV2 every
listed resource is reachable by any website, so these entries also let any
website embed the two documents. Edit 4 blocks that.

### 4. `frame-ancestors` in `content_security_policy`

`popup-fenix.html` and `logger-ui.html` have to be web-accessible so that the
companion can embed them, and that also lets any web page frame them. uBO's
panel contains a functional power switch, so a hostile page could frame the
panel and induce a click on the switch. The logger has controls that create
filters and rules, and a hostile page could aim clicks at those in the same
way. The installer appends:

```
frame-ancestors 'self' chrome-extension://<companion id>
```

Testing against Chrome 152 confirmed that, without the directive, an ordinary
HTTP page can frame the panel and access `#switch`. With the directive, the
frame resolves to `chrome-error://`, while the companion's popup remains
functional. In a test with Chrome 157, the logger behaved the same way: framed
by a web page, it resolved to `chrome-error://`, and the companion's side panel
still showed it.

### 5. `externally_connectable`

```json
"externally_connectable": { "ids": ["<companion id>"] }
```

uBO and the companion message each other through `runtime.sendMessage` with the
other's ID (edits 6 and 8). LPAs may declare the key: `_manifest_features.json`
lists `legacy_packaged_app` among the allowed `extension_types`, and
`runtime.onMessageExternal` requires only `contexts: ["privileged_extension"]`,
so it is available to every extension type. The companion lists uBO's ID in its
own manifest, and both sides check the sender's ID.

### 6. Companion bridge appended to `js/vapi-background.js`

The bridge is uBO's side of the exchange with the companion. It is appended to
`vapi-background.js`, so it shares that module's scope.

- **The companion's questions.** A `runtime.onMessageExternal` listener answers
  `{what:'uboBadge', tabId}`. The answer includes the counts
  `µb.requestStats.blockedCount` and `pageStore.counts.blocked.any`, and
  whether filtering is on for the page (`pageStore.isTrusted() === false`). It
  says whether uBO shows a count on its badge (`showIconBadge`), whether uBO is
  ready to filter (`µb.readyToFilter`), and whether its request listener is
  installed. It also says whether uBO's own pages can connect to the background
  page yet. They can once `js/messaging.js` has set
  `vAPI.messaging.defaultHandler`. Finally, the answer includes an identifier
  of this run of the background page, uBO's version and the recipe of the
  build. The worker uses the answer to tell whether uBO is running. The
  companion's pages use it to tell when they can load one of uBO's pages in a
  frame, and the popup also uses it to tell whether Chrome still runs the
  previous build.
- **Toolbar state.** uBO sets its button's icon, badge, badge colour and title
  for a tab in `vAPI.setIcon()`. It sets the button's default state in
  `vAPI.setDefaultIcon()`, which it calls while it loads. Both functions end in
  `chrome.browserAction` calls, which reach the shim of edit 2 and do nothing
  there. The bridge wraps both functions and sends the same values to the
  companion, which applies them with `chrome.action`. Each message includes the
  identifier of the run. When the identifier changes, the worker knows that uBO
  has restarted. uBO calls `vAPI.setIcon()` for a tab when its background page
  is idle, and at the latest 0.7 s after a change. The count on the button
  therefore follows the page while it loads. On a trusted site the button shows
  uBO's "off" icon. The bridge works out the badge and the title the way
  `vAPI.setIcon()` does, and the installer checks that those lines of uBO are
  still the ones the bridge was written against.
- **Keyboard commands.** For a message `{what:'command', command}`, the bridge
  calls the listener that `js/commands.js` registered with the shim of edit 2.

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
script early in `<head>` runs first, and anti-adblock loaders rely on that.
Ad-Shield's bootstrap on index.hr saves the native `fetch`, `setAttribute` and
`addEventListener` when it starts, so a main-world scriptlet that arrives later
cannot stop it. uAssets also has a rule that removes the bootstrap itself,
`rmnt, script, /HTMLIFrameElement|JSON\[|_contentWindow/`. `rmnt`
(`remove-node-text`) is an isolated-world scriptlet. It only works on a script
that the parser reaches after the scriptlet has been installed. Measured on
index.hr with Chrome 157 and uBO's own injection, the scriptlets arrived 3 to
7 ms after the bootstrap on every load in a browser with about 120 tabs. In a
fresh profile, the script that the rule targets ran in 5 of 6 loads. With the
scripts registered as described below, it ran in 0 of 8 loads, and the
main-world code ran once in each document.

The companion is an MV3 extension, so it can use `chrome.userScripts`, which
runs registered code at `document_start`, before any page script. The edit
inserts `assets/userscripts-bridge.js` into `vapi-scripting.js` and makes four
small changes to uBO's code in that file. It also adds one statement to
`getContentScripts()` in `scriptlet-filtering.js`.

**What is registered.** When uBO works out the scriptlets of a hostname that
has scriptlet filters of its own, it sends the companion two scripts for that
hostname. Each script begins with the two checks of the top frame that uBO
puts in front of its scriptlets. On a trusted site, and on a site that a
dynamic filtering rule allows, the checks stop the script before it runs a
scriptlet. The two scripts are:

- the main-world scriptlets, registered with `world: MAIN`;
- the isolated-world scriptlets, registered with `world: USER_SCRIPT`. Like
  uBO's own isolated world, the user script world is separate from the page.
  With `userScripts.configureWorld`, the companion sets the CSP of that world
  to one that allows scripts and objects from any source, inline scripts and
  `eval`. uBO's isolated world does not have a CSP, and its scriptlets rely on
  that, for example to insert an element with an inline event handler.

Three kinds of code stay in uBO's own isolated world. Scriptlets that call
extension APIs stay, because the user script world does not have those APIs.
The bridge finds them by searching uBO's scriptlet registry, including each
scriptlet's dependencies, for `chrome.` and `browser.` calls. In uBO 1.76 it
finds `trusted-click-element`, which opens closed shadow roots with
`chrome.dom`. Scriptlets imported from user resources stay too, because what
they call is unknown. The relay that passes the scriptlets' log lines to uBO's
logger also stays, because it calls `vAPI.messaging`. uBO's isolated-world code
lists its scriptlet functions in one statement, and the bridge edits that
statement in both copies. In the registered copy, an empty function replaces
each function that stays in uBO's world. The copy uBO injects runs every
function when the registered copy did not run in the document, and only the
functions that stay when it did.

**Which hostnames are registered, and how often the set changes.** Chrome keeps
an extension's registered scripts as one stored value. Every change writes all
of them to the profile and sends all of them to every tab's process again. One
hostname's two scripts are 140 to 180 KB with uBO 1.76.2's default lists,
because uBO puts its scriptlet library into each. In a measurement with
Chrome 157, 63 hostnames were registered with 250 KB of scripts per hostname,
and one more registration wrote 21 MB to disk. That is 0.33 MB per registered
hostname. With scripts of 145 KB per hostname, a registration took about 0.7 ms
per registered hostname. The bridge therefore keeps the set small and changes
it in as few steps as possible:

- Only a hostname with scriptlet filters of its own is registered. These are
  filters for the hostname, for a parent domain or for an entity, and regex or
  path filters that match the hostname. Every hostname's code also includes the
  filters that apply everywhere and the ones for frames inside other sites.
  uBO's built-in lists have one filter that applies everywhere, against pages
  that put a malicious command line into the clipboard. A hostname that has
  only such filters is not registered, and uBO's own injection runs them there,
  as in an unpatched uBO. Registering every hostname would write the whole set
  again at each new hostname. One registration for all hostnames, holding the
  filters that apply everywhere, would not come from uBO's lookup for a
  hostname. uBO applies the exceptions to such a filter in that lookup, so the
  bridge would have to reproduce them as exclusion patterns. The bridge tells
  the two kinds of hostname apart by the hostname parts of the filters that uBO
  collected for the hostname. The statement added to `getContentScripts()`
  passes those on.
- At most 48 hostnames are registered. When one more arrives, the bridge
  unregisters the hostname whose scriptlets uBO worked out longest ago.
- A hostname whose code has not changed is not sent again.
- After uBO has reset its scriptlets (a filter list update, a change to a
  site's switches, a logger toggle), the registered scripts are dropped at
  once, because they were made from the previous state. A second after the last
  reset, uBO works out the known hostnames' scriptlets again, and the bridge
  sends them in one message. For this the bridge calls uBO's own
  `registerContentScript()`, which also keeps the copy uBO injects at the next
  navigation. The registered copy and the injected one then come from one call
  of `getContentScripts()`. This matters while uBO's logger is open, because
  each call then puts a new secret into the code. The scriptlets' log lines
  reach the logger only when both copies have the same secret. Measured at the
  first load after a reset, a `set` scriptlet produced all three of its logger
  lines when both copies came from one call, and one of the three when each
  copy came from a call of its own.
- At uBO's start the registered scripts stay in place (see below). The
  companion reports their hostnames, and a second later uBO works out those
  hostnames' scriptlets and the bridge sends them in one message.

**uBO's own injection.** uBO still injects the scriptlets on every navigation,
as an unpatched uBO does. The injected code first dispatches a cancelable event
for the main-world scriptlets and another for the isolated-world ones. A
registered script that ran its scriptlets in that document cancels the event
with `preventDefault()`. `dispatchEvent()` then returns `false`, also across
worlds, and the injected code skips those scriptlets. Each scriptlet therefore
runs once in a document, whatever is registered at that moment. That holds when
the registration arrived during the navigation, when the companion was reloaded
or its "Allow user scripts" was switched off, and when the scripts were
registered in an earlier browser session. A registered script that uBO's
checks of the top frame stopped does not cancel the event, and the injected
code then decides by its own copy of those checks.

The event names contain a token that the installer generates once for the
installation and keeps as `token` in `.state.json`. A page cannot learn the
token. The registered main-world script saves the functions it uses
(`addEventListener`, `dispatchEvent`, `preventDefault`,
`stopImmediatePropagation`, `CustomEvent`, `Reflect.apply`) before any page
script runs, and page code cannot reach that script. The injected code
dispatches from uBO's isolated world, and a page cannot replace that world's
functions.

**Registrations are kept across restarts.** Chrome stores registered user
scripts in the profile, and at its next start it runs them before any extension
has started. The tabs Chrome restores therefore get the scriptlets of their
last session at `document_start`, before uBO's background page exists. In one
start, a restored page was requested 0.29 s after launch, uBO's page started at
0.33 s, and the restored page's first script had been emptied (measured). When
uBO starts, the companion tells it which hostnames are registered, and uBO
replaces their scripts with current ones. The companion drops the registered
scripts when uBO's first message reports another token or another recipe. It
also drops them when uBO does not answer (see
[The companion extension](#the-companion-extension)).

Code registered in an earlier session contains that session's secret for uBO's
web-accessible resources. In a tab that loads before uBO has replaced the code,
`prevent-xhr` cannot serve one of those resources as the response to a request.

**Incognito.** uBO runs a second instance for incognito windows. That instance
does not register any scripts, because registrations are stored on disk and
would record the sites visited in incognito windows. The scripts that the
regular instance registered run in incognito tabs too, and the incognito
instance's injected code skips the scriptlets they ran.

The companion's manifest declares the `userScripts` permission and host access
to all sites, and its "Allow user scripts" toggle has to be on; Chrome stores
the toggle as `user_scripts_enabled` in the companion's entry in Preferences.
On Linux the installer sets that pref the next time it edits the profile with
Chrome closed; on Windows `load` turns it on, and by hand it is one more click
on the companion's details page. While `chrome.userScripts` is unavailable, uBO
injects all scriptlets itself, as an unpatched uBO does. The companion's popup
then says that the toggle is off, and offers a button that opens the
companion's details page.

### 9. A content script for frames without an http(s) URL: `js/ubolpa-frames.js`

A page script can create an empty iframe and take the browser's unmodified
functions from its window. The scripts registered through `chrome.userScripts`
do not match a frame without an http(s) URL of its own, such as `about:blank`,
`about:srcdoc` or `blob:`. uBO's `tabs.executeScript` reaches an `about:` frame
a few milliseconds after the script that created the frame has continued, and
does not reach a `blob:` frame at all. In measurements, the scriptlet was
missing in a new empty frame right after `appendChild()` and in a 0 ms timeout,
and present from 5 ms on. In a `blob:` frame it never arrived.

Chrome runs a declared `document_start` content script in such a frame at once.
For a new empty iframe, the script runs while `appendChild()` is still running.
The installer adds such a script to uBO's manifest in an entry of its own, with
`match_origin_as_fallback: true`, which makes it cover `blob:` frames too. The
entry is separate because uBO's own content scripts are not meant to run in
`blob:` frames. In a frame without an http(s) URL, the content script
dispatches an event on the window of the nearest http(s) ancestor. The
main-world script registered there (edit 8) answers with an event that contains
its own source, and the content script inserts that source as an inline script.
The isolated-world scriptlets do not reach the frame this way. The content
script cannot ask an ancestor of another origin, and it leaves such a frame to
uBO's own injection. An inline script that an MV2 content script inserts runs
whatever CSP the page has, because an MV2 isolated world does not have a CSP of
its own. The event names contain the token of edit 8.

One case is out of reach of any content script. An iframe with a URL starts
with an initial empty document, and a page script can read the frame's window
before the frame's navigation has committed. Chrome does not run content
scripts in that initial document. uBO's `trusted-prevent-dom-bypass` scriptlet
exists for this case, and filter lists apply it per site.

### `browser_action` and `commands` are retained

Chrome logs a warning for each of the two keys in uBO's manifest:
"'browser_action' is only allowed for extensions, but this is a legacy packaged
app." and "'commands' is only allowed for extensions and packaged apps, but
this is a legacy packaged app." Both keys stay anyway, because they are read.
`vapi-background.js` uses `getManifest().browser_action.default_title`, so
without `browser_action` the background page does not load. The installer
copies uBO's `commands` into the companion's manifest.

### The companion extension

The companion is an MV3 extension, so Chrome lets it have a toolbar button. Its
popup shows uBO's panel in an iframe. The companion also does for uBO the other
things an LPA may not do itself: it passes on uBO's keyboard commands,
registers uBO's scriptlets as user scripts, keeps a `declarativeNetRequest`
rule for the launch guard and shows uBO's logger in a side panel.

**Split incognito.** uBO declares `"incognito": "split"`, and so does the
companion. Each extension then has one instance for regular windows and one for
incognito windows. Messages between the two extensions stay within one profile,
so each instance of the companion exchanges messages with uBO's instance for
the same kind of window. In an incognito window, the popup shows the panel of
uBO's incognito instance, the toolbar state comes from that instance, and the
keyboard commands go to it. That is the instance that filters the incognito
tabs.

**The worker's file name contains a hash of its content, and the version is new
at every build.** Chrome stores the service worker script when it registers the
worker, and runs that copy from then on. An update replaces the files while the
extension stays loaded. At its next start, Chrome registers the worker again
only when the extension's version differs from the one it recorded
(`ServiceWorkerTaskQueue::ActivateExtension`). That registration fetches the
script only when its URL differs from the registered one: for the same URL,
`ServiceWorkerRegisterJob` resolves at once. A changed worker under the same
file name and version would therefore never run. Measured with Chrome 157, such
a worker did not run, also across restarts and with the clock set two days
ahead. For this reason the installer names the file
`sw-<hash of its content>.js` (`worker_file_name()`).

A new file name is not enough. With a new file name and the recorded version,
the worker does not start at all. Chrome does not register the worker again,
and it refuses to start the registered one, because that worker's file is not
the one in the manifest (`AllowServiceWorker()` in
`chrome_content_browser_client_extensions_part.cc`). The version cannot be
derived from the content either, because the version Chrome has recorded is
usually that of an earlier build. Chrome records the version when it stores the
new registration (`OnRegistrationStoredSync` in
`service_worker_task_queue.cc`). When a worker start fails before then, Chrome
skips that step (`OnWorkerStartFail`). After an update, such a start does fail.
An event that waited for the registration starts the registered worker, which
is still the previous build's, and Chrome refuses to start it. This was
measured with builds A, B and A installed in turn, each followed by a Chrome
start. After B's start the profile still recorded A's version. With A's files
back, Chrome did not register the worker again, and the worker did not run.
`build_version()` therefore makes the version from three random numbers, so
that it differs from every earlier build's. A rebuild that leaves the worker as
it is gets a new version and keeps the file name. Chrome then registers the
worker again, and the registration resolves at once because the URL has not
changed.

In the first start after an update, Chrome also drops the events that waited
for the registration, `runtime.onStartup` among them (measured). The worker
therefore does not rely on that event. Whenever it finds its session storage
empty, it asks uBO and sets up its state for the browser session.

The Linux installer does not put a copy of the manifest into the profile's
entry, and Chrome does not keep one there for an unpacked extension either.
With a copy there, Reload on `chrome://extensions` reads the copy instead of
the file on disk. After an update the copy points to a worker file that has
been deleted, and the reload fails with "Service worker registration failed"
until Chrome restarts (measured). Entries that earlier versions of the
installer wrote have such a copy, and the installer removes it the next time it
edits the profile with Chrome closed.

A newly installed worker normally waits until the pages that the previous
worker controls are closed. Chrome can restore such a page with the session,
for example a tab that was waiting in `wait.html`. Chrome does not start the
previous worker then either, because its file is not the one in the manifest.
Until that tab was closed, the button and the commands did not work, and uBO
could not change the registered scriptlets (measured). The worker therefore
calls `skipWaiting()` when it is installed.

**Toolbar state.** The button shows what uBO sends for each tab (edit 6): the
count, the "off" icon, the title. The companion adds two states of its own:

- A tab that began loading before uBO's request listener was installed gets
  uBO's "!" badge, and its title includes uBO's text "Could not filter properly
  at browser launch. Reload the page to ensure proper filtering." uBO itself
  shows that badge and text for a tab whose requests reached it before its
  listener was installed. uBO does not know about requests made before its
  background page had loaded, so the companion marks those tabs. uBO tells the
  companion only once that its request listener is installed, and the worker
  keeps that fact in session storage. Chrome empties session storage when it
  starts and when the companion is loaded again (Reload on
  `chrome://extensions`), so a worker that finds it empty asks uBO.
- When the worker has concluded that uBO is not running, the button shows the
  "off" icon and the title "uBlock Origin is not running". The worker also
  removes the registered scriptlets, so that they stop running in pages. The
  worker asks uBO when it first runs in a browser session, and when one of the
  companion's pages reports that uBO did not answer it. It also asks when a tab
  is activated or starts to load, unless this run of the worker has heard from
  uBO in the last minute. If uBO's request listener was installed and uBO does
  not answer, the worker asks once more five seconds later. If the listener was
  not installed yet, uBO gets a minute, to allow for a browser start with many
  tabs to restore. The worker keeps the time of the first unanswered question
  in session storage and compares it with the clock each time it asks again. A
  timer in the worker would not work, because Chrome stops the worker
  30 seconds after its last event, also while it keeps asking uBO on a timer
  (measured).

**Keyboard commands.** The companion declares the commands from uBO's manifest
and passes each one on to uBO (edits 2 and 6). Their shortcuts are set at
`chrome://extensions/shortcuts`, under "uBlock Origin toolbar button".

**Launch guard.** At a browser start, Chrome loads the tabs it restores before
uBO's background page exists. In a test profile, the restored page was
requested about 0.3 s after launch, and uBO's page started at 0.3 to 0.4 s and
then loaded its filters. The page's advertising request reached the server in
every start. Chrome does not hold requests back until an MV2 background page
has started. uBO has a setting for this case, "Suspend network activity until
all filter lists are loaded" in its Filter lists pane. It is off by default on
Chromium, and on Chromium uBO can only act on it once its own page runs.

While that setting is on, the companion applies it to the time before uBO's
background page runs. It keeps a dynamic `declarativeNetRequest` rule, and
Chrome applies such a rule by itself from its start, without running any
extension. An LPA may not use that API, but an extension may. The rule
redirects every http(s) page navigation to the companion's
`wait.html#<address>`. That page asks uBO until uBO's request listener is
installed. It then adds a session rule that allows page navigations again, and
loads the address. Chrome keeps session rules in memory only, so its next start
is guarded again.

uBO reports the setting with each toolbar message for a tab, and with the
default state it sends once its request listener is installed. The companion
adds or removes the rule to match. `wait.html` stops waiting after ten seconds,
adds the session rule anyway and loads the address. When the worker has
concluded that uBO is not running, the companion removes the rule, so that the
rule does not hold pages at later starts while uBO is disabled.

Measured with Chrome 157 and "Continue where you left off", the restored tab
waited in every start, and its advertising request never reached the server.
This held with one restored tab and with sixty. The guard does not cover a page
given on the command line of a Chrome that does not restore its session. Chrome
loads the rule asynchronously, and that navigation starts first. According to
Chromium's source, and not tested, a page that the site's service worker serves
from its fetch handler does not pass through a `main_frame` rule. The guard
covers only the browser's start. uBO's incognito instance starts later, when
the first incognito window opens. The rules are the same for regular and
incognito windows, and a page opened while the incognito instance starts is not
held (measured). For the pages the guard does not hold, uBO marks the tabs it
could not filter, and the companion marks the ones that loaded before uBO did
(see Toolbar state above).

`wait.html` has to be web-accessible, because Chrome follows a redirect to an
extension page only when that page is web-accessible. Any site can therefore
open `wait.html` with an address of its choice. The page accepts only http(s)
addresses and loads them, which the site could do itself. The companion's CSP
forbids framing it.

**The logger in the side panel.** The companion's side panel shows uBO's logger
in a frame, beside the page it is logging. Chrome lists the side panel in its
side panel menu under the companion's name. In an incognito window, the side
panel shows the logger of uBO's incognito instance.

**uBO's pages load once uBO accepts their connections.** The popup and the side
panel ask uBO first, and set the frame's address once uBO's background page
accepts connections from uBO's pages (`uboStarted()` in `ubo.js`, which the
companion's pages share). In a frame, uBO's client code handles a failed
connection to the background page the way a content script does. It shuts
itself down and clears `vAPI` (`disconnectListener` in `js/vapi-client.js`),
and the page stays empty. In a test with Chrome 157, a popup page that set the
frame's address straight away was open while Chrome started. The panel stayed
empty in 5 of 8 starts, and uBO's card at `chrome://extensions` showed two
errors from `popup-fenix.js`. With the wait, the panel stayed empty in 0 of 8
starts.

**Notices in the popup.** Each notice is shown only while it applies. While uBO
does not answer, the popup shows a notice in place of uBO's panel. The notice
says "Waiting for uBlock Origin to start." after 0.7 s. Once the worker has
concluded that uBO is not running, the notice says "uBlock Origin is not
running. Check on chrome://extensions that it is turned on." instead (see
Toolbar state above). The popup also tells the worker that uBO did not answer,
so that the worker asks uBO at once. uBO's panel loads when uBO answers and
accepts connections from its pages. The side panel shows these two notices in
the same way.

Above uBO's panel, the popup can show two more notices. The first is "uBlock
Origin was updated. Chrome runs the previous version until it restarts." The
popup shows it when the recipe or the uBO version that the running uBO reports
differs from the value in the popup's own file, or when the recipe that the
worker reports differs. An answer without a recipe counts as different. The
second is the notice about "Allow user scripts" from edit 8, shown while that
toggle is off.

On Linux the installer pins the button to the toolbar
(`extensions.pinned_extensions`) when it adds the companion to a profile or
repairs the companion's entry. On Windows, `load` pins it.

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

Chrome 136 and later accept a DevTools connection to the default user data
directory when "Allow remote debugging for this browser instance" is on in
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
`0x480` in Wine's dialog, and in Windows' dialog the `Edit` inside the
`ComboBoxEx32` with ID `0x47C`) and sends `IDOK`. Then it waits until
`getExtensionsInfo()` lists both as enabled: Chrome lists an extension as
disabled, with the reason `reloading`, while it loads it again. Finally, if it
switched remote debugging on, it opens `chrome://inspect/#remote-debugging`,
waits until the page shows the checkbox ticked and clicks it, as a user would.
Chrome then closes the connection, and the page closes itself.

`load --profiles "Default|Profile 1"` loads into the named profile folders, on
the same connection and so with one prompt. `Target.createTarget` opens tabs
only in the profile Chrome started with, so for each profile `load` starts
`chrome.exe` again with `--profile-directory`. The new process hands its command
line to the running Chrome, which opens a window in that profile, always at the
New Tab page: it ignores any address on a command line handed over this way.
`load` takes the page target that was not there before as that window's tab,
navigates it to `chrome://extensions`, loads the extensions there as above, and
closes it. Profiles that already have both extensions are left out.
Chrome keeps its profiles in `%LOCALAPPDATA%\Google\Chrome\User Data`
whether it is installed for this user or for all users; only the `UserDataDir`
policy moves them, and `load` reads that policy.

### The MSI's setup wizard

The MSI contains Python, `ubo-lpa.py` and the uBO release that was newest when
it was built, twice: as the files it installs, and as one zip in its `Binary`
table. Before the first page, a custom action (`windows/msi/ca`) unpacks the zip
into the temporary folder and runs `ubo-lpa.py setup-info`. `setup-info` reports
the Chrome that `load` uses, with its version and whether it is installed for
this user or for all users; its profiles, and whether each already has both
extensions; and the newest uBO release on GitHub and the bundled one. The
wizard's pages show that: the release, newest or bundled, each with a link to
its release notes; the profiles, as checkboxes; the folder, which defaults to
`%LOCALAPPDATA%\uBOLPA`; and two options, maintenance and the daily update.

With maintenance, Windows Installer installs the program to
`%LOCALAPPDATA%\Programs\ubo-lpa` and runs `install`, `timer` and `load` from it.
The chosen folder is recorded in `HKCU\Software\ubo-lpa`, and the Start menu
entry runs `msiexec /i` with the product code, which opens the maintenance page:
Update now, Change settings (the same pages, with the folder fixed) or Remove.
The daily update setting is kept in the install's state file, so a silent
upgrade keeps it.

Without maintenance, the wizard runs `install` and `load` from its own unpacked
copy, while the progress page shows, and its `InstallUISequence` skips
`ExecuteAction`. Windows Installer then does not install any files or register
the product, so the chosen folder is all that is left. Running the MSI again
with the same folder updates it in place, with the same keys and so the same
extension IDs.

DevTools has a direct `Extensions.loadUnpacked(path)`, and retail Chrome 157
accepts it on a connection allowed in the prompt, without
`--enable-unsafe-extension-debugging`. Chrome loads extensions that way for the
current session only: after a restart they are gone, as with
`--load-extension`. When `Input.dispatchDragEvent` simulates dropping the
folder onto `chrome://extensions`, the page receives it as a directory, but the
browser answers `loadUnpacked({useDraggedPath: true})` with "No dragged path":
it records the path only for a drag from the operating system.

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

**Either platform:** a button that is not pinned appears only in the
puzzle-piece menu. Click the puzzle icon, then the pin. The Linux installer and
`load` pin the button when they add the companion to a profile. An update
leaves the button as it is.

### The button shows "!" or "uBlock Origin is not running"

**"!" on a yellow badge:** the tab began loading before uBO was filtering. This
happens to the tabs Chrome restores at launch. The tab's first requests were
not filtered, so reload it.

**The grey icon and "uBlock Origin is not running":** uBO does not answer the
companion. Check at `chrome://extensions` that uBlock Origin (under "Chrome
Apps") is enabled and has not crashed.

### A tab shows "Waiting for uBlock Origin to be ready."

uBO's setting "Suspend network activity until all filter lists are loaded" is
on, and the tab was opened before uBO was filtering. The page loads as soon as
uBO filters requests, or after ten seconds at the latest. If the text appears
for several seconds at every browser start, uBO is slow to start or does not
start. In the first case the button shows uBO's loading icon. In the second it
shows "uBlock Origin is not running", from about a minute after the start.

### The popup says "uBlock Origin was updated."

An update replaced the files while Chrome was running. Chrome keeps running the
uBO background page and the companion's worker that it loaded at its start. The
new ones run once Chrome restarts.

### uBlock Origin disappeared after a visit to chrome://apps

Chrome lists a legacy packaged app on `chrome://apps` as an unsupported app,
with a "Remove unsupported app" link. The link opens a dialog whose default
button uninstalls the app. On Linux, run `install` again with Chrome closed. On
Windows, run `load` again, or load the folder by hand. The uninstall also
deleted uBO's settings, so restore them from a backup made in uBO's dashboard.

### The popup is empty or the wrong size

The popup embeds uBO's panel in an iframe. An empty popup indicates that the
iframe did not load.

- `check` reports whether `popup-fenix.html` is missing from uBO's
  `web_accessible_resources`, or whether the companion's pages reference the
  wrong uBO ID (`ubo.js`).
- The panel goes empty when uBO's background page stops while the popup or
  the side panel is open, because uBO's page in the frame then shuts itself
  down. Close the popup or the side panel and open it again.
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
