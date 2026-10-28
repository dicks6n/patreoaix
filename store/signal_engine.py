# store/signal_engine.py
"""
Confluence signal engine — every scanner on the market overview page voting on
one trade, plus the AI prediction model.

It builds on chart_scanner (same candles, same indicators, same trained model)
and adds the scanners the chart scanner doesn't have:

    patterns     candlestick pattern on the last closed bar
    levels       room to the next support / resistance zone
    heatmap      currency strength across every scanned pair
    divergence   RSI divergence at swing points
    volume       last bar's volume vs its 20-bar average + OBV slope
    mtf          lower / main / higher timeframe scores lined up
    session      which trading sessions are open right now (quality gate)
    volatility   ATR regime and Bollinger squeeze (quality gate)
    momentum     RSI + MACD + stochastic + rate of change
    reversal     reversal candle / divergence / stretched RSI AT a level
    breakout     break of a 2+ touch zone or the 20-bar range
    correlation  what the most correlated markets are signalling
    model        logistic model trained on the symbol's own history — it only
                 gets a vote when it beat the baseline on unseen bars

Each scanner returns a vote from -1 (sell) to +1 (buy), or None when it has no
data (e.g. no volume from the feed) so it is left out instead of guessed.
Session and volatility don't vote; they scale the quality of the setup.

A setup is graded A+ / A / B from how strongly and how widely the scanners
agree. Grades A and A+ are "strong signals": the background worker
(manage.py signal_scanner) records them and notifies subscribed users.

These are statistical readings of past prices, not guarantees.
"""
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_tz

from django.core.cache import cache

from . import chart_scanner as cs
from . import mt5_service as mt5

logger = logging.getLogger(__name__)

SCANNERS = [
    # key           label          weight  icon
    ('patterns',    'Patterns',    8,  'fa-shapes'),
    ('levels',      'Levels',      7,  'fa-layer-group'),
    ('heatmap',     'Heatmap',     7,  'fa-table-cells'),
    ('divergence',  'Divergence',  6,  'fa-code-compare'),
    ('volume',      'Volume',      6,  'fa-chart-simple'),
    ('mtf',         'MTF',         14, 'fa-grip'),
    ('session',     'Session',     0,  'fa-globe'),
    ('volatility',  'Volatility',  0,  'fa-wave-square'),
    ('momentum',    'Momentum',    9,  'fa-gauge-high'),
    ('reversal',    'Reversal',    6,  'fa-rotate-left'),
    ('breakout',    'Breakout',    7,  'fa-arrow-up-right-dots'),
    ('correlation', 'Correlation', 7,  'fa-link'),
    ('model',       'AI model',    12, 'fa-brain'),
]
WEIGHT = {k: w for k, _, w, _ in SCANNERS}
GRADE_RANK = {'B': 1, 'A': 2, 'A+': 3}
STRONG_GRADES = ('A', 'A+')
AGREE_MIN = 0.25           # |vote| that counts as agreeing / opposing

FX = {'USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD'}
CRYPTO = {'BTCUSD', 'ETHUSD', 'SOLUSD', 'BNBUSD', 'XRPUSD'}
# UTC hours (approximate, ignores daylight saving)
SESSIONS = {'Sydney': (21, 6), 'Tokyo': (0, 9), 'London': (7, 16), 'New York': (12, 21)}
HOME_SESSIONS = {'USD': {'New York'}, 'CAD': {'New York'}, 'EUR': {'London'}, 'GBP': {'London'}, 'CHF': {'London'},
                 'JPY': {'Tokyo'}, 'AUD': {'Sydney', 'Tokyo'}, 'NZD': {'Sydney', 'Tokyo'},
                 'XAU': {'London', 'New York'}, 'XAG': {'London', 'New York'}, 'OIL': {'London', 'New York'}}


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def legs(symbol):
    """'EURUSD' → ('EUR', 'USD'); metals/oil/crypto → (asset, 'USD')."""
    if symbol in ('USOIL', 'UKOIL'):
        return 'OIL', 'USD'
    return symbol[:3], symbol[3:6]


