# store/p2p.py
"""
P2P marketplace: customers buy and sell USDT, BTC and ETH with each other for local money (M-Pesa, MTN MoMo, bank…).

The site never touches the local money. It only holds the USDT in escrow:
  • SELL ad (advertiser sells USDT): the USDT is locked from the advertiser's wallet when the ad is posted.
  • BUY ad (advertiser buys USDT): the USDT is locked from the seller's wallet when they open an order.
  Buyer pays outside the site → taps "I have paid" → seller checks and taps "Release" → USDT goes to the buyer
  (minus the P2P fee). Unpaid orders expire; either side can appeal and staff decide.

Test mode (P2P_LIVE=False, the default): everyone trades with separate demo coins (P2PBalance demo=True:
1,000 USDT, 0.02 BTC, 0.5 ETH on request), so every step works without real money.
P2P_LIVE=True uses real balances (USDT = the wallet / EXCHANGE account, BTC and ETH = P2PBalance demo=False)
and requires approved KYC.
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

D = Decimal
CURRENCIES = {
    'KES': ('Kenyan shilling', '🇰🇪', ['mpesa_ke', 'airtel_ke', 'bank_ke', 'cash_ke']),
    'UGX': ('Ugandan shilling', '🇺🇬', ['mtn_ug', 'airtel_ug', 'bank_ug']),
    'TZS': ('Tanzanian shilling', '🇹🇿', ['mpesa_tz', 'tigo_tz', 'airtel_tz', 'bank_tz']),
    'RWF': ('Rwandan franc', '🇷🇼', ['mtn_rw', 'airtel_rw', 'bank_rw']),
    'NGN': ('Nigerian naira', '🇳🇬', ['bank_ng', 'opay_ng', 'palmpay_ng', 'kuda_ng']),
    'GHS': ('Ghanaian cedi', '🇬🇭', ['mtn_gh', 'telecel_gh', 'bank_gh']),
    'ZAR': ('South African rand', '🇿🇦', ['bank_za', 'capitec_za']),
    'USD': ('US dollar', '🇺🇸', ['bank_us', 'wise', 'skrill']),
}
METHODS = {
    'mpesa_ke': ('M-Pesa', 'Phone number'), 'airtel_ke': ('Airtel Money', 'Phone number'),
    'bank_ke': ('Bank transfer', 'Account number'), 'cash_ke': ('M-Pesa Paybill / Till', 'Paybill or till number'),
    'mtn_ug': ('MTN Mobile Money', 'Phone number'), 'airtel_ug': ('Airtel Money', 'Phone number'), 'bank_ug': ('Bank transfer', 'Account number'),
    'mpesa_tz': ('Vodacom M-Pesa', 'Phone number'), 'tigo_tz': ('Mixx by Yas (Tigo Pesa)', 'Phone number'),
    'airtel_tz': ('Airtel Money', 'Phone number'), 'bank_tz': ('Bank transfer', 'Account number'),
    'mtn_rw': ('MTN MoMo', 'Phone number'), 'airtel_rw': ('Airtel Money', 'Phone number'), 'bank_rw': ('Bank transfer', 'Account number'),
    'bank_ng': ('Bank transfer', 'Account number'), 'opay_ng': ('OPay', 'OPay number'), 'palmpay_ng': ('PalmPay', 'PalmPay number'),
    'kuda_ng': ('Kuda', 'Account number'),
    'mtn_gh': ('MTN MoMo', 'Phone number'), 'telecel_gh': ('Telecel Cash', 'Phone number'), 'bank_gh': ('Bank transfer', 'Account number'),
    'bank_za': ('Bank transfer (EFT)', 'Account number'), 'capitec_za': ('Capitec Pay', 'Phone or account number'),
    'bank_us': ('Bank transfer', 'Account number'), 'wise': ('Wise', 'Email or Wise tag'), 'skrill': ('Skrill', 'Email'),
}
ASSETS = {                        # decimals, demo grant, name
    'USDT': (2, D('1000'), 'Tether USD'),
    'BTC': (8, D('0.02'), 'Bitcoin'),
    'ETH': (6, D('0.5'), 'Ethereum'),
}
TIME_LIMITS = [15, 30, 45, 60]
MAX_OPEN_ORDERS = 3


class P2PError(Exception):
    """Shown to the customer as-is."""


def live():
    return str(getattr(settings, 'P2P_LIVE', 'False')) == 'True'


def fee_pct():
    try:
        return D(str(getattr(settings, 'P2P_FEE_PCT', '0.2') or '0'))
    except Exception:
        return D('0.2')


def method_name(key):
    return METHODS.get(key, (key, ''))[0]


def methods_for(currency):
    return [(k, METHODS[k][0], METHODS[k][1]) for k in CURRENCIES.get(currency, ('', '', []))[2]]


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


def grant_demo(user):
    """Test mode: top the demo balances up (1,000 USDT, 0.02 BTC, 0.5 ETH)."""
    with transaction.atomic():
        for asset, (_, grant, _) in ASSETS.items():
            h = _holder(user, asset, True, lock=True)
            if h.balance < grant:
                h.balance = grant
                h.save(update_fields=['balance', 'updated_at'])
    return balances(user, True)


def _move(user, asset, amount, demo, kind, details):
    """Add (amount > 0) or take (amount < 0) coins; caller is inside a transaction."""
    from .models import Transaction
    h = _holder(user, asset, demo, lock=True)
    if amount < 0 and D(h.balance) < -amount:
        raise P2PError(f'Not enough {"demo " if demo else ""}{asset}: you need {fmt(asset, -amount)} and have {fmt(asset, h.balance)}.')
    h.balance = D(h.balance) + amount
    h.save(update_fields=['balance', 'updated_at'])
    if not demo and asset == 'USDT':               # the wallet's history (unknown types don't touch balances)
        Transaction.objects.create(user=user, amount=abs(amount), transaction_type=kind, details=details[:255])
    return h.balance


def can_trade(user):
    """→ (ok, reason)."""
    if not live():
        return True, ''
    from .models import KYCProfile
    if user.is_staff:
        return True, ''
    k = KYCProfile.objects.filter(user=user).first()
    if not (k and str(k.status).upper() == 'APPROVED'):
        return False, 'Verify your identity (KYC) to trade on P2P.'
    return True, ''


# ═══════════════════════════════════════════════════════════════
# PAYMENT METHODS & ADS
# ═══════════════════════════════════════════════════════════════
def add_method(user, data):
    from .models import P2PPaymentMethod
    key = data.get('method', '')
    if key not in METHODS:
        raise P2PError('Choose a payment method.')
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


def post_ad(user, data):
    """Create a BUY or SELL ad. SELL ads lock the USDT now."""
    from .models import P2PAd, P2PPaymentMethod
    ok, why = can_trade(user)
    if not ok:
        raise P2PError(why)
    side = 'SELL' if data.get('side') == 'SELL' else 'BUY'
    asset = (data.get('asset') or 'USDT').upper()
    if asset not in ASSETS:
        raise P2PError('Choose USDT, BTC or ETH.')
    cur = (data.get('currency') or '').upper()
    if cur not in CURRENCIES:
        raise P2PError('Choose a currency.')
    price, total = _q(_dec(data.get('price'), 'price')), _qa(asset, _dec(data.get('total'), 'amount'))
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
    allowed = set(CURRENCIES[cur][2])
    if side == 'SELL':
        ids = data.getlist('pay_methods') if hasattr(data, 'getlist') else data.get('pay_methods', [])
        pms = list(P2PPaymentMethod.objects.filter(user=user, is_active=True, pk__in=[i for i in ids if str(i).isdigit()], method__in=allowed))
        if not pms:
            raise P2PError(f'Add at least one {cur} payment method where buyers will pay you.')
        keys = sorted({p.method for p in pms})
    else:
        keys = data.getlist('methods') if hasattr(data, 'getlist') else data.get('methods', [])
        keys = sorted({k for k in keys if k in allowed})
        if not keys:
            raise P2PError('Choose how you will pay sellers.')
        pms = []
    demo = not live()
    with transaction.atomic():
        if side == 'SELL':
            _move(user, asset, -total, demo, 'P2P_ESCROW', f'P2P sell ad: {fmt(asset, total)} {asset} locked')
        ad = P2PAd.objects.create(user=user, side=side, asset=asset, currency=cur, price=price, total=total, remaining=total,
                                  min_fiat=lo, max_fiat=hi, methods=keys, time_limit=limit,
                                  terms=(data.get('terms') or '')[:1000], demo=demo)
        if pms:
            ad.pay_methods.set(pms)
    return ad


def set_ad_status(user, ad_id, status):
    from .models import P2PAd
    with transaction.atomic():
        ad = P2PAd.objects.select_for_update().filter(user=user, pk=ad_id).first()
        if not ad or ad.status == 'CLOSED':
            raise P2PError('Ad not found.')
        if status == 'CLOSED':
            if ad.orders.filter(status__in=('PENDING', 'PAID', 'APPEAL')).exists():
                raise P2PError('Finish the open orders on this ad first.')
            if ad.side == 'SELL' and ad.remaining > 0:
                _move(user, ad.asset, ad.remaining, ad.demo, 'P2P_REFUND', f'P2P ad closed: {fmt(ad.asset, ad.remaining)} {ad.asset} unlocked')
            ad.remaining = D('0') if ad.side == 'SELL' else ad.remaining
        ad.status = status
        ad.save(update_fields=['status', 'remaining', 'updated_at'])
    return ad


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════
def merchant_stats(user):
    from .models import P2POrder, P2PFeedback
    since = timezone.now() - timedelta(days=30)
    mine = P2POrder.objects.filter(Q(buyer=user) | Q(seller=user))
    done = mine.filter(status='COMPLETED')
    recent = mine.filter(created_at__gte=since)
    finished = recent.filter(status__in=('COMPLETED', 'CANCELLED')).count()
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


def open_order(user, ad_id, fiat=None, coins=None, method_id=None):
    """Take an ad. fiat (local money) or coins says how much; method_id is the taker's payment method on a BUY ad."""
    from .models import P2PAd, P2POrder, P2PPaymentMethod
    ok, why = can_trade(user)
    if not ok:
        raise P2PError(why)
    if P2POrder.objects.filter(Q(buyer=user) | Q(seller=user), status__in=('PENDING', 'PAID')).count() >= MAX_OPEN_ORDERS:
        raise P2PError(f'You can have up to {MAX_OPEN_ORDERS} open orders. Finish one first.')
    with transaction.atomic():
        ad = P2PAd.objects.select_for_update().filter(pk=ad_id).first()
        if not ad or ad.status != 'ACTIVE':
            raise P2PError('This ad is no longer available.')
        if ad.user_id == user.id:
            raise P2PError('You cannot trade with your own ad.')
        if ad.demo != (not live()):
            raise P2PError('This ad belongs to the other mode (demo / live).')
        asset = ad.asset
        if coins not in (None, ''):
            amount = _qa(asset, _dec(coins, 'amount'))
            fiat_amt = _q(amount * ad.price)
        else:
            fiat_amt = _q(_dec(fiat, 'amount'))
            amount = _qa(asset, fiat_amt / ad.price)
        if fiat_amt < ad.min_fiat or fiat_amt > ad.max_fiat:
            raise P2PError(f'Order between {ad.min_fiat:,} and {ad.max_fiat:,} {ad.currency}.')
        if amount > ad.remaining:
            raise P2PError(f'Only {fmt(asset, ad.remaining)} {asset} left on this ad.')
        if amount <= 0:
            raise P2PError('The amount is too small.')
        fee = _qa(asset, amount * fee_pct() / 100)
        if ad.side == 'SELL':                       # taker buys; escrow already locked by the ad
            buyer, seller, escrow_in_ad = user, ad.user, True
            pay_to = [{'method': method_name(p.method), 'name': p.account_name, 'number': p.account_number, 'bank': p.bank_name}
                      for p in ad.pay_methods.filter(is_active=True)]
        else:                                       # taker sells to the advertiser: lock the taker's USDT now
            buyer, seller, escrow_in_ad = ad.user, user, False
            pm = P2PPaymentMethod.objects.filter(user=user, pk=method_id, is_active=True, method__in=ad.methods).first()
            if not pm:
                raise P2PError(f'Choose one of your payment methods: {", ".join(method_name(m) for m in ad.methods)}.')
            pay_to = [{'method': method_name(pm.method), 'name': pm.account_name, 'number': pm.account_number, 'bank': pm.bank_name}]
            _move(user, asset, -amount, ad.demo, 'P2P_ESCROW', f'P2P sell: {fmt(asset, amount)} {asset} locked in escrow')
        ad.remaining -= amount
        if ad.remaining <= 0 or ad.remaining * ad.price < ad.min_fiat:
            ad.status = 'PAUSED' if ad.side == 'SELL' else ad.status
        ad.save(update_fields=['remaining', 'status', 'updated_at'])
        o = P2POrder.objects.create(
            ref='P2P' + secrets.token_hex(4).upper(), ad=ad, buyer=buyer, seller=seller, asset=asset, amount=amount, fee=fee,
            price=ad.price, fiat_amount=fiat_amt, currency=ad.currency, pay_to=pay_to, escrow_in_ad=escrow_in_ad,
            demo=ad.demo, expires_at=timezone.now() + timedelta(minutes=ad.time_limit))
    _sys(o, f'Order created. {o.buyer.username} pays {o.fiat_amount:,} {o.currency} to {o.seller.username} within '
            f'{ad.time_limit} minutes. {fmt(asset, o.amount)} {asset} is locked in escrow.')
    other = ad.user
    _notify(other, o, f'New P2P order {o.ref}: {fmt(asset, o.amount)} {asset}', f'{user.username} opened an order for {o.fiat_amount:,} {o.currency}.')
    return o


