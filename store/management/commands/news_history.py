"""
python manage.py news_history --backfill 5     → 5 years of big releases + how the markets moved (run once)
python manage.py news_history --update         → recent / coming releases and new reactions
python manage.py news_history --show FOMC      → the playbook for that release
"""
from django.core.management.base import BaseCommand

from store import news_history as nh


class Command(BaseCommand):
    help = 'Economic release history and market reactions'

    def add_arguments(self, parser):
        parser.add_argument('--backfill', type=float, default=0, help='Years of history to load')
        parser.add_argument('--update', action='store_true')
        parser.add_argument('--show', default='', help=f"One of {', '.join(nh.EVENTS)}")

    def handle(self, *args, backfill=0, update=False, show='', **opts):
        log = lambda m: self.stdout.write(m)
        if backfill:
            self.stdout.write(f'Loading {backfill:g} years of releases (month by month)…')
            self.stdout.write(self.style.SUCCESS(f'{nh.backfill(backfill, log)} releases saved.'))
            self.stdout.write('Measuring the price reaction after each one (Deriv 15-minute candles)…')
            total = 0
            while True:
                n = nh.measure_pending(limit=300, log=log)
                total += n
                self.stdout.write(f'  {total} reactions so far')
                if n == 0:
                    break
            self.stdout.write(self.style.SUCCESS(f'Done: {total} reactions measured.'))
        if update:
            n, m = nh.update(log)
            self.stdout.write(self.style.SUCCESS(f'{n} releases updated, {m} reactions measured.'))
        if show:
            key = show.upper()
            if key not in nh.EVENTS:
                self.stderr.write(f"Unknown event. Use one of: {', '.join(nh.EVENTS)}")
                return
            pb = nh.playbook(key)
            self.stdout.write(self.style.SUCCESS(pb['label']))
            for st in pb['stats']:
                self.stdout.write(f"  {st['symbol']}: {st['n']} releases · avg move 15m {st['abs_m15'] or 0:.2f}% · "
                                  f"1h {st['abs_h1']:.2f}% · 4h {st['abs_h4'] or 0:.2f}% · first-hour range {st['range_h1']:.2f}%")
                for line in nh.scenario_lines(st, pb['currency']):
                    self.stdout.write('    ' + line)
            self.stdout.write('  Timing: ' + pb['timing'])
