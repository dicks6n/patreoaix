# store/tiktok.py
"""
Posts the signal posters (store/poster.py) to TikTok as photo posts, through TikTok's Content Posting API.

Setup (once):
  1. developers.tiktok.com → Manage apps → Create app. Add the products "Login Kit" and "Content Posting API"
     (turn on "Direct Post"). Scopes: user.info.basic, video.publish.
  2. Login Kit → Redirect URI:  https://patreoaix.com/tiktok/callback/
  3. Content Posting API → "Verify domain / URL prefix":  https://patreoaix.com/media/posters/
     (TikTok downloads each poster from that address, so posting works from the hosted site, not localhost)
  4. .env:  TIKTOK_CLIENT_KEY=…   TIKTOK_CLIENT_SECRET=…
  5. On the site, as staff: Signal Posters → "Connect TikTok" → log in → Allow.
  6. python manage.py tiktok_test   → shows the connected account; --post posts the newest poster.

Until TikTok approves (audits) the app, TikTok only allows private posts (SELF_ONLY): they appear on the
account but only you can see them. Submit the app for review in the developer portal to post publicly.

The login is saved in .tiktok.json next to manage.py (never uploaded: it is in .gitignore). The access token
lasts 24 h and is renewed automatically with the refresh token (valid one year).
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

AUTH_URL = 'https://www.tiktok.com/v2/auth/authorize/'
API = 'https://open.tiktokapis.com/v2/'
SCOPES = 'user.info.basic,video.publish'
GRADE_RANK = {'B': 1, 'A': 2, 'A+': 3}


def _cfg(name, default=''):
    return str(getattr(settings, name, '') or default)


def token_file():
    return os.path.join(settings.BASE_DIR, '.tiktok.json')


def configured():
    return bool(_cfg('TIKTOK_CLIENT_KEY') and _cfg('TIKTOK_CLIENT_SECRET'))


def _load():
    try:
        with open(token_file(), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(tok):
    path = token_file()
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(tok, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def connected():
    return bool(_load().get('refresh_token'))


def enabled():
    return configured() and connected() and _cfg('TIKTOK_ENABLED', 'True') == 'True'


def redirect_uri(request=None):
    """The callback on the address the visitor is using (patreoaix.com or www.patreoaix.com), so the login comes
    back to the same session. Register both forms in the TikTok app; TIKTOK_REDIRECT_URI forces one."""
    if _cfg('TIKTOK_REDIRECT_URI'):
        return _cfg('TIKTOK_REDIRECT_URI')
    if request is not None:
        return request.build_absolute_uri('/tiktok/callback/')
    return f"{_cfg('SITE_URL').rstrip('/')}/tiktok/callback/"


# ═══════════════════════════════════════════════════════════════
# LOGIN (OAuth)
# ═══════════════════════════════════════════════════════════════
def auth_url(request):
    state = secrets.token_urlsafe(24)
    request.session['tiktok_state'] = state
    request.session['tiktok_redirect'] = redirect_uri(request)      # the token exchange must send the same one
    q = {'client_key': _cfg('TIKTOK_CLIENT_KEY'), 'scope': SCOPES, 'response_type': 'code',
         'redirect_uri': request.session['tiktok_redirect'], 'state': state}
    return AUTH_URL + '?' + urllib.parse.urlencode(q)


def _token_request(data):
    import requests
    data = {'client_key': _cfg('TIKTOK_CLIENT_KEY'), 'client_secret': _cfg('TIKTOK_CLIENT_SECRET'), **data}
    r = requests.post(API + 'oauth/token/', data=data, timeout=20,
                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
    body = r.json()
    if 'access_token' not in body:
        raise RuntimeError(body.get('error_description') or body.get('error') or f'TikTok HTTP {r.status_code}')
    body['expires_at'] = time.time() + int(body.get('expires_in', 86400)) - 120
    return body


def finish_login(request, code, state):
    expected = request.session.pop('tiktok_state', None)
    if not expected or not secrets.compare_digest(str(state), expected):
        raise RuntimeError('Login expired or did not start on this site. Press "Connect TikTok" again.')
    back = request.session.pop('tiktok_redirect', None) or redirect_uri(request)
    tok = _token_request({'code': code, 'grant_type': 'authorization_code', 'redirect_uri': back})
    _save(tok)
    return tok


def access_token():
    tok = _load()
    if not tok.get('refresh_token'):
        raise RuntimeError('TikTok is not connected (Signal Posters → Connect TikTok).')
    if time.time() >= float(tok.get('expires_at', 0)):
        new = _token_request({'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token']})
        tok.update(new)
        _save(tok)
    return tok['access_token']


def disconnect():
    try:
        os.remove(token_file())
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════
def api(path, payload=None, timeout=25):
    import requests
    r = requests.post(API + path, json=payload or {}, timeout=timeout,
                      headers={'Authorization': f'Bearer {access_token()}', 'Content-Type': 'application/json; charset=UTF-8'})
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f'TikTok HTTP {r.status_code}')
    err = body.get('error') or {}
    if err.get('code') not in (None, 'ok'):
        raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    return body.get('data') or {}


def user_info():
    import requests
    r = requests.get(API + 'user/info/', params={'fields': 'open_id,display_name,avatar_url'}, timeout=20,
                     headers={'Authorization': f'Bearer {access_token()}'})
    return (r.json().get('data') or {}).get('user') or {}


def creator_info():
    return api('post/publish/creator_info/query/')


def _privacy(info):
    """PUBLIC when TikTok allows it for this app/account, otherwise SELF_ONLY (unaudited apps)."""
    options = info.get('privacy_level_options') or ['SELF_ONLY']
    wanted = _cfg('TIKTOK_PRIVACY', 'PUBLIC_TO_EVERYONE')
    return wanted if wanted in options else ('PUBLIC_TO_EVERYONE' if 'PUBLIC_TO_EVERYONE' in options else options[0])


def post_photo(image_url, title, description):
    """Direct-post one photo. Returns TikTok's publish_id."""
    info = creator_info()
    data = api('post/publish/content/init/', {
        'post_info': {
            'title': title[:90],
            'description': description[:4000],
            'privacy_level': _privacy(info),
            'disable_comment': bool(info.get('comment_disabled')),
            'auto_add_music': True,
        },
        'source_info': {'source': 'PULL_FROM_URL', 'photo_cover_index': 0, 'photo_images': [image_url]},
        'post_mode': 'DIRECT_POST',
        'media_type': 'PHOTO',
    })
    return data.get('publish_id')


