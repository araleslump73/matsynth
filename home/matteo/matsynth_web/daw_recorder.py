"""
Micro-DAW Recorder Module
Ottimizzato per Raspberry Pi Zero 2W con bassa latenza e CPU overhead minimo
"""

import mido
import io
import time
import math
import threading
from array import array
from collections import defaultdict
from contextlib import contextmanager

import midi_router
import project_store

RECORD_GRACE_S = 0.01   # ignora i primi 10 ms di Record (eventi residui)


class DawError(Exception):
    """Rifiuto con codice di contratto (sez. 2.3). NON e' un RuntimeError:
    `import_midi` usa RuntimeError solo per "trasporto attivo" (route -> 409)."""

    def __init__(self, code, message, details=None, http=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.http = http


def _ids_zero(n):
    """array('H') di n zeri (2 byte per evento: leggero sul Pi)."""
    return array('H', bytes(2 * n))


class MultiTrackDAW:
    """
    Gestisce registrazione e riproduzione multi-traccia MIDI
    con sincronizzazione automatica e routing intelligente
    """
    
    def __init__(self, bpm=120, socketio=None, router=None):
        """
        Inizializza il sistema DAW
        
        Args:
            bpm: Beats per minute (default 120)
            socketio: Flask-SocketIO instance per eventi real-time (optional)
            router: midi_router.MidiRouter configurato (app.py). Se None la DAW
                crea un router solo in memoria (bootstrap del registro dalle
                porte enumerate, parti di default, legacy, nessun disco).
        """
        # Stato per-thread (richieste Flask concorrenti): ultimo errore e
        # revisione risultante dell'ultima mutazione di QUESTO thread.
        self._tls = threading.local()
        # Lock di editing (sez. 6.7): controllo revisione + mutazione + sort.
        # MAI tenuto durante I/O (disco, console, MIDI) ne' durante emit:
        # l'emissione di daw_state_update e l'hook di dirty partono dopo il
        # rilascio (vedi _edit()). Rientrante: le mutazioni si annidano.
        self._edit_lock = threading.RLock()
        self._edit_depth = 0
        self._edit_owner = None
        self._emit_pending = False
        self._content_pending = False
        # Revisione globale (sez. 6.6): non si ripete tra riavvii, < 2^53.
        self.revision = int(time.time() * 1000)
        # Hook "contenuto del progetto cambiato" (sez. 5.6), collegato da app.py
        # a project_store.ProjectState.mark_dirty. Chiamato fuori dal lock.
        self.on_content_change = None

        self.bpm = bpm
        self.beat_duration = 60.0 / bpm  # durata di un beat in secondi
        self.beats_per_measure = 4       # 4/4 di default
        self.slot_beats = 0.125          # risoluzione densità (1/8 di beat)
        self.socketio = socketio  # WebSocket per notifiche real-time

        # Storage tracce: {channel_id: [(beat_position, midi_msg), ...]}
        # Gli eventi sono salvati in beat (unità musicale) per supportare cambi di BPM
        self.tracks = defaultdict(list)
        # ID del clip proprietario di ogni evento (D3), parallelo a tracks[t]:
        # stessa lunghezza, stesso ordine, permutati insieme in OGNI mutazione.
        # array('H') = 2 byte per evento (16x500 coppie: ~32 KB).
        self.event_clips = {ch: array('H') for ch in range(16)}
        for ch in range(16):
            self.tracks[ch]

        # Clip model: one clip per recording take (MVP clip-based workflow)
        self.clips = defaultdict(list)   # {channel_id: [{id,start_beat,end_beat,name,created_at}, ...]}
        self._next_clip_id = 1
        self._recording_channels = []
        self._record_start_event_index = {}
        self._record_take_start_beat = 0.0
        
        # Stato di ogni traccia (16 canali MIDI)
        self.armed = {i: False for i in range(16)}      # traccia pronta per registrazione
        self.muted = {i: False for i in range(16)}      # traccia mutata
        self.has_data = {i: False for i in range(16)}   # traccia ha eventi registrati
        self.solo = {i: False for i in range(16)}        # traccia in solo
        self._any_solo = False                            # cache: almeno una traccia in solo
        self.track_names = {i: f'Track {i+1}' for i in range(16)}
        self.track_colors = {i: i for i in range(16)}    # index into 16-color palette
        
        # Stato del trasporto
        self.is_recording = False
        self.is_playing = False
        self.timeline_position = 0.0  # posizione corrente in secondi
        
        # Metronomo
        self.metronome_enabled = False
        self.metronome_thread = None
        self.last_beat = -1  # ultimo beat suonato (per evitare duplicati)
        
        # Loop points
        self.loop_enabled = False
        self.loop_start = 0.0    # beat position
        self.loop_end = 0.0      # beat position
        
        # Thread control
        self.record_thread = None
        self.playback_thread = None
        self.update_thread = None  # Thread per aggiornamenti periodici via WebSocket
        self.stop_event = threading.Event()
        self.metronome_stop_event = threading.Event()

        # Undo/Redo stack (max 10 livelli, leggero per Pi Zero)
        self._undo_stack = []
        self._redo_stack = []
        self._MAX_UNDO = 10

        # Conteggi attivi e densità
        self.active_notes = {i: 0 for i in range(16)}
        self._last_tick_counts = {i: 0 for i in range(16)}
        self._last_counts_emit_ts = 0.0

        # Density / note map cache (P2-7)
        self._density_dirty = True
        self._cached_density_map = None
        self._cached_note_map = None

        # Clipboard for copy/paste (P2-3)
        self._clipboard = []        # list of (relative_beat, midi_bytes)
        self._clipboard_channel = 0  # source channel
        # {kind: range|clip, length_beats, source_track, source_clip_id, name,
        #  sustain_release}: un solo appunti in RAM, condiviso, mai salvato (6.4).
        self._clipboard_meta = None
        # D6: beat di inizio dell'ultimo Record (None fuori dal Record) e opzione
        # "torna al punto di partenza" scelta all'avvio del Record.
        self.record_start_beat = None
        self._record_return_to_start = True
        
        # Porte MIDI: tutte tramite il router (unico lettore per input,
        # unica risoluzione delle destinazioni, tracciamento note).
        self._audible = bytearray(b'\x01' * 16)   # gate mute/solo letto sotto il lock della porta
        # La callback di Record appende a tracks[t] e event_clips[t] sotto lo
        # STESSO lock delle mutazioni e degli snapshot (ui-persistence): una
        # copia per salvataggio/undo non vede mai le due strutture disallineate.
        self._rec_lock = self._edit_lock
        self._rec_tracks = frozenset()
        self._rec_start_time = 0.0
        self._rec_initial_pos = 0.0
        self.last_error = None       # {code, message, details, http} dell'ultimo rifiuto
        self.last_warnings = []
        self.last_panic = []
        self._owns_router = router is None
        self.router = router
        self._init_midi_ports()
        
        # Avvia thread per aggiornamenti periodici WebSocket
        self._start_update_thread()
    
    def _init_midi_ports(self):
        """Collega la DAW al router; senza router esterno ne crea uno in memoria."""
        if self.router is None:
            self.router = midi_router.MidiRouter()
            self.router.start(poll=False, check_alsa=False)
        self.router.set_recorder(self._on_router_input)
        print(f"[DAW] MIDI input: {getattr(self.midi_input, 'name', None)}, "
              f"interno: {self.router.internal_status()}")

    @property
    def midi_input(self):
        """Compatibilita': porta di `keyboard-main` aperta dal router (o None)."""
        return self.router.primary_input_port() if self.router else None

    @property
    def midi_output(self):
        """Compatibilita': porta interna FluidSynth aperta dal router (o None)."""
        return self.router.internal_port_object() if self.router else None

    @property
    def _density_dirty(self):
        # Le due cache (densita' e note map) erano invalidate da un flag unico
        # che la prima ricalcolata azzerava: l'altra restava vecchia. Ora
        # "sporco" svuota entrambe e ogni mappa ricalcola solo se manca.
        return False

    @_density_dirty.setter
    def _density_dirty(self, value):
        if value:
            self._cached_density_map = None
            self._cached_note_map = None
            # Una mappa calcolata FUORI dal lock su uno snapshot precedente non
            # deve finire in cache dopo un'invalidazione (vedi _note_map).
            self._cache_gen = getattr(self, '_cache_gen', 0) + 1

    def _fail(self, code, message, details=None, http=None):
        self.last_error = {'code': code, 'message': message,
                           'details': details or {}, 'http': http}
        print(f"[DAW] {code}: {message}")
        return False

    def _update_audible(self):
        any_solo = any(self.solo.values())
        self._any_solo = any_solo
        for ch in range(16):
            self._audible[ch] = 1 if (self.solo[ch] if any_solo else not self.muted[ch]) else 0
    
    # ── stato per-thread ──────────────────────────────────────────────

    @property
    def last_error(self):
        """{code, message, details, http} dell'ultimo rifiuto di QUESTO thread
        (le richieste Flask concorrenti non si sovrascrivono l'errore)."""
        return getattr(self._tls, 'last_error', None)

    @last_error.setter
    def last_error(self, value):
        self._tls.last_error = value

    @property
    def last_revision(self):
        """Revisione letta sotto il lock alla fine dell'ultima mutazione di
        QUESTO thread: le route la restituiscono al client (mai `revision`
        riletta dopo, che potrebbe includere modifiche di altri client)."""
        return getattr(self._tls, 'revision', self.revision)

    # ── lock di editing e notifiche differite (sez. 6.7) ──────────────

    @contextmanager
    def _edit(self):
        """Sezione critica di editing. All'uscita piu' esterna: revisione
        risultante salvata per il thread, poi (FUORI dal lock) un solo
        daw_state_update e un solo hook on_content_change."""
        self._edit_lock.acquire()
        if self._edit_depth == 0:
            self._edit_owner = threading.get_ident()
        self._edit_depth += 1
        state = None
        content = False
        outer = False
        try:
            yield
        finally:
            self._edit_depth -= 1
            if self._edit_depth == 0:
                outer = True
                emit, content = self._emit_pending, self._content_pending
                self._emit_pending = self._content_pending = False
                self._edit_owner = None
                self._tls.revision = self.revision
                if emit and self.socketio:
                    try:
                        state = self.get_state()
                    except Exception as e:
                        print(f"[DAW] get_state fallito: {e}")
            self._edit_lock.release()
            if outer:
                if state is not None:
                    try:
                        self.socketio.emit('daw_state_update', state, namespace='/')
                    except Exception as e:
                        print(f"[DAW] Errore emit daw_state_update: {e}")
                if content:
                    self._call_content_hook()

    def _in_edit(self):
        return self._edit_owner == threading.get_ident()

    def _call_content_hook(self):
        hook = self.on_content_change
        if hook is not None:
            try:
                hook()
            except Exception as e:
                print(f"[DAW] on_content_change fallito: {e}")

    def _content_changed(self):
        """Il contenuto del progetto e' cambiato (sez. 5.6): hook differito
        all'uscita dal lock se siamo dentro una mutazione."""
        if self._in_edit():
            self._content_pending = True
        else:
            self._call_content_hook()

    def check_revision(self, expected):
        """Controllo `expected_revision` (sez. 6.6), da chiamare sotto il lock.
        None = controllo saltato (client legacy). Ritorna False e imposta
        last_error REVISION_CONFLICT se obsoleta (API per ui-studio)."""
        if expected is None or expected == self.revision:
            return True
        self._fail('REVISION_CONFLICT', 'Revisione obsoleta: aggiorna e riprova',
                   {'expected_revision': expected, 'current_revision': self.revision})
        return False

    # ── strutture parallele tracks / event_clips (D3) ─────────────────

    def _ids(self, ch):
        """event_clips[ch] allineato a tracks[ch]. Se qualcuno ha assegnato
        tracks[ch] senza event_clips (vecchi test/script) gli eventi sono
        considerati liberi invece di disallineare gli ID."""
        ids = self.event_clips.get(ch)
        n = len(self.tracks[ch])
        if ids is None or len(ids) != n:
            if ids is not None and len(ids):
                print(f"[DAW] event_clips[{ch}] disallineato ({len(ids)} vs {n}): eventi resi liberi")
            ids = _ids_zero(n)
            self.event_clips[ch] = ids
        return ids

    def _set_track(self, ch, events, ids):
        if len(events) != len(ids):
            raise DawError('INTERNAL', f'tracks/event_clips disallineati su traccia {ch}')
        self.tracks[ch] = events
        self.event_clips[ch] = ids
        self.has_data[ch] = bool(events)

    @staticmethod
    def _sorted_pair(events, ids):
        """Un solo sort stabile sugli indici, poi permutazione di entrambe
        (sez. 6.1). Liste nuove: il playback che itera la vecchia non vede
        mai una lista a meta' sort."""
        order = sorted(range(len(events)), key=lambda i: events[i][0])
        return [events[i] for i in order], array('H', [ids[i] for i in order])

    def _set_sorted(self, ch, events, ids):
        ev, ix = self._sorted_pair(events, ids)
        self._set_track(ch, ev, ix)

    def _drop_indices(self, ch, drop):
        events, ids = self.tracks[ch], self._ids(ch)
        keep = [i for i in range(len(events)) if i not in drop]
        self._set_track(ch, [events[i] for i in keep], array('H', [ids[i] for i in keep]))

    def _free_orphan_ids(self, ch):
        """Eventi il cui clip non esiste piu' sulla traccia -> liberi (0):
        un riferimento pendente renderebbe il progetto non riapribile."""
        valid = {int(c['id']) for c in self.clips[ch]}
        ids = self._ids(ch)
        for i, cid in enumerate(ids):
            if cid and cid not in valid:
                ids[i] = 0

    def _alloc_clip_id(self):
        cid = self._next_clip_id
        if cid > project_store.MAX_CLIP_ID:
            raise DawError('INTERNAL', 'Limite di ID clip del progetto raggiunto')
        self._next_clip_id = cid + 1
        return cid

    def _clip_at(self, ch, beat):
        """ID del clip piu' recente (ID maggiore) che contiene `beat` (6.3), o 0."""
        best = 0
        for c in self.clips[ch]:
            if c['start_beat'] <= beat < c['end_beat'] and int(c['id']) > best:
                best = int(c['id'])
        return best

    def _emit_state_change(self):
        """Emette lo stato DAW corrente via WebSocket (differito all'uscita
        dal lock se chiamato dentro una mutazione)."""
        if self._in_edit():
            self._emit_pending = True
            return
        if self.socketio:
            state = self.get_state()
            self.socketio.emit('daw_state_update', state, namespace='/')
    
    def _start_update_thread(self):
        """Avvia thread per aggiornamenti periodici durante registrazione/playback"""
        self.update_thread = threading.Thread(
            target=self._update_loop,
            daemon=True
        )
        self.update_thread.start()
    
    def _update_loop(self):
        """Loop che emette aggiornamenti periodici via WebSocket"""
        while True:
            try:
                if (self.is_recording or self.is_playing) and self.socketio:
                    # Tick leggero per timeline/playhead (payload minimo)
                    tick_payload = {
                        't': self.timeline_position,
                        'is_playing': self.is_playing,
                        'is_recording': self.is_recording,
                        'bpm': self.bpm,
                        'beats_per_measure': self.beats_per_measure
                    }

                    # Invia track_counts solo se sono cambiati (limita il payload)
                    counts_changed = False
                    counts = {}
                    for ch in range(16):
                        cnt = len(self.tracks[ch])
                        counts[ch] = cnt
                        if cnt != self._last_tick_counts.get(ch, 0):
                            counts_changed = True
                            self._last_tick_counts[ch] = cnt

                    now = time.time()
                    if counts_changed and (now - self._last_counts_emit_ts) >= 0.25:
                        track_durations = {}
                        max_duration = 0.0
                        for ch in range(16):
                            if self.tracks[ch]:
                                last_beat = self.tracks[ch][-1][0]
                                duration = last_beat * self.beat_duration
                                track_durations[ch] = duration
                                max_duration = max(max_duration, duration)

                        tick_payload['track_counts'] = counts
                        tick_payload['track_durations'] = track_durations
                        tick_payload['max_duration'] = max_duration
                        self._last_counts_emit_ts = now

                    try:
                        self.socketio.emit('daw_tick', tick_payload, namespace='/')
                    except Exception as e:
                        print(f"[DAW] Errore emit daw_tick: {e}")

                    # Eventi di attività note solo durante la registrazione
                    if self.is_recording:
                        snapshot = [{'channel': ch, 'count': cnt} for ch, cnt in self.active_notes.items() if cnt > 0]
                        try:
                            if snapshot:
                                self.socketio.emit('record_activity', {'channels': snapshot}, namespace='/')
                        except Exception as e:
                            print(f"[DAW] Errore emit record_activity: {e}")

                    time.sleep(0.05 if self.is_recording else 0.1)
                else:
                    time.sleep(0.5)
            except Exception as e:
                print(f"[DAW] Errore in update loop: {e}")
                time.sleep(0.5)
    
    def start_recording(self, channel=None, return_to_start=True):
        """
        Avvia la registrazione su canali armati

        Args:
            channel: Canale MIDI (0-15) da registrare, o None per tutti i canali armati
            return_to_start: D6 (decisione utente): dopo lo Stop di QUESTO Record
                la posizione torna al punto di partenza (default True).

        Returns:
            bool: True se registrazione avviata, False altrimenti
        """
        print(f"[DAW] start_recording chiamato con channel={channel}")
        self.last_error = None
        
        # Se channel specificato, verifica che sia armato
        if channel is not None:
            if not self.armed[channel]:
                return self._fail('NO_ARMED_TRACK', f"Canale {channel} non armato")
        else:
            # Se nessun channel specificato, verifica che almeno uno sia armato
            if not any(self.armed.values()):
                return self._fail('NO_ARMED_TRACK', "Nessun canale armato")
        
        if self.is_recording:
            return self._fail('TRANSPORT_ACTIVE', "Registrazione gia' in corso",
                              {'is_playing': self.is_playing, 'is_recording': True}, http=400)
        if self.is_playing:
            # A23: niente secondo thread di playback.
            return self._fail('TRANSPORT_ACTIVE', "Ferma la riproduzione prima di registrare",
                              {'is_playing': True, 'is_recording': False})
        
        # Determina i canali da registrare
        if channel is not None:
            recording_channels = [channel]
        else:
            recording_channels = [ch for ch in range(16) if self.armed[ch]]

        # Preflight (A3, D7): ogni traccia armata deve avere un input disponibile,
        # verificato PRIMA di impostare is_recording.
        problems = self.router.record_preflight(recording_channels)
        if problems:
            return self._fail('PORT_UNAVAILABLE', 'Ingresso MIDI non disponibile per: ' +
                              ', '.join(f"traccia {p['track'] + 1}" for p in problems),
                              {'tracks': problems})
        
        # Rilascio delle note rimaste aperte (solo canali toccati da MatSynth)
        self._send_all_notes_off()

        for ch in range(16):
            self.active_notes[ch] = 0

        # NON resettare timeline position - registra dalla posizione corrente
        # (solo REWIND resetta a 0.0)
        initial_position = self.timeline_position

        # Indici di partenza del take e is_recording nello STESSO lock delle
        # mutazioni (review L3): una modifica indicizzata o ne finisce prima
        # (indici giusti) o vede is_recording e viene rifiutata.
        with self._edit_lock:
            if self.is_recording or self.is_playing:
                return self._fail('TRANSPORT_ACTIVE', "Trasporto gia' attivo",
                                  {'is_playing': self.is_playing, 'is_recording': self.is_recording})
            self._recording_channels = list(recording_channels)
            self._record_start_event_index = {ch: len(self.tracks[ch]) for ch in recording_channels}
            # Se ci sono altre tracce con dati (non in registrazione), avvia anche la riproduzione
            has_other_tracks = any(self.has_data[ch] for ch in range(16) if ch not in recording_channels)
            self.is_recording = True
            self.stop_event.clear()
            self._record_take_start_beat = max(0.0, initial_position / self.beat_duration)
            # D6: punto di partenza in BEAT (resta giusto anche se il BPM cambia).
            self.record_start_beat = self._record_take_start_beat
            self._record_return_to_start = bool(return_to_start)
            # Gli eventi arrivano dalla callback del router (_on_router_input);
            # il thread di Record fa solo avanzare la timeline.
            self._rec_start_time = time.time()
            self._rec_initial_pos = initial_position
            self._rec_tracks = frozenset(recording_channels)
        
        # Thread di registrazione (registra su tutti i canali armati)
        self.record_thread = threading.Thread(
            target=self._record_loop, 
            args=(recording_channels, initial_position),
            daemon=True
        )
        self.record_thread.start()
        
        # Se ci sono tracce esistenti, avvia playback simultaneo
        if has_other_tracks:
            self.is_playing = True
            self.playback_thread = threading.Thread(
                target=self._playback_loop,
                daemon=True
            )
            self.playback_thread.start()
        
        # Avvia metronomo se abilitato
        if self.metronome_enabled:
            self._start_metronome()
        
        print(f"[DAW] Registrazione avviata su canali {recording_channels} da posizione {initial_position:.1f}s")
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def stop_recording(self):
        """Ferma la registrazione in corso"""
        if not self.is_recording:
            return False
        
        stop_beat = max(0.0, self.timeline_position / self.beat_duration)
        with self._rec_lock:
            self._rec_tracks = frozenset()     # la callback del router smette di registrare
        self.is_recording = False
        self.stop_event.set()

        for ch in range(16):
            self.active_notes[ch] = 0

        if self.record_thread:
            self.record_thread.join(timeout=1.0)
        # Il playback delle altre tracce avviato dal Record condivide stop_event
        # e quindi termina anche lui: lo si attende QUI (fuori dal lock) perche'
        # finche' gira riscrive timeline_position e annullerebbe il ritorno D6.
        pb = self.playback_thread
        if pb is not None and pb.is_alive() and pb is not threading.current_thread():
            pb.join(timeout=1.0)
        if self.is_playing:
            self.is_playing = False
            self._send_all_notes_off()
        self._stop_metronome()          # trasporto fermo: niente click residuo

        with self._edit():
            for ch in self._recording_channels:
                self._close_open_recorded_notes(ch, stop_beat)
            # Clip del take sugli eventi in coda (ancora non ordinati), poi un
            # solo sort permutato per traccia (ui-persistence: D3).
            if self._finalize_recording_clips():
                self.revision += 1          # una volta sola allo stop (sez. 6.6)
                self._content_changed()
            # D6 (A24): ritorno al punto di partenza del Record, deciso dal
            # backend (il client non calcola piu' nulla). Solo posizione: dati
            # e clip invariati. Un solo daw_state_update (all'uscita dal lock).
            if self._record_return_to_start and self.record_start_beat is not None:
                self.timeline_position = self.record_start_beat * self.beat_duration
            self.record_start_beat = None
            print("[DAW] Registrazione fermata")
            self._emit_state_change()  # Notifica via WebSocket
        return True

    def _close_open_recorded_notes(self, ch, stop_beat):
        """Note-on senza note-off e CC64 >= 64 non chiuso nel take appena
        registrato ricevono note-off / CC64=0 al beat di stop (sez. 3.7)."""
        start_idx = self._record_start_event_index.get(ch, len(self.tracks[ch]))
        new_events = self.tracks[ch][start_idx:]
        if not new_events:
            return
        open_notes = {}
        sustain = {}
        last_beat = 0.0
        for beat, data in new_events:
            last_beat = max(last_beat, beat)
            if len(data) < 3:
                continue
            kind, mch = data[0] & 0xF0, data[0] & 0x0F
            if kind == 0x90 and data[2] > 0:
                open_notes[(mch, data[1])] = open_notes.get((mch, data[1]), 0) + 1
            elif kind == 0x80 or kind == 0x90:
                key = (mch, data[1])
                if open_notes.get(key):
                    open_notes[key] -= 1
            elif kind == 0xB0 and data[1] == 64:
                sustain[mch] = data[2] >= 64
        beat = max(stop_beat, last_beat)
        added = 0
        ids = self._ids(ch)
        for (mch, note), count in sorted(open_notes.items()):
            for _ in range(count):
                self.tracks[ch].append((beat, [0x80 | mch, note, 0]))
                ids.append(0)
                added += 1
        for mch, held in sorted(sustain.items()):
            if held:
                self.tracks[ch].append((beat, [0xB0 | mch, 64, 0]))
                ids.append(0)
                added += 1
        if added:
            print(f"[DAW] Stop Record: chiusi {added} eventi aperti su traccia {ch}")

    def _finalize_recording_clips(self):
        """Clip del take appena registrato (sez. 6.4, D3): solo gli eventi di
        questo take (la coda di tracks[ch], non ancora ordinata) ricevono l'ID
        del clip nuovo; cornice [primo, ultimo + 0.125) cosi' anche il
        note-off/CC64=0 di chiusura e' contenuto. Poi sort permutato.
        Ritorna True se almeno una traccia ha eventi nuovi. Chiamare sotto _edit()."""
        channels = self._recording_channels or []
        recorded = False
        for ch in channels:
            ids = self._ids(ch)
            start_idx = min(self._record_start_event_index.get(ch, len(self.tracks[ch])),
                            len(self.tracks[ch]))
            new_events = self.tracks[ch][start_idx:]
            if not new_events:
                continue
            recorded = True
            start_beat = min(b for b, _ in new_events)
            end_beat = max(b for b, _ in new_events) + project_store.CLIP_TAIL_BEATS

            if self._next_clip_id > project_store.MAX_CLIP_ID:
                # Review L2: limite ID raggiunto -> take libero (ID 0), niente
                # eccezione a meta' dello stop; i dati restano e suonano.
                print(f"[DAW] Limite ID clip raggiunto: take libero su traccia {ch}")
                self._set_sorted(ch, self.tracks[ch], ids)
                self._density_dirty = True
                continue
            clip_id = self._alloc_clip_id()
            for i in range(start_idx, len(ids)):
                ids[i] = clip_id
            self.clips[ch].append({
                'id': clip_id,
                'start_beat': float(start_beat),
                'end_beat': float(end_beat),
                'name': f'Take {clip_id}',
                'created_at': time.time()
            })
            self._set_sorted(ch, self.tracks[ch], ids)
            self._density_dirty = True
            print(f"[DAW] Registrati {len(self.tracks[ch])} eventi su canale {ch}")

        self._recording_channels = []
        self._record_start_event_index = {}
        self._record_take_start_beat = 0.0
        return recorded
    
    def start_playback(self):
        """Avvia la riproduzione di tutte le tracce registrate"""
        self.last_error = None
        self.last_warnings = []
        if self.is_playing or self.is_recording:
            return self._fail('NOTHING_TO_PLAY', "Riproduzione o registrazione gia' in corso")
        
        # Verifica che ci siano tracce da riprodurre
        if not any(self.has_data.values()):
            return self._fail('NOTHING_TO_PLAY', 'Nessuna traccia da riprodurre')

        # Preflight (A3): nessuna destinazione disponibile -> rifiuto; alcune -> warning.
        self._update_audible()
        tracks = [ch for ch in range(16) if self.has_data[ch] and self._audible[ch]] \
            or [ch for ch in range(16) if self.has_data[ch]]
        problems = self.router.playback_preflight(tracks)
        if len(problems) == len(tracks):
            return self._fail('PORT_UNAVAILABLE', 'Nessuna destinazione MIDI disponibile per la riproduzione',
                              {'tracks': problems})
        if problems:
            self.last_warnings = [{'code': 'PLAYBACK_TRACKS_UNAVAILABLE',
                                   'message': 'Alcune tracce non hanno una destinazione disponibile',
                                   'details': {'tracks': problems}}]
        
        # Assicura che non ci siano note bloccate prima di iniziare
        self._send_all_notes_off()
        
        self.is_playing = True
        self.stop_event.clear()
        # NON resettare timeline_position - riprendi da dove sei
        # (solo REWIND resetta a 0.0)
        
        self.playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True
        )
        self.playback_thread.start()
        
        # Avvia metronomo se abilitato
        if self.metronome_enabled:
            self._start_metronome()
        
        print(f"[DAW] Playback avviato da posizione {self.timeline_position:.1f}s")
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def stop_playback(self):
        """Ferma la riproduzione"""
        if not self.is_playing:
            return False
        
        self.is_playing = False
        self.stop_event.set()
        
        if self.playback_thread:
            self.playback_thread.join(timeout=1.0)
        
        # Invia "All Notes Off" a tutti i canali
        self._send_all_notes_off()
        
        print("[DAW] Playback fermato")
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def stop_all(self):
        """Ferma registrazione e riproduzione"""
        self.stop_recording()
        self.stop_playback()
        self._stop_metronome()  # Ferma anche il metronomo
        self._send_all_notes_off()
        self._emit_state_change()  # Notifica via WebSocket
        return True

    def panic(self):
        """PANIC (sez. 3.7) su tutte le destinazioni aperte: note-off tracciati,
        CC64=0, CC123, CC120 sui 16 canali. Esito per porta in `last_panic`."""
        self.last_panic = self.router.panic()
        return True
    
    def rewind(self):
        """Riporta la timeline a 00:00:00"""
        # Ferma tutto
        self.stop_all()
        
        # Reset timeline
        self.timeline_position = 0.0
        self._emit_state_change()  # Notifica via WebSocket
        
        return True

    def set_position(self, seconds):
        """
        Imposta la posizione della timeline (solo quando non in registrazione/riproduzione)
        
        Args:
            seconds: posizione in secondi (>= 0)
        Returns:
            bool: True se posizione impostata, False altrimenti
        """
        if self.is_recording or self.is_playing:
            return False
        self.timeline_position = max(0.0, float(seconds))
        self._emit_state_change()
        return True

    def seek_to_beat(self, beat):
        """
        Seek to a beat position. Works during playback (stop → seek → restart).
        During recording, seek is blocked.
        
        Args:
            beat: target position in beats (>= 0)
        Returns:
            bool: True if seek was performed
        """
        if self.is_recording:
            return False
        seconds = max(0.0, float(beat)) * self.beat_duration
        was_playing = self.is_playing
        if was_playing:
            self.stop_playback()
        self.timeline_position = seconds
        if was_playing:
            self.start_playback()
        else:
            self._emit_state_change()
        return True

    def set_loop_points(self, start_beat, end_beat):
        """Imposta i punti di loop (in beat). end_beat deve essere > start_beat."""
        if end_beat <= start_beat:
            return False
        self.loop_start = float(start_beat)
        self.loop_end = float(end_beat)
        self._emit_state_change()
        self._content_changed()          # punti di loop: parte del progetto (5.6)
        return True

    def toggle_loop(self):
        """Attiva/disattiva il loop tra loop_start e loop_end."""
        if self.loop_end > self.loop_start:
            self.loop_enabled = not self.loop_enabled
        else:
            self.loop_enabled = False
        self._emit_state_change()
        self._content_changed()
        return self.loop_enabled

    
    def arm_track(self, channel, armed=True):
        """
        Arma/disarma una traccia per la registrazione
        
        Args:
            channel: Canale MIDI (0-15)
            armed: True per armare, False per disarmare
        """
        self.armed[channel] = armed
        print(f"[DAW] arm_track: state updated, armed[{channel}]={self.armed[channel]}")
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def mute_track(self, channel, muted=True):
        """
        Muta/smuta una traccia
        
        Args:
            channel: Canale MIDI (0-15)
            muted: True per mutare, False per smutare
        """
        self.muted[channel] = muted
        self._apply_audibility()
        self._emit_state_change()  # Notifica via WebSocket
        self._content_changed()
        return True
    
    def solo_track(self, channel, solo=True):
        """
        Attiva/disattiva il solo su una traccia.
        Quando almeno una traccia è in solo, solo quelle producono suono.
        """
        self.solo[channel] = solo
        self._apply_audibility()
        self._emit_state_change()
        self._content_changed()
        return True

    def _apply_audibility(self):
        """Mute/solo con effetto immediato (A25): il gate viene letto dal
        router sotto il lock della porta a ogni invio; le tracce diventate
        mute rilasciano subito note e sustain."""
        before = bytes(self._audible)
        self._update_audible()
        silenced = [ch for ch in range(16) if before[ch] and not self._audible[ch]]
        if silenced and self.is_playing:
            self.router.release_tracks(silenced)
    
    def _refuse_clear_in_record(self):
        """Review M2: Svuota durante il Record romperebbe gli indici del take
        (ownership dei clip). Consentito in Play (contratto 6.4)."""
        if self.is_recording:
            self._fail('RECORDING_ACTIVE', 'Ferma la registrazione prima di svuotare',
                       {'is_recording': True})
            return True
        return False

    def clear_track(self, channel):
        """Cancella tutti gli eventi di una traccia. None (last_error) in Record."""
        self.last_error = None
        with self._edit():
            if self._refuse_clear_in_record():
                return None
            self._push_undo()
            self._set_track(channel, [], array('H'))
            self.clips[channel] = []
            self._density_dirty = True
            print(f"[DAW] Traccia {channel} cancellata")
            self._emit_state_change()
        return True
    
    def clear_range(self, channel, start_beat, end_beat, expected_revision=None):
        """Cancella intervallo (sez. 6.4): stesso insieme di indici e stessa
        regola clip di Taglia, senza appunti. Note con note-on in [start, end)
        rimosse con il loro note-off (6.3); clip INTERAMENTE contenuti rimossi,
        quelli parzialmente sovrapposti restano (A16). Vietato con Play/Record.
        Ritorna {removed, clips_removed} oppure None (last_error)."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        rng = self._range_ok(start_beat, end_beat)
        if rng is None:
            return None
        start, end = rng
        with self._edit():
            if self._refuse_transport() or not self.check_revision(expected_revision):
                return None
            res = self._remove_range_locked(channel, start, end)
            if res['removed'] or res['clips_removed']:
                print(f"[DAW] clear_range ch={channel} [{start:.2f}, {end:.2f}) "
                      f"rimossi {res['removed']} eventi, clip {res['clips_removed']}")
        return res

    def _remove_range_locked(self, channel, start, end, indices=None):
        """Rimozione di Taglia/Cancella intervallo (sotto _edit, validazione
        gia' fatta). Un livello di undo solo se qualcosa cambia."""
        events = self.tracks[channel]
        if indices is None:
            indices = self._owned_by_id(events, self._ids(channel), start, end) if events else set()
        contained = [int(c['id']) for c in self.clips[channel]
                     if c['start_beat'] >= start and c['end_beat'] <= end]
        if not indices and not contained:
            return {'removed': 0, 'clips_removed': []}
        self._push_undo()
        if indices:
            self._drop_indices(channel, indices)
        if contained:
            gone = set(contained)
            self.clips[channel] = [c for c in self.clips[channel] if int(c['id']) not in gone]
            # Un note-off di un clip rimosso accoppiato (FIFO) a un note-on prima
            # di `start` resta sulla traccia: diventa libero, mai un ID pendente.
            self._free_orphan_ids(channel)
        self._density_dirty = True
        self._emit_state_change()
        return {'removed': len(indices), 'clips_removed': sorted(contained)}

    def clear_all_tracks(self):
        """Cancella tutte le tracce. None (last_error) in Record."""
        self.last_error = None
        with self._edit():
            if self._refuse_clear_in_record():
                return None
            self._push_undo()
            for ch in range(16):
                self._set_track(ch, [], array('H'))
                self.clips[ch] = []
            self._density_dirty = True
            print("[DAW] Tutte le tracce cancellate")
            self._emit_state_change()
        return True

    # ──────────────────────────────────────────────────────────────────
    # UNDO / REDO (leggero per Pi Zero 2W, max 10 livelli)
    # ──────────────────────────────────────────────────────────────────

    def _take_snapshot(self, transport=False):
        """Cattura stato corrente tracce (copie superficiali: le tuple e le liste
        di byte sono condivise, immutabili per contratto; event_clips copiato:
        2 byte/evento). Le liste vanno COPIATE: la callback di Record appende
        in place, uno snapshot per riferimento assorbirebbe il take.
        `transport=True` (import MIDI) include BPM e metro."""
        with self._edit_lock:
            snap = {
                'tracks': {ch: list(self.tracks[ch]) for ch in range(16)},
                'event_clips': {ch: array('H', self._ids(ch)) for ch in range(16)},
                'clips': {ch: [dict(c) for c in self.clips[ch]] for ch in range(16)},
                'next_clip_id': self._next_clip_id,
                'has_data': self.has_data.copy(),
                'track_names': self.track_names.copy(),
                'track_colors': self.track_colors.copy()
            }
            if transport:
                snap['transport'] = (self.bpm, self.beats_per_measure)
            return snap

    def _push_undo(self, transport=False):
        """Salva snapshot nello stack undo. Chiamare PRIMA dell'operazione
        distruttiva e DOPO la validazione (A21), sotto _edit(). Incrementa la
        revisione (sez. 6.6) e marca il progetto modificato (5.6)."""
        self._undo_stack.append(self._take_snapshot(transport))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self.revision += 1
        self._density_dirty = True
        self._content_changed()

    def _restore_snapshot(self, snapshot):
        """Applica uno snapshot alle tracce."""
        for ch in range(16):
            events = snapshot['tracks'].get(ch, [])
            ids = snapshot.get('event_clips', {}).get(ch)
            if ids is None or len(ids) != len(events):
                ids = _ids_zero(len(events))
            self.tracks[ch] = events
            self.event_clips[ch] = ids
            self.clips[ch] = [dict(c) for c in snapshot.get('clips', {}).get(ch, [])]
            self.has_data[ch] = snapshot['has_data'].get(ch, False)
        # A19 (ui-studio): il contatore non decresce mai, altrimenti un ID gia'
        # usato (e magari ancora negli appunti o in un client) verrebbe riassegnato.
        self._next_clip_id = max(self._next_clip_id,
                                 int(snapshot.get('next_clip_id', self._next_clip_id)))
        if 'track_names' in snapshot:
            self.track_names = snapshot['track_names'].copy()
        if 'track_colors' in snapshot:
            self.track_colors = snapshot['track_colors'].copy()
        if 'transport' in snapshot:
            self.bpm, self.beats_per_measure = snapshot['transport']
            self.beat_duration = 60.0 / self.bpm
        self.revision += 1
        self._content_changed()
        self._density_dirty = True
        self._emit_state_change()

    def undo(self):
        """Annulla l'ultima operazione distruttiva."""
        with self._edit():
            if self.is_recording or self.is_playing:
                return False
            if not self._undo_stack:
                return False
            snap = self._undo_stack.pop()
            self._redo_stack.append(self._take_snapshot('transport' in snap))
            self._restore_snapshot(snap)
        return True

    def redo(self):
        """Ripristina l'ultima operazione annullata."""
        with self._edit():
            if self.is_recording or self.is_playing:
                return False
            if not self._redo_stack:
                return False
            snap = self._redo_stack.pop()
            self._undo_stack.append(self._take_snapshot('transport' in snap))
            self._restore_snapshot(snap)
        return True

    # ──────────────────────────────────────────────────────────────────
    # MONTAGGIO (ui-studio, sez. 6.3-6.5, D3/D4): appunti, taglia/incolla,
    # operazioni clip per ID, duplica. Regole comuni:
    #   - selezione sempre esplicita (traccia + intervallo o ID clip);
    #   - controllo trasporto + revisione + validazione, POI allocazione ID,
    #     POI un solo _push_undo, POI mutazione, tutto in un solo _edit();
    #   - mai mutare in place la lista tracks[ch] (il playback la itera senza
    #     lock): liste nuove + _set_track/_set_sorted;
    #   - controlli sui byte grezzi (niente mido.Message per evento: CPU Pi);
    #   - O(n) sugli eventi della SOLA traccia interessata.
    # Fallimento = None con last_error {code, message, details, http}.
    # ──────────────────────────────────────────────────────────────────

    def _nope(self, code, message, details=None, http=None):
        self._fail(code, message, details, http)
        return None

    def _channel_ok(self, channel, field='channel'):
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 15:
            self._fail('BAD_REQUEST', 'Traccia non valida (0-15)', {'field': field})
            return False
        return True

    def _refuse_transport(self, http=None):
        if self.is_playing or self.is_recording:
            self._fail('TRANSPORT_ACTIVE', 'Ferma Play/Record prima di modificare',
                       {'is_playing': self.is_playing, 'is_recording': self.is_recording}, http)
            return True
        return False

    def _num_ok(self, value, field, lo=0.0, hi=None):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = float('nan')
        if isinstance(value, bool) or not math.isfinite(v) or v < lo or (hi is not None and v > hi):
            limit = f'{lo}..{hi}' if hi is not None else f'>= {lo}'
            self._fail('BAD_REQUEST', f'{field}: numero {limit} atteso', {'field': field})
            return None
        return v

    def _range_ok(self, start_beat, end_beat):
        start = self._num_ok(start_beat, 'start_beat', 0.0, project_store.MAX_BEAT)
        if start is None:
            return None
        end = self._num_ok(end_beat, 'end_beat', 0.0)
        if end is None:
            return None
        if end <= start:
            self._fail('BAD_REQUEST', 'end_beat deve essere > start_beat', {'field': 'end_beat'})
            return None
        return start, end

    def _get_clip(self, channel, clip_id):
        try:
            cid = int(clip_id)
        except (TypeError, ValueError):
            cid = -1
        clip = next((c for c in self.clips[channel] if int(c['id']) == cid), None)
        if clip is None:
            self._fail('NOT_FOUND', 'Clip non trovato',
                       {'kind': 'clip', 'channel': channel, 'clip_id': clip_id})
        return clip

    def _clip_indices(self, channel, cid):
        """Indici (crescenti) degli eventi posseduti dal clip `cid` (D3)."""
        return [i for i, c in enumerate(self._ids(channel)) if c == cid]

    @staticmethod
    def _clip_name(name, suffix=''):
        return (str(name or 'Clip') + suffix)[:64]   # limite dello schema (5.1)

    @staticmethod
    def _payload(events, indices, origin, length):
        """Appunti da un insieme di indici: [(beat relativo, bytes)] ordinati.
        Sustain (6.3): se l'ultimo CC64 posseduto e' >= 64 si aggiunge un
        CC64=0 a `length`, cosi' l'incolla non lascia il pedale premuto.
        Le liste di byte sono condivise (immutabili per contratto, 5.2)."""
        out = [(max(0.0, events[i][0] - origin), events[i][1]) for i in indices]
        last = None
        for i in indices:
            d = events[i][1]
            if (d[0] & 0xF0) == 0xB0 and len(d) > 2 and d[1] == 64:
                last = d
        released = last is not None and last[2] >= 64
        if released:
            out.append((float(length), [0xB0 | (last[0] & 0x0F), 64, 0]))
            out.sort(key=lambda e: e[0])          # stabile: un solo evento fuori posto
        return out, released

    def _copy_locked(self, channel, start=None, end=None, clip=None):
        """Insieme copiato + appunti (sotto lock). Ritorna (indici, payload, meta)."""
        events = self.tracks[channel]
        if clip is not None:
            # In Record la coda (non ordinata) del take ha ID 0: gli eventi del
            # clip stanno tutti nella parte gia' ordinata della traccia.
            cid = int(clip['id'])
            indices = self._clip_indices(channel, cid)
            origin, length = float(clip['start_beat']), float(clip['end_beat']) - float(clip['start_beat'])
            payload, released = self._payload(events, indices, origin, length)
            meta = {'kind': 'clip', 'length_beats': length, 'source_track': channel,
                    'source_clip_id': cid, 'name': clip.get('name') or 'Clip',
                    'sustain_release': released}
        else:
            ids = self._ids(channel)
            if self.is_recording and channel in self._rec_tracks:
                # Coda del take non ancora ordinata: la copia (sempre consentita)
                # lavora su una copia locale ordinata. Taglia e' vietata in
                # Record, quindi questi indici non vengono mai usati per rimuovere.
                events, ids = self._sorted_pair(events, ids)
            indices = sorted(self._owned_by_id(events, ids, start, end)) if events else []
            payload, released = self._payload(events, indices, start, end - start)
            meta = {'kind': 'range', 'length_beats': end - start, 'source_track': channel,
                    'source_clip_id': None, 'name': None, 'sustain_release': released}
        return indices, payload, meta

    def _set_clipboard(self, payload, meta):
        self._clipboard = payload
        self._clipboard_channel = meta['source_track']
        self._clipboard_meta = meta

    def clipboard_info(self):
        """Stato pubblico degli appunti (uno solo, condiviso, mai salvato)."""
        meta = getattr(self, '_clipboard_meta', None)
        if meta is None or (meta['kind'] == 'range' and not self._clipboard):
            return None
        info = {k: meta[k] for k in ('kind', 'length_beats', 'source_track', 'name', 'sustain_release')}
        info['events'] = len(self._clipboard)
        return info

    def copy_selection(self, channel, start_beat=None, end_beat=None, clip_id=None):
        """Copia (sez. 6.4): intervallo {channel, start_beat, end_beat} oppure
        clip {channel, clip_id}. Coppie complete + sustain (6.3). Non modifica
        il progetto (ne' revisione ne' dirty), consentita anche in Play/Record.
        Ritorna il numero di eventi copiati (senza il CC64=0 aggiunto) o None."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        rng = None
        if clip_id is None:
            rng = self._range_ok(start_beat, end_beat)
            if rng is None:
                return None
        with self._edit_lock:
            clip = None
            if clip_id is not None:
                clip = self._get_clip(channel, clip_id)
                if clip is None:
                    return None
            indices, payload, meta = self._copy_locked(channel, *(rng or (None, None)), clip=clip)
            self._set_clipboard(payload, meta)
        self._emit_state_change()                 # altri client: stato appunti
        return len(indices)

    def cut(self, channel, start_beat=None, end_beat=None, clip_id=None, expected_revision=None):
        """Taglia (sez. 6.4): copia + rimozione dello STESSO insieme di indici,
        atomica, un solo undo. Intervallo: clip interamente contenuti rimossi,
        parzialmente sovrapposti restano. Clip (estensione): copia del clip +
        sua eliminazione. Il CC64=0 di rilascio va solo negli appunti.
        Ritorna {copied, removed, clips_removed, clipboard} o None."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        rng = None
        if clip_id is None:
            rng = self._range_ok(start_beat, end_beat)
            if rng is None:
                return None
        with self._edit():
            if self._refuse_transport() or not self.check_revision(expected_revision):
                return None
            clip = None
            if clip_id is not None:
                clip = self._get_clip(channel, clip_id)
                if clip is None:
                    return None
            indices, payload, meta = self._copy_locked(channel, *(rng or (None, None)), clip=clip)
            self._set_clipboard(payload, meta)
            if clip is not None:
                res = self._delete_clip_locked(channel, clip, indices)
            else:
                res = self._remove_range_locked(channel, rng[0], rng[1], set(indices))
            res = {'copied': len(indices), 'removed': res['removed'],
                   'clips_removed': res['clips_removed'], 'clipboard': self.clipboard_info()}
            if not res['removed'] and not res['clips_removed']:
                self._emit_state_change()         # solo appunti cambiati
        return res

    def _payload_ids(self, channel, payload, target):
        """ID clip per un incolla da intervallo: regola di add_note (6.3), il
        clip piu' recente che contiene il beat del note-on; il note-off prende
        SEMPRE l'ID del suo note-on (coppia FIFO dentro gli appunti)."""
        ids = [0] * len(payload)
        partner = {}
        for _n, _v, _ob, _fb, on_l, off_l in project_store.pair_notes(payload):
            if off_l >= 0:
                partner[off_l] = on_l
        for l, (rel, d) in enumerate(payload):
            if l in partner:
                continue
            ids[l] = self._clip_at(channel, target + rel)
        for off_l, on_l in partner.items():
            ids[off_l] = ids[on_l]
        return ids

    def _insert_locked(self, channel, payload, target, ids):
        """Inserisce gli eventi degli appunti a `target` (nibble di canale
        normalizzato sulla traccia di destinazione: nuova lista di byte SOLO
        se diverso). Un sort su due sequenze gia' ordinate (timsort: ~O(n))."""
        events = list(self.tracks[channel])
        new_ids = array('H', self._ids(channel))
        for (rel, d), cid in zip(payload, ids):
            if (d[0] & 0x0F) != channel:
                d = [(d[0] & 0xF0) | channel] + list(d[1:])
            events.append((target + rel, d))
            new_ids.append(cid)
        self._set_sorted(channel, events, new_ids)
        self._density_dirty = True

    def _clip_span(self, payload, length, released):
        """Ingombro di un incolla di clip: cornice (+ margine se c'e' il CC64=0
        di rilascio, come la fine Record: la cornice deve contenerlo) e beat
        massimo degli eventi (note-off oltre la cornice compresi)."""
        frame = float(length) + (project_store.CLIP_TAIL_BEATS if released else 0.0)
        last = max((rel for rel, _d in payload), default=0.0)
        return frame, max(frame, last)

    def paste_at(self, channel, target_beat, expected_revision=None):
        """Incolla (sez. 6.4) a traccia + beat dichiarati. Appunti da intervallo:
        eventi inseriti, nessun clip creato (ID clip con la regola di add_note).
        Appunti da clip: clip nuovo (nuovo ID, stessa durata, nome + " (copia)")
        proprietario di tutti gli eventi incollati; sovrapposizioni consentite.
        Appunti vuoti -> EMPTY_CLIPBOARD (A27). Vietato con Play/Record.
        Ritorna {pasted, new_clip_id} o None."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        target = self._num_ok(target_beat, 'beat', 0.0, project_store.MAX_BEAT)
        if target is None:
            return None
        with self._edit():
            if self._refuse_transport() or not self.check_revision(expected_revision):
                return None
            meta = getattr(self, '_clipboard_meta', None)
            payload = list(self._clipboard)
            is_clip = meta is not None and meta['kind'] == 'clip'
            if not payload and not is_clip:
                return self._nope('EMPTY_CLIPBOARD', 'Appunti vuoti: copia o taglia prima di incollare')
            if is_clip:
                frame, span = self._clip_span(payload, meta['length_beats'], meta['sustain_release'])
            else:
                frame, span = 0.0, max(rel for rel, _d in payload)
            if target + span > project_store.MAX_BEAT:
                return self._nope('BAD_REQUEST', f'Incolla oltre il beat {project_store.MAX_BEAT}',
                                  {'field': 'beat'})
            if self._max_events_error(len(payload), 422):
                return None
            new_id = self._alloc_clip_id() if is_clip else None      # PRIMA dell'undo
            self._push_undo()
            if is_clip:
                ids = [new_id] * len(payload)
                self.clips[channel].append({
                    'id': new_id, 'start_beat': target, 'end_beat': target + frame,
                    'name': self._clip_name(meta['name'], ' (copia)'), 'created_at': time.time()})
            else:
                ids = self._payload_ids(channel, payload, target)
            self._insert_locked(channel, payload, target, ids)
            self._emit_state_change()
        return {'pasted': len(payload), 'new_clip_id': new_id}

    def duplicate_track(self, src_channel, dest_channel, include_config=None,
                        confirm_overwrite=None, expected_revision=None):
        """Duplica traccia (sez. 6.4, A17).

        Variante legacy (include_config e confirm_overwrite ENTRAMBI assenti,
        index.html attuale): copia solo gli eventi, liberi, e sostituisce la
        destinazione senza conferma (comportamento storico, D13).
        Variante nuova: copia eventi E clip (nuovi ID, eventi rimappati);
        destinazione con eventi o clip -> DEST_NOT_EMPTY salvo
        confirm_overwrite. `include_config` (copia della parte) e' applicato
        dalla route (router), non qui. Ritorna {dest_channel, ...} o None."""
        self.last_error = None
        legacy = include_config is None and confirm_overwrite is None
        if not self._channel_ok(src_channel) or not self._channel_ok(dest_channel, 'dest_channel'):
            return None
        if src_channel == dest_channel:
            return self._nope('BAD_REQUEST', 'Sorgente e destinazione coincidono', {'field': 'dest_channel'})
        with self._edit():
            # Override legacy 8.1: trasporto attivo -> 400 sulla variante storica.
            if self._refuse_transport(http=400 if legacy else None):
                return None
            if not self.check_revision(expected_revision):
                return None
            src_events = self.tracks[src_channel]
            src_clips = self.clips[src_channel]
            if not src_events and (legacy or not src_clips):
                return self._nope('BAD_REQUEST', 'Traccia sorgente vuota', {'field': 'channel'})
            dest_has = bool(self.tracks[dest_channel]) or (not legacy and bool(self.clips[dest_channel]))
            if dest_has and not legacy and not confirm_overwrite:
                return self._nope('DEST_NOT_EMPTY', 'La traccia di destinazione contiene dati',
                                  {'dest_channel': dest_channel, 'events': len(self.tracks[dest_channel]),
                                   'clips': len(self.clips[dest_channel])})
            mapping = {}
            if not legacy:
                for c in sorted(src_clips, key=lambda c: int(c['id'])):
                    mapping[int(c['id'])] = self._alloc_clip_id()   # PRIMA dell'undo
            was_empty = not self.tracks[dest_channel]
            self._push_undo()
            src = list(src_events)
            if legacy:
                self._set_track(dest_channel, src, _ids_zero(len(src)))
            else:
                ids = array('H', [mapping.get(c, 0) for c in self._ids(src_channel)])
                self._set_track(dest_channel, src, ids)
                now = time.time()
                self.clips[dest_channel] = [
                    {'id': mapping[int(c['id'])], 'start_beat': c['start_beat'],
                     'end_beat': c['end_beat'], 'name': c.get('name', ''), 'created_at': now}
                    for c in sorted(src_clips, key=lambda c: int(c['id']))]
            if was_empty:
                self.track_names[dest_channel] = ('Copy of ' + self.track_names.get(
                    src_channel, f'Track {src_channel + 1}'))[:32]
            self._density_dirty = True
            self._emit_state_change()
        return {'dest_channel': dest_channel, 'events': len(src), 'replaced': dest_has,
                'clip_map': {str(k): v for k, v in mapping.items()}, 'legacy': legacy}

    def rename_track(self, channel, name):
        """Rename a track. Name is trimmed to 32 chars. Un livello di undo (A14)."""
        if channel < 0 or channel > 15:
            return False
        new = str(name)[:32]
        if not new:
            return self._fail('BAD_REQUEST', 'Nome obbligatorio', {'field': 'name'})
        with self._edit():
            if self.track_names.get(channel) != new:
                self._push_undo()
                self.track_names[channel] = new
            self._emit_state_change()
        return True

    def set_track_color(self, channel, color_index):
        """Set track color by palette index (0-15). Un livello di undo (A14)."""
        if channel < 0 or channel > 15:
            return False
        color = max(0, min(15, int(color_index)))
        with self._edit():
            if self.track_colors.get(channel) != color:
                self._push_undo()
                self.track_colors[channel] = color
            self._emit_state_change()
        return True

    # ──────────────────────────────────────────────────────────────────
    # QUANTIZZAZIONE POST-REGISTRAZIONE
    # Algoritmo leggero, ideale per Raspberry Pi Zero 2W
    # ──────────────────────────────────────────────────────────────────

    def _quantize_beat_pos(self, beat, grid, swing, strength):
        """
        Calcola la posizione quantizzata di un singolo evento in beat.

        Args:
            beat:     posizione originale in beat
            grid:     dimensione della griglia in beat
                      (1.0=1/4, 0.5=1/8, 0.25=1/16, 0.125=1/32, 0.0625=1/64)
            swing:    0.0-1.0 — 0.5 = straight (no swing);
                      >0.5 ritarda le suddivisioni dispari (shuffle);
                      <0.5 le anticipa
            strength: 0.0-1.0 — quanto applicare lo snap (1.0 = allineamento perfetto)

        Returns:
            float: nuova posizione in beat (>= 0)
        """
        if grid <= 0:
            return beat
        n_round = round(beat / grid)
        target = n_round * grid
        # Swing: sposta solo le suddivisioni dispari
        if n_round % 2 == 1:
            target += (swing - 0.5) * grid   # 0.5 → offset 0 (straight)
        # Lerp tra posizione originale e target in base a strength
        return max(0.0, beat + strength * (target - beat))

    def quantize_tracks(self, channels, grid_beats, strength=1.0, swing=0.5):
        """
        Applica quantizzazione post-registrazione a uno o più canali MIDI.

        Algoritmo:
          - Accoppia ogni note_on con il suo note_off (regola FIFO unica, 6.2/A26)
            per conservare la durata; le note aperte non vengono spostate.
          - Quantizza il note_on; note_off = note_on_quantizzato + durata originale.
          - Riordina gli eventi (sort permutato insieme a event_clips).

        Returns:
            dict {channel: n_note_quantizzate} oppure None se in play/rec.
        """
        if self.is_recording or self.is_playing:
            return None

        with self._edit():
            if not channels:
                channels = [ch for ch in range(16) if self.has_data[ch]]
            results = {}
            pending = {}
            for ch in channels:
                if not self.has_data[ch] or not self.tracks[ch]:
                    continue
                events = self.tracks[ch]
                new_beats = {}
                modified = 0
                for _n, _v, on_beat, off_beat, on_idx, off_idx in self._pair_notes(events):
                    if off_idx < 0:
                        continue
                    q_on = self._quantize_beat_pos(on_beat, grid_beats, swing, strength)
                    q_off = q_on + max(0.0, off_beat - on_beat)
                    if q_on == on_beat and q_off == off_beat:
                        continue                # gia' in griglia: nessuna modifica
                    new_beats[on_idx] = q_on
                    new_beats[off_idx] = q_off
                    modified += 1
                if new_beats:
                    pending[ch] = [(new_beats.get(i, beat), msg_bytes)
                                   for i, (beat, msg_bytes) in enumerate(events)]
                results[ch] = modified
                print(f"[DAW] quantize ch={ch}: {modified} note, "
                      f"grid={grid_beats}, str={strength:.2f}, swing={swing:.2f}")
            # Calcolo completo PRIMA dell'undo: nessun livello vuoto (A21).
            if pending:
                self._push_undo()
                for ch, new_events in pending.items():
                    self._set_sorted(ch, new_events, self._ids(ch))
                self._density_dirty = True
                self._emit_state_change()
        return results

    @staticmethod
    def _pair_notes(events):
        """Funzione UNICA di accoppiamento note-on/off (sez. 6.2): FIFO per
        altezza, sui byte grezzi. [(note, vel, on_beat, off_beat, on_idx, off_idx)],
        off_idx = -1 (off_beat None) per le note aperte. API per ui-studio."""
        return project_store.pair_notes(events or [])

    def _get_note_pair_indices_in_range(self, channel, start_beat, end_beat):
        """Return set of event indices for notes whose note_on starts in [start_beat, end_beat).

        A note 'belongs' to a range if its note_on is in the range.
        Both note_on AND note_off indices are included so note pairs stay intact.
        Non-note events (CC, pitch bend, etc.) in the range are also included.
        Regola unica di sez. 6.3 (project_store.owned_indices, accoppiamento FIFO).
        """
        events = self.tracks.get(channel, [])
        if not events:
            return set()
        return self._owned_by_id(events, self._ids(channel), start_beat, end_beat)

    @staticmethod
    def _group_partners(events, ids):
        """{indice note-on: indice note-off} con FIFO per altezza DENTRO ogni ID
        clip (0 = liberi); -1 per le note aperte."""
        partner = {}
        open_notes = {}
        for idx, ev in enumerate(events):
            data = ev[1]
            kind = data[0] & 0xF0
            if kind == 0x90 and len(data) > 2 and data[2] > 0:
                open_notes.setdefault((ids[idx], data[1]), []).append(idx)
                partner[idx] = -1
            elif len(data) > 2 and (kind == 0x80 or kind == 0x90):
                pending = open_notes.get((ids[idx], data[1]))
                if pending:
                    partner[pending.pop(0)] = idx
        return partner

    @staticmethod
    def _owned_by_id(events, ids, start, end):
        """Regola 6.3 "intervalli" con D3 (ui-studio): il note-off che viaggia con
        un note-on e' il suo partner FIFO DENTRO lo stesso ID clip (0 = liberi).
        Coincide con project_store.owned_indices quando il partner FIFO globale
        ha lo stesso ID (caso normale). Con note della stessa altezza sovrapposte
        in clip diversi il partner globale puo' avere un altro ID: rimuoverlo
        sbilancerebbe entrambi i clip e una successiva eliminazione di clip
        lascerebbe una nota sospesa. Cosi' ogni ID resta sempre bilanciato."""
        owned = set()
        open_notes = {}
        for idx, ev in enumerate(events):
            beat, data = ev[0], ev[1]
            kind = data[0] & 0xF0
            if kind == 0x90 and len(data) > 2 and data[2] > 0:
                open_notes.setdefault((ids[idx], data[1]), []).append(idx)
                if start <= beat < end:
                    owned.add(idx)
            elif len(data) > 2 and (kind == 0x80 or kind == 0x90):
                pending = open_notes.get((ids[idx], data[1]))
                if pending and pending.pop(0) in owned:
                    owned.add(idx)
            elif start <= beat < end:
                owned.add(idx)
        return owned

    def transpose_notes(self, channel, semitones, start_beat=None, end_beat=None, expected_revision=None):
        """Transpose note events on a channel by semitones.

        If start_beat/end_beat are provided, transpose only notes whose note_on
        is within [start_beat, end_beat). Both note_on and note_off are transposed
        to keep note pairs consistent. Con `expected_revision` obsoleta: False
        (last_error REVISION_CONFLICT), nessuna modifica.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return 0

        st = float(start_beat) if start_beat is not None else None
        en = float(end_beat) if end_beat is not None else None
        semitones = int(semitones)

        with self._edit():
            if self.is_recording or self.is_playing:
                return None
            if not self.check_revision(expected_revision):
                return False
            events = self.tracks.get(channel)
            if not events:
                return 0
            if st is not None and en is not None and en <= st:
                return 0
            if semitones == 0:
                return 0

            # When a range is given, use note-pair-aware index set
            if st is not None and en is not None:
                pair_indices = self._get_note_pair_indices_in_range(channel, st, en)
            else:
                pair_indices = None  # transpose all

            updated = list(events)
            modified = 0
            for idx, (beat, data) in enumerate(events):
                if pair_indices is not None and idx not in pair_indices:
                    continue
                if (data[0] & 0xF0) in (0x80, 0x90) and len(data) > 2:
                    new_note = max(0, min(127, int(data[1]) + semitones))
                    if new_note != data[1]:
                        updated[idx] = (beat, [data[0], new_note, data[2]])
                        modified += 1

            if modified <= 0:
                return 0

            self._push_undo()
            # Ordine invariato: event_clips resta valido cosi' com'e'.
            self._set_track(channel, updated, self._ids(channel))
            self._density_dirty = True
            self._emit_state_change()
        print(f"[DAW] transpose ch={channel} semitones={semitones} modified={modified} range=({st},{en})")
        return modified

    def get_clips(self, channel):
        """Return clips for a channel sorted by start beat."""
        if channel < 0 or channel > 15:
            return []
        with self._edit_lock:
            return sorted([dict(c) for c in self.clips[channel]], key=lambda c: c['start_beat'])

    def get_clips_with_revision(self, channel):
        with self._edit_lock:
            return self.get_clips(channel), self.revision

    # Operazioni clip per ID (D3, sez. 6.3/6.4, A18): agiscono SOLO sugli
    # eventi con event_clips == ID, anche con altri clip sovrapposti. Ogni ID
    # e' bilanciato (stessi note-on/off per altezza, on prima di off): unire
    # o spostare in blocco sequenze bilanciate non crea mai note-off orfani.

    def _clip_op_start(self, channel, clip_id, expected_revision):
        """Prologo comune (sotto _edit): trasporto, revisione, clip esistente."""
        if self._refuse_transport() or not self.check_revision(expected_revision):
            return None
        return self._get_clip(channel, clip_id)

    def move_clip(self, channel, clip_id, target_start_beat, expected_revision=None):
        """Sposta (6.4): delta su tutti gli eventi con l'ID del clip; ID e durata
        invariati; eventi liberi o di altri clip nella zona di arrivo NON
        cambiano proprietario. start < 0 o oltre MAX_BEAT -> BAD_REQUEST."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        target = self._num_ok(target_start_beat, 'target_start_beat', 0.0, project_store.MAX_BEAT)
        if target is None:
            return None
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            cid = int(clip['id'])
            old_start, old_end = float(clip['start_beat']), float(clip['end_beat'])
            delta = target - old_start
            if delta == 0.0:
                return {'moved': 0}
            events, ids = self.tracks[channel], self._ids(channel)
            mine = [i for i, c in enumerate(ids) if c == cid]
            last = max((events[i][0] for i in mine), default=old_end)
            if max(last, old_end) + delta > project_store.MAX_BEAT:
                return self._nope('BAD_REQUEST', f'Spostamento oltre il beat {project_store.MAX_BEAT}',
                                  {'field': 'target_start_beat'})
            self._push_undo()
            if mine:
                mine_set = set(mine)
                rest = [i for i in range(len(events)) if i not in mine_set]
                # Due sequenze gia' ordinate concatenate: il sort (timsort) le
                # fonde in ~O(n). A parita' di beat gli eventi fermi vengono prima.
                new_events = [events[i] for i in rest] + \
                             [(max(0.0, events[i][0] + delta), events[i][1]) for i in mine]
                new_ids = array('H', [ids[i] for i in rest]) + array('H', [cid]) * len(mine)
                self._set_sorted(channel, new_events, new_ids)
            clip['start_beat'] = target
            clip['end_beat'] = old_end + delta
            self._density_dirty = True
            self._emit_state_change()
        return {'moved': len(mine)}

    def _delete_clip_locked(self, channel, clip, indices=None):
        cid = int(clip['id'])
        if indices is None:
            indices = self._clip_indices(channel, cid)
        self._push_undo()
        if indices:
            self._drop_indices(channel, set(indices))
        self.clips[channel] = [c for c in self.clips[channel] if int(c['id']) != cid]
        self._density_dirty = True
        self._emit_state_change()
        return {'removed': len(indices), 'clips_removed': [cid]}

    def delete_clip(self, channel, clip_id, expected_revision=None):
        """Elimina (6.4): il clip e gli eventi con il suo ID (coppie complete:
        ogni ID e' bilanciato). Gli eventi di clip sovrapposti restano."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            return self._delete_clip_locked(channel, clip)

    def transpose_clip(self, channel, clip_id, semitones, expected_revision=None):
        """Trasponi clip (6.4): note (on e off) con l'ID del clip, limitate a
        0..127. Nessun cambio di ordine: event_clips invariato."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        try:
            semitones = int(semitones)
        except (TypeError, ValueError):
            return self._nope('BAD_REQUEST', 'semitones: intero atteso', {'field': 'semitones'})
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            cid = int(clip['id'])
            events, ids = self.tracks[channel], self._ids(channel)
            updated = None
            modified = 0
            for i, c in enumerate(ids):
                if c != cid:
                    continue
                beat, d = events[i]
                if (d[0] & 0xF0) in (0x80, 0x90) and len(d) > 2:
                    n = max(0, min(127, d[1] + semitones))
                    if n != d[1]:
                        if updated is None:
                            updated = list(events)
                        updated[i] = (beat, [d[0], n, d[2]])
                        modified += 1
            if modified:
                self._push_undo()
                self._set_track(channel, updated, ids)
                self._density_dirty = True
                self._emit_state_change()
        return {'modified': modified}

    def resize_clip(self, channel, clip_id, new_end_beat, expected_revision=None):
        """Ridimensiona la fine (6.4, D4). Estensione sempre consentita (nessun
        evento cambia proprietario). Riduzione solo se new_end > beat dell'ultimo
        evento posseduto (note-on o non-nota; i note-off possono uscire),
        altrimenti CLIP_RESIZE_WOULD_EXCLUDE con details.min_end_beat (estremo
        ESCLUSO). Minimo start + 0.125. Non cambia mai il playback."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        new_end = self._num_ok(new_end_beat, 'new_end_beat', 0.0, project_store.MAX_BEAT)
        if new_end is None:
            return None
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            cid = int(clip['id'])
            new_end = max(float(clip['start_beat']) + 0.125, new_end)
            if new_end > project_store.MAX_BEAT:
                return self._nope('BAD_REQUEST', f'Fine oltre il beat {project_store.MAX_BEAT}',
                                  {'field': 'new_end_beat'})
            if new_end == float(clip['end_beat']):
                return {'end_beat': new_end}
            if new_end < float(clip['end_beat']):
                events, ids = self.tracks[channel], self._ids(channel)
                last = None
                for i, c in enumerate(ids):
                    if c == cid and not project_store._is_note_off(events[i][1]):
                        last = events[i][0]            # traccia ordinata: l'ultimo vince
                if last is not None and new_end <= last:
                    return self._nope('CLIP_RESIZE_WOULD_EXCLUDE',
                                      'La riduzione lascerebbe fuori eventi del clip',
                                      {'clip_id': cid, 'min_end_beat': last, 'exclusive': True})
            self._push_undo()
            clip['end_beat'] = new_end
            self._density_dirty = True
            self._emit_state_change()
        print(f"[DAW] resize_clip ch={channel} id={clip_id} new_end={new_end:.3f}")
        return {'end_beat': new_end}

    def split_clip(self, channel, clip_id, split_beat, expected_revision=None):
        """Dividi (6.4): originale [start, split) stesso ID, nuovo [split, end)
        nuovo ID, nome "<nome> (split)"; margine 0.01 dai bordi. Passano al
        nuovo ID gli eventi del clip con note-on (o beat, se non-nota) >= split,
        con il loro note-off (coppie FIFO DENTRO il clip); una nota a cavallo
        resta a sinistra. Nessun evento spostato: playback invariato."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        split_at = self._num_ok(split_beat, 'split_beat', 0.0, project_store.MAX_BEAT)
        if split_at is None:
            return None
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            old_start, old_end = float(clip['start_beat']), float(clip['end_beat'])
            if split_at <= old_start + 0.01 or split_at >= old_end - 0.01:
                # Override legacy 8.1: stesso 404 di "clip non trovato", codice BAD_REQUEST.
                return self._nope('BAD_REQUEST', 'Punto di divisione fuori dal clip',
                                  {'field': 'split_beat'}, http=404)
            cid = int(clip['id'])
            events, ids = self.tracks[channel], self._ids(channel)
            mine = [i for i, c in enumerate(ids) if c == cid]
            sub = [events[i] for i in mine]
            right = set()
            paired_off = set()
            for _n, _v, on_b, _fb, on_l, off_l in project_store.pair_notes(sub):
                if off_l >= 0:
                    paired_off.add(off_l)
                if on_b >= split_at:
                    right.add(on_l)
                    if off_l >= 0:
                        right.add(off_l)
            for l, (beat, d) in enumerate(sub):
                if l in paired_off or project_store._is_note_on(d):
                    continue
                if beat >= split_at:                    # non-nota (o note-off spaiato)
                    right.add(l)
            new_id = self._alloc_clip_id()              # PRIMA dell'undo
            self._push_undo()
            if right:
                new_ids = array('H', ids)
                for l in right:
                    new_ids[mine[l]] = new_id
                self._set_track(channel, events, new_ids)
            clip['end_beat'] = split_at
            self.clips[channel].append({
                'id': new_id, 'start_beat': split_at, 'end_beat': old_end,
                'name': self._clip_name(clip.get('name'), ' (split)'), 'created_at': time.time()})
            self._density_dirty = True
            self._emit_state_change()
        print(f"[DAW] split_clip ch={channel} id={clip_id} at={split_at:.3f} -> new_id={new_id}")
        return {'new_clip_id': new_id, 'moved_events': len(right)}

    def merge_clips(self, channel, clip_ids, expected_revision=None):
        """Unisci (6.4): stessa traccia, >= 2 ID distinti; risultato [min start,
        max end) con NUOVO ID, nome "Merged <id>"; gli eventi dei clip uniti
        passano al nuovo ID, quelli di altri clip no. Un ID assente -> NOT_FOUND
        (nessuna unione parziale silenziosa)."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        try:
            wanted = sorted(set(int(c) for c in clip_ids))
        except (TypeError, ValueError):
            return self._nope('BAD_REQUEST', 'clip_ids: lista di interi attesa', {'field': 'clip_ids'})
        if len(wanted) < 2:
            return self._nope('BAD_REQUEST', 'Servono almeno 2 clip distinti', {'field': 'clip_ids'})
        with self._edit():
            if self._refuse_transport() or not self.check_revision(expected_revision):
                return None
            present = {int(c['id']): c for c in self.clips[channel]}
            missing = [c for c in wanted if c not in present]
            if missing:
                return self._nope('NOT_FOUND', 'Clip non trovati sulla traccia',
                                  {'kind': 'clip', 'channel': channel, 'clip_ids': missing})
            matching = [present[c] for c in wanted]
            min_start = min(float(c['start_beat']) for c in matching)
            max_end = max(float(c['end_beat']) for c in matching)
            new_id = self._alloc_clip_id()              # PRIMA dell'undo
            self._push_undo()
            id_set = set(wanted)
            self.clips[channel] = [c for c in self.clips[channel] if int(c['id']) not in id_set]
            self.clips[channel].append({
                'id': new_id, 'start_beat': min_start, 'end_beat': max_end,
                'name': f'Merged {new_id}', 'created_at': time.time()})
            ids = self._ids(channel)
            if any(c in id_set for c in ids):
                new_ids = array('H', [new_id if c in id_set else c for c in ids])
                self._set_track(channel, self.tracks[channel], new_ids)
            self._density_dirty = True
            self._emit_state_change()
        print(f"[DAW] merge_clips ch={channel} ids={wanted} -> new_id={new_id} range=[{min_start:.3f}, {max_end:.3f}]")
        return {'new_clip_id': new_id, 'start_beat': min_start, 'end_beat': max_end}

    def create_clip(self, channel, start_beat, end_beat, expected_revision=None, name=None):
        """Crea clip da intervallo (6.4, nuova): [start, end) qualsiasi; prende
        gli eventi LIBERI dell'intervallo (regola 6.3), mai quelli di altri clip.
        Una nota passa al clip solo come coppia (note-on e note-off entrambi
        liberi, accoppiati FIFO tra gli eventi liberi). Ritorna {clip, events}."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        rng = self._range_ok(start_beat, end_beat)
        if rng is None:
            return None
        start, end = rng
        if end > project_store.MAX_BEAT:
            return self._nope('BAD_REQUEST', f'end_beat oltre {project_store.MAX_BEAT}', {'field': 'end_beat'})
        with self._edit():
            if self._refuse_transport() or not self.check_revision(expected_revision):
                return None
            events, ids = self.tracks[channel], self._ids(channel)
            free = [i for i, c in enumerate(ids) if c == 0]
            sub = [events[i] for i in free]
            take = set()
            paired_off = set()
            for _n, _v, on_b, _fb, on_l, off_l in project_store.pair_notes(sub):
                if off_l >= 0:
                    paired_off.add(off_l)
                if start <= on_b < end:
                    take.add(on_l)
                    if off_l >= 0:
                        take.add(off_l)
            for l, (beat, d) in enumerate(sub):
                if l in paired_off or project_store._is_note_on(d) or project_store._is_note_off(d):
                    continue                           # note-off spaiati restano liberi
                if start <= beat < end:
                    take.add(l)
            new_id = self._alloc_clip_id()              # PRIMA dell'undo
            self._push_undo()
            if take:
                new_ids = array('H', ids)
                for l in take:
                    new_ids[free[l]] = new_id
                self._set_track(channel, events, new_ids)
            clip = {'id': new_id, 'start_beat': start, 'end_beat': end,
                    'name': self._clip_name(name or f'Clip {new_id}'), 'created_at': time.time()}
            self.clips[channel].append(clip)
            self._density_dirty = True
            self._emit_state_change()
        return {'clip': dict(clip), 'events': len(take)}

    def duplicate_clip(self, channel, clip_id, expected_revision=None):
        """Duplica clip (6.4, nuova): copia degli eventi del clip incollata a
        end_beat dello stesso clip sulla stessa traccia, nuovo ID, nome +
        " (copia)"; stessa regola sustain della copia. NON tocca gli appunti.
        Un solo undo. Ritorna {new_clip_id, clip}."""
        self.last_error = None
        if not self._channel_ok(channel):
            return None
        with self._edit():
            clip = self._clip_op_start(channel, clip_id, expected_revision)
            if clip is None:
                return None
            _idx, payload, meta = self._copy_locked(channel, clip=clip)
            target = float(clip['end_beat'])
            frame, span = self._clip_span(payload, meta['length_beats'], meta['sustain_release'])
            if target + span > project_store.MAX_BEAT:
                return self._nope('BAD_REQUEST', f'Duplicato oltre il beat {project_store.MAX_BEAT}',
                                  {'field': 'clip_id'})
            new_id = self._alloc_clip_id()              # PRIMA dell'undo
            self._push_undo()
            new_clip = {'id': new_id, 'start_beat': target, 'end_beat': target + frame,
                        'name': self._clip_name(meta['name'], ' (copia)'), 'created_at': time.time()}
            self.clips[channel].append(new_clip)
            self._insert_locked(channel, payload, target, [new_id] * len(payload))
            self._emit_state_change()
        return {'new_clip_id': new_id, 'clip': dict(new_clip)}

    def set_bpm(self, bpm):
        """
        Imposta il tempo (BPM)
        Gli eventi sono salvati in beat, quindi cambiano velocità automaticamente.
        Emette daw_state_update (A20) e rende il progetto modificato.
        """
        if self.is_recording or self.is_playing:
            return False

        self.bpm = max(30, min(300, bpm))
        self.beat_duration = 60.0 / self.bpm
        print(f"[DAW] BPM impostato a {self.bpm}")
        self._emit_state_change()
        self._content_changed()
        return True

    def set_time_signature(self, beats_per_measure):
        """Imposta il numeratore della misura (es. 3 o 4). Emette (A20), dirty."""
        if beats_per_measure not in (3, 4):
            return False
        self.beats_per_measure = beats_per_measure
        print(f"[DAW] Time signature impostato a {beats_per_measure}/4")
        self._emit_state_change()
        self._content_changed()
        return True

    def get_events_decoded_with_revision(self, channel=-1):
        return self._events_decoded(channel)

    def get_state(self):
        """Ritorna lo stato completo del sistema (con `revision`, sez. 6.6).
        Letto sotto il lock di editing: stato coerente con la revisione."""
        with self._edit_lock:
            return self._get_state_locked()

    def _get_state_locked(self):
        # Calcola la durata di ogni traccia (converti beat in secondi per visualizzazione)
        track_durations = {}
        max_duration = 0.0
        
        for ch in range(16):
            if self.tracks[ch]:
                # Ultimo beat della traccia convertito in secondi
                last_beat = self.tracks[ch][-1][0]
                duration = last_beat * self.beat_duration
                track_durations[ch] = duration
                max_duration = max(max_duration, duration)
            else:
                track_durations[ch] = 0.0
        
        return {
            'is_recording': self.is_recording,
            'is_playing': self.is_playing,
            'timeline_position': self.timeline_position,
            'bpm': self.bpm,
            'metronome_enabled': self.metronome_enabled,
            'beats_per_measure': self.beats_per_measure,
            'armed': self.armed.copy(),
            'muted': self.muted.copy(),
            'solo': self.solo.copy(),
            'any_solo': self._any_solo,
            'has_data': self.has_data.copy(),
            'track_counts': {ch: len(self.tracks[ch]) for ch in range(16)},
            'track_durations': track_durations,
            'max_duration': max_duration,
            'loop_enabled': self.loop_enabled,
            'loop_start': self.loop_start,
            'loop_end': self.loop_end,
            'can_undo': len(self._undo_stack) > 0,
            'can_redo': len(self._redo_stack) > 0,
            'track_names': self.track_names.copy(),
            'track_colors': self.track_colors.copy(),
            'clips': {ch: self.get_clips(ch) for ch in range(16)},
            'revision': self.revision,
            # ui-studio: D6 (diagnostica, null fuori dal Record) e stato appunti
            # (Incolla abilitato solo con appunti non vuoti).
            'record_start_beat': self.record_start_beat,
            'clipboard': self.clipboard_info(),
        }

    # --- MIDI note name helper ---
    NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

    @staticmethod
    def _midi_note_name(note_num):
        """Convert MIDI note number to name like C4, F#3."""
        octave = (note_num // 12) - 1
        name = MultiTrackDAW.NOTE_NAMES[note_num % 12]
        return f'{name}{octave}'

    def get_events_decoded(self, channel=-1):
        """
        Returns decoded MIDI events for a channel (or all channels if channel=-1).
        Pairs Note On/Off to compute duration. Returns list of dicts.
        """
        return self._events_decoded(channel)[0]

    def _events_decoded(self, channel=-1):
        channels = range(16) if channel < 0 else [channel]
        snap, snap_ids, _bpm, rev, _gen = self._tracks_snapshot(channels)
        result = []

        for ch in channels:
            events = snap[ch]
            if not events:
                continue
            # Build Note On → Note Off mapping for duration
            ids = snap_ids[ch]
            note_on_map = {}  # {note_num: [(index_in_result, beat_pos), ...]}
            for idx_in_track, (beat_pos, msg_bytes) in enumerate(events):
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    continue
                entry = {
                    'ch': ch,
                    'idx': idx_in_track,
                    'beat': round(beat_pos, 4),
                    'type': msg.type,
                    'clip': ids[idx_in_track],       # ID clip proprietario (D3), 0 = libero
                }
                if msg.type == 'note_on' and msg.velocity > 0:
                    entry['note'] = self._midi_note_name(msg.note)
                    entry['note_num'] = msg.note
                    entry['vel'] = msg.velocity
                    entry['dur'] = None  # filled later
                    res_idx = len(result)
                    note_on_map.setdefault(msg.note, []).append((res_idx, beat_pos))
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    entry['note'] = self._midi_note_name(msg.note)
                    entry['note_num'] = msg.note
                    entry['vel'] = 0
                    # Match with earliest pending Note On for this pitch
                    if msg.note in note_on_map and note_on_map[msg.note]:
                        on_idx, on_beat = note_on_map[msg.note].pop(0)
                        result[on_idx]['dur'] = round(beat_pos - on_beat, 4)
                        if not note_on_map[msg.note]:
                            del note_on_map[msg.note]
                elif msg.type == 'control_change':
                    entry['cc_num'] = msg.control
                    entry['cc_val'] = msg.value
                elif msg.type == 'program_change':
                    entry['program'] = msg.program
                elif msg.type == 'pitchwheel':
                    entry['pitch'] = msg.pitch
                result.append(entry)
        return result, rev

    # ── Editing indicizzato (sez. 6.6): expected_revision, validazione PRIMA
    # dell'undo (A21), un solo sort permutato, emit dopo il rilascio del lock.
    # Errori in last_error (per-thread): RECORDING_ACTIVE / BAD_REQUEST con
    # HTTP 400 conservato (override legacy 8.1), REVISION_CONFLICT 409.

    def _refuse_recording(self):
        return self._fail('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                          {'is_recording': True}, http=400)

    def _bad_index(self, field='index'):
        return self._fail('BAD_REQUEST', 'Indice evento non valido', {'field': field}, http=400)

    @staticmethod
    def _beat_value(v):
        b = float(v)
        if not math.isfinite(b) or b < 0 or b > project_store.MAX_BEAT:
            raise ValueError(f'beat fuori intervallo 0..{project_store.MAX_BEAT}')
        return b

    def edit_event(self, channel, index, updates, expected_revision=None):
        """
        Edit an event's beat position and/or velocity.
        Only allowed when not recording.
        Returns True on success.
        """
        if self.is_recording:
            return self._refuse_recording()
        with self._edit():
            if self.is_recording:              # ricontrollo sotto lock (review L3)
                return self._refuse_recording()
            if not self.check_revision(expected_revision):
                return False
            events = self.tracks.get(channel)
            if not events or index < 0 or index >= len(events):
                return self._bad_index()
            beat_pos, msg_bytes = events[index]
            try:
                msg = mido.Message.from_bytes(msg_bytes)
                if 'beat' in updates:
                    beat_pos = self._beat_value(updates['beat'])
                if 'velocity' in updates and hasattr(msg, 'velocity'):
                    msg = msg.copy(velocity=max(0, min(127, int(updates['velocity']))))
                if 'note' in updates and hasattr(msg, 'note'):
                    msg = msg.copy(note=max(0, min(127, int(updates['note']))))
                if 'cc_num' in updates and hasattr(msg, 'control'):
                    msg = msg.copy(control=max(0, min(127, int(updates['cc_num']))))
                if 'cc_value' in updates and hasattr(msg, 'value'):
                    msg = msg.copy(value=max(0, min(127, int(updates['cc_value']))))
                if 'pitch' in updates and hasattr(msg, 'pitch'):
                    msg = msg.copy(pitch=max(-8192, min(8191, int(updates['pitch']))))
                if 'program' in updates and hasattr(msg, 'program'):
                    msg = msg.copy(program=max(0, min(127, int(updates['program']))))
            except (ValueError, TypeError) as e:
                return self._fail('BAD_REQUEST', f'Valore non valido: {e}', {'field': 'updates'}, http=400)

            self._push_undo()
            new_events = list(events)
            new_events[index] = (beat_pos, msg.bytes())
            if 'beat' in updates:
                self._set_sorted(channel, new_events, self._ids(channel))
            else:
                self._set_track(channel, new_events, self._ids(channel))
            self._density_dirty = True
            self._emit_state_change()
        return True

    def delete_event(self, channel, index, expected_revision=None):
        """
        Delete an event by index.
        Only allowed when not recording.
        Returns True on success.
        """
        if self.is_recording:
            return self._refuse_recording()
        with self._edit():
            if self.is_recording:              # ricontrollo sotto lock (review L3)
                return self._refuse_recording()
            if not self.check_revision(expected_revision):
                return False
            events = self.tracks.get(channel)
            if not events or index < 0 or index >= len(events):
                return self._bad_index()
            self._push_undo()
            self._drop_indices(channel, {index})
            self._density_dirty = True
            self._emit_state_change()
        return True

    # ── Atomic note-pair operations (note = note_on + note_off) ──

    def add_note(self, channel, note, velocity, start_beat, length_beats, expected_revision=None):
        """
        Add a complete note (note_on + note_off) to a track.
        Inserts both events, sorts once. Returns True on success.
        La nota riceve l'ID del clip piu' recente che contiene start_beat (6.3).
        """
        if self.is_recording:
            return self._refuse_recording()
        if channel < 0 or channel > 15:
            return self._fail('BAD_REQUEST', 'Traccia non valida', {'field': 'channel'}, http=400)
        try:
            note = max(0, min(127, int(note)))
            velocity = max(1, min(127, int(velocity)))
            start_beat = self._beat_value(max(0.0, float(start_beat)))
            length_beats = max(0.001, float(length_beats))
            end_beat = self._beat_value(start_beat + length_beats)
        except (ValueError, TypeError) as e:
            return self._fail('BAD_REQUEST', f'Valore non valido: {e}', {'field': 'start_beat'}, http=400)

        with self._edit():
            if self.is_recording:              # ricontrollo sotto lock (review L3)
                return self._refuse_recording()
            if not self.check_revision(expected_revision):
                return False
            if self._max_events_error(2, 422):
                return False
            self._push_undo()
            cid = self._clip_at(channel, start_beat)
            events = list(self.tracks[channel])
            ids = array('H', self._ids(channel))
            msg_on = mido.Message('note_on', channel=channel, note=note, velocity=velocity)
            msg_off = mido.Message('note_off', channel=channel, note=note, velocity=0)
            events.append((start_beat, msg_on.bytes()))
            events.append((end_beat, msg_off.bytes()))
            ids.append(cid)
            ids.append(cid)            # il note-off ha sempre l'ID del suo note-on
            self._set_sorted(channel, events, ids)
            self._density_dirty = True
            self._emit_state_change()
        return True

    def edit_note_pair(self, channel, on_idx, off_idx, updates, expected_revision=None):
        """
        Atomically edit both note_on and note_off events for a single note.
        updates may contain: on_beat, off_beat, note, velocity.
        Sorts the events list once at the end. Returns True on success.
        """
        if self.is_recording:
            return self._refuse_recording()
        with self._edit():
            if self.is_recording:              # ricontrollo sotto lock (review L3)
                return self._refuse_recording()
            if not self.check_revision(expected_revision):
                return False
            events = self.tracks.get(channel)
            if not events:
                return self._bad_index('on_idx')
            n_events = len(events)
            if on_idx < 0 or on_idx >= n_events:
                return self._bad_index('on_idx')
            if off_idx >= 0 and off_idx >= n_events:
                return self._bad_index('off_idx')

            # Tutto calcolato e validato prima dell'undo (A21).
            new_events = list(events)
            need_sort = False
            try:
                beat_pos, msg_bytes = events[on_idx]
                msg = mido.Message.from_bytes(msg_bytes)
                if 'on_beat' in updates:
                    beat_pos = self._beat_value(updates['on_beat'])
                    need_sort = True
                if 'note' in updates and hasattr(msg, 'note'):
                    msg = msg.copy(note=max(0, min(127, int(updates['note']))))
                if 'velocity' in updates and hasattr(msg, 'velocity'):
                    msg = msg.copy(velocity=max(0, min(127, int(updates['velocity']))))
                new_events[on_idx] = (beat_pos, msg.bytes())

                if off_idx >= 0:
                    beat_pos, msg_bytes = events[off_idx]
                    msg = mido.Message.from_bytes(msg_bytes)
                    if 'off_beat' in updates:
                        beat_pos = self._beat_value(updates['off_beat'])
                        need_sort = True
                    if 'note' in updates and hasattr(msg, 'note'):
                        msg = msg.copy(note=max(0, min(127, int(updates['note']))))
                    new_events[off_idx] = (beat_pos, msg.bytes())
            except (ValueError, TypeError) as e:
                return self._fail('BAD_REQUEST', f'Valore non valido: {e}', {'field': 'updates'}, http=400)

            self._push_undo()
            if need_sort:
                self._set_sorted(channel, new_events, self._ids(channel))
            else:
                self._set_track(channel, new_events, self._ids(channel))
            self._density_dirty = True
            self._emit_state_change()
        return True

    def delete_note_pair(self, channel, on_idx, off_idx, expected_revision=None):
        """
        Atomically delete both note_on and note_off events for a note.
        Handles index adjustment internally. Returns True on success.
        """
        if self.is_recording:
            return self._refuse_recording()
        with self._edit():
            if self.is_recording:              # ricontrollo sotto lock (review L3)
                return self._refuse_recording()
            if not self.check_revision(expected_revision):
                return False
            events = self.tracks.get(channel)
            if not events:
                return self._bad_index('on_idx')
            indices = set(i for i in [on_idx, off_idx] if 0 <= i < len(events))
            if not indices:
                return self._bad_index('on_idx')
            self._push_undo()
            self._drop_indices(channel, indices)
            self._density_dirty = True
            self._emit_state_change()
        return True

    # ── Editing batch di note (ui-studio, A28; usato da ui-editor) ─────
    # Tutto-o-niente: validazione COMPLETA prima dell'undo; un solo sort, un
    # solo livello, una sola revisione. Errori: 400 BAD_REQUEST (tipo/valore),
    # 422 VALIDATION_FAILED (indici che non sono la coppia FIFO attesa).

    _BATCH_EDIT_KEYS = frozenset(('on_idx', 'off_idx', 'on_beat', 'off_beat', 'note', 'velocity'))

    def _batch_int(self, entry, key, path, lo, hi, default=None):
        v = entry.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self._fail('BAD_REQUEST', f'{path}.{key}: intero {lo}..{hi} atteso', {'field': f'{path}.{key}'})
            return None
        return v

    def _batch_pairs(self, channel, entries, list_name, allowed):
        """Validazione comune di edits/pairs. Ritorna (events, [(entry, on, off)]) o None."""
        if not isinstance(entries, list) or not entries:
            return self._nope('BAD_REQUEST', f'{list_name}: lista non vuota attesa', {'field': list_name})
        parsed = []
        for k, entry in enumerate(entries):
            path = f'{list_name}[{k}]'
            if not isinstance(entry, dict):
                return self._nope('BAD_REQUEST', f'{path}: oggetto atteso', {'field': path})
            extra = sorted(set(entry) - allowed)
            if extra:
                return self._nope('BAD_REQUEST', f'{path}: campo sconosciuto {extra[0]}',
                                  {'field': f'{path}.{extra[0]}'})
            on = self._batch_int(entry, 'on_idx', path, 0, 10 ** 9)
            if on is None:
                return None
            off = self._batch_int(entry, 'off_idx', path, -1, 10 ** 9, default=-1)
            if off is None:
                return None
            parsed.append((entry, on, off))
        return parsed

    def _batch_check_pairs(self, events, ids, parsed, list_name):
        """Sotto lock: ogni (on_idx, off_idx) DEVE essere una coppia della regola
        FIFO unica (6.2) sugli eventi correnti; nessun indice ripetuto; on e off
        con lo stesso ID clip (D3: altrimenti modificarla/eliminarla
        sbilancerebbe due clip sovrapposti, vedi _owned_by_id)."""
        partner = {on: off for _n, _v, _ob, _fb, on, off in project_store.pair_notes(events)}
        group = self._group_partners(events, ids)
        seen = set()
        n = len(events)
        for k, (_entry, on, off) in enumerate(parsed):
            path = f'{list_name}[{k}]'
            if on >= n or not project_store._is_note_on(events[on][1]):
                return self._nope('VALIDATION_FAILED', 'on_idx non punta a un note-on',
                                  {'path': f'{path}.on_idx', 'reason': 'non e\' un note-on'})
            if partner.get(on, -1) != off:
                return self._nope('VALIDATION_FAILED', 'off_idx non e\' il note-off di questa nota',
                                  {'path': f'{path}.off_idx',
                                   'reason': f'note-off atteso {partner.get(on, -1)}'})
            if group.get(on, -1) != off:
                # Coppia FIFO globale diversa da quella dentro il clip: note della
                # stessa altezza sovrapposte in clip diversi. Modificarla
                # sbilancerebbe i clip (nota sospesa a una futura eliminazione).
                return self._nope('VALIDATION_FAILED',
                                  'Nota sovrapposta a una della stessa altezza in un altro clip: '
                                  'usa le operazioni sul clip',
                                  {'path': path, 'reason': f'clip {ids[on]} / {ids[off] if off >= 0 else None}'})
            for idx in (on, off):
                if idx >= 0 and idx in seen:
                    return self._nope('VALIDATION_FAILED', 'Indice ripetuto nel batch',
                                      {'path': path, 'reason': f'indice {idx} ripetuto'})
                seen.add(idx)
        return True

    def edit_notes_batch(self, channel, edits, expected_revision=None):
        """Modifica atomica di piu' coppie (contratto 8.2 `notes/batch` PUT).
        edits: [{on_idx, off_idx, on_beat?, off_beat?, note?, velocity?}] sugli
        indici della revisione attesa. Voci invalide -> errore e NESSUNA
        modifica (A28: prima venivano saltate in silenzio). Ammesso in Play,
        vietato in Record. Ritorna {modified} o None."""
        self.last_error = None
        if self.is_recording:
            return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                              {'is_recording': True})
        if not self._channel_ok(channel):
            return None
        parsed = self._batch_pairs(channel, edits, 'edits', self._BATCH_EDIT_KEYS)
        if parsed is None:
            return None
        values = []
        for k, (entry, _on, _off) in enumerate(parsed):
            path = f'edits[{k}]'
            v = {}
            for key in ('on_beat', 'off_beat'):
                if key in entry:
                    b = self._num_ok(entry[key], f'{path}.{key}', 0.0, project_store.MAX_BEAT)
                    if b is None:
                        return None
                    v[key] = b
            if 'note' in entry:
                v['note'] = self._batch_int(entry, 'note', path, 0, 127)
                if v['note'] is None:
                    return None
            if 'velocity' in entry:
                # 0 trasformerebbe il note-on in un note-off: nota orfana.
                v['velocity'] = self._batch_int(entry, 'velocity', path, 1, 127)
                if v['velocity'] is None:
                    return None
            values.append(v)
        with self._edit():
            if self.is_recording:
                return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                                  {'is_recording': True})
            if not self.check_revision(expected_revision):
                return None
            events = self.tracks[channel]
            if self._batch_check_pairs(events, self._ids(channel), parsed, 'edits') is None:
                return None
            new_events = None
            need_sort = False
            repair = False
            moved = []
            modified = 0
            for k, ((_entry, on, off), v) in enumerate(zip(parsed, values)):
                on_beat, on_d = events[on]
                nb_on = v.get('on_beat', on_beat)
                if off >= 0:
                    off_beat, off_d = events[off]
                    nb_off = v.get('off_beat', off_beat)
                    if ('on_beat' in v or 'off_beat' in v) and not nb_off > nb_on:
                        # Un note-off prima (o insieme) del suo note-on verrebbe
                        # riaccoppiato FIFO con un'altra nota: nota sospesa.
                        return self._nope('VALIDATION_FAILED', 'off_beat deve essere > on_beat',
                                          {'path': f'edits[{k}].off_beat', 'reason': 'off_beat <= on_beat'})
                elif 'off_beat' in v:
                    return self._nope('VALIDATION_FAILED', 'Nota senza note-off: off_beat non applicabile',
                                      {'path': f'edits[{k}].off_beat', 'reason': 'off_idx = -1'})
                note = v.get('note', on_d[1])
                vel = v.get('velocity', on_d[2])
                new_on = (nb_on, on_d if (note, vel) == (on_d[1], on_d[2]) else [on_d[0], note, vel])
                changes = [(on, new_on)]
                if off >= 0:
                    new_off = (nb_off, off_d if note == off_d[1] else [off_d[0], note, off_d[2]])
                    changes.append((off, new_off))
                touched = False
                for idx, ev in changes:
                    if ev != events[idx]:
                        if new_events is None:
                            new_events = list(events)
                        new_events[idx] = ev
                        touched = True
                if touched:
                    modified += 1
                    need_sort = need_sort or 'on_beat' in v or 'off_beat' in v
                    repair = repair or need_sort or 'note' in v
                    if 'on_beat' in v:
                        moved.append(on)
            if new_events is not None:
                # ui-editor: nessuna nota deve cambiare partner FIFO (6.2) dopo il
                # riordino o il cambio di altezza, e le cornici dei clip devono
                # contenere i note-on spostati (D28). Tutto prima dell'undo (A21).
                if repair and not self._pairs_preserved(events, new_events):
                    return self._nope('VALIDATION_FAILED',
                                      'La modifica sovrapporrebbe note della stessa altezza',
                                      {'path': 'edits', 'reason': 'pairing'})
                plan = self._fit_clips_plan(channel, new_events, self._ids(channel), moved)
                if plan is None:
                    return None
                self._push_undo()
                self._apply_clip_plan(channel, plan)
                if need_sort:
                    self._set_sorted(channel, new_events, self._ids(channel))
                else:
                    self._set_track(channel, new_events, self._ids(channel))
                self._density_dirty = True
                self._emit_state_change()
        return {'modified': modified}

    def delete_notes_batch(self, channel, pairs, expected_revision=None):
        """Eliminazione atomica di piu' coppie (8.2 `notes/batch` DELETE): stessa
        validazione di edit_notes_batch (coppie FIFO esatte), un solo undo.
        Ritorna {removed} (eventi rimossi) o None."""
        self.last_error = None
        if self.is_recording:
            return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                              {'is_recording': True})
        if not self._channel_ok(channel):
            return None
        parsed = self._batch_pairs(channel, pairs, 'pairs', frozenset(('on_idx', 'off_idx')))
        if parsed is None:
            return None
        with self._edit():
            if self.is_recording:
                return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                                  {'is_recording': True})
            if not self.check_revision(expected_revision):
                return None
            if self._batch_check_pairs(self.tracks[channel], self._ids(channel), parsed, 'pairs') is None:
                return None
            drop = set()
            for _e, on, off in parsed:
                drop.add(on)
                if off >= 0:
                    drop.add(off)
            self._push_undo()
            self._drop_indices(channel, drop)
            self._density_dirty = True
            self._emit_state_change()
        return {'removed': len(drop)}

    # ── ui-editor: note di una traccia, appunti di note, eventi batch ──
    # Stesse regole del batch di note: tutto-o-niente, validazione prima
    # dell'undo, un sort, un livello, una revisione; 409 RECORDING_ACTIVE in
    # Record (route nuove: nessun override), consentiti in Play (6.4).

    _EVENT_EDIT_KEYS = frozenset(('index', 'beat', 'cc_num', 'cc_value', 'program', 'pitch', 'value'))
    _EVENT_INT_FIELDS = (('cc_num', 0, 127), ('cc_value', 0, 127), ('program', 0, 127),
                         ('pitch', -8192, 8191), ('value', 0, 127))
    # Campi modificabili per tipo di evento (status senza canale).
    _EVENT_FIELDS = {0xB0: ('cc_num', 'cc_value'), 0xC0: ('program',), 0xE0: ('pitch',),
                     0xD0: ('value',), 0xA0: ('value',)}

    def get_track_notes(self, channel):
        """Note di UNA traccia per l'editor: ({notes, clips, max_beat, beats_per_measure}, revision).
        notes = [note, vel, start, end, on_idx, off_idx, clip_id] (FIFO 6.2, note
        aperte fino all'ultimo beat). Copia sotto lock, calcolo fuori."""
        with self._edit_lock:
            events = list(self.tracks.get(channel) or [])
            ids = array('H', self._ids(channel))
            if self.is_recording and channel in self._rec_tracks:
                # Coda del take non ordinata: vista ordinata (editor in sola lettura).
                events, ids = self._sorted_pair(events, ids)
            clips = sorted([dict(c) for c in self.clips[channel]], key=lambda c: c['start_beat'])
            rev, bpm = self.revision, self.beats_per_measure
        notes, max_beat, _lo, _hi = self._build_notes_full(events)
        out = [[n, v, round(s, 4), round(e, 4), on, off, ids[on]] for n, v, s, e, on, off in notes]
        return {'notes': out, 'clips': clips, 'max_beat': max_beat, 'beats_per_measure': bpm}, rev

    @staticmethod
    def _pairs_preserved(old_events, new_events):
        """True se, dopo il sort, ogni nota di `old_events` ha ancora lo stesso
        partner FIFO (6.2) in `new_events` (stessi indici prima del sort)."""
        order = sorted(range(len(new_events)), key=lambda i: new_events[i][0])
        pos = [0] * len(order)
        for p, i in enumerate(order):
            pos[i] = p
        after = {on: off for _n, _v, _a, _b, on, off in
                 project_store.pair_notes([new_events[i] for i in order])}
        for _n, _v, _a, _b, on, off in project_store.pair_notes(old_events):
            if after.get(pos[on], -2) != (pos[off] if off >= 0 else -1):
                return False
        return True

    def _fit_clips_plan(self, channel, events, ids, touched):
        """D28: cornici da allargare perche' contengano gli eventi posseduti
        spostati (note-on e non-note; i note-off possono uscire, 6.4). Senza
        effetti: [(posizione in clips, clip nuovo)] o None (errore impostato)."""
        bounds = {}
        for i in touched:
            cid = ids[i]
            if not cid or project_store._is_note_off(events[i][1]):
                continue
            b = events[i][0]
            lo, hi = bounds.get(cid, (b, b))
            bounds[cid] = (min(lo, b), max(hi, b))
        plan = []
        for k, c in enumerate(self.clips[channel]):
            got = bounds.get(int(c['id']))
            if got is None:
                continue
            start, end = float(c['start_beat']), float(c['end_beat'])
            ns = min(start, got[0])
            ne = end if got[1] < end else got[1] + project_store.CLIP_TAIL_BEATS
            if ne > project_store.MAX_BEAT:
                return self._nope('VALIDATION_FAILED', f'Clip oltre il beat {project_store.MAX_BEAT}',
                                  {'path': 'clips', 'reason': 'max_beat'})
            if (ns, ne) != (start, end):
                plan.append((k, dict(c, start_beat=ns, end_beat=ne)))
        return plan

    def _apply_clip_plan(self, channel, plan):
        for k, c in plan:
            self.clips[channel][k] = c

    def _events_total(self):
        return sum(len(self.tracks[ch]) for ch in range(16))

    def _max_events_error(self, adding, http=None):
        total = self._events_total() + adding
        if total <= project_store.MAX_EVENTS_TOTAL:
            return False
        self._fail('VALIDATION_FAILED', f'Oltre {project_store.MAX_EVENTS_TOTAL} eventi totali',
                   {'path': 'tracks', 'reason': 'max_events',
                    'max_events': project_store.MAX_EVENTS_TOTAL, 'events': total}, http)
        return True

    def copy_notes(self, channel, pairs, expected_revision=None, remove=False):
        """Copia/Taglia di note scelte (ui-editor, estensione di copy/cut 6.4):
        coppie FIFO esatte come notes/batch, appunti 'range' (origine = primo
        note-on, incolla con paste_at e la regola ID di add_note). Con
        `remove` le stesse coppie vengono eliminate in un solo undo; i clip
        restano. Vietata in Record (indici della coda non ordinata).
        Ritorna {copied, removed, clipboard} o None."""
        self.last_error = None
        if self.is_recording:
            return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                              {'is_recording': True})
        if not self._channel_ok(channel):
            return None
        parsed = self._batch_pairs(channel, pairs, 'pairs', frozenset(('on_idx', 'off_idx')))
        if parsed is None:
            return None
        with self._edit():
            if self.is_recording:
                return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                                  {'is_recording': True})
            if not self.check_revision(expected_revision):
                return None
            events = self.tracks[channel]
            if self._batch_check_pairs(events, self._ids(channel), parsed, 'pairs') is None:
                return None
            indices = sorted({i for _e, on, off in parsed for i in (on, off) if i >= 0})
            origin = min(events[on][0] for _e, on, _off in parsed)
            length = max(events[i][0] for i in indices) - origin
            payload, _released = self._payload(events, indices, origin, length)
            self._set_clipboard(payload, {'kind': 'range', 'length_beats': length, 'source_track': channel,
                                          'source_clip_id': None, 'name': None, 'sustain_release': False})
            res = {'copied': len(indices), 'removed': 0}
            if remove:
                self._push_undo()
                self._drop_indices(channel, set(indices))
                self._density_dirty = True
                res['removed'] = len(indices)
            res['clipboard'] = self.clipboard_info()
            self._emit_state_change()
        return res

    def _event_index_list(self, indices):
        if not isinstance(indices, list) or not indices:
            return self._nope('BAD_REQUEST', 'indices: lista non vuota attesa', {'field': 'indices'})
        for k, idx in enumerate(indices):
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx <= 10 ** 9:
                return self._nope('BAD_REQUEST', f'indices[{k}]: intero >= 0 atteso', {'field': f'indices[{k}]'})
        return indices

    def edit_events_batch(self, channel, edits, expected_revision=None):
        """Modifica atomica di eventi NON nota per indice (ui-editor, editor
        eventi): edits [{index, beat?, cc_num?, cc_value?, program?, pitch?,
        value?}] (`value` = pressione). Note -> 422 (si modificano a coppie con
        notes/batch). Campo non applicabile al tipo -> 422. Ritorna {modified} o None."""
        self.last_error = None
        if self.is_recording:
            return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                              {'is_recording': True})
        if not self._channel_ok(channel):
            return None
        if not isinstance(edits, list) or not edits:
            return self._nope('BAD_REQUEST', 'edits: lista non vuota attesa', {'field': 'edits'})
        parsed = []
        for k, entry in enumerate(edits):
            path = f'edits[{k}]'
            if not isinstance(entry, dict):
                return self._nope('BAD_REQUEST', f'{path}: oggetto atteso', {'field': path})
            extra = sorted(set(entry) - self._EVENT_EDIT_KEYS)
            if extra:
                return self._nope('BAD_REQUEST', f'{path}: campo sconosciuto {extra[0]}',
                                  {'field': f'{path}.{extra[0]}'})
            idx = self._batch_int(entry, 'index', path, 0, 10 ** 9)
            if idx is None:
                return None
            v = {}
            if 'beat' in entry:
                v['beat'] = self._num_ok(entry['beat'], f'{path}.beat', 0.0, project_store.MAX_BEAT)
                if v['beat'] is None:
                    return None
            for key, lo, hi in self._EVENT_INT_FIELDS:
                if key in entry:
                    v[key] = self._batch_int(entry, key, path, lo, hi)
                    if v[key] is None:
                        return None
            if not v:
                return self._nope('BAD_REQUEST', f'{path}: nessuna modifica', {'field': path})
            parsed.append((idx, v))
        with self._edit():
            if self.is_recording:
                return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                                  {'is_recording': True})
            if not self.check_revision(expected_revision):
                return None
            events = self.tracks[channel]
            ids = self._ids(channel)
            new_events = list(events)
            seen, moved = set(), []
            modified = 0
            for k, (idx, v) in enumerate(parsed):
                path = f'edits[{k}]'
                if idx >= len(events):
                    return self._nope('VALIDATION_FAILED', 'Indice evento fuori intervallo',
                                      {'path': f'{path}.index', 'reason': 'fuori intervallo'})
                if idx in seen:
                    return self._nope('VALIDATION_FAILED', 'Indice ripetuto nel batch',
                                      {'path': f'{path}.index', 'reason': f'indice {idx} ripetuto'})
                seen.add(idx)
                beat, d = events[idx]
                kind = d[0] & 0xF0
                if kind in (0x80, 0x90):
                    return self._nope('VALIDATION_FAILED', 'Le note si modificano a coppie (notes/batch)',
                                      {'path': f'{path}.index', 'reason': 'nota'})
                allowed = self._EVENT_FIELDS.get(kind, ())
                wrong = [key for key in v if key != 'beat' and key not in allowed]
                short = len(v) > ('beat' in v) and len(d) < (2 if kind in (0xC0, 0xD0) else 3)
                if wrong or short:
                    return self._nope('VALIDATION_FAILED', 'Campo non applicabile a questo evento',
                                      {'path': f'{path}.{wrong[0] if wrong else "index"}',
                                       'reason': 'tipo di evento'})
                nd = list(d)
                if kind == 0xB0:
                    nd[1] = v.get('cc_num', nd[1])
                    nd[2] = v.get('cc_value', nd[2])
                elif kind == 0xC0:
                    nd[1] = v.get('program', nd[1])
                elif kind == 0xE0 and 'pitch' in v:
                    p = v['pitch'] + 8192
                    nd[1], nd[2] = p & 0x7F, p >> 7
                elif kind == 0xD0:
                    nd[1] = v.get('value', nd[1])
                elif kind == 0xA0:
                    nd[2] = v.get('value', nd[2])
                nb = v.get('beat', beat)
                new = (nb, d if nd == list(d) else nd)
                if new != events[idx]:
                    new_events[idx] = new
                    modified += 1
                    if nb != beat:
                        moved.append(idx)
            if modified:
                plan = self._fit_clips_plan(channel, new_events, ids, moved)
                if plan is None:
                    return None
                self._push_undo()
                self._apply_clip_plan(channel, plan)
                if moved:
                    self._set_sorted(channel, new_events, ids)
                else:
                    self._set_track(channel, new_events, ids)
                self._density_dirty = True
                self._emit_state_change()
        return {'modified': modified}

    def delete_events_batch(self, channel, indices, expected_revision=None):
        """Eliminazione atomica di eventi per indice (ui-editor). Un evento di
        nota porta con se' il suo partner FIFO dentro lo stesso ID clip (come
        _owned_by_id): nessun note-off orfano creato. Ritorna {removed} o None."""
        self.last_error = None
        if self.is_recording:
            return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                              {'is_recording': True})
        if not self._channel_ok(channel) or self._event_index_list(indices) is None:
            return None
        with self._edit():
            if self.is_recording:
                return self._nope('RECORDING_ACTIVE', 'Modifica non consentita durante la registrazione',
                                  {'is_recording': True})
            if not self.check_revision(expected_revision):
                return None
            events = self.tracks[channel]
            for k, idx in enumerate(indices):
                if idx >= len(events):
                    return self._nope('VALIDATION_FAILED', 'Indice evento fuori intervallo',
                                      {'path': f'indices[{k}]', 'reason': 'fuori intervallo'})
            partner = self._group_partners(events, self._ids(channel))
            back = {off: on for on, off in partner.items() if off >= 0}
            drop = set()
            for idx in indices:
                drop.add(idx)
                if partner.get(idx, -1) >= 0:
                    drop.add(partner[idx])
                elif idx in back:
                    drop.add(back[idx])
            self._push_undo()
            self._drop_indices(channel, drop)
            self._density_dirty = True
            self._emit_state_change()
        return {'removed': len(drop)}

    def get_track_activity(self, channel, start_beat=0.0, end_beat=9999.0):
        """Restituisce intervalli uniti di attività note per una traccia nel range richiesto.
        Accoppiamento FIFO unico (A26)."""
        with self._edit_lock:
            events = self.tracks.get(channel)
            if not events:
                return []
            pairs = self._pair_notes(events)

        # Le note rimaste aperte arrivano fino all'end richiesto
        intervals = [(on_b, end_beat if off_b is None else off_b) for _n, _v, on_b, off_b, _i, _j in pairs]

        # Unisci intervalli sovrapposti e applica culling con la finestra richiesta
        intervals = [iv for iv in intervals if iv[1] > start_beat and iv[0] < end_beat]
        if not intervals:
            return []

        intervals.sort(key=lambda iv: iv[0])
        merged = []
        for s, e in intervals:
            s = max(s, start_beat)
            e = min(e, end_beat)
            if e <= s:
                continue
            if not merged or s > merged[-1][1]:
                merged.append([s, e])
            else:
                merged[-1][1] = max(merged[-1][1], e)

        return merged

    def _build_intervals_full(self, events):
        """Costruisce intervalli completi (start, end) in beat per tutte le note
        (accoppiamento FIFO unico, A26; note aperte fino all'ultimo beat)."""
        if not events:
            return [], 0.0
        max_beat = max(b for b, _ in events)
        intervals = [(on_b, max_beat if off_b is None else off_b)
                     for _n, _v, on_b, off_b, _i, _j in self._pair_notes(events)]
        intervals.sort(key=lambda iv: iv[0])
        return intervals, max_beat

    def _build_notes_full(self, events):
        """Build note-level data for piano-roll rendering.
        Returns (notes_list, max_beat, min_note, max_note).
        notes_list: [(note, velocity, start_beat, end_beat, note_on_index, note_off_index), ...]
        """
        if not events:
            return [], 0.0, 127, 0
        max_beat = max(b for b, _ in events)
        notes = [(n, v, on_b, max_beat if off_b is None else off_b, on_i, off_i)
                 for n, v, on_b, off_b, on_i, off_i in self._pair_notes(events)]
        if not notes:
            return [], max_beat, 127, 0
        min_note = min(n[0] for n in notes)
        max_note = max(n[0] for n in notes)
        notes.sort(key=lambda n: n[2])
        return notes, max_beat, min_note, max_note

    def _tracks_snapshot(self, channels=range(16)):
        """Copie superficiali sotto il lock: il calcolo pesante (16000 eventi)
        avviene fuori, cosi' la callback di Record non resta bloccata."""
        with self._edit_lock:
            return ({ch: list(self.tracks.get(ch) or []) for ch in channels},
                    {ch: array('H', self._ids(ch)) for ch in channels},
                    self.beats_per_measure, self.revision, getattr(self, '_cache_gen', 0))

    def _note_map(self):
        with self._edit_lock:
            cached = self._cached_note_map
            if not self.is_recording and cached is not None:
                return cached, self.revision
        snap, _ids, beats_per_measure, rev, gen = self._tracks_snapshot()
        total_beats = 0.0
        tracks = {}
        for ch in range(16):
            notes, max_beat, min_note, max_note = self._build_notes_full(snap[ch])
            total_beats = max(total_beats, max_beat)
            if notes:
                tracks[ch] = {
                    'notes': [
                        [n, v, round(s, 4), round(e, 4), on_idx, off_idx]
                        for n, v, s, e, on_idx, off_idx in notes
                    ],
                    'min_note': min_note,
                    'max_note': max_note
                }
        if total_beats <= 0:
            total_beats = beats_per_measure * 4
        result = {'total_beats': total_beats, 'beats_per_measure': beats_per_measure, 'tracks': tracks}
        with self._edit_lock:
            if gen == getattr(self, '_cache_gen', 0) and not self.is_recording:
                self._cached_note_map = result
        return result, rev

    def get_full_note_map(self):
        """Return per-channel note data for piano-roll rendering. Cached."""
        return self._note_map()[0]

    def get_full_note_map_with_revision(self):
        """(mappa, revisione dello snapshot da cui e' calcolata)."""
        return self._note_map()

    def _density_map(self):
        with self._edit_lock:
            cached = self._cached_density_map
            if not self.is_recording and cached is not None:
                return cached, self.revision
            slot = self.slot_beats
        snap, _ids, beats_per_measure, rev, gen = self._tracks_snapshot()

        all_intervals = {}
        total_beats = 0.0
        for ch in range(16):
            intervals, max_beat = self._build_intervals_full(snap[ch])
            all_intervals[ch] = intervals
            total_beats = max(total_beats, max_beat)

        if total_beats <= 0:
            total_beats = beats_per_measure * 4  # fallback 4 misure

        slots_count = max(1, math.ceil(total_beats / slot))
        density = {}

        for ch in range(16):
            arr = [0] * slots_count
            for start, end in all_intervals[ch]:
                if end <= start:
                    continue
                s_slot = max(0, int(math.floor(start / slot)))
                e_slot = min(slots_count, int(math.ceil(end / slot)))
                for idx in range(s_slot, e_slot):
                    arr[idx] += 1
            density[ch] = arr

        result = {
            'slot_beats': slot,
            'total_beats': total_beats,
            'beats_per_measure': beats_per_measure,
            'tracks': density
        }
        with self._edit_lock:
            if gen == getattr(self, '_cache_gen', 0) and not self.is_recording:
                self._cached_density_map = result
        return result, rev

    def get_full_density_map_with_revision(self):
        return self._density_map()

    def get_full_density_map(self):
        """Ritorna la mappa di densità (slot di 1/8 beat) per tutte le tracce. Cached."""
        return self._density_map()[0]

    def toggle_metronome(self):
        """Toggle metronomo on/off"""
        self.metronome_enabled = not self.metronome_enabled
        
        # Se è attivo durante registrazione/playback, avvia il thread
        if self.metronome_enabled and (self.is_recording or self.is_playing):
            self._start_metronome()
        elif not self.metronome_enabled:
            self._stop_metronome()
        
        self._emit_state_change()
        return self.metronome_enabled
    
    def _start_metronome(self):
        """Avvia il thread del metronomo"""
        if self.metronome_thread is None or not self.metronome_thread.is_alive():
            self.metronome_stop_event.clear()
            self.last_beat = -1
            self.metronome_thread = threading.Thread(
                target=self._metronome_loop,
                daemon=True
            )
            self.metronome_thread.start()
            print("[DAW] Metronomo avviato")
    
    def _stop_metronome(self):
        """Ferma il thread del metronomo"""
        if self.metronome_thread and self.metronome_thread.is_alive():
            self.metronome_stop_event.set()
            self.metronome_thread.join(timeout=0.5)
            print("[DAW] Metronomo fermato")
    
    def _metronome_loop(self):
        """Loop del metronomo (eseguito in thread separato). Click solo sulla
        destinazione interna (sez. 3.5): mai registrato, mai su esterni."""
        if not self.router.internal_available():
            print("[DAW] Click non disponibile: destinazione interna assente")
            return
        
        CLICK_CHANNEL = midi_router.CLICK_CHANNEL      # Canale 10 (drum channel, 0-indexed)
        ACCENT_NOTE = midi_router.CLICK_ACCENT_NOTE    # High Wood Block (primo beat della misura)
        NORMAL_NOTE = midi_router.CLICK_NOTE           # Low Wood Block (altri beat)
        VELOCITY = midi_router.CLICK_VELOCITY
        NOTE_DURATION = 0.05  # 50ms
        
        try:
            while not self.metronome_stop_event.is_set():
                # Calcola il beat corrente dalla timeline position
                current_beat = int(self.timeline_position / self.beat_duration)
                
                # Se è un nuovo beat, suona il click
                if current_beat != self.last_beat:
                    self.last_beat = current_beat
                    
                    # Primo beat della misura = accento
                    beat_in_measure = current_beat % self.beats_per_measure
                    note = ACCENT_NOTE if beat_in_measure == 0 else NORMAL_NOTE
                    
                    # Note ON
                    msg_on = mido.Message('note_on', 
                                         channel=CLICK_CHANNEL, 
                                         note=note, 
                                         velocity=VELOCITY)
                    self.router.send_internal(msg_on)
                    
                    # Breve durata
                    time.sleep(NOTE_DURATION)
                    
                    # Note OFF
                    msg_off = mido.Message('note_off', 
                                          channel=CLICK_CHANNEL, 
                                          note=note, 
                                          velocity=0)
                    self.router.send_internal(msg_off)
                
                # Sleep fino al prossimo possibile beat (con margine)
                time_to_next_beat = self.beat_duration - (self.timeline_position % self.beat_duration)
                time.sleep(min(0.01, time_to_next_beat * 0.5))
                
        except Exception as e:
            print(f"[DAW] Errore nel metronomo: {e}")
    
    def _record_loop(self, recording_channels, initial_position=0.0):
        """Thread di Record: fa solo avanzare la timeline. Gli eventi MIDI
        arrivano dalla callback del router (_on_router_input)."""
        start_time = self._rec_start_time or time.time()
        try:
            while not self.stop_event.is_set():
                self.timeline_position = initial_position + (time.time() - start_time)
                time.sleep(0.005)
        except Exception as e:
            print(f"[DAW] Errore nel loop di registrazione: {e}")

    def _on_router_input(self, parts, msg, t):
        """Callback del router per ogni messaggio canale ricevuto da un input.
        `parts` = tracce il cui input coincide con la porta/canale sorgente."""
        rec = self._rec_tracks
        if not rec:
            return
        elapsed = t - self._rec_start_time
        if elapsed < RECORD_GRACE_S:
            return
        beat_position = (self._rec_initial_pos + elapsed) / self.beat_duration
        targets = [ch for ch in parts if ch in rec]
        if not targets:
            return
        emitted = []
        with self._rec_lock:
            if self._rec_tracks is not rec:
                return
            for ch in targets:
                data = msg.bytes()
                data[0] = (data[0] & 0xF0) | ch     # evento salvato sul canale della traccia
                ids = self._ids(ch)                 # PRIMA dell'append (stessa lunghezza)
                self.tracks[ch].append((beat_position, data))
                ids.append(0)                       # parallelo (D3): ID assegnato allo stop
                emitted.append((ch, len(self.tracks[ch]) - 1))
                if msg.type == 'note_on' and msg.velocity > 0:
                    self.active_notes[ch] += 1
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    self.active_notes[ch] = max(0, self.active_notes[ch] - 1)
        if not self.socketio:
            return
        for ch, idx in emitted:
            ev_data = {'ch': ch, 'beat': round(beat_position, 4), 'type': msg.type, 'idx': idx}
            if msg.type in ('note_on', 'note_off'):
                ev_data['note'] = self._midi_note_name(msg.note)
                ev_data['note_num'] = msg.note
                ev_data['vel'] = msg.velocity
            elif msg.type == 'control_change':
                ev_data['cc_num'] = msg.control
                ev_data['cc_val'] = msg.value
            self.socketio.emit('midi_event_new', ev_data)

    def _collect_playback_events(self):
        """Collect events from tracks with data within the loop range.
        Mute/solo non filtra qui: il gate `_audible` e' applicato all'invio
        (sotto il lock della porta) cosi' ha effetto immediato (A25)."""
        events = []
        for channel in range(16):
            if not self.has_data[channel]:
                continue
            for beat_position, msg_bytes in self.tracks[channel]:
                if self.loop_enabled and self.loop_end > self.loop_start:
                    if beat_position < self.loop_start or beat_position >= self.loop_end:
                        continue
                timestamp_seconds = beat_position * self.beat_duration
                events.append((timestamp_seconds, channel, msg_bytes))
        return events

    def _playback_loop(self):
        """Loop di riproduzione con supporto solo/mute e loop points."""
        send_track = self.router.send_track
        gate = self._audible
        initial_position = self.timeline_position
        
        try:
            while not self.stop_event.is_set():
                start_time = time.time()
                
                all_events = self._collect_playback_events()
                all_events.sort(key=lambda x: x[0])
                
                event_index = 0
                while event_index < len(all_events) and all_events[event_index][0] < initial_position:
                    event_index += 1
                
                if event_index >= len(all_events):
                    break
                
                loop_end_sec = (self.loop_end * self.beat_duration) if (self.loop_enabled and self.loop_end > self.loop_start) else float('inf')
                
                print(f"[DAW] Riproduzione di {len(all_events) - event_index} eventi da {initial_position:.1f}s")
                _last_beat_emit = 0.0
                
                while not self.stop_event.is_set() and event_index < len(all_events):
                    current_time = initial_position + (time.time() - start_time)
                    self.timeline_position = current_time
                    
                    # Emit playback beat position at ~10Hz for Event Viewer highlight
                    now_mono = time.time()
                    if self.socketio and now_mono - _last_beat_emit >= 0.1:
                        _last_beat_emit = now_mono
                        self.socketio.emit('midi_playback_beat', {
                            'beat': round(current_time / self.beat_duration, 4)
                        })
                    
                    if current_time >= loop_end_sec:
                        break
                    
                    while event_index < len(all_events):
                        timestamp, channel, msg_bytes = all_events[event_index]
                        if timestamp <= current_time:
                            try:
                                msg = mido.Message.from_bytes(msg_bytes)
                                # Il router rimappa sul canale della destinazione della parte
                                if hasattr(msg, 'channel'):
                                    send_track(channel, msg, mutable=True, gate=gate)
                            except Exception:
                                pass
                            event_index += 1
                        else:
                            break
                    
                    if event_index < len(all_events):
                        next_timestamp = all_events[event_index][0]
                        sleep_time = min(0.001, max(0, next_timestamp - current_time))
                        time.sleep(sleep_time)
                    else:
                        time.sleep(0.001)
                
                if self.stop_event.is_set():
                    break
                if not self.loop_enabled or self.loop_end <= self.loop_start:
                    break
                # Restart from loop_start: rilascio note/sustain del giro (sez. 3.7)
                self.router.release_tracks(range(16))
                initial_position = self.loop_start * self.beat_duration
                self.timeline_position = initial_position
            
            if not self.is_recording:
                self.is_playing = False
                self.router.release_tracks(range(16))
                print("[DAW] Riproduzione completata")
                
        except Exception as e:
            print(f"[DAW] Errore nel loop di riproduzione: {e}")
    
    def _send_all_notes_off(self):
        """Rilascio note/sustain su tutte le destinazioni, solo sui canali
        toccati da MatSynth (sez. 3.7)."""
        try:
            self.router.release_all()
        except Exception as e:
            print(f"[DAW] Errore rilascio note: {e}")
    
    def __del__(self):
        """Cleanup alla chiusura"""
        try:
            self.stop_all()
            if getattr(self, '_owns_router', False) and self.router is not None:
                self.router.close()
        except Exception:
            pass

    def export_midi(self, channel_programs=None):
        """
        Esporta tutte le tracce con dati come file MIDI (Type 1).
        Include Bank Select + Program Change per ogni canale.

        Args:
            channel_programs: dict {ch_int: {'bank': int|None, 'program': int|None}}
                rappresentazione NEUTRA (sez. 7.1): bank = (MSB << 7) | LSB.
                `bank: None` = nessun CC0/CC32 iniziale, `program: None` =
                nessun PC iniziale; canale assente = CC0=0, CC32=0, PC 0.
        Ritorna i bytes del file MIDI. Nomi traccia = nomi DAW (sez. 7.3).
        """
        # Copie superficiali sotto il lock, costruzione del file fuori.
        with self._edit_lock:
            snapshot = {ch: list(self.tracks.get(ch) or []) for ch in range(16)}
            names = dict(self.track_names)
            bpm, bpmeas = self.bpm, self.beats_per_measure
        mid = mido.MidiFile(type=1, ticks_per_beat=480)
        tempo = mido.bpm2tempo(bpm)
        if channel_programs is None:
            channel_programs = {}

        for ch in range(16):
            events = snapshot[ch]
            if not events:
                continue

            track = mido.MidiTrack()
            # Tempo meta solo sulla prima traccia
            if len(mid.tracks) == 0:
                track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
                track.append(mido.MetaMessage('time_signature',
                                              numerator=bpmeas,
                                              denominator=4, time=0))
            track.append(mido.MetaMessage('track_name',
                                          name=str(names.get(ch) or f'Channel {ch + 1}'), time=0))

            # Inserisci Bank Select + Program Change a tick 0
            ch_prog = channel_programs.get(ch, {})
            bank = ch_prog.get('bank', 0)
            prog = ch_prog.get('program', 0)
            if bank is not None:
                bank_msb = min(127, max(0, (bank >> 7) & 0x7F))
                bank_lsb = min(127, max(0, bank & 0x7F))
                track.append(mido.Message('control_change', channel=ch,
                                          control=0, value=bank_msb, time=0))
                track.append(mido.Message('control_change', channel=ch,
                                          control=32, value=bank_lsb, time=0))
            if prog is not None:
                track.append(mido.Message('program_change', channel=ch,
                                          program=min(127, max(0, prog)), time=0))

            last_tick = 0
            for beat_pos, msg_bytes in events:      # gia' ordinati per beat
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    continue
                abs_tick = int(round(beat_pos * 480))
                delta = max(0, abs_tick - last_tick)
                # Remap channel to track channel (events may have been copied from another track)
                if hasattr(msg, 'channel'):
                    msg = msg.copy(time=delta, channel=ch)
                else:
                    msg = msg.copy(time=delta)
                track.append(msg)
                last_tick = max(last_tick, abs_tick)

            track.append(mido.MetaMessage('end_of_track', time=0))
            mid.tracks.append(track)

        buf = io.BytesIO()
        mid.save(file=buf)
        return buf.getvalue()

    # Meta gestiti dall'import: gli altri messaggi senza canale sono contati
    # come scartati (warning IMPORT_DROPPED_EVENTS, sez. 7.2).
    _IMPORT_HANDLED_META = frozenset(('set_tempo', 'time_signature', 'track_name', 'end_of_track'))

    @staticmethod
    def _analyze_smf(mid):
        """Pre-passata su TUTTE le tracce per tick assoluto (sez. 7.2, A1):
        tempo/metro, messaggi per canale in ordine (tick, sequenza), nomi
        traccia, scartati. Nessuna mutazione della DAW."""
        tempos, sigs, dropped = [], [], {}
        per_ch = {}
        names = {}
        seq = 0
        for tr_idx, track in enumerate(mid.tracks):
            abs_tick = 0
            chans = set()
            name = None
            for msg in track:
                abs_tick += msg.time
                seq += 1
                if msg.is_meta:
                    if msg.type == 'set_tempo':
                        tempos.append((abs_tick, seq, msg.tempo))
                    elif msg.type == 'time_signature':
                        sigs.append((abs_tick, seq, msg.numerator, msg.denominator))
                    elif msg.type == 'track_name':
                        name = msg.name
                    elif msg.type not in MultiTrackDAW._IMPORT_HANDLED_META:
                        dropped[msg.type] = dropped.get(msg.type, 0) + 1
                    continue
                if not hasattr(msg, 'channel'):
                    dropped[msg.type] = dropped.get(msg.type, 0) + 1
                    continue
                chans.add(msg.channel)
                per_ch.setdefault(msg.channel, []).append((abs_tick, seq, msg))
            if name and len(chans) == 1:
                names[next(iter(chans))] = name
        for lst in per_ch.values():
            lst.sort(key=lambda x: (x[0], x[1]))
        tempos.sort()
        sigs.sort()
        return tempos, sigs, per_ch, names, dropped

    def import_midi(self, midi_bytes, merge=False, convert=False):
        """
        Importa un file MIDI nelle tracce del DAW (sez. 7.2).

        Args:
            midi_bytes: contenuto binario del file MIDI
            merge: se True, aggiunge agli eventi esistenti; se False, sostituisce
            convert: accetta tempo map / metro non supportato usando il primo
                tempo e 4/4 (warning IMPORT_CONVERTED); senza, DawError
                IMPORT_NEEDS_CONVERSION.
        Returns:
            dict con info su tracce importate (+ 'warnings', 'clips').
        Un solo livello di undo per l'intero import (A13); un clip per traccia
        importata (A7); PC iniziale con bank MSB/LSB, altri PC/CC0/CC32 restano
        eventi nel tempo (A1).
        """
        if self.is_recording or self.is_playing:
            raise RuntimeError('Cannot import while recording or playing')

        try:
            mid = mido.MidiFile(file=io.BytesIO(midi_bytes))
        except Exception as e:
            raise DawError('VALIDATION_FAILED', f'File MIDI non valido: {e}',
                           {'path': '', 'reason': str(e)})

        tempos, sigs, per_ch, names, dropped = self._analyze_smf(mid)

        # Tempo/metro: controllo PRIMA di qualunque mutazione.
        issues = []
        if len({t[2] for t in tempos}) > 1:
            issues.append({'type': 'tempo_map', 'count': len(tempos)})
        sig_values = {(s[2], s[3]) for s in sigs}
        if len(sig_values) > 1:
            issues.append({'type': 'meter', 'count': len(sigs),
                           'values': sorted(f'{n}/{d}' for n, d in sig_values)})
        elif sig_values:
            n, d = next(iter(sig_values))
            if n not in (3, 4) or d != 4:
                issues.append({'type': 'meter', 'value': f'{n}/{d}'})
        imported_bpm = int(round(mido.tempo2bpm(tempos[0][2]))) if tempos else self.bpm
        if not 30 <= imported_bpm <= 300:
            # Riportato in 30..300: e' una conversione, mai in silenzio (review L8).
            issues.append({'type': 'tempo_range', 'value': imported_bpm})
            imported_bpm = max(30, min(300, imported_bpm))
        warnings = []
        if issues:
            if not convert:
                raise DawError('IMPORT_NEEDS_CONVERSION',
                               'Il file usa tempo/metro non supportati: serve una conversione esplicita',
                               {'issues': issues})
            warnings.append({'code': 'IMPORT_CONVERTED',
                             'message': 'Importato con il primo tempo (in 30..300 BPM) e 4/4 dove il metro non e\' supportato',
                             'details': {'issues': issues}})
        imported_ts = self.beats_per_measure
        if sigs:
            n, d = sigs[0][2], sigs[0][3]
            imported_ts = n if (n in (3, 4) and d == 4) else 4
        if dropped:
            warnings.append({'code': 'IMPORT_DROPPED_EVENTS',
                             'message': 'Messaggi senza canale (SysEx, realtime, meta) scartati',
                             'details': {'counts': dict(sorted(dropped.items()))}})

        ticks_per_beat = mid.ticks_per_beat or 480
        channel_programs = {}       # {ch: {'bank': (MSB<<7)|LSB, 'program': p}}
        new_by_ch = {}
        for ch, msgs in per_ch.items():
            first_note = next((tk for tk, _s, m in msgs if m.type == 'note_on' and m.velocity > 0), None)
            initial = None                         # indice del PC iniziale
            for i, (tk, _s, m) in enumerate(msgs):
                if m.type == 'program_change':
                    if first_note is None or tk <= first_note:
                        initial = i
                    break
            consumed = set()
            if initial is not None:
                msb = lsb = 0
                for i in range(initial):
                    m = msgs[i][2]
                    if m.type == 'control_change' and m.control in (0, 32):
                        if m.control == 0:
                            msb = m.value
                        else:
                            lsb = m.value
                        consumed.add(i)            # sostituiti dal suono iniziale
                consumed.add(initial)
                channel_programs[ch] = {'bank': (msb << 7) | lsb,
                                        'program': msgs[initial][2].program}
            evs = [(tk / ticks_per_beat, m.bytes()) for i, (tk, _s, m) in enumerate(msgs)
                   if i not in consumed]
            if evs:
                new_by_ch[ch] = evs

        # Review M1: limiti del formato verificati PRIMA di mutare (un import
        # oltre i limiti produrrebbe un progetto non salvabile).
        last_beat = max((evs[-1][0] for evs in new_by_ch.values()), default=0.0)
        if last_beat + project_store.CLIP_TAIL_BEATS > project_store.MAX_BEAT:
            raise DawError('VALIDATION_FAILED', f'Il file supera la durata massima ({project_store.MAX_BEAT} beat)',
                           {'path': 'tracks', 'reason': 'max_beat', 'max_beat': project_store.MAX_BEAT})
        n_new = sum(len(evs) for evs in new_by_ch.values())

        clips_created = {}
        with self._edit():
            if self.is_recording or self.is_playing:
                raise RuntimeError('Cannot import while recording or playing')
            kept = 0 if not merge else sum(len(self.tracks[ch]) for ch in range(16))
            if kept + n_new > project_store.MAX_EVENTS_TOTAL:
                raise DawError('VALIDATION_FAILED',
                               f'Oltre {project_store.MAX_EVENTS_TOTAL} eventi totali',
                               {'path': 'tracks', 'reason': 'max_events',
                                'max_events': project_store.MAX_EVENTS_TOTAL, 'events': kept + n_new})
            # Review L2: ID verificati prima di qualunque mutazione.
            if new_by_ch and self._next_clip_id + len(new_by_ch) - 1 > project_store.MAX_CLIP_ID:
                raise DawError('VALIDATION_FAILED', 'Limite ID clip raggiunto',
                               {'path': 'clips', 'reason': 'max_clip_id'})
            self._push_undo(transport=True)        # UN livello per l'intero import (A13)
            if not merge:
                for ch in range(16):
                    self._set_track(ch, [], array('H'))
                    self.clips[ch] = []
                for ch, name in names.items():
                    if 0 <= ch <= 15 and str(name).strip():
                        self.track_names[ch] = str(name).strip()[:32]
            self.bpm = max(30, min(300, imported_bpm))
            self.beat_duration = 60.0 / self.bpm
            self.beats_per_measure = imported_ts
            for ch, evs in new_by_ch.items():
                clip_id = self._alloc_clip_id()
                start_beat = min(b for b, _ in evs)
                end_beat = max(b for b, _ in evs) + project_store.CLIP_TAIL_BEATS
                self.clips[ch].append({'id': clip_id, 'start_beat': float(start_beat),
                                       'end_beat': float(end_beat), 'name': f'Import {clip_id}',
                                       'created_at': time.time()})
                clips_created[ch] = clip_id
                events = list(self.tracks[ch]) + evs
                ids = array('H', self._ids(ch))
                ids.extend(array('H', [clip_id]) * len(evs))
                self._set_sorted(ch, events, ids)
            self._density_dirty = True
            self._emit_state_change()
            info = {
                'bpm': self.bpm,
                'beats_per_measure': self.beats_per_measure,
                'channels': sorted(new_by_ch),
                'channel_programs': channel_programs,
                'total_events': sum(len(self.tracks[ch]) for ch in new_by_ch),
                'clips': clips_created,
                'warnings': warnings,
                'revision': self.revision,
            }
        print(f'[DAW] MIDI importato: BPM={info["bpm"]}, canali={info["channels"]}, programmi={channel_programs}')
        return info

    # ── Sessione (progetto nativo, sez. 5) ─────────────────────────────

    def snapshot_session(self):
        """Copia superficiale del contenuto musicale per il salvataggio
        (sez. 5.5): liste di riferimenti, array copiati; presa sotto il lock
        di editing (che e' anche quello della callback di Record), la
        serializzazione avviene FUORI dal lock."""
        with self._edit_lock:
            return {
                'tracks': {ch: list(self.tracks[ch]) for ch in range(16)},
                'event_clips': {ch: array('H', self._ids(ch)) for ch in range(16)},
                'clips': {ch: [dict(c) for c in self.clips[ch]] for ch in range(16)},
                'track_names': dict(self.track_names), 'track_colors': dict(self.track_colors),
                'muted': dict(self.muted), 'solo': dict(self.solo),
                'bpm': self.bpm, 'beats_per_measure': self.beats_per_measure,
                'loop_enabled': self.loop_enabled, 'loop_start': self.loop_start,
                'loop_end': self.loop_end, 'next_clip_id': self._next_clip_id,
                'revision': self.revision,
            }

    def load_session(self, content):
        """Scambio atomico della sessione (Apri/Nuovo, sez. 5.6 punto 4).
        `content` e' gia' completo e validato (project_store.project_to_session):
        qui nessuna validazione puo' fallire a meta'. Trasporto fermo, undo/redo
        svuotati, revisione +1, posizione 0, arm tutti off, click non avviato.
        Non rende il progetto modificato (lo stato del progetto lo gestisce app.py)."""
        for ch in range(16):
            if len(content['tracks'].get(ch, [])) != len(content['event_clips'].get(ch, [])):
                raise DawError('INTERNAL', f'Sessione incoerente sulla traccia {ch}')
        with self._edit():
            if self.is_playing or self.is_recording:
                raise DawError('TRANSPORT_ACTIVE', 'Ferma Play/Record prima di aprire',
                               {'is_playing': self.is_playing, 'is_recording': self.is_recording})
            for ch in range(16):
                self._set_track(ch, content['tracks'].get(ch, []),
                                content['event_clips'].get(ch, array('H')))
                self.clips[ch] = [dict(c) for c in content['clips'].get(ch, [])]
                self.track_names[ch] = content['track_names'].get(ch, f'Track {ch + 1}')
                self.track_colors[ch] = content['track_colors'].get(ch, ch)
                self.muted[ch] = bool(content['muted'].get(ch, False))
                self.solo[ch] = bool(content['solo'].get(ch, False))
                self.armed[ch] = False
            self.bpm = max(30, min(300, content['bpm']))
            self.beat_duration = 60.0 / self.bpm
            self.beats_per_measure = content['beats_per_measure']
            self.loop_enabled = bool(content['loop_enabled'])
            self.loop_start = float(content['loop_start'])
            self.loop_end = float(content['loop_end'])
            self._next_clip_id = int(content['next_clip_id'])
            self._undo_stack.clear()
            self._redo_stack.clear()
            self.timeline_position = 0.0
            self._update_audible()
            self.revision += 1
            self._density_dirty = True
            self._emit_state_change()
        return self.last_revision

    @staticmethod
    def empty_session():
        """Contenuto di Nuovo progetto (sez. 5.6)."""
        return {'tracks': {ch: [] for ch in range(16)},
                'event_clips': {ch: array('H') for ch in range(16)},
                'clips': {ch: [] for ch in range(16)},
                'track_names': {ch: f'Track {ch + 1}' for ch in range(16)},
                'track_colors': {ch: ch for ch in range(16)},
                'muted': {ch: False for ch in range(16)}, 'solo': {ch: False for ch in range(16)},
                'bpm': 120, 'beats_per_measure': 4, 'loop_enabled': False,
                'loop_start': 0.0, 'loop_end': 0.0, 'next_clip_id': 1}

    def check_invariants(self):
        """Diagnostica/test: tracks[t] e event_clips[t] della stessa lunghezza,
        eventi ordinati, nessun ID clip pendente. Ritorna la lista dei problemi."""
        problems = []
        with self._edit_lock:
            for ch in range(16):
                ev = self.tracks[ch]
                ids = self.event_clips.get(ch)
                if ids is None or len(ids) != len(ev):
                    problems.append(f'traccia {ch}: {len(ev)} eventi, {None if ids is None else len(ids)} ID')
                    continue
                if any(ev[i][0] > ev[i + 1][0] for i in range(len(ev) - 1)):
                    problems.append(f'traccia {ch}: eventi non ordinati')
                valid = {int(c['id']) for c in self.clips[ch]}
                if any(cid and cid not in valid for cid in ids):
                    problems.append(f'traccia {ch}: ID clip pendente')
        return problems
