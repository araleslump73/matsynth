from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import socket
import os
import io
import json
import time
import subprocess
import threading
import copy
import midi_router
from api_common import ok, err, ack_ok, ack_err, from_router_error
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
DEVICES_FILE = '/home/matteo/matsynth_web/midi_devices.json'  # Registro alias dispositivi MIDI (ui-routing)
sf_id = 1 # Global variable to track the active soundfont ID
STATE_LOCK = threading.Lock()
ALLOWED_EFFECT_TYPES = {'reverb.level', 'chorus.level', 'gain'}  # whitelist /set_effect e effect_update
_INITIALIZED = False

# Debounced state writer — batches rapid CC writes to reduce SD card I/O on Pi Zero
_state_dirty = False
_state_pending = {}       # pending flat key→value updates
_cc_pending = {}          # pending CC updates: (chan_str, cc_name) → value
_parts_pending = set()    # part_id con CC cambiati da persistere in `parts`
_state_flush_lock = threading.Lock()

def _schedule_state_flush():
    """Flush pending state writes after a short delay (debounce)."""
    global _state_dirty, _state_pending, _cc_pending
    time.sleep(0.5)  # 500ms debounce
    with _state_flush_lock:
        if not _state_pending and not _cc_pending and not _parts_pending:
            return
        pending = _state_pending.copy()
        cc_pending = _cc_pending.copy()
        parts_pending = sorted(_parts_pending)
        _state_pending.clear()
        _cc_pending.clear()
        _parts_pending.clear()
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
            for t in parts_pending:
                _put_part_in_state(state, router.get_part(t))
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

def save_part_debounced(part_id):
    """Persistenza debounced della parte (CC dagli slider) in `parts` e,
    per le parti interne, nella voce legacy `channels`."""
    global _state_dirty
    with _state_flush_lock:
        _parts_pending.add(part_id)
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

# ==========================================
# ROUTER MIDI + DAW (ui-routing)
# ==========================================

def _console(command):
    # Risolve send_fluid a ogni chiamata (i test la sostituiscono dopo l'import).
    return send_fluid(command)

_boot_state = get_last_state()
EFFECTIVE_MIDI_MODE = midi_router.resolve_effective_mode(os.environ.get('MATSYNTH_MIDI_MODE'), _boot_state)
router = midi_router.MidiRouter(
    backend=midi_router.MidoBackend(),
    parts=midi_router.parts_from_state(_boot_state)[0],
    registry=midi_router.load_registry_file(DEVICES_FILE),
    mode=EFFECTIVE_MIDI_MODE,
    devices_file=DEVICES_FILE,
    emit=lambda event, payload: socketio.emit(event, payload),
    console=_console,
    sf_id=lambda: sf_id,
)
router.start(poll=True)
print(f"[Init] Modalita' MIDI effettiva: {EFFECTIVE_MIDI_MODE}")
del _boot_state

# Inizializza il sistema DAW Multi-Track
daw = MultiTrackDAW(bpm=120, socketio=socketio, router=router)


def _put_part_in_state(state, part):
    """Scrive una parte in `state['parts']` e, se interna, nella voce legacy
    `channels[part_id]` (sez. 3.3). Chiamare sotto STATE_LOCK."""
    parts, _ = midi_router.parts_from_state(state)
    t = part['part_id']
    parts[t] = copy.deepcopy(part)
    state['parts'] = parts
    if part['destination']['kind'] == 'internal':
        channels = state.get('channels')
        if not isinstance(channels, dict):
            channels = state['channels'] = {}
        entry = channels.get(str(t))
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(midi_router.legacy_channel_entry(part))
        channels[str(t)] = entry


def save_part(part):
    """Persistenza di una parte: `parts` + voce legacy `channels`, una sola
    scrittura atomica sotto STATE_LOCK (API per ui-persistence)."""
    with STATE_LOCK:
        state = get_last_state()
        _put_part_in_state(state, part)
        _write_state_atomic(state)


