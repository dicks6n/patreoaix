# store/strategy_lab.py
"""
Strategy Lab: build a trading strategy from real rules, test it on real candles, optimise it, and let the bot
trade it.

A strategy (JSON, saved in TradingStrategy.blocks_json):
    {"name", "symbols": ["XAUUSD"], "tf": "1h",
     "buy":  {"logic": "all"|"any", "rules": [RULE…]}, "sell": {…}, "mirror": true,      # mirror = sells are the buy rules flipped
     "filters": {"session": "any"|"london"|"ny"|"overlap", "trend": "none"|"with"},  # with = only with the EMA-200 trend
     "exits": {"sl": {"type": "atr"|"pct", "v": 1.5}, "tp": {"type": "r"|"atr"|"pct"|"none", "v": 2},
               "trail": {"on": false, "atr": 2}, "breakeven": {"on": true, "r": 1}, "max_bars": 48, "opposite": true},
     "risk_pct": 1}
RULE:
    {"a": {"ind": "ema", "p": [50]}, "op": "crosses_above", "b": {"ind": "ema", "p": [200]}}
    {"a": {"ind": "rsi", "p": [14]}, "op": "lt", "b": {"ind": "value", "v": 30}}
    {"a": {"ind": "macd_hist"}, "op": "rising", "n": 3}
    {"pattern": "bullish_engulfing"}

Backtest: the signal is read on a closed candle and the trade opens at the next candle's open; spread is paid;
when one candle touches both the stop and the target, the stop counts (pessimistic).
"""
import math
import random
import re
from datetime import datetime, timezone as dt_tz

from . import chart_scanner as cs

# ═══════════════════════════════════════════════════════════════
# CATALOGUE (also sent to the page)
# ═══════════════════════════════════════════════════════════════
INDICATORS = {
    'close': ('Price (close)', []), 'open': ('Open', []), 'high': ('High', []), 'low': ('Low', []),
    'ema': ('EMA', [('period', 50)]), 'sma': ('SMA', [('period', 50)]),
    'rsi': ('RSI', [('period', 14)]),
    'macd': ('MACD line', []), 'macd_signal': ('MACD signal', []), 'macd_hist': ('MACD histogram', []),
    'bb_upper': ('Bollinger upper', [('period', 20)]), 'bb_mid': ('Bollinger middle', [('period', 20)]), 'bb_lower': ('Bollinger lower', [('period', 20)]),
    'stoch_k': ('Stochastic %K', [('period', 14)]), 'stoch_d': ('Stochastic %D', [('period', 14)]),
    'adx': ('ADX (trend strength)', [('period', 14)]), 'atr': ('ATR', [('period', 14)]),
    'highest': ('Highest high of last N', [('bars', 20)]), 'lowest': ('Lowest low of last N', [('bars', 20)]),
    'value': ('Number', []),
}
OPERATORS = {'crosses_above': 'crosses above', 'crosses_below': 'crosses below', 'gt': 'is above', 'lt': 'is below',
             'rising': 'is rising', 'falling': 'is falling'}
PATTERNS = {'bullish_engulfing': 'Bullish engulfing', 'bearish_engulfing': 'Bearish engulfing', 'hammer': 'Hammer',
            'shooting_star': 'Shooting star', 'bull_pin': 'Bullish pin bar', 'bear_pin': 'Bearish pin bar',
            'inside_bar': 'Inside bar', 'doji': 'Doji', 'three_up': 'Three green candles', 'three_down': 'Three red candles'}
MIRROR_PATTERN = {'bullish_engulfing': 'bearish_engulfing', 'bearish_engulfing': 'bullish_engulfing', 'hammer': 'shooting_star',
                  'shooting_star': 'hammer', 'bull_pin': 'bear_pin', 'bear_pin': 'bull_pin', 'three_up': 'three_down', 'three_down': 'three_up'}
MIRROR_OP = {'crosses_above': 'crosses_below', 'crosses_below': 'crosses_above', 'gt': 'lt', 'lt': 'gt', 'rising': 'falling', 'falling': 'rising'}
MIRROR_IND = {'highest': 'lowest', 'lowest': 'highest', 'bb_upper': 'bb_lower', 'bb_lower': 'bb_upper', 'high': 'low', 'low': 'high'}
OSC_100 = ('rsi', 'stoch_k', 'stoch_d')          # 0–100: a level mirrors to 100 − level
MARKETS = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD', 'ETHUSD', 'XAGUSD', 'GBPJPY', 'AUDUSD', 'USOIL']
TFS = ['15m', '1h', '4h', '1d']
SESSIONS = {'any': (0, 24), 'london': (7, 16), 'ny': (12, 21), 'overlap': (12, 16)}

