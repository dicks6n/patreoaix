# store/spaces.py
"""
Two sides of one website: LEARN (Academy, practice, chart coach, news explained) and TRADE (wallet, terminal,
signals, bots, funded). Picked on the login / signup page and switched any time from the header.

The choice is saved on UserProfile.space and in a cookie (so the login page remembers it before you sign in).
Each side has its own home, sidebar and phone bar; nothing is locked: a link to the other side always works.
"""
SPACES = ('learn', 'trade')
COOKIE = 'px_space'
HOME = {'learn': '/academy/', 'trade': '/'}


def clean(v):
    v = str(v or '').lower().strip()
    return v if v in SPACES else ''


def of(request):
    """The visitor's side: saved choice, else cookie, else 'learn' for students (start card), else 'trade'."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        from .models import UserProfile
        row = UserProfile.objects.filter(user=user).values('space', 'goal').first() or {}
        if clean(row.get('space')):
            return row['space']
        if clean(request.COOKIES.get(COOKIE)):
            return request.COOKIES[COOKIE]
        return 'learn' if row.get('goal') == 'learn' else 'trade'
    return clean(request.COOKIES.get(COOKIE)) or 'trade'


def save(request, user, space, response=None):
    space = clean(space)
    if not space:
        return ''
    if user is not None and user.is_authenticated:
        from .models import UserProfile
        UserProfile.objects.update_or_create(user=user, defaults={'space': space})
    if response is not None:
        response.set_cookie(COOKIE, space, max_age=365 * 86400, samesite='Lax', secure=request.is_secure())
    return space
