'use strict';
// Logica pura e flussi della fase ui-live (vista Suona, Libreria Scene/SoundFont, pannello sonoro esteso).
// Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const WEB = path.join(__dirname, '..', '..', 'home', 'matteo', 'matsynth_web');
const read = (rel) => fs.readFileSync(path.join(WEB, rel), 'utf8');
const code = (rel) => read(rel).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const pp = require(path.join(WEB, 'static/js/part_panel.js'));
const live = require(path.join(WEB, 'static/js/live.js'));
const lib = require(path.join(WEB, 'static/js/library_scenes.js'));
const { createState } = require(path.join(WEB, 'static/js/state.js'));

/* ── part_panel: parte pura ─────────────────────────────────────────── */

test('parseInstruments: righe "bank-program Nome", scarta il resto, nomi con trattino', () => {
    const l = pp.parseInstruments(['000-000 Piano a coda', '128-016 Batteria - power', 'rumore', '', '001-005  Chorus  ']);
    assert.deepEqual(l, [
        { bank: 0, program: 0, name: 'Piano a coda' },
        { bank: 128, program: 16, name: 'Batteria - power' },
        { bank: 1, program: 5, name: 'Chorus' }]);
    assert.deepEqual(pp.parseInstruments(null), []);
});

test('filterInstruments: parole multiple, accenti e bank-program, limite con totale', () => {
    const l = pp.parseInstruments(['000-000 Pianoforte a coda', '000-001 Pianoforte brillante', '000-024 Chitarra nylon', '128-000 Batteria standard']);
    assert.equal(pp.filterInstruments(l, 'piano coda', 10).total, 1);
    assert.equal(pp.filterInstruments(l, 'PIANOFORTE', 10).total, 2);
    assert.equal(pp.filterInstruments(l, 'batteria', 10).items[0].bank, 128);
    assert.equal(pp.filterInstruments(l, '0-24', 10).items[0].name, 'Chitarra nylon');
    const lim = pp.filterInstruments(l, '', 2);
    assert.equal(lim.items.length, 2);
    assert.equal(lim.total, 4);
    assert.equal(pp.filterInstruments(l, 'xyz', 5).total, 0);
});

test('parseMidiField: vuoto = null, 0..127, errore altrimenti (mai coercizione silenziosa)', () => {
    assert.deepEqual(pp.parseMidiField(''), { value: null, error: null });
    assert.deepEqual(pp.parseMidiField('  '), { value: null, error: null });
    assert.equal(pp.parseMidiField('0').value, 0);
    assert.equal(pp.parseMidiField('127').value, 127);
    ['128', '-1', '1.5', 'abc', '1e2'].forEach(t => assert.ok(pp.parseMidiField(t).error, t));
});

test('allowedControls: interna tutti; esterna solo i dichiarati, nessuno se non dichiara', () => {
    const internal = { destination: { kind: 'internal' } };
    const ext = { destination: { kind: 'external', device_alias: 'fantom' } };
    assert.deepEqual(pp.allowedControls(internal, null).sort((a, b) => a - b), [7, 10, 71, 72, 73, 74, 75]);
    assert.deepEqual(pp.allowedControls(ext, { controls: ['7', '74', '99'] }).sort((a, b) => a - b), [7, 74]);
    assert.deepEqual(pp.allowedControls(ext, { controls: null }), []);
    assert.deepEqual(pp.allowedControls(ext, null), []);
});

test('soundTitle: l\'esterna non mostra mai nomi FluidSynth, solo il nome utente e MSB/LSB/PC', () => {
    const inst = pp.parseInstruments(['000-005 Piano Chorus']);
    const internal = { destination: { kind: 'internal' }, sound: { bank: 0, program: 5, name: null } };
    assert.equal(pp.soundTitle(internal, inst).name, 'Piano Chorus');
    assert.equal(pp.soundTitle(internal, null).name, 'Bank 0 · Programma 5');
    const ext = { destination: { kind: 'external', device_alias: 'f' }, sound: { bank_msb: 1, bank_lsb: null, program: 7, name: null } };
    const t = pp.soundTitle(ext, inst);
    assert.equal(t.name, 'Suono non impostato');
    assert.match(t.detail, /MSB 1/);
    assert.match(t.detail, /Programma 7/);
    assert.doesNotMatch(t.detail, /LSB/);
    assert.ok(!/Piano Chorus/.test(JSON.stringify(t)));
});

