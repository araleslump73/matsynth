"""project_store.py - scene, progetti, libreria e stato SoundFont (fase `ui-persistence`).

Contratto: docs/contracts.md sez. 4 (scene, migrazione legacy, stato SoundFont),
5 (progetto `.matsynth.json`, scrittura atomica, dirty), 7.3 (bank <-> SMF).

Funzioni pure, testabili senza importare app.py (niente Flask, niente mido,
niente hardware). Il validatore e la migrazione sono il porting di
`tests/contract_reference.py` (specifica eseguibile): stessi codici e percorsi
d'errore, piu' le regole D3 (ID clip per evento).

Scelte per il Raspberry Pi Zero 2 W (416 MiB):
- I file si scrivono a blocchi (`json.dumps` C su pezzi da 256 elementi):
  niente stringa dell'intero progetto in RAM, ma la velocita' dell'encoder C
  invece di quello Python di `json.dump` (che sul Pi costa secondi).
- `event_clips` e' `array('H')` (2 byte per evento): ID clip <= 65535.
- Nessun lock tenuto durante l'I/O: gli stati qui dentro hanno lock propri,
  brevissimi, e le notifiche (emit) partono DOPO il rilascio.
"""

import errno
import json
import math
import os
import re
import secrets
import threading
import time
from array import array

# ── Costanti di contratto ────────────────────────────────────────────────

SUPPORTED_SCENE_VERSION = 1
SUPPORTED_PROJECT_VERSION = 1
SCENE_FORMAT = 'matsynth.scene'
PROJECT_FORMAT = 'matsynth.project'
PROJECT_SUFFIX = '.matsynth.json'
CC_KEYS = ('7', '10', '71', '72', '73', '74', '75')
DEFAULT_CC = {'7': 100, '10': 64, '71': 64, '72': 64, '73': 64, '74': 64, '75': 64}
ID_RE = re.compile(r'^[\w-]{1,64}$')            # Unicode \w (stem legacy con accenti)
ALIAS_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
MAX_BEAT = 100000
# Limiti da confermare con la misura sul Pi (docs/pi-measurement.md, D21).
MAX_EVENTS_TOTAL = 120000
MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_SCENE_BYTES = 256 * 1024
# event_clips e' array('H'): un ID clip deve stare in 16 bit.
MAX_CLIP_ID = 65535
CLIP_TAIL_BEATS = 0.125                         # margine clip di take/import (sez. 6.4)
DEFAULT_INPUT_ALIAS = 'keyboard-main'
DEFAULT_GLOBALS = {'gain': 1.0, 'reverb_level': 0.4, 'chorus_level': 0.4}
UNTITLED_PROJECT = 'Senza titolo'
WRITE_CHUNK = 256                               # elementi per blocco di scrittura

# Nome campo legacy -> chiave CC (stessa mappa di app.py:control)
LEGACY_CC_FIELDS = (('volume', '7'), ('pan', '10'), ('resonance', '71'), ('release', '72'),
                    ('attack', '73'), ('cutoff', '74'), ('decay', '75'))


class ContractError(Exception):
    """Documento non valido: `code` in VALIDATION_FAILED / UNSUPPORTED_VERSION,
    `path` nella forma di `error.details.path` (es. "parts[3].cc.75")."""

    def __init__(self, code, path, reason):
        super().__init__(f'{code} at {path}: {reason}')
        self.code = code
        self.path = path
        self.reason = reason

    def details(self):
        return {'path': self.path, 'reason': self.reason}


