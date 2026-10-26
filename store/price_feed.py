# store/price_feed.py
"""
Unified price feed — real-time for ALL asset classes.

Sources:
  • Crypto   → Binance REST + WS       (no key needed)
  • Metals   → TwelveData REST         (real-time)
  • Energy   → TwelveData REST         (real-time)
  • Forex    → TwelveData REST         (real-time)

Fallback chain:
  Crypto    → Binance → stale cache
  Metals    → TwelveData → OANDA → Deriv → Swissquote → gold-api.com → Yahoo → Stooq → stale cache
  Energy    → TwelveData → OANDA → Yahoo → Stooq → stale cache
  Forex     → TwelveData → OANDA → Deriv → Swissquote → Yahoo → Stooq → stale cache

Swissquote and gold-api.com need no API key and are real time, so gold keeps a price when
TwelveData runs out of credits (the free plan allows 8 a minute; every refresh spends one per
symbol) and Yahoo no longer serves spot gold (XAUUSD=X). Check every source on a server with
`python manage.py shell -c "from store import price_feed; price_feed.diagnose('XAUUSD')"`.

A stale price keeps the time it was really received, so the trade server sees it
as old ("Off quotes") instead of filling orders at a frozen figure.
"""
import json
import time
import logging
import threading
import urllib.request as _urlreq
import urllib.parse as _urlparse
from datetime import datetime, timezone as _tz
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread

logger = logging.getLogger(__name__)

try:
    from django.conf import settings
    TWELVEDATA_API_KEY = getattr(settings, 'TWELVEDATA_API_KEY', '')
    # settings.py names it OANDA_API_TOKEN; OANDA_API_KEY kept for older settings
    OANDA_API_KEY      = getattr(settings, 'OANDA_API_TOKEN', '') or getattr(settings, 'OANDA_API_KEY', '')
    OANDA_ACCOUNT_ID   = getattr(settings, 'OANDA_ACCOUNT_ID', '')
    OANDA_ENV          = getattr(settings, 'OANDA_ENV', 'practice')
except Exception:
    TWELVEDATA_API_KEY = ''
    OANDA_API_KEY = ''
    OANDA_ACCOUNT_ID = ''
    OANDA_ENV = 'practice'


# ═══════════════════════════════════════════════════════════════
# SHARED STATE
# ═══════════════════════════════════════════════════════════════
_CACHE_LOCK = threading.Lock()
_PRICE_CACHE = {}       # key -> {'price': float, 'ts': float}
_LAST_FETCH = {}        # key -> last upstream fetch ts
_CACHE_TTL  = 3.0       # cached value "fresh" for 3s
_MIN_FETCH  = 2.0       # hard floor between upstream fetches

_LAST_SNAPSHOT     = {'ts': 0, 'data': {}}
_REFRESH_IN_FLIGHT = False
_LAST_REFRESH_START = 0.0
# Every full refresh costs one TwelveData credit per forex/metal symbol — keep it bounded.
try:
    _MIN_REFRESH = float(getattr(settings, 'PRICE_REFRESH_SECONDS', 2.0))
except Exception:
    _MIN_REFRESH = 2.0


# ═══════════════════════════════════════════════════════════════
# SYMBOL MAPS
# ═══════════════════════════════════════════════════════════════
BINANCE_MAP = {
    'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT',
    'BNBUSD': 'BNBUSDT', 'XRPUSD': 'XRPUSDT', 'ADAUSD': 'ADAUSDT',
    'DOGEUSD': 'DOGEUSDT', 'DOTUSD': 'DOTUSDT', 'LINKUSD': 'LINKUSDT',
}

TWELVEDATA_MAP = {
    'XAUUSD': 'XAU/USD',
    'XAGUSD': 'XAG/USD',
    'USOIL':  'WTI/USD',
    'UKOIL':  'BRENT/USD',
    'EURUSD': 'EUR/USD',
    'GBPUSD': 'GBP/USD',
    'USDJPY': 'USD/JPY',
    'AUDUSD': 'AUD/USD',
    'USDCAD': 'USD/CAD',
    'NZDUSD': 'NZD/USD',
    'USDCHF': 'USD/CHF',
    'EURGBP': 'EUR/GBP',
    'EURJPY': 'EUR/JPY',
    'GBPJPY': 'GBP/JPY',
    'EURCHF': 'EUR/CHF',
    'GBPCHF': 'GBP/CHF',
}

OANDA_MAP = {
    'XAUUSD': 'XAU_USD',
    'XAGUSD': 'XAG_USD',
    'USOIL':  'WTICO_USD',
    'UKOIL':  'BCO_USD',
    'EURUSD': 'EUR_USD',
    'GBPUSD': 'GBP_USD',
    'USDJPY': 'USD_JPY',
    'AUDUSD': 'AUD_USD',
    'USDCAD': 'USD_CAD',
    'NZDUSD': 'NZD_USD',
    'USDCHF': 'USD_CHF',
    'EURGBP': 'EUR_GBP',
    'EURJPY': 'EUR_JPY',
    'GBPJPY': 'GBP_JPY',
    'EURCHF': 'EUR_CHF',
    'GBPCHF': 'GBP_CHF',
}

YAHOO_MAP = {
    'XAUUSD': 'XAUUSD=X', 'XAGUSD': 'XAGUSD=X',
    'USOIL':  'CL=F',     'UKOIL':  'BZ=F',
    'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'USDJPY=X', 'AUDUSD': 'AUDUSD=X',
    'USDCAD': 'USDCAD=X', 'NZDUSD': 'NZDUSD=X',
    'USDCHF': 'USDCHF=X', 'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X', 'GBPJPY': 'GBPJPY=X',
    'EURCHF': 'EURCHF=X', 'GBPCHF': 'GBPCHF=X',
}

STOOQ_MAP = {
    'XAUUSD=X': 'xauusd', 'XAGUSD=X': 'xagusd',
    'EURUSD=X': 'eurusd', 'GBPUSD=X': 'gbpusd',
    'USDJPY=X': 'usdjpy', 'AUDUSD=X': 'audusd',
    'USDCAD=X': 'usdcad', 'NZDUSD=X': 'nzdusd',
    'USDCHF=X': 'usdchf', 'EURGBP=X': 'eurgbp',
    'EURJPY=X': 'eurjpy', 'GBPJPY=X': 'gbpjpy',
    'EURCHF=X': 'eurchf', 'GBPCHF=X': 'gbpchf',
    'CL=F':    'cl.f',
}

_YAHOO_TO_INTERNAL = {v: k for k, v in YAHOO_MAP.items()}

# Deriv public ticks (no key, real time) — forex and metals
try:
    from django.conf import settings as _settings
    _DERIV_WS_APP_ID = str(getattr(_settings, 'DERIV_WS_APP_ID', '') or '1089')
except Exception:
    _DERIV_WS_APP_ID = '1089'
