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
import shutil
import midi_router
import project_store as ps
from api_common import ok, err, ack_ok, ack_err, from_router_error
from daw_recorder import MultiTrackDAW, DawError

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
PROJECTS_DIR = '/home/matteo/matsynth_web/projects/'  # Progetti nativi .matsynth.json (ui-persistence)
LIBRARY_FILE = '/home/matteo/matsynth_web/library.json'  # Preferiti/ordine scene, fuori da PRESETS_DIR
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

# Crea la directory dei progetti se non esiste
if not os.path.exists(PROJECTS_DIR):
    os.makedirs(PROJECTS_DIR)

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


# ==========================================
# STATI SCENA / PROGETTO / SOUNDFONT (ui-persistence, contracts 4.6, 4.7, 5.6)
# ==========================================
# Le notifiche partono solo quando un campo cambia (mai a ogni CC) e fuori
# dai lock degli stati; la scena corrente e' persistita debounced.

def _on_scene_state(public):
    socketio.emit('scene_state', public)
    save_state_debounced('scene', public)


scene_state = ps.SceneState(on_change=_on_scene_state)
scene_state.restore(get_last_state().get('scene'))
project_state = ps.ProjectState(on_change=lambda pub: socketio.emit('project_state', pub))
sf_state = ps.SoundfontState(on_change=lambda pub: socketio.emit('soundfont_status', pub))
# Ogni mutazione del contenuto DAW rende il progetto modificato (5.6).
daw.on_content_change = project_state.mark_dirty


