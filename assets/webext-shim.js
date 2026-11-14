// ubo-lpa-shim: chrome.browserAction and chrome.commands do not exist for a
// legacy packaged app.
//
// _manifest_features.json does not allow the manifest keys browser_action and
// commands for a legacy packaged app, and _api_features.json makes each API
// available only when its key is present, so both APIs are undefined. Without
// this shim, webext.js throws while it builds its object literal, and
// js/commands.js does not register its listener.
//
// The installer inserts this into webext.js, above promisifyNoFail, because
// that file is imported by the dashboard, logger, settings and other uBO pages
// as well as the background page.
(function(){
if ( chrome.browserAction === undefined ) {
    // The toolbar button belongs to the companion extension. uBO's calls end
    // here. The companion bridge in vapi-background.js sends the companion the
    // values that uBO computes.
    const noop = function(){};
    const cb = function(){
        const last = arguments[arguments.length-1];
        if ( typeof last === 'function' ) { last(); }
    };
    chrome.browserAction = {
        setBadgeBackgroundColor: cb, setBadgeText: cb, setIcon: cb,
        setTitle: cb, setPopup: cb, getBadgeText: cb,
        onClicked: { addListener: noop, removeListener: noop,
                     hasListener: function(){ return false; } },
    };
}
if ( chrome.commands === undefined ) {
    // The companion declares uBO's keyboard commands and passes each command
    // on to uBO. The companion bridge then calls ubolpaDispatch() with it.
    const listeners = new Set();
    chrome.commands = {
        onCommand: {
            addListener: function(fn){ listeners.add(fn); },
            removeListener: function(fn){ listeners.delete(fn); },
            hasListener: function(fn){ return listeners.has(fn); },
            ubolpaDispatch: function(command){
                for ( const fn of listeners ) { fn(command); }
            },
        },
    };
}
})();
