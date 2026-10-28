# store/referral_rewards.py
"""
"Invite a friend: you both get 7 days of Pro."

  • The friend who signs up with a referral link gets REFERRAL_PRO_DAYS of Pro instead of the normal trial.
  • The inviter gets REFERRAL_PRO_DAYS of Pro once that friend becomes a real customer: their first completed
    deposit or an approved identity check (so empty fake accounts earn nothing), at most
    REFERRAL_MAX_REWARDS_PER_MONTH times a month. A paying member's plan is extended instead.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def days():
    try:
        return int(getattr(settings, 'REFERRAL_PRO_DAYS', 7) or 0)
    except (TypeError, ValueError):
        return 7


def monthly_cap():
    try:
        return int(getattr(settings, 'REFERRAL_MAX_REWARDS_PER_MONTH', 10) or 0)
    except (TypeError, ValueError):
        return 10


def on_friend_activated(friend, reason):
    """The referred user deposited or got verified: reward their inviter once."""
    from .models import Referral
    from .membership import grant_pro_days
    if days() <= 0:
        return None
    ref = Referral.objects.filter(referred_user=friend, pro_days_given=False).select_related('referrer').first()
    if not ref or ref.referrer_id == friend.id:
        return None
    month_ago = timezone.now() - timedelta(days=30)
    given = Referral.objects.filter(referrer=ref.referrer, pro_days_given=True, pro_days_at__gte=month_ago).count()
    if monthly_cap() and given >= monthly_cap():
        logger.info(f'Referral reward cap reached for {ref.referrer}')
        return None
    ref.pro_days_given, ref.pro_days_at = True, timezone.now()
    ref.save(update_fields=['pro_days_given', 'pro_days_at'])
    grant_pro_days(ref.referrer, days(), f'🎁 +{days()} days of Pro: {friend.username} joined',
                   f'Your friend {friend.username} {reason}. {days()} days of Pro were added to your account. Invite more friends to keep it going.')
    return ref


def safe(friend, reason):
    try:
        return on_friend_activated(friend, reason)
    except Exception as e:
        logger.warning(f'referral reward failed: {e}')
        return None
