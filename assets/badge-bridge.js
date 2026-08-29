
/******************************************************************************/
// ubo-lpa badge bridge: lets the companion extension read uBO's counters.
// Appended to js/vapi-background.js.
if ( browser.runtime.onMessageExternal ) {
    browser.runtime.onMessageExternal.addListener((request, sender, callback) => {
        if ( request instanceof Object === false ) { return false; }
        if ( request.what === 'uboBadge' ) {
            try {
                const ub = self.µBlock;
                const tabId = request.tabId;
                const ps = tabId !== undefined ? ub.pageStoreFromTabId(tabId) : null;
                callback({
                    ok: true,
                    ready: ub.readyToFilter === true,
                    globalBlocked: ub.requestStats.blockedCount,
                    pageBlocked: ps ? ps.counts.blocked.any : 0,
                    netFiltering: ps ? ps.getNetFilteringSwitch() : true,
                    showBadge: ub.userSettings
                        ? ub.userSettings.showIconBadge !== false
                        : true,
                });
            } catch ( ex ) {
                callback({ ok: false, error: String(ex).substring(0, 120) });
            }
            return false;
        }
        callback({ ok: false, error: 'unknown' });
        return false;
    });
}