def midi_mode_info():
    configured = midi_router.configured_mode(get_last_state())
    return {'effective': EFFECTIVE_MIDI_MODE, 'configured': configured,
            'restart_required': configured != EFFECTIVE_MIDI_MODE}


def _part_id_or_none(t):
    return t if isinstance(t, int) and 0 <= t <= 15 else None


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

    # Parti: con `parts` valida le interne vanno via console, le esterne via
    # router (richiamo completo); senza `parts` resta il ripristino legacy.
    parts, source = midi_router.parts_from_state(state)
    for p in parts:
        if p['destination']['kind'] == 'external':
            try:
                report = router.apply_part(p, send='all')
                print(f"[Init] Parte esterna {p['part_id']}: {report['delivery']}")
            except Exception as e:
                print(f"[Init] Parte {p['part_id']} non applicata: {e}")
    if source == 'parts':
        for p in parts:
            if p['destination']['kind'] == 'internal':
                for _, cmd in midi_router.internal_console_commands(p, sf_id):
                    send_fluid(cmd)
        return

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
    """`chan` = ID parte; solo parti interne (sez. 8.1)."""
    if _part_id_or_none(chan) is None:
        return "Parte non valida", 400
    part = router.get_part(chan)
    if part['destination']['kind'] != 'internal':
        return "Parte esterna", 409
    part['sound'] = {'bank': bank, 'program': prog, 'name': part['sound'].get('name')}
    try:
        # select verso il canale di destinazione della parte, via console (D20)
        router.apply_part(part, send='all', cc_keys=[])
    except midi_router.RouterError as e:
        return f"Errore: {e.message}", 400
    save_part(part)
    notify_parts_changed([chan])
    print(f"[Channel] Saved part {chan}: bank={bank}, prog={prog}")
    return "OK"

@app.route('/set_effect/<type>/<val>')
def set_effect(type, val):
    if type not in ALLOWED_EFFECT_TYPES:
        return "Error", 400
    try:
        f_val = float(val)
        send_fluid(f"set synth.{type} {f_val}")
        save_state(type, f_val)
        return "OK"
    except (ValueError, TypeError) as e:
        print(f"[Effect] Invalid value type={type} val={val}: {e}")
        return "Error", 400

def _send_part_cc(chan, cc, val):
    """CC di una parte verso la sua destinazione + persistenza debounced."""
    delivery = router.send_part_cc(chan, cc, val)
    if str(cc) in midi_router.CC_KEYS:
        save_part_debounced(chan)
        notify_parts_changed([chan])
    return delivery

@app.route('/cc/<int:chan>/<int:cc>/<int:val>')
def control(chan, cc, val):
    if _part_id_or_none(chan) is None or not (0 <= cc <= 127 and 0 <= val <= 127):
        return "Error", 400
    _send_part_cc(chan, cc, val)
    return "OK"

@app.route('/reset_channel/<int:chan>')
def reset_channel(chan):
    """Reset di tutti i controller MIDI del canale (più veloce di singoli CC)"""
    if _part_id_or_none(chan) is None:
        return "Parte non valida", 400
    part = router.get_part(chan)
    if part['destination']['kind'] != 'internal':
        return "Parte esterna", 409
    send_fluid(f"reset {part['destination']['channel']}")
    return "OK"

@app.route('/refresh_status')
def refresh_status():
    data = send_fluid("channels")
    return jsonify({"raw": data})

@app.route('/get_state')
def get_state():
    """Restituisce tutto lo stato salvato (font, volumi, ecc)"""
    state = get_last_state()
    state['parts'] = router.get_parts()
    state['midi_mode'] = midi_mode_info()
    return jsonify(state)

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


def _daw_refusal(code, message, http=None):
    """Busta d'errore dall'ultimo rifiuto della DAW (`daw.last_error`)."""
    e = daw.last_error or {}
    return err(e.get('code', code), e.get('message', message), e.get('details'),
               http=e.get('http') or http)


