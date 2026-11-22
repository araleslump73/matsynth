"""Validatore e migrazione DI RIFERIMENTO per docs/contracts.md (sez. 3-6).

E' la specifica eseguibile dei documenti parte/scena/progetto: nessuna
dipendenza (niente jsonschema), nessun import di app.py, nessun hardware.
Non e' codice applicativo: `ui-persistence` puo' portarlo in
`project_store.py`; da quel momento tests/test_contract_examples.py deve
importare il modulo reale e conservare tutti i casi.

Errori: ContractError(code, path, reason) con `code` in
{'VALIDATION_FAILED', 'UNSUPPORTED_VERSION'} e `path` nella forma usata da
`error.details.path` (es. "parts[3].cc.75").
"""

import math
import re

SUPPORTED_SCENE_VERSION = 1
SUPPORTED_PROJECT_VERSION = 1
CC_KEYS = ('7', '10', '71', '72', '73', '74', '75')
DEFAULT_CC = {'7': 100, '10': 64, '71': 64, '72': 64, '73': 64, '74': 64, '75': 64}
ID_RE = re.compile(r'^[\w-]{1,64}$')            # Unicode \w (stem legacy con accenti)
ALIAS_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
MAX_BEAT = 100000
MAX_EVENTS_TOTAL = 120000
DEFAULT_INPUT_ALIAS = 'keyboard-main'

# Nome campo legacy -> chiave CC (stessa mappa di app.py:control)
LEGACY_CC_FIELDS = (('volume', '7'), ('pan', '10'), ('resonance', '71'), ('release', '72'),
                    ('attack', '73'), ('cutoff', '74'), ('decay', '75'))


class ContractError(Exception):
    def __init__(self, code, path, reason):
        super().__init__(f'{code} at {path}: {reason}')
        self.code = code
        self.path = path
        self.reason = reason


def _fail(path, reason, code='VALIDATION_FAILED'):
    raise ContractError(code, path, reason)


def _join(path, key):
    if isinstance(key, int):
        return f'{path}[{key}]'
    return f'{path}.{key}' if path else str(key)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _int_range(v, lo, hi, path, nullable=False):
    if v is None and nullable:
        return
    if not _is_int(v) or not lo <= v <= hi:
        _fail(path, f'intero {lo}..{hi}' + (' o null' if nullable else '') + ' atteso')


def _num_range(v, lo, hi, path):
    if not _is_num(v) or not lo <= v <= hi:
        _fail(path, f'numero {lo}..{hi} atteso')


def _str(v, path, lo=0, hi=64, nullable=False):
    if v is None and nullable:
        return
    if not isinstance(v, str) or not lo <= len(v) <= hi:
        _fail(path, f'stringa {lo}..{hi} caratteri attesa')


def _keys(obj, path, required, optional=()):
    if not isinstance(obj, dict):
        _fail(path, 'oggetto atteso')
    for k in required:
        if k not in obj:
            _fail(_join(path, k), 'campo obbligatorio mancante')
    allowed = set(required) | set(optional)
    for k in obj:
        if k not in allowed:
            _fail(_join(path, k), 'campo non previsto')


def _list(v, path, length=None):
    if not isinstance(v, list):
        _fail(path, 'array atteso')
    if length is not None and len(v) != length:
        _fail(path, f'esattamente {length} elementi attesi')


# ── Parte (sez. 3.1) ────────────────────────────────────────────────

PART_KEYS = ('part_id', 'input', 'destination', 'monitor', 'sound', 'cc')
INTERNAL_SOUND_KEYS = ('bank', 'program', 'name')
EXTERNAL_SOUND_KEYS = ('bank_msb', 'bank_lsb', 'program', 'name')