DERIV_PUBLIC_WS = f"wss://ws.derivws.com/websockets/v3?app_id={_DERIV_WS_APP_ID}"   # settings.DERIV_WS_APP_ID
DERIV_FX_MAP = {
    'XAUUSD': 'frxXAUUSD', 'XAGUSD': 'frxXAGUSD',
    'EURUSD': 'frxEURUSD', 'GBPUSD': 'frxGBPUSD',
    'USDJPY': 'frxUSDJPY', 'AUDUSD': 'frxAUDUSD',
    'USDCAD': 'frxUSDCAD', 'NZDUSD': 'frxNZDUSD',
    'USDCHF': 'frxUSDCHF', 'EURGBP': 'frxEURGBP',
    'EURJPY': 'frxEURJPY', 'GBPJPY': 'frxGBPJPY',
    'EURCHF': 'frxEURCHF', 'GBPCHF': 'frxGBPCHF',
}


class _MiniWS:
    """
    Minimal websocket client (standard library only) used when the websocket-client package isn't
    installed on the server. Text frames only, which is all the Deriv public API needs.
    """

    def __init__(self, url, timeout=8.0):
        import base64, os, socket, ssl
        from urllib.parse import urlsplit
        u = urlsplit(url)
        secure = u.scheme == 'wss'
        port = u.port or (443 if secure else 80)
        path = (u.path or '/') + (f'?{u.query}' if u.query else '')
        # Through the host's HTTPS proxy when one is set (PythonAnywhere free accounts route all traffic
        # via proxy.server:3128); urllib does the same for the REST calls.
        proxy = (os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '') if secure else ''
        if proxy and not self._no_proxy(u.hostname):
            pu = urlsplit(proxy if '://' in proxy else f'http://{proxy}')
            sock = socket.create_connection((pu.hostname, pu.port or 3128), timeout=timeout)
            sock.sendall(f'CONNECT {u.hostname}:{port} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n\r\n'.encode())
            reply = b''
            while b'\r\n\r\n' not in reply:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionError('proxy closed the connection')
                reply += chunk
            status = reply.split(b'\r\n', 1)[0]
            if b' 200' not in status:
                raise ConnectionError(f"proxy refused {u.hostname}: {status.decode(errors='ignore')} "
                                      '(on a free PythonAnywhere account only approved sites are reachable)')
        else:
            sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                      f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: Mozilla/5.0\r\n'
                      f'Origin: https://{u.hostname}\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError('websocket handshake: connection closed')
            head += chunk
        status = head.split(b'\r\n', 1)[0]
        if b' 101 ' not in status + b' ':
            raise ConnectionError(f'websocket handshake refused: {status.decode(errors="ignore")}')
        self.buf = head.split(b'\r\n\r\n', 1)[1]

    @staticmethod
    def _no_proxy(host):
        import os
        skip = [h.strip().lstrip('.') for h in (os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or '').split(',') if h.strip()]
        return any(host == h or host.endswith('.' + h) for h in skip)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('websocket closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        import os, struct
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536 else bytes([0x80 | 127]) + struct.pack('>Q', n))
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode())

    def recv(self):
        import struct
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError('websocket closed by server')
            if opcode == 0x9:
                self._frame(0xA, data)          # answer pings
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b''.join(parts).decode()

    def close(self):
        try:
            self._frame(0x8, b'')
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def ws_connect(url, timeout=8.0):
    """websocket-client when it's installed, else the built-in client (so Deriv works either way)."""
    try:
        import websocket   # websocket-client
        return websocket.create_connection(url, timeout=timeout)
    except ImportError:
        return _MiniWS(url, timeout=timeout)


# Keyless public quotes (real-time bid/ask): Swissquote for forex and metals, gold-api.com for gold and silver
SWISSQUOTE_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{}/{}"
SWISSQUOTE_MAP = {s: (s[:3], s[3:]) for s in DERIV_FX_MAP}          # XAUUSD → XAU/USD, EURUSD → EUR/USD …
GOLDAPI_URL = "https://api.gold-api.com/price/{}"
GOLDAPI_MAP = {'XAUUSD': 'XAU', 'XAGUSD': 'XAG'}
_PUBLIC_CACHE = {}           # symbol -> (price, real ts, fetched at)
_PUBLIC_BACKOFF = {}         # source -> skip until
_PUBLIC_FAILED = {}          # symbol -> when every keyless source last failed
_SOURCE = {}                 # symbol -> (source that gave the last price, when)
_TD_SYMBOL_ERRORS = {}       # TwelveData symbol -> last time its error was logged

_TD_BACKOFF_UNTIL    = 0.0   # TwelveData out of credits / failing → skip it until then
_DERIV_BACKOFF_UNTIL = 0.0
_REAL_TS = {}                # yahoo::TICKER -> time that price was really fetched
_YAHOO_BACKOFF_UNTIL = 0.0   # Yahoo answered 429 → skip it (Stooq still tried) until then


def _is_rate_limited(err):
    """HTTP 429 / quota errors raised by urllib."""
    return getattr(err, 'code', None) == 429 or '429' in str(err) or 'Too Many Requests' in str(err)


def _td_blocked():
    return time.time() < _TD_BACKOFF_UNTIL


def _td_backoff(data, seconds=60):
    """TwelveData answers 429 / 'run out of API credits' — stop asking for a minute."""
    global _TD_BACKOFF_UNTIL
    if isinstance(data, dict) and (data.get('code') in (429, 401, 403) or data.get('status') == 'error'):
        _TD_BACKOFF_UNTIL = time.time() + seconds
        logger.warning(f"TwelveData paused {seconds}s: {data.get('message', '')[:120]}")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════
def _cache_get(key):
    with _CACHE_LOCK:
        return _PRICE_CACHE.get(key)


def _cache_put(key, price, now=None):
    if price and price > 0:
        with _CACHE_LOCK:
            _PRICE_CACHE[key] = {'price': float(price), 'ts': now or time.time()}
        if '::' not in key:
            _day_update(key, float(price), now or time.time())


# ═══════════════════════════════════════════════════════════════
# DAY OPEN / HIGH / LOW  (Market Watch "L:", "H:" and daily change)
# ═══════════════════════════════════════════════════════════════
# The trading day rolls over at 17:00 New York, like forex brokers. Kept per process and
# shared through Django's cache, so a restart mid-day keeps the day's range.
_DAY_STATS = {}
_DAY_SHARED_WRITE = {}


def _trading_day(ts):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    ny = datetime.fromtimestamp(ts, ZoneInfo('America/New_York'))
    return (ny + timedelta(hours=7)).strftime('%Y-%m-%d')


def _day_shared(symbol):
    try:
        from django.core.cache import cache
        return cache.get(f"px:day:{symbol}")
    except Exception:
        return None


def _day_update(symbol, price, ts):
    day = _trading_day(ts)
    st = _DAY_STATS.get(symbol)
    if st is not None and st['day'] > day:
        return                                  # an old tick from yesterday
    if st is None or st['day'] != day:
        shared = _day_shared(symbol)
        st = dict(shared) if shared and shared.get('day') == day else {'day': day, 'open': price, 'high': price, 'low': price}
        _DAY_STATS[symbol] = st
    st['high'] = max(st['high'], price)
    st['low'] = min(st['low'], price)
    if time.time() - _DAY_SHARED_WRITE.get(symbol, 0) > 5:
        _DAY_SHARED_WRITE[symbol] = time.time()
        try:
            from django.core.cache import cache
            shared = cache.get(f"px:day:{symbol}")
            if shared and shared.get('day') == day:      # another process may have seen more of the day
                st['open'] = shared['open']
                st['high'] = max(st['high'], shared['high'])
                st['low'] = min(st['low'], shared['low'])
            cache.set(f"px:day:{symbol}", dict(st), 2 * 86400)
        except Exception:
            pass


