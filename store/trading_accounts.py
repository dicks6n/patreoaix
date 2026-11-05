# store/trading_accounts.py
"""
Trading accounts, like Exness / Deriv: besides the main PATREOAIX account, a customer opens

  REAL  accounts  their own money, moved in from the wallet and back (Transfer); KYC needed to trade
  DEMO  accounts  virtual money ($100 – $100,000) to practise; the balance can be reset any time

Each has its own number (login), leverage chosen when it is opened (changeable later), balance, positions and
history. Under the hood they are PropAccount rows with kind 'real' / 'demo' (kind 'challenge' = funded challenges),
so the MT5 terminal trades them through the same engine (Settings → Accounts, or ?book=<login>).
"""
import random
from decimal import Decimal as D

from django.db import transaction
from django.utils import timezone

from . import mt5_service as m

DEMO_BALANCES = [100, 500, 1000, 5000, 10000, 50000, 100000]
MAX_ACCOUNTS = {'real': 5, 'demo': 5}
LOGIN_LEAD = {'real': 3, 'demo': 8}            # account numbers: real 3xxxxxxx, demo 8xxxxxxx (challenges 1 / 2 / 5)


class AccountError(Exception):
    """Shown to the customer as-is."""


def _login(kind):
    from .models import PropAccount
    lead = LOGIN_LEAD[kind]
    for _ in range(50):
        n = lead * 10_000_000 + random.SystemRandom().randint(100_000, 9_999_999)
        if not PropAccount.objects.filter(login=n).exists():
            return n
    raise AccountError('Could not create an account number, please try again.')


def wallet(user):
    from .models import Account
    return Account.objects.get_or_create(user=user, account_type='EXCHANGE', defaults={'balance': D('0')})[0]


def accounts(user, archived=False):
    from .models import PropAccount
    qs = PropAccount.objects.filter(user=user, kind__in=('real', 'demo'))
    qs = qs.filter(status='CLOSED') if archived else qs.filter(status='ACTIVE')
    return list(qs.order_by('kind', '-created_at'))


def get(user, login, kind=None):
    from .models import PropAccount
    qs = PropAccount.objects.filter(user=user, login=login, kind__in=(kind,) if kind else ('real', 'demo'))
    a = qs.first()
    if not a:
        raise AccountError('Account not found.')
    return a


def create(user, kind, leverage, balance=None, nickname=''):
    from .models import PropAccount
    if kind not in ('real', 'demo'):
        raise AccountError('Choose Real or Demo.')
    try:
        leverage = int(leverage)
    except (TypeError, ValueError):
        leverage = 0
    if leverage not in m.LEVERAGE_CHOICES:
        raise AccountError('Choose a leverage.')
    if PropAccount.objects.filter(user=user, kind=kind, status='ACTIVE').count() >= MAX_ACCOUNTS[kind]:
        raise AccountError(f'You can have {MAX_ACCOUNTS[kind]} {kind} accounts. Archive one first.')
    if kind == 'demo':
        try:
            bal = D(str(int(balance)))
        except (TypeError, ValueError):
            bal = D('10000')
        if int(bal) not in DEMO_BALANCES:
            bal = D('10000')
    else:
        bal = D('0')
    n = PropAccount.objects.filter(user=user, kind=kind).count() + 1
    return PropAccount.objects.create(
        user=user, kind=kind, login=_login(kind), plan_type='standard', size=int(bal), phase='funded', status='ACTIVE',
        initial_balance=bal, balance=bal, day_start_balance=bal, day_start_date=timezone.localdate(), lowest_equity=bal,
        leverage=leverage, nickname=(nickname or f"{'Real' if kind == 'real' else 'Demo'} {n}")[:40])


def summary(a):
    """Balance, equity, margin and open positions of one account (for the cards)."""
    from .models import Trade
    met = m.get_metrics(a.user, a)
    return {'acct': a, 'balance': met['balance'], 'equity': met['equity'], 'floating': met['floating_pnl'],
            'free': met['free_margin'], 'level': met['margin_level'],
            'open': Trade.objects.filter(user=a.user, prop=a, status='OPEN').count()}


