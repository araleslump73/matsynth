"""Test DAW della fase `ui-studio` (backend): appunti/taglia/incolla pair-aware,
operazioni clip per ID (D3), duplica, batch note, next_clip_id monotono, D6.

Un test a fronte di ogni requisito di docs/contracts.md 6.3-6.6, 8.2 e delle
anomalie A15-A19, A24 (D6), A27, A28. Nessun hardware: porte finte di helpers,
mido reale.
"""

import json
import random
import time
import unittest
from array import array

import mido

from helpers import make_daw, wait_until

import project_store as ps

NOTE_ON, NOTE_OFF, CC = 0x90, 0x80, 0xB0


def on(beat, note, ch=0, vel=100):
    return (float(beat), [NOTE_ON | ch, note, vel])


def off(beat, note, ch=0):
    return (float(beat), [NOTE_OFF | ch, note, 0])


def cc(beat, num, val, ch=0):
    return (float(beat), [CC | ch, num, val])


def clip(cid, start, end, name=None):
    return {'id': cid, 'start_beat': float(start), 'end_beat': float(end),
            'name': name or f'C{cid}', 'created_at': 0.0}


def load(daw, ch, items, clips=(), next_id=None):
    """items: [(evento, clip_id)] in qualunque ordine; ordina come la DAW."""
    events = [e for e, _ in items]
    ids = array('H', [c for _, c in items])
    daw._set_sorted(ch, events, ids)
    daw.clips[ch] = [dict(c) for c in clips]
    daw._next_clip_id = next_id or (max([c['id'] for c in clips], default=0) + 1)
    daw._density_dirty = True


def by_id(daw, ch):
    """{clip_id: [(beat, status_kind, note)]} per controllare l'ownership."""
    out = {}
    for (b, d), cid in zip(daw.tracks[ch], daw.event_clips[ch]):
        out.setdefault(cid, []).append((b, d[0] & 0xF0, d[1]))
    return out


def group_balance(events, ids):
    """Per ogni ID (0 = liberi): (note aperte, note-off orfani) con FIFO nel gruppo."""
    res = {}
    groups = {}
    for (b, d), cid in zip(events, ids):
        groups.setdefault(cid, []).append((b, d))
    for cid, evs in groups.items():
        pairs = ps.pair_notes(evs)
        open_ = sum(1 for p in pairs if p[5] < 0)
        paired_offs = {p[5] for p in pairs if p[5] >= 0}
        orphans = sum(1 for i, (_b, d) in enumerate(evs)
                      if ps._is_note_off(d) and i not in paired_offs)
        res[cid] = (open_, orphans)
    return res


def state(daw):
    return ({ch: list(daw.tracks[ch]) for ch in range(16)},
            {ch: list(daw.event_clips[ch]) for ch in range(16)},
            {ch: daw.get_clips(ch) for ch in range(16)})


class _Base(unittest.TestCase):

    def setUp(self):
        self.daw, self.fi, _ = make_daw()

    def ok(self):
        self.assertEqual(self.daw.check_invariants(), [])

    def code(self):
        return self.daw.last_error['code']