def validate_part(p, path='', index=None):
    _keys(p, path, PART_KEYS)
    _int_range(p['part_id'], 0, 15, _join(path, 'part_id'))
    if index is not None and p['part_id'] != index:
        _fail(_join(path, 'part_id'), "deve essere uguale all'indice")

    ip = _join(path, 'input')
    _keys(p['input'], ip, ('device_alias', 'channel'))
    alias = p['input']['device_alias']
    if alias is not None and (not isinstance(alias, str) or not ALIAS_RE.match(alias)):
        _fail(_join(ip, 'device_alias'), 'alias non valido')
    _int_range(p['input']['channel'], 0, 15, _join(ip, 'channel'), nullable=True)

    dp = _join(path, 'destination')
    d = p['destination']
    _keys(d, dp, ('kind', 'device_alias', 'channel'))
    if d['kind'] not in ('internal', 'external'):
        _fail(_join(dp, 'kind'), 'internal o external atteso')
    if d['kind'] == 'internal' and d['device_alias'] is not None:
        _fail(_join(dp, 'device_alias'), 'null per destinazione interna')
    if d['kind'] == 'external' and (not isinstance(d['device_alias'], str)
                                    or not ALIAS_RE.match(d['device_alias'])):
        _fail(_join(dp, 'device_alias'), 'alias obbligatorio per destinazione esterna')
    _int_range(d['channel'], 0, 15, _join(dp, 'channel'))

    if not isinstance(p['monitor'], bool):
        _fail(_join(path, 'monitor'), 'bool atteso')

    sp = _join(path, 'sound')
    s = p['sound']
    if d['kind'] == 'internal':
        _keys(s, sp, INTERNAL_SOUND_KEYS)
        _int_range(s['bank'], 0, 16383, _join(sp, 'bank'))
        _int_range(s['program'], 0, 127, _join(sp, 'program'))
    else:
        _keys(s, sp, EXTERNAL_SOUND_KEYS)
        for k in ('bank_msb', 'bank_lsb', 'program'):
            _int_range(s[k], 0, 127, _join(sp, k), nullable=True)
    _str(s['name'], _join(sp, 'name'), nullable=True)

    cp = _join(path, 'cc')
    cc = p['cc']
    if not isinstance(cc, dict):
        _fail(cp, 'oggetto atteso')
    for k, v in cc.items():
        if k not in CC_KEYS:
            _fail(_join(cp, k), 'CC non previsto')
        _int_range(v, 0, 127, _join(cp, k))
    if d['kind'] == 'internal':
        for k in CC_KEYS:
            if k not in cc:
                _fail(_join(cp, k), 'obbligatorio per parte interna')


def default_part(n):
    """Parte di default (sez. 3.2)."""
    return {
        'part_id': n,
        'input': {'device_alias': DEFAULT_INPUT_ALIAS, 'channel': n},
        'destination': {'kind': 'internal', 'device_alias': None, 'channel': n},
        'monitor': True,
        'sound': {'bank': 128 if n == 9 else 0, 'program': 0, 'name': None},
        'cc': dict(DEFAULT_CC),
    }


# ── Corpo sonoro comune a scena e progetto (sez. 4.1) ───────────────

def _validate_sound_body(doc, path):
    sfp = _join(path, 'soundfont')
    sf = doc['soundfont']
    if sf is not None:
        _keys(sf, sfp, ('file',))
        f = sf['file']
        if not isinstance(f, str) or not f or '/' in f or '\\' in f:
            _fail(_join(sfp, 'file'), 'nome file base atteso')
    gp = _join(path, 'globals')
    _keys(doc['globals'], gp, ('gain', 'reverb_level', 'chorus_level'))
    _num_range(doc['globals']['gain'], 0, 10, _join(gp, 'gain'))
    _num_range(doc['globals']['reverb_level'], 0, 1, _join(gp, 'reverb_level'))
    _num_range(doc['globals']['chorus_level'], 0, 10, _join(gp, 'chorus_level'))
    pp = _join(path, 'parts')
    _list(doc['parts'], pp, 16)
    for i, part in enumerate(doc['parts']):
        validate_part(part, _join(pp, i), index=i)


