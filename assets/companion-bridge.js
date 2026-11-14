
/******************************************************************************/
// ubo-lpa companion bridge, appended to js/vapi-background.js. A legacy
// packaged app cannot have a toolbar button or keyboard commands, so the
// companion extension provides them. This code is uBO's side of the
// exchange: it answers the companion's questions, runs the commands that the
// companion passes on, and sends the companion the toolbar state that uBO
// computes.
{
    const companionId = '__COMP_ID__';

    // Identifies this run of the background page, so that the companion
    // notices when uBO restarts.
    const instance = self.crypto.randomUUID();

    browser.runtime.onMessageExternal.addListener((request, sender, callback) => {
        if ( sender.id !== companionId ) { return false; }
        if ( request instanceof Object === false ) { return false; }
        if ( request.what === 'uboBadge' ) {
            // Included in every answer.
            const about = {
                // Which run of this background page is answering.
                instance,
                // The recipe that identifies this build (recipe() in
                // ubo-lpa.py), and uBO's version. The popup shows its update
                // notice when either differs from the value in the popup's
                // own file.
                build: '__RECIPE__',
                version: browser.runtime.getManifest().version,
                // True once js/messaging.js has set up vAPI.messaging. Before
                // that, uBO's panel would fail to connect and stay empty, so
                // the companion waits before it loads the panel.
                pagesCanConnect: vAPI.messaging.defaultHandler !== null,
            };
            try {
                const ub = self.µBlock;
                const tabId = request.tabId;
                const ps = tabId !== undefined ? ub.pageStoreFromTabId(tabId) : null;
                callback({
                    ok: true,
                    ...about,
                    ready: ub.readyToFilter === true,
                    // True once uBO's request listener is installed. uBO
                    // filters every request from then on.
                    listening: vAPI.net instanceof Object &&
                        vAPI.net.suspendableListener !== undefined,
                    globalBlocked: ub.requestStats.blockedCount,
                    pageBlocked: ps ? ps.counts.blocked.any : 0,
                    // isTrusted() is true when filtering is off for the page.
                    netFiltering: ps ? ps.isTrusted() === false : true,
                    showBadge: ub.userSettings
                        ? ub.userSettings.showIconBadge !== false
                        : true,
                });
            } catch ( ex ) {
                callback({ ok: false, ...about, error: String(ex).substring(0, 120) });
            }
            return false;
        }
        if ( request.what === 'command' ) {
            // The shim for chrome.commands in webext.js calls the listener
            // that js/commands.js registered.
            browser.commands.onCommand.ubolpaDispatch(String(request.command));
            callback({ ok: true });
            return false;
        }
        callback({ ok: false, error: 'unknown' });
        return false;
    });

    const tell = msg => {
        try {
            browser.runtime.sendMessage(companionId, { ...msg, instance }, ( ) => {
                void browser.runtime.lastError;
            });
        } catch {
        }
    };

    // Returns true when uBO's setting "Suspend network activity until all
    // filter lists are loaded" is on. On Chromium, uBO can act on that setting
    // only once this page runs. While the setting is on, the launch guard in
    // the companion's worker holds page loads from Chrome's start until uBO's
    // request listener is installed. The toolbar messages include the value,
    // and uBO sends them only after it has loaded its settings.
    const launchGuard = ( ) =>
        self.µBlock?.userSettings?.suspendUntilListsAreLoaded === true;

    // vAPI.setIcon() and vAPI.setDefaultIcon() end in chrome.browserAction
    // calls. A legacy packaged app does not have that API, so the calls reach
    // the shim in webext.js, which does nothing with them. The wrappers below
    // send the same values to the companion, which applies them to its
    // button. The setIcon() wrapper works out the badge, the colour and the
    // title with the same code as vAPI.setIcon().
    const titleTemplate =
        `${browser.runtime.getManifest().browser_action.default_title} ({badge})`;
    const setIcon = vAPI.setIcon;
    vAPI.setIcon = async function(tabId, details) {
        setIcon(tabId, details);
        tabId = toTabId(tabId);
        if ( tabId === 0 ) { return; }
        const tab = await vAPI.tabs.get(tabId);
        if ( tab === null ) { return; }
        const hasUnprocessedRequest = vAPI.net && vAPI.net.hasUnprocessedRequest(tabId);
        const { parts, state } = details;
        const { badge, color } = hasUnprocessedRequest
            ? { badge: '!', color: '#FC0' }
            : details;
        const title = titleTemplate.replace('{badge}',
            state === 1 ? (badge !== '' ? badge : '0') : 'off'
        );
        tell({
            what: 'toolbar', tabId: tab.id, parts, state, badge, color, title,
            launchGuard: launchGuard(),
        });
    };
    const setDefaultIcon = vAPI.setDefaultIcon;
    vAPI.setDefaultIcon = function(flavor, text) {
        setDefaultIcon(flavor, text);
        // uBO calls this without a flavor once it has loaded its settings and
        // its filters. Of the default-state messages, only that one includes
        // launchGuard.
        tell(flavor === ''
            ? { what: 'toolbarDefault', flavor, text, launchGuard: launchGuard() }
            : { what: 'toolbarDefault', flavor, text }
        );
    };
}
