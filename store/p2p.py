# store/p2p.py
"""
P2P marketplace: customers buy and sell USDT, BTC and ETH with each other for local money (M-Pesa, MTN MoMo, bank…).

The site never touches the local money. It only holds the USDT in escrow:
  • SELL ad (advertiser sells USDT): the USDT is locked from the advertiser's wallet when the ad is posted.
  • BUY ad (advertiser buys USDT): the USDT is locked from the seller's wallet when they open an order.
  Buyer pays outside the site → taps "I have paid" → seller checks and taps "Release" → USDT goes to the buyer
  (minus the P2P fee). Unpaid orders expire; either side can appeal and staff decide.

Real money only: USDT is the customer's wallet (EXCHANGE account); BTC and ETH are P2PBalance rows
(demo=False, 8 decimals). Trading requires approved KYC. (Orders and ads from the old test mode keep demo=True
and are hidden; migration 0067 closed them.)
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .ledger import retry_busy as _retry_busy
from .p2p_markets import CURRENCIES, METHODS, grouped as currency_groups   # 130+ currencies, local + global methods

logger = logging.getLogger(__name__)

D = Decimal

ASSETS = {                        # decimals, name
    'USDT': (2, 'Tether USD'),
    'BTC': (8, 'Bitcoin'),
    'ETH': (6, 'Ethereum'),
}
TIME_LIMITS = [15, 30, 45, 60]
MAX_OPEN_ORDERS = 3
CANCEL_LIMIT = 3                  # cancelled / unpaid orders in 24 h before buying is paused for 24 h
BUYER_CANCELS = ('Cancelled by the buyer', 'The payment time ran out')


class P2PError(Exception):
    """Shown to the customer as-is."""


def live():
    """P2P always trades real balances (the old demo mode was removed)."""
    return True

def fee_pct():
    try:
        return D(str(getattr(settings, 'P2P_FEE_PCT', '0.2') or '0'))
    except Exception:
        return D('0.2')


def method_name(key):
    return METHODS.get(key, (key, ''))[0]


def methods_for(currency):
    return [(k, METHODS[k][0], METHODS[k][1]) for k in CURRENCIES.get(currency, ('', '', '', []))[3]]


def _q(v):
    return D(v).quantize(D('0.01'), rounding=ROUND_DOWN)


def _qa(asset, v):
    """A coin amount at the coin's precision (USDT 2, ETH 6, BTC 8 decimals)."""
    return D(v).quantize(D(1).scaleb(-ASSETS[asset][0]), rounding=ROUND_DOWN)


def fmt(asset, v):
    """1,000.00 USDT · 0.0215 BTC · 0.5 ETH"""
    v = _qa(asset, v or 0)
    if asset == 'USDT':
        return f'{v:,.2f}'
    return f'{v:,.{ASSETS[asset][0]}f}'.rstrip('0').rstrip('.')


# ═══════════════════════════════════════════════════════════════
# WALLETS (real or demo) and escrow movements
# ═══════════════════════════════════════════════════════════════
def _holder(user, asset, demo, lock=False):
    """Where the coins sit: live USDT is the site wallet (EXCHANGE account); everything else a P2PBalance."""
    from .models import Account, P2PBalance
    if not demo and asset == 'USDT':
        acc, _ = Account.objects.get_or_create(user=user, account_type='EXCHANGE', defaults={'balance': D('0')})
        return Account.objects.select_for_update().get(pk=acc.pk) if lock else acc
    b, _ = P2PBalance.objects.get_or_create(user=user, asset=asset, demo=demo)
    return P2PBalance.objects.select_for_update().get(pk=b.pk) if lock else b


def balance(user, asset, demo=None):
    demo = (not live()) if demo is None else demo
    return D(_holder(user, asset, demo).balance)


def balances(user, demo=None):
    return {a: balance(user, a, demo) for a in ASSETS}


def _move(user, asset, amount, demo, kind, details):
    """Add (amount > 0) or take (amount < 0) coins with one atomic UPDATE; caller is inside a transaction."""
    from . import ledger
    from .models import Transaction
    h = _holder(user, asset, demo)
    if amount < 0:
        if not ledger.debit(type(h), h.pk, -amount):
            h.refresh_from_db()
            raise P2PError(f'Not enough {"demo " if demo else ""}{asset}: you need {fmt(asset, -amount)} and have {fmt(asset, h.balance)}.')
    elif amount > 0:
        ledger.credit(type(h), h.pk, amount)
    if not demo and asset == 'USDT':               # the wallet's history (unknown types don't touch balances)
        Transaction.objects.create(user=user, amount=abs(amount), transaction_type=kind, details=details[:255])