def _sound_changed():
    """Modifica del suono (parte, globali, SoundFont): scena e progetto
    modificati (4.7, 5.6). Chiamare nei punti sincroni, mai dal flush differito."""
    scene_state.mark_dirty()
    project_state.mark_dirty()


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

    # Se abbiamo un font salvato, prova a caricarlo. Lo stato SoundFont (4.6)
    # diventa `loaded` solo dopo la verifica con `fonts`.
    font_name = state.get('font')
    if font_name:
        path = os.path.join(SF2_DIR, font_name)
        if os.path.exists(path):
            sf_state.begin(font_name)
            _run_soundfont_load(font_name, mark_dirty=False)
            print(f"[Init] Restored font {font_name}: {sf_state.public()['state']}")
        else:
            print(f"[Init] Saved font not found: {path}")
            sf_state.finish('failed', font_name, None, 'File SoundFont non trovato')
    else:
        sf_state.finish('idle' if send_fluid("fonts").strip() else 'unknown')

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
    """Route legacy (settings.html): sincrona e testuale, ma aggiorna lo stesso
    stato SoundFont della route nuova (4.6), con verifica tramite `fonts`."""
    filename = os.path.basename(filename)
    if not sf_state.begin(filename):
        return "Caricamento SoundFont gia' in corso", 409
    res = _run_soundfont_load(filename)
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
        _sound_changed()
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
    state['scene'] = scene_state.public()
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
                'pan': channel_data.get('pan', 64),          # additivo (4.4)
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
                    with open(preset_path, 'r', encoding='utf-8') as f:
                        preset_data = json.load(f)
                    if not isinstance(preset_data, dict):
                        continue
                    if ps.classify_preset(preset_data) == 'v1':
                        # Scena v1 nella stessa libreria (4.2): font = soundfont.file
                        sf = preset_data.get('soundfont')
                        font = sf.get('file', 'N/A') if isinstance(sf, dict) else 'N/A'
                    else:
                        font = preset_data.get('font', 'N/A')
                    presets.append({
                        'filename': filename,
                        'name': preset_data.get('name', filename[:-5]),
                        'created': preset_data.get('created', 'Unknown'),
                        'font': font
                    })
                except (json.JSONDecodeError, IOError, UnicodeDecodeError):
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
        preset_path = os.path.join(PRESETS_DIR, os.path.basename(filename))

        if not os.path.exists(preset_path):
            return jsonify({'status': 'error', 'message': 'Preset non trovato'}), 404

        with open(preset_path, 'r', encoding='utf-8') as f:
            preset_data = json.load(f)

        if ps.classify_preset(preset_data) == 'v1':
            # presets.html conosce solo la forma legacy: adattatore inverso (4.2)
            try:
                ps.validate_scene(preset_data)
            except ps.ContractError as e:
                return jsonify({'status': 'error', 'message': f'Scena non valida: {e.path} ({e.reason})'}), 422
            preset_data = ps.scene_to_legacy_preset(preset_data)

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
        
        old_path = os.path.join(PRESETS_DIR, os.path.basename(str(old_filename)))

        if not os.path.exists(old_path):
            return jsonify({'status': 'error', 'message': 'Preset non trovato'}), 404

        # Carica il preset esistente
        with open(old_path, 'r', encoding='utf-8') as f:
            preset_data = json.load(f)

        if ps.classify_preset(preset_data) == 'v1':
            # Scena v1: il nome file e' l'ID (stabile, usato da preferiti e
            # scena corrente): cambia solo `name`, il file resta (4.2, A10b).
            old_stem = os.path.basename(old_path)[:-5]
            taken = _scene_name_taken(new_name, exclude_id=old_stem)
            if taken:
                body, http = err('NAME_EXISTS', f'Nome gia\' usato: "{new_name}"', {'existing_id': taken})
                return jsonify(body), http
            preset_data['name'] = str(new_name)[:64]
            ps.atomic_write_json(old_path, preset_data)
            _scene_renamed(old_stem, old_stem, preset_data['name'])
            return jsonify({'status': 'ok', 'ok': True, 'message': f'Preset rinominato in "{new_name}"',
                            'new_filename': os.path.basename(old_path)})

        # Aggiorna il nome
        preset_data['name'] = new_name

        # Crea il nuovo filename
        safe_filename = "".join(c for c in new_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_filename = safe_filename.replace(' ', '_')

        if not safe_filename:
            safe_filename = f"preset_{int(time.time())}"

        new_path = os.path.join(PRESETS_DIR, f"{safe_filename}.json")

        # A10b: mai sovrascrivere un altro preset esistente.
        if os.path.normcase(os.path.abspath(new_path)) != os.path.normcase(os.path.abspath(old_path)) \
                and os.path.exists(new_path):
            body, http = err('NAME_EXISTS', f'Esiste gia\' un preset "{safe_filename}.json"',
                             {'existing_id': safe_filename})
            return jsonify(body), http

        # Salva con il nuovo nome
        with open(new_path, 'w') as f:
            json.dump(preset_data, f, indent=2)

        # Rimuovi il vecchio file solo se il nome è diverso
        if os.path.normcase(os.path.abspath(new_path)) != os.path.normcase(os.path.abspath(old_path)):
            os.remove(old_path)
            _scene_renamed(os.path.basename(old_path)[:-5], safe_filename, new_name)

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

        # 2. Applica le impostazioni per ogni canale (semantica legacy invariata:
        # solo i canali elencati, solo i campi presenti, comandi via console).
        channels = preset_data.get('channels', [])
        changed = {}
        for ch_data in channels:
            chan = ch_data.get('channel')
            if chan is None:
                continue

            # Seleziona lo strumento
            bank = ch_data.get('bank')
            prog = ch_data.get('program')
            if bank is not None and prog is not None:
                send_fluid(f"select {chan} {sf_id} {bank} {prog}")

            # Applica i CC (pan incluso se presente: A2)
            if 'volume' in ch_data:
                send_fluid(f"cc {chan} 7 {ch_data['volume']}")
            if 'pan' in ch_data:
                send_fluid(f"cc {chan} 10 {ch_data['pan']}")
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

            part = _legacy_channel_to_part(chan, ch_data, changed)
            if part is not None:
                changed[chan] = part

        # 3. Stato effettivo persistito (A2): parti interne applicate nel
        # router (send='none': i comandi sono gia' partiti) + parts/channels
        # + globali in UNA scrittura. Il flush debounced degli slider legge
        # il router, quindi non puo' piu' riportare indietro il preset.
        for part in changed.values():
            router.apply_part(part, send='none')
        _persist_sound(parts=list(changed.values()),
                       globals_={'gain': gain, 'reverb_level': reverb, 'chorus_level': chorus})
        if changed:
            notify_parts_changed(sorted(changed))
        else:
            _sound_changed()

        return jsonify({'status': 'ok', 'ok': True, 'message': 'Preset applicato con successo'})
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

def _expected_revision(data):
    """`expected_revision` facoltativa (sez. 6.6, D13): assente = nessun controllo."""
    v = data.get('expected_revision') if isinstance(data, dict) else None
    if v is None:
        return None, None
    if not isinstance(v, int) or isinstance(v, bool):
        return None, err('BAD_REQUEST', 'expected_revision: intero atteso', {'field': 'expected_revision'})
    return v, None


def _daw_edit_result(success, fallback_msg, **payload):
    """Esito di una mutazione DAW: `revision` letta sotto il lock alla fine
    della mutazione di QUESTA richiesta (daw.last_revision, per-thread)."""
    if success:
        return jsonify(ok(revision=daw.last_revision, **payload))
    e = daw.last_error or {}
    code = e.get('code', 'BAD_REQUEST')
    http = e.get('http') or (None if code in api_common_codes_new else 400)
    return _respond(err(code, e.get('message', fallback_msg), e.get('details'), http=http))


# Codici che seguono la tabella 2.3 anche sulle route esistenti (condizioni nuove).
api_common_codes_new = frozenset(('REVISION_CONFLICT',))


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
    # M2: HTTP dalla tabella 2.3 (503 PORT_UNAVAILABLE, 409 TRANSPORT_ACTIVE);
    # l'override legacy "gia' in Record" (400) arriva da daw.last_error['http'].
    return _daw_refusal('INTERNAL', 'Impossibile avviare registrazione')


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
            return jsonify(ok("Registrazione fermata", revision=daw.last_revision))
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
        data = request.get_json(silent=True) or {}
        bpm = data.get('bpm', 120)
        if not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or bpm != bpm:
            return _respond(err('BAD_REQUEST', 'bpm: numero atteso', {'field': 'bpm'}))

        success = daw.set_bpm(bpm)

        if success:
            return jsonify(ok(f"BPM impostato a {daw.bpm}", bpm=daw.bpm))
        return _respond(err('TRANSPORT_ACTIVE', "Impossibile cambiare BPM durante registrazione/riproduzione",
                            {'is_playing': daw.is_playing, 'is_recording': daw.is_recording}, http=400))

    except Exception as e:
        return _respond(err('INTERNAL', str(e)))


@app.route('/api/daw/time_signature', methods=['POST'])
def daw_set_time_signature():
    """Imposta il numeratore della misura (3/4 o 4/4)."""
    try:
        data = request.json or {}
        beats_per_measure = int(data.get('beats_per_measure', 4))
        success = daw.set_time_signature(beats_per_measure)
        if success:
            return jsonify(ok(beats_per_measure=beats_per_measure))
        return _respond(err('BAD_REQUEST', "Time signature non supportato", {'field': 'beats_per_measure'}))
    except Exception as e:
        return _respond(err('BAD_REQUEST', str(e), {'field': 'beats_per_measure'}))


@app.route('/api/daw/metronome/toggle', methods=['POST'])
def daw_toggle_metronome():
    """Toggle metronomo on/off"""
    try:
        enabled = daw.toggle_metronome()
        return jsonify(ok(f"Metronomo {'abilitato' if enabled else 'disabilitato'}", enabled=enabled))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/loop_points', methods=['POST'])
def daw_set_loop_points():
    """Imposta i punti di loop (in beat)"""
    try:
        data = request.json or {}
        start_beat = float(data.get('start_beat', 0))
        end_beat = float(data.get('end_beat', 0))
        if not (0 <= start_beat <= ps.MAX_BEAT and 0 <= end_beat <= ps.MAX_BEAT):
            return _respond(err('BAD_REQUEST', f'beat 0..{ps.MAX_BEAT}', {'field': 'start_beat'}))
        success = daw.set_loop_points(start_beat, end_beat)
        if success:
            return jsonify(ok(loop_start=start_beat, loop_end=end_beat))
        return _respond(err('BAD_REQUEST', "end_beat deve essere > start_beat", {'field': 'end_beat'}))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/loop/toggle', methods=['POST'])
def daw_toggle_loop():
    """Attiva/disattiva il loop"""
    try:
        enabled = daw.toggle_loop()
        return jsonify(ok(f"Loop {'attivato' if enabled else 'disattivato'}", enabled=enabled))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/undo', methods=['POST'])
def daw_undo():
    """Annulla l'ultima operazione distruttiva"""
    try:
        success = daw.undo()
        if success:
            return jsonify(ok("Undo eseguito", revision=daw.last_revision))
        if daw.is_playing or daw.is_recording:
            return _respond(err('TRANSPORT_ACTIVE', "undo non disponibile durante Play/Record",
                                {'is_playing': daw.is_playing, 'is_recording': daw.is_recording}, http=400))
        return _respond(err('NOTHING_TO_UNDO', "Niente da annullare"))
    except Exception as e:
        return _respond(err('INTERNAL', str(e)))


@app.route('/api/daw/redo', methods=['POST'])
def daw_redo():
    """Ripristina l'ultima operazione annullata"""
    try:
        success = daw.redo()
        if success:
            return jsonify(ok("Redo eseguito", revision=daw.last_revision))
        if daw.is_playing or daw.is_recording:
            return _respond(err('TRANSPORT_ACTIVE', "redo non disponibile durante Play/Record",
                                {'is_playing': daw.is_playing, 'is_recording': daw.is_recording}, http=400))
        return _respond(err('NOTHING_TO_UNDO', "Niente da ripristinare"))
    except Exception as e:
        return _respond(err('INTERNAL', str(e)))


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
            return _respond(err('TRANSPORT_ACTIVE', "Impossibile quantizzare durante la registrazione o il playback",
                                {'is_playing': daw.is_playing, 'is_recording': daw.is_recording}))

        if not result:
            return jsonify(ok("Nessuna traccia con dati da quantizzare", events_modified=0,
                              channels_processed=[], revision=daw.last_revision))

        total = sum(result.values())
        return jsonify(ok(f"Quantizzazione: {total} note su {len(result)} tracce",
                          channels_processed=list(result.keys()), events_modified=total,
                          revision=daw.last_revision))
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
        expected, bad = _expected_revision(data)
        if bad:
            return _respond(bad)

        modified = daw.transpose_notes(channel, semitones, start_beat, end_beat, expected_revision=expected)
        if modified is None:
            return _respond(err('TRANSPORT_ACTIVE', "Cannot transpose during recording/playback",
                                {'is_playing': daw.is_playing, 'is_recording': daw.is_recording}))
        if modified is False:
            return _daw_edit_result(False, 'Revisione obsoleta')

        return jsonify(ok(channel=channel, semitones=semitones, modified=modified,
                          revision=daw.last_revision))
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
        clips, rev = daw.get_clips_with_revision(channel)
        return jsonify(ok(clips=clips, revision=rev))
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
        name = data.get('name', '')
        name = name.strip() if isinstance(name, str) else ''
        if not name:
            return _respond(err('BAD_REQUEST', "Nome obbligatorio", {'field': 'name'}))
        if not 0 <= channel <= 15:
            return _respond(err('BAD_REQUEST', "Traccia non valida", {'field': 'channel'}))
        return _daw_edit_result(daw.rename_track(channel, name), 'Rinomina fallita',
                                name=daw.track_names[channel])
    except Exception as e:
        return _respond(err('INTERNAL', str(e)))


@app.route('/api/daw/track/<int:channel>/color', methods=['POST'])
def daw_set_track_color(channel):
    """Set track color palette index."""
    try:
        data = request.json or {}
        try:
            color_index = int(data.get('color_index', 0))
        except (TypeError, ValueError):
            return _respond(err('BAD_REQUEST', "color_index: intero 0..15", {'field': 'color_index'}))
        if not 0 <= channel <= 15:
            return _respond(err('BAD_REQUEST', "Traccia non valida", {'field': 'channel'}))
        return _daw_edit_result(daw.set_track_color(channel, color_index), 'Colore non impostato',
                                color_index=daw.track_colors[channel])
    except Exception as e:
        return _respond(err('INTERNAL', str(e)))


@app.route('/api/daw/full_density_map')
def daw_full_density_map():
    """Ritorna la mappa di densità completa per tutte le tracce."""
    try:
        density, rev = daw.get_full_density_map_with_revision()
        return jsonify(ok(map=density, revision=rev))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/full_note_map')
def daw_full_note_map():
    """Return note-level data for piano-roll rendering."""
    try:
        note_map, rev = daw.get_full_note_map_with_revision()
        return jsonify(ok(map=note_map, revision=rev))
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
        events, rev = daw.get_events_decoded_with_revision(channel)
        return jsonify(ok(events=events, revision=rev))
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
            return _respond(err('BAD_REQUEST', "No updates provided", {'field': 'updates'}))
        expected, bad = _expected_revision(data)
        if bad:
            return _respond(bad)
        return _daw_edit_result(daw.edit_event(channel, index, updates, expected_revision=expected),
                                "Cannot edit (recording or invalid index)")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/daw/track/<int:channel>/events/<int:index>', methods=['DELETE'])
def daw_delete_event(channel, index):
    """Delete an event by index."""
    try:
        if channel < 0 or channel > 15:
            return jsonify({"status": "error", "message": "Invalid channel"}), 400
        expected, bad = _expected_revision(request.get_json(silent=True) or {})
        if bad:
            return _respond(bad)
        return _daw_edit_result(daw.delete_event(channel, index, expected_revision=expected),
                                "Cannot delete (recording or invalid index)")
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
            return _respond(err('BAD_REQUEST', "Note/velocity out of range", {'field': 'note'}))
        expected, bad = _expected_revision(data)
        if bad:
            return _respond(bad)
        return _daw_edit_result(daw.add_note(channel, note, velocity, start_beat, length_beats,
                                             expected_revision=expected),
                                "Cannot add note (recording?)")
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
            return _respond(err('BAD_REQUEST', "No updates provided", {'field': 'updates'}))
        expected, bad = _expected_revision(data)
        if bad:
            return _respond(bad)
        return _daw_edit_result(daw.edit_note_pair(channel, on_idx, off_idx, updates,
                                                   expected_revision=expected),
                                "Cannot edit (recording or invalid index)")
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
        expected, bad = _expected_revision(data)
        if bad:
            return _respond(bad)
        return _daw_edit_result(daw.delete_note_pair(channel, on_idx, off_idx, expected_revision=expected),
                                "Cannot delete (recording or invalid index)")
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
    internal = router.internal_status()
    soundfont = sf_state.public()
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


def notify_parts_changed(part_ids, sound_change=True):
    """`parts_update` coalescato (max 10 Hz, ultimo valore vince).
    `sound_change`: la modifica rende scena e progetto modificati, in modo
    sincrono (non nel flush differito); il richiamo scena passa False."""
    if sound_change:
        _sound_changed()
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


# ==========================================
# PERSISTENZA: SCENE, PROGETTI, SOUNDFONT (ui-persistence, contracts sez. 4, 5, 8.2)
# ==========================================
# Route sottili: schema/validazione/I/O in project_store.py. Nessun lock tenuto
# durante I/O verso disco, console FluidSynth o porte; lo snapshot della DAW si
# prende sotto il suo lock e si serializza fuori.

def _store_error(e, doc=None, supported=1):
    """ContractError/StoreError -> busta d'errore (sez. 2.1/2.3)."""
    if isinstance(e, ps.ContractError):
        details = e.details()
        doc = getattr(e, 'doc', doc)
        if e.code == 'UNSUPPORTED_VERSION':
            details['found'] = doc.get(e.path) if isinstance(doc, dict) else None
            details['supported'] = supported
            return err(e.code, f'Versione o formato non supportati ({e.path}: {e.reason})', details)
        return err(e.code, f'Documento non valido: {e.path or "radice"} ({e.reason})', details)
    if isinstance(e, ps.StoreError):
        return err(e.code, e.message, e.details)
    if isinstance(e, DawError):
        return err(e.code, e.message, e.details, http=e.http)
    return err('INTERNAL', str(e))


def _bool_field(data, key, default=False):
    v = data.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(key)
    return v


def _transport_busy():
    if daw.is_playing or daw.is_recording:
        return err('TRANSPORT_ACTIVE', 'Ferma Play/Record prima di continuare',
                   {'is_playing': daw.is_playing, 'is_recording': daw.is_recording})
    return None


def _current_globals():
    st = get_last_state()
    return {'gain': st.get('gain', 1.0), 'reverb_level': st.get('reverb.level', 0.4),
            'chorus_level': st.get('chorus.level', 0.4)}


def _current_sound():
    """Snapshot sonoro corrente autoritativo (4.4): parti del router, SoundFont
    realmente caricato (stato 4.6), globali dello stato."""
    f = sf_state.loaded_file()
    return {'soundfont': {'file': f} if f else None, 'globals': _current_globals(),
            'parts': router.get_parts()}


def _persist_sound(parts=None, globals_=None, scene=None):
    """Parti (parts + channels legacy), globali e scena corrente in UNA
    scrittura atomica sotto STATE_LOCK (4.5 punto 4)."""
    with STATE_LOCK:
        state = get_last_state()
        for p in parts or []:
            _put_part_in_state(state, p)
        if globals_ is not None:
            state['gain'] = globals_['gain']
            state['reverb.level'] = globals_['reverb_level']
            state['chorus.level'] = globals_['chorus_level']
        if scene is not None:
            state['scene'] = scene
        _write_state_atomic(state)


def _legacy_channel_to_part(chan, ch_data, changed):
    """Voce `channels[k]` di un preset legacy -> parte interna aggiornata con i
    soli campi presenti (None se la parte e' esterna o il canale non e' valido).
    I preset legacy erano solo interni: le parti esterne non vengono toccate."""
    if not isinstance(chan, int) or isinstance(chan, bool) or not 0 <= chan <= 15:
        return None
    part = changed.get(chan) or router.get_part(chan)
    if part['destination']['kind'] != 'internal':
        return None
    bank, prog = ch_data.get('bank'), ch_data.get('program')
    if isinstance(bank, int) and isinstance(prog, int) and 0 <= bank <= 16383 and 0 <= prog <= 127:
        name = ch_data.get('instrument_name')
        part['sound'] = {'bank': bank, 'program': prog,
                         'name': name[:64] if isinstance(name, str) and name and name != 'Not Set' else None}
    for field, key in ps.LEGACY_CC_FIELDS:
        v = ch_data.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 127:
            part['cc'][key] = v
    try:
        midi_router.validate_part(part, chan)
    except midi_router.RouterError:
        return None
    return part


# ── SoundFont (4.6) ─────────────────────────────────────────────────────

def _sf_files():
    if not os.path.isdir(SF2_DIR):
        return []
    return sorted(f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3')))


def _run_soundfont_load(filename, mark_dirty=True):
    """Carica `filename` (stato gia' `loading`): unload di tutti i font, load,
    poi verifica con `fonts`. `loaded` SOLO se l'output contiene il file
    (nessun ritardo fisso usato come verifica). Nessun lock tenuto qui.
    Ritorna la risposta della console a `load`."""
    global sf_id
    path = os.path.join(SF2_DIR, filename)
    raw = send_fluid("fonts")
    for fid, _ in ps.parse_fonts_output(raw):
        send_fluid(f"unload {fid}")
    res = send_fluid(f"load {path}")
    check = send_fluid("fonts")
    loaded_id = ps.verify_font_loaded(check, path)
    if loaded_id is not None:
        sf_id = loaded_id
        save_state('font', filename)          # la chiave `font` si aggiorna solo a `loaded`
        sf_state.finish('loaded', filename, loaded_id)
        if mark_dirty:
            _sound_changed()
    elif not check.strip() and not raw.strip():
        sf_state.finish('unknown', filename, None, 'Console FluidSynth non raggiungibile')
    else:
        # Verifica non riuscita: stato `failed`, chiave `font` invariata, ma
        # sf_id segue comunque il primo font elencato (come la route legacy):
        # altrimenti ogni `select` successivo punterebbe a un ID scaricato.
        fonts = ps.parse_fonts_output(check)
        if fonts:
            sf_id = fonts[0][0]
        sf_state.finish('failed', filename, None, 'FluidSynth non riporta il SoundFont tra i caricati')
    return res


@app.route('/api/soundfonts')
def api_soundfonts():
    return jsonify(ok(files=_sf_files(), current=sf_state.public()))


@app.route('/api/soundfont/status')
def api_soundfont_status():
    return jsonify(ok(soundfont=sf_state.public()))


@app.route('/api/soundfont/load', methods=['POST'])
def api_soundfont_load():
    data = _json_body() or {}
    f = data.get('file')
    if not isinstance(f, str) or not f or os.path.basename(f) != f:
        return _respond(err('BAD_REQUEST', 'file: nome base del SoundFont atteso', {'field': 'file'}))
    if f not in _sf_files():
        return _respond(err('NOT_FOUND', f'SoundFont non trovato: {f}', {'kind': 'soundfont'}))
    busy = _transport_busy()
    if busy:
        return _respond(busy)
    if not sf_state.begin(f):
        return _respond(err('SOUNDFONT_LOADING', 'Caricamento SoundFont gia\' in corso'))
    # Thread dedicato, sequenziale: nessun lock DAW/stato tenuto durante il caricamento.
    threading.Thread(target=_run_soundfont_load, args=(f,), daemon=True).start()
    return jsonify(ok('Caricamento avviato', job=sf_state.public())), 202


# ── Scene (4.1-4.5, 4.7) ────────────────────────────────────────────────

def _library_path():
    return LIBRARY_FILE


def _load_library():
    return ps.load_library(_library_path())


def _save_library(lib):
    ps.atomic_write_json(_library_path(), lib)


def _scene_file(scene_id):
    return os.path.join(PRESETS_DIR, f'{scene_id}.json')


def _read_scene(scene_id):
    """(scena v1, source, defaulted, raw). Solleva StoreError NOT_FOUND o
    ContractError. Lettura pura: un file legacy non viene MAI riscritto."""
    if not isinstance(scene_id, str) or not ps.ID_RE.match(scene_id):
        raise ps.StoreError('NOT_FOUND', 'Scena non trovata', {'kind': 'scene'})
    path = _scene_file(scene_id)
    if not os.path.isfile(path):
        raise ps.StoreError('NOT_FOUND', 'Scena non trovata', {'kind': 'scene'})
    raw = ps.read_json_file(path, ps.MAX_SCENE_BYTES)
    try:
        kind = ps.classify_preset(raw)
        if kind == 'v1':
            ps.validate_scene(raw)
            if raw['id'] != scene_id:
                raise ps.ContractError('VALIDATION_FAILED', 'id', 'diverso dal nome del file')
            return raw, 'v1', [], raw
        if kind == 'legacy':
            scene, defaulted = ps.migrate_legacy_preset(raw, scene_id)
            return scene, 'legacy', defaulted, raw
        raise ps.ContractError('VALIDATION_FAILED', '', 'formato non riconosciuto')
    except ps.ContractError as e:
        e.doc = raw                     # per details.found di UNSUPPORTED_VERSION
        raise


def _scene_entries():
    entries = []
    if not os.path.isdir(PRESETS_DIR):
        return entries
    for fn in sorted(os.listdir(PRESETS_DIR)):
        if not fn.endswith('.json') or not os.path.isfile(os.path.join(PRESETS_DIR, fn)):
            continue
        sid = fn[:-5]
        entry = {'id': sid, 'name': sid, 'created': None, 'modified': None, 'soundfont': None,
                 'source': None, 'valid': True}
        try:
            if not ps.ID_RE.match(sid):
                raise ps.ContractError('VALIDATION_FAILED', 'id', 'nome file non valido come ID')
            scene, source, _, _ = _read_scene(sid)
            entry.update(name=scene['name'], created=scene['created'], modified=scene['modified'],
                         soundfont=scene['soundfont'], source=source)
        except (ps.ContractError, ps.StoreError) as e:
            code = e.code if isinstance(e, (ps.ContractError, ps.StoreError)) else 'VALIDATION_FAILED'
            entry['valid'] = False
            entry['error'] = {'code': code, 'message': str(e),
                              'details': e.details() if isinstance(e, ps.ContractError) else e.details}
        entries.append(entry)
    return entries


def _scene_name_taken(name, exclude_id=None):
    """ID della scena (valida) che usa gia' `name` (confronto case-insensitive)."""
    key = str(name).casefold()
    for e in _scene_entries():
        if e['id'] != exclude_id and e['valid'] and str(e['name']).casefold() == key:
            return e['id']
    return None


def _scene_renamed(old_id, new_id, new_name):
    """Aggiorna libreria e scena corrente dopo una rinomina (route legacy)."""
    if old_id != new_id:
        lib = _load_library()
        if old_id in lib['scene_favorites'] or old_id in lib['scene_order']:
            lib['scene_favorites'] = [new_id if x == old_id else x for x in lib['scene_favorites']]
            lib['scene_order'] = [new_id if x == old_id else x for x in lib['scene_order']]
            try:
                _save_library(lib)
            except ps.StoreError as e:
                print(f'[Scene] libreria non aggiornata: {e.message}')
    cur = scene_state.public()
    if cur['scene_id'] == old_id:
        scene_state.set(new_id, new_name, cur['dirty'])


def _backup_legacy(scene_id):
    """Prima di riscrivere un preset legacy: copia in .legacy_backup/ (solo se
    la copia non esiste gia': il primo originale resta)."""
    src = _scene_file(scene_id)
    bdir = os.path.join(PRESETS_DIR, '.legacy_backup')
    dst = os.path.join(bdir, os.path.basename(src))
    if os.path.exists(dst):
        return
    try:
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        raise ps.StoreError('DISK_FULL' if getattr(e, 'errno', None) == 28 else 'DISK_ERROR',
                            f'Backup del preset legacy fallito: {e}')


def _scene_meta(scene_id):
    lib = _load_library()
    for e in ps.sort_scene_entries(_scene_entries(), lib):
        if e['id'] == scene_id:
            return e
    return None


def _capture_scene(scene_id, name, created=None):
    """Cattura lo stato corrente (4.4). Ritorna (scena, warnings)."""
    snd = _current_sound()
    warnings = []
    if snd['soundfont'] is None:
        warnings.append({'code': 'SOUNDFONT_UNSPECIFIED',
                         'message': 'Nessun SoundFont verificato come caricato: scena salvata senza SoundFont',
                         'details': {'soundfont_state': sf_state.public()['state']}})
    scene = ps.make_scene(scene_id, name, snd['parts'],
                          snd['soundfont']['file'] if snd['soundfont'] else None,
                          snd['globals'], created=created)
    return scene, warnings


def _scene_name_arg(data):
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        return None
    return name.strip()


@app.route('/api/scenes')
def api_scenes_list():
    entries = ps.sort_scene_entries(_scene_entries(), _load_library())
    return jsonify(ok(scenes=entries, current=scene_state.public()))


@app.route('/api/scenes/current')
def api_scenes_current():
    return jsonify(ok(scene_state=scene_state.public()))


@app.route('/api/scenes/<scene_id>')
def api_scene_get(scene_id):
    try:
        scene, source, defaulted, raw = _read_scene(scene_id)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e, None))
    return jsonify(ok(scene=scene, source=source, warnings=ps.defaulted_warnings(defaulted)))


