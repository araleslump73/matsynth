# Checklist di regressione MatSynth (fase `ui-baseline`)

Derivata dalla tabella di sezione 6 di `plan.md`. Riferimenti "oggi" ricavati dal codice del ramo `develop` (HEAD `8615e03`); i numeri di riga sono indicativi e invecchiano: cercare per nome.

Percorsi abbreviati: `app.py`, `daw_recorder.py` = `home/matteo/matsynth_web/...`; `index.html` = `templates/index.html` (JS inline); `settings.html`, `presets.html` = pagine separate in `templates/`.

Colonna **Verifica**: vuota; ogni fase successiva la compila con data, esito e come (test automatico / browser / Pi). Non scrivere "ok" per cio' che e' solo simulato; l'hardware reale va marcato "da verificare su Pi".

## 1. Funzioni (tabella sezione 6)

| # | Funzione | Dove vive OGGI | Collocazione canonica futura | Verifica |
| --- | --- | --- | --- | --- |
| 1 | Scelta strumenti sui 16 canali | `index.html`: `<select id="select-ch-N" class="daw-inst-select" onchange="changeInst(N,..)">`, `changeInst`, `setupMixer`/`populateDropdown` (GET `/get_instruments`); `app.py` `select_prog` (`/select_prog/<chan>/<bank>/<prog>`, salva `channels.<n>.bank/program` in `last_state.json`); `loadSavedChannelState` (GET `/get_state`) | Suona > parte; Studio > inspector della traccia, stesso controllo | |
| 2 | Volume/pan e parametri sonori (CC7, 10, 71-75) | `index.html`: pannello `#panel-mixer` (`#vol-slider`, `#pan-slider`, `#cc-slider-73/75/72/74/71`, tab `#tab-mix`/`#tab-sound`), slider inline `#track-vol-N`/`#track-pan-N` (`setupInlineSliders`), `updateCC` (socket `cc_update`, fallback GET `/cc/<chan>/<cc>/<val>`); `app.py` `handle_cc_update`, `control` (persistenza debounced `save_cc_debounced`, nomi 7/10/71/72/73/74/75) | Dettaglio parte/inspector, schede Mix e Suono | |
| 3 | Gain, reverb, chorus | `index.html`: `#panel-fx` (`updateGain`, `#gain-value`, `setEff('reverb.level'/'chorus.level')`; socket `effect_update`, fallback GET `/set_effect/<type>/<val>`); `app.py` `handle_effect_update` (whitelist `gain`, `reverb.level`, `chorus.level`), `set_effect`. Gli slider reverb/chorus non hanno `id` (selezione per posizione nel DOM) | Master interno condiviso | |
| 4 | Salva preset | `presets.html` `saveCurrentPreset` (GET `/api/capture_current_config` poi POST `/api/presets/save`); `app.py` `api_capture_current_config`, `api_presets_save` (nome file sanificato, suffisso `_2`... se esiste) | Libreria > Scene, apertura contestuale da Suona | |
| 5 | Richiama preset | `presets.html` `loadPreset` (GET `/api/presets/load/<f>` poi POST `/api/presets/apply`, poi `window.location.href='/'` = ricarica pagina); `app.py` `api_presets_load`, `api_presets_apply` (non carica il SoundFont; non legge `pan`; non persiste i canali in `last_state.json`) | Libreria > Scene, apertura contestuale da Suona | |
| 6 | Cerca preset | `presets.html` `filterPresets` (`#preset-search-input`, solo lato client) | Libreria > Scene | |
| 7 | Rinomina preset | `presets.html` `openRenameModal`/`confirmRename` (`#rename-modal`); `app.py` `api_presets_rename` | Libreria > Scene | |
| 8 | Elimina preset | `presets.html` `deletePreset`; `app.py` `api_presets_delete` | Libreria > Scene | |
| 9 | Elenco preset | `presets.html` `loadPresetsList` (`#presets-container`); `app.py` `api_presets_list` | Libreria > Scene | |
| 10 | Preferiti scene / richiamo rapido | NON ESISTE oggi (nessun campo/route); funzione nuova | Suona; metadati libreria senza motore musicale aggiuntivo | |
| 11 | SoundFont: elenco | `settings.html` `fetchSF2List` (`#sf2-select`; GET `/list_sf2`, GET `/get_state` per il corrente); `app.py` `list_sf2` | Libreria > SoundFont | |
| 12 | SoundFont: caricamento | `settings.html` `changeSF2` (GET `/load_sf2/<file>`); `app.py` `load_sf2` (unload di tutti gli ID, load, aggiorna `sf_id`, salva `font`); all'avvio `restore_settings` ricarica il font salvato | Libreria > SoundFont; dipendenze nel richiamo scena | |
| 13 | Progetto: Nuovo | `index.html` `dawNewProject` (menu File `New Project`, `#daw-session-title`) | Studio e Libreria > Progetti | |
| 14 | Progetto: Salva / Salva come / Apri | Oggi NON esiste un progetto nativo: Save/Save As sono export SMF (`dawQuickSaveMidi`, `dawSaveAsMidi`, `dawDoSave`; Ctrl+S) e Open = `dawOpenMidiManager` (`#midi-manager-modal`) | Studio e Libreria > Progetti | |
| 15 | MIDI: salva | `index.html` `dawSaveMidi`/`dawDoSave` (`#midi-save-name`) -> POST `/api/daw/midi/save`; `app.py` `daw_midi_save` + `daw_recorder.py` `export_midi` (Bank MSB/LSB + PC solo a tick 0, da `last_state.json`) | Libreria > MIDI | |
| 16 | MIDI: lista | `dawRefreshMidiList` (`#midi-file-list`) -> GET `/api/daw/midi/list`; `app.py` `daw_midi_list` | Libreria > MIDI | |
| 17 | MIDI: importa (sostituisci) / aggiungi (merge) | `dawLoadMidiFile` -> GET `/api/daw/midi/load/<f>?merge=`; `dawUploadMidi` (`#midi-file-input`, ID duplicato nella pagina) -> POST `/api/daw/midi/upload` (multipart `file`, `merge`; salva anche copia in `midi/`); `app.py` `daw_midi_load`, `daw_midi_upload` + `daw_recorder.py` `import_midi` | Libreria > MIDI; separato dal salvataggio progetto | |
| 18 | MIDI: scarica | `dawDownloadMidiFile` -> GET `/api/daw/midi/download/<f>`; `app.py` `daw_midi_download` | Libreria > MIDI | |
| 19 | MIDI: elimina | `dawDeleteMidiFile` -> DELETE `/api/daw/midi/delete/<f>`; `app.py` `daw_midi_delete` | Libreria > MIDI | |
| 20 | Record | `#daw-rec` `dawRecord` (socket `transport_cmd` `record_start`/`record_stop`, fallback POST `/api/daw/record/start|stop`); `daw_recorder.py` `start_recording`, `_record_loop`, `stop_recording`, `_finalize_recording_clips` | Trasporto unico | |
| 21 | Play / Pausa | `#daw-play` `dawPlay` (toggle `play_start`/`play_stop`; "pausa" = stop senza rewind); `daw_recorder.py` `start_playback`, `_playback_loop`, `stop_playback` | Trasporto unico | |
| 22 | Stop | `#daw-stop` `dawStop` (`stop_all`); `daw_recorder.py` `stop_all` | Trasporto unico | |
| 23 | Rewind | `#daw-rewind` `dawRewind` (`rewind`); `daw_recorder.py` `rewind` | Trasporto unico | |
| 24 | Seek | click su ruler/timeline (`onCanvasPointerUp` -> `dawSeekTo`), campo `#daw-timeline` (`dawApplyTimeInput`); socket `seek_beat`/`seek`, fallback POST `/api/daw/seek_beat`; `daw_recorder.py` `seek_to_beat`, `set_position` | Trasporto unico | |
| 25 | BPM | `#daw-bpm` `dawSetBPM` -> POST `/api/daw/bpm`; `daw_recorder.py` `set_bpm` (rifiutato durante record/play) | Trasporto Studio; dettaglio anche da Suona | |
| 26 | Tempo 3/4-4/4 | `#daw-ts-select` `dawSetTimeSignature` -> POST `/api/daw/time_signature`; `set_time_signature` | Trasporto Studio; dettaglio anche da Suona | |
| 27 | Click (metronomo) | `#daw-metronome` `dawToggleMetronome` -> POST `/api/daw/metronome/toggle`; `daw_recorder.py` `_metronome_loop` (nota 76/77 sul canale 10 inviata a `midi_output`) | Trasporto Studio; dettaglio anche da Suona | |
| 28 | Loop attivo | `#daw-loop` `dawToggleLoop` (socket `toggle_loop`, fallback POST `/api/daw/loop/toggle`); `toggle_loop` | Trasporto e selezione timeline | |
| 29 | Punti A/B e loop da selezione | Menu Edit `Set Loop from Selection` `dawSetLoopFromSelection` -> POST `/api/daw/loop_points`; `set_loop_points`. Non c'e' un controllo diretto per A/B | Trasporto e selezione timeline | |
| 30 | Arm | `#arm-btn-N` `dawToggleArm` -> POST `/api/daw/track/<ch>/arm`; `arm_track` | Traccia Studio; riepilogo nel dettaglio parte | |
| 31 | Mute | `#mute-btn-N` `dawToggleMute` -> POST `/api/daw/track/<ch>/mute`; `mute_track` | Traccia Studio | |
| 32 | Solo | `#solo-btn-N` `dawToggleSolo` -> POST `/api/daw/track/<ch>/solo`; `solo_track` | Traccia Studio | |
| 33 | Attivita' traccia | `record_activity` (socket) -> `renderTrackTape`; badge `#events-N`; GET `/api/daw/track/<ch>/activity`; `get_track_activity` | Traccia Studio; riepilogo nel dettaglio parte | |
| 34 | Nome traccia | Menu Edit `Rename Track` `dawRenameTrackPrompt` -> POST `/api/daw/track/<ch>/rename`; `rename_track` | Menu della traccia | |
| 35 | Colore traccia | `dawCycleTrackColor` -> POST `/api/daw/track/<ch>/color`; `set_track_color` (palette da 16) | Menu della traccia | |
| 36 | Duplica traccia | Menu Edit `Duplicate Track` `dawDuplicateTrack` (modal `#dup-dest-select`) -> POST `/api/daw/track/<ch>/duplicate`; `duplicate_track` | Menu della traccia | |
| 37 | Svuota traccia (Purge) | Menu Edit `Purge Track` `dawClearTrack(activeControlChannel)` -> POST `/api/daw/track/<ch>/clear`; `clear_track` | Menu della traccia (con conferma) | |
| 38 | Nascondi/ripristina tracce | `dawHideTrack`, `dawShowTrack`, `dawShowAllTracks`, `#daw-hidden-bar` (View > Show All Tracks; pressione lunga su `.ch-cell`, `setupChCellHandlers`). Solo client, nessuna persistenza | Preferenze Studio e menu traccia | |
| 39 | Altezza traccia / zoom | `dawCycleTrackSize` (doppio click su `.ch-cell`; compact/medium/large), `#daw-zoom-range` `dawViewSetZoom`, `dawViewSetStart` | Preferenze Studio e menu traccia | |
| 40 | Selezione intervallo | `#daw-select-mode` `dawToggleSelectMode`, `#daw-sel-bar`, `updateSelectionUI`, `dawCancelSelection` (solo client) | Barra contestuale timeline/editor | |
| 41 | Copia / incolla / taglia | `dawCopySelection` -> POST `/api/daw/copy`; `dawPasteAtPlayhead` -> POST `/api/daw/paste`; `copy_selection`, `paste_at`. "Taglia" = copia + `dawDeleteSelection` (nessun comando unico); Ctrl+C/V | Barra contestuale timeline/editor | |
| 42 | Clip: move | Menu Edit `Move Last Clip Left/Right` `dawMoveLastClip`; trascinamento canvas (`commitClipDrag`, `hitTestClip`) -> POST `/api/daw/clip/move`; `move_clip` | Barra contestuale e inspector clip | |
| 43 | Clip: delete | Menu Edit `Delete Last Clip` `dawDeleteLastClip` -> POST `/api/daw/clip/delete`; `delete_clip` | Barra contestuale e inspector clip | |
| 44 | Clip: transpose | Menu Edit `Transpose Last Clip +-1` `dawTransposeLastClip` -> POST `/api/daw/clip/transpose`; `transpose_clip` | Barra contestuale e inspector clip | |
| 45 | Clip: resize | bordo clip su canvas (`commitClipResize`, `hitTestClipEdge`) -> POST `/api/daw/clip/resize`; `resize_clip` | Barra contestuale e inspector clip | |
| 46 | Clip: split | Menu Edit `Cut Clip at Playhead` `dawCutClipAtPlayhead` -> POST `/api/daw/clip/split`; `split_clip` (usa `dawState.current_beat`, mai valorizzato: vedi handoff) | Barra contestuale e inspector clip | |
| 47 | Clip: merge | Menu Edit `Merge All Clips` `dawMergeAllClips` -> POST `/api/daw/clip/merge`; `merge_clips` | Barra contestuale e inspector clip | |
| 48 | Quantizza (griglia, strength, swing, ambito) | `#daw-quant-bar` (`#quant-grid`, `#quant-strength`, `#quant-swing`, `#quant-channel`, `#quant-apply-btn`), `dawToggleQuantBar`, `dawQuantize` -> POST `/api/daw/quantize`; `quantize_tracks`. Ambito = canale singolo o ALL, non la selezione | Finestra unica condivisa, aperta dalla selezione | |
| 49 | Trasposizione semitoni/ottave | Barra `Transpose` (-12, -1, +1, +12) `dawTransposeActive` -> POST `/api/daw/transpose`; `transpose_notes` (intero canale o intervallo) | Barra contestuale selezione | |
| 50 | Piano roll | `#panel-pianoroll` (`openPianoRollForChannel`, `togglePianoRoll`, `renderPianoRoll`, `setPRTool`, `#pr-snap-grid`, `#pr-vel-input`); GET `/api/daw/full_note_map`; `get_full_note_map` | Studio > editor MIDI | |
| 51 | Aggiungi / sposta / ridimensiona / elimina note | `prAddNote` -> POST `/api/daw/track/<ch>/add_note`; `prCommitDragEdit` -> PUT `.../edit_note_pair`; `prDeleteSelectedNotes` -> DELETE `.../delete_note_pair`; `add_note`, `edit_note_pair`, `delete_note_pair` | Studio > editor MIDI | |
| 52 | Velocity e modifica batch note | `prCommitVelocityEdit` (corsia `#pr-velocity`) -> PUT `.../edit_note_pair`; `daw_recorder.py` `edit_notes_batch`, `delete_notes_batch` (nessuna route app.py li usa) | Editor MIDI | |
| 53 | Lista/modifica/eliminazione eventi | `#midi-event-viewer-modal` (`dawOpenEventViewer`, `dawLoadEvents`, `dawSaveEventEdit`, `dawDeleteEvent`) -> GET `/api/daw/track/<ch>/events`, PUT/DELETE `.../events/<index>`; `get_events_decoded`, `edit_event`, `delete_event` | Editor MIDI > Eventi | |
| 54 | Attivita' live (event viewer) | socket `midi_event_new` (durante record), `midi_playback_beat` (~10 Hz durante play) | Editor MIDI > Eventi | |
| 55 | Preview note | `previewNote` -> POST `/api/daw/preview_note` (noteon/noteoff diretti a FluidSynth via console 9800, sempre canale della traccia) | Editor MIDI, inviata alla destinazione della traccia | |
| 56 | Undo / redo | `#daw-undo`, `#daw-redo`, menu Edit, Ctrl+Z/Y; `dawUndo`/`dawRedo` (socket `undo`/`redo`, fallback POST `/api/daw/undo|redo`); `_take_snapshot`, `_push_undo`, max 10 livelli | Barra Studio/editor, una sola cronologia | |
| 57 | Clear range | `dawDeleteSelection` -> POST `/api/daw/track/<ch>/clear_range`; `clear_range` | Menu contestuale o Progetto, con conferma | |
| 58 | Purge / clear all | Purge: riga 37. Clear All: menu Edit `Clear All Tracks` `dawClearAll` -> POST `/api/daw/clear_all`; `clear_all_tracks` | Menu contestuale o Progetto, con conferma | |
| 59 | Reset canale | Route `reset_channel` (`/reset_channel/<chan>`) esiste; la chiamata in `changeInst` e' commentata, nessun controllo UI | Dettaglio parte / diagnostica; reset con conferma | |
| 60 | Refresh stato synth | Pulsante sync nel menu (`.daw-menu-sync`, `syncWithSynth` -> GET `/refresh_status`, parsing di `channels`); `loadInitialState` | Dettaglio parte / diagnostica | |
| 61 | PANIC | `#daw-panic` `dawPanic` (socket `panic`, fallback POST `/api/daw/panic`); `daw_recorder.py` `panic` = `_send_all_notes_off` (CC123 x16 solo su `midi_output`; NON invia All Sound Off nonostante il docstring) | Sempre visibile; tutte le destinazioni utilizzate | |
| 62 | Input MIDI | Selezione automatica per parole chiave in `daw_recorder.py` `_init_midi_ports` (FANTOM > SINCO > USB, esclude Midi Through); `settings.html` `#midi-select` (GET `/api/midi_devices` via `aconnect -i`, POST `/api/save_hardware` salva solo `midi_device` in `last_state.json`) | Routing parte/traccia; associazione dispositivi in Impostazioni | |
| 63 | Nuova uscita MIDI | NON esiste oggi: una sola uscita, cercata come `FLUID` o `128:0` in `_init_midi_ports` | Routing parte/traccia; associazione dispositivi in Impostazioni | |
| 64 | ALSA audio output | `settings.html` `#audio-select` (GET `/api/audio_devices` via `aplay -l`; POST `/api/save_hardware`, normalizza `hw:` in `plughw:`) | Impostazioni > Hardware | |
| 65 | Riavvio hardware | `settings.html` `#btn-save` `saveAndRestart` -> POST `/api/save_hardware`; `app.py` `delayed_restart` = `sudo systemctl restart matsynth.service` dopo 1 s | Impostazioni > Hardware, fuori dal flusso live | |
| 66 | IP/hostname e stato rete | `settings.html` `#network-info` (GET `/api/network`: `hostname -I`, `hostname`). Nessun indicatore globale in `index.html` | Impostazioni > Connessione e indicatore globale | |

