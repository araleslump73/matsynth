"""Test DAW della fase `ui-persistence` (blocco B): revisione, lock, event_clips
(D3), accoppiamento unico, undo, import/export, sessione.

Nessun hardware: porte finte di helpers, mido reale.
"""

import io
import json
import os
import threading
import time
import unittest

import mido

import helpers
from helpers import FIXTURES_DIR, make_daw, wait_until

import project_store as ps
from daw_recorder import DawError


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


class FakeSocket:
    """Registra gli emit e verifica che il lock di editing NON sia tenuto."""

    def __init__(self):
        self.events = []
        self.daw = None
        self.lock_held = []

    def emit(self, event, payload=None, namespace=None, **kw):
        if self.daw is not None:
            self.lock_held.append(self.daw._edit_lock._is_owned())
        self.events.append((event, payload))

    def names(self):
        return [e for e, _ in self.events]


def _daw_with_socket():
    sock = FakeSocket()
    daw, fi, fo = make_daw(socketio=sock)
    sock.daw = daw
    return daw, sock, fi


def _smf(tempos=(500000,), sig=(4, 4), extra=()):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    tr = mido.MidiTrack()
    for i, t in enumerate(tempos):
        tr.append(mido.MetaMessage('set_tempo', tempo=t, time=0 if i == 0 else 480))
    if sig:
        tr.append(mido.MetaMessage('time_signature', numerator=sig[0], denominator=sig[1], time=0))
    tr.append(mido.Message('note_on', channel=0, note=60, velocity=90, time=0))
    tr.append(mido.Message('note_off', channel=0, note=60, velocity=0, time=480))
    for m in extra:
        tr.append(m)
    mid.tracks.append(tr)
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


class InvariantTests(unittest.TestCase):
    """tracks[t] e event_clips[t] sempre della stessa lunghezza, ordinati,
    nessun ID pendente, dopo OGNI mutazione (anche quelle di ui-studio)."""

    def _ok(self, daw, step):
        self.assertEqual(daw.check_invariants(), [], step)

    def test_every_mutator_keeps_tracks_and_event_clips_parallel(self):
        daw, fi, _ = make_daw()
        steps = [
            ('import', lambda: daw.import_midi(_fixture_bytes('bank_program_cc.mid'))),
            ('import merge', lambda: daw.import_midi(_fixture_bytes('bank_program_cc.mid'), merge=True)),
            ('add_note', lambda: daw.add_note(0, 70, 100, 0.5, 1.0)),
            ('edit_event beat', lambda: daw.edit_event(0, 0, {'beat': 7.0})),
            ('delete_event', lambda: daw.delete_event(0, 1)),
            ('edit_note_pair', lambda: daw.edit_note_pair(0, *self._pair(daw, 0), {'on_beat': 9.0, 'off_beat': 9.5})),
            ('delete_note_pair', lambda: daw.delete_note_pair(0, *self._pair(daw, 0))),
            ('quantize', lambda: daw.quantize_tracks([0], 0.5)),
            ('transpose', lambda: daw.transpose_notes(0, 2)),
            ('transpose range', lambda: daw.transpose_notes(0, -1, 0.0, 2.0)),
            ('copy+paste', lambda: (daw.copy_selection(0, 0.0, 4.0), daw.paste_at(1, 8.0))),
            ('duplicate', lambda: daw.duplicate_track(0, 2)),
            ('move_clip', lambda: daw.move_clip(0, daw.get_clips(0)[0]['id'], 3.0)),
            ('resize_clip', lambda: daw.resize_clip(0, daw.get_clips(0)[0]['id'], 20.0)),
            ('split_clip', lambda: daw.split_clip(0, daw.get_clips(0)[0]['id'], 5.0)),
            ('merge_clips', lambda: daw.merge_clips(0, [c['id'] for c in daw.get_clips(0)[:2]])),
            ('transpose_clip', lambda: daw.transpose_clip(0, daw.get_clips(0)[0]['id'], 3)),
            ('edit_notes_batch', lambda: daw.edit_notes_batch(0, [dict(zip(('on_idx', 'off_idx'), self._pair(daw, 0)), on_beat=1.0)])),
            ('delete_notes_batch', lambda: daw.delete_notes_batch(0, [dict(zip(('on_idx', 'off_idx'), self._pair(daw, 0)))])),
            ('clear_range', lambda: daw.clear_range(0, 0.0, 2.0)),
            ('delete_clip', lambda: daw.delete_clip(0, daw.get_clips(0)[0]['id']) if daw.get_clips(0) else True),
            ('rename', lambda: daw.rename_track(0, 'Uno')),
            ('color', lambda: daw.set_track_color(0, 5)),
            ('undo', daw.undo), ('undo', daw.undo), ('redo', daw.redo),
            ('clear_track', lambda: daw.clear_track(1)),
            ('clear_all', daw.clear_all_tracks),
            ('undo clear_all', daw.undo),
        ]
        for name, fn in steps:
            fn()
            self._ok(daw, name)
        self.assertTrue(any(daw.tracks[ch] for ch in range(16)))
        # Record (callback del router) + stop
        daw.arm_track(3, True)
        self.assertTrue(daw.start_recording(3))
        time.sleep(0.03)
        fi.feed(mido.Message('note_on', channel=3, note=50, velocity=80))
        self.assertTrue(wait_until(lambda: len(daw.tracks[3]) == 1))
        daw.stop_recording()
        daw.stop_all()                 # lo stop del Record lascia il playback delle altre tracce
        self._ok(daw, 'record')
        daw.load_session(ps.project_to_session(self._doc(daw)))
        self._ok(daw, 'load_session')

    @staticmethod
    def _pair(daw, ch):
        p = next(p for p in daw._pair_notes(daw.tracks[ch]) if p[5] >= 0)
        return p[4], p[5]

    @staticmethod
    def _doc(daw):
        sound = {'soundfont': None, 'globals': dict(ps.DEFAULT_GLOBALS),
                 'parts': [ps.default_part(n) for n in range(16)]}
        doc = json.loads(json.dumps(ps.build_project_doc(daw.snapshot_session(), sound,
                                                         {'id': 'p-000000000001', 'name': 'T'})))
        ps.validate_project(doc)
        return doc


