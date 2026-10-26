# store/chart_scanner.py
"""
AI chart scanner — reads real candles and proposes a trade plan.

For every symbol it looks at three timeframes (lower / main / higher, set by the
trading style) and on each one:

  1. Computes the classic indicators on CLOSED bars only
       EMA 20/50/200, MACD 12-26-9, RSI 14, Stochastic 14-3-3, ADX 14,
       ATR 14, Bollinger 20/2, fractal swing points.
  2. Reads the chart: market structure (HH/HL vs LH/LL), support/resistance
     zones clustered from swing points, breakouts, RSI divergence and the
     candlestick pattern on the last closed bar.
  3. Scores twelve checks into one number from -100 (strong sell) to +100.
  4. Trains a logistic-regression model on the same features over the
     symbol's own history (label: did price reach +1.5 ATR before -1.5 ATR in
     the next 20 bars?). It is trained on the first 70 % of bars and tested on
     the last 30 %, so the accuracy shown is out-of-sample.
  5. Backtests the score rule on the same history (ATR stop, 1.5 R target).

The main and higher timeframe scores and the model's probability are blended
into the final signal; SL/TP come from swing points, S/R zones and ATR.

Nothing here is a guarantee — these are statistical estimates from past prices.
"""
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache

from . import mt5_service as mt5

logger = logging.getLogger(__name__)

STYLES = {
    #            lower  main   higher  bars on main
    'scalp':    ('5m',  '15m', '1h'),
    'intraday': ('15m', '1h',  '4h'),
    'swing':    ('1h',  '4h',  '1d'),
}
SCAN_SYMBOLS = [
    'XAUUSD', 'XAGUSD', 'USOIL', 'UKOIL',
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD',
    'EURGBP', 'EURJPY', 'GBPJPY',
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'BNBUSD', 'XRPUSD',
]
CANDLE_TTL = {'5m': 60, '15m': 120, '30m': 240, '1h': 300, '4h': 900, '1d': 1800}
BARS = 600                 # history pulled per timeframe
HORIZON = 20               # bars a trade is given to work out (model label + backtest)
FEATURES = ['trend', 'ema200', 'macd', 'rsi', 'structure', 'stoch', 'bands', 'pattern', 'levels', 'breakout', 'divergence', 'adx']
WEIGHTS = {'trend': 18, 'ema200': 8, 'macd': 12, 'rsi': 9, 'structure': 14, 'stoch': 5,
           'bands': 5, 'pattern': 9, 'levels': 8, 'breakout': 8, 'divergence': 7}
SIGNAL_THRESHOLD = 20      # |blended score| needed for BUY / SELL
# Bump when the result format changes: it is part of every cache key, so results cached by an older
# version of this file are never served after an upgrade, and pages can tell which engine answered.
ENGINE_VERSION = 'coach-3'


def result_key(symbol, style, detail=False):
    return f"aiscan:r:{ENGINE_VERSION}:{symbol}:{style}:{'d' if detail else 's'}"


