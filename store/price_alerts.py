# store/price_alerts.py
"""
Price alerts: "Tell me when gold reaches 4,050".

Checked by the always-on worker on every scanner pass (manage.py run_background → signal_scanner) from the
5-minute candles the scanner already caches: an alert fires when any candle since it was set has traded through
the level (so a quick spike between two checks is not missed). It fires once, as an in-app notification, which
store/push.py also sends to the customer's phone.
"""
import logging

from django.utils import timezone

from . import chart_scanner as cs
from . import mt5_service as mt5

logger = logging.getLogger(__name__)

SYMBOLS = cs.SCAN_SYMBOLS
NAMES = {'XAUUSD': 'Gold', 'XAGUSD': 'Silver', 'USOIL': 'US Oil', 'UKOIL': 'UK Oil', 'BTCUSD': 'Bitcoin',
         'ETHUSD': 'Ethereum', 'SOLUSD': 'Solana', 'BNBUSD': 'BNB', 'XRPUSD': 'XRP'}
MAX_ACTIVE = 30


def name(symbol):
    return NAMES.get(symbol, symbol)


def fmt(symbol, v):
    spec = mt5.SPECS.get(symbol)
    d = spec.digits if spec else 5
    return f'{v:,.{d}f}'


def last_price(symbol):
    candles = cs.candles_for(symbol, '5m').get('candles') or []
    return float(candles[-1]['close']) if candles else None


def create(user, symbol, price, note='', condition=None):
    """Above or below is worked out from the current price when not given."""
    from .models import PriceWatch
    symbol = (symbol or '').upper().replace('/', '')
    if symbol not in SYMBOLS:
        raise ValueError('Choose a market from the list.')
    try:
        price = float(str(price).replace(',', ''))
    except (TypeError, ValueError):
        raise ValueError('Enter a price.')
    if price <= 0:
        raise ValueError('Enter a price above 0.')
    if PriceWatch.objects.filter(user=user, active=True).count() >= MAX_ACTIVE:
        raise ValueError(f'You can have up to {MAX_ACTIVE} active alerts. Delete one first.')
    now = last_price(symbol)
    if condition not in ('above', 'below'):
        condition = 'above' if now is None or price >= now else 'below'
    return PriceWatch.objects.create(user=user, symbol=symbol, condition=condition, price=price, note=(note or '')[:140]), now


def _fire(w, hit):
    from .notifications import notify
    w.active, w.triggered_at, w.triggered_price = False, timezone.now(), hit
    w.save(update_fields=['active', 'triggered_at', 'triggered_price'])
    verb = 'reached' if w.condition == 'above' else 'fell to'
    notify(w.user, title=f'🔔 {name(w.symbol)} {verb} {fmt(w.symbol, w.price)}',
           message=(f'{w.symbol} is at {fmt(w.symbol, hit)} now. ' + (f'Your note: {w.note}' if w.note else 'Open the chart to see the AI setup.')),
           category='TRADE', priority='info', action_url=f'/market-overview/?symbol={w.symbol}', action_label='Open chart',
           metadata={'kind': 'price_alert', 'symbol': w.symbol, 'price': w.price})


def check():
    """Fire every alert whose level traded since it was set. Returns the alerts that fired."""
    from .models import PriceWatch
    fired = []
    active = list(PriceWatch.objects.filter(active=True).select_related('user'))
    by_symbol = {}
    for w in active:
        by_symbol.setdefault(w.symbol, []).append(w)
    for symbol, watches in by_symbol.items():
        try:
            candles = cs.candles_for(symbol, '5m').get('candles') or []
        except Exception as e:
            logger.warning(f'price alerts {symbol}: {e}')
            continue
        if not candles:
            continue
        last = float(candles[-1]['close'])
        for w in watches:
            since = w.created_at.timestamp() - 300        # include the candle that was forming when it was set
            seen = [c for c in candles if c['time'] >= since]
            if not seen:
                continue
            if w.condition == 'above' and max(float(c['high']) for c in seen) >= w.price:
                _fire(w, max(last, w.price)); fired.append(w)
            elif w.condition == 'below' and min(float(c['low']) for c in seen) <= w.price:
                _fire(w, min(last, w.price)); fired.append(w)
    return fired


def safe_check():
    try:
        return check()
    except Exception as e:
        logger.warning(f'price alerts: {e}')
        return []