def can_trade(user):
    """→ (ok, reason). Real money: approved KYC is required."""
    from .models import KYCProfile
    if user.is_staff:
        return True, ''
    k = KYCProfile.objects.filter(user=user).first()
    if not (k and str(k.status).upper() == 'APPROVED'):
        return False, 'Verify your identity (KYC) to trade on P2P.'
    return True, ''


# ═══════════════════════════════════════════════════════════════
# WHO MAY SELL (by plan)
#   Starter / free trial : buy only
#   Pro                  : sell with normal payment methods
#   VIP, staff (Desk)    : sell with every method, including the high-risk (reversible) ones
# ═══════════════════════════════════════════════════════════════
HIGH_RISK = frozenset({'paypal', 'cashapp_us', 'venmo_us', 'zelle_us', 'skrill', 'neteller', 'revolut', 'revolut_ro',
                       'payoneer', 'chime_us', 'apple_cash_us'})
LEVELS = {'none': 'Buyer only', 'pro': 'Seller (Pro)', 'vip': 'Seller · all methods (VIP)'}


def is_high_risk(key):
    return key in HIGH_RISK


def seller_level(user):
    """'none' | 'pro' | 'vip'. Only paid, active plans may sell; the free trial may not."""
    if not getattr(user, 'is_authenticated', False):
        return 'none'
    if user.is_staff:
        return 'vip'
    from .membership import tier_of
    tier, active = tier_of(user)
    if not active:
        return 'none'
    return {'VIP': 'vip', 'PRO': 'pro'}.get(tier, 'none')


def check_can_sell(user, method_keys=()):
    """Raise P2PError when this user's plan does not allow selling with these methods."""
    level = seller_level(user)
    if level == 'none':
        raise P2PError('Selling on P2P needs a Pro or VIP plan. Starter accounts can buy. Upgrade at /subscription/.')
    risky = sorted({method_name(k) for k in method_keys if k in HIGH_RISK})
    if risky and level != 'vip':
        raise P2PError(f'{", ".join(risky)} {"is a" if len(risky) == 1 else "are"} high-risk (reversible) payment '
                       f'method{"" if len(risky) == 1 else "s"}: only VIP sellers may receive payments there. '
                       'Choose another method or upgrade to VIP at /subscription/.')


def can_sell_with(user, method_keys=()):
    try:
        check_can_sell(user, method_keys)
        return True
    except P2PError:
        return False


def ad_owner_may_sell(ad):
    """A SELL ad stays on the market only while its owner's plan still covers it."""
    return ad.side != 'SELL' or can_sell_with(ad.user, ad.methods or ())


# ═══════════════════════════════════════════════════════════════
# PAYMENT METHODS & ADS
# ═══════════════════════════════════════════════════════════════
def add_method(user, data):
    from .models import P2PPaymentMethod
    key = data.get('method', '')
    if key not in METHODS:
        raise P2PError('Choose a payment method.')
    check_can_sell(user, [key])                    # payment methods are where a seller gets paid
    name, number = (data.get('account_name') or '').strip(), (data.get('account_number') or '').strip()
    if len(name) < 3 or len(number) < 4:
        raise P2PError('Enter the account name and number exactly as registered.')
    return P2PPaymentMethod.objects.create(user=user, method=key, account_name=name[:100], account_number=number[:60],
                                           bank_name=(data.get('bank_name') or '').strip()[:80])


def _dec(v, what):
    try:
        d = D(str(v).replace(',', '').strip())
    except Exception:
        raise P2PError(f'Enter a valid {what}.')
    if d <= 0:
        raise P2PError(f'The {what} must be above 0.')
    return d


