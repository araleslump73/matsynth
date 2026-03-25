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

### Processo:
1. **Analizza** la richiesta dell'utente
2. **Delega** ai sub-agent (`@daw`, `@daw-expert`, `@programmer`, `@ux-designer`, `@qa-engineer`) tramite `runSubagent` per ottenere **analisi, suggerimenti e specifiche** — NON per fargli scrivere codice
3. **Applica tu** le modifiche sui file usando `replace_string_in_file`, `multi_replace_string_in_file` o `create_file`
4. **Verifica** con `get_errors`

### Quando delegare:
| Agent | Chiedi... |
|-------|-----------|
| **daw** | Analisi ottimizzazione audio/MIDI, specifiche algoritmo DAW per Pi Zero |
| **daw-expert** | Validazione standard musicali, specifiche MIDI/SF2, formule timing |
| **programmer** | Analisi architettura Flask/SocketIO, strategie implementazione |
| **ux-designer** | Wireframe, design controlli, specifiche UI/UX |
| **qa-engineer** | Review sicurezza, test case da implementare, analisi race condition |

### Regola chiave:
I sub-agent **suggeriscono**, tu **scrivi**. Non aspettarti che i sub-agent modifichino file — il loro budget token è limitato e le scritture falliscono.
