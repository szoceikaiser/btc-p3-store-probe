"""GitHub-backed v2 store. Network access occurs only when explicitly entered.

The dedicated private branch contains one authoritative file. This adapter never
creates it, retries a write blindly, or treats a runner-local file as state.
"""
import base64
from copy import deepcopy
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from production_contract import canonical, validate_v2


MAX_BYTES = 750 * 1024
MAX_WRITES = 50
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
STORE_PATH = 'delivery/stream.json'
SCHEMA = 'btc-github-store-v1'


class StoreError(RuntimeError):
    pass


class StoreConflict(StoreError):
    pass


class StoreUnavailable(StoreError):
    pass


class BranchMoved(StoreConflict):
    def __init__(self, before, after):
        self.before, self.after = before, after
        super().__init__('GitHub store branch moved during read')


class WriteUnconfirmed(StoreConflict):
    def __init__(self, category):
        self.category = category
        super().__init__('GitHub write not confirmed at branch head')


class StoreHTTPFailure(StoreUnavailable):
    pass


def _digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def initial_envelope(snapshot, repo, branch, store_id):
    """Build blocked seed bytes for an explicit, separately approved bootstrap."""
    validate_v2(snapshot)
    if snapshot['control']['mode'] != 'blocked' or snapshot['control']['store_id'] != store_id:
        raise ValueError('Only matching blocked snapshots can seed the GitHub store')
    if snapshot['control'].get('owner') is not None:
        raise ValueError('Seed owner must be empty')
    snapshot = deepcopy(snapshot)
    snapshot['control']['owner'] = None
    body = {'schema': SCHEMA, 'repo': repo, 'branch': branch, 'store_id': store_id,
            'revision': 0, 'parent_digest': '0' * 64,
            'write_id': secrets.token_hex(16), 'body_digest': _digest(snapshot),
            'snapshot': snapshot}
    encoded = canonical(body).encode('utf-8')
    if len(encoded) > MAX_BYTES:
        raise ValueError('GitHub store exceeds 750 KiB')
    return encoded


