# store/mail_backend.py
"""
Send email over HTTPS through Brevo or SendGrid, for hosts that block mail ports (PythonAnywhere free accounts).

Turned on in settings when BREVO_API_KEY or SENDGRID_API_KEY is in .env; otherwise the normal SMTP backend is used.
Every email the site builds (store/emails.py, allauth, Django) goes through here unchanged. The inline logo
(cid:logo) is swapped for the logo's public address, because these APIs do not keep inline images reliably.
"""
import logging
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


def _addr(a):
    name, email = parseaddr(a)
    return name or '', email


class HttpEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.brevo = getattr(settings, 'BREVO_API_KEY', '') or ''
        self.sendgrid = getattr(settings, 'SENDGRID_API_KEY', '') or ''

    def send_messages(self, messages):
        sent = 0
        for m in messages:
            try:
                (self._brevo if self.brevo else self._sendgrid)(m)
                sent += 1
            except Exception as e:
                logger.warning(f'email API send to {m.to} failed: {e}')
                if not self.fail_silently:
                    raise
        return sent

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
