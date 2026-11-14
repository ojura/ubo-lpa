const UBO_ID = '__UBO_ID__';

// An update that changes this file gives the worker a new file name, and
// Chrome installs the new worker at its next start. A newly installed worker
// normally waits until the pages that the previous worker controls are
// closed. Chrome can restore such a page with the session, for example a tab
// that waits in wait.html. Chrome does not start the previous worker,
// because the manifest points to the new file. Without skipWaiting(), the
// extension would not work until those tabs are closed.
self.addEventListener('install', ( ) => { self.skipWaiting(); });

// uBO uses Chrome's split incognito mode, so this extension does too. Chrome
// runs one instance of each extension for regular windows and one for
// incognito windows. Each instance of this extension has its own worker. In
// an incognito window, the popup shows the panel of uBO's incognito
// instance, the toolbar state comes from that instance, and the keyboard
// commands go to it. The two instances share session storage, so each keeps
// its state under a key that starts with "regular:" or "incognito:".
const incognito = chrome.extension.inIncognitoContext === true;
const key = name => `${incognito ? 'incognito' : 'regular'}:${name}`;

const askUbo = msg => new Promise(resolve => {
  chrome.runtime.sendMessage(UBO_ID, msg, response => {
    resolve(chrome.runtime.lastError ? undefined : response);
  });
});

// The state below is what this instance knows about uBO in the current
// browser session. Chrome stops an idle worker, so the state is kept in
// session storage, and the worker reads it once, when it starts.
//   instance     the run of uBO's background page that the other fields
//                describe
//   listens      uBO's request listener is installed, so uBO filters requests
//   missing      the worker has concluded that uBO is not running
//   silentSince  when uBO first did not answer, while listens was false
//   early        tabs that began loading while listens was false
// Chrome empties session storage when it starts, and when this extension is
// loaded again: with Reload on chrome://extensions, or when it is switched
// off and on. A worker that finds the state empty asks uBO which run it is
// and whether its request listener is installed. After a reload, uBO may be
// running already, and it sends the message that its listener is installed
// only once. At a browser start, uBO's background page does not exist yet,
// so uBO does not answer, and silentSince records the time.
let sessionState;
const loadState = ( ) => {
  sessionState ??= chrome.storage.session.get(key('state')).then(async bin => {
    const state = Object.assign({ listens: false, missing: false, early: [] }, bin[key('state')]);
    if ( bin[key('state')] === undefined ) {
      const ubo = await askUbo({ what: 'uboBadge' });
      state.instance = ubo?.instance;
      state.listens = ubo?.listening === true;
      if ( ubo === undefined ) { state.silentSince = Date.now(); }
      await saveState(state);
    }
    return state;
  });
  return sessionState;
};
const saveState = state => chrome.storage.session.set({ [key('state')]: state });

/******************************************************************************/
// Toolbar. uBO computes the icon, the badge and the title for each tab and for
// the button's default state, and sends them here.

const iconPaths = flavor => ({
  16: `img/icon_16${flavor}.png`,
  32: `img/icon_32${flavor}.png`,
  64: `img/icon_64${flavor}.png`,
});
const TITLE = 'uBlock Origin';
// A tab that began loading before uBO's request listener was installed gets
// the badge "!" in EARLY_COLOR and the title EARLY_TITLE. uBO did not filter
// that tab's first requests, and uBO itself shows this badge and this text
// for a page whose requests arrived before its listener was installed.
const EARLY_TITLE = `${TITLE} (!)\n${__EARLY_TIP__}`;
const EARLY_COLOR = '#FC0';

