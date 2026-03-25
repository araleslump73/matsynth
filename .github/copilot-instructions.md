# MatSynth — Copilot Global Instructions

Leggi il file **CONTEXT.md** (root del repository) **solo quando necessario**:
- Prima di **implementare feature** o **modificare architettura** (API, threading, FluidSynth, MIDI)
- Quando serve conoscere **vincoli hardware Pi Zero 2W**, **API esistenti** o **known issues**
- Quando un altro agent ti chiede contesto sul sistema

**NON leggere** CONTEXT.md per: domande semplici, fix CSS/HTML isolati, risposte conversazionali, operazioni su file che già conosci dal contesto della chat.

## Regole Globali per Tutti gli Agent

1. **Backend = Pi Zero 2W** — ogni modifica Python/Flask deve essere leggera. Nessun sleep nel thread principale, nessuna libreria C non disponibile su Pi OS.
2. **Frontend = browser del client (tablet/PC)** — nessun vincolo di performance lato UI. Framework JS (React, Vue, Svelte) sono accettabili.
3. **Dopo ogni modifica architetturale** aggiorna CONTEXT.md (sezione pertinente + Known Issues se necessario).
4. **Sicurezza**: controlla sempre path traversal su filename ricevuti da URL. Sanitizza con `os.path.basename()`.
5. **Lingua**: commenti nel codice in inglese, log in inglese, risposte in italiano.
6. **SCRITTURA OBBLIGATORIA SUI FILE** — Quando devi implementare codice, **DEVI** usare `replace_string_in_file`, `multi_replace_string_in_file` o `create_file` per scrivere direttamente sui file di progetto. **MAI** proporre snippet di codice in chat senza applicarli. Se ti viene chiesto di implementare, il tuo output deve essere file modificati, non blocchi di codice da copiare. Leggi il file con `read_file` prima di modificarlo.

## Workflow di Delega agli Agent

L'utente parla **sempre con Copilot (te)**. Tu sei l'unico responsabile di scrivere sui file.

**Tutti i sub-agent sono read-only** — analizzano, suggeriscono, restituiscono testo strutturato. Non possono scrivere sui file, eseguire comandi in terminale, né invocare altri agent. Il loro budget token è limitato: passagli contesto sintetico nel prompt, non fargli leggere file enormi.

### Processo Standard (feature, cambiamenti funzionali):
1. **Raccogli** la richiesta dell'utente
2. **Delega al Business Analyst** (`@business-analyst`) per raccogliere/chiarire requisiti
3. Se il BA restituisce **domande**: presentale all'utente, raccogli le risposte, reinvoca il BA
4. Quando il BA produce **specifiche funzionali complete**: passale al **Tech Lead** (`@tech-lead`)
5. Il Tech Lead analizza l'impatto e restituisce un piano tecnico
6. Se servono pareri specialistici: invoca l'esperto pertinente tu stesso (non il Tech Lead)
7. **Applica tu** le modifiche sui file usando `replace_string_in_file`, `multi_replace_string_in_file` o `create_file`
8. **Verifica** con `get_errors`

### Processo Rapido (bug fix, fix minori, operazioni note):
1. **Analizza** la richiesta — se è un bug chiaro o un fix puntuale, salta BA e Tech Lead
2. **Delega** direttamente all'esperto pertinente per analisi
3. **Applica tu** le modifiche e verifica

### Quando delegare:
| Agent | Chiedi... |
|-------|-----------|
| **business-analyst** | Raccolta requisiti, chiarimento ambiguità, specifiche funzionali, criteri di accettazione |
| **tech-lead** | Piano tecnico, impatto architetturale, sequenza implementazione |
| **daw** | Analisi ottimizzazione audio/MIDI, specifiche algoritmo DAW per Pi Zero |
| **daw-expert** | Validazione standard musicali, specifiche MIDI/SF2, formule timing |
| **programmer** | Analisi architettura Flask/SocketIO, strategie implementazione |
| **ux-designer** | Wireframe, design controlli, specifiche UI/UX |
| **qa-engineer** | Review sicurezza, test case da implementare, analisi race condition |

### Regole chiave:
- I sub-agent **analizzano e suggeriscono**, tu **scrivi** sui file
- **Non aspettarti** che i sub-agent modifichino file — hanno solo tool read-only
- Passa **contesto sintetico** nel prompt, non chiedere di leggere CONTEXT.md intero
- Se un sub-agent non risponde, **implementa direttamente** — non reinvocarlo
