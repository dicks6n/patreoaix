# store/prop_firm.py
"""
PATREOAIX Funded: prop-firm challenges (like FundingPips / FTMO).

  • Traders buy one or many challenge accounts (sizes $5K to $200K, 2-Step or 1-Step), paid from their wallet.
  • Each account trades in the MT5 terminal on its own balance (Trade.prop / PendingOrder.prop) and must reach
    the profit target without breaking the daily or the maximum loss, on a minimum number of trading days.
  • 2-Step: Phase 1 → Phase 2 → Funded.  1-Step: Phase 1 → Funded. Each phase gets a fresh login, like a real
    prop firm sends new credentials.
  • Funded accounts pay PROFIT_SPLIT% of the profit; payouts are approved by staff and credited to the wallet.

IDs: every trader gets a trader ID (PXT-482931); every account a numeric login like MT5
     (1xxxxxxx = Phase 1, 2xxxxxxx = Phase 2, 5xxxxxxx = Funded).

check(prop) runs on every engine pass for the trader (mt5_service.process_user_orders) and on the worker
(safe_check_all), so limits are enforced even when the trader is offline.
"""
import logging
import random
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

D = Decimal
SIZES = [5000, 10000, 25000, 50000, 100000, 200000]
TIER_NAMES = {5000: 'Starter', 10000: 'Standard', 25000: 'Advanced', 50000: 'Pro', 100000: 'Elite', 200000: 'Legend'}
PRICES = {
    'two_step': {5000: 39, 10000: 69, 25000: 149, 50000: 249, 100000: 449, 200000: 899},
    'one_step': {5000: 55, 10000: 95, 25000: 199, 50000: 329, 100000: 599, 200000: 1149},
}
RULES = {
    'two_step': {'name': '2-Step', 'targets': {'1': 8, '2': 5}, 'daily': 5, 'max': 10, 'min_days': 3, 'split': 80,
                 'blurb': 'Lower price per dollar of risk room: 8% then 5%, with 5% daily and 10% max loss.'},
    'one_step': {'name': '1-Step', 'targets': {'1': 10}, 'daily': 3, 'max': 6, 'min_days': 3, 'split': 90,
                 'blurb': 'One phase to funded: 10% target, 3% daily and 6% max loss, 90% split.'},
}
LEVERAGE = 100
MAX_ACTIVE_ACCOUNTS = 10
MAX_PER_ORDER = 5
MAX_ALLOCATION = 400000          # total capital across active accounts, like FundingPips' cap
BULK_DISCOUNT = {2: 5, 3: 10, 5: 15}      # % off when buying this many or more in one order
PAYOUT_MIN = D('50')
PAYOUT_EVERY_DAYS = 7
LOGIN_PREFIX = {'1': 1, '2': 2, 'funded': 5}


class PropError(Exception):
    """Shown to the trader as-is."""


# ═══════════════════════════════════════════════════════════════
# CATALOG
# ═══════════════════════════════════════════════════════════════
def discount_pct(qty):
    return max([p for n, p in BULK_DISCOUNT.items() if qty >= n] or [0])


def quote(plan_type, size, qty=1):
    if plan_type not in PRICES or size not in PRICES[plan_type]:
        raise PropError('Choose a plan and an account size.')
    unit = D(PRICES[plan_type][size])
    off = discount_pct(qty)
    total = (unit * qty * (100 - off) / 100).quantize(D('0.01'))
    return {'unit': unit, 'qty': qty, 'discount': off, 'total': total}


def catalog():
    out = []
    for t, r in RULES.items():
        rows = []
        for size in SIZES:
            rows.append({'size': size, 'tier': TIER_NAMES[size], 'price': PRICES[t][size],
                         'targets': [round(size * pct / 100) for pct in r['targets'].values()],
                         'daily': round(size * r['daily'] / 100), 'max': round(size * r['max'] / 100)})
        out.append({'key': t, **r, 'phases': len(r['targets']), 'rows': rows,
                    'target_text': ' → '.join(f'{p}%' for p in r['targets'].values())})
    return out


