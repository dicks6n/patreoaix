# store/p2p_desk.py
"""
PATREOAIX Desk ("King"): the admin's own P2P offers in every currency for USDT, BTC and ETH.

  • BUY offers (the desk buys coins): no coins needed up front; the admin pays each seller from his own accounts,
    to the seller's payment details shown in the order.
  • SELL offers (the desk sells coins): pool ads. Coins are taken from the admin's wallet into escrow only when
    someone opens an order, so one balance backs every currency. If the balance is short the order is refused,
    and nobody pays for coins that are not there.
  • Prices are market ± margin (Paxful style) and follow live rates (p2p_rates).
  • Sell offers need a payment method of the admin that works in that currency: a local one, or one universal
    method (Wise, bank transfer, PayPal…) covers every currency. Currencies without one only get a BUY offer.

All of this is real: real ads, real escrow, real stats. Nothing is simulated.
"""
import logging
from decimal import Decimal, ROUND_DOWN

from django.utils import timezone

from . import p2p, p2p_rates
from .p2p_markets import CURRENCIES, UNIVERSAL

logger = logging.getLogger(__name__)
D = Decimal
DEFAULTS = {'buy_margin': D('-1.5'), 'sell_margin': D('1.5'), 'min_usd': D('10'), 'max_usd': D('1000'),
            'buy_capacity_usd': D('5000'), 'sell_capacity_usd': D('5000'), 'time_limit': 30}
DESK_TERMS = ('PATREOAIX Desk 👑 official offer. Pay only to the details shown in the order, from an account in '
              'your own name. Coins are released as soon as the payment is confirmed.')


def _round_fiat(v):
    """Friendly limits: 1,234.56 → 1,230 (2 significant figures for small, whole numbers above 100)."""
    v = D(v)
    if v >= 100:
        step = D(10) ** max(0, len(str(int(v))) - 2)
        return (v / step).quantize(D('1'), rounding=ROUND_DOWN) * step
    return v.quantize(D('1'), rounding=ROUND_DOWN) if v >= 1 else v.quantize(D('0.01'))


def desk_ads(user):
    from .models import P2PAd
    return P2PAd.objects.filter(user=user, demo=False).exclude(status='CLOSED').filter(terms__startswith='PATREOAIX Desk')


def post_all(user, assets=('USDT', 'BTC', 'ETH'), currencies=None, opts=None):
    """Post or update the desk offers. Returns a report dict."""
    from .models import P2PAd, P2PPaymentMethod
    if not user.is_staff:
        raise p2p.P2PError('Only the admin can run the desk.')
    o = dict(DEFAULTS, **{k: D(str(v)) if k != 'time_limit' else int(v) for k, v in (opts or {}).items() if v not in (None, '')})
    currencies = [c for c in (currencies or CURRENCIES) if c in CURRENCIES]
    mine = list(P2PPaymentMethod.objects.filter(user=user, is_active=True))
    report = {'created': 0, 'updated': 0, 'no_rate': [], 'no_method': [], 'errors': []}
    existing = {(a.side, a.asset, a.currency): a for a in desk_ads(user)}
    for cur in currencies:
        rate = p2p_rates.fx(cur)
        if not rate:
            report['no_rate'].append(cur)
            continue
        allowed = set(CURRENCIES[cur][3])
        sell_methods = [m for m in mine if m.method in allowed]
        lo, hi = _round_fiat(o['min_usd'] * rate), _round_fiat(o['max_usd'] * rate)
        for asset in assets:
            usd = p2p_rates.coin_usd(asset)
            if not usd:
                report['errors'].append(f'{asset}: no live price')
                continue
            for side, margin, cap in (('BUY', o['buy_margin'], o['buy_capacity_usd']), ('SELL', o['sell_margin'], o['sell_capacity_usd'])):
                if side == 'SELL' and not sell_methods:
                    if cur not in report['no_method']:
                        report['no_method'].append(cur)
                    continue
                total = p2p._qa(asset, cap / usd)
                ad = existing.get((side, asset, cur))
                if ad:                                         # refresh in place (keeps its history and orders)
                    ad.price_type, ad.margin, ad.min_fiat, ad.max_fiat = 'margin', margin, lo, hi
                    ad.price = p2p_rates.with_margin(p2p_rates.market_price(asset, cur), margin)
                    if ad.remaining < total:
                        ad.total, ad.remaining = total, total
                    if ad.status == 'PAUSED':
                        ad.status = 'ACTIVE'
                    if side == 'SELL':
                        ad.methods = sorted({m.method for m in sell_methods})
                    ad.save(update_fields=['price_type', 'margin', 'min_fiat', 'max_fiat', 'price', 'total', 'remaining', 'status',
                                           'methods', 'updated_at'])
                    if side == 'SELL':
                        ad.pay_methods.set(sell_methods)
                    report['updated'] += 1
                    continue
                data = {'side': side, 'asset': asset, 'currency': cur, 'price_type': 'margin', 'margin': str(margin),
                        'total': str(total), 'min_fiat': str(lo), 'max_fiat': str(hi), 'time_limit': str(o['time_limit']),
                        'terms': DESK_TERMS, 'pool': side == 'SELL'}
                if side == 'SELL':
                    data['pay_methods'] = [str(m.id) for m in sell_methods]
                else:
                    local = [k for k in CURRENCIES[cur][3] if k not in UNIVERSAL][:4]
                    data['methods'] = local + ['bank_transfer']
                try:
                    p2p.post_ad(user, data)
                    report['created'] += 1
                except p2p.P2PError as e:
                    report['errors'].append(f'{side} {asset}/{cur}: {e}')
    return report


def set_all(user, status):
    """Pause / resume every desk offer at once."""
    from .models import P2PAd
    frm = ('ACTIVE',) if status == 'PAUSED' else ('PAUSED',)
    return desk_ads(user).filter(status__in=frm).update(status=status, updated_at=timezone.now())


def summary(user):
    ads = list(desk_ads(user))
    return {'total': len(ads), 'active': sum(a.status == 'ACTIVE' for a in ads),
            'buy': sum(a.side == 'BUY' for a in ads), 'sell': sum(a.side == 'SELL' for a in ads),
            'currencies': len({a.currency for a in ads}), 'balances': {a: p2p.fmt(a, b) for a, b in p2p.balances(user).items()}}