@app.route('/api/scenes', methods=['POST'])
def api_scene_save_as():
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    name = _scene_name_arg(data)
    if name is None:
        return _respond(err('BAD_REQUEST', 'name: 1..64 caratteri', {'field': 'name'}))
    overwrite_id = data.get('overwrite_id')
    created, source = None, None
    if overwrite_id is not None:
        try:
            old, source, _, _ = _read_scene(overwrite_id)
            created = old['created']
        except ps.StoreError as e:
            return _respond(_store_error(e))
        except ps.ContractError:
            source = 'invalid'
    taken = _scene_name_taken(name, exclude_id=overwrite_id)
    if taken:
        return _respond(err('NAME_EXISTS', f'Esiste gia\' una scena "{name}"', {'existing_id': taken}))
    scene_id = overwrite_id or ps.new_scene_id()
    try:
        scene, warnings = _capture_scene(scene_id, name, created)
        if source == 'legacy':
            _backup_legacy(scene_id)
        ps.atomic_write_json(_scene_file(scene_id), scene)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e))
    scene_state.set(scene_id, name, False)
    return jsonify(ok('Scena salvata', warnings=warnings, scene=scene)), (200 if overwrite_id else 201)


@app.route('/api/scenes/<scene_id>', methods=['PUT'])
def api_scene_save(scene_id):
    try:
        old, source, _, _ = _read_scene(scene_id)
        scene, warnings = _capture_scene(scene_id, old['name'], old['created'])
        if source == 'legacy':
            _backup_legacy(scene_id)
        ps.atomic_write_json(_scene_file(scene_id), scene)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e))
    scene_state.set(scene_id, scene['name'], False)
    return jsonify(ok('Scena salvata', warnings=warnings, scene=scene))


