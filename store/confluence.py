"""
Confluence engine.

Combines the chart_scanner verdict with 12 independent scanners on the
last closed candle of the main timeframe and weights them into one score.

Returns, per symbol:
  scanners  -> list of {key, label, vote (-1..1 or None), text, gate?}
  grade     -> 'A+', 'A', 'B' or None (no grade)
  strong    -> bool (A+/A)
  conflict  -> bool (chart scanner disagrees with scanners)
  direction -> 'BUY' | 'SELL'
  base_signal -> chart scanner's own signal
  main_tf   -> main timeframe of the style
  analysed_at
Plus at the top level:
  scanners  -> catalog of scanner weights (for the frontend toggles)
  strength  -> currency strength map built from every scanned pair
  worker    -> {running, last_run}
  updated_at
"""
from __future__ import annotations
import time
from datetime import datetime
from collections import defaultdict

from django.core.cache import cache
from django.utils import timezone

from . import chart_scanner, mt5_service as mt5


SCANNERS = [
    {'key': 'patterns',    'label': 'Patterns',    'icon': 'fa-shapes',        'weight': 8},
    {'key': 'levels',      'label': 'Levels',      'icon': 'fa-layer-group',   'weight': 8},
    {'key': 'heatmap',     'label': 'Heatmap',     'icon': 'fa-table-cells',   'weight': 4},
    {'key': 'divergence',  'label': 'Divergence',  'icon': 'fa-code-compare',  'weight': 7},
    {'key': 'volume',      'label': 'Volume',      'icon': 'fa-chart-simple',  'weight': 4},
    {'key': 'mtf',         'label': 'MTF',         'icon': 'fa-grip',          'weight': 12},
    {'key': 'session',     'label': 'Session',     'icon': 'fa-globe',         'weight': 0},
    {'key': 'volatility',  'label': 'Volatility',  'icon': 'fa-wave-square',   'weight': 0},
    {'key': 'momentum',    'label': 'Momentum',    'icon': 'fa-gauge-high',    'weight': 9},
    {'key': 'reversal',    'label': 'Reversal',    'icon': 'fa-rotate-left',   'weight': 6},
    {'key': 'breakout',    'label': 'Breakout',    'icon': 'fa-rocket',        'weight': 8},
    {'key': 'correlation', 'label': 'Correlation', 'icon': 'fa-diagram-project','weight': 5},
]


def _closes(s):   return s.c
def _atr(s):      return s.atr[s.n - 1] if s.atr and s.atr[-1] else 1e-9


