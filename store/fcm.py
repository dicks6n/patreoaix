# store/fcm.py
"""
Firebase Cloud Messaging (FCM HTTP v1): instant notifications in the PATREOAIX Android app, delivered by Google
Play services like WhatsApp messages, even when the app is closed.

Setup (once, on the server only, never in git):
    Firebase console → Project settings → Service accounts → Generate new private key → upload the JSON file to
    the server (e.g. ~/secrets/firebase-patreoaix.json) and add to .env:
        FIREBASE_CREDENTIALS=/home/<you>/secrets/firebase-patreoaix.json
Without it nothing is sent; the app falls back to its 15-minute check.

Messages are data-only with high priority, so the app itself builds the notification (same channel, grouping and
tags as the rest of the app's notifications).
"""
import json
import logging
import os
import threading

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)
SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
_lock = threading.Lock()
_creds = None


def _path():
    return str(getattr(settings, 'FIREBASE_CREDENTIALS', '') or '')


def enabled():
    p = _path()
    return bool(p and os.path.isfile(p))


def _token():
    """(access token, project id) from the service-account key, refreshed when it expires."""
    global _creds
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    with _lock:
        if _creds is None:
            _creds = service_account.Credentials.from_service_account_file(_path(), scopes=[SCOPE])
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token, _creds.project_id


def register(user, token, device=''):
    from .models import FcmDevice
    token = (token or '').strip()
    if not (20 <= len(token) <= 4096) or any(c.isspace() for c in token):
        raise ValueError('Invalid device token.')
    d, _ = FcmDevice.objects.update_or_create(token=token, defaults={'user': user, 'device': (device or '')[:255]})
    return d


def unregister(user, token):
    from .models import FcmDevice
    return FcmDevice.objects.filter(user=user, token=token).delete()[0]


def send(user, title, body='', url='/', tag=None, sticky=False, category=''):
    """Send to every Android device of the user. Returns how many devices accepted it."""
    if not enabled() or not user:
        return 0
    import requests
    from .models import FcmDevice
    devices = list(FcmDevice.objects.filter(user=user))
    if not devices:
        return 0
    access, project = _token()
    data = {'title': (title or 'PATREOAIX')[:120], 'body': (body or '')[:300], 'url': url or '/', 'tag': tag or '',
            'sticky': '1' if sticky else '0', 'category': category or ''}
    ok = 0
    for d in devices:
        msg = {'message': {'token': d.token, 'data': data,
                           'android': {'priority': 'high', 'ttl': '86400s',
                                       **({'collapse_key': tag[:60]} if tag else {})}}}
        try:
            r = requests.post(f'https://fcm.googleapis.com/v1/projects/{project}/messages:send', json=msg, timeout=6,
                              headers={'Authorization': f'Bearer {access}'})
        except Exception as e:
            logger.warning(f'fcm {user}: {e}')
            continue
        if r.status_code == 200:
            ok += 1
            FcmDevice.objects.filter(pk=d.pk).update(last_ok_at=timezone.now())
        elif r.status_code == 404 or 'UNREGISTERED' in r.text or (r.status_code == 400 and 'registration token' in r.text):
            d.delete()                                   # app uninstalled or token replaced
        else:
            logger.warning(f'fcm {user}: {r.status_code} {r.text[:200]}')
    return ok


def safe_send(*args, **kwargs):
    try:
        return send(*args, **kwargs)
    except Exception as e:
        logger.warning(f'fcm failed: {e}')
        return 0
