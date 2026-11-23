# Revisione `ui-routing` (commit 84de7f9)

Revisione in sola lettura. Righe = file **del commit** (`git show 84de7f9:<path>`), non del worktree.
Prove: script nella scratchpad (non nel repo) col Python del venv, sulle copie del commit:
`rev_checks/check_app.py`, `check_router.py`, `check_loop.py`, `check_boot.py`.
`tests/test_routing.py` del commit sulle stesse copie: 25 test OK.

Il tipo di prova e' indicato per ogni difetto:
- **[codice reale]**: route Flask o DAW reali, con il test client o un FakeBackend.
- **[fake]**: router reale su FakeBackend.
- **[interleaving simulato]**: lo stato intermedio fra thread e' stato costruito a mano.
- **[solo hardware]**: dipende da rtmidi/ALSA o dai dispositivi reali.

## ALTA

### H1. Il bootstrap salva su disco `keyboard-main` con `input: null` e Record resta rifiutato per sempre (anche in `legacy`, la modalita' di default)
- **Dove:** `midi_router.py:714-722` (`start`: bootstrap + `_save_registry`), `126-144` (`bootstrap_registry`), `756-767` (enumerazione fallita -> `names_in or []`).
- **Scenario [fake]:** primo avvio senza tastiera accesa o collegata (oppure enumerazione degli input fallita, oppure due candidati).
  - `midi_devices.json` viene scritto con `input: null`.
  - Agli avvii successivi il file esiste e il bootstrap non viene piu' ripetuto: `keyboard-main` resta `unbound` anche con la FANTOM collegata (`check_boot.py`: `unbound`).
  - Ogni Record risponde `PORT_UNAVAILABLE`.
  - Prima, la ricerca per parole chiave veniva ripetuta a ogni avvio, quindi un riavvio risolveva il problema.
  - Oggi non esiste un'interfaccia per associare la tastiera (arrivera' con `ui-offline-settings`): resta solo `PUT /api/midi/devices/keyboard-main` oppure cancellare il file a mano.
- **[solo hardware]:** se la FANTOM-08 espone due porte dello stesso client (per esempio "MIDI 1" e una porta DAW/MIDI 2), il bootstrap trova due candidati e lascia `null` anche con la tastiera collegata (`check_boot.py`).
  - Il comportamento segue alla lettera il contratto 3.4, ma porta alla stessa regressione.
  - Registrare i nomi reali delle porte.
- **Correzione minima:**
  - non salvare il registro di bootstrap quando `input` e' `null`: tenerlo solo in RAM e ripetere il bootstrap al prossimo avvio;
  - in alternativa, durante il polling, finche' `keyboard-main` non e' stato modificato dall'utente e resta `unbound`, associarlo al primo candidato unico che compare.
  - Per il caso FANTOM a due porte serve una decisione dell'utente (unicita' per client, preferendo la prima porta).

## MEDIA

### M1. Regressione dello stato: `channels` scritto dalle route legacy viene sovrascritto o ignorato, e lo strumento importato si perde al riavvio
- **Dove:**
  - `app.py:2074-2103` (`daw_midi_load`) e `2141-2183` (`daw_midi_upload`) scrivono solo `channels[ch].bank/program` e inviano `select`, senza aggiornare `router._parts` ne' `parts`;
  - `app.py:64` (flush debounced) e `216-231` (`_put_part_in_state`) riscrivono `channels[t]` con la parte del router, che a quel punto e' vecchia;
  - `midi_router.py:371-379`: al riavvio `parts`, se presente, ha la precedenza.
- **Scenario [codice reale, test client]:**
  1. si muove uno slider qualsiasi, e nello stato nasce la chiave `parts`;
  2. si carica un MIDI, che imposta `channels['3'].program=50`;
  3. si muove lo slider volume della parte 3: `channels['3'].program` torna al valore vecchio del router (`check_app.py` 3b: 0).
  - Anche senza il passo 3, al riavvio vince `parts` e lo strumento importato si perde.
  - Inoltre `/api/parts` e `/get_state.parts` mostrano un suono diverso da quello che suona davvero.