@app.route('/api/scenes/<scene_id>', methods=['PATCH'])
def api_scene_patch(scene_id):
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    for k in data:
        if k not in ('name', 'favorite', 'order'):
            return _respond(err('BAD_REQUEST', f'Campo non modificabile: {k}', {'field': k}))
    fav, order = data.get('favorite'), data.get('order')
    if fav is not None and not isinstance(fav, bool):
        return _respond(err('BAD_REQUEST', 'favorite: bool atteso', {'field': 'favorite'}))
    if order is not None and (not isinstance(order, int) or isinstance(order, bool) or order < 0):
        return _respond(err('BAD_REQUEST', 'order: intero >= 0 atteso', {'field': 'order'}))
    try:
        _, source, _, raw = _read_scene(scene_id)
        if 'name' in data:
            name = _scene_name_arg(data)
            if name is None:
                return _respond(err('BAD_REQUEST', 'name: 1..64 caratteri', {'field': 'name'}))
            taken = _scene_name_taken(name, exclude_id=scene_id)
            if taken:
                return _respond(err('NAME_EXISTS', f'Esiste gia\' una scena "{name}"', {'existing_id': taken}))
            if source == 'legacy':
                _backup_legacy(scene_id)
            raw['name'] = name                   # solo il nome: ID e file restano (4.2)
            ps.atomic_write_json(_scene_file(scene_id), raw)
            _scene_renamed(scene_id, scene_id, name)
        if fav is not None or order is not None:
            lib = ps.library_set_meta(_load_library(), scene_id, favorite=fav, order=order)
            _save_library(lib)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e))
    return jsonify(ok(scene_meta=_scene_meta(scene_id)))


