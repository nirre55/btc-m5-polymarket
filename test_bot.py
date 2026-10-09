import ast
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import bot
from engine import connect, initialize, schedule, intents, save, Engine, position, apply_reconciliation, exposure_ok, phase, meta
from frozen_calendar import load_rules, matches, next_activation, TZ, calendar_values
from public_api import quote, validate_market, official_winner
from reporting import report

D=Decimal
ROOT=Path(__file__).resolve().parent
NOW=int(datetime(2026,10,6,13,40,tzinfo=timezone.utc).timestamp())
OPEN=NOW+300


def market(opening=OPEN):
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    return {'slug':f'btc-updown-5m-{opening}','conditionId':'condition','eventStartTime':iso(opening),
            'endDate':iso(opening+300),'startDate':iso(opening-86400),
            'outcomes':'["Up","Down"]','clobTokenIds':'["up","down"]','version':'v1',
            'resolutionSource':'https://data.chain.link/streams/btc-usd-twap-60s-streams',
            'description':'Up if Chainlink TWAP is greater than or equal to reference.',
            'active':True,'acceptingOrders':True,'enableOrderBook':True,'closed':False}


def book(token='up',now=NOW):
    return {'asset_id':token,'market':'condition','timestamp':str(now*1000),
            'tick_size':'0.01','min_order_size':'5','neg_risk':False,
            'asks':[{'price':'0.52','size':'20'},{'price':'0.50','size':'2'}]}


class FakeAPI:
    def __init__(self):
        self.t=NOW;self.uncertainty=1;self.m=market();self.b=book();self.winner=None
    def now(self): return self.t
    def sync(self): return self.t
    def market(self,opening): return self.m if opening==OPEN else None
    def book(self,token): return {**self.b,'asset_id':token,'timestamp':str(self.t*1000)}
    def clob(self,cid):
        return {'condition_id':'condition','market_slug':self.m['slug'],'accepting_orders':True,
                'closed':bool(self.winner),'tokens':[{'token_id':t,'outcome':o,'winner':self.winner==o}
                     for t,o in [('up','Up'),('down','Down')]]}
    def binance(self,opening): return [[opening*1000,'100','100','100','100','0',(opening+300)*1000-1]]


class FakeAdapter:
    def __init__(self):
        self.signs=0;self.posts=0;self.ambiguous=False;self.available='100'
        self.rejected=False;self.funds_fail=False
        self.read={'status':'LIVE','matched_qty':'0','fills':[]}
    def funds(self,i,reservations,fee):
        if self.funds_fail: raise TimeoutError()
        return {'available':self.available}
    def sign(self,i): self.signs+=1;return object(),'oid'
    def submit(self,signed):
        self.posts+=1
        if self.ambiguous: raise TimeoutError()
        if self.rejected: return {'ok':False,'order_id':None,'status':'REJECTED','trade_ids':[], 'retryable_funds':True}
        return {'ok':True,'order_id':'oid','status':'live','trade_ids':[]}
    def reconcile(self,i): return self.read
    def cancel_expired_intent(self,i): return {'canceled':True,'terminal_acknowledged':True}


class TestFrozen(unittest.TestCase):
    def test_fresh_database_cannot_accept_changed_selection(self):
        with tempfile.TemporaryDirectory() as folder, patch('bot.load_rules',return_value=([], 'changed')):
            with self.assertRaises(ValueError): bot.open_db(Path(folder),bot.configuration(mode='prepare'))
            self.assertFalse((Path(folder)/'bot.sqlite3').exists())
    def test_exact_567_and_byte_provenance(self):
        rules,digest=load_rules()
        self.assertEqual(len(rules),567)
        self.assertEqual(digest,bot.FROZEN_SHA256)
        original=ROOT.parent.parent/'results/BTCUSDT_M5_DRYRUN_HANDOFF/selected_rules.json'
        if original.exists():
            self.assertEqual((ROOT/'selected_rules.json').read_bytes(),original.read_bytes())
        self.assertEqual(sum('classement_m5_toutes' in r['cohorts'] for r in rules),500)
        self.assertEqual(sum('m5_min100_taux60' in r['cohorts'] for r in rules),67)

    def test_calendar_functions_identical_to_dryrun(self):
        original=ROOT.parent/'BTCUSDT_M5_CALENDAR_DRYRUN/bot.py'
        if not original.exists():
            self.skipTest('source comparison requires the original research workspace; frozen SHA checked independently')
        old=ast.parse(original.read_text(encoding='utf-8'))
        new=ast.parse((ROOT/'frozen_calendar.py').read_text(encoding='utf-8'))
        for name in ['calendar_values','matches','next_activation','load_rules']:
            get=lambda tree:ast.dump(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name))
            self.assertEqual(get(old),get(new))

    def test_dst_fall_two_distinct_occurrences(self):
        r={'slot':18,'calendar':{}}
        a=int(datetime(2026,11,1,5,29,tzinfo=timezone.utc).timestamp()*1000)
        first=next_activation(r,a);second=next_activation(r,first)
        self.assertEqual(second-first,3600000)
        self.assertTrue(matches(r,first));self.assertTrue(matches(r,second))

    def test_dst_spring_nonexistent_skipped(self):
        r={'slot':30,'calendar':{}}
        a=int(datetime(2027,3,14,0,0,tzinfo=TZ).timestamp()*1000)
        result=next_activation(r,a)
        self.assertEqual(datetime.fromtimestamp(result/1000,TZ).day,15)

    def test_calendar_numeric_filters(self):
        d=datetime(2026,10,30,13,40,tzinfo=TZ)
        v=calendar_values(d)
        self.assertEqual(v,{'jour_semaine':4,'jour_mois':30,'mois':10,'trimestre':4,
                            'rang_mois':5,'distance_fin_mois':1,'dernier_jour_semaine':1,'weekend':0})
        r={'slot':164,'calendar':v}
        self.assertTrue(matches(r,int(d.timestamp()*1000)))
        r['calendar']={**v,'distance_fin_mois':0}
        self.assertFalse(matches(r,int(d.timestamp()*1000)))


