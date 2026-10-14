"""Fail-closed P2 production snapshot rules. No network or implicit bootstrap."""
from copy import deepcopy
import hashlib
import json


CANDLE_MS = 4 * 60 * 60 * 1000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def effective_config(raw):
    if not isinstance(raw, dict):
        raise ValueError('Configuration must be an object')
    config = {k: deepcopy(v) for k, v in raw.items() if not k.startswith('_')}
    if (config.get('muster_cvd') != 'usd' or config.get('bias_short') is not False
            or config.get('ausbruch_ruecktest') is not False):
        raise ValueError('P2 configuration requires usd CVD, no shorts and no E42')
    canonical(config)
    return config


def validate_v2(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('version') != 2:
        raise ValueError('Unknown production snapshot version')
    if not all(isinstance(snapshot.get(k), dict) for k in ('engine', 'watch', 'commands', 'control')):
        raise ValueError('Incomplete production snapshot')
    control = snapshot['control']
    required = ('stream_id', 'store_id', 'target_binding', 'bot_identity', 'mode',
                'code_sha', 'config_sha256', 'migration', 'health', 'reconciliations')
    if any(k not in control for k in required):
        raise ValueError('Incomplete production control')
    for k in ('stream_id', 'store_id', 'target_binding', 'bot_identity', 'code_sha', 'config_sha256'):
        if not isinstance(control[k], str) or not control[k]:
            raise ValueError('Invalid production identity: '+k)
    if control['mode'] not in ('offline', 'blocked', 'active'):
        raise ValueError('Unknown production mode')
    if not isinstance(control['health'], dict) or not isinstance(control['reconciliations'], list):
        raise ValueError('Invalid production control history')
    migration = control['migration']
    if not isinstance(migration, dict) or migration.get('legacy_delivery') != 'unknown_no_replay':
        raise ValueError('Missing validated legacy migration')
    for k in ('migration_id', 'B', 'W', 'history_sha256', 'history_count', 'source_hashes', 'unknowns', 'gates'):
        if k not in migration:
            raise ValueError('Incomplete migration: '+k)
    b, w = migration['B'], migration['W']
    if type(b) is not int or type(w) is not int or b % CANDLE_MS or w % CANDLE_MS or b != w-CANDLE_MS:
        raise ValueError('Invalid migration cutover')
    if type(migration['history_count']) is not int or migration['history_count'] < 0:
        raise ValueError('Invalid imported history count')
    if snapshot['engine'].get('demo') or snapshot['engine'].get('state_version') != 2:
        raise ValueError('Production engine must be versioned and non-demo')
    from position_state import pos_from_state
    pos_from_state(snapshot['engine'])
    delivery = snapshot['engine'].get('_delivery')
    if not isinstance(delivery, dict) or delivery.get('version') != 1:
        raise ValueError('Missing delivery journal')
    from telegram_outbox import load_delivery
    load_delivery(snapshot['engine'])
    imported = {'signals': delivery['signals']['signals'][:migration['history_count']]}
    if digest(imported) != migration['history_sha256']:
        raise ValueError('Imported signal history changed')
    if snapshot['engine'].get('config') != effective_config(snapshot['engine'].get('config')):
        raise ValueError('Invalid effective configuration')
    if digest(snapshot['engine']['config']) != control['config_sha256']:
        raise ValueError('Configuration pin mismatch')
    if snapshot['engine']['last_signal_ts'] < b:
        raise ValueError('Engine moved behind cutover')
    for entry in snapshot['commands'].values():
        if not isinstance(entry, dict) or '_delivery' not in entry or 'result' not in entry:
            raise ValueError('Incomplete durable command')
        load_delivery(entry)
    return snapshot


def verify_runtime_config(snapshot, raw, code_sha=None, target_binding=None, bot_identity=None):
    validate_v2(snapshot)
    control = snapshot['control']
    config = effective_config(raw)
    if digest(config) != control['config_sha256'] or config != snapshot['engine']['config']:
        raise ValueError('Runtime configuration differs from pinned configuration')
    for key, value in [('code_sha', code_sha), ('target_binding', target_binding),
                       ('bot_identity', bot_identity)]:
        if value is not None and control[key] != value:
            raise ValueError('Runtime '+key+' differs from stream binding')
    return config