def get_day_stats(symbol):
    """{'day', 'open', 'high', 'low'} of the current trading day (mid prices), or None."""
    today = _trading_day(time.time())
    st = _DAY_STATS.get(symbol)
    if st and st['day'] == today:
        return dict(st)
    shared = _day_shared(symbol)
    return dict(shared) if shared and shared.get('day') == today else None


def _should_fetch(key, ttl=None, min_interval=None):
    """Returns (should_fetch, cached_price_or_None). Thread-safe."""
    ttl = ttl or _CACHE_TTL
    min_interval = min_interval or _MIN_FETCH
    now = time.time()
    with _CACHE_LOCK:
        entry = _PRICE_CACHE.get(key)
        if entry and (now - entry['ts']) < ttl:
            return False, entry['price']
        last = _LAST_FETCH.get(key, 0)
        if (now - last) < min_interval:
            return False, entry['price'] if entry else None
        _LAST_FETCH[key] = now
    return True, entry['price'] if entry else None


# ═══════════════════════════════════════════════════════════════
# FETCHERS — REST
# ═══════════════════════════════════════════════════════════════
_CRYPTO_ALT = {}      # internal symbol -> (price, fetched at)


def _fetch_binance(binance_symbol, timeout=2.0):
    """
    Real-time crypto price: Binance, else Coinbase, else Kraken (keyless). Binance refuses US servers
    (HTTP 451): feed_failed() then pauses it for an hour and the other two answer meanwhile.
    """
    if not cooling('Binance'):
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
            req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with _urlreq.urlopen(req, timeout=timeout) as resp:
                return float(json.loads(resp.read())['price'])
        except Exception as e:
            feed_failed('Binance', e, rate_pause=60)
    internal = next((k for k, v in BINANCE_MAP.items() if v == binance_symbol), None)
    return _crypto_alt_price(internal, timeout) if internal else None


def _crypto_alt_price(internal, timeout=3.0):
    hit = _CRYPTO_ALT.get(internal)
    if hit and time.time() - hit[1] < 3:            # several pages ask every 2 s: one fetch serves them
        return hit[0]
    price = None
    if internal in COINBASE_MAP and not cooling('Coinbase'):
        try:
            d = _get_json(f"https://api.exchange.coinbase.com/products/{COINBASE_MAP[internal]}/ticker", timeout)
            price = float(d['price'])
        except Exception as e:
            feed_failed('Coinbase', e, rate_pause=60)
    if not price and internal in KRAKEN_MAP and not cooling('Kraken'):
        try:
            d = _get_json(f"https://api.kraken.com/0/public/Ticker?pair={KRAKEN_MAP[internal]}", timeout)
            res = d.get('result') or {}
            if res:
                price = float(next(iter(res.values()))['c'][0])
        except Exception as e:
            feed_failed('Kraken', e, rate_pause=60)
    if price and price > 0:
        _CRYPTO_ALT[internal] = (price, time.time())
        return price
    return hit[0] if hit else None


def _fetch_twelvedata_single(internal_symbol, timeout=4.0):
    """Single-symbol TwelveData price."""
    if not TWELVEDATA_API_KEY:
        return None
    td_sym = TWELVEDATA_MAP.get(internal_symbol)
    if not td_sym or _td_blocked():
        return None
    try:
        params = _urlparse.urlencode({
            'symbol': td_sym,
            'apikey': TWELVEDATA_API_KEY,
        })
        url = f"https://api.twelvedata.com/price?{params}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if 'price' in data:
            return float(data['price'])
        _td_backoff(data)
    except Exception as e:
        if _is_rate_limited(e):
            _td_backoff({'code': 429, 'message': str(e)})
        logger.debug(f"TwelveData {internal_symbol} failed: {e}")
    return None


def _fetch_twelvedata_batch(internal_symbols, timeout=5.0):
    """Batch fetch — saves API quota."""
    if not TWELVEDATA_API_KEY:
        return {}

    td_symbols = [TWELVEDATA_MAP[s] for s in internal_symbols if s in TWELVEDATA_MAP]
    if not td_symbols or _td_blocked():
        return {}

    try:
        params = _urlparse.urlencode({
            'symbol': ','.join(td_symbols),
            'apikey': TWELVEDATA_API_KEY,
        })
        url = f"https://api.twelvedata.com/price?{params}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if 'price' not in data and _td_backoff(data):
            return {}

        results = {}
        if len(td_symbols) == 1:
            if 'price' in data:
                for internal, td_s in TWELVEDATA_MAP.items():
                    if td_s == td_symbols[0]:
                        results[internal] = float(data['price'])
                        break
        else:
            for internal, td_s in TWELVEDATA_MAP.items():
                if td_s in data and isinstance(data[td_s], dict) and 'price' in data[td_s]:
                    try:
                        results[internal] = float(data[td_s]['price'])
                    except (ValueError, TypeError):
                        pass
                elif td_s in data and isinstance(data[td_s], dict) and time.time() - _TD_SYMBOL_ERRORS.get(td_s, 0) > 600:
                    _TD_SYMBOL_ERRORS[td_s] = time.time()
                    logger.warning(f"TwelveData gave no price for {td_s}: {str(data[td_s].get('message', ''))[:160]}")
        if not results and len(td_symbols) > 1:
            first = next((v for v in data.values() if isinstance(v, dict)), None)
            _td_backoff(first)
        return results
    except Exception as e:
        if _is_rate_limited(e):
            _td_backoff({'code': 429, 'message': str(e)})      # 429 arrives as an HTTP error, not JSON
            return {}
        logger.warning(f"TwelveData batch failed: {e}")
        return {}


def _fetch_oanda_batch(internal_symbols, timeout=5.0):
    """Batch OANDA pricing."""
    if not OANDA_API_KEY:
        return {}

    oanda_syms = [OANDA_MAP[s] for s in internal_symbols if s in OANDA_MAP]
    if not oanda_syms:
        return {}

    base = ('https://api-fxpractice.oanda.com'
            if OANDA_ENV == 'practice'
            else 'https://api-fxtrade.oanda.com')
    url = f"{base}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing?instruments={','.join(oanda_syms)}"

    try:
        req = _urlreq.Request(url, headers={
            'Authorization': f'Bearer {OANDA_API_KEY}',
            'Content-Type': 'application/json',
        })
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())

        results = {}
        for p in data.get('prices', []):
            o_sym = p.get('instrument')
            bids = p.get('bids') or []
            asks = p.get('asks') or []
            if bids and asks:
                bid = float(bids[0]['price'])
                ask = float(asks[0]['price'])
                for internal, mapped in OANDA_MAP.items():
                    if mapped == o_sym:
                        results[internal] = (bid + ask) / 2
                        break
        return results
    except Exception as e:
        logger.warning(f"OANDA batch failed: {e}")
        return {}


