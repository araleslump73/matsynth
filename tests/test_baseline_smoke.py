"""Characterization test della baseline `ui-baseline`.

Fotografano il comportamento ATTUALE di daw_recorder.py e app.py. Non sono
specifiche: dove il comportamento e' anomalo rispetto al piano (plan.md sez. 12)
il test attuale descrive lo stato di fatto e un test `expectedFailure`
descrive il comportamento desiderato. Quando una fase successiva corregge
l'anomalia, l'expectedFailure diventa "unexpected success": va rimosso il
decoratore e aggiornato il test caratterizzante.

Nessun hardware, nessun rtmidi, nessun FluidSynth: vedi tests/helpers.py.
"""

import io
import json
import os
import shutil
import tempfile
import unittest

import mido

import helpers
from helpers import FIXTURES_DIR, make_daw, wait_until


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


class DawInstantiationTests(unittest.TestCase):

    def test_instantiate_without_ports(self):
        daw, fi, fo = make_daw(with_input=False, with_output=False)
        self.assertIsNone(daw.midi_input)
        self.assertIsNone(daw.midi_output)
        self.assertEqual(daw.bpm, 120)
        self.assertEqual(daw.beats_per_measure, 4)
        self.assertEqual(sorted(daw.armed), list(range(16)))
        self.assertFalse(any(daw.has_data.values()))
        st = daw.get_state()
        self.assertFalse(st['is_recording'])
        self.assertFalse(st['is_playing'])
        self.assertEqual(st['track_names'][0], 'Track 1')

    def test_ports_opened_through_real_selection_logic(self):
        daw, fi, fo = make_daw()
        self.assertIs(daw.midi_input, fi)
        self.assertIs(daw.midi_output, fo)

    def test_input_selection_keyword_priority_and_midi_through(self):
        # Priorita' FANTOM > SINCO > USB, 'Midi Through' escluso.
        daw, fi, _ = make_daw(input_names=['Midi Through Port-0', 'USB Generic', 'FANTOM-08 MIDI'])
        self.assertEqual(fi.name, 'FANTOM-08 MIDI')

    def test_input_selection_ambiguous_takes_first_no_error(self):
        # Comportamento ATTUALE: con due porte equivalenti vince la prima,
        # senza errore. Il piano (sez. 12) vuole errore/stato non disponibile.
        daw, fi, _ = make_daw(input_names=['USB Keyboard A', 'USB Keyboard B'])
        self.assertEqual(fi.name, 'USB Keyboard A')

    def test_input_not_found_leaves_none(self):
        daw, fi, fo = make_daw(input_names=['Midi Through Port-0'])
        self.assertIsNone(daw.midi_input)
        self.assertIsNotNone(daw.midi_output)

    def test_output_selection_fluid_or_128_0(self):
        daw, _, fo = make_daw(output_names=['Other Synth', 'Midi Through:Midi Through Port-0 14:0',
                                            'FLUID Synth (123):Synth input port 128:0'])
        self.assertTrue(fo.name.startswith('FLUID'))
        daw, _, fo = make_daw(output_names=['Something 128:0'])
        self.assertEqual(fo.name, 'Something 128:0')


