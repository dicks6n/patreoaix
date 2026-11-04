# store/practice.py
"""
Academy Practice simulator: a chart stops at a decision point, the student chooses BUY, SELL or SKIP and a
risk:reward, then the next candles replay and the trade is scored in R.

Two kinds of chart:
  real        a stored AcademyExample (a real candlestick pattern found in market history, store/academy_examples.py):
              the candles up to the pattern, then the real candles that followed
  simulated   a seeded generator plants a textbook setup (trend pullback, breakout, range rejection, double top /
              bottom, exhaustion reversal, or a messy chart where skipping is right). Like real markets, the textbook
              outcome happens only part of the time (about 65%), so a good decision can still lose.

The candles after the decision point never leave the server before the answer: the browser gets a signed token,
and the answer rebuilds the same chart from it (simulated charts are deterministic from their seed).
"""
import hashlib
import random
from datetime import datetime, timedelta, timezone as dt_tz

from django.core import signing

SALT = 'academy.practice.v1'
TOKEN_AGE = 6 * 3600
WIN_ODDS = 0.65                       # how often a planted setup plays out the textbook way
RRS = (1, 2, 3)
REAL_SHARE = 0.35                     # share of real charts when there are stored examples

MARKETS = {   # symbol: (start price, typical candle range on 1h, decimals)
    'XAUUSD': (2650.0, 4.2, 2), 'EURUSD': (1.0850, 0.0013, 5), 'GBPUSD': (1.2700, 0.0016, 5),
    'USDJPY': (151.20, 0.22, 3), 'BTCUSD': (68000.0, 420.0, 1), 'US30': (42000.0, 85.0, 1),
}
TF_SCALE = {'15m': (0.5, 900), '1h': (1.0, 3600), '4h': (2.0, 14400)}

SETUPS = {
    # key: (name, what the textbook says, why)
    'pullback': ('Trend pullback', 'trade with the trend',
                 'The trend made higher highs (or lower lows), price pulled back, and the last candle rejected the '
                 'pullback. Joining the trend after a pullback is the classic high-probability entry.'),
    'breakout': ('Range breakout', 'trade the breakout',
                 'Price was stuck in a range, then a strong candle closed outside it. A clean close beyond the range '
                 'often starts a move as stops and new orders pile in.'),
    'range': ('Range rejection', 'fade the edge of the range',
              'Price reached the edge of a well-tested range and the last candle rejected it. Inside a range, '
              'selling the top and buying the bottom works until the range breaks.'),
    'double': ('Double top / bottom', 'trade the neckline break',
               'Price failed twice at the same level and then broke the neckline between the two swings: a '
               'classic reversal pattern.'),
    'exhaustion': ('Exhaustion reversal', 'trade the reversal',
                   'After a long move with oversized candles, an engulfing candle formed against the trend. Late, '
                   'stretched moves often snap back.'),
    'chop': ('No clear setup', 'skip',
             'There is no trend, no clean level and no strong last candle. The best trade here is no trade: '
             'skipping messy charts protects your account.'),
}
SETUP_KEYS = list(SETUPS)


# ═══════════════════════════════════════════════════════════════
# SIMULATED CHARTS
# ═══════════════════════════════════════════════════════════════
class _Gen:
    def __init__(self, rng, price, vol):
        self.r, self.p, self.v, self.cs = rng, price, vol, []

    def bar(self, drift=0.0, noise=0.55, body=None, wick_up=None, wick_dn=None):
        o = self.p
        c = o + (body * self.v if body is not None else drift * self.v + self.r.gauss(0, noise) * self.v)
        wu = (wick_up if wick_up is not None else abs(self.r.gauss(0, 0.28))) * self.v
        wd = (wick_dn if wick_dn is not None else abs(self.r.gauss(0, 0.28))) * self.v
        self.cs.append([o, max(o, c) + wu, min(o, c) - wd, c])
        self.p = c

    def walk(self, n, drift=0.0, noise=0.55):
        for _ in range(n):
            self.bar(drift, noise)

    def range_(self, n, mid, half):
        for _ in range(n):
            pull = (mid - self.p) / (half or 1) * 0.55       # mean-reverting inside the range
            self.bar(pull, 0.5)
            c = self.cs[-1]
            top, bot = mid + half, mid - half
            if c[1] > top + 0.15 * self.v:
                c[1] = top + abs(self.r.gauss(0, 0.1)) * self.v
            if c[2] < bot - 0.15 * self.v:
                c[2] = bot - abs(self.r.gauss(0, 0.1)) * self.v
            c[3] = min(max(c[3], bot), top)
            c[1], c[2] = max(c[1], c[0], c[3]), min(c[2], c[0], c[3])
            self.p = c[3]

    def rejection(self, d):
        """A hammer (d=+1) or shooting star (d=-1) closing back in direction d."""
        self.bar(body=0.35 * d, wick_up=0.12 if d > 0 else 1.6, wick_dn=1.6 if d > 0 else 0.12)

    def engulf(self, d):
        self.bar(body=-0.45 * d, wick_up=0.1, wick_dn=0.1)
        prev = self.cs[-1]
        self.p = prev[3]
        size = abs(prev[3] - prev[0]) / self.v + 0.9
        self.bar(body=size * d, wick_up=0.15, wick_dn=0.15)