- **Correzione minima:** in `daw_midi_load` e `daw_midi_upload` aggiornare la parte con `router.apply_part(p, send='none')` + `save_part(p)` al posto della scrittura diretta di `channels`. Le route sono di `ui-persistence`, ma il difetto nasce qui ed e' **bloccante per `ui-persistence`**.

### M2. Record rifiutato: HTTP 400 invece di 503 `PORT_UNAVAILABLE` e invece di 409 `TRANSPORT_ACTIVE` (con Play attivo)
- **Dove:** `app.py:889` (`_daw_refusal(..., http=400)`) + `866-870` (`http=e.get('http') or http`).
- **Scenario [codice reale]:** `check_app.py` restituisce `400 PORT_UNAVAILABLE` e `400 TRANSPORT_ACTIVE`. Il contratto 8.1 chiede 503 e 409; l'override a 400 vale solo per "gia' in Record".
- **Correzione minima:** passare `http=None` a `_daw_refusal`. L'override 400 e' gia' portato da `_fail(..., http=400)` in `daw_recorder.py:248-249`.

### M3. `managed`: il rilascio del trasporto taglia le note suonate dal vivo (monitor) e il sustain del pedale
- **Dove:**
  - `daw_recorder.py:270` e `446` (`release_all` all'avvio di Record e Play);
  - `daw_recorder.py:2008` e `2014` (`release_tracks(range(16))` a ogni ripartenza del loop e a fine playback);
  - `479` e `490` (stop);
  - `midi_router.py:1198-1223`.
- **Scenario [fake]:** in overdub con loop attivo, l'utente tiene una nota sulla parte 0 (monitor interno, ch0). A ogni ripartenza del loop arrivano note-off + CC64=0 + CC123 su ch0, quindi la nota si interrompe e il pedale resta abbassato fisicamente ma non per il synth (`check_loop.py`: True con Play in corso).
  - Il tracciamento per (porta, canale, nota) non distingue la sorgente.
  - In `legacy` l'effetto c'era gia' (vecchio CC123 su tutti i canali), quindi li' non e' una regressione.
- **Correzione minima:** rilasciare solo le destinazioni delle tracce con `has_data`, non `range(16)`, ed evitare `release_all` all'avvio.
- **Correzione completa:** contatori separati per playback e monitor; rilasciare solo i primi, e CC64=0 solo se l'ha inviato il playback.

### M4. `managed`: un input scollegato con note tenute lascia note sospese sulla destinazione del monitor
- **Dove:** `midi_router.py:868-870`: l'input viene chiuso senza rilasciare le destinazioni delle sue parti.
- **Scenario [fake]:** si tiene premuto un tasto (monitor -> FluidSynth) e si scollega la tastiera. Il note-on resta inviato, con il contatore a 1, e nessun note-off (`check_router.py` A). Il caso "scollegare con note/sustain attivi" e' nel piano 12.
  - Inoltre un Record in corso resta `is_recording=True` senza `midi_error` (viene emesso solo `ports_status`).
- **Correzione minima:**
  - in `_apply_enumeration`, per gli input rimossi, raccogliere le parti in `ip.match`/`ip.mon` e chiamare `release_tracks(...)` dopo aver rilasciato `_cfg_lock`;
  - emettere `midi_error` per l'input perso.

### M5. Deadlock potenziale fra il polling e la callback rtmidi (impatto: app bloccata; probabilita' bassa; **da verificare su hardware**)
- **Dove:**
  - `midi_router.py:868-870` e `962-968`: `close()` dell'input viene chiamato sotto `_cfg_lock`;
  - `1163-1164` e `1177-1194`: dalla callback, un invio fallito chiama `_mark_out_failed`, che prende `_cfg_lock`.
- **Scenario [interleaving simulato + fake che fa join in `close()`]:**
  - In ALSA, `RtMidi closePort` fa join del thread di input.
  - Caso realistico: modalita' managed, parte che monitora sulla FANTOM (Local OFF), cavo USB staccato mentre si suona.
  - La callback e' dentro un invio che fallisce; intanto il polling tiene `_cfg_lock` e chiude l'input. La callback aspetta `_cfg_lock` e il polling aspetta la callback (`check_router.py` E: True).
  - A quel punto si bloccano il polling e tutte le route che usano `_cfg_lock`.
- **Stessa famiglia:** con `_cfg_lock` tenuto si eseguono `open_output`/`open_input` e gli invii di cleanup (`849-855`, `899-929`), contro quanto dice il docstring (`23-25`).
- **Correzione minima:**
  - sotto lock impostare solo `ip.closed=True` e togliere l'input dalle tabelle; `port.close()` e gli invii di cleanup vanno fatti dopo il rilascio del lock;
  - dal thread della callback non prendere mai `_cfg_lock`: `_mark_out_failed` va accodato con `self._deferred.call_later(0, ...)`.

## BASSA

### L1. Finestra fra `_track_ch` e `_track_out` in `_rebuild_tables` [interleaving simulato]
- **Dove:** `midi_router.py:983-984`: il canale nuovo viene pubblicato prima della porta nuova.
- **Scenario:** il playback invia proprio in quella finestra, cioe' sulla vecchia porta col nuovo canale. `apply_part` rilascia solo il vecchio canale e la nota resta sospesa (`check_router.py` B: note_on ch5 su FluidSynth, contatore 1).
- **Correzione:** una sola lista di tuple `(op, ch)` assegnata in modo atomico, oppure rilasciare `old_op` su entrambi i canali, il vecchio e il nuovo.

### L2. Ritorno di una porta: cleanup solo se c'erano note o sustain tracciati; stato di ripristino non esposto [fake]
- **Dove:** `midi_router.py:945-946` (`_dead_outs` solo se `touched`/`sustain`), `909-913`.
- **Scenario:** al ritorno della porta non vengono inviati CC64=0/CC123 (contratto 3.5: "DEVE"); `check_router.py` C restituisce `[]`.
  - `last_recovery` non e' esposto da nessuna route (grep su `app.py`), quindi lo "stato di ripristino" non e' visibile.
- **Correzione:** tenere sempre il descrittore delle porte perse, inviare il cleanup forzato al ritorno ed esporre `last_recovery` in `/api/system/status` o nel payload `ports_status`.

### L3. Pi Zero 2 W: l'enumerazione crea 4 client ALSA ogni 2 s
- **Dove:** `midi_router.py:567-571` e `756-767`. `mido.get_input_names()` e `get_output_names()` chiamano ognuna `get_devices()`, che crea MidiIn+MidiOut (mido 1.3.3, `backends/rtmidi.py:51-71`).
- **Correzione:** una sola `get_devices()` per ciclo di polling (si dimezzano i client). Saltare `_rebuild_tables` e la ricostruzione dei dict se i nomi non sono cambiati.
- Una porta che continua a fallire viene riaperta e loggata ogni 2 s.

### L4. Tracce mute: `Message.from_bytes` + lock per ogni evento
- **Dove:** `daw_recorder.py:1986-1989`.
- **Correzione:** controllare prima `if gate[channel]` fuori dal lock; il controllo sotto lock resta quello che conta.

### L5. JSON/emit di `midi_event_new` nel thread rtmidi
- **Dove:** `daw_recorder.py:1913-1924`.
- **Effetto:** con `async_mode='threading'` (`app.py:16`) e' sicuro, ma ritarda l'evento successivo dello stesso input. Il monitor dell'evento corrente parte prima (`midi_router.py:1298-1299`).
- **Correzione:** accodare l'evento e farlo emettere al thread di Record (5 ms).

### L6. Riaggancio rapido di un dispositivo USB [solo hardware]
- **Scenario:** il dispositivo si riaggancia entro un ciclo di polling di 2 s con lo stesso nome (client ALSA riusato). Lo stato resta `available` su un oggetto porta vecchio e ALSA non segnala errori in invio, quindi gli invii vanno persi senza errore.
- **Da verificare:** con il cavo vero.

## Test che non verificano cio' che dichiarano (categoria 8)
- `test_record_during_play_is_transport_active_409` controlla solo `last_error.code` della DAW, non l'HTTP (che e' 400, M2). Nessun test a livello di route copre il caso 503 `PORT_UNAVAILABLE` di Record.
- `test_loop_restart_releases`: l'asserzione sul note-off 60 arriva dopo `stop_all()`, quindi passerebbe anche senza il rilascio al loop. Il controllo su CC123 invece e' valido.
- Fake troppo permissivi:
  - `FakeMidiInput.feed` chiama la callback in modo sincrono nel thread del test e `close()` non fa join, quindi nessuna concorrenza (M5 non e' rilevabile);
  - `FakeMidiOutput.send` non fallisce mai salvo dopo `close()`, quindi `_mark_out_failed`, lo stato `offline` da invio fallito e il PANIC parziale non sono testati.
- Non testati, benche' il handoff li dichiari implementati:
  - cleanup al ritorno porta (`_dead_outs`, `last_recovery`);
  - perdita dell'input con note tenute (M4);
  - persistenza del bootstrap (H1);
  - `select_prog`/`/cc` e coerenza `parts`/`channels` dopo le route legacy (M1).
- Le modifiche a `test_baseline_smoke.py` sono coerenti con il contratto (11.4): non sono state allentate.

## Categorie senza difetti
- **Note doppie (2):** nessun difetto.
  - In `legacy` `ip.mon` e' vuota e non c'e' inoltro.
  - In `managed` `startfluid.sh` mette `autoconnect=0` e non esegue `aconnect`.
  - C'e' un solo lettore per input (callback, nessun `iter_pending` residuo).
  - `/api/midi_devices` e `/api/save_hardware` eseguono solo `aconnect -i` (lettura).
  - `double_monitor` e' solo un avviso, come da contratto.
- **Conformita' (4):**
  - click solo interno, ch 9, note 76/77, mai registrato (la registrazione arriva solo dalle callback di input);
  - nessun fallback (`send_internal`/`_track_out` = None se la porta non e' disponibile);
  - D7 rispettato: il preflight viene prima di `is_recording`;
  - `legacy` e' il default per env, stato e registro (assente o corrotto);
  - filtro `_CHANNEL_TYPES`: niente SysEx, clock o realtime; il playback salta i messaggi senza canale.
  - Restano fuori da questo punto H1 (bootstrap) e M2 (codici HTTP).
- **startfluid.sh (5):** nessun difetto (`bash -n` OK; `jq` non disponibile qui, logica verificata leggendo lo script).
  - File assente, corrotto o con valore non stringa: `jq` fallisce o restituisce un valore diverso da `managed`, quindi `legacy`.
  - `aconnect` solo in `legacy`; `export` corretto; il rollback 10.3 e' coerente.
- **Regressioni sulle route (6), oltre a M1 e M2:** nessuna.
  - `legacy_daw.js` legge `/get_state.channels`, `ack.status` e `data.message`, tutti conservati.
  - `/get_state` e' cambiato solo in modo additivo.
  - Errori di `preview_note` ignorati dal client.

## Da verificare su hardware
- Nomi reali delle porte FANTOM e numero di porte (H1).
- Se `closePort` fa join con callback attiva (M5).
- Riaggancio USB rapido (L6).
- Costo CPU del polling sul Pi (L3).
