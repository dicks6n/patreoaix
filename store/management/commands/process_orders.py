# store/management/commands/process_orders.py
# Run alongside price_tracker:   python manage.py process_orders
import time
from django.core.management.base import BaseCommand
from store.mt5_service import process_all


class Command(BaseCommand):
    help = "MT5 order engine: fills pending orders, executes S/L + T/P, runs stop-out."

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=1.0, help='Seconds between passes')
        parser.add_argument('--once', action='store_true', help='Run a single pass and exit')

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS('Order engine started'))
        while True:
            try:
                process_all()
            except Exception as e:
                self.stderr.write(f'pass failed: {e}')
            if opts['once']:
                break
            time.sleep(opts['interval'])