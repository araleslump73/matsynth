/* MatSynth - live.js (fase ui-live)
 * Vista Suona: scena attuale (Salva / Salva come, indicatore "modificata"), preferite, ricerca, precedente/successiva,
 * selettore delle 16 parti, pannello sonoro unico (part_panel.js) e master interno (gain, reverb, chorus).
 * Smartphone in verticale per primo; tablet = stessa implementazione con tre colonne via CSS.
 *
 * Regole (piano sez. 4, 5, 10; contracts.md 9, 11.2):
 *  - Tutte le letture di rete (scene, strumenti, master) partono all'avvio e alla riconnessione, MAI in un
 *    gestore di cambio vista: showView non genera richieste. Un solo listener per evento, registrato una volta.
 *  - Con la vista nascosta (Studio) gli aggiornamenti di stato non toccano il DOM: si segnano "da aggiornare" e si
 *    ridisegna al ritorno (nessun lavoro inutile sul Pi, nessun reflow di elementi invisibili).
 *  - Gli elenchi si ridisegnano solo se la loro "chiave" cambia; il resto aggiorna solo testi che sono cambiati.
 *  - Slider master: valori coalescati (max 1 invio ogni 40 ms + valore finale), come gli slider delle parti.
 *  - Nessuna azione dipende da hover, tasto destro o pressione lunga; i bersagli toccabili sono >= 44 px (CSS).
 * Funzioni pure in testa (provate con node --test tests/js). */