Funzioni presenti oggi ma non nella tabella (da collocare comunque): barra stato `#daw-status`, titolo sessione `#daw-session-title` (`dawSetSessionName`), `snap` globale `#daw-snap-grid`, ridimensionamento pannello `#daw-resize-handle`, scorciatoie tastiera (due listener `keydown`, `index.html` ~4122 e ~4967), mappe `/api/daw/full_density_map` e `/api/daw/track/<ch>/clips`.

## 2. Route HTTP di `app.py`

`ok` = `{"status":"ok", ...}`; errori `{"status":"error","message":...}` con 400/404/409/500 salvo diversa indicazione. Le route legacy di synth rispondono testo semplice `OK`.

### Pagine e synth (FluidSynth console 9800)

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| GET | `/` | Pagina principale | render `index.html` con `files` (elenco SF2) |
| GET | `/settings` | Pagina impostazioni | render `settings.html` |
| GET | `/presets` | Pagina preset | render `presets.html` |
| GET | `/list_sf2` | Elenco SoundFont in `/usr/share/sounds/sf2/` | out: lista nomi file `.sf2/.sf3` |
| GET | `/load_sf2/<filename>` | Scarica tutti i font, carica quello scelto, salva `font` | out: testo `Caricato: <risposta fluid>` |
| GET | `/get_instruments` | Strumenti del font attivo (`inst <sf_id>`) | out: lista stringhe `"bank-prog Nome"` |
| GET | `/select_prog/<chan>/<bank>/<prog>` | Seleziona strumento e salva in `channels.<chan>` | out: `OK` |
| GET | `/set_effect/<type>/<val>` | `set synth.<type> <float>` e salva (qualsiasi `type`: nessuna whitelist su questa route) | out: `OK` / 400 |
| GET | `/cc/<chan>/<cc>/<val>` | Invia CC; salva debounced per 7,10,71-75 | out: `OK` |
| GET | `/reset_channel/<chan>` | `reset <chan>` | out: `OK` |
| GET | `/refresh_status` | Output di `channels` | out: `{"raw": str}` |
| GET | `/get_state` | Contenuto di `last_state.json` | out: dict (`gain`, `reverb.level`, `chorus.level`, `font`, `channels`, `audio_device`, `midi_device`) |
| GET | `/api/capture_current_config` | Snapshot per salvare preset | out: `{status, config:{font,gain,reverb_level,chorus_level,channels:[{channel,bank,program,attack,release,decay,cutoff,resonance,volume,instrument_name}]}}` (nessun `pan`) |

