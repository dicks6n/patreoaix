"""
python manage.py check_feeds [SYMBOL ...]

Asks every candle source for 1H candles from THIS server and prints what came back, so you can see
which providers your host can reach (Binance refuses US servers, Yahoo blocks datacenters, TwelveData
needs TWELVEDATA_API_KEY, Deriv needs the websocket-client package).
"""
import time

from django.core.management.base import BaseCommand

from store import price_feed


class Command(BaseCommand):
    help = 'Test every candle source from this server'

    def add_arguments(self, parser):
        parser.add_argument('symbols', nargs='*', default=['XAUUSD', 'EURUSD', 'BTCUSD'])
        parser.add_argument('--tf', default='1h')

    def handle(self, *args, symbols, tf, **opts):
        from store import views
        try:
            import websocket  # noqa: F401
            self.stdout.write('websocket-client: installed')
        except ImportError:
            self.stdout.write(self.style.ERROR('websocket-client: NOT installed → pip install websocket-client (Deriv needs it)'))
        self.stdout.write(f"TWELVEDATA_API_KEY: {'set' if price_feed.TWELVEDATA_API_KEY else 'NOT set'}")
        sources = [
            ('Deriv', lambda s: price_feed.fetch_deriv_candles(s, tf, 300)),
            ('Kraken', lambda s: price_feed.fetch_kraken_candles(s, tf, 300)),
            ('Coinbase', lambda s: price_feed.fetch_coinbase_candles(s, tf, 300)),
            ('Binance', lambda s: views._fetch_binance_candles(price_feed.BINANCE_MAP[s], tf, 300) if s in price_feed.BINANCE_MAP else None),
            ('TwelveData', lambda s: price_feed.fetch_twelvedata_candles(s, tf, 300)),
            ('Yahoo', lambda s: views._fetch_yahoo_candles(price_feed.YAHOO_MAP[s], tf, 300) if s in price_feed.YAHOO_MAP else None),
        ]
        for sym in [s.upper() for s in symbols]:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{sym} {tf}'))
            for name, fn in sources:
                t0 = time.time()
                try:
                    c = fn(sym)
                except Exception as e:
                    c, err = [], str(e)
                else:
                    err = (price_feed._LAST_ERR.get(name) or {}).get('error', '')
                if c is None:
                    continue
                took = f'{time.time() - t0:.1f}s'
                if c:
                    self.stdout.write(self.style.SUCCESS(f'  {name:<11} OK   {len(c)} candles, last close {c[-1]["close"]}  ({took})'))
                else:
                    self.stdout.write(self.style.WARNING(f'  {name:<11} --   nothing  ({took}) {err[:120]}'))
            c, src = views.get_candles(sym, tf)
            msg = f'  → the site uses: {src} ({len(c)} candles)' if c else '  → the site has NO candles for this market'
            self.stdout.write(self.style.SUCCESS(msg) if c else self.style.ERROR(msg))
