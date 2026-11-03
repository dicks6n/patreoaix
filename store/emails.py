# store/emails.py
"""
Branded emails: one template (logo, heading, details table, button, footer) for every email the site sends.

    from store import emails
    emails.send(user, 'Deposit approved', 'Your deposit is in your wallet',
                intro='We added $50.00 to your wallet.', rows=[('Amount', '$50.00'), ('Method', 'M-Pesa')],
                button=('Open my wallet', '/wallet/'), tone='good')

Sent from DEFAULT_FROM_EMAIL as "PATREOAIX <patreoaix@gmail.com>", in a background thread so a page never waits on
Gmail. The logo is attached inline (cid:logo), so it shows even where remote images are blocked.
Notifications (deposits, withdrawals, KYC, security, key P2P steps) are emailed from the Notification signal in
models.py via email_for_notification(); users can switch non-security emails off in Notification settings.
"""
import logging
import threading
from email.mime.image import MIMEImage
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)
LOGO = Path(__file__).resolve().parent / 'static' / 'store' / 'icons' / 'icon-192.png'
TONES = {'info': '#F5B301', 'good': '#16A34A', 'warn': '#D97706', 'bad': '#DC2626'}


def site_url():
    return str(getattr(settings, 'SITE_URL', '') or 'https://www.patreoaix.com').rstrip('/')


def from_address():
    addr = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or getattr(settings, 'EMAIL_HOST_USER', '')
    return addr if '<' in addr else f'PATREOAIX <{addr}>'


def deliverable(address):
    """Real inboxes only: sample and test accounts use .invalid addresses."""
    a = (address or '').strip().lower()
    return bool(a and '@' in a and not a.endswith('.invalid'))


def _abs(url):
    url = str(url or '')
    return url if url.startswith('http') else site_url() + (url if url.startswith('/') else '/' + url)


def build(to, subject, title, intro='', rows=None, button=None, code=None, note='', tone='info', name=''):
    """→ an EmailMultiAlternatives ready to send (HTML + plain text + inline logo)."""
    ctx = {'subject': subject, 'title': title, 'intro': intro, 'rows': rows or [], 'code': code, 'note': note,
           'name': name, 'accent': TONES.get(tone, TONES['info']), 'site': site_url(),
           'button': (button[0], _abs(button[1])) if button else None,
           'support': getattr(settings, 'SUPPORT_EMAIL', '') or getattr(settings, 'EMAIL_HOST_USER', '')}
    html = render_to_string('emails/base.html', ctx)
    text = render_to_string('emails/base.txt', ctx)
    msg = EmailMultiAlternatives(subject, text, from_address(), [to])
    msg.attach_alternative(html, 'text/html')
    msg.mixed_subtype = 'related'
    try:
        img = MIMEImage(LOGO.read_bytes(), 'png')
        img.add_header('Content-ID', '<logo>')
        img.add_header('Content-Disposition', 'inline', filename='patreoaix.png')
        msg.attach(img)
    except Exception as e:
        logger.warning(f'email logo missing: {e}')
    return msg


def _deliver(msg):
    try:
        msg.send(fail_silently=False)
    except Exception as e:
        logger.warning(f'email to {msg.to} failed: {e}')


def send(user_or_email, subject, title, background=True, **kw):
    """Send one branded email. user_or_email: a User or an address. Returns False when there is nowhere to send."""
    to = getattr(user_or_email, 'email', user_or_email)
    if not deliverable(to):
        return False
    if 'name' not in kw and hasattr(user_or_email, 'username'):
        kw['name'] = user_or_email.first_name or user_or_email.username
    msg = build(to, subject, title, **kw)
    if background:
        threading.Thread(target=_deliver, args=(msg,), daemon=False).start()
    else:
        msg.send(fail_silently=False)
    return True


# ── notifications → email ─────────────────────────────────────
EMAIL_KEYS = {'funding', 'kyc', 'security'}      # always worth an email; P2P only for the steps that need action


def email_for_notification(n):
    """Email a Notification when it matters: money in/out, verification, security, and P2P payment/release/appeal."""
    from . import notif_prefs as NP
    meta = n.metadata if isinstance(n.metadata, dict) else {}
    if meta.get('out') or meta.get('chat') or not deliverable(getattr(n.user, 'email', '')):
        return False
    key = NP.of_notification(n)
    p2p_step = key == 'p2p' and (meta.get('sticky') or any(w in (n.title or '').lower() for w in ('released', 'completed', 'cancel')))
    if key not in EMAIL_KEYS and not p2p_step:
        return False
    if key != 'security' and not NP.get(n.user).get('email', True):
        return False                               # the user turned emails off (security emails always go)
    tone = {'success': 'good', 'warning': 'warn', 'error': 'bad', 'danger': 'bad'}.get(str(n.priority or '').lower(), 'info')
    link = NP.safe_link(n.action_url) or '/notifications/'
    return send(n.user, f'{n.title or "PATREOAIX"} · PATREOAIX', n.title or 'Account update', intro=n.message,
                button=(n.action_label or 'Open PATREOAIX', link), tone=tone,
                note='Security alerts are always emailed.' if key == 'security' else
                     'You can turn these emails off in Notification settings.')
