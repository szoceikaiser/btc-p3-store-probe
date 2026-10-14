"""A4 persistent-volume adapter. No provisioning or network access on startup.

Requires one surviving volume with reliable SQLite locks/fsync (not NFS or
independent replicas). See A4-VERTRAG.md. Local files are only projections.
"""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import time

import telegram_outbox as box
import production_contract as contract

active_store = ContextVar('active_delivery_store', default=None)
command_texts = ContextVar('delivery_command_texts', default=None)


def validate(snapshot):
    if isinstance(snapshot, dict) and snapshot.get('version') == 2:
        return contract.validate_v2(snapshot)
    if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
        raise ValueError('Invalid durable snapshot')
    for key in ('engine', 'watch', 'commands'):
        if not isinstance(snapshot.get(key), dict):
            raise ValueError('Incomplete durable snapshot')
    box.load_delivery(snapshot['engine'])
    if snapshot['engine'].get('demo'):
        raise ValueError('Demo state cannot seed a durable delivery stream')
    if snapshot['engine'] and '_delivery' not in snapshot['engine']:
        raise ValueError('Migration requires engine state with complete delivery projections')
    for entry in snapshot['commands'].values():
        if not isinstance(entry, dict) or '_delivery' not in entry or 'result' not in entry:
            raise ValueError('Incomplete durable command')
        box.load_delivery(entry)


def provision(path, store_id, snapshot):
    """Explicit offline bootstrap from a reviewed complete snapshot; never overwrite.

    Operator must seed existing engine state, projections and watch. An empty
    snapshot is appropriate ONLY for a genuinely new stream, not migration.
    """
    validate(snapshot)
    if not isinstance(store_id, str) or not store_id.strip():
        raise ValueError('Store identity required')
    if snapshot['version'] == 2 and (snapshot['control']['store_id'] != store_id
                                     or snapshot['control']['mode'] != 'blocked'):
        raise ValueError('Provision requires matching blocked production store')
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(path/'mutex.sqlite')) as lock, lock:
        lock.execute('CREATE TABLE mutex (id INTEGER PRIMARY KEY)')
    with closing(sqlite3.connect(path/'snapshot.sqlite')) as db, db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), store_id TEXT, revision INTEGER, body TEXT, digest TEXT)')
        body = box.canonical(snapshot)
        db.execute('INSERT INTO snapshot VALUES (1, ?, 0, ?, ?)',
                   (store_id, body, hashlib.sha256(body.encode()).hexdigest()))