async function setTabState(msg) {
  const { tabId, state } = msg;
  let { parts, badge, color, title } = msg;
  if ( (await loadState()).early.includes(tabId) ) {
    parts = (parts | 0b0110) & ~0b1000;
    badge = '!';
    color = EARLY_COLOR;
    title = EARLY_TITLE;
  }
  const jobs = [];
  if ( parts === undefined || (parts & 0b0001) !== 0 ) {
    jobs.push(chrome.action.setIcon({
      tabId,
      path: iconPaths(state === 1 ? '' : '-off'),
    }));
  }
  if ( (parts & 0b0010) !== 0 ) {
    jobs.push(chrome.action.setBadgeText({
      tabId,
      text: (parts & 0b1000) === 0 ? badge : '',
    }));
  }
  if ( (parts & 0b0100) !== 0 ) {
    jobs.push(chrome.action.setBadgeBackgroundColor({ tabId, color }));
  }
  jobs.push(chrome.action.setTitle({ tabId, title }));
  // A tab can close before its update arrives.
  await Promise.allSettled(jobs);
}

async function setDefaultState(flavor, text, title = TITLE) {
  await Promise.allSettled([
    chrome.action.setIcon({ path: iconPaths(flavor) }),
    chrome.action.setBadgeText({ text }),
    chrome.action.setBadgeBackgroundColor({ color: text === '!' ? EARLY_COLOR : '#666666' }),
    chrome.action.setBadgeTextColor({ color: '#FFFFFF' }),
    chrome.action.setTitle({ title }),
  ]);
}

// uBO sends the default state without a flavor once its request listener is
// installed. From then on it filters every request.
async function uboListens() {
  const state = await loadState();
  if ( state.listens ) { return; }
  state.listens = true;
  await saveState(state);
  if ( await guardInstalled() ) { await openGuard(); }
  for ( const tabId of state.early ) {
    await Promise.allSettled([
      chrome.action.setBadgeText({ tabId, text: '!' }),
      chrome.action.setBadgeBackgroundColor({ tabId, color: EARLY_COLOR }),
      chrome.action.setTitle({ tabId, title: EARLY_TITLE }),
    ]);
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if ( change.status !== 'loading' ) { return; }
  lookForUbo();
  const state = await loadState();
  const listed = state.early.includes(tabId);
  if ( state.listens ) {
    // The tab loads again, this time with uBO filtering.
    if ( listed === false ) { return; }
    state.early = state.early.filter(a => a !== tabId);
  } else {
    if ( listed || /^https?:/.test(tab.url ?? '') === false ) { return; }
    state.early.push(tabId);
  }
  await saveState(state);
});

chrome.tabs.onRemoved.addListener(async tabId => {
  const state = await loadState();
  if ( state.early.includes(tabId) === false ) { return; }
  state.early = state.early.filter(a => a !== tabId);
  await saveState(state);
});

/******************************************************************************/
// Detecting that uBO is not running. uBO does not report that it stops, so
// the worker asks uBO at these times:
// - when the worker first runs in a browser session (loadState());
// - when a tab is activated or starts to load, unless this run of the worker
//   heard from uBO in the last minute;
// - when one of this extension's pages reports that uBO did not answer it.
// Once the worker has concluded that uBO is not running, the button says so,
// and the worker unregisters uBO's scriptlets so that they stop running in
// pages.
//
// When uBO's request listener was installed and uBO does not answer, the
// worker asks again five seconds later, in case uBO is restarting. When the
// listener was not installed yet, the worker concludes that uBO is not
// running only after a minute without an answer, to allow for a browser
// start with many tabs to restore. The session state keeps the time of the
// first unanswered question, and each later look compares it with the
// clock. A timer in the worker cannot count that minute, because Chrome
// stops a worker 30 seconds after its last event, even while the worker
// keeps asking uBO on a timer (measured).

const HEARD_RECENTLY = 60000;
const START_GRACE = 60000;
let lastAnswer = 0;
let lastLook = 0;
let looking;

// Values set for a tab take precedence over the button's defaults, so a change
// of the defaults is applied to every open tab as well.
async function setEveryTab(flavor, title) {
  for ( const tab of await chrome.tabs.query({}) ) {
    await Promise.allSettled([
      chrome.action.setIcon({ tabId: tab.id, path: iconPaths(flavor) }),
      chrome.action.setBadgeText({ tabId: tab.id, text: '' }),
      chrome.action.setTitle({ tabId: tab.id, title }),
    ]);
  }
}

