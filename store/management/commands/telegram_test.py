"""
python manage.py telegram_test              → checks the bot token and channel, posts a test message + chart
python manage.py telegram_test --last       → (re)posts the newest recorded AI signal
python manage.py telegram_test --open       → posts every open AI signal (any grade) not posted yet
python manage.py telegram_test --all        → posts every AI signal of the last 7 days (--days N), open and closed;
                                              a closed one gets its result (TP / SL / expired) as a reply
python manage.py telegram_test --now brief  → posts a scheduled post now: brief, pulse, recap or weekly
python manage.py telegram_test --photo pic.jpg --caption "Text"   → posts any photo (a file or an https link);
                                                --photo can be given several times
python manage.py telegram_test --find-channel → lists the channels the bot was added to, with their ids
                                                (a private channel needs its -100… id, not the t.me/+ link)
"""
from django.core.management.base import BaseCommand

from store import telegram_bot as tg


class Command(BaseCommand):
    help = 'Check the Telegram bot and post a test message to the channel'

    def add_arguments(self, parser):
        parser.add_argument('--last', action='store_true', help='Post the newest recorded AI signal')
        parser.add_argument('--open', action='store_true', help='Post the open AI signals not posted yet')
        parser.add_argument('--all', action='store_true', help='Post every signal of the last --days days, open and closed')
        parser.add_argument('--days', type=int, default=7, help='How far back --all goes (default 7)')
        parser.add_argument('--now', choices=['brief', 'pulse', 'recap', 'weekly'], help='Post one of the scheduled posts now')
        parser.add_argument('--photo', action='append', default=[], help='Post this photo (file path or https link); repeat for several')
        parser.add_argument('--caption', default='', help='Caption for --photo')
        parser.add_argument('--find-channel', action='store_true', help='Show the id of the channels the bot was added to')

    def handle(self, *args, last=False, find_channel=False, open=False, photo=(), caption='', all=False, days=7, now=None, **opts):
        if find_channel:
            return self.find_channel()
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
        if photo:
            return self.post_photos(photo, caption)
        if now:
            from store import signal_alerts, telegram_schedule as ts
            if now in ('brief', 'pulse'):
                results, strength, _ = signal_alerts.confluence('intraday')
                (ts.morning_brief if now == 'brief' else ts.market_pulse)(results, strength)
            else:
                (ts.daily_recap if now == 'recap' else ts.weekly_report)()
            self.stdout.write(self.style.SUCCESS(f'Posted the {now}.'))
            return
        if open or all:
            return self.post_signals(open_only=not all, days=days)
        if last:
            from django.core.cache import cache
            from store.models import ScannerSignal
            sig = ScannerSignal.objects.order_by('-created_at').first()
            if not sig:
                self.stderr.write('No AI signals recorded yet.')
                return
            cache.delete(f'tg:sent:{sig.id}')
            mid = tg.post_signal(sig, force=True)
            self.stdout.write(self.style.SUCCESS(f'Posted signal {sig} (message {mid})') if mid else self.style.WARNING('Not posted (see the log)'))
            return
        bars = [{'open': 100 + i * .3, 'high': 101 + i * .3, 'low': 99.5 + i * .3, 'close': 100.6 + i * .3} for i in range(60)]
        png = tg.render_chart('XAUUSD', '1h', 'BUY', bars, 117.0, 115.5, 120.0, 122.0, 2, title='TEST · PATREOAIX bot connected')
        tg.api('sendPhoto', {'chat_id': tg._cfg('TELEGRAM_CHANNEL_ID'), 'parse_mode': 'HTML',
                             'caption': '✅ <b>PATREOAIX signals bot is connected.</b>\nStrong AI setups will be posted here automatically.'},
               files={'photo': ('test.png', png, 'image/png')})
        self.stdout.write(self.style.SUCCESS('Test message posted to the channel.'))

    def post_signals(self, open_only, days):
        """Every signal (any grade), oldest first, skipping ones already in the channel."""
        import time
        from datetime import timedelta
        from django.core.cache import cache
        from django.utils import timezone
        from store.models import ScannerSignal
        sigs = ScannerSignal.objects.order_by('created_at')
        sigs = sigs.filter(outcome='OPEN') if open_only else sigs.filter(created_at__gte=timezone.now() - timedelta(days=days))
        posted = skipped = 0
        for sig in sigs:
            label = f'{sig.grade} {sig.direction} {sig.symbol} {sig.timeframe} {sig.created_at:%d %b %H:%M}'
            if cache.get(f'tg:msg:{sig.id}'):
                skipped += 1
                self.stdout.write(f'  already posted: {label}')
                continue
            mid = tg.post_signal(sig, force=True)
            if not mid:
                self.stderr.write(self.style.ERROR(f'  failed: {label} (see the log)'))
                continue
            posted += 1
            if sig.outcome != 'OPEN':
                tg.post_outcome(sig)                         # reply with how it ended
            self.stdout.write(self.style.SUCCESS(f'  posted: {label}' + ('' if sig.outcome == 'OPEN' else f'  → {sig.outcome} {sig.result_r or 0:+.2f}R')))
            time.sleep(3)                                    # Telegram allows ~20 posts a minute in a channel
        what = 'open signals' if open_only else f'signals from the last {days} days'
        self.stdout.write(self.style.SUCCESS(f'Posted {posted} of {sigs.count()} {what}' + (f' ({skipped} were already posted)' if skipped else '') + '.'))

    def post_photos(self, photos, caption):
        """Any photo: a file on this computer or an https link. Over 10 MB (Telegram's photo limit) goes as a file."""
        import mimetypes
        import os
        chat = tg._cfg('TELEGRAM_CHANNEL_ID')
        for i, src in enumerate(photos):
            data = {'chat_id': chat}
            if caption and i == 0:
                data['caption'] = caption[:1024]
            try:
                if src.startswith(('http://', 'https://')):
                    msg = tg.api('sendPhoto', {**data, 'photo': src}, timeout=30)
                else:
                    path = os.path.expanduser(src)
                    if not os.path.isfile(path):
                        self.stderr.write(self.style.ERROR(f'File not found: {path}'))
                        continue
                    kind = 'photo' if os.path.getsize(path) <= 10 * 1024 * 1024 else 'document'
                    with open(path, 'rb') as fh:
                        msg = tg.api('sendPhoto' if kind == 'photo' else 'sendDocument', data, timeout=60,
                                     files={kind: (os.path.basename(path), fh, mimetypes.guess_type(path)[0] or 'image/jpeg')})
                self.stdout.write(self.style.SUCCESS(f"Posted {src} (message {msg['message_id']})"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'{src}: Telegram refused: {e}'))

    def find_channel(self):
        if not tg._cfg('TELEGRAM_BOT_TOKEN'):
            self.stderr.write(self.style.ERROR('Set TELEGRAM_BOT_TOKEN in .env first.'))
            return
        try:
            updates = tg.api('getUpdates', {'allowed_updates': '["my_chat_member","channel_post"]'})
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Telegram refused: {e}'))
            return
        found = {}
        for u in updates:
            chat = (u.get('my_chat_member') or u.get('channel_post') or {}).get('chat') or {}
            if chat.get('type') == 'channel':
                found[chat['id']] = chat.get('title') or chat.get('username')
        if not found:
            self.stderr.write('No channel seen yet. Add the bot to the channel as an admin (or remove and add it again, '
                              'or post any message in the channel), then run this again within 24 hours.')
            return
        for cid, title in found.items():
            self.stdout.write(self.style.SUCCESS(f'{title}:  TELEGRAM_CHANNEL_ID={cid}'))
