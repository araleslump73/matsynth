'use strict';
// Test unitari di editor_model.js (fase ui-editor, contracts.md 6.2, 8.2, D28-D31). Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const m = require('../../home/matteo/matsynth_web/static/js/editor_model.js');

// [note, vel, start, end, on_idx, off_idx, clip]
const RAW = [[64, 90, 2, 3, 2, 3, 1], [60, 100, 0, 1, 0, 1, 1], [67, 80, 4, 8, 4, -1, 0]];
const notes = () => m.normalizeNotes(RAW);
const view = (extra) => Object.assign({ ppb: 10, rowH: 10, scrollX: 0, scrollY: 0 }, extra || {});

test('normalizeNotes: ordina per inizio e conserva coppia e clip', () => {
    const n = notes();
    assert.deepEqual(n.map(x => x.note), [60, 64, 67]);
    assert.deepEqual([n[0].on, n[0].off, n[0].clip], [0, 1, 1]);
    assert.equal(n[2].off, -1);
    assert.deepEqual(m.normalizeNotes([[1, 2]]), []);
});

test('snap: arrotonda e tronca alla griglia, libero = nessuno snap', () => {
    assert.equal(m.snapBeat(1.3, 0.25), 1.25);
    assert.equal(m.snapBeat(1.13, 0.25), 1.25);
    assert.equal(m.snapFloor(1.24, 0.25), 1);
    assert.equal(m.snapFloor(1.25, 0.25), 1.25);
    assert.equal(m.snapBeat(1.2345678, 0), 1.234568);
});

test('geometria: beat/px e nota/riga inverse, nota 127 in alto', () => {
    const v = view({ scrollX: 50, scrollY: 100 });
    assert.equal(m.beatToX(v, 10), 50);
    assert.equal(m.xToBeat(v, 50), 10);
    assert.equal(m.noteToY(v, 127), -100);
    assert.equal(m.yToNote(v, m.noteToY(v, 60) + 5), 60);
    assert.equal(m.yToNote(v, -1e6), 127);
    assert.equal(m.yToNote(v, 1e6), 0);
});

test('visibleIndices: solo le note che intersecano la finestra (anche quelle iniziate prima)', () => {
    const n = notes();
    assert.deepEqual(m.visibleIndices(n, 2.5, 4.5), [1, 2]);
    assert.deepEqual(m.visibleIndices(n, 5, 6), [2]);
    assert.deepEqual(m.visibleIndices(n, 9, 10), []);
});

test('hitTest: corpo, zona di fine, mancato, riga allargata al tocco', () => {
    const n = notes(), v = view();
    const y60 = m.noteToY(v, 60) + 5;
    assert.deepEqual(m.hitTest(n, v, 2, y60, { edgePx: 3 }), { index: 0, part: 'body' });
    assert.deepEqual(m.hitTest(n, v, 9, y60, { edgePx: 3 }), { index: 0, part: 'end' });
    assert.equal(m.hitTest(n, v, 15, y60), null);
    assert.equal(m.hitTest(n, v, 2, y60 + 12), null);
    // Righe da 10 px: con minH 44 un tocco 12 px sotto la riga prende ancora la nota.
    assert.deepEqual(m.hitTest(n, v, 2, y60 + 12, { minH: 44 }), { index: 0, part: 'body' });
    // Nota corta: la zona di fine non supera meta' nota.
    const short = m.normalizeNotes([[60, 100, 0, 0.4, 0, 1, 0]]);
    assert.equal(m.hitTest(short, v, 1, y60, { edgePx: 20 }).part, 'body');
});

test('rectSelect: intersezione beat x altezza, in qualunque verso', () => {
    const n = notes();
    assert.deepEqual(m.rectSelect(n, 0, 2.5, 59, 65), [0, 1]);
    assert.deepEqual(m.rectSelect(n, 2.5, 0, 65, 59), [0, 1]);
    assert.deepEqual(m.rectSelect(n, 0, 10, 66, 70), [2]);
    assert.deepEqual(m.rectSelect(n, 1.5, 1.8, 0, 127), []);
    assert.deepEqual(m.toggleIndex([1, 2], 2), [1]);
    assert.deepEqual(m.union([1, 2], [2, 3]), [1, 2, 3]);
});

