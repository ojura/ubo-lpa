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
  // Always written, so a failed or disabled state clears a stale count rather
  // than leaving the previous tab's number visible.
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
