# store/news_live.py
"""
Live news scanner: for the big releases (NFP, CPI, FOMC, PPI, GDP, ECB …) one board with

  upcoming   countdown + the odds before the number: how often this release beats its forecast, and for each
             market the chance it rises (beat odds × what each market did after a beat / a miss) and its usual
             1-hour move in pips
  awaiting   the time has passed, the number is not in the feed yet: the calendar is re-read every 20 seconds
  released   the actual is out: beat / miss / in line decides the call for each market — BUY or SELL, how often
             that call was right after the same kind of surprise, and the average 1-hour move in pips

Everything comes from store/news_history.py (past releases and how each market reacted). With little history a
market falls back to the textbook reaction (good data → stronger currency) and a typical move, marked "estimate".
This is statistics about past releases, not a promise.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_tz

from django.core.cache import cache

from . import news_history as nh

logger = logging.getLogger(__name__)

PIP = {'XAUUSD': 0.1, 'USDJPY': 0.01, 'BTCUSD': 1.0}             # everything else 0.0001
TYPICAL_H1 = {'NFP': 0.45, 'CPI': 0.35, 'CORE_CPI': 0.35, 'FOMC': 0.5, 'ECB': 0.35, 'BOE': 0.3, 'BOJ': 0.4,
              'PPI': 0.2, 'CORE_PCE': 0.2, 'GDP': 0.2, 'RETAIL': 0.2}  # gold, % in the first hour
SCALE = {'XAUUSD': 1.0, 'EURUSD': 0.45, 'GBPUSD': 0.5, 'USDJPY': 0.5, 'BTCUSD': 1.6, 'EURGBP': 0.3}


def pip_size(symbol):
    return PIP.get(symbol, 0.0001)


def _price(symbol, fallback=None):
    try:
        from .mt5_service import cached_quote
        q = cached_quote(symbol)
        if q is not None:
            return float((q.bid + q.ask) / 2)
    except Exception:
        pass
    return fallback


def to_pips(symbol, pct, price):
    if pct is None or not price:
        return None
    return round(abs(price * pct / 100) / pip_size(symbol))


def beat_odds(key, last=36):
    """Share of past releases that beat / missed their forecast."""
    from .models import EconEvent
    evs = list(EconEvent.objects.filter(key=key, actual__isnull=False, forecast__isnull=False).order_by('-when')[:last])
    signs = [nh.surprise_sign(e) for e in evs]
    n = len([s for s in signs if s])
    if n < 4:
        return 0.5, 0.5, len(evs)
    beat = sum(1 for s in signs if s > 0) / n
    return beat, 1 - beat, len(evs)


def side_moves(key, symbol, sign, last=36):
    """1-hour moves of `symbol` after past releases with the same surprise (+1 beat, -1 miss) → (n, up share,
    average size of up moves %, average size of down moves %)."""
    from .models import NewsReaction
    rows = [r for r in NewsReaction.objects.filter(event__key=key, symbol=symbol, h1__isnull=False)
            .select_related('event').order_by('-event__when')[:last] if nh.surprise_sign(r.event) == sign]
    if not rows:
        return 0, None, None, None
    ups = [r.h1 for r in rows if r.h1 > 0]
    downs = [-r.h1 for r in rows if r.h1 < 0]
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    return len(rows), len(ups) / len(rows), avg(ups), avg(downs)


def _fallback_dir(currency, symbol, sign):
    return -sign if (currency, symbol) in nh.INVERSE else sign


def market_call(key, currency, symbol, state, sign, p_beat):
    """One market's line on the board."""
    st = None
    try:
        st = nh.symbol_stats(key, symbol)
    except Exception as e:
        logger.warning(f'news scanner stats {key} {symbol}: {e}')
    price = _price(symbol, st.get('price_now') if st else None)
    typical = (st.get('abs_h1') if st and st.get('abs_h1') else TYPICAL_H1.get(key, 0.2) * SCALE.get(symbol, 0.5))
    row = {'symbol': symbol, 'price': price, 'history': st['n'] if st else 0, 'estimate': not st or st['n'] < 6,
           'pips_typical': to_pips(symbol, typical, price)}

    if state == 'released' and sign:
        n, up_share, up_size, down_size = side_moves(key, symbol, sign)
        if n >= 3:
            direction = 1 if up_share >= 0.5 else -1                          # what this market did most often
            conf = up_share if direction > 0 else 1 - up_share
            pct = (up_size if direction > 0 else down_size) or typical        # usual size of a move that way
        else:
            direction, conf, pct = _fallback_dir(currency, symbol, sign), 0.6, typical
        row.update(call='BUY' if direction > 0 else 'SELL', prob=round(conf * 100), pips=to_pips(symbol, pct, price),
                   locked=True, note='Weak edge: history is close to 50/50' if conf < 0.58 else '')
    elif state == 'released':
        row.update(call='WAIT', prob=50, pips=to_pips(symbol, typical * 0.5, price), locked=True,
                   note='In line with the forecast: usually a small, choppy move')
    else:
        def p_up_side(s):
            n, up_share, _, _ = side_moves(key, symbol, s)
            return up_share if n >= 3 else (0.62 if _fallback_dir(currency, symbol, s) > 0 else 0.38)
        p_up = p_beat * p_up_side(1) + (1 - p_beat) * p_up_side(-1)
        row.update(call='BUY' if p_up >= 0.5 else 'SELL', prob=round(max(p_up, 1 - p_up) * 100), pips=row['pips_typical'],
                   locked=False, p_up=round(p_up * 100))
    return row