test('buildRoutingPatch: solo i gruppi cambiati; interno azzera l\'alias di uscita', () => {
    const part = { input: { device_alias: 'keyboard-main', channel: 0 }, destination: { kind: 'internal', device_alias: null, channel: 0 }, monitor: true };
    const same = pp.draftFromPart(part);
    assert.deepEqual(pp.buildRoutingPatch(part, same), {});
    const d1 = Object.assign({}, same, { kind: 'external', destAlias: 'fantom', destChannel: 4 });
    assert.deepEqual(pp.buildRoutingPatch(part, d1), { destination: { kind: 'external', device_alias: 'fantom', channel: 4 } });
    const d2 = Object.assign({}, same, { inChannel: null, monitor: false });
    assert.deepEqual(pp.buildRoutingPatch(part, d2), { input: { device_alias: 'keyboard-main', channel: null }, monitor: false });
    const d3 = Object.assign({}, same, { destAlias: 'ignorato', destChannel: 3 });
    assert.deepEqual(pp.buildRoutingPatch(part, d3), { destination: { kind: 'internal', device_alias: null, channel: 3 } });
});

test('describeNotes: conflitti 3.6, stato porte e MONITOR_HANDLED_BY_ALSA (forte solo con alsa_plays_on)', () => {
    const part = { input: { device_alias: 'k', channel: 0 }, destination: { kind: 'internal' } };
    const conflicts = [{ type: 'shared_destination', parts: [1, 4] }, { type: 'double_monitor', parts: [1, 2] }, { type: 'shared_input', parts: [5, 6] }];
    const n = pp.describeNotes(1, conflicts, null, part);
    assert.deepEqual(n.map(x => x.code), ['shared_destination', 'double_monitor']);
    assert.match(n[0].text, /parte 5/);
    const rt = { input_status: 'missing', destination_status: 'available', warnings: [{ code: 'MONITOR_HANDLED_BY_ALSA', details: {} }] };
    const n2 = pp.describeNotes(0, [], rt, part);
    assert.deepEqual(n2.map(x => [x.code, x.level]), [['input_status', 'warn'], ['MONITOR_HANDLED_BY_ALSA', 'info']]);
    const rt2 = { input_status: 'available', destination_status: 'offline', warnings: [{ code: 'MONITOR_HANDLED_BY_ALSA', details: { alsa_plays_on: { kind: 'internal', channel: 2 } } }] };
    const n3 = pp.describeNotes(0, [], rt2, part);
    assert.equal(n3.filter(x => x.level === 'warn').length, 2);
    assert.match(n3[1].text, /canale interno 3/);
});

test('errorText: esito sconosciuto non e\' mai un successo; codici in italiano; extra per chiamante', () => {
    assert.match(pp.errorText({ unknown_outcome: true, error: { message: 'timeout' } }), /Esito non confermato/);
    assert.match(pp.errorText({ error: { code: 'RECORDING_ACTIVE', message: '' } }), /registrazione/);
    assert.match(pp.errorText({ error: { code: 'VALIDATION_FAILED', message: 'no', details: { path: 'parts[1].cc.7' } } }), /parts\[1\]\.cc\.7/);
    assert.equal(pp.errorText({ error: { code: 'X', message: 'm' } }, { X: 'extra' }), 'extra m');
});