@app.route('/api/scenes/<scene_id>', methods=['DELETE'])
def api_scene_delete(scene_id):
    if not isinstance(scene_id, str) or not ps.ID_RE.match(scene_id) or not os.path.isfile(_scene_file(scene_id)):
        return _respond(err('NOT_FOUND', 'Scena non trovata', {'kind': 'scene'}))
    try:
        os.remove(_scene_file(scene_id))
    except OSError as e:
        return _respond(err('DISK_ERROR', f'Eliminazione fallita: {e}'))
    try:
        _save_library(ps.library_remove(_load_library(), scene_id))
    except ps.StoreError as e:
        print(f'[Scene] libreria non aggiornata: {e.message}')
    if scene_state.public()['scene_id'] == scene_id:
        scene_state.set(None, None, True)
    return jsonify(ok('Scena eliminata'))


def _sound_preflight(sound, allow_partial, sf_mode='require_match'):
    """Punti 5-6 del preflight (4.5) su un corpo sonoro. Ritorna
    (preflight, warnings, errore | None). Nessun effetto collaterale."""
    warnings = []
    loaded = sf_state.loaded_file()
    required = sound['soundfont']['file'] if sound['soundfont'] else None
    if required is None:
        warnings.append({'code': 'SOUNDFONT_UNSPECIFIED',
                         'message': 'Nessun SoundFont richiesto: applicato sul SoundFont caricato',
                         'details': {'loaded': loaded}})
    elif required != loaded:
        if sf_mode == 'keep_current':
            warnings.append({'code': 'SOUNDFONT_UNSPECIFIED',
                             'message': 'SoundFont richiesto non caricato: applicato sul SoundFont corrente',
                             'details': {'required': required, 'loaded': loaded}})
        elif os.path.isfile(os.path.join(SF2_DIR, required)):
            return None, warnings, err('SOUNDFONT_CHANGE_REQUIRED', f'Serve il SoundFont {required}',
                                       {'required': required, 'loaded': loaded})
        else:
            return None, warnings, err('SOUNDFONT_MISSING', f'SoundFont {required} non presente',
                                       {'required': required})
    parts, missing = [], []
    for p in sound['parts']:
        st = router.destination_status(p)
        if st == 'available':
            parts.append({'part_id': p['part_id'], 'action': 'apply', 'destination_status': st})
        else:
            parts.append({'part_id': p['part_id'], 'action': 'skip_unavailable', 'destination_status': st})
            missing.append({'part_id': p['part_id'], 'alias': p['destination']['device_alias'],
                            'direction': 'output', 'status': st})
    if missing and not allow_partial:
        return None, warnings, err('PORTS_UNAVAILABLE', 'Destinazioni MIDI non disponibili',
                                   {'parts': missing})
    if missing:
        warnings.append({'code': 'PART_UNAVAILABLE', 'message': 'Parti saltate: destinazione non disponibile',
                         'details': {'parts': missing}})
    pre = {'soundfont': {'required': required, 'loaded': loaded, 'match': required is None or required == loaded},
           'parts': parts}
    return pre, warnings, None