class RecordingTests(unittest.TestCase):

    def _record(self, daw, fi, messages, channel=0, settle=0.03):
        daw.arm_track(channel, True)
        self.assertTrue(daw.start_recording(channel))
        # start_recording dorme 50 ms, poi il loop ignora i primi 10 ms.
        import time
        time.sleep(settle)
        fi.feed(*messages)
        n = len([m for m in messages if hasattr(m, 'channel') and m.channel == channel])
        self.assertTrue(wait_until(lambda: len(daw.tracks[channel]) >= n))
        self.assertTrue(daw.stop_recording())

    def test_record_note_pair_creates_events_and_clip(self):
        daw, fi, fo = make_daw()
        on = mido.Message('note_on', channel=0, note=60, velocity=90)
        off = mido.Message('note_off', channel=0, note=60, velocity=0)
        self._record(daw, fi, [on, off])
        ev = daw.tracks[0]
        self.assertEqual([b for _, b in ev], [on.bytes(), off.bytes()])
        self.assertLessEqual(ev[0][0], ev[1][0])
        self.assertGreater(ev[0][0], 0.0)           # beat > 0: tempo reale trascorso
        self.assertTrue(daw.has_data[0])
        clips = daw.get_clips(0)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]['id'], 1)
        self.assertAlmostEqual(clips[0]['start_beat'], ev[0][0])
        self.assertFalse(daw.is_recording)

    def test_record_filters_unarmed_channels_and_non_channel_messages(self):
        daw, fi, fo = make_daw()
        daw.arm_track(0, True)
        self.assertTrue(daw.start_recording(0))
        import time
        time.sleep(0.03)
        fi.feed(mido.Message('note_on', channel=5, note=60, velocity=90),   # canale non in registrazione
                mido.Message('sysex', data=[1, 2, 3]),                      # senza .channel
                mido.Message('note_on', channel=0, note=62, velocity=90))
        self.assertTrue(wait_until(lambda: len(daw.tracks[0]) == 1))
        time.sleep(0.02)
        daw.stop_recording()
        self.assertEqual(len(daw.tracks[0]), 1)
        self.assertEqual(len(daw.tracks[5]), 0)

    def test_record_does_not_thru_to_output(self):
        # Il monitoring live NON passa dalla DAW (aconnect/ALSA diretto): oggi
        # nessun invio live a midi_output durante la registrazione, a parte
        # l'All Notes Off di sicurezza dell'avvio (CC123 x16).
        daw, fi, fo = make_daw()
        self._record(daw, fi, [mido.Message('note_on', channel=0, note=60, velocity=90),
                               mido.Message('note_off', channel=0, note=60, velocity=0)])
        self.assertEqual(fo.notes(), [])
        self.assertTrue(all(m.type == 'control_change' and m.control == 123 for m in fo.sent))

    def test_record_requires_armed_track(self):
        daw, fi, fo = make_daw()
        self.assertFalse(daw.start_recording())
        self.assertFalse(daw.is_recording)

    def test_record_without_input_port_sets_is_recording_but_records_nothing(self):
        # Anomalia ATTUALE (da non correggere qui): senza porta di input
        # start_recording ritorna True e is_recording=True, ma il thread esce
        # subito. Il piano (sez. 12) vuole "nessuno stato recording fittizio".
        daw, _, _ = make_daw(with_input=False)
        daw.arm_track(0, True)
        self.assertTrue(daw.start_recording(0))
        self.assertTrue(daw.is_recording)
        self.assertTrue(wait_until(lambda: not daw.record_thread.is_alive()))
        self.assertTrue(daw.is_recording)           # resta "fittizio" fino a stop esplicito
        daw.stop_recording()
        self.assertEqual(len(daw.tracks[0]), 0)

    @unittest.expectedFailure
    def test_DESIRED_record_without_input_port_is_refused(self):
        daw, _, _ = make_daw(with_input=False)
        daw.arm_track(0, True)
        self.assertFalse(daw.start_recording(0))


class PlaybackAndEditTests(unittest.TestCase):

    def test_add_note_undo_redo(self):
        daw, _, _ = make_daw()
        self.assertTrue(daw.add_note(0, 60, 100, 1.0, 0.5))
        self.assertEqual(len(daw.tracks[0]), 2)
        self.assertEqual(daw.tracks[0][0][0], 1.0)
        self.assertEqual(daw.tracks[0][1][0], 1.5)
        self.assertTrue(daw.get_state()['can_undo'])
        self.assertTrue(daw.undo())
        self.assertEqual(len(daw.tracks[0]), 0)
        self.assertTrue(daw.redo())
        self.assertEqual(len(daw.tracks[0]), 2)

    def test_playback_sends_note_pair_on_track_channel_then_all_notes_off(self):
        daw, _, fo = make_daw()
        daw.set_bpm(300)                            # beat = 0.2 s: test breve
        daw.add_note(3, 64, 100, 0.0, 0.25)
        self.assertTrue(daw.start_playback())
        self.assertTrue(wait_until(lambda: not daw.is_playing, timeout=3))
        notes = fo.notes()
        self.assertEqual([(m.type, m.channel, m.note) for m in notes],
                         [('note_on', 3, 64), ('note_off', 3, 64)])
        cc123 = [m for m in fo.sent if m.type == 'control_change' and m.control == 123]
        self.assertGreaterEqual(len(cc123), 16)     # almeno un giro di All Notes Off

    def test_mute_excludes_track_from_playback(self):
        daw, _, fo = make_daw()
        daw.set_bpm(300)
        daw.add_note(0, 60, 100, 0.0, 0.25)
        daw.add_note(1, 62, 100, 0.0, 0.25)
        daw.mute_track(0, True)
        self.assertTrue(daw.start_playback())
        self.assertTrue(wait_until(lambda: not daw.is_playing, timeout=3))
        self.assertEqual({m.channel for m in fo.notes()}, {1})

    def test_panic_sends_cc123_on_all_16_channels(self):
        daw, _, fo = make_daw()
        daw.panic()
        self.assertEqual([m.channel for m in fo.sent if m.control == 123], list(range(16)))

    def test_playback_without_output_port_exits_leaving_is_playing_true(self):
        # Come per il record: stato "fittizio" ATTUALE (documentato, non corretto).
        daw, _, _ = make_daw(with_output=False)
        daw.add_note(0, 60, 100, 0.0, 0.25)
        self.assertTrue(daw.start_playback())
        self.assertTrue(wait_until(lambda: not daw.playback_thread.is_alive()))
        self.assertTrue(daw.is_playing)
        daw.stop_playback()