# ═══════════════════════════════════════════════════════════════
# IDS
# ═══════════════════════════════════════════════════════════════
def trader_id(user):
    """PXT-482931: six digits, never starting with 0, unique."""
    from .models import PropTrader
    t = PropTrader.objects.filter(user=user).first()
    if t:
        return t.code
    for _ in range(50):
        code = f'PXT-{random.SystemRandom().randint(100000, 999999)}'
        if not PropTrader.objects.filter(code=code).exists():
            try:
                return PropTrader.objects.create(user=user, code=code).code
            except Exception:
                t = PropTrader.objects.filter(user=user).first()
                if t:
                    return t.code
    raise PropError('Could not create a trader ID, please try again.')


def _new_login(phase):
    from .models import PropAccount
    lead = LOGIN_PREFIX[phase]
    for _ in range(50):
        login = lead * 10_000_000 + random.SystemRandom().randint(0, 9_999_999)
        if login >= lead * 10_000_000 + 100_000 and not PropAccount.objects.filter(login=login).exists():
            return login
    raise PropError('Could not create an account number, please try again.')


# ═══════════════════════════════════════════════════════════════
# BUYING
# ═══════════════════════════════════════════════════════════════
def _new_account(user, plan_type, size, phase, price=D('0'), ref='', previous=None):
    from .models import PropAccount
    bal = D(size)
    return PropAccount.objects.create(
        user=user, login=_new_login(phase), plan_type=plan_type, size=size, phase=phase, status='ACTIVE',
        initial_balance=bal, balance=bal, day_start_balance=bal, day_start_date=_today(), lowest_equity=bal,
        price_paid=price, order_ref=ref, previous=previous)


def buy(user, plan_type, size, qty=1):
    """Pay from the wallet (EXCHANGE balance) and open `qty` Phase 1 accounts. Returns the new accounts."""
    from .models import Account, PropAccount, Transaction, Notification
    try:
        size, qty = int(size), int(qty)
    except (TypeError, ValueError):
        raise PropError('Choose an account size and a quantity.')
    if not 1 <= qty <= MAX_PER_ORDER:
        raise PropError(f'You can buy 1 to {MAX_PER_ORDER} accounts per order.')
    q = quote(plan_type, size, qty)
    active = PropAccount.objects.filter(user=user, status='ACTIVE')
    if active.count() + qty > MAX_ACTIVE_ACCOUNTS:
        raise PropError(f'You can run up to {MAX_ACTIVE_ACCOUNTS} accounts at once (you have {active.count()}).')
    allocated = sum(a.size for a in active)
    if allocated + size * qty > MAX_ALLOCATION:
        raise PropError(f'The maximum total allocation is ${MAX_ALLOCATION:,}. You have ${allocated:,} in active accounts.')
    trader_id(user)
    ref = 'PXO-' + secrets.token_hex(3).upper()
    with transaction.atomic():
        wallet, _ = Account.objects.get_or_create(user=user, account_type='EXCHANGE', defaults={'balance': D('0')})
        wallet = Account.objects.select_for_update().get(pk=wallet.pk)
        if wallet.balance < q['total']:
            raise PropError(f"Not enough balance: the order is ${q['total']} and your wallet has ${wallet.balance}. Deposit first.")
        wallet.balance -= q['total']
        wallet.save(update_fields=['balance', 'updated_at'])
        Transaction.objects.create(user=user, amount=q['total'], transaction_type='CHALLENGE_FEE',
                                   details=f"{qty}× {RULES[plan_type]['name']} ${size:,} challenge · order {ref}"
                                           + (f" · {q['discount']}% bundle discount" if q['discount'] else ''))
        unit_paid = (q['total'] / qty).quantize(D('0.01'))
        accounts = [_new_account(user, plan_type, size, '1', unit_paid, ref) for _ in range(qty)]
    logins = ', '.join(f'#{a.login}' for a in accounts)
    try:
        Notification.objects.create(
            user=user, title=f'🏁 {qty} challenge account{"s" if qty > 1 else ""} ready',
            message=f"{RULES[plan_type]['name']} ${size:,}: {logins}. Open the terminal, pick the account in Settings → Accounts and start trading.",
            category='ACCOUNT', priority='success', action_url='/funded/', action_label='My accounts')
    except Exception:
        pass
    return accounts


