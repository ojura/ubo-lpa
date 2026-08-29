// ubo-lpa-shim: chrome.browserAction does not exist for a legacy packaged app.
//
// _manifest_features.json limits the browser_action manifest key to
// extension_types:["extension"], and _api_features.json makes the browserAction
// API available only when manifest:browser_action is present. A legacy packaged
// app can declare neither, so the API is undefined and webext.js throws while
// building its object literal.
//
// This is prepended to webext.js because that file is imported by the dashboard,
// logger, settings and other uBO pages as well as the background page.
(function(){
if ( chrome.browserAction ) { return; }
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
})();