# ═══════════════════════════════════════════════════════════════
# INDICATORS (full series, None until warmed up)
# ═══════════════════════════════════════════════════════════════
def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder(values, n):
    """Wilder's smoothing (RMA) — what MT5 uses inside RSI / ATR / ADX."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(close, n=14):
    gains = [0.0] + [max(close[i] - close[i - 1], 0) for i in range(1, len(close))]
    losses = [0.0] + [max(close[i - 1] - close[i], 0) for i in range(1, len(close))]
    ag, al = wilder(gains[1:], n), wilder(losses[1:], n)
    out = [None]
    for g, l in zip(ag, al):
        out.append(None if g is None else (100.0 if l == 0 else 100 - 100 / (1 + g / l)))
    return out


def true_range(high, low, close):
    return [high[0] - low[0]] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
                                 for i in range(1, len(close))]


def adx(high, low, close, n=14):
    tr = true_range(high, low, close)
    pdm = [0.0] + [max(high[i] - high[i - 1], 0) if high[i] - high[i - 1] > low[i - 1] - low[i] else 0
                   for i in range(1, len(close))]
    mdm = [0.0] + [max(low[i - 1] - low[i], 0) if low[i - 1] - low[i] > high[i] - high[i - 1] else 0
                   for i in range(1, len(close))]
    atr_, sp, sm = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    pdi, mdi, dx = [None] * len(close), [None] * len(close), []
    for i in range(len(close)):
        if atr_[i]:
            pdi[i], mdi[i] = 100 * sp[i] / atr_[i], 100 * sm[i] / atr_[i]
            s = pdi[i] + mdi[i]
            dx.append(100 * abs(pdi[i] - mdi[i]) / s if s else 0)
        else:
            dx.append(None)
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx_ = [None] * len(close)
    if first is not None:
        sm_dx = wilder(dx[first:], n)
        for j, v in enumerate(sm_dx):
            adx_[first + j] = v
    return adx_, pdi, mdi


def macd(close, fast=12, slow=26, sig=9):
    ef, es = ema(close, fast), ema(close, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_ = [None] * len(line)
    s = ema(line[first:], sig)
    for j, v in enumerate(s):
        sig_[first + j] = v
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig_)]
    return line, sig_, hist


def bollinger(close, n=20, k=2.0):
    mid, up, lo = [None] * len(close), [None] * len(close), [None] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


def stochastic(high, low, close, k=14, d=3, slow=3):
    raw = [None] * len(close)
    for i in range(k - 1, len(close)):
        hh, ll = max(high[i - k + 1:i + 1]), min(low[i - k + 1:i + 1])
        raw[i] = 50.0 if hh == ll else 100 * (close[i] - ll) / (hh - ll)

    def sma(vals, n):
        out = [None] * len(vals)
        for i in range(len(vals)):
            w = vals[max(0, i - n + 1):i + 1]
            if len(w) == n and all(v is not None for v in w):
                out[i] = sum(w) / n
        return out
    kk = sma(raw, slow)
    return kk, sma(kk, d)


def fractals(high, low, side=2):
    """Swing highs / lows (Bill Williams fractals). Index i is only known at bar i + side."""
    ph, pl = [], []
    for i in range(side, len(high) - side):
        if all(high[i] > high[i - j] for j in range(1, side + 1)) and all(high[i] >= high[i + j] for j in range(1, side + 1)):
            ph.append(i)
        if all(low[i] < low[i - j] for j in range(1, side + 1)) and all(low[i] <= low[i + j] for j in range(1, side + 1)):
            pl.append(i)
    return ph, pl


# ═══════════════════════════════════════════════════════════════
# CHART READING
# ═══════════════════════════════════════════════════════════════
class Series:
    """Every indicator for one timeframe, computed once."""

    def __init__(self, candles):
        self.t = [int(c['time']) for c in candles]
        self.o = [float(c['open']) for c in candles]
        self.h = [float(c['high']) for c in candles]
        self.l = [float(c['low']) for c in candles]
        self.c = [float(c['close']) for c in candles]
        self.n = len(self.c)
        self.ema20, self.ema50, self.ema200 = ema(self.c, 20), ema(self.c, 50), ema(self.c, 200)
        self.rsi = rsi(self.c)
        self.atr = wilder(true_range(self.h, self.l, self.c), 14)
        self.adx, self.pdi, self.mdi = adx(self.h, self.l, self.c)
        self.macd, self.macd_sig, self.hist = macd(self.c)
        self.bb_mid, self.bb_up, self.bb_lo = bollinger(self.c)
        self.stk, self.std = stochastic(self.h, self.l, self.c)
        self.ph, self.pl = fractals(self.h, self.l)

    def ready(self, i):
        return i >= 60 and None not in (self.ema20[i], self.ema50[i], self.rsi[i], self.atr[i], self.adx[i],
                                        self.hist[i], self.hist[i - 1], self.bb_up[i], self.stk[i], self.std[i])

    def pivots_until(self, i, lookback=200):
        """Swing points already confirmed at bar i (fractal needs 2 bars to the right)."""
        lo = i - lookback
        return ([p for p in self.ph if lo <= p <= i - 2], [p for p in self.pl if lo <= p <= i - 2])

    def levels(self, i, lookback=200):
        """Support / resistance zones: swing points within 0.6 ATR merged, strength = touches."""
        ph, pl = self.pivots_until(i, lookback)
        atr = self.atr[i] or 0
        pts = sorted([self.h[p] for p in ph] + [self.l[p] for p in pl])
        zones = []
        for p in pts:
            if zones and p - zones[-1]['hi'] <= atr * 0.6:
                z = zones[-1]
                z['hi'] = p
                z['sum'] += p
                z['touches'] += 1
            else:
                zones.append({'lo': p, 'hi': p, 'sum': p, 'touches': 1})
        return [{'price': z['sum'] / z['touches'], 'touches': z['touches']} for z in zones]


def candle_pattern(s, i):
    """Pattern on bar i → (name, +1 bullish / -1 bearish / 0)."""
    o, h, l, c = s.o[i], s.h[i], s.l[i], s.c[i]
    po, pc = s.o[i - 1], s.c[i - 1]
    rng = h - l
    if rng <= 0:
        return None, 0
    body = abs(c - o)
    upper, lower = h - max(o, c), min(o, c) - l
    atr = s.atr[i] or rng
    if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
        return 'Bullish engulfing', 1
    if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
        return 'Bearish engulfing', -1
    if lower >= 2 * body and upper <= body * 0.6 and rng > 0.6 * atr:
        return 'Hammer / bullish pin bar', 1
    if upper >= 2 * body and lower <= body * 0.6 and rng > 0.6 * atr:
        return 'Shooting star / bearish pin bar', -1
    if i >= 2:
        o2, c2 = s.o[i - 2], s.c[i - 2]
        if c2 < o2 and abs(pc - po) < abs(c2 - o2) * 0.4 and c > o and c > (o2 + c2) / 2:
            return 'Morning star', 1
        if c2 > o2 and abs(pc - po) < abs(c2 - o2) * 0.4 and c < o and c < (o2 + c2) / 2:
            return 'Evening star', -1
    if h <= s.h[i - 1] and l >= s.l[i - 1]:
        return 'Inside bar', 0
    if body <= rng * 0.1:
        return 'Doji', 0
    return None, 0


def structure(s, i):
    ph, pl = s.pivots_until(i, 150)
    if len(ph) < 2 or len(pl) < 2:
        return 0, 'Not enough swings yet'
    hh, hl = s.h[ph[-1]] > s.h[ph[-2]], s.l[pl[-1]] > s.l[pl[-2]]
    if hh and hl:
        return 1, 'Higher highs and higher lows (uptrend structure)'
    if not hh and not hl:
        return -1, 'Lower highs and lower lows (downtrend structure)'
    return 0, 'Mixed swings (range)'


def divergence(s, i):
    ph, pl = s.pivots_until(i, 80)
    if len(pl) >= 2:
        a, b = pl[-2], pl[-1]
        if s.l[b] < s.l[a] and s.rsi[b] and s.rsi[a] and s.rsi[b] > s.rsi[a] + 2 and i - b <= 12:
            return 1, 'Bullish RSI divergence (lower low, higher RSI)'
    if len(ph) >= 2:
        a, b = ph[-2], ph[-1]
        if s.h[b] > s.h[a] and s.rsi[b] and s.rsi[a] and s.rsi[b] < s.rsi[a] - 2 and i - b <= 12:
            return -1, 'Bearish RSI divergence (higher high, lower RSI)'
    return 0, None


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def read_bar(s, i, with_text=False):
    """Feature vector (each in [-1, 1]) + score for closed bar i."""
    c, atr = s.c[i], s.atr[i]
    f, why = {}, []

    def note(key, text):
        if with_text and text:
            why.append({'key': key, 'text': text, 'impact': round(f[key] * WEIGHTS.get(key, 0), 1)})

    # 1. trend: price vs EMA50, EMA20 vs EMA50, EMA50 slope
    slope = (s.ema50[i] - s.ema50[i - 10]) / (atr * 2) if s.ema50[i - 10] else 0
    f['trend'] = clamp(0.4 * (1 if c > s.ema50[i] else -1) + 0.3 * (1 if s.ema20[i] > s.ema50[i] else -1) + 0.3 * clamp(slope))
    note('trend', f"Price {'above' if c > s.ema50[i] else 'below'} EMA 50, EMA 20 {'over' if s.ema20[i] > s.ema50[i] else 'under'} EMA 50")
    # 2. long-term filter
    if s.ema200[i]:
        f['ema200'] = 1.0 if c > s.ema200[i] else -1.0
        note('ema200', f"{'Above' if c > s.ema200[i] else 'Below'} the 200 EMA")
    else:
        f['ema200'] = 0.0
    # 3. MACD histogram sign + momentum
    h0, h1 = s.hist[i], s.hist[i - 1]
    f['macd'] = clamp((0.6 if h0 > 0 else -0.6) + (0.4 if h0 > h1 else -0.4))
    crossed = (h0 > 0) != (h1 > 0)
    note('macd', ('MACD just crossed ' + ('up' if h0 > 0 else 'down')) if crossed else
         f"MACD histogram {'positive' if h0 > 0 else 'negative'} and {'rising' if h0 > h1 else 'falling'}")
    # 4. RSI: momentum zone, but stretched readings argue against chasing
    r = s.rsi[i]
    if r >= 75:
        f['rsi'] = -0.5
        note('rsi', f'RSI {r:.0f} — overbought, late to buy')
    elif r <= 25:
        f['rsi'] = 0.5
        note('rsi', f'RSI {r:.0f} — oversold, late to sell')
    else:
        f['rsi'] = clamp((r - 50) / 15)
        note('rsi', f"RSI {r:.0f} ({'bullish' if r > 55 else 'bearish' if r < 45 else 'neutral'} momentum)")
    # 5. market structure
    f['structure'], txt = structure(s, i)
    f['structure'] = float(f['structure'])
    note('structure', txt)
    # 6. stochastic cross
    k0, d0, k1, d1 = s.stk[i], s.std[i], s.stk[i - 1], s.std[i - 1]
    f['stoch'] = 0.0
    if None not in (k1, d1):
        if k1 <= d1 and k0 > d0 and k0 < 80:
            f['stoch'] = 1.0
            note('stoch', f'Stochastic crossed up at {k0:.0f}')
        elif k1 >= d1 and k0 < d0 and k0 > 20:
            f['stoch'] = -1.0
            note('stoch', f'Stochastic crossed down at {k0:.0f}')
    # 7. Bollinger: close outside the band in the trend's direction = expansion
    f['bands'] = 0.0
    if c > s.bb_up[i]:
        f['bands'] = 1.0 if f['trend'] > 0 else -0.5
        note('bands', 'Closed above the upper Bollinger band')
    elif c < s.bb_lo[i]:
        f['bands'] = -1.0 if f['trend'] < 0 else 0.5
        note('bands', 'Closed below the lower Bollinger band')
    # 8. candlestick pattern
    name, d = candle_pattern(s, i)
    f['pattern'] = float(d)
    if name:
        note('pattern', f'{name} on the last closed candle')
    # 9/10. support / resistance: room to run, and breakouts
    lv = s.levels(i)
    above = [z for z in lv if z['price'] > c + atr * 0.1]
    below = [z for z in lv if z['price'] < c - atr * 0.1]
    res = min(above, key=lambda z: z['price'])['price'] if above else None
    sup = max(below, key=lambda z: z['price'])['price'] if below else None
    room_up = (res - c) / atr if res else 5
    room_dn = (c - sup) / atr if sup else 5
    f['levels'] = clamp((min(room_up, 4) - min(room_dn, 4)) / 3)
    if with_text:
        if res and room_up < 1:
            note('levels', f'Resistance close above ({room_up:.1f} ATR away)')
        elif sup and room_dn < 1:
            note('levels', f'Support close below ({room_dn:.1f} ATR away)')
    f['breakout'] = 0.0
    prev_c = s.c[i - 1]
    for z in lv:
        if z['touches'] >= 2 and prev_c <= z['price'] < c - atr * 0.2:
            f['breakout'] = 1.0
            note('breakout', f"Broke above a {z['touches']}-touch resistance")
            break
        if z['touches'] >= 2 and prev_c >= z['price'] > c + atr * 0.2:
            f['breakout'] = -1.0
            note('breakout', f"Broke below a {z['touches']}-touch support")
            break
    # 11. divergence
    d, txt = divergence(s, i)
    f['divergence'] = float(d)
    note('divergence', txt)
    # 12. ADX: trend strength (direction from DI) — used as a multiplier and a model feature
    a = s.adx[i]
    f['adx'] = clamp((a - 20) / 20) * (1 if s.pdi[i] > s.mdi[i] else -1)

    trend_mult = 1.0 if a >= 20 else 0.65
    total = sum(WEIGHTS.values())
    raw = 0.0
    for k, w in WEIGHTS.items():
        mult = trend_mult if k in ('trend', 'macd', 'structure', 'breakout') else 1.0
        raw += f[k] * w * mult
    score = clamp(raw / total * 1.6, -1, 1) * 100
    if with_text:
        why.append({'key': 'adx', 'text': f"ADX {a:.0f} — {'trending' if a >= 25 else 'weak trend' if a >= 20 else 'ranging, trend signals discounted'}", 'impact': 0})
    return f, score, why, {'resistance': res, 'support': sup, 'levels': lv}


# ═══════════════════════════════════════════════════════════════
# MODEL: logistic regression fitted with Newton's method (IRLS)
# ═══════════════════════════════════════════════════════════════
def _solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                fct = m[r][col] / m[col][col]
                for k in range(col, n + 1):
                    m[r][k] -= fct * m[col][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _sigmoid(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def fit_logistic(X, y, l2=1.0, iters=8):
    d = len(X[0]) + 1
    w = [0.0] * d
    for _ in range(iters):
        H = [[0.0] * d for _ in range(d)]
        g = [0.0] * d
        for xi, yi in zip(X, y):
            v = [1.0] + xi
            p = _sigmoid(sum(a * b for a, b in zip(w, v)))
            r = p * (1 - p)
            for a in range(d):
                g[a] += (p - yi) * v[a]
                va = v[a] * r
                Ha = H[a]
                for b in range(a, d):
                    Ha[b] += va * v[b]
        for a in range(d):
            for b in range(a):
                H[a][b] = H[b][a]
            if a:                       # don't shrink the intercept
                H[a][a] += l2
                g[a] += l2 * w[a]
        step = _solve(H, g)
        if step is None:
            break
        w = [a - b for a, b in zip(w, step)]
        if max(abs(x) for x in step) < 1e-6:
            break
    return w


def predict(w, x):
    return _sigmoid(sum(a * b for a, b in zip(w, [1.0] + x)))


def label(s, i, k=1.5):
    """1 if price touched +k·ATR before -k·ATR within HORIZON bars, 0 if the reverse, None if neither."""
    atr = s.atr[i]
    up, dn = s.c[i] + k * atr, s.c[i] - k * atr
    for j in range(i + 1, min(s.n, i + 1 + HORIZON)):
        hit_up, hit_dn = s.h[j] >= up, s.l[j] <= dn
        if hit_up and hit_dn:
            return None          # both in one bar — order unknown
        if hit_up:
            return 1
        if hit_dn:
            return 0
    return None


def train_and_backtest(s, last):
    """Walk the history once: features per bar, model fit/test, and a rule backtest."""
    rows = []
    for i in range(60, last + 1):
        if s.ready(i):
            f, score, _, _ = read_bar(s, i)
            rows.append((i, [f[k] for k in FEATURES], score))
    labelled = [(i, x, label(s, i)) for i, x, _ in rows if i <= last - 1]
    labelled = [(i, x, y) for i, x, y in labelled if y is not None]
    model = {'trained': False}
    if len(labelled) >= 80:
        cut = int(len(labelled) * 0.7)
        train, test = labelled[:cut], labelled[cut:]
        w = fit_logistic([x for _, x, _ in train], [y for _, _, y in train])
        hits = sum(1 for _, x, y in test if (predict(w, x) >= 0.5) == (y == 1))
        base = sum(y for _, _, y in test) / len(test)
        w_all = fit_logistic([x for _, x, _ in labelled], [y for _, _, y in labelled])
        model = {'trained': True, 'weights': w_all, 'test_accuracy': hits / len(test),
                 'baseline': max(base, 1 - base), 'samples': len(labelled), 'test_samples': len(test)}

    # Backtest the score rule: enter on close when |score| ≥ threshold, SL 1.5 ATR, TP 1.5 R, one trade at a time
    trades, i_next = [], 0
    for i, _, score in rows:
        if i < i_next or i >= last or abs(score) < SIGNAL_THRESHOLD:
            continue
        long_ = score > 0
        risk = 1.5 * s.atr[i]
        entry = s.c[i]
        sl, tp = (entry - risk, entry + 1.5 * risk) if long_ else (entry + risk, entry - 1.5 * risk)
        result, exit_i = None, None
        for j in range(i + 1, min(s.n, i + 1 + HORIZON * 2)):
            hit_sl = s.l[j] <= sl if long_ else s.h[j] >= sl
            hit_tp = s.h[j] >= tp if long_ else s.l[j] <= tp
            if hit_sl:                 # pessimistic when both happen in one bar
                result, exit_i = -1.0, j
                break
            if hit_tp:
                result, exit_i = 1.5, j
                break
        if result is None:
            exit_i = min(s.n - 1, i + HORIZON * 2)
            move = (s.c[exit_i] - entry) if long_ else (entry - s.c[exit_i])
            result = move / risk
        trades.append(result)
        i_next = exit_i + 1
    wins = [r for r in trades if r > 0]
    losses = [-r for r in trades if r <= 0]
    backtest = {
        'trades': len(trades),
        'win_rate': len(wins) / len(trades) if trades else None,
        'avg_r': sum(trades) / len(trades) if trades else None,
        'profit_factor': (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else (None if not wins else float('inf')),
        'bars': last - 60,
    }
    return model, backtest


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════
def candles_for(symbol, tf):
    key = f'aiscan:c:{symbol}:{tf}'
    data = cache.get(key)
    if data is None:
        from .views import get_candles          # views imports a lot; keep it lazy
        try:
            candles, source = get_candles(symbol, tf, BARS)
        except Exception as e:
            logger.warning(f'AI scan candles {symbol} {tf}: {e}')
            candles, source = [], None
        candles = sorted((c for c in candles or [] if c.get('time')), key=lambda c: c['time'])
        data = {'candles': candles, 'source': source}
        cache.set(key, data, CANDLE_TTL.get(tf, 300) if candles else 30)
    return data


def _r(symbol, v):
    if v is None:
        return None
    spec = mt5.SPECS.get(symbol)
    return round(v, spec.digits if spec else 5)


# ═══════════════════════════════════════════════════════════════
# ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyse_tf(symbol, tf, train=False):
    data = candles_for(symbol, tf)
    candles = data['candles']
    if len(candles) < 120:
        return None
    s = Series(candles)
    last = s.n - 2                      # the final candle is still forming
    while last > 60 and not s.ready(last):
        last -= 1
    if not s.ready(last):
        return None
    f, score, why, lv = read_bar(s, last, with_text=True)
    out = {'tf': tf, 'series': s, 'last': last, 'features': f, 'score': score, 'reasons': why,
           'levels': lv, 'source': data['source']}
    if train:
        out['model'], out['backtest'] = train_and_backtest(s, last)
        if out['model'].get('trained'):
            out['model']['prob_up'] = predict(out['model']['weights'], [f[k] for k in FEATURES])
    return out


def trade_plan(symbol, direction, main, price):
    s, i = main['series'], main['last']
    atr = s.atr[i]
    lv = main['levels']['levels']
    ph, pl = s.pivots_until(s.n - 1, 60)
    if direction == 'BUY':
        swing = [s.l[p] for p in pl if s.l[p] < price]
        sl = (max(swing) - 0.2 * atr) if swing else price - 1.5 * atr
        if not (1.0 * atr <= price - sl <= 2.5 * atr):
            sl = price - 1.5 * atr
        risk = price - sl
        targets = sorted(z['price'] for z in lv if z['price'] > price + 1.2 * risk)
        tp1 = targets[0] if targets and targets[0] <= price + 4 * risk else price + 1.5 * risk
        tp2 = max(tp1 + risk, price + 3 * risk)
    else:
        swing = [s.h[p] for p in ph if s.h[p] > price]
        sl = (min(swing) + 0.2 * atr) if swing else price + 1.5 * atr
        if not (1.0 * atr <= sl - price <= 2.5 * atr):
            sl = price + 1.5 * atr
        risk = sl - price
        targets = sorted((z['price'] for z in lv if z['price'] < price - 1.2 * risk), reverse=True)
        tp1 = targets[0] if targets and targets[0] >= price - 4 * risk else price - 1.5 * risk
        tp2 = min(tp1 - risk, price - 3 * risk)
    ema20 = s.ema20[i]
    pullback = None
    # A better entry only makes sense between the price and the stop
    if (direction == 'BUY' and sl + 0.3 * atr < ema20 < price - 0.4 * atr) or \
       (direction == 'SELL' and price + 0.4 * atr < ema20 < sl - 0.3 * atr):
        pullback = _r(symbol, ema20)
    risk_per_lot = margin_per_lot = None
    try:
        risk_per_lot = abs(float(mt5.calc_profit(symbol, direction, 1, price, sl)))
        margin_per_lot = float(mt5.calc_margin(symbol, 1, price))
    except Exception:
        pass
    return {
        'entry': _r(symbol, price), 'sl': _r(symbol, sl), 'tp1': _r(symbol, tp1), 'tp2': _r(symbol, tp2),
        'rr': round(abs(tp1 - price) / risk, 2) if risk else None,
        'risk_atr': round(risk / atr, 2), 'atr': _r(symbol, atr),
        'pullback_entry': pullback, 'risk_per_lot_usd': round(risk_per_lot, 2) if risk_per_lot else None,
        'margin_per_lot_usd': round(margin_per_lot, 2) if margin_per_lot else None,
        'volume_min': float(mt5.SPECS[symbol].volume_min) if symbol in mt5.SPECS and hasattr(mt5.SPECS[symbol], 'volume_min') else 0.01,
    }


# ═══════════════════════════════════════════════════════════════
# ENTRY COACH — where are we in the move, and is this the entry?
# ═══════════════════════════════════════════════════════════════
# A trend trader does not buy the top of an impulse. They wait for the pullback
# (retracement) into the 38.2–78.6 % Fibonacci zone of the last leg, ideally where
# support / an EMA / an old swing level sits, then wait for price to show it is
# turning (a break of the pullback's last lower high, or a rejection candle)
# before entering, with the stop beyond the pullback low and the target at the
# old high and its extensions. The coach reads exactly that from closed candles.
ZIGZAG_ATR = 2.0           # a swing must reverse at least this many ATR to count
FIB_LEVELS = (0, 0.236, 0.382, 0.5, 0.618, 0.786, 1)
ZONE = (0.382, 0.786)      # where a pullback entry is allowed
GOLDEN = (0.5, 0.618)      # where most trend traders want to buy / sell
STATE_TEXT = {
    'READY': 'Entry confirmed',
    'IN_ZONE': 'In the entry zone, wait for confirmation',
    'WAIT_PULLBACK': 'Wait for the pullback',
    'EXTENDED': "Don't chase, wait for a pullback",
    'MISSED': 'Missed: price left the zone',
    'INVALID': 'Setup broken',
    'NO_TREND': 'No clear trend, stand aside',
}


def zigzag(s, end, k=ZIGZAG_ATR, lookback=300):
    """Significant swing points up to closed bar `end`.
    Returns (confirmed pivots [(i, price, 'H'|'L')], running extreme (i, price, 'H'|'L') or None)."""
    start = max(1, end - lookback)
    atr_end = s.atr[end] or (s.h[end] - s.l[end]) or 1e-9
    piv, direction, hi_i, lo_i, ext = [], 0, start, start, start
    for i in range(start + 1, end + 1):
        a = s.atr[i] or atr_end
        if direction == 0:
            if s.h[i] > s.h[hi_i]:
                hi_i = i
            if s.l[i] < s.l[lo_i]:
                lo_i = i
            if s.h[hi_i] - s.l[lo_i] >= k * a:
                if lo_i < hi_i:
                    piv.append((lo_i, s.l[lo_i], 'L'))
                    direction, ext = 1, hi_i
                else:
                    piv.append((hi_i, s.h[hi_i], 'H'))
                    direction, ext = -1, lo_i
        elif direction == 1:
            if s.h[i] >= s.h[ext]:
                ext = i
            elif s.h[ext] - s.l[i] >= k * a:
                piv.append((ext, s.h[ext], 'H'))
                direction, ext = -1, i
        else:
            if s.l[i] <= s.l[ext]:
                ext = i
            elif s.h[i] - s.l[ext] >= k * a:
                piv.append((ext, s.l[ext], 'L'))
                direction, ext = 1, i
    running = None
    if direction == 1:
        running = (ext, s.h[ext], 'H')
    elif direction == -1:
        running = (ext, s.l[ext], 'L')
    return piv, running


def swing_trend(piv, running):
    """+1 higher highs & higher lows, -1 lower highs & lower lows, 0 mixed / not enough swings.
    A running extreme counts only when it extends the trend (a new high / new low)."""
    highs = [p[1] for p in piv if p[2] == 'H']
    lows = [p[1] for p in piv if p[2] == 'L']
    if running and running[2] == 'H' and highs and running[1] > highs[-1]:
        highs = highs + [running[1]]
    if running and running[2] == 'L' and lows and running[1] < lows[-1]:
        lows = lows + [running[1]]
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 1
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return -1
    return 0


def _swing_labels(s, piv, running, keep=8):
    out, last = [], {'H': None, 'L': None}
    pts = piv + ([running] if running else [])
    for i, price, kind in pts:
        prev = last[kind]
        if kind == 'H':
            lab = 'H' if prev is None else ('HH' if price > prev else 'LH')
        else:
            lab = 'L' if prev is None else ('HL' if price > prev else 'LL')
        last[kind] = price
        out.append({'time': s.t[i], 'price': price, 'kind': kind, 'label': lab})
    return out[-keep:]


def _fmt(symbol, v):
    spec = mt5.SPECS.get(symbol)
    d = spec.digits if spec else 5
    return f'{v:.{d}f}'


def entry_coach(symbol, main, higher, lower, price):
    """Pullback-entry read of the main timeframe. Everything comes from closed candles + the live price."""
    s, last = main['series'], main['last']
    tf = main['tf'].upper()
    atr = s.atr[last]
    f = lambda v: _fmt(symbol, v)
    piv, running = zigzag(s, last)
    trend = swing_trend(piv, running)
    htf_dir, htf_text = 0, None
    if higher:
        hs, hl = higher['series'], higher['last']
        hp, hr = zigzag(hs, hl)
        htf_dir = swing_trend(hp, hr)
        if htf_dir == 0 and abs(higher['score']) >= 15:
            htf_dir = 1 if higher['score'] > 0 else -1
        htf_text = higher['tf'].upper()
    out = {'state': 'NO_TREND', 'side': None, 'tf': main['tf'], 'atr': _r(symbol, atr),
           'swings': _swing_labels(s, piv, running), 'trend': trend, 'htf_trend': htf_dir,
           'htf_tf': higher['tf'] if higher else None}
    checks = []

    def check(key, label, status, text, weight=1):
        checks.append({'key': key, 'label': label, 'status': status, 'text': text, 'weight': weight})

    lows = [p for p in piv if p[2] == 'L']
    highs = [p for p in piv if p[2] == 'H']
    if trend == 0 or (trend == 1 and not lows) or (trend == -1 and not highs):
        mixed = 'Swings are mixed (no higher highs + higher lows or lower highs + lower lows)' if len(piv) >= 4 \
            else 'Not enough clear swings yet'
        check('structure', 'Market structure', 'fail', f'{mixed} on the {tf}. Pullback entries need a trend.', 3)
        out.update(headline=STATE_TEXT['NO_TREND'],
                   instruction=f'No trend to pull back into on the {tf}. Wait for a clear break of the range, then for the first pullback.',
                   story=[f'{mixed} on the {tf} chart, so there is no "with-trend" side and no retracement to measure. '
                          'Most losing trades come from forcing entries in ranges: stand aside or trade only the range edges with small size.'],
                   checks=checks, quality=0, fib=None, plan=None, retrace=None)
        return out

    side = 'BUY' if trend == 1 else 'SELL'
    up = trend == 1
    sgn = 1 if up else -1
    word = 'uptrend' if up else 'downtrend'
    # The leg being retraced. Impulse still running (price beyond the last confirmed swing high):
    # last swing low → highest high since. Otherwise the pullback (and any bounce inside it) belongs
    # to the leg that made the last confirmed high: the swing low before it → that high. Mirror for a downtrend.
    ext_of = (lambda j: s.h[j]) if up else (lambda j: -s.l[j])
    tops, starts = (highs, lows) if up else (lows, highs)
    run_ext = max(range(starts[-1][0], last + 1), key=ext_of)
    if not tops or (tops[-1][0] < starts[-1][0] and ext_of(run_ext) > ext_of(tops[-1][0])):
        a_i, a_p, _ = starts[-1]                         # impulse still making new extremes
        b_i = run_ext
    elif tops[-1][0] > starts[-1][0]:
        a_i, a_p, _ = starts[-1]                         # pulling back from the last confirmed top
        b_i = tops[-1][0]
    else:
        b_i = tops[-1][0]                                # bouncing inside the pullback
        prior = [p for p in starts if p[0] < b_i]
        a_i, a_p, _ = prior[-1] if prior else starts[-1]
    b_p = s.h[b_i] if up else s.l[b_i]
    leg = abs(b_p - a_p) or atr
    lvl = lambda r: b_p - sgn * r * leg            # 0 = leg end (B), 1 = leg start (A)
    r_now = (b_p - price) / leg * sgn
    after = range(b_i + 1, last + 1)
    pb_i = (min(after, key=lambda j: s.l[j]) if up else max(after, key=lambda j: s.h[j])) if len(after) else None
    pb_p = (s.l[pb_i] if up else s.h[pb_i]) if pb_i is not None else b_p
    extreme = min(pb_p, price) if up else max(pb_p, price)
    r_deep = (b_p - extreme) / leg * sgn
    fib = {str(r): _r(symbol, lvl(r)) for r in FIB_LEVELS}
    fib.update({'1.272': _r(symbol, b_p + sgn * 0.272 * leg), '1.618': _r(symbol, b_p + sgn * 0.618 * leg)})
    out.update(side=side, retrace=round(r_now, 3), retrace_deepest=round(r_deep, 3),
               fib={'levels': fib, 'a': {'time': s.t[a_i], 'price': _r(symbol, a_p)},
                    'b': {'time': s.t[b_i], 'price': _r(symbol, b_p)},
                    'zone': [_r(symbol, lvl(ZONE[0])), _r(symbol, lvl(ZONE[1]))],
                    'golden': [_r(symbol, lvl(GOLDEN[0])), _r(symbol, lvl(GOLDEN[1]))]})

    # ── confirmation (trigger) on the main timeframe ──
    trig = {'found': False, 'level': None, 'how': None, 'bar_time': None}
    if pb_i is not None and r_deep >= ZONE[0]:
        inner = [p for p in (s.ph if up else s.pl) if b_i < p < pb_i and p <= last - 2]
        t_level = (s.h[inner[-1]] if up else s.l[inner[-1]]) if inner else (s.h[pb_i] if up else s.l[pb_i])
        trig['level'] = _r(symbol, t_level)
        for j in range(pb_i + 1, last + 1):
            if (s.c[j] - t_level) * sgn > 0:
                trig.update(found=True, how=f'{tf} close {"above" if up else "below"} the last {"lower high" if up else "higher low"} at {f(t_level)}', bar=j)
                break
        if not trig['found']:
            for j in (last - 1, last):
                name, d = candle_pattern(s, j)
                touched = (s.l[j] if up else s.h[j])
                if d == sgn and (b_p - touched) / leg * sgn >= ZONE[0]:
                    trig.update(found=True, how=f'{name} in the zone on the {tf}', bar=j)
                    break
        if not trig['found']:
            for j in range(pb_i + 1, last + 1):
                e0, e1 = s.ema20[j], s.ema20[j - 1]
                if e0 and e1 and (s.c[j] - e0) * sgn > 0 and (s.c[j - 1] - e1) * sgn <= 0:
                    trig.update(found=True, how=f'{tf} close back {"above" if up else "below"} the 20 EMA ({f(e0)}) after the pullback', bar=j)
                    break
        if trig['found']:
            trig['bar_time'] = s.t[trig.pop('bar')]
            trig['bars_ago'] = last - next(j for j in range(last + 1) if s.t[j] == trig['bar_time'])
    early = None
    if lower and pb_i is not None and r_deep >= ZONE[0] and not trig['found']:
        ls, ll = lower['series'], lower['last']
        idx = [j for j in range(ll + 1) if ls.t[j] >= s.t[b_i]]
        if len(idx) > 4:
            lpb = min(idx, key=lambda j: ls.l[j]) if up else max(idx, key=lambda j: ls.h[j])
            inner = [p for p in (ls.ph if up else ls.pl) if idx[0] < p < lpb and p <= ll - 2]
            if inner:
                lt = ls.h[inner[-1]] if up else ls.l[inner[-1]]
                if any((ls.c[j] - lt) * sgn > 0 for j in range(lpb + 1, ll + 1)):
                    early = {'tf': lower['tf'], 'level': _r(symbol, lt)}

    # ── state ──
    htf_against = htf_dir == -trend
    if r_now > 1:
        state = 'INVALID'
    elif r_deep < 0.236:
        state = 'EXTENDED'
    elif r_deep < ZONE[0]:
        state = 'WAIT_PULLBACK'
    elif trig['found']:
        state = 'READY' if r_now >= 0.2 and trig.get('bars_ago', 0) <= 6 else 'MISSED'
    elif r_now >= ZONE[0]:
        state = 'IN_ZONE'
    else:
        state = 'MISSED'

    # ── plan ──
    buf = 0.25 * atr
    if state == 'READY':
        entry, order = price, 'market'
        sl = pb_p - sgn * buf
    elif state == 'IN_ZONE' and trig['level'] is not None:
        entry, order = trig['level'] + sgn * 0.05 * atr, 'stop'
        sl = pb_p - sgn * buf
    else:
        entry, order = lvl(0.618), 'limit'
        if (price - entry) * sgn <= 0:
            entry = price
        sl = a_p - sgn * buf
    if abs(entry - sl) < 0.6 * atr:
        sl = entry - sgn * 0.6 * atr
    risk = abs(entry - sl)
    tp1 = b_p if (b_p - entry) * sgn >= 1.0 * risk else b_p + sgn * 0.272 * leg
    tp2 = b_p + sgn * 0.618 * leg
    if (tp2 - tp1) * sgn < 0.5 * risk:
        tp2 = tp1 + sgn * risk
    rr = abs(tp1 - entry) / risk if risk else 0
    lv = main['levels']['levels']
    blockers = [z for z in lv if z['touches'] >= 2 and 0.3 * atr < (z['price'] - entry) * sgn < (tp1 - entry) * sgn - 0.3 * atr]
    block = (min(blockers, key=lambda z: abs(z['price'] - entry)) if blockers else None)
    out['plan'] = {'order': order, 'side': side, 'entry': _r(symbol, entry), 'sl': _r(symbol, sl),
                   'tp1': _r(symbol, tp1), 'tp2': _r(symbol, tp2), 'rr': round(rr, 2),
                   'invalid': _r(symbol, a_p), 'risk_atr': round(risk / atr, 2)}
    out['trigger'] = trig
    out['early'] = early

    # ── checklist ──
    if higher:
        if htf_dir == trend:
            check('htf', f'{htf_text} trend', 'pass', f'The {htf_text} is also in an {word}: you trade with the bigger picture.', 3)
        elif htf_against:
            check('htf', f'{htf_text} trend', 'fail', f'The {htf_text} trend points the other way. This pullback may be the start of a bigger reversal.', 3)
        else:
            check('htf', f'{htf_text} trend', 'warn', f'The {htf_text} has no clear trend. Fine, but targets may be harder to reach.', 3)
    sw = out['swings']
    pair = ' / '.join(x['label'] for x in sw[-4:])
    check('structure', 'Market structure', 'pass',
          f"{tf} makes {'higher highs and higher lows' if up else 'lower highs and lower lows'} ({pair}).", 3)
    pct = f'{r_now * 100:.0f}%'
    if state == 'INVALID':
        check('location', 'Retracement', 'fail', f'Price went past the start of the leg ({f(a_p)}), so the {word} structure is broken.', 3)
    elif r_deep < 0.236:
        check('location', 'Retracement', 'fail', f'Only {pct} retraced: price is still at the extreme of the move. Buying here means chasing.' if up
              else f'Only {pct} retraced: price is still at the extreme of the move. Selling here means chasing.', 3)
    elif r_deep < ZONE[0]:
        check('location', 'Retracement', 'warn', f'{pct} retraced. A shallow pullback; the entry zone starts at 38.2% ({f(lvl(0.382))}).', 3)
    elif r_now > ZONE[1]:
        check('location', 'Retracement', 'warn', f'{pct} retraced: a deep pullback. Still valid until {f(a_p)} breaks, but buyers are weaker.' if up
              else f'{pct} retraced: a deep pullback. Still valid until {f(a_p)} breaks, but sellers are weaker.', 3)
    else:
        golden = GOLDEN[0] <= r_deep <= GOLDEN[1] + 0.05
        check('location', 'Retracement', 'pass', f'Pullback reached {r_deep * 100:.0f}% of the leg' + (' (golden zone 50–61.8%).' if golden else ', inside the 38.2–78.6% entry zone.'), 3)
    zlo, zhi = sorted((lvl(ZONE[0]), lvl(ZONE[1])))
    conf = []
    for z in lv:
        if z['touches'] >= 2 and zlo - 0.25 * atr <= z['price'] <= zhi + 0.25 * atr:
            conf.append(f"{z['touches']}-touch {'support' if up else 'resistance'} at {f(z['price'])}")
    old = [p for p in (highs if up else lows) if p[0] < a_i]
    if old and zlo - 0.25 * atr <= old[-1][1] <= zhi + 0.25 * atr:
        conf.append(f"old swing {'high' if up else 'low'} {f(old[-1][1])} (broken level, now {'support' if up else 'resistance'})")
    for name, series in (('EMA 50', s.ema50), ('EMA 20', s.ema20)):
        v = series[last]
        if v and zlo - 0.25 * atr <= v <= zhi + 0.25 * atr:
            conf.append(f'{name} at {f(v)}')
    if conf:
        check('confluence', 'Zone confluence', 'pass', 'The zone lines up with ' + ', '.join(conf[:3]) + '.', 2)
    else:
        check('confluence', 'Zone confluence', 'warn', 'No support level or EMA inside the zone: Fibonacci alone is a weaker reason.', 2)
    if trig['found']:
        check('trigger', 'Confirmation', 'pass', trig['how'][0].upper() + trig['how'][1:] + '.', 3)
    elif state in ('IN_ZONE', 'MISSED') and trig['level'] is not None:
        check('trigger', 'Confirmation', 'warn',
              f"Not yet. Wait for a {tf} close {'above' if up else 'below'} {f(trig['level'])} or a {'bullish' if up else 'bearish'} engulfing / pin bar in the zone.", 3)
    else:
        check('trigger', 'Confirmation', 'warn', 'Comes after the pullback reaches the zone.', 3)
    if early:
        check('early', f"{early['tf'].upper()} early signal", 'pass',
              f"The {early['tf'].upper()} already broke {'above' if up else 'below'} {f(early['level'])}: an aggressive entry is possible before the {tf} confirms.", 1)
    pr = [s.rsi[j] for j in range(b_i, last + 1) if s.rsi[j] is not None]
    r_last = s.rsi[last]
    if pr and r_last is not None:
        cooled = min(pr) < 50 if up else max(pr) > 50
        stretched = r_last > 70 if up else r_last < 30
        if stretched:
            check('momentum', 'Momentum (RSI)', 'warn', f'RSI {r_last:.0f}: stretched, a late entry.', 1)
        elif cooled:
            check('momentum', 'Momentum (RSI)', 'pass', f"RSI cooled to {min(pr) if up else max(pr):.0f} in the pullback and is {r_last:.0f} now: momentum has reset.", 1)
        else:
            check('momentum', 'Momentum (RSI)', 'warn', f'RSI {r_last:.0f}: the pullback has not cooled momentum yet.', 1)
    d, txt = divergence(s, last)
    if d == sgn:
        check('divergence', 'Divergence', 'pass', txt + ' at the pullback.', 1)
    elif d == -sgn:
        check('divergence', 'Divergence', 'fail', txt + ': momentum is fading in your direction.', 1)
    if rr >= 2:
        check('rr', 'Reward : risk', 'pass', f'{rr:.1f}R to the first target from {f(entry)}.', 2)
    elif rr >= 1.5:
        check('rr', 'Reward : risk', 'warn', f'{rr:.1f}R to the first target. Acceptable, not great.', 2)
    else:
        check('rr', 'Reward : risk', 'fail', f'Only {rr:.1f}R to the first target from {f(entry)}: not worth the risk.', 2)
    if block:
        check('room', 'Room to target', 'warn', f"{block['touches']}-touch {'resistance' if up else 'support'} at {f(block['price'])} sits before the target. Consider taking profit there.", 1)
    else:
        check('room', 'Room to target', 'pass', 'No strong level between entry and the first target.', 1)
    a_val = s.adx[last]
    if a_val is not None:
        check('adx', 'Trend strength (ADX)', 'pass' if a_val >= 20 else 'warn',
              f'ADX {a_val:.0f}: ' + ('a real trend.' if a_val >= 25 else 'a moderate trend.' if a_val >= 20 else 'weak. Pullbacks can turn into ranges.'), 1)
    hist_atr = sorted(v for v in s.atr[max(0, last - 100):last + 1] if v)
    if hist_atr:
        ratio = atr / hist_atr[len(hist_atr) // 2]
        check('volatility', 'Volatility', 'pass' if 0.6 <= ratio <= 1.8 else 'warn',
              f'ATR {f(atr)} is {ratio:.1f}× its usual size' + ('.' if 0.6 <= ratio <= 1.8 else
              (': very quiet, moves may stall.' if ratio < 0.6 else ': unusually wild, widen the stop or reduce size.')), 1)

    if state == 'READY' and htf_against:
        state = 'IN_ZONE'
        check('htf_block', 'Entry blocked', 'fail', f'The trigger fired, but the {htf_text} trend is against it. Skipping is the disciplined choice.', 0)
    if state == 'READY' and rr < 1.2:
        state = 'MISSED'
    out['state'] = state
    out['headline'] = STATE_TEXT[state]
    w_total = sum(c['weight'] for c in checks if c['status'] != 'info')
    w_got = sum(c['weight'] * (1 if c['status'] == 'pass' else 0.4 if c['status'] == 'warn' else 0) for c in checks)
    out['quality'] = int(round(100 * w_got / w_total)) if w_total else 0
    out['checks'] = checks

    # ── what to do now, in plain words ──
    zone_txt = f'{f(lvl(GOLDEN[0]))}–{f(lvl(GOLDEN[1]))}'
    dist = abs(price - lvl(0.5)) / atr
    buy = 'buy' if up else 'sell'
    instr = {
        'EXTENDED': f'Do not {buy} here. Wait for price to pull back to {zone_txt} (about {dist:.1f} ATR away), then for confirmation.',
        'WAIT_PULLBACK': f'Pullback under way ({pct}). The best zone is {zone_txt}, about {dist:.1f} ATR away. Set an alert there, or a {buy} limit at {f(lvl(0.618))}.',
        'IN_ZONE': (f"Price is in the zone. Wait for a {tf} close {'above' if up else 'below'} {f(trig['level'])}" if trig['level'] is not None else 'Price is in the zone. Wait for a rejection candle')
                   + f", or place a {buy} stop at {f(entry)} with the stop at {f(sl)}.",
        'READY': f"{buy.capitalize()} now near {f(price)}. Stop {f(sl)}, target {f(tp1)} ({rr:.1f}R), then {f(tp2)}.",
        'MISSED': f'Price bounced without you or the reward is too small. Wait for the next pullback toward {zone_txt}.',
        'INVALID': f'Price broke {f(a_p)}, the start of the leg. The {word} is in question: wait for new structure.',
    }[state]
    out['instruction'] = instr
    story = [f"The {tf} is in an {word}: {'higher highs and higher lows' if up else 'lower highs and lower lows'}. "
             f"The last leg ran from {f(a_p)} to {f(b_p)} ({leg / atr:.1f} ATR)."
             + (f" The {htf_text} agrees." if higher and htf_dir == trend else f" The {htf_text} disagrees, so be careful." if htf_against else '')]
    if state == 'EXTENDED':
        story.append(f'Price is still near the {"high" if up else "low"} of that leg ({pct} retraced). After a strong move, markets usually pull back before continuing, '
                     f'and the best entries are in the 50–61.8% zone ({zone_txt}), with a stop beyond {f(a_p)}.')
    elif state == 'WAIT_PULLBACK':
        story.append(f'Since the {"top" if up else "bottom"} at {f(b_p)}, price has retraced {pct} of the leg. That is still shallow: '
                     f'the coach waits for 38.2–61.8% ({f(lvl(0.382))} to {f(lvl(0.618))}) where the risk is small compared with the target at {f(b_p)}.')
    elif state in ('IN_ZONE', 'READY', 'MISSED'):
        story.append(f'The pullback reached {r_deep * 100:.0f}% ({f(pb_p)}) and price is at {pct} now. '
                     + (f'Confluence: {", ".join(conf[:2])}. ' if conf else '')
                     + ('Confirmation came: ' + trig['how'] + '.' if trig['found'] else
                        'No confirmation yet: price can keep falling through the zone, so the coach waits for buyers to prove themselves.' if up else
                        'No confirmation yet: price can keep rising through the zone, so the coach waits for sellers to prove themselves.'))
    elif state == 'INVALID':
        story.append(f'Price has moved beyond {f(a_p)}, where the leg started, so this was not a pullback but a reversal. Any new trade needs fresh structure.')
    if state != 'INVALID':
        story.append(f"Plan: {buy} {'at market' if order == 'market' else ('on a stop at ' if order == 'stop' else 'with a limit at ') + f(entry)}, stop {f(sl)} "
                     f"(beyond the {'pullback low' if order != 'limit' else 'leg start'} + 0.25 ATR), target 1 {f(tp1)}, target 2 {f(tp2)}. "
                     f"Reward:risk {rr:.1f}. The idea is wrong if price closes {'below' if up else 'above'} {f(a_p)}.")
    out['story'] = story
    return out


def analyse(symbol, style='intraday', detail=False):
    symbol = symbol.upper()
    style = style if style in STYLES else 'intraday'
    key = result_key(symbol, style, detail)
    hit = cache.get(key)
    if hit is not None:
        return hit
    lower_tf, main_tf, higher_tf = STYLES[style]
    main = analyse_tf(symbol, main_tf, train=True)
    if not main:
        res = {'symbol': symbol, 'ok': False, 'error': 'Not enough price history from the data feeds'}
        cache.set(key, res, 60)
        return res
    higher = analyse_tf(symbol, higher_tf)
    lower = analyse_tf(symbol, lower_tf) if detail else None

    q = mt5.cached_quote(symbol)
    price = float(q.bid) if q else main['series'].c[-1]

    model = main['model']
    prob = model.get('prob_up')
    model_score = (prob - 0.5) * 200 if prob is not None else 0
    hs = higher['score'] if higher else 0
    blended = 0.55 * main['score'] + 0.30 * hs + 0.15 * model_score
    if not higher:
        blended = 0.8 * main['score'] + 0.2 * model_score

    if blended >= SIGNAL_THRESHOLD and hs > -SIGNAL_THRESHOLD:
        signal = 'BUY'
    elif blended <= -SIGNAL_THRESHOLD and hs < SIGNAL_THRESHOLD:
        signal = 'SELL'
    else:
        signal = 'WAIT'
    agree = sum(1 for x in (main['score'], hs, model_score) if (x > 0) == (blended > 0) and abs(x) >= 10)
    confidence = int(round(min(95, 40 + abs(blended) * 0.45 + agree * 5)))

    reasons = sorted(main['reasons'], key=lambda r: -abs(r['impact']))
    if higher:
        reasons.insert(0, {'key': 'htf', 'impact': round(hs / 5, 1),
                           'text': f"{higher_tf.upper()} trend is {'bullish' if hs > 15 else 'bearish' if hs < -15 else 'neutral'} ({hs:+.0f})"})
    if prob is not None:
        reasons.insert(1, {'key': 'model', 'impact': round(model_score / 5, 1),
                           'text': f"Model: {prob * 100:.0f}% chance price moves up 1.5 ATR before down 1.5 ATR"})

    bt = main['backtest']

    # Compact scanner summary — mirrors what ai_prediction_models.html shows,
    # but computed on the server from real candles.
    s, i = main['series'], main['last']
    pattern_name, pattern_dir = candle_pattern(s, i)
    lv_main = main['levels']
    scanners_summary = {
        'patterns': {
            'name': pattern_name,
            'bias': 'BULLISH' if pattern_dir > 0 else 'BEARISH' if pattern_dir < 0 else 'NEUTRAL',
        },
        'levels': {
            'support': _r(symbol, lv_main['support']) if lv_main.get('support') else None,
            'resistance': _r(symbol, lv_main['resistance']) if lv_main.get('resistance') else None,
        },
        'mtf': {
            'alignment': round(abs(main['score']), 0),
            'direction': 'UP' if main['score'] > 0 else 'DOWN',
        },
        'divergence': next((r['text'] for r in main['reasons'] if r['key'] == 'divergence'), None),
        'breakout': next((r['text'] for r in main['reasons'] if r['key'] == 'breakout'), None),
        'rsi': round(s.rsi[i], 1),
        'adx': round(s.adx[i], 1),
    }

    res = {
        'symbol': symbol, 'ok': True, 'style': style,
        'name': mt5.SPECS[symbol].description if symbol in mt5.SPECS else symbol,
        'kind': mt5.SPECS[symbol].kind if symbol in mt5.SPECS else 'other',
        'price': _r(symbol, price), 'signal': signal, 'score': round(blended, 1),
        'confidence': confidence,
        'timeframes': [{'tf': t['tf'], 'score': round(t['score'], 1)} for t in (lower, main, higher) if t],
        'model': {k: (round(v, 4) if isinstance(v, float) else v) for k, v in model.items() if k != 'weights'},
        'backtest': {k: (None if isinstance(v, float) and not math.isfinite(v) else (round(v, 3) if isinstance(v, float) else v)) for k, v in bt.items()},
        'reasons': reasons[:8] if detail else reasons[:3],
        'plan': trade_plan(symbol, signal, main, price) if signal != 'WAIT' else None,
        'pattern': next((r['text'] for r in main['reasons'] if r['key'] == 'pattern'), None),
        'rsi': round(main['series'].rsi[main['last']], 1),
        'adx': round(main['series'].adx[main['last']], 1),
        'source': main['source'], 'main_tf': main_tf,
        'analysed_at': int(time.time()),
        'scanners': scanners_summary,
    }
    if signal == 'WAIT':
        lean = 'BUY' if blended > 0 else 'SELL'
        res['lean'] = lean
        res['watch'] = trade_plan(symbol, lean, main, price)
    res['engine'] = ENGINE_VERSION
    try:
        res['coach'] = entry_coach(symbol, main, higher, lower, price)
    except Exception as e:
        logger.exception(f'Entry coach failed for {symbol}: {e}')
        res['coach'] = None
        res['coach_error'] = f'{type(e).__name__}: {e}'[:200]
    if detail:
        res['charts'] = {t['tf']: chart_payload(symbol, t) for t in (lower, main, higher) if t}
    cache.set(key, res, CANDLE_TTL.get(main_tf, 300))
    return res


def chart_payload(symbol, t, bars=220):
    s = t['series']
    start = max(0, s.n - bars)
    rng = range(start, s.n)
    lv = t['levels']['levels']
    price = s.c[t['last']]
    atr = s.atr[t['last']]
    strong = [z for z in lv if z['touches'] >= 2] or lv
    near = sorted(strong, key=lambda z: abs(z['price'] - price))[:5]
    markers = []
    for p in s.ph:
        if p >= start and p <= s.n - 3:
            markers.append({'time': s.t[p], 'position': 'aboveBar', 'shape': 'circle', 'color': '#ea3943', 'size': 0.4})
    for p in s.pl:
        if p >= start and p <= s.n - 3:
            markers.append({'time': s.t[p], 'position': 'belowBar', 'shape': 'circle', 'color': '#16c784', 'size': 0.4})
    name, d = candle_pattern(s, t['last'])
    if name:
        markers.append({'time': s.t[t['last']], 'position': 'belowBar' if d >= 0 else 'aboveBar',
                        'shape': 'arrowUp' if d > 0 else 'arrowDown' if d < 0 else 'square',
                        'color': '#16c784' if d > 0 else '#ea3943' if d < 0 else '#8b93a7', 'text': name.split(' /')[0]})
    markers.sort(key=lambda m: m['time'])
    return {
        'tf': t['tf'], 'score': round(t['score'], 1),
        'candles': [{'time': s.t[i], 'open': s.o[i], 'high': s.h[i], 'low': s.l[i], 'close': s.c[i]} for i in rng],
        'ema20': [{'time': s.t[i], 'value': s.ema20[i]} for i in rng if s.ema20[i] is not None],
        'ema50': [{'time': s.t[i], 'value': s.ema50[i]} for i in rng if s.ema50[i] is not None],
        'levels': [{'price': _r(symbol, z['price']), 'touches': z['touches'],
                    'kind': 'resistance' if z['price'] > price else 'support'} for z in near if abs(z['price'] - price) < atr * 8],
        'markers': markers,
    }


def scan(symbols, style='intraday'):
    symbols = [s for s in symbols if s in SCAN_SYMBOLS] or SCAN_SYMBOLS
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda s: _safe(s, style), symbols))
    return [r for r in results if r]


def _safe(symbol, style):
    try:
        return analyse(symbol, style)
    except Exception as e:
        logger.exception(f'AI scan failed for {symbol}: {e}')
        return {'symbol': symbol, 'ok': False, 'error': 'Analysis failed'}

# ═══════════════════════════════════════════════════════════════
# SCRUTINY — every strategy a trader uses, each voting on one market
# ═══════════════════════════════════════════════════════════════
# Each strategy returns a vote in [-1, 1] (sell … buy), a confidence and a plain-English
# reason, plus the levels it looked at. Nothing here predicts with certainty: it lays out
# what each school of analysis sees on the same closed candles so the trader can judge
# agreement and conflict. Fundamentals are added by the view (they need the calendar).
STRATEGY_META = {
    # key: (label, group, weight, icon)
    'trend':        ('Trend following', 'Trend & structure', 1.2, 'fa-arrow-trend-up'),
    'mtf':          ('Multi-timeframe', 'Trend & structure', 1.2, 'fa-layer-group'),
    'structure':    ('Market structure (BOS / CHoCH)', 'Trend & structure', 1.0, 'fa-stairs'),
    'pullback':     ('Pullback entry (Fibonacci)', 'Trend & structure', 1.0, 'fa-ruler-combined'),
    'liquidity':    ('Liquidity (pools & sweeps)', 'Smart money & liquidity', 1.0, 'fa-droplet'),
    'orderblocks':  ('Order blocks', 'Smart money & liquidity', 0.8, 'fa-cubes'),
    'fvg':          ('Fair value gaps', 'Smart money & liquidity', 0.5, 'fa-arrows-up-down'),
    'premium':      ('Premium / discount', 'Smart money & liquidity', 0.5, 'fa-scale-balanced'),
    'sr':           ('Support & resistance', 'Price action', 0.8, 'fa-grip-lines'),
    'price_action': ('Candlestick patterns', 'Price action', 0.6, 'fa-chart-simple'),
    'session':      ('Session breakout', 'Price action', 0.4, 'fa-clock'),
    'momentum':     ('Momentum (MACD / RSI / ROC)', 'Oscillators', 0.8, 'fa-gauge-high'),
    'mean_rev':     ('Mean reversion', 'Oscillators', 0.6, 'fa-rotate-left'),
    'divergence':   ('Divergence', 'Oscillators', 0.6, 'fa-code-branch'),
    'volatility':   ('Volatility regime', 'Oscillators', 0.3, 'fa-wave-square'),
    'volume':       ('Volume', 'Oscillators', 0.4, 'fa-chart-column'),
    'model':        ('Trained model', 'Model & fundamentals', 0.8, 'fa-brain'),
    'fundamentals': ('Fundamentals (news surprises)', 'Model & fundamentals', 1.0, 'fa-landmark'),
}


def _vote(key, vote, text, conf=0.6, details=None, levels=None):
    lab, group, weight, icon = STRATEGY_META[key]
    return {'key': key, 'name': lab, 'group': group, 'weight': weight, 'icon': icon,
            'vote': None if vote is None else round(clamp(vote), 2), 'conf': round(conf, 2),
            'text': text, 'details': details or [], 'levels': levels or []}


def _day_key(ts):
    """Forex trading day of a bar (rolls over at 17:00 New York ≈ 21:00/22:00 UTC; 21:00 UTC used)."""
    return int((ts + 3 * 3600) // 86400)


def _session_levels(symbol, s, last):
    """Previous-day high/low and today's Asian range from one series' closed bars."""
    out = {}
    days = {}
    for j in range(max(0, last - 600), last + 1):
        k = _day_key(s.t[j])
        d = days.setdefault(k, [s.h[j], s.l[j]])
        d[0] = max(d[0], s.h[j]); d[1] = min(d[1], s.l[j])
    keys = sorted(days)
    if len(keys) >= 2:
        out['pdh'], out['pdl'] = days[keys[-2]]
    today = int(s.t[last] // 86400)
    asia = [j for j in range(max(0, last - 300), last + 1) if int(s.t[j] // 86400) == today and (s.t[j] % 86400) < 7 * 3600]
    if len(asia) >= 2:
        out['asia_hi'] = max(s.h[j] for j in asia)
        out['asia_lo'] = min(s.l[j] for j in asia)
    return out


def _equal_levels(s, last, atr, side, lookback=150):
    """Equal highs / lows (≥ 2 swing points within 0.15 ATR) that price has not run through yet = resting liquidity."""
    piv = [p for p in (s.ph if side == 'H' else s.pl) if last - lookback <= p <= last - 2]
    vals = [(p, s.h[p] if side == 'H' else s.l[p]) for p in piv]
    pools = []
    used = set()
    for a in range(len(vals)):
        if a in used:
            continue
        grp = [vals[a]]
        for b in range(a + 1, len(vals)):
            if b not in used and abs(vals[b][1] - vals[a][1]) <= 0.15 * atr:
                grp.append(vals[b]); used.add(b)
        if len(grp) >= 2:
            lvl = max(v for _, v in grp) if side == 'H' else min(v for _, v in grp)
            after = range(max(p for p, _ in grp) + 1, last + 1)
            taken = any(s.h[j] > lvl for j in after) if side == 'H' else any(s.l[j] < lvl for j in after)
            if not taken:
                pools.append({'price': lvl, 'touches': len(grp), 'time': s.t[max(p for p, _ in grp)]})
    return pools


def _order_blocks(s, last, atr, lookback=120):
    """Last opposite candle before a displacement (≥ 2 ATR move in 3 bars) that is still unmitigated."""
    obs = []
    for k in range(max(2, last - lookback), last - 3):
        bull_disp = s.c[k + 3] - s.l[k] >= 2 * atr and s.c[k + 1] > s.h[k]
        bear_disp = s.h[k] - s.c[k + 3] >= 2 * atr and s.c[k + 1] < s.l[k]
        if s.c[k] < s.o[k] and bull_disp:
            lo, hi = s.l[k], max(s.o[k], s.c[k])
            if not any(s.c[j] < lo for j in range(k + 4, last + 1)):
                obs.append({'side': 'bull', 'lo': lo, 'hi': hi, 'time': s.t[k]})
        if s.c[k] > s.o[k] and bear_disp:
            lo, hi = min(s.o[k], s.c[k]), s.h[k]
            if not any(s.c[j] > hi for j in range(k + 4, last + 1)):
                obs.append({'side': 'bear', 'lo': lo, 'hi': hi, 'time': s.t[k]})
    return obs


def _fvgs(s, last, atr, lookback=120):
    """Three-candle imbalances bigger than 0.2 ATR that price has not fully refilled."""
    gaps = []
    for k in range(max(1, last - lookback), last):
        if s.l[k + 1] - s.h[k - 1] > 0.2 * atr:                      # bullish gap
            lo, hi = s.h[k - 1], s.l[k + 1]
            if not any(s.l[j] <= lo for j in range(k + 2, last + 1)):
                gaps.append({'side': 'bull', 'lo': lo, 'hi': hi, 'time': s.t[k]})
        if s.l[k - 1] - s.h[k + 1] > 0.2 * atr:                      # bearish gap
            lo, hi = s.h[k + 1], s.l[k - 1]
            if not any(s.h[j] >= hi for j in range(k + 2, last + 1)):
                gaps.append({'side': 'bear', 'lo': lo, 'hi': hi, 'time': s.t[k]})
    return gaps


def scrutiny(symbol, style='intraday'):
    """Every strategy's read of one market (cached per candle). Fundamentals and the verdict are added by the caller."""
    symbol = symbol.upper()
    style = style if style in STYLES else 'intraday'
    key = f'aiscan:scr:{ENGINE_VERSION}:{symbol}:{style}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    lower_tf, main_tf, higher_tf = STYLES[style]
    main = analyse_tf(symbol, main_tf)
    if not main:
        return {'ok': False, 'error': 'Not enough price history from the data feeds'}
    higher = analyse_tf(symbol, higher_tf)
    lower = analyse_tf(symbol, lower_tf)
    s, i = main['series'], main['last']
    q = mt5.cached_quote(symbol)
    price = float(q.bid) if q else s.c[i]
    atr = s.atr[i]
    f = lambda v: _fmt(symbol, v)
    out, liq = [], []

    def lvl(price_, label, kind):
        liq.append({'price': _r(symbol, price_), 'label': label, 'kind': kind,
                    'side': 'above' if price_ > price else 'below', 'dist_atr': round((price_ - price) / atr, 2)})

    # 1. trend following — EMA stack, slope, ADX direction
    e20, e50, e200 = s.ema20[i], s.ema50[i], s.ema200[i]
    stack = (1 if e20 > e50 else -1) + (1 if e50 > (e200 or e50) else -1 if e200 else 0) + (1 if s.c[i] > e50 else -1)
    slope = (s.ema50[i] - s.ema50[i - 10]) / atr if s.ema50[i - 10] else 0
    a_val, pdi, mdi = s.adx[i], s.pdi[i], s.mdi[i]
    v = stack / 3 * (1.0 if a_val >= 25 else 0.75 if a_val >= 20 else 0.45)
    out.append(_vote('trend', v,
        f"EMA 20 {'above' if e20 > e50 else 'below'} 50" + (f", 50 {'above' if e50 > e200 else 'below'} 200" if e200 else '')
        + f", price {'above' if s.c[i] > e50 else 'below'} the 50. ADX {a_val:.0f} ({'+DI leads' if pdi > mdi else '-DI leads'}): "
        + ('a strong trend.' if a_val >= 25 else 'a moderate trend.' if a_val >= 20 else 'weak, trend signals are discounted.'),
        conf=min(1, a_val / 35), details=[f'EMA 50 slope {slope:+.2f} ATR over 10 bars'],
        levels=[{'price': _r(symbol, e50), 'label': 'EMA 50'}] + ([{'price': _r(symbol, e200), 'label': 'EMA 200'}] if e200 else [])))

    # 2. multi-timeframe
    hs = higher['score'] if higher else 0
    ls = lower['score'] if lower else 0
    v = (main['score'] + 1.5 * hs + 0.5 * ls) / 300
    agree = sum(1 for x in (main['score'], hs, ls) if abs(x) >= 15 and (x > 0) == (v > 0))
    out.append(_vote('mtf', v,
        f"{main_tf.upper()} {main['score']:+.0f}, {higher_tf.upper()} {hs:+.0f}, {lower_tf.upper()} {ls:+.0f}: "
        + (f'{agree} of 3 timeframes agree.' if agree else 'the timeframes disagree.'), conf=agree / 3))

    # 3. market structure — break of structure / change of character
    piv, run = zigzag(s, i)
    trend = swing_trend(piv, run)
    highs = [p for p in piv if p[2] == 'H']
    lows = [p for p in piv if p[2] == 'L']
    v, txt = 0.0, 'Not enough swings to read structure.'
    if highs and lows:
        lh, ll = highs[-1][1], lows[-1][1]
        if s.c[i] > lh:
            v, txt = (0.8, f'Break of structure up: closed above the last swing high {f(lh)} in an uptrend.') if trend >= 0 else \
                     (0.6, f'Change of character: a downtrend just closed above its last lower high {f(lh)}, an early reversal sign.')
        elif s.c[i] < ll:
            v, txt = (-0.8, f'Break of structure down: closed below the last swing low {f(ll)} in a downtrend.') if trend <= 0 else \
                     (-0.6, f'Change of character: an uptrend just closed below its last higher low {f(ll)}, an early reversal sign.')
        else:
            v = 0.5 * trend
            txt = (f"{'Higher highs and higher lows' if trend > 0 else 'Lower highs and lower lows' if trend < 0 else 'Mixed swings (range)'}. "
                   f"Range to break: {f(ll)} – {f(lh)}.")
        lvl(lh, 'Swing high', 'swing'); lvl(ll, 'Swing low', 'swing')
    out.append(_vote('structure', v, txt, conf=0.7))

    # 4. pullback / Fibonacci (the Market Overview entry coach)
    coach = None
    try:
        coach = entry_coach(symbol, main, higher, lower, price)
    except Exception as e:
        logger.warning(f'scrutiny coach {symbol}: {e}')
    if coach and coach.get('side'):
        sgn = 1 if coach['side'] == 'BUY' else -1
        w = {'READY': 1.0, 'IN_ZONE': 0.6, 'WAIT_PULLBACK': 0.35, 'EXTENDED': 0.15, 'MISSED': 0.2, 'INVALID': -0.3}.get(coach['state'], 0)
        out.append(_vote('pullback', sgn * w, coach['instruction'], conf=coach['quality'] / 100,
                         levels=[{'price': coach['fib']['golden'][0], 'label': '50%'}, {'price': coach['fib']['golden'][1], 'label': '61.8%'}] if coach.get('fib') else []))
        if coach.get('fib'):
            z = coach['fib']['zone']
            lvl(z[0], '38.2% (zone)', 'fib'); lvl(z[1], '78.6% (zone)', 'fib')
    else:
        out.append(_vote('pullback', 0, coach['instruction'] if coach else 'No trend to pull back into.', conf=0.3))

    # 5. liquidity — equal highs/lows, previous day, Asian range, sweeps
    pools_h = _equal_levels(s, i, atr, 'H')
    pools_l = _equal_levels(s, i, atr, 'L')
    sess = _session_levels(symbol, lower['series'] if lower else s, lower['last'] if lower else i)
    for p in pools_h:
        lvl(p['price'], f"Equal highs ×{p['touches']} (buy stops)", 'liquidity')
    for p in pools_l:
        lvl(p['price'], f"Equal lows ×{p['touches']} (sell stops)", 'liquidity')
    if 'pdh' in sess:
        lvl(sess['pdh'], 'Previous day high', 'liquidity'); lvl(sess['pdl'], 'Previous day low', 'liquidity')
    if 'asia_hi' in sess:
        lvl(sess['asia_hi'], 'Asian session high', 'session'); lvl(sess['asia_lo'], 'Asian session low', 'session')
    sweep = None
    recent_h = [p for p in s.ph if i - 60 <= p <= i - 3]
    recent_l = [p for p in s.pl if i - 60 <= p <= i - 3]
    for j in range(i - 4, i + 1):
        for p in recent_h:
            if p < j and s.h[j] > s.h[p] > s.c[j] and max(s.h[p + 1:j], default=s.h[p]) <= s.h[p]:
                sweep = (-1, f"Liquidity sweep: the {main_tf.upper()} wicked above the swing high {f(s.h[p])} and closed back below. Buy stops were taken; sellers stepped in.")
        for p in recent_l:
            if p < j and s.l[j] < s.l[p] < s.c[j] and min(s.l[p + 1:j], default=s.l[p]) >= s.l[p]:
                sweep = (1, f"Liquidity sweep: the {main_tf.upper()} wicked below the swing low {f(s.l[p])} and closed back above. Sell stops were taken; buyers stepped in.")
    above = sorted([x for x in liq if x['kind'] == 'liquidity' and x['side'] == 'above'], key=lambda x: x['dist_atr'])
    below = sorted([x for x in liq if x['kind'] == 'liquidity' and x['side'] == 'below'], key=lambda x: -x['dist_atr'])
    if sweep:
        v, txt = 0.75 * sweep[0], sweep[1]
    elif above or below:
        na = above[0]['dist_atr'] if above else 99
        nb = -below[0]['dist_atr'] if below else 99
        v = (0.25 if na < nb else -0.25) * (1 if min(na, nb) <= 4 else 0.4)
        tgt = above[0] if na < nb else below[0]
        txt = (f"No sweep yet. Nearest resting liquidity: {tgt['label'].lower()} at {f(tgt['price'])} "
               f"({abs(tgt['dist_atr']):.1f} ATR {'above' if na < nb else 'below'}). Price tends to be drawn to it.")
    else:
        v, txt = 0, 'No clear liquidity pools nearby.'
    out.append(_vote('liquidity', v, txt, conf=0.8 if sweep else 0.4,
                     details=[f"{len(pools_h)} equal-high pool(s) above, {len(pools_l)} equal-low pool(s) below"]))

    # 6. order blocks
    obs = _order_blocks(s, i, atr)
    near = [o for o in obs if o['lo'] - 0.3 * atr <= price <= o['hi'] + 0.3 * atr]
    for o in sorted(obs, key=lambda o: abs((o['lo'] + o['hi']) / 2 - price))[:4]:
        lvl((o['lo'] + o['hi']) / 2, f"{'Bullish' if o['side'] == 'bull' else 'Bearish'} order block {f(o['lo'])}–{f(o['hi'])}", 'orderblock')
    if near:
        o = near[-1]
        v = 0.6 if o['side'] == 'bull' else -0.6
        txt = f"Price is at an unmitigated {'bullish' if v > 0 else 'bearish'} order block ({f(o['lo'])}–{f(o['hi'])}), where institutions last {'bought' if v > 0 else 'sold'} before a strong move."
    else:
        v, txt = 0, (f'{len(obs)} unmitigated order block(s) on the chart, none at the current price.' if obs else 'No unmitigated order blocks.')
    out.append(_vote('orderblocks', v, txt, conf=0.6 if near else 0.3))

    # 7. fair value gaps
    gaps = _fvgs(s, i, atr)
    inside = [g for g in gaps if g['lo'] - 0.2 * atr <= price <= g['hi'] + 0.2 * atr]
    for g in sorted(gaps, key=lambda g: abs((g['lo'] + g['hi']) / 2 - price))[:4]:
        lvl((g['lo'] + g['hi']) / 2, f"{'Bullish' if g['side'] == 'bull' else 'Bearish'} FVG {f(g['lo'])}–{f(g['hi'])}", 'fvg')
    if inside:
        g = inside[-1]
        v = 0.4 if g['side'] == 'bull' else -0.4
        txt = f"Price is rebalancing inside a {'bullish' if v > 0 else 'bearish'} fair value gap ({f(g['lo'])}–{f(g['hi'])}); these often act as {'support' if v > 0 else 'resistance'}."
    else:
        v, txt = 0, (f'{len(gaps)} unfilled gap(s); price tends to come back to fill them.' if gaps else 'No unfilled fair value gaps.')
    out.append(_vote('fvg', v, txt, conf=0.4))

    # 8. premium / discount of the dealing range
    rng_hi = max(s.h[max(0, i - 100):i + 1]); rng_lo = min(s.l[max(0, i - 100):i + 1])
    if highs and lows:
        rng_hi, rng_lo = max(highs[-1][1], run[1] if run and run[2] == 'H' else highs[-1][1]), min(lows[-1][1], run[1] if run and run[2] == 'L' else lows[-1][1])
    pos = (price - rng_lo) / (rng_hi - rng_lo) if rng_hi > rng_lo else 0.5
    v = clamp((0.5 - pos) * 1.6)
    out.append(_vote('premium', v,
        f"Price sits at {pos * 100:.0f}% of the dealing range {f(rng_lo)}–{f(rng_hi)}: "
        + ('discount (cheap), better for buying.' if pos < 0.45 else 'premium (expensive), better for selling.' if pos > 0.55 else 'at equilibrium.'),
        conf=0.5, levels=[{'price': _r(symbol, (rng_hi + rng_lo) / 2), 'label': 'Equilibrium'}]))
    lvl((rng_hi + rng_lo) / 2, 'Equilibrium (50%)', 'range')

    # 9. support & resistance (+ breakout and retest)
    lv = main['levels']['levels']
    sup = [z for z in lv if z['price'] < price - 0.1 * atr]
    res = [z for z in lv if z['price'] > price + 0.1 * atr]
    s_near = max(sup, key=lambda z: z['price']) if sup else None
    r_near = min(res, key=lambda z: z['price']) if res else None
    v, txt = 0.0, ''
    brk = main['features'].get('breakout', 0)
    if brk:
        v, txt = 0.6 * brk, next((r['text'] for r in main['reasons'] if r['key'] == 'breakout'), 'Breakout') + '; a retest of it is a classic entry.'
    else:
        ds = (price - s_near['price']) / atr if s_near else 9
        dr = (r_near['price'] - price) / atr if r_near else 9
        if ds < 0.5 and (not r_near or dr > 1):
            v, txt = 0.4, f"Sitting on {s_near['touches']}-touch support {f(s_near['price'])} with room above."
        elif dr < 0.5 and (not s_near or ds > 1):
            v, txt = -0.4, f"Pressing {r_near['touches']}-touch resistance {f(r_near['price'])} with room below."
        else:
            v = clamp((min(dr, 4) - min(ds, 4)) / 8)
            txt = (f"Support {f(s_near['price'])} ({ds:.1f} ATR below), " if s_near else 'No support close below, ') + \
                  (f"resistance {f(r_near['price'])} ({dr:.1f} ATR above)." if r_near else 'no resistance close above.')
    for z in sorted(lv, key=lambda z: abs(z['price'] - price))[:6]:
        if z['touches'] >= 2:
            lvl(z['price'], f"{'Resistance' if z['price'] > price else 'Support'} ×{z['touches']}", 'sr')
    out.append(_vote('sr', v, txt, conf=0.6))

    # 10. candlestick patterns (stronger at a level)
    name, d = candle_pattern(s, i)
    at_level = any(abs(x['price'] - s.c[i]) < 0.5 * atr for x in liq if x['kind'] in ('sr', 'liquidity', 'orderblock'))
    out.append(_vote('price_action', d * (0.6 if at_level else 0.25),
        (f"{name} on the last closed {main_tf.upper()} candle" + (' at a key level: meaningful.' if at_level else ', away from key levels: weak on its own.'))
        if name else 'No candlestick pattern on the last closed candle.', conf=0.6 if at_level and d else 0.3))

    # 11. session breakout (London / New York out of the Asian range)
    spec = mt5.SPECS.get(symbol)
    if spec and spec.kind in ('forex', 'metal') and 'asia_hi' in sess:
        hr = (time.time() % 86400) / 3600
        if 7 <= hr < 20:
            if price > sess['asia_hi']:
                v, txt = 0.4, f"Broke above the Asian high {f(sess['asia_hi'])} during London / New York: breakout buyers in control."
            elif price < sess['asia_lo']:
                v, txt = -0.4, f"Broke below the Asian low {f(sess['asia_lo'])} during London / New York: breakout sellers in control."
            else:
                v, txt = 0, f"Still inside the Asian range {f(sess['asia_lo'])}–{f(sess['asia_hi'])}: wait for the break (and watch for a fake-out sweep)."
        else:
            v, txt = 0, f"Asian session range forming: {f(sess['asia_lo'])}–{f(sess['asia_hi'])}."
        out.append(_vote('session', v, txt, conf=0.4))
    else:
        out.append(_vote('session', None, 'Session breakouts apply to forex and metals.'))

    # 12. momentum
    h0, h1 = s.hist[i], s.hist[i - 1]
    roc = (s.c[i] - s.c[i - 10]) / s.c[i - 10] * 100 if s.c[i - 10] else 0
    rsi_v = s.rsi[i]
    v = 0.4 * (1 if h0 > 0 else -1) + 0.2 * (1 if h0 > h1 else -1) + 0.4 * clamp((rsi_v - 50) / 20)
    out.append(_vote('momentum', v,
        f"MACD histogram {'positive' if h0 > 0 else 'negative'} and {'rising' if h0 > h1 else 'falling'}, RSI {rsi_v:.0f}, "
        f"{roc:+.2f}% over 10 bars.", conf=0.6))

    # 13. mean reversion (fades extremes; weaker in strong trends)
    bw = s.bb_up[i] - s.bb_lo[i]
    pb = (s.c[i] - s.bb_lo[i]) / bw if bw else 0.5
    v, txt = 0.0, f'Inside the Bollinger bands (%B {pb:.2f}), RSI {rsi_v:.0f}: no stretch to fade.'
    if pb > 1 and rsi_v > 68:
        v, txt = -0.7, f'Above the upper band with RSI {rsi_v:.0f}: stretched, a pullback to the 20 EMA ({f(s.ema20[i])}) is likely.'
    elif pb < 0 and rsi_v < 32:
        v, txt = 0.7, f'Below the lower band with RSI {rsi_v:.0f}: stretched, a bounce to the 20 EMA ({f(s.ema20[i])}) is likely.'
    elif pb > 0.9:
        v, txt = -0.3, f'Near the upper band (%B {pb:.2f}).'
    elif pb < 0.1:
        v, txt = 0.3, f'Near the lower band (%B {pb:.2f}).'
    if a_val >= 30 and v:
        v *= 0.5; txt += f' ADX {a_val:.0f}: strong trends stay stretched, so this is discounted.'
    out.append(_vote('mean_rev', v, txt, conf=0.5))

    # 14. divergence (main and lower timeframe)
    d1, t1 = divergence(s, i)
    d2, t2 = divergence(lower['series'], lower['last']) if lower else (0, None)
    if d1:
        out.append(_vote('divergence', 0.6 * d1, f'{main_tf.upper()}: {t1}.', conf=0.6))
    elif d2:
        out.append(_vote('divergence', 0.35 * d2, f'{lower_tf.upper()}: {t2}.', conf=0.4))
    else:
        out.append(_vote('divergence', 0, 'No RSI divergence on the last swings.', conf=0.3))

    # 15. volatility regime (squeeze → expansion)
    widths = [s.bb_up[j] - s.bb_lo[j] for j in range(max(0, i - 120), i + 1) if s.bb_up[j] is not None]
    pct = sum(1 for w in widths if w < bw) / len(widths) if widths else 0.5
    atrs = sorted(v for v in s.atr[max(0, i - 100):i + 1] if v)
    ratio = atr / atrs[len(atrs) // 2] if atrs else 1
    mom_sign = 1 if h0 > 0 else -1
    if pct < 0.15:
        v, txt = 0.3 * mom_sign, f'Bollinger squeeze (narrowest {pct * 100:.0f}% of the last 120 bars): a volatility expansion is building; momentum points {"up" if mom_sign > 0 else "down"}.'
    else:
        v, txt = 0, f'ATR is {ratio:.1f}× normal; band width in the {pct * 100:.0f}% percentile of the last 120 bars.'
    out.append(_vote('volatility', v, txt, conf=0.3, details=[f'ATR {f(atr)}']))

    # 16. volume (only when the feed has real volume)
    vols = [c.get('volume') or 0 for c in candles_for(symbol, main_tf)['candles'][-60:]]
    if sum(1 for x in vols if x) > 40:
        avg = sum(vols[-21:-1]) / 20 or 1
        lastv = vols[-2] if len(vols) > 1 else vols[-1]
        up_bar = s.c[i] > s.o[i]
        if lastv > 1.8 * avg:
            out.append(_vote('volume', 0.4 if up_bar else -0.4, f'Volume spike ({lastv / avg:.1f}× average) on a {"rising" if up_bar else "falling"} candle: real participation.', conf=0.5))
        else:
            out.append(_vote('volume', 0, f'Volume {lastv / avg:.1f}× average: nothing unusual.', conf=0.3))
    else:
        out.append(_vote('volume', None, 'No reliable volume for this market (forex and CFD feeds have none).'))

    # 17. trained model
    tr = analyse(symbol, style)
    m = tr.get('model') or {}
    if m.get('trained') and m.get('prob_up') is not None and m.get('test_accuracy', 0) > m.get('baseline', 1) + 0.01:
        out.append(_vote('model', (m['prob_up'] - 0.5) * 2.5, f"{m['prob_up'] * 100:.0f}% chance of +1.5 ATR before −1.5 ATR "
                         f"(accuracy {m['test_accuracy'] * 100:.0f}% vs baseline {m['baseline'] * 100:.0f}% on unseen bars).", conf=0.6))
    else:
        out.append(_vote('model', None, 'The model did not beat the simple baseline on unseen bars, so it gets no vote.' if m.get('trained') else 'Not enough history to train the model.'))

    liq.sort(key=lambda x: -x['price'])
    res = {'ok': True, 'symbol': symbol, 'style': style, 'main_tf': main_tf, 'price': _r(symbol, price), 'atr': _r(symbol, atr),
           'strategies': out, 'levels': liq, 'coach': coach,
           'zones': {'order_blocks': [{k: (_r(symbol, v) if k in ('lo', 'hi') else v) for k, v in o.items()} for o in obs[-8:]],
                     'fvgs': [{k: (_r(symbol, v) if k in ('lo', 'hi') else v) for k, v in g.items()} for g in gaps[-8:]],
                     'range': {'hi': _r(symbol, rng_hi), 'lo': _r(symbol, rng_lo)}},
           'sessions': {k: _r(symbol, v) for k, v in sess.items()}, 'analysed_at': int(time.time())}
    cache.set(key, res, CANDLE_TTL.get(main_tf, 300))
    return res


def scrutiny_verdict(strategies, coach, news_block=None):
    """Weighted vote of the strategies + what the entry coach says about timing → one call to action."""
    used = [x for x in strategies if x['vote'] is not None and x.get('enabled', True)]
    den = sum(x['weight'] for x in used) or 1
    score = sum(x['weight'] * x['vote'] for x in used) / den * 100
    sign = 1 if score >= 0 else -1
    agree = [x for x in used if x['vote'] * sign >= 0.2]
    against = [x for x in used if x['vote'] * sign <= -0.2]
    conviction = round(min(100, abs(score) * (0.6 + 0.8 * len(agree) / max(1, len(used)))))
    side = 'BUY' if sign > 0 else 'SELL'
    st = coach.get('state') if coach else None
    coach_side = coach.get('side') if coach else None
    if abs(score) < 12:
        action, headline = 'STAND_ASIDE', 'No edge: the strategies cancel out'
    elif news_block:
        action, headline = 'WAIT_NEWS', f"{'Bullish' if sign > 0 else 'Bearish'} lean, but wait for the news"
    elif coach_side and coach_side != side:
        action, headline = 'CONFLICT', f"{'Bullish' if sign > 0 else 'Bearish'} lean against the chart's trend: higher risk"
    elif st == 'READY':
        action, headline = 'ENTER', f"{'Buy' if sign > 0 else 'Sell'}: entry confirmed"
    elif st == 'IN_ZONE':
        action, headline = 'GET_READY', f"{'Buy' if sign > 0 else 'Sell'} setup in the zone: wait for confirmation"
    elif st in ('WAIT_PULLBACK', 'EXTENDED'):
        action, headline = 'WAIT_PULLBACK', f"{'Bullish' if sign > 0 else 'Bearish'}: wait for the pullback"
    elif st == 'MISSED':
        action, headline = 'WAIT_PULLBACK', f"{'Bullish' if sign > 0 else 'Bearish'}: the last entry was missed, wait for the next"
    else:
        action, headline = 'WATCH', f"{'Bullish' if sign > 0 else 'Bearish'} lean without a clean structure"
    return {'score': round(score, 1), 'side': side, 'conviction': conviction, 'action': action, 'headline': headline,
            'agree': [x['key'] for x in agree], 'against': [x['key'] for x in against], 'voting': len(used)}
