# store/context_processors.py
from django.conf import settings

from .models import KYCProfile


def site_flags(request):
    """Server capabilities the pages adapt to (websockets are off on hosts like PythonAnywhere)."""
    return {'websockets_enabled': getattr(settings, 'WEBSOCKETS_ENABLED', True)}


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