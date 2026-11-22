'use strict';
// Verifiche statiche della pagina composta (contracts.md 12, fase ui-shell). Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const WEB = path.join(__dirname, '..', '..', 'home', 'matteo', 'matsynth_web');
const read = (rel) => fs.readFileSync(path.join(WEB, rel), 'utf8');
// Codice senza commenti (i commenti citano io() a scopo esplicativo).
const code = (rel) => read(rel).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const JS_DIR = path.join(WEB, 'static', 'js');
const jsFiles = fs.readdirSync(JS_DIR).filter(f => f.endsWith('.js'));

// Risolve ricorsivamente {% include 'partials/x.html' %}; i {# commenti #} Jinja sono rimossi.
function compose(rel) {
    return read(rel)
        .replace(/\{#[\s\S]*?#\}/g, '')
        .replace(/\{%\s*include\s+'([^']+)'\s*%\}/g, (m, p) => compose(path.join('templates', p)));
}

test('nessun ID duplicato nella pagina composta (A9)', () => {
    const html = compose('templates/index.html');
    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]);
    const seen = new Map();
    ids.forEach(id => seen.set(id, (seen.get(id) || 0) + 1));
    const dup = [...seen].filter(([, n]) => n > 1).map(([id]) => id);
    assert.deepEqual(dup, [], 'ID duplicati: ' + dup.join(', '));
    assert.ok(seen.has('midi-file-input'));
    assert.ok(seen.has('midi-file-input-modal'));
    assert.ok(ids.length > 100, 'la pagina composta deve contenere il DAW (' + ids.length + ' ID)');
});

test('ogni funzione legacy referenziata da onclick/onchange/oninput esiste in legacy_daw.js o in un modulo', () => {
    const html = compose('templates/index.html');
    const legacy = read('static/js/legacy_daw.js');
    const partPanel = read('static/js/part_panel.js');
    const calls = new Set();
    for (const m of html.matchAll(/\son(?:click|change|input|focus|blur|keydown)="([^"]*)"/g)) {
        for (const c of m[1].matchAll(/(?<![.\w'])([A-Za-z_]\w*)\s*\(/g)) calls.add(c[1]);
    }
    const builtin = new Set(['if', 'event', 'stopPropagation', 'click', 'getElementById', 'blur']);
    const missing = [...calls].filter(fn => !builtin.has(fn) &&
        !new RegExp('function\\s+' + fn + '\\s*\\(').test(legacy) && !new RegExp('\\b' + fn + '\\s*=').test(legacy));
    assert.deepEqual(missing, [], 'handler senza definizione: ' + missing.join(', '));
    // Il pannello legacy generato da part_panel.js usa handler inline che devono esistere in legacy_daw.js.
    const generated = [...partPanel.matchAll(/(updateCC|switchMixerTab|updatePanLabel)\(/g)].map(m => m[1]);
    new Set(generated).forEach(fn => assert.match(legacy, new RegExp('function\\s+' + fn + '\\s*\\(')));
});

test('un solo io() nel codice: solo api.js', () => {
    const offenders = jsFiles.filter(f => f !== 'api.js').filter(f => /(^|[^.\w])io\s*\(/.test(code('static/js/' + f)));
    assert.deepEqual(offenders, []);
    assert.equal((read('static/js/api.js').match(/env\.io\(/g) || []).length, 1);
    assert.ok(!/\bio\(\)/.test(read('templates/index.html')), 'niente io() inline nella pagina');
    assert.match(read('static/js/legacy_daw.js'), /dawSocket\s*=\s*window\.MatSynth\.api\.socket\(\)/);
});

test('shell.showView/openPanel/closePanel non chiamano api (nessun comando al cambio vista)', () => {
    const src = read('static/js/shell.js');
    function body(name) {
        const start = src.indexOf('function ' + name + '(');
        assert.ok(start >= 0, name);
        let depth = 0, i = src.indexOf('{', start);
        const from = i;
        for (; i < src.length; i++) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}' && --depth === 0) break;
        }
        return src.slice(from, i + 1);
    }
    ['showView', 'openPanel', 'hidePanel', 'closePanel'].forEach(fn => {
        assert.ok(!/\bapi\./.test(body(fn)), fn + ' non deve usare api.*');
        assert.ok(!/fetch\(|\.emit\(|XMLHttpRequest/.test(body(fn)), fn + ' non deve fare rete');
    });
});

test('nessun setInterval/setTimeout ricorrente nei moduli nuovi (tranne throttle e toast una tantum)', () => {
    jsFiles.filter(f => f !== 'legacy_daw.js').forEach(f => {
        assert.ok(!/setInterval\(/.test(read('static/js/' + f)), f + ' usa setInterval');
    });
});

test('l\'interfaccia window.MatSynth espone esattamente cio\' che dice contracts.md 11.2', () => {
    // Esegue i moduli in un window finto e controlla la forma dell'interfaccia.
    const vm = require('node:vm');
    const noop = () => {};
    const el = () => new Proxy(function () {}, { get: () => el(), apply: () => el() });
    const window = {
        document: { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, body: { setAttribute: noop, classList: { add: noop, remove: noop } }, dispatchEvent: noop, contains: () => false, createElement: () => el() },
        localStorage: { getItem: () => null, setItem: noop },
        fetch: noop, io: () => ({ connected: false, on: noop, off: noop, emit: noop }), FormData: class {}, AbortController,
        requestAnimationFrame: noop, dispatchEvent: noop, Event: class {}, CustomEvent: class {}, location: { hash: '' }
    };
    window.window = window;
    const ctx = vm.createContext(window);
    for (const f of ['units.js', 'state.js', 'api.js', 'part_panel.js', 'library.js']) {
        vm.runInContext(read('static/js/' + f), ctx, { filename: f });
    }
    const MS = window.MatSynth;
    assert.deepEqual(Object.keys(MS.units).filter(k => ['SNAP_OPTIONS', 'beatsToBBT', 'beatsToSeconds', 'secondsToBeats'].includes(k)).sort(),
        ['SNAP_OPTIONS', 'beatsToBBT', 'beatsToSeconds', 'secondsToBeats']);
    ['request', 'emit', 'on'].forEach(k => assert.equal(typeof MS.api[k], 'function', 'api.' + k));
    ['get', 'subscribe', 'setSelection'].forEach(k => assert.equal(typeof MS.state[k], 'function', 'state.' + k));
    assert.equal(typeof MS.partPanel.mount, 'function');
    assert.equal(typeof MS.library.registerSection, 'function');
    assert.deepEqual(Array.from(MS.state.DOMAINS), ['connection', 'transport', 'daw', 'parts', 'scene', 'project', 'soundfont', 'ports', 'selection', 'prefs']);
    assert.equal(MS.state.PREFS_KEY, 'matsynth.prefs.v1');
});

test('nessuna dipendenza esterna nuova nei moduli (solo CDN gia\' presenti nel template)', () => {
    jsFiles.forEach(f => {
        const src = read('static/js/' + f);
        assert.ok(!/https?:\/\//.test(src.replace(/\/\*[\s\S]*?\*\//g, '')) || f === 'legacy_daw.js', f + ' referenzia URL esterni');
    });
    const css = read('static/css/style.css');
    assert.ok(!/@import|https?:\/\//.test(css));
});
