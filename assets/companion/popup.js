const f = document.getElementById('ubo');

// Apply the last known size synchronously, before the iframe gets a src, so the
// popup opens at the right size instead of visibly resizing once uBO reports.
// localStorage because it is synchronous; chrome.storage resolves after paint.
try {
  const c = JSON.parse(localStorage.getItem('uboPopupSize') || 'null');
  if ( c && c.w > 0 && c.h > 0 ) {
    f.style.width = c.w + 'px';
    f.style.height = c.h + 'px';
  }
} catch ( ex ) {
}

// Register before any await so the listener receives an early first report.
const UBO_ORIGIN = 'chrome-extension://__UBO_ID__';

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

(async () => {
  let tabId = -1;
  try {
    const tabs = await chrome.tabs.query({active: true, currentWindow: true});
    if ( tabs.length ) { tabId = tabs[0].id; }
  } catch ( ex ) {
  }
  f.src = 'chrome-extension://__UBO_ID__/popup-fenix.html?tabId=' + tabId;
})();