// uBO's messages include an identifier of the run of its background page. A
// new identifier means that uBO has restarted and has not yet reported that
// its request listener is installed, so listens is set to false.
async function uboInstance(instance) {
  const state = await loadState();
  if ( instance === undefined || instance === state.instance ) { return; }
  state.instance = instance;
  state.listens = false;
  await saveState(state);
}

async function uboAnswered() {
  lastAnswer = Date.now();
  const state = await loadState();
  if ( state.missing === false && state.silentSince === undefined ) { return; }
  const wasMissing = state.missing;
  state.missing = false;
  delete state.silentSince;
  await saveState(state);
  if ( wasMissing === false ) { return; }
  await setDefaultState('', '');
  // uBO sends each tab's state again the next time it updates that tab's
  // toolbar icon (updateToolbarIcon() in js/tab.js).
  await setEveryTab('', TITLE);
  // The same run of uBO may have been too busy to answer, and does not send
  // the message that its request listener is installed a second time.
  if ( (await askUbo({ what: 'uboBadge' }))?.listening ) { await uboListens(); }
}

async function uboMissing() {
  const state = await loadState();
  state.missing = true;
  state.listens = false;
  delete state.silentSince;
  await saveState(state);
  const title = `${TITLE} is not running`;
  await setDefaultState('-off', '', title);
  await setEveryTab('-off', title);
  // Without uBO the guard would hold every page until wait.html gives up.
  await setGuard(false);
  guardWanted = undefined;
  if ( incognito === false ) {
    await queueUserScripts(( ) => dropScriptlets()).catch(( ) => { });
  }
}

async function checkUbo() {
  if ( await askUbo({ what: 'uboBadge' }) === undefined ) { return false; }
  await uboAnswered();
  return true;
}

// Starts look() unless a look is already running. Without reported, it starts
// one only when this run of the worker has not heard from uBO in the last
// minute and the previous look started at least a second ago. With reported,
// one of this extension's pages asked uBO and did not get an answer, and the
// look starts at once.
function lookForUbo(reported = false) {
  const now = Date.now();
  if ( looking !== undefined ) { return; }
  if ( reported === false ) {
    if ( now - lastAnswer < HEARD_RECENTLY || now - lastLook < 1000 ) { return; }
  }
  lastLook = now;
  looking = look(now).catch(( ) => { }).then(( ) => { looking = undefined; });
}

async function look(now) {
  const state = await loadState();
  // uBO is asked even when the worker has concluded that it is not running,
  // so that an answer can end that state (uboAnswered()).
  if ( await checkUbo() || state.missing ) { return; }
  if ( state.listens ) {
    await new Promise(resolve => setTimeout(resolve, 5000));
    if ( await checkUbo() === false ) { await uboMissing(); }
    return;
  }
  if ( state.silentSince === undefined ) {
    state.silentSince = now;
    await saveState(state);
  } else if ( now - state.silentSince >= START_GRACE ) {
    await uboMissing();
  }
}

// Starts the worker at browser start, so that START_GRACE, the minute that
// uBO gets to answer, begins then. In the first start after an update,
// Chrome drops the events that waited for the worker's registration,
// onStartup among them (measured). The minute then begins with the first
// tab event.
chrome.runtime.onStartup.addListener(( ) => { loadState(); });

chrome.tabs.onActivated.addListener(( ) => { lookForUbo(); });

/******************************************************************************/
// Launch guard. At a browser start, Chrome loads the tabs it restores before
// uBO's background page exists, so uBO does not filter their requests. uBO's
// setting "Suspend network activity until all filter lists are loaded" is
// meant to prevent that, but on Chromium uBO can act on it only once its own
// page runs. While the setting is on, this extension keeps a dynamic
// declarativeNetRequest rule, which Chrome applies by itself from its start.
// The rule redirects every http(s) page navigation to wait.html, which loads
// the address once uBO's request listener is installed. A session rule then
// switches the redirect off. Chrome drops session rules when it exits, so
// the redirect applies again at its next start.

