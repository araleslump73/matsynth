/* MatSynth - part_panel.js
 * Pannello sonoro UNICO per una parte, estratto da #panel-mixer di index.html (ui-shell) ed esteso da ui-live.
 *   partPanel.mount(container, {partId, legacy?}) -> {setPart(id), refresh(), destroy()}
 *  - legacy:true  -> stesso markup/ID/handler inline della pagina originale (usato da #panel-mixer-body di
 *    Studio: legacy_daw.js lo pilota con activeControlChannel/channelCC, comportamento invariato).
 *  - default      -> versione moderna per Suona/inspector: ID con prefisso univoco (nessun duplicato nel DOM),
 *    testata (nome, destinazione, suono, volume grande), schede Suono | Mix | Routing, valori numerici
 *    editabili oltre agli slider, invio coalescato (max 1 ogni 40 ms per CC + valore finale al rilascio)
 *    come da contracts.md 9.1. `chan` di cc_update = ID PARTE (0-15).
 *  - Parte esterna: nome utente + Bank MSB/LSB/Program (vuoto = non inviato), e SOLO i CC dichiarati in
 *    `controls` del dispositivo (3.4); nessun nome FluidSynth mostrato come se fosse dello strumento.
 *  - Avvisi: conflitti (3.6), stato porte, MONITOR_HANDLED_BY_ALSA (legacy).
 * Perche' la struttura (righe di slider) si ridisegna SOLO quando cambia la "firma" (tipo di destinazione,
 * alias, controlli dichiarati, parte): `parts_update` arriva fino a 10 Hz e ricostruire il DOM a ogni evento
 * sarebbe reflow continuo; di norma si aggiornano solo testi/valori che sono davvero cambiati. */
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
    var ALL_CC = MIX_CONTROLS.concat(SOUND_CONTROLS);
    var CC_DEF = {};
    ALL_CC.forEach(function (c) { CC_DEF[c.cc] = c; });

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

    /* ══════════════════ Parte pura (provata con node --test) ══════════════════ */

    function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

    function norm(s) {
        var t = String(s === undefined || s === null ? '' : s).toLowerCase();
        return typeof t.normalize === 'function' ? t.normalize('NFD').replace(/[̀-ͯ]/g, '') : t;
    }

    // `/get_instruments` risponde con righe "000-005 Nome" (bank-program Nome). Le righe non conformi si scartano.
    function parseInstruments(lines) {
        var out = [];
        (Array.isArray(lines) ? lines : []).forEach(function (s) {
            var m = /^\s*(\d+)\s*-\s*(\d+)\s+(.+?)\s*$/.exec(String(s));
            if (m) out.push({ bank: parseInt(m[1], 10), program: parseInt(m[2], 10), name: m[3] });
        });
        return out;
    }

    // Famiglie General MIDI (program / 8). Sui SoundFont non GM sono solo indicative; bank >= 120 = batterie.
    var CATEGORIES = [
        { id: 'piano', label: 'Piano' }, { id: 'chrom', label: 'Percussioni cromatiche' }, { id: 'organ', label: 'Organi' },
        { id: 'guitar', label: 'Chitarre' }, { id: 'bass', label: 'Bassi' }, { id: 'strings', label: 'Archi' },
        { id: 'ensemble', label: 'Ensemble e cori' }, { id: 'brass', label: 'Ottoni' }, { id: 'reed', label: 'Ance' },
        { id: 'pipe', label: 'Flauti' }, { id: 'lead', label: 'Synth lead' }, { id: 'pad', label: 'Synth pad' },
        { id: 'sfx', label: 'Synth FX' }, { id: 'ethnic', label: 'Etnici' }, { id: 'perc', label: 'Percussivi' },
        { id: 'effects', label: 'Effetti sonori' }, { id: 'drums', label: 'Batterie' }
    ];
    function instrumentCategory(it) {
        if (!it || !Number.isInteger(it.program)) return null;
        if (it.bank >= 120) return 'drums';
        var f = Math.floor(it.program / 8);
        return f >= 0 && f < 16 ? CATEGORIES[f].id : null;
    }
    // Categorie presenti nell'elenco, nell'ordine GM, con il numero di strumenti.
    function instrumentCategories(list) {
        var n = {};
        (list || []).forEach(function (it) { var c = instrumentCategory(it); if (c) n[c] = (n[c] || 0) + 1; });
        return CATEGORIES.filter(function (c) { return n[c.id]; }).map(function (c) { return { id: c.id, label: c.label, count: n[c.id] }; });
    }

    // Ricerca per parole (tutte presenti), senza maiuscole/accenti, anche su "bank-program"; `category` opzionale.
    function filterInstruments(list, query, limit, category) {
        var words = norm(query).split(/\s+/).filter(Boolean);
        var out = [], total = 0;
        for (var i = 0; i < list.length; i++) {
            var it = list[i];
            if (category && instrumentCategory(it) !== category) continue;
            var hay = norm(it.name) + ' ' + it.bank + '-' + it.program + ' ' + it.bank + ' ' + it.program;
            var ok = true;
            for (var w = 0; w < words.length; w++) if (hay.indexOf(words[w]) < 0) { ok = false; break; }
            if (!ok) continue;
            total += 1;
            if (!limit || out.length < limit) out.push(it);
        }
        return { items: out, total: total };
    }

    function findInstrument(list, bank, program) {
        for (var i = 0; i < list.length; i++) if (list[i].bank === bank && list[i].program === program) return list[i];
        return null;
    }

    // Campo MIDI 0..127 oppure vuoto (= null = "non inviare quel messaggio", 3.1).
    function parseMidiField(text) {
        var t = String(text === undefined || text === null ? '' : text).trim();
        if (t === '') return { value: null, error: null };
        if (!/^\d{1,3}$/.test(t) || parseInt(t, 10) > 127) return { value: null, error: 'Inserisci un numero da 0 a 127, oppure lascia vuoto' };
        return { value: parseInt(t, 10), error: null };
    }

    function isExternal(part) { return !!(part && part.destination && part.destination.kind === 'external'); }

    // CC mostrabili: interna = tutti; esterna = solo quelli dichiarati dal dispositivo (nessuno se non dichiara nulla).
    function allowedControls(part, device) {
        if (!isExternal(part)) return ALL_CC.map(function (c) { return c.cc; });
        var declared = device && Array.isArray(device.controls) ? device.controls : [];
        var out = [];
        ALL_CC.forEach(function (c) { if (declared.indexOf(String(c.cc)) >= 0) out.push(c.cc); });
        return out;
    }

    function destinationLabel(part, device) {
        if (!part) return 'Destinazione sconosciuta';
        if (!part.destination) return 'Interno (FluidSynth)';
        var ch = (part.destination.channel | 0) + 1;
        if (part.destination.kind === 'external') {
            var alias = part.destination.device_alias;
            var name = device && device.label ? device.label : alias;
            return 'Esterno: ' + (name || 'nessuno strumento') + ' · canale ' + ch;
        }
        return 'Interno (FluidSynth) · canale ' + ch;
    }

    // Nome del suono per titolo: esterna = solo nome utente (mai nomi FluidSynth); interna = nome da elenco o bank/programma.
    function soundTitle(part, instruments) {
        if (!part || !part.sound) return { name: 'Suono non letto', detail: '' };
        var s = part.sound;
        if (isExternal(part)) {
            var bits = [];
            if (s.bank_msb !== null && s.bank_msb !== undefined) bits.push('MSB ' + s.bank_msb);
            if (s.bank_lsb !== null && s.bank_lsb !== undefined) bits.push('LSB ' + s.bank_lsb);
            if (s.program !== null && s.program !== undefined) bits.push('Programma ' + s.program);
            return { name: s.name || 'Suono non impostato', detail: bits.length ? bits.join(' · ') : 'Nessun bank/programma da inviare' };
        }
        var inst = instruments ? findInstrument(instruments, s.bank, s.program) : null;
        var name = s.name || (inst ? inst.name : null) || ('Bank ' + s.bank + ' · Programma ' + s.program);
        return { name: name, detail: 'Bank ' + s.bank + ' · Programma ' + s.program };
    }

    var STATUS_TEXT = { available: 'disponibile', missing: 'assente', ambiguous: 'ambigua', unbound: 'non associato', offline: 'errore' };
    function statusText(st) { return STATUS_TEXT[st] || (st ? String(st) : 'sconosciuto'); }

    function partNames(ids, except) {
        return ids.filter(function (i) { return i !== except; }).map(function (i) { return 'parte ' + (i + 1); }).join(', ');
    }

    // Avvisi per una parte da `conflicts` (3.6) e `runtime` (3.5). Livello 'warn' = serve attenzione.
    function describeNotes(partId, conflicts, runtime, part) {
        var notes = [];
        (Array.isArray(conflicts) ? conflicts : []).forEach(function (c) {
            if (!c || !Array.isArray(c.parts) || c.parts.indexOf(partId) < 0) return;
            var others = partNames(c.parts, partId);
            if (c.type === 'shared_destination') notes.push({ level: 'warn', code: 'shared_destination', text: 'Destinazione condivisa con ' + others + ': cambiare suono o controlli dell\'una cambia anche l\'altra. Non c\'è isolamento audio.' });
            else if (c.type === 'shared_input') notes.push({ level: 'warn', code: 'shared_input', text: 'Lo stesso ingresso arriva anche a ' + others + ': se le tracce sono armate registrano entrambe.' });
            else if (c.type === 'double_monitor') notes.push({ level: 'warn', code: 'double_monitor', text: 'Monitor doppio con ' + others + ': ogni nota suona due volte sulla stessa destinazione.' });
        });
        if (runtime) {
            if (runtime.destination_status && runtime.destination_status !== 'available') {
                notes.push({ level: 'warn', code: 'destination_status', text: 'Destinazione ' + statusText(runtime.destination_status) + ': le modifiche vengono salvate ma non inviate.' });
            }
            if (part && part.input && part.input.device_alias && runtime.input_status && runtime.input_status !== 'available') {
                notes.push({ level: 'warn', code: 'input_status', text: 'Ingresso ' + statusText(runtime.input_status) + ': questa parte non riceve eventi dalla tastiera.' });
            }
            (Array.isArray(runtime.warnings) ? runtime.warnings : []).forEach(function (w) {
                if (!w) return;
                if (w.code === 'MONITOR_HANDLED_BY_ALSA') {
                    var plays = w.details && w.details.alsa_plays_on;
                    if (plays) {
                        notes.push({ level: 'warn', code: w.code, text: 'Modalità legacy: l\'ingresso è collegato direttamente a FluidSynth (ALSA) e suona sempre sul canale interno ' + ((plays.channel | 0) + 1) + ', anche se la destinazione di questa parte è un\'altra. L\'opzione Monitor non ha effetto.' });
                    } else {
                        notes.push({ level: 'info', code: w.code, text: 'Modalità legacy: il monitor dell\'ingresso è gestito da ALSA; l\'opzione Monitor non ha effetto.' });
                    }
                } else if (w.message) {
                    notes.push({ level: 'info', code: w.code || 'warning', text: w.message });
                }
            });
        }
        return notes;
    }

    // Bozza del routing -> PATCH con soli gruppi cambiati (input/destination sostituiscono l'intero sotto-oggetto).
    function buildRoutingPatch(part, draft) {
        var patch = {};
        var cur = part || {};
        var curIn = cur.input || { device_alias: null, channel: null };
        if (draft.inAlias !== curIn.device_alias || draft.inChannel !== curIn.channel) {
            patch.input = { device_alias: draft.inAlias, channel: draft.inChannel };
        }
        var curDest = cur.destination || { kind: 'internal', device_alias: null, channel: 0 };
        var alias = draft.kind === 'external' ? draft.destAlias : null;
        if (draft.kind !== curDest.kind || alias !== curDest.device_alias || draft.destChannel !== curDest.channel) {
            patch.destination = { kind: draft.kind, device_alias: alias, channel: draft.destChannel };
        }
        if (draft.monitor !== !!cur.monitor) patch.monitor = draft.monitor;
        return patch;
    }

    function draftFromPart(part) {
        var inp = part && part.input ? part.input : { device_alias: null, channel: null };
        var dest = part && part.destination ? part.destination : { kind: 'internal', device_alias: null, channel: 0 };
        return { inAlias: inp.device_alias, inChannel: inp.channel, kind: dest.kind, destAlias: dest.device_alias,
                 destChannel: dest.channel, monitor: !!(part && part.monitor) };
    }

    // Messaggi d'errore in italiano per codice (2.3). `extra` = mappa codice -> testo specifica del chiamante.
    var ERROR_TEXT = {
        BAD_REQUEST: 'Richiesta non valida.',
        VALIDATION_FAILED: 'Valore non valido.',
        NOT_FOUND: 'Elemento non trovato.',
        NAME_EXISTS: 'Esiste già un elemento con questo nome.',
        TRANSPORT_ACTIVE: 'Play o Record sono attivi: fermali prima.',
        RECORDING_ACTIVE: 'Operazione non consentita durante la registrazione.',
        SYNTH_UNAVAILABLE: 'FluidSynth non raggiungibile.',
        PORT_UNAVAILABLE: 'Porta MIDI non disponibile.',
        NOT_INTERNAL_PART: 'Comando valido solo per le parti interne.',
        SOUNDFONT_LOADING: 'Un SoundFont è in caricamento: attendi la fine.',
        UNSAVED_CHANGES: 'Ci sono modifiche non salvate.',
        OPERATION_IN_PROGRESS: 'Un\'altra operazione su progetto o scena è in corso: riprova tra poco.',
        DISK_FULL: 'Disco pieno.',
        DISK_ERROR: 'Errore di scrittura su disco.',
        INTERNAL: 'Errore interno del server.'
    };
    function errorText(r, extra) {
        if (!r) return 'Errore sconosciuto';
        if (r.unknown_outcome) return 'Esito non confermato: ' + (r.error && r.error.message ? r.error.message : 'nessuna risposta') + '. Controlla lo stato prima di riprovare.';
        var e = r.error || {};
        var base = (extra && extra[e.code]) || ERROR_TEXT[e.code];
        var msg = e.message || '';
        if (e.code === 'VALIDATION_FAILED' && e.details && e.details.path) msg = (msg ? msg + ' ' : '') + '(' + e.details.path + ')';
        if (base && msg && msg !== base) return base + ' ' + msg;
        return base || msg || 'Errore';
    }

    /* Coalescer: per ogni chiave tiene solo l'ultimo valore e lo invia al massimo ogni `wait` ms; `flush`
     * invia subito il valore finale. Perche': uno slider produce un evento per pixel, il server ne vuole uno
     * ogni tanto + quello finale (niente scritture a ogni movimento, piano sez. 10). Una sola leva temporale
     * per chiave, nessun timer periodico. */
    function createCoalescer(send, wait, now, setT, clearT) {
        now = now || function () { return Date.now(); };
        setT = setT || function (fn, ms) { return setTimeout(fn, ms); };
        clearT = clearT || function (t) { clearTimeout(t); };
        var last = {}, pending = {}, timers = {};
        function fire(key) {
            if (!pending[key]) return;
            var p = pending[key];
            delete pending[key];
            last[key] = now();
            send(key, p.value, p.meta);
        }
        return {
            push: function (key, value, meta, flushNow) {
                pending[key] = { value: value, meta: meta };
                if (flushNow) { clearT(timers[key]); timers[key] = null; fire(key); return; }
                var w = wait - (now() - (last[key] || 0));
                if (w <= 0) fire(key);
                else if (!timers[key]) timers[key] = setT(function () { timers[key] = null; fire(key); }, w);
            },
            flush: function () {
                Object.keys(pending).forEach(function (k) { clearT(timers[k]); timers[k] = null; fire(k); });
            },
            hasPending: function () { return Object.keys(pending).length > 0; }
        };
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

    function mountLegacy(container, opts) {
        container.innerHTML = legacyMarkup();
        return {
            // In Studio la parte attiva e' decisa dal codice legacy (setControlChannel): qui solo inoltro.
            setPart: function (id) { if (typeof root.setControlChannel === 'function') root.setControlChannel(id); },
            refresh: function () {},
            destroy: function () { container.textContent = ''; }
        };
    }

    /* ══════════════════ Archivi condivisi (una sola copia per pagina) ══════════════════
     * Elenco strumenti del SoundFont e registro dispositivi servono a tutti i pannelli montati: si leggono una
     * volta e solo su richiesta esplicita o al cambio di SoundFont/porte, mai a ogni cambio vista. */
    var listeners = { instruments: [], devices: [] };
    function emit(kind) { listeners[kind].slice().forEach(function (fn) { try { fn(); } catch (e) { /* un pannello non blocca gli altri */ } }); }
    function onStore(kind, fn) {
        listeners[kind].push(fn);
        return function () { var i = listeners[kind].indexOf(fn); if (i >= 0) listeners[kind].splice(i, 1); };
    }

    var sfLoadSeq = 0;               // cresce a ogni stato 'loading' del SoundFont visto dal client
    var instrumentsStore = {
        list: [], loaded: false, loading: false, error: null, forFile: null, promise: null,
        load: function (force) {
            var ms = root.MatSynth || {};
            if (!ms.api) return Promise.resolve([]);
            if (this.loading) return this.promise;
            if (this.loaded && !force) return Promise.resolve(this.list);
            var self = this;
            this.loading = true;
            var sf0 = ms.state ? ms.state.get('soundfont') : null;
            var file0 = sf0 && sf0.state === 'loaded' ? sf0.file : null;
            var seq0 = sfLoadSeq;
            this.promise = ms.api.request('GET', '/get_instruments').then(function (r) {
                self.loading = false;
                var sf = ms.state ? ms.state.get('soundfont') : null;
                if (r.ok && Array.isArray(r.body)) {
                    self.list = parseInstruments(r.body);
                    // Elenco vuoto (synth occupato, SoundFont in caricamento): non conta come letto, si riprova
                    // alla prossima apertura del browser o quando il SoundFont risulta caricato.
                    self.loaded = self.list.length > 0;
                    self.error = self.list.length ? null : 'Nessuno strumento ricevuto: SoundFont non caricato o synth occupato. Riprova tra poco.';
                    // L'elenco e' legato al file solo se nessun caricamento SoundFont e' iniziato durante la richiesta.
                    var same = seq0 === sfLoadSeq && (!file0 || (sf && sf.file === file0));
                    self.forFile = self.loaded && same && sf && sf.state === 'loaded' && sf.file ? sf.file : null;
                } else {
                    self.loaded = false;
                    self.error = 'Elenco strumenti non letto: ' + errorText(r);
                }
                emit('instruments');
                return self.list;
            });
            return this.promise;
        },
        invalidate: function () { this.loaded = false; this.list = []; this.forFile = null; }
    };

    var devicesStore = {
        list: [], loaded: false, loading: false, error: null, timer: null,
        get: function (alias) {
            for (var i = 0; i < this.list.length; i++) if (this.list[i].alias === alias) return this.list[i];
            return null;
        },
        load: function (force) {
            var ms = root.MatSynth || {};
            if (!ms.api) return Promise.resolve([]);
            if (this.loading) return Promise.resolve(this.list);
            if (this.loaded && !force) return Promise.resolve(this.list);
            var self = this;
            this.loading = true;
            return ms.api.request('GET', '/api/midi/devices').then(function (r) {
                self.loading = false;
                if (r.ok && r.body && Array.isArray(r.body.devices)) { self.list = r.body.devices; self.loaded = true; self.error = null; }
                else { self.error = 'Dispositivi MIDI non letti: ' + errorText(r); }
                emit('devices');
                return self.list;
            });
        },
        // Le porte cambiano (cavo USB): si rilegge dopo una breve pausa, un solo timer.
        scheduleReload: function () {
            var self = this;
            if (this.timer) return;
            this.timer = setTimeout(function () { self.timer = null; self.load(true); }, 400);
        }
    };

    // Cache dell'ultimo `runtime` per parte: `parts_update` porta solo le parti cambiate e state.mergePartsUpdate
    // sostituisce `runtime` per intero, quindi le altre parti andrebbero perse.
    var runtimeCache = {};
    function readRuntime(partsDomain) {
        if (partsDomain && Array.isArray(partsDomain.runtime)) {
            partsDomain.runtime.forEach(function (r) { if (r && Number.isInteger(r.part_id)) runtimeCache[r.part_id] = r; });
        }
    }

    var wired = false;
    var mountedCount = 0;
    function wireStores() {
        if (wired) return;
        var ms = root.MatSynth || {};
        if (!ms.state) return;
        wired = true;
        // Una sola iscrizione per pagina (non per pannello): si legge `runtime`, si invalidano gli strumenti al
        // cambio SoundFont e si rileggono i dispositivi se cambiano le porte.
        ms.state.subscribe('parts', function (value) { readRuntime(value); });
        readRuntime(ms.state.get('parts'));
        ms.state.subscribe('soundfont', function (sf) {
            if (sf && sf.state === 'loading') { sfLoadSeq += 1; return; }
            if (!(sf && sf.state === 'loaded' && sf.file)) return;
            if (sf.file !== instrumentsStore.forFile) {
                // Cambio file (o primo stato "loaded" dopo un elenco non legato a un file): si rilegge. Se una
                // lettura e' in corso, se ne fa una nuova al termine (quella in corso potrebbe essere del vecchio SF).
                var pending = instrumentsStore.loading ? instrumentsStore.promise : null;
                instrumentsStore.invalidate();
                if (mountedCount > 0) {
                    if (pending) pending.then(function () { if (instrumentsStore.forFile !== sf.file) { instrumentsStore.invalidate(); instrumentsStore.load(true); } });
                    else instrumentsStore.load(true);
                }
            }
        });
        ms.state.subscribe('ports', function () { if (mountedCount > 0) devicesStore.scheduleReload(); });
    }

    /* ── DOM ── */
    function el(tag, attrs, kids) {
        var d = root.document;
        var n = d.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) {
            var v = attrs[k];
            if (v === null || v === undefined || v === false) return;
            if (k === 'class') n.className = v;
            else if (k === 'text') n.textContent = v;
            else if (k === 'html') n.innerHTML = v;
            else n.setAttribute(k, v === true ? '' : String(v));
        });
        (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === 'string' ? d.createTextNode(c) : c); });
        return n;
    }
    function setText(n, t) { if (n && n.textContent !== t) n.textContent = t; }

    var TABS = [
        { id: 'sound', label: 'Suono', icon: 'fa-wave-square' },
        { id: 'mix', label: 'Mix', icon: 'fa-sliders-h' },
        { id: 'routing', label: 'Routing', icon: 'fa-route' }
    ];

    function mountModern(container, opts) {
        var ms = root.MatSynth || {};
        var api = ms.api, state = ms.state;
        wireStores();
        mountedCount += 1;
        var u = 'pp' + (++uidCounter);
        var partId = Number.isInteger(opts.partId) ? opts.partId : 0;
        container.classList.add('pp');
        container.textContent = '';

        var activeTab = 'sound';
        var browserOpen = false;
        var browserDraw = null;
        var busy = false;
        var sig = null;                     // firma della struttura corrente
        var controls = {};                  // cc -> {range, num, hint, unsent}
        var dragging = {};                  // cc -> true mentre il dito/mouse e' sullo slider (niente eco dal server)
        var routingDraft = null;
        var routingTouched = false;
        var routingKey = '';
        var statusTimer = null;
        var dead = false;

        /* ── scheletro fisso ── */
        var titleEl = el('span', { class: 'pp-title' });
        var destEl = el('span', { class: 'pp-dest' });
        var alertBtn = el('button', { type: 'button', class: 'pp-alert', hidden: true });
        var soundName = el('strong', { class: 'pp-soundname' });
        var soundDetail = el('small', { class: 'pp-sounddetail' });
        var changeBtn = el('button', { type: 'button', class: 'pp-btn pp-btn-primary pp-change' });
        var volHost = el('div', { class: 'pp-volhost' });
        var tabsEl = el('div', { class: 'pp-tabs', role: 'tablist', 'aria-label': 'Controlli della parte' });
        var panes = {};
        TABS.forEach(function (t) {
            tabsEl.appendChild(el('button', { type: 'button', class: 'pp-tab', role: 'tab', id: u + '-tab-' + t.id,
                'aria-controls': u + '-pane-' + t.id, 'aria-selected': t.id === activeTab ? 'true' : 'false',
                tabindex: t.id === activeTab ? '0' : '-1', 'data-tab': t.id }, [el('i', { class: 'fas ' + t.icon, 'aria-hidden': 'true' }), ' ' + t.label]));
            panes[t.id] = el('div', { class: 'pp-pane', role: 'tabpanel', id: u + '-pane-' + t.id, 'aria-labelledby': u + '-tab-' + t.id, hidden: t.id !== activeTab });
        });
        var browserEl = el('div', { class: 'pp-browser', hidden: true });
        var statusEl = el('p', { class: 'pp-status', role: 'status', 'aria-live': 'polite' });

        container.appendChild(el('div', { class: 'pp-head' }, [
            el('div', { class: 'pp-titlerow' }, [titleEl, destEl, alertBtn]),
            el('div', { class: 'pp-soundrow' }, [el('div', { class: 'pp-soundtext' }, [soundName, soundDetail]), changeBtn]),
            volHost
        ]));
        container.appendChild(browserEl);
        var mainEl = el('div', { class: 'pp-main' }, [tabsEl, panes.sound, panes.mix, panes.routing]);
        container.appendChild(mainEl);
        container.appendChild(statusEl);

        function say(msg, kind) {
            statusEl.setAttribute('data-kind', kind || 'info');
            // Prefisso testuale: lo stato non dipende dal solo colore.
            var prefix = kind === 'error' ? 'Errore: ' : kind === 'warn' ? 'Attenzione: ' : '';
            statusEl.textContent = msg ? prefix + msg : '';
            clearTimeout(statusTimer);
            if (msg && kind !== 'error' && kind !== 'warn') statusTimer = setTimeout(function () { statusEl.textContent = ''; }, 4000);
        }

        function routingKeyOf(part) { return part ? JSON.stringify([part.input, part.destination, !!part.monitor]) : ''; }
        function curPart() { return state ? state.getPart(partId) : null; }
        function curDevice(part) {
            return part && isExternal(part) ? devicesStore.get(part.destination.device_alias) : null;
        }

        /* ── invio CC (coalescato) ── */
        var coal = createCoalescer(function (cc, val, meta) {
            if (state) state.updatePartCC(meta.part, Number(cc), val, { origin: u });
            if (!api) return;
            // Socket connesso -> solo ack; altrimenti solo HTTP: mai entrambi (contracts 2.5).
            api.command('cc_update', { chan: meta.part, cc: Number(cc), val: val },
                { method: 'GET', url: '/cc/' + meta.part + '/' + cc + '/' + val }).then(function (r) {
                if (!r.ok && !dead) say('CC' + cc + ' parte ' + (meta.part + 1) + ': ' + errorText(r), 'error');
            });
        }, THROTTLE_MS);

        /* ── righe di slider ── */
        function addRow(host, cc, big) {
            var c = CC_DEF[cc];
            var id = u + '-cc' + cc;
            var isPan = cc === 10;
            var range = el('input', { type: 'range', class: 'pp-range' + (big ? ' pp-range-xl' : ''), id: id, min: '0', max: '127', step: '1', value: String(c.def), 'data-cc': cc });
            var num = el('input', { type: 'number', class: 'pp-num', id: id + '-num', min: '0', max: '127', step: '1', inputmode: 'numeric',
                value: String(c.def), 'data-cc': cc, 'aria-label': c.label + ' valore numerico (CC' + cc + ')' });
            var hint = isPan ? el('span', { class: 'pp-hint', 'aria-hidden': 'true', text: 'C' }) : null;
            var unsent = el('span', { class: 'pp-unsent', hidden: true, text: 'non inviato' });
            host.appendChild(el('div', { class: 'pp-row' + (big ? ' pp-row-xl' : ''), 'data-cc': cc }, [
                el('label', { class: 'pp-label', for: id }, [c.label + ' ', el('span', { class: 'pp-cc', text: 'CC' + cc }), unsent]),
                hint, num, range
            ]));
            controls[cc] = { range: range, num: num, hint: hint, unsent: unsent, def: c.def };
        }

        function show(cc, val) {
            var c = controls[cc];
            if (!c) return;
            var s = String(val);
            if (c.range.value !== s) c.range.value = s;
            if (c.num.value !== s) c.num.value = s;
            if (c.hint) setText(c.hint, panLabel(val));
        }

        /* ── struttura (si ricostruisce solo se cambia la firma) ── */
        function signature(part) {
            var dev = curDevice(part);
            return [partId, isExternal(part) ? 'e' : 'i', part && part.destination ? part.destination.device_alias : '',
                    allowedControls(part, dev).join(','), part ? 'p' : 'n', devicesStore.loaded ? 'd' : '-'].join('|');
        }

        function note(text, cls) { return el('p', { class: 'pp-note' + (cls ? ' ' + cls : ''), text: text }); }

        function field(label, input, id, help) {
            return el('div', { class: 'pp-field' }, [el('label', { for: id, text: label }), input, help ? el('small', { text: help }) : null]);
        }

        function buildSoundPane(part, allowed) {
            var host = panes.sound;
            host.textContent = '';
            if (!part) { host.appendChild(note('Parti non ancora lette dal server.')); return; }
            if (isExternal(part)) {
                var s = part.sound || {};
                var nameIn = el('input', { type: 'text', class: 'pp-text', id: u + '-xname', maxlength: '64', value: s.name || '', autocomplete: 'off' });
                function numIn(id, v) { return el('input', { type: 'text', class: 'pp-num pp-num-wide', id: id, inputmode: 'numeric', maxlength: '3', value: v === null || v === undefined ? '' : String(v), autocomplete: 'off' }); }
                var msb = numIn(u + '-xmsb', s.bank_msb), lsb = numIn(u + '-xlsb', s.bank_lsb), pc = numIn(u + '-xpc', s.program);
                var send = el('button', { type: 'button', class: 'pp-btn pp-btn-primary', id: u + '-xsend', text: 'Invia suono' });
                host.appendChild(note('Il nome è solo un\'etichetta tua. Lascia vuoto un numero per non inviare quel messaggio. L\'invio non conferma lo stato dello strumento.'));
                host.appendChild(field('Nome del suono', nameIn, u + '-xname'));
                host.appendChild(el('div', { class: 'pp-grid3' }, [
                    field('Bank MSB (CC0)', msb, u + '-xmsb'), field('Bank LSB (CC32)', lsb, u + '-xlsb'), field('Programma (PC)', pc, u + '-xpc')]));
                host.appendChild(el('div', { class: 'pp-actions' }, [send]));
                send.addEventListener('click', function () {
                    var a = parseMidiField(msb.value), b = parseMidiField(lsb.value), c = parseMidiField(pc.value);
                    var bad = a.error || b.error || c.error;
                    if (bad) { say(bad, 'error'); return; }
                    var nm = nameIn.value.trim();
                    patchPart({ sound: { bank_msb: a.value, bank_lsb: b.value, program: c.value, name: nm ? nm.slice(0, 64) : null } }, 'Suono inviato');
                });
                var any = false;
                SOUND_CONTROLS.forEach(function (c2) { if (allowed.indexOf(c2.cc) >= 0) { addRow(host, c2.cc, false); any = true; } });
                if (!any) host.appendChild(note('Questo strumento non dichiara controlli sonori (CC71-75): nessun cursore. Per altri messaggi usa l\'editor eventi in Studio.'));
            } else {
                var s2 = part.sound || {};
                var sn = note(internalSoundNote(s2));
                sn.id = u + '-snote';
                host.appendChild(sn);
                SOUND_CONTROLS.forEach(function (c3) { addRow(host, c3.cc, false); });
            }
        }

        function internalSoundNote(s) {
            return 'Suono interno (FluidSynth): bank ' + s.bank + ', programma ' + s.program + '. Usa "Cambia suono" per scegliere da elenco.';
        }

        // Dopo un richiamo scena o un cambio da un altro client i campi del suono seguono lo stato, tranne quello in uso.
        var soundKey = null;
        function refreshSoundFields(part) {
            var s = part && part.sound ? part.sound : {};
            var key = JSON.stringify(s);
            if (key === soundKey) return;
            soundKey = key;
            var sn = container.querySelector('#' + u + '-snote');
            if (sn) setText(sn, internalSoundNote(s));
            [['xname', s.name], ['xmsb', s.bank_msb], ['xlsb', s.bank_lsb], ['xpc', s.program]].forEach(function (f) {
                var inp = container.querySelector('#' + u + '-' + f[0]);
                if (inp && root.document.activeElement !== inp) inp.value = f[1] === null || f[1] === undefined ? '' : String(f[1]);
            });
        }

        function buildMixPane(part, allowed) {
            var host = panes.mix;
            host.textContent = '';
            if (!part) { host.appendChild(note('Parti non ancora lette dal server.')); return; }
            if (allowed.indexOf(10) >= 0) addRow(host, 10, false);
            else if (isExternal(part)) host.appendChild(note('Pan non dichiarato da questo strumento.'));
            host.appendChild(note('Questa parte è anche la traccia ' + (partId + 1) + ' di Studio: cambiare suono o controlli cambia anche la traccia.'));
            if (!isExternal(part)) {
                var reset = el('button', { type: 'button', class: 'pp-btn', id: u + '-reset', text: 'Reset canale…' });
                var confirmBox = el('div', { class: 'pp-confirm', hidden: true }, [
                    el('p', { text: 'Invia il comando di reset di FluidSynth per il canale della parte ' + (partId + 1) + '. I controller del synth tornano ai valori iniziali; i valori salvati nella parte restano quelli mostrati qui.' }),
                    el('div', { class: 'pp-actions' }, [
                        el('button', { type: 'button', class: 'pp-btn pp-btn-danger', 'data-act': 'reset-ok', text: 'Conferma reset' }),
                        el('button', { type: 'button', class: 'pp-btn', 'data-act': 'reset-cancel', text: 'Annulla' })])
                ]);
                host.appendChild(el('div', { class: 'pp-actions' }, [reset]));
                host.appendChild(confirmBox);
                reset.addEventListener('click', function () { confirmBox.hidden = false; reset.disabled = true; });
                confirmBox.addEventListener('click', function (e) {
                    var b = e.target.closest ? e.target.closest('[data-act]') : null;
                    if (!b) return;
                    confirmBox.hidden = true; reset.disabled = false;
                    if (b.getAttribute('data-act') !== 'reset-ok') return;
                    var pid = partId;
                    say('Reset in corso…', 'info');
                    api.request('GET', '/reset_channel/' + pid).then(function (r) {
                        if (!r.ok) { say(errorText(r, { NOT_INTERNAL_PART: 'Il reset vale solo per le parti interne.' }), 'error'); return; }
                        say('Reset inviato a FluidSynth (esito non verificabile dal synth).', 'ok');
                    });
                });
            }
        }

        function buildVolume(part, allowed) {
            volHost.textContent = '';
            delete controls[7];
            if (part && allowed.indexOf(7) >= 0) addRow(volHost, 7, true);
            else if (part && isExternal(part)) volHost.appendChild(note('Volume (CC7) non dichiarato da questo strumento.', 'pp-note-small'));
        }

        function buildStructure(part) {
            controls = {};
            var allowed = allowedControls(part, curDevice(part));
            buildVolume(part, allowed);
            buildSoundPane(part, allowed);
            buildMixPane(part, allowed);
            buildRoutingPane(part);
            updateChangeButton(part);
        }

        function updateChangeButton(part) {
            var ext = isExternal(part);
            changeBtn.textContent = '';
            changeBtn.appendChild(el('i', { class: 'fas ' + (ext ? 'fa-pen' : 'fa-music'), 'aria-hidden': 'true' }));
            changeBtn.appendChild(root.document.createTextNode(' ' + (ext ? 'Modifica suono' : 'Cambia suono')));
            changeBtn.disabled = !part;
        }

        /* ── routing ── */
        function aliasOptions(devs, key, current, nullLabel) {
            var opts = [];
            if (nullLabel) opts.push({ value: '', label: nullLabel });
            var seen = false;
            devs.forEach(function (d) {
                if (key === 'output' && !d.output) return;
                if (d.alias === current) seen = true;
                var st = d.status && d.status[key];
                opts.push({ value: d.alias, label: (d.label || d.alias) + ' (' + d.alias + ')' + (st && st !== 'available' ? ' – ' + statusText(st) : '') });
            });
            if (current && !seen) opts.push({ value: current, label: current + ' – non nel registro' });
            return opts;
        }
        function selectEl(id, options, value) {
            var s = el('select', { class: 'pp-select', id: id });
            options.forEach(function (o) { s.appendChild(el('option', { value: o.value, text: o.label })); });
            s.value = value;
            return s;
        }
        function channelOptions(withAll) {
            var o = withAll ? [{ value: 'all', label: 'Tutti i canali' }] : [];
            for (var i = 0; i < 16; i++) o.push({ value: String(i), label: 'Canale ' + (i + 1) });
            return o;
        }

        function buildRoutingPane(part) {
            var host = panes.routing;
            host.textContent = '';
            routingTouched = false;
            routingKey = routingKeyOf(part);
            if (!part || !part.input || !part.destination) {
                host.appendChild(note(part ? 'Il server non fornisce il routing (backend precedente): sezione non disponibile.' : 'Parti non ancora lette dal server.'));
                return;
            }
            routingDraft = draftFromPart(part);
            var devs = devicesStore.list;
            var inAlias = selectEl(u + '-rin', aliasOptions(devs, 'input', routingDraft.inAlias, 'Nessun ingresso (non registra)'), routingDraft.inAlias || '');
            var inCh = selectEl(u + '-rinch', channelOptions(true), routingDraft.inChannel === null ? 'all' : String(routingDraft.inChannel));
            var kindInt = el('button', { type: 'button', class: 'pp-seg', 'data-kind': 'internal', text: 'Interno' });
            var kindExt = el('button', { type: 'button', class: 'pp-seg', 'data-kind': 'external', text: 'Esterno' });
            var destAlias = selectEl(u + '-rdest', aliasOptions(devs, 'output', routingDraft.destAlias, null), routingDraft.destAlias || '');
            var destCh = selectEl(u + '-rdch', channelOptions(false), String(routingDraft.destChannel));
            var mon = el('input', { type: 'checkbox', id: u + '-rmon', class: 'pp-check' });
            mon.checked = routingDraft.monitor;
            var inHelp = el('small', { class: 'pp-help' });
            var kindHelp = el('p', { class: 'pp-note pp-note-warn', hidden: true });
            var apply = el('button', { type: 'button', class: 'pp-btn pp-btn-primary', text: 'Applica routing' });
            var restore = el('button', { type: 'button', class: 'pp-btn', text: 'Ripristina' });
            var notesBox = el('div', { class: 'pp-notes', id: u + '-notes' });
            var statusLine = el('p', { class: 'pp-note', id: u + '-rstat' });

            function readDraft() {
                routingDraft = {
                    inAlias: inAlias.value === '' ? null : inAlias.value,
                    inChannel: inCh.value === 'all' ? null : parseInt(inCh.value, 10),
                    kind: kindExt.getAttribute('aria-pressed') === 'true' ? 'external' : 'internal',
                    destAlias: destAlias.value === '' ? null : destAlias.value,
                    destChannel: parseInt(destCh.value, 10),
                    monitor: mon.checked
                };
            }
            function sync() {
                readDraft();
                var ext = routingDraft.kind === 'external';
                destAlias.parentNode.hidden = !ext;
                if (ext && routingDraft.destAlias === null && destAlias.options.length) { destAlias.selectedIndex = 0; routingDraft.destAlias = destAlias.value || null; }
                var p = curPart();
                var cur = p && p.destination ? p.destination.kind : 'internal';
                kindHelp.hidden = routingDraft.kind === cur;
                kindHelp.textContent = routingDraft.kind === cur ? '' :
                    'Cambiando il tipo di destinazione, suono e controlli della parte tornano ai valori predefiniti' + (ext ? ' e il monitor resta spento se non lo attivi qui sotto.' : '.');
                inHelp.textContent = routingDraft.inAlias === null ? 'Senza ingresso questa traccia non registra e non c\'è monitor.' :
                    routingDraft.inChannel === null ? 'Tutti i canali: ogni traccia che corrisponde a questo ingresso riceve gli stessi eventi.' : '';
                var patch = buildRoutingPatch(p, routingDraft);
                var changed = Object.keys(patch).length > 0;
                apply.disabled = !changed || busy || (ext && !routingDraft.destAlias);
                restore.disabled = !changed;
            }
            function setKind(k) {
                kindInt.setAttribute('aria-pressed', k === 'internal' ? 'true' : 'false');
                kindExt.setAttribute('aria-pressed', k === 'external' ? 'true' : 'false');
            }
            setKind(routingDraft.kind);
            host.appendChild(el('fieldset', { class: 'pp-fieldset' }, [el('legend', { text: 'Ingresso' }),
                field('Strumento di ingresso', inAlias, u + '-rin'), field('Canale in ingresso', inCh, u + '-rinch'), inHelp]));
            host.appendChild(el('fieldset', { class: 'pp-fieldset' }, [el('legend', { text: 'Destinazione' }),
                el('div', { class: 'pp-segrow', role: 'group', 'aria-label': 'Tipo di destinazione' }, [kindInt, kindExt]),
                field('Strumento di uscita', destAlias, u + '-rdest'), field('Canale in uscita', destCh, u + '-rdch'), kindHelp,
                el('label', { class: 'pp-checkrow', for: u + '-rmon' }, [mon, el('span', { text: 'Monitor: inoltra l\'ingresso alla destinazione' })])]));
            host.appendChild(el('div', { class: 'pp-actions' }, [apply, restore]));
            host.appendChild(statusLine);
            host.appendChild(notesBox);
            var rdest = destAlias.parentNode;   // .pp-field del selettore di uscita

            function onAny(e) {
                var seg = e.target.closest ? e.target.closest('.pp-seg') : null;
                if (seg) setKind(seg.getAttribute('data-kind'));
                routingTouched = true;
                sync();
            }
            host.addEventListener('change', onAny);
            host.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('.pp-seg')) onAny(e); });
            apply.addEventListener('click', function () {
                readDraft();
                var patch = buildRoutingPatch(curPart(), routingDraft);
                if (!Object.keys(patch).length) return;
                patchPart(patch, 'Routing applicato').then(function (r) { if (r && r.ok) { routingTouched = false; buildRoutingPane(curPart()); renderNotes(); } });
            });
            restore.addEventListener('click', function () { routingTouched = false; buildRoutingPane(curPart()); renderNotes(); });
            rdest.hidden = routingDraft.kind !== 'external';
            sync();
        }

        /* ── avvisi e stato (si aggiornano senza ricostruire) ── */
        function renderNotes() {
            var part = curPart();
            var runtime = runtimeCache[partId] || null;
            var state_ = state ? state.get('parts') : null;
            var notes = describeNotes(partId, state_ ? state_.conflicts : [], runtime, part);
            var warns = notes.filter(function (n) { return n.level === 'warn'; }).length;
            alertBtn.hidden = warns === 0;
            if (warns) setText(alertBtn, '⚠ ' + warns + (warns === 1 ? ' avviso' : ' avvisi'));
            var box = container.querySelector('#' + u + '-notes');
            if (box) {
                var key = notes.map(function (n) { return n.code + n.text; }).join('|');
                if (box.getAttribute('data-key') !== key) {
                    box.setAttribute('data-key', key);
                    box.textContent = '';
                    if (!notes.length) box.appendChild(el('p', { class: 'pp-note', text: 'Nessun avviso di routing per questa parte.' }));
                    notes.forEach(function (n) {
                        box.appendChild(el('p', { class: 'pp-alertline', 'data-level': n.level }, [el('strong', { text: n.level === 'warn' ? 'Attenzione: ' : 'Nota: ' }), n.text]));
                    });
                }
            }
            var rs = container.querySelector('#' + u + '-rstat');
            if (rs && runtime) setText(rs, 'Ingresso: ' + statusText(runtime.input_status) + ' · Destinazione: ' + statusText(runtime.destination_status) +
                (runtime.monitor_effective ? ' · Monitor attivo' : ' · Monitor non attivo'));
        }

        function refresh() {
            if (dead) return;
            var part = curPart();
            var s = signature(part);
            if (s !== sig) {
                sig = s;
                buildStructure(part);
                soundKey = null;
                routingKey = routingKeyOf(part);
            } else if (!routingTouched) {
                // Routing cambiato da un altro client e nessuna bozza in corso: il modulo si riallinea.
                var rk = routingKeyOf(part);
                if (rk !== routingKey) { routingKey = rk; buildRoutingPane(part); }
            }
            setText(titleEl, 'Parte ' + (partId + 1));
            setText(destEl, destinationLabel(part, curDevice(part)));
            var t = soundTitle(part, instrumentsStore.loaded ? instrumentsStore.list : null);
            setText(soundName, t.name);
            setText(soundDetail, t.detail);
            refreshSoundFields(part);
            var ext = isExternal(part);
            Object.keys(controls).forEach(function (cc) {
                var c = controls[cc];
                if (dragging[cc] || root.document.activeElement === c.range || root.document.activeElement === c.num) return;
                var has = part && part.cc && part.cc[cc] !== undefined;
                var v = has ? part.cc[cc] : c.def;
                show(cc, v);
                if (c.unsent) c.unsent.hidden = !(ext && !has);
            });
            renderNotes();
        }

        /* ── PATCH parte (suono, routing) ── */
        function patchPart(body, okText) {
            if (busy) return Promise.resolve(null);
            var pid = partId;
            busy = true;
            say('Invio in corso…', 'info');
            return api.request('PATCH', '/api/parts/' + pid, body).then(function (r) {
                busy = false;
                if (dead) return r;
                if (!r.ok) {
                    say(errorText(r), 'error');
                    if (r.unknown_outcome) resync();
                    return r;
                }
                if (r.body && r.body.part && state) {
                    state.mergePartsUpdate({ parts: [r.body.part], runtime: isObj(r.body.runtime) ? [r.body.runtime] : undefined, conflicts: r.body.conflicts });
                }
                var delivery = r.body && r.body.applied ? r.body.applied.delivery : null;
                if (pid === partId) {
                    if (delivery === 'failed') say('Configurazione salvata ma invio NON riuscito: controlla lo strumento o la porta.', 'warn');
                    else if (delivery === 'skipped') say('Destinazione non disponibile: configurazione salvata, nulla è stato inviato.', 'warn');
                    else say(okText + ' (inviato, non verificato sullo strumento).', 'ok');
                }
                refresh();
                return r;
            });
        }

        // Dopo un esito sconosciuto lo stato autoritativo si rilegge (2.6): nessuna ripetizione automatica.
        function resync() {
            api.request('GET', '/api/parts').then(function (r) {
                if (r.ok && r.body && Array.isArray(r.body.parts) && state) {
                    state.set('parts', { source: 'api', items: r.body.parts, runtime: r.body.runtime || [], conflicts: r.body.conflicts || [] });
                }
            });
        }

        /* ── browser strumenti (solo interni) ── */
        function closeBrowser() {
            browserOpen = false;
            browserDraw = null;
            browserEl.hidden = true;
            browserEl.textContent = '';
            mainEl.hidden = false;
            changeBtn.setAttribute('aria-expanded', 'false');
            changeBtn.focus();
        }

        function openBrowser() {
            var part = curPart();
            if (!part) return;
            if (isExternal(part)) { selectTab('sound'); var nm = container.querySelector('#' + u + '-xname'); if (nm) nm.focus(); return; }
            browserOpen = true;
            mainEl.hidden = true;
            browserEl.hidden = false;
            changeBtn.setAttribute('aria-expanded', 'true');
            browserEl.textContent = '';
            var search = el('input', { type: 'search', class: 'pp-text pp-search', id: u + '-isearch', placeholder: 'Cerca strumento, bank o programma', autocomplete: 'off', 'aria-label': 'Cerca strumento' });
            var closeB = el('button', { type: 'button', class: 'pp-btn', text: 'Chiudi elenco' });
            var info = el('p', { class: 'pp-note', role: 'status' });
            var cats = el('div', { class: 'pp-cats', role: 'group', 'aria-label': 'Categorie (famiglie General MIDI)' });
            var category = '';
            var catsKey = null;
            var list = el('ul', { class: 'pp-instlist' });
            browserEl.appendChild(el('div', { class: 'pp-browserhead' }, [el('strong', { text: 'Scegli il suono della parte ' + (partId + 1) }), closeB]));
            browserEl.appendChild(search);
            browserEl.appendChild(cats);
            browserEl.appendChild(info);
            browserEl.appendChild(list);
            var LIMIT = 120;
            var pickBusy = false;
            function drawCats() {
                var found = instrumentsStore.loaded ? instrumentCategories(instrumentsStore.list) : [];
                if (category && !found.some(function (c) { return c.id === category; })) category = '';
                var key = found.map(function (c) { return c.id + c.count; }).join() + '|' + category;
                if (key === catsKey) return;
                catsKey = key;
                cats.textContent = '';
                cats.hidden = found.length < 2;
                [{ id: '', label: 'Tutti' }].concat(found).forEach(function (c) {
                    cats.appendChild(el('button', { type: 'button', class: 'pp-cat', 'data-cat': c.id, 'aria-pressed': c.id === category ? 'true' : 'false',
                        text: c.label + (c.count ? ' ' + c.count : '') }));
                });
            }
            function draw() {
                var p = curPart();
                var sound = p && p.sound ? p.sound : {};
                list.textContent = '';
                drawCats();
                if (!instrumentsStore.loaded) {
                    info.textContent = instrumentsStore.loading ? 'Lettura degli strumenti…' : (instrumentsStore.error || 'Elenco non ancora letto.');
                    if (!instrumentsStore.loading) list.appendChild(el('li', null, [el('button', { type: 'button', class: 'pp-btn', 'data-retry': '1', text: 'Riprova' })]));
                    return;
                }
                var res = filterInstruments(instrumentsStore.list, search.value, LIMIT, category);
                info.textContent = instrumentsStore.error ? instrumentsStore.error :
                    res.total === 0 ? 'Nessuno strumento corrisponde.' :
                    res.total > res.items.length ? 'Primi ' + res.items.length + ' di ' + res.total + ': restringi la ricerca.' : res.total + (res.total === 1 ? ' strumento' : ' strumenti');
                res.items.forEach(function (it) {
                    var sel = it.bank === sound.bank && it.program === sound.program;
                    var b = el('button', { type: 'button', class: 'pp-inst' + (sel ? ' is-selected' : ''), 'aria-current': sel ? 'true' : null,
                        'data-bank': it.bank, 'data-program': it.program }, [
                        el('span', { class: 'pp-inst-mark', 'aria-hidden': 'true', text: sel ? '✓' : '' }),
                        el('span', { class: 'pp-inst-name', text: it.name }),
                        el('small', { class: 'pp-inst-num', text: it.bank + '-' + it.program })]);
                    list.appendChild(el('li', null, [b]));
                });
            }
            browserDraw = draw;
            search.addEventListener('input', draw);
            cats.addEventListener('click', function (e) {
                var b = e.target.closest ? e.target.closest('.pp-cat') : null;
                if (!b) return;
                category = b.getAttribute('data-cat') || '';
                draw();
            });
            closeB.addEventListener('click', closeBrowser);
            browserEl.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); closeBrowser(); } });
            list.addEventListener('click', function (e) {
                if (e.target.closest && e.target.closest('[data-retry]')) {
                    instrumentsStore.load(true).then(function () { if (browserOpen) draw(); });
                    draw();
                    return;
                }
                var b = e.target.closest ? e.target.closest('.pp-inst') : null;
                if (!b || pickBusy) return;
                pickBusy = true;
                b.disabled = true;
                var bank = parseInt(b.getAttribute('data-bank'), 10), prog = parseInt(b.getAttribute('data-program'), 10);
                var inst = findInstrument(instrumentsStore.list, bank, prog);
                patchPart({ sound: { bank: bank, program: prog, name: inst ? inst.name.slice(0, 64) : null } }, 'Suono cambiato').then(function (r) {
                    pickBusy = false;
                    if (r && r.ok) closeBrowser(); else b.disabled = false;
                });
            });
            draw();
            instrumentsStore.load(!instrumentsStore.loaded && !instrumentsStore.loading ? true : false).then(function () { if (browserOpen) draw(); });
            search.focus();
        }

        /* ── schede ── */
        function selectTab(name) {
            activeTab = name;
            TABS.forEach(function (t) {
                var tab = tabsEl.querySelector('#' + u + '-tab-' + t.id);
                var on = t.id === name;
                tab.setAttribute('aria-selected', on ? 'true' : 'false');
                tab.tabIndex = on ? 0 : -1;
                panes[t.id].hidden = !on;
            });
            if (name === 'routing' && !devicesStore.loaded) devicesStore.load(false);
        }

        /* ── eventi (delegati sul contenitore: un listener per tipo, rimossi in destroy) ── */
        function onInput(e) {
            var t = e.target;
            if (!t.dataset || t.dataset.cc === undefined) return;
            var cc = t.dataset.cc;
            if (t.type !== 'range') return;           // il campo numerico invia solo a conferma (change)
            var val = clampCC(t.value);
            show(cc, val);
            coal.push(cc, val, { part: partId }, false);
        }

        function onChange(e) {
            var t = e.target;
            if (!t.dataset || t.dataset.cc === undefined) return;
            var cc = t.dataset.cc;
            if (t.type === 'number' && String(t.value).trim() === '') { refresh(); return; }   // vuoto non e' 0
            var val = clampCC(t.value);
            show(cc, val);
            dragging[cc] = false;
            coal.push(cc, val, { part: partId }, true);    // valore finale garantito al rilascio / conferma numerica
        }

        function onPointerDown(e) {
            var t = e.target;
            if (t && t.dataset && t.dataset.cc !== undefined && t.type === 'range') dragging[t.dataset.cc] = true;
        }
        function onPointerEnd(e) {
            var t = e.target;
            if (t && t.dataset && t.dataset.cc !== undefined) { dragging[t.dataset.cc] = false; }
            else Object.keys(dragging).forEach(function (k) { dragging[k] = false; });
        }

        function onClick(e) {
            var tab = e.target.closest ? e.target.closest('.pp-tab') : null;
            if (tab) { selectTab(tab.dataset.tab); return; }
            if (e.target.closest && e.target.closest('.pp-change')) { if (browserOpen) closeBrowser(); else openBrowser(); return; }
            if (e.target.closest && e.target.closest('.pp-alert')) selectTab('routing');
        }

        function onKey(e) {
            if (!e.target.classList || !e.target.classList.contains('pp-tab')) return;
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                var ids = TABS.map(function (t) { return t.id; });
                var i = ids.indexOf(e.target.dataset.tab);
                var next = ids[(i + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
                selectTab(next);
                container.querySelector('#' + u + '-tab-' + next).focus();
                e.preventDefault();
            }
        }

        container.addEventListener('input', onInput);
        container.addEventListener('change', onChange);
        container.addEventListener('click', onClick);
        container.addEventListener('keydown', onKey);
        container.addEventListener('pointerdown', onPointerDown);
        container.addEventListener('pointerup', onPointerEnd);
        container.addEventListener('pointercancel', onPointerEnd);
        container.addEventListener('lostpointercapture', onPointerEnd);

        var unsub = state ? state.subscribe('parts', function (value, domain, meta) {
            readRuntime(value);
            if (meta && meta.origin === u) return;
            if (meta && meta.part_id !== undefined && meta.part_id !== partId) return;
            refresh();
        }) : function () {};
        var unsubInst = onStore('instruments', function () { if (dead) return; refresh(); if (browserOpen && browserDraw) browserDraw(); });
        var unsubDev = onStore('devices', function () { if (!dead) refresh(); });
        // Il registro dispositivi serve a conoscere i `controls` di una parte esterna: si legge una volta.
        var p0 = curPart();
        if (isExternal(p0) && !devicesStore.loaded) devicesStore.load(false);

        refresh();

        return {
            setPart: function (id) {
                if (!Number.isInteger(id) || id < 0 || id > 15 || id === partId) { if (id === partId) refresh(); return; }
                coal.flush();          // eventuale ultimo valore ancora in coda va alla parte precedente
                if (browserOpen) closeBrowser();
                partId = id;
                dragging = {};
                routingTouched = false;
                say('', 'info');
                var p = curPart();
                if (isExternal(p) && !devicesStore.loaded) devicesStore.load(false);
                sig = null;
                refresh();
            },
            getPart: function () { return partId; },
            refresh: refresh,
            destroy: function () {
                dead = true;
                coal.flush();
                container.removeEventListener('input', onInput);
                container.removeEventListener('change', onChange);
                container.removeEventListener('click', onClick);
                container.removeEventListener('keydown', onKey);
                container.removeEventListener('pointerdown', onPointerDown);
                container.removeEventListener('pointerup', onPointerEnd);
                container.removeEventListener('pointercancel', onPointerEnd);
                container.removeEventListener('lostpointercapture', onPointerEnd);
                unsub(); unsubInst(); unsubDev();
                mountedCount = Math.max(0, mountedCount - 1);
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

    return {
        mount: mount, panLabel: panLabel, clampCC: clampCC, THROTTLE_MS: THROTTLE_MS,
        // Parte pura e archivi condivisi (usati da live.js / library_scenes.js e dai test)
        parseInstruments: parseInstruments, filterInstruments: filterInstruments, findInstrument: findInstrument,
        instrumentCategory: instrumentCategory, instrumentCategories: instrumentCategories,
        parseMidiField: parseMidiField, allowedControls: allowedControls, destinationLabel: destinationLabel,
        soundTitle: soundTitle, describeNotes: describeNotes, buildRoutingPatch: buildRoutingPatch,
        draftFromPart: draftFromPart, errorText: errorText, createCoalescer: createCoalescer, statusText: statusText,
        isExternal: isExternal,
        instruments: instrumentsStore, devices: devicesStore, onStore: onStore,
        runtimeFor: function (id) { return runtimeCache[id] || null; }
    };
});
