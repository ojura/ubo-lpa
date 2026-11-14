// Until uBO filters requests, the launch guard redirects each page
// navigation here, with the address after the # (see "Launch guard" in the
// worker). This page waits until uBO's request listener is installed,
// switches the guard off for the rest of the browser session, and loads the
// address it was given. askUbo() is in ubo.js.

// OPEN_RULE is the session rule that switches the guard off. The worker adds
// the same rule once uBO's request listener is installed.
const OPEN_RULE = __OPEN_RULE__;
const target = location.hash.slice(1);

// Any site can open this page with an address of its choice after the #, so
// only a web address is accepted.
const valid = /^https?:\/\//.test(target);

(async () => {
  if ( valid === false ) {
    document.title = 'uBlock Origin';
    document.getElementById('state').textContent = 'This page was opened without a web address to go to.';
    document.getElementById('state').hidden = false;
    return;
  }
  const start = performance.now();
  // uBO usually answers within a second, so the page shows its text and the
  // address only after 0.7 s. It stops waiting after 10 s, in case uBO does
  // not start at all.
  const show = setTimeout(() => {
    document.getElementById('target').textContent = target;
    document.getElementById('state').hidden = false;
    document.getElementById('target').hidden = false;
  }, 700);
  let listens = false;
  while ( listens === false && performance.now() - start < 10000 ) {
    listens = (await askUbo())?.listening === true;
    if ( listens === false ) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  clearTimeout(show);
  // The guard is switched off here, so that loading the address does not
  // depend on the worker. When uBO's request listener is installed, the
  // worker is told so as well, because uBO's own message may not have
  // reached it yet.
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [ OPEN_RULE.id ],
    addRules: [ OPEN_RULE ],
  });
  if ( listens ) {
    // The page waits at most 0.5 s for the worker's answer, and then loads
    // the address whether or not the worker answered.
    await Promise.race([
      chrome.runtime.sendMessage({ what: 'uboListens' }).catch(() => {}),
      new Promise(resolve => setTimeout(resolve, 500)),
    ]);
  }
  location.replace(target);
})();
