# store/coach_card.py
"""
Images that look like the Market Overview analysis panel: the verdict, the entry coach (headline, what to do now,
quality, pullback gauge), the chart with the plan's levels, the checklist and how many strategies agree.

Used for the Telegram signal photo, the TikTok posters and the "wait / get ready" updates.

    view = build_view('XAUUSD', 'intraday')            # same data as /api/ai-scrutiny/
    png  = render(view)                                 # 1080×1600 (Telegram)
    png  = render(view, tall=True)                      # 1080×1920 (TikTok)
    png  = render(view, signal=sig)                     # a recorded signal: its own entry / stop / targets
    png  = render(view, signal=sig, result=True)        # the same, with TARGET HIT / STOPPED OUT
"""
import io
import logging
import time
from datetime import datetime, timezone as dt_tz

from django.conf import settings

from .poster import font, _digits, BG, PANEL, LINE, TEXT, MUTE, UP, DN, AMBER, UP2

logger = logging.getLogger(__name__)

PANEL2 = (26, 32, 45)
ACTION = {   # verdict → (big word, colour); BUY / SELL colour is set from the side
    'ENTER': ('{SIDE} NOW', None),
    'GET_READY': ('GET READY', AMBER),
    'WAIT_PULLBACK': ('WAIT', AMBER),
    'WAIT_NEWS': ('WAIT · NEWS', AMBER),
    'CONFLICT': ('CAUTION', DN),
    'WATCH': ('WATCH', MUTE),
    'STAND_ASIDE': ('STAND ASIDE', MUTE),
}
STATE_LABEL = {'READY': 'Entry ready', 'IN_ZONE': 'In zone', 'WAIT_PULLBACK': 'Pulling back', 'EXTENDED': 'Extended',
               'MISSED': 'Missed', 'INVALID': 'Broken', 'NO_TREND': 'No trend'}
STATE_COLOR = {'READY': UP, 'IN_ZONE': AMBER, 'WAIT_PULLBACK': TEXT, 'EXTENDED': MUTE, 'MISSED': MUTE,
               'INVALID': DN, 'NO_TREND': MUTE}
CHECK = {'pass': ('✓', UP), 'warn': ('!', AMBER), 'fail': ('✕', DN), 'info': ('i', MUTE)}


# ═══════════════════════════════════════════════════════════════
# DATA (the same as the Market Overview scrutiny API)
# ═══════════════════════════════════════════════════════════════
def build_view(symbol, style='intraday'):
    """→ {symbol, style, tf, price, coach, verdict, candles, name} or None when the market has no data."""
    from . import chart_scanner as cs, mt5_service
    symbol = symbol.upper()
    scr = cs.scrutiny(symbol, style)
    if not scr or not scr.get('ok'):
        return None
    strategies = [dict(x) for x in scr['strategies']]
    block = None
    try:
        from .views import _fundamentals
        fund = _fundamentals(symbol, mt5_service.SPECS.get(symbol))
        if fund.get('applies'):
            has = fund.get('feed_ok') and fund.get('released')
            strategies.append(cs._vote('fundamentals', fund.get('vote') if has else None, fund['text'], conf=0.7 if has else 0.3))
        block = fund.get('block')
    except Exception as e:
        logger.info(f'coach card: fundamentals for {symbol} unavailable ({e})')
    verdict = cs.scrutiny_verdict(strategies, scr.get('coach'), news_block=block)
    try:
        candles = cs.candles_for(symbol, scr['main_tf'])['candles']
    except Exception:
        candles = []
    spec = mt5_service.SPECS.get(symbol)
    return {'symbol': symbol, 'style': scr.get('style', style), 'tf': scr['main_tf'], 'price': scr.get('price'),
            'coach': scr.get('coach') or {}, 'verdict': verdict, 'candles': candles,
            'name': getattr(spec, 'name', '') or symbol, 'news': block}


