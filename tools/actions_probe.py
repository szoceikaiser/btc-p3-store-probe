"""Manually dispatched synthetic Hosted probe; real entry/CAS, local dummy only.

No bootstrap, recovery, retries, transport network, or store state fabrication.
The local sqlite/trace files meter and coordinate this test; GitHub remains
the sole delivery authority. Never run this tool in a production repository.
"""
import argparse
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'engine'), str(ROOT/'tools')]
import actions_delivery as actions
import durable_delivery as durable
import github_delivery as github
import main
import telegram_notify as tg
import produktion_actions_runner as entry
from production_contract import canonical, digest, effective_config, validate_v2
from strategy_core import Candle

REPO = 'szoceikaiser/btc-p3-store-probe'
REPO_ID = '1414013858'
SPEC_PATH = ROOT/'ops/actions-probe/spec.json'
WORKFLOW = '.github/workflows/p4-actions-probe.yml'
TARGET = 'p4-local-dummy-target'
TOKEN = 'p4-local-dummy-token'
TARGET_HASH = hashlib.sha256(TARGET.encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as out:
        out.write(canonical(value)+'\n'); out.flush(); os.fsync(out.fileno())


def event(root, role, value):
    # A child has its own append-only trace; no interprocess writes to one file.
    with (Path(root)/(role+'.jsonl')).open('a', encoding='utf-8', newline='\n') as out:
        out.write(canonical(value)+'\n'); out.flush(); os.fsync(out.fileno())


def identity(code_sha, spec, hosted=True):
    if not re.fullmatch('[0-9a-f]{40}', code_sha):
        raise ValueError('Reviewed commit SHA required')
    env = os.environ
    if (env.get('GITHUB_REPOSITORY') != REPO or env.get('GITHUB_REPOSITORY_ID') != REPO_ID
            or env.get('GITHUB_SHA') != code_sha
            or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_REF') != 'refs/heads/'+spec['code_branch']
            or env.get('GITHUB_WORKFLOW_REF') != REPO+'/'+WORKFLOW+'@refs/heads/'+spec['code_branch']
            or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or not re.fullmatch('[1-9][0-9]*', env.get('GITHUB_RUN_ID',''))
            or not re.fullmatch('[1-9][0-9]*', env.get('GITHUB_RUN_NUMBER',''))):
        raise ValueError('Actual manual testrepository/ref/run/attempt differs')
    approved = int(env.get('PROBE_APPROVED_AT_MS','0'))
    now = int(time.time()*1000)
    if not 0 <= now-approved < 20*60*60*1000:
        raise ValueError('Human start approval must be fresh within twenty hours')
    if hosted:
        release = Path('/etc/os-release').read_text()
        if (sys.platform != 'linux' or sys.version_info[:2] != (3,12)
                or 'ID=ubuntu' not in release or 'VERSION_ID="24.04"' not in release):
            raise ValueError('Reviewed Ubuntu 24.04 / Python 3.12 required')
    entry.check_code_pin(code_sha)


def runtime(code_sha, section, spec):
    pair = spec['pairs']['success' if section=='success' else 'loss']
    cfg = load(ROOT/'ops/actions-probe/config.synthetic.json')
    return {'schema':actions.POLICY, 'enabled':True, 'store_adapter':'github',
        'code_repository':REPO, 'code_ref':'refs/heads/'+spec['code_branch'],
        'private_store_repository':REPO, 'store_branch':pair['stream_branch'],
        'fence_branch':pair['fence_branch'], 'store_id':pair['store_id'],
        'store_path':github.STORE_PATH, 'fence_path':actions.FENCE_PATH,
        'workflow':WORKFLOW, 'code_sha':code_sha, 'config_sha256':digest(effective_config(cfg)),
        'bot_identity':'p4-local-dummy-bot', 'activation_review_sha256':digest(spec),
        'confirmation_policy':actions.POLICY,
        'confirmation_budgets':spec['runtime_budgets']['success' if section=='success' else 'loss']}


def seed_bodies(code_sha, section, spec):
    """Deterministic NEW synthetic bootstrap bytes only; never a runtime upload."""
    rt=runtime(code_sha,section,spec)
    snap=load(ROOT/'ops/actions-probe/snapshot.synthetic.json')
    snap['control']['store_id']=rt['store_id'];snap['control']['code_sha']=code_sha
    blocked=json.loads(github.initial_envelope(snap,REPO,rt['store_branch'],rt['store_id']))
    blocked['write_id']='p4-offline-blocked-seed-'+section
    active=json.loads(canonical(blocked));active['revision']=1
    active['parent_digest']=blocked['body_digest'];active['snapshot']['control']['mode']='active'
    active['body_digest']=github._digest(active['snapshot'])
    active['write_id']='p4-offline-synthetic-activation-'+section
    return canonical(blocked).encode(),canonical(active).encode(),actions.seed_fence(entry.deployment(rt))


def environment(rt, root):
    # GITHUB_TOKEN is supplied only to the hosted process by github.token.
    token = os.environ.get('PROBE_GITHUB_TOKEN')
    if not token:
        raise ValueError('Repository GITHUB_TOKEN missing')
    return {'BTC_DELIVERY_ADAPTER':'github','BTC_DELIVERY_GITHUB_REPO':REPO,
        'BTC_DELIVERY_GITHUB_BRANCH':rt['store_branch'],'BTC_DELIVERY_FENCE_BRANCH':rt['fence_branch'],
        'BTC_DELIVERY_STORE_ID':rt['store_id'],'BTC_DELIVERY_CODE_SHA':rt['code_sha'],
        'BTC_DELIVERY_RUN_ID':os.environ['GITHUB_RUN_ID'],
        'BTC_DELIVERY_RUN_ATTEMPT':os.environ['GITHUB_RUN_ATTEMPT'],
        'BTC_DELIVERY_BOT_IDENTITY':rt['bot_identity'],'BTC_DELIVERY_RUN_REPOSITORY':REPO,
        'BTC_DELIVERY_WORKFLOW':WORKFLOW,'BTC_DELIVERY_CONFIRMATION_POLICY':actions.POLICY,
        'BTC_DELIVERY_RUNNER_ROOT':str(Path(root).resolve()),
        'BTC_DELIVERY_EXPECTED_START_MS':str(load(ROOT/'ops/actions-probe/snapshot.synthetic.json')['control']['migration']['W']),
        'BTC_DELIVERY_GITHUB_TOKEN':token,'TELEGRAM_BOT_TOKEN':TOKEN,'TELEGRAM_CHAT_ID':TARGET}


def reserve(root, method, size, spec):
    with sqlite3.connect(Path(root)/'test-meter.sqlite', timeout=5) as db:
        db.execute('PRAGMA synchronous=FULL'); db.execute('BEGIN IMMEDIATE')
        section, get, put, request, started = db.execute('SELECT * FROM meter').fetchone()
        cap = spec['section_caps'][section]
        if (method not in ('GET','PUT') or get+(method=='GET') > cap['get']
                or put+(method=='PUT') > cap['put'] or size > 16384
                or request+size > cap['request_bytes']
                or not 0 <= int(time.time()*1000)-started < 900000):
            raise github.StoreUnavailable('Section test request/byte/deadline cap')
        db.execute('UPDATE meter SET get_count=?,put_count=?,request_bytes=?',
                   (get+(method=='GET'),put+(method=='PUT'),request+size))


def guarded_http(root, role, spec, opener):
    prefix = 'https://api.github.com/repos/'+REPO+'/'
    pairs = spec['pairs'].values()
    branches = {p[k] for p in pairs for k in ('stream_branch','fence_branch')}
    def open_request(request, timeout):
        url = request.full_url; parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path)
        method = request.get_method()
        if not url.startswith(prefix) or not 0 < timeout <= 15:
            raise AssertionError('Foreign/unbounded test request')
        tail = path.split('/'+REPO+'/',1)[1]
        if method=='PUT':
            payload = json.loads(request.data)
            if (tail not in ('contents/'+github.STORE_PATH,'contents/'+actions.FENCE_PATH)
                    or payload['branch'] not in branches or not payload.get('sha')):
                raise AssertionError('Test write/bootstrap scope refused')
        elif (tail.startswith('git/ref/heads/') and tail.split('git/ref/heads/')[1] not in branches):
            raise AssertionError('Old/foreign branch read refused')
        elif not (tail.startswith('git/ref/heads/') or tail.startswith('git/commits/')
                  or tail in ('contents/'+github.STORE_PATH,'contents/'+actions.FENCE_PATH)):
            raise AssertionError('Unexpected probe route')
        size = len(request.data or b'')
        reserve(root, method, size, spec)
        event(root,role,{'event':'request','method':method,'route':parts.path+
            ('?'+parts.query if parts.query else ''),'request_bytes':size})
        try:
            result = opener(request,timeout)
        except BaseException as exc:
            event(root,role,{'event':'request-failed','type':type(exc).__name__,
                'http_status':getattr(exc,'code',None)})
            raise
        return result
    return open_request


