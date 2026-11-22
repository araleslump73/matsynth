'use strict';
// Test unitari di state.js. Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createState } = require('../../home/matteo/matsynth_web/static/js/state.js');

function fakeStorage(initial) {
    const m = new Map(Object.entries(initial || {}));
    return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), _m: m };
}

test('get/set/subscribe/unsubscribe', () => {
    const s = createState({});
    const seen = [];
    const off = s.subscribe('scene', (v) => seen.push(v.name));
    s.set('scene', { scene_id: 's-1', name: 'A', dirty: false });
    off(); off();   // doppia annullazione innocua
    s.set('scene', { scene_id: 's-2', name: 'B', dirty: true });
    assert.deepEqual(seen, ['A']);
    assert.equal(s.subscriberCount('scene'), 0);
    assert.throws(() => s.get('inesistente'));
});

test('un subscriber che lancia non blocca gli altri', () => {
    const s = createState({});
    const orig = console.error; console.error = () => {};
    let ok = false;
    s.subscribe('scene', () => { throw new Error('boom'); });
    s.subscribe('scene', () => { ok = true; });
    s.set('scene', { dirty: true });
    console.error = orig;
    assert.ok(ok);
});

test('patch notifica solo se cambia qualcosa', () => {
    const s = createState({});
    let n = 0; s.subscribe('transport', () => n++);
    assert.equal(s.patch('transport', { bpm: 120 }), false);     // stesso valore di default
    assert.equal(s.patch('transport', { bpm: 100 }), true);
    assert.equal(n, 1);
});

test('ingestDawState aggiorna daw e transport; ingestTick non tocca daw', () => {
    const s = createState({});
    const daw = { is_playing: true, is_recording: false, bpm: 90, beats_per_measure: 3, timeline_position: 2.5, revision: 7, clips: {} };
    s.ingestDawState(daw);
    assert.equal(s.get('daw'), daw);
    assert.deepEqual(s.get('transport'), { is_playing: true, is_recording: false, bpm: 90, beats_per_measure: 3, position: 2.5 });
    let dawNotified = 0; s.subscribe('daw', () => dawNotified++);
    s.ingestTick({ t: 3.1, is_playing: true, is_recording: false, bpm: 90, beats_per_measure: 3 });
    assert.equal(s.get('transport').position, 3.1);
    assert.equal(dawNotified, 0);
    assert.equal(s.get('daw'), daw);
});

test('tick identico non notifica, posizione diversa si', () => {
    const s = createState({});
    let n = 0; s.subscribe('transport', () => n++);
    const t = { t: 1, is_playing: true, is_recording: false, bpm: 120, beats_per_measure: 4 };
    s.ingestTick(t); s.ingestTick(Object.assign({}, t));
    assert.equal(n, 1);
    s.ingestTick(Object.assign({}, t, { t: 1.1 }));
    assert.equal(n, 2);
});

test('setSelection: forma 6.8, tipo non valido rifiutato', () => {
    const s = createState({});
    s.setSelection({ kind: 'clip', track: 2, clip_ids: [3] });
    const sel = s.get('selection');
    assert.deepEqual(Object.keys(sel).sort(), ['clip_ids', 'end_beat', 'event_indices', 'kind', 'notes', 'revision', 'start_beat', 'track']);
    assert.equal(sel.kind, 'clip');
    assert.deepEqual(sel.clip_ids, [3]);
    assert.throws(() => s.setSelection({ kind: 'ultimo-clip' }));
    s.clearSelection();
    assert.equal(s.get('selection').kind, 'none');
});