def _get(user, ref, lock=True, staff=False):
    from .models import P2POrder
    qs = P2POrder.objects.select_for_update() if lock else P2POrder.objects
    o = qs.select_related('ad', 'buyer', 'seller').filter(ref=ref).first()
    if not o or not (staff or user.id in (o.buyer_id, o.seller_id)):
        raise P2PError('Order not found.')
    return o


def mark_paid(user, ref):
    with transaction.atomic():
        o = _get(user, ref)
        if o.buyer_id != user.id:
            raise P2PError('Only the buyer can mark the order as paid.')
        if o.status != 'PENDING':
            raise P2PError('This order is not waiting for payment.')
        if timezone.now() > o.expires_at:
            raise P2PError('The payment time is over: the order was cancelled.')
        o.status, o.paid_at = 'PAID', timezone.now()
        o.save(update_fields=['status', 'paid_at'])
    _sys(o, f'{user.username} marked the order as paid. Seller: check that {o.fiat_amount:,} {o.currency} has really '
            'arrived in your account (not just an SMS or screenshot), then release.')
    _notify(o.seller, o, f'💰 Buyer paid: release {fmt(o.asset, o.amount)} {o.asset} ({o.ref})',
            f'Check your {o.currency} account for {o.fiat_amount:,} {o.currency} from {o.buyer.username}, then release.')
    return o


