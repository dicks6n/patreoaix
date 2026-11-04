# store/membership.py
"""
Plans and what each one opens. Every plan has a level (rank); a feature asks for a minimum level.

  0  Free / Starter   first lessons of each course, manual trading, buying on P2P
  1  Academy          every course and topic, cheat sheets, unlimited "Read my chart" AI
  2  Trader           Academy + AI signals everywhere, price & signal alerts, P2P selling
  3  Pro (and trial)  Trader + bots, copy trading, AI auto-trader, $500 funded account
  4  VIP              everything + high-risk P2P methods, $2,500 funded account
     Staff            everything

The level is the higher of the paid plan and a reward pass (UserProfile.pass_tier / pass_until: days of a plan
won in Rewards; it never changes billing).

members_only(view) wraps a view in urls.py: pages show the "members only" screen,
API calls get a 403 JSON answer the pages already display as an error.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import render

MEMBER_TIERS = ('PRO', 'VIP')          # Pro-level paid plans (funded account, plan extension by Pro days)
PAID_TIERS = ('ACADEMY', 'TRADER', 'PRO', 'VIP')
RANKS = {'FREE': 0, 'STARTER': 0, 'ACADEMY': 1, 'TRADER': 2, 'PRO': 3, 'PRO_TRIAL': 3, 'VIP': 4}
PLAN_NAMES = {0: 'Free', 1: 'Academy', 2: 'Trader', 3: 'Pro', 4: 'VIP'}
SIGNALS_RANK, AUTO_RANK, ACADEMY_RANK = 2, 3, 1
MEMBERS_ONLY_MSG = 'Signals are for Trader, Pro and VIP members. Upgrade to unlock every AI signal.'
AUTO_TRADING_MSG = 'Automated trading is for Pro and VIP members. Upgrade to unlock bots, copy trading and the auto-trader.'


TRIAL_TIER = 'pro_trial'          # the free trial: Pro features, ends by itself
TRIAL_DAYS = 3


def tier_of(user):
    from .models import UserProfile
    try:
        p = UserProfile.objects.filter(user=user).first()
    except Exception:
        p = None
    if p is not None and p.subscription_tier == TRIAL_TIER and p.is_active_subscription:
        from django.utils import timezone
        if not p.subscription_expires_at or p.subscription_expires_at <= timezone.now():
            _end_trial(p)
    return (str(getattr(p, 'subscription_tier', '') or 'free').upper().strip(), bool(getattr(p, 'is_active_subscription', False)))


def rank_of_tier(tier):
    t = str(tier or '').upper().strip()
    if t in RANKS:
        return RANKS[t]
    return max([r for k, r in RANKS.items() if k in t] or [0])


def sub_rank(user):
    """Level of the paid plan / trial alone (no reward pass)."""
    tier, active = tier_of(user)
    return rank_of_tier(tier) if active else 0


def pass_rank(user):
    from django.utils import timezone
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).only('pass_tier', 'pass_until').first()
    if p and p.pass_tier and p.pass_until and p.pass_until > timezone.now():
        return rank_of_tier(p.pass_tier)
    return 0


def rank(user):
    """0 Free · 1 Academy · 2 Trader · 3 Pro · 4 VIP (staff 4)."""
    if not getattr(user, 'is_authenticated', False):
        return 0
    if user.is_staff or user.is_superuser:
        return 4
    return max(sub_rank(user), pass_rank(user))


def has_rank(user, n):
    return rank(user) >= n


def plan_name(user):
    return PLAN_NAMES.get(rank(user), 'Free')


def grant_pass(user, tier, days, title='', message=''):
    """Days of a plan won in Rewards: a pass on top of the paid plan (extends a pass of the same plan)."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import UserProfile, Notification
    p, _ = UserProfile.objects.get_or_create(user=user)
    now = timezone.now()
    tier = str(tier).lower()
    if p.pass_tier == tier and p.pass_until and p.pass_until > now:
        p.pass_until += timedelta(days=days)
    elif not (p.pass_until and p.pass_until > now and rank_of_tier(p.pass_tier) > rank_of_tier(tier)):
        p.pass_tier, p.pass_until = tier, now + timedelta(days=days)
    else:                                          # a higher pass is running: add the days after it
        p.pass_until += timedelta(days=days)
    p.save(update_fields=['pass_tier', 'pass_until'])
    if title:
        try:
            Notification.objects.create(user=user, title=title, category='ACCOUNT', priority='success',
                                        message=message or f'{days} days of {tier.title()} added.',
                                        action_url='/rewards/', action_label='See rewards')
        except Exception:
            pass
    return p.pass_until


def trial_available(user):
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).first()
    return bool(user.is_authenticated and sub_rank(user) == 0 and not user.is_staff and not (p and p.trial_used))


