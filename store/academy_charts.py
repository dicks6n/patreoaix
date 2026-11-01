# store/academy_charts.py
"""
Pictures for the academy: candles and chart patterns drawn as inline SVG (no image files, sharp on every screen,
and the colours follow the site's light / dark theme through CSS classes):

    .ak-up / .ak-dn     bullish / bearish candles        .ak-hl   highlight box around the pattern
    .ak-ln              level / trend lines               .ak-tx   labels

A picture is a dict:
    {'candles': [(o, h, l, c), …], 'mark': (first, last), 'lines': [...], 'notes': [...]}
or built from a price path with path(points) for chart patterns (head and shoulders, triangles…).
"""
import math

from django.utils.html import escape
from django.utils.safestring import mark_safe


# ═══════════════════════════════════════════════════════════════
# BUILDING BLOCKS
# ═══════════════════════════════════════════════════════════════
def c(o, h, l, cl):
    return (o, h, l, cl)


def trend(start, n, step, body=0.65, wick=0.35):
    """n candles moving `step` per candle from `start` (down when step < 0). Returns (candles, last close)."""
    out, p = [], start
    for i in range(n):
        o, cl = p, p + step
        wob = abs(step) * (0.25 if i % 2 else -0.15)
        o, cl = o + wob * 0.3, cl - wob * 0.2
        hi, lo = max(o, cl) + abs(step) * wick, min(o, cl) - abs(step) * wick
        out.append((o, hi, lo, cl))
        p = cl
    return out, p


def _noise(i):
    return math.sin(i * 12.9898) * 43758.5453 % 1


def path(points, per=4, vol=0.9):
    """Candles following a price path through `points` (each segment `per` candles). → candles, index of each point."""
    out, idx = [], [0]
    prev = points[0]
    for a, b in zip(points, points[1:]):
        for k in range(1, per + 1):
            target = a + (b - a) * k / per
            n = _noise(len(out) + 1)
            o = prev
            cl = target + (n - 0.5) * vol * 0.6
            if k == per:
                cl = b
            hi = max(o, cl) + vol * (0.25 + n * 0.5)
            lo = min(o, cl) - vol * (0.25 + (1 - n) * 0.5)
            out.append((o, hi, lo, cl))
            prev = cl
        idx.append(len(out) - 1)
    return out, idx


# ═══════════════════════════════════════════════════════════════
# SVG
# ═══════════════════════════════════════════════════════════════
def svg(pic, width=360, height=200, pad=14):
    cs = pic['candles']
    lines = pic.get('lines') or []
    hi = max(x[1] for x in cs)
    lo = min(x[2] for x in cs)
    for ln in lines:
        for v in (ln.get('y'), ln.get('y1'), ln.get('y2')):
            if v is not None:
                hi, lo = max(hi, v), min(lo, v)
    rng = (hi - lo) or 1
    top, bottom = pad + 4, height - pad - (14 if pic.get('notes') else 4)
    n = len(cs)
    slot = (width - 2 * pad) / max(n, 1)
    bw = max(3.0, min(slot * 0.62, 26))
    Y = lambda v: top + (hi - v) / rng * (bottom - top)
    X = lambda i: pad + slot * (i + 0.5)
    parts = [f'<svg class="ak-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(pic.get("title", "chart"))}">']
    mark = pic.get('mark')
    if mark:
        a, b = mark
        y0 = min(Y(cs[i][1]) for i in range(a, b + 1)) - 6
        y1 = max(Y(cs[i][2]) for i in range(a, b + 1)) + 6
        parts.append(f'<rect class="ak-hl" x="{X(a) - slot / 2 + 1:.1f}" y="{y0:.1f}" width="{slot * (b - a + 1) - 2:.1f}" '
                     f'height="{y1 - y0:.1f}" rx="8"/>')
    for ln in lines:
        cls = 'ak-ln' + (' ' + ln['cls'] if ln.get('cls') else '')
        if 'y' in ln:
            y = Y(ln['y'])
            x1 = X(ln.get('from', 0)) - slot / 2
            x2 = X(ln.get('to', n - 1)) + slot / 2
            parts.append(f'<line class="{cls}" x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}"/>')
            if ln.get('label'):
                parts.append(f'<text class="ak-tx" x="{x2 - 2:.1f}" y="{y - 4:.1f}" text-anchor="end">{escape(ln["label"])}</text>')
        else:
            x1, x2 = X(ln['i1']), X(ln['i2'])
            parts.append(f'<line class="{cls}" x1="{x1:.1f}" y1="{Y(ln["y1"]):.1f}" x2="{x2:.1f}" y2="{Y(ln["y2"]):.1f}"/>')
            if ln.get('label'):
                parts.append(f'<text class="ak-tx" x="{x2:.1f}" y="{Y(ln["y2"]) - 5:.1f}" text-anchor="end">{escape(ln["label"])}</text>')
    for i, (o, h, l, cl) in enumerate(cs):
        cls = 'ak-up' if cl >= o else 'ak-dn'
        x = X(i)
        parts.append(f'<line class="{cls}" x1="{x:.1f}" y1="{Y(h):.1f}" x2="{x:.1f}" y2="{Y(l):.1f}" stroke-width="1.6"/>')
        y_top, y_bot = Y(max(o, cl)), Y(min(o, cl))
        parts.append(f'<rect class="{cls}" x="{x - bw / 2:.1f}" y="{y_top:.1f}" width="{bw:.1f}" height="{max(y_bot - y_top, 1.6):.1f}" rx="1.5"/>')
    for note in pic.get('notes') or []:
        i, text = note[0], note[1]
        where = note[2] if len(note) > 2 else 'below'
        if where == 'above':
            y = Y(cs[i][1]) - 7
        else:
            y = min(Y(cs[i][2]) + 14, height - 4)
        parts.append(f'<text class="ak-tx" x="{X(i):.1f}" y="{y:.1f}" text-anchor="middle">{escape(text)}</text>')
    parts.append('</svg>')
    return mark_safe(''.join(parts))


