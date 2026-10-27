"""
python manage.py telegram_test              → checks the bot token and channel, posts a test message + chart
python manage.py telegram_test --last       → (re)posts the newest recorded AI signal
"""
from django.core.management.base import BaseCommand

from store import telegram_bot as tg


class Command(BaseCommand):
    help = 'Check the Telegram bot and post a test message to the channel'

    def add_arguments(self, parser):
        parser.add_argument('--last', action='store_true', help='Post the newest recorded AI signal')

    def handle(self, *args, last=False, **opts):
        if not tg.enabled():
            self.stderr.write(self.style.ERROR('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID in .env (and TELEGRAM_ENABLED=True).'))
            return
        try:
            me = tg.api('getMe')
            self.stdout.write(self.style.SUCCESS(f"Bot OK: @{me.get('username')}"))
            chat = tg.api('getChat', {'chat_id': tg._cfg('TELEGRAM_CHANNEL_ID')})
            self.stdout.write(self.style.SUCCESS(f"Channel OK: {chat.get('title') or chat.get('username')}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Telegram refused: {e}'))
            self.stderr.write('Check the token, the channel id, and that the bot is an admin of the channel.')
            return
        if last:
            from django.core.cache import cache
            from store.models import ScannerSignal
            sig = ScannerSignal.objects.order_by('-created_at').first()
            if not sig:
                self.stderr.write('No AI signals recorded yet.')
                return
            cache.delete(f'tg:sent:{sig.id}')
            mid = tg.post_signal(sig)
            self.stdout.write(self.style.SUCCESS(f'Posted signal {sig} (message {mid})') if mid else self.style.WARNING('Not posted (grade below TELEGRAM_MIN_GRADE?)'))
            return
        bars = [{'open': 100 + i * .3, 'high': 101 + i * .3, 'low': 99.5 + i * .3, 'close': 100.6 + i * .3} for i in range(60)]
        png = tg.render_chart('XAUUSD', '1h', 'BUY', bars, 117.0, 115.5, 120.0, 122.0, 2, title='TEST · PATREOAIX bot connected')
        tg.api('sendPhoto', {'chat_id': tg._cfg('TELEGRAM_CHANNEL_ID'), 'parse_mode': 'HTML',
                             'caption': '✅ <b>PATREOAIX signals bot is connected.</b>\nStrong AI setups will be posted here automatically.'},
               files={'photo': ('test.png', png, 'image/png')})
        self.stdout.write(self.style.SUCCESS('Test message posted to the channel.'))
