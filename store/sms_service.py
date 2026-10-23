"""
PATREOAIX SMS Service — Trial-Compatible
==========================================

Centralized SMS gateway with:
  • Twilio Trial support (uses predefined templates)
  • Automatic upgrade detection (uses custom messages)
  • Rate limiting per user/template
  • Opt-in checks
  • Feature flags (per category)
  • Phone normalization to E.164

When Twilio account is upgraded → set TWILIO_UPGRADED=True in settings.
Trial mode is default.
"""

import logging
import re
from decimal import Decimal
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


# ============================================================
# LAZY TWILIO IMPORT (so app runs even if package is missing)
# ============================================================
try:
    from twilio.rest import Client as TwilioClient
    from twilio.base.exceptions import TwilioRestException
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    TwilioClient = None
    TwilioRestException = Exception


# ============================================================
# TWILIO TRIAL PREDEFINED TEMPLATES
# ============================================================
# Trial accounts can ONLY send these exact body strings.
# Custom text will fail with error 572006.
# Reference: https://www.twilio.com/docs/errors/572006

TRIAL_TEMPLATES = {
    'sms_2fa',                      # Two-factor authentication
    'sms_appointment_reminders',    # Appointment reminders
    'sms_order_confirmation',       # Order confirmations
    'sms_delivery_updates',         # Delivery updates
    'sms_customer_support',         # Customer support
    'sms_marketing_promotions',     # Marketing/promotions
    'sms_event_notifications',      # Event notifications
    'sms_account_alerts',           # Account alerts
    'sms_feedback_surveys',         # Feedback surveys
    'sms_internal_alerts',          # Internal alerts
}

# Map our internal template names → Twilio trial templates
TRIAL_TEMPLATE_MAP = {
    'otp':          'sms_2fa',
    'deposit':      'sms_account_alerts',
    'withdrawal':   'sms_account_alerts',
    'trade':        'sms_internal_alerts',
    'security':     'sms_account_alerts',
    'referral':     'sms_marketing_promotions',
    'general':      'sms_account_alerts',
    'kyc':          'sms_account_alerts',
    'payout':       'sms_account_alerts',
}


