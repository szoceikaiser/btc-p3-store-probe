"""Shared P2 entry for isolated, net-free continuation rehearsal only.

The active production invocation is intentionally not exposed by this P2 CLI.
The copied store is destroyed afterwards; its synthetic receipts are never
usable as live evidence.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import sqlite3
from unittest.mock import patch
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
import durable_delivery as durable
import main
import telegram_notify as tg
from production_contract import canonical, verify_runtime_config
from strategy_core import Candle, FlowPoint


def run_checked(kind,data_dir,fetch=None,watch_raw=None,now_ms=None,sth=None):
    """Single fail-closed entry for later H/G wiring; not exposed as a P2 live CLI."""
    needed=('BTC_DELIVERY_STORE_ID','BTC_DELIVERY_RUNNER_ROOT',
            'BTC_DELIVERY_CODE_SHA','BTC_DELIVERY_BOT_IDENTITY',
            'TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')
    if any(not os.environ.get(k) for k in needed):
        raise ValueError('Production store/config/identity/transport environment incomplete')
    if kind in ('signal','watch'):
        metadata=('BTC_DELIVERY_RUN_REPOSITORY','BTC_DELIVERY_WORKFLOW','BTC_DELIVERY_RUN_ID',
                  'BTC_DELIVERY_RUN_ATTEMPT','BTC_DELIVERY_EXPECTED_START_MS')
        if any(not os.environ.get(k) for k in metadata):
            raise ValueError('Explicit run and schedule identity missing')
        if os.environ.get('BTC_DELIVERY_ADAPTER') == 'github':
            for actual, pinned in (('GITHUB_REPOSITORY','BTC_DELIVERY_RUN_REPOSITORY'),
                ('GITHUB_RUN_ID','BTC_DELIVERY_RUN_ID'), ('GITHUB_RUN_ATTEMPT','BTC_DELIVERY_RUN_ATTEMPT'),
                ('GITHUB_SHA','BTC_DELIVERY_CODE_SHA')):
                if not os.environ.get(actual) or os.environ[actual] != os.environ[pinned]:
                    raise ValueError('Actual GitHub run differs from runtime binding')
            expected_ref=(os.environ['BTC_DELIVERY_RUN_REPOSITORY']+'/'
                +os.environ['BTC_DELIVERY_WORKFLOW']+'@'+os.environ.get('GITHUB_REF',''))
            if not os.environ.get('GITHUB_REF') or os.environ.get('GITHUB_WORKFLOW_REF')!=expected_ref:
                raise ValueError('Actual workflow differs from run binding')
    if os.environ.get('BTC_DELIVERY_ADAPTER') == 'github':
        needed_github=('BTC_DELIVERY_GITHUB_REPO','BTC_DELIVERY_GITHUB_BRANCH',
            'BTC_DELIVERY_GITHUB_TOKEN','BTC_DELIVERY_RUN_ID','BTC_DELIVERY_RUN_ATTEMPT')
        if os.environ.get('BTC_DELIVERY_STORE') or any(not os.environ.get(k) for k in needed_github):
            raise ValueError('GitHub store identity or permission missing')
    elif os.environ.get('BTC_DELIVERY_ADAPTER', 'sqlite') != 'sqlite' or not os.environ.get('BTC_DELIVERY_STORE'):
        raise ValueError('Production store adapter missing or unknown')
    if not Path(data_dir,'config.json').is_file():
        raise FileNotFoundError('Pinned runtime configuration missing')
    durable.configured_store(data_dir)
    if kind=='signal': return main.run_engine(fetch=fetch or main.fetch_market_data,data_dir=data_dir)
    if kind=='watch': return main.watch_flush(data_dir=data_dir,now_ms=now_ms,kerzen_roh=watch_raw)
    if kind=='lage': return main.lage_abruf(fetch=fetch or main.fetch_market_data,
                                           data_dir=data_dir,sth=sth or main.sth_kostenbasis)
    if kind=='test': return main.send_testnachricht(data_dir=data_dir)
    raise ValueError('Unknown or forbidden production operation')


def simulate(store_path,store_id,config_path,fixture_path,kind):
    if kind not in ('signal','watch','lage','test'):
        raise ValueError('Unsupported offline runner kind')
    config=json.loads(Path(config_path).read_text(encoding='utf-8'))
    fixture=json.loads(Path(fixture_path).read_text(encoding='utf-8'))
    if fixture.get('synthetic') is not True:
        raise ValueError('Explicit synthetic fixture required')
    with durable.VolumeStore(store_path,store_id) as source:
        if source.snapshot['version']!=2 or source.snapshot['control']['mode']!='blocked':
            raise ValueError('Offline rehearsal requires blocked v2 source')
        verify_runtime_config(source.snapshot,config)
        snapshot=deepcopy(source.snapshot)
        source_revision=source.revision
        source_body=canonical(source.snapshot)
    with tempfile.TemporaryDirectory(prefix='produktion-offline-') as td:
        root=Path(td); data=root/'runner'/'data'; data.mkdir(parents=True)
        target='synthetic-target'
        target_hash=hashlib.sha256(target.encode()).hexdigest()
        snapshot['control']['target_binding']=target_hash
        snapshot['control']['bot_identity']='synthetic-bot'
        durable.provision(root/'store',store_id,snapshot)
        with durable.VolumeStore(root/'store',store_id) as simulation:
            simulation.snapshot['control']['mode']='active'
            simulation.commit()
        (data/'config.json').write_text(canonical(config),encoding='utf-8')
        candles=[Candle(*row) for row in fixture.get('candles',[])]
        flow=[FlowPoint(**row) for row in fixture.get('flow',[])]
        def fetch(previous=None):
            return candles,flow,fixture.get('oi_history',previous or [])
        def fake_transport(text,token,chat):
            assert token=='synthetic-token' and chat==target
            return {'status':'confirmed','message_id':73,'target_binding':target_hash}
        def network_forbidden(*args,**kwargs):
            raise AssertionError('Network forbidden in P2 offline runner')
        env={'BTC_DELIVERY_STORE':str(root/'store'),'BTC_DELIVERY_STORE_ID':store_id,
             'BTC_DELIVERY_ADAPTER':'sqlite',
             'BTC_DELIVERY_RUN_REPOSITORY':'synthetic/code','BTC_DELIVERY_WORKFLOW':'.github/workflows/synthetic.yml',
             'BTC_DELIVERY_RUN_ID':'synthetic-100','BTC_DELIVERY_RUN_ATTEMPT':'1',
             'BTC_DELIVERY_EXPECTED_START_MS':str(fixture.get('expected_start_ms',fixture.get('now_ms',0))),
             'BTC_DELIVERY_RUNNER_ROOT':str(root/'runner'),
             'BTC_DELIVERY_CODE_SHA':snapshot['control']['code_sha'],
             'BTC_DELIVERY_BOT_IDENTITY':'synthetic-bot',
             'BTC_DELIVERY_OPERATION_ID':fixture.get('operation_id','synthetic-operation'),
             'TELEGRAM_BOT_TOKEN':'synthetic-token','TELEGRAM_CHAT_ID':target}
        with patch.dict(os.environ,env),patch.object(urllib.request,'urlopen',side_effect=network_forbidden),\
             patch.object(main,'deliver_telegram',side_effect=fake_transport),\
             patch.object(tg,'deliver_telegram',side_effect=fake_transport):
            run_checked(kind,data,fetch=fetch,watch_raw=fixture.get('watch_raw'),
                        now_ms=fixture.get('now_ms'),sth=lambda:None)
        with durable.VolumeStore(root/'store',store_id) as result:
            messages=[m for state in [result.snapshot['engine'],*result.snapshot['commands'].values()]
                      for m in state.get('_delivery',{}).get('messages',[])]
            summary={'schema':'produktion-offline-run-v1','kind':kind,'source_revision':source_revision,
                     'simulated_revision':result.revision,'simulated_messages':len(messages),
                     'simulated_confirmed':sum(m['status']=='confirmed' for m in messages),
                     'real_transport':False}
    with durable.VolumeStore(store_path,store_id) as source_after:
        summary['source_unchanged']=(source_after.revision==source_revision and
                                     canonical(source_after.snapshot)==source_body)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--offline',action='store_true',required=True)
    for key in ('store','store-id','config','fixture','kind'): p.add_argument('--'+key,required=True)
    a=p.parse_args()
    try:
        print(canonical(simulate(a.store,a.store_id,a.config,a.fixture,a.kind)))
    except sqlite3.OperationalError as exc:
        error='mutex_conflict' if 'locked' in str(exc).lower() else 'store_unavailable'
        print(canonical({'schema':'produktion-offline-run-v1','error_class':error}))
        sys.exit(10 if error=='mutex_conflict' else 40)
    except (ValueError,FileNotFoundError,KeyError):
        print(canonical({'schema':'produktion-offline-run-v1','error_class':'identity_config_or_migration'}))
        sys.exit(20)
    except RuntimeError:
        print(canonical({'schema':'produktion-offline-run-v1','error_class':'delivery_blocked'}))
        sys.exit(30)
    except Exception:
        print(canonical({'schema':'produktion-offline-run-v1','error_class':'offline_data_or_transport'}))
        sys.exit(50)