def _apply_sound(sound, preflight, scene=None):
    """Applicazione (4.5): globali, poi parti (rilascio note sulla vecchia
    destinazione dentro router.apply_part), poi persistenza in UNA scrittura.
    Nessun lock tenuto durante console/porte. Ritorna (risultati, ok_globali,
    errore_disco | None)."""
    g = sound['globals']
    globals_ok = True
    for cmd in (f"set synth.gain {g['gain']}", f"set synth.reverb.level {g['reverb_level']}",
                f"set synth.chorus.level {g['chorus_level']}"):
        if not send_fluid(cmd):
            globals_ok = False
    results = []
    for p, info in zip(sound['parts'], preflight['parts']):
        if info['action'] == 'skip_unavailable':
            # Configurazione desiderata conservata, nulla inviato, nessun fallback.
            router.apply_part(p, send='none')
            results.append({'part_id': p['part_id'], 'result': 'skipped_unavailable', 'delivery': 'skipped'})
            continue
        try:
            rep = router.apply_part(p, send='all')
        except midi_router.RouterError:
            rep = {'delivery': 'failed'}
        delivery = rep.get('delivery', 'failed')
        results.append({'part_id': p['part_id'], 'result': 'failed' if delivery == 'failed' else 'sent',
                        'delivery': delivery})
    disk = None
    try:
        _persist_sound(parts=sound['parts'], globals_=g, scene=scene)
    except OSError as e:
        disk = err('DISK_FULL' if getattr(e, 'errno', None) == 28 else 'DISK_ERROR',
                   f'Suono applicato ma stato non salvato: {e}')
    notify_parts_changed(list(range(16)), sound_change=False)
    return results, globals_ok, disk


def _partial_error(results, globals_ok, what):
    failed = [r['part_id'] for r in results if r['result'] == 'failed']
    if not failed and globals_ok:
        return None
    applied = [r['part_id'] for r in results if r['result'] == 'sent']
    return err('PARTIAL_APPLY', f'{what} applicato solo in parte: verificare FluidSynth/porte',
               {'committed': True, 'applied_parts': applied, 'failed_parts': failed,
                'globals': 'sent' if globals_ok else 'failed'})


def _scene_flow(scene_id, do_apply):
    data = _json_body() or {}
    try:
        discard = _bool_field(data, 'discard_changes')
        allow_partial = _bool_field(data, 'allow_partial')
    except ValueError as e:
        return _respond(err('BAD_REQUEST', f'{e}: bool atteso', {'field': str(e)}))
    try:
        scene, source, _, raw = _read_scene(scene_id)          # 1. scena valida
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e, None))
    busy = _transport_busy()                                     # 2. trasporto fermo
    if busy:
        return _respond(busy)
    if sf_state.is_loading():                                    # 3.
        return _respond(err('SOUNDFONT_LOADING', 'Caricamento SoundFont in corso'))
    if scene_state.public()['dirty'] and not discard:            # 4.
        return _respond(err('UNSAVED_CHANGES', 'La scena corrente ha modifiche non salvate',
                            {'scope': 'scene'}))
    pre, warnings, error = _sound_preflight(scene, allow_partial)  # 5-6.
    if error:
        return _respond(error)
    if not do_apply:
        return jsonify(ok(preflight=pre, warnings=warnings))
    clean = {'scene_id': scene_id, 'name': scene['name'], 'dirty': False}
    results, globals_ok, disk = _apply_sound(scene, pre, scene=clean)
    partial = _partial_error(results, globals_ok, 'Scena')
    if partial or disk:
        scene_state.set(scene_id, scene['name'], True)
        project_state.mark_dirty()
        return _respond(partial or disk)
    scene_state.set(scene_id, scene['name'], False)
    project_state.mark_dirty()           # lo snapshot sonoro del progetto e' cambiato
    return jsonify(ok('Scena richiamata', warnings=warnings,
                      recall={'scene_id': scene_id, 'parts': results}))


@app.route('/api/scenes/<scene_id>/preflight', methods=['POST'])
def api_scene_preflight(scene_id):
    return _scene_flow(scene_id, do_apply=False)


@app.route('/api/scenes/<scene_id>/recall', methods=['POST'])
def api_scene_recall(scene_id):
    return _scene_flow(scene_id, do_apply=True)


# ── Progetti (5) ────────────────────────────────────────────────────────

_project_meta_cache = {}          # percorso -> (file_stat, meta): niente ri-parse a ogni elenco


def _project_file(pid):
    return os.path.join(PROJECTS_DIR, pid + ps.PROJECT_SUFFIX)


def _project_exists(pid):
    return isinstance(pid, str) and bool(ps.ID_RE.match(pid)) and os.path.isfile(_project_file(pid))


def _project_meta(pid):
    path = _project_file(pid)
    st = ps.file_stat(path)
    cached = _project_meta_cache.get(path)
    if cached and cached[0] == st:
        return dict(cached[1])
    meta = {'id': pid, 'name': pid, 'modified': None, 'size_bytes': st[1] if st else 0, 'valid': True}
    try:
        doc = ps.read_json_file(path, ps.MAX_PROJECT_BYTES)
        ps.validate_project(doc)
        meta.update(name=doc['name'], modified=doc['modified'])
    except (ps.ContractError, ps.StoreError) as e:
        meta['valid'] = False
        meta['error'] = {'code': e.code, 'message': str(e),
                         'details': e.details() if isinstance(e, ps.ContractError) else e.details}
    _project_meta_cache[path] = (st, meta)
    return dict(meta)


def _project_list():
    if not os.path.isdir(PROJECTS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(PROJECTS_DIR)):
        if fn.endswith(ps.PROJECT_SUFFIX) and not fn.startswith('.'):
            out.append(_project_meta(fn[:-len(ps.PROJECT_SUFFIX)]))
    out.sort(key=lambda m: str(m['name']).casefold())
    return out


def _project_name_taken(name, exclude_id=None):
    key = str(name).casefold()
    for m in _project_list():
        if m['id'] != exclude_id and m['valid'] and str(m['name']).casefold() == key:
            return m['id']
    return None


def _project_payload(**extra):
    payload = {'project_state': project_state.public(), 'revision': daw.revision}
    payload.update(extra)
    return ok(**payload)


def _common_replace_checks(data):
    """Regole comuni Nuovo/Apri/Import sostitutivo (5.6): trasporto fermo,
    Salva/Scarta/Annulla se modificato."""
    busy = _transport_busy()
    if busy:
        return busy
    try:
        discard = _bool_field(data, 'discard_changes')
    except ValueError:
        return err('BAD_REQUEST', 'discard_changes: bool atteso', {'field': 'discard_changes'})
    if project_state.public()['dirty'] and not discard:
        return err('UNSAVED_CHANGES', 'Il progetto ha modifiche non salvate', {'scope': 'project'})
    return None


def _build_current_project_doc(pid, name, created, source_scene):
    """Snapshot (sotto il lock DAW, copie superficiali) -> documento validato.
    La validazione prima della scrittura garantisce che il file riapra
    (invariante salvataggio/caricamento, 5.3): se fallisce, nulla viene scritto."""
    session = daw.snapshot_session()
    sound = _current_sound()
    cur_scene = scene_state.public()
    if cur_scene['scene_id'] and cur_scene['name']:
        source_scene = {'id': cur_scene['scene_id'], 'name': cur_scene['name'][:64]}
    meta = {'id': pid, 'name': name, 'created': created, 'modified': ps.now_str(),
            'source_scene': source_scene,
            'devices': ps.devices_for_parts(sound['parts'], router.devices_list())}
    doc = ps.build_project_doc(session, sound, meta)
    ps.validate_project(doc)
    return doc


