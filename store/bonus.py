# store/bonus.py
"""
First-deposit bonus: the first completed deposit gets FIRST_DEPOSIT_BONUS_PCT % extra (default 100 %, at most
FIRST_DEPOSIT_BONUS_MAX USD) credited to the exchange balance as a BONUS transaction.

The bonus can be traded at once. It becomes withdrawable once the customer has traded BONUS_TURNOVER_X times the
bonus (stakes + margin of closed trades since the bonus); until then the bonus amount is held back from
withdrawals (the customer's own deposit and profits stay withdrawable). Turn it off with FIRST_DEPOSIT_BONUS_PCT=0.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


def _d(name, default):
    try:
        return Decimal(str(getattr(settings, name, default) or default))
    except Exception:
        return Decimal(str(default))


def pct():
    return _d('FIRST_DEPOSIT_BONUS_PCT', 100)


def cap():
    return _d('FIRST_DEPOSIT_BONUS_MAX', 500)


def turnover_x():
    return _d('BONUS_TURNOVER_X', 10)


def enabled():
    return pct() > 0


def on_deposit(tx):
    """Called for every new completed DEPOSIT transaction; grants the bonus on the first one."""
    from .models import DepositBonus, Notification, Transaction
    if not enabled() or DepositBonus.objects.filter(user=tx.user).exists():
        return None
    earlier = Transaction.objects.filter(user=tx.user, transaction_type='DEPOSIT', status='completed').exclude(pk=tx.pk)
    if earlier.exists():
        return None                                          # not the first deposit
    amount = min(Decimal(tx.amount) * pct() / 100, cap()).quantize(Decimal('0.01'))
    if amount <= 0:
        return None
    b = DepositBonus.objects.create(user=tx.user, deposit=tx.amount, amount=amount,
                                    required_turnover=(amount * turnover_x()).quantize(Decimal('0.01')))
    Transaction.objects.create(user=tx.user, amount=amount, transaction_type='BONUS',
                               details=f'{pct():.0f}% first-deposit bonus')      # credited by the Transaction signal
    try:
        Notification.objects.create(
            user=tx.user, title=f'🎁 ${amount} deposit bonus added',
            message=f'You received a {pct():.0f}% bonus on your first deposit. Trade it right away; it becomes '
                    f'withdrawable after ${b.required_turnover} of trading volume.',
            category='ACCOUNT', priority='success', action_url='/account-operations/funding/payment-methods/deposit',
            action_label='View')
    except Exception as e:
        logger.warning(f'bonus notification: {e}')
    return b


def turnover(user, since):
    """Trading volume since the bonus: stakes (digits, binaries) and margin (MT5-style trades) of closed trades."""
    from .models import Trade
    qs = Trade.objects.filter(user=user, date_closed__gte=since).exclude(status__in=('OPEN', 'PENDING', 'CANCELLED'))
    agg = qs.aggregate(stake=Sum('stake'), margin=Sum('margin_used'))
    return Decimal(agg['stake'] or 0) + Decimal(agg['margin'] or 0)


def status(user):
    """{'amount', 'locked', 'turnover', 'required', 'progress'} or None when the user has no bonus."""
    from .models import DepositBonus, Notification
    b = DepositBonus.objects.filter(user=user).first()
    if not b:
        return None
    done = turnover(user, b.created_at) if not b.released_at else b.required_turnover
    if not b.released_at and done >= b.required_turnover:
        b.released_at = timezone.now()
        b.save(update_fields=['released_at'])
        try:
            Notification.objects.create(user=user, title='🎁 Bonus unlocked', category='ACCOUNT', priority='success',
                                        message=f'Your ${b.amount} deposit bonus is now withdrawable.')
        except Exception:
            pass
    req = b.required_turnover or Decimal('1')
    return {'amount': b.amount, 'locked': not b.released_at, 'turnover': min(done, req), 'required': b.required_turnover,
            'progress': int(min(100, done / req * 100)), 'released_at': b.released_at}


def locked_amount(user):
    st = status(user)
    return st['amount'] if st and st['locked'] else Decimal('0')
