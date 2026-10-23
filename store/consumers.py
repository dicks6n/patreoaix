# store/consumers.py
import asyncio
import json
import time as _time
import logging
from decimal import Decimal

import websockets as _ws_client

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.conf import settings

from .models import (
    Account, Trade, Asset, Notification, SiteSettings, UserBrokerAccount,
)
from .deriv_service import DerivService
from .ai_engine import ai_engine
from . import price_feed
from . import mt5_service



from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


def broadcast_trade_event(event_type, trade_data, user_id):
    """
    Broadcast a trade event to every connected client of that user.
    Event types: 'trade_opened', 'trade_closed'
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        f'trade_events_user_{user_id}',
        {
            'type': 'trade_event',
            'event': event_type,
            'trade': trade_data,
        }
    )

logger = logging.getLogger(__name__)

DIGIT_CONTRACTS = {
    'DIGITEVEN', 'DIGITODD', 'DIGITOVER', 'DIGITUNDER',
    'DIGITMATCH', 'DIGITDIFF',
}


# ═══════════════════════════════════════════════════════════════
# TRADING CONSUMER (unchanged from your original)
# ═══════════════════════════════════════════════════════════════
class TradingConsumer(AsyncWebsocketConsumer):

    SIGNAL_WATCHLIST = [
        'R_100', 'R_75',
        'frxXAUUSD',
        'frxEURUSD',
        'frxUSDJPY',
    ]
    SIGNAL_INTERVAL_SECONDS = 15

    async def connect(self):
        self.deriv_service = DerivService()
        await self.accept()
        self.signal_task = asyncio.create_task(self.signal_feed_loop())

    async def disconnect(self, close_code):
        task = getattr(self, 'signal_task', None)
        if task:
            task.cancel()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            action = data.get('action')

            if action == 'get_account_balance':
                db_data = await self.get_db_account_data()
                if db_data:
                    await self.send(text_data=json.dumps({
                        'type': 'account_info', 'data': db_data
                    }))
                else:
                    await self.send_error("No site account found for this user.")

            elif action == 'get_deriv_demo_balance' or action == 'get_balance':
                deriv_data = await self.fetch_deriv_demo_balance()

                if isinstance(deriv_data, dict):
                    balance_val = deriv_data.get('balance', deriv_data.get('amount', '0.00'))
                    if isinstance(balance_val, dict):
                        balance_val = balance_val.get('balance', '0.00')
                else:
                    balance_val = str(deriv_data or '0.00')

                await self.send(text_data=json.dumps({
                    'type': 'balance',
                    'balance': str(balance_val),
                    'data': deriv_data
                }))

            elif action == 'predict_even_odd' or action == 'predict':
                symbol = data.get('symbol', 'R_100')
                prediction = await self.generate_ai_prediction(symbol)
                await self.send(text_data=json.dumps({
                    'type': 'ai_prediction', 'result': prediction
                }))

            elif action == 'run_bot':
                await self.execute_site_trade(data)

        except Exception as e:
            logger.warning(f"Error handling consumer receive: {e}")
            await self.send_error("Something went wrong processing that request.")

    @database_sync_to_async
    def get_db_account_data(self):
        try:
            account = Account.objects.filter(
                user=self.scope["user"], account_type='EXCHANGE'
            ).first()

            if not account:
                account = Account.objects.filter(user=self.scope["user"]).first()

            if not account:
                return None

            return {'balance': str(account.balance), 'currency': 'USD'}
        except Exception as e:
            logger.warning(f"Error getting account: {e}")
            return None

    @database_sync_to_async
    def get_site_settings(self):
        return SiteSettings.load()

    @database_sync_to_async
    def get_account(self):
        account = Account.objects.filter(
            user=self.scope["user"], account_type='TRADING'
        ).first()

        if account:
            return account

        account = Account.objects.filter(user=self.scope["user"]).first()
        if account:
            return account

        account = Account.objects.create(
            user=self.scope["user"],
            account_type='TRADING',
            balance=Decimal('0.00')
        )
        return account

    @database_sync_to_async
    def debit_balance(self, stake):
        account = Account.objects.filter(
            user=self.scope["user"], account_type='TRADING'
        ).first()

        if not account:
            account = Account.objects.filter(user=self.scope["user"]).first()

        if not account:
            account = Account.objects.create(
                user=self.scope["user"],
                account_type='TRADING',
                balance=Decimal('0.00')
            )

        account.balance = Decimal(str(account.balance)) - Decimal(str(stake))
        account.save()
        return account.balance

    @database_sync_to_async
    def open_trade(self, user, symbol, contract_type, stake, entry_price,
                   target_digit=None):
        asset, _ = Asset.objects.get_or_create(
            symbol=symbol,
            defaults={
                'name': symbol,
                'asset_type': 'synthetic',
                'price': Decimal(str(entry_price)),
            }
        )

        try:
            parsed_target_digit = int(target_digit) if target_digit is not None else None
        except (TypeError, ValueError):
            parsed_target_digit = None

        return Trade.objects.create(
            user=user,
            asset=asset,
            direction=contract_type,
            stake=Decimal(str(stake)),
            entry_price=Decimal(str(entry_price)),
            target_digit=parsed_target_digit,
            status='OPEN',
        )

    @database_sync_to_async
    def refund_trade(self, trade, stake):
        account = Account.objects.filter(user=trade.user).first()

        if not account:
            account = Account.objects.create(
                user=trade.user,
                account_type='TRADING',
                balance=Decimal('0.00')
            )

        account.balance = Decimal(str(account.balance)) + Decimal(str(stake))
        account.save()
        trade.status = 'CANCELLED'
        trade.save()
        return account.balance

    @database_sync_to_async
    def settle_trade(self, trade, status, payout, settlement_price):
        trade.status = status
        trade.exit_price = Decimal(str(settlement_price))
        trade.payout = payout
        trade.save()

        account = Account.objects.filter(user=trade.user).first()

        if not account:
            account = Account.objects.create(
                user=trade.user,
                account_type='TRADING',
                balance=Decimal('0.00')
            )

        if status == 'WON' and payout > 0:
            account.balance = Decimal(str(account.balance)) + Decimal(str(payout))
            account.save()

        Notification.objects.create(
            user=trade.user,
            title="Trade Closed",
            message=(
                f"Your {trade.asset.symbol} {trade.direction} trade was "
                f"{status.lower()}." + (f" Payout: ${payout}" if status == 'WON' else "")
            )
        )
        return account.balance

    @database_sync_to_async
    def get_deriv_token(self):
        user = self.scope.get("user")
        if user and not isinstance(user, AnonymousUser) and user.is_authenticated:
            account = UserBrokerAccount.objects.filter(
                user=user, broker='DERIV', is_active=True
            ).first()
            if account and account.api_token:
                return account.api_token, account.account_number

        return getattr(settings, 'DERIV_API_TOKEN', None), None

    async def fetch_deriv_demo_balance(self):
        token, account_number = await self.get_deriv_token()
        if not token:
            return {
                'connected': False,
                'message': 'No Deriv account connected.',
                'balance': '0.00',
            }

        info = await self.deriv_service.get_account_info(
            token=token, account_number=account_number or ""
        )

        if isinstance(info, dict) and not info.get('connected'):
            return {'connected': False, 'message': 'Could not reach your Deriv account.', 'balance': None}

        if isinstance(info, dict):
            balance_val = info.get('balance', info.get('amount', '0.00'))
            if isinstance(balance_val, dict):
                balance_val = balance_val.get('balance', '0.00')
            info['balance'] = str(balance_val)
            info['connected'] = True
            return info

        return {
            'connected': True,
            'balance': str(info or '0.00'),
            'amount': str(info or '0.00'),
        }

    async def signal_feed_loop(self):
        try:
            while True:
                for symbol in self.SIGNAL_WATCHLIST:
                    try:
                        await self.push_signal_log(symbol)
                    except Exception as e:
                        logger.warning(f"signal_feed_loop error for {symbol}: {e}")
                await asyncio.sleep(self.SIGNAL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            pass

    async def push_signal_log(self, symbol):
        ticks = await self.deriv_service.get_recent_ticks(symbol=symbol, count=50)
        if not ticks:
            return

        result = ai_engine.predict_all_contracts(symbol, ticks)
        if 'error' in result:
            return

        top = result['top_signal']
        contract = top['contract_type']
        direction_word = 'BUY' if contract in ('CALL', 'DIGITOVER', 'DIGITEVEN') else 'SELL'
        message = f"{direction_word}: {symbol} -> {contract} ({top['confidence']}) @ {result['last_price']}"

        await self.send(text_data=json.dumps({
            'type': 'signal_log',
            'symbol': symbol,
            'level': direction_word,
            'message': message,
            'contract_type': contract,
            'confidence': top['raw_confidence'],
            'last_price': result['last_price'],
            'barrier': top['barrier'],
        }))

    async def generate_ai_prediction(self, symbol):
        ticks = await self.deriv_service.get_recent_ticks(symbol=symbol, count=50)
        result = ai_engine.predict_all_contracts(symbol, ticks)
        if 'error' in result:
            return result

        top = result['top_signal']
        return {
            "symbol": symbol,
            "predicted_contract": top['contract_type'],
            "confidence": top['confidence'],
            "raw_confidence": top['raw_confidence'],
            "last_price": str(result['last_price']),
            "last_digit": result['last_digit'],
            "barrier": top['barrier'],
        }

    async def send_error(self, message):
        await self.send(text_data=json.dumps({
            'type': 'error', 'message': message
        }))

    @staticmethod
    def evaluate_outcome(contract_type, entry_price, entry_digit, settlement_price,
                         settlement_digit, barrier=None, target_digit=None):
        contract_type = (contract_type or '').upper()

        if contract_type == 'CALL':
            return settlement_price > entry_price
        if contract_type == 'PUT':
            return settlement_price < entry_price
        if contract_type == 'DIGITEVEN':
            return settlement_digit % 2 == 0
        if contract_type == 'DIGITODD':
            return settlement_digit % 2 != 0
        if contract_type == 'DIGITOVER':
            b = int(barrier) if barrier is not None else 4
            return settlement_digit > b
        if contract_type == 'DIGITUNDER':
            b = int(barrier) if barrier is not None else 5
            return settlement_digit < b
        if contract_type == 'DIGITMATCH':
            t = int(target_digit) if target_digit is not None else settlement_digit
            return settlement_digit == t
        if contract_type == 'DIGITDIFF':
            t = int(target_digit) if target_digit is not None else -1
            return settlement_digit != t

        return settlement_price > entry_price

    async def execute_site_trade(self, data):
        user = self.scope.get("user")
        if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
            await self.send_error("You must be logged in to trade.")
            return

        settings_obj = await self.get_site_settings()
        if not settings_obj.trading_enabled:
            await self.send_error("Trading is currently paused by the site admin.")
            return

        symbol = data.get('symbol', 'R_100')
        contract_type = (data.get('contract_type') or 'DIGITEVEN').upper()
        barrier = data.get('barrier')
        target_digit = data.get('target_digit')
        duration = data.get('duration', 1)
        try:
            duration = max(1, int(duration))
        except (TypeError, ValueError):
            duration = 1

        requested_amount = data.get('amount')
        try:
            stake = (
                Decimal(str(requested_amount))
                if requested_amount not in (None, '')
                else settings_obj.default_trade_amount
            )
        except Exception:
            stake = settings_obj.default_trade_amount

        if stake < settings_obj.min_trade_amount or stake > settings_obj.max_trade_amount:
            await self.send_error(
                f"Stake must be between {settings_obj.min_trade_amount} and "
                f"{settings_obj.max_trade_amount} USD."
            )
            return

        account = await self.get_account()
        if account is None or Decimal(str(account.balance)) < stake:
            await self.send_error("Insufficient site balance for this trade.")
            return

        entry_ticks = await self.deriv_service.get_settlement_ticks(
            symbol=symbol, count=1, timeout=10
        )
        if not entry_ticks:
            await self.send_error("Could not reach the live price feed. Please try again.")
            return
        entry_price = entry_ticks[0]
        entry_digit = ai_engine.extract_last_digit(entry_price)

        trade = await self.open_trade(user, symbol, contract_type, stake, entry_price, target_digit)
        new_balance = await self.debit_balance(stake)

        await self.send(text_data=json.dumps({
            'type': 'trade_opened',
            'trade_id': trade.id,
            'symbol': symbol,
            'contract_type': contract_type,
            'stake': str(stake),
            'entry_price': entry_price,
            'balance': str(new_balance),
        }))

        settlement_ticks = await self.deriv_service.get_settlement_ticks(
            symbol=symbol, count=duration, timeout=20
        )

        if not settlement_ticks:
            refunded_balance = await self.refund_trade(trade, stake)
            await self.send(text_data=json.dumps({
                'type': 'trade_result',
                'trade_id': trade.id,
                'status': 'CANCELLED',
                'message': 'Live price feed was unavailable — stake refunded.',
                'balance': str(refunded_balance),
            }))
            return

        settlement_price = settlement_ticks[-1]
        settlement_digit = ai_engine.extract_last_digit(settlement_price)

        won = self.evaluate_outcome(
            contract_type, entry_price, entry_digit, settlement_price,
            settlement_digit, barrier, target_digit
        )

        payout = Decimal('0.00')
        if won:
            payout = (
                stake * (Decimal('1') + Decimal(str(settings_obj.payout_percentage)) / Decimal('100'))
            ).quantize(Decimal('0.01'))

        final_balance = await self.settle_trade(
            trade, 'WON' if won else 'LOST', payout, settlement_price
        )

        await self.send(text_data=json.dumps({
            'type': 'trade_result',
            'trade_id': trade.id,
            'status': 'WON' if won else 'LOST',
            'entry_price': entry_price,
            'settlement_price': settlement_price,
            'payout': str(payout),
            'balance': str(final_balance),
        }))


# ═══════════════════════════════════════════════════════════════
# LIVE CHART CONSUMER — UPDATED
#   Crypto    → Binance WS (direct, ~50ms)
#   Metals    → TwelveData WS (real-time streaming) OR REST poll
#   Energy    → TwelveData WS (real-time streaming) OR REST poll
#   Forex     → TwelveData WS (real-time streaming) OR REST poll
#   Synth     → Deriv WS
# ═══════════════════════════════════════════════════════════════
class LiveChartConsumer(AsyncWebsocketConsumer):

    DERIV_MAP = {
        'R_10':  'R_10',
        'R_25':  'R_25',
        'R_50':  'R_50',
        'R_75':  'R_75',
        'R_100': 'R_100',
    }

    async def connect(self):
        await self.accept()

        self.symbol = None
        self.source = None
        self.binance_url = None
        self.yahoo_ticker = None
        self.deriv_stream = None

        self.binance_task = None
        self.yahoo_task = None
        self.deriv_task = None
        self.deriv_ws = None

        self.running = False

        # Market Watch quotes (many symbols, bid/ask from the trade server)
        self.quote_symbols = []
        self.quotes_task = None

        await self.send(json.dumps({
            'type': 'status',
            'message': 'Live chart connected'
        }))

    async def disconnect(self, close_code):
        await self._stop_streams()
        if self.quotes_task:
            self.quotes_task.cancel()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = data.get('action')

        if action == 'subscribe':
            new_symbol = (data.get('symbol') or 'BTCUSD').upper()
            if new_symbol.endswith('USDT'):
                new_symbol = new_symbol[:-1]
            if new_symbol == self.symbol:
                return
            await self._stop_streams()
            self.symbol = new_symbol
            await self._start_streams(new_symbol)

        elif action == 'unsubscribe':
            await self._stop_streams()
            self.symbol = None

        elif action == 'subscribe_quotes':
            symbols = data.get('symbols') or []
            self.quote_symbols = [s for s in (str(x).upper() for x in symbols)
                                  if s in mt5_service.SPECS][:60]
            if self.quote_symbols and not self.quotes_task:
                self.quotes_task = asyncio.create_task(self._stream_quotes())

    # ═══════════════════════════════════════════════════════════
    # MARKET WATCH — bid/ask for every subscribed symbol, once a second.
    # Same quotes the trade server fills at.
    # ═══════════════════════════════════════════════════════════
    async def _stream_quotes(self):
        last = {}
        try:
            while True:
                try:
                    price_feed.refresh_all_async()
                    quotes = mt5_service.quotes_payload(self.quote_symbols)
                except Exception as e:      # one bad refresh must not stop the stream for good
                    logger.warning(f"Quote refresh failed: {e}")
                    quotes = {}
                changed = {s: q for s, q in quotes.items()
                           if last.get(s) != (q['bid'], q['ask'])}
                if changed:
                    for s, q in changed.items():
                        last[s] = (q['bid'], q['ask'])
                    await self.send(json.dumps({'type': 'quotes', 'quotes': changed}))
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Quote stream stopped: {e}")
            self.quotes_task = None

    async def _start_streams(self, symbol):
        self.running = True

        # ── Crypto → Binance WS ──────────────────────────────
        if symbol in price_feed.BINANCE_MAP:
            self.source = 'BINANCE'
            binance_sym = price_feed.BINANCE_MAP[symbol]
            self.binance_url = f"wss://stream.binance.com:9443/ws/{binance_sym}@trade"
            self.binance_task = asyncio.create_task(self._stream_binance())
            await self.send(json.dumps({
                'type': 'status',
                'message': f'Streaming {symbol} from Binance'
            }))

        # ── Metals / Energy / Forex ──────────────────────────
        elif (symbol in price_feed.TWELVEDATA_MAP
              or symbol in price_feed.YAHOO_MAP):

            # Prefer TwelveData WebSocket if we have a key
            if (price_feed.TWELVEDATA_API_KEY
                    and symbol in price_feed.TWELVEDATA_MAP):
                self.source = 'TWELVEDATA'
                self.yahoo_task = asyncio.create_task(self._stream_twelvedata_ws())
                await self.send(json.dumps({
                    'type': 'status',
                    'message': f'Streaming {symbol} from TwelveData WS'
                }))
            else:
                # Fall back to REST polling (OANDA or Yahoo under the hood)
                self.source = price_feed.get_source_for(symbol)
                self.yahoo_task = asyncio.create_task(self._poll_rest())
                await self.send(json.dumps({
                    'type': 'status',
                    'message': f'Streaming {symbol} from {self.source}'
                }))

        # ── Synthetics → Deriv WS ────────────────────────────
        elif symbol in self.DERIV_MAP:
            self.source = 'DERIV'
            self.deriv_stream = self.DERIV_MAP[symbol]
            self.deriv_task = asyncio.create_task(self._stream_deriv())
            await self.send(json.dumps({
                'type': 'status',
                'message': f'Streaming {symbol} from Deriv'
            }))

        else:
            await self.send(json.dumps({
                'type': 'error',
                'message': f'No live feed for {symbol}'
            }))
            self.running = False

    async def _stop_streams(self):
        self.running = False

        for task in (self.binance_task, self.yahoo_task, self.deriv_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        self.binance_task = None
        self.yahoo_task = None
        self.deriv_task = None

        if self.deriv_ws:
            try:
                await self.deriv_ws.close()
            except Exception:
                pass
            self.deriv_ws = None

        self.binance_url = None
        self.yahoo_ticker = None
        self.deriv_stream = None

    # ═══════════════════════════════════════════════════════════
    # BINANCE WS (crypto)
    # ═══════════════════════════════════════════════════════════
    async def _stream_binance(self):
        while self.running:
            try:
                async with _ws_client.connect(self.binance_url) as ws:
                    await self.send(json.dumps({
                        'type': 'status',
                        'message': f'Binance {self.symbol} live'
                    }))

                    async for raw in ws:
                        if not self.running:
                            break
                        payload = json.loads(raw)
                        await self.send(json.dumps({
                            'type': 'tick',
                            'symbol': self.symbol,
                            'price': float(payload['p']),
                            'qty': float(payload.get('q', 0)),
                            'time': payload['T'],
                            'source': 'BINANCE',
                        }))
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self.running:
                    return
                await self.send(json.dumps({
                    'type': 'error',
                    'message': f'Binance dropped: {e}. Retrying…'
                }))
                await asyncio.sleep(3)

    # ═══════════════════════════════════════════════════════════
    # TWELVEDATA WS — real-time for metals/energy/forex
    # ═══════════════════════════════════════════════════════════
    async def _stream_twelvedata_ws(self):
        td_sym = price_feed.TWELVEDATA_MAP.get(self.symbol)
        if not td_sym:
            return

        url = (f"wss://ws.twelvedata.com/v1/quotes/price"
               f"?apikey={price_feed.TWELVEDATA_API_KEY}")
        drops = 0

        while self.running:
            try:
                async with _ws_client.connect(
                    url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # Subscribe
                    await ws.send(json.dumps({
                        'action': 'subscribe',
                        'params': {'symbols': td_sym},
                    }))

                    await self.send(json.dumps({
                        'type': 'status',
                        'message': f'TwelveData WS {self.symbol} live'
                    }))

                    async for raw in ws:
                        if not self.running:
                            break

                        msg = json.loads(raw)

                        # Price event
                        if msg.get('event') == 'price':
                            try:
                                price = float(msg['price'])
                            except (KeyError, ValueError, TypeError):
                                continue

                            await self.send(json.dumps({
                                'type': 'tick',
                                'symbol': self.symbol,
                                'price': price,
                                'time': int(_time.time() * 1000),
                                'source': 'TWELVEDATA',
                            }))

                        # Subscribe confirmation. A symbol the plan doesn't stream comes back
                        # under "fails" with status "ok", and then no price ever arrives.
                        elif msg.get('event') == 'subscribe-status':
                            if msg.get('status') != 'ok' or msg.get('fails'):
                                logger.warning(f"TwelveData WS refused {td_sym}: {msg}")
                                await self.send(json.dumps({
                                    'type': 'status',
                                    'message': f'{self.symbol}: TwelveData stream unavailable, using REST prices'
                                }))
                                await self._poll_rest()
                                return

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self.running:
                    return
                drops += 1
                if drops >= 2:          # keeps failing (plan, network): the REST chain always has a price
                    logger.warning(f"TwelveData WS for {td_sym} failed twice ({e}); polling REST instead")
                    await self._poll_rest()
                    return
                await self.send(json.dumps({
                    'type': 'error',
                    'message': f'TwelveData WS dropped: {e}. Retrying…'
                }))
                await asyncio.sleep(3)

    # ═══════════════════════════════════════════════════════════
    # REST POLLING — fallback for OANDA / Yahoo
    # ═══════════════════════════════════════════════════════════
    async def _poll_rest(self):
        loop = asyncio.get_event_loop()
        # Immediate first tick
        try:
            price = await loop.run_in_executor(
                None, price_feed.get_single_price_preferred, self.symbol
            )
            if price and price > 0:
                await self.send(json.dumps({
                    'type': 'tick',
                    'symbol': self.symbol,
                    'price': price,
                    'time': int(_time.time() * 1000),
                    'source': price_feed.get_source_for(self.symbol),
                }))
        except Exception as e:
            logger.warning(f"Initial tick failed for {self.symbol}: {e}")

        while self.running:
            try:
                await asyncio.sleep(1.0)
                if not self.running:
                    return

                price = await loop.run_in_executor(
                    None, price_feed.get_single_price_preferred, self.symbol
                )
                if price and price > 0:
                    await self.send(json.dumps({
                        'type': 'tick',
                        'symbol': self.symbol,
                        'price': price,
                        'time': int(_time.time() * 1000),
                        'source': price_feed.get_source_for(self.symbol),
                    }))
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Poll error for {self.symbol}: {e}")
                await asyncio.sleep(2)

    # ═══════════════════════════════════════════════════════════
    # DERIV WS (synthetics)
    # ═══════════════════════════════════════════════════════════
    async def _stream_deriv(self):
        while self.running:
            try:
                self.deriv_ws = await _ws_client.connect(
                    "wss://ws.derivws.com/websockets/v3?app_id=1089",
                    open_timeout=8,
                    ping_interval=20,
                    ping_timeout=10,
                )

                await self.deriv_ws.send(json.dumps({
                    "ticks_history": self.deriv_stream,
                    "adjust_start_time": 1,
                    "count": 1,
                    "end": "latest",
                    "style": "ticks",
                }))
                await self.deriv_ws.send(json.dumps({
                    "ticks": self.deriv_stream,
                    "subscribe": 1,
                }))

                await self.send(json.dumps({
                    'type': 'status',
                    'message': f'Deriv {self.symbol} live'
                }))

                while self.running:
                    try:
                        raw = await asyncio.wait_for(self.deriv_ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            await self.deriv_ws.send(json.dumps({"ping": 1}))
                        except Exception:
                            break
                        continue

                    data = json.loads(raw)

                    if data.get('msg_type') == 'history' and 'history' in data:
                        prices = data['history'].get('prices', [])
                        times = data['history'].get('times', [])
                        if prices:
                            await self.send(json.dumps({
                                'type': 'tick',
                                'symbol': self.symbol,
                                'price': float(prices[-1]),
                                'time': int(times[-1] * 1000) if times else None,
                                'source': 'DERIV',
                            }))
                        continue

                    if data.get('msg_type') == 'tick' and 'tick' in data:
                        tick = data['tick']
                        await self.send(json.dumps({
                            'type': 'tick',
                            'symbol': self.symbol,
                            'price': float(tick['quote']),
                            'time': int(tick['epoch'] * 1000),
                            'source': 'DERIV',
                        }))
                        continue

                    if data.get('msg_type') == 'error':
                        await self.send(json.dumps({
                            'type': 'error',
                            'message': data.get('error', {}).get('message', 'Deriv error')
                        }))
                        await asyncio.sleep(2)
                        break

                    if data.get('msg_type') == 'pong':
                        continue

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self.running:
                    return
                await self.send(json.dumps({
                    'type': 'error',
                    'message': f'Deriv dropped: {e}. Retrying…'
                }))
                await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════
# TRADE EVENTS CONSUMER — real-time trade open/close sync
# ═══════════════════════════════════════════════════════════════
class TradeEventConsumer(AsyncWebsocketConsumer):
    """
    Broadcasts trade open/close events to the user's connected clients.
    Any page (trade.html, mt5_mobile.html, scanner) subscribed here
    gets notified the instant a trade happens.
    """

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.group_name = f'trade_events_user_{user.id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def trade_event(self, event):
        await self.send(text_data=json.dumps({
            'type': 'trade_event',
            'event': event['event'],
            'trade': event['trade'],
        }))