class RevisionAndUndoTests(unittest.TestCase):

    def test_revision_rules(self):
        daw, _, _ = make_daw()
        r0 = daw.revision
        self.assertGreater(r0, 1_600_000_000_000)
        self.assertLess(r0, 2 ** 53)
        daw.add_note(0, 60, 100, 0.0, 1.0)
        r1 = daw.revision
        self.assertEqual(r1, r0 + 1)
        self.assertEqual(daw.last_revision, r1)
        daw.set_bpm(100)
        daw.set_time_signature(3)
        daw.mute_track(0, True)
        daw.solo_track(1, True)
        daw.arm_track(2, True)
        daw.set_loop_points(0, 4)
        daw.toggle_loop()
        daw.seek_to_beat(2.0)
        self.assertEqual(daw.revision, r1)            # nessuna di queste sposta indici
        daw.undo()
        self.assertEqual(daw.revision, r1 + 1)
        self.assertEqual(daw.get_state()['revision'], daw.revision)

    def test_stale_expected_revision_is_refused_without_changes(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        stale = daw.revision - 1
        before = list(daw.tracks[0])
        undo_depth = len(daw._undo_stack)
        for call in (lambda: daw.edit_event(0, 0, {'velocity': 1}, expected_revision=stale),
                     lambda: daw.delete_event(0, 0, expected_revision=stale),
                     lambda: daw.edit_note_pair(0, 0, 1, {'note': 70}, expected_revision=stale),
                     lambda: daw.delete_note_pair(0, 0, 1, expected_revision=stale),
                     lambda: daw.add_note(0, 61, 100, 2.0, 1.0, expected_revision=stale),
                     lambda: daw.transpose_notes(0, 5, expected_revision=stale)):
            self.assertFalse(call())
            self.assertEqual(daw.last_error['code'], 'REVISION_CONFLICT')
            self.assertEqual(daw.last_error['details'],
                             {'expected_revision': stale, 'current_revision': daw.revision})
        self.assertEqual(daw.tracks[0], before)
        self.assertEqual(len(daw._undo_stack), undo_depth)
        self.assertTrue(daw.edit_event(0, 0, {'velocity': 1}, expected_revision=daw.revision))

    def test_validation_precedes_undo_a21(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        depth, rev, before = len(daw._undo_stack), daw.revision, list(daw.tracks[0])
        self.assertFalse(daw.edit_event(0, 0, {'velocity': 'abc'}))
        self.assertEqual(daw.last_error['code'], 'BAD_REQUEST')
        self.assertFalse(daw.edit_event(0, 0, {'beat': -1}))
        self.assertFalse(daw.edit_event(0, 0, {'beat': float('nan')}))
        self.assertFalse(daw.edit_note_pair(0, 0, 1, {'off_beat': 'x'}))
        self.assertFalse(daw.edit_event(0, 99, {'velocity': 3}))
        self.assertEqual(daw.last_error['code'], 'BAD_REQUEST')
        self.assertEqual((len(daw._undo_stack), daw.revision, daw.tracks[0]), (depth, rev, before))
        daw.is_recording = True
        try:
            self.assertFalse(daw.edit_event(0, 0, {'velocity': 3}))
            self.assertEqual((daw.last_error['code'], daw.last_error['http']), ('RECORDING_ACTIVE', 400))
        finally:
            daw.is_recording = False

    def test_rename_and_color_are_undoable_a14(self):
        daw, _, _ = make_daw()
        self.assertTrue(daw.rename_track(2, 'Basso'))
        self.assertTrue(daw.set_track_color(2, 9))
        self.assertEqual(len(daw._undo_stack), 2)
        self.assertTrue(daw.rename_track(2, 'Basso'))      # invariato: nessun livello
        self.assertEqual(len(daw._undo_stack), 2)
        daw.undo()
        self.assertEqual((daw.track_names[2], daw.track_colors[2]), ('Basso', 2))
        daw.undo()
        self.assertEqual(daw.track_names[2], 'Track 3')
        daw.redo()
        self.assertEqual(daw.track_names[2], 'Basso')

    def test_quantize_without_changes_pushes_no_undo(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 1.0, 1.0)
        depth = len(daw._undo_stack)
        self.assertEqual(daw.quantize_tracks([0], 0.5), {0: 0})   # gia' in griglia
        self.assertEqual(len(daw._undo_stack), depth)               # nessun livello vuoto
        daw.add_note(0, 62, 100, 2.1, 1.0)
        self.assertEqual(daw.quantize_tracks([0], 0.5), {0: 1})
        self.assertEqual(len(daw._undo_stack), depth + 2)

    def test_undo_limit_is_10(self):
        daw, _, _ = make_daw()
        for i in range(15):
            daw.add_note(0, 60, 100, float(i), 0.5)
        self.assertEqual(len(daw._undo_stack), 10)


class PairingTests(unittest.TestCase):
    """A26: un solo accoppiamento FIFO in note map, quantizza, attivita', densita'."""

    def _daw(self):
        daw, _, _ = make_daw()
        # due note C4 sovrapposte: on 0, on 1, off 2, off 3 (FIFO: [0,2] e [1,3])
        for beat, data in ((0.0, [0x90, 60, 100]), (1.0, [0x90, 60, 90]),
                           (2.0, [0x80, 60, 0]), (3.0, [0x80, 60, 0])):
            daw.tracks[0].append((beat, data))
            daw.event_clips[0].append(0)
        daw.has_data[0] = True
        daw._density_dirty = True
        return daw

    def test_note_map_fifo(self):
        notes = self._daw().get_full_note_map()['tracks'][0]['notes']
        self.assertEqual([n[2:] for n in notes], [[0.0, 2.0, 0, 2], [1.0, 3.0, 1, 3]])

    def test_quantize_uses_fifo_pairs(self):
        daw = self._daw()
        daw.quantize_tracks([0], 1.0, strength=1.0)
        notes = daw.get_full_note_map()['tracks'][0]['notes']
        # Con la vecchia sovrascrittura la prima nota perdeva il suo note-off.
        self.assertEqual(sorted((n[2], n[3]) for n in notes), [(0.0, 2.0), (1.0, 3.0)])

    def test_activity_and_density_use_fifo(self):
        daw = self._daw()
        self.assertEqual(daw.get_track_activity(0, 0.0, 10.0), [[0.0, 3.0]])
        intervals, _ = daw._build_intervals_full(daw.tracks[0])
        self.assertEqual(intervals, [(0.0, 2.0), (1.0, 3.0)])

    def test_note_map_and_density_caches_are_independent(self):
        daw = self._daw()
        daw.get_full_note_map()
        daw.add_note(1, 40, 100, 0.0, 1.0)
        daw.get_full_note_map()                      # ricalcola e non deve "pulire" la densita'
        dens = daw.get_full_density_map()
        self.assertTrue(any(dens['tracks'][1]))


class ClipOwnershipTests(unittest.TestCase):

    def test_add_note_inside_overlapping_clips_takes_most_recent(self):
        daw, _, _ = make_daw()
        daw.clips[0] = [{'id': 3, 'start_beat': 0.0, 'end_beat': 8.0, 'name': 'a', 'created_at': 0},
                        {'id': 5, 'start_beat': 4.0, 'end_beat': 12.0, 'name': 'b', 'created_at': 0}]
        daw.add_note(0, 60, 100, 5.0, 4.0)        # note-off a 9: stesso ID del note-on
        daw.add_note(0, 62, 100, 1.0, 1.0)
        daw.add_note(0, 64, 100, 20.0, 1.0)       # fuori da ogni clip: libera
        ids = dict(zip([(b, d[1], d[0] & 0xF0) for b, d in daw.tracks[0]], daw.event_clips[0]))
        self.assertEqual(ids[(5.0, 60, 0x90)], 5)
        self.assertEqual(ids[(9.0, 60, 0x80)], 5)
        self.assertEqual(ids[(1.0, 62, 0x90)], 3)
        self.assertEqual(ids[(20.0, 64, 0x90)], 0)

    def test_record_take_and_overdub_are_separate_clips(self):
        daw, fi, _ = make_daw()
        daw.add_note(0, 40, 100, 0.0, 0.5)        # evento libero preesistente
        daw.arm_track(0, True)
        for take in range(2):
            daw.timeline_position = 0.0
            self.assertTrue(daw.start_recording(0))
            time.sleep(0.03)
            n = len(daw.tracks[0])
            fi.feed(mido.Message('note_on', channel=0, note=60 + take, velocity=90))
            self.assertTrue(wait_until(lambda: len(daw.tracks[0]) == n + 1))
            rev = daw.revision
            daw.stop_recording()
            self.assertEqual(daw.revision, rev + 1)
        clips = sorted(daw.get_clips(0), key=lambda c: c['id'])     # ordine dei take
        self.assertEqual(len(clips), 2)
        by_note = {}
        for (b, d), cid in zip(daw.tracks[0], daw.event_clips[0]):
            by_note.setdefault(d[1], set()).add(cid)
        self.assertEqual(by_note[40], {0})
        self.assertEqual(by_note[60], {clips[0]['id']})       # on + off di chiusura
        self.assertEqual(by_note[61], {clips[1]['id']})
        last_take0 = max(b for (b, d), c in zip(daw.tracks[0], daw.event_clips[0]) if c == clips[0]['id'])
        self.assertAlmostEqual(clips[0]['end_beat'], last_take0 + 0.125)

    def test_delete_clip_leaves_no_dangling_ids(self):
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        cid = daw.get_clips(0)[0]['id']
        daw.resize_clip(0, cid, 1.0)              # riduzione legacy: eventi fuori cornice
        daw.delete_clip(0, cid)
        self.assertEqual(daw.check_invariants(), [])
        self.assertNotIn(cid, set(daw.event_clips[0]))


class ImportExportTests(unittest.TestCase):

    def test_one_undo_reverts_whole_import_including_tempo_a13(self):
        daw, _, _ = make_daw()
        daw.add_note(5, 60, 100, 0.0, 1.0)
        before = (list(daw.tracks[5]), daw.bpm, daw.get_clips(5))
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))           # sostituisci
        self.assertEqual(daw.bpm, 100)
        self.assertEqual(daw.tracks[5], [])
        self.assertTrue(daw.undo())
        self.assertEqual((list(daw.tracks[5]), daw.bpm, daw.get_clips(5)), before)
        self.assertEqual(daw.tracks[0], [])
        self.assertTrue(daw.redo())
        self.assertEqual(daw.bpm, 100)
        depth = len(daw._undo_stack)
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'), merge=True)   # aggiungi
        self.assertEqual(len(daw._undo_stack), depth + 1)
        self.assertEqual(len(daw.get_clips(0)), 2)
        daw.undo()
        self.assertEqual(len(daw.get_clips(0)), 1)

    def test_tempo_map_and_meter_need_conversion(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        before = list(daw.tracks[0])
        for data, issue in ((_smf(tempos=(500000, 400000)), 'tempo_map'), (_smf(sig=(6, 8)), 'meter')):
            with self.assertRaises(DawError) as ctx:
                daw.import_midi(data)
            self.assertEqual(ctx.exception.code, 'IMPORT_NEEDS_CONVERSION')
            self.assertEqual(ctx.exception.details['issues'][0]['type'], issue)
            self.assertEqual(daw.tracks[0], before)       # nessuna mutazione
        info = daw.import_midi(_smf(tempos=(500000, 400000)), convert=True)
        self.assertEqual(info['bpm'], 120)                # primo tempo
        self.assertIn('IMPORT_CONVERTED', [w['code'] for w in info['warnings']])
        info = daw.import_midi(_smf(sig=(6, 8)), convert=True)
        self.assertEqual(daw.beats_per_measure, 4)
        self.assertNotIsInstance(DawError('X', 'y'), RuntimeError)

    def test_sysex_is_dropped_and_counted(self):
        daw, _, _ = make_daw()
        info = daw.import_midi(_smf(extra=[mido.Message('sysex', data=[1, 2, 3], time=0)]))
        w = [w for w in info['warnings'] if w['code'] == 'IMPORT_DROPPED_EVENTS']
        self.assertEqual(w[0]['details']['counts'], {'sysex': 1})

    def test_invalid_smf_is_validation_failed(self):
        daw, _, _ = make_daw()
        with self.assertRaises(DawError) as ctx:
            daw.import_midi(b'MThd garbage garbage')
        self.assertEqual(ctx.exception.code, 'VALIDATION_FAILED')

    def test_pc_and_bank_across_tracks_by_absolute_tick(self):
        # Type 1: CC0/CC32/PC su una traccia, note su un'altra.
        mid = mido.MidiFile(type=1, ticks_per_beat=480)
        a, b = mido.MidiTrack(), mido.MidiTrack()
        a.append(mido.Message('control_change', channel=2, control=0, value=3, time=0))
        a.append(mido.Message('control_change', channel=2, control=32, value=4, time=0))
        a.append(mido.Message('program_change', channel=2, program=9, time=0))
        a.append(mido.Message('program_change', channel=2, program=11, time=960))
        b.append(mido.Message('note_on', channel=2, note=60, velocity=90, time=0))
        b.append(mido.Message('note_off', channel=2, note=60, velocity=0, time=480))
        mid.tracks.extend([a, b])
        buf = io.BytesIO()
        mid.save(file=buf)
        daw, _, _ = make_daw()
        info = daw.import_midi(buf.getvalue())
        self.assertEqual(info['channel_programs'], {2: {'bank': (3 << 7) | 4, 'program': 9}})
        self.assertIn((2.0, [0xC2, 11]), daw.tracks[2])

    def test_export_names_and_null_bank_program(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        daw.add_note(1, 62, 100, 0.0, 1.0)
        daw.rename_track(0, 'Piano')
        mid = mido.MidiFile(file=io.BytesIO(daw.export_midi({0: {'bank': None, 'program': 7},
                                                             1: {'bank': 130, 'program': None}})))
        t0 = list(mid.tracks[0])
        self.assertIn('Piano', [m.name for m in t0 if m.type == 'track_name'])
        self.assertFalse([m for m in t0 if m.type == 'control_change' and m.control in (0, 32)])
        self.assertEqual([m.program for m in t0 if m.type == 'program_change'], [7])
        t1 = [m for m in mid.tracks[1] if not m.is_meta]
        self.assertEqual([(m.control, m.value) for m in t1[:2]], [(0, 1), (32, 2)])
        self.assertFalse([m for m in t1 if m.type == 'program_change'])


class SessionTests(unittest.TestCase):

    def _sound(self):
        return {'soundfont': {'file': 'GeneralUser-GS.sf2'}, 'globals': dict(ps.DEFAULT_GLOBALS),
                'parts': [ps.default_part(n) for n in range(16)]}

    def test_workload_round_trip_through_project_document(self):
        daw, _, _ = make_daw()
        daw.import_midi(_fixture_bytes('workload_16x500.mid'))
        daw.add_note(3, 100, 100, 3000.0, 1.0)    # evento libero fuori dai clip
        daw.rename_track(1, 'Archi')
        daw.set_track_color(1, 7)
        daw.mute_track(2, True)
        daw.solo_track(4, True)
        daw.set_bpm(97.5)
        daw.set_time_signature(3)
        daw.set_loop_points(4.0, 12.5)
        daw.toggle_loop()
        snap = daw.snapshot_session()
        doc = ps.build_project_doc(snap, self._sound(), {'id': 'p-0123456789ab', 'name': 'W'})
        text = json.dumps(doc, separators=(',', ':'))
        self.assertLess(len(text.encode()), ps.MAX_PROJECT_BYTES)
        doc2 = json.loads(text)
        self.assertEqual(ps.validate_project(doc2), [])
        daw2, _, _ = make_daw()
        daw2.arm_track(0, True)
        daw2.add_note(0, 1, 1, 0.0, 1.0)
        rev = daw2.revision
        daw2.load_session(ps.project_to_session(doc2))
        for ch in range(16):
            self.assertEqual(daw2.tracks[ch], daw.tracks[ch], ch)
            self.assertEqual(list(daw2.event_clips[ch]), list(daw.event_clips[ch]), ch)
            self.assertEqual(daw2.get_clips(ch), daw.get_clips(ch), ch)
        for attr in ('track_names', 'track_colors', 'muted', 'solo', 'bpm', 'beats_per_measure',
                     'loop_enabled', 'loop_start', 'loop_end', '_next_clip_id'):
            self.assertEqual(getattr(daw2, attr), getattr(daw, attr), attr)
        self.assertNotEqual(daw2.revision, rev)
        self.assertFalse(daw2.get_state()['can_undo'])
        self.assertFalse(any(daw2.armed.values()))
        self.assertFalse(daw2.is_playing or daw2.is_recording)
        self.assertEqual(daw2.timeline_position, 0.0)
        self.assertEqual(daw2.check_invariants(), [])

    def test_load_session_refused_while_playing(self):
        daw, _, _ = make_daw()
        daw.is_playing = True
        try:
            with self.assertRaises(DawError) as ctx:
                daw.load_session(daw.empty_session())
            self.assertEqual(ctx.exception.code, 'TRANSPORT_ACTIVE')
        finally:
            daw.is_playing = False


class LockAndEmitTests(unittest.TestCase):

    def test_bpm_and_meter_emit_state_update_a20(self):
        daw, sock, _ = _daw_with_socket()
        sock.events.clear()
        daw.set_bpm(90)
        daw.set_time_signature(3)
        upd = [p for e, p in sock.events if e == 'daw_state_update']
        self.assertEqual([(p['bpm'], p['beats_per_measure']) for p in upd], [(90, 4), (90, 3)])

    def test_one_emit_per_mutation_outside_the_lock(self):
        daw, sock, _ = _daw_with_socket()
        hook_lock = []
        daw.on_content_change = lambda: hook_lock.append(daw._edit_lock._is_owned())
        sock.events.clear()
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        daw.add_note(0, 60, 100, 0.0, 1.0)
        daw.transpose_clip(0, daw.get_clips(0)[0]['id'], 2)
        self.assertEqual(sock.names().count('daw_state_update'), 3)
        self.assertEqual(sock.lock_held, [False] * len(sock.lock_held))
        self.assertTrue(hook_lock)
        self.assertNotIn(True, hook_lock)
        self.assertEqual(sock.events[-1][1]['revision'], daw.revision)

    def test_concurrent_edits_with_same_expected_revision_a29(self):
        daw, _, _ = make_daw()
        for i in range(50):
            daw.add_note(0, 60, 100, float(i), 0.5)
        rev = daw.revision
        barrier = threading.Barrier(2)
        results = []

        def worker(vel):
            barrier.wait()
            ok = daw.edit_event(0, 0, {'velocity': vel}, expected_revision=rev)
            results.append((ok, None if ok else daw.last_error['code']))

        threads = [threading.Thread(target=worker, args=(v,)) for v in (10, 20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(r[0] for r in results), [False, True])
        self.assertEqual([r[1] for r in results if not r[0]], ['REVISION_CONFLICT'])
        self.assertEqual(daw.revision, rev + 1)


if __name__ == '__main__':
    unittest.main()
