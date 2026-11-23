# Handoff `ui-studio`

Commit: `10c4c24` (backend, dettagli in `ui-studio-backend.md`) ed `e799293` (frontend).

## Implementato (frontend)

- `templates/partials/studio.html`, `static/css/studio.css`: vista Studio nella shell, composta da:
  - barra del trasporto;
  - barra della vista (strumento, snap, zoom, segui);
  - timeline a 16 tracce (ruler, header con R/M/S, lane con i clip, playhead);
  - barra contestuale delle operazioni;
  - inspector con le schede Clip, Traccia, Suono e Mixer.
- Asset solo locali, nessun framework. Pulsanti di almeno 44 px.
- Layout responsive:
  - inspector laterale da 260 px sotto i 1024 px in orizzontale;
  - inspector in basso (max 30vh) in verticale o con altezze di 700 px o meno.
- `static/js/studio_model.js` (UMD, logica pura, testata in `tests/js/studio.test.js`):
  - conversioni beat/px/BBT e snap;
  - normalizzazione degli intervalli;
  - selezione (clip, multi-clip per Unisci, intervallo, traccia);
  - stato abilitato/motivazione di ogni comando (`commandStates`);
  - costruzione delle richieste API con `expected_revision` (`buildRequest`);
  - selezione dopo l'operazione (`selectionAfter`, usa `new_clip_id` e `clip`);
  - drag di spostamento e ridimensionamento con snap; palette colori.
- `static/js/studio.js`: UI collegata a `MatSynth.state`/`api` (domini `daw` e `transport`).
  - Rendering per riga con firma, così si ridisegnano solo le tracce cambiate. Density map su canvas, coalescata con `requestAnimationFrame`. La playhead usa `transform`.
  - Selezione: tocco su un clip. La multi-selezione ("Multi" oppure Shift/Ctrl) serve solo per Unisci. Il drag su un'area vuota della lane seleziona un intervallo. Un tocco sulla lane imposta traccia e cursore; un tocco sul ruler fa seek e imposta il cursore.
  - Operazioni contestuali:
    - sposta e ridimensiona, sia con drag del clip e della maniglia sia con i campi dell'inspector;
    - dividi al cursore, unisci, elimina;
    - trasponi di ±1 semitono e di ±1 ottava;
    - duplica;
    - copia, taglia e incolla al cursore sulla traccia selezionata;
    - crea un clip dall'intervallo;
    - cancella l'intervallo (con conferma);
    - loop dalla selezione.
  - Ogni comando disabilitato mostra la motivazione (`aria-disabled` + `title`/`data-reason`). L'`aria-label` mantiene il nome del comando.
  - Traccia:
    - rinomina (anche con Invio) e colore;
    - duplica traccia, con `include_config` facoltativo e conferma di sovrascrittura;
    - svuota la traccia o tutte le tracce, con conferma.
  - Suono: monta il `partPanel` esistente. Mixer: 16 volumi con invio coalescato.
  - Trasporto unico: Torna all'inizio, Play, Rec, Stop, BPM, metro, Click, Loop, Annulla e Ripeti. Il checkbox "Ritorno" invia `return_to_start` (D6) ed è salvato nelle preferenze come `studio.returnToStart`.
  - Tastiera (solo con la vista visibile):
    - Spazio = Play/Stop;
    - Ctrl+C, Ctrl+X, Ctrl+V, Ctrl+D;
    - Canc/Backspace = elimina;
    - Ctrl+Z annulla; Ctrl+Y e Ctrl+Shift+Z ripetono;
    - Esc = deseleziona;
    - Invio/Spazio su un clip focalizzato lo seleziona.
  - Durante Play e Record le modifiche sono disabilitate con la motivazione "Ferma Play/Record per modificare", coerente con il 409 del backend.
- Vista classica: il pulsante "Vista classica" mostra `studio_legacy.html` (piano roll ed editor eventi) e "Torna a Studio" riporta alla vista nuova. Da `legacy_daw.js` e dal menu legacy sono state tolte le operazioni "ultimo clip", sostituite dalle operazioni per ID della vista nuova.
- `static/css/style.css`: il toast della shell ha `pointer-events: none`. Prima copriva la barra contestuale in basso per 4 s e assorbiva il primo click, per esempio su "Crea clip" subito dopo una rinomina.

## Verificato nel browser

Prove fatte con Playwright su un server locale con FakeBackend del router (porte MIDI finte) e dati seed, compresi clip sovrapposti. Nessun errore JS e nessun ID duplicato. Funzionano:

- selezione dei clip; dividi al cursore; multi-selezione + Unisci;
- Elimina con più clip selezionati: rifiutato con la motivazione;
- delete, undo e redo, da pulsante e da tastiera;
- drag di spostamento e di ridimensionamento;
- copia e incolla su un'altra traccia; trasposizione ±12; Ctrl+D;
- intervallo + Crea clip (anche sovrapposto a un clip esistente); Cancella intervallo;
- colore, mute, rinomina con Invio;
- schede Suono (`partPanel`) e Mixer (16 righe);
- Vista classica e ritorno a Studio;
- Play: la playhead avanza e le modifiche si disabilitano. Spazio fa Play/Stop;
- Record su una traccia armata: allo Stop la posizione torna al beat di partenza;
- layout a 1024×640, 768×1024 e 390×844, senza overflow orizzontale della pagina.

Test: `node --test tests/js` → 105/105; `python -m unittest discover -s tests -p "test_*.py"` → 232 OK.

## Deviazioni e decisioni aperte

- **Multi-selezione solo per Unisci**: le altre operazioni agiscono su un clip alla volta (un gesto = un undo, come da contratto). Per estenderle servirebbero API batch.
- **Libreria Progetti/MIDI**: il pulsante "Progetti/MIDI" apre il pannello Libreria esistente. La sezione progetti e l'import MIDI dedicati (`library_projects.js`) non sono stati realizzati in questa fase.
- **Tracce nascoste/compattate**: non implementate, si vedono sempre 16 righe.
- **Vista classica mantenuta** finché `ui-editor` non sostituisce piano roll ed editor eventi.
- Deviazioni del backend: D22 e D23 in `contracts.md` (vedi `ui-studio-backend.md`); `include_config` su duplica traccia.
- **Difetto in `shell.js` (WIP ui-live, non toccato)**: `guardLegacyShortcuts` fa `stopPropagation` in capture su `document` per i tasti dentro `.ms-panel`, `.ms-globalbar` e `#view-live`. Così blocca i listener `keydown` dei target in quei contenitori. Studio non ne dipende (ascolta su `body`), ma va corretto da chi chiude ui-live.

## Difetti della review (`ui-persistence-review.md`)

- Corretti nel backend: M1, M2, L2, L3.
- Restano: H1 e gli altri, non legati alle operazioni di Studio.

## Resta da fare

- `ui-editor`: editor note dettagliato, poi rimozione della vista classica (`studio_legacy.html` e le parti relative di `legacy_daw.js`).
- Sezione Progetti/MIDI della Libreria; tracce nascoste.

## Da verificare sul Pi e sul tablet

- Drag touch reale (soglia `DRAG_PX`, pointer capture) e scroll orizzontale della timeline con lo strumento "Scorri".
- Fluidità di rendering con molte note: costo della density map dopo ogni revisione, CPU del browser sul tablet, carico del Pi durante Play con la vista aperta (aggiornamenti `transport` frequenti).
- Layout su schermi bassi (smartphone in orizzontale): inspector a 30vh e barra contestuale scorrevole.
- Record + "Ritorno" con il FANTOM reale; i rifiuti 409 durante il Record.