class CopyCutTests(_Base):
    """A15 + 6.3: coppie complete, note-off di note iniziate prima esclusi, sustain."""

    def _boundary_track(self):
        load(self.daw, 0, [
            (on(0.0, 60), 0), (off(1.5, 60), 0),     # inizia prima: resta, anche il suo off
            (on(1.0, 62), 0), (off(5.0, 62), 0),     # inizia dentro, finisce dopo end
            (on(2.0, 64), 0), (off(2.5, 64), 0),
            (cc(1.5, 64, 127), 0),                    # pedale premuto dentro l'intervallo
            (on(4.5, 65), 0), (off(4.8, 65), 0),     # fuori (end = 4)
        ])

    def test_copy_range_is_pair_aware_and_adds_sustain_release(self):
        self._boundary_track()
        rev = self.daw.revision
        n = self.daw.copy_selection(0, 1.0, 4.0)
        self.assertEqual(n, 5)                         # 62 on/off, 64 on/off, CC64
        self.assertEqual(self.daw.revision, rev)       # la copia non cambia revisione
        rel = [(b, d[0] & 0xF0, d[1], d[2]) for b, d in self.daw._clipboard]
        self.assertIn((0.0, NOTE_ON, 62, 100), rel)
        self.assertIn((4.0, NOTE_OFF, 62, 0), rel)     # note-off oltre end: incluso
        self.assertNotIn(NOTE_OFF, [k for _b, k, n_, _v in rel if n_ == 60])
        self.assertIn((3.0, CC, 64, 0), rel)            # CC64=0 a end - start
        self.assertEqual(rel, sorted(rel, key=lambda r: r[0]))
        info = self.daw.clipboard_info()
        self.assertEqual((info['kind'], info['length_beats'], info['sustain_release']), ('range', 3.0, True))

    def test_cut_paste_across_boundary_complete_pairs_one_undo(self):
        """Piano 12 / contratto 12 ui-studio: taglia/incolla a cavallo di un confine."""
        self._boundary_track()
        before = state(self.daw)
        depth, rev = len(self.daw._undo_stack), self.daw.revision
        res = self.daw.cut(0, 1.0, 4.0)
        self.assertEqual((res['copied'], res['removed']), (5, 5))
        self.assertEqual(len(self.daw._undo_stack), depth + 1)
        self.assertEqual(self.daw.revision, rev + 1)
        left = [(b, d[0] & 0xF0, d[1]) for b, d in self.daw.tracks[0]]
        self.assertEqual(left, [(0.0, NOTE_ON, 60), (1.5, NOTE_OFF, 60),
                                (4.5, NOTE_ON, 65), (4.8, NOTE_OFF, 65)])
        self.ok()
        res = self.daw.paste_at(1, 10.0)
        self.assertEqual((res['pasted'], res['new_clip_id']), (6, None))
        notes = self.daw.get_full_note_map()['tracks'][1]['notes']
        self.assertEqual(sorted((n[0], n[2], n[3]) for n in notes), [(62, 10.0, 14.0), (64, 11.0, 11.5)])
        self.assertTrue(all((d[0] & 0x0F) == 1 for _b, d in self.daw.tracks[1]))   # nibble = traccia
        self.assertEqual(self.daw.get_clips(1), [])                                  # nessun clip
        self.ok()
        self.daw.undo()
        self.daw.undo()
        self.assertEqual(state(self.daw), before)

    def test_clear_range_and_cut_keep_partial_clips_a16(self):
        load(self.daw, 0, [(on(0.5, 60), 1), (off(1.0, 60), 1),
                           (on(2.5, 62), 2), (off(3.0, 62), 2),
                           (on(5.0, 64), 2), (off(6.0, 64), 2)],
             [clip(1, 0, 2), clip(2, 2, 8)])
        res = self.daw.clear_range(0, 0.0, 4.0)
        self.assertEqual((res['removed'], res['clips_removed']), (4, [1]))
        self.assertEqual([c['id'] for c in self.daw.get_clips(0)], [2])      # parziale: resta
        self.assertEqual(by_id(self.daw, 0), {2: [(5.0, NOTE_ON, 64), (6.0, NOTE_OFF, 64)]})
        self.ok()

    def test_overlapping_same_pitch_cut_keeps_every_clip_balanced(self):
        # FIFO globale: on_A(0) con off_B(3). Per ID: A [0,4), B [2,3).
        load(self.daw, 0, [(on(0, 60), 1), (on(2, 60), 2), (off(3, 60), 2), (off(4, 60), 1)],
             [clip(1, 0, 5), clip(2, 2, 4)])
        self.daw.clear_range(0, 0.0, 1.0)
        bal = group_balance(self.daw.tracks[0], self.daw.event_clips[0])
        self.assertEqual(bal, {2: (0, 0)})
        self.assertTrue(self.daw.delete_clip(0, 2) is not None)
        self.assertEqual(self.daw.tracks[0], [])          # nessun note-off orfano rimasto

    def test_copy_clip_and_cut_clip(self):
        load(self.daw, 0, [(on(4, 60), 3), (off(5, 60), 3), (on(4.5, 70), 0), (off(4.7, 70), 0)],
             [clip(3, 4, 8, 'Riff')])
        self.assertEqual(self.daw.copy_selection(0, clip_id=3), 2)
        info = self.daw.clipboard_info()
        self.assertEqual((info['kind'], info['length_beats'], info['name']), ('clip', 4.0, 'Riff'))
        res = self.daw.cut(0, clip_id=3)
        self.assertEqual((res['removed'], res['clips_removed']), (2, [3]))
        self.assertEqual(by_id(self.daw, 0), {0: [(4.5, NOTE_ON, 70), (4.7, NOTE_OFF, 70)]})
        self.assertIsNone(self.daw.copy_selection(0, clip_id=99))
        self.assertEqual(self.code(), 'NOT_FOUND')

    def test_copy_allowed_during_play_cut_refused(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0)])
        self.daw.is_playing = True
        try:
            self.assertEqual(self.daw.copy_selection(0, 0.0, 2.0), 2)
            self.assertIsNone(self.daw.cut(0, 0.0, 2.0))
            self.assertEqual(self.code(), 'TRANSPORT_ACTIVE')
            self.assertIsNone(self.daw.clear_range(0, 0.0, 2.0))
            self.assertIsNone(self.daw.paste_at(0, 4.0))
            self.assertEqual(self.code(), 'TRANSPORT_ACTIVE')
        finally:
            self.daw.is_playing = False
        self.assertEqual(len(self.daw.tracks[0]), 2)

    def test_invalid_ranges(self):
        for args in ((0, 2.0, 2.0), (0, -1.0, 2.0), (0, float('nan'), 2.0), (16, 0.0, 1.0)):
            self.assertIsNone(self.daw.copy_selection(*args))
            self.assertEqual(self.code(), 'BAD_REQUEST')


class PasteTests(_Base):

    def test_empty_clipboard_is_409_a27(self):
        rev = self.daw.revision
        self.assertIsNone(self.daw.paste_at(0, 0.0))
        self.assertEqual(self.code(), 'EMPTY_CLIPBOARD')
        self.daw.copy_selection(0, 0.0, 4.0)            # intervallo vuoto: appunti vuoti
        self.assertIsNone(self.daw.paste_at(0, 0.0))
        self.assertEqual(self.code(), 'EMPTY_CLIPBOARD')
        self.assertEqual(self.daw.revision, rev)
        self.assertIsNone(self.daw.clipboard_info())

    def test_paste_clip_creates_clip_owning_all_events(self):
        load(self.daw, 2, [(on(0, 60, ch=2), 4), (off(1, 60, ch=2), 4)], [clip(4, 0, 2, 'Basso')])
        self.daw.copy_selection(2, clip_id=4)
        res = self.daw.paste_at(5, 8.0)
        cid = res['new_clip_id']
        self.assertEqual(cid, 5)
        self.assertEqual(self.daw.get_clips(5)[0]['name'], 'Basso (copia)')
        self.assertEqual((self.daw.get_clips(5)[0]['start_beat'], self.daw.get_clips(5)[0]['end_beat']), (8.0, 10.0))
        self.assertEqual(by_id(self.daw, 5), {cid: [(8.0, NOTE_ON, 60), (9.0, NOTE_OFF, 60)]})
        self.assertTrue(all((d[0] & 0x0F) == 5 for _b, d in self.daw.tracks[5]))
        # sovrapposizione consentita: secondo incolla nello stesso punto
        res2 = self.daw.paste_at(5, 8.0)
        self.assertEqual(len(self.daw.get_clips(5)), 2)
        self.assertNotEqual(res2['new_clip_id'], cid)
        self.ok()

    def test_paste_range_ids_follow_add_note_rule(self):
        load(self.daw, 0, [], [clip(1, 10, 20)])
        load(self.daw, 1, [(on(0, 60, ch=1), 0), (off(3, 60, ch=1), 0), (cc(0.5, 7, 90, ch=1), 0)])
        self.daw._next_clip_id = 2
        self.daw.copy_selection(1, 0.0, 4.0)
        self.daw.paste_at(0, 9.5)        # note-on a 9.5 fuori, CC a 10.0 dentro il clip 1
        got = {(b, k, n): cid for (b, d), cid in zip(self.daw.tracks[0], self.daw.event_clips[0])
               for k, n in [(d[0] & 0xF0, d[1])]}
        self.assertEqual(got, {(9.5, NOTE_ON, 60): 0, (12.5, NOTE_OFF, 60): 0, (10.0, CC, 7): 1})
        self.daw.paste_at(0, 11.0)       # tutto dentro: note-off (14.0) con l'ID del note-on
        self.assertEqual(by_id(self.daw, 0)[1], [(10.0, CC, 7), (11.0, NOTE_ON, 60), (11.5, CC, 7),
                                                 (14.0, NOTE_OFF, 60)])
        bal = group_balance(self.daw.tracks[0], self.daw.event_clips[0])
        self.assertEqual(bal[1], (0, 0))
        self.ok()

    def test_paste_beyond_max_beat_is_refused(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0)])
        self.daw.copy_selection(0, 0.0, 2.0)
        self.assertIsNone(self.daw.paste_at(0, ps.MAX_BEAT - 0.5))
        self.assertEqual(self.code(), 'BAD_REQUEST')
        self.assertIsNone(self.daw.paste_at(0, -1))
        self.assertEqual(len(self.daw.tracks[0]), 2)

    def test_stale_revision(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0)])
        self.daw.copy_selection(0, 0.0, 2.0)
        stale = self.daw.revision - 1
        for call in (lambda: self.daw.paste_at(0, 4.0, expected_revision=stale),
                     lambda: self.daw.cut(0, 0.0, 2.0, expected_revision=stale),
                     lambda: self.daw.clear_range(0, 0.0, 2.0, expected_revision=stale)):
            self.assertIsNone(call())
            self.assertEqual(self.code(), 'REVISION_CONFLICT')
        self.assertEqual(len(self.daw.tracks[0]), 2)


