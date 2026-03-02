from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import socket
import os
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
sf_id = 1 # Variabile globale per tenere traccia dell'ID del soundfont attivo
STATE_LOCK = threading.Lock()
_INITIALIZED = False

# Crea la directory dei preset se non esiste
if not os.path.exists(PRESETS_DIR):
    os.makedirs(PRESETS_DIR)

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
        print(f"Errore salvataggio stato: {e}")

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

            # Per comandi di lettura (inst, fonts, channels)
            time.sleep(0.2)
            data = ""
            while True:
                try:
                    chunk = sock.recv(4096).decode()
                    if not chunk: break
                    data += chunk
                except:
                    break
            return data
    except Exception as e:
        print(f"Errore Socket: {e}")
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
    # Usiamo i valori salvati o default sicuri
    gain = state.get('gain', 0.7)
    rev = state.get('reverb.level', 0.4)
    cho = state.get('chorus.level', 0.4)
    
    print(f"Ripristino: Gain {gain}, Rev {rev}, Chorus {cho}")
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
            print(f"Ripristino font {font_name}: {res}")
            # Aggiorna l'ID attivo del font
            global sf_id
            sf_id = get_active_sf_id()
        else:
            print(f"Font salvato non trovato: {path}")

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
    # Piccola attesa per permettere a FluidSynth di essere pronto
    time.sleep(2)
    restore_settings()
    # Aggiorna l'ID attivo del font dopo il ripristino
    sf_id = get_active_sf_id()
    print(f"Soundfont ID caricato all'avvio: {sf_id}")

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
        
        print(f"✓ Salvato canale {chan}: bank={bank}, prog={prog}")
        _write_state_atomic(state)
    
    return "OK"

@app.route('/set_effect/<type>/<val>')
def set_effect(type, val):
    try:
        f_val = float(val)
        send_fluid(f"set synth.{type} {f_val}")
        save_state(type, f_val)
        return "OK"
    except:
        return "Errore", 400

@app.route('/cc/<int:chan>/<int:cc>/<int:val>')
def control(chan, cc, val):
    send_fluid(f"cc {chan} {cc} {val}")
    
    # Salva i CC importanti per i preset
    # attack=73, release=72, cutoff=74, resonance=71, volume=7, decay=75
    if cc in [7, 71, 72, 73, 74, 75]:
        with STATE_LOCK:
            state = get_last_state()
            if 'channels' not in state:
                state['channels'] = {}
            if str(chan) not in state['channels']:
                state['channels'][str(chan)] = {}
            
            # Mappa i CC ai nomi
            cc_names = {
                7: 'volume',
                71: 'resonance',
                72: 'release',
                73: 'attack',
                74: 'cutoff',
                75: 'decay'
            }
            state['channels'][str(chan)][cc_names[cc]] = val
            _write_state_atomic(state)
    
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
        print(f"Errore cattura configurazione: {e}")
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
                except:
                    continue
        
        # Ordina per nome
        presets.sort(key=lambda x: x['name'].lower())
        return jsonify(presets)
    except Exception as e:
        print(f"Errore lettura preset: {e}")
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
        print(f"Errore salvataggio preset: {e}")
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
        print(f"Errore caricamento preset: {e}")
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
        print(f"Errore eliminazione preset: {e}")
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
        print(f"Errore rinominazione preset: {e}")
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
                send_fluid(f"cc {chan} 76 {ch_data['decay']}")
            if 'cutoff' in ch_data:
                send_fluid(f"cc {chan} 74 {ch_data['cutoff']}")
            if 'resonance' in ch_data:
                send_fluid(f"cc {chan} 71 {ch_data['resonance']}")
        
        return jsonify({'status': 'ok', 'message': 'Preset applicato con successo'})
    except Exception as e:
        print(f"Errore applicazione preset: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# rotte per settings
# ==========================================
# ROTTE PER LE IMPOSTAZIONI HARDWARE E RETE
# ==========================================

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

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
        print(f"Errore lettura audio: {e}")

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
        print(f"Errore lettura midi: {e}")

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
        # Debug: stampa cosa viene salvato
        print(f"DEBUG: Salvando audio_device: {audio_device}")
        save_state('audio_device', audio_device)
    if 'midi' in data and data['midi']: 
        save_state('midi_device', data['midi'])
    
    # Riavvia il servizio systemd in un thread separato dopo 1 secondo
    # per permettere alla risposta HTTP di arrivare al browser
    def delayed_restart():
        time.sleep(1)
        os.system("sudo systemctl restart matsynth.service")
    
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
    try:
        data = request.json
        armed = data.get('armed', True)
        
        success = daw.arm_track(channel, armed)
        
        print(f"[DAW API] Traccia {channel} {'armata' if armed else 'disarmata'} - success={success}")
        print(f"[DAW API] Stato armed aggiornato: {daw.armed[channel]}")
        
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


@app.route('/api/daw/track/<int:channel>/clear', methods=['POST'])
def daw_clear_track(channel):
    """Cancella una traccia specifica"""
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


@app.route('/api/daw/full_density_map')
def daw_full_density_map():
    """Ritorna la mappa di densità completa per tutte le tracce."""
    try:
        density = daw.get_full_density_map()
        return jsonify({"status": "ok", "map": density})
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


startup_init_once()

if __name__ == '__main__':
    print("Starting server with WebSocket support...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
