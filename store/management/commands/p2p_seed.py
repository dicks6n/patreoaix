"""
Fill the P2P market with sample traders, offers and trade history (for a presentation copy of the site).

    P2P_SEED_ALLOWED=1 python manage.py p2p_seed                  # 150 traders, ~600 offers, ~4,000 trades
    P2P_SEED_ALLOWED=1 python manage.py p2p_seed --traders 300 --trades 10000 --days 120
    P2P_SEED_ALLOWED=1 python manage.py p2p_seed --remove         # delete everything it created

It only runs when the environment variable P2P_SEED_ALLOWED=1 is set, so it can never run on the live site by
accident (never set it in the live site's .env). Every account it creates has an email ending in SEED_DOMAIN,
which is how --remove finds them; nothing else is touched.
"""
import os
import random
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

# CHANGED: Use a realistic domain so the front-end treats them like real users.
# But we keep it unique so --remove can find and delete ONLY these accounts later.
SEED_DOMAIN = 'fakedata.p2p'          
D = Decimal

# currency: USDT price fallback (when no cached rate), payment methods offered
MARKETS = {
    'KES': ('129.4', ['mpesa_ke', 'airtel_ke', 'bank_ke']),
    'UGX': ('3650', ['mtn_ug', 'airtel_ug', 'bank_ug']),
    'TZS': ('2600', ['mpesa_tz', 'tigo_tz', 'airtel_tz', 'bank_tz']),
    'NGN': ('1550', ['bank_ng', 'opay_ng', 'palmpay_ng', 'kuda_ng', 'moniepoint_ng']),
    'GHS': ('15.4', ['mtn_gh', 'telecel_gh', 'bank_gh']),
    'ZAR': ('18.1', ['bank_za', 'capitec_za', 'payshap_za']),
    'RWF': ('1420', ['mtn_rw', 'airtel_rw', 'bank_rw']),
    'ZMW': ('26.5', ['mtn_zm', 'airtel_zm', 'zamtel_zm']),
    'USD': ('1', ['bank_us', 'zelle_us', 'cashapp_us', 'chime_us']),
    'EUR': ('0.92', ['sepa_eu', 'sepa_instant_eu']),
    'GBP': ('0.78', ['faster_payments_gb', 'monzo_gb']),
    'INR': ('87.5', ['upi_in', 'imps_in', 'phonepe_in']),
    'AED': ('3.67', ['payit_ae', 'bank_transfer']),
}
WEIGHT = {'KES': 22, 'NGN': 16, 'UGX': 10, 'TZS': 9, 'GHS': 8, 'ZAR': 7, 'RWF': 4, 'ZMW': 4, 'USD': 6, 'EUR': 4,
          'GBP': 3, 'INR': 4, 'AED': 3}
COIN_USD = {'USDT': D('1'), 'BTC': D('95000'), 'ETH': D('3500')}
FIRST = ['Wanjiru', 'Kamau', 'Otieno', 'Achieng', 'Mwangi', 'Njeri', 'Kiprop', 'Chebet', 'Musa', 'Amina', 'Okello',
         'Nakato', 'Ssempa', 'Baraka', 'Neema', 'Juma', 'Zawadi', 'Chidi', 'Ngozi', 'Tunde', 'Bisi', 'Emeka', 'Kofi',
         'Ama', 'Kwame', 'Akosua', 'Thabo', 'Lerato', 'Sipho', 'Naledi', 'Eric', 'Aline', 'Mutale', 'Chanda', 'Daniel',
         'Grace', 'Brian', 'Faith', 'Kevin', 'Mercy', 'Rahul', 'Priya', 'Omar', 'Layla', 'James', 'Sarah', 'Lucas', 'Emma']
STYLE = ['{f}Trades', '{f}_fx', '{f}{n}', '{f}.p2p', '{f}Exchange', 'Swift{f}', '{f}_otc', '{f}Pay', '{f}{n}x', 'the{f}']
TERMS = ['Fast release, usually within 5 minutes.', 'Pay only from an account in your own name. No third-party payments.',
         'Online 7am to 11pm. Send the exact amount.', 'Do not write anything about crypto in the payment reference.',
         'Verified merchant, repeat buyers welcome.', '']