def _transport_start_record(channel):
    """Logica comune HTTP/socket per Record. Ritorna (body, http)."""
    armed_channels = [ch for ch in range(16) if daw.armed.get(ch)]
    if not armed_channels:
        return err('NO_ARMED_TRACK', "Nessun canale armato. Attiva ARM su almeno un canale.")
    if channel is not None:
        if not isinstance(channel, int) or not 0 <= channel <= 15:
            return err('BAD_REQUEST', 'channel deve essere 0-15', {'field': 'channel'})
        if not daw.armed.get(channel):
            return err('NO_ARMED_TRACK', f"Canale {channel} non è armato. Attiva ARM prima di registrare.")
        success = daw.start_recording(channel)
        armed_channels = [channel]
    else:
        success = daw.start_recording()
    if success:
        return ok(f"Registrazione avviata su canali {armed_channels}"), 200
    return _daw_refusal('INTERNAL', 'Impossibile avviare registrazione', http=400)


def _transport_start_play():
    if daw.start_playback():
        return ok("Riproduzione avviata", warnings=daw.last_warnings), 200
    return _daw_refusal('NOTHING_TO_PLAY', "Nessuna traccia da riprodurre o già in riproduzione")


@app.route('/api/daw/record/start', methods=['POST'])
def daw_start_recording():
    """Avvia la registrazione su tutti i canali armati"""
    try:
        data = request.get_json(silent=True) or {}
        body, http = _transport_start_record(data.get('channel', None))
        return jsonify(body), http
    except Exception as e:
        print(f"[DAW API] ECCEZIONE in start_recording: {e}")
        import traceback
        traceback.print_exc()
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/record/stop', methods=['POST'])
def daw_stop_recording():
    """Ferma la registrazione in corso (chiude note/sustain aperti)"""
    try:
        if daw.stop_recording():
            return jsonify(ok("Registrazione fermata"))
        body, http = err('BAD_REQUEST', "Nessuna registrazione in corso")
        return jsonify(body), http
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/play/start', methods=['POST'])
def daw_start_playback():
    """Avvia la riproduzione di tutte le tracce"""
    try:
        body, http = _transport_start_play()
        return jsonify(body), http
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/play/stop', methods=['POST'])
def daw_stop_playback():
    """Ferma la riproduzione"""
    try:
        if daw.stop_playback():
            return jsonify(ok("Riproduzione fermata"))
        body, http = err('BAD_REQUEST', "Nessuna riproduzione in corso")
        return jsonify(body), http
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/stop_all', methods=['POST'])
def daw_stop_all():
    """Ferma tutto: registrazione e riproduzione"""
    try:
        daw.stop_all()
        return jsonify(ok("Tutto fermato"))
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/panic', methods=['POST'])
def daw_panic():
    """PANIC su tutte le destinazioni aperte (sez. 3.7)"""
    try:
        daw.panic()
        return jsonify(ok("PANIC inviato", ports=daw.last_panic))
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


@app.route('/api/daw/rewind', methods=['POST'])
def daw_rewind():
    """Riporta la timeline a 00:00:00"""
    try:
        daw.rewind()
        return jsonify(ok("Timeline resettata"))
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


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
        
        return jsonify(ok(f"Traccia {channel} {'mutata' if muted else 'smutata'}",
                          muted=daw.muted[channel]))  # Ritorna lo stato effettivo
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
        
        return jsonify(ok(f"Traccia {channel} solo {'attivato' if solo else 'disattivato'}",
                          solo=daw.solo[channel]))
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
            body, http = err('BAD_REQUEST', "channel must be 0-15", {'field': 'channel'})
            return jsonify(body), http

        note = max(0, min(127, note))
        velocity = max(1, min(127, velocity))
        length_ms = max(40, min(400, length_ms))

        # Destinazione della traccia via router; note-off sulla stessa porta/canale.
        if not router.preview_note(channel, note, velocity, length_ms):
            body, http = err('PORT_UNAVAILABLE', 'Destinazione della traccia non disponibile',
                             {'tracks': [{'track': channel,
                                          'status': router.destination_status(router.get_part(channel))}]})
            return jsonify(body), http
        return jsonify(ok())
    except Exception as e:
        print(f"[DAW] preview error: {e}")
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


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