class VolumeStore:
    def __init__(self, path, store_id):
        self.path, self.store_id = Path(path), store_id
        self.db = self.lock = None

    def __enter__(self):
        # mode=rw is essential: a lost/unmounted database must NEVER become empty.
        try:
            self.lock = sqlite3.connect((self.path/'mutex.sqlite').as_uri()+'?mode=rw', uri=True, timeout=0)
            self.lock.execute('BEGIN IMMEDIATE')
            self.db = sqlite3.connect((self.path/'snapshot.sqlite').as_uri()+'?mode=rw', uri=True, timeout=0)
            self.db.execute('PRAGMA synchronous=FULL')
            row = self.db.execute('SELECT store_id,revision,body,digest FROM snapshot WHERE id=1').fetchone()
            if row is None or row[0] != self.store_id or hashlib.sha256(row[2].encode()).hexdigest() != row[3]:
                raise ValueError('Durable store identity/integrity mismatch')
            self.revision = row[1]
            self.snapshot = json.loads(row[2])
            validate(self.snapshot)
            if self.snapshot['version'] == 2 and self.snapshot['control']['store_id'] != self.store_id:
                raise ValueError('Control store identity mismatch')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()
        if self.lock is not None:
            self.lock.close()
        self.db = self.lock = None

    def commit(self):
        validate(self.snapshot)
        body = box.canonical(self.snapshot)
        with self.db:
            result = self.db.execute('UPDATE snapshot SET revision=revision+1,body=?,digest=? WHERE id=1 AND revision=? AND store_id=?',
                (body, hashlib.sha256(body.encode()).hexdigest(), self.revision, self.store_id))
            if result.rowcount != 1:
                raise RuntimeError('Durable revision conflict')
            completion = self.snapshot.get('control', {}).get('health', {}).get('completion_candidate')
            if completion and completion['revision'] == self.revision + 1:
                self.db.execute('CREATE TABLE IF NOT EXISTS health_revisions (revision INTEGER PRIMARY KEY, body TEXT, digest TEXT)')
                self.db.execute('INSERT INTO health_revisions VALUES (?, ?, ?)',
                    (self.revision+1, body, hashlib.sha256(body.encode()).hexdigest()))
        self.revision += 1
        row = self.db.execute('SELECT revision,body,digest FROM snapshot WHERE id=1').fetchone()
        if row != (self.revision, body, hashlib.sha256(body.encode()).hexdigest()):
            raise RuntimeError('Durable write readback mismatch')

    def confirmed_record(self):
        row = self.db.execute('SELECT revision,body,digest FROM snapshot WHERE id=1').fetchone()
        if row[0] != self.revision or json.loads(row[1]) != self.snapshot or contract.digest(self.snapshot) != row[2]:
            raise RuntimeError('Durable revision not confirmed')
        return {'adapter': 'sqlite', 'store_id': self.store_id, 'revision': self.revision,
                'snapshot_digest': row[2]}

    def read_revision(self, locator):
        row = self.db.execute('SELECT body,digest FROM health_revisions WHERE revision=?',
                             (locator['revision'],)).fetchone()
        if row is None or row[1] != locator['snapshot_digest'] or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise RuntimeError('Durable completion revision mismatch')
        snapshot = json.loads(row[0]); validate(snapshot)
        return snapshot

    def recover(self):
        changed = False
        for state in [self.snapshot['engine'], *self.snapshot['commands'].values()]:
            for m in state.get('_delivery', {}).get('messages', []):
                if m['status'] == 'sending':
                    m['status'], m['reason'] = 'uncertain', 'process_interrupted'
                    changed = True
        if changed:
            self.commit()

    def blocked(self):
        return any(m['status'] in {'sending', 'uncertain'}
                   for state in [self.snapshot['engine'], *self.snapshot['commands'].values()]
                   for m in state.get('_delivery', {}).get('messages', []))


@contextmanager
def read_only_volume(path, store_id):
    """Snapshot transaction only, no owner, mutex write, creation or recovery."""
    store = VolumeStore(Path(path).resolve(), store_id)
    with closing(sqlite3.connect((store.path/'snapshot.sqlite').as_uri()+'?mode=ro',
                                uri=True, timeout=0)) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT store_id,revision,body,digest FROM snapshot WHERE id=1').fetchone()
        if row is None or row[0] != store_id or hashlib.sha256(row[2].encode()).hexdigest() != row[3]:
            raise ValueError('Read-only durable identity/integrity mismatch')
        store.db, store.revision, store.snapshot = db, row[1], json.loads(row[2])
        validate(store.snapshot)
        if store.snapshot.get('version') == 2 and store.snapshot['control']['store_id'] != store_id:
            raise ValueError('Read-only control identity mismatch')
        yield store


def configured_store(directory):
    raw, sid = os.environ.get('BTC_DELIVERY_STORE'), os.environ.get('BTC_DELIVERY_STORE_ID')
    adapter = os.environ.get('BTC_DELIVERY_ADAPTER', 'sqlite')
    if not sid or (adapter == 'sqlite' and not raw):
        raise RuntimeError('Durable delivery store is not provisioned; send refused')
    runner_raw = os.environ.get('BTC_DELIVERY_RUNNER_ROOT')
    if not runner_raw or not Path(runner_raw).is_absolute():
        raise ValueError('Absolute BTC_DELIVERY_RUNNER_ROOT required')
    runner = Path(runner_raw).resolve()
    if not Path(directory).resolve().is_relative_to(runner):
        raise ValueError('Data directory outside declared runner')
    if adapter == 'github':
        if raw:
            raise ValueError('GitHub store must not have a local fallback volume')
        from github_delivery import GitHubStore
        keys = ('BTC_DELIVERY_GITHUB_REPO', 'BTC_DELIVERY_GITHUB_BRANCH',
                'BTC_DELIVERY_GITHUB_TOKEN', 'BTC_DELIVERY_RUN_ID',
                'BTC_DELIVERY_RUN_ATTEMPT')
        if any(not os.environ.get(k) for k in keys):
            raise ValueError('GitHub store identity, run or token missing')
        from production_confirmation import configured_client
        return GitHubStore(configured_client(os.environ['BTC_DELIVERY_GITHUB_TOKEN'], os.environ, runner),
            os.environ['BTC_DELIVERY_GITHUB_REPO'], os.environ['BTC_DELIVERY_GITHUB_BRANCH'],
            sid, os.environ['BTC_DELIVERY_RUN_ID'], os.environ['BTC_DELIVERY_RUN_ATTEMPT'])
    if adapter != 'sqlite':
        raise ValueError('Unknown delivery adapter')
    path = Path(raw)
    if not path.is_absolute() or path.resolve().is_relative_to(runner):
        raise ValueError('Delivery volume must be absolute and outside entire runner')
    return VolumeStore(path.resolve(), sid)


