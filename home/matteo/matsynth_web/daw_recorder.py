"""
Micro-DAW Recorder Module
Ottimizzato per Raspberry Pi Zero 2W con bassa latenza e CPU overhead minimo
"""

import mido
import time
import threading
from collections import defaultdict


class MultiTrackDAW:
    """
    Gestisce registrazione e riproduzione multi-traccia MIDI
    con sincronizzazione automatica e routing intelligente
    """
    
    def __init__(self, bpm=120, socketio=None):
        """
        Inizializza il sistema DAW
        
        Args:
            bpm: Beats per minute (default 120)
            socketio: Flask-SocketIO instance per eventi real-time (optional)
        """
        self.bpm = bpm
        self.beat_duration = 60.0 / bpm  # durata di un beat in secondi
        self.socketio = socketio  # WebSocket per notifiche real-time
        
        # Storage tracce: {channel_id: [(beat_position, midi_msg), ...]}
        # Gli eventi sono salvati in beat (unità musicale) per supportare cambi di BPM
        self.tracks = defaultdict(list)
        
        # Stato di ogni traccia (16 canali MIDI)
        self.armed = {i: False for i in range(16)}      # traccia pronta per registrazione
        self.muted = {i: False for i in range(16)}      # traccia mutata
        self.has_data = {i: False for i in range(16)}   # traccia ha eventi registrati
        
        # Stato del trasporto
        self.is_recording = False
        self.is_playing = False
        self.timeline_position = 0.0  # posizione corrente in secondi
        
        # Metronomo
        self.metronome_enabled = False
        self.metronome_thread = None
        self.last_beat = -1  # ultimo beat suonato (per evitare duplicati)
        
        # Thread control
        self.record_thread = None
        self.playback_thread = None
        self.update_thread = None  # Thread per aggiornamenti periodici via WebSocket
        self.stop_event = threading.Event()
        self.metronome_stop_event = threading.Event()
        
        # Porte MIDI
        self.midi_input = None
        self.midi_output = None
        self._init_midi_ports()
        
        # Avvia thread per aggiornamenti periodici WebSocket
        self._start_update_thread()
    
    def _init_midi_ports(self):
        """Inizializza le porte MIDI virtuali e fisiche"""
        try:
            # Trova la tastiera fisica (FANTOM, SINCO, USB, etc.)
            available_inputs = mido.get_input_names()
            physical_keyboard = None
            
            # Priorità: FANTOM > SINCO > USB > altro (escludi Midi Through)
            keywords = ['FANTOM', 'SINCO', 'USB']
            for keyword in keywords:
                for port_name in available_inputs:
                    if keyword in port_name.upper() and 'MIDI THROUGH' not in port_name.upper():
                        physical_keyboard = port_name
                        break
                if physical_keyboard:
                    break
            
            if physical_keyboard:
                self.midi_input = mido.open_input(physical_keyboard)
                print(f"[DAW] MIDI Input connesso: {physical_keyboard}")
            else:
                print("[DAW] ATTENZIONE: Nessuna tastiera MIDI fisica trovata")
                print(f"[DAW] Porte disponibili: {available_inputs}")
            
            # Trova FluidSynth come output
            available_outputs = mido.get_output_names()
            fluidsynth_port = None
            
            for port_name in available_outputs:
                if 'FLUID' in port_name or '128:0' in port_name:
                    fluidsynth_port = port_name
                    break
            
            if fluidsynth_port:
                self.midi_output = mido.open_output(fluidsynth_port)
                print(f"[DAW] MIDI Output connesso: {fluidsynth_port}")
            else:
                print("[DAW] ATTENZIONE: FluidSynth non trovato come output MIDI")
                
        except Exception as e:
            print(f"[DAW] Errore inizializzazione MIDI: {e}")
    
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
                # Emetti aggiornamento solo se registrazione o playback attivi
                if self.is_recording or self.is_playing:
                    self._emit_state_change()
                    time.sleep(0.5)  # Aggiorna ogni 500ms durante attività
                else:
                    time.sleep(1.0)  # Check meno frequente quando idle
            except Exception as e:
                print(f"[DAW] Errore in update loop: {e}")
                time.sleep(1.0)
    
    def start_recording(self, channel=None):
        """
        Avvia la registrazione su canali armati
        
        Args:
            channel: Canale MIDI (0-15) da registrare, o None per tutti i canali armati
            
        Returns:
            bool: True se registrazione avviata, False altrimenti
        """
        print(f"[DAW] start_recording chiamato con channel={channel}")
        print(f"[DAW] armed state: {self.armed}")
        print(f"[DAW] is_recording: {self.is_recording}")
        
        # Se channel specificato, verifica che sia armato
        if channel is not None:
            if not self.armed[channel]:
                print(f"[DAW] Canale {channel} non armato, esco")
                return False
        else:
            # Se nessun channel specificato, verifica che almeno uno sia armato
            if not any(self.armed.values()):
                print(f"[DAW] Nessun canale armato, esco")
                return False
        
        if self.is_recording:
            print(f"[DAW] Già in registrazione, esco")
            return False
        
        # FLUSH della coda MIDI per evitare messaggi accumulati
        if self.midi_input:
            # Svuota tutti i messaggi in attesa prima di iniziare
            discarded_count = 0
            for _ in self.midi_input.iter_pending():
                discarded_count += 1
            if discarded_count > 0:
                print(f"[DAW] Scartati {discarded_count} messaggi MIDI in buffer prima di registrare")
        
        # Invia "All Notes Off" per sicurezza (pulisce eventuali note stuck)
        self._send_all_notes_off()
        
        # Breve pausa per stabilizzare il sistema
        time.sleep(0.05)  # 50ms
        
        # Determina i canali da registrare
        if channel is not None:
            recording_channels = [channel]
        else:
            recording_channels = [ch for ch in range(16) if self.armed[ch]]
        
        # Se ci sono altre tracce con dati (non in registrazione), avvia anche la riproduzione
        has_other_tracks = any(self.has_data[ch] for ch in range(16) if ch not in recording_channels)
        
        self.is_recording = True
        self.stop_event.clear()
        
        # NON resettare timeline position - registra dalla posizione corrente
        # (solo REWIND resetta a 0.0)
        initial_position = self.timeline_position
        
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
        
        self.is_recording = False
        self.stop_event.set()
        
        if self.record_thread:
            self.record_thread.join(timeout=1.0)
        
        print("[DAW] Registrazione fermata")
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def start_playback(self):
        """Avvia la riproduzione di tutte le tracce registrate"""
        if self.is_playing or self.is_recording:
            return False
        
        # Verifica che ci siano tracce da riprodurre
        if not any(self.has_data.values()):
            return False
        
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
    
    def rewind(self):
        """Riporta la timeline a 00:00:00"""
        # Ferma tutto
        self.stop_all()
        
        # Reset timeline
        self.timeline_position = 0.0
        self._emit_state_change()  # Notifica via WebSocket
        
        return True
        
        print("[DAW] Timeline resettata")
        return True
    
    def arm_track(self, channel, armed=True):
        """
        Arma/disarma una traccia per la registrazione
        
        Args:
            channel: Canale MIDI (0-15)
            armed: True per armare, False per disarmare
        """
        self.armed[channel] = armed
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
        self._emit_state_change()  # Notifica via WebSocket
        return True
    
    def clear_track(self, channel):
        """Cancella tutti gli eventi di una traccia"""
        self.tracks[channel] = []
        self.has_data[channel] = False
        print(f"[DAW] Traccia {channel} cancellata")
        return True
    
    def clear_all_tracks(self):
        """Cancella tutte le tracce"""
        for ch in range(16):
            self.clear_track(ch)
        print("[DAW] Tutte le tracce cancellate")
        return True
    
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
            'armed': self.armed.copy(),
            'muted': self.muted.copy(),
            'has_data': self.has_data.copy(),
            'track_counts': {ch: len(self.tracks[ch]) for ch in range(16)},
            'track_durations': track_durations,  # Durata di ogni traccia in secondi
            'max_duration': max_duration  # Durata massima tra tutte le tracce
        }
    
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
        """Loop del metronomo (eseguito in thread separato)"""
        if not self.midi_output:
            return
        
        CLICK_CHANNEL = 9  # Canale 10 (drum channel, 0-indexed)
        ACCENT_NOTE = 76   # High Wood Block (primo beat della misura)
        NORMAL_NOTE = 77   # Low Wood Block (altri beat)
        VELOCITY = 100
        NOTE_DURATION = 0.05  # 50ms
        
        beats_per_measure = 4  # 4/4 time
        
        try:
            while not self.metronome_stop_event.is_set():
                # Calcola il beat corrente dalla timeline position
                current_beat = int(self.timeline_position / self.beat_duration)
                
                # Se è un nuovo beat, suona il click
                if current_beat != self.last_beat:
                    self.last_beat = current_beat
                    
                    # Primo beat della misura = accento
                    beat_in_measure = current_beat % beats_per_measure
                    note = ACCENT_NOTE if beat_in_measure == 0 else NORMAL_NOTE
                    
                    # Note ON
                    msg_on = mido.Message('note_on', 
                                         channel=CLICK_CHANNEL, 
                                         note=note, 
                                         velocity=VELOCITY)
                    self.midi_output.send(msg_on)
                    
                    # Breve durata
                    time.sleep(NOTE_DURATION)
                    
                    # Note OFF
                    msg_off = mido.Message('note_off', 
                                          channel=CLICK_CHANNEL, 
                                          note=note, 
                                          velocity=0)
                    self.midi_output.send(msg_off)
                
                # Sleep fino al prossimo possibile beat (con margine)
                time_to_next_beat = self.beat_duration - (self.timeline_position % self.beat_duration)
                time.sleep(min(0.01, time_to_next_beat * 0.5))
                
        except Exception as e:
            print(f"[DAW] Errore nel metronomo: {e}")
    
    def _record_loop(self, recording_channels, initial_position=0.0):
        """Loop di registrazione (eseguito in thread separato)
        
        Args:
            recording_channels: Lista di canali su cui registrare (es. [0, 1, 2])
            initial_position: Posizione iniziale della timeline (offset temporale)
        """
        if not self.midi_input:
            print("[DAW] Errore: Nessun input MIDI disponibile")
            return
        
        start_time = time.time()
        GRACE_PERIOD = 0.01  # Ignora i primi 10ms per evitare messaggi residui
        
        try:
            while not self.stop_event.is_set():
                # Calcola posizione corrente sulla timeline
                current_time = initial_position + (time.time() - start_time)
                
                # Aggiorna sempre la timeline position (scorre continuamente)
                self.timeline_position = current_time
                
                # Polling veloce con timeout minimo
                for msg in self.midi_input.iter_pending():
                    # Filtra solo messaggi dei canali in registrazione
                    if hasattr(msg, 'channel') and msg.channel in recording_channels:
                        elapsed_time = time.time() - start_time
                        
                        # Ignora messaggi nei primi millisecondi (grace period)
                        # per evitare di registrare eventi residui nel buffer
                        if elapsed_time < GRACE_PERIOD:
                            continue
                        
                        # Timestamp assoluto sulla timeline in secondi
                        timestamp_seconds = initial_position + elapsed_time
                        
                        # Converti in beat (posizione musicale) per supportare cambi di BPM
                        beat_position = timestamp_seconds / self.beat_duration
                        
                        # Salva evento in formato tuple leggera (beat, msg_bytes)
                        self.tracks[msg.channel].append((beat_position, msg.bytes()))
                        
                        # NON fare thru a FluidSynth - è già collegato direttamente alla tastiera
                        # Il monitoring real-time avviene tramite la connessione diretta aconnect
                
                # Sleep minimo per non sovraccaricare CPU
                time.sleep(0.001)  # 1ms polling
            
            # Marca le tracce come aventi dati e riordina gli eventi per beat position
            for channel in recording_channels:
                if len(self.tracks[channel]) > 0:
                    # Riordina gli eventi per beat position (potrebbero essere stati aggiunti in ordine sparso)
                    self.tracks[channel].sort(key=lambda x: x[0])
                    self.has_data[channel] = True
                    print(f"[DAW] Registrati {len(self.tracks[channel])} eventi su canale {channel}")
                
        except Exception as e:
            print(f"[DAW] Errore nel loop di registrazione: {e}")
    
    def _playback_loop(self):
        """Loop di riproduzione (eseguito in thread separato)"""
        if not self.midi_output:
            print("[DAW] Errore: Nessun output MIDI disponibile")
            return
        
        # Salva la posizione iniziale (da dove riprendere)
        initial_position = self.timeline_position
        start_time = time.time()
        
        # Prepara gli eventi di tutte le tracce non mutate
        # Converti beat position in secondi usando BPM corrente
        all_events = []
        for channel in range(16):
            if self.has_data[channel] and not self.muted[channel]:
                for beat_position, msg_bytes in self.tracks[channel]:
                    # Converti beat in secondi con BPM corrente
                    timestamp_seconds = beat_position * self.beat_duration
                    all_events.append((timestamp_seconds, channel, msg_bytes))
        
        # Ordina gli eventi per timestamp in secondi
        all_events.sort(key=lambda x: x[0])
        
        # Salta gli eventi già passati (prima della posizione corrente)
        event_index = 0
        while event_index < len(all_events) and all_events[event_index][0] < initial_position:
            event_index += 1
        
        print(f"[DAW] Riproduzione di {len(all_events) - event_index} eventi da {initial_position:.1f}s")
        
        try:
            while not self.stop_event.is_set() and event_index < len(all_events):
                current_time = initial_position + (time.time() - start_time)
                
                # Aggiorna timeline position
                self.timeline_position = current_time
                
                # Invia tutti gli eventi che dovrebbero essere già suonati
                while event_index < len(all_events):
                    timestamp, channel, msg_bytes = all_events[event_index]
                    
                    if timestamp <= current_time:
                        # Ricostruisci e invia il messaggio
                        try:
                            msg = mido.Message.from_bytes(msg_bytes)
                            self.midi_output.send(msg)
                        except:
                            pass
                        
                        event_index += 1
                    else:
                        break
                
                # Sleep fino al prossimo evento o polling minimo
                if event_index < len(all_events):
                    next_timestamp = all_events[event_index][0]
                    sleep_time = min(0.001, max(0, next_timestamp - current_time))
                    time.sleep(sleep_time)
                else:
                    time.sleep(0.001)
            
            # Loop completato
            if not self.is_recording:
                self.is_playing = False
                self._send_all_notes_off()
                print("[DAW] Riproduzione completata")
                
        except Exception as e:
            print(f"[DAW] Errore nel loop di riproduzione: {e}")
    
    def _send_all_notes_off(self):
        """Invia All Notes Off su tutti i canali"""
        if not self.midi_output:
            return
        
        try:
            for channel in range(16):
                # Control Change 123 (All Notes Off)
                try:
                    msg = mido.Message('control_change', channel=channel, control=123, value=0)
                    self.midi_output.send(msg)
                except TypeError:
                    # Alcune porte MIDI potrebbero non supportare send() diretto
                    pass
        except Exception as e:
            print(f"[DAW] Errore invio All Notes Off: {e}")
    
    def __del__(self):
        """Cleanup alla chiusura"""
        self.stop_all()
        
        if self.midi_input:
            self.midi_input.close()
        
        if self.midi_output:
            self.midi_output.close()
