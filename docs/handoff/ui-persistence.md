# Handoff `ui-persistence`

Backend: scene versionate, progetto nativo `.matsynth.json`, adattatori legacy, import/export MIDI coerenti con il routing, revisione/lock della DAW. Riferimento: `docs/contracts.md` 4, 5, 6, 7, 8.1 (fase persistence), 8.2 (scene/progetti/SoundFont), 11.2-11.4.

## Stato dei sotto-blocchi

| Blocco | Contenuto | Stato |
| --- | --- | --- |
| A | `project_store.py` + `tests/test_persistence_store.py` (26 test) | FATTO |
| B | `daw_recorder.py` + `tests/test_persistence_daw.py` (25 test), baseline SMF aggiornati | FATTO |
| C | `app.py` + `tests/test_persistence_app.py` (29 test, incl. M1/M2), baseline preset aggiornati | FATTO |
| D | Misure 16x500 su PC, handoff | FATTO |

## Implementato

**`home/matteo/matsynth_web/project_store.py` (nuovo, funzioni pure)**
- Validatore portato da `tests/contract_reference.py` (stessi codici e percorsi): `validate_part`, `validate_scene`, `validate_project`, `default_part`, `migrate_legacy_preset`, `owned_indices`, `ContractError`.
- D3: eventi `[beat, bytes]` o `[beat, bytes, clip_id]`. `clip_id` deve essere un clip della stessa traccia, altrimenti errore con path `tracks[i].events[j][2]`.
- `pair_notes`: regola unica FIFO per altezza, sui byte grezzi.
- `migrate_event_clips`: migrazione per intervalli, clip in ordine di ID crescente.
- `project_to_session`, `build_project_doc`: il writer scrive il `clip_id` solo se != 0 ed esistente, e normalizza il nibble di canale solo dove serve.
- `scene_to_legacy_preset`, `make_scene`, `classify_preset`, `defaulted_warnings`.
- `fluid_bank_to_smf` / `smf_to_fluid_bank` (GS, D5), `external_sound_to_smf` / `smf_to_external_sound`.
- `atomic_write_json`: tmp unico nella stessa cartella, JSON compatto scritto a blocchi con l'encoder C, fsync, `os.replace`, fsync della cartella su POSIX. Su errore: tmp rimosso, file precedente intatto, `StoreError` `DISK_FULL`/`DISK_ERROR`.
- `read_json_file`, `parse_json_bytes` con limite di dimensione.
- `library.json`: `load_library`, `library_set_meta`, `library_remove`, `sort_scene_entries`.
- Stati osservabili `SceneState`, `ProjectState` (contatore `gen` contro la perdita di dirty durante Salva), `SoundfontState`: notifica solo se cambia, sempre fuori dal lock.
- `parse_fonts_output`, `verify_font_loaded`.
- Costanti: `MAX_PROJECT_BYTES` 2 MiB, `MAX_EVENTS_TOTAL` 120000, `MAX_CLIP_ID` 65535, `MAX_SCENE_BYTES` 256 KiB.

**`daw_recorder.py`**
- Revisione: `revision`, inizializzata a `time.time()*1000`, +1 in `_push_undo`, `_restore_snapshot`, fine Record con eventi, import, `load_session`.
- Lock e notifiche:
  - `_edit_lock` (RLock) tenuto per controllo revisione + validazione + undo + mutazione + sort.
  - Contesto `_edit()`: all'uscita più esterna, FUORI dal lock, un solo `daw_state_update` e un solo `on_content_change` (hook dirty).
  - `last_error` e `last_revision` sono per-thread: le richieste Flask concorrenti non si sovrascrivono.
  - `check_revision(expected)` è l'API per ui-studio.
- `event_clips[t]` (`array('H')`) parallelo a `tracks[t]` in OGNI mutazione, anche in quelle di ui-studio (cambiate solo meccanicamente):
  - helper `_set_track`, `_sorted_pair` (un sort sugli indici, poi permutazione; liste nuove), `_drop_indices`, `_free_orphan_ids`, `_alloc_clip_id`, `_clip_at`;
  - la callback di Record appende alle due strutture sotto lo stesso lock (`_rec_lock` è ora alias di `_edit_lock`).
