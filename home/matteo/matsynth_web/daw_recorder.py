"""
Micro-DAW Recorder Module
Ottimizzato per Raspberry Pi Zero 2W con bassa latenza e CPU overhead minimo
"""

import mido
import io
import time
import math
import threading
from collections import defaultdict

import midi_router

RECORD_GRACE_S = 0.01   # ignora i primi 10 ms di Record (eventi residui)


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
        self.bpm = bpm
        self.beat_duration = 60.0 / bpm  # durata di un beat in secondi
        self.beats_per_measure = 4       # 4/4 di default
        self.slot_beats = 0.125          # risoluzione densità (1/8 di beat)
        self.socketio = socketio  # WebSocket per notifiche real-time
        
        # Storage tracce: {channel_id: [(beat_position, midi_msg), ...]}
        # Gli eventi sono salvati in beat (unità musicale) per supportare cambi di BPM
        self.tracks = defaultdict(list)

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
        
        # Porte MIDI: tutte tramite il router (unico lettore per input,
        # unica risoluzione delle destinazioni, tracciamento note).
        self._audible = bytearray(b'\x01' * 16)   # gate mute/solo letto sotto il lock della porta
        self._rec_lock = threading.Lock()
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
    
    def _emit_state_change(self):
        """Emette lo stato DAW corrente via WebSocket"""
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
    
    def start_recording(self, channel=None):
        """
        Avvia la registrazione su canali armati
        
        Args:
            channel: Canale MIDI (0-15) da registrare, o None per tutti i canali armati
            
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

        # Capture recording-take boundaries to build clips when recording stops
        self._recording_channels = list(recording_channels)
        self._record_start_event_index = {ch: len(self.tracks[ch]) for ch in recording_channels}
        
        # Se ci sono altre tracce con dati (non in registrazione), avvia anche la riproduzione
        has_other_tracks = any(self.has_data[ch] for ch in range(16) if ch not in recording_channels)
        
        for ch in range(16):
            self.active_notes[ch] = 0

        self.is_recording = True
        self.stop_event.clear()
        
        # NON resettare timeline position - registra dalla posizione corrente
        # (solo REWIND resetta a 0.0)
        initial_position = self.timeline_position
        self._record_take_start_beat = max(0.0, initial_position / self.beat_duration)

        # Gli eventi arrivano dalla callback del router (_on_router_input);
        # il thread di Record fa solo avanzare la timeline.
        with self._rec_lock:
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

        for ch in self._recording_channels:
            self._close_open_recorded_notes(ch, stop_beat)
            if self.tracks[ch]:
                self.tracks[ch].sort(key=lambda x: x[0])
                self.has_data[ch] = True
                self._density_dirty = True
                print(f"[DAW] Registrati {len(self.tracks[ch])} eventi su canale {ch}")

        # Build one clip per channel for the just-finished recording take
        self._finalize_recording_clips()
        
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
        for (mch, note), count in sorted(open_notes.items()):
            for _ in range(count):
                self.tracks[ch].append((beat, [0x80 | mch, note, 0]))
                added += 1
        for mch, held in sorted(sustain.items()):
            if held:
                self.tracks[ch].append((beat, [0xB0 | mch, 64, 0]))
                added += 1
        if added:
            print(f"[DAW] Stop Record: chiusi {added} eventi aperti su traccia {ch}")

    def _finalize_recording_clips(self):
        """Create clip objects from events recorded in the last take."""
        channels = self._recording_channels or []
        if not channels:
            return

        for ch in channels:
            start_idx = self._record_start_event_index.get(ch, len(self.tracks[ch]))
            new_events = self.tracks[ch][start_idx:]
            if not new_events:
                continue

            start_beat = min(b for b, _ in new_events)
            end_beat = max(b for b, _ in new_events)
            if end_beat <= start_beat:
                end_beat = start_beat + 0.125

            clip_id = self._next_clip_id
            self._next_clip_id += 1
            self.clips[ch].append({
                'id': clip_id,
                'start_beat': float(start_beat),
                'end_beat': float(end_beat),
                'name': f'Take {clip_id}',
                'created_at': time.time()
            })

        self._recording_channels = []
        self._record_start_event_index = {}
        self._record_take_start_beat = 0.0
    
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
        return True

    def toggle_loop(self):
        """Attiva/disattiva il loop tra loop_start e loop_end."""
        if self.loop_end > self.loop_start:
            self.loop_enabled = not self.loop_enabled
        else:
            self.loop_enabled = False
        self._emit_state_change()
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
        return True
    
    def solo_track(self, channel, solo=True):
        """
        Attiva/disattiva il solo su una traccia.
        Quando almeno una traccia è in solo, solo quelle producono suono.
        """
        self.solo[channel] = solo
        self._apply_audibility()
        self._emit_state_change()
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
    
    def clear_track(self, channel):
        """Cancella tutti gli eventi di una traccia"""
        self._push_undo()
        self.tracks[channel] = []
        self.clips[channel] = []
        self.has_data[channel] = False
        self._density_dirty = True
        print(f"[DAW] Traccia {channel} cancellata")
        self._emit_state_change()
        return True
    
    def clear_range(self, channel, start_beat, end_beat):
        """Cancella note il cui note_on cade nell'intervallo [start_beat, end_beat).

        Usa il note-pair-aware approach: se il note_on è nel range, sia note_on
        che note_off vengono rimossi per mantenere l'integrità delle coppie.
        """
        if channel < 0 or channel > 15:
            return False
        indices_to_delete = self._get_note_pair_indices_in_range(channel, start_beat, end_beat)
        if not indices_to_delete:
            return True
        self._push_undo()
        before = len(self.tracks[channel])
        self.tracks[channel] = [
            (b, msg) for idx, (b, msg) in enumerate(self.tracks[channel])
            if idx not in indices_to_delete
        ]
        self.clips[channel] = [
            c for c in self.clips[channel]
            if c['end_beat'] <= start_beat or c['start_beat'] >= end_beat
        ]
        removed = before - len(self.tracks[channel])
        self.has_data[channel] = len(self.tracks[channel]) > 0
        self._density_dirty = True
        print(f"[DAW] clear_range ch={channel} [{start_beat:.2f}, {end_beat:.2f}) rimossi {removed} eventi")
        self._emit_state_change()
        return True

    def clear_all_tracks(self):
        """Cancella tutte le tracce"""
        self._push_undo()
        for ch in range(16):
            self.tracks[ch] = []
            self.clips[ch] = []
            self.has_data[ch] = False
        self._density_dirty = True
        print("[DAW] Tutte le tracce cancellate")
        self._emit_state_change()
        return True

    # ──────────────────────────────────────────────────────────────────
    # UNDO / REDO (leggero per Pi Zero 2W, max 10 livelli)
    # ──────────────────────────────────────────────────────────────────

    def _take_snapshot(self):
        """Cattura stato corrente tracce (shallow copy — tuple sono immutabili)."""
        return {
            'tracks': {ch: list(self.tracks[ch]) for ch in range(16)},
            'clips': {ch: [dict(c) for c in self.clips[ch]] for ch in range(16)},
            'next_clip_id': self._next_clip_id,
            'has_data': self.has_data.copy(),
            'track_names': self.track_names.copy(),
            'track_colors': self.track_colors.copy()
        }

    def _push_undo(self):
        """Salva snapshot nello stack undo. Chiamare PRIMA dell'operazione distruttiva."""
        self._undo_stack.append(self._take_snapshot())
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _restore_snapshot(self, snapshot):
        """Applica uno snapshot alle tracce."""
        for ch in range(16):
            self.tracks[ch] = snapshot['tracks'].get(ch, [])
            self.clips[ch] = [dict(c) for c in snapshot.get('clips', {}).get(ch, [])]
            self.has_data[ch] = snapshot['has_data'].get(ch, False)
        self._next_clip_id = int(snapshot.get('next_clip_id', self._next_clip_id))
        if 'track_names' in snapshot:
            self.track_names = snapshot['track_names'].copy()
        if 'track_colors' in snapshot:
            self.track_colors = snapshot['track_colors'].copy()
        self._density_dirty = True
        self._emit_state_change()

    def undo(self):
        """Annulla l'ultima operazione distruttiva."""
        if self.is_recording or self.is_playing:
            return False
        if not self._undo_stack:
            return False
        self._redo_stack.append(self._take_snapshot())
        self._restore_snapshot(self._undo_stack.pop())
        return True

    def redo(self):
        """Ripristina l'ultima operazione annullata."""
        if self.is_recording or self.is_playing:
            return False
        if not self._redo_stack:
            return False
        self._undo_stack.append(self._take_snapshot())
        self._restore_snapshot(self._redo_stack.pop())
        return True

    # ──────────────────────────────────────────────────────────────────
    # COPY / PASTE (P2-3)
    # ──────────────────────────────────────────────────────────────────

    def copy_selection(self, channel, start_beat, end_beat):
        """
        Copy events from a channel in [start_beat, end_beat) to clipboard.
        Events stored with beat positions relative to start_beat.
        Returns number of copied events.
        """
        if channel < 0 or channel > 15 or end_beat <= start_beat:
            return 0
        self._clipboard = [
            (b - start_beat, msg)
            for b, msg in self.tracks.get(channel, [])
            if start_beat <= b < end_beat
        ]
        self._clipboard_channel = channel
        return len(self._clipboard)

    def paste_at(self, channel, target_beat):
        """
        Paste clipboard events at target_beat on the given channel.
        Returns number of pasted events.
        """
        if not self._clipboard or self.is_recording or self.is_playing:
            return 0
        if channel < 0 or channel > 15:
            return 0
        self._push_undo()
        for rel_beat, msg_bytes in self._clipboard:
            self.tracks[channel].append((target_beat + rel_beat, msg_bytes))
        self.tracks[channel].sort(key=lambda x: x[0])
        self.has_data[channel] = len(self.tracks[channel]) > 0
        self._density_dirty = True
        self._emit_state_change()
        return len(self._clipboard)

    def duplicate_track(self, src_channel, dest_channel):
        """Copy MIDI events from src_channel to dest_channel. Only events are copied, not instrument/CC/color/name.
        If dest_channel has existing events, they are replaced. Returns dest channel or -1."""
        if self.is_recording or self.is_playing:
            return -1
        if src_channel < 0 or src_channel > 15 or dest_channel < 0 or dest_channel > 15:
            return -1
        if src_channel == dest_channel:
            return -1
        if not self.tracks[src_channel]:
            return -1
        was_empty = not self.tracks[dest_channel]
        self._push_undo()
        self.tracks[dest_channel] = list(self.tracks[src_channel])
        self.has_data[dest_channel] = True
        # Only set name if dest was empty
        if was_empty:
            self.track_names[dest_channel] = 'Copy of ' + self.track_names.get(src_channel, f'Track {src_channel+1}')
        self._density_dirty = True
        self._emit_state_change()
        return dest_channel

    def rename_track(self, channel, name):
        """Rename a track. Name is trimmed to 32 chars."""
        if channel < 0 or channel > 15:
            return False
        self.track_names[channel] = str(name)[:32]
        self._emit_state_change()
        return True

    def set_track_color(self, channel, color_index):
        """Set track color by palette index (0-15)."""
        if channel < 0 or channel > 15:
            return False
        self.track_colors[channel] = max(0, min(15, int(color_index)))
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
          - Accoppia ogni note_on con il suo note_off per conservare la durata.
          - Quantizza il note_on; note_off = note_on_quantizzato + durata originale.
          - Riordina gli eventi per posizione temporale.

        Args:
            channels:    lista di canali 0-15 da processare.
                         Lista vuota = tutti i canali con dati.
            grid_beats:  dimensione della griglia in beat.
            strength:    0.0-1.0 (1.0 = allineamento perfetto).
            swing:       0.0-1.0 (0.5 = straight, ciclo jazz tipico ≈ 0.67).

        Returns:
            dict {channel: n_note_quantizzate} oppure None se in play/rec.
        """
        if self.is_recording or self.is_playing:
            return None

        if not channels:
            channels = [ch for ch in range(16) if self.has_data[ch]]

        self._push_undo()
        results = {}

        for ch in channels:
            if not self.has_data[ch] or not self.tracks[ch]:
                continue

            events = self.tracks[ch]
            active_notes = {}   # note_num → (on_beat, on_index)
            new_beats = {}      # event_index → nuova posizione in beat
            modified = 0

            for i, (beat, msg_bytes) in enumerate(events):
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    continue

                if msg.type == 'note_on' and msg.velocity > 0:
                    active_notes[msg.note] = (beat, i)

                elif msg.type == 'note_off' or (
                        msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active_notes:
                        on_beat, on_idx = active_notes.pop(msg.note)
                        duration = beat - on_beat

                        q_on  = self._quantize_beat_pos(
                            on_beat, grid_beats, swing, strength)
                        q_off = q_on + max(0.0, duration)

                        new_beats[on_idx] = q_on
                        new_beats[i]      = q_off
                        modified += 1

            if new_beats:
                new_events = [
                    (new_beats.get(i, beat), msg_bytes)
                    for i, (beat, msg_bytes) in enumerate(events)
                ]
                new_events.sort(key=lambda x: x[0])
                self.tracks[ch] = new_events

            results[ch] = modified
            print(f"[DAW] quantize ch={ch}: {modified} note, "
                  f"grid={grid_beats}, str={strength:.2f}, swing={swing:.2f}")

        if results:
            self._density_dirty = True
            self._emit_state_change()

        return results

    def _get_note_pair_indices_in_range(self, channel, start_beat, end_beat):
        """Return set of event indices for notes whose note_on starts in [start_beat, end_beat).

        A note 'belongs' to a range if its note_on is in the range.
        Both note_on AND note_off indices are included so note pairs stay intact.
        Non-note events (CC, pitch bend, etc.) in the range are also included.
        """
        events = self.tracks.get(channel, [])
        if not events:
            return set()

        notes, _, _, _ = self._build_notes_full(events)
        indices = set()

        # Include both on/off indices for notes whose note_on is in range
        for _note, _vel, s_beat, _e_beat, on_idx, off_idx in notes:
            if start_beat <= s_beat < end_beat:
                indices.add(on_idx)
                if off_idx >= 0:
                    indices.add(off_idx)

        # Include non-note events in the range (CC, pitch bend, etc.)
        for idx, (beat, msg_bytes) in enumerate(events):
            if idx in indices:
                continue
            if start_beat <= beat < end_beat:
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    indices.add(idx)
                    continue
                if msg.type not in ('note_on', 'note_off'):
                    indices.add(idx)

        return indices

    def transpose_notes(self, channel, semitones, start_beat=None, end_beat=None):
        """Transpose note events on a channel by semitones.

        If start_beat/end_beat are provided, transpose only notes whose note_on
        is within [start_beat, end_beat). Both note_on and note_off are transposed
        to keep note pairs consistent.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return 0

        events = self.tracks.get(channel)
        if not events:
            return 0

        st = float(start_beat) if start_beat is not None else None
        en = float(end_beat) if end_beat is not None else None
        if st is not None and en is not None and en <= st:
            return 0

        semitones = int(semitones)
        if semitones == 0:
            return 0

        # When a range is given, use note-pair-aware index set
        if st is not None and en is not None:
            pair_indices = self._get_note_pair_indices_in_range(channel, st, en)
        else:
            pair_indices = None  # transpose all

        updated = []
        modified = 0
        for idx, (beat, msg_bytes) in enumerate(events):
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                updated.append((beat, msg_bytes))
                continue

            should_transpose = (pair_indices is None or idx in pair_indices)

            if should_transpose and hasattr(msg, 'note') and msg.type in ('note_on', 'note_off'):
                new_note = max(0, min(127, int(msg.note) + semitones))
                if new_note != msg.note:
                    msg = msg.copy(note=new_note)
                    modified += 1
            updated.append((beat, msg.bytes()))

        if modified <= 0:
            return 0

        self._push_undo()
        self.tracks[channel] = updated
        self.has_data[channel] = len(self.tracks[channel]) > 0
        self._density_dirty = True
        self._emit_state_change()
        print(f"[DAW] transpose ch={channel} semitones={semitones} modified={modified} range=({st},{en})")
        return modified

    def get_clips(self, channel):
        """Return clips for a channel sorted by start beat."""
        if channel < 0 or channel > 15:
            return []
        return sorted([dict(c) for c in self.clips[channel]], key=lambda c: c['start_beat'])

    def move_clip(self, channel, clip_id, target_start_beat):
        """Move a clip and all its note-pair-aware events to a target start beat.

        Uses note_on position to determine clip ownership: if note_on is inside
        the clip range, both note_on AND note_off are moved together.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return False

        clip = next((c for c in self.clips[channel] if int(c['id']) == int(clip_id)), None)
        if not clip:
            return False

        old_start = float(clip['start_beat'])
        old_end = float(clip['end_beat'])
        target_start = max(0.0, float(target_start_beat))
        delta = target_start - old_start
        if abs(delta) < 1e-9:
            return True

        indices_to_move = self._get_note_pair_indices_in_range(channel, old_start, old_end)

        self._push_undo()
        moved = []
        for idx, (beat, msg_bytes) in enumerate(self.tracks[channel]):
            if idx in indices_to_move:
                moved.append((max(0.0, beat + delta), msg_bytes))
            else:
                moved.append((beat, msg_bytes))
        moved.sort(key=lambda x: x[0])
        self.tracks[channel] = moved

        clip['start_beat'] = max(0.0, old_start + delta)
        clip['end_beat'] = max(clip['start_beat'] + 0.01, old_end + delta)
        self._density_dirty = True
        self._emit_state_change()
        return True

    def delete_clip(self, channel, clip_id):
        """Delete a clip and remove all its note-pair-aware events.

        Uses note_on position to determine clip ownership: if note_on is inside
        the clip range, both note_on AND note_off are removed.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return False

        clip = next((c for c in self.clips[channel] if int(c['id']) == int(clip_id)), None)
        if not clip:
            return False

        start_beat = float(clip['start_beat'])
        end_beat = float(clip['end_beat'])
        indices_to_delete = self._get_note_pair_indices_in_range(channel, start_beat, end_beat)

        self._push_undo()
        self.tracks[channel] = [
            (b, msg) for idx, (b, msg) in enumerate(self.tracks[channel])
            if idx not in indices_to_delete
        ]
        self.clips[channel] = [c for c in self.clips[channel] if int(c['id']) != int(clip_id)]
        self.has_data[channel] = len(self.tracks[channel]) > 0
        self._density_dirty = True
        self._emit_state_change()
        return True

    def transpose_clip(self, channel, clip_id, semitones):
        """Transpose note events inside one clip range."""
        if channel < 0 or channel > 15:
            return 0
        clip = next((c for c in self.clips[channel] if int(c['id']) == int(clip_id)), None)
        if not clip:
            return 0
        return self.transpose_notes(channel, semitones, clip['start_beat'], clip['end_beat'])

    def resize_clip(self, channel, clip_id, new_end_beat):
        """Non-destructive clip resize: only changes end_beat, does NOT delete events.

        Notes beyond new_end_beat remain in the track; they just won't be
        visible inside the clip boundary. Extending the clip later will
        reveal them again.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return False

        clip = next((c for c in self.clips[channel] if int(c['id']) == int(clip_id)), None)
        if not clip:
            return False

        try:
            new_end_val = float(new_end_beat)
        except (TypeError, ValueError):
            return False

        new_end = max(float(clip['start_beat']) + 0.125, new_end_val)
        if abs(new_end - float(clip['end_beat'])) < 1e-9:
            return True

        self._push_undo()
        clip['end_beat'] = new_end
        self._density_dirty = True
        self._emit_state_change()
        print(f"[DAW] resize_clip ch={channel} id={clip_id} new_end={new_end:.3f}")
        return True

    def split_clip(self, channel, clip_id, split_beat):
        """Split a clip at split_beat into two clips.

        Original clip becomes [start_beat, split_beat).
        New clip becomes [split_beat, end_beat).
        Events are NOT moved — they stay in place. Each clip just marks a range.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return False

        clip = next((c for c in self.clips[channel] if int(c['id']) == int(clip_id)), None)
        if not clip:
            return False

        try:
            split_at = float(split_beat)
        except (TypeError, ValueError):
            return False
        old_start = float(clip['start_beat'])
        old_end = float(clip['end_beat'])

        # split_beat must be strictly inside the clip
        if split_at <= old_start + 0.01 or split_at >= old_end - 0.01:
            return False

        self._push_undo()

        # Shrink original clip to [start, split)
        clip['end_beat'] = split_at

        # Create new clip for [split, end)
        new_id = self._next_clip_id
        self._next_clip_id += 1
        self.clips[channel].append({
            'id': new_id,
            'start_beat': split_at,
            'end_beat': old_end,
            'name': f'{clip.get("name", "Clip")} (split)',
            'created_at': time.time()
        })
        self._density_dirty = True
        self._emit_state_change()
        print(f"[DAW] split_clip ch={channel} id={clip_id} at={split_at:.3f} -> new_id={new_id}")
        return new_id

    def merge_clips(self, channel, clip_ids):
        """Merge multiple clips into one spanning their combined range.

        Events are NOT moved. The resulting clip covers min(start_beat)..max(end_beat).
        Original clips are removed and replaced by one new clip.
        """
        if self.is_recording or self.is_playing:
            return None
        if channel < 0 or channel > 15:
            return False

        try:
            id_set = set(int(cid) for cid in clip_ids)
        except (TypeError, ValueError):
            return False
        matching = [c for c in self.clips[channel] if int(c['id']) in id_set]
        if len(matching) < 2:
            return False

        min_start = min(float(c['start_beat']) for c in matching)
        max_end = max(float(c['end_beat']) for c in matching)

        self._push_undo()

        # Remove old clips
        self.clips[channel] = [c for c in self.clips[channel] if int(c['id']) not in id_set]

        # Create merged clip
        new_id = self._next_clip_id
        self._next_clip_id += 1
        self.clips[channel].append({
            'id': new_id,
            'start_beat': min_start,
            'end_beat': max_end,
            'name': f'Merged {new_id}',
            'created_at': time.time()
        })
        self._density_dirty = True
        self._emit_state_change()
        print(f"[DAW] merge_clips ch={channel} ids={list(id_set)} -> new_id={new_id} range=[{min_start:.3f}, {max_end:.3f}]")
        return new_id

    def set_bpm(self, bpm):
        """
        Imposta il tempo (BPM)
        Gli eventi sono salvati in beat, quindi cambiano velocità automaticamente
        """
        if self.is_recording or self.is_playing:
            return False
        
        self.bpm = max(30, min(300, bpm))
        self.beat_duration = 60.0 / self.bpm
        print(f"[DAW] BPM impostato a {self.bpm}")
        return True

    def set_time_signature(self, beats_per_measure):
        """Imposta il numeratore della misura (es. 3 o 4)."""
        if beats_per_measure not in (3, 4):
            return False
        self.beats_per_measure = beats_per_measure
        print(f"[DAW] Time signature impostato a {beats_per_measure}/4")
        return True
    
    def get_state(self):
        """Ritorna lo stato completo del sistema"""
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
            'clips': {ch: self.get_clips(ch) for ch in range(16)}
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
        result = []
        channels = range(16) if channel < 0 else [channel]

        for ch in channels:
            events = self.tracks.get(ch)
            if not events:
                continue
            # Build Note On → Note Off mapping for duration
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
        return result

    def edit_event(self, channel, index, updates):
        """
        Edit an event's beat position and/or velocity.
        Only allowed when not recording.
        Returns True on success.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events or index < 0 or index >= len(events):
            return False

        self._push_undo()
        beat_pos, msg_bytes = events[index]

        try:
            msg = mido.Message.from_bytes(msg_bytes)
        except Exception:
            return False

        if 'beat' in updates:
            beat_pos = float(updates['beat'])
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

        events[index] = (beat_pos, msg.bytes())

        # Re-sort if beat changed
        if 'beat' in updates:
            events.sort(key=lambda x: x[0])
            self._density_dirty = True

        self._emit_state_change()
        return True

    def delete_event(self, channel, index):
        """
        Delete an event by index.
        Only allowed when not recording.
        Returns True on success.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events or index < 0 or index >= len(events):
            return False

        self._push_undo()
        del events[index]
        if not events:
            self.has_data[channel] = False
        self._density_dirty = True
        self._emit_state_change()
        return True

    # ── Atomic note-pair operations (note = note_on + note_off) ──

    def add_note(self, channel, note, velocity, start_beat, length_beats):
        """
        Add a complete note (note_on + note_off) to a track.
        Inserts both events, sorts once. Returns True on success.
        """
        if self.is_recording:
            return False
        if channel < 0 or channel > 15:
            return False
        note = max(0, min(127, int(note)))
        velocity = max(1, min(127, int(velocity)))
        start_beat = max(0.0, float(start_beat))
        length_beats = max(0.001, float(length_beats))
        end_beat = start_beat + length_beats

        self._push_undo()
        events = self.tracks.setdefault(channel, [])
        msg_on = mido.Message('note_on', channel=channel, note=note, velocity=velocity)
        msg_off = mido.Message('note_off', channel=channel, note=note, velocity=0)
        events.append((start_beat, msg_on.bytes()))
        events.append((end_beat, msg_off.bytes()))
        events.sort(key=lambda x: x[0])
        self.has_data[channel] = True
        self._density_dirty = True
        self._emit_state_change()
        return True

    def edit_note_pair(self, channel, on_idx, off_idx, updates):
        """
        Atomically edit both note_on and note_off events for a single note.
        updates may contain: on_beat, off_beat, note, velocity.
        Sorts the events list once at the end. Returns True on success.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events:
            return False
        n_events = len(events)
        if on_idx < 0 or on_idx >= n_events:
            return False
        if off_idx >= 0 and off_idx >= n_events:
            return False

        self._push_undo()
        need_sort = False

        # Edit note_on event
        if on_idx >= 0:
            beat_pos, msg_bytes = events[on_idx]
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                return False
            if 'on_beat' in updates:
                beat_pos = float(updates['on_beat'])
                need_sort = True
            if 'note' in updates and hasattr(msg, 'note'):
                msg = msg.copy(note=max(0, min(127, int(updates['note']))))
            if 'velocity' in updates and hasattr(msg, 'velocity'):
                msg = msg.copy(velocity=max(0, min(127, int(updates['velocity']))))
            events[on_idx] = (beat_pos, msg.bytes())

        # Edit note_off event
        if off_idx >= 0:
            beat_pos, msg_bytes = events[off_idx]
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                return False
            if 'off_beat' in updates:
                beat_pos = float(updates['off_beat'])
                need_sort = True
            if 'note' in updates and hasattr(msg, 'note'):
                msg = msg.copy(note=max(0, min(127, int(updates['note']))))
            events[off_idx] = (beat_pos, msg.bytes())

        if need_sort:
            events.sort(key=lambda x: x[0])

        self._density_dirty = True
        self._emit_state_change()
        return True

    def delete_note_pair(self, channel, on_idx, off_idx):
        """
        Atomically delete both note_on and note_off events for a note.
        Handles index adjustment internally. Returns True on success.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events:
            return False

        # Collect valid indices, sort descending to delete from the end first
        indices = sorted(set(i for i in [on_idx, off_idx] if 0 <= i < len(events)), reverse=True)
        if not indices:
            return False

        self._push_undo()
        for idx in indices:
            del events[idx]
        if not events:
            self.has_data[channel] = False
        self._density_dirty = True
        self._emit_state_change()
        return True

    def edit_notes_batch(self, channel, edits):
        """
        Atomically edit multiple note pairs in a single operation.
        edits: list of dicts, each with on_idx, off_idx, and optional on_beat, off_beat, note, velocity.
        All changes are applied before sorting once.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events:
            return False
        n_events = len(events)

        self._push_undo()
        need_sort = False

        for edit in edits:
            on_idx = int(edit.get('on_idx', -1))
            off_idx = int(edit.get('off_idx', -1))

            # Edit note_on event
            if 0 <= on_idx < n_events:
                beat_pos, msg_bytes = events[on_idx]
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    continue
                if 'on_beat' in edit:
                    beat_pos = float(edit['on_beat'])
                    need_sort = True
                if 'note' in edit and hasattr(msg, 'note'):
                    msg = msg.copy(note=max(0, min(127, int(edit['note']))))
                if 'velocity' in edit and hasattr(msg, 'velocity'):
                    msg = msg.copy(velocity=max(0, min(127, int(edit['velocity']))))
                events[on_idx] = (beat_pos, msg.bytes())

            # Edit note_off event
            if 0 <= off_idx < n_events:
                beat_pos, msg_bytes = events[off_idx]
                try:
                    msg = mido.Message.from_bytes(msg_bytes)
                except Exception:
                    continue
                if 'off_beat' in edit:
                    beat_pos = float(edit['off_beat'])
                    need_sort = True
                if 'note' in edit and hasattr(msg, 'note'):
                    msg = msg.copy(note=max(0, min(127, int(edit['note']))))
                events[off_idx] = (beat_pos, msg.bytes())

        if need_sort:
            events.sort(key=lambda x: x[0])

        self._density_dirty = True
        self._emit_state_change()
        return True

    def delete_notes_batch(self, channel, pairs):
        """
        Atomically delete multiple note pairs in a single operation.
        pairs: list of dicts with on_idx and off_idx.
        All indices collected, sorted descending, deleted from end.
        """
        if self.is_recording:
            return False
        events = self.tracks.get(channel)
        if not events:
            return False
        n_events = len(events)

        indices = set()
        for pair in pairs:
            on_idx = int(pair.get('on_idx', -1))
            off_idx = int(pair.get('off_idx', -1))
            if 0 <= on_idx < n_events:
                indices.add(on_idx)
            if 0 <= off_idx < n_events:
                indices.add(off_idx)

        if not indices:
            return False

        self._push_undo()
        for idx in sorted(indices, reverse=True):
            del events[idx]
        if not events:
            self.has_data[channel] = False
        self._density_dirty = True
        self._emit_state_change()
        return True

    def get_track_activity(self, channel, start_beat=0.0, end_beat=9999.0):
        """Restituisce intervalli uniti di attività note per una traccia nel range richiesto."""
        events = self.tracks.get(channel)
        if not events:
            return []

        active_notes = {}
        intervals = []

        for beat_position, msg_bytes in events:
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                continue

            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = beat_position
            elif msg.type in ('note_off', 'note_on') and (msg.type == 'note_off' or msg.velocity == 0):
                if msg.note in active_notes:
                    start = active_notes.pop(msg.note)
                    intervals.append((start, beat_position))

        # Chiudi eventuali note rimaste aperte fino all'end richiesto
        for start in active_notes.values():
            intervals.append((start, end_beat))

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
        """Costruisce intervalli completi (start, end) in beat per tutte le note."""
        if not events:
            return [], 0.0

        active_notes = {}
        intervals = []
        max_beat = 0.0

        for beat_position, msg_bytes in events:
            max_beat = max(max_beat, beat_position)
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                continue

            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[msg.note] = beat_position
            elif msg.type in ('note_off', 'note_on') and (msg.type == 'note_off' or msg.velocity == 0):
                if msg.note in active_notes:
                    start = active_notes.pop(msg.note)
                    intervals.append((start, beat_position))

        for start in active_notes.values():
            intervals.append((start, max_beat))

        intervals.sort(key=lambda iv: iv[0])
        return intervals, max_beat

    def _build_notes_full(self, events):
        """Build note-level data for piano-roll rendering.
        Returns (notes_list, max_beat, min_note, max_note).
        notes_list: [(note, velocity, start_beat, end_beat, note_on_index, note_off_index), ...]
        """
        if not events:
            return [], 0.0, 127, 0

        active_notes = {}
        notes = []
        max_beat = 0.0
        min_note = 127
        max_note = 0

        for idx, (beat_position, msg_bytes) in enumerate(events):
            max_beat = max(max_beat, beat_position)
            try:
                msg = mido.Message.from_bytes(msg_bytes)
            except Exception:
                continue

            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes.setdefault(msg.note, []).append((beat_position, msg.velocity, idx))
            elif msg.type in ('note_off', 'note_on') and (msg.type == 'note_off' or msg.velocity == 0):
                if msg.note in active_notes and active_notes[msg.note]:
                    start, vel, on_idx = active_notes[msg.note].pop(0)
                    notes.append((msg.note, vel, start, beat_position, on_idx, idx))
                    min_note = min(min_note, msg.note)
                    max_note = max(max_note, msg.note)
                    if not active_notes[msg.note]:
                        del active_notes[msg.note]

        for note_num, pending in active_notes.items():
            for start, vel, on_idx in pending:
                notes.append((note_num, vel, start, max_beat, on_idx, -1))
                min_note = min(min_note, note_num)
                max_note = max(max_note, note_num)

        notes.sort(key=lambda n: n[2])
        return notes, max_beat, min_note, max_note

    def get_full_note_map(self):
        """Return per-channel note data for piano-roll rendering. Cached."""
        if not self.is_recording and not self._density_dirty and self._cached_note_map is not None:
            return self._cached_note_map

        beats_per_measure = self.beats_per_measure
        total_beats = 0.0
        tracks = {}

        for ch in range(16):
            notes, max_beat, min_note, max_note = self._build_notes_full(
                self.tracks.get(ch)
            )
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

        result = {
            'total_beats': total_beats,
            'beats_per_measure': beats_per_measure,
            'tracks': tracks
        }
        self._cached_note_map = result
        self._density_dirty = False
        return result

    def get_full_density_map(self):
        """Ritorna la mappa di densità (slot di 1/8 beat) per tutte le tracce. Cached."""
        # Bypass cache during recording — events are added continuously
        if not self.is_recording and not self._density_dirty and self._cached_density_map is not None:
            return self._cached_density_map

        slot = self.slot_beats
        beats_per_measure = self.beats_per_measure

        all_intervals = {}
        total_beats = 0.0
        for ch in range(16):
            intervals, max_beat = self._build_intervals_full(self.tracks.get(ch))
            all_intervals[ch] = intervals
            total_beats = max(total_beats, max_beat)

        if total_beats <= 0:
            total_beats = beats_per_measure * 4  # fallback 4 misure

        slots_count = max(1, math.ceil(total_beats / slot))
        density = {}

        for ch in range(16):
            arr = [0] * slots_count
            intervals = all_intervals[ch]
            for start, end in intervals:
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
        self._cached_density_map = result
        self._density_dirty = False
        return result
    
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
                self.tracks[ch].append((beat_position, data))
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
            channel_programs: dict {ch_int: {'bank': int, 'program': int}} dai settings correnti
        Ritorna i bytes del file MIDI.
        """
        mid = mido.MidiFile(type=1, ticks_per_beat=480)
        tempo = mido.bpm2tempo(self.bpm)
        if channel_programs is None:
            channel_programs = {}

        for ch in range(16):
            events = self.tracks.get(ch)
            if not events:
                continue

            track = mido.MidiTrack()
            # Tempo meta solo sulla prima traccia
            if len(mid.tracks) == 0:
                track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
                track.append(mido.MetaMessage('time_signature',
                                              numerator=self.beats_per_measure,
                                              denominator=4, time=0))
            track.append(mido.MetaMessage('track_name',
                                          name=f'Channel {ch + 1}', time=0))

            # Inserisci Bank Select + Program Change a tick 0
            ch_prog = channel_programs.get(ch, {})
            bank = ch_prog.get('bank', 0)
            prog = min(127, max(0, ch_prog.get('program', 0)))
            bank_msb = min(127, max(0, (bank >> 7) & 0x7F))
            bank_lsb = min(127, max(0, bank & 0x7F))
            track.append(mido.Message('control_change', channel=ch,
                                      control=0, value=bank_msb, time=0))
            track.append(mido.Message('control_change', channel=ch,
                                      control=32, value=bank_lsb, time=0))
            track.append(mido.Message('program_change', channel=ch,
                                      program=prog, time=0))

            sorted_events = sorted(events, key=lambda e: e[0])
            last_tick = 0
            for beat_pos, msg_bytes in sorted_events:
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
                last_tick = abs_tick

            track.append(mido.MetaMessage('end_of_track', time=0))
            mid.tracks.append(track)

        buf = io.BytesIO()
        mid.save(file=buf)
        return buf.getvalue()

    def import_midi(self, midi_bytes, merge=False):
        """
        Importa un file MIDI nelle tracce del DAW.

        Args:
            midi_bytes: contenuto binario del file MIDI
            merge: se True, aggiunge agli eventi esistenti; se False, sostituisce
        Returns:
            dict con info su tracce importate
        """
        if self.is_recording or self.is_playing:
            raise RuntimeError('Cannot import while recording or playing')

        buf = io.BytesIO(midi_bytes)
        mid = mido.MidiFile(file=buf)

        # Leggi tempo dal file
        imported_bpm = self.bpm
        imported_ts = self.beats_per_measure
        for track in mid.tracks:
            for msg in track:
                if msg.type == 'set_tempo':
                    imported_bpm = int(round(mido.tempo2bpm(msg.tempo)))
                elif msg.type == 'time_signature':
                    if msg.numerator in (3, 4):
                        imported_ts = msg.numerator

        ticks_per_beat = mid.ticks_per_beat or 480

        if not merge:
            self.clear_all_tracks()

        self.bpm = max(30, min(300, imported_bpm))
        self.beat_duration = 60.0 / self.bpm
        self.beats_per_measure = imported_ts

        imported_channels = set()
        channel_programs = {}  # {ch: {'bank': int, 'program': int}}
        bank_msb = {}  # temporaneo per Bank Select MSB per canale

        for track in mid.tracks:
            abs_tick = 0
            for msg in track:
                abs_tick += msg.time
                if msg.is_meta:
                    continue
                if not hasattr(msg, 'channel'):
                    continue
                ch = msg.channel

                # Cattura Bank Select MSB (CC 0)
                if msg.type == 'control_change' and msg.control == 0:
                    bank_msb[ch] = msg.value
                    continue
                # Ignora Bank Select LSB (CC 32)
                if msg.type == 'control_change' and msg.control == 32:
                    continue
                # Cattura Program Change
                if msg.type == 'program_change':
                    channel_programs[ch] = {
                        'bank': bank_msb.get(ch, 0),
                        'program': msg.program
                    }
                    continue

                beat_pos = abs_tick / ticks_per_beat
                self.tracks[ch].append((beat_pos, msg.bytes()))
                self.has_data[ch] = True
                imported_channels.add(ch)

        # Riordina le tracce per beat position
        for ch in imported_channels:
            self.tracks[ch].sort(key=lambda x: x[0])

        self._density_dirty = True
        self._emit_state_change()
        print(f'[DAW] MIDI importato: BPM={self.bpm}, canali={sorted(imported_channels)}, programmi={channel_programs}')
        return {
            'bpm': self.bpm,
            'beats_per_measure': self.beats_per_measure,
            'channels': sorted(imported_channels),
            'channel_programs': channel_programs,
            'total_events': sum(len(self.tracks[ch]) for ch in imported_channels)
        }