class ClipOpTests(_Base):
    """A18 / D3: ogni operazione tocca solo gli eventi del proprio ID."""

    def _overlap(self):
        # clip 1 [0,8) e clip 2 [4,12) sovrapposti, evento libero a 6
        load(self.daw, 0, [(on(1, 60), 1), (off(2, 60), 1), (on(5, 62), 1), (off(9, 62), 1),
                           (on(4.5, 70), 2), (off(5.5, 70), 2), (cc(6, 1, 33), 0)],
             [clip(1, 0, 8), clip(2, 4, 12)])

    def test_move_overlapping_clip_does_not_touch_the_other(self):
        self._overlap()
        before2 = by_id(self.daw, 0)[2]
        free = by_id(self.daw, 0)[0]
        res = self.daw.move_clip(0, 1, 20.0)
        self.assertEqual(res['moved'], 4)
        g = by_id(self.daw, 0)
        self.assertEqual(g[2], before2)
        self.assertEqual(g[0], free)
        self.assertEqual(g[1], [(21.0, NOTE_ON, 60), (22.0, NOTE_OFF, 60), (25.0, NOTE_ON, 62), (29.0, NOTE_OFF, 62)])
        c1 = next(c for c in self.daw.get_clips(0) if c['id'] == 1)
        self.assertEqual((c1['start_beat'], c1['end_beat']), (20.0, 28.0))
        self.ok()
        self.assertIsNone(self.daw.move_clip(0, 1, -1.0))
        self.assertEqual(self.code(), 'BAD_REQUEST')
        self.assertIsNone(self.daw.move_clip(0, 1, ps.MAX_BEAT - 1))
        self.assertEqual(self.code(), 'BAD_REQUEST')

    def test_delete_and_transpose_only_own_events(self):
        self._overlap()
        self.daw.transpose_clip(0, 2, 5)
        g = by_id(self.daw, 0)
        self.assertEqual([n for _b, _k, n in g[2]], [75, 75])
        self.assertEqual([n for _b, k, n in g[1] if k != CC], [60, 60, 62, 62])
        res = self.daw.delete_clip(0, 1)
        self.assertEqual(res['removed'], 4)
        self.assertEqual(set(by_id(self.daw, 0)), {0, 2})
        self.assertEqual([c['id'] for c in self.daw.get_clips(0)], [2])
        self.ok()

    def test_resize_d4(self):
        self._overlap()
        self.assertEqual(self.daw.resize_clip(0, 1, 16.0)['end_beat'], 16.0)      # estensione
        self.assertEqual(by_id(self.daw, 0)[0], [(6.0, CC, 1)])                    # nessun evento adottato
        res = self.daw.resize_clip(0, 1, 5.0)          # ultimo posseduto: note-on a 5
        self.assertIsNone(res)
        self.assertEqual(self.code(), 'CLIP_RESIZE_WOULD_EXCLUDE')
        self.assertEqual(self.daw.last_error['details']['min_end_beat'], 5.0)
        self.assertEqual(self.daw.resize_clip(0, 1, 5.25)['end_beat'], 5.25)       # note-off a 9 fuori: ok
        self.assertIsNone(self.daw.resize_clip(0, 2, 0.0))   # minimo 4.125 <= note-on a 4.5
        self.assertEqual(self.daw.last_error['details']['min_end_beat'], 4.5)
        self.ok()

    def test_resize_min_length_and_empty_clip(self):
        load(self.daw, 0, [], [clip(1, 2, 6)])
        self.assertEqual(self.daw.resize_clip(0, 1, 0.0)['end_beat'], 2.125)

    def test_split_reassigns_right_half_and_keeps_playback(self):
        self._overlap()
        before = list(self.daw.tracks[0])
        res = self.daw.split_clip(0, 1, 3.0)
        new = res['new_clip_id']
        self.assertEqual(self.daw.tracks[0], before)                  # eventi non spostati
        g = by_id(self.daw, 0)
        self.assertEqual(g[1], [(1.0, NOTE_ON, 60), (2.0, NOTE_OFF, 60)])
        self.assertEqual(g[new], [(5.0, NOTE_ON, 62), (9.0, NOTE_OFF, 62)])
        clips = {c['id']: c for c in self.daw.get_clips(0)}
        self.assertEqual((clips[1]['end_beat'], clips[new]['start_beat'], clips[new]['end_beat']), (3.0, 3.0, 8.0))
        self.assertEqual(clips[new]['name'], 'C1 (split)')
        self.ok()

    def test_split_crossing_note_stays_left(self):
        load(self.daw, 0, [(on(1, 60), 1), (off(6, 60), 1), (cc(5, 64, 0), 1)], [clip(1, 0, 8)])
        new = self.daw.split_clip(0, 1, 4.0)['new_clip_id']
        g = by_id(self.daw, 0)
        self.assertEqual(g[1], [(1.0, NOTE_ON, 60), (6.0, NOTE_OFF, 60)])
        self.assertEqual(g[new], [(5.0, CC, 64)])
        self.assertIsNone(self.daw.split_clip(0, 1, 3.995))
        self.assertEqual((self.code(), self.daw.last_error['http']), ('BAD_REQUEST', 404))

    def test_merge_remaps_ids_and_rejects_missing(self):
        self._overlap()
        load(self.daw, 0, list(zip(self.daw.tracks[0], self.daw.event_clips[0])) + [(on(30, 50), 3), (off(31, 50), 3)],
             self.daw.get_clips(0) + [clip(3, 30, 32)])
        self.assertIsNone(self.daw.merge_clips(0, [1, 99]))
        self.assertEqual(self.code(), 'NOT_FOUND')
        self.assertIsNone(self.daw.merge_clips(0, [1, 1]))
        self.assertEqual(self.code(), 'BAD_REQUEST')
        res = self.daw.merge_clips(0, [1, 2])
        new = res['new_clip_id']
        self.assertEqual((res['start_beat'], res['end_beat']), (0.0, 12.0))
        self.assertEqual(set(by_id(self.daw, 0)), {0, 3, new})
        self.assertEqual(len(by_id(self.daw, 0)[new]), 6)
        self.ok()

    def test_create_clip_takes_only_free_complete_pairs(self):
        load(self.daw, 0, [(on(1, 60), 0), (off(2, 60), 0), (on(3, 62), 5), (off(4, 62), 5),
                           (cc(3.5, 7, 80), 0), (on(9, 64), 0), (off(10, 64), 0)], [clip(5, 3, 5)])
        res = self.daw.create_clip(0, 0.0, 6.0)
        cid = res['clip']['id']
        self.assertEqual(res['events'], 3)
        g = by_id(self.daw, 0)
        self.assertEqual(g[cid], [(1.0, NOTE_ON, 60), (2.0, NOTE_OFF, 60), (3.5, CC, 7)])
        self.assertEqual(g[5], [(3.0, NOTE_ON, 62), (4.0, NOTE_OFF, 62)])
        self.assertEqual(g[0], [(9.0, NOTE_ON, 64), (10.0, NOTE_OFF, 64)])
        self.assertEqual(self.daw.create_clip(0, 50.0, 52.0)['events'], 0)      # clip vuoto ammesso
        self.ok()

    def test_duplicate_clip_after_itself_keeps_clipboard(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1), (cc(0.5, 64, 100), 1)], [clip(1, 0, 4, 'A')])
        self.daw.copy_selection(0, 0.0, 0.25)
        cb = list(self.daw._clipboard)
        res = self.daw.duplicate_clip(0, 1)
        new = res['new_clip_id']
        self.assertEqual(self.daw._clipboard, cb)
        c = res['clip']
        # CC64 >= 64 come ultimo pedale: CC64=0 a fine clip, cornice + 0.125 per contenerlo.
        self.assertEqual((c['start_beat'], c['end_beat'], c['name']), (4.0, 8.125, 'A (copia)'))
        self.assertEqual(by_id(self.daw, 0)[new], [(4.0, NOTE_ON, 60), (4.5, CC, 64), (5.0, NOTE_OFF, 60), (8.0, CC, 64)])
        self.assertEqual(len(self.daw._undo_stack), 1)
        self.ok()

    def test_clip_ops_refused_with_transport_and_stale_revision(self):
        self._overlap()
        calls = [lambda **k: self.daw.move_clip(0, 1, 3.0, **k),
                 lambda **k: self.daw.resize_clip(0, 1, 20.0, **k),
                 lambda **k: self.daw.split_clip(0, 1, 3.0, **k),
                 lambda **k: self.daw.merge_clips(0, [1, 2], **k),
                 lambda **k: self.daw.delete_clip(0, 1, **k),
                 lambda **k: self.daw.transpose_clip(0, 1, 2, **k),
                 lambda **k: self.daw.create_clip(0, 0.0, 1.0, **k),
                 lambda **k: self.daw.duplicate_clip(0, 1, **k)]
        before = state(self.daw)
        for flag in ('is_playing', 'is_recording'):
            setattr(self.daw, flag, True)
            try:
                for call in calls:
                    self.assertIsNone(call())
                    self.assertEqual(self.code(), 'TRANSPORT_ACTIVE')
            finally:
                setattr(self.daw, flag, False)
        for call in calls:
            self.assertIsNone(call(expected_revision=self.daw.revision - 1))
            self.assertEqual(self.code(), 'REVISION_CONFLICT')
        self.assertEqual(state(self.daw), before)
        self.assertEqual(self.daw._undo_stack, [])
        for call in calls[:2]:
            self.assertIsNotNone(call(expected_revision=self.daw.revision))

    def test_clip_not_found(self):
        for call in (lambda: self.daw.move_clip(0, 7, 1.0), lambda: self.daw.delete_clip(0, 7),
                     lambda: self.daw.resize_clip(0, 7, 3.0), lambda: self.daw.split_clip(0, 7, 1.0),
                     lambda: self.daw.transpose_clip(0, 7, 1), lambda: self.daw.duplicate_clip(0, 7)):
            self.assertIsNone(call())
            self.assertEqual(self.code(), 'NOT_FOUND')