### Preset legacy

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| GET | `/api/presets/list` | Elenco ordinato per nome | out: `[{filename,name,created,font}]` |
| POST | `/api/presets/save` | Salva preset (suffisso `_N` se esiste) | in: `{name,font,gain,reverb_level,chorus_level,channels[]}`; out: `{status,message,saved_name,filename}` |
| GET | `/api/presets/load/<filename>` | Legge il JSON | out: `{status,data}` |
| DELETE | `/api/presets/delete/<filename>` | Elimina | out: `{status,message}` |
| POST | `/api/presets/rename` | Rinomina (sovrascrive senza controllo se il nuovo nome esiste) | in: `{old_filename,new_name}`; out: `{status,message,new_filename}` |
| POST | `/api/presets/apply` | Applica globali e canali a FluidSynth | in: `{preset:{...}}`; out: `{status,message}` |

### Hardware / rete

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| GET | `/api/network` | IP e hostname | out: `{ips:[...],hostname}` |
| GET | `/api/audio_devices` | `aplay -l` | out: `{devices:[{id,name}],current}` |
| GET | `/api/midi_devices` | `aconnect -i` | out: `{devices:[{id,name}],current}` |
| POST | `/api/save_hardware` | Salva device e riavvia il servizio | in: `{audio?,midi?}`; out: `{status}` (riavvio dopo 1 s) |