# ═══════════════════════════════════════════════════════════════
# CANDLESTICK PATTERNS
# ═══════════════════════════════════════════════════════════════
def _down(n=4, start=110, step=-2.2):
    return trend(start, n, step)


def _up(n=4, start=90, step=2.2):
    return trend(start, n, step)


def candle_pics():
    P = {}

    # anatomy: one bullish and one bearish candle, big
    P['anatomy'] = {'candles': [c(100, 112, 95, 108), c(108, 111, 96, 99)], 'title': 'Candle anatomy',
                    'notes': [(0, 'Bullish'), (1, 'Bearish')]}

    d, p = _down()
    P['hammer'] = {'candles': d + [c(p, p + 0.6, p - 6.5, p + 1.6), c(p + 1.6, p + 4.5, p + 1.2, p + 4)], 'mark': (4, 4),
                   'notes': [(4, 'Hammer')]}
    d, p = _down()
    P['inverted_hammer'] = {'candles': d + [c(p, p + 6.5, p - 0.5, p + 1.4), c(p + 1.4, p + 4.8, p + 1, p + 4.4)], 'mark': (4, 4),
                            'notes': [(4, 'Inverted hammer')]}
    u, p = _up()
    P['hanging_man'] = {'candles': u + [c(p, p + 0.5, p - 6.5, p - 1.2), c(p - 1.2, p - 0.8, p - 4.8, p - 4.2)], 'mark': (4, 4),
                        'notes': [(4, 'Hanging man')]}
    u, p = _up()
    P['shooting_star'] = {'candles': u + [c(p, p + 7, p - 0.5, p - 1.2), c(p - 1.2, p - 0.7, p - 5, p - 4.5)], 'mark': (4, 4),
                          'notes': [(4, 'Shooting star', 'above')]}
    u, p = _up(3)
    P['doji'] = {'candles': u + [c(p, p + 3.5, p - 3.5, p + 0.1), c(p, p + 1, p - 3.5, p - 3)], 'mark': (3, 3),
                 'notes': [(3, 'Doji')]}
    d, p = _down(3)
    P['dragonfly_doji'] = {'candles': d + [c(p, p + 0.2, p - 6, p + 0.05), c(p, p + 3.5, p - 0.3, p + 3.2)], 'mark': (3, 3),
                           'notes': [(3, 'Dragonfly')]}
    u, p = _up(3)
    P['gravestone_doji'] = {'candles': u + [c(p, p + 6, p - 0.2, p - 0.05), c(p, p + 0.3, p - 3.6, p - 3.3)], 'mark': (3, 3),
                            'notes': [(3, 'Gravestone', 'above')]}
    u, p = _up(3, step=1.2)
    P['long_legged_doji'] = {'candles': u + [c(p, p + 5, p - 5, p + 0.1), c(p, p + 2, p - 1.5, p + 1.2)], 'mark': (3, 3),
                             'notes': [(3, 'Long-legged doji')]}
    P['spinning_top'] = {'candles': trend(100, 3, 1.4)[0] + [c(104.2, 107, 101, 104.9), c(104.9, 106, 103, 105.4)], 'mark': (3, 3),
                         'notes': [(3, 'Spinning top')]}
    P['bull_marubozu'] = {'candles': trend(100, 3, -1.2)[0] + [c(96.4, 104.5, 96.4, 104.5), c(104.5, 106.5, 103.5, 106)], 'mark': (3, 3),
                          'notes': [(3, 'Bullish marubozu')]}
    P['bear_marubozu'] = {'candles': trend(100, 3, 1.2)[0] + [c(103.6, 103.6, 95.5, 95.5), c(95.5, 96.5, 93, 93.6)], 'mark': (3, 3),
                          'notes': [(3, 'Bearish marubozu')]}

    d, p = _down()
    P['bullish_engulfing'] = {'candles': d + [c(p, p + 0.6, p - 2.6, p - 2), c(p - 2.6, p + 2.6, p - 3, p + 2.2),
                                              c(p + 2.2, p + 5, p + 1.8, p + 4.6)], 'mark': (4, 5), 'notes': [(5, 'Engulfing')]}
    u, p = _up()
    P['bearish_engulfing'] = {'candles': u + [c(p, p + 2.6, p - 0.6, p + 2), c(p + 2.6, p + 3, p - 2.6, p - 2.2),
                                              c(p - 2.2, p - 1.8, p - 5, p - 4.6)], 'mark': (4, 5), 'notes': [(5, 'Engulfing', 'above')]}
    d, p = _down()
    P['bullish_harami'] = {'candles': d + [c(p + 1, p + 1.4, p - 4.4, p - 4), c(p - 2.8, p - 1.4, p - 3.3, p - 1.8),
                                           c(p - 1.8, p + 1.4, p - 2.2, p + 1)], 'mark': (4, 5), 'notes': [(5, 'Harami')]}
    u, p = _up()
    P['bearish_harami'] = {'candles': u + [c(p - 1, p + 4.4, p - 1.4, p + 4), c(p + 2.8, p + 3.3, p + 1.4, p + 1.8),
                                           c(p + 1.8, p + 2.2, p - 1.4, p - 1)], 'mark': (4, 5), 'notes': [(5, 'Harami', 'above')]}
    d, p = _down()
    P['piercing_line'] = {'candles': d + [c(p + 0.5, p + 0.9, p - 4.2, p - 3.8), c(p - 4.6, p - 0.6, p - 5, p - 1),
                                          c(p - 1, p + 2.2, p - 1.4, p + 1.8)], 'mark': (4, 5), 'notes': [(5, 'Piercing')]}
    u, p = _up()
    P['dark_cloud'] = {'candles': u + [c(p - 0.5, p + 4.2, p - 0.9, p + 3.8), c(p + 4.6, p + 5, p + 0.6, p + 1),
                                       c(p + 1, p + 1.4, p - 2.2, p - 1.8)], 'mark': (4, 5), 'notes': [(5, 'Dark cloud', 'above')]}
    d, p = _down()
    P['tweezer_bottom'] = {'candles': d + [c(p, p + 0.4, p - 4, p - 2.5), c(p - 2.5, p + 0.5, p - 4, p),
                                           c(p, p + 2.6, p - 0.4, p + 2.2)], 'mark': (4, 5),
                           'lines': [{'y': p - 4, 'from': 3, 'to': 6, 'cls': 'ak-sup'}], 'notes': [(5, 'Tweezer bottom')]}
    u, p = _up()
    P['tweezer_top'] = {'candles': u + [c(p, p + 4, p - 0.4, p + 2.5), c(p + 2.5, p + 4, p - 0.5, p),
                                        c(p, p + 0.4, p - 2.6, p - 2.2)], 'mark': (4, 5),
                        'lines': [{'y': p + 4, 'from': 3, 'to': 6, 'cls': 'ak-res'}], 'notes': [(5, 'Tweezer top', 'above')]}

    d, p = _down()
    P['morning_star'] = {'candles': d + [c(p + 0.5, p + 0.9, p - 4.4, p - 4), c(p - 4.8, p - 3.9, p - 6.2, p - 5.1),
                                         c(p - 4.4, p - 0.6, p - 4.8, p - 0.9), c(p - 0.9, p + 1.8, p - 1.2, p + 1.4)],
                         'mark': (4, 6), 'notes': [(5, 'Morning star')]}
    u, p = _up()
    P['evening_star'] = {'candles': u + [c(p - 0.5, p + 4.4, p - 0.9, p + 4), c(p + 4.8, p + 6.2, p + 3.9, p + 5.1),
                                         c(p + 4.4, p + 4.8, p + 0.6, p + 0.9), c(p + 0.9, p + 1.2, p - 1.8, p - 1.4)],
                         'mark': (4, 6), 'notes': [(5, 'Evening star', 'above')]}
    d, p = _down(3)
    P['three_white_soldiers'] = {'candles': d + [c(p, p + 3.4, p - 0.3, p + 3), c(p + 2, p + 5.6, p + 1.8, p + 5.2),
                                                 c(p + 4.2, p + 7.9, p + 4, p + 7.5)], 'mark': (3, 5), 'notes': [(4, '3 white soldiers')]}
    u, p = _up(3)
    P['three_black_crows'] = {'candles': u + [c(p, p + 0.3, p - 3.4, p - 3), c(p - 2, p - 1.8, p - 5.6, p - 5.2),
                                              c(p - 4.2, p - 4, p - 7.9, p - 7.5)], 'mark': (3, 5), 'notes': [(4, '3 black crows', 'above')]}
    d, p = _down()
    P['three_inside_up'] = {'candles': d + [c(p + 1, p + 1.4, p - 4.4, p - 4), c(p - 3, p - 1.2, p - 3.4, p - 1.6),
                                            c(p - 1.6, p + 2.4, p - 2, p + 2)], 'mark': (4, 6), 'notes': [(5, '3 inside up')]}
    u, p = _up()
    P['three_inside_down'] = {'candles': u + [c(p - 1, p + 4.4, p - 1.4, p + 4), c(p + 3, p + 3.4, p + 1.2, p + 1.6),
                                              c(p + 1.6, p + 2, p - 2.4, p - 2)], 'mark': (4, 6), 'notes': [(5, '3 inside down', 'above')]}
    u, p = _up(4)
    P['rising_three'] = {'candles': u + [c(p, p + 5.4, p - 0.3, p + 5), c(p + 4.6, p + 5, p + 3.4, p + 3.8),
                                         c(p + 3.8, p + 4.2, p + 2.6, p + 3), c(p + 3, p + 3.4, p + 1.8, p + 2.2),
                                         c(p + 2.2, p + 7.6, p + 2, p + 7.2)], 'mark': (4, 8), 'notes': [(6, 'Rising three')]}
    d, p = _down(4)
    P['falling_three'] = {'candles': d + [c(p, p + 0.3, p - 5.4, p - 5), c(p - 4.6, p - 3.4, p - 5, p - 3.8),
                                          c(p - 3.8, p - 2.6, p - 4.2, p - 3), c(p - 3, p - 1.8, p - 3.4, p - 2.2),
                                          c(p - 2.2, p - 2, p - 7.6, p - 7.2)], 'mark': (4, 8), 'notes': [(6, 'Falling three', 'above')]}
    d, p = _down(3)
    P['bullish_kicker'] = {'candles': d + [c(p + 0.5, p + 0.8, p - 3.4, p - 3), c(p + 1.5, p + 6, p + 1.4, p + 5.6),
                                           c(p + 5.6, p + 7.5, p + 5, p + 7)], 'mark': (3, 4), 'notes': [(4, 'Kicker')]}
    for k, v in P.items():
        v.setdefault('title', k.replace('_', ' '))
    return P


