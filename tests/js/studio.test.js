'use strict';
// Test unitari di studio_model.js (fase ui-studio, contracts.md 6.4-6.8 e 8.2). Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const m = require('../../home/matteo/matsynth_web/static/js/studio_model.js');

function daw(extra) {
    return Object.assign({
        bpm: 120, beats_per_measure: 4, revision: 7, max_duration: 0, loop_end: 0, clipboard: null,
        track_names: { 0: 'Piano' },
        clips: { 0: [{ id: 1, start_beat: 0, end_beat: 4, name: 'A' }, { id: 2, start_beat: 8, end_beat: 12, name: 'B' }] }
    }, extra || {});
}
const clipSel = (ids, track) => ({ kind: 'clip', track: track || 0, clip_ids: ids });

test('snapBeat: griglia, libero e arrotondamento', () => {
    assert.equal(m.snapBeat(1.3, 0.5), 1.5);
    assert.equal(m.snapBeat(1.2, 0.5), 1);
    assert.equal(m.snapBeat(1.2345678, 0), 1.234568);
    assert.equal(m.snapBeat('x', 1), 0);
});

test('zoomStep resta nei livelli e aggancia valori fuori tabella', () => {
    const L = m.ZOOM_LEVELS;
    assert.equal(m.zoomStep(L[L.length - 1], 1), L[L.length - 1]);
    assert.equal(m.zoomStep(L[0], -1), L[0]);
    assert.ok(L.includes(m.zoomStep(33, 1)));
});

test('altezza e visibilita\' tracce producono geometria compatta senza spazi vuoti', () => {
    const sizes = Array(16).fill('medium');
    const hidden = Array(16).fill(false);
    sizes[1] = 'large';
    sizes[3] = 'compact';
    hidden[1] = true;
    const layout = m.trackGeometry(sizes, hidden);
    assert.equal(layout.heights[1], 110);
    assert.equal(layout.tops[2], 64);
    assert.equal(layout.tops[3], 128);
    assert.equal(layout.totalHeight, 14 * 64 + 40);
    assert.equal(m.trackGeometry(null, null).totalHeight, 16 * 64);
    assert.deepEqual(['large', 'compact', 'medium'], [
        m.nextTrackSize('medium'), m.nextTrackSize('large'), m.nextTrackSize('compact')
    ]);
});

test('timelineBeats: minimo 16 battute, margine di 8 oltre l\'ultimo clip', () => {
    assert.equal(m.timelineBeats(daw({ clips: {} })), 64);
    const d = daw({ clips: { 3: [{ id: 9, start_beat: 0, end_beat: 100 }] } });
    assert.equal(m.timelineBeats(d), (Math.ceil(100 / 4) + 8) * 4);
    // durata eventi liberi in secondi: 60 s a 120 BPM = 120 beat
    assert.equal(m.timelineBeats(daw({ clips: {}, max_duration: 60 })), (30 + 8) * 4);
});

test('layoutClips: corsie solo per i clip sovrapposti (D3)', () => {
    const out = m.layoutClips([
        { id: 3, start_beat: 2, end_beat: 6 },
        { id: 1, start_beat: 0, end_beat: 4 },
        { id: 4, start_beat: 3, end_beat: 5 },
        { id: 5, start_beat: 10, end_beat: 12 }
    ]);
    const by = Object.fromEntries(out.map(o => [o.clip.id, o]));
    assert.deepEqual([by[1].lane, by[3].lane, by[4].lane], [0, 1, 2]);
    assert.deepEqual([by[1].lanes, by[3].lanes, by[4].lanes], [3, 3, 3]);
    assert.deepEqual([by[5].lane, by[5].lanes], [0, 1]);
    // clip adiacenti (fine = inizio) non si sovrappongono
    const adj = m.layoutClips([{ id: 1, start_beat: 0, end_beat: 4 }, { id: 2, start_beat: 4, end_beat: 8 }]);
    assert.deepEqual(adj.map(o => [o.lane, o.lanes]), [[0, 1], [0, 1]]);
});

test('rulerTicks: etichette delle battute diradate quando lo zoom e\' basso', () => {
    const dense = m.rulerTicks(0, 16, 32, 4).filter(t => t.major).map(t => t.label);
    assert.deepEqual(dense, ['1', '2', '3', '4', '5']);
    const sparse = m.rulerTicks(0, 64, 6, 4).filter(t => t.major).map(t => t.label);
    assert.deepEqual(sparse, ['1', '3', '5', '7', '9', '11', '13', '15', '17']);
    assert.ok(m.rulerTicks(0, 16, 6, 4).every(t => t.major));
});

test('dragMove/dragResize: snap, niente beat negativi, durata minima', () => {
    const c = { start_beat: 4, end_beat: 8 };
    assert.equal(m.dragMove(c, 1.3, 1), 5);
    assert.equal(m.dragMove(c, -10, 1), 0);
    assert.equal(m.dragResize(c, 2.2, 0.5), 10);
    assert.equal(m.dragResize(c, -9, 1), 4 + m.MIN_CLIP_BEATS);
    assert.deepEqual(m.normalizeRange(6, 2), { start_beat: 2, end_beat: 6 });
});

