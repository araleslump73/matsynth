# Handoff `ui-baseline`

## Implementato

Nessuna modifica a `home/matteo/matsynth_web/`. File nuovi:

- `docs/regression-checklist.md`: 66 voci derivate dalle 29 righe della tabella di sez. 6 con posizione attuale (file, funzione/route/ID DOM), collocazione futura e colonna Verifica vuota; tabelle separate di tutte le route HTTP di `app.py` e degli eventi Socket.IO (client->server e server->client) per `ui-contracts`.
- `tests/helpers.py`: `FakeMidiInput` (`feed`, `iter_pending`, `close`), `FakeMidiOutput` (`send` registra i `mido.Message`, `notes()`, `of_type()`, `close`), `make_daw()` (DAW senza hardware, con selezione porte reale), `import_app()` + `redirect_app_paths()` (importa `app.py` con rete/disco neutralizzati), `wait_until`.
- `tests/fixtures/make_fixtures.py` + risultato committabile: `workload_16x500.mid` (16 tracce, 500 coppie note, 120 BPM, 64 KB), `bank_program_cc.mid` (Bank MSB1/LSB2/PC7 a tick 0, PC 12 a beat 4, sustain CC64, pitch bend, 100 BPM), `presets/legacy_minimal.json`, `legacy_pan.json`, `legacy_decay.json` (sintetici, forma di `api_presets_save`/`api_capture_current_config`). Lo script e' deterministico (md5 identico su due esecuzioni).
- `tests/test_baseline_smoke.py`: 40 test unittest (characterization).
- `tools/pi_baseline.sh` + `docs/pi-measurement.md`: preparazione della misura sul Pi.

## Come aggirato l'hardware

- `import daw_recorder` non richiede rtmidi. Solo `MultiTrackDAW._init_midi_ports()` (chiamato da `__init__`) tocca le porte: senza rtmidi mido solleva `ImportError`, catturato nella funzione, e la DAW nasce con `midi_input = midi_output = None`.
- `make_daw()` usa `mock.patch.object` solo su `mido.get_input_names/get_output_names/open_input/open_output` durante il costruttore; `mido.Message`, `MidiFile`, ecc. sono quelli reali, la logica di selezione per parole chiave gira davvero.
- `app.py` non e' importabile "a freddo" (makedirs su `/home/matteo/...`, istanza DAW, `startup_init_once` verso la console 9800 con 5 retry). `import_app()` durante l'import rende `os.makedirs` no-op, `socket.socket` un errore (`send_fluid` ritorna `""`), `time.sleep` no-op solo nel thread principale; poi `redirect_app_paths()` punta `PRESETS_DIR/MIDI_DIR/STATE_FILE` in una cartella temporanea e sostituisce `send_fluid` con un registratore dei comandi. Verificato che non venga creata nessuna cartella fuori dal temp. L'import costa circa 1 s e stampa log.
- Ogni `MultiTrackDAW` avvia un thread demone `_update_loop` che non si ferma mai: un thread inerte per istanza fino a fine processo (innocuo).

## Verifica (esito reale)

Comando del brief, lanciato 3 volte consecutive:

    python -m unittest discover -s tests -p "test_*.py" -v
    Ran 40 tests in ~3.4s -> OK (expected failures=4)

0 fallimenti, 0 errori, nessuna flakiness osservata. Per la registrazione i test dipendono da piccole attese reali (50 ms + 10 ms di grace period in `start_recording`/`_record_loop`) con `wait_until`; su un Pi lento potrebbero servire timeout piu' larghi, ma non e' pensato per girare sul Pi.

`bash -n tools/pi_baseline.sh`: sintassi ok. Lo script NON e' stato eseguito.

## Comportamenti anomali attuali (documentati, NON corretti)

