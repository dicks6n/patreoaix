# store/telegram_bot.py
"""
Posts the AI scanner's strong setups to a Telegram channel, and their results as replies.

Setup (once):
  1. In Telegram, talk to @BotFather → /newbot → copy the token.
  2. Add the bot to your channel as an administrator with "Post messages".
  3. .env:  TELEGRAM_BOT_TOKEN=123456:ABC…   TELEGRAM_CHANNEL_ID=@yourchannel   (or -100… for a private channel)
  4. python manage.py telegram_test        → checks the token and posts a test message

Called from signal_alerts.record_signals / update_outcomes, i.e. inside the background scanner
(manage.py run_background / signal_scanner), so the website never waits on Telegram. Every call has a
short timeout and is wrapped so a Telegram problem can't break the scanner.
"""
import io
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API = 'https://api.telegram.org/bot{token}/{method}'
GRADE_RANK = {'B': 1, 'A': 2, 'A+': 3}


def _cfg(name, default=''):
    return str(getattr(settings, name, '') or default)


def enabled():
    return bool(_cfg('TELEGRAM_BOT_TOKEN') and _cfg('TELEGRAM_CHANNEL_ID')) and _cfg('TELEGRAM_ENABLED', 'True') == 'True'


def api(method, data=None, files=None, timeout=12):
    """One Bot API call → result dict. Raises RuntimeError with Telegram's description on failure."""
    import requests
    url = API.format(token=_cfg('TELEGRAM_BOT_TOKEN'), method=method)
    r = requests.post(url, data=data or {}, files=files, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f'Telegram HTTP {r.status_code}')
    if not body.get('ok'):
        raise RuntimeError(body.get('description') or f'Telegram HTTP {r.status_code}')
    if method.startswith('send'):
        cache.set('tg:last_post', __import__('time').time(), 7 * 86400)   # for the "channel is quiet" check
    return body['result']


# ═══════════════════════════════════════════════════════════════
# CHART IMAGE (Pillow — no extra packages)
# ═══════════════════════════════════════════════════════════════
def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)          # Pillow ≥ 10.1 ships a scalable default font
    except TypeError:
        return ImageFont.load_default()


def render_chart(symbol, timeframe, direction, candles, entry, sl, tp, tp2=None, digits=2, title=None):
    """PNG bytes: the last 70 candles with entry / stop / target lines, PATREOAIX dark style."""
    from PIL import Image, ImageDraw
    W, H, padL, padR, padT, padB = 960, 540, 24, 132, 70, 36
    bg, grid, text, mute = (11, 14, 20), (30, 36, 48), (230, 233, 239), (120, 128, 145)
    up, dn, amber = (22, 199, 132), (234, 57, 67), (240, 185, 11)
    img = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(img)
    bars = [c for c in candles if c.get('high') is not None][-70:]
    levels = [v for v in (entry, sl, tp, tp2) if v is not None]
    lo = min([float(c['low']) for c in bars] + levels) if bars else min(levels)
    hi = max([float(c['high']) for c in bars] + levels) if bars else max(levels)
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.06, hi + span * 0.06
    Y = lambda p: padT + (hi - float(p)) / (hi - lo) * (H - padT - padB)
    n = max(len(bars), 1)
    step = (W - padL - padR) / n
    for k in range(5):                                           # grid
        y = padT + k * (H - padT - padB) / 4
        d.line([(padL, y), (W - padR, y)], fill=grid, width=1)
        d.text((W - padR + 8, y - 8), f'{hi - k * (hi - lo) / 4:.{digits}f}', fill=mute, font=_font(14))
    for i, c in enumerate(bars):                                 # candles
        o, h_, l_, cl = (float(c[k]) for k in ('open', 'high', 'low', 'close'))
        x = padL + i * step + step / 2
        col = up if cl >= o else dn
        d.line([(x, Y(h_)), (x, Y(l_))], fill=col, width=1)
        top, bot = Y(max(o, cl)), Y(min(o, cl))
        d.rectangle([x - step * 0.32, top, x + step * 0.32, max(bot, top + 1)], fill=col)

    def level(price, color, label):
        if price is None:
            return
        y = Y(price)
        for x0 in range(padL, W - padR, 14):                     # dashed line
            d.line([(x0, y), (min(x0 + 8, W - padR), y)], fill=color, width=2)
        tag = f'{label} {price:.{digits}f}'
        d.rounded_rectangle([W - padR + 4, y - 12, W - 6, y + 12], radius=5, fill=color)
        d.text((W - padR + 10, y - 9), tag, fill=(10, 12, 16), font=_font(15))
    level(tp2, (16, 150, 100), 'TP2')
    level(tp, up, 'TP')
    level(sl, dn, 'SL')
    level(entry, amber, 'ENTRY')
    head = title or f"{'BUY' if direction == 'BUY' else 'SELL'}  {symbol}  ·  {timeframe.upper()}"
    d.text((padL, 18), head, fill=up if direction == 'BUY' else dn, font=_font(30))
    d.text((W - 210, 24), 'PATREOAIX AI', fill=amber, font=_font(20))
    d.text((padL, H - 26), 'Not financial advice · manage your risk', fill=mute, font=_font(14))
    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    return out.getvalue()