# ═══════════════════════════════════════════════════════════════
# DRAWING
# ═══════════════════════════════════════════════════════════════
def _wrap(d, text, size, width, bold=False, max_lines=4):
    words, lines, cur = str(text or '').split(), [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if d.textlength(t, font=font(size, bold)) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and ' '.join(lines) != ' '.join(words):
        lines[-1] = lines[-1].rstrip('.,;') + '…'
    return lines


def _pill(d, x, y, text, fg, bg=None, outline=None, size=24, h=44):
    w = d.textlength(text, font=font(size)) + 32
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=bg, outline=outline, width=2 if outline else 0)
    d.text((x + w / 2, y + h / 2), text, font=font(size), fill=fg, anchor='mm')
    return x + w


def _gauge(d, box, retrace, deepest=None):
    """Pullback depth bar like the site's gauge: 0 … 100 %, entry zone 38.2–78.6, golden 50–61.8."""
    x0, y0, x1, y1 = box
    X = lambda r: x0 + (x1 - x0) * max(0.0, min(1.0, r))
    d.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) / 2, fill=PANEL2)
    d.rectangle([X(0.382), y0, X(0.786), y1], fill=(64, 52, 18))
    d.rectangle([X(0.5), y0, X(0.618), y1], fill=(150, 112, 10))
    for r, lab in ((0, '0%'), (0.382, '38.2'), (0.5, '50'), (0.618, '61.8'), (0.786, '78.6'), (1, '100%')):
        d.text((X(r), y1 + 20), lab, font=font(20, False), fill=MUTE, anchor='mm')
    if deepest is not None:
        d.rectangle([X(deepest) - 2, y0 - 6, X(deepest) + 2, y1 + 6], fill=MUTE)
    if retrace is not None:
        d.rounded_rectangle([X(retrace) - 4, y0 - 14, X(retrace) + 4, y1 + 14], radius=3, fill=TEXT)
        d.text((X(retrace), y0 - 30), f'now {retrace * 100:.0f}%', font=font(22), fill=TEXT, anchor='mm')


TV_UP, TV_DN = (38, 166, 154), (239, 83, 80)         # TradingView candle colours