TEMPLATES = [
    {'id': 'trend_pullback', 'name': 'EMA trend pullback', 'blurb': 'With the 200 EMA trend, buy when the 20 EMA crosses back above the 50.',
     'symbols': ['XAUUSD'], 'tf': '1h', 'mirror': True, 'filters': {'session': 'any', 'trend': 'with'},
     'buy': {'logic': 'all', 'rules': [{'a': {'ind': 'ema', 'p': [20]}, 'op': 'crosses_above', 'b': {'ind': 'ema', 'p': [50]}}]},
     'exits': {'sl': {'type': 'atr', 'v': 1.5}, 'tp': {'type': 'r', 'v': 2}, 'trail': {'on': False, 'atr': 2}, 'breakeven': {'on': True, 'r': 1}, 'max_bars': 48, 'opposite': True}},
    {'id': 'rsi_reversion', 'name': 'RSI bounce at the bands', 'blurb': 'RSI climbs back above 30 while price is under the lower Bollinger band.',
     'symbols': ['EURUSD'], 'tf': '1h', 'mirror': True, 'filters': {'session': 'any', 'trend': 'none'},
     'buy': {'logic': 'all', 'rules': [{'a': {'ind': 'rsi', 'p': [14]}, 'op': 'crosses_above', 'b': {'ind': 'value', 'v': 30}},
                                        {'a': {'ind': 'low', 'p': []}, 'op': 'lt', 'b': {'ind': 'bb_lower', 'p': [20]}}]},
     'exits': {'sl': {'type': 'atr', 'v': 1.2}, 'tp': {'type': 'r', 'v': 1.5}, 'trail': {'on': False, 'atr': 2}, 'breakeven': {'on': False, 'r': 1}, 'max_bars': 24, 'opposite': False}},
    {'id': 'london_breakout', 'name': 'London breakout', 'blurb': 'Close above the 20-candle high in the London session, ADX above 20.',
     'symbols': ['XAUUSD'], 'tf': '15m', 'mirror': True, 'filters': {'session': 'london', 'trend': 'none'},
     'buy': {'logic': 'all', 'rules': [{'a': {'ind': 'close', 'p': []}, 'op': 'crosses_above', 'b': {'ind': 'highest', 'p': [20]}},
                                        {'a': {'ind': 'adx', 'p': [14]}, 'op': 'gt', 'b': {'ind': 'value', 'v': 20}}]},
     'exits': {'sl': {'type': 'atr', 'v': 1.5}, 'tp': {'type': 'r', 'v': 2.5}, 'trail': {'on': True, 'atr': 2}, 'breakeven': {'on': True, 'r': 1}, 'max_bars': 32, 'opposite': False}},
    {'id': 'macd_momentum', 'name': 'MACD momentum', 'blurb': 'MACD crosses its signal above zero with price over the 50 EMA.',
     'symbols': ['BTCUSD'], 'tf': '4h', 'mirror': True, 'filters': {'session': 'any', 'trend': 'none'},
     'buy': {'logic': 'all', 'rules': [{'a': {'ind': 'macd', 'p': []}, 'op': 'crosses_above', 'b': {'ind': 'macd_signal', 'p': []}},
                                        {'a': {'ind': 'close', 'p': []}, 'op': 'gt', 'b': {'ind': 'ema', 'p': [50]}}]},
     'exits': {'sl': {'type': 'atr', 'v': 2}, 'tp': {'type': 'r', 'v': 3}, 'trail': {'on': True, 'atr': 2.5}, 'breakeven': {'on': True, 'r': 1}, 'max_bars': 40, 'opposite': True}},
    {'id': 'engulfing_200', 'name': 'Engulfing at the trend', 'blurb': 'A bullish engulfing candle above the 200 EMA with RSI under 60.',
     'symbols': ['GBPUSD'], 'tf': '1h', 'mirror': True, 'filters': {'session': 'any', 'trend': 'with'},
     'buy': {'logic': 'all', 'rules': [{'pattern': 'bullish_engulfing'}, {'a': {'ind': 'rsi', 'p': [14]}, 'op': 'lt', 'b': {'ind': 'value', 'v': 60}}]},
     'exits': {'sl': {'type': 'atr', 'v': 1.5}, 'tp': {'type': 'r', 'v': 2}, 'trail': {'on': False, 'atr': 2}, 'breakeven': {'on': True, 'r': 1}, 'max_bars': 36, 'opposite': False}},
    {'id': 'stoch_scalp', 'name': 'Stochastic scalp', 'blurb': 'Stochastic %K crosses above %D below 25, fast exits.',
     'symbols': ['XAUUSD'], 'tf': '15m', 'mirror': True, 'filters': {'session': 'overlap', 'trend': 'with'},
     'buy': {'logic': 'all', 'rules': [{'a': {'ind': 'stoch_k', 'p': [14]}, 'op': 'crosses_above', 'b': {'ind': 'stoch_d', 'p': [14]}},
                                        {'a': {'ind': 'stoch_k', 'p': [14]}, 'op': 'lt', 'b': {'ind': 'value', 'v': 25}}]},
     'exits': {'sl': {'type': 'atr', 'v': 1}, 'tp': {'type': 'r', 'v': 1.5}, 'trail': {'on': False, 'atr': 2}, 'breakeven': {'on': False, 'r': 1}, 'max_bars': 12, 'opposite': True}},
]
DEFAULT_EXITS = {'sl': {'type': 'atr', 'v': 1.5}, 'tp': {'type': 'r', 'v': 2}, 'trail': {'on': False, 'atr': 2},
                 'breakeven': {'on': False, 'r': 1}, 'max_bars': 48, 'opposite': False}


def catalogue():
    return {'indicators': {k: {'label': v[0], 'params': v[1]} for k, v in INDICATORS.items()}, 'operators': OPERATORS,
            'patterns': PATTERNS, 'markets': MARKETS, 'tfs': TFS, 'sessions': list(SESSIONS), 'templates': TEMPLATES}