test('createCoalescer: un invio ogni `wait` ms per chiave e il valore finale; flush immediato', () => {
    let t = 1000; const timers = []; const sent = [];
    const c = pp.createCoalescer((k, v) => sent.push([k, v]), 40, () => t, (fn, ms) => { timers.push({ fn, at: t + ms }); return timers.length - 1; }, (id) => { if (timers[id]) timers[id].dead = true; });
    c.push('7', 10, null, false);              // primo: parte subito
    t = 1010; c.push('7', 11, null, false);
    t = 1020; c.push('7', 12, null, false);      // coalescati: nessun invio intermedio
    assert.deepEqual(sent, [['7', 10]]);
    t = 1040; timers.filter(x => !x.dead && x.at <= t).forEach(x => { x.dead = true; x.fn(); });
    assert.deepEqual(sent, [['7', 10], ['7', 12]]);    // l'ultimo valore, non 11
    t = 1050; c.push('7', 99, null, true);               // rilascio: valore finale subito
    assert.deepEqual(sent[sent.length - 1], ['7', 99]);
    assert.equal(c.hasPending(), false);
    c.push('10', 1, null, false); c.push('10', 2, null, false); c.flush();
    assert.deepEqual(sent.slice(-1)[0], ['10', 2]);
});

/* ── live.js: parte pura ────────────────────────────────────────────── */

test('master: clampStep arrotonda al passo e limita; masterFromState segna i valori non letti', () => {
    const g = live.MASTER[0];
    assert.equal(live.clampStep(g, 0.30000000000000004), 0.3);
    assert.equal(live.clampStep(g, 9), 3);
    assert.equal(live.clampStep(g, -1), 0);
    assert.equal(live.clampStep(g, 'x'), 1);
    const m = live.masterFromState({ gain: '1.5', 'reverb.level': 2 });
    assert.deepEqual(m.gain, { value: 1.5, known: true });
    assert.deepEqual(m['reverb.level'], { value: 1, known: true });
    assert.deepEqual(m['chorus.level'], { value: 0.4, known: false });
    assert.equal(live.formatMaster(g, 1), '1.0');
});

test('nextPartId gira 1-16; partRowInfo non espone nomi FluidSynth per le esterne', () => {
    assert.equal(live.nextPartId(15, 1), 0);
    assert.equal(live.nextPartId(0, -1), 15);
    const ext = { destination: { kind: 'external', device_alias: 'fantom', channel: 4 }, sound: { name: 'Pad mio', program: 3 } };
    const i = live.partRowInfo(ext, 2, pp, pp.parseInstruments(['000-003 Nome FluidSynth']), null, []);
    assert.equal(i.name, 'Pad mio');
    assert.equal(i.dest, 'fantom · ch 5');
    const n = live.partRowInfo({ destination: { kind: 'internal', channel: 0 }, sound: { bank: 0, program: 3 } }, 0, pp, pp.parseInstruments(['000-003 Nome FluidSynth']), null, [{ type: 'shared_destination', parts: [0, 1] }]);
    assert.equal(n.name, 'Nome FluidSynth');
    assert.equal(n.warn, 1);
});

/* ── library_scenes.js: pura ─────────────────────────────────────────── */

const SC = [
    { id: 'a', name: 'Piano caldo', favorite: true, valid: true, soundfont: { file: 'GeneralUser.sf2' } },
    { id: 'b', name: 'Organo', favorite: false, valid: true, soundfont: null },
    { id: 'c', name: 'Rotta', favorite: true, valid: false, error: { message: 'x' } },
    { id: 'd', name: 'Pad', favorite: false, valid: true, soundfont: { file: 'Piano-Pro.sf2' } }];

