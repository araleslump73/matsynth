/* MatSynth - library_scenes.js (fase ui-live)
 * Servizio Scene (elenco, salva, salva come, richiama, preferiti, rinomina, elimina), caricamento SoundFont con
 * stato osservabile, e le sezioni "Scene" e "SoundFont" della Libreria (sostituiscono i segnaposto della shell).
 *
 * Regole (contracts.md 2, 4, 8.2; piano sez. 5):
 *  - Ogni azione attende l'esito HTTP e lo mostra; timeout/rete = "esito non confermato", mai successo.
 *  - Richiamo scena = POST /api/scenes/<id>/recall, nessun reload e nessun riavvio. Si chiama direttamente e si
 *    decide in base a `error.code` (un 409 non ha effetti collaterali). Flusso:
 *      TRANSPORT_ACTIVE -> spiegazione + Stop esplicito (mai fermato in automatico)
 *      UNSAVED_CHANGES  -> Salva / Scarta / Annulla
 *      SOUNDFONT_CHANGE_REQUIRED -> conferma, carica il SoundFont (stato osservabile), poi richiama con
 *                          discard_changes:true (il caricamento rende di nuovo "modificata" la scena, 4.7: la
 *                          scelta Salva/Scarta e' gia' stata fatta prima del passo SoundFont)
 *      PORTS_UNAVAILABLE -> richiamo parziale SOLO dopo consenso (allow_partial)
 *      PARTIAL_APPLY     -> errore, mai successo.
 *  - Nessuna finestra nativa (confirm/prompt): dialoghi nella pagina con focus gestito (anche perche' bloccano il
 *    thread e i test automatici).
 *  - Nessun timer periodico: l'unico setTimeout e' il ripiego (una tantum) dell'attesa SoundFont.
 *  - Le funzioni pure e il servizio (con dipendenze iniettate) sono provati con `node --test tests/js`. */
