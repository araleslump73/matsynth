/* MatSynth - midi_editor.js (fase ui-editor)
 * Editor note (piano roll su canvas) ed editor eventi (lista virtualizzata) nella vista #view-editor,
 * aperto da Studio (MS.editor.open). Logica pura in editor_model.js.
 *
 * Regole (contracts.md 6.2-6.8, 8.2, D28-D33):
 *  - Una modifica alla volta; ogni richiesta porta expected_revision. Un gesto (trascinamento, tocco con
 *    Disegna, cancellazione, operazione della barra) = UNA richiesta = un livello di undo = revisione +1.
 *    409 REVISION_CONFLICT = rilettura, nessuna ripetizione automatica.
 *  - La selezione delle note e' locale all'editor (indici della revisione letta). Dopo una modifica propria
 *    si riseleziona per contenuto (nota, inizio, fine) se la revisione e' quella attesa, altrimenti si svuota.
 *  - Leggero per il Pi/tablet: canvas grandi quanto la parte visibile, si disegnano solo le note visibili
 *    (ricerca binaria); dati riletti solo al cambio di revisione (debounce) e solo con l'editor visibile;
 *    nessun timer periodico. Durante il Record l'editor e' in sola lettura. */
(function (root) {
    'use strict';
    var MS = root.MatSynth = root.MatSynth || {};
    var doc = root.document;
    var api = MS.api, state = MS.state, units = MS.units, M = MS.editorModel, SM = MS.studioModel, shell = MS.shell;
    if (!api || !state || !units || !M) return;

    var DRAG_PX = 6;              // soglia tocco -> trascinamento
    var EDGE_MOUSE = 8, EDGE_TOUCH = 18;
    var TOUCH_ROW = 28;           // altezza minima toccabile di una nota al tocco (px)
    var EV_ROW = 44;              // altezza riga lista eventi (px), uguale al CSS
    var RELOAD_DEBOUNCE = 120;
    var LEN_OPTIONS = [
        { value: 4, label: '1/1' }, { value: 2, label: '1/2' }, { value: 1, label: '1/4' },
        { value: 0.5, label: '1/8' }, { value: 0.25, label: '1/16' }, { value: 0.125, label: '1/32' }
    ];

    var el = {};
    var ui = {
        track: clampTrack(state.getPref('editor.track', 0)),
        clipId: null,
        tab: 'notes',
        tool: 'select',
        multi: false,
        audition: state.getPref('editor.audition', true) !== false,
        ppb: state.getPref('editor.zoomX', M.DEFAULT_X),
        rowH: state.getPref('editor.zoomY', M.DEFAULT_Y),
        snap: state.getPref('editor.snap', 0.25),
        len: state.getPref('editor.len', 1),
        vel: 100,
        notes: [], maxLen: 0, clips: [], rev: null, maxBeat: 0, bpmeas: 4,
        loaded: false, loading: false, reloadAgain: false, loadTimer: null,
        sel: [], pendingKeys: null, pendingRev: null,
        busy: false, lastDawSeq: 0,
        drag: null, pointers: {}, raf: false,
        cursorBeat: null, fitPending: false,
        events: [], evRows: [], evSel: {}, evRev: null, evLoading: false, evAgain: false, evRaf: false,
        previewBusy: false, reasonTimer: null, lastPreviewPitch: -1
    };
    if (M.ZOOM_X.indexOf(ui.ppb) < 0) ui.ppb = M.DEFAULT_X;
    if (M.ZOOM_Y.indexOf(ui.rowH) < 0) ui.rowH = M.DEFAULT_Y;

    function clampTrack(t) { t = Math.round(Number(t)); return t >= 0 && t <= 15 ? t : 0; }
    function $(id) { return doc.getElementById(id); }
    function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
    function daw() { return state.get('daw') || {}; }
    function transport() { return state.get('transport') || {}; }
    function recording() { return !!transport().is_recording; }
    function visible() { var v = $('view-editor'); return !!(v && !v.hidden); }
    function toast(msg, kind) { if (shell && shell.toast) shell.toast(msg, kind); }
    function fmtBeat(b) { return units.beatsToBBT(b, ui.bpmeas || daw().beats_per_measure || 4).text; }
    function posBeat() {
        var d = daw(), bpm = d.bpm > 0 ? d.bpm : 120;
        return Math.max(0, Number(transport().position || 0) * bpm / 60);
    }
    function view() { return { ppb: ui.ppb, rowH: ui.rowH, scrollX: el.scroll.scrollLeft, scrollY: el.scroll.scrollTop }; }
    function trackColor(t) {
        var d = daw();
        var idx = d.track_colors && d.track_colors[t] !== undefined ? d.track_colors[t] : t;
        var pal = SM && SM.TRACK_PALETTE;
        return pal ? pal[((idx % 16) + 16) % 16] : '#00d4ff';
    }
    function trackName(t) {
        var d = daw();
        var n = d.track_names ? (d.track_names[t] || d.track_names[String(t)]) : '';
        return (t + 1) + (n ? ' · ' + n : '');
    }
    function findClip(id) {
        for (var i = 0; i < ui.clips.length; i++) if (ui.clips[i].id === id) return ui.clips[i];
        return null;
    }
    function showReason(text, node) {
        var n = node || (ui.tab === 'events' ? el.evReason : el.reason);
        setText(n, text || '');
        clearTimeout(ui.reasonTimer);
        if (text) ui.reasonTimer = setTimeout(function () { setText(el.reason, ''); setText(el.evReason, ''); }, 4000);
    }

    /* ── Rete ────────────────────────────────────────────────────────── */
    function errorText(r) {
        if (r.unknown_outcome) return 'Esito non confermato: controlla lo stato prima di ripetere';
        var e = r.error || {}, d = e.details || {};
        if (e.code === 'REVISION_CONFLICT') return 'Il progetto e\' cambiato: dati riletti, ripeti il comando';
        if (e.code === 'TRANSPORT_ACTIVE') return 'Ferma Play/Record prima di questa operazione';
        if (e.code === 'RECORDING_ACTIVE') return 'Non disponibile durante il Record';
        if (e.code === 'EMPTY_CLIPBOARD') return 'Appunti vuoti';
        if (d.reason === 'pairing') return 'Modifica rifiutata: due note uguali si scambierebbero (sovrapposizione)';
        if (d.reason === 'max_events') return 'Troppi eventi nel progetto';
        return e.message || 'Operazione non riuscita';
    }

    function refreshState() {
        return api.request('GET', '/api/daw/state').then(function (r) {
            if (r.ok && r.body && r.body.data) state.ingestDawState(r.body.data);
        });
    }

    // `keys`: chiavi (M.noteKey) da riselezionare se la revisione dopo la modifica e' quella attesa.
    function edit(req, label, keys) {
        if (ui.busy) { showReason('Attendi la risposta del comando precedente'); return Promise.resolve(null); }
        if (!req || req.error) { showReason(req ? req.error : 'Nessuna modifica'); return Promise.resolve(null); }
        if (recording() && req.method !== 'GET') { showReason('Editor in sola lettura durante il Record'); return Promise.resolve(null); }
        ui.busy = true;
        var seq = ui.lastDawSeq;
        var expected = req.body && req.body.expected_revision;
        renderCommands();
        return api.request(req.method, req.url, req.body).then(function (r) {
            ui.busy = false;
            if (r.ok) {
                var b = r.body || {};
                if (keys) {
                    ui.pendingKeys = keys;
                    ui.pendingRev = typeof b.revision === 'number' ? b.revision : (typeof expected === 'number' ? expected + 1 : null);
                }
                if (label) toast(label, 'ok');
                if (ui.lastDawSeq === seq) refreshState();
                scheduleLoad(0);
            } else {
                toast(errorText(r), 'error');
                if (r.unknown_outcome || (r.error && r.error.code === 'REVISION_CONFLICT')) { refreshState(); scheduleLoad(0); }
            }
            renderCommands();
            return r;
        });
    }

    function scheduleLoad(delay) {
        clearTimeout(ui.loadTimer);
        ui.loadTimer = setTimeout(function () {
            if (!visible()) return;
            if (ui.tab === 'events') loadEvents(); else loadNotes();
        }, delay === undefined ? RELOAD_DEBOUNCE : delay);
    }

    function loadNotes() {
        if (ui.loading) { ui.reloadAgain = true; return; }
        ui.loading = true;
        var track = ui.track;
        api.request('GET', '/api/daw/track/' + track + '/notes').then(function (r) {
            ui.loading = false;
            if (track !== ui.track) { loadNotes(); return; }
            if (!r.ok) { if (!r.unknown_outcome) showReason(errorText(r)); }
            else {
                var b = r.body || {};
                var prevRev = ui.rev;
                ui.notes = M.normalizeNotes(b.notes);
                ui.maxLen = M.maxLength(ui.notes);
                ui.clips = b.clips || [];
                ui.rev = b.revision;
                ui.maxBeat = Number(b.max_beat) || 0;
                ui.bpmeas = b.beats_per_measure || ui.bpmeas;
                if (ui.pendingKeys && ui.pendingRev === ui.rev) ui.sel = M.reselect(ui.notes, ui.pendingKeys);
                else if (prevRev !== ui.rev || !ui.loaded) ui.sel = [];
                ui.pendingKeys = null; ui.pendingRev = null;
                ui.loaded = true;
                if (ui.fitPending) { ui.fitPending = false; fit(); }
                renderAll();
            }
            if (ui.reloadAgain || (ui.loaded && daw().revision !== undefined && daw().revision !== ui.rev && !recording())) {
                ui.reloadAgain = false;
                if (daw().revision !== ui.rev) scheduleLoad();
            }
        });
    }

    function loadEvents() {
        if (ui.evLoading) { ui.evAgain = true; return; }
        ui.evLoading = true;
        var track = ui.track;
        api.request('GET', '/api/daw/track/' + track + '/events').then(function (r) {
            ui.evLoading = false;
            if (track !== ui.track) { loadEvents(); return; }
            if (r.ok) {
                var b = r.body || {};
                if (b.revision !== ui.evRev) ui.evSel = {};
                ui.events = b.events || [];
                ui.evRev = b.revision;
                renderEvents(true);
            } else if (!r.unknown_outcome) showReason(errorText(r), el.evReason);
            if (ui.evAgain) { ui.evAgain = false; loadEvents(); }
        });
    }

    function preview(pitch, vel) {
        if (!ui.audition || ui.previewBusy || isNaN(pitch)) return;
        ui.previewBusy = true;
        api.request('POST', '/api/daw/preview_note', { channel: ui.track, note: pitch, velocity: vel || ui.vel, length_ms: 250 })
            .then(function () { ui.previewBusy = false; });
    }

    function transportCmd(cmd, btn) {
        var urls = { play_start: '/api/daw/play/start', play_stop: '/api/daw/play/stop', stop_all: '/api/daw/stop_all' };
        if (btn) btn.disabled = true;
        var p = api.command ? api.command('transport_cmd', { cmd: cmd }, { method: 'POST', url: urls[cmd], body: {} })
            : api.request('POST', urls[cmd], {});
        return p.then(function (r) {
            if (btn) btn.disabled = false;
            if (!r.ok) toast(errorText(r), 'error');
            renderCommands();
        });
    }

    /* ── Geometria e disegno ────────────────────────────────────────── */
    function totalBeats() {
        var bm = ui.bpmeas || 4, end = Math.max(ui.maxBeat, 0);
        ui.clips.forEach(function (c) { end = Math.max(end, Number(c.end_beat) || 0); });
        if (ui.notes.length) end = Math.max(end, ui.notes[ui.notes.length - 1].start + ui.maxLen);
        if (ui.cursorBeat !== null) end = Math.max(end, ui.cursorBeat);
        end = Math.max(end, posBeat());
        return Math.min(M.MAX_BEAT, Math.max(16 * bm, Math.ceil(end / bm) * bm + 8 * bm));
    }

    function sizeCanvas(cv, w, h) {
        var dpr = Math.min(root.devicePixelRatio || 1, 2);
        var W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(h * dpr));
        if (cv.width !== W || cv.height !== H) {
            cv.width = W; cv.height = H;
            cv.style.width = w + 'px'; cv.style.height = h + 'px';
        }
        var ctx = cv.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        return ctx;
    }

    function scheduleDraw() {
        if (ui.raf) return;
        ui.raf = true;
        root.requestAnimationFrame(function () { ui.raf = false; draw(); });
    }

    function hexAlpha(hex, a) {
        var h = String(hex || '#00d4ff').replace('#', '');
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        var n = parseInt(h, 16);
        return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
    }

    // Nota come apparirebbe durante il trascinamento (anteprima locale, nessuna richiesta).
    function displayed(i, n) {
        var d = ui.drag;
        if (!d || !d.moved || !d.selSet || !d.selSet[i]) {
            if (d && d.kind === 'vel' && d.targets[i]) return { note: n.note, start: n.start, end: n.end, vel: d.value };
            return n;
        }
        if (d.kind === 'move') return { note: n.note + d.dPitch, start: n.start + d.dBeat, end: n.end + d.dBeat, vel: n.vel };
        if (d.kind === 'resize' && n.off >= 0) return { note: n.note, start: n.start, end: Math.max(n.start + M.MIN_LEN, n.end + d.dBeat), vel: n.vel };
        return n;
    }

    function draw() {
        if (!visible() || ui.tab !== 'notes' || !el.scroll) return;
        var w = el.grid.clientWidth, h = el.grid.clientHeight;
        if (w < 2 || h < 2) return;
        var total = totalBeats();
        var sw = Math.ceil(total * ui.ppb) + 'px', sh = (128 * ui.rowH) + 'px';
        if (el.spacer.style.width !== sw) el.spacer.style.width = sw;
        if (el.spacer.style.height !== sh) el.spacer.style.height = sh;
        var v = view();
        var b0 = v.scrollX / v.ppb, b1 = (v.scrollX + w) / v.ppb;
        var nTop = M.yToNote(v, 0), nBot = M.yToNote(v, h - 1);
        var color = trackColor(ui.track);
        var ctx = sizeCanvas(el.canvas, w, h);

        // Righe: tasti neri piu' scuri, linea piu' marcata sui Do.
        ctx.fillStyle = '#14141f';
        ctx.fillRect(0, 0, w, h);
        for (var p = nBot; p <= nTop; p++) {
            var y = M.noteToY(v, p);
            if (M.isBlack(p)) { ctx.fillStyle = '#0e0e17'; ctx.fillRect(0, y, w, v.rowH); }
            if (p % 12 === 0) { ctx.fillStyle = '#2c2c40'; ctx.fillRect(0, y + v.rowH - 1, w, 1); }
        }
        // Cornici dei clip della traccia (il clip aperto piu' evidente).
        ui.clips.forEach(function (c) {
            var x0 = M.beatToX(v, c.start_beat), x1 = M.beatToX(v, c.end_beat);
            if (x1 < 0 || x0 > w) return;
            var open = c.id === ui.clipId;
            ctx.fillStyle = hexAlpha(color, open ? 0.10 : 0.05);
            ctx.fillRect(x0, 0, x1 - x0, h);
            ctx.fillStyle = hexAlpha(color, open ? 0.9 : 0.35);
            ctx.fillRect(x0, 0, 1, h); ctx.fillRect(x1 - 1, 0, 1, h);
        });
        // Griglia verticale: battute, beat, suddivisioni dello snap se abbastanza larghe.
        var bm = ui.bpmeas || 4;
        var step = ui.snap > 0 && ui.snap * v.ppb >= 8 ? ui.snap : (v.ppb >= 8 ? 1 : bm);
        var g = Math.floor(b0 / step) * step;
        for (; g <= b1; g += step) {
            var gx = Math.round(M.beatToX(v, g));
            var isBar = Math.abs(g / bm - Math.round(g / bm)) < 1e-6;
            var isBeat = Math.abs(g - Math.round(g)) < 1e-6;
            ctx.fillStyle = isBar ? '#3a3a55' : (isBeat ? '#25253a' : '#1b1b2b');
            ctx.fillRect(gx, 0, 1, h);
        }
        // Note visibili.
        var vis = M.visibleIndices(ui.notes, b0 - 64, b1, ui.maxLen + 64);
        var selSet = {};
        ui.sel.forEach(function (i) { selSet[i] = true; });
        var erase = ui.drag && ui.drag.kind === 'erase' ? ui.drag.hits : null;
        ctx.font = '10px sans-serif';
        ctx.textBaseline = 'middle';
        for (var k = 0; k < vis.length; k++) {
            var i = vis[k], n = displayed(i, ui.notes[i]);
            if (n.note > nTop + 1 || n.note < nBot - 1) continue;
            var r = M.noteRect(v, n);
            if (r.x > w || r.x + r.w < 0) continue;
            var other = ui.clipId !== null && ui.notes[i].clip !== ui.clipId;
            var on = !!selSet[i];
            ctx.fillStyle = erase && erase[i] ? 'rgba(255,82,82,0.8)' : (on ? '#ffffff' : hexAlpha(color, (other ? 0.35 : 0.55) + 0.4 * (n.vel / 127)));
            ctx.fillRect(r.x + 0.5, r.y + 1, Math.max(1, r.w - 1), Math.max(1, r.h - 2));
            ctx.fillStyle = on ? '#00d4ff' : 'rgba(0,0,0,0.45)';
            ctx.fillRect(r.x + 0.5, r.y + 1, 2, Math.max(1, r.h - 2));
            if (ui.notes[i].off < 0) { ctx.fillStyle = '#ffab40'; ctx.fillRect(r.x + r.w - 3, r.y + 1, 3, Math.max(1, r.h - 2)); }
            if (r.w > 34 && r.h >= 12) {
                ctx.fillStyle = on ? '#000' : 'rgba(0,0,0,0.75)';
                ctx.fillText(M.noteName(n.note), r.x + 5, r.y + r.h / 2);
            }
        }
        // Nota in disegno e rettangolo di selezione.
        var d = ui.drag;
        if (d && d.kind === 'draw') {
            var dr = M.noteRect(v, { note: d.pitch, start: d.start, end: d.start + d.len });
            ctx.fillStyle = 'rgba(0,212,255,0.55)';
            ctx.fillRect(dr.x, dr.y + 1, dr.w, Math.max(1, dr.h - 2));
        }
        if (d && d.kind === 'rect' && d.moved) {
            var rx = Math.min(d.x0, d.x1), ry = Math.min(d.y0, d.y1);
            ctx.strokeStyle = '#00d4ff'; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
            ctx.strokeRect(rx + 0.5, ry + 0.5, Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
            ctx.setLineDash([]);
            ctx.fillStyle = 'rgba(0,212,255,0.08)';
            ctx.fillRect(rx, ry, Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
        }
        if (ui.cursorBeat !== null) {
            var cx = Math.round(M.beatToX(v, ui.cursorBeat));
            ctx.fillStyle = '#ffab40'; ctx.fillRect(cx, 0, 1, h);
        }
        drawKeys(v, h, nTop, nBot);
        drawRuler(v, w, b0, b1);
        drawVel(v, vis, selSet);
        renderPlayhead();
    }

    function drawKeys(v, h, nTop, nBot) {
        var w = el.keys.clientWidth;
        var ctx = sizeCanvas(el.keysCanvas, w, h);
        ctx.fillStyle = '#d8d8e0'; ctx.fillRect(0, 0, w, h);
        ctx.font = '9px sans-serif'; ctx.textBaseline = 'middle';
        for (var p = nBot; p <= nTop; p++) {
            var y = M.noteToY(v, p);
            if (M.isBlack(p)) { ctx.fillStyle = '#1a1a24'; ctx.fillRect(0, y, w * 0.62, v.rowH); }
            ctx.fillStyle = '#9a9aa8'; ctx.fillRect(0, y + v.rowH - 1, w, p % 12 === 0 || p % 12 === 5 ? 1 : 0);
            if (p % 12 === 0 && v.rowH >= 8) { ctx.fillStyle = '#222'; ctx.fillText(M.noteName(p), w - 26, y + v.rowH / 2); }
        }
    }

    function drawRuler(v, w, b0, b1) {
        var h = el.ruler.clientHeight;
        var ctx = sizeCanvas(el.rulerCanvas, w, h);
        ctx.fillStyle = '#0f0f18'; ctx.fillRect(0, 0, w, h);
        var bm = ui.bpmeas || 4;
        var every = v.ppb * bm >= 40 ? 1 : (v.ppb * bm >= 16 ? 4 : 8);
        ctx.font = '10px sans-serif'; ctx.textBaseline = 'top';
        for (var b = Math.floor(b0); b <= b1; b++) {
            var x = Math.round(M.beatToX(v, b));
            var bar = b % bm === 0;
            if (!bar && v.ppb < 12) continue;
            ctx.fillStyle = bar ? '#6a6a88' : '#3a3a50';
            ctx.fillRect(x, bar ? 0 : h - 8, 1, bar ? h : 8);
            if (bar && (b / bm) % every === 0) { ctx.fillStyle = '#b0b0c8'; ctx.fillText(String(b / bm + 1), x + 3, 3); }
        }
        ui.clips.forEach(function (c) {
            var x0 = M.beatToX(v, c.start_beat), x1 = M.beatToX(v, c.end_beat);
            if (x1 < 0 || x0 > w) return;
            ctx.fillStyle = c.id === ui.clipId ? 'rgba(0,212,255,0.7)' : 'rgba(150,150,180,0.35)';
            ctx.fillRect(x0, h - 3, x1 - x0, 3);
        });
        if (ui.cursorBeat !== null) {
            var cx = M.beatToX(v, ui.cursorBeat);
            ctx.fillStyle = '#ffab40';
            ctx.beginPath(); ctx.moveTo(cx - 6, 0); ctx.lineTo(cx + 6, 0); ctx.lineTo(cx, 10); ctx.closePath(); ctx.fill();
        }
    }

    function drawVel(v, vis, selSet) {
        var w = el.velLane.clientWidth, h = el.velLane.clientHeight;
        if (w < 2 || h < 2) return;
        var ctx = sizeCanvas(el.velCanvas, w, h);
        ctx.fillStyle = '#0d0d14'; ctx.fillRect(0, 0, w, h);
        ctx.fillStyle = '#1f1f2e'; ctx.fillRect(0, Math.round(h / 2), w, 1);
        var color = trackColor(ui.track);
        for (var k = 0; k < vis.length; k++) {
            var i = vis[k], n = displayed(i, ui.notes[i]);
            var x = M.beatToX(v, n.start);
            if (x < -4 || x > w) continue;
            var bh = Math.max(2, (n.vel / 127) * (h - 4));
            ctx.fillStyle = selSet[i] ? '#00d4ff' : hexAlpha(color, 0.75);
            ctx.fillRect(Math.round(x), h - bh, 3, bh);
        }
    }

    function renderPlayhead() {
        if (!el.playhead) return;
        var v = view(), x = M.beatToX(v, posBeat());
        var t = 'translateX(' + Math.round(x) + 'px)';
        var show = x >= 0 && x <= el.grid.clientWidth;
        el.playhead.style.transform = t; el.rulerPlayhead.style.transform = t;
        el.playhead.style.display = show ? '' : 'none'; el.rulerPlayhead.style.display = show ? '' : 'none';
        // Segue la riproduzione a pagine (niente scorrimento continuo: costa meno sul Pi).
        if (transport().is_playing && !ui.drag && (x > el.grid.clientWidth - 8 || x < 0)) {
            el.scroll.scrollLeft = Math.max(0, posBeat() * ui.ppb - 24);
        }
    }

    /* ── Selezione, testi e comandi ─────────────────────────────────── */
    function setSel(list) { ui.sel = list; renderCommands(); scheduleDraw(); }

    function renderCommands() {
        if (!el.root) return;
        var rec = recording(), busy = ui.busy, d = daw(), t = transport(), n = ui.sel.length;
        var ro = rec || busy;
        var any = ui.notes.length > 0;
        function dis(op, off, why) {
            var bs = el.root.querySelectorAll('[data-ed-op="' + op + '"]');
            for (var i = 0; i < bs.length; i++) {
                bs[i].disabled = !!off;
                if (off && why) bs[i].title = why; else bs[i].removeAttribute('title');
            }
        }
        var needSel = n ? null : 'Seleziona almeno una nota';
        var why = rec ? 'Non disponibile durante il Record' : (busy ? 'Attendi la risposta' : null);
        dis('select_all', !any, 'Nessuna nota');
        dis('select_none', !n, 'Nessuna nota selezionata');
        dis('copy', rec || busy || !n, why || needSel);
        ['cut', 'delete', 'velocity'].forEach(function (op) { dis(op, ro || !n, why || needSel); });
        dis('transpose', ro || !n, why || needSel);
        dis('paste', ro || !!t.is_playing, why || (t.is_playing ? 'Ferma Play prima di incollare' : null));
        dis('quantize_open', ro || !any, why || 'Nessuna nota');
        dis('quantize', ro || !any, why);
        el.undo.disabled = ro || !!t.is_playing || !d.can_undo;
        el.redo.disabled = ro || !!t.is_playing || !d.can_redo;
        var playOn = !!t.is_playing;
        el.play.classList.toggle('is-on', playOn);
        el.play.setAttribute('aria-label', playOn ? 'Ferma la riproduzione' : 'Play');
        el.play.querySelector('i').className = playOn ? 'fas fa-pause' : 'fas fa-play';
        if (n) {
            var b = M.selectionBounds(ui.notes, ui.sel);
            setText(el.selText, n + (n === 1 ? ' nota' : ' note') + ' · ' + fmtBeat(b.start) +
                (n === 1 ? ' · ' + M.noteName(ui.notes[ui.sel[0]].note) + ' vel ' + ui.notes[ui.sel[0]].vel : ''));
        } else {
            setText(el.selText, rec ? 'Record in corso: editor in sola lettura' :
                (ui.notes.length + ' note' + (ui.cursorBeat !== null ? ' · incolla a ' + fmtBeat(ui.cursorBeat) : '')));
        }
        if (n && ui.cursorBeat !== null) setText(el.selText, el.selText.textContent + ' · incolla a ' + fmtBeat(ui.cursorBeat));
        el.root.classList.toggle('is-readonly', rec);
    }

    function renderHeader() {
        var d = daw();
        if (el.track.options.length !== 16) {
            var html = '';
            for (var t = 0; t < 16; t++) html += '<option value="' + t + '"></option>';
            el.track.innerHTML = html;
        }
        for (var i = 0; i < 16; i++) setText(el.track.options[i], trackName(i));
        el.track.value = String(ui.track);
        var c = ui.clipId !== null ? findClip(ui.clipId) : null;
        if (ui.clipId !== null && ui.loaded && !c) ui.clipId = null;
        setText(el.title, c ? 'Clip ' + (c.name || ('#' + c.id)) + ' · ' + fmtBeat(c.start_beat) + ' – ' + fmtBeat(c.end_beat)
            : 'Tutta la traccia');
        if (d.bpm) ui.bpmeas = d.beats_per_measure || ui.bpmeas;
    }

    function renderAll() {
        if (!el.root || !visible()) return;
        renderHeader();
        renderCommands();
        if (ui.tab === 'notes') scheduleDraw(); else renderEvents(false);
    }

    /* ── Zoom e Adatta ─────────────────────────────────────────────── */
    function setZoom(axis, dir) {
        var w = el.grid.clientWidth, h = el.grid.clientHeight, v = view();
        if (axis === 'x') {
            var cb = (v.scrollX + w / 2) / ui.ppb;
            var nx = M.zoomStep(M.ZOOM_X, ui.ppb, dir);
            if (nx === ui.ppb) return;
            ui.ppb = nx; state.setPref('editor.zoomX', nx);
            draw();
            el.scroll.scrollLeft = Math.max(0, cb * nx - w / 2);
        } else {
            var cn = (v.scrollY + h / 2) / ui.rowH;
            var ny = M.zoomStep(M.ZOOM_Y, ui.rowH, dir);
            if (ny === ui.rowH) return;
            ui.rowH = ny; state.setPref('editor.zoomY', ny);
            draw();
            el.scroll.scrollTop = Math.max(0, cn * ny - h / 2);
        }
        scheduleDraw();
    }

    // Mostra il clip aperto (o tutte le note): zoom orizzontale piu' grande che ci sta, note centrate.
    function fit() {
        var w = el.grid.clientWidth || 600, h = el.grid.clientHeight || 300;
        var c = ui.clipId !== null ? findClip(ui.clipId) : null;
        var s, e, list = ui.notes;
        if (c) { s = c.start_beat; e = c.end_beat; list = ui.notes.filter(function (n) { return n.clip === c.id; }); }
        else if (ui.notes.length) { var bb = M.selectionBounds(ui.notes, ui.notes.map(function (_n, i) { return i; })); s = bb.start; e = bb.end; }
        else { s = 0; e = 4 * (ui.bpmeas || 4); }
        var span = Math.max(1, e - s), ppb = M.ZOOM_X[0];
        M.ZOOM_X.forEach(function (z) { if (span * z <= w - 16) ppb = z; });
        ui.ppb = ppb; state.setPref('editor.zoomX', ppb);
        var lo = 127, hi = 0;
        list.forEach(function (n) { lo = Math.min(lo, n.note); hi = Math.max(hi, n.note); });
        if (lo > hi) { lo = 48; hi = 72; }
        draw();
        el.scroll.scrollLeft = Math.max(0, s * ppb - 8);
        el.scroll.scrollTop = Math.max(0, ((127 - (lo + hi) / 2) + 0.5) * ui.rowH - h / 2);
        scheduleDraw();
    }

    /* ── Gesti sul piano roll ───────────────────────────────────────── */
    function local(e, node) {
        var r = node.getBoundingClientRect();
        return { x: e.clientX - r.left, y: e.clientY - r.top };
    }
    function canEdit() {
        if (recording()) { showReason('Editor in sola lettura durante il Record'); return false; }
        if (ui.busy) { showReason('Attendi la risposta del comando precedente'); return false; }
        return true;
    }
    function hit(p, touch) {
        return M.hitTest(ui.notes, view(), p.x, p.y, { edgePx: touch ? EDGE_TOUCH : EDGE_MOUSE, minH: touch ? TOUCH_ROW : 0, maxLen: ui.maxLen });
    }
    function selSetOf(list) { var s = {}; list.forEach(function (i) { s[i] = true; }); return s; }

    function onDown(e) {
        if (e.button !== undefined && e.button > 0) return;
        var touch = e.pointerType === 'touch' || e.pointerType === 'pen';
        ui.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
        var ids = Object.keys(ui.pointers);
        if (ids.length === 2 && ui.tool !== 'pan') {
            // Secondo dito: annulla il gesto in corso e scorre con due dita.
            ui.drag = { kind: 'pan2', sx: el.scroll.scrollLeft, sy: el.scroll.scrollTop, c: centroid() };
            scheduleDraw();
            return;
        }
        if (ids.length > 2) return;
        if (ui.tool === 'pan') {
            if (touch) return;   // al tocco scorre il browser (touch-action: pan-x pan-y)
            ui.drag = { kind: 'pan', x0: e.clientX, y0: e.clientY, sx: el.scroll.scrollLeft, sy: el.scroll.scrollTop, moved: true };
            el.scroll.setPointerCapture(e.pointerId);
            return;
        }
        var p = local(e, el.scroll), v = view();
        // Il contenitore ha le scrollbar: un tocco sopra di esse non e' un gesto.
        if (p.x >= el.scroll.clientWidth || p.y >= el.scroll.clientHeight) return;
        e.preventDefault();
        try { el.scroll.setPointerCapture(e.pointerId); } catch (_e) { /* puntatore gia' rilasciato */ }
        var additive = ui.multi || e.shiftKey || e.ctrlKey || e.metaKey;
        var h = hit(p, touch);
        var base = { x0: p.x, y0: p.y, x1: p.x, y1: p.y, moved: false, additive: additive, touch: touch,
            beat0: M.xToBeat(v, p.x), pitch0: M.yToNote(v, p.y) };
        if (ui.tool === 'erase') {
            base.kind = 'erase'; base.hits = {};
            if (h) base.hits[h.index] = true;
            ui.drag = base; scheduleDraw();
            return;
        }
        if (h) {
            var wasSel = ui.sel.indexOf(h.index) >= 0;
            if (!wasSel) ui.sel = additive ? M.union(ui.sel, [h.index]) : [h.index];
            base.kind = h.part === 'end' && ui.notes[h.index].off >= 0 ? 'resize' : 'move';
            base.idx = h.index; base.wasSel = wasSel; base.dBeat = 0; base.dPitch = 0;
            base.selSet = selSetOf(ui.sel);
            ui.drag = base;
            ui.lastPreviewPitch = ui.notes[h.index].note;
            if (!wasSel) preview(ui.notes[h.index].note, ui.notes[h.index].vel);
            renderCommands(); scheduleDraw();
            return;
        }
        if (ui.tool === 'draw') {
            var start = M.snapFloor(base.beat0, ui.snap);
            base.kind = 'draw'; base.start = start; base.pitch = base.pitch0; base.len = ui.len;
            ui.drag = base;
            preview(base.pitch, ui.vel);
            scheduleDraw();
            return;
        }
        base.kind = 'rect'; base.base = additive ? ui.sel.slice() : [];
        ui.drag = base;
    }

    function centroid() {
        var xs = 0, ys = 0, k = 0;
        Object.keys(ui.pointers).forEach(function (id) { xs += ui.pointers[id].x; ys += ui.pointers[id].y; k++; });
        return { x: xs / k, y: ys / k };
    }

    function onMove(e) {
        if (ui.pointers[e.pointerId]) ui.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
        var d = ui.drag;
        if (!d) {
            if (e.pointerType === 'mouse' && ui.tool === 'select') {
                var hh = hit(local(e, el.scroll), false);
                el.scroll.classList.toggle('is-resize', !!(hh && hh.part === 'end'));
                el.scroll.classList.toggle('is-move', !!(hh && hh.part === 'body'));
            }
            return;
        }
        if (d.kind === 'pan2') {
            var c = centroid();
            el.scroll.scrollLeft = d.sx - (c.x - d.c.x);
            el.scroll.scrollTop = d.sy - (c.y - d.c.y);
            return;
        }
        if (d.kind === 'pan') {
            el.scroll.scrollLeft = d.sx - (e.clientX - d.x0);
            el.scroll.scrollTop = d.sy - (e.clientY - d.y0);
            return;
        }
        var p = local(e, el.scroll), v = view();
        d.x1 = p.x; d.y1 = p.y;
        if (!d.moved && Math.abs(p.x - d.x0) < DRAG_PX && Math.abs(p.y - d.y0) < DRAG_PX) return;
        d.moved = true;
        var beat = M.xToBeat(v, p.x), raw = beat - d.beat0;
        if (d.kind === 'move') {
            var a = ui.notes[d.idx];
            var dB = ui.snap > 0 ? M.snapBeat(a.start + raw, ui.snap) - a.start : raw;
            var cl = M.clampMove(ui.notes, ui.sel, dB, M.yToNote(v, p.y) - d.pitch0);
            d.dBeat = cl.dBeat; d.dPitch = cl.dPitch;
            if (a.note + d.dPitch !== ui.lastPreviewPitch) { ui.lastPreviewPitch = a.note + d.dPitch; preview(ui.lastPreviewPitch, a.vel); }
        } else if (d.kind === 'resize') {
            var r = ui.notes[d.idx];
            var end = ui.snap > 0 ? M.snapBeat(r.end + raw, ui.snap) : r.end + raw;
            d.dBeat = Math.max(end, r.start + (ui.snap > 0 ? ui.snap : M.MIN_LEN)) - r.end;
        } else if (d.kind === 'draw') {
            var e2 = ui.snap > 0 ? M.snapBeat(beat, ui.snap) : beat;
            d.len = Math.max(ui.snap > 0 ? ui.snap : M.MIN_LEN, e2 - d.start);
        } else if (d.kind === 'erase') {
            var h = hit(p, d.touch);
            if (h) d.hits[h.index] = true;
        } else if (d.kind === 'rect') {
            var inRect = M.rectSelect(ui.notes, M.xToBeat(v, d.x0), beat, M.yToNote(v, d.y0), M.yToNote(v, p.y));
            ui.sel = M.union(d.base, inRect);
            renderCommands();
        }
        scheduleDraw();
    }

    function onUp(e) {
        delete ui.pointers[e.pointerId];
        var d = ui.drag;
        if (!d) return;
        if (d.kind === 'pan2') { if (!Object.keys(ui.pointers).length) ui.drag = null; scheduleDraw(); return; }
        ui.drag = null;
        if (e.type === 'pointercancel') { scheduleDraw(); return; }
        if (d.kind === 'pan') return;
        var v = view();
        if (d.kind === 'move' || d.kind === 'resize') {
            if (!d.moved) {
                if (d.additive && d.wasSel) ui.sel = M.toggleIndex(ui.sel, d.idx);
                else if (!d.additive) ui.sel = [d.idx];
                renderCommands(); scheduleDraw();
                return;
            }
            if (!canEdit()) { scheduleDraw(); return; }
            var res = d.kind === 'move' ? M.moveEdits(ui.notes, ui.sel, d.dBeat, d.dPitch) : M.resizeEdits(ui.notes, ui.sel, d.dBeat, M.MIN_LEN);
            if (res.error) { showReason(res.error); scheduleDraw(); return; }
            if (!res.edits.length) { scheduleDraw(); return; }
            edit(M.editRequest(ui.track, ui.rev, res.edits), null, res.keys).then(scheduleDraw);
            scheduleDraw();
            return;
        }
        if (d.kind === 'draw') {
            if (!canEdit()) { scheduleDraw(); return; }
            var len = M.drawLength(ui.notes, d.pitch, d.start, d.len);
            if (len === null) { showReason('C\'e\' gia\' una nota ' + M.noteName(d.pitch) + ' in quel punto'); scheduleDraw(); return; }
            var key = M.noteKey({ note: d.pitch, start: d.start, end: d.start + len });
            edit(M.addRequest(ui.track, ui.rev, d.pitch, ui.vel, d.start, len), null, [key]);
            ui.drag = { kind: 'draw', pitch: d.pitch, start: d.start, len: len, ghost: true };
            scheduleDraw();
            setTimeout(function () { if (ui.drag && ui.drag.ghost) { ui.drag = null; scheduleDraw(); } }, 400);
            return;
        }
        if (d.kind === 'erase') {
            var list = Object.keys(d.hits).map(Number);
            scheduleDraw();
            if (!list.length || !canEdit()) return;
            var keep = ui.sel.filter(function (i) { return !d.hits[i]; }).map(function (i) { return M.noteKey(ui.notes[i]); });
            edit(M.deleteRequest(ui.track, ui.rev, ui.notes, list), list.length + (list.length === 1 ? ' nota eliminata' : ' note eliminate'), keep);
            return;
        }
        if (d.kind === 'rect') {
            if (!d.moved) {
                if (!d.additive) ui.sel = [];
                ui.cursorBeat = M.snapBeat(M.xToBeat(v, d.x0), ui.snap);
            }
            renderCommands(); scheduleDraw();
        }
    }

    // Velocity lane: trascinamento verticale = nuova velocity per la nota toccata (o per la selezione, se
    // la nota toccata ne fa parte). Una richiesta al rilascio.
    function velFromY(y) { var h = el.velLane.clientHeight; return Math.max(1, Math.min(127, Math.round((1 - (y - 2) / (h - 4)) * 127))); }
    function onVelDown(e) {
        if (e.button !== undefined && e.button > 0) return;
        if (!canEdit()) return;
        var p = local(e, el.velLane), v = view();
        var vis = M.visibleIndices(ui.notes, M.xToBeat(v, p.x - 12), M.xToBeat(v, p.x + 12), ui.maxLen);
        var best = -1, bd = 1e9;
        vis.forEach(function (i) {
            var dx = Math.abs(M.beatToX(v, ui.notes[i].start) - p.x);
            var bonus = ui.sel.indexOf(i) >= 0 ? -4 : 0;
            if (dx <= 12 && dx + bonus < bd) { bd = dx + bonus; best = i; }
        });
        if (best < 0) return;
        e.preventDefault();
        try { el.velLane.setPointerCapture(e.pointerId); } catch (_e) { /* ignorato */ }
        var targets = ui.sel.indexOf(best) >= 0 ? ui.sel.slice() : [best];
        if (ui.sel.indexOf(best) < 0) ui.sel = [best];
        ui.drag = { kind: 'vel', list: targets, targets: selSetOf(targets), value: velFromY(p.y), moved: true };
        renderCommands(); scheduleDraw();
    }
    function onVelMove(e) {
        if (!ui.drag || ui.drag.kind !== 'vel') return;
        ui.drag.value = velFromY(local(e, el.velLane).y);
        setText(el.reason, 'Velocity ' + ui.drag.value);
        scheduleDraw();
    }
    function onVelUp(e) {
        var d = ui.drag;
        if (!d || d.kind !== 'vel') return;
        ui.drag = null;
        if (e.type === 'pointercancel') { scheduleDraw(); return; }
        var res = M.velocityEdits(ui.notes, d.list, d.value, 'set');
        if (res.edits.length) edit(M.editRequest(ui.track, ui.rev, res.edits), 'Velocity ' + d.value, res.keys);
        scheduleDraw();
    }

    /* ── Operazioni della barra ─────────────────────────────────────── */
    function targetsOrAll() { return ui.sel.length ? ui.sel.slice() : ui.notes.map(function (_n, i) { return i; }); }
    function count(n) { return n + (n === 1 ? ' nota' : ' note'); }

    function runOp(op, btn) {
        if (btn && btn.disabled) return;
        var n = ui.sel.length, res;
        switch (op) {
            case 'select_all': return setSel(ui.notes.map(function (_n, i) { return i; }));
            case 'select_none': return setSel([]);
            case 'copy':
                if (!n) return showReason('Seleziona almeno una nota');
                return edit(M.clipboardRequest('copy', ui.track, ui.rev, ui.notes, ui.sel), count(n) + ' copiate', ui.sel.map(function (i) { return M.noteKey(ui.notes[i]); }));
            case 'cut':
                if (!n || !canEdit()) return n ? null : showReason('Seleziona almeno una nota');
                return edit(M.clipboardRequest('cut', ui.track, ui.rev, ui.notes, ui.sel), count(n) + ' tagliate', []);
            case 'paste': {
                if (!canEdit()) return;
                var at = ui.cursorBeat !== null ? ui.cursorBeat : M.snapBeat(posBeat(), ui.snap);
                return edit(M.pasteRequest(ui.track, ui.rev, at), 'Incollato a ' + fmtBeat(at), []);
            }
            case 'delete':
                if (!n) return showReason('Seleziona almeno una nota');
                if (!canEdit()) return;
                return edit(M.deleteRequest(ui.track, ui.rev, ui.notes, ui.sel), count(n) + ' eliminate', []);
            case 'transpose': {
                if (!n) return showReason('Seleziona almeno una nota');
                if (!canEdit()) return;
                var s = Number(btn ? btn.getAttribute('data-semitones') : 0);
                res = M.transposeEdits(ui.notes, ui.sel, s);
                if (res.error) return showReason(res.error);
                return edit(M.editRequest(ui.track, ui.rev, res.edits), 'Trasposte di ' + (s > 0 ? '+' : '') + s, res.keys);
            }
            case 'velocity': {
                if (!n) return showReason('Seleziona almeno una nota');
                if (!canEdit()) return;
                var vv = Math.round(Number(el.vel.value));
                if (!(vv >= 1 && vv <= 127)) return showReason('Velocity: 1..127');
                res = M.velocityEdits(ui.notes, ui.sel, vv, 'set');
                if (!res.edits.length) return showReason('Velocity gia\' ' + vv);
                return edit(M.editRequest(ui.track, ui.rev, res.edits), 'Velocity ' + vv, res.keys);
            }
            case 'quantize_open':
                el.quant.hidden = !el.quant.hidden;
                if (!el.quant.hidden) el.qGrid.focus();
                return;
            case 'quantize_close': el.quant.hidden = true; return;
            case 'quantize': {
                if (!canEdit()) return;
                var targets = targetsOrAll();
                res = M.quantizeEdits(ui.notes, targets, Number(el.qGrid.value), Number(el.qStrength.value) / 100, Number(el.qSwing.value) / 100);
                el.quant.hidden = true;
                if (!res.edits.length) return showReason('Note gia\' sulla griglia');
                return edit(M.editRequest(ui.track, ui.rev, res.edits), count(res.edits.length) + ' quantizzate', ui.sel.length ? res.keys : null);
            }
        }
    }

    function nudge(dBeat, dPitch) {
        if (!ui.sel.length || !canEdit()) return;
        var res = M.moveEdits(ui.notes, ui.sel, dBeat, dPitch);
        if (res.error) return showReason(res.error);
        if (res.edits.length) edit(M.editRequest(ui.track, ui.rev, res.edits), null, res.keys);
    }

    function onCmd(cmd, btn) {
        var t = transport();
        switch (cmd) {
            case 'back': return shell && shell.showView ? shell.showView('studio') : null;
            case 'play': return transportCmd(t.is_playing ? 'play_stop' : 'play_start', btn);
            case 'stop': return transportCmd('stop_all', btn);
            case 'undo': return edit({ method: 'POST', url: '/api/daw/undo', body: {} }, 'Annullato');
            case 'redo': return edit({ method: 'POST', url: '/api/daw/redo', body: {} }, 'Ripetuto');
            case 'multi':
                ui.multi = !ui.multi;
                btn.setAttribute('aria-pressed', String(ui.multi));
                return;
            case 'audition':
                ui.audition = !ui.audition;
                state.setPref('editor.audition', ui.audition);
                btn.setAttribute('aria-pressed', String(ui.audition));
                return;
            case 'zoom-x-in': return setZoom('x', 1);
            case 'zoom-x-out': return setZoom('x', -1);
            case 'zoom-y-in': return setZoom('y', 1);
            case 'zoom-y-out': return setZoom('y', -1);
            case 'fit': return fit();
        }
    }

    function setTool(tool) {
        ui.tool = tool;
        el.root.setAttribute('data-tool', tool);
        var b = el.root.querySelectorAll('[data-ed-tool]');
        for (var i = 0; i < b.length; i++) b[i].setAttribute('aria-pressed', String(b[i].getAttribute('data-ed-tool') === tool));
    }

    function setTab(tab) {
        ui.tab = tab;
        var b = el.root.querySelectorAll('[data-ed-tab]');
        for (var i = 0; i < b.length; i++) {
            var on = b[i].getAttribute('data-ed-tab') === tab;
            b[i].setAttribute('aria-pressed', String(on)); b[i].setAttribute('aria-selected', String(on));
        }
        el.notesPane.hidden = tab !== 'notes';
        el.eventsPane.hidden = tab !== 'events';
        if (tab === 'events') { renderEvents(true); loadEvents(); } else { loadNotes(); scheduleDraw(); }
    }

    /* ── Editor eventi (lista virtualizzata) ────────────────────────── */
    function evFiltered() {
        var f = el.evFilter.value;
        var list = M.filterEvents(ui.events, f, f === 'note');
        if (el.evClipOnly.checked && ui.clipId !== null) list = list.filter(function (e) { return e.clip === ui.clipId; });
        return list;
    }
    function esc(s) { return String(s === undefined || s === null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    function renderEvents(full) {
        if (!el.evList || ui.tab !== 'events') return;
        if (full) ui.evRows = evFiltered();
        var rows = ui.evRows;
        el.evSpacer.style.height = Math.max(1, rows.length * EV_ROW) + 'px';
        if (ui.evRaf) return;
        ui.evRaf = true;
        root.requestAnimationFrame(function () { ui.evRaf = false; paintEvents(); });
    }

    function paintEvents() {
        var rows = ui.evRows, ro = recording() || ui.busy;
        var top = el.evList.scrollTop, hgt = el.evList.clientHeight || 400;
        var first = Math.max(0, Math.floor(top / EV_ROW) - 2), last = Math.min(rows.length, Math.ceil((top + hgt) / EV_ROW) + 2);
        var html = '';
        if (!rows.length) html = '<div class="ed-ev-empty">' + (ui.events.length ? 'Nessun evento con questo filtro.' : 'Nessun evento su questa traccia.') + '</div>';
        for (var i = first; i < last; i++) {
            var e = rows[i], k = M.eventKind(e), f = M.valueField(e), isNote = k === 'note';
            var dis = (ro || isNote) ? ' disabled' : '';
            var val = isNote ? (e.vel !== undefined ? e.vel : '') : M.eventValue(e);
            html += '<div class="ed-ev-row' + (ui.evSel[e.idx] ? ' is-sel' : '') + (isNote ? ' is-note' : '') + '" style="top:' + (i * EV_ROW) + 'px" data-idx="' + e.idx + '">' +
                '<label class="ed-ev-check"><input type="checkbox" data-ev-sel="' + e.idx + '"' + (ui.evSel[e.idx] ? ' checked' : '') + ' aria-label="Seleziona evento ' + (i + 1) + '"></label>' +
                '<span>' + esc(fmtBeat(e.beat)) + '</span>' +
                '<input type="number" step="0.25" min="0" max="' + M.MAX_BEAT + '" value="' + esc(Math.round(e.beat * 1e4) / 1e4) + '" data-ev-f="beat" aria-label="Beat"' + dis + '>' +
                '<span>' + esc(M.describeEvent(e)) + '</span>' +
                (k === 'cc' ? '<input type="number" min="0" max="127" step="1" value="' + esc(e.cc_num) + '" data-ev-f="cc_num" aria-label="Numero CC"' + dis + '>' : '<span></span>') +
                (f || isNote ? '<input type="number" step="1" min="' + (f ? f.lo : 0) + '" max="' + (f ? f.hi : 127) + '" value="' + esc(val) + '" data-ev-f="value" aria-label="Valore"' + (f ? dis : ' disabled') + '>' : '<span></span>') +
                '<span class="ed-ev-clip">' + (e.clip ? '#' + esc(e.clip) : '–') + '</span>' +
                '</div>';
        }
        // Le righe si ricostruiscono solo se cambiano: mantiene il focus durante la scrittura in un campo.
        var sig = first + ':' + last + ':' + ui.evRev + ':' + rows.length + ':' + ro + ':' + Object.keys(ui.evSel).length + ':' + el.evFilter.value;
        if (el.evList.getAttribute('data-sig') !== sig) {
            el.evList.setAttribute('data-sig', sig);
            var olds = el.evList.querySelectorAll('.ed-ev-row, .ed-ev-empty');
            for (var j = 0; j < olds.length; j++) olds[j].parentNode.removeChild(olds[j]);
            el.evSpacer.insertAdjacentHTML('afterend', html);
        }
        var nsel = Object.keys(ui.evSel).length;
        setText(el.evSelText, nsel ? nsel + (nsel === 1 ? ' evento selezionato' : ' eventi selezionati') : rows.length + ' eventi mostrati');
        el.evAll.checked = rows.length > 0 && rows.every(function (r) { return ui.evSel[r.idx]; });
        var bs = el.eventsPane.querySelectorAll('[data-ed-ev]');
        for (var b = 0; b < bs.length; b++) bs[b].disabled = ro || !nsel;
    }

    function evByIdx(idx) {
        for (var i = 0; i < ui.events.length; i++) if (ui.events[i].idx === idx) return ui.events[i];
        return null;
    }
    function evEdit(req, label) {
        if (ui.busy) { showReason('Attendi la risposta del comando precedente', el.evReason); return; }
        if (req.error) { showReason(req.error, el.evReason); return; }
        ui.evSel = {};
        edit(req, label).then(function () { loadEvents(); });
    }
    function onEvChange(e) {
        var t = e.target;
        if (t.hasAttribute('data-ev-sel')) {
            var idx = Number(t.getAttribute('data-ev-sel'));
            if (t.checked) ui.evSel[idx] = true; else delete ui.evSel[idx];
            renderEvents(false);
            return;
        }
        var f = t.getAttribute('data-ev-f');
        if (!f) return;
        var row = t.closest('.ed-ev-row'), ev = row && evByIdx(Number(row.getAttribute('data-idx')));
        if (!ev) return;
        var patch = {}; patch[f] = t.value;
        var r = M.eventEdit(ev, patch);
        if (!r) return;
        if (r.error) { showReason(r.error, el.evReason); renderEvents(true); return; }
        evEdit(M.eventsEditRequest(ui.track, ui.evRev, [r]), 'Evento modificato');
    }
    function onEvAction(act) {
        var sel = ui.events.filter(function (e) { return ui.evSel[e.idx]; });
        if (!sel.length) return showReason('Seleziona almeno un evento', el.evReason);
        if (act === 'delete') {
            return evEdit(M.eventsDeleteRequest(ui.track, ui.evRev, sel.map(function (e) { return e.idx; })),
                sel.length + (sel.length === 1 ? ' evento eliminato' : ' eventi eliminati'));
        }
        var dBeat = Number(el.evShift.value) || 0;
        var value = el.evValue.value;
        var editable = sel.filter(function (e) { return M.eventKind(e) !== 'note'; });
        if (!editable.length) return showReason('Le note si modificano nel piano roll', el.evReason);
        var res = M.eventsBatchEdits(editable, { dBeat: dBeat, value: value === '' ? undefined : Number(value), mode: el.evMode.value });
        if (res.error) return showReason(res.error, el.evReason);
        if (!res.edits.length) return showReason('Nessuna modifica: imposta uno spostamento o un valore', el.evReason);
        evEdit(M.eventsEditRequest(ui.track, ui.evRev, res.edits), res.edits.length + ' eventi modificati');
    }

    /* ── Tastiera (solo con l'editor visibile) ──────────────────────── */
    function onKeydown(e) {
        if (!visible()) return;
        if (doc.body.classList.contains('ms-panel-open')) return;
        if (e.target.closest && e.target.closest('.ms-globalbar, .ms-panel')) return;
        var tag = e.target && e.target.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        var k = e.key, ctrl = e.ctrlKey || e.metaKey;
        if (ctrl && (k === 'z' || k === 'Z')) { e.preventDefault(); onCmd(e.shiftKey ? 'redo' : 'undo'); return; }
        if (ctrl && (k === 'y' || k === 'Y')) { e.preventDefault(); onCmd('redo'); return; }
        if (k === ' ' && !(e.target.closest && e.target.closest('button'))) { e.preventDefault(); onCmd(transport().is_recording ? 'stop' : 'play'); return; }
        if (ui.tab !== 'notes') return;
        var map = null;
        if (ctrl && (k === 'a' || k === 'A')) map = 'select_all';
        else if (ctrl && (k === 'c' || k === 'C')) map = 'copy';
        else if (ctrl && (k === 'x' || k === 'X')) map = 'cut';
        else if (ctrl && (k === 'v' || k === 'V')) map = 'paste';
        else if (k === 'Delete' || k === 'Backspace') map = 'delete';
        if (map) { e.preventDefault(); runOp(map); return; }
        if (k === 'Escape') { el.quant.hidden = true; ui.cursorBeat = null; setSel([]); return; }
        if (!ui.sel.length) return;
        var step = ui.snap > 0 ? ui.snap : 0.25;
        if (k === 'ArrowUp' || k === 'ArrowDown') { e.preventDefault(); nudge(0, (k === 'ArrowUp' ? 1 : -1) * (e.shiftKey ? 12 : 1)); }
        else if (k === 'ArrowLeft' || k === 'ArrowRight') { e.preventDefault(); nudge((k === 'ArrowRight' ? 1 : -1) * step, 0); }
    }

    /* ── Apertura ───────────────────────────────────────────────────── */
    // opts: {track, clipId, startBeat}. Senza clip si mostra tutta la traccia.
    function open(opts) {
        var o = opts || {};
        var track = clampTrack(o.track !== undefined ? o.track : ui.track);
        if (track !== ui.track) { ui.notes = []; ui.clips = []; ui.rev = null; ui.loaded = false; ui.events = []; ui.evRev = null; }
        ui.track = track;
        state.setPref('editor.track', track);
        ui.clipId = o.clipId !== undefined && o.clipId !== null ? Number(o.clipId) : null;
        ui.sel = []; ui.evSel = {}; ui.cursorBeat = o.startBeat !== undefined && o.startBeat !== null ? Number(o.startBeat) : null;
        ui.fitPending = true;
        if (shell && shell.showView && !visible()) shell.showView('editor');
        renderAll();
        if (ui.tab === 'events') loadEvents();
        loadNotes();
    }

    function wire() {
        el.root.addEventListener('click', function (e) {
            var b = e.target.closest('button');
            if (!b || !el.root.contains(b)) return;
            if (b.hasAttribute('data-ed-op')) return runOp(b.getAttribute('data-ed-op'), b);
            if (b.hasAttribute('data-ed-cmd')) return onCmd(b.getAttribute('data-ed-cmd'), b);
            if (b.hasAttribute('data-ed-tool')) return setTool(b.getAttribute('data-ed-tool'));
            if (b.hasAttribute('data-ed-tab')) return setTab(b.getAttribute('data-ed-tab'));
            if (b.hasAttribute('data-ed-ev')) return onEvAction(b.getAttribute('data-ed-ev'));
        });
        el.scroll.addEventListener('pointerdown', onDown);
        el.scroll.addEventListener('pointermove', onMove);
        el.scroll.addEventListener('pointerup', onUp);
        el.scroll.addEventListener('pointercancel', onUp);
        el.scroll.addEventListener('scroll', scheduleDraw, { passive: true });
        el.scroll.addEventListener('wheel', function (e) {
            if (!(e.ctrlKey || e.metaKey)) return;
            e.preventDefault();
            setZoom(e.shiftKey ? 'y' : 'x', e.deltaY < 0 ? 1 : -1);
        }, { passive: false });
        el.velLane.addEventListener('pointerdown', onVelDown);
        el.velLane.addEventListener('pointermove', onVelMove);
        el.velLane.addEventListener('pointerup', onVelUp);
        el.velLane.addEventListener('pointercancel', onVelUp);
        el.keys.addEventListener('pointerdown', function (e) {
            var p = local(e, el.keys);
            var was = ui.audition; ui.audition = true;
            preview(M.yToNote(view(), p.y), ui.vel);
            ui.audition = was;
        });
        el.ruler.addEventListener('click', function (e) {
            var p = local(e, el.ruler);
            ui.cursorBeat = M.snapBeat(M.xToBeat(view(), p.x), ui.snap);
            renderCommands(); scheduleDraw();
        });
        root.addEventListener('resize', function () { if (visible()) { scheduleDraw(); renderEvents(false); } });
        el.track.addEventListener('change', function () { open({ track: Number(el.track.value) }); });
        el.snap.addEventListener('change', function () { ui.snap = Number(el.snap.value); state.setPref('editor.snap', ui.snap); scheduleDraw(); });
        el.len.addEventListener('change', function () { ui.len = Number(el.len.value); state.setPref('editor.len', ui.len); });
        el.vel.addEventListener('change', function () {
            var v = Math.round(Number(el.vel.value));
            ui.vel = v >= 1 && v <= 127 ? v : 100;
            el.vel.value = String(ui.vel);
        });
        el.qStrength.addEventListener('input', function () { setText(el.qStrengthOut, el.qStrength.value + '%'); });
        el.qSwing.addEventListener('input', function () { setText(el.qSwingOut, el.qSwing.value + '%'); });
        el.evList.addEventListener('scroll', function () { renderEvents(false); }, { passive: true });
        el.evList.addEventListener('change', onEvChange);
        el.evFilter.addEventListener('change', function () { renderEvents(true); });
        el.evClipOnly.addEventListener('change', function () { renderEvents(true); });
        el.evAll.addEventListener('change', function () {
            ui.evSel = {};
            if (el.evAll.checked) ui.evRows.forEach(function (r) { ui.evSel[r.idx] = true; });
            renderEvents(false);
        });
        doc.body.addEventListener('keydown', onKeydown);

        state.subscribe('daw', function () {
            ui.lastDawSeq++;
            if (!visible()) return;
            renderHeader();
            renderCommands();
            var rev = daw().revision;
            if (ui.tab === 'notes' && rev !== ui.rev) scheduleLoad();
            else if (ui.tab === 'events' && rev !== ui.evRev) scheduleLoad();
            else scheduleDraw();
        });
        state.subscribe('transport', function (_v, _d, meta) {
            if (!visible()) return;
            if (ui.tab === 'notes') renderPlayhead();
            if (!(meta && meta.tick)) renderCommands();
        });
        doc.addEventListener('matsynth:viewchange', function (e) {
            if (!(e.detail && e.detail.view === 'editor')) { ui.drag = null; ui.pointers = {}; return; }
            if (!ui.loaded) ui.fitPending = true;
            root.requestAnimationFrame(function () { renderAll(); scheduleLoad(0); });
        });
    }

    function init() {
        el.root = $('ed-root');
        if (!el.root) return;
        ['track', 'title', 'play', 'undo', 'redo', 'multi', 'snap', 'len', 'vel', 'ruler', 'keys', 'grid', 'canvas',
            'playhead', 'scroll', 'spacer', 'quant', 'reason'].forEach(function (k) { el[k] = $('ed-' + k); });
        el.notesPane = $('ed-notes-pane'); el.eventsPane = $('ed-events-pane');
        el.rulerCanvas = $('ed-ruler-canvas'); el.rulerPlayhead = $('ed-ruler-playhead');
        el.keysCanvas = $('ed-keys-canvas'); el.velLane = $('ed-vel-lane'); el.velCanvas = $('ed-vel-canvas');
        el.selText = $('ed-sel-text');
        el.qGrid = $('ed-q-grid'); el.qStrength = $('ed-q-strength'); el.qSwing = $('ed-q-swing');
        el.qStrengthOut = $('ed-q-strength-out'); el.qSwingOut = $('ed-q-swing-out');
        el.evList = $('ed-ev-list'); el.evSpacer = $('ed-ev-spacer'); el.evFilter = $('ed-ev-filter');
        el.evClipOnly = $('ed-ev-clip-only'); el.evShift = $('ed-ev-shift'); el.evValue = $('ed-ev-value');
        el.evMode = $('ed-ev-mode'); el.evAll = $('ed-ev-all'); el.evSelText = $('ed-ev-sel-text'); el.evReason = $('ed-ev-reason');
        var named = el.root.querySelectorAll('.st-btn');
        for (var n = 0; n < named.length; n++) {
            if (named[n].hasAttribute('aria-label')) continue;
            var lab = named[n].querySelector('.st-btn-label');
            var txt = lab ? lab.textContent.trim() : named[n].getAttribute('title');
            if (txt) named[n].setAttribute('aria-label', txt);
        }
        units.fillSnapSelect(el.snap, ui.snap);
        units.fillSnapSelect(el.len, ui.len, LEN_OPTIONS);
        units.fillSnapSelect(el.qGrid, ui.snap > 0 ? ui.snap : 0.25, units.QUANT_OPTIONS);
        $('ed-audition').setAttribute('aria-pressed', String(ui.audition));
        setTool('select');
        wire();
        if (visible()) open({});
    }

    MS.editor = {
        open: open,
        // Diagnostica per i test in browser.
        debug: function () {
            return { track: ui.track, clipId: ui.clipId, rev: ui.rev, notes: ui.notes.length, sel: ui.sel.slice(), busy: ui.busy,
                tab: ui.tab, tool: ui.tool, ppb: ui.ppb, rowH: ui.rowH, cursorBeat: ui.cursorBeat, events: ui.events.length, evRev: ui.evRev };
        },
        notes: function () { return ui.notes.slice(); },
        view: function () { return view(); }
    };

    init();
})(window);
