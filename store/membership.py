# store/membership.py
"""
Who may see signals and use automated trading.

  Pro / VIP (active subscription)   signals everywhere (market overview scanner, signal pages,
                                    prediction models, alerts, copy signals) + AI Gold auto-trader, bots
  Starter / free                    manual trading only (terminal, trade page, manual digits trade)
  Staff                             everything

members_only(view) wraps a view in urls.py: pages show the "members only" screen,
API calls get a 403 JSON answer the pages already display as an error.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import render

MEMBER_TIERS = ('PRO', 'VIP')
MEMBERS_ONLY_MSG = 'Signals are for Pro and VIP members. Starter accounts trade manually: upgrade to unlock signals.'
AUTO_TRADING_MSG = 'Automated trading is for Pro and VIP members. Starter accounts trade manually: upgrade to unlock it.'


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


def trial_available(user):
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).first()
    return bool(user.is_authenticated and not is_member(user) and not (p and p.trial_used))


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
    """Active Pro or VIP subscription (or staff)."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    tier, active = tier_of(user)
    return active and any(t in tier for t in MEMBER_TIERS)


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


def members_only(view, feature='Signals', message=MEMBERS_ONLY_MSG):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if _wants_json(request):
                return JsonResponse({'success': False, 'error': 'Please log in.'}, status=401)
            return redirect_to_login(request.get_full_path())
        if not is_member(request.user):
            return members_only_response(request, feature, message)
        return view(request, *args, **kwargs)
    return wrapped


def auto_trading_only(view):
    return members_only(view, feature='Automated trading', message=AUTO_TRADING_MSG)


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