def _write_project_doc(pid, doc):
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    path = _project_file(pid)
    ps.atomic_write_json(path, doc)
    return ps.file_stat(path)


@app.route('/api/projects')
def api_projects_list():
    return jsonify(ok(projects=_project_list(), current=project_state.public()))


@app.route('/api/projects/current')
def api_projects_current():
    return jsonify(_project_payload())


@app.route('/api/projects/new', methods=['POST'])
def api_project_new():
    data = _json_body() or {}
    error = _common_replace_checks(data)
    if error:
        return _respond(error)
    try:
        daw.load_session(daw.empty_session())
    except DawError as e:
        return _respond(_store_error(e))
    project_state.set(None, ps.UNTITLED_PROJECT, False)
    return jsonify(_project_payload(revision=daw.last_revision))


@app.route('/api/projects/open', methods=['POST'])
def api_project_open():
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    pid = data.get('id')
    if not _project_exists(pid):
        return _respond(err('NOT_FOUND', 'Progetto non trovato', {'kind': 'project'}))
    error = _common_replace_checks(data)
    if error:
        return _respond(error)
    try:
        allow_partial = _bool_field(data, 'allow_partial')
    except ValueError:
        return _respond(err('BAD_REQUEST', 'allow_partial: bool atteso', {'field': 'allow_partial'}))
    sf_mode = data.get('soundfont', 'require_match')
    if sf_mode not in ('require_match', 'keep_current'):
        return _respond(err('BAD_REQUEST', "soundfont: 'require_match' o 'keep_current'", {'field': 'soundfont'}))
    # 2. Lettura e validazione COMPLETA fuori da ogni lock: la sessione corrente
    # resta intatta finche' tutto non e' valido (5.3).
    doc = None
    try:
        doc = ps.read_json_file(_project_file(pid), ps.MAX_PROJECT_BYTES)
        warnings = ps.validate_project(doc)
        content = ps.project_to_session(doc)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e, doc))
    # 3. Preflight del suono (nessun effetto collaterale).
    pre, sw, error = _sound_preflight(doc['sound'], allow_partial, sf_mode)
    if error:
        return _respond(error)
    warnings = list(warnings) + sw
    # 4. Scambio atomico sotto il lock DAW.
    try:
        rev = daw.load_session(content)
    except DawError as e:
        return _respond(_store_error(e))
    del content
    # 5. Applicazione del suono (fuori dal lock DAW).
    results, globals_ok, disk = _apply_sound(doc['sound'], pre, scene={'scene_id': None, 'name': None, 'dirty': False})
    partial = _partial_error(results, globals_ok, 'Suono del progetto') or disk
    # 6. Stato del progetto e della scena.
    project_state.set(pid, doc['name'], bool(partial), ps.file_stat(_project_file(pid)),
                      created=doc['created'], source_scene=doc.get('source_scene'))
    scene_state.set(None, None, False)
    if partial:
        return _respond(partial)
    return jsonify(_project_payload(revision=rev, sound={'parts': results}, warnings=warnings))


@app.route('/api/projects/save', methods=['POST'])
def api_project_save():
    data = _json_body() or {}
    overwrite = data.get('overwrite', False)
    if not isinstance(overwrite, bool):
        return _respond(err('BAD_REQUEST', 'overwrite: bool atteso', {'field': 'overwrite'}))
    snap = project_state.snapshot()
    pid = snap['id']
    if pid is None:
        return _respond(err('NO_CURRENT_FILE', 'Progetto mai salvato: usa Salva come'))
    current = ps.file_stat(_project_file(pid))
    if current != snap['file_stat'] and not overwrite:
        return _respond(err('FILE_CHANGED', 'Il file del progetto e\' cambiato su disco',
                            {'expected': snap['file_stat'], 'found': current}))
    try:
        doc = _build_current_project_doc(pid, snap['name'], snap['created'], snap['source_scene'])
        st = _write_project_doc(pid, doc)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e))
    project_state.set(pid, snap['name'], False, st, created=doc['created'],
                      source_scene=doc.get('source_scene'), if_gen=snap['gen'])
    return jsonify(_project_payload(message='Progetto salvato'))


@app.route('/api/projects/save_as', methods=['POST'])
def api_project_save_as():
    data = _json_body()
    if data is None:
        return _respond(err('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''}))
    name = _scene_name_arg(data)
    if name is None:
        return _respond(err('BAD_REQUEST', 'name: 1..64 caratteri', {'field': 'name'}))
    overwrite_id = data.get('overwrite_id')
    created = None
    if overwrite_id is not None:
        if not _project_exists(overwrite_id):
            return _respond(err('NOT_FOUND', 'Progetto da sovrascrivere non trovato', {'kind': 'project'}))
        try:
            created = ps.read_json_file(_project_file(overwrite_id), ps.MAX_PROJECT_BYTES).get('created')
        except (ps.ContractError, ps.StoreError, AttributeError):
            created = None
    taken = _project_name_taken(name, exclude_id=overwrite_id)
    if taken:
        return _respond(err('NAME_EXISTS', f'Esiste gia\' un progetto "{name}"', {'existing_id': taken}))
    snap = project_state.snapshot()
    pid = overwrite_id or ps.new_project_id()
    try:
        doc = _build_current_project_doc(pid, name, created if isinstance(created, str) else None,
                                         snap['source_scene'])
        st = _write_project_doc(pid, doc)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e))
    project_state.set(pid, name, False, st, created=doc['created'],
                      source_scene=doc.get('source_scene'), if_gen=snap['gen'])
    return jsonify(_project_payload(message='Progetto salvato')), 201


@app.route('/api/projects/<pid>', methods=['PATCH'])
def api_project_patch(pid):
    if not _project_exists(pid):
        return _respond(err('NOT_FOUND', 'Progetto non trovato', {'kind': 'project'}))
    data = _json_body() or {}
    name = _scene_name_arg(data)
    if name is None:
        return _respond(err('BAD_REQUEST', 'name: 1..64 caratteri', {'field': 'name'}))
    taken = _project_name_taken(name, exclude_id=pid)
    if taken:
        return _respond(err('NAME_EXISTS', f'Esiste gia\' un progetto "{name}"', {'existing_id': taken}))
    doc = None
    try:
        doc = ps.read_json_file(_project_file(pid), ps.MAX_PROJECT_BYTES)
        ps.validate_project(doc)
        doc['name'] = name
        st = _write_project_doc(pid, doc)
    except (ps.ContractError, ps.StoreError) as e:
        return _respond(_store_error(e, doc))
    snap = project_state.snapshot()
    if snap['id'] == pid:
        # Rinomina del file aperto: aggiorna nome e stat (Salva successivo non da' FILE_CHANGED).
        project_state.update(name=name, file_stat=st)
    return jsonify(ok(project_meta=_project_meta(pid)))


@app.route('/api/projects/<pid>', methods=['DELETE'])
def api_project_delete(pid):
    if not _project_exists(pid):
        return _respond(err('NOT_FOUND', 'Progetto non trovato', {'kind': 'project'}))
    try:
        os.remove(_project_file(pid))
    except OSError as e:
        return _respond(err('DISK_ERROR', f'Eliminazione fallita: {e}'))
    snap = project_state.snapshot()
    if snap['id'] == pid:
        # Il contenuto in memoria resta, ma non ha piu' un file.
        project_state.set(None, snap['name'], True, None, created=snap['created'],
                          source_scene=snap['source_scene'])
    return jsonify(_project_payload(message='Progetto eliminato'))


