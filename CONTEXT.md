# MatSynth — Documento di Contesto e Specifiche

> **Versione analizzata**: branch `feature/daw_enanched`  
> **Data**: 2026-03-25  
> **Aggiornare questo file** ogni volta che si introducono cambiamenti architetturali rilevanti.

---

## 1. Descrizione del Progetto

**MatSynth** è un controller web multitimbrico per **FluidSynth**, progettato per girare su hardware ultra-limitato (Raspberry Pi Zero 2 W). Permette di gestire un sintetizzatore MIDI a 16 canali, un micro-DAW per registrazione/playback loop-based, e le impostazioni hardware, tutto da browser senza installare nulla sul client.

### Filosofia Fondamentale
- **Leggerezza prima di tutto**: ogni scelta tecnica (librerie, polling, payload WebSocket) deve considerare i vincoli dell'hw target.
- **Il client è tablet o PC**: il frontend gira sul browser del client (non sul Pi), quindi il carico di rendering e JS è sul dispositivo dell'utente. Framework complessi (React, Vue, Svelte...) sono tecnicamente compatibili, ma attualmente non usati per semplicità di deploy.
- **Controllo locale**: il dispositivo è headless, connesso alla rete locale. Non c'è cloud, non c'è autenticazione.

---

## 2. Stack Tecnologico

| Layer | Tecnologia | Note vincoli |
|-------|-----------|--------------|
| Backend | Python 3.7+, Flask ≥ 3.0 | Minimo overhead, no ORM |
| Real-time | Flask-SocketIO ≥ 5.3, `async_mode='threading'` | Threading (non eventlet/gevent) per compatibilità Pi Zero 2W |
| Audio Engine | FluidSynth (daemon) | Controllato via TCP socket locale (porta 9800) |
| MIDI I/O | `mido` library | Lettura tastiera fisica → FluidSynth output |
| Frontend | HTML5 + Bootstrap 5 (CDN) + CSS custom | Vanilla JS attuale — framework JS (React, Vue, Svelte) compatibili in futuro; il build risiede su dev machine, non sul Pi |
| Deploy | systemd service + script bash/ps1/bat | `matsynth.service`, `startfluid.sh` |
| Stato persistente | JSON file (`last_state.json`) + scrittura atomica | Lock threading, fsync, rename atomico |

### Dipendenze Python (`requirements.txt`)
```
Flask>=3.0.0
flask-socketio>=5.3.0
```
> **`mido` non è in requirements.txt** — da aggiungere.

### Dipendenze di sistema (Raspberry Pi OS / Debian)
```
fluidsynth  alsa-utils  jq  python3
```

---

## 3. Architettura del Sistema

```
[Browser]
    │  HTTP REST (Flask routes)
    │  WebSocket (Socket.IO /  namespace)
    ▼
[app.py — Flask Server]  ←──  porta 5000 (default Flask)
    │  TCP socket (porta 9800)    │  mido ALSA ports
    ▼                             ▼
[FluidSynth daemon]        [MultiTrackDAW — daw_recorder.py]
    │                             │
    ▼                             │
[ALSA audio out]          [MIDI Input (tastiera fisica)]
[ALSA MIDI ports]         [MIDI Output → FluidSynth]
```

### Flusso di avvio (`startfluid.sh`)
1. Legge `last_state.json` via `jq` (soundfont, audio device, gain, reverb, chorus, MIDI device).
2. Avvia **FluidSynth** in background con parametri dinamici:
   - `-s` (server TCP su 9800), `-i` (no interactive prompt)
   - `synth.cpu-cores=3`, `z 64` (buffer piccolo = bassa latenza)
   - `synth.dynamic-sample-loading=1` (riduce RAM)
   - `midi.autoconnect=1`
3. Attende 2 secondi.
4. (Opzionale) Connette il device MIDI tramite `aconnect`.
5. Avvia `python3 app.py`.

---

## 4. Struttura File

```
matsynth/
├── CONTEXT.md                  ← questo file
├── README.md
├── requirements.txt
├── deploy.sh / deploy.ps1 / deploy.bat
├── setup-ssh-key.sh
└── home/matteo/
    ├── startfluid.sh           ← script avvio completo
    └── matsynth_web/
        ├── app.py              ← Flask app + tutte le API REST + SocketIO handlers
        ├── daw_recorder.py     ← classe MultiTrackDAW (MIDI recording/playback)
        ├── static/
        │   └── style.css       ← stili custom (dark theme, sliders verticali, canvases)
        └── templates/
            ├── index.html      ← UI principale (DAW + Mixer + Channel Controls)
            ├── presets.html    ← gestione preset
            └── settings.html  ← impostazioni hardware/rete
```

