"""Disabled-by-default hosted Actions entry; no local persistence/bootstrap."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'engine'), str(ROOT/'tools')]
import actions_delivery as actions
import github_delivery as github
import produktion_runner as runner
from production_contract import canonical, digest, effective_config


def check_code_pin(code_sha):
    head = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
    changed = subprocess.check_output(['git','status','--porcelain','--untracked-files=all',
                                      '--','engine','tools','ops'], cwd=ROOT, text=True)
    if head != code_sha or changed:
        raise ValueError('Checked-out runtime code differs from reviewed clean commit')


def validate(runtime, kind, data_dir):
    if runtime.get('enabled') is not True:
        raise ValueError('Hosted Actions runtime is disabled')
    if (runtime.get('schema') != actions.POLICY or kind not in ('signal','watch')
            or runtime.get('confirmation_policy') != actions.POLICY
            or runtime.get('store_path') != github.STORE_PATH
            or runtime.get('fence_path') != actions.FENCE_PATH
            or not re.fullmatch('[0-9a-f]{64}', runtime.get('activation_review_sha256') or '')):
        raise ValueError('Explicit reviewed Actions activation binding required')
    fields = {'BTC_DELIVERY_ADAPTER': 'github',
              'BTC_DELIVERY_GITHUB_REPO': runtime.get('private_store_repository'),
              'BTC_DELIVERY_GITHUB_BRANCH': runtime.get('store_branch'),
              'BTC_DELIVERY_FENCE_BRANCH': runtime.get('fence_branch'),
              'BTC_DELIVERY_STORE_ID': runtime.get('store_id'),
              'BTC_DELIVERY_CODE_SHA': runtime.get('code_sha'),
              'BTC_DELIVERY_BOT_IDENTITY': runtime.get('bot_identity'),
              'BTC_DELIVERY_RUN_REPOSITORY': runtime.get('code_repository'),
              'BTC_DELIVERY_WORKFLOW': runtime.get('workflow'),
              'BTC_DELIVERY_CONFIRMATION_POLICY': actions.POLICY}
    if any(not v or os.environ.get(k) != v for k,v in fields.items()):
        raise ValueError('Actions environment differs from reviewed deployment')
    if runtime['fence_branch'] == runtime['store_branch']:
        raise ValueError('Separate fence branch preserves accepted store backup format')
    for actual, pinned in (('GITHUB_REPOSITORY','BTC_DELIVERY_RUN_REPOSITORY'),
                          ('GITHUB_RUN_ID','BTC_DELIVERY_RUN_ID'),
                          ('GITHUB_RUN_ATTEMPT','BTC_DELIVERY_RUN_ATTEMPT'),
                          ('GITHUB_SHA','BTC_DELIVERY_CODE_SHA')):
        if not os.environ.get(actual) or os.environ[actual] != os.environ.get(pinned):
            raise ValueError('Actual Actions run binding differs')
    if (os.environ.get('GITHUB_REF') != runtime.get('code_ref')
            or os.environ.get('GITHUB_WORKFLOW_REF') != runtime['code_repository']+'/'
                +runtime['workflow']+'@'+runtime['code_ref']
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'
            or not re.fullmatch('[1-9][0-9]*', os.environ.get('GITHUB_RUN_NUMBER',''))
            or not re.fullmatch('[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID',''))
            or not re.fullmatch('[0-9a-f]{40}', runtime.get('code_sha') or '')
            or any(os.environ.get(k) for k in ('BTC_DELIVERY_STORE',
                    'BTC_DELIVERY_CONFIRMATION_CONFIG','BTC_DELIVERY_REQUEST_JOURNAL'))):
        raise ValueError('Hosted run/ref/attempt or persistence binding refused')
    root = Path(os.environ.get('BTC_DELIVERY_RUNNER_ROOT',''))
    if not root.is_absolute() or not Path(data_dir).resolve().is_relative_to(root.resolve()):
        raise ValueError('Absolute contained runner projection required')
    if any(not os.environ.get(k) for k in ('BTC_DELIVERY_GITHUB_TOKEN',
             'TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','BTC_DELIVERY_EXPECTED_START_MS')):
        raise ValueError('Production environment incomplete')
    if not re.fullmatch('[1-9][0-9]*', os.environ['BTC_DELIVERY_EXPECTED_START_MS']):
        raise ValueError('Explicit positive schedule occurrence required')
    config = json.loads(Path(data_dir,'config.json').read_text(encoding='utf-8'))
    if digest(effective_config(config)) != runtime.get('config_sha256'):
        raise ValueError('Effective strategy configuration differs')
    check_code_pin(runtime['code_sha'])
    return fields


def deployment(runtime):
    # Fixed size deployment pin, including budgets, ref, strategy and review.
    return {k: runtime[k] for k in ('code_repository','code_ref','workflow',
        'private_store_repository','store_branch','fence_branch','store_id','store_path','fence_path',
        'code_sha','config_sha256','bot_identity','activation_review_sha256',
        'confirmation_policy','confirmation_budgets')}


def run_runtime(runtime_path, kind, data_dir, **fixtures):
    runtime = json.loads(Path(runtime_path).read_text(encoding='utf-8'))
    fields = validate(runtime, kind, data_dir)
    now = int(time.time()*1000)
    policy = {**runtime['confirmation_budgets'], 'deadline_utc_ms': now+900000}
    binding = {'repo':runtime['private_store_repository'], 'branch':runtime['store_branch'],
               'store_id':runtime['store_id'], 'run_id':os.environ['GITHUB_RUN_ID'],
               'run_attempt':'1', 'code_sha':runtime['code_sha']}
    # Snapshot exact factory environment; all clients in this process share counters.
    fields.update({k: os.environ.get(k,'') for k in (
        'BTC_DELIVERY_RUN_ID','BTC_DELIVERY_RUN_ATTEMPT','BTC_DELIVERY_RUNNER_ROOT',
        'BTC_DELIVERY_EXPECTED_START_MS','GITHUB_REPOSITORY','GITHUB_SHA','GITHUB_REF',
        'GITHUB_WORKFLOW_REF','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_RUN_NUMBER')})
    client = actions.ActionsClient(os.environ['BTC_DELIVERY_GITHUB_TOKEN'],
                                   actions.RunBudget(policy,binding), fields)
    run = {'number':int(os.environ['GITHUB_RUN_NUMBER']), 'id':binding['run_id'], 'attempt':'1',
           'kind':kind, 'expected_start_ms':os.environ['BTC_DELIVERY_EXPECTED_START_MS'],
           'deadline_utc_ms':policy['deadline_utc_ms']}
    pin = deployment(runtime)
    client.acquire(pin,run)
    scope = actions.active_client.set(client)
    try:
        result = runner.run_checked(kind, Path(data_dir), **fixtures)
        store = github.read_only(client,binding['repo'],binding['branch'],binding['store_id'])
        if store.snapshot['control'].get('owner') is not None or store.blocked() or client.journal.halted:
            raise github.StoreConflict('Incomplete/uncertain stream keeps Actions fence open')
        health = store.snapshot['control']['health']
        finished = health.get('successful_runs',{}).get(kind,{}).get('run',{})
        current = health.get('current_run',{})
        if (current.get('status') != 'completed' or finished.get('status') != 'completed'
                or any(current.get(k) != v or finished.get(k) != v for k,v in (
                    ('run_id',run['id']),('run_attempt','1'),('kind',kind),
                    ('repository',runtime['code_repository']),('workflow',runtime['workflow']),
                    ('code_sha',runtime['code_sha']),('config_sha256',runtime['config_sha256']),
                    ('expected_start_ms',int(run['expected_start_ms']))))):
            raise github.StoreConflict('Exact successful engine run attestation missing')
        client.close(pin,store.confirmed_record())
        return {'result':result, 'get':client.journal.get, 'put':client.journal.put,
                'request_bytes':client.journal.request, 'response_reserve':client.journal.response}
    finally:
        client.admitted = False
        actions.active_client.reset(scope)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--runtime',required=True)
    p.add_argument('--kind',choices=('signal','watch'),required=True)
    p.add_argument('--data-dir',required=True)
    a = p.parse_args()
    try: run_runtime(a.runtime,a.kind,a.data_dir)
    except Exception:
        print('Hosted Actions refused or halted; operator review required.',file=sys.stderr)
        sys.exit(20)