def normalise(spec):
    """Fill gaps and clamp numbers so a bad request can't break the engine."""
    spec = dict(spec or {})
    spec['symbols'] = [s for s in (spec.get('symbols') or []) if s in MARKETS][:8] or ['XAUUSD']
    spec['tf'] = spec.get('tf') if spec.get('tf') in TFS else '1h'
    for side in ('buy', 'sell'):
        blk = spec.get(side) or {}
        spec[side] = {'logic': 'any' if blk.get('logic') == 'any' else 'all', 'rules': [r for r in (blk.get('rules') or []) if isinstance(r, dict)][:8]}
    spec['mirror'] = bool(spec.get('mirror', True))
    f = spec.get('filters') or {}
    spec['filters'] = {'session': f.get('session') if f.get('session') in SESSIONS else 'any', 'trend': 'with' if f.get('trend') == 'with' else 'none'}
    e = dict(DEFAULT_EXITS, **(spec.get('exits') or {}))
    num = lambda x, d, lo, hi: max(lo, min(hi, float(x))) if isinstance(x, (int, float)) or (isinstance(x, str) and re.match(r'^-?\d+(\.\d+)?$', x)) else d
    e['sl'] = {'type': (e.get('sl') or {}).get('type') if (e.get('sl') or {}).get('type') in ('atr', 'pct') else 'atr', 'v': num((e.get('sl') or {}).get('v'), 1.5, .1, 20)}
    e['tp'] = {'type': (e.get('tp') or {}).get('type') if (e.get('tp') or {}).get('type') in ('r', 'atr', 'pct', 'none') else 'r', 'v': num((e.get('tp') or {}).get('v'), 2, .1, 50)}
    e['trail'] = {'on': bool((e.get('trail') or {}).get('on')), 'atr': num((e.get('trail') or {}).get('atr'), 2, .3, 20)}
    e['breakeven'] = {'on': bool((e.get('breakeven') or {}).get('on')), 'r': num((e.get('breakeven') or {}).get('r'), 1, .2, 10)}
    e['max_bars'] = int(num(e.get('max_bars'), 48, 2, 500))
    e['opposite'] = bool(e.get('opposite'))
    spec['exits'] = e
    spec['risk_pct'] = num(spec.get('risk_pct'), 1, .1, 10)
    spec['name'] = str(spec.get('name') or 'My strategy')[:80]
    return spec


# ═══════════════════════════════════════════════════════════════
# INDICATORS ON A CANDLE SERIES
# ═══════════════════════════════════════════════════════════════
class Data:
    def __init__(self, candles):
        self.t = [int(c['time']) for c in candles]
        self.o = [float(c['open']) for c in candles]
        self.h = [float(c['high']) for c in candles]
        self.l = [float(c['low']) for c in candles]
        self.c = [float(c['close']) for c in candles]
        self.n = len(self.c)
        self._cache = {}

    def get(self, ind, p=None):
        p = tuple(int(x) for x in (p or []) if str(x).lstrip('-').isdigit())
        key = (ind, p)
        if key in self._cache:
            return self._cache[key]
        n = p[0] if p else None
        c = self.c
        if ind in ('close', 'open', 'high', 'low'):
            out = {'close': self.c, 'open': self.o, 'high': self.h, 'low': self.l}[ind]
        elif ind == 'ema':
            out = cs.ema(c, max(2, n or 50))
        elif ind == 'sma':
            k = max(2, n or 50)
            out, s = [None] * self.n, 0.0
            for i, v in enumerate(c):
                s += v
                if i >= k:
                    s -= c[i - k]
                if i >= k - 1:
                    out[i] = s / k
        elif ind == 'rsi':
            out = cs.rsi(c, max(2, n or 14))
        elif ind in ('macd', 'macd_signal', 'macd_hist'):
            m, sg, h = cs.macd(c)
            out = {'macd': m, 'macd_signal': sg, 'macd_hist': h}[ind]
        elif ind in ('bb_upper', 'bb_mid', 'bb_lower'):
            mid, up, lo = cs.bollinger(c, max(2, n or 20))
            out = {'bb_upper': up, 'bb_mid': mid, 'bb_lower': lo}[ind]
        elif ind in ('stoch_k', 'stoch_d'):
            k, d = cs.stochastic(self.h, self.l, c, max(2, n or 14))
            out = k if ind == 'stoch_k' else d
        elif ind == 'adx':
            out = cs.adx(self.h, self.l, c, max(2, n or 14))[0]
        elif ind == 'atr':
            out = cs.wilder(cs.true_range(self.h, self.l, c), max(2, n or 14))
        elif ind in ('highest', 'lowest'):
            k = max(2, n or 20)
            src = self.h if ind == 'highest' else self.l
            fn = max if ind == 'highest' else min
            out = [None] * self.n
            for i in range(k, self.n):
                out[i] = fn(src[i - k:i])               # previous k candles (not including this one)
        else:
            out = [None] * self.n
        self._cache[key] = out
        return out


def _series(d, side):
    if side.get('ind') == 'value':
        try:
            v = float(side.get('v'))
        except (TypeError, ValueError):
            v = 0.0
        return [v] * d.n
    return d.get(side.get('ind'), side.get('p'))


def _pattern(d, name, i):
    o, h, l, c = d.o[i], d.h[i], d.l[i], d.c[i]
    po, pc = d.o[i - 1], d.c[i - 1]
    body, rng = abs(c - o), (h - l) or 1e-12
    up_w, lo_w = h - max(o, c), min(o, c) - l
    if name == 'bullish_engulfing':
        return pc < po and c > o and c >= po and o <= pc and body > abs(pc - po)
    if name == 'bearish_engulfing':
        return pc > po and c < o and c <= po and o >= pc and body > abs(pc - po)
    if name in ('hammer', 'bull_pin'):
        return lo_w >= 2 * body and up_w <= body * (1 if name == 'hammer' else .6) and lo_w > rng * .55
    if name in ('shooting_star', 'bear_pin'):
        return up_w >= 2 * body and lo_w <= body * (1 if name == 'shooting_star' else .6) and up_w > rng * .55
    if name == 'inside_bar':
        return h < d.h[i - 1] and l > d.l[i - 1]
    if name == 'doji':
        return body <= rng * .1
    if name == 'three_up':
        return i > 2 and all(d.c[j] > d.o[j] for j in (i - 2, i - 1, i))
    if name == 'three_down':
        return i > 2 and all(d.c[j] < d.o[j] for j in (i - 2, i - 1, i))
    return False