@_retry_busy
def post_ad(user, data):
    """Create a BUY or SELL ad. SELL ads lock the USDT now."""
    from .models import P2PAd, P2PPaymentMethod
    ok, why = can_trade(user)
    if not ok:
        raise P2PError(why)
    side = 'SELL' if data.get('side') == 'SELL' else 'BUY'
    if side == 'SELL':
        check_can_sell(user)
    asset = (data.get('asset') or 'USDT').upper()
    if asset not in ASSETS:
        raise P2PError('Choose USDT, BTC or ETH.')
    cur = (data.get('currency') or '').upper()
    if cur not in CURRENCIES:
        raise P2PError('Choose a currency.')
    from . import p2p_rates
    price_type = 'margin' if data.get('price_type') == 'margin' else 'fixed'
    margin = D('0')
    if price_type == 'margin':
        try:
            margin = D(str(data.get('margin') or '0').replace('%', '').strip()).quantize(D('0.01'))
        except Exception:
            raise P2PError('Enter the margin as a number, e.g. 2 for 2% above the market or -1 for 1% below.')
        if not D('-20') <= margin <= D('50'):
            raise P2PError('The margin must be between -20% and +50% of the market price.')
        market = p2p_rates.market_price(asset, cur)
        if not market:
            raise P2PError(f'No live {asset}/{cur} market rate right now. Use a fixed price or try again in a few minutes.')
        price = p2p_rates.with_margin(market, margin)
    else:
        price = _q(_dec(data.get('price'), 'price'))
    total = _qa(asset, _dec(data.get('total'), 'amount'))
    pool = bool(data.get('pool')) and user.is_staff and side == 'SELL'     # desk: coins taken per order
    lo, hi = _q(_dec(data.get('min_fiat'), 'minimum')), _q(_dec(data.get('max_fiat'), 'maximum'))
    if lo > hi:
        raise P2PError('The minimum order must be below the maximum.')
    if lo > total * price:
        raise P2PError(f'The minimum order ({lo:,} {cur}) is more than the whole ad ({total * price:,.2f} {cur}).')
    try:
        limit = int(data.get('time_limit') or 15)
    except ValueError:
        limit = 15
    limit = limit if limit in TIME_LIMITS else 15
    allowed = set(CURRENCIES[cur][3])
    if side == 'SELL':
        ids = data.getlist('pay_methods') if hasattr(data, 'getlist') else data.get('pay_methods', [])
        pms = list(P2PPaymentMethod.objects.filter(user=user, is_active=True, pk__in=[i for i in ids if str(i).isdigit()], method__in=allowed))
        if not pms:
            raise P2PError(f'Add at least one {cur} payment method where buyers will pay you.')
        keys = sorted({p.method for p in pms})
        check_can_sell(user, keys)
    else:
        keys = data.getlist('methods') if hasattr(data, 'getlist') else data.get('methods', [])
        keys = sorted({k for k in keys if k in allowed})
        if not keys:
            raise P2PError('Choose how you will pay sellers.')
        pms = []
    demo = not live()
    with transaction.atomic():
        if side == 'SELL' and not pool:
            _move(user, asset, -total, demo, 'P2P_ESCROW', f'P2P sell ad: {fmt(asset, total)} {asset} locked')
        ad = P2PAd.objects.create(user=user, side=side, asset=asset, currency=cur, price=price, total=total, remaining=total,
                                  price_type=price_type, margin=margin, pool=pool,
                                  min_fiat=lo, max_fiat=hi, methods=keys, time_limit=limit,
                                  terms=(data.get('terms') or '')[:1000], demo=demo)
        if pms:
            ad.pay_methods.set(pms)
    return ad


@_retry_busy
def set_ad_status(user, ad_id, status):
    from .models import P2PAd
    ad = P2PAd.objects.filter(user=user, pk=ad_id).exclude(status='CLOSED').first()
    if not ad:
        raise P2PError('Ad not found.')
    if status != 'CLOSED':
        P2PAd.objects.filter(pk=ad.pk, status__in=('ACTIVE', 'PAUSED')).update(status=status, updated_at=timezone.now())
        ad.refresh_from_db()
        return ad
    if ad.orders.filter(status__in=('PENDING', 'PAID', 'APPEAL')).exists():
        raise P2PError('Finish the open orders on this ad first.')
    with transaction.atomic():
        left = ad.remaining
        # close only if nothing changed since we read it (an order opened a moment ago would change `remaining`)
        if not P2PAd.objects.filter(pk=ad.pk, remaining=left).exclude(status='CLOSED').update(
                status='CLOSED', remaining=D('0') if ad.side == 'SELL' else left, updated_at=timezone.now()):
            raise P2PError('The ad just changed (a new order?). Refresh and try again.')
        if ad.side == 'SELL' and left > 0:
            _move(user, ad.asset, left, ad.demo, 'P2P_REFUND', f'P2P ad closed: {fmt(ad.asset, left)} {ad.asset} unlocked')
    ad.refresh_from_db()
    return ad


