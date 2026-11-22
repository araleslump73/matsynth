/* MatSynth - shell.js
 * Shell della pagina: navigazione Suona | Studio, pannelli Libreria/Impostazioni, barra globale
 * (connessione, trasporto, modifiche non salvate, PANIC) e cablaggio UNICO degli eventi Socket.IO.
 *
 * Regole (piano sez. 4, 10; contracts.md 9, 11.2):
 *  - showView()/openPanel() NON inviano alcun comando musicale e non toccano il trasporto: cambiano solo
 *    visibilita' e focus. (Verifica statica: queste funzioni non chiamano api.*.)
 *  - Un solo socket (api.js) e ogni listener e' registrato UNA volta in init(): i cambi vista non ne aggiungono.
 *  - Nessun timer periodico: gli aggiornamenti arrivano da eventi; il DOM si tocca solo se il testo cambia.
 * Va caricato dopo units/state/api/part_panel/library e PRIMA di legacy_daw.js: la parte sincrona di boot
 * (pannello Mix/Suono legacy, select snap) deve esistere quando parte il codice legacy. */
(function (root) {
    'use strict';
    var MS = root.MatSynth = root.MatSynth || {};
    var doc = root.document;
    var api = MS.api, state = MS.state, units = MS.units, partPanel = MS.partPanel, library = MS.library;

    var VIEWS = ['live', 'studio', 'editor'];
    // Suona e' ancora un segnaposto finche' non arriva ui-live: fino ad allora la pagina si apre su Studio
    // (= comportamento precedente). ui-live cambia solo questa costante.
    var DEFAULT_VIEW = 'studio';
    var PANELS = ['library', 'settings'];

    var currentView = null;
    var openPanelName = null;
    var lastFocus = null;
    var initialized = false;
    var snapshotGen = 0;
    var livePanel = null;
    var toastTimer = null;

    function $(id) { return doc.getElementById(id); }
    function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }

    /* ── Viste ─────────────────────────────────────────────────────────── */
    function showView(view) {
        if (VIEWS.indexOf(view) < 0) return false;
        var prev = currentView;
        VIEWS.forEach(function (v) {
            var el = $('view-' + v);
            if (el) el.hidden = (v !== view);
        });
        currentView = view;
        doc.body.setAttribute('data-view', view);
        // L'editor e' una sottovista di Studio: la voce di navigazione evidenziata resta Studio.
        var navView = view === 'live' ? 'live' : 'studio';
        var buttons = doc.querySelectorAll('[data-view-target]');
        for (var i = 0; i < buttons.length; i++) {
            var on = buttons[i].getAttribute('data-view-target') === navView;
            buttons[i].setAttribute('aria-current', on ? 'page' : 'false');
            buttons[i].classList.toggle('is-active', on);
        }
        if (view !== 'editor' && state) state.setPref('lastView', view);
        if (view !== prev) {
            doc.dispatchEvent(new root.CustomEvent('matsynth:viewchange', { detail: { view: view, previous: prev } }));
            if (view !== 'live') {
                // I canvas legacy misurano la larghezza: dopo essere stati nascosti vanno rimisurati.
                // Il listener 'resize' legacy riavvia anche il loop di disegno fermato mentre la vista era nascosta.
                root.requestAnimationFrame(function () { root.dispatchEvent(new root.Event('resize')); });
            }
        }
        return true;
    }

    /* ── Pannelli (Libreria, Impostazioni) ─────────────────────────────── */
    function focusables(panel) {
        var list = panel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        var out = [];
        for (var i = 0; i < list.length; i++) {
            var el = list[i];
            if (el.disabled || el.hidden || el.offsetParent === null) continue;
            out.push(el);
        }
        return out;
    }

    function openPanel(name, section) {
        if (PANELS.indexOf(name) < 0) return false;
        var panel = $('ms-panel-' + name);
        if (!panel) return false;
        if (openPanelName && openPanelName !== name) hidePanel(openPanelName, true);
        if (!openPanelName) lastFocus = doc.activeElement;
        openPanelName = name;
        panel.hidden = false;
        doc.body.classList.add('ms-panel-open');
        if (name === 'library' && library) library.show(section || library.activeSection() || undefined);
        var f = focusables(panel);
        var close = panel.querySelector('[data-close-panel]');
        (close || f[0] || panel).focus();
        return true;
    }

    function hidePanel(name, keepFocusState) {
        var panel = $('ms-panel-' + name);
        if (panel) panel.hidden = true;
        if (!keepFocusState) {
            openPanelName = null;
            doc.body.classList.remove('ms-panel-open');
            var back = lastFocus;
            lastFocus = null;
            if (back && typeof back.focus === 'function' && doc.contains(back)) back.focus();
        }
    }

    function closePanel() {
        if (openPanelName) hidePanel(openPanelName, false);
    }

    function onPanelKeydown(e) {
        if (!openPanelName) return;
        var panel = $('ms-panel-' + openPanelName);
        if (e.key === 'Escape') {
            // Un modale legacy aperto sopra il pannello (z-index maggiore) ha la precedenza: non chiudere sotto.
            var modals = doc.querySelectorAll('.modal-overlay');
            for (var m = 0; m < modals.length; m++) {
                if (modals[m].style.display !== 'none' && modals[m].offsetParent !== null) return;
            }
            e.preventDefault();
            e.stopPropagation();
            closePanel();
            return;
        }
        if (e.key === 'Tab' && panel) {
            var f = focusables(panel);
            if (!f.length) return;
            var first = f[0], last = f[f.length - 1];
            if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
            else if (!panel.contains(doc.activeElement)) { e.preventDefault(); first.focus(); }
        }
    }

    /* ── Scorciatoie legacy: attive solo in Studio ─────────────────────── */
    // Il listener legacy su document (Spazio = Play, Ctrl+S, ...) resta, ma in fase di cattura lo
    // zittiamo fuori da Studio, con un pannello aperto o con il focus sulla barra della shell:
    // altrimenti Spazio su un pulsante della shell avvierebbe la riproduzione e annullerebbe il click.
    function guardLegacyShortcuts(e) {
        var t = e.target;
        var inChrome = t && t.closest && t.closest('.ms-globalbar, .ms-panel, #view-live');
        if (currentView !== 'studio' || openPanelName || inChrome) e.stopPropagation();
    }

    /* ── Barra globale ─────────────────────────────────────────────────── */
    function renderConnection() {
        var c = state.get('connection');
        var el = $('ms-conn');
        if (!el) return;
        var st = c.connected ? 'online' : (c.ever_connected ? 'offline' : 'connecting');
        el.setAttribute('data-state', st);
        setText($('ms-conn-text'), st === 'online' ? 'Connesso' : st === 'offline' ? 'Non connesso' : 'Connessione…');
        el.setAttribute('title', st === 'online' ? 'Connessione con MatSynth attiva' :
            st === 'offline' ? 'Connessione persa: ritento automaticamente' : 'In attesa di MatSynth');
    }

    function renderTransport() {
        var t = state.get('transport');
        var el = $('ms-transport');
        if (!el) return;
        var st = t.is_recording ? 'rec' : (t.is_playing ? 'play' : 'stop');
        if (el.getAttribute('data-state') !== st) el.setAttribute('data-state', st);
        setText($('ms-transport-text'), st === 'rec' ? 'REC' : st === 'play' ? 'PLAY' : 'STOP');
        setText($('ms-time'), units.formatTime(t.position));
    }

    function renderDirty() {
        var el = $('ms-dirty');
        if (!el) return;
        var scene = state.get('scene'), project = state.get('project');
        var what = [];
        if (project && project.dirty) what.push('progetto');
        if (scene && scene.dirty) what.push('scena');
        el.hidden = what.length === 0;
        if (what.length) {
            el.setAttribute('title', 'Modifiche non salvate: ' + what.join(' e '));
            setText($('ms-dirty-text'), 'Non salvato');
        }
    }

    function toast(message, kind) {
        var el = $('ms-toast');
        if (!el) return;
        el.textContent = message;
        el.setAttribute('data-kind', kind || 'info');
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { el.hidden = true; }, 4000);
    }

    // PANIC e' idempotente: se l'esito e' incerto si puo' ripremere senza rischi.
    function panic() {
        var btn = $('ms-panic');
        if (btn) btn.classList.add('is-busy');
        api.command('transport_cmd', { cmd: 'panic' }, { method: 'POST', url: '/api/daw/panic' }).then(function (r) {
            if (btn) btn.classList.remove('is-busy');
            if (r.ok) toast('PANIC inviato: note fermate', 'ok');
            else if (r.unknown_outcome) toast('PANIC: esito non confermato, riprova', 'error');
            else toast('PANIC non riuscito: ' + (r.error ? r.error.message : 'errore'), 'error');
        });
    }

    /* ── Snapshot (apertura e riconnessione, contracts 2.6) ────────────── */
    function legacyParts(gen) {
        return api.request('GET', '/get_state').then(function (r) {
            if (gen !== snapshotGen || !r.ok || !r.body || typeof r.body !== 'object') return;
            var ccNames = { volume: 7, pan: 10, resonance: 71, release: 72, attack: 73, cutoff: 74, decay: 75 };
            var items = [];
            var channels = r.body.channels || {};
            Object.keys(channels).forEach(function (k) {
                var ch = channels[k], id = parseInt(k, 10);
                if (!(id >= 0 && id <= 15) || !ch) return;
                var cc = {};
                Object.keys(ccNames).forEach(function (n) { if (ch[n] !== undefined) cc[ccNames[n]] = parseInt(ch[n], 10); });
                items.push({ part_id: id, sound: { bank: ch.bank, program: ch.program }, cc: cc });
            });
            items.sort(function (a, b) { return a.part_id - b.part_id; });
            state.set('parts', { source: 'legacy', items: items, runtime: [], conflicts: [] });
        });
    }

    function extraSnapshots(gen) {
        // Route nuove: se mancano (backend non ancora aggiornato) l'errore e' atteso e ignorato.
        [['/api/projects/current', 'project_state', 'project'],
         ['/api/scenes/current', 'scene_state', 'scene'],
         ['/api/soundfont/status', 'soundfont', 'soundfont'],
         ['/api/midi/ports', null, 'ports']].forEach(function (spec) {
            api.request('GET', spec[0]).then(function (r) {
                if (gen !== snapshotGen || !r.ok || !r.body) return;
                state.set(spec[2], spec[1] ? r.body[spec[1]] : r.body);
            });
        });
    }

    function loadSnapshot() {
        var gen = ++snapshotGen;
        api.request('GET', '/api/daw/state').then(function (r) {
            if (gen === snapshotGen && r.ok && r.body && r.body.data) state.ingestDawState(r.body.data);
        });
        api.request('GET', '/api/parts').then(function (r) {
            if (gen !== snapshotGen) return;
            if (r.ok && r.body && Array.isArray(r.body.parts)) {
                state.set('parts', { source: 'api', items: r.body.parts, runtime: r.body.runtime || [], conflicts: r.body.conflicts || [] });
                extraSnapshots(gen);
            } else {
                legacyParts(gen);   // backend precedente: stesse informazioni da /get_state
            }
        });
    }

    /* ── Cablaggio eventi (una volta sola) ─────────────────────────────── */
    function wireSocket() {
        api.on('connect', function () {
            state.set('connection', { connected: true, ever_connected: true, since: Date.now(), last_error: null });
            loadSnapshot();
        });
        api.on('disconnect', function (reason) {
            var c = state.get('connection');
            state.set('connection', { connected: false, ever_connected: c.ever_connected, since: Date.now(), last_error: reason || null });
        });
        api.on('connect_error', function (err) {
            var c = state.get('connection');
            state.set('connection', { connected: false, ever_connected: c.ever_connected, since: c.since, last_error: err && err.message ? err.message : 'errore' });
        });
        api.on('daw_state_update', function (d) { state.ingestDawState(d); });
        api.on('daw_tick', function (t) { state.ingestTick(t); });
        api.on('parts_update', function (p) { state.mergePartsUpdate(p); });
        api.on('scene_state', function (p) { state.set('scene', p); });
        api.on('project_state', function (p) { state.set('project', p); });
        api.on('soundfont_status', function (p) { state.set('soundfont', p); });
        api.on('ports_status', function (p) { state.set('ports', p); });
        // Se il socket era gia' connesso prima di iscriversi (script caricati dopo), allinea subito.
        if (api.connected()) {
            state.set('connection', { connected: true, ever_connected: true, since: Date.now(), last_error: null });
            loadSnapshot();
        }
    }

    function wireUi() {
        // Un solo listener delegato per tutta la pagina: i pulsanti non ne registrano uno ciascuno.
        doc.addEventListener('click', function (e) {
            var el = e.target.closest ? e.target.closest('[data-view-target],[data-open-panel],[data-close-panel]') : null;
            if (el) {
                if (el.hasAttribute('data-view-target')) { closePanel(); showView(el.getAttribute('data-view-target')); }
                else if (el.hasAttribute('data-open-panel')) { openPanel(el.getAttribute('data-open-panel'), el.getAttribute('data-section') || undefined); }
                else closePanel();
                return;
            }
            // Clic sullo sfondo scuro (fuori dal foglio) = chiudi.
            if (openPanelName && e.target.classList && e.target.classList.contains('ms-panel')) closePanel();
        });
        // Entrambi in fase di cattura e in quest'ordine: il pannello gestisce Esc/Tab prima che la guardia
        // fermi la propagazione verso i listener legacy.
        doc.addEventListener('keydown', onPanelKeydown, true);
        doc.addEventListener('keydown', guardLegacyShortcuts, true);
        var panic_ = $('ms-panic');
        if (panic_) panic_.addEventListener('click', panic);

        state.subscribe('connection', renderConnection);
        state.subscribe('transport', renderTransport);
        state.subscribe('scene', renderDirty);
        state.subscribe('project', renderDirty);
    }

    /* ── Vista Suona (segnaposto finche' non arriva ui-live) ───────────── */
    function initLive() {
        var host = $('live-part-panel');
        var select = $('live-part-select');
        if (!host || !select || !partPanel) return;
        // Stesso pannello sonoro dell'inspector Studio: qui in versione moderna (ID con prefisso, campi numerici).
        livePanel = partPanel.mount(host, { partId: 0 });
        select.addEventListener('change', function () { livePanel.setPart(parseInt(select.value, 10)); });
        function renderScene() {
            var s = state.get('scene');
            setText($('live-scene-name'), s && s.name ? s.name : 'Nessuna scena caricata');
            var d = $('live-scene-dirty');
            if (d) d.hidden = !(s && s.dirty);
        }
        state.subscribe('scene', renderScene);
        renderScene();
    }

    /* ── Boot ──────────────────────────────────────────────────────────── */
    function placeholderSection(id, title, text, href, linkText) {
        library.registerSection(id, {
            title: title,
            render: function (box) {
                var p = doc.createElement('p');
                p.className = 'ms-note';
                p.textContent = text;
                box.appendChild(p);
                if (href) {
                    var a = doc.createElement('a');
                    a.className = 'ms-btn ms-btn-secondary';
                    a.href = href;
                    a.textContent = linkText;
                    box.appendChild(a);
                }
            }
        });
    }

    // Segnaposto: ui-live (Scene, SoundFont) e ui-studio (Progetti, MIDI) li sostituiscono con registerSection
    // sullo stesso id. Finche' non ci sono, le funzioni esistenti restano raggiungibili dalle pagine attuali.
    function registerDefaultSections() {
        if (!library) return;
        placeholderSection('scene', 'Scene', 'Le scene (preset completi dei 16 canali) si gestiscono oggi dalla pagina Preset: salva, richiama, cerca, rinomina, elimina.', '/presets', 'Apri la pagina Preset');
        placeholderSection('progetti', 'Progetti', 'I progetti nativi arriveranno con la fase Studio. Per ora Nuovo / Salva / Apri sono nel menu File di Studio.');
        placeholderSection('midi', 'MIDI', 'Gestione dei file MIDI (salva, importa, scarica, elimina) disponibile dal menu File di Studio.');
        placeholderSection('soundfont', 'SoundFont', 'Elenco e caricamento dei SoundFont si trovano oggi nella pagina Impostazioni.', '/settings', 'Apri Impostazioni');
    }

    function bootSync() {
        registerDefaultSections();
        // 1) Pannello Mix/Suono di Studio, stesso markup dell'originale ma generato dal modulo unico.
        var legacyHost = $('panel-mixer-body');
        if (legacyHost && partPanel) partPanel.mount(legacyHost, { legacy: true });
        // 2) Etichette snap dalla tabella unica (A12). Selezione iniziale come prima: Studio 1/32, piano roll 1/16.
        units.fillSnapSelect($('daw-snap-grid'), 0.125);
        units.fillSnapSelect($('pr-snap-grid'), 0.25);
    }

    function init() {
        if (initialized) return;
        initialized = true;
        bootSync();
        if (library) library.mount($('ms-library-tabs'), $('ms-library-body'));
        wireUi();
        initLive();
        renderConnection(); renderTransport(); renderDirty();
        var hash = (root.location && root.location.hash || '').replace('#', '');
        var start = VIEWS.indexOf(hash) >= 0 ? hash : state.getPref('lastView', DEFAULT_VIEW);
        showView(VIEWS.indexOf(start) >= 0 ? start : DEFAULT_VIEW);
        wireSocket();
    }

    MS.shell = {
        showView: showView,
        openPanel: openPanel,
        closePanel: closePanel,
        currentView: function () { return currentView; },
        isStudioVisible: function () { return currentView === 'studio' || currentView === 'editor'; },
        toast: toast,
        init: init,
        // Diagnostica per la verifica in browser (un solo socket, nessun listener in piu' dopo i cambi vista).
        debug: function () {
            var s = {};
            state.DOMAINS.forEach(function (d) { s[d] = state.subscriberCount(d); });
            return { view: currentView, panel: openPanelName, api: api.stats(), subscribers: s };
        }
    };

    // I file sono in fondo al <body>: il DOM sopra esiste gia'. Boot sincrono, prima di legacy_daw.js.
    init();
})(window);
