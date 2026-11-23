# Handoff `ui-offline-settings`

## Implementato

Solo frontend/template/asset (nessun file Python, `startfluid.sh`, `legacy_daw.js`, `live.js`, `studio*.js`, `part_panel.js` toccati), come da `docs/contracts.md` 11.2.

- **Asset locali** `static/vendor/` (`README.md` con versione, licenza, URL, SHA-512 dei tarball e SHA-256 dei file): Bootstrap 5.3.0 (css + bundle js), Font Awesome Free 6.4.0 (`fontawesome.min.css` + `solid.min.css` + `fa-solid-900.woff2`: l'interfaccia usa solo `fas`), Socket.IO client 4.5.4. Licenza accanto a ciascuno. ~600 KB. `.gitattributes` locale `* -text` per non alterare gli hash con la conversione dei fine riga.
- **Niente CDN/Google Fonts**: `<head>` di `index.html` e `presets.html` puntano a `/static/vendor/...` (Socket.IO resta nel `<head>` prima di `api.js`); bundle Bootstrap a fine body sostituito 1:1. Prima riga di `static/style.css` (la `@import` Google Fonts stava li', non in `static/css/style.css` che non aveva font remoti): ora commento; Orbitron/Roboto ricadono su `sans-serif` come dichiarano le regole `font-family` (aspetto del titolo diverso: da valutare).
- `templates/settings.html`: pagina minima con `meta refresh` verso `/?panel=settings` (funziona senza JS, nessun asset). `/settings` resta valido.
- `templates/partials/settings.html` + `static/js/settings.js` + `static/css/settings.css`: pannello con 5 schede.
  - **Connessione**: stato del socket (live), hostname `.local`, indirizzi (`GET /api/network`) con URL di accesso; stato vuoto "nessuna rete". Non dimostra connettivita': lo dice.
  - **Audio e hardware**: scheda ALSA (`/api/audio_devices`), tastiera ALSA per la modalita' legacy (`/api/midi_devices`, e' la `midi_device` che `startfluid.sh` collega: concetto distinto dagli alias), "Salva e riavvia" con conferma esplicita nel pannello; invia solo i campi cambiati.
  - **MIDI**: porte reali (`GET /api/midi/ports`, `?all=1` con motivo di esclusione), dispositivi/alias (`GET /api/midi/devices`): crea (regex alias = `midi_router.ALIAS_RE`), associa/scollega ingresso e uscita (`PUT`, `from_port`/`null`), scelta tra porte ambigue (`POST .../select`, solo se `ambiguous`), elimina con conferma (`DEVICE_IN_USE` mostra le parti). Si rilegge su evento `ports_status` (nessun polling), senza perdere i messaggi di esito.
  - **Modalita'**: `GET /api/system/status` (effettiva, configurata, `restart_required`, warning), `PUT /api/system/mode`; managed etichettata "sperimentale, non verificata su hardware" e richiede conferma; rollback spiegato (scegli Legacy + riavvia / chiave `midi_mode` a mano); riavvio solo su richiesta esplicita e attivo solo se `restart_required`.
  - **SoundFont (provvisoria)**: `/list_sf2`, `/get_state`, `GET /load_sf2/<f>` (timeout lungo di `api.js`), con conferma e blocco in Play/Record; esito = testo FluidSynth, dichiarato "inviato". Serve perche' oggi `/settings` e' l'UNICO posto che la offre (checklist 11-12); ui-live la sostituisce con Libreria > SoundFont e la scheda va tolta.
- **Riavvio, esito reale**: `POST /api/save_hardware` risponde 200 PRIMA del riavvio (thread con `sudo systemctl`, esito non riportato). `settings.js` mostra "richiesto", poi osserva `GET /api/system/status` ogni 2 s (e la disconnessione del socket): **confermato solo se il servizio sparisce e ritorna**; mai sparito in 20 s = "NON confermato" (possibile mancanza di permessi sudo); non di ritorno in 90 s = "irraggiungibile". Un esito sconosciuto della POST avvia comunque l'osservazione. Riavvio bloccato in Play/Record, avviso se progetto/scena non salvati. L'unico ciclo temporizzato e' questa attesa.
- **Apertura**: `settings.js` osserva `hidden` di `#ms-panel-settings` (dati letti a ogni apertura, nessuna richiesta all'avvio), avvolge `MatSynth.shell.openPanel` e legge `data-section` per honorare `openPanel('settings', section)` (`connessione|audio|midi|modalita|soundfont`), e gestisce il deep link `/?panel=settings|library&section=...` (poi pulisce l'URL).
- **`/presets` (deviazione)**: `presets.html` ha solo gli asset nel `<head>` aggiornati e una sonda: se esiste `static/js/library_scenes.js` (file di ui-live) rimanda a `/?panel=library&section=scene`; altrimenti resta la pagina preset funzionante. Motivo: il segnaposto "Scene" della Libreria (in `shell.js`) rimanda a `/presets`, e `presets.html` e' oggi l'unica gestione dei preset (checklist 4-9): rimandare subito alla shell farebbe girare in tondo e perderebbe salva/richiama/rinomina/elimina. `/presets?legacy=1` forza la vecchia pagina anche dopo.
- Test: `tests/js/settings.test.js` (16 test: funzioni pure, nessun URL esterno nei file app, asset vendor e licenze, ordine Socket.IO/api.js, una sola chiamata a `save_hardware`, niente `fetch`/`setInterval`/`confirm`). `docs/regression-checklist.md`: colonna Verifica compilata per le righe 11, 12, 62-66 (solo cio' che e' stato davvero provato).

## Verificato e come

    node --check static/js/settings.js                      -> ok
    node --test tests/js                                     -> 63/63 ok (47 precedenti + 16 nuovi)
    <python venv> -m unittest discover -s tests -p "test_*.py" -> 112 test (86 + quelli di ui-persistence in corso), OK (expected failures=3)

Prova offline reale (Edge headless con `--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"`, pilotato via DevTools Protocol, backend di `serve_ui.py` con hardware finto e dati in cartella temporanea, porta 5066 perche' la 5055 era condivisa con altri server di altre fasi): `/`, `/settings`, `/presets`, ogni scheda, a 360x800, 390x844, 768x1024, 1024x768, 1366x768.
- 0 richieste non locali (tutte `127.0.0.1`); `static/vendor/*` tutti 200 (bootstrap css+js, fontawesome css+woff2, socket.io); font `Font Awesome 6 Free` caricato (icone visibili negli screenshot); socket connesso ("Connesso"); 0 errori JS; `scrollWidth == innerWidth` a ogni larghezza e nessun elemento del pannello oltre il viewport. Con i CDN bloccati la versione precedente andava in errore (`io` nullo): ora no.
- 404 attesi solo su `favicon.ico` e sulle route di `ui-persistence` non ancora presenti all'avvio (`/api/projects/current`, `/api/scenes/current`, `/api/soundfont/status`).
- Interazioni provate nel browser contro il server locale: modalita' `managed` (conferma, `PUT`, "configurata Managed / in uso Legacy / riavvio necessario", pulsante riavvio attivo); riavvio senza che il servizio si fermi -> "NON confermato" dopo 20 s; riavvio con server fermato e rilanciato a mano -> "di nuovo raggiungibile" (con avviso "console FluidSynth NON disponibile"); alias: validazione, creazione, eliminazione con esito mostrato.
- Screenshot: `C:\Users\matteo\AppData\Local\Temp\claude\536d55de09776702\54c8da1e-11c7-471f-ac18-6ec8deca92f7\scratchpad\shots\offline_*.png` (fuori dal repo).
- Grep: nessun `http(s)://` esterno ne' `googleapis|gstatic|cdn.` in `templates/` e `static/` (esclusi `static/vendor/`, `app.old*`, `indexold`, `loop`). In `static/vendor/` restano solo URL in commenti di licenza e lo xmlns SVG di Bootstrap, non operativi.
- Integrita' asset: SHA-512 dei tre tarball npm = `dist.integrity` del registro (vedi `static/vendor/README.md`).

## NON verificato / da verificare

- Touch reale e target >= 44 px misurati sul dispositivo (CSS usa `--ms-touch`, ma solo screenshot a viewport emulati).
- Raspberry Pi reale: `hostname -I`, `aplay -l`, `aconnect -i`, `sudo systemctl restart matsynth.service`, durata reale del riavvio (soglie 20 s / 90 s a taratura), tastiera/FANTOM-08 e uscite reali nelle liste porte/associazioni (nel test le porte erano vuote), stati `ambiguous`/`offline` con strumenti veri.
- Hotspot reale con tutte le richieste esterne bloccate dal telefono; accesso da `.local` (mDNS).
- `managed` dopo il riavvio e rollback: la UI scrive solo la configurazione; l'effetto e' di `ui-routing` (da provare su hardware).
- Caricamento SoundFont reale dalla scheda provvisoria; Safari/iOS.
- Aspetto del titolo senza Orbitron (font di sistema).
- Il flusso `/presets` -> Libreria > Scene dopo la consegna di `library_scenes.js`.

## Decisioni e deviazioni

- `/presets` condizionale (sopra) invece di conversione immediata; `/settings` e' un meta refresh, non un `include` di `index.html` (URL pulito dopo il rimando, pagina minima).
- Scheda SoundFont aggiunta (non elencata nel compito) per non perdere checklist 11-12.
- Font Awesome ridotto al solo `solid`; se un'altra fase usasse `far`/`fab` va aggiunto il relativo css+woff2.
- Il contratto cita la "prima riga di `static/css/style.css`": quel file non aveva font remoti, la riga era in `static/style.css` (radice).
- `index.html`: oltre al `<head>`, ho sostituito anche il `<script>` del bundle Bootstrap a fine body (era un CDN) e il tag di `settings.js` sta in `partials/settings.html` (con `defer`), cosi' il body di `index.html` non cambia.

## Punti aperti per le fasi successive

- `ui-live`: consegnando `static/js/library_scenes.js` attiva da sola il rimando `/presets` -> Libreria > Scene; deve poi togliere la scheda SoundFont provvisoria da `settings.js`/`partials/settings.html` e il segnaposto "SoundFont" che rimanda a `/settings`.
- Backend (coordinatore): `/api/save_hardware` riporta 200 prima del riavvio e non il suo esito; una route di stato con l'ora di avvio del processo (o un contatore) renderebbe la conferma del riavvio esatta invece che dedotta. `/api/system/status.soundfont` e' ancora il segnaposto (sez. 4.6).
- `ui-acceptance`: provare i punti "NON verificato" sul Pi/hotspot.
