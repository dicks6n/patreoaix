# store/management/commands/run_background.py
"""
Everything that runs in extra terminals on the laptop, in ONE process for a host
(PythonAnywhere "Always-on task", a Docker worker, systemd …):

    python manage.py run_background                    # order engine every 1 s + AI scanner every 60 s
    python manage.py run_background --no-scanner       # order engine only
    python manage.py run_background --scan-interval 120

  • order engine  (= process_orders)  fills pending orders, S/L, T/P, trailing stops, swaps, stop out,
                                       and keeps live prices fresh in the shared cache for the website
  • AI scanner    (= signal_scanner)   scans every market, records A / A+ setups, notifies subscribers

The scanner runs in its own thread, so a slow scan never delays order handling.
Needs a cache shared between processes (settings.CACHES = file-based) so the website sees the prices.
"""
import threading
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections


class Command(BaseCommand):
    help = 'Order engine + AI signal scanner in one long-running process (for hosting)'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=1.0, help='Seconds between order-engine passes')
        parser.add_argument('--scan-interval', type=int, default=60, help='Seconds between AI scanner passes')
        parser.add_argument('--no-scanner', action='store_true', help='Run the order engine only')

    def handle(self, *args, **opts):
        from store.mt5_service import process_all

        if not opts['no_scanner']:
            threading.Thread(target=self._scanner, args=(opts['scan_interval'],), daemon=True).start()
        self.stdout.write(self.style.SUCCESS('Order engine started'))
        errors = 0
        while True:
            started = time.time()
            try:
                close_old_connections()          # a long-lived process must not keep a dead DB connection
                process_all()
                errors = 0
            except Exception as e:
                errors += 1
                if errors in (1, 10) or errors % 100 == 0:      # log the first, then occasionally
                    self.stderr.write(f'order engine pass failed ({errors}x): {e}')
            time.sleep(max(0.2, opts['interval'] - (time.time() - started)))

    def _scanner(self, interval):
        time.sleep(10)                            # let the price feed warm up first
        self.stdout.write(self.style.SUCCESS('AI signal scanner started'))
        while True:
            started = time.time()
            try:
                close_old_connections()
                call_command('signal_scanner', once=True, stdout=self.stdout, stderr=self.stderr)
            except Exception as e:
                self.stderr.write(f'scanner pass failed: {e}')
            time.sleep(max(10, interval - (time.time() - started)))
