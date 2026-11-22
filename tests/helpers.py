"""Helper di test per MatSynth (nessun hardware, nessun rtmidi).

Uso:
    from helpers import FakeMidiInput, FakeMidiOutput, make_daw, FIXTURES_DIR

`unittest discover -s tests` mette gia' tests/ in sys.path, quindi
`import helpers` funziona; per sicurezza questo modulo aggiunge anche
home/matteo/matsynth_web a sys.path (cosi' `import daw_recorder` funziona).

Cosa richiederebbe hardware (verificato leggendo il codice):
  - Le porte MIDI passano tutte da `midi_router.MidiRouter`. La DAW autonoma
    (`MultiTrackDAW()` senza router) crea un router in memoria che usa
    `midi_router.MidoBackend`, cioe' mido.get_input_names / get_output_names
    / open_input(callback=...) / open_output risolti a ogni chiamata.
    Senza python-rtmidi mido solleva ImportError, catturato dal router: la
    DAW si istanzia comunque con midi_input = midi_output = None.
  - `import daw_recorder` NON richiede rtmidi (import di mido e' lazy sul backend).
  - Il thread demone `_update_loop` parte in __init__ ma e' inerte senza
    socketio (dorme 0.5s in loop). Non viene mai fermato: un thread per DAW
    istanziata resta vivo fino a fine processo (innocuo nei test).
  - Importare `app.py` solo con `import_app()`: all'import crea
    /home/matteo/... con makedirs, apre le porte tramite il router (polling
    ogni 2 s), parla con FluidSynth sulla 9800 e ritenta 5 volte con sleep.

Come lo aggiriamo: `make_daw()` applica `unittest.mock.patch.object` SOLO ai
quattro punti di apertura porte del modulo mido REALE durante la costruzione.
Il codice reale del router (bootstrap `keyboard-main`, interno = client che
inizia con "FLUID Synth") viene quindi eseguito davvero; mido.Message & co.
restano quelli veri. Per piu' porte e destinazioni esterne: `make_router()`
con `FakeBackend`, poi `MultiTrackDAW(router=router)`.
"""

import os
import sys
import time
from collections import deque
from unittest import mock

import mido

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_HERE)
APP_DIR = os.path.join(REPO_ROOT, 'home', 'matteo', 'matsynth_web')
FIXTURES_DIR = os.path.join(_HERE, 'fixtures')

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


class FakeMidiInput:
    """Porta di input finta.

    Il router (midi_router) apre gli input con `open_input(name, callback=cb)`:
    in quel caso `feed(msg)` chiama subito la callback (come fa il thread di
    rtmidi). Senza callback `feed` accoda e `iter_pending()` svuota la coda.
    """

    def __init__(self, name='FANTOM-08 Fake'):
        self.name = name
        self.closed = False
        self.callback = None
        self._queue = deque()

    def feed(self, *messages):
        for m in messages:
            if self.callback is not None and not self.closed:
                self.callback(m)
            else:
                self._queue.append(m)

    def iter_pending(self):
        while self._queue:
            yield self._queue.popleft()

    def close(self):
        self.closed = True


class FakeMidiOutput:
    """Porta di output finta: registra i mido.Message passati a `send()`.

    Il codice reale usa solo `send(msg)` e `close()`.
    `sent` e' la lista completa; `notes()` filtra note_on/off.
    """

    def __init__(self, name='FLUID Synth Fake'):
        self.name = name
        self.closed = False
        self.sent = []

    def send(self, msg):
        if self.closed:
            raise IOError('port closed')
        self.sent.append(msg)

    def close(self):
        self.closed = True

    def of_type(self, *types):
        return [m for m in self.sent if m.type in types]

    def notes(self):
        return self.of_type('note_on', 'note_off')


def make_daw(bpm=120, with_input=True, with_output=True, socketio=None,
             input_names=None, output_names=None):
    """Istanzia MultiTrackDAW senza hardware.

    Ritorna (daw, fake_in, fake_out); fake_in/fake_out sono None se
    disattivati (in quel caso nessuna porta 'esiste' per il nome cercato).
    `input_names`/`output_names` permettono di simulare piu' porte per
    verificare la logica di selezione per parole chiave; la porta fake
    restituita da open_* prende il nome effettivamente aperto.
    """
    import daw_recorder  # import tardivo: sys.path e' gia' a posto

    fake_in = FakeMidiInput() if with_input else None
    fake_out = FakeMidiOutput() if with_output else None
    if input_names is None:
        input_names = [fake_in.name] if fake_in else []
    if output_names is None:
        output_names = [fake_out.name] if fake_out else []

    def _open_in(name, *a, callback=None, **k):
        fake_in.name = name
        fake_in.callback = callback
        return fake_in

    def _open_out(name, *a, **k):
        fake_out.name = name
        return fake_out

    with mock.patch.object(mido, 'get_input_names', return_value=list(input_names)),          mock.patch.object(mido, 'get_output_names', return_value=list(output_names)),          mock.patch.object(mido, 'open_input', side_effect=_open_in),          mock.patch.object(mido, 'open_output', side_effect=_open_out):
        daw = daw_recorder.MultiTrackDAW(bpm=bpm, socketio=socketio)
    return daw, fake_in, fake_out


