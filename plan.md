# MatSynth: piano di razionalizzazione dell'interfaccia e della DAW MIDI

## 1. Obiettivo e decisioni concordate

Trasformare MatSynth in uno strumento con due esperienze coerenti:

- **Suona**: scegliere e modificare suoni, salvare e richiamare scene, comandare FluidSynth o strumenti MIDI esterni. Deve funzionare bene soprattutto su smartphone, come il display di una tastiera moderna.
- **Studio**: registrare, riprodurre e montare MIDI su 16 tracce, con mixer, click, loop, clip ed editor MIDI. Il tablet e' il dispositivo principale per il montaggio.

L'editor MIDI e' una sottovista di Studio, a schermo intero quando necessario. Libreria e Impostazioni sono strumenti di supporto, non ulteriori modalita' musicali principali.

Decisioni confermate dall'utente:

1. Registrazione e riproduzione **MIDI**, non registrazione audio.
2. Destinazione per traccia: **FluidSynth interno oppure strumento MIDI esterno**, per esempio Roland FANTOM-08 o altra tastiera collegata.
3. Scene complete dei 16 canali e del routing; **layer e split rimandati**.
4. Salvataggio di **progetti completi**, mantenendo importazione/esportazione MIDI.
5. Monitor verso FluidSynth attivo per impostazione iniziale; monitor verso tastiera esterna spento e attivabile esplicitamente.
6. Richiamo di scene complete consentito soltanto con Play e Record fermi.
7. Cambiare vista non ferma il trasporto, non cambia suoni e non riavvia il servizio.
8. Sono consentite modifiche backend necessarie a questa esperienza; non una riscrittura generale della DAW.

Questo documento e' una specifica da implementare, non una descrizione di funzionalita' gia' completate.

## 2. Baseline verificata e vincoli

Repository: `araleslump73/matsynth`. Base applicativa: merge su `main` **c20d3800b211086ed6f2dc5f6ac3325deedd1b48**, contenente il codice recuperato dal Raspberry Pi. Ramo di lavoro: `develop`; alla verifica il suo HEAD era `e9fc8da`, un successivo commit di deployment, senza differenze nei sorgenti applicativi rispetto a main.

Sono comparsi anche cambiamenti locali relativi alla rete. Chi implementa deve ricontrollare il worktree, integrarli solo se pertinenti e non sovrascriverli. Non ripristinare automaticamente il vecchio stash dell'interfaccia: la precedente prova di integrazione aveva molti conflitti.

### Architettura attuale

Browser -> Flask/Flask-SocketIO sulla porta 5000 -> un'istanza `MultiTrackDAW` e FluidSynth. I comandi di configurazione del synth passano anche dalla sua console TCP locale, porta 9800. MIDI gestito con mido/python-rtmidi; audio prodotto da FluidSynth o dagli strumenti esterni.

Il dispositivo verificato e' **Raspberry Pi Zero 2 W**, con circa **416 MiB di RAM utilizzabile**. Al controllo non era collegata una tastiera MIDI esterna. I test hardware del routing restano quindi da eseguire: non dichiarare verificata la compatibilita' della FANTOM sulla base delle sole porte simulate.

| Sorgente relativo alla radice del repository | Evidenza rilevante |
| --- | --- |
| `home\matteo\matsynth_web\templates\index.html` | Interfaccia monolitica con menu, trasporto, 16 tracce, pannelli synth, piano roll e JavaScript inline. Comandi duplicati e operazioni riferite all'ultimo clip. |
| `home\matteo\matsynth_web\static\style.css:1` | Font remoto e numerose regole responsive sovrapposte. |
| `home\matteo\matsynth_web\daw_recorder.py:103` | Selezione input per parole chiave; una sola uscita, cercata come FluidSynth. Nessun routing esterno per traccia. |
| `home\matteo\matsynth_web\daw_recorder.py:1791` | Il lettore MIDI lavora durante Record; il monitoring live attuale avviene tramite collegamento ALSA diretto, non tramite la DAW. |
| `home\matteo\startfluid.sh:80` | `midi.autoconnect=1`, oltre alla possibile connessione esplicita con `aconnect`. Da coordinare con il nuovo router, altrimenti si rischiano note doppie. |
| `home\matteo\matsynth_web\daw_recorder.py:1146` | Stato condiviso di trasporto, tracce e clip; eventi memorizzati in beat. |
| `home\matteo\matsynth_web\daw_recorder.py:586` | Undo a snapshot leggeri, massimo 10 livelli. Cache delle mappe note/densita'. |
| `home\matteo\matsynth_web\app.py:453` | Preset JSON: SoundFont, canali e impostazioni globali. |
| `home\matteo\matsynth_web\app.py:583` | Il richiamo preset non carica automaticamente il SoundFont; la persistenza dei parametri applicati va resa coerente. |
| `home\matteo\matsynth_web\app.py:1729` | Salvataggio attuale come SMF; non e' un progetto nativo che preserva clip e routing. |
| `home\matteo\matsynth_web\daw_recorder.py:2069` | Import attuale con BPM globale, 3/4 o 4/4; Bank LSB e Program Change nel tempo richiedono attenzione per il routing esterno. |

