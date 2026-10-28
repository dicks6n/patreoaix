"""
Background AI scanner.

Runs chart_scanner.scan() across all watch symbols on three timeframes
(scalp / intraday / swing), and for every BUY/SELL setup above the
confidence threshold that hasn't already been alerted recently, creates a
Notification for every user with an active subscription.

Run it with:
    python manage.py ai_scanner_worker            # single pass
    python manage.py ai_scanner_worker --loop 60  # run every 60 seconds
"""
import time
from datetime import datetime

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from store import chart_scanner
from store.models import Notification, User


ALERT_TTL_MINUTES = 30        # don't repeat the same signal for this long
MIN_CONFIDENCE     = 72       # only alert when confidence >= this
MIN_SCORE          = 25       # and |score| (blended, -100..100) >= this
SCAN_STYLES        = ('scalp', 'intraday', 'swing', 'position')


class Command(BaseCommand):
    help = 'Scan markets with chart_scanner and push AI trade alerts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop', type=int, default=0,
            help='Repeat every N seconds (0 = single pass)',
        )

    def handle(self, *args, **options):
        loop = int(options.get('loop') or 0)
        if loop > 0:
            self.stdout.write(f'AI scanner worker starting — every {loop}s')
            while True:
                try:
                    self._cycle()
                except Exception as e:
                    self.stderr.write(f'cycle error: {e}')
                time.sleep(loop)
        else:
            self._cycle()

    def _cycle(self):
        started = datetime.now()
        self.stdout.write(f'[{started:%H:%M:%S}] scan start')

        all_opps = []
        for style in SCAN_STYLES:
            try:
                results = chart_scanner.scan(chart_scanner.SCAN_SYMBOLS, style=style)
            except Exception as e:
                self.stderr.write(f'  scan({style}) failed: {e}')
                continue
            for r in results:
                if not r.get('ok'):
                    continue
                if r['signal'] not in ('BUY', 'SELL'):
                    continue
                if r['confidence'] < MIN_CONFIDENCE:
                    continue
                if abs(r['score']) < MIN_SCORE:
                    continue
                all_opps.append({**r, 'style': style})

        if not all_opps:
            self.stdout.write('  no qualifying setups')
            return

        fresh = []
        for o in all_opps:
            key = f'aiworker:{o["symbol"]}:{o["style"]}:{o["signal"]}'
            if cache.get(key):
                continue
            cache.set(key, 1, ALERT_TTL_MINUTES * 60)
            fresh.append(o)

        if not fresh:
            self.stdout.write('  all setups were already alerted recently')
            return

        recipients = list(
            User.objects.filter(
                is_active=True,
                userprofile__is_active_subscription=True,
            ).only('id', 'username')
        )
        if not recipients:
            self.stdout.write('  no subscribed users to notify')
            return

        count = 0
        for o in fresh:
            plan = o.get('plan') or {}
            direction_word = 'BUY' if o['signal'] == 'BUY' else 'SELL'
            entry = plan.get('entry')
            sl    = plan.get('sl')
            tp1   = plan.get('tp1')

            title = f"AI {direction_word} signal: {o['symbol']}"
            message = (
                f"{o['name']} · {o['style']} · {o['confidence']}% confidence\n"
                f"Score {o['score']:+.0f} · RSI {o['rsi']} · ADX {o['adx']}\n"
                f"Entry {entry} · SL {sl} · TP {tp1}"
            )
            action_url = f"/market-overview/?symbol={o['symbol']}&style={o['style']}"

            for user in recipients:
                Notification.objects.create(
                    user=user,
                    title=title,
                    message=message,
                    category='TRADE',
                    priority='warning' if o['confidence'] >= 85 else 'info',
                    action_url=action_url,
                    action_label='Open chart',
                )
                count += 1

        elapsed = (datetime.now() - started).total_seconds()
        self.stdout.write(
            f'  {len(fresh)} fresh setups · {count} notifications · {elapsed:.1f}s'
        )