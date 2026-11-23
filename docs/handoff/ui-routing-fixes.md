# Handoff `ui-routing-fixes` (correzioni dopo `ui-routing-review.md`)

File toccati: `home/matteo/matsynth_web/midi_router.py`, `tests/test_routing.py`, nuovo `tests/routing_fakes.py`. Nessun altro file (`daw_recorder.py`, `app.py`, `tests/helpers.py` invariati da questa fase).

## Corretto

### M5 (deadlock polling/callback) + I/O sotto lock
- Ordine dei lock, documentato nel docstring del modulo: `_enum_lock -> _cfg_lock -> op.lock`, mai al contrario.
  - `_enum_lock` (nuovo): serializza le transizioni porte (`_apply_enumeration`, `close()`, gestione invio fallito); protegge `_dead_outs`, `_failed`, `last_recovery`. Puo' essere tenuto durante l'I/O.
  - `_cfg_lock`: solo copie e pubblicazioni brevi, mai durante open/close/send MIDI.
  - `op.lock`: foglia (sotto di esso solo `port.send()`).
- `_apply_enumeration` in tre passi: `_plan_enumeration` (sotto `_cfg_lock`: calcolo, distacco delle porte da chiudere, tabelle senza di esse), `_port_io` (senza `_cfg_lock`: chiusure, rilasci, aperture, cleanup di ritorno), `_publish_enumeration` (sotto `_cfg_lock`: porte aperte e stati).
- `close()` di un input viene chiamato senza alcun lock; `close()` del router fa lo stesso ed e' riutilizzabile (lo usa `RoutingAppTests.setUp`).
- Dalla callback rtmidi non si prende mai `_cfg_lock`. Un invio fallito marca `op.failed` (gli invii successivi tornano subito) e accoda `_handle_out_failed` sul thread differito (coalescato per porta). Quindi lo stato `offline` e `midi_error` dopo un invio fallito arrivano in modo **asincrono** (qualche ms). Vale per tutti i chiamanti (playback, monitor, preview, click, rilasci, PANIC).
- La DAW (`_on_router_input`) prende solo `_rec_lock` e nessun metodo del router: l'invariante "la callback non prende `_cfg_lock`" vale anche con il codice DAW attuale.

### L1 (porta e canale pubblicati separatamente)
- `_track_out` e `_track_ch` sono sostituiti da `_routes`: una tupla di 16 coppie `(op, canale)`, assegnata in un solo passo.
- `send_track` ricontrolla sotto `op.lock` che porta e canale siano ancora quelli correnti. Lettori aggiornati: `preview_note`, `release_tracks`, `playback_preflight`, `apply_part`.

### H1 (bootstrap senza ingresso salvato per sempre)
- Scelta: il registro di bootstrap con `keyboard-main.input = null` (nessuna tastiera, candidati ambigui o enumerazione input fallita) **non viene scritto su disco**: resta in RAM con il flag `_bootstrap_unsaved`.
- Finche' il flag e' attivo e `keyboard-main.input` e' `null`, ogni enumerazione riuscita ripete la stessa regola di `bootstrap_registry` (`_rebind_bootstrap`). Al primo candidato **unico** associa solo `keyboard-main.input` e salva.
  - Candidati ambigui: mai scelti.
  - Senza file, il prossimo avvio rifa' comunque il bootstrap. E' coerente con 3.4 ("solo se `midi_devices.json` non esiste").
- Il flag si spegne con un `PUT` o `DELETE` dell'utente su `keyboard-main`: una scelta esplicita dell'utente, anche `input: null`, non viene mai sovrascritta.
- Caso residuo, accettato: se l'utente salva un **altro** alias prima che compaia la tastiera e poi riavvia, il file contiene `keyboard-main` `null` e al riavvio non c'e' piu' bootstrap. Nello stesso processo il riaggancio continua; dopo il riavvio l'utente associa la tastiera in Impostazioni.
- FANTOM-08 con due porte dello stesso client: resta `unbound` (ambiguo, contratto 3.4). Serve una decisione dell'utente (per esempio "prima porta del client"); non presa qui.

