"""Verifica degli esempi normativi di docs/contracts.md (fase ui-contracts).

- ogni file docs/schemas/*.json e' JSON valido e citato nel contratto;
- gli esempi validi passano il validatore REALE (home/matteo/matsynth_web/project_store.py,
  portato da tests/contract_reference.py in `ui-persistence`), i casi non validi
  falliscono con il codice e il percorso attesi;
- la migrazione dei preset legacy (fixture reali di tests/fixtures/presets) produce
  esattamente l'esempio docs/schemas/scene_from_legacy_decay.json;
- le buste di risposta di api_examples.json rispettano sez. 2 e la tabella dei codici
  letta DAL TESTO del contratto (coerenza testo <-> esempi);
- gli eventi del progetto d'esempio sono byte che mido (reale) accetta e che il DAW
  attuale puo' contenere ed esportare.

Nessun hardware, nessun rtmidi, nessun import di app.py.
"""

import copy
import io
import json
import os
import re
import unittest

import mido

from helpers import FIXTURES_DIR, REPO_ROOT, make_daw
import project_store as cr     # validatore reale (sez. 11.4: stessi casi del riferimento)

DOCS = os.path.join(REPO_ROOT, 'docs')
SCHEMAS = os.path.join(DOCS, 'schemas')
CONTRACT = os.path.join(DOCS, 'contracts.md')


def _load(name):
    with open(os.path.join(SCHEMAS, name), encoding='utf-8') as f:
        return json.load(f)


def _contract_text():
    with open(CONTRACT, encoding='utf-8') as f:
        return f.read()


def _section(text, start_heading, next_heading):
    a = text.index(start_heading)
    b = text.index(next_heading, a)
    return text[a:b]


def _error_code_table(text):
    """Tabella sez. 2.3: righe '| `CODE` | 409 | ...'."""
    sec = _section(text, '### 2.3 Codici d', '### 2.4')
    table = {code: int(http) for code, http in re.findall(r'^\| `([A-Z_]+)` \| (\d{3}) \|', sec, re.M)}
    assert table, 'tabella 2.3 non trovata'
    return table


def _warning_codes(text):
    sec = _section(text, '### 2.4 Codici di warning', '### 2.5')
    return set(re.findall(r'`([A-Z][A-Z_]+)`', sec))


def _apply_mutations(doc, mutations):
    doc = copy.deepcopy(doc)
    for m in mutations:
        path = m['set'] if 'set' in m else m['del']
        target = doc
        for key in path[:-1]:
            target = target[key]
        if 'set' in m:
            target[path[-1]] = m['value']
        else:
            del target[path[-1]]
    return doc


VALIDATORS = {
    'part': lambda d: cr.validate_part(d),
    'scene': lambda d: cr.validate_scene(d),
    'project': lambda d: cr.validate_project(d),
}


class SchemaFilesTests(unittest.TestCase):

    def test_every_schema_file_is_json_and_cited_in_contract(self):
        text = _contract_text()
        names = sorted(n for n in os.listdir(SCHEMAS) if n.endswith('.json'))
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            with self.subTest(file=name):
                _load(name)                                   # JSON valido
                self.assertIn(f'docs/schemas/{name}', text)   # citato nel testo

    def test_valid_examples_pass(self):
        cr.validate_part(_load('part_internal.json'))
        cr.validate_part(_load('part_external.json'))
        cr.validate_scene(_load('scene_v1.json'))
        cr.validate_scene(_load('scene_from_legacy_decay.json'))
        self.assertEqual(cr.validate_project(_load('project_v1.json')), [])

    def test_default_parts_match_contract(self):
        for n in range(16):
            p = cr.default_part(n)
            cr.validate_part(p, index=n)
            self.assertEqual(p['destination'], {'kind': 'internal', 'device_alias': None, 'channel': n})
            self.assertEqual(p['input'], {'device_alias': 'keyboard-main', 'channel': n})
            self.assertTrue(p['monitor'])
        self.assertEqual(cr.default_part(9)['sound']['bank'], 128)

    def test_midi_devices_example_shape(self):
        reg = _load('midi_devices.json')
        self.assertEqual(reg['schema_version'], 1)
        aliases = [d['alias'] for d in reg['devices']]
        self.assertEqual(len(aliases), len(set(aliases)))
        for d in reg['devices']:
            self.assertEqual(set(d), {'alias', 'label', 'input', 'output', 'controls'})
            self.assertRegex(d['alias'], cr.ALIAS_RE)
            for direction in ('input', 'output'):
                if d[direction] is not None:
                    self.assertEqual(set(d[direction]), {'client', 'port'})
                    # nessun dato runtime nel descrittore (numero client/porta, PID)
                    self.assertNotRegex(d[direction]['port'], r'\d+:\d+$|\(\d+')
            if d['controls'] is not None:
                self.assertTrue(set(d['controls']) <= set(cr.CC_KEYS))
        self.assertIn('keyboard-main', aliases)


