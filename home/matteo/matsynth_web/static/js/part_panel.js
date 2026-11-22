/* MatSynth - part_panel.js
 * Pannello sonoro UNICO (schede Mix / Suono) per una parte, estratto da #panel-mixer di index.html.
 *   partPanel.mount(container, {partId, legacy?}) -> {setPart(id), destroy()}
 *  - legacy:true  -> stesso markup/ID/handler inline della pagina originale (usato da #panel-mixer-body di
 *    Studio: legacy_daw.js lo pilota con activeControlChannel/channelCC, comportamento invariato).
 *  - default      -> versione moderna per Suona/inspector: ID con prefisso univoco (nessun duplicato nel DOM),
 *    valori numerici editabili oltre agli slider, invio coalescato (max 1 ogni 40 ms per CC + valore finale
 *    al rilascio) come da contracts.md 9.1. `chan` di cc_update = ID PARTE (0-15).
 * Per estendere (scheda Routing, parti esterne) e' prevista SOLO la fase ui-live. */
(function (root, factory) {
    'use strict';
    var mod = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = mod;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.partPanel = mod;
    }
})(typeof window !== 'undefined' ? window : this, function (root) {
    'use strict';

    var THROTTLE_MS = 40;
    var uidCounter = 0;

    // CC sonori e relativi nomi (unico mapping, contracts.md 3.1). Il CC "sustain dell'inviluppo" non esiste.
    var MIX_CONTROLS = [
        { cc: 7, label: 'Volume', def: 100 },
        { cc: 10, label: 'Pan', def: 64 }
    ];
    var SOUND_CONTROLS = [
        { cc: 73, label: 'Attacco', short: 'ATK', icon: 'fa-arrow-up', def: 64 },
        { cc: 75, label: 'Decadimento', short: 'DEC', icon: 'fa-hourglass-half', def: 64 },
        { cc: 72, label: 'Rilascio', short: 'REL', icon: 'fa-arrow-down', def: 64 },
        { cc: 74, label: 'Cutoff', short: 'CUT', icon: 'fa-filter', def: 64 },
        { cc: 71, label: 'Risonanza', short: 'RES', icon: 'fa-wave-square', def: 64 }
    ];

    function clampCC(v) {
        var n = Math.round(Number(v));
        if (!isFinite(n)) return 0;
        return Math.max(0, Math.min(127, n));
    }

    // Stessa logica del pannello legacy (updatePanLabel).
    function panLabel(val) {
        var v = parseInt(val, 10);
        if (v < 60) return 'L' + (64 - v);
        if (v > 68) return 'R' + (v - 64);
        return 'C';
    }

    /* ── Markup legacy: identico a index.html originale (ID, classi, handler inline) ── */
    function legacyMarkup() {
        var sound = SOUND_CONTROLS.map(function (c) {
            return '' +
                '<div class="col">' +
                '<div class="vertical-slider-group">' +
                '<span id="val-' + c.cc + '" class="val-badge">64</span>' +
                '<div class="vertical-range-container">' +
                '<input type="range" class="form-range vertical-slider" id="cc-slider-' + c.cc + '" min="0" max="127" value="64" ' +
                'oninput="updateCC(' + c.cc + ', this.value); document.getElementById(\'val-' + c.cc + '\').innerText=this.value">' +
                '</div>' +
                '<label class="slider-label-vert"><i class="fas ' + c.icon + ' d-block mb-1"></i>&nbsp;' + c.short + '</label>' +
                '</div></div>';
        }).join('');
        return '' +
            '<div class="daw-panel-tabs">' +
            '<button class="daw-panel-tab active" data-tab="tab-mix" onclick="switchMixerTab(\'tab-mix\')"><i class="fas fa-sliders-h"></i> MIX</button>' +
            '<button class="daw-panel-tab" data-tab="tab-sound" onclick="switchMixerTab(\'tab-sound\')"><i class="fas fa-wave-square"></i> SOUND</button>' +
            '</div>' +
            '<div id="tab-mix" class="daw-tab-pane active">' +
            '<span id="tab-mix-inline-hint" class="daw-tab-inline-hint" style="display:none;"><i class="fas fa-info-circle"></i> Vol/Pan visibili inline nella track in modalità large</span>' +
            '<div class="mb-3 p-3" style="background: rgba(0, 255, 136, 0.08); border-radius: 10px; border: 1px solid rgba(0, 255, 136, 0.2);">' +
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
            '<label class="slider-label icon-label mb-0" style="color: #00ff88; font-size: 0.9rem;"><i class="fas fa-volume-up"></i> VOLUME (CC7)</label>' +
            '<span id="val-7" class="badge-custom" style="font-size: 0.9rem; padding: 4px 12px;">100</span></div>' +
            '<input type="range" class="form-range" id="vol-slider" min="0" max="127" value="100" ' +
            'oninput="updateCC(7, this.value); document.getElementById(\'val-7\').innerText=this.value" style="height: 8px;">' +
            '</div>' +
            '<div class="mb-3 p-3" style="background: rgba(0, 180, 255, 0.08); border-radius: 10px; border: 1px solid rgba(0, 180, 255, 0.2);">' +
            '<div class="d-flex justify-content-between align-items-center mb-2">' +
            '<label class="slider-label icon-label mb-0" style="color: #00b4ff; font-size: 0.9rem;"><i class="fas fa-arrows-alt-h"></i> PAN (CC10)</label>' +
            '<span id="val-10" class="badge-custom" style="font-size: 0.9rem; padding: 4px 12px;">C</span></div>' +
            '<input type="range" class="form-range" id="pan-slider" min="0" max="127" value="64" ' +
            'oninput="updateCC(10, this.value); updatePanLabel(this.value)" style="height: 8px;">' +
            '<div class="d-flex justify-content-between text-muted" style="font-size: 0.7rem; margin-top: 4px;"><span>L</span><span>C</span><span>R</span></div>' +
            '</div></div>' +
            '<div id="tab-sound" class="daw-tab-pane"><div class="row g-2">' + sound + '</div></div>';
    }

    /* ── Markup moderno ── */
    function rowMarkup(u, c, isPan) {
        var id = u + '-cc' + c.cc;
        return '' +
            '<div class="pp-row" data-cc="' + c.cc + '">' +
            '<label class="pp-label" for="' + id + '">' + c.label + ' <span class="pp-cc">CC' + c.cc + '</span></label>' +
            '<input type="range" class="pp-range" id="' + id + '" min="0" max="127" step="1" value="' + c.def + '" data-cc="' + c.cc + '">' +
            '<input type="number" class="pp-num" id="' + id + '-num" min="0" max="127" step="1" inputmode="numeric" ' +
            'value="' + c.def + '" data-cc="' + c.cc + '" aria-label="' + c.label + ' valore numerico (CC' + c.cc + ')">' +
            (isPan ? '<span class="pp-hint" id="' + id + '-hint" aria-hidden="true">C</span>' : '') +
            '</div>';
    }

    function modernMarkup(u) {
        return '' +
            '<div class="pp-tabs" role="tablist" aria-label="Controlli della parte">' +
            '<button type="button" class="pp-tab" role="tab" id="' + u + '-tab-mix" aria-controls="' + u + '-pane-mix" aria-selected="true" data-tab="mix">Mix</button>' +
            '<button type="button" class="pp-tab" role="tab" id="' + u + '-tab-sound" aria-controls="' + u + '-pane-sound" aria-selected="false" tabindex="-1" data-tab="sound">Suono</button>' +
            '</div>' +
            '<div class="pp-pane" role="tabpanel" id="' + u + '-pane-mix" aria-labelledby="' + u + '-tab-mix">' +
            MIX_CONTROLS.map(function (c) { return rowMarkup(u, c, c.cc === 10); }).join('') +
            '</div>' +
            '<div class="pp-pane" role="tabpanel" id="' + u + '-pane-sound" aria-labelledby="' + u + '-tab-sound" hidden>' +
            SOUND_CONTROLS.map(function (c) { return rowMarkup(u, c, false); }).join('') +
            '</div>' +
            '<p class="pp-status" role="status" aria-live="polite"></p>';
    }

    /* ── Montaggio ── */
    function mountLegacy(container, opts) {
        container.innerHTML = legacyMarkup();
        return {
            // In Studio la parte attiva e' decisa dal codice legacy (setControlChannel): qui solo inoltro.
            setPart: function (id) { if (typeof root.setControlChannel === 'function') root.setControlChannel(id); },
            destroy: function () { container.textContent = ''; }
        };
    }

    function mountModern(container, opts) {
        var ms = root.MatSynth || {};
        var api = ms.api, state = ms.state;
        var u = 'pp' + (++uidCounter);
        var partId = Number.isInteger(opts.partId) ? opts.partId : 0;
        container.innerHTML = modernMarkup(u);
        container.classList.add('pp');

        var statusEl = container.querySelector('.pp-status');
        var statusTimer = null;
        var lastSent = {};        // cc -> timestamp ultimo invio
        var pending = {};         // cc -> {part, val} da inviare (ultimo valore vince)
        var timers = {};
        var controls = {};        // cc -> {range, num, hint}
        MIX_CONTROLS.concat(SOUND_CONTROLS).forEach(function (c) {
            controls[c.cc] = {
                range: container.querySelector('#' + u + '-cc' + c.cc),
                num: container.querySelector('#' + u + '-cc' + c.cc + '-num'),
                hint: container.querySelector('#' + u + '-cc' + c.cc + '-hint'),
                def: c.def
            };
        });

        function say(msg) {
            statusEl.textContent = msg;
            clearTimeout(statusTimer);
            if (msg) statusTimer = setTimeout(function () { statusEl.textContent = ''; }, 4000);
        }

        function show(cc, val) {
            var c = controls[cc];
            if (!c) return;
            if (c.range.value !== String(val)) c.range.value = val;
            if (c.num.value !== String(val)) c.num.value = val;
            if (c.hint) c.hint.textContent = panLabel(val);
        }

        function refresh() {
            var part = state ? state.getPart(partId) : null;
            Object.keys(controls).forEach(function (cc) {
                var c = controls[cc];
                // Non sovrascrivere il controllo che l'utente sta toccando.
                if (document.activeElement === c.range || document.activeElement === c.num) return;
                var v = part && part.cc && part.cc[cc] !== undefined ? part.cc[cc] : c.def;
                show(cc, v);
            });
        }

        function sendNow(part, cc, val) {
            if (state) state.updatePartCC(part, cc, val, { origin: u });
            if (!api) return;
            // Socket connesso -> solo ack; altrimenti solo HTTP: mai entrambi (contracts 2.5).
            api.command('cc_update', { chan: part, cc: Number(cc), val: val },
                { method: 'GET', url: '/cc/' + part + '/' + cc + '/' + val }).then(function (r) {
                if (!r.ok) say(r.unknown_outcome ? 'Esito non confermato per CC' + cc : 'Invio non riuscito: ' + (r.error ? r.error.message : 'errore'));
            });
        }

        function fire(cc) {
            var p = pending[cc];
            if (!p) return;
            delete pending[cc];
            lastSent[cc] = Date.now();
            sendNow(p.part, cc, p.val);
        }

        function schedule(cc, val, flush) {
            pending[cc] = { part: partId, val: val };
            if (flush) { clearTimeout(timers[cc]); timers[cc] = null; fire(cc); return; }
            var wait = THROTTLE_MS - (Date.now() - (lastSent[cc] || 0));
            if (wait <= 0) fire(cc);
            else if (!timers[cc]) timers[cc] = setTimeout(function () { timers[cc] = null; fire(cc); }, wait);
        }

        function flushAll() {
            Object.keys(pending).forEach(function (cc) { clearTimeout(timers[cc]); timers[cc] = null; fire(cc); });
        }

        function onInput(e) {
            var t = e.target;
            if (!t.dataset || t.dataset.cc === undefined) return;
            var cc = t.dataset.cc;
            var val = clampCC(t.value);
            show(cc, val);
            // Il campo numerico invia solo a conferma (change), lo slider durante il trascinamento.
            if (t.type === 'range') schedule(cc, val, false);
        }

        function onChange(e) {
            var t = e.target;
            if (!t.dataset || t.dataset.cc === undefined) return;
            var cc = t.dataset.cc;
            var val = clampCC(t.value);
            show(cc, val);
            schedule(cc, val, true);   // valore finale garantito al rilascio / conferma numerica
        }

        function selectTab(name) {
            ['mix', 'sound'].forEach(function (n) {
                var tab = container.querySelector('#' + u + '-tab-' + n);
                var pane = container.querySelector('#' + u + '-pane-' + n);
                var on = n === name;
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
                pane.hidden = !on;
            });
        }

        function onClick(e) {
            var tab = e.target.closest ? e.target.closest('.pp-tab') : null;
            if (tab) selectTab(tab.dataset.tab);
        }

        function onKey(e) {
            if (!e.target.classList || !e.target.classList.contains('pp-tab')) return;
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                var next = e.target.dataset.tab === 'mix' ? 'sound' : 'mix';
                selectTab(next);
                container.querySelector('#' + u + '-tab-' + next).focus();
                e.preventDefault();
            }
        }

        container.addEventListener('input', onInput);
        container.addEventListener('change', onChange);
        container.addEventListener('click', onClick);
        container.addEventListener('keydown', onKey);

        var unsub = state ? state.subscribe('parts', function (value, domain, meta) {
            if (meta && meta.origin === u) return;
            if (meta && meta.part_id !== undefined && meta.part_id !== partId) return;
            refresh();
        }) : function () {};

        refresh();

        return {
            setPart: function (id) {
                if (!Number.isInteger(id) || id < 0 || id > 15) return;
                flushAll();            // eventuale ultimo valore ancora in coda va alla parte precedente
                partId = id;
                refresh();
            },
            destroy: function () {
                flushAll();
                container.removeEventListener('input', onInput);
                container.removeEventListener('change', onChange);
                container.removeEventListener('click', onClick);
                container.removeEventListener('keydown', onKey);
                unsub();
                clearTimeout(statusTimer);
                container.textContent = '';
                container.classList.remove('pp');
            }
        };
    }

    function mount(container, opts) {
        opts = opts || {};
        if (!container) throw new Error('partPanel.mount: container mancante');
        return opts.legacy ? mountLegacy(container, opts) : mountModern(container, opts);
    }

    return { mount: mount, panLabel: panLabel, clampCC: clampCC, THROTTLE_MS: THROTTLE_MS };
});
