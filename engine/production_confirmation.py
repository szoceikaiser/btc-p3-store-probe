"""Explicit opt-in production readback policy. No journal bootstrap at runtime.

Request reservations survive process loss. The journal belongs on a persistent
single-writer volume outside the runner; a fresh reviewed binding is required
for each run. Unknown reservations poison that binding, never reset it.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse

from production_contract import canonical
from github_delivery import GitHubContentsClient, StoreUnavailable, MAX_RESPONSE_BYTES

POLICY = 'github-confirmation-v1'
FIELDS = {'policy', 'confirmation_cycles', 'deadline_utc_ms', 'timeout_seconds',
          'run_get_limit', 'run_put_limit', 'case_get_limit', 'case_put_limit',
          'request_byte_limit', 'response_byte_limit',
          'run_request_byte_limit', 'run_response_byte_limit'}
BINDING_FIELDS = {'repo', 'branch', 'store_id', 'run_id', 'run_attempt', 'code_sha'}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != FIELDS or value['policy'] != POLICY:
        raise ValueError('Explicit complete production confirmation policy required')
    limits = {'confirmation_cycles': (3, 3), 'deadline_utc_ms': (1, 10**14),
              'timeout_seconds': (1, 15), 'run_get_limit': (11, 640),
              'run_put_limit': (1, 50), 'case_get_limit': (11, 11),
              'case_put_limit': (1, 1), 'request_byte_limit': (1, 1100*1024),
              'response_byte_limit': (1, MAX_RESPONSE_BYTES),
              'run_request_byte_limit': (1, 60*1024*1024),
              'run_response_byte_limit': (1, 1380*1024*1024)}
    for key, (lo, hi) in limits.items():
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError('Invalid confirmation budget: '+key)
    if value['run_response_byte_limit'] < 12*value['response_byte_limit']:
        raise ValueError('Missing one-write/readback response reserve')
    return value


def validate_binding(value):
    if (not isinstance(value, dict) or set(value) != BINDING_FIELDS
            or any(not isinstance(x, str) or not x for x in value.values())
            or not re.fullmatch('[0-9a-f]{40}', value['code_sha'])):
        raise ValueError('Complete pinned run binding required')
    GitHubContentsClient._route(value['repo'], '')
    return value


def provision_journal(path, policy, binding, now_ms=None):
    """Explicit local preparation only. Never called by the factory or runner."""
    validate_policy(policy); validate_binding(binding)
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    if not 0 < policy['deadline_utc_ms']-now_ms <= 15*60*1000:
        raise ValueError('Fresh deadline within fifteen minutes required')
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError('Existing absolute journal parent required')
    # Exclusive creation; no replacement, fallback or implicit parent creation.
    with path.open('xb'): pass
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE binding (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT, identity TEXT, created_ms INTEGER, highwater_ms INTEGER, halted INTEGER)')
        db.execute('INSERT INTO binding VALUES (1,?,?,?,?,0)',
                   (canonical(policy), canonical(binding), now_ms, now_ms))
        db.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, method TEXT, route_hash TEXT, case_id TEXT, request_bytes INTEGER, response_reserved INTEGER, response_bytes INTEGER, status TEXT)')
        db.execute('CREATE TABLE cases (write_id TEXT PRIMARY KEY, status TEXT)')


class RequestJournal:
    def __init__(self, path, policy, binding, clock=None):
        self.path = Path(path)
        self.policy = dict(validate_policy(policy))
        self.binding = dict(validate_binding(binding))
        self.clock = clock or (lambda: int(time.time()*1000))
        if not self.path.is_absolute(): raise ValueError('Absolute request journal required')
        self.check()

    def connect(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=0)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def guard(self, db):
        row = db.execute('SELECT policy,identity,created_ms,highwater_ms,halted FROM binding WHERE id=1').fetchone()
        now = self.clock()
        if (row is None or row[0] != canonical(self.policy) or row[1] != canonical(self.binding)
                or row[4] or type(now) is not int or now < row[3]
                or not row[2] <= now < self.policy['deadline_utc_ms']
                or self.policy['deadline_utc_ms']-row[2] > 15*60*1000
                or db.execute("SELECT 1 FROM requests WHERE status='reserved'").fetchone()):
            raise StoreUnavailable('Request journal identity/deadline/outcome blocked')
        db.execute('UPDATE binding SET highwater_ms=? WHERE id=1', (now,))
        return now

    def check(self):
        try:
            with closing(self.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE'); self.guard(db)
                if db.execute("SELECT 1 FROM cases WHERE status='open'").fetchone():
                    raise StoreUnavailable('Unfinished write case requires review')
        except (sqlite3.Error, OSError):
            raise StoreUnavailable('Persistent request journal unavailable') from None

    def totals(self, db, case_id=None):
        clause, args = ('', ()) if case_id is None else (' WHERE case_id=?', (case_id,))
        return db.execute("SELECT COALESCE(SUM(method='GET'),0),COALESCE(SUM(method='PUT'),0),COALESCE(SUM(request_bytes),0),COALESCE(SUM(response_reserved),0) FROM requests"+clause,args).fetchone()

    def begin_case(self, write_id, payload_size):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE'); self.guard(db)
            get, put, request, response = self.totals(db)
            p = self.policy
            if (get+11 > p['run_get_limit'] or put+1 > p['run_put_limit']
                    or payload_size > p['request_byte_limit']
                    or request+payload_size > p['run_request_byte_limit']
                    or response+12*p['response_byte_limit'] > p['run_response_byte_limit']):
                raise StoreUnavailable('Complete write/readback budget unavailable')
            if db.execute("SELECT 1 FROM cases WHERE status='open'").fetchone():
                raise StoreUnavailable('Previous write case not confirmed')
            db.execute('INSERT INTO cases VALUES (?,?)',(write_id,'open'))

    def complete_case(self, write_id):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE'); self.guard(db)
            if db.execute("UPDATE cases SET status='confirmed' WHERE write_id=? AND status='open'",(write_id,)).rowcount != 1:
                raise StoreUnavailable('Write case completion mismatch')

    def reserve(self, method, route, request_bytes, case_id):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE'); now = self.guard(db)
            get, put, request, response = self.totals(db)
            p = self.policy
            if (method not in ('GET', 'PUT') or request_bytes > p['request_byte_limit']
                    or get+(method=='GET') > p['run_get_limit']
                    or put+(method=='PUT') > p['run_put_limit']
                    or request+request_bytes > p['run_request_byte_limit']
                    or response+p['response_byte_limit'] > p['run_response_byte_limit']):
                raise StoreUnavailable('Run request/byte budget exhausted')
            if method == 'PUT' and not case_id:
                raise StoreUnavailable('PUT outside single-write case')
            if case_id:
                cg, cp, _, _ = self.totals(db, case_id)
                if cg+(method=='GET') > p['case_get_limit'] or cp+(method=='PUT') > 1:
                    raise StoreUnavailable('Case request budget exhausted')
            cursor = db.execute('INSERT INTO requests(method,route_hash,case_id,request_bytes,response_reserved,status) VALUES (?,?,?,?,?,?)',
                (method, hashlib.sha256(route.encode()).hexdigest(), case_id,
                 request_bytes, p['response_byte_limit'], 'reserved'))
            return cursor.lastrowid, min(p['timeout_seconds'], (p['deadline_utc_ms']-now)/1000)

    def finish(self, request_id, response_bytes, status):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            row = db.execute('SELECT highwater_ms FROM binding WHERE id=1').fetchone()
            valid = (type(now) is int and row[0] <= now < self.policy['deadline_utc_ms']
                     and response_bytes <= self.policy['response_byte_limit'])
            if not valid: status = 'blocked'
            db.execute('UPDATE requests SET response_bytes=?,status=? WHERE id=? AND status=?',
                       (response_bytes, status, request_id, 'reserved'))
            db.execute('UPDATE binding SET highwater_ms=?,halted=MAX(halted,?) WHERE id=1',
                       (max(now,row[0]), int(not valid or status=='blocked')))
        if not valid: raise StoreUnavailable('Response crossed deadline or byte budget')

    def halt(self):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE binding SET halted=1 WHERE id=1')


class ProductionContentsClient(GitHubContentsClient):
    confirmation_attempts = 3

    def __init__(self, token, journal):
        super().__init__(token, timeout=journal.policy['timeout_seconds'])
        self.journal = journal
        self.case_id = None
        self.response_byte_limit = journal.policy['response_byte_limit']
        self._response_bytes = 0

    @staticmethod
    def open_request(request, timeout):
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                raise StoreUnavailable('Unbudgeted HTTP redirect refused')
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)

    def _request(self, method, route, payload=None):
        # Scope every request to this store, including immutable reads.
        prefix = self._route(self.journal.binding['repo'], '/')
        if not route.startswith(prefix): raise StoreUnavailable('Foreign request route')
        request_bytes = 0 if payload is None else len(canonical(payload).encode('utf-8'))
        request_id, timeout = self.journal.reserve(method, route, request_bytes, self.case_id)
        old_timeout = self.timeout; self.timeout = timeout
        self._response_bytes = 0
        try:
            # Count wire bytes, including JSON whitespace; retain the conservative
            # response reservation permanently, even for failed attempts.
            result = super()._request(method, route, payload)
            self.journal.finish(request_id, self._response_bytes, 'completed')
            return result
        except Exception:
            # PUT transport failure may be resolved by exact reads in this same
            # process. A hard exit leaves 'reserved', blocking every restart.
            self.journal.finish(request_id, self._response_bytes, 'failed')
            raise
        finally:
            self.timeout = old_timeout

    def update(self, repo, branch, expected_blob_sha, body, write_id, path='delivery/stream.json'):
        if self.case_id is not None: raise StoreUnavailable('Previous write case unclosed')
        if (repo != self.journal.binding['repo'] or branch != self.journal.binding['branch']
                or path != 'delivery/stream.json' or not expected_blob_sha):
            raise StoreUnavailable('Write binding or runtime bootstrap denied')
        import base64
        payload = {'message': 'delivery revision '+write_id,
                   'content': base64.b64encode(body).decode('ascii'),
                   'sha': expected_blob_sha, 'branch': branch}
        self.journal.begin_case(write_id, len(canonical(payload).encode('utf-8')))
        self.case_id = write_id
        return super().update(repo, branch, expected_blob_sha, body, write_id, path)

    def confirmation_read(self, repo, branch, path, retrying=False):
        return self.read(repo, branch, path)

    def finalize_confirmation(self, repo, branch, commit_sha):
        latest = self._request('GET', self._route(repo, '/git/ref/heads/'+urllib.parse.quote(branch,safe='')))
        if latest['object']['sha'] != commit_sha:
            raise StoreUnavailable('Head changed after exact parent confirmation')
        self.journal.complete_case(self.case_id)
        self.case_id = None

    def halt_confirmation(self):
        self.journal.halt()


def configured_client(token, environment, runner):
    """Only an explicit selection changes the legacy one-cycle behavior."""
    selected = environment.get('BTC_DELIVERY_CONFIRMATION_POLICY')
    if selected == 'github-actions-fence-v1':
        if environment.get('BTC_DELIVERY_CONFIRMATION_CONFIG') or environment.get('BTC_DELIVERY_REQUEST_JOURNAL'):
            raise ValueError('Hosted Actions policy forbids alternate journals')
        from actions_delivery import configured_actions_client
        return configured_actions_client(token, environment)
    if not selected:
        if any(environment.get(k) for k in ('BTC_DELIVERY_CONFIRMATION_CONFIG', 'BTC_DELIVERY_REQUEST_JOURNAL')):
            raise ValueError('Confirmation configuration without explicit policy')
        return GitHubContentsClient(token)
    if selected != POLICY: raise ValueError('Unknown production confirmation policy')
    config = environment.get('BTC_DELIVERY_CONFIRMATION_CONFIG')
    journal = environment.get('BTC_DELIVERY_REQUEST_JOURNAL')
    if not config or not journal: raise ValueError('Policy configuration and durable budgets required')
    config, journal = Path(config), Path(journal)
    if (not config.is_absolute() or not journal.is_absolute()
            or journal.resolve().is_relative_to(runner.resolve())):
        raise ValueError('Persistent request journal must be outside entire runner')
    policy = validate_policy(json.loads(config.read_text(encoding='utf-8')))
    binding = {key: environment.get(env,'') for key, env in (
        ('repo','BTC_DELIVERY_GITHUB_REPO'), ('branch','BTC_DELIVERY_GITHUB_BRANCH'),
        ('store_id','BTC_DELIVERY_STORE_ID'), ('run_id','BTC_DELIVERY_RUN_ID'),
        ('run_attempt','BTC_DELIVERY_RUN_ATTEMPT'), ('code_sha','BTC_DELIVERY_CODE_SHA'))}
    return ProductionContentsClient(token, RequestJournal(journal, policy, binding))