# ═══════════════════════════════════════════════════════════════
# CHART PATTERNS & CONCEPTS (price paths)
# ═══════════════════════════════════════════════════════════════
def _p(points, per=3, lines=(), notes=(), vol=0.9, mark=None):
    cs, idx = path(points, per, vol)
    L = []
    for ln in lines:
        ln = dict(ln)
        for k in ('i1', 'i2', 'from', 'to'):
            if k in ln:
                ln[k] = idx[ln[k]]
        L.append(ln)
    N = [(idx[i], t, *rest) for i, t, *rest in notes]
    pic = {'candles': cs, 'lines': L, 'notes': N}
    if mark:
        pic['mark'] = (idx[mark[0]], idx[mark[1]])
    return pic


def pattern_pics():
    P = {}
    P['head_shoulders'] = _p([90, 100, 95, 108, 95, 100, 90, 86], lines=[{'y': 95, 'from': 2, 'to': 7, 'label': 'Neckline', 'cls': 'ak-sup'}],
                             notes=[(1, 'L. shoulder', 'above'), (3, 'Head', 'above'), (5, 'R. shoulder', 'above')])
    P['inverse_head_shoulders'] = _p([110, 100, 105, 92, 105, 100, 110, 114], lines=[{'y': 105, 'from': 2, 'to': 7, 'label': 'Neckline', 'cls': 'ak-res'}],
                                     notes=[(1, 'Shoulder'), (3, 'Head'), (5, 'Shoulder')])
    P['double_top'] = _p([88, 104, 96, 104, 94, 89], per=4, lines=[{'y': 104.5, 'from': 1, 'to': 3, 'label': 'Resistance', 'cls': 'ak-res'},
                                                                   {'y': 96, 'from': 2, 'to': 5, 'label': 'Neckline', 'cls': 'ak-sup'}],
                         notes=[(1, 'Top 1', 'above'), (3, 'Top 2', 'above')])
    P['double_bottom'] = _p([112, 96, 104, 96, 106, 111], per=4, lines=[{'y': 95.5, 'from': 1, 'to': 3, 'label': 'Support', 'cls': 'ak-sup'},
                                                                        {'y': 104, 'from': 2, 'to': 5, 'label': 'Neckline', 'cls': 'ak-res'}],
                            notes=[(1, 'Bottom 1'), (3, 'Bottom 2')])
    P['triple_top'] = _p([90, 104, 97, 104, 97, 104, 95, 90], lines=[{'y': 104.5, 'from': 1, 'to': 5, 'cls': 'ak-res'},
                                                                     {'y': 97, 'from': 2, 'to': 7, 'label': 'Support', 'cls': 'ak-sup'}])
    P['ascending_triangle'] = _p([90, 104, 95, 104, 99, 104, 101.5, 110], lines=[{'y': 104.3, 'from': 1, 'to': 6, 'label': 'Flat top', 'cls': 'ak-res'},
                                                                                 {'i1': 0, 'y1': 90, 'i2': 6, 'y2': 101.5, 'cls': 'ak-sup'}])
    P['descending_triangle'] = _p([110, 96, 105, 96, 101, 96, 98.5, 90], lines=[{'y': 95.7, 'from': 1, 'to': 6, 'label': 'Flat bottom', 'cls': 'ak-sup'},
                                                                                {'i1': 0, 'y1': 110, 'i2': 6, 'y2': 98.5, 'cls': 'ak-res'}])
    P['symmetrical_triangle'] = _p([90, 110, 94, 106, 97, 103, 99, 101, 108], lines=[{'i1': 1, 'y1': 110, 'i2': 7, 'y2': 101, 'cls': 'ak-res'},
                                                                                      {'i1': 0, 'y1': 90, 'i2': 6, 'y2': 99, 'cls': 'ak-sup'}])
    P['bull_flag'] = _p([88, 106, 103, 105, 101.5, 103.5, 100.5, 112], lines=[{'i1': 1, 'y1': 106.5, 'i2': 5, 'y2': 103.8, 'cls': 'ak-res'},
                                                                              {'i1': 2, 'y1': 102.8, 'i2': 6, 'y2': 100.2, 'cls': 'ak-sup'}],
                        notes=[(1, 'Pole', 'above'), (4, 'Flag')])
    P['bear_flag'] = _p([112, 94, 97, 95, 98.5, 96.5, 99.5, 88], lines=[{'i1': 1, 'y1': 93.5, 'i2': 5, 'y2': 96.2, 'cls': 'ak-sup'},
                                                                        {'i1': 2, 'y1': 97.2, 'i2': 6, 'y2': 99.8, 'cls': 'ak-res'}],
                        notes=[(1, 'Pole'), (4, 'Flag', 'above')])
    P['pennant'] = _p([88, 106, 101, 104.5, 102, 103.8, 102.6, 112], lines=[{'i1': 1, 'y1': 106.5, 'i2': 5, 'y2': 104, 'cls': 'ak-res'},
                                                                            {'i1': 2, 'y1': 100.6, 'i2': 6, 'y2': 102.4, 'cls': 'ak-sup'}])
    P['rising_wedge'] = _p([90, 98, 94, 102, 99, 104.5, 102.5, 105.5, 95], lines=[{'i1': 1, 'y1': 98, 'i2': 7, 'y2': 105.5, 'cls': 'ak-res'},
                                                                                  {'i1': 0, 'y1': 90, 'i2': 6, 'y2': 102.5, 'cls': 'ak-sup'}])
    P['falling_wedge'] = _p([110, 102, 106, 98, 101, 95.5, 97.5, 94.5, 105], lines=[{'i1': 1, 'y1': 102, 'i2': 7, 'y2': 94.5, 'cls': 'ak-sup'},
                                                                                    {'i1': 0, 'y1': 110, 'i2': 6, 'y2': 97.5, 'cls': 'ak-res'}])
    P['cup_handle'] = _p([108, 100, 95, 94, 96, 101, 108, 104.5, 106.5, 114], lines=[{'y': 108.3, 'from': 0, 'to': 8, 'label': 'Rim', 'cls': 'ak-res'}],
                         notes=[(3, 'Cup'), (7, 'Handle')])
    P['rectangle'] = _p([95, 105, 96, 104.5, 96.5, 105, 96, 104.8, 112], lines=[{'y': 105.3, 'from': 1, 'to': 7, 'label': 'Resistance', 'cls': 'ak-res'},
                                                                                {'y': 95.8, 'from': 0, 'to': 7, 'label': 'Support', 'cls': 'ak-sup'}])

    # concepts
    P['support_resistance'] = _p([100, 110, 101, 110.5, 100.5, 109.5, 101, 106], per=3,
                                 lines=[{'y': 110.6, 'label': 'Resistance', 'cls': 'ak-res'}, {'y': 100.2, 'label': 'Support', 'cls': 'ak-sup'}])
    P['role_reversal'] = _p([92, 100, 95, 100.3, 108, 100.6, 112], per=3,
                            lines=[{'y': 100.4, 'label': 'Old resistance → new support', 'cls': 'ak-sup'}], notes=[(5, 'Retest')])
    P['uptrend'] = _p([90, 98, 94, 103, 99, 108, 104, 113], lines=[{'i1': 0, 'y1': 90, 'i2': 6, 'y2': 104, 'label': 'Trendline', 'cls': 'ak-sup'}],
                      notes=[(1, 'HH', 'above'), (2, 'HL'), (3, 'HH', 'above'), (4, 'HL'), (5, 'HH', 'above'), (6, 'HL')])
    P['downtrend'] = _p([113, 105, 109, 100, 104, 95, 99, 90], lines=[{'i1': 0, 'y1': 113, 'i2': 6, 'y2': 99, 'label': 'Trendline', 'cls': 'ak-res'}],
                        notes=[(1, 'LL'), (2, 'LH', 'above'), (3, 'LL'), (4, 'LH', 'above'), (5, 'LL')])
    P['channel'] = _p([90, 100, 95, 105, 100, 110, 105, 113], lines=[{'i1': 1, 'y1': 100, 'i2': 7, 'y2': 115, 'cls': 'ak-res'},
                                                                      {'i1': 0, 'y1': 90, 'i2': 6, 'y2': 105, 'cls': 'ak-sup'}])
    P['bos'] = _p([90, 100, 95, 106, 100, 112], per=3, lines=[{'y': 106, 'from': 3, 'to': 5, 'label': 'BOS', 'cls': 'ak-res'}])
    P['choch'] = _p([112, 104, 108, 99, 104, 101, 110], per=3, lines=[{'y': 104, 'from': 4, 'to': 6, 'label': 'CHoCH', 'cls': 'ak-res'}],
                    notes=[(3, 'Last LL'), (5, 'HL')])
    P['order_block'] = _p([100, 98, 97, 109, 113, 105, 98.5, 112], per=3,
                          lines=[{'y': 99.5, 'from': 2, 'to': 7, 'cls': 'ak-sup'}, {'y': 97, 'from': 2, 'to': 7, 'label': 'Order block', 'cls': 'ak-sup'}],
                          notes=[(6, 'Return')])
    fvg_c = [c(100, 101, 99, 100.5), c(100.5, 101.5, 100, 101), c(101, 101.6, 100.4, 101.2), c(101.2, 110, 101, 109.5),
             c(109.5, 111, 104, 110.3), c(110.3, 111.2, 108, 108.5), c(108.5, 109, 102.5, 104), c(104, 108, 103.6, 107.5),
             c(107.5, 112, 107, 111.5)]
    P['fvg'] = {'candles': fvg_c, 'lines': [{'y': 101.6, 'from': 2, 'to': 8, 'cls': 'ak-sup'}, {'y': 104, 'from': 2, 'to': 8, 'label': 'Fair value gap', 'cls': 'ak-sup'}],
                'mark': (2, 4)}
    P['liquidity_sweep'] = _p([104, 98, 103, 98.2, 102.5, 98.1, 101, 96.2, 104, 109], per=3,
                              lines=[{'y': 98, 'from': 1, 'to': 9, 'label': 'Equal lows = stops', 'cls': 'ak-sup'}], notes=[(7, 'Sweep')])
    P['supply_demand'] = _p([96, 108, 107, 99, 101, 107.5, 113, 104, 108], per=3,
                            lines=[{'y': 108.6, 'from': 1, 'to': 8, 'cls': 'ak-res'}, {'y': 106.8, 'from': 1, 'to': 8, 'label': 'Supply', 'cls': 'ak-res'}])
    P['breakout_retest'] = _p([95, 104, 97, 104.2, 99, 110, 104.5, 115], per=3,
                              lines=[{'y': 104.3, 'label': 'Level', 'cls': 'ak-res'}], notes=[(5, 'Breakout', 'above'), (6, 'Retest = entry')])
    P['pullback_entry'] = _p([90, 100, 95, 106, 100.5, 112], per=3,
                             lines=[{'i1': 0, 'y1': 90, 'i2': 4, 'y2': 100.5, 'cls': 'ak-sup'}], notes=[(2, 'Buy here'), (4, 'Buy here')])
    P['range_trading'] = P['rectangle']
    P['fibonacci'] = _p([90, 112, 103.6, 118], per=4, lines=[{'y': 112, 'from': 1, 'to': 3, 'label': '0%', 'cls': 'ak-res'},
                                                             {'y': 103.6, 'from': 1, 'to': 3, 'label': '38.2%', 'cls': 'ak-sup'},
                                                             {'y': 101, 'from': 1, 'to': 3, 'label': '50%'},
                                                             {'y': 98.4, 'from': 1, 'to': 3, 'label': '61.8%'},
                                                             {'y': 90, 'from': 0, 'to': 3, 'label': '100%'}])
    P['divergence'] = _p([95, 104, 99, 107, 101, 109, 97], per=3, lines=[{'i1': 1, 'y1': 104.4, 'i2': 5, 'y2': 109.4, 'label': 'Higher highs…', 'cls': 'ak-res'}],
                         notes=[(5, '…RSI makes lower highs', 'above')])
    P['ma_cross'] = _p([110, 104, 98, 96, 100, 106, 112], per=3, notes=[(3, 'Fast MA crosses up')])
    for k, v in P.items():
        v.setdefault('title', k.replace('_', ' '))
    return P


_CACHE = {}


def picture(key):
    """SVG markup for a picture key, or ''."""
    if not _CACHE:
        _CACHE.update(candle_pics())
        _CACHE.update(pattern_pics())
    pic = _CACHE.get(key)
    return svg(pic) if pic else ''


def keys():
    picture('anatomy')
    return set(_CACHE)
