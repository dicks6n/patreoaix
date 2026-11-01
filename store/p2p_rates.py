# store/p2p_rates.py
"""
Live market prices for P2P (Paxful-style "market ± %" pricing).

    market price (local money per coin) = coin price in USD × USD→currency rate

  • Coin prices: USDT = 1 USD; BTC / ETH from the site's live price feed (store/price_feed), falling back to the
    last 5-minute candle the scanner caches.
  • Exchange rates: open.er-api.com (free, no key, ~160 currencies), refreshed hourly. The last good table is kept
    for a week, so a short outage never blanks prices. P2P_FX_URL in .env can point to another source with the same
    JSON shape ({"rates": {"KES": 129.1, …}}).
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)
D = Decimal
FX_KEY, FX_LAST_KEY = 'p2p:fx', 'p2p:fx:last'
FX_TTL, FX_LAST_TTL = 3600, 7 * 86400
COIN_SYMBOL = {'BTC': 'BTCUSD', 'ETH': 'ETHUSD'}


def _fx_url():
    return getattr(settings, 'P2P_FX_URL', '') or 'https://open.er-api.com/v6/latest/USD'


def fx_table():
    """{'KES': Decimal(…), …} USD → currency, or {} when no rates were ever fetched."""
    table = cache.get(FX_KEY)
    if table:
        return table
    try:
        import requests
        r = requests.get(_fx_url(), timeout=10)
        rates = (r.json() or {}).get('rates') or {}
        table = {k.upper(): D(str(v)) for k, v in rates.items() if v}
        if table:
            table['USD'] = D('1')
            cache.set(FX_KEY, table, FX_TTL)
            cache.set(FX_LAST_KEY, table, FX_LAST_TTL)
            return table
    except Exception as e:
        logger.warning(f'P2P exchange rates unavailable: {e}')
    table = cache.get(FX_LAST_KEY) or {'USD': D('1')}
    cache.set(FX_KEY, table, 300)                    # try the source again in 5 minutes
    return table


def fx(currency):
    return fx_table().get((currency or '').upper())


def coin_usd(asset):
    """USD price of one coin, or None."""
    if asset == 'USDT':
        return D('1')
    sym = COIN_SYMBOL.get(asset)
    if not sym:
        return None
    key = f'p2p:coin:{asset}'
    try:
        from . import price_feed
        e = price_feed.get_price_entry(sym)
        if e and e.get('price'):
            v = D(str(e['price']))
            cache.set(key, v, 86400)
            return v
    except Exception:
        pass
    try:
        from . import chart_scanner
        c = chart_scanner.candles_for(sym, '5m').get('candles') or []
        if c:
            v = D(str(c[-1]['close']))
            cache.set(key, v, 86400)
            return v
    except Exception:
        pass
    return cache.get(key)


def market_price(asset, currency):
    """Local money per coin at the market, 2 decimals, or None when a rate is missing."""
    usd, rate = coin_usd(asset), fx(currency)
    if not usd or not rate:
        return None
    return (usd * rate).quantize(D('0.01'), rounding=ROUND_HALF_UP)


def with_margin(market, margin):
    return (D(market) * (1 + D(margin) / 100)).quantize(D('0.01'), rounding=ROUND_HALF_UP)


def ad_price(ad):
    """The price an order is opened at: market ± margin for margin ads (live), else the fixed price."""
    if getattr(ad, 'price_type', 'fixed') == 'margin':
        m = market_price(ad.asset, ad.currency)
        if m:
            return with_margin(m, ad.margin)
    return ad.price


def refresh_ad_prices(force=False):
    """Store the live price on every active margin ad (used for sorting and the list). At most once a minute."""
    from .models import P2PAd
    if not force and not cache.add('p2p:refresh', 1, 60):
        return 0
    n = 0
    markets = {}
    for ad in P2PAd.objects.filter(price_type='margin').exclude(status='CLOSED').only('id', 'asset', 'currency', 'margin', 'price'):
        k = (ad.asset, ad.currency)
        if k not in markets:
            markets[k] = market_price(*k)
        if markets[k]:
            p = with_margin(markets[k], ad.margin)
            if p != ad.price:
                P2PAd.objects.filter(pk=ad.pk).update(price=p)
                n += 1
    return n


def safe_refresh():
    try:
        return refresh_ad_prices()
    except Exception as e:
        logger.warning(f'P2P price refresh: {e}')
        return 0
