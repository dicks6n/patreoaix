# store/academy_examples.py
"""
Real candlestick examples for the academy.

The finder scans real market history (the same feeds as the Market Overview: gold, forex, crypto, indices on 1h / 4h / 1d)
for every candlestick pattern, scores each match (clean shape, a real trend before it, the move that followed) and keeps
the best recent one per pattern in AcademyExample. Lessons then show that real chart with its market, timeframe and
date, and what price did next. Until a pattern has been found, the lesson shows a labelled diagram instead.

Runs once a day from the background scanner (safe_daily) and on demand:  python manage.py academy_examples
"""
import logging
from datetime import datetime, timezone as dt_tz

from django.core.cache import cache

logger = logging.getLogger(__name__)

SYMBOLS = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD', 'ETHUSD', 'US30', 'NAS100', 'XAGUSD', 'AUDUSD']
TIMEFRAMES = ['1h', '4h', '1d']
BEFORE, AFTER = 10, 5            # candles shown before the pattern and after it


# ═══════════════════════════════════════════════════════════════
# CANDLE MATHS
# ═══════════════════════════════════════════════════════════════
def _b(x):
    return abs(x['c'] - x['o'])


def _r(x):
    return max(x['h'] - x['l'], 1e-12)


def _up(x):
    return x['c'] > x['o']


def _dn(x):
    return x['c'] < x['o']


def _uw(x):
    return x['h'] - max(x['o'], x['c'])


def _lw(x):
    return min(x['o'], x['c']) - x['l']


def _mid(x):
    return (x['o'] + x['c']) / 2


class Ctx:
    """Averages and the trend before index i."""
    def __init__(self, cs):
        self.cs = cs

    def avg_body(self, i, n=14):
        seg = self.cs[max(0, i - n):i]
        return sum(_b(x) for x in seg) / len(seg) if seg else 0

    def avg_range(self, i, n=14):
        seg = self.cs[max(0, i - n):i]
        return sum(_r(x) for x in seg) / len(seg) if seg else 0

    def trend(self, i, n=6):
        """Move of the closes over the n candles before i, in average ranges (+ up, - down)."""
        if i - n < 0:
            return 0
        ar = self.avg_range(i) or 1e-12
        return (self.cs[i - 1]['c'] - self.cs[i - n]['c']) / ar


# Each detector: (cs, ctx, i) → quality 0..1 for a pattern whose LAST candle is i.  (length, direction, detector)
def _small(x, ctx, i, k=0.1):
    return _b(x) <= _r(x) * k


def d_hammer(cs, ctx, i):
    x = cs[i]
    if _lw(x) >= 2.2 * _b(x) and _uw(x) <= 0.35 * _b(x) + _r(x) * 0.05 and _b(x) >= _r(x) * 0.12 and _r(x) >= ctx.avg_range(i) * 0.9:
        return min(1, _lw(x) / _r(x))
    return 0


def d_inverted(cs, ctx, i):
    x = cs[i]
    if _uw(x) >= 2.2 * _b(x) and _lw(x) <= 0.35 * _b(x) + _r(x) * 0.05 and _b(x) >= _r(x) * 0.12 and _r(x) >= ctx.avg_range(i) * 0.9:
        return min(1, _uw(x) / _r(x))
    return 0


def d_doji(cs, ctx, i):
    x = cs[i]
    if _small(x, ctx, i, 0.08) and 0.25 < _uw(x) / _r(x) < 0.75 and _r(x) >= ctx.avg_range(i) * 0.7:
        return 1 - _b(x) / _r(x)
    return 0


def d_dragonfly(cs, ctx, i):
    x = cs[i]
    if _small(x, ctx, i, 0.08) and _uw(x) <= _r(x) * 0.1 and _r(x) >= ctx.avg_range(i) * 0.9:
        return 1 - _uw(x) / _r(x)
    return 0


def d_gravestone(cs, ctx, i):
    x = cs[i]
    if _small(x, ctx, i, 0.08) and _lw(x) <= _r(x) * 0.1 and _r(x) >= ctx.avg_range(i) * 0.9:
        return 1 - _lw(x) / _r(x)
    return 0


def d_long_legged(cs, ctx, i):
    x = cs[i]
    if _small(x, ctx, i, 0.08) and min(_uw(x), _lw(x)) >= _r(x) * 0.35 and _r(x) >= ctx.avg_range(i) * 1.4:
        return min(1, _r(x) / ctx.avg_range(i) / 2)
    return 0


