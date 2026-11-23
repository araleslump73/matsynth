"""Test della fase `ui-routing` (docs/contracts.md sez. 3, 8.2, 10).

Router e DAW su FakeBackend (nessun rtmidi/hardware); route di app.py con
il router dell'app ricablato su un FakeBackend.
"""

import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import mido

import helpers
import routing_fakes
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
            # Entrambe le condizioni PRIMA dello stop: il note-off della nota a
            # cavallo del loop deve venire dalla ripartenza, non da stop_all().
            self.assertTrue(wait_until(lambda: (0, 123, 0) in _ccs(out) and ('note_off', 0, 60) in
                                       [(m.type, m.channel, m.note) for m in out.notes()], timeout=2))
            self.assertTrue(daw.is_playing)
        finally:
            daw.stop_all()

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


ON60 = mido.Message('note_on', channel=0, note=60, velocity=90)
OFF60 = mido.Message('note_off', channel=0, note=60, velocity=0)


class RouterConcurrencyTests(unittest.TestCase):
    """Fake con callback su thread proprio e close() con join (routing_fakes)."""

    def _router(self, **kw):
        router, be = routing_fakes.make_routing_router(**kw)
        self.addCleanup(routing_fakes.close_router, router, be)
        return router, be

    def test_m5_poll_closing_input_while_callback_send_fails_no_deadlock(self):
        # Interleaving di M5: la callback e' dentro un invio che fallira'; il
        # polling chiude l'input (close() = join del thread della callback).
        router, be = self._router(mode='managed')
        inp, out = be.inputs[FANTOM_NAME], be.outputs[FLUID_NAME]
        out.fail = True
        out.block_next()
        inp.feed(ON60)
        self.assertTrue(out.entered.wait(2), 'callback non arrivata in send()')
        del be.inputs[FANTOM_NAME]
        poll = threading.Thread(target=router.refresh_ports, daemon=True)
        poll.start()
        self.assertTrue(inp.close_started.wait(2), 'il polling non ha chiuso l input')
        out.release()                      # send fallisce -> gestione del fallimento
        poll.join(2.0)
        self.assertFalse(poll.is_alive(), 'deadlock polling/callback (M5)')
        self.assertFalse(inp.join_timed_out)
        self.assertTrue(wait_until(lambda: router.internal_status() == 'offline'))

    def test_m5_router_close_while_callback_send_fails_no_deadlock(self):
        router, be = self._router(mode='managed')
        inp, out = be.inputs[FANTOM_NAME], be.outputs[FLUID_NAME]
        out.fail = True
        out.block_next()
        inp.feed(ON60)
        self.assertTrue(out.entered.wait(2))
        closer = threading.Thread(target=router.close, daemon=True)
        closer.start()
        self.assertTrue(inp.close_started.wait(2))
        out.release()
        closer.join(2.0)
        self.assertFalse(closer.is_alive(), 'deadlock close()/callback (M5)')
        self.assertFalse(inp.join_timed_out)

    def test_m5_no_cfg_lock_held_during_port_io(self):
        # Ogni apertura/chiusura porta prova a prendere _cfg_lock da un altro
        # thread: se il router lo tenesse durante l'I/O, il tentativo scadrebbe.
        router, be = self._router(outputs=(FLUID_NAME, FANTOM_NAME), registry=_registry_with_fantom_out(),
                                  mode='managed')
        probes = []

        def probe(kind, name):
            got = []

            def try_lock():
                ok_ = router._cfg_lock.acquire(timeout=0.5)
                if ok_:
                    router._cfg_lock.release()
                got.append(ok_)
            th = threading.Thread(target=try_lock, daemon=True)
            th.start()
            th.join(2.0)
            probes.append((kind, bool(got and got[0])))
        be.io_probe = probe
        del be.inputs[FANTOM_NAME]
        del be.outputs[FANTOM_NAME]
        router.refresh_ports()
        be.add_input(FANTOM_NAME)
        be.add_output(FANTOM_NAME)
        router.refresh_ports()
        router.close()
        kinds = {k for k, _ in probes}
        self.assertTrue({'open_input', 'open_output', 'close_input', 'close_output'} <= kinds, probes)
        self.assertTrue(all(ok_ for _, ok_ in probes), probes)

    def test_m4_lost_input_releases_held_monitor_note_and_reports(self):
        events = []
        router, be = self._router(mode='managed', emit=lambda ev, p: events.append((ev, p)))
        inp, fluid = be.inputs[FANTOM_NAME], be.outputs[FLUID_NAME]
        inp.feed(mido.Message('control_change', channel=0, control=64, value=127), ON60)
        self.assertTrue(inp.wait_idle())
        self.assertIn(('note_on', 0, 60), [(m.type, m.channel, m.note) for m in fluid.notes()])
        del fluid.sent[:]
        del be.inputs[FANTOM_NAME]                 # cavo staccato con tasto e pedale tenuti
        router.refresh_ports()
        self.assertEqual([(m.type, m.channel, m.note) for m in fluid.notes()], [('note_off', 0, 60)])
        self.assertIn((0, 64, 0), _ccs(fluid))
        self.assertIn((0, 123, 0), _ccs(fluid))
        self.assertEqual(router._outs[FLUID_NAME].live_notes[60], 0)
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'missing')
        errors = [p for ev, p in events if ev == 'midi_error']
        self.assertEqual([(e['alias'], e['direction'], e['status']) for e in errors],
                         [('keyboard-main', 'input', 'missing')])
        # Record rifiutato da qui in poi (nessun falso "disponibile").
        self.assertEqual(router.record_preflight([0]),
                         [{'track': 0, 'alias': 'keyboard-main', 'status': 'missing'}])

    def test_l2_port_return_sends_cleanup_without_tracked_notes(self):
        events = []
        parts = [_external_part(t, 5) if t == 2 else midi_router.default_part(t) for t in range(16)]
        router, be = self._router(outputs=(FLUID_NAME, FANTOM_NAME), registry=_registry_with_fantom_out(),
                                  parts=parts, emit=lambda ev, p: events.append((ev, p)))
        del be.outputs[FANTOM_NAME]
        router.refresh_ports()
        self.assertEqual(router.port_status('fantom', 'output'), 'missing')
        back = be.add_output(FANTOM_NAME)
        router.refresh_ports()
        self.assertEqual(router.port_status('fantom', 'output'), 'available')
        # Cleanup (contratto 3.5) prima di qualunque altro invio, anche senza note tracciate.
        self.assertEqual([(m.channel, m.control, m.value) for m in back.sent], [(5, 64, 0), (5, 123, 0)])
        rec = router.describe_ports()['last_recovery']
        self.assertEqual([(r['port'], r['cleanup'], r['channels']) for r in rec], [(FANTOM_NAME, 'sent', [5])])
        payloads = [p for ev, p in events if ev == 'ports_status']
        self.assertEqual(payloads[-1]['last_recovery'][-1]['port'], FANTOM_NAME)

    def test_send_failure_offline_then_return_releases_tracked_note(self):
        events = []
        router, be = self._router(emit=lambda ev, p: events.append((ev, p)))
        fluid = be.outputs[FLUID_NAME]
        self.assertIsNotNone(router.send_track(0, ON60))
        fluid.fail = True
        self.assertIsNone(router.send_track(0, mido.Message('note_on', channel=0, note=62, velocity=90)))
        self.assertTrue(wait_until(lambda: router.internal_status() == 'offline'))
        self.assertIsNone(router.send_track(0, ON60))          # nessun fallback, nessun invio
        self.assertTrue(any(ev == 'midi_error' and p['direction'] == 'output' for ev, p in events))
        daw = MultiTrackDAW(router=router)
        daw.add_note(0, 60, 100, 0.0, 1.0)
        self.assertFalse(daw.start_playback())
        self.assertEqual(daw.last_error['code'], 'PORT_UNAVAILABLE')
        fluid.fail = False
        del fluid.sent[:]
        router.refresh_ports()                                  # porta di nuovo apribile
        self.assertEqual(router.internal_status(), 'available')
        self.assertEqual([(m.type, m.channel, m.note) for m in fluid.sent[:1]], [('note_off', 0, 60)])
        self.assertIn((0, 123, 0), _ccs(fluid))
        self.assertEqual(router.describe_ports()['last_recovery'][-1]['cleanup'], 'sent')

    def test_partial_panic_reports_failed_port_and_marks_it_offline(self):
        router, be = self._router(outputs=(FLUID_NAME, FANTOM_NAME), registry=_registry_with_fantom_out())
        be.outputs[FANTOM_NAME].fail = True
        report = {r['port']: r['delivery'] for r in router.panic()}
        self.assertEqual(report, {FLUID_NAME: 'sent', FANTOM_NAME: 'failed'})
        self.assertEqual(sorted(c for c, k, _ in _ccs(be.outputs[FLUID_NAME]) if k == 120), list(range(16)))
        self.assertTrue(wait_until(lambda: router.port_status('fantom', 'output') == 'offline'))
        self.assertEqual(router.internal_status(), 'available')

    def test_l1_route_and_channel_published_together(self):
        # Deterministico: dopo OGNI assegnamento di attributo del router durante
        # apply_part si fa un invio. Se porta e canale fossero pubblicati con due
        # assegnamenti, uno degli invii vedrebbe il canale nuovo con la porta
        # vecchia (FluidSynth ch5 / FANTOM ch0): nota sospesa (L1).
        class ProbingRouter(midi_router.MidiRouter):
            probe = None

            def __setattr__(self, name, value):
                object.__setattr__(self, name, value)
                if self.probe is not None and name != 'probe':
                    self.probe()

        be = helpers.FakeBackend(inputs=(FANTOM_NAME,), outputs=(FLUID_NAME, FANTOM_NAME))
        router = ProbingRouter(backend=be, registry=_registry_with_fantom_out(), sf_id=lambda: 1)
        router.start(poll=False, check_alsa=False)
        busy = []

        def probe():
            if busy:
                return
            busy.append(1)
            try:
                router.send_track(0, mido.Message('note_on', channel=0, note=64, velocity=90))
            finally:
                busy.pop()
        router.probe = probe
        router.apply_part(_external_part(0, 5), send='none')
        router.apply_part(midi_router.default_part(0), send='none')
        router.probe = None
        fluid, fantom = be.outputs[FLUID_NAME], be.outputs[FANTOM_NAME]
        self.assertTrue(fantom.notes(), 'la sonda non ha mai suonato sulla destinazione esterna')
        self.assertEqual({m.channel for m in fluid.sent}, {0})
        self.assertEqual({m.channel for m in fantom.sent}, {5})

    def _live_and_loop(self):
        """managed: l'utente tiene nota 60 e pedale su parte 0 (monitor ch0);
        il playback (traccia 0, stesso canale) ha una nota 62 a cavallo del loop."""
        router, be = self._router(mode='managed')
        inp, fluid = be.inputs[FANTOM_NAME], be.outputs[FLUID_NAME]
        inp.feed(mido.Message('control_change', channel=0, control=64, value=127), ON60)
        self.assertTrue(inp.wait_idle())
        daw = MultiTrackDAW(bpm=600, router=router)              # beat = 0.1 s
        daw.tracks[0] = [(0.0, [0x90, 62, 100]), (3.0, [0x80, 62, 0])]
        daw.has_data[0] = True
        daw.set_loop_points(0.0, 1.0)
        daw.toggle_loop()
        self.addCleanup(daw.stop_all)
        return router, be, inp, fluid, daw

    def test_m3_loop_and_stop_keep_live_note_and_pedal(self):
        router, be, inp, fluid, daw = self._live_and_loop()
        self.assertTrue(daw.start_playback())
        loop_off = lambda: [m for m in fluid.notes() if m.type == 'note_off' and m.note == 62]
        self.assertTrue(wait_until(lambda: len(loop_off()) >= 2, timeout=3), 'loop non ripartito')
        daw.stop_all()
        notes = [(m.type, m.channel, m.note) for m in fluid.notes()]
        self.assertNotIn(('note_off', 0, 60), notes)          # nota live non tagliata
        self.assertNotIn((0, 64, 0), _ccs(fluid))             # pedale live non azzerato
        self.assertNotIn((0, 123, 0), _ccs(fluid))            # nessun all-notes-off sul canale live
        # Il rilascio dell'utente arriva normalmente; poi il trasporto torna a ripulire tutto.
        inp.feed(OFF60, mido.Message('control_change', channel=0, control=64, value=0))
        self.assertTrue(inp.wait_idle())
        self.assertIn(('note_off', 0, 60), [(m.type, m.channel, m.note) for m in fluid.notes()])
        self.assertIn((0, 64, 0), _ccs(fluid))
        router.send_track(0, mido.Message('note_on', channel=0, note=70, velocity=90))
        router.release_all()
        self.assertIn((0, 123, 0), _ccs(fluid))

    def test_m3_panic_and_routing_change_still_release_live_notes(self):
        router, be, inp, fluid, daw = self._live_and_loop()
        router.panic()
        self.assertIn(('note_off', 0, 60), [(m.type, m.channel, m.note) for m in fluid.notes()])
        self.assertEqual(router._outs[FLUID_NAME].live_notes[60], 0)
        inp.feed(ON60)
        self.assertTrue(inp.wait_idle())
        del fluid.sent[:]
        part = router.get_part(0)
        part['monitor'] = False                                 # il note-off live non arrivera' piu'
        router.apply_part(part, send='none')
        self.assertEqual([(m.type, m.channel, m.note) for m in fluid.notes()], [('note_off', 0, 60)])