# ═══════════════════════════════════════════════════════════════
# POSTS
# ═══════════════════════════════════════════════════════════════
def _digits(symbol):
    from . import mt5_service
    spec = mt5_service.SPECS.get(symbol)
    return spec.digits if spec else 5


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def signal_caption(sig, r=None):
    f = lambda v: '—' if v is None else f'{v:.{_digits(sig.symbol)}f}'
    buy = sig.direction == 'BUY'
    lines = [
        f"{'🟢' if buy else '🔴'} <b>{sig.grade} {sig.direction} {_esc(sig.symbol)}</b> · {sig.timeframe.upper()} · {sig.style}",
        '',
        f"🎯 Entry: <code>{f(sig.entry)}</code>",
        f"🛑 Stop loss: <code>{f(sig.stop_loss)}</code>",
        f"✅ Take profit: <code>{f(sig.take_profit)}</code>" + (f"  ·  TP2 <code>{f(sig.take_profit_2)}</code>" if sig.take_profit_2 else ''),
    ]
    if sig.rr:
        lines.append(f"⚖️ Reward : risk  <b>{sig.rr:.2f}</b>")
    if r and r.get('agree'):
        lines.append(f"🧠 {len(r['agree'])}/{r.get('voting', len(r.get('scanners', [])))} scanners agree · quality {round(sig.quality)}")
    site = _cfg('SITE_URL')
    if site:
        lines.append(f'\n<a href="{site}/market-overview/?symbol={sig.symbol}&amp;style={sig.style}">Open the setup on PATREOAIX</a>')
    lines.append('\n<i>Not financial advice. Trade small, always use the stop.</i>')
    return '\n'.join(lines)


def post_signal(sig, r=None, force=False):
    """Post one new signal (chart + caption). Returns the Telegram message id, or None.
    force=True posts it whatever its grade (telegram_test --open / --all)."""
    if not enabled():
        return None
    if not force and GRADE_RANK.get(sig.grade, 0) < GRADE_RANK.get(_cfg('TELEGRAM_MIN_GRADE', 'A'), 2):
        return None
    if not cache.add(f'tg:sent:{sig.id}', 1, 90 * 86400):     # posted already (scan ran twice)
        return cache.get(f'tg:msg:{sig.id}')
    chat, caption = _cfg('TELEGRAM_CHANNEL_ID'), signal_caption(sig, r)
    msg = None
    try:
        from . import chart_scanner, coach_card
        png = coach_card.for_signal(sig)                       # looks like the Market Overview panel
        if not png:
            candles = chart_scanner.candles_for(sig.symbol, sig.timeframe)['candles']
            png = render_chart(sig.symbol, sig.timeframe, sig.direction, candles, sig.entry, sig.stop_loss,
                               sig.take_profit, sig.take_profit_2, _digits(sig.symbol))
        msg = api('sendPhoto', {'chat_id': chat, 'caption': caption, 'parse_mode': 'HTML'},
                  files={'photo': (f'{sig.symbol}.png', png, 'image/png')}, timeout=20)
    except Exception as e:
        logger.warning(f'Telegram photo for signal {sig.id} failed ({e}); sending text')
        try:
            msg = api('sendMessage', {'chat_id': chat, 'text': caption, 'parse_mode': 'HTML', 'disable_web_page_preview': 'true'})
        except Exception as e2:
            cache.delete(f'tg:sent:{sig.id}')                  # try again on the next pass
            logger.warning(f'Telegram post for signal {sig.id} failed: {e2}')
            return None
    cache.set(f'tg:msg:{sig.id}', msg['message_id'], 90 * 86400)
    return msg['message_id']


