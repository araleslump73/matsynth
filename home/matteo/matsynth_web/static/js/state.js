/* MatSynth - state.js
 * Stato condiviso lato client (UNA sola sorgente per pagina) e selezione esplicita (contracts.md 6.8, 9).
 * Il backend resta autoritativo: qui si tiene solo la copia dell'ultimo snapshot/evento ricevuto.
 * Domini: connection, transport, daw, parts, scene, project, soundfont, ports, selection, prefs.
 * Modulo puro (nessun DOM, storage iniettabile) per testarlo sotto Node. */
(function (root, factory) {
    'use strict';
    if (typeof module === 'object' && module.exports) module.exports = { createState: factory };
    if (typeof window !== 'undefined' && root === window) {
        var storage = null;
        try { storage = root.localStorage; } catch (e) { storage = null; }   // accesso puo' lanciare (finestra privata)
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.createState = factory;
        root.MatSynth.state = factory({ storage: storage });
    }
})(typeof window !== 'undefined' ? window : this, function createState(env) {
    'use strict';
    env = env || {};

    var PREFS_KEY = 'matsynth.prefs.v1';
    var SELECTION_KINDS = ['none', 'track', 'clip', 'range', 'notes', 'events'];
    var DOMAINS = ['connection', 'transport', 'daw', 'parts', 'scene', 'project', 'soundfont', 'ports', 'selection', 'prefs'];

    function emptySelection() {
        return { kind: 'none', track: null, clip_ids: [], start_beat: null, end_beat: null,
                 notes: [], event_indices: [], revision: null };
    }

    var data = {
        connection: { connected: false, ever_connected: false, since: null, last_error: null },
        transport: { is_playing: false, is_recording: false, bpm: 120, beats_per_measure: 4, position: 0 },
        daw: null,
        parts: null,                 // {source:'api'|'legacy', items:[{part_id, sound, cc, ...}], conflicts, runtime}
        scene: { scene_id: null, name: null, dirty: false },
        project: { id: null, name: null, dirty: false, file_stat: null },
        soundfont: null,
        ports: null,
        selection: emptySelection(),
        prefs: {}
    };

    var subs = {};   // dominio -> array di funzioni
    DOMAINS.forEach(function (d) { subs[d] = []; });

    function checkDomain(d) {
        if (DOMAINS.indexOf(d) < 0) throw new Error('Dominio sconosciuto: ' + d);
    }

    function notify(domain, meta) {
        // Copia: un subscriber puo' annullare l'iscrizione durante la notifica.
        var list = subs[domain].slice();
        for (var i = 0; i < list.length; i++) {
            try { list[i](data[domain], domain, meta || {}); }
            catch (e) { if (typeof console !== 'undefined') console.error('[state] subscriber', domain, e); }
        }
    }

    function get(domain) { checkDomain(domain); return data[domain]; }

    function set(domain, value, meta) {
        checkDomain(domain);
        data[domain] = value;
        notify(domain, meta);
    }

    // Merge superficiale; notifica solo se qualche campo e' realmente cambiato
    // (i tick a 10-20 Hz non devono svegliare i subscriber senza motivo).
    function patch(domain, partial, meta) {
        checkDomain(domain);
        var cur = data[domain] || {};
        var changed = false;
        var next = {};
        Object.keys(cur).forEach(function (k) { next[k] = cur[k]; });
        Object.keys(partial).forEach(function (k) {
            if (next[k] !== partial[k]) { next[k] = partial[k]; changed = true; }
        });
        if (!changed) return false;
        data[domain] = next;
        notify(domain, meta);
        return true;
    }

    function subscribe(domain, fn) {
        checkDomain(domain);
        subs[domain].push(fn);
        var active = true;
        return function unsubscribe() {
            if (!active) return;
            active = false;
            var i = subs[domain].indexOf(fn);
            if (i >= 0) subs[domain].splice(i, 1);
        };
    }

    function subscriberCount(domain) { return subs[domain].length; }

    // ── Trasporto e stato DAW ───────────────────────────────────────────────
    function clipIdsOf(daw, track) {
        var ids = {};
        if (!daw || !daw.clips) return ids;
        Object.keys(daw.clips).forEach(function (t) {
            if (track !== null && track !== undefined && String(t) !== String(track)) return;
            (daw.clips[t] || []).forEach(function (c) { ids[c.id] = true; });
        });
        return ids;
    }

    // 6.8: a ogni nuova revision, note/eventi invalidati, clip filtrati su quelli esistenti, range conservato.
    function applyRevisionToSelection(daw) {
        var sel = data.selection;
        if (daw.revision === undefined || daw.revision === null) return;
        if (sel.kind === 'none' || sel.revision === daw.revision) return;
        var next = JSON.parse(JSON.stringify(sel));
        next.notes = [];
        next.event_indices = [];
        var alive = clipIdsOf(daw, sel.track);
        next.clip_ids = sel.clip_ids.filter(function (id) { return alive[id]; });
        if (next.kind === 'clip' && next.clip_ids.length === 0) next.kind = sel.track !== null ? 'track' : 'none';
        if ((next.kind === 'notes' || next.kind === 'events')) next.kind = sel.track !== null ? 'track' : 'none';
        next.revision = daw.revision;
        var changed = JSON.stringify(next) !== JSON.stringify(sel);
        data.selection = next;
        if (changed) notify('selection', { reason: 'revision' });
    }

    function ingestDawState(daw) {
        if (!daw || typeof daw !== 'object') return;
        data.daw = daw;
        notify('daw');
        patch('transport', {
            is_playing: !!daw.is_playing,
            is_recording: !!daw.is_recording,
            bpm: daw.bpm || data.transport.bpm,
            beats_per_measure: daw.beats_per_measure || data.transport.beats_per_measure,
            position: typeof daw.timeline_position === 'number' ? daw.timeline_position : data.transport.position
        });
        applyRevisionToSelection(daw);
    }

    function ingestTick(tick) {
        if (!tick || typeof tick !== 'object') return;
        // Il tick porta solo la posizione e i flag: non tocca `daw` (nessuna revisione nei tick, 9.1).
        patch('transport', {
            is_playing: !!tick.is_playing,
            is_recording: !!tick.is_recording,
            bpm: tick.bpm || data.transport.bpm,
            beats_per_measure: tick.beats_per_measure || data.transport.beats_per_measure,
            position: typeof tick.t === 'number' ? tick.t : data.transport.position
        }, { tick: true });
    }

    // ── Selezione (forma 6.8) ───────────────────────────────────────────────
    function setSelection(sel) {
        var next = emptySelection();
        sel = sel || {};
        if (SELECTION_KINDS.indexOf(sel.kind) < 0 && sel.kind !== undefined) {
            throw new Error('Tipo di selezione non valido: ' + sel.kind);
        }
        next.kind = sel.kind || 'none';
        if (next.kind !== 'none') {
            next.track = Number.isInteger(sel.track) ? sel.track : null;
            next.clip_ids = Array.isArray(sel.clip_ids) ? sel.clip_ids.slice() : [];
            next.start_beat = typeof sel.start_beat === 'number' ? sel.start_beat : null;
            next.end_beat = typeof sel.end_beat === 'number' ? sel.end_beat : null;
            next.notes = Array.isArray(sel.notes) ? sel.notes.map(function (n) { return { on_idx: n.on_idx, off_idx: n.off_idx }; }) : [];
            next.event_indices = Array.isArray(sel.event_indices) ? sel.event_indices.slice() : [];
            // La revisione di riferimento e' quella dello stato DAW corrente se non indicata: serve a invalidare gli indici.
            next.revision = sel.revision !== undefined ? sel.revision :
                            (data.daw && data.daw.revision !== undefined ? data.daw.revision : null);
        }
        data.selection = next;
        notify('selection');
    }

    function clearSelection() { setSelection({ kind: 'none' }); }

    // ── Parti ───────────────────────────────────────────────────────────────
    function getPart(partId) {
        var p = data.parts;
        if (!p || !Array.isArray(p.items)) return null;
        for (var i = 0; i < p.items.length; i++) if (p.items[i].part_id === partId) return p.items[i];
        return null;
    }

    // Aggiorna un CC di una parte senza scrivere sul server (l'invio e' di chi chiama). meta.origin
    // permette ai subscriber di ignorare i propri eco (es. il pannello che ha generato il cambio).
    function updatePartCC(partId, cc, value, meta) {
        var p = data.parts;
        if (!p || !Array.isArray(p.items)) {
            p = { source: 'local', items: [], conflicts: [], runtime: [] };
            data.parts = p;
        }
        // Copy-on-write: chi ha gia' letto lo snapshot precedente non lo vede cambiare di nascosto.
        var items = p.items.slice();
        var idx = -1;
        for (var i = 0; i < items.length; i++) if (items[i].part_id === partId) { idx = i; break; }
        var item = idx >= 0 ? Object.assign({}, items[idx]) : { part_id: partId };
        item.cc = Object.assign({}, item.cc || {});
        item.cc[String(cc)] = value;
        if (idx >= 0) items[idx] = item; else items.push(item);
        data.parts = Object.assign({}, p, { items: items });
        notify('parts', Object.assign({ part_id: partId, cc: cc, value: value }, meta || {}));
    }

    // Evento `parts_update` (9.2): contiene solo le parti cambiate; le altre restano com'erano.
    function mergePartsUpdate(payload) {
        if (!payload || !Array.isArray(payload.parts)) return;
        var cur = data.parts || { source: 'api', items: [], conflicts: [], runtime: [] };
        var items = cur.items.slice();
        payload.parts.forEach(function (np) {
            var idx = -1;
            for (var i = 0; i < items.length; i++) if (items[i].part_id === np.part_id) { idx = i; break; }
            if (idx >= 0) items[idx] = np; else items.push(np);
        });
        items.sort(function (a, b) { return a.part_id - b.part_id; });
        data.parts = {
            source: 'api', items: items,
            runtime: Array.isArray(payload.runtime) ? payload.runtime : cur.runtime,
            conflicts: Array.isArray(payload.conflicts) ? payload.conflicts : cur.conflicts
        };
        notify('parts', { origin: 'server' });
    }

    // ── Preferenze (localStorage con try/catch: puo' mancare o lanciare) ─────
    function loadPrefs() {
        try {
            if (!env.storage) return;
            var raw = env.storage.getItem(PREFS_KEY);
            if (raw) {
                var parsed = JSON.parse(raw);
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) data.prefs = parsed;
            }
        } catch (e) { /* storage assente o JSON corrotto: si riparte da preferenze vuote */ }
    }

    function getPref(key, fallback) {
        var v = data.prefs[key];
        return v === undefined ? fallback : v;
    }

    function setPref(key, value) {
        var next = Object.assign({}, data.prefs);
        next[key] = value;
        data.prefs = next;
        try { if (env.storage) env.storage.setItem(PREFS_KEY, JSON.stringify(next)); } catch (e) { /* quota/privato: resta in memoria */ }
        notify('prefs', { key: key });
    }

    loadPrefs();

    return {
        DOMAINS: DOMAINS,
        PREFS_KEY: PREFS_KEY,
        get: get,
        set: set,
        patch: patch,
        subscribe: subscribe,
        subscriberCount: subscriberCount,
        setSelection: setSelection,
        clearSelection: clearSelection,
        ingestDawState: ingestDawState,
        ingestTick: ingestTick,
        getPart: getPart,
        updatePartCC: updatePartCC,
        mergePartsUpdate: mergePartsUpdate,
        getPref: getPref,
        setPref: setPref
    };
});
