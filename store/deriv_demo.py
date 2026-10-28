# store/deriv_demo.py
"""
Demo trading on the user's own Deriv DEMO (virtual money) account from the digits page.

Flow (one websocket per trade):
    new Deriv API (Personal Access Token / "Connect Deriv" login): REST OTP → the demo account's own socket
    legacy a1- API token: authorize on the legacy socket (Deriv retired that host in 2026; kept as a fallback)
    → proposal (price of the contract) → buy → proposal_open_contract until sold → balance

Real-money accounts (the customer's own Deriv real account) are used only when all of these hold:
  • the site owner turned it on: DERIV_REAL_TRADING=True and TOKEN_ENCRYPTION_KEY set (tokens encrypted),
  • the customer accepted the real-money risk warning for that account (UserBrokerAccount.real_enabled_at),
  • every real request says so explicitly (the views require confirm_real), within DERIV_REAL_MAX_STAKE.
A demo account is checked to really be virtual on every trade (authorize.is_virtual), and vice versa.
"""
import json
import logging
import time

from . import price_feed
from .deriv_service import is_legacy_token

logger = logging.getLogger(__name__)

VOLATILITY = ('R_10', 'R_25', 'R_50', 'R_75', 'R_100', '1HZ10V', '1HZ25V', '1HZ50V', '1HZ75V', '1HZ100V')
CONTRACTS = {'DIGITMATCH', 'DIGITDIFF', 'DIGITEVEN', 'DIGITODD', 'DIGITOVER', 'DIGITUNDER'}
NEEDS_DIGIT = {'DIGITMATCH', 'DIGITDIFF', 'DIGITOVER', 'DIGITUNDER'}
MIN_STAKE, MAX_STAKE = 0.35, 1000
MAX_TICKS = 10                    # Deriv digit contracts run 1-10 ticks
PIP_SIZE = {'R_10': 3, 'R_25': 3, 'R_50': 4, 'R_75': 4, 'R_100': 2,          # decimals Deriv quotes them with
            '1HZ10V': 2, '1HZ25V': 2, '1HZ50V': 2, '1HZ75V': 2, '1HZ100V': 2}


class DemoTradeError(Exception):
    """Shown to the trader as-is."""


def _accounts(user, kind):
    from .models import UserBrokerAccount
    rows = UserBrokerAccount.objects.filter(user=user, broker='DERIV', is_active=True, server_name=kind) \
        .exclude(api_token__isnull=True).exclude(api_token='')
    return [a for a in rows if a.api_token]           # a token that can't be decrypted reads as ''


def demo_account(user):
    """The user's connected Deriv demo account (with a token), or None."""
    rows = _accounts(user, 'Deriv Demo')               # set from Deriv's own account list (is_demo / is_virtual)
    return rows[0] if rows else None


def real_account(user):
    """The user's connected Deriv real-money account (USD first), or None."""
    rows = _accounts(user, 'Deriv Real')
    rows.sort(key=lambda a: (str(getattr(a, 'last_balance_currency', '') or 'USD').upper() != 'USD', a.id))
    return rows[0] if rows else None


def real_allowed():
    """Owner switch + encrypted tokens. Without both, real-money trading is off for everyone."""
    from django.conf import settings
    from .fields import encryption_on
    return str(getattr(settings, 'DERIV_REAL_TRADING', 'False')) == 'True' and encryption_on()


def real_max_stake():
    from django.conf import settings
    try:
        return max(0.35, float(getattr(settings, 'DERIV_REAL_MAX_STAKE', 50) or 50))
    except (TypeError, ValueError):
        return 50.0


def is_real(acc):
    return getattr(acc, 'server_name', '') == 'Deriv Real'


def account_for(user, mode):
    return real_account(user) if mode == 'real' else demo_account(user)


def _check_stake(acc, stake):
    if is_real(acc) and stake > real_max_stake():
        raise DemoTradeError(f'Real-money stakes are limited to {real_max_stake():.2f} per contract on this site.')


