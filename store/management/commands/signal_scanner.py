# store/management/commands/signal_scanner.py
"""
Background AI signal scanner.

    python manage.py signal_scanner                 # runs forever, every 60 s
    python manage.py signal_scanner --once          # one pass (cron-friendly)
    python manage.py signal_scanner --interval 120 --styles intraday,swing

Each pass runs every scanner on every market for each trading style that has
subscribers (intraday is always scanned), records new A / A+ setups, notifies
subscribed users, and checks open signals against the candles that came after.
"""
import time

from django.core.management.base import BaseCommand

from store import signal_alerts as sa
from store import signal_engine as se
from store.models import SignalAlertSubscription


class Command(BaseCommand):
    help = 'Scan all markets in the background and notify users of strong AI confluence setups'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Run one pass and exit')
        parser.add_argument('--interval', type=int, default=60, help='Seconds between passes (default 60)')
        parser.add_argument('--styles', default='', help='Comma-separated styles to scan (default: intraday + every style with subscribers)')

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS('🧠 AI signal scanner started'))
        while True:
            started = time.time()
            try:
                self.run_pass(opts['styles'])
            except Exception as e:
                self.stderr.write(f'Scanner pass failed: {e}')
            if opts['once']:
                break
            time.sleep(max(5, opts['interval'] - (time.time() - started)))

    def run_pass(self, styles_opt):
        if styles_opt:
            styles = [s.strip() for s in styles_opt.split(',') if s.strip()]
        else:
            styles = sorted({'intraday'} | set(SignalAlertSubscription.objects.filter(enabled=True).values_list('style', flat=True)))
        latest = ([], {})
        for style in styles:
            t0 = time.time()
            results, strength = se.scan_all(style)
            if style == 'intraday':
                latest = (results, strength)
            sa.save_snapshot(style, results, strength, 'worker')
            new = sa.record_signals(results, style)
            from store import telegram_bot
            telegram_bot.safe(telegram_bot.post_scan, results, style)
            strong = sum(1 for r in results if r['strong'])
            self.stdout.write(f'[{time.strftime("%H:%M:%S")}] {style}: {len(results)} markets, {strong} strong, '
                              f'{len(new)} new ({time.time() - t0:.1f}s)')
            for s in new:
                self.stdout.write(self.style.SUCCESS(f'   ➜ {s.grade} {s.direction} {s.symbol} {s.timeframe} @ {s.entry} '
                                                     f'SL {s.stop_loss} TP {s.take_profit} — notified {s.notified}'))
        for s in sa.update_outcomes():
            self.stdout.write(f'   ✓ {s.symbol} {s.direction} {s.outcome} {s.result_r:+.2f}R')
        from store import news_history                     # release history + reactions (at most hourly)
        news_history.safe_tick()
        from store import telegram_schedule                # morning brief, pulse, news alerts, daily / weekly results
        for name in telegram_schedule.safe_run(*latest):
            self.stdout.write(f'   ✈ Telegram {name} posted')
