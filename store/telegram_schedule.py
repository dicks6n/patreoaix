# store/telegram_schedule.py
"""
Keeps the Telegram channel active every day, not only when an A / A+ signal appears.

Run by the background scanner after every pass (manage.py run_background → signal_scanner), so it needs
nothing else. Times are in TELEGRAM_TIMEZONE (default: the site's TIME_ZONE, Africa/Nairobi).

  ☀️ Morning brief       TELEGRAM_BRIEF_HOUR (8)    AI bias of the main markets + today's high-impact news
  📊 Market pulse        when the channel has been quiet for TELEGRAM_PULSE_HOURS (3), 07:00–23:00 only
  ⚠️ News alert          30 minutes before each high-impact economic event
  🏁 Daily results       TELEGRAM_RECAP_HOUR (22)   image card: today's closed signals, wins, losses, net R
  🏆 Weekly report       TELEGRAM_WEEKLY_DAY (6 = Sunday) at TELEGRAM_RECAP_HOUR - 2: last 7 days
  ⏳ Coach updates       when the Market Overview verdict of a watched market changes to WAIT / GET READY /
                         ENTRY CONFIRMED (TELEGRAM_COACH_SYMBOLS), with the same panel as an image

Each post goes out once (cache keys per day / per event); TELEGRAM_SCHEDULE=False turns all of it off.
"""
import io
import logging
import time
from datetime import datetime, timedelta, timezone as dt_tz

from django.conf import settings
from django.core.cache import cache

from . import telegram_bot as tg

logger = logging.getLogger(__name__)

LAST_POST = 'tg:last_post'
FLAG = {'USD': '🇺🇸', 'EUR': '🇪🇺', 'GBP': '🇬🇧', 'JPY': '🇯🇵', 'AUD': '🇦🇺', 'CAD': '🇨🇦', 'CHF': '🇨🇭',
        'NZD': '🇳🇿', 'CNY': '🇨🇳'}


def _cfg(name, default):
    return str(getattr(settings, name, '') or default)


def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(_cfg('TELEGRAM_TIMEZONE', settings.TIME_ZONE or 'UTC'))
    except Exception:
        return dt_tz.utc


def now_local():
    return datetime.now(_tz())


def mark_posted():
    cache.set(LAST_POST, time.time(), 7 * 86400)


def quiet_for():
    """Seconds since the channel's last post (by any part of the bot)."""
    last = cache.get(LAST_POST)
    return time.time() - last if last else 10 ** 9


def _once(key, ttl=3 * 86400):
    return cache.add(key, 1, ttl)


def _send(text):
    tg.api('sendMessage', {'chat_id': tg._cfg('TELEGRAM_CHANNEL_ID'), 'text': text, 'parse_mode': 'HTML',
                           'disable_web_page_preview': 'true'})


def _photo(png, caption):
    tg.api('sendPhoto', {'chat_id': tg._cfg('TELEGRAM_CHANNEL_ID'), 'caption': caption[:1024], 'parse_mode': 'HTML'},
           files={'photo': ('patreoaix.png', png, 'image/png')}, timeout=25)


def _link(path, label):
    site = tg._cfg('SITE_URL')
    return (f'\n<a href="{site}{path}">{label}</a>  ·  👑 <a href="{site}/subscribe/">Subscribe</a>') if site else ''


def _price(symbol, v):
    return '—' if v is None else f'{v:,.{tg._digits(symbol)}f}'


# ═══════════════════════════════════════════════════════════════
# POSTS
# ═══════════════════════════════════════════════════════════════
def _bias_lines(results, n):
    out = []
    for r in sorted(results, key=lambda r: -abs(r.get('confluence') or 0))[:n]:
        arrow = '🟢 BUY ' if r['direction'] == 'BUY' else '🔴 SELL'
        grade = f" · <b>{r['grade']}</b>" if r.get('grade') else ''
        out.append(f"{arrow} <b>{tg._esc(r['symbol'])}</b> {r['main_tf'].upper()} · bias {abs(r.get('confluence') or 0):.0f}%{grade}"
                   f"  <code>{_price(r['symbol'], r.get('price'))}</code>")
    return out