class StoreError(Exception):
    """Errore di I/O o di stato (DISK_FULL, DISK_ERROR, NOT_FOUND, ...)."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


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


def now_str():
    """Orario informativo (sez. 1.4): il Pi non ha RTC, nessuna logica ci dipende."""
    return time.strftime('%Y-%m-%d %H:%M:%S')


def new_scene_id():
    return 's-' + secrets.token_hex(6)


def new_project_id():
    return 'p-' + secrets.token_hex(6)


# ── Parte (sez. 3.1) ────────────────────────────────────────────────────
# NB: midi_router.validate_part applica le stesse regole (errore RouterError);
# tests/test_persistence_store.py verifica che i due validatori concordino.

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


# ── Corpo sonoro comune a scena e progetto (sez. 4.1) ───────────────────

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
    _check_format_version(doc, SCENE_FORMAT, SUPPORTED_SCENE_VERSION)
    _keys(doc, '', SCENE_KEYS)
    if not isinstance(doc['id'], str) or not ID_RE.match(doc['id']):
        _fail('id', 'ID non valido')
    _str(doc['name'], 'name', 1, 64)
    _str(doc['created'], 'created', 0, 32)
    _str(doc['modified'], 'modified', 0, 32)
    _validate_sound_body(doc, '')


# ── Accoppiamento e ownership (sez. 6.2, 6.3) ───────────────────────────
# Lavorano sui byte grezzi (status & 0xF0): nessun mido.Message per evento,
# cosi' 16000 eventi si scorrono in pochi ms anche sul Pi.

def _is_note_on(data):
    return (data[0] & 0xF0) == 0x90 and len(data) > 2 and data[2] > 0


def _is_note_off(data):
    kind = data[0] & 0xF0
    return len(data) > 2 and (kind == 0x80 or (kind == 0x90 and data[2] == 0))


def pair_notes(events):
    """Regola UNICA di accoppiamento (sez. 6.2): FIFO per altezza.

    `events`: sequenza di (beat, bytes) in ordine. Ritorna la lista
    [(note, vel, on_beat, off_beat, on_idx, off_idx)] nell'ordine dei note-off,
    poi le note aperte (off_beat = None, off_idx = -1) nell'ordine dei note-on.
    """
    open_notes = {}
    pairs = []
    for idx, ev in enumerate(events):
        data = ev[1]
        if not data:
            continue
        kind = data[0] & 0xF0
        if kind == 0x90 and len(data) > 2 and data[2] > 0:
            open_notes.setdefault(data[1], []).append((ev[0], data[2], idx))
        elif len(data) > 2 and (kind == 0x80 or kind == 0x90):
            pending = open_notes.get(data[1])
            if pending:
                on_beat, vel, on_idx = pending.pop(0)
                pairs.append((data[1], vel, on_beat, ev[0], on_idx, idx))
    opened = []
    for note, pending in open_notes.items():
        for on_beat, vel, on_idx in pending:
            opened.append((note, vel, on_beat, None, on_idx, -1))
    opened.sort(key=lambda n: n[4])
    return pairs + opened


def _pair_fifo_unpaired(events):
    """Conta i note-on senza note-off con la regola FIFO per altezza (sez. 6.2)."""
    return sum(1 for p in pair_notes([(ev[0], ev[1]) for ev in events]) if p[5] < 0)


def owned_indices(events, start, end):
    """Indici posseduti da [start, end) secondo sez. 6.3: note con note-on nell'intervallo
    (con il loro note-off, accoppiato FIFO per altezza) + eventi non-nota nell'intervallo."""
    owned = set()
    open_notes = {}
    for idx, ev in enumerate(events):
        beat, data = ev[0], ev[1]
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


def migrate_event_clips(events, clips):
    """Migrazione D3 (sez. 6.4): eventi senza clip_id -> ID per intervalli,
    clip per clip in ordine di ID crescente; un evento gia' assegnato non
    cambia clip. Ritorna array('H') parallelo a `events`."""
    ids = array('H', bytes(2 * len(events)))
    for c in sorted(clips, key=lambda c: c['id']):
        for idx in owned_indices(events, c['start_beat'], c['end_beat']):
            if not ids[idx]:
                ids[idx] = c['id']
    return ids


# ── Progetto (sez. 5) ───────────────────────────────────────────────────

PROJECT_KEYS = ('format', 'schema_version', 'id', 'name', 'created', 'modified',
                'transport', 'sound', 'tracks', 'next_clip_id')
PROJECT_OPTIONAL_KEYS = ('devices', 'source_scene')
TRACK_KEYS = ('id', 'name', 'color', 'mute', 'solo', 'events', 'clips')
CLIP_KEYS = ('id', 'start_beat', 'end_beat', 'name', 'created_at')