- `_pair_notes` è l'accoppiamento unico (A26), usato da `quantize_tracks`, `get_track_activity`, `_build_intervals_full`, `_build_notes_full`. `_get_note_pair_indices_in_range` usa `project_store.owned_indices`.
- Editing indicizzato:
  - `edit_event`, `delete_event`, `edit_note_pair`, `delete_note_pair`, `add_note` e `transpose_notes` accettano `expected_revision`.
  - La validazione viene PRIMA dell'undo (A21).
  - Errori: `RECORDING_ACTIVE`/`BAD_REQUEST` con l'HTTP 400 legacy, `REVISION_CONFLICT` 409.
  - `add_note` assegna l'ID del clip più recente che contiene il beat (6.3).
- `rename_track`/`set_track_color` creano un livello di undo, solo se il valore cambia (A14).
- `set_bpm`/`set_time_signature` emettono `daw_state_update` e rendono dirty (A20). Loop, mute e solo rendono dirty.
- `quantize_tracks` crea un livello di undo solo se qualcosa cambia davvero.
- Import (A1, A7, A13):
  - pre-passata su tutte le tracce SMF per tick assoluto;
  - PC iniziale con bank `(MSB<<7)|LSB`; gli altri PC/CC0/CC32 restano eventi al loro beat;
  - tempo map o metro non supportato -> `DawError IMPORT_NEEDS_CONVERSION` (con `convert=True`: primo tempo, 4/4, warning `IMPORT_CONVERTED`); SysEx/meta non gestiti contati in `IMPORT_DROPPED_EVENTS`;
  - un clip per traccia importata `[primo, ultimo+0.125)`, che possiede gli eventi importati;
  - nomi traccia da `track_name` (Sostituisci);
  - UN livello di undo, che include BPM e metro;
  - `DawError` non è `RuntimeError`: "trasporto attivo" resta `RuntimeError`, come oggi.
- Export: nomi traccia DAW; `bank: None` / `program: None` = messaggio non scritto.
- Fine Record: ID del take sulla coda non ancora ordinata, cornice `[primo, ultimo+0.125)`, poi sort permutato. Le strutture del clip di take sono di ui-studio; qui è anticipato il minimo necessario all'invariante D3.
- Sessione:
  - `snapshot_session` (copie superficiali sotto lock);
  - `load_session` (scambio atomico, trasporto fermo, undo/redo vuoti, revisione +1, posizione 0, arm off);
  - `empty_session`;
  - `check_invariants` (diagnostica).
- `merge_clips` rimappa gli ID dei clip uniti sul nuovo; `delete_clip` e `clear_range` liberano gli ID pendenti.
- Cache: un flag `_density_dirty` unico faceva restare vecchia una delle due mappe (note map o densità). Ora "sporco" svuota entrambe.
- Note map, densità ed eventi decodificati sono calcolati su uno snapshot FUORI dal lock, per non bloccare la callback di Record sul Pi.

**`app.py`**
- Globali `PROJECTS_DIR`, `LIBRARY_FILE`; stati `scene_state` (persistito debounced in `last_state.json` chiave `scene`), `project_state`, `sf_state`; `daw.on_content_change = project_state.mark_dirty`.
- `_sound_changed()` (scena e progetto dirty) viene chiamato in modo sincrono da:
  - `notify_parts_changed` (che copre `/select_prog`, `/cc`, `cc_update`, `PATCH /api/parts`);
  - `/set_effect`, `effect_update`;
  - caricamento SoundFont;
  - `/api/presets/apply`.
- Route legacy:
  - `list` include le v1 (`font = soundfont.file`);
  - `load` di una v1 restituisce la forma legacy;
  - `rename`: 409 `NAME_EXISTS` se il file di destinazione esiste (A10b); su una v1 cambia solo `name`;
  - `apply` invia anche `cc ch 10 pan` e persiste `parts` + `channels` + globali in una scrittura, aggiornando il router con `send='none'` (A2);
  - `capture_current_config` aggiunge `pan`;
  - `/load_sf2` aggiorna lo stato SoundFont.
