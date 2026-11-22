'use strict';
// Test di api.js con fetch e io finti (contracts.md 2.1, 2.5, 2.6, 9). Esecuzione: node --test tests/js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createApi } = require('../../home/matteo/matsynth_web/static/js/api.js');

function resp(status, body, opts) {
    opts = opts || {};
    const text = opts.raw !== undefined ? opts.raw : (typeof body === 'string' ? body : JSON.stringify(body));
    return { status, ok: status >= 200 && status < 300, text: () => opts.unreadable ? Promise.reject(new Error('boom')) : Promise.resolve(text) };
}

function makeEnv(handler, extra) {
    const calls = [];
    const env = Object.assign({
        fetch: (url, init) => { calls.push({ url, init }); return handler(url, init); },
        AbortController,
        FormData: typeof FormData !== 'undefined' ? FormData : undefined
    }, extra || {});
    return { env, calls };
}

// Socket finto minimale compatibile con la parte di socket.io usata da api.js.
function fakeIo() {
    const state = { created: 0, sockets: [] };
    function io() {
        state.created += 1;
        const handlers = {};
        const sock = {
            connected: false,
            emitted: [],
            ackMode: 'ok',
            on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
            off(ev, fn) { handlers[ev] = (handlers[ev] || []).filter(f => f !== fn); },
            emit(ev, payload, ack) {
                sock.emitted.push({ ev, payload });
                if (sock.ackMode === 'ok') ack({ status: 'ok' });
                else if (sock.ackMode === 'new') ack({ ok: true, status: 'ok', warnings: [{ code: 'X' }] });
                else if (sock.ackMode === 'error') ack({ ok: false, status: 'error', error: { code: 'NO_ARMED_TRACK', message: 'Nessuna traccia', details: {} } });
                else if (sock.ackMode === 'legacy-error') ack({ status: 'error', message: 'Values out of range' });
                else if (sock.ackMode === 'empty') ack();
                // 'silent': nessun ack
            },
            fire(ev, arg) { (handlers[ev] || []).slice().forEach(f => f(arg)); },
            count(ev) { return (handlers[ev] || []).length; }
        };
        state.sockets.push(sock);
        return sock;
    }
    io.state = state;
    return io;
}

/* ── request: buste ─────────────────────────────────────────────── */

test('busta nuova di successo', async () => {
    const { env } = makeEnv(() => Promise.resolve(resp(200, { ok: true, status: 'ok', message: 'Fatto', revision: 5, warnings: [{ code: 'SHARED_DESTINATION', message: 'x', details: {} }] })));
    const r = await createApi(env).request('GET', '/api/x');
    assert.equal(r.ok, true);
    assert.equal(r.http, 200);
    assert.equal(r.error, null);
    assert.equal(r.unknown_outcome, false);
    assert.equal(r.message, 'Fatto');
    assert.equal(r.warnings[0].code, 'SHARED_DESTINATION');
    assert.equal(r.body.revision, 5);
});

test('busta nuova di errore: error.code e details preservati', async () => {
    const body = { ok: false, status: 'error', message: 'Revisione obsoleta', error: { code: 'REVISION_CONFLICT', message: 'Revisione obsoleta', details: { expected_revision: 41, current_revision: 43 } } };
    const { env } = makeEnv(() => Promise.resolve(resp(409, body)));
    const r = await createApi(env).request('POST', '/api/daw/cut', { a: 1 });
    assert.equal(r.ok, false);
    assert.equal(r.http, 409);
    assert.equal(r.error.code, 'REVISION_CONFLICT');
    assert.equal(r.error.details.current_revision, 43);
    assert.equal(r.unknown_outcome, false);
});

test('busta legacy {status:ok, message}', async () => {
    const { env } = makeEnv(() => Promise.resolve(resp(200, { status: 'ok', message: 'Preset applicato' })));
    const r = await createApi(env).request('POST', '/api/presets/apply', {});
    assert.equal(r.ok, true);
    assert.equal(r.message, 'Preset applicato');
    assert.deepEqual(r.warnings, []);
});

test('busta legacy di errore con HTTP 400: codice sintetico HTTP_ERROR e messaggio conservato', async () => {
    const { env } = makeEnv(() => Promise.resolve(resp(400, { status: 'error', message: 'Nessun canale armato' })));
    const r = await createApi(env).request('POST', '/api/daw/record/start');
    assert.equal(r.ok, false);
    assert.equal(r.http, 400);
    assert.equal(r.error.code, 'HTTP_ERROR');
    assert.equal(r.error.message, 'Nessun canale armato');
    assert.equal(r.error.details.http, 400);
});

test('HTTP 200 con status:error o ok:false e\' un errore', async () => {
    let { env } = makeEnv(() => Promise.resolve(resp(200, { status: 'error', message: 'No' })));
    let r = await createApi(env).request('GET', '/x');
    assert.equal(r.ok, false);
    assert.equal(r.error.message, 'No');
    ({ env } = makeEnv(() => Promise.resolve(resp(200, { ok: false, message: 'No2' }))));
    r = await createApi(env).request('GET', '/x');
    assert.equal(r.ok, false);
});