def _fetch_yahoo(ticker, timeout=2.5):
    """Yahoo chart — LAST RESORT fallback."""
    global _YAHOO_BACKOFF_UNTIL
    should, cached = _should_fetch(f"yahoo::{ticker}", ttl=_CACHE_TTL, min_interval=_MIN_FETCH)
    if not should:
        return cached

    try:
        if time.time() < _YAHOO_BACKOFF_UNTIL:
            raise RuntimeError('Yahoo paused (rate limited)')
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{ticker}?interval=1m&range=1d")
        headers = {
            'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            'Accept': 'application/json',
        }
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            price = meta.get('regularMarketPrice')
            if not price:
                quote = result[0].get('indicators', {}).get('quote', [{}])[0]
                closes = [c for c in quote.get('close', []) if c is not None]
                if closes:
                    price = closes[-1]
            if price and float(price) > 0:
                _cache_put(f"yahoo::{ticker}", float(price))
                _REAL_TS[f"yahoo::{ticker}"] = time.time()
                return float(price)
    except Exception as e:
        if _is_rate_limited(e) and time.time() >= _YAHOO_BACKOFF_UNTIL:
            _YAHOO_BACKOFF_UNTIL = time.time() + 120
            logger.warning(f"Yahoo rate limited: paused 120s ({e})")
        else:
            logger.debug(f"Yahoo {ticker} failed: {e}")

    price = _fetch_stooq(ticker)
    if price:
        _cache_put(f"yahoo::{ticker}", price)
        _REAL_TS[f"yahoo::{ticker}"] = time.time()
        return price

    return cached


def _yahoo_ts(internal_symbol):
    """When the Yahoo/Stooq price for a symbol was really received (None = unknown → now)."""
    t = YAHOO_MAP.get(internal_symbol)
    return _REAL_TS.get(f"yahoo::{t}") if t else None


def _fetch_deriv_batch(internal_symbols, timeout=4.0):
    """Latest Deriv tick for each forex/metal symbol over one public socket → {symbol: (price, epoch)}."""
    global _DERIV_BACKOFF_UNTIL
    wanted = {DERIV_FX_MAP[s]: s for s in internal_symbols if s in DERIV_FX_MAP}
    if not wanted or time.time() < _DERIV_BACKOFF_UNTIL:
        return {}
    out = {}
    try:
        ws = ws_connect(DERIV_PUBLIC_WS, timeout=timeout)
        try:
            ids = {}
            for i, dsym in enumerate(wanted, start=1):
                ids[i] = dsym
                ws.send(json.dumps({'ticks_history': dsym, 'count': 1, 'end': 'latest',
                                    'style': 'ticks', 'req_id': i}))
            deadline = time.time() + timeout
            while len(out) < len(ids) and time.time() < deadline:
                msg = json.loads(ws.recv())
                dsym = ids.get(msg.get('req_id')) or (msg.get('echo_req') or {}).get('ticks_history')
                hist = msg.get('history') or {}
                if dsym in wanted and hist.get('prices'):
                    price = float(hist['prices'][-1])
                    epoch = float((hist.get('times') or [time.time()])[-1])
                    if price > 0:
                        out[wanted[dsym]] = (price, min(epoch, time.time()))
                elif msg.get('error') and dsym in wanted:
                    out.setdefault(wanted[dsym], None)
        finally:
            ws.close()
    except Exception as e:
        _DERIV_BACKOFF_UNTIL = time.time() + 30
        logger.warning(f"Deriv batch failed: {e}")
    return {k: v for k, v in out.items() if v}


def _fetch_stooq(ticker):
    """Stooq CSV — no rate limits."""
    sym = STOOQ_MAP.get(ticker)
    if not sym:
        return None
    try:
        url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=3) as resp:
            text = resp.read().decode('utf-8', errors='ignore')
        lines = text.strip().split('\n')
        if len(lines) < 2:
            return None
        parts = lines[1].split(',')
        if len(parts) < 7:
            return None
        close = parts[6].strip()
        if close and close not in ('N/A', ''):
            return float(close)
    except Exception:
        pass
    return None


