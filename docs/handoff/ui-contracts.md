# Handoff `ui-contracts`

## Implementato

Nessuna modifica al codice applicativo (`home/matteo/`). File nuovi:

- `docs/contracts.md`: contratto normativo (13 sezioni): convenzioni e unita' (beat, snap centralizzato), busta risposte/errori superset compatibile con `index.html` + tabella codici/HTTP + warning + ack/timeout, schema parte e routing (alias, descrittori porta, stati, monitor legacy/managed, conflitti, regole di trasporto), scena v1 + mappa di migrazione legacy campo per campo + preflight/richiamo + stato SoundFont + dirty scena, progetto `.matsynth.json` (schema, validazione, scrittura atomica, cosa non serializzare, Nuovo/Apri/Salva/Salva come/Import, dirty, RAM), modello DAW (accoppiamento FIFO, ownership, invariante clip non sovrapposti, tabella operazioni, undo, revisione, lock, selezione client), import/export SMF (bank neutro, PC iniziale vs PC nel tempo, convenzione GS per FluidSynth), tabella route esistenti (cosa cambia, chi) e route nuove con payload, eventi Socket.IO esistenti/nuovi con frequenze, flag `midi_mode` e rollback, ordine fasi + ownership file + interfacce (`window.MatSynth`, API Python tra fasi), 29 anomalie (11 baseline + 18 nuove) assegnate, destino di ciascuno dei 40 test baseline, criteri di accettazione per fase, 21 deviazioni/punti da confermare.
- `docs/schemas/`: `part_internal.json`, `part_external.json`, `scene_v1.json`, `scene_from_legacy_decay.json` (migrazione attesa della fixture reale), `project_v1.json`, `midi_devices.json`, `api_examples.json` (27 richieste/risposte), `invalid_cases.json` (37 casi non validi con codice/percorso attesi + 6 casi accettati con warning).
- `tests/contract_reference.py`: validatore e migratore di riferimento (specifica eseguibile, nessuna dipendenza, nessun import di `app.py`).
- `tests/test_contract_examples.py`: 22 test (JSON validi e citati, casi invalidi, migrazione fixture legacy, buste API contro la tabella codici **letta dal testo** del contratto, compatibilita' del formato eventi con `daw.tracks` e `export_midi` reali, regola di ownership di riferimento == `daw_recorder._get_note_pair_indices_in_range` reale, clip d'esempio che possiedono anche l'ultimo evento, coerenza chiavi schema <-> testo).

## Verificato e come

    cd /c/Users/matteo/projects/matsynth && <venv>/python.exe -m unittest discover -s tests -p "test_*.py" -v
    Ran 62 tests in ~3.8s -> OK (expected failures=4)

40 test baseline invariati + 22 nuovi. Ramo del worktree: `feature/ui-redesign` (il brief indica `develop`); nessun commit. Il test di coerenza ha gia' intercettato un esempio non citato nel testo (corretto).

Misure RAM/JSON della fixture 16x500 (contratto 5.7): eseguite su PC Windows, CPython 64 bit, `tracemalloc`, con il DAW reale (`helpers.make_daw`): ~2.6 MB eventi, ~309 KB JSON compatto, +2.7 MB parse, ~128 KB per snapshot undo. **Non sono misure del Pi.**

Verificato leggendo il codice (non eseguendo): `mido.Message.bytes()` ritorna una lista (eseguito), `copy_selection` non pair-aware, `clear_range` elimina clip parzialmente sovrapposti, `duplicate_track` non copia clip, `_restore_snapshot` riporta `_next_clip_id`, `set_bpm`/`set_time_signature` senza emit, `import_midi(merge=True)` senza undo, accoppiamento diverso in `quantize_tracks`/`get_track_activity`, Record durante Play avvia un secondo playback, mute/solo applicati al giro successivo, rewind implicito lato client dopo Record (`recordStartPosition`), etichette snap ambigue.

## NON verificato / da verificare su hardware

- Forma reale dei nomi porta rtmidi/ALSA sul Pi (normalizzazione 3.4), FANTOM-08, Local Control/monitor, scollegamento con note/sustain.
- Convenzione bank GS (`synth.midi-bank-select=gs`, percussioni su canale 10) con FluidSynth reale (D5).
- Efficacia di CC120/CC123/CC64 su strumenti esterni (D15); `managed` al riavvio e rollback (10.3).
- Picchi RAM e limiti `MAX_PROJECT_BYTES`/`MAX_EVENTS_TOTAL` sul Pi (D21).
- Anomalie A23, A25 (evidenza statica, non riprodotte con test).

## Decisioni principali (dettaglio in `docs/contracts.md` sez. 13)

- Busta errori additiva: `ok` + `status` + `message` + `error{code,message,details}`; codici HTTP esistenti invariati sulle route esistenti.
- Revisione globale (init `time.time()*1000`) incrementata in `_push_undo`/`_restore_snapshot`/fine Record/import/apri/nuovo; indici validi solo con la revisione; nessun ID per nota (piu' leggero). `expected_revision` facoltativa per il client legacy, obbligatoria nelle route batch.
- Clip = marcatori d'intervallo, ownership per note-on, **invariante di non sovrapposizione** con fusione dell'overdub (D3); riduzione clip limitata agli eventi posseduti (D4); clip di take/import = `[primo, ultimo + 0.125)` cosi' il note-off/CC64=0 di chiusura sull'ultimo beat e' posseduto.
- Route esistenti: `error.code` sempre dalla tabella, ma HTTP legacy conservato solo negli override elencati in 8.1 (`api_common.err(..., http=)`); i client decidono su `error.code`.
- `MultiTrackDAW` senza router DEVE restare autonomo (router di default in memoria, niente disco): i test baseline che istanziano il DAW direttamente restano validi.
- Caricare un SoundFont rende `dirty` scena e progetto; nel richiamo in due passi il client riporta `discard_changes` nel secondo passo.
- Legacy -> v1 con default espliciti (pan 64 se assente: D1; canali non elencati = default: D2); la route legacy `apply` mantiene la sua semantica.
- Cambio SoundFont nel richiamo = due passi client (D9); stato SF verificato con `fonts`.
- Flag `midi_mode` in `last_state.json`, effettivo da `MATSYNTH_MIDI_MODE` esportato da `startfluid.sh`, default `legacy` (D10).
- Backend seriale `ui-routing -> ui-persistence -> ui-studio (-> ui-editor)`; `ui-shell`, `ui-live`, `ui-offline-settings` solo frontend; CSS per vista (D11).

## Punti aperti per le fasi successive

- L'utente ha DECISO D6 (ritorno dopo Record: si mantiene, come opzione); dovrebbe ancora confermare D1, D2, D3, D4, D7 prima di `ui-persistence`/`ui-studio`.
- `ui-routing`: registrare nell'handoff i nomi porta reali appena disponibili; estendere `tests/helpers.py` (porte finte multiple, router neutralizzabile in `import_app`).
- `ui-persistence`: decidere se portare `tests/contract_reference.py` in `project_store.py` (poi il test importa il modulo reale); aggiungere `PROJECTS_DIR`, `DEVICES_FILE`, `LIBRARY_FILE` a `helpers.redirect_app_paths`.
- Il metodo di misura del jitter MIDI reale resta indefinito (punto aperto della baseline).