def _check_format_version(doc, fmt, supported):
    # Formato e versione PRIMA di tutto: una versione futura con campi nuovi
    # deve dare UNSUPPORTED_VERSION, non VALIDATION_FAILED.
    if not isinstance(doc, dict):
        _fail('', 'oggetto atteso')
    if doc.get('format') != fmt:
        _fail('format', f'atteso {fmt}', 'UNSUPPORTED_VERSION')
    v = doc.get('schema_version')
    if not _is_int(v):
        _fail('schema_version', 'intero atteso')
    if v > supported:
        _fail('schema_version', f'versione {v} > {supported}', 'UNSUPPORTED_VERSION')
    if v < 1:
        _fail('schema_version', 'versione non valida')


SCENE_KEYS = ('format', 'schema_version', 'id', 'name', 'created', 'modified',
              'soundfont', 'globals', 'parts')


def validate_scene(doc):
    _check_format_version(doc, 'matsynth.scene', SUPPORTED_SCENE_VERSION)
    _keys(doc, '', SCENE_KEYS)
    if not isinstance(doc['id'], str) or not ID_RE.match(doc['id']):
        _fail('id', 'ID non valido')
    _str(doc['name'], 'name', 1, 64)
    _str(doc['created'], 'created', 0, 32)
    _str(doc['modified'], 'modified', 0, 32)
    _validate_sound_body(doc, '')


# ── Progetto (sez. 5) ───────────────────────────────────────────────

PROJECT_KEYS = ('format', 'schema_version', 'id', 'name', 'created', 'modified',
                'transport', 'sound', 'tracks', 'next_clip_id')
PROJECT_OPTIONAL_KEYS = ('devices', 'source_scene')
TRACK_KEYS = ('id', 'name', 'color', 'mute', 'solo', 'events', 'clips')
CLIP_KEYS = ('id', 'start_beat', 'end_beat', 'name', 'created_at')


def _validate_event(ev, path):
    if not isinstance(ev, list) or len(ev) != 2:
        _fail(path, '[beat, bytes] atteso')
    beat, data = ev
    if not _is_num(beat) or not 0 <= beat <= MAX_BEAT:
        _fail(_join(path, 0), f'beat 0..{MAX_BEAT} atteso')
    if not isinstance(data, list) or not data:
        _fail(_join(path, 1), 'lista di byte attesa')
    status = data[0]
    if not _is_int(status) or not 0x80 <= status <= 0xEF:
        _fail(_join(path, 1), 'solo messaggi di canale (status 0x80..0xEF)')
    expected_len = 2 if (status & 0xF0) in (0xC0, 0xD0) else 3
    if len(data) != expected_len:
        _fail(_join(path, 1), f'{expected_len} byte attesi')
    for b in data[1:]:
        if not _is_int(b) or not 0 <= b <= 127:
            _fail(_join(path, 1), 'byte dati 0..127 attesi')
    return beat


def _pair_fifo_unpaired(events):
    """Conta i note-on senza note-off con la regola FIFO per altezza (sez. 6.2)."""
    open_notes = {}
    for _beat, data in events:
        kind = data[0] & 0xF0
        if kind == 0x90 and data[2] > 0:
            open_notes[data[1]] = open_notes.get(data[1], 0) + 1
        elif kind == 0x80 or (kind == 0x90 and data[2] == 0):
            if open_notes.get(data[1]):
                open_notes[data[1]] -= 1
    return sum(open_notes.values())


def owned_indices(events, start, end):
    """Indici posseduti da [start, end) secondo sez. 6.3: note con note-on nell'intervallo
    (con il loro note-off, accoppiato FIFO per altezza) + eventi non-nota nell'intervallo."""
    owned = set()
    open_notes = {}
    for idx, (beat, data) in enumerate(events):
        kind = data[0] & 0xF0
        if kind == 0x90 and data[2] > 0:
            open_notes.setdefault(data[1], []).append(idx)
            if start <= beat < end:
                owned.add(idx)
        elif kind == 0x80 or (kind == 0x90 and data[2] == 0):
            pending = open_notes.get(data[1])
            if pending:
                on_idx = pending.pop(0)
                if on_idx in owned:
                    owned.add(idx)
        elif start <= beat < end:
            owned.add(idx)
    return owned


