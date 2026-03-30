from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import socket
import os
import io
import json
import time
import subprocess
import threading
from daw_recorder import MultiTrackDAW

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Configurazione
SF2_DIR = "/usr/share/sounds/sf2/"
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'
PRESETS_DIR = '/home/matteo/matsynth_web/presets/'  # Directory per i preset salvati
MIDI_DIR = '/home/matteo/matsynth_web/midi/'  # Directory per i file MIDI salvati
sf_id = 1 # Global variable to track the active soundfont ID
STATE_LOCK = threading.Lock()
_INITIALIZED = False

# Debounced state writer — batches rapid CC writes to reduce SD card I/O on Pi Zero
_state_dirty = False
_state_pending = {}       # pending flat key→value updates
_cc_pending = {}          # pending CC updates: (chan_str, cc_name) → value
_state_flush_lock = threading.Lock()

def _schedule_state_flush():
    """Flush pending state writes after a short delay (debounce)."""
    global _state_dirty, _state_pending, _cc_pending
    time.sleep(0.5)  # 500ms debounce
    with _state_flush_lock:
        if not _state_pending and not _cc_pending:
            return
        pending = _state_pending.copy()
        cc_pending = _cc_pending.copy()
        _state_pending.clear()
        _cc_pending.clear()
        _state_dirty = False
    try:
        with STATE_LOCK:
            state = get_last_state()
            state.update(pending)
            if cc_pending:
                if 'channels' not in state:
                    state['channels'] = {}
                for (chan_str, cc_name), val in cc_pending.items():
                    if chan_str not in state['channels']:
                        state['channels'][chan_str] = {}
                    state['channels'][chan_str][cc_name] = val
            _write_state_atomic(state)
    except Exception as e:
        print(f"[State] Flush error: {e}")

def save_state_debounced(key, value):
    """Queue a flat state update; actual write is debounced to reduce I/O."""
    global _state_dirty
    with _state_flush_lock:
        _state_pending[key] = value
        should_start = not _state_dirty
        _state_dirty = True
    if should_start:
        threading.Thread(target=_schedule_state_flush, daemon=True).start()

def save_cc_debounced(chan, cc_name, val):
    """Queue a CC state update (nested under channels); debounced."""
    global _state_dirty
    with _state_flush_lock:
        _cc_pending[(str(chan), cc_name)] = val
        should_start = not _state_dirty
        _state_dirty = True
    if should_start:
        threading.Thread(target=_schedule_state_flush, daemon=True).start()

# Crea la directory dei preset se non esiste
if not os.path.exists(PRESETS_DIR):
    os.makedirs(PRESETS_DIR)

# Crea la directory MIDI se non esiste
if not os.path.exists(MIDI_DIR):
    os.makedirs(MIDI_DIR)

# Inizializza il sistema DAW Multi-Track
daw = MultiTrackDAW(bpm=120, socketio=socketio)

def _write_state_atomic(state: dict):
    """Scrive lo stato in modo atomico per evitare file troncati su scritture concorrenti."""
    tmp_path = STATE_FILE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_FILE)

def save_state(key, value):
    try:
        with STATE_LOCK:
            state = get_last_state()
            state[key] = value
            _write_state_atomic(state)
    except Exception as e:
        print(f"[State] Save error: {e}")

def get_last_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            # Se il file è corrotto, rinomina e riparti dai default
            try:
                corrupted_path = STATE_FILE + f".corrupt_{int(time.time())}"
                os.rename(STATE_FILE, corrupted_path)
                print(f"File stato corrotto, backup in {corrupted_path}: {e}")
            except Exception as e2:
                print(f"Impossibile rinominare file stato corrotto: {e2}")
    return {"gain": 1.0, "reverb.level": 0.4, "chorus.level": 0.4, "font": "GeneralUser-GS.sf2"}

def send_fluid(command):
    try:
        # Timeout breve e connessione rapida
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect((FLUID_HOST, FLUID_PORT))
            sock.sendall(f"{command}\n".encode())
            
            # Se è un comando di set/select, usciamo subito
            if command.startswith(("set", "select", "cc", "unload", "load")):
                # Per load e unload leggiamo comunque la risposta per sicurezza o debug
                if command.startswith(("load", "unload")):
                    time.sleep(0.1)
                    return sock.recv(4096).decode()
                return "OK"

            # Read commands (inst, fonts, channels)
            time.sleep(0.2)
            data = ""
            while True:
                try:
                    chunk = sock.recv(4096).decode()
                    if not chunk: break
                    data += chunk
                except socket.timeout:
                    break
                except OSError:
                    break
            return data
    except Exception as e:
        print(f"[FluidSynth] Socket error: {e}")
        return ""

def get_active_sf_id():
    """Trova l'ID del soundfont attualmente caricato (es. 4, 5, ecc.)"""
    raw = send_fluid("fonts")
    # Output tipico:
    # ID  Name
    #  4  /percorso/file.sf2
    lines = raw.split('\n')
    for line in lines:
        parts = line.strip().split()
        # Se la riga inizia con un numero, quello è l'ID
        if parts and parts[0].isdigit():
            return parts[0]
    return "1" # Fallback nel caso peggiore

def restore_settings():
    state = get_last_state()
    gain = state.get('gain', 0.7)
    rev = state.get('reverb.level', 0.4)
    cho = state.get('chorus.level', 0.4)
    
    print(f"[Init] Restore: Gain {gain}, Rev {rev}, Chorus {cho}")
    send_fluid(f"set synth.gain {gain}")
    send_fluid(f"set synth.reverb.level {rev}")
    send_fluid(f"set synth.chorus.level {cho}")

    # Se abbiamo un font salvato, prova a caricarlo
    font_name = state.get('font')
    if font_name:
        path = os.path.join(SF2_DIR, font_name)
        if os.path.exists(path):
            # scarica tutti i font correnti e carica quello salvato
            raw_fonts = send_fluid("fonts")
            for line in raw_fonts.split('\n'):
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    send_fluid(f"unload {parts[0]}")
            res = send_fluid(f"load {path}")
            print(f"[Init] Restored font {font_name}: {res}")
            # Aggiorna l'ID attivo del font
            global sf_id
            sf_id = get_active_sf_id()
        else:
            print(f"[Init] Saved font not found: {path}")

    # Applica banche, programmi e CC salvati per ogni canale
    saved_channels = state.get('channels', {})
    for ch_str, ch_data in saved_channels.items():
        try:
            ch = int(ch_str)
        except ValueError:
            continue
        bank = ch_data.get('bank')
        prog = ch_data.get('program')
        if bank is not None and prog is not None:
            send_fluid(f"select {ch} {sf_id} {bank} {prog}")
        # Applica CC se presenti
        cc_map = {
            'volume': ('7', ch_data.get('volume')),
            'pan': ('10', ch_data.get('pan')),
            'attack': ('73', ch_data.get('attack')),
            'release': ('72', ch_data.get('release')),
            'decay': ('75', ch_data.get('decay')),
            'cutoff': ('74', ch_data.get('cutoff')),
            'resonance': ('71', ch_data.get('resonance')),
        }
        for _, cc_info in cc_map.items():
            cc_num, cc_val = cc_info
            if cc_val is not None:
                send_fluid(f"cc {ch} {cc_num} {cc_val}")


