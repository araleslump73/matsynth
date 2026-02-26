#!/bin/bash

#!/bin/bash
SF2_PATH="/usr/share/sounds/sf2"
STATE_FILE="/home/matteo/matsynth_web/last_state.json"

# 1. Recupera il font e pulisci le virgolette se presenti
LAST_FONT=$(jq -r '.font // "GeneralUser-GS.sf2"' "$STATE_FILE" 2>/dev/null)

# Se jq fallisce o il file è vuoto, usa un file che SAI esistere in quella cartella
if [ "$LAST_FONT" == "null" ] || [ -z "$LAST_FONT" ]; then
    LAST_FONT="GeneralUser-GS.sf2"
fi

# 2. Recupera le impostazioni hardware
AUDIO_DEVICE=$(jq -r '.audio_device // "plughw:0"' "$STATE_FILE" 2>/dev/null)
if [ "$AUDIO_DEVICE" == "null" ] || [ -z "$AUDIO_DEVICE" ]; then
    AUDIO_DEVICE="plughw:0"
fi

# 3. Recupera il gain (volume master)
GAIN=$(jq -r '.gain // "1.0"' "$STATE_FILE" 2>/dev/null)
if [ "$GAIN" == "null" ] || [ -z "$GAIN" ]; then
    GAIN="1.0"
fi

# 4. Recupera reverb level
REVERB=$(jq -r '."reverb.level" // "0.4"' "$STATE_FILE" 2>/dev/null)
if [ "$REVERB" == "null" ] || [ -z "$REVERB" ]; then
    REVERB="0.4"
fi

# 5. Recupera chorus level
CHORUS=$(jq -r '."chorus.level" // "0.4"' "$STATE_FILE" 2>/dev/null)
if [ "$CHORUS" == "null" ] || [ -z "$CHORUS" ]; then
    CHORUS="0.4"
fi

# 6. Recupera MIDI device (se specificato)
MIDI_DEVICE=$(jq -r '.midi_device // ""' "$STATE_FILE" 2>/dev/null)
if [ "$MIDI_DEVICE" == "null" ]; then
    MIDI_DEVICE=""
fi

# Pulizia preventiva
sudo killall -9 fluidsynth 2>/dev/null
sudo killall -9 python3 2>/dev/null

echo "=========================================="
echo "MatSynth Startup Configuration"
echo "=========================================="
echo "Soundfont:     $LAST_FONT"
echo "Audio Device:  $AUDIO_DEVICE"
echo "Master Gain:   $GAIN"
echo "Reverb Level:  $REVERB"
echo "Chorus Level:  $CHORUS"
if [ -n "$MIDI_DEVICE" ]; then
    echo "MIDI Device:   $MIDI_DEVICE"
else
    echo "MIDI Device:   Autoconnect"
fi
echo "=========================================="

# Avvio FluidSynth con parametri ottimizzati per Raspberry Pi Zero 2W
fluidsynth -i -s \
  -g "$GAIN" \
  -o shell.prompt="" \
  -o synth.dynamic-sample-loading=1 \
  -o synth.sample-cache-size=1 \
  -o synth.lock-memory=0 \
  -a alsa \
  -o audio.alsa.device="$AUDIO_DEVICE" \
  -o audio.period-size=256 \
  -o audio.periods=2 \
  -o synth.cpu-cores=2 \
  -o synth.polyphony=64 \
  -o synth.midi-channels=16 \
  -o midi.autoconnect=1 \
  -o synth.reverb.active=yes \
  -o synth.reverb.level="$REVERB" \
  -o synth.reverb.room-size=0.6 \
  -o synth.chorus.active=yes \
  -o synth.chorus.level="$CHORUS" \
  -o synth.chorus.nr=1 \
  -o synth.chorus.speed=0.3 \
  -o synth.chorus.depth=5.0 \
  -r 44100 \
  -z 128 \
  "$SF2_PATH/$LAST_FONT" > /dev/null 2>&1 &

# Attendi che FluidSynth si avvii
sleep 2

# Connetti il dispositivo MIDI specifico se configurato
if [ -n "$MIDI_DEVICE" ]; then
    echo "Connecting MIDI device $MIDI_DEVICE to FluidSynth..."
    # Trova il port di FluidSynth (solitamente 128:0)
    FLUID_PORT=$(aconnect -o | grep -i "FLUID Synth" | head -1 | cut -d' ' -f2 | cut -d':' -f1)
    if [ -n "$FLUID_PORT" ]; then
        aconnect "$MIDI_DEVICE:0" "$FLUID_PORT:0" 2>/dev/null
        echo "MIDI connected: $MIDI_DEVICE -> $FLUID_PORT"
    fi
fi

#web server
/usr/bin/python3 /home/matteo/matsynth_web/app.py


