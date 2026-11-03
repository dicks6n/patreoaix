"""
Full P2P trade between two test accounts (seller and buyer), for checking the trade flow on a TEST database.

    P2P_TEST_ALLOWED=1 python manage.py p2p_trade_test            # create both accounts and run one trade
    P2P_TEST_ALLOWED=1 python manage.py p2p_trade_test --remove   # delete the two accounts and everything they traded

Refuses to run unless P2P_TEST_ALLOWED=1 is set, so it never runs on the live site by accident (never set it in the
live site's .env). It credits the test seller with 500 USDT, which is only valid on a test database. The accounts
have random passwords, printed once; their emails end in TEST_DOMAIN, which is how --remove finds them.
"""
import os
import secrets
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

TEST_DOMAIN = 'trade-test.invalid'
ACCOUNTS = [('seller_test', 'PRO'), ('buyer_test', 'free')]


def _guard():
    if os.environ.get('P2P_TEST_ALLOWED') != '1':
        raise CommandError('Refused: set P2P_TEST_ALLOWED=1 (test database only, never the live site).')


class Command(BaseCommand):
    help = 'Run one full P2P trade between two test accounts, or remove them (test database only)'

    def add_arguments(self, p):
        p.add_argument('--remove', action='store_true', help='delete the test accounts and their trades')

    def handle(self, *a, **o):
        _guard()
        return self.remove() if o['remove'] else self.run()

    # ── run ────────────────────────────────────────────────────
    def run(self):
        from store import p2p
        from store.models import Account, KYCProfile, P2PMessage, UserProfile
        if User.objects.filter(email__endswith='@' + TEST_DOMAIN).exists():
            raise CommandError('Test accounts already exist. Run with --remove first.')
        passwords = {}
        people = {}
        for name, tier in ACCOUNTS:
            pw = secrets.token_urlsafe(12)
            passwords[name] = pw
            u = User.objects.create_user(name, f'{name}@{TEST_DOMAIN}', pw, first_name=name.split('_')[0].title())
            KYCProfile.objects.update_or_create(user=u, defaults={'status': 'APPROVED'})
            p, _ = UserProfile.objects.get_or_create(user=u)
            p.subscription_tier, p.is_active_subscription = tier, True
            p.save()
            people[name] = u
        seller, buyer = people['seller_test'], people['buyer_test']
        acc, _ = Account.objects.get_or_create(user=seller, account_type='EXCHANGE', defaults={'balance': D('0')})
        acc.balance = D('500')
        acc.save()
        out = []
        out.append('1. seller has 500 USDT in wallet (test funds)')
        m = p2p.add_method(seller, {'method': 'mpesa_ke', 'account_name': 'Seller Test', 'account_number': '0712345678'})
        out.append('2. seller added an M-Pesa payment method')
        ad = p2p.post_ad(seller, {'side': 'SELL', 'asset': 'USDT', 'currency': 'KES', 'price_type': 'fixed', 'price': '130',
                                  'total': '200', 'min_fiat': '1000', 'max_fiat': '26000', 'methods': ['mpesa_ke'],
                                  'pay_methods': [m.id], 'time_limit': '15'})
        acc.refresh_from_db()
        out.append(f'3. seller posted a SELL ad: 200 USDT at 130 KES; wallet {acc.balance} USDT (100 in escrow)')
        o = p2p.open_order(buyer, ad.id, fiat='13000', method_id=None)
        out.append(f'4. buyer opened order {o.ref}: {o.amount} USDT for {o.fiat_amount} KES')
        p2p.send_details(seller, o.ref, {'method': 'mpesa_ke', 'account_name': 'Seller Test', 'account_number': '0712345678'})
        out.append('5. seller sent payment details for this trade (the buyer sees them instantly)')
        p2p.message(buyer, o.ref, text='Hi, sending the M-Pesa now')
        out.append('5b. buyer sent a chat message')
        p2p.mark_paid(buyer, o.ref)
        out.append('6. buyer marked paid')
        p2p.release(seller, o.ref, password=passwords['seller_test'])
        o.refresh_from_db()
        out.append(f'7. seller released: status {o.status}')
        p2p.feedback(buyer, o.ref, True, 'Fast release, thank you')
        p2p.feedback(seller, o.ref, True, 'Paid promptly')
        out.append('8. both left feedback')
        bacc = Account.objects.filter(user=buyer, account_type='EXCHANGE').first()
        out.append(f'9. buyer wallet {bacc.balance if bacc else 0} USDT (after the 0.2% fee)')
        out.append(f'10. chat log: {P2PMessage.objects.filter(order=o).count()} system and chat messages')
        self.stdout.write('\n'.join(out))
        self.stdout.write('\nTest logins (shown once):')
        for name, _ in ACCOUNTS:
            self.stdout.write(f'  {name}  password: {passwords[name]}')
        self.stdout.write('\nRemove later: P2P_TEST_ALLOWED=1 python manage.py p2p_trade_test --remove')

    # ── remove ─────────────────────────────────────────────────
    def remove(self):
        from store.models import Account, P2PAd, P2PFeedback, P2PMessage, P2POrder, P2PPaymentMethod
        users = User.objects.filter(email__endswith='@' + TEST_DOMAIN)
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
            P2PAd.objects.filter(user_id__in=ids).delete()
            P2PPaymentMethod.objects.filter(user_id__in=ids).delete()
            Account.objects.filter(user_id__in=ids).delete()
            users.delete()
        self.stdout.write(self.style.SUCCESS(f'Removed {len(ids)} test accounts and {n_orders} trade(s).'))