(function (root, factory) {
    'use strict';
    var mod = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = mod;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.live = mod;
        mod.init(root.MatSynth);
    }
})(typeof window !== 'undefined' ? window : this, function (root) {
    'use strict';

    /* ══════════════════ Parte pura ══════════════════ */

    var MASTER = [
        { key: 'gain', label: 'Volume master', min: 0, max: 3, step: 0.1, def: 1.0, digits: 1 },
        { key: 'reverb.level', label: 'Reverb', min: 0, max: 1, step: 0.05, def: 0.4, digits: 2 },
        { key: 'chorus.level', label: 'Chorus', min: 0, max: 1, step: 0.05, def: 0.4, digits: 2 }
    ];

    function clampStep(def, v) {
        var n = Number(v);
        if (!isFinite(n)) return def.def;
        n = Math.max(def.min, Math.min(def.max, n));
        // arrotonda al passo per evitare 0.30000000000000004 nei valori inviati
        var steps = Math.round((n - def.min) / def.step);
        return Number((def.min + steps * def.step).toFixed(4));
    }

    function formatMaster(def, v) { return Number(v).toFixed(def.digits); }

    // Valori master da `/get_state` (chiavi di last_state.json). Chiave assente = valore predefinito + `known:false`.
    function masterFromState(st) {
        var out = {};
        MASTER.forEach(function (d) {
            var raw = st && st[d.key] !== undefined ? parseFloat(st[d.key]) : NaN;
            out[d.key] = isFinite(raw) ? { value: clampStep(d, raw), known: true } : { value: d.def, known: false };
        });
        return out;
    }

    // Riga dell'elenco parti: testo breve, mai nomi FluidSynth per una parte esterna (soundTitle lo garantisce).
    function partRowInfo(part, id, pp, instruments, runtime, conflicts) {
        var t = pp.soundTitle(part, instruments);
        var dest;
        if (!part) dest = 'non letta';
        else if (!part.destination) dest = 'Interno';
        else if (part.destination.kind === 'external') dest = (part.destination.device_alias || 'esterno') + ' · ch ' + ((part.destination.channel | 0) + 1);
        else dest = 'Interno · ch ' + ((part.destination.channel | 0) + 1);
        var notes = part ? pp.describeNotes(id, conflicts || [], runtime, part).filter(function (n) { return n.level === 'warn'; }).length : 0;
        return { name: t.name, dest: dest, warn: notes };
    }

    function nextPartId(id, dir) { return (id + (dir > 0 ? 1 : 15)) % 16; }

    function sceneKey(list, curId, favOnly) {
        return list.filter(function (s) { return !favOnly || s.favorite; })
            .map(function (s) { return s.id + '\u0001' + s.name + '\u0001' + (s.favorite ? 1 : 0) + (s.valid === false ? 'x' : '') + (s.id === curId ? 'c' : ''); }).join('\u0002');
    }

    /* ══════════════════ DOM ══════════════════ */
    function init(MS) {
        var doc = root.document;
        var api = MS.api, state = MS.state, pp = MS.partPanel;
        if (!doc || !api || !state || !pp) return;
        var view = doc.getElementById('view-live');
        var rootEl = doc.getElementById('lv-root');
        if (!view || !rootEl) return;
        var scenes = MS.scenes;          // library_scenes.js (stesso partial, caricato prima)
        var tools = MS.sceneTools;
        if (!scenes || !tools) { if (typeof console !== 'undefined') console.error('[live] servizio scene mancante'); return; }

        function $(id) { return doc.getElementById(id); }
        function setText(n, t) { if (n && n.textContent !== t) n.textContent = t; }
        function h(tag, attrs, kids) {
            var n = doc.createElement(tag);
            if (attrs) Object.keys(attrs).forEach(function (k) {
                var v = attrs[k];
                if (v === null || v === undefined || v === false) return;
                if (k === 'class') n.className = v; else if (k === 'text') n.textContent = v; else n.setAttribute(k, v === true ? '' : String(v));
            });
            (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === 'string' ? doc.createTextNode(c) : c); });
            return n;
        }
        function visible() { return !view.hidden; }

        var selected = (function () {
            var p = state.getPref('livePart', 0);
            return Number.isInteger(p) && p >= 0 && p <= 15 ? p : 0;
        })();
        var stale = true;                 // ridisegno necessario al prossimo mostrarsi della vista

        /* ── Scena ── */
        var els = {
            name: $('lv-scene-name'), dirty: $('lv-scene-dirty'), save: $('lv-save'), saveAs: $('lv-saveas'),
            note: $('lv-transport-note'), stop: $('lv-stop'), prev: $('lv-prev'), next: $('lv-next'),
            search: $('lv-scene-search'), info: $('lv-scene-info'), results: $('lv-scene-results'), favs: $('lv-favs')
        };
        var keys = { results: '', favs: '' };

        function sceneButton(sc, cur) {
            var isCur = cur && cur.scene_id === sc.id;
            return h('li', null, [h('button', { type: 'button', class: 'lv-btn lv-scenebtn' + (isCur ? ' is-current' : ''), 'data-scene': sc.id,
                'aria-current': isCur ? 'true' : null, disabled: sc.valid === false }, [
                isCur ? h('span', { class: 'lv-mark', 'aria-hidden': 'true', text: '▶' }) : null,
                h('span', { class: 'lv-scenebtn-name', text: sc.name }),
                isCur ? h('small', { text: 'attuale' }) : null])]);
        }

        function drawScenes() {
            var cur = state.get('scene') || {};
            var tr = state.get('transport');
            setText(els.name, cur.name ? cur.name : 'Nessuna scena caricata');
            els.dirty.hidden = !cur.dirty;
            var busy = scenes.isBusy();
            els.save.disabled = busy; els.saveAs.disabled = busy;
            els.save.hidden = !cur.scene_id;      // senza scena corrente c'e' solo "Salva come…"
            els.note.hidden = !tools.transportBusy(tr);
            var list = scenes.list();
            var prev = tools.neighborScene(list, cur.scene_id, -1), next = tools.neighborScene(list, cur.scene_id, 1);
            els.prev.disabled = busy || !prev;
            els.next.disabled = busy || !next;
            var q = els.search.value.trim();
            var shown = q ? tools.filterScenes(list, q).slice(0, 8) : [];
            els.results.hidden = !q;
            var rk = q + '|' + sceneKey(shown, cur.scene_id, false);
            if (rk !== keys.results) {
                keys.results = rk;
                els.results.textContent = '';
                shown.forEach(function (s) { els.results.appendChild(sceneButton(s, cur)); });
            }
            var favs = tools.favoriteScenes(list);
            var fk = sceneKey(favs, cur.scene_id, true);
            if (fk !== keys.favs) {
                keys.favs = fk;
                els.favs.textContent = '';
                favs.forEach(function (s) { els.favs.appendChild(sceneButton(s, cur)); });
            }
            setText(els.info, scenes.loadError() ? scenes.loadError() :
                !scenes.isLoaded() ? 'Lettura delle scene…' :
                !list.length ? 'Nessuna scena salvata: premi «Salva come…» per creare la prima.' :
                q ? (shown.length ? (tools.filterScenes(list, q).length > shown.length ? 'Prime ' + shown.length + ' corrispondenze: restringi la ricerca o apri la Libreria.' : shown.length + (shown.length === 1 ? ' scena trovata' : ' scene trovate')) : 'Nessuna scena corrisponde.') :
                !favs.length ? 'Nessuna preferita: in Libreria premi «Preferita?» su una scena.' : '');
            els.favs.hidden = !!q || !favs.length;
            $('lv-fav-title').hidden = !!q || !favs.length;
        }

        function recallScene(id) { return scenes.recall(id); }
        function stepScene(dir) {
            var cur = state.get('scene') || {};
            var sc = tools.neighborScene(scenes.list(), cur.scene_id, dir);
            if (sc) recallScene(sc.id);
        }

        rootEl.addEventListener('click', function (e) {
            var t = e.target.closest ? e.target.closest('button') : null;
            if (!t) return;
            if (t.hasAttribute('data-scene')) { recallScene(t.getAttribute('data-scene')); return; }
            if (t === els.save) { scenes.saveCurrent(); return; }
            if (t === els.saveAs) { scenes.saveAs(''); return; }
            if (t === els.stop) { scenes.stopTransport(); return; }
            if (t === els.prev) { stepScene(-1); return; }
            if (t === els.next) { stepScene(1); return; }
        });
        els.search.addEventListener('input', function () { if (visible()) drawScenes(); });

        /* ── Parti ── */
        var partList = $('lv-partlist');
        var partsBox = $('lv-parts');
        var toggle = $('lv-part-toggle');
        var rows = [];
        for (var i = 0; i < 16; i++) {
            var name = h('strong', { class: 'lv-part-name' });
            var dest = h('small', { class: 'lv-part-dest' });
            var flag = h('span', { class: 'lv-part-flag', hidden: true, title: 'Avvisi di routing: apri la scheda Routing della parte' }, [h('span', { 'aria-hidden': 'true', text: '⚠' }), h('span', { class: 'lv-sr', text: ' avvisi' })]);
            var btn = h('button', { type: 'button', class: 'lv-part', 'data-part': i, 'aria-pressed': 'false' }, [
                h('span', { class: 'lv-part-n', text: String(i + 1) }), h('span', { class: 'lv-part-text' }, [name, dest]), flag]);
            partList.appendChild(h('li', null, [btn]));
            rows.push({ btn: btn, name: name, dest: dest, flag: flag });
        }

        function drawParts() {
            var partsState = state.get('parts');
            var conflicts = partsState && partsState.conflicts ? partsState.conflicts : [];
            var inst = pp.instruments.loaded ? pp.instruments.list : null;
            for (var k = 0; k < 16; k++) {
                var info = partRowInfo(state.getPart(k), k, pp, inst, pp.runtimeFor(k), conflicts);
                var r = rows[k];
                setText(r.name, info.name);
                setText(r.dest, info.dest);
                r.flag.hidden = info.warn === 0;
                var on = k === selected;
                if (r.btn.getAttribute('aria-pressed') !== (on ? 'true' : 'false')) {
                    r.btn.setAttribute('aria-pressed', on ? 'true' : 'false');
                    r.btn.classList.toggle('is-selected', on);
                    if (on) r.btn.setAttribute('aria-current', 'true'); else r.btn.removeAttribute('aria-current');
                }
            }
            var sel = rows[selected];
            setText($('lv-part-current'), 'Parte ' + (selected + 1) + ' · ' + sel.name.textContent);
        }

        var panel = pp.mount($('lv-part-panel'), { partId: selected });

        function selectPart(id) {
            if (!Number.isInteger(id) || id < 0 || id > 15) return;
            selected = id;
            state.setPref('livePart', id);       // la selezione sopravvive a orientamento, cambio vista e ricarica
            panel.setPart(id);
            drawParts();
            partsBox.classList.remove('is-open');
            toggle.setAttribute('aria-expanded', 'false');
        }
        partList.addEventListener('click', function (e) {
            var b = e.target.closest ? e.target.closest('.lv-part') : null;
            if (b) selectPart(parseInt(b.getAttribute('data-part'), 10));
        });
        $('lv-part-prev').addEventListener('click', function () { selectPart(nextPartId(selected, -1)); });
        $('lv-part-next').addEventListener('click', function () { selectPart(nextPartId(selected, 1)); });
        toggle.addEventListener('click', function () {
            var open = !partsBox.classList.contains('is-open');
            partsBox.classList.toggle('is-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        });

        /* ── Master interno ── */
        var masterHost = $('lv-master-rows');
        var masterStatus = $('lv-master-status');
        var masterEls = {};
        var masterTimer = null;
        var masterDrag = {};
        function masterSay(msg, kind) {
            masterStatus.setAttribute('data-kind', kind || 'info');
            masterStatus.textContent = msg ? (kind === 'error' ? 'Errore: ' : kind === 'warn' ? 'Attenzione: ' : '') + msg : '';
            root.clearTimeout(masterTimer);
            if (msg && kind !== 'error' && kind !== 'warn') masterTimer = root.setTimeout(function () { masterStatus.textContent = ''; }, 4000);
        }
        var masterValues = masterFromState(null);
        var coal = pp.createCoalescer(function (key, val) {
            api.command('effect_update', { type: key, val: val }, { method: 'GET', url: '/set_effect/' + encodeURIComponent(key) + '/' + val }).then(function (r) {
                if (!r.ok) masterSay(pp.errorText(r), 'error');
            });
        }, pp.THROTTLE_MS);

        MASTER.forEach(function (d, idx) {
            var id = 'lv-m' + idx;
            var range = h('input', { type: 'range', class: 'pp-range pp-range-xl', id: id, min: d.min, max: d.max, step: d.step, value: d.def, 'data-eff': d.key });
            var num = h('input', { type: 'number', class: 'pp-num', id: id + '-num', min: d.min, max: d.max, step: d.step, inputmode: 'decimal', value: formatMaster(d, d.def), 'data-eff': d.key, 'aria-label': d.label + ' valore numerico' });
            var unk = h('span', { class: 'pp-unsent', hidden: true, text: 'non letto' });
            masterHost.appendChild(h('div', { class: 'pp-row pp-row-xl' }, [h('label', { class: 'pp-label', for: id, text: d.label + ' ' }, [unk]), num, range]));
            masterEls[d.key] = { range: range, num: num, unk: unk, def: d };
        });
        function showMaster(key, value, known) {
            var m = masterEls[key];
            var s = String(value);
            if (m.range.value !== s) m.range.value = s;
            var t = formatMaster(m.def, value);
            if (m.num.value !== t) m.num.value = t;
            m.unk.hidden = known !== false;
        }
        function drawMaster() {
            MASTER.forEach(function (d) {
                var m = masterEls[d.key];
                if (masterDrag[d.key] || doc.activeElement === m.range || doc.activeElement === m.num) return;
                var v = masterValues[d.key];
                showMaster(d.key, v.value, v.known);
            });
        }
        masterHost.addEventListener('input', function (e) {
            var t = e.target;
            if (t.type !== 'range' || !t.dataset.eff) return;
            var m = masterEls[t.dataset.eff];
            var v = clampStep(m.def, t.value);
            masterValues[t.dataset.eff] = { value: v, known: true };
            showMaster(t.dataset.eff, v, true);
            coal.push(t.dataset.eff, v, null, false);
        });
        masterHost.addEventListener('change', function (e) {
            var t = e.target;
            if (!t.dataset.eff) return;
            masterDrag[t.dataset.eff] = false;
            if (t.type === 'number' && String(t.value).trim() === '') { drawMaster(); return; }
            var m = masterEls[t.dataset.eff];
            var v = clampStep(m.def, t.value);
            masterValues[t.dataset.eff] = { value: v, known: true };
            showMaster(t.dataset.eff, v, true);
            coal.push(t.dataset.eff, v, null, true);
        });
        masterHost.addEventListener('pointerdown', function (e) { if (e.target.dataset && e.target.dataset.eff && e.target.type === 'range') masterDrag[e.target.dataset.eff] = true; });
        function endDrag() { masterDrag = {}; }
        masterHost.addEventListener('pointerup', endDrag);
        masterHost.addEventListener('pointercancel', endDrag);
        masterHost.addEventListener('lostpointercapture', endDrag);

        function loadMaster() {
            api.request('GET', '/get_state').then(function (r) {
                if (!r.ok || !r.body || typeof r.body !== 'object') { masterSay('Valori master non letti: ' + pp.errorText(r), 'warn'); return; }
                masterValues = masterFromState(r.body);
                drawMaster();
            });
        }

        /* ── Aggiornamenti ── */
        function redrawAll() {
            if (!visible()) { stale = true; return; }
            stale = false;
            drawScenes(); drawParts(); drawMaster();
            panel.refresh();
        }

        // Iscrizioni: una sola volta, per tutta la vita della pagina (la vista Suona non viene mai distrutta).
        state.subscribe('scene', function () { if (visible()) drawScenes(); else stale = true; });
        state.subscribe('transport', function () { if (visible()) drawScenes(); else stale = true; });
        state.subscribe('parts', function () { if (visible()) drawParts(); else stale = true; });
        scenes.subscribe(function () { if (visible()) drawScenes(); else stale = true; });
        pp.onStore('instruments', function () { if (visible()) drawParts(); else stale = true; });
        pp.instruments.load(false);
        // Gli elenchi strumenti cambiano solo dopo un caricamento SoundFont: si ridisegna il nome dei suoni.
        var lastConnected = state.get('connection').connected;
        state.subscribe('connection', function (c) {
            if (c.connected && !lastConnected) {
                // Riconnessione (anche primo collegamento): si rileggono i dati propri della vista, una volta.
                scenes.refresh(); loadMaster(); pp.instruments.load(true);
            }
            lastConnected = c.connected;
        });
        doc.addEventListener('matsynth:viewchange', function (e) {
            // Solo ridisegno locale dai dati gia' in memoria: nessuna richiesta al cambio vista.
            if (e.detail && e.detail.view === 'live' && stale) redrawAll();
        });

        scenes.refresh();
        loadMaster();
        redrawAll();

        MS.live.selectPart = selectPart;
    }

    return {
        init: init, MASTER: MASTER, clampStep: clampStep, formatMaster: formatMaster, masterFromState: masterFromState,
        partRowInfo: partRowInfo, nextPartId: nextPartId, sceneKey: sceneKey
    };
});