def _return_escrow(o):
    """Cancel / refund: the USDT goes back where it came from."""
    from .models import P2PAd
    if o.escrow_in_ad:
        ad = P2PAd.objects.select_for_update().get(pk=o.ad_id)
        if ad.status == 'CLOSED':
            _move(o.seller, o.asset, o.amount, o.demo, 'P2P_REFUND', f'P2P order {o.ref} cancelled: {fmt(o.asset, o.amount)} {o.asset} returned')
        else:
            ad.remaining += o.amount
            if ad.status == 'PAUSED' and ad.remaining * ad.price >= ad.min_fiat:
                ad.status = 'ACTIVE'
            ad.save(update_fields=['remaining', 'status', 'updated_at'])
    else:
        _move(o.seller, o.asset, o.amount, o.demo, 'P2P_REFUND', f'P2P order {o.ref} cancelled: {fmt(o.asset, o.amount)} {o.asset} returned')
        ad = P2PAd.objects.select_for_update().get(pk=o.ad_id)
        ad.remaining += o.amount
        ad.save(update_fields=['remaining', 'updated_at'])


def cancel(user, ref, reason='Cancelled by the buyer', system=False):
    with transaction.atomic():
        o = _get(user, ref, staff=system)
        if not system and o.buyer_id != user.id:
            raise P2PError('Only the buyer can cancel. If you were not paid, wait for the timer or appeal.')
        if o.status not in ('PENDING',) and not system:
            raise P2PError('A paid order cannot be cancelled by the buyer. Open an appeal if something is wrong.')
        if o.status in ('COMPLETED', 'CANCELLED'):
            return o
        _return_escrow(o)
        o.status, o.cancelled_at, o.cancel_reason = 'CANCELLED', timezone.now(), reason[:200]
        o.save(update_fields=['status', 'cancelled_at', 'cancel_reason'])
    _sys(o, f'Order cancelled: {reason}. The {o.asset} is back with the seller.')
    for u in (o.buyer, o.seller):
        if u.id != getattr(user, 'id', None):
            _notify(u, o, f'P2P order {o.ref} cancelled', reason, ok=False)
    return o


