"""F13: locally durable state transaction and ordered Telegram intentions.

state.json is authoritative; signals/oi/watch are repairable projections.
No Telegram token is persisted. Uncertain delivery requires separate review.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

# A4: a durable session commits authoritative state BEFORE the local mirror.
durable_writer = ContextVar('durable_writer', default=None)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def identity(kind, event, sequence, payload):
    return hashlib.sha256(canonical([kind, event, sequence, payload]).encode()).hexdigest()


def atomic_json(path, value):
    """Replace one file after syncing it; old or new complete JSON survives a crash."""
    path = Path(path)
    data = canonical(value)
    writer = durable_writer.get()
    if writer is not None:
        writer(path, value)
    fd, temp = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def engine_lock(directory):
    key = hashlib.sha256(os.path.normcase(str(Path(directory).resolve())).encode()).hexdigest()
    lock = Path(tempfile.gettempdir())/('btc-engine-'+key+'.lock')
    with lock.open('a+b') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Engine-Datenpfad bereits in Bearbeitung') from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Engine-Datenpfad bereits in Bearbeitung') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def load_delivery(state):
    if '_delivery' not in state:
        return dict(version=1, messages=[])
    delivery = deepcopy(state['_delivery'])
    if delivery.get('version') != 1 or not isinstance(delivery.get('messages'), list):
        raise ValueError('Unbekanntes/unvollstaendiges Versandschema')
    if not all(k in delivery for k in ('signals', 'oi_history')):
        raise ValueError('Versandtransaktion ohne Projektionen')
    if not isinstance(delivery['signals'], dict) or not isinstance(delivery['signals'].get('signals'), list) or not isinstance(delivery['oi_history'], list):
        raise ValueError('Ungueltige Versandprojektionen')
    if 'watch_resolution' in delivery and not isinstance(delivery['watch_resolution'], dict):
        raise ValueError('Ungueltige Flush-Aufloesung')
    seen = set()
    for m in delivery['messages']:
        if not isinstance(m, dict) or not all(k in m for k in ('id', 'kind', 'event', 'sequence', 'payload', 'text', 'text_sha256',
                                    'status', 'attempts', 'target', 'receipt', 'reason')):
            raise ValueError('Unvollstaendige Versandabsicht')
        if m['id'] != identity(m['kind'], m['event'], m['sequence'], m['payload']) or m['id'] in seen:
            raise ValueError('Ungueltige/doppelte Nachrichtenidentitaet')
        seen.add(m['id'])
        if m['status'] not in {'pending', 'sending', 'rejected', 'confirmed', 'uncertain', 'preview'}:
            raise ValueError('Unbekannter Versandstatus')
        if not isinstance(m['text'], str) or not m['text'] or type(m['attempts']) is not int or m['attempts'] < 0:
            raise ValueError('Ungueltige Versandabsicht')
        if m['text_sha256'] != hashlib.sha256(m['text'].encode()).hexdigest():
            raise ValueError('Veraenderter Nachrichtentext')
        if m['status'] in {'sending', 'rejected', 'confirmed', 'uncertain'} and (m['attempts'] < 1 or not isinstance(m['target'], str) or len(m['target']) != 64):
            raise ValueError('Versuch ohne gebundenes Ziel')
        if m['status'] == 'confirmed' and (type(m['receipt']) is not int or m['receipt'] <= 0):
            raise ValueError('Bestaetigung ohne Telegram-Beleg')
    return delivery


def enqueue(delivery, kind, event, sequence, payload, text, preview=False):
    mid = identity(kind, event, sequence, payload)
    existing = next((m for m in delivery['messages'] if m['id'] == mid), None)
    if existing:
        if existing['text'] != text:
            raise ValueError('Textkonflikt derselben Nachrichtenidentitaet')
        return
    if not text:
        raise ValueError('Leere Versandnachricht')
    delivery['messages'].append(dict(id=mid, kind=kind, event=event, sequence=sequence,
        payload=deepcopy(payload), text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        status='preview' if preview else 'pending',
        attempts=0, target=None, receipt=None, reason=None))


def recover(state, state_path):
    delivery = load_delivery(state)
    changed = False
    for m in delivery['messages']:
        if m['status'] == 'sending':
            m['status'], m['reason'] = 'uncertain', 'process_interrupted'
            changed = True
    if changed:
        state['_delivery'] = delivery
        atomic_json(state_path, state)
    return delivery


def project(state, directory):
    d = load_delivery(state)
    if '_delivery' not in state:
        return
    atomic_json(directory/'signals.json', d['signals'])
    atomic_json(directory/'oi_history.json', d['oi_history'])
    resolution = d.get('watch_resolution')
    if resolution:
        path = directory/'watch.json'
        current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if current.get('gewarnt_ts') == resolution['gewarnt_ts']:
            atomic_json(path, {**current, **resolution})


def drain(state, state_path, sender, token, target, dry_run=False, require_target_receipt=False):
    """Persist sending before call and receipt after. Stop at the first gap."""
    if dry_run or not token or not target:
        return False
    target = str(target)
    target_key = hashlib.sha256(target.encode()).hexdigest()
    d = state['_delivery']
    for m in d['messages']:
        if m['status'] in {'confirmed', 'preview'}:
            continue
        if m['status'] in {'uncertain', 'sending'}:
            print('Telegram-Versand angehalten: unklare Zustellung '+m['id'])
            return False
        if m['target'] is not None and m['target'] != target_key:
            print('Telegram-Versand angehalten: Nachrichtenziel geaendert')
            return False
        m['target'], m['status'], m['reason'] = target_key, 'sending', None
        m['attempts'] += 1
        m['attempt_started_ms'] = int(time.time()*1000)
        atomic_json(state_path, state)
        try:
            result = sender(m['text'], token, target)
        except Exception:
            result = dict(status='uncertain', reason='transport_exception', message_id=None)
        # Legacy booleans/invalid responses are not evidence of acceptance or rejection.
        if not isinstance(result, dict) or result.get('status') not in {'confirmed', 'rejected', 'uncertain'}:
            result = dict(status='uncertain', reason='invalid_result', message_id=None)
        if result['status'] == 'confirmed' and (type(result.get('message_id')) is not int or result['message_id'] <= 0):
            result = dict(status='uncertain', reason='missing_receipt', message_id=None)
        if result['status'] == 'confirmed' and require_target_receipt and result.get('target_binding') != target_key:
            result = dict(status='uncertain', reason='receipt_target_mismatch', message_id=None)
        m['status'] = result['status']
        m['receipt'] = result.get('message_id') if m['status'] == 'confirmed' else None
        # Persist only fixed categories, never response text or exception/token.
        m['reason'] = None if m['status'] == 'confirmed' else m['status']
        atomic_json(state_path, state)
        if m['status'] != 'confirmed':
            print('Telegram-Versand angehalten: '+m['status']+' '+m['id'])
            return False
    return True
