# Handoff `ui-live`

Fase: vista Suona, browser suoni, scene/preferite, controllo parte/master, richiamo senza ricaricare la pagina
(piano riga 427; contratto 4.4-4.7, 8.1/8.2, 11.2, 12). Nessun file Python modificato.

## Implementato (frontend)

- **Vista Suona** (`templates/partials/live.html`, `static/js/live.js`, `static/css/live.css`): un solo layout, impilato
  sotto i 768 px (scena, parti, parte selezionata, master) e a tre colonne da 768 px in su (scene | parti | dettaglio + master).
  Ora è la vista predefinita (`shell.js`, `DEFAULT_VIEW = 'live'`); `prefs.lastView` e l'hash dell'URL hanno la precedenza.
  - Scena: nome, indicatore "modificata", Salva (PUT della scena attuale; senza scena attuale diventa Salva come),
    Salva come (POST; su `NAME_EXISTS` chiede conferma e usa `overwrite_id`, mai suffissi), precedente/successiva,
    ricerca con risultati richiamabili, griglia delle preferite, link a Libreria > Scene.
  - Parti 1-16: elenco con nome del suono, destinazione e avvisi di routing; su telefono si apre a tendina, con frecce precedente/successiva.
  - Master interno (gain/reverb/chorus) tramite `effect_update`, coalescato come gli slider delle parti; i valori si leggono da `/get_state`.
