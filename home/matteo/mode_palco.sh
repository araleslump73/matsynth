#!/bin/bash
set -euo pipefail

NOME_HOTSPOT="${MATSYNTH_HOTSPOT_SSID:-MatSynth}"
NOME_PROFILO="${MATSYNTH_HOTSPOT_PROFILE:-Profilo_Palco}"

if ! command -v nmcli >/dev/null 2>&1; then
    echo "Errore: nmcli non disponibile." >&2
    exit 1
fi

if nmcli connection show "$NOME_PROFILO" >/dev/null 2>&1; then
    sudo nmcli connection up "$NOME_PROFILO"
else
    if [[ -z "${MATSYNTH_HOTSPOT_PASSWORD:-}" ]]; then
        if [[ ! -t 0 ]]; then
            echo "Imposta MATSYNTH_HOTSPOT_PASSWORD o usa un terminale interattivo." >&2
            exit 1
        fi
        read -r -s -p "Password hotspot: " MATSYNTH_HOTSPOT_PASSWORD
        echo
    fi
    if [[ ${#MATSYNTH_HOTSPOT_PASSWORD} -lt 8 ]]; then
        echo "La password hotspot deve avere almeno 8 caratteri." >&2
        exit 1
    fi
    sudo nmcli device wifi hotspot ifname wlan0 ssid "$NOME_HOTSPOT" \
        password "$MATSYNTH_HOTSPOT_PASSWORD" con-name "$NOME_PROFILO"
    unset MATSYNTH_HOTSPOT_PASSWORD
fi

echo "Modalita Palco attivata. La connessione SSH potrebbe interrompersi."