test('moveEdits: coppia completa, limiti 0..127 e beat >= 0, nota aperta senza off_beat', () => {
    const n = notes();
    let r = m.moveEdits(n, [0, 1], 1, 2);
    assert.deepEqual(r.edits, [
        { on_idx: 0, off_idx: 1, on_beat: 1, off_beat: 2, note: 62 },
        { on_idx: 2, off_idx: 3, on_beat: 3, off_beat: 4, note: 66 }]);
    assert.deepEqual(r.keys, ['62:1:2', '66:3:4']);
    r = m.moveEdits(n, [0, 1], -5, 100);         // limitato: inizio 0, nota max 127
    assert.deepEqual(r.edits[0], { on_idx: 0, off_idx: 1, note: 123 });
    assert.deepEqual(r.edits[1], { on_idx: 2, off_idx: 3, note: 127 });
    r = m.moveEdits(n, [2], 1, 0);
    assert.deepEqual(r.edits, [{ on_idx: 4, off_idx: -1, on_beat: 5 }]);
    assert.deepEqual(m.moveEdits(n, [0], 0, 0).edits, []);
});

test('resizeEdits: lunghezza minima, nota aperta ignorata', () => {
    const n = notes();
    assert.deepEqual(m.resizeEdits(n, [0], 0.5).edits, [{ on_idx: 0, off_idx: 1, off_beat: 1.5 }]);
    assert.deepEqual(m.resizeEdits(n, [0], -5, 0.25).edits, [{ on_idx: 0, off_idx: 1, off_beat: 0.25 }]);
    assert.deepEqual(m.resizeEdits(n, [2], 1).edits, []);
});

test('velocityEdits e transposeEdits: limiti e messaggi', () => {
    const n = notes();
    assert.deepEqual(m.velocityEdits(n, [0, 1], 127, 'set').edits.map(e => e.velocity), [127, 127]);
    assert.deepEqual(m.velocityEdits(n, [0], -500, 'add').edits[0].velocity, 1);
    assert.deepEqual(m.transposeEdits(n, [0, 1], 12).edits.map(e => e.note), [72, 76]);
    assert.match(m.transposeEdits(m.normalizeNotes([[120, 1, 0, 1, 0, 1, 0]]), [0], 12).error, /tastiera/);
    assert.match(m.transposeEdits(n, [0], 0).error, /diverso/);
    assert.match(m.transposeEdits(n, [0], 25).error, /24/);
});

test('quantizeBeat/quantizeEdits: come il server (forza, swing), durata conservata', () => {
    assert.equal(m.quantizeBeat(1.1, 0.5, 0.5, 1), 1);
    assert.equal(m.quantizeBeat(1.1, 0.5, 0.5, 0.5), 1.05);
    assert.equal(m.quantizeBeat(0.6, 0.5, 0.75, 1), 0.625);   // posizione dispari: swing
    assert.equal(m.quantizeBeat(0.01, 0.5, 0, 1), 0);
    const n = m.normalizeNotes([[60, 100, 0.1, 1.1, 0, 1, 0], [62, 100, 2, 3, 2, 3, 0], [64, 100, 3.2, 9, 4, -1, 0]]);
    assert.deepEqual(m.quantizeEdits(n, [0, 1, 2], 0.5).edits, [{ on_idx: 0, off_idx: 1, on_beat: 0, off_beat: 1 }]);
});

test('drawLength: non sovrapporsi alla nota successiva della stessa altezza', () => {
    const n = notes();
    assert.equal(m.drawLength(n, 60, 0.5, 1), null);           // dentro una nota esistente
    assert.equal(m.drawLength(n, 64, 1, 4), 1);                // fino all'inizio della nota a 2
    assert.equal(m.drawLength(n, 61, 1, 0.5), 0.5);
});

