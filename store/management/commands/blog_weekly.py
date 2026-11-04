"""Write this week's outlook and last week's recap now: python manage.py blog_weekly [--force]"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from store import public_pages as pp


class Command(BaseCommand):
    help = "Write the weekly outlook (this week) and recap (last week) on /blog/ now."

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Rewrite posts that already exist')

    def handle(self, *args, force=False, **kw):
        mon = pp._week_start(date.today())
        for name, fn, day in (('outlook', pp.write_outlook, mon), ('recap', pp.write_recap, mon - timedelta(days=7))):
            post = fn(day, force=force)
            self.stdout.write(f'{name}: ' + (f'{post.title}  →  {post.get_absolute_url()}' if post
                                             else 'nothing written (already there, or no data yet)'))