def release(user, ref, staff=False, note=''):
    """Seller (or staff in an appeal) sends the USDT to the buyer."""
    with transaction.atomic():
        o = _get(user, ref, staff=staff)
        if not staff and o.seller_id != user.id:
            raise P2PError(f'Only the seller can release the {o.asset}.')
        if o.status not in ('PENDING', 'PAID', 'APPEAL') or (o.status == 'APPEAL' and not staff):
            raise P2PError('This order cannot be released now.')
        _move(o.buyer, o.asset, o.amount - o.fee, o.demo, 'P2P_BUY', f'P2P {o.ref}: bought {fmt(o.asset, o.amount)} {o.asset} for {o.fiat_amount:,} {o.currency}')
        if not o.demo and o.asset == 'USDT':
            from .models import Transaction
            Transaction.objects.create(user=o.seller, amount=o.amount, transaction_type='P2P_SELL',
                                       details=f'P2P {o.ref}: sold {fmt(o.asset, o.amount)} {o.asset} for {o.fiat_amount:,} {o.currency}')
            if o.fee:
                Transaction.objects.create(user=o.buyer, amount=o.fee, transaction_type='P2P_FEE', details=f'P2P fee {o.ref}')
        o.status, o.completed_at = 'COMPLETED', timezone.now()
        if staff:
            o.resolution, o.resolved_by = (note or 'Released to the buyer by support')[:255], user
        o.save(update_fields=['status', 'completed_at', 'resolution', 'resolved_by'])
    _sys(o, f'{fmt(o.asset, o.amount - o.fee)} {o.asset} released to {o.buyer.username}' + (f' (fee {fmt(o.asset, o.fee)})' if o.fee else '') +
            ('. Decided by support.' if staff else '. Trade complete.'))
    _notify(o.buyer, o, f'✅ {fmt(o.asset, o.amount - o.fee)} {o.asset} received ({o.ref})', f'The seller released your {o.asset}. Leave feedback for them.')
    if staff:
        _notify(o.seller, o, f'Appeal decided: {o.ref}', f'Support released the {o.asset} to the buyer.', ok=False)
    return o


