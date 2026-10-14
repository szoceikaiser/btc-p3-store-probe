"""Hosted-runner admission fence on a separate branch in the store repository.

No resume: attempt 1 only, monotonically increasing workflow run_number,
open fence has no TTL. Counters may be volatile because admission survives
every subsequent request and a replacement process cannot enter that run.
"""
from contextvars import ContextVar
import base64
import json
import secrets
import time

from production_contract import canonical
from production_confirmation import ProductionContentsClient, validate_policy
from github_delivery import StoreUnavailable, StoreConflict, StoreHTTPFailure

POLICY = 'github-actions-fence-v1'
FENCE_PATH = 'delivery/actions-fence.json'
MAX_FENCE_BYTES = 8192
active_client = ContextVar('hosted_actions_client', default=None)


def seed_fence(deployment):
    """Offline bytes only; separately reviewed bootstrap, no API creation."""
    body = canonical({'schema': POLICY, 'deployment': deployment, 'phase': 'closed',
                      'run': {'number': 0, 'id': None, 'attempt': '1'},
                      'write_id': 'offline-seed', 'completion': None}).encode()
    if len(body) > MAX_FENCE_BYTES: raise ValueError('Fence seed too large')
    return body


class RunBudget:
    def __init__(self, policy, binding, clock=None):
        # Reuse accepted hard bounds; this policy deliberately selects three reads.
        self.policy = dict(validate_policy(policy))
        self.binding = dict(binding)
        self.clock = clock or (lambda: int(time.time()*1000))
        self.started = self.highwater = self.clock()
        if not 0 < policy['deadline_utc_ms']-self.started <= 900000:
            raise ValueError('Fresh fifteen-minute run deadline required')
        self.get = self.put = self.request = self.response = 0
        self.case = None
        self.pending = None
        self.halted = False

    def guard(self):
        now = self.clock()
        if self.halted or self.pending or not self.highwater <= now < self.policy['deadline_utc_ms']:
            raise StoreUnavailable('Actions budget/outcome/deadline blocked')
        self.highwater = now
        return now

    def begin_case(self, write_id, payload_size):
        self.guard(); p = self.policy
        if (self.case or self.get+11 > p['run_get_limit'] or self.put+1 > p['run_put_limit']
                or payload_size > p['request_byte_limit']
                or self.request+payload_size > p['run_request_byte_limit']
                or self.response+12*p['response_byte_limit'] > p['run_response_byte_limit']):
            raise StoreUnavailable('Complete Actions write/readback reserve unavailable')
        self.case = [write_id, 0, 0]

    def reserve(self, method, route, size, case_id):
        now = self.guard(); p = self.policy
        if (method not in ('GET','PUT') or size > p['request_byte_limit']
                or self.get+(method=='GET') > p['run_get_limit']
                or self.put+(method=='PUT') > p['run_put_limit']
                or self.request+size > p['run_request_byte_limit']
                or self.response+p['response_byte_limit'] > p['run_response_byte_limit']
                or (method=='PUT' and not case_id)
                or (case_id and (not self.case or self.case[0]!=case_id
                    or self.case[1]+(method=='GET') > 11 or self.case[2]+(method=='PUT') > 1))):
            self.halted = True
            raise StoreUnavailable('Actions request/byte budget exhausted')
        self.get += method=='GET'; self.put += method=='PUT'
        self.request += size; self.response += p['response_byte_limit']
        if case_id:
            self.case[1] += method=='GET'; self.case[2] += method=='PUT'
        self.pending = self.get+self.put
        return self.pending, min(p['timeout_seconds'], (p['deadline_utc_ms']-now)/1000)

    def finish(self, request_id, response_bytes, status):
        now = self.clock()
        if (request_id != self.pending or not self.highwater <= now < self.policy['deadline_utc_ms']
                or response_bytes > self.policy['response_byte_limit']):
            self.halted = True
            raise StoreUnavailable('Actions response crossed deadline/byte budget')
        self.pending = None; self.highwater = now

    def complete_case(self, write_id):
        self.guard()
        if not self.case or self.case[0] != write_id:
            raise StoreUnavailable('Actions case mismatch')
        self.case = None

    def halt(self):
        self.halted = True