def merchant_stats(user):
    from .models import P2POrder, P2PFeedback
    since = timezone.now() - timedelta(days=30)
    mine = P2POrder.objects.filter(Q(buyer=user) | Q(seller=user))
    done = mine.filter(status='COMPLETED')
    recent = mine.filter(created_at__gte=since)
    # only failures that are this user's fault count against them: orders they cancelled or left unpaid as the
    # buyer, and appeals decided against them (a seller is not punished when a buyer walks away)
    at_fault = recent.filter(buyer=user, status='CANCELLED', cancel_reason__in=BUYER_CANCELS).count() \
        + recent.filter(seller=user, status='COMPLETED', resolved_by__isnull=False).count() \
        + recent.filter(buyer=user, status='CANCELLED', cancel_reason='Appeal: refunded to the seller').count()
    finished = recent.filter(status='COMPLETED').count() + at_fault
    fb = P2PFeedback.objects.filter(to_user=user)
    pos, total = fb.filter(positive=True).count(), fb.count()
    rel = [(o.completed_at - o.paid_at).total_seconds() for o in done.filter(seller=user, paid_at__isnull=False)[:50]]
    return {'trades': done.count(), 'trades_30d': recent.filter(status='COMPLETED').count(),
            'completion': round(recent.filter(status='COMPLETED').count() / finished * 100) if finished else None,
            'positive': round(pos / total * 100) if total else None, 'feedback': total,
            'release_min': round(sum(rel) / len(rel) / 60, 1) if rel else None}


def stats_map(users):
    out = {}
    for u in users:
        if u.id not in out:
            out[u.id] = merchant_stats(u)
    return out


# ═══════════════════════════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════════════════════════
def _sys(order, text):
    from .models import P2PMessage
    P2PMessage.objects.create(order=order, text=text)


def _notify(user, order, title, message, ok=True):
    from .models import Notification
    try:
        Notification.objects.create(user=user, title=title[:200], message=message, category='TRADE',
                                    priority='success' if ok else 'warning', action_url=f'/p2p/order/{order.ref}/',
                                    action_label='Open order')
    except Exception:
        pass


def blocked_ids(user):
    """Users this user blocked or was blocked by (no trading in either direction)."""
    from .models import P2PBlock
    mine = set(P2PBlock.objects.filter(user=user).values_list('blocked_id', flat=True))
    theirs = set(P2PBlock.objects.filter(blocked=user).values_list('user_id', flat=True))
    return mine | theirs


def block(user, other_username, on=True):
    from django.contrib.auth.models import User
    from .models import P2PBlock
    other = User.objects.filter(username=other_username).first()
    if not other or other.id == user.id:
        raise P2PError('User not found.')
    if on:
        P2PBlock.objects.get_or_create(user=user, blocked=other)
    else:
        P2PBlock.objects.filter(user=user, blocked=other).delete()
    return other


def buy_pause(user):
    """→ datetime until which this user cannot open buy orders (too many cancels / unpaid), or None."""
    from .models import P2POrder
    since = timezone.now() - timedelta(hours=24)
    rows = list(P2POrder.objects.filter(buyer=user, status='CANCELLED', cancelled_at__gte=since,
                                        cancel_reason__in=BUYER_CANCELS).order_by('-cancelled_at').values_list('cancelled_at', flat=True))
    if len(rows) >= CANCEL_LIMIT:
        return rows[CANCEL_LIMIT - 1] + timedelta(hours=24)
    return None


