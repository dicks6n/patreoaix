import time
import json
import threading
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
import websocket
from store.models import Trade


# ==========================================
# SYMBOL MAP — internal → Deriv symbol
# ==========================================
SYMBOL_MAP = {
    'XAUUSD': 'frxXAUUSD',
    'USOIL': 'frxXBRUSD',
    'EURUSD': 'frxEURUSD',
    'EURGBP': 'frxEURGBP',
    'EURJPY': 'frxEURJPY',
    'GBPUSD': 'frxGBPUSD',
    'GBPJPY': 'frxGBPJPY',
    'USDJPY': 'frxUSDJPY',
    'BTCUSD': 'cryBTCUSD',
    'R_10': 'R_10',
    'R_25': 'R_25',
    'R_50': 'R_50',
    'R_75': 'R_75',
    'R_100': 'R_100',
}

REVERSE_MAP = {v: k for k, v in SYMBOL_MAP.items()}

# In-memory price cache updated by the WebSocket thread
PRICE_CACHE = {}


def get_multiplier(symbol):
    """Contract size multiplier for each symbol type."""
    if symbol == 'XAUUSD':
        return 100
    if symbol == 'BTCUSD':
        return 1
    if symbol == 'USOIL':
        return 1000
    if 'JPY' in symbol:
        return 1000
    if symbol.startswith('R_'):
        return 1
    return 100000


def calculate_pnl(symbol, direction, volume, entry, current):
    """Calculate unrealized P&L for a position."""
    diff = (current - entry) if direction == 'BUY' else (entry - current)
    return diff * volume * get_multiplier(symbol)


class Command(BaseCommand):
    help = 'Continuously track live prices and update open trade P&L'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('🚀 Starting price tracker...'))

        # Start WebSocket in background thread
        ws_thread = threading.Thread(target=self.run_websocket, daemon=True)
        ws_thread.start()

        # Main loop: push cached prices to DB every 2 seconds
        while True:
            try:
                self.update_open_trades()
            except Exception as e:
                self.stderr.write(f'DB update error: {e}')
            time.sleep(2)

    # ==========================================
    # WEBSOCKET THREAD
    # ==========================================
    def run_websocket(self):
        def on_open(ws):
            self.stdout.write(self.style.SUCCESS('✅ Deriv WebSocket connected'))
            for deriv_symbol in SYMBOL_MAP.values():
                ws.send(json.dumps({'ticks': deriv_symbol, 'subscribe': 1}))

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data.get('msg_type') == 'tick':
                    tick = data['tick']
                    internal = REVERSE_MAP.get(tick['symbol'])
                    if internal:
                        PRICE_CACHE[internal] = float(tick['quote'])
            except Exception:
                pass

        def on_error(ws, error):
            self.stderr.write(f'WS error: {error}')

        def on_close(ws, *args):
            self.stdout.write(self.style.WARNING('⚠️ WS closed — reconnecting in 5s'))

        # Outer while-loop keeps the WebSocket alive forever
        while True:
            try:
                ws = websocket.WebSocketApp(
                    'wss://ws.derivws.com/websockets/v3?app_id=1089',
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                ws.run_forever()
            except Exception as e:
                self.stderr.write(f'WS run error: {e}')
            time.sleep(5)

    # ==========================================
    # DB SYNC LOOP
    # ==========================================
    def update_open_trades(self):
        open_trades = Trade.objects.filter(status='OPEN')
        if not open_trades.exists():
            return

        now = timezone.now()

        with transaction.atomic():
            for trade in open_trades:
                symbol = trade.asset.symbol if trade.asset else None
                if not symbol or symbol not in PRICE_CACHE:
                    continue

                current_price = Decimal(str(PRICE_CACHE[symbol]))
                entry = Decimal(str(trade.entry_price or 0))
                volume = Decimal(str(trade.stake or 0))

                pnl = calculate_pnl(
                    symbol,
                    trade.direction,
                    float(volume),
                    float(entry),
                    float(current_price)
                )

                trade.current_price = current_price
                trade.pnl = Decimal(str(round(pnl, 2)))
                trade.last_price_update = now
                trade.save(update_fields=['current_price', 'pnl', 'last_price_update'])