- **Pannello sonoro moderno** (`static/js/part_panel.js`, lo stesso usato dall'inspector di Studio in modalità moderna):
  schede Suono/Mix/Routing, volume sempre visibile, `PATCH /api/parts/<id>` con esito `applied.delivery`, conflitti e `MONITOR_HANDLED_BY_ALSA`.
  - Parte interna: **browser suoni** con ricerca (parole, accenti, "bank-program"), **categorie** (famiglie GM, D25), elenco limitato a 120 voci,
    voce attuale evidenziata, pulsante Riprova se l'elenco non arriva; la scelta invia `sound {bank, program, name}`.
  - Parte esterna: nome utente, MSB/LSB/PC (vuoto = non inviare), solo i CC dichiarati dal dispositivo, nessun nome FluidSynth.
  - Routing: ingresso (alias + canale/tutti), destinazione interna/esterna (alias + canale), monitor, Applica/Ripristina.
  - Archivi condivisi per pagina: `instruments` (elenco strumenti, legato al file SoundFont) e `devices` (registro dispositivi).
- **Scene e SoundFont** (`static/js/library_scenes.js`): servizio `MS.scenes` (refresh, recall, saveAs, saveCurrent, preferita,
  rinomina, elimina, caricamento SF) con dialoghi nella pagina (niente finestre native) e sezioni Libreria > Scene e Libreria > SoundFont,
  che sostituiscono i segnaposto della shell.
  - Richiamo: `POST /api/scenes/<id>/recall`, nessun reload. `TRANSPORT_ACTIVE` (anche controllato prima della richiesta): spiegazione e Stop,
    mai fermato da solo. `UNSAVED_CHANGES`: Salva e richiama / Scarta e richiama / Annulla. `SOUNDFONT_CHANGE_REQUIRED`: due passi (D9),
    carica, attende `loaded`, poi richiama con `discard_changes: true`. `SOUNDFONT_MISSING`, `PORTS_UNAVAILABLE` (parziale solo con consenso,
    `allow_partial`), `PARTIAL_APPLY` (errore, stato riletto), esito sconosciuto (nessuna riprova automatica, stato riletto).
- **Impostazioni** (`static/js/settings.js`): tolta la scheda SoundFont; `openPanel('settings','soundfont')` rimanda a Libreria > SoundFont (D24).

## Bug corretti

1. **Strumenti del SoundFont non visibili nella vista Suona (segnalato sul Pi).** Causa: nella versione installata (HEAD prima di questa fase)
   la vista Suona montava il pannello moderno, che aveva solo i cursori CC e nessun elenco strumenti; `/get_instruments` veniva letto
   solo dal vecchio mixer di Studio (`legacy_daw.setupMixer`, menu `#instrument-select`). Il backend era corretto. Ora il pannello ha il browser suoni.
   Rafforzato anche l'archivio strumenti: un elenco vuoto (synth occupato o SF in caricamento) non conta più come "letto" e si può riprovare;
   l'elenco è legato al file SF solo se nessun caricamento è iniziato durante la richiesta; al passaggio a `loaded` con un file diverso
   si rilegge, anche se c'era una lettura in corso (prima la lettura in corso poteva restare valida per il SF vecchio).
2. **`guardLegacyShortcuts` bloccava i tasti** in pannelli, barra globale e vista Suona: era in fase di cattura su `document`
   con `stopPropagation`, quindi il tasto non raggiungeva mai il target (Esc del browser suoni, Invio nei dialoghi, frecce delle schede).
   Ora è in fase di bubble su `document`, registrata prima di `legacy_daw.js`, e usa `stopImmediatePropagation`: ferma solo i listener legacy
   successivi. Studio non cambia (il suo listener è su `body` e ferma già la propagazione); con Studio visibile, senza pannello aperto e con
   il focus fuori dalla shell, le scorciatoie legacy funzionano come prima.
3. I campi del suono (nota della parte interna, MSB/LSB/PC/nome della parte esterna) ora seguono lo stato dopo un richiamo o un cambio
   da un altro client (prima restava il testo vecchio fino al cambio di struttura).

## Test

- JS: `node --test tests/js` → 108/108 (`tests/js/live.test.js`: logica pura del pannello, categorie, master, scene, flussi di richiamo con API finta,
  controlli statici: niente reload/navigazione/finestre native/`fetch` diretto/`setInterval`, guardia legacy in bubble prima di `legacy_daw.js`).
- Python: `python -m unittest discover -s tests -p "test_*.py"` → 232 OK (backend non toccato).

## Verificato nel browser

Chromium headless (Playwright) contro un server locale con console FluidSynth finta (ritardo 2,2 s sul comando `inst`, come sul Pi)
e porte MIDI finte (`tests/helpers.py`):

- Vista Suona di default, nessun errore JS; browser suoni con 80 strumenti, categorie, ricerca + categoria; scelta → `select 0 1 0 24`,
  titolo della parte aggiornato, browser chiuso; Esc chiude il browser; Invio nel campo volume → `cc 0 7 90`; Spazio su un pulsante della
  vista Suona = click, non avvia il Play legacy; master → `set synth.gain 0.8`.
- Routing: parte 3 su esterno `fantom` via `PATCH /api/parts/2`; per l'esterna niente nomi FluidSynth, campi MSB/LSB/PC, solo CC 7/10/74/71.
- Salva come (Invio nel dialogo), scena modificata dopo cambio suono, richiamo dalla ricerca e dalle preferite senza navigazione
  (programma e CC inviati), `UNSAVED_CHANGES` con Salva/Scarta/Annulla, Play attivo → richiamo bloccato e Stop esplicito.
- Libreria > SoundFont: caricamento di un altro SF con conferma, stato `loaded` ed elenco strumenti riletto per il nuovo file; Esc chiude il pannello.
- Layout 360x800, 390x844, 768x1024, 1024x640: nessun overflow orizzontale, tutti i controlli visibili >= 44 px.
- Studio si apre e Spazio invia `transport_cmd` come prima; `/presets` e `/settings` si caricano senza errori.

## Deviazioni e decisioni aperte

- D24 (`contracts.md` 13): toccati `shell.js` (vista predefinita, guardia scorciatoie) e `settings.js` (scheda SoundFont spostata in Libreria).
- D25: categorie = famiglie General MIDI, indicative sui SoundFont non GM.
- La vista predefinita è ora Suona: chi aveva già aperto Studio continua a trovare Studio (`prefs.lastView`).

## Resta da fare

- Ordinamento manuale delle preferite (il campo `order` esiste nel backend, non c'è ancora la UI).
- Il browser suoni mostra al massimo 120 voci alla volta (si restringe con ricerca o categoria): con SF2 molto grandi valutare la paginazione.

## Da verificare sul Pi e sul telefono/tablet

- Il browser suoni mostra gli strumenti del SF attivo dopo l'avvio e dopo un cambio SF (ogni `/get_instruments` sul Pi impiega ~2,2 s:
  all'avvio ci sono due letture, una del vecchio mixer di Studio e una dell'archivio condiviso).
- Tempo tra la scelta di uno strumento e il cambio di suono mentre è in corso una lettura `inst` (la console FluidSynth risponde in serie).
- Richiamo di una scena con SoundFont diverso (tempi reali di caricamento) e con un dispositivo esterno scollegato (`PORTS_UNAVAILABLE`).
- Touch reale: scorrimento dell'elenco strumenti e della riga categorie, slider senza salti, nessun gesto nascosto.
