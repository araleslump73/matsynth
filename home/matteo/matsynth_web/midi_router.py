"""
midi_router.py - unico registro/dispatcher MIDI di MatSynth (fase `ui-routing`).

Contratto: docs/contracts.md sez. 3 (parti, porte, monitor, trasporto) e 10
(modalita' legacy/managed). Non e' un secondo motore DAW: tiene solo
registro porte, risoluzione delle destinazioni, invio serializzato,
tracciamento note e cleanup. La DAW (daw_recorder.py) gli passa playback,
click e cleanup; app.py gli passa CC/bank/program, preview e configurazione.

Scelte per il Raspberry Pi Zero 2 W (416 MiB, 4 core lenti):
- Lettura input con la callback di rtmidi (thread C gia' esistente in
  python-rtmidi): nessun thread Python di lettura e nessun polling
  `iter_pending()` a 1 ms, ne' dentro ne' fuori dal Record.
- Percorso caldo (playback/monitor) = due accessi a lista precalcolata + lock
  della porta; nessun dict/tupla creati per evento. Il tracciamento delle
  note usa bytearray preallocati (16x128 contatori per porta).
- Un solo thread ausiliario "deferred" avviato solo al primo uso (note-off
  della preview, coalescenza `input_activity`): dorme su Condition, zero
  risvegli quando non c'e' nulla in coda. Nessun thread per nota/traccia.
- Polling porte (solo enumerazione, nessun I/O MIDI) ogni 2 s solo se
  avviato esplicitamente con `start(poll=True)` (lo fa app.py, non la DAW
  autonoma ne' i test).
- Nessun lock di configurazione tenuto durante l'invio: si risolve la
  destinazione, poi si invia sotto il solo lock della porta. La console
  FluidSynth (TCP 9800) e' chiamata sempre fuori da qualunque lock.

Nessun inoltro di SysEx, clock o realtime; nessun invio di Local Control.
"""

import copy
import heapq
import json
import os
import re
import threading
import time

import mido

# ── Costanti di contratto (sez. 1.1, 3.1, 3.2, 3.4, 3.5, 10) ────────────

CC_KEYS = ('7', '10', '71', '72', '73', '74', '75')
DEFAULT_CC = {'7': 100, '10': 64, '71': 64, '72': 64, '73': 64, '74': 64, '75': 64}
CC_NAMES = {7: 'volume', 10: 'pan', 71: 'resonance', 72: 'release',
            73: 'attack', 74: 'cutoff', 75: 'decay'}
ALIAS_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
DEFAULT_INPUT_ALIAS = 'keyboard-main'
DEFAULT_INPUT_LABEL = 'Tastiera principale'
MODES = ('legacy', 'managed')
DEFAULT_MODE = 'legacy'
BOOTSTRAP_KEYWORDS = ('FANTOM', 'SINCO', 'USB')   # usate SOLO dal bootstrap
INTERNAL_CLIENT_PREFIX = 'FLUID Synth'
POLL_INTERVAL_S = 2.0
ACTIVITY_INTERVAL_S = 0.1                          # input_activity max 10 Hz

# Click: destinazione logica interna fissa (sez. 3.5)
CLICK_CHANNEL = 9
CLICK_ACCENT_NOTE = 76
CLICK_NOTE = 77
CLICK_VELOCITY = 100

STATUS_LABELS = {'available': 'disponibile', 'missing': 'assente',
                 'ambiguous': 'ambigua', 'unbound': 'non associato',
                 'offline': 'errore'}

# Solo messaggi di canale: tutto il resto (sysex, clock, start/stop,
# active sensing, song position...) non viene ne' inoltrato ne' registrato.
_CHANNEL_TYPES = frozenset(('note_on', 'note_off', 'polytouch', 'control_change',
                            'program_change', 'aftertouch', 'pitchwheel'))

_ALSA_SUFFIX_RE = re.compile(r'\s\d+:\d+$')
_PID_RE = re.compile(r'\s?\(\d+(:\d+)?\)')

LEGACY_MONITOR_MESSAGE = "Modalita' legacy: il monitor e' il collegamento ALSA diretto"


class RouterError(Exception):
    """Errore con codice di contratto (sez. 2.3), tradotto da app.py nella busta."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}


# ── Funzioni pure: nomi porta e registro (sez. 3.4) ─────────────────────

def normalize_port_name(name):
    """Descrittore persistibile da un nome runtime mido/rtmidi.

    1) toglie il suffisso ALSA `client:porta` numerico; 2) toglie i PID tra
    parentesi; 3) divide al primo ':' in client/porta. Unica funzione di
    normalizzazione del progetto (contratto 3.4).
    """
    s = _ALSA_SUFFIX_RE.sub('', str(name))
    s = _PID_RE.sub('', s)
    if ':' in s:
        client, port = s.split(':', 1)
        return {'client': client.strip(), 'port': port.strip()}
    return {'client': s.strip(), 'port': ''}


def _desc_key(desc):
    return (desc['client'], desc['port']) if desc else None


def port_exclusion(desc, direction):
    """Motivo per cui una porta non e' proposta normalmente (o None)."""
    client = desc['client']
    if client == 'System':
        return 'system'
    if client.upper().startswith('MIDI THROUGH'):
        return 'through'
    if client.startswith('RtMidi'):
        return 'own'
    if direction == 'input' and client.startswith(INTERNAL_CLIENT_PREFIX):
        return 'internal'
    return None


def is_internal_descriptor(desc):
    return bool(desc) and desc['client'].startswith(INTERNAL_CLIENT_PREFIX)


def bootstrap_registry(input_names):
    """Registro iniziale (solo se midi_devices.json non esiste).

    `keyboard-main` riceve il descrittore della porta che la vecchia selezione
    per parole chiave avrebbe scelto, SOLO se il candidato e' unico per la
    parola chiave vincente; altrimenti input null (stato `unbound`).
    """
    chosen = None
    for kw in BOOTSTRAP_KEYWORDS:
        cands = [n for n in input_names
                 if kw in str(n).upper()
                 and port_exclusion(normalize_port_name(n), 'input') is None]
        if cands:
            if len(cands) == 1:
                chosen = normalize_port_name(cands[0])
            break
    return {'schema_version': 1, 'devices': [{
        'alias': DEFAULT_INPUT_ALIAS, 'label': DEFAULT_INPUT_LABEL,
        'input': chosen, 'output': None, 'controls': None}]}


def _validate_descriptor(d, path):
    if d is None:
        return
    if not isinstance(d, dict) or set(d) != {'client', 'port'} \
            or not isinstance(d['client'], str) or not isinstance(d['port'], str) \
            or not d['client'] or len(d['client']) > 128 or len(d['port']) > 128:
        raise RouterError('VALIDATION_FAILED', 'Descrittore porta non valido',
                          {'path': path, 'reason': '{client, port} stringhe attese'})


def _validate_controls(c, path):
    if c is None:
        return
    if not isinstance(c, list) or any(k not in CC_KEYS for k in c):
        raise RouterError('VALIDATION_FAILED', 'Controlli non validi',
                          {'path': path, 'reason': 'lista di chiavi CC di sez. 3.1 o null'})