class TestPublic(unittest.TestCase):
    def test_window_and_creation_are_distinct(self):
        self.assertEqual(validate_market(market(),OPEN),{'Up':'up','Down':'down'})
    def test_wrong_window_rejected(self):
        m=market();m['endDate']=market(OPEN+300)['endDate']
        with self.assertRaises(ValueError): validate_market(m,OPEN)
    def test_wrong_slug_rejected(self):
        with self.assertRaises(ValueError): validate_market(market(OPEN-300),OPEN)
    def test_v2_outcome_identifier(self):
        m=market();m.update(version='v2',positionIds=['pos-up','pos-down'])
        self.assertEqual(validate_market(m,OPEN)['Down'],'pos-down')
    def test_best_ask_unsorted_and_minimum(self):
        p,s,t,a=quote(book(),'up','condition',D(NOW))
        self.assertEqual((p,s,t),(D('.50'),D('5.00'),D('.01')))
    def test_round_tick_up_and_minimum_up(self):
        b=book();b['asks']=[{'price':'.503','size':'2'}];b['min_order_size']='5.001'
        p,s,_,_=quote(b,'up','condition',D(NOW))
        self.assertEqual((p,s),(D('.51'),D('5.01')))
    def test_stale_future_and_empty_books(self):
        for b in [{**book(),'timestamp':str((NOW-60)*1000)},
                  {**book(),'timestamp':str((NOW+3)*1000)}, {**book(),'asks':[]}]:
            with self.assertRaises(ValueError): quote(b,'up','condition',D(NOW))
    def test_wrong_token_or_condition(self):
        for token,cid in [('down','condition'),('up','other')]:
            with self.assertRaises(ValueError): quote(book(),token,cid,D(NOW))
    def test_nan_and_bad_tick_rejected(self):
        for b in [{**book(),'min_order_size':'NaN'},{**book(),'tick_size':'0.02'},
                  {**book(),'asks':[{'price':'NaN','size':'2'}]}]:
            with self.assertRaises(ValueError): quote(b,'up','condition',D(NOW))
    def test_resolution_requires_closed_and_one_winner(self):
        c={'closed':False,'tokens':[{'outcome':'Up','winner':True}]}
        self.assertIsNone(official_winner(c));c['closed']=True
        self.assertEqual(official_winner(c),'Up')
        c['tokens'].append({'outcome':'Down','winner':True})
        self.assertIsNone(official_winner(c))


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.db=connect(self.folder/'test.sqlite3');self.api=FakeAPI()
        d=datetime.fromtimestamp(OPEN,TZ);slot=d.hour*12+d.minute//5
        self.rules=[{'id':'a','prediction':'V','slot':slot,'calendar':{},'cohorts':['classement_m5_toutes']},
                    {'id':'b','prediction':'V','slot':slot,'calendar':{},'cohorts':['m5_min100_taux60']},
                    {'id':'c','prediction':'R','slot':slot,'calendar':{},'cohorts':['classement_m5_toutes']}]
        self.config=bot.configuration(mode='prepare')
        initialize(self.db,self.rules,'digest','prepare')
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder)
        schedule(self.db,self.rules,NOW,self.config)
    def tearDown(self): self.db.close();self.temp.cleanup()
    def one(self,direction='Up'): return next(i for i in intents(self.db) if i['opening']==OPEN and i['direction']==direction)
    def prepared(self):
        i=self.one()
        with self.db: self.engine.prepare(i,NOW)
        return i
    def live(self):
        self.config.update(mode='live',max_order_cost='10',max_total_committed_cost='100',
                           max_daily_committed_cost='100',max_open_orders=10)
        adapter=FakeAdapter();self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter)
        return adapter
    def test_grouping_and_no_hidden_vote(self):
        rows=[i for i in intents(self.db) if i['opening']==OPEN]
        self.assertEqual(len(rows),2);self.assertTrue(all(i['conflict'] for i in rows))
        self.assertEqual(self.db.execute('SELECT count(*) FROM links WHERE intent_id=?',(self.one()['id'],)).fetchone()[0],2)
    def test_schedule_idempotence(self):
        before=len(intents(self.db));schedule(self.db,self.rules,NOW,self.config)
        self.assertEqual(len(intents(self.db)),before)
    def test_future_preparation_and_zero_position(self):
        i=self.prepared();self.assertEqual(i['state'],'PREPARED');self.assertLess(i['prepared_at'],OPEN)
        self.assertEqual(position(self.db,i)['quantity'],'0');self.assertIsNone(position(self.db,i)['result'])
    def test_no_current_market_substitution(self):
        self.api.m=None;i=self.one()
        with self.db: self.engine.prepare(i,NOW)
        self.assertEqual(i['state'],'WAITING');self.assertNotIn('price',i)
        self.assertIn('not_listed',i['last_error'])
    def test_missing_future_retry_then_prepare(self):
        self.api.m=None;i=self.one()
        with self.db: self.engine.prepare(i,NOW)
        self.api.m=market()
        with self.db: self.engine.prepare(i,NOW+20)
        self.assertEqual(i['state'],'PREPARED')
    def test_absent_distant_market_checked_hourly(self):
        i=self.one();i['opening']=NOW+7200
        with self.db: self.engine.retry(i,'exact_future_market_not_listed',NOW)
        self.assertEqual(i['next_check'],NOW+3600)
    def test_absent_market_gets_one_final_check_before_opening(self):
        i=self.one();i['opening']=NOW+1800
        with self.db: self.engine.retry(i,'exact_future_market_not_listed',NOW)
        self.assertEqual(i['next_check'],i['opening']-60)
        with self.db: self.engine.retry(i,'exact_future_market_not_listed',i['opening']-60)
        self.assertEqual(i['next_check'],i['opening'])
        self.api.t=i['opening']
        with self.db: self.engine.prepare(i,self.api.t)
        self.assertEqual(i['state'],'MISSED');self.assertNotIn('price',i)
    def test_existing_market_errors_do_not_wait_an_hour(self):
        i=self.one()
        with self.db: self.engine.retry(i,'not_accepting_orders',NOW)
        self.assertLess(i['next_check'],NOW+3600)
    def test_never_prepare_after_opening(self):
        i=self.one();self.api.t=OPEN
        with self.db: self.engine.prepare(i,OPEN)
        self.assertEqual(i['state'],'MISSED');self.assertNotIn('price',i)
    def test_no_reprice_on_followup(self):
        i=self.prepared();initial=i['price'];self.api.b['asks']=[{'price':'.70','size':'50'}]
        with self.db: self.engine.observe(i,NOW+20)
        self.assertEqual(i['price'],initial)
    def test_paper_partial_once_and_expired_remainder(self):
        self.config['mode']='paper';i=self.one();i['mode']='paper'
        with self.db: self.engine.prepare(i,NOW)
        self.assertEqual(i['state'],'PAPER_OPEN');self.assertEqual(position(self.db,i)['quantity'],'2')
        self.api.t=OPEN+302
        with self.db: self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'PAPER_EXPIRED_REMAINDER');self.assertEqual(position(self.db,i)['quantity'],'2')
        self.assertIsNone(position(self.db,i)['result']);self.assertEqual(i['binance']['hypothesis_result'],'LOSS')
        self.api.winner='Up'
        with self.db: self.engine.observe(i,self.api.t+30)
        self.assertEqual(position(self.db,i)['result'],'WIN')
    def test_binance_doji_does_not_determine_polymarket(self):
        i=self.prepared();self.api.t=OPEN+302;self.api.winner='Up'
        with self.db: self.engine.observe(i,self.api.t)
        self.assertEqual(i['winner'],'Up');self.assertEqual(i['binance']['color'],'DOJI')
        self.assertEqual(i['binance']['hypothesis_result'],'LOSS');self.assertIsNone(position(self.db,i)['result'])
    def test_simulated_and_real_bases_cannot_mix(self):
        with self.assertRaises(ValueError): initialize(self.db,self.rules,'digest','live')
    def test_selection_mutation_rejected(self):
        with self.assertRaises(ValueError): initialize(self.db,self.rules,'other','prepare')
    def test_public_modes_reject_execution_adapter(self):
        with self.assertRaises(ValueError): Engine(self.db,self.rules,self.config,self.api,self.folder,FakeAdapter())
    def test_halt_blocks_live_post(self):
        i=self.prepared();adapter=self.live();(self.folder/'HALT').touch()
        self.engine.submit(i,NOW);self.assertEqual(adapter.posts,0);self.assertEqual(adapter.signs,0)
    def test_sigterm_stop_callback_blocks_new_orders(self):
        i=self.prepared();adapter=self.live()
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter,stop_requested=lambda:True)
        self.engine.submit(i,NOW)
        self.assertEqual(adapter.posts,0);self.assertEqual(adapter.signs,0)
    def test_cooperative_stop_file_blocks_new_orders(self):
        i=self.prepared();adapter=self.live();(self.folder/'stop.request').touch()
        self.engine.submit(i,NOW)
        self.assertEqual(adapter.posts,0);self.assertEqual(adapter.signs,0)
    def test_ambiguous_submission_no_retry_after_restart(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW)
        self.assertEqual(i['state'],'UNKNOWN');self.assertEqual(adapter.posts,1)
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter)
        self.engine.submit(i,NOW)
        self.assertEqual(adapter.posts,1)
        second=self.one('Down')
        with self.db: self.engine.prepare(second,NOW)
        adapter.ambiguous=False
        self.engine.submit(second,NOW)
        self.assertEqual(adapter.posts,2);self.assertEqual(second['state'],'LIVE')
        reservations=self.engine.funds_reservations(NOW+100)
        self.assertTrue(any(D(x['missing'])==D(i['committed_cost']) for x in reservations))
    def test_insufficient_funds_restart_then_credit_preserves_price(self):
        i=self.prepared();price=i['price'];adapter=self.live();adapter.available='0'
        self.engine.submit(i,NOW)
        self.assertEqual(i['state'],'WAITING_FUNDS');self.assertEqual(adapter.signs,0)
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter)
        i=self.one();adapter.available='10'
        self.api.b['asks']=[{'price':'.70','size':'50'}]
        self.engine.submit(i,NOW+30)
        self.assertEqual(i['state'],'LIVE');self.assertEqual(i['price'],price)
        self.assertEqual(adapter.posts,1)
    def test_funds_read_failure_never_signs(self):
        i=self.prepared();adapter=self.live();adapter.funds_fail=True
        with self.assertRaises(TimeoutError): self.engine.submit(i,NOW)
        self.assertEqual(adapter.signs,0);self.assertEqual(adapter.posts,0)
    def balance_policy(self):
        adapter=self.live()
        self.config.update(use_available_balance=True,balance_retry_seconds=7200,
            max_order_cost=None,max_total_committed_cost=None,max_daily_committed_cost=None,max_open_orders=None)
        return adapter
    def test_unknown_cash_is_unavailable_to_other_orders(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW)
        second=self.one('Down')
        with self.db: self.engine.prepare(second,NOW)
        adapter.ambiguous=False
        adapter.funds=lambda intent,reservations,fee:{'available':str(max(D(0),D('3')-sum((D(x['missing'])+D(x['pending']) for x in reservations),D(0))))}
        self.engine.submit(second,NOW+100)
        self.assertEqual(adapter.posts,1);self.assertEqual(second['state'],'WAITING_FUNDS')
    def test_stale_book_retries_fresh_window_without_long_backoff(self):
        i=self.prepared();i['attempts']=100
        with self.db:self.engine.retry(i,'PublicDataError:stale or future book',NOW)
        self.assertEqual(i['next_check'],NOW+15)
    def test_expired_unknown_releases_cash_after_terminal_reply_and_checks(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW);i['winner']='Down'
        adapter.read={'status':'UNKNOWN','matched_qty':'0','fills':[]}
        self.api.t=OPEN+1300
        with self.db:self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'CLOSED_UNCONFIRMED')
        self.assertEqual(self.engine.funds_reservations(self.api.t),[])
        self.engine.submit(i,self.api.t);self.assertEqual(adapter.posts,1)
    def test_expiration_alone_never_releases_uncertain_cash(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW);i['winner']='Down'
        adapter.read={'status':'UNKNOWN','matched_qty':'0','fills':[]}
        adapter.cancel_expired_intent=lambda i:{'canceled':False,'terminal_acknowledged':False}
        self.api.t=OPEN+1300
        with self.db:self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'UNKNOWN')
        self.assertTrue(self.engine.funds_reservations(self.api.t))
    def test_canceled_unknown_waits_for_settlement_grace(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW);i['winner']='Down'
        adapter.read={'status':'UNKNOWN','matched_qty':'0','fills':[]}
        self.api.t=OPEN+600
        with self.db:self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'UNKNOWN')
    def test_cancel_failure_still_reconciles_confirmed_execution(self):
        i=self.prepared();adapter=self.live();self.engine.submit(i,NOW)
        def failed(i):raise TimeoutError()
        adapter.cancel_expired_intent=failed
        adapter.read={'status':'FILLED','matched_qty':'5','fills':[{'id':'fill','leg':'taker',
            'qty':'5','price':i['price'],'status':'CONFIRMED'}]}
        self.api.t=OPEN+1300
        with self.db:self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'FILLED');self.assertEqual(position(self.db,i)['quantity'],'5')
    def test_pending_settlement_keeps_cash_reserved(self):
        i=self.prepared();adapter=self.live();adapter.ambiguous=True
        self.engine.submit(i,NOW);i['winner']='Down'
        adapter.read={'status':'UNKNOWN','matched_qty':'2','fills':[{'id':'fill','leg':'taker',
            'qty':'2','price':i['price'],'status':'MINED'}]}
        self.api.t=OPEN+1300
        with self.db:self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'UNKNOWN');self.assertTrue(self.engine.funds_reservations(self.api.t))
    def test_five_minute_cash_pause_repeats_and_resumes(self):
        i=self.prepared();adapter=self.balance_policy()
        self.config['balance_retry_seconds']=300;adapter.available='0'
        self.engine.submit(i,NOW)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+300)
        self.engine.poll_paused_funds(NOW+300)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+600)
        adapter.available='10';self.engine.poll_paused_funds(NOW+600)
        self.assertEqual(meta(self.db,'funds_retry_at'),0)
    def test_high_water_persists_after_loss_restart_and_new_maximum(self):
        i=self.prepared();adapter=self.live();self.config['position_balance_percent']='3'
        for cash,expected in [('100','100'),('97','100'),('200','200'),('150','200')]:
            adapter.funds=lambda *args,cash=cash:{'balance':cash,'available':cash}
            self.engine.read_funds(i,NOW)
            self.assertEqual(D(meta(self.db,'balance_high_water')),D(expected))
            self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter)
            self.assertEqual(self.engine.position_size(D('.5'),D('5')),D(expected)*D('.03')/D('.5'))
    def test_percent_size_floor_rounding_and_config_change(self):
        self.config['position_balance_percent']='3'
        with self.db:meta(self.db,'balance_high_water','50')
        self.assertEqual(self.engine.position_size(D('.5'),D('5')),D('5'))
        with self.db:meta(self.db,'balance_high_water','100')
        self.assertEqual(self.engine.position_size(D('.52'),D('5')),D('5.76'))
        self.assertLessEqual(self.engine.position_size(D('.52'),D('5'))*D('.52'),D('3'))
        self.config['position_balance_percent']='4'
        self.assertEqual(self.engine.position_size(D('.5'),D('5')),D('8'))
    def test_percent_sizes_before_signing_and_reserves_fees(self):
        i=self.prepared();adapter=self.live();self.config['position_balance_percent']='3'
        adapter.funds=lambda *args:{'balance':'100','available':'100'}
        self.engine.submit(i,NOW)
        self.assertEqual(D(i['size']),D('6'))
        self.assertEqual(D(i['committed_cost']),D(i['size'])*D(i['price'])*D('1.10'))
    def test_reference_survives_later_transaction_rollback(self):
        i=self.prepared();adapter=self.live();self.config['position_balance_percent']='3'
        adapter.funds=lambda *args:{'balance':'100','available':'100'}
        with self.assertRaises(ValueError):
            with self.db:
                self.engine.read_funds(i,NOW)
                raise ValueError('later_book_failure')
        self.assertEqual(meta(self.db,'balance_high_water'),'100')
    def test_insufficient_cash_never_shrinks_percent_position(self):
        i=self.prepared();adapter=self.live();self.config['position_balance_percent']='3'
        with self.db:meta(self.db,'balance_high_water','200')
        adapter.funds=lambda *args:{'balance':'5','available':'5'}
        self.engine.submit(i,NOW)
        self.assertEqual(adapter.signs,0);self.assertEqual(i['state'],'WAITING_FUNDS')
        self.assertGreater(D(i['size'])*D(i['price']),D('5'))
    def test_balance_policy_without_fixed_caps_still_checks_cash(self):
        i=self.prepared();adapter=self.balance_policy();adapter.available='0'
        self.assertTrue(exposure_ok(self.db,i,self.config,NOW)[0])
        self.engine.submit(i,NOW)
        self.assertEqual(i['state'],'WAITING_FUNDS');self.assertEqual(adapter.signs,0)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+7200)
    def test_global_two_hour_pause_survives_restart_and_repeats_then_resumes(self):
        i=self.prepared();i['opening']=NOW+20000
        self.api.m=market(i['opening']);self.api.market=lambda opening:self.api.m
        with self.db: save(self.db,i)
        adapter=self.balance_policy();calls=[]
        def funds(*args): calls.append(self.api.t);return {'available':adapter.available}
        adapter.funds=funds;adapter.available='0'
        with self.db: self.engine.submit(i,NOW)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+7200)
        self.db.close();self.db=connect(self.folder/'test.sqlite3')
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder,adapter)
        self.engine.submit(i,NOW+3600)
        self.engine.poll_paused_funds(NOW+7199)
        self.assertEqual(len(calls),1);self.assertEqual(adapter.signs,0)
        self.engine.poll_paused_funds(NOW+7200)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+14400)
        adapter.available='10'
        self.engine.poll_paused_funds(NOW+14399)
        self.assertEqual(len(calls),2)
        self.api.t=NOW+14400
        self.engine.poll_paused_funds(self.api.t)
        self.assertEqual(meta(self.db,'funds_retry_at'),0)
        i=next(x for x in intents(self.db) if x['id']==i['id'])
        self.engine.submit(i,self.api.t)
        self.assertEqual(i['state'],'LIVE');self.assertEqual(adapter.posts,1)
        self.assertEqual(i['price'],'0.50')
    def test_pause_blocks_other_direction_without_read_or_sign(self):
        i=self.prepared();adapter=self.balance_policy();adapter.available='0'
        self.engine.submit(i,NOW)
        other=self.one('Down')
        with self.db: self.engine.prepare(other,NOW)
        adapter.available='100'
        self.engine.submit(other,NOW+10)
        self.assertEqual(other['state'],'WAITING_FUNDS');self.assertEqual(adapter.signs,0)
        self.assertEqual(other['next_check'],OPEN)
    def test_expired_intent_not_resurrected_by_balance_credit(self):
        i=self.prepared();adapter=self.balance_policy();adapter.available='0'
        self.engine.submit(i,NOW)
        self.api.t=OPEN+1
        with self.db: self.engine.observe(i,self.api.t)
        adapter.available='100';self.api.t=NOW+7200
        self.engine.poll_paused_funds(self.api.t)
        self.engine.submit(i,self.api.t)
        self.assertEqual(i['state'],'PREPARED_NOT_SUBMITTED');self.assertEqual(adapter.posts,0)
    def test_balance_read_failure_at_wakeup_keeps_new_orders_blocked(self):
        i=self.prepared();adapter=self.balance_policy();adapter.available='0'
        self.engine.submit(i,NOW);adapter.funds_fail=True
        self.engine.poll_paused_funds(NOW+7200)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+7500)
        self.assertEqual(meta(self.db,'funds_read_error'),'TimeoutError')
        self.assertEqual(adapter.signs,0)
    def test_explicit_funds_rejection_starts_two_hour_pause(self):
        i=self.prepared();adapter=self.balance_policy();adapter.rejected=True
        self.engine.submit(i,NOW)
        self.assertNotIn('committed_cost',i)
        self.assertEqual(meta(self.db,'funds_retry_at'),NOW+7200)
        self.engine.submit(i,NOW+30);self.assertEqual(adapter.posts,1)
    def test_explicit_funds_rejection_retries_without_commitment(self):
        i=self.prepared();adapter=self.live();adapter.rejected=True
        self.engine.submit(i,NOW)
        self.assertEqual(i['state'],'WAITING_FUNDS');self.assertNotIn('committed_cost',i)
        adapter.rejected=False;self.engine.submit(i,NOW+30)
        self.assertEqual(i['state'],'LIVE');self.assertEqual(adapter.posts,2)
    def test_waiting_funds_past_opening_is_never_submitted(self):
        i=self.prepared();adapter=self.live();adapter.available='0'
        self.engine.submit(i,NOW);self.api.t=OPEN+1
        self.engine.submit(i,OPEN+1)
        self.engine.observe(i,OPEN+1)
        self.assertEqual(i['state'],'PREPARED_NOT_SUBMITTED');self.assertEqual(adapter.posts,0)
    def test_resolved_exposure_recycles_but_daily_limit_does_not(self):
        first=self.prepared();adapter=self.live();first['mode']='live'
        self.engine.submit(first,NOW)
        adapter.read={'status':'FILLED','matched_qty':'5','fills':[
            {'id':'f','leg':'taker','qty':'5','price':'.5','status':'CONFIRMED'}]}
        self.api.winner='Down';self.api.t=OPEN+302
        self.engine.observe(first,self.api.t)
        second=self.one('Down');second.update(price='.5',size='5')
        self.config.update(max_total_committed_cost='3',max_daily_committed_cost='3')
        self.assertEqual(exposure_ok(self.db,second,self.config,NOW)[1],'max_daily_committed_cost')
        self.config['max_daily_committed_cost']='100'
        self.assertTrue(exposure_ok(self.db,second,self.config,NOW)[0])
        # Resolution releases the risk cap, but it never invents cash credit.
        self.api.winner=None;self.api.t=NOW;adapter.available='0'
        with self.db: self.engine.prepare(second,NOW)
        self.engine.submit(second,NOW)
        self.assertEqual(second['state'],'WAITING_FUNDS')
    def test_filled_unresolved_position_still_occupies_exposure(self):
        first=self.prepared();adapter=self.live();first['mode']='live'
        self.engine.submit(first,NOW)
        adapter.read={'status':'FILLED','matched_qty':'5','fills':[
            {'id':'f','leg':'taker','qty':'5','price':'.5','status':'CONFIRMED'}]}
        self.engine.observe(first,NOW)
        second=self.one('Down');second.update(price='.5',size='5')
        self.config['max_open_orders']=1
        self.assertEqual(exposure_ok(self.db,second,self.config,NOW)[1],'max_open_orders')
    def test_ten_funded_and_seven_waiting_resume_after_cash_credit(self):
        template=self.prepared();adapter=self.live();cash=[D('27.5')]
        self.config.update(max_open_orders=30,max_total_committed_cost='100',max_daily_committed_cost='100')
        adapter.funds=lambda *args:{'available':str(cash[0])}
        def post(signed):
            adapter.posts+=1;cash[0]-=D('2.75')
            return {'ok':True,'order_id':signed,'status':'live','trade_ids':[]}
        adapter.submit=post
        adapter.sign=lambda i:(i['id'],i['id'])
        def exact_market(opening):
            m=market();m.update(slug=f'btc-updown-5m-{opening}',
                               eventStartTime=datetime.fromtimestamp(opening,timezone.utc).isoformat(),
                               endDate=datetime.fromtimestamp(opening+300,timezone.utc).isoformat())
            m['events']=[];self.api.m=m;return m
        self.api.market=exact_market
        queue=[]
        with self.db:
            self.db.execute('DELETE FROM links')
            self.db.execute('DELETE FROM intents')
            for k in range(17):
                i={**template,'id':f'queue-{k}','opening':OPEN+k*300,'mode':'live','state':'PREPARED'}
                self.db.execute('INSERT INTO intents VALUES (?,?,?,?,?,0)',
                                (i['id'],i['opening'],i['direction'],i['state'],json.dumps(i)))
                self.engine.submit(i,NOW);queue.append(i)
        self.assertEqual(adapter.posts,10)
        self.assertEqual(sum(i['state']=='WAITING_FUNDS' for i in queue),7)
        cash[0]=D('19.25')  # externally credited cash, not theoretical winners
        with self.db:
            for i in queue:
                if i['state']=='WAITING_FUNDS': self.engine.submit(i,NOW+30)
        self.assertEqual(adapter.posts,17)
        self.assertTrue(all(i['state']=='LIVE' for i in queue))
    def test_72_hour_horizon_and_absent_market_survives_restart(self):
        self.assertEqual(self.config['horizon_hours'],72)
        self.assertTrue(any(i['opening']>=OPEN+2*86400 for i in intents(self.db)))
        i=self.one();self.api.m=None
        with self.db: self.engine.prepare(i,NOW)
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder)
        self.assertEqual(self.one()['state'],'WAITING')
    def test_crash_sending_recovered_as_unknown(self):
        i=self.prepared()
        with self.db: save(self.db,i,'SENDING')
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder)
        self.assertEqual(self.one()['state'],'UNKNOWN')
    def test_exposure_no_arbitrary_budget(self):
        i=self.prepared();self.config['mode']='live'
        self.assertEqual(exposure_ok(self.db,i,self.config,NOW),(False,'live_limits_required'))
        self.config.update(max_order_cost='1',max_total_committed_cost='10',max_daily_committed_cost='10',max_open_orders=1)
        self.assertEqual(exposure_ok(self.db,i,self.config,NOW)[1],'max_order_cost')
    def test_shared_exposure_counts_both_directions(self):
        first=self.prepared();first.update(committed_cost='2.75',committed_at=NOW)
        with self.db: save(self.db,first,'LIVE')
        second=self.one('Down')
        with self.db: self.engine.prepare(second,NOW)
        self.config.update(mode='live',max_order_cost='10',max_total_committed_cost='4',
                           max_daily_committed_cost='100',max_open_orders=10)
        self.assertEqual(exposure_ok(self.db,second,self.config,NOW)[1],'max_total_committed_cost')
    def test_restart_preserves_initial_quote_and_links(self):
        i=self.prepared();initial=i['price'];identity=i['id']
        self.db.close();self.db=connect(self.folder/'test.sqlite3')
        initialize(self.db,self.rules,'digest','prepare')
        self.engine=Engine(self.db,self.rules,self.config,self.api,self.folder)
        self.assertEqual(self.one()['price'],initial);self.assertEqual(self.one()['id'],identity)
        self.assertEqual(self.db.execute('SELECT count(*) FROM links WHERE intent_id=?',(identity,)).fetchone()[0],2)
    def test_canceled_partial_position_survives_resolution(self):
        i=self.prepared();adapter=self.live();i['mode']='live';i['order_id']='oid'
        with self.db: save(self.db,i,'LIVE')
        adapter.read={'status':'CANCELED_MARKET_RESOLVED','matched_qty':'2',
                      'fills':[{'id':'t1','leg':'maker','qty':'2','price':'.50','status':'CONFIRMED'}]}
        self.api.winner='Down';self.api.t=OPEN+302
        with self.db: self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'CANCELED_MARKET_RESOLVED')
        self.assertEqual(position(self.db,i)['quantity'],'2');self.assertEqual(position(self.db,i)['result'],'LOSS')
    def test_gtc_does_not_claim_local_expiration(self):
        self.config.update(mode='paper',order_type='GTC');i=self.one();i['mode']='paper'
        with self.db: self.engine.prepare(i,NOW)
        self.api.t=OPEN+302
        with self.db: self.engine.observe(i,self.api.t)
        self.assertEqual(i['state'],'PAPER_OPEN')
    def test_overfilled_response_rolled_back(self):
        i=self.prepared();r={'status':'LIVE','matched_qty':'5','fills':[
            {'id':'t1','leg':'taker','qty':'3','price':'.5','status':'CONFIRMED'},
            {'id':'t2','leg':'taker','qty':'3','price':'.5','status':'CONFIRMED'}]}
        with self.assertRaises(ValueError):
            with self.db: apply_reconciliation(self.db,i,r)
        self.assertEqual(position(self.db,i)['quantity'],'0')
    def test_confirmed_partial_fill_idempotent_and_pending_distinct(self):
        i=self.prepared();i['mode']='live'
        f={'id':'t1','leg':'taker','qty':'2','price':'.49','status':'TRADE_STATUS_MATCHED'}
        r={'status':'LIVE','matched_qty':'2','fills':[f]}
        with self.db: apply_reconciliation(self.db,i,r)
        self.assertEqual(position(self.db,i)['quantity'],'0')
        f['status']='TRADE_STATUS_CONFIRMED';i['winner']='Up'
        with self.db: apply_reconciliation(self.db,i,r);apply_reconciliation(self.db,i,r)
        self.assertEqual(position(self.db,i)['quantity'],'2');self.assertEqual(position(self.db,i)['result'],'WIN')
        self.assertEqual(self.db.execute('SELECT count(*) FROM fills').fetchone()[0],1)
    def test_confirmed_trade_cannot_regress(self):
        i=self.prepared();f={'id':'t1','leg':'taker','qty':'2','price':'.5','status':'CONFIRMED'}
        r={'status':'LIVE','matched_qty':'2','fills':[f]}
        with self.db: apply_reconciliation(self.db,i,r)
        f['status']='FAILED'
        with self.assertRaises(ValueError):
            with self.db: apply_reconciliation(self.db,i,r)
    def test_phase_is_not_order_or_position(self):
        i=self.prepared();self.assertEqual(phase(i,NOW),'BEFORE_START')
        self.assertEqual(phase(i,OPEN),'IN_PROGRESS')
        self.assertEqual(phase(i,OPEN+300),'ENDED_AWAITING_OFFICIAL_RESOLUTION')
    def test_conflict_skip_both_policy(self):
        other=connect(self.folder/'skip.sqlite3');initialize(other,self.rules,'digest','prepare')
        self.config['conflict_policy']='skip_both';schedule(other,self.rules,NOW,self.config)
        self.assertTrue(all(i['state']=='CONFLICT_SKIPPED' for i in intents(other)))
        other.close()
    def test_report_n_zero_unavailable_and_frozen_histories(self):
        rules,_=load_rules();db=connect(self.folder/'full.sqlite3');initialize(db,rules,'digest','prepare')
        summary=report(db,rules,self.folder,self.config,False)
        self.assertEqual(summary['confirmed_or_simulated_positions'],0)
        data=json.loads((self.folder/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(all(r['winrate'] is None for r in data['rules']))
        self.assertEqual(data['rules'][0]['historical'],rules[0]['historical']);db.close()


class TestSDKOffline(unittest.TestCase):
    def test_available_balance_activation_needs_explicit_policy_and_live_flag(self):
        c=bot.configuration(mode='prepare');c.update(enable_live=True,use_available_balance=True)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'local.json';p.write_text(json.dumps(c))
            with patch.dict('os.environ',{'POLY_ENABLE_LIVE':'I_ACCEPT_LIVE_ORDERS'}):
                self.assertTrue(bot.configuration(p,'live')['use_available_balance'])
                c['use_available_balance']=False;p.write_text(json.dumps(c))
                with self.assertRaises(ValueError): bot.configuration(p,'live')
            c['use_available_balance']=True;p.write_text(json.dumps(c))
            with patch.dict('os.environ',{'POLY_ENABLE_LIVE':''}):
                with self.assertRaises(ValueError): bot.configuration(p,'live')
    def test_invalid_balance_policy_rejected(self):
        c=bot.configuration(mode='prepare')
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'local.json'
            for changes in ({'use_available_balance':'true'},{'balance_retry_seconds':0},
                            {'balance_retry_seconds':7200.5}):
                p.write_text(json.dumps({**c,**changes}))
                with self.assertRaises(ValueError): bot.configuration(p,'prepare')
    def funds_adapter(self,balance=10000000,orders=()):
        from execution import LiveAdapter
        from polymarket.environments import PRODUCTION
        from polymarket._internal.actions.orders.context import resolve_order_exchange_address
        config=PRODUCTION._config
        spender=str(resolve_order_exchange_address(config,asset_id='123',neg_risk=False))
        adapter=object.__new__(LiveAdapter)
        adapter.client=SimpleNamespace(wallet_type='EOA',_ctx=SimpleNamespace(environment_config=config,
            secure_clob=SimpleNamespace(get_bytes=lambda *a,**kw:b'')),
            get_balance_allowance=lambda **kw:SimpleNamespace(balance=balance,allowances={spender.upper():10000000}),
            list_open_orders=lambda:SimpleNamespace(iter_items=lambda:iter(orders)))
        return adapter
    def test_account_buy_reservations_and_units_include_other_bots(self):
        orders=[SimpleNamespace(id='external',side='BUY',price=D('.5'),original_size=D('10'),size_matched=D('2')),
                SimpleNamespace(id='sell',side='SELL',price=D('.5'),original_size=D('10'),size_matched=D('0'))]
        adapter=self.funds_adapter(orders=orders)
        f=adapter.funds({'token':'123','neg_risk':False},[],'.10')
        self.assertEqual(D(f['balance']),D('10'))
        self.assertEqual(D(f['open_buy_reserved']),D('4.4'))
        self.assertEqual(D(f['available']),D('5.6'))
    def test_recent_order_not_yet_visible_blocks_overspending(self):
        adapter=self.funds_adapter(balance=3000000)
        f=adapter.funds({'token':'123','neg_risk':False},[
            {'order_id':'missing','missing':'2.75','pending':'0'}],'.10')
        self.assertEqual(D(f['available']),D('.25'))
    def test_own_open_order_not_double_counted_and_pending_fill_reserved(self):
        order=SimpleNamespace(id='own',side='BUY',price=D('.5'),original_size=D('5'),size_matched=D('2'))
        adapter=self.funds_adapter(orders=[order])
        f=adapter.funds({'token':'123','neg_risk':False},[
            {'order_id':'own','missing':'2.75','pending':'1.1'}],'.10')
        self.assertEqual(D(f['available']),D('7.25'))
    def test_missing_allowance_waits_without_approving(self):
        adapter=self.funds_adapter()
        adapter.client.get_balance_allowance=lambda **kw:SimpleNamespace(balance=10000000,allowances={})
        self.assertEqual(adapter.funds({'token':'123','neg_risk':False},[],'.1')['available'],'0')
    def test_invalid_account_amounts_fail_closed(self):
        order=SimpleNamespace(id='bad',side='BUY',price=D('.5'),original_size=D('5'),size_matched=D('6'))
        with self.assertRaises(ValueError): self.funds_adapter(orders=[order]).funds({'token':'123','neg_risk':False},[],'.1')
        with self.assertRaises(ValueError): self.funds_adapter(balance=-1).funds({'token':'123','neg_risk':False},[],'.1')
    def test_balance_index_refresh_is_get_and_rate_limited(self):
        adapter=self.funds_adapter();calls=[]
        adapter.client._ctx.secure_clob.get_bytes=lambda path,**kw:calls.append((path,kw))
        with patch('execution.time.monotonic',side_effect=[100,110,131]):
            for _ in range(3): adapter.funds({'token':'123','neg_risk':False},[],'.1')
        self.assertEqual(len(calls),2);self.assertEqual(calls[0][0],'/balance-allowance/update')
    def test_http400_funds_rejection_is_safe_but_timeout_is_ambiguous(self):
        from polymarket.errors import RequestRejectedError
        adapter=self.funds_adapter()
        def rejected(_): raise RequestRejectedError('not enough balance / allowance',status=400)
        adapter.client.post_order=rejected
        self.assertTrue(adapter.submit(object())['retryable_funds'])
        def timeout(_): raise TimeoutError()
        adapter.client.post_order=timeout
        with self.assertRaises(TimeoutError): adapter.submit(object())
    def test_four_variable_configuration_derives_credentials(self):
        from execution import credential_options
        options,kind=credential_options({'POLYMARKET_PRIVATE_KEY':'fixture-key','POLYMARKET_FUNDER':'fixture-wallet',
                         'POLYMARKET_SIGNATURE_TYPE':'3','POLYMARKET_API_URL':'https://clob.polymarket.com'})
        self.assertEqual(options,{'private_key':'fixture-key','wallet':'fixture-wallet'})
        self.assertEqual(kind,3);self.assertNotIn('credentials',options)
    def test_legacy_manual_credentials_still_supported(self):
        from execution import credential_options
        options,kind=credential_options({'POLY_PRIVATE_KEY':'fixture-key','POLY_WALLET_ADDRESS':'fixture-wallet',
                        'POLY_API_KEY':'fixture-api','POLY_API_SECRET':'fixture-secret','POLY_API_PASSPHRASE':'fixture-pass'})
        self.assertEqual(options['credentials'].key,'fixture-api');self.assertIsNone(kind)
    def test_configuration_rejects_conflicts_partial_trio_and_other_endpoint(self):
        from execution import credential_options
        base={'POLYMARKET_PRIVATE_KEY':'fixture-key','POLYMARKET_FUNDER':'fixture-wallet'}
        for override in [{'POLY_PRIVATE_KEY':'other-key'},{'POLY_API_KEY':'partial'},
                         {'POLYMARKET_API_URL':'https://example.invalid'},{'POLYMARKET_SIGNATURE_TYPE':'4'}]:
            with self.assertRaises(RuntimeError): credential_options({**base,**override})
    def test_bootstrap_is_mocked_and_checks_deposit_wallet_type(self):
        from execution import LiveAdapter
        from polymarket import SecureClient
        from unittest.mock import MagicMock
        env={'POLY_ENABLE_LIVE':'I_ACCEPT_LIVE_ORDERS','POLYMARKET_PRIVATE_KEY':'fixture-key',
             'POLYMARKET_FUNDER':'fixture-wallet','POLYMARKET_SIGNATURE_TYPE':'3'}
        fake=MagicMock();fake.wallet_type='DEPOSIT_WALLET'
        with patch('execution.os.environ',env),patch.object(SecureClient,'create',return_value=fake) as create:
            adapter=LiveAdapter({'mode':'live','enable_live':True})
            self.assertIs(adapter.client,fake)
            self.assertEqual(create.call_args.kwargs,{'private_key':'fixture-key','wallet':'fixture-wallet'})
            fake.create_limit_order.assert_not_called();fake.post_order.assert_not_called()
    def test_bootstrap_wallet_type_mismatch_closes_without_orders(self):
        from execution import LiveAdapter
        from polymarket import SecureClient
        from unittest.mock import MagicMock
        env={'POLY_ENABLE_LIVE':'I_ACCEPT_LIVE_ORDERS','POLYMARKET_PRIVATE_KEY':'fixture-key',
             'POLYMARKET_FUNDER':'fixture-wallet','POLYMARKET_SIGNATURE_TYPE':'3'}
        fake=MagicMock();fake.wallet_type='EOA'
        with patch('execution.os.environ',env),patch.object(SecureClient,'create',return_value=fake):
            with self.assertRaises(RuntimeError): LiveAdapter({'mode':'live','enable_live':True})
        fake.close.assert_called_once();fake.post_order.assert_not_called();fake.create_limit_order.assert_not_called()
    def test_adapter_maps_taker_and_maker_fills_without_account_access(self):
        from execution import LiveAdapter
        intent={'order_id':'oid','token':'up','condition':'condition','price':'.50','size':'5',
                'trade_ids':['immediate']}
        order=SimpleNamespace(id='oid',asset_id='up',condition_id='condition',side='BUY',
                    price=D('.5'),original_size=D('5'),status='LIVE',size_matched=D('3'),associate_trades=['later'])
        taker=SimpleNamespace(id='immediate',taker_order_id='oid',asset_id='up',side='BUY',size=D('2'),
                             price=D('.49'),status='CONFIRMED',transaction_hash='tx1')
        maker=SimpleNamespace(id='later',taker_order_id='other',maker_orders=[SimpleNamespace(
                    order_id='oid',asset_id='up',side='BUY',matched_amount=D('1'),price=D('.5'))],
                    status='MATCHED',transaction_hash=None)
        called=[]
        def reads(id):
            called.append(id)
            return SimpleNamespace(iter_items=lambda:iter([taker if id=='immediate' else maker]))
        adapter=object.__new__(LiveAdapter)
        adapter.client=SimpleNamespace(get_order=lambda **kw:order,list_account_trades=reads)
        result=adapter.reconcile(intent)
        self.assertEqual(set(called),{'immediate','later'})
        self.assertEqual(result['matched_qty'],'3')
        self.assertEqual([(f['leg'],f['qty']) for f in result['fills']],[('taker','2'),('maker','1')])
    def test_adapter_rejects_other_market_without_trade_reads(self):
        from execution import LiveAdapter
        adapter=object.__new__(LiveAdapter)
        adapter.client=SimpleNamespace(get_order=lambda **kw:SimpleNamespace(asset_id='down',side='BUY'))
        with self.assertRaises(ValueError): adapter.reconcile({'order_id':'oid','token':'up'})
    def test_missing_order_recovers_exact_confirmed_trade(self):
        from execution import LiveAdapter
        intent={'order_id':'oid','token':'up','condition':'condition','price':'.50','size':'5','committed_at':NOW}
        trade=SimpleNamespace(id='fill',taker_order_id='oid',asset_id='up',side='BUY',
                    size=D('5'),price=D('.49'),status='CONFIRMED',transaction_hash='tx')
        other=SimpleNamespace(id='other',taker_order_id='elsewhere',maker_orders=[])
        def unavailable(**kw): raise TimeoutError()
        adapter=object.__new__(LiveAdapter)
        adapter.client=SimpleNamespace(get_order=unavailable,
            list_open_orders=lambda **kw:SimpleNamespace(iter_items=lambda:iter([])),
            list_account_trades=lambda **kw:SimpleNamespace(iter_items=lambda:iter([other,trade])))
        result=adapter.reconcile(intent)
        self.assertEqual(result['status'],'FILLED');self.assertEqual(result['matched_qty'],'5')
        self.assertEqual(len(result['fills']),1)
    def test_empty_or_partial_history_never_proves_order_absent(self):
        from execution import LiveAdapter
        intent={'order_id':'oid','token':'up','condition':'condition','price':'.50','size':'5','committed_at':NOW}
        adapter=object.__new__(LiveAdapter)
        for qty in (None,D('2')):
            trades=[] if qty is None else [SimpleNamespace(id='fill',taker_order_id='oid',asset_id='up',
                side='BUY',size=qty,price=D('.5'),status='CONFIRMED',transaction_hash='tx')]
            adapter.client=SimpleNamespace(list_account_trades=lambda **kw:SimpleNamespace(iter_items=lambda:iter(trades)))
            self.assertEqual(adapter.reconcile_trades(intent)['status'],'UNKNOWN')
    def test_history_cannot_accept_wrong_asset_or_excess_quantity(self):
        from execution import LiveAdapter
        intent={'order_id':'oid','token':'up','condition':'condition','price':'.50','size':'5','committed_at':NOW}
        adapter=object.__new__(LiveAdapter)
        for token,qty in [('down','5'),('up','6')]:
            trade=SimpleNamespace(id='fill',taker_order_id='oid',asset_id=token,side='BUY',
                size=D(qty),price=D('.5'),status='CONFIRMED',transaction_hash='tx')
            adapter.client=SimpleNamespace(list_account_trades=lambda **kw:SimpleNamespace(iter_items=lambda:iter([trade])))
            with self.assertRaises(ValueError): adapter.reconcile_trades(intent)
    def test_adapter_rejected_response_never_creates_position(self):
        from execution import LiveAdapter
        from polymarket.models.clob.order_response import RejectedOrder
        adapter=object.__new__(LiveAdapter)
        adapter.client=SimpleNamespace(post_order=lambda x:RejectedOrder(code='not_enough_balance',message='fixture'))
        result=adapter.submit(object())
        self.assertFalse(result['ok']);self.assertIsNone(result['order_id']);self.assertEqual(result['trade_ids'],[])
    def test_live_default_rejected_before_secret_access(self):
        from execution import LiveAdapter
        with self.assertRaises(RuntimeError): LiveAdapter(bot.configuration(mode='prepare'))
        with patch.dict('os.environ',{'POLY_ENABLE_LIVE':''}):
            with self.assertRaises(ValueError): bot.configuration(mode='live')
    def test_pinned_sdk_interfaces_without_authentication_or_signing(self):
        import importlib.metadata
        from polymarket import SecureClient, SignedOrder, ApiKeyCreds
        self.assertEqual(importlib.metadata.version('polymarket-client'),'0.12.0')
        for name in ['create_limit_order','post_order','get_order','list_account_trades']:
            self.assertTrue(callable(getattr(SecureClient,name)))
        self.assertIn('expiration',inspect.signature(SecureClient.create_limit_order).parameters)
        self.assertIn('apiKey',ApiKeyCreds.model_fields['key'].validation_alias)
        self.assertIn('timestamp',{f.name for f in fields(SignedOrder)})
    def test_order_hash_matches_eip712_without_signing(self):
        from polymarket import SignedOrder
        from polymarket.environments import PRODUCTION
        from polymarket._internal.actions.orders.types import UnsignedOrder
        from polymarket._internal.actions.orders.context import resolve_order_exchange_address
        from polymarket._internal.protocol import is_v2_position_id
        from polymarket._internal.actions.orders.typed_data import _build_standard_typed_data
        from eth_account.messages import encode_typed_data, _hash_eip191_message
        from dataclasses import asdict
        from execution import order_identity
        config=PRODUCTION._config
        addr='0x'+'11'*20;zero='0x'+'00'*32
        signed=SignedOrder(builder=zero,expiration=OPEN+360,maker=addr,maker_amount=2500000,
          metadata=zero,order_type='GTD',salt=123,side='BUY',signature='0x',signature_type=0,
          signer=addr,taker_amount=5000000,timestamp=NOW*1000,token_id='123')
        fake=SimpleNamespace(_ctx=SimpleNamespace(environment_config=config))
        values=asdict(signed);values.pop('signature');values.pop('post_only')
        u=UnsignedOrder(**values,chain_id=config.chain_id,
           exchange_address=resolve_order_exchange_address(config,asset_id='123',neg_risk=False))
        version='3' if is_v2_position_id('123') else '2'
        expected='0x'+_hash_eip191_message(encode_typed_data(full_message=_build_standard_typed_data(u,protocol_version=version))).hex()
        self.assertEqual(order_identity(signed,fake,False),expected)


if __name__=='__main__': unittest.main()