def _strength_line(strength):
    if not strength:
        return ''
    ranked = sorted(strength.items(), key=lambda kv: kv[1], reverse=True)
    strong = ', '.join(f"{FLAG.get(k, '')}{k}" for k, _ in ranked[:2])
    weak = ', '.join(f"{FLAG.get(k, '')}{k}" for k, _ in ranked[-2:])
    return f'💪 Strongest: {strong}   🥀 Weakest: {weak}'


def _events(hours_from, hours_to, impact='high'):
    try:
        from .calendar_feed import get_calendar
        events = get_calendar(2).get('events') or []
    except Exception as e:
        logger.warning(f'Telegram schedule: calendar failed: {e}')
        return []
    now = datetime.now(dt_tz.utc)
    out = []
    for e in events:
        d = e.get('date')
        if not isinstance(d, datetime) or e.get('impact') != impact:
            continue
        d = d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)
        if now + timedelta(hours=hours_from) <= d <= now + timedelta(hours=hours_to):
            out.append(dict(e, date=d))
    return out


def morning_brief(results, strength):
    today = now_local()
    lines = [f"☀️ <b>Good morning traders — {today:%A %d %b}</b>", '', '<b>AI bias on the main markets</b>']
    lines += _bias_lines(results, 6) or ['No clear bias yet, markets are quiet.']
    s = _strength_line(strength)
    if s:
        lines += ['', s]
    news = _events(0, 24)
    lines += ['', '<b>⚠️ High-impact news today</b>' if news else '📅 No high-impact news today.']
    for e in news[:6]:
        lines.append(f"{e['date'].astimezone(_tz()):%H:%M} {FLAG.get(e.get('currency'), '')}{e.get('currency', '')} {tg._esc(e.get('event', ''))}")
    lines.append(_link('/market-overview/', 'Live AI scanner'))
    lines.append('\n<i>Signals post here the moment the scanner finds them. Not financial advice.</i>')
    _send('\n'.join(lines))


def market_pulse(results, strength):
    lines = [f"📊 <b>Market pulse · {now_local():%H:%M}</b>", '']
    lines += _bias_lines(results, 5)
    s = _strength_line(strength)
    if s:
        lines += ['', s]
    watch = [r for r in results if r.get('grade') == 'B']
    if watch:
        lines += ['', '👀 Building up (grade B): ' + ', '.join(f"{r['symbol']} {r['direction']}" for r in watch[:4])]
    lines.append(_link('/market-overview/', 'Full scanner'))
    _send('\n'.join(lines))


def news_alert(e):
    when = e['date'].astimezone(_tz())
    text = (f"⚠️ <b>High-impact news in 30 minutes</b>\n\n"
            f"{FLAG.get(e.get('currency'), '')} <b>{e.get('currency', '')} · {tg._esc(e.get('event', ''))}</b> at {when:%H:%M}\n"
            + (f"Forecast <code>{e['forecast']}</code> · Previous <code>{e['previous']}</code>\n" if e.get('forecast') or e.get('previous') else '')
            + f"\nExpect fast moves on {e.get('currency', '')} pairs" + (' and gold' if e.get('currency') == 'USD' else '')
            + '. Tighten stops or wait for the candle to close.')
    _send(text)


# ═══════════════════════════════════════════════════════════════
# BIG RELEASES: playbook before, result after (store/news_history.py)
# ═══════════════════════════════════════════════════════════════
def _bias(results, symbol):
    r = next((x for x in results or [] if x.get('symbol') == symbol), None)
    if not r or not r.get('direction'):
        return ''
    return f"{r['direction']} {abs(r.get('confluence') or 0):.0f}%" + (f" ({r['grade']})" if r.get('grade') else '')