def configured_for_delivery():
    return bool(os.environ.get('BTC_DELIVERY_STORE') or
                os.environ.get('BTC_DELIVERY_ADAPTER') == 'github')


def has_credentials():
    return bool(os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))


@contextmanager
def session(directory, dry_run=False):
    # Unconfigured offline runs preserve the established local preview workflow.
    if dry_run or (not has_credentials() and not configured_for_delivery()):
        yield None
        return
    with configured_store(directory) as store:
        store.recover()
        if store.snapshot['version'] == 2:
            control = store.snapshot['control']
            if control['mode'] != 'active':
                raise ValueError('Production stream is not active')
            raw = json.loads((Path(directory)/'config.json').read_text(encoding='utf-8'))
            target = os.environ.get('TELEGRAM_CHAT_ID', '')
            if not target or not has_credentials():
                raise ValueError('Production transport identity missing')
            contract.verify_runtime_config(store.snapshot, raw,
                code_sha=os.environ.get('BTC_DELIVERY_CODE_SHA', ''),
                target_binding=hashlib.sha256(target.encode()).hexdigest(),
                bot_identity=os.environ.get('BTC_DELIVERY_BOT_IDENTITY', ''))
            if store.blocked():
                raise RuntimeError('Uncertain delivery blocks all production work')
        token = active_store.set(store)
        try:
            yield store
        finally:
            active_store.reset(token)


def restore(store, directory):
    directory.mkdir(parents=True, exist_ok=True)
    box.atomic_json(directory/'state.json', store.snapshot['engine'])
    box.atomic_json(directory/'watch.json', store.snapshot['watch'])
    box.project(store.snapshot['engine'], directory)
    # Empty explicit bootstrap must also overwrite stale checkout projections.
    if '_delivery' not in store.snapshot['engine']:
        box.atomic_json(directory/'signals.json', {'signals': []})
        box.atomic_json(directory/'oi_history.json', [])


@contextmanager
def engine_session(directory, dry_run=False):
    with session(directory, dry_run) as store:
        if store is None:
            yield
            return
        restore(store, directory)
        state_path = (directory/'state.json').resolve()
        def persist(path, state):
            if path.resolve() == state_path:
                store.snapshot['engine'] = deepcopy(state)
                resolution = state.get('_delivery', {}).get('watch_resolution')
                if resolution and store.snapshot['watch'].get('gewarnt_ts') == resolution['gewarnt_ts']:
                    store.snapshot['watch'].update(resolution)
                store.commit()
        token = box.durable_writer.set(persist)
        try:
            yield
        finally:
            box.durable_writer.reset(token)


def can_dispatch():
    store = active_store.get()
    return store is None or not store.blocked()


def runtime_config(directory):
    """Resolve the single pinned config for v2; legacy callers keep their own path."""
    store=active_store.get()
    if store is None or store.snapshot['version']!=2:
        return None
    raw=json.loads((Path(directory)/'config.json').read_text(encoding='utf-8'))
    return contract.verify_runtime_config(store.snapshot,raw)


