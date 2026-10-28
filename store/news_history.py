# store/news_history.py
"""
News history: every big release (NFP, CPI, FOMC, PPI …) with actual vs forecast, and how gold, the majors and
bitcoin moved after it (15 min, 1 h, 4 h, 1 day). From that, a playbook for the next release: how big the move
usually is, which way it went after a beat or a miss, and when to execute.

    python manage.py news_history --backfill 5     # 5 years of releases + price reactions (run once, takes a while)
    python manage.py news_history --update         # the last weeks (the background scanner does this hourly)
    python manage.py news_history --show FOMC      # the playbook in the console

Data: releases from TradingView's economic calendar, prices from Deriv's 15-minute candles.
Everything here is history and statistics, not a promise about the next release.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_tz

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# key: (label, currency, title pattern, higher number = good for the currency?)
EVENTS = {
    'FOMC':     ('FOMC rate decision', 'USD', r'fed(eral)?\s*(funds)?\s*(interest)?\s*rate\s*decision|fomc.*(rate|decision)|federal funds rate', True),
    'NFP':      ('Non-farm payrolls (NFP)', 'USD', r'non[\s-]?farm\s*(payrolls|employment)', True),
    'CPI':      ('CPI inflation m/m', 'USD', r'^(inflation\s*rate|cpi)\s*(mom|m/m)$', True),
    'CORE_CPI': ('Core CPI m/m', 'USD', r'^core\s*(inflation\s*rate|cpi)\s*(mom|m/m)$', True),
    'PPI':      ('PPI m/m', 'USD', r'^ppi\s*(mom|m/m)$', True),
    'CORE_PCE': ('Core PCE m/m', 'USD', r'core\s*pce\s*price\s*index\s*(mom|m/m)', True),
    'GDP':      ('GDP q/q', 'USD', r'^gdp\s*(growth\s*rate|annualized)?.*(qoq|q/q)', True),
    'RETAIL':   ('Retail sales m/m', 'USD', r'^retail\s*sales\s*(mom|m/m)$', True),
    'ISM_MFG':  ('ISM manufacturing PMI', 'USD', r'ism\s*manufacturing\s*pmi', True),
    'ISM_SRV':  ('ISM services PMI', 'USD', r'ism\s*(services|non-manufacturing)\s*pmi', True),
    'CLAIMS':   ('Initial jobless claims', 'USD', r'initial\s*jobless\s*claims', False),
    'UNEMP':    ('Unemployment rate', 'USD', r'^unemployment\s*rate$', False),
    'ADP':      ('ADP employment', 'USD', r'adp\s*(employment|non[\s-]?farm)', True),
    'ECB':      ('ECB rate decision', 'EUR', r'(ecb|main\s*refinancing|deposit\s*facility).*rate|^interest\s*rate\s*decision$', True),
    'BOE':      ('BoE rate decision (MPC)', 'GBP', r'(boe|bank\s*of\s*england)?.*interest\s*rate\s*decision|^bank\s*rate', True),
    'BOJ':      ('BoJ rate decision', 'JPY', r'(boj|bank\s*of\s*japan)?.*interest\s*rate\s*decision', True),
    'UK_CPI':   ('UK CPI y/y', 'GBP', r'^(inflation\s*rate|cpi)\s*(yoy|y/y)$', True),
}
# the central-bank patterns overlap ("interest rate decision"): the currency decides
COUNTRIES = {'USD': 'US', 'EUR': 'EU', 'GBP': 'GB', 'JPY': 'JP'}
SYMBOLS = {'USD': ['XAUUSD', 'EURUSD', 'USDJPY', 'BTCUSD'], 'EUR': ['EURUSD', 'XAUUSD'],
           'GBP': ['GBPUSD', 'EURGBP'], 'JPY': ['USDJPY']}
# a currency-positive surprise pushes these the other way (they quote the currency second, or are priced in it)
INVERSE = {('USD', 'XAUUSD'), ('USD', 'EURUSD'), ('USD', 'GBPUSD'), ('USD', 'BTCUSD'), ('EUR', 'EURGBP')}
GRAN = 900                       # 15-minute candles
BIG = ('FOMC', 'NFP', 'CPI', 'CORE_CPI', 'PPI', 'CORE_PCE', 'ECB', 'BOE', 'BOJ', 'GDP', 'RETAIL')


def classify(title, currency):
    t = str(title or '').strip().lower()
    for key, (_, cur, pattern, _) in EVENTS.items():
        if cur == currency and re.search(pattern, t):
            return key
    return None


def _num(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r'-?\d+(\.\d+)?', str(v).replace(',', ''))
    if not m:
        return None
    return float(m.group(0))                 # TradingView sends plain numbers; ForexFactory "250K" keeps its unit


# ═══════════════════════════════════════════════════════════════
# STORE RELEASES
# ═══════════════════════════════════════════════════════════════
def save_events(rows):
    from .models import EconEvent
    n = 0
    for e in rows:
        key = classify(e.get('event'), e.get('currency'))
        if not key or not e.get('date'):
            continue
        EconEvent.objects.update_or_create(source_id=str(e['id'])[:60], defaults={
            'key': key, 'title': str(e.get('event'))[:160], 'currency': e.get('currency') or '',
            'when': e['date'], 'actual': _num(e.get('actual')), 'forecast': _num(e.get('forecast')),
            'previous': _num(e.get('previous')), 'unit': str(e.get('unit') or '')[:12]})
        n += 1
    return n


def fetch_range(start, end):
    from .calendar_feed import fetch_tradingview
    return fetch_tradingview(start=start, end=end, countries=','.join(COUNTRIES.values()))


def backfill(years=5, log=print):
    """Month by month, oldest first."""
    end = datetime.now(dt_tz.utc) + timedelta(days=14)
    start = end - timedelta(days=int(years * 365))
    total, cur = 0, start
    while cur < end:
        nxt = min(cur + timedelta(days=31), end)
        try:
            total += save_events(fetch_range(cur, nxt))
        except Exception as e:
            log(f'  {cur:%Y-%m}: {e}')
        cur = nxt
        time.sleep(0.6)
    return total


# ═══════════════════════════════════════════════════════════════
# PRICE REACTIONS (Deriv 15-minute candles)
# ═══════════════════════════════════════════════════════════════
class _Deriv:
    def __init__(self):
        from . import price_feed
        self.pf = price_feed
        self.ws = price_feed.deriv_connect(timeout=10)
        self.req = 100

    def candles(self, symbol, start, end):
        dsym = self.pf.DERIV_CANDLE_MAP.get(symbol)
        if not dsym:
            return []
        self.req += 1
        rid = self.req
        self.ws.send(json.dumps({'ticks_history': dsym, 'style': 'candles', 'granularity': GRAN,
                                 'start': int(start), 'end': int(end), 'count': 5000, 'req_id': rid}))
        deadline = time.time() + 15
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get('req_id') != rid:
                continue
            if msg.get('error'):
                return []
            return [{'t': int(c['epoch']), 'h': float(c['high']), 'l': float(c['low']), 'c': float(c['close'])}
                    for c in msg.get('candles') or []]
        return []

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _measure(bars, t0):
    """Reaction numbers from 15-minute candles around release time t0 (epoch)."""
    before = [b for b in bars if b['t'] < t0]
    after = [b for b in bars if b['t'] >= t0]
    if not before or not after or after[0]['t'] - t0 > 3600:
        return None                                   # market closed around the release
    p0 = before[-1]['c']
    pct = lambda p: round((p - p0) / p0 * 100, 4)
    at = lambda sec: next((b['c'] for b in reversed(after) if b['t'] <= t0 + sec - GRAN), None)
    first_hour = [b for b in after if b['t'] < t0 + 3600]
    out = {'price0': p0, 'm15': pct(at(900)) if at(900) else None, 'h1': pct(at(3600)) if at(3600) else None,
           'h4': pct(at(14400)) if at(14400) else None,
           'd1': pct(at(86400)) if after[-1]['t'] >= t0 + 86400 - 2 * GRAN else None,
           'up1h': max(0.0, pct(max(b['h'] for b in first_hour))) if first_hour else None,
           'down1h': min(0.0, pct(min(b['l'] for b in first_hour))) if first_hour else None,
           'path': [pct(b['c']) for b in after if b['t'] < t0 + 4 * 3600][:16]}
    return out


def measure_pending(limit=400, log=print):
    """Reactions for releases that have none yet (or whose 1-day move wasn't known yet)."""
    from django.db.models import Q
    from .models import EconEvent, NewsReaction
    now = timezone.now()
    todo = []
    for ev in EconEvent.objects.filter(when__lte=now - timedelta(hours=4)).order_by('-when')[:3000]:
        have = {r.symbol: r for r in ev.reactions.all()}
        for sym in SYMBOLS.get(ev.currency, []):
            r = have.get(sym)
            if r is None or (r.d1 is None and ev.when <= now - timedelta(hours=26) and ev.when >= now - timedelta(days=10)):
                todo.append((ev, sym))
        if len(todo) >= limit:
            break
    if not todo:
        return 0
    done = 0
    d = _Deriv()
    try:
        for ev, sym in todo:
            t0 = int(ev.when.timestamp())
            try:
                bars = d.candles(sym, t0 - 2 * 3600, t0 + 86400 + GRAN)
            except Exception as e:
                log(f'  {sym} {ev}: {e}')
                d.close()
                d = _Deriv()
                continue
            m = _measure(bars, t0)
            if m:
                NewsReaction.objects.update_or_create(event=ev, symbol=sym, defaults=m)
                done += 1
    finally:
        d.close()
    return done


def update(log=print):
    """Recent and coming releases + their reactions. Cheap enough to run every hour."""
    now = datetime.now(dt_tz.utc)
    n = save_events(fetch_range(now - timedelta(days=21), now + timedelta(days=10)))
    m = measure_pending(limit=120, log=log)
    return n, m


def safe_tick():
    """Called by the background scanner; does real work at most once an hour."""
    if not cache.add('news:history:tick', 1, 3600):
        return None
    try:
        return update(log=lambda *_: None)
    except Exception as e:
        logger.warning(f'news history update failed: {e}')
        return None


# ═══════════════════════════════════════════════════════════════
# STATISTICS → PLAYBOOK
# ═══════════════════════════════════════════════════════════════
def surprise_sign(ev):
    """+1 = good for the currency (better than forecast), -1 = bad, 0 = in line / unknown."""
    if ev.actual is None or ev.forecast is None:
        return 0
    diff = ev.actual - ev.forecast
    if abs(diff) < 1e-9:
        return 0
    good = EVENTS.get(ev.key, (None, None, None, True))[3]
    return 1 if (diff > 0) == good else -1


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def symbol_stats(key, symbol, last=24):
    from .models import NewsReaction
    rows = list(NewsReaction.objects.filter(event__key=key, symbol=symbol, h1__isnull=False)
                .select_related('event').order_by('-event__when')[:last])
    if not rows:
        return None
    inv = (rows[0].event.currency, symbol) in INVERSE

    def side(rs, s):
        sub = [r for r in rs if surprise_sign(r.event) == s]
        if not sub:
            return None
        moves = [r.h1 for r in sub]
        expected = -s if inv else s             # which way the symbol "should" move after that surprise
        hits = sum(1 for m in moves if m * expected > 0)
        return {'n': len(sub), 'avg_h1': _avg(moves), 'same_way': hits / len(sub), 'expected_dir': expected}

    cont = [r for r in rows if r.m15 and r.h1]
    cont_share = sum(1 for r in cont if r.m15 * r.h1 > 0 and abs(r.h1) >= abs(r.m15) * 0.8) / len(cont) if cont else None
    return {
        'symbol': symbol, 'n': len(rows),
        'abs_m15': _avg([abs(r.m15) for r in rows if r.m15 is not None]),
        'abs_h1': _avg([abs(r.h1) for r in rows]), 'abs_h4': _avg([abs(r.h4) for r in rows if r.h4 is not None]),
        'range_h1': _avg([(r.up1h or 0) - (r.down1h or 0) for r in rows]),
        'up_share': sum(1 for r in rows if r.h1 > 0) / len(rows),
        'beat': side(rows, 1), 'miss': side(rows, -1), 'inline': side(rows, 0),
        'continuation': cont_share, 'price_now': rows[0].price0,
        'last': [{'when': r.event.when, 'actual': r.event.actual, 'forecast': r.event.forecast,
                  'surprise': surprise_sign(r.event), 'm15': r.m15, 'h1': r.h1, 'h4': r.h4, 'path': r.path} for r in rows[:8]],
    }


def timing_advice(st, when_local):
    """When to execute, from how the first 15 minutes usually relate to the first hour."""
    t15 = when_local + timedelta(minutes=15)
    t60 = when_local + timedelta(minutes=60)
    c = st.get('continuation') if st else None
    if c is None:
        return f"Stay out for the release; reassess after the {t15:%H:%M} candle closes."
    if c >= 0.6:
        return (f"The first 15-minute move held for the hour {c * 100:.0f}% of the time: let the {t15:%H:%M} "
                f"15-min candle close, then trade in its direction with the stop beyond that candle.")
    if c <= 0.4:
        return (f"The first spike reversed {100 - c * 100:.0f}% of the time: don't chase it. Wait for {t60:%H:%M} "
                f"and look for the move back toward the pre-release price.")
    return (f"Mixed history ({c * 100:.0f}% continuation): skip the first 15 minutes and trade the 1-hour structure "
            f"after {t60:%H:%M}.")


def upcoming(hours=48, keys=BIG):
    """Tracked releases in the next `hours` from the live calendar."""
    from .calendar_feed import get_calendar
    now = datetime.now(dt_tz.utc)
    out = []
    for e in get_calendar(3).get('events') or []:
        key = classify(e.get('event'), e.get('currency'))
        d = e.get('date')
        if key in keys and isinstance(d, datetime) and now - timedelta(minutes=5) <= d <= now + timedelta(hours=hours):
            out.append(dict(e, key=key))
    # one entry per key and time (CPI has several lines at the same minute)
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: e['date']):
        k = (e['key'], e['date'])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def playbook(key, when=None, forecast=None, previous=None, tz=None):
    """Everything the page / Telegram show for one coming release."""
    label, currency = EVENTS[key][0], EVENTS[key][1]
    when_local = (when or datetime.now(dt_tz.utc)).astimezone(tz) if tz else (when or datetime.now(dt_tz.utc))
    stats = [s for s in (symbol_stats(key, sym) for sym in SYMBOLS[currency]) if s]
    lead = stats[0] if stats else None
    expect = None
    f, p = _num(forecast), _num(previous)
    if f is not None and p is not None and f != p:
        good = EVENTS[key][3]
        expect = ('stronger' if (f > p) == good else 'weaker') + f' than last time ({p:g} → {f:g} expected)'
    return {'key': key, 'label': label, 'currency': currency, 'when': when, 'when_local': when_local,
            'forecast': forecast, 'previous': previous, 'expectation': expect, 'stats': stats,
            'timing': timing_advice(lead, when_local), 'press_conference': key in ('FOMC', 'ECB', 'BOE', 'BOJ'),
            'second_wave': when_local + timedelta(minutes=30) if key in ('FOMC', 'ECB') else None}