# ═══════════════════════════════════════════════════════════════
# OBJECTIVES & RULES
# ═══════════════════════════════════════════════════════════════
def _today():
    return timezone.now().date()            # the trading day resets at 00:00 UTC


def rules_for(prop):
    r = RULES[prop.plan_type]
    target = r['targets'].get(prop.phase)
    return {'target_pct': target, 'daily_pct': r['daily'], 'max_pct': r['max'], 'min_days': r['min_days'],
            'split': r['split'], 'name': r['name']}


def trading_days(prop):
    from .models import Trade
    days = {timezone.localtime(d, timezone.utc).date() for d in
            Trade.objects.filter(prop=prop, parent__isnull=True).values_list('date_opened', flat=True)}
    return len(days)


def objectives(prop, metrics=None):
    """Progress of every rule, for the dashboard and the checks."""
    from . import mt5_service as mt5
    r = rules_for(prop)
    m = metrics or mt5.get_metrics(prop.user, prop)
    init = D(prop.initial_balance)
    equity, balance = D(m['equity']), D(m['balance'])
    day_start = D(prop.day_start_balance if prop.day_start_date == _today() else max(prop.balance, prop.day_start_balance))
    daily_floor = day_start - init * r['daily_pct'] / 100
    max_floor = init * (100 - r['max_pct']) / 100
    profit = balance - init
    out = {
        'equity': equity, 'balance': balance, 'profit': profit, 'profit_pct': round(profit / init * 100, 2),
        'daily_floor': daily_floor.quantize(D('0.01')), 'daily_left': (equity - daily_floor).quantize(D('0.01')),
        'daily_limit': (init * r['daily_pct'] / 100).quantize(D('0.01')),
        'max_floor': max_floor.quantize(D('0.01')), 'max_left': (equity - max_floor).quantize(D('0.01')),
        'max_limit': (init * r['max_pct'] / 100).quantize(D('0.01')),
        'days': trading_days(prop), 'open_positions': m['position_count'], **r,
    }
    out['daily_used_pct'] = max(0, min(100, round(float((day_start - equity) / (init * r['daily_pct'] / 100) * 100), 1)))
    out['max_used_pct'] = max(0, min(100, round(float((init - equity) / (init * r['max_pct'] / 100) * 100), 1)))
    if r['target_pct']:
        target = init * r['target_pct'] / 100
        out['target'] = target.quantize(D('0.01'))
        out['target_progress'] = max(0, min(100, round(float(profit / target * 100), 1)))
        out['target_hit'] = profit >= target
    out['days_ok'] = out['days'] >= r['min_days']
    return out


def _close_everything(prop, reason):
    from . import mt5_service as mt5
    from .models import Trade, PendingOrder
    PendingOrder.objects.filter(prop=prop, status='PENDING').update(status='CANCELLED', reason=reason[:200])
    for t in Trade.objects.filter(prop=prop, status='OPEN').select_related('asset'):
        try:
            q = mt5.cached_quote(t.asset.symbol) or mt5.live_quote(t.asset.symbol)
            if q:
                mt5._close_by_server(t.id, mt5.close_price_for(t.direction, q), 'BREACH' if 'loss' in reason else 'PASS')
        except Exception as e:
            logger.warning(f'prop close {prop.login} trade {t.id}: {e}')


def _notify(prop, title, message, ok=True):
    from .models import Notification
    try:
        Notification.objects.create(user=prop.user, title=title, message=message, category='ACCOUNT',
                                    priority='success' if ok else 'error', action_url='/funded/', action_label='My accounts')
    except Exception:
        pass