def barrier(root, role, record):
    value = {'event':'closed-fence-observed','commit':record['commit_sha'],
             'blob':record['blob_sha'],'body_sha256':hashlib.sha256(record['body']).hexdigest()}
    write(Path(root)/(role+'.ready.json'),value); event(root,role,value)
    other = Path(root)/('B.ready.json' if role=='A' else 'A.ready.json')
    until = time.monotonic()+20
    while not other.exists():
        if time.monotonic() >= until: raise RuntimeError('Contender rendezvous missing')
        time.sleep(.05)
    if load(other) != value: raise RuntimeError('Contenders did not read identical real fence')


def patches(root, role, spec, opener, race=False, crash=False):
    stack = ExitStack()
    stack.enter_context(patch.object(actions.ActionsClient,'open_request',
        staticmethod(guarded_http(root,role,spec,opener))))
    stack.enter_context(patch.object(urllib.request,'urlopen',
        side_effect=AssertionError('Market/transport/raw HTTP forbidden')))
    # Guard both imported transport references; source engine remains unchanged.
    def dummy(text, token, target):
        if token != TOKEN or target != TARGET or not text:
            raise AssertionError('Dummy transport binding differs')
        store = durable.active_store.get()
        messages = store.snapshot['engine']['_delivery']['messages']
        sending = [m for m in messages if m['status']=='sending']
        if len(sending)!=1 or store.snapshot['control']['owner'] is None:
            raise AssertionError('Durable sending intent not confirmed before dummy')
        event(root,role,{'event':'durable-intent-confirmed', 'stream_pin':store.confirmed_record(),
            'message_id':sending[0]['id'],'text_sha256':sending[0]['text_sha256']})
        if crash:
            event(root,role,{'event':'hard-exit-before-dummy','exit_code':23})
            os._exit(23)
        event(root,role,{'event':'dummy-send','message_id':sending[0]['id']})
        return {'status':'confirmed','message_id':73,'target_binding':TARGET_HASH}
    stack.enter_context(patch.object(main,'deliver_telegram',side_effect=dummy))
    stack.enter_context(patch.object(tg,'deliver_telegram',side_effect=dummy))
    if race:
        actual = actions.ActionsClient.fence_read
        def read(client, deployment):
            record, value = actual(client,deployment)
            if value['phase']=='closed' and not client.admitted: barrier(root,role,record)
            return record,value
        stack.enter_context(patch.object(actions.ActionsClient,'fence_read',read))
    return stack