### DAW: trasporto e stato

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| GET | `/api/daw/state` | Stato completo | out: `{status,data:{is_recording,is_playing,timeline_position,bpm,metronome_enabled,beats_per_measure,armed,muted,solo,any_solo,has_data,track_counts,track_durations,max_duration,loop_enabled,loop_start,loop_end,can_undo,can_redo,track_names,track_colors,clips}}` |
| POST | `/api/daw/record/start` | Avvia record sui canali armati | in: `{channel?}`; 400 se nessun armato |
| POST | `/api/daw/record/stop` | Ferma record | 400 se non in record |
| POST | `/api/daw/play/start` | Avvia play | 400 se niente da suonare o gia' in play |
| POST | `/api/daw/play/stop` | Ferma play | 400 se non in play |
| POST | `/api/daw/stop_all` | Ferma tutto | |
| POST | `/api/daw/panic` | All Notes Off | |
| POST | `/api/daw/rewind` | Stop e posizione 0 | |
| POST | `/api/daw/seek_beat` | Seek in beat | in: `{beat:number}`; 409 durante record |
| POST | `/api/daw/bpm` | Imposta BPM | in: `{bpm}`; 400 se in record/play |
| POST | `/api/daw/time_signature` | 3 o 4 | in: `{beats_per_measure}`; out: `{status,beats_per_measure}` |
| POST | `/api/daw/metronome/toggle` | Click on/off | out: `{status,message,enabled}` |
| POST | `/api/daw/loop_points` | Punti loop in beat | in: `{start_beat,end_beat}`; out: `{status,loop_start,loop_end}` |
| POST | `/api/daw/loop/toggle` | Loop on/off | out: `{status,message,enabled}` |
| POST | `/api/daw/undo`, `/api/daw/redo` | Cronologia | 400 se vuota |