def simulate(seed):
    """→ dict(symbol, timeframe, setup, bias, candles before, candles after) — always the same for the same seed."""
    rng = random.Random(seed)
    symbol = rng.choice(list(MARKETS))
    tf = rng.choice(list(TF_SCALE))
    price, vol, dec = MARKETS[symbol]
    vol *= TF_SCALE[tf][0]
    price *= 1 + rng.uniform(-0.04, 0.04)
    setup = rng.choices(SETUP_KEYS, weights=[24, 18, 16, 14, 12, 16])[0]
    d = rng.choice((1, -1))
    g = _Gen(rng, price, vol)

    if setup == 'pullback':
        g.walk(rng.randint(8, 14), 0, 0.5)
        g.walk(rng.randint(26, 34), 0.38 * d, 0.6)
        g.walk(rng.randint(5, 8), -0.5 * d, 0.45)
        g.rejection(d)
    elif setup == 'breakout':
        g.walk(rng.randint(10, 16), 0.3 * d, 0.55)
        mid, half = g.p, vol * rng.uniform(1.8, 2.6)
        g.range_(rng.randint(22, 30), mid, half)
        g.bar(body=d * ((mid + d * half - g.p) * d / vol + rng.uniform(0.9, 1.4)), wick_up=0.15, wick_dn=0.15)
    elif setup == 'range':
        g.walk(rng.randint(8, 12), 0, 0.5)
        mid, half = g.p, vol * rng.uniform(2.4, 3.2)
        g.range_(rng.randint(28, 36), mid, half)
        edge = mid - d * half                                    # buy the bottom (d=+1) / sell the top (d=-1)
        for _ in range(rng.randint(3, 5)):
            g.bar(drift=(edge - g.p) / vol * 0.45, noise=0.25)
        g.p = edge + d * 0.15 * vol
        g.rejection(d)
    elif setup == 'double':
        g.walk(rng.randint(6, 10), 0, 0.5)
        g.walk(rng.randint(14, 18), -0.45 * d, 0.5)              # into the double bottom (d=+1) / top (d=-1)
        level = g.p
        g.walk(rng.randint(6, 8), 0.5 * d, 0.45)
        neck = max(c[1] for c in g.cs[-6:]) if d > 0 else min(c[2] for c in g.cs[-6:])
        n = rng.randint(6, 8)
        for _ in range(n):
            g.bar(drift=(level - g.p) / vol / 2, noise=0.25)
        g.cs[-1][2 if d > 0 else 1] = level - d * 0.1 * vol
        k = rng.randint(5, 7)
        for _ in range(k):
            g.bar(drift=(neck - g.p) / vol / 2.2, noise=0.3)
        g.bar(body=d * ((neck - g.p) * d / vol + rng.uniform(0.6, 1.0)), wick_up=0.15, wick_dn=0.15)
    elif setup == 'exhaustion':
        g.walk(rng.randint(8, 12), 0, 0.5)
        g.walk(rng.randint(24, 30), -0.4 * d, 0.55)
        for _ in range(3):
            g.bar(body=-d * rng.uniform(1.5, 2.3), wick_up=0.2, wick_dn=0.2)
        g.engulf(d)
    else:                                                        # chop: nothing to trade
        g.walk(rng.randint(44, 52), 0, 0.9)
        g.bar(body=rng.uniform(-0.3, 0.3))
        d = 0

    before = len(g.cs)
    if d == 0:
        drift = rng.choice((1, -1)) * rng.uniform(0, 0.12)
    elif rng.random() < WIN_ODDS:
        drift = d * rng.uniform(0.45, 0.7)
    else:
        drift = -d * rng.uniform(0.4, 0.65)
    g.walk(26, drift, 0.6)

    for c in g.cs:                                               # keep every candle valid after the hand edits
        c[1], c[2] = max(c[1], c[0], c[3]), min(c[2], c[0], c[3])
    t0 = int(datetime(2025, 1, 6, tzinfo=dt_tz.utc).timestamp()) + rng.randint(0, 400) * 86400
    step = TF_SCALE[tf][1]
    cs = [[t0 + i * step] + [round(x, dec) for x in c] for i, c in enumerate(g.cs)]
    return {'source': 'sim', 'symbol': symbol, 'timeframe': tf, 'setup': setup, 'bias': d, 'decimals': dec,
            'candles': cs[:before], 'after': cs[before:]}