def invoke(root, role, code_sha, section, opener, race=False, crash=False):
    spec=load(SPEC_PATH); rt=runtime(code_sha,section,spec)
    folder=Path(root)/role; folder.mkdir(); data=folder/'data'; data.mkdir()
    (data/'config.json').write_bytes((ROOT/'ops/actions-probe/config.synthetic.json').read_bytes())
    path=folder/'runtime.json'; write(path,rt)
    w=load(ROOT/'ops/actions-probe/snapshot.synthetic.json')['control']['migration']['W']
    with patch.dict(os.environ,environment(rt,folder)), patches(root,role,spec,opener,race,crash):
        return entry.run_runtime(path,'signal',data,
            fetch=lambda previous:([Candle(w,100,101,99,100)],[],previous))


def observer(root, role, rt, spec, opener):
    binding={'repo':REPO,'branch':rt['store_branch'],'store_id':rt['store_id']}
    policy={**rt['confirmation_budgets'],'run_get_limit':32,'run_put_limit':1,
            'deadline_utc_ms':int(time.time()*1000)+900000}
    budget=actions.RunBudget(policy,binding)
    client=actions.ActionsClient(os.environ['PROBE_GITHUB_TOKEN'],budget,environment(rt,root))
    with patches(root,role,spec,opener):
        stream=github.read_only(client,REPO,rt['store_branch'],rt['store_id'])
        fence, value=client.fence_read(entry.deployment(rt))
        parents={ 'stream':client.parents(REPO,stream._record['commit_sha']),
                  'fence':client.parents(REPO,fence['commit_sha']) }
    return {'stream_pin':stream.confirmed_record(),'stream':stream._envelope,
        'fence_pin':{k:v for k,v in fence.items() if k!='body'},'fence':value,
        'stream_bytes_sha256':hashlib.sha256(stream._record['body']).hexdigest(),
        'fence_bytes_sha256':hashlib.sha256(fence['body']).hexdigest(), 'parents':parents}


