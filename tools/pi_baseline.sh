#!/usr/bin/env bash
# pi_baseline.sh - misura SOLA LETTURA di risorse su Raspberry Pi Zero 2 W.
#
# Cosa fa: campiona a intervalli regolari, leggendo solo /proc (e vcgencmd se
# presente), RSS e CPU del processo Python di MatSynth e di fluidsynth, RAM
# disponibile, swap, load average, temperatura/throttling. In piu', con
# --jitter, misura il ritardo dello scheduler su sleep da 1 ms (la stessa
# cadenza del polling in daw_recorder.py) come proxy di jitter da CPU/IO.
#
# Cosa NON fa: non invia MIDI, non tocca ALSA/aconnect, non riavvia servizi,
# non scrive nulla nel progetto. Scrive solo su stdout (o sul file di -o).
# Il jitter MIDI reale end-to-end NON e' misurato qui (vedi docs/pi-measurement.md).
#
# Uso:  tools/pi_baseline.sh [-d SECONDI] [-i INTERVALLO] [-l ETICHETTA] [-o FILE] [--jitter]
#   -d  durata totale in secondi            (default 60)
#   -i  intervallo di campionamento in s    (default 2)
#   -l  etichetta della prova nel CSV       (default "baseline")
#   -o  scrive il CSV anche su FILE         (default: solo stdout)
#   --jitter  esegue un campionamento di jitter (python3, ~5 s) a inizio e fine
#
# Variabili opzionali: PID_APP, PID_FLUID per forzare i PID.

set -u

DURATION=60
INTERVAL=2
LABEL=baseline
OUT=""
DO_JITTER=0

while [ $# -gt 0 ]; do
    case "$1" in
        -d) DURATION="$2"; shift 2 ;;
        -i) INTERVAL="$2"; shift 2 ;;
        -l) LABEL="$2"; shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        --jitter) DO_JITTER=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "Argomento sconosciuto: $1" >&2; exit 2 ;;
    esac
done

if [ ! -r /proc/meminfo ]; then
    echo "Serve Linux con /proc (da lanciare sul Pi)." >&2
    exit 1
fi

CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)

# --- Individuazione processi (solo lettura di /proc) -----------------------
find_app_pid() {
    local p
    for p in /proc/[0-9]*; do
        [ -r "$p/cmdline" ] || continue
        # cmdline separata da NUL: cerca python che esegue app.py di matsynth_web
        if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -Eq 'python[0-9.]* .*app\.py'; then
            if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q 'matsynth'; then
                echo "${p#/proc/}"; return
            fi
            # fallback: cwd della cartella matsynth_web
            if readlink "$p/cwd" 2>/dev/null | grep -q 'matsynth_web'; then
                echo "${p#/proc/}"; return
            fi
        fi
    done
}

find_fluid_pid() {
    local p
    for p in /proc/[0-9]*; do
        [ -r "$p/comm" ] || continue
        if [ "$(cat "$p/comm" 2>/dev/null)" = "fluidsynth" ]; then
            echo "${p#/proc/}"; return
        fi
    done
}

PID_APP="${PID_APP:-$(find_app_pid)}"
PID_FLUID="${PID_FLUID:-$(find_fluid_pid)}"

# --- Lettori --------------------------------------------------------------
rss_kb()   { awk '/^VmRSS:/ {print $2}' "/proc/$1/status" 2>/dev/null; }
# utime+stime in tick (campi 14 e 15 di /proc/PID/stat; il nome puo' contenere spazi)
cpu_ticks() {
    [ -r "/proc/$1/stat" ] || return
    sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | awk '{print $12 + $13}'
}
mem_avail_kb() { awk '/^MemAvailable:/ {print $2}' /proc/meminfo; }
swap_used_kb() { awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print t-f}' /proc/meminfo; }
load1()    { cut -d' ' -f1 /proc/loadavg; }
temp_c()   { command -v vcgencmd >/dev/null 2>&1 && vcgencmd measure_temp 2>/dev/null | sed "s/[^0-9.]//g"; }
throttled() { command -v vcgencmd >/dev/null 2>&1 && vcgencmd get_throttled 2>/dev/null | sed 's/.*=//'; }