const GUARD_RULE = {
  id: 1,
  priority: 1,
  action: {
    type: 'redirect',
    redirect: { regexSubstitution: `${chrome.runtime.getURL('wait.html')}#\\0` },
  },
  condition: { regexFilter: '^https?://.*', resourceTypes: [ 'main_frame' ] },
};
const OPEN_RULE = __OPEN_RULE__;

// Only the instance for regular windows adds and removes the rules.
async function guardInstalled() {
  if ( incognito ) { return false; }
  const rules = await chrome.declarativeNetRequest.getDynamicRules();
  return rules.some(rule => rule.id === GUARD_RULE.id);
}

function openGuard() {
  return chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [ OPEN_RULE.id ],
    addRules: [ OPEN_RULE ],
  });
}

// uBO includes the setting's value in its toolbar messages.
let guardWanted;
async function setGuard(wanted) {
  if ( incognito || wanted === guardWanted ) { return; }
  guardWanted = wanted;
  const dnr = chrome.declarativeNetRequest;
  if ( wanted === await guardInstalled() ) { return; }
  if ( wanted ) {
    // The setting was switched on while the browser runs. When uBO's request
    // listener is installed, the session rule is added first, so that the
    // redirect takes effect only at the next start.
    if ( (await loadState()).listens ) { await openGuard(); }
    await dnr.updateDynamicRules({ addRules: [ GUARD_RULE ] });
  } else {
    await dnr.updateDynamicRules({ removeRuleIds: [ GUARD_RULE.id ] });
  }
}

/******************************************************************************/
// Scriptlets. For each hostname that has scriptlet filters of its own, uBO
// sends that hostname's scriptlets here, and chrome.userScripts runs them at
// document_start, before the page's own scripts. The main-world scriptlets
// run in the page's world, and the isolated-world ones in the user script
// world. uBO injects the scriptlets itself wherever a registered script did
// not run, so when registering fails here, the scriptlets still run, only
// later. Chrome writes all registered scripts to the profile and sends them
// to every tab's process again with every change. uBO therefore decides
// which hostnames are registered, and it sends several hostnames in one
// message where it can. Operations run one at a time and in arrival order: a
// reset that arrives after a registration has to remove it.

// uBO's own isolated world does not have a CSP, and its scriptlets rely on
// that, for example to insert an element with an inline event handler.
const WORLD_CSP = "script-src * 'unsafe-inline' 'unsafe-eval' data: blob:; object-src *";

let userScriptQueue = Promise.resolve();
const queueUserScripts = fn => {
  const run = userScriptQueue.then(fn);
  userScriptQueue = run.catch(( ) => { });
  return run;
};

const scriptIds = hostname => [ `main:${hostname}`, `isolated:${hostname}` ];
const WORLDS = [ 'MAIN', 'USER_SCRIPT' ];

// Passes all scripts to Chrome in one call. When Chrome rejects that call,
// for example because one hostname does not make a valid match pattern,
// inOneCall() passes each script in a call of its own, so that the other
// scripts are still registered.
async function inOneCall(method, scripts) {
  if ( scripts.length === 0 ) { return; }
  try {
    await chrome.userScripts[method](scripts);
  } catch {
    for ( const script of scripts ) {
      await chrome.userScripts[method]([ script ]).catch(( ) => { });
    }
  }
}

async function setScriptlets(hosts, drop) {
  const ids = [ ...hosts.map(a => a.hostname), ...drop ].flatMap(scriptIds);
  const existing = await chrome.userScripts.getScripts({ ids });
  const have = new Set(existing.map(a => a.id));
  const remove = drop.flatMap(scriptIds).filter(id => have.has(id));
  const update = [];
  const add = [];
  for ( const { hostname, main, isolated } of hosts ) {
    const codes = [ main, isolated ];
    scriptIds(hostname).forEach((id, i) => {
      if ( codes[i] === undefined ) {
        if ( have.has(id) ) { remove.push(id); }
        return;
      }
      (have.has(id) ? update : add).push({
        id,
        matches: [ `*://${hostname}/*` ],
        js: [ { code: codes[i] } ],
        runAt: 'document_start',
        world: WORLDS[i],
        allFrames: true,
      });
    });
  }
  if ( remove.length !== 0 ) {
    await chrome.userScripts.unregister({ ids: remove });
  }
  await inOneCall('update', update);
  await inOneCall('register', add);
}

