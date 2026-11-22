# Handoff `ui-shell`

## Implementato

Solo frontend (nessun file Python, `startfluid.sh`, `settings.html`, `presets.html` toccati), come da `docs/contracts.md` 11.2.

- `templates/index.html`: shell con barra globale (connessione, trasporto, "non salvato", PANIC), navigazione **Suona | Studio** + Libreria/Impostazioni secondari, tre viste (`#view-live`, `#view-studio`, `#view-editor`) e due pannelli (`#ms-panel-library`, `#ms-panel-settings`). Ordine script: `units -> state -> api -> part_panel -> library -> shell -> legacy_daw`.
- `templates/partials/`: `studio_legacy.html` (markup DAW precedente, invariato), `legacy_modals.html` (modali fuori dalle viste), placeholder `live.html`, `studio.html`, `editor.html`, `settings.html`.
- `static/js/`: `api.js` (unico `io()`, `request`/`emit` che non rigettano, buste nuova/legacy/array/testo, `CLIENT_TIMEOUT`/`NETWORK_ERROR` con `unknown_outcome`), `state.js` (domini del contratto, `prefs` in `localStorage` `matsynth.prefs.v1`), `shell.js` (viste/pannelli senza rete, snapshot su connessione con fallback `/get_state` se `/api/parts` manca), `units.js` (`SNAP_OPTIONS` sez. 1.3, conversioni), `part_panel.js` (pannello sonoro unico Mix/Suono), `library.js` (`registerSection`), `legacy_daw.js` (JS inline spostato, usa `MatSynth.api.socket()`).
- `static/css/style.css` (token/base/shell) + `live/studio/editor/settings.css` vuoti (D11). `static/style.css` radice resta lo stile del DAW attuale.
- Test JS: `tests/js/*.test.js` (47 test).
- Corregge A9 (ID `midi-file-input` duplicato -> `midi-file-input-modal`) e A12 (etichette snap da `units.SNAP_OPTIONS`).

## Verificato e come

    node --test tests/js                                   -> 47/47 ok
    node --check home/matteo/matsynth_web/static/js/*.js   -> ok
    .venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"  -> 62 ok (4 expected failures)

Browser headless (Chromium via script `verifica-web`, Flask locale avviato con `tests/helpers.import_app` + `redirect_app_paths`, porta 5055, viewport 1024x768): nessun errore JS, socket connesso, DAW renderizzato; 21 cicli `showView` live/studio/editor + `openPanel('library')`/`closePanel()` = **0 fetch, 0 emit**, un solo socket. Unico 404: `GET /api/parts` (atteso, route di `ui-routing`).

## NON verificato

- Viewport 360x800, 390x844, 768x1024, 1366x768 e touch reale.
- Clic manuale di tutte le righe 1-61 di `docs/regression-checklist.md` (la struttura e' invariata, ma non e' stato rieseguito a mano).
- Funzionamento offline: i CDN (Bootstrap, Font Awesome, Socket.IO) sono ancora nel `<head>`; li toglie `ui-offline-settings`.

## Per le fasi successive

- `ui-routing`: la shell legge gia' `/api/parts`, `/api/midi/ports` ed eventi `parts_update`, `ports_status`; appena esistono, il fallback legacy smette di essere usato.
- `ui-live`/`ui-studio`/`ui-editor` riempiono i placeholder e rimuovono da `legacy_daw.js` solo le parti che sostituiscono.