test('reselect: per contenuto, con duplicati contati', () => {
    const n = m.normalizeNotes([[60, 1, 0, 1, 0, 1, 0], [60, 1, 0, 1, 2, 3, 0], [62, 1, 0, 1, 4, 5, 0]]);
    assert.deepEqual(m.reselect(n, ['60:0:1']), [0]);
    assert.deepEqual(m.reselect(n, ['60:0:1', '60:0:1', '62:0:1']), [0, 1, 2]);
    assert.deepEqual(m.reselect(n, [m.noteKey({ note: 62, start: 0.00004, end: 1 })]), [2]);
});

test('richieste: batch con revisione, coppie esplicite, nessuna richiesta vuota', () => {
    const n = notes();
    const e = m.editRequest(3, 9, [{ on_idx: 0, off_idx: 1, note: 61 }]);
    assert.deepEqual(e, { method: 'PUT', url: '/api/daw/track/3/notes/batch',
        body: { expected_revision: 9, edits: [{ on_idx: 0, off_idx: 1, note: 61 }] } });
    assert.ok(m.editRequest(3, 9, []).error);
    assert.deepEqual(m.deleteRequest(3, 9, n, [1, 2]).body.pairs, [{ on_idx: 2, off_idx: 3 }, { on_idx: 4, off_idx: -1 }]);
    assert.ok(m.deleteRequest(3, 9, n, []).error);
    const c = m.clipboardRequest('cut', 3, 9, n, [0]);
    assert.deepEqual([c.url, c.body.channel, c.body.expected_revision], ['/api/daw/cut', 3, 9]);
    assert.equal(m.clipboardRequest('copy', 3, 9, n, [0]).url, '/api/daw/copy');
    assert.deepEqual(m.pasteRequest(3, 9, -1).body, { channel: 3, beat: 0, expected_revision: 9 });
    assert.deepEqual(m.addRequest(3, 9, 60, 0, 1, 0.5).body,
        { note: 60, velocity: 1, start_beat: 1, length_beats: 0.5, expected_revision: 9 });
});

test('eventi: filtro, descrizione, modifica con validazione e batch', () => {
    const ev = [
        { idx: 0, beat: 0, type: 'note_on', note: 'C4', note_num: 60, vel: 100 },
        { idx: 1, beat: 0.5, type: 'control_change', cc_num: 7, cc_val: 90 },
        { idx: 2, beat: 1, type: 'program_change', program: 5 },
        { idx: 3, beat: 2, type: 'pitchwheel', pitch: 0 },
        { idx: 4, beat: 3, type: 'aftertouch', value: 10 }];
    assert.deepEqual(m.filterEvents(ev, 'all').map(e => e.idx), [1, 2, 3, 4]);
    assert.deepEqual(m.filterEvents(ev, 'all', true).length, 5);
    assert.deepEqual(m.filterEvents(ev, 'cc').map(e => e.idx), [1]);
    assert.equal(m.describeEvent(ev[1]), 'CC 7');
    assert.deepEqual(m.eventEdit(ev[1], { value: 64, beat: 1 }), { index: 1, cc_value: 64, beat: 1 });
    assert.equal(m.eventEdit(ev[1], { value: 90 }), null);
    assert.match(m.eventEdit(ev[1], { value: 200 }).error, /0\.\.127/);
    assert.match(m.eventEdit(ev[3], { value: 9000 }).error, /8191/);
    assert.deepEqual(m.eventEdit(ev[3], { value: -8192 }), { index: 3, pitch: -8192 });
    assert.match(m.eventEdit(ev[2], { cc_num: 1 }).error, /CC/);
    assert.ok(m.eventEdit(ev[0], { beat: 1 }).error);
    const b = m.eventsBatchEdits([ev[1], ev[2], ev[4]], { dBeat: -1, value: 5, mode: 'add' });
    assert.deepEqual(b.edits, [{ index: 1, beat: 0, cc_value: 95 }, { index: 2, beat: 0, program: 10 },
        { index: 4, beat: 2, value: 15 }]);
    assert.deepEqual(m.eventsDeleteRequest(0, 4, [1, 2]),
        { method: 'DELETE', url: '/api/daw/track/0/events/batch', body: { expected_revision: 4, indices: [1, 2] } });
    assert.ok(m.eventsEditRequest(0, 4, []).error);
});