def _scanner_vote(key, s, reasons_by_key):
    """Return (vote, text, gate) for one scanner key on the latest closed bar."""
    i = s.n - 1

    if key == 'patterns':
        name, d = chart_scanner.candle_pattern(s, i)
        if name:
            return float(d), f'{name} on the last closed candle.', None
        return 0.0, 'No readable candlestick pattern on the last candle.', None

    if key == 'levels':
        lv = s.levels(i)
        if not lv:
            return 0.0, 'No clustered swing levels yet.', None
        price = s.c[i]
        above = [z for z in lv if z['price'] > price]
        below = [z for z in lv if z['price'] < price]
        res = min(above, key=lambda z: z['price'])['price'] if above else None
        sup = max(below, key=lambda z: z['price'])['price'] if below else None
        atr = _atr(s)
        room_up = (res - price) / atr if res else 5
        room_dn = (price - sup) / atr if sup else 5
        vote = max(-1, min(1, (room_up - room_dn) / 4))
        if res and room_up < 1:
            return vote, f'Resistance only {room_up:.1f} ATR above price.', None
        if sup and room_dn < 1:
            return vote, f'Support only {room_dn:.1f} ATR below price.', None
        return vote, f'{round(room_up, 1)} ATR to resistance, {round(room_dn, 1)} ATR to support.', None

    if key == 'heatmap':
        counts = [0] * 10
        for c in s.c[-200:]:
            txt = f'{c:.5f}'.replace('.', '')
            if txt and txt[-1].isdigit():
                counts[int(txt[-1])] += 1
        total = sum(counts) or 1
        # inform but never vote strongly — heatmaps on real prices are noise
        return 0.0, f'Digit distribution over {total} bars — not a directional signal on real markets.', None

    if key == 'divergence':
        d, txt = chart_scanner.divergence(s, i)
        return float(d), txt or 'No divergence between price and RSI.', None

    if key == 'volume':
        # no true volume on spot quotes; use range expansion as a proxy
        rngs = [(s.h[j] - s.l[j]) for j in range(max(0, i - 20), i + 1)]
        if len(rngs) < 10:
            return 0.0, 'Not enough bars.', None
        avg = sum(rngs[:-5]) / max(1, len(rngs) - 5)
        recent = sum(rngs[-5:]) / 5
        spike = recent / avg if avg else 1
        # expansion is a *direction amplifier*, not a direction itself
        return 0.0, f'Bar-range expansion ×{spike:.2f} versus the 20-bar average.', None

    if key == 'mtf':
        # alignment of three EMAs: 20/50/200
        ema20, ema50, ema200 = s.ema20[i], s.ema50[i], s.ema200[i]
        if None in (ema20, ema50, ema200):
            return 0.0, 'EMAs not warmed up.', None
        score = 0.0
        if s.c[i] > ema20: score += 0.4
        elif s.c[i] < ema20: score -= 0.4
        if ema20 > ema50: score += 0.3
        elif ema20 < ema50: score -= 0.3
        if ema50 > ema200: score += 0.3
        elif ema50 < ema200: score -= 0.3
        label = 'aligned bullish' if score > 0.5 else 'aligned bearish' if score < -0.5 else 'mixed'
        return max(-1, min(1, score)), f'EMA 20/50/200 {label}.', None

    if key == 'session':
        # gate: 0 when the market is closed, 1 when open, 0.7 during low-liquidity hours
        from .views import is_market_open
        is_open, reason, hint = is_market_open(_symbol_of(s))
        if not is_open:
            return None, reason + (f'. {hint}.' if hint else ''), 0.0
        hour = datetime.now(timezone.utc).hour
        liq = 1.0 if 7 <= hour < 21 else 0.7
        return None, f'{reason}.', liq

    if key == 'volatility':
        atr = _atr(s)
        pct = atr / s.c[i] * 100
        # gate: 0.6 when volatility is extreme
        gate = 0.6 if pct > 0.8 else 1.0
        return None, f'ATR is {pct:.2f}% of price.', gate

    if key == 'momentum':
        closes = s.c[-20:]
        chg = (closes[-1] - closes[0]) / closes[0] * 100
        if chg > 0.05: return min(1.0, chg / 1.5), f'+{chg:.2f}% over last 20 bars.', None
        if chg < -0.05: return max(-1.0, chg / 1.5), f'{chg:.2f}% over last 20 bars.', None
        return 0.0, 'Flat momentum.', None

    if key == 'reversal':
        recent = s.c[-40:]
        hi, lo = max(recent), min(recent)
        pos = (s.c[i] - lo) / (hi - lo or 1e-9)
        if pos > 0.85:  return -0.7, 'Price near the top of its 40-bar range.', None
        if pos < 0.15:  return +0.7, 'Price near the bottom of its 40-bar range.', None
        return 0.0, 'Price mid-range.', None

    if key == 'breakout':
        lv = s.levels(i)
        price, prev = s.c[i], s.c[i - 1]
        atr = _atr(s)
        for z in lv:
            if z['touches'] >= 2 and prev <= z['price'] < price - atr * 0.2:
                return 1.0, f'Broke above a {z["touches"]}-touch resistance.', None
            if z['touches'] >= 2 and prev >= z['price'] > price + atr * 0.2:
                return -1.0, f'Broke below a {z["touches"]}-touch support.', None
        return 0.0, 'No breakout.', None

    if key == 'correlation':
        prices = s.c[-30:]
        def norm(a):
            lo, hi = min(a), max(a); r = hi - lo or 1e-9
            return [(x - lo) / r for x in a]
        a, b = norm(prices[-10:]), norm(prices[-20:-10])
        ma = sum(a) / 10
        mb = sum(b) / 10
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5 or 1e-9
        c = num / den
        return max(-1, min(1, c)), f'Self-correlation {c:+.2f} between last two windows.', None

    return 0.0, 'No reading.', None


