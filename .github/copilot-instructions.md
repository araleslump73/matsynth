# MatSynth — Copilot Global Instructions

Leggi sempre il file **CONTEXT.md** nella root del repository prima di rispondere a qualsiasi domanda o implementare qualsiasi modifica.

CONTEXT.md contiene:
- Architettura completa del sistema (Flask + FluidSynth + MIDI + WebSocket)
- Vincoli hardware CRITICI (Raspberry Pi Zero 2W: 512 MB RAM, CPU 1 GHz ARM quad-core)
- Tutte le API REST e SocketIO documentate
- Known issues e debiti tecnici prioritizzati
- Regole per sviluppatori (ottimizzazioni backend, vincoli librerie Python, libertà framework frontend)
- Ruoli del team e aree di competenza

## Regole Globali per Tutti gli Agent

1. **Backend = Pi Zero 2W** — ogni modifica Python/Flask deve essere leggera. Nessun sleep nel thread principale, nessuna libreria C non disponibile su Pi OS.
2. **Frontend = browser del client (tablet/PC)** — nessun vincolo di performance lato UI. Framework JS (React, Vue, Svelte) sono accettabili.
3. **Dopo ogni modifica architetturale** aggiorna CONTEXT.md (sezione pertinente + Known Issues se necessario).
4. **Sicurezza**: controlla sempre path traversal su filename ricevuti da URL. Sanitizza con `os.path.basename()`.
5. **Lingua**: commenti nel codice in italiano, log in italiano, risposte in italiano.
