# store/notif_prefs.py
"""
Notification preferences (saved on UserProfile.notif_prefs) and the rules that apply them.

  push            True/False      phone / desktop pop-ups (the browser permission is per device on top of this)
  news_email      True/False      NFP / CPI / FOMC prediction emails (store/news_email.py)
  sms             UserProfile.sms_opt_in (phone needed)
  cats            which kinds pop up: trades, funding, p2p, signals, kyc, referral, news (security always does)
  quiet           {'on': bool, 'start': 'HH:MM', 'end': 'HH:MM'} no pop-ups at night (security and P2P orders still do)

Every notification is always kept in the bell / Messages; preferences only decide what pops up.
"""
from datetime import datetime

CATEGORIES = [
    ('trades', 'Trades', 'Orders filled, positions closed, stop loss and take profit', 'fa-chart-line'),
    ('funding', 'Deposits & withdrawals', 'Money in and out of your wallet', 'fa-wallet'),
    ('p2p', 'P2P orders', 'New orders, payments and releases', 'fa-people-arrows'),
    ('signals', 'Signals & price alerts', 'AI signals, copy trading and your price alerts', 'fa-bolt'),
    ('kyc', 'Verification', 'Identity verification results', 'fa-id-card'),
    ('referral', 'Referrals & rewards', 'Friends who join and rewards you earn', 'fa-user-group'),
    ('news', 'News & announcements', 'Platform news and new features', 'fa-bullhorn'),
]
DEFAULTS = {'push': True, 'email': True, 'news_email': True, 'cats': {k: True for k, *_ in CATEGORIES}, 'quiet': {'on': False, 'start': '22:00', 'end': '07:00'}}


def of_notification(n):
    """Preference key of a Notification."""
    meta = n.metadata if isinstance(n.metadata, dict) else {}
    title = (n.title or '').lower()
    if n.category in ('SECURITY',) or 'password' in title or 'login' in title:
        return 'security'
    if 'p2p' in title or str(n.action_url or '').startswith('/p2p/'):
        return 'p2p'
    if meta.get('chat') == 'support':
        return 'support'
    return {'TRADE': 'trades', 'COPY_TRADE': 'signals', 'FUNDING': 'funding', 'PAYOUT': 'funding', 'ACCOUNT': 'funding',
            'KYC': 'kyc', 'REFERRAL': 'referral'}.get(n.category, 'signals' if 'signal' in title or 'alert' in title else 'news')


def get(user):
    from .models import UserProfile
    p = UserProfile.objects.filter(user=user).only('notif_prefs').first()
    raw = (p.notif_prefs if p and isinstance(p.notif_prefs, dict) else {}) or {}
    prefs = {'push': raw.get('push', DEFAULTS['push']), 'email': raw.get('email', DEFAULTS['email']),
             'news_email': raw.get('news_email', DEFAULTS['news_email']),
             'cats': dict(DEFAULTS['cats'], **(raw.get('cats') or {})),
             'quiet': dict(DEFAULTS['quiet'], **(raw.get('quiet') or {}))}
    return prefs


def _in_quiet(q, now=None):
    if not q.get('on'):
        return False
    try:
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        sh, sm = (int(x) for x in q['start'].split(':'))
        eh, em = (int(x) for x in q['end'].split(':'))
        s, e = sh * 60 + sm, eh * 60 + em
        return s <= cur < e if s < e else (cur >= s or cur < e)
    except Exception:
        return False


def should_push(n):
    """May this notification pop up on the user's devices?"""
    key = of_notification(n)
    if key in ('security', 'support'):
        return True                                   # always: account safety and answers from the team
    prefs = get(n.user)
    if not prefs['push'] or not prefs['cats'].get(key, True):
        return False
    if key != 'p2p' and _in_quiet(prefs['quiet']):
        return False                                  # P2P orders have a payment timer: they still come through
    return True


def safe_link(url):
    """Only links to real pages of the site ('' otherwise), so a tap never lands on an error page."""
    from django.urls import resolve, Resolver404
    url = str(url or '')
    if not url.startswith('/') or url.startswith('//'):
        return ''
    try:
        resolve(url.split('?')[0].split('#')[0])
        return url
    except Resolver404:
        return ''