def fail(prop, reason):
    from .models import PropAccount
    with transaction.atomic():
        p = PropAccount.objects.select_for_update().get(pk=prop.pk)
        if p.status != 'ACTIVE':
            return p
        p.status, p.fail_reason, p.ended_at = 'FAILED', reason[:255], timezone.now()
        p.save(update_fields=['status', 'fail_reason', 'ended_at', 'updated_at'])
    _close_everything(p, reason)
    _notify(p, f'Account #{p.login} breached', f'{reason} The account is closed. You can start a new challenge any time.', ok=False)
    return p


def advance(prop):
    """Profit target + minimum days reached: next phase (or funded) on a fresh login."""
    from .models import PropAccount
    r = RULES[prop.plan_type]
    phases = list(r['targets'].keys())
    nxt = phases[phases.index(prop.phase) + 1] if prop.phase in phases and phases.index(prop.phase) + 1 < len(phases) else 'funded'
    with transaction.atomic():
        p = PropAccount.objects.select_for_update().get(pk=prop.pk)
        if p.status != 'ACTIVE' or p.phase == 'funded':
            return None
        p.status, p.ended_at = 'PASSED', timezone.now()
        p.save(update_fields=['status', 'ended_at', 'updated_at'])
        new = _new_account(p.user, p.plan_type, p.size, nxt, D('0'), p.order_ref, previous=p)
    _close_everything(p, 'Phase passed')
    if nxt == 'funded':
        _notify(p, f'🏆 You are funded! ${p.size:,} account #{new.login}',
                f'Congratulations, you passed the {r["name"]} challenge. Your funded account #{new.login} is ready; '
                f'you keep {r["split"]}% of the profit. Complete identity verification before your first payout.')
    else:
        _notify(p, f'✅ Phase {p.phase} passed: Phase {nxt} account #{new.login}',
                f'Well done! Your Phase {nxt} account #{new.login} (${p.size:,}) is ready with the same rules and a {r["targets"][nxt]}% target.')
    return new


def check(prop):
    """Daily reset, loss limits and the profit target for one active account."""
    from . import mt5_service as mt5
    from .models import PropAccount
    if prop.status != 'ACTIVE':
        return prop.status
    today = _today()
    if prop.day_start_date != today:                     # new trading day: the daily loss counts from here
        m = mt5.get_metrics(prop.user, prop)
        start = max(D(prop.balance), D(m['equity']))
        PropAccount.objects.filter(pk=prop.pk).update(day_start_balance=start, day_start_date=today)
        prop.day_start_balance, prop.day_start_date = start, today
    o = objectives(prop)
    if prop.lowest_equity is None or o['equity'] < prop.lowest_equity:
        PropAccount.objects.filter(pk=prop.pk).update(lowest_equity=o['equity'])
    if o['equity'] <= o['max_floor']:
        fail(prop, f"Maximum loss reached: equity {o['equity']:,.2f} fell to the {o['max_pct']}% limit ({o['max_floor']:,.2f}).")
        return 'FAILED'
    if o['equity'] <= o['daily_floor']:
        fail(prop, f"Daily loss limit reached: equity {o['equity']:,.2f} fell below today's floor {o['daily_floor']:,.2f} ({o['daily_pct']}%).")
        return 'FAILED'
    if prop.phase != 'funded' and o.get('target_hit') and o['days_ok'] and not o['open_positions']:
        advance(prop)
        return 'PASSED'
    return 'ACTIVE'


def check_all():
    from .models import PropAccount
    n = 0
    for p in PropAccount.objects.filter(status='ACTIVE').select_related('user'):
        try:
            if check(p) != 'ACTIVE':
                n += 1
        except Exception as e:
            logger.warning(f'prop check {p.login}: {e}')
    return n


def safe_check_all():
    try:
        return check_all()
    except Exception as e:
        logger.warning(f'prop checks: {e}')
        return 0


