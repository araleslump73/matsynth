"""Test delle route di `ui-editor`: GET note per traccia, copia/taglia con
`pairs`, eventi batch (busta 2.1, codici 2.3, revisione obbligatoria)."""

import unittest
from array import array

import helpers  # noqa: F401  (percorsi di import)
from test_persistence_app import _AppCase


class EditorRouteTests(_AppCase):

    def setUp(self):
        super().setUp()
        self.daw = self.app.daw
        # Gli appunti sopravvivono a load_session: non lasciarli ad altri test.
        self.addCleanup(self.daw._set_clipboard, [], {'kind': 'range', 'length_beats': 0.0,
                                                       'source_track': 0, 'source_clip_id': None,
                                                       'name': None, 'sustain_release': False})
        self.daw.add_note(0, 60, 100, 0.0, 1.0)
        self.daw.add_note(0, 62, 100, 2.0, 1.0)
        with self.daw._edit():
            self.daw._push_undo()
            evs = list(self.daw.tracks[0]) + [(1.5, [0xB0, 7, 90])]
            ids = array('H', list(self.daw._ids(0)) + [0])
            self.daw._set_sorted(0, evs, ids)

    def rev(self):
        return self.daw.revision

    def notes(self):
        r = self.client.get('/api/daw/track/0/notes')
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def test_get_notes_envelope(self):
        body = self.notes()
        self.assertTrue(body['ok'])
        self.assertEqual(body['revision'], self.rev())
        self.assertEqual([n[0] for n in body['notes']], [60, 62])
        self.assertEqual(len(body['notes'][0]), 7)
        r = self.client.get('/api/daw/track/16/notes')
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))

    def test_copy_cut_pairs(self):
        n = self.notes()['notes'][1]
        pair = [{'on_idx': n[4], 'off_idx': n[5]}]
        r = self.client.post('/api/daw/copy', json={'channel': 0, 'pairs': pair})
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))   # revisione obbligatoria
        r = self.client.post('/api/daw/copy', json={'channel': 0, 'pairs': pair, 'expected_revision': self.rev()})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['clipboard']['kind'], 'range')
        rev = self.rev()
        r = self.client.post('/api/daw/cut', json={'channel': 0, 'pairs': pair, 'expected_revision': rev})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['revision'], rev + 1)
        self.assertEqual([x[0] for x in self.notes()['notes']], [60])
        r = self.client.post('/api/daw/cut', json={'channel': 0, 'pairs': pair, 'expected_revision': rev})
        self.assertEqual((r.status_code, self._code(r)), (409, 'REVISION_CONFLICT'))

    def test_events_batch_routes(self):
        idx = next(i for i, (_b, d) in enumerate(self.daw.tracks[0]) if d[0] & 0xF0 == 0xB0)
        url = '/api/daw/track/0/events/batch'
        r = self.client.put(url, json={'edits': [{'index': idx, 'cc_value': 5}]})
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))
        r = self.client.put(url, json={'edits': [{'index': 0, 'beat': 1.0}], 'expected_revision': self.rev()})
        self.assertEqual((r.status_code, self._code(r)), (422, 'VALIDATION_FAILED'))
        rev = self.rev()
        r = self.client.put(url, json={'edits': [{'index': idx, 'cc_value': 5}], 'expected_revision': rev})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual((r.get_json()['modified'], r.get_json()['revision']), (1, rev + 1))
        self.daw.is_recording = True
        try:
            r = self.client.delete(url, json={'indices': [idx], 'expected_revision': self.rev()})
            self.assertEqual((r.status_code, self._code(r)), (409, 'RECORDING_ACTIVE'))
        finally:
            self.daw.is_recording = False
        r = self.client.delete(url, json={'indices': [idx], 'expected_revision': self.rev()})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['removed'], 1)


if __name__ == '__main__':
    unittest.main()
