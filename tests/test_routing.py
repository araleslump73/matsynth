"""Test della fase `ui-routing` (docs/contracts.md sez. 3, 8.2, 10).

Router e DAW su FakeBackend (nessun rtmidi/hardware); route di app.py con
il router dell'app ricablato su un FakeBackend.
"""

import copy
import json
import os
import shutil
import tempfile
import time
import unittest

import mido

import helpers
from helpers import FANTOM_NAME, FLUID_NAME, make_router, wait_until

import midi_router
from daw_recorder import MultiTrackDAW

FANTOM_DESC = {'client': 'FANTOM-08', 'port': 'FANTOM-08 MIDI 1'}


def _registry_with_fantom_out():
    return {'schema_version': 1, 'devices': [
        {'alias': 'keyboard-main', 'label': 'Tastiera', 'input': FANTOM_DESC,
         'output': None, 'controls': None},
        {'alias': 'fantom', 'label': 'FANTOM-08', 'input': FANTOM_DESC,
         'output': FANTOM_DESC, 'controls': ['7']}]}


def _external_part(t, channel, alias='fantom', monitor=False):
    p = midi_router.default_part(t)
    p['destination'] = {'kind': 'external', 'device_alias': alias, 'channel': channel}
    p['sound'] = midi_router.default_external_sound()
    p['cc'] = {}
    p['monitor'] = monitor
    return p


def _ext_router(mode='legacy', parts=None):
    return make_router(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME, FANTOM_NAME),
                       registry=_registry_with_fantom_out(), parts=parts, mode=mode)


def _ccs(port):
    return [(m.channel, m.control, m.value) for m in port.sent if m.type == 'control_change']


class PortNameTests(unittest.TestCase):

    def test_normalize_contract_examples(self):
        n = midi_router.normalize_port_name
        self.assertEqual(n('FANTOM-08:FANTOM-08 MIDI 1 20:0'), FANTOM_DESC)
        self.assertEqual(n('FLUID Synth (1234):Synth input port (1234:0) 128:0'),
                         {'client': 'FLUID Synth', 'port': 'Synth input port'})
        self.assertEqual(n('Solo nome'), {'client': 'Solo nome', 'port': ''})

    def test_bootstrap_unique_ambiguous_absent(self):
        boot = lambda names: midi_router.bootstrap_registry(names)['devices'][0]['input']
        self.assertEqual(boot(['Midi Through:Midi Through Port-0 14:0', FANTOM_NAME]), FANTOM_DESC)
        self.assertIsNone(boot(['USB A:USB A MIDI 1 20:0', 'USB B:USB B MIDI 1 24:0']))
        self.assertIsNone(boot(['Midi Through:Midi Through Port-0 14:0']))

    def test_internal_destination_requires_unique_fluid_client(self):
        r, _ = make_router(outputs=('Something 128:0',))
        self.assertEqual(r.internal_status(), 'missing')
        r, _ = make_router(outputs=(FLUID_NAME, 'FLUID Synth (999):Synth input port (999:0) 129:0'))
        self.assertEqual(r.internal_status(), 'ambiguous')
        self.assertFalse(r.internal_available())


