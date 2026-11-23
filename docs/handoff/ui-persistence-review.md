# Revisione `ui-persistence` (commit 69e8175)

Revisione in sola lettura. Le righe citate sono quelle dei file **del commit** (`git show 69e8175:<path>`), non del worktree.

Come sono state fatte le prove:
- il commit e' stato estratto con `git archive` in `scratchpad/rev_checks2/tree`; script `c1`..`c7` in `scratchpad/rev_checks2/`, eseguiti col Python del venv;
- la suite del commit, eseguita su quella copia, da' `Ran 180 tests ... OK`.

Tipo di prova indicato per ogni difetto:
- **[codice reale]**: route Flask (test client) o `MultiTrackDAW` reali, con le porte finte di `tests/helpers.py` e oggetti `mido` reali;
- **[interleaving forzato]**: un wrapper sospende `daw.snapshot_session` su un `threading.Event` finche' l'altra richiesta non e' finita. Il codice sotto test non e' stato modificato;
- **[analisi]**: nessuno script, solo lettura del codice.

## ALTA

### H1. Salva / Salva come in parallelo con Apri, Import sostitutivo o Nuovo: il file del progetto A viene sovrascritto con un altro contenuto
- **Dove:** `app.py:2738` (`project_state.snapshot()`) e `2747` -> `2638` (`daw.snapshot_session()`); poi `2751` (`project_state.set(pid, ..., if_gen=...)`). Stessa struttura in `save_as`, `2776-2785`.
  - Nessuna serializzazione tra Salva e gli altri cambi di sessione: `api_project_open` (2682), `api_project_new` (2668), `_midi_import` (2908).
- **Scenario [interleaving forzato, codice reale] `c1_save_race.py`:**
  - Caso 1. Il client 1 salva A (nota 60); nel frattempo il client 2 fa Import MIDI sostitutivo (nota 72). Entrambi rispondono 200.
    - Il file di A su disco contiene solo la nota 72: l'originale e' perso.
    - `project_state` torna `id = A`, `name = 'A'`: annulla il distacco D12 voluto dall'import.
  - Caso 2. Salva A in parallelo con Apri B. La Salva risponde 200.
    - Il file di A contiene B (nota 40).
    - `project_state` = A con `dirty: false`, mentre la sessione in memoria e' B.
    - Il prossimo Salva dell'utente scrive di nuovo B dentro A.
- **Perche' succede:** `pid` e `gen` vengono letti prima dello snapshot della sessione, e nessun controllo verifica che sessione e progetto corrente siano ancora quelli. `if_gen` non protegge: `load_session` non chiama l'hook di dirty, e l'import lascia solo `dirty: true`.
- Lo scenario e' realistico: il piano prevede smartphone e tablet collegati insieme; basta anche un doppio tocco su Salva + Apri.
- Anche due Apri concorrenti, o Apri insieme a un richiamo scena, mescolano parti e sessione: `_apply_sound` intrecciato, `project_state` scritto dall'ultimo.
- **Correzione minima:**
  - un lock di operazione a livello di modulo (`_session_op_lock`), preso con `acquire(blocking=False)` da Salva, Salva come, Apri, Nuovo, Import sostitutivo/aggiungi (load e upload), richiamo scena e PATCH/DELETE progetto;
  - se il lock e' occupato: `409` con un codice nuovo (proposta `OPERATION_IN_PROGRESS`, da aggiungere a 2.3), senza attesa;
  - il lock non e' quello della DAW e la callback MIDI non lo prende mai, quindi tenerlo durante l'I/O non viola 6.7;
  - in alternativa (meno robusta): un contatore `daw.session_gen`, incrementato da `load_session` e dall'import, letto insieme a `project_state.snapshot()`, con la Salva che rinuncia se cambia prima della scrittura.

## MEDIA

