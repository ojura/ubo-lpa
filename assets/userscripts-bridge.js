
// ubo-lpa user scripts bridge, inserted into js/vapi-scripting.js. uBO injects
// each site's scriptlets with tabs.executeScript when a navigation commits.
// The injected code arrives after the page's first inline scripts have run, so
// an anti-adblock script in <head> runs before the scriptlets meant to stop
// it. The companion extension can use chrome.userScripts, which runs
// registered code at document_start, before any page script. For each hostname
// that has scriptlet filters of its own, this bridge sends the companion two
// scripts: the main-world scriptlets, and the isolated-world scriptlets, which
// the companion registers in the user script world.
//
// uBO still injects on every navigation. Before the injected code runs one of
// its two parts, the main-world or the isolated-world scriptlets, it
// dispatches a cancelable event for that part, called the presence event. A
// registered script that has run its scriptlets in the document cancels the
// event, and the injected code then skips that part. Each scriptlet therefore
// runs once in a document, whatever the companion has registered at that
// moment.
//
// The event names contain a token that the installer generates, and a page
// cannot learn it. The registered main-world script saves the browser
// functions it uses before any page script runs, so a page that replaces those
// functions later does not see the names. The injected code dispatches its
// events from uBO's isolated world, whose functions a page cannot replace.
//
// Chrome stores an extension's registered scripts as one value. Every change
// writes all of them to the profile and sends all of them to every tab's
// process again. The code for one hostname is 140 to 180 KB with uBO 1.76.2's
// default lists, so the bridge keeps the set small and changes it in as few
// steps as possible. It registers only hostnames with scriptlet filters of
// their own, at most MAX_HOSTS of them, and it does not send a hostname again
// while its code is unchanged. After uBO resets its scriptlets, one message
// has the companion drop the whole set, and a second later one message
// registers the known hostnames again.
import { builtinScriptlets } from './resources/scriptlets.js';

