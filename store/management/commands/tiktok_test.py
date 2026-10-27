"""
python manage.py tiktok_test            → shows whether TikTok is set up and connected, and the posting options
python manage.py tiktok_test --post     → posts the newest signal poster now
"""
from django.core.management.base import BaseCommand

from store import poster
from store import tiktok as tt


class Command(BaseCommand):
    help = 'Check the TikTok connection and optionally post the newest signal poster'

    def add_arguments(self, parser):
        parser.add_argument('--post', action='store_true', help='Post the newest poster')

    def handle(self, *args, post=False, **opts):
        if not tt.configured():
            self.stderr.write(self.style.ERROR('Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env (steps at the top of store/tiktok.py).'))
            return
        if not tt.connected():
            self.stderr.write(self.style.ERROR('Not connected yet: on the site open Signal Posters (/posters/) and press "Connect TikTok".'))
            self.stdout.write(f'Redirect URI to register in the TikTok app: {tt.redirect_uri()}')
            return
        try:
            user = tt.user_info()
            info = tt.creator_info()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'TikTok refused: {e}'))
            return
        self.stdout.write(self.style.SUCCESS(f"Connected: {user.get('display_name') or info.get('creator_nickname')}"))
        self.stdout.write(f"Privacy options TikTok allows now: {', '.join(info.get('privacy_level_options') or [])}"
                          f"  → posting as {tt._privacy(info)}")
        if not post:
            return
        items = poster.recent(1)
        if not items:
            self.stderr.write('No posters yet: python manage.py make_posters --open')
            return
        p = items[0]
        rel = p['url'][len(tt.settings.MEDIA_URL):]
        url = tt.poster_url(rel)
        self.stdout.write(f'Posting {url}')
        pid = tt.post_photo(url, p['caption'].split('\n', 1)[0] or 'PATREOAIX AI signal', p['caption'])
        self.stdout.write(self.style.SUCCESS(f'Sent (publish id {pid}). Check your TikTok in a minute or two.'))