def initialize(root, section):
    root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=False)
    with sqlite3.connect(root/'test-meter.sqlite') as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE meter(section TEXT,get_count INTEGER,put_count INTEGER,request_bytes INTEGER,started INTEGER)')
        db.execute('INSERT INTO meter VALUES (?,0,0,0,?)',(section,int(time.time()*1000)))
    return root


def summary(root, section, readback, codes=None):
    spec=load(SPEC_PATH)
    traces={path.stem:[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            for path in sorted(Path(root).glob('*.jsonl'))}
    with sqlite3.connect(Path(root)/'test-meter.sqlite') as db:
        _,get,put,request,_=db.execute('SELECT * FROM meter').fetchone()
    return {'schema':'btc-p4-hosted-probe-v1','section':section,'passed':True,
        'actual_run':{k:os.environ[k] for k in ('GITHUB_RUN_ID','GITHUB_RUN_NUMBER','GITHUB_RUN_ATTEMPT','GITHUB_SHA')},
        'counts':{'get':get,'put':put,'request_bytes':request,
                  'response_reserved':(get+put)*65536},
        'fixed_caps':spec['section_caps'][section],'child_exit_codes':codes,
        'dummy_sends':sum(e['event']=='dummy-send' for events in traces.values() for e in events),
        'real_market_requests':0,'real_transport_requests':0,'readback':readback,'traces':traces,
        'python':sys.version.split()[0], 'image':os.environ.get('ImageVersion','unavailable')}


def check_loss(proof):
    snapshot=proof['stream']['snapshot']; fence=proof['fence']
    messages=snapshot['engine']['_delivery']['messages']
    if (fence['phase']!='open' or snapshot['control']['owner'] is None
            or len(messages)!=1 or messages[0]['status']!='sending'
            or messages[0]['attempts']!=1 or messages[0]['target']!=TARGET_HASH):
        raise AssertionError('Positive durable crash state missing')


def execute(root, code_sha, section, opener, prior_run=None, child_launcher=None):
    spec=load(SPEC_PATH);rt=runtime(code_sha,section,spec)
    if section in ('success','race-crash'):
        initial=observer(root,'bootstrap-readback',rt,spec,opener)
        _,stream_bytes,fence_bytes=seed_bodies(code_sha,'success' if section=='success' else 'loss',spec)
        if (initial['stream_bytes_sha256']!=hashlib.sha256(stream_bytes).hexdigest()
                or initial['fence_bytes_sha256']!=hashlib.sha256(fence_bytes).hexdigest()):
            raise AssertionError('Exact pristine bootstrap required; repeat/new attempt forbidden')
    if section=='success':
        result=invoke(root,'winner',code_sha,section,opener)
        proof=observer(root,'observer',rt,spec,opener)
        snap=proof['stream']['snapshot']; fence=proof['fence']
        messages=snap['engine']['_delivery']['messages']
        if (fence['phase']!='closed' or fence['completion']['stream']!=proof['stream_pin']
                or snap['control']['owner'] is not None
                or snap['control']['health']['current_run']['status']!='completed'
                or snap['engine']['last_signal_ts']!=snap['control']['migration']['W']
                or len(messages)!=1 or messages[0]['status']!='confirmed' or messages[0]['attempts']!=1):
            raise AssertionError('Exact completed stream/fence readback missing')
        report=summary(root,section,proof);report['initial_readback']=initial
        report['entry_counts']={k:v for k,v in result.items() if k!='result'}
        if report['dummy_sends']!=1: raise AssertionError('Expected one dummy send')
        return report
    if section=='race-crash':
        if child_launcher is None:
            def child_launcher(role):
                return subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),
                    '--child',role,'--out',str(root),'--code-sha',code_sha],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        children={role:child_launcher(role) for role in ('A','B')};codes={}
        try:
            for role,child in children.items():
                stdout,stderr=child.communicate(timeout=240); codes[role]=child.returncode
                (Path(root)/(role+'.stdout.log')).write_bytes(stdout)
                (Path(root)/(role+'.stderr.log')).write_bytes(stderr)
        finally:
            for child in children.values():
                if child.poll() is None: child.kill();child.wait()
        if sorted(codes.values())!=[23,41]:
            raise AssertionError('Expected actual hard exit 23 and stale CAS conflict 41: '+str(codes))
        ready=[load(Path(root)/(r+'.ready.json')) for r in ('A','B')]
        if ready[0]!=ready[1]: raise AssertionError('Different initial CAS inputs')
        proof=observer(root,'observer',rt,spec,opener);check_loss(proof)
        report=summary(root,section,proof,codes);report['initial_readback']=initial
        winner=next(r for r in codes if codes[r]==23); loser=next(r for r in codes if codes[r]==41)
        if (not any(e['event']=='hard-exit-before-dummy' for e in report['traces'][winner])
                or not any(e.get('http_status')==409 for e in report['traces'][loser])
                or sum(e.get('method')=='PUT' for e in report['traces'][loser])!=1
                or report['dummy_sends']!=0):
            raise AssertionError('Positive crash/conflict/zero-send evidence missing')
        intent=next(e for e in report['traces'][winner] if e['event']=='durable-intent-confirmed')
        if intent['stream_pin']!=proof['stream_pin']: raise AssertionError('Crash intent differs from exact readback')
        return report
    if section=='refusal':
        before=observer(root,'observer-before',rt,spec,opener);check_loss(before)
        if (not re.fullmatch('[1-9][0-9]*',prior_run or '')
                or before['fence']['run']['id']!=prior_run
                or prior_run==os.environ['GITHUB_RUN_ID']
                or before['fence']['run']['number']>=int(os.environ['GITHUB_RUN_NUMBER'])):
            raise AssertionError('Fresh actual run must bind the recorded crash run')
        try: invoke(root,'new-runner',code_sha,section,opener)
        except github.StoreConflict as exc:
            if str(exc)!='Open/old/repeated Actions run requires operator review': raise
            event(root,'new-runner',{'event':'open-fence-refused'})
        else: raise AssertionError('New runner admitted unexpectedly')
        after=observer(root,'observer-after',rt,spec,opener)
        if before!=after: raise AssertionError('Refusal changed real remote state')
        report=summary(root,section,after)
        if report['counts']['put']!=0 or report['dummy_sends']!=0:
            raise AssertionError('New runner attempted write/send')
        return report
    raise ValueError('Unknown section')


