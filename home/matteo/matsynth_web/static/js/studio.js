/* MatSynth - studio.js (fase ui-studio)
 * Vista Studio: trasporto unico, tracce (arm/mute/solo), timeline con clip e intervalli, barra contestuale,
 * inspector Clip | Traccia | Suono | Mixer. Logica pura in studio_model.js.
 *
 * Regole (contracts.md 2.6, 6.4-6.8, 9; piano sez. 10):
 *  - Le modifiche sono SERIALIZZATE: una sola richiesta di editing in volo; durante l'attesa i comandi sono
 *    disabilitati. Ogni richiesta porta `expected_revision`; 409 REVISION_CONFLICT = rilettura dello stato,
 *    nessuna ripetizione automatica. Timeout = "Esito non confermato" (si rilegge lo stato, non si ripete).
 *  - Nessun "ultimo clip": ogni comando usa la selezione esplicita (state.selection); senza selezione il
 *    pulsante e' disattivato e il tocco mostra la motivazione.
 *  - Leggero per il Pi/tablet: clip come <div> ricostruiti solo quando cambia la loro firma; righello e mappa
 *    densita' su canvas grandi quanto la parte visibile, ridisegnati su scroll/zoom/dati via rAF; densita'
 *    riletta solo al cambio di revisione (debounce 150 ms) e solo con la vista visibile; nessun timer periodico.
 *  - Trascinamento: anteprima locale (transform), UNA richiesta al rilascio (un livello di undo). */
