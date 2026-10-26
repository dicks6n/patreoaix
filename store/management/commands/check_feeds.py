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
        parser.add_argument('symbols', nargs='*', default=['XAUUSD', 'EURUSD', 'GBPJPY', 'BTCUSD'])
        parser.add_argument('--tf', default='1h')

    def handle(self, *args, symbols, tf, **opts):
        from django.core.cache import cache
        from store import views
        # forget earlier pauses so every source is really asked now
        names = ('Deriv', 'Kraken', 'Coinbase', 'Binance', 'BinanceFutures', 'TwelveData', 'Yahoo', 'OANDA')
        price_feed._COOL.clear()
        price_feed._LAST_ERR.clear()
        for n in names:
            cache.delete(f'feed:cool:{n}')
        import os
        proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        self.stdout.write(f"outbound proxy: {proxy or 'none'}" + ('  (free PythonAnywhere account: only approved sites are reachable)' if proxy and 'proxy.server' in proxy else ''))
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
            ('OANDA', lambda s: price_feed.fetch_oanda_candles(s, tf, 300) if price_feed.OANDA_API_KEY else None),
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
                    err = (price_feed._LAST_ERR.pop(name, None) or {}).get('error', '')
                for n in names:                       # each attempt starts unpaused, so one failure doesn't hide the next
                    price_feed._COOL.pop(n, None)
                    cache.delete(f'feed:cool:{n}')
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
