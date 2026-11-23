"""Test di `project_store.py` (fase `ui-persistence`, blocco A).

Funzioni pure: nessun import di app.py, nessun hardware. Le scritture
avvengono solo in cartelle temporanee.
"""

import copy
import errno
import json
import os
import shutil
import tempfile
import unittest
from array import array
from unittest import mock

import helpers  # noqa: F401  (sys.path verso home/matteo/matsynth_web)
import midi_router
import project_store as ps
from helpers import FIXTURES_DIR, REPO_ROOT

SCHEMAS = os.path.join(REPO_ROOT, 'docs', 'schemas')


def _load(name):
    with open(os.path.join(SCHEMAS, name), encoding='utf-8') as f:
        return json.load(f)


def _preset(name):
    with open(os.path.join(FIXTURES_DIR, 'presets', name), encoding='utf-8') as f:
        return json.load(f)


class ValidatorTests(unittest.TestCase):

    def test_part_validators_agree_with_router(self):
        # Due validatori della stessa sezione 3.1 (router e store): stesso esito.
        cases = [c for c in _load('invalid_cases.json')['invalid'] if c['schema'] == 'part']
        for case in cases:
            with self.subTest(case=case['id']):
                doc = _load(case['base'])
                for m in case['mutations']:
                    path = m.get('set') or m.get('del')
                    target = doc
                    for k in path[:-1]:
                        target = target[k]
                    if 'set' in m:
                        target[path[-1]] = m['value']
                    else:
                        del target[path[-1]]
                with self.assertRaises(ps.ContractError):
                    ps.validate_part(doc)
                with self.assertRaises(midi_router.RouterError):
                    midi_router.validate_part(doc)
        for n in range(16):
            self.assertEqual(ps.default_part(n), midi_router.default_part(n))

    def test_event_with_clip_id_is_accepted(self):
        doc = _load('project_v1.json')
        doc['tracks'][0]['events'] = [[0.0, [144, 60, 100], 1], [1.0, [128, 60, 0], 1],
                                      [2.0, [176, 7, 90], 0]]
        self.assertEqual(ps.validate_project(doc), [])

    def test_dangling_clip_id_is_rejected_with_path(self):
        doc = _load('project_v1.json')
        doc['tracks'][0]['events'][1] = [0.9871, [128, 60, 0], 2]      # clip 2 sta sulla traccia 1
        with self.assertRaises(ps.ContractError) as ctx:
            ps.validate_project(doc)
        self.assertEqual((ctx.exception.code, ctx.exception.path),
                         ('VALIDATION_FAILED', 'tracks[0].events[1][2]'))

    def test_clip_id_must_fit_array_h(self):
        doc = _load('project_v1.json')
        doc['tracks'][0]['clips'][0]['id'] = 70000
        doc['next_clip_id'] = 70001
        with self.assertRaises(ps.ContractError) as ctx:
            ps.validate_project(doc)
        self.assertEqual(ctx.exception.path, 'tracks[0].clips[0].id')
        doc = _load('project_v1.json')
        doc['next_clip_id'] = 65537
        with self.assertRaises(ps.ContractError) as ctx:
            ps.validate_project(doc)
        self.assertEqual(ctx.exception.path, 'next_clip_id')
        doc = _load('project_v1.json')
        doc['tracks'][0]['events'][0] = [0.0, [144, 60, 100], True]
        with self.assertRaises(ps.ContractError) as ctx:
            ps.validate_project(doc)
        self.assertEqual(ctx.exception.path, 'tracks[0].events[0][2]')

    def test_overlap_warning_only_without_clip_ids(self):
        doc = _load('project_v1.json')
        doc['tracks'][0]['clips'].append({'id': 4, 'start_beat': 2.0, 'end_beat': 5.0,
                                          'name': 'Take 4', 'created_at': 1.0})
        doc['next_clip_id'] = 5
        self.assertEqual([w['code'] for w in ps.validate_project(doc)], ['CLIP_OVERLAP_PRESENT'])
        doc['tracks'][0]['events'][0].append(1)
        self.assertEqual(ps.validate_project(doc), [])

    def test_future_version_wins_over_other_errors(self):
        doc = _load('project_v1.json')
        doc['schema_version'] = 2
        doc['tracks'] = 'garbage'
        with self.assertRaises(ps.ContractError) as ctx:
            ps.validate_project(doc)
        self.assertEqual(ctx.exception.code, 'UNSUPPORTED_VERSION')

    def test_pair_notes_fifo_with_overlapping_same_pitch(self):
        # A26: due note sovrapposte della stessa altezza -> FIFO (la prima si chiude prima).
        evs = [(0.0, [0x90, 60, 100]), (1.0, [0x90, 60, 90]), (2.0, [0x80, 60, 0]),
               (3.0, [0x80, 60, 0]), (4.0, [0x90, 62, 80])]
        pairs = ps.pair_notes(evs)
        self.assertEqual(pairs[0], (60, 100, 0.0, 2.0, 0, 2))
        self.assertEqual(pairs[1], (60, 90, 1.0, 3.0, 1, 3))
        self.assertEqual(pairs[2], (62, 80, 4.0, None, 4, -1))


