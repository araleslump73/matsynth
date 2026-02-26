# 🎹 MatSynth - Multitimbric Synthesizer Controller

**MatSynth** è un'interfaccia web moderna e completa per controllare **FluidSynth** tramite browser, progettata per trasformare un Raspberry Pi (o qualsiasi sistema Linux) in un sintetizzatore hardware multitimbrico professionale.

## 📋 Indice

- [Caratteristiche Principali](#-caratteristiche-principali)
- [Requisiti di Sistema](#-requisiti-di-sistema)
- [Installazione](#-installazione)
- [Configurazione](#-configurazione)
- [Utilizzo](#-utilizzo)
- [Architettura del Sistema](#-architettura-del-sistema)
- [Riferimento API](#-riferimento-api)
- [Risoluzione Problemi](#-risoluzione-problemi)
- [Best Practices](#-best-practices)
- [Licenza](#-licenza)

## ✨ Caratteristiche Principali

### 🎛️ Controllo MIDI Multitimbrico
- **16 canali MIDI indipendenti** con selezione strumenti individuale
- Supporto completo per **Bank Select (MSB/LSB)** e **Program Change**
- **Control Change MIDI** in tempo reale per ogni canale:
  - Attack (CC73) - Controllo dell'attacco del suono
  - Release (CC72) - Controllo del rilascio del suono
  - Cutoff (CC74) - Controllo della frequenza di taglio del filtro
  - Resonance (CC71) - Controllo della risonanza del filtro

### 🎵 Gestione Soundfont
- Caricamento dinamico di soundfont **SF2** e **SF3**
- Scaricamento automatico del soundfont precedente prima di caricare uno nuovo
- Tracciamento automatico dell'ID del soundfont attivo
- Persistenza della selezione del soundfont tra i riavvii

### 🎚️ Effetti Master
- **Master Volume (Gain)**: 0.0 - 3.0 con persistenza dello stato
- **Reverb Level**: Controllo del livello di riverbero globale
- **Chorus Level**: Controllo del livello di chorus globale
- Salvataggio automatico delle impostazioni

### ⚙️ Configurazione Hardware
- **Selezione dispositivi audio ALSA**: Scelta della scheda audio per l'output
- **Selezione dispositivi MIDI**: Scelta della tastiera/controller MIDI
- **Auto-discovery** di dispositivi audio e MIDI collegati
- Riavvio automatico del servizio dopo le modifiche

### 💾 Persistenza Stato
- Salvataggio automatico in `last_state.json` di:
  - Ultimo soundfont caricato
  - Livelli di gain, reverb e chorus
  - Dispositivi audio e MIDI selezionati
- Ripristino automatico all'avvio

### 🌐 Interfaccia Utente Moderna
- Design moderno con gradiente e animazioni
- Interfaccia responsive per mobile e tablet
- Temi scuri ottimizzati per l'uso in studio
- Feedback visivo immediato per tutte le operazioni
- Icone Font Awesome per una UI intuitiva

### 🔄 Sistema di Sincronizzazione
- Funzione "Refresh" per sincronizzare lo stato dell'interfaccia con FluidSynth
- Aggiornamento automatico delle selezioni degli strumenti
- Gestione dello stato globale in tempo reale

### 📡 Informazioni di Rete
- Visualizzazione hostname e indirizzi IP del sistema
- Supporto per connessioni USB e Ethernet/WiFi
- Ideale per configurazioni headless (senza monitor)

## 🔧 Requisiti di Sistema

### Software
- **Python**: 3.7 o superiore
- **Flask**: 3.0.0 o superiore
- **FluidSynth**: versione con supporto shell telnet
- **Sistema Operativo**: Linux (testato su Raspberry Pi OS)

### Hardware Consigliato
- **Raspberry Pi 3/4** o superiore (o qualsiasi PC Linux)
- **Raspberry Pi Zero 2W**: Supportato con ottimizzazioni specifiche (vedi sezione Ottimizzazione)
- **Scheda audio USB** (opzionale, può usare l'audio integrato)
- **Tastiera MIDI USB** o controller MIDI
- **Almeno 512 MB di RAM** disponibile per FluidSynth

### Pacchetti di Sistema
```bash
sudo apt-get install fluidsynth fluid-soundfont-gm fluid-soundfont-gs
sudo apt-get install alsa-utils jq  # Per gestione audio e parsing JSON
```

## 📦 Installazione

### Nota sui Percorsi
Il repository contiene una struttura di directory `home/matteo/` che riflette dove i file saranno installati sul sistema. Quando clonato in `/home/matteo/matsynth`, i percorsi completi saranno:
- Script avvio: `/home/matteo/matsynth/home/matteo/startfluid.sh`
- Applicazione: `/home/matteo/matsynth/home/matteo/matsynth_web/app.py`

### Ottimizzazione per Raspberry Pi Zero 2W

**IMPORTANTE**: Se stai installando su Raspberry Pi Zero 2W, esegui prima lo script di ottimizzazione:

```bash
cd /home/matteo/matsynth
sudo ./setup_pi_zero_optimizations.sh
```

Questo script:
- Configura swap a 512MB (essenziale per soundfont)
- Imposta CPU governor su "performance"
- Disabilita servizi non necessari
- Ottimizza parametri di sistema

Per monitorare le prestazioni in tempo reale:
```bash
./monitor_performance.sh
```

Consulta la [Guida Ottimizzazione Completa](OPTIMIZATION_GUIDE.md) per dettagli.

### 1. Clonare il Repository
```bash
cd /home/matteo
git clone https://github.com/araleslump73/matsynth.git
cd matsynth
```

### 2. Installare le Dipendenze Python
```bash
pip install -r requirements.txt
```

### 3. Creare le Directory Necessarie
```bash
sudo mkdir -p /usr/share/sounds/sf2
sudo mkdir -p /home/matteo/matsynth_web
```

### 4. Copiare i Soundfont
```bash
# Copiare i file SF2/SF3 nella directory dei soundfont
sudo cp *.sf2 /usr/share/sounds/sf2/
sudo cp *.sf3 /usr/share/sounds/sf2/
```

### 5. Rendere Eseguibile lo Script di Avvio
```bash
chmod +x /home/matteo/matsynth/home/matteo/startfluid.sh
```

## ⚙️ Configurazione

### Configurazione di Base in `app.py`

Modificare le seguenti costanti nel file `home/matteo/matsynth_web/app.py`:

```python
# Directory contenente i soundfont SF2/SF3
SF2_DIR = "/usr/share/sounds/sf2/"

# Configurazione connessione FluidSynth
FLUID_HOST = "127.0.0.1"  # Indirizzo del server FluidSynth
FLUID_PORT = 9800          # Porta telnet di FluidSynth

# File di stato per persistenza
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'
```

### Configurazione di FluidSynth

Lo script `startfluid.sh` configura automaticamente FluidSynth con parametri ottimali per Raspberry Pi Zero 2W:

```bash
fluidsynth -i -s \
  -g "$GAIN" \
  -o shell.prompt="" \
  -o synth.dynamic-sample-loading=1 \
  -o synth.sample-cache-size=1 \
  -o synth.lock-memory=0 \
  -a alsa \
  -o audio.alsa.device="$AUDIO_DEVICE" \
  -o audio.period-size=256 \
  -o audio.periods=2 \
  -o synth.cpu-cores=2 \
  -o synth.polyphony=64 \
  -o synth.midi-channels=16 \
  -o midi.autoconnect=1 \
  -o synth.reverb.active=yes \
  -o synth.reverb.level="$REVERB" \
  -o synth.reverb.room-size=0.6 \
  -o synth.chorus.active=yes \
  -o synth.chorus.level="$CHORUS" \
  -o synth.chorus.nr=1 \
  -o synth.chorus.speed=0.3 \
  -o synth.chorus.depth=5.0 \
  -r 44100 \
  -z 128 \
  "$SF2_PATH/$LAST_FONT"
```

#### Parametri Chiave (Ottimizzati per Pi Zero 2W):
- **`-g $GAIN`**: Gain master (volume)
- **`-o shell.prompt=""`**: Disabilita il prompt per comunicazione telnet pulita
- **`-o synth.dynamic-sample-loading=1`**: Caricamento dinamico dei sample per risparmiare RAM
- **`-o synth.sample-cache-size=1`**: Cache minima per risparmio memoria
- **`-o synth.lock-memory=0`**: Non blocca memoria (permette swap se necessario)
- **`-a alsa`**: Driver audio ALSA
- **`-o audio.alsa.device="$AUDIO_DEVICE"`**: Dispositivo audio specifico
- **`-o audio.period-size=256`**: Dimensione periodo ottimale per Pi Zero
- **`-o audio.periods=2`**: 2 periodi per bilanciamento latenza/stabilità
- **`-o synth.cpu-cores=2`**: Usa 2 core CPU (lascia 2 per OS/Flask)
- **`-o synth.polyphony=64`**: Limite 64 voci simultanee (previene overload)
- **`-o midi.autoconnect=1`**: Connessione automatica dispositivi MIDI
- **`-o synth.reverb.room-size=0.6`**: Reverb leggero (era 0.9)
- **`-o synth.chorus.nr=1`**: 1 oscillatore chorus (era 2, risparmia CPU)
- **`-r 44100`**: Sample rate 44.1 kHz
- **`-z 128`**: Buffer size 128 sample (~3ms latenza)

### Configurazione come Servizio systemd

Per avviare MatSynth automaticamente all'avvio del sistema:

```bash
sudo nano /etc/systemd/system/matsynth.service
```

Contenuto del file:

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

Abilitare e avviare il servizio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable matsynth.service
sudo systemctl start matsynth.service
```

Verificare lo stato:

```bash
sudo systemctl status matsynth.service
```

## 🚀 Utilizzo

### Avvio Manuale

```bash
/home/matteo/matsynth/home/matteo/startfluid.sh
```

L'applicazione web sarà disponibile su:
- **Locale**: `http://localhost:5000`
- **Rete**: `http://<indirizzo-ip>:5000`
- **Hostname**: `http://<hostname>.local:5000`

### Interfaccia Web

#### Pagina Principale (Mixer)

**1. Selezione Soundfont**
   - Usa il menu a tendina "SELECT SOUNDFONT" in basso
   - Seleziona un file SF2/SF3
   - Attendi 5 secondi per il caricamento (reload automatico)

**2. Configurazione Canali MIDI**
   - Ogni riga rappresenta un canale MIDI (1-16)
   - Clicca sulla riga per selezionare il canale target per i controlli
   - Usa il menu a tendina per selezionare lo strumento
   - La riga attiva è evidenziata in giallo

**3. Controlli Per-Canale**
   - **ATK (Attack)**: Tempo di attacco del suono (CC73)
   - **REL (Release)**: Tempo di rilascio del suono (CC72)
   - **CUT (Cutoff)**: Frequenza di taglio del filtro (CC74)
   - **RES (Resonance)**: Risonanza del filtro (CC71)
   - I controlli agiscono sul canale selezionato (indicato in alto)

**4. Effetti Master**
   - **MASTER VOLUME**: Regola il gain globale (0.0 = mute, 1.0 = normale, 3.0 = boost)
   - **MASTER REVERB**: Livello di riverbero globale
   - **MASTER CHORUS**: Livello di chorus globale

**5. Pulsante Refresh**
   - Sincronizza l'interfaccia con lo stato corrente di FluidSynth
   - Utile dopo connessioni MIDI esterne o modifiche manuali

#### Pagina Settings

**1. Network Status**
   - Visualizza hostname e indirizzi IP del sistema
   - Utile per accesso remoto

**2. Audio Output**
   - Seleziona la scheda audio ALSA per l'output
   - Mostra tutte le schede audio rilevate (hw:0, hw:1, ecc.)

**3. MIDI Input**
   - Seleziona la tastiera/controller MIDI
   - Autoconnessione al port di FluidSynth

**4. Save & Restart**
   - Salva le configurazioni hardware
   - Riavvia il servizio MatSynth
   - Attendi 5 secondi per il riavvio completo

### Utilizzo da Tastiera MIDI

Una volta configurato il dispositivo MIDI:

1. Suona la tastiera - il suono viene generato immediatamente
2. Gli strumenti sono assegnati per canale dall'interfaccia web
3. I controlli MIDI standard della tastiera (modulation, pitch bend, ecc.) funzionano normalmente
4. Le velocity delle note sono completamente supportate

## 🏗️ Architettura del Sistema

### Struttura dei File

```
matsynth/
├── README.md                           # Questo file
├── requirements.txt                    # Dipendenze Python
├── .gitignore                         # File da ignorare in Git
└── home/
    └── matteo/
        ├── startfluid.sh              # Script di avvio FluidSynth
        └── matsynth_web/
            ├── app.py                 # Backend Flask
            ├── static/
            │   └── style.css         # Stili CSS
            └── templates/
                ├── index.html        # Pagina mixer principale
                └── settings.html     # Pagina impostazioni
```

### Flusso di Comunicazione

```
┌─────────────────┐
│  Web Browser    │
│  (Frontend)     │
└────────┬────────┘
         │ HTTP/AJAX
         │ (port 5000)
         ▼
┌─────────────────┐
│  Flask App      │
│  (app.py)       │
└────────┬────────┘
         │ Telnet
         │ (port 9800)
         ▼
┌─────────────────┐      ┌──────────────┐
│  FluidSynth     │◄────►│  MIDI Input  │
│  (Synth Engine) │      │  (Keyboard)  │
└────────┬────────┘      └──────────────┘
         │
         ▼
┌─────────────────┐
│  Audio Output   │
│  (ALSA/hw:X)   │
└─────────────────┘
```

### Componenti Principali

#### 1. **app.py** - Backend Flask
- **Gestione comunicazioni**: Telnet con FluidSynth (socket TCP)
- **API REST**: Endpoint per tutte le operazioni
- **Persistenza stato**: Lettura/scrittura JSON
- **Discovery hardware**: Scansione dispositivi ALSA e MIDI
- **Gestione processo**: Riavvio servizio systemd

#### 2. **index.html** - Interfaccia Mixer
- **JavaScript frontend**: Gestione UI e chiamate AJAX
- **Gestione stato locale**: Sincronizzazione con backend
- **Event handlers**: Click, change, input per controlli
- **Rendering dinamico**: Popolamento dropdown strumenti

#### 3. **settings.html** - Configurazione Hardware
- **Scansione dispositivi**: Rilevamento audio/MIDI
- **Salvataggio persistente**: Scrittura su last_state.json
- **Riavvio servizio**: Chiamata systemd via subprocess

#### 4. **startfluid.sh** - Script di Avvio
- **Parsing JSON**: Lettura configurazione con jq
- **Pulizia processi**: Killall di istanze precedenti
- **Avvio FluidSynth**: Con parametri ottimizzati
- **Connessione MIDI**: Autoconnect con aconnect
- **Avvio Flask**: Server web sulla porta 5000

#### 5. **style.css** - Stili Moderni
- **Design gradiente**: Temi scuri con colori neon
- **Responsive layout**: Adattamento mobile/tablet
- **Animazioni CSS**: Transizioni fluide
- **Custom scrollbar**: Stile mixer channels

### Gestione dello Stato

Il file `last_state.json` contiene:

```json
{
  "font": "GeneralUser-GS.sf2",
  "gain": 1.0,
  "reverb.level": 0.4,
  "chorus.level": 0.4,
  "audio_device": "plughw:1",
  "midi_device": "20"
}
```

**Ciclo di vita**:
1. **Avvio**: `startfluid.sh` legge il JSON e configura FluidSynth
2. **Runtime**: Flask legge/scrive il JSON ad ogni modifica
3. **Riavvio**: Le impostazioni vengono ripristinate automaticamente

### Protocollo Telnet FluidSynth

MatSynth comunica con FluidSynth tramite comandi telnet:

| Comando | Descrizione | Esempio |
|---------|-------------|---------|
| `fonts` | Lista soundfont caricati | `fonts` |
| `load <path>` | Carica un soundfont | `load /path/file.sf2` |
| `unload <sfid>` | Scarica un soundfont | `unload 1` |
| `inst <sfid>` | Lista strumenti | `inst 1` |
| `select <ch> <sfid> <bank> <prog>` | Seleziona strumento | `select 0 1 0 0` |
| `cc <ch> <cc> <val>` | Invia Control Change | `cc 0 74 127` |
| `set <param> <val>` | Imposta parametro | `set synth.gain 1.5` |
| `channels` | Lista stato canali | `channels` |

### Timeout e Performance

- **Timeout socket telnet**: 2 secondi
- **Delay dopo set/select**: Immediato (no wait)
- **Delay dopo load/unload**: 0.1 secondi
- **Delay comandi lettura**: 0.2 secondi
- **Reload pagina dopo load SF2**: 5 secondi (caricamento RAM)

## 📚 Riferimento API

### Rotte Flask

#### Pagine Web

| Rotta | Metodo | Descrizione |
|-------|--------|-------------|
| `/` | GET | Pagina principale mixer |
| `/settings` | GET | Pagina impostazioni hardware |

#### Soundfont

| Rotta | Metodo | Descrizione | Risposta |
|-------|--------|-------------|----------|
| `/list_sf2` | GET | Lista file SF2/SF3 | JSON array di nomi file |
| `/load_sf2/<filename>` | GET | Carica soundfont | Testo conferma |
| `/get_instruments` | GET | Lista strumenti SF caricato | JSON array stringhe |

#### Controllo MIDI

| Rotta | Metodo | Descrizione | Parametri |
|-------|--------|-------------|-----------|
| `/select_prog/<ch>/<bank>/<prog>` | GET | Seleziona strumento | ch: 0-15, bank: 0-127, prog: 0-127 |
| `/cc/<ch>/<cc>/<val>` | GET | Invia Control Change | ch: 0-15, cc: 0-127, val: 0-127 |
| `/reset_channel/<ch>` | GET | Reset canale MIDI | ch: 0-15 |

#### Effetti

| Rotta | Metodo | Descrizione | Parametri |
|-------|--------|-------------|-----------|
| `/set_effect/<type>/<val>` | GET | Imposta effetto | type: gain, reverb.level, chorus.level<br>val: float |

#### Stato e Sincronizzazione

| Rotta | Metodo | Descrizione | Risposta |
|-------|--------|-------------|----------|
| `/get_state` | GET | Ottieni stato salvato | JSON oggetto stato |
| `/refresh_status` | GET | Stato canali FluidSynth | JSON con output "channels" |

#### Hardware e Rete

| Rotta | Metodo | Descrizione | Risposta |
|-------|--------|-------------|----------|
| `/api/network` | GET | Info rete (IP, hostname) | JSON con ips[], hostname |
| `/api/audio_devices` | GET | Lista schede audio | JSON con devices[], current |
| `/api/midi_devices` | GET | Lista dispositivi MIDI | JSON con devices[], current |
| `/api/save_hardware` | POST | Salva config hardware | JSON: {audio, midi} |

### Esempi di Chiamate API

#### JavaScript (dall'interfaccia web)

```javascript
// Caricare un soundfont
await fetch('/load_sf2/Piano.sf2');

// Selezionare uno strumento sul canale 0 (Grand Piano: bank 0, prog 0)
await fetch('/select_prog/0/0/0');

// Inviare un Control Change (Cutoff su canale 1)
await fetch('/cc/1/74/100');

// Impostare il master volume
await fetch('/set_effect/gain/1.5');

// Ottenere lo stato corrente
const res = await fetch('/get_state');
const state = await res.json();
console.log(state.gain); // 1.5
```

#### cURL (da terminale)

```bash
# Ottenere la lista di soundfont
curl http://localhost:5000/list_sf2

# Caricare un soundfont
curl http://localhost:5000/load_sf2/GeneralUser-GS.sf2

# Selezionare strumento
curl http://localhost:5000/select_prog/0/0/1

# Impostare il gain
curl http://localhost:5000/set_effect/gain/1.2

# Sincronizzare stato canali
curl http://localhost:5000/refresh_status
```

## 🔍 Risoluzione Problemi

### FluidSynth Non Si Connette

**Sintomo**: Errore "Connection refused" o timeout

**Soluzioni**:
```bash
# Verificare che FluidSynth sia in esecuzione
ps aux | grep fluidsynth

# Verificare che la porta 9800 sia in ascolto
sudo netstat -tlnp | grep 9800

# Riavviare il servizio
sudo systemctl restart matsynth.service

# Controllare i log
sudo journalctl -u matsynth.service -f
```

### Soundfont Non Si Carica

**Sintomo**: Interfaccia freeze o nessun suono

**Soluzioni**:
```bash
# Verificare permessi sui file SF2
ls -la /usr/share/sounds/sf2/

# Verificare spazio su disco
df -h

# Verificare RAM disponibile (soundfont richiedono molta RAM)
free -h

# Per Raspberry Pi Zero: usare soundfont SF3 compressi invece di SF2
```

### Audio Distorto o Latente

**Sintomo**: Audio crackling o latenza alta

**Soluzioni**:
```bash
# Ridurre buffer size in startfluid.sh
-z 32  # invece di 64

# Aumentare priorità processo
sudo nice -n -10 fluidsynth ...

# Usare scheda audio USB dedicata invece dell'audio integrato

# Verificare underrun ALSA
dmesg | grep -i alsa
```

### Dispositivi MIDI Non Rilevati

**Sintomo**: Tastiera MIDI non appare in Settings

**Soluzioni**:
```bash
# Verificare connessione USB
lsusb

# Verificare driver ALSA
aconnect -i

# Verificare permessi utente
sudo usermod -aG audio matteo

# Ricaricare moduli ALSA
sudo modprobe snd-seq-midi
```

### Interfaccia Web Non Risponde

**Sintomo**: Timeout o errori 500

**Soluzioni**:
```bash
# Verificare che Flask sia in esecuzione
ps aux | grep python

# Controllare errori Python
sudo journalctl -u matsynth.service | tail -50

# Verificare porta 5000
sudo netstat -tlnp | grep 5000

# Riavviare servizio web
sudo systemctl restart matsynth.service
```

### Servizio systemd Fallisce all'Avvio

**Sintomo**: `systemctl status matsynth` mostra "failed"

**Soluzioni**:
```bash
# Verificare sintassi script
bash -n /home/matteo/matsynth/home/matteo/startfluid.sh

# Controllare permessi esecuzione
chmod +x /home/matteo/matsynth/home/matteo/startfluid.sh

# Verificare percorsi nel service file
sudo systemctl cat matsynth.service

# Eseguire manualmente per vedere errori (assicurarsi che i percorsi corrispondano alla propria installazione)
/home/matteo/matsynth/home/matteo/startfluid.sh
```

## 💡 Best Practices

### Ottimizzazione per Raspberry Pi Zero 2W

Il Raspberry Pi Zero 2W ha risorse limitate (512MB RAM, CPU quad-core 1GHz). MatSynth è stato ottimizzato per funzionare efficientemente su questo hardware:

#### Ottimizzazioni Applicate

**FluidSynth** (`startfluid.sh`):
- **CPU cores ridotti a 2**: Lascia risorse per il sistema operativo e Flask
- **Buffer aumentato a 128**: Riduce il carico CPU (latenza ~3ms a 44.1kHz)
- **Polyphony limitata a 64 voci**: Previene picchi di memoria e CPU
- **Cache campioni ottimizzata**: `sample-cache-size=1` per memoria minima
- **Reverb/Chorus ridotti**: Parametri più leggeri (room-size 0.6, chorus nr=1)
- **Period size 256**: Bilanciamento ottimale latenza/CPU per Pi Zero

**Flask Application** (`app.py`):
- **Socket timeout ridotto a 1s**: Risposta più veloce
- **Buffer ridotti a 2KB**: Meno memoria per comunicazione
- **Threaded mode abilitato**: Migliore gestione richieste concorrenti
- **Caching lista SF2**: Riduce accessi al filesystem (cache 30s)
- **TCP_NODELAY attivo**: Comunicazione più reattiva con FluidSynth

#### Configurazione Sistema per Pi Zero 2W

1. **Swap File** (raccomandato per evitare out-of-memory):
```bash
# Aumenta swap a 512MB (se non già fatto)
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# Imposta: CONF_SWAPSIZE=512
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

2. **CPU Governor** (prestazioni massime):
```bash
# Imposta performance governor per latenza minima
sudo apt-get install cpufrequtils
echo 'GOVERNOR="performance"' | sudo tee /etc/default/cpufrequtils
sudo systemctl restart cpufrequtils
```

3. **Overclocking Moderato** (opzionale, aumenta calore):
```bash
sudo nano /boot/config.txt
# Aggiungi (overclock moderato e sicuro):
arm_freq=1200
over_voltage=2
gpu_mem=16
```

4. **Disabilita Servizi Non Necessari**:
```bash
# Libera risorse disabilitando servizi inutili
sudo systemctl disable bluetooth
sudo systemctl disable hciuart
sudo systemctl disable avahi-daemon
```

5. **Usa Soundfont SF3 Compressi**:
```bash
# SF3 usano ~10x meno RAM di SF2
# Esempio: GeneralUser GS (SF2: ~200MB, SF3: ~30MB)
# Scarica SF3 da: https://github.com/FluidSynth/fluidsynth/wiki/SoundFont
```

#### Monitoraggio Risorse

Verifica utilizzo risorse durante l'uso:

```bash
# CPU e Memoria in tempo reale
htop

# Solo FluidSynth e Python
watch -n 1 'ps aux | grep -E "fluidsynth|python3" | grep -v grep'

# Temperatura (importante su Pi Zero senza ventola)
vcgencmd measure_temp

# Memoria disponibile
free -h
```

#### Limiti e Raccomandazioni

**Soundfont consigliati per Pi Zero 2W**:
- ✅ **SF3 fino a 50MB**: Funzionamento ottimale
- ✅ **SF2 fino a 30MB**: Accettabile con swap
- ⚠️ **SF2 50-100MB**: Possibile ma lento al caricamento
- ❌ **SF2 > 100MB**: Sconsigliato (rischio crash)

**Polifonia effettiva**:
- Limitata a **64 voci simultanee** (configurazione ottimizzata)
- Per piano/organo: sufficiente per uso normale
- Per orchestrali densi: può troncare note su accordi complessi

**Latenza audio**:
- Buffer 128 @ 44.1kHz = **~3ms di latenza**
- Accettabile per performance live
- Per latenza minore: richiede CPU più potente

### Performance

1. **Usare soundfont SF3** invece di SF2 su dispositivi con poca RAM (Raspberry Pi Zero 2W)
2. **Polyphony già limitata** a 64 voci nella configurazione ottimizzata
3. **Reverb/chorus già ottimizzati** per Raspberry Pi Zero 2W
4. **Usare scheda audio USB** per migliore qualità e minor latenza (opzionale)

### Sicurezza

1. **Non esporre porta 5000 su Internet** senza autenticazione
2. **Usare firewall** per limitare accessi alla LAN locale
3. **Creare utente dedicato** invece di usare root o utente principale
4. **Limitare permessi** sui file di configurazione

```bash
sudo ufw allow from 192.168.1.0/24 to any port 5000
```

### Backup e Manutenzione

1. **Fare backup** del file `last_state.json` periodicamente
2. **Versionare** soundfont personalizzati
3. **Testare** dopo aggiornamenti di sistema
4. **Monitorare** uso RAM con soundfont grandi

```bash
# Backup automatico dello stato
cp /home/matteo/matsynth_web/last_state.json \
   /home/matteo/matsynth_web/last_state.json.bak
```

### Organizzazione Soundfont

1. **Nomenclatura chiara**: `Piano_Bright.sf2`, `Strings_Orchestra.sf3`
2. **Categorizzazione**: Creare sottodirectory per tipo
3. **Documentazione**: Mantenere un README con info sui soundfont
4. **Pulizia**: Rimuovere soundfont non utilizzati per liberare spazio

### Utilizzo Live

1. **Pre-caricare** il soundfont corretto prima dell'esibizione
2. **Testare** tutte le patch e effetti
3. **Avere backup**: Secondo Raspberry Pi configurato identico
4. **Disabilitare aggiornamenti** automatici prima di eventi importanti

## 🎓 Esempi Avanzati

### Multi-Canale per Performance Live

Configurazione tipica per tastiera split/layer:

```javascript
// Canale 0: Piano principale (layer inferiore)
await fetch('/select_prog/0/0/0');  // Acoustic Grand Piano

// Canale 1: Strings (layer superiore)
await fetch('/select_prog/1/0/48');  // String Ensemble

// Canale 9: Drums (sempre canale 10 per GM)
await fetch('/select_prog/9/128/0');  // Standard Drum Kit

// Regolare volumi relativi (via CC7 - Channel Volume)
await fetch('/cc/0/7/100');  // Piano al massimo
await fetch('/cc/1/7/70');   // Strings più bassi
await fetch('/cc/9/7/90');   // Drums bilanciati
```

### Automazione con Python

Script per cambiare setup automaticamente:

```python
import requests

BASE_URL = "http://localhost:5000"

def setup_piano_strings():
    """Setup piano + strings per ballad"""
    requests.get(f"{BASE_URL}/load_sf2/GeneralUser-GS.sf2")
    time.sleep(5)  # Attendi caricamento
    
    requests.get(f"{BASE_URL}/select_prog/0/0/0")   # Piano
    requests.get(f"{BASE_URL}/select_prog/1/0/48")  # Strings
    requests.get(f"{BASE_URL}/set_effect/reverb.level/0.7")
    requests.get(f"{BASE_URL}/set_effect/gain/1.2")
    
def setup_rock_organ():
    """Setup organ rock con Leslie effect"""
    requests.get(f"{BASE_URL}/select_prog/0/0/16")  # Rock Organ
    requests.get(f"{BASE_URL}/set_effect/chorus.level/0.8")
    requests.get(f"{BASE_URL}/set_effect/gain/1.8")
    requests.get(f"{BASE_URL}/cc/0/74/90")  # Cutoff più aperto

# Usa le funzioni
setup_piano_strings()
```

### Integrazione con DAW via MIDI

Per usare MatSynth come modulo sonoro per una DAW:

1. Configurare MIDI routing in Linux:
```bash
# Creare port virtuale MIDI
sudo modprobe snd-virmidi

# Connettere DAW -> MatSynth
aconnect <DAW_port> <FluidSynth_port>
```

2. Nella DAW (es. Ardour, Reaper):
   - Creare track MIDI
   - Selezionare output verso FluidSynth
   - Registrare/sequenziare normalmente

3. Controllare strumenti da MatSynth web interface

## 🤝 Contribuire

Contributi sono benvenuti! Per favore:

1. Fork il repository
2. Crea un branch per la feature (`git checkout -b feature/AmazingFeature`)
3. Commit le modifiche (`git commit -m 'Add some AmazingFeature'`)
4. Push al branch (`git push origin feature/AmazingFeature`)
5. Apri una Pull Request

### Aree di Miglioramento

- [ ] Autenticazione utente per accesso remoto sicuro
- [ ] Presets salvabili e caricabili
- [ ] Editor grafico per curve ADSR
- [ ] Supporto per file MIDI playback
- [ ] Registrazione performance in MIDI
- [ ] Visualizzazione spettro audio in tempo reale
- [ ] Supporto per SFZ soundfont
- [ ] Modalità multi-utente per jam session
- [ ] App mobile nativa (iOS/Android)

## 📄 Licenza

MIT License

Copyright (c) 2026 MatSynth Project

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

**Sviluppato con ❤️ per la comunità musicale open source**

Per supporto e discussioni: [GitHub Issues](https://github.com/araleslump73/matsynth/issues)