def _sv(key, label, vote, text, gate=None):
    return {'key': key, 'label': label, 'vote': None if vote is None else round(_clamp(vote), 3),
            'text': text, 'gate': gate}


# ═══════════════════════════════════════════════════════════════
# SCANNERS
# ═══════════════════════════════════════════════════════════════
def scan_patterns(main):
    s, i = main['series'], main['last']
    name, d = cs.candle_pattern(s, i)
    if not name:
        pname, pd = cs.candle_pattern(s, i - 1)
        if pname and pd and (s.c[i] - s.o[i]) * pd > 0:          # yesterday's pattern, confirmed by this candle
            return _sv('patterns', 'Patterns', 0.6 * pd, f'{pname} confirmed by the next candle')
        return _sv('patterns', 'Patterns', 0, 'No candlestick pattern on the last closed candle')
    return _sv('patterns', 'Patterns', d, f'{name} on the last closed candle' + ('' if d else ' (indecision)'))


def scan_levels(main):
    f, lv, atr = main['features'], main['levels'], main['series'].atr[main['last']]
    c = main['series'].c[main['last']]
    parts = []
    if lv.get('resistance'):
        parts.append(f"resistance {(lv['resistance'] - c) / atr:.1f} ATR above")
    if lv.get('support'):
        parts.append(f"support {(c - lv['support']) / atr:.1f} ATR below")
    return _sv('levels', 'Levels', f['levels'], ('Next ' + ', '.join(parts)) if parts else 'No nearby zones — open space')


def scan_divergence(main):
    d, txt = cs.divergence(main['series'], main['last'])
    return _sv('divergence', 'Divergence', d, txt or 'No RSI divergence at the last swings')


def scan_volume(main, candles):
    s, i = main['series'], main['last']
    vol = [float(c.get('volume') or 0) for c in candles]
    if len(vol) != s.n or i < 25 or sum(1 for v in vol[i - 20:i + 1] if v > 0) < 15:
        return _sv('volume', 'Volume', None, 'No volume data from this feed')
    avg = sum(vol[i - 20:i]) / 20 or 1
    rel = vol[i] / avg
    body = 1 if s.c[i] > s.o[i] else -1 if s.c[i] < s.o[i] else 0
    obv, obv_start = 0.0, None
    for j in range(i - 20, i + 1):
        obv += vol[j] if s.c[j] > s.c[j - 1] else -vol[j] if s.c[j] < s.c[j - 1] else 0
        if obv_start is None:
            obv_start = obv
    obv_dir = _clamp((obv - obv_start) / (avg * 6))
    spike = body * _clamp(rel - 1) if rel >= 1.3 else 0
    vote = 0.6 * spike + 0.4 * obv_dir
    txt = f'Last candle volume {rel:.1f}× its 20-bar average' + (f" on a {'green' if body > 0 else 'red'} candle" if rel >= 1.3 and body else '')
    txt += f"; OBV {'rising' if obv_dir > 0.15 else 'falling' if obv_dir < -0.15 else 'flat'}"
    return _sv('volume', 'Volume', vote, txt)


def scan_mtf(lower, main, higher):
    parts, num, den = [], 0.0, 0.0
    for t, w in ((lower, 0.25), (main, 0.35), (higher, 0.40)):
        if t:
            parts.append(f"{t['tf'].upper()} {t['score']:+.0f}")
            num += w * t['score']
            den += w
    if den == 0:
        return _sv('mtf', 'MTF', None, 'No timeframe data')
    vote = _clamp(num / den / 45)
    scores = [t['score'] for t in (lower, main, higher) if t]
    aligned = all(x > 10 for x in scores) or all(x < -10 for x in scores)
    if aligned:
        vote = _clamp(vote * 1.25)
    return _sv('mtf', 'MTF', vote, ' · '.join(parts) + (' — all aligned' if aligned else ' — mixed'))


