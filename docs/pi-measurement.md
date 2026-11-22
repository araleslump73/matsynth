# Misura della baseline sul Raspberry Pi (da eseguire a mano)

Stato: **preparazione soltanto**. Nessuna misura e' stata eseguita; i numeri della baseline vanno raccolti sul Pi Zero 2 W prima di modificare il codice applicativo (fasi successive a `ui-baseline`) e ripetuti dopo, con la stessa procedura. Il piano (sez. 12, prova 6) richiede: nessun OOM, nessuna nuova crescita persistente oltre il 10% dopo cicli ripetuti, nessuna regressione misurabile del jitter.

## Cosa e' misurato

`tools/pi_baseline.sh` e' di sola lettura: legge `/proc` (e `vcgencmd` se c'e') e stampa un CSV.

| Colonna | Significato |
| --- | --- |
| `app_rss_kb`, `fluid_rss_kb` | RSS del processo Python di MatSynth e di `fluidsynth` |
| `app_cpu_pct`, `fluid_cpu_pct` | CPU come % di UN core (utime+stime nell'intervallo); 400 = tutti e 4 i core |
| `mem_avail_kb`, `swap_used_kb` | `MemAvailable` e swap usato |
| `load1`, `temp_c`, `throttled` | load average, temperatura SoC, flag di throttling (`0x0` = nessuno) |
| `# jitter_*_ms` (con `--jitter`) | ritardo medio/p99/max di uno `sleep(1 ms)` in Python, a inizio e fine prova |

Limite dichiarato: il valore di jitter e' un **proxy** (latenza dello scheduler sotto il carico presente), non il jitter MIDI end-to-end tra evento programmato e ricezione da FluidSynth/tastiera. Il playback attuale (`_playback_loop`, polling a 1 ms) non e' strumentato e misurare il MIDI in uscita richiederebbe un loopback o uno strumento di cattura che oggi non e' definito: se serve, va deciso a parte e annotato come metodo diverso (non confrontabile con questo proxy).

## Procedura

Prerequisiti: Pi con il servizio MatSynth e FluidSynth attivi nella configurazione abituale, nessun altro carico (chiudere browser aperti sul Pi, un solo client web collegato). Il repository sul Pi non e' necessario: bastano lo script e la fixture. Copiali a mano (per esempio con una chiavetta o `scp` dal tuo PC); questa preparazione non li ha copiati.

File da portare:

- `tools/pi_baseline.sh`
- `tests/fixtures/workload_16x500.mid` (16 tracce, 500 coppie nota per traccia, 120 BPM, circa 62 s; 64 KB)

1. **Riposo.** Con l'app avviata e nessuna attivita':

   ```bash
   bash pi_baseline.sh -d 60 -i 2 -l idle -o idle.csv --jitter
   ```

2. **Carico fixture.** Dal browser: menu File > Import from PC, scegliere `workload_16x500.mid`. Poi, subito:

   ```bash
   bash pi_baseline.sh -d 180 -i 2 -l load_import -o load_import.csv --jitter
   ```

   Durante i 180 s premere Play dal browser, lasciar suonare l'intero brano (62 s), fermare, ripetere Play/Stop altre 2 volte. Annotare a mano l'orario di ogni Play/Stop.

3. **Cicli ripetuti (crescita RSS).** Ripetere 5 volte: Clear All, Import della fixture, Play fino alla fine. In parallelo:

   ```bash
   bash pi_baseline.sh -d 900 -i 5 -l cycles -o cycles.csv
   ```

   Si guarda la tendenza di `app_rss_kb` tra la fine di ogni ciclo: la soglia del piano e' +10% persistente.

4. **Registrazione sotto carico** (solo con tastiera MIDI collegata, quindi opzionale): armare un canale, Record con la fixture caricata, suonare 30 s.

   ```bash
   bash pi_baseline.sh -d 60 -i 1 -l rec_over_load -o rec.csv --jitter
   ```

Lo script rifiuta di sovrascrivere un file `-o` esistente. Se non trova i processi stampa un avviso e lascia le colonne vuote; in quel caso passare i PID a mano: `PID_APP=1234 PID_FLUID=567 bash pi_baseline.sh ...`.

## Cosa riportare

Per ciascuna prova: i CSV, la versione del codice (hash git) in esecuzione sul Pi, modello e RAM (`cat /proc/device-tree/model; grep MemTotal /proc/meminfo`), se `throttled` e' sempre `0x0`, e qualsiasi OOM nel `dmesg`/`journalctl -k`. Se il workload 16x500 non regge gia' nella baseline (OOM, swap in crescita, playback che salta), dichiararlo e indicare il carico massimo verificato, come richiede il piano: non e' un fallimento della misura.

I risultati vanno registrati in un file separato (per esempio `docs/handoff/pi-baseline-results.md`) senza dati privati del Pi (hostname, IP, credenziali, backup).