class FakeBackend:
    """Backend porte per midi_router.MidiRouter con piu' porte finte.

    `inputs`/`outputs`: nomi runtime -> FakeMidiInput/FakeMidiOutput.
    Le porte si aggiungono/tolgono a runtime (poi `router.refresh_ports()`)
    per simulare scollegamento e ricollegamento. `fail_open` contiene nomi
    la cui apertura solleva IOError (stato `offline`).
    """

    def __init__(self, inputs=(), outputs=()):
        self.inputs = {n: FakeMidiInput(n) for n in inputs}
        self.outputs = {n: FakeMidiOutput(n) for n in outputs}
        self.fail_open = set()
        self.opened = []

    def get_input_names(self):
        return list(self.inputs)

    def get_output_names(self):
        return list(self.outputs)

    def open_input(self, name, callback):
        if name in self.fail_open:
            raise IOError('apertura fallita (finta)')
        port = self.inputs[name]
        port.closed = False
        port.callback = callback
        self.opened.append(('input', name))
        return port

    def open_output(self, name):
        if name in self.fail_open:
            raise IOError('apertura fallita (finta)')
        port = self.outputs[name]
        port.closed = False
        self.opened.append(('output', name))
        return port

    def add_output(self, name):
        self.outputs[name] = FakeMidiOutput(name)
        return self.outputs[name]

    def add_input(self, name):
        self.inputs[name] = FakeMidiInput(name)
        return self.inputs[name]


FLUID_NAME = 'FLUID Synth (1234):Synth input port (1234:0) 128:0'
FANTOM_NAME = 'FANTOM-08:FANTOM-08 MIDI 1 20:0'


def make_router(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME,), parts=None,
                mode='legacy', registry=None, devices_file=None, emit=None,
                console=None, start=True):
    """MidiRouter su FakeBackend (nessun mido/rtmidi). Ritorna (router, backend)."""
    import midi_router
    backend = FakeBackend(inputs, outputs)
    router = midi_router.MidiRouter(backend=backend, parts=parts, registry=registry,
                                    mode=mode, devices_file=devices_file, emit=emit,
                                    console=console, sf_id=lambda: 1)
    if start:
        router.start(poll=False, check_alsa=False)
    return router, backend

_APP = None


def import_app():
    """Importa app.py (Flask) senza effetti collaterali sul disco/rete.

    app.py all'import: (1) os.makedirs('/home/matteo/matsynth_web/presets|midi'),
    (2) istanzia una MultiTrackDAW, (3) startup_init_once() parla con la
    console FluidSynth su 127.0.0.1:9800 con 5 retry e sleep.
    Qui, SOLO durante l'import: makedirs diventa no-op, socket.socket
    solleva OSError (send_fluid cattura e ritorna ""), time.sleep dal thread
    principale e' no-op (i thread demone della DAW dormono davvero).
    Dopo l'import PRESETS_DIR/MIDI_DIR/STATE_FILE vanno ridirezionati dal
    chiamante su una cartella temporanea (vedi `redirect_app_paths`) e
    `app.send_fluid` sostituito con un registratore.
    """
    global _APP
    if _APP is not None:
        return _APP
    import socket
    import threading
    real_sleep = time.sleep

    def _sleep(s):
        if threading.current_thread() is not threading.main_thread():
            real_sleep(s)

    def _no_socket(*a, **k):
        raise OSError('rete disabilitata nei test')

    with mock.patch.object(os, 'makedirs', lambda *a, **k: None),          mock.patch.object(socket, 'socket', _no_socket),          mock.patch.object(time, 'sleep', _sleep),          _neutral_router():
        import app as app_module
    _APP = app_module
    return _APP


class _neutral_router:
    """Durante l'import di app.py: router senza thread di polling, senza
    `aconnect -l` e senza scritture del registro su disco (il bootstrap resta
    in RAM). `redirect_app_paths` poi punta DEVICES_FILE in una cartella
    temporanea."""

    def __enter__(self):
        import midi_router
        real_start = midi_router.MidiRouter.start

        def _start(router, poll=True, check_alsa=True):
            return real_start(router, poll=False, check_alsa=False)

        self._patches = [mock.patch.object(midi_router.MidiRouter, 'start', _start),
                         mock.patch.object(midi_router, 'atomic_write_json', lambda *a, **k: None)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def redirect_app_paths(app_module, tmp_dir):
    """Punta preset/midi/state di app.py in tmp_dir e registra i comandi
    FluidSynth. Ritorna la lista `commands` (stringhe passate a send_fluid)."""
    presets = os.path.join(tmp_dir, 'presets') + os.sep
    midis = os.path.join(tmp_dir, 'midi') + os.sep
    os.makedirs(presets, exist_ok=True)
    os.makedirs(midis, exist_ok=True)
    app_module.PRESETS_DIR = presets
    app_module.MIDI_DIR = midis
    app_module.STATE_FILE = os.path.join(tmp_dir, 'last_state.json')
    app_module.DEVICES_FILE = os.path.join(tmp_dir, 'midi_devices.json')
    router = getattr(app_module, 'router', None)
    if router is not None:
        router.devices_file = app_module.DEVICES_FILE
    commands = []

    def _fake_send_fluid(command):
        commands.append(command)
        return ''

    app_module.send_fluid = _fake_send_fluid
    app_module.sf_id = '1'
    return commands


def wait_until(predicate, timeout=3.0, step=0.005):
    """Polling breve per i thread di record/playback (evita sleep fissi)."""
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def load_json_fixture(*parts):
    import json
    with open(os.path.join(FIXTURES_DIR, *parts), 'r', encoding='utf-8') as f:
        return json.load(f)