@app.route('/api/projects/<pid>/download')
def api_project_download(pid):
    if not _project_exists(pid):
        return _respond(err('NOT_FOUND', 'Progetto non trovato', {'kind': 'project'}))
    meta = _project_meta(pid)
    fname = ps.safe_file_name(meta['name'], pid) + ps.PROJECT_SUFFIX
    return send_file(_project_file(pid), mimetype='application/json', as_attachment=True,
                     download_name=fname)


@app.route('/api/projects/upload', methods=['POST'])
def api_project_upload():
    f = request.files.get('file')
    if f is None or not f.filename:
        return _respond(err('BAD_REQUEST', 'Nessun file inviato', {'field': 'file'}))
    raw = f.read(ps.MAX_PROJECT_BYTES + 1)
    if len(raw) > ps.MAX_PROJECT_BYTES:
        return _respond(err('PAYLOAD_TOO_LARGE', 'File progetto troppo grande',
                            {'limit_bytes': ps.MAX_PROJECT_BYTES}))
    doc = None
    try:
        doc = ps.parse_json_bytes(raw, ps.MAX_PROJECT_BYTES)
        ps.validate_project(doc)
    except ps.ContractError as e:
        return _respond(_store_error(e, doc))
    name = request.form.get('name')
    name = name.strip() if isinstance(name, str) and name.strip() else doc['name']
    if len(name) > 64:
        return _respond(err('BAD_REQUEST', 'name: 1..64 caratteri', {'field': 'name'}))
    taken = _project_name_taken(name)
    if taken:
        return _respond(err('NAME_EXISTS', f'Esiste gia\' un progetto "{name}"', {'existing_id': taken}))
    pid = ps.new_project_id()                 # sempre un nuovo id: mai sovrascrivere
    doc['id'], doc['name'] = pid, name
    try:
        _write_project_doc(pid, doc)
    except ps.StoreError as e:
        return _respond(_store_error(e))
    return jsonify(ok('Progetto caricato', project_meta=_project_meta(pid))), 201


# ── Import/export MIDI con routing (7.2, 7.3; M1) ──────────────────────

def _apply_imported_programs(channel_programs):
    """Suono iniziale importato -> parti (7.2): interna = bank FluidSynth
    (convenzione GS, D5), esterna = MSB/LSB/PC. Nulla va a FluidSynth per le
    parti esterne (A22). Router + parts + channels coerenti in UNA scrittura
    (M1: il flush degli slider legge il router, quindi non annulla l'import)."""
    changed = []
    for ch, prog in sorted(channel_programs.items()):
        t = int(ch)
        if not 0 <= t <= 15:
            continue
        part = router.get_part(t)
        if part['destination']['kind'] == 'internal':
            part['sound'] = {'bank': ps.smf_to_fluid_bank(prog.get('bank'), part['destination']['channel']),
                             'program': prog.get('program', 0), 'name': None}
        else:
            part['sound'] = ps.smf_to_external_sound(part['sound'], prog.get('bank'), prog.get('program'))
        try:
            router.apply_part(part, send='all', cc_keys=[])
        except midi_router.RouterError as e:
            print(f'[MIDI] Parte {t} non applicata: {e.message}')
            continue
        changed.append(part)
    if changed:
        _persist_sound(parts=changed)
        notify_parts_changed([p['part_id'] for p in changed])
    return changed


def _parse_flag(value):
    if value is None:
        return None
    return str(value).lower() in ('1', 'true', 'yes', 'on')


def _midi_import(midi_bytes, merge, convert, discard, source_name):
    """Logica comune load/upload. `discard` None = client legacy (nessun
    controllo dirty, D13). Ritorna (body, http)."""
    if discard is not None and not merge and project_state.public()['dirty'] and not discard:
        return err('UNSAVED_CHANGES', 'Il progetto ha modifiche non salvate', {'scope': 'project'})
    try:
        info = daw.import_midi(midi_bytes, merge=merge, convert=bool(convert))
    except RuntimeError as e:
        return err('TRANSPORT_ACTIVE', str(e), {'is_playing': daw.is_playing,
                                                'is_recording': daw.is_recording}, http=409)
    except DawError as e:
        return _store_error(e)
    _apply_imported_programs(info.get('channel_programs', {}))
    if not merge:
        # Import sostitutivo: progetto staccato dal file (D12).
        snap = project_state.snapshot()
        project_state.set(None, str(source_name)[:64] or ps.UNTITLED_PROJECT, True, None,
                          source_scene=snap['source_scene'])
    else:
        project_state.mark_dirty()
    warnings = info.pop('warnings', [])
    return ok(warnings=warnings, data=info, revision=info.get('revision', daw.revision)), 200


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
        _sound_changed()
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
            return ack_ok(revision=daw.last_revision) if daw.stop_recording() \
                else ack_err('BAD_REQUEST', 'Nessuna registrazione in corso')
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
                return ack_ok(revision=daw.last_revision)
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

        # Suono iniziale per traccia dalla sua parte (7.3): interna -> bank
        # FluidSynth convertito con la convenzione GS (D5); esterna -> MSB/LSB/PC.
        channel_programs = {}
        not_representable = []
        for ch, part in enumerate(router.get_parts()):
            snd = part['sound']
            if part['destination']['kind'] == 'internal':
                bank14, warn = ps.fluid_bank_to_smf(snd['bank'], ch)   # canale SMF = ID traccia
                if warn:
                    not_representable.append({'track': ch, 'bank': snd['bank']})
                channel_programs[ch] = {'bank': bank14, 'program': snd['program']}
            else:
                bank14, prog = ps.external_sound_to_smf(snd)
                channel_programs[ch] = {'bank': bank14, 'program': prog}

        midi_bytes = daw.export_midi(channel_programs=channel_programs)
        if not midi_bytes:
            return jsonify({"status": "error", "message": "Nessuna traccia da esportare"}), 400

        with open(filepath, 'wb') as f:
            f.write(midi_bytes)

        warnings = []
        has_data = {ch for ch in range(16) if daw.tracks.get(ch)}
        not_representable = [w for w in not_representable if w['track'] in has_data]
        if not_representable:
            warnings.append({'code': 'BANK_NOT_REPRESENTABLE',
                             'message': 'Bank FluidSynth non esprimibile in SMF: scritto solo il Program Change',
                             'details': {'tracks': not_representable}})
        return jsonify(ok(f"Salvato come {safe_name}.mid", warnings=warnings,
                          filename=f"{safe_name}.mid",
                          notice='Alias delle porte e confini dei clip non sono salvati in un file MIDI'))
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
        body, http = _midi_import(midi_bytes, merge, _parse_flag(request.args.get('convert')),
                                  _parse_flag(request.args.get('discard_changes')),
                                  os.path.splitext(safe)[0])
        return jsonify(body), http
    except Exception as e:
        body, http = err('INTERNAL', str(e))
        return jsonify(body), http


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
        original_name = os.path.splitext(os.path.basename(f.filename))[0]
        body, http = _midi_import(midi_bytes, merge, _parse_flag(request.form.get('convert')),
                                  _parse_flag(request.form.get('discard_changes')), original_name)
        if http != 200:
            return jsonify(body), http

        # Salva anche una copia nella cartella MIDI
        safe_name = "".join(c for c in original_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_') or f"upload_{int(time.time())}"
        filepath = os.path.join(MIDI_DIR, f"{safe_name}.mid")
        counter = 2
        while os.path.exists(filepath):
            filepath = os.path.join(MIDI_DIR, f"{safe_name}_{counter}.mid")
            counter += 1
        with open(filepath, 'wb') as out:
            out.write(midi_bytes)
        # Suono iniziale gia' applicato da _midi_import (router + parts + channels).
        return jsonify(body), http
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 409
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    print("Starting server with WebSocket support...")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
