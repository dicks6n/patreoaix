# store/rewards.py
"""
Rewards: points for useful actions → 6 levels → each level's rewards are claimed once.

  Earn      lessons, academy topics, verifying identity, deposits, daily visits and 7-day streaks, trading days,
            P2P trades, friends who join, passing a challenge phase, weekly missions.
  Levels    Starter · Bronze · Silver · Gold · Platinum · Diamond
  Rewards   days of a plan (a pass on top of the paid plan, store/membership.grant_pass), a cash trading bonus
            (withdrawable after BONUS_TURNOVER_X × its amount of trading, like the deposit bonus), free funded
            challenges, % off the next challenge, a P2P fee discount, badges.
  Safety    every action counts once (RewardEvent is unique per user + kind + ref); cash and challenges need
            approved KYC; sample/test accounts (.invalid emails) earn nothing.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)
D = Decimal

# kind: (points, label shown in "How to earn", how often)
EARN = {
    'signup': (50, 'Create your account', 'once'),
    'goal': (25, 'Pick what brings you here', 'once'),
    'kyc': (300, 'Verify your identity', 'once'),
    'first_deposit': (300, 'Make your first deposit', 'once'),
    'deposit': (50, 'Make a deposit', 'per day'),
    'login': (10, 'Visit PATREOAIX', 'per day'),
    'streak7': (150, '7 days in a row', 'every 7 days'),
    'lesson': (20, 'Pass an academy lesson', 'per lesson'),
    'academy_grad': (300, 'Graduate the beginner academy', 'once'),
    'topic': (5, 'Read an academy topic', 'per topic'),
    'chart_ai': (10, 'Get an AI read of a chart', 'per day'),
    'practice': (3, 'Make a practice trade (up to 15 a day)', 'per trade'),
    'practice_win': (5, 'A textbook practice decision', 'up to 10 a day'),
    'trade_day': (15, 'Trade on the terminal', 'per day'),
    'p2p_trade': (40, 'Complete a P2P trade', 'per trade'),
    'referral': (250, 'Invite a friend who becomes a customer', 'per friend'),
    'challenge_pass': (500, 'Pass a funded challenge phase', 'per phase'),
}

# (name, icon, points needed, rewards). Reward tuples:
#   ('pass', tier, days) · ('cash', usd) · ('challenge', plan_type, size) · ('challenge_off', pct)
#   ('p2p_fee', pct_off, days) · ('badge', name)
LEVELS = [
    ('Starter', '🌱', 0, []),
    ('Bronze', '🥉', 500, [('pass', 'academy', 7), ('badge', 'Bronze')]),
    ('Silver', '🥈', 1500, [('cash', 5), ('pass', 'trader', 3), ('p2p_fee', 10, 30)]),
    ('Gold', '🥇', 4000, [('cash', 10), ('pass', 'pro', 7), ('challenge_off', 20)]),
    ('Platinum', '💎', 9000, [('cash', 25), ('challenge', 'two_step', 5000), ('p2p_fee', 25, 60)]),
    ('Diamond', '👑', 20000, [('cash', 50), ('challenge', 'one_step', 10000), ('pass', 'pro', 30)]),
]

# weekly missions: key → (title, kinds counted, target, bonus points)
MISSIONS = {
    'learn3': ('Finish 3 lessons or academy topics', ('lesson', 'topic'), 3, 60),
    'login4': ('Visit on 4 different days', ('login',), 4, 40),
    'trade3': ('Trade on 3 different days', ('trade_day',), 3, 60),
    'chart1': ('Get an AI read of one of your charts', ('chart_ai',), 1, 30),
    'p2p1': ('Complete a P2P trade', ('p2p_trade',), 1, 40),
    'practice10': ('Make 10 practice trades in the simulator', ('practice',), 10, 50),
}
MISSIONS_FOR = {'learn': ('learn3', 'practice10', 'chart1', 'login4'), 'part': ('trade3', 'practice10', 'learn3', 'login4'),
                'pro': ('trade3', 'p2p1', 'login4'), 'funded': ('trade3', 'learn3', 'login4')}
DEFAULT_MISSIONS = ('login4', 'learn3', 'trade3')


class RewardError(Exception):
    """Shown to the customer as-is."""


def reward_label(r):
    k = r[0]
    if k == 'pass':
        return f'{r[2]} days of {r[1].title()}'
    if k == 'cash':
        return f'${r[1]} trading bonus'
    if k == 'challenge':
        return f"Free ${r[2]:,} {'1-Step' if r[1] == 'one_step' else '2-Step'} funded challenge"
    if k == 'challenge_off':
        return f'{r[1]}% off your next funded challenge'
    if k == 'p2p_fee':
        return f'{r[1]}% off P2P fees for {r[2]} days'
    if k == 'badge':
        return f'{r[1]} badge on your profile'
    return str(r)


def _real(user):
    return bool(user and getattr(user, 'is_authenticated', True) and user.is_active
                and not (getattr(user, 'email', '') or '').lower().endswith('.invalid'))


# ── points ──────────────────────────────────────────────────
def points(user):
    from .models import RewardEvent
    return RewardEvent.objects.filter(user=user).aggregate(s=Sum('points'))['s'] or 0


def level_index(pts):
    i = 0
    for n, (_, _, need, _) in enumerate(LEVELS):
        if pts >= need:
            i = n
    return i


def award(user, kind, ref='', pts=None):
    """Give the points for `kind` once per (kind, ref). Returns the points given (0 when already counted)."""
    from .models import RewardEvent, Notification
    if not _real(user):
        return 0
    pts = EARN.get(kind, (0,))[0] if pts is None else pts
    if not pts:
        return 0
    before = points(user)
    try:
        with transaction.atomic():
            RewardEvent.objects.create(user=user, kind=kind, ref=str(ref)[:60], points=pts)
    except IntegrityError:
        return 0
    after = before + pts
    if level_index(after) > level_index(before):
        name, icon = LEVELS[level_index(after)][:2]
        try:
            Notification.objects.create(user=user, category='ACCOUNT', priority='success',
                                        title=f'{icon} You reached {name}!',
                                        message='New rewards are waiting for you: ' + ', '.join(
                                            reward_label(r) for r in LEVELS[level_index(after)][3]) + '.',
                                        action_url='/rewards/', action_label='Claim rewards')
        except Exception:
            pass
    if kind not in ('mission', 'streak7'):
        _check_missions(user)
    return pts


def safe_award(user, kind, ref='', pts=None):
    try:
        return award(user, kind, ref, pts)
    except Exception as e:
        logger.warning(f'reward {kind} for {user}: {e}')
        return 0


def daily_visit(user):
    """First visit of the day: +login, and +streak7 on every 7th day in a row."""
    from .models import RewardEvent
    today = timezone.localdate()
    if not award(user, 'login', today.isoformat()):
        return
    days = set(RewardEvent.objects.filter(user=user, kind='login', created_at__date__gte=today - timedelta(days=60))
               .values_list('ref', flat=True))
    streak, d = 0, today
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    if streak and streak % 7 == 0:
        award(user, 'streak7', today.isoformat())


def streak(user):
    from .models import RewardEvent
    today = timezone.localdate()
    days = set(RewardEvent.objects.filter(user=user, kind='login', created_at__date__gte=today - timedelta(days=60))
               .values_list('ref', flat=True))
    n, d = 0, today if today.isoformat() in days else today - timedelta(days=1)
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


# ── weekly missions ─────────────────────────────────────────
def _week():
    today = timezone.localdate()
    start = today - timedelta(days=today.weekday())
    y, w, _ = today.isocalendar()
    return f'{y}-W{w:02d}', start


def missions(user):
    from .models import RewardEvent, UserProfile
    week, start = _week()
    goal = UserProfile.objects.filter(user=user).values_list('goal', flat=True).first() or ''
    keys = MISSIONS_FOR.get(goal, DEFAULT_MISSIONS)
    rows = list(RewardEvent.objects.filter(user=user, created_at__date__gte=start).values_list('kind', 'ref'))
    done_refs = {ref for kind, ref in rows if kind == 'mission'}
    out = []
    for k in keys:
        title, kinds, target, bonus = MISSIONS[k]
        got = sum(1 for kind, _ in rows if kind in kinds)
        out.append({'key': k, 'title': title, 'target': target, 'progress': min(got, target), 'points': bonus,
                    'pct': int(min(got, target) / target * 100), 'done': f'{week}:{k}' in done_refs})
    return out


def _check_missions(user):
    week, _ = _week()
    for m in missions(user):
        if m['progress'] >= m['target'] and not m['done']:
            award(user, 'mission', f'{week}:{m["key"]}', m['points'])


# ── claiming a level ────────────────────────────────────────
def _kyc_ok(user):
    from .models import KYCProfile
    return user.is_staff or KYCProfile.objects.filter(user=user, status__iexact='APPROVED').exists()


def claim(user, level):
    """Give every reward of `level` once. Returns the RewardClaim."""
    from . import membership
    from .models import Notification, RewardClaim, Transaction, UserProfile
    try:
        level = int(level)
        name, icon, need, rewards = LEVELS[level]
    except (TypeError, ValueError, IndexError):
        raise RewardError('Unknown level.')
    if not rewards:
        raise RewardError('Nothing to claim at this level.')
    if points(user) < need:
        raise RewardError(f'Reach {name} ({need:,} points) to claim these rewards.')
    if RewardClaim.objects.filter(user=user, level=level).exists():
        raise RewardError('You already claimed these rewards.')
    needs_kyc = any(r[0] in ('cash', 'challenge') for r in rewards)
    if needs_kyc and not _kyc_ok(user):
        raise RewardError('Verify your identity (KYC) first: cash and funded challenge rewards need a verified account.')
    from . import bonus, prop_firm
    with transaction.atomic():
        try:
            c = RewardClaim.objects.create(user=user, level=level, detail=[reward_label(r) for r in rewards])
        except IntegrityError:
            raise RewardError('You already claimed these rewards.')
        p, _ = UserProfile.objects.get_or_create(user=user)
        now = timezone.now()
        for r in rewards:
            k = r[0]
            if k == 'pass':
                membership.grant_pass(user, r[1], r[2])
            elif k == 'cash':
                amt = D(str(r[1]))
                c.cash += amt
                c.required_turnover += (amt * bonus.turnover_x()).quantize(D('0.01'))
                Transaction.objects.create(user=user, amount=amt, transaction_type='BONUS',
                                           details=f'{icon} {name} reward: ${amt} trading bonus')   # credited by the signal
            elif k == 'challenge':
                from .models import PropAccount
                if PropAccount.objects.filter(user=user, status='ACTIVE', kind='challenge').count() >= prop_firm.MAX_ACTIVE_ACCOUNTS:
                    raise RewardError(f'You already run {prop_firm.MAX_ACTIVE_ACCOUNTS} challenges. Finish one, then claim.')
                prop_firm.trader_id(user)
                prop_firm._new_account(user, r[1], r[2], '1', D('0'), f'RWD-{level}-{user.id}')
            elif k == 'challenge_off':
                p.challenge_off_pct = max(p.challenge_off_pct or 0, r[1])
            elif k == 'p2p_fee':
                p.p2p_fee_off_pct = max(p.p2p_fee_off_pct or 0, r[1])
                p.p2p_fee_off_until = max(p.p2p_fee_off_until or now, now) + timedelta(days=r[2])
        p.save(update_fields=['challenge_off_pct', 'p2p_fee_off_pct', 'p2p_fee_off_until'])
        c.save(update_fields=['cash', 'required_turnover'])
    try:
        Notification.objects.create(user=user, category='ACCOUNT', priority='success',
                                    title=f'{icon} {name} rewards claimed', message='You received: ' + ', '.join(c.detail) + '.',
                                    action_url='/rewards/', action_label='See rewards')
    except Exception:
        pass
    return c


# ── reward cash: held back from withdrawals until traded ────
def locked_cash(user):
    """Reward cash still under its trading requirement (released and notified once it is met)."""
    from . import bonus
    from .models import Notification, RewardClaim
    locked = D('0')
    for c in RewardClaim.objects.filter(user=user, cash__gt=0, released_at__isnull=True):
        if bonus.turnover(user, c.created_at) >= c.required_turnover:
            RewardClaim.objects.filter(pk=c.pk).update(released_at=timezone.now())
            try:
                Notification.objects.create(user=user, title='🎁 Reward cash unlocked', category='ACCOUNT', priority='success',
                                            message=f'Your ${c.cash} reward bonus is now withdrawable.',
                                            action_url='/wallet/', action_label='Open wallet')
            except Exception:
                pass
        else:
            locked += D(c.cash)
    return locked


def cash_status(user):
    from . import bonus
    from .models import RewardClaim
    rows = []
    for c in RewardClaim.objects.filter(user=user, cash__gt=0).order_by('created_at'):
        done = min(bonus.turnover(user, c.created_at), D(c.required_turnover))
        rows.append({'level': LEVELS[c.level][0], 'cash': c.cash, 'required': c.required_turnover, 'done': done,
                     'pct': int(done / c.required_turnover * 100) if c.required_turnover else 100,
                     'released': bool(c.released_at) or done >= c.required_turnover})
    return rows


# ── discounts used by P2P and the funded challenge shop ─────
def p2p_fee_discount(user):
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).only('p2p_fee_off_pct', 'p2p_fee_off_until').first()
    if p and p.p2p_fee_off_pct and p.p2p_fee_off_until and p.p2p_fee_off_until > timezone.now():
        return int(p.p2p_fee_off_pct)
    return 0


# ── badges (worked out from events) ─────────────────────────
BADGES = [
    ('verified', '🛡️', 'Verified', lambda c: c.get('kyc', 0) >= 1),
    ('first_trade', '📈', 'First trade', lambda c: c.get('trade_day', 0) >= 1),
    ('graduate', '🎓', 'Graduate', lambda c: c.get('academy_grad', 0) >= 1),
    ('bookworm', '📚', 'Bookworm', lambda c: c.get('topic', 0) >= 20),
    ('streak', '🔥', '7-day streak', lambda c: c.get('streak7', 0) >= 1),
    ('p2p', '🤝', 'P2P trader', lambda c: c.get('p2p_trade', 0) >= 5),
    ('ambassador', '📣', 'Ambassador', lambda c: c.get('referral', 0) >= 3),
    ('funded', '🏆', 'Challenge passed', lambda c: c.get('challenge_pass', 0) >= 1),
    ('missions', '🎯', 'Mission master', lambda c: c.get('mission', 0) >= 10),
]


def badges(user):
    from django.db.models import Count
    from .models import RewardEvent
    counts = dict(RewardEvent.objects.filter(user=user).values_list('kind').annotate(n=Count('id')))
    return [{'key': k, 'icon': i, 'name': n, 'got': bool(test(counts))} for k, i, n, test in BADGES]


def summary(user):
    """Everything the rewards page and the header badge need."""
    from .models import RewardClaim, RewardEvent
    pts = points(user)
    i = level_index(pts)
    name, icon, need, _ = LEVELS[i]
    nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    claimed = set(RewardClaim.objects.filter(user=user).values_list('level', flat=True))
    kyc = _kyc_ok(user)
    levels = []
    for n, (ln, li, lneed, rew) in enumerate(LEVELS):
        if not rew:
            continue
        state = 'claimed' if n in claimed else ('ready' if pts >= lneed else 'locked')
        levels.append({'n': n, 'name': ln, 'icon': li, 'need': lneed, 'rewards': [reward_label(r) for r in rew],
                       'state': state, 'kyc': any(r[0] in ('cash', 'challenge') for r in rew) and not kyc,
                       'pct': min(100, int(pts / lneed * 100)) if lneed else 100})
    span = (nxt[2] - need) if nxt else 1
    return {'points': pts, 'level': i, 'name': name, 'icon': icon, 'next': nxt[0] if nxt else None,
            'next_icon': nxt[1] if nxt else '', 'next_need': nxt[2] if nxt else None,
            'to_next': (nxt[2] - pts) if nxt else 0, 'pct': int((pts - need) / span * 100) if nxt else 100,
            'levels': levels, 'ready': sum(1 for l in levels if l['state'] == 'ready'), 'kyc': kyc,
            'streak': streak(user), 'missions': missions(user), 'badges': badges(user),
            'cash': cash_status(user),
            'recent': [{'label': 'Weekly mission' if e.kind == 'mission' else EARN.get(e.kind, (0, e.kind.replace('_', ' ').title()))[1],
                        'points': e.points, 'created_at': e.created_at} for e in RewardEvent.objects.filter(user=user)[:12]],
            'earn': [{'kind': k, 'points': v[0], 'label': v[1], 'often': v[2]} for k, v in EARN.items()]}


class DailyVisitMiddleware:
    """Counts the first visit of each day (points + streak) without touching the database on other requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not request.path.startswith(('/static/', '/media/', '/api/')):
            today = timezone.localdate().isoformat()
            try:
                if request.session.get('rw_day') != today:
                    request.session['rw_day'] = today
                    daily_visit(user)
            except Exception as e:
                logger.warning(f'daily visit reward: {e}')
        return self.get_response(request)
