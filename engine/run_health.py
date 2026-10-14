"""Run completion attestation shared by SQLite and GitHub. No transport."""
from copy import deepcopy
import os
import secrets


def identity(store, kind):
    keys = {'repository': 'BTC_DELIVERY_RUN_REPOSITORY', 'workflow': 'BTC_DELIVERY_WORKFLOW',
            'run_id': 'BTC_DELIVERY_RUN_ID', 'run_attempt': 'BTC_DELIVERY_RUN_ATTEMPT'}
    result = {k: os.environ.get(v) for k, v in keys.items()}
    if any(not isinstance(v, str) or not v for v in result.values()):
        raise ValueError('Complete run identity required for signal/watch health')
    if not result['run_attempt'].isdigit() or int(result['run_attempt']) < 1:
        raise ValueError('Invalid run attempt')
    expected = os.environ.get('BTC_DELIVERY_EXPECTED_START_MS', '')
    if not expected.isdigit():
        raise ValueError('Explicit expected schedule occurrence required')
    if hasattr(store, 'owner') and any(store.owner[k] != result[k] for k in ('run_id', 'run_attempt')):
        raise ValueError('Health run differs from store owner')
    control = store.snapshot['control']
    return {**result, 'kind': kind, 'expected_start_ms': int(expected),
            'store_id': control['store_id'], 'stream_id': control['stream_id'],
            'code_sha': control['code_sha'], 'config_sha256': control['config_sha256'],
            'nonce': getattr(store, 'owner', {}).get('nonce') or secrets.token_hex(16)}


def record_event(store, kind, event, now, error_class):
    h = store.snapshot['control']['health']
    if event == 'start':
        run = identity(store, kind)
        h['current_run'] = {**run, 'started_ms': now, 'status': 'running'}
        h.pop('completion_candidate', None)
        h.update(run_kind=kind, run_started_ms=now, last_error_class=None)
        store.commit()
        return
    if event != 'finish':
        raise ValueError('Unknown health event')
    current = h.get('current_run')
    requested = identity(store, kind)
    if not current or any(current[k] != requested[k] for k in requested if k != 'nonce'):
        raise ValueError('Finish without matching run start')
    if now < current['started_ms']:
        raise ValueError('Run clock moved backwards')
    messages = [m for s in [store.snapshot['engine'], *store.snapshot['commands'].values()]
                for m in s.get('_delivery', {}).get('messages', [])]
    if any(m['status'] != 'confirmed' for m in messages):
        error_class = error_class or 'delivery_incomplete'
    if store.snapshot['control']['mode'] != 'active':
        error_class = error_class or 'stream_blocked'
    if kind == 'watch' and 'watch_observed_ms' not in current:
        error_class = error_class or 'watch_observation_missing'
    h.update(run_finished_ms=now, last_error_class=error_class)
    current.update(finished_ms=now, status='failed' if error_class else 'completed')
    if error_class:
        store.commit()
        return
    # Revision is known before the write; its own commit SHA is deliberately absent.
    candidate = {**deepcopy(current), 'revision': store.revision+1,
                 'last_signal_ts': store.snapshot['engine']['last_signal_ts']}
    h['completion_candidate'] = candidate
    store.commit()
    locator = store.confirmed_record()  # available only after exact readback
    binding = {'run': deepcopy(candidate), 'completion': locator}
    h.setdefault('successful_runs', {})[kind] = binding
    index = h.setdefault('completed_runs', {}).setdefault(kind, {})
    index[str(candidate['expected_start_ms'])] = deepcopy(binding)
    # A bounded lookup index, not a truncation of journals or immutable history.
    # Eight slots cover the 45m watch deadline and later already completed slots.
    while len(index) > 8:
        del index[min(index, key=int)]
    if kind == 'watch': h['last_watch_check_ms'] = now
    if kind == 'signal': h['last_signal_ts'] = candidate['last_signal_ts']
    store.commit()  # attestation itself must also be durably confirmed
