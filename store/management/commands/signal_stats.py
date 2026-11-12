"""
python manage.py signal_stats              → how the published AI signals ended (last 30 days)
python manage.py signal_stats --days 90    → a longer window

Shows per grade and style: wins (TP), stops (SL), expired, win rate, average R, and how FAST the stops came
(stopped within 3 candles = the entry or the stop was wrong, not the direction). Run it before and after an
engine change to compare (each signal stores the engine's result, nothing is recalculated).
"""
from collections import defaultdict
from datetime import timedelta
from statistics import median

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import ScannerSignal

TF_SEC = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}


class Command(BaseCommand):
    help = 'Win rate, average R and fast stop-outs of the published AI signals'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--since', help='only signals created after this date (YYYY-MM-DD), e.g. the day of an update')

    def handle(self, *args, days=30, since=None, **kw):
        qs = ScannerSignal.objects.exclude(outcome='OPEN')
        if since:
            qs = qs.filter(created_at__date__gte=since)
        else:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
        groups = defaultdict(list)
        for s in qs:
            groups[(s.style, s.grade)].append(s)
            groups[('ALL', s.grade)].append(s)
        if not groups:
            self.stdout.write('No closed signals in this window yet.')
            return
        self.stdout.write(f"{'style':9} {'grade':5} {'n':>4} {'TP':>4} {'SL':>4} {'EXP':>4} {'win%':>6} {'avg R':>6} {'fast SL':>8} {'bars→SL':>8}")
        for (style, grade), rows in sorted(groups.items(), key=lambda kv: (kv[0][0] != 'ALL', kv[0])):
            tp = sum(r.outcome == 'TP' for r in rows)
            sl = [r for r in rows if r.outcome == 'SL']
            ex = sum(r.outcome == 'EXPIRED' for r in rows)
            decided = tp + len(sl)
            bars = [((r.closed_at - r.created_at).total_seconds() / TF_SEC.get(r.timeframe, 3600)) for r in sl if r.closed_at]
            fast = sum(b <= 3 for b in bars)
            avg_r = sum(r.result_r or 0 for r in rows) / len(rows)
            self.stdout.write(f"{style:9} {grade:5} {len(rows):>4} {tp:>4} {len(sl):>4} {ex:>4} "
                              f"{(tp / decided * 100 if decided else 0):>5.0f}% {avg_r:>+6.2f} {fast:>4}/{len(sl):<3} "
                              f"{(median(bars) if bars else 0):>8.1f}")
        self.stdout.write('\nfast SL = stopped within 3 candles of the signal. bars→SL = median candles until the stop was hit.')
