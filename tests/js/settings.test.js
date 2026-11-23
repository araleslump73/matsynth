'use strict';
// Parti pure di static/js/settings.js e controlli statici della fase ui-offline-settings.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const S = require('../../home/matteo/matsynth_web/static/js/settings.js');

const WEB = path.join(__dirname, '..', '..', 'home', 'matteo', 'matsynth_web');
const read = (rel) => fs.readFileSync(path.join(WEB, rel), 'utf8');

test('validateAlias: stessa regola di midi_router.ALIAS_RE', () => {
    ['keyboard-main', 'fantom', 'a', 'x1-y2', 'a'.repeat(32)].forEach(a => assert.equal(S.validateAlias(a), null, a));
    ['', '-x', 'Fantom', 'a b', 'a_b', 'a'.repeat(33), null, 5].forEach(a => assert.notEqual(S.validateAlias(a), null, String(a)));
});

test('portStatusInfo: etichette 3.5 e stato sconosciuto', () => {
    assert.equal(S.portStatusInfo('available').label, 'disponibile');
    assert.equal(S.portStatusInfo('missing').label, 'assente');
    assert.equal(S.portStatusInfo('ambiguous').label, 'ambigua');
    assert.equal(S.portStatusInfo('unbound').label, 'non associato');
    assert.equal(S.portStatusInfo('offline').tone, 'error');
    assert.equal(S.portStatusInfo('boh').label, 'boh');
    assert.equal(S.portStatusInfo(undefined).label, 'sconosciuto');
});

test('descriptorLabel', () => {
    assert.equal(S.descriptorLabel(null), 'Non associato');
    assert.equal(S.descriptorLabel({ client: 'FANTOM-08', port: 'FANTOM-08 MIDI 1' }), 'FANTOM-08 · FANTOM-08 MIDI 1');
    assert.equal(S.descriptorLabel({ client: 'Solo', port: '' }), 'Solo');
    assert.equal(S.descriptorLabel({ client: 'X', port: 'X' }), 'X');
});

test('portChoices: esclude le uscite interne, segna la porta gia\' associata, marca le escluse', () => {
    const ports = {
        inputs: [{ name: 'FANTOM-08:FANTOM-08 MIDI 1 20:0', descriptor: { client: 'FANTOM-08', port: 'FANTOM-08 MIDI 1' }, aliases: ['keyboard-main'], excluded: null },
                 { name: 'Midi Through:Midi Through Port-0 14:0', descriptor: { client: 'Midi Through', port: 'Midi Through Port-0' }, aliases: [], excluded: 'through' }],
        outputs: [{ name: 'FLUID Synth (1):Synth input port (1:0) 128:0', descriptor: { client: 'FLUID Synth', port: 'Synth input port' }, aliases: [], internal: true, excluded: null },
                  { name: 'FANTOM-08:FANTOM-08 MIDI 1 20:0', descriptor: { client: 'FANTOM-08', port: 'FANTOM-08 MIDI 1' }, aliases: ['fantom'], internal: false, excluded: null }]
    };
    const ins = S.portChoices(ports, 'input', 'keyboard-main');
    assert.equal(ins.length, 2);
    assert.equal(ins[0].selected, true);
    assert.equal(ins[1].selected, false);
    assert.match(ins[1].label, /Port-0 \(Midi Through\)$/);
    const outs = S.portChoices(ports, 'output', 'fantom');
    assert.equal(outs.length, 1);
    assert.equal(outs[0].selected, true);
    assert.deepEqual(S.portChoices(null, 'input', 'x'), []);
    assert.deepEqual(S.portChoices({}, 'output', 'x'), []);
});

test('deviceBody: from_port oppure null', () => {
    assert.deepEqual(S.deviceBody('input', 'FANTOM-08:x 20:0'), { input: { from_port: 'FANTOM-08:x 20:0' } });
    assert.deepEqual(S.deviceBody('output', ''), { output: null });
});