test('filterScenes / favoriteScenes / neighborScene', () => {
    assert.deepEqual(lib.filterScenes(SC, 'piano').map(s => s.id), ['a', 'd']);     // anche per SoundFont
    assert.deepEqual(lib.filterScenes(SC, ' ').length, 4);
    assert.deepEqual(lib.favoriteScenes(SC).map(s => s.id), ['a']);                  // la non valida non e' richiamabile
    assert.equal(lib.neighborScene(SC, 'a', 1).id, 'b');
    assert.equal(lib.neighborScene(SC, 'b', 1).id, 'd');                             // salta la non valida
    assert.equal(lib.neighborScene(SC, 'd', 1), null);                               // niente giro ai bordi
    assert.equal(lib.neighborScene(SC, 'a', -1), null);
    assert.equal(lib.neighborScene(SC, null, 1).id, 'a');
    assert.equal(lib.neighborScene(SC, null, -1).id, 'd');
    assert.equal(lib.neighborScene([], 'a', 1), null);
});

test('soundfontView: ogni stato ha testo e tono', () => {
    assert.equal(lib.soundfontView({ state: 'loaded', file: 'X.sf2' }).tone, 'ok');
    assert.equal(lib.soundfontView({ state: 'loading', file: 'X.sf2' }).tone, 'busy');
    assert.match(lib.soundfontView({ state: 'failed', file: 'X.sf2', error: 'boom' }).text, /boom/);
    assert.equal(lib.soundfontView({ state: 'unknown' }).tone, 'warn');
    assert.equal(lib.soundfontView(null).tone, 'dim');
});

/* ── Servizio scene con fake ────────────────────────────────────────── */

const okR = (body) => ({ ok: true, http: 200, body: body || {}, error: null, warnings: (body && body.warnings) || [], unknown_outcome: false });
const errR = (code, details, extra) => Object.assign({ ok: false, http: 409, body: null, error: { code, message: code, details: details || {} }, warnings: [], unknown_outcome: false }, extra || {});

function harness(routes, answers) {
    const calls = []; const notes = []; const dialogs = [];
    const state = createState({});
    const queue = {};
    Object.keys(routes).forEach(k => { queue[k] = Array.isArray(routes[k]) ? routes[k].slice() : [routes[k]]; });
    const api = {
        request(method, url, body) {
            calls.push({ method, url, body });
            const key = method + ' ' + url;
            const q = queue[key];
            if (!q) return Promise.resolve(okR({ scenes: [], scene_state: { scene_id: null, name: null, dirty: false }, parts: [], soundfont: { state: 'idle' } }));
            const r = q.length > 1 ? q.shift() : q[0];
            return Promise.resolve(typeof r === 'function' ? r(body) : r);
        },
        command(ev, payload, fb) { calls.push({ command: ev, payload, fallback: fb && fb.url }); return Promise.resolve(okR()); }
    };
    const ui = {
        choice(o) { dialogs.push({ kind: 'choice', title: o.title, ids: o.actions.map(a => a.id) }); const a = answers.shift(); return Promise.resolve(a === undefined ? o.dismissId : a); },
        prompt(o) { dialogs.push({ kind: 'prompt', title: o.title }); const a = answers.shift(); return Promise.resolve(a === undefined ? null : a); },
        progress() { dialogs.push({ kind: 'progress' }); return { update() {}, close() { dialogs.push({ kind: 'progress-close' }); } }; },
        notify(m, k) { notes.push([k, m]); }
    };
    const svc = lib.createSceneService({ api, state, ui, errorText: pp.errorText, setTimeout: () => 0, clearTimeout: () => {} });
    return { svc, calls, notes, dialogs, state };
}
const recallCalls = (h) => h.calls.filter(c => /\/recall$/.test(c.url));

test('recall riuscito: una sola POST /recall, nessun reload, notifica di successo', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': okR({ recall: {} }) }, []);
    const r = await h.svc.recall('s-1');
    assert.equal(r.ok, true);
    assert.deepEqual(recallCalls(h).map(c => c.body), [{ discard_changes: false, allow_partial: false }]);
    assert.equal(h.notes[0][0], 'ok');
    assert.equal(h.dialogs.length, 0);
});

