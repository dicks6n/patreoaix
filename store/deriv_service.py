"""
Deriv Service — Uses public WebSocket for market data.
The public endpoint accepts `ticks_history` and `ticks` subscriptions
without any authentication, which is what we need for chart data.
"""
import json
import logging
import ssl
import asyncio
import time
import websockets
import requests
import pandas as pd
from django.conf import settings

logger = logging.getLogger(__name__)

# SSL context for secure WebSocket connections
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# ✅ Public WebSocket (no auth needed) — used for market data (ticks + candles)
from .price_feed import DERIV_PUBLIC_WS as PUBLIC_WS_URL     # app_id from settings.DERIV_WS_APP_ID

# REST base for OTP + authenticated endpoints
REST_BASE_URL = "https://api.derivws.com"

# New Deriv API (2026): OAuth 2.0 Authorization Code + PKCE on auth.deriv.com.
# The old oauth.deriv.com redirect that returned acct1/token1 belongs to the legacy API.
OAUTH_AUTHORIZE_URL = "https://auth.deriv.com/oauth2/auth"
OAUTH_TOKEN_URL = "https://auth.deriv.com/oauth2/token"
ACCOUNTS_PATH = "/trading/v1/options/accounts"

# Account-ID prefixes that mean demo money (legacy VRTC/VRW; new-API demo options accounts)
DEMO_PREFIXES = ('VRTC', 'VRW', 'VRT', 'DOT', 'DEM')


def is_legacy_token(token):
    """Legacy API tokens look like 'a1-…'. They only work on the legacy WebSocket with `authorize`."""
    return bool(token) and str(token).startswith('a1-')


def looks_demo(account_id):
    return str(account_id or '').upper().startswith(DEMO_PREFIXES)

SYMBOL_MAP = {
    "Gold / USD (XAU)": "frxXAUUSD",
    "Volatility 100 Index": "R_100",
    "Volatility 75 Index": "R_75",
    "Volatility 50 Index": "R_50",
    "Volatility 25 Index": "R_25",
    "Volatility 10 Index": "R_10",
    "EUR/USD": "frxEURUSD",
    "GBP/USD": "frxGBPUSD",
    "USD/JPY": "frxUSDJPY",
    "BTC/USD": "cryBTCUSD",
}

BASE_PRICES = {
    'R_10': 1000.00,
    'R_25': 2500.00,
    'R_50': 5000.00,
    'R_75': 7500.00,
    'R_100': 10000.00,
    '1HZ10V': 1000.00,
    '1HZ25V': 2500.00,
    '1HZ50V': 5000.00,
    '1HZ75V': 7500.00,
    '1HZ100V': 10000.00,
    'frxXAUUSD': 4454.08,
    'frxXTIUSD': 83.44,
    'frxXBRUSD': 83.44,
    'frxEURUSD': 1.1583,
    'frxGBPUSD': 1.2940,
    'frxUSDJPY': 146.15,
    'frxEURGBP': 0.8415,
    'frxEURJPY': 159.20,
    'frxGBPJPY': 189.10,
    'cryBTCUSD': 78912.33,
    'cryETHUSD': 3456.78,
}


def async_to_sync_safe(fn):
    from asgiref.sync import async_to_sync
    return async_to_sync(fn)