def _rule_true(d, rule, i):
    if rule.get('pattern'):
        return _pattern(d, rule['pattern'], i)
    a = _series(d, rule.get('a') or {})
    op = rule.get('op')
    if op in ('rising', 'falling'):
        k = max(1, min(20, int(rule.get('n') or 3)))
        if i - k < 0 or any(a[j] is None for j in range(i - k, i + 1)):
            return False
        return all((a[j] > a[j - 1]) if op == 'rising' else (a[j] < a[j - 1]) for j in range(i - k + 1, i + 1))
    b = _series(d, rule.get('b') or {'ind': 'value', 'v': 0})
    if a[i] is None or b[i] is None:
        return False
    if op == 'gt':
        return a[i] > b[i]
    if op == 'lt':
        return a[i] < b[i]
    if a[i - 1] is None or b[i - 1] is None:
        return False
    if op == 'crosses_above':
        return a[i - 1] <= b[i - 1] and a[i] > b[i]
    if op == 'crosses_below':
        return a[i - 1] >= b[i - 1] and a[i] < b[i]
    return False


def mirror_rules(block):
    """Sell rules = buy rules flipped (crosses above → below, RSI 30 → 70, highest → lowest, hammer → shooting star)."""
    out = []
    for r in block.get('rules') or []:
        if r.get('pattern'):
            p = MIRROR_PATTERN.get(r['pattern'])
            if p:
                out.append({'pattern': p})
            elif r['pattern'] in ('inside_bar', 'doji'):
                out.append(dict(r))
            continue
        a, b = dict(r.get('a') or {}), dict(r.get('b') or {})
        a['ind'] = MIRROR_IND.get(a.get('ind'), a.get('ind'))
        if b:
            b['ind'] = MIRROR_IND.get(b.get('ind'), b.get('ind'))
            if b.get('ind') == 'value' and a.get('ind') in OSC_100:
                try:
                    b['v'] = 100 - float(b.get('v'))
                except (TypeError, ValueError):
                    pass
            elif b.get('ind') == 'value' and a.get('ind') in ('macd', 'macd_signal', 'macd_hist'):
                try:
                    b['v'] = -float(b.get('v'))
                except (TypeError, ValueError):
                    pass
        nr = dict(r, a=a, op=MIRROR_OP.get(r.get('op'), r.get('op')))
        if b:
            nr['b'] = b
        if a.get('ind') == 'adx' or (a.get('ind') == 'atr'):
            nr['op'] = r.get('op')                  # trend strength / volatility filters stay the same for sells
        out.append(nr)
    return {'logic': block.get('logic', 'all'), 'rules': out}


def signals(d, spec):
    """→ list of +1 / −1 / 0 per candle (read at that candle's close)."""
    buy = spec['buy']
    sell = mirror_rules(buy) if spec['mirror'] else spec['sell']
    lo, hi = SESSIONS[spec['filters']['session']]
    trend = d.get('ema', [200]) if spec['filters']['trend'] == 'with' else None
    out = [0] * d.n
    for i in range(2, d.n):
        if (lo, hi) != (0, 24):
            hr = datetime.fromtimestamp(d.t[i], dt_tz.utc).hour
            if not lo <= hr < hi:
                continue
        def ok(block):
            rs = block['rules']
            if not rs:
                return False
            vals = (_rule_true(d, r, i) for r in rs)
            return all(vals) if block['logic'] == 'all' else any(vals)
        b, s = ok(buy), ok(sell)
        if trend is not None:
            if trend[i] is None:
                continue
            b = b and d.c[i] > trend[i]
            s = s and d.c[i] < trend[i]
        out[i] = 1 if b and not s else (-1 if s and not b else 0)
    return out


# ═══════════════════════════════════════════════════════════════
# BACKTEST
# ═══════════════════════════════════════════════════════════════
def _spread(symbol):
    try:
        from .mt5_service import get_spec
        sp = get_spec(symbol)
        return float(sp.spread) * float(sp.point)
    except Exception:
        return 0.0


