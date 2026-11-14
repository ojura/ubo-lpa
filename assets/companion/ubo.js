// The popup and wait.html share this file. askUbo() asks
// uBO about its state, and uboStarted() waits until a page may show one of
// uBO's own pages in a frame.
const UBO_ID = '__UBO_ID__';
const UBO_ORIGIN = `chrome-extension://${UBO_ID}`;

// Resolves with uBO's answer, or with undefined when uBO's background page
// does not answer.
const askUbo = () => new Promise(resolve => {
  chrome.runtime.sendMessage(UBO_ID, { what: 'uboBadge' }, response => {
    resolve(chrome.runtime.lastError ? undefined : response);
  });
});

// Resolves with uBO's answer once uBO's background page accepts connections
// from uBO's pages. If one of uBO's pages is loaded in a frame before then,
// it stays empty. When its connection to the background page fails, uBO's
// code in a frame shuts itself down (disconnectListener in
// js/vapi-client.js) and does not try again. A popup opened while Chrome is
// still starting would show such an empty panel.
//
// say(text) shows a notice while uboStarted() waits, and say('') removes it.
// uBO usually answers at once, so the waiting notice appears only after
// 0.7 s. The worker decides whether uBO is not running (lookForUbo() in the
// worker). uboStarted() tells the worker that uBO did not answer, at most
// once a second, and shows the notice that uBO is not running once the
// worker has concluded that.
async function uboStarted(say) {
  const start = performance.now();
  let toldWorker = -Infinity;
  let missing = false;
  for (;;) {
    const ubo = await askUbo();
    // After an update, the running uBO can be a build whose bridge does not
    // report pagesCanConnect. Its background page is running, so its pages
    // can connect.
    if ( ubo !== undefined && ubo.pagesCanConnect !== false ) {
      say('');
      return ubo;
    }
    const waited = performance.now() - start;
    if ( ubo !== undefined ) {
      missing = false;
    } else if ( waited - toldWorker >= 1000 ) {
      toldWorker = waited;
      // The page waits at most 0.5 s for the worker's answer, because after
      // an update the worker may not be able to answer.
      missing = await Promise.race([
        chrome.runtime.sendMessage({ what: 'uboSilent' }).catch(() => false),
        new Promise(resolve => setTimeout(resolve, 500, false)),
      ]) === true;
    }
    if ( missing ) {
      say('uBlock Origin is not running. Check on chrome://extensions ' +
          'that it is turned on.');
    } else if ( waited > 700 ) {
      say('Waiting for uBlock Origin to start.');
    }
    await new Promise(resolve => setTimeout(resolve, waited > 10000 ? 1000 : 100));
  }
}
