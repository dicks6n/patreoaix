# store/facebook.py
"""
Posts the signal posters (store/poster.py) to a Facebook PAGE, through the Graph API.

Meta only lets apps post to Pages (not personal profiles, also not profiles in "professional mode").

Setup (once):
  1. Create a Facebook Page for PATREOAIX (facebook.com → Pages → Create).
  2. developers.facebook.com → My Apps → Create app → type "Business". Add the product "Facebook Login for Business"
     (or "Facebook Login"). Valid OAuth Redirect URI:  https://www.patreoaix.com/facebook/callback/
  3. .env:  FACEBOOK_APP_ID=…   FACEBOOK_APP_SECRET=…   (App settings → Basic)
  4. On the site, as staff: Signal Posters → "Connect Facebook" → log in → pick the Page → allow.
     You are the app's admin and the Page's admin, so posting to your own Page needs no App Review.

The login gives a long-lived user token; the Page token taken from it does not expire. Both are saved in
.facebook.json next to manage.py (never uploaded: it is in .gitignore). If you change your Facebook password or
remove the app, press Connect again.

Settings (.env): FACEBOOK_ENABLED (True), FACEBOOK_MIN_GRADE (A), FACEBOOK_DAILY_LIMIT (8),
FACEBOOK_POST_RESULTS (True), FACEBOOK_GRAPH_VERSION (v23.0).
"""
import json
import logging
import os
import secrets
import time
import urllib.parse

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

SCOPES = 'pages_show_list,pages_read_engagement,pages_manage_posts'
GRADE_RANK = {'B': 1, 'A': 2, 'A+': 3}


def _cfg(name, default=''):
    return str(getattr(settings, name, '') or os.getenv(name, '') or default)


def graph():
    return f"https://graph.facebook.com/{_cfg('FACEBOOK_GRAPH_VERSION', 'v23.0')}/"


def token_file():
    return os.path.join(settings.BASE_DIR, '.facebook.json')


def configured():
    return bool(_cfg('FACEBOOK_APP_ID') and _cfg('FACEBOOK_APP_SECRET'))


def _load():
    try:
        with open(token_file(), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(data):
    path = token_file()
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def page():
    """The Page posters go to: {'id', 'name', 'access_token'} or None."""
    d = _load()
    pid = _cfg('FACEBOOK_PAGE_ID') or d.get('page_id')
    for p in d.get('pages') or []:
        if str(p.get('id')) == str(pid):
            return p
    pages = d.get('pages') or []
    return pages[0] if len(pages) == 1 else None


def pages():
    return [{'id': p['id'], 'name': p.get('name', '')} for p in _load().get('pages') or []]


def connected():
    return page() is not None


def enabled():
    return configured() and connected() and _cfg('FACEBOOK_ENABLED', 'True') == 'True'


def redirect_uri(request=None):
    if _cfg('FACEBOOK_REDIRECT_URI'):
        return _cfg('FACEBOOK_REDIRECT_URI')
    if request is not None:
        return request.build_absolute_uri('/facebook/callback/')
    return f"{_cfg('SITE_URL').rstrip('/')}/facebook/callback/"


# ═══════════════════════════════════════════════════════════════
# LOGIN
# ═══════════════════════════════════════════════════════════════
def auth_url(request):
    state = secrets.token_urlsafe(24)
    request.session['fb_state'] = state
    request.session['fb_redirect'] = redirect_uri(request)
    q = {'client_id': _cfg('FACEBOOK_APP_ID'), 'redirect_uri': request.session['fb_redirect'], 'state': state,
         'scope': SCOPES, 'response_type': 'code'}
    return f"https://www.facebook.com/{_cfg('FACEBOOK_GRAPH_VERSION', 'v23.0')}/dialog/oauth?" + urllib.parse.urlencode(q)


def _get(path, **params):
    import requests
    r = requests.get(graph() + path, params=params, timeout=25)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f'Facebook HTTP {r.status_code}')
    if 'error' in body:
        e = body['error']
        raise RuntimeError(e.get('error_user_msg') or e.get('message') or str(e))
    return body