def _symbol_of(s):
    return getattr(s, 'symbol', '') or ''


def _grade(score, gate, chart_signal):
    """A+ / A / B from the weighted score, gated, and consistent with the chart signal."""
    if abs(score) < 15:
        return None, False, False
    direction = 'BUY' if score > 0 else 'SELL'
    if direction != chart_signal and chart_signal != 'WAIT':
        return None, False, True          # conflict
    quality = abs(score) * gate
    if quality >= 45:
        return 'A+', True, False
    if quality >= 30:
        return 'A', True, False
    return 'B', False, False


def build_for_symbol(symbol, style):
    res = chart_scanner.analyse(symbol, style=style)
    if not res.get('ok'):
        return None
    main_tf = chart_scanner.STYLES[style][1]
    data = chart_scanner.candles_for(symbol, main_tf)
    candles = data['candles']
    if len(candles) < 120:
        return None
    s = chart_scanner.Series(candles)
    s.symbol = symbol

    scanners = []
    for meta in SCANNERS:
        vote, text, gate = _scanner_vote(meta['key'], s, {})
        scanners.append({'key': meta['key'], 'vote': vote, 'text': text, **({'gate': gate} if gate is not None else {})})

    # Weighted score from scanners only (frontend re-weights with its own enabled set)
    weight_sum = sum(m['weight'] for m in SCANNERS)
    num = sum(x['vote'] * next(m for m in SCANNERS if m['key'] == x['key'])['weight']
              for x in scanners if x['vote'] is not None)
    raw_score = (num / weight_sum * 100) if weight_sum else 0.0
    gate = 1.0
    for x in scanners:
        if 'gate' in x: gate *= x['gate']
    grade, strong, conflict = _grade(raw_score, gate, res['signal'])
    direction = 'BUY' if raw_score > 0 else 'SELL'

    return {
        'symbol': symbol,
        'main_tf': main_tf,
        'grade': grade,
        'strong': strong,
        'conflict': conflict,
        'direction': direction,
        'base_signal': res['signal'],
        'scanners': scanners,
        'analysed_at': int(time.time()),
    }


def strength_map():
    """Simple currency strength: aggregate the sign of return over the last 20 bars of each pair."""
    try:
        quotes = chart_scanner.mt5.cached_prices()
    except Exception:
        quotes = {}
    buckets = defaultdict(float)
    for sym in chart_scanner.SCAN_SYMBOLS:
        data = chart_scanner.candles_for(sym, '1h')
        candles = data['candles']
        if len(candles) < 30:
            continue
        closes = [c['close'] for c in candles[-20:]]
        pct = (closes[-1] - closes[0]) / closes[0] * 100
        if len(sym) == 6:
            base, quote = sym[:3], sym[3:]
        elif 'XAU' in sym:
            base, quote = 'XAU', 'USD'
        elif 'XAG' in sym:
            base, quote = 'XAG', 'USD'
        elif sym.startswith('BTC'):
            base, quote = 'BTC', 'USD'
        elif sym.startswith('ETH'):
            base, quote = 'ETH', 'USD'
        else:
            continue
        buckets[base] += pct
        buckets[quote] -= pct
    # normalise to ±100 for display
    if not buckets:
        return {}
    mx = max(abs(v) for v in buckets.values()) or 1
    return {k: (v / mx) * 100 for k, v in buckets.items()}


def worker_status():
    """Read the last-run timestamp the background worker stores in cache."""
    last = cache.get('aiworker:last_run')
    return {
        'running': cache.get('aiworker:alive', False),
        'last_run': last.isoformat() if last else None,
    }