def _fetch_swissquote(symbol, timeout=3.0):
    """(mid, ts) from Swissquote's public best bid/offer: the tightest spread among the freshest platforms."""
    pair = SWISSQUOTE_MAP.get(symbol)
    if not pair or time.time() < _PUBLIC_BACKOFF.get('swissquote', 0):
        return None
    try:
        req = _urlreq.Request(SWISSQUOTE_URL.format(*pair), headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        rows = []
        for platform in data if isinstance(data, list) else []:
            ts = float(platform.get('ts') or 0) / 1000.0
            for prof in platform.get('spreadProfilePrices') or []:
                bid, ask = float(prof.get('bid') or 0), float(prof.get('ask') or 0)
                if bid > 0 and ask >= bid:
                    rows.append((ts, ask - bid, (bid + ask) / 2))
        if rows:
            newest = max(r[0] for r in rows)
            ts, _, mid = min((r for r in rows if r[0] >= newest - 5), key=lambda r: r[1])
            return mid, min(ts or time.time(), time.time())
    except Exception as e:
        if _is_rate_limited(e):
            _PUBLIC_BACKOFF['swissquote'] = time.time() + 60
        logger.debug(f"Swissquote {symbol} failed: {e}")
    return None


def _fetch_goldapi(symbol, timeout=3.0):
    """(price, ts) from gold-api.com (gold and silver spot, no key)."""
    code = GOLDAPI_MAP.get(symbol)
    if not code or time.time() < _PUBLIC_BACKOFF.get('goldapi', 0):
        return None
    try:
        req = _urlreq.Request(GOLDAPI_URL.format(code), headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        price = float(data.get('price') or 0)
        if price <= 0:
            return None
        ts = time.time()
        if data.get('updatedAt'):
            try:
                ts = min(ts, datetime.fromisoformat(str(data['updatedAt']).replace('Z', '+00:00')).timestamp())
            except ValueError:
                pass
        return price, ts
    except Exception as e:
        if _is_rate_limited(e):
            _PUBLIC_BACKOFF['goldapi'] = time.time() + 60
        logger.debug(f"gold-api {symbol} failed: {e}")
    return None


def _fetch_public(symbol):
    """Keyless real-time fallback → (price, ts) or None. Each symbol is asked at most every 2 seconds."""
    hit = _PUBLIC_CACHE.get(symbol) or None
    if hit and time.time() - hit[2] < 2.0:
        return hit[:2]
    if not hit and time.time() - _PUBLIC_FAILED.get(symbol, 0) < 10:
        return None
    for name, fetch in (('SWISSQUOTE', _fetch_swissquote), ('GOLDAPI', _fetch_goldapi)):
        got = fetch(symbol)
        if got:
            _PUBLIC_CACHE[symbol] = (got[0], got[1], time.time())
            _note_source(symbol, name)
            return got
    _PUBLIC_FAILED[symbol] = time.time()
    if hit:
        _PUBLIC_CACHE[symbol] = (hit[0], hit[1], time.time())   # don't hammer the sources while they fail
        return hit[:2]
    return None


def _note_source(symbol, source):
    _SOURCE[symbol] = (source, time.time())


# ═══════════════════════════════════════════════════════════════
# CANDLES
# ═══════════════════════════════════════════════════════════════
# ── Provider cool-down ──
# A provider that answers 429 (rate limit) or "out of API credits" is skipped for a while instead of
# being hammered (Yahoo lengthens its block when you keep calling). Shared through Django's cache so
# the web server and the background scanner back off together; a module dict covers a missing cache.
_COOL = {}


def cool_down(name, seconds, reason=''):
    until = time.time() + seconds
    if _COOL.get(name, 0) < until - 1:
        logger.warning(f"{name} paused for {seconds}s ({reason or 'rate limited'})")
    _COOL[name] = until
    try:
        from django.core.cache import cache
        cache.set(f"feed:cool:{name}", until, seconds)
    except Exception:
        pass


def cooling(name):
    """True while `name` is paused after a rate limit."""
    until = _COOL.get(name, 0)
    try:
        from django.core.cache import cache
        until = max(until, cache.get(f"feed:cool:{name}") or 0)
    except Exception:
        pass
    return until > time.time()


def is_rate_limit(err):
    s = str(err).lower()
    return '429' in s or 'too many requests' in s or 'credits' in s or 'rate limit' in s


_LAST_ERR = {}      # provider -> {'at': ts, 'error': text, 'paused_for': s}


def feed_failed(name, err, rate_pause=65):
    """
    Record why a provider failed and pause it so the next request doesn't wait on it again:
      429 / out of credits      → rate_pause seconds
      451 / 403 / restricted    → 1 hour (blocked from this server's region, e.g. Binance on US hosts)
      timeout / connection      → 2 minutes
    """
    s = str(err)
    low = s.lower()
    if is_rate_limit(err):
        pause, why = rate_pause, 'rate limited'
    elif '451' in low or '403' in low or 'forbidden' in low or 'restricted' in low or 'eligibility' in low:
        pause, why = 3600, f'blocked from this server ({s[:80]})'
    elif 'timed out' in low or 'timeout' in low or 'connection' in low or 'name or service' in low \
            or 'nodename' in low or 'unreachable' in low or 'proxy' in low or 'ssl' in low:
        pause, why = 120, f'network ({s[:80]})'
    else:
        pause, why = 0, s[:120]
    _LAST_ERR[name] = {'at': int(time.time()), 'error': s[:300], 'paused_for': pause}
    if pause:
        cool_down(name, pause, why)
    else:
        logger.warning(f"{name} failed: {s[:200]}")


def feed_status():
    """Per-provider state for diagnostics (paused?, last error)."""
    names = set(_LAST_ERR) | {k for k in _COOL}
    return {n: {'paused': cooling(n), **_LAST_ERR.get(n, {})} for n in sorted(names)}


_TD_INTERVAL = {
    '1m':  '1min',
    '5m':  '5min',
    '15m': '15min',
    '30m': '30min',
    '1h':  '1h',
    '4h':  '4h',
    '1d':  '1day',
    '1w':  '1week',
    '1M':  '1month',
}


def fetch_twelvedata_candles(internal_symbol, timeframe='1h', bars=500):
    """Fetch OHLC candles from TwelveData /time_series."""
    if not TWELVEDATA_API_KEY:
        return []

    td_sym = TWELVEDATA_MAP.get(internal_symbol)
    if not td_sym:
        return []

    td_interval = _TD_INTERVAL.get(timeframe, '1h')
    if cooling('TwelveData'):
        return []

    try:
        params = _urlparse.urlencode({
            'symbol':     td_sym,
            'interval':   td_interval,
            'outputsize': min(int(bars), 5000),
            'apikey':     TWELVEDATA_API_KEY,
            'order':      'ASC',
        })
        url = f"https://api.twelvedata.com/time_series?{params}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        if data.get('status') == 'error':
            if is_rate_limit(data.get('message')):
                feed_failed('TwelveData', data.get('message'))
            else:
                logger.warning(f"TwelveData candles error for {internal_symbol}: "
                               f"{data.get('message')}")
            return []

        values = data.get('values') or []
        if not values:
            return []

        candles = []
        for v in values:
            try:
                dt_str = v['datetime']
                if len(dt_str) == 10:
                    dt = datetime.strptime(dt_str, '%Y-%m-%d')
                else:
                    dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')

                epoch = int(dt.replace(tzinfo=_tz.utc).timestamp())
                candles.append({
                    'time':   epoch,
                    'open':   float(v['open']),
                    'high':   float(v['high']),
                    'low':    float(v['low']),
                    'close':  float(v['close']),
                    'volume': float(v.get('volume') or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue

        candles.sort(key=lambda c: c['time'])
        return candles

    except Exception as e:
        feed_failed('TwelveData', e)
        return []


# ── Keyless candle sources that work from any server (datacenter IPs included) ──
# Binance refuses US servers (HTTP 451) and Yahoo blocks datacenters, so a hosted site needs these.
_GRAN = {'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400}
DERIV_CANDLE_MAP = dict(DERIV_FX_MAP, **{
    'EURAUD': 'frxEURAUD', 'EURCAD': 'frxEURCAD', 'GBPAUD': 'frxGBPAUD', 'AUDJPY': 'frxAUDJPY',
    'AUDCAD': 'frxAUDCAD', 'AUDNZD': 'frxAUDNZD', 'NZDJPY': 'frxNZDJPY',
    'BTCUSD': 'cryBTCUSD', 'ETHUSD': 'cryETHUSD',
})
DERIV_SYNTHETIC = ('R_', '1HZ', 'BOOM', 'CRASH', 'JD', 'STPRNG', 'RDBEAR', 'RDBULL')
KRAKEN_MAP = {'BTCUSD': 'XBTUSD', 'ETHUSD': 'ETHUSD', 'SOLUSD': 'SOLUSD', 'XRPUSD': 'XRPUSD', 'ADAUSD': 'ADAUSD',
              'DOGEUSD': 'XDGUSD', 'DOTUSD': 'DOTUSD', 'LINKUSD': 'LINKUSD', 'BNBUSD': 'BNBUSD'}
COINBASE_MAP = {'BTCUSD': 'BTC-USD', 'ETHUSD': 'ETH-USD', 'SOLUSD': 'SOL-USD', 'XRPUSD': 'XRP-USD', 'ADAUSD': 'ADA-USD',
                'DOGEUSD': 'DOGE-USD', 'DOTUSD': 'DOT-USD', 'LINKUSD': 'LINK-USD'}


def fetch_deriv_candles(internal_symbol, timeframe='1h', bars=500, timeout=8.0):
    """OHLC from Deriv's public socket (same connection the live prices use; needs websocket-client)."""
    # forex / metals / crypto map to frx… / cry… names; synthetic indices (R_75, 1HZ100V…) use their own name
    dsym = DERIV_CANDLE_MAP.get(internal_symbol) or (internal_symbol if internal_symbol.startswith(DERIV_SYNTHETIC) else None)
    gran = _GRAN.get(timeframe)
    if not dsym or not gran or cooling('Deriv'):
        return []
    try:
        ws = ws_connect(DERIV_PUBLIC_WS, timeout=timeout)
        try:
            ws.send(json.dumps({'ticks_history': dsym, 'style': 'candles', 'granularity': gran,
                                'count': max(1, min(int(bars), 5000)), 'end': 'latest', 'adjust_start_time': 1, 'req_id': 7}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get('req_id') != 7:
                    continue
                if msg.get('error'):
                    err = msg['error'].get('message') or msg['error'].get('code')
                    if 'market is presently closed' not in str(err).lower():
                        logger.warning(f"Deriv candles {dsym}: {err}")
                    return []
                rows = msg.get('candles') or []
                return [{'time': int(c['epoch']), 'open': float(c['open']), 'high': float(c['high']),
                         'low': float(c['low']), 'close': float(c['close']), 'volume': 0.0} for c in rows]
        finally:
            ws.close()
    except Exception as e:
        feed_failed('Deriv', e if str(e) else f'timeout ({type(e).__name__})', rate_pause=60)
    return []


def deriv_live_ticks(deriv_symbol, count=1, timeout=None):
    """
    The next `count` live ticks of a Deriv symbol, for settling digit contracts:
    [{'quote', 'epoch', 'pip_size'}]. Same connection as the live prices (websocket-client or the
    built-in client). Raises with the reason when Deriv can't deliver them in time.
    """
    timeout = timeout or count * 2 + 8
    ws = ws_connect(DERIV_PUBLIC_WS, timeout=min(timeout, 10))
    try:
        ws.send(json.dumps({'ticks': deriv_symbol, 'subscribe': 1, 'req_id': 21}))
        ticks, deadline = [], time.time() + timeout
        while len(ticks) < count:
            if time.time() > deadline:
                raise TimeoutError(f'only {len(ticks)} of {count} ticks from Deriv in {timeout}s')
            msg = json.loads(ws.recv())
            if msg.get('error'):
                raise RuntimeError(msg['error'].get('message') or msg['error'].get('code') or 'Deriv error')
            tick = msg.get('tick')
            if tick and tick.get('quote') is not None:
                ticks.append({'quote': float(tick['quote']), 'epoch': int(tick.get('epoch') or 0),
                              'pip_size': int(tick.get('pip_size') if tick.get('pip_size') is not None else 2)})
        return ticks
    finally:
        try:
            ws.send(json.dumps({'forget_all': 'ticks'}))
        except Exception:
            pass
        ws.close()


def _get_json(url, timeout=8):
    req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# Forex and gold on Kraken (plain https, keyless): the fallback when the Deriv socket is not reachable,
# e.g. a PythonAnywhere account that only allows approved sites. Gold = PAXG (a token backed by physical
# gold, within a fraction of a percent of spot), rescaled to the live gold price.
KRAKEN_FX_MAP = {
    'EURUSD': 'EURUSD', 'GBPUSD': 'GBPUSD', 'USDJPY': 'USDJPY', 'AUDUSD': 'AUDUSD', 'USDCAD': 'USDCAD',
    'USDCHF': 'USDCHF', 'EURGBP': 'EURGBP', 'EURJPY': 'EURJPY', 'EURCHF': 'EURCHF', 'EURAUD': 'EURAUD',
    'EURCAD': 'EURCAD', 'AUDJPY': 'AUDJPY', 'XAUUSD': 'PAXGUSD',
}
PROXY_ASSETS = {'XAUUSD'}           # candles from a tracking asset: rescale to the live price
_KRAKEN_UNKNOWN = set()             # pairs Kraken said it doesn't list (not asked again)


def _rescale_to_spot(internal_symbol, candles):
    """Shift a tracking asset's candles (PAXG for gold) onto the live spot price, so levels match the quote."""
    if internal_symbol not in PROXY_ASSETS or not candles:
        return candles
    entry = get_price_entry(internal_symbol)
    spot = entry['price'] if entry and entry.get('price') else None
    last = candles[-1]['close']
    if not spot or not last or abs(spot / last - 1) > 0.05:      # no spot yet, or not comparable: leave as is
        return candles
    k = spot / last
    return [dict(c, open=c['open'] * k, high=c['high'] * k, low=c['low'] * k, close=c['close'] * k) for c in candles]


def fetch_kraken_candles(internal_symbol, timeframe='1h', bars=500):
    """OHLC from Kraken's public API (keyless, serves US servers): crypto, major forex pairs, gold (PAXG). Up to 720 bars."""
    pair = KRAKEN_MAP.get(internal_symbol) or KRAKEN_FX_MAP.get(internal_symbol)
    interval = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240, '1d': 1440}.get(timeframe)
    if not pair or not interval or pair in _KRAKEN_UNKNOWN or cooling('Kraken'):
        return []
    try:
        data = _get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
        if data.get('error'):
            if any('Unknown asset pair' in str(x) for x in data['error']):
                _KRAKEN_UNKNOWN.add(pair)
            logger.warning(f"Kraken candles {pair}: {data['error']}")
            return []
        res = data.get('result') or {}
        rows = next((v for k, v in res.items() if k != 'last'), [])
        out = [{'time': int(r[0]), 'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]),
                'close': float(r[4]), 'volume': float(r[6])} for r in rows]
        return _rescale_to_spot(internal_symbol, out[-int(bars):])
    except Exception as e:
        feed_failed('Kraken', e, rate_pause=60)
        return []


def fetch_coinbase_candles(internal_symbol, timeframe='1h', bars=300):
    """Crypto OHLC from Coinbase Exchange (keyless). Max 300 bars; no 30m / 4h candles."""
    product = COINBASE_MAP.get(internal_symbol) or ('PAXG-USD' if internal_symbol == 'XAUUSD' else None)
    gran = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400}.get(timeframe)
    if not product or not gran or cooling('Coinbase'):
        return []
    try:
        rows = _get_json(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={gran}")
        if not isinstance(rows, list):
            return []
        out = [{'time': int(r[0]), 'low': float(r[1]), 'high': float(r[2]), 'open': float(r[3]),
                'close': float(r[4]), 'volume': float(r[5])} for r in rows]
        out.sort(key=lambda c: c['time'])               # Coinbase sends newest first
        return _rescale_to_spot(internal_symbol, out[-int(bars):])
    except Exception as e:
        feed_failed('Coinbase', e, rate_pause=60)
        return []


def fetch_oanda_candles(internal_symbol, timeframe='1h', bars=500):
    """Fetch OHLC candles from OANDA v20 /candles."""
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return []

    o_sym = OANDA_MAP.get(internal_symbol)
    if not o_sym:
        return []
    if cooling('OANDA'):
        return []

    gran = {
        '1m': 'M1', '5m': 'M5', '15m': 'M15', '30m': 'M30',
        '1h': 'H1', '4h': 'H4', '1d': 'D', '1w': 'W', '1M': 'M',
    }.get(timeframe, 'H1')

    base = ('https://api-fxpractice.oanda.com'
            if OANDA_ENV == 'practice'
            else 'https://api-fxtrade.oanda.com')
    url = (f"{base}/v3/instruments/{o_sym}/candles"
           f"?granularity={gran}&count={min(bars, 5000)}&price=M")

    try:
        req = _urlreq.Request(url, headers={
            'Authorization': f'Bearer {OANDA_API_KEY}',
            'Content-Type': 'application/json',
        })
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        candles = []
        for c in data.get('candles', []):
            if not c.get('complete', True):
                continue
            mid = c.get('mid', {})
            try:
                dt_str = c['time'].split('.')[0] + 'Z'
                dt = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ')
                epoch = int(dt.replace(tzinfo=_tz.utc).timestamp())
                candles.append({
                    'time':   epoch,
                    'open':   float(mid['o']),
                    'high':   float(mid['h']),
                    'low':    float(mid['l']),
                    'close':  float(mid['c']),
                    'volume': int(c.get('volume') or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue

        candles.sort(key=lambda x: x['time'])
        return candles

    except Exception as e:
        feed_failed('OANDA', e)
        return []


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════
def get_cached_prices():
    """Instant snapshot — never blocks."""
    with _CACHE_LOCK:
        return dict(_LAST_SNAPSHOT['data'])


def get_price_entry(symbol):
    """{'price': float, 'ts': epoch} of the last price received for a symbol, or None."""
    entry = _cache_get(symbol)
    if not entry or time.time() - entry['ts'] > 2.0:
        shared = _shared_get(symbol)          # a newer live tick seen by another server process
        if shared and (not entry or shared['ts'] > entry['ts']):
            _cache_put(symbol, shared['price'], now=shared['ts'])
            entry = shared
    return dict(entry) if entry else None


# Live ticks are also written to Django's cache, so the process that places orders sees the
# price the chart's live-feed process received (shared when the project's cache is Redis/Memcached).
_SHARED_LAST_WRITE = {}


def _shared_put(symbol, price, ts):
    if time.time() - _SHARED_LAST_WRITE.get(symbol, 0) < 0.5:
        return
    _SHARED_LAST_WRITE[symbol] = time.time()
    try:
        from django.core.cache import cache
        cache.set(f"px:price:{symbol}", {'price': float(price), 'ts': ts}, 300)
    except Exception:
        pass


def _shared_get(symbol):
    try:
        from django.core.cache import cache
        v = cache.get(f"px:price:{symbol}")
        if v and v.get('price', 0) > 0:
            return {'price': float(v['price']), 'ts': float(v['ts'])}
    except Exception:
        pass
    return None


def put_price(symbol, price):
    """Record a price from another source (e.g. Deriv ticks) in the shared cache."""
    _cache_put(symbol, price)
    if price and price > 0:
        with _CACHE_LOCK:
            _LAST_SNAPSHOT['data'][symbol] = float(price)
        _shared_put(symbol, price, time.time())


def refresh_if_stale(max_age=2.0):
    """Blocking refresh of every symbol when the snapshot is older than max_age seconds."""
    if time.time() - _LAST_SNAPSHOT['ts'] >= max_age:
        refresh_all_sync()


def get_single_price(symbol):
    """Fetch a single price with full fallback chain."""
    # Crypto
    if symbol in BINANCE_MAP:
        return _fetch_binance(BINANCE_MAP[symbol])

    # Forex/metals/energy
    if symbol in TWELVEDATA_MAP:
        p = _fetch_twelvedata_single(symbol)
        if p and p > 0:
            return p

        o = _fetch_oanda_batch([symbol])
        if symbol in o and o[symbol] > 0:
            return o[symbol]

        d = _fetch_deriv_batch([symbol])
        if symbol in d:
            return d[symbol][0]

        pub = _fetch_public(symbol)
        if pub:
            return pub[0]

        if symbol in YAHOO_MAP:
            y = _fetch_yahoo(YAHOO_MAP[symbol])
            if y and y > 0:
                return y

    return None


def get_single_price_preferred(symbol):
    """
    Real-time with smart cache.
    Used by the WS consumer for Gold/Oil/Forex ticks.
    """
    # Crypto → Binance (1-second cache so parallel callers share one request)
    if symbol in BINANCE_MAP:
        should, cached = _should_fetch(f"bn::{symbol}", ttl=1.0, min_interval=1.0)
        if not should:
            return (_cache_get(symbol) or {}).get('price') or cached
        p = _fetch_binance(BINANCE_MAP[symbol])
        if p and p > 0:
            _cache_put(f"bn::{symbol}", p)
            _cache_put(symbol, p)
        return p or (_cache_get(symbol) or {}).get('price')

    # Forex/Metals/Energy
    if symbol in TWELVEDATA_MAP:
        # 2-second cache so parallel WS clients share one upstream fetch
        should, cached = _should_fetch(f"td::{symbol}", ttl=2.0, min_interval=2.0)
        if not should:
            return cached if cached else (_cache_get(symbol) or {}).get('price')

        # TwelveData real-time
        p = _fetch_twelvedata_single(symbol)
        if p and p > 0:
            _cache_put(f"td::{symbol}", p)
            _cache_put(symbol, p)
            _note_source(symbol, 'TWELVEDATA')
            return p

        # OANDA fallback
        o = _fetch_oanda_batch([symbol])
        if symbol in o and o[symbol] > 0:
            _cache_put(f"td::{symbol}", o[symbol])
            _cache_put(symbol, o[symbol])
            _note_source(symbol, 'OANDA')
            return o[symbol]

        # Deriv public ticks
        d = _fetch_deriv_batch([symbol])
        if symbol in d:
            p, ts = d[symbol]
            _cache_put(f"td::{symbol}", p, now=ts)
            _cache_put(symbol, p, now=ts)
            _note_source(symbol, 'DERIV')
            return p

        # Keyless real-time quotes (Swissquote, gold-api.com)
        pub = _fetch_public(symbol)
        if pub:
            p, ts = pub
            _cache_put(f"td::{symbol}", p, now=ts)
            _cache_put(symbol, p, now=ts)
            return p

        # Yahoo fallback
        if symbol in YAHOO_MAP:
            y = _fetch_yahoo(YAHOO_MAP[symbol])
            if y and y > 0:
                _cache_put(f"td::{symbol}", y, now=_yahoo_ts(symbol))
                _cache_put(symbol, y, now=_yahoo_ts(symbol))
                _note_source(symbol, 'YAHOO')
                return y

        # Stale cache
        entry = _cache_get(symbol)
        return entry['price'] if entry else None

    return None


def get_source_for(symbol):
    """Return source label for the frontend badge: the feed that really gave the last price, when known."""
    src = _SOURCE.get(symbol)
    if src and time.time() - src[1] < 120:
        return src[0]
    if symbol in BINANCE_MAP:
        return 'BINANCE'
    if symbol in TWELVEDATA_MAP and TWELVEDATA_API_KEY:
        return 'TWELVEDATA'
    if symbol in OANDA_MAP and OANDA_API_KEY:
        return 'OANDA'
    if symbol in YAHOO_MAP:
        return 'YAHOO'
    return 'UNKNOWN'


def refresh_all_async():
    """Background refresh — returns immediately. At most one upstream refresh per _MIN_REFRESH seconds."""
    global _REFRESH_IN_FLIGHT, _LAST_REFRESH_START
    with _CACHE_LOCK:
        if _REFRESH_IN_FLIGHT or time.time() - _LAST_REFRESH_START < _MIN_REFRESH:
            return
        _REFRESH_IN_FLIGHT = True
        _LAST_REFRESH_START = time.time()

    def _worker():
        global _REFRESH_IN_FLIGHT
        try:
            prices = {}
            stamps = {}   # symbol -> when that price was really received (default: now)

            # Crypto — parallel
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(_fetch_binance, bn): internal
                    for internal, bn in BINANCE_MAP.items()
                }
                try:
                    for fut in as_completed(futures, timeout=4):
                        internal = futures[fut]
                        try:
                            p = fut.result(timeout=0.1)
                            if p and p > 0:
                                prices[internal] = p
                        except Exception:
                            pass
                except Exception:
                    pass   # a slow Binance must not cancel the forex refresh below

            # Forex/metals — TwelveData batch
            fx_symbols = list(TWELVEDATA_MAP.keys())
            td_prices = _fetch_twelvedata_batch(fx_symbols)
            prices.update(td_prices)
            for s_ in td_prices:
                _note_source(s_, 'TWELVEDATA')

            # Missing → OANDA
            missing = [s for s in fx_symbols if s not in prices]
            if missing and OANDA_API_KEY:
                o_prices = _fetch_oanda_batch(missing)
                prices.update(o_prices)
                for s_ in o_prices:
                    _note_source(s_, 'OANDA')

            # Missing → Deriv public ticks
            missing = [s for s in fx_symbols if s not in prices]
            if missing:
                for internal, (p, ts) in _fetch_deriv_batch(missing).items():
                    prices[internal] = p
                    stamps[internal] = ts
                    _note_source(internal, 'DERIV')

            # Missing → keyless real-time quotes (Swissquote / gold-api.com)
            _fill_public([s for s in fx_symbols if s not in prices], prices, stamps)

            # Still missing → Yahoo
            still_missing = [s for s in fx_symbols if s not in prices]
            if still_missing:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    yf_futures = {
                        pool.submit(_fetch_yahoo, YAHOO_MAP[s]): s
                        for s in still_missing if s in YAHOO_MAP
                    }
                    try:
                        for fut in as_completed(yf_futures, timeout=5):
                            internal = yf_futures[fut]
                            try:
                                p = fut.result(timeout=0.1)
                                if p and p > 0:
                                    prices[internal] = p
                                    stamps[internal] = _yahoo_ts(internal)
                                    _note_source(internal, 'YAHOO')
                            except Exception:
                                pass
                    except Exception:
                        pass

            if prices:
                with _CACHE_LOCK:
                    _LAST_SNAPSHOT['data'].update(prices)
                    _LAST_SNAPSHOT['ts'] = time.time()

                for internal, price in prices.items():
                    _cache_put(internal, price, now=stamps.get(internal))

        except Exception as e:
            logger.exception(f"refresh_all_async worker failed: {e}")
        finally:
            _REFRESH_IN_FLIGHT = False

    Thread(target=_worker, daemon=True).start()


def refresh_all_sync(timeout=6.0):
    """Synchronous refresh — used by WS consumers."""
    prices = {}

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(_fetch_binance, bn): internal
            for internal, bn in BINANCE_MAP.items()
        }
        deadline = time.time() + timeout / 2
        try:
            for fut in as_completed(futures, timeout=timeout / 2):
                if time.time() > deadline:
                    break
                internal = futures[fut]
                try:
                    p = fut.result(timeout=0.1)
                    if p and p > 0:
                        prices[internal] = p
                except Exception:
                    pass
        except Exception:
            pass

    fx_symbols = list(TWELVEDATA_MAP.keys())
    td_prices = _fetch_twelvedata_batch(fx_symbols)
    prices.update(td_prices)

    missing = [s for s in fx_symbols if s not in prices]
    if missing and OANDA_API_KEY:
        prices.update(_fetch_oanda_batch(missing))

    stamps = {}
    missing = [s for s in fx_symbols if s not in prices]
    if missing:
        for internal, (p, ts) in _fetch_deriv_batch(missing).items():
            prices[internal] = p
            stamps[internal] = ts
    _fill_public([s for s in fx_symbols if s not in prices], prices, stamps)

    if prices:
        with _CACHE_LOCK:
            _LAST_SNAPSHOT['data'].update(prices)
            _LAST_SNAPSHOT['ts'] = time.time()
        for internal, price in prices.items():
            _cache_put(internal, price, now=stamps.get(internal))

    return prices

def _fill_public(symbols, prices, stamps):
    """Fill `prices` / `stamps` for symbols the keyed feeds missed, from the keyless sources (in parallel)."""
    symbols = [s for s in symbols if s in SWISSQUOTE_MAP or s in GOLDAPI_MAP]
    if not symbols:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        futures = {pool.submit(_fetch_public, s): s for s in symbols}
        try:
            for fut in as_completed(futures, timeout=5):
                try:
                    got = fut.result(timeout=0.1)
                except Exception:
                    got = None
                if got:
                    prices[futures[fut]] = got[0]
                    stamps[futures[fut]] = got[1]
        except Exception:
            pass   # a slow source must not hold up the rest of the refresh


def diagnose(symbol='XAUUSD'):
    """
    Ask every source for `symbol` and print what each one answers — run it on the server:
        python manage.py shell -c "from store import price_feed; price_feed.diagnose('XAUUSD')"
    """
    symbol = symbol.upper()
    rows = []

    def attempt(name, fn):
        t0 = time.time()
        try:
            got = fn()
        except Exception as e:
            got = f'error: {e}'
        rows.append((name, got, time.time() - t0))

    print(f"Price sources for {symbol}")
    print(f"  TwelveData key: {'set' if TWELVEDATA_API_KEY else 'missing'}"
          + (f" (paused {int(_TD_BACKOFF_UNTIL - time.time())}s more: out of credits or refused)" if _td_blocked() else ''))
    print(f"  OANDA key:      {'set' if OANDA_API_KEY else 'missing'}")
    try:
        import websocket  # noqa: F401
        print("  websocket-client (needed for Deriv): installed")
    except ImportError:
        print("  websocket-client (needed for Deriv): NOT INSTALLED → pip install websocket-client")
    if symbol in BINANCE_MAP:
        attempt('Binance', lambda: _fetch_binance(BINANCE_MAP[symbol]))
    if symbol in TWELVEDATA_MAP:
        attempt('TwelveData', lambda: _fetch_twelvedata_single(symbol))
        attempt('OANDA', lambda: _fetch_oanda_batch([symbol]).get(symbol))
        attempt('Deriv', lambda: (_fetch_deriv_batch([symbol]).get(symbol) or (None,))[0])
        attempt('Swissquote', lambda: (_fetch_swissquote(symbol) or (None,))[0])
        attempt('gold-api.com', lambda: (_fetch_goldapi(symbol) or (None,))[0])
    if symbol in YAHOO_MAP:
        t = YAHOO_MAP[symbol]
        attempt(f'Yahoo {t}', lambda: _fetch_yahoo(t))
    for name, got, took in rows:
        print(f"  {name:<16} {got if got not in (None, '') else 'no price'}   ({took:.1f}s)")
    entry = get_price_entry(symbol)
    if entry:
        print(f"  In the cache: {entry['price']} from {time.time() - entry['ts']:.0f}s ago (trades need under 30s)")
    else:
        print("  In the cache: nothing yet")
    return rows
