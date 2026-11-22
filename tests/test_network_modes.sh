#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -f -- "$TMP_DIR/nmcli" "$TMP_DIR/sudo" "$TMP_DIR/calls"; rmdir -- "$TMP_DIR"' EXIT

cat > "$TMP_DIR/nmcli" <<'MOCK'
#!/bin/bash
printf '%s\n' "$*" >> "$CALLS_FILE"
if [[ "$1" == connection && "$2" == show ]]; then
    [[ "${MOCK_PROFILE_EXISTS:-0}" == 1 ]]
elif [[ "$1" == connection && "$2" == up && "${MOCK_FAIL_UP:-0}" == 1 ]]; then
    exit 1
fi
MOCK
cat > "$TMP_DIR/sudo" <<'MOCK'
#!/bin/bash
exec "$@"
MOCK
chmod +x "$TMP_DIR/nmcli" "$TMP_DIR/sudo"
export PATH="$TMP_DIR:$PATH" CALLS_FILE="$TMP_DIR/calls"
unset MATSYNTH_HOTSPOT_SSID MATSYNTH_HOTSPOT_PROFILE MATSYNTH_HOTSPOT_PASSWORD

export MOCK_PROFILE_EXISTS=1
bash "$ROOT/home/matteo/mode_palco.sh" >/dev/null
grep -Fx 'connection up Profilo_Palco' "$CALLS_FILE" >/dev/null

: > "$CALLS_FILE"
export MOCK_PROFILE_EXISTS=0
if bash "$ROOT/home/matteo/mode_palco.sh" </dev/null >/dev/null 2>&1; then
    echo "Hotspot senza password accettato" >&2
    exit 1
fi
if grep -F 'device wifi hotspot' "$CALLS_FILE" >/dev/null; then
    echo "Hotspot creato senza password" >&2
    exit 1
fi

: > "$CALLS_FILE"
if MATSYNTH_HOTSPOT_PASSWORD='short' bash "$ROOT/home/matteo/mode_palco.sh" >/dev/null 2>&1; then
    echo "Password hotspot troppo corta accettata" >&2
    exit 1
fi
if grep -F 'device wifi hotspot' "$CALLS_FILE" >/dev/null; then
    echo "Hotspot creato con password troppo corta" >&2
    exit 1
fi

: > "$CALLS_FILE"
MATSYNTH_HOTSPOT_PASSWORD='test-password' bash "$ROOT/home/matteo/mode_palco.sh" >/dev/null
grep -Fx 'device wifi hotspot ifname wlan0 ssid MatSynth password test-password con-name Profilo_Palco' "$CALLS_FILE" >/dev/null

: > "$CALLS_FILE"
if MOCK_PROFILE_EXISTS=1 MOCK_FAIL_UP=1 bash "$ROOT/home/matteo/mode_palco.sh" >/dev/null 2>&1; then
    echo "Errore di attivazione hotspot non segnalato" >&2
    exit 1
fi

: > "$CALLS_FILE"
export MOCK_PROFILE_EXISTS=1
bash "$ROOT/home/matteo/mode_studio.sh" 'Test Wi-Fi' >/dev/null
grep -Fx 'connection up Test Wi-Fi' "$CALLS_FILE" >/dev/null

: > "$CALLS_FILE"
export MOCK_PROFILE_EXISTS=0
bash "$ROOT/home/matteo/mode_studio.sh" 'Test Wi-Fi' >/dev/null
grep -Fx -- '--ask device wifi connect Test Wi-Fi' "$CALLS_FILE" >/dev/null

if bash "$ROOT/home/matteo/mode_studio.sh" >/dev/null 2>&1; then
    echo "Modalita Studio senza nome rete accettata" >&2
    exit 1
fi

echo "Network mode tests passed"