def event_preview(e, results=None):
    from . import news_card, news_history as nh
    pb = nh.playbook(e['key'], e['date'], e.get('forecast'), e.get('previous'), tz=_tz())
    wl = pb['when_local']
    lines = [f"🏦 <b>{tg._esc(pb['label'])}</b> · today {wl:%H:%M} (Nairobi)" if wl.date() == now_local().date()
             else f"🏦 <b>{tg._esc(pb['label'])}</b> · {wl:%a %d %b %H:%M} (Nairobi)"]
    if e.get('forecast') or e.get('previous'):
        lines.append(f"Forecast <code>{tg._esc(e.get('forecast') or '—')}</code> · Previous <code>{tg._esc(e.get('previous') or '—')}</code>")
    if pb['expectation']:
        lines.append(f"Consensus: {tg._esc(pb['expectation'])}")
    if pb['stats']:
        lines += ['', f"<b>What history says</b> (last {pb['stats'][0]['n']} releases)"]
        for st in pb['stats'][:3]:
            lines.append(f"• {st['symbol']}: ±{(st['abs_h1'] or 0):.2f}% in the first hour, ±{(st['abs_m15'] or 0):.2f}% in 15 min")
        for line in nh.scenario_lines(pb['stats'][0], pb['currency']):
            lines.append('• If ' + tg._esc(line))
    bias = [f"{sym} {_bias(results, sym)}" for sym in nh.SYMBOLS[pb['currency']][:3] if _bias(results, sym)]
    if bias:
        lines += ['', '🧠 AI bias now: ' + ' · '.join(bias)]
    lines += ['', f"⏱ <b>When to execute:</b> {tg._esc(pb['timing'])}"]
    if pb['second_wave']:
        lines.append(f"🎤 Press conference ≈ {pb['second_wave']:%H:%M}: expect a second wave of volatility.")
    lines.append('\n<i>History is not a promise. Spreads widen at the release: small size, always a stop.</i>')
    lines.append(_link(f"/news-impact/?event={pb['key']}", 'Full history on PATREOAIX'))
    _photo(news_card.playbook_png(pb), '\n'.join(lines))


def event_result(e):
    """About an hour after the release: actual vs forecast and how the markets moved."""
    from . import news_history as nh
    key = e['key']
    label, currency = nh.EVENTS[key][0], nh.EVENTS[key][1]
    t0 = int(e['date'].timestamp())
    moves = []
    d = nh._Deriv()
    try:
        for sym in nh.SYMBOLS[currency][:3]:
            m = nh._measure(d.candles(sym, t0 - 2 * 3600, int(time.time())), t0)
            if m:
                moves.append((sym, m))
    finally:
        d.close()
    actual, forecast = e.get('actual'), e.get('forecast')
    a, f = nh._num(actual), nh._num(forecast)
    verdict = ''
    if a is not None and f is not None:
        good = nh.EVENTS[key][3]
        verdict = ' · ✅ better than forecast' if (a > f) == good and a != f else ' · ❌ worse than forecast' if a != f else ' · ➖ as expected'
    lines = [f"📊 <b>{tg._esc(label)}: the result</b>",
             f"Actual <code>{tg._esc(actual or '—')}</code> · Forecast <code>{tg._esc(forecast or '—')}</code>{verdict}", '']
    for sym, m in moves:
        lines.append(f"• {sym}: {nh.fmt_pct(m['m15'])} in 15 min · {nh.fmt_pct(m['h1'])} after 1 h "
                     f"(range {nh.fmt_pct(m['down1h'])} / {nh.fmt_pct(m['up1h'])})")
    if not moves:
        lines.append('Price data for the reaction is not available yet.')
    lines.append(_link(f"/news-impact/?event={key}", 'See the chart history'))
    _send('\n'.join(lines))