Ognuno ha un test che descrive lo stato di fatto e, dove utile, un test `DESIRED` marcato `expectedFailure`. Quando la fase pertinente corregge, il decoratore va tolto e il test caratterizzante aggiornato (l'expectedFailure diventa "unexpected success" e fa ricordare di farlo).

1. **Import SMF perde il Bank LSB e sovrascrive il programma.** Fixture `bank_program_cc.mid` (CC0=1, CC32=2, PC7, poi PC12 a beat 4): `import_midi` ritorna `{'bank': 1, 'program': 12}`. Il LSB (CC32) e' scartato, l'ultimo Program Change vince, e nessun Program Change resta nelle tracce (quello a beat 4 e' perso). L'export invece scrive correttamente MSB/LSB/PC7 a tick 0 da `bank=(1<<7)|2`: il round trip non e' simmetrico (export ok, import no). Test: `test_import_bank_lsb_is_dropped_and_last_program_change_wins`, `test_DESIRED_import_keeps_bank_msb1_lsb2_program7`, `test_DESIRED_program_change_at_nonzero_beat_is_kept_at_that_beat`. Stesso effetto a livello route: `/api/daw/midi/upload` invia a FluidSynth `select 0 1 1 12`.
2. **Preset: `pan` ignorato.** `api_presets_apply` non legge `pan` e `api_capture_current_config` non lo produce; il piano (sez. 12) vuole pan quando presente. Il preset non persiste i canali in `last_state.json` (solo gain/reverb/chorus), non carica il SoundFont, e dalla UI di `presets.html` ricarica la pagina (`window.location.href='/'`). Test: `test_apply_pan_is_ignored_today`, `test_DESIRED_apply_pan_sends_cc10`, `test_apply_persists_globals_but_not_channels_in_state_file`. Il decay CC75 invece viene applicato (`cc <ch> 75 <v>`).
3. **Stato "recording/playing" fittizio senza porte.** Con `midi_input=None`, `start_recording` ritorna True e lascia `is_recording=True` (il thread esce subito); idem `is_playing` senza `midi_output`. Il piano vuole errore/nessuno stato fittizio. Test caratterizzante piu' `test_DESIRED_record_without_input_port_is_refused`.
4. **Selezione porta ambigua: vince la prima**, senza errore (due `USB ...`). Priorita' FANTOM > SINCO > USB; `Midi Through` escluso. Il piano vuole errore/stato non disponibile.
5. **Nessun invio live durante Record** (il monitoring e' ALSA diretto): oggi l'unico traffico su `midi_output` in record e' l'All Notes Off iniziale. Utile come riferimento per `ui-routing` (monitor esterno ON/OFF).
6. **PANIC invia solo CC123 su `midi_output`**, non All Sound Off, nonostante il docstring "All Notes Off e All Sound Off".
7. **Import non crea clip** (`get_clips` vuoto dopo import), solo la registrazione li crea.
8. **Studio, "Cut Clip at Playhead" probabilmente rotto** (evidenza statica, non verificato in browser): `dawCutClipAtPlayhead` legge `dawState.current_beat`, che nessun codice valorizza (`get_state` non lo emette); resta 0 e la ricerca della clip che contiene il playhead fallisce sempre.
9. **ID DOM duplicato `midi-file-input`** in `index.html` (menu File, riga ~43, e modale MIDI, riga ~5124): `getElementById` usa il primo.
10. **Route `/set_effect/<type>/<val>`** non ha whitelist (quella via socket si'). `api_presets_rename` sovrascrive senza controllo se il nuovo nome file esiste.
11. **Funzioni della tabella senza UI oggi**: preferiti scene (non esistono), reset canale (route presente, chiamata commentata in `changeInst`), uscita MIDI selezionabile (non esiste), punti A/B diretti (solo "loop da selezione"), "taglia" (non esiste come comando).

## Misure Pi: NON eseguite

Nessuna misura e' stata fatta: niente RSS/CPU/swap/jitter reali, nessun accesso al Pi (vincolo del brief). Pronti da eseguire a mano: `tools/pi_baseline.sh` e la procedura in `docs/pi-measurement.md` con `tests/fixtures/workload_16x500.mid`. Il jitter dello script e' un proxy di scheduler (sleep 1 ms), non il jitter MIDI end-to-end; quest'ultimo non ha ancora un metodo definito. Anche il tempo di import (0,24 s) ed export (0,5 s) della fixture 16x500 misurati qui sono su un PC e non dicono nulla sul Pi.

## Da verificare su hardware / browser (non coperto)

Tastiera MIDI reale, FANTOM-08, `aconnect`/ALSA, FluidSynth reale, hotspot, tutta la UI nel browser (le righe della checklist hanno la colonna Verifica vuota). I test usano porte finte e un registratore di comandi: dimostrano la logica Python, non l'integrazione.

## Decisioni e deviazioni

- Tabella di sez. 6 spezzata in 66 righe (una per operazione) invece delle 29 righe aggregate, per poterle spuntare singolarmente.
- Importare `app.py` nei test (con patch di rete/disco) invece di duplicarne la logica: permette di testare le route reali dei preset con il test client Flask. Costo: dipendenza dai nomi globali di `app.py` (`PRESETS_DIR`, `MIDI_DIR`, `STATE_FILE`, `send_fluid`, `sf_id`). Se `ui-routing`/`ui-persistence` li spostano, aggiornare `helpers.redirect_app_paths`.
- `tools/pi_baseline.sh` non ha il bit eseguibile impostato (lavoro da Windows): lanciarlo con `bash tools/pi_baseline.sh`.
- Nessun tentativo di eseguire lo script di misura (richiesto: non eseguibile qui).

## Punti aperti per la fase successiva (`ui-contracts`)

- Decidere la rappresentazione del bank: oggi `bank` intero = `(MSB<<7)|LSB` in export, solo MSB in import, mentre FluidSynth/UI usano bank 0/128.
- Indici posizionali per modifica note/eventi (`on_idx/off_idx`, `/events/<index>`): nessuna revisione, rischio di indici obsoleti (criterio sez. 12 "revisione obsoleta").
- Formato errori non uniforme: alcune route danno `status:error`+HTTP, `/set_effect` e `/select_prog` testo semplice, `rename`/`color` `ok|error` senza messaggio.
- Concorrenza: `last_state.json` viene scritto da piu' punti (`select_prog` diretto, `save_state`, flush debounced).
- Metodo di misura del jitter MIDI reale.
