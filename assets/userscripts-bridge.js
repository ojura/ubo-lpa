
// ubo-lpa user scripts. uBO injects each site's scriptlets with
// tabs.executeScript when a navigation commits, and that arrives after the
// page's first inline scripts have run; an anti-adblock script in <head> runs
// before the scriptlets meant to stop it. The companion extension has
// chrome.userScripts, which injects registered code into the main world at
// document_start, before any page script. This hands each site's main-world
// scriptlets to the companion. Once it confirms a site, navigations that start
// later get only the isolated-world part from here, so the scriptlets run once.
// Without the companion, or while its "Allow user scripts" toggle is off, uBO
// injects every part with executeScript, as an unpatched uBO does.
const ubolpaUserScripts = (( ) => {
    const companionId = '__COMP_ID__';
    const navStarts = new Map();
    const send = msg => new Promise(resolve => {
        try {
            chrome.runtime.sendMessage(companionId, msg, response => {
                resolve(chrome.runtime.lastError ? undefined : response);
            });
        } catch {
            resolve();
        }
    });
    return {
        register(hostname, entry, details) {
            if ( Boolean(details?.mainCode) === false ) { return; }
            entry.isolatedOnlyCode =
                uBO_assembleIsolatedCode({ ...details, mainCode: '' }) ?? null;
            const code = [
                '(function uBO_userScript() {',
                details.earlyBailoutCode ?? '',
                details.mainCode,
                '})();',
            ].join('\n');
            send({ what: 'registerScriptlets', hostname, code }).then(response => {
                if ( response?.ok === true ) { entry.coveredAt = Date.now(); }
            });
        },
        forget(hostname) {
            send({ what: 'unregisterScriptlets', hostname });
        },
        reset() {
            navStarts.clear();
            send({ what: 'resetScriptlets' });
        },
        navStarted(details) {
            if ( navStarts.size > 1024 ) { navStarts.clear(); }
            navStarts.set(`${details.tabId}:${details.frameId}`, details.timeStamp);
        },
        // The code to inject when a navigation commits. A frame whose own URL
        // is not http(s), such as about:blank, does not match the registered
        // user script and still gets the main-world part from here.
        codeFor(entry, details) {
            if ( entry === undefined ) { return; }
            entry.t = Date.now();
            const started = navStarts.get(`${details.tabId}:${details.frameId}`);
            const covered = entry.coveredAt !== undefined &&
                started !== undefined && started > entry.coveredAt &&
                /^https?:\/\//.test(details.url);
            return covered ? entry.isolatedOnlyCode : entry.code;
        },
    };
})();