def appeal(user, ref, reason):
    with transaction.atomic():
        o = _get(user, ref)
        if o.status != 'PAID':
            raise P2PError('You can appeal once the buyer has marked the order as paid.')
        if timezone.now() < o.paid_at + timedelta(minutes=10) and user.id == o.buyer_id:
            raise P2PError('Give the seller 10 minutes to check the payment before appealing.')
        if len((reason or '').strip()) < 10:
            raise P2PError('Explain what happened (at least a sentence).')
        o.status, o.appeal_by, o.appeal_reason = 'APPEAL', user, reason.strip()[:2000]
        o.save(update_fields=['status', 'appeal_by', 'appeal_reason'])
    _sys(o, f'{user.username} opened an appeal: "{o.appeal_reason}". Support will review the chat and proofs. '
            'Upload your payment proof / statement here.')
    other = o.seller if user.id == o.buyer_id else o.buyer
    _notify(other, o, f'⚠️ Appeal opened on {o.ref}', 'Upload your proof in the order chat; support will decide.', ok=False)
    return o


def resolve_refund(staff_user, ref, note=''):
    """Staff: the buyer did not pay → USDT back to the seller."""
    with transaction.atomic():
        o = _get(staff_user, ref, staff=True)
        if o.status not in ('APPEAL', 'PAID', 'PENDING'):
            raise P2PError('This order is already finished.')
        _return_escrow(o)
        o.status, o.cancelled_at = 'CANCELLED', timezone.now()
        o.cancel_reason = 'Appeal: refunded to the seller'
        o.resolution, o.resolved_by = (note or 'Refunded to the seller by support')[:255], staff_user
        o.save(update_fields=['status', 'cancelled_at', 'cancel_reason', 'resolution', 'resolved_by'])
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
