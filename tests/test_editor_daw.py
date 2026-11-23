"""Test DAW della fase `ui-editor`: note per traccia, copia/taglia di note
scelte, eventi batch, cornice dei clip (D28), accoppiamento FIFO preservato
(D29), limite MAX_EVENTS_TOTAL su add_note/paste (D30)."""

import unittest

import helpers  # noqa: F401  (percorsi di import)
import project_store as ps
from test_studio_daw import _Base, by_id, cc, clip, load, off, on


class TrackNotesTests(_Base):

    def test_notes_carry_clip_id_and_revision(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1), (on(5, 62), 0), (off(6, 62), 0)],
             clips=[clip(1, 0, 4)])
        data, rev = self.daw.get_track_notes(0)
        self.assertEqual(rev, self.daw.revision)
        self.assertEqual([n[0] for n in data['notes']], [60, 62])
        self.assertEqual([n[6] for n in data['notes']], [1, 0])
        self.assertEqual(data['notes'][0][4:6], [0, 1])
        self.assertEqual([c['id'] for c in data['clips']], [1])


class NotesBatchEditorTests(_Base):

    def test_move_out_of_clip_grows_frame_in_one_undo(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1)], clips=[clip(1, 0, 4)])
        rev = self.daw.revision
        res = self.daw.edit_notes_batch(0, [{'on_idx': 0, 'off_idx': 1, 'on_beat': 6.0, 'off_beat': 7.0}],
                                        expected_revision=rev)
        self.assertEqual(res, {'modified': 1})
        c = self.daw.get_clips(0)[0]
        self.assertEqual((c['start_beat'], c['end_beat']), (0.0, 6.0 + ps.CLIP_TAIL_BEATS))
        self.assertEqual(by_id(self.daw, 0), {1: [(6.0, 0x90, 60), (7.0, 0x80, 60)]})
        self.assertEqual(self.daw.revision, rev + 1)
        self.ok()
        self.daw.undo()
        c = self.daw.get_clips(0)[0]
        self.assertEqual((c['start_beat'], c['end_beat']), (0.0, 4.0))

    def test_overlap_same_pitch_refused_without_changes(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0), (on(2, 60), 0), (off(3, 60), 0)])
        before = list(self.daw.tracks[0])
        rev = self.daw.revision
        # Sposta la seconda dentro la prima: FIFO accoppierebbe on(0) con off(0.8).
        res = self.daw.edit_notes_batch(0, [{'on_idx': 2, 'off_idx': 3, 'on_beat': 0.2, 'off_beat': 0.8}],
                                        expected_revision=rev)
        self.assertIsNone(res)
        self.assertEqual((self.code(), self.daw.last_error['details']['reason']),
                         ('VALIDATION_FAILED', 'pairing'))
        self.assertEqual(self.daw.tracks[0], before)
        self.assertEqual(self.daw.revision, rev)
        # Trasporre la seconda su un'altezza libera resta consentito.
        res = self.daw.edit_notes_batch(0, [{'on_idx': 2, 'off_idx': 3, 'note': 64}], expected_revision=rev)
        self.assertEqual(res, {'modified': 1})
        self.ok()


class CopyNotesTests(_Base):

    def _track(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1), (on(2, 62), 1), (off(3, 62), 1),
                           (cc(2.5, 7, 90), 1)], clips=[clip(1, 0, 4)])

    def test_copy_pairs_then_paste_keeps_relative_positions(self):
        self._track()
        rev = self.daw.revision
        res = self.daw.copy_notes(0, [{'on_idx': 2, 'off_idx': 4}], expected_revision=rev)
        self.assertEqual((res['copied'], res['removed']), (2, 0))
        self.assertEqual(res['clipboard']['kind'], 'range')
        self.assertEqual(self.daw.revision, rev)
        self.assertIsNotNone(self.daw.paste_at(0, 8.0, expected_revision=rev))
        notes = [(n[0], n[2], n[3]) for n in self.daw.get_track_notes(0)[0]['notes']]
        self.assertIn((62, 8.0, 9.0), notes)
        self.ok()

    def test_cut_pairs_one_undo_and_refused_in_record(self):
        self._track()
        rev = self.daw.revision
        res = self.daw.copy_notes(0, [{'on_idx': 0, 'off_idx': 1}, {'on_idx': 2, 'off_idx': 4}],
                                  expected_revision=rev, remove=True)
        self.assertEqual(res['removed'], 4)
        self.assertEqual(self.daw.revision, rev + 1)
        self.assertEqual(by_id(self.daw, 0), {1: [(2.5, 0xB0, 7)]})
        self.assertEqual(len(self.daw.get_clips(0)), 1)
        self.daw.undo()
        self.assertEqual(len(self.daw.tracks[0]), 5)
        self.daw.is_recording = True
        try:
            self.assertIsNone(self.daw.copy_notes(0, [{'on_idx': 0, 'off_idx': 1}]))
            self.assertEqual(self.code(), 'RECORDING_ACTIVE')
        finally:
            self.daw.is_recording = False

    def test_wrong_pair_refused(self):
        self._track()
        self.assertIsNone(self.daw.copy_notes(0, [{'on_idx': 0, 'off_idx': 3}]))
        self.assertEqual(self.code(), 'VALIDATION_FAILED')