class DuplicateTrackTests(_Base):
    """A17: duplicazione con o senza configurazione, destinazione occupata."""

    def _src(self):
        load(self.daw, 0, [(on(0, 60), 1), (off(1, 60), 1), (on(2, 62), 0), (off(3, 62), 0)],
             [clip(1, 0, 2, 'Take 1')])

    def test_legacy_variant_unchanged(self):
        self._src()
        load(self.daw, 3, [(on(5, 40, ch=3), 0), (off(6, 40, ch=3), 0)], [], next_id=2)
        res = self.daw.duplicate_track(0, 3)                 # nessun campo nuovo: legacy
        self.assertTrue(res['legacy'])
        self.assertEqual(self.daw.tracks[3], self.daw.tracks[0])     # sostituzione silenziosa
        self.assertEqual(list(self.daw.event_clips[3]), [0, 0, 0, 0])
        self.assertEqual(self.daw.get_clips(3), [])
        self.ok()

    def test_new_variant_copies_clips_and_requires_confirmation(self):
        self._src()
        load(self.daw, 3, [(on(5, 40, ch=3), 0), (off(6, 40, ch=3), 0)], [], next_id=2)
        before = state(self.daw)
        self.assertIsNone(self.daw.duplicate_track(0, 3, include_config=False))
        self.assertEqual(self.code(), 'DEST_NOT_EMPTY')
        self.assertEqual(state(self.daw), before)
        res = self.daw.duplicate_track(0, 3, include_config=False, confirm_overwrite=True)
        self.assertTrue(res['replaced'])
        new = res['clip_map']['1']
        self.assertEqual(new, 2)
        self.assertEqual([c['id'] for c in self.daw.get_clips(3)], [2])
        self.assertEqual(self.daw.get_clips(3)[0]['name'], 'Take 1')
        self.assertEqual(list(self.daw.event_clips[3]), [2, 2, 0, 0])
        self.ok()
        res = self.daw.duplicate_track(0, 4, include_config=True)       # destinazione vuota
        self.assertFalse(res['replaced'])
        self.assertEqual(self.daw.track_names[4], 'Copy of Track 1')

    def test_transport_and_errors(self):
        self._src()
        self.daw.is_playing = True
        try:
            self.assertIsNone(self.daw.duplicate_track(0, 3))
            self.assertEqual((self.code(), self.daw.last_error['http']), ('TRANSPORT_ACTIVE', 400))
            self.assertIsNone(self.daw.duplicate_track(0, 3, include_config=False))
            self.assertEqual((self.code(), self.daw.last_error['http']), ('TRANSPORT_ACTIVE', None))
        finally:
            self.daw.is_playing = False
        self.assertIsNone(self.daw.duplicate_track(0, 0))
        self.assertIsNone(self.daw.duplicate_track(5, 6, include_config=False))
        self.assertEqual(self.code(), 'BAD_REQUEST')