test('recall con avvisi (PART_UNAVAILABLE) e\' un avviso, non un successo pieno', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': okR({ warnings: [{ code: 'PART_UNAVAILABLE', message: 'Parti saltate' }] }) }, []);
    await h.svc.recall('s-1');
    assert.equal(h.notes[0][0], 'warn');
    assert.match(h.notes[0][1], /Parti saltate/);
});

test('UNSAVED_CHANGES -> Salva: PUT della scena corrente e poi riprova SENZA discard', async () => {
    const h = harness({
        'POST /api/scenes/s-2/recall': [errR('UNSAVED_CHANGES', { scope: 'scene' }), okR({})],
        'PUT /api/scenes/s-1': okR({ scene: {} })
    }, ['save']);
    h.state.set('scene', { scene_id: 's-1', name: 'Attuale', dirty: true });
    const r = await h.svc.recall('s-2');
    assert.equal(r.ok, true);
    assert.deepEqual(h.dialogs[0].ids, ['save', 'discard', 'cancel']);
    const put = h.calls.find(c => c.method === 'PUT');
    assert.equal(put.url, '/api/scenes/s-1');
    assert.deepEqual(recallCalls(h).map(c => c.body.discard_changes), [false, false]);
});

test('UNSAVED_CHANGES -> Scarta riprova con discard_changes:true; Annulla non riprova', async () => {
    let h = harness({ 'POST /api/scenes/s-2/recall': [errR('UNSAVED_CHANGES', { scope: 'scene' }), okR({})] }, ['discard']);
    assert.equal((await h.svc.recall('s-2')).ok, true);
    assert.deepEqual(recallCalls(h).map(c => c.body.discard_changes), [false, true]);
    h = harness({ 'POST /api/scenes/s-2/recall': errR('UNSAVED_CHANGES', { scope: 'scene' }) }, ['cancel']);
    const r = await h.svc.recall('s-2');
    assert.equal(r.ok, false);
    assert.equal(recallCalls(h).length, 1);
});

test('Salva senza scena corrente diventa Salva come (nome richiesto)', async () => {
    const h = harness({ 'POST /api/scenes': okR({ scene: { id: 's-9' } }) }, ['Nuova']);
    h.state.set('scene', { scene_id: null, name: null, dirty: true });
    const r = await h.svc.saveCurrent();
    assert.equal(r.ok, true);
    assert.equal(h.dialogs[0].kind, 'prompt');
    assert.deepEqual(h.calls.find(c => c.method === 'POST').body, { name: 'Nuova' });
});

test('cambio SoundFont in due passi: conferma, carica, attende `loaded`, poi richiama con discard_changes:true', async () => {
    const h = harness({
        'POST /api/scenes/s-3/recall': [errR('SOUNDFONT_CHANGE_REQUIRED', { required: 'B.sf2', loaded: 'A.sf2' }), okR({})],
        'POST /api/soundfont/load': okR({ job: { state: 'loading', file: 'B.sf2' } })
    }, ['go']);
    h.state.set('soundfont', { state: 'loaded', file: 'A.sf2' });
    const p = h.svc.recall('s-3');
    await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    // dopo il 202 il passo (b) NON parte: serve lo stato `loaded` osservato
    assert.equal(recallCalls(h).length, 1);
    assert.equal(h.state.get('soundfont').state, 'loading');
    h.state.set('soundfont', { state: 'loaded', file: 'B.sf2' });
    const r = await p;
    assert.equal(r.ok, true);
    assert.deepEqual(recallCalls(h).map(c => c.body.discard_changes), [false, true]);
    assert.ok(h.dialogs.some(d => d.kind === 'progress-close'));
});