def express(user, side, asset, currency, fiat):
    """Pick the best offer that fits `fiat` and open the order. side: 'buy' or 'sell' (what the user wants)."""
    from .models import P2PAd, P2PPaymentMethod
    from . import p2p_rates
    p2p_rates.refresh_pair(asset, currency)
    fiat = _q(_dec(fiat, 'amount'))
    want_sell = side == 'sell'
    if want_sell:
        check_can_sell(user)
    qs = P2PAd.objects.filter(status='ACTIVE', asset=asset, currency=currency, demo=not live(),
                              side='BUY' if want_sell else 'SELL', min_fiat__lte=fiat, max_fiat__gte=fiat) \
        .exclude(user=user).exclude(user_id__in=blocked_ids(user)).order_by('-price' if want_sell else 'price')
    mine = {m.method: m for m in P2PPaymentMethod.objects.filter(user=user, is_active=True)} if want_sell else {}
    for ad in qs[:50]:
        if ad.remaining * ad.price < fiat:
            continue
        method_id = None
        if not want_sell and not ad_owner_may_sell(ad):
            continue
        if want_sell:
            match = next((mine[k] for k in ad.methods if k in mine and can_sell_with(user, [k])), None)
            if not match:
                continue
            method_id = match.id
        st = merchant_stats(ad.user)
        if st['completion'] is not None and st['completion'] < 60:
            continue                                      # skip unreliable traders in express mode
        return open_order(user, ad.id, fiat=fiat, method_id=method_id)
    if want_sell and not mine:
        raise P2PError('Add a payment method first (My ads & payment) so buyers can pay you.')
    raise P2PError(f'No offer can take {fiat:,} {currency} right now. Try another amount or pick an offer in the list.')


@_retry_busy
def open_order(user, ad_id, fiat=None, coins=None, method_id=None):
    """Take an ad. fiat (local money) or coins says how much; method_id is the taker's payment method on a BUY ad."""
    from .models import P2PAd, P2POrder, P2PPaymentMethod
    ok, why = can_trade(user)
    if not ok:
        raise P2PError(why)
    if P2POrder.objects.filter(Q(buyer=user) | Q(seller=user), status__in=('PENDING', 'PAID')).count() >= MAX_OPEN_ORDERS:
        raise P2PError(f'You can have up to {MAX_OPEN_ORDERS} open orders. Finish one first.')
    with transaction.atomic():
        ad = P2PAd.objects.filter(pk=ad_id).first()
        if not ad or ad.status != 'ACTIVE':
            raise P2PError('This ad is no longer available.')
        if ad.user_id == user.id:
            raise P2PError('You cannot trade with your own ad.')
        if ad.user_id in blocked_ids(user):
            raise P2PError('You cannot trade with this user.')
        if ad.side == 'SELL' and not ad_owner_may_sell(ad):
            raise P2PError('This ad is no longer available.')
        if ad.side == 'SELL':
            until = buy_pause(user)
            if until and until > timezone.now():
                raise P2PError(f'Buying is paused until {timezone.localtime(until):%d %b %H:%M} because of '
                               f'{CANCEL_LIMIT} cancelled or unpaid orders in 24 hours.')
        if ad.demo != (not live()):
            raise P2PError('This ad belongs to the other mode (demo / live).')
        asset = ad.asset
        from . import p2p_rates
        price = p2p_rates.ad_price(ad)                 # margin ads: today's market ± margin, at this moment
        if coins not in (None, ''):
            amount = _qa(asset, _dec(coins, 'amount'))
            fiat_amt = _q(amount * price)
        else:
            fiat_amt = _q(_dec(fiat, 'amount'))
            amount = _qa(asset, fiat_amt / price)
        if fiat_amt < ad.min_fiat or fiat_amt > ad.max_fiat:
            raise P2PError(f'Order between {ad.min_fiat:,} and {ad.max_fiat:,} {ad.currency}.')
        if amount > ad.remaining:
            raise P2PError(f'Only {fmt(asset, ad.remaining)} {asset} left on this ad.')
        if amount <= 0:
            raise P2PError('The amount is too small.')
        fee = _qa(asset, amount * fee_pct() / 100)
        if ad.side == 'SELL':                       # taker buys; escrow already locked by the ad (or, desk: taken now)
            buyer, seller, escrow_in_ad = user, ad.user, not ad.pool
            pay_to = [{'method': method_name(p.method), 'name': p.account_name, 'number': p.account_number, 'bank': p.bank_name}
                      for p in ad.pay_methods.filter(is_active=True)]
            if ad.pool:
                try:
                    _move(ad.user, asset, -amount, ad.demo, 'P2P_ESCROW', f'P2P desk sale: {fmt(asset, amount)} {asset} locked in escrow')
                except P2PError:
                    raise P2PError('This offer is temporarily out of stock. Try a smaller amount or another offer.')
        else:                                       # taker sells to the advertiser: lock the taker's USDT now
            buyer, seller, escrow_in_ad = ad.user, user, False
            check_can_sell(user)
            pm = P2PPaymentMethod.objects.filter(user=user, pk=method_id, is_active=True, method__in=ad.methods).first()
            if not pm:
                raise P2PError(f'Choose one of your payment methods: {", ".join(method_name(m) for m in ad.methods)}.')
            check_can_sell(user, [pm.method])
            pay_to = [{'method': method_name(pm.method), 'name': pm.account_name, 'number': pm.account_number, 'bank': pm.bank_name}]
            _move(user, asset, -amount, ad.demo, 'P2P_ESCROW', f'P2P sell: {fmt(asset, amount)} {asset} locked in escrow')
        if not P2PAd.objects.filter(pk=ad.pk, status='ACTIVE', remaining__gte=amount).update(
                remaining=F('remaining') - amount, updated_at=timezone.now()):
            raise P2PError('Someone just took this offer. Refresh and try again.')   # rolls back the escrow above
        ad.refresh_from_db()
        if ad.side == 'SELL' and (ad.remaining <= 0 or ad.remaining * ad.price < ad.min_fiat):
            P2PAd.objects.filter(pk=ad.pk, status='ACTIVE').update(status='PAUSED')
        o = P2POrder.objects.create(
            ref='P2P' + secrets.token_hex(4).upper(), ad=ad, buyer=buyer, seller=seller, asset=asset, amount=amount, fee=fee,
            price=price, fiat_amount=fiat_amt, currency=ad.currency, pay_to=pay_to, escrow_in_ad=escrow_in_ad,
            demo=ad.demo, expires_at=timezone.now() + timedelta(minutes=ad.time_limit))
    _sys(o, f'Order created. {o.buyer.username} pays {o.fiat_amount:,} {o.currency} to {o.seller.username} within '
            f'{ad.time_limit} minutes. {fmt(asset, o.amount)} {asset} is locked in escrow.')
    other = ad.user
    _notify(other, o, f'New P2P order {o.ref}: {fmt(asset, o.amount)} {asset}', f'{user.username} opened an order for {o.fiat_amount:,} {o.currency}.')
    return o