class ClipIdCounterTests(_Base):

    def test_next_clip_id_never_decreases_after_undo_a19(self):
        self.daw.add_note(0, 60, 100, 0.0, 1.0)
        c1 = self.daw.create_clip(0, 0.0, 2.0)['clip']['id']
        self.daw.undo()
        self.assertEqual(self.daw.get_clips(0), [])
        c2 = self.daw.create_clip(0, 0.0, 2.0)['clip']['id']
        self.assertGreater(c2, c1)                    # ID non riusato
        self.daw.undo()
        self.daw.redo()
        self.assertEqual([c['id'] for c in self.daw.get_clips(0)], [c2])
        c3 = self.daw.split_clip(0, c2, 0.5)['new_clip_id']
        self.assertGreater(c3, c2)
        self.daw.undo()
        self.daw.undo()
        self.assertGreater(self.daw._next_clip_id, c3)

    def test_undo_snapshots_restore_event_clips(self):
        load(self.daw, 0, [(on(1, 60), 1), (off(2, 60), 1)], [clip(1, 0, 4)])
        before = state(self.daw)
        self.daw.split_clip(0, 1, 0.5)
        self.daw.merge_clips(0, [c['id'] for c in self.daw.get_clips(0)])
        self.daw.undo()
        self.daw.undo()
        self.assertEqual(state(self.daw), before)