# ═══════════════════════════════════════════════════════════════
# REAL CHARTS (stored academy examples)
# ═══════════════════════════════════════════════════════════════
def _pattern_bias(key):
    from .academy_examples import PATTERNS
    return PATTERNS.get(key, (1, 0, 0, None))[1]


def real(key):
    from .models import AcademyExample
    ex = AcademyExample.objects.filter(key=key).first()
    if not ex or len(ex.candles) < ex.mark_to + 3:
        return None
    cs = [list(c) for c in ex.candles]
    dec = max((len(str(c[4]).split('.')[1]) if '.' in str(c[4]) else 0) for c in cs[:5])
    return {'source': 'real', 'symbol': ex.symbol, 'timeframe': ex.timeframe, 'setup': key, 'bias': _pattern_bias(key),
            'decimals': min(dec, 5), 'candles': cs[:ex.mark_to + 1], 'after': cs[ex.mark_to + 1:],
            'mark': [ex.mark_from, ex.mark_to], 'at': ex.at}


def _real_keys():
    from django.core.cache import cache
    keys = cache.get('practice:real_keys')
    if keys is None:
        from .models import AcademyExample
        keys = [k for k, n in ((e.key, len(e.candles) - e.mark_to - 1) for e in AcademyExample.objects.only('key', 'candles', 'mark_to'))
                if n >= 3 and _pattern_bias(k) != 0]
        cache.set('practice:real_keys', keys, 3600)
    return keys


# ═══════════════════════════════════════════════════════════════
# ROUND: next chart → answer
# ═══════════════════════════════════════════════════════════════
def _atr(cs, n=14):
    rs = [c[2] - c[3] for c in cs[-n:]]
    return sum(rs) / len(rs) if rs else 0


def stops(cs):
    """Stop distance for a BUY and for a SELL: beyond the swing of the last 6 candles, kept between 0.8 and 3 ATR."""
    atr = _atr(cs) or abs(cs[-1][4]) * 0.001
    entry = cs[-1][4]
    low, high = min(c[3] for c in cs[-6:]), max(c[2] for c in cs[-6:])
    clamp = lambda x: min(max(x, 0.8 * atr), 3 * atr)
    return entry, clamp(entry - low + 0.15 * atr), clamp(high - entry + 0.15 * atr)


def build(token_data):
    if token_data.get('src') == 'real':
        return real(token_data['key'])
    return simulate(token_data['seed'])


def new_round(prefer=None, exclude=()):
    """A fresh chart for the browser: candles up to the decision point, stop sizes, and a signed token."""
    rng = random.SystemRandom()
    keys = [k for k in _real_keys() if k not in exclude] if prefer != 'sim' else []
    if keys and (prefer == 'real' or rng.random() < REAL_SHARE):
        data = {'src': 'real', 'key': rng.choice(keys), 'n': rng.getrandbits(40)}
    else:
        data = {'src': 'sim', 'seed': rng.getrandbits(48)}
    sc = build(data)
    if sc is None:
        data = {'src': 'sim', 'seed': rng.getrandbits(48)}
        sc = build(data)
    entry, buy_risk, sell_risk = stops(sc['candles'])
    out = {'token': signing.dumps(data, salt=SALT, compress=True), 'source': sc['source'], 'symbol': sc['symbol'],
           'timeframe': sc['timeframe'].upper(), 'candles': sc['candles'], 'decimals': sc['decimals'],
           'entry': entry, 'risk': {'BUY': buy_risk, 'SELL': sell_risk}, 'after_n': len(sc['after'])}
    if sc['source'] == 'real':
        out['mark'] = sc['mark']
        out['when'] = sc['at'].strftime('%d %b %Y') if sc.get('at') else ''
    return out


def play(after, side, entry, risk, rr):
    """Walk the next candles: which is hit first, stop or target? (both in one candle → the stop, to be fair)."""
    d = 1 if side == 'BUY' else -1
    sl, tp = entry - d * risk, entry + d * risk * rr
    for i, c in enumerate(after):
        hit_sl = c[3] <= sl if d > 0 else c[2] >= sl
        hit_tp = c[2] >= tp if d > 0 else c[3] <= tp
        if hit_sl:
            return {'outcome': 'SL', 'r': -1.0, 'bar': i, 'sl': sl, 'tp': tp}
        if hit_tp:
            return {'outcome': 'TP', 'r': float(rr), 'bar': i, 'sl': sl, 'tp': tp}
    last = after[-1][4] if after else entry
    return {'outcome': 'END', 'r': round((last - entry) * d / risk, 2), 'bar': len(after) - 1, 'sl': sl, 'tp': tp}


