# MatSynth

**MatSynth** è un controller web per **FluidSynth** pensato per Raspberry Pi (anche headless) o qualsiasi Linux: permette di gestire **16 canali MIDI**, soundfont, controlli CC e **preset** direttamente da browser.

- Backend: **Python + Flask**
- Frontend: **HTML + Bootstrap + CSS custom**
- Engine audio: **FluidSynth** (controllo via TCP socket “telnet-like”)
- Deploy: script `deploy.sh` / `deploy.ps1` / `deploy.bat`

## Caratteristiche

### Mixer multitimbrico
- 16 canali MIDI indipendenti
- Selezione strumento per canale tramite **Bank (MSB/LSB) + Program**
- Controlli CC per il canale attivo:
  - Volume **CC7**
  - Attack **CC73**
  - Decay **CC75**
  - Release **CC72**
  - Cutoff **CC74**
  - Resonance **CC71**
- Pulsante **Refresh** per sincronizzare UI ↔ stato FluidSynth (`channels`)

### Master / Global
- Master Gain (0.0–3.0) → `set synth.gain`
- Reverb level → `set synth.reverb.level`
- Chorus level → `set synth.chorus.level`
- Persistenza automatica su `last_state.json`

### Soundfont
- Elenco e caricamento soundfont **.sf2** e **.sf3** da directory configurata
- All’atto del load: scarica tutti i soundfont precedenti e carica il nuovo
- Tracciamento automatico dell’ID soundfont attivo (`fonts` → `sf_id`)

### Preset
- Pagina dedicata `/presets`
- Salvataggio configurazione completa (16 canali + global) in JSON
- Lista, ricerca, rename e delete preset
- Apply preset (non carica automaticamente il soundfont per evitare timeout su hardware lento: il font va caricato prima)

### Settings (hardware + rete)
- Visualizza hostname e IP (`/api/network`)
- Scansione e selezione output audio ALSA (`aplay -l`)
- Scansione e selezione device MIDI (`aconnect -i`)
- “Save & Restart” → salva su stato e riavvia servizio systemd

### DAW MIDI
- Registrazione e riproduzione multitraccia, metronomo e loop
- Editor di note e clip, quantizzazione, trasposizione e undo/redo
- Importazione ed esportazione di file MIDI

## Uso di Suona, Studio ed Editor

- **Suona** gestisce le 16 parti, i suoni, il mixer e le scene preferite.
- **Studio** mostra la timeline. Usa `+`/`−` per lo zoom orizzontale; seleziona una traccia e, nell'inspector **Traccia**, cambia l'altezza o nascondila. Le tracce nascoste si ripristinano dai pulsanti sopra la timeline.
- **Editor note** apre il piano roll della traccia o del clip selezionato. I controlli `↔` e `↕` regolano lo zoom orizzontale e verticale; **Adatta** inquadra il clip o le note.
- La **scena** salva suoni, parti e controlli del synth; il **progetto MatSynth** salva la sessione DAW (note, clip, tracce, tempo e routing); un file **MIDI** contiene eventi musicali portabili ma non conserva tutti i dati MatSynth, per esempio i confini dei clip e gli alias delle porte. Importa/esporta MIDI e salva/apri progetto sono operazioni diverse.

I preset legacy vengono letti tramite migrazione in memoria: la lettura non riscrive i file originali. Non eliminare i vecchi preset durante la migrazione.

### Routing MIDI e FANTOM

La modalità MIDI predefinita è `legacy`: la tastiera può essere collegata direttamente a FluidSynth via ALSA; il controllo software **Monitor** delle parti non inoltra note live in questa modalità. `managed` è sperimentale e non va attivata come modalità predefinita prima delle prove hardware. Prima di abilitare il monitor software sulla FANTOM, verifica il suo **Local Control** per evitare note doppie. Non usare contemporaneamente Local Control e monitor senza aver verificato il routing.

### Rete: Studio e Palco
Gli script in `home/matteo/` usano NetworkManager e non contengono credenziali.

**Automatico (consigliato):** `matsynth-network.service` esegue
`auto_network.sh` una sola volta all'avvio: se entro 45 s NetworkManager si
collega a una rete Wi-Fi salvata resta in Studio, altrimenti attiva l'hotspot
(Palco). Non controlla più la rete dopo l'avvio: per cambiare modalità si
riavvia il Pi (o si usano gli script manuali). Richiede che il profilo hotspot
esista: crealo una volta con `bash home/matteo/mode_palco.sh`.

```bash
sudo cp home/matteo/matsynth-network.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matsynth-network.service
```

Variabili: `MATSYNTH_WIFI_WAIT` (secondi di attesa), `MATSYNTH_HOTSPOT_PROFILE`.

Manuale:

- `bash home/matteo/mode_palco.sh` attiva il profilo hotspot esistente oppure
  chiede la password per crearne uno. In alternativa la password può essere
  fornita nella variabile d'ambiente `MATSYNTH_HOTSPOT_PASSWORD`.
- `bash home/matteo/mode_studio.sh "nome rete o profilo"` attiva un profilo
  salvato oppure chiede le credenziali per una nuova rete.

Il cambio di rete può interrompere la sessione SSH; per i prompt interattivi
via SSH usare un terminale con `ssh -t`. Queste versioni senza credenziali non
vengono installate automaticamente sul Pi.

## Requisiti

### Software
- Python 3
- Flask, Flask-SocketIO, mido e python-rtmidi (vedi `requirements.txt`)
- FluidSynth
- ALSA utils + jq

