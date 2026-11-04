# store/bot_engine.py
"""
The PATREOAIX trading bot: it trades the AI scanner's setups on the customer's own PATREOAIX trading account
(the MT5 terminal), by itself, inside the rules the customer set.

  WATCH    the scanner checks 19 markets around the clock (store/signal_alerts.py)
  FILTER   a new setup is only taken when it matches the bot's rules: markets, style, minimum grade,
           trading session, a pause around NFP / CPI / FOMC
  TRADE    market order with the setup's stop loss and take profit; the size is worked out so that hitting the
           stop costs `risk %` of the equity (never more: rounded down to the lot step)
  PROTECT  the terminal closes it at the stop or the target (server side, even when the customer is offline);
           when the AI retires the setup the bot closes its trade; limits: open trades, trades a day, and a daily
           loss limit that switches the bot off for the day

Settings and state live on UserBot (one bot per customer); what it did is in BotEvent (the activity feed).
Pro / VIP only to switch on (membership.can_auto_trade), like copy trading.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal, ROUND_DOWN

from django.utils import timezone

from . import signal_engine as se

logger = logging.getLogger(__name__)

MARKETS = [('XAUUSD', 'Gold'), ('XAGUSD', 'Silver'), ('EURUSD', 'EUR/USD'), ('GBPUSD', 'GBP/USD'), ('USDJPY', 'USD/JPY'),
           ('AUDUSD', 'AUD/USD'), ('USDCAD', 'USD/CAD'), ('GBPJPY', 'GBP/JPY'), ('USOIL', 'Oil'), ('BTCUSD', 'Bitcoin'),
           ('ETHUSD', 'Ethereum'), ('SOLUSD', 'Solana')]
STYLES = [('scalp', 'Scalping', '15-minute chart, quick trades'), ('intraday', 'Intraday', '1-hour chart, closed the same day or next'),
          ('swing', 'Swing', '4-hour chart, a few days')]
SESSIONS = [('any', 'Around the clock'), ('london_ny', 'London + New York (07:00–20:00 GMT)'), ('ny', 'New York only (12:00–20:00 GMT)')]
DEFAULTS = {'markets': ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'], 'styles': ['intraday'], 'min_grade': 'A+',
            'risk_pct': 1.0, 'max_open': 2, 'max_per_day': 4, 'daily_loss_pct': 3.0, 'session': 'london_ny', 'news_pause': True}
KIND = 'ai_bot'           # UserBot rows of this bot (the Strategy Builder keeps its own rows in the same table)
LIMITS = {'risk_pct': (0.25, 5.0), 'max_open': (1, 10), 'max_per_day': (1, 20), 'daily_loss_pct': (1.0, 20.0)}


# ═══════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════
def bot_for(user):
    from .models import UserBot
    bot = UserBot.objects.filter(user=user, config_json__kind=KIND).order_by('id').first()
    if not bot:
        bot = UserBot.objects.create(user=user, name='My AI bot', config_json=dict(DEFAULTS, kind=KIND), state_json={})
    return bot


def config(bot):
    c = dict(DEFAULTS, **(bot.config_json or {}))
    c['markets'] = [m for m in c['markets'] if m in dict(MARKETS)] or DEFAULTS['markets']
    c['styles'] = [s for s in c['styles'] if s in dict((k, v) for k, v, _ in STYLES)] or DEFAULTS['styles']
    return c


def clean(post):
    """Settings from the form, clamped to safe limits."""
    def num(name, cast):
        lo, hi = LIMITS[name]
        try:
            v = cast(post.get(name))
        except (TypeError, ValueError):
            v = DEFAULTS[name]
        return max(lo, min(hi, v))
    styles = [s for s in post.getlist('styles') if s in [k for k, *_ in STYLES]]
    return {
        'kind': KIND,
        'markets': [m for m in post.getlist('markets') if m in dict(MARKETS)] or DEFAULTS['markets'],
        'styles': styles or DEFAULTS['styles'],
        'min_grade': post.get('min_grade') if post.get('min_grade') in ('A+', 'A') else 'A+',
        'risk_pct': round(num('risk_pct', float), 2), 'max_open': num('max_open', int), 'max_per_day': num('max_per_day', int),
        'daily_loss_pct': round(num('daily_loss_pct', float), 1),
        'session': post.get('session') if post.get('session') in dict(SESSIONS) else 'london_ny',
        'news_pause': post.get('news_pause') == 'on',
    }


def log(user, kind, message, signal=None, trade=None, pnl=None):
    from .models import BotEvent
    try:
        return BotEvent.objects.create(user=user, kind=kind, message=message[:300], signal=signal, trade=trade, pnl=pnl)
    except Exception as e:
        logger.warning(f'bot event {user}: {e}')


# ═══════════════════════════════════════════════════════════════
# RULES
# ═══════════════════════════════════════════════════════════════
def in_session(cfg, now=None):
    h = (now or datetime.now(dt_tz.utc)).hour
    return {'any': True, 'london_ny': 7 <= h < 20, 'ny': 12 <= h < 20}.get(cfg['session'], True)


def news_blackout(now=None):
    """A big release (NFP, CPI, FOMC …) in the next 30 minutes or the last 15 → its name, else ''."""
    try:
        from . import news_history as nh
        from .calendar_feed import get_calendar
        now = now or datetime.now(dt_tz.utc)
        for e in get_calendar(2).get('events') or []:
            d, key = e.get('date'), nh.classify(e.get('event'), e.get('currency'))
            if key in nh.BIG and isinstance(d, datetime) and now - timedelta(minutes=15) <= d <= now + timedelta(minutes=30):
                return nh.EVENTS[key][0]
    except Exception:
        pass
    return ''


def bot_trades(user):
    from .models import Trade, BotEvent
    ids = BotEvent.objects.filter(user=user, kind='OPEN', trade__isnull=False).values_list('trade_id', flat=True)
    return Trade.objects.filter(pk__in=ids)


def today_pnl(user):
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = Decimal('0')
    for t in bot_trades(user).filter(date_opened__gte=start - timedelta(days=3)):
        if t.status == 'OPEN':
            try:
                from .mt5_service import position_snapshot
                total += position_snapshot(t)['profit']
            except Exception:
                pass
        elif t.date_closed and t.date_closed >= start:
            total += Decimal(str(t.pnl or 0))
    return total


def size(user, sig, risk_pct):
    """→ (volume, risk_usd, note). Lots so that the stop costs at most risk % of the equity."""
    from .mt5_service import get_metrics, get_spec, calc_profit
    spec = get_spec(sig.symbol)
    equity = Decimal(str(get_metrics(user)['equity']))
    risk = (equity * Decimal(str(risk_pct)) / 100).quantize(Decimal('0.01'))
    entry = sig.entry
    try:                                                     # size from the price it will really fill at (spread included)
        from .mt5_service import live_quote
        q = live_quote(sig.symbol)
        if q is not None:
            entry = q.ask if sig.direction == 'BUY' else q.bid
    except Exception:
        pass
    per_lot = abs(Decimal(str(calc_profit(sig.symbol, sig.direction, 1, entry, sig.stop_loss))))
    if per_lot <= 0 or risk <= 0:
        return None, risk, 'No room for a stop on this setup.'
    vol = (risk / per_lot / spec.volume_step).to_integral_value(rounding=ROUND_DOWN) * spec.volume_step
    vol = min(vol, spec.volume_max)
    if vol < spec.volume_min:
        need = (per_lot * spec.volume_min).quantize(Decimal('0.01'))
        return None, risk, (f'{risk_pct}% of ${equity:,.2f} is ${risk}, but the smallest trade ({spec.volume_min} lot) '
                            f'would risk ${need} here. Add funds or raise the risk.')
    return vol, (per_lot * vol).quantize(Decimal('0.01')), ''


def check(bot, sig, cfg=None, own=False):
    """Would the bot take this setup? → (ok, reason). Reasons are shown to the customer.
    own=True: a setup from the customer's own deployed strategy (Strategy Lab): its rules already chose the market."""
    from .membership import can_auto_trade
    cfg = cfg or config(bot)
    user = bot.user
    if own:
        pass
    elif sig.symbol not in cfg['markets']:
        return False, f'{sig.symbol} is not in your markets.'
    if not own and sig.style not in cfg['styles']:
        return False, f'{sig.style.capitalize()} setup; your bot trades {", ".join(cfg["styles"])}.'
    if not own and se.GRADE_RANK.get(sig.grade, 0) < se.GRADE_RANK.get(cfg['min_grade'], 3):
        return False, f'Grade {sig.grade}; your bot waits for {cfg["min_grade"]}.'
    if not can_auto_trade(user):
        return False, 'Switching the bot on needs the Pro plan.'
    if not in_session(cfg):
        return False, 'Outside your trading session.'
    if cfg['news_pause']:
        news = news_blackout()
        if news:
            return False, f'Paused around {news}.'
    trades = bot_trades(user)
    if trades.filter(status='OPEN').count() >= cfg['max_open']:
        return False, f'Already {cfg["max_open"]} bot trades open (your limit).'
    if trades.filter(date_opened__gte=timezone.now() - timedelta(days=1)).count() >= cfg['max_per_day']:
        return False, f'{cfg["max_per_day"]} trades in the last 24 hours (your daily limit).'
    if trades.filter(status='OPEN', asset__symbol=sig.symbol).exists():
        return False, f'Already in a {sig.symbol} trade.'
    return True, ''