async function dropScriptlets() {
  if ( chrome.userScripts !== undefined ) {
    await chrome.userScripts.unregister();
  }
}

// uBO sends hello when it starts. The message includes uBO's epoch, which is
// the installer's token and the recipe of the build. Scripts registered
// under another epoch are unregistered, because they may listen for the
// presence event under another name. They would then not cancel it, and uBO
// would run the scriptlets a second time. The answer lists the hostnames
// that have scripts, and uBO then replaces those scripts with current ones.
async function hello(epoch) {
  const bin = await chrome.storage.local.get('epoch');
  if ( bin.epoch !== epoch ) {
    await dropScriptlets();
    await chrome.storage.local.clear();
    await chrome.storage.local.set({ epoch });
  }
  await chrome.userScripts.configureWorld({ csp: WORLD_CSP });
  const hosts = new Set();
  for ( const script of await chrome.userScripts.getScripts() ) {
    hosts.add(script.id.slice(script.id.indexOf(':') + 1));
  }
  return Array.from(hosts);
}

/******************************************************************************/

chrome.runtime.onMessageExternal.addListener((msg, sender, respond) => {
  if ( sender.id !== UBO_ID || msg instanceof Object === false ) { return false; }
  uboAnswered();
  if ( typeof msg.launchGuard === 'boolean' ) { setGuard(msg.launchGuard); }
  if ( msg.what === 'toolbar' ) {
    uboInstance(msg.instance).then(( ) => setTabState(msg));
    return false;
  }
  if ( msg.what === 'toolbarDefault' ) {
    uboInstance(msg.instance).then(( ) => {
      setDefaultState(msg.flavor, msg.text);
      if ( msg.flavor === '' ) { uboListens(); }
    });
    return false;
  }
  let job;
  if ( incognito ) {
    return false;
  } else if ( chrome.userScripts === undefined ) {
    // The permission is missing or "Allow user scripts" is off for this
    // extension.
    respond({ ok: false, error: 'chrome.userScripts is not available' });
    return false;
  } else if ( msg.what === 'hello' ) {
    job = ( ) => hello(msg.epoch);
  } else if ( msg.what === 'setScriptlets' ) {
    job = ( ) => setScriptlets(msg.hosts ?? [], msg.drop ?? []);
  } else if ( msg.what === 'resetScriptlets' ) {
    job = ( ) => dropScriptlets();
  } else {
    return false;
  }
  queueUserScripts(job).then(
    hosts => respond({ ok: true, hosts }),
    error => respond({ ok: false, error: String(error) }),
  );
  return true;
});

/******************************************************************************/
// Keyboard commands. A legacy packaged app cannot declare them, so this
// extension declares uBO's commands and passes each one on to uBO. uBO then
// runs the listener that its js/commands.js registered.

chrome.commands.onCommand.addListener(command => {
  askUbo({ what: 'command', command });
});

// Answers the messages of this extension's own pages.
chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if ( sender.id !== chrome.runtime.id ) { return false; }
  // The popup compares this recipe with the one in its own file; see
  // popup.js.
  if ( msg?.what === 'build' ) {
    respond('__RECIPE__');
    return false;
  }
  // wait.html found that uBO's request listener is installed and is about to
  // load the address it was given; see wait.js.
  if ( msg?.what === 'uboListens' ) {
    uboListens().then(( ) => respond(true), ( ) => respond(false));
    return true;
  }
  // A page reports that uBO did not answer its question, and asks whether
  // the worker has concluded that uBO is not running; see uboStarted() in
  // ubo.js. The report starts look(), and the answer is the current state,
  // so the page learns the result of that look when it asks again.
  if ( msg?.what === 'uboSilent' ) {
    lookForUbo(true);
    loadState().then(state => respond(state.missing), ( ) => respond(false));
    return true;
  }
  return false;
});