@app.route('/api/daw/clip/resize', methods=['POST'])
def daw_resize_clip():
    """Non-destructive clip resize: change end_beat without deleting events."""
    try:
        data = request.json if isinstance(request.json, dict) else {}

        raw_channel = data.get('channel', None)
        raw_clip_id = data.get('clip_id', None)
        raw_new_end_beat = data.get('new_end_beat', None)

        try:
            channel = int(raw_channel)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid channel: must be an integer between 0 and 15"}), 400

        try:
            clip_id = int(raw_clip_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid clip_id: must be a non-negative integer"}), 400

        try:
            new_end_beat = float(raw_new_end_beat)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid new_end_beat: must be a number"}), 400

        if channel < 0 or channel > 15 or clip_id < 0:
            return jsonify({"status": "error", "message": "Invalid channel or clip_id"}), 400
        if new_end_beat < 0:
            return jsonify({"status": "error", "message": "Invalid new_end_beat: must be >= 0"}), 400

        result = daw.resize_clip(channel, clip_id, new_end_beat)
        if result is None:
            return jsonify({"status": "error", "message": "Cannot resize clip during recording/playback"}), 409
        if not result:
            return jsonify({"status": "error", "message": "Clip not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clip/split', methods=['POST'])
def daw_split_clip():
    """Split a clip at a given beat into two clips."""
    try:
        data = request.json if isinstance(request.json, dict) else {}

        raw_channel = data.get('channel', None)
        raw_clip_id = data.get('clip_id', None)
        raw_split_beat = data.get('split_beat', None)

        try:
            channel = int(raw_channel)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid channel: must be an integer between 0 and 15"}), 400

        try:
            clip_id = int(raw_clip_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid clip_id: must be a non-negative integer"}), 400

        try:
            split_beat = float(raw_split_beat)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid split_beat: must be a number"}), 400

        if channel < 0 or channel > 15 or clip_id < 0:
            return jsonify({"status": "error", "message": "Invalid channel or clip_id"}), 400
        if split_beat < 0:
            return jsonify({"status": "error", "message": "Invalid split_beat: must be >= 0"}), 400

        result = daw.split_clip(channel, clip_id, split_beat)
        if result is None:
            return jsonify({"status": "error", "message": "Cannot split clip during recording/playback"}), 409
        if result is False:
            return jsonify({"status": "error", "message": "Clip not found or split point outside clip"}), 404
        return jsonify({"status": "ok", "new_clip_id": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/clip/merge', methods=['POST'])
def daw_merge_clips():
    """Merge multiple clips on the same channel into one."""
    try:
        data = request.json if isinstance(request.json, dict) else {}

        raw_channel = data.get('channel', None)
        try:
            channel = int(raw_channel)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid channel: must be an integer between 0 and 15"}), 400

        clip_ids = data.get('clip_ids', [])

        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        if not isinstance(clip_ids, list) or len(clip_ids) < 2:
            return jsonify({"status": "error", "message": "Need at least 2 clip_ids"}), 400

        parsed_clip_ids = []
        for idx, cid in enumerate(clip_ids):
            try:
                parsed_clip_ids.append(int(cid))
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": f"Invalid clip_ids[{idx}]: must be an integer"}), 400

        result = daw.merge_clips(channel, parsed_clip_ids)
        if result is None:
            return jsonify({"status": "error", "message": "Cannot merge clips during recording/playback"}), 409
        if result is False:
            return jsonify({"status": "error", "message": "Not enough matching clips found"}), 404
        return jsonify({"status": "ok", "new_clip_id": result})
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
            body, http = err('BAD_REQUEST', "Missing or invalid beat", {'field': 'beat'})
            return jsonify(body), http
        success = daw.seek_to_beat(float(beat))
        if not success:
            body, http = err('RECORDING_ACTIVE', "Cannot seek during recording")
            return jsonify(body), http
        return jsonify(ok())
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


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


@app.route('/api/daw/track/<int:channel>/edit_note_pair', methods=['PUT'])
def daw_edit_note_pair(channel):
    """Atomically edit both note_on and note_off of a note (avoids index desync on sort)."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        data = request.get_json(force=True)
        on_idx = int(data.get('on_idx', -1))
        off_idx = int(data.get('off_idx', -1))
        allowed = ('on_beat', 'off_beat', 'note', 'velocity')
        updates = {k: data[k] for k in allowed if k in data}
        if not updates:
            return jsonify({"status": "error", "message": "No updates provided"}), 400
        ok = daw.edit_note_pair(channel, on_idx, off_idx, updates)
        if not ok:
            return jsonify({"status": "error", "message": "Cannot edit (recording or invalid index)"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/delete_note_pair', methods=['DELETE'])
def daw_delete_note_pair(channel):
    """Atomically delete both note_on and note_off events of a note."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        data = request.get_json(force=True)
        on_idx = int(data.get('on_idx', -1))
        off_idx = int(data.get('off_idx', -1))
        ok = daw.delete_note_pair(channel, on_idx, off_idx)
        if not ok:
            return jsonify({"status": "error", "message": "Cannot delete (recording or invalid index)"}), 400
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# SISTEMA, PORTE MIDI, PARTI (ui-routing, contracts sez. 8.2)
# ==========================================

def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _respond(body_http):
    body, http = body_http
    return jsonify(body), http


def _router_call(fn, *args):
    """Esegue un'operazione del registro: RouterError -> busta; errori disco -> DISK_*."""
    try:
        return fn(*args), None
    except midi_router.RouterError as e:
        return None, from_router_error(e)
    except OSError as e:
        code = 'DISK_FULL' if getattr(e, 'errno', None) == 28 else 'DISK_ERROR'
        return None, err(code, f'Scrittura registro fallita: {e}')


def _console_available():
    try:
        with socket.create_connection((FLUID_HOST, FLUID_PORT), timeout=0.5):
            return True
    except OSError:
        return False


@app.route('/api/system/status')
def api_system_status():
    state = get_last_state()
    internal = router.internal_status()
    soundfont = {'state': 'idle', 'file': state.get('font'), 'sf_id': sf_id, 'error': None,
                 'started_at': None, 'finished_at': None}
    return jsonify(ok(midi_mode=midi_mode_info(),
                      synth={'console': 'available' if _console_available() else 'missing',
                             'midi_port': internal,
                             'click': 'available' if router.internal_available() else internal},
                      soundfont=soundfont,
                      revision=getattr(daw, 'revision', None),
                      warnings=list(router.startup_warnings)))


@app.route('/api/system/mode', methods=['PUT'])
def api_system_mode():
    data = _json_body()
    mode = data.get('mode') if data else None
    if mode not in midi_router.MODES:
        return _respond(err('BAD_REQUEST', "mode deve essere 'legacy' o 'managed'", {'field': 'mode'}))
    try:
        with STATE_LOCK:
            state = get_last_state()
            state['midi_mode'] = mode
            _write_state_atomic(state)
    except OSError as e:
        return _respond(err('DISK_FULL' if getattr(e, 'errno', None) == 28 else 'DISK_ERROR', str(e)))
    info = midi_mode_info()
    msg = 'Riavvia il servizio per applicare la modalita' if info['restart_required'] else None
    return jsonify(ok(msg, midi_mode=info))


@app.route('/api/midi/ports')
def api_midi_ports():
    include_all = request.args.get('all') in ('1', 'true')
    return jsonify(ok(**router.describe_ports(include_all)))


@app.route('/api/midi/devices')
def api_midi_devices_list():
    return jsonify(ok(devices=router.describe_devices()))


@app.route('/api/midi/devices/<alias>', methods=['PUT'])
def api_midi_device_put(alias):
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    res, error = _router_call(router.set_device, alias, data)
    if error:
        return _respond(error)
    created, _ = res
    return jsonify(ok(device=router.describe_device(alias))), (201 if created else 200)


@app.route('/api/midi/devices/<alias>', methods=['DELETE'])
def api_midi_device_delete(alias):
    _, error = _router_call(router.delete_device, alias)
    if error:
        return _respond(error)
    return jsonify(ok())


@app.route('/api/midi/devices/<alias>/select', methods=['POST'])
def api_midi_device_select(alias):
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    device, error = _router_call(router.select_port, alias, data.get('direction'), data.get('port'))
    if error:
        return _respond(error)
    return jsonify(ok(device=device))


_parts_update_pending = set()
_parts_update_lock = threading.Lock()


def _flush_parts_update():
    with _parts_update_lock:
        ids = sorted(_parts_update_pending)
        _parts_update_pending.clear()
    if not ids:
        return
    parts = router.get_parts()
    socketio.emit('parts_update', {'parts': [parts[t] for t in ids],
                                   'runtime': [router.part_runtime(t, parts[t]) for t in ids],
                                   'conflicts': midi_router.compute_conflicts(parts)})


def notify_parts_changed(part_ids):
    """`parts_update` coalescato (max 10 Hz, ultimo valore vince)."""
    with _parts_update_lock:
        _parts_update_pending.update(part_ids)
    router.call_later(0.1, _flush_parts_update, key='parts_update')


@app.route('/api/parts')
def api_parts_list():
    parts = router.get_parts()
    return jsonify(ok(parts=parts,
                      runtime=[router.part_runtime(t, parts[t]) for t in range(16)],
                      conflicts=midi_router.compute_conflicts(parts),
                      midi_mode=midi_mode_info()))


@app.route('/api/parts/<int:t>')
def api_part_get(t):
    if _part_id_or_none(t) is None:
        return _respond(err('NOT_FOUND', f'Parte {t} inesistente', {'kind': 'part'}))
    part = router.get_part(t)
    return jsonify(ok(part=part, runtime=router.part_runtime(t, part)))


@app.route('/api/parts/<int:t>', methods=['PATCH'])
def api_part_patch(t):
    if _part_id_or_none(t) is None:
        return _respond(err('NOT_FOUND', f'Parte {t} inesistente', {'kind': 'part'}))
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    current = router.get_part(t)
    try:
        new = midi_router.merge_part_patch(current, data)
    except midi_router.RouterError as e:
        details = dict(e.details or {})
        if 'path' in details:
            details['path'] = f"parts[{t}].{details['path']}" if details['path'] else f'parts[{t}]'
        return _respond(err(e.code, e.message, details))
    if new['input'] != current['input'] and daw.is_recording:
        return _respond(err('RECORDING_ACTIVE', "Non si cambia l'ingresso durante la registrazione",
                            {'part_id': t}))
    try:
        applied = router.apply_part(new, send='diff')
    except midi_router.RouterError as e:
        return _respond(from_router_error(e))
    try:
        save_part(new)
    except OSError as e:
        return _respond(err('DISK_FULL' if getattr(e, 'errno', None) == 28 else 'DISK_ERROR',
                            f'Parte applicata ma non salvata: {e}'))
    parts = router.get_parts()
    runtime = router.part_runtime(t, parts[t])
    notify_parts_changed([t])
    return jsonify(ok(part=parts[t], runtime=runtime,
                      conflicts=midi_router.compute_conflicts(parts), applied=applied))


startup_init_once()

# ==========================================
# SOCKETIO EVENT HANDLERS (Hybrid approach)
# ==========================================


@socketio.on('cc_update')
def handle_cc_update(data):
    """CC dagli slider (fast path): `chan` = ID parte, inviato alla sua destinazione."""
    try:
        chan = int(data['chan'])
        cc_num = int(data['cc'])
        val = int(data['val'])
        if not (0 <= chan <= 15 and 0 <= cc_num <= 127 and 0 <= val <= 127):
            return ack_err('BAD_REQUEST', 'Values out of range')
        delivery = _send_part_cc(chan, cc_num, val)
        if delivery == 'failed':
            code = 'SYNTH_UNAVAILABLE' if router.get_part(chan)['destination']['kind'] == 'internal' \
                else 'PORT_UNAVAILABLE'
            return ack_err(code, 'Invio CC fallito', {'part_id': chan}, delivery=delivery)
        return ack_ok(delivery=delivery)
    except Exception as e:
        print(f"[WS] cc_update error: {e}")
        return ack_err('BAD_REQUEST', str(e))


@socketio.on('effect_update')
def handle_effect_update(data):
    """Handle effect changes via WebSocket for lower latency"""
    try:
        etype = str(data['type'])
        if etype not in ALLOWED_EFFECT_TYPES:
            return ack_err('BAD_REQUEST', f'Invalid effect type: {etype}', {'field': 'type'})
        val = float(data['val'])
        send_fluid(f"set synth.{etype} {val}")
        save_state(etype, val)
        return ack_ok()
    except Exception as e:
        print(f"[WS] effect_update error: {e}")
        return ack_err('BAD_REQUEST', str(e))


@socketio.on('transport_cmd')
def handle_transport_cmd(data):
    """Comandi di trasporto via WebSocket: ack con gli stessi `code` delle route HTTP."""
    try:
        cmd = data.get('cmd')
        if cmd == 'play_start':
            return _transport_start_play()[0]
        if cmd == 'record_start':
            return _transport_start_record(data.get('channel'))[0]
        if cmd == 'play_stop':
            return ack_ok() if daw.stop_playback() else ack_err('BAD_REQUEST', 'Nessuna riproduzione in corso')
        if cmd == 'record_stop':
            return ack_ok() if daw.stop_recording() else ack_err('BAD_REQUEST', 'Nessuna registrazione in corso')
        if cmd == 'stop_all':
            daw.stop_all()
            return ack_ok()
        if cmd == 'panic':
            daw.panic()
            return ack_ok(ports=daw.last_panic)
        if cmd == 'rewind':
            daw.rewind()
            return ack_ok()
        if cmd == 'seek':
            position = data.get('position')
            if position is None or not isinstance(position, (int, float)):
                return ack_err('BAD_REQUEST', 'Missing or invalid position', {'field': 'position'})
            if not daw.set_position(position):
                return ack_err('TRANSPORT_ACTIVE', 'Cannot seek while playing or recording',
                               {'is_playing': daw.is_playing, 'is_recording': daw.is_recording})
            return ack_ok()
        if cmd == 'seek_beat':
            beat = data.get('beat')
            if beat is None or not isinstance(beat, (int, float)):
                return ack_err('BAD_REQUEST', 'Missing or invalid beat', {'field': 'beat'})
            if not daw.seek_to_beat(float(beat)):
                return ack_err('RECORDING_ACTIVE', 'Cannot seek during recording')
            return ack_ok()
        if cmd in ('undo', 'redo'):
            if (daw.undo() if cmd == 'undo' else daw.redo()):
                return ack_ok()
            if daw.is_playing or daw.is_recording:
                return ack_err('TRANSPORT_ACTIVE', f'{cmd} non disponibile durante Play/Record',
                               {'is_playing': daw.is_playing, 'is_recording': daw.is_recording})
            return ack_err('NOTHING_TO_UNDO', f'Niente da {"annullare" if cmd == "undo" else "ripristinare"}')
        if cmd == 'toggle_loop':
            daw.toggle_loop()
            return ack_ok()
        return ack_err('BAD_REQUEST', f'Unknown command: {cmd}', {'field': 'cmd'})
    except Exception as e:
        print(f"[WS] transport_cmd error: {e}")
        return ack_err('INTERNAL', str(e))


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
