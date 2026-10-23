"""
SMS OTP generation, storage, and verification.
"""

import random
import secrets
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


def generate_otp(length: int = None) -> str:
    """Generate a secure numeric OTP."""
    length = length or getattr(settings, 'SMS_OTP_LENGTH', 6)
    return ''.join(secrets.choice('0123456789') for _ in range(length))


def store_otp(identifier: str, otp: str, purpose: str = 'login') -> None:
    """Store OTP in cache with expiry."""
    expiry = getattr(settings, 'SMS_OTP_EXPIRY_SECONDS', 300)
    key = f'sms_otp_{purpose}_{identifier}'
    cache.set(key, otp, timeout=expiry)


def verify_otp(identifier: str, otp: str, purpose: str = 'login') -> bool:
    """Verify OTP and consume it (single use)."""
    key = f'sms_otp_{purpose}_{identifier}'
    stored = cache.get(key)

    if not stored:
        return False

    if str(stored) != str(otp):
        return False

    # Single-use — delete after success
    cache.delete(key)
    return True


def get_otp_ttl(identifier: str, purpose: str = 'login') -> int:
    """Return seconds until OTP expires. 0 if expired."""
    key = f'sms_otp_{purpose}_{identifier}'
    value = cache.get(key)
    if not value:
        return 0
    # Django cache doesn't expose TTL uniformly, so we can't get exact TTL
    # Return configured max as approximation
    return getattr(settings, 'SMS_OTP_EXPIRY_SECONDS', 300)