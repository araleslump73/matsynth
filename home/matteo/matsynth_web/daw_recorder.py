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
    
    def __init__(self, bpm=120):
        """
        Inizializza il sistema DAW
        
        Args:
            bpm: Beats per minute (default 120)
        """
        self.bpm = bpm
        self.beat_duration = 60.0 / bpm  # durata di un beat in secondi
        
        # Storage tracce: {channel_id: [(timestamp, midi_msg), ...]}
        self.tracks = defaultdict(list)
        
        # Stato di ogni traccia (16 canali MIDI)
        self.armed = {i: False for i in range(16)}      # traccia pronta per registrazione
        self.muted = {i: False for i in range(16)}      # traccia mutata
        self.has_data = {i: False for i in range(16)}   # traccia ha eventi registrati
        
        # Stato del trasporto
        self.is_recording = False
        self.is_playing = False
        self.timeline_position = 0.0  # posizione corrente in secondi
        
        # Thread control
        self.record_thread = None
        self.playback_thread = None
        self.stop_event = threading.Event()
        
        # Porte MIDI
        self.midi_input = None
        self.midi_output = None
        self._init_midi_ports()
    
    def _init_midi_ports(self):
        """Inizializza le porte MIDI virtuali e fisiche"""
        try:
            # Trova la tastiera fisica (SINCO o simile)
            available_inputs = mido.get_input_names()
            physical_keyboard = None
            
            for port_name in available_inputs:
                if 'SINCO' in port_name or 'USB' in port_name or 'Midi Through' not in port_name:
                    physical_keyboard = port_name
                    break
            
            if physical_keyboard:
                self.midi_input = mido.open_input(physical_keyboard)
                print(f"[DAW] MIDI Input connesso: {physical_keyboard}")
            else:
                print("[DAW] ATTENZIONE: Nessuna tastiera MIDI fisica trovata")
            
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
    
    def start_recording(self, channel):
        """
        Avvia la registrazione su un canale specifico
        
        Args:
            channel: Canale MIDI (0-15) da registrare
            
        Returns:
            bool: True se registrazione avviata, False altrimenti
        """
        if not self.armed[channel]:
            return False
        
        if self.is_recording:
            return False
        
        # Se ci sono altre tracce con dati, avvia anche la riproduzione
        has_other_tracks = any(self.has_data[ch] for ch in range(16) if ch != channel)
        
        self.is_recording = True
        self.stop_event.clear()
        
        # Reset timeline position all'inizio
        self.timeline_position = 0.0
        
        # Thread di registrazione
        self.record_thread = threading.Thread(
            target=self._record_loop, 
            args=(channel,),
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
        
        print(f"[DAW] Registrazione avviata su canale {channel}")
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
        self.timeline_position = 0.0
        
        self.playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True
        )
        self.playback_thread.start()
        
        print("[DAW] Playback avviato")
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
        return True
    
    def stop_all(self):
        """Ferma registrazione e riproduzione"""
        self.stop_recording()
        self.stop_playback()
        self._send_all_notes_off()
        return True
    
    def rewind(self):
        """Riporta la timeline a 00:00:00"""
        was_playing = self.is_playing
        was_recording = self.is_recording
        
        # Ferma tutto
        self.stop_all()
        
        # Reset timeline
        self.timeline_position = 0.0
        
        # Riavvia se necessario
        if was_playing:
            self.start_playback()
        
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
        return True
    
    def mute_track(self, channel, muted=True):
        """
        Muta/smuta una traccia
        
        Args:
            channel: Canale MIDI (0-15)
            muted: True per mutare, False per smutare
        """
        self.muted[channel] = muted
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
        Può essere cambiato solo quando il sistema è fermo
        """
        if self.is_recording or self.is_playing:
            return False
        
        self.bpm = max(30, min(300, bpm))
        self.beat_duration = 60.0 / self.bpm
        print(f"[DAW] BPM impostato a {self.bpm}")
        return True
    
    def get_state(self):
        """Ritorna lo stato completo del sistema"""
        return {
            'is_recording': self.is_recording,
            'is_playing': self.is_playing,
            'timeline_position': self.timeline_position,
            'bpm': self.bpm,
            'armed': self.armed.copy(),
            'muted': self.muted.copy(),
            'has_data': self.has_data.copy(),
            'track_counts': {ch: len(self.tracks[ch]) for ch in range(16)}
        }
    
    def _record_loop(self, channel):
        """Loop di registrazione (eseguito in thread separato)"""
        if not self.midi_input:
            print("[DAW] Errore: Nessun input MIDI disponibile")
            return
        
        start_time = time.time()
        
        try:
            while not self.stop_event.is_set():
                # Polling veloce con timeout minimo
                for msg in self.midi_input.iter_pending():
                    # Filtra solo messaggi del canale corretto
                    if hasattr(msg, 'channel') and msg.channel == channel:
                        timestamp = time.time() - start_time
                        
                        # Salva evento in formato tuple leggera
                        self.tracks[channel].append((timestamp, msg.bytes()))
                        
                        # Invia a FluidSynth per monitoring real-time
                        if self.midi_output:
                            self.midi_output.send(msg)
                
                # Sleep minimo per non sovraccaricare CPU
                time.sleep(0.001)  # 1ms polling
            
            # Marca la traccia come avente dati
            if len(self.tracks[channel]) > 0:
                self.has_data[channel] = True
                print(f"[DAW] Registrati {len(self.tracks[channel])} eventi su canale {channel}")
                
        except Exception as e:
            print(f"[DAW] Errore nel loop di registrazione: {e}")
    
    def _playback_loop(self):
        """Loop di riproduzione (eseguito in thread separato)"""
        if not self.midi_output:
            print("[DAW] Errore: Nessun output MIDI disponibile")
            return
        
        start_time = time.time()
        
        # Prepara gli eventi di tutte le tracce non mutate
        all_events = []
        for channel in range(16):
            if self.has_data[channel] and not self.muted[channel]:
                for timestamp, msg_bytes in self.tracks[channel]:
                    # Ricostruisci il messaggio MIDI
                    all_events.append((timestamp, channel, msg_bytes))
        
        # Ordina gli eventi per timestamp
        all_events.sort(key=lambda x: x[0])
        
        print(f"[DAW] Riproduzione di {len(all_events)} eventi totali")
        
        try:
            event_index = 0
            while not self.stop_event.is_set() and event_index < len(all_events):
                current_time = time.time() - start_time
                
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
                msg = mido.Message('control_change', channel=channel, control=123, value=0)
                self.midi_output.send(msg)
        except Exception as e:
            print(f"[DAW] Errore invio All Notes Off: {e}")
    
    def __del__(self):
        """Cleanup alla chiusura"""
        self.stop_all()
        
        if self.midi_input:
            self.midi_input.close()
        
        if self.midi_output:
            self.midi_output.close()
