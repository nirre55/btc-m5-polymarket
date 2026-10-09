"""Durable intent/order/fill/resolution state machine. No secret access here."""
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import time

from frozen_calendar import matches, load_rules
from public_api import validate_market, quote, official_winner, PublicDataError

D = Decimal


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def connect(path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,opening INTEGER NOT NULL,
      direction TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,next_check REAL NOT NULL DEFAULT 0,
      UNIQUE(opening,direction));
    CREATE TABLE IF NOT EXISTS links(intent_id TEXT REFERENCES intents(id),rule_id TEXT REFERENCES rules(id),
      PRIMARY KEY(intent_id,rule_id));
    CREATE TABLE IF NOT EXISTS fills(intent_id TEXT REFERENCES intents(id),trade_id TEXT,leg TEXT,
      qty TEXT NOT NULL,price TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,
      PRIMARY KEY(intent_id,trade_id,leg));
    CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
      intent_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL);
    ''')
    return db


def meta(db, key, value=None):
    if value is not None:
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key,dumps(value)))
        return value
    row = db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None


def initialize(db, rules, digest, mode):
    with db:
        for key,value in [('selection_sha256',digest),('mode',mode)]:
            old = meta(db,key)
            if old is not None and old != value:
                raise ValueError('selection_or_mode_changed')
            meta(db,key,value)
        for r in rules:
            old = db.execute('SELECT payload FROM rules WHERE id=?',(r['id'],)).fetchone()
            if old and old[0] != dumps(r):
                raise ValueError('stored_rule_changed')
            db.execute('INSERT OR IGNORE INTO rules VALUES (?,?)',(r['id'],dumps(r)))


def journal(db, intent, kind, data):
    db.execute('INSERT INTO journal(at,intent_id,kind,payload) VALUES (?,?,?,?)',
               (time.time(),intent,kind,dumps(data)))


def save(db, i, state=None, next_check=None):
    if state is not None:
        i['state'] = state
    if next_check is not None:
        i['next_check'] = next_check
    db.execute('UPDATE intents SET state=?,payload=?,next_check=? WHERE id=?',
               (i['state'],dumps(i),i.get('next_check',0),i['id']))


def intents(db):
    return [json.loads(r[0]) for r in db.execute('SELECT payload FROM intents ORDER BY opening,direction')]


def schedule(db, rules, now, config):
    # Cursor tracks missed periods after downtime, never creates retrospective orders.
    current = int(now)//300*300
    cursor = meta(db,'last_slot')
    history_start = current+300 if cursor is None else min(current+300,cursor+300)
    history_end = current if cursor is None else min(current,cursor+86400)
    end = current + int(config['horizon_hours']*3600)//300*300
    with db:
        openings=list(range(history_start,history_end+1,300))+list(range(current+300,end+1,300))
        for opening in openings:
            grouped = {'Up':[], 'Down':[]}
            for rule in rules:
                if matches(rule,opening*1000):
                    grouped['Up' if rule['prediction']=='V' else 'Down'].append(rule['id'])
            conflict = all(grouped.values())
            for direction, ids in grouped.items():
                if not ids:
                    continue
                identity = f'{opening}:{direction}'
                state = 'MISSED' if opening <= now else ('CONFLICT_SKIPPED' if conflict and config['conflict_policy']=='skip_both' else 'WAITING')
                i = {'id':identity,'opening':opening,'direction':direction,'state':state,
                     'created_at':now,'conflict':conflict,'next_check':0,'attempts':0,'mode':config['mode']}
                changed = db.execute('INSERT OR IGNORE INTO intents VALUES (?,?,?,?,?,0)',
                                     (identity,opening,direction,state,dumps(i))).rowcount
                for rule_id in ids:
                    db.execute('INSERT OR IGNORE INTO links VALUES (?,?)',(identity,rule_id))
                if changed:
                    journal(db,identity,'SCHEDULED',{'state':state,'rules':ids,'conflict':conflict})
        meta(db,'last_slot',history_end)
        meta(db,'missed_backlog_seconds',current-history_end)


def phase(i, now):
    if i.get('winner'):
        return 'RESOLVED'
    if now < i['opening']:
        return 'BEFORE_START'
    if now < i['opening']+300:
        return 'IN_PROGRESS'
    return 'ENDED_AWAITING_OFFICIAL_RESOLUTION'


def committed_risk(db, i, config):
    """Release exposure only after final order/trades; cash remains a separate gate."""
    if not i.get('committed_cost') or i['state']=='REJECTED':
        return D(0)
    terminal = i['state'] in ('FILLED','CANCELED','CANCELLED','EXPIRED','INVALID',
                             'CANCELED_MARKET_RESOLVED','PAPER_EXPIRED_REMAINDER','CLOSED_UNCONFIRMED')
    p = position(db,i)
    if not terminal or p['unconfirmed_trade_count'] or D(p['quantity'])!=D(i.get('matched_qty','0')):
        return D(i['committed_cost'])
    if i.get('winner'):
        return D(0)
    return D(p['gross_cost'])*(1+D(config['fee_reserve_fraction']))


def exposure_ok(db, i, config, now):
    limits = ['max_order_cost','max_total_committed_cost','max_daily_committed_cost','max_open_orders']
    if config['mode']=='live' and not config.get('use_available_balance') and any(config[k] is None for k in limits):
        return False, 'live_limits_required'
    reserve = 1 + D(config['fee_reserve_fraction'])
    cost = D(i['price'])*D(i['size'])*reserve
    existing = [x for x in intents(db) if x['id']!=i['id'] and x.get('committed_cost')]
    total = sum((committed_risk(db,x,config) for x in existing),D(0))
    day = int(now)//86400
    daily = sum((D(x['committed_cost']) for x in existing if x['state']!='REJECTED' and int(x['committed_at'])//86400==day),D(0))
    active = sum(committed_risk(db,x,config)>0 for x in existing)
    for key,amount in [('max_order_cost',cost),('max_total_committed_cost',total+cost),
                       ('max_daily_committed_cost',daily+cost),('max_open_orders',D(active+1))]:
        if config[key] is not None and amount > D(str(config[key])):
            return False,key
    return True, str(cost)


def apply_reconciliation(db, i, result):
    matched = D(result['matched_qty'])
    if not matched.is_finite() or not 0 <= matched <= D(i['size']):
        raise ValueError('invalid_matched_qty')
    for f in result['fills']:
        qty, price = D(f['qty']), D(f['price'])
        if not qty.is_finite() or not price.is_finite() or qty<=0 or not 0<price<=D(i['price']):
            raise ValueError('invalid_fill')
        old = db.execute('SELECT status,qty,price FROM fills WHERE intent_id=? AND trade_id=? AND leg=?',
                         (i['id'],f['id'],f['leg'])).fetchone()
        if old and (D(old['qty'])!=qty or D(old['price'])!=price):
            raise ValueError('fill_amount_changed')
        status = f['status'].removeprefix('TRADE_STATUS_')
        if status not in ('CONFIRMED','FAILED','RETRYING','MATCHED','MATCHED_NOT_BROADCASTED','MINED'):
            raise ValueError('unknown_trade_status')
        if old and old['status']=='CONFIRMED' and status!='CONFIRMED':
            raise ValueError('confirmed_fill_regressed')
        db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?) ON CONFLICT(intent_id,trade_id,leg) '
                   'DO UPDATE SET status=excluded.status,payload=excluded.payload',
                   (i['id'],f['id'],f['leg'],str(qty),str(price),status,dumps(f)))
    total=sum((D(r[0]) for r in db.execute('SELECT qty FROM fills WHERE intent_id=? AND status!=?',
                                         (i['id'],'FAILED'))),D(0))
    if total>D(i['size']):
        raise ValueError('fills_exceed_requested_size')
    i['matched_qty'] = str(matched)
    i['exchange_status'] = result['status']
    # Matched is not a confirmed position. Terminal order can still have pending trades.
    save(db,i,'UNKNOWN' if result['status']=='UNKNOWN' else 'LIVE')
    journal(db,i['id'],'RECONCILED',{'matched_qty':str(matched),'exchange_status':result['status']})


def position(db, i):
    rows = list(db.execute('SELECT qty,price,status FROM fills WHERE intent_id=?',(i['id'],)))
    confirmed = [r for r in rows if r['status'] in ('CONFIRMED','PAPER_SIMULATED')]
    qty = sum((D(r['qty']) for r in confirmed),D(0))
    cost = sum((D(r['qty'])*D(r['price']) for r in confirmed),D(0))
    return {'quantity':str(qty),'gross_cost':str(cost),
            'unconfirmed_trade_count':sum(r['status'] not in ('CONFIRMED','PAPER_SIMULATED','FAILED') for r in rows),
            'result':('WIN' if i['direction']==i['winner'] else 'LOSS') if qty>0 and i.get('winner') else None,
            'gross_payout':str(qty if i['direction']==i.get('winner') else D(0)) if i.get('winner') and qty>0 else None,
            'kind':'SIMULATED' if i['mode']=='paper' else ('REAL_CONFIRMED' if qty>0 else 'NO_EXECUTED_POSITION'),
            'note':'gross executed shares and theoretical gross payout exclude fees; not net token balance, redemption or cash receipt'}


class Engine:
    def __init__(self, db, rules, config, api, state_dir, adapter=None,stop_requested=None):
        self.db,self.rules,self.config,self.api,self.state_dir,self.adapter = db,rules,config,api,Path(state_dir),adapter
        self.stop_requested=stop_requested or (lambda:False)
        if config['mode']!='live' and adapter is not None:
            raise ValueError('no_execution_adapter_in_public_modes')
        with db:
            for i in intents(db):
                if i['state']=='SENDING':
                    save(db,i,'UNKNOWN',0)
                elif i['state']=='SIGNING':
                    save(db,i,'PREPARED',0)  # no post can occur until SENDING committed

    def halted(self):
        return self.stop_requested() or (self.state_dir/'stop.request').exists() or (self.state_dir/'HALT').exists()

    def retry(self, i, reason, now):
        i['attempts'] += 1
        i['last_error'] = reason
        if reason=='exact_future_market_not_listed':
            # Hourly discovery, with one last chance 60s before opening.
            # If that check is also negative, wake at opening only to mark MISSED.
            last_chance=i['opening']-60
            next_check=min(now+3600,last_chance) if last_chance>now else i['opening']
            save(self.db,i,next_check=next_check)
            return
        if reason=='PublicDataError:stale or future book':
            # Stable future books may refresh intermittently. A five-minute
            # backoff can repeatedly miss the same 30-second freshness window.
            save(self.db,i,next_check=now+15)
            return
        delay = min(300,15*2**min(i['attempts']-1,5))
        # Other transient failures keep faster retries near imminent events.
        delay = min(delay,max(15,(i['opening']-now)/4)) if i['opening']>now else delay
        save(self.db,i,next_check=now+delay)

    def prepare(self, i, now):
        if now+self.api.uncertainty >= i['opening']:
            save(self.db,i,'MISSED')
            return
        m = self.api.market(i['opening'])
        if m is None:
            self.retry(i,'exact_future_market_not_listed',now)
            return
        assets = validate_market(m,i['opening'])
        c = self.api.clob(m['conditionId'])
        if c.get('condition_id') != m['conditionId'] or c.get('market_slug') != m['slug']:
            raise ValueError('clob_market_mismatch')
        token = str(assets[i['direction']])
        if not any(str(t.get('token_id'))==token and t.get('outcome')==i['direction'] for t in c.get('tokens',[])):
            raise ValueError('clob_outcome_mismatch')
        if not (m.get('active') and m.get('acceptingOrders') and m.get('enableOrderBook')
                and not m.get('closed') and c.get('accepting_orders') and not c.get('closed')):
            self.retry(i,'not_accepting_orders',now)
            return
        book = self.api.book(token)
        now = self.api.now()
        if now+self.api.uncertainty >= i['opening']:
            save(self.db,i,'MISSED')
            return
        price,size,tick,asks = quote(book,token,m['conditionId'],D(str(now)),self.config['max_book_age_seconds'])
        i.update(condition=m['conditionId'],token=token,slug=m['slug'],market_version=m.get('version','v1'),
                 description=m['description'],resolution_source=m['resolutionSource'],
                 neg_risk=bool(book['neg_risk']),price=str(price),size=str(size),tick=str(tick),
                 book_timestamp=book['timestamp'],prepared_at=now,
                 order_type=self.config['order_type'],expiration=i['opening']+360,
                 matched_qty='0',last_error=None)
        journal(self.db,i['id'],'PREPARED',{'market':m,'book':book,'initial_price':str(price),'size':str(size)})
        save(self.db,i,'PREPARED',now)
        if self.config['mode']=='paper':
            allowed,cost = exposure_ok(self.db,i,self.config,now)
            if not allowed:
                i['last_error']='exposure_limit:'+cost
                save(self.db,i,next_check=now+30)
                return
            remaining=size
            # Snapshot simulation only once, no assumed future queue fills.
            for index,(p,s) in enumerate(asks):
                if p>price or remaining<=0:
                    break
                qty = min(remaining,s)
                self.db.execute('INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?)',
                    (i['id'],f'paper:{index}','snapshot',str(qty),str(p),'PAPER_SIMULATED',dumps({'book_timestamp':book['timestamp']})))
                remaining -= qty
            i.update(committed_cost=cost,committed_at=now,matched_qty=str(size-remaining))
            save(self.db,i,'PAPER_OPEN',now+30)

    def funds_reservations(self, now, exclude=None):
        # Include local orders not yet visible in the account snapshot, plus
        # matched-but-unconfirmed portions. Account-wide open BUYs are read by
        # the adapter, including orders from other bots (never modified here).
        reserve = 1+D(self.config['fee_reserve_fraction'])
        reservations=[]
        for x in intents(self.db):
            if x['id']==exclude or not x.get('committed_cost') or x['state']=='REJECTED':
                continue
            p=position(self.db,x)
            pending=max(D(0),D(x.get('matched_qty','0'))-D(p['quantity']))
            missing = D(x['committed_cost']) if (x['state'] in ('LIVE','SENDING','UNKNOWN') and not x.get('reservation_released_at')) or now-x['committed_at']<60 or p['unconfirmed_trade_count'] else D(0)
            if missing or pending:
                reservations.append({'order_id':x['order_id'],'missing':str(missing),
                                     'pending':str(pending*D(x['price'])*reserve)})
        return reservations

    def read_funds(self, i, now):
        funds=self.adapter.funds(i,self.funds_reservations(now,i.get('id')),self.config['fee_reserve_fraction'])
        available=D(funds['available'])
        if not available.is_finite() or available<0:
            raise ValueError('invalid_available_balance')
        meta(self.db,'funds',funds)
        meta(self.db,'funds_checked_at',time.time())
        meta(self.db,'funds_read_error','')
        if self.config.get('position_balance_percent') is not None:
            cash=D(funds['balance'])
            if not cash.is_finite() or cash<0:
                raise ValueError('invalid_reference_balance')
            peak=D(meta(self.db,'balance_high_water') or '0')
            if cash>peak:
                meta(self.db,'balance_high_water',str(cash))
                journal(self.db,None,'BALANCE_HIGH_WATER',{'balance':str(cash)})
            # Preserve a verified maximum even if a subsequent market/book read
            # fails and its outer transaction rolls back.
            self.db.commit()
        return available

    def position_size(self, price, minimum):
        percent=self.config.get('position_balance_percent')
        if percent is None:
            return minimum
        target=D(meta(self.db,'balance_high_water') or '0')*D(str(percent))/100
        shares=(target/price).quantize(D('.01'),rounding=ROUND_FLOOR)
        return max(minimum,shares)

    def poll_position_balance(self, now):
        if self.config['mode']!='live' or self.config.get('position_balance_percent') is None or self.halted():
            return
        if now-(meta(self.db,'funds_checked_at') or 0)<300:
            return
        candidates=[i for i in intents(self.db) if i.get('token')]
        if not candidates:
            return
        try:
            self.read_funds(candidates[-1],now)
        except Exception as exc:
            with self.db:
                meta(self.db,'funds_read_error',type(exc).__name__)
                # Avoid hammering a failed account endpoint every cycle.
                meta(self.db,'funds_checked_at',time.time())

    def wait_for_funds(self, i, now, reason):
        i['last_error']=reason
        if self.config.get('use_available_balance'):
            retry_at=now+self.config['balance_retry_seconds']
            meta(self.db,'funds_retry_at',retry_at)
            meta(self.db,'funds_probe',{'token':i['token'],'neg_risk':i['neg_risk'],
                 'required_cost':str(D(i['price'])*D(i['size'])*(1+D(self.config['fee_reserve_fraction'])))})
            journal(self.db,i['id'],'FUNDS_PAUSE',{'retry_at':retry_at,'reason':reason})
        else:
            retry_at=now+30
        save(self.db,i,'WAITING_FUNDS',min(retry_at,i['opening']))

    def poll_paused_funds(self, now):
        if self.config['mode']!='live' or not self.config.get('use_available_balance') or self.halted():
            return
        retry_at=meta(self.db,'funds_retry_at') or 0
        if not retry_at or now<retry_at:
            return
        probe=meta(self.db,'funds_probe')
        future=[i for i in intents(self.db) if i['opening']>now and i['state'] in ('PREPARED','WAITING_FUNDS')]
        if future:
            candidate=min(future,key=lambda x:D(x['price'])*D(x['size']))
            probe={'token':candidate['token'],'neg_risk':candidate['neg_risk'],
                   'required_cost':str(D(candidate['price'])*D(candidate['size'])*(1+D(self.config['fee_reserve_fraction'])))}
        if not probe:
            raise ValueError('missing_funds_probe')
        with self.db:
            try:
                available=self.read_funds(probe,now)
            except Exception as exc:
                # A read failure is not evidence of sufficient funds.
                meta(self.db,'funds_read_error',type(exc).__name__)
                meta(self.db,'funds_retry_at',now+300)
                return
            if available<D(probe['required_cost']):
                meta(self.db,'funds_retry_at',now+self.config['balance_retry_seconds'])
                journal(self.db,None,'FUNDS_STILL_INSUFFICIENT',{'retry_at':meta(self.db,'funds_retry_at')})
            else:
                meta(self.db,'funds_retry_at',0)
                journal(self.db,None,'FUNDS_AVAILABLE',{'available':str(available)})
                for i in future:
                    if i['state']=='WAITING_FUNDS':
                        save(self.db,i,next_check=now)

    def submit(self, i, now):
        if self.halted() or now+self.api.uncertainty>=i['opening']:
            return
        # An uncertain order keeps its own cash reservation. Never sign this
        # intention again, but unrelated intentions may use the remaining cash.
        if i['state'] not in ('PREPARED','WAITING_FUNDS'):
            return
        earlier=[x for x in intents(self.db) if now+self.api.uncertainty<x['opening']<i['opening']
                 and x['state'] in ('WAITING','PREPARED','WAITING_FUNDS')]
        if earlier:
            first=min(earlier,key=lambda x:x['opening'])
            i['last_error']='waiting_for_earlier_market:'+first['id']
            save(self.db,i,next_check=now+15)
            return
        retry_at=meta(self.db,'funds_retry_at') or 0
        if self.config.get('use_available_balance') and now<retry_at:
            i['last_error']='funds_pause_until_next_check'
            save(self.db,i,'WAITING_FUNDS',min(retry_at,i['opening']))
            return
        available=self.read_funds(i,now)
        if i['state']=='WAITING_FUNDS' and available>=D(i['price'])*D(i['size'])*(1+D(self.config['fee_reserve_fraction'])):
            i['state']='PREPARED'
        # Verify current constraints, but NEVER change the stored price.
        m=self.api.market(i['opening'])
        if not m or validate_market(m,i['opening'])[i['direction']]!=i['token'] or not m.get('acceptingOrders') or m.get('closed'):
            raise ValueError('market_no_longer_accepting')
        book=self.api.book(i['token'])
        _,minimum,tick,_=quote(book,i['token'],i['condition'],D(str(self.api.now())),self.config['max_book_age_seconds'])
        if D(i['price'])%tick or (self.config.get('position_balance_percent') is None and D(i['size'])<minimum):
            raise ValueError('constraints_changed_no_reprice')
        size=self.position_size(D(i['price']),minimum)
        if self.config.get('position_balance_percent') is not None:
            i['size']=str(size)
            i['sizing_reference_balance']=meta(self.db,'balance_high_water') or '0'
            i['sizing_percent']=str(self.config['position_balance_percent'])
        allowed,cost=exposure_ok(self.db,i,self.config,now)
        if not allowed:
            i['last_error']='exposure_limit:'+cost
            save(self.db,i,next_check=now+30)
            return
        if available<D(cost):
            self.wait_for_funds(i,now,'insufficient_available_funds')
            return
        if self.config.get('use_available_balance'):
            meta(self.db,'funds_retry_at',0)
        save(self.db,i,'SIGNING')
        self.db.commit()
        try:
            signed,identity=self.adapter.sign(i)
        except Exception:
            save(self.db,i,'PREPARED')
            raise
        now=self.api.now()
        if self.halted() or now+self.api.uncertainty>=i['opening']:
            save(self.db,i,'PREPARED')
            return
        i.update(order_id=identity,committed_cost=cost,committed_at=now)
        save(self.db,i,'SENDING')
        journal(self.db,i['id'],'SUBMIT_STARTED',{'order_id':identity,'price':i['price'],'size':i['size']})
        self.db.commit()  # durable ambiguity boundary BEFORE network POST
        try:
            r=self.adapter.submit(signed)
            if r['ok'] and r['order_id']!=identity:
                raise ValueError('server_order_hash_mismatch')
            i['exchange_status']=r['status']
            i['trade_ids']=r.get('trade_ids',[])
            if r['ok']:
                i['last_error']=None
                save(self.db,i,'LIVE',now+15)
            else:
                for key in ('committed_cost','committed_at','order_id'):
                    i.pop(key,None)
                if r.get('retryable_funds'):
                    self.wait_for_funds(i,now,'balance_or_allowance_rejected')
                else:
                    i['last_error']='order_rejected'
                    save(self.db,i,'REJECTED',now+30)
            journal(self.db,i['id'],'SUBMIT_RESPONSE',r)
        except Exception as e:
            i['last_error']='submission_ambiguous:'+type(e).__name__
            save(self.db,i,'UNKNOWN',now+15)
            journal(self.db,i['id'],'SUBMIT_AMBIGUOUS',{'error_type':type(e).__name__})
        self.db.commit()

    def observe(self, i, now):
        if i['state'] in ('LIVE','UNKNOWN','CLOSED_UNCONFIRMED'):
            released=i.get('reservation_released_at')
            if not released and now>=i['opening'] and not i.get('cancel_terminal_ack'):
                try:
                    cancellation=self.adapter.cancel_expired_intent(i)
                    i['cancel_terminal_ack']=bool(cancellation['terminal_acknowledged'])
                    journal(self.db,i['id'],'CANCEL_AT_OPENING',cancellation)
                    save(self.db,i)
                except Exception as exc:
                    journal(self.db,i['id'],'CANCEL_READ_ERROR',{'error_type':type(exc).__name__})
            result=self.adapter.reconcile(i)
            apply_reconciliation(self.db,i,result)
            p=position(self.db,i)
            terminal=str(result['status']).removeprefix('ORDER_STATUS_').upper()
            if p['unconfirmed_trade_count']==0 and D(p['quantity'])==D(result['matched_qty']):
                if terminal in ('MATCHED','FILLED') and D(p['quantity'])==D(i['size']):
                    i['state']='FILLED'
                elif terminal in ('CANCELED','CANCELLED','EXPIRED','INVALID','CANCELED_MARKET_RESOLVED'):
                    i['state']=terminal
            if released and i['state']=='UNKNOWN':
                i['state']='CLOSED_UNCONFIRMED'
            if (not released and i['state']=='UNKNOWN' and i.get('cancel_terminal_ack')
                    and i['order_type']=='GTD' and now>=i['expiration']+900 and i.get('winner')
                    and not p['unconfirmed_trade_count'] and D(p['quantity'])==D(i['matched_qty'])):
                # Terminal reply + completed authenticated history + official
                # resolution + fresh cash after settlement grace. Retire the
                # cash reservation, without asserting this order never filled.
                self.read_funds(i,now)
                i.update(reservation_released_at=now,state='CLOSED_UNCONFIRMED',
                         last_error='closed_without_complete_order_history')
                journal(self.db,i['id'],'EXPIRED_RESERVATION_RELEASED',{
                    'confirmed_qty':p['quantity'],'cancel_terminal_ack':True,
                    'expiry':i['expiration'],'history_status':result['status']})
                if meta(self.db,'funds_retry_at'):
                    meta(self.db,'funds_retry_at',now)
        if now >= i['opening']+302 and not i.get('binance'):
            k=self.api.binance(i['opening'])
            if k and int(k[0][0])==i['opening']*1000 and int(k[0][6])<int(now*1000):
                op,cp=D(k[0][1]),D(k[0][4])
                direction='Up' if cp>op else 'Down' if cp<op else 'DOJI'
                i['binance']={'open':str(op),'close':str(cp),'color':direction,
                              'hypothesis_result':'WIN' if direction==i['direction'] else 'LOSS',
                              'kind':'BINANCE_DIAGNOSTIC_NOT_POLYMARKET_SETTLEMENT'}
        if now>=i['opening']+300 and i.get('condition') and not i.get('winner'):
            c=self.api.clob(i['condition'])
            winner=official_winner(c)
            if winner:
                i.update(winner=winner,resolved_at=now)
                journal(self.db,i['id'],'OFFICIAL_RESOLUTION',c)
        if i['state']=='PAPER_OPEN' and now>=i['opening']+300 and i['order_type']=='GTD':
            i['state']='PAPER_EXPIRED_REMAINDER'
        if i['state'] in ('PREPARED','WAITING_FUNDS') and now>=i['opening']:
            i['state']='PREPARED_NOT_SUBMITTED'
        save(self.db,i,next_check=now+30 if now<i['opening']+600 else now+300)

    def cycle(self):
        now=self.api.sync()
        schedule(self.db,self.rules,now,self.config)
        self.poll_paused_funds(now)
        self.poll_position_balance(now)
        all_intents=intents(self.db)
        due=[i for i in all_intents if i.get('next_check',0)<=now and
             (not i.get('winner') or i['state'] in ('LIVE','UNKNOWN','CLOSED_UNCONFIRMED') or not i.get('binance'))]
        # Reconcile committed capital before buying; available future slots first.
        due.sort(key=lambda i:(0 if i['state'] in ('LIVE','UNKNOWN') or (i.get('committed_cost') and not i.get('winner')) else 1 if i['opening']>now else 2,
                               i['opening'] if i['opening']>now else i.get('next_check',0)))
        for i in due[:self.config['requests_per_cycle']]:
            if self.stop_requested() or (self.state_dir/'stop.request').exists() or self.api.now()-now>45:
                break
            try:
                with self.db:
                    if i['state']=='WAITING':
                        if not self.halted():
                            self.prepare(i,self.api.now())
                            if i['state']=='PREPARED' and self.config['mode']=='live':
                                self.submit(i,self.api.now())
                    elif i['state'] in ('PREPARED','WAITING_FUNDS') and self.config['mode']=='live' and now<i['opening']:
                        self.submit(i,self.api.now())
                    else:
                        self.observe(i,self.api.now())
                    if i['state']=='PREPARED' and self.config['mode']=='prepare':
                        save(self.db,i,next_check=now+30)
            except Exception as e:
                # No exception text from authenticated SDK is written (may contain request credentials).
                with self.db:
                    if i['state']=='SIGNING':
                        i['state']='PREPARED'
                    reason=type(e).__name__+(':'+str(e) if isinstance(e,PublicDataError) else '')
                    self.retry(i,reason,self.api.now())
        with self.db:
            meta(self.db,'last_public_success',time.time())
            meta(self.db,'server_now',self.api.now())
            meta(self.db,'clock_uncertainty',self.api.uncertainty)