def validate_project(doc):
    """Valida un progetto; ritorna la lista dei warning (codici sez. 2.4)."""
    _check_format_version(doc, 'matsynth.project', SUPPORTED_PROJECT_VERSION)
    _keys(doc, '', PROJECT_KEYS, PROJECT_OPTIONAL_KEYS)
    if not isinstance(doc['id'], str) or not ID_RE.match(doc['id']):
        _fail('id', 'ID non valido')
    _str(doc['name'], 'name', 1, 64)
    _str(doc['created'], 'created', 0, 32)
    _str(doc['modified'], 'modified', 0, 32)

    t = doc['transport']
    _keys(t, 'transport', ('bpm', 'beats_per_measure', 'loop'))
    _num_range(t['bpm'], 30, 300, 'transport.bpm')
    if t['beats_per_measure'] not in (3, 4) or isinstance(t['beats_per_measure'], bool):
        _fail('transport.beats_per_measure', '3 o 4 atteso')
    lp = t['loop']
    _keys(lp, 'transport.loop', ('enabled', 'start_beat', 'end_beat'))
    if not isinstance(lp['enabled'], bool):
        _fail('transport.loop.enabled', 'bool atteso')
    _num_range(lp['start_beat'], 0, MAX_BEAT, 'transport.loop.start_beat')
    _num_range(lp['end_beat'], 0, MAX_BEAT, 'transport.loop.end_beat')
    if lp['enabled'] and lp['end_beat'] <= lp['start_beat']:
        _fail('transport.loop.end_beat', 'end_beat > start_beat con loop attivo')

    _keys(doc['sound'], 'sound', ('soundfont', 'globals', 'parts'))
    _validate_sound_body(doc['sound'], 'sound')

    src = doc.get('source_scene')
    if src is not None:
        _keys(src, 'source_scene', ('id', 'name'))
        _str(src['name'], 'source_scene.name', 1, 64)
    devs = doc.get('devices')
    if devs is not None:
        if not isinstance(devs, dict):
            _fail('devices', 'oggetto atteso')
        for alias, dev in devs.items():
            dp = _join('devices', alias)
            if not ALIAS_RE.match(alias):
                _fail(dp, 'alias non valido')
            _keys(dev, dp, ('label', 'input', 'output'))
            _str(dev['label'], _join(dp, 'label'), 1, 64)
            for direction in ('input', 'output'):
                desc = dev[direction]
                if desc is not None:
                    _keys(desc, _join(dp, direction), ('client', 'port'))
                    _str(desc['client'], _join(_join(dp, direction), 'client'), 1, 128)
                    _str(desc['port'], _join(_join(dp, direction), 'port'), 0, 128)

    warnings = []
    _list(doc['tracks'], 'tracks', 16)
    clip_ids = set()
    total_events = 0
    for i, tr in enumerate(doc['tracks']):
        tp = _join('tracks', i)
        _keys(tr, tp, TRACK_KEYS)
        if tr['id'] != i or not _is_int(tr['id']):
            _fail(_join(tp, 'id'), "deve essere uguale all'indice")
        _str(tr['name'], _join(tp, 'name'), 1, 32)
        _int_range(tr['color'], 0, 15, _join(tp, 'color'))
        for k in ('mute', 'solo'):
            if not isinstance(tr[k], bool):
                _fail(_join(tp, k), 'bool atteso')
        ep = _join(tp, 'events')
        _list(tr['events'], ep)
        last = -1.0
        for j, ev in enumerate(tr['events']):
            beat = _validate_event(ev, _join(ep, j))
            if beat < last:
                _fail(_join(_join(ep, j), 0), 'beat non ordinati')
            last = beat
        total_events += len(tr['events'])
        unpaired = _pair_fifo_unpaired(tr['events'])
        if unpaired:
            warnings.append({'code': 'UNPAIRED_NOTE_ON', 'details': {'track': i, 'count': unpaired}})

        cp = _join(tp, 'clips')
        _list(tr['clips'], cp)
        spans = []
        for j, c in enumerate(tr['clips']):
            cj = _join(cp, j)
            _keys(c, cj, CLIP_KEYS)
            _int_range(c['id'], 1, 2 ** 31, _join(cj, 'id'))
            if c['id'] in clip_ids:
                _fail(_join(cj, 'id'), 'ID clip duplicato nel progetto')
            clip_ids.add(c['id'])
            _num_range(c['start_beat'], 0, MAX_BEAT, _join(cj, 'start_beat'))
            _num_range(c['end_beat'], 0, MAX_BEAT, _join(cj, 'end_beat'))
            if c['end_beat'] <= c['start_beat']:
                _fail(_join(cj, 'end_beat'), 'end_beat > start_beat atteso')
            _str(c['name'], _join(cj, 'name'), 0, 64)
            if not _is_num(c['created_at']):
                _fail(_join(cj, 'created_at'), 'numero atteso')
            spans.append((c['start_beat'], c['end_beat'], c['id']))
        spans.sort()
        for (s1, e1, id1), (s2, _e2, id2) in zip(spans, spans[1:]):
            if s2 < e1:   # intervalli semiaperti: adiacenti non si sovrappongono
                warnings.append({'code': 'CLIP_OVERLAP_PRESENT',
                                 'details': {'track': i, 'clip_ids': [id1, id2]}})
    if total_events > MAX_EVENTS_TOTAL:
        _fail('tracks', f'oltre {MAX_EVENTS_TOTAL} eventi')
    nci = doc['next_clip_id']
    if not _is_int(nci) or nci < 1 or (clip_ids and nci <= max(clip_ids)):
        _fail('next_clip_id', 'maggiore di ogni ID clip atteso')
    return warnings


