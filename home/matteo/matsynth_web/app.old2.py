from flask import Flask, render_template, request, jsonify
import socket
import os

app = Flask(__name__)

# Configurazione
SF2_DIR = "/usr/share/sounds/sf2/"
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800

def send_fluid(command):
    try:
        with socket.create_connection((FLUID_HOST, FLUID_PORT), timeout=10) as sock:
            sock.sendall(f"{command}\n".encode())
            # Pausa cruciale per il Pi Zero: diamo tempo a FluidSynth di generare la lista
            import time
            time.sleep(0.5)

            response = ""
            while True:
                # Impostiamo un timeout brevissimo per il singolo pacchetto
                sock.settimeout(0.2)
                try:
                    data = sock.recv(16384).decode()
                    if not data: break
                    response += data
                except socket.timeout:
                    # Se per 0.2 secondi non arriva nulla, consideriamo la lista finita
                    break
            return response
    except Exception as e:
        print(f"Errore Socket: {e}")
        return ""

@app.route('/')
def index():
    files = [f for f in os.listdir(SF2_DIR) if f.endswith(('.sf2', '.sf3'))]
    return render_template('index.html', files=files)

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

# Cambia <float:val> in <val> per accettare sia 0 che 0.5
@app.route('/set_effect/<type>/<val>')
def set_effect(type, val):
    try:
        # Trasformiamo in float qui dentro, così Python gestisce tutto
        f_val = float(val)

        # Se è 0 o 1, lo trasformiamo in 0.0 o 1.0 per FluidSynth
        comando = f"set synth.{type} {f_val}"

        send_fluid(comando)
        print(f"ESEGUITO: {comando}")
        return "OK"
    except ValueError:
        return "Valore non valido", 400

@app.route('/select_prog/<int:chan>/<int:bank>/<int:prog>')
def select_prog(chan, bank, prog):
    """
    Riceve il cambio strumento dal mixer.
    Esempio chiamata: /select_prog/0/120/0 (Canale 1, Banco 120, Programma 0)
    """
    try:
        # Il comando FluidSynth è: select canale sfont_id banco programma
        # Usiamo 1 come sfont_id (che abbiamo verificato funzionare con 'inst 1')
        comando = f"select {chan} 1 {bank} {prog}"
        send_fluid(comando)
        print(f"MIXER: Canale {chan} impostato su Banco {bank}, Prog {prog}")
        return "OK"
    except Exception as e:
        print(f"Errore cambio strumento: {e}")
        return str(e), 500
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