class EventsBatchTests(_Base):

    def _track(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1), (cc(0.5, 7, 90), 1),
                           ((2.0, [0xC0, 5]), 1), ((3.0, [0xE0, 0, 64]), 0)],
             clips=[clip(1, 0, 4)])

    def _idx(self, kind):
        return next(i for i, (_b, d) in enumerate(self.daw.tracks[0]) if d[0] & 0xF0 == kind)

    def test_edit_many_one_undo_and_fit_clip(self):
        self._track()
        rev = self.daw.revision
        i_cc, i_pc, i_pb = self._idx(0xB0), self._idx(0xC0), self._idx(0xE0)
        res = self.daw.edit_events_batch(0, [{'index': i_cc, 'cc_value': 10, 'beat': 6.0},
                                             {'index': i_pc, 'program': 9},
                                             {'index': i_pb, 'pitch': -8192}], expected_revision=rev)
        self.assertEqual(res, {'modified': 3})
        self.assertEqual(self.daw.revision, rev + 1)
        evs = {d[0] & 0xF0: (b, list(d)) for b, d in self.daw.tracks[0]}
        self.assertEqual(evs[0xB0], (6.0, [0xB0, 7, 10]))
        self.assertEqual(evs[0xC0][1], [0xC0, 9])
        self.assertEqual(evs[0xE0][1], [0xE0, 0, 0])
        self.assertEqual(self.daw.get_clips(0)[0]['end_beat'], 6.0 + ps.CLIP_TAIL_BEATS)
        self.assertEqual(by_id(self.daw, 0)[1][-1], (6.0, 0xB0, 7))
        self.ok()
        self.daw.undo()
        self.assertEqual(self.daw.get_clips(0)[0]['end_beat'], 4.0)

    def test_refusals_are_atomic(self):
        self._track()
        before = list(self.daw.tracks[0])
        cases = [
            [{'index': 0, 'beat': 1.0}],                                    # nota
            [{'index': self._idx(0xC0), 'cc_value': 1}],                    # campo errato
            [{'index': self._idx(0xB0), 'cc_value': 1}, {'index': 99, 'cc_value': 2}],
        ]
        for edits in cases:
            self.assertIsNone(self.daw.edit_events_batch(0, edits))
            self.assertEqual(self.code(), 'VALIDATION_FAILED', edits)
        self.assertIsNone(self.daw.edit_events_batch(0, [{'index': 0, 'cc_value': 300}]))
        self.assertEqual(self.code(), 'BAD_REQUEST')
        self.assertEqual(self.daw.tracks[0], before)

    def test_delete_note_event_takes_partner(self):
        self._track()
        rev = self.daw.revision
        res = self.daw.delete_events_batch(0, [self._idx(0x80), self._idx(0xB0)], expected_revision=rev)
        self.assertEqual(res, {'removed': 3})
        self.assertFalse(any(d[0] & 0xF0 in (0x80, 0x90) for _b, d in self.daw.tracks[0]))
        self.assertEqual(self.daw.revision, rev + 1)
        self.ok()

    def test_recording_refused(self):
        self._track()
        self.daw.is_recording = True
        try:
            self.assertIsNone(self.daw.delete_events_batch(0, [2]))
            self.assertEqual(self.code(), 'RECORDING_ACTIVE')
            self.assertIsNone(self.daw.edit_events_batch(0, [{'index': 2, 'cc_value': 1}]))
            self.assertEqual(self.code(), 'RECORDING_ACTIVE')
        finally:
            self.daw.is_recording = False


class MaxEventsTests(_Base):

    def test_add_note_and_paste_respect_max_events(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0)])
        self.daw.copy_selection(0, 0.0, 2.0)
        old = ps.MAX_EVENTS_TOTAL
        ps.MAX_EVENTS_TOTAL = 3
        try:
            self.assertFalse(self.daw.add_note(0, 64, 100, 4.0, 1.0))
            self.assertEqual((self.code(), self.daw.last_error['http']), ('VALIDATION_FAILED', 422))
            self.assertIsNone(self.daw.paste_at(0, 8.0))
            self.assertEqual(self.daw.last_error['details']['reason'], 'max_events')
            self.assertEqual(len(self.daw.tracks[0]), 2)
        finally:
            ps.MAX_EVENTS_TOTAL = old


if __name__ == '__main__':
    unittest.main()
