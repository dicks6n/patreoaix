# store/free_signals.py
"""
Free members and the AI signals: see them, a little later.

  • /signals/free/  every A / A+ signal of the last 7 days. Pro members (and the trial) see them the moment the
    scanner records them; free members see the full levels FREE_DELAY_MIN minutes later. Newer ones show as a
    locked card (market + grade + "unlocks in N min") so they know what they are missing.
  • One delayed signal a day as a notification: the first A / A+ signal of the day, FREE_DELAY_MIN minutes after
    Pro members got it, to free members who were active lately and kept "Signals" notifications on.
  • "Pro members got it at 08:42": when a signal hits its target, free members get the result and the time Pro
    members were told (at most PROMO_PER_DAY a day).

Run by the background worker on every pass (signal_scanner → tick). Sent markers live in the cache (90 days), so a
restart never repeats a message. In the bell and as phone pop-ups only: never emailed (keeps the sender reputation).
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

FREE_DELAY_MIN = 30                   # free members see a signal this long after Pro members
WINDOW_DAYS = 7                       # the free signals page shows this many days
STRONG = ('A', 'A+')
DAILY_FREE = 1                        # delayed signals sent as a notification to free members per day
PROMO_PER_DAY = 2                     # "Pro members got it at …" result notices per day
ACTIVE_DAYS = 30                      # only members who logged in lately
MAX_AUDIENCE = 3000                   # per message (most recent logins first)


def delay():
    return timedelta(minutes=FREE_DELAY_MIN)


def is_pro(user):
    from .membership import is_member
    return is_member(user)


def page(user):
    """Rows for /signals/free/: unlocked (full levels) or locked (market + grade + minutes left)."""
    from .models import ScannerSignal
    now = timezone.now()
    pro = is_pro(user)
    rows = []
    for s in ScannerSignal.objects.filter(grade__in=STRONG, created_at__gte=now - timedelta(days=WINDOW_DAYS)).order_by('-created_at')[:60]:
        opens = s.created_at + delay()
        locked = not pro and s.outcome == 'OPEN' and now < opens
        rows.append({'s': s, 'locked': locked, 'mins_left': max(1, int((opens - now).total_seconds() // 60) + 1) if locked else 0,
                     'pro_at': s.created_at, 'free_at': opens})
    decided = [r['s'] for r in rows if r['s'].outcome in ('TP', 'SL')]
    wins = sum(1 for s in decided if s.outcome == 'TP')
    return {'rows': rows, 'pro': pro, 'delay': FREE_DELAY_MIN, 'locked_n': sum(r['locked'] for r in rows),
            'win_rate': round(wins / len(decided) * 100) if decided else None, 'decided': len(decided), 'days': WINDOW_DAYS}


def audience():
    """Free members who were active lately and keep signal notifications on."""
    from django.contrib.auth.models import User
    from . import notif_prefs
    from .membership import is_member
    since = timezone.now() - timedelta(days=ACTIVE_DAYS)
    out = []
    for u in User.objects.filter(is_active=True, is_staff=False, last_login__gte=since).order_by('-last_login')[:MAX_AUDIENCE * 2]:
        if is_member(u):
            continue
        try:
            if not notif_prefs.get(u)['cats'].get('signals', True):
                continue
        except Exception:
            pass
        out.append(u)
        if len(out) >= MAX_AUDIENCE:
            break
    return out


def _fmt(sig, v):
    from .signal_engine import _fmt as f
    try:
        return f(sig.symbol, v)
    except Exception:
        return f'{v}'


def _day_count(kind):
    key = f"free:{kind}:{timezone.now():%Y%m%d}"
    cache.add(key, 0, 2 * 86400)
    return key, cache.get(key) or 0


def _bump(key):
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 2 * 86400)


def send_delayed():
    """The day's free signal: the first A / A+ signal that is FREE_DELAY_MIN old and still open."""
    from .models import ScannerSignal
    from .notifications import notify
    key, n = _day_count('daily')
    if n >= DAILY_FREE:
        return 0
    now = timezone.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cand = (ScannerSignal.objects.filter(grade__in=STRONG, outcome='OPEN', created_at__gte=max(start_of_day, now - delay() - timedelta(hours=2)),
                                         created_at__lte=now - delay()).order_by('created_at').first())
    if not cand or not cache.add(f'free:sent:{cand.id}', 1, 90 * 86400):
        return 0
    _bump(key)
    users = audience()
    f = lambda v: _fmt(cand, v)
    title = f"🔓 Your free signal today: {cand.grade} {cand.direction} {cand.symbol}"
    msg = (f"{cand.timeframe.upper()} · entry {f(cand.entry)} · stop {f(cand.stop_loss)} · target {f(cand.take_profit)}. "
           f"Pro members got it {FREE_DELAY_MIN} min earlier, at {cand.created_at:%H:%M} UTC, with every other signal of the day. "
           "Check the price before entering: it may have moved. Not financial advice.")
    sent = 0
    for u in users:
        if notify(u, title=title, message=msg, category='SYSTEM', priority='info', action_url='/signals/free/',
                  action_label='See free signals', metadata={'kind': 'free_signal', 'signal_id': cand.id, 'no_email': True}):
            sent += 1
    logger.info(f'Free delayed signal {cand.id} sent to {sent} members')
    return sent


def send_promos(closed):
    """'Pro members got it at …' for signals that just hit their target."""
    from .notifications import notify
    from .membership import TRIAL_DAYS
    sent = 0
    for sig in closed:
        if sig.outcome != 'TP' or sig.grade not in STRONG or (sig.result_r or 0) <= 0:
            continue
        key, n = _day_count('promo')
        if n >= PROMO_PER_DAY:
            break
        if not cache.add(f'free:promo:{sig.id}', 1, 90 * 86400):
            continue
        _bump(key)
        title = f"✅ {sig.symbol} {sig.direction} hit its target: {sig.result_r:+.2f}R"
        users = audience()
        for u in users:
            from .membership import trial_available
            cta = f'Start your free {TRIAL_DAYS}-day Pro trial' if trial_available(u) else 'Upgrade to Pro'
            msg = (f"Pro members got this {sig.grade} {sig.timeframe.upper()} signal at {sig.created_at:%H:%M} UTC "
                   f"(entry {_fmt(sig, sig.entry)}, target {_fmt(sig, sig.take_profit)}). Free members see signals "
                   f"{FREE_DELAY_MIN} min later. {cta} to get every signal the moment it appears. "
                   "Past results do not guarantee future ones; every result, losses included, is on /results/.")
            if notify(u, title=title, message=msg, category='SYSTEM', priority='success', action_url='/subscription/',
                      action_label=cta.split(' to ')[0], metadata={'kind': 'pro_promo', 'signal_id': sig.id, 'no_email': True}):
                sent += 1
    return sent


def tick(closed=()):
    return send_delayed() + send_promos(closed)


def safe_tick(closed=()):
    try:
        return tick(closed)
    except Exception as e:
        logger.warning(f'free signals tick failed: {e}')
        return 0