test('SoundFont non caricato (failed): la scena NON viene richiamata e l\'utente vede l\'errore', async () => {
    const h = harness({
        'POST /api/scenes/s-3/recall': errR('SOUNDFONT_CHANGE_REQUIRED', { required: 'B.sf2', loaded: 'A.sf2' }),
        'POST /api/soundfont/load': okR({ job: { state: 'loading', file: 'B.sf2' } })
    }, ['go']);
    const p = h.svc.recall('s-3');
    await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    h.state.set('soundfont', { state: 'failed', file: 'B.sf2', error: 'file corrotto' });
    const r = await p;
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'soundfont');
    assert.equal(recallCalls(h).length, 1);
    assert.ok(h.dialogs.some(d => d.title === 'SoundFont non caricato'));
});

test('Play/Record attivi: controllo anticipato senza richiesta; Stop solo su scelta esplicita', async () => {
    const h = harness({}, ['stop']);
    h.state.set('transport', { is_playing: true, is_recording: false, bpm: 120, beats_per_measure: 4, position: 0 });
    const r = await h.svc.recall('s-1');
    assert.equal(r.reason, 'transport');
    assert.equal(recallCalls(h).length, 0);                                          // nessuna richiesta di richiamo
    const stop = h.calls.find(c => c.command === 'transport_cmd');
    assert.deepEqual(stop.payload, { cmd: 'stop_all' });
    assert.equal(stop.fallback, '/api/daw/stop_all');
    // senza scelta: nessuno Stop automatico
    const h2 = harness({}, ['close']);
    h2.state.set('transport', { is_playing: false, is_recording: true, bpm: 120, beats_per_measure: 4, position: 0 });
    await h2.svc.recall('s-1');
    assert.equal(h2.calls.filter(c => c.command).length, 0);
});

test('TRANSPORT_ACTIVE dal server: spiegazione, nessuna riprova automatica', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': errR('TRANSPORT_ACTIVE', { is_playing: true, is_recording: false }) }, ['close']);
    const r = await h.svc.recall('s-1');
    assert.equal(r.reason, 'transport');
    assert.equal(recallCalls(h).length, 1);
});

test('PORTS_UNAVAILABLE: richiamo parziale solo dopo consenso (allow_partial); rifiuto = nessuna riprova', async () => {
    const det = { parts: [{ part_id: 3, alias: 'fantom', direction: 'output', status: 'missing' }] };
    let h = harness({ 'POST /api/scenes/s-1/recall': [errR('PORTS_UNAVAILABLE', det), okR({ warnings: [{ code: 'PART_UNAVAILABLE', message: 'x' }] })] }, ['partial']);
    const r = await h.svc.recall('s-1');
    assert.equal(r.ok, true);
    assert.deepEqual(recallCalls(h).map(c => c.body.allow_partial), [false, true]);
    assert.equal(h.notes[0][0], 'warn');
    h = harness({ 'POST /api/scenes/s-1/recall': errR('PORTS_UNAVAILABLE', det) }, ['cancel']);
    assert.equal((await h.svc.recall('s-1')).ok, false);
    assert.equal(recallCalls(h).length, 1);
});

test('OPERATION_IN_PROGRESS: solo un avviso breve (toast), nessun dialogo e nessuna riprova', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': errR('OPERATION_IN_PROGRESS') }, []);
    const r = await h.svc.recall('s-1');
    assert.deepEqual([r.ok, r.reason], [false, 'busy']);
    assert.equal(recallCalls(h).length, 1);
    assert.equal(h.dialogs.length, 0);
    assert.equal(h.notes[0][0], 'warn');
    assert.match(h.notes[0][1], /riprova tra poco/);
});

test('PARTIAL_APPLY e\' un errore (mai successo) e rilegge lo stato', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': errR('PARTIAL_APPLY', { committed: true, applied_parts: [0], failed_parts: [1, 2] }, { http: 503 }) }, []);
    const r = await h.svc.recall('s-1');
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'partial-apply');
    assert.ok(!h.notes.some(n => n[0] === 'ok'));
    assert.ok(h.calls.some(c => c.url === '/api/scenes/current'));
});

