#!/bin/bash
# Eseguito una sola volta all'avvio da matsynth-network.service (root).
# Attende che NetworkManager si colleghi a una rete Wi-Fi salvata (Studio);
# se entro TIMEOUT secondi non succede, attiva l'hotspot (Palco). Poi termina.
set -euo pipefail

NOME_PROFILO="${MATSYNTH_HOTSPOT_PROFILE:-Profilo_Palco}"
TIMEOUT="${MATSYNTH_WIFI_WAIT:-45}"
INTERVALLO="${MATSYNTH_WIFI_POLL:-3}"

if ! command -v nmcli >/dev/null 2>&1; then
    echo "Errore: nmcli non disponibile." >&2
    exit 1
fi

# 0 se c'e' una connessione Wi-Fi attiva diversa dall'hotspot
rete_casa_attiva() {
    local nome tipo
    while IFS=: read -r nome tipo; do
        if [[ "$tipo" == *wireless* && "$nome" != "$NOME_PROFILO" ]]; then
            return 0
        fi
    done < <(nmcli -t -f NAME,TYPE connection show --active)
    return 1
}

elapsed=0
while (( elapsed < TIMEOUT )); do
    if rete_casa_attiva; then
        echo "Connesso alla rete Wi-Fi: modalita Studio."
        exit 0
    fi
    sleep "$INTERVALLO"
    elapsed=$(( elapsed + INTERVALLO ))
done

if ! nmcli connection show "$NOME_PROFILO" >/dev/null 2>&1; then
    echo "Profilo '$NOME_PROFILO' assente: esegui una volta mode_palco.sh per crearlo." >&2
    exit 1
fi

echo "Nessuna rete Wi-Fi dopo ${TIMEOUT}s: attivo l'hotspot (Palco)."
nmcli connection up "$NOME_PROFILO"
