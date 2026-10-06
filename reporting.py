from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import csv
import html
import io
import json
import os
import time

from engine import intents, meta, position, phase, dumps
from frozen_calendar import TZ, next_activation, GROUPS


def atomic(path, text):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(text,encoding='utf-8')
    os.replace(temp,path)


def stamp(t, local=False):
    return datetime.fromtimestamp(t,TZ if local else timezone.utc).isoformat(timespec='seconds')


def report(db, rules, folder, config, alive):
    folder=Path(folder)
    now=time.time()
    health={r[0]:json.loads(r[1]) for r in db.execute('SELECT key,value FROM meta')}
    rows=intents(db)
    linkmap={r['id']:[] for r in rules}
    for link in db.execute('SELECT intent_id,rule_id FROM links'):
        linkmap[link['rule_id']].append(link['intent_id'])
    by_id={i['id']:i for i in rows}
    for i in rows:
        i['phase']=phase(i,now)
        i['position']=position(db,i)
        i['opening_utc']=stamp(i['opening'])
        i['opening_toronto']=stamp(i['opening'],True)
        i['rule_count']=db.execute('SELECT COUNT(*) FROM links WHERE intent_id=?',(i['id'],)).fetchone()[0]
    rule_rows=[]
    for r in rules:
        rr=[by_id[x] for x in linkmap[r['id']]]
        n=sum(x['position']['result'] is not None for x in rr)
        wins=sum(x['position']['result']=='WIN' for x in rr)
        next_ms=next_activation(r,int(now*1000))
        rule_rows.append({'id':r['id'],'condition':r['condition_description'],'prediction':r['prediction'],
          'groups':r['cohorts'],'historical':r['historical'], 'intent_links':len(rr),
          'resolved_executed_positions':n,'wins':wins,'losses':n-wins,'winrate':wins/n if n else None,
          'next_activation_toronto':stamp(next_ms/1000,True) if next_ms is not None else None})
    cohorts={}
    for g in GROUPS:
        ids={r['id'] for r in rules if g in r['cohorts']}
        linked={i for r in ids for i in linkmap[r]}
        event_rows=[by_id[x] for x in linked]
        settled=[x for x in event_rows if x['position']['result']]
        cohorts[g]={'rules':len(ids),'activations':sum(len(linkmap[r]) for r in ids),
                    'unique_intents':len(linked),'unique_candles':len({x['opening'] for x in event_rows}),
                    'executed_resolved_intents':len(settled),'wins':sum(x['position']['result']=='WIN' for x in settled)}
    summary={'generated_utc':stamp(now),'mode':config['mode'],'process_alive':alive,'halted':(folder/'HALT').exists(),
       'health':health,'frozen_rules':len(rules),'unique_candles':len({i['opening'] for i in rows}),
       'intents':len(rows),'activations':sum(len(x) for x in linkmap.values()),'states':dict(Counter(i['state'] for i in rows)),
       'contradictory_candles':len({i['opening'] for i in rows if i['conflict']}),
       'confirmed_or_simulated_positions':sum(float(i['position']['quantity'])>0 for i in rows),
       'cohorts':cohorts,'execution_disabled':config['mode']!='live',
       'limits':{k:v for k,v in config.items() if k.startswith('max_')},
       'note':'Hypothèses historiques non validées. Aucun fill réel en prepare/paper. Attributions par règle non additives.'}
    atomic(folder/'status.json',dumps(summary))
    atomic(folder/'report.json',dumps({**summary,'intent_details':rows,'rules':rule_rows}))
    for name,data in [('intents.csv',rows),('rules.csv',rule_rows)]:
        buf=io.StringIO(); fields=sorted({k for row in data for k in row})
        writer=csv.DictWriter(buf,fields); writer.writeheader()
        writer.writerows({k:dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in data)
        atomic(folder/name,buf.getvalue())
    def e(v): return html.escape(str(v if v is not None else '—'))
    def table(headers, records):
        return '<table><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr>'+''.join(
            '<tr>'+''.join('<td>'+e(v)+'</td>' for v in record)+'</tr>' for record in records)+'</table>'
    body='<h1>BTC M5 · Polymarket · '+e(config['mode'].upper())+'</h1><p>'+e(summary['generated_utc'])+'</p>'
    body+='<p><b>Soumission réelle '+('DÉSACTIVÉE' if summary['execution_disabled'] else 'ACTIVÉE PAR CONFIGURATION LOCALE')+'</b> · Processus '+e(alive)+' · HALT '+e(summary['halted'])+'</p>'
    body+='<p>567 hypothèses figées, sans nouvelle sélection. Historique Binance distinct du règlement Polymarket. '
    body+='Un ordre préparé ne constitue pas une position. WIN/LOSS exige une quantité exécutée confirmée (ou une simulation étiquetée en paper) et une résolution officielle.</p>'
    body+='<pre>'+e(dumps(summary))+'</pre><h2>Intentions et positions</h2>'
    body+=table(['Ouverture Toronto','Direction','Règles','Contradiction','Phase marché','État ordre','Prix initial','Quantité demandée','Matched','Quantité confirmée/simulée','Résultat exécuté','Résultat Binance séparé','Erreur'],
      [(i['opening_toronto'],i['direction'],i['rule_count'],i['conflict'],i['phase'],i['state']+'/'+str(i.get('exchange_status','')),
        i.get('price'),i.get('size'),i.get('matched_qty'),i['position']['quantity'],i['position']['result'],
        i.get('binance',{}).get('hypothesis_result'),i.get('last_error')) for i in rows])
    body+='<h2>Provenance par règle</h2><p>Les positions partagées sont attribuées à chaque règle liée ; ne pas additionner leurs quantités. N=0 : taux indisponible. L’historique et le taux Polymarket mesurent des cibles différentes.</p>'
    body+=table(['Règle','Condition','V/R','Groupes','N historique','Taux historique Binance','Liens','N exécuté résolu','Wins','Losses','Taux prospectif Polymarket','Prochain créneau Toronto'],
      [(r['id'],r['condition'],r['prediction'],','.join(r['groups']),r['historical']['n'],r['historical']['winrate'],r['intent_links'],
        r['resolved_executed_positions'],r['wins'],r['losses'],r['winrate'],r['next_activation_toronto']) for r in rule_rows])
    atomic(folder/'report.html','<!doctype html><html lang="fr"><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Polymarket BTC M5</title><style>body{font:14px system-ui;margin:24px;background:#fafafa}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:7px;border:1px solid #ddd;text-align:left}th{background:#dfe9f5}tr:nth-child(even){background:#f0f4f8}pre{white-space:pre-wrap}</style>'+body+'</html>')
    return summary
