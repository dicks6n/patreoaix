# store/push.py
"""
Web Push: notifications on the customer's phone / desktop even when the site is closed
(installed app on iPhone 16.4+, Android, Chrome, Edge, Firefox, Safari).

Setup (once):  python manage.py vapid_keys   → paste the three lines into .env, reload the site.
Without VAPID keys nothing is sent and the "Enable notifications" button stays hidden.

Every in-app notification (store/notifications.notify) is also pushed to the user's devices.
"""
import json
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def public_key():
    return str(getattr(settings, 'VAPID_PUBLIC_KEY', '') or '')


def enabled():
    return bool(public_key() and getattr(settings, 'VAPID_PRIVATE_KEY', ''))


def subscribe(user, data, user_agent=''):
    from .models import PushSubscription
    keys = data.get('keys') or {}
    endpoint = str(data.get('endpoint') or '')
    if not endpoint.startswith('https://') or not keys.get('p256dh') or not keys.get('auth'):
        raise ValueError('Invalid push subscription.')
    sub, _ = PushSubscription.objects.update_or_create(
        endpoint=endpoint, defaults={'user': user, 'p256dh': keys['p256dh'][:255], 'auth': keys['auth'][:255],
                                     'user_agent': (user_agent or '')[:255]})
    return sub


def unsubscribe(user, endpoint):
    from .models import PushSubscription
    return PushSubscription.objects.filter(user=user, endpoint=endpoint).delete()[0]


def send(user, title, body='', url='/', tag=None):
    """Push to every device of the user. Returns how many devices accepted it."""
    if not enabled() or not user:
        return 0
    from pywebpush import webpush, WebPushException
    from .models import PushSubscription
    payload = json.dumps({'title': title[:120], 'body': (body or '')[:300], 'url': url or '/', 'tag': tag or ''})
    claims = {'sub': 'mailto:' + (getattr(settings, 'VAPID_EMAIL', '') or 'support@patreoaix.com')}
    ok = 0
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush({'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}}, payload,
                    vapid_private_key=settings.VAPID_PRIVATE_KEY, vapid_claims=dict(claims), ttl=3600, timeout=5)
            ok += 1
            PushSubscription.objects.filter(pk=sub.pk).update(last_ok_at=timezone.now())
        except WebPushException as e:
            code = getattr(getattr(e, 'response', None), 'status_code', None)
            if code in (404, 410):                       # the browser dropped this subscription
                sub.delete()
            else:
                logger.warning(f'push {user}: {e}')
        except Exception as e:
            logger.warning(f'push {user}: {e}')
    return ok


def safe_send(*args, **kwargs):
    try:
        return send(*args, **kwargs)
    except Exception as e:
        logger.warning(f'push failed: {e}')
        return 0