def _validate_event(ev, path):
    """`[beat, bytes]` o `[beat, bytes, clip_id]` (D3). Ritorna (beat, clip_id)."""
    # Tuple ammesse: e' la forma in memoria (validazione prima di scrivere).
    if not isinstance(ev, (list, tuple)) or len(ev) not in (2, 3):
        _fail(path, '[beat, bytes] o [beat, bytes, clip_id] atteso')
    beat, data = ev[0], ev[1]
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
    cid = 0
    if len(ev) == 3:
        cid = ev[2]
        if not _is_int(cid) or not 0 <= cid <= MAX_CLIP_ID:
            _fail(_join(path, 2), f'clip_id intero 0..{MAX_CLIP_ID} atteso')
    return beat, cid


def _warning(code, message, details):
    return {'code': code, 'message': message, 'details': details}


def validate_project(doc):
    """Valida un progetto; ritorna la lista dei warning (codici sez. 2.4).

    Warning (mai rifiuto, sez. 5.3): UNPAIRED_NOTE_ON, CLIP_OVERLAP_PRESENT
    (solo se il documento non porta ID clip per evento e va migrato).
    """
    _check_format_version(doc, PROJECT_FORMAT, SUPPORTED_PROJECT_VERSION)
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
    uses_clip_ids = False
    overlaps = []
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
        total_events += len(tr['events'])
        if total_events > MAX_EVENTS_TOTAL:
            _fail('tracks', f'oltre {MAX_EVENTS_TOTAL} eventi')
        last = -1.0
        refs = []                                 # (indice evento, clip_id != 0)
        for j, ev in enumerate(tr['events']):
            beat, cid = _validate_event(ev, _join(ep, j))
            if beat < last:
                _fail(_join(_join(ep, j), 0), 'beat non ordinati')
            last = beat
            if len(ev) == 3:
                uses_clip_ids = True
            if cid:
                refs.append((j, cid))
        unpaired = _pair_fifo_unpaired(tr['events'])
        if unpaired:
            warnings.append(_warning('UNPAIRED_NOTE_ON', 'Note-on senza note-off',
                                     {'track': i, 'count': unpaired}))

        cp = _join(tp, 'clips')
        _list(tr['clips'], cp)
        spans = []
        track_ids = set()
        for j, c in enumerate(tr['clips']):
            cj = _join(cp, j)
            _keys(c, cj, CLIP_KEYS)
            _int_range(c['id'], 1, MAX_CLIP_ID, _join(cj, 'id'))
            if c['id'] in clip_ids:
                _fail(_join(cj, 'id'), 'ID clip duplicato nel progetto')
            clip_ids.add(c['id'])
            track_ids.add(c['id'])
            _num_range(c['start_beat'], 0, MAX_BEAT, _join(cj, 'start_beat'))
            _num_range(c['end_beat'], 0, MAX_BEAT, _join(cj, 'end_beat'))
            if c['end_beat'] <= c['start_beat']:
                _fail(_join(cj, 'end_beat'), 'end_beat > start_beat atteso')
            _str(c['name'], _join(cj, 'name'), 0, 64)
            if not _is_num(c['created_at']):
                _fail(_join(cj, 'created_at'), 'numero atteso')
            spans.append((c['start_beat'], c['end_beat'], c['id']))
        for j, cid in refs:
            if cid not in track_ids:
                _fail(_join(_join(ep, j), 2), 'clip_id non corrisponde a un clip della traccia')
        spans.sort()
        for (s1, e1, id1), (s2, _e2, id2) in zip(spans, spans[1:]):
            if s2 < e1:   # intervalli semiaperti: adiacenti non si sovrappongono
                overlaps.append((i, id1, id2))
    nci = doc['next_clip_id']
    if not _is_int(nci) or nci < 1 or nci > MAX_CLIP_ID + 1 or (clip_ids and nci <= max(clip_ids)):
        _fail('next_clip_id', 'maggiore di ogni ID clip atteso')
    if not uses_clip_ids:
        # Documento senza ID clip per evento (prima di D3): l'apertura assegna
        # gli ID per intervalli (6.4); con clip sovrapposti l'esito va segnalato.
        for i, id1, id2 in overlaps:
            warnings.append(_warning('CLIP_OVERLAP_PRESENT',
                                     'Clip sovrapposti senza ID per evento: assegnati per intervalli',
                                     {'track': i, 'clip_ids': [id1, id2]}))
    return warnings


