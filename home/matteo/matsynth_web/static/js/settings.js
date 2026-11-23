/* MatSynth - settings.js (fase ui-offline-settings)
 * Pannello Impostazioni della shell: Connessione, Audio e hardware, MIDI (porte e alias), Modalita' MIDI,
 * SoundFont (provvisorio fino alla sezione Libreria > SoundFont di ui-live).
 *
 * Regole:
 *  - Nessuna richiesta all'avvio della pagina: i dati si leggono solo quando il pannello si apre o si cambia
 *    scheda (nessun polling; sul Pi Zero 2 W ogni timer costa). L'unico ciclo temporizzato e' l'attesa del
 *    riavvio, limitata nel tempo e attiva solo dopo una richiesta esplicita.
 *  - Ogni azione passa da MatSynth.api.request e mostra l'esito reale: timeout/rete = "esito non confermato",
 *    mai successo. Una risposta 200 a "salva e riavvia" dice solo che il riavvio e' STATO RICHIESTO: il
 *    backend lo esegue in un thread dopo 1 s e non ne riporta l'esito (sudo puo' fallire), quindi l'esito si
 *    deduce osservando che il servizio sparisce e ritorna (evaluateRestart).
 *  - Le funzioni pure (sotto) non toccano DOM/rete e sono provate con `node --test tests/js`. */