### Path fissi (hardcoded — da parametrizzare in futuro)
```python
SF2_DIR    = "/usr/share/sounds/sf2/"
STATE_FILE = '/home/matteo/matsynth_web/last_state.json'
PRESETS_DIR= '/home/matteo/matsynth_web/presets/'
MIDI_DIR   = '/home/matteo/matsynth_web/midi/'
FLUID_HOST = "127.0.0.1"
FLUID_PORT = 9800
```

---

## 5. Moduli Funzionali

### 5.1 `app.py` — Flask Application

#### Comunicazione con FluidSynth (`send_fluid`)
- Connessione TCP socket per ogni comando (no connessione persistente).
- Timeout: **2 secondi**.
- Comandi `set/select/cc/unload/load` → nessuna attesa risposta (fire & forget).
- Comandi di lettura (`inst`, `fonts`, `channels`) → `sleep(0.2)` + `recv` loop.
- **Lock globale `STATE_LOCK`** (threading.Lock) per tutte le scritture su `last_state.json`.

#### Gestione Stato (`last_state.json`)
```json
{
  "gain": 1.0,
  "reverb.level": 0.4,
  "chorus.level": 0.4,
  "font": "GeneralUser-GS.sf2",
  "audio_device": "plughw:0",
  "midi_device": "",
  "channels": {
    "0": { "bank": 0, "program": 0, "volume": 100, "attack": 64,
           "decay": 64, "release": 64, "cutoff": 64, "resonance": 64 },
    ...
  }
}
```
- Scrittura **atomica**: write `.tmp` → `fsync` → `os.replace`.
- File corrotto → rinomina con timestamp, riparte dai default.

#### REST API — Rotte Principali

| Metodo | Route | Funzione |
|--------|-------|----------|
| GET | `/` | Mixer principale |
| GET | `/presets` | Pagina preset |
| GET | `/settings` | Pagina impostazioni |
| GET | `/list_sf2` | Lista soundfont disponibili |
| GET | `/load_sf2/<filename>` | Carica soundfont (unload tutti + load nuovo) |
| GET | `/get_instruments` | Lista strumenti del soundfont attivo |
| GET | `/select_prog/<chan>/<bank>/<prog>` | Seleziona strumento su canale |
| GET | `/cc/<chan>/<cc>/<val>` | Invia CC MIDI + salva stato |
| GET | `/set_effect/<type>/<val>` | Imposta gain/reverb/chorus globali |
| GET | `/refresh_status` | Legge stato canali da FluidSynth (`channels`) |
| GET | `/get_state` | Ritorna `last_state.json` completo |
| GET | `/reset_channel/<chan>` | Reset controllori MIDI canale |
| GET | `/api/network` | Hostname + IP del dispositivo |
| GET | `/api/audio_devices` | Lista schede audio ALSA (`aplay -l`) |
| GET | `/api/midi_devices` | Lista device MIDI (`aconnect -i`) |
| POST | `/api/save_hardware` | Salva impostazioni HW + riavvia servizio systemd |
| GET | `/api/capture_current_config` | Snapshot configurazione completa |
| GET | `/api/presets/list` | Lista preset salvati |
| POST | `/api/presets/save` | Salva preset corrente |
| GET | `/api/presets/load/<filename>` | Carica dati preset |
| DELETE | `/api/presets/delete/<filename>` | Elimina preset |
| POST | `/api/presets/rename` | Rinomina preset |
| POST | `/api/presets/apply` | Applica preset a FluidSynth |
| GET/POST | `/api/daw/*` | Tutte le API DAW (vedi §5.2) |

#### SocketIO Events (server → client)
| Evento | Payload | Frequenza |
|--------|---------|-----------|
| `daw_state_update` | stato completo DAW | su ogni cambio stato |
| `daw_tick` | `{t, is_playing, is_recording, bpm, beats_per_measure}` + opz. `track_counts` | ~10 Hz in rec, ~10 Hz in play |
| `record_activity` | `{channels: [{channel, count}]}` | ~20 Hz in rec, solo canali con note attive |