def _get(user, ref, lock=False, staff=False):
    from .models import P2POrder
    o = P2POrder.objects.select_related('ad', 'buyer', 'seller').filter(ref=ref).first()
    if not o or not (staff or user.id in (o.buyer_id, o.seller_id)):
        raise P2PError('Order not found.')
    return o


@_retry_busy
def mark_paid(user, ref):
    from .models import P2POrder
    o = _get(user, ref)
    if o.buyer_id != user.id:
        raise P2PError('Only the buyer can mark the order as paid.')
    now = timezone.now()
    if not P2POrder.objects.filter(pk=o.pk, status='PENDING', expires_at__gt=now).update(status='PAID', paid_at=now):
        o.refresh_from_db()
        raise P2PError('The payment time is over: the order was cancelled.' if o.status in ('PENDING', 'CANCELLED')
                       else 'This order is not waiting for payment.')
    o.refresh_from_db()
    _sys(o, f'{user.username} marked the order as paid. Seller: check that {o.fiat_amount:,} {o.currency} has really '
            'arrived in your account (not just an SMS or screenshot), then release.')
    _notify(o.seller, o, f'💰 Buyer paid: release {fmt(o.asset, o.amount)} {o.asset} ({o.ref})',
            f'Check your {o.currency} account for {o.fiat_amount:,} {o.currency} from {o.buyer.username}, then release.')
    return o


