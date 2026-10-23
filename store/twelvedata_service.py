# twelvedata_service.py
"""
TwelveData service for real-time Gold, Oil, and Forex streaming.
Free tier: 800 calls/day + WebSocket streaming.
"""
import json
import logging
import urllib.request as _urlreq
import urllib.parse as _urlparse

logger = logging.getLogger(__name__)

# TwelveData symbol mapping
TWELVEDATA_SYMBOLS = {
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


def fetch_twelvedata_quote(symbol, api_key):
    """
    Fetch a single real-time quote from TwelveData.
    Returns dict with price, or None on failure.
    """
    td_symbol = TWELVEDATA_SYMBOLS.get(symbol)
    if not td_symbol:
        return None

    try:
        params = _urlparse.urlencode({
            'symbol': td_symbol,
            'apikey': api_key,
        })
        url = f"https://api.twelvedata.com/price?{params}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        if 'price' in data:
            return {
                'symbol': symbol,
                'price': float(data['price']),
                'source': 'TWELVEDATA',
            }
    except Exception as e:
        logger.warning(f"TwelveData fetch failed for {symbol}: {e}")

    return None


def fetch_twelvedata_batch(symbols, api_key):
    """
    Fetch multiple quotes in one API call (saves quota).
    TwelveData supports comma-separated symbols.
    """
    td_symbols = [TWELVEDATA_SYMBOLS.get(s) for s in symbols if s in TWELVEDATA_SYMBOLS]
    td_symbols = [s for s in td_symbols if s]
    if not td_symbols:
        return {}

    try:
        params = _urlparse.urlencode({
            'symbol': ','.join(td_symbols),
            'apikey': api_key,
        })
        url = f"https://api.twelvedata.com/price?{params}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        results = {}
        # Single symbol returns flat dict; multiple returns nested
        if isinstance(data, dict) and 'price' in data:
            results[symbols[0]] = float(data['price'])
        else:
            for internal, td_sym in TWELVEDATA_SYMBOLS.items():
                if td_sym in data and 'price' in data[td_sym]:
                    results[internal] = float(data[td_sym]['price'])
        return results
    except Exception as e:
        logger.warning(f"TwelveData batch fetch failed: {e}")
        return {}