class ActionsClient(ProductionContentsClient):
    def __init__(self, token, budget, environment):
        super().__init__(token, budget)
        self.environment = dict(environment)
        self.admitted = False
        self.fence_branch = environment['BTC_DELIVERY_FENCE_BRANCH']

    def read(self, repo, branch, path='delivery/stream.json'):
        expected = {FENCE_PATH:self.fence_branch,
                    'delivery/stream.json':self.journal.binding['branch']}
        if repo != self.journal.binding['repo'] or expected.get(path) != branch:
            raise StoreUnavailable('Actions read branch/path differs from binding')
        return super().read(repo,branch,path)

    def update(self, repo, branch, expected_blob_sha, body, write_id, path='delivery/stream.json'):
        if not self.admitted:
            raise StoreUnavailable('Business write outside admitted Actions run')
        return super().update(repo,branch,expected_blob_sha,body,write_id,path)

    def fence_write(self, old, value):
        body = canonical(value).encode()
        if len(body) > MAX_FENCE_BYTES: raise StoreUnavailable('Fence too large')
        write_id = value['write_id']
        payload = {'message': 'actions fence '+write_id, 'content': base64.b64encode(body).decode(),
                   'sha': old['blob_sha'], 'branch': self.fence_branch}
        self.journal.begin_case(write_id, len(canonical(payload).encode()))
        self.case_id = write_id
        repo, branch = self.journal.binding['repo'], self.fence_branch
        try:
            try:
                result = self._request('PUT', self._route(repo, '/contents/'+FENCE_PATH), payload)
                expected = result['commit']['sha']
            except StoreHTTPFailure: raise
            except StoreUnavailable: expected = None
            for attempt in range(3):
                observed = self.read(repo, branch, FENCE_PATH)
                if observed == old and attempt < 2: continue
                if (observed['body'] != body or observed == old
                        or (expected is not None and observed['commit_sha'] != expected)
                        or self.parents(repo, observed['commit_sha']) != [old['commit_sha']]):
                    raise StoreConflict('Fence mutation not confirmed')
                self.finalize_confirmation(repo, branch, observed['commit_sha'])
                return observed
            raise StoreConflict('Fence still old')
        except BaseException:
            self.journal.halt()
            raise

    def fence_read(self, deployment):
        record = self.read(self.journal.binding['repo'], self.fence_branch, FENCE_PATH)
        if len(record['body']) > MAX_FENCE_BYTES: raise StoreUnavailable('Fence too large')
        value = json.loads(record['body'])
        if (record['body'] != canonical(value).encode() or value.get('schema') != POLICY
                or value.get('deployment') != deployment or value.get('phase') not in ('open','closed')
                or type(value.get('run',{}).get('number')) is not int
                or value['run']['number'] < 0):
            raise StoreConflict('Fence identity/shape mismatch')
        return record, value

    def acquire(self, deployment, run):
        record, value = self.fence_read(deployment)
        if value['phase'] != 'closed' or run['number'] <= value['run']['number'] or run['attempt'] != '1':
            raise StoreConflict('Open/old/repeated Actions run requires operator review')
        self.run = dict(run)
        self.fence = {**value, 'phase': 'open', 'run': dict(run),
                      'write_id': secrets.token_hex(16), 'completion': None}
        self.fence_write(record, self.fence)
        self.admitted = True

    def close(self, deployment, completion):
        record, value = self.fence_read(deployment)
        if value != self.fence or self.journal.case or self.journal.pending:
            raise StoreConflict('Actions fence changed or case unfinished')
        closed = {**value, 'phase': 'closed', 'write_id': secrets.token_hex(16),
                  'completion': {'stream': completion, 'get_before_close': self.journal.get,
                                 'put_before_close': self.journal.put,
                                 'request_bytes_before_close': self.journal.request,
                                 'response_reserve_before_close': self.journal.response}}
        # There are no business operations after this write, even if reply is lost.
        self.admitted = False
        self.fence_write(record, closed)


def configured_actions_client(token, environment):
    client = active_client.get()
    if (client is None or not client.admitted or client.token != token
            or any(environment.get(k) != v for k,v in client.environment.items())):
        raise ValueError('Hosted Actions factory requires the exact admitted runtime scope')
    client.journal.guard()
    return client
