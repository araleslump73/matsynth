# Handoff `ui-studio` (parte BACKEND)

Stato: FATTO (backend). Riferimento: `docs/contracts.md` 6.3-6.8, 8.1-8.2, 11.2 (`ui-studio`), 11.3, D3/D4/D6.
Il frontend e l'handoff complessivo sono in `docs/handoff/ui-studio.md`.

## Stato dei sotto-blocchi

| Blocco | Contenuto | Stato |
| --- | --- | --- |
| 0 | Lettura contratto/handoff, suite di partenza: 180 test OK | FATTO |
| A | DAW: copy/cut/paste/clear_range pair-aware, appunti range/clip | FATTO |
| B | DAW: operazioni clip per ID (move/resize/split/merge/delete/transpose/create/duplicate), duplicate_track, A19 | FATTO |
| C | DAW: edit/delete_notes_batch (422), D6 return_to_start | FATTO |
| R | Fix della review `ui-persistence` toccati da ui-studio (M1, M2, L2, L3) | FATTO |
| D | app.py: route con busta e codici | FATTO |
| E | Misura PC, handoff | FATTO |

## Cosa c'e'

**DAW (`daw_recorder.py`)**, tutte le mutazioni dentro `_edit()` (una revisione e
un livello di undo per gesto, emit/hook fuori dal lock, nessuna chiamata al
router sotto lock), `event_clips` sempre parallelo, note-off con lo stesso ID
del note-on (FIFO per altezza dentro ogni ID, `_owned_by_id`/`_group_partners`):

- `copy_selection` (intervallo o clip, sustain CC64=0 negli appunti, consentita
  in Play/Record, nessuna revisione), `cut`, `paste_at` (`EMPTY_CLIPBOARD`; appunti
  da clip -> clip nuovo " (copia)"), `clear_range` (A16), `clipboard_info()` in `get_state().clipboard`.
- Per ID: `move_clip`, `resize_clip` (D4: `CLIP_RESIZE_WOULD_EXCLUDE` con
  `details.min_end_beat`), `split_clip`, `merge_clips`, `delete_clip`,
  `transpose_clip`, `create_clip`, `duplicate_clip`; `duplicate_track`
  (variante legacy D13 e variante nuova con clip a nuovi ID, `DEST_NOT_EMPTY`).
- `edit_notes_batch`/`delete_notes_batch` tutto-o-niente (400 tipo/valore, 422
  coppie non FIFO o di clip diversi).
- D6: `start_recording(channel, return_to_start=True)`, `record_start_beat` nello stato.
- A19: `next_clip_id` dopo undo = max (mai riuso di ID).

**Fix della review** (`docs/handoff/ui-persistence-review.md`):

- M1: `import_midi` rifiuta prima di mutare un file oltre `MAX_BEAT`
  (`VALIDATION_FAILED`, `details.reason: "max_beat"`) o un import (anche
  Aggiungi) oltre `MAX_EVENTS_TOTAL` (`reason: "max_events"`). paste/move/batch
  avevano gia' i limiti.
- M2: `clear_track`/`clear_all_tracks` rifiutati in Record (`409 RECORDING_ACTIVE`,
  controllo dentro `_edit`); consentiti in Play. `clear_range` era gia' vietato in Play/Record.
- L3: `edit_event`, `delete_event`, `add_note`, `edit_note_pair`,
  `delete_note_pair` (e `transpose_notes`) ricontrollano il Record DENTRO il
  lock; `start_recording` imposta indici di partenza del take e `is_recording`
  sotto `_edit_lock`.
- L2: `import_midi` verifica gli ID clip disponibili prima di mutare;
  `_finalize_recording_clips` al limite `MAX_CLIP_ID` lascia il take libero (ID 0) senza eccezione.
- Non fatti (fuori dalle operazioni di ui-studio): H1 e gli altri punti della review restano come annotati li'.
- Un controllo "note-off con ID diverso dal note-on" in `check_invariants` e'
  stato provato e tolto: l'editor di eventi singoli legacy (`edit_event`) puo'
  legittimamente spezzare una coppia. Il bilanciamento per ID resta verificato
  dal test casuale `RandomizedIntegrityTests` dopo ogni operazione di montaggio.

**Route (`app.py`)**, busta 2.1, HTTP dalla tabella 2.3 salvo gli override 8.1
dichiarati dalla DAW (`last_error.http`); i dict di risultato al primo livello
(compatibilita' `legacy_daw.js`: `new_clip_id` e `dest_channel` interi):

| Route | Note |
| --- | --- |
| POST `/api/daw/track/<t>/clear`, `/clear_all` | + `revision`; 409 `RECORDING_ACTIVE` in Record |
| POST `/api/daw/track/<t>/clear_range` | `expected_revision?`; `{removed, clips_removed, revision}` |
| POST `/api/daw/copy` | intervallo o `clip_id`; `{copied, clipboard, revision}` (revisione invariata) |
| POST `/api/daw/cut` (nuova) | `{copied, removed, clips_removed, clipboard, revision}` |
| POST `/api/daw/paste` | `{pasted, new_clip_id, revision}`; 409 `EMPTY_CLIPBOARD` |
| POST `/api/daw/clip/move`, `/resize`, `/split`, `/merge`, `/delete`, `/transpose` | `expected_revision?`; 404 `NOT_FOUND`, 409 `TRANSPORT_ACTIVE`, split fuori clip = 404 `BAD_REQUEST` (override) |
| POST `/api/daw/clip/create`, `/clip/duplicate` (nuove) | 201 |
| POST `/api/daw/track/<t>/duplicate` | `include_config`/`confirm_overwrite` booleani; legacy se entrambi assenti; trasporto attivo = 400 (override) |
| PUT/DELETE `/api/daw/track/<t>/notes/batch` (nuove) | `expected_revision` obbligatoria (400), 422 `VALIDATION_FAILED` |
| POST `/api/daw/record/start`, socket `record_start` | `return_to_start` booleano (default true) |

`include_config: true` copia sulla parte destinazione **destinazione, suono, CC
e monitor** della parte sorgente (via router, fuori dal lock DAW, persistito e
notificato con `parts_update`); l'**ingresso** della destinazione resta il suo
(due parti sullo stesso ingresso suonerebbero doppie). Per una destinazione
interna il canale FluidSynth resta quello della parte destinazione. Se il router
rifiuta la parte: eventi duplicati comunque, warning `PART_UNAVAILABLE`,
`config_copied: false`.

## Test

- `tests/test_studio_daw.py` (45 test): appunti, incolla, operazioni clip, duplica,
  contatore ID, batch, D6, fix review, sequenze casuali con undo/redo e validazione del documento.
- `tests/test_studio_app.py` (7 test): busta, codici/override, revisione e conflitto, compatibilita' legacy, batch, `return_to_start`, Svuota in Record.
- Suite completa: `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` -> 232 test OK.

## Misura PC (16 tracce x 500 coppie, clip unico per traccia)

copia intervallo 100 beat 1,2 ms; taglia+undo 1,9 ms; copia+incolla+undo 2,8 ms;
sposta clip+undo 1,5 ms; dividi+undo 1,7 ms; duplica clip+undo 2,1 ms;
`get_state` 0,07 ms. Sul Pi Zero 2 W attendersi circa 10-20 volte tanto
(da misurare: punto in `docs/handoff/ui-studio.md`).
