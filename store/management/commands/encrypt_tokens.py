"""
python manage.py encrypt_tokens   → encrypts every stored broker token with TOKEN_ENCRYPTION_KEY (safe to re-run)
"""
from django.core.management.base import BaseCommand

from store.fields import encryption_on, PREFIX


class Command(BaseCommand):
    help = 'Encrypt the broker API tokens already in the database'

    def handle(self, *args, **opts):
        if not encryption_on():
            self.stderr.write(self.style.ERROR('Set a valid TOKEN_ENCRYPTION_KEY in .env first.'))
            return
        from django.db import connection
        from store.models import UserBrokerAccount
        table = UserBrokerAccount._meta.db_table
        with connection.cursor() as cur:
            cur.execute(f'SELECT id, api_token FROM {table}')
            plain = [i for i, tok in cur.fetchall() if tok and not str(tok).startswith(PREFIX)]
        for acc in UserBrokerAccount.objects.filter(id__in=plain):
            acc.save(update_fields=['api_token'])        # the field encrypts on save
        self.stdout.write(self.style.SUCCESS(f'Encrypted {len(plain)} token(s).'))
