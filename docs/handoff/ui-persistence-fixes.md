# Correzioni della revisione `ui-persistence`

Riferimento: `docs/handoff/ui-persistence-review.md`. Commit delle correzioni: `f00c044` (`fix(persistence): review findings H1, M3, L1-L8`).
M1, M2, L2, L3 erano gia' corretti nella fase `ui-studio` (`docs/handoff/ui-studio.md`).

## Difetti corretti

| # | Correzione | Test (falliscono senza il fix) |
| --- | --- | --- |
| H1 | `_session_op_lock` a livello di modulo in `app.py`, preso con `acquire(blocking=False)` dal decoratore `_session_op` su Salva, Salva come, Apri, Nuovo, Import MIDI (`load`/`upload`, sostitutivo e Aggiungi), richiamo scena, PATCH/DELETE/upload progetto. Se occupato: `409 OPERATION_IN_PROGRESS` subito, nessuna attesa. Non e' il lock DAW; la callback MIDI non lo prende mai. | `SessionOpLockTests`: la Salva resta sospesa dentro `daw.snapshot_session` su un `threading.Event`; le altre 8 operazioni rispondono 409, il file di A contiene A, `project_state` e revisione invariati; poi il lock e' libero. Verificato che con un lock finto (sempre acquisito) il test fallisce. Il lock si rilascia anche dopo errori. |
| M3 | `convert` assente = conversione automatica con warning `IMPORT_CONVERTED`; solo `convert=false` esplicito da' 422 (D26). | `test_legacy_client_without_convert_gets_automatic_conversion_m3` |
| L1 | Rinomina legacy di una scena v1: nome `strip()[:64]`, vuoto -> `400 BAD_REQUEST`, `validate_scene` prima di scrivere. | `test_legacy_rename_of_v1_scene_refuses_empty_name_l1` |
| L4 | Chiave di radice facoltativa `event_clip_ids: true`, sempre scritta dal writer: il documento e' D3 e la migrazione per intervalli non si applica. Non bool -> `VALIDATION_FAILED` (D27). | `test_d3_document_without_owned_events_does_not_adopt_free_events_l4` |
| L5 | Import del bank interno con `smf_to_fluid_bank(bank, t)` (ID traccia), come l'export. | `test_internal_drum_bank_round_trip_uses_track_id_l5` |
| L6 | `_run_soundfont_load` avvolge il caricamento: qualunque eccezione chiude lo stato come `failed`. | `test_unexpected_exception_does_not_leave_loading_forever_l6` |
| L7 | Delete legacy di un preset: `library_remove` + scena corrente azzerata (`_scene_removed`, condivisa con la DELETE nuova). `_library_lock` attorno a load/modify/save di `library.json` (`_update_library`). | `test_legacy_delete_updates_library_and_current_scene_l7`, `test_concurrent_library_patches_are_not_lost_l7` |
| L8 | BPM del primo tempo fuori da 30..300: issue `{'type':'tempo_range','value':bpm}`, quindi `IMPORT_NEEDS_CONVERSION` / `IMPORT_CONVERTED`. | `test_bpm_outside_range_is_a_conversion_not_silent_l8` |
| Risorse Pi | `_write_project_doc` riempie `_project_meta_cache` con lo stat e i metadati del documento appena validato. `_project_name_taken` usa `_project_name_of`: cache se fresca, altrimenti legge il JSON senza validazione completa. | `test_listing_and_name_check_do_not_revalidate_after_writes` |

Frontend: `OPERATION_IN_PROGRESS` ha il testo in `part_panel.js` (`ERROR_TEXT`); il richiamo scena (`library_scenes.js`, usato da Suona e Libreria) mostra un toast `warn`, senza dialogo e senza riprova (test in `tests/js/live.test.js`). `api.js` e' generico e passa il codice. `studio.js` non chiama route di sessione; per gli altri codici mostra il `message` del server (gia' in italiano). La vecchia UI mostra "Load error: <messaggio>".

## Contratto (`docs/contracts.md`)

- 2.3: nuovo codice `OPERATION_IN_PROGRESS` (409); `IMPORT_NEEDS_CONVERSION` solo con `convert: false` esplicito.
- 2.4: `IMPORT_CONVERTED` anche con `convert` assente.
- 5.1: chiave facoltativa `event_clip_ids`; 6.4: la migrazione vale solo per i documenti pre-D3.
- 5.6: regola "una operazione di sessione alla volta".
- 7.2 e 8.1: `convert` assente, `tempo_range`.

## Decisioni da confermare con l'utente

- **D26** (M3): `convert` assente = conversione automatica con warning. I client nuovi devono inviare `convert`.
- **D27** (L4): chiave `event_clip_ids: true` senza incrementare `schema_version`. E' un'eccezione a 5.1/D18. Un file scritto ora non si apre con il codice precedente: nessuna versione rilasciata lo precede.

## Test

- Python: `Ran 243 tests ... OK`. JS: `node --test tests/js` 109/109.
- Instabilita' gia' presente, solo su Windows: `MidiRouteTests.test_m1_import_keeps_router_parts_and_channels_coherent` fallisce circa 1 volta su 10. Causa: `[State] Flush error: [WinError 5] Access is denied` in `_write_state_atomic` (`os.replace` su `last_state.json` mentre e' aperto). Test e codice non sono toccati da queste correzioni. Sul Pi (POSIX) `os.replace` non ha questo limite.

## Cosa resta

- Nessun deploy e nessun push.
- Il richiamo scena dalla route legacy `/api/presets/apply` e `POST /api/soundfont/load` non prendono il lock: la review non li elencava.
- `_scene_entries` resta senza cache: e' un problema solo con centinaia di scene.
- Le misure sul Pi (tempi dell'elenco progetti, RAM dell'import) non sono ancora state fatte.
- L'esempio `docs/schemas/project_v1.json` resta senza `event_clip_ids`: e' un documento pre-D3 valido e i suoi test usano la migrazione.