def fmt_pct(x):
    return '—' if x is None else f'{x:+.2f}%'


def scenario_lines(st, currency):
    """Plain-language 'if beat / if miss' lines for one market."""
    lines = []
    for name, side in (('better than forecast', st.get('beat')), ('worse than forecast', st.get('miss'))):
        if side and side['n'] >= 3:
            lines.append(f"{name}: {st['symbol']} moved {fmt_pct(side['avg_h1'])} on average in 1 h "
                         f"({side['same_way'] * 100:.0f}% of {side['n']} times in the usual direction)")
    return lines


def recent(limit=12, keys=BIG):
    from .models import EconEvent
    evs = list(EconEvent.objects.filter(key__in=keys, when__lte=timezone.now()).prefetch_related('reactions')
               .order_by('-when')[:limit * 3])
    seen, out = set(), []
    for ev in evs:
        k = (ev.key, ev.when)
        if k in seen:
            continue
        seen.add(k)
        out.append({'event': ev, 'label': EVENTS[ev.key][0], 'surprise': surprise_sign(ev),
                    'reactions': sorted(ev.reactions.all(), key=lambda r: SYMBOLS.get(ev.currency, []).index(r.symbol)
                                        if r.symbol in SYMBOLS.get(ev.currency, []) else 9)})
        if len(out) >= limit:
            break
    return out