def simulate(d, sig, spec, symbol='', start=0, end=None):
    """Walk the candles with the exits. → list of trades (dicts)."""
    e = spec['exits']
    atr = d.get('atr', [14])
    spread = _spread(symbol)
    end = end or d.n
    trades, i = [], max(start, 1)
    while i < end - 1:
        dirn = sig[i]
        if not dirn or atr[i] is None:
            i += 1
            continue
        j = i + 1                                           # enter at the next candle's open
        entry = d.o[j] + (spread / 2 if dirn > 0 else -spread / 2)
        dist = atr[i] * e['sl']['v'] if e['sl']['type'] == 'atr' else entry * e['sl']['v'] / 100
        if dist <= 0:
            i += 1
            continue
        sl = entry - dirn * dist
        tp_t, tp_v = e['tp']['type'], e['tp']['v']
        tp = None if tp_t == 'none' else entry + dirn * (dist * tp_v if tp_t == 'r' else atr[i] * tp_v if tp_t == 'atr' else entry * tp_v / 100)
        stop, be_done, reason, exit_p, k = sl, False, None, None, j
        while k < end:
            hi_, lo_ = d.h[k], d.l[k]
            hit_sl = lo_ <= stop if dirn > 0 else hi_ >= stop
            hit_tp = tp is not None and (hi_ >= tp if dirn > 0 else lo_ <= tp)
            if hit_sl:
                gap = (d.o[k] < stop) if dirn > 0 else (d.o[k] > stop)
                exit_p, reason = (d.o[k] if gap else stop), ('BE' if be_done and abs(stop - entry) < dist * .05 else ('TRAIL' if stop != sl and not be_done else 'SL'))
                if stop != sl and (stop - entry) * dirn > 0:
                    reason = 'TRAIL'
                break
            if hit_tp:
                exit_p, reason = tp, 'TP'
                break
            if k - j + 1 >= e['max_bars']:
                exit_p, reason = d.c[k], 'TIME'
                break
            if e['opposite'] and sig[k] == -dirn:
                exit_p, reason = d.c[k], 'SIGNAL'
                break
            # manage the stop at this candle's close
            if e['breakeven']['on'] and not be_done and (d.c[k] - entry) * dirn >= dist * e['breakeven']['r']:
                stop = entry if (entry - stop) * dirn > 0 else stop
                be_done = True
            if e['trail']['on'] and atr[k]:
                cand = d.c[k] - dirn * atr[k] * e['trail']['atr']
                if (cand - stop) * dirn > 0:
                    stop = cand
            k += 1
        if exit_p is None:
            break                                           # still open at the end of the data
        exit_p -= dirn * spread / 2
        r = (exit_p - entry) * dirn / dist
        trades.append({'dir': dirn, 'i': j, 'x': k, 't_in': d.t[j], 't_out': d.t[k], 'entry': entry, 'exit': exit_p,
                       'sl': sl, 'tp': tp, 'r': round(r, 3), 'reason': reason, 'bars': k - j + 1})
        i = k + 1 if k > i else i + 1
    return trades


def stats(trades, risk_pct=1.0, start_equity=10000.0):
    eq, peak, max_dd, curve = start_equity, start_equity, 0.0, []
    for t in trades:
        eq *= 1 + risk_pct / 100 * t['r']
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
        curve.append({'t': t['t_out'], 'eq': round(eq, 2), 'dd': round((peak - eq) / peak * 100, 2)})
    rs = [t['r'] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    gross_w, gross_l = sum(wins), -sum(losses)
    mean = sum(rs) / len(rs) if rs else 0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)) if len(rs) > 1 else 0
    months = {}
    eq2 = start_equity
    for t in trades:
        before = eq2
        eq2 *= 1 + risk_pct / 100 * t['r']
        m = datetime.fromtimestamp(t['t_out'], dt_tz.utc).strftime('%Y-%m')
        months[m] = months.get(m, 0) + (eq2 - before) / before * 100
    return {
        'trades': len(rs), 'wins': len(wins), 'win_rate': round(len(wins) / len(rs) * 100, 1) if rs else 0,
        'net_pct': round((eq / start_equity - 1) * 100, 2), 'end_equity': round(eq, 2),
        'profit_factor': round(gross_w / gross_l, 2) if gross_l > 0 else (99.0 if gross_w > 0 else 0),
        'expectancy': round(mean, 3), 'avg_win': round(sum(wins) / len(wins), 2) if wins else 0,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0, 'max_dd': round(max_dd, 2),
        'losing_streak': worst, 'best': round(max(rs), 2) if rs else 0, 'worst': round(min(rs), 2) if rs else 0,
        'sqn': round(mean / sd * math.sqrt(len(rs)), 2) if sd else 0,
        'avg_bars': round(sum(t['bars'] for t in trades) / len(trades), 1) if trades else 0,
        'longs': sum(1 for t in trades if t['dir'] > 0), 'shorts': sum(1 for t in trades if t['dir'] < 0),
        'curve': curve, 'months': [{'m': k, 'pct': round(v, 2)} for k, v in sorted(months.items())],
        'r_list': rs,
    }


def grade(st):
    """A plain-language verdict on the results."""
    if st['trades'] < 8:
        return 'C', 'Too few trades to judge. Use a lower timeframe, more markets or looser rules.'
    pf, e, dd = st['profit_factor'], st['expectancy'], st['max_dd']
    if pf >= 1.6 and e >= .25 and dd <= 15:
        return 'A', 'Strong edge on this history with a controlled drawdown. Check it on other markets before trusting it.'
    if pf >= 1.25 and e > .1:
        return 'B', 'A positive edge. Try the optimizer and the walk-forward check before going live.'
    if pf >= 1:
        return 'C', 'Roughly break-even after costs. Tighten the filters or change the exits.'
    return 'D', 'Loses money on this history. Flip it, add a trend filter or try another market.'


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════
def candles(symbol, tf, bars=1000):
    from .views import get_candles
    cs_, source = get_candles(symbol, tf, bars)
    cs_ = sorted((c for c in cs_ or [] if c.get('time')), key=lambda c: c['time'])
    return cs_, source


