# MatSynth — Copilot Global Instructions

Leggi il file **CONTEXT.md** (root del repository) **solo quando necessario**:
- Prima di **implementare feature** o **modificare architettura** (API, threading, FluidSynth, MIDI)
- Quando serve conoscere **vincoli hardware Pi Zero 2W**, **API esistenti** o **known issues**

**NON leggere** CONTEXT.md per: domande semplici, fix CSS/HTML isolati, risposte conversazionali, operazioni su file che già conosci dal contesto della chat.

---

## 1. Regole Globali

1. **Backend = Pi Zero 2W** — ogni modifica Python/Flask deve essere leggera. Nessun sleep nel thread principale, nessuna libreria C non disponibile su Pi OS.
2. **Frontend = browser del client (tablet/PC)** — nessun vincolo di performance lato UI. Framework JS (React, Vue, Svelte) sono accettabili.
3. **Dopo ogni modifica architetturale** aggiorna CONTEXT.md (sezione pertinente + Known Issues se necessario).
4. **Sicurezza**: controlla sempre path traversal su filename ricevuti da URL. Sanitizza con `os.path.basename()`.
5. **Lingua**: commenti nel codice in inglese, log in inglese, risposte all'utente in italiano.
6. **SCRITTURA OBBLIGATORIA SUI FILE** — Quando devi implementare codice, **DEVI** usare `replace_string_in_file`, `multi_replace_string_in_file` o `create_file` per scrivere direttamente sui file di progetto. **MAI** proporre snippet di codice in chat senza applicarli. Se ti viene chiesto di implementare, il tuo output deve essere file modificati, non blocchi di codice da copiare. Leggi il file con `read_file` prima di modificarlo.

---

## 2. Processo di Lavoro (senza sub-agent)

Copilot gestisce **tutto internamente** — analisi requisiti, piano tecnico, implementazione, QA — usando la conoscenza specialistica nelle sezioni seguenti.

### Processo Standard (feature, cambiamenti funzionali):
1. **Raccogli** la richiesta dell'utente
2. **Analisi requisiti** (ruolo: Business Analyst) — se la richiesta è ambigua, formula domande di chiarimento all'utente prima di procedere. Quando i requisiti sono chiari, definisci mentalmente: requisiti funzionali, criteri di accettazione, edge case, impatto su funzionalità esistenti.
3. **Piano tecnico** (ruolo: Tech Lead) — analizza impatto architetturale, file coinvolti, rischi Pi Zero 2W, sequenza di implementazione.
4. **Implementazione** (ruolo: Programmer) — leggi i file, scrivi il codice direttamente usando `replace_string_in_file` / `multi_replace_string_in_file` / `create_file`.
5. **Verifica** con `get_errors`
6. Se servono competenze audio/MIDI, UX o sicurezza: applica le regole delle sezioni specialistiche sotto.

### Processo Rapido (bug fix, fix minori, operazioni note):
1. **Analizza** la richiesta — se è un bug chiaro o un fix puntuale, salta analisi requisiti e piano tecnico
2. **Implementa direttamente** e verifica con `get_errors`

---

## 3. Competenze Specialistiche Integrate

### 3.1 Programmazione (ex Programmer Agent)

**Stack**: Flask + SocketIO (`async_mode='threading'`) + mido + FluidSynth (TCP) | HTML5 + Bootstrap 5 + Vanilla JS + Canvas API

#### Regole Backend (Pi Zero 2W)
- Mai `sleep()` nel thread principale Flask
- Sempre `STATE_LOCK` per accesso a `last_state.json`
- `os.path.basename()` su filename da URL
- `async_mode='threading'` — non cambiare mai
- No librerie C non disponibili su Pi OS
- Gestire eccezioni su FluidSynth/subprocess con try/except
- Log: `print(f"[Modulo] messaggio")`

#### Regole Frontend
- No DOM reflow in loop — solo `transform`, `opacity`, `requestAnimationFrame`
- WebSocket payload minimi — niente dati ridondanti
- Debounce/throttle su chiamate REST intensive