def event_posts(now, results):
    if _cfg('TELEGRAM_EVENT_POSTS', 'True') != 'True':
        return []
    from . import news_history as nh
    from .calendar_feed import get_calendar
    sent = []
    lead = float(_cfg('TELEGRAM_EVENT_PREVIEW_MINUTES', 120))
    utc = datetime.now(dt_tz.utc)
    for e in nh.upcoming(hours=lead / 60 + 1):
        mins = (e['date'] - utc).total_seconds() / 60
        if 0 < mins <= lead and _once(f"tg:evprev:{e['key']}:{e['date']:%Y%m%d%H%M}", 3 * 86400):
            event_preview(e, results)
            sent.append(f"preview {e['key']}")
            break                                            # one big post per pass
    for e in get_calendar(2).get('events') or []:
        key = nh.classify(e.get('event'), e.get('currency'))
        d = e.get('date')
        if key in nh.BIG and isinstance(d, datetime) and timedelta(minutes=62) <= utc - d <= timedelta(minutes=110) \
                and _once(f"tg:evres:{key}:{d:%Y%m%d%H%M}", 3 * 86400):
            event_result(dict(e, key=key))
            sent.append(f'result {key}')
            break
    return sent


COACH_POST = {   # verdict → (icon, word); other verdicts (stand aside, watch) are not posted
    'ENTER': ('✅', 'ENTRY CONFIRMED'),
    'GET_READY': ('🎯', 'GET READY'),
    'WAIT_PULLBACK': ('⏳', 'WAIT'),
    'WAIT_NEWS': ('📰', 'WAIT FOR THE NEWS'),
}


def coach_symbols():
    raw = _cfg('TELEGRAM_COACH_SYMBOLS', 'XAUUSD,EURUSD,GBPUSD,USDJPY,BTCUSD,USOIL')
    return [x.strip().upper() for x in raw.split(',') if x.strip()]


def coach_caption(view):
    v, c = view['verdict'], view.get('coach') or {}
    icon, word = COACH_POST.get(v.get('action'), ('📊', v.get('action', '')))
    lines = [f"{icon} <b>{tg._esc(view['symbol'])} {view['tf'].upper()} · {word}</b>",
             tg._esc(v.get('headline', '')), '',
             tg._esc(c.get('instruction') or c.get('headline') or '')]
    plan = c.get('plan') or {}
    if v.get('action') in ('ENTER', 'GET_READY') and plan.get('entry') is not None:
        p = lambda x: _price(view['symbol'], x)
        lines += ['', f"🎯 Entry <code>{p(plan.get('entry'))}</code> · 🛑 SL <code>{p(plan.get('sl'))}</code> · "
                      f"✅ TP <code>{p(plan.get('tp1'))}</code>" + (f" · R:R {plan['rr']:.1f}" if plan.get('rr') else '')]
    lines.append(f"\n🧠 Conviction {v.get('conviction', 0)}% · {len(v.get('agree') or [])}/{v.get('voting', 0)} strategies agree")
    lines.append(_link(f"/market-overview/?symbol={view['symbol']}&amp;style={view.get('style', 'intraday')}", 'Open the full analysis'))
    lines.append('\n<i>Not financial advice. Wait for your own confirmation and always use a stop.</i>')
    return '\n'.join(lines)


def coach_updates(now):
    """At most one post per pass: the first watched market whose verdict changed to something worth telling."""
    from . import coach_card
    if _cfg('TELEGRAM_COACH_UPDATES', 'True') != 'True':
        return None
    day = f'{now:%Y%m%d}'
    if (cache.get(f'tg:coach:n:{day}') or 0) >= int(_cfg('TELEGRAM_COACH_DAILY', 12)):
        return None
    gap = float(_cfg('TELEGRAM_COACH_MINUTES', 60)) * 60
    for sym in coach_symbols():
        if not cache.add(f'tg:coach:chk:{sym}', 1, 300):      # look at each market every 5 minutes
            continue
        try:
            view = coach_card.build_view(sym, 'intraday')
        except Exception as e:
            logger.info(f'coach update {sym}: {e}')
            continue
        if not view:
            continue
        v = view['verdict']
        state = f"{v.get('action')}:{v.get('side')}:{(view.get('coach') or {}).get('state')}"
        last = cache.get(f'tg:coach:last:{sym}')
        if state == last:
            continue
        cache.set(f'tg:coach:last:{sym}', state, 3 * 86400)
        if v.get('action') not in COACH_POST:
            continue                                         # stand aside / watch: remembered, not posted
        entering = v.get('action') == 'ENTER'
        if not entering and not (7 <= now.hour < 23):
            continue
        if not entering and time.time() - (cache.get(f'tg:coach:at:{sym}') or 0) < gap:
            continue
        _photo(coach_card.render(view), coach_caption(view))
        cache.set(f'tg:coach:at:{sym}', time.time(), 86400)
        cache.add(f'tg:coach:n:{day}', 0, 2 * 86400)
        try:
            cache.incr(f'tg:coach:n:{day}')
        except ValueError:
            pass
        return f'coach {sym} {v.get("action")}'
    return None