def validate_registry(doc):
    """Valida il documento midi_devices.json (schema docs/schemas/midi_devices.json)."""
    if not isinstance(doc, dict) or set(doc) != {'schema_version', 'devices'}:
        raise RouterError('VALIDATION_FAILED', 'Registro dispositivi non valido',
                          {'path': '', 'reason': 'chiavi schema_version/devices attese'})
    if doc['schema_version'] != 1:
        raise RouterError('UNSUPPORTED_VERSION', 'Versione registro non supportata',
                          {'found': doc['schema_version'], 'supported': 1})
    if not isinstance(doc['devices'], list):
        raise RouterError('VALIDATION_FAILED', 'Registro dispositivi non valido',
                          {'path': 'devices', 'reason': 'array atteso'})
    seen = set()
    for i, d in enumerate(doc['devices']):
        p = f'devices[{i}]'
        if not isinstance(d, dict) or set(d) != {'alias', 'label', 'input', 'output', 'controls'}:
            raise RouterError('VALIDATION_FAILED', 'Dispositivo non valido',
                              {'path': p, 'reason': 'chiavi alias/label/input/output/controls attese'})
        if not isinstance(d['alias'], str) or not ALIAS_RE.match(d['alias']) or d['alias'] in seen:
            raise RouterError('VALIDATION_FAILED', 'Alias non valido o duplicato',
                              {'path': p + '.alias', 'reason': 'alias unico, regex 1.1'})
        seen.add(d['alias'])
        if not isinstance(d['label'], str) or not 1 <= len(d['label']) <= 64:
            raise RouterError('VALIDATION_FAILED', 'Etichetta non valida',
                              {'path': p + '.label', 'reason': 'stringa 1..64'})
        _validate_descriptor(d['input'], p + '.input')
        _validate_descriptor(d['output'], p + '.output')
        _validate_controls(d['controls'], p + '.controls')
    return doc


