# store/mt5_messages.py
"""
Messages tab of the terminal (like MetaTrader 5 mobile: push notifications arrive as chats, one chat per sender,
with an avatar, the last message, the time and an unread counter).

Chats are built from the user's Notification rows:
    trade      Trading      TRADE, COPY_TRADE        orders, positions, stop loss / take profit, P2P orders
    broker     PATREOAIX    FUNDING, PAYOUT, ACCOUNT, KYC, SECURITY
    community  Community    REFERRAL
    news       News         SYSTEM and anything else
    support    Support      a real two-way chat: the user writes (a SupportTicket is opened for the team), staff
                            answer from the admin message center; both sides are Notification rows with
                            metadata {'chat': 'support'} ('out': True for the user's own messages).
"""
import json
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

CHATS = [
    # key, name, avatar icon, avatar colour, description
    ('trade', 'Trading', 'fa-chart-line', '#0a84ff', 'Orders, positions, stop loss and take profit'),
    ('broker', 'PATREOAIX', 'fa-building-columns', '#f0b90b', 'Deposits, withdrawals, verification and security'),
    ('support', 'Support', 'fa-headset', '#30d158', 'Write to the PATREOAIX team'),
    ('community', 'Community', 'fa-user-group', '#bf5af2', 'Referrals and rewards'),
    ('news', 'News', 'fa-bullhorn', '#ff9f0a', 'Platform news and announcements'),
]
BY_KEY = {c[0]: c for c in CHATS}
CATEGORY_CHAT = {'TRADE': 'trade', 'COPY_TRADE': 'trade', 'FUNDING': 'broker', 'PAYOUT': 'broker', 'ACCOUNT': 'broker',
                 'KYC': 'broker', 'SECURITY': 'broker', 'REFERRAL': 'community'}


def chat_of(n):
    meta = n.metadata if isinstance(n.metadata, dict) else {}
    if meta.get('chat') in BY_KEY:
        return meta['chat']
    return CATEGORY_CHAT.get(n.category, 'news')


def _ajax(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _row(n):
    meta = n.metadata if isinstance(n.metadata, dict) else {}
    return {'id': n.id, 'title': n.title, 'text': n.message, 'time': n.created_at.isoformat(),
            'url': n.action_url or '', 'label': n.action_label or '', 'priority': n.priority,
            'out': bool(meta.get('out')), 'read': n.is_read}


def _qs(user):
    from .models import Notification
    return Notification.objects.filter(user=user, is_archived=False)


def unread_count(user):
    return _qs(user).filter(is_read=False).count()


@login_required
def chats(request):
    """GET → the chat list with the last message and unread count of each chat."""
    if not _ajax(request):
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)
    last, unread = {}, {}
    for n in _qs(request.user).order_by('-created_at')[:600]:
        k = chat_of(n)
        if k not in last:
            last[k] = n
        if not n.is_read:
            unread[k] = unread.get(k, 0) + 1
    out = []
    for key, name, icon, color, desc in CHATS:
        n = last.get(key)
        out.append({'key': key, 'name': name, 'icon': icon, 'color': color,
                    'last': (('You: ' if (n.metadata or {}).get('out') else '') + (n.title + ': ' if n.title and key != 'support' else '') + n.message)[:140] if n else desc,
                    'time': n.created_at.isoformat() if n else None, 'unread': unread.get(key, 0)})
    out.sort(key=lambda c: (c['time'] is None, -(datetime.fromisoformat(c['time']).timestamp()) if c['time'] else 0))
    return JsonResponse({'success': True, 'chats': out, 'unread': sum(unread.values())})


@login_required
def chat(request, key):
    """GET → the messages of one chat (oldest first, last 150). Opening a chat marks it read."""
    if not _ajax(request) or key not in BY_KEY:
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)
    rows = [n for n in _qs(request.user).order_by('-created_at')[:800] if chat_of(n) == key][:150]
    ids = [n.id for n in rows if not n.is_read]
    if ids:
        _qs(request.user).filter(id__in=ids).update(is_read=True)
    _, name, icon, color, desc = BY_KEY[key]
    return JsonResponse({'success': True, 'chat': {'key': key, 'name': name, 'icon': icon, 'color': color, 'desc': desc,
                                                   'can_write': key == 'support'},
                         'messages': [_row(n) for n in reversed(rows)], 'unread': unread_count(request.user)})


@login_required
@require_POST
def read_all(request):
    _qs(request.user).filter(is_read=False).update(is_read=True)
    return JsonResponse({'success': True, 'unread': 0})


@login_required
@require_POST
def send_support(request):
    """The user writes to support from the terminal: kept in the chat and opened as a ticket for the team."""
    from .models import Notification, SupportTicket
    try:
        text = (json.loads(request.body or '{}').get('text') or '').strip()
    except ValueError:
        text = ''
    if not text:
        return JsonResponse({'success': False, 'error': 'Type a message first.'})
    if len(text) > 2000:
        return JsonResponse({'success': False, 'error': 'The message is too long (2,000 characters at most).'})
    recent = Notification.objects.filter(user=request.user, metadata__chat='support', metadata__out=True,
                                         created_at__gte=timezone.now() - timedelta(minutes=10)).count()
    if recent >= 10:
        return JsonResponse({'success': False, 'error': 'Too many messages. Wait a few minutes, the team will answer.'})
    n = Notification.objects.create(user=request.user, title='', message=text, category='SYSTEM', priority='info',
                                    metadata={'chat': 'support', 'out': True}, is_read=True)
    SupportTicket.objects.create(user=request.user, category='Account & Security', subject=text[:80] or 'Terminal message',
                                 description=f'[Sent from the terminal Messages chat]\n\n{text}')
    try:
        from .notifications import notify_staff
        notify_staff(title=f'Support message from {request.user.username}', message=text[:300], category='SYSTEM',
                     action_url='/admin-messages/')
    except Exception:
        pass
    if not Notification.objects.filter(user=request.user, metadata__chat='support', metadata__out=False,
                                       metadata__auto=True, created_at__gte=timezone.now() - timedelta(hours=12)).exists():
        Notification.objects.create(user=request.user, title='', category='SYSTEM', priority='info', is_read=True,
                                    metadata={'chat': 'support', 'out': False, 'auto': True},
                                    message='Thanks, we received your message. The team answers here, usually within a few hours.')
    return JsonResponse({'success': True, 'message': _row(n)})


def staff_reply(staff_user, user, text):
    """Staff answer a user's support chat (admin message center)."""
    from .models import Notification
    return Notification.objects.create(user=user, title='', message=text.strip()[:2000], category='SYSTEM', priority='info',
                                       metadata={'chat': 'support', 'out': False, 'by': staff_user.username})