---

### 3.2 Ottimizzazione Audio/DAW (ex DAW Agent)

**Target**: Pi Zero 2W — quad-core ARM 1 GHz, 512 MB RAM

#### Performance Backend Python
- `send_fluid()` è costoso (TCP open/close) — minimizzare le call
- Pre-calcolare ciò che è possibile, evitare calcoli nel loop di playback
- Thread daemon per operazioni continue
- CPU budget: Flask ~10%, FluidSynth ~60%, DAW threads ~20%, OS ~10%

#### Performance Frontend JS/Canvas
- Rendering: solo `requestAnimationFrame` + Canvas puro o CSS `transform`/`opacity`
- **Zero DOM reflow** in loop — dirty rectangles, off-screen rendering, bitmasking
- Pre-allocare array per evitare GC durante playback
- WebSocket payload minimi

#### Principio guida
Se una soluzione è costosa in cicli di clock, **scartala e proponi alternativa** ("smoke and mirrors").

---

### 3.3 Standard MIDI e Musica (ex DAW-Expert Agent)

#### Competenze da applicare quando si tocca codice MIDI/audio:
- **MIDI 1.0**: note_on/off, CC (mappa 0-127, MSB/LSB, NRPN), PC, SysEx, timing (tick, PPQ, MTC, MIDI Clock, SPP)
- **General MIDI**: GM/GM2, drum map ch10, Roland GS, Yamaha XG
- **SoundFont SF2/SF3**: generatori, modulatori, loop points, compressione Ogg
- **SMF**: formato 0/1/2, delta time VLQ, meta-events
- **Sequencer**: PPQ, tempo map, swing engine, groove quantize, humanizzazione
- **FluidSynth**: comandi shell, `synth.*` params, limitazioni (no automation continua, solo SF2/SF3)
- **Timing**: scale logaritmiche volume, latenza MIDI accettabile (<10ms live, <1ms click), beat↔tick↔secondi

#### Quando codifichi logica MIDI:
- Verifica correttezza musicale dell'implementazione
- Controlla vincoli FluidSynth (cosa è possibile vs workaround)
- Attenzione alle "bandiere rosse" — scelte che sembrerebbero corrette tecnicamente ma un musicista troverebbe sbagliate

---

### 3.4 UX Design (ex UX-Designer Agent)

#### Regole di Design
- **Touch-first**: target minimo 44×44px, gesture touch (drag, tap, long-press)
- **Dark theme**: preserva stile dark + accent rosa/magenta
- **Feedback immediato**: risposta visiva entro 16ms (un frame)
- **Nessun DOM reflow**: per animazioni continue usa `requestAnimationFrame` + Canvas o CSS `transform`/`opacity`
- **Stato visivo chiaro**: ARM, MUTE, REC — colore + icona distintivi anche per daltonici
- **Stack UI**: HTML5 + Bootstrap 5 + Vanilla JS + Canvas API (2D)

---

### 3.5 QA e Sicurezza (ex QA-Engineer Agent)

#### Checklist Sicurezza (OWASP)

| Check | Pattern da cercare |
|-------|-------------------|
| Path Traversal | filename da URL senza `os.path.basename()` |
| Injection | `os.system()` con input utente → usare `subprocess.run(list)` |
| Race Condition | globali senza lock, `sf_id` non protetto |
| Error Handling | call FluidSynth/subprocess senza try/except |

#### Checklist Performance

- WebSocket payload < 1 KB per tick
- Nessun polling più frequente del necessario
- Thread che non terminano, socket non chiusi, lock contesi
- CPU budget: Flask ~10%, FluidSynth ~60%, DAW threads ~20%, OS ~10%

#### Quando fare review:
- Dopo ogni modifica che tocca input utente (URL params, form, WebSocket)
- Dopo ogni modifica a thread/lock/globali condivisi
- Dopo ogni modifica a chiamate FluidSynth/subprocess