class InvalidCasesTests(unittest.TestCase):

    def test_invalid_cases_fail_with_expected_code_and_path(self):
        cases = _load('invalid_cases.json')['invalid']
        self.assertGreaterEqual(len(cases), 30)
        ids = [c['id'] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case in cases:
            with self.subTest(case=case['id']):
                doc = _apply_mutations(_load(case['base']), case['mutations'])
                with self.assertRaises(cr.ContractError) as ctx:
                    VALIDATORS[case['schema']](doc)
                self.assertEqual(ctx.exception.code, case['expected_code'], ctx.exception.reason)
                self.assertEqual(ctx.exception.path, case['expected_path'], ctx.exception.reason)

    def test_accepted_with_warnings(self):
        # Il loader non deve essere piu' severo di cio' che il DAW puo' contenere (sez. 5.3).
        for case in _load('invalid_cases.json')['accepted_with_warnings']:
            with self.subTest(case=case['id']):
                doc = _apply_mutations(_load(case['base']), case['mutations'])
                warnings = cr.validate_project(doc)
                self.assertEqual(sorted({w['code'] for w in warnings}), sorted(case['expected_warnings']))


class LegacyMigrationTests(unittest.TestCase):

    def _fixture(self, name):
        with open(os.path.join(FIXTURES_DIR, 'presets', name), encoding='utf-8') as f:
            return json.load(f)

    def test_decay_fixture_migrates_to_published_example(self):
        scene, defaulted = cr.migrate_legacy_preset(self._fixture('legacy_decay.json'), 'legacy_decay')
        self.assertEqual(scene, _load('scene_from_legacy_decay.json'))
        # campi e canali riempiti da default (warning LEGACY_FIELD_DEFAULTED)
        self.assertIn('parts[0].cc.10', defaulted)       # pan mai salvato dai preset legacy
        self.assertIn('parts[1]', defaulted)             # canale non elencato
        self.assertNotIn('parts[3]', defaulted)

    def test_decay_bank_program_cc75_and_globals_preserved(self):
        scene, _ = cr.migrate_legacy_preset(self._fixture('legacy_decay.json'), 'legacy_decay')
        p0, p3, p9 = scene['parts'][0], scene['parts'][3], scene['parts'][9]
        self.assertEqual((p0['sound']['bank'], p0['sound']['program']), (0, 4))
        self.assertEqual((p3['sound']['bank'], p3['sound']['program']), (128, 0))
        self.assertEqual((p9['sound']['bank'], p9['sound']['program']), (1, 7))
        self.assertEqual([p0['cc']['75'], p3['cc']['75'], p9['cc']['75']], [90, 12, 127])
        self.assertEqual((p0['cc']['73'], p0['cc']['72'], p0['cc']['74'], p0['cc']['71']), (10, 70, 50, 30))
        self.assertEqual(scene['globals'], {'gain': 0.8, 'reverb_level': 0.5, 'chorus_level': 0.1})
        self.assertEqual(scene['soundfont'], {'file': 'Synthetic.sf2'})

    def test_pan_fixture_keeps_pan(self):
        scene, defaulted = cr.migrate_legacy_preset(self._fixture('legacy_pan.json'), 'legacy_pan')
        self.assertEqual(scene['parts'][0]['cc']['10'], 20)
        self.assertEqual(scene['parts'][1]['cc']['10'], 100)
        self.assertNotIn('parts[0].cc.10', defaulted)

    def test_minimal_fixture_defaults(self):
        scene, defaulted = cr.migrate_legacy_preset(self._fixture('legacy_minimal.json'), 'legacy_minimal')
        self.assertIsNone(scene['soundfont'])             # font "" -> null
        self.assertEqual(scene['parts'], [cr.default_part(n) for n in range(16)])
        self.assertEqual(len([d for d in defaulted if re.fullmatch(r'parts\[\d+\]', d)]), 16)

    def test_legacy_invalid_value_is_rejected_not_clamped(self):
        data = self._fixture('legacy_decay.json')
        data['channels'][0]['decay'] = 300
        with self.assertRaises(cr.ContractError) as ctx:
            cr.migrate_legacy_preset(data, 'x')
        self.assertEqual((ctx.exception.code, ctx.exception.path), ('VALIDATION_FAILED', 'channels[0].decay'))

    def test_migration_does_not_mutate_input(self):
        data = self._fixture('legacy_pan.json')
        before = copy.deepcopy(data)
        cr.migrate_legacy_preset(data, 'legacy_pan')
        self.assertEqual(data, before)


class ApiExamplesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        text = _contract_text()
        cls.codes = _error_code_table(text)
        cls.warning_codes = _warning_codes(text)
        cls.examples = _load('api_examples.json')['examples']

    def test_error_table_has_expected_core_codes(self):
        for code, http in (('REVISION_CONFLICT', 409), ('TRANSPORT_ACTIVE', 409), ('VALIDATION_FAILED', 422),
                           ('UNSUPPORTED_VERSION', 422), ('PORT_UNAVAILABLE', 503), ('PARTIAL_APPLY', 503),
                           ('UNSAVED_CHANGES', 409), ('NOT_FOUND', 404), ('BAD_REQUEST', 400)):
            self.assertEqual(self.codes.get(code), http, code)

    def _check_warnings(self, warnings, where):
        self.assertIsInstance(warnings, list, where)
        for w in warnings:
            self.assertEqual(set(w), {'code', 'message', 'details'}, where)
            self.assertIn(w['code'], self.warning_codes, where)
            self.assertIsInstance(w['details'], dict, where)

    def test_envelopes(self):
        self.assertGreaterEqual(len(self.examples), 20)
        for ex in self.examples:
            with self.subTest(example=ex['name']):
                http = ex.get('http', ex.get('http_equivalent'))
                body = ex['response']
                self.assertIsInstance(body['ok'], bool)
                if body['ok']:
                    self.assertEqual(body['status'], 'ok')
                    self.assertIn(http, (200, 201, 202))
                    self.assertNotIn('error', body)
                else:
                    self.assertEqual(body['status'], 'error')
                    err = body['error']
                    self.assertEqual(set(err), {'code', 'message', 'details'})
                    self.assertEqual(body['message'], err['message'])
                    self.assertIsInstance(err['details'], dict)
                    self.assertIn(err['code'], self.codes)
                    self.assertEqual(self.codes[err['code']], http)
                    if err['code'] == 'VALIDATION_FAILED':
                        self.assertIn('path', err['details'])
                        self.assertIn('reason', err['details'])
                self._check_warnings(body.get('warnings', []), ex['name'])
                if 'runtime' in body:
                    self._check_warnings(body['runtime'].get('warnings', []), ex['name'])
                if 'part' in body:
                    cr.validate_part(body['part'])
                if 'scene' in body:
                    cr.validate_scene(body['scene'])
                if 'revision' in body:
                    self.assertIsInstance(body['revision'], int)

    def test_external_part_example_has_monitor_off(self):
        ex = next(e for e in self.examples if e['path'] == '/api/parts/1' if 'path' in e)
        self.assertFalse(ex['response']['part']['monitor'])   # sez. 3.2: esterno = monitor spento


class ProjectExampleCompatibilityTests(unittest.TestCase):
    """Il formato eventi del progetto coincide con la rappresentazione in memoria del DAW."""

    def test_event_bytes_are_mido_messages(self):
        proj = _load('project_v1.json')
        for tr in proj['tracks']:
            for beat, data in tr['events']:
                msg = mido.Message.from_bytes(data)
                self.assertEqual(msg.bytes(), data)
                self.assertTrue(hasattr(msg, 'channel'))

    def test_in_memory_tuples_serialize_to_project_events(self):
        daw, _, _ = make_daw()
        daw.add_note(0, 60, 100, 0.0, 0.9871)
        events = json.loads(json.dumps(daw.tracks[0]))    # tuple (beat, list) -> [beat, [..]]
        self.assertEqual(events, [[0.0, [144, 60, 100]], [0.9871, [128, 60, 0]]])
        doc = _load('project_v1.json')
        doc['tracks'][0]['events'] = events
        doc['tracks'][0]['clips'] = []
        doc['tracks'][1]['clips'] = []
        self.assertEqual(cr.validate_project(doc), [])

    def test_example_clips_own_all_their_events_including_last(self):
        # Sez. 6.4: clip di take/import = [primo, ultimo + 0.125): l'ultimo evento
        # (note-off/CC64=0 di chiusura) deve essere posseduto, altrimenti uno
        # spostamento lo abbandonerebbe.
        proj = _load('project_v1.json')
        for tr in proj['tracks']:
            if not tr['clips']:
                continue
            owned = set()
            for c in tr['clips']:
                owned |= cr.owned_indices(tr['events'], c['start_beat'], c['end_beat'])
            self.assertEqual(owned, set(range(len(tr['events']))), f"traccia {tr['id']}")
            last_beat = tr['events'][-1][0]
            self.assertGreaterEqual(max(c['end_beat'] for c in tr['clips']), last_beat + 0.125)

    def test_ownership_matches_current_daw_rule(self):
        # La regola di riferimento coincide con daw_recorder._get_note_pair_indices_in_range.
        proj = _load('project_v1.json')
        daw, _, _ = make_daw()
        for tr in proj['tracks'][:2]:
            daw.tracks[tr['id']] = [(b, list(d)) for b, d in tr['events']]
            for start, end in ((0.0, 1.0), (0.5, 2.6), (1.0, 3.0), (2.0, 4.5)):
                with self.subTest(track=tr['id'], start=start, end=end):
                    self.assertEqual(cr.owned_indices(tr['events'], start, end),
                                     daw._get_note_pair_indices_in_range(tr['id'], start, end))

    def test_example_loads_into_current_daw_and_exports(self):
        proj = _load('project_v1.json')
        daw, _, _ = make_daw()
        for tr in proj['tracks']:
            daw.tracks[tr['id']] = [(b, list(d)) for b, d in tr['events']]
            daw.has_data[tr['id']] = bool(tr['events'])
        mid = mido.MidiFile(file=io.BytesIO(daw.export_midi()))
        self.assertEqual(len(mid.tracks), 2)
        notes = daw.get_full_note_map()['tracks']
        # coppie FIFO del contratto (sez. 6.2): 2 note sulla traccia 0, 1 sulla traccia 1
        self.assertEqual(len(notes[0]['notes']), 2)
        self.assertEqual(len(notes[1]['notes']), 1)


class ContractTextCoherenceTests(unittest.TestCase):

    def test_schema_keys_are_documented(self):
        text = _contract_text()
        keys = set(cr.PART_KEYS) | set(cr.INTERNAL_SOUND_KEYS) | set(cr.EXTERNAL_SOUND_KEYS) \
            | set(cr.SCENE_KEYS) | set(cr.PROJECT_KEYS) | set(cr.PROJECT_OPTIONAL_KEYS) \
            | set(cr.TRACK_KEYS) | set(cr.CLIP_KEYS)
        # ogni chiave dello schema eseguibile compare nel testo normativo come `chiave`,
        # `percorso.chiave` o "chiave"
        missing = sorted(k for k in keys if not re.search(r'[`."]%s[`"]' % re.escape(k), text))
        self.assertEqual(missing, [])

    def test_all_error_codes_used_in_text_are_in_table(self):
        text = _contract_text()
        table = _error_code_table(text)
        warnings = _warning_codes(text)
        client_only = {'CLIENT_TIMEOUT', 'NETWORK_ERROR'}
        used = set(re.findall(r'`(?:\d{3} )?([A-Z][A-Z]+_[A-Z_]+)`', text))
        unknown = sorted(c for c in used if c not in table and c not in warnings and c not in client_only
                         and not c.startswith(('MAX_', 'MATSYNTH_', 'PROJECTS_', 'PRESETS_', 'DEVICES_',
                                               'LIBRARY_', 'SF2_', 'MIDI_DIR', 'STATE_')))
        self.assertEqual(unknown, [])


if __name__ == '__main__':
    unittest.main()