class BatchTests(_Base):
    """A28: validazione completa, 422, un solo undo/revisione."""

    def _notes(self):
        load(self.daw, 0, [(on(0, 60), 0), (off(1, 60), 0), (on(2, 62), 0), (off(3, 62), 0),
                           (on(4, 64), 0)])           # 64 aperta (off_idx -1)
        return {p[0]: (p[4], p[5]) for p in self.daw._pair_notes(self.daw.tracks[0])}

    def test_edit_batch_one_undo_one_revision(self):
        p = self._notes()
        rev = self.daw.revision
        res = self.daw.edit_notes_batch(0, [
            {'on_idx': p[60][0], 'off_idx': p[60][1], 'on_beat': 5.0, 'off_beat': 6.0},
            {'on_idx': p[62][0], 'off_idx': p[62][1], 'note': 67, 'velocity': 30},
            {'on_idx': p[64][0], 'off_idx': -1, 'velocity': 11}], expected_revision=rev)
        self.assertEqual(res['modified'], 3)
        self.assertEqual(self.daw.revision, rev + 1)
        self.assertEqual(len(self.daw._undo_stack), 1)
        notes = sorted((n[0], n[1], n[2], n[3]) for n in self.daw.get_full_note_map()['tracks'][0]['notes'])
        self.assertEqual(notes[0], (60, 100, 5.0, 6.0))
        self.assertEqual(notes[2], (67, 30, 2.0, 3.0))
        self.assertEqual(notes[1][:2], (64, 11))
        self.daw.undo()
        self.assertEqual(self.daw.tracks[0][0], on(0, 60))

    def test_edit_batch_invalid_entries_change_nothing(self):
        p = self._notes()
        before = list(self.daw.tracks[0])
        rev = self.daw.revision
        good = {'on_idx': p[60][0], 'off_idx': p[60][1], 'velocity': 5}
        cases = [
            ([good, {'on_idx': 99, 'off_idx': -1}], 'VALIDATION_FAILED'),            # fuori intervallo
            ([good, {'on_idx': p[60][1], 'off_idx': -1}], 'VALIDATION_FAILED'),      # non e' un note-on
            ([good, {'on_idx': p[62][0], 'off_idx': p[60][1]}], 'VALIDATION_FAILED'),  # off di un'altra nota
            ([good, dict(good)], 'VALIDATION_FAILED'),                                # indice ripetuto
            ([{'on_idx': p[62][0], 'off_idx': p[62][1], 'on_beat': 3.5}], 'VALIDATION_FAILED'),  # off <= on
            ([{'on_idx': p[64][0], 'off_idx': -1, 'off_beat': 9.0}], 'VALIDATION_FAILED'),
            ([{'on_idx': p[60][0], 'off_idx': p[60][1], 'velocity': 0}], 'BAD_REQUEST'),  # diventerebbe note-off
            ([{'on_idx': p[60][0], 'off_idx': p[60][1], 'note': 128}], 'BAD_REQUEST'),
            ([{'on_idx': p[60][0], 'off_idx': p[60][1], 'on_beat': -1}], 'BAD_REQUEST'),
            ([{'on_idx': p[60][0], 'off_idx': p[60][1], 'start_beat': 1}], 'BAD_REQUEST'),  # campo ignoto
            ([{'on_idx': '0', 'off_idx': 1}], 'BAD_REQUEST'),
            ([], 'BAD_REQUEST'),
        ]
        for edits, code in cases:
            self.assertIsNone(self.daw.edit_notes_batch(0, edits, expected_revision=rev), edits)
            self.assertEqual(self.code(), code, edits)
        self.assertIsNone(self.daw.edit_notes_batch(0, [good], expected_revision=rev - 1))
        self.assertEqual(self.code(), 'REVISION_CONFLICT')
        self.assertEqual(self.daw.tracks[0], before)
        self.assertEqual((self.daw.revision, self.daw._undo_stack), (rev, []))

    def test_delete_batch_and_recording(self):
        p = self._notes()
        rev = self.daw.revision
        self.assertIsNone(self.daw.delete_notes_batch(0, [{'on_idx': p[60][0], 'off_idx': p[62][1]}],
                                                      expected_revision=rev))
        self.assertEqual(self.code(), 'VALIDATION_FAILED')
        res = self.daw.delete_notes_batch(0, [{'on_idx': p[60][0], 'off_idx': p[60][1]},
                                              {'on_idx': p[64][0], 'off_idx': -1}], expected_revision=rev)
        self.assertEqual(res['removed'], 3)
        self.assertEqual(self.daw.tracks[0], [on(2, 62), off(3, 62)])
        self.assertEqual(len(self.daw._undo_stack), 1)
        self.daw.is_recording = True
        try:
            self.assertIsNone(self.daw.edit_notes_batch(0, [{'on_idx': 0, 'off_idx': 1}]))
            self.assertEqual((self.code(), self.daw.last_error['http']), ('RECORDING_ACTIVE', None))
        finally:
            self.daw.is_recording = False
        self.daw.is_playing = True               # in Play l'editing note e' consentito (6.4)
        try:
            self.assertIsNotNone(self.daw.edit_notes_batch(0, [{'on_idx': 0, 'off_idx': 1, 'velocity': 9}]))
        finally:
            self.daw.is_playing = False

    def test_pair_across_two_clips_is_refused(self):
        load(self.daw, 0, [(on(0, 60), 1), (on(2, 60), 2), (off(3, 60), 2), (off(4, 60), 1)],
             [clip(1, 0, 5), clip(2, 2, 4)])
        p = self.daw._pair_notes(self.daw.tracks[0])[0]      # FIFO: on_A(0) con off_B(3)
        self.assertIsNone(self.daw.delete_notes_batch(0, [{'on_idx': p[4], 'off_idx': p[5]}]))
        self.assertEqual(self.code(), 'VALIDATION_FAILED')


class ReturnToStartTests(unittest.TestCase):
    """A24 / D6: dopo Stop di Record la posizione torna al punto di partenza."""

    def _record(self, daw, fi, start_beat, **kw):
        daw.timeline_position = start_beat * daw.beat_duration
        self.assertTrue(daw.start_recording(0, **kw))
        self.assertEqual(daw.get_state()['record_start_beat'], start_beat)
        time.sleep(0.05)
        fi.feed(mido.Message('note_on', channel=0, note=60, velocity=90))
        self.assertTrue(wait_until(lambda: len(daw.tracks[0]) >= 1))
        time.sleep(0.05)
        self.assertGreater(daw.timeline_position, start_beat * daw.beat_duration)

    def test_returns_after_record_stop_by_default(self):
        daw, fi, _ = make_daw()
        daw.arm_track(0, True)
        self._record(daw, fi, 4.0)
        daw.stop_recording()
        self.assertAlmostEqual(daw.timeline_position, 4.0 * daw.beat_duration)
        self.assertIsNone(daw.get_state()['record_start_beat'])
        self.assertEqual(len(daw.get_clips(0)), 1)              # dati e clip invariati

    def test_option_off_keeps_stop_position(self):
        daw, fi, _ = make_daw()
        daw.arm_track(0, True)
        self._record(daw, fi, 2.0, return_to_start=False)
        pos = daw.timeline_position
        daw.stop_recording()
        self.assertGreaterEqual(daw.timeline_position, pos)
        self.assertGreater(daw.timeline_position, 2.0 * daw.beat_duration)

    def test_stop_all_in_record_with_other_tracks_playing(self):
        daw, fi, _ = make_daw()
        daw.add_note(5, 40, 100, 0.0, 100.0)                    # altra traccia: playback simultaneo
        daw.arm_track(0, True)
        self._record(daw, fi, 1.0)
        self.assertTrue(daw.is_playing)
        daw.stop_all()
        self.assertFalse(daw.is_playing or daw.is_recording)
        self.assertAlmostEqual(daw.timeline_position, 1.0 * daw.beat_duration)
        time.sleep(0.05)                                        # nessun thread la riscrive
        self.assertAlmostEqual(daw.timeline_position, 1.0 * daw.beat_duration)

    def test_record_stop_alone_also_stops_companion_playback(self):
        daw, fi, _ = make_daw()
        daw.add_note(5, 40, 100, 0.0, 100.0)
        daw.arm_track(0, True)
        self._record(daw, fi, 1.0)
        daw.stop_recording()
        self.assertFalse(daw.is_playing)
        self.assertAlmostEqual(daw.timeline_position, 1.0 * daw.beat_duration)

    def test_bpm_changed_uses_beats(self):
        daw, fi, _ = make_daw(bpm=120)
        daw.arm_track(0, True)
        self._record(daw, fi, 8.0)
        daw.bpm, daw.beat_duration = 60, 1.0                    # simula un cambio BPM
        daw.stop_recording()
        self.assertAlmostEqual(daw.timeline_position, 8.0)      # 8 beat a 60 BPM = 8 s

    def test_no_return_after_play_stop(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 100.0)
        daw.timeline_position = 1.0
        self.assertTrue(daw.start_playback())
        time.sleep(0.1)
        daw.stop_playback()
        self.assertGreater(daw.timeline_position, 1.0)
        self.assertIsNone(daw.get_state()['record_start_beat'])


