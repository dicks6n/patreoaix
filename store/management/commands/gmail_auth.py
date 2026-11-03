"""
One-time setup: let the site send email through its own Gmail account with the Gmail API (works where mail ports
are blocked, e.g. PythonAnywhere free accounts).

    python manage.py gmail_auth

Needs GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env (Google Cloud Console → OAuth client, type "Desktop app").
Prints a link: open it, sign in as the site's Gmail, allow "Send email on your behalf". The browser then goes to a
page that does not load (http://localhost...): copy that whole address from the address bar and paste it here.
Prints the GMAIL_REFRESH_TOKEN line to add to .env.
"""
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

REDIRECT = 'http://localhost:8765/'
SCOPE = 'https://www.googleapis.com/auth/gmail.send'


class Command(BaseCommand):
    help = 'One-time Gmail API setup: prints the GMAIL_REFRESH_TOKEN for .env'

    def handle(self, *a, **o):
        cid, secret = settings.GMAIL_CLIENT_ID, settings.GMAIL_CLIENT_SECRET
        if not cid or not secret:
            raise CommandError('Add GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET to .env first.')
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': cid, 'redirect_uri': REDIRECT, 'response_type': 'code', 'scope': SCOPE,
            'access_type': 'offline', 'prompt': 'consent', 'login_hint': settings.EMAIL_HOST_USER})
        self.stdout.write('\n1. Open this link in your browser, sign in as ' + settings.EMAIL_HOST_USER + ' and press Allow:\n')
        self.stdout.write(url + '\n')
        self.stdout.write('\n2. The browser then shows "This site can\'t be reached" at http://localhost:8765/...')
        self.stdout.write('   That is expected. Copy the WHOLE address from the address bar and paste it here.\n')
        pasted = input('Address: ').strip()
        code = (parse_qs(urlparse(pasted).query).get('code') or [pasted])[0]
        r = requests.post('https://oauth2.googleapis.com/token', timeout=20, data={
            'code': code, 'client_id': cid, 'client_secret': secret, 'redirect_uri': REDIRECT,
            'grant_type': 'authorization_code'})
        if r.status_code != 200:
            raise CommandError(f'Google said {r.status_code}: {r.text[:300]}')
        token = r.json().get('refresh_token')
        if not token:
            raise CommandError('No refresh token came back. Remove the app at myaccount.google.com → Security → '
                               'Third-party access, then run this again.')
        self.stdout.write(self.style.SUCCESS('\n3. Add this line to .env, then reload the site:\n'))
        self.stdout.write(f'GMAIL_REFRESH_TOKEN={token}\n')