### Non-obiettivi

- Registrazione audio USB, waveform audio, mixdown audio, plugin/VST, streaming audio al browser.
- Editor proprietario dei suoni Roland, trasferimento automatico di scene Roland, gestione SysEx specifica del produttore.
- Layer, split, arpeggiatore, tracce illimitate, tempo map, MIDI Clock/master-slave.
- Nuovo framework SPA, database server, account utente o piattaforma cloud.
- Riscrittura della gestione WiFi/hotspot: integrare lo stato disponibile, senza supporre automatismi non verificati.

L'audio USB della FANTOM non e' un prerequisito. USB GENERIC sullo strumento e' MIDI-only; la compatibilita' audio Linux in modalita' VENDOR non e' stata verificata ed e' fuori da questo intervento.

## 3. Terminologia e modello mentale

| Termine nell'interfaccia | Significato |
| --- | --- |
| **Suono** | Strumento/bank/program di FluidSynth, oppure riferimento Bank MSB/LSB + Program Change di una destinazione esterna. |
| **Parte** | Una delle 16 configurazioni di ingresso, destinazione, canale MIDI e parametri. |
| **Scena** | Fotografia delle 16 parti, di un SoundFont interno e delle impostazioni globali interne. Non contiene registrazioni. |
| **Traccia** | Contenitore di eventi e clip MIDI in Studio, inizialmente associato alla parte con lo stesso ID. |
| **Clip** | Segmento/take MIDI selezionabile e montabile su una traccia. |
| **Progetto** | Eventi, clip, nomi/colori, routing, configurazione sonora, BPM, metro e loop: tutto cio' che serve per riaprire il lavoro. |
| **File MIDI** | Formato di scambio musicale. Non sostituisce il progetto nativo. |

Gli attuali preset completi diventano **Scene** nella nuova UI. Non introdurre subito un ulteriore formato di preset per singolo suono.

Prima versione: relazione 1:1 tra 16 parti e 16 tracce. Modificare il suono della parte 3 modifica anche il suono della traccia 3: indicarlo nel dettaglio, senza duplicare configurazioni indipendenti nascoste.

ID logici/API 0-15, etichette utente 1-16. Il canale di uscita MIDI e' un campo separato dall'ID della traccia. Il canale 10 puo' essere suggerito come batteria interna, ma non imporre tale interpretazione agli strumenti esterni.

## 4. Architettura delle schermate

### Elementi condivisi

- Navigazione sempre riconoscibile: **Suona | Studio**, Libreria e Impostazioni come pulsanti secondari.
- Stato connessione, stato trasporto, indicatore modifiche non salvate e **PANIC** raggiungibili da entrambe le viste.
- Un solo pannello di controllo sonoro riutilizzato da Suona e dall'inspector della traccia.
- Una sola finestra Libreria con sezioni **Scene / Progetti / MIDI / SoundFont**.
- Una sola sorgente di stato e un solo collegamento Socket.IO per pagina.
- Il cambio vista e l'apertura di un pannello non inviano comandi musicali.

### Suona: smartphone, orientamento verticale

```text
MatSynth   Connessione   Stato Play/Rec   PANIC
Scena attuale *                [Salva] [...]
------------------------------------------------
Scene preferite / ricerca / precedente-successiva
[Scena A] [Scena B]
[Scena C] [Scena D]
------------------------------------------------
Parte selezionata: 1    [scegli parte 1-16]
Suono: Grand Piano            [Cambia suono]
Destinazione: Interno / Strumento - canale
Volume      [slider grande + valore]
[Suono] [Mix] [Routing]
------------------------------------------------
Master interno    [volume]
[Suona] [Studio]       [Libreria] [Impostazioni]
```

Mostrare prima scena e suono, non una tabella compressa di 16 canali. Le altre parti restano accessibili tramite elenco; nessuna funzione va eliminata per farla entrare sul telefono.

Tablet: scene a sinistra, parti al centro, dettaglio della parte a destra. Mostrare piu' informazioni, senza introdurre una seconda implementazione.

### Studio: tablet

```text
Progetto *   [Progetti/MIDI] [Salva]      PANIC
[Inizio] [Play/Pausa] [Record] [Stop]  Posizione
BPM  Metro  Click  Loop A-B
------------------------------------------------
Tracce                 Timeline / clip / righello
1 Piano  REC M S        [Take 1][Take 2]
2 Pad    REC M S                [Take 3]
3 Esterno REC M S       [Take 4]
...                    ...
------------------------------------------------
Selezione: clip Take 2, traccia 1
[Taglia] [Copia] [Incolla] [Duplica] [Dividi] [...]
Inspector: Suono | Mix | Routing
[Apri editor MIDI]
```