### M4 (ingresso scollegato con note tenute)
- Per ogni ingresso rimosso, la parte di pianificazione raccoglie le coppie `(op, canale)` delle parti in `ip.mon`. Dopo la `close()` (cioe' senza callback in volo) rilascia con `scope='live'`: note-off delle note live, CC64=0 e CC123 secondo la regola M3.
- Se la porta e' sparita dall'enumerazione viene emesso `midi_error` `{alias, port, direction: 'input', status, message: 'Ingresso MIDI scollegato', at}`. Lo stato porta diventa `missing` e `record_preflight` lo riporta: nessun falso "disponibile".
- Cambio di comportamento anche in `legacy` (il default): staccando la tastiera prima si emetteva solo `ports_status`, ora anche `midi_error`. Nessun JS gestisce ancora `midi_error`: utile per `ui-offline-settings`.
- **Deviazione da segnalare:** `status` nel payload e' lo stato reale (`missing`, tab. 3.5: "nessuna porta corrisponde"), non `offline` come scrive letteralmente la 9.2. Forzare `offline` sarebbe contraddetto dal polling successivo. Va confermato con l'utente.
- **Non risolto qui (serve `daw_recorder.py`):** un Record in corso resta `is_recording=True`; la UI riceve `midi_error`, ma la DAW non lo sa. Richiesta a chi possiede `daw_recorder.py`:
  - il router puo' esporre un ascoltatore, per esempio `router.set_port_listener(fn)` chiamato con gli stessi eventi `midi_error`;
  - la DAW decide se fermare il Record (con chiusura delle note aperte e ritorno D6) o marcarlo con un errore.

### L2 (ritorno porta)
- Una porta di uscita persa (sparita dall'enumerazione o fallita in invio) viene **sempre** tenuta in `_dead_outs`, anche senza note tracciate. Al ritorno, prima di pubblicarla, viene inviato il cleanup forzato.
  - Canali coperti: rotte MatSynth di quella porta, piu' i canali toccati.
  - Contenuto: note-off tracciati, CC64=0, CC123.
- `close()` del router svuota `_dead_outs` (chiusura voluta).
- `last_recovery` e' esposto da `describe_ports()`, quindi in `GET /api/midi/ports` (`ok(**describe_ports())`) e nel payload `ports_status`, **senza toccare `app.py`**.
  - Ogni voce: `{port, at, cleanup: 'sent'|'none'|'failed', channels}`, ultime 8.
  - La chiave e' fuori dalla firma di cambio stato.
  - Cleanup fallito: la porta resta `offline` e viene emesso `midi_error`.

### M3 (il trasporto taglia note e pedale live) - risolto dentro il router
- `_OutPort` ha due serie di contatori: trasporto (`notes/touched/sustain`: playback, preview, click) e live (`live_notes/live_touched/live_sustain`: monitor). La callback chiama `send_track(..., live=True)`. Il parametro `live` e' nuovo, con default `False`, quindi le chiamate di `daw_recorder.py` restano compatibili.
- `release_tracks` e `release_all`, i metodi usati da `daw_recorder.py` (`start_recording`, `start_playback`, `stop_playback`, `stop_all` tramite `_send_all_notes_off`; `_apply_audibility`; `_playback_loop` alla ripartenza del loop e a fine brano), rilasciano **solo il trasporto**. Poiche' CC64=0 e CC123 agiscono su tutto il canale del synth:
  - CC64=0 si salta se su quel (porta, canale) l'utente tiene il pedale live;
  - CC123 si salta se l'utente tiene note o pedale live.
  - E' una lettura del 3.7 ("rilasciare note e sustain **interessati**"): da confermare.
- Rilasciano tutto (trasporto + live): PANIC; cambio destinazione in `apply_part`; ritorno porta; porta non piu' usata.
  - Cambio solo di input/monitor in `apply_part`: solo le note live, il cui note-off non arrivera' piu'.
  - Ingresso perso: solo le note live.
- In `legacy` i contatori live restano a zero (nessun monitor), quindi il comportamento e' identico a prima.
- Limite MIDI, non risolvibile: se playback e utente suonano **la stessa nota sullo stesso canale**, il note-off del trasporto la spegne anche per l'utente.
- Restano utili, ma non necessari, in `daw_recorder.py` (correzione minima del revisore): rilasciare solo le tracce con `has_data` invece di `range(16)` e non chiamare `release_all` all'avvio. Riducono messaggi inutili, non correggono note.

### Test (fake piu' severi)
- `tests/routing_fakes.py`:
  - `ThreadedMidiInput`: callback su un thread proprio; `close()` fa join, con timeout di 5 s e `join_timed_out`, quindi nessun test puo' bloccarsi per sempre.
  - `FlakyMidiOutput`: `fail`, `block_next()`/`release()`.
  - `RoutingBackend`: `fail_enum`, `io_probe` su open/close.
  - `make_routing_router`, `close_router`.
- Nuovi test in `tests/test_routing.py`:
  - M5: deadlock polling/callback; deadlock `close()`/callback; nessun `_cfg_lock` tenuto durante open/close (sonda da un altro thread).
  - M4: ingresso perso con nota e pedale tenuti.
  - L2: ritorno porta senza note; invio fallito, poi `offline`, Play `PORT_UNAVAILABLE`, ritorno con note-off della nota tracciata; PANIC parziale.
  - L1: sonda deterministica: un invio dopo ogni assegnamento di attributo del router durante `apply_part`.
  - M3: loop e stop non tagliano la nota e il pedale live; PANIC e cambio monitor li rilasciano.
  - H1 (`BootstrapRegistryTests`): senza tastiera e poi con tastiera; riavvio senza file; enumerazione fallita; candidati ambigui mai scelti; scelta dell'utente non sovrascritta.
- `test_loop_restart_releases` rafforzato: il note-off viene verificato **prima** di `stop_all()`.

## Verificato e come
- Comando del brief:

      "$VENV/Scripts/python.exe" -m unittest discover -s tests -p "test_*.py" -v

  Esito: **179 test OK** negli ultimi 2 run consecutivi; prima, 149 OK in 3 run consecutivi. Il numero cresce perche' la fase parallela `ui-persistence` sta aggiungendo test.
  - Durante il lavoro alcuni run hanno dato fallimenti transitori (`test_baseline_smoke.LegacyPresetTests`, `test_persistence_app`), con numero di test e insieme di fallimenti che cambiavano fra un run e l'altro.
  - Con il router di HEAD su una copia dello stesso albero si e' visto lo stesso: sono modifiche in corso di `ui-persistence`, non di questa fase. Agli ultimi run, tutto verde.
- `tests/test_routing.py` da solo: 40 test OK, 5 run consecutivi.
- I 15 nuovi test eseguiti contro il `midi_router.py` di HEAD (copia nella scratchpad):
  - 12 falliscono sull'asserzione del difetto: M5 x3 (il deadlock viene riprodotto: polling vivo dopo 2 s, si sblocca solo per il timeout di 5 s del fake), L1, L2, M3 loop/stop, M4, PANIC parziale, H1 x4;
  - 2 vanno in errore per API assente (`live_notes`, chiave `last_recovery`);
  - 1 passa anche su HEAD (`test_user_choice_is_not_overridden`): e' una guardia contro la regressione della nuova logica H1.
- Tutto e' **simulato con fake**: nessuna prova su rtmidi/ALSA reali.

## NON corretto (e perche')
- **L3** (4 client ALSA per ciclo di polling): solo documentato.
  - Una singola `get_devices()` richiede l'API privata di mido (`backend._get_devices`) e scavalcherebbe le patch di `helpers.make_daw` sulle quattro funzioni pubbliche di mido.
  - Proposta: `MidoBackend.get_port_names()` con una sola enumerazione, usata solo se le funzioni pubbliche non sono sostituite, piu' un test dedicato. Saltare la ricostruzione se i nomi non cambiano ha un guadagno trascurabile rispetto alla creazione dei client ALSA.
- **L4, L5**: in `daw_recorder.py` (fuori perimetro).
  - L4: controllare `gate[channel]` prima di `Message.from_bytes`.
  - L5: accodare `midi_event_new` e farlo emettere al thread di Record.
- **L6** (riaggancio USB entro 2 s): solo hardware.
- **M1, M2**: in `app.py` (fase `ui-persistence`).
- Una porta che continua a fallire viene ritentata e loggata ogni 2 s, come prima (permette il recupero automatico).

## Da verificare su hardware (Pi + FANTOM-08 + FluidSynth)
- `closePort` di RtMidi/ALSA con callback attiva (join) e assenza di blocchi: staccare il cavo USB mentre si suona in `managed`, con monitor verso la FANTOM (Local OFF) e verso FluidSynth.
- Scollegamento con tasto e pedale tenuti: niente note sospese su FluidSynth e FANTOM; `midi_error` in UI; Record in corso (vedi richiesta DAW).
- Ritorno della porta: CC64=0 e CC123 ricevuti prima delle nuove note; `last_recovery` in `/api/midi/ports`.
- M3 dal vivo: tenere nota e pedale durante la ripartenza del loop e lo stop, con FluidSynth (comportamento di CC123 e del note-off sulla stessa nota) e con la FANTOM.
- Nomi reali delle porte e numero di porte della FANTOM (H1, caso a due porte); primo avvio senza tastiera, poi accensione.
- Latenza della gestione differita di un invio fallito sul Pi Zero 2 W.

## Regole per i chiamanti (anche nel docstring di `midi_router.py`)
- Mai chiamare `refresh_ports`, `set_device`, `delete_device`, `select_port` o `close` tenendo `router._cfg_lock`, o un lock che la callback di input puo' prendere (per esempio `_rec_lock` della DAW). Oggi `test_persistence_app.py` usa `_cfg_lock` solo per assegnare `_devices` e chiama `refresh_ports` fuori: e' corretto.
- Il recorder agganciato con `set_recorder` (`_on_router_input`) gira nel thread rtmidi: non deve chiamare metodi del router che prendono `_cfg_lock` o `_enum_lock`. Verificato sul codice DAW attuale, che non ne chiama.
- `release_all`/`release_tracks` ora rilasciano solo il trasporto. Un futuro chiamante che debba azzerare tutto in `managed` (richiamo scena, caricamento progetto) deve usare `panic()` o il cambio destinazione di `apply_part`. Grep al termine della fase: nessun nuovo chiamante oltre quelli della DAW elencati sopra.

## Rischio residuo
- Una gestione differita di un invio fallito ancora in coda si riconosce dall'identita' di `op`, e gli `op` vengono riusati tramite `_dead_outs`. Mitigato: se nel frattempo la porta e' stata riaperta e ripulita (`op.failed` azzerato), la gestione non fa nulla.

## Note per le fasi successive
- API interne cambiate (nessun chiamante esterno trovato con grep):
  - `_track_out`/`_track_ch` sostituiti da `_routes`;
  - `_release_out(op, channels, scope, force)` senza `events`;
  - `_mark_out_failed` sostituito da `_schedule_out_failed`/`_handle_out_failed`.
- API pubbliche: `send_track(..., live=False)` ha un parametro in piu'. `release_tracks`/`release_all` ora rilasciano solo il trasporto (in `legacy` nessuna differenza). `describe_ports()` ha la chiave in piu' `last_recovery`.
- I test che controllano `offline` dopo un invio fallito devono usare `wait_until` (gestione asincrona).