class ReviewFixTests(unittest.TestCase):
    """Difetti di docs/handoff/ui-persistence-review.md toccati da ui-studio."""

    def _smf(self, beats, n_notes=1):
        mid = mido.MidiFile(ticks_per_beat=480)
        tr = mido.MidiTrack()
        mid.tracks.append(tr)
        tr.append(mido.Message('note_on', note=60, velocity=100, time=0))
        tr.append(mido.Message('note_off', note=60, velocity=0, time=int(beats * 480)))
        for _ in range(n_notes - 1):
            tr.append(mido.Message('note_on', note=62, velocity=100, time=0))
            tr.append(mido.Message('note_off', note=62, velocity=0, time=0))
        import io
        buf = io.BytesIO()
        mid.save(file=buf)
        return buf.getvalue()

    def test_m1_import_beyond_max_beat_refused_without_mutation(self):
        from daw_recorder import DawError
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 1.0)
        before, rev = state(daw), daw.revision
        with self.assertRaises(DawError) as cm:
            daw.import_midi(self._smf(ps.MAX_BEAT + 10))
        self.assertEqual(cm.exception.code, 'VALIDATION_FAILED')
        self.assertEqual(cm.exception.details['reason'], 'max_beat')
        self.assertEqual((state(daw), daw.revision), (before, rev))

    def test_m1_import_merge_beyond_max_events_refused(self):
        from daw_recorder import DawError
        daw, _, _ = make_daw()
        load(daw, 0, [(on(i * 0.001, 60), 0) if i % 2 == 0 else (off(i * 0.001, 60), 0)
                      for i in range(ps.MAX_EVENTS_TOTAL - 2)])
        rev = daw.revision
        with self.assertRaises(DawError) as cm:
            daw.import_midi(self._smf(1.0, n_notes=2), merge=True)
        self.assertEqual(cm.exception.details['reason'], 'max_events')
        self.assertEqual(daw.revision, rev)
        self.assertEqual(len(daw.tracks[0]), ps.MAX_EVENTS_TOTAL - 2)

    def test_l2_import_refused_when_clip_ids_exhausted(self):
        from daw_recorder import DawError
        daw, _, _ = make_daw()
        daw._next_clip_id = ps.MAX_CLIP_ID + 1
        rev = daw.revision
        with self.assertRaises(DawError):
            daw.import_midi(self._smf(1.0))
        self.assertEqual(daw.revision, rev)
        self.assertEqual(daw.tracks[0], [])

    def _start_record(self, daw, fi):
        daw.arm_track(0, True)
        self.assertTrue(daw.start_recording())
        time.sleep(0.05)
        fi.feed(mido.Message('note_on', channel=0, note=60, velocity=90))
        self.assertTrue(wait_until(lambda: len(daw.tracks[0]) >= 1))

    def test_m2_clear_refused_during_record_allowed_in_play(self):
        daw, fi, _ = make_daw()
        daw.add_note(1, 40, 100, 0.0, 1.0)
        self._start_record(daw, fi)
        rev = daw.revision
        self.assertIsNone(daw.clear_track(0))
        self.assertEqual(daw.last_error['code'], 'RECORDING_ACTIVE')
        self.assertIsNone(daw.clear_all_tracks())
        self.assertEqual(daw.revision, rev)
        fi.feed(mido.Message('note_off', channel=0, note=60, velocity=0))
        self.assertTrue(wait_until(lambda: len(daw.tracks[0]) >= 2))
        daw.stop_recording()
        self.assertEqual(daw.check_invariants(), [])
        self.assertEqual(len(daw.get_clips(0)), 1)
        self.assertTrue(daw.start_playback())
        try:
            self.assertTrue(daw.clear_track(1))
        finally:
            daw.stop_playback()

    def test_l3_indexed_edits_rechecked_under_lock(self):
        daw, fi, _ = make_daw()
        daw.add_note(1, 40, 100, 0.0, 1.0)
        self._start_record(daw, fi)
        try:
            rev = daw.revision
            for call in (lambda: daw.edit_event(1, 0, {'velocity': 10}),
                         lambda: daw.delete_event(1, 0),
                         lambda: daw.add_note(1, 50, 100, 2.0, 1.0),
                         lambda: daw.edit_note_pair(1, 0, 1, {'note': 41}),
                         lambda: daw.delete_note_pair(1, 0, 1)):
                daw.last_error = None
                self.assertFalse(call())
                self.assertEqual(daw.last_error['code'], 'RECORDING_ACTIVE')
            self.assertEqual(daw.revision, rev)
        finally:
            daw.stop_recording()

    def test_l2_finalize_with_exhausted_ids_leaves_free_take(self):
        daw, fi, _ = make_daw()
        daw._next_clip_id = ps.MAX_CLIP_ID + 1
        self._start_record(daw, fi)
        fi.feed(mido.Message('note_off', channel=0, note=60, velocity=0))
        self.assertTrue(wait_until(lambda: len(daw.tracks[0]) >= 2))
        daw.stop_recording()
        self.assertEqual(daw.get_clips(0), [])
        self.assertEqual(list(daw.event_clips[0]), [0, 0])
        self.assertEqual(daw.check_invariants(), [])