(function (root, factory) {
    'use strict';
    var mod = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = mod;
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        mod.init(root.MatSynth);
    }
})(typeof window !== 'undefined' ? window : this, function (root) {
    'use strict';

    var SF_WAIT_MS = 90000;      // ripiego: oltre questo tempo si rilegge lo stato e, se ancora in corso, "esito non confermato"
    var MAX_STEPS = 6;           // difesa da cicli di riprova

    /* ══════════════════ Parte pura ══════════════════ */

    function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
    function norm(s) {
        var t = String(s === undefined || s === null ? '' : s).toLowerCase();
        return typeof t.normalize === 'function' ? t.normalize('NFD').replace(/[̀-ͯ]/g, '') : t;
    }

    // Ricerca per parole (tutte presenti) su nome e SoundFont, senza maiuscole/accenti.
    function filterScenes(list, query) {
        var words = norm(query).split(/\s+/).filter(Boolean);
        if (!words.length) return list.slice();
        return list.filter(function (s) {
            var hay = norm(s.name) + ' ' + norm(s.soundfont && s.soundfont.file ? s.soundfont.file : (typeof s.soundfont === 'string' ? s.soundfont : ''));
            return words.every(function (w) { return hay.indexOf(w) >= 0; });
        });
    }

    function favoriteScenes(list) { return list.filter(function (s) { return s.valid !== false && s.favorite; }); }

    // Scena precedente/successiva rispetto alla corrente, nell'ordine della Libreria; salta le scene non valide.
    // Senza scena corrente: successiva = prima, precedente = ultima. Ai bordi si ferma (nessun giro).
    function neighborScene(list, currentId, dir) {
        var valid = list.filter(function (s) { return s.valid !== false; });
        if (!valid.length) return null;
        var idx = -1;
        for (var i = 0; i < valid.length; i++) if (valid[i].id === currentId) { idx = i; break; }
        if (idx < 0) return dir > 0 ? valid[0] : valid[valid.length - 1];
        var n = idx + (dir > 0 ? 1 : -1);
        return n >= 0 && n < valid.length ? valid[n] : null;
    }

    function soundfontFile(sf) { return sf && sf.file ? sf.file : null; }

    // Testo e tono dello stato SoundFont (4.6). `tone` + prefisso testuale: non solo colore.
    function soundfontView(sf) {
        if (!sf) return { tone: 'dim', text: 'Stato SoundFont non ancora letto.' };
        var f = sf.file ? ' «' + sf.file + '»' : '';
        switch (sf.state) {
            case 'loaded': return { tone: 'ok', text: 'Caricato' + f + '.' };
            case 'loading': return { tone: 'busy', text: 'Caricamento in corso' + f + '…' };
            case 'failed': return { tone: 'error', text: 'Caricamento non riuscito' + f + (sf.error ? ': ' + sf.error : '.') };
            case 'unknown': return { tone: 'warn', text: 'Stato sconosciuto: console FluidSynth non raggiungibile.' };
            default: return { tone: 'dim', text: 'Nessuna verifica ancora' + (sf.file ? ' (ultimo: ' + sf.file + ')' : '') + '.' };
        }
    }

    function transportBusy(tr) { return !!(tr && (tr.is_playing || tr.is_recording)); }
    function transportWhat(tr) { return tr && tr.is_recording ? 'Record' : 'Play'; }

    function partList(details) {
        var arr = details && Array.isArray(details.parts) ? details.parts : [];
        return arr.map(function (p) {
            var alias = p.alias ? ' (' + p.alias + ')' : '';
            var st = { missing: 'assente', ambiguous: 'ambigua', unbound: 'non associata', offline: 'errore', unavailable: 'non disponibile' }[p.status] || p.status || 'non disponibile';
            return 'Parte ' + ((p.part_id | 0) + 1) + alias + ': ' + st;
        });
    }

    var SCENE_ERRORS = {
        NAME_EXISTS: 'Esiste già una scena con questo nome.',
        NOT_FOUND: 'La scena non esiste più (eliminata da un altro dispositivo?).',
        VALIDATION_FAILED: 'La scena non è valida e non può essere usata.',
        UNSUPPORTED_VERSION: 'La scena è stata salvata da una versione più recente di MatSynth.',
        SOUNDFONT_LOADING: 'Un SoundFont è in caricamento: attendi che finisca.',
        SOUNDFONT_MISSING: 'Il SoundFont richiesto non è presente sul dispositivo.',
        DISK_FULL: 'Disco pieno: nulla è stato scritto.',
        DISK_ERROR: 'Errore di scrittura: il file precedente è intatto.'
    };

    /* ══════════════════ Servizio (dipendenze iniettate) ══════════════════
     * env = {api, state, ui, errorText, setTimeout, clearTimeout, afterRecall?}
     * ui  = {choice(opts)->Promise<id>, prompt(opts)->Promise<string|null>, progress(opts)->{update,close}, notify(msg, kind)} */
    function createSceneService(env) {
        var api = env.api, state = env.state, ui = env.ui;
        var setT = env.setTimeout || function (fn, ms) { return setTimeout(fn, ms); };
        var clearT = env.clearTimeout || function (t) { clearTimeout(t); };
        var errText = env.errorText || function (r) { return r && r.error ? r.error.message : 'Errore'; };

        var scenes = [];
        var loaded = false;
        var loadError = null;
        var busy = false;
        var pendingRefresh = null;
        var listeners = [];

        function emit() { listeners.slice().forEach(function (fn) { try { fn(); } catch (e) { if (typeof console !== 'undefined') console.error('[scenes]', e); } }); }
        function subscribe(fn) {
            listeners.push(fn);
            return function () { var i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
        }
        function msg(r) { return errText(r, SCENE_ERRORS); }
        function enc(id) { return encodeURIComponent(id); }
        function byId(id) { for (var i = 0; i < scenes.length; i++) if (scenes[i].id === id) return scenes[i]; return null; }

        function sameScene(a, b) { return !!a && !!b && a.scene_id === b.scene_id && a.name === b.name && !!a.dirty === !!b.dirty; }

        // Elenco: si legge a ogni apertura della Libreria e dopo ogni modifica, mai a ogni cambio vista.
        function refresh() {
            if (pendingRefresh) return pendingRefresh;
            pendingRefresh = api.request('GET', '/api/scenes').then(function (r) {
                pendingRefresh = null;
                if (r.ok && r.body && Array.isArray(r.body.scenes)) {
                    scenes = r.body.scenes;
                    loaded = true;
                    loadError = null;
                    if (isObj(r.body.current) && !sameScene(r.body.current, state.get('scene'))) state.set('scene', r.body.current);
                } else {
                    loadError = 'Elenco scene non letto: ' + msg(r);
                }
                emit();
                return { ok: r.ok };
            });
            return pendingRefresh;
        }

        function resyncState() {
            // Dopo un esito sconosciuto (2.6): si rilegge lo stato autoritativo, senza ripetere l'azione.
            return Promise.all([
                api.request('GET', '/api/scenes/current').then(function (r) {
                    if (r.ok && r.body && isObj(r.body.scene_state)) state.set('scene', r.body.scene_state);
                }),
                api.request('GET', '/api/parts').then(function (r) {
                    if (r.ok && r.body && Array.isArray(r.body.parts)) {
                        state.set('parts', { source: 'api', items: r.body.parts, runtime: r.body.runtime || [], conflicts: r.body.conflicts || [] });
                    }
                })
            ]);
        }

        function problem(title, lines, extra) {
            return ui.choice({ title: title, body: lines, actions: [{ id: 'ok', label: 'Chiudi', kind: 'primary' }].concat(extra || []), dismissId: 'ok' });
        }

        /* ── Trasporto ── */
        function stopTransport() {
            return api.command('transport_cmd', { cmd: 'stop_all' }, { method: 'POST', url: '/api/daw/stop_all' }).then(function (r) {
                if (r.ok) ui.notify('Riproduzione/registrazione fermata.', 'ok');
                else ui.notify('Stop non riuscito: ' + msg(r), 'error');
                return r;
            });
        }

        function transportDialog(what) {
            return ui.choice({
                title: what + ' in corso',
                body: ['Il richiamo di una scena cambia suoni e controlli mentre la musica suona, quindi è bloccato finché Play e Record sono attivi.',
                       'MatSynth non ferma nulla da solo: premi Stop quando vuoi, poi richiama la scena.'],
                actions: [{ id: 'stop', label: 'Stop', kind: 'primary' }, { id: 'close', label: 'Chiudi' }],
                dismissId: 'close'
            }).then(function (id) { return id === 'stop' ? stopTransport() : null; });
        }

        /* ── SoundFont ── */
        function waitSoundfont(file, prog) {
            return new Promise(function (resolve) {
                var done = false, unsub = null, timer = null;
                function finish(res) {
                    if (done) return;
                    done = true;
                    if (unsub) unsub();
                    clearT(timer);
                    prog.close();
                    resolve(res);
                }
                function check(sf) {
                    if (done || !sf) return;
                    if (sf.state === 'loaded' && sf.file === file) finish({ ok: true });
                    else if (sf.state === 'failed' || sf.state === 'unknown') finish({ ok: false, message: soundfontView(sf).text });
                    else prog.update('Caricamento di «' + file + '» in corso…');
                }
                unsub = state.subscribe('soundfont', check);
                check(state.get('soundfont'));
                // Ripiego una tantum: se l'evento non arriva si rilegge lo stato; ancora "in corso" = esito non confermato.
                timer = setT(function () {
                    api.request('GET', '/api/soundfont/status').then(function (r) {
                        if (done) return;
                        if (r.ok && r.body && r.body.soundfont) state.set('soundfont', r.body.soundfont);
                        if (done) return;
                        finish({ ok: false, unknown: true, message: 'Esito non confermato: il SoundFont risulta ancora in caricamento o lo stato non è leggibile.' });
                    });
                }, SF_WAIT_MS);
            });
        }

        // Avvia il caricamento e attende lo stato `loaded` reale (4.6): 202 non significa "caricato".
        function loadSoundfont(file) {
            var prog = ui.progress({ title: 'Caricamento SoundFont', text: 'Invio del comando per «' + file + '»…' });
            return api.request('POST', '/api/soundfont/load', { file: file }).then(function (r) {
                if (!r.ok) {
                    prog.close();
                    return { ok: false, message: msg(r), error: r.error, unknown: !!r.unknown_outcome };
                }
                if (r.body && isObj(r.body.job)) state.set('soundfont', r.body.job);
                return waitSoundfont(file, prog);
            });
        }

        /* ── Salva / Salva come ── */
        function reportSaved(r, what) {
            var warns = r.warnings || [];
            if (warns.length) ui.notify(what + ' con avvisi: ' + warns.map(function (w) { return w.message; }).join(' '), 'warn');
            else ui.notify(what + '.', 'ok');
        }

        function saveAs(initialName) {
            return ui.prompt({ title: 'Salva scena come…', label: 'Nome della scena (max 64 caratteri)', value: initialName || '', maxLength: 64, confirmLabel: 'Salva' }).then(function (name) {
                if (name === null) return { ok: false, cancelled: true };
                return postScene(name, null);
            });
        }

        function postScene(name, overwriteId) {
            var body = { name: name };
            if (overwriteId) body.overwrite_id = overwriteId;
            return api.request('POST', '/api/scenes', body).then(function (r) {
                if (r.ok) {
                    reportSaved(r, overwriteId ? 'Scena «' + name + '» sovrascritta' : 'Scena «' + name + '» salvata');
                    return refresh().then(function () { return { ok: true, scene: r.body && r.body.scene }; });
                }
                var code = r.error && r.error.code;
                if (code === 'NAME_EXISTS' && r.error.details && r.error.details.existing_id) {
                    var existing = r.error.details.existing_id;
                    return ui.choice({
                        title: 'Esiste già una scena con questo nome',
                        body: ['Una scena «' + name + '» esiste già. Sovrascriverla la sostituisce con lo stato attuale e il contenuto precedente va perso.'],
                        actions: [{ id: 'overwrite', label: 'Sovrascrivi', kind: 'danger' }, { id: 'rename', label: 'Cambia nome', kind: 'primary' }, { id: 'cancel', label: 'Annulla' }],
                        dismissId: 'cancel'
                    }).then(function (id) {
                        if (id === 'overwrite') return postScene(name, existing);
                        if (id === 'rename') return saveAs(name);
                        return { ok: false, cancelled: true };
                    });
                }
                if (r.unknown_outcome) resyncState();
                return problem('Salvataggio non riuscito', [msg(r)]).then(function () { return { ok: false, error: r.error }; });
            });
        }

        function saveCurrent(o) {
            var cur = state.get('scene');
            if (!cur || !cur.scene_id) return saveAs('');
            function put() {
                return api.request('PUT', '/api/scenes/' + enc(cur.scene_id), {}).then(function (r) {
                    if (r.ok) {
                        reportSaved(r, 'Scena «' + cur.name + '» salvata');
                        return refresh().then(function () { return { ok: true }; });
                    }
                    if (r.unknown_outcome) resyncState();
                    return problem('Salvataggio non riuscito', [msg(r)]).then(function () { return { ok: false, error: r.error }; });
                });
            }
            if (o && o.confirmed) return put();
            return ui.choice({
                title: 'Salva scena',
                body: ['Sovrascrivere la scena «' + (cur.name || cur.scene_id) + '» con lo stato attuale dei 16 canali e dei master?'],
                actions: [{ id: 'save', label: 'Sovrascrivi', kind: 'primary' }, { id: 'cancel', label: 'Annulla' }],
                dismissId: 'cancel'
            }).then(function (id) { return id === 'save' ? put() : { ok: false, cancelled: true }; });
        }

        /* ── Richiamo ── */
        function recall(id) {
            if (busy) return Promise.resolve({ ok: false, reason: 'busy' });
            var sc = byId(id);
            var name = sc ? sc.name : id;
            var opts = { discard_changes: false, allow_partial: false };
            var steps = 0;
            busy = true;
            emit();

            function done(res) { busy = false; emit(); return res; }

            function step() {
                steps += 1;
                if (steps > MAX_STEPS) return Promise.resolve({ ok: false, reason: 'loop' });
                var tr = state.get('transport');
                // Controllo anticipato: nessuna richiesta inutile se Play/Record e' gia' noto. Il server resta l'autorita'.
                if (transportBusy(tr)) return transportDialog(transportWhat(tr)).then(function () { return { ok: false, reason: 'transport' }; });
                return api.request('POST', '/api/scenes/' + enc(id) + '/recall', { discard_changes: opts.discard_changes, allow_partial: opts.allow_partial }).then(handle);
            }

            function handle(r) {
                if (r.ok) return success(r);
                var code = r.error && r.error.code;
                var det = (r.error && r.error.details) || {};
                if (r.unknown_outcome) {
                    resyncState();
                    return problem('Esito non confermato', ['Il richiamo di «' + name + '» potrebbe essere stato eseguito oppure no. Ho riletto lo stato dal dispositivo: controlla il suono prima di riprovare.', msg(r)])
                        .then(function () { return { ok: false, reason: 'unknown' }; });
                }
                switch (code) {
                    case 'TRANSPORT_ACTIVE':
                        return transportDialog(det.is_recording ? 'Record' : 'Play').then(function () { return { ok: false, reason: 'transport' }; });
                    case 'OPERATION_IN_PROGRESS':
                        // Un altro dispositivo sta salvando/aprendo: avviso breve, nessuna riprova automatica.
                        ui.notify(msg(r), 'warn');
                        return { ok: false, reason: 'busy' };
                    case 'UNSAVED_CHANGES':
                        return ui.choice({
                            title: 'Modifiche non salvate',
                            body: ['La scena attuale è stata modificata. Richiamando «' + name + '» le modifiche andrebbero perse.'],
                            actions: [{ id: 'save', label: 'Salva e richiama', kind: 'primary' }, { id: 'discard', label: 'Scarta e richiama', kind: 'danger' }, { id: 'cancel', label: 'Annulla' }],
                            dismissId: 'cancel'
                        }).then(function (choice) {
                            if (choice === 'discard') { opts.discard_changes = true; return step(); }
                            if (choice === 'save') {
                                return saveCurrent({ confirmed: true }).then(function (res) {
                                    return res.ok ? step() : { ok: false, reason: res.cancelled ? 'cancelled' : 'save-failed' };
                                });
                            }
                            return { ok: false, reason: 'cancelled' };
                        });
                    case 'SOUNDFONT_CHANGE_REQUIRED':
                        return ui.choice({
                            title: 'Serve un altro SoundFont',
                            body: ['«' + name + '» usa il SoundFont «' + det.required + '»' + (det.loaded ? ', ora è caricato «' + det.loaded + '»' : '') + '.',
                                   'Caricarlo interrompe il suono per qualche secondo e cambia gli strumenti di tutte le parti. Poi la scena viene richiamata.'],
                            actions: [{ id: 'go', label: 'Carica SoundFont e richiama', kind: 'primary' }, { id: 'cancel', label: 'Annulla' }],
                            dismissId: 'cancel'
                        }).then(function (choice) {
                            if (choice !== 'go') return { ok: false, reason: 'cancelled' };
                            return loadSoundfont(det.required).then(function (res) {
                                if (!res.ok) {
                                    return problem('SoundFont non caricato', [res.message || 'Caricamento non riuscito.', 'La scena NON è stata richiamata.']).then(function () { return { ok: false, reason: 'soundfont' }; });
                                }
                                // 4.7: il caricamento ha reso di nuovo "modificata" la scena; la decisione e' gia' stata presa.
                                opts.discard_changes = true;
                                return step();
                            });
                        });
                    case 'SOUNDFONT_MISSING':
                        return problem('SoundFont mancante', ['«' + name + '» richiede «' + det.required + '», che non è presente sul dispositivo. Nessuna sostituzione automatica con un altro SoundFont.']).then(function () { return { ok: false, reason: 'missing' }; });
                    case 'PORTS_UNAVAILABLE':
                        return ui.choice({
                            title: 'Alcune destinazioni non sono disponibili',
                            body: ['Questi strumenti non sono raggiungibili:'].concat(partList(det), ['Puoi richiamare la scena solo in parte: le parti non disponibili vengono saltate (mantengono la configurazione salvata, nulla viene inviato e nessun altro strumento le sostituisce).']),
                            actions: [{ id: 'partial', label: 'Richiama solo in parte', kind: 'primary' }, { id: 'cancel', label: 'Annulla' }],
                            dismissId: 'cancel'
                        }).then(function (choice) {
                            if (choice !== 'partial') return { ok: false, reason: 'cancelled' };
                            opts.allow_partial = true;
                            return step();
                        });
                    case 'PARTIAL_APPLY':
                        resyncState();
                        return problem('Scena applicata solo in parte', [
                            'Il richiamo di «' + name + '» si è interrotto a metà. NON è riuscito del tutto.',
                            det.failed_parts && det.failed_parts.length ? 'Parti non riuscite: ' + det.failed_parts.map(function (p) { return p + 1; }).join(', ') + '.' : '',
                            'La configurazione desiderata è stata comunque salvata e la scena risulta modificata. Controlla FluidSynth o le porte e riprova.'].filter(Boolean))
                            .then(function () { return { ok: false, reason: 'partial-apply' }; });
                    default:
                        if (code === 'NOT_FOUND') refresh();
                        return problem('Richiamo non riuscito', [msg(r)]).then(function () { return { ok: false, reason: 'error', error: r.error }; });
                }
            }

            function success(r) {
                var warns = r.warnings || [];
                if (warns.length) ui.notify('Scena «' + name + '» richiamata con avvisi: ' + warns.map(function (w) { return w.message; }).join(' '), 'warn');
                else ui.notify('Scena «' + name + '» richiamata.', 'ok');
                // Lo stato arriva anche da eventi; la rilettura copre il caso di socket assente.
                return resyncState().then(refresh).then(function () { return { ok: true, warnings: warns }; });
            }

            return step().then(done, function (e) { busy = false; emit(); throw e; });
        }

        /* ── Preferiti, rinomina, elimina ── */
        function toggleFavorite(id) {
            var sc = byId(id);
            if (!sc) return Promise.resolve({ ok: false });
            var want = !sc.favorite;
            return api.request('PATCH', '/api/scenes/' + enc(id), { favorite: want }).then(function (r) {
                if (!r.ok) { ui.notify('Preferito non aggiornato: ' + msg(r), 'error'); if (r.unknown_outcome) refresh(); return { ok: false }; }
                sc.favorite = want;
                emit();
                return { ok: true, favorite: want };
            });
        }

        function rename(id) {
            var sc = byId(id);
            if (!sc) return Promise.resolve({ ok: false });
            return ui.prompt({ title: 'Rinomina scena', label: 'Nuovo nome (max 64 caratteri)', value: sc.name, maxLength: 64, confirmLabel: 'Rinomina' }).then(function (name) {
                if (name === null || name === sc.name) return { ok: false, cancelled: true };
                return api.request('PATCH', '/api/scenes/' + enc(id), { name: name }).then(function (r) {
                    if (!r.ok) {
                        if (r.unknown_outcome) refresh();
                        return problem('Rinomina non riuscita', [msg(r)]).then(function () { return { ok: false }; });
                    }
                    ui.notify('Scena rinominata in «' + name + '».', 'ok');
                    return refresh().then(function () { return { ok: true }; });
                });
            });
        }

        function remove(id) {
            var sc = byId(id);
            var name = sc ? sc.name : id;
            return ui.choice({
                title: 'Eliminare la scena?',
                body: ['«' + name + '» verrà eliminata dal dispositivo e non si potrà recuperare. Le modifiche attuali ai suoni non cambiano.'],
                actions: [{ id: 'del', label: 'Elimina', kind: 'danger' }, { id: 'cancel', label: 'Annulla', kind: 'primary' }],
                dismissId: 'cancel'
            }).then(function (choice) {
                if (choice !== 'del') return { ok: false, cancelled: true };
                return api.request('DELETE', '/api/scenes/' + enc(id)).then(function (r) {
                    if (!r.ok) {
                        if (r.unknown_outcome) refresh();
                        return problem('Eliminazione non riuscita', [msg(r)]).then(function () { return { ok: false }; });
                    }
                    ui.notify('Scena «' + name + '» eliminata.', 'ok');
                    return refresh().then(function () { return { ok: true }; });
                });
            });
        }

        return {
            subscribe: subscribe, refresh: refresh, list: function () { return scenes; }, byId: byId,
            isLoaded: function () { return loaded; }, loadError: function () { return loadError; }, isBusy: function () { return busy; },
            recall: recall, saveCurrent: saveCurrent, saveAs: saveAs, toggleFavorite: toggleFavorite, rename: rename, remove: remove,
            loadSoundfont: loadSoundfont, stopTransport: stopTransport, resyncState: resyncState
        };
    }

    /* ══════════════════ DOM: dialoghi in pagina ══════════════════ */
    function createDialogUi(doc, win, toast) {
        var uid = 0;
        var chain = Promise.resolve();
        var current = null;          // {overlay, dialog, dismiss, restore}

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

        function focusables(dlg) {
            var list = dlg.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
            var out = [];
            for (var i = 0; i < list.length; i++) if (!list[i].disabled && list[i].offsetParent !== null) out.push(list[i]);
            return out;
        }

        // Registrato UNA volta in fase di cattura su window: precede il listener di document della shell, cosi' Esc
        // chiude solo il dialogo e non anche la Libreria aperta sotto.
        var wired = false;
        function wire() {
            if (wired) return;
            wired = true;
            win.addEventListener('keydown', function (e) {
                if (!current) return;
                if (e.key === 'Escape') {
                    e.preventDefault(); e.stopPropagation();
                    if (current.dismiss) current.dismiss();
                } else if (e.key === 'Tab') {
                    var f = focusables(current.dialog);
                    if (!f.length) { e.preventDefault(); return; }
                    var first = f[0], last = f[f.length - 1];
                    if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
                    else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
                    else if (!current.dialog.contains(doc.activeElement)) { e.preventDefault(); first.focus(); }
                    e.stopPropagation();
                }
            }, true);
        }

        function open(title, bodyNodes, actionNodes, dismiss) {
            wire();
            uid += 1;
            var tid = 'lv-dlg-t' + uid;
            var dlg = h('div', { class: 'lv-dlg', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': tid }, [
                h('h3', { id: tid, class: 'lv-dlg-title', text: title }),
                h('div', { class: 'lv-dlg-body' }, bodyNodes),
                actionNodes.length ? h('div', { class: 'lv-dlg-actions' }, actionNodes) : null
            ]);
            var overlay = h('div', { class: 'lv-dlg-overlay' }, [dlg]);
            var restore = doc.activeElement;
            doc.body.appendChild(overlay);
            current = { overlay: overlay, dialog: dlg, dismiss: dismiss, restore: restore };
            return current;
        }

        function close(inst) {
            if (!inst || !inst.overlay.parentNode) return;
            inst.overlay.parentNode.removeChild(inst.overlay);
            if (current === inst) current = null;
            if (inst.restore && typeof inst.restore.focus === 'function' && doc.contains(inst.restore)) inst.restore.focus();
        }

        function choice(o) {
            chain = chain.then(function () {
                return new Promise(function (resolve) {
                    var inst;
                    function pick(id) { close(inst); resolve(id); }
                    var body = (Array.isArray(o.body) ? o.body : [o.body]).filter(Boolean).map(function (t) { return h('p', { text: t }); });
                    var btns = o.actions.map(function (a) {
                        var b = h('button', { type: 'button', class: 'lv-btn' + (a.kind ? ' lv-btn-' + a.kind : ''), text: a.label });
                        b.addEventListener('click', function () { pick(a.id); });
                        return b;
                    });
                    inst = open(o.title, body, btns, function () { pick(o.dismissId); });
                    // Il primo focus va sull'azione piu' sicura (non distruttiva) quando c'e' un'azione primaria.
                    var primary = null;
                    o.actions.forEach(function (a, i) { if (!primary && a.kind === 'primary') primary = btns[i]; });
                    (primary || btns[0]).focus();
                });
            });
            return chain;
        }

        function prompt(o) {
            chain = chain.then(function () {
                return new Promise(function (resolve) {
                    var inst;
                    function finish(v) { close(inst); resolve(v); }
                    var id = 'lv-dlg-in' + uid;
                    var input = h('input', { type: 'text', class: 'lv-input', id: id, maxlength: o.maxLength || 64, value: o.value || '', autocomplete: 'off' });
                    var err = h('p', { class: 'lv-dlg-error', role: 'alert' });
                    var ok = h('button', { type: 'button', class: 'lv-btn lv-btn-primary', text: o.confirmLabel || 'OK' });
                    var cancel = h('button', { type: 'button', class: 'lv-btn', text: 'Annulla' });
                    function submit() {
                        var v = input.value.trim();
                        if (!v) { err.textContent = 'Inserisci un nome.'; input.focus(); return; }
                        if (o.maxLength && v.length > o.maxLength) { err.textContent = 'Massimo ' + o.maxLength + ' caratteri.'; return; }
                        finish(v);
                    }
                    ok.addEventListener('click', submit);
                    cancel.addEventListener('click', function () { finish(null); });
                    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
                    inst = open(o.title, [h('label', { for: id, class: 'lv-label', text: o.label }), input, err], [ok, cancel], function () { finish(null); });
                    input.focus();
                    input.select();
                });
            });
            return chain;
        }

        function progress(o) {
            // Non entra in coda: e' un indicatore di attesa tra due dialoghi, senza azioni ne' Esc.
            var text = h('p', { role: 'status', 'aria-live': 'polite', text: o.text });
            var inst = open(o.title, [text, h('p', { class: 'lv-dlg-hint', text: 'Non chiudere la pagina: il caricamento prosegue sul dispositivo.' })], [], null);
            return {
                update: function (t) { if (text.textContent !== t) text.textContent = t; },
                close: function () { close(inst); }
            };
        }

        return { choice: choice, prompt: prompt, progress: progress, notify: toast };
    }

    /* ══════════════════ DOM: sezioni della Libreria ══════════════════ */
    function init(MS) {
        var doc = root.document;
        var api = MS.api, state = MS.state, library = MS.library, partPanel = MS.partPanel;
        if (!api || !state || !doc) return;

        function toast(m, kind) { if (MS.shell && MS.shell.toast) MS.shell.toast(m, kind); }
        var ui = createDialogUi(doc, root, toast);
        var service = createSceneService({ api: api, state: state, ui: ui, errorText: partPanel ? partPanel.errorText : null });

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
        function icon(name) { return h('i', { class: 'fas fa-' + name, 'aria-hidden': 'true' }); }
        function setText(n, t) { if (n && n.textContent !== t) n.textContent = t; }

        /* ── Sezione Scene ── */
        var sceneView = null;       // {box, draw, query} della sezione aperta (una sola alla volta)

        function drawScenes() {
            if (!sceneView) return;
            if (!sceneView.box.isConnected) { sceneView = null; return; }
            sceneView.draw();
        }

        function renderScenes(host) {
            // Contenitore proprio per ogni apertura: i listener si perdono con lui (il corpo della Libreria e' condiviso).
            var box = h('div', { class: 'lb-root' });
            host.appendChild(box);
            var view = { box: box, query: '' };
            var cur = h('p', { class: 'lv-curline' });
            var searchIn = h('input', { type: 'search', class: 'lv-input', id: 'lb-scene-search', placeholder: 'Cerca scena per nome o SoundFont', 'aria-label': 'Cerca scena', autocomplete: 'off' });
            var saveB = h('button', { type: 'button', class: 'lv-btn lv-btn-primary' }, [icon('save'), ' Salva scena attuale']);
            var saveAsB = h('button', { type: 'button', class: 'lv-btn' }, [icon('plus'), ' Salva come nuova…']);
            var info = h('p', { class: 'ms-note', role: 'status' });
            var list = h('ul', { class: 'lb-list' });
            var stopRow = h('div', { class: 'lv-notice', role: 'status', hidden: true });
            box.appendChild(cur);
            box.appendChild(h('div', { class: 'lv-actions' }, [saveB, saveAsB]));
            box.appendChild(stopRow);
            box.appendChild(h('label', { class: 'lv-label', for: 'lb-scene-search', text: 'Cerca' }));
            box.appendChild(searchIn);
            box.appendChild(info);
            box.appendChild(list);

            view.draw = function () {
                var s = state.get('scene') || {};
                var tr = state.get('transport');
                cur.textContent = '';
                cur.appendChild(h('span', { class: 'lv-curname', text: s.name ? 'Scena attuale: ' + s.name : 'Nessuna scena caricata (suono del progetto)' }));
                if (s.dirty) cur.appendChild(h('span', { class: 'lv-pill lv-pill-warn' }, [icon('pen'), ' modificata']));
                saveB.disabled = service.isBusy();
                saveAsB.disabled = service.isBusy();
                saveB.lastChild.textContent = ' ' + (s.scene_id ? 'Salva «' + s.name + '»' : 'Salva scena attuale');
                stopRow.hidden = !transportBusy(tr);
                if (!stopRow.hidden && !stopRow.firstChild) {
                    var b = h('button', { type: 'button', class: 'lv-btn lv-btn-danger', 'data-act': 'stop' }, [icon('stop'), ' Stop']);
                    stopRow.appendChild(h('span', { text: 'Play o Record sono attivi: il richiamo delle scene è bloccato finché non premi Stop.' }));
                    stopRow.appendChild(b);
                }
                var all = service.list();
                var shown = filterScenes(all, searchIn.value);
                info.textContent = service.loadError() ? service.loadError() :
                    !service.isLoaded() ? 'Lettura delle scene…' :
                    !all.length ? 'Nessuna scena salvata. Salva lo stato attuale per crearne una.' :
                    !shown.length ? 'Nessuna scena corrisponde alla ricerca.' : shown.length + (shown.length === 1 ? ' scena' : ' scene');
                list.textContent = '';
                shown.forEach(function (sc) { list.appendChild(sceneRow(sc, s)); });
            };
            function sceneRow(sc, curScene) {
                var isCur = curScene && curScene.scene_id === sc.id;
                var li = h('li', { class: 'lb-item' + (isCur ? ' is-current' : '') + (sc.valid === false ? ' is-invalid' : ''), 'data-id': sc.id });
                var name = h('span', { class: 'lb-name' }, [
                    isCur ? h('span', { class: 'lb-mark', 'aria-hidden': 'true', text: '▶' }) : null,
                    h('strong', { text: sc.name }),
                    isCur ? h('span', { class: 'lv-pill', text: 'attuale' }) : null,
                    sc.source === 'legacy' ? h('span', { class: 'lv-pill lv-pill-dim', title: 'Preset del vecchio formato: al primo salvataggio viene convertito (con copia di sicurezza)', text: 'preset legacy' }) : null]);
                var star = h('button', { type: 'button', class: 'lv-btn lb-star', 'data-act': 'fav', 'aria-pressed': sc.favorite ? 'true' : 'false',
                    'aria-label': 'Preferita: ' + sc.name }, [icon('star'), ' ' + (sc.favorite ? 'Preferita' : 'Preferita?')]);
                var meta = h('small', { class: 'lb-meta', text: sc.valid === false ? 'Non valida: ' + (sc.error && sc.error.message ? sc.error.message : 'file illeggibile') :
                    'SoundFont: ' + (sc.soundfont && sc.soundfont.file ? sc.soundfont.file : (typeof sc.soundfont === 'string' && sc.soundfont ? sc.soundfont : 'non specificato')) });
                var recall = h('button', { type: 'button', class: 'lv-btn lv-btn-primary', 'data-act': 'recall', disabled: sc.valid === false || service.isBusy() }, [icon('play'), ' Richiama']);
                var ren = h('button', { type: 'button', class: 'lv-btn', 'data-act': 'rename', disabled: sc.valid === false || service.isBusy() }, [icon('pen'), ' Rinomina']);
                var del = h('button', { type: 'button', class: 'lv-btn', 'data-act': 'delete', disabled: service.isBusy() }, [icon('trash'), ' Elimina']);
                li.appendChild(name);
                li.appendChild(meta);
                li.appendChild(h('div', { class: 'lv-actions' }, [recall, star, ren, del]));
                return li;
            }
            searchIn.addEventListener('input', function () { view.draw(); });
            box.addEventListener('click', function (e) {
                var b = e.target.closest ? e.target.closest('[data-act]') : null;
                if (!b) return;
                var act = b.getAttribute('data-act');
                var li = b.closest('.lb-item');
                var id = li ? li.getAttribute('data-id') : null;
                if (act === 'stop') { service.stopTransport(); return; }
                if (act === 'fav') { b.disabled = true; service.toggleFavorite(id).then(view.draw); return; }
                if (act === 'recall') {
                    service.recall(id).then(function (r) { if (r && r.ok && MS.shell) MS.shell.closePanel(); });
                    return;
                }
                if (act === 'rename') { service.rename(id); return; }
                if (act === 'delete') { service.remove(id); }
            });
            saveB.addEventListener('click', function () { service.saveCurrent(); });
            saveAsB.addEventListener('click', function () { service.saveAs(''); });
            sceneView = view;
            view.draw();
            service.refresh();            // dati freschi a ogni apertura della Libreria
        }

        /* ── Sezione SoundFont ── */
        var sfView = null;
        var sfFiles = { loaded: false, files: [], error: null };

        function drawSf() {
            if (!sfView) return;
            if (!sfView.box.isConnected) { sfView = null; return; }
            sfView.draw();
        }

        function readSfFiles() {
            return api.request('GET', '/api/soundfonts').then(function (r) {
                if (r.ok && r.body && Array.isArray(r.body.files)) {
                    sfFiles = { loaded: true, files: r.body.files, error: null };
                    if (isObjLike(r.body.current)) state.set('soundfont', r.body.current);
                } else {
                    sfFiles = { loaded: false, files: [], error: 'Elenco SoundFont non letto: ' + (partPanel ? partPanel.errorText(r) : 'errore') };
                }
                drawSf();
            });
        }
        function isObjLike(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

        function renderSoundfont(host) {
            var box = h('div', { class: 'lb-root' });
            host.appendChild(box);
            var view = { box: box };
            var stateLine = h('p', { class: 'lv-sfstate', role: 'status' });
            var note = h('p', { class: 'ms-note', text: 'Un solo SoundFont è attivo alla volta. Caricarne un altro scarica quello attuale: il suono si interrompe per qualche secondo e gli strumenti di tutte le parti cambiano. Con Play o Record attivi il caricamento è bloccato.' });
            var notice = h('div', { class: 'lv-notice', role: 'status', hidden: true });
            var info = h('p', { class: 'ms-note', role: 'status' });
            var list = h('ul', { class: 'lb-list' });
            box.appendChild(stateLine);
            box.appendChild(note);
            box.appendChild(notice);
            box.appendChild(info);
            box.appendChild(list);
            view.draw = function () {
                var sf = state.get('soundfont');
                var v = soundfontView(sf);
                var prefix = { ok: 'OK · ', busy: 'In corso · ', error: 'ERRORE · ', warn: 'ATTENZIONE · ', dim: '' }[v.tone] || '';
                stateLine.setAttribute('data-tone', v.tone);
                setText(stateLine, prefix + v.text);
                var tr = state.get('transport');
                var blocked = transportBusy(tr);
                var loading = !!(sf && sf.state === 'loading');
                notice.hidden = !blocked;
                if (blocked && !notice.firstChild) {
                    notice.appendChild(h('span', { text: 'Play o Record sono attivi: il caricamento è bloccato finché non premi Stop.' }));
                    notice.appendChild(h('button', { type: 'button', class: 'lv-btn lv-btn-danger', 'data-act': 'stop' }, [icon('stop'), ' Stop']));
                }
                if (!blocked) notice.textContent = '';
                setText(info, sfFiles.error ? sfFiles.error : !sfFiles.loaded ? 'Lettura dei SoundFont…' : !sfFiles.files.length ? 'Nessun file .sf2/.sf3 trovato sul dispositivo.' : sfFiles.files.length + (sfFiles.files.length === 1 ? ' SoundFont' : ' SoundFont'));
                var inUse = sf && sf.state === 'loaded' ? sf.file : null;
                list.textContent = '';
                sfFiles.files.forEach(function (f) {
                    var cur = f === inUse;
                    var b = h('button', { type: 'button', class: 'lv-btn lv-btn-primary', 'data-act': 'load', 'data-file': f, disabled: cur || loading || blocked }, [icon('folder-open'), ' ' + (cur ? 'In uso' : 'Carica')]);
                    list.appendChild(h('li', { class: 'lb-item' + (cur ? ' is-current' : '') }, [
                        h('span', { class: 'lb-name' }, [cur ? h('span', { class: 'lb-mark', 'aria-hidden': 'true', text: '▶' }) : null, h('strong', { text: f }), cur ? h('span', { class: 'lv-pill', text: 'in uso' }) : null]),
                        h('div', { class: 'lv-actions' }, [b])]));
                });
            };
            box.addEventListener('click', function (e) {
                var b = e.target.closest ? e.target.closest('[data-act]') : null;
                if (!b) return;
                if (b.getAttribute('data-act') === 'stop') { service.stopTransport(); return; }
                if (b.getAttribute('data-act') !== 'load') return;
                var file = b.getAttribute('data-file');
                ui.choice({
                    title: 'Caricare «' + file + '»?',
                    body: ['Il SoundFont attuale viene scaricato e «' + file + '» caricato: il suono si interrompe per qualche secondo.',
                           'La scena attuale risulterà modificata (il SoundFont fa parte della scena).'],
                    actions: [{ id: 'go', label: 'Carica', kind: 'primary' }, { id: 'cancel', label: 'Annulla' }],
                    dismissId: 'cancel'
                }).then(function (id) {
                    if (id !== 'go') return null;
                    return service.loadSoundfont(file).then(function (res) {
                        if (res.ok) ui.notify('SoundFont «' + file + '» caricato e verificato.', 'ok');
                        else ui.notify('SoundFont non caricato: ' + (res.message || 'errore'), 'error');
                        if (res.unknown) readSfFiles();
                        drawSf();
                    });
                });
            });
            sfView = view;
            view.draw();
            readSfFiles();               // dati freschi a ogni apertura
            api.request('GET', '/api/soundfont/status').then(function (r) { if (r.ok && r.body && r.body.soundfont) state.set('soundfont', r.body.soundfont); });
        }

        // Iscrizioni a livello di modulo, UNA sola volta: le viste si ridisegnano solo se sono ancora nel DOM.
        service.subscribe(drawScenes);
        state.subscribe('scene', drawScenes);
        state.subscribe('transport', function () { drawScenes(); drawSf(); });
        state.subscribe('soundfont', drawSf);

        if (library) {
            library.registerSection('scene', { title: 'Scene', render: renderScenes });
            library.registerSection('soundfont', { title: 'SoundFont', render: renderSoundfont });
        }

        MS.scenes = service;
        MS.sceneTools = { filterScenes: filterScenes, neighborScene: neighborScene, favoriteScenes: favoriteScenes, transportBusy: transportBusy };
    }

    return {
        init: init, createSceneService: createSceneService, filterScenes: filterScenes, favoriteScenes: favoriteScenes,
        neighborScene: neighborScene, soundfontView: soundfontView, transportBusy: transportBusy, partList: partList
    };
});
