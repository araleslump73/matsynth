/* MatSynth - api.js
 * Unico punto di accesso a rete e Socket.IO per tutta la pagina (contracts.md 2 e 9).
 *  - UN SOLO io(): creato alla prima richiesta e riusato (anche dal codice legacy).
 *  - request()/emit() non rigettano mai: restituiscono sempre {ok, http, body, error, warnings, unknown_outcome}.
 *  - Normalizza la busta nuova ({ok,status,error:{code,...}}), quella legacy ({status:'ok'|'error', message})
 *    e le risposte legacy a testo/array (/select_prog, /list_sf2, ...).
 *  - Timeout o errore di rete = esito SCONOSCIUTO (unknown_outcome), mai successo (2.6).
 * Perche' una factory con dipendenze iniettate: i test Node usano fetch/io finti senza browser. */
(function (root, factory) {
    'use strict';
    if (typeof module === 'object' && module.exports) module.exports = { createApi: factory };
    if (typeof window !== 'undefined' && root === window) {
        root.MatSynth = root.MatSynth || {};
        root.MatSynth.createApi = factory;
        root.MatSynth.api = factory({
            fetch: function () { return root.fetch.apply(root, arguments); },
            io: typeof root.io === 'function' ? root.io : null,
            FormData: root.FormData,
            AbortController: root.AbortController
        });
    }
})(typeof window !== 'undefined' ? window : this, function createApi(env) {
    'use strict';
    env = env || {};

    // Timeout di 2.6.
    var TIMEOUTS = { http: 8000, long: 30000, ack: 3000 };

    // Richieste "lunghe" (2.6): apri/salva/upload progetto, richiamo scena, avvio SoundFont, import MIDI.
    var LONG_PATTERNS = [
        /^\/api\/projects\/(open|save|save_as|upload)(\?|$)/,
        /^\/api\/scenes\/[^/]+\/recall(\?|$)/,
        /^\/api\/soundfont\/load(\?|$)/,
        /^\/api\/daw\/midi\/(upload|load\/)/,
        /^\/load_sf2\//
    ];

    function isLongRequest(url) {
        var path = String(url || '');
        for (var i = 0; i < LONG_PATTERNS.length; i++) if (LONG_PATTERNS[i].test(path)) return true;
        return false;
    }

    function clientError(code, message, details) {
        return {
            ok: false, http: 0, body: null, message: message,
            error: { code: code, message: message, details: details || {} },
            warnings: [], unknown_outcome: true
        };
    }

    function isObject(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

    // Estrae l'oggetto errore dalla busta nuova o ricostruisce uno equivalente da quella legacy.
    function errorFrom(body, http) {
        if (isObject(body) && isObject(body.error) && typeof body.error.code === 'string') {
            return {
                code: body.error.code,
                message: body.error.message || body.message || 'Errore',
                details: isObject(body.error.details) ? body.error.details : {}
            };
        }
        var msg = (isObject(body) && typeof body.message === 'string' && body.message) ||
                  (typeof body === 'string' && body.trim() && body.length < 300 ? body.trim() : '') ||
                  (http ? 'Errore HTTP ' + http : 'Errore');
        return { code: http ? 'HTTP_ERROR' : 'LEGACY_ERROR', message: msg, details: http ? { http: http } : {} };
    }

    /* Regola di successo (2.1.4): HTTP 2xx E (ok===true | ok assente e status==='ok' | corpo legacy
     * senza status: array, testo, oggetto senza ok/status). ok===false o status==='error' sono
     * errori anche con HTTP 200. */
    function normalize(http, body) {
        var good = http >= 200 && http < 300;
        if (good && isObject(body)) {
            if (body.ok === false) good = false;
            else if (body.ok === undefined && body.status === 'error') good = false;
        }
        var warnings = isObject(body) && Array.isArray(body.warnings) ? body.warnings : [];
        return {
            ok: good,
            http: http,
            body: body,
            message: isObject(body) && typeof body.message === 'string' ? body.message : '',
            error: good ? null : errorFrom(body, http),
            warnings: warnings,
            unknown_outcome: false
        };
    }

    function normalizeAck(ack) {
        // Handler senza valore di ritorno: il server ha risposto ma senza payload = ricevuto.
        if (ack === undefined || ack === null) {
            return { ok: true, http: 0, body: null, message: '', error: null, warnings: [], unknown_outcome: false };
        }
        var good = true;
        if (isObject(ack)) {
            if (ack.ok === false) good = false;
            else if (ack.ok === undefined && ack.status === 'error') good = false;
        }
        return {
            ok: good, http: 0, body: ack,
            message: isObject(ack) && typeof ack.message === 'string' ? ack.message : '',
            error: good ? null : errorFrom(ack, 0),
            warnings: isObject(ack) && Array.isArray(ack.warnings) ? ack.warnings : [],
            unknown_outcome: false
        };
    }

    var socketInstance = null;
    var ioCalls = 0;                 // diagnostica: deve restare 1 (criterio "un solo socket")
    var listeners = {};              // evento -> numero di listener registrati tramite api.on

    function getSocket() {
        if (!socketInstance) {
            if (typeof env.io !== 'function') return null;
            ioCalls += 1;
            socketInstance = env.io(env.ioOptions);
        }
        return socketInstance;
    }

    function connected() {
        var s = socketInstance;
        return !!(s && s.connected);
    }

    function request(method, url, body, opts) {
        opts = opts || {};
        var timeout = opts.timeout || (isLongRequest(url) ? TIMEOUTS.long : TIMEOUTS.http);
        var init = { method: String(method || 'GET').toUpperCase(), headers: {} };

        if (body !== undefined && body !== null && init.method !== 'GET' && init.method !== 'HEAD') {
            if (env.FormData && body instanceof env.FormData) init.body = body;   // il browser imposta il boundary
            else if (typeof body === 'string') { init.body = body; init.headers['Content-Type'] = 'application/json'; }
            else { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
        }

        var controller = env.AbortController ? new env.AbortController() : null;
        if (controller) init.signal = controller.signal;

        return new Promise(function (resolve) {
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                if (controller) { try { controller.abort(); } catch (e) { /* gia' chiusa */ } }
                resolve(clientError('CLIENT_TIMEOUT',
                    'Esito non confermato: nessuna risposta entro ' + Math.round(timeout / 1000) + ' s',
                    { url: url, timeout_ms: timeout }));
            }, timeout);

            function done(result) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(result);
            }

            Promise.resolve()
                .then(function () { return env.fetch(url, init); })
                .then(function (res) {
                    return Promise.resolve(res.text()).then(function (text) {
                        var parsed = text;
                        if (text === '' || text === undefined || text === null) parsed = null;
                        else { try { parsed = JSON.parse(text); } catch (e) { parsed = text; } }
                        done(normalize(res.status, parsed));
                    }, function () {
                        // Corpo illeggibile: se HTTP 2xx l'esito e' noto ma senza dati; non-2xx resta errore.
                        done(normalize(res.status, null));
                    });
                })
                .catch(function (err) {
                    var aborted = err && err.name === 'AbortError';
                    done(clientError(aborted ? 'CLIENT_TIMEOUT' : 'NETWORK_ERROR',
                        aborted ? 'Esito non confermato: richiesta interrotta'
                                : 'Rete non raggiungibile: esito non confermato',
                        { url: url, reason: err && err.message ? String(err.message) : '' }));
                });
        });
    }

    function emit(event, payload, opts) {
        opts = opts || {};
        var timeout = opts.timeout || TIMEOUTS.ack;
        return new Promise(function (resolve) {
            var s = getSocket();
            if (!s || !s.connected) {
                // Nulla e' stato inviato: esito certo (NON sconosciuto), il chiamante puo' usare il fallback HTTP.
                resolve({ ok: false, http: 0, body: null, message: 'Socket non connesso',
                          error: { code: 'NOT_CONNECTED', message: 'Socket non connesso', details: {} },
                          warnings: [], unknown_outcome: false });
                return;
            }
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                resolve(clientError('CLIENT_TIMEOUT',
                    'Esito non confermato: nessun ack entro ' + Math.round(timeout / 1000) + ' s',
                    { event: event, timeout_ms: timeout }));
            }, timeout);
            try {
                s.emit(event, payload, function (ack) {
                    if (settled) return;
                    settled = true;
                    clearTimeout(timer);
                    resolve(normalizeAck(ack));
                });
            } catch (err) {
                settled = true;
                clearTimeout(timer);
                resolve(clientError('NETWORK_ERROR', 'Invio non riuscito', { event: event }));
            }
        });
    }

    /* Comando con fallback (2.5): se il socket e' connesso usa SOLO l'ack, altrimenti SOLO HTTP.
     * Mai entrambi: un comando non va duplicato su due canali. Dopo un timeout d'ack NON si
     * ripete via HTTP (esito sconosciuto: potrebbe essere stato eseguito). */
    function command(event, payload, fallback, opts) {
        if (connected()) return emit(event, payload, opts);
        if (!fallback) return emit(event, payload, opts);
        return request(fallback.method || 'POST', fallback.url, fallback.body, opts);
    }

    function on(event, fn) {
        var s = getSocket();
        if (!s) return function () {};
        s.on(event, fn);
        listeners[event] = (listeners[event] || 0) + 1;
        var active = true;
        return function unsubscribe() {
            if (!active) return;
            active = false;
            s.off(event, fn);
            listeners[event] = Math.max(0, (listeners[event] || 1) - 1);
        };
    }

    return {
        TIMEOUTS: TIMEOUTS,
        request: request,
        emit: emit,
        command: command,
        on: on,
        socket: getSocket,
        connected: connected,
        isLongRequest: isLongRequest,
        // Diagnostica per la verifica in browser: ioCalls deve essere 1, listeners stabile dopo i cambi vista.
        stats: function () {
            var copy = {};
            Object.keys(listeners).forEach(function (k) { copy[k] = listeners[k]; });
            return { ioCalls: ioCalls, listeners: copy };
        }
    };
});
