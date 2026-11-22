# Handoff `ui-routing`

## Implementato

Backend MIDI (owner: `app.py`, `daw_recorder.py`, `startfluid.sh`, nuovo `midi_router.py`), come da `docs/contracts.md` sez. 3, 8.2 e 10.

- `midi_router.py`: unico registro e dispatcher MIDI.
  - Contiene registro dispositivi `midi_devices.json` (bootstrap `keyboard-main` solo con un candidato unico), `normalize_port_name`, stati porta `available|missing|ambiguous|unbound|offline`, destinazione interna = unico client `FLUID Synth*` (non si usa piu' `128:0`).
  - Un solo lettore per input, tramite la callback di rtmidi (nessun `iter_pending`); monitor per parte solo in `managed`.
  - Tracciamento note per porta, rilascio, PANIC e cleanup al ritorno della porta; polling porte ogni 2 s, solo da `app.py`.
- `api_common.py`: busta della sez. 2 (`ok`, `err`, `ack_ok`, `ack_err`, `from_router_error`, tabella HTTP 2.3).
- `daw_recorder.py`: porte, playback, click e PANIC passano dal router.
  - Record: controlla `NO_ARMED_TRACK`, poi `TRANSPORT_ACTIVE`, poi `PORT_UNAVAILABLE` (`details.tracks`) **prima** di `is_recording` (A3, D7). Una traccia registra secondo l'input della sua parte; gli eventi vengono salvati sul canale della traccia.
  - Stop di Record: le note aperte e il CC64 aperto vengono chiusi al beat di stop.
  - Play: `NOTHING_TO_PLAY` o `PORT_UNAVAILABLE`; se solo alcune tracce sono indisponibili, warning `PLAYBACK_TRACKS_UNAVAILABLE`. Gli eventi vanno alla destinazione della parte, con il canale rimappato.
  - Mute/solo hanno effetto immediato (gate sotto il lock della porta, A25).
  - Rilascio di note e sustain su stop, seek, ripartenza del loop, mute e cambio routing.
  - PANIC: su tutte le porte aperte, 16 canali, note-off tracciati + CC64/123/120 (A6); click solo interno, sul canale 9.
- `app.py`:
  - Route nuove: `GET /api/system/status`, `PUT /api/system/mode`, `GET /api/midi/ports[?all=1]`, `GET|PUT|DELETE /api/midi/devices/<alias>`, `POST /api/midi/devices/<alias>/select`, `GET /api/parts`, `GET|PATCH /api/parts/<id>` (evento `parts_update` coalescato a 10 Hz).
  - `save_part(part)` scrive `parts` + la voce legacy `channels` in un'unica scrittura atomica (API per `ui-persistence`).
  - Route DAW/socket con la busta nuova; `select_prog`, `/cc` e `reset_channel` rifiutano le parti esterne (409); `/set_effect` ha la whitelist (400); `/get_state` aggiunge `parts` e `midi_mode`; `restore_settings` applica le esterne via router.
- `startfluid.sh`: `MIDI_MODE` da `last_state.json` (default `legacy`), `midi.autoconnect=$AUTOCONNECT`, `aconnect` solo in legacy, `export MATSYNTH_MIDI_MODE`.
- `.gitignore`: `midi_devices.json` (stato runtime, come `last_state.json`).

## Scelte documentate

- Comandi sonori **interni** (bank/program/CC) via console 9800 (`select`/`cc`), come oggi (D20). Nota e click interni via porta MIDI FluidSynth. Esterni: tutto via porta MIDI. Esito sempre `sent`/`failed`, mai "confermato".
- Codici d'errore scelti dove il contratto lascia margine:
  - `preview_note` fallita: `503 PORT_UNAVAILABLE` (`legacy_daw.js` la ignora).
  - `cc_update` fallito: `SYNTH_UNAVAILABLE` per una parte interna, `PORT_UNAVAILABLE` per una esterna.
  - `/get_state.midi_mode` e' l'oggetto `{effective, configured, restart_required}`.
- `GET /api/system/status.soundfont` e' un **segnaposto** `{state:'idle', file, sf_id, ...}`: il vero stato del caricamento (sez. 4.6) lo implementa `ui-persistence`. `revision` vale `null` finche' la DAW non ha revisioni.
- `managed` resta opt-in: il default e' `legacy` ovunque (script, backend, registro assente o corrotto).

## Verificato e come

    .venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"  -> 86 ok (3 expected failures), 3 run consecutivi
    node --test tests/js                                                      -> 47/47 ok
    "C:\Program Files\Git\bin\bash.exe" -n home/matteo/startfluid.sh          -> ok

- `tests/test_routing.py` (25 test):
  - normalizzazione con gli esempi 3.4; bootstrap unico, ambiguo e assente; interno `missing`/`ambiguous`;
  - playback e preview su destinazione esterna con canale rimappato; monitor `managed` on/off, `legacy` che non inoltra nulla;
  - rifiuti Record/Play; rilascio su stop, mute, seek, loop e cambio routing; PANIC su ogni porta; click solo sul canale 9 interno;
  - route: whitelist `/set_effect`, busta di `/api/parts`, `PATCH` 422/400/409 `RECORDING_ACTIVE`/`skipped`/`sent`, CRUD dispositivi con `DEVICE_IN_USE`, `/api/system/*`, busta delle route DAW.
- Browser headless (Flask locale con `helpers.import_app`): nessun errore JS, la shell legge `/api/parts` (`source: "api"`, 16 parti) e `/api/midi/ports`. 404 attesi solo su `/api/projects/current`, `/api/scenes/current`, `/api/soundfont/status` (route di `ui-persistence`).

## NON verificato (richiede hardware, fase `ui-acceptance`)

- Nomi reali delle porte sul Pi (`mido.get_input_names()` / `get_output_names()` con FANTOM-08 e FluidSynth): da registrare qui. La normalizzazione e' testata solo sugli esempi del contratto.
- FANTOM-08 come destinazione esterna (bank MSB/LSB/program, CC), con Local ON e Local OFF.
- Scollegamento e ricollegamento a caldo (cleanup al ritorno, `ports_status`, `midi_error`).
- `managed` dopo il riavvio del servizio (`autoconnect=0`, nessun `aconnect`, monitor via router, latenza) e rollback a `legacy` (sez. 10.3).
- Avviso `MONITOR_HANDLED_BY_ALSA` con `aconnect -l` reale.
- Carico CPU sul Pi Zero 2 W durante playback 16 tracce e monitor.

## Per le fasi successive

- `ui-persistence`: usare `save_part()` / `router.apply_part(part, send='all')` per il richiamo scena; sostituire il segnaposto `soundfont` in `/api/system/status`.
- `ui-studio`: D6 (ritorno all'inizio dopo Record) non e' implementato qui.
- `ui-offline-settings`: UI per `/api/midi/devices*` e `PUT /api/system/mode`.
- Decisioni utente (2026-11-23): D1, D2, D4, D7 confermate come da raccomandazione; **D3 cambiata**: ID clip per evento, clip sovrapposti consentiti (contratto aggiornato in 5.2, 6.1, 6.3, 6.4, 13). Impatta `ui-persistence` (formato eventi `[beat, bytes, clip_id]`) e `ui-studio` (operazioni clip).
