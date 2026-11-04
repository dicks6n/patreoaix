# store/seo.py
"""
What search engines read: /robots.txt, /sitemap.xml, /favicon.ico, and the site-wide meta tags
(context processor `seo`: Google / Bing verification codes, the share image, the logo).

Google shows a site's icon in results from the homepage's <link rel="icon"> (at least 48×48) or /favicon.ico;
without them it shows a grey globe. The logo in the knowledge panel comes from the Organization JSON-LD.
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.cache import cache_control

STATIC = Path(__file__).resolve().parent / 'static' / 'store'
PRIVATE = ['/admin/', '/api/', '/cron/', '/accounts/', '/wallet/', '/p2p/', '/terminal/', '/dashboard/', '/profile/',
           '/notifications/', '/subscription/', '/account-operations/', '/kyc/', '/rewards/', '/welcome/', '/support/']


def site():
    return str(getattr(settings, 'SITE_URL', '') or 'https://www.patreoaix.com').rstrip('/')


@cache_control(max_age=86400, public=True)
def robots_txt(request):
    lines = ['User-agent: *', 'Allow: /'] + [f'Disallow: {p}' for p in PRIVATE] + ['', f'Sitemap: {site()}/sitemap.xml', '']
    return HttpResponse('\n'.join(lines), content_type='text/plain')


def _urls():
    from . import academy_library as lib
    from .views import LEGAL_DOCS
    out = [('/', '1.0', 'daily'), ('/academy/', '0.9', 'weekly'), ('/academy/cheatsheet/', '0.8', 'monthly'),
           ('/academy/cheatsheet/?show=patterns', '0.8', 'monthly'), ('/results/', '0.7', 'daily'),
           ('/platforms/download/', '0.7', 'monthly'), ('/legal/', '0.3', 'yearly')]
    for c in lib.COURSES:
        out.append((f"/academy/c/{c['key']}/", '0.8', 'monthly'))
        for t in c['topics']:
            out.append((f"/academy/c/{c['key']}/{t['key']}/", '0.7', 'monthly'))
    for slug, _ in LEGAL_DOCS:
        out.append((f'/legal/{slug}/', '0.3', 'yearly'))
    return out


@cache_control(max_age=3600, public=True)
def sitemap_xml(request):
    from xml.sax.saxutils import escape
    base = site()
    rows = ''.join(f'<url><loc>{escape(base + u)}</loc><changefreq>{f}</changefreq><priority>{p}</priority></url>'
                   for u, p, f in _urls())
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'
    return HttpResponse(xml, content_type='application/xml')


@cache_control(max_age=604800, public=True)
def favicon(request):
    f = STATIC / 'favicon.ico'
    if not f.exists():
        raise Http404
    return FileResponse(open(f, 'rb'), content_type='image/x-icon')


def context(request):
    """Template context: verification codes and absolute URLs for the share image and the logo."""
    base = site()
    return {'seo_google': getattr(settings, 'GOOGLE_SITE_VERIFICATION', ''), 'seo_bing': getattr(settings, 'BING_SITE_VERIFICATION', ''),
            'seo_site': base, 'seo_image': f'{base}/static/store/social-card.png', 'seo_logo': f'{base}/static/store/icons/icon-512.png',
            'seo_telegram': getattr(settings, 'TELEGRAM_INVITE_URL', '')}