### DAW: tracce

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| POST | `/api/daw/track/<ch>/arm` | Arm | in: `{armed}`; out: `{status,message,armed}` |
| POST | `/api/daw/track/<ch>/mute` | Mute | in: `{muted}`; out: `{...,muted}` |
| POST | `/api/daw/track/<ch>/solo` | Solo | in: `{solo}`; out: `{...,solo}` |
| POST | `/api/daw/track/<ch>/clear` | Svuota traccia | |
| POST | `/api/daw/clear_all` | Svuota tutto | |
| POST | `/api/daw/track/<ch>/clear_range` | Cancella intervallo | in: `{start_beat,end_beat}` |
| POST | `/api/daw/track/<ch>/duplicate` | Duplica su altro canale | in: `{dest_channel}`; out: `{status,dest_channel}` |
| POST | `/api/daw/track/<ch>/rename` | Nome | in: `{name}` |
| POST | `/api/daw/track/<ch>/color` | Colore | in: `{color_index}` |
| GET | `/api/daw/track/<ch>/clips` | Clip della traccia | out: `{status,clips:[{id,start_beat,end_beat,name,created_at}]}` |
| GET | `/api/daw/track/<ch>/activity` | Intervalli attivita' | query: `start,end`; out: `{status,intervals}` |
| GET | `/api/daw/full_density_map` | Mappa densita' | out: `{status,map}` |
| GET | `/api/daw/full_note_map` | Mappa note (piano roll) | out: `{status,map}` |