def run(spec, symbol=None, with_candles=True):
    spec = normalise(spec)
    symbol = symbol or spec['symbols'][0]
    cdl, source = candles(symbol, spec['tf'])
    if len(cdl) < 150:
        return {'ok': False, 'error': f'Not enough price history for {symbol} {spec["tf"]} right now. Try again in a minute.'}
    d = Data(cdl)
    sig = signals(d, spec)
    trades = simulate(d, sig, spec, symbol)
    st = stats(trades, spec['risk_pct'])
    g, verdict = grade(st)
    out = {'ok': True, 'symbol': symbol, 'tf': spec['tf'], 'source': source, 'bars': d.n,
           'from': d.t[0], 'to': d.t[-1], 'stats': st, 'grade': g, 'verdict': verdict,
           'trades': [dict(t, t_in=t['t_in'], t_out=t['t_out']) for t in trades[-300:]]}
    if with_candles:
        out['candles'] = [[d.t[i], d.o[i], d.h[i], d.l[i], d.c[i]] for i in range(d.n)]
    return out


def run_markets(spec, symbols=None):
    spec = normalise(spec)
    rows = []
    for sym in (symbols or MARKETS[:8]):
        try:
            r = run(spec, sym, with_candles=False)
        except Exception as e:
            r = {'ok': False, 'error': str(e)[:80]}
        if r.get('ok'):
            st = r['stats']
            rows.append({'symbol': sym, 'ok': True, 'trades': st['trades'], 'win_rate': st['win_rate'], 'net_pct': st['net_pct'],
                         'profit_factor': st['profit_factor'], 'max_dd': st['max_dd'], 'expectancy': st['expectancy'], 'grade': r['grade']})
        else:
            rows.append({'symbol': sym, 'ok': False, 'error': r.get('error', '')})
    rows.sort(key=lambda r: (not r['ok'], -(r.get('profit_factor') or 0)))
    return rows


SL_GRID = [0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
TP_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


def optimise(spec, symbol=None):
    """Stops × targets on the first 70% of the history (in-sample), then the winner on the last 30% (out-of-sample)."""
    spec = normalise(spec)
    symbol = symbol or spec['symbols'][0]
    cdl, _ = candles(symbol, spec['tf'])
    if len(cdl) < 200:
        return {'ok': False, 'error': f'Not enough price history for {symbol} right now.'}
    d = Data(cdl)
    sig = signals(d, spec)                                  # rules don't change: read once
    cut = int(d.n * .7)
    grid, best = [], None
    for sl in SL_GRID:
        row = []
        for tp in TP_GRID:
            s2 = dict(spec, exits=dict(spec['exits'], sl={'type': 'atr', 'v': sl}, tp={'type': 'r', 'v': tp}))
            st = stats(simulate(d, sig, s2, symbol, 0, cut), spec['risk_pct'])
            score = st['expectancy'] * math.sqrt(st['trades']) if st['trades'] >= 6 else 0   # robust: edge × sample size (SQN-like)
            row.append({'sl': sl, 'tp': tp, 'pf': st['profit_factor'], 'net': st['net_pct'], 'trades': st['trades'], 'score': round(score, 2)})
            if best is None or score > best['score'] or (score == best['score'] and st['net_pct'] > best['net']):
                best = row[-1]
        grid.append(row)
    if not best or best['score'] <= 0:
        return {'ok': False, 'error': 'Too few trades on this history to optimise (needs 6+ in the first 70%). '
                                      'Loosen the rules, add markets or use a lower timeframe.'}
    s_best = dict(spec, exits=dict(spec['exits'], sl={'type': 'atr', 'v': best['sl']}, tp={'type': 'r', 'v': best['tp']}))
    oos = stats(simulate(d, sig, s_best, symbol, cut, d.n), spec['risk_pct'])
    ins = stats(simulate(d, sig, s_best, symbol, 0, cut), spec['risk_pct'])
    holds = oos['trades'] >= 3 and oos['profit_factor'] >= 1 and oos['expectancy'] > 0
    return {'ok': True, 'symbol': symbol, 'sl_grid': SL_GRID, 'tp_grid': TP_GRID, 'grid': grid, 'best': best, 'score_note': 'Ranked by average R per trade × √trades (rewards an edge that repeats), not by the luckiest profit factor.',
            'in_sample': {k: ins[k] for k in ('trades', 'win_rate', 'profit_factor', 'net_pct', 'max_dd', 'expectancy')},
            'out_sample': {k: oos[k] for k in ('trades', 'win_rate', 'profit_factor', 'net_pct', 'max_dd', 'expectancy')},
            'holds': holds,
            'verdict': ('The best settings also made money on the 30% of history they never saw: a good sign it is not overfitted.' if holds else
                        'The best settings did not hold up on the unseen 30% of history: likely overfitted. Keep simpler settings.')}


def monte_carlo(rs, risk_pct=1.0, runs=500, seed=7):
    """Bootstrap: 500 alternative histories drawn from these trades (with replacement), so some runs get more of the
    losers and some more of the winners. Shows the range of results and drawdowns luck alone could give."""
    if len(rs) < 5:
        return None
    rnd = random.Random(seed)
    n = len(rs)
    finals, dds, paths = [], [], []
    for k in range(runs):
        eq, peak, dd, path = 100.0, 100.0, 0.0, [100.0]
        for _ in range(n):
            eq *= 1 + risk_pct / 100 * rs[rnd.randrange(n)]
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100)
            path.append(round(eq, 2))
        finals.append(eq)
        dds.append(dd)
        if k < 60:
            paths.append(path)
    finals.sort()
    dds.sort()
    pct = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return {'paths': paths, 'final_p5': round(pct(finals, .05) - 100, 1), 'final_p50': round(pct(finals, .5) - 100, 1),
            'final_p95': round(pct(finals, .95) - 100, 1), 'dd_p50': round(pct(dds, .5), 1), 'dd_p95': round(pct(dds, .95), 1),
            'p_loss': round(sum(1 for f in finals if f < 100) / len(finals) * 100, 1),
            'p_dd20': round(sum(1 for x in dds if x >= 20) / len(dds) * 100, 1)}


