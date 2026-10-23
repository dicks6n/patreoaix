# store/mt5_service.py
"""
MT5-style trade server.

Everything that decides a price, a margin or a profit lives here, so the
terminal, the REST endpoints and the background order engine all agree:

  • Symbol specification  — digits, contract size, currencies, leverage,
                             spread, swap, stops level, volume limits
  • Quotes                — bid / ask built server-side from the price feed
                             (the browser never supplies a trade price)
  • Profit & margin       — converted to the USD account currency
                             (USDJPY profit is in JPY, EURGBP margin in EUR …)
  • Orders                — market open / close / partial close / close by,
                             S/L + T/P modify, trailing stop
  • Pending orders        — Buy/Sell Limit, Buy/Sell Stop, Buy/Sell Stop Limit,
                             expiration GTC / Today / Specified
  • Engine                — fills pending orders, executes S/L, T/P and
                             trailing stops, charges swap at rollover,
                             margin call warning and stop out

Runs from `manage.py process_orders` and on every /api/mt5/metrics/ call.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import price_feed

logger = logging.getLogger(__name__)

NY = ZoneInfo('America/New_York')
CENT = Decimal('0.01')
ZERO = Decimal('0.00')

ACCOUNT_CURRENCY = 'USD'
MARGIN_CALL_LEVEL = Decimal(str(getattr(settings, 'MT5_MARGIN_CALL_LEVEL', 100)))
STOP_OUT_LEVEL = Decimal(str(getattr(settings, 'MT5_STOP_OUT_LEVEL', 50)))
QUOTE_MAX_AGE = float(getattr(settings, 'MT5_QUOTE_MAX_AGE', 30))      # seconds
MAX_PENDING_ORDERS = 50
TICKET_OFFSET = 8400                                                    # position ticket = 8400 + Trade.id
PENDING_TICKET_OFFSET = 9000000                                         # order ticket = 9000000 + PendingOrder.id

DERIV_WS_URL = 'wss://ws.derivws.com/websockets/v3?app_id=1089'


class TradeError(Exception):
    """A request the trade server refuses. The message is shown to the trader as-is."""


def q2(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ═══════════════════════════════════════════════════════════════
# SYMBOL SPECIFICATION
# ═══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    description: str
    kind: str              # forex | metal | energy | crypto | synthetic
    digits: int
    contract_size: Decimal
    base: str              # base / margin currency
    quote: str             # profit currency
    leverage: int
    spread: int            # points
    swap_long: Decimal     # points per lot per day
    swap_short: Decimal
    stops_level: int       # points: min distance of S/L, T/P and pending prices from market
    volume_min: Decimal = Decimal('0.01')
    volume_max: Decimal = Decimal('100')
    volume_step: Decimal = Decimal('0.01')
    commission: Decimal = ZERO     # USD per lot per side

    @property
    def point(self):
        return Decimal(1).scaleb(-self.digits)

    def as_json(self):
        return {
            'symbol': self.symbol, 'description': self.description, 'kind': self.kind,
            'digits': self.digits, 'point': float(self.point),
            'contract_size': float(self.contract_size),
            'base': self.base, 'quote': self.quote, 'leverage': self.leverage,
            'spread': self.spread, 'swap_long': float(self.swap_long), 'swap_short': float(self.swap_short),
            'stops_level': self.stops_level,
            'volume_min': float(self.volume_min), 'volume_max': float(self.volume_max),
            'volume_step': float(self.volume_step), 'commission': float(self.commission),
        }


# symbol: (description, kind, digits, contract size, base, quote, leverage, spread pts, swap long, swap short, stops level)
# Spreads and swaps are broker settings — tune them here (or per symbol in settings.MT5_SYMBOL_OVERRIDES).
_SPECS = {
    'EURUSD': ('Euro vs US Dollar',              'forex', 5, 100000, 'EUR', 'USD', 500, 12, -7.2,  1.9, 0),
    'GBPUSD': ('British Pound vs US Dollar',     'forex', 5, 100000, 'GBP', 'USD', 500, 15, -4.6, -0.8, 0),
    'USDJPY': ('US Dollar vs Japanese Yen',      'forex', 3, 100000, 'USD', 'JPY', 500, 14, 12.5, -25.0, 0),
    'AUDUSD': ('Australian Dollar vs US Dollar', 'forex', 5, 100000, 'AUD', 'USD', 500, 14, -4.0, -0.5, 0),
    'USDCAD': ('US Dollar vs Canadian Dollar',   'forex', 5, 100000, 'USD', 'CAD', 500, 18,  1.2, -6.5, 0),
    'NZDUSD': ('New Zealand Dollar vs US Dollar','forex', 5, 100000, 'NZD', 'USD', 500, 18, -3.5, -1.0, 0),
    'USDCHF': ('US Dollar vs Swiss Franc',       'forex', 5, 100000, 'USD', 'CHF', 500, 16,  4.5, -11.0, 0),
    'EURGBP': ('Euro vs British Pound',          'forex', 5, 100000, 'EUR', 'GBP', 500, 15, -5.5,  0.8, 0),
    'EURJPY': ('Euro vs Japanese Yen',           'forex', 3, 100000, 'EUR', 'JPY', 500, 20,  5.0, -18.0, 0),
    'GBPJPY': ('British Pound vs Japanese Yen',  'forex', 3, 100000, 'GBP', 'JPY', 500, 28,  9.0, -25.0, 0),
    'EURCHF': ('Euro vs Swiss Franc',            'forex', 5, 100000, 'EUR', 'CHF', 500, 20, -2.5, -4.5, 0),
    'GBPCHF': ('British Pound vs Swiss Franc',   'forex', 5, 100000, 'GBP', 'CHF', 500, 30,  1.5, -10.0, 0),
    'XAUUSD': ('Gold vs US Dollar',              'metal', 2, 100,    'XAU', 'USD', 100, 25, -45.0, 20.0, 0),
    'XAGUSD': ('Silver vs US Dollar',            'metal', 3, 5000,   'XAG', 'USD', 100, 30, -6.0,  2.0, 0),
    'USOIL':  ('WTI Crude Oil',                  'energy', 2, 1000,  'USD', 'USD', 100, 4,  -5.0, -3.0, 0),
    'UKOIL':  ('Brent Crude Oil',                'energy', 2, 1000,  'USD', 'USD', 100, 4,  -5.0, -3.0, 0),
    'BTCUSD': ('Bitcoin vs US Dollar',           'crypto', 2, 1, 'BTC', 'USD', 20, 2500, -2500, -1500, 0),
    'ETHUSD': ('Ethereum vs US Dollar',          'crypto', 2, 1, 'ETH', 'USD', 20, 200,  -150,  -100, 0),
    'SOLUSD': ('Solana vs US Dollar',            'crypto', 2, 1, 'SOL', 'USD', 20, 10,   -8,    -5, 0),
    'BNBUSD': ('BNB vs US Dollar',               'crypto', 2, 1, 'BNB', 'USD', 20, 60,   -40,   -25, 0),
    'XRPUSD': ('XRP vs US Dollar',               'crypto', 4, 1, 'XRP', 'USD', 20, 10,   -4,    -2, 0),
    'ADAUSD': ('Cardano vs US Dollar',           'crypto', 4, 1, 'ADA', 'USD', 20, 10,   -2,    -1, 0),
    'DOGEUSD': ('Dogecoin vs US Dollar',         'crypto', 5, 1, 'DOGE', 'USD', 20, 20,  -4,    -2, 0),
    'DOTUSD': ('Polkadot vs US Dollar',          'crypto', 3, 1, 'DOT', 'USD', 20, 10,   -4,    -2, 0),
    'LINKUSD': ('Chainlink vs US Dollar',        'crypto', 3, 1, 'LINK', 'USD', 20, 15,  -6,    -3, 0),
    'R_10':   ('Volatility 10 Index',            'synthetic', 3, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    'R_25':   ('Volatility 25 Index',            'synthetic', 3, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    'R_50':   ('Volatility 50 Index',            'synthetic', 4, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    'R_75':   ('Volatility 75 Index',            'synthetic', 4, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    'R_100':  ('Volatility 100 Index',           'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    '1HZ10V': ('Volatility 10 (1s) Index',       'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    '1HZ25V': ('Volatility 25 (1s) Index',       'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    '1HZ50V': ('Volatility 50 (1s) Index',       'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    '1HZ75V': ('Volatility 75 (1s) Index',       'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
    '1HZ100V': ('Volatility 100 (1s) Index',     'synthetic', 2, 1, 'USD', 'USD', 200, 0, 0, 0, 0),
}

# Symbols shown in the terminal's Market Watch, in order.
MARKET_WATCH = [
    'XAUUSD', 'XAGUSD', 'USOIL',
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'NZDUSD', 'USDCHF',
    'EURGBP', 'EURJPY', 'GBPJPY', 'EURCHF', 'GBPCHF',
    'BTCUSD', 'ETHUSD',
]


def _build_specs():
    overrides = getattr(settings, 'MT5_SYMBOL_OVERRIDES', {}) or {}
    commissions = getattr(settings, 'MT5_COMMISSION_PER_LOT', {}) or {}
    specs = {}
    for sym, (desc, kind, digits, cs, base, quote, lev, spread, sl, ss, stops) in _SPECS.items():
        fields = dict(
            symbol=sym, description=desc, kind=kind, digits=digits,
            contract_size=Decimal(str(cs)), base=base, quote=quote, leverage=lev,
            spread=spread, swap_long=Decimal(str(sl)), swap_short=Decimal(str(ss)),
            stops_level=stops,
            commission=Decimal(str(commissions.get(sym, commissions.get(kind, 0)))),
        )
        for k, v in (overrides.get(sym) or {}).items():
            if k in fields:
                fields[k] = Decimal(str(v)) if isinstance(fields[k], Decimal) else v
        specs[sym] = SymbolSpec(**fields)
    return specs


SPECS = _build_specs()


def get_spec(symbol):
    spec = SPECS.get((symbol or '').upper())
    if spec is None:
        raise TradeError(f"Unknown symbol {symbol}")
    return spec


def symbol_specs_json(symbols=None):
    """Spec table for the terminal (single source of truth for digits, spread, contract size …)."""
    return {s: SPECS[s].as_json() for s in (symbols or SPECS) if s in SPECS}


def get_contract_size(symbol):
    spec = SPECS.get((symbol or '').upper())
    return spec.contract_size if spec else Decimal('1')


def get_leverage_for_symbol(symbol):
    spec = SPECS.get((symbol or '').upper())
    return spec.leverage if spec else 100


def ticket_for(trade):
    """Position ticket of a trade (a partial close keeps its parent's ticket)."""
    return TICKET_OFFSET + (trade.parent_id or trade.id)


# ═══════════════════════════════════════════════════════════════
# QUOTES  (bid / ask)
# ═══════════════════════════════════════════════════════════════
@dataclass
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    time: float

    @property
    def mid(self):
        return (self.bid + self.ask) / 2


def _quote_from_mid(spec, mid, ts):
    point = spec.point
    mid = Decimal(str(mid))
    half = (Decimal(spec.spread) * point) / 2
    bid = (mid - half).quantize(point, rounding=ROUND_DOWN)
    ask = bid + Decimal(spec.spread) * point
    return Quote(spec.symbol, bid, ask, ts)


def _deriv_mid(symbol):
    """Latest tick for a Deriv synthetic index (blocking, cached for 1s)."""
    entry = price_feed.get_price_entry(symbol)
    if entry and time.time() - entry['ts'] < 1.0:
        return entry['price']
    import websocket   # websocket-client (already used by price_tracker)
    ws = websocket.create_connection(DERIV_WS_URL, timeout=5)
    try:
        ws.send(json.dumps({'ticks_history': symbol, 'count': 1, 'end': 'latest', 'style': 'ticks'}))
        msg = json.loads(ws.recv())
        price = float(msg['history']['prices'][-1])
    finally:
        ws.close()
    price_feed.put_price(symbol, price)
    return price


def live_quote(symbol, max_age=None):
    """
    Fresh quote for executing a trade. Fetches upstream when the cache is old.
    Returns None when there is no fresh price ("No prices" / "Off quotes" in MT5).
    """
    spec = get_spec(symbol)
    max_age = QUOTE_MAX_AGE if max_age is None else max_age
    try:
        if spec.kind == 'synthetic':
            _deriv_mid(spec.symbol)
        else:
            price_feed.get_single_price_preferred(spec.symbol)
    except Exception as e:
        logger.warning(f"live_quote fetch failed for {symbol}: {e}")
    entry = price_feed.get_price_entry(spec.symbol)
    if not entry or entry['price'] <= 0 or time.time() - entry['ts'] > max_age:
        return None
    return _quote_from_mid(spec, entry['price'], entry['ts'])


def cached_quote(symbol):
    """Last known quote from the cache — never blocks. For display, P&L and the order engine."""
    spec = SPECS.get((symbol or '').upper())
    if not spec:
        return None
    entry = price_feed.get_price_entry(spec.symbol)
    if entry and entry['price'] > 0:
        return _quote_from_mid(spec, entry['price'], entry['ts'])
    snap = price_feed.get_cached_prices().get(spec.symbol)
    if snap and float(snap) > 0:
        return _quote_from_mid(spec, snap, 0)
    return None


def is_fresh(quote, max_age=None):
    max_age = QUOTE_MAX_AGE if max_age is None else max_age
    return quote is not None and (time.time() - quote.time) <= max_age


def quotes_payload(symbols):
    """{symbol: {bid, ask, time}} for the terminal's Market Watch push."""
    out = {}
    for sym in symbols:
        q = cached_quote(sym)
        if q:
            spec = SPECS[sym.upper()]
            out[sym] = {'bid': float(q.bid), 'ask': float(q.ask), 'time': int(q.time * 1000), 'digits': spec.digits}
            day = price_feed.get_day_stats(spec.symbol)
            if day:     # Market Watch shows the day's range and change on the bid
                out[sym].update({'open': float(_quote_from_mid(spec, day['open'], 0).bid),
                                 'low': float(_quote_from_mid(spec, day['low'], 0).bid),
                                 'high': float(_quote_from_mid(spec, day['high'], 0).bid)})
    return out


# ═══════════════════════════════════════════════════════════════
# CURRENCY CONVERSION, PROFIT, MARGIN, SWAP
# ═══════════════════════════════════════════════════════════════
_USD_PAIRS = {   # currency -> (pair, True if the pair quotes USD per unit of the currency)
    'EUR': ('EURUSD', True), 'GBP': ('GBPUSD', True), 'AUD': ('AUDUSD', True),
    'NZD': ('NZDUSD', True), 'XAU': ('XAUUSD', True), 'XAG': ('XAGUSD', True),
    'BTC': ('BTCUSD', True), 'ETH': ('ETHUSD', True),
    'JPY': ('USDJPY', False), 'CHF': ('USDCHF', False), 'CAD': ('USDCAD', False),
}


def usd_rate(currency):
    """How many USD one unit of `currency` is worth right now."""
    if currency == 'USD':
        return Decimal('1')
    pair, direct = _USD_PAIRS.get(currency, (None, True))
    if not pair:
        raise TradeError(f"No conversion rate for {currency}")
    q = cached_quote(pair)
    if q is None or not is_fresh(q, max_age=600):
        q = live_quote(pair, max_age=600) or q
    if q is None:
        raise TradeError(f"No prices for {pair} (needed to convert {currency} to USD)")
    mid = q.mid
    return mid if direct else (Decimal('1') / mid)


def to_usd(amount, currency):
    return Decimal(amount) * usd_rate(currency)


def calc_profit(symbol, direction, volume, open_price, close_price):
    """Profit in USD (unrounded). BUY closes at bid, SELL at ask — the caller passes the right price."""
    spec = get_spec(symbol)
    open_price, close_price = Decimal(str(open_price)), Decimal(str(close_price))
    diff = (close_price - open_price) if direction == 'BUY' else (open_price - close_price)
    return to_usd(diff * spec.contract_size * Decimal(str(volume)), spec.quote)


def calc_margin(symbol, volume, price=None):
    """
    MT5 margin in USD.
      Forex:  lots × contract size / leverage, in the base currency
      CFD:    lots × contract size × price / leverage
    """
    spec = SPECS.get((symbol or '').upper())
    volume = Decimal(str(volume or 0))
    if spec is None:
        return q2(volume)
    lev = Decimal(spec.leverage)
    if spec.kind == 'forex':
        return q2(to_usd(spec.contract_size * volume / lev, spec.base))
    if price is None:
        q = cached_quote(spec.symbol)
        price = q.mid if q else Decimal('0')
    return q2(to_usd(spec.contract_size * volume * Decimal(str(price)) / lev, spec.quote))


def commission_per_side(symbol, volume):
    """Commission charged on one deal (negative = charged to the trader)."""
    spec = get_spec(symbol)
    return -q2(spec.commission * Decimal(str(volume)))


def swap_per_day(symbol, direction, volume):
    spec = get_spec(symbol)
    points = spec.swap_long if direction == 'BUY' else spec.swap_short
    return to_usd(points * spec.point * spec.contract_size * Decimal(str(volume)), spec.quote)


def _rollovers(after, until):
    """17:00 New York rollover instants in (after, until]."""
    day = after.astimezone(NY).date()
    while True:
        r = datetime(day.year, day.month, day.day, 17, 0, tzinfo=NY)
        if r > until:
            return
        if r > after:
            yield r
        day += timedelta(days=1)


def _swap_multiplier(kind, rollover):
    if kind == 'synthetic':
        return 0
    if kind == 'crypto':
        return 1
    wd = rollover.weekday()           # Mon=0 … Sun=6 (New York time)
    if wd >= 5:
        return 0
    return 3 if wd == 2 else 1       # triple swap on Wednesday covers the weekend


def close_price_for(direction, quote):
    """A BUY position closes at the bid, a SELL position at the ask."""
    return quote.bid if direction == 'BUY' else quote.ask


# ═══════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════
def _market_open(symbol):
    try:
        from .views import is_market_open
        ok, reason, hint = is_market_open(symbol)
        return bool(ok), reason + (f". {hint}." if hint else '')
    except Exception:
        return True, ''


def require_market_open(symbol):
    ok, msg = _market_open(symbol)
    if not ok:
        raise TradeError(f"Market closed. {msg}".strip())


def parse_volume(spec, raw):
    try:
        volume = Decimal(str(raw))
    except Exception:
        raise TradeError("Invalid volume")
    if volume < spec.volume_min or volume > spec.volume_max:
        raise TradeError(f"Invalid volume: must be between {spec.volume_min} and {spec.volume_max}")
    if (volume / spec.volume_step) % 1 != 0:
        raise TradeError(f"Invalid volume: step is {spec.volume_step}")
    return volume.quantize(spec.volume_step)


def parse_price(spec, raw, name):
    if raw is None or str(raw).strip() in ('', '0', 'null', 'None'):
        return None
    try:
        p = Decimal(str(raw))
    except Exception:
        raise TradeError(f"Invalid {name}")
    if p <= 0:
        raise TradeError(f"Invalid {name}")
    return p.quantize(spec.point, rounding=ROUND_HALF_UP)


def check_stops(spec, direction, sl, tp, ref_price, against_market=True):
    """
    MT5 stop rules. For a position the reference is the price it closes at
    (bid for BUY, ask for SELL); for a pending order it is the order price.
    S/L and T/P must sit at least stops_level points away.
    """
    gap = Decimal(spec.stops_level) * spec.point
    where = 'the current price' if against_market else 'the order price'
    if direction == 'BUY':
        if sl is not None and not sl < ref_price - gap:
            raise TradeError(f"Invalid S/L: for a BUY it must be below {where} ({ref_price})")
        if tp is not None and not tp > ref_price + gap:
            raise TradeError(f"Invalid T/P: for a BUY it must be above {where} ({ref_price})")
    else:
        if sl is not None and not sl > ref_price + gap:
            raise TradeError(f"Invalid S/L: for a SELL it must be above {where} ({ref_price})")
        if tp is not None and not tp < ref_price - gap:
            raise TradeError(f"Invalid T/P: for a SELL it must be below {where} ({ref_price})")


# ═══════════════════════════════════════════════════════════════
# ACCOUNT METRICS
# ═══════════════════════════════════════════════════════════════
def _trading_account(user, lock=False):
    from .models import Account
    acc, _ = Account.objects.get_or_create(user=user, account_type='TRADING', defaults={'balance': ZERO})
    if lock:
        acc = Account.objects.select_for_update().get(pk=acc.pk)
    return acc


def position_snapshot(trade):
    """Live figures for one open position, from cached quotes."""
    symbol = trade.asset.symbol if trade.asset else ''
    spec = SPECS.get(symbol)
    entry = Decimal(str(trade.entry_price or 0))
    volume = Decimal(str(trade.stake or 0))
    quote = cached_quote(symbol) if spec else None
    if quote:
        current = close_price_for(trade.direction, quote)
    else:
        current = Decimal(str(trade.current_price or entry))
    try:
        profit = q2(calc_profit(symbol, trade.direction, volume, entry, current)) if spec else q2(trade.pnl or 0)
    except TradeError:
        profit = q2(trade.pnl or 0)
    try:
        margin = calc_margin(symbol, volume, quote.mid if quote else entry) if spec else q2(trade.margin_used or 0)
    except TradeError:
        margin = q2(trade.margin_used or 0)
    return {
        'symbol': symbol, 'spec': spec, 'quote': quote, 'current': current,
        'profit': profit, 'swap': q2(trade.swap or 0), 'commission': q2(trade.commission or 0),
        'margin': margin,
    }


def get_metrics(user):
    """
    MT5 account figures:
        balance       cash in the TRADING account
        floating_pnl  Σ profit + swap of open positions
        equity        balance + floating
        margin_used   Σ margin of open positions at current prices
        free_margin   equity − margin
        margin_level  equity / margin × 100   (None with no positions)
    """
    from .models import Trade

    balance = q2(_trading_account(user).balance)
    open_trades = list(Trade.objects.filter(user=user, status='OPEN').select_related('asset'))

    profit = swap = margin = ZERO
    for t in open_trades:
        s = position_snapshot(t)
        profit += s['profit']
        swap += s['swap']
        margin += s['margin']

    floating = q2(profit + swap)
    equity = q2(balance + floating)
    margin = q2(margin)
    free_margin = q2(equity - margin)
    margin_level = q2(equity / margin * 100) if margin > 0 else None

    return {
        'balance': balance,
        'profit': q2(profit),
        'swap': q2(swap),
        'floating_pnl': floating,
        'equity': equity,
        'margin_used': margin,
        'free_margin': free_margin,
        'margin_level': margin_level,
        'margin_call_level': MARGIN_CALL_LEVEL,
        'stop_out_level': STOP_OUT_LEVEL,
        'margin_call': margin_level is not None and margin_level < MARGIN_CALL_LEVEL,
        'position_count': len(open_trades),
        'can_open_new': free_margin > 0,
    }


# ═══════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════
def _broadcast(event, data, user_id):
    def send():
        try:
            from .consumers import broadcast_trade_event
            broadcast_trade_event(event, data, user_id)
        except Exception:
            pass
    transaction.on_commit(send)


def _notify(user, title, message, priority='info'):
    try:
        from .notifications import notify
        notify(user, title=title, message=message, category='TRADE', priority=priority,
               action_url='/terminal/', action_label='Open Terminal')
    except Exception:
        pass


REASON_LABELS = {'CLIENT': 'Client', 'SL': 'Stop Loss', 'TP': 'Take Profit', 'SO': 'Stop Out',
                 'CLOSE_BY': 'Close By', 'EXPERT': 'Expert'}


# ═══════════════════════════════════════════════════════════════
# MARKET ORDERS
# ═══════════════════════════════════════════════════════════════
def _create_position(user, spec, direction, volume, price, sl, tp, account, comment=''):
    """Open a position at `price`. Caller holds the account lock and has checked margin."""
    from .models import Asset, Trade, Transaction

    asset, _ = Asset.objects.get_or_create(
        symbol=spec.symbol,
        defaults={'name': spec.description, 'asset_type': 'crypto' if spec.kind == 'crypto' else
                  'synthetic' if spec.kind == 'synthetic' else 'forex', 'price': price},
    )
    margin = calc_margin(spec.symbol, volume, price)
    commission = commission_per_side(spec.symbol, volume)

    trade = Trade.objects.create(
        user=user, asset=asset, direction=direction, stake=volume,
        entry_price=price, current_price=price, target_digit=None, status='OPEN',
        payout=ZERO, pnl=ZERO, stop_loss=sl, take_profit=tp,
        margin_used=margin, leverage=spec.leverage, contract_size=spec.contract_size,
        commission=commission, swap=ZERO, swap_charged_at=timezone.now(),
    )

    if commission:
        account.balance += commission
        account.save(update_fields=['balance', 'updated_at'])
        Transaction.objects.create(
            user=user, amount=abs(commission), transaction_type='TRADE_COMMISSION',
            details=f"Commission #{ticket_for(trade)} {direction} {volume} {spec.symbol}",
        )

    _broadcast('trade_opened', {
        'id': trade.id, 'ticket': ticket_for(trade), 'symbol': spec.symbol, 'direction': direction,
        'volume': float(volume), 'entry': float(price), 'sl': float(sl) if sl else None,
        'tp': float(tp) if tp else None, 'margin_used': float(margin), 'comment': comment,
    }, user.id)
    return trade


def open_position(user, symbol, direction, volume, sl=None, tp=None):
    """Market Execution: fills at the server's ask (BUY) or bid (SELL)."""
    direction = (direction or '').upper()
    if direction not in ('BUY', 'SELL'):
        raise TradeError("Invalid order type")
    spec = get_spec(symbol)
    volume = parse_volume(spec, volume)
    sl = parse_price(spec, sl, 'S/L')
    tp = parse_price(spec, tp, 'T/P')
    require_market_open(spec.symbol)

    quote = live_quote(spec.symbol)
    if quote is None:
        raise TradeError("No prices")
    price = quote.ask if direction == 'BUY' else quote.bid
    check_stops(spec, direction, sl, tp, close_price_for(direction, quote))

    with transaction.atomic():
        account = _trading_account(user, lock=True)
        margin = calc_margin(spec.symbol, volume, price)
        commission = commission_per_side(spec.symbol, volume)
        free = get_metrics(user)['free_margin']
        if free < margin - commission:
            raise TradeError(f"Not enough money. Required margin {margin:.2f}, free margin {free:.2f}")
        trade = _create_position(user, spec, direction, volume, price, sl, tp, account)

    _notify(user, f"Position opened: {direction.lower()} {volume} {spec.symbol}",
            f"#{ticket_for(trade)} at {price} · margin {margin:.2f} USD")
    return trade


def _realize(trade, volume, exit_price, reason, account, charge_commission=True):
    """
    Close `volume` of a locked open position at `exit_price`.
    Partial close books the closed part as its own Trade row linked to the
    parent (same position ticket); the parent keeps the remaining volume.
    Balance += profit + swap + exit commission, never below 0.
    Returns (closed_row, net).
    """
    from .models import Trade, Transaction

    symbol = trade.asset.symbol if trade.asset else 'UNKNOWN'
    total = Decimal(str(trade.stake))
    volume = min(Decimal(str(volume)), total)
    full = volume >= total
    ratio = volume / total
    entry = Decimal(str(trade.entry_price or 0))
    exit_price = Decimal(str(exit_price))

    profit = q2(calc_profit(symbol, trade.direction, volume, entry, exit_price))
    swap_part = q2(trade.swap or 0) if full else q2((trade.swap or 0) * ratio)
    comm_in_part = q2(trade.commission or 0) if full else q2((trade.commission or 0) * ratio)
    comm_out = commission_per_side(symbol, volume) if (charge_commission and symbol in SPECS) else ZERO
    net = profit + swap_part + comm_out

    if net < 0 and account.balance + net < 0:          # negative balance protection
        net = -max(account.balance, ZERO)
        profit = net - swap_part - comm_out

    status = 'WON' if net >= 0 else 'LOST'
    now = timezone.now()

    if full:
        closed = trade
        trade.status = status
        trade.exit_price = exit_price
        trade.current_price = exit_price
        trade.pnl = profit
        trade.close_commission = comm_out
        trade.payout = (volume + net) if status == 'WON' else ZERO
        trade.date_closed = now
        trade.margin_used = ZERO
        trade.close_reason = reason
        trade.save()
    else:
        closed = Trade.objects.create(
            user=trade.user, asset=trade.asset, parent=trade, direction=trade.direction,
            stake=volume, entry_price=entry, exit_price=exit_price, current_price=exit_price,
            target_digit=None, status=status, pnl=profit,
            payout=(volume + net) if status == 'WON' else ZERO,
            stop_loss=trade.stop_loss, take_profit=trade.take_profit,
            margin_used=ZERO, leverage=trade.leverage, contract_size=trade.contract_size,
            swap=swap_part, commission=comm_in_part, close_commission=comm_out,
            date_closed=now, close_reason=reason,
        )
        Trade.objects.filter(pk=closed.pk).update(date_opened=trade.date_opened)
        trade.stake = total - volume
        trade.swap = q2((trade.swap or 0) - swap_part)
        trade.commission = q2((trade.commission or 0) - comm_in_part)
        trade.margin_used = q2(Decimal(str(trade.margin_used or 0)) * (1 - ratio))
        trade.save()

    account.balance += net
    account.save(update_fields=['balance', 'updated_at'])

    label = REASON_LABELS.get(reason, reason)
    Transaction.objects.create(
        user=trade.user, amount=abs(net),
        transaction_type='TRADE_PAYOUT' if status == 'WON' else 'TRADE_LOSS',
        details=(f"[{label}] {'Partial close ' if not full else ''}#{ticket_for(trade)} "
                 f"{trade.direction} {volume} {symbol} @ {exit_price} | "
                 f"{'+' if net >= 0 else '-'}${abs(net):.2f}"),
    )
    _broadcast('trade_closed', {
        'id': trade.id, 'ticket': ticket_for(trade), 'symbol': symbol, 'direction': trade.direction,
        'volume': float(volume), 'remaining': float(0 if full else total - volume),
        'price': float(exit_price), 'pnl': float(net), 'profit': float(profit),
        'status': status, 'reason': reason,
    }, trade.user_id)
    return closed, net


def close_position(user, trade_id, volume=None, reason='CLIENT'):
    """Close (or partially close) a position at market."""
    from .models import Trade

    with transaction.atomic():
        try:
            trade = Trade.objects.select_for_update().select_related('asset').get(id=trade_id, user=user)
        except Trade.DoesNotExist:
            raise TradeError("Position not found")
        if trade.status != 'OPEN':
            raise TradeError("Position already closed")
        symbol = trade.asset.symbol if trade.asset else ''
        spec = get_spec(symbol)
        total = Decimal(str(trade.stake))
        vol = total if volume in (None, '') else parse_volume(spec, volume)
        if vol > total:
            raise TradeError(f"Invalid volume: position volume is {total}")
        if vol < total and total - vol < spec.volume_min:
            raise TradeError(f"Invalid volume: remaining volume would be below {spec.volume_min}")
        require_market_open(symbol)
        quote = live_quote(symbol)
        if quote is None:
            raise TradeError("No prices")
        account = _trading_account(user, lock=True)
        closed, net = _realize(trade, vol, close_price_for(trade.direction, quote), reason, account)
        remaining = Decimal('0') if vol >= total else total - vol
        return {'trade': trade, 'closed': closed, 'net': net, 'volume': vol,
                'remaining': remaining, 'price': close_price_for(trade.direction, quote),
                'balance': account.balance}


def close_by(user, trade_id, by_id):
    """
    Close By: close a position with an opposite one on the same symbol.
    The common volume is closed without a second spread — total profit is
    (sell open − buy open) × volume; the larger position keeps the rest.
    """
    from .models import Trade

    with transaction.atomic():
        rows = {t.id: t for t in Trade.objects.select_for_update().select_related('asset')
                .filter(user=user, id__in=[trade_id, by_id], status='OPEN')}
        a, b = rows.get(int(trade_id)), rows.get(int(by_id))
        if not a or not b:
            raise TradeError("Position not found")
        sa = a.asset.symbol if a.asset else ''
        if sa != (b.asset.symbol if b.asset else '') or a.direction == b.direction:
            raise TradeError("Close By needs an opposite position on the same symbol")
        require_market_open(sa)
        buy, sell = (a, b) if a.direction == 'BUY' else (b, a)
        vol = min(Decimal(str(buy.stake)), Decimal(str(sell.stake)))
        account = _trading_account(user, lock=True)
        sell_open = Decimal(str(sell.entry_price))
        _, net1 = _realize(buy, vol, sell_open, 'CLOSE_BY', account, charge_commission=False)
        _, net2 = _realize(sell, vol, sell_open, 'CLOSE_BY', account, charge_commission=False)
        return {'volume': vol, 'net': net1 + net2, 'balance': account.balance}


def modify_position(user, trade_id, sl='keep', tp='keep', trailing='keep'):
    """Change S/L, T/P (None removes) and the trailing stop distance in points (0/None = off)."""
    from .models import Trade

    with transaction.atomic():
        try:
            trade = Trade.objects.select_for_update().select_related('asset').get(id=trade_id, user=user)
        except Trade.DoesNotExist:
            raise TradeError("Position not found")
        if trade.status != 'OPEN':
            raise TradeError("Position already closed")
        spec = get_spec(trade.asset.symbol if trade.asset else '')
        new_sl = trade.stop_loss if sl == 'keep' else parse_price(spec, sl, 'S/L')
        new_tp = trade.take_profit if tp == 'keep' else parse_price(spec, tp, 'T/P')
        if sl != 'keep' or tp != 'keep':
            quote = live_quote(spec.symbol) or cached_quote(spec.symbol)
            if quote is None:
                raise TradeError("No prices")
            check_stops(spec, trade.direction,
                        new_sl if sl != 'keep' else None,
                        new_tp if tp != 'keep' else None,
                        close_price_for(trade.direction, quote))
        trade.stop_loss = new_sl
        trade.take_profit = new_tp
        if trailing != 'keep':
            pts = int(trailing or 0)
            if pts and pts < max(spec.stops_level, 1):
                raise TradeError(f"Invalid trailing stop: minimum is {max(spec.stops_level, 1)} points")
            trade.trailing_points = pts or None
        trade.save(update_fields=['stop_loss', 'take_profit', 'trailing_points'])
        return trade


# ═══════════════════════════════════════════════════════════════
# PENDING ORDERS
# ═══════════════════════════════════════════════════════════════
PENDING_TYPES = ('BUY_LIMIT', 'SELL_LIMIT', 'BUY_STOP', 'SELL_STOP', 'BUY_STOP_LIMIT', 'SELL_STOP_LIMIT')


def next_rollover(now=None):
    """End of the current trading day (17:00 New York) — expiry of 'Today' orders."""
    now = now or timezone.now()
    return next(_rollovers(now, now + timedelta(days=2)))


def _validate_pending(spec, otype, price, stoplimit, sl, tp, quote):
    gap = Decimal(spec.stops_level) * spec.point
    bid, ask = quote.bid, quote.ask
    if otype == 'BUY_LIMIT' and not price < ask - gap:
        raise TradeError(f"Invalid price: a Buy Limit must be below the ask ({ask})")
    if otype == 'SELL_LIMIT' and not price > bid + gap:
        raise TradeError(f"Invalid price: a Sell Limit must be above the bid ({bid})")
    if otype in ('BUY_STOP', 'BUY_STOP_LIMIT') and not price > ask + gap:
        raise TradeError(f"Invalid price: a Buy Stop must be above the ask ({ask})")
    if otype in ('SELL_STOP', 'SELL_STOP_LIMIT') and not price < bid - gap:
        raise TradeError(f"Invalid price: a Sell Stop must be below the bid ({bid})")
    if otype.endswith('STOP_LIMIT'):
        if stoplimit is None:
            raise TradeError("Set the Stop Limit price")
        if otype == 'BUY_STOP_LIMIT' and not stoplimit < price:
            raise TradeError("Invalid Stop Limit price: for a Buy Stop Limit it must be below the stop price")
        if otype == 'SELL_STOP_LIMIT' and not stoplimit > price:
            raise TradeError("Invalid Stop Limit price: for a Sell Stop Limit it must be above the stop price")
    exec_price = stoplimit if otype.endswith('STOP_LIMIT') else price
    check_stops(spec, 'BUY' if otype.startswith('BUY') else 'SELL', sl, tp, exec_price, against_market=False)


def _parse_expiration(exp_type, expires_at):
    exp_type = (exp_type or 'GTC').upper()
    if exp_type == 'GTC':
        return 'GTC', None
    if exp_type == 'DAY':
        return 'DAY', next_rollover()
    if exp_type == 'SPECIFIED':
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(str(expires_at or ''))
        if dt is None:
            raise TradeError("Invalid expiration")
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        if dt <= timezone.now():
            raise TradeError("Invalid expiration: must be in the future")
        return 'SPECIFIED', dt
    raise TradeError("Invalid expiration")


def place_pending(user, symbol, otype, volume, price, stoplimit=None, sl=None, tp=None,
                  expiration='GTC', expires_at=None):
    from .models import PendingOrder

    otype = (otype or '').upper()
    if otype not in PENDING_TYPES:
        raise TradeError("Invalid order type")
    spec = get_spec(symbol)
    volume = parse_volume(spec, volume)
    price = parse_price(spec, price, 'price')
    if price is None:
        raise TradeError("Set the order price")
    stoplimit = parse_price(spec, stoplimit, 'Stop Limit price')
    sl = parse_price(spec, sl, 'S/L')
    tp = parse_price(spec, tp, 'T/P')
    exp_type, exp_at = _parse_expiration(expiration, expires_at)
    require_market_open(spec.symbol)

    quote = live_quote(spec.symbol)
    if quote is None:
        raise TradeError("No prices")
    _validate_pending(spec, otype, price, stoplimit, sl, tp, quote)

    if PendingOrder.objects.filter(user=user, status='PENDING').count() >= MAX_PENDING_ORDERS:
        raise TradeError(f"Too many pending orders (max {MAX_PENDING_ORDERS})")
    margin = calc_margin(spec.symbol, volume, stoplimit or price)
    free = get_metrics(user)['free_margin']
    if free < margin:
        raise TradeError(f"Not enough money. Required margin {margin:.2f}, free margin {free:.2f}")

    return PendingOrder.objects.create(
        user=user, symbol=spec.symbol, order_type=otype, volume=volume, price=price,
        stoplimit_price=stoplimit, stop_loss=sl, take_profit=tp,
        expiration_type=exp_type, expires_at=exp_at,
    )


def modify_pending(user, order_id, price, stoplimit=None, sl=None, tp=None, expiration='GTC', expires_at=None):
    from .models import PendingOrder

    with transaction.atomic():
        try:
            o = PendingOrder.objects.select_for_update().get(id=order_id, user=user)
        except PendingOrder.DoesNotExist:
            raise TradeError("Order not found")
        if o.status != 'PENDING':
            raise TradeError(f"Order is already {o.status.lower()}")
        spec = get_spec(o.symbol)
        price = parse_price(spec, price, 'price')
        if price is None:
            raise TradeError("Set the order price")
        stoplimit = parse_price(spec, stoplimit, 'Stop Limit price')
        sl = parse_price(spec, sl, 'S/L')
        tp = parse_price(spec, tp, 'T/P')
        exp_type, exp_at = _parse_expiration(expiration, expires_at)
        quote = live_quote(spec.symbol)
        if quote is None:
            raise TradeError("No prices")
        _validate_pending(spec, o.order_type, price, stoplimit, sl, tp, quote)
        o.price, o.stoplimit_price, o.stop_loss, o.take_profit = price, stoplimit, sl, tp
        o.expiration_type, o.expires_at = exp_type, exp_at
        o.save()
        return o


def cancel_pending(user, order_id):
    from .models import PendingOrder

    with transaction.atomic():
        try:
            o = PendingOrder.objects.select_for_update().get(id=order_id, user=user)
        except PendingOrder.DoesNotExist:
            raise TradeError("Order not found")
        if o.status != 'PENDING':
            raise TradeError(f"Order is already {o.status.lower()}")
        o.status = 'CANCELLED'
        o.reason = 'Cancelled by client'
        o.save(update_fields=['status', 'reason'])
        return o


def _fill_order(order_id, fill_price):
    """Turn a triggered pending order into a position (or reject it for lack of margin)."""
    from .models import PendingOrder

    with transaction.atomic():
        o = PendingOrder.objects.select_for_update().get(id=order_id)
        if o.status != 'PENDING':
            return None
        spec = get_spec(o.symbol)
        fill = Decimal(str(fill_price))
        direction = 'BUY' if o.order_type.startswith('BUY') else 'SELL'
        account = _trading_account(o.user, lock=True)
        margin = calc_margin(spec.symbol, o.volume, fill)
        if get_metrics(o.user)['free_margin'] < margin:
            o.status = 'REJECTED'
            o.reason = 'Not enough money'
            o.save(update_fields=['status', 'reason'])
            _notify(o.user, f"Order rejected: {o.get_order_type_display()} {o.volume} {o.symbol}",
                    "Not enough free margin when the price was reached.", 'warning')
            return None
        trade = _create_position(o.user, spec, direction, Decimal(str(o.volume)), fill,
                                 o.stop_loss, o.take_profit, account,
                                 comment=o.get_order_type_display())
        o.status = 'FILLED'
        o.trade = trade
        o.filled_at = timezone.now()
        o.save(update_fields=['status', 'trade', 'filled_at'])
    _notify(o.user, f"Order filled: {o.get_order_type_display()} {o.volume} {o.symbol}",
            f"Position #{ticket_for(trade)} opened at {fill}")
    return trade.id


def process_pending_orders(user=None):
    """Expire, activate (stop limit) and fill pending orders whose price has been reached."""
    from .models import PendingOrder

    now = timezone.now()
    qs = PendingOrder.objects.filter(status='PENDING')
    if user is not None:
        qs = qs.filter(user=user)
    qs.filter(expires_at__isnull=False, expires_at__lte=now).update(status='EXPIRED', reason='Expired')

    filled = []
    for o in qs.filter(status='PENDING'):
        quote = cached_quote(o.symbol)
        if not is_fresh(quote) or not _market_open(o.symbol)[0]:
            continue
        p = Decimal(str(o.price))
        t = o.order_type
        fill = None
        if t == 'BUY_LIMIT' and quote.ask <= p:
            fill = quote.ask
        elif t == 'SELL_LIMIT' and quote.bid >= p:
            fill = quote.bid
        elif t == 'BUY_STOP' and quote.ask >= p:
            fill = quote.ask
        elif t == 'SELL_STOP' and quote.bid <= p:
            fill = quote.bid
        elif t in ('BUY_STOP_LIMIT', 'SELL_STOP_LIMIT'):
            hit = quote.ask >= p if t == 'BUY_STOP_LIMIT' else quote.bid <= p
            if hit:   # stop reached → becomes a limit order at the stop limit price
                PendingOrder.objects.filter(pk=o.pk, status='PENDING').update(
                    order_type='BUY_LIMIT' if t == 'BUY_STOP_LIMIT' else 'SELL_LIMIT',
                    price=o.stoplimit_price, stoplimit_price=None)
            continue
        if fill is not None:
            try:
                tid = _fill_order(o.id, fill)
                if tid:
                    filled.append(tid)
            except Exception as e:
                logger.warning(f"Pending order {o.id} fill failed: {e}")
    return filled


# ═══════════════════════════════════════════════════════════════
# S/L, T/P, TRAILING STOP, SWAP, STOP OUT
# ═══════════════════════════════════════════════════════════════
def _close_by_server(trade_id, price, reason):
    from .models import Trade

    with transaction.atomic():
        trade = Trade.objects.select_for_update().select_related('asset').get(id=trade_id)
        if trade.status != 'OPEN':
            return None
        account = _trading_account(trade.user, lock=True)
        closed, net = _realize(trade, trade.stake, price, reason, account)
    label = REASON_LABELS.get(reason, reason)
    _notify(trade.user, f"{label}: {trade.direction.lower()} {closed.stake} {trade.asset.symbol}",
            f"#{ticket_for(trade)} closed at {price} · {'+' if net >= 0 else '-'}{abs(net):.2f} USD",
            'success' if net >= 0 else 'warning')
    return trade.id


def process_sl_tp(user=None):
    """Trail stops, then close every position whose S/L or T/P has been reached."""
    from .models import Trade

    qs = Trade.objects.filter(status='OPEN', direction__in=('BUY', 'SELL')).select_related('asset')
    if user is not None:
        qs = qs.filter(user=user)

    closed = []
    for t in qs:
        sym = t.asset.symbol if t.asset else None
        spec = SPECS.get(sym or '')
        if not spec or not (t.stop_loss or t.take_profit or t.trailing_points):
            continue
        quote = cached_quote(sym)
        if not is_fresh(quote) or not _market_open(sym)[0]:
            continue
        ref = close_price_for(t.direction, quote)
        entry = Decimal(str(t.entry_price))

        if t.trailing_points:
            dist = Decimal(t.trailing_points) * spec.point
            if t.direction == 'BUY' and ref - entry >= dist:
                new_sl = ref - dist
                if t.stop_loss is None or new_sl > t.stop_loss:
                    Trade.objects.filter(pk=t.pk, status='OPEN').update(stop_loss=new_sl)
                    t.stop_loss = new_sl
            elif t.direction == 'SELL' and entry - ref >= dist:
                new_sl = ref + dist
                if t.stop_loss is None or new_sl < t.stop_loss:
                    Trade.objects.filter(pk=t.pk, status='OPEN').update(stop_loss=new_sl)
                    t.stop_loss = new_sl

        hit = None
        if t.direction == 'BUY':
            if t.stop_loss and ref <= t.stop_loss:
                hit = 'SL'
            elif t.take_profit and ref >= t.take_profit:
                hit = 'TP'
        else:
            if t.stop_loss and ref >= t.stop_loss:
                hit = 'SL'
            elif t.take_profit and ref <= t.take_profit:
                hit = 'TP'
        if hit:
            try:
                if _close_by_server(t.id, ref, hit):
                    closed.append(t.id)
            except Exception as e:
                logger.warning(f"{hit} close failed for trade {t.id}: {e}")
    return closed


def charge_swaps(user=None):
    """Add the daily swap to open positions for every 17:00 New York rollover they were held over."""
    from .models import Trade

    now = timezone.now()
    qs = Trade.objects.filter(status='OPEN', direction__in=('BUY', 'SELL')).select_related('asset')
    if user is not None:
        qs = qs.filter(user=user)
    for t in qs:
        spec = SPECS.get(t.asset.symbol if t.asset else '')
        if not spec:
            continue
        since = t.swap_charged_at or t.date_opened
        days, last = 0, None
        for r in _rollovers(since, now):
            days += _swap_multiplier(spec.kind, r)
            last = r
        if last is None:
            continue
        try:
            amount = q2(swap_per_day(spec.symbol, t.direction, t.stake) * days) if days else ZERO
        except TradeError:
            continue   # no conversion rate yet — try again next pass
        with transaction.atomic():
            row = Trade.objects.select_for_update().get(pk=t.pk)
            if row.status != 'OPEN' or (row.swap_charged_at and row.swap_charged_at >= last):
                continue
            row.swap = q2((row.swap or 0) + amount)
            row.swap_charged_at = last
            row.save(update_fields=['swap', 'swap_charged_at'])


def check_stop_out(user):
    """
    Margin call at MARGIN_CALL_LEVEL (notification), stop out at STOP_OUT_LEVEL:
    close the most losing position until the margin level recovers.
    """
    from .models import Trade
    from django.core.cache import cache

    closed = []
    for _ in range(50):
        m = get_metrics(user)
        ml = m['margin_level']
        if ml is None or ml >= MARGIN_CALL_LEVEL:
            break
        if ml >= STOP_OUT_LEVEL:
            key = f"mt5:margin_call:{user.id}"
            if not cache.get(key):
                cache.set(key, 1, 3600)
                _notify(user, "Margin call",
                        f"Margin level is {ml}%. Positions are closed automatically below {STOP_OUT_LEVEL}%.",
                        'warning')
            break

        worst, worst_pnl, worst_price = None, None, None
        for t in Trade.objects.filter(user=user, status='OPEN').select_related('asset'):
            s = position_snapshot(t)
            if not is_fresh(s['quote']) or not _market_open(s['symbol'])[0]:
                continue
            total = s['profit'] + s['swap']
            if worst is None or total < worst_pnl:
                worst, worst_pnl, worst_price = t, total, s['current']
        if worst is None:
            break
        if not _close_by_server(worst.id, worst_price, 'SO'):
            break
        closed.append(worst.id)
    return closed


def process_user_orders(user):
    """Run the whole engine for one user (cheap enough to call every couple of seconds)."""
    for fn in (process_pending_orders, charge_swaps, process_sl_tp, check_stop_out):
        try:
            fn(user)
        except Exception as e:
            logger.warning(f"{fn.__name__} failed for {getattr(user, 'id', '?')}: {e}")


def process_all():
    """Run the engine for every user with open positions or pending orders."""
    from .models import PendingOrder, Trade
    from django.contrib.auth import get_user_model

    price_feed.refresh_if_stale(max_age=2.0)
    ids = set(Trade.objects.filter(status='OPEN').values_list('user_id', flat=True))
    ids |= set(PendingOrder.objects.filter(status='PENDING').values_list('user_id', flat=True))
    for u in get_user_model().objects.filter(id__in=ids):
        process_user_orders(u)


# Legacy name kept for older imports.
SPREAD_FRAC = Decimal('0.00002')
