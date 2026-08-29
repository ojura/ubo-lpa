// ubo-lpa: report uBO's panel size to the companion, which owns the iframe.
// Must be an external file: uBO ships script-src 'self'.
(function(){
  if ( window === window.top ) { return; }
  let lastW = 0, lastH = 0;
  // Width is derived from uBO's own CSS variable rather than measured. In a
  // narrow viewport uBO adds .portrait and the panel becomes fluid, so
  // measuring it reproduces the iframe width and creates a feedback loop.
  // The probe lives for the life of the page. Creating and removing one per
  // measurement mutates the observed subtree, which schedules another
  // measurement and leaves a timer pending indefinitely.
  let probe;
  function naturalWidth() {
    if ( probe === undefined ) {
      probe = document.createElement('div');
      probe.style.cssText =
        'position:absolute;visibility:hidden;width:var(--popup-main-min-width)';
      document.body.appendChild(probe);
    }
    return Math.ceil(probe.getBoundingClientRect().width);
  }
  function post() {
    const panes = document.getElementById('panes');
    if ( document.body === null || panes === null ) { return; }
    const w = naturalWidth();
    // #panes is content-sized; scrollHeight would be max(content, viewport)
    // and therefore viewport-coupled too.
    const h = Math.ceil(panes.getBoundingClientRect().height);
    if ( w === 0 || h === 0 ) { return; }
    if ( w === lastW && h === lastH ) { return; }
    lastW = w; lastH = h;
    window.parent.postMessage(
      { type: '__RESIZE_MSG__', width: w, height: h },
      'chrome-extension://__COMP_ID__'
    );
  }
  // Coalesce uBO's mutation burst: post() forces layout twice per call.
  let timer;
  function schedule() {
    if ( timer ) { clearTimeout(timer); }
    timer = setTimeout(() => {
      timer = undefined;
      if ( document.body && document.body.classList.contains('loading') ) {
        schedule();
        return;
      }
      observer.disconnect();
      try {
        post();
      } finally {
        observer.observe(document.documentElement, {
          childList: true, subtree: true, attributes: true
        });
      }
    }, 10);
  }
  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true
  });
  window.addEventListener('load', schedule);
  schedule();
})();
