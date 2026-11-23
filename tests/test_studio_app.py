"""Test delle route di `ui-studio` (blocco D): busta 2.1, codici 2.3/8.1,
revisione, compatibilita' con legacy_daw.js, notes/batch, return_to_start.

Stessa impalcatura di test_persistence_app (app con rete/disco neutralizzati,
router su FakeBackend).
"""

import unittest

import helpers  # noqa: F401  (percorsi di import)
import midi_router
from test_persistence_app import _AppCase


class StudioRouteTests(_AppCase):

    def setUp(self):
        super().setUp()
        self.daw = self.app.daw
        for b in (0.0, 1.0, 2.0, 3.0):
            self.daw.add_note(0, 60, 100, b, 0.5)

    def post(self, path, **body):
        return self.client.post(path, json=body)

    def rev(self):
        return self.daw.revision

    def _clip(self, start=0.0, end=4.0, ch=0):
        r = self.post('/api/daw/clip/create', channel=ch, start_beat=start, end_beat=end,
                      expected_revision=self.rev())
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()['clip']['id']

    def test_create_move_envelope_revision_and_conflict(self):
        cid = self._clip()
        rev = self.rev()
        r = self.post('/api/daw/clip/move', channel=0, clip_id=cid, target_start_beat=8.0,
                      expected_revision=rev)
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['revision'], rev + 1)
        r = self.post('/api/daw/clip/move', channel=0, clip_id=cid, target_start_beat=0.0,
                      expected_revision=rev)
        self.assertEqual((r.status_code, self._code(r)), (409, 'REVISION_CONFLICT'))
        self.assertEqual(self.rev(), rev + 1)

    def test_not_found_bad_request_and_split_override(self):
        cid = self._clip()
        r = self.post('/api/daw/clip/delete', channel=0, clip_id=999)
        self.assertEqual((r.status_code, self._code(r)), (404, 'NOT_FOUND'))
        r = self.post('/api/daw/clip/move', channel=0, clip_id='x', target_start_beat=1)
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))
        r = self.post('/api/daw/clip/split', channel=0, clip_id=cid, split_beat=50.0)
        self.assertEqual((r.status_code, self._code(r)), (404, 'BAD_REQUEST'))
        self.assertEqual(r.get_json()['error']['details']['field'], 'split_beat')
        r = self.post('/api/daw/clip/split', channel=0, clip_id=cid, split_beat=2.0)
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertIsInstance(body['new_clip_id'], int)           # legacy_daw.js
        r = self.post('/api/daw/clip/merge', channel=0, clip_ids=[cid, body['new_clip_id']])
        self.assertIsInstance(r.get_json()['new_clip_id'], int)
        r = self.post('/api/daw/clip/transpose', channel=0, clip_id=cid, semitones=30)
        self.assertEqual(r.status_code, 400)

    def test_transport_active_refused_copy_allowed(self):
        cid = self._clip()
        self.assertTrue(self.daw.start_playback())
        try:
            r = self.post('/api/daw/clip/move', channel=0, clip_id=cid, target_start_beat=8.0)
            self.assertEqual((r.status_code, self._code(r)), (409, 'TRANSPORT_ACTIVE'))
            r = self.post('/api/daw/cut', channel=0, start_beat=0, end_beat=1)
            self.assertEqual(r.status_code, 409)
            r = self.post('/api/daw/copy', channel=0, clip_id=cid)
            self.assertEqual(r.status_code, 200, r.get_json())
            r = self.post('/api/daw/track/0/duplicate', dest_channel=3)     # override legacy
            self.assertEqual((r.status_code, self._code(r)), (400, 'TRANSPORT_ACTIVE'))
        finally:
            self.daw.stop_all()

    def test_copy_cut_paste(self):
        r = self.post('/api/daw/paste', channel=1, beat=0)
        self.assertEqual((r.status_code, self._code(r)), (409, 'EMPTY_CLIPBOARD'))
        rev = self.rev()
        body = self.post('/api/daw/copy', channel=0, start_beat=0, end_beat=2).get_json()
        self.assertEqual((body['copied'], body['clipboard']['kind'], body['revision']), (4, 'range', rev))
        self.assertEqual(self.rev(), rev)
        body = self.post('/api/daw/cut', channel=0, start_beat=2, end_beat=4,
                         expected_revision=rev).get_json()
        self.assertEqual((body['copied'], body['removed'], body['revision']), (4, 4, rev + 1))
        self.assertEqual(body['clipboard']['length_beats'], 2.0)
        body = self.post('/api/daw/paste', channel=1, beat=8, expected_revision=rev + 1).get_json()
        self.assertEqual((body['pasted'], body['new_clip_id'], body['revision']), (4, None, rev + 2))
        r = self.post('/api/daw/track/0/clear_range', start_beat=0, end_beat=8, expected_revision=rev + 2)
        self.assertEqual(r.get_json()['removed'], 4)
        r = self.post('/api/daw/track/0/clear_range', start_beat=4, end_beat=1)
        self.assertEqual(r.status_code, 400)
        r = self.post('/api/daw/clear_all')
        self.assertEqual(r.get_json()['revision'], rev + 4)

    def test_duplicate_track_variants(self):
        body = self.post('/api/daw/track/0/duplicate', dest_channel=2).get_json()
        self.assertEqual(body['dest_channel'], 2)                  # legacy: int al primo livello
        self.assertTrue(body['legacy'])
        r = self.post('/api/daw/track/0/duplicate', dest_channel=2, include_config=False)
        self.assertEqual((r.status_code, self._code(r)), (409, 'DEST_NOT_EMPTY'))
        part = self.app.router.get_part(0)
        part['sound'] = dict(part['sound'], program=33)
        self.app.router.apply_part(part, send='none')
        r = self.post('/api/daw/track/0/duplicate', dest_channel=2, include_config=True,
                      confirm_overwrite=True)
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertTrue(body['config_copied'])
        dest = self.app.router.get_part(2)
        self.assertEqual(dest['sound']['program'], 33)
        self.assertEqual(dest['destination']['channel'], 2)
        self.assertEqual(dest['input'], midi_router.default_part(2)['input'])
        r = self.post('/api/daw/track/0/duplicate', dest_channel=2, include_config='si')
        self.assertEqual(r.status_code, 400)

    def test_notes_batch(self):
        pairs = [p for p in self.daw._pair_notes(self.daw.tracks[0]) if p[5] >= 0]
        r = self.client.put('/api/daw/track/0/notes/batch',
                            json={'edits': [{'on_idx': pairs[0][4], 'off_idx': pairs[0][5], 'note': 62}]})
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))
        r = self.client.put('/api/daw/track/0/notes/batch',
                            json={'expected_revision': self.rev(),
                                  'edits': [{'on_idx': pairs[0][5], 'off_idx': pairs[0][4]}]})
        self.assertEqual((r.status_code, self._code(r)), (422, 'VALIDATION_FAILED'))
        rev = self.rev()
        r = self.client.put('/api/daw/track/0/notes/batch',
                            json={'expected_revision': rev,
                                  'edits': [{'on_idx': p[4], 'off_idx': p[5], 'note': 64} for p in pairs]})
        self.assertEqual((r.status_code, r.get_json()['revision']), (200, rev + 1))
        pairs = [p for p in self.daw._pair_notes(self.daw.tracks[0]) if p[5] >= 0]
        r = self.client.delete('/api/daw/track/0/notes/batch',
                               json={'expected_revision': rev + 1,
                                     'pairs': [{'on_idx': pairs[0][4], 'off_idx': pairs[0][5]}]})
        self.assertEqual((r.status_code, r.get_json()['removed']), (200, 2))

    def test_record_return_to_start_and_clear_refused(self):
        self.daw.arm_track(5, True)
        r = self.post('/api/daw/record/start', return_to_start='no')
        self.assertEqual(r.status_code, 400)
        r = self.post('/api/daw/record/start', return_to_start=False)
        self.assertEqual(r.status_code, 200, r.get_json())
        try:
            self.assertFalse(self.daw._record_return_to_start)
            r = self.post('/api/daw/track/0/clear')
            self.assertEqual((r.status_code, self._code(r)), (409, 'RECORDING_ACTIVE'))
            r = self.post('/api/daw/clear_all')
            self.assertEqual(r.status_code, 409)
        finally:
            self.daw.stop_all()
        r = self.post('/api/daw/track/0/clear')
        self.assertEqual(r.status_code, 200)
        self.assertIn('revision', r.get_json())


if __name__ == '__main__':
    unittest.main()