test('modeView: effettiva vs configurata e riavvio', () => {
    const same = S.modeView({ effective: 'legacy', configured: 'legacy', restart_required: false });
    assert.equal(same.restartRequired, false);
    assert.equal(same.line, 'In uso: Legacy · Configurata: Legacy');
    const diff = S.modeView({ effective: 'legacy', configured: 'managed', restart_required: true });
    assert.equal(diff.restartRequired, true);
    assert.match(diff.line, /Managed · riavvio necessario/);
    const bad = S.modeView(null);
    assert.equal(bad.effective, null);
    assert.equal(bad.restartRequired, false);
});

test('networkView: indirizzi, URL e rete assente', () => {
    const v = S.networkView({ hostname: 'matsynth', ips: ['192.168.4.1', 'fe80::1', ''] }, { protocol: 'http:', port: '5000' });
    assert.equal(v.mdns, 'matsynth.local');
    assert.equal(v.hasNetwork, true);
    assert.deepEqual(v.addresses.map(a => a.url), ['http://192.168.4.1:5000/', 'http://[fe80::1]:5000/']);
    assert.equal(v.mdnsUrl, 'http://matsynth.local:5000/');
    const none = S.networkView({ hostname: 'matsynth', ips: [] }, { protocol: 'http:', port: '' });
    assert.equal(none.hasNetwork, false);
    assert.equal(S.networkView(null, {}).hostname, '');
});

test('deviceListView: corrente non rilevato e lista malformata', () => {
    const v = S.deviceListView({ devices: [{ id: 'plughw:1', name: 'Scheda 1: USB' }, { id: '', name: 'x' }, null], current: 'plughw:2' });
    assert.deepEqual(v.options, [{ id: 'plughw:1', name: 'Scheda 1: USB' }]);
    assert.equal(v.currentMissing, true);
    assert.equal(S.deviceListView({ devices: [{ id: 'a' }], current: 'a' }).currentMissing, false);
    assert.deepEqual(S.deviceListView('boh').options, []);
});

test('restartGuard: blocca durante Play/Record, avvisa se non salvato', () => {
    assert.equal(S.restartGuard({ transport: { is_recording: true } }).blocked, true);
    assert.match(S.restartGuard({ transport: { is_playing: true } }).reason, /Riproduzione/);
    const g = S.restartGuard({ transport: {}, project: { dirty: true }, scene: { dirty: true } });
    assert.equal(g.blocked, false);
    assert.equal(g.warnings.length, 2);
    assert.deepEqual(S.restartGuard({}).warnings, []);
});

test('evaluateRestart: confermato solo se il servizio e\' sparito E tornato', () => {
    assert.equal(S.evaluateRestart({ elapsed: 3000, sawDown: false, up: true }), 'waiting');
    assert.equal(S.evaluateRestart({ elapsed: 5000, sawDown: true, up: false }), 'waiting');
    assert.equal(S.evaluateRestart({ elapsed: 9000, sawDown: true, up: true }), 'confirmed');
    // mai sparito: un 200 iniziale NON e' prova di riavvio
    assert.equal(S.evaluateRestart({ elapsed: S.RESTART_DOWN_WAIT_MS, sawDown: false, up: true }), 'unconfirmed');
    assert.equal(S.evaluateRestart({ elapsed: S.RESTART_TOTAL_MS, sawDown: true, up: false }), 'unreachable');
});

test('isServiceDown: rete/timeout/5xx = giu\'; 2xx e 4xx = risponde', () => {
    assert.equal(S.isServiceDown({ ok: true, http: 200 }), false);
    assert.equal(S.isServiceDown({ ok: false, http: 0, unknown_outcome: true }), true);
    assert.equal(S.isServiceDown({ ok: false, http: 502 }), true);
    assert.equal(S.isServiceDown({ ok: false, http: 404 }), false);
});

test('errorText: codice DEVICE_IN_USE con parti (1-based) e avviso su esito sconosciuto', () => {
    const r = { ok: false, error: { code: 'DEVICE_IN_USE', message: 'Dispositivo fantom usato da parti', details: { parts: [0, 4] } } };
    assert.equal(S.errorText(r), 'Dispositivo fantom usato da parti (parti: 1, 5)');
    const t = { ok: false, unknown_outcome: true, error: { code: 'CLIENT_TIMEOUT', message: 'Esito non confermato: nessuna risposta entro 8 s', details: {} } };
    assert.match(S.errorText(t), /Controlla lo stato prima di ripetere/);
    assert.equal(S.errorText({ ok: true }), '');
});