class _Session:
    """One Deriv trading websocket for the demo account; every request waits for its own req_id."""

    def __init__(self, acc, timeout=10):
        token = acc.api_token
        self.req = 0
        self.legacy = is_legacy_token(token)
        if self.legacy:
            try:
                self.ws = price_feed.ws_connect(price_feed.DERIV_PUBLIC_WS_LEGACY, timeout=timeout)
            except Exception as e:
                raise DemoTradeError(f'Legacy "a1-" tokens need Deriv\'s old API, which is not reachable ({e}). '
                                     'Reconnect Deriv on the dashboard with "Connect Deriv" or a Personal Access Token.')
            self.auth = self.call({'authorize': token}, timeout=timeout)['authorize']
            return
        # new API: the account's own socket from a one-time password
        from .deriv_service import DerivService
        svc = DerivService()
        if not svc.app_id:
            raise DemoTradeError('Set DERIV_APP_ID (your Deriv app ID) in .env: the new Deriv API needs it to open the demo account.')
        url = svc._get_authenticated_ws_url(token, acc.account_number)
        if not url:
            raise DemoTradeError(f"Deriv did not open the demo account {acc.account_number}: {svc.last_error or 'no socket returned'}")
        try:
            self.ws = price_feed.ws_connect(url, timeout=timeout)
        except Exception as e:
            raise DemoTradeError(f'Could not reach Deriv: {e}')
        bal = self.call({'balance': 1}, timeout=timeout).get('balance') or {}
        self.auth = {'loginid': bal.get('loginid') or acc.account_number, 'balance': bal.get('balance'),
                     'currency': bal.get('currency') or 'USD', 'is_virtual': not is_real(acc)}   # per Deriv's account list

    def call(self, request, timeout=10):
        self.req += 1
        rid = self.req
        self.ws.send(json.dumps(dict(request, req_id=rid)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get('req_id') != rid:
                continue
            if msg.get('error'):
                raise DemoTradeError(msg['error'].get('message') or msg['error'].get('code') or 'Deriv error')
            return msg
        raise DemoTradeError('Deriv did not answer in time')

    def contract(self, request):
        """A proposal in this API's dialect: the new Deriv API renamed `symbol` to `underlying_symbol`."""
        request = dict(request)
        if not self.legacy and 'symbol' in request:
            request['underlying_symbol'] = request.pop('symbol')
        return request

    def buy(self, proposal_request, amount, timeout=15):
        """Price the contract, then buy it. New-API proposals may only carry the id: pay at most the stake."""
        p = self.call(self.contract(proposal_request))['proposal']
        return self.call({'buy': p['id'], 'price': p.get('ask_price') or amount}, timeout=timeout)['buy']

    def send(self, request):
        self.req += 1
        self.ws.send(json.dumps(dict(request, req_id=self.req)))
        return self.req

    def recv(self):
        return json.loads(self.ws.recv())

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _require_mode(acc, auth):
    """The account is what the customer picked: demo really virtual, real really real, and real allowed."""
    if not is_real(acc):
        if not auth.get('is_virtual'):
            raise DemoTradeError(f"The connected token belongs to the REAL account {auth.get('loginid')}. "
                                 'Demo trading only runs on a demo (virtual) account: create the token on your demo account.')
        return
    if not real_allowed():
        raise DemoTradeError('Real-money trading is not enabled on this site.')
    if not getattr(acc, 'real_enabled_at', None):
        raise DemoTradeError('Turn on real-money trading for your account first (accept the risk warning).')
    if auth.get('is_virtual'):
        raise DemoTradeError(f"{auth.get('loginid')} is a demo account, not a real one.")


def account_info(acc):
    """{loginid, balance, currency} of the demo account, straight from Deriv."""
    s = _Session(acc)
    try:
        _require_mode(acc, s.auth)
        return {'loginid': s.auth.get('loginid'), 'balance': float(s.auth.get('balance') or 0),
                'currency': s.auth.get('currency') or 'USD'}
    finally:
        s.close()


def _digit(display_value):
    """Last digit of a tick as Deriv shows it (the display value keeps trailing zeros)."""
    text = str(display_value or '').strip()
    return int(text[-1]) if text and text[-1].isdigit() else None


def place_trade(acc, symbol, contract_type, stake, duration, barrier=None):
    """
    Buy one digit contract on the demo account and wait for Deriv to settle it.
    Returns the same fields as the site's own digit trades (won, payout, tick_digits, final_digit,
    duration_used, new_balance) plus Deriv's contract_id, loginid, currency, buy_price, profit.
    """
    contract_type = (contract_type or '').upper()
    if symbol not in VOLATILITY:
        raise DemoTradeError('Deriv digit trading runs on the Volatility indices.')
    if contract_type not in CONTRACTS:
        raise DemoTradeError(f'Unknown contract type {contract_type}.')
    try:
        stake = round(float(stake), 2)
        duration = int(duration)
    except (TypeError, ValueError):
        raise DemoTradeError('Invalid stake or duration.')
    if not MIN_STAKE <= stake <= MAX_STAKE:
        raise DemoTradeError(f'Stake must be between {MIN_STAKE} and {MAX_STAKE}.')
    _check_stake(acc, stake)
    if not 1 <= duration <= MAX_TICKS:
        raise DemoTradeError(f'Deriv digit contracts run 1 to {MAX_TICKS} ticks.')
    proposal = {'proposal': 1, 'amount': stake, 'basis': 'stake', 'contract_type': contract_type,
                'duration': duration, 'duration_unit': 't', 'symbol': symbol}
    if contract_type in NEEDS_DIGIT:
        if barrier is None or str(barrier) == '':
            raise DemoTradeError('Pick a digit first.')
        proposal['barrier'] = str(int(barrier))

    s = _Session(acc)
    try:
        _require_mode(acc, s.auth)
        proposal['currency'] = s.auth.get('currency') or 'USD'
        b = s.buy(proposal, stake)
        cid = b['contract_id']

        # follow the contract until Deriv settles it
        rid = s.send({'proposal_open_contract': 1, 'contract_id': cid, 'subscribe': 1})
        deadline = time.time() + duration * 3 + 20
        poc = {}
        while time.time() < deadline:
            msg = s.recv()
            if msg.get('req_id') != rid:
                continue
            if msg.get('error'):
                raise DemoTradeError(msg['error'].get('message') or 'Deriv error while settling')
            poc = msg.get('proposal_open_contract') or {}
            if poc.get('is_sold') or poc.get('status') in ('won', 'lost'):
                break
        else:
            raise DemoTradeError(f'Contract {cid} bought; Deriv had not settled it after {duration * 3 + 20}s. '
                                 'Check its result in your Deriv statement.')

        balance = s.call({'balance': 1}).get('balance', {}).get('balance')
        pip = PIP_SIZE.get(symbol, 2)
        shown = lambda disp, raw: disp if disp not in (None, '') else (f'{float(raw):.{pip}f}' if raw not in (None, '') else None)
        ticks = poc.get('tick_stream') or []
        tick_digits = [d for d in (_digit(shown(t.get('tick_display_value'), t.get('tick'))) for t in ticks) if d is not None]
        exit_shown = shown(poc.get('exit_tick_display_value'), poc.get('exit_tick'))
        final = _digit(exit_shown) if exit_shown else (tick_digits[-1] if tick_digits else None)
        sell = float(poc.get('sell_price') or 0)
        won = poc.get('status') == 'won' or (poc.get('status') is None and sell > 0)
        return {
            'success': True, 'account': 'deriv_real' if is_real(acc) else 'deriv_demo', 'loginid': s.auth.get('loginid'), 'currency': proposal['currency'],
            'contract_id': cid, 'transaction_id': b.get('transaction_id'), 'longcode': b.get('longcode'),
            'buy_price': float(b.get('buy_price') or stake), 'won': bool(won), 'payout': sell,
            'profit': round(sell - float(b.get('buy_price') or stake), 2),
            'tick_digits': tick_digits, 'final_digit': final, 'duration_used': duration,
            'entry_tick': shown(poc.get('entry_tick_display_value'), poc.get('entry_tick')), 'exit_tick': exit_shown,
            'new_balance': float(balance) if balance is not None else None,
        }
    finally:
        s.close()


# ═══════════════════════════════════════════════════════════════
# MULTIPLIERS (trade.html): BUY = MULTUP, SELL = MULTDOWN on the demo account
# ═══════════════════════════════════════════════════════════════
DEFAULT_MULTIPLIERS = [50, 100, 200, 300, 500]


def deriv_symbol(symbol):
    """Site symbol → Deriv underlying: XAUUSD → frxXAUUSD, BTCUSD → cryBTCUSD, R_100 → R_100."""
    symbol = (symbol or '').upper()
    if symbol in VOLATILITY or symbol.startswith(('R_', '1HZ')):
        return symbol
    return price_feed.DERIV_CANDLE_MAP.get(symbol) or ('frx' + symbol)


def multiplier_options(symbol):
    """Multipliers Deriv allows on this underlying (from contracts_for on the public socket), cached 1 h."""
    from django.core.cache import cache
    dsym = deriv_symbol(symbol)
    key = f'deriv:mult:{dsym}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    opts = []
    try:
        ws = price_feed.deriv_connect(timeout=8)
        try:
            ws.send(json.dumps({'contracts_for': dsym, 'currency': 'USD', 'req_id': 31}))
            deadline = time.time() + 8
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get('req_id') != 31:
                    continue
                for c in (msg.get('contracts_for') or {}).get('available') or []:
                    if c.get('contract_type') == 'MULTUP' and c.get('multiplier_range'):
                        opts = sorted({int(m) for m in c['multiplier_range']})
                        break
                break
        finally:
            ws.close()
    except Exception as e:
        logger.warning(f'contracts_for {dsym}: {e}')
    cache.set(key, opts, 3600 if opts else 120)
    return opts


def _poc(s, contract_id):
    return s.call({'proposal_open_contract': 1, 'contract_id': contract_id}).get('proposal_open_contract') or {}


def _position(poc):
    side = 'BUY' if poc.get('contract_type') == 'MULTUP' else 'SELL'
    lo = poc.get('limit_order') or {}
    amount = lambda k: (lo.get(k) or {}).get('order_amount')
    return {
        'contract_id': poc.get('contract_id'), 'side': side, 'symbol': poc.get('underlying_symbol') or poc.get('underlying'),
        'display_name': poc.get('display_name') or poc.get('underlying_symbol') or poc.get('underlying'),
        'stake': float(poc.get('buy_price') or 0), 'multiplier': poc.get('multiplier'),
        'entry': poc.get('entry_spot_display_value') or poc.get('entry_spot'),
        'current': poc.get('current_spot_display_value') or poc.get('current_spot'),
        'profit': float(poc.get('profit') or 0), 'bid_price': float(poc.get('bid_price') or 0),
        'stop_loss': amount('stop_loss'), 'take_profit': amount('take_profit'),
        'stop_out': amount('stop_out'), 'can_sell': bool(poc.get('is_valid_to_sell')),
        'is_sold': bool(poc.get('is_sold')), 'status': poc.get('status'),
        'opened': poc.get('date_start'),
    }


def open_multiplier(acc, symbol, side, stake, multiplier, stop_loss=None, take_profit=None):
    side = (side or '').upper()
    if side not in ('BUY', 'SELL'):
        raise DemoTradeError('Side must be BUY or SELL.')
    try:
        stake = round(float(stake), 2)
        multiplier = int(multiplier)
    except (TypeError, ValueError):
        raise DemoTradeError('Invalid stake or multiplier.')
    if not 1 <= stake <= 2000:
        raise DemoTradeError('Stake must be between 1 and 2000 USD.')
    _check_stake(acc, stake)
    proposal = {'proposal': 1, 'amount': stake, 'basis': 'stake', 'symbol': deriv_symbol(symbol),
                'contract_type': 'MULTUP' if side == 'BUY' else 'MULTDOWN', 'multiplier': multiplier}
    limit = {}
    for k, v in (('stop_loss', stop_loss), ('take_profit', take_profit)):
        if v not in (None, '', 0, '0'):
            try:
                v = round(float(v), 2)
            except (TypeError, ValueError):
                raise DemoTradeError(f'Invalid {k.replace("_", " ")} amount.')
            if v <= 0:
                raise DemoTradeError(f'{k.replace("_", " ").capitalize()} is an amount in USD and must be above 0.')
            limit[k] = v
    if limit:
        proposal['limit_order'] = limit
    s = _Session(acc)
    try:
        _require_mode(acc, s.auth)
        proposal['currency'] = s.auth.get('currency') or 'USD'
        b = s.buy(proposal, stake)
        pos = _position(_poc(s, b['contract_id']))
        balance = s.call({'balance': 1}).get('balance', {}).get('balance')
        return {'success': True, 'account': 'deriv_real' if is_real(acc) else 'deriv_demo', 'loginid': s.auth.get('loginid'), 'currency': proposal['currency'],
                'contract_id': b['contract_id'], 'buy_price': float(b.get('buy_price') or stake), 'longcode': b.get('longcode'),
                'position': pos, 'new_balance': float(balance) if balance is not None else None}
    finally:
        s.close()


def open_multipliers(acc):
    """Every open Multipliers contract on the demo account, with live profit."""
    s = _Session(acc)
    try:
        _require_mode(acc, s.auth)
        contracts = (s.call({'portfolio': 1}).get('portfolio') or {}).get('contracts') or []
        out = [_position(_poc(s, c['contract_id'])) for c in contracts
               if str(c.get('contract_type', '')).startswith('MULT')]
        return {'success': True, 'loginid': s.auth.get('loginid'), 'currency': s.auth.get('currency') or 'USD',
                'balance': float(s.auth.get('balance') or 0) if s.auth.get('balance') is not None else None,
                'positions': [p for p in out if not p['is_sold']]}
    finally:
        s.close()


def close_multiplier(acc, contract_id):
    """Sell (close) one open contract at the market price."""
    try:
        contract_id = int(contract_id)
    except (TypeError, ValueError):
        raise DemoTradeError('Invalid contract.')
    s = _Session(acc)
    try:
        _require_mode(acc, s.auth)
        sold = s.call({'sell': contract_id, 'price': 0}, timeout=15)['sell']
        pos = _position(_poc(s, contract_id))
        balance = s.call({'balance': 1}).get('balance', {}).get('balance')
        return {'success': True, 'contract_id': contract_id, 'sold_for': float(sold.get('sold_for') or 0),
                'profit': pos.get('profit'), 'new_balance': float(balance) if balance is not None else None}
    finally:
        s.close()