class SessionDocTests(unittest.TestCase):

    def _session(self):
        tracks = {t: [] for t in range(16)}
        ids = {t: array('H') for t in range(16)}
        clips = {t: [] for t in range(16)}
        # Clip 1 [0, 4.125) possiede la nota a 0; la nota a 2 e' LIBERA dentro la cornice
        # (es. dopo un'estensione del clip, che non assegna eventi: 6.4).
        tracks[0] = [(0.0, [0x90, 60, 100]), (1.0, [0x80, 60, 0]),
                     (2.0, [0x90, 64, 90]), (4.0, [0x80, 64, 0])]
        ids[0] = array('H', [1, 1, 0, 0])
        clips[0] = [{'id': 1, 'start_beat': 0.0, 'end_beat': 4.125, 'name': 'Take 1', 'created_at': 1.0}]
        return {'tracks': tracks, 'event_clips': ids, 'clips': clips,
                'track_names': {t: f'Track {t + 1}' for t in range(16)},
                'track_colors': {t: t for t in range(16)},
                'muted': {t: False for t in range(16)}, 'solo': {t: t == 3 for t in range(16)},
                'bpm': 96, 'beats_per_measure': 3, 'loop_enabled': True,
                'loop_start': 1.0, 'loop_end': 9.5, 'next_clip_id': 2}

    def _sound(self):
        return {'soundfont': {'file': 'X.sf2'}, 'globals': dict(ps.DEFAULT_GLOBALS),
                'parts': [ps.default_part(n) for n in range(16)]}

    def test_round_trip_keeps_free_event_inside_clip_frame_free(self):
        doc = ps.build_project_doc(self._session(), self._sound(),
                                   {'id': 'p-000000000001', 'name': 'RT'})
        doc = json.loads(json.dumps(doc))
        self.assertEqual(ps.validate_project(doc), [])
        sess = ps.project_to_session(doc)
        self.assertEqual(list(sess['event_clips'][0]), [1, 1, 0, 0])
        self.assertEqual(sess['tracks'][0], self._session()['tracks'][0])
        self.assertEqual((sess['bpm'], sess['beats_per_measure'], sess['loop_enabled'],
                          sess['loop_start'], sess['loop_end'], sess['next_clip_id']),
                         (96, 3, True, 1.0, 9.5, 2))
        self.assertTrue(sess['solo'][3])

    def test_pre_d3_document_is_migrated_by_ranges(self):
        doc = _load('project_v1.json')
        sess = ps.project_to_session(doc)
        self.assertEqual(list(sess['event_clips'][0]), [1] * 6)
        self.assertEqual(list(sess['event_clips'][1]), [2] * 4)

    def test_writer_drops_dangling_ids_and_normalizes_nibble(self):
        s = self._session()
        s['event_clips'][0] = array('H', [7, 1, 0, 0])       # 7 non esiste: mai scritto
        s['tracks'][2] = [(0.0, [0x95, 50, 10]), (1.0, [0x85, 50, 0])]
        s['event_clips'][2] = array('H', [0, 0])
        doc = json.loads(json.dumps(ps.build_project_doc(s, self._sound(), {'id': 'p-1', 'name': 'x'})))
        self.assertEqual(doc['tracks'][0]['events'][0], [0.0, [144, 60, 100]])
        self.assertEqual(doc['tracks'][2]['events'][0][1], [0x92, 50, 10])
        self.assertEqual(s['tracks'][2][0][1], [0x95, 50, 10])        # memoria non modificata
        ps.validate_project(doc)


class AtomicWriteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='matsynth_store_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'p.matsynth.json')

    def _leftovers(self):
        return [n for n in os.listdir(self.tmp) if n.endswith('.tmp')]

    def test_chunked_output_equals_compact_json(self):
        obj = {'a': [[i * 0.5, [144, i % 128, 1]] for i in range(1000)],
               'b': {'c': [{'x': 'è'}] * 3, 'd': None}, 'e': 'ü'}
        ps.atomic_write_json(self.path, obj)
        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(text, json.dumps(obj, separators=(',', ':'), ensure_ascii=False))
        self.assertEqual(self._leftovers(), [])

    def test_replace_failure_keeps_previous_file_and_removes_tmp(self):
        ps.atomic_write_json(self.path, {'v': 1})
        with mock.patch.object(os, 'replace', side_effect=OSError(errno.EIO, 'I/O')):
            with self.assertRaises(ps.StoreError) as ctx:
                ps.atomic_write_json(self.path, {'v': 2})
        self.assertEqual(ctx.exception.code, 'DISK_ERROR')
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'v': 1})
        self.assertEqual(self._leftovers(), [])

    def test_enospc_is_disk_full(self):
        ps.atomic_write_json(self.path, {'v': 1})
        with mock.patch.object(os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space')):
            with self.assertRaises(ps.StoreError) as ctx:
                ps.atomic_write_json(self.path, {'v': 2})
        self.assertEqual(ctx.exception.code, 'DISK_FULL')
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'v': 1})
        self.assertEqual(self._leftovers(), [])

    def test_nan_is_refused_without_touching_file(self):
        ps.atomic_write_json(self.path, {'v': 1})
        with self.assertRaises(ps.StoreError):
            ps.atomic_write_json(self.path, {'v': float('nan')})
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'v': 1})
        self.assertEqual(self._leftovers(), [])

    def test_read_json_limits(self):
        with open(self.path, 'w') as f:
            f.write('{"a": ' + '1' * 100 + '}')
        with self.assertRaises(ps.ContractError) as ctx:
            ps.read_json_file(self.path, 50)
        self.assertEqual(ctx.exception.reason, 'file troppo grande')
        with open(self.path, 'w') as f:
            f.write('{broken')
        with self.assertRaises(ps.ContractError):
            ps.read_json_file(self.path, 1000)
        with self.assertRaises(ps.StoreError) as ctx:
            ps.read_json_file(os.path.join(self.tmp, 'none.json'), 1000)
        self.assertEqual(ctx.exception.code, 'NOT_FOUND')


class SceneAdapterTests(unittest.TestCase):

    def test_v1_to_legacy_keeps_values_and_pan(self):
        scene, _ = ps.migrate_legacy_preset(_preset('legacy_pan.json'), 'legacy_pan')
        legacy = ps.scene_to_legacy_preset(scene)
        self.assertEqual(legacy['font'], 'Synthetic.sf2')
        self.assertEqual(len(legacy['channels']), 16)
        ch0 = legacy['channels'][0]
        self.assertEqual((ch0['volume'], ch0['pan'], ch0['instrument_name']), (110, 20, 'Piano L'))
        self.assertEqual(legacy['global_settings'], {'gain': 1.2, 'reverb_level': 0.35, 'chorus_level': 0.25})
        # legacy -> v1 -> legacy -> v1: stesse parti
        again, _ = ps.migrate_legacy_preset(legacy, 'legacy_pan')
        self.assertEqual(again['parts'], scene['parts'])

    def test_external_parts_are_not_exported_to_legacy(self):
        scene = _load('scene_v1.json')
        legacy = ps.scene_to_legacy_preset(scene)
        ext = [p['part_id'] for p in scene['parts'] if p['destination']['kind'] == 'external']
        self.assertTrue(ext)
        self.assertFalse([c for c in legacy['channels'] if c['channel'] in ext])

    def test_classify(self):
        self.assertEqual(ps.classify_preset(_load('scene_v1.json')), 'v1')
        self.assertEqual(ps.classify_preset(_preset('legacy_minimal.json')), 'legacy')
        self.assertEqual(ps.classify_preset({'foo': 1}), 'invalid')
        self.assertEqual(ps.classify_preset([]), 'invalid')

    def test_make_scene_validates(self):
        parts = [ps.default_part(n) for n in range(16)]
        s = ps.make_scene('s-0123456789ab', 'Nuova', parts, None, ps.DEFAULT_GLOBALS)
        ps.validate_scene(s)
        with self.assertRaises(ps.ContractError):
            ps.make_scene('s-0123456789ab', '', parts, None, ps.DEFAULT_GLOBALS)


