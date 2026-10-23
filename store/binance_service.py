"""
Binance Spot balance service.
Read-only — only fetches balance, never places orders.
"""
import hmac
import hashlib
import time
import logging
import requests
from decimal import Decimal
from urllib.parse import urlencode
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# api.binance.com plus Binance's documented backup hosts (same API, different routes)
BINANCE_HOSTS = ['https://api.binance.com', 'https://api1.binance.com', 'https://api2.binance.com',
                 'https://api3.binance.com', 'https://api-gcp.binance.com']
STABLES = {'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD', 'USDP', 'DAI'}


def friendly_error(status, body):
    """Binance error → what the user should actually do."""
    code = (body or {}).get('code')
    msg = (body or {}).get('msg') or ''
    if status == 451 or 'restricted location' in msg.lower():
        return ('Binance blocks API calls from this server\'s country (HTTP 451). The site must be hosted '
                'outside restricted regions (e.g. not in the US) for Binance to answer.')
    if code == -2015 or code == -2014 or status == 401:
        return ('Binance rejected the key: check it was copied fully, that "Enable Reading" is ticked, '
                'and — if you set an IP restriction — that this server\'s IP is on the list.')
    if code == -1021:
        return 'The server clock is out of sync with Binance — retrying with Binance time failed. Sync the server clock (NTP).'
    if code == -1022:
        return 'Wrong secret key — the signature did not match. Paste the Secret Key again.'
    if status == 418 or status == 429:
        return 'Binance rate limit hit — wait a minute and try again.'
    return msg or f'Binance HTTP {status}'


class BinanceService:
    BASE_URL = 'https://api.binance.com'

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key
        self.api_secret = api_secret

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def _time_offset(self, host):
        """ms to add to our clock so signed requests aren't rejected for clock drift (-1021)."""
        off = cache.get('binance:time_offset')
        if off is None:
            try:
                server = requests.get(f'{host}/api/v3/time', timeout=5).json()['serverTime']
                off = int(server - time.time() * 1000)
            except Exception:
                off = 0
            cache.set('binance:time_offset', off, 600)
        return off

    def _signed_get(self, endpoint: str, params: dict = None) -> dict:
        """Signed GET — tries the backup hosts on network errors / 5xx, raises HTTPError with a friendly .reason."""
        last_exc = None
        for host in BINANCE_HOSTS:
            p = dict(params or {})
            p['timestamp'] = int(time.time() * 1000) + self._time_offset(host)
            p['recvWindow'] = 10000
            query_string = urlencode(p)
            url = f"{host}{endpoint}?{query_string}&signature={self._sign(query_string)}"
            try:
                response = requests.get(url, headers={'X-MBX-APIKEY': self.api_key}, timeout=10)
            except requests.RequestException as e:
                last_exc = e
                continue
            if response.status_code >= 500:
                last_exc = requests.HTTPError(f'Binance {response.status_code}', response=response)
                continue
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if body.get('code') == -1021:           # clock drift: refresh the offset once
                    cache.delete('binance:time_offset')
                err = requests.HTTPError(friendly_error(response.status_code, body), response=response)
                err.reason = friendly_error(response.status_code, body)
                raise err
            return response.json()
        if isinstance(last_exc, requests.HTTPError):
            last_exc.reason = friendly_error(last_exc.response.status_code, {})
            raise last_exc
        raise requests.ConnectionError(f'Could not reach Binance from this server ({last_exc.__class__.__name__ if last_exc else "no route"})')

    def _prices(self):
        """Public USDT prices for valuing non-stablecoin assets."""
        prices = cache.get('binance:usdt_prices')
        if prices is None:
            prices = {}
            for host in BINANCE_HOSTS:
                try:
                    rows = requests.get(f'{host}/api/v3/ticker/price', timeout=8).json()
                    prices = {r['symbol']: float(r['price']) for r in rows if isinstance(r, dict) and r.get('symbol', '').endswith('USDT')}
                    break
                except Exception:
                    continue
            cache.set('binance:usdt_prices', prices, 60)
        return prices

    def get_account_info(self) -> dict:
        """Fetch account info with balances"""
        return self._signed_get('/api/v3/account')

    def get_stablecoin_balance(self) -> dict:
        """
        Returns total stablecoin balance (USDT + USDC + BUSD + FDUSD).
        This is what we display as the Binance spot balance.
        """
        try:
            data = self.get_account_info()
        except requests.HTTPError as e:
            return {'success': False, 'error': getattr(e, 'reason', None) or str(e)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

        total = Decimal('0.00')          # stablecoins, as before
        breakdown = {}
        others, prices = {}, None
        for bal in data.get('balances', []):
            asset = bal.get('asset')
            amount = Decimal(str(bal.get('free', '0'))) + Decimal(str(bal.get('locked', '0')))
            if amount <= 0:
                continue
            if asset in STABLES:
                total += amount
                breakdown[asset] = float(amount)
            else:
                others[asset] = amount

        # Coins (BTC, ETH, BNB…) valued at the live USDT price so a non-stable wallet doesn't show 0
        coins_usdt = Decimal('0.00')
        if others:
            prices = self._prices()
            for asset, amount in others.items():
                px = prices.get(f'{asset}USDT')
                if px:
                    v = amount * Decimal(str(px))
                    coins_usdt += v
                    if v >= Decimal('0.01'):
                        breakdown[asset] = float(amount)

        return {
            'success': True,
            'balance': float(total + coins_usdt),
            'stable_balance': float(total),
            'coins_usdt': float(coins_usdt),
            'currency': 'USDT',
            'breakdown': breakdown,
            'raw': {'accountType': data.get('accountType', 'SPOT'), 'canTrade': data.get('canTrade')},
        }

    def validate_credentials(self) -> tuple:
        """Returns (is_valid, error_message)"""
        if not self.api_key or not self.api_secret:
            return False, 'Both the API Key and the Secret Key are required.'
        try:
            self.get_account_info()
            return True, None
        except requests.HTTPError as e:
            return False, getattr(e, 'reason', None) or f"HTTP {getattr(e.response, 'status_code', '?')}"
        except Exception as e:
            return False, str(e)