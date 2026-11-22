from flask import Flask, render_template, request, jsonify
import socket
import os
import json
import time

app = Flask(__name__)

# Configurazione - Percorsi allineati al tuo sistema
SF2_DIR = "/usr/share/sounds/sf2/"
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'

def save_state(key, value):
    """Salva una singola impostazione nel file JSON"""
    try:
        state = get_last_state()
        state[key] = value
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Errore salvataggio stato: {e}")

def get_last_state():
    """Legge l'ultimo stato salvato o restituisce i default"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"gain": 1.0, "reverb.level": 0.4, "chorus.level": 0.4, "font": "GeneralUser-GS.sf2"}

def send_fluid(command):
    """Invia comandi a FluidSynth via Socket Telnet"""
    try:
        with socket.create_connection((FLUID_HOST, FLUID_PORT), timeout=10) as sock:
            sock.sendall(f"{command}\n".encode())
            time.sleep(0.3) # Tempo di elaborazione per il Pi Zero
            response = ""
            while True:
                sock.settimeout(0.3)
                try:
                    data = sock.recv(16384).decode()
                    if not data: break
                    response += data
                except socket.timeout:
                    break
            return response
    except Exception as e:
        print(f"Errore Socket: {e}")
        return ""

def restore_settings():
    """Prova a ripristinare i parametri finché FluidSynth non è pronto"""
    state = get_last_state()
    max_retries = 10

    # Prende il valore dal JSON. Se non lo trova, usa quello dello script di avvio
    gain_val = state.get('gain', 0.7)  # 0.7 è il tuo default nello script
    rev_val = state.get('reverb.level', 0.4)
    cho_val = state.get('chorus.level', 0.4)

    print("Tentativo di ripristino configurazione...")

    print(f"Ripristino attivo: Gain {gain_val}, Riverbero {rev_val}")
    
    send_fluid(f"set synth.gain {gain_val}")
    send_fluid(f"set synth.reverb.level {rev_val}")
    send_fluid(f"set synth.chorus.level {cho_val}")



@app.route('/')
def index():
    files = [f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3'))] if os.path.exists(SF2_DIR) else []
    return render_template('index.html', files=files)

@app.route('/list_sf2')
def list_sf2():
    if not os.path.exists(SF2_DIR): return jsonify([])
    files = [f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3'))]
    return jsonify(files)

#@app.route('/load_sf2/<filename>')
#def load_sf2(filename):
#    path = os.path.join(SF2_DIR, filename)
#    send_fluid("unload 1")
#    risposta = send_fluid(f"load {path}")
#    save_state('font', filename)
#    return f"Caricato: {risposta}"


@app.route('/load_sf2/<filename>')
def load_sf2(filename):
    path = os.path.join(SF2_DIR, filename)

    # 1. Chiediamo la lista attuale
    raw_data = send_fluid("fonts")
    
    # 2. Analizziamo riga per riga per trovare i numeri ID
    # L'output è tipo:
    # ID  Name
    #  1  /usr/share/sounds/sf2/GeneralUser.sf2
    ids_to_unload = []
    
    lines = raw_data.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue 
        
        # Saltiamo l'intestazione
        if line.lower().startswith("id"): continue
        
        # Prendiamo il primo "pezzo" della riga (l'ID)
        parts = line.split()
        if parts and parts[0].isdigit():
            ids_to_unload.append(parts[0])

    # 3. Scarichiamo TUTTI i font trovati
    for sf_id in ids_to_unload:
        print(f"Scaricamento ID: {sf_id}")
        send_fluid(f"unload {sf_id}")

    # 4. Ora carichiamo il nuovo (che prenderà sicuramente ID 1)
    risposta = send_fluid(f"load {path}")
    
    save_state('font', filename)
    return f"Font caricato. Scaricati ID: {ids_to_unload}. Risposta load: {risposta}"





@app.route('/get_instruments')
def get_instruments():
    # Usiamo 'inst' senza numero per vedere tutto il contenuto caricato
    data = send_fluid("inst 1")
    lines = data.split('\n')
    instruments = []
    for line in lines:
        if "-" in line and any(char.isdigit() for char in line):
            instruments.append(line.strip())
    return jsonify(instruments)

@app.route('/set_effect/<type>/<val>')
def set_effect(type, val):
    try:
        f_val = float(val)
        comando = f"set synth.{type} {f_val}"
        send_fluid(comando)
        save_state(type, f_val)
        return "OK"
    except ValueError:
        return "Valore non valido", 400

@app.route('/select_prog/<int:chan>/<int:bank>/<int:prog>')
def select_prog(chan, bank, prog):
    # sfont_id fisso a 1 come concordato
    comando = f"select {chan} 1 {bank} {prog}"
    send_fluid(comando)
    return "OK"

@app.route('/refresh_status')
def refresh_status():
    # 'channels' su alcune versioni è limitato.
    # Proviamo a recuperare lo stato dettagliato.
    data = send_fluid("channels")
    # Se i dati non contengono 'bank', significa che la shell è in modalità breve
    return jsonify({"raw": data})


@app.route('/cc/<int:chan>/<int:cc>/<int:val>')
def control(chan, cc, val):
    send_fluid(f"cc {chan} {cc} {val}")
    return "OK"

if __name__ == '__main__':
    # Attendiamo che FluidSynth sia pronto prima di ripristinare
    time.sleep(2)
    restore_settings()
    # threaded=False per risparmiare risorse sul Pi Zero
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=False)
