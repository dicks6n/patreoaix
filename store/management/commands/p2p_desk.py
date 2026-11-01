"""
PATREOAIX Desk from the console:
    python manage.py p2p_desk --user ADMIN                       # post / refresh offers in every currency
    python manage.py p2p_desk --user ADMIN --currencies KES,UGX --buy-margin -1 --sell-margin 2
    python manage.py p2p_desk --user ADMIN --pause | --resume
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Post or refresh the admin's P2P desk offers (USDT / BTC / ETH, every currency)"

    def add_arguments(self, p):
        p.add_argument('--user', required=True, help='staff username that owns the desk')
        p.add_argument('--currencies', default='', help='comma list; empty = all')
        p.add_argument('--assets', default='USDT,BTC,ETH')
        p.add_argument('--buy-margin', default=None)
        p.add_argument('--sell-margin', default=None)
        p.add_argument('--min-usd', default=None)
        p.add_argument('--max-usd', default=None)
        p.add_argument('--pause', action='store_true')
        p.add_argument('--resume', action='store_true')

    def handle(self, *a, **o):
        from store import p2p, p2p_desk
        u = User.objects.filter(username=o['user']).first()
        if not u or not u.is_staff:
            raise CommandError('Give the username of a staff (admin) account.')
        if o['pause'] or o['resume']:
            n = p2p_desk.set_all(u, 'PAUSED' if o['pause'] else 'ACTIVE')
            self.stdout.write(self.style.SUCCESS(f"{n} desk offers {'paused' if o['pause'] else 'resumed'}"))
            return
        curs = [c.strip().upper() for c in o['currencies'].split(',') if c.strip()] or None
        assets = [x.strip().upper() for x in o['assets'].split(',') if x.strip()]
        opts = {'buy_margin': o['buy_margin'], 'sell_margin': o['sell_margin'], 'min_usd': o['min_usd'], 'max_usd': o['max_usd']}
        try:
            r = p2p_desk.post_all(u, assets, curs, opts)
        except p2p.P2PError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"created {r['created']} · refreshed {r['updated']}"))
        if r['no_method']:
            self.stdout.write(f"buy offers only (add a payment method to sell): {', '.join(r['no_method'])}")
        if r['no_rate']:
            self.stdout.write(f"no exchange rate: {', '.join(r['no_rate'])}")
        for e in r['errors'][:20]:
            self.stdout.write(self.style.WARNING(e))