- Route nuove:
  - `/api/soundfonts`, `/api/soundfont/load` (202 + thread, verifica con `fonts`), `/api/soundfont/status`;
  - `/api/scenes*` (lista/current/get/POST/PUT/PATCH/DELETE/preflight/recall);
  - `/api/projects*` (lista/current/new/open/save/save_as/PATCH/DELETE/download/upload).
- Richiamo scena:
  - preflight nell'ordine 4.5: scena valida -> trasporto -> SF in caricamento -> dirty -> SoundFont -> porte;
  - con `allow_partial` le parti indisponibili sono applicate con `send='none'` (configurazione conservata, nulla inviato, nessun fallback);
  - le parti passano da `router.apply_part(send='all')`;
  - parti, globali e scena sono persistiti in UNA scrittura;
  - un fallimento a metà dà `503 PARTIAL_APPLY` con `committed: true` e scena dirty.
- Apri progetto:
  - lettura e validazione complete FUORI dal lock, prima di toccare la sessione;
  - poi preflight del suono, poi `daw.load_session`, poi applicazione del suono;
  - scena = null; progetto pulito (dirty se l'applicazione è parziale).
- Salva:
  - `NO_CURRENT_FILE`; `FILE_CHANGED` se lo stat non coincide (salvo `overwrite`);
  - il documento è validato PRIMA di scrivere, quindi un file scritto riapre sempre;
  - `dirty=false` solo se nessuna mutazione è arrivata durante il salvataggio.
- MIDI:
  - `daw_midi_load` e upload passano da `_midi_import`, con parametri `convert` e `discard_changes` (dirty controllato solo se il parametro è presente, D13);
  - Sostituisci stacca il progetto dal file (D12);
  - suono iniziale per parte: interna = `select` con bank GS; esterna = MSB/LSB/PC alla porta, nulla a FluidSynth (A22);
  - `daw_midi_save` converte i bank da parti del router (7.3), con warning `BANK_NOT_REPRESENTABLE`.
- Revisione nelle risposte DAW di 8.1 (persistence): state, bpm, undo/redo, rename/color, clips, full_density_map/full_note_map (fuori da `map`), quantize, transpose, events GET/PUT/DELETE, add_note, edit/delete_note_pair, record/stop, ack socket di undo/redo/record_stop. Busta 2.1 con codici e override legacy di 8.1.
- `/api/system/status.soundfont` ora riporta lo stato reale, non più il segnaposto. `/get_state` aggiunge `scene`.
- M1: router + `parts` + `channels` coerenti dopo l'import (vedi Deviazioni).
- M2: Record rifiutato usa la tabella HTTP (503 `PORT_UNAVAILABLE`, 409 `TRANSPORT_ACTIVE`).

**Test e helper**
- `tests/helpers.py`: `redirect_app_paths` redirige anche `PROJECTS_DIR`, `LIBRARY_FILE`, `SF2_DIR` e azzera gli stati scena/progetto/SoundFont e la cache dei metadati progetto.
- `tests/test_contract_examples.py`: importa `project_store` (tutti i casi conservati, 11.4).
- `tests/contract_reference.py` diventa un alias che re-esporta `project_store`.
- `tests/test_baseline_smoke.py`, secondo 11.4:
  - i due DESIRED import e il DESIRED pan diventano test normali;
  - `test_import_bank_lsb_is_dropped...` e `test_apply_pan_is_ignored_today` sono rimossi;
  - `test_import_does_not_create_clips` -> `test_import_creates_one_clip_per_imported_track`;
  - `..._not_channels_in_state_file` -> `test_apply_persists_globals_and_applied_channels_in_state_file`;
  - l'upload si aspetta `select 0 1 1 7`;
  - i test "invariati" non sono stati toccati.

## Verificato e come

    cd /c/Users/matteo/projects/matsynth && <venv>/python.exe -m unittest discover -s tests -p "test_*.py" -v
    Ran 180 tests -> OK (0 failures, 0 expected failures); 179 OK in 3 esecuzioni consecutive prima dell'ultimo test aggiunto
    node --test tests/js -> 63/63 ok (frontend non toccato)

Il totale include i test di `test_routing.py` dell'agente che corregge il router. Copertura dei requisiti:
- invariante parallela con ogni mutatore, record, undo/redo, import, open;
- revisione obsoleta -> 409 senza modifiche (DAW e route);
- concorrenza: due thread con la stessa revisione, uno solo passa;
- emit e hook mai sotto lock;
- A13, A14, A20, A21, A26;
- round-trip del progetto 16x500 (eventi, ID clip, clip, nomi, colori, mute/solo, BPM/metro/loop, `next_clip_id`, revisione cambiata, undo vuoto, arm off);
- evento libero dentro una cornice clip resta libero dopo il round-trip;
- file invalido, versione futura o corrotto -> 422 con sessione intatta;
- `os.replace` fallito -> 500 `DISK_ERROR`, file precedente intatto, nessun `.tmp`; `ENOSPC` -> `DISK_FULL`;
- richiamo scena: 409 trasporto, dirty, SoundFont mancante/da cambiare, porte; parziale con consenso; 503 `PARTIAL_APPLY`; esterni via porta;
- SoundFont `loaded` solo dopo `fonts`; fallimento senza toccare la chiave `font`;
- M1 (coerenza dopo import e dopo uno slider), M2 (HTTP 503/409), A22, export MSB1/LSB2/PC7;
- nessuna scrittura fuori dalla cartella temporanea (controllato: `C:\home` non esiste dopo la suite).

**Misura su PC, NON sul Pi** (Windows, Intel i5 3a gen, CPython 3.11 64 bit, `tracemalloc`; script in scratchpad `measure.py`), fixture `workload_16x500.mid` = 16000 eventi:

| Voce | Valore |
| --- | --- |
| File progetto | 354 982 byte (347 KiB), limite 2 MiB |
| `snapshot_session` (sotto lock) | 0,17 ms |
| Salvataggio completo (snapshot + doc + validazione + scrittura atomica) | 109 ms, di cui validazione 68 ms |
| Caricamento completo (lettura + validazione + sessione + scambio) | 95 ms |
| Scrittura: `json.dumps` C intero 33 ms, `json.dump` Python 242 ms | Scelto: C a blocchi da 256 elementi |
| Picco `tracemalloc` salvataggio / caricamento (allocazioni Python, NON RSS) | 1,5 MB / 4,5 MB |

Sul Pi Zero 2 W i tempi saranno verosimilmente 5-15 volte maggiori: da misurare.

## NON verificato / da verificare su hardware

- **D5, convenzione bank GS di FluidSynth.** Assunzione: `synth.midi-bank-select=gs` (default, non cambiato da `startfluid.sh`), CC0 = bank, CC32 ignorato, canale 10 = batteria. Ne derivano:
  - export: bank interno N<128 -> MSB N / LSB 0; 128 su canale 9 -> 0; altri >=128 -> solo PC + warning;
  - import su parte interna: MSB, oppure 128 su canale 9;
  - da verificare con FluidSynth reale: caricare l'SMF esportato e controllare con `channels` lo strumento selezionato.
- Tempi reali di caricamento SF2 e output reale di `fonts` sul Pi: la verifica cerca il percorso completo o il nome base del file.
- Tempi e picco RAM di Apri/Salva sul Pi; soglie `MAX_*` (D21).
- Invio di bank/PC alla FANTOM-08 nel richiamo e nell'import; Local Control.
- Comportamento in browser: nessun frontend toccato; la vecchia UI riceve buste additive compatibili.

## Decisioni e deviazioni

1. **Migrazione D3 per documento.** Gli ID per intervalli si assegnano solo se NESSUN evento del documento ha il terzo elemento, altrimenti il round-trip "adotterebbe" eventi liberi dentro una cornice. `CLIP_OVERLAP_PRESENT` viene emesso solo in quel caso. Caso limite: un progetto nuovo con clip ma nessun evento posseduto verrebbe migrato; il playback non cambia.
2. **ID clip <= 65535** (`array('H')`). Il validatore rifiuta ID e `next_clip_id` oltre il limite; l'allocazione oltre il limite solleva errore.
3. **Validazione completa del documento prima di ogni salvataggio**: più CPU (68 ms su PC), ma un file scritto riapre sempre. Se la sessione in memoria non è serializzabile (beat > 100000, > 120000 eventi), Salva risponde 422 e non scrive.
4. **M1.** L'import usa `router.apply_part(part, send='all', cc_keys=[])` e non `send='none'`, perché il contratto (7.2, 11.4: `select 0 1 1 7`) richiede di applicare il suono importato. Segue la persistenza di `parts` + `channels` in UNA scrittura (`_persist_sound`, stessa funzione `_put_part_in_state` di `save_part`). Le parti saltate nel richiamo parziale usano `send='none'`.
5. **Apri con `soundfont: "keep_current"`** e SoundFont diverso: warning `SOUNDFONT_UNSPECIFIED` con `details.required/loaded`, perché in 2.4 non esiste un codice dedicato.
6. **Undo dell'import ripristina anche BPM e metro** (snapshot con trasporto solo per l'import).
7. **Stop di Record**: la cornice del take passa a `ultimo + 0.125` e gli ID vengono assegnati prima del sort. Anticipa una parte di `_finalize_recording_clips` (ui-studio), necessaria all'invariante.
8. **Rinomina legacy di una v1** cambia solo `name`, file e ID restano. Rinomina legacy di un preset legacy: libreria e scena corrente seguono il nuovo stem.
9. **`/api/projects`, campo `valid`**: validazione completa, con cache per `(mtime_ns, size)`.
10. **Export SMF**: per il riconoscimento del canale batteria si usa il canale SMF (= ID traccia), non `destination.channel`.
11. **`quantize` risponde con le note effettivamente spostate**, non con tutte le coppie.
12. **`/load_sf2` durante un caricamento** risponde 409 con corpo testo (condizione nuova).
13. **`scene_state` viene scritto due volte**: nella scrittura unica del richiamo e poi dal writer debounced. Il valore è lo stesso.
14. **Verifica SoundFont non riuscita** (`fonts` non riporta il file): stato `failed`, chiave `font` invariata, ma `sf_id` segue comunque il primo font elencato, come la route legacy. Altrimenti i `select` successivi punterebbero a un ID scaricato. Test: `test_unverified_load_still_tracks_active_sf_id`.
15. **RSS non misurata** (solo `tracemalloc`): la misura RSS va fatta sul Pi con `tools/pi_baseline.sh`.