const ubolpaUserScripts = (( ) => {
    const companionId = '__COMP_ID__';
    const token = '__TOKEN__';
    // Chrome stores registrations in the profile, so registrations made by
    // uBO's incognito instance would record the sites visited in incognito
    // windows. That instance therefore does not register scripts. The scripts
    // that the regular instance registered run in incognito tabs as well.
    const enabled = chrome.extension.inIncognitoContext !== true;

    const send = msg => new Promise(resolve => {
        if ( enabled === false ) { return resolve(); }
        try {
            chrome.runtime.sendMessage(companionId, msg, response => {
                resolve(chrome.runtime.lastError ? undefined : response);
            });
        } catch {
            resolve();
        }
    });

    // Returns the code that dispatches the presence event of a part. In the
    // page, that code evaluates to true unless a registered script cancels the
    // event.
    const absent = part =>
        `self.dispatchEvent(new CustomEvent('${token}:${part}', { cancelable: true }))`;

    // Scriptlets that call extension APIs stay in uBO's own isolated world,
    // because the user script world does not have those APIs. The search below
    // looks in each scriptlet and its dependencies for calls to chrome.* and
    // browser.*. Among the isolated-world scriptlets of uBO 1.76, it finds
    // trusted-click-element, which opens closed shadow roots with chrome.dom.
    // Scriptlets imported from user resources stay too, because the bridge
    // cannot tell what they call.
    const ownWorldNames = (( ) => {
        const byName = new Map(builtinScriptlets.map(a => [ a.name, a ]));
        const reExtensionApi = /\b(?:chrome|browser)\s*\??\.\s*[a-z]/;
        const uses = (entry, seen) => {
            if ( entry === undefined || seen.has(entry) ) { return false; }
            seen.add(entry);
            if ( reExtensionApi.test(String(entry.fn)) ) { return true; }
            return (entry.dependencies ?? []).some(name =>
                uses(byName.get(name), seen)
            );
        };
        return new Set(builtinScriptlets
            .filter(a => uses(a, new Set()))
            .map(a => a.fn.name)
        );
    })();
    const ownWorld = name => ownWorldNames.has(name) || name.startsWith('zeta_');

    // The isolated-world code lists its scriptlet functions in one statement,
    // and splitIsolated() rewrites that statement in two copies. In the
    // registered copy (early), which runs at document_start, an empty function
    // replaces each function that stays in uBO's own isolated world. The copy
    // uBO injects (late) runs every function when the registered copy did not
    // run in the document, and otherwise only the functions that stay. The
    // relay that passes log lines to uBO's logger calls vAPI.messaging, which
    // the user script world does not have, so splitIsolated() removes it from
    // the registered copy.
    const reFunctionRefs =
        /^scriptletGlobals\.functionRefs = (?:\/\* \d+ \*\/ )?\[([\w$,]+)\];$/m;
    const reCommChannel = /^initCommChannel\(".*"\);$/m;
    const empty = '( ) => { }';
    const splitIsolated = code => {
        const match = reFunctionRefs.exec(code ?? '');
        if ( match === null ) { return; }
        const names = match[1].split(',');
        const early = names.map(name => ownWorld(name) ? empty : name);
        if ( early.every(a => a === empty) ) { return; }
        const own = names.map(name => ownWorld(name) ? name : empty);
        return {
            early: code
                .replace(match[0], ( ) =>
                    `scriptletGlobals.functionRefs = [${early.join(',')}];`
                )
                .replace(reCommChannel, 'void 0;'),
            late: code.replace(match[0], ( ) =>
                `scriptletGlobals.functionRefs = ${absent('isolated')}\n` +
                `    ? [${names.join(',')}]\n` +
                `    : [${own.join(',')}];`
            ),
        };
    };

    // The registered main-world script. uBO puts two checks of the top frame
    // in front of its scriptlets. They stop the script on a trusted site, and
    // on a site that a dynamic filtering rule allows. The script cancels the
    // presence event only when the checks let the scriptlets run. When they
    // stop it, the injected code decides by its own copy of the checks. The
    // script also answers the pull event with a push event whose detail is its
    // own source. In a frame without an http(s) URL of its own, such as
    // about:blank, js/ubolpa-frames.js dispatches the pull event on the
    // nearest http(s) ancestor and runs the source in the frame.
    const earlyMain = details => [
        '(function uBO_early() {',
        'const apply = Reflect.apply;',
        'const listen = self.addEventListener;',
        'const dispatch = self.dispatchEvent;',
        'const prevent = Event.prototype.preventDefault;',
        'const stop = Event.prototype.stopImmediatePropagation;',
        'const CE = CustomEvent;',
        `if ( apply(dispatch, self, [ new CE('${token}:main', { cancelable: true }) ]) === false ) { return; }`,
        'const source = `(${uBO_early.toString()})();`;',
        'let ran = false;',
        `apply(listen, self, [ '${token}:main', ev => {`,
        '    if ( ran ) { apply(prevent, ev, []); }',
        '    apply(stop, ev, []);',
        '}, true ]);',
        `apply(listen, self, [ '${token}:pull', ev => {`,
        '    apply(stop, ev, []);',
        `    apply(dispatch, self, [ new CE('${token}:push', { detail: source }) ]);`,
        '}, true ]);',
        '(function(uBO_ran) {',
        details.earlyBailoutCode ?? '',
        'uBO_ran();',
        details.mainCode,
        '})(( ) => { ran = true; });',
        '})();',
    ].join('\n');

    // The registered script for the user script world. A page cannot reach
    // that world, so the script calls the browser's functions directly.
    const earlyIsolated = (details, code) => [
        '(function uBO_earlyIsolated() {',
        `if ( ${absent('isolated')} === false ) { return; }`,
        'let ran = false;',
        `self.addEventListener('${token}:isolated', ev => {`,
        '    if ( ran ) { ev.preventDefault(); }',
        '    ev.stopImmediatePropagation();',
        '}, true);',
        '(function(uBO_ran) {',
        details.earlyBailoutCode ?? '',
        'uBO_ran();',
        code,
        '})(( ) => { ran = true; });',
        '})();',
    ].join('\n');

    // Returns true when the hostname has scriptlet filters of its own. Every
    // hostname's code includes the filters that apply everywhere and the
    // filters for frames inside other sites. A hostname that has only those is
    // left to uBO's own injection, as in an unpatched uBO.
    //
    // details.ubolpaTargets holds the hostname parts of the filters that uBO
    // collected for the hostname. Edit 8 adds that list to the result of
    // getContentScripts() in js/scriptlet-filtering.js. uBO collects '*' for a
    // filter that applies everywhere, the hostname or entity that matched, and
    // 'site>>' for a filter that applies to frames inside that site. It also
    // collects every 'hostname/path' and '/regex/' target, whatever the
    // hostname, so hasOwn() tests those against the hostname.
    const hasOwn = details => {
        const hostname = details?.hostname ?? '';
        for ( const target of details?.ubolpaTargets ?? [] ) {
            if ( target === '*' || target.endsWith('>>') ) { continue; }
            const slash = target.indexOf('/');
            if ( slash === -1 ) { return true; }
            if ( slash !== 0 ) {
                const hn = target.slice(0, slash);
                if ( hostname === hn || hostname.endsWith(`.${hn}`) ) { return true; }
                continue;
            }
            // As in extractSubTargets() in js/static-ext-filtering-db.js, only
            // the part of the regex before its first escaped slash applies to
            // the hostname. A regex without an escaped slash applies to the
            // hostname as a whole.
            const path = target.indexOf('\\/');
            const restr = path !== -1 ? `${target.slice(1, path)}$` : target.slice(1, -1);
            try {
                if ( new RegExp(restr).test(hostname) ) { return true; }
            } catch {
            }
        }
        return false;
    };

    // Computes the key of a hostname's code. consider() sends the code only
    // when its key differs from the key of the code the companion has. With
    // uBO's logger closed, the code from two calls of getContentScripts() for
    // one hostname differs only in a random name, and the key leaves that name
    // out. With the logger open, every call also puts a new secret into the
    // code, so the key changes and the hostname is sent again. The scriptlets'
    // log lines reach the logger only when the registered copy has the same
    // secret as the copy uBO injects.
    const reRandomName = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
    const contentKey = text => {
        // cyrb53, by bryc
        let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
        const s = text.replace(reRandomName, '');
        for ( let i = 0; i < s.length; i++ ) {
            const ch = s.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return `${s.length}.${4294967296 * (2097151 & h2) + (h1 >>> 0)}`;
    };

    // known maps each hostname the bridge keeps track of to the key of the
    // code the companion has for it. The value is null while the companion
    // does not have code for the hostname, and '' when the companion has code
    // from an earlier session, whose key the bridge does not know. The
    // hostname whose scriptlets uBO worked out longest ago comes first.
    const MAX_HOSTS = 48;
    const known = new Map();
    const pending = new Map();
    const dropped = new Set();
    let flushTimer;

    // The companion cannot register scripts while its "Allow user scripts"
    // toggle is off, and it then answers with ok: false. After such an answer,
    // or when the companion does not answer, the bridge still notes the
    // hostnames. When uBO works out a hostname's scriptlets, the bridge then
    // asks the companion again, at most once a minute.
    let unavailable = false;
    let lastAsked = 0;
    // The epoch combines the installer's token and the recipe of the build.
    // The companion drops the scripts that were registered under another
    // epoch, and answers with the hostnames it has scripts for.
    const hello = ( ) => {
        lastAsked = Date.now();
        return send({ what: 'hello', epoch: '__EPOCH__' }).then(response => {
            unavailable = response?.ok !== true;
            if ( unavailable ) { return; }
            for ( const hostname of response.hosts ?? [] ) {
                if ( known.has(hostname) === false ) { known.set(hostname, ''); }
            }
            scheduleRefresh();
        });
    };

    const flush = ( ) => {
        flushTimer = undefined;
        if ( pending.size === 0 && dropped.size === 0 ) { return; }
        const hostnames = Array.from(pending.keys());
        const msg = {
            what: 'setScriptlets',
            hosts: Array.from(pending, ([ hostname, scripts ]) => ({ hostname, ...scripts })),
            drop: Array.from(dropped),
        };
        pending.clear();
        dropped.clear();
        send(msg).then(response => {
            if ( response?.ok === true ) { return; }
            unavailable = true;
            for ( const hostname of hostnames ) {
                if ( known.has(hostname) ) { known.set(hostname, null); }
            }
        });
    };

    // flush() runs in a zero-delay timeout. The hostnames that refresh() has
    // uBO work out in one loop, after a reset and at uBO's start, therefore go
    // to the companion in one message.
    const schedule = ( ) => {
        flushTimer ??= self.setTimeout(flush, 0);
    };

    const drop = hostname => {
        pending.delete(hostname);
        dropped.add(hostname);
        schedule();
    };

    // Puts a hostname last in the list, and drops the first ones when the
    // list is full.
    const note = (hostname, key) => {
        known.delete(hostname);
        known.set(hostname, key);
        for ( const [ oldest, oldestKey ] of known ) {
            if ( known.size <= MAX_HOSTS ) { break; }
            known.delete(oldest);
            if ( oldestKey !== null ) { drop(oldest); }
        }
    };

    const consider = (hostname, details) => {
        const had = known.get(hostname);
        if ( hasOwn(details) === false ) {
            // A hostname that is in the list stays in it, without code. uBO's
            // filters are empty for a moment while it reloads its lists, and
            // the bridge registers the hostname again after the reload.
            if ( had === undefined ) { return; }
            known.set(hostname, null);
            if ( had !== null ) { drop(hostname); }
            return;
        }
        if ( unavailable ) {
            note(hostname, null);
            if ( Date.now() - lastAsked > 60000 ) { hello(); }
            return;
        }
        const scripts = {};
        if ( details.mainCode ) {
            scripts.main = earlyMain(details);
        }
        const split = splitIsolated(details.isolatedCode);
        if ( split !== undefined ) {
            scripts.isolated = earlyIsolated(details, split.early);
        }
        const key = contentKey(`${scripts.main}\n${scripts.isolated}`);
        note(hostname, key);
        if ( key === had ) { return; }
        dropped.delete(hostname);
        pending.set(hostname, scripts);
        schedule();
    };

    // refresh() runs a second after uBO has reset its scriptlets, and a second
    // after the companion has answered hello. For every known hostname that
    // the companion does not have current code for, it has uBO work out the
    // hostname's scriptlets again, so that the companion has them before the
    // next navigation to that hostname. It calls uBO's own
    // registerContentScript(), which keeps the copy uBO injects at that
    // navigation and calls register() with the same result. The registered
    // copy and the copy uBO injects therefore come from one call of
    // getContentScripts().
    let started = false;
    let refreshTimer;
    const refresh = ( ) => {
        refreshTimer = undefined;
        if ( typeof getContentScripts !== 'function' ) { return; }
        for ( const [ hostname, key ] of Array.from(known) ) {
            if ( key !== null && key !== '' ) { continue; }
            contentScriptDict.delete(hostname);
            registerContentScript(`https://${hostname}/`);
        }
    };
    const scheduleRefresh = ( ) => {
        if ( enabled === false ) { return; }
        self.clearTimeout(refreshTimer);
        refreshTimer = self.setTimeout(refresh, 1000);
    };

    return {
        // Returns the details with the copy uBO injects in place of the
        // isolated-world code. From these details, uBO builds the code it
        // injects when a navigation commits.
        late(details) {
            const split = splitIsolated(details?.isolatedCode);
            if ( split === undefined ) { return details; }
            return { ...details, isolatedCode: split.late };
        },
        // Called when uBO has worked out a hostname's scriptlets, for a
        // navigation or from refresh().
        register(hostname, details) {
            if ( enabled === false ) { return; }
            consider(hostname, details);
        },
        // Called when uBO has reset its scriptlets, for example after a filter
        // list update, after a change to a site's switches, and when the
        // logger opens or closes. The registered scripts were made from the
        // previous state, so the companion drops them at once. A second after
        // the last reset, one message registers the known hostnames' scripts
        // again. uBO also resets while it loads its filters at its start. This
        // method ignores those resets, because the scripts registered at that
        // point come from the last session and stay until start() replaces
        // them.
        reset() {
            if ( enabled === false || started === false ) { return; }
            pending.clear();
            dropped.clear();
            for ( const hostname of known.keys() ) {
                known.set(hostname, null);
            }
            send({ what: 'resetScriptlets' });
            scheduleRefresh();
        },
        // Called once, at uBO's start, after its filters are loaded.
        // Registered scripts stay across a browser restart, so that the tabs
        // Chrome restores get their scriptlets before uBO has started. The
        // companion drops them when another build registered them. It answers
        // with the hostnames it has scripts for, and a second later the bridge
        // replaces their scripts with current ones.
        start() {
            started = true;
            if ( enabled === false ) { return; }
            hello();
        },
    };
})();