class DerivService:
    def __init__(self):
        self.app_id = getattr(settings, 'DERIV_APP_ID', '')
        self.api_token = getattr(settings, 'DERIV_API_TOKEN', '')
        self._use_fallback = False
        self._fallback_started_at = 0
        self._last_prices = {}
        self.last_error = None

    def resolve_symbol(self, raw_symbol):
        return SYMBOL_MAP.get(raw_symbol, raw_symbol)

    # ==========================================
    # OTP FLOW — Get authenticated WebSocket URL
    # ==========================================
    def _rest_headers(self, token):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        if self.app_id:                       # required for Personal Access Tokens, harmless for OAuth tokens
            headers["Deriv-App-ID"] = str(self.app_id)
        return headers

    @staticmethod
    def _http_reason(e):
        """Readable reason from a Deriv REST error (they reply with an `errors` array)."""
        resp = getattr(e, 'response', None)
        code = getattr(resp, 'status_code', None)
        msg = ''
        try:
            body = resp.json()
            errs = body.get('errors') or ([body.get('error')] if body.get('error') else [])
            if errs:
                first = errs[0]
                msg = first.get('message') or first.get('detail') or str(first) if isinstance(first, dict) else str(first)
        except Exception:
            pass
        if code == 401:
            return 'Deriv login expired or the token is invalid — reconnect your Deriv account'
        if code == 403:
            return msg or 'Deriv refused access — check the app permissions (scope "trade") or the App ID'
        if code == 429:
            return 'Deriv rate limit reached — try again in a minute'
        return f"Deriv API error {code}: {msg}" if code else str(e)

    def _get_authenticated_ws_url(self, token, account_number):
        endpoint = f"{REST_BASE_URL}{ACCOUNTS_PATH}/{account_number}/otp"
        self.last_error = None
        try:
            response = requests.post(endpoint, headers=self._rest_headers(token), timeout=10)
            response.raise_for_status()
            data = response.json()
            ws_url = data.get('data', {}).get('url')
            if not ws_url:
                logger.warning(f"OTP response missing 'url' field: {data}")
                self.last_error = 'Deriv did not return a trading socket for this account'
                return None
            return ws_url
        except requests.HTTPError as e:
            logger.warning(f"OTP request HTTP error: {e} — {getattr(e.response, 'text', '')[:200]}")
            self.last_error = self._http_reason(e)
            return None
        except Exception as e:
            logger.warning(f"OTP request failed: {e}")
            self.last_error = f'Could not reach Deriv ({e.__class__.__name__})'
            return None

    # ==========================================
    # OAUTH 2.0 + PKCE (new API)
    # ==========================================
    @staticmethod
    def new_pkce():
        """(code_verifier, code_challenge) — verifier 64 url-safe chars, challenge = BASE64URL(SHA256(verifier))."""
        import base64, hashlib, secrets
        verifier = secrets.token_urlsafe(48)[:64]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return verifier, challenge

    @staticmethod
    def authorize_url(client_id, redirect_uri, state, code_challenge, scope='trade'):
        from urllib.parse import urlencode
        return OAUTH_AUTHORIZE_URL + '?' + urlencode({
            'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri, 'scope': scope,
            'state': state, 'code_challenge': code_challenge, 'code_challenge_method': 'S256',
        })

    def exchange_code(self, client_id, code, code_verifier, redirect_uri):
        """Authorization code → (access_token, expires_in, error)."""
        try:
            r = requests.post(OAUTH_TOKEN_URL, timeout=15, headers={'Accept': 'application/json'}, data={
                'grant_type': 'authorization_code', 'client_id': client_id, 'code': code,
                'code_verifier': code_verifier, 'redirect_uri': redirect_uri,
            })
            data = r.json() if r.content else {}
            if r.status_code >= 400 or not data.get('access_token'):
                return None, None, data.get('error_description') or data.get('error') or f'Token exchange failed (HTTP {r.status_code})'
            return data['access_token'], data.get('expires_in'), None
        except Exception as e:
            return None, None, f'Could not reach Deriv to finish the login ({e.__class__.__name__})'

    # ==========================================
    # ACCOUNTS (demo + real)
    # ==========================================
    def list_accounts(self, token):
        """
        Every Options account behind a token: [{'id', 'is_demo', 'currency', 'balance'|None, 'raw'}], error.
        New-API tokens use REST; legacy a1- tokens use `authorize` on the legacy socket.
        """
        if not token:
            return [], 'No Deriv token'
        if is_legacy_token(token):
            return async_to_sync_safe(self.legacy_accounts)(token)
        try:
            r = requests.get(f"{REST_BASE_URL}{ACCOUNTS_PATH}", headers=self._rest_headers(token), timeout=10)
            r.raise_for_status()
            return self._parse_accounts(r.json()), None
        except requests.HTTPError as e:
            return [], self._http_reason(e)
        except Exception as e:
            return [], f'Could not reach Deriv ({e.__class__.__name__})'

    @staticmethod
    def _parse_accounts(payload):
        """Accept the shapes Deriv uses: {'data': [...]}, {'data': {'accounts': [...]}}, {'accounts': [...]}, [...]."""
        items = payload
        if isinstance(items, dict):
            items = items.get('data', items)
        if isinstance(items, dict):
            items = items.get('accounts') or items.get('items') or [items]
        out = []
        for a in items or []:
            if not isinstance(a, dict):
                continue
            acc_id = a.get('account_id') or a.get('id') or a.get('loginid') or a.get('account_number')
            if not acc_id:
                continue
            kind = str(a.get('account_type') or a.get('type') or a.get('environment') or a.get('mode') or '').lower()
            if any(k in a for k in ('is_virtual', 'is_demo', 'demo')):
                demo = bool(a.get('is_virtual') or a.get('is_demo') or a.get('demo'))
            elif kind:
                demo = kind in ('demo', 'virtual', 'practice', 'test')
            else:
                demo = looks_demo(acc_id)
            bal = a.get('balance')
            if isinstance(bal, dict):
                bal = bal.get('amount', bal.get('balance'))
            try:
                bal = float(bal) if bal is not None else None
            except (TypeError, ValueError):
                bal = None
            out.append({'id': str(acc_id), 'is_demo': demo, 'currency': a.get('currency') or 'USD', 'balance': bal, 'raw': a})
        out.sort(key=lambda x: x['is_demo'])        # real first
        return out

    async def legacy_accounts(self, token):
        """Legacy token: authorize on the public socket → account_list + the authorized account's balance."""
        try:
            async with websockets.connect(PUBLIC_WS_URL, ssl=ssl_context, open_timeout=8) as ws:
                await ws.send(json.dumps({"authorize": token}))
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if msg.get('error'):
                    return [], f"Deriv: {msg['error'].get('message', 'authorize failed')}"
                auth = msg.get('authorize', {})
                out = []
                for a in auth.get('account_list', []) or [{'loginid': auth.get('loginid'), 'is_virtual': auth.get('is_virtual'), 'currency': auth.get('currency')}]:
                    lid = a.get('loginid')
                    if not lid or a.get('is_disabled'):
                        continue
                    out.append({'id': lid, 'is_demo': bool(a.get('is_virtual')), 'currency': a.get('currency') or 'USD',
                                'balance': float(auth['balance']) if lid == auth.get('loginid') and auth.get('balance') is not None else None, 'raw': a})
                out.sort(key=lambda x: x['is_demo'])
                return out, None
        except Exception as e:
            return [], f'Could not reach Deriv ({e.__class__.__name__})'

    async def _legacy_balance(self, token, account_number):
        try:
            async with websockets.connect(PUBLIC_WS_URL, ssl=ssl_context, open_timeout=8) as ws:
                await ws.send(json.dumps({"authorize": token}))
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if msg.get('error'):
                    return self._demo_account_info(account_number, f"Deriv: {msg['error'].get('message', 'authorize failed')}")
                auth = msg['authorize']
                if account_number and account_number != auth.get('loginid'):
                    # the token logged in as another account of the same client: ask for every balance
                    await ws.send(json.dumps({"balance": 1, "account": "all"}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    row = ((reply.get('balance') or {}).get('accounts') or {}).get(account_number)
                    if not row:
                        return self._demo_account_info(account_number, f"This token only covers {auth.get('loginid')} — reconnect Deriv to refresh {account_number}")
                    bal = float(row.get('balance') or 0)
                    demo = bool(row.get('demo_account')) if 'demo_account' in row else looks_demo(account_number)
                    return {"balance": f"{bal:,.2f}", "raw_balance": bal, "currency": row.get('currency') or 'USD',
                            "is_demo": demo, "loginid": account_number, "server": "Deriv (legacy token)", "connected": True}
                bal = float(auth.get('balance') or 0)
                return {"balance": f"{bal:,.2f}", "raw_balance": bal, "currency": auth.get('currency') or 'USD',
                        "is_demo": bool(auth.get('is_virtual')), "loginid": auth.get('loginid', account_number),
                        "server": "Deriv (legacy token)", "connected": True}
        except Exception as e:
            return self._demo_account_info(account_number, f'Could not reach Deriv ({e.__class__.__name__})')

    # ==========================================
    # ACCOUNT INFO
    # ==========================================
    async def get_account_info(self, token=None, account_number=""):
        active_token = token or self.api_token

        if not active_token or not account_number:
            return self._demo_account_info(account_number, 'No Deriv token saved — connect your Deriv account')

        if is_legacy_token(active_token):
            return await self._legacy_balance(active_token, account_number)

        ws_url = await asyncio.to_thread(
            self._get_authenticated_ws_url, active_token, account_number
        )
        if not ws_url:
            logger.warning("Could not obtain authenticated WebSocket URL")
            return self._demo_account_info(account_number, getattr(self, 'last_error', None))

        try:
            async with websockets.connect(
                ws_url,
                ssl=ssl_context,
                open_timeout=8,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"balance": 1, "subscribe": 1}))

                balance = None
                currency = "USD"
                loginid = account_number
                is_virtual = looks_demo(account_number)

                start_time = asyncio.get_event_loop().time()
                while (asyncio.get_event_loop().time() - start_time) < 8:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        msg = json.loads(raw)

                        if msg.get('msg_type') == 'balance' and 'balance' in msg:
                            balance = float(msg['balance'].get('balance', 0))
                            currency = msg['balance'].get('currency', 'USD')
                            loginid = msg['balance'].get('loginid', account_number)
                            break

                        if msg.get('msg_type') == 'error' or msg.get('error'):
                            err_msg = (msg.get('error') or {}).get('message')
                            logger.warning(f"Deriv error: {err_msg}")
                            return self._demo_account_info(account_number, f"Deriv: {err_msg}")
                    except asyncio.TimeoutError:
                        break

                if balance is None:
                    logger.warning("No balance returned from Deriv WebSocket")
                    return self._demo_account_info(account_number, 'Deriv did not send a balance')

                return {
                    "balance": f"{balance:,.2f}",
                    "raw_balance": balance,
                    "currency": currency,
                    "is_demo": is_virtual,
                    "loginid": str(loginid),
                    "server": "Deriv Demo" if is_virtual else "Deriv Real",
                    "connected": True,
                }

        except Exception as e:
            logger.warning(f"Deriv Account Info Error: {e}")
            return self._demo_account_info(account_number, f'Deriv socket error ({e.__class__.__name__})')

    def _demo_account_info(self, account_number="", error=None):
        # Deriv could not be reached / logged into: say so instead of showing a made-up balance
        return {
            "balance": None,
            "raw_balance": None,
            "currency": "USD",
            "is_demo": False,
            "loginid": str(account_number or ''),
            "server": "Deriv",
            "connected": False,
            "error": error,
        }

    # ==========================================
    # TICKS
    # ==========================================
    async def get_recent_ticks(self, symbol="frxXAUUSD", count=50):
        deriv_symbol = self.resolve_symbol(symbol)

        try:
            async with websockets.connect(
                PUBLIC_WS_URL,
                ssl=ssl_context,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                req = {
                    "ticks_history": deriv_symbol,
                    "adjust_start_time": 1,
                    "count": count,
                    "end": "latest",
                    "style": "ticks",
                    "subscribe": 0,
                }
                await ws.send(json.dumps(req))
                res = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))

                if "error" in res:
                    logger.warning(f"Deriv ticks error for {deriv_symbol}: {res['error']}")
                    return []

                if "history" in res and "prices" in res["history"]:
                    prices = [float(p) for p in res["history"]["prices"]]
                    if prices:
                        self._last_prices[deriv_symbol] = prices[-1]
                    return prices

        except Exception as e:
            logger.warning(f"Deriv ticks unavailable for {deriv_symbol}: {e}")

        return []

    async def get_settlement_ticks(self, symbol, count=1, timeout=10, detail=False):
        """
        Live ticks used to open and settle real-balance contracts.
        Never falls back to generated prices: returns [] when Deriv can't be
        reached, and the caller refunds / rejects the trade.
        """
        deriv_symbol = self.resolve_symbol(symbol)

        try:
            async with websockets.connect(
                PUBLIC_WS_URL,
                ssl=ssl_context,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"ticks": deriv_symbol, "subscribe": 1}))

                ticks = []
                start_time = asyncio.get_event_loop().time()

                while len(ticks) < count:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        break
                    try:
                        remaining = max(0.5, timeout - elapsed)
                        response = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        data = json.loads(response)

                        if "error" in data:
                            logger.warning(f"Deriv settlement error: {data['error']}")
                            return []

                        if "tick" in data and "quote" in data["tick"]:
                            quote = float(data["tick"]["quote"])
                            if detail:
                                ticks.append({'quote': quote, 'epoch': int(data["tick"].get("epoch") or 0),
                                              'pip_size': int(data["tick"].get("pip_size") or 2)})
                            else:
                                ticks.append(quote)
                            self._last_prices[deriv_symbol] = quote
                    except asyncio.TimeoutError:
                        break

                try:
                    await ws.send(json.dumps({"forget_all": "ticks"}))
                except Exception:
                    pass

                # A contract settles on its exact tick count, or not at all
                return ticks if len(ticks) >= count else []

        except Exception as e:
            logger.warning(f"Error getting settlement ticks for {deriv_symbol}: {e}")

        return []

    # ==========================================
    # CANDLES — Historical OHLC from public WS
    # ==========================================
    async def get_candles(self, symbol="frxXAUUSD", granularity=3600, count=500):
        """
        Fetch historical OHLC candles from Deriv's public WebSocket.
        Used by /api/candles/ for forex/metals/oil.
        """
        deriv_symbol = self.resolve_symbol(symbol)

        try:
            async with websockets.connect(
                PUBLIC_WS_URL,
                ssl=ssl_context,
                open_timeout=8,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                req = {
                    "ticks_history": deriv_symbol,
                    "adjust_start_time": 1,
                    "count": min(count, 5000),
                    "end": "latest",
                    "granularity": granularity,
                    "style": "candles",
                    "subscribe": 0,
                }
                await ws.send(json.dumps(req))

                for _ in range(10):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        res = json.loads(raw)

                        # ✅ Debug log to see what Deriv actually returns
                        logger.info(
                            f"Deriv candles raw response for {deriv_symbol}: "
                            f"msg_type={res.get('msg_type')}, "
                            f"has_candles={'candles' in res}, "
                            f"has_error={'error' in res}"
                        )

                        # ── Primary: `candles` array ──
                        if 'candles' in res and isinstance(res['candles'], list):
                            candles = [
                                {
                                    'time': int(c['epoch']),
                                    'open': float(c['open']),
                                    'high': float(c['high']),
                                    'low':  float(c['low']),
                                    'close': float(c['close']),
                                    'volume': float(c.get('volume', 0) or 0),
                                }
                                for c in res['candles']
                            ]
                            logger.info(f"Returning {len(candles)} candles for {deriv_symbol}")
                            return candles

                        # ── Secondary: `history` with prices/times ──
                        if res.get('msg_type') == 'history' and 'history' in res:
                            hist = res['history']
                            prices = hist.get('prices', [])
                            times = hist.get('times', [])
                            if prices and times:
                                logger.info(f"Returning {len(prices)} history ticks as candles")
                                return [
                                    {
                                        'time': int(t),
                                        'open': float(p),
                                        'high': float(p),
                                        'low':  float(p),
                                        'close': float(p),
                                        'volume': 0,
                                    }
                                    for t, p in zip(times, prices)
                                ]

                        # ── Error handling ──
                        if res.get('msg_type') == 'error' or 'error' in res:
                            err = res.get('error', {})
                            logger.warning(
                                f"Deriv candles error for {deriv_symbol}: "
                                f"{err.get('message', err)}"
                            )
                            return []

                    except asyncio.TimeoutError:
                        logger.warning(f"Deriv candles timeout for {deriv_symbol}")
                        return []

        except Exception as e:
            logger.warning(f"Deriv candles fetch failed for {deriv_symbol}: {e}")
            return []

    # ==========================================
    # OHLC AGGREGATION
    # ==========================================
    def aggregate_ticks_to_ohlc(self, ticks_response, interval_minutes=1):
        if not ticks_response:
            return None

        if isinstance(ticks_response, list):
            prices = ticks_response
            now = int(time.time())
            epochs = [now - (len(prices) - i) for i in range(len(prices))]
        elif 'ticks' in ticks_response:
            ticks = ticks_response['ticks']
            prices = [float(t['quote']) for t in ticks]
            epochs = [t['epoch'] for t in ticks]
        else:
            return None

        if not prices:
            return None

        index = pd.to_datetime(epochs, unit='s')
        series = pd.Series(data=prices, index=index)
        ohlc_resampled = series.resample(f'{interval_minutes}min').ohlc()
        ohlc_resampled.columns = [col.lower() for col in ohlc_resampled.columns]
        ohlc_resampled.reset_index(inplace=True)
        ohlc_resampled.dropna(inplace=True)
        return ohlc_resampled.to_dict(orient='records')

    def reset_fallback(self):
        self._use_fallback = False
        self._fallback_started_at = 0
        logger.info("Fallback flag reset - will try live connection")