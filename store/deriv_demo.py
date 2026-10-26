# store/deriv_demo.py
"""
Demo trading on the user's own Deriv DEMO (virtual money) account from the digits page.

Flow over Deriv's API (one websocket per trade, the same connection the live prices use):
    authorize (the user's a1- API token) → refuse unless the account is virtual
    → proposal (price of the contract) → buy → proposal_open_contract until sold → balance

Real-money accounts are refused on purpose: the token is checked on every trade (authorize.is_virtual).
"""
import json
import logging
import time

from . import price_feed
from .deriv_service import is_legacy_token, looks_demo

logger = logging.getLogger(__name__)

VOLATILITY = ('R_10', 'R_25', 'R_50', 'R_75', 'R_100', '1HZ10V', '1HZ25V', '1HZ50V', '1HZ75V', '1HZ100V')
CONTRACTS = {'DIGITMATCH', 'DIGITDIFF', 'DIGITEVEN', 'DIGITODD', 'DIGITOVER', 'DIGITUNDER'}
NEEDS_DIGIT = {'DIGITMATCH', 'DIGITDIFF', 'DIGITOVER', 'DIGITUNDER'}
MIN_STAKE, MAX_STAKE = 0.35, 1000
MAX_TICKS = 10                    # Deriv digit contracts run 1-10 ticks


class DemoTradeError(Exception):
    """Shown to the trader as-is."""


def demo_account(user):
    """The user's connected Deriv demo account (with a token), or None."""
    from .models import UserBrokerAccount
    for acc in UserBrokerAccount.objects.filter(user=user, broker='DERIV', is_active=True).exclude(api_token__isnull=True).exclude(api_token=''):
        if acc.server_name == 'Deriv Demo' or looks_demo(acc.account_number):
            return acc
    return None


class _Session:
    """One authorized Deriv websocket; every request waits for its own req_id."""

    def __init__(self, token, timeout=10):
        if not is_legacy_token(token):
            raise DemoTradeError('Demo trading needs a Deriv API token (it starts with "a1-") created on your demo account. '
                                 'Make one in Deriv → Settings → API token with the Trade scope, and connect it on your dashboard.')
        try:
            self.ws = price_feed.ws_connect(price_feed.DERIV_PUBLIC_WS, timeout=timeout)
        except Exception as e:
            raise DemoTradeError(f'Could not reach Deriv: {e}')
        self.req = 0
        self.auth = self.call({'authorize': token}, timeout=timeout)['authorize']

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


def _require_demo(auth):
    if not auth.get('is_virtual'):
        raise DemoTradeError(f"The connected token belongs to the REAL account {auth.get('loginid')}. "
                             'Demo trading only runs on a demo (virtual) account: create the token on your demo account.')


def account_info(acc):
    """{loginid, balance, currency} of the demo account, straight from Deriv."""
    s = _Session(acc.api_token)
    try:
        _require_demo(s.auth)
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
        raise DemoTradeError('Demo digit trading runs on the Volatility indices.')
    if contract_type not in CONTRACTS:
        raise DemoTradeError(f'Unknown contract type {contract_type}.')
    try:
        stake = round(float(stake), 2)
        duration = int(duration)
    except (TypeError, ValueError):
        raise DemoTradeError('Invalid stake or duration.')
    if not MIN_STAKE <= stake <= MAX_STAKE:
        raise DemoTradeError(f'Stake must be between {MIN_STAKE} and {MAX_STAKE}.')
    if not 1 <= duration <= MAX_TICKS:
        raise DemoTradeError(f'Deriv digit contracts run 1 to {MAX_TICKS} ticks.')
    proposal = {'proposal': 1, 'amount': stake, 'basis': 'stake', 'contract_type': contract_type,
                'duration': duration, 'duration_unit': 't', 'symbol': symbol}
    if contract_type in NEEDS_DIGIT:
        if barrier is None or str(barrier) == '':
            raise DemoTradeError('Pick a digit first.')
        proposal['barrier'] = str(int(barrier))

    s = _Session(acc.api_token)
    try:
        _require_demo(s.auth)
        proposal['currency'] = s.auth.get('currency') or 'USD'
        p = s.call(proposal)['proposal']
        b = s.call({'buy': p['id'], 'price': p['ask_price']}, timeout=15)['buy']
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
        ticks = poc.get('tick_stream') or []
        tick_digits = [d for d in (_digit(t.get('tick_display_value')) for t in ticks) if d is not None]
        final = _digit(poc.get('exit_tick_display_value')) if poc.get('exit_tick_display_value') else (tick_digits[-1] if tick_digits else None)
        sell = float(poc.get('sell_price') or 0)
        won = poc.get('status') == 'won' or (poc.get('status') is None and sell > 0)
        return {
            'success': True, 'account': 'deriv_demo', 'loginid': s.auth.get('loginid'), 'currency': proposal['currency'],
            'contract_id': cid, 'transaction_id': b.get('transaction_id'), 'longcode': b.get('longcode'),
            'buy_price': float(b.get('buy_price') or stake), 'won': bool(won), 'payout': sell,
            'profit': round(sell - float(b.get('buy_price') or stake), 2),
            'tick_digits': tick_digits, 'final_digit': final, 'duration_used': duration,
            'entry_tick': poc.get('entry_tick_display_value'), 'exit_tick': poc.get('exit_tick_display_value'),
            'new_balance': float(balance) if balance is not None else None,
        }
    finally:
        s.close()