class SmfRoundTripTests(unittest.TestCase):

    def test_workload_fixture_shape(self):
        mid = mido.MidiFile(file=io.BytesIO(_fixture_bytes('workload_16x500.mid')))
        self.assertEqual(len(mid.tracks), 16)
        for ch, tr in enumerate(mid.tracks):
            ons = [m for m in tr if m.type == 'note_on' and m.velocity > 0]
            offs = [m for m in tr if m.type == 'note_off']
            self.assertEqual((len(ons), len(offs)), (500, 500), f'canale {ch}')

    def test_workload_import_export_import_is_stable(self):
        daw, _, _ = make_daw()
        info = daw.import_midi(_fixture_bytes('workload_16x500.mid'))
        self.assertEqual(info['channels'], list(range(16)))
        self.assertEqual(info['total_events'], 16 * 1000)   # i Program Change non finiscono nelle tracce
        self.assertEqual(info['bpm'], 120)
        before = {ch: list(daw.tracks[ch]) for ch in range(16)}
        exported = daw.export_midi(channel_programs=info['channel_programs'])
        daw2, _, _ = make_daw()
        info2 = daw2.import_midi(exported)
        self.assertEqual(info2['total_events'], 16000)
        for ch in range(16):
            self.assertEqual(list(daw2.tracks[ch]), before[ch], f'canale {ch}')
        self.assertEqual(info2['channel_programs'], info['channel_programs'])

    def test_export_structure(self):
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('workload_16x500.mid'))
        mid = mido.MidiFile(file=io.BytesIO(daw.export_midi()))
        self.assertEqual(mid.type, 1)
        self.assertEqual(mid.ticks_per_beat, 480)
        self.assertEqual(len(mid.tracks), 16)
        first = [m for m in mid.tracks[0] if m.is_meta]
        self.assertTrue(any(m.type == 'set_tempo' for m in first))
        # Bank MSB/LSB + Program Change a tick 0 su ogni traccia
        head = [m.type for m in mid.tracks[1] if not m.is_meta][:3]
        self.assertEqual(head, ['control_change', 'control_change', 'program_change'])

    def test_import_does_not_create_clips(self):
        # Comportamento ATTUALE: l'import non crea clip (solo la registrazione lo fa).
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        self.assertEqual(daw.get_clips(0), [])

    def test_import_refused_while_playing_or_recording(self):
        daw, _, _ = make_daw()
        daw.is_playing = True
        with self.assertRaises(RuntimeError):
            daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        daw.is_playing = False

    def test_sustain_and_pitchbend_survive_round_trip(self):
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        types = [mido.Message.from_bytes(b).type for _, b in daw.tracks[0]]
        self.assertIn('pitchwheel', types)
        self.assertEqual(sum(1 for _, b in daw.tracks[0] if b[:2] == [0xB0, 64]), 2)
        before = list(daw.tracks[0])
        daw2, _, _ = make_daw()
        daw2.import_midi(daw.export_midi({0: {'bank': 130, 'program': 7}}))
        self.assertEqual(list(daw2.tracks[0]), before)
        self.assertEqual(daw2.bpm, 100)

    def test_export_writes_bank_msb1_lsb2_program7_from_channel_programs(self):
        # Lato export: bank=(1<<7)|2 -> CC0=1, CC32=2, PC 7 a tick 0 (corretto oggi).
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        mid = mido.MidiFile(file=io.BytesIO(daw.export_midi({0: {'bank': (1 << 7) | 2, 'program': 7}})))
        msgs = [m for m in mid.tracks[0] if not m.is_meta]
        self.assertEqual((msgs[0].control, msgs[0].value), (0, 1))
        self.assertEqual((msgs[1].control, msgs[1].value), (32, 2))
        self.assertEqual((msgs[2].type, msgs[2].program), ('program_change', 7))

    # --- Anomalie ATTUALI in import (da non correggere in questa fase) ---

    def test_import_bank_lsb_is_dropped_and_last_program_change_wins(self):
        # Fixture: CC0=1, CC32=2, PC 7 a beat 0; PC 12 a beat 4.
        # ATTUALE: bank = solo MSB (1), LSB ignorato; il programma finale e' 12
        # (l'ultimo PC sovrascrive); nessun Program Change resta nelle tracce.
        daw, _, _ = make_daw()
        info = daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        self.assertEqual(info['channel_programs'], {0: {'bank': 1, 'program': 12}})
        self.assertFalse([b for _, b in daw.tracks[0] if b[0] & 0xF0 == 0xC0])

    @unittest.expectedFailure
    def test_DESIRED_import_keeps_bank_msb1_lsb2_program7(self):
        daw, _, _ = make_daw()
        info = daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        self.assertEqual(info['channel_programs'][0], {'bank': (1 << 7) | 2, 'program': 7})

    @unittest.expectedFailure
    def test_DESIRED_program_change_at_nonzero_beat_is_kept_at_that_beat(self):
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        self.assertIn((4.0, [0xC0, 12]), [(b, m) for b, m in daw.tracks[0]])