`docs/regression-checklist.md`: compilata la colonna Verifica delle righe 4, 5, 7-10, 12-15, 17, 25, 26, 28, 29, 31-35, 48-51, 53, 56. Modificate solo quelle celle: 26 righe del diff.

## Punti aperti per le fasi successive

- **ui-studio**, operazioni clip con semantica D3, oggi solo meccanica:
  - `move_clip`/`delete_clip`/`transpose_clip` usano ancora l'ownership per intervallo;
  - `split_clip` non riassegna gli ID della metà destra;
  - `paste`/`duplicate_track` producono eventi liberi.
- **ui-studio**: A19 (`_restore_snapshot` riporta indietro `next_clip_id`) non è toccato, come da ownership; `notes/batch` con `expected_revision` obbligatoria e 422 (A28); A16, A17, A18, A24/D6.
- **Router**: nessuna modifica fatta a `midi_router.py`. Richiesta facoltativa al suo owner: `midi_router.atomic_write_json` (registro dispositivi) duplica `project_store.atomic_write_json`, come gia' annotato nel suo docstring; si puo' consolidare. Da sapere: in `MidiRouter._deliver` la console interna considera fallito un `''` (il fake dei test di default restituisce `''`); per i test di successo serve un fake che risponda `'OK'`.
- **ui-live / ui-studio (frontend)**: inviare sempre `expected_revision`, `discard_changes`, `allow_partial`; gestire `PARTIAL_APPLY`, `SOUNDFONT_CHANGE_REQUIRED` (richiamo in due passi, D9) e gli eventi `scene_state`/`project_state`/`soundfont_status`.
- **Contratto**: valutare un warning dedicato per `keep_current` (deviazione 5).
