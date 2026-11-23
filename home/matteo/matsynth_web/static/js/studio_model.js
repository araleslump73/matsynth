/* MatSynth - studio_model.js (fase ui-studio)
 * Logica PURA della vista Studio: geometria della timeline, snap, corsie dei clip sovrapposti, righello,
 * abilitazione dei comandi e costruzione delle richieste. Nessun DOM, nessuna rete: testabile sotto Node
 * (tests/js/studio.test.js). studio.js la usa per disegnare e per inviare i comandi.
 * Regole (contracts.md 6.4, 6.6, 6.8): ogni comando dichiara il suo oggetto (traccia, ID clip, intervallo),
 * niente "ultimo clip"; senza selezione valida il comando e' disabilitato con una motivazione. */
(function (root, factory) {
    var mod = factory();
    if (typeof module === 'object' && module.exports) module.exports = mod;
    else { root.MatSynth = root.MatSynth || {}; root.MatSynth.studioModel = mod; }
})(typeof window !== 'undefined' ? window : this, function () {
    'use strict';

    var MIN_CLIP_BEATS = 0.125;     // come daw_recorder (lunghezza minima di un clip)
    var SPLIT_MARGIN = 0.01;        // margine dai bordi per Dividi (6.4)
    var TRANSPOSE_MAX = 24;         // limite della route clip/transpose
    var ZOOM_LEVELS = [6, 10, 16, 24, 32, 48, 64, 96, 128];   // px per beat
    var DEFAULT_ZOOM = 32;
    // Stessa tavolozza della vista classica (legacy_daw.js TRACK_PALETTE): track_colors e' un indice 0-15.
    var TRACK_PALETTE = ['#00d4ff', '#ff4444', '#44ff44', '#ffcc00', '#ff66ff', '#ff8844', '#44ffff', '#88ff44',
        '#ff4488', '#4488ff', '#ffff44', '#aa44ff', '#44ffaa', '#ff8888', '#88ccff', '#cccccc'];

    function num(v, d) { var n = Number(v); return isFinite(n) ? n : d; }
    function round6(v) { return Math.round(v * 1e6) / 1e6; }

    function snapBeat(beat, snap) {
        var b = num(beat, 0);
        var s = num(snap, 0);
        if (!(s > 0)) return round6(b);
        return round6(Math.round(b / s) * s);
    }

    function beatToX(beat, pxPerBeat) { return num(beat, 0) * pxPerBeat; }
    function xToBeat(x, pxPerBeat) { return pxPerBeat > 0 ? Math.max(0, num(x, 0) / pxPerBeat) : 0; }

    function zoomStep(current, dir) {
        var i = ZOOM_LEVELS.indexOf(current);
        if (i < 0) {
            i = 0;
            while (i < ZOOM_LEVELS.length - 1 && ZOOM_LEVELS[i] < current) i++;
        }
        i = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, i + (dir > 0 ? 1 : -1)));
        return ZOOM_LEVELS[i];
    }

    function clipsOf(daw, track) {
        if (!daw || !daw.clips) return [];
        return daw.clips[track] || daw.clips[String(track)] || [];
    }

    function findClip(daw, track, id) {
        var list = clipsOf(daw, track);
        for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
        return null;
    }

    function positionBeats(daw, positionSeconds) {
        var bpm = daw && daw.bpm > 0 ? daw.bpm : 120;
        return Math.max(0, num(positionSeconds, 0) * bpm / 60);
    }

    // Lunghezza disegnata: tutti i clip, gli eventi liberi (durata traccia) e 8 battute di margine per
    // poter incollare/spostare oltre la fine. Minimo 16 battute.
    function timelineBeats(daw, extraBeats) {
        var bpmeas = daw && daw.beats_per_measure > 0 ? daw.beats_per_measure : 4;
        var end = 0;
        if (daw) {
            if (daw.clips) Object.keys(daw.clips).forEach(function (t) {
                (daw.clips[t] || []).forEach(function (c) { end = Math.max(end, num(c.end_beat, 0)); });
            });
            if (daw.max_duration > 0) end = Math.max(end, positionBeats(daw, daw.max_duration));
            if (daw.loop_end > 0) end = Math.max(end, num(daw.loop_end, 0));
        }
        end = Math.max(end, num(extraBeats, 0));
        var bars = Math.max(16, Math.ceil(end / bpmeas) + 8);
        return bars * bpmeas;
    }

    // Corsie per clip sovrapposti (D3): ogni clip prende la prima corsia libera; `lanes` = corsie del
    // gruppo di sovrapposizione, cosi' l'altezza si divide solo dove serve.
    function layoutClips(clips) {
        var list = (clips || []).slice().sort(function (a, b) {
            return (a.start_beat - b.start_beat) || (a.id - b.id);
        });
        var out = [];
        var group = [];
        var groupEnd = -Infinity;
        var laneEnds = [];
        function closeGroup() {
            var n = laneEnds.length || 1;
            group.forEach(function (g) { g.lanes = n; });
            group = [];
            laneEnds = [];
        }
        list.forEach(function (c) {
            var s = num(c.start_beat, 0), e = num(c.end_beat, s);
            if (group.length && s >= groupEnd) { closeGroup(); groupEnd = -Infinity; }
            var lane = 0;
            while (lane < laneEnds.length && laneEnds[lane] > s) lane++;
            laneEnds[lane] = e;
            var item = { clip: c, lane: lane, lanes: 1 };
            group.push(item);
            out.push(item);
            groupEnd = Math.max(groupEnd, e);
        });
        closeGroup();
        return out;
    }

    // Righello: tacca per battuta (etichetta) e per beat quando c'e' spazio. Solo l'intervallo visibile.
    function rulerTicks(fromBeat, toBeat, pxPerBeat, beatsPerMeasure) {
        var bpm = beatsPerMeasure > 0 ? beatsPerMeasure : 4;
        var showBeats = pxPerBeat >= 12;
        var barEvery = 1;
        while (barEvery * bpm * pxPerBeat < 36) barEvery *= 2;
        var ticks = [];
        var start = Math.max(0, Math.floor(fromBeat / bpm) * bpm);
        for (var b = start; b <= toBeat; b += 1) {
            var isBar = b % bpm === 0;
            var bar = b / bpm;
            if (isBar && bar % barEvery === 0) ticks.push({ beat: b, major: true, label: String(bar + 1) });
            else if (showBeats) ticks.push({ beat: b, major: false, label: '' });
        }
        return ticks;
    }

    function normalizeRange(a, b) {
        var s = Math.max(0, Math.min(num(a, 0), num(b, 0)));
        var e = Math.max(num(a, 0), num(b, 0));
        return { start_beat: round6(s), end_beat: round6(e) };
    }

    // Trascinamento: valori calcolati SOLO dal delta in beat; la richiesta parte al rilascio (un undo).
    function dragMove(clip, deltaBeats, snap) {
        var target = snapBeat(num(clip.start_beat, 0) + num(deltaBeats, 0), snap);
        return Math.max(0, target);
    }

    function dragResize(clip, deltaBeats, snap) {
        var start = num(clip.start_beat, 0);
        var end = snapBeat(num(clip.end_beat, start) + num(deltaBeats, 0), snap);
        return round6(Math.max(start + MIN_CLIP_BEATS, end));
    }

    function selectedClips(daw, sel) {
        if (!sel || sel.kind !== 'clip') return [];
        var out = [];
        (sel.clip_ids || []).forEach(function (id) {
            var c = findClip(daw, sel.track, id);
            if (c) out.push(c);
        });
        return out;
    }

    function splitPointOk(clip, beat) {
        return !!clip && beat > clip.start_beat + SPLIT_MARGIN && beat < clip.end_beat - SPLIT_MARGIN;
    }

    function hasClipboard(daw) {
        var cb = daw && daw.clipboard;
        return !!(cb && cb.kind);   // il server manda null quando gli appunti sono vuoti
    }

    function trackLabel(daw, t) {
        var name = daw && daw.track_names ? (daw.track_names[t] || daw.track_names[String(t)]) : '';
        return name || ('Traccia ' + (t + 1));
    }

    /* Stato dei comandi contestuali. ctx: {daw, sel, transport:{is_playing,is_recording}, busy, cursorBeat}.
     * Ritorna {nome: {enabled, reason}}: `reason` e' la motivazione mostrata quando e' disabilitato. */
    var COMMANDS = ['copy', 'cut', 'paste', 'duplicate', 'split', 'merge', 'delete', 'transpose',
        'create', 'clear_range', 'loop_from_sel', 'move', 'resize', 'duplicate_track', 'clear_track'];

    function commandStates(ctx) {
        var daw = ctx.daw, sel = ctx.sel || { kind: 'none' }, tr = ctx.transport || {};
        var active = !!(tr.is_playing || tr.is_recording);
        var clips = selectedClips(daw, sel);
        var hasTrack = sel.kind !== 'none' && sel.track >= 0 && sel.track <= 15;
        var isRange = sel.kind === 'range' && sel.end_beat > sel.start_beat;
        var one = clips.length === 1 ? clips[0] : null;
        var st = {};
        function set(name, reason) { st[name] = { enabled: !reason, reason: reason || '' }; }
        var busy = ctx.busy ? 'Attendi la risposta del comando precedente' : '';
        var stopFirst = active ? 'Ferma Play/Record per modificare' : '';
        var noObj = 'Seleziona un clip o un intervallo';

        set('copy', busy || (one || isRange ? '' : (clips.length > 1 ? 'Copia un clip alla volta' : noObj)));
        set('cut', busy || stopFirst || (one || isRange ? '' : (clips.length > 1 ? 'Taglia un clip alla volta' : noObj)));
        set('paste', busy || stopFirst || (!hasTrack ? 'Seleziona la traccia di destinazione' :
            (!hasClipboard(daw) ? 'Appunti vuoti: prima Copia o Taglia' : '')));
        // Un gesto = un livello di undo (6.5): con piu' clip selezionati vale solo Unisci.
        var single = one ? '' : (clips.length > 1 ? 'Con piu\' clip selezionati e\' disponibile solo Unisci' : 'Seleziona un clip');
        set('duplicate', busy || stopFirst || single);
        var cursor = num(ctx.cursorBeat, NaN);
        set('split', busy || stopFirst || (!one ? 'Seleziona un solo clip' :
            (!splitPointOk(one, cursor) ? 'Porta il cursore dentro il clip selezionato' : '')));
        set('merge', busy || stopFirst || (clips.length >= 2 ? '' : 'Seleziona almeno 2 clip della stessa traccia'));
        set('delete', busy || stopFirst || single);
        set('transpose', busy || stopFirst || single);
        set('move', busy || stopFirst || (one ? '' : 'Seleziona un solo clip'));
        set('resize', busy || stopFirst || (one ? '' : 'Seleziona un solo clip'));
        set('create', busy || stopFirst || (isRange ? '' : 'Seleziona un intervallo'));
        set('clear_range', busy || stopFirst || (isRange ? '' : 'Seleziona un intervallo'));
        set('loop_from_sel', busy || (isRange || one ? '' : noObj));
        set('duplicate_track', busy || stopFirst || (hasTrack ? '' : 'Seleziona una traccia'));
        set('clear_track', busy || (tr.is_recording ? 'Non disponibile durante il Record' : '') ||
            (hasTrack ? '' : 'Seleziona una traccia'));
        return st;
    }

    /* Richieste HTTP dei comandi (contracts.md 8). Ritorna {method, url, body} oppure {error}.
     * Ogni body porta channel ed expected_revision (6.6). opts: {cursorBeat, semitones, target, ...}. */
    function buildRequest(cmd, daw, sel, opts) {
        opts = opts || {};
        var rev = daw ? daw.revision : undefined;
        var clips = selectedClips(daw, sel);
        var one = clips.length === 1 ? clips[0] : null;
        var t = sel ? sel.track : 0;
        var needOne = ['split', 'move', 'resize', 'delete', 'duplicate', 'transpose'];
        if (needOne.indexOf(cmd) >= 0 && !one) return { error: 'Seleziona un clip' };
        if (cmd === 'merge' && clips.length < 2) return { error: 'Seleziona almeno 2 clip' };
        function body(extra) {
            var b = { channel: t };
            if (rev !== undefined && rev !== null) b.expected_revision = rev;
            Object.keys(extra || {}).forEach(function (k) { b[k] = extra[k]; });
            return b;
        }
        function range() { return { start_beat: sel.start_beat, end_beat: sel.end_beat }; }
        switch (cmd) {
            case 'copy':
            case 'cut':
                return { method: 'POST', url: '/api/daw/' + cmd, body: body(one ? { clip_id: one.id } : range()) };
            case 'paste':
                return { method: 'POST', url: '/api/daw/paste', body: body({ beat: Math.max(0, num(opts.cursorBeat, 0)) }) };
            case 'split':
                return { method: 'POST', url: '/api/daw/clip/split', body: body({ clip_id: one.id, split_beat: num(opts.cursorBeat, 0) }) };
            case 'merge':
                return { method: 'POST', url: '/api/daw/clip/merge', body: body({ clip_ids: clips.map(function (c) { return c.id; }) }) };
            case 'move':
                return { method: 'POST', url: '/api/daw/clip/move', body: body({ clip_id: one.id, target_start_beat: Math.max(0, num(opts.target, 0)) }) };
            case 'resize':
                return { method: 'POST', url: '/api/daw/clip/resize', body: body({ clip_id: one.id, new_end_beat: num(opts.target, 0) }) };
            case 'delete':
                return { method: 'POST', url: '/api/daw/clip/delete', body: body({ clip_id: one.id }) };
            case 'duplicate':
                return { method: 'POST', url: '/api/daw/clip/duplicate', body: body({ clip_id: one.id }) };
            case 'transpose':
                return { method: 'POST', url: '/api/daw/clip/transpose', body: body({ clip_id: one.id,
                    semitones: Math.max(-TRANSPOSE_MAX, Math.min(TRANSPOSE_MAX, Math.round(num(opts.semitones, 0)))) }) };
            case 'create':
                return { method: 'POST', url: '/api/daw/clip/create', body: body(range()) };
            case 'clear_range':
                return { method: 'POST', url: '/api/daw/track/' + t + '/clear_range', body: body(range()) };
            case 'duplicate_track':
                return { method: 'POST', url: '/api/daw/track/' + t + '/duplicate', body: body({
                    dest_channel: opts.dest, include_config: !!opts.includeConfig, confirm_overwrite: !!opts.confirmOverwrite }) };
            case 'clear_track':
                return { method: 'POST', url: '/api/daw/track/' + t + '/clear', body: {} };
            case 'loop_from_sel': {
                var s = one ? one.start_beat : sel.start_beat, e = one ? one.end_beat : sel.end_beat;
                return { method: 'POST', url: '/api/daw/loop_points', body: { start_beat: s, end_beat: e } };
            }
            default:
                return { error: 'Comando sconosciuto: ' + cmd };
        }
    }

    // Selezione dopo una risposta riuscita: il comando dice cosa diventa selezionato.
    function selectionAfter(cmd, sel, body) {
        body = body || {};
        if (cmd === 'split' && body.new_clip_id) return { kind: 'clip', track: sel.track, clip_ids: [body.new_clip_id] };
        if (cmd === 'merge' && body.new_clip_id) return { kind: 'clip', track: sel.track, clip_ids: [body.new_clip_id] };
        if (cmd === 'paste' && body.new_clip_id) return { kind: 'clip', track: sel.track, clip_ids: [body.new_clip_id] };
        if (cmd === 'create' && body.clip) return { kind: 'clip', track: sel.track, clip_ids: [body.clip.id] };
        if (cmd === 'duplicate' && body.new_clip_id) return { kind: 'clip', track: sel.track, clip_ids: [body.new_clip_id] };
        if (cmd === 'delete' || (cmd === 'cut' && sel.kind === 'clip')) return { kind: 'track', track: sel.track };
        return null;
    }

    function describeSelection(daw, sel, beatsPerMeasure, bbt) {
        if (!sel || sel.kind === 'none') return 'Nessuna selezione';
        var tl = trackLabel(daw, sel.track);
        var fmt = function (b) { return bbt ? bbt(b, beatsPerMeasure).text : String(b); };
        if (sel.kind === 'track') return 'Traccia ' + (sel.track + 1) + ' · ' + tl;
        if (sel.kind === 'range') return 'Intervallo ' + fmt(sel.start_beat) + ' – ' + fmt(sel.end_beat) + ' · traccia ' + (sel.track + 1);
        if (sel.kind === 'clip') {
            var clips = selectedClips(daw, sel);
            if (clips.length === 1) return 'Clip «' + (clips[0].name || ('Clip ' + clips[0].id)) + '» · traccia ' + (sel.track + 1);
            return clips.length + ' clip · traccia ' + (sel.track + 1);
        }
        return 'Selezione: ' + sel.kind;
    }

    // Selezione clip con tocco: tocco semplice = solo quel clip; in modalita' "aggiungi" alterna l'ID
    // (solo sulla stessa traccia: Unisci lavora su una traccia).
    function toggleClipSelection(sel, track, id, additive) {
        if (!additive || !sel || sel.kind !== 'clip' || sel.track !== track) return { kind: 'clip', track: track, clip_ids: [id] };
        var ids = (sel.clip_ids || []).slice();
        var i = ids.indexOf(id);
        if (i >= 0) ids.splice(i, 1); else ids.push(id);
        if (!ids.length) return { kind: 'track', track: track };
        return { kind: 'clip', track: track, clip_ids: ids };
    }

    return {
        MIN_CLIP_BEATS: MIN_CLIP_BEATS,
        SPLIT_MARGIN: SPLIT_MARGIN,
        TRANSPOSE_MAX: TRANSPOSE_MAX,
        ZOOM_LEVELS: ZOOM_LEVELS,
        DEFAULT_ZOOM: DEFAULT_ZOOM,
        TRACK_PALETTE: TRACK_PALETTE,
        COMMANDS: COMMANDS,
        snapBeat: snapBeat,
        beatToX: beatToX,
        xToBeat: xToBeat,
        zoomStep: zoomStep,
        clipsOf: clipsOf,
        findClip: findClip,
        positionBeats: positionBeats,
        timelineBeats: timelineBeats,
        layoutClips: layoutClips,
        rulerTicks: rulerTicks,
        normalizeRange: normalizeRange,
        dragMove: dragMove,
        dragResize: dragResize,
        selectedClips: selectedClips,
        splitPointOk: splitPointOk,
        hasClipboard: hasClipboard,
        trackLabel: trackLabel,
        commandStates: commandStates,
        buildRequest: buildRequest,
        selectionAfter: selectionAfter,
        describeSelection: describeSelection,
        toggleClipSelection: toggleClipSelection
    };
});
