"""
store/notifications.py
======================

Helper for creating Notifications consistently.

Usage:
    from .notifications import notify

    notify(
        user,
        title="Deposit Approved ✅",
        message="Your $50.00 has been credited.",
        category='FUNDING',
        priority='success',
        action_url='/funding/',
        action_label='View Wallet',
    )
"""

import logging
from .models import Notification

logger = logging.getLogger(__name__)


# ============================================================
# CATEGORY CONSTANTS (matches Notification.CATEGORY_CHOICES)
# ============================================================
CATEGORY_TRADE = 'TRADE'
CATEGORY_FUNDING = 'FUNDING'
CATEGORY_REFERRAL = 'REFERRAL'
CATEGORY_ACCOUNT = 'ACCOUNT'
CATEGORY_KYC = 'KYC'
CATEGORY_COPY_TRADE = 'COPY_TRADE'
CATEGORY_PAYOUT = 'PAYOUT'
CATEGORY_SYSTEM = 'SYSTEM'
CATEGORY_SECURITY = 'SECURITY'


# ============================================================
# PRIORITY CONSTANTS
# ============================================================
PRIORITY_INFO = 'info'
PRIORITY_SUCCESS = 'success'
PRIORITY_WARNING = 'warning'
PRIORITY_ERROR = 'error'


def notify(
    user,
    title: str,
    message: str,
    category: str = 'SYSTEM',
    priority: str = 'info',
    action_url: str = None,
    action_label: str = None,
    metadata: dict = None,
    expires_at=None,
):
    """
    Create a notification for a user.

    Args:
        user: Django User instance
        title: Short title (max 200 chars)
        message: Full message body
        category: One of CATEGORY_* constants
        priority: 'info' | 'success' | 'warning' | 'error'
        action_url: Optional deep link, e.g. '/funding/'
        action_label: Optional CTA text, e.g. 'View Wallet'
        metadata: Optional dict for extra context
        expires_at: Optional datetime for auto-expiry

    Returns:
        Notification instance (or None if creation failed)
    """
    if not user:
        logger.warning("notify() called with no user")
        return None

    try:
        notification = Notification.objects.create(
            user=user,
            title=title[:200],          # ensure title fits max_length
            message=message,
            category=category,
            priority=priority,
            action_url=action_url,
            action_label=action_label,
            metadata=metadata,
            expires_at=expires_at,
        )
        return notification
    except Exception as e:
        # Never let a notification crash the calling view
        logger.error(f"notify() failed for user={getattr(user, 'username', '?')}: {e}")
        return None


def notify_many(users, **kwargs):
    """
    Send the same notification to multiple users.
    Returns list of created Notification instances.
    """
    return [notify(user, **kwargs) for user in users]


def notify_staff(**kwargs):
    """
    Send a notification to all active staff members.
    Useful for admin alerts (new deposit, withdrawal, KYC, etc.)
    """
    from django.contrib.auth.models import User

    staff = User.objects.filter(is_staff=True, is_active=True)
    return notify_many(staff, **kwargs)