jitter_probe() {
    # 5 s di sleep da 1 ms; riporta ritardo medio/p99/max in ms.
    # Legge solo il tempo: nessun I/O, nessun MIDI. Costa CPU per ~5 s.
    command -v python3 >/dev/null 2>&1 || { echo "n/d,n/d,n/d"; return; }
    python3 - <<'PY'
import time
N = 5000
d = []
for _ in range(N):
    t0 = time.perf_counter()
    time.sleep(0.001)
    d.append((time.perf_counter() - t0 - 0.001) * 1000.0)
d.sort()
print("%.3f,%.3f,%.3f" % (sum(d) / N, d[int(N * 0.99)], d[-1]))
PY
}

emit() {
    echo "$1"
    [ -n "$OUT" ] && echo "$1" >> "$OUT"
}

# --- Intestazione ---------------------------------------------------------
if [ -n "$OUT" ] && [ -e "$OUT" ]; then
    echo "Rifiuto di sovrascrivere $OUT: scegliere un file nuovo." >&2
    exit 1
fi

emit "# pi_baseline label=$LABEL host=$(hostname 2>/dev/null) kernel=$(uname -r) date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
emit "# pid_app=${PID_APP:-NONTROVATO} pid_fluid=${PID_FLUID:-NONTROVATO} duration=${DURATION}s interval=${INTERVAL}s clk_tck=$CLK_TCK"
emit "# cpu_* = % di UN core (il Pi Zero 2 W ha 4 core: 100 = un core pieno)"
if [ -z "$PID_APP" ]; then echo "ATTENZIONE: processo Python di MatSynth non trovato" >&2; fi
if [ -z "$PID_FLUID" ]; then echo "ATTENZIONE: processo fluidsynth non trovato" >&2; fi

if [ "$DO_JITTER" = 1 ]; then
    emit "# jitter_start_ms(mean,p99,max)=$(jitter_probe)"
fi

emit "label,elapsed_s,app_rss_kb,fluid_rss_kb,app_cpu_pct,fluid_cpu_pct,mem_avail_kb,swap_used_kb,load1,temp_c,throttled"

# --- Campionamento --------------------------------------------------------
prev_app=$( [ -n "$PID_APP" ] && cpu_ticks "$PID_APP" )
prev_fl=$( [ -n "$PID_FLUID" ] && cpu_ticks "$PID_FLUID" )
start=$(date +%s)
end=$((start + DURATION))
prev_t=$start

while :; do
    sleep "$INTERVAL"
    now=$(date +%s)
    dt=$((now - prev_t)); [ "$dt" -lt 1 ] && dt=1

    a_rss=""; f_rss=""; a_cpu=""; f_cpu=""
    if [ -n "$PID_APP" ] && [ -r "/proc/$PID_APP/stat" ]; then
        a_rss=$(rss_kb "$PID_APP")
        cur=$(cpu_ticks "$PID_APP")
        a_cpu=$(awk -v c="$cur" -v p="${prev_app:-0}" -v hz="$CLK_TCK" -v dt="$dt" 'BEGIN {printf "%.1f", (c-p)/hz/dt*100}')
        prev_app=$cur
    fi
    if [ -n "$PID_FLUID" ] && [ -r "/proc/$PID_FLUID/stat" ]; then
        f_rss=$(rss_kb "$PID_FLUID")
        cur=$(cpu_ticks "$PID_FLUID")
        f_cpu=$(awk -v c="$cur" -v p="${prev_fl:-0}" -v hz="$CLK_TCK" -v dt="$dt" 'BEGIN {printf "%.1f", (c-p)/hz/dt*100}')
        prev_fl=$cur
    fi
    prev_t=$now

    emit "$LABEL,$((now - start)),$a_rss,$f_rss,$a_cpu,$f_cpu,$(mem_avail_kb),$(swap_used_kb),$(load1),$(temp_c),$(throttled)"

    [ "$now" -ge "$end" ] && break
done

if [ "$DO_JITTER" = 1 ]; then
    emit "# jitter_end_ms(mean,p99,max)=$(jitter_probe)"
fi
