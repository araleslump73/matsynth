# MatSynth Web

Web interface per controllare FluidSynth via telnet da browser.

## Caratteristiche

- Caricamento dinamico di soundfont SF2/SF3
- Selezione strumenti per canale con banco e program change
- Controlli effetti (gain, reverb, chorus)
- Invio Control Change MIDI
- Stato persistente (ultimo soundfont, volumi)

## Requisiti

- Python 3.7+
- Flask
- FluidSynth con server telnet attivo sulla porta 9800

## Installazione

```bash
pip install -r requirements.txt
```

## Configurazione

Modifica le costanti in `app.py` se necessario:

```python
SF2_DIR = "/usr/share/sounds/sf2/"  # Cartella con i soundfont
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'
```

## Avvio FluidSynth

FluidSynth deve essere avviato con il server telnet attivo:

```bash
fluidsynth -s -i -a alsa -m alsa_seq -g 0.7 -o shell.port=9800
```

## Esecuzione

```bash
python home/matteo/matsynth_web/app.py
```

L'applicazione sarà disponibile su `http://localhost:5000`

## Deployment su Raspberry Pi

Sono disponibili tre script per trasferire i file sul Raspberry Pi usando SCP:

### Windows PowerShell
```powershell
.\deploy.ps1 matteo@matsynth
```

### Windows Command Prompt (cmd)
```cmd
deploy.bat matteo@matsynth
```

### Linux / Mac / WSL
```bash
chmod +x deploy.sh
./deploy.sh matteo@matsynth
```

Gli script:
- Copiano ricorsivamente tutti i file da `home/matteo/matsynth_web` al Raspberry Pi
- Preservano i permessi e i timestamp dei file
- Utilizzano la compressione per velocizzare il trasferimento
- Offrono l'opzione di riavviare automaticamente il servizio `matsynth.service`

**Nota**: Assicurati di avere:
- SSH configurato e funzionante sul Raspberry Pi
- Chiavi SSH o password per l'accesso
- OpenSSH Client installato (su Windows: Settings > Apps > Optional Features)

## Note

- Per cambiare banco e preset in un solo comando telnet: `select <canale> <sfid> <banco> <preset>`
- Per cambiare banco senza `sfid`: invia in sequenza `cc <canale> 0 <banco_msb>`, `cc <canale> 32 <banco_lsb>`, `prog <canale> <preset>`
- Il file `last_state.json` mantiene lo stato tra i riavvii

## Licenza

MIT