### M1. Sessioni accettate dalla DAW ma non salvabili (Salva -> 422): import senza limiti, incolla e sposta clip senza controllo del beat
- **Dove:**
  - `daw_recorder.py:2400-2514` (`import_midi`: nessun controllo di `MAX_BEAT` ne' di `MAX_EVENTS_TOTAL`);
  - `996-1016` (`paste_at`) con la route `app.py:1405-1415` (beat negativo/NaN/infinito accettato);
  - `1259-1276` (`move_clip`: solo il limite inferiore 0);
  - `1820-1821` e `1836-1837` (`edit_notes_batch`: `float()` senza controlli). Oggi nessuna route la chiama: difetto latente per `notes/batch` di ui-studio.
- **Scenario [codice reale] `c2_unsaveable.py`.** Ogni operazione risponde 200, poi Salva come risponde 422 `VALIDATION_FAILED`:
  - import SMF con nota a beat 100001;
  - import di 120002 eventi;
  - `/api/daw/paste` con `beat: -5`;
  - `edit_notes_batch` con `on_beat: NaN`;
  - `/api/daw/clip/move` a 200000.
- Viola l'invariante 5.3 ("tutto cio' che il writer puo' produrre dallo stato in memoria DEVE essere accettato dal loader"). L'utente scopre al Salva che il lavoro non si salva; il rimedio e' indovinare quale undo fare.
- Per l'import il contratto chiede un rifiuto o una conversione esplicita dei dati fuori ambito. Sul Pi, 120000 eventi passano dagli oggetti `mido` (centinaia di byte l'uno): il picco di RAM va misurato prima di accettarli.
- **Correzione minima:**
  - in `import_midi`, PRIMA dell'`_edit()`: se `max(beat) > MAX_BEAT` oppure `eventi esistenti (se merge) + nuovi > MAX_EVENTS_TOTAL`, sollevare `DawError('VALIDATION_FAILED', ..., {'reason': ...})` (422);
  - in `paste_at` e `move_clip`: usare `_beat_value` (0..MAX_BEAT, finito) su target e beat risultanti, e rifiutare con `False` / `BAD_REQUEST`;
  - in `edit_notes_batch`: usare `_beat_value`. Questa parte e' della fase ui-studio (A28), ma va segnalata.

### M2. Svuota traccia / Cancella intervallo / Svuota tutto durante il Record: ID del take sugli eventi sbagliati, take senza clip, note lasciate aperte, revisione non incrementata
- **Dove:**
  - `daw_recorder.py:842-890` (`clear_track`, `clear_range`, `clear_all_tracks`: nessun controllo di `is_recording`);
  - route `app.py:1143-1177`;
  - indice di partenza del take fissato in `daw_recorder.py:491` e usato a `571` (`_close_open_recorded_notes`, senza `min`) e a `617-628` (`_finalize_recording_clips`).
- **Scenario [codice reale, porte finte, callback reale del router] `c3_clear_during_record.py`:**
  - A, `clear_range` a meta' take. Solo 4 degli 8 eventi del take ricevono l'ID del clip, gli altri restano liberi. La cornice del clip (`[20.25, 20.46)`) non contiene le prime note del take.
  - B, `clear_track` a meta' take. Il take (3 eventi) resta senza clip e senza ID. La nota 61 resta aperta nei dati (nessun note-off di chiusura). La revisione NON cambia allo Stop, anche se il contenuto e' cambiato.
- Con l'ownership per ID di ui-studio, un difetto di ownership diventa un errore musicale: sposta/elimina clip agiscono sugli eventi sbagliati.
- **Correzione minima:**
  - nei tre metodi, dentro `_edit()`: `if self.is_recording: return self._refuse_recording()`, e nelle route `409 RECORDING_ACTIVE`/`TRANSPORT_ACTIVE`. Il contratto 6.4 vuole gia' il 409 per "cancella intervallo"; per "Svuota" va confermato;
  - in alternativa: dopo ogni rimozione, ricalcolare `self._record_start_event_index[ch]` sottraendo gli indici rimossi sotto l'indice di partenza.

### M3. La vecchia UI non riesce piu' a importare SMF con cambi di tempo o metro diverso da 3/4 o 4/4
- **Dove:** `daw_recorder.py:2440-2444`; `app.py:3124-3125` e `3175-3176`. `convert` assente viene trattato come `False`.
- **Scenario [codice reale] `c6_legacy_import_tempo.py`:** le stesse chiamate di `legacy_daw.js` (`dawLoadMidiFile`, `dawUploadMidi`, senza `convert`) su un SMF con due tempi, o con metro 6/8.
  - Risposta `422 IMPORT_NEEDS_CONVERSION`.
  - La UI mostra solo "Load error: ...", e da li' non c'e' modo di convertire.
  - Prima del commit quei file si importavano.