def tv_chart(img, box, candles, side, entry, sl, tp, tp2, digits, start_time=None, end_time=None):
    """TradingView-style chart with the Long / Short Position tool: profit zone (entry → target) in green,
    risk zone (entry → stop) in red, from the entry candle to the right, with the usual labels."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=24, fill=(19, 23, 34))
    axis_w = 150
    cx0, cy0, cx1, cy1 = x0 + 16, y0 + 24, x1 - axis_w, y1 - 24
    bars = [c for c in candles if c.get('high') is not None][-60:]
    if not bars:
        return
    future = 24                                      # empty space on the right, like TradingView
    n = len(bars) + future
    step = (cx1 - cx0) / n
    prices = [float(v) for v in (entry, sl, tp, tp2) if v is not None]
    lo = min([float(c['low']) for c in bars] + prices)
    hi = max([float(c['high']) for c in bars] + prices)
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.06, hi + span * 0.06
    Y = lambda p: cy0 + (hi - float(p)) / (hi - lo) * (cy1 - cy0)
    X = lambda i: cx0 + i * step + step / 2
    fmt = lambda v: f'{v:,.{digits}f}'

    # grid + price scale
    for k in range(7):
        pv = hi - k * (hi - lo) / 6
        yy = Y(pv)
        d.line([(cx0, yy), (x1 - axis_w + 4, yy)], fill=(30, 35, 48), width=1)
        d.text((x1 - axis_w + 14, yy), fmt(pv), font=font(19, False), fill=MUTE, anchor='lm')
    for k in range(1, 6):
        xx = cx0 + k * (cx1 - cx0) / 6
        d.line([(xx, cy0), (xx, cy1)], fill=(26, 31, 43), width=1)
    d.line([(x1 - axis_w + 4, cy0 - 10), (x1 - axis_w + 4, cy1 + 10)], fill=(42, 48, 64), width=1)

    # position box (drawn under the candles, translucent)
    if entry is not None and tp is not None and sl is not None:
        idx = max(0, len(bars) - 6)                   # live setup: the box starts a few candles back, like a drawn tool
        if start_time:
            for i, c in enumerate(bars):
                if c.get('time') and c['time'] >= start_time:
                    idx = i
                    break
        end_idx = n - 1
        if end_time:
            for i, c in enumerate(bars):
                if c.get('time') and c['time'] >= end_time:
                    end_idx = max(i, idx + 1)
                    break
        bx0, bx1 = X(idx) - step / 2, min(X(end_idx) + step / 2, cx1)
        over = Image.new('RGBA', img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(over)
        od.rectangle([bx0, min(Y(entry), Y(tp)), bx1, max(Y(entry), Y(tp))], fill=TV_UP + (64,))
        od.rectangle([bx0, min(Y(entry), Y(sl)), bx1, max(Y(entry), Y(sl))], fill=TV_DN + (64,))
        if tp2 is not None:
            od.rectangle([bx0, min(Y(tp), Y(tp2)), bx1, max(Y(tp), Y(tp2))], fill=TV_UP + (30,))
        img.alpha_composite(over) if img.mode == 'RGBA' else img.paste(Image.alpha_composite(img.convert('RGBA'), over).convert('RGB'))
        d = ImageDraw.Draw(img)
        d.line([(bx0, Y(entry)), (bx1, Y(entry))], fill=(120, 123, 134), width=2)
        d.line([(bx0, Y(tp)), (bx1, Y(tp))], fill=TV_UP, width=2)
        d.line([(bx0, Y(sl)), (bx1, Y(sl))], fill=TV_DN, width=2)
        if tp2 is not None:
            for xs in range(int(bx0), int(bx1), 14):
                d.line([(xs, Y(tp2)), (min(xs + 7, bx1), Y(tp2))], fill=TV_UP, width=2)
        risk = abs(entry - sl) or 1e-12
        pct = lambda v: abs(v - entry) / entry * 100 if entry else 0

        def centre(w):                                 # keep the label inside the chart area
            return min(max((bx0 + bx1) / 2, cx0 + w / 2 + 4), cx1 - w / 2 - 4)

        def tag(yc, text, bg, above):
            w = d.textlength(text, font=font(20)) + 22
            mid = centre(w)
            ty = yc - 34 if above else yc + 8
            d.rounded_rectangle([mid - w / 2, ty, mid + w / 2, ty + 28], radius=5, fill=bg)
            d.text((mid, ty + 14), text, font=font(20), fill=(255, 255, 255), anchor='mm')
        long_ = side == 'BUY'
        tag(Y(tp), f'Target: {fmt(tp)} ({pct(tp):.2f}%)', TV_UP, long_)
        tag(Y(sl), f'Stop: {fmt(sl)} ({pct(sl):.2f}%)', TV_DN, not long_)
        rr = abs(tp - entry) / risk
        mid_txt = f"{'Long' if long_ else 'Short'} Position · R:R {rr:.2f}"
        w = d.textlength(mid_txt, font=font(20)) + 22
        mid = centre(w)
        d.rounded_rectangle([mid - w / 2, Y(entry) - 14, mid + w / 2, Y(entry) + 14], radius=5, fill=(42, 46, 57))
        d.text((mid, Y(entry)), mid_txt, font=font(20), fill=(255, 255, 255), anchor='mm')
        axis_tags = [(tp, TV_UP), (entry, (120, 123, 134)), (sl, TV_DN)]
    else:
        axis_tags = []

    # candles
    for i, c in enumerate(bars):
        o, h_, l_, cl = (float(c[k]) for k in ('open', 'high', 'low', 'close'))
        x = X(i)
        col = TV_UP if cl >= o else TV_DN
        d.line([(x, Y(h_)), (x, Y(l_))], fill=col, width=2)
        top, bot = Y(max(o, cl)), Y(min(o, cl))
        d.rectangle([x - step * 0.36, top, x + step * 0.36, max(bot, top + 2)], fill=col)

    # price-scale tags (position levels + last price), nudged apart so none overlap
    last = float(bars[-1]['close'])
    axis_tags.append((last, TV_UP if last >= float(bars[-1]['open']) else TV_DN))
    for xs in range(int(cx0), int(x1 - axis_w), 10):
        d.line([(xs, Y(last)), (min(xs + 4, x1 - axis_w), Y(last))], fill=axis_tags[-1][1], width=1)
    placed = []
    for v, col in sorted(axis_tags, key=lambda t: Y(t[0])):
        yy = Y(v)
        if placed and yy - placed[-1] < 32:
            yy = placed[-1] + 32
        placed.append(yy)
        d.rectangle([x1 - axis_w + 6, yy - 15, x1 - 8, yy + 15], fill=col)
        d.text((x1 - axis_w + 14, yy), fmt(v), font=font(19), fill=(255, 255, 255), anchor='lm')


def render(view, tall=False, signal=None, result=False):
    """PNG bytes. `signal` (a ScannerSignal) replaces the coach plan with the recorded levels."""
    from PIL import Image, ImageDraw
    W, H = 1080, (1920 if tall else 1600)
    sym = view['symbol']
    dg = _digits(sym)
    fmt = lambda v: '—' if v is None else f'{v:,.{dg}f}'
    coach, verdict = view.get('coach') or {}, view.get('verdict') or {}
    plan = dict(coach.get('plan') or {})
    if signal is not None:
        plan.update(side=signal.direction, entry=signal.entry, sl=signal.stop_loss, tp1=signal.take_profit,
                    tp2=signal.take_profit_2, rr=signal.rr)
    side = (signal.direction if signal is not None else verdict.get('side')) or plan.get('side') or 'BUY'
    side_col = UP if side == 'BUY' else DN

    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    pad = 56

    # ── header ──
    d.rectangle([0, 0, W, 8], fill=AMBER)
    d.rounded_rectangle([pad, 40, pad + 60, 100], radius=14, fill=AMBER)
    d.text((pad + 30, 70), 'P', font=font(40), fill=BG, anchor='mm')
    d.text((pad + 78, 46), 'PATREO', font=font(36), fill=TEXT)
    d.text((pad + 78 + d.textlength('PATREO', font=font(36)), 46), 'AIX', font=font(36), fill=AMBER)
    d.text((pad + 80, 92), 'AI MARKET ANALYST', font=font(18), fill=MUTE)
    when = datetime.now(dt_tz.utc)
    d.text((W - pad, 50), f'{when:%d %b %Y}', font=font(24), fill=TEXT, anchor='ra')
    d.text((W - pad, 82), f'{when:%H:%M} UTC', font=font(22, False), fill=MUTE, anchor='ra')

    # ── market ──
    y = 140
    d.text((pad, y), sym, font=font(84), fill=TEXT)
    sub = f"{view['tf'].upper()} · {view.get('style', 'intraday').upper()}" + (f" · {signal.grade}-GRADE SIGNAL" if signal is not None else '')
    d.text((pad + 4, y + 104), sub, font=font(26, False), fill=MUTE)
    if view.get('price') is not None:
        d.text((W - pad, y + 18), fmt(view['price']), font=font(46), fill=TEXT, anchor='ra')
        d.text((W - pad, y + 80), 'price', font=font(22, False), fill=MUTE, anchor='ra')
    y += 160

    # ── verdict banner ──
    if result and signal is not None and signal.outcome in ('TP', 'SL', 'EXPIRED'):
        word, col = {'TP': ('TARGET HIT', UP), 'SL': ('STOPPED OUT', DN), 'EXPIRED': ('EXPIRED', MUTE)}[signal.outcome]
        line2 = f"{signal.direction} from {fmt(signal.entry)} · result {(signal.result_r or 0):+.2f}R"
        right = f'{(signal.result_r or 0):+.2f}R'
    elif signal is not None:
        word, col = f'{side} SIGNAL', side_col
        line2 = verdict.get('headline') or coach.get('headline') or ''
        right = f"{verdict.get('conviction', 0)}%"
    else:
        word, col = ACTION.get(verdict.get('action'), ('WATCH', MUTE))
        word = word.replace('{SIDE}', side)
        col = col or side_col
        line2 = verdict.get('headline') or ''
        right = f"{verdict.get('conviction', 0)}%"
    bh = 150
    d.rounded_rectangle([pad, y, W - pad, y + bh], radius=24, fill=PANEL, outline=col, width=3)
    d.rounded_rectangle([pad, y, pad + 16, y + bh], radius=8, fill=col)
    d.text((pad + 44, y + 22), word, font=font(56), fill=col)
    d.text((pad + 46, y + 98), (_wrap(d, line2, 26, W - 2 * pad - 260, max_lines=1) or [''])[0], font=font(26, False), fill=TEXT)
    d.text((W - pad - 34, y + 50), right, font=font(52), fill=TEXT, anchor='ra')
    d.text((W - pad - 34, y + 112), 'result' if result else 'conviction', font=font(20, False), fill=MUTE, anchor='ra')
    y += bh + 26

    # ── entry coach ──
    state = coach.get('state') or 'NO_TREND'
    d.rounded_rectangle([pad, y, W - pad, y + 64], radius=14, fill=PANEL)
    d.text((pad + 24, y + 32), 'ENTRY COACH', font=font(22), fill=MUTE, anchor='lm')
    x = pad + 24 + d.textlength('ENTRY COACH', font=font(22)) + 20
    x = _pill(d, x, y + 12, STATE_LABEL.get(state, state), STATE_COLOR.get(state, TEXT), bg=PANEL2, size=22, h=40)
    d.text((W - pad - 24, y + 32), f"quality {coach.get('quality', 0)}/100", font=font(24), fill=TEXT, anchor='rm')
    y += 80
    instr = coach.get('instruction') or coach.get('headline') or ''
    if signal is not None:
        rr = f" ({plan['rr']:.1f}R)" if plan.get('rr') else ''
        if result and signal.outcome in ('TP', 'SL', 'EXPIRED'):
            how = {'TP': f"reached the target {fmt(signal.take_profit)}", 'SL': f"hit the stop {fmt(signal.stop_loss)}",
                   'EXPIRED': 'expired without reaching the target or the stop'}[signal.outcome]
            instr = f"{signal.direction} from {fmt(signal.entry)} {how}. Result {(signal.result_r or 0):+.2f}R."
        else:
            instr = (f"{signal.direction} {sym} at {fmt(signal.entry)}. Stop {fmt(signal.stop_loss)}, "
                     f"target {fmt(signal.take_profit)}{rr}" + (f", then {fmt(signal.take_profit_2)}." if signal.take_profit_2 else '.'))
    lines = _wrap(d, instr, 30, W - 2 * pad - 70, bold=True, max_lines=3 if not tall else 4)
    ih = 34 + 42 * len(lines)
    icol = {'READY': UP, 'IN_ZONE': AMBER, 'WAIT_PULLBACK': AMBER, 'INVALID': DN}.get(state, MUTE)
    d.rounded_rectangle([pad, y, W - pad, y + ih], radius=16, fill=PANEL2)
    d.rectangle([pad, y + 8, pad + 6, y + ih - 8], fill=icol)
    for k, ln in enumerate(lines):
        d.text((pad + 34, y + 18 + 42 * k), ln, font=font(30), fill=TEXT)
    y += ih + 26

    if coach.get('retrace') is not None and not result:
        y += 34
        _gauge(d, (pad + 10, y, W - pad - 10, y + 20), coach.get('retrace'), coach.get('retrace_deepest'))
        y += 66

    # ── levels row ──
    if plan.get('entry') is not None:
        cells = [('ENTRY', plan.get('entry'), AMBER), ('STOP', plan.get('sl'), DN), ('TARGET', plan.get('tp1'), UP)]
        if plan.get('tp2') is not None:
            cells.append(('TARGET 2', plan.get('tp2'), UP2))
        cw = (W - 2 * pad - 12 * (len(cells) - 1)) / len(cells)
        for k, (lab, v, c) in enumerate(cells):
            cx = pad + k * (cw + 12)
            d.rounded_rectangle([cx, y, cx + cw, y + 104], radius=16, fill=PANEL)
            d.text((cx + cw / 2, y + 30), lab, font=font(20), fill=MUTE, anchor='mm')
            d.text((cx + cw / 2, y + 72), fmt(v), font=font(34 if len(cells) == 4 else 38), fill=c, anchor='mm')
        y += 120

    # ── bottom: checklist + strategies + footer (fixed), chart takes the space left ──
    checks = [c for c in (coach.get('checks') or []) if c.get('status') != 'info'][:6]
    room = H - y - 110 - 90 - 24                       # footer, strategies bar, gaps
    n = min(len(checks), max(0, int((room - (700 if tall else 520)) // 60)))  # the chart comes first
    chart_h = room - 60 * n - 20
    if chart_h < 220 or not view.get('candles'):
        n = min(len(checks), max(0, int(room // 60)))
        chart_h = 0
    checks = checks[:n]
    if chart_h:
        start = signal.bar_time if signal is not None else None
        end = int(signal.closed_at.timestamp()) if (result and signal is not None and signal.closed_at) else None
        tv_chart(img, (pad, y, W - pad, y + chart_h), view['candles'], side, plan.get('entry'), plan.get('sl'),
                 plan.get('tp1'), plan.get('tp2'), dg, start_time=start, end_time=end)
        d = ImageDraw.Draw(img)
        y += chart_h + 20

    for c in checks:
        mark, mc = CHECK.get(c.get('status'), ('i', MUTE))
        d.ellipse([pad, y + 6, pad + 40, y + 46], fill=PANEL2)
        d.text((pad + 20, y + 26), mark, font=font(24), fill=mc, anchor='mm')
        label = c.get('label', '')
        d.text((pad + 58, y + 26), label, font=font(26), fill=TEXT, anchor='lm')
        lx = pad + 58 + d.textlength(label, font=font(26)) + 16
        rest = (_wrap(d, c.get('text', ''), 22, W - pad - lx, max_lines=1) or [''])[0]
        d.text((lx, y + 27), rest, font=font(22, False), fill=MUTE, anchor='lm')
        y += 60
    y += 24

    agree, against, voting = len(verdict.get('agree') or []), len(verdict.get('against') or []), verdict.get('voting') or 0
    if voting:
        d.rounded_rectangle([pad, y, W - pad, y + 70], radius=16, fill=PANEL)
        bar_x0, bar_x1 = pad + 24, W - pad - 330
        d.rounded_rectangle([bar_x0, y + 28, bar_x1, y + 42], radius=7, fill=PANEL2)
        d.rounded_rectangle([bar_x0, y + 28, bar_x0 + (bar_x1 - bar_x0) * agree / voting, y + 42], radius=7, fill=side_col)
        d.text((W - pad - 24, y + 35), f'{agree}/{voting} strategies agree · {against} against', font=font(22), fill=TEXT, anchor='rm')
    d.text((W / 2, H - 70), f"Free AI analysis · {(getattr(settings, 'SITE_DOMAIN', '') or 'patreoaix.com').upper()}",
           font=font(28), fill=AMBER, anchor='mm')
    d.text((W / 2, H - 32), 'Not financial advice. Trade small, always use a stop.', font=font(20, False), fill=MUTE, anchor='mm')
    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    return out.getvalue()


def for_signal(sig, tall=False, result=False):
    """Market-Overview-style image for a recorded signal, or None if the analysis can't be built."""
    try:
        view = build_view(sig.symbol, sig.style)
        if view:
            return render(view, tall=tall, signal=sig, result=result)
    except Exception as e:
        logger.warning(f'coach card for signal {sig.id} failed: {e}')
    return None