class BankConversionTests(unittest.TestCase):

    def test_internal_gs_convention(self):
        self.assertEqual(ps.fluid_bank_to_smf(0, 0), (0, None))
        self.assertEqual(ps.fluid_bank_to_smf(5, 2), (5 << 7, None))
        self.assertEqual(ps.fluid_bank_to_smf(128, 9), (0, None))
        self.assertEqual(ps.fluid_bank_to_smf(128, 3), (None, 'BANK_NOT_REPRESENTABLE'))
        self.assertEqual(ps.smf_to_fluid_bank(130, 0), 1)
        self.assertEqual(ps.smf_to_fluid_bank(130, 9), 128)
        self.assertEqual(ps.smf_to_fluid_bank(None, 0), 0)
        for bank in range(128):
            b14, _ = ps.fluid_bank_to_smf(bank, 4)
            self.assertEqual(ps.smf_to_fluid_bank(b14, 4), bank)

    def test_external_round_trip_msb1_lsb2_pc7(self):
        snd = {'bank_msb': 1, 'bank_lsb': 2, 'program': 7, 'name': 'X'}
        b14, prog = ps.external_sound_to_smf(snd)
        self.assertEqual((b14, prog), (130, 7))
        back = ps.smf_to_external_sound({'bank_msb': None, 'bank_lsb': None, 'program': None, 'name': 'X'},
                                        b14, prog)
        self.assertEqual(back, snd)
        self.assertEqual(ps.external_sound_to_smf({'bank_msb': None, 'bank_lsb': None, 'program': None}),
                         (None, None))
        self.assertEqual(ps.external_sound_to_smf({'bank_msb': 3, 'bank_lsb': None, 'program': 1})[0], 3 << 7)


class LibraryAndStateTests(unittest.TestCase):

    def test_library_favorite_order_remove(self):
        lib = ps.default_library()
        ps.library_set_meta(lib, 'a', favorite=True, order=0)
        ps.library_set_meta(lib, 'b', order=0)
        self.assertEqual(lib['scene_order'], ['b', 'a'])
        self.assertEqual(lib['scene_favorites'], ['a'])
        ps.library_set_meta(lib, 'a', order=5)           # invariato il preferito
        self.assertEqual(lib['scene_favorites'], ['a'])
        entries = ps.sort_scene_entries([{'id': 'z', 'name': 'Zeta'}, {'id': 'a', 'name': 'A'},
                                         {'id': 'b', 'name': 'B'}, {'id': 'c', 'name': 'alfa'}], lib)
        self.assertEqual([e['id'] for e in entries], ['b', 'a', 'c', 'z'])
        ps.library_remove(lib, 'a')
        self.assertEqual((lib['scene_order'], lib['scene_favorites']), (['b'], []))

    def test_library_corrupt_file_gives_defaults(self):
        tmp = tempfile.mkdtemp(prefix='matsynth_lib_')
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, 'library.json')
        with open(path, 'w') as f:
            f.write('{nope')
        self.assertEqual(ps.load_library(path), ps.default_library())

    def test_states_notify_only_on_change(self):
        seen = []
        st = ps.ProjectState(on_change=seen.append)
        st.mark_dirty()
        st.mark_dirty()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]['dirty'])
        snap = st.snapshot()
        st.mark_dirty()                              # mutazione durante il salvataggio
        st.set('p-1', 'X', False, file_stat=[1, 2], if_gen=snap['gen'])
        self.assertTrue(st.public()['dirty'])        # non azzerato: arrivata una mutazione
        snap = st.snapshot()
        st.set('p-1', 'X', False, file_stat=[1, 2], if_gen=snap['gen'])
        self.assertFalse(st.public()['dirty'])
        sc_seen = []
        sc = ps.SceneState(on_change=sc_seen.append)
        sc.set('s-1', 'A', False)
        sc.set('s-1', 'A', False)
        sc.mark_dirty()
        sc.mark_dirty()
        self.assertEqual(len(sc_seen), 2)

    def test_soundfont_state_single_job(self):
        seen = []
        sf = ps.SoundfontState(on_change=seen.append)
        self.assertTrue(sf.begin('A.sf2'))
        self.assertFalse(sf.begin('B.sf2'))
        self.assertIsNone(sf.loaded_file())
        sf.finish('loaded', 'A.sf2', '2')
        self.assertEqual(sf.loaded_file(), 'A.sf2')
        self.assertEqual([s['state'] for s in seen], ['loading', 'loaded'])

    def test_verify_font_loaded(self):
        raw = 'ID  Name\n  3  /usr/share/sounds/sf2/GeneralUser-GS.sf2\n'
        self.assertEqual(ps.verify_font_loaded(raw, '/usr/share/sounds/sf2/GeneralUser-GS.sf2'), '3')
        self.assertIsNone(ps.verify_font_loaded(raw, '/usr/share/sounds/sf2/Other.sf2'))
        self.assertIsNone(ps.verify_font_loaded('', '/x.sf2'))


if __name__ == '__main__':
    unittest.main()