class RandomizedIntegrityTests(unittest.TestCase):
    """Sequenze casuali (seme fisso) di operazioni di montaggio: dopo ogni passo
    nessun disallineamento, nessun ID pendente, ogni ID clip bilanciato, il
    documento progetto resta valido, undo ripristina esattamente, next_clip_id
    non decresce."""

    def _doc_ok(self, daw):
        sound = {'soundfont': None, 'globals': dict(ps.DEFAULT_GLOBALS),
                 'parts': [ps.default_part(n) for n in range(16)]}
        doc = json.loads(json.dumps(ps.build_project_doc(daw.snapshot_session(), sound,
                                                         {'id': 'p-000000000001', 'name': 'R'})))
        ps.validate_project(doc)

    def test_random_sequences(self):
        for seed in range(6):
            self._run(seed)

    def _run(self, seed):
        rnd = random.Random(seed)
        daw, _, _ = make_daw()
        for ch in (0, 1):
            for _ in range(25):
                daw.add_note(ch, rnd.choice((60, 62, 64)), 100, rnd.randrange(0, 64) / 4, rnd.randrange(1, 12) / 4)
            daw.create_clip(ch, 0.0, 8.0)
            daw.create_clip(ch, 4.0, 14.0)

        def a_clip(ch):
            cl = daw.get_clips(ch)
            return rnd.choice(cl)['id'] if cl else 1

        ch_ = lambda: rnd.choice((0, 1))
        beat = lambda: rnd.randrange(0, 80) / 4
        ops = [
            lambda: daw.copy_selection(ch_(), *sorted((beat(), beat() + 0.25))),
            lambda: daw.cut(ch_(), *sorted((beat(), beat() + 0.25))),
            lambda: daw.clear_range(ch_(), *sorted((beat(), beat() + 0.25))),
            lambda: daw.paste_at(ch_(), beat()),
            lambda: (lambda c: daw.copy_selection(c, clip_id=a_clip(c)))(ch_()),
            lambda: (lambda c: daw.cut(c, clip_id=a_clip(c)))(ch_()),
            lambda: (lambda c: daw.move_clip(c, a_clip(c), beat()))(ch_()),
            lambda: (lambda c: daw.resize_clip(c, a_clip(c), beat()))(ch_()),
            lambda: (lambda c: daw.split_clip(c, a_clip(c), beat()))(ch_()),
            lambda: (lambda c: daw.merge_clips(c, [x['id'] for x in daw.get_clips(c)][:2]))(ch_()),
            lambda: (lambda c: daw.delete_clip(c, a_clip(c)))(ch_()),
            lambda: (lambda c: daw.transpose_clip(c, a_clip(c), rnd.choice((-12, 3, 70))))(ch_()),
            lambda: daw.create_clip(ch_(), *sorted((beat(), beat() + 1))),
            lambda: (lambda c: daw.duplicate_clip(c, a_clip(c)))(ch_()),
            lambda: daw.duplicate_track(ch_(), rnd.choice((2, 3)), include_config=False, confirm_overwrite=True),
            lambda: daw.duplicate_track(ch_(), 3),
            lambda: self._batch(daw, rnd, ch_()),
            lambda: daw.add_note(ch_(), rnd.choice((60, 62)), 90, beat(), 1.0),
        ]
        last_next = daw._next_clip_id
        for step in range(220):
            if rnd.random() < 0.15 and daw._undo_stack:
                prev = state(daw)
                if daw.undo():
                    daw.redo()
                    self.assertEqual(state(daw), prev, f'seed {seed} passo {step}: undo/redo')
                continue
            before = state(daw)
            depth = len(daw._undo_stack)
            op = rnd.choice(ops)
            op()
            self.assertEqual(daw.check_invariants(), [], f'seed {seed} passo {step}')
            if state(daw) != before:      # un gesto completato = un livello di undo
                self.assertTrue(len(daw._undo_stack) > depth or depth == daw._MAX_UNDO,
                                f'seed {seed} passo {step}: mutazione senza undo')
            for ch in range(4):
                for cid, (open_, orphans) in group_balance(daw.tracks[ch], daw.event_clips[ch]).items():
                    self.assertEqual((open_, orphans), (0, 0), f'seed {seed} passo {step} ch {ch} id {cid}')
            self.assertGreaterEqual(daw._next_clip_id, last_next)
            last_next = daw._next_clip_id
            if len(daw._undo_stack) > depth or (depth == daw._MAX_UNDO and state(daw) != before):
                after = state(daw)
                daw.undo()
                self.assertEqual(state(daw), before, f'seed {seed} passo {step}: undo')
                daw.redo()
                self.assertEqual(state(daw), after, f'seed {seed} passo {step}: redo')
            if step % 20 == 0:
                self._doc_ok(daw)
        self._doc_ok(daw)

    @staticmethod
    def _batch(daw, rnd, ch):
        pairs = [p for p in daw._pair_notes(daw.tracks[ch]) if p[5] >= 0]
        if not pairs:
            return None
        p = rnd.choice(pairs)
        if rnd.random() < 0.5:
            return daw.delete_notes_batch(ch, [{'on_idx': p[4], 'off_idx': p[5]}])
        start = rnd.randrange(0, 60) / 4
        return daw.edit_notes_batch(ch, [{'on_idx': p[4], 'off_idx': p[5], 'on_beat': start,
                                          'off_beat': start + 0.5, 'note': rnd.choice((60, 64))}])


if __name__ == '__main__':
    unittest.main()