# ═══════════════════════════════════════════════════════════════
# ACTIONS (called by the scanner worker)
# ═══════════════════════════════════════════════════════════════
def take(bot, sig, own=None):
    """Open a trade for a scanner setup, or (own = strategy name) for a setup from the customer's own strategy."""
    from . import mt5_service as m
    from .models import ScannerSignal
    from .notifications import notify
    real = sig if isinstance(sig, ScannerSignal) else None
    label = f'{own} strategy' if own else f'{sig.grade} {sig.style} setup'
    ok, why = check(bot, sig, own=bool(own))
    if not ok:
        if own or ('not in your markets' not in why and 'your bot trades' not in why and 'waits for' not in why):
            log(bot.user, 'SKIP', f'Skipped {sig.direction} {sig.symbol} ({label}): {why}', signal=real)
        return None
    vol, risk, note = size(bot.user, sig, config(bot)['risk_pct'])
    if not vol:
        log(bot.user, 'SKIP', f'Skipped {sig.direction} {sig.symbol}: {note}', signal=real)
        return None
    try:
        trade = m.open_position(bot.user, sig.symbol, sig.direction, vol, sl=sig.stop_loss, tp=sig.take_profit)
    except Exception as e:
        log(bot.user, 'ERROR', f'Could not open {sig.direction} {sig.symbol}: {e}', signal=real)
        return None
    tp_txt = f'{sig.take_profit:g}' if sig.take_profit else 'none'
    log(bot.user, 'OPEN', f'Opened {sig.direction} {vol} {sig.symbol} at {trade.entry_price.normalize()} · stop {sig.stop_loss:g} · '
                          f'target {tp_txt} · risking ${risk} ({label})', signal=real, trade=trade)
    try:
        notify(bot.user, title=f'🤖 Bot opened {sig.direction} {sig.symbol}',
               message=f'{vol} lot at {trade.entry_price.normalize()}, stop {sig.stop_loss:g}, target {tp_txt}. Risk ${risk}. ({label})',
               category='TRADE', priority='success', action_url='/bot/', action_label='Open bot')
    except Exception:
        pass
    return trade