def d_spinning(cs, ctx, i):
    x = cs[i]
    if 0.12 * _r(x) <= _b(x) <= 0.3 * _r(x) and min(_uw(x), _lw(x)) >= _b(x) * 1.0 and _r(x) >= ctx.avg_range(i) * 0.8:
        return 0.7
    return 0


def d_marubozu(sign):
    def f(cs, ctx, i):
        x = cs[i]
        if (_up(x) if sign > 0 else _dn(x)) and _b(x) >= _r(x) * 0.9 and _b(x) >= ctx.avg_body(i) * 2:
            return min(1, _b(x) / ctx.avg_body(i) / 3)
        return 0
    return f


def d_engulfing(sign):
    def f(cs, ctx, i):
        a, x = cs[i - 1], cs[i]
        if sign > 0 and _dn(a) and _up(x) and x['c'] >= a['o'] and x['o'] <= a['c'] and _b(x) >= _b(a) * 1.3 \
                and _b(a) >= ctx.avg_body(i) * 0.4:
            return min(1, _b(x) / _b(a) / 2.5)
        if sign < 0 and _up(a) and _dn(x) and x['c'] <= a['o'] and x['o'] >= a['c'] and _b(x) >= _b(a) * 1.3 \
                and _b(a) >= ctx.avg_body(i) * 0.4:
            return min(1, _b(x) / _b(a) / 2.5)
        return 0
    return f


def d_harami(sign):
    def f(cs, ctx, i):
        a, x = cs[i - 1], cs[i]
        big = _b(a) >= ctx.avg_body(i) * 1.5
        inside = max(x['o'], x['c']) <= max(a['o'], a['c']) and min(x['o'], x['c']) >= min(a['o'], a['c']) and _b(x) <= _b(a) * 0.45
        if big and inside and ((sign > 0 and _dn(a) and _up(x)) or (sign < 0 and _up(a) and _dn(x))):
            return 0.6 + 0.4 * (1 - _b(x) / _b(a))
        return 0
    return f


def d_piercing(cs, ctx, i):
    a, x = cs[i - 1], cs[i]
    if _dn(a) and _up(x) and _b(a) >= ctx.avg_body(i) * 1.2 and x['o'] <= a['c'] and _mid(a) < x['c'] < a['o']:
        return 0.6 + 0.4 * (x['c'] - _mid(a)) / max(a['o'] - _mid(a), 1e-12)
    return 0


def d_dark_cloud(cs, ctx, i):
    a, x = cs[i - 1], cs[i]
    if _up(a) and _dn(x) and _b(a) >= ctx.avg_body(i) * 1.2 and x['o'] >= a['c'] and a['o'] < x['c'] < _mid(a):
        return 0.6 + 0.4 * (_mid(a) - x['c']) / max(_mid(a) - a['o'], 1e-12)
    return 0


def d_tweezer(sign):
    def f(cs, ctx, i):
        a, x = cs[i - 1], cs[i]
        tol = ctx.avg_range(i) * 0.06
        if sign > 0 and _dn(a) and _up(x) and abs(a['l'] - x['l']) <= tol and _lw(a) + _lw(x) >= _r(a) * 0.4:
            return 0.8
        if sign < 0 and _up(a) and _dn(x) and abs(a['h'] - x['h']) <= tol and _uw(a) + _uw(x) >= _r(a) * 0.4:
            return 0.8
        return 0
    return f


def d_star(sign):
    def f(cs, ctx, i):
        a, m, x = cs[i - 2], cs[i - 1], cs[i]
        ab = ctx.avg_body(i)
        if _b(a) < ab * 1.2 or _b(m) > _b(a) * 0.35 or _b(x) < ab * 0.9:
            return 0
        if sign > 0 and _dn(a) and _up(x) and max(m['o'], m['c']) <= a['c'] + ab * 0.2 and x['c'] > _mid(a):
            return 0.6 + 0.4 * min(1, (x['c'] - _mid(a)) / max(_b(a), 1e-12))
        if sign < 0 and _up(a) and _dn(x) and min(m['o'], m['c']) >= a['c'] - ab * 0.2 and x['c'] < _mid(a):
            return 0.6 + 0.4 * min(1, (_mid(a) - x['c']) / max(_b(a), 1e-12))
        return 0
    return f