def atomic_write_json(path, obj):
    """Scrittura atomica (contratto 5.5): temporaneo unico nella stessa
    cartella, fsync, os.replace. Il file precedente resta intatto su errore.
    NOTA per ui-persistence: da consolidare in project_store.atomic_write_json.
    """
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, f'.{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if hasattr(os, 'O_DIRECTORY'):          # fsync della directory solo su POSIX
            try:
                fd = os.open(d, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_registry_file(path):
    """Legge midi_devices.json. Ritorna il documento valido o None se assente.
    File corrotto/non valido: rinominato `.corrupt_<ts>` (come get_last_state)
    e None, cosi' il bootstrap riparte senza perdere il file originale."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return validate_registry(json.load(f))
    except Exception as e:
        try:
            bad = f'{path}.corrupt_{int(time.time())}'
            os.rename(path, bad)
            print(f'[Router] Registro dispositivi non valido ({e}): spostato in {bad}')
        except OSError as e2:
            print(f'[Router] Registro dispositivi non valido e non rinominabile: {e2}')
        return None


# ── Funzioni pure: parti (sez. 3.1-3.3, 3.6) ───────────────────────────

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _vfail(path, reason):
    raise RouterError('VALIDATION_FAILED', f'Parte non valida: {path} ({reason})',
                      {'path': path, 'reason': reason})


def _vjoin(path, key):
    return f'{path}.{key}' if path else str(key)


def _vkeys(obj, path, required):
    if not isinstance(obj, dict):
        _vfail(path or '.', 'oggetto atteso')
    for k in required:
        if k not in obj:
            _vfail(_vjoin(path, k), 'campo obbligatorio mancante')
    for k in obj:
        if k not in required:
            _vfail(_vjoin(path, k), 'campo non previsto')


def _vint(v, lo, hi, path, nullable=False):
    if v is None and nullable:
        return
    if not _is_int(v) or not lo <= v <= hi:
        _vfail(path, f'intero {lo}..{hi}' + (' o null' if nullable else '') + ' atteso')


def validate_part(p, index=None):
    """Valida una parte secondo sez. 3.1 (stesso esito di tests/contract_reference.py)."""
    _vkeys(p, '', ('part_id', 'input', 'destination', 'monitor', 'sound', 'cc'))
    _vint(p['part_id'], 0, 15, 'part_id')
    if index is not None and p['part_id'] != index:
        _vfail('part_id', "deve essere uguale all'indice")
    _vkeys(p['input'], 'input', ('device_alias', 'channel'))
    a = p['input']['device_alias']
    if a is not None and (not isinstance(a, str) or not ALIAS_RE.match(a)):
        _vfail('input.device_alias', 'alias non valido')
    _vint(p['input']['channel'], 0, 15, 'input.channel', nullable=True)
    d = p['destination']
    _vkeys(d, 'destination', ('kind', 'device_alias', 'channel'))
    if d['kind'] not in ('internal', 'external'):
        _vfail('destination.kind', 'internal o external atteso')
    if d['kind'] == 'internal' and d['device_alias'] is not None:
        _vfail('destination.device_alias', 'null per destinazione interna')
    if d['kind'] == 'external' and (not isinstance(d['device_alias'], str)
                                    or not ALIAS_RE.match(d['device_alias'])):
        _vfail('destination.device_alias', 'alias obbligatorio per destinazione esterna')
    _vint(d['channel'], 0, 15, 'destination.channel')
    if not isinstance(p['monitor'], bool):
        _vfail('monitor', 'bool atteso')
    s = p['sound']
    if d['kind'] == 'internal':
        _vkeys(s, 'sound', ('bank', 'program', 'name'))
        _vint(s['bank'], 0, 16383, 'sound.bank')
        _vint(s['program'], 0, 127, 'sound.program')
    else:
        _vkeys(s, 'sound', ('bank_msb', 'bank_lsb', 'program', 'name'))
        for k in ('bank_msb', 'bank_lsb', 'program'):
            _vint(s[k], 0, 127, 'sound.' + k, nullable=True)
    if s['name'] is not None and (not isinstance(s['name'], str) or len(s['name']) > 64):
        _vfail('sound.name', 'stringa 0..64 caratteri attesa')
    cc = p['cc']
    if not isinstance(cc, dict):
        _vfail('cc', 'oggetto atteso')
    for k, v in cc.items():
        if k not in CC_KEYS:
            _vfail('cc.' + str(k), 'CC non previsto')
        _vint(v, 0, 127, 'cc.' + k)
    if d['kind'] == 'internal':
        for k in CC_KEYS:
            if k not in cc:
                _vfail('cc.' + k, 'obbligatorio per parte interna')
    return p


def default_internal_sound(channel):
    return {'bank': 128 if channel == 9 else 0, 'program': 0, 'name': None}


def default_external_sound():
    return {'bank_msb': None, 'bank_lsb': None, 'program': None, 'name': None}


def default_part(n):
    """Parte di default (sez. 3.2): input canale N -> FluidSynth canale N."""
    return {'part_id': n,
            'input': {'device_alias': DEFAULT_INPUT_ALIAS, 'channel': n},
            'destination': {'kind': 'internal', 'device_alias': None, 'channel': n},
            'monitor': True,
            'sound': default_internal_sound(n),
            'cc': dict(DEFAULT_CC)}


def default_parts():
    return [default_part(n) for n in range(16)]


_LEGACY_CC_FIELDS = (('volume', '7'), ('pan', '10'), ('resonance', '71'), ('release', '72'),
                     ('attack', '73'), ('cutoff', '74'), ('decay', '75'))


def part_from_legacy_channel(n, ch_data):
    """Parte interna da una voce legacy `channels[n]` (mappa di sez. 4.3).
    Campi assenti o non validi = default (lo stato non deve impedire l'avvio)."""
    p = default_part(n)
    if not isinstance(ch_data, dict):
        return p
    for key, lo, hi in (('bank', 0, 16383), ('program', 0, 127)):
        v = ch_data.get(key)
        if _is_int(v) and lo <= v <= hi:
            p['sound'][key] = v
    name = ch_data.get('instrument_name')
    if isinstance(name, str) and name and name != 'Not Set':
        p['sound']['name'] = name[:64]
    for field, key in _LEGACY_CC_FIELDS:
        v = ch_data.get(field)
        if _is_int(v) and 0 <= v <= 127:
            p['cc'][key] = v
    return p


def parts_from_state(state):
    """Parti correnti da last_state.json (sez. 3.3): `parts` se valida,
    altrimenti costruite da `channels` (default per i campi assenti)."""
    raw = state.get('parts') if isinstance(state, dict) else None
    if isinstance(raw, list) and len(raw) == 16:
        try:
            for i, p in enumerate(raw):
                validate_part(p, i)
            return copy.deepcopy(raw), 'parts'
        except RouterError as e:
            print(f"[Router] 'parts' in stato non valida ({e.message}): ricostruite da channels")
    channels = state.get('channels', {}) if isinstance(state, dict) else {}
    if not isinstance(channels, dict):
        channels = {}
    parts = []
    for n in range(16):
        ch_data = channels.get(str(n))
        parts.append(part_from_legacy_channel(n, ch_data) if ch_data is not None else default_part(n))
    return parts, 'channels'


def legacy_channel_entry(part):
    """Voce legacy `channels[part_id]` per una parte interna (sez. 3.3)."""
    entry = {'bank': part['sound']['bank'], 'program': part['sound']['program']}
    for field, key in _LEGACY_CC_FIELDS:
        entry[field] = part['cc'][key]
    return entry


def merge_part_patch(current, patch):
    """Applica un PATCH (sez. 8.2) a una parte e ritorna la nuova parte validata.

    input/destination/sound sostituiscono il sotto-oggetto, cc fonde le chiavi;
    cambio di `kind` con le regole di default di sez. 3.2.
    """
    if not isinstance(patch, dict):
        raise RouterError('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''})
    for k in patch:
        if k not in ('input', 'destination', 'monitor', 'sound', 'cc'):
            raise RouterError('BAD_REQUEST', f'Campo non modificabile: {k}', {'field': k})
    new = copy.deepcopy(current)
    cc_base = new['cc']
    if 'input' in patch:
        new['input'] = copy.deepcopy(patch['input'])
    if 'destination' in patch:
        dest = copy.deepcopy(patch['destination'])
        new['destination'] = dest
        old_kind = current['destination']['kind']
        new_kind = dest.get('kind') if isinstance(dest, dict) else None
        if new_kind != old_kind and new_kind in ('internal', 'external'):
            if new_kind == 'external':
                new['sound'] = default_external_sound()
                cc_base = {}
                if 'monitor' not in patch:
                    new['monitor'] = False      # esterno: monitor inizialmente spento
            else:
                ch = dest.get('channel')
                new['sound'] = default_internal_sound(ch if _is_int(ch) else -1)
                cc_base = dict(DEFAULT_CC)
            new['cc'] = cc_base
    if 'monitor' in patch:
        new['monitor'] = patch['monitor']
    if 'sound' in patch:
        new['sound'] = copy.deepcopy(patch['sound'])
    if 'cc' in patch:
        if not isinstance(patch['cc'], dict):
            _vfail('cc', 'oggetto atteso')
        merged = dict(cc_base)
        merged.update(patch['cc'])
        new['cc'] = merged
    validate_part(new, current['part_id'])
    return new


def compute_conflicts(parts):
    """Conflitti di sez. 3.6 (avvisi: il backend non li risolve)."""
    out = []
    by_dest = {}
    for p in parts:
        d = p['destination']
        by_dest.setdefault((d['kind'], d['device_alias'], d['channel']), []).append(p['part_id'])
    for (kind, alias, ch), ids in sorted(by_dest.items(), key=lambda kv: kv[1][0]):
        if len(ids) > 1:
            out.append({'type': 'shared_destination', 'parts': ids,
                        'destination': {'kind': kind, 'device_alias': alias, 'channel': ch}})
    groups = {}                  # (alias, parti) -> canali MIDI condivisi
    for alias in sorted({p['input']['device_alias'] for p in parts
                         if p['input']['device_alias'] is not None}):
        members = [p for p in parts if p['input']['device_alias'] == alias]
        for ch in range(16):
            ids = tuple(p['part_id'] for p in members
                        if p['input']['channel'] is None or p['input']['channel'] == ch)
            if len(ids) > 1:
                groups.setdefault((alias, ids), []).append(ch)
    for (alias, ids), chans in groups.items():
        out.append({'type': 'shared_input', 'parts': list(ids),
                    'input': {'device_alias': alias, 'channels': chans}})
        mon = [i for i in ids if parts[i]['monitor']]
        by_d = {}
        for i in mon:
            d = parts[i]['destination']
            by_d.setdefault((d['kind'], d['device_alias'], d['channel']), []).append(i)
        for (kind, dal, dch), dup in by_d.items():
            if len(dup) > 1:
                out.append({'type': 'double_monitor', 'parts': dup,
                            'destination': {'kind': kind, 'device_alias': dal, 'channel': dch}})
    return out


def external_sound_messages(part, include_sound=True, cc_keys=None):
    """[(etichetta, mido.Message)] per una parte esterna: CC0, CC32, PC, poi CC
    presenti, sempre sul canale di destinazione (sez. 4.5 punto 3)."""
    ch = part['destination']['channel']
    s = part['sound']
    msgs = []
    if include_sound:
        if s.get('bank_msb') is not None:
            msgs.append(('bank_msb', mido.Message('control_change', channel=ch, control=0, value=s['bank_msb'])))
        if s.get('bank_lsb') is not None:
            msgs.append(('bank_lsb', mido.Message('control_change', channel=ch, control=32, value=s['bank_lsb'])))
        if s.get('program') is not None:
            msgs.append(('program', mido.Message('program_change', channel=ch, program=s['program'])))
    for k in (CC_KEYS if cc_keys is None else [k for k in CC_KEYS if k in cc_keys]):
        if k in part['cc']:
            msgs.append(('cc' + k, mido.Message('control_change', channel=ch, control=int(k),
                                                value=part['cc'][k])))
    return msgs


def internal_console_commands(part, sf_id, include_sound=True, cc_keys=None):
    """[(etichetta, comando console)] per una parte interna. Via unica scelta
    (D20): bank/program/CC interni passano dalla console 9800 come oggi
    (`select` usa il numero di bank FluidSynth, non esprimibile con CC0/CC32)."""
    ch = part['destination']['channel']
    cmds = []
    if include_sound:
        cmds.append(('program', f"select {ch} {sf_id} {part['sound']['bank']} {part['sound']['program']}"))
    for k in (CC_KEYS if cc_keys is None else [k for k in CC_KEYS if k in cc_keys]):
        if k in part['cc']:
            cmds.append(('cc' + k, f"cc {ch} {k} {part['cc'][k]}"))
    return cmds


# ── Modalita' legacy/managed (sez. 10) ─────────────────────────────────

def resolve_effective_mode(env_value, state):
    """Modalita' effettiva, letta una volta all'avvio: MATSYNTH_MIDI_MODE
    (cio' che startfluid.sh ha configurato), altrimenti `midi_mode` dello
    stato, altrimenti legacy. Valori non riconosciuti -> legacy."""
    if env_value is not None and env_value != '':
        return env_value if env_value in MODES else DEFAULT_MODE
    return configured_mode(state)


def configured_mode(state):
    v = state.get('midi_mode') if isinstance(state, dict) else None
    return v if v in MODES else DEFAULT_MODE


def parse_aconnect_list(text):
    """Connessioni da `aconnect -l` (sola lettura): [(client_sorgente, client_destinazione)].
    Formato ALSA da verificare su hardware."""
    clients = {}
    conns = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^client (\d+): '([^']*)'", line)
        if m:
            cur = int(m.group(1))
            clients[cur] = m.group(2)
            continue
        m = re.match(r'^\s+Connecting To:\s*(.+)$', line)
        if m and cur is not None:
            for part in m.group(1).split(','):
                mm = re.match(r'\s*(\d+):\d+', part)
                if mm:
                    conns.append((cur, int(mm.group(1))))
    return [(clients.get(a, str(a)), clients.get(b, str(b))) for a, b in conns]


def direct_alsa_to_fluid(text, input_clients):
    """Client di input (descrittori) gia' collegati via ALSA a FluidSynth."""
    found = []
    for src, dst in parse_aconnect_list(text):
        src_n = normalize_port_name(src)['client']
        if dst.startswith(INTERNAL_CLIENT_PREFIX) and src_n in input_clients and src_n not in found:
            found.append(src_n)
    return found


# ── Backend porte (sostituibile nei test) ──────────────────────────────

class MidoBackend:
    """Accesso alle porte tramite il modulo mido, risolto a ogni chiamata
    (cosi' tests/helpers.make_daw puo' intercettare le quattro funzioni)."""

    def get_input_names(self):
        return mido.get_input_names()

    def get_output_names(self):
        return mido.get_output_names()

    def open_input(self, name, callback):
        return mido.open_input(name, callback=callback)

    def open_output(self, name):
        return mido.open_output(name)


# ── Stato runtime delle porte ──────────────────────────────────────────

class _OutPort:
    """Porta di uscita aperta, condivisa da tutte le parti che la usano.
    Contatori note/sustain preallocati: tracciamento senza allocazioni."""
    __slots__ = ('name', 'desc', 'port', 'lock', 'notes', 'touched', 'sustain')

    def __init__(self, name, desc, port):
        self.name = name
        self.desc = desc
        self.port = port
        self.lock = threading.Lock()          # serializza gli invii su questa porta
        self.notes = bytearray(16 * 128)      # note-on attivi per (canale, nota)
        self.touched = bytearray(16)          # canali su cui MatSynth ha suonato
        self.sustain = bytearray(16)          # CC64 >= 64 inviato e non rilasciato


class _InPort:
    __slots__ = ('name', 'desc', 'port', 'aliases', 'match', 'mon', 'closed')

    def __init__(self, name, desc):
        self.name = name
        self.desc = desc
        self.port = None
        self.aliases = set()
        self.match = ((),) * 16      # canale -> parti il cui input corrisponde
        self.mon = ((),) * 16        # canale -> parti con monitor efficace (solo managed)
        self.closed = False


class _Deferred:
    """Unico thread ausiliario, avviato al primo uso: esegue funzioni a una
    scadenza (note-off della preview, coalescenza degli eventi). Dorme su
    Condition: nessun risveglio periodico."""

    def __init__(self):
        self._cv = threading.Condition()
        self._heap = []
        self._seq = 0
        self._keys = set()
        self._thread = None

    def call_later(self, delay, fn, key=None):
        with self._cv:
            if key is not None:
                if key in self._keys:
                    return False
                self._keys.add(key)
            self._seq += 1
            heapq.heappush(self._heap, (time.monotonic() + delay, self._seq, fn, key))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='midi-deferred', daemon=True)
                self._thread.start()
            self._cv.notify()
        return True

    def _run(self):
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                due = self._heap[0][0]
                now = time.monotonic()
                if due > now:
                    self._cv.wait(due - now)
                    continue
                _, _, fn, key = heapq.heappop(self._heap)
                if key is not None:
                    self._keys.discard(key)
            try:
                fn()
            except Exception as e:
                print(f'[Router] Errore funzione differita: {e}')