def strategy_tick():
    """Deployed Strategy Lab strategies: read the last closed candle of each of their markets; trade new signals."""
    from types import SimpleNamespace
    from django.core.cache import cache
    from .models import TradingStrategy, UserBot
    from . import strategy_lab as L
    import json
    if not cache.add('bot:strategy_tick', 1, 55):
        return 0
    n = 0
    for bot in UserBot.objects.filter(is_running=True, config_json__kind=KIND).select_related('user'):
        state = dict(bot.state_json or {})
        seen = dict(state.get('lab') or {})
        for st in TradingStrategy.objects.filter(user=bot.user, is_deployed=True):
            try:
                spec = json.loads(st.blocks_json or '{}')
            except ValueError:
                continue
            if not isinstance(spec, dict) or 'buy' not in spec:
                continue
            for sym in L.normalise(spec)['symbols']:
                try:
                    got = L.live_signal(spec, sym)
                except Exception as e:
                    logger.warning(f'lab live {st.id} {sym}: {e}')
                    continue
                if not got:
                    continue
                direction, entry, sl, tp, bar = got
                key = f'{st.id}:{sym}'
                if seen.get(key) == bar:
                    continue
                seen[key] = bar
                sig = SimpleNamespace(symbol=sym, direction=direction, entry=entry, stop_loss=sl, take_profit=tp, grade='S', style='strategy')
                if take(bot, sig, own=st.name):
                    n += 1
        if seen != (state.get('lab') or {}):
            state['lab'] = seen
            UserBot.objects.filter(pk=bot.pk).update(state_json=state)
    return n


def on_new_signal(sig):
    from .models import UserBot
    taken = []
    for bot in UserBot.objects.filter(is_running=True, config_json__kind=KIND).select_related('user'):
        try:
            if take(bot, sig):
                taken.append(bot.user_id)
        except Exception as e:
            logger.warning(f'bot {bot.user} {sig}: {e}')
    return taken


