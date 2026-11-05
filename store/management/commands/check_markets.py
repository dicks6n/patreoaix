"""Which markets get live prices and chart candles on this server.

    python manage.py check_markets            # every market in the terminal's Market Watch
    python manage.py check_markets US30 EURAUD
"""
import time

from django.core.management.base import BaseCommand

from store import mt5_service as m, price_feed as pf


class Command(BaseCommand):
    help = 'Check live prices and candles for every tradable market.'

    def add_arguments(self, parser):
        parser.add_argument('symbols', nargs='*')

    def handle(self, *args, symbols=None, **kw):
        from store.views import get_candles, is_market_open
        syms = [s.upper() for s in symbols] if symbols else list(m.MARKET_WATCH)
        ok = no_price = no_candles = 0
        self.stdout.write(f"{'market':8} {'type':7} {'open':5} {'price':>14} {'age':>6}  {'source':10} candles")
        for s in syms:
            spec = m.SPECS.get(s)
            if not spec:
                self.stdout.write(f'{s:8} unknown symbol')
                continue
            is_open = is_market_open(s)[0]
            q = None
            try:
                q = m.live_quote(s, max_age=3600)
            except Exception:
                pass
            entry = pf.get_price_entry(s)
            age = f"{time.time() - entry['ts']:.0f}s" if entry else '—'
            src = (getattr(pf, '_SOURCE', {}).get(s) or ('',))[0] or ('BINANCE/KRAKEN' if s in pf.BINANCE_MAP else '')
            try:
                cdl, csrc = get_candles(s, '1h', 60)
            except Exception:
                cdl, csrc = [], None
            mark = '✅' if q and cdl else '⚠️'
            ok += bool(q and cdl)
            no_price += not q
            no_candles += not cdl
            self.stdout.write(f"{s:8} {spec.kind:7} {'yes' if is_open else 'no':5} {(str(q.mid) if q else 'NO PRICE'):>14} {age:>6}  "
                              f"{str(src)[:10]:10} {len(cdl)} {csrc or ''} {mark}")
        self.stdout.write(f'\n{ok} of {len(syms)} markets have a live price and candles · {no_price} without a price · {no_candles} without candles')
        if no_price:
            self.stdout.write('Markets marked NO PRICE outside their trading hours are normal (forex / indices close at the weekend).')