### DAW: editing

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| POST | `/api/daw/quantize` | Quantizza | in: `{channels[],grid,strength 0-100,swing 0-100}`; griglie valide 1, .5, .25, .125, .0625; 409 durante record/play; out: `{status,channels_processed,events_modified,message}` |
| POST | `/api/daw/transpose` | Trasponi canale/intervallo | in: `{channel,semitones -24..24,start_beat?,end_beat?}`; out: `{status,channel,semitones,modified}` |
| POST | `/api/daw/preview_note` | Anteprima su FluidSynth | in: `{channel,note,velocity,length_ms}` |
| POST | `/api/daw/copy` | Copia in clipboard | in: `{channel,start_beat,end_beat}`; out: `{status,copied}` |
| POST | `/api/daw/paste` | Incolla | in: `{channel,beat}`; out: `{status,pasted}` |
| POST | `/api/daw/clip/move` | Sposta clip | in: `{channel,clip_id,target_start_beat}`; 404 clip assente, 409 durante transport |
| POST | `/api/daw/clip/delete` | Elimina clip e suoi eventi | in: `{channel,clip_id}` |
| POST | `/api/daw/clip/transpose` | Trasponi clip | in: `{channel,clip_id,semitones}`; out: `{status,modified}` |
| POST | `/api/daw/clip/resize` | Cambia fine clip | in: `{channel,clip_id,new_end_beat}` |
| POST | `/api/daw/clip/split` | Divide clip | in: `{channel,clip_id,split_beat}`; out: `{status,new_clip_id}` |
| POST | `/api/daw/clip/merge` | Unisce clip | in: `{channel,clip_ids[>=2]}`; out: `{status,new_clip_id}` |
| GET | `/api/daw/track/<ch>/events` | Eventi decodificati (`-1` = tutti) | out: `{status,events}` |
| PUT | `/api/daw/track/<ch>/events/<index>` | Modifica evento | in: subset di `beat,velocity,note,cc_value,cc_num,pitch,program`; indice = posizione nella lista, non stabile |
| DELETE | `/api/daw/track/<ch>/events/<index>` | Elimina evento | |
| POST | `/api/daw/track/<ch>/add_note` | Aggiunge nota | in: `{note,velocity,start_beat,length_beats}` |
| PUT | `/api/daw/track/<ch>/edit_note_pair` | Modifica coppia on/off | in: `{on_idx,off_idx,on_beat?,off_beat?,note?,velocity?}`; indici posizionali, nessuna revisione |
| DELETE | `/api/daw/track/<ch>/delete_note_pair` | Elimina coppia | in: `{on_idx,off_idx}` |

