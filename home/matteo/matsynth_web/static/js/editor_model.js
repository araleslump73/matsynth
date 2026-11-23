/* MatSynth - editor_model.js (fase ui-editor)
 * Logica PURA dell'editor note/eventi: geometria del piano roll, snap, hit-test, selezione a rettangolo,
 * trasformazioni (sposta, ridimensiona, velocity, trasponi, quantizza) e costruzione delle richieste.
 * Nessun DOM, nessuna rete: testabile sotto Node (tests/js/editor.test.js). midi_editor.js la usa.
 * Regole (contracts.md 6.2, 6.6, 8.2, D28-D31): una nota e' la coppia FIFO (on_idx, off_idx) della
 * revisione letta; ogni gesto produce UNA richiesta (notes/batch, add_note, copy/cut/paste, events/batch),
 * quindi un livello di undo e revisione +1. */
(function (root, factory) {
    var mod = factory();
    if (typeof module === 'object' && module.exports) module.exports = mod;
    else { root.MatSynth = root.MatSynth || {}; root.MatSynth.editorModel = mod; }
})(typeof window !== 'undefined' ? window : this, function () {
    'use strict';

    var MAX_BEAT = 100000;          // project_store.MAX_BEAT
    var MIN_LEN = 1 / 64;           // lunghezza minima di una nota (beat)
    var ZOOM_X = [8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256];   // px per beat
    var ZOOM_Y = [8, 10, 12, 16, 20, 24, 32, 44];                   // px per semitono
    var DEFAULT_X = 64, DEFAULT_Y = 16;
    var TRANSPOSE_MAX = 24;
    var NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

    function num(v, d) { var n = Number(v); return isFinite(n) ? n : d; }
    function r6(v) { return Math.round(v * 1e6) / 1e6; }
    function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

    function snapBeat(beat, snap) {
        var b = num(beat, 0), s = num(snap, 0);
        if (!(s > 0)) return r6(b);
        return r6(Math.round(b / s) * s);
    }
    function snapFloor(beat, snap) {
        var b = num(beat, 0), s = num(snap, 0);
        if (!(s > 0)) return r6(b);
        return r6(Math.floor(b / s + 1e-9) * s);
    }

    function noteName(n) { return NOTE_NAMES[((n % 12) + 12) % 12] + (Math.floor(n / 12) - 1); }
    function isBlack(n) { var k = ((n % 12) + 12) % 12; return k === 1 || k === 3 || k === 6 || k === 8 || k === 10; }

    function zoomStep(levels, cur, dir) {
        var i = levels.indexOf(cur);
        if (i < 0) { i = 0; while (i < levels.length - 1 && levels[i] < cur) i++; }
        return levels[clamp(i + (dir > 0 ? 1 : -1), 0, levels.length - 1)];
    }

    // Risposta di GET /api/daw/track/<t>/notes -> note ordinate per inizio.
    // notes: [note, vel, start, end, on_idx, off_idx, clip_id]; off_idx = -1 = nota aperta.
    function normalizeNotes(raw) {
        var out = [];
        (raw || []).forEach(function (n) {
            if (!Array.isArray(n) || n.length < 6) return;
            out.push({ note: n[0], vel: n[1], start: n[2], end: n[3], on: n[4], off: n[5], clip: n[6] || 0 });
        });
        out.sort(function (a, b) { return a.start - b.start || a.note - b.note || a.on - b.on; });
        return out;
    }

    function maxLength(notes) {
        var m = 0;
        for (var i = 0; i < notes.length; i++) { var l = notes[i].end - notes[i].start; if (l > m) m = l; }
        return m;
    }

    // Primo indice con start >= beat (note ordinate per start).
    function lowerBound(notes, beat) {
        var lo = 0, hi = notes.length;
        while (lo < hi) { var mid = (lo + hi) >> 1; if (notes[mid].start < beat) lo = mid + 1; else hi = mid; }
        return lo;
    }

    // Indici delle note che intersecano [b0, b1): solo queste si disegnano (migliaia di note restano leggere).
    function visibleIndices(notes, b0, b1, maxLen) {
        var out = [];
        var ml = maxLen === undefined ? maxLength(notes) : maxLen;
        for (var i = lowerBound(notes, b0 - ml); i < notes.length && notes[i].start < b1; i++) {
            if (notes[i].end > b0 || notes[i].start >= b0) out.push(i);
        }
        return out;
    }

    // Vista: ppb (px/beat), rowH (px/semitono), scrollX/scrollY in px; nota 127 in alto.
    function beatToX(view, beat) { return beat * view.ppb - view.scrollX; }
    function xToBeat(view, x) { return Math.max(0, (x + view.scrollX) / view.ppb); }
    function noteToY(view, note) { return (127 - note) * view.rowH - view.scrollY; }
    function yToNote(view, y) { return clamp(127 - Math.floor((y + view.scrollY) / view.rowH), 0, 127); }

    function noteRect(view, n) {
        var x = beatToX(view, n.start);
        return { x: x, y: noteToY(view, n.note), w: Math.max(2, (n.end - n.start) * view.ppb), h: view.rowH };
    }

    // Hit-test in px: la nota sotto il punto (l'ultima disegnata vince); `part` = 'end' nella zona di
    // ridimensionamento a destra (edgePx, piu' larga al tocco, mai oltre meta' nota), altrimenti 'body'.
    // Con righe basse (< minH) la riga toccabile si allarga verticalmente fino a minH px.
    function hitTest(notes, view, x, y, opts) {
        var o = opts || {};
        var edge = o.edgePx || 10, minH = o.minH || 0;
        var beat = xToBeat(view, x);
        var cand = visibleIndices(notes, beat - (edge / view.ppb), beat + (edge / view.ppb), o.maxLen);
        var pad = Math.max(0, (minH - view.rowH) / 2);
        var best = null;
        for (var k = 0; k < cand.length; k++) {
            var n = notes[cand[k]];
            var r = noteRect(view, n);
            if (y < r.y - pad || y >= r.y + r.h + pad) continue;
            if (x < r.x || x > r.x + r.w) continue;
            var dy = Math.abs(y - (r.y + r.h / 2));
            if (best && dy > best.dy) continue;
            var zone = Math.min(edge, r.w / 2);
            best = { index: cand[k], part: (x >= r.x + r.w - zone) ? 'end' : 'body', dy: dy };
        }
        return best ? { index: best.index, part: best.part } : null;
    }

    // Selezione a rettangolo (beat x semitoni): note che lo intersecano.
    function rectSelect(notes, b0, b1, p0, p1) {
        var lo = Math.min(b0, b1), hi = Math.max(b0, b1);
        var plo = Math.min(p0, p1), phi = Math.max(p0, p1);
        var out = [];
        visibleIndices(notes, lo, hi === lo ? hi + 1e-9 : hi).forEach(function (i) {
            var n = notes[i];
            if (n.note >= plo && n.note <= phi && n.end > lo && n.start <= hi) out.push(i);
        });
        return out;
    }

    function toggleIndex(list, i) {
        var out = list.slice(), k = out.indexOf(i);
        if (k >= 0) out.splice(k, 1); else out.push(i);
        return out;
    }
    function union(a, b) {
        var seen = {}, out = [];
        a.concat(b).forEach(function (i) { if (!seen[i]) { seen[i] = true; out.push(i); } });
        return out;
    }

    // Chiave di contenuto per riselezionare dopo una modifica propria (gli indici cambiano a ogni revisione).
    // Millesimi di beat: il server arrotonda a 4 decimali, cosi' l'arrotondamento non conta.
    function r3(v) { return Math.round(v * 1e3) / 1e3; }
    function noteKey(n) { return n.note + ':' + r3(n.start) + ':' + r3(n.end); }
    function reselect(notes, keys) {
        var want = {};
        keys.forEach(function (k) { want[k] = (want[k] || 0) + 1; });
        var out = [];
        notes.forEach(function (n, i) { var k = noteKey(n); if (want[k]) { want[k]--; out.push(i); } });
        return out;
    }

    function pairOf(n) { return { on_idx: n.on, off_idx: n.off }; }

    // Voce di notes/batch per una nota con i nuovi valori; null se nulla cambia.
    function editEntry(n, next) {
        var e = pairOf(n), changed = false;
        if (next.start !== undefined && r6(next.start) !== r6(n.start)) { e.on_beat = r6(next.start); changed = true; }
        if (n.off >= 0 && next.end !== undefined && r6(next.end) !== r6(n.end)) { e.off_beat = r6(next.end); changed = true; }
        if (next.note !== undefined && next.note !== n.note) { e.note = next.note; changed = true; }
        if (next.vel !== undefined && next.vel !== n.vel) { e.velocity = next.vel; changed = true; }
        return changed ? e : null;
    }

    function collect(notes, sel, fn) {
        var edits = [], keys = [];
        for (var k = 0; k < sel.length; k++) {
            var n = notes[sel[k]];
            if (!n) continue;
            var next = fn(n);
            if (next && next.error) return { error: next.error };
            var e = next ? editEntry(n, next) : null;
            if (e) edits.push(e);
            var nn = next || {};
            keys.push(noteKey({ note: nn.note !== undefined ? nn.note : n.note,
                start: nn.start !== undefined ? nn.start : n.start,
                end: nn.end !== undefined && n.off >= 0 ? nn.end : n.end }));
        }
        return { edits: edits, keys: keys };
    }

    // Trascinamento del gruppo: lo spostamento si limita perche' nessuna nota esca da 0..127 o da 0..MAX_BEAT.
    function clampMove(notes, sel, dBeat, dPitch) {
        var minS = Infinity, maxE = -Infinity, minN = 127, maxN = 0;
        sel.forEach(function (i) {
            var n = notes[i]; if (!n) return;
            minS = Math.min(minS, n.start); maxE = Math.max(maxE, n.end);
            minN = Math.min(minN, n.note); maxN = Math.max(maxN, n.note);
        });
        if (minS === Infinity) return { dBeat: 0, dPitch: 0 };
        return { dBeat: r6(clamp(dBeat, -minS, MAX_BEAT - maxE)), dPitch: clamp(dPitch, -minN, 127 - maxN) };
    }

    function moveEdits(notes, sel, dBeat, dPitch) {
        var c = clampMove(notes, sel, dBeat, dPitch);
        return collect(notes, sel, function (n) {
            return { start: n.start + c.dBeat, end: n.off >= 0 ? n.end + c.dBeat : undefined, note: n.note + c.dPitch };
        });
    }

    function resizeEdits(notes, sel, dBeat, minLen) {
        var ml = minLen > 0 ? minLen : MIN_LEN;
        return collect(notes, sel, function (n) {
            if (n.off < 0) return null;
            return { end: clamp(n.end + dBeat, n.start + ml, MAX_BEAT) };
        });
    }

    // mode 'set': velocity = value; 'add': velocity + value. Sempre 1..127 (0 sarebbe un note-off).
    function velocityEdits(notes, sel, value, mode) {
        return collect(notes, sel, function (n) {
            var v = mode === 'add' ? n.vel + value : value;
            return { vel: clamp(Math.round(v), 1, 127) };
        });
    }

    function transposeEdits(notes, sel, semis) {
        var s = Math.round(num(semis, 0));
        if (s === 0) return { error: 'Semitoni: valore diverso da 0' };
        if (Math.abs(s) > TRANSPOSE_MAX) return { error: 'Semitoni: massimo ' + TRANSPOSE_MAX };
        return collect(notes, sel, function (n) {
            var t = n.note + s;
            if (t < 0 || t > 127) return { error: 'Una nota uscirebbe dalla tastiera (0-127)' };
            return { note: t };
        });
    }

    // Stesso algoritmo di daw_recorder._quantize_beat_pos (grid in beat, swing 0..1 con 0.5 dritto).
    function quantizeBeat(beat, grid, swing, strength) {
        if (!(grid > 0)) return beat;
        var n = Math.round(beat / grid);
        var target = n * grid;
        if (n % 2 !== 0) target += (swing - 0.5) * grid;
        return Math.max(0, beat + (target - beat) * strength);
    }

    // Quantizza l'inizio, conserva la durata (come quantize_tracks); note aperte escluse.
    function quantizeEdits(notes, sel, grid, strength, swing) {
        var st = strength === undefined ? 1 : strength, sw = swing === undefined ? 0.5 : swing;
        return collect(notes, sel, function (n) {
            if (n.off < 0) return null;
            var q = r6(quantizeBeat(n.start, grid, sw, st));
            return { start: q, end: r6(q + (n.end - n.start)) };
        });
    }

    // Disegno: lunghezza ridotta per non sovrapporsi alla nota successiva della stessa altezza
    // (FIFO la riaccoppierebbe). null se l'inizio cade dentro una nota esistente della stessa altezza.
    function drawLength(notes, pitch, start, len) {
        var out = Math.max(MIN_LEN, len);
        for (var i = 0; i < notes.length; i++) {
            var n = notes[i];
            if (n.note !== pitch) continue;
            if (n.start <= start && n.end > start) return null;
            if (n.start > start && n.start < start + out) out = n.start - start;
        }
        return out >= MIN_LEN ? r6(out) : null;
    }

    function selectionBounds(notes, sel) {
        var s = Infinity, e = -Infinity;
        sel.forEach(function (i) { var n = notes[i]; if (n) { s = Math.min(s, n.start); e = Math.max(e, n.end); } });
        return s === Infinity ? null : { start: s, end: e };
    }

    // ── Richieste (corpo e URL; midi_editor.js le invia serializzate) ──
    function tUrl(track, tail) { return '/api/daw/track/' + track + tail; }
    function editRequest(track, rev, edits) {
        if (!edits || !edits.length) return { error: 'Nessuna modifica' };
        return { method: 'PUT', url: tUrl(track, '/notes/batch'), body: { expected_revision: rev, edits: edits } };
    }
    function deleteRequest(track, rev, notes, sel) {
        if (!sel.length) return { error: 'Seleziona almeno una nota' };
        return { method: 'DELETE', url: tUrl(track, '/notes/batch'),
            body: { expected_revision: rev, pairs: sel.map(function (i) { return pairOf(notes[i]); }) } };
    }
    function clipboardRequest(kind, track, rev, notes, sel) {
        if (!sel.length) return { error: 'Seleziona almeno una nota' };
        return { method: 'POST', url: kind === 'cut' ? '/api/daw/cut' : '/api/daw/copy',
            body: { channel: track, expected_revision: rev, pairs: sel.map(function (i) { return pairOf(notes[i]); }) } };
    }
    function pasteRequest(track, rev, beat) {
        return { method: 'POST', url: '/api/daw/paste', body: { channel: track, beat: r6(Math.max(0, beat)), expected_revision: rev } };
    }
    function addRequest(track, rev, pitch, vel, start, len) {
        return { method: 'POST', url: tUrl(track, '/add_note'),
            body: { note: pitch, velocity: clamp(vel, 1, 127), start_beat: r6(start), length_beats: r6(len), expected_revision: rev } };
    }

    // ── Eventi (lista) ──
    var EVENT_KINDS = {
        control_change: 'cc', program_change: 'pc', pitchwheel: 'pb', aftertouch: 'at', polytouch: 'at',
        note_on: 'note', note_off: 'note'
    };
    function eventKind(e) { return EVENT_KINDS[e.type] || 'other'; }
    function filterEvents(events, filter, includeNotes) {
        return (events || []).filter(function (e) {
            var k = eventKind(e);
            if (k === 'note' && !includeNotes) return false;
            return !filter || filter === 'all' || k === filter;
        });
    }
    // Campo valore modificabile per tipo: [chiave nel corpo di events/batch, min, max, chiave nell'evento].
    function valueField(e) {
        switch (eventKind(e)) {
            case 'cc': return { key: 'cc_value', lo: 0, hi: 127, prop: 'cc_val' };
            case 'pc': return { key: 'program', lo: 0, hi: 127, prop: 'program' };
            case 'pb': return { key: 'pitch', lo: -8192, hi: 8191, prop: 'pitch' };
            case 'at': return { key: 'value', lo: 0, hi: 127, prop: 'value' };
            default: return null;
        }
    }
    function eventValue(e) { var f = valueField(e); return f ? e[f.prop] : null; }
    function describeEvent(e) {
        switch (eventKind(e)) {
            case 'cc': return 'CC ' + e.cc_num;
            case 'pc': return 'Program';
            case 'pb': return 'Pitch bend';
            case 'at': return e.type === 'polytouch' ? 'Aftertouch ' + noteName(e.note_num) : 'Aftertouch';
            case 'note': return (e.type === 'note_on' && e.vel > 0 ? 'Nota on ' : 'Nota off ') + (e.note || '');
            default: return e.type;
        }
    }
    // Una voce di events/batch; `patch` = {beat?, value?, cc_num?}. Validazione locale con messaggio.
    function eventEdit(e, patch) {
        var out = { index: e.idx }, f = valueField(e), any = false;
        if (eventKind(e) === 'note') return { error: 'Le note si modificano nel piano roll' };
        if (patch.beat !== undefined) {
            var b = num(patch.beat, NaN);
            if (!(b >= 0 && b <= MAX_BEAT)) return { error: 'Posizione: 0..' + MAX_BEAT + ' beat' };
            if (r6(b) !== r6(e.beat)) { out.beat = r6(b); any = true; }
        }
        if (patch.value !== undefined) {
            if (!f) return { error: 'Valore non modificabile per questo evento' };
            var v = Math.round(num(patch.value, NaN));
            if (!(v >= f.lo && v <= f.hi)) return { error: 'Valore: ' + f.lo + '..' + f.hi };
            if (v !== e[f.prop]) { out[f.key] = v; any = true; }
        }
        if (patch.cc_num !== undefined) {
            if (eventKind(e) !== 'cc') return { error: 'Numero CC solo per i Control Change' };
            var c = Math.round(num(patch.cc_num, NaN));
            if (!(c >= 0 && c <= 127)) return { error: 'Numero CC: 0..127' };
            if (c !== e.cc_num) { out.cc_num = c; any = true; }
        }
        return any ? out : null;
    }
    // Batch su piu' eventi: sposta di dBeat e/o imposta/somma il valore. Errore al primo evento invalido.
    function eventsBatchEdits(events, opts) {
        var edits = [];
        for (var i = 0; i < events.length; i++) {
            var e = events[i], p = {};
            if (opts.dBeat) p.beat = Math.max(0, e.beat + opts.dBeat);
            if (opts.value !== undefined && opts.value !== null && opts.value !== '') {
                var f = valueField(e);
                if (f) p.value = clamp(opts.mode === 'add' ? e[f.prop] + Number(opts.value) : Number(opts.value), f.lo, f.hi);
            }
            var r = eventEdit(e, p);
            if (r && r.error) return { error: r.error };
            if (r) edits.push(r);
        }
        return { edits: edits };
    }
    function eventsEditRequest(track, rev, edits) {
        if (!edits || !edits.length) return { error: 'Nessuna modifica' };
        return { method: 'PUT', url: tUrl(track, '/events/batch'), body: { expected_revision: rev, edits: edits } };
    }
    function eventsDeleteRequest(track, rev, indices) {
        if (!indices || !indices.length) return { error: 'Seleziona almeno un evento' };
        return { method: 'DELETE', url: tUrl(track, '/events/batch'), body: { expected_revision: rev, indices: indices.slice() } };
    }

    return {
        MAX_BEAT: MAX_BEAT, MIN_LEN: MIN_LEN, ZOOM_X: ZOOM_X, ZOOM_Y: ZOOM_Y, DEFAULT_X: DEFAULT_X, DEFAULT_Y: DEFAULT_Y,
        TRANSPOSE_MAX: TRANSPOSE_MAX,
        snapBeat: snapBeat, snapFloor: snapFloor, noteName: noteName, isBlack: isBlack, zoomStep: zoomStep,
        normalizeNotes: normalizeNotes, maxLength: maxLength, visibleIndices: visibleIndices,
        beatToX: beatToX, xToBeat: xToBeat, noteToY: noteToY, yToNote: yToNote, noteRect: noteRect,
        hitTest: hitTest, rectSelect: rectSelect, toggleIndex: toggleIndex, union: union,
        noteKey: noteKey, reselect: reselect, editEntry: editEntry, clampMove: clampMove,
        moveEdits: moveEdits, resizeEdits: resizeEdits, velocityEdits: velocityEdits, transposeEdits: transposeEdits,
        quantizeBeat: quantizeBeat, quantizeEdits: quantizeEdits, drawLength: drawLength, selectionBounds: selectionBounds,
        editRequest: editRequest, deleteRequest: deleteRequest, clipboardRequest: clipboardRequest,
        pasteRequest: pasteRequest, addRequest: addRequest,
        eventKind: eventKind, filterEvents: filterEvents, valueField: valueField, eventValue: eventValue,
        describeEvent: describeEvent, eventEdit: eventEdit, eventsBatchEdits: eventsBatchEdits,
        eventsEditRequest: eventsEditRequest, eventsDeleteRequest: eventsDeleteRequest
    };
});