(function (root) {
    'use strict';
    var MS = root.MatSynth = root.MatSynth || {};
    var doc = root.document;
    var api = MS.api, state = MS.state, units = MS.units, model = MS.studioModel, shell = MS.shell;
    if (!api || !state || !units || !model) return;

    var HEAD_W = 168;          // larghezza intestazione traccia (px), uguale al CSS --st-head
    var ROW_H = 64;            // altezza corsia (px), uguale al CSS --st-row
    var DRAG_PX = 6;           // soglia oltre la quale un tocco diventa trascinamento
    var DENSITY_DEBOUNCE = 150;

    var el = {};
    var ui = {
        legacy: !!state.getPref('studio.legacy', false),
        ppb: state.getPref('studio.zoom', model.DEFAULT_ZOOM),
        snap: state.getPref('studio.snap', 0.25),
        tool: 'select',
        multi: false,
        follow: state.getPref('studio.follow', true) !== false,
        returnToStart: state.getPref('studio.returnToStart', true) !== false,
        tab: 'clip',
        cursorBeat: null,
        busy: false,
        totalBeats: 64,
        rowSig: [],
        drag: null,
        rafPending: false,
        density: null,
        densityRev: null,
        densityTimer: null,
        densityInFlight: false,
        lastDawSeq: 0,
        partPanel: null,
        partPanelTrack: null,
        mixerBuilt: false,
        ccTimers: {},
        reasonTimer: null
    };
    if (model.ZOOM_LEVELS.indexOf(ui.ppb) < 0) ui.ppb = model.DEFAULT_ZOOM;

    function $(id) { return doc.getElementById(id); }
    function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
    function daw() { return state.get('daw') || {}; }
    function sel() { return state.get('selection') || { kind: 'none' }; }
    function transport() { return state.get('transport') || {}; }
    function isActive() { var t = transport(); return !!(t.is_playing || t.is_recording); }
    function visible() {
        var v = $('view-studio');
        return !!(v && !v.hidden && !ui.legacy);
    }
    function toast(msg, kind) { if (shell && shell.toast) shell.toast(msg, kind); }
    function posBeat() { return model.positionBeats(daw(), transport().position); }
    function cursorOrPlayhead() { return ui.cursorBeat !== null ? ui.cursorBeat : posBeat(); }
    function fmtBeat(b) { return units.beatsToBBT(b, daw().beats_per_measure || 4).text; }

    function showReason(text) {
        setText(el.reason, text || '');
        clearTimeout(ui.reasonTimer);
        if (text) ui.reasonTimer = setTimeout(function () { setText(el.reason, ''); }, 4000);
    }

    /* ── Rete: comandi serializzati ─────────────────────────────────── */
    function errorText(r) {
        if (r.unknown_outcome) return 'Esito non confermato: controlla lo stato prima di ripetere';
        var e = r.error || {};
        if (e.code === 'REVISION_CONFLICT') return 'Il progetto e\' cambiato: stato riletto, ripeti il comando';
        if (e.code === 'TRANSPORT_ACTIVE') return 'Ferma Play/Record prima di modificare';
        if (e.code === 'RECORDING_ACTIVE') return 'Non disponibile durante il Record';
        if (e.code === 'EMPTY_CLIPBOARD') return 'Appunti vuoti';
        return e.message || 'Operazione non riuscita';
    }

    function refreshState() {
        return api.request('GET', '/api/daw/state').then(function (r) {
            if (r.ok && r.body && r.body.data) state.ingestDawState(r.body.data);
        });
    }

    // Una sola modifica alla volta. `after(body)` riceve il corpo della risposta riuscita.
    function edit(req, label, after) {
        if (ui.busy) { showReason('Attendi la risposta del comando precedente'); return Promise.resolve(null); }
        if (req.error) { showReason(req.error); return Promise.resolve(null); }
        ui.busy = true;
        var seq = ui.lastDawSeq;
        renderCommands();
        return api.request(req.method, req.url, req.body).then(function (r) {
            ui.busy = false;
            if (r.ok) {
                if (after) after(r.body || {});
                if (label) toast(label, 'ok');
                // daw_state_update arriva di norma prima della risposta; se manca, rileggo una volta.
                if (ui.lastDawSeq === seq) refreshState();
            } else {
                toast(errorText(r), 'error');
                if (r.unknown_outcome || (r.error && r.error.code === 'REVISION_CONFLICT')) refreshState();
            }
            renderCommands();
            return r;
        });
    }

    // Comandi senza revisione (trasporto, arm/mute/solo, BPM...): non bloccano l'editing ma un pulsante
    // resta occupato finche' non arriva la risposta.
    function simple(btn, promise, onOk) {
        if (btn) { btn.disabled = true; btn.classList.add('is-busy'); }
        return promise.then(function (r) {
            if (btn) { btn.disabled = false; btn.classList.remove('is-busy'); }
            if (r.ok) { if (onOk) onOk(r.body || {}); }
            else toast(errorText(r), 'error');
            renderTransport();
            return r;
        });
    }

    function transportCmd(cmd, extra, btn) {
        var payload = { cmd: cmd };
        Object.keys(extra || {}).forEach(function (k) { payload[k] = extra[k]; });
        var fallback = {
            play_start: '/api/daw/play/start', play_stop: '/api/daw/play/stop', record_start: '/api/daw/record/start',
            record_stop: '/api/daw/record/stop', stop_all: '/api/daw/stop_all', rewind: '/api/daw/rewind',
            undo: '/api/daw/undo', redo: '/api/daw/redo', toggle_loop: '/api/daw/loop/toggle', seek_beat: '/api/daw/seek_beat'
        }[cmd];
        var body = {};
        Object.keys(extra || {}).forEach(function (k) { body[k] = extra[k]; });
        return simple(btn, api.command('transport_cmd', payload, { method: 'POST', url: fallback, body: body }));
    }

    /* ── Costruzione DOM (una volta) ─────────────────────────────────── */
    function buildRows() {
        var frag = doc.createDocumentFragment();
        for (var t = 0; t < 16; t++) {
            var row = doc.createElement('div');
            row.className = 'st-row';
            row.setAttribute('data-track', t);
            row.innerHTML =
                '<div class="st-head" data-st-head="' + t + '">' +
                '<span class="st-head-color"></span>' +
                '<button type="button" class="st-head-name" data-st-select-track="' + t + '"><span class="st-head-num">' + (t + 1) + '</span><span class="st-head-label"></span></button>' +
                '<div class="st-head-btns">' +
                '<button type="button" class="st-mini st-arm" data-st-flag="arm" data-t="' + t + '" aria-pressed="false" aria-label="Arma traccia ' + (t + 1) + '">R</button>' +
                '<button type="button" class="st-mini st-mute" data-st-flag="mute" data-t="' + t + '" aria-pressed="false" aria-label="Muto traccia ' + (t + 1) + '">M</button>' +
                '<button type="button" class="st-mini st-solo" data-st-flag="solo" data-t="' + t + '" aria-pressed="false" aria-label="Solo traccia ' + (t + 1) + '">S</button>' +
                '</div></div>' +
                '<div class="st-lane" data-st-lane="' + t + '"><div class="st-range" hidden></div></div>';
            frag.appendChild(row);
        }
        el.rows.appendChild(frag);
        el.rowEls = el.rows.querySelectorAll('.st-row');
    }

    function buildSwatches() {
        var html = '';
        model.TRACK_PALETTE.forEach(function (c, i) {
            html += '<button type="button" class="st-swatch" data-st-color="' + i + '" style="background:' + c + '" aria-label="Colore ' + (i + 1) + '" aria-pressed="false"></button>';
        });
        el.swatches.innerHTML = html;
    }

    function buildMixer() {
        if (ui.mixerBuilt) return;
        ui.mixerBuilt = true;
        var html = '';
        for (var t = 0; t < 16; t++) {
            html += '<div class="st-mix-row" data-t="' + t + '">' +
                '<button type="button" class="st-mix-name" data-st-select-track="' + t + '"><span class="st-head-num">' + (t + 1) + '</span> <span class="st-mix-label"></span></button>' +
                '<input type="range" class="st-mix-vol" min="0" max="127" value="100" data-st-vol="' + t + '" aria-label="Volume traccia ' + (t + 1) + '">' +
                '<span class="st-mix-val">100</span>' +
                '<button type="button" class="st-mini st-mute" data-st-flag="mute" data-t="' + t + '" aria-pressed="false" aria-label="Muto traccia ' + (t + 1) + '">M</button>' +
                '<button type="button" class="st-mini st-solo" data-st-flag="solo" data-t="' + t + '" aria-pressed="false" aria-label="Solo traccia ' + (t + 1) + '">S</button>' +
                '</div>';
        }
        el.mixer.innerHTML = '<p class="ms-note">Volume (CC7) delle parti; per pan e suono usa la scheda Suono.</p>' + html;
        renderMixer();
    }

    /* ── Render ──────────────────────────────────────────────────────── */
    function trackColor(d, t) {
        var idx = d.track_colors && d.track_colors[t] !== undefined ? d.track_colors[t] : t;
        return model.TRACK_PALETTE[((idx % 16) + 16) % 16];
    }
    function flag(d, key, t) { return !!(d[key] && (d[key][t] || d[key][String(t)])); }

    function renderTracks() {
        var d = daw(), s = sel();
        var width = Math.ceil(ui.totalBeats * ui.ppb);
        el.content.style.width = (HEAD_W + width) + 'px';
        for (var t = 0; t < 16; t++) {
            var row = el.rowEls[t];
            var color = trackColor(d, t);
            var head = row.firstChild;
            head.firstChild.style.background = color;
            setText(head.querySelector('.st-head-label'), model.trackLabel(d, t));
            ['arm:armed', 'mute:muted', 'solo:solo'].forEach(function (pair) {
                var p = pair.split(':');
                var b = head.querySelector('[data-st-flag="' + p[0] + '"]');
                var on = flag(d, p[1], t);
                if (b.getAttribute('aria-pressed') !== String(on)) b.setAttribute('aria-pressed', String(on));
            });
            row.classList.toggle('is-selected', s.kind !== 'none' && s.track === t);
            var lane = row.lastChild;
            lane.style.width = width + 'px';
            var clips = model.clipsOf(d, t);
            var sig = ui.ppb + '|' + color + '|' + JSON.stringify(clips);
            if (ui.rowSig[t] !== sig) {
                ui.rowSig[t] = sig;
                renderClips(lane, t, clips, color);
            }
            var selIds = s.kind === 'clip' && s.track === t ? s.clip_ids : [];
            var nodes = lane.querySelectorAll('.st-clip');
            for (var i = 0; i < nodes.length; i++) {
                var on = selIds.indexOf(Number(nodes[i].getAttribute('data-clip'))) >= 0;
                nodes[i].classList.toggle('is-selected', on);
                nodes[i].setAttribute('aria-pressed', String(on));
            }
            var rg = lane.firstChild;
            var showRange = s.kind === 'range' && s.track === t;
            rg.hidden = !showRange;
            if (showRange) {
                rg.style.left = model.beatToX(s.start_beat, ui.ppb) + 'px';
                rg.style.width = Math.max(2, model.beatToX(s.end_beat - s.start_beat, ui.ppb)) + 'px';
            }
        }
    }

    function renderClips(lane, t, clips, color) {
        var old = lane.querySelectorAll('.st-clip');
        for (var i = 0; i < old.length; i++) lane.removeChild(old[i]);
        var frag = doc.createDocumentFragment();
        model.layoutClips(clips).forEach(function (it) {
            var c = it.clip;
            var div = doc.createElement('div');
            div.className = 'st-clip';
            div.setAttribute('data-clip', c.id);
            div.setAttribute('data-t', t);
            div.setAttribute('role', 'button');
            div.setAttribute('tabindex', '0');
            div.setAttribute('aria-pressed', 'false');
            div.setAttribute('aria-label', (c.name || 'Clip ' + c.id) + ', da ' + fmtBeat(c.start_beat) + ' a ' + fmtBeat(c.end_beat));
            var h = (ROW_H - 6) / it.lanes;
            div.style.left = model.beatToX(c.start_beat, ui.ppb) + 'px';
            div.style.width = Math.max(6, model.beatToX(c.end_beat - c.start_beat, ui.ppb)) + 'px';
            div.style.top = (3 + it.lane * h) + 'px';
            div.style.height = Math.max(10, h - 2) + 'px';
            div.style.borderColor = color;
            div.style.setProperty('--st-clip-color', color);
            div.innerHTML = '<span class="st-clip-name"></span><span class="st-clip-handle" data-st-handle="1" aria-hidden="true"></span>';
            div.firstChild.textContent = c.name || ('Clip ' + c.id);
            frag.appendChild(div);
        });
        lane.appendChild(frag);
    }

    function renderPlayhead() {
        var x = model.beatToX(posBeat(), ui.ppb);
        el.playhead.style.transform = 'translateX(' + (HEAD_W + x) + 'px)';
        var sl = el.scroll.scrollLeft;
        el.rulerPlayhead.style.transform = 'translateX(' + (x - sl) + 'px)';
        if (ui.cursorBeat !== null) {
            el.cursor.hidden = false;
            el.cursor.style.transform = 'translateX(' + (HEAD_W + model.beatToX(ui.cursorBeat, ui.ppb)) + 'px)';
        } else el.cursor.hidden = true;
        var t = transport();
        if (ui.follow && t.is_playing && !ui.drag) {
            var viewW = el.scroll.clientWidth - HEAD_W;
            if (x < sl || x > sl + viewW - 24) el.scroll.scrollLeft = Math.max(0, x - 24);
        }
    }

    function renderTransport() {
        var t = transport(), d = daw();
        var b = posBeat();
        setText(el.bbt, units.beatsToBBT(b, t.beats_per_measure || d.beats_per_measure || 4).text);
        setText(el.time, units.formatTime(t.position));
        var playing = !!t.is_playing, rec = !!t.is_recording;
        el.play.classList.toggle('is-on', playing && !rec);
        el.record.classList.toggle('is-on', rec);
        setText(el.play.querySelector('.st-btn-label'), playing && !rec ? 'Pausa' : 'Play');
        var icon = el.play.querySelector('i');
        if (icon) icon.className = playing && !rec ? 'fas fa-pause' : 'fas fa-play';
        el.play.setAttribute('aria-label', playing && !rec ? 'Pausa' : 'Play');
        el.play.disabled = rec;                                   // in Record c'e' solo Stop (piano: niente pausa)
        el.record.disabled = playing && !rec;
        el.record.setAttribute('aria-label', rec ? 'Ferma la registrazione' : 'Record');
        el.undo.disabled = playing || rec || !d.can_undo || ui.busy;
        el.redo.disabled = playing || rec || !d.can_redo || ui.busy;
        el.bpm.disabled = playing || rec;
        el.meter.disabled = playing || rec;
        el.click.setAttribute('aria-pressed', String(!!d.metronome_enabled));
        el.loop.setAttribute('aria-pressed', String(!!d.loop_enabled));
        if (doc.activeElement !== el.bpm && d.bpm && String(Math.round(d.bpm)) !== el.bpm.value) el.bpm.value = String(Math.round(d.bpm));
        if (d.beats_per_measure && el.meter.value !== String(d.beats_per_measure)) el.meter.value = String(d.beats_per_measure);
        renderPlayhead();
    }

    function renderCommands() {
        var states = model.commandStates({ daw: daw(), sel: sel(), transport: transport(), busy: ui.busy, cursorBeat: cursorOrPlayhead() });
        var btns = el.root.querySelectorAll('[data-st-op]');
        for (var i = 0; i < btns.length; i++) {
            var op = btns[i].getAttribute('data-st-op');
            var st = states[op];
            if (!st) {
                if (op === 'clear_all') st = { enabled: !ui.busy && !transport().is_recording, reason: transport().is_recording ? 'Non disponibile durante il Record' : 'Attendi' };
                else st = { enabled: !ui.busy, reason: 'Attendi la risposta del comando precedente' };
            }
            btns[i].setAttribute('aria-disabled', String(!st.enabled));
            btns[i].title = st.enabled ? '' : st.reason;
            btns[i].setAttribute('data-reason', st.reason);
        }
        var s = sel();
        setText(el.selText, model.describeSelection(daw(), s, daw().beats_per_measure || 4, units.beatsToBBT));
        setText(el.cursorText, 'Cursore ' + fmtBeat(cursorOrPlayhead()) + (ui.cursorBeat === null ? ' (posizione)' : ''));
        el.undo.disabled = isActive() || !daw().can_undo || ui.busy;
        el.redo.disabled = isActive() || !daw().can_redo || ui.busy;
    }

    function renderInspector() {
        var d = daw(), s = sel();
        var clips = model.selectedClips(d, s);
        var one = clips.length === 1 ? clips[0] : null;
        el.clipEmpty.hidden = !!one;
        el.clipForm.hidden = !one;
        if (clips.length > 1) setText(el.clipEmpty, clips.length + ' clip selezionati: puoi unirli con Unisci.');
        else setText(el.clipEmpty, 'Tocca un clip per selezionarlo. Trascina un clip selezionato per spostarlo, la maniglia a destra per cambiarne la fine.');
        if (one) {
            setText(el.clipName, one.name || ('Clip ' + one.id));
            setText(el.clipId, String(one.id));
            setText(el.clipSpan, fmtBeat(one.start_beat) + ' – ' + fmtBeat(one.end_beat));
            if (doc.activeElement !== el.clipStart) el.clipStart.value = String(one.start_beat);
            if (doc.activeElement !== el.clipEnd) el.clipEnd.value = String(one.end_beat);
            el.clipStart.step = String(ui.snap > 0 ? ui.snap : 0.01);
            el.clipEnd.step = el.clipStart.step;
        }
        var hasTrack = s.kind !== 'none';
        el.trackEmpty.hidden = hasTrack;
        el.trackForm.hidden = !hasTrack;
        el.soundEmpty.hidden = hasTrack;
        if (hasTrack) {
            if (doc.activeElement !== el.trackName) el.trackName.value = (d.track_names && d.track_names[s.track]) || '';
            var ci = d.track_colors && d.track_colors[s.track] !== undefined ? d.track_colors[s.track] : s.track;
            var sw = el.swatches.children;
            for (var i = 0; i < sw.length; i++) sw[i].setAttribute('aria-pressed', String(i === ci));
            var opts = '';
            for (var t = 0; t < 16; t++) {
                if (t === s.track) continue;
                opts += '<option value="' + t + '">' + (t + 1) + ' · ' + escapeHtml(model.trackLabel(d, t)) + (flag(d, 'has_data', t) ? ' (con dati)' : '') + '</option>';
            }
            var keep = el.dupDest.value;
            if (el.dupDest.getAttribute('data-sig') !== opts) {
                el.dupDest.innerHTML = opts;
                el.dupDest.setAttribute('data-sig', opts);
                if (keep && Number(keep) !== s.track) el.dupDest.value = keep;
            }
        }
        if (ui.tab === 'sound') mountSound();
        if (ui.tab === 'mixer') renderMixer();
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
    }

    function mountSound() {
        var s = sel();
        if (s.kind === 'none' || !MS.partPanel) return;
        if (!ui.partPanel) {
            try { ui.partPanel = MS.partPanel.mount(el.soundHost, { partId: s.track }); } catch (e) { ui.partPanel = null; return; }
            ui.partPanelTrack = s.track;
        } else if (ui.partPanelTrack !== s.track) {
            ui.partPanel.setPart(s.track);
            ui.partPanelTrack = s.track;
        }
    }

    function partVolume(t) {
        var p = state.getPart ? state.getPart(t) : null;
        var v = p && p.cc ? p.cc[7] : undefined;
        return v === undefined || v === null ? 100 : Number(v);
    }

    function renderMixer() {
        if (!ui.mixerBuilt) return;
        var d = daw();
        var rows = el.mixer.querySelectorAll('.st-mix-row');
        for (var t = 0; t < rows.length; t++) {
            var r = rows[t];
            setText(r.querySelector('.st-mix-label'), model.trackLabel(d, t));
            var vol = r.querySelector('.st-mix-vol');
            if (doc.activeElement !== vol && !ui.ccTimers[t]) {
                var v = String(partVolume(t));
                if (vol.value !== v) vol.value = v;
                setText(r.querySelector('.st-mix-val'), v);
            }
            ['mute:muted', 'solo:solo'].forEach(function (pair) {
                var p = pair.split(':');
                r.querySelector('[data-st-flag="' + p[0] + '"]').setAttribute('aria-pressed', String(flag(d, p[1], t)));
            });
        }
    }

    function renderAll() {
        if (!el.root) return;
        ui.totalBeats = model.timelineBeats(daw(), ui.cursorBeat || 0);
        renderTracks();
        renderTransport();
        renderCommands();
        renderInspector();
        scheduleCanvas();
    }

    /* ── Canvas: righello e densita' (solo la parte visibile) ────────── */
    function scheduleCanvas() {
        if (ui.rafPending || !visible()) return;
        ui.rafPending = true;
        root.requestAnimationFrame(function () { ui.rafPending = false; drawCanvas(); });
    }

    function drawCanvas() {
        var d = daw();
        var sl = el.scroll.scrollLeft;
        var w = Math.max(1, el.scroll.clientWidth - HEAD_W);
        var dpr = Math.min(2, root.devicePixelRatio || 1);
        var from = model.xToBeat(sl, ui.ppb), to = model.xToBeat(sl + w, ui.ppb);
        // Righello
        var rc = el.rulerCanvas;
        if (rc.width !== Math.round(w * dpr)) { rc.width = Math.round(w * dpr); rc.style.width = w + 'px'; }
        rc.height = Math.round(32 * dpr);
        var g = rc.getContext('2d');
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, w, 32);
        if (d.loop_end > d.loop_start) {
            var lx = model.beatToX(d.loop_start, ui.ppb) - sl, lw = model.beatToX(d.loop_end - d.loop_start, ui.ppb);
            g.fillStyle = d.loop_enabled ? 'rgba(0,212,255,0.28)' : 'rgba(154,156,176,0.18)';
            g.fillRect(lx, 0, lw, 8);
        }
        g.font = '11px system-ui, sans-serif';
        model.rulerTicks(from, to + 1, ui.ppb, d.beats_per_measure || 4).forEach(function (tk) {
            var x = Math.round(model.beatToX(tk.beat, ui.ppb) - sl) + 0.5;
            g.fillStyle = tk.major ? '#9a9cb0' : '#3a3a52';
            g.fillRect(x, tk.major ? 10 : 22, 1, tk.major ? 22 : 10);
            if (tk.label) { g.fillStyle = '#e6e6ee'; g.fillText(tk.label, x + 3, 21); }
        });
        // Densita' (eventi, inclusi quelli liberi fuori dai clip)
        var dc = el.density;
        var h = ROW_H * 16;
        dc.style.left = (HEAD_W + sl) + 'px';
        if (dc.width !== Math.round(w * dpr) || dc.height !== Math.round(h * dpr)) {
            dc.width = Math.round(w * dpr); dc.height = Math.round(h * dpr);
            dc.style.width = w + 'px'; dc.style.height = h + 'px';
        }
        var c = dc.getContext('2d');
        c.setTransform(dpr, 0, 0, dpr, 0, 0);
        c.clearRect(0, 0, w, h);
        var dm = ui.density;
        if (dm && dm.tracks) {
            var slot = dm.slot_beats || 0.125;
            var first = Math.max(0, Math.floor(from / slot)), last = Math.ceil(to / slot);
            for (var t = 0; t < 16; t++) {
                var arr = dm.tracks[t] || dm.tracks[String(t)];
                if (!arr) continue;
                c.fillStyle = trackColor(d, t);
                var y0 = t * ROW_H;
                for (var i = first; i < Math.min(last, arr.length); i++) {
                    var n = arr[i];
                    if (!n) continue;
                    var bh = Math.min(ROW_H - 12, 4 + n * 4);
                    c.globalAlpha = 0.55;
                    c.fillRect(model.beatToX(i * slot, ui.ppb) - sl, y0 + ROW_H - 4 - bh, Math.max(1, slot * ui.ppb - 0.5), bh);
                }
            }
            c.globalAlpha = 1;
        }
        renderPlayhead();
    }

    function scheduleDensity() {
        if (!visible()) return;
        var rev = daw().revision;
        if (ui.densityRev === rev && ui.density && !transport().is_recording) return;
        clearTimeout(ui.densityTimer);
        ui.densityTimer = setTimeout(loadDensity, DENSITY_DEBOUNCE);
    }

    function loadDensity() {
        ui.densityTimer = null;
        if (ui.densityInFlight || !visible()) return;
        ui.densityInFlight = true;
        api.request('GET', '/api/daw/full_density_map').then(function (r) {
            ui.densityInFlight = false;
            if (r.ok && r.body && r.body.map) {
                ui.density = r.body.map;
                ui.densityRev = r.body.revision;
                scheduleCanvas();
                if (ui.densityRev !== daw().revision) scheduleDensity();
            }
        });
    }

    /* ── Selezione e puntatore sulla timeline ────────────────────────── */
    function laneBeat(lane, clientX) {
        var rect = lane.getBoundingClientRect();
        return model.xToBeat(clientX - rect.left, ui.ppb);
    }

    function selectTrack(t) {
        var s = sel();
        if (s.kind !== 'none' && s.track === t && s.kind !== 'track') return;
        state.setSelection({ kind: 'track', track: t });
    }

    function onPointerDown(e) {
        if (e.button !== undefined && e.button !== 0) return;
        var clipEl = e.target.closest('.st-clip');
        var lane = e.target.closest('.st-lane');
        if (!lane) return;
        var t = Number(lane.getAttribute('data-st-lane'));
        var s = sel();
        var d0 = { x: e.clientX, y: e.clientY, t: t, lane: lane, pointerId: e.pointerId, moved: false, startBeat: laneBeat(lane, e.clientX) };
        if (clipEl) {
            var id = Number(clipEl.getAttribute('data-clip'));
            d0.clipEl = clipEl;
            d0.clipId = id;
            var selected = s.kind === 'clip' && s.track === t && s.clip_ids.length === 1 && s.clip_ids[0] === id;
            if (selected && ui.tool === 'select' && !isActive()) {
                d0.mode = e.target.hasAttribute('data-st-handle') ? 'resize' : 'move';
                d0.clip = model.findClip(daw(), t, id);
                try { clipEl.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
                e.preventDefault();
            } else d0.mode = 'tap-clip';
        } else {
            d0.mode = ui.tool === 'select' ? 'range' : 'tap-lane';
            if (d0.mode === 'range') { try { lane.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ } }
        }
        ui.drag = d0;
    }

    function onPointerMove(e) {
        var d = ui.drag;
        if (!d || e.pointerId !== d.pointerId) return;
        var dx = e.clientX - d.x;
        if (!d.moved && Math.abs(dx) < DRAG_PX) return;
        if (d.mode === 'tap-clip' || d.mode === 'tap-lane') return;
        d.moved = true;
        var delta = dx / ui.ppb;
        if (d.mode === 'move' && d.clip) {
            d.target = model.dragMove(d.clip, delta, ui.snap);
            d.clipEl.style.transform = 'translateX(' + model.beatToX(d.target - d.clip.start_beat, ui.ppb) + 'px)';
            d.clipEl.classList.add('is-dragging');
            showReason('Sposta a ' + fmtBeat(d.target));
        } else if (d.mode === 'resize' && d.clip) {
            d.target = model.dragResize(d.clip, delta, ui.snap);
            d.clipEl.style.width = Math.max(6, model.beatToX(d.target - d.clip.start_beat, ui.ppb)) + 'px';
            d.clipEl.classList.add('is-dragging');
            showReason('Fine a ' + fmtBeat(d.target));
        } else if (d.mode === 'range') {
            var r = model.normalizeRange(model.snapBeat(d.startBeat, ui.snap), model.snapBeat(laneBeat(d.lane, e.clientX), ui.snap));
            d.range = r;
            var rg = d.lane.firstChild;
            rg.hidden = false;
            rg.style.left = model.beatToX(r.start_beat, ui.ppb) + 'px';
            rg.style.width = Math.max(2, model.beatToX(r.end_beat - r.start_beat, ui.ppb)) + 'px';
        }
        e.preventDefault();
    }

    function onPointerUp(e) {
        var d = ui.drag;
        if (!d || e.pointerId !== d.pointerId) return;
        ui.drag = null;
        if (e.type === 'pointercancel') { renderAll(); return; }
        var s = sel();
        if (!d.moved) {
            if (d.clipId !== undefined) {
                state.setSelection(model.toggleClipSelection(s, d.t, d.clipId, ui.multi || e.shiftKey || e.ctrlKey || e.metaKey));
            } else {
                ui.cursorBeat = model.snapBeat(d.startBeat, ui.snap);
                state.setSelection({ kind: 'track', track: d.t });
            }
            renderAll();
            return;
        }
        if (d.mode === 'range' && d.range) {
            if (d.range.end_beat > d.range.start_beat) {
                state.setSelection({ kind: 'range', track: d.t, start_beat: d.range.start_beat, end_beat: d.range.end_beat });
            }
            renderAll();
            return;
        }
        if ((d.mode === 'move' || d.mode === 'resize') && d.clip && d.target !== undefined) {
            var unchanged = d.mode === 'move' ? d.target === d.clip.start_beat : d.target === d.clip.end_beat;
            if (unchanged) { renderAll(); return; }
            showReason('');
            var req = model.buildRequest(d.mode, daw(), sel(), { target: d.target });
            edit(req, d.mode === 'move' ? 'Clip spostato' : 'Clip ridimensionato').then(function (r) {
                if (r && r.ok) return;
                ui.rowSig[d.t] = null;          // annulla l'anteprima locale
                renderAll();
                if (r && r.error && r.error.code === 'CLIP_RESIZE_WOULD_EXCLUDE' && r.error.details) {
                    showReason('Fine minima: ' + fmtBeat(r.error.details.min_end_beat));
                }
            });
        }
    }

    function onRulerTap(e) {
        var rect = el.ruler.getBoundingClientRect();
        var beat = model.snapBeat(model.xToBeat(e.clientX - rect.left + el.scroll.scrollLeft, ui.ppb), ui.snap);
        if (transport().is_recording) { showReason('Spostare la posizione durante il Record non e\' consentito'); return; }
        ui.cursorBeat = beat;
        transportCmd('seek_beat', { beat: beat });
        renderAll();
    }

    /* ── Operazioni contestuali ──────────────────────────────────────── */
    function runOp(op, btn) {
        if (btn && btn.getAttribute('aria-disabled') === 'true') { showReason(btn.getAttribute('data-reason')); return; }
        var d = daw(), s = sel();
        var cursor = cursorOrPlayhead();
        var req, label, after = null;
        switch (op) {
            case 'copy':
                req = model.buildRequest('copy', d, s);
                label = 'Copiato negli appunti';
                after = function (b) { label = (b.copied || 0) + ' eventi copiati'; };
                break;
            case 'cut': req = model.buildRequest('cut', d, s); label = 'Tagliato'; break;
            case 'paste': req = model.buildRequest('paste', d, s, { cursorBeat: cursor }); label = 'Incollato a ' + fmtBeat(cursor); break;
            case 'duplicate': req = model.buildRequest('duplicate', d, s); label = 'Clip duplicato'; break;
            case 'split': req = model.buildRequest('split', d, s, { cursorBeat: cursor }); label = 'Clip diviso a ' + fmtBeat(cursor); break;
            case 'merge': req = model.buildRequest('merge', d, s); label = 'Clip uniti'; break;
            case 'delete': req = model.buildRequest('delete', d, s); label = 'Clip eliminato'; break;
            case 'transpose': {
                var st = Number(btn.getAttribute('data-semitones'));
                req = model.buildRequest('transpose', d, s, { semitones: st });
                label = 'Trasposto ' + (st > 0 ? '+' : '') + st;
                break;
            }
            case 'create': req = model.buildRequest('create', d, s); label = 'Clip creato'; break;
            case 'clear_range':
                if (!root.confirm('Cancellare gli eventi dell\'intervallo selezionato?')) return;
                req = model.buildRequest('clear_range', d, s); label = 'Intervallo cancellato';
                break;
            case 'loop_from_sel': req = model.buildRequest('loop_from_sel', d, s); label = 'Punti di loop impostati'; break;
            case 'move':
            case 'resize': {
                var v = Number(op === 'move' ? el.clipStart.value : el.clipEnd.value);
                if (!isFinite(v) || v < 0) { showReason('Valore non valido'); return; }
                req = model.buildRequest(op, d, s, { target: v });
                label = op === 'move' ? 'Clip spostato' : 'Clip ridimensionato';
                break;
            }
            case 'rename': {
                var name = el.trackName.value.trim();
                if (!name) { showReason('Scrivi un nome'); return; }
                req = { method: 'POST', url: '/api/daw/track/' + s.track + '/rename', body: { name: name } };
                label = 'Traccia rinominata';
                break;
            }
            case 'duplicate_track': return duplicateTrack(false);
            case 'clear_track':
                if (!root.confirm('Svuotare la traccia ' + (s.track + 1) + ' (' + model.trackLabel(d, s.track) + ')? Si puo\' annullare con Annulla.')) return;
                req = model.buildRequest('clear_track', d, s); label = 'Traccia svuotata';
                break;
            case 'clear_all':
                if (!root.confirm('Svuotare TUTTE le tracce? Si puo\' annullare con Annulla.')) return;
                req = { method: 'POST', url: '/api/daw/clear_all', body: {} }; label = 'Tutte le tracce svuotate';
                break;
            default: return;
        }
        edit(req, null, function (b) {
            if (after) after(b);
            var next = model.selectionAfter(op, s, b);
            if (next) state.setSelection(next);
            toast(label, 'ok');
        }).then(function (r) {
            if (r && !r.ok && r.error && r.error.code === 'CLIP_RESIZE_WOULD_EXCLUDE' && r.error.details) {
                showReason('Fine minima: ' + fmtBeat(r.error.details.min_end_beat));
            }
        });
    }

    function duplicateTrack(confirmOverwrite) {
        var d = daw(), s = sel();
        var dest = Number(el.dupDest.value);
        if (!(dest >= 0 && dest <= 15)) { showReason('Scegli la traccia di destinazione'); return; }
        var req = model.buildRequest('duplicate_track', d, s, { dest: dest, includeConfig: el.dupConfig.checked, confirmOverwrite: confirmOverwrite });
        edit(req, null, function (b) {
            var msg = 'Traccia duplicata su ' + (dest + 1);
            if (el.dupConfig.checked && b.config_copied === false) msg += ' (configurazione non copiata)';
            toast(msg, b.config_copied === false && el.dupConfig.checked ? 'error' : 'ok');
        }).then(function (r) {
            if (r && !r.ok && r.error && r.error.code === 'DEST_NOT_EMPTY' && !confirmOverwrite) {
                if (root.confirm('La traccia ' + (dest + 1) + ' contiene gia\' dati: sostituirli?')) duplicateTrack(true);
            }
        });
    }

    function onFlag(btn) {
        var t = Number(btn.getAttribute('data-t'));
        var f = btn.getAttribute('data-st-flag');
        var on = btn.getAttribute('aria-pressed') !== 'true';
        var key = { arm: 'armed', mute: 'muted', solo: 'solo' }[f];
        var body = {};
        body[key] = on;
        if (f === 'arm' && transport().is_recording) { showReason('Cambia l\'arm prima del Record'); }
        simple(btn, api.request('POST', '/api/daw/track/' + t + '/' + f, body), function () { refreshIfNoUpdate(); });
    }

    function refreshIfNoUpdate() {
        var seq = ui.lastDawSeq;
        setTimeout(function () { if (ui.lastDawSeq === seq) refreshState(); }, 250);
    }

    function sendVolume(t, val, final) {
        // Coalescenza: al massimo un invio ogni 40 ms per parte + il valore finale (contracts 9.1).
        var key = t;
        ui.ccPending = ui.ccPending || {};
        ui.ccPending[key] = val;
        function fire() {
            ui.ccTimers[key] = null;
            if (ui.ccPending[key] === undefined) return;
            var v = ui.ccPending[key];
            delete ui.ccPending[key];
            api.command('cc_update', { chan: t, cc: 7, val: v }, null);
            if (state.updatePartCC) state.updatePartCC(t, 7, v);
        }
        if (final) { clearTimeout(ui.ccTimers[key]); fire(); return; }
        if (!ui.ccTimers[key]) { fire(); ui.ccTimers[key] = setTimeout(fire, 40); }
    }

    function onTransport(cmd, btn) {
        var t = transport();
        switch (cmd) {
            case 'rewind':
                if (t.is_recording) { showReason('Ferma il Record prima di tornare all\'inizio'); return; }
                ui.cursorBeat = null;
                return transportCmd('rewind', null, btn);
            case 'play': return transportCmd(t.is_playing ? 'play_stop' : 'play_start', null, btn);
            case 'record':
                if (t.is_recording) return transportCmd('record_stop', null, btn);
                return transportCmd('record_start', { return_to_start: ui.returnToStart }, btn);
            case 'stop': return transportCmd('stop_all', null, btn);
            case 'undo': return edit({ method: 'POST', url: '/api/daw/undo', body: {} }, 'Annullato');
            case 'redo': return edit({ method: 'POST', url: '/api/daw/redo', body: {} }, 'Ripetuto');
            case 'metronome': return simple(btn, api.request('POST', '/api/daw/metronome/toggle', {}), refreshIfNoUpdate);
            case 'loop': return transportCmd('toggle_loop', null, btn);
            case 'multi':
                ui.multi = !ui.multi;
                btn.setAttribute('aria-pressed', String(ui.multi));
                return;
            case 'follow':
                ui.follow = !ui.follow;
                state.setPref('studio.follow', ui.follow);
                btn.setAttribute('aria-pressed', String(ui.follow));
                return;
            case 'zoom-in':
            case 'zoom-out': return setZoom(model.zoomStep(ui.ppb, cmd === 'zoom-in' ? 1 : -1));
            case 'legacy': return setLegacy(true);
            case 'editor': return openEditor();
        }
    }

    // Editor note/eventi (midi_editor.js) sulla traccia/clip della selezione; senza selezione la traccia 1.
    function openEditor() {
        if (!MS.editor) return;
        var s = sel();
        var track = s.kind === 'none' ? 0 : s.track;
        var clipId = s.kind === 'clip' && s.clip_ids && s.clip_ids.length ? s.clip_ids[0] : null;
        var c = clipId !== null ? model.findClip(daw(), track, clipId) : null;
        MS.editor.open({ track: track, clipId: clipId, startBeat: c ? c.start_beat : null });
    }

    function setZoom(ppb) {
        if (ppb === ui.ppb) return;
        // Mantiene fermo il beat al centro della parte visibile.
        var w = el.scroll.clientWidth - HEAD_W;
        var centre = model.xToBeat(el.scroll.scrollLeft + w / 2, ui.ppb);
        ui.ppb = ppb;
        state.setPref('studio.zoom', ppb);
        renderAll();
        el.scroll.scrollLeft = Math.max(0, model.beatToX(centre, ppb) - w / 2);
        scheduleCanvas();
    }

    function setTool(tool) {
        ui.tool = tool;
        el.root.setAttribute('data-tool', tool);
        var b = el.root.querySelectorAll('[data-st-tool]');
        for (var i = 0; i < b.length; i++) b[i].setAttribute('aria-pressed', String(b[i].getAttribute('data-st-tool') === tool));
    }

    function setTab(tab) {
        ui.tab = tab;
        var tabs = el.root.querySelectorAll('[data-st-tab]');
        for (var i = 0; i < tabs.length; i++) tabs[i].setAttribute('aria-selected', String(tabs[i].getAttribute('data-st-tab') === tab));
        var panes = el.root.querySelectorAll('[data-st-pane]');
        for (var j = 0; j < panes.length; j++) panes[j].hidden = panes[j].getAttribute('data-st-pane') !== tab;
        if (tab === 'mixer') buildMixer();
        renderInspector();
    }

    // Vista classica: il markup legacy (piano roll, editor eventi) resta disponibile finche' ui-editor non lo
    // sostituisce. Una sola delle due e' visibile; il loop di disegno legacy si ferma quando e' nascosta.
    function setLegacy(on) {
        ui.legacy = !!on;
        state.setPref('studio.legacy', ui.legacy);
        el.root.hidden = ui.legacy;
        var box = $('studio-legacy');
        if (box) box.hidden = !ui.legacy;
        el.backBar.hidden = !ui.legacy;
        if (ui.legacy) root.requestAnimationFrame(function () { root.dispatchEvent(new root.Event('resize')); });
        else { renderAll(); scheduleDensity(); }
    }

    /* ── Tastiera (solo vista nuova visibile) ────────────────────────── */
    // Ascoltato su body in bubbling: arriva dopo i listener degli elementi (Invio sui clip, campi) e prima
    // di quelli legacy su document, che con la vista nuova visibile non devono agire sulla selezione
    // nascosta della vista classica.
    function onKeydown(e) {
        if (!visible()) return;
        if (doc.body.classList.contains('ms-panel-open')) return;
        if (e.target.closest && e.target.closest('.ms-globalbar, .ms-panel')) return;
        e.stopPropagation();
        var tag = e.target && e.target.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        var k = e.key, ctrl = e.ctrlKey || e.metaKey;
        var map = null;
        if (ctrl && (k === 'c' || k === 'C')) map = 'copy';
        else if (ctrl && (k === 'x' || k === 'X')) map = 'cut';
        else if (ctrl && (k === 'v' || k === 'V')) map = 'paste';
        else if (ctrl && (k === 'd' || k === 'D')) map = 'duplicate';
        else if (k === 'Delete' || k === 'Backspace') map = 'delete';
        if (map) {
            e.preventDefault();
            runOp(map, el.root.querySelector('[data-st-op="' + map + '"]'));
            return;
        }
        if (ctrl && (k === 'z' || k === 'Z')) { e.preventDefault(); onTransport(e.shiftKey ? 'redo' : 'undo'); return; }
        if (ctrl && (k === 'y' || k === 'Y')) { e.preventDefault(); onTransport('redo'); return; }
        if (k === ' ' && !(e.target.closest && e.target.closest('button'))) { e.preventDefault(); onTransport(transport().is_recording ? 'stop' : 'play'); return; }
        if (k === 'Escape') { state.clearSelection(); ui.cursorBeat = null; renderAll(); }
    }

    /* ── Cablaggio ───────────────────────────────────────────────────── */
    function wire() {
        el.root.addEventListener('click', function (e) {
            var b = e.target.closest('button');
            if (!b || !el.root.contains(b)) return;
            if (b.hasAttribute('data-st-op')) return runOp(b.getAttribute('data-st-op'), b);
            if (b.hasAttribute('data-st-cmd')) return onTransport(b.getAttribute('data-st-cmd'), b);
            if (b.hasAttribute('data-st-flag')) return onFlag(b);
            if (b.hasAttribute('data-st-tool')) return setTool(b.getAttribute('data-st-tool'));
            if (b.hasAttribute('data-st-tab')) return setTab(b.getAttribute('data-st-tab'));
            if (b.hasAttribute('data-st-select-track')) {
                var t = Number(b.getAttribute('data-st-select-track'));
                state.setSelection({ kind: 'track', track: t });
                return;
            }
            if (b.hasAttribute('data-st-color')) {
                var s = sel();
                if (s.kind === 'none') return;
                edit({ method: 'POST', url: '/api/daw/track/' + s.track + '/color', body: { color_index: Number(b.getAttribute('data-st-color')) } }, 'Colore impostato');
            }
        });
        el.rows.addEventListener('pointerdown', onPointerDown);
        el.rows.addEventListener('pointermove', onPointerMove);
        el.rows.addEventListener('pointerup', onPointerUp);
        el.rows.addEventListener('pointercancel', onPointerUp);
        el.rows.addEventListener('keydown', function (e) {
            var c = e.target.closest && e.target.closest('.st-clip');
            if (c && (e.key === 'Enter' || e.key === ' ')) {
                e.preventDefault();
                e.stopPropagation();
                state.setSelection(model.toggleClipSelection(sel(), Number(c.getAttribute('data-t')), Number(c.getAttribute('data-clip')), ui.multi || e.shiftKey));
            }
        });
        el.ruler.addEventListener('click', onRulerTap);
        el.scroll.addEventListener('scroll', scheduleCanvas, { passive: true });
        root.addEventListener('resize', scheduleCanvas);
        el.bpm.addEventListener('change', function () {
            var v = Number(el.bpm.value);
            if (!(v >= 20 && v <= 300)) { showReason('BPM tra 20 e 300'); renderTransport(); return; }
            simple(null, api.request('POST', '/api/daw/bpm', { bpm: v }), refreshIfNoUpdate);
        });
        el.meter.addEventListener('change', function () {
            simple(null, api.request('POST', '/api/daw/time_signature', { beats_per_measure: Number(el.meter.value) }), refreshIfNoUpdate);
        });
        el.snap.addEventListener('change', function () {
            ui.snap = Number(el.snap.value);
            state.setPref('studio.snap', ui.snap);
            renderInspector();
        });
        el.returnStart.addEventListener('change', function () {
            ui.returnToStart = !!el.returnStart.checked;
            state.setPref('studio.returnToStart', ui.returnToStart);
        });
        el.mixer.addEventListener('input', function (e) {
            if (!e.target.hasAttribute('data-st-vol')) return;
            var t = Number(e.target.getAttribute('data-st-vol'));
            setText(e.target.parentNode.querySelector('.st-mix-val'), e.target.value);
            sendVolume(t, Number(e.target.value), false);
        });
        el.mixer.addEventListener('change', function (e) {
            if (!e.target.hasAttribute('data-st-vol')) return;
            sendVolume(Number(e.target.getAttribute('data-st-vol')), Number(e.target.value), true);
        });
        el.backBar.addEventListener('click', function (e) { if (e.target.closest('button')) setLegacy(false); });
        doc.body.addEventListener('keydown', onKeydown);
        el.trackName.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); runOp('rename'); }
        });

        state.subscribe('daw', function () {
            ui.lastDawSeq++;
            if (!visible()) return;
            renderAll();
            scheduleDensity();
        });
        state.subscribe('transport', function (_v, _d, meta) {
            if (!visible()) return;
            renderTransport();
            if (!(meta && meta.tick)) renderCommands();
        });
        state.subscribe('selection', function () { if (visible()) { renderTracks(); renderCommands(); renderInspector(); } });
        state.subscribe('parts', function () { if (visible() && ui.tab === 'mixer') renderMixer(); });
        doc.addEventListener('matsynth:viewchange', function (e) {
            if (e.detail && e.detail.view === 'studio' && !ui.legacy) {
                root.requestAnimationFrame(function () { renderAll(); scheduleDensity(); });
            }
        });
    }

    function init() {
        el.root = $('st-root');
        if (!el.root) return;
        ['rows', 'scroll', 'content', 'playhead', 'cursor', 'density', 'ruler', 'bpm', 'meter', 'snap', 'play',
            'record', 'undo', 'redo', 'click', 'loop', 'bbt', 'time', 'reason', 'swatches', 'mixer'].forEach(function (k) { el[k] = $('st-' + k); });
        el.rulerCanvas = $('st-ruler-canvas');
        el.rulerPlayhead = $('st-ruler-playhead');
        el.selText = $('st-sel-text');
        el.cursorText = $('st-cursor-text');
        el.returnStart = $('st-return-start');
        el.clipEmpty = $('st-clip-empty'); el.clipForm = $('st-clip-form');
        el.clipName = $('st-clip-name'); el.clipId = $('st-clip-id'); el.clipSpan = $('st-clip-span');
        el.clipStart = $('st-clip-start'); el.clipEnd = $('st-clip-end');
        el.trackEmpty = $('st-track-empty'); el.trackForm = $('st-track-form'); el.trackName = $('st-track-name');
        el.dupDest = $('st-dup-dest'); el.dupConfig = $('st-dup-config');
        el.soundEmpty = $('st-sound-empty'); el.soundHost = $('st-sound-host');
        // Barra "Torna a Studio" sopra la vista classica (creata qui: il markup legacy non va modificato).
        el.backBar = doc.createElement('div');
        el.backBar.className = 'st-backbar';
        el.backBar.innerHTML = '<button type="button" class="ms-btn ms-btn-secondary"><i class="fas fa-arrow-left" aria-hidden="true"></i> Torna a Studio</button>' +
            '<span class="ms-note">Vista classica: piano roll ed editor eventi. Le operazioni sui clip sono nella vista Studio.</span>';
        var box = $('studio-legacy');
        if (box) box.parentNode.insertBefore(el.backBar, box);
        el.root.style.setProperty('--st-head', HEAD_W + 'px');
        el.root.style.setProperty('--st-row', ROW_H + 'px');
        // Nome accessibile stabile: le etichette si nascondono sugli schermi stretti e il title dei
        // pulsanti disabilitati diventa la motivazione, quindi il nome va fissato in aria-label.
        var named = el.root.querySelectorAll('.st-btn');
        for (var n = 0; n < named.length; n++) {
            if (named[n].hasAttribute('aria-label')) continue;
            var lab = named[n].querySelector('.st-btn-label');
            var txt = lab ? lab.textContent.trim() : named[n].getAttribute('title');
            if (txt) named[n].setAttribute('aria-label', txt);
        }
        units.fillSnapSelect(el.snap, ui.snap);
        el.returnStart.checked = ui.returnToStart;
        el.root.querySelector('#st-follow').setAttribute('aria-pressed', String(ui.follow));
        buildRows();
        buildSwatches();
        setTool('select');
        wire();
        setLegacy(ui.legacy);
    }

    MS.studio = {
        // Diagnostica per i test in browser.
        debug: function () { return { legacy: ui.legacy, ppb: ui.ppb, busy: ui.busy, cursorBeat: ui.cursorBeat, densityRev: ui.densityRev }; },
        setLegacy: setLegacy
    };

    init();
})(window);