# ── Migrazione preset legacy -> scena v1 (sez. 4.3) ────────────────

def migrate_legacy_preset(data, file_stem):
    """Funzione pura: preset legacy (dict) -> (scena v1, percorsi riempiti da default)."""
    if not isinstance(data, dict):
        _fail('', 'oggetto atteso')
    defaulted = []
    parts = [default_part(n) for n in range(16)]
    listed = set()
    for k, ch in enumerate(data.get('channels') or []):
        cpath = f'channels[{k}]'
        if not isinstance(ch, dict):
            _fail(cpath, 'oggetto atteso')
        n = ch.get('channel')
        if n is None:
            continue                       # come api_presets_apply
        _int_range(n, 0, 15, _join(cpath, 'channel'))
        part = default_part(n)             # l'ultima voce per canale vince
        listed.add(n)
        if 'bank' in ch:
            _int_range(ch['bank'], 0, 16383, _join(cpath, 'bank'))
            part['sound']['bank'] = ch['bank']
        else:
            defaulted.append(f'parts[{n}].sound.bank')
        if 'program' in ch:
            _int_range(ch['program'], 0, 127, _join(cpath, 'program'))
            part['sound']['program'] = ch['program']
        else:
            defaulted.append(f'parts[{n}].sound.program')
        name = ch.get('instrument_name')
        if isinstance(name, str) and name and name != 'Not Set':
            part['sound']['name'] = name[:64]
        for field, key in LEGACY_CC_FIELDS:
            if field in ch:
                _int_range(ch[field], 0, 127, _join(cpath, field))
                part['cc'][key] = ch[field]
            else:
                defaulted.append(f'parts[{n}].cc.{key}')
        parts[n] = part
    for n in range(16):
        if n not in listed:
            defaulted.append(f'parts[{n}]')
    gs = data.get('global_settings') or {}
    glob = {}
    for key, default in (('gain', 1.0), ('reverb_level', 0.4), ('chorus_level', 0.4)):
        if key in gs:
            glob[key] = gs[key]
        else:
            glob[key] = default
            defaulted.append(f'globals.{key}')
    font = data.get('font')
    created = data.get('created', 'Unknown')
    scene = {
        'format': 'matsynth.scene',
        'schema_version': 1,
        'id': file_stem,
        'name': data.get('name') or file_stem,
        'created': created,
        'modified': created,
        'soundfont': {'file': font} if font else None,
        'globals': glob,
        'parts': parts,
    }
    validate_scene(scene)    # globali fuori limite -> VALIDATION_FAILED (nessun clamp)
    return scene, sorted(set(defaulted))