- Inspector laterale su tablet largo; pannello inferiore su tablet stretto e telefono.
- Mixer come pannello/sottovista di Studio, non seconda DAW.
- Smartphone: controllo trasporto, arm/mute/solo, scelta traccia e operazioni esplicite accessibili; editor apribile a schermo intero. Non promettere la stessa densita' del tablet.
- Nascondere una traccia e' solo una preferenza di visualizzazione, mai un mute implicito. Deve esistere un elenco per ripristinarla.

### Editor MIDI

- Intestazione con progetto, traccia, clip/intervallo e pulsante **Torna a Studio**.
- Piano roll, tastiera verticale, righello musicale e corsia velocity.
- Strumenti espliciti: seleziona, disegna, cancella; zoom e scorrimento separati dall'editing.
- Barra contestuale: sposta, durata, pitch, velocity, taglia/copia/incolla, quantizza, trasponi, elimina.
- **Eventi** come scheda dello stesso editor per CC, sustain, pitch bend, pressure, bank/program e ispezione numerica.
- Nessuna azione importante disponibile soltanto con hover, tasto destro o pressione prolungata.

## 5. Flussi principali e comportamento

### Scegliere e modificare un suono

1. Selezionare una parte; intestazione e destinazione restano visibili durante la modifica.
2. Interno: browser degli strumenti del SoundFont, con ricerca; bank/program visibili nei dettagli.
3. Esterno: nome assegnato dall'utente e campi opzionali Bank MSB, Bank LSB, Program Change. Non mostrare i nomi di FluidSynth come se fossero quelli della tastiera.
4. Modificare volume/pan e parametri supportati. Evidenziare la scena come modificata; non sovrascriverla automaticamente.
5. Salvare la scena oppure **Salva come**. Conferma esplicita per sovrascrittura; niente suffissi silenziosi o perdita del nome corrente.

Parametri interni da preservare: volume CC7, pan CC10, attack CC73, decay CC75, release CC72, cutoff CC74, resonance CC71; gain, reverb e chorus globali. Non chiamare l'editor "ADSR completo": il controllo sustain dell'inviluppo non e' presente.

Per gli esterni, i CC sonori non sono universalmente supportati: mostrare solo i controlli dichiarati nella configurazione del dispositivo, lasciando disponibile l'editor eventi generico. **Master interno** e FX interni non controllano l'audio della tastiera esterna.

### Richiamare una scena

- Richiamo senza cambio pagina e senza riavvio del servizio per scene sul SoundFont gia' caricato.
- Con modifiche non salvate: Salva / Scarta / Annulla.
- Con Play o Record attivi: richiamo bloccato con spiegazione e comando Stop; non fermare automaticamente.
- Validare scena, SoundFont e associazioni porte prima di applicarla.
- Se cambia SoundFont, richiedere conferma dell'interruzione possibile del suono, mostrare caricamento e verificare l'esito effettivo.
- Una sola selezione SoundFont globale nella prima versione; niente precaricamento di librerie multiple sul Pi Zero.
- Gestire note attive e sustain sulle parti interessate prima di sostituire il routing. Non promettere seamless switching.
- Porta esterna assente: richiamo esplicitamente parziale solo dopo consenso, con parte segnata non disponibile; nessun fallback automatico verso un altro strumento.
- MIDI inviato a una tastiera non implica conferma del suo stato sonoro: non mostrare una falsa verifica hardware.

### Registrare

1. Creare/aprire un progetto; scegliere input, filtro canale e destinazione di ogni traccia.
2. Armare le tracce; controllare indicatori di ingresso e monitor.
3. Impostare posizione, BPM, metro e click; avviare Record.
4. Registrare MIDI anche se il monitor e' spento. L'ascolto e la registrazione sono decisioni indipendenti.
5. Stop conclude il take e crea clip coerenti. Chiudere correttamente note/sustain ancora aperti, con comportamento testato.
6. Conservare la registrazione in overdub e la riproduzione delle altre tracce gia' presenti.

Record deve fallire prima dell'avvio se nessun input utilizzabile e' associato alle tracce armate. Non impostare uno stato "Recording" quando il thread non puo' leggere MIDI.

### Trasporto

- Play riparte dalla posizione corrente; Pausa durante playback rilascia le note di playback e conserva la posizione.
- Stop termina Record/Play/click, senza cancellare dati e senza rewind implicito.
- Inizio/Rewind torna a zero esplicitamente.
- Durante Record non offrire una pausa non implementata; mostrare Stop.
- Seek durante playback secondo il comportamento esistente verificato; seek durante Record non consentito.
- Conservare BPM globale e metri 3/4 e 4/4. Conteggio visualizzato in battute/beat, con secondi nel dettaglio.

### Montaggio e selezione