def project_uses_clip_ids(doc):
    return any(len(ev) == 3 for tr in doc['tracks'] for ev in tr['events'])


def project_to_session(doc):
    """Documento progetto VALIDATO -> contenuto di sessione per
    MultiTrackDAW.load_session (nessun effetto sulla DAW: la sessione
    corrente resta intatta finche' il chiamante non fa lo scambio).

    Le liste di byte del documento sono riusate (immutabili per contratto,
    sez. 5.2): nessuna copia evento per evento oltre alla tupla.
    """
    migrate = not project_uses_clip_ids(doc)
    tracks, event_clips, clips = {}, {}, {}
    names, colors, muted, solo = {}, {}, {}, {}
    for tr in doc['tracks']:
        t = tr['id']
        evs = [(float(ev[0]), ev[1]) for ev in tr['events']]
        cl = [{'id': c['id'], 'start_beat': float(c['start_beat']), 'end_beat': float(c['end_beat']),
               'name': c['name'], 'created_at': c['created_at']} for c in tr['clips']]
        if migrate:
            ids = migrate_event_clips(evs, cl)
        else:
            ids = array('H', [ev[2] if len(ev) == 3 else 0 for ev in tr['events']])
        tracks[t], event_clips[t], clips[t] = evs, ids, cl
        names[t], colors[t] = tr['name'], tr['color']
        muted[t], solo[t] = tr['mute'], tr['solo']
    tp = doc['transport']
    return {
        'tracks': tracks, 'event_clips': event_clips, 'clips': clips,
        'track_names': names, 'track_colors': colors, 'muted': muted, 'solo': solo,
        'bpm': tp['bpm'], 'beats_per_measure': tp['beats_per_measure'],
        'loop_enabled': tp['loop']['enabled'],
        'loop_start': float(tp['loop']['start_beat']), 'loop_end': float(tp['loop']['end_beat']),
        'next_clip_id': doc['next_clip_id'],
    }


def build_project_doc(session, sound, meta):
    """Sessione DAW (snapshot superficiale di MultiTrackDAW.snapshot_session)
    + snapshot sonoro + metadati -> documento progetto v1.

    Il writer scrive il terzo elemento dell'evento solo se il clip_id e' != 0 e
    corrisponde a un clip della traccia (difesa: mai un riferimento pendente
    che impedirebbe la riapertura). Il nibble di canale viene normalizzato
    all'ID traccia solo per gli eventi che differiscono (nuova lista solo li').
    """
    tracks = []
    max_id = 0
    for t in range(16):
        evs = session['tracks'].get(t) or []
        ids = session['event_clips'].get(t)
        cl = session['clips'].get(t) or []
        valid = {int(c['id']) for c in cl}
        out = []
        for i, (beat, data) in enumerate(evs):
            if (data[0] & 0x0F) != t:
                data = [(data[0] & 0xF0) | t] + list(data[1:])
            cid = ids[i] if ids is not None and i < len(ids) else 0
            out.append((beat, data, cid) if cid and cid in valid else (beat, data))
        clips = []
        for c in sorted(cl, key=lambda c: (c['start_beat'], c['id'])):
            cid = int(c['id'])
            max_id = max(max_id, cid)
            clips.append({'id': cid, 'start_beat': float(c['start_beat']), 'end_beat': float(c['end_beat']),
                          'name': str(c.get('name') or '')[:64],
                          'created_at': float(c.get('created_at') or 0.0)})
        name = str(session['track_names'].get(t) or f'Track {t + 1}')[:32] or f'Track {t + 1}'
        tracks.append({'id': t, 'name': name, 'color': int(session['track_colors'].get(t, t)),
                       'mute': bool(session['muted'].get(t)), 'solo': bool(session['solo'].get(t)),
                       'events': out, 'clips': clips})
    doc = {
        'format': PROJECT_FORMAT,
        'schema_version': SUPPORTED_PROJECT_VERSION,
        'id': meta['id'],
        'name': meta['name'],
        'created': meta.get('created') or now_str(),
        'modified': meta.get('modified') or now_str(),
        'transport': {'bpm': session['bpm'], 'beats_per_measure': session['beats_per_measure'],
                      'loop': {'enabled': bool(session['loop_enabled']),
                               'start_beat': float(session['loop_start']),
                               'end_beat': float(session['loop_end'])}},
        'sound': sound,
        'source_scene': meta.get('source_scene'),
        'tracks': tracks,
        'next_clip_id': max(int(session['next_clip_id']), max_id + 1),
    }
    if meta.get('devices'):
        doc['devices'] = meta['devices']
    return doc


