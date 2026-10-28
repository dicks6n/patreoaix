"""python manage.py ai_learn → learn from the closed signals now and show each strategy's new weight."""
from django.core.management.base import BaseCommand

from store import ai_learning


class Command(BaseCommand):
    help = "Let the scanner learn from its closed signals"

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=ai_learning.DAYS)

    def handle(self, *args, days=ai_learning.DAYS, **opts):
        r = ai_learning.learn(days)
        self.stdout.write(self.style.SUCCESS(f"{r['n']} closed signals in {days} days · hit rate {((r['base'] or 0) * 100):.0f}%"))
        if r['n'] < ai_learning.MIN_SIGNALS:
            self.stdout.write(f"Needs at least {ai_learning.MIN_SIGNALS} closed signals before it changes any weight.")
        for key, d in sorted(r['detail'].items(), key=lambda kv: -kv[1]['multiplier']):
            self.stdout.write(f"  {d['label']:<12} agreed with {d['signals']:>3} · right {d['hit_rate'] * 100:>3.0f}% → weight ×{d['multiplier']}")