test('commandStates: motivazioni con selezione vuota, clip singolo e multipli', () => {
    let st = m.commandStates({ daw: daw(), sel: { kind: 'none' } });
    assert.equal(st.copy.enabled, false);
    assert.match(st.copy.reason, /Seleziona/);
    assert.match(st.paste.reason, /traccia/);

    st = m.commandStates({ daw: daw(), sel: clipSel([1]), cursorBeat: 2 });
    for (const k of ['copy', 'cut', 'duplicate', 'split', 'delete', 'transpose', 'move', 'resize', 'loop_from_sel']) {
        assert.equal(st[k].enabled, true, k);
    }
    assert.equal(st.merge.enabled, false);
    assert.match(st.paste.reason, /Appunti vuoti/);

    st = m.commandStates({ daw: daw(), sel: clipSel([1]), cursorBeat: 6 });
    assert.match(st.split.reason, /cursore/);

    st = m.commandStates({ daw: daw(), sel: clipSel([1, 2]) });
    assert.equal(st.merge.enabled, true);
    assert.equal(st.delete.enabled, false);
    assert.match(st.delete.reason, /solo Unisci/);
});

test('commandStates: trasporto attivo, busy e appunti', () => {
    const d = daw({ clipboard: { kind: 'clip', length_beats: 4 } });
    let st = m.commandStates({ daw: d, sel: clipSel([1]), transport: { is_playing: true } });
    assert.equal(st.copy.enabled, true, 'copiare non modifica: ammesso in Play');
    assert.match(st.cut.reason, /Ferma/);
    assert.match(st.paste.reason, /Ferma/);
    st = m.commandStates({ daw: d, sel: { kind: 'track', track: 0 }, transport: { is_recording: true } });
    assert.match(st.clear_track.reason, /Record/);
    st = m.commandStates({ daw: d, sel: { kind: 'track', track: 0 } });
    assert.equal(st.paste.enabled, true);
    st = m.commandStates({ daw: d, sel: { kind: 'track', track: 0 }, busy: true });
    assert.match(st.paste.reason, /Attendi/);
    st = m.commandStates({ daw: d, sel: { kind: 'range', track: 0, start_beat: 2, end_beat: 6 } });
    assert.equal(st.create.enabled, true);
    assert.equal(st.clear_range.enabled, true);
    assert.equal(st.delete.enabled, false);
});

test('buildRequest: URL, channel, expected_revision e limiti', () => {
    const d = daw();
    let r = m.buildRequest('split', d, clipSel([1]), { cursorBeat: 2 });
    assert.deepEqual(r, { method: 'POST', url: '/api/daw/clip/split',
        body: { channel: 0, expected_revision: 7, clip_id: 1, split_beat: 2 } });
    r = m.buildRequest('transpose', d, clipSel([2]), { semitones: 99 });
    assert.equal(r.body.semitones, m.TRANSPOSE_MAX);
    r = m.buildRequest('merge', d, clipSel([1, 2]));
    assert.deepEqual(r.body.clip_ids, [1, 2]);
    r = m.buildRequest('copy', d, { kind: 'range', track: 0, start_beat: 1, end_beat: 3 });
    assert.deepEqual(r.body, { channel: 0, expected_revision: 7, start_beat: 1, end_beat: 3 });
    r = m.buildRequest('clear_range', d, { kind: 'range', track: 5, start_beat: 1, end_beat: 3 });
    assert.equal(r.url, '/api/daw/track/5/clear_range');
    r = m.buildRequest('move', d, clipSel([1]), { target: -3 });
    assert.equal(r.body.target_start_beat, 0);
    r = m.buildRequest('loop_from_sel', d, clipSel([2]));
    assert.deepEqual(r.body, { start_beat: 8, end_beat: 12 });
    assert.ok(m.buildRequest('delete', d, clipSel([1, 2])).error, 'delete multiplo rifiutato');
    assert.ok(m.buildRequest('delete', d, clipSel([99])).error, 'clip sparito (ID non piu\' valido)');
    assert.ok(m.buildRequest('boh', d, clipSel([1])).error);
});

test('selectionAfter e toggleClipSelection', () => {
    const sel = clipSel([1]);
    assert.deepEqual(m.selectionAfter('split', sel, { new_clip_id: 5 }), clipSel([5]));
    assert.deepEqual(m.selectionAfter('create', { kind: 'range', track: 0 }, { clip: { id: 8 } }), clipSel([8]));
    assert.deepEqual(m.selectionAfter('delete', sel, {}), { kind: 'track', track: 0 });
    assert.equal(m.selectionAfter('move', sel, {}), null);

    let s = m.toggleClipSelection(sel, 0, 2, true);
    assert.deepEqual(s.clip_ids, [1, 2]);
    s = m.toggleClipSelection(s, 0, 1, true);
    assert.deepEqual(s.clip_ids, [2]);
    assert.deepEqual(m.toggleClipSelection(s, 0, 2, true), { kind: 'track', track: 0 });
    assert.deepEqual(m.toggleClipSelection(s, 3, 4, true), clipSel([4], 3), 'altra traccia: riparte');
    assert.deepEqual(m.toggleClipSelection(clipSel([1, 2]), 0, 2, false), clipSel([2]));
});

test('describeSelection e trackLabel', () => {
    const d = daw();
    assert.equal(m.trackLabel(d, 0), 'Piano');
    assert.equal(m.trackLabel(d, 4), 'Traccia 5');
    assert.match(m.describeSelection(d, clipSel([1])), /«A»/);
    assert.match(m.describeSelection(d, clipSel([1, 2])), /^2 clip/);
    assert.equal(m.describeSelection(d, null), 'Nessuna selezione');
});
