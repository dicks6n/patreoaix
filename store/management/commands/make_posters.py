"""
TikTok-size posters (1080×1920) for AI signals, saved in media/posters/ and listed at /posters/.

python manage.py make_posters --open      → posters for every open signal
python manage.py make_posters --last 5    → the 5 newest signals (result posters for the closed ones)
python manage.py make_posters --id 12     → one signal
"""
from django.core.management.base import BaseCommand

from store import poster
from store.models import ScannerSignal


class Command(BaseCommand):
    help = 'Make TikTok-size signal posters'

    def add_arguments(self, parser):
        parser.add_argument('--open', action='store_true', help='Every open signal')
        parser.add_argument('--last', type=int, default=0, help='The N newest signals')
        parser.add_argument('--id', type=int, help='One signal by id')

    def handle(self, *args, open=False, last=0, id=None, **opts):
        if id:
            sigs = ScannerSignal.objects.filter(pk=id)
        elif open:
            sigs = ScannerSignal.objects.filter(outcome='OPEN').order_by('-created_at')
        else:
            sigs = ScannerSignal.objects.order_by('-created_at')[:last or 1]
        n = 0
        for sig in sigs:
            paths = [poster.save_poster(sig)]
            if sig.outcome != 'OPEN':
                paths.append(poster.save_poster(sig, result=True))
            for p in paths:
                self.stdout.write(self.style.SUCCESS(f'  {p}'))
                n += 1
        self.stdout.write(f'{n} poster(s) in media/posters/ — download them at /posters/' if n else 'No signals found.')