# ═══════════════════════════════════════════════════════════════
# PLAIN ENGLISH → RULES
# ═══════════════════════════════════════════════════════════════
SYM_WORDS = {'gold': 'XAUUSD', 'xau': 'XAUUSD', 'xauusd': 'XAUUSD', 'silver': 'XAGUSD', 'xagusd': 'XAGUSD', 'eurusd': 'EURUSD', 'euro': 'EURUSD',
             'gbpusd': 'GBPUSD', 'cable': 'GBPUSD', 'pound': 'GBPUSD', 'usdjpy': 'USDJPY', 'yen': 'USDJPY', 'btc': 'BTCUSD', 'bitcoin': 'BTCUSD',
             'btcusd': 'BTCUSD', 'eth': 'ETHUSD', 'ethereum': 'ETHUSD', 'ethusd': 'ETHUSD', 'gbpjpy': 'GBPJPY', 'audusd': 'AUDUSD', 'oil': 'USOIL', 'usoil': 'USOIL'}
TF_WORDS = [(r'\b(15 ?m|15 ?min(ute)?s?)\b', '15m'), (r'\b(1 ?h|1 ?hour|hourly|h1)\b', '1h'), (r'\b(4 ?h|4 ?hours?|h4)\b', '4h'), (r'\b(daily|1 ?d|d1|day chart)\b', '1d')]


