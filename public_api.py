"""Unauthenticated GET only. Endpoint failures never cause fallback to another slot."""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import json
import time
import urllib.request
import urllib.parse
import urllib.error

D = Decimal


class PublicDataError(ValueError):
    """Safe diagnostic codes produced only from unauthenticated data validation."""


def seconds(iso):
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())


def array(value):
    return json.loads(value) if isinstance(value, str) else value


def validate_market(m, opening):
    if m['slug'] != f'btc-updown-5m-{opening}':
        raise PublicDataError('wrong slug')
    starts = [m.get('eventStartTime')] + [e.get('startTime') for e in m.get('events', [])]
    starts = [seconds(s) for s in starts if s]
    if not starts or any(s != opening for s in starts) or seconds(m['endDate']) != opening + 300:
        raise PublicDataError('market window mismatch')
    outcomes = array(m['outcomes'])
    if sorted(outcomes) != ['Down', 'Up']:
        raise PublicDataError('unexpected outcomes')
    # Refuse unidentified resolution contracts, keep complete description in evidence.
    if 'chain.link' not in m.get('resolutionSource', '') or not m.get('description'):
        raise PublicDataError('missing Chainlink resolution contract')
    version = m.get('version', 'v1')
    assets = array(m['positionIds'] if version == 'v2' else m['clobTokenIds'])
    if len(assets) != 2 or len(set(assets)) != 2:
        raise PublicDataError('invalid outcome assets')
    return dict(zip(outcomes, assets))


def quote(book, token, condition, now, max_age=30):
    if str(book['asset_id']) != str(token) or book['market'] != condition:
        raise PublicDataError('wrong book')
    age = now - D(str(book['timestamp'])) / 1000
    if age < -2 or age > max_age:
        raise PublicDataError('stale or future book')
    tick, minimum = D(str(book['tick_size'])), D(str(book['min_order_size']))
    if not tick.is_finite() or tick not in map(D, ['0.1','0.01','0.005','0.0025','0.001','0.0001']):
        raise PublicDataError('unsupported tick')
    if not minimum.is_finite() or minimum <= 0:
        raise PublicDataError('invalid minimum')
    asks = [(D(a['price']), D(a['size'])) for a in book['asks']]
    if any(not p.is_finite() or not s.is_finite() or not 0 < p < 1 or s < 0 for p,s in asks):
        raise PublicDataError('invalid ask')
    asks = sorted((p,s) for p,s in asks if s > 0)
    if not asks:
        raise PublicDataError('no ask')
    price = (asks[0][0] / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if price >= 1 or price < tick:
        raise PublicDataError('price outside permitted range')
    size = minimum.quantize(D('.01'), rounding=ROUND_CEILING)
    return price, size, tick, asks


def official_winner(clob):
    winners = [t['outcome'] for t in clob.get('tokens', []) if t.get('winner') is True]
    return winners[0] if clob.get('closed') is True and len(winners) == 1 and winners[0] in ('Up','Down') else None


class PublicAPI:
    def __init__(self):
        self.server = None
        self.synced = 0
        self.uncertainty = 0
        self.retry_at = {}

    def get(self, base, path, **params):
        if time.monotonic() < self.retry_at.get(base, 0):
            raise RuntimeError('endpoint_backoff')
        url = base + path + ('?' + urllib.parse.urlencode(params) if params else '')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 CalendarPublicObserver/1.0',
                                                 'Cache-Control':'no-cache'})
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return json.loads(response.read(), parse_float=D)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                try: delay = max(15, float(e.headers.get('Retry-After', '30')))
                except ValueError: delay = 30
                self.retry_at[base] = time.monotonic() + delay
            raise

    def sync(self):
        before = time.monotonic()
        server = int(self.get('https://clob.polymarket.com', '/time'))
        after = time.monotonic()
        self.server, self.synced = server, (before + after)/2
        self.uncertainty = 1 + (after-before)/2
        if self.uncertainty > 5:
            raise RuntimeError('clock_rtt_too_large')
        return self.now()

    def now(self):
        if self.server is None or time.monotonic()-self.synced > 120:
            raise RuntimeError('clock_untrusted')
        return self.server + time.monotonic()-self.synced

    def market(self, opening):
        slug = f'btc-updown-5m-{opening}'
        rows = self.get('https://gamma-api.polymarket.com', '/markets', slug=slug)
        rows = [m for m in rows if m.get('slug') == slug]
        if not rows:
            return None
        if len(rows) != 1:
            raise PublicDataError('ambiguous exact market')
        validate_market(rows[0], opening)
        return rows[0]

    def clob(self, condition):
        return self.get('https://clob.polymarket.com', '/markets/' + condition)

    def book(self, token):
        return self.get('https://clob.polymarket.com', '/book', token_id=token)

    def binance(self, opening):
        server=self.get('https://data-api.binance.vision','/api/v3/time')['serverTime']
        if int(server) < (opening+302)*1000:
            return []
        return self.get('https://data-api.binance.vision','/api/v3/klines',symbol='BTCUSDT',
                        interval='5m',startTime=opening*1000,endTime=(opening+300)*1000-1,limit=1)
