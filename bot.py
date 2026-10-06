"""Public order preparation by default. Live requires separate explicit user configuration."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import signal

from engine import connect, initialize, Engine, meta, dumps
from frozen_calendar import load_rules
from public_api import PublicAPI, validate_market, quote
from reporting import report, atomic

ROOT=Path(__file__).resolve().parent
FROZEN_SHA256='5dde1f027cbc7b8fd88f5d02b3cc70777380b8cbfcd7be47d985534f3e407013'


def configuration(path=None, mode=None, require_activation=True):
    config=json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    local=Path(path) if path else ROOT/'config.local.json'
    if local.exists():
        supplied=json.loads(local.read_text(encoding='utf-8'))
        if set(supplied)-set(config):
            raise ValueError('unknown_configuration_keys')
        config.update(supplied)
    if mode:
        config['mode']=mode
    if config['mode'] not in ('prepare','paper','live') or config['conflict_policy'] not in ('both','skip_both') or config['order_type'] not in ('GTC','GTD'):
        raise ValueError('invalid_policy')
    if not 5<=config['poll_seconds']<=300 or not 1<=config['horizon_hours']<=72 or not 1<=config['requests_per_cycle']<=100 or not 1<=config['max_book_age_seconds']<=60:
        raise ValueError('invalid_poll_or_horizon')
    from decimal import Decimal
    if type(config['use_available_balance']) is not bool or type(config['balance_retry_seconds']) is not int or not 30<=config['balance_retry_seconds']<=86400:
        raise ValueError('invalid_balance_policy')
    for k in ['max_order_cost','max_total_committed_cost','max_daily_committed_cost','max_open_orders']:
        if config[k] is not None and (not Decimal(str(config[k])).is_finite() or Decimal(str(config[k]))<=0):
            raise ValueError('positive_finite_limit_required')
    if config['max_open_orders'] is not None and Decimal(str(config['max_open_orders'])) != Decimal(str(config['max_open_orders'])).to_integral_value():
        raise ValueError('integer_open_order_limit_required')
    fee=Decimal(config['fee_reserve_fraction'])
    if not fee.is_finite() or not 0<=fee<=1:
        raise ValueError('invalid_fee_reserve')
    if config['mode']=='live' and require_activation:
        if config['enable_live'] is not True or os.environ.get('POLY_ENABLE_LIVE')!='I_ACCEPT_LIVE_ORDERS':
            raise ValueError('live_disabled')
        if not config['use_available_balance'] and any(config[k] is None for k in ['max_order_cost','max_total_committed_cost','max_daily_committed_cost','max_open_orders']):
            raise ValueError('explicit_live_limits_required')
    return config


class Lock:
    def __init__(self,path):
        self.file=Path(path).open('a+b')
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                if Path(path).stat().st_size==0:
                    self.file.write(b'0');self.file.flush();self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('service_already_running') from None
    def close(self):
        self.file.close()


def running(folder):
    try:
        lock=Lock(folder/'service.lock')
    except RuntimeError:
        return True
    lock.close()
    return False


def open_db(folder,config):
    rules,digest=load_rules()
    if digest!=FROZEN_SHA256:
        raise ValueError('frozen_selection_hash_mismatch')
    db=connect(folder/'bot.sqlite3')
    initialize(db,rules,digest,config['mode'])
    return db,rules


def run(folder,config,once=False,launch_token=None):
    lock=Lock(folder/'service.lock')
    db=None;adapter=None
    stopping=False
    previous_signals={}
    def request_stop(signum,frame):
        nonlocal stopping
        stopping=True
    try:
        # Each explicit launch starts a new run; HALT remains persistent.
        (folder/'stop.request').unlink(missing_ok=True)
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous_signals[sig]=signal.signal(sig,request_stop)
        db,rules=open_db(folder,config)
        with db:
            meta(db,'pid',os.getpid());meta(db,'lifecycle','RUNNING');meta(db,'started_at',time.time())
            if meta(db,'first_started_at') is None: meta(db,'first_started_at',time.time())
            meta(db,'policy',config)
            if launch_token: meta(db,'launch_token',launch_token)
        if config['mode']=='live':
            from execution import LiveAdapter
            adapter=LiveAdapter(config)
        engine=Engine(db,rules,config,PublicAPI(),folder,adapter,stop_requested=lambda:stopping)
        report(db,rules,folder,config,True)
        while not stopping and not (folder/'stop.request').exists():
            with db: meta(db,'heartbeat',time.time())
            try:
                engine.cycle()
                with db: meta(db,'last_cycle_error','')
            except Exception as exc:
                with db: meta(db,'last_cycle_error',type(exc).__name__)
            report(db,rules,folder,config,True)
            if once:
                break
            deadline=time.monotonic()+config['poll_seconds']
            while time.monotonic()<deadline and not stopping and not (folder/'stop.request').exists():
                time.sleep(.25)
    finally:
        if adapter:
            adapter.close()
        if db:
            with db: meta(db,'lifecycle','STOPPED');meta(db,'stopped_at',time.time())
            report(db,rules,folder,config,False)
            db.close()
        lock.close()
        for sig,handler in previous_signals.items():
            signal.signal(sig,handler)


def probe(folder):
    from decimal import Decimal
    api=PublicAPI();now=api.sync();current=int(now)//300*300
    evidence={'retrieved_at':time.time(),'server_now':now,'uncertainty':api.uncertainty,'markets':[],'signed_or_submitted':False}
    for opening in [current+300,current+3600,current+86400]:
        m=api.market(opening)
        row={'opening':opening,'market':m,'books':[]}
        if m:
            row['clob']=api.clob(m['conditionId'])
            for outcome,token in validate_market(m,opening).items():
                book=api.book(token)
                price,size,tick,_=quote(book,token,m['conditionId'],Decimal(str(api.now())))
                row['books'].append({'outcome':outcome,'book':book,'prepared_quote':{'price':str(price),'size':str(size),'tick':str(tick)}})
        evidence['markets'].append(row)
    atomic(folder/'probe.json',dumps(evidence))
    print(dumps({'evidence':str(folder/'probe.json'),'signed_or_submitted':False,
       'markets':[{'opening':x['opening'],'available':x['market'] is not None,
                   'quotes':[b['prepared_quote'] for b in x['books']]} for x in evidence['markets']]}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','stop','status','report','run','probe','halt','resume'])
    parser.add_argument('--mode',choices=['prepare','paper','live'])
    parser.add_argument('--config')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--launch-token',help=argparse.SUPPRESS)
    args=parser.parse_args()
    # stop/status/halt never need access to keys or permission to initialize live client.
    config=configuration(args.config,args.mode,require_activation=args.command in ('start','run'))
    folder=ROOT/('state_'+config['mode']);folder.mkdir(exist_ok=True)
    if args.command=='run':
        run(folder,config,args.once,args.launch_token)
    elif args.command=='start':
        if running(folder):
            print('Service déjà actif.');return
        (folder/'stop.request').unlink(missing_ok=True)
        executable=Path(sys.executable)
        flags=0
        if os.name=='nt':
            executable=executable.with_name('pythonw.exe')
            flags=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP
        launch_token=str(uuid.uuid4())
        command=[str(executable),str(ROOT/'bot.py'),'run','--mode',config['mode'],'--launch-token',launch_token]
        if args.config: command.extend(['--config',str(Path(args.config).resolve())])
        with (folder/'launcher.log').open('a',encoding='utf-8') as log:
            child=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                   creationflags=flags,start_new_session=os.name!='nt')
        until=time.monotonic()+20
        while time.monotonic()<until:
            if running(folder) and (folder/'status.json').exists():
                status=json.loads((folder/'status.json').read_text(encoding='utf-8'))
                if status['health'].get('launch_token')==launch_token:
                    print(dumps({'started':True,'mode':config['mode'],'pid':status['health']['pid'],'report':str(folder/'report.html')}));return
            time.sleep(.25)
        raise RuntimeError('startup_timeout')
    elif args.command=='stop':
        atomic(folder/'stop.request',str(time.time()))
        until=time.monotonic()+55
        while running(folder) and time.monotonic()<until: time.sleep(.25)
        print('Arrêt '+('en attente du réseau' if running(folder) else 'confirmé')+' ; base conservée.')
    elif args.command=='halt':
        atomic(folder/'HALT',str(time.time()))
        print('HALT actif : suspend les nouvelles préparations/soumissions. Les ordres déjà soumis restent suivis et ne sont pas annulés.')
    elif args.command=='resume':
        (folder/'HALT').unlink(missing_ok=True)
        print('HALT retiré pour '+config['mode'])
    elif args.command=='probe':
        probe(folder)
    else:
        if not running(folder):
            db,rules=open_db(folder,config)
            report(db,rules,folder,config,False);db.close()
        print((folder/'status.json').read_text(encoding='utf-8'))
        print('Rapport : '+str(folder/'report.html'))


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        # SDK errors can carry credential-bearing request details; never dump traceback.
        known={'live_disabled','explicit_live_limits_required','unknown_configuration_keys','invalid_policy',
               'invalid_poll_or_horizon','positive_finite_limit_required','integer_open_order_limit_required',
               'invalid_fee_reserve','invalid_balance_policy','startup_timeout','launch_failed_see_launcher_log','service_already_running'}
        detail=':'+str(e) if str(e) in known else ''
        print('Échec : '+type(e).__name__+detail+'. Consulter configuration, rapport et journal local. Aucun secret affiché.',file=sys.stderr)
        sys.exit(1)