(function (root, factory) {
    'use strict';
    var mod = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = mod;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.settings = mod;
        mod.init();
    }
})(typeof window !== 'undefined' ? window : this, function (root) {
    'use strict';

    /* ══════════════════ Parte pura ══════════════════ */

    var ALIAS_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;          // stessa regola di midi_router.ALIAS_RE
    var RESTART_DOWN_WAIT_MS = 20000;   // oltre questo tempo senza che il servizio sparisca: riavvio NON confermato
    var RESTART_TOTAL_MS = 90000;       // oltre questo tempo senza che torni: ancora irraggiungibile
    var SECTIONS = [
        { id: 'connessione', title: 'Connessione' },
        { id: 'audio', title: 'Audio e hardware' },
        { id: 'midi', title: 'MIDI' },
        { id: 'modalita', title: 'Modalità' },
        { id: 'soundfont', title: 'SoundFont' }
    ];

    function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

    function validateAlias(alias) {
        if (typeof alias !== 'string' || !alias) return 'Inserisci un nome breve (alias).';
        if (!ALIAS_RE.test(alias)) return 'Alias non valido: usa 1-32 caratteri tra minuscole, cifre e trattino, senza iniziare con il trattino.';
        return null;
    }

    // Etichette di 3.5.
    var PORT_STATUS = {
        available: { label: 'disponibile', tone: 'ok' },
        missing: { label: 'assente', tone: 'warn' },
        ambiguous: { label: 'ambigua', tone: 'warn' },
        unbound: { label: 'non associato', tone: 'dim' },
        offline: { label: 'errore', tone: 'error' }
    };
    function portStatusInfo(status) {
        return PORT_STATUS[status] || { label: status ? String(status) : 'sconosciuto', tone: 'dim' };
    }

    function descriptorLabel(d) {
        if (!isObj(d)) return 'Non associato';
        var c = d.client || '', p = d.port || '';
        if (c && p && c !== p) return c + ' · ' + p;
        return c || p || 'Non associato';
    }

    var EXCLUDED_LABEL = { system: 'di sistema', through: 'Midi Through', own: 'porta interna di MatSynth' };

    // Opzioni per associare un alias a una porta reale. Le uscite interne (FluidSynth) non si propongono
    // come esterne (3.4). Le porte escluse compaiono solo se la lista e' stata chiesta con ?all=1.
    function portChoices(ports, direction, alias) {
        var list = isObj(ports) && Array.isArray(ports[direction === 'output' ? 'outputs' : 'inputs'])
            ? ports[direction === 'output' ? 'outputs' : 'inputs'] : [];
        var out = [];
        list.forEach(function (p) {
            if (!isObj(p) || typeof p.name !== 'string') return;
            if (direction === 'output' && p.internal) return;
            var label = descriptorLabel(p.descriptor) || p.name;
            if (p.excluded) label += ' (' + (EXCLUDED_LABEL[p.excluded] || 'esclusa') + ')';
            var aliases = Array.isArray(p.aliases) ? p.aliases : [];
            out.push({ value: p.name, label: label, selected: aliases.indexOf(alias) >= 0 });
        });
        return out;
    }

    function deviceBody(direction, portName) {
        var body = {};
        body[direction] = portName ? { from_port: portName } : null;
        return body;
    }

    function modeLabel(m) { return m === 'managed' ? 'Managed' : m === 'legacy' ? 'Legacy' : '—'; }

    function modeView(info) {
        var eff = isObj(info) ? info.effective : null;
        var conf = isObj(info) ? info.configured : null;
        var restart = !!(isObj(info) && info.restart_required);
        var line = 'In uso: ' + modeLabel(eff) + ' · Configurata: ' + modeLabel(conf);
        if (restart) line += ' · riavvio necessario per applicarla';
        return { effective: eff, configured: conf, restartRequired: restart, line: line };
    }

    function cleanIps(list) {
        return (Array.isArray(list) ? list : []).filter(function (x) { return typeof x === 'string' && x; });
    }

    // Indirizzi di accesso. `loc` = {protocol, port}. Non e' un test di connettivita': sono gli indirizzi che
    // il Pi dichiara di avere (hostname -I).
    function networkView(body, loc) {
        loc = loc || {};
        var proto = loc.protocol || 'http:';
        var port = loc.port ? ':' + loc.port : '';
        var host = isObj(body) && typeof body.hostname === 'string' && body.hostname ? body.hostname : '';
        var addresses = cleanIps(isObj(body) ? body.ips : []).map(function (ip) {
            var h = ip.indexOf(':') >= 0 ? '[' + ip + ']' : ip;
            return { ip: ip, url: proto + '//' + h + port + '/' };
        });
        return {
            hostname: host,
            mdns: host ? host + '.local' : '',
            mdnsUrl: host ? proto + '//' + host + '.local' + port + '/' : '',
            addresses: addresses,
            hasNetwork: addresses.length > 0
        };
    }

    // Elenco schede (/api/audio_devices) o tastiere ALSA (/api/midi_devices): stessa forma {devices:[{id,name}], current}.
    function deviceListView(body) {
        var options = [];
        if (isObj(body) && Array.isArray(body.devices)) {
            body.devices.forEach(function (d) {
                if (isObj(d) && d.id !== undefined && d.id !== null && d.id !== '') {
                    options.push({ id: String(d.id), name: String(d.name || d.id) });
                }
            });
        }
        var current = isObj(body) && body.current ? String(body.current) : '';
        var known = options.some(function (o) { return o.id === current; });
        return { options: options, current: current, currentMissing: !!current && !known };
    }

    // Un riavvio ferma suono e DAW: bloccato durante Play/Record (fuori dal flusso live), avviso se ci sono
    // modifiche non salvate (la sessione DAW e' in RAM del processo che si riavvia).
    function restartGuard(snap) {
        snap = snap || {};
        var t = snap.transport || {};
        var warnings = [];
        if (t.is_recording || t.is_playing) {
            return { blocked: true,
                     reason: (t.is_recording ? 'Registrazione' : 'Riproduzione') + ' in corso: ferma il trasporto prima di riavviare.',
                     warnings: warnings };
        }
        if (snap.project && snap.project.dirty) warnings.push('Il progetto ha modifiche non salvate: andranno perse.');
        if (snap.scene && snap.scene.dirty) warnings.push('La scena ha modifiche non salvate: andranno perse.');
        return { blocked: false, reason: '', warnings: warnings };
    }

    // Esito del riavvio dedotto da cio' che si osserva (vedi intestazione): confermato solo se il servizio e'
    // sparito E ritornato.
    function evaluateRestart(obs) {
        var elapsed = obs.elapsed || 0;
        if (obs.sawDown && obs.up) return 'confirmed';
        if (!obs.sawDown && elapsed >= RESTART_DOWN_WAIT_MS) return 'unconfirmed';
        if (obs.sawDown && !obs.up && elapsed >= RESTART_TOTAL_MS) return 'unreachable';
        return 'waiting';
    }

    // Una richiesta fallita per rete o per server spento conta come "servizio giu'"; un 4xx significa che risponde.
    function isServiceDown(res) {
        if (!res) return true;
        if (res.ok) return false;
        return !!res.unknown_outcome || !res.http || res.http >= 500;
    }

    function errorText(res) {
        if (!res || res.ok) return '';
        var e = res.error || {};
        var msg = e.message || res.message || 'Errore';
        var d = e.details || {};
        if (e.code === 'DEVICE_IN_USE' && Array.isArray(d.parts) && d.parts.length) {
            msg += ' (parti: ' + d.parts.map(function (p) { return Number(p) + 1; }).join(', ') + ')';
        }
        if (res.unknown_outcome) msg += ' Controlla lo stato prima di ripetere l\'azione.';
        return msg;
    }

    var api = {
        SECTIONS: SECTIONS, ALIAS_RE: ALIAS_RE, RESTART_DOWN_WAIT_MS: RESTART_DOWN_WAIT_MS, RESTART_TOTAL_MS: RESTART_TOTAL_MS,
        validateAlias: validateAlias, portStatusInfo: portStatusInfo, descriptorLabel: descriptorLabel,
        portChoices: portChoices, deviceBody: deviceBody, modeView: modeView, networkView: networkView,
        deviceListView: deviceListView, restartGuard: restartGuard, evaluateRestart: evaluateRestart,
        isServiceDown: isServiceDown, errorText: errorText
    };

    /* ══════════════════ Parte DOM (solo in pagina) ══════════════════ */

    api.init = function init() {
        var MS = root.MatSynth, doc = root.document;
        var http = MS.api, state = MS.state;
        var panel = doc.getElementById('ms-panel-settings');
        var tabsEl = doc.getElementById('st-tabs');
        var bodyEl = doc.getElementById('st-body');
        if (!panel || !tabsEl || !bodyEl) return;

        var active = null;            // scheda mostrata
        var pendingSection = null;    // scheda richiesta da openPanel('settings', section)
        var loadGen = 0;              // risposte di rendering vecchie vengono ignorate
        var restartWatch = null;      // un solo riavvio osservato alla volta
        var portsTimer = null;
        var tabUnsub = null;          // iscrizioni di stato della scheda corrente
        var softRefresh = null;       // rilettura della scheda corrente senza perdere i messaggi di esito

        function h(tag, attrs, kids) {
            var el = doc.createElement(tag);
            if (attrs) Object.keys(attrs).forEach(function (k) {
                var v = attrs[k];
                if (v === null || v === undefined || v === false) return;
                if (k === 'class') el.className = v;
                else if (k === 'text') el.textContent = v;
                else el.setAttribute(k, v === true ? '' : v);
            });
            (kids || []).forEach(function (c) { if (c) el.appendChild(typeof c === 'string' ? doc.createTextNode(c) : c); });
            return el;
        }
        function icon(name) { return h('i', { class: 'fas fa-' + name, 'aria-hidden': 'true' }); }
        function btn(label, cls, iconName, attrs) {
            var a = { type: 'button', class: 'ms-btn ' + (cls || 'ms-btn-secondary') };
            if (attrs) Object.keys(attrs).forEach(function (k) { a[k] = attrs[k]; });
            return h('button', a, [iconName ? icon(iconName) : null, h('span', { text: label })]);
        }
        function card(title, kids) {
            return h('section', { class: 'ms-card st-card' }, [h('h3', { class: 'ms-card-title', text: title })].concat(kids));
        }
        function note(text) { return h('p', { class: 'ms-note', text: text }); }
        function status() { return h('p', { class: 'st-status', role: 'status', 'aria-live': 'polite' }); }
        function setStatus(el, kind, text) {
            if (!el) return;
            el.setAttribute('data-kind', kind || '');
            el.textContent = text || '';
        }
        function badge(info) { return h('span', { class: 'st-badge', 'data-tone': info.tone, text: info.label }); }
        function field(label, control, id) {
            return h('div', { class: 'ms-field' }, [h('label', { for: id, text: label }), control]);
        }

        // Esegue un'azione asincrona con il pulsante disattivato: niente doppio invio.
        function busy(button, fn) {
            if (button.disabled) return;
            button.disabled = true;
            var done = function () { button.disabled = false; };
            var p;
            try { p = fn(); } catch (e) { done(); throw e; }
            Promise.resolve(p).then(done, done);
        }

        // Blocco di conferma esplicita nello stesso pannello (un window.confirm sul touch e' facile da accettare
        // per sbaglio e non spiega cosa succede). Il focus parte su "Annulla".
        function askConfirm(host, opts) {
            var old = host.querySelector('.st-confirm');
            if (old) old.remove();
            var cancel = btn('Annulla', 'ms-btn-secondary');
            var ok = btn(opts.confirmLabel, 'st-danger', opts.icon || 'power-off');
            var box = h('div', { class: 'st-confirm', role: 'alertdialog', 'aria-label': opts.title }, [
                h('strong', { text: opts.title }),
                h('p', { text: opts.text })
            ]);
            (opts.warnings || []).forEach(function (w) { box.appendChild(h('p', { class: 'st-warn', text: w })); });
            if (opts.blocked) {
                box.appendChild(h('p', { class: 'st-warn', text: opts.blocked }));
                ok.disabled = true;
            }
            box.appendChild(h('div', { class: 'ms-actions' }, [ok, cancel]));
            cancel.addEventListener('click', function () { box.remove(); });
            ok.addEventListener('click', function () { box.remove(); opts.onConfirm(); });
            host.appendChild(box);
            cancel.focus();
            return box;
        }

        function snapshotGuard() {
            return restartGuard({ transport: state.get('transport'), project: state.get('project'), scene: state.get('scene') });
        }

        /* ── Riavvio del servizio e osservazione dell'esito ─────────────── */
        function watchRestart(out, onBack) {
            if (restartWatch) restartWatch.cancel();
            var start = Date.now();
            var sawDown = false, stopped = false, timer = null;
            var unsub = state.subscribe('connection', function (c) { if (c && c.ever_connected && !c.connected) sawDown = true; });
            function finish(kind, text) {
                stopped = true;
                clearTimeout(timer);
                unsub();
                restartWatch = null;
                setStatus(out, kind, text);
            }
            restartWatch = { cancel: function () { stopped = true; clearTimeout(timer); unsub(); } };
            function tick() {
                if (stopped) return;
                http.request('GET', '/api/system/status', null, { timeout: 3000 }).then(function (r) {
                    if (stopped) return;
                    var down = isServiceDown(r);
                    if (down) sawDown = true;
                    var verdict = evaluateRestart({ elapsed: Date.now() - start, sawDown: sawDown, up: !down });
                    if (verdict === 'confirmed') {
                        var b = r.body || {};
                        var console_ = b.synth && b.synth.console === 'available' ? 'console FluidSynth disponibile' : 'console FluidSynth NON disponibile';
                        finish(b.synth && b.synth.console === 'available' ? 'ok' : 'warn',
                            'Servizio di nuovo raggiungibile dopo il riavvio; ' + console_ + '. Ricarica la pagina per rileggere lo stato.');
                        if (onBack) onBack(r);
                    } else if (verdict === 'unconfirmed') {
                        finish('error', 'Riavvio NON confermato: il servizio non si è mai fermato in ' + Math.round(RESTART_DOWN_WAIT_MS / 1000) +
                            ' s. Potrebbe non essere stato eseguito (permessi di sistema). Le modifiche salvate valgono dal prossimo riavvio.');
                    } else if (verdict === 'unreachable') {
                        finish('error', 'Il servizio non è tornato raggiungibile entro ' + Math.round(RESTART_TOTAL_MS / 1000) +
                            ' s. Controlla il dispositivo (alimentazione, rete) e ricarica la pagina.');
                    } else {
                        setStatus(out, 'busy', sawDown ? 'Riavvio in corso: servizio fermo, attendo che torni…' : 'Riavvio richiesto: attendo che il servizio si fermi e riparta…');
                        timer = setTimeout(tick, 2000);
                    }
                });
            }
            timer = setTimeout(tick, 1500);
        }

        // POST /api/save_hardware: salva i soli campi passati e riavvia. 200 = riavvio RICHIESTO, non eseguito.
        function requestRestart(body, out, onBack) {
            setStatus(out, 'busy', 'Invio della richiesta di riavvio…');
            return http.request('POST', '/api/save_hardware', body).then(function (r) {
                if (r.ok) {
                    setStatus(out, 'busy', 'Richiesta accettata, riavvio non ancora confermato…');
                    watchRestart(out, onBack);
                } else if (r.unknown_outcome) {
                    setStatus(out, 'warn', errorText(r) + ' Osservo se il servizio si riavvia…');
                    watchRestart(out, onBack);
                } else {
                    setStatus(out, 'error', 'Riavvio non richiesto: ' + errorText(r));
                }
            });
        }

        function reloadButton() {
            return btn('Ricarica la pagina', 'ms-btn-secondary', 'sync-alt', { 'data-act': 'reload' });
        }

        /* ── Scheda: Connessione ────────────────────────────────────────── */
        function renderConnessione(host, gen) {
            var out = status();
            var conn = state.get('connection');
            var live = h('p', { class: 'st-line' }, [h('span', { class: 'st-key', text: 'Collegamento a MatSynth' })]);
            function drawLive(c) {
                var old = live.querySelector('.st-badge');
                if (old) old.remove();
                live.appendChild(badge(c.connected ? { label: 'attivo', tone: 'ok' } : { label: c.ever_connected ? 'perso' : 'in attesa', tone: c.ever_connected ? 'error' : 'warn' }));
            }
            drawLive(conn);
            // Si aggiorna da solo (la scheda puo' aprirsi prima che il socket si colleghi); l'iscrizione cade al cambio scheda.
            tabUnsub = state.subscribe('connection', drawLive);
            var netBox = h('div', { class: 'st-net' }, [h('p', { class: 'ms-note', text: 'Lettura della rete…' })]);
            var refresh = btn('Aggiorna', 'ms-btn-secondary', 'sync-alt');
            refresh.addEventListener('click', function () { busy(refresh, function () { return load(); }); });
            host.appendChild(card('Rete', [live, netBox, h('div', { class: 'ms-actions' }, [refresh]), out]));
            host.appendChild(card('Aprire MatSynth da un altro dispositivo', [
                note('Collega telefono o tablet alla stessa rete (o all\'hotspot del dispositivo) e apri uno degli indirizzi qui sopra. ' +
                     'Gli indirizzi sono quelli dichiarati dal sistema: non dimostrano che la rete funzioni.')]));

            function load() {
                setStatus(out, 'busy', 'Lettura in corso…');
                return http.request('GET', '/api/network').then(function (r) {
                    if (gen !== loadGen) return;
                    netBox.textContent = '';
                    if (!r.ok) { setStatus(out, 'error', 'Impossibile leggere la rete: ' + errorText(r)); return; }
                    setStatus(out, '', '');
                    var v = networkView(r.body, { protocol: root.location.protocol, port: root.location.port });
                    netBox.appendChild(h('p', { class: 'st-line' }, [
                        h('span', { class: 'st-key', text: 'Rete' }),
                        badge(v.hasNetwork ? { label: 'indirizzo assegnato', tone: 'ok' } : { label: 'nessuna rete', tone: 'warn' })]));
                    if (v.hostname) {
                        netBox.appendChild(h('p', { class: 'st-line' }, [
                            h('span', { class: 'st-key', text: 'Nome' }), h('span', { class: 'st-val', text: v.mdns })]));
                    }
                    if (v.hasNetwork) {
                        v.addresses.forEach(function (a) {
                            netBox.appendChild(h('p', { class: 'st-line' }, [
                                h('span', { class: 'st-key', text: 'Indirizzo' }), h('span', { class: 'st-val', text: a.ip }),
                                h('a', { class: 'st-link', href: a.url, text: a.url })]));
                        });
                    } else {
                        netBox.appendChild(note('Nessun indirizzo di rete: il dispositivo è raggiungibile solo dal collegamento USB locale.'));
                    }
                });
            }
            load();
        }

        /* ── Scheda: Audio e hardware ───────────────────────────────────── */
        function renderAudio(host, gen) {
            var out = status();
            var audioSel = h('select', { id: 'st-audio', class: 'ms-select', disabled: true }, [h('option', { text: 'Ricerca schede audio…' })]);
            var alsaSel = h('select', { id: 'st-alsa-kb', class: 'ms-select', disabled: true }, [h('option', { text: 'Ricerca tastiere…' })]);
            var audioState = null, alsaState = null;
            var save = btn('Salva e riavvia il servizio', 'st-primary', 'save');
            save.disabled = true;
            var box = h('div');

            host.appendChild(card('Uscita audio (ALSA)', [
                note('Scheda su cui FluidSynth emette l\'audio. La scelta si applica solo riavviando il servizio.'),
                field('Scheda audio', audioSel, 'st-audio')]));
            host.appendChild(card('Tastiera per il collegamento ALSA (modalità Legacy)', [
                note('In modalità Legacy lo script di avvio collega questa tastiera a FluidSynth. Le associazioni per alias (scheda MIDI) valgono per il router di MatSynth e sono un\'altra cosa.'),
                field('Tastiera USB', alsaSel, 'st-alsa-kb')]));
            host.appendChild(card('Riavvio', [
                note('Salvare riavvia il servizio MatSynth: il suono si interrompe per alcuni secondi. Fallo fuori dall\'uso dal vivo.'),
                h('div', { class: 'ms-actions' }, [save]), box, out]));

            function fill(sel, view, emptyText, keepLabel) {
                sel.textContent = '';
                sel.disabled = false;
                if (!view.options.length) {
                    sel.appendChild(h('option', { value: '', text: emptyText }));
                    sel.disabled = true;
                    return;
                }
                if (keepLabel) sel.appendChild(h('option', { value: '', text: keepLabel }));
                if (view.currentMissing) sel.appendChild(h('option', { value: view.current, text: 'Salvata: ' + view.current + ' (non rilevata)' }));
                view.options.forEach(function (o) { sel.appendChild(h('option', { value: o.id, text: o.name })); });
                sel.value = view.current || '';
                if (sel.value !== (view.current || '')) sel.value = '';
            }

            Promise.all([http.request('GET', '/api/audio_devices'), http.request('GET', '/api/midi_devices')]).then(function (rs) {
                if (gen !== loadGen) return;
                var problems = [];
                if (rs[0].ok) { audioState = deviceListView(rs[0].body); fill(audioSel, audioState, 'Nessuna scheda audio rilevata', null); }
                else { audioSel.textContent = ''; audioSel.appendChild(h('option', { text: 'Non disponibile' })); problems.push('schede audio: ' + errorText(rs[0])); }
                if (rs[1].ok) { alsaState = deviceListView(rs[1].body); fill(alsaSel, alsaState, 'Nessuna tastiera rilevata', 'Lascia invariata'); }
                else { alsaSel.textContent = ''; alsaSel.appendChild(h('option', { text: 'Non disponibile' })); problems.push('tastiere: ' + errorText(rs[1])); }
                if (problems.length) setStatus(out, 'error', 'Lettura non riuscita (' + problems.join('; ') + ').');
                save.disabled = false;
            });

            save.addEventListener('click', function () {
                var body = {};
                if (audioState && audioSel.value && audioSel.value !== audioState.current) body.audio = audioSel.value;
                if (alsaState && alsaSel.value && alsaSel.value !== alsaState.current) body.midi = alsaSel.value;
                var changed = Object.keys(body).length > 0;
                var g = snapshotGuard();
                askConfirm(box, {
                    title: 'Riavviare il servizio MatSynth?',
                    text: (changed ? 'Verranno salvate le modifiche e il servizio sarà riavviato. ' : 'Nessuna modifica da salvare: il servizio sarà solo riavviato. ') +
                          'Audio e MIDI si interrompono per alcuni secondi.',
                    warnings: g.warnings, blocked: g.blocked ? g.reason : '',
                    confirmLabel: 'Conferma riavvio',
                    onConfirm: function () { busy(save, function () { return requestRestart(body, out, function () { box.appendChild(reloadButton()); }); }); }
                });
            });
        }

        /* ── Scheda: MIDI ───────────────────────────────────────────────── */
        function renderMidi(host, gen) {
            var out = status();
            var showAll = !!renderMidi.showAll;
            var portsBox = h('div', { class: 'st-ports' }, [note('Lettura delle porte…')]);
            var devBox = h('div', { class: 'st-devices' });
            var allToggle = h('input', { type: 'checkbox', id: 'st-all-ports' });
            allToggle.checked = showAll;
            allToggle.addEventListener('change', function () { renderMidi.showAll = allToggle.checked; load(); });
            var refresh = btn('Aggiorna', 'ms-btn-secondary', 'sync-alt');
            refresh.addEventListener('click', function () { busy(refresh, function () { return load(); }); });

            host.appendChild(card('Porte MIDI rilevate', [
                portsBox,
                h('label', { class: 'st-check', for: 'st-all-ports' }, [allToggle, h('span', { text: 'Mostra anche le porte escluse (sistema, Midi Through, interne)' })]),
                h('div', { class: 'ms-actions' }, [refresh])]));
            host.appendChild(card('Dispositivi (alias)', [
                note('Un alias rappresenta uno strumento (tastiera, modulo). Le parti usano l\'alias, non il nome della porta: se la porta cambia numero o nome ALSA basta riassociare qui.'),
                devBox, out]));
            host.appendChild(newDeviceCard());

            function newDeviceCard() {
                var alias = h('input', { id: 'st-new-alias', class: 'ms-select', type: 'text', maxlength: '32', autocapitalize: 'none', autocomplete: 'off', placeholder: 'es. fantom' });
                var label = h('input', { id: 'st-new-label', class: 'ms-select', type: 'text', maxlength: '64', placeholder: 'es. FANTOM-08' });
                var add = btn('Aggiungi dispositivo', 'ms-btn-secondary', 'plus');
                var st = status();
                add.addEventListener('click', function () {
                    var a = alias.value.trim(), l = label.value.trim() || a;
                    var bad = validateAlias(a);
                    if (bad) { setStatus(st, 'error', bad); alias.focus(); return; }
                    busy(add, function () {
                        setStatus(st, 'busy', 'Salvataggio…');
                        return http.request('PUT', '/api/midi/devices/' + encodeURIComponent(a), { label: l }).then(function (r) {
                            if (!r.ok) { setStatus(st, 'error', 'Non aggiunto: ' + errorText(r)); return; }
                            setStatus(st, 'ok', 'Dispositivo "' + a + '" ' + (r.http === 201 ? 'creato' : 'aggiornato') + '. Ora associa la porta.');
                            alias.value = ''; label.value = '';
                            load();
                        });
                    });
                });
                return card('Nuovo dispositivo', [field('Alias (nome breve)', alias, 'st-new-alias'), field('Etichetta', label, 'st-new-label'),
                    h('div', { class: 'ms-actions' }, [add]), st]);
            }

            function deviceDirection(d, direction, ports, rowStatus) {
                var title = direction === 'input' ? 'Ingresso' : 'Uscita';
                var st = (d.status && d.status[direction]) || 'unbound';
                var info = portStatusInfo(st);
                var selId = 'st-' + d.alias + '-' + direction;
                var sel = h('select', { id: selId, class: 'ms-select' });
                var choices = portChoices(ports, direction, d.alias);
                sel.appendChild(h('option', { value: '', text: 'Nessuna (non associato)' }));
                var cur = '';
                choices.forEach(function (c) {
                    sel.appendChild(h('option', { value: c.value, text: c.label }));
                    if (c.selected && !cur) cur = c.value;
                });
                sel.value = cur;
                var assoc = btn('Associa', 'ms-btn-secondary', 'link');
                assoc.addEventListener('click', function () {
                    busy(assoc, function () {
                        setStatus(rowStatus, 'busy', 'Salvataggio associazione…');
                        return http.request('PUT', '/api/midi/devices/' + encodeURIComponent(d.alias), deviceBody(direction, sel.value)).then(function (r) {
                            if (!r.ok) { setStatus(rowStatus, 'error', 'Associazione non salvata: ' + errorText(r)); return; }
                            setStatus(rowStatus, 'ok', title + ' di "' + d.alias + '" ' + (sel.value ? 'associato.' : 'scollegato dall\'alias.'));
                            load(true);
                        });
                    });
                });
                var parts = [
                    h('p', { class: 'st-line' }, [h('span', { class: 'st-key', text: title }), badge(info),
                        h('span', { class: 'st-val', text: descriptorLabel(d[direction]) })]),
                    field('Porta ' + (direction === 'input' ? 'di ingresso' : 'di uscita'), sel, selId),
                    h('div', { class: 'ms-actions' }, [assoc])
                ];
                if (st === 'ambiguous' && d.candidates && Array.isArray(d.candidates[direction]) && d.candidates[direction].length) {
                    var cid = selId + '-cand';
                    var cs = h('select', { id: cid, class: 'ms-select' });
                    d.candidates[direction].forEach(function (n) { cs.appendChild(h('option', { value: n, text: n })); });
                    var pick = btn('Usa questa porta', 'ms-btn-secondary', 'check');
                    pick.addEventListener('click', function () {
                        busy(pick, function () {
                            setStatus(rowStatus, 'busy', 'Selezione…');
                            return http.request('POST', '/api/midi/devices/' + encodeURIComponent(d.alias) + '/select', { direction: direction, port: cs.value }).then(function (r) {
                                if (!r.ok) { setStatus(rowStatus, 'error', 'Scelta non applicata: ' + errorText(r)); return; }
                                setStatus(rowStatus, 'ok', 'Porta scelta (vale fino alla prossima riconnessione).');
                                load(true);
                            });
                        });
                    });
                    parts.push(note('Più porte corrispondono a questo strumento: scegline una. La scelta vale solo fino alla prossima riconnessione.'));
                    parts.push(field('Porta da usare', cs, cid));
                    parts.push(h('div', { class: 'ms-actions' }, [pick]));
                }
                return h('div', { class: 'st-dir' }, parts);
            }

            function deviceCard(d, ports) {
                var rowStatus = status();
                var box = h('div', { class: 'st-device' }, [
                    h('h4', { class: 'st-device-title' }, [h('span', { text: d.label || d.alias }), h('code', { text: d.alias })]),
                    deviceDirection(d, 'input', ports, rowStatus),
                    deviceDirection(d, 'output', ports, rowStatus)
                ]);
                var del = btn('Elimina dispositivo', 'ms-btn-secondary', 'trash');
                del.addEventListener('click', function () {
                    askConfirm(box, {
                        title: 'Eliminare "' + d.alias + '"?', text: 'Le associazioni di questo strumento vanno perse. Se una parte lo usa, MatSynth rifiuta l\'eliminazione.',
                        confirmLabel: 'Elimina', icon: 'trash',
                        onConfirm: function () {
                            busy(del, function () {
                                setStatus(rowStatus, 'busy', 'Eliminazione…');
                                return http.request('DELETE', '/api/midi/devices/' + encodeURIComponent(d.alias)).then(function (r) {
                                    if (!r.ok) { setStatus(rowStatus, 'error', 'Non eliminato: ' + errorText(r)); return; }
                                    setStatus(out, 'ok', 'Dispositivo "' + d.alias + '" eliminato.');
                                    load(true);
                                });
                            });
                        }
                    });
                });
                box.appendChild(h('div', { class: 'ms-actions' }, [del]));
                box.appendChild(rowStatus);
                return box;
            }

            function renderPorts(p) {
                portsBox.textContent = '';
                [['inputs', 'Ingressi'], ['outputs', 'Uscite']].forEach(function (pair) {
                    var list = Array.isArray(p[pair[0]]) ? p[pair[0]] : [];
                    portsBox.appendChild(h('h4', { class: 'st-sub', text: pair[1] }));
                    if (!list.length) { portsBox.appendChild(note('Nessuna porta rilevata.')); return; }
                    var ul = h('ul', { class: 'st-list' });
                    list.forEach(function (x) {
                        var li = h('li', null, [h('span', { class: 'st-val', text: descriptorLabel(x.descriptor) || x.name })]);
                        if (x.internal) li.appendChild(h('span', { class: 'st-badge', 'data-tone': 'dim', text: 'interna' }));
                        if (x.excluded) li.appendChild(h('span', { class: 'st-badge', 'data-tone': 'dim', text: EXCLUDED_LABEL[x.excluded] || 'esclusa' }));
                        (x.aliases || []).forEach(function (a) { li.appendChild(h('span', { class: 'st-badge', 'data-tone': 'ok', text: a })); });
                        ul.appendChild(li);
                    });
                    portsBox.appendChild(ul);
                });
            }

            function load(keepStatus) {
                if (!keepStatus) setStatus(out, 'busy', 'Lettura in corso…');
                var q = renderMidi.showAll ? '?all=1' : '';
                return Promise.all([http.request('GET', '/api/midi/ports' + q), http.request('GET', '/api/midi/devices')]).then(function (rs) {
                    if (gen !== loadGen) return;
                    if (!keepStatus) setStatus(out, '', '');
                    if (rs[0].ok && rs[0].body) renderPorts(rs[0].body);
                    else { portsBox.textContent = ''; portsBox.appendChild(h('p', { class: 'st-status', 'data-kind': 'error', text: 'Porte non lette: ' + errorText(rs[0]) })); }
                    devBox.textContent = '';
                    if (!rs[1].ok) { setStatus(out, 'error', 'Dispositivi non letti: ' + errorText(rs[1])); return; }
                    var devs = rs[1].body && Array.isArray(rs[1].body.devices) ? rs[1].body.devices : [];
                    if (!devs.length) devBox.appendChild(note('Nessun dispositivo registrato. Aggiungine uno qui sotto e associa la porta.'));
                    devs.forEach(function (d) { devBox.appendChild(deviceCard(d, rs[0].ok ? rs[0].body : null)); });
                });
            }
            softRefresh = function () { load(true); };
            load();
        }

        /* ── Scheda: Modalità MIDI ──────────────────────────────────────── */
        function renderModalita(host, gen) {
            var out = status();
            var infoBox = h('div', { class: 'st-mode-info' }, [note('Lettura dello stato…')]);
            var choices = h('div', { class: 'st-mode-choices' });
            var restartBox = h('div');
            var restartOut = status();
            host.appendChild(card('Modalità MIDI', [infoBox, choices, out]));
            host.appendChild(card('Riavvio', [
                note('Il cambio di modalità vale solo dopo il riavvio del servizio: lo script di avvio rilegge la configurazione. Nessun riavvio parte da solo.'),
                restartBox, restartOut]));

            var MODES = [
                { id: 'legacy', title: 'Legacy (predefinita)', text: 'Come oggi: la tastiera è collegata a FluidSynth direttamente da ALSA all\'avvio. È la modalità sicura e anche il ritorno in caso di problemi.' },
                { id: 'managed', title: 'Managed (sperimentale)', text: 'Il router di MatSynth gestisce tastiera, monitor per parte e uscite esterne; ALSA non collega più la tastiera. NON è ancora verificata su hardware reale.' }
            ];

            function draw(sys) {
                var mv = modeView(sys.midi_mode);
                infoBox.textContent = '';
                infoBox.appendChild(h('p', { class: 'st-line' }, [h('span', { class: 'st-key', text: 'In uso' }), badge({ label: modeLabel(mv.effective), tone: 'ok' })]));
                infoBox.appendChild(h('p', { class: 'st-line' }, [h('span', { class: 'st-key', text: 'Configurata' }),
                    badge({ label: modeLabel(mv.configured), tone: mv.restartRequired ? 'warn' : 'ok' })]));
                if (mv.restartRequired) infoBox.appendChild(h('p', { class: 'st-warn', text: 'La modalità configurata è diversa da quella in uso: serve il riavvio del servizio.' }));
                (sys.warnings || []).forEach(function (w) {
                    infoBox.appendChild(h('p', { class: 'st-warn', text: (w && w.message) || (w && w.code) || String(w) }));
                });
                choices.textContent = '';
                MODES.forEach(function (m) {
                    var selected = mv.configured === m.id;
                    var b = h('button', { type: 'button', class: 'st-mode-btn', 'aria-pressed': selected ? 'true' : 'false', 'data-mode': m.id }, [
                        h('strong', { text: m.title + (selected ? ' — configurata' : '') }), h('span', { text: m.text })]);
                    b.addEventListener('click', function () { chooseMode(m, mv, b); });
                    choices.appendChild(b);
                });
                restartBox.textContent = '';
                var rb = btn('Riavvia il servizio', 'st-primary', 'power-off');
                rb.disabled = !mv.restartRequired;
                rb.addEventListener('click', function () {
                    var g = snapshotGuard();
                    askConfirm(restartBox, {
                        title: 'Riavviare il servizio MatSynth?',
                        text: 'Il servizio si riavvia e applica la modalità ' + modeLabel(mv.configured) + '. Il suono si interrompe per alcuni secondi.' +
                              (mv.configured === 'managed' ? ' Se qualcosa non va, torna a Legacy da questa scheda e riavvia di nuovo.' : ''),
                        warnings: g.warnings, blocked: g.blocked ? g.reason : '', confirmLabel: 'Conferma riavvio',
                        onConfirm: function () {
                            busy(rb, function () { return requestRestart({}, restartOut, function () { restartBox.appendChild(reloadButton()); }); });
                        }
                    });
                });
                restartBox.appendChild(h('div', { class: 'ms-actions' }, [rb]));
                if (!mv.restartRequired) restartBox.appendChild(note('Nessun riavvio necessario: modalità configurata e in uso coincidono.'));
            }

            function put(mode) {
                setStatus(out, 'busy', 'Salvataggio della modalità…');
                return http.request('PUT', '/api/system/mode', { mode: mode }).then(function (r) {
                    if (!r.ok) { setStatus(out, 'error', 'Modalità non salvata: ' + errorText(r)); return; }
                    setStatus(out, 'ok', 'Modalità salvata come configurata. Non è ancora in uso: serve il riavvio.');
                    return load(true);
                });
            }

            function chooseMode(m, mv, button) {
                if (mv.configured === m.id) return;
                if (m.id === 'managed') {
                    askConfirm(choices, {
                        title: 'Impostare Managed?',
                        text: 'Managed è sperimentale e non è stata ancora verificata su hardware reale. Dopo il riavvio la tastiera suona solo tramite il router di MatSynth. ' +
                              'Rollback: scegli Legacy e riavvia (oppure rimuovi la chiave midi_mode da last_state.json a mano sul dispositivo).',
                        confirmLabel: 'Imposta Managed', icon: 'check',
                        onConfirm: function () { busy(button, function () { return put('managed'); }); }
                    });
                } else {
                    busy(button, function () { return put('legacy'); });
                }
            }

            function load(keep) {
                if (!keep) setStatus(out, 'busy', 'Lettura in corso…');
                return http.request('GET', '/api/system/status').then(function (r) {
                    if (gen !== loadGen) return;
                    if (!r.ok || !r.body || !r.body.midi_mode) {
                        infoBox.textContent = '';
                        setStatus(out, 'error', 'Stato non letto: ' + (r.ok ? 'risposta senza modalità (backend precedente?)' : errorText(r)));
                        return;
                    }
                    if (!keep) setStatus(out, '', '');
                    draw(r.body);
                });
            }
            load();
        }

        /* ── Scheda: SoundFont (provvisoria) ────────────────────────────── */
        function renderSoundfont(host, gen) {
            var out = status();
            var sel = h('select', { id: 'st-sf2', class: 'ms-select', disabled: true }, [h('option', { text: 'Ricerca SoundFont…' })]);
            var load = btn('Carica SoundFont', 'st-primary', 'folder-open');
            load.disabled = true;
            var box = h('div');
            host.appendChild(card('SoundFont', [
                note('Provvisorio: la gestione dei SoundFont passerà alla Libreria. Caricare un SoundFont scarica quello attuale: fermalo prima.'),
                field('File .sf2 / .sf3', sel, 'st-sf2'), h('div', { class: 'ms-actions' }, [load]), box, out]));
            var current = '';
            Promise.all([http.request('GET', '/list_sf2'), http.request('GET', '/get_state')]).then(function (rs) {
                if (gen !== loadGen) return;
                sel.textContent = '';
                var files = rs[0].ok && Array.isArray(rs[0].body) ? rs[0].body : null;
                if (!files) { setStatus(out, 'error', 'Elenco non letto: ' + errorText(rs[0])); sel.appendChild(h('option', { text: 'Non disponibile' })); return; }
                current = rs[1].ok && rs[1].body && typeof rs[1].body.font === 'string' ? rs[1].body.font : '';
                if (!files.length) { sel.appendChild(h('option', { text: 'Nessun SoundFont trovato' })); return; }
                files.forEach(function (f) { sel.appendChild(h('option', { value: f, text: f })); });
                if (current && files.indexOf(current) >= 0) sel.value = current;
                sel.disabled = false; load.disabled = false;
                if (current) setStatus(out, '', 'Attuale: ' + current);
            });
            load.addEventListener('click', function () {
                var file = sel.value;
                if (!file) return;
                var g = snapshotGuard();
                askConfirm(box, {
                    title: 'Caricare "' + file + '"?', text: 'Il SoundFont attuale viene scaricato e il nuovo caricato: il suono si interrompe per qualche secondo.',
                    warnings: g.warnings, blocked: g.blocked ? g.reason : '', confirmLabel: 'Conferma caricamento', icon: 'folder-open',
                    onConfirm: function () {
                        busy(load, function () {
                            setStatus(out, 'busy', 'Caricamento in corso (fino a 30 s)…');
                            return http.request('GET', '/load_sf2/' + encodeURIComponent(file)).then(function (r) {
                                if (!r.ok) { setStatus(out, 'error', 'Caricamento non riuscito: ' + errorText(r)); return; }
                                // La risposta e' il testo di FluidSynth: dice che il comando e' stato inviato, non verifica l'ascolto.
                                setStatus(out, 'ok', 'Comando inviato a FluidSynth: ' + (typeof r.body === 'string' ? r.body : 'ok') + '. Ricarica la pagina per aggiornare gli strumenti.');
                                box.appendChild(reloadButton());
                            });
                        });
                    }
                });
            });
        }

        /* ── Schede e apertura ──────────────────────────────────────────── */
        var RENDERERS = { connessione: renderConnessione, audio: renderAudio, midi: renderMidi, modalita: renderModalita, soundfont: renderSoundfont };

        function buildTabs() {
            tabsEl.textContent = '';
            SECTIONS.forEach(function (s) {
                var b = h('button', { type: 'button', class: 'ms-tab', id: 'st-tab-' + s.id, role: 'tab', 'aria-controls': 'st-body',
                    'aria-selected': s.id === active ? 'true' : 'false', tabindex: s.id === active ? '0' : '-1', 'data-section': s.id, text: s.title });
                tabsEl.appendChild(b);
            });
        }

        function show(id) {
            if (!RENDERERS[id]) id = SECTIONS[0].id;
            active = id;
            loadGen += 1;
            clearTimeout(portsTimer);
            if (tabUnsub) { tabUnsub(); tabUnsub = null; }
            softRefresh = null;
            buildTabs();
            bodyEl.setAttribute('aria-labelledby', 'st-tab-' + id);
            bodyEl.textContent = '';
            RENDERERS[id](bodyEl, loadGen);
        }

        tabsEl.addEventListener('click', function (e) {
            var t = e.target.closest ? e.target.closest('.ms-tab') : null;
            if (t) show(t.getAttribute('data-section'));
        });
        tabsEl.addEventListener('keydown', function (e) {
            if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
            var i = SECTIONS.map(function (s) { return s.id; }).indexOf(active);
            i = (i + (e.key === 'ArrowRight' ? 1 : SECTIONS.length - 1)) % SECTIONS.length;
            show(SECTIONS[i].id);
            var el = doc.getElementById('st-tab-' + SECTIONS[i].id);
            if (el) el.focus();
            e.preventDefault();
        });
        bodyEl.addEventListener('click', function (e) {
            var r = e.target.closest ? e.target.closest('[data-act="reload"]') : null;
            if (r) root.location.reload();
        });

        // Il pannello e' di shell.openPanel: qui si osserva solo l'attributo `hidden` per leggere i dati ad ogni
        // apertura (freschi) e non prima (nessuna richiesta all'avvio della pagina).
        function onVisibility() {
            if (panel.hidden) { loadGen += 1; clearTimeout(portsTimer); if (tabUnsub) { tabUnsub(); tabUnsub = null; } return; }
            var want = pendingSection || active || SECTIONS[0].id;
            pendingSection = null;
            show(want);
        }
        new root.MutationObserver(function () {
            var visible = !panel.hidden;
            if (visible !== onVisibility.wasVisible) { onVisibility.wasVisible = visible; onVisibility(); }
        }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });
        onVisibility.wasVisible = !panel.hidden;

        // La sezione richiesta: openPanel('settings', section) da codice, oppure data-section sul pulsante di apertura.
        if (MS.shell && typeof MS.shell.openPanel === 'function') {
            var orig = MS.shell.openPanel;
            MS.shell.openPanel = function (name, section) {
                if (name === 'settings' && section) pendingSection = section;
                return orig.apply(MS.shell, arguments);
            };
        }
        doc.addEventListener('click', function (e) {
            var b = e.target.closest ? e.target.closest('[data-open-panel="settings"]') : null;
            if (b && b.getAttribute('data-section')) pendingSection = b.getAttribute('data-section');
        }, true);

        // Le porte possono cambiare a pannello aperto (cavo USB): l'evento ports_status ridisegna la scheda MIDI
        // dopo una breve pausa, senza polling.
        state.subscribe('ports', function () {
            if (panel.hidden || active !== 'midi') return;
            if (bodyEl.querySelector('.st-confirm') || bodyEl.contains(doc.activeElement) && /^(SELECT|INPUT)$/.test(doc.activeElement.tagName)) return;
            clearTimeout(portsTimer);
            portsTimer = setTimeout(function () { if (!panel.hidden && active === 'midi' && softRefresh) softRefresh(); }, 400);
        });

        // Apertura da URL (/settings e /presets rimandano qui): ?panel=settings&section=midi, poi l'URL si pulisce.
        (function deepLink() {
            var q;
            try { q = new root.URLSearchParams(root.location.search); } catch (e) { return; }
            var name = q.get('panel');
            if (name !== 'settings' && name !== 'library') return;
            try { root.history.replaceState(null, '', root.location.pathname + root.location.hash); } catch (e) { /* ignorato */ }
            if (MS.shell) MS.shell.openPanel(name, q.get('section') || undefined);
        })();
    };

    return api;
});