# ── Router ─────────────────────────────────────────────────────────────

class MidiRouter:
    """Registro porte + risoluzione destinazioni + invio serializzato.

    Costruito senza argomenti e' il router "in memoria" della DAW autonoma
    (registro bootstrap in RAM, parti di default, legacy, nessun disco,
    nessun thread di polling).
    """

    def __init__(self, backend=None, parts=None, registry=None, mode=DEFAULT_MODE,
                 devices_file=None, emit=None, console=None, sf_id=None):
        self.backend = backend or MidoBackend()
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.devices_file = devices_file
        self._emit_fn = emit
        self._console = console            # callable(cmd) -> str ("" = errore socket)
        self._sf_id = sf_id                # callable() -> id SoundFont attivo
        self._cfg_lock = threading.RLock()
        self._parts = copy.deepcopy(parts) if parts is not None else default_parts()
        for i, p in enumerate(self._parts):
            validate_part(p, i)
        self._devices = None
        if registry is not None:
            validate_registry(registry)
            self._devices = {d['alias']: copy.deepcopy(d) for d in registry['devices']}
        self._in_names = []
        self._out_names = []
        self._outs = {}                    # nome runtime -> _OutPort aperta
        self._ins = {}                     # nome runtime -> _InPort aperta
        self._dead_outs = {}               # descrittore -> _OutPort persa (note da ripulire al ritorno)
        self._failed = set()               # (nome, direzione) apertura/invio falliti -> offline
        self._selection = {}               # (alias, direzione) -> nome scelto tra ambigui
        self._status = {}                  # (alias, direzione) -> stato
        self._bound = {}                   # (alias, direzione) -> nome runtime se available
        self._candidates = {}              # (alias, direzione) -> nomi se ambiguous
        self._internal_status = 'missing'
        self._internal_name = None
        self._internal_candidates = []
        self._track_out = [None] * 16      # parte -> _OutPort (percorso caldo)
        self._track_ch = list(range(16))   # parte -> canale di destinazione
        self._recorder = None              # callable(parti, msg, t) della DAW
        self._activity = bytearray(16)
        self._deferred = _Deferred()
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self._last_signature = None
        self.started = False
        self.last_recovery = []
        self.startup_warnings = []
        self._rebuild_tables()

    # ── ciclo di vita ──────────────────────────────────────────────────

    def start(self, poll=True, check_alsa=True):
        """Enumera e apre le porte; bootstrap del registro se assente; avvia
        il polling solo se richiesto (app.py). Mai eccezioni verso il chiamante."""
        try:
            names_in, names_out = self._enumerate()
            if self._devices is None:
                doc = bootstrap_registry(names_in or [])
                with self._cfg_lock:
                    self._devices = {d['alias']: d for d in doc['devices']}
                try:
                    self._save_registry()
                except Exception as e:
                    # Registro valido in RAM anche se il disco fallisce: si riprova al prossimo PUT.
                    print(f'[Router] Salvataggio registro bootstrap fallito: {e}')
            self._apply_enumeration(names_in or [], names_out or [])
            self.started = True
        except Exception as e:
            print(f'[Router] Errore avvio: {e}')
        if self.mode == 'managed' and check_alsa:
            self._check_direct_alsa()
        if poll and self._poll_thread is None:
            self._poll_thread = threading.Thread(target=self._poll_loop, name='midi-ports-poll', daemon=True)
            self._poll_thread.start()

    def close(self):
        self._poll_stop.set()
        with self._cfg_lock:
            for ip in list(self._ins.values()):
                self._close_input(ip)
            self._ins.clear()
            for op in list(self._outs.values()):
                try:
                    op.port.close()
                except Exception:
                    pass
                op.port = None
            self._outs.clear()
            self._rebuild_tables()

    def _poll_loop(self):
        # Solo enumerazione ogni 2 s: nessun I/O MIDI, nessuna emissione se nulla cambia.
        while not self._poll_stop.wait(POLL_INTERVAL_S):
            try:
                self.refresh_ports()
            except Exception as e:
                print(f'[Router] Errore polling porte: {e}')

    def _enumerate(self):
        try:
            names_in = list(self.backend.get_input_names())
        except Exception as e:
            print(f'[Router] Enumerazione input fallita: {e}')
            names_in = None
        try:
            names_out = list(self.backend.get_output_names())
        except Exception as e:
            print(f'[Router] Enumerazione output fallita: {e}')
            names_out = None
        return names_in, names_out

    def refresh_ports(self):
        """Rilegge l'elenco porte e riallinea aperture/stati. Ritorna True se
        lo stato visibile e' cambiato (ed emette `ports_status`)."""
        names_in, names_out = self._enumerate()
        if names_in is None:
            names_in = self._in_names       # enumerazione fallita: nessun cambio di stato
        if names_out is None:
            names_out = self._out_names
        return self._apply_enumeration(names_in, names_out)

    def _check_direct_alsa(self):
        """managed: segnala (senza rimuovere) un collegamento ALSA diretto
        tastiera -> FluidSynth rimasto attivo (sez. 10.2)."""
        try:
            import subprocess
            text = subprocess.check_output(['aconnect', '-l'], timeout=3).decode('utf-8', 'replace')
        except Exception as e:
            print(f'[Router] aconnect -l non disponibile: {e}')
            return
        clients = set()
        with self._cfg_lock:
            for d in (self._devices or {}).values():
                if d['input']:
                    clients.add(d['input']['client'])
        found = direct_alsa_to_fluid(text, clients)
        if found:
            self.startup_warnings.append({
                'code': 'MONITOR_HANDLED_BY_ALSA',
                'message': 'Collegamento ALSA diretto verso FluidSynth ancora presente: possibili note doppie',
                'details': {'clients': found}})

    # ── risoluzione porte ──────────────────────────────────────────────

    def _apply_enumeration(self, names_in, names_out):
        events = []
        with self._cfg_lock:
            self._in_names = list(names_in)
            self._out_names = list(names_out)
            dkey_in = {n: _desc_key(normalize_port_name(n)) for n in names_in}
            dkey_out = {n: _desc_key(normalize_port_name(n)) for n in names_out}
            # Scelte tra porte ambigue valide solo finche' la porta scelta esiste.
            for k, n in list(self._selection.items()):
                if n not in (dkey_in if k[1] == 'input' else dkey_out):
                    del self._selection[k]
            for k in list(self._failed):
                if k[0] not in (dkey_in if k[1] == 'input' else dkey_out):
                    self._failed.discard(k)
            status, bound, cands = {}, {}, {}
            for alias, dev in (self._devices or {}).items():
                for direction, dk in (('input', dkey_in), ('output', dkey_out)):
                    key = (alias, direction)
                    desc = dev[direction]
                    if desc is None:
                        status[key] = 'unbound'
                        continue
                    want = _desc_key(desc)
                    matches = [n for n, k in dk.items() if k == want]
                    if not matches:
                        status[key] = 'missing'
                    elif len(matches) > 1 and self._selection.get(key) not in matches:
                        status[key] = 'ambiguous'
                        cands[key] = matches
                    else:
                        name = matches[0] if len(matches) == 1 else self._selection[key]
                        status[key] = 'available'
                        bound[key] = name
            internal = [n for n, k in dkey_out.items() if k[0].startswith(INTERNAL_CLIENT_PREFIX)]
            self._internal_candidates = internal if len(internal) > 1 else []
            if len(internal) == 1:
                self._internal_name, int_status = internal[0], 'available'
            else:
                self._internal_name, int_status = None, ('missing' if not internal else 'ambiguous')

            # Uscite volute: interna + uscite degli alias disponibili (una apertura per porta).
            want_out = {}
            if self._internal_name:
                want_out[self._internal_name] = dkey_out[self._internal_name]
            for (alias, direction), name in bound.items():
                if direction == 'output':
                    want_out[name] = dkey_out[name]
            for name in list(self._outs):
                if name not in want_out:
                    op = self._outs.pop(name)
                    if name in dkey_out:
                        # Porta presente ma non piu' usata: rilascio, poi chiusura.
                        self._release_out(op, None, events)
                    self._close_out(op, keep_state=name not in dkey_out)
            for name, dk in want_out.items():
                if name in self._outs:
                    continue
                op = self._open_out(name, dk, events)
                if op is None:
                    continue
                self._outs[name] = op
            # Ingressi voluti: un lettore per porta, condiviso dagli alias.
            want_in = {}
            for (alias, direction), name in bound.items():
                if direction == 'input':
                    want_in.setdefault(name, set()).add(alias)
            for name in list(self._ins):
                if name not in want_in:
                    self._close_input(self._ins.pop(name))
            for name, aliases in want_in.items():
                ip = self._ins.get(name)
                if ip is None:
                    ip = self._open_input(name, dkey_in[name], events)
                    if ip is None:
                        continue
                    self._ins[name] = ip
                ip.aliases = aliases
            # Apertura fallita -> offline (mai un'altra porta al suo posto).
            for (alias, direction), name in list(bound.items()):
                is_open = name in (self._ins if direction == 'input' else self._outs)
                if not is_open:
                    status[(alias, direction)] = 'offline'
                    del bound[(alias, direction)]
            if self._internal_name and self._internal_name not in self._outs:
                int_status = 'offline'
            self._status, self._bound, self._candidates = status, bound, cands
            self._internal_status = int_status
            self._rebuild_tables()
            signature = (tuple(names_in), tuple(names_out), tuple(sorted(status.items())), int_status)
            changed = signature != self._last_signature
            self._last_signature = signature
            payload = self._ports_status_payload_locked() if changed else None
        self._emit_events(events)
        if changed and payload is not None:
            self._emit('ports_status', payload)
        return changed

    def _open_out(self, name, dk, events):
        try:
            port = self.backend.open_output(name)
        except Exception as e:
            if (name, 'output') not in self._failed:
                self._failed.add((name, 'output'))
                events.append(('midi_error', self._error_payload(name, 'output', f'Apertura fallita: {e}')))
            print(f'[Router] Apertura output {name} fallita: {e}')
            return None
        self._failed.discard((name, 'output'))
        op = self._dead_outs.pop(dk, None)
        if op is None:
            op = _OutPort(name, {'client': dk[0], 'port': dk[1]}, port)
            print(f'[Router] Output aperto: {name}')
            return op
        # Ritorno di una porta persa: cleanup delle rotte MatSynth PRIMA di nuovi invii.
        op.name, op.port = name, port
        routed = {p['destination']['channel'] for p in self._parts
                  if self._out_matches_dest(dk, p['destination'])}
        chans = sorted(set(ch for ch in range(16) if op.touched[ch] or op.sustain[ch]) | routed)
        ok = self._release_out(op, chans, events, force=True)
        self.last_recovery.append({'port': name, 'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                   'cleanup': 'sent' if ok else 'failed', 'channels': chans})
        del self.last_recovery[:-8]
        if not ok:
            # Cleanup non riuscito: nessun nuovo invio su questa porta, resta offline.
            self._close_out(op, keep_state=True)
            self._failed.add((name, 'output'))
            return None
        print(f'[Router] Output ripristinato: {name} (cleanup canali {chans})')
        return op

    def _out_matches_dest(self, dk, dest):
        if dest['kind'] == 'internal':
            return dk[0].startswith(INTERNAL_CLIENT_PREFIX)
        dev = (self._devices or {}).get(dest['device_alias'])
        return bool(dev and dev['output'] and _desc_key(dev['output']) == dk)

    def _close_out(self, op, keep_state):
        try:
            if op.port is not None:
                op.port.close()
        except Exception:
            pass
        with op.lock:
            op.port = None
        if keep_state and (any(op.touched) or any(op.sustain)):
            self._dead_outs[_desc_key(op.desc)] = op

    def _open_input(self, name, dk, events):
        ip = _InPort(name, {'client': dk[0], 'port': dk[1]})
        try:
            ip.port = self.backend.open_input(name, callback=self._make_input_callback(ip))
        except Exception as e:
            if (name, 'input') not in self._failed:
                self._failed.add((name, 'input'))
                events.append(('midi_error', self._error_payload(name, 'input', f'Apertura fallita: {e}')))
            print(f'[Router] Apertura input {name} fallita: {e}')
            return None
        self._failed.discard((name, 'input'))
        print(f'[Router] Input aperto: {name}')
        return ip

    def _close_input(self, ip):
        ip.closed = True
        try:
            if ip.port is not None:
                ip.port.close()
        except Exception:
            pass

    def _rebuild_tables(self):
        """Ricalcola le tabelle del percorso caldo (chiamare sotto _cfg_lock).
        Le liste nuove sostituiscono le vecchie con un solo assegnamento."""
        track_out = [None] * 16
        track_ch = [0] * 16
        for t, p in enumerate(self._parts):
            d = p['destination']
            track_ch[t] = d['channel']
            if d['kind'] == 'internal':
                name = self._internal_name if self._internal_status != 'offline' else None
            else:
                name = self._bound.get((d['device_alias'], 'output'))
            track_out[t] = self._outs.get(name) if name else None
        self._track_ch = track_ch
        self._track_out = track_out
        managed = self.mode == 'managed'
        for ip in self._ins.values():
            match = [[] for _ in range(16)]
            mon = [[] for _ in range(16)]
            for t, p in enumerate(self._parts):
                if p['input']['device_alias'] not in ip.aliases:
                    continue
                chans = range(16) if p['input']['channel'] is None else (p['input']['channel'],)
                for c in chans:
                    match[c].append(t)
                    if managed and p['monitor']:
                        mon[c].append(t)
            ip.match = tuple(tuple(m) for m in match)
            ip.mon = tuple(tuple(m) for m in mon)

    # ── stato ──────────────────────────────────────────────────────────

    def port_status(self, alias, direction):
        if alias is None:
            return 'unbound'
        with self._cfg_lock:
            if self._devices is None or alias not in self._devices:
                return 'missing'
            return self._status.get((alias, direction),
                                    'unbound' if self._devices[alias][direction] is None else 'missing')

    def internal_status(self):
        return self._internal_status

    def internal_available(self):
        return self._internal_status == 'available' and self._internal_name in self._outs

    def destination_status(self, part):
        d = part['destination']
        if d['kind'] == 'internal':
            return self._internal_status
        return self.port_status(d['device_alias'], 'output')

    def input_status(self, part):
        return self.port_status(part['input']['device_alias'], 'input')

    def internal_port_object(self):
        op = self._outs.get(self._internal_name) if self._internal_name else None
        return op.port if op else None

    def primary_input_port(self):
        """Porta di `keyboard-main` (compatibilita' di MultiTrackDAW.midi_input)."""
        with self._cfg_lock:
            name = self._bound.get((DEFAULT_INPUT_ALIAS, 'input'))
            ip = self._ins.get(name) if name else None
            if ip is None and self._ins:
                ip = next(iter(self._ins.values()))
            return ip.port if ip else None

    def get_parts(self):
        with self._cfg_lock:
            return copy.deepcopy(self._parts)

    def get_part(self, t):
        with self._cfg_lock:
            return copy.deepcopy(self._parts[t])

    def part_runtime(self, t, part=None):
        p = part if part is not None else self.get_part(t)
        in_st = self.input_status(p)
        dest_st = self.destination_status(p)
        warnings = []
        if self.mode == 'legacy':
            if p['input']['device_alias'] is not None:
                details = {}
                if p['destination']['kind'] == 'external' or \
                        p['input']['channel'] != p['destination']['channel']:
                    # In legacy l'input autoconnesso suona comunque su FluidSynth sul suo canale.
                    details = {'alsa_plays_on': {'kind': 'internal', 'channel': p['input']['channel']}}
                warnings.append({'code': 'MONITOR_HANDLED_BY_ALSA',
                                 'message': LEGACY_MONITOR_MESSAGE, 'details': details})
            effective = False
        else:
            effective = bool(p['monitor']) and in_st == 'available' and dest_st == 'available'
        return {'part_id': t, 'input_status': in_st, 'destination_status': dest_st,
                'monitor_effective': effective, 'warnings': warnings}

    def record_preflight(self, tracks):
        """Tracce armate senza input utilizzabile (Record le rifiuta tutte, D7)."""
        problems = []
        with self._cfg_lock:
            for t in tracks:
                p = self._parts[t]
                alias = p['input']['device_alias']
                st = self.port_status(alias, 'input')
                if st != 'available':
                    problems.append({'track': t, 'alias': alias, 'status': st})
        return problems

    def playback_preflight(self, tracks):
        problems = []
        with self._cfg_lock:
            for t in tracks:
                p = self._parts[t]
                st = self.destination_status(p)
                if st != 'available' or self._track_out[t] is None:
                    problems.append({'track': t, 'alias': p['destination']['device_alias'],
                                     'status': st if st != 'available' else 'offline'})
        return problems

    # ── invio (percorso caldo) ─────────────────────────────────────────

    def set_recorder(self, fn):
        self._recorder = fn

    def _send_locked(self, op, msg):
        """Invio + tracciamento sotto op.lock. Ritorna True/False/None (None =
        note-off non tracciato scartato: nulla da spegnere)."""
        typ = msg.type
        if typ == 'note_on' or typ == 'note_off':
            idx = (msg.channel << 7) | msg.note
            if typ == 'note_on' and msg.velocity:
                op.port.send(msg)
                if op.notes[idx] < 255:
                    op.notes[idx] += 1
                op.touched[msg.channel] = 1
                return True
            if not op.notes[idx]:
                return None         # nessuna nota da chiudere su questa porta
            op.port.send(msg)
            op.notes[idx] -= 1
            return True
        op.port.send(msg)
        if typ == 'control_change' and msg.control == 64:
            if msg.value >= 64:
                op.sustain[msg.channel] = 1
                op.touched[msg.channel] = 1
            else:
                op.sustain[msg.channel] = 0
        return True

    def _send_out(self, op, msg):
        failed = None
        with op.lock:
            if op.port is None:
                return False
            try:
                res = self._send_locked(op, msg)
            except Exception as e:
                failed, res = e, False
        if failed is not None:
            self._mark_out_failed(op, failed)
        return res is not False

    def send_track(self, t, msg, mutable=False, gate=None):
        """Invia `msg` alla destinazione della parte `t`, rimappato sul suo canale.

        `mutable=True`: il messaggio e' usa-e-getta (playback) e il canale viene
        cambiato in place, senza copia. `gate`: sequenza indicizzata per parte
        (es. udibilita' mute/solo) controllata SOTTO il lock della porta, cosi'
        un mute + rilascio concorrente non lascia passare un note-on dopo il
        rilascio. Ritorna l'_OutPort usata o None.
        """
        for _ in range(3):
            op = self._track_out[t]
            if op is None:
                return None
            failed = None
            with op.lock:
                if self._track_out[t] is not op or op.port is None:
                    continue                    # routing cambiato nel frattempo: risolvi di nuovo
                if gate is not None and not gate[t]:
                    return None
                ch = self._track_ch[t]
                if msg.channel != ch:
                    if mutable:
                        msg.channel = ch
                    else:
                        msg = msg.copy(channel=ch)
                try:
                    res = self._send_locked(op, msg)
                except Exception as e:
                    failed, res = e, False
            if failed is not None:
                self._mark_out_failed(op, failed)
                return None
            return op if res is not False else None
        return None

    def send_internal(self, msg):
        """Invio alla destinazione interna (click): nessun fallback esterno."""
        name = self._internal_name
        op = self._outs.get(name) if name else None
        if op is None:
            return False
        return self._send_out(op, msg)

    def _mark_out_failed(self, op, err):
        print(f'[Router] Invio su {op.name} fallito: {err}')
        events = []
        with self._cfg_lock:
            if self._outs.get(op.name) is op:
                del self._outs[op.name]
                self._close_out(op, keep_state=True)
                self._failed.add((op.name, 'output'))
                for key, name in list(self._bound.items()):
                    if name == op.name and key[1] == 'output':
                        self._status[key] = 'offline'
                        del self._bound[key]
                if op.name == self._internal_name:
                    self._internal_status = 'offline'
                self._rebuild_tables()
                events.append(('midi_error', self._error_payload(op.name, 'output', f'Invio fallito: {err}')))
                self._last_signature = None
        self._emit_events(events)

    # ── rilascio note / PANIC ──────────────────────────────────────────

    def _release_out(self, op, channels, events, force=False):
        """Note-off tracciati + CC64=0 + CC123 sui canali coinvolti.
        Con `force` (ritorno porta) invia CC64/CC123 anche se nulla e' tracciato."""
        with op.lock:
            if op.port is None:
                return False
            try:
                for ch in (range(16) if channels is None else channels):
                    if not force and not op.touched[ch] and not op.sustain[ch]:
                        continue
                    base = ch << 7
                    seg = op.notes[base:base + 128]
                    if seg.count(0) != 128:          # scansione in C, non in Python
                        for n in range(128):
                            for _ in range(seg[n]):
                                op.port.send(mido.Message('note_off', channel=ch, note=n, velocity=0))
                        op.notes[base:base + 128] = bytes(128)
                    op.port.send(mido.Message('control_change', channel=ch, control=64, value=0))
                    op.port.send(mido.Message('control_change', channel=ch, control=123, value=0))
                    op.touched[ch] = 0
                    op.sustain[ch] = 0
                return True
            except Exception as e:
                err = e
        events.append(('midi_error', self._error_payload(op.name, 'output', f'Invio fallito: {err}')))
        return False

    def release_tracks(self, tracks):
        """Rilascia note/sustain sulle destinazioni (porta, canale) delle parti."""
        with self._cfg_lock:
            by_op = {}
            for t in tracks:
                op = self._track_out[t]
                if op is not None:
                    by_op.setdefault(id(op), (op, set()))[1].add(self._track_ch[t])
        events = []
        for op, chans in by_op.values():
            self._release_out(op, sorted(chans), events)
        self._emit_events(events)

    def release_part_notes(self, part_id):
        self.release_tracks((part_id,))

    def release_all(self):
        """Rilascio su tutte le porte aperte, solo sui canali toccati da MatSynth."""
        with self._cfg_lock:
            ops = list(self._outs.values())
        events = []
        for op in ops:
            self._release_out(op, None, events)
        self._emit_events(events)

    def panic(self):
        """PANIC (sez. 3.7): su ogni destinazione aperta, tutti i 16 canali:
        note-off tracciati, CC64=0, CC123, CC120. Ritorna l'esito per porta."""
        with self._cfg_lock:
            ops = list(self._outs.values())
        report = []
        events = []
        for op in ops:
            ok = True
            with op.lock:
                if op.port is None:
                    continue
                try:
                    for ch in range(16):
                        base = ch << 7
                        seg = op.notes[base:base + 128]
                        if seg.count(0) != 128:
                            for n in range(128):
                                for _ in range(seg[n]):
                                    op.port.send(mido.Message('note_off', channel=ch, note=n, velocity=0))
                    op.notes[:] = bytes(len(op.notes))
                    for ch in range(16):
                        op.port.send(mido.Message('control_change', channel=ch, control=64, value=0))
                        op.port.send(mido.Message('control_change', channel=ch, control=123, value=0))
                        op.port.send(mido.Message('control_change', channel=ch, control=120, value=0))
                    op.touched[:] = bytes(16)
                    op.sustain[:] = bytes(16)
                except Exception as e:
                    ok = False
                    events.append(('midi_error', self._error_payload(op.name, 'output', f'PANIC fallito: {e}')))
            report.append({'port': op.name, 'delivery': 'sent' if ok else 'failed'})
        self._emit_events(events)
        return report

    # ── ingresso / monitor ─────────────────────────────────────────────

    def _make_input_callback(self, ip):
        send_track = self.send_track
        activity = self._activity

        def _on_message(msg):
            # Eseguita nel thread di rtmidi: nessuna allocazione evitabile,
            # nessun lock di configurazione, eccezioni mai propagate a rtmidi.
            try:
                if ip.closed or msg.type not in _CHANNEL_TYPES:
                    return
                now = time.time()
                ch = msg.channel
                for t in ip.mon[ch]:            # vuota in legacy: zero reinvii
                    send_track(t, msg)          # copia solo se il canale va rimappato
                match = ip.match[ch]
                if match:
                    rec = self._recorder
                    if rec is not None:
                        rec(match, msg, now)
                    for t in match:
                        activity[t] = 1
                    if self._emit_fn is not None:
                        self._deferred.call_later(ACTIVITY_INTERVAL_S, self._flush_activity, key='activity')
            except Exception as e:
                print(f'[Router] Errore callback input: {e}')
        return _on_message

    def _flush_activity(self):
        parts = [t for t in range(16) if self._activity[t]]
        self._activity[:] = bytes(16)
        if parts:
            self._emit('input_activity', {'parts': parts})

    # ── preview ────────────────────────────────────────────────────────

    def preview_note(self, t, note, velocity, length_ms):
        """Nota breve alla destinazione della traccia; il note-off va alla
        stessa porta/canale del note-on (anche se il routing cambia) ed e'
        programmato sul thread differito unico, non su un thread per nota."""
        on = mido.Message('note_on', channel=self._track_ch[t], note=note, velocity=velocity)
        op = self.send_track(t, on, mutable=True)
        if op is None:
            return False
        # on.channel e' il canale effettivamente usato (rimappato in place).
        off = mido.Message('note_off', channel=on.channel, note=note, velocity=0)
        self._deferred.call_later(length_ms / 1000.0, lambda: self._send_out(op, off))
        return True

    def call_later(self, delay, fn, key=None):
        """Esecuzione differita sul thread unico (usata da app.py per coalescere eventi)."""
        return self._deferred.call_later(delay, fn, key)

    # ── configurazione parti ───────────────────────────────────────────

    def apply_part(self, part, send='diff', cc_keys=None):
        """Sostituisce la parte e invia cio' che serve alla destinazione.

        send: 'diff' (cio' che e' cambiato), 'all' (suono + tutti i CC, uso
        richiamo scena), 'none'. Se la destinazione cambia, note e sustain
        vengono rilasciati sulla VECCHIA destinazione dopo il cambio di
        tabella (nessun note-on puo' finire sulla vecchia dopo il rilascio).
        Ritorna il report `applied` di sez. 8.2.
        """
        t = part['part_id'] if isinstance(part, dict) and _is_int(part.get('part_id')) else -1
        if not 0 <= t <= 15:
            _vfail('part_id', 'intero 0..15 atteso')
        validate_part(part, t)
        new = copy.deepcopy(part)
        with self._cfg_lock:
            old = self._parts[t]
            old_op, old_ch = self._track_out[t], self._track_ch[t]
            dest_changed = old['destination'] != new['destination']
            # Anche input/monitor: un tasto tenuto il cui note-off non arrivera'
            # piu' (input cambiato, monitor spento) resterebbe sospeso.
            route_changed = dest_changed or old['input'] != new['input'] \
                or old['monitor'] != new['monitor']
            self._parts[t] = new
            self._rebuild_tables()
        if route_changed and old_op is not None:
            events = []
            self._release_out(old_op, [old_ch], events)
            self._emit_events(events)
        if send == 'none':
            include_sound, keys = False, []
        elif send == 'all' or dest_changed:
            include_sound, keys = True, None
        else:
            include_sound = old['sound'] != new['sound']
            keys = [k for k in CC_KEYS if new['cc'].get(k) != old['cc'].get(k) and k in new['cc']]
        if cc_keys is not None:
            keys = [k for k in CC_KEYS if k in cc_keys and k in new['cc']]
        return self._deliver(t, new, include_sound, keys)

    def _deliver(self, t, part, include_sound, cc_keys):
        status = self.destination_status(part)
        report = {'delivery': 'skipped', 'sent': [], 'destination_status': status}
        if not include_sound and not cc_keys and cc_keys is not None:
            return report
        if part['destination']['kind'] == 'internal':
            if self._console is None:
                return report
            sf = self._sf_id() if callable(self._sf_id) else (self._sf_id or 1)
            cmds = internal_console_commands(part, sf, include_sound, cc_keys)
            if not cmds:
                return report
            for label, cmd in cmds:          # console fuori da ogni lock
                if not self._console(cmd):
                    report['delivery'] = 'failed'
                    return report
                report['sent'].append(label)
            report['delivery'] = 'sent'
            return report
        if status != 'available':
            return report
        msgs = external_sound_messages(part, include_sound, cc_keys)
        if not msgs:
            return report
        for label, msg in msgs:
            if self.send_track(t, msg, mutable=True) is None:
                report['delivery'] = 'failed'
                return report
            report['sent'].append(label)
        report['delivery'] = 'sent'
        return report

    def send_part_cc(self, t, cc, value):
        """CC di una parte (fast path slider, route /cc): stessa risoluzione
        della destinazione. Interno -> console; esterno -> porta. Ritorna
        'sent' | 'failed' | 'skipped'."""
        with self._cfg_lock:
            part = self._parts[t]
            key = str(cc)
            if key in CC_KEYS:
                part['cc'][key] = value
            kind, ch = part['destination']['kind'], part['destination']['channel']
            alias = part['destination']['device_alias']
        if kind == 'internal':
            if self._console is None:
                return 'skipped'
            return 'sent' if self._console(f'cc {ch} {cc} {value}') else 'failed'
        if self.port_status(alias, 'output') != 'available':
            return 'skipped'
        msg = mido.Message('control_change', channel=ch, control=cc, value=value)
        return 'sent' if self.send_track(t, msg, mutable=True) is not None else 'failed'

    # ── registro dispositivi ───────────────────────────────────────────

    def devices_list(self):
        with self._cfg_lock:
            return [copy.deepcopy(d) for d in (self._devices or {}).values()]

    def _save_registry(self):
        if not self.devices_file or self._devices is None:
            return
        doc = {'schema_version': 1, 'devices': list(self._devices.values())}
        atomic_write_json(self.devices_file, doc)

    def set_device(self, alias, body):
        """Crea/aggiorna un alias (PUT /api/midi/devices/<alias>). Ritorna (creato, device)."""
        if not isinstance(alias, str) or not ALIAS_RE.match(alias):
            raise RouterError('BAD_REQUEST', 'Alias non valido', {'field': 'alias'})
        if not isinstance(body, dict):
            raise RouterError('BAD_REQUEST', 'Corpo JSON oggetto atteso', {'field': ''})
        for k in body:
            if k not in ('label', 'input', 'output', 'controls'):
                raise RouterError('BAD_REQUEST', f'Campo non previsto: {k}', {'field': k})
        with self._cfg_lock:
            devices = dict(self._devices or {})
            old = devices.get(alias)
            dev = copy.deepcopy(old) if old else {'alias': alias, 'label': alias, 'input': None,
                                                  'output': None, 'controls': None}
            if 'label' in body:
                if not isinstance(body['label'], str) or not 1 <= len(body['label']) <= 64:
                    raise RouterError('BAD_REQUEST', 'Etichetta 1..64 caratteri', {'field': 'label'})
                dev['label'] = body['label']
            for direction, names in (('input', self._in_names), ('output', self._out_names)):
                if direction not in body:
                    continue
                v = body[direction]
                if v is None:
                    dev[direction] = None
                elif isinstance(v, dict) and set(v) == {'from_port'}:
                    if v['from_port'] not in names:
                        raise RouterError('BAD_REQUEST', f"Porta {direction} non presente: {v['from_port']}",
                                          {'field': f'{direction}.from_port'})
                    dev[direction] = normalize_port_name(v['from_port'])
                elif isinstance(v, dict) and set(v) == {'descriptor'}:
                    try:
                        _validate_descriptor(v['descriptor'], f'{direction}.descriptor')
                    except RouterError:
                        raise RouterError('BAD_REQUEST', 'Descrittore non valido',
                                          {'field': f'{direction}.descriptor'})
                    dev[direction] = dict(v['descriptor'])
                else:
                    raise RouterError('BAD_REQUEST', f'{direction}: from_port, descriptor o null',
                                      {'field': direction})
            if 'controls' in body:
                try:
                    _validate_controls(body['controls'], 'controls')
                except RouterError:
                    raise RouterError('BAD_REQUEST', 'controls: lista di CC di sez. 3.1 o null',
                                      {'field': 'controls'})
                c = body['controls']
                dev['controls'] = None if c is None else [k for k in CC_KEYS if k in c]
            devices[alias] = dev
            prev = self._devices
            self._devices = devices
            try:
                self._save_registry()
            except Exception:
                self._devices = prev
                raise
        self.refresh_ports()
        return old is None, copy.deepcopy(dev)

    def delete_device(self, alias):
        with self._cfg_lock:
            if not self._devices or alias not in self._devices:
                raise RouterError('NOT_FOUND', f'Dispositivo {alias} inesistente', {'kind': 'device'})
            used = [p['part_id'] for p in self._parts
                    if p['input']['device_alias'] == alias or p['destination']['device_alias'] == alias]
            if used:
                raise RouterError('DEVICE_IN_USE', f'Dispositivo {alias} usato da parti', {'parts': used})
            prev = self._devices
            self._devices = {a: d for a, d in prev.items() if a != alias}
            try:
                self._save_registry()
            except Exception:
                self._devices = prev
                raise
        self.refresh_ports()

    def select_port(self, alias, direction, port):
        if direction not in ('input', 'output'):
            raise RouterError('BAD_REQUEST', 'direction: input o output', {'field': 'direction'})
        with self._cfg_lock:
            if not self._devices or alias not in self._devices:
                raise RouterError('NOT_FOUND', f'Dispositivo {alias} inesistente', {'kind': 'device'})
            cands = self._candidates.get((alias, direction), [])
            if port not in cands:
                raise RouterError('BAD_REQUEST', 'Porta non tra i candidati ambigui', {'field': 'port'})
            self._selection[(alias, direction)] = port
        self.refresh_ports()
        return self.describe_device(alias)

    def describe_device(self, alias):
        with self._cfg_lock:
            d = copy.deepcopy(self._devices[alias])
            d['status'] = {'input': self.port_status(alias, 'input'),
                           'output': self.port_status(alias, 'output')}
            c = {k[1]: list(v) for k, v in self._candidates.items() if k[0] == alias}
            if c:
                d['candidates'] = c
            return d

    def describe_devices(self):
        with self._cfg_lock:
            return [self.describe_device(a) for a in (self._devices or {})]

    def describe_ports(self, include_all=False):
        with self._cfg_lock:
            def aliases_for(desc, direction):
                k = _desc_key(desc)
                return [a for a, d in (self._devices or {}).items()
                        if d[direction] and _desc_key(d[direction]) == k]
            inputs, outputs = [], []
            for n in self._in_names:
                desc = normalize_port_name(n)
                ex = port_exclusion(desc, 'input')
                if ex and not include_all:
                    continue
                inputs.append({'name': n, 'descriptor': desc, 'aliases': aliases_for(desc, 'input'),
                               'excluded': ex})
            for n in self._out_names:
                desc = normalize_port_name(n)
                ex = port_exclusion(desc, 'output')
                if ex and not include_all:
                    continue
                outputs.append({'name': n, 'descriptor': desc, 'aliases': aliases_for(desc, 'output'),
                                'internal': is_internal_descriptor(desc), 'excluded': ex})
            return {'inputs': inputs, 'outputs': outputs}

    def _ports_status_payload_locked(self):
        p = self.describe_ports(False)
        p['devices'] = [{'alias': a, 'status': {'input': self.port_status(a, 'input'),
                                                'output': self.port_status(a, 'output')}}
                        for a in (self._devices or {})]
        p['internal'] = self._internal_status
        return p

    def ports_status_payload(self):
        with self._cfg_lock:
            return self._ports_status_payload_locked()

    # ── emissione eventi (sempre fuori dai lock) ───────────────────────

    def _error_payload(self, name, direction, message):
        alias = None
        for (a, d), n in self._bound.items():
            if n == name and d == direction:
                alias = a
                break
        return {'alias': alias, 'port': name, 'direction': direction, 'status': 'offline',
                'message': message, 'at': time.strftime('%Y-%m-%d %H:%M:%S')}

    def _emit(self, event, payload):
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(event, payload)
        except Exception as e:
            print(f'[Router] Errore emit {event}: {e}')

    def _emit_events(self, events):
        for ev, payload in events:
            self._emit(ev, payload)