def devices_for_parts(parts, devices):
    """Descrittori informativi degli alias usati dalle parti (sez. 5.1 `devices`)."""
    used = set()
    for p in parts:
        if p['input']['device_alias']:
            used.add(p['input']['device_alias'])
        if p['destination']['device_alias']:
            used.add(p['destination']['device_alias'])
    out = {}
    for d in devices:
        if d.get('alias') in used:
            label = str(d.get('label') or d['alias'])[:64] or d['alias']
            out[d['alias']] = {'label': label, 'input': d.get('input'), 'output': d.get('output')}
    return out


# ── Scene: migrazione legacy e adattatore inverso (sez. 4.2, 4.3) ───────

def classify_preset(data):
    """'v1' | 'legacy' | 'invalid' (rilevamento di sez. 4.2)."""
    if not isinstance(data, dict):
        return 'invalid'
    if data.get('format') == SCENE_FORMAT:
        return 'v1'
    if 'format' not in data and any(k in data for k in ('channels', 'global_settings', 'font')):
        return 'legacy'
    return 'invalid'


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
        'format': SCENE_FORMAT,
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


def scene_to_legacy_preset(scene):
    """Scena v1 -> forma legacy per presets.html (sez. 4.2): solo parti interne.
    `channel` = ID parte (come la chiave legacy `channels` di last_state)."""
    channels = []
    for p in scene['parts']:
        if p['destination']['kind'] != 'internal':
            continue
        cc = p['cc']
        channels.append({
            'channel': p['part_id'], 'bank': p['sound']['bank'], 'program': p['sound']['program'],
            'volume': cc['7'], 'pan': cc['10'], 'attack': cc['73'], 'release': cc['72'],
            'decay': cc['75'], 'cutoff': cc['74'], 'resonance': cc['71'],
            'instrument_name': p['sound']['name'] or 'Not Set',
        })
    g = scene['globals']
    return {'name': scene['name'], 'created': scene['created'],
            'font': scene['soundfont']['file'] if scene['soundfont'] else '',
            'channels': channels,
            'global_settings': {'gain': g['gain'], 'reverb_level': g['reverb_level'],
                                'chorus_level': g['chorus_level']}}


def make_scene(scene_id, name, parts, soundfont_file, globals_, created=None):
    """Scena v1 dallo stato corrente (cattura 4.4). Valida prima di ritornare."""
    ts = now_str()
    scene = {'format': SCENE_FORMAT, 'schema_version': SUPPORTED_SCENE_VERSION,
             'id': scene_id, 'name': name, 'created': created if created is not None else ts,
             'modified': ts,
             'soundfont': {'file': soundfont_file} if soundfont_file else None,
             'globals': {'gain': globals_['gain'], 'reverb_level': globals_['reverb_level'],
                         'chorus_level': globals_['chorus_level']},
             'parts': parts}
    validate_scene(scene)
    return scene


def defaulted_warnings(paths):
    if not paths:
        return []
    return [_warning('LEGACY_FIELD_DEFAULTED',
                     'Campi assenti nel preset originale: verranno usati i valori di default',
                     {'paths': list(paths)})]


# ── Conversione bank FluidSynth <-> SMF (sez. 7.3, D5) ──────────────────
# Convenzione assunta: FluidSynth `synth.midi-bank-select = gs` (default, non
# cambiato da startfluid.sh): CC0 = numero di bank, CC32 ignorato, canale 10
# batteria. DA VERIFICARE CON FLUIDSYNTH REALE (D5).