class RoutingDawTests(unittest.TestCase):

    def test_playback_and_preview_go_to_external_destination_channel(self):
        router, be = _ext_router(parts=[_external_part(t, 5) if t == 2 else midi_router.default_part(t)
                                        for t in range(16)])
        daw = MultiTrackDAW(bpm=300, router=router)
        daw.add_note(2, 64, 100, 0.0, 0.25)
        self.assertTrue(daw.start_playback())
        self.assertTrue(wait_until(lambda: not daw.is_playing))
        fantom, fluid = be.outputs[FANTOM_NAME], be.outputs[FLUID_NAME]
        self.assertEqual([(m.type, m.channel, m.note) for m in fantom.notes()],
                         [('note_on', 5, 64), ('note_off', 5, 64)])
        self.assertEqual(fluid.notes(), [])
        del fantom.sent[:]
        self.assertTrue(router.preview_note(2, 60, 90, 10))
        self.assertTrue(wait_until(lambda: len(fantom.notes()) == 2))
        self.assertEqual([(m.type, m.channel) for m in fantom.notes()], [('note_on', 5), ('note_off', 5)])

    def test_managed_monitor_on_off_and_legacy_forwards_nothing(self):
        on = mido.Message('note_on', channel=0, note=60, velocity=90)
        router, be = make_router(mode='managed')
        be.inputs[FANTOM_NAME].feed(on)
        self.assertEqual([(m.type, m.channel) for m in be.outputs[FLUID_NAME].notes()], [('note_on', 0)])
        part = router.get_part(0)
        part['monitor'] = False
        router.apply_part(part, send='none')
        del be.outputs[FLUID_NAME].sent[:]
        be.inputs[FANTOM_NAME].feed(on)
        self.assertEqual(be.outputs[FLUID_NAME].sent, [])

        router, be = make_router(mode='legacy')
        be.inputs[FANTOM_NAME].feed(on)
        self.assertEqual(be.outputs[FLUID_NAME].sent, [])
        self.assertFalse(router.part_runtime(0)['monitor_effective'])

    def test_record_refused_before_is_recording_with_track_details(self):
        router, _ = make_router(inputs=())
        daw = MultiTrackDAW(router=router)
        daw.arm_track(0, True)
        self.assertFalse(daw.start_recording())
        self.assertFalse(daw.is_recording)
        self.assertEqual(daw.last_error['code'], 'PORT_UNAVAILABLE')
        self.assertIn({'track': 0, 'alias': 'keyboard-main', 'status': 'unbound'},
                      daw.last_error['details']['tracks'])

    def test_record_during_play_is_transport_active_409(self):
        router, _ = make_router()
        daw = MultiTrackDAW(bpm=60, router=router)
        daw.add_note(1, 60, 100, 0.0, 4.0)
        self.assertTrue(daw.start_playback())
        try:
            daw.arm_track(0, True)
            self.assertFalse(daw.start_recording(0))
            self.assertEqual(daw.last_error['code'], 'TRANSPORT_ACTIVE')   # 409 dalla tabella 2.3
            self.assertFalse(daw.is_recording)
        finally:
            daw.stop_all()

    def test_play_partial_destinations_warns(self):
        router, be = _ext_router(parts=[_external_part(t, 0, alias='missing-dev') if t == 1
                                        else midi_router.default_part(t) for t in range(16)])
        daw = MultiTrackDAW(bpm=300, router=router)
        daw.add_note(0, 60, 100, 0.0, 0.25)
        daw.add_note(1, 62, 100, 0.0, 0.25)
        self.assertTrue(daw.start_playback())
        wait_until(lambda: not daw.is_playing)
        codes = [w['code'] for w in daw.last_warnings]
        self.assertIn('PLAYBACK_TRACKS_UNAVAILABLE', codes)

    def _held_note_playing(self, channel=0):
        router, be = make_router()
        daw = MultiTrackDAW(bpm=60, router=router)
        daw.tracks[channel] = [(0.0, [0x90 | channel, 60, 100]), (0.0, [0xB0 | channel, 64, 127]),
                               (8.0, [0x80 | channel, 60, 0])]
        daw.has_data[channel] = True
        self.assertTrue(daw.start_playback())
        out = be.outputs[FLUID_NAME]
        self.assertTrue(wait_until(lambda: (channel, 64, 127) in _ccs(out)))
        return daw, router, out

    def _assert_released(self, out, channel=0):
        self.assertIn(('note_off', channel, 60), [(m.type, m.channel, m.note) for m in out.notes()])
        self.assertIn((channel, 64, 0), _ccs(out))
        self.assertIn((channel, 123, 0), _ccs(out))

    def test_stop_releases_held_note_and_sustain(self):
        daw, _, out = self._held_note_playing()
        daw.stop_playback()
        self._assert_released(out)

    def test_mute_during_playback_releases_immediately(self):
        daw, _, out = self._held_note_playing()
        try:
            daw.mute_track(0, True)
            self._assert_released(out)
        finally:
            daw.stop_all()

    def test_seek_during_playback_releases(self):
        daw, _, out = self._held_note_playing()
        try:
            self.assertTrue(daw.seek_to_beat(4.0))
            self._assert_released(out)
        finally:
            daw.stop_all()

    def test_loop_restart_releases(self):
        router, be = make_router()
        daw = MultiTrackDAW(bpm=600, router=router)     # beat = 0.1 s
        daw.tracks[0] = [(0.0, [0x90, 60, 100]), (0.5, [0x90, 62, 100]), (3.0, [0x80, 60, 0])]
        daw.has_data[0] = True
        daw.set_loop_points(0.0, 1.0)
        daw.toggle_loop()
        out = be.outputs[FLUID_NAME]
        self.assertTrue(daw.start_playback())
        try:
            self.assertTrue(wait_until(lambda: (0, 123, 0) in _ccs(out), timeout=2))
        finally:
            daw.stop_all()
        self.assertIn(('note_off', 0, 60), [(m.type, m.channel, m.note) for m in out.notes()])

    def test_routing_change_releases_on_old_destination(self):
        router, be = _ext_router()
        on = mido.Message('note_on', channel=0, note=60, velocity=90)
        self.assertIsNotNone(router.send_track(0, on))
        router.apply_part(_external_part(0, 3), send='none')
        fluid = be.outputs[FLUID_NAME]
        self.assertIn(('note_off', 0, 60), [(m.type, m.channel, m.note) for m in fluid.notes()])
        self.assertIn((0, 123, 0), _ccs(fluid))
        self.assertEqual(be.outputs[FANTOM_NAME].notes(), [])

    def test_panic_on_every_open_destination(self):
        router, be = _ext_router()
        router.send_track(0, mido.Message('note_on', channel=0, note=60, velocity=90))
        report = router.panic()
        self.assertEqual(sorted(r['port'] for r in report), sorted([FLUID_NAME, FANTOM_NAME]))
        for name in (FLUID_NAME, FANTOM_NAME):
            ccs = _ccs(be.outputs[name])
            for ctl in (64, 123, 120):
                self.assertEqual(sorted(c for c, k, _ in ccs if k == ctl), list(range(16)), (name, ctl))
        self.assertIn(('note_off', 0, 60), [(m.type, m.channel, m.note) for m in be.outputs[FLUID_NAME].notes()])

    def test_click_only_on_internal_channel_9(self):
        parts = [_external_part(t, t) for t in range(16)]
        router, be = _ext_router(parts=parts)
        daw = MultiTrackDAW(bpm=600, router=router)
        daw.add_note(0, 60, 100, 0.0, 4.0)
        daw.toggle_metronome()
        self.assertTrue(daw.start_playback())
        fluid = be.outputs[FLUID_NAME]
        try:
            self.assertTrue(wait_until(lambda: len(fluid.notes()) >= 2))
        finally:
            daw.stop_all()
        self.assertEqual({(m.channel, m.note in (76, 77)) for m in fluid.notes()}, {(9, True)})
        self.assertFalse([m for m in be.outputs[FANTOM_NAME].notes() if m.channel == 9 and m.note in (76, 77)])


class RoutingAppTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = helpers.import_app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='matsynth_routing_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.commands = helpers.redirect_app_paths(self.app, self.tmp)
        r = self.app.router
        r.close()                    # porte del test precedente: si riaprono sul nuovo backend
        self.backend = helpers.FakeBackend(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME, FANTOM_NAME))
        r.backend = self.backend
        with r._cfg_lock:
            r._devices = {d['alias']: d for d in _registry_with_fantom_out()['devices']}
        for t in range(16):
            r.apply_part(midi_router.default_part(t), send='none')
        r.refresh_ports()
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.app.daw.stop_all()

    def _patch(self, t, body):
        return self.client.patch(f'/api/parts/{t}', json=body)

    def test_set_effect_whitelist(self):
        self.assertEqual(self.client.get('/set_effect/synth.gain/1').status_code, 400)
        self.assertEqual(self.client.get('/set_effect/gain/0.5').status_code, 200)

    def test_parts_list_envelope(self):
        body = self.client.get('/api/parts').get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(len(body['parts']), 16)
        self.assertEqual(len(body['runtime']), 16)
        self.assertEqual(body['midi_mode']['effective'], 'legacy')
        self.assertEqual(self.client.get('/api/parts/16').status_code, 404)

    def test_patch_validation_failed_422(self):
        r = self._patch(0, {'destination': {'kind': 'internal', 'device_alias': None, 'channel': 99}})
        self.assertEqual(r.status_code, 422)
        body = r.get_json()
        self.assertEqual(body['error']['code'], 'VALIDATION_FAILED')
        self.assertTrue(body['error']['details']['path'].startswith('parts[0].'))
        self.assertEqual(self._patch(0, {'nope': 1}).status_code, 400)

    def test_patch_input_during_record_is_409(self):
        self.app.daw.is_recording = True
        try:
            r = self._patch(0, {'input': {'device_alias': 'fantom', 'channel': None}})
        finally:
            self.app.daw.is_recording = False
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['error']['code'], 'RECORDING_ACTIVE')

    def test_patch_external_unbound_is_skipped_and_persisted(self):
        self.client.put('/api/midi/devices/synth-b', json={'label': 'Synth B'})
        r = self._patch(3, {'destination': {'kind': 'external', 'device_alias': 'synth-b', 'channel': 2},
                            'sound': {'bank_msb': 1, 'bank_lsb': 0, 'program': 5, 'name': None}})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body['applied']['delivery'], 'skipped')
        self.assertEqual(body['runtime']['destination_status'], 'unbound')
        self.assertFalse(body['part']['monitor'])
        with open(self.app.STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        self.assertEqual(state['parts'][3]['destination']['device_alias'], 'synth-b')

    def test_patch_external_available_sends_bank_program(self):
        r = self._patch(4, {'destination': {'kind': 'external', 'device_alias': 'fantom', 'channel': 6},
                            'sound': {'bank_msb': 1, 'bank_lsb': 2, 'program': 7, 'name': None}})
        self.assertEqual(r.get_json()['applied']['delivery'], 'sent')
        sent = [(m.type, m.channel) for m in self.backend.outputs[FANTOM_NAME].sent]
        self.assertIn(('program_change', 6), sent)
        self.assertEqual(self.backend.outputs[FLUID_NAME].sent, [])

    def test_patch_internal_sound_uses_console(self):
        self.app.send_fluid = lambda cmd: (self.commands.append(cmd), 'OK')[1]
        r = self._patch(1, {'sound': {'bank': 0, 'program': 12, 'name': None}})
        self.assertEqual(r.get_json()['applied']['delivery'], 'sent')
        self.assertTrue(any(c.startswith('select 1 ') and c.endswith(' 0 12') for c in self.commands),
                        self.commands)

    def test_devices_crud_and_in_use(self):
        r = self.client.put('/api/midi/devices/ext-1', json={'output': {'from_port': FANTOM_NAME}})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['device']['status']['output'], 'available')
        self.assertEqual(self.client.put('/api/midi/devices/ext-1', json={'label': 'X'}).status_code, 200)
        self.assertEqual(self.client.put('/api/midi/devices/Bad_Alias', json={}).status_code, 400)
        self.assertEqual(self.client.delete('/api/midi/devices/keyboard-main').get_json()['error']['code'],
                         'DEVICE_IN_USE')
        self.assertEqual(self.client.delete('/api/midi/devices/ext-1').status_code, 200)
        self.assertEqual(self.client.delete('/api/midi/devices/ext-1').status_code, 404)

    def test_ports_and_system_endpoints(self):
        ports = self.client.get('/api/midi/ports').get_json()
        self.assertTrue(ports['ok'])
        self.assertEqual([p['name'] for p in ports['outputs'] if p['internal']], [FLUID_NAME])
        status = self.client.get('/api/system/status').get_json()
        self.assertTrue(status['ok'])
        self.assertEqual(status['synth']['midi_port'], 'available')
        self.assertEqual(status['midi_mode']['effective'], 'legacy')
        self.assertEqual(self.client.put('/api/system/mode', json={'mode': 'turbo'}).status_code, 400)
        body = self.client.put('/api/system/mode', json={'mode': 'managed'}).get_json()
        self.assertEqual(body['midi_mode'], {'effective': 'legacy', 'configured': 'managed',
                                             'restart_required': True})

    def test_daw_routes_envelope_and_refusals(self):
        daw = self.app.daw
        for ch in range(16):
            daw.arm_track(ch, False)
            daw.clear_track(ch)
        r = self.client.post('/api/daw/record/start', json={})
        self.assertEqual(r.get_json()['error']['code'], 'NO_ARMED_TRACK')
        daw.arm_track(0, True)
        r = self.client.post('/api/daw/play/start')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['error']['code'], 'NOTHING_TO_PLAY')
        r = self.client.post('/api/daw/panic').get_json()
        self.assertTrue(r['ok'])
        self.assertEqual(sorted(p['port'] for p in r['ports']), sorted([FLUID_NAME, FANTOM_NAME]))


if __name__ == '__main__':
    unittest.main()
