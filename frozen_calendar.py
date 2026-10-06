import calendar, json, hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parent
TZ=ZoneInfo("America/Toronto")
GROUPS={"classement_m5_toutes":500,"m5_min100_taux60":67}
FIELDS={"jour_semaine","jour_mois","mois","trimestre","rang_mois","distance_fin_mois","dernier_jour_semaine","weekend"}

def calendar_values(d):
    last = calendar.monthrange(d.year, d.month)[1]
    return {'jour_semaine': d.weekday(), 'jour_mois': d.day, 'mois': d.month,
            'trimestre': (d.month - 1) // 3 + 1, 'rang_mois': (d.day - 1) // 7 + 1,
            'distance_fin_mois': last - d.day, 'dernier_jour_semaine': int(d.day + 7 > last),
            'weekend': int(d.weekday() >= 5)}

def matches(rule, opening):
    d = datetime.fromtimestamp(opening / 1000, TZ)
    values = calendar_values(d)
    return d.hour * 12 + d.minute // 5 == rule['slot'] and all(
        values[k] == v for k, v in rule['calendar'].items())

def next_activation(rule, after):
    date = datetime.fromtimestamp(after / 1000, TZ).date()
    for offset in range(370):
        day = date + timedelta(days=offset)
        if not all(calendar_values(day)[k] == v for k, v in rule['calendar'].items()):
            continue
        naive = datetime(day.year, day.month, day.day, rule['slot'] // 12, rule['slot'] % 12 * 5)
        candidates = set()
        for fold in (0, 1):
            local = naive.replace(tzinfo=TZ, fold=fold)
            opening = int(local.timestamp() * 1000)
            # Round-trip rejects nonexistent spring-forward local times.
            if datetime.fromtimestamp(opening / 1000, TZ).replace(tzinfo=None) == naive and opening > after:
                candidates.add(opening)
        if candidates:
            return min(candidates)
    return None

def load_rules(path=ROOT / 'selected_rules.json'):
    raw = path.read_bytes()
    package = json.loads(raw)
    rules = package['rules']
    assert package['selection'] == GROUPS and package['deduplicated_rules'] == len(rules) == 567
    assert package['symbol'] == 'BTCUSDT' and package['timeframe'] == '5m'
    assert len({r['id'] for r in rules}) == 567
    assert {g: sum(g in r['cohorts'] for r in rules) for g in GROUPS} == GROUPS
    for r in rules:
        assert set(r['calendar']) <= FIELDS and r['prediction'] in ('V', 'R')
        assert r['timezone'] == 'America/Toronto' and r['symbol'] == 'BTCUSDT'
        assert r['market'] == 'binance_spot' and r['timeframe'] == '5m'
        assert r['opening_time'] == f"{r['slot']//12:02d}:{r['slot']%12*5:02d}"
        canonical = {k: r[k] for k in ('calendar', 'slot', 'prediction')}
        assert r['id'] == 'btc_m5_' + hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]
        assert set(r['cohorts']) <= set(GROUPS)
        h = r['historical']
        assert h['n'] > 0 and 0 <= h['wins'] <= h['n'] and abs(h['winrate'] - h['wins']/h['n']) < 1e-12
        if 'm5_min100_taux60' in r['cohorts']:
            assert h['n'] >= 100 and h['wins'] * 5 >= h['n'] * 3
    return rules, hashlib.sha256(raw).hexdigest()