test('esito sconosciuto (timeout): nessuna riprova automatica, stato riletto', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': errR('CLIENT_TIMEOUT', {}, { unknown_outcome: true, http: 0 }) }, []);
    const r = await h.svc.recall('s-1');
    assert.equal(r.reason, 'unknown');
    assert.equal(recallCalls(h).length, 1);
    assert.ok(h.calls.some(c => c.url === '/api/scenes/current'));
    assert.ok(h.dialogs.some(d => d.title === 'Esito non confermato'));
});

test('Salva come con nome esistente: chiede conferma e usa overwrite_id; "Cambia nome" non sovrascrive', async () => {
    let h = harness({ 'POST /api/scenes': [errR('NAME_EXISTS', { existing_id: 's-7' }), okR({ scene: { id: 's-7' } })] }, ['Piano', 'overwrite']);
    assert.equal((await h.svc.saveAs('')).ok, true);
    const posts = h.calls.filter(c => c.method === 'POST');
    assert.deepEqual(posts.map(c => c.body), [{ name: 'Piano' }, { name: 'Piano', overwrite_id: 's-7' }]);
    h = harness({ 'POST /api/scenes': errR('NAME_EXISTS', { existing_id: 's-7' }) }, ['Piano', 'cancel']);
    assert.equal((await h.svc.saveAs('')).ok, false);
    assert.equal(h.calls.filter(c => c.method === 'POST').length, 1);               // nessun suffisso silenzioso
    h = harness({ 'POST /api/scenes': [errR('NAME_EXISTS', { existing_id: 's-7' }), okR({ scene: {} })] }, ['Piano', 'rename', 'Piano 2']);
    assert.equal((await h.svc.saveAs('')).ok, true);
    assert.deepEqual(h.calls.filter(c => c.method === 'POST').map(c => c.body.name), ['Piano', 'Piano 2']);
});

test('recall concorrente rifiutato; elimina chiede conferma', async () => {
    const h = harness({ 'POST /api/scenes/s-1/recall': okR({}) }, ['cancel']);
    const p1 = h.svc.recall('s-1');
    const p2 = await h.svc.recall('s-1');
    assert.equal(p2.reason, 'busy');
    await p1;
    const r = await h.svc.remove('s-1');
    assert.equal(r.cancelled, true);
    assert.ok(!h.calls.some(c => c.method === 'DELETE'));
});

/* ── Verifiche statiche ─────────────────────────────────────────────── */