# ═══════════════════════════════════════════════════════════════
# PAYOUTS (funded accounts)
# ═══════════════════════════════════════════════════════════════
def payout_status(prop):
    from .models import PropPayout
    if prop.phase != 'funded' or prop.status != 'ACTIVE':
        return {'ok': False, 'why': 'Payouts are for active funded accounts.'}
    profit = D(prop.balance) - D(prop.initial_balance)
    last = PropPayout.objects.filter(account=prop).exclude(status='REJECTED').first()
    since = last.created_at if last else prop.created_at
    wait = since + timedelta(days=PAYOUT_EVERY_DAYS) - timezone.now()
    r = rules_for(prop)
    share = (profit * r['split'] / 100).quantize(D('0.01')) if profit > 0 else D('0')
    out = {'profit': profit.quantize(D('0.01')), 'share': share, 'split': r['split'], 'ok': True, 'why': ''}
    if PropPayout.objects.filter(account=prop, status='PENDING').exists():
        out.update(ok=False, why='A payout request is already being processed.')
    elif wait.total_seconds() > 0:
        out.update(ok=False, why=f'Next payout available in {max(1, wait.days + (1 if wait.seconds else 0))} day(s).')
    elif profit < PAYOUT_MIN:
        out.update(ok=False, why=f'At least ${PAYOUT_MIN} of profit is needed for a payout.')
    elif prop.trades.filter(status='OPEN').exists():
        out.update(ok=False, why='Close all positions before requesting a payout.')
    return out


def request_payout(user, login):
    from .models import PropAccount, PropPayout, KYCProfile
    p = PropAccount.objects.filter(user=user, login=login).first()
    if not p:
        raise PropError('Account not found.')
    kyc = KYCProfile.objects.filter(user=user).first()
    if not (user.is_staff or (kyc and str(kyc.status).upper() == 'APPROVED')):
        raise PropError('Verify your identity (KYC) before your first payout.')
    st = payout_status(p)
    if not st['ok']:
        raise PropError(st['why'])
    with transaction.atomic():
        p = PropAccount.objects.select_for_update().get(pk=p.pk)
        profit = D(p.balance) - D(p.initial_balance)
        share = (profit * st['split'] / 100).quantize(D('0.01'))
        po = PropPayout.objects.create(account=p, user=user, profit=profit, split=st['split'], amount=share)
        p.balance = p.initial_balance                    # profit leaves the account; the balance resets
        p.day_start_balance = p.initial_balance
        p.save(update_fields=['balance', 'day_start_balance', 'updated_at'])
    _notify(p, f'💸 Payout requested: ${share}', f'{st["split"]}% of ${profit:.2f} profit on #{p.login}. You will be notified when it is paid to your wallet.')
    return po


def pay(payout):
    """Staff: credit an approved payout to the trader's wallet (admin action)."""
    from .models import PropPayout, Transaction
    with transaction.atomic():
        po = PropPayout.objects.select_for_update().get(pk=payout.pk)
        if po.status != 'PENDING':
            return po
        po.status, po.processed_at = 'PAID', timezone.now()
        po.save(update_fields=['status', 'processed_at'])
        Transaction.objects.create(user=po.user, amount=po.amount, transaction_type='ADJUSTMENT',
                                   details=f'Funded payout #{po.account.login} ({po.split}% of ${po.profit})')   # credits the wallet
    _notify(po.account, f'✅ Payout paid: ${po.amount}', f'Your funded payout from #{po.account.login} is in your wallet.')
    return po


def reject(payout, note=''):
    """Staff: reject a payout; the profit goes back onto the account."""
    from .models import PropPayout, PropAccount
    with transaction.atomic():
        po = PropPayout.objects.select_for_update().get(pk=payout.pk)
        if po.status != 'PENDING':
            return po
        po.status, po.processed_at, po.note = 'REJECTED', timezone.now(), note[:255]
        po.save(update_fields=['status', 'processed_at', 'note'])
        p = PropAccount.objects.select_for_update().get(pk=po.account_id)
        p.balance = D(p.balance) + po.profit
        p.save(update_fields=['balance', 'updated_at'])
    _notify(po.account, 'Payout not approved', note or 'Your payout request was not approved. The profit is back on the account.', ok=False)
    return po