#### SocketIO Events (client → server)
| Evento | Funzione |
|--------|---------|
| `cc_update` | Aggiorna CC via WebSocket (alternativa all'HTTP polling) |
| `effect_update` | Aggiorna effetti globali via WebSocket |

---

### 5.2 `daw_recorder.py` — MultiTrackDAW

#### Struttura dati
```python
tracks: defaultdict(list)  # {channel_id: [(beat_position: float, midi_msg: bytes), ...]}
armed:  {0..15: bool}       # traccia pronta per recording
muted:  {0..15: bool}       # traccia mutata
has_data: {0..15: bool}     # traccia ha eventi
```
- **Gli eventi sono salvati in beat** (float), non in secondi. Questo permette cambi di BPM senza perdita di dati.
- `beat_position = elapsed_seconds / beat_duration`

#### Thread Architecture
| Thread | Scopo | Frequenza sleep |
|--------|-------|----------------|
| `_update_loop` (daemon) | Emette `daw_tick` / `record_activity` via WebSocket | 50ms in rec, 100ms in play, 500ms idle |
| `record_thread` (daemon) | Legge MIDI input, scrive `tracks[]` | polling `iter_pending()` + sleep 1ms |
| `playback_thread` (daemon) | Legge `tracks[]`, scrive MIDI output con timing preciso | `time.sleep` event-driven |
| `metronome_thread` (daemon) | Suona click metronomo su canale 9 (drum) | beat-synchronized |

#### Ottimizzazioni per Pi Zero 2W
- Payload WebSocket minimo: invia `track_counts` solo se cambiati E con debounce 250ms.
- Buffer MIDI flush all'avvio registrazione (scarta messaggi accumulati).
- `_send_all_notes_off()` prima di ogni start/stop per evitare note stucked.
- `dynamic-sample-loading=1` in FluidSynth per ridurre consumo RAM.
- Buffer ALSA `-z 64` (latenza ~1.5ms a 44100 Hz).
- `synth.cpu-cores=3` (Pi Zero 2W ha 4 core, 1 libero per OS/Flask).

#### Transport Controls
- **PLAY**: `start_playback()` — riprende dalla posizione corrente (non da 0).
- **REC**: `start_recording()` — avvia su tutti i canali armati; se ci sono tracce esistenti avvia anche il playback simultaneo (overdub).
- **STOP**: `stop_all()` + `_send_all_notes_off()`.
- **REWIND**: `stop_all()` + reset `timeline_position = 0.0`.
- **PANIC**: `_send_all_notes_off()` su tutti i 16 canali.

#### Quantizzazione Post-Registrazione
- Algoritmo: accoppia `note_on`/`note_off`, quantizza `note_on`, calcola `note_off = note_on_q + durata_originale`.
- Parametri: `grid_beats` (1/4, 1/8, 1/16...), `strength` (0–1), `swing` (0.5 = straight).
- Non disponibile durante play/rec.

#### MIDI Port Discovery (priorità tastiera fisica)
```
FANTOM > SINCO > USB > altro  (esclusi 'MIDI THROUGH')
FluidSynth output: cerca 'FLUID' o '128:0' nei nomi porta
```

#### REST API DAW

| Metodo | Route | Descrizione |
|--------|-------|-------------|
| GET | `/api/daw/state` | Stato completo DAW |
| POST | `/api/daw/record/start` | Avvia registrazione (canali armati) |
| POST | `/api/daw/record/stop` | Ferma registrazione |
| POST | `/api/daw/play/start` | Avvia playback |
| POST | `/api/daw/play/stop` | Ferma playback |
| POST | `/api/daw/stop_all` | Stop tutto |
| POST | `/api/daw/panic` | All Notes Off |
| POST | `/api/daw/rewind` | Torna a 00:00:00 |
| POST | `/api/daw/track/<ch>/arm` | Arma/disarma traccia |
| POST | `/api/daw/track/<ch>/mute` | Muta/smuta traccia |
| POST | `/api/daw/track/<ch>/solo` | Solo/unsolo traccia |
| POST | `/api/daw/track/<ch>/clear` | Cancella traccia |
| POST | `/api/daw/clear_all` | Cancella tutte le tracce |
| POST | `/api/daw/bpm` | Imposta BPM (30–300) |
| POST | `/api/daw/time_signature` | Imposta time signature (3 o 4) |
| POST | `/api/daw/position` | Imposta posizione timeline (secondi) |
| POST | `/api/daw/loop_points` | Imposta loop start/end (in beat) |
| POST | `/api/daw/loop/toggle` | Toggle loop on/off |
| POST | `/api/daw/undo` | Undo ultima operazione distruttiva |
| POST | `/api/daw/redo` | Redo ultima operazione annullata |
| POST | `/api/daw/quantize` | Quantizza tracce selezionate |
| GET | `/api/daw/density_map` | Mappa densità note (slot 1/8 beat) |
| GET | `/api/daw/track/<ch>/activity` | Intervalli note per traccia |
| POST | `/api/daw/midi/save` | Esporta MIDI file (.mid) |
| GET | `/api/daw/midi/list` | Lista file MIDI salvati |
| GET | `/api/daw/midi/download/<filename>` | Download file MIDI |
| DELETE | `/api/daw/midi/delete/<filename>` | Elimina file MIDI |
| POST | `/api/daw/range/clear` | Cancella range di beat su un canale |
| POST | `/api/daw/seek_beat` | Seek to beat position (works during playback) |
| POST | `/api/daw/copy` | Copy selection events to clipboard |
| POST | `/api/daw/paste` | Paste clipboard events at target beat |
| POST | `/api/daw/track/<ch>/duplicate` | Duplicate track to first empty channel |
| POST | `/api/daw/track/<ch>/rename` | Rename track (max 32 chars) |
| POST | `/api/daw/track/<ch>/color` | Set track color palette index (0-15) |
| GET | `/api/daw/full_density_map` | Full density map (cached, invalidated on data change) |

---

### 5.3 Frontend (`index.html`)

#### Sezioni UI (collapsibili)
1. **MICRO-DAW RECORDER** — transport, BPM, time signature, zoom, 16 track rows con canvas.
2. **MIDI CHANNELS** — 16 righe, dropdown strumento per canale.
3. **CHANNEL CONTROLS** — slider CC (volume CC7, pan CC10, attack CC73, decay CC75, release CC72, cutoff CC74, resonance CC71).
4. **MASTER VOLUME** — slider gain 0–3 (MUTE/NORMAL/BOOST).
5. **MASTER EFFECTS** — Reverb e Chorus globali.

#### Rendering Timeline (Canvas)
- 16 canvas `<canvas id="track-canvas-N">` (altezza 22px).
- Rendering impulsi nota (intervalli uniti) via `getFullDensityMap()` + `getTrackActivity()`.
- Zoom: 10–200 px/beat (slider `daw-zoom-range`).
- Drag inerziale sulla timeline (momentum scrolling).
- Snap grid: Free, 1, 1/2, 1/4, 1/8 (default), 1/16.
- Select mode: selezione range temporale per delete eventi.
- Playhead animato via `requestAnimationFrame`.

#### Gestione Stato JavaScript
```javascript
let dawState = null;          // stato DAW (da WebSocket)
let instrumentData = [];      // lista strumenti soundfont
let activeControlChannel = 0; // canale attivo per CC sliders
let hiddenTracks = new Set(); // tracce nascoste nell'UI
let selectMode = false;
let selectionInBeat, selectionOutBeat; // range selezione
```

#### WebSocket Client
- `socket.io` CDN.
- Listeners: `daw_state_update`, `daw_tick`, `record_activity`.
- Emitters: `cc_update`, `effect_update` (usati come alternativa HTTP per latenza ridotta).

---

## 6. Vincoli Hardware — Raspberry Pi Zero 2 W

| Risorsa | Specifica | Impatto sul progetto |
|---------|-----------|---------------------|
| CPU | ARM Cortex-A53 quad-core 1 GHz | Flask in threading, no coroutines pesanti |
| RAM | 512 MB | Soundfont leggeri (SF3 compresso, max ~50 MB), no caching grosso |
| Storage | microSD (lento, fragile) | Scrittura atomica stato, minimo I/O |
| Rete | WiFi 2.4 GHz onboard | Latenza variabile LAN, WebSocket va bene |
| Audio | No jack 3.5mm nativo — USB audio | ALSA `plughw:N`, buffer 64 campioni |
| MIDI | USB OTG (singola porta) | Un hub USB per tastiera + audio |

### Regole per sviluppatori
- **Mai** caricare soundfont grandi (> 100 MB SF2 non compressi).
- **Mai** usare sleep() lunghi nel thread principale Flask.
- **Limitare** le chiamate `send_fluid()` (ogni call apre/chiude socket TCP).
- **Preferire** WebSocket a polling HTTP per aggiornamenti frequenti.
- **Minificare** payload JSON: no campi ridondanti, no nested inutili.
- **No** librerie Python con dipendenze C pesanti non già disponibili su Pi OS.
- `async_mode='threading'` in SocketIO — **non** cambiare a eventlet/gevent senza test.
- **Framework JS frontend**: React, Vue, Svelte o simili sono accettabili perché il bundle viene servito staticamente e l'esecuzione è interamente sul browser del client (tablet/PC). Il Pi Zero 2W serve solo file statici e API REST/WebSocket — nessun overhead aggiuntivo lato server.

---

## 7. Preset System

### Formato preset JSON
```json
{
  "name": "My Preset",
  "created": "2025-01-01 12:00:00",
  "font": "GeneralUser-GS.sf2",
  "channels": [
    {
      "channel": 0, "bank": 0, "program": 0,
      "volume": 100, "attack": 64, "decay": 64,
      "release": 64, "cutoff": 64, "resonance": 64,
      "instrument_name": "Acoustic Grand Piano"
    }
  ],
  "global_settings": {
    "gain": 1.0,
    "reverb_level": 0.4,
    "chorus_level": 0.4
  }
}
```
**Nota critica**: il soundfont **non viene caricato automaticamente** nell'apply per evitare timeout su hardware lento. L'utente deve caricarlo manualmente prima di applicare il preset.

---

## 8. MIDI Channels & CC Map

| CC# | Parametro | Range | Default |
|-----|-----------|-------|---------|
| 7 | Volume | 0–127 | 100 |
| 71 | Resonance | 0–127 | 64 |
| 72 | Release | 0–127 | 64 |
| 73 | Attack | 0–127 | 64 |
| 74 | Cutoff (filter freq) | 0–127 | 64 |
| 75 | Decay | 0–127 | 64 |

Canali MIDI: **0–15** (internamente), visualizzati **1–16** nell'UI.  
Canale 9 (0-indexed) = drum kit per il metronomo.

---

## 9. Known Issues & Debiti Tecnici

| # | Tipo | Descrizione | Priorità |
|---|------|-------------|---------|
| 1 | Bug | `mido` non è in `requirements.txt` | Alta |
| 2 | Security | Path traversal potenziale su `/load_sf2/<filename>` (nessuna sanificazione del path) | Alta |
| 3 | Security | Path traversal potenziale su `/api/presets/load/<filename>` e `/delete/<filename>` | Alta |
| 4 | Security | `subprocess.check_output(['hostname', '-I'])` — input non controllato, ma locale | Bassa |
| 5 | Design | Path hardcoded (`/home/matteo/...`) — non portabile | Media |
| 6 | Design | `sf_id` globale non thread-safe (nessun lock) | Media |
| 7 | Performance | Ogni `send_fluid()` apre/chiude socket — considerare pool/connessione persistente | Media |
| 8 | UX | Nessun feedback visivo durante caricamento soundfont (può durare 5–10 sec su Pi Zero) | Media |
| 9 | UX | `resetUISliders()` chiamato ad ogni cambio strumento — perde CC personalizzati | Bassa |
| 10 | Test | Nessun test automatico present | Alta |

---

## 10. Ruoli del Team e Agent Copilot

Ogni ruolo ha un corrispondente file agent in `.github/agents/` che configura Copilot con competenze, tool e regole specifiche. Il **Tech Lead** è il punto di ingresso che coordina gli altri.

| Ruolo | Agent file | Responsabilità principale |
|-------|-----------|--------------------------|
| Tech Lead (coordinatore) | `tech-lead.agent.md` | Analisi richieste, pianificazione, delega ai sotto-agent |
| UX Designer | `ux-designer.agent.md` | UI/UX touch-first, wireframe, accessibilità, design DAW |
| Esperto DAW / DSP | `daw.agent.md` | Ottimizzazione audio, timeline, transport, rendering Canvas |
| Esperto Standard Musicali | `daw-expert.agent.md` | Validazione MIDI, SF2, timing, quantizzazione, standard DAW |
| Programmatore Full-Stack | `programmer.agent.md` | Implementazione Flask, SocketIO, JS, Canvas API |
| QA / Security Engineer | `qa-engineer.agent.md` | Test pytest, review OWASP, performance Pi, validazione MIDI |

Regole globali condivise: `.github/copilot-instructions.md`

### UX Designer (`ux-designer.agent.md`)
- **Dominio**: `index.html`, `presets.html`, `settings.html`, `style.css`.
- **Focus attuale**: usabilità su **tablet e PC** (target primario), feedback visivo latenza, accessibilità controlli DAW, design professionale dell'interfaccia grafica. 
- **Vincoli**: il rendering avviene sul browser del client, quindi non ci sono limiti di CPU/RAM lato UI. Framework JS complessi (React, Vue, Svelte) sono compatibili; il processo di build risiede sulla dev machine, non sul Pi. Attualmente si usa Bootstrap 5 + vanilla JS — valutare migrazione se la complessità UI lo richiede.

### Esperto DAW / DSP (`daw.agent.md`)
- **Dominio**: `daw_recorder.py` (algoritmi e ottimizzazione), UI DAW in `index.html` (Canvas, timeline, transport).
- **Aree chiave**: ottimizzazione playback/recording per Pi Zero, rendering Canvas iper-ottimizzato, loop di riproduzione, threading audio.
- **Vincoli hardware**: latenza audio/MIDI su Pi Zero, limitazioni buffer, CPU budget critico.

### Esperto Standard Musicali (`daw-expert.agent.md`)
- **Dominio**: standard MIDI 1.0/2.0, SF2/SF3, SMF, General MIDI, FluidSynth.
- **Aree chiave**: validazione timing, quantizzazione, swing, metronomo, loop point, esportazione MIDI standard, mappa CC, teoria musicale applicata.
- **Ruolo**: consulenza e validazione — non implementa direttamente, definisce le specifiche algoritmiche per `programmer` e `daw`.

### Programmatore Full-Stack (`programmer.agent.md`)
- **Dominio**: tutti i file (`*.py`, `*.html`, `*.css`, `*.js`, deploy scripts, `startfluid.sh`).
- **Linguaggi**: Python (backend), JavaScript ES6+ vanilla (frontend).
- **Aree chiave**: Flask API, SocketIO, comunicazione FluidSynth, state management, concorrenza threading, sicurezza path, deploy systemd.
- **Vincoli**: Python 3.7+, Flask threading, niente DB relazionale.
- **Stile**: nessun linter configurato, inglese nei commenti/log, PEP8 suggerito.

### QA / Security Engineer (`qa-engineer.agent.md`)
- **Dominio**: test di integrazione Flask, test MIDI (mock), test UX browser, review sicurezza.
- **Tooling suggerito**: `pytest` + `pytest-flask` + `unittest.mock` per isolare FluidSynth/MIDI.
- **Test critici**: scrittura stato atomica, path traversal, comportamento sotto latenza elevata, OWASP Top 10.

---

## 11. Environment & Deploy

### Servizio systemd (esempio)
```ini
[Unit]
Description=MatSynth Web Controller
After=network.target sound.target

[Service]
User=matteo
ExecStart=/home/matteo/matsynth/home/matteo/startfluid.sh
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

### Deploy scripts forniti
- `deploy.sh` — Linux/Mac
- `deploy.ps1` — Windows PowerShell
- `deploy.bat` — Windows CMD
- `setup-ssh-key.sh` — setup chiave SSH per deploy

### Avvio manuale
```bash
/home/matteo/matsynth/home/matteo/startfluid.sh
```

---

## 12. Glossario

| Termine | Significato |
|---------|-------------|
| SF2/SF3 | SoundFont — file audio campionato per FluidSynth |
| BPM | Beats Per Minute — velocità del metronomo |
| Beat | Unità musicale di base (es. 60 BPM = 1 beat/sec) |
| CC | MIDI Control Change — parametro di controllo (volume, filtro, ecc.) |
| Bank | Gruppo di programmi MIDI (selezione via MSB/LSB) |
| Program | Numero strumento MIDI all'interno di un bank |
| ARM | Stato traccia pronta per registrazione |
| Overdub | Registrare su tracce esistenti in riproduzione |
| Quantize | Snap automatico delle note alla griglia ritmica |
| Swing | Ritardo rhythmico su suddivisioni dispari (groovy feel) |
| ALSA | Linux audio/MIDI subsystem |
| plughw:N | Identificatore dispositivo audio ALSA |
| aconnect | Tool ALSA per collegare porte MIDI |
| Telnet-like | Protocollo TCP testuale usato da FluidSynth shell |
