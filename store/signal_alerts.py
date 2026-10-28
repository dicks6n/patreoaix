# store/signal_alerts.py
"""
Database side of the confluence engine: snapshots, strong-signal log,
notifications and outcome tracking. Used by manage.py signal_scanner and by
the market overview API.
"""
import logging
from datetime import timedelta

from django.db import DatabaseError
from django.db.models import Avg, Count, Q
from django.utils import timezone

from . import chart_scanner as cs
from . import signal_engine as se
from .models import ScannerSignal, ScannerSnapshot, SignalAlertSubscription
from .notifications import notify

logger = logging.getLogger(__name__)

TF_SECONDS = {'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800}
EXPIRY_BARS = cs.HORIZON * 2         # same window the chart scanner backtest gives a trade
COOLDOWN_BARS = 4                     # main-timeframe candles before the same call can repeat
WORKER_FRESH = 180                    # seconds: the worker counts as running if it saved this recently


# ═══════════════════════════════════════════════════════════════
# SNAPSHOTS
# ═══════════════════════════════════════════════════════════════
def save_snapshot(style, results, strength, source='web'):
    # only a cache: a busy database (SQLite 'database is locked') or a missing migration must not fail the request
    try:
        ScannerSnapshot.objects.update_or_create(style=style, defaults={'results': results, 'strength': strength, 'source': source})
        return True
    except DatabaseError as e:
        logger.warning(f'Scanner snapshot not saved ({style}): {e}')
        return False


def get_snapshot(style, max_age=None):
    try:
        snap = ScannerSnapshot.objects.filter(style=style).first()
    except DatabaseError as e:
        logger.warning(f'Scanner snapshot not readable ({style}): {e}')
        return None
    if not snap:
        return None
    if max_age is not None and (timezone.now() - snap.updated_at).total_seconds() > max_age:
        return None
    return snap


def worker_status():
    try:
        last = ScannerSnapshot.objects.filter(source='worker').order_by('-updated_at').first()
    except DatabaseError:
        return {'running': False, 'last_run': None}
    if not last:
        return {'running': False, 'last_run': None}
    age = (timezone.now() - last.updated_at).total_seconds()
    return {'running': age <= WORKER_FRESH, 'last_run': last.updated_at.isoformat(), 'age': int(age)}


def confluence(style, symbols=None, fresh=False):
    """Latest confluence results: the worker's snapshot when recent, otherwise computed now."""
    snap = None if fresh else get_snapshot(style, max_age=120)
    if snap:
        results, strength, when = snap.results, snap.strength, snap.updated_at
    else:
        results, strength = se.scan_all(style)
        save_snapshot(style, results, strength, 'web')
        when = timezone.now()
    if symbols:
        results = [r for r in results if r['symbol'] in symbols]
    return results, strength, when


# ═══════════════════════════════════════════════════════════════
# STRONG SIGNALS
# ═══════════════════════════════════════════════════════════════
def subscribers(style, grade, symbol):
    from .membership import is_member
    rank = se.GRADE_RANK.get(grade, 0)
    for sub in SignalAlertSubscription.objects.filter(enabled=True, style=style, user__is_active=True).select_related('user'):
        if rank < se.GRADE_RANK.get(sub.min_grade, 2):
            continue
        if sub.symbols and symbol not in sub.symbols:
            continue
        if not is_member(sub.user):          # signal alerts are for Pro / VIP members only
            continue
        yield sub.user


def record_signals(results, style):
    """Log new strong setups and notify subscribers. Returns the new ScannerSignal rows."""
    created = []
    for r in results:
        p = r.get('plan') or {}
        if not r.get('strong') or not p.get('entry') or not p.get('sl') or not p.get('tp1'):
            continue
        same = ScannerSignal.objects.filter(symbol=r['symbol'], style=style)
        if same.filter(Q(outcome='OPEN', direction=r['direction']) | Q(bar_time=r['bar_time'], direction=r['direction'])).exists():
            continue                                    # already told people about this one
        cooldown = timezone.now() - timedelta(seconds=TF_SECONDS.get(r['main_tf'], 3600) * COOLDOWN_BARS)
        if same.filter(direction=r['direction'], created_at__gte=cooldown).exists():
            continue                                    # just had one: wait a few candles before calling it again
        for old in same.filter(outcome='OPEN').exclude(direction=r['direction']):
            _close(old, 'EXPIRED', p['entry'])           # the market flipped: retire the opposite call
        sig = ScannerSignal.objects.create(
            symbol=r['symbol'], style=style, timeframe=r['main_tf'], direction=r['direction'], grade=r['grade'],
            confluence=r['confluence'], quality=r['quality'], entry=p['entry'], stop_loss=p['sl'], take_profit=p['tp1'],
            take_profit_2=p.get('tp2'), rr=p.get('rr'), bar_time=r['bar_time'], agree=r['agree'], scanners=r['scanners'],
        )
        title, message = se.alert_text(r)
        n = 0
        for user in subscribers(style, r['grade'], r['symbol']):
            if notify(user, title=title, message=message, category='TRADE',
                      priority='success' if r['direction'] == 'BUY' else 'warning',
                      action_url=f"/market-overview/?symbol={r['symbol']}&style={style}", action_label='Open setup',
                      metadata={'kind': 'ai_signal', 'signal_id': sig.id, 'symbol': r['symbol'], 'direction': r['direction'], 'grade': r['grade']}):
                n += 1
        ScannerSignal.objects.filter(pk=sig.pk).update(notified=n)
        sig.notified = n
        created.append(sig)
        from . import copy_trading                     # copy onto customers' Deriv accounts first (AI Copy Trading)
        copy_trading.safe(copy_trading.on_new_signal, sig)
        from . import telegram_bot                     # post to the Telegram channel (never breaks the scan)
        telegram_bot.safe(telegram_bot.post_signal, sig, r)
        from . import poster, tiktok                    # 1080×1920 poster (media/posters/), posted to TikTok
        tiktok.safe_post(sig, poster.safe_save(sig))
    return created


def _close(sig, outcome, exit_price):
    risk = abs(sig.entry - sig.stop_loss) or 1e-12
    move = (exit_price - sig.entry) if sig.direction == 'BUY' else (sig.entry - exit_price)
    sig.outcome = outcome
    sig.result_r = round(move / risk, 2)
    sig.closed_at = timezone.now()
    sig.save(update_fields=['outcome', 'result_r', 'closed_at'])
    from . import copy_trading                           # close the customers' copies of it
    copy_trading.safe(copy_trading.on_signal_closed, sig)


def update_outcomes(notify_users=True):
    """Walk the candles after each open signal: stop first if both touch in one bar (pessimistic)."""
    closed = []
    for sig in ScannerSignal.objects.filter(outcome='OPEN'):
        candles = cs.candles_for(sig.symbol, sig.timeframe)['candles']
        after = [c for c in candles if c['time'] > sig.bar_time]
        last_closed = after[:-1]                        # the final candle is still forming
        result = None
        for c in last_closed:
            lo, hi = float(c['low']), float(c['high'])
            hit_sl = lo <= sig.stop_loss if sig.direction == 'BUY' else hi >= sig.stop_loss
            hit_tp = hi >= sig.take_profit if sig.direction == 'BUY' else lo <= sig.take_profit
            if hit_sl:
                result = ('SL', sig.stop_loss)
                break
            if hit_tp:
                result = ('TP', sig.take_profit)
                break
        if not result and len(last_closed) >= EXPIRY_BARS:
            result = ('EXPIRED', float(last_closed[EXPIRY_BARS - 1]['close']))
        if not result:
            continue
        _close(sig, *result)
        closed.append(sig)
        from . import telegram_bot                     # reply in the channel with the result
        telegram_bot.safe(telegram_bot.post_outcome, sig)
        from . import poster, tiktok
        tiktok.safe_post(sig, poster.safe_save(sig, result=True), True)
        if notify_users and sig.notified:
            word = {'TP': 'hit its target', 'SL': 'hit its stop', 'EXPIRED': 'expired'}[sig.outcome]
            for user in subscribers(sig.style, sig.grade, sig.symbol):
                notify(user, title=f"{sig.symbol} {sig.direction} signal {word} ({sig.result_r:+.2f}R)",
                       message=f"{sig.grade} {sig.direction} {sig.symbol} {sig.timeframe.upper()} from {sig.created_at:%d %b %H:%M} UTC "
                               f"closed {sig.outcome} at {result[1]} ({sig.result_r:+.2f}R).",
                       category='TRADE', priority='success' if sig.result_r > 0 else 'info',
                       action_url=f"/market-overview/?symbol={sig.symbol}&style={sig.style}", action_label='View',
                       metadata={'kind': 'ai_signal_outcome', 'signal_id': sig.id})
    return closed


def history(style=None, limit=30, days=30):
    qs = ScannerSignal.objects.all()
    if style:
        qs = qs.filter(style=style)
    recent = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    agg = recent.exclude(outcome='OPEN').aggregate(n=Count('id'), avg_r=Avg('result_r'),
                                                   tp=Count('id', filter=Q(outcome='TP')), sl=Count('id', filter=Q(outcome='SL')))
    decided = agg['tp'] + agg['sl']
    stats = {
        'days': days, 'signals': recent.count(), 'open': recent.filter(outcome='OPEN').count(),
        'closed': agg['n'], 'tp': agg['tp'], 'sl': agg['sl'],
        'hit_rate': round(agg['tp'] / decided, 3) if decided else None,
        'avg_r': round(agg['avg_r'], 2) if agg['avg_r'] is not None else None,
    }
    rows = [{
        'id': s.id, 'symbol': s.symbol, 'style': s.style, 'timeframe': s.timeframe, 'direction': s.direction, 'grade': s.grade,
        'quality': s.quality, 'entry': s.entry, 'sl': s.stop_loss, 'tp': s.take_profit, 'rr': s.rr,
        'agree': len(s.agree or []), 'outcome': s.outcome, 'result_r': s.result_r,
        'created_at': s.created_at.isoformat(), 'closed_at': s.closed_at.isoformat() if s.closed_at else None,
    } for s in qs[:limit]]
    return rows, stats