def set_leverage(user, login, leverage):
    a = get(user, login)
    try:
        leverage = int(leverage)
    except (TypeError, ValueError):
        leverage = 0
    if leverage not in m.LEVERAGE_CHOICES:
        raise AccountError('Choose a leverage.')
    old = a.leverage
    a.leverage = leverage
    a.save(update_fields=['leverage', 'updated_at'])
    if leverage < old:
        met = m.get_metrics(user, a)
        if met['margin_level'] is not None and met['margin_level'] < m.MARGIN_CALL_LEVEL * 2:
            a.leverage = old
            a.save(update_fields=['leverage', 'updated_at'])
            raise AccountError(f'1:{leverage} would put the open trades on #{a.login} near a margin call. Close some first.')
    return a


def set_demo_balance(user, login, balance):
    from .models import Trade
    a = get(user, login, 'demo')
    try:
        bal = int(balance)
    except (TypeError, ValueError):
        bal = 0
    if bal not in DEMO_BALANCES:
        raise AccountError('Choose one of the demo balances.')
    if Trade.objects.filter(user=user, prop=a, status='OPEN').exists():
        raise AccountError('Close the open trades on this demo account first.')
    a.balance = a.initial_balance = a.day_start_balance = a.lowest_equity = D(bal)
    a.size = bal
    a.save(update_fields=['balance', 'initial_balance', 'day_start_balance', 'lowest_equity', 'size', 'updated_at'])
    return a


def transfer(user, src, dst, amount):
    """Move money between the wallet ('wallet') and a real account (login), either way."""
    from .models import PropAccount, Transaction
    try:
        amount = D(str(amount)).quantize(D('0.01'))
    except Exception:
        raise AccountError('Enter an amount.')
    if amount <= 0:
        raise AccountError('Enter an amount above 0.')
    if src == dst:
        raise AccountError('Pick two different accounts.')
    if 'wallet' not in (src, dst):
        raise AccountError('Transfers go between your wallet and a real account.')
    login = int(dst if src == 'wallet' else src)
    with transaction.atomic():
        a = PropAccount.objects.select_for_update().filter(user=user, login=login, kind='real', status='ACTIVE').first()
        if not a:
            raise AccountError('Real account not found.')
        w = type(wallet(user)).objects.select_for_update().get(pk=wallet(user).pk)
        if src == 'wallet':
            if w.balance < amount:
                raise AccountError(f'Your wallet has ${w.balance:,.2f}. Deposit first.')
            w.balance -= amount
            a.balance += amount
            a.initial_balance += amount
            txt = f'Wallet → real account #{a.login}'
        else:
            free = m.get_metrics(user, a)['free_margin']
            if amount > min(a.balance, free):
                raise AccountError(f'You can move up to ${max(D("0"), min(a.balance, free)):,.2f} (open trades keep their margin).')
            a.balance -= amount
            w.balance += amount
            txt = f'Real account #{a.login} → wallet'
        w.save(update_fields=['balance', 'updated_at'])
        a.save(update_fields=['balance', 'initial_balance', 'updated_at'])
        Transaction.objects.create(user=user, amount=amount, transaction_type='INTERNAL_TRANSFER', details=txt)
    return a


def archive(user, login):
    from .models import Trade
    a = get(user, login)
    if Trade.objects.filter(user=user, prop=a, status='OPEN').exists():
        raise AccountError('Close the open trades first.')
    if a.kind == 'real' and a.balance > 0:
        raise AccountError(f'Move the ${a.balance:,.2f} back to your wallet first.')
    a.status, a.ended_at = 'CLOSED', timezone.now()
    a.save(update_fields=['status', 'ended_at', 'updated_at'])
    return a


def restore(user, login):
    a = get(user, login)
    if a.status != 'CLOSED':
        return a
    from .models import PropAccount
    if PropAccount.objects.filter(user=user, kind=a.kind, status='ACTIVE').count() >= MAX_ACCOUNTS[a.kind]:
        raise AccountError(f'You already have {MAX_ACCOUNTS[a.kind]} active {a.kind} accounts.')
    a.status, a.ended_at = 'ACTIVE', None
    a.save(update_fields=['status', 'ended_at', 'updated_at'])
    return a