- La selezione e' esplicita: traccia, clip, intervallo oppure note. Ogni comando dichiara il suo oggetto.
- Rimuovere il riferimento implicito all'**ultimo clip**. Niente selezione: comando disabilitato e motivazione disponibile.
- Copia non modifica il progetto. Taglia e' un'unica operazione annullabile: copia + rimozione atomica.
- Incolla dichiara traccia e posizione; per clip crea metadati clip, non soli eventi invisibili.
- Duplicazione traccia deve dichiarare se copia solo MIDI o anche configurazione; conservare la variante legacy "solo MIDI". Destinazione occupata: conferma.
- Sposta, ridimensiona, dividi e unisci operano su ID clip espliciti. Prima versione: unione sulla stessa traccia.
- Durate note e note-off devono rimanere accoppiati. Copia/taglio di note selezionate include la coppia completa; non eliminare da solo un note-off fuori intervallo.
- Definire ownership delle note ai confini dei clip prima di modificare gli algoritmi: split non deve cambiare il playback dell'intera timeline; eventuale taglio/retrigger di una nota richiede un'azione esplicita. Copia/spostamento non deve lasciare note sospese.
- Undo/redo: un gesto completato = un livello, incluso editing batch e taglio. Conservare il limite attuale di 10 livelli; non estenderlo senza misura RAM.

**Unita' musicali:** un beat = una semiminima. Snap 1/4 = 1 beat, 1/8 = 0.5, 1/16 = 0.25. Centralizzare etichette e conversioni: le attuali etichette piano-roll possono essere ambigue. Non reinterpretare dati salvati senza migrazione.

## 6. Mappa completa delle funzioni

| Funzione attuale/richiesta | Collocazione canonica futura |
| --- | --- |
| Scelta strumenti sui 16 canali | Suona > parte; Studio > inspector della traccia, stesso controllo |
| Volume/pan e parametri sonori | Dettaglio parte/inspector, schede Mix e Suono |
| Gain, reverb, chorus | Master interno condiviso |
| Salva/richiama/cerca/rinomina/elimina preset | Libreria > Scene, apertura contestuale da Suona |
| Preferiti scene e richiamo rapido | Suona; metadati libreria senza motore musicale aggiuntivo |
| SoundFont: elenco/caricamento | Libreria > SoundFont; gestione dipendenze nel richiamo scena |
| Nuovo/salva/salva come/apri | Studio e Libreria > Progetti |
| MIDI: salva/lista/importa/aggiungi/scarica/elimina | Libreria > MIDI; separato dal salvataggio progetto |
| Record/play/pausa/stop/rewind/seek | Trasporto unico |
| BPM, 3/4-4/4, click | Trasporto Studio; dettaglio accessibile anche da Suona |
| Loop attivo, punti A/B, loop da selezione | Trasporto e selezione timeline |
| Arm/mute/solo/attivita' | Traccia Studio; riepilogo nel dettaglio parte |
| Nomi/colori/duplicazione/svuota traccia | Menu della traccia |
| Nascondi/ripristina tracce; altezza/zoom | Preferenze Studio e menu traccia |
| Selezione intervallo, copia/incolla/taglia | Barra contestuale timeline/editor |
| Clip move/delete/transpose/resize/split/merge | Barra contestuale e inspector clip |
| Quantizza: griglia, strength, swing e ambito | Finestra unica condivisa, aperta dalla selezione |
| Trasposizione per semitoni/ottave | Barra contestuale selezione |
| Piano roll, aggiungi/sposta/ridimensiona/elimina note | Studio > editor MIDI |
| Velocity e modifica batch note | Editor MIDI |
| Lista/modifica/eliminazione eventi; attivita' live | Editor MIDI > Eventi |
| Preview note | Editor MIDI, inviata alla destinazione della traccia |
| Undo/redo | Barra Studio/editor, una sola cronologia |
| Clear range, purge, clear all | Menu contestuale o Progetto, con conferma esplicita |
| Reset canale/refresh stato synth | Dettaglio parte / diagnostica; reset con conferma |
| PANIC | Sempre visibile; tutte le destinazioni utilizzate |
| Input MIDI e nuova uscita MIDI | Routing parte/traccia; associazione dispositivi in Impostazioni |
| ALSA audio output, riavvio hardware | Impostazioni > Hardware, fuori dal flusso live |
| IP/hostname e stato rete disponibile | Impostazioni > Connessione e indicatore globale |

Questa tabella va trasformata in checklist di regressione durante l'implementazione. Le funzioni avanzate diventano contestuali, non vengono rimosse.

## 7. Routing MIDI: intervento backend indispensabile

### Contratto per parte/traccia

Esempio di struttura nuova; ID e canali qui sono zero-based:

```json
{
  "part_id": 0,
  "input": {"device_alias": "keyboard-main", "channel": 0},
  "destination": {"kind": "internal", "device_alias": null, "channel": 0},
  "monitor": true,
  "sound": {"bank": 0, "program": 0},
  "cc": {"7": 100, "10": 64, "73": 64, "75": 64, "72": 64, "74": 64, "71": 64}
}
```

Per destinazione esterna: `kind: external`, alias della porta e `bank_msb`, `bank_lsb`, `program` opzionali. Non convertire automaticamente un bank SoundFont in un bank Roland.