test('nessun reload/navigazione, nessuna finestra nativa, nessun fetch diretto, nessun setInterval nei moduli ui-live', () => {
    ['static/js/live.js', 'static/js/library_scenes.js', 'static/js/part_panel.js'].forEach(f => {
        const c = code(f);
        assert.ok(!/location\.(href|reload|assign|replace)|window\.open|history\.go/.test(c), f + ': navigazione');
        assert.ok(!/(^|[^.\w])(confirm|alert|prompt)\s*\(/.test(c.replace(/function\s+\w+/g, 'function F')), f + ': finestra nativa');
        assert.ok(!/(^|[^.\w])fetch\s*\(|XMLHttpRequest/.test(c), f + ': rete fuori da api.js');
        assert.ok(!/setInterval\(/.test(c), f + ': setInterval');
        assert.ok(!/(^|[^.\w])io\s*\(/.test(c), f + ': un solo io()');
        assert.ok(!/dblclick|contextmenu|mouseover|mouseenter|longpress|touchhold/.test(c), f + ': azione solo hover/tasto destro/pressione lunga');
    });
    assert.match(code('static/js/library_scenes.js'), /'\/api\/scenes\/' \+ enc\(id\) \+ '\/recall'/);
});

test('cambio vista: live.js non invia richieste dal gestore viewchange', () => {
    const src = read('static/js/live.js');
    const i = src.indexOf("'matsynth:viewchange'");
    assert.ok(i > 0);
    const body = src.slice(i, src.indexOf('});', i) + 3);
    assert.ok(!/api\.|scenes\.refresh|loadMaster|instruments\.load|\.request\(/.test(body), 'gestore viewchange');
});

test('live.html: id unici attesi, script nell\'ordine giusto; /presets sonda library_scenes.js', () => {
    const html = read('templates/partials/live.html').replace(/\{#[\s\S]*?#\}/g, '');
    ['lv-root', 'lv-scene-name', 'lv-scene-dirty', 'lv-save', 'lv-saveas', 'lv-prev', 'lv-next', 'lv-scene-search', 'lv-partlist', 'lv-part-panel', 'lv-master-rows'].forEach(id => assert.ok(html.includes('id="' + id + '"'), id));
    assert.ok(html.indexOf('library_scenes.js') < html.indexOf('live.js'));
    assert.match(html, /library_scenes\.js" defer/);
    assert.ok(fs.existsSync(path.join(WEB, 'static/js/library_scenes.js')));
    assert.match(read('templates/presets.html'), /\/static\/js\/library_scenes\.js/);
    // gli ID del vecchio segnaposto non esistono piu' (la shell non monta due pannelli sonori)
    assert.ok(!/live-part-panel|live-part-select/.test(html));
    assert.ok(!/initLive|live-part-panel/.test(code('static/js/shell.js')));
});

test('scheda SoundFont rimossa dalle Impostazioni, rimando a Libreria > SoundFont', () => {
    const s = code('static/js/settings.js');
    assert.ok(!/renderSoundfont|\/load_sf2|\/list_sf2/.test(s));
    assert.ok(!/id: 'soundfont'/.test(s));
    assert.match(s, /'library', 'soundfont'/);
});

test('instrumentCategory / instrumentCategories: famiglie GM per program, bank >= 120 = batterie', () => {
    const l = pp.parseInstruments(['000-000 Piano', '000-007 Clavinet', '000-024 Nylon', '000-033 Basso finger', '008-004 Rhodes detune', '120-000 Kit', '128-016 Power']);
    assert.equal(pp.instrumentCategory(l[0]), 'piano');
    assert.equal(pp.instrumentCategory(l[1]), 'piano');
    assert.equal(pp.instrumentCategory(l[2]), 'guitar');
    assert.equal(pp.instrumentCategory(l[4]), 'piano');          // variazioni (bank 8) restano nella famiglia
    assert.equal(pp.instrumentCategory(l[6]), 'drums');
    assert.equal(pp.instrumentCategory(null), null);
    assert.deepEqual(pp.instrumentCategories(l).map(c => c.id + ':' + c.count), ['piano:3', 'guitar:1', 'bass:1', 'drums:2']);
    assert.deepEqual(pp.instrumentCategories([]), []);
});

test('filterInstruments con categoria: si combina con la ricerca e il totale conta solo la categoria', () => {
    const l = pp.parseInstruments(['000-000 Piano', '000-001 Piano brillante', '000-024 Chitarra', '128-000 Piano kit']);
    assert.equal(pp.filterInstruments(l, '', 10, 'piano').total, 2);
    assert.equal(pp.filterInstruments(l, 'piano', 10, 'drums').items[0].name, 'Piano kit');
    assert.equal(pp.filterInstruments(l, 'piano', 10, '').total, 3);
    assert.equal(pp.filterInstruments(l, '', 10, 'organ').total, 0);
});

test('shell: la guardia legacy e\' in bubble con stopImmediatePropagation, registrata prima di legacy_daw.js', () => {
    const sh = code('static/js/shell.js');
    assert.match(sh, /addEventListener\('keydown', guardLegacyShortcuts, false\)/);
    assert.match(sh, /function guardLegacyShortcuts[\s\S]*?stopImmediatePropagation\(\)/);
    assert.doesNotMatch(sh, /function guardLegacyShortcuts\(e\) \{[^}]*\be\.stopPropagation\(\)/);
    const idx = read('templates/index.html');
    assert.ok(idx.indexOf('/static/js/shell.js') < idx.indexOf('/static/js/legacy_daw.js'));
});