def health_event(kind, event, error_class=None):
    store=active_store.get()
    if store is None or store.snapshot['version']!=2:
        return
    h=store.snapshot['control']['health']
    now=int(time.time()*1000)
    if kind in ('signal', 'watch'):
        from run_health import record_event
        record_event(store, kind, event, now, error_class)
        return
    if event=='start':
        h.update(run_kind=kind,run_started_ms=now,last_error_class=None)
    elif event=='finish':
        h['run_finished_ms']=now
        h['last_error_class']=error_class
        if kind=='watch' and error_class is None:
            h['last_watch_check_ms']=now
        if kind=='signal' and error_class is None:
            h['last_signal_ts']=store.snapshot['engine']['last_signal_ts']
    else:
        raise ValueError('Unknown health event')
    store.commit()


def watch_observation(candle_open_ms, observed_ms):
    store = active_store.get()
    if store is not None and store.snapshot.get('version') == 2:
        current = store.snapshot['control']['health']['current_run']
        if current['kind'] != 'watch' or current['status'] != 'running':
            raise ValueError('Watch observation without current run')
        current.update(watch_candle_open_ms=candle_open_ms, watch_observed_ms=observed_ms)


def stage_text(text):
    batch = command_texts.get()
    if batch is None:
        raise RuntimeError('Direct one-shot send refused: durable command required')
    batch.append(text)
    return True


def durable_command(kind):
    """Freeze a complete one-shot batch before transport; stable request IDs retry it."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            directory = Path(bound.arguments['data_dir'])
            dry = bound.arguments.get('dry_run', False)
            if dry or (not has_credentials() and not configured_for_delivery()):
                return fn(*args, **kwargs)
            request_id = os.environ.get('BTC_DELIVERY_OPERATION_ID')
            if kind != 'watch' and (not request_id or not request_id.strip()):
                raise ValueError('Stable BTC_DELIVERY_OPERATION_ID required')
            with box.engine_lock(directory), session(directory) as store:
                if store is None:
                    raise RuntimeError('Durable command requires storage')
                if store.snapshot['version'] == 2 and kind == 'resend':
                    raise ValueError('Historical resend forbidden for migrated stream')
                health_event(kind,'start')
                directory.mkdir(parents=True, exist_ok=True)
                # Only watch needs an engine projection; lage/test/resend do not
                # overwrite the local trading state as a side effect.
                if kind == 'watch':
                    restore(store, directory)
                key = kind if kind == 'watch' else box.identity('command', kind, 0, request_id)
                commands = store.snapshot['commands']
                entry = commands.get(key)
                def drain(entry):
                    if not can_dispatch():
                        return False
                    def persist(path, state):
                        commands[key] = deepcopy(state)
                        store.commit()
                    token = box.durable_writer.set(persist)
                    try:
                        import telegram_notify as tg
                        return box.drain(entry, directory/'command-delivery.json', tg.deliver_telegram,
                            os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID'],
                            require_target_receipt=store.snapshot['version']==2)
                    finally:
                        box.durable_writer.reset(token)
                if entry is not None:
                    done = drain(entry)
                    if kind != 'watch' or not done:
                        health_event(kind,'finish',None if done else 'delivery_incomplete')
                        return deepcopy(entry['result'])
                if store.blocked():
                    raise RuntimeError('Uncertain delivery requires manual review')
                batch = []
                token = command_texts.set(batch)
                try:
                    if kind == 'resend':
                        # History must come from durable state, never stale Git.
                        directory.mkdir(parents=True, exist_ok=True)
                        hist = store.snapshot['engine'].get('_delivery', {}).get('signals', {'signals': []})
                        box.atomic_json(directory/'signals.json', hist)
                    result = fn(*args, **kwargs)
                finally:
                    command_texts.reset(token)
                if kind == 'watch' and not batch:
                    health_event(kind,'finish')
                    return result
                delivery = box.load_delivery(entry or {})
                delivery.update(signals={'signals': []}, oi_history=[])
                event = result['gewarnt_ts'] if kind == 'watch' else request_id
                for index, text in enumerate(batch):
                    box.enqueue(delivery, kind, event, index, {'text': text}, text)
                entry = {'_delivery': delivery, 'result': result}
                commands[key] = entry
                if kind == 'watch':
                    store.snapshot['watch'] = deepcopy(result)
                store.commit()
                drain(entry)
                health_event(kind,'finish',None if not store.blocked() else 'delivery_uncertain')
                return result
        return wrapped
    return decorate
