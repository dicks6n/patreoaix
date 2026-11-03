# store/payouts.py
"""
Saved withdrawal accounts ("bind once, withdraw in one tap"), on UserProfile.payout_accounts:
    {'M-Pesa': {'destination': '0712345678', 'name': 'Amina W.', 'updated': '2026-11-04T10:00:00+03:00'}, 'Crypto': {...}}

The first withdrawal with a method binds its destination. Changing a saved destination needs the account password and
sends a security alert (email + push), so a stolen session cannot quietly redirect withdrawals.
"""
import re

from django.utils import timezone

METHODS = {                     # key: (label, what to enter, input mode, placeholder)
    'M-Pesa': ('M-Pesa', 'M-Pesa phone number', 'tel', '0712 345 678'),
    'Crypto': ('USDT (TRC20)', 'USDT TRC20 wallet address', 'text', 'T…'),
    'Bank': ('Bank transfer', 'Bank name, account number and account name', 'text', 'Equity Bank · 0123456789 · Amina W.'),
}


class PayoutError(Exception):
    pass


def clean(method, destination):
    d = (destination or '').strip()
    if method == 'M-Pesa':
        digits = re.sub(r'[\s\-+]', '', d)
        if re.fullmatch(r'0[17]\d{8}', digits):
            return digits
        if re.fullmatch(r'254[17]\d{8}', digits):
            return '0' + digits[3:]
        raise PayoutError('Enter a Kenyan M-Pesa number like 0712 345 678.')
    if method == 'Crypto':
        if re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', d):
            return d
        raise PayoutError('Enter a USDT TRC20 address: 34 characters starting with T.')
    if len(d) < 8:
        raise PayoutError('Enter the bank name, account number and account name.')
    return d[:200]


def accounts(user):
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).only('payout_accounts').first()
    return dict(p.payout_accounts or {}) if p and isinstance(p.payout_accounts, dict) else {}


def bound(user, method):
    return (accounts(user).get(method) or {}).get('destination', '')


def bind(user, method, destination, password=None):
    """Save (or change) the destination for a method. Returns the cleaned destination."""
    from .models import Notification, UserProfile
    if method not in METHODS:
        raise PayoutError('Choose a withdrawal method.')
    dest = clean(method, destination)
    current = bound(user, method)
    if current == dest:
        return dest
    if current and not (password and user.check_password(password)):
        raise PayoutError('Enter your account password to change a saved withdrawal account.')
    p, _ = UserProfile.objects.get_or_create(user=user)
    acc = dict(p.payout_accounts or {})
    acc[method] = {'destination': dest, 'updated': timezone.now().isoformat()}
    p.payout_accounts = acc
    p.save(update_fields=['payout_accounts'])
    if current:
        Notification.objects.create(
            user=user, category='SECURITY', priority='warning', title=f'Withdrawal account changed ({METHODS[method][0]})',
            message=f'Your {METHODS[method][0]} withdrawal account was changed to {mask(dest)}. '
                    f"If this wasn't you, change your password now and contact support.",
            action_url='/wallet/', action_label='Check wallet')
    return dest


def remove(user, method, password):
    from .models import UserProfile
    if not (password and user.check_password(password)):
        raise PayoutError('Enter your account password to remove a withdrawal account.')
    p, _ = UserProfile.objects.get_or_create(user=user)
    acc = dict(p.payout_accounts or {})
    acc.pop(method, None)
    p.payout_accounts = acc
    p.save(update_fields=['payout_accounts'])


def mask(dest):
    d = str(dest or '')
    return d if len(d) <= 8 else f'{d[:4]}…{d[-4:]}'


def for_template(user):
    acc = accounts(user)
    return [{'key': k, 'label': v[0], 'what': v[1], 'mode': v[2], 'ph': v[3],
             'dest': (acc.get(k) or {}).get('destination', ''), 'masked': mask((acc.get(k) or {}).get('destination', ''))}
            for k, v in METHODS.items()]