def cli():
    p=argparse.ArgumentParser()
    p.add_argument('--section',choices=('success','race-crash','refusal'))
    p.add_argument('--child',choices=('A','B'))
    p.add_argument('--code-sha',required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--prior-run')
    a=p.parse_args();spec=load(SPEC_PATH)
    identity(a.code_sha,spec)
    opener=actions.ProductionContentsClient.open_request
    if a.child:
        try: invoke(a.out,a.child,a.code_sha,'race-crash',opener,race=True,crash=True)
        except github.StoreConflict:
            event(a.out,a.child,{'event':'CAS-conflict-refused'}); return 41
        return 42  # a surviving winner is never a positive kill proof
    if not a.section: p.error('Section required')
    root=initialize(a.out,a.section)
    try: report=execute(root,a.code_sha,a.section,opener,a.prior_run)
    except Exception as exc:
        failed={'schema':'btc-p4-hosted-probe-halt-v1','passed':False,
            'section':a.section,'type':type(exc).__name__,'reason':str(exc),
            'traces':{p.name:p.read_text(encoding='utf-8') for p in root.glob('*.jsonl')},
            'child_logs':{p.name:p.read_text(encoding='utf-8',errors='replace')[:8192]
                          for p in root.glob('*.log')}}
        write(root/'halt.json',failed);print('P4_PROBE_HALT_EVIDENCE='+canonical(failed))
        raise
    write(root/'result.json',report)
    print('P4_PROBE_RESULT='+canonical(report))
    return 0


if __name__=='__main__':
    try: sys.exit(cli())
    except Exception as exc:
        print('P4_PROBE_HALTED='+canonical({'type':type(exc).__name__,'reason':str(exc)}),file=sys.stderr)
        sys.exit(20)