def from_text(text):
    """→ (spec, understood: list of plain sentences). Reads the usual ways traders describe a setup."""
    t = ' ' + re.sub(r'\s+', ' ', str(text or '').lower().replace('/', '')) + ' '
    understood, rules = [], []
    syms = []
    for w, s in SYM_WORDS.items():
        if re.search(r'\b' + w + r'\b', t) and s not in syms:
            syms.append(s)
    tf = next((v for pat, v in TF_WORDS if re.search(pat, t)), '1h')
    num = r'(\d+(?:\.\d+)?)'

    def add(rule, say):
        rules.append(rule)
        understood.append(say)

    for m in re.finditer(r'(ema|sma|ma|moving average) ?' + num + r' (?:crosses|cross|crossing) (above|over|below|under) (?:the )?(ema|sma|ma|moving average) ?' + num, t):
        kind = 'sma' if m.group(1) == 'sma' else 'ema'
        op = 'crosses_above' if m.group(3) in ('above', 'over') else 'crosses_below'
        add({'a': {'ind': kind, 'p': [int(float(m.group(2)))]}, 'op': op, 'b': {'ind': kind, 'p': [int(float(m.group(5)))]}},
            f'{kind.upper()} {int(float(m.group(2)))} {OPERATORS[op]} {kind.upper()} {int(float(m.group(5)))}')
    for m in re.finditer(r'(?:price|close) (?:is )?(above|over|below|under) (?:the )?(ema|sma|ma|moving average) ?' + num, t):
        kind = 'sma' if m.group(2) == 'sma' else 'ema'
        op = 'gt' if m.group(1) in ('above', 'over') else 'lt'
        add({'a': {'ind': 'close', 'p': []}, 'op': op, 'b': {'ind': kind, 'p': [int(float(m.group(3)))]}}, f'Price {OPERATORS[op]} {kind.upper()} {int(float(m.group(3)))}')
    for m in re.finditer(r'rsi(?: ?(\d+))? (crosses |cross |goes |moves )?(above|over|below|under|<|>) ?' + num, t):
        per = int(m.group(1)) if m.group(1) else 14
        cross = bool(m.group(2)) and m.group(2).strip() in ('crosses', 'cross')
        up = m.group(3) in ('above', 'over', '>')
        op = ('crosses_above' if up else 'crosses_below') if cross else ('gt' if up else 'lt')
        add({'a': {'ind': 'rsi', 'p': [per]}, 'op': op, 'b': {'ind': 'value', 'v': float(m.group(4))}}, f'RSI {per} {OPERATORS[op]} {m.group(4)}')
    if re.search(r'rsi (is )?oversold', t):
        add({'a': {'ind': 'rsi', 'p': [14]}, 'op': 'lt', 'b': {'ind': 'value', 'v': 30}}, 'RSI below 30 (oversold)')
    if re.search(r'macd (line )?(crosses|cross) (above|over) (the )?signal', t):
        add({'a': {'ind': 'macd'}, 'op': 'crosses_above', 'b': {'ind': 'macd_signal'}}, 'MACD crosses above its signal')
    elif re.search(r'macd (line )?(crosses|cross) (below|under) (the )?signal', t):
        add({'a': {'ind': 'macd'}, 'op': 'crosses_below', 'b': {'ind': 'macd_signal'}}, 'MACD crosses below its signal')
    if re.search(r'macd (is )?(above|over) (zero|0)', t):
        add({'a': {'ind': 'macd'}, 'op': 'gt', 'b': {'ind': 'value', 'v': 0}}, 'MACD above zero')
    m = re.search(r'(?:break|breaks|breakout|closes?) (?:above|over|out of|of)? ?(?:the )?(?:last )?' + num + r'[- ]?(?:candle|bar|period)s?(?:[- ]high)?', t)
    if m:
        n = int(float(m.group(1)))
        add({'a': {'ind': 'close', 'p': []}, 'op': 'crosses_above', 'b': {'ind': 'highest', 'p': [n]}}, f'Close breaks above the {n}-candle high')
    elif re.search(r'\bbreakout\b', t):
        add({'a': {'ind': 'close', 'p': []}, 'op': 'crosses_above', 'b': {'ind': 'highest', 'p': [20]}}, 'Close breaks above the 20-candle high')
    if re.search(r'lower (bollinger )?band', t):
        add({'a': {'ind': 'low', 'p': []}, 'op': 'lt', 'b': {'ind': 'bb_lower', 'p': [20]}}, 'Price touches the lower Bollinger band')
    if re.search(r'stoch(astic)? (crosses|cross) (above|over|up)', t):
        add({'a': {'ind': 'stoch_k', 'p': [14]}, 'op': 'crosses_above', 'b': {'ind': 'stoch_d', 'p': [14]}}, 'Stochastic %K crosses above %D')
    m = re.search(r'adx (?:is )?(?:above|over|>) ?' + num, t)
    if m:
        add({'a': {'ind': 'adx', 'p': [14]}, 'op': 'gt', 'b': {'ind': 'value', 'v': float(m.group(1))}}, f'ADX above {m.group(1)} (trending)')
    for w, p in (('engulfing', 'bullish_engulfing'), ('hammer', 'hammer'), ('pin bar', 'bull_pin'), ('pinbar', 'bull_pin'), ('inside bar', 'inside_bar'), ('three green', 'three_up')):
        if w in t:
            add({'pattern': p}, PATTERNS[p] + ' candle')
    exits = dict(DEFAULT_EXITS)
    m = re.search(r'(?:stop|sl|stop loss|stop-loss)[^0-9]{0,12}' + num + r' ?(x ?)?(atr|%|percent)', t)
    if m:
        exits['sl'] = {'type': 'atr' if m.group(3) == 'atr' else 'pct', 'v': float(m.group(1))}
        understood.append(f'Stop loss {m.group(1)} {"× ATR" if m.group(3) == "atr" else "%"}')
    m = re.search(r'(?:target|tp|take profit|take-profit)[^0-9]{0,12}' + num + r' ?(r\b|x|atr|%|percent)', t) or re.search(r'\b1 ?: ?' + num + r'\b()', t)
    if m:
        u = m.group(2) if m.lastindex and m.lastindex >= 2 else ''
        typ = 'atr' if u == 'atr' else 'pct' if u in ('%', 'percent') else 'r'
        exits['tp'] = {'type': typ, 'v': float(m.group(1))}
        understood.append(f'Take profit {m.group(1)}{"R (times the risk)" if typ == "r" else " × ATR" if typ == "atr" else "%"}')
    if 'trailing' in t or 'trail' in t:
        exits['trail'] = {'on': True, 'atr': 2}
        understood.append('Trailing stop 2 × ATR')
    if 'breakeven' in t or 'break even' in t or 'break-even' in t:
        exits['breakeven'] = {'on': True, 'r': 1}
        understood.append('Stop to break-even at +1R')
    session = 'london' if 'london' in t else 'ny' if ('new york' in t or ' ny ' in t) else 'any'
    trend = 'with' if re.search(r'(with|in) the trend|trend filter|ema ?200 trend|uptrend', t) else 'none'
    if session != 'any':
        understood.append({'london': 'Only in the London session', 'ny': 'Only in the New York session'}[session])
    if trend == 'with':
        understood.append('Only with the EMA-200 trend')
    only_buy = re.search(r'\b(buy|long)s? only\b|\bonly (buy|long)', t)
    spec = normalise({'name': 'From my words', 'symbols': syms or ['XAUUSD'], 'tf': tf, 'mirror': not only_buy,
                      'buy': {'logic': 'all', 'rules': rules}, 'sell': {'logic': 'all', 'rules': []},
                      'filters': {'session': session, 'trend': trend}, 'exits': exits})
    understood.insert(0, f'Market {", ".join(spec["symbols"])} · {spec["tf"]} chart' + (' · buys only' if only_buy else ' · buys and the mirrored sells'))
    return spec, understood


# ═══════════════════════════════════════════════════════════════
# LIVE: the bot trades deployed strategies
# ═══════════════════════════════════════════════════════════════
def live_signal(spec, symbol):
    """The signal on the last CLOSED candle → (direction, entry, sl, tp, bar_time) or None."""
    spec = normalise(spec)
    data = cs.candles_for(symbol, spec['tf'])
    cdl = sorted((c for c in data.get('candles') or [] if c.get('time')), key=lambda c: c['time'])[:-1]   # last one still forming
    if len(cdl) < 210:
        return None
    d = Data(cdl)
    sig = signals(d, spec)
    i = d.n - 1
    if not sig[i]:
        return None
    atr = d.get('atr', [14])[i]
    if not atr:
        return None
    e, dirn, entry = spec['exits'], sig[i], d.c[i]
    dist = atr * e['sl']['v'] if e['sl']['type'] == 'atr' else entry * e['sl']['v'] / 100
    tp_t, tp_v = e['tp']['type'], e['tp']['v']
    tp = None if tp_t == 'none' else entry + dirn * (dist * tp_v if tp_t == 'r' else atr * tp_v if tp_t == 'atr' else entry * tp_v / 100)
    return ('BUY' if dirn > 0 else 'SELL', entry, entry - dirn * dist, tp, d.t[i])