def finish_login(request, code, state):
    expected = request.session.pop('fb_state', None)
    if not expected or not secrets.compare_digest(str(state), expected):
        raise RuntimeError('Login expired or did not start on this site. Press "Connect Facebook" again.')
    back = request.session.pop('fb_redirect', None) or redirect_uri(request)
    app_id, secret = _cfg('FACEBOOK_APP_ID'), _cfg('FACEBOOK_APP_SECRET')
    short = _get('oauth/access_token', client_id=app_id, client_secret=secret, redirect_uri=back, code=code)['access_token']
    long = _get('oauth/access_token', grant_type='fb_exchange_token', client_id=app_id, client_secret=secret,
                fb_exchange_token=short)['access_token']
    # Page tokens taken from a long-lived user token do not expire
    accounts = _get('me/accounts', access_token=long, fields='id,name,access_token,tasks', limit=100).get('data') or []
    pages_ = [{'id': a['id'], 'name': a.get('name', ''), 'access_token': a['access_token']}
              for a in accounts if a.get('access_token') and ('CREATE_CONTENT' in (a.get('tasks') or []) or not a.get('tasks'))]
    if not pages_:
        raise RuntimeError('No Facebook Page found that you can post to. Create a Page (or pick it on the Facebook '
                           'login screen under "Choose the Pages"), then press Connect again.')
    old = _load()
    keep = old.get('page_id') if any(p['id'] == old.get('page_id') for p in pages_) else None
    _save({'user_token': long, 'pages': pages_, 'page_id': keep or (pages_[0]['id'] if len(pages_) == 1 else None),
           'connected_at': int(time.time())})
    return pages_


def choose_page(page_id):
    d = _load()
    if not any(str(p['id']) == str(page_id) for p in d.get('pages') or []):
        raise RuntimeError('That Page is not in your Facebook login.')
    d['page_id'] = str(page_id)
    _save(d)


def disconnect():
    try:
        os.remove(token_file())
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════
# POSTING
# ═══════════════════════════════════════════════════════════════
def post_photo(image_url, caption):
    """Publish one photo with its caption on the Page. Returns the post id."""
    import requests
    p = page()
    if not p:
        raise RuntimeError('Facebook is not connected (Signal Posters → Connect Facebook).')
    r = requests.post(graph() + f"{p['id']}/photos", timeout=60,
                      data={'url': image_url, 'caption': caption[:5000], 'published': 'true', 'access_token': p['access_token']})
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f'Facebook HTTP {r.status_code}')
    if 'error' in body:
        e = body['error']
        msg = e.get('error_user_msg') or e.get('message') or str(e)
        if e.get('code') in (190, 102):
            msg += ' (the Facebook login expired: press Connect Facebook again)'
        raise RuntimeError(msg)
    return body.get('post_id') or body.get('id')


def poster_url(rel):
    return f"{_cfg('SITE_URL').rstrip('/')}{settings.MEDIA_URL}{rel.replace(os.sep, '/')}"


def post_poster(sig, rel, result=False):
    """Post one saved poster. Grade filter, daily cap and no duplicates (like TikTok)."""
    if not enabled() or not rel:
        return None
    if result and _cfg('FACEBOOK_POST_RESULTS', 'True') != 'True':
        return None
    if GRADE_RANK.get(sig.grade, 0) < GRADE_RANK.get(_cfg('FACEBOOK_MIN_GRADE', 'A'), 2):
        return None
    key = f"fb:sent:{sig.id}:{'r' if result else 's'}"
    if not cache.add(key, 1, 90 * 86400):
        return None
    day = f"fb:day:{time.strftime('%Y%m%d')}"
    cache.add(day, 0, 2 * 86400)
    if (cache.get(day) or 0) >= int(_cfg('FACEBOOK_DAILY_LIMIT', '8')):
        logger.info(f'Facebook daily limit reached; poster for signal {sig.id} not posted')
        return None
    from . import poster
    text = poster.caption(sig, result)
    try:
        pid = post_photo(poster_url(rel), text)
    except Exception:
        cache.delete(key)
        raise
    try:
        cache.incr(day)
    except ValueError:
        cache.set(day, 1, 2 * 86400)
    logger.info(f'Facebook post for signal {sig.id}: {pid}')
    return pid


def safe_post(sig, rel, result=False):
    """Never let Facebook break the scanner."""
    try:
        return post_poster(sig, rel, result)
    except Exception as e:
        logger.warning(f'Facebook post for signal {sig.id} failed: {e}')
        return None
