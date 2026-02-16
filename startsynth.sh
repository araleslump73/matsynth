#!/bin/bash
# Script di avvio per FluidSynth con server telnet

# Percorso soundfont di default (modifica se necessario)
DEFAULT_SF2="/usr/share/sounds/sf2/GeneralUser-GS.sf2"

# Avvia FluidSynth con:
# -s         = server mode (no audio driver if not specified, uses default)
# -i         = interactive shell
# -a alsa    = audio driver ALSA
# -m alsa_seq = MIDI driver ALSA sequencer
# -g 0.7     = gain iniziale (volume)
# -o shell.port=9800 = abilita server telnet sulla porta 9800

fluidsynth \
  -s \
  -i \
  -a alsa \
  -m alsa_seq \
  -g 0.7 \
  -o shell.port=9800 \
  "$DEFAULT_SF2"