class SMSService:
    """
    Centralized SMS gateway.

    Trial mode:   send(body='sms_2fa')     ← predefined template name
    Upgraded:     send(body='Your code: 123456')  ← custom text

    Usage:
        from store.sms_service import sms_service
        sms_service.send_otp(user, otp)
        sms_service.send_deposit_approved(user, amount, new_balance)
        sms_service.send_withdrawal_submitted(user, amount, method)
        sms_service.send_trade_closed(user, symbol, direction, pnl)
    """

    def __init__(self):
        self.client = None

        if not getattr(settings, 'SMS_ENABLED', False):
            logger.info("SMS disabled via settings.SMS_ENABLED")
            return

        if not TWILIO_AVAILABLE:
            logger.warning("Twilio package not installed — run: pip install twilio")
            return

        sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
        token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
        from_number = getattr(settings, 'TWILIO_PHONE_NUMBER', '')

        if not (sid and token and from_number):
            logger.warning(
                "Twilio credentials incomplete — set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER in settings/env"
            )
            return

        try:
            self.client = TwilioClient(sid, token)
            mode = "UPGRADED" if self.is_upgraded else "TRIAL"
            logger.info(f"Twilio client initialized ({mode} mode)")
        except Exception as e:
            logger.error(f"Twilio init failed: {e}")

    # ============================================================
    # ACCOUNT MODE DETECTION
    # ============================================================

    @property
    def is_upgraded(self) -> bool:
        """
        Returns True if Twilio account is upgraded (custom SMS allowed).
        Set TWILIO_UPGRADED=True in settings after upgrading your account.
        """
        return bool(getattr(settings, 'TWILIO_UPGRADED', False))

    def _resolve_body(self, template: str, custom_message: str) -> str:
        """
        Return the body to send to Twilio.
        - Upgraded mode → custom_message
        - Trial mode → predefined template name
        """
        if self.is_upgraded:
            return custom_message

        # Trial mode — map to allowed template
        trial_name = TRIAL_TEMPLATE_MAP.get(template, 'sms_account_alerts')

        # Extra safety: if someone passes a raw template name directly
        if trial_name not in TRIAL_TEMPLATES:
            trial_name = 'sms_account_alerts'

        return trial_name

    # ============================================================
    # UTILS
    # ============================================================

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Normalize any phone format to E.164 (+254712345678).
        Handles: 0712345678, +254712345678, 254712345678, 00254712345678
        """
        if not phone:
            return ''

        p = re.sub(r'[^\d+]', '', str(phone).strip())

        # Remove leading '00'
        if p.startswith('00'):
            p = '+' + p[2:]

        # If starts with 0 (local Kenya format), swap to +254
        if p.startswith('0'):
            p = '+254' + p[1:]

        # If no '+' but starts with country code, add '+'
        if not p.startswith('+'):
            p = '+' + p

        return p

    def _rate_limit_ok(self, user, template: str) -> bool:
        """Prevent SMS spam — max N SMS per user per hour."""
        if not user or not getattr(user, 'is_authenticated', False):
            return True

        limit = getattr(settings, 'SMS_RATE_LIMIT_PER_HOUR', 10)
        hour_key = timezone.now().strftime('%Y%m%d%H')
        key = f"sms_rl_{user.id}_{template}_{hour_key}"

        try:
            count = cache.get(key, 0)
            if count >= limit:
                logger.warning(f"SMS rate limit hit for {user.username} [{template}]")
                return False
            cache.set(key, count + 1, timeout=3600)
        except Exception:
            # If cache is down, don't block SMS
            pass

        return True

    def _feature_enabled(self, template: str) -> bool:
        """Check if this SMS category is enabled via settings."""
        if not getattr(settings, 'SMS_ENABLED', False):
            return False

        flags = {
            'otp':        getattr(settings, 'SMS_OTP_ENABLED', True),
            'deposit':    getattr(settings, 'SMS_DEPOSIT_ENABLED', True),
            'withdrawal': getattr(settings, 'SMS_WITHDRAWAL_ENABLED', True),
            'trade':      getattr(settings, 'SMS_TRADE_ENABLED', True),
            'security':   getattr(settings, 'SMS_SECURITY_ENABLED', True),
            'referral':   getattr(settings, 'SMS_REFERRAL_ENABLED', True),
        }
        return flags.get(template, True)

    def _can_send_to_user(self, user) -> bool:
        """Check user has phone + SMS opt-in enabled."""
        if not user:
            return False

        profile = getattr(user, 'userprofile', None)
        if not profile:
            return False

        if not getattr(profile, 'phone_number', None):
            return False

        if not getattr(profile, 'sms_opt_in', True):
            return False

        return True

    # ============================================================
    # CORE SEND METHOD
    # ============================================================

    def send(
        self,
        to_phone: str,
        message: str,
        template: str = 'general',
        user=None,
        raise_errors: bool = False,
    ) -> dict:
        """
        Low-level send.

        Args:
            to_phone: Phone number (any format)
            message:  Custom text (used only if upgraded)
            template: Internal category ('otp', 'deposit', etc.)
            user:     Optional User for rate-limit tracking

        Returns:
            {'success': bool, 'sid': str, 'error': str, 'mode': 'trial'|'upgraded'}
        """
        result = {'success': False, 'sid': None, 'error': None,
                  'mode': 'trial' if not self.is_upgraded else 'upgraded'}

        # ---- Guards ----
        if not getattr(settings, 'SMS_ENABLED', False):
            result['error'] = 'SMS disabled'
            return result

        if not self.client:
            result['error'] = 'SMS client not initialized'
            logger.warning("SMS not sent — Twilio client unavailable")
            return result

        if not self._feature_enabled(template):
            result['error'] = f'Feature "{template}" disabled'
            return result

        if user and not self._rate_limit_ok(user, template):
            result['error'] = 'Rate limit exceeded'
            return result

        phone = self.normalize_phone(to_phone)
        if not phone:
            result['error'] = 'Invalid phone number'
            return result

        # ---- Resolve body based on trial/upgraded ----
        body_to_send = self._resolve_body(template, message)

        # ---- Send via Twilio ----
        try:
            msg = self.client.messages.create(
                body=body_to_send,
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone,
            )
            result['success'] = True
            result['sid'] = msg.sid
            logger.info(
                f"SMS sent → {phone} [{template}] [{result['mode']}] "
                f"body='{body_to_send[:40]}' SID: {msg.sid}"
            )
        except TwilioRestException as e:
            result['error'] = str(e)
            logger.error(f"SMS failed → {phone} [{template}]: {e}")
            if raise_errors:
                raise
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"SMS unexpected error → {phone} [{template}]: {e}")
            if raise_errors:
                raise

        return result

    def send_to_user(self, user, message: str, template: str = 'general') -> dict:
        """Send SMS to a User object, honoring opt-in + phone from UserProfile."""
        if not self._can_send_to_user(user):
            return {
                'success': False,
                'sid': None,
                'error': 'User has no phone or SMS disabled',
                'mode': 'trial' if not self.is_upgraded else 'upgraded',
            }

        profile = user.userprofile
        return self.send(profile.phone_number, message, template=template, user=user)

    # ============================================================
    # HIGH-LEVEL TEMPLATES
    # ============================================================

    # ---- OTP ----
    def send_otp(self, user, otp: str) -> dict:
        """Send a 6-digit OTP for login or 2FA."""
        msg = (
            f"PATREOAIX verification code: {otp}\n"
            f"Valid for {getattr(settings, 'SMS_OTP_EXPIRY_SECONDS', 300) // 60} minutes.\n"
            f"Never share this code with anyone."
        )
        return self.send_to_user(user, msg, template='otp')

    def send_otp_to_phone(self, phone: str, otp: str) -> dict:
        """Send OTP to a raw phone number (during signup)."""
        msg = (
            f"PATREOAIX verification code: {otp}\n"
            f"Valid for {getattr(settings, 'SMS_OTP_EXPIRY_SECONDS', 300) // 60} minutes."
        )
        return self.send(phone, msg, template='otp')

    # ---- Deposits ----
    def send_deposit_submitted(self, user, amount) -> dict:
        msg = (
            f"PATREOAIX: Your deposit of ${amount:.2f} has been submitted.\n"
            f"Status: Pending review\n"
            f"We'll notify you once approved."
        )
        return self.send_to_user(user, msg, template='deposit')

    def send_deposit_approved(self, user, amount, new_balance=None) -> dict:
        msg = f"PATREOAIX: Deposit of ${amount:.2f} approved!"
        if new_balance is not None:
            msg += f"\nNew Exchange balance: ${new_balance:.2f}"
        return self.send_to_user(user, msg, template='deposit')

    def send_deposit_rejected(self, user, amount, reason='Contact support') -> dict:
        msg = (
            f"PATREOAIX: Deposit of ${amount:.2f} was rejected.\n"
            f"Reason: {reason}\n"
            f"Contact support for help."
        )
        return self.send_to_user(user, msg, template='deposit')

    # ---- Withdrawals ----
    def send_withdrawal_submitted(self, user, amount, method='Crypto') -> dict:
        msg = (
            f"PATREOAIX: Withdrawal of ${amount:.2f} via {method} submitted.\n"
            f"Status: Pending admin approval\n"
            f"Processing time: up to 24 hours."
        )
        return self.send_to_user(user, msg, template='withdrawal')

    def send_withdrawal_approved(self, user, amount, method='Crypto') -> dict:
        msg = (
            f"PATREOAIX: Withdrawal of ${amount:.2f} via {method} approved!\n"
            f"Funds are on the way."
        )
        return self.send_to_user(user, msg, template='withdrawal')

    def send_withdrawal_rejected(self, user, amount, reason='Contact support') -> dict:
        msg = (
            f"PATREOAIX: Withdrawal of ${amount:.2f} was rejected.\n"
            f"Reason: {reason}\n"
            f"Your balance was not debited."
        )
        return self.send_to_user(user, msg, template='withdrawal')

    # ---- Trades ----
    def send_trade_opened(self, user, symbol, direction, volume, entry) -> dict:
        msg = (
            f"PATREOAIX: {direction} {symbol} opened\n"
            f"Volume: {volume} lots\n"
            f"Entry: {entry}"
        )
        return self.send_to_user(user, msg, template='trade')

    def send_trade_closed(self, user, symbol, direction, pnl, result='WON') -> dict:
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"PATREOAIX: {direction} {symbol} closed\n"
            f"Result: {result}\n"
            f"P&L: {sign}${pnl:.2f}"
        )
        return self.send_to_user(user, msg, template='trade')

    # ---- Security ----
    def send_password_changed(self, user) -> dict:
        msg = (
            f"PATREOAIX: Your password was changed successfully.\n"
            f"If this wasn't you, contact support immediately."
        )
        return self.send_to_user(user, msg, template='security')

    def send_new_login(self, user, ip, browser) -> dict:
        msg = (
            f"PATREOAIX: New login detected\n"
            f"IP: {ip}\n"
            f"Device: {browser}\n"
            f"If this wasn't you, secure your account."
        )
        return self.send_to_user(user, msg, template='security')

    def send_kyc_status(self, user, status, reason='') -> dict:
        if status == 'APPROVED':
            msg = "PATREOAIX: Your KYC has been approved. Full trading unlocked!"
        else:
            msg = f"PATREOAIX: KYC rejected. Reason: {reason or 'Documents unclear'}"
        return self.send_to_user(user, msg, template='security')

    # ---- Referrals ----
    def send_referral_reward(self, user, amount, from_username='') -> dict:
        msg = f"PATREOAIX: You earned ${amount:.2f} referral reward!"
        if from_username:
            msg += f"\nFrom: @{from_username}"
        msg += "\nWithdraw from your Referral dashboard."
        return self.send_to_user(user, msg, template='referral')


# ============================================================
# SINGLETON
# ============================================================
sms_service = SMSService()