- La traccia resta logica 0-15; gli eventi in playback vengono rimappati sul canale della sua destinazione.
- `input.channel: null` significa tutti i canali di quell'input; l'UI deve esplicitare che ciascuna traccia corrispondente ricevera' gli eventi.
- Default migrato: traccia N da input canale N verso FluidSynth canale N; monitor interno attivo.
- Esterno: monitor inizialmente spento. Può registrare la tastiera con Local Control ON senza reinviarle le stesse note.
- Modalita' controller con Local OFF: l'utente abilita il monitor esterno esplicitamente. Non inviare Local Control automaticamente.
- Due tracce sullo stesso device/canale condividono suono/CC e possono interferire: avviso chiaro, non isolamento audio fittizio.

### Implementazione minima affidabile

1. Piccolo router condiviso, per esempio `midi_router.py`, con registro porte, apertura/chiusura e invio. Non un motore DAW alternativo.
2. Un solo lettore per ogni input aperto, attivo anche fuori da Record. Il recorder si aggancia al flusso per le tracce armate; nessun secondo consumatore `iter_pending()` della stessa porta.
3. Live input, playback, preview e comandi CC/bank/program passano dalla stessa risoluzione delle destinazioni.
4. Le porte sono condivise tra tracce; niente thread o apertura output per ogni nota/traccia. Gestire serializzazione degli invii e cambi di configurazione.
5. Monitor e registrazione sono indipendenti. Mute/solo restano controlli del playback DAW; non alterare segretamente la registrazione o il Local Control del dispositivo.
6. Click resta una destinazione logica **interna**, non l'uscita esterna della traccia selezionata. Conservare inizialmente il canale interno 10 e le note 76/77; non registrare il click e non cambiare il program della batteria per attivarlo.
7. La mancanza di FluidSynth non impedisce automaticamente tracce esclusivamente esterne; riportare separatamente l'indisponibilita' del synth/click interno.

### Connessioni ALSA e migrazione

Il nuovo monitoring software e il vecchio autoconnect non possono essere attivi insieme sulle stesse rotte.

- Introdurre una scelta di compatibilita' `legacy/managed` nel startup/config; mantenere legacy durante la transizione.
- In managed: disabilitare l'autoconnect FluidSynth e non creare la vecchia connessione tastiera -> FluidSynth tramite startup.
- Rimuovere solo connessioni legacy appartenenti a MatSynth, non tutte le connessioni ALSA del sistema.
- Verificare la modalita' managed al riavvio, prima di renderla predefinita. Rollback a legacy documentato.
- Nessun reader/router nel browser: la navigazione e la perdita del WiFi non devono interrompere il MIDI gestito sul Pi.

### Porte, errori e note sospese

- Esporre ingressi e uscite reali; non affidarsi a parole chiave FANTOM/USB o all'ID numerico ALSA 128:0.
- Persistenza tramite alias utente e descrittore della porta. Numero client/PID solo dato runtime.
- Dopo riconnessione: associazione solo se non ambigua; due dispositivi identici richiedono scelta. Mai scegliere silenziosamente un altro strumento.
- Escludere System/Midi Through/porte proprie dagli input proposti normalmente. Non usare deduplicazione temporale delle note come rimedio ai loop.
- In perdita output: stato offline visibile e errore registrato, niente falso successo. Conservare destinazione configurata e dati del progetto.
- Tracciare note inviate per destinazione/canale; note-off deve raggiungere la destinazione originale anche dopo una modifica.
- Stop, mute/solo che interrompono voci, seek, loop, cambio routing e PANIC devono rilasciare note/sustain interessati. PANIC include CC64=0, CC123 e CC120 dove supportati, oltre a note-off espliciti quando necessari.
- Non assumere di poter spegnere retroattivamente una porta scollegata: al ritorno mostrare stato di ripristino, effettuare cleanup sulle rotte MatSynth prima di nuovi invii.
- Nessun inoltro automatico di SysEx, clock o realtime tra porte nella prima versione.

## 8. Scene, progetti e compatibilita' dei dati

### Scene

Schema versionato con ID stabile, nome, SoundFont, 16 parti/routing/CC e impostazioni globali interne. Favoriti e ordine appartengono alla libreria.

Leggere i JSON legacy senza distruggerli. Adattare `channels`, `global_settings`, bank/program e nomi CC al modello nuovo. Valori non presenti: default espliciti e compatibili. Conservare CC75 per decay.

Prima del richiamo completare preflight; applicare e salvare lo stato effettivo, compresi pan e parametri di canale. Una sequenza parzialmente fallita non va dichiarata riuscita. Per strumenti esterni distinguere stato desiderato, porta disponibile e messaggi inviati.

Un SoundFont mancante richiede associazione/caricamento esplicito, non la sostituzione silenziosa con General MIDI.

### Progetto nativo

Un file JSON versionato, per esempio `.matsynth.json`, salvato sul Pi con scrittura atomica. Non aggiungere database o archivi multi-file senza necessita'.

Contenuto minimo:

- `schema_version`, ID/nome progetto.
- BPM, beats-per-measure e loop A/B.
- Snapshot sonoro completo, non solo riferimento al nome di una scena modificabile.
- 16 tracce con ID, nome, colore, parte/routing, mute/solo.
- Eventi `(beat, bytes MIDI)` validati, metadati clip e contatore ID clip.
- Riferimenti SoundFont e alias dispositivo; nessuna credenziale di rete.

Non serializzare porte aperte, thread, note attive, cache, clipboard o cronologia undo. Alla riapertura il trasporto resta fermo, il click non parte e le tracce non si riarmano automaticamente. Preferenze di vista/selezione sono separate dal contenuto musicale.

Salva sovrascrive il progetto corrente dopo conferma quando necessaria; Salva come crea una copia distinta. Nuovo/Apri/Import sostitutivo con dati modificati: Salva / Scarta / Annulla.

Caricare e validare l'intero documento prima di sostituire la sessione corrente. Versione futura sconosciuta, evento invalido o errore disco: errore esplicito e progetto in memoria preservato. Prevedere esportazione/download del progetto per backup e trasferimento.

### MIDI import/export

- Mantenere gli endpoint SMF esistenti, esplicitando **Importa MIDI** ed **Esporta MIDI**.
- Sostituisci oppure Aggiungi, con conferma dell'ambito. Creare clip selezionabili anche per dati importati.
- Correggere i punti accoppiati al nuovo routing: Bank MSB/LSB corretti, Program Change nel tempo conservati, nessuna applicazione incondizionata a FluidSynth di programmi destinati all'esterno.
- Preservare note, CC, sustain, pitch bend e pressure supportati.
- Export standard iniziale su 16 canali logici, con nomi traccia e impostazioni iniziali corrette per ogni tipo di destinazione. Avviso: gli alias delle porte e i confini dei clip non sono portabili in un normale SMF.
- Per tempo map, metri non supportati o dati fuori ambito: rifiuto chiaro oppure conversione esplicita e confermata. Non perdere silenziosamente informazioni chiamando il risultato "import completo".
- Salvare il progetto non deve generare soltanto un MIDI piu' un riferimento fragile alla scena.

## 9. Struttura frontend/backend e contratti

Mantenere Flask, Socket.IO, JavaScript leggero e canvas esistenti. Estrazione progressiva, non sostituzione in un unico grande commit.

Possibile organizzazione, da ridurre se un modulo non ha responsabilita' reale:

```text
home\matteo\matsynth_web\
  app.py                    API/adattatori, non nuovo motore musicale
  daw_recorder.py            trasporto, eventi, clip, editing e undo
  midi_router.py            unico registro/dispatcher MIDI
  project_store.py           schema/validazione/salvataggio nativo, se utile
  templates\index.html       shell e include Jinja
  templates\partials\        Suona, Studio, editor, libreria, impostazioni
  static\js\
    api.js                  fetch/ack/errori condivisi
    state.js                stato condiviso e selezione
    shell.js                navigazione/pannelli
    live.js                 esperienza Suona
    studio.js               timeline, tracce, trasporto
    midi_editor.js          piano roll ed eventi
    library.js              scene/progetti/MIDI
  static\vendor\            dipendenze browser locali con licenze
  static\style.css           base/tokens; sezioni coerenti per vista
```

Non modificare copie legacy `app.old*.py`, `indexold.html` e `loop.html` come se fossero entrypoint. Conservare gli URL `/settings` e `/presets` durante la transizione, con lo stesso servizio di stato; poi possono aprire il pannello corrispondente della shell.

### Stato e API

- Stato backend autoritativo. Store frontend con domini connessione, trasporto, parti/scena, progetto, selezione e preferenze.
- Riutilizzare `daw_state_update`, `daw_tick`, eventi attivita' e le API editing esistenti.
- Snapshot su apertura/riconnessione; tick leggeri per posizione. Nessuna chiamata a full-note-map a ogni tick.
- Tutte le nuove azioni attendono esito HTTP/ack e mostrano errori. Timeout non significa successo.
- Aggiunte minime: elenco porte input/output, lettura/aggiornamento routing parte, CRUD progetti, scene versionate e stato caricamento SoundFont.
- Estendere/adattare API preset esistenti, invece di duplicare la logica per una nuova libreria.
- Campo di revisione per modifiche DAW; ID clip stabili. Per note/eventi indicizzati, associare gli indici alla revisione: dopo edit/undo/import una selezione obsoleta va aggiornata o invalidata, mai applicata a eventi diversi.
- Nuovo client invia revisione attesa nelle modifiche indicizzate/batch; conflitto -> 409 e aggiornamento dello stato. I tick non incrementano la revisione di editing.
- Taglia, incolla clip e modifica batch devono essere operazioni coerenti lato backend, con un solo undo e un solo aggiornamento strutturale.
- Non tenere lock di stato durante attese di rete, caricamento SF2 o invio hardware lento.

I payload definitivi e le risposte d'errore devono essere documentati prima di sviluppare separatamente frontend e backend. Non introdurre cambi di firma senza aggiornare i call site.

## 10. Touch, offline e prestazioni

### Requisiti verificabili di interfaccia

