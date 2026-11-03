# store/mail_backend.py
"""
Send email over HTTPS, for hosts that block mail ports (PythonAnywhere free accounts):
  • Gmail API with the site's own Gmail account (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN;
    one-time setup: python manage.py gmail_auth), or
  • Brevo / SendGrid (BREVO_API_KEY or SENDGRID_API_KEY).
Turned on in settings when one of these is in .env; otherwise the normal SMTP backend is used.
Every email the site builds (store/emails.py, allauth, Django) goes through here unchanged. The inline logo
(cid:logo) is swapped for the logo's public address, because these APIs do not keep inline images reliably.
"""
import base64
import logging
import threading
import time
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


def _logo_url():
    site = str(getattr(settings, 'SITE_URL', '') or 'https://www.patreoaix.com').rstrip('/')
    return f'{site}/static/store/icons/icon-192.png'


def _parts(msg):
    text = msg.body or ''
    html = next((c for c, mime in getattr(msg, 'alternatives', []) if mime == 'text/html'), None)
    if html:
        html = html.replace('cid:logo', _logo_url())
    return text, html


_gmail = {'token': '', 'until': 0}
_gmail_lock = threading.Lock()


def gmail_access_token():
    """A fresh access token from the refresh token (cached until shortly before it expires)."""
    with _gmail_lock:
        if _gmail['token'] and time.time() < _gmail['until']:
            return _gmail['token']
        r = requests.post('https://oauth2.googleapis.com/token', timeout=20, data={
            'client_id': settings.GMAIL_CLIENT_ID, 'client_secret': settings.GMAIL_CLIENT_SECRET,
            'refresh_token': settings.GMAIL_REFRESH_TOKEN, 'grant_type': 'refresh_token'})
        if r.status_code != 200:
            raise RuntimeError(f'Gmail sign-in {r.status_code}: {r.text[:300]}')
        d = r.json()
        _gmail.update(token=d['access_token'], until=time.time() + int(d.get('expires_in', 3600)) - 120)
        return _gmail['token']


def _addr(a):
    name, email = parseaddr(a)
    return name or '', email


class HttpEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.brevo = getattr(settings, 'BREVO_API_KEY', '') or ''
        self.sendgrid = getattr(settings, 'SENDGRID_API_KEY', '') or ''
        self.gmail = bool(getattr(settings, 'GMAIL_REFRESH_TOKEN', ''))

    def send_messages(self, messages):
        sent = 0
        for m in messages:
            try:
                (self._gmail if self.gmail else self._brevo if self.brevo else self._sendgrid)(m)
                sent += 1
            except Exception as e:
                logger.warning(f'email API send to {m.to} failed: {e}')
                if not self.fail_silently:
                    raise
        return sent

    def _gmail(self, m):
        """The whole MIME message (HTML, text and the inline logo) as Gmail would send it from its own servers."""
        raw = base64.urlsafe_b64encode(m.message().as_bytes()).decode()
        r = requests.post('https://gmail.googleapis.com/gmail/v1/users/me/messages/send', json={'raw': raw}, timeout=30,
                          headers={'Authorization': f'Bearer {gmail_access_token()}'})
        if r.status_code >= 300:
            if r.status_code == 401:
                _gmail['until'] = 0                   # token revoked or expired: fetch a new one next time
            raise RuntimeError(f'Gmail API {r.status_code}: {r.text[:300]}')

    def _brevo(self, m):
        text, html = _parts(m)
        name, email = _addr(m.from_email)
        body = {'sender': {'name': name or 'PATREOAIX', 'email': email}, 'to': [{'email': t} for t in m.to],
                'subject': m.subject, 'textContent': text or ' '}
        if html:
            body['htmlContent'] = html
        if m.reply_to:
            body['replyTo'] = {'email': _addr(m.reply_to[0])[1]}
        r = requests.post('https://api.brevo.com/v3/smtp/email', json=body, timeout=20,
                          headers={'api-key': self.brevo, 'accept': 'application/json'})
        if r.status_code >= 300:
            raise RuntimeError(f'Brevo {r.status_code}: {r.text[:300]}')

    def _sendgrid(self, m):
        text, html = _parts(m)
        name, email = _addr(m.from_email)
        content = [{'type': 'text/plain', 'value': text or ' '}] + ([{'type': 'text/html', 'value': html}] if html else [])
        body = {'personalizations': [{'to': [{'email': t} for t in m.to]}],
                'from': {'email': email, 'name': name or 'PATREOAIX'}, 'subject': m.subject, 'content': content}
        if m.reply_to:
            body['reply_to'] = {'email': _addr(m.reply_to[0])[1]}
        r = requests.post('https://api.sendgrid.com/v3/mail/send', json=body, timeout=20,
                          headers={'Authorization': f'Bearer {self.sendgrid}'})
        if r.status_code >= 300:
            raise RuntimeError(f'SendGrid {r.status_code}: {r.text[:300]}')