// ── Controlli statici ────────────────────────────────────────────────────

test('nessun riferimento a CDN o Google Fonts in template, CSS e JS (esclusi vendor e pagine legacy)', () => {
    const files = [];
    (function walk(dir) {
        fs.readdirSync(path.join(WEB, dir), { withFileTypes: true }).forEach(e => {
            const rel = dir + '/' + e.name;
            if (e.isDirectory()) { if (rel !== 'static/vendor') walk(rel); return; }
            if (/\.(html|css|js)$/.test(e.name) && !/old|loop/i.test(e.name)) files.push(rel);
        });
    })('templates');
    ['static'].forEach(d => (function walk(dir) {
        fs.readdirSync(path.join(WEB, dir), { withFileTypes: true }).forEach(e => {
            const rel = dir + '/' + e.name;
            if (e.isDirectory()) { if (rel !== 'static/vendor') walk(rel); return; }
            if (/\.(html|css|js)$/.test(e.name) && !/old|loop/i.test(e.name)) files.push(rel);
        });
    })(d));
    assert.ok(files.length > 10);
    files.forEach(f => {
        const src = read(f).replace(/\{#[\s\S]*?#\}/g, '').replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
        assert.ok(!/https?:\/\/(?!127\.0\.0\.1)/.test(src), f + ' contiene un URL esterno');
        assert.ok(!/googleapis|gstatic|cdn\./i.test(src), f + ' cita CDN/Google Fonts');
    });
});

test('asset vendor presenti, con licenza accanto, e referenziati dai template', () => {
    ['bootstrap/css/bootstrap.min.css', 'bootstrap/js/bootstrap.bundle.min.js', 'bootstrap/LICENSE',
     'fontawesome/css/fontawesome.min.css', 'fontawesome/css/solid.min.css', 'fontawesome/webfonts/fa-solid-900.woff2', 'fontawesome/LICENSE.txt',
     'socket.io/socket.io.min.js', 'socket.io/LICENSE', 'README.md'].forEach(f => {
        assert.ok(fs.existsSync(path.join(WEB, 'static', 'vendor', f)), f);
    });
    ['templates/index.html', 'templates/presets.html'].forEach(t => {
        const html = read(t);
        assert.match(html, /\/static\/vendor\/bootstrap\/css\/bootstrap\.min\.css/);
        assert.match(html, /\/static\/vendor\/fontawesome\/css\/solid\.min\.css/);
    });
    const index = read('templates/index.html');
    // Socket.IO prima di api.js (api.js cattura window.io al caricamento).
    assert.ok(index.indexOf('/static/vendor/socket.io/socket.io.min.js') < index.indexOf('/static/js/api.js'));
});

test('pannello Impostazioni: contenitori attesi e /settings rimanda alla shell', () => {
    const part = read('templates/partials/settings.html');
    ['id="st-tabs"', 'id="st-body"', 'id="ms-panel-settings-title"', 'settings.js'].forEach(s => assert.ok(part.includes(s), s));
    assert.match(read('templates/settings.html'), /url=\/\?panel=settings/);
});

test('settings.js: ogni richiesta passa da MatSynth.api e nessun riavvio parte da solo', () => {
    const src = read('static/js/settings.js').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    assert.ok(!/\bfetch\s*\(/.test(src), 'niente fetch diretto');
    assert.ok(!/\bio\s*\(/.test(src), 'niente secondo io()');
    assert.ok(!/setInterval/.test(src), 'niente polling periodico');
    assert.ok(!/window\.confirm|\balert\s*\(/.test(src));
    // /api/save_hardware (riavvio) e' raggiungibile solo da requestRestart, chiamata da onConfirm.
    assert.equal((src.match(/save_hardware/g) || []).length, 1);
    assert.equal((src.match(/requestRestart\(/g) || []).length, 3);   // definizione + 2 usi, entrambi dentro onConfirm
});
