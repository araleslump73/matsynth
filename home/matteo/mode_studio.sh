#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
    echo "Uso: $0 <nome rete o profilo Wi-Fi>" >&2
    exit 2
fi
NOME_RETE_CASA="$1"

if ! command -v nmcli >/dev/null 2>&1; then
    echo "Errore: nmcli non disponibile." >&2
    exit 1
fi

if nmcli connection show "$NOME_RETE_CASA" >/dev/null 2>&1; then
    sudo nmcli connection up "$NOME_RETE_CASA"
else
    sudo nmcli --ask device wifi connect "$NOME_RETE_CASA"
fi

echo "Modalita Studio attivata. La connessione SSH potrebbe interrompersi."