def scan_session(symbol, now=None):
    now = now or datetime.now(dt_tz.utc)
    h = now.hour
    if symbol in CRYPTO:
        return _sv('session', 'Session', None, 'Crypto trades 24/7', gate=1.0)
    if now.weekday() == 5 or (now.weekday() == 6 and h < 21) or (now.weekday() == 4 and h >= 21):
        return _sv('session', 'Session', None, 'Market closed for the weekend', gate=0.0)
    open_ = [n for n, (a, b) in SESSIONS.items() if (a <= h < b if a < b else (h >= a or h < b))]
    home = set().union(*(HOME_SESSIONS.get(x, set()) for x in legs(symbol)))
    if {'London', 'New York'} <= set(open_):
        gate, txt = 1.0, 'London–New York overlap (highest liquidity)'
    elif home & set(open_):
        gate, txt = 1.0, f"{', '.join(sorted(home & set(open_)))} session open for this market"
    elif open_:
        gate, txt = 0.8, f"{', '.join(open_)} open — not this market's main session"
    else:
        gate, txt = 0.6, 'Between sessions — thin liquidity'
    return _sv('session', 'Session', None, txt + ' (UTC hours, approx.)', gate=gate)


def scan_volatility(main):
    s, i = main['series'], main['last']
    atrs = [a for a in s.atr[max(0, i - 100):i + 1] if a]
    widths = [(s.bb_up[j] - s.bb_lo[j]) / s.bb_mid[j] for j in range(max(0, i - 100), i + 1)
              if s.bb_up[j] is not None and s.bb_mid[j]]
    if len(atrs) < 30 or len(widths) < 30:
        return _sv('volatility', 'Volatility', None, 'Not enough history', gate=1.0)
    med = sorted(atrs)[len(atrs) // 2]
    ratio = s.atr[i] / med if med else 1
    pct = sum(1 for w in widths if w <= widths[-1]) / len(widths)
    was_squeezed = min(widths[-6:-1]) <= sorted(widths)[len(widths) // 5]
    vote, gate = None, 1.0
    if ratio > 2.2:
        gate, txt = 0.6, f'ATR {ratio:.1f}× normal — extreme spike, wider stops needed'
    elif ratio < 0.6:
        gate, txt = 0.85, f'ATR {ratio:.1f}× normal — very quiet market'
    else:
        txt = f'ATR {ratio:.1f}× normal — healthy volatility'
    if was_squeezed and pct > 0.5:
        vote = 0.6 if s.c[i] > s.bb_mid[i] else -0.6
        txt += f"; Bollinger squeeze releasing {'upward' if vote > 0 else 'downward'}"
    elif pct < 0.2:
        txt += '; Bollinger squeeze building (breakout pending)'
    return _sv('volatility', 'Volatility', vote, txt, gate=gate)


def scan_momentum(main):
    f, s, i = main['features'], main['series'], main['last']
    roc = (s.c[i] - s.c[i - 10]) / (s.atr[i] * 3) if s.atr[i] else 0
    vote = 0.35 * f['rsi'] + 0.35 * f['macd'] + 0.15 * f['stoch'] + 0.15 * _clamp(roc)
    return _sv('momentum', 'Momentum', vote,
               f"RSI {s.rsi[i]:.0f}, MACD histogram {'rising' if s.hist[i] > s.hist[i - 1] else 'falling'}, 10-bar move {roc * 3:+.1f} ATR")


def scan_reversal(main):
    s, i, f = main['series'], main['last'], main['features']
    atr, c, lv = s.atr[i], s.c[i], main['levels']
    near_sup = lv.get('support') is not None and (c - lv['support']) / atr < 1.0
    near_res = lv.get('resistance') is not None and (lv['resistance'] - c) / atr < 1.0
    name, d = cs.candle_pattern(s, i)
    r0, r1 = s.rsi[i], s.rsi[i - 1]
    bull = [x for x in (d > 0 and name, f['divergence'] > 0 and 'bullish divergence',
                        r1 is not None and r1 < 30 <= r0 and 'RSI back above 30') if x]
    bear = [x for x in (d < 0 and name, f['divergence'] < 0 and 'bearish divergence',
                        r1 is not None and r1 > 70 >= r0 and 'RSI back below 70') if x]
    if near_sup and bull:
        return _sv('reversal', 'Reversal', 1.0 if len(bull) > 1 else 0.6, 'Bullish reversal at support: ' + ', '.join(bull))
    if near_res and bear:
        return _sv('reversal', 'Reversal', -1.0 if len(bear) > 1 else -0.6, 'Bearish reversal at resistance: ' + ', '.join(bear))
    if bull or bear:
        return _sv('reversal', 'Reversal', 0.3 if bull else -0.3, ', '.join(bull or bear).capitalize() + ' — but not at a key level')
    return _sv('reversal', 'Reversal', 0, 'No reversal setup')


def scan_breakout(main):
    s, i, f = main['series'], main['last'], main['features']
    hi20, lo20 = max(s.h[i - 20:i]), min(s.l[i - 20:i])
    don = 0.7 if s.c[i] > hi20 else -0.7 if s.c[i] < lo20 else 0
    vote = f['breakout'] if f['breakout'] else don
    if f['breakout']:
        txt = f"Broke {'above' if f['breakout'] > 0 else 'below'} a tested zone on the last close"
    elif don:
        txt = f"Closed {'above the 20-bar high' if don > 0 else 'below the 20-bar low'}"
    else:
        txt = 'Inside the 20-bar range'
    return _sv('breakout', 'Breakout', vote, txt)


def currency_strength(results):
    """Average directional score per currency from every scanned pair."""
    acc = {}
    for r in results:
        if not r or not r.get('ok'):
            continue
        b, q = legs(r['symbol'])
        if b in FX and q in FX:
            acc.setdefault(b, []).append(r['score'])
            acc.setdefault(q, []).append(-r['score'])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def scan_heatmap(symbol, strength):
    b, q = legs(symbol)
    if not strength or 'USD' not in strength:
        return _sv('heatmap', 'Heatmap', None, 'Currency strength not available yet')
    if b in strength and q in strength:
        diff = strength[b] - strength[q]
        return _sv('heatmap', 'Heatmap', diff / 50, f'{b} {strength[b]:+.0f} vs {q} {strength[q]:+.0f} on the strength heatmap')
    if q == 'USD':          # gold, silver, oil, crypto: a weak dollar helps them
        return _sv('heatmap', 'Heatmap', -strength['USD'] / 60, f"USD strength {strength['USD']:+.0f} ({'headwind' if strength['USD'] > 10 else 'tailwind' if strength['USD'] < -10 else 'neutral'})")
    return _sv('heatmap', 'Heatmap', None, 'Not covered by the heatmap')


def _returns(candles, n=120):
    t = [c['time'] for c in candles[-n - 1:-1]]
    c = [float(x['close']) for x in candles[-n - 1:-1]]
    return {t[k]: (c[k] - c[k - 1]) / c[k - 1] for k in range(1, len(c)) if c[k - 1]}


def pearson(a, b):
    keys = sorted(set(a) & set(b))
    if len(keys) < 40:
        return None
    x, y = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
    sx = math.sqrt(sum((p - mx) ** 2 for p in x))
    sy = math.sqrt(sum((q - my) ** 2 for q in y))
    return sxy / (sx * sy) if sx and sy else None


def scan_correlation(symbol, main_tf, by_symbol):
    mine = cs.candles_for(symbol, main_tf)['candles']
    if len(mine) < 60:
        return _sv('correlation', 'Correlation', None, 'Not enough history')
    ra = _returns(mine)
    peers = []
    for other, r in by_symbol.items():
        if other == symbol or not r or not r.get('ok'):
            continue
        rho = pearson(ra, _returns(cs.candles_for(other, main_tf)['candles']))
        if rho is not None and abs(rho) >= 0.5:
            peers.append((abs(rho), rho, other, r['score']))
    if not peers:
        return _sv('correlation', 'Correlation', None, 'No strongly correlated market right now')
    peers = sorted(peers, reverse=True)[:4]
    num = sum(rho * _clamp(score / 40) for _, rho, _, score in peers)
    den = sum(a for a, _, _, _ in peers)
    vote = num / den
    txt = ', '.join(f"{o} (ρ {rho:+.2f}) {'buy' if score > 10 else 'sell' if score < -10 else 'flat'}" for _, rho, o, score in peers)
    return _sv('correlation', 'Correlation', vote, txt)


def scan_model(base):
    m = base.get('model') or {}
    if not m.get('trained') or m.get('prob_up') is None:
        return _sv('model', 'AI model', None, 'Model not trained — not enough labelled history')
    acc, basel, p = m.get('test_accuracy') or 0, m.get('baseline') or 0.5, m['prob_up']
    if acc <= basel + 0.01:
        return _sv('model', 'AI model', None,
                   f'Model {p * 100:.0f}% up, but it scored {acc * 100:.0f}% on unseen bars vs a {basel * 100:.0f}% baseline — not counted')
    return _sv('model', 'AI model', (p - 0.5) * 2.5,
               f'{p * 100:.0f}% chance of +1.5 ATR before -1.5 ATR · {acc * 100:.0f}% accurate on unseen bars (baseline {basel * 100:.0f}%)')


# ═══════════════════════════════════════════════════════════════
# CONFLUENCE
# ═══════════════════════════════════════════════════════════════
def grade_setup(base, scanners):
    from .ai_learning import multipliers                 # learned daily from the closed signals
    learned = multipliers()
    w = lambda k: WEIGHT[k] * learned.get(k, 1.0)
    voting = [x for x in scanners if x['vote'] is not None and WEIGHT.get(x['key'])]
    den = sum(w(x['key']) for x in voting)
    conf = (sum(w(x['key']) * x['vote'] for x in voting) / den * 100) if den else 0
    direction = 'BUY' if conf > 0 else 'SELL'
    sign = 1 if conf > 0 else -1
    agree = [x['key'] for x in voting if x['vote'] * sign >= AGREE_MIN]
    oppose = [x['key'] for x in voting if x['vote'] * sign <= -AGREE_MIN]
    gate = 1.0
    for x in scanners:
        if x.get('gate') is not None:
            gate *= x['gate']
    quality = abs(conf) * gate
    by = {x['key']: x for x in scanners}
    mtf_ok = by['mtf']['vote'] is not None and by['mtf']['vote'] * sign > 0
    model_v = by['model']['vote']
    model_ok = model_v is None or model_v * sign > 0
    base_ok = base.get('signal') == direction
    plan = base.get('plan') if base_ok else (base.get('watch') if base.get('lean') == direction else None)
    rr_ok = bool(plan and (plan.get('rr') or 0) >= 1.5)

    grade = None
    opposed = base.get('signal') in ('BUY', 'SELL') and not base_ok     # the chart scanner calls the other way
    if gate > 0 and not opposed:
        share = len(agree) / len(voting) if voting else 0
        if base_ok and rr_ok and quality >= 45 and len(agree) >= 8 and share >= 0.7 and len(oppose) <= 1 and mtf_ok and model_ok:
            grade = 'A+'
        elif base_ok and rr_ok and quality >= 35 and len(agree) >= 7 and share >= 0.6 and len(oppose) <= 1 and mtf_ok and model_ok:
            grade = 'A'
        elif quality >= 22 and len(agree) >= 5 and len(oppose) <= 3:
            grade = 'B'
    return {
        'direction': direction, 'confluence': round(conf, 1), 'quality': round(quality, 1), 'gate': round(gate, 2),
        'agree': agree, 'oppose': oppose, 'voting': len(voting), 'grade': grade,
        'strong': grade in STRONG_GRADES, 'plan': plan, 'conflict': opposed,
    }


def evaluate(symbol, style, base, by_symbol, strength):
    lower_tf, main_tf, higher_tf = cs.STYLES[style]
    main = cs.analyse_tf(symbol, main_tf)
    if not main:
        return None
    higher = cs.analyse_tf(symbol, higher_tf)
    lower = cs.analyse_tf(symbol, lower_tf)
    candles = cs.candles_for(symbol, main_tf)['candles']
    scanners = [
        scan_patterns(main), scan_levels(main), scan_heatmap(symbol, strength), scan_divergence(main),
        scan_volume(main, candles), scan_mtf(lower, main, higher), scan_session(symbol), scan_volatility(main),
        scan_momentum(main), scan_reversal(main), scan_breakout(main),
        scan_correlation(symbol, main_tf, by_symbol), scan_model(base),
    ]
    g = grade_setup(base, scanners)
    s = main['series']
    return {
        'symbol': symbol, 'style': style, 'main_tf': main_tf, 'name': base.get('name'), 'kind': base.get('kind'),
        'price': base.get('price'), 'base_signal': base.get('signal'), 'base_score': base.get('score'),
        'bar_time': s.t[main['last']], 'scanners': scanners, **g,
        'analysed_at': int(time.time()),
    }


def scan_all(style='intraday', symbols=None):
    """Confluence for every symbol. Returns (results, strength)."""
    style = style if style in cs.STYLES else 'intraday'
    bases = cs.scan(list(cs.SCAN_SYMBOLS), style)          # cached per main-timeframe candle
    by_symbol = {b['symbol']: b for b in bases if b}
    strength = currency_strength(bases)
    wanted = [s for s in (symbols or cs.SCAN_SYMBOLS) if by_symbol.get(s, {}).get('ok')]

    def one(sym):
        key = f"sigeng:{sym}:{style}"
        hit = cache.get(key)
        if hit and hit.get('base_score') == by_symbol[sym].get('score'):
            return hit
        try:
            res = evaluate(sym, style, by_symbol[sym], by_symbol, strength)
        except Exception as e:
            logger.exception(f'Confluence failed for {sym}: {e}')
            return None
        if res:
            cache.set(key, res, 60)
        return res
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [r for r in pool.map(one, wanted) if r]
    results.sort(key=lambda r: (-(GRADE_RANK.get(r['grade'], 0)), -r['quality']))
    return results, {k: round(v, 1) for k, v in strength.items()}


def scanner_meta():
    return [{'key': k, 'label': label, 'weight': w, 'icon': icon} for k, label, w, icon in SCANNERS]


def _fmt(symbol, v):
    if v is None:
        return '—'
    spec = mt5.SPECS.get(symbol)
    return f'{v:.{spec.digits if spec else 5}f}'


def alert_text(r):
    """Title + message for a notification about strong setup r."""
    p = r.get('plan') or {}
    by = {x['key']: x for x in r['scanners']}
    labels = {k: lab for k, lab, _, _ in SCANNERS}
    title = f"{'🟢' if r['direction'] == 'BUY' else '🔴'} {r['grade']} {r['direction']} {r['symbol']} · {r['main_tf'].upper()}"
    lines = [f"Entry {_fmt(r['symbol'], p.get('entry'))} · SL {_fmt(r['symbol'], p.get('sl'))} · "
             f"TP {_fmt(r['symbol'], p.get('tp1'))}" + (f" (R:R {p['rr']})" if p.get('rr') else '')]
    lines.append(f"{len(r['agree'])}/{r['voting']} scanners agree: " + ', '.join(labels[k] for k in r['agree']) + '.')
    for k in ('patterns', 'mtf', 'model'):
        if k in r['agree']:
            lines.append(by[k]['text'] + '.')
    lines.append('Not financial advice — manage your risk.')
    return title, ' '.join(lines)
