from flask import Flask, render_template, request, jsonify
import socket
import os
import json
import time
import subprocess
import threading

app = Flask(__name__)

# Configurazione
SF2_DIR = "/usr/share/sounds/sf2/"
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'
sf_id = 1 # Variabile globale per tenere traccia dell'ID del soundfont attivo

def save_state(key, value):
    try:
        state = get_last_state()
        state[key] = value
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Errore salvataggio stato: {e}")

def get_last_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
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
                
                # Formato compatibile con FluidSynth (es. hw:1)
                dev_id = f"hw:{card_num}" 
                devices.append({"id": dev_id, "name": f"Scheda {card_num}: {name}"})
    except Exception as e:
        print(f"Errore lettura audio: {e}")

    state = get_last_state()
    # Se non c'è una scheda salvata, mettiamo un default vuoto
    current = state.get('audio_device', '') 
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
        save_state('audio_device', data['audio'])
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


if __name__ == '__main__':
    time.sleep(2)
    restore_settings()
    # Inizializza l'ID del soundfont all'avvio
    sf_id = get_active_sf_id()
    print(f"Soundfont ID caricato: {sf_id}")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=False)
