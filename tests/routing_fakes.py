"""Fake piu' severi per i test del router MIDI (fase `ui-routing-fixes`).

I fake di `helpers.py` chiamano la callback di input nel thread del test e
`close()` non aspetta nulla: nessuna concorrenza, quindi deadlock e note
sospese fra thread non sono rilevabili. Qui:

- `ThreadedMidiInput`: la callback gira in un thread dedicato, come il thread
  C di python-rtmidi; `close()` fa join di quel thread, come `closePort` di
  RtMidi su ALSA. Il join ha un timeout lungo (mai un test bloccato per
  sempre) e `join_timed_out` dice se e' scaduto.
- `FlakyMidiOutput`: `send()` puo' fallire (`fail = True`) o bloccarsi fino a
  `release()` (`block_next()`), per provare invio fallito e stato `offline`.
- `RoutingBackend`: FakeBackend con queste porte e con enumerazione che puo'
  fallire (`fail_enum`).

Nessun rtmidi: solo `mido.Message` reali. Non modifica `helpers.py`.
"""

import threading

import helpers
from helpers import FANTOM_NAME, FLUID_NAME

JOIN_TIMEOUT_S = 5.0


class ThreadedMidiInput(helpers.FakeMidiInput):
    """Input con callback su un thread proprio; `close()` fa join."""

    def __init__(self, name):
        super().__init__(name)
        self._cv = threading.Condition()
        self._items = []
        self._busy = False
        self._thread = None
        self.close_started = threading.Event()
        self.join_timed_out = False

    def feed(self, *messages):
        with self._cv:
            for m in messages:
                if self.callback is None or self.closed:
                    self._queue.append(m)
                    continue
                self._items.append(m)
            if self._items and self._thread is None:
                self._thread = threading.Thread(target=self._run, name='fake-rtmidi-in', daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                while not self._items and not self.closed:
                    self._cv.wait()
                if not self._items:            # chiusa e coda vuota: fine del thread
                    self._cv.notify_all()
                    return
                m = self._items.pop(0)
                cb = self.callback
                self._busy = True
            try:
                if cb is not None:
                    cb(m)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def wait_idle(self, timeout=2.0):
        """True quando tutti i messaggi accodati sono stati consegnati."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._items and not self._busy, timeout)

    def close(self):
        self.close_started.set()
        with self._cv:
            self.closed = True
            self._items.clear()                # come rtmidi: niente callback dopo la chiusura
            t = self._thread
            self._thread = None
            self._cv.notify_all()
        if t is not None and t is not threading.current_thread():
            t.join(JOIN_TIMEOUT_S)
            if t.is_alive():
                self.join_timed_out = True


class FlakyMidiOutput(helpers.FakeMidiOutput):
    """Output che puo' fallire o bloccarsi durante `send()`."""

    def __init__(self, name):
        super().__init__(name)
        self.fail = False
        self.entered = threading.Event()
        self._gate = None

    def block_next(self):
        """Il prossimo `send()` si blocca finche' il test chiama `release()`."""
        self.entered.clear()
        self._gate = threading.Event()

    def release(self):
        if self._gate is not None:
            self._gate.set()

    def send(self, msg):
        gate = self._gate
        if gate is not None:
            self.entered.set()
            gate.wait(JOIN_TIMEOUT_S)
            self._gate = None
        if self.fail:
            raise IOError('invio fallito (finto)')
        super().send(msg)


class RoutingBackend(helpers.FakeBackend):
    """FakeBackend con input a thread, output che possono fallire ed
    enumerazione che puo' fallire (`fail_enum` = {'input', 'output'})."""

    def __init__(self, inputs=(), outputs=()):
        super().__init__((), ())
        self.inputs = {n: self._wrap(ThreadedMidiInput(n)) for n in inputs}
        self.outputs = {n: self._wrap(FlakyMidiOutput(n)) for n in outputs}
        self.fail_enum = set()
        self.enum_calls = 0
        # io_probe(kind, name): chiamata a ogni apertura/chiusura porta, per
        # verificare dall'esterno quali lock il router tiene durante l'I/O.
        self.io_probe = None

    def _probe(self, kind, name):
        if self.io_probe is not None:
            self.io_probe(kind, name)

    def _wrap(self, port):
        real_close = port.close

        def close():
            self._probe('close_' + ('input' if isinstance(port, ThreadedMidiInput) else 'output'), port.name)
            real_close()
        port.close = close
        return port

    def open_input(self, name, callback):
        self._probe('open_input', name)
        return super().open_input(name, callback)

    def open_output(self, name):
        self._probe('open_output', name)
        return super().open_output(name)

    def get_input_names(self):
        self.enum_calls += 1
        if 'input' in self.fail_enum:
            raise OSError('ALSA non pronto (finto)')
        return list(self.inputs)

    def get_output_names(self):
        self.enum_calls += 1
        if 'output' in self.fail_enum:
            raise OSError('ALSA non pronto (finto)')
        return list(self.outputs)

    def add_output(self, name):
        self.outputs[name] = self._wrap(FlakyMidiOutput(name))
        return self.outputs[name]

    def add_input(self, name):
        self.inputs[name] = self._wrap(ThreadedMidiInput(name))
        return self.inputs[name]


def make_routing_router(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME,), parts=None,
                        mode='legacy', registry=None, devices_file=None, emit=None,
                        start=True, backend=None):
    """MidiRouter su RoutingBackend. Ritorna (router, backend)."""
    import midi_router
    backend = backend or RoutingBackend(inputs, outputs)
    router = midi_router.MidiRouter(backend=backend, parts=parts, registry=registry,
                                    mode=mode, devices_file=devices_file, emit=emit,
                                    console=None, sf_id=lambda: 1)
    if start:
        router.start(poll=False, check_alsa=False)
    return router, backend


def close_router(router, backend):
    """Chiusura a fine test: ferma i thread dei fake anche se un test fallisce."""
    for p in list(backend.outputs.values()):
        if isinstance(p, FlakyMidiOutput):
            p.release()
    try:
        router.close()
    except Exception:
        pass
    for p in list(backend.inputs.values()):
        p.close()