def fluid_bank_to_smf(bank, channel):
    """Bank FluidSynth di una parte interna -> (bank14 neutro | None, warning | None)."""
    if 0 <= bank <= 127:
        return bank << 7, None
    if bank == 128 and channel == 9:
        return 0, None
    return None, 'BANK_NOT_REPRESENTABLE'


def smf_to_fluid_bank(bank14, channel):
    """Bank neutro a 14 bit importato -> bank FluidSynth di una parte interna."""
    if channel == 9:
        return 128
    return ((bank14 or 0) >> 7) & 0x7F


def external_sound_to_smf(sound):
    """Suono esterno -> (bank14 | None, program | None) (sez. 7.3)."""
    msb, lsb = sound.get('bank_msb'), sound.get('bank_lsb')
    bank = None if msb is None and lsb is None else ((msb or 0) << 7) | (lsb or 0)
    return bank, sound.get('program')


def smf_to_external_sound(sound, bank14, program):
    out = dict(sound)
    if bank14 is not None:
        out['bank_msb'] = (bank14 >> 7) & 0x7F
        out['bank_lsb'] = bank14 & 0x7F
    if program is not None:
        out['program'] = program
    return out


# ── I/O atomico (sez. 5.5) ──────────────────────────────────────────────

def _dump_stream(obj, write, depth=0):
    """JSON compatto scritto a blocchi: dizionari e liste lunghe vengono
    attraversati, il resto serializzato dall'encoder C a pezzi piccoli."""
    if isinstance(obj, dict) and depth < 4:
        write('{')
        first = True
        for k, v in obj.items():
            if not first:
                write(',')
            first = False
            write(json.dumps(str(k), ensure_ascii=False))
            write(':')
            _dump_stream(v, write, depth + 1)
        write('}')
    elif isinstance(obj, (list, tuple)) and len(obj) > WRITE_CHUNK:
        write('[')
        for i in range(0, len(obj), WRITE_CHUNK):
            chunk = json.dumps(obj[i:i + WRITE_CHUNK], separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
            if i:
                write(',')
            write(chunk[1:-1])
        write(']')
    elif isinstance(obj, (list, tuple)) and depth < 4 and any(isinstance(x, dict) for x in obj):
        write('[')
        for i, x in enumerate(obj):
            if i:
                write(',')
            _dump_stream(x, write, depth + 1)
        write(']')
    else:
        write(json.dumps(obj, separators=(',', ':'), ensure_ascii=False, allow_nan=False))


def _fsync_dir(d):
    if hasattr(os, 'O_DIRECTORY'):          # fsync della directory solo su POSIX
        try:
            fd = os.open(d, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def atomic_write_json(path, obj):
    """Unica funzione di scrittura dei file nuovi (sez. 5.5): temporaneo unico
    nella stessa cartella, JSON compatto a blocchi, flush + fsync, os.replace,
    fsync della cartella. Su qualunque errore il temporaneo viene rimosso e il
    file precedente resta intatto; solleva StoreError DISK_FULL/DISK_ERROR."""
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, f'.{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            _dump_stream(obj, f.write)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(d)
    except BaseException as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, OSError):
            code = 'DISK_FULL' if e.errno == errno.ENOSPC else 'DISK_ERROR'
            raise StoreError(code, f'Scrittura di {os.path.basename(path)} fallita: {e}',
                             {'file': os.path.basename(path)}) from e
        if isinstance(e, (ValueError, TypeError)):
            raise StoreError('INTERNAL', f'Dati non serializzabili: {e}') from e
        raise


def read_json_file(path, max_bytes):
    """Legge un JSON con limite di dimensione. File assente -> StoreError
    NOT_FOUND; troppo grande o JSON illeggibile -> ContractError
    VALIDATION_FAILED; altri errori di I/O -> StoreError DISK_ERROR."""
    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            _fail('', 'file troppo grande')
        with open(path, 'rb') as f:
            raw = f.read(max_bytes + 1)
    except FileNotFoundError:
        raise StoreError('NOT_FOUND', 'File non trovato', {'file': os.path.basename(path)})
    except OSError as e:
        raise StoreError('DISK_ERROR', f'Lettura fallita: {e}', {'file': os.path.basename(path)})
    return parse_json_bytes(raw, max_bytes)


def parse_json_bytes(raw, max_bytes):
    if len(raw) > max_bytes:
        _fail('', 'file troppo grande')
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        _fail('', f'JSON non valido: {e}')


def file_stat(path):
    """(mtime_ns, size) per il controllo "file cambiato" (sez. 5.6, D17)."""
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def safe_file_name(name, fallback):
    s = ''.join(c for c in str(name) if c.isalnum() or c in (' ', '-', '_')).strip().replace(' ', '_')
    return s or fallback


# ── Libreria (preferiti/ordine scene, sez. 4.1) ─────────────────────────

def default_library():
    return {'schema_version': 1, 'scene_favorites': [], 'scene_order': []}


def load_library(path):
    """library.json o default. Un file illeggibile non blocca la libreria
    (i preferiti sono metadati, non dati musicali): si riparte dai default."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return default_library()
    lib = default_library()
    if isinstance(doc, dict):
        for k in ('scene_favorites', 'scene_order'):
            v = doc.get(k)
            if isinstance(v, list):
                lib[k] = [x for x in v if isinstance(x, str) and ID_RE.match(x)]
    return lib


def library_set_meta(lib, scene_id, favorite=None, order=None):
    favs = [x for x in lib['scene_favorites'] if x != scene_id]
    if favorite is True or (favorite is None and scene_id in lib['scene_favorites']):
        favs.append(scene_id)
    lib['scene_favorites'] = favs
    if order is not None:
        seq = [x for x in lib['scene_order'] if x != scene_id]
        seq.insert(max(0, min(len(seq), order)), scene_id)
        lib['scene_order'] = seq
    return lib


def library_remove(lib, scene_id):
    lib['scene_favorites'] = [x for x in lib['scene_favorites'] if x != scene_id]
    lib['scene_order'] = [x for x in lib['scene_order'] if x != scene_id]
    return lib


def sort_scene_entries(entries, lib):
    """Ordina per `order` (indice in scene_order) poi per nome."""
    pos = {sid: i for i, sid in enumerate(lib['scene_order'])}
    favs = set(lib['scene_favorites'])
    for e in entries:
        e['order'] = pos.get(e['id'])
        e['favorite'] = e['id'] in favs
    entries.sort(key=lambda e: (e['order'] is None, e['order'] if e['order'] is not None else 0,
                                str(e.get('name') or '').casefold()))
    return entries


# ── Stati osservabili (scena corrente, progetto, SoundFont) ─────────────

class _Observable:
    """Stato con lock proprio; la notifica (`on_change(public_dict)`) parte
    solo quando un campo pubblico cambia e sempre FUORI dal lock."""

    def __init__(self, on_change=None):
        self._lock = threading.Lock()
        self.on_change = on_change

    def _public_locked(self):
        raise NotImplementedError

    def public(self):
        with self._lock:
            return self._public_locked()

    def _notify(self, before, after):
        if before != after and self.on_change is not None:
            try:
                self.on_change(after)
            except Exception as e:      # una notifica fallita non deve rompere la mutazione
                print(f'[Store] notifica fallita: {e}')


class SceneState(_Observable):
    """Scena corrente e flag "modificata" (sez. 4.7)."""

    def __init__(self, on_change=None):
        super().__init__(on_change)
        self.scene_id = None
        self.name = None
        self.dirty = False

    def _public_locked(self):
        return {'scene_id': self.scene_id, 'name': self.name, 'dirty': self.dirty}

    def restore(self, data):
        """Ripristino all'avvio da last_state.json `scene` (nessuna notifica)."""
        if isinstance(data, dict):
            sid = data.get('scene_id')
            name = data.get('name')
            with self._lock:
                self.scene_id = sid if isinstance(sid, str) and ID_RE.match(sid) else None
                self.name = name if isinstance(name, str) else None
                self.dirty = bool(data.get('dirty'))

    def set(self, scene_id, name, dirty):
        with self._lock:
            before = self._public_locked()
            self.scene_id, self.name, self.dirty = scene_id, name, bool(dirty)
            after = self._public_locked()
        self._notify(before, after)
        return after

    def mark_dirty(self):
        with self._lock:
            if self.dirty:
                return
            before = self._public_locked()
            self.dirty = True
            after = self._public_locked()
        self._notify(before, after)


class ProjectState(_Observable):
    """Progetto corrente e flag "modificato" (sez. 5.6).

    `gen` cresce a ogni mark_dirty: Salva azzera `dirty` solo se nessuna
    mutazione e' arrivata tra lo snapshot e la fine della scrittura.
    """

    def __init__(self, on_change=None):
        super().__init__(on_change)
        self.id = None
        self.name = UNTITLED_PROJECT
        self.dirty = False
        self.file_stat = None
        self.created = None
        self.source_scene = None
        self.gen = 0

    def _public_locked(self):
        return {'id': self.id, 'name': self.name, 'dirty': self.dirty,
                'file_stat': list(self.file_stat) if self.file_stat else None}

    def snapshot(self):
        with self._lock:
            return {'id': self.id, 'name': self.name, 'dirty': self.dirty,
                    'file_stat': self.file_stat, 'created': self.created,
                    'source_scene': self.source_scene, 'gen': self.gen}

    def set(self, id_, name, dirty, file_stat=None, created=None, source_scene=None, if_gen=None):
        """Imposta lo stato. Con `if_gen`, `dirty=False` viene applicato solo
        se nessuna mutazione e' avvenuta dopo lo snapshot con quel `gen`."""
        with self._lock:
            before = self._public_locked()
            self.id, self.name = id_, name
            self.file_stat = list(file_stat) if file_stat else None
            self.created = created
            self.source_scene = source_scene
            if if_gen is not None and not dirty and self.gen != if_gen:
                self.dirty = True
            else:
                self.dirty = bool(dirty)
            after = self._public_locked()
        self._notify(before, after)
        return after

    def update(self, **fields):
        with self._lock:
            before = self._public_locked()
            for k, v in fields.items():
                setattr(self, k, v)
            after = self._public_locked()
        self._notify(before, after)
        return after

    def mark_dirty(self):
        with self._lock:
            self.gen += 1
            if self.dirty:
                return
            before = self._public_locked()
            self.dirty = True
            after = self._public_locked()
        self._notify(before, after)


SF_STATES = ('idle', 'loading', 'loaded', 'failed', 'unknown')


class SoundfontState(_Observable):
    """Stato del caricamento SoundFont (sez. 4.6): un job alla volta."""

    def __init__(self, on_change=None):
        super().__init__(on_change)
        self._s = {'state': 'idle', 'file': None, 'sf_id': None, 'error': None,
                   'started_at': None, 'finished_at': None}

    def _public_locked(self):
        return dict(self._s)

    def begin(self, file):
        """Passa a `loading`; False se un caricamento e' gia' in corso."""
        with self._lock:
            if self._s['state'] == 'loading':
                return False
            before = self._public_locked()
            self._s = {'state': 'loading', 'file': file, 'sf_id': None, 'error': None,
                       'started_at': now_str(), 'finished_at': None}
            after = self._public_locked()
        self._notify(before, after)
        return True

    def finish(self, state, file=None, sf_id=None, error=None):
        with self._lock:
            before = self._public_locked()
            started = self._s.get('started_at') if self._s['state'] == 'loading' else None
            self._s = {'state': state, 'file': file, 'sf_id': sf_id, 'error': error,
                       'started_at': started or now_str(), 'finished_at': now_str()}
            after = self._public_locked()
        self._notify(before, after)
        return after

    def loaded_file(self):
        with self._lock:
            return self._s['file'] if self._s['state'] == 'loaded' else None

    def is_loading(self):
        with self._lock:
            return self._s['state'] == 'loading'


def parse_fonts_output(raw):
    """Righe di `fonts` della console FluidSynth -> [(id, percorso)]."""
    out = []
    for line in (raw or '').split('\n'):
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((parts[0], parts[1].strip()))
    return out


def verify_font_loaded(raw, path):
    """sf_id del font `path` se l'output di `fonts` lo contiene, altrimenti None.
    Confronto sul percorso completo o, in mancanza, sul nome base del file."""
    base = os.path.basename(path)
    for fid, fpath in parse_fonts_output(raw):
        if fpath == path or path in fpath or os.path.basename(fpath) == base:
            return fid
    return None