- Target touch almeno 44x44 CSS px; controlli primari preferibilmente 48px o piu'.
- Test viewport: 360x800 e 390x844, tablet 768x1024 e 1024x768, desktop 1366x768.
- Niente overflow orizzontale della shell. Lo scorrimento della timeline e' intenzionale e confinato al suo contenitore.
- Etichette leggibili, selezione e stato Record riconoscibili anche senza colore; icone con nome accessibile.
- Pointer Events e gestione coerente di tap/drag/cancel. Zoom/scorrimento non devono creare note o spostare clip accidentalmente.
- Valori editabili numericamente oltre agli slider; controlli di trasporto non dipendono dal doppio tap.
- Dialoghi con focus/chiusura corretti; cambio orientamento non perde selezione o lascia overlay fuori schermo.
- Lingua coerente: italiano nella UI di questa versione; termini MIDI/CC disponibili nel dettaglio.

### Hotspot senza internet

- Servire localmente Socket.IO client, Bootstrap e icone indispensabili; rispettare le licenze.
- Eliminare dipendenze operative da CDN e Google Fonts; preferire font di sistema o asset locali consentiti.
- Il browser deve aprire e usare l'app sul Pi anche con tutte le richieste esterne bloccate.
- Non usare `navigator.onLine` come prova che il Pi sia raggiungibile: conta la connessione effettiva all'app.
- Nessun service worker/PWA obbligatorio: prima ottenere un'app LAN offline corretta senza cache difficili da invalidare.
- SoundFont e scene gia' presenti sul Pi devono essere richiamabili offline.

### Limiti del Pi

- Preservare scritture stato debounce/atomiche, cache note/densita', snapshot undo leggeri e aggiornamenti 10Hz playback / 20Hz Record gia' presenti.
- Non salvare file a ogni movimento dello slider; coalescere CC mantenendo il valore finale.
- Rendering canvas nel browser solo per vista visibile e dati cambiati; niente ridisegno completo delle 16 tracce a ogni tick.
- Nessuna moltiplicazione di socket, timer o listener dopo cambio vista.
- Caricamento SF2 sequenziale con esito osservabile; niente ritardi fissi usati come verifica.
- Prima/dopo: misurare RAM/RSS, CPU, jitter MIDI e swap con lo stesso SoundFont e lo stesso progetto. Non chiamare "ottimizzazione" una regressione nascosta dal browser.

## 11. Todo di implementazione e dipendenze

Ogni voce deve produrre una modifica verificata e una breve nota di handoff. La numerazione non implica dipendenze aggiuntive.

| ID | Lavoro e risultato | Dipendenze |
| --- | --- | --- |
| `ui-baseline` | Inventario/regressioni delle funzioni della tabella; fixture MIDI e preset; misure sul Pi; backup dati privati senza pubblicarli. | Nessuna |
| `ui-contracts` | Definire schema parti/scene/progetti, semantica clip/note, revisioni, API/errori, compatibilita' legacy e criteri di caricamento. | ui-baseline |
| `ui-shell` | Estrarre stato/API/controlli condivisi dalla pagina; shell Suona/Studio e selezione esplicita, mantenendo comportamento esistente. | ui-contracts |
| `ui-routing` | Router continuo, routing interno/esterno, monitor sicuro, cleanup e modalita' startup managed/legacy; test porte simulate. | ui-contracts |
| `ui-persistence` | Scene versionate e progetto nativo atomico; adattatori legacy; import/export MIDI coerenti con routing. | ui-contracts, ui-routing |
| `ui-live` | Vista Suona, browser suoni, scene/favoriti, controllo parte/master e richiamo senza ricaricare pagina. | ui-shell, ui-routing, ui-persistence |
| `ui-studio` | Timeline/tracce/mixer/inspector, trasporto unico, selezione clip e operazioni contestuali complete. | ui-shell, ui-routing, ui-persistence |
| `ui-editor` | Editor note/eventi, editing touch, cut/copy/paste/batch, quantizza/trasponi e undo coerenti. | ui-studio |
| `ui-offline-settings` | Asset locali, impostazioni hardware con stato/esito reale, responsive/accessibilita', URL legacy compatibili. | ui-shell |
| `ui-acceptance` | Regressioni complete, prove hardware interno/esterno, hotspot offline, prestazioni e documentazione utente/deployment/rollback. | ui-live, ui-editor, ui-offline-settings |

Un'eventuale suddivisione tra AI deve assegnare responsabilita' sui file e contratti gia' definiti, non far riscrivere lo stato condiviso a piu' implementatori. Evitare modifiche parallele non coordinate a `app.py`, `daw_recorder.py` e alla shell.

## 12. Test e criteri di accettazione

Sono criteri futuri da eseguire; questo piano non afferma che siano gia' superati.

### Backend e dati

Usare test Python isolati con porte mido e FluidSynth simulati; la libreria standard `unittest` basta inizialmente. Non richiedere una tastiera per tutti i test.