def on_signal_closed(sig):
    """The AI retired the setup (the market flipped or it ran out of time): close the bot trades still open on it."""
    from . import mt5_service as m
    from .models import BotEvent
    if sig.outcome not in ('EXPIRED',):
        return                                    # SL / TP: the terminal closes those itself at the real price
    for ev in BotEvent.objects.filter(kind='OPEN', signal=sig, trade__status='OPEN').select_related('trade', 'user'):
        try:
            m.close_position(ev.user, ev.trade_id, reason='CLIENT')
            log(ev.user, 'INFO', f'Closed {ev.trade.direction} {sig.symbol}: the AI retired this setup.', signal=sig, trade=ev.trade)
        except Exception as e:
            log(ev.user, 'ERROR', f'Could not close {sig.symbol}: {e}', signal=sig, trade=ev.trade)


def tick():
    """Every scanner pass: report closed bot trades, enforce the daily loss limit."""
    from .models import BotEvent, UserBot
    from .mt5_service import get_metrics
    done = set(BotEvent.objects.filter(kind='CLOSE').values_list('trade_id', flat=True))
    for ev in BotEvent.objects.filter(kind='OPEN', trade__isnull=False).exclude(trade__status='OPEN').exclude(trade_id__in=done).select_related('trade', 'user'):
        t = ev.trade
        why = {'TP': 'target hit', 'SL': 'stop hit', 'SO': 'stop out', 'CLIENT': 'closed'}.get(t.close_reason, 'closed')
        pnl = Decimal(str(t.pnl or 0))
        log(ev.user, 'CLOSE', f'{t.direction} {t.asset.symbol if t.asset else ""} {why}: {"+" if pnl >= 0 else "−"}${abs(pnl):,.2f}',
            signal=ev.signal, trade=t, pnl=pnl)
    for bot in UserBot.objects.filter(is_running=True, config_json__kind=KIND).select_related('user'):
        cfg = config(bot)
        try:
            eq = Decimal(str(get_metrics(bot.user)['equity']))
            pnl = today_pnl(bot.user)
            if eq > 0 and pnl < 0 and -pnl >= (eq - pnl) * Decimal(str(cfg['daily_loss_pct'])) / 100:
                bot.is_running = False
                bot.state_json = dict(bot.state_json or {}, paused='daily_loss', paused_at=timezone.now().isoformat())
                bot.save(update_fields=['is_running', 'state_json', 'updated_at'])
                log(bot.user, 'WARN', f'Daily loss limit reached ({cfg["daily_loss_pct"]}%, −${abs(pnl):,.2f}). Bot switched off for today.')
                from .notifications import notify
                notify(bot.user, title='🤖 Bot paused: daily loss limit', message='Your bot stopped for today to protect your account. Switch it back on tomorrow.',
                       category='TRADE', priority='warning', action_url='/bot/', action_label='Open bot')
        except Exception as e:
            logger.warning(f'bot guard {bot.user}: {e}')


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        logger.warning(f'bot {getattr(fn, "__name__", fn)}: {e}')


# ═══════════════════════════════════════════════════════════════
# THE PAGE
# ═══════════════════════════════════════════════════════════════
def radar(bot):
    """What the bot sees right now: open AI setups in its markets, and what it would do with each."""
    from .models import ScannerSignal
    cfg = config(bot)
    out = []
    for sig in ScannerSignal.objects.filter(outcome='OPEN', symbol__in=cfg['markets']).order_by('-created_at')[:8]:
        ok, why = check(bot, sig, cfg)
        vol = risk = None
        if ok:
            try:
                vol, risk, note = size(bot.user, sig, cfg['risk_pct'])
                if not vol:
                    ok, why = False, note
            except Exception as e:
                ok, why = False, str(e)
        out.append({'sig': sig, 'ok': ok, 'why': why, 'vol': vol, 'risk': risk,
                    'taken': bot_trades(bot.user).filter(bot_events__signal=sig).exists()})
    return out


def stats(user):
    trades = list(bot_trades(user))
    closed = [t for t in trades if t.status != 'OPEN']
    wins = [t for t in closed if Decimal(str(t.pnl or 0)) > 0]
    total = sum((Decimal(str(t.pnl or 0)) for t in closed), Decimal('0'))
    today = today_pnl(user)
    money = lambda v: ('+' if v >= 0 else '−') + f'${abs(v):,.2f}'
    return {'trades': len(trades), 'open': len(trades) - len(closed), 'closed': len(closed),
            'win_rate': round(len(wins) / len(closed) * 100) if closed else None, 'total': total, 'today': today,
            'total_txt': money(total), 'today_txt': money(today)}