def startup_init_once():
    global _INITIALIZED, sf_id
    if _INITIALIZED:
        return
    _INITIALIZED = True
    # Wait for FluidSynth with progressive retry
    for attempt in range(5):
        test = send_fluid("fonts")
        if test.strip():
            print(f"[Init] FluidSynth ready after {attempt + 1} attempt(s)")
            break
        wait = 1.0 + attempt * 0.5
        print(f"[Init] FluidSynth not ready, retrying in {wait:.1f}s...")
        time.sleep(wait)
    restore_settings()
    # Aggiorna l'ID attivo del font dopo il ripristino
    sf_id = get_active_sf_id()
    print(f"[Init] Active soundfont ID: {sf_id}")

@app.route('/')
def index():
    files = [f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3'))] if os.path.exists(SF2_DIR) else []
    return render_template('index.html', files=files)

@app.route('/settings')
def settings():
    return render_template('settings.html')

@app.route('/list_sf2')
def list_sf2():
    if not os.path.exists(SF2_DIR): return jsonify([])
    files = [f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3'))]
    return jsonify(files)

@app.route('/load_sf2/<filename>')
def load_sf2(filename):
    global sf_id
    path = os.path.join(SF2_DIR, filename)
    
    # 1. Troviamo TUTTI gli ID caricati attualmente
    raw_fonts = send_fluid("fonts")
    lines = raw_fonts.split('\n')
    for line in lines:
        parts = line.strip().split()
        if parts and parts[0].isdigit():
            # Scarichiamo ogni ID trovato (che sia 1, 4 o 100)
            send_fluid(f"unload {parts[0]}")
    
    # 2. Carichiamo il nuovo file (che prenderà un nuovo ID incrementale)
    res = send_fluid(f"load {path}")
    sf_id = get_active_sf_id()
    
    save_state('font', filename)
    return f"Caricato: {res}"

@app.route('/get_instruments')
def get_instruments():
    
    # 2. Chiediamo gli strumenti DI QUEL ID SPECIFICO
    data = send_fluid(f"inst {sf_id}")
    
    lines = data.split('\n')
    instruments = []
    for line in lines:
        if "-" in line and any(char.isdigit() for char in line):
            instruments.append(line.strip())
    return jsonify(instruments)

@app.route('/select_prog/<int:chan>/<int:bank>/<int:prog>')
def select_prog(chan, bank, prog):
    # Usa l'ID già caricato in memoria (molto più veloce!)
    comando = f"select {chan} {sf_id} {bank} {prog}"
    send_fluid(comando)
    
    # Salva lo stato del canale per il preset
    with STATE_LOCK:
        state = get_last_state()
        if 'channels' not in state:
            state['channels'] = {}
        
        # Preserva i dati esistenti del canale (CC, ecc.)
        if str(chan) not in state['channels']:
            state['channels'][str(chan)] = {}
        
        state['channels'][str(chan)]['bank'] = bank
        state['channels'][str(chan)]['program'] = prog
        
        print(f"[Channel] Saved ch {chan}: bank={bank}, prog={prog}")
        _write_state_atomic(state)
    
    return "OK"

@app.route('/set_effect/<type>/<val>')
def set_effect(type, val):
    try:
        f_val = float(val)
        send_fluid(f"set synth.{type} {f_val}")
        save_state(type, f_val)
        return "OK"
    except (ValueError, TypeError) as e:
        print(f"[Effect] Invalid value type={type} val={val}: {e}")
        return "Error", 400

@app.route('/cc/<int:chan>/<int:cc>/<int:val>')
def control(chan, cc, val):
    send_fluid(f"cc {chan} {cc} {val}")
    
    # Save important CCs for presets (debounced to reduce SD card I/O)
    cc_names = {7: 'volume', 10: 'pan', 71: 'resonance', 72: 'release',
                73: 'attack', 74: 'cutoff', 75: 'decay'}
    if cc in cc_names:
        save_cc_debounced(chan, cc_names[cc], val)
    
    return "OK"

@app.route('/reset_channel/<int:chan>')
def reset_channel(chan):
    """Reset di tutti i controller MIDI del canale (più veloce di singoli CC)"""
    send_fluid(f"reset {chan}")
    return "OK"

@app.route('/refresh_status')
def refresh_status():
    data = send_fluid("channels")
    return jsonify({"raw": data})

@app.route('/get_state')
def get_state():
    """Restituisce tutto lo stato salvato (font, volumi, ecc)"""
    return jsonify(get_last_state())

@app.route('/api/capture_current_config')
def api_capture_current_config():
    """Cattura la configurazione MIDI completa corrente"""
    try:
        with STATE_LOCK:
            state = get_last_state()
        
        # Ottieni informazioni sui canali da FluidSynth
        channels_raw = send_fluid("channels")
        lines = channels_raw.split('\n')
        
        # Ottieni i dati dei canali salvati
        saved_channels = state.get('channels', {})
        
        channels = []
        for i in range(16):
            # Per ogni canale, cerca la riga corrispondente per il nome dello strumento
            channel_line = None
            instrument_name = 'Not Set'
            for line in lines:
                if line.strip().startswith(f'chan {i},'):
                    channel_line = line
                    break
            
            if channel_line and ',' in channel_line:
                instrument_name = channel_line.split(',')[1].strip()
            
            # Usa i dati salvati se disponibili, altrimenti usa default
            channel_data = saved_channels.get(str(i), {})
            
            channels.append({
                'channel': i,
                'bank': channel_data.get('bank', 0),
                'program': channel_data.get('program', 0),
                'attack': channel_data.get('attack', 64),
                'release': channel_data.get('release', 64),
                'decay': channel_data.get('decay', 64),
                'cutoff': channel_data.get('cutoff', 64),
                'resonance': channel_data.get('resonance', 64),
                'volume': channel_data.get('volume', 100),
                'instrument_name': instrument_name
            })
        
        config = {
            'font': state.get('font', ''),
            'gain': state.get('gain', 1.0),
            'reverb_level': state.get('reverb.level', 0.4),
            'chorus_level': state.get('chorus.level', 0.4),
            'channels': channels
        }
        
        return jsonify({'status': 'ok', 'config': config})
    except Exception as e:
        print(f"[Config] Capture error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==========================================
# ROTTE PER LA GESTIONE DEI PRESET MIDI
# ==========================================

@app.route('/presets')
def presets_page():
    """Pagina per la gestione dei preset"""
    return render_template('presets.html')

@app.route('/api/presets/list')
def api_presets_list():
    """Restituisce la lista di tutti i preset salvati"""
    try:
        if not os.path.exists(PRESETS_DIR):
            return jsonify([])
        
        presets = []
        for filename in os.listdir(PRESETS_DIR):
            if filename.endswith('.json'):
                preset_path = os.path.join(PRESETS_DIR, filename)
                try:
                    with open(preset_path, 'r') as f:
                        preset_data = json.load(f)
                        presets.append({
                            'filename': filename,
                            'name': preset_data.get('name', filename[:-5]),
                            'created': preset_data.get('created', 'Unknown'),
                            'font': preset_data.get('font', 'N/A')
                        })
                except (json.JSONDecodeError, IOError):
                    continue
        
        # Ordina per nome
        presets.sort(key=lambda x: x['name'].lower())
        return jsonify(presets)
    except Exception as e:
        print(f"[Preset] List error: {e}")
        return jsonify([])

@app.route('/api/presets/save', methods=['POST'])
def api_presets_save():
    """Salva la configurazione corrente come preset"""
    try:
        data = request.json
        preset_name = data.get('name', 'Untitled')
        
        # Sanitizza il nome del file
        safe_filename = "".join(c for c in preset_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_filename = safe_filename.replace(' ', '_')
        
        if not safe_filename:
            safe_filename = f"preset_{int(time.time())}"
        
        # Controlla se il file esiste già e rinomina automaticamente
        base_filename = safe_filename
        base_preset_name = preset_name
        counter = 2
        preset_path = os.path.join(PRESETS_DIR, f"{safe_filename}.json")
        
        while os.path.exists(preset_path):
            safe_filename = f"{base_filename}_{counter}"
            preset_name = f"{base_preset_name} ({counter})"
            preset_path = os.path.join(PRESETS_DIR, f"{safe_filename}.json")
            counter += 1
        
        # Crea la struttura del preset
        preset_data = {
            'name': preset_name,
            'created': time.strftime('%Y-%m-%d %H:%M:%S'),
            'font': data.get('font', ''),
            'channels': data.get('channels', []),
            'global_settings': {
                'gain': data.get('gain', 1.0),
                'reverb_level': data.get('reverb_level', 0.4),
                'chorus_level': data.get('chorus_level', 0.4)
            }
        }
        
        with open(preset_path, 'w') as f:
            json.dump(preset_data, f, indent=2)
        
        return jsonify({
            'status': 'ok', 
            'message': f'Preset "{preset_name}" salvato con successo',
            'saved_name': preset_name,
            'filename': f"{safe_filename}.json"
        })
    except Exception as e:
        print(f"[Preset] Save error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/presets/load/<filename>')
def api_presets_load(filename):
    """Carica un preset e restituisce i dati"""
    try:
        preset_path = os.path.join(PRESETS_DIR, filename)
        
        if not os.path.exists(preset_path):
            return jsonify({'status': 'error', 'message': 'Preset non trovato'}), 404
        
        with open(preset_path, 'r') as f:
            preset_data = json.load(f)
        
        return jsonify({'status': 'ok', 'data': preset_data})
    except Exception as e:
        print(f"[Preset] Load error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/presets/delete/<filename>', methods=['DELETE'])
def api_presets_delete(filename):
    """Elimina un preset"""
    try:
        preset_path = os.path.join(PRESETS_DIR, filename)
        
        if not os.path.exists(preset_path):
            return jsonify({'status': 'error', 'message': 'Preset non trovato'}), 404
        
        os.remove(preset_path)
        return jsonify({'status': 'ok', 'message': 'Preset eliminato con successo'})
    except Exception as e:
        print(f"[Preset] Delete error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/presets/rename', methods=['POST'])
def api_presets_rename():
    """Rinomina un preset esistente"""
    try:
        data = request.json
        old_filename = data.get('old_filename')
        new_name = data.get('new_name', 'Untitled')
        
        old_path = os.path.join(PRESETS_DIR, old_filename)
        
        if not os.path.exists(old_path):
            return jsonify({'status': 'error', 'message': 'Preset non trovato'}), 404
        
        # Carica il preset esistente
        with open(old_path, 'r') as f:
            preset_data = json.load(f)
        
        # Aggiorna il nome
        preset_data['name'] = new_name
        
        # Crea il nuovo filename
        safe_filename = "".join(c for c in new_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_filename = safe_filename.replace(' ', '_')
        
        if not safe_filename:
            safe_filename = f"preset_{int(time.time())}"
        
        new_path = os.path.join(PRESETS_DIR, f"{safe_filename}.json")
        
        # Salva con il nuovo nome
        with open(new_path, 'w') as f:
            json.dump(preset_data, f, indent=2)
        
        # Rimuovi il vecchio file solo se il nome è diverso
        if old_path != new_path:
            os.remove(old_path)
        
        return jsonify({
            'status': 'ok', 
            'message': f'Preset rinominato in "{new_name}"',
            'new_filename': f"{safe_filename}.json"
        })
    except Exception as e:
        print(f"[Preset] Rename error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/presets/apply', methods=['POST'])
def api_presets_apply():
    """Applica un preset caricato a FluidSynth"""
    try:
        data = request.json
        preset_data = data.get('preset')
        
        if not preset_data:
            return jsonify({'status': 'error', 'message': 'Dati preset mancanti'}), 400
        
        # NOTA: Il soundfont NON viene caricato automaticamente per evitare timeout.
        # L'utente deve caricare manualmente il soundfont desiderato prima di applicare il preset.
        
        # Ottieni l'ID del soundfont attualmente caricato
        global sf_id
        
        # 1. Applica le impostazioni globali
        global_settings = preset_data.get('global_settings', {})
        gain = global_settings.get('gain', 1.0)
        reverb = global_settings.get('reverb_level', 0.4)
        chorus = global_settings.get('chorus_level', 0.4)
        
        send_fluid(f"set synth.gain {gain}")
        send_fluid(f"set synth.reverb.level {reverb}")
        send_fluid(f"set synth.chorus.level {chorus}")
        
        save_state('gain', gain)
        save_state('reverb.level', reverb)
        save_state('chorus.level', chorus)
        
        # 2. Applica le impostazioni per ogni canale
        channels = preset_data.get('channels', [])
        for ch_data in channels:
            chan = ch_data.get('channel')
            if chan is None:
                continue
            
            # Seleziona lo strumento
            bank = ch_data.get('bank')
            prog = ch_data.get('program')
            if bank is not None and prog is not None:
                send_fluid(f"select {chan} {sf_id} {bank} {prog}")
            
            # Applica i CC
            if 'volume' in ch_data:
                send_fluid(f"cc {chan} 7 {ch_data['volume']}")
            if 'attack' in ch_data:
                send_fluid(f"cc {chan} 73 {ch_data['attack']}")
            if 'release' in ch_data:
                send_fluid(f"cc {chan} 72 {ch_data['release']}")
            if 'decay' in ch_data:
                send_fluid(f"cc {chan} 75 {ch_data['decay']}")
            if 'cutoff' in ch_data:
                send_fluid(f"cc {chan} 74 {ch_data['cutoff']}")
            if 'resonance' in ch_data:
                send_fluid(f"cc {chan} 71 {ch_data['resonance']}")
        
        return jsonify({'status': 'ok', 'message': 'Preset applicato con successo'})
    except Exception as e:
        print(f"[Preset] Apply error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ==========================================
# HARDWARE AND NETWORK SETTINGS ROUTES
# ==========================================

@app.route('/api/network')
def api_network():
    """Legge l'indirizzo IP e l'hostname del Raspberry"""
    try:
        # Esegue 'hostname -I' e divide gli IP in una lista
        ips_raw = subprocess.check_output(['hostname', '-I']).decode('utf-8').strip()
        ips = ips_raw.split() if ips_raw else []
        hostname = subprocess.check_output(['hostname']).decode('utf-8').strip()
    except Exception:
        ips = []
        hostname = "matsynth"
    return jsonify({"ips": ips, "hostname": hostname})

@app.route('/api/audio_devices')
def api_audio():
    """Scansiona le schede audio fisicamente collegate"""
    devices = []
    try:
        # Lancia 'aplay -l' per vedere i dispositivi di riproduzione
        out = subprocess.check_output(['aplay', '-l']).decode('utf-8')
        for line in out.split('\n'):
            if line.startswith('card'):
                # Estrae il numero della scheda e il nome
                # Esempio: "card 1: Fantom08 [Fantom-08], device 0: USB Audio [USB Audio]"
                parts = line.split(':')
                card_num = parts[0].replace('card', '').strip()
                name = parts[1].split(',')[0].strip()
                
                # Formato compatibile con FluidSynth (es. plughw:1)
                dev_id = f"plughw:{card_num}"
                print(f"DEBUG: Device trovato: {dev_id} - {name}")
                devices.append({"id": dev_id, "name": f"Scheda {card_num}: {name}"})
    except Exception as e:
        print(f"[Audio] Device scan error: {e}")

    with STATE_LOCK:
        state = get_last_state()
    # Se non c'è una scheda salvata, mettiamo un default vuoto
    current = state.get('audio_device', '')
    # Converti vecchi valori hw: in plughw: per compatibilità
    if current and current.startswith('hw:'):
        current = current.replace('hw:', 'plughw:', 1)
    return jsonify({"devices": devices, "current": current})

@app.route('/api/midi_devices')
def api_midi():
    """Scansiona le tastiere MIDI collegate"""
    devices = []
    try:
        # Lancia 'aconnect -i' per vedere le tastiere
        out = subprocess.check_output(['aconnect', '-i']).decode('utf-8')
        for line in out.split('\n'):
            if line.startswith('client'):
                # Estrae il numero client e il nome
                # Esempio: "client 20: 'Fantom-08' [type=kernel,card=1]"
                client_num = line.split(':')[0].replace('client', '').strip()
                name = line.split("'")[1] if "'" in line else line
                
                # Ignoriamo i dispositivi di sistema interni di Linux
                if "System" not in name and "Midi Through" not in name:
                    devices.append({"id": client_num, "name": name})
    except Exception as e:
        print(f"[MIDI] Device scan error: {e}")

    with STATE_LOCK:
        state = get_last_state()
    current = state.get('midi_device', '')
    return jsonify({"devices": devices, "current": current})

@app.route('/api/save_hardware', methods=['POST'])
def save_hardware():
    """Salva le impostazioni e riavvia il servizio MatSynth"""
    data = request.json
    if 'audio' in data and data['audio']:
        audio_device = data['audio']
        # Forza sempre plughw: invece di hw:
        if audio_device.startswith('hw:'):
            audio_device = audio_device.replace('hw:', 'plughw:', 1)
        print(f"[Settings] Saving audio_device: {audio_device}")
        save_state('audio_device', audio_device)
    if 'midi' in data and data['midi']: 
        save_state('midi_device', data['midi'])
    
    # Riavvia il servizio systemd in un thread separato dopo 1 secondo
    # per permettere alla risposta HTTP di arrivare al browser
    def delayed_restart():
        time.sleep(1)
        subprocess.run(["sudo", "systemctl", "restart", "matsynth.service"])
    
    restart_thread = threading.Thread(target=delayed_restart, daemon=True)
    restart_thread.start()
    
    return jsonify({"status": "ok"})


# ==========================================
# DAW MULTI-TRACK RECORDER API
# ==========================================

@app.route('/api/daw/state', methods=['GET'])
def daw_get_state():
    """Ottiene lo stato completo del DAW"""
    try:
        state = daw.get_state()
        return jsonify({"status": "ok", "data": state})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/record/start', methods=['POST'])
def daw_start_recording():
    """Avvia la registrazione su tutti i canali armati"""
    try:
        data = request.json or {}
        channel = data.get('channel', None)
        
        print(f"[DAW API] === START RECORDING REQUEST ===")
        print(f"[DAW API] request.json: {request.json}")
        print(f"[DAW API] data: {data}")
        print(f"[DAW API] channel param: {channel}")
        print(f"[DAW API] daw.armed dict: {daw.armed}")
        
        # Trova i canali armati
        armed_channels = [ch for ch in range(16) if daw.armed.get(ch)]
        print(f"[DAW API] armed_channels list: {armed_channels}")
        
        if not armed_channels:
            print(f"[DAW API] ERRORE: Nessun canale armato!")
            return jsonify({
                "status": "error", 
                "message": "Nessun canale armato. Attiva ARM su almeno un canale."
            }), 400
        
        if channel is not None:
            # Registrazione su canale specifico (backward compatibility)
            print(f"[DAW API] Richiesta start recording su canale {channel}")
            if not daw.armed.get(channel):
                print(f"[DAW API] ERRORE: Canale {channel} non armato!")
                return jsonify({
                    "status": "error", 
                    "message": f"Canale {channel} non è armato. Attiva ARM prima di registrare."
                }), 400
            success = daw.start_recording(channel)
        else:
            # Registrazione su tutti i canali armati (nuovo workflow)
            print(f"[DAW API] Richiesta start recording su canali armati: {armed_channels}")
            print(f"[DAW API] Stato is_recording: {daw.is_recording}")
            success = daw.start_recording()
        
        print(f"[DAW API] start_recording ritorna: {success}")
        
        if success:
            return jsonify({"status": "ok", "message": f"Registrazione avviata su canali {armed_channels}"})
        else:
            print(f"[DAW API] ERRORE: Impossibile avviare registrazione")
            return jsonify({"status": "error", "message": "Impossibile avviare registrazione"}), 400
            
    except Exception as e:
        print(f"[DAW API] ECCEZIONE in start_recording: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/record/stop', methods=['POST'])
def daw_stop_recording():
    """Ferma la registrazione in corso"""
    try:
        success = daw.stop_recording()
        
        if success:
            return jsonify({"status": "ok", "message": "Registrazione fermata"})
        else:
            return jsonify({"status": "error", "message": "Nessuna registrazione in corso"}), 400
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/play/start', methods=['POST'])
def daw_start_playback():
    """Avvia la riproduzione di tutte le tracce"""
    try:
        success = daw.start_playback()
        
        if success:
            return jsonify({"status": "ok", "message": "Riproduzione avviata"})
        else:
            return jsonify({"status": "error", "message": "Nessuna traccia da riprodurre o già in riproduzione"}), 400
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/play/stop', methods=['POST'])
def daw_stop_playback():
    """Ferma la riproduzione"""
    try:
        success = daw.stop_playback()
        
        if success:
            return jsonify({"status": "ok", "message": "Riproduzione fermata"})
        else:
            return jsonify({"status": "error", "message": "Nessuna riproduzione in corso"}), 400
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/stop_all', methods=['POST'])
def daw_stop_all():
    """Ferma tutto: registrazione e riproduzione"""
    try:
        daw.stop_all()
        return jsonify({"status": "ok", "message": "Tutto fermato"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/panic', methods=['POST'])
def daw_panic():
    """Invia All Notes Off e All Sound Off su tutti i canali MIDI"""
    try:
        daw.panic()
        return jsonify({"status": "ok", "message": "PANIC inviato"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/rewind', methods=['POST'])
def daw_rewind():
    """Riporta la timeline a 00:00:00"""
    try:
        daw.rewind()
        return jsonify({"status": "ok", "message": "Timeline resettata"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/arm', methods=['POST'])
def daw_arm_track(channel):
    """Arma/disarma una traccia per la registrazione"""
    if not 0 <= channel <= 15:
        return jsonify({"status": "error", "message": "Channel must be 0-15"}), 400
    try:
        data = request.json
        armed = data.get('armed', True)
        
        success = daw.arm_track(channel, armed)
        
        print(f"[DAW API] Track {channel} {'armed' if armed else 'disarmed'} - success={success}")
        
        return jsonify({
            "status": "ok", 
            "message": f"Traccia {channel} {'armata' if armed else 'disarmata'}",
            "armed": daw.armed[channel]  # Ritorna lo stato effettivo
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/mute', methods=['POST'])
def daw_mute_track(channel):
    """Muta/smuta una traccia"""
    if not 0 <= channel <= 15:
        return jsonify({"status": "error", "message": "Channel must be 0-15"}), 400
    try:
        data = request.json
        muted = data.get('muted', True)
        
        daw.mute_track(channel, muted)
        
        return jsonify({
            "status": "ok", 
            "message": f"Traccia {channel} {'mutata' if muted else 'smutata'}",
            "muted": daw.muted[channel]  # Ritorna lo stato effettivo
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/solo', methods=['POST'])
def daw_solo_track(channel):
    """Attiva/disattiva il solo su una traccia"""
    if not 0 <= channel <= 15:
        return jsonify({"status": "error", "message": "Channel must be 0-15"}), 400
    try:
        data = request.json
        solo = data.get('solo', True)
        
        daw.solo_track(channel, solo)
        
        return jsonify({
            "status": "ok",
            "message": f"Traccia {channel} solo {'attivato' if solo else 'disattivato'}",
            "solo": daw.solo[channel]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/clear', methods=['POST'])
def daw_clear_track(channel):
    """Cancella una traccia specifica"""
    if not 0 <= channel <= 15:
        return jsonify({"status": "error", "message": "Channel must be 0-15"}), 400
    try:
        daw.clear_track(channel)
        return jsonify({"status": "ok", "message": f"Traccia {channel} cancellata"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clear_all', methods=['POST'])
def daw_clear_all():
    """Cancella tutte le tracce"""
    try:
        daw.clear_all_tracks()
        return jsonify({"status": "ok", "message": "Tutte le tracce cancellate"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/clear_range', methods=['POST'])
def daw_clear_range(channel):
    """Cancella gli eventi di una traccia in un intervallo di beat"""
    try:
        data = request.get_json()
        start_beat = float(data.get('start_beat', 0))
        end_beat = float(data.get('end_beat', 0))
        if end_beat <= start_beat:
            return jsonify({"status": "error", "message": "end_beat deve essere > start_beat"}), 400
        daw.clear_range(channel, start_beat, end_beat)
        return jsonify({"status": "ok", "message": f"Range [{start_beat:.2f}, {end_beat:.2f}) cancellato su ch {channel}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/bpm', methods=['POST'])
def daw_set_bpm():
    """Imposta il tempo (BPM)"""
    try:
        data = request.json
        bpm = data.get('bpm', 120)
        
        success = daw.set_bpm(bpm)
        
        if success:
            return jsonify({"status": "ok", "message": f"BPM impostato a {bpm}"})
        else:
            return jsonify({
                "status": "error", 
                "message": "Impossibile cambiare BPM durante registrazione/riproduzione"
            }), 400
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/time_signature', methods=['POST'])
def daw_set_time_signature():
    """Imposta il numeratore della misura (3/4 o 4/4)."""
    try:
        data = request.json or {}
        beats_per_measure = int(data.get('beats_per_measure', 4))
        success = daw.set_time_signature(beats_per_measure)
        if success:
            return jsonify({"status": "ok", "beats_per_measure": beats_per_measure})
        else:
            return jsonify({"status": "error", "message": "Time signature non supportato"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/metronome/toggle', methods=['POST'])
def daw_toggle_metronome():
    """Toggle metronomo on/off"""
    try:
        enabled = daw.toggle_metronome()
        return jsonify({
            "status": "ok", 
            "message": f"Metronomo {'abilitato' if enabled else 'disabilitato'}",
            "enabled": enabled
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/loop_points', methods=['POST'])
def daw_set_loop_points():
    """Imposta i punti di loop (in beat)"""
    try:
        data = request.json or {}
        start_beat = float(data.get('start_beat', 0))
        end_beat = float(data.get('end_beat', 0))
        success = daw.set_loop_points(start_beat, end_beat)
        if success:
            return jsonify({"status": "ok", "loop_start": start_beat, "loop_end": end_beat})
        else:
            return jsonify({"status": "error", "message": "end_beat deve essere > start_beat"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/loop/toggle', methods=['POST'])
def daw_toggle_loop():
    """Attiva/disattiva il loop"""
    try:
        enabled = daw.toggle_loop()
        return jsonify({
            "status": "ok",
            "message": f"Loop {'attivato' if enabled else 'disattivato'}",
            "enabled": enabled
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/undo', methods=['POST'])
def daw_undo():
    """Annulla l'ultima operazione distruttiva"""
    try:
        success = daw.undo()
        if success:
            return jsonify({"status": "ok", "message": "Undo eseguito"})
        else:
            return jsonify({"status": "error", "message": "Niente da annullare"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/redo', methods=['POST'])
def daw_redo():
    """Ripristina l'ultima operazione annullata"""
    try:
        success = daw.redo()
        if success:
            return jsonify({"status": "ok", "message": "Redo eseguito"})
        else:
            return jsonify({"status": "error", "message": "Niente da ripristinare"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/quantize', methods=['POST'])
def daw_quantize():
    """Applica quantizzazione post-registrazione alle tracce DAW."""
    try:
        data = request.json or {}

        # Canali: lista di int 0-15, oppure vuota = tutti
        channels_raw = data.get('channels', [])
        if not channels_raw:
            channels = []
        else:
            channels = [int(c) for c in channels_raw
                        if 0 <= int(c) <= 15]

        # Griglia in beat (validi: 1.0, 0.5, 0.25, 0.125, 0.0625)
        grid = float(data.get('grid', 0.25))
        valid_grids = {1.0, 0.5, 0.25, 0.125, 0.0625}
        if grid not in valid_grids:
            return jsonify({"status": "error",
                            "message": "Valore griglia non valido"}), 400

        # Strength 0-100 → 0.0-1.0
        strength = max(0.0, min(1.0, float(data.get('strength', 100)) / 100.0))

        # Swing 0-100 → 0.0-1.0  (50 = straight)
        swing = max(0.0, min(1.0, float(data.get('swing', 50)) / 100.0))

        result = daw.quantize_tracks(channels, grid, strength, swing)

        if result is None:
            return jsonify({
                "status": "error",
                "message": "Impossibile quantizzare durante la registrazione o il playback"
            }), 409

        if not result:
            return jsonify({
                "status": "ok",
                "message": "Nessuna traccia con dati da quantizzare",
                "events_modified": 0,
                "channels_processed": []
            })

        total = sum(result.values())
        return jsonify({
            "status": "ok",
            "channels_processed": list(result.keys()),
            "events_modified": total,
            "message": f"Quantizzazione: {total} note su {len(result)} tracce"
        })
    except Exception as e:
        print(f"[DAW] quantize error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/transpose', methods=['POST'])
def daw_transpose():
    """Transpose notes on one channel (whole track or beat-range)."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', -1))
        semitones = int(data.get('semitones', 0))

        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "channel must be 0-15"}), 400
        if semitones < -24 or semitones > 24:
            return jsonify({"status": "error", "message": "semitones must be between -24 and 24"}), 400

        start_beat = data.get('start_beat', None)
        end_beat = data.get('end_beat', None)

        modified = daw.transpose_notes(channel, semitones, start_beat, end_beat)
        if modified is None:
            return jsonify({"status": "error", "message": "Cannot transpose during recording/playback"}), 409

        return jsonify({
            "status": "ok",
            "channel": channel,
            "semitones": semitones,
            "modified": modified
        })
    except Exception as e:
        print(f"[DAW] transpose error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/preview_note', methods=['POST'])
def daw_preview_note():
    """Play a very short preview note on a channel for piano-roll interactions."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', 0))
        note = int(data.get('note', 60))
        velocity = int(data.get('velocity', 96))
        length_ms = int(data.get('length_ms', 120))

        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "channel must be 0-15"}), 400

        note = max(0, min(127, note))
        velocity = max(1, min(127, velocity))
        length_ms = max(40, min(400, length_ms))

        send_fluid(f"noteon {channel} {note} {velocity}")

        def _noteoff():
            try:
                time.sleep(length_ms / 1000.0)
                send_fluid(f"noteoff {channel} {note}")
            except Exception as ex:
                print(f"[DAW] preview noteoff error: {ex}")

        threading.Thread(target=_noteoff, daemon=True).start()
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"[DAW] preview error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/copy', methods=['POST'])
def daw_copy():
    """Copy events from a selection range on a channel to the clipboard."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', 0))
        start_beat = float(data.get('start_beat', 0))
        end_beat = float(data.get('end_beat', 0))
        count = daw.copy_selection(channel, start_beat, end_beat)
        return jsonify({"status": "ok", "copied": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/paste', methods=['POST'])
def daw_paste():
    """Paste clipboard events at a target beat on a channel."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', 0))
        target_beat = float(data.get('beat', 0))
        count = daw.paste_at(channel, target_beat)
        return jsonify({"status": "ok", "pasted": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/clips', methods=['GET'])
def daw_track_clips(channel):
    """Return clip objects for one track/channel."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        return jsonify({"status": "ok", "clips": daw.get_clips(channel)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clip/move', methods=['POST'])
def daw_move_clip():
    """Move one clip to a new start beat."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', -1))
        clip_id = int(data.get('clip_id', -1))
        target_start_beat = float(data.get('target_start_beat', 0.0))

        if channel < 0 or channel > 15 or clip_id < 0:
            return jsonify({"status": "error", "message": "Invalid channel or clip_id"}), 400

        result = daw.move_clip(channel, clip_id, target_start_beat)
        if result is None:
            return jsonify({"status": "error", "message": "Cannot move clip during recording/playback"}), 409
        if not result:
            return jsonify({"status": "error", "message": "Clip not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clip/delete', methods=['POST'])
def daw_delete_clip():
    """Delete one clip and its events."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', -1))
        clip_id = int(data.get('clip_id', -1))

        if channel < 0 or channel > 15 or clip_id < 0:
            return jsonify({"status": "error", "message": "Invalid channel or clip_id"}), 400

        result = daw.delete_clip(channel, clip_id)
        if result is None:
            return jsonify({"status": "error", "message": "Cannot delete clip during recording/playback"}), 409
        if not result:
            return jsonify({"status": "error", "message": "Clip not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clip/transpose', methods=['POST'])
def daw_transpose_clip():
    """Transpose note events inside one clip range."""
    try:
        data = request.json or {}
        channel = int(data.get('channel', -1))
        clip_id = int(data.get('clip_id', -1))
        semitones = int(data.get('semitones', 0))

        if channel < 0 or channel > 15 or clip_id < 0:
            return jsonify({"status": "error", "message": "Invalid channel or clip_id"}), 400
        if semitones < -24 or semitones > 24:
            return jsonify({"status": "error", "message": "semitones must be between -24 and 24"}), 400

        modified = daw.transpose_clip(channel, clip_id, semitones)
        if modified is None:
            return jsonify({"status": "error", "message": "Cannot transpose during recording/playback"}), 409
        return jsonify({"status": "ok", "modified": modified})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/duplicate', methods=['POST'])
def daw_duplicate_track(channel):
    """Duplicate track events to a user-chosen channel. Instrument and CC stay unchanged on dest."""
    try:
        data = request.json or {}
        dest_channel = data.get('dest_channel')
        if dest_channel is None:
            return jsonify({"status": "error", "message": "dest_channel required"}), 400
        dest_channel = int(dest_channel)
        if dest_channel < 0 or dest_channel > 15:
            return jsonify({"status": "error", "message": "dest_channel out of range"}), 400
        if dest_channel == channel:
            return jsonify({"status": "error", "message": "Cannot duplicate onto same channel"}), 400

        result = daw.duplicate_track(channel, dest_channel)
        if result == -1:
            return jsonify({"status": "error", "message": "Cannot duplicate (source empty or transport active)"}), 400
        return jsonify({"status": "ok", "dest_channel": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/rename', methods=['POST'])
def daw_rename_track(channel):
    """Rename a track."""
    try:
        data = request.json or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({"status": "error", "message": "Name required"}), 400
        ok = daw.rename_track(channel, name)
        return jsonify({"status": "ok" if ok else "error"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/color', methods=['POST'])
def daw_set_track_color(channel):
    """Set track color palette index."""
    try:
        data = request.json or {}
        color_index = int(data.get('color_index', 0))
        ok = daw.set_track_color(channel, color_index)
        return jsonify({"status": "ok" if ok else "error"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/full_density_map')
def daw_full_density_map():
    """Ritorna la mappa di densità completa per tutte le tracce."""
    try:
        density = daw.get_full_density_map()
        return jsonify({"status": "ok", "map": density})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/full_note_map')
def daw_full_note_map():
    """Return note-level data for piano-roll rendering."""
    try:
        note_map = daw.get_full_note_map()
        return jsonify({"status": "ok", "map": note_map})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/seek_beat', methods=['POST'])
def daw_seek_beat():
    """Seek to a beat position (works during playback too)."""
    try:
        data = request.json or {}
        beat = data.get('beat')
        if beat is None or not isinstance(beat, (int, float)):
            return jsonify({"status": "error", "message": "Missing or invalid beat"}), 400
        success = daw.seek_to_beat(float(beat))
        if not success:
            return jsonify({"status": "error", "message": "Cannot seek during recording"}), 409
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/activity')
def daw_track_activity(channel):
    """Restituisce intervalli di attività note per una traccia nel range visibile."""
    try:
        start = float(request.args.get('start', 0.0))
        end = float(request.args.get('end', start + 16.0))
        intervals = daw.get_track_activity(channel, start, end)
        return jsonify({"status": "ok", "intervals": intervals})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/events')
def daw_track_events(channel):
    """Returns decoded MIDI events list for a channel (-1 = all)."""
    try:
        events = daw.get_events_decoded(channel)
        return jsonify({"status": "ok", "events": events})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/events/<int:index>', methods=['PUT'])
def daw_edit_event(channel, index):
    """Edit an event's properties (beat, velocity, note, cc_num, cc_value, pitch, program)."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        data = request.json or {}
        allowed = ('beat', 'velocity', 'note', 'cc_value', 'cc_num', 'pitch', 'program')
        updates = {k: data[k] for k in allowed if k in data}
        if not updates:
            return jsonify({"status": "error", "message": "No updates provided"}), 400
        ok = daw.edit_event(channel, index, updates)
        if not ok:
            return jsonify({"status": "error", "message": "Cannot edit (recording or invalid index)"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/events/<int:index>', methods=['DELETE'])
def daw_delete_event(channel, index):
    """Delete an event by index."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        ok = daw.delete_event(channel, index)
        if not ok:
            return jsonify({"status": "error", "message": "Cannot delete (recording or invalid index)"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/add_note', methods=['POST'])
def daw_add_note(channel):
    """Add a note to a track (Piano Roll draw tool)."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        data = request.get_json(force=True)
        note = int(data.get('note', 60))
        velocity = int(data.get('velocity', 100))
        start_beat = float(data.get('start_beat', 0))
        length_beats = float(data.get('length_beats', 0.5))
        if not (0 <= note <= 127 and 0 <= velocity <= 127):
            return jsonify({"status": "error", "message": "Note/velocity out of range"}), 400
        ok = daw.add_note(channel, note, velocity, start_beat, length_beats)
        if not ok:
            return jsonify({"status": "error", "message": "Cannot add note (recording?)"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


startup_init_once()

# ==========================================
# SOCKETIO EVENT HANDLERS (Hybrid approach)
# ==========================================

ALLOWED_EFFECT_TYPES = {'reverb.level', 'chorus.level', 'gain'}

@socketio.on('cc_update')
def handle_cc_update(data):
    """Handle CC messages via WebSocket for lower latency"""
    try:
        chan = int(data['chan'])
        cc_num = int(data['cc'])
        val = int(data['val'])
        if not (0 <= chan <= 15 and 0 <= cc_num <= 127 and 0 <= val <= 127):
            return {'status': 'error', 'message': 'Values out of range'}
        send_fluid(f"cc {chan} {cc_num} {val}")
        cc_names = {7: 'volume', 10: 'pan', 71: 'resonance', 72: 'release',
                    73: 'attack', 74: 'cutoff', 75: 'decay'}
        if cc_num in cc_names:
            save_cc_debounced(chan, cc_names[cc_num], val)
        return {'status': 'ok'}
    except Exception as e:
        print(f"[WS] cc_update error: {e}")
        return {'status': 'error', 'message': str(e)}


@socketio.on('effect_update')
def handle_effect_update(data):
    """Handle effect changes via WebSocket for lower latency"""
    try:
        etype = str(data['type'])
        if etype not in ALLOWED_EFFECT_TYPES:
            return {'status': 'error', 'message': f'Invalid effect type: {etype}'}
        val = float(data['val'])
        send_fluid(f"set synth.{etype} {val}")
        save_state(etype, val)
        return {'status': 'ok'}
    except Exception as e:
        print(f"[WS] effect_update error: {e}")
        return {'status': 'error', 'message': str(e)}


@socketio.on('transport_cmd')
def handle_transport_cmd(data):
    """Handle transport commands via WebSocket for lower latency"""
    try:
        cmd = data.get('cmd')
        if cmd == 'play_start':
            success = daw.start_playback()
        elif cmd == 'play_stop':
            success = daw.stop_playback()
        elif cmd == 'record_start':
            channel = data.get('channel')
            armed_channels = [ch for ch in range(16) if daw.armed.get(ch)]
            if not armed_channels:
                return {'status': 'error', 'message': 'Nessun canale armato. Attiva ARM su almeno un canale.'}
            success = daw.start_recording(channel) if channel is not None else daw.start_recording()
        elif cmd == 'record_stop':
            success = daw.stop_recording()
        elif cmd == 'stop_all':
            daw.stop_all()
            success = True
        elif cmd == 'panic':
            daw.panic()
            success = True
        elif cmd == 'rewind':
            daw.rewind()
            success = True
        elif cmd == 'seek':
            position = data.get('position')
            if position is None or not isinstance(position, (int, float)):
                return {'status': 'error', 'message': 'Missing or invalid position'}
            success = daw.set_position(position)
            if not success:
                return {'status': 'error', 'message': 'Cannot seek while playing or recording'}
        elif cmd == 'seek_beat':
            beat = data.get('beat')
            if beat is None or not isinstance(beat, (int, float)):
                return {'status': 'error', 'message': 'Missing or invalid beat'}
            success = daw.seek_to_beat(float(beat))
            if not success:
                return {'status': 'error', 'message': 'Cannot seek during recording'}
        elif cmd == 'undo':
            success = daw.undo()
        elif cmd == 'redo':
            success = daw.redo()
        elif cmd == 'toggle_loop':
            daw.toggle_loop()
            success = True
        else:
            return {'status': 'error', 'message': f'Unknown command: {cmd}'}
        return {'status': 'ok'} if success else {'status': 'error', 'message': f'{cmd} failed'}
    except Exception as e:
        print(f"[WS] transport_cmd error: {e}")
        return {'status': 'error', 'message': str(e)}


@app.route('/api/daw/midi/save', methods=['POST'])
def daw_midi_save():
    """Salva le tracce registrate come file MIDI sul Raspberry Pi"""
    try:
        data = request.json or {}
        name = data.get('name', '').strip()
        if not name:
            name = f"session_{int(time.time())}"

        # Sanitizza il nome del file
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_')
        if not safe_name:
            safe_name = f"session_{int(time.time())}"

        # Evita sovrascritture: aggiungi suffisso
        base_name = safe_name
        filepath = os.path.join(MIDI_DIR, f"{safe_name}.mid")
        counter = 2
        while os.path.exists(filepath):
            safe_name = f"{base_name}_{counter}"
            filepath = os.path.join(MIDI_DIR, f"{safe_name}.mid")
            counter += 1

        # Leggi i programmi correnti dei canali dal saved state
        state = get_last_state()
        saved_channels = state.get('channels', {})
        channel_programs = {}
        for ch_str, ch_data in saved_channels.items():
            try:
                ch = int(ch_str)
            except ValueError:
                continue
            bank = ch_data.get('bank')
            prog = ch_data.get('program')
            if bank is not None and prog is not None:
                channel_programs[ch] = {'bank': bank, 'program': prog}

        midi_bytes = daw.export_midi(channel_programs=channel_programs)
        if not midi_bytes:
            return jsonify({"status": "error", "message": "Nessuna traccia da esportare"}), 400

        with open(filepath, 'wb') as f:
            f.write(midi_bytes)

        return jsonify({
            "status": "ok",
            "filename": f"{safe_name}.mid",
            "message": f"Salvato come {safe_name}.mid"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/midi/list')
def daw_midi_list():
    """Restituisce la lista dei file MIDI salvati"""
    try:
        if not os.path.exists(MIDI_DIR):
            return jsonify([])
        files = []
        for fn in sorted(os.listdir(MIDI_DIR)):
            if fn.lower().endswith(('.mid', '.midi')):
                fp = os.path.join(MIDI_DIR, fn)
                stat = os.stat(fp)
                files.append({
                    'filename': fn,
                    'name': fn.rsplit('.', 1)[0],
                    'size': stat.st_size,
                    'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime))
                })
        return jsonify(files)
    except Exception as e:
        return jsonify([])  


@app.route('/api/daw/midi/load/<filename>')
def daw_midi_load(filename):
    """Carica un file MIDI salvato nelle tracce del DAW"""
    try:
        # Previeni path traversal
        safe = os.path.basename(filename)
        filepath = os.path.join(MIDI_DIR, safe)
        if not os.path.exists(filepath):
            return jsonify({"status": "error", "message": "File non trovato"}), 404
        with open(filepath, 'rb') as f:
            midi_bytes = f.read()
        merge = request.args.get('merge', 'false').lower() == 'true'
        info = daw.import_midi(midi_bytes, merge=merge)

        # Applica gli strumenti importati a FluidSynth e salva nello stato
        ch_progs = info.get('channel_programs', {})
        if ch_progs:
            with STATE_LOCK:
                st = get_last_state()
                if 'channels' not in st:
                    st['channels'] = {}
                for ch_str, prog_data in ch_progs.items():
                    ch = int(ch_str)
                    bank = prog_data.get('bank', 0)
                    prog = prog_data.get('program', 0)
                    send_fluid(f"select {ch} {sf_id} {bank} {prog}")
                    if str(ch) not in st['channels']:
                        st['channels'][str(ch)] = {}
                    st['channels'][str(ch)]['bank'] = bank
                    st['channels'][str(ch)]['program'] = prog
                _write_state_atomic(st)

        return jsonify({"status": "ok", "data": info})
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 409
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/midi/delete/<filename>', methods=['DELETE'])
def daw_midi_delete(filename):
    """Elimina un file MIDI salvato"""
    try:
        safe = os.path.basename(filename)
        filepath = os.path.join(MIDI_DIR, safe)
        if not os.path.exists(filepath):
            return jsonify({"status": "error", "message": "File non trovato"}), 404
        os.remove(filepath)
        return jsonify({"status": "ok", "message": f"{safe} eliminato"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/midi/download/<filename>')
def daw_midi_download(filename):
    """Scarica un file MIDI salvato"""
    try:
        safe = os.path.basename(filename)
        filepath = os.path.join(MIDI_DIR, safe)
        if not os.path.exists(filepath):
            return jsonify({"status": "error", "message": "File non trovato"}), 404
        return send_file(filepath, mimetype='audio/midi',
                         as_attachment=True, download_name=safe)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/midi/upload', methods=['POST'])
def daw_midi_upload():
    """Carica un file MIDI dal PC nelle tracce del DAW"""
    try:
        if 'file' not in request.files:
            return jsonify({"status": "error", "message": "Nessun file inviato"}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({"status": "error", "message": "File vuoto"}), 400
        midi_bytes = f.read(5 * 1024 * 1024)
        if len(midi_bytes) < 14:
            return jsonify({"status": "error", "message": "File troppo piccolo o non valido"}), 400
        merge = request.form.get('merge', 'false').lower() == 'true'
        info = daw.import_midi(midi_bytes, merge=merge)

        # Salva anche una copia nella cartella MIDI
        original_name = os.path.splitext(os.path.basename(f.filename))[0]
        safe_name = "".join(c for c in original_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_') or f"upload_{int(time.time())}"
        filepath = os.path.join(MIDI_DIR, f"{safe_name}.mid")
        counter = 2
        while os.path.exists(filepath):
            filepath = os.path.join(MIDI_DIR, f"{safe_name}_{counter}.mid")
            counter += 1
        with open(filepath, 'wb') as out:
            out.write(midi_bytes)

        # Applica gli strumenti importati a FluidSynth e salva nello stato
        ch_progs = info.get('channel_programs', {})
        if ch_progs:
            with STATE_LOCK:
                st = get_last_state()
                if 'channels' not in st:
                    st['channels'] = {}
                for ch_str, prog_data in ch_progs.items():
                    ch = int(ch_str)
                    bank = prog_data.get('bank', 0)
                    prog = prog_data.get('program', 0)
                    send_fluid(f"select {ch} {sf_id} {bank} {prog}")
                    if str(ch) not in st['channels']:
                        st['channels'][str(ch)] = {}
                    st['channels'][str(ch)]['bank'] = bank
                    st['channels'][str(ch)]['program'] = prog
                _write_state_atomic(st)

        return jsonify({"status": "ok", "data": info})
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 409
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    print("Starting server with WebSocket support...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
