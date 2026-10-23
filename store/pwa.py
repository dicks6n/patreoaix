# store/pwa.py
"""
Installable web app ("Add to Home Screen"):

  /manifest.webmanifest  app name, icons, colours; opens straight into the MT5 terminal
  /sw.js                 service worker (must live at the site root to control every page)
  /offline/              shown when the app is opened without a connection

The service worker never caches API calls, prices or form posts: trading data is
always live. It only keeps the app shell, icons and static files so the app opens
instantly, and falls back to the offline page when there is no network.
"""
import json

from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

APP_VERSION = '2026-10-19.1'          # bump to make installed apps refresh their cache


def _icon(path):
    try:
        return static(path)
    except Exception:
        return '/static/' + path


@require_GET
@cache_control(max_age=3600, public=True)
def manifest(request):
    icons = [
        {'src': _icon('store/img/icons/icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
        {'src': _icon('store/img/icons/icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
        {'src': _icon('store/img/icons/icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
    ]
    data = {
        'id': '/terminal/',
        'name': 'PATREOAIX Trader',
        'short_name': 'PATREOAIX',
        'description': 'Trade forex, gold and crypto with AI signals: MT5-style terminal.',
        'start_url': '/terminal/?source=pwa',
        'scope': '/',
        'display': 'standalone',
        'display_override': ['standalone', 'minimal-ui'],
        'orientation': 'any',
        'background_color': '#000000',
        'theme_color': '#000000',
        'categories': ['finance', 'business'],
        'icons': icons,
        'shortcuts': [
            {'name': 'Terminal', 'url': '/terminal/?source=pwa', 'icons': [icons[0]]},
            {'name': 'Dashboard', 'url': '/?source=pwa', 'icons': [icons[0]]},
            {'name': 'Market overview', 'url': '/market-overview/?source=pwa', 'icons': [icons[0]]},
            {'name': 'Deposit', 'url': '/account-operations/funding/payment-methods/deposit?source=pwa', 'icons': [icons[0]]},
        ],
    }
    return JsonResponse(data, content_type='application/manifest+json', json_dumps_params={'indent': 1})


SW = r"""
// PATREOAIX service worker  (version __VERSION__)
const CACHE = 'px-shell-__VERSION__';
const OFFLINE = '/offline/';
const PRECACHE = [OFFLINE, __ICONS__];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

const NEVER = [/\/api\//, /\/ws\//, /\/admin\//, /\/trade\/execute/, /\/accounts\//, /\/login/, /\/logout/, /\/signup/, /\/deriv-callback/, /\/connect-/];

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                                  // orders, forms: straight to the server
  const url = new URL(req.url);
  if (url.origin === location.origin && NEVER.some(r => r.test(url.pathname))) return;   // live data is never cached

  // pages are never stored (they hold balances and the security token): network, or the offline screen
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }

  // static files and the CDN libraries the pages use: serve fast from cache, refresh in the background
  const isStatic = (url.origin === location.origin && url.pathname.startsWith('/static/')) ||
                   /(cdnjs\.cloudflare\.com|unpkg\.com|cdn\.jsdelivr\.net|cdn\.tailwindcss\.com|fonts\.(googleapis|gstatic)\.com)$/.test(url.hostname);
  if (isStatic) {
    e.respondWith(caches.open(CACHE).then(async c => {
      const hit = await c.match(req);
      const net = fetch(req).then(res => { if (res && (res.ok || res.type === 'opaque')) c.put(req, res.clone()); return res; }).catch(() => hit);
      return hit || net;
    }));
  }
});

self.addEventListener('message', e => { if (e.data === 'skip-waiting') self.skipWaiting(); });
"""


@require_GET
def service_worker(request):
    icons = [_icon(p) for p in ('store/img/icons/icon-192.png', 'store/img/icons/icon-512.png', 'store/img/icons/apple-touch-icon.png')]
    js = SW.replace('__VERSION__', APP_VERSION).replace('__ICONS__', ', '.join(json.dumps(i) for i in icons))
    resp = HttpResponse(js, content_type='application/javascript')
    resp['Service-Worker-Allowed'] = '/'
    resp['Cache-Control'] = 'no-cache'
    return resp


OFFLINE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#000000">
<title>Offline · PATREOAIX</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif}
  .w{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;padding-top:max(24px,env(safe-area-inset-top));box-sizing:border-box;gap:14px}
  img{width:72px;height:72px;border-radius:18px}
  h1{font-size:20px;margin:6px 0 0} p{color:#8e8e93;font-size:14px;line-height:1.5;margin:0;max-width:320px}
  button{margin-top:10px;background:#0a84ff;color:#fff;border:0;border-radius:12px;padding:14px 28px;font-size:16px;font-weight:600}
</style></head><body><div class="w">
<img src="__ICON__" alt="">
<h1>You're offline</h1>
<p>Prices and orders need a connection. Your open positions keep running on the server, and stops and targets still apply.</p>
<button onclick="location.href='/terminal/'">Try again</button>
</div><script>addEventListener('online',()=>location.href='/terminal/')</script></body></html>"""


@require_GET
@cache_control(max_age=86400, public=True)
def offline(request):
    return HttpResponse(OFFLINE_HTML.replace('__ICON__', _icon('store/img/icons/icon-192.png')))
