from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Find real chart examples of every candlestick pattern for the academy (store/academy_examples.py).'

    def handle(self, *args, **opts):
        from store import academy_examples as ae
        found = ae.find_all()
        for key in ae.PATTERNS:
            e = found.get(key)
            self.stdout.write(f"{key:22s} " + (f"{e['symbol']} {e['timeframe']} {e['at']:%Y-%m-%d %H:%M} · next {ae.AFTER} candles {e['move_after']:+.2f}%"
                                               if e else '— not found in the current history (diagram shown)'))
        self.stdout.write(self.style.SUCCESS(f'{len(found)} of {len(ae.PATTERNS)} patterns have a real example.'))