def _refresh_if_waiting(events, now):
    """A release time has passed but no actual yet: re-read the calendar (at most every 20 s)."""
    from .calendar_feed import CACHE_KEY
    waiting = any(not e.get('actual') and now - timedelta(minutes=30) <= e['date'] <= now for e in events)
    if waiting and not cache.get('news_live:refreshed'):
        cache.set('news_live:refreshed', 1, 20)
        cache.delete(CACHE_KEY)
        return True
    return False


def board(hours_back=6, days_ahead=7):
    """The whole scanner board (JSON-ready)."""
    from .calendar_feed import get_calendar
    now = datetime.now(dt_tz.utc)

    def tracked():
        out = []
        for e in get_calendar(days_ahead).get('events') or []:
            key = nh.classify(e.get('event'), e.get('currency'))
            d = e.get('date')
            if key in nh.BIG and isinstance(d, datetime) and now - timedelta(hours=hours_back) <= d <= now + timedelta(days=days_ahead):
                out.append(dict(e, key=key))
        return out

    events = tracked()
    if _refresh_if_waiting(events, now):
        events = tracked()
    seen, rows = set(), []
    for e in sorted(events, key=lambda e: e['date']):
        k = (e['key'], e['date'])
        if k in seen:
            continue
        seen.add(k)
        label, currency, _, good = nh.EVENTS[e['key']]
        actual, forecast, previous = nh._num(e.get('actual')), nh._num(e.get('forecast')), nh._num(e.get('previous'))
        state = 'released' if actual is not None else ('awaiting' if e['date'] <= now else 'upcoming')
        sign = 0
        if actual is not None and forecast is not None and abs(actual - forecast) > 1e-9:
            sign = 1 if (actual > forecast) == good else -1
        p_beat, p_miss, n_hist = beat_odds(e['key'])
        surprise_pct = (round((actual - forecast) / abs(forecast) * 100, 1)
                        if actual is not None and forecast not in (None, 0) else None)
        rows.append({
            'id': f"{e['key']}{e['date']:%Y%m%d%H%M}", 'key': e['key'], 'label': label, 'currency': currency,
            'when': e['date'].isoformat(), 'seconds': int((e['date'] - now).total_seconds()), 'state': state,
            'actual': e.get('actual'), 'forecast': e.get('forecast'), 'previous': e.get('previous'),
            'result': {1: 'BEAT', -1: 'MISS', 0: 'IN LINE'}[sign] if state == 'released' else None,
            'good_for': f'good for {currency}' if sign > 0 else (f'bad for {currency}' if sign < 0 else None), 'surprise_pct': surprise_pct,
            'p_beat': round(p_beat * 100), 'p_miss': round(p_miss * 100), 'history': n_hist,
            'markets': [market_call(e['key'], currency, s, state, sign, p_beat) for s in nh.SYMBOLS.get(currency, [])],
        })
    upcoming = [r for r in rows if r['state'] != 'released']
    focus = next((r for r in rows if r['state'] == 'awaiting'), None) or \
        next((r for r in reversed(rows) if r['state'] == 'released' and r['seconds'] > -3 * 3600), None) or \
        (upcoming[0] if upcoming else (rows[-1] if rows else None))
    return {'now': now.isoformat(), 'focus': focus, 'events': rows,
            'poll': 4 if any(r['state'] == 'awaiting' or 0 <= r['seconds'] <= 600 for r in rows) else 30}