test('risposte legacy a testo e array (/select_prog, /list_sf2)', async () => {
    let { env } = makeEnv(() => Promise.resolve(resp(200, 'OK')));
    let r = await createApi(env).request('GET', '/select_prog/0/0/1');
    assert.equal(r.ok, true);
    assert.equal(r.body, 'OK');
    ({ env } = makeEnv(() => Promise.resolve(resp(200, ['a.sf2', 'b.sf2']))));
    r = await createApi(env).request('GET', '/list_sf2');
    assert.equal(r.ok, true);
    assert.deepEqual(r.body, ['a.sf2', 'b.sf2']);
    ({ env } = makeEnv(() => Promise.resolve(resp(200, { channels: {}, gain: 1 }))));   // /get_state: oggetto senza ok/status
    r = await createApi(env).request('GET', '/get_state');
    assert.equal(r.ok, true);
});

test('testo di errore legacy con HTTP 400 diventa il messaggio', async () => {
    const { env } = makeEnv(() => Promise.resolve(resp(400, 'Invalid effect type')));
    const r = await createApi(env).request('GET', '/set_effect/x/1');
    assert.equal(r.ok, false);
    assert.equal(r.error.message, 'Invalid effect type');
});

test('HTTP non-2xx con corpo illeggibile o HTML e\' sempre un errore', async () => {
    let { env } = makeEnv(() => Promise.resolve(resp(500, '', { unreadable: true })));
    let r = await createApi(env).request('GET', '/x');
    assert.equal(r.ok, false);
    assert.equal(r.http, 500);
    assert.equal(r.error.code, 'HTTP_ERROR');
    ({ env } = makeEnv(() => Promise.resolve(resp(404, '<!doctype html><html>' + 'x'.repeat(400) + '</html>'))));
    r = await createApi(env).request('GET', '/api/inesistente');
    assert.equal(r.ok, false);
    assert.equal(r.http, 404);
    assert.equal(r.error.message, 'Errore HTTP 404');   // testo lungo non usato come messaggio
});

test('HTTP 200 con corpo vuoto = ok senza dati', async () => {
    const { env } = makeEnv(() => Promise.resolve(resp(200, '')));
    const r = await createApi(env).request('POST', '/x');
    assert.equal(r.ok, true);
    assert.equal(r.body, null);
});

/* ── request: timeout, rete, corpo ───────────────────────────────── */

test('timeout = CLIENT_TIMEOUT con esito sconosciuto, mai rigetto', async () => {
    let aborted = false;
    const { env } = makeEnv((url, init) => new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => { aborted = true; const e = new Error('abort'); e.name = 'AbortError'; reject(e); });
    }));
    const r = await createApi(env).request('POST', '/api/daw/paste', {}, { timeout: 30 });
    assert.equal(r.ok, false);
    assert.equal(r.unknown_outcome, true);
    assert.equal(r.error.code, 'CLIENT_TIMEOUT');
    assert.equal(r.http, 0);
    assert.equal(aborted, true);
    assert.match(r.error.message, /Esito non confermato/);
});

test('errore di rete = NETWORK_ERROR con esito sconosciuto', async () => {
    const { env } = makeEnv(() => Promise.reject(new TypeError('Failed to fetch')));
    const r = await createApi(env).request('GET', '/x');
    assert.equal(r.ok, false);
    assert.equal(r.unknown_outcome, true);
    assert.equal(r.error.code, 'NETWORK_ERROR');
});

test('fetch che lancia in modo sincrono non rigetta', async () => {
    const { env } = makeEnv(() => { throw new Error('sync'); });
    const r = await createApi(env).request('GET', '/x');
    assert.equal(r.error.code, 'NETWORK_ERROR');
});

test('corpo: JSON con Content-Type, GET senza corpo, FormData intatto, metodo maiuscolo', async () => {
    const { env, calls } = makeEnv(() => Promise.resolve(resp(200, { ok: true })));
    const api = createApi(env);
    await api.request('post', '/a', { bpm: 100 });
    assert.equal(calls[0].init.method, 'POST');
    assert.equal(calls[0].init.body, '{"bpm":100}');
    assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
    await api.request('GET', '/b', { ignorato: true });
    assert.equal(calls[1].init.body, undefined);
    const fd = new FormData(); fd.append('merge', 'true');
    await api.request('POST', '/api/daw/midi/upload', fd);
    assert.equal(calls[2].init.body, fd);
    assert.equal(calls[2].init.headers['Content-Type'], undefined);
    await api.request('POST', '/c');
    assert.equal(calls[3].init.body, undefined);
});

test('timeout lunghi per le richieste elencate in 2.6', () => {
    const api = createApi({});
    assert.equal(api.TIMEOUTS.http, 8000);
    assert.equal(api.TIMEOUTS.long, 30000);
    assert.equal(api.TIMEOUTS.ack, 3000);
    ['/api/projects/open', '/api/projects/save', '/api/projects/save_as', '/api/projects/upload',
        '/api/scenes/s-3fa91c0b7d22/recall', '/api/soundfont/load', '/api/daw/midi/upload', '/api/daw/midi/load/x.mid?merge=true'
    ].forEach(u => assert.equal(api.isLongRequest(u), true, u));
    ['/api/daw/state', '/api/scenes', '/api/projects/current', '/cc/0/7/100'].forEach(u => assert.equal(api.isLongRequest(u), false, u));
});

