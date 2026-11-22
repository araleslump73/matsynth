"""Genera le fixture MIDI e i preset legacy SINTETICI usati dai test e dalla
misura sul Pi. Deterministico: rieseguirlo produce byte identici.

    python tests/fixtures/make_fixtures.py

Non copia dati reali del Pi: tutti i valori sono inventati.
"""

import json
import os

import mido

HERE = os.path.dirname(os.path.abspath(__file__))
TPB = 480                      # stesso ticks_per_beat usato da export_midi
WORKLOAD_BPM = 120
NOTE_PAIRS_PER_TRACK = 500
WORKLOAD_TRACKS = 16


def _workload():
    """16 tracce (una per canale) x 500 coppie note-on/off.

    Una nota ogni 1/4 di beat, durata 1/5 di beat: 125 beat = ~62.5 s a 120 BPM,
    densita' realistica per un brano denso, ~16000 eventi totali.
    Tracce sfalsate di pitch cosi' le note non coincidono tra canali.
    """
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    step = TPB // 4
    length = TPB // 5
    for ch in range(WORKLOAD_TRACKS):
        tr = mido.MidiTrack()
        if ch == 0:
            tr.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(WORKLOAD_BPM), time=0))
            tr.append(mido.MetaMessage('time_signature', numerator=4, denominator=4, time=0))
        tr.append(mido.MetaMessage('track_name', name=f'Channel {ch + 1}', time=0))
        tr.append(mido.Message('program_change', channel=ch, program=ch, time=0))
        last = 0
        for i in range(NOTE_PAIRS_PER_TRACK):
            note = 36 + (i * 7 + ch * 3) % 48
            vel = 60 + (i * 13 + ch) % 60
            on = i * step
            off = on + length
            tr.append(mido.Message('note_on', channel=ch, note=note, velocity=vel, time=on - last))
            tr.append(mido.Message('note_off', channel=ch, note=note, velocity=0, time=length))
            last = off
        tr.append(mido.MetaMessage('end_of_track', time=0))
        mid.tracks.append(tr)
    mid.save(os.path.join(HERE, 'workload_16x500.mid'))


def _bank_program():
    """Canale 0: Bank MSB 1 / LSB 2 / Program 7 a tick 0; Program Change 12 a
    beat 4 (non zero); sustain CC64 on/off; pitch bend; poche note."""
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(100), time=0))
    tr.append(mido.MetaMessage('time_signature', numerator=4, denominator=4, time=0))
    tr.append(mido.MetaMessage('track_name', name='Channel 1', time=0))
    ch = 0
    # (beat assoluto in tick, messaggio) -> convertito in delta dopo l'ordinamento
    ev = [
        (0, mido.Message('control_change', channel=ch, control=0, value=1)),
        (0, mido.Message('control_change', channel=ch, control=32, value=2)),
        (0, mido.Message('program_change', channel=ch, program=7)),
        (0, mido.Message('note_on', channel=ch, note=60, velocity=100)),
        (TPB, mido.Message('note_off', channel=ch, note=60, velocity=0)),
        (TPB, mido.Message('control_change', channel=ch, control=64, value=127)),  # sustain on beat 1
        (TPB + TPB // 2, mido.Message('pitchwheel', channel=ch, pitch=4096)),       # beat 1.5
        (2 * TPB, mido.Message('note_on', channel=ch, note=64, velocity=90)),
        (2 * TPB, mido.Message('pitchwheel', channel=ch, pitch=0)),                 # beat 2: ritorno a centro
        (3 * TPB, mido.Message('note_off', channel=ch, note=64, velocity=0)),
        (3 * TPB, mido.Message('control_change', channel=ch, control=64, value=0)), # sustain off beat 3
        (4 * TPB, mido.Message('program_change', channel=ch, program=12)),          # PC a beat 4
        (4 * TPB, mido.Message('note_on', channel=ch, note=67, velocity=80)),
        (5 * TPB, mido.Message('note_off', channel=ch, note=67, velocity=0)),
    ]
    last = 0
    for tick, m in sorted(ev, key=lambda e: e[0]):  # sort stabile: mantiene l'ordine sopra
        tr.append(m.copy(time=tick - last))
        last = tick
    tr.append(mido.MetaMessage('end_of_track', time=0))
    mid.tracks.append(tr)
    mid.save(os.path.join(HERE, 'bank_program_cc.mid'))


def _channel(ch, bank, program, volume=100, attack=64, release=64, decay=64,
             cutoff=64, resonance=64, name='Piano', pan=None):
    """Stessa forma di api_capture_current_config (app.py ~riga 388)."""
    d = {
        'channel': ch, 'bank': bank, 'program': program,
        'attack': attack, 'release': release, 'decay': decay,
        'cutoff': cutoff, 'resonance': resonance, 'volume': volume,
        'instrument_name': name,
    }
    if pan is not None:
        d['pan'] = pan   # NOTA: oggi capture non lo produce e apply non lo legge
    return d


def _preset(name, font, channels, gain=1.0, reverb=0.4, chorus=0.4):
    """Stessa forma scritta da api_presets_save (app.py ~riga 480)."""
    return {
        'name': name,
        'created': '2025-01-01 12:00:00',
        'font': font,
        'channels': channels,
        'global_settings': {'gain': gain, 'reverb_level': reverb, 'chorus_level': chorus},
    }


def _presets():
    out = os.path.join(HERE, 'presets')
    os.makedirs(out, exist_ok=True)
    files = {
        # minimo: nessun canale, solo globali di default
        'legacy_minimal.json': _preset('Synthetic Minimal', '', []),
        # pan presente per due canali (valori != 64 per distinguerli dal default)
        'legacy_pan.json': _preset(
            'Synthetic Pan', 'Synthetic.sf2',
            [_channel(0, 0, 0, volume=110, pan=20, name='Piano L'),
             _channel(1, 0, 48, volume=90, pan=100, name='Strings R')],
            gain=1.2, reverb=0.35, chorus=0.25),
        # decay CC75 != 64 su canali diversi + banco non zero
        'legacy_decay.json': _preset(
            'Synthetic Decay', 'Synthetic.sf2',
            [_channel(0, 0, 4, decay=90, attack=10, release=70, cutoff=50, resonance=30, name='EP'),
             _channel(3, 128, 0, decay=12, volume=80, name='Drums'),
             _channel(9, 1, 7, decay=127, name='Bank1')],
            gain=0.8, reverb=0.5, chorus=0.1),
    }
    for fn, data in files.items():
        with open(os.path.join(out, fn), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, indent=2)
            f.write('\n')


if __name__ == '__main__':
    _workload()
    _bank_program()
    _presets()
    print('Fixture generate in', HERE)
