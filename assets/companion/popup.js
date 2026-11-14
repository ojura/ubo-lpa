const f = document.getElementById('ubo');

// Apply the last known size synchronously, before the iframe gets a src, so the
// popup opens at the right size instead of visibly resizing once uBO reports.
// localStorage is used because it is synchronous. chrome.storage resolves after
// the first paint.
try {
  const c = JSON.parse(localStorage.getItem('uboPopupSize') || 'null');
  if ( c && c.w > 0 && c.h > 0 ) {
    f.style.width = c.w + 'px';
    f.style.height = c.h + 'px';
  }
} catch ( ex ) {
}

// Register before any await so the listener receives an early first report.
// UBO_ORIGIN and uboStarted() are in ubo.js.
window.addEventListener('message', e => {
  if ( e.origin !== UBO_ORIGIN ) { return; }
  if ( e.source !== f.contentWindow ) { return; }
  if ( !e.data || e.data.type !== '__RESIZE_MSG__' ) { return; }
  const w = e.data.width, h = e.data.height;
  if ( !Number.isFinite(w) || !Number.isFinite(h) ) { return; }
  if ( w < 100 || w > 800 || h < 50 || h > 600 ) { return; }
  f.style.width = w + 'px';
  f.style.height = h + 'px';
  try {
    localStorage.setItem('uboPopupSize', JSON.stringify({ w, h }));
  } catch ( ex ) {
  }
});

// Adds a notice above uBO's panel, with a button when an action is given.
// Each notice is shown only while it applies.
const notices = document.getElementById('notices');
const notice = (text, action) => {
  const line = document.createElement('p');
  line.textContent = text;
  if ( action ) {
    const button = document.createElement('button');
    button.textContent = action.label;
    button.addEventListener('click', action.run);
    line.append(button);
  }
  notices.append(line);
  return line;
};

// Shows the notice that says why uBO's panel is not there, in place of the
// panel. An empty text removes the notice.
let waitLine;
const sayWaiting = text => {
  document.body.classList.toggle('waiting', text !== '');
  if ( text === '' ) {
    waitLine?.remove();
    waitLine = undefined;
    return;
  }
  waitLine ??= notice('');
  waitLine.textContent = text;
};

(async () => {
  const [ tabs, ubo ] = await Promise.all([
    chrome.tabs.query({active: true, currentWindow: true}).catch(() => []),
    uboStarted(sayWaiting),
  ]);
  const tabId = tabs.length ? tabs[0].id : -1;
  f.src = `${UBO_ORIGIN}/popup-fenix.html?tabId=${tabId}`;

  // An update replaces the files while Chrome runs. Chrome reads this file
  // from disk when the popup opens, but it keeps running the uBO background
  // page and the worker that it loaded at its start. Both report the recipe
  // of their build. The popup shows the update notice when either recipe, or
  // uBO's version, differs from the value in this file. An answer without a
  // recipe comes from an older build and counts as a difference. A worker
  // that does not answer within 3 s is left out of the comparison.
  const worker = await Promise.race([
    chrome.runtime.sendMessage({ what: 'build' }).then(build => ({ build }), () => ({})),
    new Promise(resolve => setTimeout(resolve, 3000)),
  ]);
  if (
    ubo.build !== '__RECIPE__' || ubo.version !== '__UBO_VERSION__' ||
    (worker !== undefined && worker.build !== '__RECIPE__')
  ) {
    notice('uBlock Origin was updated. Chrome runs the previous version until it restarts.');
  }
  // Without chrome.userScripts, uBO injects its scriptlets itself, and they
  // start only after a page's first scripts have run.
  if ( chrome.userScripts === undefined ) {
    notice('\u201cAllow user scripts\u201d is off for this button, so scriptlets ' +
           'start after a page\u2019s first scripts.', {
      label: 'Open this button\u2019s settings',
      run: () => chrome.tabs.create({ url: `chrome://extensions/?id=${chrome.runtime.id}` }),
    });
  }
})();
