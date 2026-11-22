"""Busta standard delle risposte JSON e degli ack Socket.IO (docs/contracts.md sez. 2).

Funzioni pure: niente Flask qui dentro, cosi' sono testabili senza importare
app.py. Le route usano `respond(...)` che ritorna la tupla (dict, http) che
Flask converte in JSON.
"""

ERROR_HTTP = {
    'BAD_REQUEST': 400,
    'NOTHING_TO_UNDO': 400,
    'NO_ARMED_TRACK': 400,
    'NOTHING_TO_PLAY': 400,
    'NOT_FOUND': 404,
    'NAME_EXISTS': 409,
    'TRANSPORT_ACTIVE': 409,
    'RECORDING_ACTIVE': 409,
    'REVISION_CONFLICT': 409,
    'UNSAVED_CHANGES': 409,
    'SOUNDFONT_CHANGE_REQUIRED': 409,
    'SOUNDFONT_MISSING': 409,
    'SOUNDFONT_LOADING': 409,
    'PORTS_UNAVAILABLE': 409,
    'CLIP_OVERLAP': 409,
    'CLIP_RESIZE_WOULD_EXCLUDE': 409,
    'DEST_NOT_EMPTY': 409,
    'FILE_CHANGED': 409,
    'NO_CURRENT_FILE': 409,
    'DEVICE_IN_USE': 409,
    'EMPTY_CLIPBOARD': 409,
    'NOT_INTERNAL_PART': 409,
    'VALIDATION_FAILED': 422,
    'UNSUPPORTED_VERSION': 422,
    'IMPORT_NEEDS_CONVERSION': 422,
    'PAYLOAD_TOO_LARGE': 413,
    'SYNTH_UNAVAILABLE': 503,
    'PORT_UNAVAILABLE': 503,
    'PARTIAL_APPLY': 503,
    'DISK_FULL': 500,
    'DISK_ERROR': 500,
    'INTERNAL': 500,
}


def ok(message=None, warnings=None, **payload):
    """Successo: `ok`, `status` e payload al primo livello."""
    body = {'ok': True, 'status': 'ok'}
    if message is not None:
        body['message'] = message
    if warnings:
        body['warnings'] = list(warnings)
    body.update(payload)
    return body


def err(code, message, details=None, http=None, **payload):
    """Errore: ritorna (body, http). `http` esplicito solo per gli override
    legacy di sez. 8.1; altrimenti la tabella 2.3 (codice sconosciuto -> 500)."""
    if http is None:
        http = ERROR_HTTP.get(code, 500)
    body = {'ok': False, 'status': 'error', 'message': message,
            'error': {'code': code, 'message': message,
                      'details': dict(details) if details else {}}}
    body.update(payload)
    return body, http


def ack_ok(message=None, warnings=None, **payload):
    """Ack Socket.IO di successo: stessa busta delle route (sez. 2.5)."""
    return ok(message, warnings, **payload)


def ack_err(code, message, details=None, **payload):
    """Ack Socket.IO d'errore: solo il body (gli ack non hanno codice HTTP)."""
    return err(code, message, details, **payload)[0]


def from_router_error(e, http=None):
    """Converte un midi_router.RouterError nella busta d'errore."""
    return err(e.code, e.message, e.details, http=http)