def answer(token, choice, rr):
    """→ the full result for the browser, or raises ValueError."""
    try:
        data = signing.loads(token, salt=SALT, max_age=TOKEN_AGE)
    except signing.BadSignature:
        raise ValueError('This chart has expired. Load a new one.')
    choice = str(choice or '').upper()
    if choice not in ('BUY', 'SELL', 'SKIP'):
        raise ValueError('Choose BUY, SELL or Skip.')
    try:
        rr = int(rr)
    except (TypeError, ValueError):
        rr = 2
    rr = rr if rr in RRS else 2
    sc = build(data)
    if sc is None:
        raise ValueError('This chart is no longer available. Load a new one.')
    entry, buy_risk, sell_risk = stops(sc['candles'])
    sims = {'BUY': play(sc['after'], 'BUY', entry, buy_risk, rr), 'SELL': play(sc['after'], 'SELL', entry, sell_risk, rr)}
    mine = sims.get(choice)
    r = mine['r'] if mine else 0.0
    bias = sc['bias']
    textbook = 'BUY' if bias > 0 else ('SELL' if bias < 0 else 'SKIP')
    best = max(sims, key=lambda s: sims[s]['r'])
    good_skip = choice == 'SKIP' and all(s['r'] <= 0.3 for s in sims.values())
    if sc['source'] == 'real':
        name = sc['setup'].replace('_', ' ').capitalize()
        why = (f"{name} on real {sc['symbol']} {sc['timeframe'].upper()} candles. The textbook bias of this pattern is "
               f"{'up' if bias > 0 else 'down'}: {textbook}.")
    else:
        name, rule, why = SETUPS[sc['setup']]
    if choice == textbook:
        verdict = 'Textbook decision.' + (' The market went the other way this time: that happens in about a third '
                                          'of setups, and good decisions still win over many trades.' if r < 0 else '')
    elif choice == 'SKIP':
        verdict = ('Good skip: neither side worked out.' if good_skip else
                   f'You skipped a {textbook} setup. Skipping is never wrong when you are unsure, but this one was tradable.')
    else:
        verdict = (f'The textbook call here was {textbook}.' + (' You still won: lucky, not repeatable.' if r > 0 else ''))
    return {'choice': choice, 'rr': rr, 'r': r, 'outcome': mine['outcome'] if mine else 'SKIP', 'bar': mine['bar'] if mine else None,
            'sl': mine['sl'] if mine else None, 'tp': mine['tp'] if mine else None, 'entry': entry,
            'after': sc['after'], 'sims': {k: {'r': v['r'], 'outcome': v['outcome']} for k, v in sims.items()},
            'textbook': textbook, 'setup': sc['setup'], 'setup_name': name, 'why': why, 'verdict': verdict,
            'correct': choice == textbook or good_skip, 'best': best if sims[best]['r'] > 0 else 'SKIP',
            'source': sc['source'], 'symbol': sc['symbol'], 'timeframe': sc['timeframe'].upper(),
            'digest': hashlib.sha256(token.encode()).hexdigest()[:40]}


# ═══════════════════════════════════════════════════════════════
# SCOREBOARD
# ═══════════════════════════════════════════════════════════════
def summarize(rows):
    """rows: [(choice, r, correct, setup)] oldest first → stats for the scoreboard."""
    trades = [x for x in rows if x[0] != 'SKIP']
    wins = [x for x in trades if x[1] > 0]
    streak = best = 0
    for x in rows:
        streak = streak + 1 if x[2] else 0
        best = max(best, streak)
    by = {}
    for x in rows:
        b = by.setdefault(x[3], [0, 0, 0.0])
        b[0] += 1
        b[1] += 1 if x[2] else 0
        b[2] += x[1]
    setups = sorted(({'key': k, 'name': SETUPS[k][0] if k in SETUPS else k.replace('_', ' ').capitalize(), 'n': v[0],
                      'right': round(v[1] / v[0] * 100), 'r': round(v[2], 1)} for k, v in by.items()), key=lambda s: -s['n'])
    total_r = sum(x[1] for x in trades)
    return {'played': len(rows), 'trades': len(trades), 'wins': len(wins),
            'win_rate': round(len(wins) / len(trades) * 100) if trades else 0,
            'total_r': round(total_r, 2), 'avg_r': round(total_r / len(trades), 2) if trades else 0,
            'right': round(sum(1 for x in rows if x[2]) / len(rows) * 100) if rows else 0,
            'streak': streak, 'best_streak': best, 'setups': setups[:8],
            'ready': len(trades) >= 30 and total_r > 0}


def user_stats(user):
    from .models import PracticeTrade
    rows = list(PracticeTrade.objects.filter(user=user).order_by('created_at')
                .values_list('choice', 'result_r', 'correct', 'setup')[:5000])
    return summarize(rows)