class LegacyPresetTests(unittest.TestCase):
    """Preset legacy letti/applicati attraverso le route reali di app.py.

    app.py viene importato con rete/disco neutralizzati (helpers.import_app) e
    send_fluid sostituito da un registratore: i comandi FluidSynth sono quelli
    che il codice reale avrebbe inviato alla console 9800.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = helpers.import_app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='matsynth_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.commands = helpers.redirect_app_paths(self.app, self.tmp)
        src = os.path.join(FIXTURES_DIR, 'presets')
        for fn in os.listdir(src):
            shutil.copy(os.path.join(src, fn), self.app.PRESETS_DIR)
        self.client = self.app.app.test_client()

    def _apply(self, filename):
        data = self.client.get(f'/api/presets/load/{filename}').get_json()
        self.assertEqual(data['status'], 'ok')
        del self.commands[:]
        r = self.client.post('/api/presets/apply', json={'preset': data['data']})
        self.assertEqual(r.get_json()['status'], 'ok')
        return list(self.commands)

    def test_list_sorted_by_name_case_insensitive(self):
        r = self.client.get('/api/presets/list').get_json()
        self.assertEqual([p['name'] for p in r],
                         ['Synthetic Decay', 'Synthetic Minimal', 'Synthetic Pan'])
        self.assertEqual({p['filename'] for p in r},
                         {'legacy_decay.json', 'legacy_minimal.json', 'legacy_pan.json'})

    def test_load_returns_file_content(self):
        r = self.client.get('/api/presets/load/legacy_decay.json').get_json()
        with open(os.path.join(FIXTURES_DIR, 'presets', 'legacy_decay.json')) as f:
            self.assertEqual(r['data'], json.load(f))

    def test_load_missing_is_404(self):
        self.assertEqual(self.client.get('/api/presets/load/nope.json').status_code, 404)

    def test_apply_decay_cc75_bank_program_and_globals(self):
        cmds = self._apply('legacy_decay.json')
        self.assertIn('set synth.gain 0.8', cmds)
        self.assertIn('set synth.reverb.level 0.5', cmds)
        self.assertIn('set synth.chorus.level 0.1', cmds)
        self.assertIn('select 0 1 0 4', cmds)
        self.assertIn('select 3 1 128 0', cmds)
        self.assertIn('select 9 1 1 7', cmds)
        self.assertIn('cc 0 75 90', cmds)
        self.assertIn('cc 3 75 12', cmds)
        self.assertIn('cc 9 75 127', cmds)
        self.assertIn('cc 0 73 10', cmds)   # attack
        self.assertIn('cc 0 72 70', cmds)   # release
        self.assertIn('cc 0 74 50', cmds)   # cutoff
        self.assertIn('cc 0 71 30', cmds)   # resonance

    def test_apply_does_not_load_soundfont(self):
        cmds = self._apply('legacy_pan.json')
        self.assertFalse([c for c in cmds if c.startswith(('load', 'unload'))])

    def test_apply_pan_is_ignored_today(self):
        # ATTUALE: api_presets_apply non legge 'pan' (e api_capture_current_config
        # non lo produce): il CC10 del preset non viene mai inviato.
        cmds = self._apply('legacy_pan.json')
        self.assertIn('cc 0 7 110', cmds)
        self.assertFalse([c for c in cmds if c.startswith('cc 0 10 ') or c.startswith('cc 1 10 ')])

    @unittest.expectedFailure
    def test_DESIRED_apply_pan_sends_cc10(self):
        cmds = self._apply('legacy_pan.json')
        self.assertIn('cc 0 10 20', cmds)
        self.assertIn('cc 1 10 100', cmds)

    def test_apply_minimal_uses_defaults_and_no_channel_commands(self):
        cmds = self._apply('legacy_minimal.json')
        self.assertEqual(cmds, ['set synth.gain 1.0', 'set synth.reverb.level 0.4',
                                'set synth.chorus.level 0.4'])

    def test_apply_persists_globals_but_not_channels_in_state_file(self):
        self._apply('legacy_decay.json')
        with open(self.app.STATE_FILE) as f:
            st = json.load(f)
        self.assertEqual((st['gain'], st['reverb.level'], st['chorus.level']), (0.8, 0.5, 0.1))
        self.assertNotIn('channels', st)    # i parametri canale applicati non vengono persistiti

    def test_apply_missing_preset_is_400(self):
        r = self.client.post('/api/presets/apply', json={})
        self.assertEqual(r.status_code, 400)

    def test_save_produces_legacy_shape_and_dedupes_name(self):
        body = {'name': 'My Scene', 'font': 'Synthetic.sf2', 'gain': 0.9,
                'reverb_level': 0.3, 'chorus_level': 0.2,
                'channels': [{'channel': 0, 'bank': 0, 'program': 1, 'decay': 77}]}
        r1 = self.client.post('/api/presets/save', json=body).get_json()
        r2 = self.client.post('/api/presets/save', json=body).get_json()
        self.assertEqual(r1['filename'], 'My_Scene.json')
        self.assertEqual(r2['filename'], 'My_Scene_2.json')
        self.assertEqual(r2['saved_name'], 'My Scene (2)')
        with open(os.path.join(self.app.PRESETS_DIR, 'My_Scene.json')) as f:
            saved = json.load(f)
        self.assertEqual(set(saved), {'name', 'created', 'font', 'channels', 'global_settings'})
        self.assertEqual(saved['global_settings'], {'gain': 0.9, 'reverb_level': 0.3, 'chorus_level': 0.2})

    def test_rename_and_delete(self):
        r = self.client.post('/api/presets/rename',
                             json={'old_filename': 'legacy_minimal.json', 'new_name': 'Renamed One'}).get_json()
        self.assertEqual(r['new_filename'], 'Renamed_One.json')
        self.assertFalse(os.path.exists(os.path.join(self.app.PRESETS_DIR, 'legacy_minimal.json')))
        d = self.client.delete('/api/presets/delete/Renamed_One.json').get_json()
        self.assertEqual(d['status'], 'ok')

    def test_midi_upload_applies_bank_msb_only_to_synth(self):
        # Route-level della stessa anomalia dell'import (bank LSB perso).
        import io as _io
        data = {'file': (_io.BytesIO(_fixture_bytes('bank_program_cc.mid')), 'bank.mid')}
        r = self.client.post('/api/daw/midi/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(r.get_json()['status'], 'ok')
        self.assertIn('select 0 1 1 12', self.commands)
        self.assertTrue(os.path.exists(os.path.join(self.app.MIDI_DIR, 'bank.mid')))


if __name__ == '__main__':
    unittest.main()