def _period(days):
    """[start, end) of the last `days` days including today, in the channel's timezone."""
    today = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1), today + timedelta(days=1)


def _closed(start, end):
    from .models import ScannerSignal
    return list(ScannerSignal.objects.filter(closed_at__gte=start, closed_at__lt=end).exclude(outcome='OPEN').order_by('closed_at'))


def results_card(title, subtitle, sigs, opened, still_open):
    """1080×1350 PNG: stats + one row per closed signal."""
    from PIL import Image, ImageDraw
    from .poster import font, BG, PANEL, TEXT, MUTE, UP, DN, AMBER
    W, H = 1080, 1350
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 10], fill=AMBER)
    d.text((60, 60), 'PATREO', font=font(44), fill=TEXT)
    d.text((60 + d.textlength('PATREO', font=font(44)), 60), 'AIX', font=font(44), fill=AMBER)
    d.text((W - 60, 72), subtitle, font=font(26, False), fill=MUTE, anchor='ra')
    d.text((60, 150), title, font=font(72), fill=TEXT)
    wins = sum(1 for s in sigs if s.outcome == 'TP')
    losses = sum(1 for s in sigs if s.outcome == 'SL')
    net = sum(s.result_r or 0 for s in sigs)
    rate = f'{round(100 * wins / (wins + losses))}%' if wins + losses else '—'
    stats = [('SIGNALS', str(opened)), ('WINS', str(wins)), ('LOSSES', str(losses)), ('WIN RATE', rate)]
    cw = (W - 120 - 3 * 16) / 4
    for i, (k, v) in enumerate(stats):
        x = 60 + i * (cw + 16)
        d.rounded_rectangle([x, 270, x + cw, 400], radius=20, fill=PANEL)
        d.text((x + cw / 2, 308), k, font=font(22), fill=MUTE, anchor='mm')
        d.text((x + cw / 2, 360), v, font=font(48), fill=UP if k == 'WINS' else DN if k == 'LOSSES' else TEXT, anchor='mm')
    d.rounded_rectangle([60, 424, W - 60, 524], radius=20, fill=PANEL)
    d.text((96, 474), 'NET RESULT', font=font(30), fill=MUTE, anchor='lm')
    d.text((W - 96, 474), f'{net:+.2f}R', font=font(56), fill=UP if net > 0 else DN if net < 0 else TEXT, anchor='rm')
    y = 560
    rows = sigs[-8:]
    if not rows:
        d.text((W / 2, 760), 'No trades closed in this period.', font=font(34, False), fill=MUTE, anchor='mm')
    for s in rows:
        d.rounded_rectangle([60, y, W - 60, y + 74], radius=16, fill=PANEL)
        col = UP if s.direction == 'BUY' else DN
        d.text((90, y + 37), s.direction, font=font(28), fill=col, anchor='lm')
        d.text((200, y + 37), f'{s.symbol}  {s.timeframe.upper()}  {s.grade}', font=font(30), fill=TEXT, anchor='lm')
        word, wc = {'TP': ('TARGET', UP), 'SL': ('STOPPED', DN)}.get(s.outcome, ('EXPIRED', MUTE))
        d.text((W - 96, y + 37), f'{word} {(s.result_r or 0):+.2f}R', font=font(30), fill=wc, anchor='rm')
        y += 86
    if still_open:
        d.text((60, H - 150), f'{still_open} signal{"s" if still_open != 1 else ""} still running', font=font(28, False), fill=MUTE)
    d.text((W / 2, H - 84), f"Free AI signals · {(getattr(settings, 'PUBLIC_HOST', '') or 'www.patreoaix.com').upper()}",
           font=font(30), fill=AMBER, anchor='mm')
    out = io.BytesIO()
    img.save(out, format='PNG', optimize=True)
    return out.getvalue(), wins, losses, net