- E' conforme alla lettera di 7.2. Pero' per `discard_changes` la fase ha gia' applicato la tolleranza legacy (D13: parametro assente = client vecchio) e per `convert` no: i file MIDI reali hanno spesso piu' tempi.
- **Correzione minima:** come D13, `convert` assente = conversione automatica con warning `IMPORT_CONVERTED`; `convert=false` esplicito = 422. Da confermare con l'utente.

## BASSA

### L1. Rinomina legacy di una scena v1 con nome vuoto: file scritto senza validazione, scena resa invalida
- **Dove:** `app.py:699-711` (`preset_data['name'] = str(new_name)[:64]` e poi `atomic_write_json`, senza `validate_scene` e senza controllo `1..64`).
- **Scenario [codice reale] `c4_legacy_rename_v1.py`:**
  - `POST /api/presets/rename` con `new_name: ''` risponde 200;
  - poi `GET /api/scenes/<id>` -> 422, richiamo -> 422, elenco `valid: false`;
  - la PATCH della nuova API non la ripara, perche' `_read_scene` fallisce prima.
  - Un nome di 80 caratteri invece viene troncato e va bene.
- **Correzione minima:** `name = str(new_name).strip()[:64]`, 400 se vuoto; `ps.validate_scene(preset_data)` prima di scrivere.