def status(publish_id):
    return api('post/publish/status/fetch/', {'publish_id': publish_id})


# ═══════════════════════════════════════════════════════════════
# SIGNAL POSTERS
# ═══════════════════════════════════════════════════════════════
def poster_url(rel):
    return f"{_cfg('SITE_URL').rstrip('/')}{settings.MEDIA_URL}{rel.replace(os.sep, '/')}"


def post_poster(sig, rel, result=False):
    """Post one saved poster (path relative to MEDIA_ROOT). Grade filter, daily cap and no duplicates."""
    if not enabled() or not rel:
        return None
    if result and _cfg('TIKTOK_POST_RESULTS', 'True') != 'True':
        return None
    if GRADE_RANK.get(sig.grade, 0) < GRADE_RANK.get(_cfg('TIKTOK_MIN_GRADE', 'A'), 2):
        return None
    key = f"tt:sent:{sig.id}:{'r' if result else 's'}"
    if not cache.add(key, 1, 90 * 86400):
        return None
    day = f"tt:day:{time.strftime('%Y%m%d')}"
    cache.add(day, 0, 2 * 86400)
    if (cache.get(day) or 0) >= int(_cfg('TIKTOK_DAILY_LIMIT', '10')):
        logger.info(f'TikTok daily limit reached; poster for signal {sig.id} not posted')
        return None
    from . import poster
    text = poster.caption(sig, result)
    title = text.split('\n', 1)[0]
    try:
        pid = post_photo(poster_url(rel), title, text)
    except Exception:
        cache.delete(key)                          # try again with the next poster
        raise
    try:
        cache.incr(day)
    except ValueError:
        cache.set(day, 1, 2 * 86400)
    logger.info(f'TikTok post started for signal {sig.id}: {pid}')
    return pid


def safe_post(sig, rel, result=False):
    """Never let TikTok break the scanner."""
    try:
        return post_poster(sig, rel, result)
    except Exception as e:
        logger.warning(f'TikTok post for signal {sig.id} failed: {e}')
        return None