class BootstrapRegistryTests(unittest.TestCase):
    """H1: registro di bootstrap senza ingresso mai salvato; completato dal polling."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='matsynth_boot_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, 'midi_devices.json')

    def _saved_input(self):
        with open(self.file, encoding='utf-8') as f:
            return json.load(f)['devices'][0]['input']

    def test_no_keyboard_not_persisted_then_bound_when_it_appears(self):
        router, be = make_router(inputs=(), devices_file=self.file)
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'unbound')
        self.assertFalse(os.path.exists(self.file))
        router.refresh_ports()
        self.assertFalse(os.path.exists(self.file))
        be.add_input(FANTOM_NAME)
        router.refresh_ports()
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'available')
        self.assertEqual(self._saved_input(), FANTOM_DESC)
        daw = MultiTrackDAW(router=router)
        daw.arm_track(0, True)
        try:
            self.assertTrue(daw.start_recording())
        finally:
            daw.stop_all()

    def test_restart_without_file_bootstraps_again(self):
        make_router(inputs=(), devices_file=self.file)
        self.assertIsNone(midi_router.load_registry_file(self.file))
        router, _ = make_router(inputs=(FANTOM_NAME,), devices_file=self.file,
                                registry=midi_router.load_registry_file(self.file))
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'available')
        self.assertEqual(self._saved_input(), FANTOM_DESC)

    def test_input_enumeration_failure_at_start_not_persisted(self):
        be = routing_fakes.RoutingBackend((FANTOM_NAME,), (FLUID_NAME,))
        be.fail_enum = {'input'}
        router, _ = routing_fakes.make_routing_router(backend=be, devices_file=self.file)
        self.addCleanup(routing_fakes.close_router, router, be)
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'unbound')
        self.assertFalse(os.path.exists(self.file))
        router.refresh_ports()                     # ancora fallita: nulla cambia
        self.assertFalse(os.path.exists(self.file))
        be.fail_enum = set()
        router.refresh_ports()
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'available')
        self.assertEqual(self._saved_input(), FANTOM_DESC)

    def test_ambiguous_candidates_are_never_chosen(self):
        router, be = make_router(inputs=(), devices_file=self.file)
        be.add_input('USB A:USB A MIDI 1 20:0')
        be.add_input('USB B:USB B MIDI 1 24:0')
        router.refresh_ports()
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'unbound')
        self.assertFalse(os.path.exists(self.file))
        del be.inputs['USB B:USB B MIDI 1 24:0']        # ora il candidato e' unico
        router.refresh_ports()
        self.assertEqual(self._saved_input(), {'client': 'USB A', 'port': 'USB A MIDI 1'})

    def test_user_choice_is_not_overridden(self):
        router, be = make_router(inputs=(), devices_file=self.file)
        router.set_device('keyboard-main', {'input': None, 'label': 'Mia tastiera'})
        be.add_input(FANTOM_NAME)
        router.refresh_ports()
        self.assertEqual(router.port_status('keyboard-main', 'input'), 'unbound')
        self.assertIsNone(self._saved_input())


if __name__ == '__main__':
    unittest.main()