def d_three(sign):
    def f(cs, ctx, i):
        t = cs[i - 2:i + 1]
        ab, ar = ctx.avg_body(i - 2), ctx.avg_range(i - 2)
        ok = all((_up(x) if sign > 0 else _dn(x)) and _b(x) >= ab * 1.1 and _b(x) >= ar * 0.55 and _b(x) >= _r(x) * 0.6 for x in t)
        steps = all((t[k]['c'] > t[k - 1]['c']) if sign > 0 else (t[k]['c'] < t[k - 1]['c']) for k in (1, 2))
        opens = all(min(t[k - 1]['o'], t[k - 1]['c']) <= t[k]['o'] <= max(t[k - 1]['o'], t[k - 1]['c']) for k in (1, 2))
        return 0.85 if ok and steps and opens else 0
    return f


def d_three_inside(sign):
    h = d_harami(sign)

    def f(cs, ctx, i):
        if not h(cs, ctx, i - 1):
            return 0
        a, x = cs[i - 2], cs[i]
        if sign > 0 and _up(x) and x['c'] > a['o']:
            return 0.85
        if sign < 0 and _dn(x) and x['c'] < a['o']:
            return 0.85
        return 0
    return f


def d_three_methods(sign):
    def f(cs, ctx, i):
        a, mids, x = cs[i - 4], cs[i - 3:i], cs[i]
        ab = ctx.avg_body(i - 4)
        if _b(a) < ab * 1.4 or _b(x) < ab * 1.2:
            return 0
        lo, hi = a['l'], a['h']
        if not all(lo <= y['l'] and y['h'] <= hi and _b(y) <= _b(a) * 0.6 for y in mids):
            return 0
        if sign > 0 and _up(a) and _up(x) and x['c'] > a['c'] and sum(_dn(y) for y in mids) >= 2:
            return 0.9
        if sign < 0 and _dn(a) and _dn(x) and x['c'] < a['c'] and sum(_up(y) for y in mids) >= 2:
            return 0.9
        return 0
    return f


def d_kicker(cs, ctx, i):
    a, x = cs[i - 1], cs[i]
    ab = ctx.avg_body(i)
    if _dn(a) and _up(x) and x['o'] >= a['o'] and _b(x) >= ab * 1.5 and _b(a) >= ab * 0.8 and x['l'] >= a['c']:
        return 0.9
    return 0


# key: (length, expected move after: +1 up / -1 down / 0 either, trend needed before: +1 up / -1 down / 0 any, detector)
PATTERNS = {
    'hammer':               (1, +1, -1, d_hammer),
    'inverted_hammer':      (1, +1, -1, d_inverted),
    'hanging_man':          (1, -1, +1, d_hammer),
    'shooting_star':        (1, -1, +1, d_inverted),
    'doji':                 (1, 0, 0, d_doji),
    'dragonfly_doji':       (1, +1, -1, d_dragonfly),
    'gravestone_doji':      (1, -1, +1, d_gravestone),
    'long_legged_doji':     (1, 0, 0, d_long_legged),
    'spinning_top':         (1, 0, 0, d_spinning),
    'bull_marubozu':        (1, +1, 0, d_marubozu(+1)),
    'bear_marubozu':        (1, -1, 0, d_marubozu(-1)),
    'bullish_engulfing':    (2, +1, -1, d_engulfing(+1)),
    'bearish_engulfing':    (2, -1, +1, d_engulfing(-1)),
    'bullish_harami':       (2, +1, -1, d_harami(+1)),
    'bearish_harami':       (2, -1, +1, d_harami(-1)),
    'piercing_line':        (2, +1, -1, d_piercing),
    'dark_cloud':           (2, -1, +1, d_dark_cloud),
    'tweezer_bottom':       (2, +1, -1, d_tweezer(+1)),
    'tweezer_top':          (2, -1, +1, d_tweezer(-1)),
    'morning_star':         (3, +1, -1, d_star(+1)),
    'evening_star':         (3, -1, +1, d_star(-1)),
    'three_white_soldiers': (3, +1, 0, d_three(+1)),
    'three_black_crows':    (3, -1, 0, d_three(-1)),
    'three_inside_up':      (3, +1, -1, d_three_inside(+1)),
    'three_inside_down':    (3, -1, +1, d_three_inside(-1)),
    'rising_three':         (5, +1, +1, d_three_methods(+1)),
    'falling_three':        (5, -1, -1, d_three_methods(-1)),
    'bullish_kicker':       (2, +1, -1, d_kicker),
}


