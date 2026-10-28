# store/poster.py
"""
Vertical 1080×1920 signal posters for TikTok / Instagram / WhatsApp status.

Every new A / A+ signal gets a poster (chart + entry / stop / targets), and when it closes a second one
with the result. They are saved under media/posters/<date>/ and listed at /posters/ (staff), where each
can be downloaded together with a ready caption and hashtags.

    python manage.py make_posters --open      → posters for the signals open now
    python manage.py make_posters --id 12     → poster for one signal (its result poster too if it closed)
"""
import io
import logging
import os
from datetime import datetime, timezone as dt_tz

from django.conf import settings

logger = logging.getLogger(__name__)

W, H = 1080, 1920
BG, PANEL, LINE = (9, 12, 18), (18, 23, 33), (34, 41, 56)
TEXT, MUTE = (236, 239, 244), (128, 137, 155)
UP, DN, AMBER, UP2 = (22, 199, 132), (234, 57, 67), (240, 185, 11), (16, 150, 100)

FONT_FILES = {
    True: ['DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
           '/System/Library/Fonts/Supplemental/Arial Bold.ttf', '/Library/Fonts/Arial Bold.ttf',
           'C:/Windows/Fonts/arialbd.ttf'],
    False: ['DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/System/Library/Fonts/Supplemental/Arial.ttf', '/Library/Fonts/Arial.ttf',
            'C:/Windows/Fonts/arial.ttf'],
}
_fonts = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _fonts:
        from PIL import ImageFont
        for path in FONT_FILES[bold]:
            try:
                _fonts[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            try:
                _fonts[key] = ImageFont.load_default(size=size)
            except TypeError:
                _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def _digits(symbol):
    from . import mt5_service
    spec = mt5_service.SPECS.get(symbol)
    return spec.digits if spec else 5


def _text(d, xy, s, size, fill, bold=True, anchor='la'):
    d.text(xy, s, fill=fill, font=font(size, bold), anchor=anchor)


def _chart(d, box, candles, levels, digits):
    """Candles + dashed level lines with price tags inside box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=28, fill=PANEL)
    tagw = 250
    cx0, cy0, cx1, cy1 = x0 + 24, y0 + 30, x1 - tagw - 10, y1 - 30
    bars = [c for c in candles if c.get('high') is not None][-60:]
    prices = [float(v) for v, _, _ in levels if v is not None]
    lo = min([float(c['low']) for c in bars] + prices)
    hi = max([float(c['high']) for c in bars] + prices)
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.05, hi + span * 0.05
    Y = lambda p: cy0 + (hi - float(p)) / (hi - lo) * (cy1 - cy0)
    for k in range(5):
        y = cy0 + k * (cy1 - cy0) / 4
        d.line([(cx0, y), (cx1, y)], fill=LINE, width=1)
    step = (cx1 - cx0) / max(len(bars), 1)
    for i, c in enumerate(bars):
        o, h_, l_, cl = (float(c[k]) for k in ('open', 'high', 'low', 'close'))
        x = cx0 + i * step + step / 2
        col = UP if cl >= o else DN
        d.line([(x, Y(h_)), (x, Y(l_))], fill=col, width=2)
        top, bot = Y(max(o, cl)), Y(min(o, cl))
        d.rectangle([x - step * 0.33, top, x + step * 0.33, max(bot, top + 2)], fill=col)
    placed = []
    for price, color, label in levels:                       # ENTRY last so it is drawn on top
        if price is None:
            continue
        y = Y(price)
        for xs in range(int(cx0), int(cx1), 18):
            d.line([(xs, y), (min(xs + 10, cx1), y)], fill=color, width=3)
        ty = y
        for py in placed:                                     # keep tags from overlapping
            if abs(ty - py) < 44:
                ty = py + (44 if ty >= py else -44)
        placed.append(ty)
        d.rounded_rectangle([x1 - tagw, ty - 20, x1 - 14, ty + 20], radius=10, fill=color)
        _text(d, (x1 - tagw + 12, ty), f'{label} {price:.{digits}f}', 24, (8, 10, 14), anchor='lm')


def render_poster(sig, candles=None, result=False):
    """PNG bytes. result=True draws the closed-trade version (TARGET HIT / STOPPED OUT / EXPIRED)."""
    from PIL import Image, ImageDraw
    if candles is None:
        try:
            from . import chart_scanner
            candles = chart_scanner.candles_for(sig.symbol, sig.timeframe)['candles']
        except Exception:
            candles = []
    dg = _digits(sig.symbol)
    buy = sig.direction == 'BUY'
    col = UP if buy else DN
    f = lambda v: '—' if v is None else f'{v:,.{dg}f}'
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 10], fill=col)

    # header
    d.rounded_rectangle([60, 70, 136, 146], radius=18, fill=AMBER)
    _text(d, (98, 108), 'P', 50, BG, anchor='mm')
    _text(d, (158, 82), 'PATREO', 46, TEXT)
    _text(d, (158 + d.textlength('PATREO', font=font(46)), 82), 'AIX', 46, AMBER)
    when = (sig.closed_at if result and sig.closed_at else sig.created_at) or datetime.now(dt_tz.utc)
    _text(d, (W - 60, 92), 'AI SIGNAL', 30, AMBER, anchor='ra')
    _text(d, (W - 60, 130), f'{when:%d %b %Y · %H:%M} UTC', 24, MUTE, bold=False, anchor='ra')

    # direction + market
    _text(d, (60, 210), sig.direction, 190, col)
    _text(d, (60, 420), sig.symbol, 110, TEXT)
    tag = f'{sig.timeframe.upper()} · {sig.style.upper()}'
    _text(d, (64, 548), tag, 34, MUTE, bold=False)
    gx = 64 + d.textlength(tag, font=font(34, False)) + 26
    d.rounded_rectangle([gx, 540, gx + 120, 592], radius=12, outline=AMBER, width=3)
    _text(d, (gx + 60, 566), sig.grade, 32, AMBER, anchor='mm')

    # result banner (closed version)
    top = 630
    if result and sig.outcome in ('TP', 'SL', 'EXPIRED'):
        word, bcol = {'TP': ('TARGET HIT', UP), 'SL': ('STOPPED OUT', DN), 'EXPIRED': ('EXPIRED', MUTE)}[sig.outcome]
        d.rounded_rectangle([60, top, W - 60, top + 130], radius=24, fill=bcol)
        _text(d, (100, top + 65), word, 64, BG, anchor='lm')
        _text(d, (W - 100, top + 65), f'{(sig.result_r or 0):+.2f}R', 64, BG, anchor='rm')
        top += 160

    # chart: whatever height is left above the level rows, stats and footer
    rows = [('ENTRY', sig.entry, AMBER), ('STOP LOSS', sig.stop_loss, DN), ('TAKE PROFIT', sig.take_profit, UP)]
    if sig.take_profit_2:
        rows.append(('TAKE PROFIT 2', sig.take_profit_2, UP2))
    rh, stats_h, footer_top = (94 if len(rows) == 4 else 110), 130, H - 190
    chart_h = footer_top - top - 30 - len(rows) * rh - 20 - stats_h
    levels = [(sig.take_profit_2, UP2, 'TP2'), (sig.take_profit, UP, 'TP'), (sig.stop_loss, DN, 'SL'), (sig.entry, AMBER, 'ENTRY')]
    _chart(d, (60, top, W - 60, top + chart_h), candles or [], levels, dg)
    y = top + chart_h + 30

    # level rows
    for label, v, c in rows:
        d.rounded_rectangle([60, y, W - 60, y + rh - 16], radius=20, fill=PANEL)
        d.rounded_rectangle([60, y, 74, y + rh - 16], radius=6, fill=c)
        _text(d, (104, y + (rh - 16) / 2), label, 34, MUTE, anchor='lm')
        _text(d, (W - 96, y + (rh - 16) / 2), f(v), 52, c, anchor='rm')
        y += rh

    # stats
    agree = len(sig.agree or [])
    stats = [('RISK : REWARD', f'1 : {sig.rr:.2f}' if sig.rr else '—'), ('SCANNERS AGREE', str(agree) if agree else '—'),
             ('QUALITY', f'{round(sig.quality)}' if sig.quality else '—')]
    cw = (W - 120 - 40) / 3
    for i, (label, val) in enumerate(stats):
        x = 60 + i * (cw + 20)
        d.rounded_rectangle([x, y + 4, x + cw, y + 4 + stats_h], radius=20, fill=PANEL)
        _text(d, (x + cw / 2, y + 44), label, 22, MUTE, anchor='mm')
        _text(d, (x + cw / 2, y + 94), val, 42, TEXT, anchor='mm')

    # footer
    site = (getattr(settings, 'SITE_DOMAIN', '') or 'patreoaix.com').upper()
    _text(d, (W / 2, H - 130), f'More signals: {site}', 34, AMBER, anchor='mm')
    _text(d, (W / 2, H - 76), 'Not financial advice. Trade small, always use a stop.', 26, MUTE, bold=False, anchor='mm')
    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    return out.getvalue()


# ═══════════════════════════════════════════════════════════════
# FILES + CAPTIONS
# ═══════════════════════════════════════════════════════════════
def poster_dir():
    return os.path.join(settings.MEDIA_ROOT, 'posters')


def caption(sig, result=False):
    dg = _digits(sig.symbol)
    f = lambda v: '—' if v is None else f'{v:.{dg}f}'
    tag = sig.symbol.lower().replace('/', '')
    if result:
        word = {'TP': 'Target hit ✅', 'SL': 'Stopped out ❌', 'EXPIRED': 'Expired ⏱'}.get(sig.outcome, sig.outcome)
        head = f'{word} {sig.direction} {sig.symbol} {(sig.result_r or 0):+.2f}R'
    else:
        head = f"{'🟢' if sig.direction == 'BUY' else '🔴'} {sig.grade} {sig.direction} {sig.symbol} {sig.timeframe.upper()}"
    return (f"{head}\nEntry {f(sig.entry)} | SL {f(sig.stop_loss)} | TP {f(sig.take_profit)}\n"
            f"AI signals daily: {getattr(settings, 'SITE_DOMAIN', '') or 'patreoaix.com'}\n"
            f"Not financial advice.\n"
            f"#{tag} #forex #trading #tradingsignals #forexsignals #daytrading #ai #patreoaix")


def save_poster(sig, result=False, candles=None):
    """Render and save one poster; returns its path relative to MEDIA_ROOT."""
    when = (sig.closed_at if result and sig.closed_at else sig.created_at) or datetime.now(dt_tz.utc)
    rel = os.path.join('posters', f'{when:%Y-%m-%d}', f"{sig.id}_{sig.symbol}_{sig.direction}{'_' + sig.outcome if result else ''}.png")
    path = os.path.join(settings.MEDIA_ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        from . import coach_card                               # Market-Overview look; the plain poster if the analysis fails
        fh.write((coach_card.for_signal(sig, tall=True, result=result) if candles is None else None)
                 or render_poster(sig, candles, result))
    with open(path[:-4] + '.txt', 'w', encoding='utf-8') as fh:
        fh.write(caption(sig, result))
    return rel


def recent(limit=60):
    """Newest posters first: [{'url', 'name', 'caption', 'date'}]."""
    root = poster_dir()
    items = []
    if not os.path.isdir(root):
        return items
    for day in sorted(os.listdir(root), reverse=True):
        folder = os.path.join(root, day)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder), key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True):
            if not name.endswith('.png'):
                continue
            txt = os.path.join(folder, name[:-4] + '.txt')
            items.append({'url': f"{settings.MEDIA_URL}posters/{day}/{name}", 'name': name, 'date': day,
                          'caption': open(txt, encoding='utf-8').read() if os.path.exists(txt) else ''})
            if len(items) >= limit:
                return items
    return items


def safe_save(sig, result=False):
    """Never let a poster break the scanner."""
    if str(getattr(settings, 'POSTERS_ENABLED', 'True')) != 'True':
        return None
    try:
        return save_poster(sig, result)
    except Exception as e:
        logger.warning(f'Poster for signal {sig.id} failed: {e}')
        return None