CHAT = [('buyer', 'Hi, sending now'), ('buyer', 'Done, please check'), ('seller', 'Received, releasing'),
        ('seller', 'Thank you, welcome again'), ('buyer', 'Got it, thanks!')]


def _q(v, places=2):
    return D(v).quantize(D(1).scaleb(-places), rounding=ROUND_DOWN)


class Command(BaseCommand):
    help = 'Fill the P2P market with sample traders, offers and trade history (presentation copy only)'

    def add_arguments(self, p):
        p.add_argument('--traders', type=int, default=150)
        p.add_argument('--trades', type=int, default=4000)
        p.add_argument('--days', type=int, default=90, help='spread the trade history over this many days')
        p.add_argument('--remove', action='store_true', help='delete everything this command created')
        p.add_argument('--status', action='store_true', help='which database is used and how many offers it holds')

    def handle(self, *a, **o):
        if o['status']:
            return self.status()
        if os.environ.get('P2P_SEED_ALLOWED') != '1':
            raise CommandError('Refused: set P2P_SEED_ALLOWED=1 (only on the presentation copy, never on the live site).')
        from django.db import connection
        self.stdout.write(f"Database: {connection.settings_dict['NAME']}")
        if o['remove']:
            return self.remove()
        if User.objects.filter(email__endswith='@' + SEED_DOMAIN).exists():
            raise CommandError('Sample data is already there. Run with --remove first to start again.')
        rnd = random.Random()
        with transaction.atomic():
            users = self.make_traders(rnd, o['traders'])
            ads = self.make_ads(rnd, users)
            n = self.make_trades(rnd, users, ads, o['trades'], o['days'])
        self.stdout.write(self.style.SUCCESS(f'{len(users)} traders, {len(ads)} offers, {n} trades added. '
                                             f'Remove later with: P2P_SEED_ALLOWED=1 python manage.py p2p_seed --remove'))

    # ── traders ────────────────────────────────────────────────
    def make_traders(self, rnd, count):
        from store.models import UserProfile
        now, users, taken = timezone.now(), [], set(User.objects.values_list('username', flat=True))
        while len(users) < count:
            f = rnd.choice(FIRST)
            name = rnd.choice(STYLE).format(f=f, n=rnd.randint(1, 99))[:30]
            if name.lower() in {t.lower() for t in taken}:
                continue
            taken.add(name)
            u = User(username=name, email=f'{name.lower().replace(".", "_")}@{SEED_DOMAIN}', first_name=f,
                     date_joined=now - timedelta(days=rnd.randint(20, 400)))
            u.set_unusable_password()
            users.append(u)
        User.objects.bulk_create(users)
        users = list(User.objects.filter(email__endswith='@' + SEED_DOMAIN))
        profiles = []
        for u in users:
            p, _ = UserProfile.objects.get_or_create(user=u)
            r = rnd.random()                        # sellers need Pro / VIP; about a fifth only buy
            p.subscription_tier, p.is_active_subscription = ('VIP', True) if r < .3 else ('PRO', True) if r < .8 else ('free', False)
            if p.is_active_subscription:
                p.subscription_started_at, p.subscription_expires_at = now - timedelta(days=30), now + timedelta(days=365)
            profiles.append(p)
        UserProfile.objects.bulk_update(profiles, ['subscription_tier', 'is_active_subscription',
                                                   'subscription_started_at', 'subscription_expires_at'])
        for u, p in zip(users, profiles):
            u.level = {'VIP': 'vip', 'PRO': 'pro'}.get(p.subscription_tier, 'none') if p.is_active_subscription else 'none'
            u.home = rnd.choices(list(WEIGHT), weights=list(WEIGHT.values()))[0]
        return users

    # ── offers ─────────────────────────────────────────────────
    def price(self, asset, cur):
        from store import p2p_rates
        try:
            usd = COIN_USD[asset] if asset == 'USDT' else (p2p_rates.coin_usd(asset) or COIN_USD[asset])
            fx = p2p_rates.fx_cached(cur)
            return D(str(usd)) * D(str(fx)) if fx else COIN_USD[asset] * D(MARKETS[cur][0])
        except Exception:
            return COIN_USD[asset] * D(MARKETS[cur][0])

    def make_ads(self, rnd, users):
        from store import p2p
        from store.models import P2PAd, P2PPaymentMethod
        ads, methods = [], []
        for u in users:
            for _ in range(rnd.randint(2, 6)):
                cur = u.home if rnd.random() < .75 else rnd.choices(list(WEIGHT), weights=list(WEIGHT.values()))[0]
                side = 'BUY' if u.level == 'none' or rnd.random() < .45 else 'SELL'
                asset = rnd.choices(['USDT', 'BTC', 'ETH'], weights=[80, 12, 8])[0]
                ok = [m for m in MARKETS[cur][1] if m in p2p.METHODS and (side == 'BUY' or u.level == 'vip' or m not in p2p.HIGH_RISK)]
                if not ok:
                    continue
                pick = rnd.sample(ok, k=min(len(ok), rnd.randint(1, 3)))
                margin = D(str(round(rnd.uniform(.2, 2.8) if side == 'SELL' else -rnd.uniform(.2, 2.5), 2)))
                price = _q(self.price(asset, cur) * (1 + margin / 100))
                usd = D(rnd.choice([150, 300, 500, 800, 1200, 2000, 3500, 5000, 8000, 15000]))
                total = _q(usd / COIN_USD[asset], p2p.ASSETS[asset][0])
                fx_unit = price / COIN_USD[asset]   # local money per 1 USD
                min_f = _q(fx_unit * D(rnd.choice([5, 10, 20, 50])), 0)
                max_f = max(min_f * 2, _q(price * total * D(str(rnd.uniform(.4, 1))), 0))
                ads.append(P2PAd(user=u, side=side, asset=asset, currency=cur, price=price, price_type='margin',
                                 margin=margin, total=total, remaining=_q(total * D(str(rnd.uniform(.35, 1))), p2p.ASSETS[asset][0]),
                                 min_fiat=min_f, max_fiat=max_f, methods=pick, time_limit=rnd.choice([15, 15, 30, 45]),
                                 terms=rnd.choice(TERMS), status='ACTIVE' if rnd.random() < .9 else 'PAUSED', demo=False))
                if side == 'SELL':
                    for m in pick:
                        methods.append(P2PPaymentMethod(user=u, method=m, account_name=f'{u.first_name} {rnd.choice(FIRST)}',
                                                        account_number=f'0{rnd.randint(700000000, 799999999)}'))
        P2PAd.objects.bulk_create(ads)
        P2PPaymentMethod.objects.bulk_create(methods)
        ads = list(P2PAd.objects.filter(user__in=users))
        by_user = {}
        for m in P2PPaymentMethod.objects.filter(user__in=users):
            by_user.setdefault((m.user_id, m.method), m)
        for ad in ads:
            if ad.side == 'SELL':
                ad.pay_methods.set([by_user[(ad.user_id, k)] for k in ad.methods if (ad.user_id, k) in by_user])
        return ads

    # ── trade history ──────────────────────────────────────────
    def make_trades(self, rnd, users, ads, count, days):
        from store import p2p
        from store.models import P2PFeedback, P2PMessage, P2POrder
        now, fee = timezone.now(), p2p.fee_pct() / 100
        orders, when, refs = [], [], set()
        for _ in range(count):
            ad = rnd.choice(ads)
            other = rnd.choice(users)
            if other.id == ad.user_id:
                continue
            buyer, seller = (other, ad.user) if ad.side == 'SELL' else (ad.user, other)
            places = p2p.ASSETS[ad.asset][0]
            fiat = _q(D(str(rnd.uniform(float(ad.min_fiat), float(ad.max_fiat)))), 0)
            amount = _q(fiat / ad.price, places)
            if amount <= 0:
                continue
            created = now - timedelta(days=days * rnd.random() ** 1.6, minutes=rnd.randint(0, 59))   # busier lately
            r = rnd.random()
            status = 'COMPLETED' if r < .93 else 'CANCELLED'
            paid = created + timedelta(minutes=rnd.uniform(1.5, 9))
            done = paid + timedelta(minutes=rnd.uniform(.5, 11))
            ref = 'P2P' + '%08X' % rnd.getrandbits(32)
            if ref in refs:
                continue
            refs.add(ref)
            orders.append(P2POrder(
                ref=ref,
                ad=ad, buyer=buyer, seller=seller, asset=ad.asset, amount=amount,
                fee=_q(amount * fee, places), price=ad.price, fiat_amount=fiat, currency=ad.currency,
                pay_to=[{'method': p2p.method_name(m), 'number': f'0{rnd.randint(700000000, 799999999)}'} for m in ad.methods[:1]],
                escrow_in_ad=ad.side == 'SELL', status=status, demo=False,
                expires_at=created + timedelta(minutes=ad.time_limit),
                paid_at=paid if status == 'COMPLETED' else None, completed_at=done if status == 'COMPLETED' else None,
                cancelled_at=created + timedelta(minutes=ad.time_limit) if status == 'CANCELLED' else None,
                cancel_reason='' if status == 'COMPLETED' else rnd.choice(p2p.BUYER_CANCELS)))
            when.append(created)
        P2POrder.objects.bulk_create(orders)
        saved = list(P2POrder.objects.filter(ref__in=[o.ref for o in orders]))
        at = {o.ref: w for o, w in zip(orders, when)}
        for o in saved:
            o.created_at = at[o.ref]
        P2POrder.objects.bulk_update(saved, ['created_at'], batch_size=500)
        fb, msgs = [], []
        for o in saved:
            if o.status != 'COMPLETED':
                continue
            if rnd.random() < .7:
                fb.append(P2PFeedback(order=o, from_user_id=o.buyer_id, to_user_id=o.seller_id, positive=rnd.random() < .97,
                                      comment=rnd.choice(['', '', 'Fast and smooth', 'Trusted seller', 'Quick release, thanks'])))
            if rnd.random() < .5:
                fb.append(P2PFeedback(order=o, from_user_id=o.seller_id, to_user_id=o.buyer_id, positive=rnd.random() < .98))
            for who, text in CHAT[:rnd.randint(0, len(CHAT))]:
                msgs.append(P2PMessage(order=o, sender_id=o.buyer_id if who == 'buyer' else o.seller_id, text=text))
        P2PFeedback.objects.bulk_create(fb, batch_size=500)
        P2PMessage.objects.bulk_create(msgs, batch_size=500)
        return len(saved)

    def status(self):
        from django.db import connection
        from django.db.models import Count
        from store.models import P2PAd, P2POrder
        self.stdout.write(f"Database: {connection.settings_dict['NAME']}")
        seed = User.objects.filter(email__endswith='@' + SEED_DOMAIN)
        self.stdout.write(f"Sample traders: {seed.count()}   offers: {P2PAd.objects.filter(user__in=seed).count()}   "
                          f"trades: {P2POrder.objects.filter(ad__user__in=seed).count()}")
        rows = P2PAd.objects.filter(status='ACTIVE', demo=False).values('currency', 'side').annotate(n=Count('id')).order_by('currency')
        line = {}
        for r in rows:
            line.setdefault(r['currency'], {})[r['side']] = r['n']
        for cur, v in line.items():
            self.stdout.write(f"  {cur}: {v.get('SELL', 0)} sell offers (Buy tab), {v.get('BUY', 0)} buy offers (Sell tab)")
        if not line:
            self.stdout.write('  No active offers in this database.')

    # ── remove ─────────────────────────────────────────────────
    def remove(self):
        from django.db.models import Q
        from store.models import P2PAd, P2PFeedback, P2PMessage, P2POrder, P2PPaymentMethod
        users = User.objects.filter(email__endswith='@' + SEED_DOMAIN)
        ids = list(users.values_list('id', flat=True))
        if not ids:
            self.stdout.write('Nothing to remove.')
            return
        with transaction.atomic():
            orders = P2POrder.objects.filter(Q(buyer_id__in=ids) | Q(seller_id__in=ids) | Q(ad__user_id__in=ids))
            P2PFeedback.objects.filter(order__in=orders).delete()
            P2PMessage.objects.filter(order__in=orders).delete()
            n_orders = orders.count()
            orders.delete()
            n_ads = P2PAd.objects.filter(user_id__in=ids).delete()[0]
            P2PPaymentMethod.objects.filter(user_id__in=ids).delete()
            users.delete()
        self.stdout.write(self.style.SUCCESS(f'Removed {len(ids)} traders, {n_orders} trades and their offers.'))
