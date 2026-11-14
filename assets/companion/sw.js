const UBO_ID = '__UBO_ID__';

// Badge text is per-tab. Without an explicit tabId Chrome sets it globally, so a
// background tab finishing a load would overwrite the active tab's count.
async function refresh(tabId) {
  if ( typeof tabId !== 'number' || tabId < 0 ) { return; }
  let text = '';
  try {
    const r = await new Promise((resolve) => {
      chrome.runtime.sendMessage(UBO_ID, { what: 'uboBadge', tabId }, resp => {
        resolve(chrome.runtime.lastError ? null : resp);
      });
    });
    if ( r && r.ok && r.ready && r.netFiltering && r.showBadge !== false ) {
      const n = r.pageBlocked || 0;
      text = n > 9999 ? '9k+' : (n ? String(n) : '');
    }
  } catch ( e ) {
  }
  // The text is written even when empty, so a stale count is cleared when
  // the request fails, uBO is not ready yet, filtering is off for the page, or
  // the badge is turned off.
  try {
    await chrome.action.setBadgeText({ tabId, text });
    if ( text ) {
      await chrome.action.setBadgeBackgroundColor({ tabId, color: '#666666' });
    }
  } catch ( e ) {
  }
}

chrome.tabs.onActivated.addListener(i => refresh(i.tabId));
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if ( info.status === 'complete' ) { refresh(tabId); }
});

// uBO sends each site's main-world scriptlets here, and chrome.userScripts
// injects them at document_start, before the page's own scripts. uBO keeps
// injecting them itself until a site is confirmed here, so when registering
// fails, the scriptlets still run, only later. Operations run one at a time
// and in arrival order: a reset that arrives after a registration has to
// remove it.
let userScriptQueue = Promise.resolve();
const queueUserScripts = fn => {
  const run = userScriptQueue.then(fn);
  userScriptQueue = run.catch(() => {});
  return run;
};
const userScriptId = hostname => `site:${hostname}`;

async function registerScriptlets(hostname, code) {
  const matches = [ `*://${hostname}/*` ];
  // Registering succeeds without host access, but Chrome then does not inject
  // the script. uBO would still count the site as confirmed and stop injecting
  // the main-world part, so the page would run without it.
  if ( await chrome.permissions.contains({ origins: matches }) !== true ) {
    throw new Error(`no host access to ${hostname}`);
  }
  const script = {
    id: userScriptId(hostname),
    matches,
    js: [ { code } ],
    runAt: 'document_start',
    world: 'MAIN',
    allFrames: true,
  };
  const existing = await chrome.userScripts.getScripts({ ids: [ script.id ] });
  if ( existing.length !== 0 ) {
    await chrome.userScripts.update([ script ]);
  } else {
    await chrome.userScripts.register([ script ]);
  }
}

chrome.runtime.onMessageExternal.addListener((msg, sender, respond) => {
  if ( sender.id !== UBO_ID || msg instanceof Object === false ) { return false; }
  let job;
  if ( chrome.userScripts === undefined ) {
    // The permission is missing or "Allow user scripts" is off for this
    // extension.
    respond({ ok: false, error: 'chrome.userScripts is not available' });
    return false;
  } else if ( msg.what === 'registerScriptlets' ) {
    job = ( ) => registerScriptlets(msg.hostname, msg.code);
  } else if ( msg.what === 'unregisterScriptlets' ) {
    job = ( ) => chrome.userScripts.unregister({ ids: [ userScriptId(msg.hostname) ] });
  } else if ( msg.what === 'resetScriptlets' ) {
    job = ( ) => chrome.userScripts.unregister();
  } else {
    return false;
  }
  queueUserScripts(job).then(
    ( ) => respond({ ok: true }),
    error => respond({ ok: false, error: String(error) }),
  );
  return true;
});
