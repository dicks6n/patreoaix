# store/transfers.py
"""
Send money from one PATREOAIX wallet to another (instant, free).

Safety:
  • the recipient is shown (masked name, username, verified badge) before the sender confirms;
  • the sender's password is required;
  • both wallets are locked in a fixed order inside one transaction, so two quick clicks can never spend twice,
    and an idempotency key makes a repeated submit return the first transfer instead of sending again;
  • a locked deposit bonus cannot be sent (only the withdrawable part of the wallet);
  • limits: TRANSFER_MAX per transfer, TRANSFER_DAILY_LIMIT per day; without approved KYC at most
    TRANSFER_NO_KYC_DAILY per day.
Wallet history: TRANSFER_OUT / TRANSFER_IN transactions (they don't move balances by themselves).
"""
import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .ledger import retry_busy as _retry_busy

logger = logging.getLogger(__name__)
D = Decimal
MIN_AMOUNT = D('1')


class TransferError(Exception):
    """Shown to the customer as-is."""


def _limit(name, default):
    try:
        return D(str(getattr(settings, name, default) or default))
    except InvalidOperation:
        return D(str(default))


def limits():
    return {'max': _limit('TRANSFER_MAX', 5000), 'daily': _limit('TRANSFER_DAILY_LIMIT', 10000),
            'no_kyc_daily': _limit('TRANSFER_NO_KYC_DAILY', 100)}


def kyc_ok(user):
    from .models import KYCProfile
    if user.is_staff:
        return True
    k = KYCProfile.objects.filter(user=user).first()
    return bool(k and str(k.status).upper() == 'APPROVED')


def find_user(query):
    """Username (with or without @), email, trader ID (PXT-123456) or phone number → User or None."""
    from .models import PropTrader, UserProfile
    q = (query or '').strip()
    if not q:
        return None
    if re.fullmatch(r'(?i)pxt-?\d{6}', q):
        t = PropTrader.objects.filter(code__iexact='PXT-' + q[-6:]).select_related('user').first()
        return t.user if t else None
    if '@' in q and not q.startswith('@'):
        return User.objects.filter(email__iexact=q, is_active=True).first()
    digits = re.sub(r'\D', '', q)
    if len(digits) >= 9 and not re.search(r'[a-zA-Z]', q):
        p = UserProfile.objects.filter(phone_number__endswith=digits[-9:]).select_related('user').first()
        if p and p.user.is_active:
            return p.user
    return User.objects.filter(username__iexact=q.lstrip('@'), is_active=True).first()


def masked_name(user):
    full = (user.get_full_name() or '').strip()
    if not full:
        return user.username
    parts = full.split()
    return parts[0] + (' ' + parts[-1][0] + '.' if len(parts) > 1 else '')


def preview(sender, query):
    u = find_user(query)
    if not u:
        raise TransferError('No PATREOAIX user found. Check the username, email, trader ID or phone number.')
    if u.id == sender.id:
        raise TransferError('That is your own account.')
    return {'username': u.username, 'name': masked_name(u), 'verified': kyc_ok(u), 'initial': u.username[:1].upper()}


def _wallet(user):
    from .models import Account
    acc, _ = Account.objects.get_or_create(user=user, account_type='EXCHANGE', defaults={'balance': D('0')})
    return acc


def _bonus_locked(user):
    from . import bonus
    try:
        return D(bonus.locked_amount(user))
    except Exception:
        return D('0')


def available(user):
    """What the user can send or withdraw: the wallet minus a still-locked deposit bonus."""
    return max(D('0'), D(_wallet(user).balance) - _bonus_locked(user))


def sent_today(user):
    from .models import WalletTransfer
    since = timezone.now() - timedelta(hours=24)
    return WalletTransfer.objects.filter(sender=user, created_at__gte=since).aggregate(s=Sum('amount'))['s'] or D('0')