### L2. Limite degli ID clip: `_alloc_clip_id` fallisce a meta' mutazione e lascia stati parziali
- **Dove:**
  - `daw_recorder.py:348-353`;
  - chiamate dopo modifiche gia' fatte: `merge_clips` 1427-1430 (clip gia' rimossi), `import_midi` 2493-2504 (tracce gia' svuotate), `_finalize_recording_clips` 626 (`_recording_channels` non azzerato);
  - il validatore ammette `next_clip_id = 65536` (`project_store.py:519`).
- **Scenario [codice reale] `c5_clip_id_limit.py`:** progetto valido con clip 65534/65535 e `next_clip_id` 65536.
  - `merge_clips` -> `INTERNAL`: clip spariti e `check_invariants` = "ID clip pendente".
  - `import_midi` -> `INTERNAL`: sessione svuotata (0 eventi; l'undo la recupera).
  - `stop_recording` -> eccezione: take senza clip, revisione invariata, `_recording_channels` resta `[1]`.
- E' un caso limite (65535 allocazioni), ma raggiungibile con un file valido.
- **Correzione minima:**
  - allocare gli ID PRIMA di `_push_undo` e di ogni modifica (pre-controllo `self._next_clip_id + n - 1 <= MAX_CLIP_ID`);
  - per lo stop del Record, se il limite e' raggiunto, lasciare il take libero, senza eccezione.

### L3. Controllo `is_recording` fuori dal lock nelle modifiche indicizzate (TOCTOU con l'avvio del Record)
- **Dove:**
  - `daw_recorder.py:1616`, `1661`, `1683`, `1720`, `1775`: il controllo avviene prima di `with self._edit()`;
  - `start_recording` `491-512`: calcola `_record_start_event_index` e imposta `is_recording` senza `_edit_lock`.
- **Scenario [analisi]:**
  - un `add_note` supera il controllo;
  - parte il Record e fissa l'indice N;
  - `add_note` inserisce e riordina;
  - allo Stop un evento pre-esistente (o la nota) finisce nella coda `[N:]` e riceve l'ID del take.
- La finestra e' stretta: stesso effetto di M2, in piccolo.
- **Correzione minima:** ripetere il controllo dentro `_edit()`; in `start_recording` calcolare l'indice e impostare `_rec_tracks` nello stesso `with self._edit_lock`.

### L4. Migrazione D3 per documento: un documento con clip ma senza eventi posseduti "adotta" gli eventi liberi alla riapertura
- **Dove:** `project_store.py:531-554` (`project_uses_clip_ids` e `migrate_event_clips`).
- **Scenario [analisi]:**
  - si cancellano tutte le note di un take (il clip resta);
  - si incollano eventi liberi nella sua cornice;
  - Salva: nessun evento ha il terzo elemento;
  - alla riapertura la migrazione assegna gli eventi incollati al clip.
- L'ownership cambia attraverso Salva/Apri, il playback no. E' gia' dichiarato nell'handoff (Decisione 1).
- **Correzione minima:** il writer deve distinguere "documento D3 senza eventi posseduti" da "documento pre-D3". Per esempio una chiave facoltativa di radice `"event_clip_ids": true`, sempre scritta, che disattiva la migrazione. Serve una decisione sul contratto (5.1 vieta chiavi extra).

### L5. Bank delle parti interne: export e import usano canali diversi per decidere "batteria"
- **Dove:** `app.py:3061` (export: `fluid_bank_to_smf(bank, ch)` con `ch` = ID traccia) e `2886` (import: `smf_to_fluid_bank(bank, destination.channel)`).
- **Scenario [analisi]:**
  - parte 9 interna, `destination.channel = 3`, bank 128;
  - l'export scrive bank 0, come batteria del canale 10;
  - il re-import sulla stessa parte da' `smf_to_fluid_bank(0, 3)` = 0, quindi strumento melodico.
  - Il round-trip cambia suono quando il canale e' rimappato (solo in `managed`).
- **Correzione minima:** usare lo stesso criterio nei due versi. La deviazione 10 ha scelto l'ID traccia per l'export: applicarlo anche all'import, oppure usare il canale di destinazione in entrambi.

### L6. Il thread di caricamento SoundFont non ha `try/finally`
- **Dove:** `app.py:2112-2141` e `2168`.
- **Scenario [analisi]:** se un'eccezione imprevista esce da `_run_soundfont_load`, `sf_state` resta `loading` per sempre. A quel punto ogni `POST /api/soundfont/load` e ogni richiamo scena rispondono `409 SOUNDFONT_LOADING` fino al riavvio. Oggi nessuna chiamata interna dovrebbe sollevare (`send_fluid`/`save_state` catturano tutto), quindi e' un caso di robustezza.
- **Correzione minima:** `try: ... except Exception as e: sf_state.finish('failed', filename, None, str(e))`.

### L7. Route legacy e libreria: aggiornamenti persi o stato non allineato
- **Dove e cosa:**
  - `app.py:667-680`: `DELETE /api/presets/delete/<file>` su una scena v1 non aggiorna `library.json` ne' `scene_state`. Se era la scena corrente, `scene_id` punta a un file inesistente.
  - `app.py:2405-2406`: PATCH `favorite`/`order` fa un read-modify-write di `library.json` senza lock. Due PATCH concorrenti: una delle due va persa. La scrittura resta atomica.
- **Correzione minima:**
  - nella delete legacy, se lo stem e' una scena: `library_remove` + `scene_state.set(None, None, True)`;
  - un `threading.Lock` attorno a load/modify/save della libreria.

### L8. Import: BPM fuori da 30..300 ridotto senza avviso
- **Dove:** `daw_recorder.py:2448` e `2500`.
- **Scenario [analisi]:** un SMF a 20 o 400 BPM viene importato a 30 o 300 senza warning. La regola "conversione esplicita" di 7.2 non lo copre.
- **Correzione minima:** aggiungere `{'type': 'tempo_range', 'value': bpm}` agli `issues`, cosi' passa da `IMPORT_NEEDS_CONVERSION` / `IMPORT_CONVERTED`.

## Risorse sul Pi Zero 2 W

- **Elenco progetti `_project_meta` (`app.py:2575-2591`): costo alto alla prima chiamata [analisi].**
  - Al primo elenco dopo l'avvio, e dopo ogni Salva (cambia lo stat), ogni progetto viene letto e validato per intero: fino a 2 MiB e ~68 ms per 16x500 su PC, stimati 0,5-1 s sul Pi.
  - `_project_name_taken` (2605) lo fa dentro Salva come, PATCH e upload. Con 20 progetti la prima Salva come dopo il boot puo' costare ~10 s sul Pi (stima, da misurare).
  - Proposta: dopo ogni scrittura riempire `_project_meta_cache` con lo stat e i metadati appena validati; per il solo controllo del nome leggere senza validare.
- **Scene `_scene_entries` (`app.py:2215-2237`): nessuna cache.**
  - Ogni Salva come o rinomina rilegge e rivalida tutti i file preset; la PATCH lo fa due volte (`_scene_name_taken` e `_scene_meta`).
  - I file sono piccoli: e' un problema solo con centinaia di scene.
- **Cosa va bene:**
  - snapshot sotto il lock (copie superficiali); note map e densita' calcolate fuori dal lock;
  - `get_state` sotto il lock solo all'uscita di `_edit`;
  - undo limitato a 10 livelli; nessuna crescita senza limite rilevante (`_project_meta_cache` conserva i percorsi cancellati, trascurabile).
- **Migrazione D3:** O(clip x eventi) per traccia, solo su documenti pre-D3. Va misurata su un caso con molti clip.

## Categorie senza difetti confermati

- **Atomicita' di scrittura** (`project_store.py:847-872`):
  - il file temporaneo ha un nome unico nella stessa cartella;
  - flush + fsync, `os.replace`, fsync della cartella su POSIX;
  - il temporaneo viene rimosso su qualunque eccezione e il file precedente resta intatto;
  - NaN viene rifiutato prima di toccare il file.
  - `last_state.json` usa ancora `_write_state_atomic`, con nome fisso sotto `STATE_LOCK`: e' preesistente.
- **Validazione prima dello scambio:** Apri legge, valida e costruisce la sessione fuori dal lock; versione futura, file corrotto o evento invalido danno 422 con la sessione intatta. La Salva valida prima di scrivere.
- **Path traversal:**
  - ID scena e progetto controllati con `ID_RE` (`[\w-]`, niente `.` `/` `\`);
  - file MIDI con `basename`; SoundFont come nome base presente nell'elenco;
  - nomi unicode usati solo come dato.
  - Nota minore: `ID_RE.match` con `$` accetta un `\n` finale (`fullmatch` o `\Z` sarebbero piu' rigorosi). Non e' sfruttabile: nessuna route crea ID da input libero.
- **`event_clips`** parallelo in tutte le mutazioni verificate (sort permutato, rimozioni, undo/redo, import, open, duplicate, merge/move/split), compresi gli snapshot di undo. L'unico disallineamento di ownership trovato viene dal Record (M2, L3).
- **Revisioni:** +1 una sola volta per gesto; mai sui tick; il 409 avviene sotto il lock prima di qualunque modifica o undo. Eccezione: M2-B.
- **Lock ed emit:**
  - emit e hook di dirty sempre fuori da `_edit_lock`;
  - nessuna chiamata al router tenendo `_edit_lock`;
  - la callback di input prende solo `_edit_lock` dopo il monitor;
  - nessuna inversione con `_cfg_lock` o `STATE_LOCK` trovata.
- **Richiamo scena:**
  - ordine del preflight come 4.5; Play/Record bloccano;
  - `allow_partial` applica le parti saltate con `send='none'`;
  - `PARTIAL_APPLY` con `committed`; SoundFont `loaded` solo dopo `fonts`;
  - `presets.html` riceve la forma legacy. Eccezioni: L1 e L7.
- **Import/export MIDI:**
  - bank MSB/LSB; PC a beat non zero conservato come evento;
  - un clip per traccia importata; un solo undo (BPM e metro inclusi);
  - nessun invio a FluidSynth per le parti esterne.
  - Eccezioni: M1, M3, L5, L8.

## Test (qualita')

- `test_concurrent_edits_with_same_expected_revision_a29` (`tests/test_persistence_daw.py:484-505`) non dimostra il lock.
  - `c7_test_without_lock.py` sostituisce `_edit_lock` con un lock che non esclude nessuno: il test passa 30 volte su 30.
  - Con il GIL i due thread si serializzano comunque.
  - Per provare l'esclusione serve sospendere un thread dentro la sezione critica, per esempio con un `on_content_change` o un `check_revision` che aspetta un `Event`, e verificare che l'altro resti bloccato.
- `check_invariants` (`daw_recorder.py:2600-2616`) non controlla che il note-off abbia lo stesso ID del suo note-on (6.3). `test_every_mutator...` non copre mutazioni durante il Record (M2) ne' il limite degli ID (L2).
- Mancano test di concorrenza tra Salva e Apri/Import/Nuovo (H1) e test dei limiti all'import (M1).
- Nei test app, il `send_fluid` finto di `redirect_app_paths` risponde `''` ai `set`. Per questo ogni Apri o richiamo senza `FakeFluid` finisce in `PARTIAL_APPLY` (globali `failed`): visto in `c1`. Non e' un difetto del codice, ma i test che non installano `FakeFluid` verificano solo il ramo parziale.

## Verificato / non verificato

- Verificato su PC, con codice reale e porte finte: tutti gli scenari marcati [codice reale] o [interleaving forzato].
- Non verificato: tempi e RAM sul Pi (stime sopra), FluidSynth reale (D5, `fonts`), FANTOM-08.