class GitHubContentsClient:
    """Small REST boundary; tests replace this class with a local fake."""

    def __init__(self, token, timeout=15):
        if not token:
            raise ValueError('GitHub store token missing')
        self.token, self.timeout = token, timeout

    def _request(self, method, route, payload=None):
        url = 'https://api.github.com' + route
        data = None if payload is None else canonical(payload).encode('utf-8')
        request = urllib.request.Request(url, data=data, method=method, headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer ' + self.token,
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
            'User-Agent': 'btc-delivery-store'})
        try:
            opener = getattr(self, 'open_request', urllib.request.urlopen)
            limit = getattr(self, 'response_byte_limit', MAX_RESPONSE_BYTES)
            with opener(request, timeout=self.timeout) as response:
                raw = response.read(limit+1)
                if hasattr(self, '_response_bytes'):
                    self._response_bytes = len(raw)
                if len(raw) > limit:
                    raise StoreUnavailable('GitHub response budget exhausted')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Never include a response body, URL with credentials, or token in logs.
            if exc.code == 409:
                raise StoreConflict('GitHub store conflict') from None
            raise StoreHTTPFailure('GitHub store HTTP '+str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise StoreUnavailable('GitHub store request outcome unknown') from None

    @staticmethod
    def _route(repo, tail):
        owner, separator, name = repo.partition('/')
        if (not separator or not owner or not name or '/' in name
                or any(x in {'.', '..'} for x in (owner, name))):
            raise ValueError('Expected owner/repository identity')
        return '/repos/' + urllib.parse.quote(owner, safe='') + '/' + urllib.parse.quote(name, safe='') + tail

    def read(self, repo, branch, path=STORE_PATH):
        if not branch or not path:
            raise ValueError('Explicit store branch and path required')
        ref = self._request('GET', self._route(repo, '/git/ref/heads/' + urllib.parse.quote(branch, safe='')))
        commit_sha = ref['object']['sha']
        record = self.read_at(repo, commit_sha, path)
        latest = self._request('GET', self._route(repo, '/git/ref/heads/' + urllib.parse.quote(branch, safe='')))
        if latest['object']['sha'] != commit_sha:
            raise BranchMoved(commit_sha, latest['object']['sha'])
        return record

    def read_at(self, repo, commit_sha, path=STORE_PATH):
        """Immutable commit read; never claims a writer owner."""
        if not isinstance(commit_sha,str) or not re.fullmatch('[0-9a-f]{40}',commit_sha):
            raise StoreUnavailable('Invalid pinned commit SHA')
        query = urllib.parse.urlencode({'ref': commit_sha})
        item = self._request('GET', self._route(repo, '/contents/' + urllib.parse.quote(path, safe='/')) + '?' + query)
        if item.get('type') != 'file' or item.get('encoding') != 'base64':
            raise StoreUnavailable('GitHub store content is not a base64 file')
        try:
            body = base64.b64decode(item['content'], validate=False)
        except (KeyError, ValueError):
            raise StoreUnavailable('GitHub store content cannot be decoded') from None
        actual_blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if item['sha'] != actual_blob:
            raise StoreUnavailable('GitHub store blob SHA mismatch')
        return {'commit_sha': commit_sha, 'blob_sha': item['sha'], 'body': body}

    def update(self, repo, branch, expected_blob_sha, body, write_id, path=STORE_PATH):
        if not expected_blob_sha:
            raise ValueError('No runtime bootstrap through Contents API')
        result = self._request('PUT', self._route(repo, '/contents/' + urllib.parse.quote(path, safe='/')),
            {'message': 'delivery revision ' + write_id,
             'content': base64.b64encode(body).decode('ascii'),
             'sha': expected_blob_sha, 'branch': branch})
        return result['commit']['sha']

    def parents(self, repo, commit_sha):
        result = self._request('GET', self._route(repo, '/git/commits/' + urllib.parse.quote(commit_sha, safe='')))
        return [p['sha'] for p in result['parents']]

    def is_ancestor(self, repo, base, head):
        if any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{40}',x) for x in (base,head)):
            raise StoreUnavailable('Invalid ancestry SHA')
        result = self._request('GET', self._route(repo, '/compare/'+base+'...'+head+'?per_page=1'))
        return (result['base_commit']['sha'] == base
                and result['merge_base_commit']['sha'] == base
                and result['status'] in ('ahead', 'identical'))


class GitHubStore:
    """One run owns every state mutation until verified release or operator review."""

    def __init__(self, client, repo, branch, store_id, run_id, run_attempt, path=STORE_PATH):
        if not all(isinstance(x, str) and x for x in (repo, branch, store_id, run_id, run_attempt)):
            raise ValueError('Complete GitHub store/run identity required')
        self.client, self.repo, self.branch, self.store_id, self.path = client, repo, branch, store_id, path
        self.owner = {'repository': repo, 'run_id': run_id, 'run_attempt': run_attempt,
                      'nonce': secrets.token_hex(16)}
        self.writes = 0
        self.warning = False
        self._record = None
        self._envelope = None
        self._entered = False
        self._halted = False

    def _parse(self, record):
        try:
            body = record['body']
            envelope = json.loads(body)
            if body != canonical(envelope).encode('utf-8'):
                raise ValueError('Noncanonical store body')
            if (envelope['schema'] != SCHEMA or envelope['repo'] != self.repo
                    or envelope['branch'] != self.branch or envelope['store_id'] != self.store_id):
                raise ValueError('GitHub store identity mismatch')
            if type(envelope['revision']) is not int or envelope['revision'] < 0:
                raise ValueError('Invalid GitHub revision')
            if (not isinstance(envelope['write_id'], str) or not envelope['write_id']
                    or not isinstance(envelope['parent_digest'], str)
                    or len(envelope['parent_digest']) != 64):
                raise ValueError('Invalid GitHub write lineage')
            if envelope['body_digest'] != _digest(envelope['snapshot']):
                raise ValueError('GitHub store digest mismatch')
            validate_v2(envelope['snapshot'])
            if envelope['snapshot']['control']['store_id'] != self.store_id:
                raise ValueError('Control store ID mismatch')
            if 'owner' not in envelope['snapshot']['control']:
                raise ValueError('GitHub owner field missing')
            owner = envelope['snapshot']['control']['owner']
            if owner is not None and (not isinstance(owner, dict) or
                    any(not owner.get(k) for k in ('repository', 'run_id', 'run_attempt', 'nonce'))):
                raise ValueError('Invalid GitHub owner')
            if len(body) > MAX_BYTES:
                raise ValueError('GitHub store exceeds 750 KiB')
            return envelope
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Invalid GitHub store envelope') from exc

    def _read(self):
        record = self.client.read(self.repo, self.branch, self.path)
        return record, self._parse(record)

    def __enter__(self):
        record, envelope = self._read()
        snapshot = envelope['snapshot']
        if snapshot['control']['mode'] != 'active':
            raise ValueError('GitHub stream is not active')
        if snapshot['control'].get('owner') is not None:
            raise StoreConflict('GitHub stream already owned; maintenance review required')
        if any(message['status'] in {'sending', 'uncertain'}
                for state in [snapshot['engine'], *snapshot['commands'].values()]
                for message in state.get('_delivery', {}).get('messages', [])):
            raise StoreConflict('Uncertain GitHub stream requires delivery review')
        self._record, self._envelope = record, envelope
        self.snapshot = deepcopy(snapshot)
        self.revision = envelope['revision']
        self.snapshot['control']['owner'] = deepcopy(self.owner)
        self._write()
        self._entered = True
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self._entered and not self._halted and not self.blocked():
            self.snapshot['control']['owner'] = None
            self._write()
        self._entered = False

    def _write(self):
        if self._halted:
            raise StoreConflict('GitHub store write outcome was not confirmed')
        validate_v2(self.snapshot)
        if self.writes >= MAX_WRITES:
            raise StoreUnavailable('GitHub write budget exhausted')
        if self._envelope is None or self._record is None:
            raise StoreUnavailable('GitHub store was not read')
        write_id = secrets.token_hex(16)
        next_envelope = {**self._envelope, 'revision': self.revision + 1,
                         'parent_digest': self._envelope['body_digest'],
                         'write_id': write_id, 'body_digest': _digest(self.snapshot),
                         'snapshot': deepcopy(self.snapshot)}
        body = canonical(next_envelope).encode('utf-8')
        if len(body) > MAX_BYTES:
            raise StoreUnavailable('GitHub store size budget exhausted')
        self.warning = self.warning or len(body) >= int(MAX_BYTES * .8) or self.writes+1 >= int(MAX_WRITES * .8)
        self.writes += 1
        prior_commit = self._record['commit_sha']
        try:
            try:
                commit_sha = self.client.update(self.repo, self.branch,
                    self._record['blob_sha'], body, write_id, self.path)
            except StoreHTTPFailure:
                raise
            except StoreUnavailable:
                # A lost PUT response is resolved only by an exact, independently
                # read write and parent commit; all other outcomes remain blocked.
                commit_sha = None
            attempts = getattr(self.client, 'confirmation_attempts', 1)
            if type(attempts) is not int or attempts not in (1, 3):
                raise WriteUnconfirmed('invalid-confirmation-bound')
            for attempt in range(attempts):
                try:
                    reader = getattr(self.client, 'confirmation_read', self.client.read)
                    record = reader(self.repo, self.branch, self.path, retrying=attempt>0) if hasattr(self.client, 'confirmation_read') else reader(self.repo, self.branch, self.path)
                    observed = self._parse(record)
                except BranchMoved as exc:
                    permitted = (prior_commit in (exc.before, exc.after)
                        and all(isinstance(x,str) and re.fullmatch('[0-9a-f]{40}',x) for x in (exc.before,exc.after))
                        and (commit_sha is None or {exc.before,exc.after}<={prior_commit,commit_sha}))
                    if permitted and attempt+1<attempts: continue
                    raise WriteUnconfirmed('unstable-confirmation-head') from None
                if record['commit_sha'] == prior_commit and record == self._record:
                    if attempt+1<attempts: continue
                    raise WriteUnconfirmed('old-confirmed-only')
                if (record['body'] != body or observed != next_envelope
                        or (commit_sha is not None and record['commit_sha'] != commit_sha)):
                    raise WriteUnconfirmed('foreign-or-different-confirmation')
                if self.client.parents(self.repo, record['commit_sha']) != [prior_commit]:
                    raise WriteUnconfirmed('wrong-confirmation-parent')
                if hasattr(self.client, 'finalize_confirmation'):
                    self.client.finalize_confirmation(self.repo, self.branch, record['commit_sha'])
                break
        except BaseException:
            self._halted = True
            if hasattr(self.client, 'halt_confirmation'):
                self.client.halt_confirmation()
            raise
        self._record, self._envelope = record, observed
        self.revision = observed['revision']

    def commit(self):
        if not self._entered or self.snapshot['control'].get('owner') != self.owner:
            raise StoreConflict('GitHub write without current owner')
        self._write()

    def confirmed_record(self):
        if self._halted or self._record is None:
            raise StoreUnavailable('No confirmed revision')
        return {'adapter': 'github', 'repo': self.repo, 'branch': self.branch,
            'path': self.path, 'store_id': self.store_id, 'revision': self.revision,
            'commit_sha': self._record['commit_sha'], 'blob_sha': self._record['blob_sha'],
            'snapshot_digest': self._envelope['body_digest']}

    def read_revision(self, locator):
        if not self.client.is_ancestor(self.repo, locator['commit_sha'], self._record['commit_sha']):
            raise StoreConflict('Completion is not in current store history')
        record = self.client.read_at(self.repo, locator['commit_sha'], self.path)
        envelope = self._parse(record)
        if (record['commit_sha'] != locator['commit_sha'] or record['blob_sha'] != locator['blob_sha']
                or envelope['revision'] != locator['revision']
                or envelope['body_digest'] != locator['snapshot_digest']):
            raise StoreConflict('Completion revision mismatch')
        return envelope['snapshot']


    def recover(self):
        changed = False
        for state in [self.snapshot['engine'], *self.snapshot['commands'].values()]:
            for message in state.get('_delivery', {}).get('messages', []):
                if message['status'] == 'sending':
                    message['status'], message['reason'] = 'uncertain', 'process_interrupted'
                    changed = True
        if changed:
            self.commit()

    def blocked(self):
        return any(message['status'] in {'sending', 'uncertain'}
            for state in [self.snapshot['engine'], *self.snapshot['commands'].values()]
            for message in state.get('_delivery', {}).get('messages', []))


def read_only(client, repo, branch, store_id, path=STORE_PATH):
    """Consistent authenticated head, without GitHubStore.__enter__ or any PUT."""
    store = GitHubStore(client, repo, branch, store_id, 'read-only', 'read-only', path)
    store._record, store._envelope = store._read()
    store.snapshot = deepcopy(store._envelope['snapshot'])
    store.revision = store._envelope['revision']
    return store


def review_takeover(client, repo, branch, store_id, expected_owner, expected_revision, evidence, path=STORE_PATH):
    """Explicit maintenance only; no age-based owner expiry or automatic retry."""
    required = ('reviewer', 'evidence_sha256', 'old_owner', 'run_completed',
                'workers_terminated', 'triggers_paused')
    if (not isinstance(evidence, dict) or any(k not in evidence for k in required)
            or not isinstance(expected_owner, dict) or not expected_owner
            or evidence['old_owner'] != expected_owner
            or not evidence['reviewer'] or not evidence['evidence_sha256']
            or any(evidence[k] is not True for k in required[3:])):
        raise ValueError('Verified termination and paused triggers required')
    store = GitHubStore(client, repo, branch, store_id, 'maintenance', 'review', path)
    record, envelope = store._read()
    snapshot = envelope['snapshot']
    if (envelope['revision'] != expected_revision
            or snapshot['control'].get('owner') != expected_owner):
        raise StoreConflict('Maintenance owner or revision changed')
    snapshot = deepcopy(snapshot)
    converted = 0
    for state in [snapshot['engine'], *snapshot['commands'].values()]:
        for message in state.get('_delivery', {}).get('messages', []):
            if message['status'] == 'sending':
                message['status'], message['reason'] = 'uncertain', 'owner_terminated'
                converted += 1
    snapshot['control']['owner'] = None
    snapshot['control'].setdefault('owner_events', []).append({
        'old_owner': expected_owner, 'reviewer': evidence['reviewer'],
        'evidence_sha256': evidence['evidence_sha256'],
        'converted_sending': converted, 'at_revision': expected_revision})
    store._record, store._envelope = record, envelope
    store.snapshot, store.revision = snapshot, expected_revision
    store._write()
    return {'revision': store.revision, 'converted_sending': converted, 'mode': snapshot['control']['mode']}