@_retry_busy
def send(sender, query, amount, password, note='', idem_key=''):
    """Move `amount` USD from sender's wallet to the recipient's. Returns the WalletTransfer."""
    from .models import Account, Notification, Transaction, WalletTransfer
    idem_key = (idem_key or secrets.token_hex(16))[:40]
    done = WalletTransfer.objects.filter(sender=sender, idem_key=idem_key).first()
    if done:
        return done                                       # the same form submitted twice
    if not password or not sender.check_password(password):
        raise TransferError('Wrong password.')
    recipient = find_user(query)
    if not recipient:
        raise TransferError('No PATREOAIX user found with those details.')
    if recipient.id == sender.id:
        raise TransferError('You cannot send money to yourself.')
    try:
        amount = D(str(amount).replace(',', '').strip())
    except InvalidOperation:
        raise TransferError('Enter a valid amount.')
    if not amount.is_finite():
        raise TransferError('Enter a valid amount.')
    if amount != amount.quantize(D('0.01'), rounding=ROUND_DOWN):
        raise TransferError('Use at most 2 decimal places.')
    lim = limits()
    if amount < MIN_AMOUNT:
        raise TransferError(f'The minimum transfer is ${MIN_AMOUNT}.')
    if amount > lim['max']:
        raise TransferError(f'The maximum per transfer is ${lim["max"]:,.0f}.')
    today = sent_today(sender)
    daily = lim['daily'] if kyc_ok(sender) else lim['no_kyc_daily']
    if today + amount > daily:
        left = max(D('0'), daily - today)
        extra = '' if kyc_ok(sender) else ' Verify your identity to raise it.'
        raise TransferError(f'Daily limit ${daily:,.0f} (you can still send ${left:,.2f} today).{extra}')
    note = (note or '').strip()[:140]
    try:
        with transaction.atomic():
            from . import ledger
            src, dst = _wallet(sender), _wallet(recipient)
            bonus_locked = _bonus_locked(sender)
            if not ledger.debit(Account, src.pk, amount, keep=bonus_locked):      # atomic: no double spend
                src.refresh_from_db()
                free = max(D('0'), D(src.balance) - bonus_locked)
                raise TransferError(f'Not enough balance: you can send up to ${free:,.2f}'
                                    + (' (your deposit bonus stays locked until its trading volume is reached)' if bonus_locked else '') + '.')
            ledger.credit(Account, dst.pk, amount)
            t = WalletTransfer.objects.create(ref='TRF' + secrets.token_hex(4).upper(), sender=sender, recipient=recipient,
                                              amount=amount, note=note, idem_key=idem_key)
            Transaction.objects.create(user=sender, amount=amount, transaction_type='TRANSFER_OUT',
                                       details=f'Sent to @{recipient.username} · {t.ref}' + (f' · {note}' if note else ''))
            Transaction.objects.create(user=recipient, amount=amount, transaction_type='TRANSFER_IN',
                                       details=f'Received from @{sender.username} · {t.ref}' + (f' · {note}' if note else ''))
    except IntegrityError:                                # same idem_key raced: return the one that won
        return WalletTransfer.objects.get(sender=sender, idem_key=idem_key)
    for user, title, msg in ((sender, f'💸 ${amount:,.2f} sent to @{recipient.username}', f'Transfer {t.ref}.' + (f' Note: {note}' if note else '')),
                             (recipient, f'💰 You received ${amount:,.2f} from @{sender.username}', f'Transfer {t.ref} is in your wallet.' + (f' Note: {note}' if note else ''))):
        try:
            Notification.objects.create(user=user, title=title, message=msg, category='ACCOUNT', priority='success',
                                        action_url='/wallet/', action_label='Open wallet')
        except Exception:
            pass
    return t


def history(user, n=20):
    from .models import WalletTransfer
    rows = list(WalletTransfer.objects.filter(Q(sender=user) | Q(recipient=user)).select_related('sender', 'recipient')[:n])
    for r in rows:
        r.out = r.sender_id == user.id
        r.other = r.recipient if r.out else r.sender
    return rows