### MIDI file

| Metodo | Path | Scopo | In / out |
| --- | --- | --- | --- |
| POST | `/api/daw/midi/save` | Export SMF in `midi/` (nome sanificato, suffisso `_N`) | in: `{name?}`; out: `{status,filename,message}` |
| GET | `/api/daw/midi/list` | Elenco file | out: `[{filename,name,size,modified}]` |
| GET | `/api/daw/midi/load/<filename>` | Importa file salvato | query: `merge=true|false`; out: `{status,data:{bpm,beats_per_measure,channels,channel_programs,total_events}}`; applica `select` a FluidSynth e salva bank/program in `last_state.json`; 409 durante transport |
| DELETE | `/api/daw/midi/delete/<filename>` | Elimina | |
| GET | `/api/daw/midi/download/<filename>` | Scarica | `audio/midi` attachment |
| POST | `/api/daw/midi/upload` | Importa da PC e salva copia | multipart `file` (max 5 MiB letti), form `merge`; stessa risposta di `load` |

## 3. Eventi Socket.IO

Namespace `/`; `async_mode='threading'`, `cors_allowed_origins="*"`. Il client usa `io()` da CDN (`cdn.socket.io 4.5.4`).

| Direzione | Evento | Payload | Note |
| --- | --- | --- | --- |
| client -> server | `cc_update` | `{chan,cc,val}` (0-15, 0-127, 0-127) | ack `{status}`; persistenza debounced |
| client -> server | `effect_update` | `{type in gain/reverb.level/chorus.level, val}` | ack `{status}` |
| client -> server | `transport_cmd` | `{cmd, ...}` con `cmd` in `play_start`, `play_stop`, `record_start` (`channel?`), `record_stop`, `stop_all`, `panic`, `rewind`, `seek` (`position` s), `seek_beat` (`beat`), `undo`, `redo`, `toggle_loop` | ack `{status,message?}` |
| server -> client | `daw_state_update` | stato completo come `/api/daw/state` | emesso a ogni cambio di stato |
| server -> client | `daw_tick` | `{t,is_playing,is_recording,bpm,beats_per_measure[,track_counts,track_durations,max_duration]}` | 20 Hz in record, 10 Hz in play; i contatori al massimo ogni 0,25 s |
| server -> client | `record_activity` | `{channels:[{channel,count}]}` | solo in record, solo se ci sono note attive |
| server -> client | `midi_event_new` | `{ch,beat,type,idx[,note,note_num,vel][,cc_num,cc_val]}` | durante record; emesso con `socketio.emit` senza namespace |
| server -> client | `midi_playback_beat` | `{beat}` | ~10 Hz in play |

## 4. Come usare la checklist

- Ogni riga della sezione 1 deve avere una posizione nuova verificata prima di `ui-acceptance`.
- Test automatici di partenza: `tests/test_baseline_smoke.py` (copre soprattutto righe 15-17, 20-22, 51, 61 e i preset 4-9); il resto va verificato in browser o sul Pi.
- Quando una route o un evento cambia contratto, aggiornare le sezioni 2-3 e `docs/contracts.md`.
