#!/bin/bash

#!/bin/bash
SF2_PATH="/usr/share/sounds/sf2"
STATE_FILE="/home/matteo/matsynth_web/last_state.json"

# 1. Recupera il font e pulisci le virgolette se presenti
LAST_FONT=$(jq -r '.font // "font.sf2"' "$STATE_FILE" 2>/dev/null)

# Se jq fallisce o il file è vuoto, usa un file che SAI esistere in quella cartella
if [ "$LAST_FONT" == "null" ] || [ -z "$LAST_FONT" ]; then
    LAST_FONT="GeneralUser-GS.sf2"
fi


# Pulizia preventiva
sudo killall -9 fluidsynth 2>/dev/null
sudo killall -9 python3 2>/dev/null

echo "Avvio FluidSynth con: $LAST_FONT"
#fluidsynth -is -a alsa -o audio.alsa.device=plughw:0 -o synth.cpu-cores=3 -o midi.autoconnect=1 -o synth.reverb.level=0.4 -o synth.reverb.room-size=0.9 -o synth.chorus.active=yes -o synth.chorus.level=0.1 -o synth.chorus.nr=2 -o synth.chorus.speed=0.4 -o synth.chorus.depth=8.0 -r 44100 -z 64 /usr/share/sounds/sf2/FluidR3_GM.sf2
fluidsynth -i -s -g 0.7 -o shell.prompt="" -o synth.dynamic-sample-loading=1 -a alsa -o audio.alsa.device=plughw:0 -o synth.cpu-cores=3 -o midi.autoconnect=1 -o synth.reverb.level=0.4 -o synth.reverb.room-size=0.9 -o synth.chorus.active=yes -o synth.chorus.level=0.4 -o synth.chorus.nr=2 -o synth.chorus.speed=0.4 -o synth.chorus.depth=8.0 -r 44100 -z 64 $SF2_PATH/$LAST_FONT > /dev/null 2>&1 &

#web server
/usr/bin/python3 /home/matteo/matsynth_web/app.py  


