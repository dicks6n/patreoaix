# store/news_card.py
"""Images for big releases: the playbook before (history + timing) and the result after (how the chart went)."""
import io

from django.conf import settings

from .poster import font, BG, PANEL, TEXT, MUTE, UP, DN, AMBER

PANEL2 = (26, 32, 45)


def _header(d, W, title, sub):
    d.rectangle([0, 0, W, 8], fill=AMBER)
    d.text((56, 40), 'PATREO', font=font(34), fill=TEXT)
    d.text((56 + d.textlength('PATREO', font=font(34)), 40), 'AIX', font=font(34), fill=AMBER)
    d.text((W - 56, 48), 'NEWS PLAYBOOK', font=font(22), fill=AMBER, anchor='ra')
    d.text((56, 110), title, font=font(54), fill=TEXT)
    d.text((58, 178), sub, font=font(26, False), fill=MUTE)


def _paths(d, box, rows, inverse):
    """The price path after each past release (% from the pre-release price), coloured by the surprise."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=20, fill=PANEL)
    paths = [r for r in rows if r.get('path')]
    if not paths:
        d.text(((x0 + x1) / 2, (y0 + y1) / 2), 'No price history yet', font=font(26, False), fill=MUTE, anchor='mm')
        return
    lo = min(min(p['path'] + [0]) for p in paths)
    hi = max(max(p['path'] + [0]) for p in paths)
    span = (hi - lo) or 0.1
    lo, hi = lo - span * 0.1, hi + span * 0.1
    cx0, cx1, cy0, cy1 = x0 + 70, x1 - 24, y0 + 44, y1 - 40
    Y = lambda v: cy0 + (hi - v) / (hi - lo) * (cy1 - cy0)
    X = lambda i: cx0 + i / 15 * (cx1 - cx0)
    d.line([(cx0, Y(0)), (cx1, Y(0))], fill=(70, 78, 96), width=2)
    for k, lab in ((0, 'release'), (4, '+1h'), (8, '+2h'), (15, '+4h')):
        d.line([(X(k), cy0), (X(k), cy1)], fill=(34, 41, 56), width=1)
        d.text((X(k), cy1 + 18), lab, font=font(18, False), fill=MUTE, anchor='mm')
    for v in (hi, 0, lo):
        d.text((x0 + 14, Y(v)), f'{v:+.2f}%', font=font(16, False), fill=MUTE, anchor='lm')
    for p in reversed(paths[:8]):
        col = UP if p['surprise'] > 0 else DN if p['surprise'] < 0 else MUTE
        pts = [(X(i), Y(v)) for i, v in enumerate([0] + p['path'][:15])]
        d.line(pts, fill=col, width=3, joint='curve')
    d.text((x0 + 20, y0 + 14), 'Price path after the last releases', font=font(20), fill=TEXT)
    d.text((x1 - 20, y0 + 14), 'green = better than forecast · red = worse', font=font(17, False), fill=MUTE, anchor='ra')


def playbook_png(pb):
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    wl = pb['when_local']
    _header(d, W, pb['label'], f"{wl:%A %d %b · %H:%M} (Nairobi)" + (f" · forecast {pb['forecast']} · previous {pb['previous']}" if pb.get('forecast') else ''))
    y = 230
    stats = pb['stats'][:4]
    if not stats:
        d.text((W / 2, 400), 'History is still loading for this release.', font=font(30, False), fill=MUTE, anchor='mm')
    cw = (W - 112 - 12 * (len(stats) - 1)) / max(1, len(stats))
    for i, st in enumerate(stats):
        x = 56 + i * (cw + 12)
        d.rounded_rectangle([x, y, x + cw, y + 170], radius=18, fill=PANEL)
        d.text((x + cw / 2, y + 30), st['symbol'], font=font(26), fill=AMBER, anchor='mm')
        d.text((x + cw / 2, y + 78), f"±{(st['abs_h1'] or 0):.2f}%", font=font(40), fill=TEXT, anchor='mm')
        d.text((x + cw / 2, y + 118), 'avg move in 1 h', font=font(17, False), fill=MUTE, anchor='mm')
        d.text((x + cw / 2, y + 146), f"{st['n']} releases", font=font(17, False), fill=MUTE, anchor='mm')
    y += 196
    lead = stats[0] if stats else None
    if lead:
        lines = []
        for name, side in (('Better than forecast', lead.get('beat')), ('Worse than forecast', lead.get('miss'))):
            if side and side['n'] >= 3:
                col = UP if (side['avg_h1'] or 0) > 0 else DN
                lines.append((name, f"{lead['symbol']} {side['avg_h1']:+.2f}% in 1 h · {side['same_way'] * 100:.0f}% of {side['n']}", col))
        for name, txt, col in lines:
            d.rounded_rectangle([56, y, W - 56, y + 74], radius=16, fill=PANEL)
            d.text((84, y + 37), name, font=font(26), fill=TEXT, anchor='lm')
            d.text((W - 84, y + 37), txt, font=font(24), fill=col, anchor='rm')
            y += 86
        _paths(d, (56, y + 6, W - 56, y + 420), lead['last'], None)
        y += 446
    # timing
    from .coach_card import _wrap
    tl = _wrap(d, 'WHEN TO EXECUTE: ' + pb['timing'], 26, W - 150, bold=True, max_lines=4)
    h = 30 + 36 * len(tl)
    d.rounded_rectangle([56, y, W - 56, y + h], radius=16, fill=PANEL2)
    d.rectangle([56, y + 8, 62, y + h - 8], fill=AMBER)
    for k, ln in enumerate(tl):
        d.text((86, y + 16 + 36 * k), ln, font=font(26), fill=TEXT)
    d.text((W / 2, H - 70), f"AI news analysis · {getattr(settings, 'PUBLIC_HOST', '') or 'www.patreoaix.com'}".upper(),
           font=font(26), fill=AMBER, anchor='mm')
    d.text((W / 2, H - 32), 'History is not a promise. Spreads widen at the release: trade small.', font=font(19, False), fill=MUTE, anchor='mm')
    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    return out.getvalue()
