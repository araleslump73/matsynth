# Handoff `ui-editor`

Commit:
- `40da6e0`: backend;
- `7455f41`: modello puro e test JS;
- `f4ddb30`: UI.

Deviazioni dal contratto: D28-D34 in `docs/contracts.md` (sez. 13), route nuove in 8.2 ("Editor note/eventi").

## Implementato

### Backend (`daw_recorder.py`, `app.py`)

- `GET /api/daw/track/<t>/notes` (`get_track_notes`): note di una sola traccia, accoppiate FIFO, nel formato `[note, vel, start, end, on_idx, off_idx, clip_id]`. Restituisce anche `clips`, `max_beat` e la revisione, in una copia sotto lock.
- `copy`/`cut` con `pairs` (`copy_notes`): copia o taglia le note scelte per indice.
  - Gli appunti sono di tipo `range`, con origine nel primo note-on; si incollano con `paste_at`.
  - Taglia = un solo undo; i clip restano.
- `PUT/DELETE /api/daw/track/<t>/events/batch` (`edit_events_batch`, `delete_events_batch`): modifica ed eliminazione atomica di CC, program, pitch bend e pressure per indice.
  - Le note si modificano a coppie con `notes/batch`.
  - Se si elimina un evento di nota, il suo partner FIFO dello stesso clip viene eliminato con lui.
- `notes/batch`:
  - allarga la cornice del clip quando le note possedute escono dai bordi (D28);
  - rifiuta le modifiche che cambierebbero l'accoppiamento FIFO (D29).
- `add_note`/`paste`: rispettano `MAX_EVENTS_TOTAL` (D30).
- Tutte le operazioni:
  - rispondono `409 RECORDING_ACTIVE` durante il Record;
  - usano `expected_revision`;
  - fanno un undo e incrementano la revisione di 1 per ogni chiamata.
- Test: `tests/test_editor_daw.py`, `tests/test_editor_app.py`.

### Modello (`static/js/editor_model.js`, UMD)

Logica pura, testata in `tests/js/editor.test.js`:
- snap e coordinate: beat/px, nota/y, `noteRect`;
- hit-test con maniglia di ridimensionamento e target minimo touch;
- note visibili con ricerca binaria (`visibleIndices`);
- selezione a rettangolo, toggle, riselezione per contenuto (`reselect`);
- costruzione delle modifiche: sposta, ridimensiona, velocity, trasponi, quantizza (forza/swing), lunghezza disegno;
- richieste API;
- filtro, descrizione e modifica degli eventi.

### UI

File: `templates/partials/editor.html`, `static/css/editor.css`, `static/js/midi_editor.js`. Vista `#view-editor` della shell, `MS.editor.open({track, clipId, startBeat})`.

- Apertura:
  - da Studio, con "Editor note" nella barra della vista oppure "Modifica note" nel riquadro Clip;
  - si apre su traccia e clip selezionati;
  - "← Studio" torna indietro.
- Piano roll su canvas (righello, tasti, griglia, velocity lane):
  - disegna solo le note visibili;
  - un solo `requestAnimationFrame` per frame;
  - scroll nativo su `#ed-scroll` con uno spacer;
  - con 3000 note, circa 16 ms per frame sul PC.
- Strumenti espliciti: Seleziona, Disegna, Cancella, Mano (pan) e Multi.
  - Zoom e scroll non creano mai note.
  - Lo scorrimento a due dita annulla il gesto in corso.
  - Ctrl+rotella = zoom.
- Controlli e operazioni:
  - snap, lunghezza e velocity della nuova nota, zoom X/Y, "Adatta", anteprima audio (`preview_note`);
  - selezione: tap, rettangolo, tutto o niente;
  - spostamento e ridimensionamento con drag;
  - velocity dalla lane o dal campo apposito;
  - copia, taglia e incolla, al cursore del righello o al playhead;
  - trasponi di ±1 o ±12;
  - quantizza (griglia/forza/swing) sulla selezione, o su tutte le note se non c'è selezione;
  - elimina;
  - undo e redo, anche con Ctrl+Z / Ctrl+Y.
- Ogni gesto produce una richiesta con `expected_revision`: un livello di undo, revisione +1.
  - In caso di `409`, la selezione viene azzerata e le note rilette.
  - In caso di `422`, compare un messaggio dedicato per `pairing` e `max_events`; negli altri casi (es. `max_beat`) si mostra il messaggio del server.
- Scheda Eventi:
  - lista virtualizzata con righe da 44 px;
  - filtro per tipo e opzione "solo clip";
  - selezione multipla, sposta di Δ beat, imposta un valore, elimina, tutto in batch.
- Durante il Record l'editor è in sola lettura (D33).
- Rilettura con debounce di 120 ms al cambio della revisione, solo se l'editor è visibile; nessun polling.
- Preferenze salvate: traccia, zoom, snap, lunghezza, anteprima.

## Verificato

- Test:
  - Python: 257 OK;
  - JS (`node --test tests/js`): 123/123.
- Playwright (server locale con porte MIDI finte, Chromium): 52/52.
  - Tutte le operazioni sopra, con mouse e con eventi pointer touch.
  - Clip con 3000 note.
  - Undo e redo, con la revisione che sale di 1 per gesto.
  - L'ID clip viene conservato: note-on e note-off restano nello stesso clip.
  - Sola lettura durante il Record, simulata lato client.
- Layout a 390x844, 768x1024 e 1024x640: nessun overflow del documento e pulsanti di almeno 44 px. Le barre scorrono in orizzontale sugli schermi stretti, come in Studio.
- Nessun errore JS. Gli unici HTTP ≥400 sono i `503` di `preview_note`, attesi senza synth.
- Studio, Suona, Vista classica, `/presets` e `/settings` funzionano ancora.

## Resta

- La Vista classica (`legacy_daw.js`, piano roll ed editor eventi vecchi) non è stata rimossa. La decisione spetta a `ui-acceptance` (D34).
- Le note appena incollate non vengono selezionate in automatico, perché `paste_at` non restituisce gli indici.
- Durante il Play l'editor non segue la playhead in modo continuo: la playhead si vede, ma non c'è auto-scroll.
- Non c'è l'inserimento di nuovi eventi CC/PC dalla lista: si modificano e si eliminano solo quelli esistenti.
- Da confermare con l'utente: D28 (allargamento automatico della cornice) e D32 (quantizza senza selezione = tutte le note).

## Da verificare sul Pi

- Fluidità del canvas con migliaia di note su Chromium del Pi o del tablet: scroll, zoom e drag.
- Editing touch reale su tablet: precisione delle maniglie, rettangolo di selezione, pan a due dita.
- Anteprima audio delle note con FluidSynth reale: latenza, nessun blocco del callback MIDI.
- Record reale: editor in sola lettura, poi rilettura alla fine del take.
- Tempo di `GET track/<t>/notes` e di `notes/batch` su tracce da circa 500 coppie (fixture 16 x 500).
