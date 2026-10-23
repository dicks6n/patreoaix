# store/management/commands/run_deriv_stream.py
import asyncio
import json
import websockets
from django.core.management.base import BaseCommand
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

DERIV_APP_ID = "34mjct1dBCmYao65TsMoD"  # Demo App ID (replace with yours)
DERIV_WS_URL = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

class Command(BaseCommand):
    help = "Connects to Deriv WebSocket and streams real-time ticks to Django Channels"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting Real-time Deriv Websocket Worker..."))
        asyncio.run(self.stream_deriv_ticks())

    async def stream_deriv_ticks(self):
        channel_layer = get_channel_layer()

        async with websockets.connect(DERIV_WS_URL) as ws:
            # 1. Subscribe to Live Price Ticks for Gold (frxXAUUSD) or Volatility 100 (R_100)
            subscribe_request = {
                "ticks": "frxXAUUSD"
            }
            await ws.send(json.dumps(subscribe_request))

            # 2. Continuous Loop listening for new ticks
            while True:
                response = await ws.recv()
                data = json.loads(response)

                if "tick" in data:
                    tick_data = data["tick"]
                    symbol = tick_data["symbol"]
                    price = float(tick_data["quote"])
                    
                    # Compute Last Digit (for Digit Strategies)
                    price_str = f"{price:.2f}".replace(".", "")
                    last_digit = int(price_str[-1]) if price_str else 0

                    # Mock AI Live Evaluation (Incorporate your AI model logic here)
                    mock_signal = "BUY" if last_digit % 2 == 0 else "SELL"
                    mock_confidence = 82.5

                    # 3. Broadcast real-time data to connected dashboard clients
                    await channel_layer.group_send(
                        "live_trading_group",
                        {
                            "type": "broadcast_tick",
                            "symbol": symbol,
                            "price": price,
                            "last_digit": last_digit,
                            "signal": mock_signal,
                            "confidence": mock_confidence,
                        }
                    )