def _return_escrow(o):
    """Cancel / refund: the coins go back where they came from (caller already moved the order's status)."""
    from .models import P2PAd
    now = timezone.now()
    if o.escrow_in_ad:
        # back onto the ad while it is open; if it was closed meanwhile, straight back to the seller
        if P2PAd.objects.filter(pk=o.ad_id).exclude(status='CLOSED').update(remaining=F('remaining') + o.amount, updated_at=now):
            ad = P2PAd.objects.get(pk=o.ad_id)
            if ad.status == 'PAUSED' and ad.remaining * ad.price >= ad.min_fiat:
                P2PAd.objects.filter(pk=ad.pk, status='PAUSED').update(status='ACTIVE')
        else:
            _move(o.seller, o.asset, o.amount, o.demo, 'P2P_REFUND', f'P2P order {o.ref} cancelled: {fmt(o.asset, o.amount)} {o.asset} returned')
    else:
        _move(o.seller, o.asset, o.amount, o.demo, 'P2P_REFUND', f'P2P order {o.ref} cancelled: {fmt(o.asset, o.amount)} {o.asset} returned')
        P2PAd.objects.filter(pk=o.ad_id).exclude(status='CLOSED').update(remaining=F('remaining') + o.amount, updated_at=now)


@_retry_busy
def cancel(user, ref, reason='Cancelled by the buyer', system=False):
    """Buyer cancels an unpaid order, or the timer runs out (system). A paid order is never cancelled this way."""
    from .models import P2POrder
    o = _get(user, ref, staff=system)
    if not system and o.buyer_id != user.id:
        raise P2PError('Only the buyer can cancel. If you were not paid, wait for the timer or appeal.')
    now = timezone.now()
    q = P2POrder.objects.filter(pk=o.pk, status='PENDING')
    if system:
        q = q.filter(expires_at__lte=now)              # the buyer may have tapped "paid" a moment ago
    with transaction.atomic():
        if not q.update(status='CANCELLED', cancelled_at=now, cancel_reason=reason[:200]):
            o.refresh_from_db()
            if system:
                return o
            raise P2PError('A paid order cannot be cancelled by the buyer. Open an appeal if something is wrong.'
                           if o.status in ('PAID', 'APPEAL') else 'This order is already finished.')
        _return_escrow(o)
    o.refresh_from_db()
    _sys(o, f'Order cancelled: {reason}. The {o.asset} is back with the seller.')
    for u in (o.buyer, o.seller):
        if u.id != getattr(user, 'id', None) or system:
            _notify(u, o, f'P2P order {o.ref} cancelled', reason, ok=False)
    return o


@_retry_busy
def release(user, ref, staff=False, note='', password=None):
    """Seller (after the buyer marked paid, with their password) or staff (appeal) sends the coins to the buyer."""
    from .models import P2POrder, Transaction
    if not staff and not (password and user.check_password(password)):
        raise P2PError('Wrong password. Enter your account password to release.')
    o = _get(user, ref, staff=staff)
    if not staff and o.seller_id != user.id:
        raise P2PError(f'Only the seller can release the {o.asset}.')
    allowed = ('PENDING', 'PAID', 'APPEAL') if staff else ('PAID',)
    changes = {'status': 'COMPLETED', 'completed_at': timezone.now()}
    if staff:
        changes.update(resolution=(note or 'Released to the buyer by support')[:255], resolved_by=user)
    with transaction.atomic():
        if not P2POrder.objects.filter(pk=o.pk, status__in=allowed).update(**changes):   # exactly one release
            o.refresh_from_db()
            raise P2PError('Release is possible once the buyer has marked the order as paid.' if o.status == 'PENDING'
                           else 'This order cannot be released now.')
        _move(o.buyer, o.asset, o.amount - o.fee, o.demo, 'P2P_BUY', f'P2P {o.ref}: bought {fmt(o.asset, o.amount)} {o.asset} for {o.fiat_amount:,} {o.currency}')
        if not o.demo and o.asset == 'USDT':
            Transaction.objects.create(user=o.seller, amount=o.amount, transaction_type='P2P_SELL',
                                       details=f'P2P {o.ref}: sold {fmt(o.asset, o.amount)} {o.asset} for {o.fiat_amount:,} {o.currency}')
            if o.fee:
                Transaction.objects.create(user=o.buyer, amount=o.fee, transaction_type='P2P_FEE', details=f'P2P fee {o.ref}')
    o.refresh_from_db()
    _sys(o, f'{fmt(o.asset, o.amount - o.fee)} {o.asset} released to {o.buyer.username}' + (f' (fee {fmt(o.asset, o.fee)})' if o.fee else '') +
            ('. Decided by support.' if staff else '. Trade complete.'))
    _notify(o.buyer, o, f'✅ {fmt(o.asset, o.amount - o.fee)} {o.asset} received ({o.ref})', f'The seller released your {o.asset}. Leave feedback for them.')
    if staff:
        _notify(o.seller, o, f'Appeal decided: {o.ref}', f'Support released the {o.asset} to the buyer.', ok=False)
    return o