/* ── Socket.IO: un solo io(), emit, on ───────────────────────────── */

test('un solo io() anche con molte chiamate; on/unsubscribe bilanciati', () => {
    const io = fakeIo();
    const api = createApi({ io });
    const s1 = api.socket(), s2 = api.socket();
    assert.equal(s1, s2);
    const off1 = api.on('daw_tick', () => {});
    const off2 = api.on('daw_tick', () => {});
    api.on('daw_state_update', () => {});
    assert.equal(io.state.created, 1);
    assert.deepEqual(api.stats(), { ioCalls: 1, listeners: { daw_tick: 2, daw_state_update: 1 } });
    off1(); off1();     // idempotente
    off2();
    assert.equal(api.stats().listeners.daw_tick, 0);
    assert.equal(s1.count('daw_tick'), 0);
});

test('on consegna gli eventi e dopo unsubscribe no', () => {
    const io = fakeIo();
    const api = createApi({ io });
    const got = [];
    const off = api.on('midi_playback_beat', d => got.push(d.beat));
    api.socket().fire('midi_playback_beat', { beat: 1 });
    off();
    api.socket().fire('midi_playback_beat', { beat: 2 });
    assert.deepEqual(got, [1]);
});

test('emit senza socket connesso: NOT_CONNECTED, esito certo (non sconosciuto), nulla inviato', async () => {
    const io = fakeIo();
    const api = createApi({ io });
    const r = await api.emit('transport_cmd', { cmd: 'panic' });
    assert.equal(r.ok, false);
    assert.equal(r.error.code, 'NOT_CONNECTED');
    assert.equal(r.unknown_outcome, false);
    assert.equal(api.socket().emitted.length, 0);
    assert.equal(await createApi({}).emit('x', {}).then(r => r.error.code), 'NOT_CONNECTED');   // nemmeno io disponibile
});

test('emit: ack legacy, ack busta nuova, ack di errore, ack vuoto', async () => {
    const io = fakeIo();
    const api = createApi({ io });
    const sock = api.socket();
    sock.connected = true;
    let r = await api.emit('cc_update', { chan: 0, cc: 7, val: 100 });
    assert.equal(r.ok, true);
    assert.deepEqual(sock.emitted[0], { ev: 'cc_update', payload: { chan: 0, cc: 7, val: 100 } });
    sock.ackMode = 'new';
    r = await api.emit('transport_cmd', { cmd: 'play_start' });
    assert.equal(r.ok, true);
    assert.equal(r.warnings[0].code, 'X');
    sock.ackMode = 'error';
    r = await api.emit('transport_cmd', { cmd: 'record_start' });
    assert.equal(r.ok, false);
    assert.equal(r.error.code, 'NO_ARMED_TRACK');
    sock.ackMode = 'legacy-error';
    r = await api.emit('cc_update', {});
    assert.equal(r.ok, false);
    assert.equal(r.error.message, 'Values out of range');
    sock.ackMode = 'empty';
    r = await api.emit('cc_update', {});
    assert.equal(r.ok, true);
});

test('emit: nessun ack entro il timeout = CLIENT_TIMEOUT con esito sconosciuto', async () => {
    const io = fakeIo();
    const api = createApi({ io });
    const sock = api.socket();
    sock.connected = true; sock.ackMode = 'silent';
    const r = await api.emit('transport_cmd', { cmd: 'stop_all' }, { timeout: 25 });
    assert.equal(r.ok, false);
    assert.equal(r.unknown_outcome, true);
    assert.equal(r.error.code, 'CLIENT_TIMEOUT');
});

test('command: socket connesso -> solo ack, mai HTTP; disconnesso -> solo HTTP; timeout ack non ripete via HTTP', async () => {
    const io = fakeIo();
    const { env, calls } = makeEnv(() => Promise.resolve(resp(200, { status: 'ok' })), { io });
    const api = createApi(env);
    const sock = api.socket();
    const fb = { method: 'POST', url: '/api/daw/panic' };

    sock.connected = true;
    let r = await api.command('transport_cmd', { cmd: 'panic' }, fb);
    assert.equal(r.ok, true);
    assert.equal(calls.length, 0);
    assert.equal(sock.emitted.length, 1);

    sock.connected = false;
    r = await api.command('transport_cmd', { cmd: 'panic' }, fb);
    assert.equal(r.ok, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/daw/panic');
    assert.equal(sock.emitted.length, 1, 'nessun doppio invio');

    sock.connected = true; sock.ackMode = 'silent';
    r = await api.command('transport_cmd', { cmd: 'panic' }, fb, { timeout: 20 });
    assert.equal(r.unknown_outcome, true);
    assert.equal(calls.length, 1, 'timeout d\'ack: nessun fallback automatico');
});
