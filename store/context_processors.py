# store/context_processors.py
from django.conf import settings

from .models import KYCProfile


def _social_logins():
    """Google / Apple sign-in only when the provider is set up (Social applications in admin, or settings),
    otherwise the buttons would lead to an error page."""
    from django.core.cache import cache
    hit = cache.get('social_logins')
    if hit is not None:
        return hit
    found = {k: bool((getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {}).get(k) or {}).get('APP') or
                     (getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {}).get(k) or {}).get('APPS')) for k in ('google', 'apple')}
    try:
        from allauth.socialaccount.models import SocialApp
        for k in SocialApp.objects.filter(provider__in=['google', 'apple']).values_list('provider', flat=True):
            found[k] = True
    except Exception:
        pass
    cache.set('social_logins', found, 300)
    return found


def site_flags(request):
    """Server capabilities the pages adapt to (websockets are off on hosts like PythonAnywhere)."""
    return {'websockets_enabled': getattr(settings, 'WEBSOCKETS_ENABLED', True),
            'deriv_ws_app_id': getattr(settings, 'DERIV_WS_APP_ID', '1089'),
            'deriv_public_ws_url': _deriv_public_ws(), 'social_logins': _social_logins()}


def membership(request):
    """member_tier / is_member for the Subscribe button in the header and sidebar."""
    if not request.user.is_authenticated:
        return {'is_member': False, 'member_tier': ''}
    try:
        from .membership import tier_of, trial_days_left, trial_available, rank, PLAN_NAMES
        tier, active = tier_of(request.user)
        r = rank(request.user)
        member = r > 0
        days = trial_days_left(request.user) if active and 'TRIAL' in tier else None
        avatar = ''
        try:
            from .models import UserProfile
            p = UserProfile.objects.filter(user=request.user).only('avatar').first()
            avatar = p.avatar.url if p and p.avatar else ''
        except Exception:
            pass
        my_level = None
        try:
            from .rewards import LEVELS, level_index, points
            from .models import RewardClaim
            pts = points(request.user)
            li = level_index(pts)
            ready = sum(1 for n, (_, _, need, rew) in enumerate(LEVELS) if rew and pts >= need) - \
                RewardClaim.objects.filter(user=request.user).count()
            my_level = {'name': LEVELS[li][0], 'icon': LEVELS[li][1], 'points': pts, 'ready': max(0, ready)}
        except Exception:
            pass
        return {'is_member': member, 'member_rank': r, 'my_level': my_level,
                'member_tier': (f'TRIAL · {days}d' if days is not None else PLAN_NAMES.get(r, '').upper()) if member else '',
                'on_trial': days is not None, 'trial_available': not member and trial_available(request.user), 'my_avatar': avatar}
    except Exception:
        return {'is_member': False, 'member_tier': ''}


def _deriv_public_ws():
    from . import price_feed
    return price_feed.DERIV_PUBLIC_WS


def kyc_status(request):
    """
    Injects kyc_status into every template context.
    Values: 'APPROVED' | 'PENDING' | 'REJECTED' | 'MISSING'
    """
    if not request.user.is_authenticated:
        return {'kyc_status': 'MISSING'}

    # Staff/superusers are always treated as verified
    if request.user.is_staff or request.user.is_superuser:
        return {'kyc_status': 'APPROVED'}

    try:
        profile = KYCProfile.objects.only('status').filter(user=request.user).first()
    except Exception:
        return {'kyc_status': 'MISSING'}

    if not profile or not profile.status:
        return {'kyc_status': 'MISSING'}

    status = str(profile.status).upper().strip()

    # Normalize common variants
    if status in ('APPROVED', 'VERIFIED', 'VERIFICATION_SUCCESS'):
        return {'kyc_status': 'APPROVED'}
    if status in ('PENDING', 'UNDER_REVIEW', 'IN_REVIEW', 'PROCESSING'):
        return {'kyc_status': 'PENDING'}
    if status in ('REJECTED', 'FAILED', 'DECLINED'):
        return {'kyc_status': 'REJECTED'}

    return {'kyc_status': 'MISSING'}

def promos(request):
    """First-deposit bonus settings for the banners (store/bonus.py) and the trial length."""
    from django.conf import settings
    from . import bonus
    from .membership import TRIAL_DAYS
    return {'bonus_on': bonus.enabled(), 'bonus_pct': f'{bonus.pct():.0f}', 'bonus_cap': f'{bonus.cap():.0f}',
            'bonus_x': f'{bonus.turnover_x():.0f}', 'trial_days': TRIAL_DAYS, 'referral_days': _ref_days(),
            'vapid_public_key': getattr(settings, 'VAPID_PUBLIC_KEY', '') or ''}


def _ref_days():
    from .referral_rewards import days
    return days()


def p2p_attention(request):
    """P2P orders waiting for this user (buyer must pay, seller must release, or an appeal) → bar on every page."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    try:
        from django.db.models import Q
        from .models import P2POrder
        u = request.user
        o = (P2POrder.objects.filter(Q(buyer=u, status='PENDING') | Q(seller=u, status='PAID') |
                                     Q(Q(buyer=u) | Q(seller=u), status='APPEAL'))
             .only('ref', 'status', 'buyer_id', 'asset', 'fiat_amount', 'currency').order_by('created_at').first())
    except Exception:
        return {}
    if not o:
        return {}
    if o.status == 'APPEAL':
        text = f'Appeal open on P2P order {o.ref}: add your proof in the chat.'
    elif o.buyer_id == u.id:
        text = f'P2P order {o.ref}: pay {o.fiat_amount:,.0f} {o.currency} and tap "I have paid" before the timer ends.'
    else:
        text = f'P2P order {o.ref}: the buyer paid. Check your account and release the {o.asset}.'
    return {'p2p_attention': {'ref': o.ref, 'text': text}}
