"""Test delle route di `ui-persistence` (blocco C): preset legacy, scene,
progetti, SoundFont, MIDI con routing, revisione nelle route DAW, M1/M2.

app.py importato con rete/disco neutralizzati (helpers.import_app); tutte le
scritture in una cartella temporanea (helpers.redirect_app_paths); router
dell'app ricablato su FakeBackend come in tests/test_routing.py.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import mido

import helpers
from helpers import FANTOM_NAME, FIXTURES_DIR, FLUID_NAME, wait_until

import midi_router
import project_store as ps

FANTOM_DESC = {'client': 'FANTOM-08', 'port': 'FANTOM-08 MIDI 1'}


def _registry():
    return {'schema_version': 1, 'devices': [
        {'alias': 'keyboard-main', 'label': 'Tastiera', 'input': FANTOM_DESC, 'output': None, 'controls': None},
        {'alias': 'fantom', 'label': 'FANTOM-08', 'input': FANTOM_DESC, 'output': FANTOM_DESC, 'controls': ['7']},
        {'alias': 'synth-b', 'label': 'Synth B', 'input': None, 'output': None, 'controls': None}]}


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


class FakeFluid:
    """Console FluidSynth finta: `fonts` elenca i file caricati (ID 2)."""

    def __init__(self):
        self.commands = []
        self.loaded = []
        self.fail_prefixes = ()
        self.ignore_load = False
        self.reported_path = None      # `fonts` riporta un percorso diverso da quello caricato

    def __call__(self, cmd):
        self.commands.append(cmd)
        if cmd == 'fonts':
            return 'ID  Name\n' + ''.join(f'  2  {p}\n' for p in self.loaded)
        if cmd.startswith('load '):
            path = cmd[5:]
            if os.path.exists(path) and not self.ignore_load:
                self.loaded = [self.reported_path or path]
            return 'loaded SoundFont has ID 2\n'
        if cmd.startswith('unload'):
            self.loaded = []
            return ''
        if cmd.startswith(self.fail_prefixes):
            return ''
        return 'OK'


def _external_part(t, channel, alias='fantom', sound=None):
    p = midi_router.default_part(t)
    p['destination'] = {'kind': 'external', 'device_alias': alias, 'channel': channel}
    p['sound'] = sound or midi_router.default_external_sound()
    p['cc'] = {}
    p['monitor'] = False
    return p


class _AppCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = helpers.import_app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='matsynth_persist_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        helpers.redirect_app_paths(self.app, self.tmp)
        self.fluid = FakeFluid()
        self.app.send_fluid = self.fluid
        r = self.app.router
        r.close()
        self.backend = helpers.FakeBackend(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME, FANTOM_NAME))
        r.backend = self.backend
        with r._cfg_lock:
            r._devices = {d['alias']: d for d in _registry()['devices']}
        for t in range(16):
            r.apply_part(midi_router.default_part(t), send='none')
        r.refresh_ports()
        daw = self.app.daw
        daw.stop_all()
        daw.load_session(daw.empty_session())
        self.app.project_state.set(None, ps.UNTITLED_PROJECT, False)
        self.app.scene_state.set(None, None, False)
        self.client = self.app.app.test_client()
        for fn in os.listdir(os.path.join(FIXTURES_DIR, 'presets')):
            shutil.copy(os.path.join(FIXTURES_DIR, 'presets', fn), self.app.PRESETS_DIR)

    def tearDown(self):
        self.app.daw.stop_all()

    # utilita'
    def _sf(self, name='Synthetic.sf2'):
        path = os.path.join(self.app.SF2_DIR, name)
        with open(path, 'wb') as f:
            f.write(b'RIFF')
        return path

    def _load_sf(self, name='Synthetic.sf2'):
        self._sf(name)
        r = self.client.post('/api/soundfont/load', json={'file': name})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertTrue(wait_until(lambda: self.app.sf_state.public()['state'] != 'loading'))
        self.assertEqual(self.app.sf_state.public()['state'], 'loaded')

    def _state(self):
        with open(self.app.STATE_FILE, encoding='utf-8') as f:
            return json.load(f)

    def _code(self, r):
        return r.get_json()['error']['code']


class SceneRouteTests(_AppCase):

    def test_list_get_and_legacy_migration_warning(self):
        body = self.client.get('/api/scenes').get_json()
        self.assertTrue(body['ok'])
        self.assertEqual({s['id'] for s in body['scenes']}, {'legacy_decay', 'legacy_minimal', 'legacy_pan'})
        self.assertTrue(all(s['source'] == 'legacy' and s['valid'] for s in body['scenes']))
        r = self.client.get('/api/scenes/legacy_decay').get_json()
        with open(os.path.join(helpers.REPO_ROOT, 'docs', 'schemas', 'scene_from_legacy_decay.json'), encoding='utf-8') as f:
            self.assertEqual(r['scene'], json.load(f))
        w = r['warnings'][0]
        self.assertEqual(w['code'], 'LEGACY_FIELD_DEFAULTED')
        self.assertIn('parts[0].cc.10', w['details']['paths'])
        self.assertEqual(self.client.get('/api/scenes/nope').status_code, 404)
        self.assertEqual(self.client.get('/api/scenes/..%2Fx').status_code, 404)

    def test_invalid_scene_is_listed_as_invalid_and_422(self):
        with open(os.path.join(self.app.PRESETS_DIR, 'bad.json'), 'w') as f:
            f.write('{"format": "matsynth.scene", "schema_version": 9}')
        body = self.client.get('/api/scenes').get_json()
        bad = next(s for s in body['scenes'] if s['id'] == 'bad')
        self.assertFalse(bad['valid'])
        self.assertEqual(bad['error']['code'], 'UNSUPPORTED_VERSION')
        r = self.client.get('/api/scenes/bad')
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()['error']['details']['found'], 9)

    def test_save_as_name_exists_and_overwrite(self):
        self._load_sf()
        r = self.client.post('/api/scenes', json={'name': 'Concerto'})
        self.assertEqual(r.status_code, 201, r.get_json())
        scene = r.get_json()['scene']
        self.assertRegex(scene['id'], r'^s-[0-9a-f]{12}$')
        self.assertEqual(scene['soundfont'], {'file': 'Synthetic.sf2'})
        self.assertEqual(self.app.scene_state.public(), {'scene_id': scene['id'], 'name': 'Concerto', 'dirty': False})
        r = self.client.post('/api/scenes', json={'name': 'concerto'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['error']['details']['existing_id'], scene['id'])
        r = self.client.post('/api/scenes', json={'name': 'Synthetic pan'})     # nome di una legacy
        self.assertEqual(self._code(r), 'NAME_EXISTS')
        r = self.client.post('/api/scenes', json={'name': 'Concerto', 'overwrite_id': scene['id']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['scene']['id'], scene['id'])
        self.assertEqual(r.get_json()['scene']['created'], scene['created'])
        # adattatori legacy sulla scena v1
        lst = self.client.get('/api/presets/list').get_json()
        self.assertIn({'filename': scene['id'] + '.json', 'name': 'Concerto', 'created': scene['created'],
                       'font': 'Synthetic.sf2'}, lst)
        legacy = self.client.get(f'/api/presets/load/{scene["id"]}.json').get_json()['data']
        self.assertEqual(set(legacy), {'name', 'created', 'font', 'channels', 'global_settings'})
        self.assertIn('pan', legacy['channels'][0])

    def test_put_on_legacy_scene_makes_backup_and_keeps_id(self):
        with open(os.path.join(self.app.PRESETS_DIR, 'legacy_pan.json'), 'rb') as f:
            original = f.read()
        r = self.client.put('/api/scenes/legacy_pan', json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        backup = os.path.join(self.app.PRESETS_DIR, '.legacy_backup', 'legacy_pan.json')
        with open(backup, 'rb') as f:
            self.assertEqual(f.read(), original)
        doc = json.load(open(os.path.join(self.app.PRESETS_DIR, 'legacy_pan.json'), encoding='utf-8'))
        self.assertEqual((doc['format'], doc['id'], doc['name']), ('matsynth.scene', 'legacy_pan', 'Synthetic Pan'))
        self.client.put('/api/scenes/legacy_pan', json={})          # secondo salvataggio: backup invariato
        with open(backup, 'rb') as f:
            self.assertEqual(f.read(), original)
        names = [p['filename'] for p in self.client.get('/api/presets/list').get_json()]
        self.assertEqual(sorted(names), ['legacy_decay.json', 'legacy_minimal.json', 'legacy_pan.json'])

    def test_patch_name_favorite_order_and_delete(self):
        r = self.client.patch('/api/scenes/legacy_minimal', json={'name': 'Minima', 'favorite': True, 'order': 0})
        self.assertEqual(r.status_code, 200, r.get_json())
        meta = r.get_json()['scene_meta']
        self.assertEqual((meta['name'], meta['favorite'], meta['order']), ('Minima', True, 0))
        self.assertTrue(os.path.exists(os.path.join(self.app.PRESETS_DIR, 'legacy_minimal.json')))
        self.assertTrue(os.path.exists(self.app.LIBRARY_FILE))
        self.assertEqual(self.client.get('/api/scenes').get_json()['scenes'][0]['id'], 'legacy_minimal')
        self.assertEqual(self._code(self.client.patch('/api/scenes/legacy_pan', json={'name': 'minima'})),
                         'NAME_EXISTS')
        self.app.scene_state.set('legacy_minimal', 'Minima', False)
        self.assertEqual(self.client.delete('/api/scenes/legacy_minimal').status_code, 200)
        self.assertEqual(self.app.scene_state.public(), {'scene_id': None, 'name': None, 'dirty': True})
        self.assertNotIn('legacy_minimal', ps.load_library(self.app.LIBRARY_FILE)['scene_favorites'])
        self.assertEqual(self.client.delete('/api/scenes/legacy_minimal').status_code, 404)

    def test_legacy_rename_refuses_to_overwrite_a10b(self):
        r = self.client.post('/api/presets/rename', json={'old_filename': 'legacy_minimal.json',
                                                          'new_name': 'legacy_pan'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['error']['code'], 'NAME_EXISTS')
        self.assertTrue(os.path.exists(os.path.join(self.app.PRESETS_DIR, 'legacy_minimal.json')))
        with open(os.path.join(self.app.PRESETS_DIR, 'legacy_pan.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['name'], 'Synthetic Pan')

    def test_legacy_rename_of_v1_scene_keeps_file_and_id(self):
        sid = self.client.post('/api/scenes', json={'name': 'Uno'}).get_json()['scene']['id']
        r = self.client.post('/api/presets/rename', json={'old_filename': sid + '.json', 'new_name': 'Due'})
        self.assertEqual(r.get_json()['new_filename'], sid + '.json')
        self.assertEqual(self.client.get(f'/api/scenes/{sid}').get_json()['scene']['name'], 'Due')
        self.assertEqual(self.app.scene_state.public()['name'], 'Due')


class RecallTests(_AppCase):

    def test_recall_blocked_with_transport_active(self):
        self.app.daw.is_playing = True
        try:
            del self.fluid.commands[:]
            r = self.client.post('/api/scenes/legacy_minimal/recall', json={})
        finally:
            self.app.daw.is_playing = False
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._code(r), 'TRANSPORT_ACTIVE')
        self.assertEqual(self.fluid.commands, [])

    def test_unsaved_changes_then_discard(self):
        self.client.get('/cc/0/7/90')                     # modifica di parte -> scena modificata
        self.assertTrue(self.app.scene_state.public()['dirty'])
        r = self.client.post('/api/scenes/legacy_minimal/recall', json={})
        self.assertEqual((r.status_code, self._code(r)), (409, 'UNSAVED_CHANGES'))
        self.assertEqual(r.get_json()['error']['details'], {'scope': 'scene'})
        r = self.client.post('/api/scenes/legacy_minimal/recall', json={'discard_changes': True})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_soundfont_missing_change_required_then_recall(self):
        r = self.client.post('/api/scenes/legacy_decay/recall', json={})
        self.assertEqual((r.status_code, self._code(r)), (409, 'SOUNDFONT_MISSING'))
        self._sf()
        r = self.client.post('/api/scenes/legacy_decay/preflight', json={})
        self.assertEqual(self._code(r), 'SOUNDFONT_CHANGE_REQUIRED')
        self.assertEqual(r.get_json()['error']['details'], {'required': 'Synthetic.sf2', 'loaded': None})
        self._load_sf()
        self.assertTrue(self.app.scene_state.public()['dirty'])     # il caricamento SF rende dirty (4.7)
        del self.fluid.commands[:]
        pre = self.client.post('/api/scenes/legacy_decay/preflight', json={'discard_changes': True})
        self.assertEqual(pre.status_code, 200)
        self.assertTrue(pre.get_json()['preflight']['soundfont']['match'])
        self.assertEqual(self.fluid.commands, [])                   # preflight senza effetti
        self.app.project_state.set(None, ps.UNTITLED_PROJECT, False)
        r = self.client.post('/api/scenes/legacy_decay/recall', json={'discard_changes': True})
        self.assertEqual(r.status_code, 200, r.get_json())
        cmds = self.fluid.commands
        for c in ('set synth.gain 0.8', 'select 0 2 0 4', 'cc 0 75 90', 'cc 0 10 64', 'select 3 2 128 0',
                  'select 1 2 0 0', 'select 9 2 1 7'):
            self.assertIn(c, cmds)
        self.assertEqual(self.app.scene_state.public(), {'scene_id': 'legacy_decay', 'name': 'Synthetic Decay',
                                                         'dirty': False})
        self.assertTrue(self.app.project_state.public()['dirty'])
        st = self._state()
        self.assertEqual(st['parts'][0]['sound']['program'], 4)
        self.assertEqual(st['channels']['3']['decay'], 12)
        self.assertEqual(st['scene']['scene_id'], 'legacy_decay')
        self.assertEqual(st['gain'], 0.8)
        self.assertEqual(self.app.router.get_part(9)['sound']['bank'], 1)

    def _external_scene(self, alias):
        scene, _ = ps.migrate_legacy_preset(helpers.load_json_fixture('presets', 'legacy_minimal.json'), 'ext_scene')
        scene['parts'][1] = _external_part(1, 4, alias, {'bank_msb': 1, 'bank_lsb': 2, 'program': 7, 'name': 'Pad'})
        ps.atomic_write_json(os.path.join(self.app.PRESETS_DIR, 'ext_scene.json'), scene)

    def test_ports_unavailable_and_partial_with_consent(self):
        self._external_scene('synth-b')               # alias senza porte: unbound
        r = self.client.post('/api/scenes/ext_scene/recall', json={})
        self.assertEqual((r.status_code, self._code(r)), (409, 'PORTS_UNAVAILABLE'))
        self.assertEqual(r.get_json()['error']['details']['parts'],
                         [{'part_id': 1, 'alias': 'synth-b', 'direction': 'output', 'status': 'unbound'}])
        r = self.client.post('/api/scenes/ext_scene/recall', json={'allow_partial': True})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertIn('PART_UNAVAILABLE', [w['code'] for w in body['warnings']])
        self.assertEqual(body['recall']['parts'][1]['result'], 'skipped_unavailable')
        self.assertEqual(self.app.router.get_part(1)['destination']['device_alias'], 'synth-b')
        self.assertFalse([c for c in self.fluid.commands if c.startswith('select 1 ')])

    def test_external_part_recall_sends_bank_program_to_port(self):
        self._external_scene('fantom')
        r = self.client.post('/api/scenes/ext_scene/recall', json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        sent = [(m.type, m.channel, getattr(m, 'control', None), getattr(m, 'value', getattr(m, 'program', None)))
                for m in self.backend.outputs[FANTOM_NAME].sent]
        self.assertEqual(sent[:3], [('control_change', 4, 0, 1), ('control_change', 4, 32, 2),
                                    ('program_change', 4, None, 7)])

    def test_partial_apply_is_503_and_scene_dirty(self):
        self.fluid.fail_prefixes = ('select 5 ',)
        r = self.client.post('/api/scenes/legacy_minimal/recall', json={})
        self.assertEqual((r.status_code, self._code(r)), (503, 'PARTIAL_APPLY'))
        d = r.get_json()['error']['details']
        self.assertTrue(d['committed'])
        self.assertIn(5, d['failed_parts'])
        self.assertEqual(self.app.scene_state.public(), {'scene_id': 'legacy_minimal', 'name': 'Synthetic Minimal',
                                                         'dirty': True})


class SoundfontRouteTests(_AppCase):

    def test_load_errors_and_status(self):
        self.assertEqual(self.client.post('/api/soundfont/load', json={'file': 'X.sf2'}).status_code, 404)
        self.assertEqual(self.client.post('/api/soundfont/load', json={'file': '../x.sf2'}).status_code, 400)
        self._sf()
        self.app.daw.is_recording = True
        try:
            self.assertEqual(self._code(self.client.post('/api/soundfont/load', json={'file': 'Synthetic.sf2'})),
                             'TRANSPORT_ACTIVE')
        finally:
            self.app.daw.is_recording = False
        self.app.sf_state.begin('Other.sf2')
        self.assertEqual(self._code(self.client.post('/api/soundfont/load', json={'file': 'Synthetic.sf2'})),
                         'SOUNDFONT_LOADING')
        self.app.sf_state.finish('idle')
        self._load_sf()
        sf = self.client.get('/api/soundfont/status').get_json()['soundfont']
        self.assertEqual((sf['state'], sf['file'], sf['sf_id']), ('loaded', 'Synthetic.sf2', '2'))
        self.assertEqual(self.client.get('/api/system/status').get_json()['soundfont']['state'], 'loaded')
        self.assertEqual(self._state()['font'], 'Synthetic.sf2')
        self.assertEqual(self.app.sf_id, '2')
        self.assertEqual(self.client.get('/api/soundfonts').get_json()['files'], ['Synthetic.sf2'])

    def test_unverified_load_is_failed_and_font_key_untouched(self):
        self._sf('A.sf2')
        self.fluid.ignore_load = True                  # FluidSynth non lo carica davvero
        self.client.post('/api/soundfont/load', json={'file': 'A.sf2'})
        self.assertTrue(wait_until(lambda: self.app.sf_state.public()['state'] != 'loading'))
        self.assertEqual(self.app.sf_state.public()['state'], 'failed')
        self.assertFalse(os.path.exists(self.app.STATE_FILE) and self._state().get('font') == 'A.sf2')

    def test_unverified_load_still_tracks_active_sf_id(self):
        # `fonts` elenca un font (ID 2) ma con un percorso che non corrisponde:
        # stato failed, chiave font invariata, sf_id NON resta su un ID scaricato.
        self._sf('B.sf2')
        self.app.sf_id = '7'
        self.fluid.reported_path = '/altrove/Diverso.sf2'
        self.client.post('/api/soundfont/load', json={'file': 'B.sf2'})
        self.assertTrue(wait_until(lambda: self.app.sf_state.public()['state'] != 'loading'))
        self.assertEqual(self.app.sf_state.public()['state'], 'failed')
        self.assertEqual(self.app.sf_id, '2')
        self.assertFalse(os.path.exists(self.app.STATE_FILE) and self._state().get('font') == 'B.sf2')

    def test_legacy_load_sf2_route_updates_state(self):
        self._sf()
        r = self.client.get('/load_sf2/Synthetic.sf2')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith('Caricato:'))
        self.assertEqual(self.app.sf_state.public()['state'], 'loaded')


class ProjectRouteTests(_AppCase):

    def _fill(self):
        daw = self.app.daw
        daw.import_midi(_fixture_bytes('bank_program_cc.mid'))
        daw.add_note(2, 64, 90, 1.0, 0.5)
        daw.rename_track(2, 'Pad')
        daw.set_track_color(2, 11)
        daw.mute_track(2, True)
        daw.set_loop_points(0.0, 8.0)
        daw.toggle_loop()

    def _save_as(self, name='Prova'):
        r = self.client.post('/api/projects/save_as', json={'name': name})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()['project_state']['id']

    def test_save_as_new_open_round_trip(self):
        self._fill()
        self.assertTrue(self.app.project_state.public()['dirty'])
        pid = self._save_as()
        ps_pub = self.app.project_state.public()
        self.assertEqual((ps_pub['name'], ps_pub['dirty']), ('Prova', False))
        path = os.path.join(self.app.PROJECTS_DIR, pid + '.matsynth.json')
        doc = json.load(open(path, encoding='utf-8'))
        self.assertEqual(ps.validate_project(doc), [])
        daw = self.app.daw
        before = {ch: (list(daw.tracks[ch]), list(daw.event_clips[ch]), daw.get_clips(ch)) for ch in range(16)}
        names, colors, muted = dict(daw.track_names), dict(daw.track_colors), dict(daw.muted)
        loop = (daw.loop_enabled, daw.loop_start, daw.loop_end, daw.bpm)
        self.assertEqual(self.client.post('/api/projects/new', json={}).status_code, 200)
        self.assertEqual(daw.tracks[0], [])
        daw.arm_track(0, True)
        rev = daw.revision
        r = self.client.post('/api/projects/open', json={'id': pid, 'soundfont': 'keep_current'})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body['revision'], daw.revision)
        self.assertNotEqual(daw.revision, rev)
        for ch in range(16):
            self.assertEqual((list(daw.tracks[ch]), list(daw.event_clips[ch]), daw.get_clips(ch)), before[ch], ch)
        self.assertEqual((dict(daw.track_names), dict(daw.track_colors), dict(daw.muted)), (names, colors, muted))
        self.assertEqual((daw.loop_enabled, daw.loop_start, daw.loop_end, daw.bpm), loop)
        self.assertFalse(daw.is_playing or daw.is_recording or any(daw.armed.values()))
        self.assertFalse(daw.get_state()['can_undo'])
        self.assertEqual(self.app.project_state.public()['id'], pid)
        self.assertFalse(self.app.project_state.public()['dirty'])
        self.assertEqual(self.app.scene_state.public(), {'scene_id': None, 'name': None, 'dirty': False})

    def test_dirty_rules_and_unsaved_changes(self):
        pid = self._save_as()
        self.app.daw.arm_track(0, True)
        self.client.post('/api/daw/seek_beat', json={'beat': 2.0})
        self.assertFalse(self.app.project_state.public()['dirty'])     # arm/posizione non sporcano
        self.client.get('/cc/0/7/80')
        self.assertTrue(self.app.project_state.public()['dirty'])
        r = self.client.post('/api/projects/new', json={})
        self.assertEqual((r.status_code, self._code(r)), (409, 'UNSAVED_CHANGES'))
        r = self.client.post('/api/projects/open', json={'id': pid})
        self.assertEqual(self._code(r), 'UNSAVED_CHANGES')
        self.assertEqual(self.client.post('/api/projects/save', json={}).status_code, 200)
        self.assertFalse(self.app.project_state.public()['dirty'])
        self.client.post('/api/daw/bpm', json={'bpm': 99})
        self.assertTrue(self.app.project_state.public()['dirty'])
        self.assertEqual(self.client.post('/api/projects/new', json={'discard_changes': True}).status_code, 200)

    def test_save_errors_no_current_file_changed_and_disk(self):
        self.assertEqual(self._code(self.client.post('/api/projects/save', json={})), 'NO_CURRENT_FILE')
        pid = self._save_as()
        path = os.path.join(self.app.PROJECTS_DIR, pid + '.matsynth.json')
        with open(path, 'a') as f:
            f.write(' ')                               # modifica esterna
        r = self.client.post('/api/projects/save', json={})
        self.assertEqual((r.status_code, self._code(r)), (409, 'FILE_CHANGED'))
        self.assertEqual(self.client.post('/api/projects/save', json={'overwrite': True}).status_code, 200)
        with open(path, 'rb') as f:
            good = f.read()
        self.app.daw.add_note(0, 60, 100, 0.0, 1.0)
        with mock.patch.object(os, 'replace', side_effect=OSError(5, 'I/O')):
            r = self.client.post('/api/projects/save', json={})
        self.assertEqual((r.status_code, self._code(r)), (500, 'DISK_ERROR'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), good)               # file precedente intatto
        self.assertEqual([n for n in os.listdir(self.app.PROJECTS_DIR) if n.endswith('.tmp')], [])
        self.assertTrue(self.app.project_state.public()['dirty'])

    def test_invalid_future_and_corrupt_files_leave_session_intact(self):
        self._fill()
        pid = self._save_as()
        daw = self.app.daw
        path = os.path.join(self.app.PROJECTS_DIR, pid + '.matsynth.json')
        doc = json.load(open(path, encoding='utf-8'))
        cases = []
        future = dict(doc, schema_version=2)
        cases.append((future, 'UNSUPPORTED_VERSION', 'schema_version'))
        bad_ev = json.loads(json.dumps(doc))
        bad_ev['tracks'][0]['events'][1][1] = [240, 1, 247]
        cases.append((bad_ev, 'VALIDATION_FAILED', 'tracks[0].events[1][1]'))
        for i, (d, code, p) in enumerate(cases):
            ps.atomic_write_json(os.path.join(self.app.PROJECTS_DIR, f'p-bad{i}.matsynth.json'), d)
        with open(os.path.join(self.app.PROJECTS_DIR, 'p-corrupt.matsynth.json'), 'w') as f:
            f.write('{"format": "matsynth.project", ')
        daw.add_note(5, 70, 100, 0.0, 1.0)
        snapshot = ({ch: list(daw.tracks[ch]) for ch in range(16)}, daw.revision)
        for i, (_d, code, p) in enumerate(cases):
            r = self.client.post('/api/projects/open', json={'id': f'p-bad{i}', 'discard_changes': True})
            self.assertEqual((r.status_code, self._code(r)), (422, code))
            self.assertEqual(r.get_json()['error']['details']['path'], p)
        r = self.client.post('/api/projects/open', json={'id': 'p-corrupt', 'discard_changes': True})
        self.assertEqual((r.status_code, self._code(r)), (422, 'VALIDATION_FAILED'))
        self.assertEqual(self.client.post('/api/projects/open', json={'id': 'p-none'}).status_code, 404)
        self.assertEqual(({ch: list(daw.tracks[ch]) for ch in range(16)}, daw.revision), snapshot)
        self.assertEqual(self.app.project_state.public()['id'], pid)
        r = self.client.post('/api/projects/open', json={'id': 'p-bad0', 'discard_changes': True})
        self.assertEqual(r.get_json()['error']['details']['found'], 2)
        listing = {m['id']: m for m in self.client.get('/api/projects').get_json()['projects']}
        self.assertFalse(listing['p-bad0']['valid'])
        self.assertTrue(listing[pid]['valid'])

    def test_open_refused_while_playing_and_soundfont_mismatch(self):
        self._load_sf()
        pid = self._save_as()
        self.app.daw.is_playing = True
        try:
            r = self.client.post('/api/projects/open', json={'id': pid, 'discard_changes': True})
        finally:
            self.app.daw.is_playing = False
        self.assertEqual((r.status_code, self._code(r)), (409, 'TRANSPORT_ACTIVE'))
        self._load_sf('Other.sf2')
        r = self.client.post('/api/projects/open', json={'id': pid, 'discard_changes': True})
        self.assertEqual(self._code(r), 'SOUNDFONT_CHANGE_REQUIRED')
        r = self.client.post('/api/projects/open', json={'id': pid, 'discard_changes': True,
                                                         'soundfont': 'keep_current'})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_upload_download_patch_delete(self):
        pid = self._save_as('Base')
        path = os.path.join(self.app.PROJECTS_DIR, pid + '.matsynth.json')
        dl = self.client.get(f'/api/projects/{pid}/download')
        with open(path, 'rb') as f:
            self.assertEqual(dl.data, f.read())
        self.assertIn('Base.matsynth.json', dl.headers['Content-Disposition'])
        dl.close()          # Windows: il file aperto da send_file bloccherebbe os.replace
        data = {'file': (io.BytesIO(dl.data), 'x.matsynth.json')}
        r = self.client.post('/api/projects/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(self._code(r), 'NAME_EXISTS')
        data = {'file': (io.BytesIO(dl.data), 'x.matsynth.json'), 'name': 'Copia'}
        r = self.client.post('/api/projects/upload', data=data, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 201, r.get_json())
        new_id = r.get_json()['project_meta']['id']
        self.assertNotEqual(new_id, pid)
        self.assertEqual(self.app.project_state.public()['id'], pid)       # upload non apre
        big = {'file': (io.BytesIO(b' ' * (ps.MAX_PROJECT_BYTES + 1)), 'big.json')}
        r = self.client.post('/api/projects/upload', data=big, content_type='multipart/form-data')
        self.assertEqual((r.status_code, self._code(r)), (413, 'PAYLOAD_TOO_LARGE'))
        bad = {'file': (io.BytesIO(b'{"format":"matsynth.project","schema_version":1}'), 'b.json')}
        self.assertEqual(self.client.post('/api/projects/upload', data=bad,
                                          content_type='multipart/form-data').status_code, 422)
        r = self.client.patch(f'/api/projects/{pid}', json={'name': 'copia'})
        self.assertEqual(self._code(r), 'NAME_EXISTS')
        r = self.client.patch(f'/api/projects/{pid}', json={'name': 'Rinominato'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['project_meta']['name'], 'Rinominato')
        self.assertEqual(self.app.project_state.public()['name'], 'Rinominato')
        self.assertEqual(self.client.post('/api/projects/save', json={}).status_code, 200)   # stat aggiornato
        r = self.client.delete(f'/api/projects/{pid}')
        self.assertEqual(r.status_code, 200)
        st = r.get_json()['project_state']
        self.assertEqual((st['id'], st['dirty']), (None, True))


class MidiRouteTests(_AppCase):

    def _upload(self, **form):
        data = {'file': (io.BytesIO(_fixture_bytes('bank_program_cc.mid')), 'bank.mid')}
        data.update(form)
        return self.client.post('/api/daw/midi/upload', data=data, content_type='multipart/form-data')

    def test_m1_import_keeps_router_parts_and_channels_coherent(self):
        r = self._upload()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn('select 0 1 1 7', self.fluid.commands)
        self.assertEqual(self.app.router.get_part(0)['sound'], {'bank': 1, 'program': 7, 'name': None})
        st = self._state()
        self.assertEqual((st['parts'][0]['sound']['bank'], st['parts'][0]['sound']['program']), (1, 7))
        self.assertEqual((st['channels']['0']['bank'], st['channels']['0']['program']), (1, 7))
        # Slider dopo l'import: il flush debounced non deve riportare il vecchio programma.
        self.client.get('/cc/0/7/90')
        self.assertTrue(wait_until(lambda: self._state()['parts'][0]['cc']['7'] == 90, timeout=3))
        st = self._state()
        self.assertEqual((st['parts'][0]['sound']['program'], st['channels']['0']['program']), (7, 7))
        self.assertEqual(st['channels']['0']['volume'], 90)
        # Import sostitutivo: progetto staccato dal file (D12)
        pst = self.app.project_state.public()
        self.assertEqual((pst['id'], pst['name'], pst['dirty']), (None, 'bank', True))
        self.assertIn('revision', r.get_json())

    def test_a22_import_on_external_part_sends_nothing_to_fluidsynth(self):
        self.app.router.apply_part(_external_part(0, 6), send='none')
        del self.fluid.commands[:]
        self.assertEqual(self._upload().status_code, 200)
        self.assertFalse([c for c in self.fluid.commands if c.startswith('select 0 ')])
        out = [(m.type, m.channel, getattr(m, 'control', None), getattr(m, 'value', getattr(m, 'program', None)))
               for m in self.backend.outputs[FANTOM_NAME].sent]
        self.assertEqual(out[-3:], [('control_change', 6, 0, 1), ('control_change', 6, 32, 2),
                                    ('program_change', 6, None, 7)])
        self.assertEqual(self.app.router.get_part(0)['sound'],
                         {'bank_msb': 1, 'bank_lsb': 2, 'program': 7, 'name': None})

    def test_export_external_and_internal_banks(self):
        self.app.router.apply_part(_external_part(0, 6, sound={'bank_msb': 1, 'bank_lsb': 2, 'program': 7,
                                                               'name': 'Pad'}), send='none')
        p3 = midi_router.default_part(3)
        p3['sound']['bank'] = 128
        self.app.router.apply_part(p3, send='none')
        daw = self.app.daw
        daw.add_note(0, 60, 100, 0.0, 1.0)
        daw.add_note(3, 36, 100, 0.0, 1.0)
        r = self.client.post('/api/daw/midi/save', json={'name': 'exp'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual([w['code'] for w in r.get_json()['warnings']], ['BANK_NOT_REPRESENTABLE'])
        mid = mido.MidiFile(os.path.join(self.app.MIDI_DIR, 'exp.mid'))
        t0 = [m for m in mid.tracks[0] if not m.is_meta][:3]
        self.assertEqual([(m.type, getattr(m, 'control', None), getattr(m, 'value', getattr(m, 'program', None)))
                          for m in t0], [('control_change', 0, 1), ('control_change', 32, 2),
                                         ('program_change', None, 7)])
        # round trip import: stessi MSB/LSB/PC sulla parte esterna
        daw2_info = daw.import_midi(open(os.path.join(self.app.MIDI_DIR, 'exp.mid'), 'rb').read())
        self.assertEqual(daw2_info['channel_programs'][0], {'bank': 130, 'program': 7})
        t3 = [m for m in mid.tracks[1] if not m.is_meta]
        self.assertEqual(t3[0].type, 'program_change')          # bank 128 su canale 3: solo PC

    def test_import_unsaved_changes_and_conversion(self):
        self.app.project_state.mark_dirty()
        r = self._upload(discard_changes='false')
        self.assertEqual((r.status_code, self._code(r)), (409, 'UNSAVED_CHANGES'))
        self.assertEqual(self._upload(discard_changes='true').status_code, 200)
        mid = mido.MidiFile(type=1, ticks_per_beat=480)
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage('set_tempo', tempo=500000, time=0))
        tr.append(mido.MetaMessage('set_tempo', tempo=400000, time=480))
        tr.append(mido.Message('note_on', note=60, velocity=90, time=0))
        mid.tracks.append(tr)
        buf = io.BytesIO()
        mid.save(file=buf)
        with open(os.path.join(self.app.MIDI_DIR, 'tm.mid'), 'wb') as f:
            f.write(buf.getvalue())
        r = self.client.get('/api/daw/midi/load/tm.mid')
        self.assertEqual((r.status_code, self._code(r)), (422, 'IMPORT_NEEDS_CONVERSION'))
        r = self.client.get('/api/daw/midi/load/tm.mid?convert=1')
        self.assertEqual(r.status_code, 200)
        self.assertIn('IMPORT_CONVERTED', [w['code'] for w in r.get_json()['warnings']])


class DawRouteTests(_AppCase):

    def test_indexed_edit_with_stale_revision_is_409_and_changes_nothing(self):
        daw = self.app.daw
        r = self.client.post('/api/daw/track/0/add_note', json={'note': 60, 'start_beat': 0, 'length_beats': 1})
        rev = r.get_json()['revision']
        self.assertEqual(rev, daw.revision)
        ev = self.client.get('/api/daw/track/0/events').get_json()
        self.assertEqual(ev['revision'], rev)
        before = list(daw.tracks[0])
        r = self.client.put('/api/daw/track/0/events/0', json={'velocity': 5, 'expected_revision': rev - 1})
        self.assertEqual((r.status_code, self._code(r)), (409, 'REVISION_CONFLICT'))
        self.assertEqual(r.get_json()['error']['details'], {'expected_revision': rev - 1, 'current_revision': rev})
        self.assertEqual(daw.tracks[0], before)
        r = self.client.put('/api/daw/track/0/events/0', json={'velocity': 5, 'expected_revision': rev})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['revision'], rev + 1)
        self.assertEqual(self.client.put('/api/daw/track/0/events/0',
                                         json={'velocity': 5, 'expected_revision': 'x'}).status_code, 400)
        r = self.client.put('/api/daw/track/0/events/99', json={'velocity': 5})
        self.assertEqual((r.status_code, self._code(r)), (400, 'BAD_REQUEST'))
        nm = self.client.get('/api/daw/full_note_map').get_json()
        self.assertEqual(nm['revision'], daw.revision)
        self.assertIn('tracks', nm['map'])

    def test_rename_color_undo_and_bpm_envelopes(self):
        r = self.client.post('/api/daw/track/1/rename', json={'name': 'Basso'})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['revision'], self.app.daw.revision)
        self.assertEqual(self._code(self.client.post('/api/daw/track/1/rename', json={'name': ' '})), 'BAD_REQUEST')
        r = self.client.post('/api/daw/undo')
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(self.app.daw.track_names[1], 'Track 2')
        self.client.post('/api/daw/undo')
        r = self.client.post('/api/daw/undo')
        self.assertEqual((r.status_code, self._code(r)), (400, 'NOTHING_TO_UNDO'))
        self.app.daw.is_playing = True
        try:
            r = self.client.post('/api/daw/bpm', json={'bpm': 90})
        finally:
            self.app.daw.is_playing = False
        self.assertEqual((r.status_code, self._code(r)), (400, 'TRANSPORT_ACTIVE'))

    def test_m2_record_refusals_use_contract_http(self):
        daw = self.app.daw
        for ch in range(16):
            daw.arm_track(ch, False)
        p0 = midi_router.default_part(0)
        p0['input'] = {'device_alias': 'synth-b', 'channel': 0}          # ingresso non associato
        self.app.router.apply_part(p0, send='none')
        daw.arm_track(0, True)
        r = self.client.post('/api/daw/record/start', json={})
        self.assertEqual((r.status_code, self._code(r)), (503, 'PORT_UNAVAILABLE'))
        self.app.router.apply_part(midi_router.default_part(0), send='none')
        daw.add_note(1, 60, 100, 50.0, 1.0)
        self.assertTrue(daw.start_playback())
        try:
            r = self.client.post('/api/daw/record/start', json={})
            self.assertEqual((r.status_code, self._code(r)), (409, 'TRANSPORT_ACTIVE'))
        finally:
            daw.stop_all()
            daw.arm_track(0, False)


if __name__ == '__main__':
    unittest.main()
