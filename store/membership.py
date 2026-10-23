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


def tier_of(user):
    from .models import UserProfile
    try:
        p = UserProfile.objects.filter(user=user).first()
    except Exception:
        p = None
    return (str(getattr(p, 'subscription_tier', '') or 'free').upper().strip(), bool(getattr(p, 'is_active_subscription', False)))


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