def _recap(days, title, head, label):
    from .models import ScannerSignal
    start, end = _period(days)
    sigs = _closed(start, end)
    opened = ScannerSignal.objects.filter(created_at__gte=start, created_at__lt=end).count()
    still_open = ScannerSignal.objects.filter(outcome='OPEN').count()
    sub = f'{start:%d %b}' + (f' – {(end - timedelta(days=1)):%d %b}' if days > 1 else '')
    png, wins, losses, net = results_card(title, sub, sigs, opened, still_open)
    caption = (f"{head} <b>{label} · {sub}</b>\n\n"
               f"Signals: <b>{opened}</b> · ✅ {wins} · ❌ {losses} · Net <b>{net:+.2f}R</b>\n"
               + (f"Running now: {still_open}\n" if still_open else '')
               + _link('/market-overview/', 'Full history on PATREOAIX')
               + '\n<i>Every signal is tracked automatically against the real candles. Not financial advice.</i>')
    _photo(png, caption)


def daily_recap():
    _recap(1, "TODAY'S RESULTS", '🏁', "Today's results")


def weekly_report():
    _recap(7, 'WEEKLY REPORT', '🏆', 'Weekly report')


# ═══════════════════════════════════════════════════════════════
# SCHEDULER (called after every scanner pass)
# ═══════════════════════════════════════════════════════════════
def run(results, strength):
    """Post whatever is due now. Returns the names of the posts sent."""
    if not tg.enabled() or _cfg('TELEGRAM_SCHEDULE', 'True') != 'True':
        return []
    now = now_local()
    day = f'{now:%Y%m%d}'
    brief_h, recap_h = int(_cfg('TELEGRAM_BRIEF_HOUR', 8)), int(_cfg('TELEGRAM_RECAP_HOUR', 22))
    weekly_d = int(_cfg('TELEGRAM_WEEKLY_DAY', 6))
    pulse_h = float(_cfg('TELEGRAM_PULSE_HOURS', 3))
    sent = []

    def due(name, hour, fn, *args):
        if hour <= now.hour < hour + 3 and _once(f'tg:sched:{name}:{day}'):
            try:
                fn(*args)
                sent.append(name)
            except Exception:
                cache.delete(f'tg:sched:{name}:{day}')        # try again on the next pass
                raise

    due('brief', brief_h, morning_brief, results, strength)
    due('recap', recap_h, daily_recap)
    if now.weekday() == weekly_d:
        due('weekly', max(recap_h - 2, 0), weekly_report)

    if _cfg('TELEGRAM_NEWS_ALERTS', 'True') == 'True':
        for e in _events(0, 0.5):                              # starts within 30 minutes
            key = f"tg:news:{e.get('id') or e.get('event')}:{e['date']:%Y%m%d%H%M}"
            if _once(key):
                news_alert(e)
                sent.append('news')

    sent += event_posts(now, results)
    name = None if sent else coach_updates(now)
    if name:
        sent.append(name)

    if pulse_h and 7 <= now.hour < 23 and results and quiet_for() >= pulse_h * 3600 and not sent:
        if _once(f'tg:sched:pulse:{day}:{now.hour}', 3600):
            market_pulse(results, strength)
            sent.append('pulse')
    return sent


def safe_run(results, strength):
    try:
        return run(results, strength)
    except Exception as e:
        logger.warning(f'Telegram schedule failed: {e}')
        return []