def post_outcome(sig):
    """Reply to the signal's post with how it ended (TP / SL / expired, in R)."""
    if not enabled() or not cache.add(f'tg:done:{sig.id}', 1, 90 * 86400):
        return None
    reply_to = cache.get(f'tg:msg:{sig.id}')
    if not reply_to:                                             # never posted (e.g. below the minimum grade)
        return None
    icon, word = {'TP': ('✅', 'TARGET HIT'), 'SL': ('❌', 'STOPPED OUT'), 'EXPIRED': ('⏱', 'EXPIRED')}.get(sig.outcome, ('ℹ️', sig.outcome))
    r = sig.result_r or 0
    text = (f"{icon} <b>{word}</b> · {sig.direction} {_esc(sig.symbol)} {sig.timeframe.upper()}\n"
            f"Result: <b>{r:+.2f}R</b>")
    try:
        api('sendMessage', {'chat_id': _cfg('TELEGRAM_CHANNEL_ID'), 'text': text, 'parse_mode': 'HTML',
                            'reply_to_message_id': reply_to, 'allow_sending_without_reply': 'true'})
    except Exception as e:
        cache.delete(f'tg:done:{sig.id}')
        logger.warning(f'Telegram outcome for signal {sig.id} failed: {e}')


def post_scan(results, style):
    """After a scan pass: one message listing every strong setup the scan found right now.

    The same list is not repeated within TELEGRAM_SCAN_REPEAT_MINUTES (default 60; 0 = post after every scan),
    but any change (new market, flipped direction, new grade) is posted on the next scan.
    """
    if not enabled() or _cfg('TELEGRAM_SCAN_POSTS', 'True') != 'True':
        return None
    floor = GRADE_RANK.get(_cfg('TELEGRAM_MIN_GRADE', 'A'), 2)
    strong = [r for r in results if GRADE_RANK.get(r.get('grade'), 0) >= floor and (r.get('plan') or {}).get('entry')]
    if not strong:
        return None
    key = '|'.join(sorted(f"{r['symbol']}:{r['direction']}:{r['grade']}" for r in strong))
    repeat = int(_cfg('TELEGRAM_SCAN_REPEAT_MINUTES', '60') or 0) * 60
    if repeat and cache.get(f'tg:scan:{style}') == key:
        return None
    lines = [f"🧠 <b>PATREOAIX AI scan · {style.title()}</b>  ({len(strong)} setup{'s' if len(strong) != 1 else ''})", '']
    for r in strong[:15]:
        p, d = r['plan'], _digits(r['symbol'])
        f = lambda v: '—' if v is None else f'{v:.{d}f}'
        lines.append(f"{'🟢' if r['direction'] == 'BUY' else '🔴'} <b>{r['grade']} {r['direction']} {_esc(r['symbol'])}</b> {r['main_tf'].upper()}"
                     f" · {len(r.get('agree') or [])}/{r.get('voting', '?')} agree")
        lines.append(f"     Entry <code>{f(p.get('entry'))}</code> · SL <code>{f(p.get('sl'))}</code> · TP <code>{f(p.get('tp1'))}</code>"
                     + (f" · R:R {p['rr']}" if p.get('rr') else ''))
    site = _cfg('SITE_URL')
    if site:
        lines.append(f'\n<a href="{site}/market-overview/?style={style}">Live scanner on PATREOAIX</a>')
    lines.append('\n<i>Not financial advice. Trade small, always use the stop.</i>')
    msg = api('sendMessage', {'chat_id': _cfg('TELEGRAM_CHANNEL_ID'), 'text': '\n'.join(lines),
                              'parse_mode': 'HTML', 'disable_web_page_preview': 'true'})
    cache.set(f'tg:scan:{style}', key, repeat or 60)
    return msg['message_id']


def safe(fn, *args):
    """Never let Telegram break the scanner."""
    try:
        return fn(*args)
    except Exception as e:
        logger.warning(f'Telegram {fn.__name__} failed: {e}')
        return None