def scan(cs, key):
    """All matches of one pattern in a candle list → [(score, first, last, move_after_pct)] best first."""
    n_len, move, need, det = PATTERNS[key]
    ctx = Ctx(cs)
    out = []
    for i in range(max(BEFORE + n_len, 15), len(cs) - AFTER):
        first = i - n_len + 1
        tr = ctx.trend(first)
        if need > 0 and tr < 1.5 or need < 0 and tr > -1.5:
            continue
        try:
            q = det(cs, ctx, i)
        except (ZeroDivisionError, IndexError):
            q = 0
        if not q:
            continue
        after = cs[i + AFTER]['c']
        pct = (after - cs[i]['c']) / cs[i]['c'] * 100
        ar = ctx.avg_range(i) or 1e-12
        conf = (after - cs[i]['c']) / ar                 # move after, in average ranges
        follow = conf * move if move else min(abs(conf), 2) / 2
        trend_q = min(abs(tr), 5) / 5 if need else 0.5
        score = q * 2 + trend_q + max(-1, min(follow, 3)) * 0.8 + i / len(cs) * 0.5    # newer is a little better
        out.append((score, first, i, pct))
    out.sort(reverse=True)
    return out


# ═══════════════════════════════════════════════════════════════
# FINDER
# ═══════════════════════════════════════════════════════════════
def _norm(raw):
    out = []
    for x in raw or []:
        try:
            t = x.get('time') or x.get('t')
            out.append({'t': int(t if not isinstance(t, str) else datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()),
                        'o': float(x.get('open', x.get('o'))), 'h': float(x.get('high', x.get('h'))),
                        'l': float(x.get('low', x.get('l'))), 'c': float(x.get('close', x.get('c')))})
        except (TypeError, ValueError, AttributeError):
            continue
    return [x for x in out if x['h'] >= x['l'] > 0]


def find_all(sources=None, keys=None, save=True):
    """sources: {(symbol, tf): candles}; default: the live feeds. Returns {key: example dict}."""
    if sources is None:
        from . import chart_scanner
        sources = {}
        for sym in SYMBOLS:
            for tf in TIMEFRAMES:
                try:
                    sources[(sym, tf)] = chart_scanner.candles_for(sym, tf).get('candles') or []
                except Exception as e:
                    logger.info(f'academy examples {sym} {tf}: {e}')
    data = {k: _norm(v) for k, v in sources.items()}
    data = {k: v for k, v in data.items() if len(v) >= 60}
    best = {}
    for key in keys or PATTERNS:
        for (sym, tf), cs in data.items():
            hits = scan(cs, key)
            if hits and (key not in best or hits[0][0] > best[key]['score']):
                score, first, last, pct = hits[0]
                a, b = max(0, first - BEFORE), min(len(cs), last + AFTER + 1)
                best[key] = {'key': key, 'symbol': sym, 'timeframe': tf, 'score': round(score, 3),
                             'candles': [[x['t'], x['o'], x['h'], x['l'], x['c']] for x in cs[a:b]],
                             'mark_from': first - a, 'mark_to': last - a, 'move_after': round(pct, 3),
                             'at': datetime.fromtimestamp(cs[last]['t'], dt_tz.utc)}
    if save and best:
        from .models import AcademyExample
        for key, ex in best.items():
            AcademyExample.objects.update_or_create(key=key, defaults={k: v for k, v in ex.items() if k != 'key'})
    return best


def safe_daily():
    """Once a day from the background scanner."""
    if not cache.add('academy:examples:run', 1, 20 * 3600):
        return 0
    try:
        return len(find_all())
    except Exception as e:
        logger.warning(f'Academy examples: {e}')
        return 0


# ═══════════════════════════════════════════════════════════════
# PICTURES
# ═══════════════════════════════════════════════════════════════
def real_picture(key):
    """→ {'svg', 'symbol', 'timeframe', 'at', 'move_after', 'real': True} or None."""
    from .models import AcademyExample
    from .academy_charts import svg
    ex = AcademyExample.objects.filter(key=key).first()
    if not ex or not ex.candles:
        return None
    cs = [(x[1], x[2], x[3], x[4]) for x in ex.candles]
    pic = {'candles': cs, 'mark': (ex.mark_from, ex.mark_to), 'title': f'{key} on {ex.symbol}'}
    return {'svg': svg(pic, width=420, height=220), 'symbol': ex.symbol, 'timeframe': ex.timeframe.upper(), 'at': ex.at,
            'move_after': ex.move_after, 'after_n': AFTER, 'real': True}