@_retry_busy
def appeal(user, ref, reason):
    from .models import P2POrder
    o = _get(user, ref)
    if o.status != 'PAID':
        raise P2PError('You can appeal once the buyer has marked the order as paid.')
    if user.id == o.buyer_id and timezone.now() < o.paid_at + timedelta(minutes=10):
        raise P2PError('Give the seller 10 minutes to check the payment before appealing.')
    reason = (reason or '').strip()
    if len(reason) < 10:
        raise P2PError('Explain what happened (at least a sentence).')
    if not P2POrder.objects.filter(pk=o.pk, status='PAID').update(status='APPEAL', appeal_by=user, appeal_reason=reason[:2000]):
        raise P2PError('This order just changed. Refresh the page.')
    o.refresh_from_db()
    _sys(o, f'{user.username} opened an appeal: "{o.appeal_reason}". Support will review the chat and proofs. '
            'Upload your payment proof / statement here.')
    other = o.seller if user.id == o.buyer_id else o.buyer
    _notify(other, o, f'⚠️ Appeal opened on {o.ref}', 'Upload your proof in the order chat; support will decide.', ok=False)
    return o


@_retry_busy
def resolve_refund(staff_user, ref, note=''):
    """Staff: the buyer did not pay → coins back to the seller."""
    from .models import P2POrder
    o = _get(staff_user, ref, staff=True)
    with transaction.atomic():
        if not P2POrder.objects.filter(pk=o.pk, status__in=('APPEAL', 'PAID', 'PENDING')).update(
                status='CANCELLED', cancelled_at=timezone.now(), cancel_reason='Appeal: refunded to the seller',
                resolution=(note or 'Refunded to the seller by support')[:255], resolved_by=staff_user):
            raise P2PError('This order is already finished.')
        _return_escrow(o)
    o.refresh_from_db()
    _sys(o, f'Support decided: the {o.asset} goes back to the seller. ' + (note or ''))
    for u in (o.buyer, o.seller):
        _notify(u, o, f'Appeal decided: {o.ref}', f'Support returned the {o.asset} to the seller.', ok=u.id == o.seller_id)
    return o


def feedback(user, ref, positive, comment=''):
    from .models import P2PFeedback
    o = _get(user, ref, lock=False)
    if o.status != 'COMPLETED':
        raise P2PError('Feedback opens when the trade is complete.')
    to = o.seller if user.id == o.buyer_id else o.buyer
    P2PFeedback.objects.update_or_create(order=o, from_user=user, defaults={'to_user': to, 'positive': bool(positive),
                                                                         'comment': (comment or '')[:200]})


def message(user, ref, text='', image=None):
    from .models import P2PMessage
    o = _get(user, ref, lock=False, staff=user.is_staff)
    text = (text or '').strip()[:1000]
    if not text and not image:
        raise P2PError('Write a message or attach a photo.')
    if image is not None:
        if image.size > 6 * 1024 * 1024:
            raise P2PError('The photo is too large (6 MB max).')
        ctype = getattr(image, 'content_type', '') or ''
        if not (ctype.startswith('image/') or ctype == 'application/pdf'):
            raise P2PError('Attach a photo (JPG, PNG) or a PDF.')
        ext = {'image/png': 'png', 'image/webp': 'webp', 'application/pdf': 'pdf'}.get(ctype, 'jpg')
        image.name = f'{o.ref}_{secrets.token_hex(8)}.{ext}'
    return P2PMessage.objects.create(order=o, sender=user, text=text, image=image or '')


def expire_due():
    """Cancel unpaid orders whose timer ran out (called from pages and the worker)."""
    from .models import P2POrder
    n = 0
    for o in P2POrder.objects.filter(status='PENDING', expires_at__lt=timezone.now()).select_related('buyer'):
        try:
            cancel(o.buyer, o.ref, 'The payment time ran out', system=True)
            n += 1
        except Exception as e:
            logger.warning(f'P2P expire {o.ref}: {e}')
    return n


def safe_expire():
    try:
        return expire_due()
    except Exception as e:
        logger.warning(f'P2P expire: {e}')
        return 0
