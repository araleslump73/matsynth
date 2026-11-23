# MatSynth: contratti dati, API ed eventi (fase `ui-contracts`)

Stato: **normativo** per le fasi `ui-shell`, `ui-routing`, `ui-persistence`, `ui-live`, `ui-studio`, `ui-editor`, `ui-offline-settings`, `ui-acceptance`. Nessun codice applicativo e' stato modificato in questa fase: tutto cio' che segue e' da implementare, salvo dove e' scritto "oggi" (comportamento attuale verificato leggendo il sorgente del ramo di lavoro, HEAD `304977b`).

Parole chiave: **DEVE / MUST** = obbligatorio; **NON DEVE / MUST NOT** = vietato; **DOVREBBE / SHOULD** = raccomandato, scostarsi solo con motivo scritto nell'handoff; **PUO' / MAY** = facoltativo. Riferimenti al codice nella forma `file:funzione` (i numeri di riga invecchiano: cercare per nome). Percorsi: `app.py`, `daw_recorder.py`, `index.html` = `home/matteo/matsynth_web/...`; `startfluid.sh` = `home/matteo/startfluid.sh`.

Esempi JSON eseguibili: `docs/schemas/*.json`, verificati da `tests/test_contract_examples.py` con il validatore/migratore di riferimento `tests/contract_reference.py` (specifica eseguibile di sez. 3-6, nessuna dipendenza; `ui-persistence` PUO' portarlo in `project_store.py` e poi far importare al test il modulo reale). In caso di discordanza tra testo ed esempi prevale il testo, e la discordanza va corretta.

## Indice

1. [Convenzioni generali e unita' musicali](#1-convenzioni-generali-e-unita-musicali)
2. [Formato delle risposte, errori, ack e timeout](#2-formato-delle-risposte-errori-ack-e-timeout)
3. [Parte e routing](#3-parte-e-routing)
4. [Scena versionata e migrazione dei preset legacy](#4-scena-versionata-e-migrazione-dei-preset-legacy)
5. [Progetto nativo `.matsynth.json`](#5-progetto-nativo-matsynthjson)
6. [Modello DAW: eventi, coppie, clip, undo, revisioni](#6-modello-daw-eventi-coppie-clip-undo-revisioni)
7. [Import/export MIDI (SMF)](#7-importexport-midi-smf)
8. [API HTTP](#8-api-http)
9. [Eventi Socket.IO](#9-eventi-socketio)
10. [Modalita' di startup `legacy` / `managed`](#10-modalita-di-startup-legacy--managed)
11. [Fasi: ordine, ownership, interfacce; anomalie e test](#11-fasi-ordine-ownership-interfacce-anomalie-e-test)
12. [Criteri di accettazione per fase](#12-criteri-di-accettazione-per-fase)
13. [Deviazioni e punti da confermare con l'utente](#13-deviazioni-e-punti-da-confermare-con-lutente)

---

## 1. Convenzioni generali e unita' musicali

### 1.1 Identificatori

| Oggetto | Tipo / formato | Regola |
| --- | --- | --- |
| Parte, traccia | intero `0..15` | ID logico/API zero-based; etichetta utente = ID + 1. Relazione 1:1: la parte N e' la configurazione sonora della traccia N. |
| Canale MIDI (`input.channel`, `destination.channel`) | intero `0..15` | Campo separato dall'ID della traccia; etichetta utente 1-16. |
| `channel` nelle route DAW esistenti (`/api/daw/track/<ch>/...`, body `channel`) | intero `0..15` | **E' l'ID della traccia**, non un canale MIDI (nome storico conservato per compatibilita'). |
| Clip | intero `>= 1` | Unico nel progetto (contatore globale `next_clip_id`, non per traccia), mai riusato nello stesso progetto (sez. 6.4). |
| Scena | stringa, regex Python `^[\w-]{1,64}$` (Unicode) | Nuove scene: `s-` + 12 cifre esadecimali minuscole (es. `s-3fa91c0b7d22`). Scene legacy: lo *stem* del nome file (es. `legacy_decay`), vedi 4.2. |
| Progetto | stringa, stessa regex | Nuovi progetti: `p-` + 12 cifre esadecimali minuscole. |
| Alias dispositivo | stringa, regex `^[a-z0-9][a-z0-9-]{0,31}$` | Scelto/derivato dall'etichetta utente, unico nel registro dispositivi (sez. 3.4). |

Gli ID sono opachi per il client: NON DEVE costruirli ne' derivarne significato; nelle URL li codifica con `encodeURIComponent`.

### 1.2 Tempo musicale

- Le posizioni sono in **beat** (`float`, `>= 0`); **un beat = una semiminima** (quarto). E' gia' cosi' oggi: `daw_recorder.py:_record_loop` salva `beat = secondi / beat_duration`.
- BPM unico globale (`30..300`, `daw_recorder.py:set_bpm` fa clamp), metro `beats_per_measure` in `{3, 4}` su denominatore 4. Nessuna tempo map.
- Secondi = `beat * 60 / bpm`. Il campo `timeline_position` di `get_state`/`daw_tick` e' in **secondi** (oggi); la posizione in beat si calcola lato client con la formula, non esiste un campo `current_beat` (vedi anomalia A8).
- Visualizzazione: `battuta.beat.sedicesimo`, con `battuta = floor(b / bpm_misura) + 1`, `beat = floor(b mod bpm_misura) + 1`, `sedicesimo = floor(frac(b) * 4) + 1`; secondi nel dettaglio.
- I beat NON DEVONO essere arrotondati quando si salvano o si spostano dati (il `repr` dei float Python fa round-trip esatto). L'arrotondamento a 4 decimali esiste solo nelle risposte di visualizzazione (`get_full_note_map`, `get_events_decoded`) e NON DEVE essere riusato come valore di scrittura senza snap esplicito.

### 1.3 Snap e griglie (centralizzazione delle etichette)

Valori in beat, etichette in valori di nota. Tabella unica, DEVE vivere in un solo modulo client (`static/js/units.js`, owner `ui-shell`) e in nessun altro posto:

| Valore (beat) | Etichetta UI | Note |
| --- | --- | --- |
| `0` | Libero | nessuno snap |
| `1` | 1/4 | semiminima |
| `0.5` | 1/8 | croma |
| `0.25` | 1/16 | |
| `0.125` | 1/32 | |
| `0.0625` | 1/64 | |

Migrazione etichette (anomalia A12): oggi `index.html` `#daw-snap-grid` e `#pr-snap-grid` hanno gli stessi `value` ma etichette "frazione di beat" (`1`, `1/2`, `1/4`, `1/8`, `1/16`), mentre `#quant-grid` usa gia' le etichette corrette. Solo le etichette cambiano: i valori restano in beat, nessun dato salvato contiene lo snap (non c'e' persistenza oggi, nessun `localStorage`), quindi **nessuna migrazione dati**. Le griglie di quantizzazione valide restano `{1, 0.5, 0.25, 0.125, 0.0625}` (`app.py:daw_quantize`).

### 1.4 JSON e file

- Codifica UTF-8; file su disco scritti compatti (`separators=(',', ':')`, `ensure_ascii=False`, `allow_nan=False`). Le risposte HTTP restano quelle di `jsonify`.
- Le mappe per traccia nello stato DAW esistente (`armed`, `muted`, `clips`, ...) hanno chiavi stringa `"0".."15"` in JSON (effetto di `jsonify` su chiavi int): il client DEVE accettarle cosi'. I nuovi documenti (scena, progetto) usano **array di 16 elementi** con indice = ID.
- Orari: stringhe `"YYYY-MM-DD HH:MM:SS"` ora locale del Pi (stesso formato dei preset legacy). Il Pi non ha RTC: gli orari sono informativi e NON DEVONO guidare logica (il controllo "file cambiato" usa `st_mtime_ns` + `st_size`, sez. 5.6).

---

## 2. Formato delle risposte, errori, ack e timeout

### 2.1 Busta standard (superset compatibile)

`index.html` oggi controlla `data.status === 'ok'` / `ack.status` e legge `data.message` in circa 40 punti. La busta nuova e' quindi un **superset** di quella attuale.

Successo:

```json
{"ok": true, "status": "ok", "message": "Scena richiamata", "revision": 1700000001234, "warnings": []}
```

- `ok: true` e `status: "ok"` DEVONO essere entrambi presenti in ogni risposta JSON nuova.
- Il payload sta **al primo livello** sotto chiavi con nome (`part`, `parts`, `scene`, `project`, `clips`, ...), come gia' fanno quasi tutte le route attuali. Eccezione storica conservata: `GET /api/daw/state` mette lo stato sotto `data`.
- `message` (italiano, per l'utente) e' facoltativo nei successi.
- `warnings` facoltativo: lista di `{code, message, details}` (codici in 2.4). Un successo con warning DEVE essere mostrato come tale, non come successo pieno.
- Le route DAW che mutano contenuto DEVONO includere `revision` (sez. 6.6).

Errore:

```json
{"ok": false, "status": "error", "message": "Revisione obsoleta: aggiorna e riprova",
 "error": {"code": "REVISION_CONFLICT", "message": "Revisione obsoleta: aggiorna e riprova",
           "details": {"expected_revision": 41, "current_revision": 43}}}
```

- `ok: false`, `status: "error"`, `message` (= `error.message`) e `error: {code, message, details}` DEVONO essere presenti. `details` e' un oggetto (eventualmente `{}`), mai `null`.
- `code` e' un valore della tabella 2.3, in MAIUSCOLO_CON_UNDERSCORE, stabile: il client decide in base a `code`, mai al testo.
- Per gli errori di validazione `details` DEVE contenere `path` (percorso nel documento, es. `parts[3].cc.75`) e `reason`.

Regole di compatibilita':

1. Le route **nuove** DEVONO usare la busta completa.
2. Le route **esistenti** toccate da una fase DEVONO aggiungere `ok` e (sugli errori) `error`, conservando `status`, `message`, i campi di payload attuali e **lo stesso codice HTTP** per le condizioni d'errore gia' esistenti (es. "nessun canale armato" resta 400). Le condizioni d'errore nuove usano i codici di 2.2.
3. Route legacy a testo semplice (`/select_prog`, `/cc`, `/set_effect`, `/reset_channel`, `/load_sf2`) e route che rispondono con un array nudo (`/list_sf2`, `/get_instruments`, `/api/presets/list`, `/api/daw/midi/list`) restano **invariate** finche' esiste un chiamante legacy; i nuovi client usano gli equivalenti JSON di sez. 8. Le route nuove NON DEVONO mai rispondere con array nudi o testo.
4. `static/js/api.js` (owner `ui-shell`) normalizza: esito = `HTTP 2xx` **e** (`body.ok === true` oppure, se `ok` manca, `body.status === 'ok'`, oppure body array/testo per le route legacy elencate al punto 3). Un HTTP non-2xx e' sempre un errore anche se il body e' illeggibile.

### 2.2 Codici HTTP

| HTTP | Quando |
| --- | --- |
| 200 | Successo (anche con `warnings`). |
| 201 | Creazione di una risorsa con ID nuovo (scena, progetto, dispositivo). |
| 202 | Lavoro avviato ma non concluso (solo `POST /api/soundfont/load`): l'esito arriva da stato/evento. |
| 400 | Richiesta malformata: JSON illeggibile, tipo errato, campo obbligatorio mancante, valore fuori intervallo di un parametro. |
| 404 | Risorsa inesistente (scena, progetto, clip, file MIDI/SF2, alias). |
| 409 | Conflitto con lo stato corrente: trasporto attivo, revisione obsoleta, modifiche non salvate, nome esistente, sovrapposizione clip, conferma richiesta. Lo stato del server e' invariato. |
| 413 | File caricato oltre il limite (progetto 2 MiB, sez. 5.8; MIDI 5 MiB come oggi). |
| 422 | Documento (scena, progetto, file MIDI) leggibile ma semanticamente non valido o di versione non supportata. Stato invariato. |
| 500 | Errore interno o di disco (`INTERNAL`, `DISK_ERROR`, `DISK_FULL`). |
| 503 | Dipendenza non disponibile: FluidSynth (console 9800 o porta MIDI interna), porta MIDI esterna, oppure applicazione interrotta a meta' (`PARTIAL_APPLY`, con `details.committed`). |

### 2.3 Codici d'errore (`error.code`)

| Codice | HTTP | Significato / quando |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | Vedi 2.2. `details.field`. |
| `NOTHING_TO_UNDO` | 400 | Undo/redo con pila vuota (HTTP 400 conservato da `app.py:daw_undo`). |
| `NO_ARMED_TRACK` | 400 | Record senza tracce armate (HTTP 400 conservato da `app.py:daw_start_recording`). |
| `NOTHING_TO_PLAY` | 400 | Play senza dati o gia' in Play (HTTP 400 conservato da `app.py:daw_start_playback`). |
| `NOT_FOUND` | 404 | `details.kind` in `scene`, `project`, `clip`, `midi_file`, `soundfont`, `device`. |
| `NAME_EXISTS` | 409 | Nome scena/progetto gia' usato (confronto case-insensitive). `details.existing_id`. |
| `TRANSPORT_ACTIVE` | 409 | Serve trasporto fermo (Play o Record attivi). `details.is_playing`, `details.is_recording`. |
| `RECORDING_ACTIVE` | 409 | Operazione vietata solo durante Record (editing indicizzato, cambio input, seek). |
| `REVISION_CONFLICT` | 409 | `expected_revision` diversa dalla corrente (sez. 6.6). |
| `UNSAVED_CHANGES` | 409 | Progetto o scena modificati e richiesta senza `discard_changes: true`. `details.scope` in `project`, `scene`. |
| `SOUNDFONT_CHANGE_REQUIRED` | 409 | La scena/progetto richiede un SoundFont diverso da quello caricato. `details.required`, `details.loaded`. |
| `SOUNDFONT_MISSING` | 409 | Il SoundFont richiesto non esiste in `SF2_DIR`. `details.required`. |
| `SOUNDFONT_LOADING` | 409 | Un caricamento SoundFont e' in corso. |
| `PORTS_UNAVAILABLE` | 409 | Preflight: parti con porta `missing`/`ambiguous`/`unbound`/`offline` e richiesta senza `allow_partial: true`. `details.parts: [{part_id, alias, direction, status}]`. |
| `CLIP_OVERLAP` | 409 | Riservato: con l'ownership per ID (D3, sez. 6.4) le operazioni su clip non lo producono piu'. |
| `CLIP_RESIZE_WOULD_EXCLUDE` | 409 | Ridimensionamento che lascerebbe fuori eventi posseduti. `details.min_end_beat`. |
| `DEST_NOT_EMPTY` | 409 | Duplicazione su traccia con eventi senza `confirm_overwrite: true`. |
| `FILE_CHANGED` | 409 | Salva: il file su disco e' cambiato dall'apertura/ultimo salvataggio. Riprovare con `overwrite: true`. |
| `NO_CURRENT_FILE` | 409 | Salva senza progetto su file: usare Salva come. |
| `DEVICE_IN_USE` | 409 | Eliminazione alias usato da parti. `details.parts`. |
| `EMPTY_CLIPBOARD` | 409 | Incolla con appunti vuoti. |
| `NOT_INTERNAL_PART` | 409 | Comando solo-FluidSynth (route legacy `/select_prog`, `/reset_channel`) su parte esterna. |
| `VALIDATION_FAILED` | 422 | `details.path`, `details.reason`. |
| `UNSUPPORTED_VERSION` | 422 | `schema_version` superiore a quella supportata o formato sconosciuto. `details.found`, `details.supported`. |
| `IMPORT_NEEDS_CONVERSION` | 422 | SMF con tempo map, metro non 3/4-4/4 o metri multipli, senza `convert: true`. `details.issues`. |
| `PAYLOAD_TOO_LARGE` | 413 | `details.limit_bytes`. |
| `SYNTH_UNAVAILABLE` | 503 | Console FluidSynth non raggiungibile o porta MIDI interna assente. |
| `PORT_UNAVAILABLE` | 503 | Record senza input utilizzabile, Play senza alcuna destinazione disponibile, invio su porta assente. `details.tracks`/`details.alias`. |
| `PARTIAL_APPLY` | 503 | Sequenza interrotta a meta' (es. FluidSynth caduto durante un richiamo). `details.committed` (bool: la parte non sonora e' stata applicata?), `details.applied_parts`, `details.failed_parts`. MAI presentato come successo. |
| `DISK_FULL` | 500 | `ENOSPC` in scrittura; file precedente intatto. |
| `DISK_ERROR` | 500 | Altro errore I/O; file precedente intatto. |
| `INTERNAL` | 500 | Eccezione non prevista. |

**HTTP legacy vs tabella.** La colonna HTTP vale per le route nuove e per le condizioni d'errore nuove. Sulle route **esistenti** una condizione d'errore che oggi ha gia' un codice HTTP lo conserva (regola 2.1.2) anche se la tabella ne indica un altro: `api_common.err(code, message, details=None, http=None)` usa la tabella se `http` e' `None` e accetta un override esplicito, usato SOLO per gli override elencati in 8.1 ("Override HTTP legacy"). I client DEVONO decidere su `error.code`, mai sul solo codice HTTP (che serve solo a distinguere successo/errore).

### 2.4 Codici di warning (`warnings[].code`)

`SOUNDFONT_UNSPECIFIED` (scena senza SoundFont: applicata sul SoundFont caricato), `PART_UNAVAILABLE` (parte saltata con consenso `allow_partial`), `SHARED_DESTINATION`, `SHARED_INPUT`, `DOUBLE_MONITOR` (sez. 3.6), `MONITOR_HANDLED_BY_ALSA` (modalita' legacy, sez. 10), `UNPAIRED_NOTE_ON` (note-on senza note-off nel documento), `CLIP_OVERLAP_PRESENT` (progetto aperto con clip sovrapposti ed eventi senza `clip_id`: assegnati con la migrazione di 6.4), `BANK_NOT_REPRESENTABLE` (export SMF, sez. 7.3), `IMPORT_DROPPED_EVENTS` (SysEx/realtime scartati all'import, con conteggi), `IMPORT_CONVERTED` (import con `convert: true`), `PLAYBACK_TRACKS_UNAVAILABLE` (Play avviato ma alcune tracce hanno destinazione non disponibile), `LEGACY_FIELD_DEFAULTED` (scena legacy con campi assenti riempiti dai default, sez. 4.3).

### 2.5 Socket.IO: ack

- Gli eventi client->server esistenti (`cc_update`, `effect_update`, `transport_cmd`) rispondono con un ack che DEVE seguire la stessa busta di 2.1 (oggi `{status, message?}`: aggiungere `ok`/`error`, conservando `status`/`message`).
- Il client DEVE passare sempre una callback d'ack per i comandi e trattare l'assenza di ack entro il timeout come **esito sconosciuto**.
- Fallback HTTP (gia' presente in `index.html`: se `dawSocket.connected` e' falso usa la route HTTP equivalente) DEVE restare: un comando NON DEVE essere inviato su entrambi i canali.

### 2.6 Timeout e comportamento del client

| Tipo di richiesta | Timeout client |
| --- | --- |
| Ack Socket.IO | 3 s |
| HTTP ordinario | 8 s |
| Apri/Salva/Salva come/upload progetto, richiamo scena, avvio caricamento SoundFont, import MIDI | 30 s |

- **Timeout non significa successo.** Allo scadere il client DEVE mostrare "Esito non confermato", rileggere lo stato autoritativo (`GET /api/daw/state`, `GET /api/parts`, `GET /api/projects/current`, `GET /api/soundfont/status` secondo il dominio) e NON DEVE ripetere automaticamente operazioni non idempotenti (incolla, taglia, aggiungi nota, salva come).
- Ogni azione nuova DEVE attendere l'esito (HTTP o ack) prima di aggiornare l'interfaccia come riuscita; durante l'attesa il controllo e' disabilitato o mostra uno stato "in corso".
- Le richieste di editing DAW dello stesso client DEVONO essere serializzate (una alla volta): ogni risposta porta la nuova `revision` da usare nella successiva (sez. 6.6).
- Su `connect` (anche riconnessione) il client DEVE rileggere lo snapshot completo (stato DAW, parti, progetto, scena, SoundFont, porte) prima di riabilitare i comandi.

---

## 3. Parte e routing

Owner implementazione: `ui-routing` (modulo nuovo `midi_router.py` + route in `app.py`). Consumatori: `ui-persistence` (scene/progetti includono le parti), `ui-live`/`ui-studio` (pannello sonoro e inspector).

### 3.1 Schema della parte (configurazione persistente)

Una parte e' un oggetto con **esattamente** queste chiavi (chiavi extra -> `VALIDATION_FAILED`):

| Campo | Tipo | Regole |
| --- | --- | --- |
| `part_id` | int `0..15` | Uguale all'indice nell'array delle 16 parti. |
| `input` | oggetto | `{device_alias, channel}`. |
| `input.device_alias` | stringa alias o `null` | `null` = **nessun ingresso**: la traccia non riceve eventi live, non registra (Record la segnala, sez. 3.7) e non c'e' monitor. NON significa "tutti gli ingressi" ne' "ingresso predefinito". |
| `input.channel` | int `0..15` o `null` | `null` = tutti i canali di quell'ingresso: ogni traccia che corrisponde riceve gli eventi (l'UI DEVE dirlo esplicitamente). |
| `destination` | oggetto | `{kind, device_alias, channel}`. |
| `destination.kind` | `"internal"` \| `"external"` | |
| `destination.device_alias` | `null` se `internal`; alias stringa se `external` | |
| `destination.channel` | int `0..15` | Canale MIDI di uscita; gli eventi di playback/monitor/preview vengono rimappati su questo canale. |
| `monitor` | bool | Inoltro live dell'ingresso alla destinazione (sez. 3.5). |
| `sound` | oggetto, forma dipendente da `destination.kind` | Interno: `{bank, program, name}`; esterno: `{bank_msb, bank_lsb, program, name}`. |
| `sound.bank` (interno) | int `0..16383` | **Numero di bank FluidSynth** cosi' come lo usa la console (`select <ch> <sf_id> <bank> <prog>`, `app.py:select_prog`): tipicamente `0`, `128` = percussioni. NON e' una coppia MSB/LSB. |
| `sound.program` (interno) | int `0..127` | |
| `sound.bank_msb`, `sound.bank_lsb`, `sound.program` (esterno) | int `0..127` o `null` | `null` = non inviare quel messaggio. Nessuna conversione automatica da bank SoundFont a bank Roland. |
| `sound.name` | stringa `<= 64` o `null` | Interno: nome dello strumento FluidSynth (informativo, es. da `inst`). Esterno: nome assegnato dall'utente; NON DEVE essere un nome FluidSynth. |
| `cc` | oggetto | Chiavi stringa in `{"7","10","71","72","73","74","75"}`, valori int `0..127`. Interno: tutte e 7 le chiavi OBBLIGATORIE. Esterno: sottoinsieme qualsiasi (chiave assente = non inviato). |

Nomi CC (unico mapping, uguale a `app.py:control`/`handle_cc_update`): `7` volume, `10` pan, `71` resonance, `72` release, `73` attack, `74` cutoff, `75` decay. Nessun controllo "sustain" dell'inviluppo: l'UI NON DEVE chiamarlo "ADSR completo".

Esempi validi: `docs/schemas/part_internal.json`, `docs/schemas/part_external.json`. Esempi non validi con codice atteso: `docs/schemas/invalid_cases.json`.

### 3.2 Default (anche per la migrazione)

Parte N di default (= comportamento di oggi: input canale N -> FluidSynth canale N, monitor interno via ALSA):

```json
{"part_id": 0,
 "input": {"device_alias": "keyboard-main", "channel": 0},
 "destination": {"kind": "internal", "device_alias": null, "channel": 0},
 "monitor": true,
 "sound": {"bank": 0, "program": 0, "name": null},
 "cc": {"7": 100, "10": 64, "71": 64, "72": 64, "73": 64, "74": 64, "75": 64}}
```

- Unica eccezione: la parte 9 (canale 10) ha `sound.bank = 128` (batteria interna suggerita, non imposta agli esterni).
- `keyboard-main` e' l'alias creato al primo avvio dal bootstrap del registro (3.4).
- Default di una parte che passa a `external` senza `sound`/`cc` nella stessa richiesta: `sound = {bank_msb: null, bank_lsb: null, program: null, name: null}`, `cc = {}`, **`monitor = false`** salvo `monitor: true` esplicito nella stessa richiesta (piano sez. 7: esterno con monitor inizialmente spento). Ritorno a `internal`: `sound`/`cc` = default sopra (con bank 128 se `destination.channel == 9`), monitor invariato.

### 3.3 Dove vive lo stato delle parti

- Stato corrente: `last_state.json`, chiave nuova **`parts`** (array di 16 parti, schema 3.1), scritta con il writer debounced/atomico esistente (`app.py:save_state`/`_schedule_state_flush`, sotto `STATE_LOCK`).
- La chiave legacy `channels` DEVE restare aggiornata per le parti interne (`bank`, `program`, `volume`, `pan`, `attack`, `release`, `decay`, `cutoff`, `resonance`) finche' esistono chiamanti legacy (`restore_settings`, `api_capture_current_config`, `daw_midi_save`, `presets.html`). Unico punto di scrittura: una funzione `save_part(part)` in `app.py` (owner `ui-routing`) che aggiorna entrambe nella stessa scrittura.
- Avvio: se `parts` esiste la si usa; altrimenti la si costruisce da `channels` con la stessa mappa di 4.3 (campi assenti = default 3.2) e routing di default. `restore_settings` applica le parti interne via console come oggi e le esterne via router se la porta e' disponibile.

### 3.4 Registro dispositivi, alias e descrittori porta

File `home/matteo/matsynth_web/midi_devices.json` (scrittura atomica, sez. 5.5; esempio `docs/schemas/midi_devices.json`):

```json
{"schema_version": 1,
 "devices": [
  {"alias": "keyboard-main", "label": "Tastiera principale",
   "input": {"client": "FANTOM-08", "port": "FANTOM-08 MIDI 1"},
   "output": null, "controls": null},
  {"alias": "fantom", "label": "FANTOM-08",
   "input": {"client": "FANTOM-08", "port": "FANTOM-08 MIDI 1"},
   "output": {"client": "FANTOM-08", "port": "FANTOM-08 MIDI 1"},
   "controls": ["7", "10", "74", "71"]}]}
```

- Un alias rappresenta uno strumento; `input`/`output` sono **descrittori** (o `null` = non associato in quella direzione). `controls` = CC sonori dichiarati supportati dall'esterno (chiavi di 3.1); `null` = nessuno dichiarato: l'UI mostra solo l'editor eventi generico (piano sez. 5).
- **Descrittore** = nome porta normalizzato, senza dati runtime. Da un nome mido/rtmidi ALSA (forma attesa `"<client>:<porta> <client_id>:<port_id>"`, es. `"FANTOM-08:FANTOM-08 MIDI 1 20:0"`; FluidSynth: `"FLUID Synth (1234):Synth input port (1234:0) 128:0"`):
  1. togliere il suffisso finale `\s\d+:\d+$` (numero client/porta ALSA, runtime);
  2. togliere ogni `\s?\(\d+(:\d+)?\)` (PID, runtime);
  3. dividere al primo `:` in `client` e `port` (trim). Senza `:` -> `client` = nome intero, `port` = `""`.
  La forma esatta dei nomi sul Pi e' **da verificare su hardware** (`ui-routing` DEVE registrarla nell'handoff); la funzione di normalizzazione DEVE essere una sola (`midi_router.normalize_port_name`) e coperta da test con i nomi di esempio sopra.
- Numero client ALSA, PID e nome runtime completo sono solo dati runtime, MAI persistiti.
- **Bootstrap** (solo se `midi_devices.json` non esiste): crea `keyboard-main` con `input` = descrittore della porta che la selezione legacy (`daw_recorder.py:_init_midi_ports`, parole chiave FANTOM > SINCO > USB, escluso Midi Through) sceglierebbe **se e solo se il candidato e' unico** per la parola chiave vincente; altrimenti `input: null` (stato `unbound`, l'utente associa in Impostazioni). Le parole chiave non vengono piu' usate dopo il bootstrap.
- Porte escluse dalle proposte normali (restano visibili con `?all=1`, con motivo): client `System`, `Midi Through`, client che iniziano con `RtMidi` (porte proprie di mido/rtmidi), e, tra gli input, `FLUID Synth`. In uscita `FLUID Synth` e' marcata `internal: true` e non e' proposta come esterna.
- **Destinazione interna**: risolta dal router come l'unica porta di uscita il cui `client` (dopo normalizzazione) **inizia con** `"FLUID Synth"`. Non si usa piu' `'128:0' in nome` (`_init_midi_ports` oggi). Zero porte -> interno `missing`; piu' di una -> `ambiguous` (nessuna scelta automatica).

### 3.5 Stati porta, monitor, invio

Stato di un alias in una direzione (`input`/`output`), calcolato a runtime dal router:

| Stato | Significato | Etichetta UI |
| --- | --- | --- |
| `available` | esattamente una porta reale corrisponde al descrittore, ed e' aperta | disponibile |
| `missing` | nessuna porta corrisponde | assente |
| `ambiguous` | piu' porte corrispondono (due strumenti identici); serve una scelta esplicita (`POST /api/midi/devices/<alias>/select`), valida solo fino alla prossima riconnessione | ambigua |
| `unbound` | descrittore `null` in quella direzione | non associato |
| `offline` | la porta corrispondeva ma apertura/invio sono falliti; errore registrato ed emesso (`midi_error`) | errore |

- Nessun fallback: con stato diverso da `available` il router NON DEVE inviare/leggere su un'altra porta.
- Un solo lettore per input aperto, attivo anche fuori da Record (svuota la coda di continuo); il recorder si aggancia al flusso del router per le tracce armate. NON DEVE esistere un secondo consumatore `iter_pending()` della stessa porta (oggi `start_recording` e `_record_loop` leggono direttamente `self.midi_input`).
- Polling porte: il router DOVREBBE rileggere l'elenco porte ogni 2 s (solo enumerazione, nessun I/O MIDI) ed emettere `ports_status` **solo se cambia**. Al ritorno di una porta, prima di nuovi invii, DEVE inviare il cleanup (note-off tracciati + CC64=0 + CC123) sulle rotte MatSynth di quella porta e mostrare lo stato di ripristino.
- **Monitor** (`managed`): ogni evento canale ricevuto da un input disponibile viene confrontato con le parti (`input.device_alias` uguale e `input.channel` uguale o `null`); per ogni parte con `monitor: true` viene inviato **una volta** alla destinazione, con il canale rimappato su `destination.channel`. Monitor e registrazione sono indipendenti: si registra anche con monitor spento; mute/solo NON influenzano monitor ne' registrazione. Nessun invio di Local Control. Nessun inoltro di SysEx, clock o realtime.
- **Monitor in `legacy`**: il router NON DEVE inoltrare nulla live (il monitoring interno e' il collegamento ALSA diretto: `midi.autoconnect=1` e blocco `aconnect` di `startfluid.sh`). Il campo `monitor` e' conservato ma inefficace: runtime `monitor_effective: false`, warning `MONITOR_HANDLED_BY_ALSA`. In legacy un input autoconnesso suona comunque su FluidSynth sul suo canale originale, qualunque sia la destinazione della parte: l'UI DEVE dirlo per le parti esterne o con `input.channel != destination.channel`.
- Playback, preview note, CC/bank/program e monitor DEVONO passare dalla **stessa** risoluzione della destinazione (`midi_router`). Porte condivise tra tracce, una apertura per porta, invii serializzati (un lock per porta, mai tenuto durante attese di rete o caricamento SF2). Nessun thread per nota o per traccia.
- I comandi sonori interni (bank/program/CC) continuano a passare dalla console 9800 come oggi oppure dalla porta MIDI interna: `ui-routing` sceglie e documenta UNA via per tipo di comando. In entrambi i casi l'esito e' "inviato", non "confermato" (`app.py:send_fluid` per `set/select/cc` ritorna `"OK"` senza leggere risposta; stringa vuota = errore di socket).
- Il router traccia le note inviate per `(porta, canale, nota)`; il note-off DEVE raggiungere la destinazione originale anche se il routing e' cambiato nel frattempo.
- **Click**: destinazione logica interna fissa (FluidSynth, canale 10 = `9`, note 76/77, velocity 100, come `daw_recorder.py:_metronome_loop`); mai registrato, mai inviato a un esterno, nessun cambio program sul canale batteria. Interno indisponibile -> click indisponibile (`click: "missing"` in `/api/system/status`), nessun fallback.
- Interno indisponibile NON impedisce tracce esclusivamente esterne.

### 3.6 Conflitti (avvisi, non errori)

Calcolati su ogni `GET /api/parts` e nella risposta di `PATCH /api/parts/<id>`, lista `conflicts`:

| `type` | Condizione | Significato per l'utente |
| --- | --- | --- |
| `shared_destination` | >= 2 parti con stessa (`kind`, `device_alias`, `channel`) | condividono suono e CC sullo strumento: cambiare l'una cambia l'altra; nessun isolamento audio. |
| `shared_input` | >= 2 parti con stesso `input.device_alias` e canali che si intersecano (uguali o uno `null`) | ogni evento arriva a tutte: se armate registrano entrambe. |
| `double_monitor` | `shared_input` + stessa destinazione + entrambe `monitor: true` | ogni nota suona due volte sulla stessa destinazione. |

Formato: `{"type": "shared_destination", "parts": [1, 4], "destination": {...}}`. Il backend NON DEVE risolvere i conflitti da solo.

### 3.7 Regole di trasporto legate al routing

- **Record** (`daw_recorder.py:start_recording` + route): DEVE fallire **prima** di impostare `is_recording` se una qualsiasi traccia armata non ha un input utilizzabile (`device_alias` `null` o stato diverso da `available`): `503 PORT_UNAVAILABLE`, `details.tracks: [{track, alias, status}]`. Corregge l'anomalia A3 (oggi `is_recording=True` fittizio). Scelta piu' severa del minimo del piano (vedi D7).
- La traccia N registra gli eventi che corrispondono all'input della **parte N** (alias + canale o `null`), non piu' "canale MIDI == N" (`_record_loop` oggi filtra `msg.channel in recording_channels`; con il routing di default il risultato e' identico).
- Record con Play gia' attivo: `409 TRANSPORT_ACTIVE` (oggi parte un secondo thread di playback: anomalia A23, evidenza statica).
- **Play**: se nessuna traccia da suonare ha destinazione disponibile -> `503 PORT_UNAVAILABLE` e `is_playing` resta `false` (anomalia A3). Se solo alcune: avvio con warning `PLAYBACK_TRACKS_UNAVAILABLE` (`details.tracks`); quegli eventi non vengono inviati.
- **Ritorno al punto di partenza dopo Record (decisione utente, D6)**: per poter registrare altri strumenti sullo stesso intervallo, dopo lo Stop di una registrazione la posizione di trasporto DEVE tornare al punto in cui il Record e' iniziato. Regole: (a) vale SOLO dopo uno Stop di Record (`record_stop`, `stop_all` in Record, errore che interrompe il Record); dopo un semplice Play, Pausa o Stop di Play la posizione resta dov'e' (nessun rewind implicito); (b) lo implementa il **backend** (non il client): all'avvio del Record il DAW memorizza `record_start_beat` (in beat, per restare corretto anche se cambia il BPM) e allo stop imposta `timeline_position` a quel punto, poi emette un solo `daw_state_update`; il client NON deve piu' fare calcoli con `recordStartPosition`; (c) e' un'opzione, ATTIVA di default: parametro `return_to_start` (bool, default `true`) di `record_start` (socket e `POST /api/daw/record/start`); se `false` la posizione resta dove il Record e' stato fermato; la UI Studio espone l'interruttore "Torna all'inizio dopo Record" e ne conserva il valore nelle preferenze (`prefs`, sez. 9) senza stato persistente lato backend; (d) i dati registrati e le clip non cambiano; il ritorno e' solo di posizione. Il backend espone `record_start_beat` (o `null`) in `daw_state_update` finche' il Record e' attivo e fino al ritorno, per diagnostica. Implementazione: `ui-studio` (anomalia A24, corretta cosi'), con test di unita' su `stop_recording`.
- **Stop di Record**: le note ancora aperte (note-on senza note-off) ricevono un note-off al beat di stop, e un CC64 >= 64 non chiuso riceve CC64=0 al beat di stop, prima del sort e della creazione clip. Cosi' nessun take resta con note pendenti.
- Stop, pausa, seek, fine/ripartenza loop, mute/solo durante il playback (che oggi agiscono solo al giro successivo perche' `_collect_playback_events` fotografa gli eventi a inizio giro: anomalia A25, evidenza statica), cambio routing di una parte, PANIC: DEVONO rilasciare note e sustain interessati (note-off tracciati + CC64=0 sui canali coinvolti + CC123).
- **PANIC**: su tutte le destinazioni in uso (interna + esterne aperte), tutti i 16 canali: note-off tracciati, CC64=0, CC123, CC120 (inviati sempre: "dove supportati" non e' rilevabile). Oggi solo CC123 sull'uscita FluidSynth (anomalia A6).
- Cambio `input` di una parte durante Record: `409 RECORDING_ACTIVE`. Cambio destinazione/suono/CC: sempre consentito (con rilascio note sulla vecchia destinazione).
- Inviare MIDI a uno strumento esterno NON conferma il suo stato sonoro: le risposte riportano `delivery: "sent"` o `"failed"`, mai "verificato".

---

## 4. Scena versionata e migrazione dei preset legacy

Owner: `ui-persistence` (modulo nuovo `project_store.py`: schema, validazione, I/O atomico, adattatori legacy; route in `app.py`). Consumatore UI: `ui-live`.

### 4.1 Schema v1

Chiavi esatte (extra -> `VALIDATION_FAILED`):

| Campo | Tipo | Regole |
| --- | --- | --- |
| `format` | `"matsynth.scene"` | Assente o diverso -> non e' una scena v1 (vedi 4.2 per il rilevamento legacy). |
| `schema_version` | int | Supportata: `1`. Maggiore -> `422 UNSUPPORTED_VERSION`, file non toccato. |
| `id` | stringa ID (1.1) | Stabile: non cambia con rinomina. |
| `name` | stringa `1..64` | Unica tra le scene (case-insensitive). |
| `created`, `modified` | stringa orario (1.4) | `created` legacy conservato verbatim (anche `"Unknown"`). |
| `soundfont` | `{"file": "<nome>.sf2"}` o `null` | `file` = nome base in `SF2_DIR` (`/usr/share/sounds/sf2/`), niente separatori di percorso. `null` = scena senza SoundFont richiesto. |
| `globals` | `{gain, reverb_level, chorus_level}` | `gain` 0..10, `reverb_level` 0..1, `chorus_level` 0..10 (limiti FluidSynth; l'UI attuale usa 0..3 / 0..1 / 0..1). Solo FX **interni**: NON controllano l'audio degli esterni. |
| `parts` | array di esattamente 16 parti (3.1) | `parts[i].part_id == i`. Routing incluso. |

Favoriti e ordine NON stanno nella scena: file `home/matteo/matsynth_web/library.json` `{"schema_version":1,"scene_favorites":[id...],"scene_order":[id...]}` (owner `ui-persistence`, scrittura atomica). Fuori da `PRESETS_DIR` per non comparire nell'elenco legacy (`app.py:api_presets_list` elenca ogni `*.json` di `PRESETS_DIR`).

Esempio completo: `docs/schemas/scene_v1.json`.

### 4.2 File, ID e coesistenza con i preset legacy

- Le scene v1 stanno in `PRESETS_DIR` (`/home/matteo/matsynth_web/presets/`) come `<id>.json`. Una sola libreria: le API legacy `/api/presets/*` e le nuove `/api/scenes/*` leggono la stessa cartella.
- **Rilevamento**: file con `format == "matsynth.scene"` -> v1; file senza `format` con almeno una tra `channels`/`global_settings`/`font` -> **legacy**; altro -> elencato con `valid: false`, `error.code = VALIDATION_FAILED`.
- ID di un file legacy = stem del nome file (`legacy_decay.json` -> `legacy_decay`). Stem che non rispetta la regex 1.1 -> `valid: false` (resta usabile dalla pagina legacy).
- La lettura NON DEVE mai riscrivere un file legacy. La migrazione avviene in memoria a ogni lettura (funzione pura `migrate_legacy_preset(data, file_stem) -> scene_v1`).
- **Salva** (PUT) su una scena legacy: prima copia il file originale in `PRESETS_DIR/.legacy_backup/<nome file>` (solo se la copia non esiste gia'), poi scrive la v1 **nello stesso file** (stesso ID = stem). `.legacy_backup` e' una cartella: `api_presets_list` la ignora (filtra `endswith('.json')`).
- Rinomina (PATCH `name`) cambia solo `name` nel file: ID e nome file restano. Corregge per le scene nuove la sovrascrittura silenziosa di `app.py:api_presets_rename` (anomalia A10b); la route legacy di rinomina DEVE comunque rispondere `409 NAME_EXISTS` invece di sovrascrivere un file esistente.
- Adattatore inverso per la pagina legacy (`presets.html` resta attiva fino a `ui-offline-settings`): `GET /api/presets/load/<file>` su un file v1 DEVE restituire la **forma legacy** (`scene_to_legacy_preset`: `name`, `created`, `font`, `channels[16]` con `channel, bank, program, volume, pan, attack, release, decay, cutoff, resonance, instrument_name` solo per parti interne, `global_settings`); su un file legacy restituisce il contenuto verbatim (test `test_load_returns_file_content`). `api_presets_list` su un file v1 riporta `font = soundfont.file` (o `"N/A"`).

### 4.3 Mappa di migrazione legacy -> v1 (campo per campo)

Forma legacy (da `app.py:api_presets_save` e `api_capture_current_config`, fixture `tests/fixtures/presets/*.json`):
`{name, created, font, channels: [{channel, bank, program, attack, release, decay, cutoff, resonance, volume, instrument_name, pan?}], global_settings: {gain, reverb_level, chorus_level}}`.

| Legacy | v1 | Se assente | Note |
| --- | --- | --- | --- |
| (nome file) | `id` | — | stem del file. |
| `name` | `name` | stem del file | |
| `created` | `created` | `"Unknown"` | verbatim (come `api_presets_list`). |
| (nessuno) | `modified` | = `created` | |
| `font` | `soundfont` | `null` | `""` o assente -> `null`; altrimenti `{"file": font}`. |
| `global_settings.gain` | `globals.gain` | `1.0` | default identici a `api_presets_apply`. |
| `global_settings.reverb_level` | `globals.reverb_level` | `0.4` | |
| `global_settings.chorus_level` | `globals.chorus_level` | `0.4` | |
| `channels[k].channel` | indice della parte | voce ignorata se `null` (come oggi) | valore fuori `0..15` -> `VALIDATION_FAILED`. Duplicati: vince l'ultima voce (equivale all'applicazione sequenziale legacy). |
| `channels[k].bank` | `parts[ch].sound.bank` | `0` (`128` per ch 9) | numero di bank FluidSynth, invariato (`select ch sf bank prog`). |
| `channels[k].program` | `parts[ch].sound.program` | `0` | |
| `channels[k].instrument_name` | `parts[ch].sound.name` | `null` | `"Not Set"` -> `null`. |
| `channels[k].volume` | `parts[ch].cc["7"]` | `100` | |
| `channels[k].pan` | `parts[ch].cc["10"]` | `64` | Oggi mai salvato da `api_capture_current_config` (vedi D1). |
| `channels[k].resonance` | `parts[ch].cc["71"]` | `64` | |
| `channels[k].release` | `parts[ch].cc["72"]` | `64` | |
| `channels[k].attack` | `parts[ch].cc["73"]` | `64` | |
| `channels[k].cutoff` | `parts[ch].cc["74"]` | `64` | |
| `channels[k].decay` | `parts[ch].cc["75"]` | `64` | **CC75 conservato** (non CC72/CC73). |
| (nessuno) | `parts[ch].input/destination/monitor` | default 3.2 | I preset legacy erano solo interni per costruzione. |
| canale non elencato | `parts[ch]` | default 3.2 completa | Vedi D2. |
| chiavi sconosciute | — | ignorate | restano nel file originale/backup. |

Valori non interi o fuori `0..127` (bank fuori `0..16383`, globali fuori limite) -> la scena e' `valid: false` con `VALIDATION_FAILED` e `details.path`; nessun clamp silenzioso. Ogni campo riempito da default produce, nella risposta di `GET /api/scenes/<id>`, un warning `LEGACY_FIELD_DEFAULTED` con `details.paths` (cosi' l'UI puo' segnalare "pan non salvato nel preset originale: verra' centrato").

Esempio eseguibile: `tests/fixtures/presets/legacy_decay.json` migrato = `docs/schemas/scene_from_legacy_decay.json` (verificato dal test).

**La route legacy `POST /api/presets/apply` mantiene la propria semantica** (applica solo i canali elencati e solo i campi presenti: test `test_apply_minimal_uses_defaults_and_no_channel_commands`), con due correzioni assegnate a `ui-persistence`: invia `cc <ch> 10 <pan>` se `pan` e' presente (test `test_DESIRED_apply_pan_sends_cc10`) e persiste lo stato effettivo dei canali applicati in `last_state.json` (`parts` + `channels`). Il SoundFont resta non caricato da quella route (test `test_apply_does_not_load_soundfont`).

### 4.4 Cattura (Salva / Salva come)

- Il backend cattura lo stato **corrente autoritativo** (`last_state.json` `parts`, SoundFont caricato, globali): il client NON invia le parti (diversamente da `presets.html` oggi, che fa `GET /api/capture_current_config` e poi rimanda tutto in `POST /api/presets/save`).
- `soundfont` catturato = SoundFont realmente caricato secondo lo stato SF (4.6), non solo la chiave `font` dello stato.
- Salva come con nome gia' esistente -> `409 NAME_EXISTS` (`details.existing_id`); la sovrascrittura richiede `overwrite_id` esplicito (dopo conferma utente). Nessun suffisso silenzioso, nessuna perdita del nome corrente. (La route legacy `api_presets_save` conserva il suffisso `_2` solo per `presets.html`: vedi D16.)
- Dopo Salva/Salva come la scena salvata diventa la **scena corrente** e `scene_state.dirty = false`.
- `api_capture_current_config` (legacy) DEVE aggiungere `pan` per canale (additivo) cosi' anche i preset salvati dalla pagina legacy conservano il pan.

### 4.5 Preflight e richiamo

`POST /api/scenes/<id>/preflight` (nessun effetto collaterale) e `POST /api/scenes/<id>/recall` (rifa' il preflight, poi applica). Body (tutti facoltativi): `{"discard_changes": false, "allow_partial": false}`.

Ordine dei controlli del preflight (il primo che fallisce determina l'errore):

1. Scena valida (4.1/4.3) -> altrimenti `422 VALIDATION_FAILED`/`UNSUPPORTED_VERSION`.
2. Play o Record attivi -> `409 TRANSPORT_ACTIVE`. **Il backend NON ferma il trasporto**; l'UI mostra la spiegazione e un comando Stop. Il cambio vista resta consentito.
3. Caricamento SoundFont in corso -> `409 SOUNDFONT_LOADING`.
4. Scena corrente modificata (`scene_state.dirty`) e `discard_changes` non vero -> `409 UNSAVED_CHANGES` (`details.scope: "scene"`): l'UI propone Salva / Scarta / Annulla (Salva = salva la scena corrente e riprova; Scarta = riprova con `discard_changes: true`).
5. `soundfont` della scena: `null` -> ok con warning `SOUNDFONT_UNSPECIFIED`; uguale al caricato -> ok; diverso ed esistente in `SF2_DIR` -> `409 SOUNDFONT_CHANGE_REQUIRED` (`details.required`, `details.loaded`); inesistente -> `409 SOUNDFONT_MISSING`. Nessuna sostituzione silenziosa con un altro SoundFont/General MIDI.
6. Porte: per ogni parte esterna (e per l'interno se usato) stato `available`; altrimenti, senza `allow_partial: true` -> `409 PORTS_UNAVAILABLE` con l'elenco. Con `allow_partial: true` le parti indisponibili sono saltate (warning `PART_UNAVAILABLE`), mai reindirizzate altrove.

Risposta del preflight riuscito: `{"ok": true, "status": "ok", "preflight": {"soundfont": {"required": "...", "loaded": "...", "match": true}, "parts": [{"part_id": 0, "action": "apply" | "skip_unavailable", "destination_status": "available"}]}, "warnings": [...]}`.

**Cambio SoundFont = due passi orchestrati dal client** (vedi D9): (a) conferma utente dell'interruzione possibile del suono, `POST /api/soundfont/load`, attesa dello stato `loaded` (4.6); (b) `recall`. Un solo SoundFont globale; nessun precaricamento.

Applicazione (`recall`), in quest'ordine, senza tenere lock durante l'I/O verso FluidSynth o le porte:

1. Per ogni parte il cui routing cambia: rilascio note/sustain sulla vecchia destinazione (3.7).
2. Globali interni (`set synth.gain`, `set synth.reverb.level`, `set synth.chorus.level`).
3. Per ogni parte applicata: interno -> `select <ch> <sf_id> <bank> <program>` + i 7 CC; esterno -> CC0 (`bank_msb` se non `null`), CC32 (`bank_lsb` se non `null`), Program Change (se non `null`), poi i CC presenti; sempre sul `destination.channel`.
4. Persistenza dello **stato effettivo** (parti applicate + parti saltate con la loro configurazione desiderata conservata, globali, scena corrente) in `last_state.json` in UNA scrittura.

Risposta: `200` con `{"recall": {"scene_id": "...", "parts": [{"part_id": 0, "result": "sent" | "skipped_unavailable" | "failed", "delivery": "sent"}]}, "warnings": [...]}` se nessuna parte e' `failed`. Se una qualsiasi parte e' `failed` (es. console 9800 caduta a meta') -> `503 PARTIAL_APPLY` con `details.applied_parts`, `details.failed_parts`, `details.committed: true` (la configurazione desiderata e' stata comunque salvata) e la scena corrente diventa `dirty: true`. Una sequenza parzialmente fallita NON DEVE essere mostrata come riuscita.

Dopo un richiamo riuscito: scena corrente = `id`, `scene_state.dirty = false`, evento `scene_state` + `parts_update`. Il **progetto** diventa `dirty` (il suo snapshot sonoro e' cambiato).

### 4.6 Stato SoundFont

Owner: `ui-persistence`. Stato in memoria (unico job alla volta):

```json
{"state": "loaded", "file": "GeneralUser-GS.sf2", "sf_id": 1, "error": null,
 "started_at": "2026-11-22 10:00:00", "finished_at": "2026-11-22 10:00:07"}
```

- `state` in `idle` (avvio, nessuna verifica ancora), `loading`, `loaded`, `failed`, `unknown` (console 9800 irraggiungibile).
- `loaded` SOLO dopo verifica: l'output di `fonts` (gia' interrogato da `app.py:get_active_sf_id`) contiene il percorso del file richiesto; `sf_id` = ID letto da li'. Nessun ritardo fisso usato come verifica (oggi `send_fluid` per `load` fa `sleep(0.1)` e un solo `recv`).
- Il caricamento gira in un thread dedicato, sequenziale; nessun lock DAW/stato tenuto durante il caricamento. Richiesta durante `loading` -> `409 SOUNDFONT_LOADING`. Con Play/Record attivi -> `409 TRANSPORT_ACTIVE`.
- Ogni transizione emette `soundfont_status` (sez. 9). La chiave `font` di `last_state.json` si aggiorna solo a `loaded`.
- La route legacy `GET /load_sf2/<file>` (usata da `settings.html`) resta sincrona e testuale ma DEVE aggiornare lo stesso stato.
- All'avvio `restore_settings` DEVE impostare lo stato (`loaded`/`failed`/`unknown`) con la stessa verifica.

### 4.7 Scena corrente e flag "modificata"

- Stato backend `scene_state = {"scene_id": "s-..." | null, "name": "..." | null, "dirty": false}`, persistito in `last_state.json` chiave `scene` (debounced).
- `dirty` diventa `true` per ogni modifica di parte (suono, CC, routing, monitor), dei globali o del **SoundFont caricato** (`POST /api/soundfont/load` o `/load_sf2` riusciti: lo snapshot sonoro contiene `soundfont`), da qualunque client e via qualunque route (anche `cc_update`, `/cc`, `/select_prog`, `/set_effect`, `effect_update`). Le stesse modifiche rendono `dirty` anche il progetto (5.6). Torna `false` solo con Salva/Salva come/richiamo riuscito della scena. Mai "auto-salvataggio" della scena.
- Richiamo in due passi (D9): il preflight controlla `UNSAVED_CHANGES` **prima** del SoundFont, quindi l'utente sceglie Salva/Scarta prima del cambio SF; il caricamento SF del passo (a) rende di nuovo `dirty` la scena, percio' il client DEVE riportare nel passo (b) la decisione gia' presa (`discard_changes: true`) invece di richiederla.
- Dopo Apri progetto: `scene_id = null`, `name = null` ("Suono del progetto"), `dirty = false`.
- Evento `scene_state` solo quando un campo cambia (non a ogni CC).

---

## 5. Progetto nativo `.matsynth.json`

Owner: `ui-persistence` (`project_store.py` + route). Consumatore UI: `ui-studio` (Libreria > Progetti).

### 5.1 Schema v1

File `PROJECTS_DIR/<id>.matsynth.json`, con `PROJECTS_DIR = /home/matteo/matsynth_web/projects/` (nuova costante in `app.py`, da aggiungere a `tests/helpers.redirect_app_paths`). Chiavi esatte della radice (extra -> `VALIDATION_FAILED`; `devices` e `source_scene` facoltative):

| Campo | Tipo | Regole |
| --- | --- | --- |
| `format` | `"matsynth.project"` | Altro -> `422 UNSUPPORTED_VERSION`. |
| `schema_version` | int | Supportata `1`; maggiore -> `422 UNSUPPORTED_VERSION` (`details.found`, `details.supported`). |
| `id` | stringa ID | |
| `name` | stringa `1..64` | Unica tra i progetti (case-insensitive). |
| `created`, `modified` | stringa orario | |
| `transport` | oggetto | `{"bpm": 30..300 (numero), "beats_per_measure": 3|4, "loop": {"enabled": bool, "start_beat": >=0, "end_beat": >=0}}`; `enabled: true` richiede `end_beat > start_beat` (come `daw_recorder.py:toggle_loop`). |
| `sound` | oggetto | **Snapshot sonoro completo**: `{"soundfont", "globals", "parts"}` con lo stesso schema del corpo della scena (4.1). Non e' un riferimento a una scena modificabile. |
| `source_scene` | `{"id", "name"}` o `null` | Solo informativo (da quale scena era partito il suono); MAI usato per ricaricare il suono. |
| `tracks` | array di esattamente 16 | `tracks[i].id == i`. |
| `tracks[i].name` | stringa `1..32` | Come `rename_track` (tronca a 32). Default `"Track N"` (codice attuale). |
| `tracks[i].color` | int `0..15` | Indice palette (`set_track_color`). |
| `tracks[i].mute`, `tracks[i].solo` | bool | |
| `tracks[i].events` | array di `[beat, [b0, b1(, b2)]]` | Vedi 5.2. |
| `tracks[i].clips` | array di `{"id", "start_beat", "end_beat", "name", "created_at"}` | `id` int >= 1 unico nel progetto; `0 <= start_beat < end_beat`; `name` stringa `<= 64`; `created_at` numero (epoch s, come oggi). |
| `next_clip_id` | int | `>` di ogni `clips[].id`. |
| `devices` | oggetto alias -> `{"label", "input", "output"}` | Facoltativo, informativo: descrittori (3.4) degli alias usati, per proporre l'associazione su un altro Pi. MAI associazione automatica. |

Esempio: `docs/schemas/project_v1.json`.

### 5.2 Eventi

- Ogni evento e' `[beat, bytes]` oppure `[beat, bytes, clip_id]` (D3): `clip_id` int >= 1 = ID di un clip della stessa traccia che possiede l'evento; assente o `0` = evento libero. Il writer scrive il terzo elemento solo se != 0. Un `clip_id` che non corrisponde a un clip della traccia -> `422 VALIDATION_FAILED`. `beat` numero finito `0 <= beat <= 100000`; `bytes` lista di interi = esattamente `mido.Message.bytes()` (che restituisce una **lista** di int: verificato), cioe' la stessa rappresentazione di `daw_recorder.py:self.tracks[ch]` (tuple `(beat, list)` che `json` serializza gia' come array: nessuna conversione).
- Solo messaggi di canale: status `0x80..0xEF`; lunghezza 3 per note-off/note-on/poly pressure/CC/pitch bend (`0x8n, 0x9n, 0xAn, 0xBn, 0xEn`), 2 per program change/channel pressure (`0xCn, 0xDn`); byte dati `0..127`. E' cio' che il DAW puo' contenere (`_record_loop` e `import_midi` scartano i messaggi senza `channel`).
- Ordine: `beat` non decrescente nell'array (il DAW li tiene ordinati; `sort` stabile). Il writer DEVE scrivere nell'ordine in memoria.
- Il **nibble di canale** nei byte NON e' significativo (il playback rimappa sulla traccia/destinazione: `_playback_loop`, `export_midi`). Il loader DEVE accettarlo qualunque sia; il writer DOVREBBE normalizzarlo all'ID traccia.
- I beat NON DEVONO essere arrotondati in scrittura.
- Le liste di byte sono immutabili per contratto: nessun codice DEVE modificarle in place (oggi nessuno lo fa: si crea sempre un nuovo `msg.bytes()`); per questo copie superficiali bastano per snapshot e salvataggio.

### 5.3 Validazione: "carica e valida tutto prima di sostituire"

Il loader DEVE costruire in memoria la nuova sessione completa (tracce, clip, nomi, colori, mute/solo, trasporto, parti) **senza toccare** la sessione corrente; solo se tutto e' valido avviene lo scambio (5.6). Qualsiasi errore (file illeggibile, JSON invalido, versione futura, evento invalido, errore disco) -> errore esplicito e sessione precedente **intatta**.

La validazione NON DEVE essere piu' severa di cio' che il DAW puo' contenere, altrimenti un progetto salvato potrebbe non riaprirsi. Quindi sono **accettati con warning** (mai rifiutati, mai modificati):

- note-on senza note-off (`UNPAIRED_NOTE_ON`, con conteggio per traccia);
- clip sovrapposti sulla stessa traccia (consentiti, D3; `CLIP_OVERLAP_PRESENT` solo se mancano i `clip_id`, sez. 6.4);
- eventi fuori da ogni clip;
- nibble di canale diverso dall'ID traccia.

Rifiutati (`422 VALIDATION_FAILED`, `details.path` es. `tracks[3].events[120]`): tipi/chiavi errati o mancanti, chiavi extra, numero di tracce/parti diverso da 16, beat non finiti/negativi/fuori limite o non ordinati, byte non conformi a 5.2, clip con `end_beat <= start_beat` o ID duplicati, `next_clip_id` non maggiore degli ID, parti non conformi a 3.1. Versione futura -> `422 UNSUPPORTED_VERSION`.

Ordine: `format` e `schema_version` si controllano **prima** di ogni altro campo (una versione futura con campi nuovi DEVE dare `UNSUPPORTED_VERSION`, non `VALIDATION_FAILED`); stessa regola per le scene. Il primo errore trovato determina la risposta (non serve elencarli tutti).

**Invariante salvataggio/caricamento**: tutto cio' che il writer puo' produrre dallo stato in memoria DEVE essere accettato dal loader (test di round-trip obbligatorio in `ui-persistence`).

### 5.4 Cosa NON serializzare

Porte aperte, nomi runtime/numero client/PID, thread, note attive, cache (`_cached_note_map`, densita'), appunti, cronologia undo/redo, `revision`, posizione della timeline, stato Play/Record, `armed`, `metronome_enabled`, stato SoundFont runtime, preferenze di vista (tracce nascoste, zoom, altezza, snap, vista corrente: client, `localStorage` chiave `matsynth.prefs.v1`, mai nel progetto), selezione, credenziali di rete o qualunque dato di sistema.

### 5.5 Scrittura atomica (progetti, scene, registro dispositivi, libreria)

Una sola funzione (`project_store.atomic_write_json(path, obj)`), usata per tutti i file nuovi:

1. Serializzare in un file temporaneo **nella stessa directory** del file finale, con nome unico (`.<nome>.<pid>.<thread_id>.tmp`: oggi `app.py:_write_state_atomic` usa un nome fisso `last_state.json.tmp`, sicuro solo perche' tutte le chiamate stanno sotto `STATE_LOCK`).
2. `json.dump(..., separators=(',', ':'), ensure_ascii=False, allow_nan=False)` direttamente sul file (scrittura a blocchi, nessuna stringa intera in RAM).
3. `f.flush()`, `os.fsync(f.fileno())`, `os.replace(tmp, finale)`, poi `fsync` della directory (solo POSIX; su Windows si salta).
4. Su qualunque eccezione: rimuovere il temporaneo, lasciare intatto il file precedente, rispondere `500 DISK_FULL` (`errno.ENOSPC`) o `500 DISK_ERROR`.
5. Lo snapshot dei dati da salvare si prende sotto il lock DAW (6.7) con copie superficiali (liste di riferimenti, ~128 KB misurati per 16x500); la serializzazione e l'I/O avvengono **fuori** dal lock.

### 5.6 Operazioni sul progetto e flag "modificato"

Stato backend `project_state = {"id": "p-..." | null, "name": "Senza titolo", "dirty": false, "file_stat": [mtime_ns, size] | null}` (owner `project_store`), esposto da `GET /api/projects/current` ed evento `project_state`.

**Chi mantiene `dirty`**: solo il backend. `dirty = true` a ogni mutazione del contenuto del progetto, da qualunque client:

- DAW: ogni percorso che passa da `_push_undo`, undo/redo, fine registrazione con eventi, import MIDI, BPM, metro, punti/stato loop, mute/solo, nome/colore traccia. `MultiTrackDAW` DEVE chiamare un hook `self.on_content_change()` (default no-op, collegato da `app.py` a `project_store.mark_dirty`) in questi punti.
- Suono: ogni modifica di parte, globali o SoundFont caricato (le stesse che rendono la scena `dirty`, 4.7) e ogni richiamo di scena.
- `dirty = false` solo dopo Salva, Salva come, Apri, Nuovo riusciti. Un undo che riporta allo stato salvato NON azzera `dirty` (semplicita').
- Arm, posizione, Play/Record, click, preferenze di vista NON rendono `dirty`.
- L'evento `project_state` si emette solo quando un campo cambia (il passaggio `false -> true` una volta, non a ogni mutazione).

Regole comuni a Nuovo / Apri / Import sostitutivo: trasporto fermo (altrimenti `409 TRANSPORT_ACTIVE`, nessuno stop automatico) e, se `dirty` e manca `discard_changes: true`, `409 UNSAVED_CHANGES` (`details.scope: "project"`). L'UI propone **Salva / Scarta / Annulla** (Salva = Salva o Salva come, poi ripete la richiesta; Scarta = ripete con `discard_changes: true`).

| Operazione | Comportamento |
| --- | --- |
| **Nuovo** (`POST /api/projects/new`) | Tracce/clip vuote, nomi `Track N`, colori `i`, mute/solo off, BPM 120, 4/4, loop off 0/0, `next_clip_id = 1`, undo/redo vuoti, posizione 0, arm off. Il suono corrente resta (diventa lo snapshot del nuovo progetto). `id = null`, `name = "Senza titolo"`, `dirty = false`. Non annullabile (la conferma Salva/Scarta lo protegge). |
| **Apri** (`POST /api/projects/open`) | 1) controlli comuni; 2) lettura (limite 5.8) e validazione completa (5.3) fuori lock; 3) preflight del suono come 4.5 punti 5-6 (`soundfont`: `"require_match"` default -> `409 SOUNDFONT_CHANGE_REQUIRED`/`SOUNDFONT_MISSING`, oppure `"keep_current"` -> warning; porte -> `409 PORTS_UNAVAILABLE` salvo `allow_partial`); 4) scambio atomico sotto il lock DAW: tracce, clip, nomi, colori, mute/solo, BPM, metro, loop, `next_clip_id`; undo/redo svuotati; `revision` incrementata; posizione 0; Play/Record fermi; **arm tutti off**; click non avviato (il flag `metronome_enabled` resta quello corrente, il click suona solo con il trasporto); 5) applicazione del suono come 4.5 "Applicazione"; 6) `project_state` = file aperto, `dirty = false`, `scene_state.scene_id = null`. Se il punto 5 fallisce a meta' -> `503 PARTIAL_APPLY` con `details.committed: true` (il contenuto musicale e' gia' aperto). |
| **Salva** (`POST /api/projects/save`) | Sovrascrive il file del progetto corrente. `id == null` -> `409 NO_CURRENT_FILE` (l'UI usa Salva come). Se `os.stat` del file differisce da `file_stat` registrato all'apertura/ultimo salvataggio (altro client, upload) -> `409 FILE_CHANGED` salvo `overwrite: true` dopo conferma. Aggiorna `modified`, `file_stat`, `dirty = false`. |
| **Salva come** (`POST /api/projects/save_as`) | Nuovo `id`, nuovo file, `name` dato. Nome esistente -> `409 NAME_EXISTS` (`details.existing_id`) salvo `overwrite_id` esplicito (che sovrascrive QUEL file mantenendone l'id). Il progetto corrente diventa la copia. Il file precedente resta invariato. |
| **Import MIDI sostitutivo** | Come Apri riguardo a trasporto/`dirty`; dopo l'import il progetto e' **staccato** dal file: `id = null`, `name = <stem del file MIDI>`, `dirty = true` (evita di sovrascrivere per errore il progetto originale con il contenuto importato: D12). Import **Aggiungi** (merge): stesso progetto, `dirty = true`. |
| **Elimina** (`DELETE /api/projects/<id>`) | Se e' il corrente: il contenuto in memoria resta, `id = null`, `dirty = true`. |
| **Esporta/scarica** (`GET /api/projects/<id>/download`) | Allegato `<nome sanificato>.matsynth.json`, contenuto identico al file. |
| **Carica da PC** (`POST /api/projects/upload`) | multipart `file`; limite 5.8 (`413`); validazione completa 5.3; salvato come nuovo progetto con **nuovo id** (mai sovrascrive); nome esistente -> `409 NAME_EXISTS` (il client puo' ripetere con il campo form `name`). NON apre il progetto. |

### 5.7 Dimensioni e RAM (fixture 16 x 500 coppie)

Misure su PC (CPython 64 bit, `tracemalloc`, `tests/fixtures/workload_16x500.mid` = 16000 eventi), **da rimisurare sul Pi** (sistema operativo e bitness del Pi non verificati; su 32 bit gli oggetti sono piu' piccoli):

| Voce | Misura |
| --- | --- |
| Eventi in `daw.tracks` dopo import | ~2.6 MB (picco import ~6.4 MB) |
| JSON compatto degli eventi `[beat,[b0,b1,b2]]` | ~309 KB (variante esadecimale ~266 KB: scartata, richiede conversioni) |
| `json.loads` del JSON eventi | +~2.7 MB |
| Uno snapshot undo senza eventi modificati | ~128 KB (solo riferimenti) |
| `get_state()` serializzato | ~1.8 KB; `get_full_note_map()` ~215 KB |

Stima picco di Apri: file letto (~0.3 MB) + documento parsato (~2.7 MB) + nuova sessione (~2.6 MB) + sessione vecchia con undo (~2.6 MB + fino a 10 livelli) prima del rilascio: **~10-35 MB nel caso peggiore**, contro 416 MiB del Pi. Lo scambio libera la vecchia sessione (undo compreso).

Budget: undo 10 livelli; un livello costa ~128 KB se nessun evento cambia, fino a ~2.6 MB se un'operazione riscrive tutti gli eventi (quantizza tutto): <= ~26 MB. Nessuna estensione del limite di 10 senza misura sul Pi.

### 5.8 Limiti

- `MAX_PROJECT_BYTES = 2 MiB` (circa 7 volte la fixture 16x500; ~20 MB di picco di parse) per lettura da disco e upload; oltre -> `413 PAYLOAD_TOO_LARGE` (upload) o `422 VALIDATION_FAILED` (`details.reason: "file troppo grande"`) in apertura.
- `MAX_EVENTS_TOTAL = 120000` per progetto (validazione).
- Limite e soglie DEVONO essere costanti in `project_store.py` e ridefinibili solo dopo misura sul Pi (`docs/pi-measurement.md`).

---

## 6. Modello DAW: eventi, coppie, clip, undo, revisioni

Stato in `daw_recorder.py:MultiTrackDAW`. Owner: `ui-persistence` per revisione, lock, hook `dirty`, import/export e regole di accoppiamento; `ui-studio` per le operazioni su clip/intervalli/appunti e le nuove route di editing (sez. 11).

### 6.1 Eventi e tracce

- `self.tracks[t]` = lista di tuple `(beat, bytes_list)` ordinata per beat, una per traccia logica `t`, con accanto `self.event_clips[t]`: `array('H')` parallelo (stessa lunghezza, stesso ordine) con l'ID del clip proprietario di ogni evento (`0` = libero; D3). Ogni mutazione che inserisce, rimuove o riordina eventi DEVE aggiornare le due strutture insieme (riordino: un solo `sort` su indici, poi permutazione di entrambe). Nessun ID univoco per evento (vedi 6.6: bastano revisione + indice).
- L'indice di un evento e' la sua posizione in `self.tracks[t]`. E' significativo SOLO insieme a `(t, revision)`.
- Ogni mutazione che cambia l'ordine DEVE riordinare con `sort(key=beat)` stabile prima di rilasciare il lock.

### 6.2 Regola unica di accoppiamento note-on/note-off

**Canonica: FIFO per altezza.** Scorrendo la traccia in ordine, un note-off (o note-on con velocity 0) chiude il note-on **piu' vecchio** ancora aperto della stessa nota. E' la regola di `_build_notes_full` e `get_events_decoded` (oggi). `quantize_tracks` e `get_track_activity`/`_build_intervals_full` oggi usano un dizionario che **sovrascrive** il note-on precedente della stessa nota (accoppiamento diverso con note sovrapposte della stessa altezza: anomalia A26): `ui-persistence` DEVE allinearli a una sola funzione condivisa (`_pair_notes(events) -> [(note, vel, on_beat, off_beat, on_idx, off_idx)]`, `off_idx = -1` se aperta).

Il piano roll e l'editor usano le coppie `(on_idx, off_idx)` restituite da `get_full_note_map` (`notes: [note, vel, start, end, on_idx, off_idx]`).

### 6.3 Appartenenza di un evento a un intervallo/clip (ownership)

**Clip (D3): per ID.** Un evento appartiene al clip il cui ID e' in `event_clips[t]`; il note-off ha sempre lo stesso ID del suo note-on (coppia FIFO, 6.2). Le operazioni su clip (sposta, elimina, trasponi, copia/duplica clip) agiscono SOLO sugli eventi con quell'ID, anche se altri clip si sovrappongono. Gli eventi aggiunti nel piano roll dentro un clip ricevono l'ID del clip piu' recente (ID maggiore) che contiene il loro beat; fuori da ogni clip restano liberi.

**Intervalli** (copia/taglia/cancella intervallo, trasponi/quantizza intervallo, e scelta degli eventi da assegnare a un clip nuovo): regola unica gia' implementata da `_get_note_pair_indices_in_range` (oggi usata da `clear_range`, `move_clip`, `delete_clip`, `transpose_notes` con intervallo):

- una **nota** appartiene a `[start, end)` se il suo **note-on** cade in `[start, end)`; con lei viaggia **sempre** il suo note-off, anche se fuori intervallo;
- un note-off la cui nota inizia prima di `start` NON appartiene all'intervallo (resta dov'e');
- un **evento non-nota** (CC, sustain, pitch bend, pressure, program change) appartiene se il suo beat cade in `[start, end)`.

Conseguenze normative: copiare, tagliare, spostare, cancellare un intervallo o un clip opera sempre su coppie complete; nessun note-off orfano viene creato o eliminato da solo. **Split non cambia il playback** (marca solo un confine). Tagliare/ritriggerare una nota a cavallo di un confine e' un'azione esplicita futura, NON prevista nella prima versione.

Sustain: copia/taglio di un intervallo il cui ultimo CC64 posseduto e' `>= 64` DEVE aggiungere negli appunti un CC64=0 a `end - start` (relativo), cosi' l'incolla non lascia il pedale premuto. Per gli spostamenti di clip non si inseriscono eventi: il rilascio a stop/loop/seek (3.7) copre il caso.

### 6.4 Clip

Un clip e' un **marcatore di intervallo** sulla traccia (`{id, start_beat, end_beat, name, created_at}`), non un contenitore separato: gli eventi stanno in `self.tracks[t]` e appartengono al clip secondo 6.3. Possono esistere eventi fuori da ogni clip ("liberi": note aggiunte nel piano roll, dati vecchi); suonano normalmente e la timeline DEVE mostrarli (la mappa densita' li include gia').

**Clip sovrapposti consentiti (D3).** Con l'ownership per ID (6.3) spostare un clip non trascina mai gli eventi di un altro clip (corregge A18). L'intervallo `[start_beat, end_beat)` del clip e' la sua cornice visiva: DEVE contenere tutti i note-on e gli eventi non-nota posseduti (i note-off possono uscirne). `CLIP_OVERLAP` non viene piu' prodotto dalle operazioni su clip; `CLIP_OVERLAP_PRESENT` resta solo come warning informativo all'apertura di progetti v1 senza `clip_id` (vedi "Migrazione" sotto).

- Migrazione: eventi senza `clip_id` (progetti salvati prima di D3, sessione importata, dati vecchi) ricevono l'ID con la regola 6.3 "intervalli", applicata clip per clip in ordine di ID crescente: un evento gia' assegnato non cambia clip. Gli eventi che restano fuori sono liberi.
- `next_clip_id` non decresce mai: undo/redo ripristinano clip con i loro ID ma il contatore resta `max(corrente, snapshot)` (oggi `_restore_snapshot` lo riporta indietro: un ID potrebbe essere riassegnato dopo un undo, anomalia A19).

Operazioni. Ogni operazione che modifica dati: un solo livello di undo, un solo `daw_state_update`, `revision` incrementata, progetto `dirty`. Durante Play o Record: `409 TRANSPORT_ACTIVE` per tutte le operazioni su clip, taglia, incolla, duplica e cancella intervallo (le route clip lo fanno gia'; `paste_at` oggi ritorna 0 in silenzio). Copia e' sempre consentita. Svuota traccia/tutto restano come oggi (nessun controllo di trasporto). Aggiungi/modifica/elimina nota ed eventi restano vietati solo durante Record (come oggi), consentiti in Play: il playback in corso usa la copia di eventi presa a inizio giro.

| Operazione | Semantica normativa | Oggi |
| --- | --- | --- |
| Fine Record | Per ogni traccia registrata con eventi nuovi: chiusura note/sustain (3.7), poi un clip nuovo `[primo beat, ultimo beat + 0.125)`, nome `Take <id>`, a cui appartengono **solo gli eventi di questo take** (gli eventi gia' presenti nell'intervallo mantengono il loro clip). Il margine `+0.125` (lunghezza minima di clip gia' usata dal codice) resta: la cornice deve contenere anche il CC64=0 di chiusura. Un overdub che interseca take esistenti resta un clip separato (D3). | `_finalize_recording_clips` usa `[primo, ultimo)` (l'ultimo evento resta fuori) e crea sempre un clip nuovo, anche sovrapposto. |
| Sposta (`clip/move`) | Delta su tutti gli eventi con l'ID del clip; il clip mantiene ID e durata. Gli eventi liberi o di altri clip nella zona di arrivo NON cambiano proprietario. Destinazione con `start_beat < 0` -> `400 BAD_REQUEST`. | ownership per intervallo: trascina anche eventi di clip sovrapposti (A18). |
| Ridimensiona (`clip/resize`, solo fine) | Estensione: sempre consentita; non assegna eventi (i proprietari non cambiano). Riduzione: consentita solo se `new_end_beat > ` beat dell'ultimo evento posseduto (note-on o non-nota); altrimenti `409 CLIP_RESIZE_WOULD_EXCLUDE` con `details.min_end_beat` (il client puo' limitare il trascinamento). Minimo `start_beat + 0.125` come oggi. Non cambia mai il playback. Vedi D4. | Riduzione "non distruttiva": le note fuori restano e suonano, senza clip. |
| Dividi (`clip/split`) | Clip originale `[start, split)` stesso ID, nuovo clip `[split, end)` nuovo ID, nome `"<nome> (split)"`; margine 0.01 beat dai bordi. Gli eventi del clip con note-on (o beat, se non-nota) `>= split` passano al nuovo ID; una nota a cavallo resta del clip sinistro. Eventi non spostati, playback invariato. | ownership per intervallo. |
| Unisci (`clip/merge`) | Stessa traccia, >= 2 ID; risultato `[min start, max end)` con **nuovo ID**, nome `"Merged <id>"` (come oggi); gli eventi dei clip uniti passano al nuovo ID, quelli di altri clip no. | ownership per intervallo. |
| Elimina (`clip/delete`) | Rimuove il clip e gli eventi con il suo ID (coppie complete). | ownership per intervallo. |
| Trasponi clip | Note possedute (on e off). | invariato. |
| Crea clip da intervallo (nuova, `clip/create`) | `[start, end)` qualsiasi; prende gli eventi **liberi** dell'intervallo (regola 6.3 "intervalli"), non quelli di altri clip. Permette di rendere selezionabili eventi liberi. | non esiste. |
| Copia (`copy`) | Intervallo `{channel, start_beat, end_beat}` oppure clip `{channel, clip_id}`. Insieme copiato = ownership 6.3 (coppie complete + sustain 6.3). Non modifica il progetto (ne' `revision` ne' `dirty`). Appunti in memoria del server, uno solo, condiviso tra client, mai salvati. Copia di un clip memorizza anche la durata e il nome del clip. | `copy_selection` copia ogni evento con beat nell'intervallo: note-off di note iniziate prima, note-off mancanti per note che finiscono dopo (anomalia A15). |
| Taglia (nuova, `cut`) | Copia + rimozione dello **stesso** insieme di indici, atomica, un solo undo. I clip interamente contenuti nell'intervallo sono rimossi; quelli parzialmente sovrapposti restano (solo i loro eventi nell'intervallo spariscono). | non esiste (copia + `clear_range` = insiemi diversi). |
| Cancella intervallo (`clear_range`) | Stesso insieme e stessa regola clip di Taglia, senza appunti. | elimina anche i clip **parzialmente** sovrapposti (i loro eventi restanti diventano liberi: anomalia A16). |
| Incolla (`paste`) | Dichiara traccia e beat. Appunti da intervallo: eventi inseriti (beat relativi + destinazione), nessun clip creato, nessun controllo sovrapposizione. Appunti da clip: crea un clip nuovo (nuovo ID, stessa durata e nome + " (copia)") in `[beat, beat+durata)`, proprietario degli eventi incollati; sovrapposizioni consentite (D3). Appunti vuoti -> `409 EMPTY_CLIPBOARD`. Nibble canale normalizzato alla traccia di destinazione. | inserisce solo eventi, mai clip. |
| Duplica clip (nuova, `clip/duplicate`) | Copia del clip (eventi con il suo ID) incollata a `end_beat` dello stesso clip sulla stessa traccia, nuovo ID; un solo undo. | non esiste. |
| Duplica traccia (`track/<t>/duplicate`) | Campo `include_config` (`false` = variante legacy "solo MIDI"; `true` = anche la parte: routing/suono/CC copiati sulla parte destinazione). Copia eventi **e clip** (nuovi ID). Destinazione con eventi -> `409 DEST_NOT_EMPTY` salvo `confirm_overwrite: true`. Compatibilita': se il client NON invia ne' `include_config` ne' `confirm_overwrite` (index.html attuale), comportamento legacy (sostituzione senza clip) finche' `ui-studio` non rimuove la vecchia UI. | copia solo eventi, sostituisce in silenzio, nessun clip (anomalia A17). |
| Svuota traccia / tutto | Come oggi (`clear_track`, `clear_all_tracks`), con conferma esplicita lato UI; vietato in Record (`409 RECORDING_ACTIVE`, D22). | invariato. |
| Quantizza, Trasponi intervallo | Come oggi, accoppiamento FIFO (6.2). | quantize accoppia diversamente. |

### 6.5 Undo / redo

- Un **gesto completato = un livello** (`_push_undo` una volta), inclusi editing batch, taglia, incolla clip, import MIDI (sostitutivo o aggiungi: oggi l'aggiungi non e' annullabile e il sostitutivo passa da `clear_all_tracks`: anomalia A13), rinomina e colore traccia (oggi fuori cronologia ma ripristinati dallo snapshot di un undo successivo: anomalia A14).
- Limite **10** livelli (`_MAX_UNDO`), snapshot leggeri come oggi (`_take_snapshot`: copie di liste, tuple condivise). Un trascinamento produce un livello solo al rilascio, mai durante.
- Una sola cronologia per Studio ed editor. Undo/redo vietati durante Play/Record (come oggi).
- La validazione DEVE precedere `_push_undo` (oggi `edit_event` e `edit_note_pair` possono lasciare un livello vuoto se `from_bytes` fallisce: anomalia A21).
- Apri/Nuovo progetto svuotano undo e redo.

### 6.6 Revisione

`MultiTrackDAW.revision`: intero globale (non per traccia), inizializzato all'avvio a `int(time.time() * 1000)` (non si ripete tra riavvii del servizio, nessun ABA con client vecchi; < 2^53, sicuro in JS), poi **+1** a ogni cambiamento che puo' spostare indici di eventi o validita' di ID clip:

- ogni chiamata a `_push_undo` (quindi ogni mutazione di editing, comprese rinomina/colore dopo A14);
- `_restore_snapshot` (undo/redo);
- fine Record (dopo sort e creazione clip), import MIDI, Apri, Nuovo.

Non cambia MAI: tick, posizione, Play/Stop, arm/mute/solo, BPM, metro, loop, click, durante la registrazione (gli eventi accodati durante il Record non toccano la revisione; incrementa una sola volta allo stop). Un incremento "di troppo" (mutazione poi fallita) e' innocuo: invalida solo selezioni.

Contratto:

- `revision` compare in `daw_state_update`/`GET /api/daw/state`, in `GET /api/daw/full_note_map`, `GET /api/daw/track/<t>/events`, `GET /api/daw/track/<t>/clips` e in ogni risposta di mutazione DAW (valore **dopo** la mutazione).
- Le richieste che usano indici (`events/<index>` PUT/DELETE, `edit_note_pair`, `delete_note_pair`, `notes/batch`) e tutte le nuove operazioni di editing accettano `expected_revision`. Se presente e diverso dalla revisione corrente -> `409 REVISION_CONFLICT` con `details.expected_revision`, `details.current_revision`, **nessuna modifica**.
- Se `expected_revision` e' **assente** il controllo e' saltato (compatibilita' con `index.html` attuale). I client nuovi (`ui-studio`, `ui-editor`) DEVONO sempre inviarlo per le operazioni indicizzate e per le nuove route batch/cut/paste; le route nuove `notes/batch` lo rendono **obbligatorio** (`400 BAD_REQUEST` se manca).
- `midi_event_new.idx` (durante il Record) e' provvisorio, solo per visualizzazione: non DEVE essere usato per modifiche.
- Comportamento del client al `409 REVISION_CONFLICT`: rileggere note map/eventi, **invalidare** la selezione di note/eventi (mai riapplicarla per indice), mostrare "Le note sono cambiate: selezione annullata", non ripetere in automatico. Selezioni di clip (ID) e di intervallo (beat) restano se gli ID esistono ancora.
- Gli ID clip sono stabili per le operazioni che non eliminano il clip (sposta, ridimensiona, trasponi, rinomina; dividi conserva l'ID a sinistra); per le route clip `expected_revision` e' facoltativo ma i client nuovi DEVONO inviarlo.

### 6.7 Lock DAW

Oggi `MultiTrackDAW` non ha lock: controlli e mutazioni da thread Flask diversi possono intrecciarsi. `ui-persistence` DEVE introdurre `self._edit_lock = threading.RLock()` e tenerlo per "controllo `expected_revision` + mutazione + incremento revisione + sort", e per gli snapshot di salvataggio. Il lock NON DEVE essere tenuto durante I/O (disco, rete, console FluidSynth, invii MIDI) ne' durante `socketio.emit`: l'emissione di `daw_state_update` avviene dopo il rilascio. Il thread di playback legge copie (`_collect_playback_events`) e non prende il lock per ogni evento.

### 6.8 Selezione (client)

La selezione e' **solo client** (`static/js/state.js`, owner `ui-shell`), per pagina, mai sul server (piu' client = selezioni indipendenti):

```js
selection = {kind: 'none'|'track'|'clip'|'range'|'notes'|'events',
             track: 0, clip_ids: [3], start_beat: 4.0, end_beat: 8.0,
             notes: [{on_idx: 10, off_idx: 11}], event_indices: [], revision: 1700000001234}
```

- Ogni comando dichiara il suo oggetto (traccia, ID clip, intervallo o coppie) nel body: niente "ultimo clip" (`index.html:getLastClip` oggi; rimosso da `ui-studio`). Senza selezione valida il comando e' disabilitato con motivazione visibile.
- A ogni nuova `revision` ricevuta: `notes`/`event_indices` invalidati; `clip_ids` filtrati su quelli esistenti; `range` conservato.

---

## 7. Import/export MIDI (SMF)

Owner: `ui-persistence` (`daw_recorder.py:import_midi`/`export_midi`, `app.py:daw_midi_*`). Le route SMF esistenti restano; nell'UI diventano esplicitamente **Importa MIDI** ed **Esporta MIDI**, separate da Salva progetto. Un SMF non e' un progetto: alias porte e confini clip non sono portabili (l'export lo dice all'utente).

### 7.1 Rappresentazione neutra di bank/program

Il dizionario `channel_programs` scambiato con `export_midi`/`import_midi` ha la forma `{ch: {"bank": (MSB << 7) | LSB, "program": p}}` (14 bit, come oggi in `export_midi` e come atteso da `test_DESIRED_import_keeps_bank_msb1_lsb2_program7`, che confronta il dizionario **esatto** `{'bank': 130, 'program': 7}`: nessuna chiave in piu').

### 7.2 Import

- **Suono iniziale**: per ogni canale, il **primo** Program Change, se cade a un tick `<=` del primo note-on del canale (stesso tick incluso), e' "iniziale": va in `channel_programs` con il bank dato dagli ultimi CC0/CC32 visti **prima** di esso (assenti = 0). Quei CC0/CC32 e quel PC sono consumati (non entrano nelle tracce). Fixture `bank_program_cc.mid` -> `{0: {"bank": 130, "program": 7}}`.
- **Tutti gli altri** Program Change e CC0/CC32 restano **eventi nel tempo** al loro beat (fixture: `(4.0, [0xC0, 12])` in `tracks[0]`). Il PC a beat non zero non e' piu' perso e non "vince" sul suono iniziale (anomalia A1).
- Restano compatibili: `test_workload_import_export_import_is_stable` (PC iniziali consumati: `total_events == 16000`), `test_sustain_and_pitchbend_survive_round_trip`.
- Note, CC, sustain, pitch bend, poly/channel pressure preservati. Messaggi senza canale (SysEx, realtime, meta non gestiti) scartati e **contati**: warning `IMPORT_DROPPED_EVENTS` con `details.counts`.
- Tempo/metro: un solo `set_tempo` (o tutti uguali) e al massimo un `time_signature` con numeratore 3 o 4 e denominatore 4 -> import normale. Altrimenti (tempo map, metri diversi o multipli) -> `422 IMPORT_NEEDS_CONVERSION` con `details.issues` (es. `[{"type":"tempo_map","count":3},{"type":"meter","value":"6/8"}]`) salvo `convert: true` (query/form), che usa il **primo** tempo e 4/4 se il metro non e' supportato, e risponde con warning `IMPORT_CONVERTED`. Oggi vince l'ultimo `set_tempo` in silenzio.
- **Clip**: l'import crea un clip per traccia importata, `[primo beat, ultimo beat + 0.125)` (stesso margine della fine Record, 6.4: ogni evento importato e' posseduto; anomalia A7). Gli eventi importati appartengono al clip creato; con Aggiungi resta un clip separato anche se interseca clip esistenti (D3).
- Un solo livello di undo per l'intero import (A13); `revision` +1; progetto `dirty` (5.6, staccato dal file se sostitutivo).
- Nomi traccia: con Sostituisci, se una traccia SMF contiene un solo canale e un meta `track_name`, quel nome DOVREBBE diventare il nome della traccia (troncato a 32).
- Applicazione del suono iniziale (route `daw_midi_load`/`daw_midi_upload`), per traccia `t` secondo la sua parte:
  - **interna** -> bank FluidSynth secondo la convenzione 7.3 (`MSB`, con percussioni su canale 9), `select` via console, parte e `channels` aggiornati;
  - **esterna** -> `sound.bank_msb = bank >> 7`, `sound.bank_lsb = bank & 127`, `sound.program`; invio CC0/CC32/PC alla porta solo se `available`. NON DEVE essere applicato nulla a FluidSynth per parti esterne (oggi `select` incondizionato a FluidSynth: anomalia A22).
  - Il test `test_midi_upload_applies_bank_msb_only_to_synth` diventa: parte 0 interna -> `select 0 <sf_id> 1 7`.

### 7.3 Export e conversione bank per parti interne

- `export_midi(channel_programs)` resta neutro (scrive `CC0 = bank >> 7`, `CC32 = bank & 127`, PC a tick 0) e i suoi test restano validi. La conversione dal **bank FluidSynth** di una parte interna al valore neutro e' fatta dal chiamante con **una sola** funzione (`project_store.fluid_bank_to_smf(bank, channel)` e inversa `smf_to_fluid_bank(bank14, channel)`), secondo la modalita' di default di FluidSynth `synth.midi-bank-select = gs` (CC0 = numero di bank, CC32 ignorato; `startfluid.sh` non la cambia):
  - bank interno `0..127` -> neutro `bank << 7` (MSB = bank, LSB = 0);
  - bank interno `128` su canale `9` -> neutro `0` (in GS il canale 10 e' batteria: FluidSynth seleziona le percussioni);
  - bank interno `>= 128` su altri canali -> non rappresentabile: si scrive solo il PC (`{"bank": None, "program": p}`) e si aggiunge il warning `BANK_NOT_REPRESENTABLE`;
  - import su parte interna: bank FluidSynth = MSB (`bank14 >> 7`); se `destination.channel == 9` -> `128`.
  Oggi `daw_midi_save` passa il bank FluidSynth direttamente come valore a 14 bit (bank 128 -> MSB 1/LSB 0; bank 5 -> MSB 0/LSB 5) e l'import legge solo il MSB: asimmetrico. La convenzione GS e' **da verificare con FluidSynth reale** (D5).
- Estensione di `export_midi` (compatibile): una voce del dizionario PUO' avere `"bank": None` (nessun CC0/CC32 iniziale) e/o `"program": None` (nessun PC iniziale). Un canale **assente** dal dizionario mantiene il comportamento di oggi (CC0=0, CC32=0, PC 0: test `test_export_structure`).
- Parti esterne: neutro = `(bank_msb << 7) | bank_lsb`; entrambi `null` -> `"bank": None`; uno solo `null` -> scritto come 0 (in SMF il bank select e' la coppia CC0+CC32); `program null` -> `"program": None`. Round-trip garantito: MSB 1, LSB 2, PC 7 -> stessi valori (piano sez. 12).
- Export su 16 canali logici, canale = ID traccia (come oggi); nomi traccia = nomi DAW (oggi `"Channel N"`); i PC/bank nel tempo esportati al loro tick (gia' oggi: sono eventi).

---

## 8. API HTTP

Legenda colonna "Cambia": **=** invariata; **+** estensione additiva (campi nuovi in risposta/richiesta facoltativi, busta 2.1, stessi codici HTTP per gli errori esistenti); **S** semantica cambiata (descritta). Colonna "Fase" = chi la modifica. Inventario completo di partenza: `docs/regression-checklist.md` sez. 2 (corretto qui dove serviva: la route legacy `/set_effect` non ha whitelist, quella socket si').

### 8.1 Route esistenti

| Metodo, path | Cambia | Fase | Contratto |
| --- | --- | --- | --- |
| GET `/`, `/settings`, `/presets` | = | — | Restano; `/settings` e `/presets` aprono il pannello della shell tramite i loro template (owner `ui-offline-settings`), senza modificare `app.py`. |
| GET `/list_sf2`, `/get_instruments` | = | — | Array nudi (legacy). Nuovo equivalente: `GET /api/soundfonts`. |
| GET `/load_sf2/<file>` | S | persistence | Testo invariato; aggiorna lo stato SoundFont (4.6). |
| GET `/select_prog/<chan>/<bank>/<prog>` | S | routing | `chan` = ID parte; solo parti interne (esterna -> HTTP 409 testo `"Parte esterna"`); scrive via `save_part`; rende `dirty` scena e progetto. |
| GET `/set_effect/<type>/<val>` | S | routing | Whitelist `gain`, `reverb.level`, `chorus.level` (altro -> 400 testo), come `handle_effect_update` (anomalia A10a). |
| GET `/cc/<chan>/<cc>/<val>` | S | routing | `chan` = ID parte, inviato alla destinazione della parte sul suo canale; persistenza debounced in `parts`. |
| GET `/reset_channel/<chan>` | S | routing | Solo parti interne (esterna -> 409 testo). UI con conferma (`ui-live`). |
| GET `/refresh_status`, `/get_state` | + | routing | `/get_state` aggiunge chiavi `parts`, `midi_mode`, `scene` (additivo). |
| GET `/api/capture_current_config` | + | persistence | Aggiunge `pan` per canale. |
| GET `/api/presets/list` | + | persistence | Include le scene v1 (`font` = `soundfont.file`); ignora `.legacy_backup/`. |
| GET `/api/presets/load/<file>` | + | persistence | File v1 -> forma legacy (4.2); legacy -> verbatim. |
| POST `/api/presets/save` | = | — | Deprecata (suffisso `_N` conservato solo per `presets.html`, D16). |
| DELETE `/api/presets/delete/<file>` | = | — | |
| POST `/api/presets/rename` | S | persistence | Nome file di destinazione esistente (diverso dall'origine) -> `409 NAME_EXISTS`, nessuna sovrascrittura (A10b). |
| POST `/api/presets/apply` | S | persistence | Invia anche `pan` se presente; persiste lo stato effettivo; resto invariato (4.3). |
| GET `/api/network`, `/api/audio_devices`, `/api/midi_devices`, POST `/api/save_hardware` | = | — | `/api/midi_devices` (`aconnect -i`) resta per `settings.html` legacy; il nuovo registro e' `/api/midi/*`. |
| GET `/api/daw/state` | + | persistence | `data.revision` (6.6). |
| POST `/api/daw/record/start` | S | routing | Preflight input (3.7): `503 PORT_UNAVAILABLE`; Play attivo -> `409 TRANSPORT_ACTIVE`; "nessun canale armato" resta 400. Accetta `return_to_start` (bool, default `true`): vedi "Ritorno al punto di partenza dopo Record" (implementato da `ui-studio`). |
| POST `/api/daw/record/stop` | S | routing | Chiude note/sustain aperti (3.7); risposta + `revision`. |
| POST `/api/daw/play/start` | S | routing | Nessuna destinazione -> `503 PORT_UNAVAILABLE`; warning `PLAYBACK_TRACKS_UNAVAILABLE`. |
| POST `/api/daw/play/stop`, `/stop_all`, `/rewind` | + | routing | Rilascio note/sustain tracciati. |
| POST `/api/daw/panic` | S | routing | Contenuto PANIC 3.7, tutte le destinazioni. |
| POST `/api/daw/seek_beat` | + | routing | Rilascio note se in Play; 409 durante Record come oggi. |
| POST `/api/daw/bpm`, `/time_signature` | + | persistence | DEVONO emettere `daw_state_update` (oggi `set_bpm`/`set_time_signature` non lo fanno: anomalia A20) e rendere `dirty`. |
| POST `/api/daw/metronome/toggle`, `/loop_points`, `/loop/toggle` | + | persistence | `dirty` per i loop; il click non rende `dirty`. |
| POST `/api/daw/undo`, `/redo` | + | persistence | + `revision`; `error.code = NOTHING_TO_UNDO` (HTTP 400 invariato). |
| POST `/api/daw/track/<t>/arm` | = | — | Non rende `dirty`. |
| POST `/api/daw/track/<t>/mute`, `/solo` | + | routing | Effetto immediato in playback con rilascio note (A25); `dirty`. |
| POST `/api/daw/track/<t>/clear`, `/api/daw/clear_all` | + | studio | + `revision`. |
| POST `/api/daw/track/<t>/clear_range` | S | studio | Regola clip 6.4 (A16); `expected_revision`; + `revision`. |
| POST `/api/daw/track/<t>/duplicate` | S | studio | `{dest_channel, include_config?, confirm_overwrite?}` (6.4, A17). |
| POST `/api/daw/track/<t>/rename`, `/color` | S | persistence | Un livello di undo (A14); `dirty`; + `revision`. Errore con busta 2.1 (oggi `status:error` senza messaggio). |
| GET `/api/daw/track/<t>/clips` | + | persistence | + `revision`. |
| GET `/api/daw/track/<t>/activity` | S | persistence | Accoppiamento FIFO (6.2). |
| GET `/api/daw/full_density_map`, `/full_note_map` | + | persistence | + `revision` (fuori da `map`). Nessuna chiamata a ogni tick. |
| POST `/api/daw/quantize` | S | persistence | Accoppiamento FIFO; + `revision`. |
| POST `/api/daw/transpose` | + | persistence | `expected_revision`; + `revision`. |
| POST `/api/daw/preview_note` | S | routing | Inviata alla **destinazione della traccia** via router (oggi `noteon`/`noteoff` su console FluidSynth); stessa durata 40-400 ms. |
| POST `/api/daw/copy` | S | studio | Pair-aware + sustain; `{channel, start_beat, end_beat}` oppure `{channel, clip_id}`; risposta `{copied, clipboard: {kind: "range"|"clip", length_beats, source_track}}`. Non cambia `revision`. |
| POST `/api/daw/paste` | S | studio | `{channel, beat, expected_revision?}`; risposta `{pasted, new_clip_id|null, revision}`; `409 EMPTY_CLIPBOARD`. Con appunti vuoti oggi ritorna `{status: ok, pasted: 0}`: i client legacy ricevono ora 409 (accettabile: era un falso successo). |
| POST `/api/daw/clip/move`, `/resize`, `/split`, `/merge`, `/delete`, `/transpose` | S | studio | Regole 6.4; `expected_revision?`; + `revision`; codici esistenti (400/404/409) invariati. |
| GET `/api/daw/track/<t>/events` | + | persistence | + `revision`. |
| PUT/DELETE `/api/daw/track/<t>/events/<index>` | + | persistence | `expected_revision?`; `RECORDING_ACTIVE` mappato sul 400 esistente per compatibilita'; + `revision`. |
| POST `/api/daw/track/<t>/add_note` | + | persistence | + `revision`. |
| PUT `/api/daw/track/<t>/edit_note_pair`, DELETE `/delete_note_pair` | + | persistence | `expected_revision?`; + `revision`. |
| POST `/api/daw/midi/save` | S | persistence | Conversione bank 7.3; nomi traccia; warning `BANK_NOT_REPRESENTABLE`. |
| GET `/api/daw/midi/list`, DELETE `/delete/<f>`, GET `/download/<f>` | = | — | |
| GET `/api/daw/midi/load/<f>`, POST `/api/daw/midi/upload` | S | persistence | Import 7.2; parametri nuovi facoltativi `convert`, `discard_changes` (query per `load`, form per `upload`). Controllo `dirty` solo se il parametro `discard_changes` e' presente: `false` + progetto `dirty` + Sostituisci -> `409 UNSAVED_CHANGES`; `true` -> procede. Parametro assente (client legacy) = comportamento attuale senza controllo (D13); i client nuovi DEVONO inviarlo sempre. `409` durante transport invariato. |

**Override HTTP legacy** (unici casi in cui `error.code` e HTTP non seguono la tabella 2.3; tutto il resto la segue):

| Route esistente | Condizione | `error.code` | HTTP conservato |
| --- | --- | --- | --- |
| PUT/DELETE `/api/daw/track/<t>/events/<index>`, PUT `/edit_note_pair`, DELETE `/delete_note_pair`, POST `/add_note` | durante Record | `RECORDING_ACTIVE` | 400 |
| stesse route | indice non valido | `BAD_REQUEST` (`details.field`) | 400 |
| POST `/api/daw/bpm` | durante Play/Record | `TRANSPORT_ACTIVE` | 400 |
| POST `/api/daw/track/<t>/duplicate` | trasporto attivo (oggi stesso messaggio di "sorgente vuota") | `TRANSPORT_ACTIVE` | 400 |
| POST `/api/daw/record/start` | gia' in Record | `TRANSPORT_ACTIVE` | 400 |
| POST `/api/daw/undo`, `/redo` | durante Play/Record (oggi indistinguibile da pila vuota) | `TRANSPORT_ACTIVE` | 400 |
| POST `/api/daw/clip/split` | punto fuori dal clip (oggi stesso 404 di "clip non trovato") | `BAD_REQUEST` (`details.field: "split_beat"`) | 404 |
| POST `/api/daw/record/stop`, `/play/stop` | niente da fermare | `BAD_REQUEST` | 400 |

Le route nuove NON DEVONO usare override. Le route legacy a testo (`/select_prog`, `/reset_channel`) restano testo: il 409 per parte esterna e' un `409` con corpo testo.

### 8.2 Route nuove (minime)

Tutte con busta 2.1. `<t>` = ID traccia, `<id>` = ID scena/progetto, `<alias>` = alias dispositivo.

**Sistema e modalita'** (owner `ui-routing`)

| Metodo, path | Richiesta | Risposta (`200` salvo indicazione) |
| --- | --- | --- |
| GET `/api/system/status` | — | `{midi_mode: {effective, configured, restart_required}, synth: {console: "available"|"missing", midi_port: <stato 3.5>, click: <stato>}, soundfont: <stato 4.6>, revision}` |
| PUT `/api/system/mode` | `{mode: "legacy"|"managed"}` | `{midi_mode: {effective, configured, restart_required: true|false}}`. Scrive solo la configurazione (sez. 10); NON riavvia. Valore diverso -> 400. |

**Porte e dispositivi** (owner `ui-routing`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| GET `/api/midi/ports[?all=1]` | — | `{inputs: [{name, descriptor, aliases: [..], excluded: null|"system"|"through"|"own"}], outputs: [{name, descriptor, aliases, internal: bool, excluded}]}`. `name` = nome runtime (non persistere). |
| GET `/api/midi/devices` | — | `{devices: [{alias, label, input, output, controls, status: {input: <stato>, output: <stato>}, candidates: {input: [name...], output: [...]}}]}` (`candidates` solo se `ambiguous`). |
| PUT `/api/midi/devices/<alias>` | `{label, input: {from_port: "<name runtime>"} | {descriptor: {...}} | null, output: (idem), controls: [..]|null}` | `201` se nuovo, `200` se aggiornato: `{device}`. Alias non conforme -> 400. |
| DELETE `/api/midi/devices/<alias>` | — | `{}`; usato da parti -> `409 DEVICE_IN_USE`. |
| POST `/api/midi/devices/<alias>/select` | `{direction: "input"|"output", port: "<name runtime>"}` | Scelta tra candidati ambigui, solo runtime: `{device}`. Porta non tra i candidati -> 400. |

**Parti** (owner `ui-routing`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| GET `/api/parts` | — | `{parts: [16 parti 3.1], runtime: [{part_id, input_status, destination_status, monitor_effective, warnings: [..]}], conflicts: [3.6], midi_mode}` |
| GET `/api/parts/<t>` | — | `{part, runtime}` |
| PATCH `/api/parts/<t>` | sottoinsieme di `{input, destination, monitor, sound, cc}`: `input`/`destination`/`sound` sostituiscono l'intero sotto-oggetto; `cc` fonde le chiavi; regole di default 3.2 al cambio di `kind` | `{part, runtime, conflicts, applied: {delivery: "sent"|"failed"|"skipped", sent: ["program","cc7",...], destination_status}}`. Parte risultante non valida -> `422 VALIDATION_FAILED`; cambio `input` in Record -> `409 RECORDING_ACTIVE`; destinazione non disponibile -> `200` con `applied.delivery: "skipped"` e config salvata (la destinazione configurata si conserva). |

Il fast path degli slider resta `cc_update` (Socket.IO, `chan` = ID parte). Per i globali interni resta `effect_update`.

**SoundFont** (owner `ui-persistence`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| GET `/api/soundfonts` | — | `{files: ["GeneralUser-GS.sf2", ...], current: <stato 4.6>}` |
| POST `/api/soundfont/load` | `{file}` | `202 {job: <stato 4.6 con state "loading">}`; inesistente -> `404 NOT_FOUND`; in corso -> `409 SOUNDFONT_LOADING`; Play/Record -> `409 TRANSPORT_ACTIVE`. |
| GET `/api/soundfont/status` | — | `{soundfont: <stato 4.6>}` |

**Scene** (owner `ui-persistence`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| GET `/api/scenes` | — | `{scenes: [{id, name, created, modified, soundfont, source: "v1"|"legacy", favorite, order, valid, error?}], current: <scene_state>}`, ordinate per `order` poi nome. |
| GET `/api/scenes/current` | — | `{scene_state}` |
| GET `/api/scenes/<id>` | — | `{scene: <v1, migrata se legacy>, source, warnings}` |
| POST `/api/scenes` | `{name, overwrite_id?}` | Salva come (cattura 4.4): `201 {scene}`; `409 NAME_EXISTS`. |
| PUT `/api/scenes/<id>` | `{}` | Salva (sovrascrive con lo stato corrente, conserva id/nome/created; backup se legacy): `{scene}`. |
| PATCH `/api/scenes/<id>` | `{name?, favorite?, order?}` | `{scene_meta}`; nome esistente -> `409 NAME_EXISTS`. `favorite`/`order` in `library.json`. |
| DELETE `/api/scenes/<id>` | — | `{}`; se era la corrente -> `scene_state.scene_id = null`, `dirty = true`. |
| POST `/api/scenes/<id>/preflight` | `{discard_changes?, allow_partial?}` | 4.5 |
| POST `/api/scenes/<id>/recall` | `{discard_changes?, allow_partial?}` | 4.5 |

**Progetti** (owner `ui-persistence`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| GET `/api/projects` | — | `{projects: [{id, name, modified, size_bytes, valid, error?}], current: <project_state>}` |
| GET `/api/projects/current` | — | `{project_state, revision}` |
| POST `/api/projects/new` | `{discard_changes?}` | `{project_state, revision}` |
| POST `/api/projects/open` | `{id, discard_changes?, allow_partial?, soundfont?: "require_match"|"keep_current"}` | `{project_state, revision, sound: <come recall 4.5>, warnings}`; errori 5.6. |
| POST `/api/projects/save` | `{overwrite?}` | `{project_state}`; `409 NO_CURRENT_FILE`/`FILE_CHANGED`; `500 DISK_*`. |
| POST `/api/projects/save_as` | `{name, overwrite_id?}` | `201 {project_state}`; `409 NAME_EXISTS`. |
| PATCH `/api/projects/<id>` | `{name}` | `{project_meta}`; `409 NAME_EXISTS`. |
| DELETE `/api/projects/<id>` | — | `{project_state}` |
| GET `/api/projects/<id>/download` | — | allegato `application/json` |
| POST `/api/projects/upload` | multipart `file`, `name?` | `201 {project_meta}`; `413`, `422`, `409 NAME_EXISTS`. |

**Editing DAW atomico** (owner `ui-studio`; usate da `ui-studio` e `ui-editor`)

| Metodo, path | Richiesta | Risposta |
| --- | --- | --- |
| POST `/api/daw/cut` | `{channel, start_beat, end_beat, expected_revision}` | `{copied, removed, clipboard, revision}` (6.4) |
| POST `/api/daw/clip/create` | `{channel, start_beat, end_beat, expected_revision}` | `201 {clip, revision}` |
| POST `/api/daw/clip/duplicate` | `{channel, clip_id, expected_revision}` | `201 {new_clip_id, revision}`; `404` |
| PUT `/api/daw/track/<t>/notes/batch` | `{expected_revision, edits: [{on_idx, off_idx, on_beat?, off_beat?, note?, velocity?}]}` (`expected_revision` obbligatoria) | `{modified, revision}`; tutte le modifiche applicate in un solo passaggio sugli indici della revisione attesa, un sort, un undo (`daw_recorder.py:edit_notes_batch`, oggi senza route). Indice fuori intervallo o `on_idx` che non punta a un note-on -> `422 VALIDATION_FAILED` e nessuna modifica (oggi `edit_notes_batch` salta in silenzio le voci invalide). |
| DELETE `/api/daw/track/<t>/notes/batch` | `{expected_revision, pairs: [{on_idx, off_idx}]}` | `{removed, revision}` (`delete_notes_batch`). |

Selezione esplicita: non serve una route; ogni operazione riceve l'oggetto nel body (6.8). Stato "dirty": `GET /api/projects/current`, `GET /api/scenes/current` + eventi 9.2. Modalita' managed/legacy: `GET /api/system/status`, `PUT /api/system/mode`.

Esempi di richieste/risposte: `docs/schemas/api_examples.json` (verificati dal test per forma della busta e codici).

---

## 9. Eventi Socket.IO

Un solo socket per pagina (`io()` una volta, owner `static/js/api.js`/`state.js` di `ui-shell`); nessun listener duplicato dopo cambio vista. Namespace `/`, `async_mode='threading'` come oggi.

### 9.1 Esistenti (restano)

| Direzione | Evento | Payload | Frequenza | Cambia |
| --- | --- | --- | --- | --- |
| c->s | `cc_update` | `{chan, cc, val}`; `chan` = **ID parte** | coalescato dal client: max ~1 ogni 40 ms per slider + valore finale al rilascio | S (routing per parte); ack busta 2.1 |
| c->s | `effect_update` | `{type in gain/reverb.level/chorus.level, val}` | come sopra | + ack busta |
| c->s | `transport_cmd` | `{cmd, ...}`: `play_start`, `play_stop`, `record_start` (`channel?`, `return_to_start?` bool default `true`, vedi "Ritorno al punto di partenza dopo Record"), `record_stop`, `stop_all`, `panic`, `rewind`, `seek` (`position` s, solo da fermo), `seek_beat` (`beat`), `undo`, `redo`, `toggle_loop` | su azione | + ack busta con gli stessi `code` delle route HTTP equivalenti |
| s->c | `daw_state_update` | stato completo come `GET /api/daw/state` `data`: `is_recording, is_playing, timeline_position (s), bpm, metronome_enabled, beats_per_measure, armed, muted, solo, any_solo, has_data, track_counts, track_durations, max_duration, loop_enabled, loop_start, loop_end, can_undo, can_redo, track_names, track_colors, clips` (mappe con chiavi `"0".."15"`) + **`revision`** | a ogni cambio di stato DAW, mai periodico | + `revision` |
| s->c | `daw_tick` | `{t, is_playing, is_recording, bpm, beats_per_measure[, track_counts, track_durations, max_duration]}` | 20 Hz in Record, 10 Hz in Play; contatori al massimo ogni 0.25 s (`_update_loop`) | = (nessuna revisione nei tick) |
| s->c | `record_activity` | `{channels: [{channel, count}]}` (`channel` = ID traccia) | solo in Record, solo con note attive, con il tick | = |
| s->c | `midi_event_new` | `{ch, beat, type, idx[, note, note_num, vel][, cc_num, cc_val]}` | per evento registrato | = (`idx` provvisorio, 6.6) |
| s->c | `midi_playback_beat` | `{beat}` | ~10 Hz in Play | = |

### 9.2 Nuovi

| Evento (s->c) | Payload | Quando / frequenza | Owner |
| --- | --- | --- | --- |
| `ports_status` | `{inputs: [...], outputs: [...], devices: [{alias, status: {input, output}}], internal: <stato>}` (forme di 8.2) | solo quando cambia (polling 2 s lato server) | routing |
| `parts_update` | `{parts: [parte 3.1...], runtime: [...], conflicts: [...]}` con solo le parti cambiate | coalescato: max 10 Hz, ultimo valore vince (gli slider non producono un evento per messaggio) | routing |
| `input_activity` | `{parts: [part_id...]}` parti che hanno ricevuto input nell'ultimo intervallo | max 10 Hz, solo se c'e' attivita' (Record o no) | routing |
| `midi_error` | `{alias, direction, status: "offline", message, at}` | a ogni transizione verso `offline` | routing |
| `soundfont_status` | stato 4.6 | a ogni transizione | persistence |
| `scene_state` | `{scene_id, name, dirty}` | quando un campo cambia | persistence |
| `project_state` | `{id, name, dirty, file_stat}` | quando un campo cambia | persistence |

Regole: nessun evento nuovo periodico in assenza di cambiamenti; il client NON DEVE chiamare `full_note_map`/`full_density_map` a ogni tick (solo dopo `daw_state_update` con `revision` cambiata, con debounce come `scheduleDensityRefresh` oggi, 150 ms).

---

## 10. Modalita' di startup `legacy` / `managed`

Owner: `ui-routing` (`startfluid.sh`, `app.py`, `midi_router.py`). Interfaccia di impostazione: `ui-offline-settings`.

### 10.1 Dove vive il flag

- Chiave **`midi_mode`** in `/home/matteo/matsynth_web/last_state.json`, valori `"legacy"` | `"managed"`. Assente, illeggibile o diversa -> **`legacy`** (default; `managed` e' solo opt-in e NON DEVE diventare default in questo progetto prima delle prove hardware).
- `startfluid.sh` (che gia' legge `last_state.json` con `jq`):

```bash
MIDI_MODE=$(jq -r '.midi_mode // "legacy"' "$STATE_FILE" 2>/dev/null)
[ "$MIDI_MODE" = "managed" ] || MIDI_MODE="legacy"
if [ "$MIDI_MODE" = "managed" ]; then AUTOCONNECT=0; else AUTOCONNECT=1; fi
export MATSYNTH_MIDI_MODE="$MIDI_MODE"
# fluidsynth ... -o midi.autoconnect=$AUTOCONNECT ...
# blocco "aconnect $MIDI_DEVICE -> FLUID" eseguito SOLO se MIDI_MODE=legacy
```

- Backend: modalita' **effettiva** letta una sola volta all'avvio da `MATSYNTH_MIDI_MODE` (cio' che lo script ha davvero configurato); se la variabile manca (avvio manuale di `app.py`) -> chiave `midi_mode` di `last_state.json` -> `legacy`. Non cambia mai a processo avviato. `configured` = valore attuale della chiave; `restart_required = configured != effective`.
- `PUT /api/system/mode` scrive solo la chiave (con il writer di stato sotto `STATE_LOCK`). Il riavvio resta un'azione esplicita separata (flusso esistente `POST /api/save_hardware` -> `sudo systemctl restart matsynth.service`, che rilancia `startfluid.sh` e quindi FluidSynth: le connessioni ALSA di FluidSynth non sopravvivono al suo riavvio).

### 10.2 Differenze di comportamento

| Aspetto | `legacy` (default) | `managed` |
| --- | --- | --- |
| FluidSynth `midi.autoconnect` | `1` (come oggi) | `0` |
| `aconnect` tastiera -> FluidSynth in `startfluid.sh` | si' se `midi_device` impostato (come oggi) | mai |
| Monitor live | ALSA diretto; il router non inoltra nulla (3.5) | router per parte (`monitor`) |
| Lettura input (router, un lettore per porta) | si' (registrazione e indicatori) | si' |
| Playback/preview/CC verso destinazioni interne ed esterne | via router | via router |
| Rimozione connessioni | nessuna | solo connessioni create da MatSynth (startup o router); MAI `aconnect -x` o rimozione di connessioni altrui |

In `managed` non possono coesistere collegamento ALSA diretto e software-thru della stessa tastiera (piano sez. 12): all'avvio in `managed` il router DOVREBBE verificare con `aconnect -l` (sola lettura) che non esista una connessione tastiera -> FluidSynth e, se esiste, segnalarlo in `/api/system/status` (`warnings: [{code: "MONITOR_HANDLED_BY_ALSA"}]`) **senza** rimuoverla se non e' stata creata da MatSynth.

### 10.3 Rollback a `legacy`

1. Da UI: Impostazioni > MIDI > modalita' `legacy` (`PUT /api/system/mode`), poi "Riavvia servizio".
2. A mano sul Pi (se l'UI non e' raggiungibile): `jq '.midi_mode = "legacy"' last_state.json > /tmp/s.json && mv /tmp/s.json /home/matteo/matsynth_web/last_state.json` (oppure rimuovere la chiave), poi `sudo systemctl restart matsynth.service`.
3. Se `last_state.json` e' corrotto: `startfluid.sh` (jq fallisce -> default) e `app.py:get_last_state` (rinomina il file corrotto, default senza `midi_mode`) ricadono entrambi su `legacy`.
4. Verifica: `aconnect -l` mostra di nuovo la tastiera collegata a `FLUID Synth`; `GET /api/system/status` -> `effective: "legacy"`.

Il rollback e le prove di `managed` al riavvio sono **da verificare su hardware** (fase `ui-acceptance`).

---

## 11. Fasi: ordine, ownership, interfacce; anomalie e test

### 11.1 Ordine e regola dei file condivisi

```text
ui-contracts
  |-- ui-shell (solo frontend) ......................\
  |-- ui-routing (backend) -> ui-persistence (backend) -> ui-live (solo frontend)
                                                     -> ui-studio (backend editing + frontend) -> ui-editor
      ui-shell -> ui-offline-settings (solo frontend/template/asset)
  ui-live + ui-editor + ui-offline-settings -> ui-acceptance
```

- **Backend seriale**: `app.py`, `daw_recorder.py`, `startfluid.sh` sono modificati SOLO, e in quest'ordine, da `ui-routing` -> `ui-persistence` -> `ui-studio` -> (`ui-editor`, solo correzioni alle route di editing di `ui-studio`, quando nessun'altra fase backend e' attiva). `ui-shell`, `ui-live`, `ui-offline-settings` NON DEVONO modificare file Python ne' `startfluid.sh`: se scoprono una mancanza backend, la scrivono nell'handoff e il coordinatore la assegna alla fase backend successiva o a un intervento dedicato.
- Codice backend nuovo in moduli nuovi (`midi_router.py`, `api_common.py`, `project_store.py`) con funzioni pure testabili senza importare `app.py`; in `app.py` solo route sottili e cablaggio. Niente blueprint obbligatori (le route restano in `app.py`, raggruppate in sezioni commentate per fase) perche' i test esistenti sostituiscono globali di `app.py` (`send_fluid`, `PRESETS_DIR`, `MIDI_DIR`, `STATE_FILE`, `sf_id`: `tests/helpers.py:redirect_app_paths`) e devono continuare a funzionare. Ogni nuova globale di percorso (`PROJECTS_DIR`, `DEVICES_FILE`, `LIBRARY_FILE`) DEVE essere aggiunta a `redirect_app_paths` dalla fase che la introduce; l'avvio del router DEVE essere neutralizzabile in `helpers.import_app` (niente rtmidi nei test).
- **Frontend per file**: ogni fase possiede file propri (tabella 11.2). La pagina resta una (`index.html`) con include Jinja di `templates/partials/*.html`; CSS per vista in `static/css/<vista>.css` caricati da `index.html` (deviazione D11 rispetto a un unico `style.css` a sezioni).
- Nessuna fase modifica `app.old.py`, `app.old2.py`, `indexold.html`, `loop.html`.

### 11.2 Ownership e interfacce per fase

**`ui-shell`** (solo frontend)
- Possiede: `templates/index.html` (struttura shell, `<head>`, navigazione Suona | Studio, Libreria e Impostazioni secondari, barra globale con connessione/trasporto/modificato/PANIC, punti di include), `templates/partials/` (crea `live.html`, `studio.html`, `editor.html`, `settings.html` vuoti/placeholder e sposta il markup DAW attuale in `studio_legacy.html`), `static/js/api.js`, `state.js`, `shell.js`, `units.js`, `part_panel.js` (pannello sonoro unico, estratto da `#panel-mixer`: schede Mix/Suono), `library.js` (finestra Libreria con sezioni registrabili), `static/js/legacy_daw.js` (JS inline attuale spostato **senza cambi di comportamento**, adattato solo per usare il socket/stato unici), `static/css/style.css` (token/base) e `static/css/{live,studio,editor,settings}.css` vuoti.
- Consuma: sez. 1.3, 2, 6.8, 9 (con le route esistenti: il backend nuovo puo' non esserci ancora; `api.js` gestisce entrambe le buste).
- Interfaccia esposta (globale `window.MatSynth`, unica):
  - `api.request(method, url, body?, {timeout?}) -> Promise<{ok, http, body, error, warnings, unknown_outcome}>`: non rigetta mai per errori HTTP; timeout/rete -> `ok: false`, `error.code` client `CLIENT_TIMEOUT`/`NETWORK_ERROR`, `unknown_outcome: true` (2.6).
  - `api.emit(event, payload, {timeout?})` (stessa forma, ack 3 s); `api.on(event, fn) -> unsubscribe`; un solo `io()`.
  - `state.get(domain)`, `state.subscribe(domain, fn) -> unsubscribe`, domini `connection`, `transport`, `daw`, `parts`, `scene`, `project`, `soundfont`, `ports`, `selection`, `prefs`; `state.setSelection(sel)` (forma 6.8); `prefs` in `localStorage` `matsynth.prefs.v1` con try/catch.
  - `shell.showView('live'|'studio'|'editor')`, `shell.openPanel('library'|'settings', section?)`: nessun comando musicale inviato.
  - `partPanel.mount(container, {partId}) -> {setPart(id), destroy()}`.
  - `library.registerSection(id, {title, render(container), onShow?})`.
  - `units.SNAP_OPTIONS`, `units.beatsToBBT(b, bpm_misura)`, `units.beatsToSeconds(b, bpm)`, `units.secondsToBeats(s, bpm)`.
- NON tocca: file Python, `startfluid.sh`, `settings.html`, `presets.html`.
- Corregge: A9 (ID duplicato `midi-file-input`), A12 (etichette snap).

**`ui-routing`** (backend)
- Possiede: nuovo `midi_router.py` (registro porte, normalizzazione nomi, alias/registro `midi_devices.json`, lettori input, risoluzione destinazioni, invio serializzato, tracciamento note, cleanup, PANIC, monitor, polling porte), nuovo `api_common.py` (`ok(**payload)`, `err(code, message, http, details=None)`, mappa codice -> HTTP di 2.3, helper ack), in `daw_recorder.py`: `_init_midi_ports`, `start_recording`/`stop_recording` (precondizioni, chiusura note/sustain), `_record_loop`, `_collect_playback_events`/`_playback_loop` (mute/solo immediati), `_metronome_loop`, `_send_all_notes_off`, `panic`, `stop_*`; in `app.py`: `restore_settings`/`startup_init_once` (parti + modalita'), `save_part`, `select_prog`, `set_effect`, `control`, `reset_channel`, `daw_preview_note`, route record/play/stop/panic/seek/mute/solo, `handle_cc_update`, `handle_effect_update`, `handle_transport_cmd`, route nuove `/api/system/*`, `/api/midi/*`, `/api/parts*`; `startfluid.sh` (sez. 10); `tests/helpers.py` (estensioni porte finte).
- Consuma: sez. 2, 3, 9, 10.
- **DAW autonomo**: `MultiTrackDAW(bpm, socketio)` senza argomento router DEVE continuare a funzionare come oggi, creando un router di default **solo in memoria** (registro bootstrap in RAM dalle porte enumerate con le quattro funzioni mido che `tests/helpers.make_daw` intercetta, parti di default 3.2, modalita' `legacy`, nessuna scrittura su disco). `app.py` passa invece il router configurato (registro `midi_devices.json`, parti da `last_state.json`, modalita' effettiva). Cosi' i test "invariati" di 11.4 che istanziano il DAW direttamente restano validi.
- Espone a `ui-persistence`: `midi_router` API Python `get_parts()`, `apply_part(part) -> report`, `release_part_notes(part_id)`, `port_status(alias, direction)`, `send_internal(msg)`, `send_track(track_id, msg)`; `app.save_part(part)`.
- NON tocca: import/export, undo, clip, preset/scene/progetti, frontend.
- Corregge: A3, A4, A5, A6, A10a, A23, A25.

**`ui-persistence`** (backend)
- Possiede: nuovo `project_store.py` (schemi scena/progetto, validatore, `migrate_legacy_preset`, `scene_to_legacy_preset`, `atomic_write_json`, `fluid_bank_to_smf`/`smf_to_fluid_bank`, `project_state`/`scene_state`/`mark_dirty`, `library.json`, stato SoundFont); in `daw_recorder.py`: `revision`, `_edit_lock`, hook `on_content_change`, `_push_undo`/`_restore_snapshot` (revisione), funzione di accoppiamento unica (6.2) e suoi utenti (`quantize_tracks`, `get_track_activity`, `_build_intervals_full`), `import_midi`, `export_midi`, `edit_event`/`delete_event`/`edit_note_pair`/`delete_note_pair`/`add_note`/`transpose_notes` (solo `expected_revision`, validazione prima dell'undo), `rename_track`/`set_track_color` (undo), `set_bpm`/`set_time_signature` (emit), serializzazione/caricamento dello stato DAW; in `app.py`: route preset legacy, `api_capture_current_config`, `load_sf2`, `daw_midi_*`, route scene/progetti/SoundFont nuove, `revision` nelle route DAW elencate in 8.1 con fase "persistence".
- Consuma: sez. 2, 3 (via API Python di `ui-routing`), 4, 5, 6.1-6.3, 6.5-6.7, 7, 9.
- Espone a `ui-studio`: `daw.revision`, `daw._edit_lock`, helper `check_revision(expected)` -> errore 409, funzione di accoppiamento e `_get_note_pair_indices_in_range`, `project_store.mark_dirty`.
- NON tocca: operazioni clip/appunti/duplica/cut (di `ui-studio`), router, frontend.
- Corregge: A1, A2 (backend), A7, A10b, A13, A14, A20, A21, A22, A26, A29.

**`ui-live`** (solo frontend)
- Possiede: `templates/partials/live.html`, `static/js/live.js`, `static/js/library_scenes.js` (sezioni Scene e SoundFont della Libreria), `static/css/live.css`; e' l'**unica** fase che estende `static/js/part_panel.js` dopo `ui-shell` (scheda Routing, campi suono esterno, `controls` dichiarati, avvisi conflitti/legacy).
- Consuma: 2, 3, 4, 8.2 (parti, porte, scene, SoundFont), 9.2.
- NON tocca: file Python, `legacy_daw.js`, `studio*.js`, `midi_editor.js`.
- Corregge: A2 (nessun reload: richiamo dalla shell), A11 (preferiti, reset canale con conferma, uscita MIDI selezionabile nell'UI).

**`ui-studio`** (backend editing + frontend)
- Possiede: in `daw_recorder.py`: `copy_selection`, `paste_at`, nuove `cut`, `create_clip`, `duplicate_clip`, `clear_range`, `duplicate_track`, `move_clip`, `resize_clip`, `split_clip`, `merge_clips`, `delete_clip`, `transpose_clip`, `_finalize_recording_clips` (take separato), `get_clips`, `event_clips` (ID clip per evento, D3), `next_clip_id` monotono, `edit_notes_batch`/`delete_notes_batch`; in `app.py`: route clip, copy/paste/cut, duplicate, clear/clear_range, nuove `notes/batch`; frontend `templates/partials/studio.html`, `static/js/studio.js`, `static/js/library_projects.js` (sezioni Progetti e MIDI), `static/css/studio.css`; rimuove da `legacy_daw.js` le parti timeline/tracce/trasporto sostituite.
- Consuma: 2, 5.6 (operazioni progetto), 6, 7 (import/export UI), 8, 9; monta `part_panel.js` senza modificarlo.
- NON tocca: router, `project_store.py` (salvo chiamare la sua API), `part_panel.js`, `live.js`.
- Corregge: A8, A11 (punti A/B diretti, Taglia), A15, A16, A17, A18, A19, A24, A27, A28.

**`ui-editor`** (frontend; backend solo correzioni coordinate)
- Possiede: `templates/partials/editor.html`, `static/js/midi_editor.js`, `static/css/editor.css`; rimuove da `legacy_daw.js` piano roll ed event viewer.
- Consuma: 1.2-1.3, 2, 6 (coppie, revisione, selezione note, batch, cut/paste), 8.1-8.2, 9.
- NON tocca: `studio.js` salvo il punto di apertura/ritorno concordato, router, `project_store.py`.

**`ui-offline-settings`** (solo frontend/template/asset)
- Possiede: `static/vendor/` (client Socket.IO, Bootstrap, icone indispensabili, con licenze), i link di asset nel `<head>` di `index.html`/`settings.html`/`presets.html` (solo quelli), la prima riga di `static/css/style.css` (font remoto), `templates/settings.html` e `templates/presets.html` (diventano pagine che aprono il pannello della shell), `templates/partials/settings.html`, `static/js/settings.js`, `static/css/settings.css`.
- Consuma: 2, 3.4-3.5 (associazione dispositivi), 8.2 sistema/porte/dispositivi, 10 (UI della modalita' con stato effettivo/configurato e riavvio esplicito).
- NON tocca: file Python (le route `/settings`, `/presets` restano), `startfluid.sh`.

### 11.3 Anomalie: dove si correggono

| # | Anomalia (baseline `docs/handoff/ui-baseline.md` 1-11, poi nuove trovate in questa fase) | Fase | Test che cambia |
| --- | --- | --- | --- |
| A1 | Import SMF: LSB perso, ultimo PC vince, PC nel tempo persi | ui-persistence | `test_DESIRED_import_keeps_bank_msb1_lsb2_program7` e `test_DESIRED_program_change_at_nonzero_beat_is_kept_at_that_beat` -> normali; `test_import_bank_lsb_is_dropped_and_last_program_change_wins` rimosso/invertito; `test_midi_upload_applies_bank_msb_only_to_synth` -> `select 0 1 1 7` |
| A2 | Preset: pan ignorato, canali non persistiti, SF non caricato, reload pagina | ui-persistence (backend), ui-live (no reload) | `test_DESIRED_apply_pan_sends_cc10` -> normale; `test_apply_pan_is_ignored_today` rimosso; `test_apply_persists_globals_but_not_channels_in_state_file` aggiornato (canali persistiti) |
| A3 | Record/Play fittizi senza porte | ui-routing | `test_DESIRED_record_without_input_port_is_refused` -> normale; `test_record_without_input_port_sets_is_recording_but_records_nothing` e `test_playback_without_output_port_exits_leaving_is_playing_true` sostituiti dai test di rifiuto |
| A4 | Porta ambigua: vince la prima | ui-routing | `test_input_selection_ambiguous_takes_first_no_error` -> stato `ambiguous`, nessuna apertura |
| A5 | Nessun invio live in Record (monitor ALSA) | ui-routing (`managed`) | `test_record_does_not_thru_to_output` resta per `legacy`; nuovi test monitor ON/OFF in `managed` |
| A6 | PANIC solo CC123 | ui-routing | `test_panic_sends_cc123_on_all_16_channels` aggiornato (filtrare `type == 'control_change'`: con note-off espliciti `m.control` non esiste) |
| A7 | Import senza clip | ui-persistence | `test_import_does_not_create_clips` -> un clip per traccia importata |
| A8 | "Cut Clip at Playhead" usa `dawState.current_beat` mai valorizzato | ui-studio | test browser/JS (beat = `timeline_position * bpm / 60`) |
| A9 | ID DOM duplicato `midi-file-input` | ui-shell | verifica statica (nessun ID duplicato nella pagina composta) |
| A10a | `/set_effect` senza whitelist | ui-routing | nuovo test 400 |
| A10b | `api_presets_rename` sovrascrive | ui-persistence | nuovo test `409 NAME_EXISTS`; `test_rename_and_delete` resta |
| A11 | Funzioni senza UI (preferiti, reset canale, uscita MIDI, A/B diretti, Taglia) | ui-persistence (backend preferiti), ui-live, ui-studio | checklist browser |
| A12 | Etichette snap ambigue (`#daw-snap-grid`, `#pr-snap-grid`) | ui-shell | verifica statica/JS di `units.SNAP_OPTIONS` |
| A13 | Import Aggiungi non annullabile; Sostituisci via `clear_all_tracks` | ui-persistence | nuovo test: un undo annulla l'intero import |
| A14 | Rinomina/colore fuori cronologia ma ripristinati dagli snapshot | ui-persistence | nuovo test |
| A15 | `copy_selection` non rispetta le coppie | ui-studio | nuovo test taglia/incolla a cavallo del confine |
| A16 | `clear_range` elimina clip parzialmente sovrapposti | ui-studio | nuovo test |
| A17 | `duplicate_track` senza clip, sostituzione silenziosa | ui-studio | nuovo test |
| A18 | Clip sovrapposti (overdub, move, paste): ownership ambigua | ui-studio | nuovi test ownership per ID (D3): move/delete di un clip sovrapposto non tocca l'altro |
| A19 | `next_clip_id` ripristinato dall'undo (ID riusabili) | ui-studio | nuovo test |
| A20 | `set_bpm`/`set_time_signature` senza `daw_state_update` | ui-persistence | nuovo test con socket finto |
| A21 | `_push_undo` prima della validazione (`edit_event`, `edit_note_pair`) | ui-persistence | nuovo test |
| A22 | Import applica `select` a FluidSynth per tutti i canali, anche esterni | ui-persistence | nuovo test con parte esterna |
| A23 | Record durante Play avvia un secondo thread di playback (evidenza statica) | ui-routing | nuovo test `409 TRANSPORT_ACTIVE` |
| A24 | Il client riporta la posizione all'inizio del Record dopo lo stop (`index.html` handler `daw_state_update`, `recordStartPosition`). **Decisione utente (D6): il comportamento resta** (serve a registrare piu' strumenti sullo stesso intervallo), ma passa al backend come opzione `return_to_start` (default on) valida solo dopo Stop di Record | ui-studio | test unittest su `stop_recording` (ritorno solo dopo Record, non dopo Play; opzione off; BPM cambiato) + test browser. Nel frattempo `legacy_daw.js` (ui-shell) conserva il comportamento attuale |
| A25 | Mute/solo in playback agiscono solo al giro successivo (evidenza statica) | ui-routing | nuovo test |
| A26 | Accoppiamento note diverso tra funzioni (FIFO vs sovrascrittura) | ui-persistence | nuovo test con note sovrapposte della stessa altezza |
| A27 | `paste` con appunti vuoti risponde `ok` con `pasted: 0` (falso successo) | ui-studio | nuovo test `409 EMPTY_CLIPBOARD` |
| A28 | `edit_notes_batch` salta in silenzio voci invalide; nessuna route batch | ui-studio | nuovo test `422` |
| A29 | Nessun lock nel DAW (controllo + mutazione non atomici) | ui-persistence | test di concorrenza leggero (due thread, una revisione attesa) |

### 11.4 Destino dei 40 test di `tests/test_baseline_smoke.py`

Restano **invariati** (devono continuare a passare dopo ogni fase): `test_instantiate_without_ports`, `test_input_not_found_leaves_none` (semantica: input `unbound`/assente), `test_record_note_pair_creates_events_and_clip`, `test_record_filters_unarmed_channels_and_non_channel_messages`, `test_record_does_not_thru_to_output` (legacy), `test_record_requires_armed_track`, `test_add_note_undo_redo`, `test_mute_excludes_track_from_playback`, `test_workload_fixture_shape`, `test_workload_import_export_import_is_stable`, `test_export_structure`, `test_import_refused_while_playing_or_recording`, `test_sustain_and_pitchbend_survive_round_trip`, `test_export_writes_bank_msb1_lsb2_program7_from_channel_programs`, `test_list_sorted_by_name_case_insensitive`, `test_load_returns_file_content`, `test_load_missing_is_404`, `test_apply_decay_cc75_bank_program_and_globals`, `test_apply_does_not_load_soundfont`, `test_apply_minimal_uses_defaults_and_no_channel_commands`, `test_apply_missing_preset_is_400`, `test_save_produces_legacy_shape_and_dedupes_name`, `test_rename_and_delete`.

"Invariati" = stesse asserzioni. Il **setup** PUO' cambiare solo in `tests/helpers.py` (es. nomi delle porte finte resi realistici, `"FLUID Synth (1234):Synth input port (1234:0) 128:0"`; oggi `FakeMidiOutput` si chiama `"FLUID Synth Fake"`, compatibile con la regola "inizia con"), mai nel corpo dei test.

Cambiano (la fase indicata aggiorna il test nello stesso intervento e lo scrive nell'handoff):

| Test | Fase | Nuova aspettativa |
| --- | --- | --- |
| `test_ports_opened_through_real_selection_logic` | ui-routing | apertura tramite registro/bootstrap |
| `test_input_selection_keyword_priority_and_midi_through` | ui-routing | diventa test del bootstrap (priorita' usata solo per creare `keyboard-main`) |
| `test_input_selection_ambiguous_takes_first_no_error` | ui-routing | A4 |
| `test_output_selection_fluid_or_128_0` | ui-routing | interno = `client` che inizia con `"FLUID Synth"`, niente `128:0` |
| `test_record_without_input_port_sets_is_recording_but_records_nothing` | ui-routing | rimosso (A3) |
| `test_DESIRED_record_without_input_port_is_refused` | ui-routing | togliere `expectedFailure` |
| `test_playback_sends_note_pair_on_track_channel_then_all_notes_off` | ui-routing | resta la coppia note-on/off; l'asserzione sui CC123 si adatta al nuovo cleanup |
| `test_panic_sends_cc123_on_all_16_channels` | ui-routing | A6 |
| `test_playback_without_output_port_exits_leaving_is_playing_true` | ui-routing | rimosso (A3) |
| `test_import_does_not_create_clips` | ui-persistence | A7 |
| `test_import_bank_lsb_is_dropped_and_last_program_change_wins` | ui-persistence | rimosso (A1) |
| `test_DESIRED_import_keeps_bank_msb1_lsb2_program7` | ui-persistence | togliere `expectedFailure` |
| `test_DESIRED_program_change_at_nonzero_beat_is_kept_at_that_beat` | ui-persistence | togliere `expectedFailure` |
| `test_apply_pan_is_ignored_today` | ui-persistence | rimosso (A2) |
| `test_DESIRED_apply_pan_sends_cc10` | ui-persistence | togliere `expectedFailure` |
| `test_apply_persists_globals_but_not_channels_in_state_file` | ui-persistence | i canali applicati sono persistiti |
| `test_midi_upload_applies_bank_msb_only_to_synth` | ui-persistence | `select 0 1 1 7` |

`tests/test_contract_examples.py` (questa fase) DEVE continuare a passare; se `ui-persistence` porta il validatore in `project_store.py`, il test DEVE importare quello reale e conservare tutti i casi.

---

## 12. Criteri di accettazione per fase

Comando di test unico (brief): `python -m unittest discover -s tests -p "test_*.py" -v`, 0 fallimenti; i test non richiedono rtmidi, hardware o rete (porte `FakeMidiInput`/`FakeMidiOutput` con `mido.Message` reali, `send_fluid` registrato). Ogni fase compila la colonna Verifica di `docs/regression-checklist.md` per le righe che tocca e scrive `docs/handoff/<fase>.md`. "Verificato" solo per cio' che e' stato eseguito; il resto e' "da verificare su hardware/browser".

**ui-shell**
- [ ] Pagina composta senza errori JS (`node --check` su ogni `static/js/*.js`; nessun ID duplicato nel markup composto, A9).
- [ ] Un solo `io()` nel codice; `api.request`/`api.emit` gestiscono busta nuova, busta legacy, array/testo legacy, timeout = `unknown_outcome` (test JS o test Python che estrae ed esegue con Node).
- [ ] Tutte le funzioni della checklist sez. 1 raggiungibili come prima (nessuna rimossa); cambio vista non invia richieste musicali (verifica statica: `shell.showView` non chiama `api`).
- [ ] `units.SNAP_OPTIONS` = tabella 1.3; le select snap la usano (A12).
- Da verificare in browser: 20 cambi Suona/Studio/editor con un solo socket e nessun listener/timer duplicato; viewport di piano sez. 10.

**ui-routing**
- [ ] Traccia logica 0 -> una sola coppia note-on/off su FluidSynth canale 0; traccia 1 instradata a esterno canale 4 -> solo output esterno canale 4 (playback, preview, monitor).
- [ ] `managed`, monitor esterno OFF: eventi registrati, zero reinvii; ON: un solo invio per evento. `legacy`: zero inoltri live.
- [ ] Porta mancante/ambigua: stato corretto, nessun fallback, Record rifiutato prima di `is_recording` (`test_DESIRED_record_without_input_port_is_refused` normale), Play senza destinazioni rifiutato.
- [ ] Stop/loop/seek/PANIC/cambio routing/mute in playback con note tenute e sustain: dopo l'operazione ogni note-on inviato ha il suo note-off sulla stessa porta/canale e CC64=0 inviato (contabilita' su `FakeMidiOutput`).
- [ ] Click solo su destinazione interna canale 9 note 76/77, mai registrato, mai su esterni.
- [ ] `normalize_port_name` sui nomi d'esempio di 3.4; bootstrap `keyboard-main` (unico / ambiguo / assente).
- [ ] `startfluid.sh`: `bash -n`; con `midi_mode` assente/`legacy`/`managed`/JSON corrotto produce rispettivamente autoconnect 1/1/0/1 (test con `jq` se disponibile, altrimenti revisione manuale annotata).
- [ ] Busta 2.1 su route nuove e ack socket; `/set_effect` con whitelist (A10a).
- Da verificare su hardware: nomi porte reali sul Pi, FANTOM-08 in input e output, Local ON + monitor OFF / Local OFF + monitor ON senza note doppie, scollegamento/ricollegamento con note e sustain attivi, `managed` dopo riavvio e rollback (sez. 10.3), jitter.

**ui-persistence**
- [ ] Preset legacy: stessi bank/program, CC, globali, pan quando presente e decay CC75 (via route legacy e via migrazione v1: `scene_from_legacy_decay.json`); `test_DESIRED_apply_pan_sends_cc10` normale.
- [ ] Bank MSB 1, LSB 2, PC 7: export/import mantiene i valori (parte esterna esatta; `channel_programs` = 130/7); PC a beat 4 resta a beat 4 (due test DESIRED normali).
- [ ] Progetto round-trip: eventi (beat esatti), clip/ID, `next_clip_id`, nomi/colori, mute/solo, parti/routing, snapshot suono, BPM/metro/loop equivalenti; all'apertura trasporto fermo, arm off, `revision` cambiata, undo vuoto.
- [ ] File invalido / versione futura / errore disco simulato (eccezione in `os.replace` o `ENOSPC` simulato): sessione precedente intatta, errore esplicito con codice 2.3, nessun file parziale sostituisce il precedente, temporaneo rimosso.
- [ ] Richiesta indicizzata con revisione obsoleta: `409 REVISION_CONFLICT`, nessuna modifica.
- [ ] `dirty`: impostato da ogni mutazione elencata in 5.6 e da modifiche di parte; azzerato solo da salva/apri/nuovo; `409 UNSAVED_CHANGES` su Nuovo/Apri senza `discard_changes`.
- [ ] Richiamo scena: `409 TRANSPORT_ACTIVE` con Play/Record attivi; SoundFont diverso/mancante -> 409 senza effetti; porte assenti -> 409 o parziale con consenso; fallimento a meta' -> `503 PARTIAL_APPLY`.
- [ ] Stato SoundFont `loaded` solo dopo verifica `fonts` (con `send_fluid` registrato).
- [ ] Workload 16x500: apertura/salvataggio di un progetto da 16000 eventi sotto `MAX_PROJECT_BYTES`; misura RSS su PC annotata come tale.
- Da verificare su hardware: tempi di caricamento SF2 reali, picco RAM su Pi in Apri, convenzione bank GS con FluidSynth reale (D5).

**ui-live**
- [ ] Cambio suono e richiamo scena senza reload/navigazione e senza riavvio servizio (test statico: nessun `location.href`/`reload` nel flusso; `fetch` verso `/api/scenes/<id>/recall`).
- [ ] Play/Record attivi: richiamo bloccato con spiegazione e Stop, cambio vista consentito.
- [ ] Salva / Scarta / Annulla su `UNSAVED_CHANGES`; Salva come con nome esistente chiede conferma (`overwrite_id`); nessun suffisso silenzioso.
- [ ] Parte esterna: campi MSB/LSB/PC, nome utente, solo i CC dichiarati; nessun nome FluidSynth mostrato per l'esterno; avvisi conflitti e `MONITOR_HANDLED_BY_ALSA`.
- Da verificare in browser/hardware: smartphone 360x800 e 390x844, target >= 44 px, nessun gesto nascosto.

**ui-studio**
- [ ] Taglia/incolla note a cavallo di un confine: coppie complete, beat/durata corretti, un solo undo; split/move/merge senza note-off orfani; clip sovrapposti: ogni operazione tocca solo gli eventi del proprio ID (D3).
- [ ] Nessun comando riferito all'"ultimo clip"; comandi disabilitati senza selezione (verifica statica: nessun uso di `getLastClip`).
- [ ] Duplica traccia: `include_config` e `confirm_overwrite` rispettati; variante legacy conservata.
- [ ] Stop di Play/Pausa senza rewind implicito; Stop di Record riporta la posizione al punto di inizio Record se `return_to_start` (default on), non se off (A24/D6, decisione utente); seek in Record non offerto.
- [ ] Progetti: Nuovo/Apri/Salva/Salva come/upload/download dalla Libreria con gestione di tutti gli errori 5.6.
- Da verificare in browser/hardware: tablet 768x1024 e 1024x768, registrazione e selezione clip con touch, prova musicale sez. 12 del piano (interno + esterno, overdub, click, mute/solo).

**ui-editor**
- [ ] Modifica batch, trascinamento, velocity: una richiesta `notes/batch` con `expected_revision` per gesto, un livello di undo.
- [ ] `409 REVISION_CONFLICT` -> selezione note invalidata e note map riletta, mai applicata a note diverse.
- [ ] Strumenti seleziona/disegna/cancella espliciti; zoom/scroll non creano note (test di logica dei gestori dove possibile).
- [ ] Eventi (CC, sustain, pitch bend, pressure, bank/program) visualizzati e modificabili per indice con revisione.
- Da verificare in browser: editing touch su tablet, nessuna azione solo hover/tasto destro/pressione lunga.

**ui-offline-settings**
- [ ] Nessun riferimento a CDN o Google Fonts nei template e nel CSS (grep); asset in `static/vendor/` con licenze.
- [ ] `/settings` e `/presets` aprono il pannello della shell; impostazione modalita' mostra effettiva/configurata e richiede riavvio esplicito.
- Da verificare: app aperta dall'hotspot con tutte le richieste esterne bloccate; viewport di piano.

**ui-acceptance**: checklist completa, prove hardware sez. 12 del piano (1-6), misura 16x500 su Pi con `tools/pi_baseline.sh` prima/dopo, documentazione utente/deployment/rollback. Non attivare `managed` come default.

---

## 13. Deviazioni e punti da confermare con l'utente

Ogni voce: scelta adottata in questo contratto (le fasi la implementano) e raccomandazione. Se l'utente decide diversamente, aggiornare questo documento prima della fase interessata.

| # | Punto | Scelta adottata / raccomandazione |
| --- | --- | --- |
| D1 | I preset legacy non contengono mai `pan` (`api_capture_current_config` non lo produce). Il piano chiede "default espliciti". | Nella migrazione v1 `pan` assente = **64 (centro)**: il richiamo di un vecchio preset con la nuova UI centra il pan (prima restava invariato). Warning `LEGACY_FIELD_DEFAULTED` visibile. Alternativa: "invariato" (richiede campi opzionali nelle parti: piu' complesso). **Confermato dall'utente.** |
| D2 | Canali non elencati in un preset legacy (es. `legacy_decay.json` ha solo 0, 3, 9). | Migrazione v1 = parte di default completa (piano: default espliciti); la route legacy `/api/presets/apply` continua a non toccarli. Il richiamo dalla nuova UI resetta quei canali al default. **Confermato dall'utente.** |
| D3 | Clip sovrapposti: il piano non li vieta, ma con ownership per intervallo producono spostamenti sbagliati. | **DECISIONE UTENTE (sostituisce la raccomandazione iniziale "invariante + fusione"): ID clip per evento.** Ogni evento porta l'ID del clip che lo possiede (0 = libero); i clip possono sovrapporsi e un overdub resta un take separato ("[Take 1][Take 2]" come nel mockup). Vedi 5.2, 6.1, 6.3, 6.4. Costo: +1 intero per evento (fixture 16 x 500 coppie: ~16000 eventi, ordine di 100-200 KiB in piu'); ogni mutazione degli eventi DEVE preservare l'ID. |
| D4 | Ridimensionamento in riduzione: oggi "non distruttivo" ma le note fuori continuano a suonare senza clip. | Riduzione consentita solo fino all'ultimo evento posseduto (`409 CLIP_RESIZE_WOULD_EXCLUDE`). Per accorciare davvero: Taglia/Cancella intervallo, poi ridimensiona. Alternativa: riduzione distruttiva. **Confermato dall'utente.** |
| D5 | Bank interni in SMF: export di oggi `(bank>>7, bank&127)` (bank 128 -> MSB 1; bank 5 -> LSB 5), import solo MSB. | Convenzione GS di FluidSynth (`synth.midi-bank-select=gs`, default): MSB = bank, LSB 0; percussioni 128 su canale 10 -> MSB 0; bank >= 128 su altri canali non rappresentabile (warning). **Da verificare con FluidSynth reale** prima di chiudere `ui-persistence`. |
| D6 | `index.html` dopo lo stop del Record riporta la posizione all'inizio del take (`recordStartPosition`); il piano vieta il rewind implicito allo Stop. | **DECISIONE UTENTE (sostituisce la raccomandazione iniziale): il ritorno al punto di partenza dopo Record si MANTIENE**, per registrare altri strumenti sullo stesso intervallo. Solo dopo Stop di Record (mai dopo Play), opzione `return_to_start` attiva di default e disattivabile, implementata nel backend (vedi sez. 6, "Ritorno al punto di partenza dopo Record"). Deroga esplicita al "senza rewind implicito" del piano, limitata a questo caso. |
| D7 | Record con alcune tracce armate senza input: il piano chiede di fallire se *nessun* input e' utilizzabile. | Si rifiuta se **una qualsiasi** traccia armata non ha input utilizzabile (nessun take parziale silenzioso). **Confermato dall'utente** (implementato in `ui-routing`). |
| D8 | Record premuto durante Play. | `409 TRANSPORT_ACTIVE` (nessun punch-in nella prima versione); oggi avvia un secondo playback (A23). |
| D9 | Richiamo scena con SoundFont diverso. | Due passi lato client (carica SF con stato osservabile, poi richiama), non una transazione backend unica: piu' semplice, nessun lock lungo, esito di ciascun passo visibile. Tra i due passi i canali suonano col nuovo SF e i vecchi programmi. |
| D10 | Flag `legacy/managed`. | Chiave `midi_mode` in `last_state.json` (gia' letto da `startfluid.sh` con `jq`); cambio solo con riavvio esplicito; default `legacy`. |
| D11 | Struttura CSS. | Un file per vista in `static/css/` invece di sezioni in un unico `style.css`, per permettere fasi frontend parallele senza conflitti. |
| D12 | Import MIDI sostitutivo con progetto aperto. | Il progetto viene staccato dal file (`id = null`, nome = file MIDI): Salva non sovrascrive per errore il progetto originale. |
| D13 | Compatibilita' con `index.html` attuale. | Campi nuovi assenti = comportamento legacy (`expected_revision`, `discard_changes`, `include_config`/`confirm_overwrite`) finche' la vecchia UI esiste; i client nuovi DEVONO inviarli. Comporta che la vecchia UI non riceve le nuove protezioni. |
| D14 | Rinomina/colore traccia diventano livelli di undo. | Coerente con "un gesto = un livello" e corregge A14; consuma livelli dei 10 disponibili. |
| D15 | PANIC "dove supportati". | Il supporto non e' rilevabile: si inviano sempre CC64=0, CC123, CC120 e note-off tracciati. Efficacia sugli strumenti esterni da verificare su hardware. |
| D16 | "Niente suffissi silenziosi" vs `api_presets_save` (suffisso `_2`, test `test_save_produces_legacy_shape_and_dedupes_name`). | La route legacy resta invariata finche' serve `presets.html`; la nuova UI usa solo `/api/scenes` (409 + conferma). |
| D17 | Orari e Pi senza RTC. | Orari solo informativi; il controllo "file cambiato" usa `st_mtime_ns` + dimensione. |
| D18 | Campi sconosciuti nei documenti v1. | Rifiutati (`VALIDATION_FAILED`): ogni aggiunta futura incrementa `schema_version`. I preset legacy invece li tollerano (restano nel file originale). |
| D19 | Ingresso "di default". | `input.device_alias: null` = nessun ingresso; il default migrato usa l'alias esplicito `keyboard-main` creato dal bootstrap. |
| D20 | Destinazione dei comandi sonori interni (console 9800 o porta MIDI interna). | Lasciata a `ui-routing` purche' sia una sola via per tipo di comando e documentata; esito sempre "inviato", non "confermato". |
| D21 | Misure RAM. | Fatte su PC 64 bit (5.7); le soglie (`MAX_PROJECT_BYTES`, `MAX_EVENTS_TOTAL`) vanno confermate con la misura sul Pi (`docs/pi-measurement.md`), non ancora eseguita. |
| D22 | Svuota traccia/tutto durante il Record (`ui-studio`, review M2). | 6.4 diceva "come oggi"; ora `409 RECORDING_ACTIVE` in Record (svuotare rompeva gli indici del take e l'ownership dei clip). In Play resta consentito. |
| D23 | Duplica traccia con `include_config: true` (`ui-studio`). | Copiati destinazione, suono, CC e monitor; l'ingresso della parte destinazione NON viene copiato (evita due parti sullo stesso ingresso); per destinazione interna il canale resta quello della parte destinazione. Da confermare con l'utente. |

Nessuna contraddizione bloccante trovata nel piano. Le tensioni principali (D1/D2 default espliciti vs compatibilita' legacy, D6 rewind dopo Record: deciso dall'utente di mantenerlo come opzione, D16 suffissi legacy) sono risolte dando priorita' al piano per la UI nuova e conservando il comportamento legacy solo sulle route legacy.

