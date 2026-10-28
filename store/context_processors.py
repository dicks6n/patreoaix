# store/context_processors.py
from django.conf import settings

from .models import KYCProfile


def site_flags(request):
    """Server capabilities the pages adapt to (websockets are off on hosts like PythonAnywhere)."""
    return {'websockets_enabled': getattr(settings, 'WEBSOCKETS_ENABLED', True),
            'deriv_ws_app_id': getattr(settings, 'DERIV_WS_APP_ID', '1089'),
            'deriv_public_ws_url': _deriv_public_ws()}


def membership(request):
    """member_tier / is_member for the Subscribe button in the header and sidebar."""
    if not request.user.is_authenticated:
        return {'is_member': False, 'member_tier': ''}
    try:
        from .membership import tier_of, MEMBER_TIERS, trial_days_left, trial_available
        tier, active = tier_of(request.user)
        member = active and any(t in tier for t in MEMBER_TIERS)
        days = trial_days_left(request.user) if member and 'TRIAL' in tier else None
        return {'is_member': member, 'member_tier': (f'TRIAL · {days}d' if days is not None else tier) if member else '',
                'on_trial': days is not None, 'trial_available': not member and trial_available(request.user)}
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
