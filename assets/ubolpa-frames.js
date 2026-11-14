// ubo-lpa frames. The scripts that the companion registers do not match a
// frame without an http(s) URL of its own: an about:blank, about:srcdoc or
// blob: frame. uBO's tabs.executeScript reaches an about: frame only after
// the script that created the frame has continued, and it does not reach a
// blob: frame. A page script can therefore create an empty iframe and take
// the browser's unmodified functions from it.
//
// Chrome runs a declared content script in such a frame at document_start.
// For a new empty iframe, that is while appendChild() is still running. This
// content script asks the nearest http(s) ancestor for the main-world
// scriptlets registered there. The registered script in that ancestor
// answers with an event whose detail is its own source (earlyMain in
// js/vapi-scripting.js), and this script runs the source in the frame as an
// inline script. The page's CSP does not block that inline script, because
// an MV2 content script inserts it. The event names contain the installer's
// token, which pages cannot observe.
(( ) => {
    if ( /^https?:$/.test(location.protocol) ) { return; }
    let code;
    try {
        let w = self;
        while ( w !== w.parent ) {
            w = w.parent;
            // Reading a cross-origin ancestor's location throws, and such an
            // ancestor cannot be asked.
            if ( /^https?:$/.test(w.location.protocol) ) { break; }
        }
        if ( w === self || /^https?:$/.test(w.location.protocol) === false ) { return; }
        const onPush = ev => {
            code = ev.detail;
            ev.stopImmediatePropagation();
        };
        w.addEventListener('__TOKEN__:push', onPush, true);
        w.dispatchEvent(new CustomEvent('__TOKEN__:pull'));
        w.removeEventListener('__TOKEN__:push', onPush, true);
    } catch {
        return;
    }
    if ( typeof code !== 'string' ) { return; }
    const script = document.createElement('script');
    script.textContent = code;
    (document.head || document.documentElement).appendChild(script);
    script.remove();
    script.textContent = '';
    // uBO injects its code into this frame later, in this same isolated
    // world, and ubolpaFrameDone tells that code that the main-world
    // scriptlets have run here. The inserted script also cancels the presence
    // event that uBO's code dispatches, but a page that calls document.open()
    // on the frame removes the script's listener. The flag is for that case.
    self.ubolpaFrameDone = true;
})();