- Evento input su traccia logica 0 -> una sola coppia note-on/off su FluidSynth canale 0; traccia logica 1 -> solo output esterno canale 4.
- Monitor esterno OFF: eventi registrati, zero reinvii live. ON: un solo invio live per evento; playback e preview seguono la stessa destinazione.
- Porta mancante/ambigua: errore/stato non disponibile; nessuna scelta di fallback e nessuno stato recording fittizio.
- In modalita' managed non coesistono direct-ALSA e software-thru della stessa tastiera.
- Stop/loop/seek/panic/cambio routing, incluse note tenute e sustain: nessuna nota rimane sospesa sulle porte ancora disponibili.
- Click sempre interno, non nel file MIDI registrato e non sulla tastiera esterna per effetto della selezione.
- Bank MSB 1, LSB 2, program 7: export/import mantiene valori corretti; Program Change a beat non zero resta a quel beat.
- Taglia/incolla note che attraversano un confine: coppie complete, beat/durata corretti e un solo undo; split/move/merge non producono note-off orfani.
- Progetto round-trip: eventi, beat, clip/ID, nomi/colori, routing, snapshot suoni, BPM/metro/loop equivalenti. All'apertura trasporto fermo.
- Preset legacy: stessi bank/program, CC e impostazioni globali, inclusi pan quando presente e decay CC75.
- File invalido/versione futura/errore disco: sessione precedente intatta, errore esplicito; nessun file parziale sostituisce il precedente.
- Selezione di eventi con revisione obsoleta: rifiuto controllato, non modifica della nota sbagliata.

### Browser

- Nessun errore JavaScript o risorsa indispensabile mancante nei viewport elencati.
- Almeno 20 cambi Suona/Studio/editor: esattamente un socket attivo, nessun timer/listener duplicato, trasporto e suoni invariati.
- Ogni azione della tabella funzionale ha una posizione verificata e una selezione esplicita.
- Richiamo scena sullo stesso SoundFont: nessuna navigazione/reload e nessun riavvio servizio.
- Play/Record attivi: scena bloccata, cambio vista consentito.
- Test con tutte le richieste internet bloccate, mantenendo raggiungibile il Pi: app e funzioni essenziali operative.
- Smartphone: cambio suono e richiamo scena accessibili senza gesti nascosti; tablet: registrazione, selezione clip e modifica note eseguibili con touch.

### Prova musicale sul Raspberry Pi

1. Collegare una tastiera USB MIDI e verificare input/output reali; per la FANTOM usare la configurazione MIDI compatibile, senza basarsi sull'audio USB.
2. Registrare una parte interna e una esterna, riascoltarle insieme, verificare program/CC, mute/solo, click e overdub.
3. Provare Local ON + monitor esterno OFF e controller Local OFF + monitor ON: ascoltare e contare che non ci siano note doppie.
4. Scollegare/ricollegare il dispositivo con note/sustain attivi; verificare errore, associazione porte e cleanup.
5. Collegare tablet/telefono all'hotspot senza internet; riaprire app, scena e progetto.
6. Con fixture identica di 16 tracce e 500 coppie di note per traccia, confrontare CPU/RSS/jitter con la baseline. Obiettivo: nessun OOM, nessuna nuova crescita persistente oltre il 10% dopo cicli ripetuti e nessuna regressione misurabile del jitter rispetto alla baseline. Se il workload non regge gia' nella baseline, dichiararlo e definire un limite verificato, non nasconderlo.

Non attivare managed come default e non dichiarare completo il supporto esterno finche' le prove hardware pertinenti non sono superate.

### Definizione di completamento

Le due viste e l'editor sono utilizzabili su dispositivi touch; tutte le funzioni esistenti hanno una collocazione; registrazione/playback interno ed esterno sono verificati; scene e progetti riaprono correttamente; l'app funziona nell'hotspot senza internet; prestazioni e compatibilita' sono documentate.

Aggiornare README e documentazione direttamente pertinente con guida Suona/Studio, differenza scena/progetto/MIDI, Local Control/monitor, migrazione dati, deployment e rollback. Non includere credenziali, archivi privati o backup del Pi nei commit.

## 13. Istruzione breve da passare a un'altra AI

> Implementa questo piano nel repository araleslump73/matsynth, sul ramo di lavoro concordato. Prima verifica HEAD/worktree e non sovrascrivere modifiche altrui o il vecchio stash. Usa il codice applicativo recuperato dal Pi come baseline, non le copie legacy. Mantieni Flask/Socket.IO/mido, 16 tracce e gli accorgimenti del Pi Zero 2 W. Segui i contratti e le dipendenze; completa una fase con test prima della successiva. Le viste devono condividere stato e motore. Audio recording, layer/split ed editor proprietario Roland sono fuori scope. Non distribuire sul Pi e non cambiare le sue connessioni MIDI/rete senza approvazione esplicita; la fase di routing richiede prove hardware prima dell'attivazione definitiva. Riporta cosa e' implementato, cosa e' verificato e cosa resta da verificare, senza dichiarare successo dalle sole simulazioni.