Debian/Raspberry Pi OS:
```bash
sudo apt-get update
sudo apt-get install -y fluidsynth alsa-utils jq
```

### Hardware (consigliato)
- Raspberry Pi 3/4 (supportabile Pi Zero 2 W usando soundfont leggeri/SF3)
- Controller MIDI USB
- Scheda audio USB (opzionale)

## Installazione

> Il repository contiene una struttura `home/matteo/` che rispecchia i percorsi di installazione previsti.

### 1) Clona il repository
```bash
cd /home/matteo
git clone https://github.com/araleslump73/matsynth.git
cd matsynth
```

### 2) Dipendenze Python
```bash
python3 -m pip install -r requirements.txt
```

### 3) Soundfont
Crea la directory (se non esiste) e copia i soundfont:
```bash
sudo mkdir -p /usr/share/sounds/sf2
sudo cp *.sf2 /usr/share/sounds/sf2/
sudo cp *.sf3 /usr/share/sounds/sf2/
```

### 4) Permessi script avvio
```bash
chmod +x /home/matteo/matsynth/home/matteo/startfluid.sh
```

## Avvio

### Avvio manuale
```bash
/home/matteo/matsynth/home/matteo/startfluid.sh
```

UI web:
- `http://localhost:5000`
- `http://<ip>:5000`
- `http://<hostname>.local:5000`

## systemd (avvio automatico)

Crea:
```bash
sudo nano /etc/systemd/system/matsynth.service
```

Esempio:
```ini
[Unit]
Description=MatSynth Multitimbric Synthesizer
After=network.target sound.target

[Service]
Type=simple
User=matteo
WorkingDirectory=/home/matteo/matsynth
ExecStart=/home/matteo/matsynth/home/matteo/startfluid.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Abilita e avvia:
```bash
sudo systemctl daemon-reload
sudo systemctl enable matsynth.service
sudo systemctl start matsynth.service
```

Log:
```bash
sudo journalctl -u matsynth.service -f
```

## Architettura (come funziona)

### Componenti
- `home/matteo/startfluid.sh`
  - legge `last_state.json` con `jq`
  - avvia FluidSynth con ALSA device, gain, reverb/chorus
  - avvia Flask: `python3 /home/matteo/matsynth_web/app.py`
- `home/matteo/matsynth_web/app.py`
  - espone API HTTP (Flask)
  - invia comandi a FluidSynth su `127.0.0.1:9800` via socket TCP
  - salva/ripristina stato su `last_state.json`
  - gestisce preset in `presets/`

### Flusso
Browser → HTTP (5000) → Flask → TCP (9800) → FluidSynth → ALSA out

## API principali (reference rapido)

### Soundfont
- `GET /list_sf2` → lista file in `SF2_DIR`
- `GET /load_sf2/<filename>` → unload di tutti gli ID, load del file, aggiorna `sf_id`, salva stato
- `GET /get_instruments` → `inst <sf_id>`

### Canali/CC
- `GET /select_prog/<ch>/<bank>/<prog>` → `select ch sf_id bank prog`
- `GET /cc/<ch>/<cc>/<val>` → `cc ch cc val`
- `GET /refresh_status` → `channels`

### Global effects
- `GET /set_effect/<type>/<val>` → `set synth.<type> <val>` (es: `gain`, `reverb.level`, `chorus.level`)

### Preset
- `GET /api/presets/list`
- `POST /api/presets/save`
- `GET /api/presets/load/<filename>`
- `DELETE /api/presets/delete/<filename>`
- `POST /api/presets/rename`
- `POST /api/presets/apply`

## Note e limitazioni note

- L’apply del preset **non carica automaticamente** il soundfont (scelta voluta per evitare timeout su hardware limitato): carica prima il font dalla home.

## Sicurezza

L’app non implementa autenticazione: **non esporre la porta 5000 su Internet**.
Consigliato limitarla alla LAN (firewall) o usare un reverse proxy con auth.

## Deploy

Script inclusi:
- Linux/macOS: `deploy.sh`
- Windows PowerShell: `deploy.ps1`
- Windows CMD: `deploy.bat`

Esempio:
```bash
./deploy.sh matteo@matsynth
```

Prima del deploy salva progetto/scena e ferma il trasporto: il riavvio del servizio interrompe Play/Record. Il deploy non sostituisce la cartella di stato e i preset, ma conviene conservare un backup privato sul Pi:

```bash
cd /home/matteo
tar -czf "$HOME/backup_pre_deploy_$(date +%Y%m%d_%H%M%S).tgz" matsynth_web
```

Per un rollback, imposta `BACKUP` sul nome dell'archivio scelto e controllane il contenuto. Solo dopo la verifica ripristinalo e riavvia il servizio. Il ripristino sovrascrive i file contenuti nel backup; salva prima ogni dato creato dopo il backup.

```bash
BACKUP="$HOME/backup_pre_deploy_20261123_114659.tgz" # sostituisci con l'archivio scelto
tar -tzf "$BACKUP"
# Dopo aver verificato l'elenco:
tar -xzf "$BACKUP" -C /home/matteo
sudo systemctl restart matsynth.service
```

Per tornare alla modalità MIDI sicura `legacy`, da **Impostazioni → MIDI → Modalità** scegli `legacy` e riavvia il servizio. La modalità effettiva si verifica in `/api/system/status`.

## Licenza

MIT