def start_trial(user, days=None):
    """Give the user the free Pro trial once. Returns True when it started."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import UserProfile, Notification
    days = days or TRIAL_DAYS
    p, _ = UserProfile.objects.get_or_create(user=user)
    if p.trial_used or (p.is_active_subscription and p.subscription_tier != TRIAL_TIER):
        return False
    now = timezone.now()
    p.subscription_tier, p.is_active_subscription, p.trial_used = TRIAL_TIER, True, True
    p.subscription_started_at, p.subscription_expires_at = now, now + timedelta(days=days)
    p.save(update_fields=['subscription_tier', 'is_active_subscription', 'trial_used',
                          'subscription_started_at', 'subscription_expires_at'])
    try:
        Notification.objects.create(user=user, title=f'🎉 Your {days}-day Pro trial has started', category='ACCOUNT',
                                    priority='success', action_url='/market-overview/', action_label='See AI signals',
                                    message='All AI signals, the entry coach, alerts and auto-trading are unlocked until '
                                            f'{p.subscription_expires_at:%d %b %H:%M} UTC.')
    except Exception:
        pass
    return True


def _end_trial(p):
    from .models import Notification
    p.subscription_tier, p.is_active_subscription = 'free', False
    p.save(update_fields=['subscription_tier', 'is_active_subscription'])
    try:
        Notification.objects.create(user=p.user, title='Your Pro trial has ended', category='ACCOUNT', priority='info',
                                    action_url='/subscription/', action_label='Subscribe',
                                    message='Subscribe to Pro to keep getting every AI signal, alerts and auto-trading.')
    except Exception:
        pass


def trial_days_left(user):
    from django.utils import timezone
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).first()
    if not (p and p.subscription_tier == TRIAL_TIER and p.is_active_subscription and p.subscription_expires_at):
        return None
    secs = (p.subscription_expires_at - timezone.now()).total_seconds()
    return max(0, int(secs // 86400) + (1 if secs % 86400 else 0))


def is_member(user):
    """May see AI signals: Trader, Pro, VIP, the Pro trial or a reward pass of those (or staff)."""
    return rank(user) >= SIGNALS_RANK


def can_auto_trade(user):
    """Bots, copy trading, the AI auto-trader: Pro / VIP (and the trial)."""
    return rank(user) >= AUTO_RANK


def _wants_json(request):
    return (request.path.startswith('/api/')
            or request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('accept', '')
            or 'application/json' in request.headers.get('content-type', ''))


def members_only_response(request, feature='Signals', message=MEMBERS_ONLY_MSG):
    tier, active = tier_of(request.user)
    if _wants_json(request):
        return JsonResponse({'success': False, 'error': message, 'members_only': True,
                             'tier': tier, 'upgrade_url': '/subscription/'}, status=403)
    return render(request, 'store/members_only.html', {
        'feature': feature, 'message': message,
        'tier': tier if active or tier not in MEMBER_TIERS else f'{tier} (inactive)',
    }, status=403)


def members_only(view, feature='Signals', message=MEMBERS_ONLY_MSG, min_rank=SIGNALS_RANK):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if _wants_json(request):
                return JsonResponse({'success': False, 'error': 'Please log in.'}, status=401)
            return redirect_to_login(request.get_full_path())
        if rank(request.user) < min_rank:
            return members_only_response(request, feature, message)
        return view(request, *args, **kwargs)
    return wrapped


def auto_trading_only(view):
    return members_only(view, feature='Automated trading', message=AUTO_TRADING_MSG, min_rank=AUTO_RANK)


def grant_pro_days(user, days, title, message=''):
    """Give `days` of Pro: extends a paid plan or a trial, or opens a Pro pass for a free account."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import UserProfile, Notification
    p, _ = UserProfile.objects.get_or_create(user=user)
    now = timezone.now()
    tier = str(p.subscription_tier or '').upper()
    if p.is_active_subscription and any(t in tier for t in MEMBER_TIERS):
        p.subscription_expires_at = max(p.subscription_expires_at or now, now) + timedelta(days=days)
        p.save(update_fields=['subscription_expires_at'])
    elif p.is_active_subscription and rank_of_tier(tier) > 0:
        grant_pass(user, 'pro', days)                # Academy / Trader subscribers: a Pro pass, their plan stays
    else:
        p.subscription_tier, p.is_active_subscription = TRIAL_TIER, True
        p.subscription_started_at, p.subscription_expires_at = now, now + timedelta(days=days)
        p.save(update_fields=['subscription_tier', 'is_active_subscription', 'subscription_started_at', 'subscription_expires_at'])
    try:
        Notification.objects.create(user=user, title=title, category='ACCOUNT', priority='success',
                                    message=message or f'{days} days of Pro added: every AI signal is unlocked.',
                                    action_url='/market-overview/', action_label='See AI signals')
    except Exception:
        pass