test('selezione: nuova revision invalida note/eventi, filtra clip, conserva range (6.8)', () => {
    const s = createState({});
    s.ingestDawState({ revision: 1, clips: { 0: [{ id: 3 }, { id: 4 }] } });
    s.setSelection({ kind: 'notes', track: 0, notes: [{ on_idx: 1, off_idx: 2 }], clip_ids: [3, 4] });
    assert.equal(s.get('selection').revision, 1);
    s.ingestDawState({ revision: 2, clips: { 0: [{ id: 4 }] } });
    let sel = s.get('selection');
    assert.deepEqual(sel.notes, []);
    assert.deepEqual(sel.clip_ids, [4]);
    assert.equal(sel.kind, 'track');            // note invalidate -> si torna alla traccia
    assert.equal(sel.revision, 2);

    s.setSelection({ kind: 'clip', track: 0, clip_ids: [4] });
    s.ingestDawState({ revision: 3, clips: { 0: [] } });
    assert.equal(s.get('selection').kind, 'track');       // clip scomparso
    assert.deepEqual(s.get('selection').clip_ids, []);

    s.setSelection({ kind: 'range', track: 1, start_beat: 4, end_beat: 8 });
    s.ingestDawState({ revision: 4, clips: {} });
    sel = s.get('selection');
    assert.equal(sel.kind, 'range');
    assert.equal(sel.start_beat, 4);
    assert.equal(sel.end_beat, 8);
});

test('stessa revision non tocca la selezione; i tick mai', () => {
    const s = createState({});
    s.ingestDawState({ revision: 5, clips: { 0: [{ id: 1 }] } });
    s.setSelection({ kind: 'notes', track: 0, notes: [{ on_idx: 0, off_idx: 1 }] });
    let n = 0; s.subscribe('selection', () => n++);
    s.ingestDawState({ revision: 5, clips: { 0: [{ id: 1 }] } });
    s.ingestTick({ t: 9, is_playing: true });
    assert.equal(n, 0);
    assert.equal(s.get('selection').notes.length, 1);
});

test('updatePartCC copy-on-write con meta.origin', () => {
    const s = createState({});
    s.set('parts', { source: 'api', items: [{ part_id: 0, cc: { 7: 100 } }, { part_id: 1, cc: {} }], conflicts: [], runtime: [] });
    const before = s.get('parts');
    const metas = []; s.subscribe('parts', (v, d, m) => metas.push(m));
    s.updatePartCC(0, 7, 64, { origin: 'pannello-1' });
    assert.equal(before.items[0].cc[7], 100, 'lo snapshot precedente non cambia');
    assert.equal(s.getPart(0).cc['7'], 64);
    assert.equal(s.getPart(1).cc['7'], undefined);
    assert.deepEqual(metas[0], { part_id: 0, cc: 7, value: 64, origin: 'pannello-1' });
    s.updatePartCC(5, 10, 33);             // parte non presente: creata
    assert.equal(s.getPart(5).cc['10'], 33);
});

test('mergePartsUpdate sostituisce solo le parti indicate', () => {
    const s = createState({});
    s.set('parts', { source: 'api', items: [{ part_id: 0, v: 1 }, { part_id: 1, v: 1 }], conflicts: [], runtime: [] });
    s.mergePartsUpdate({ parts: [{ part_id: 1, v: 2 }], conflicts: [{ type: 'shared_input' }] });
    assert.deepEqual(s.get('parts').items, [{ part_id: 0, v: 1 }, { part_id: 1, v: 2 }]);
    assert.equal(s.get('parts').conflicts.length, 1);
    s.mergePartsUpdate(null);             // payload malformato ignorato
    assert.equal(s.get('parts').items.length, 2);
});

test('prefs: persistenza in matsynth.prefs.v1, storage corrotto o che lancia', () => {
    const st = fakeStorage();
    const s = createState({ storage: st });
    s.setPref('lastView', 'live');
    assert.equal(s.getPref('lastView', 'studio'), 'live');
    assert.deepEqual(JSON.parse(st._m.get('matsynth.prefs.v1')), { lastView: 'live' });
    const s2 = createState({ storage: st });
    assert.equal(s2.getPref('lastView', 'studio'), 'live');

    assert.equal(createState({ storage: fakeStorage({ 'matsynth.prefs.v1': '{non json' }) }).getPref('x', 7), 7);
    assert.equal(createState({ storage: fakeStorage({ 'matsynth.prefs.v1': '[1,2]' }) }).getPref('x', 8), 8);

    const throwing = { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('QuotaExceeded'); } };
    const s3 = createState({ storage: throwing });
    assert.doesNotThrow(() => s3.setPref('a', 1));
    assert.equal(s3.getPref('a', 0), 1, 'resta in memoria');
    assert.doesNotThrow(() => createState({}).setPref('b', 2));
});
