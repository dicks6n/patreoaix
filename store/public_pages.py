# store/public_pages.py
"""
Public pages written by the site itself, for people arriving from Google (no login needed):

  /news/                          the latest big releases (NFP, CPI, FOMC …): result + how gold, EURUSD … moved
  /news/<event>/                  one release's hub: every past result, beat rate, usual move per market, next date
  /news/<event>/<yyyy-mm-dd>/     one release: actual vs forecast, beat / miss, 15m / 1h / 4h move in % and pips,
                                  a short write-up created from the numbers
  /calendar/                      the economic calendar (/economic-calendar/ redirects here; open to everyone)
  /blog/, /blog/<slug>/           articles; the weekly outlook (Sunday / Monday) and the weekly recap (Saturday)
                                  are written automatically by the background scanner (`safe_weekly`)

All numbers come from EconEvent / NewsReaction (store/news_history.py) and ScannerSignal: nothing is invented.
"""
import json
import logging
from datetime import datetime, timedelta, timezone as dt_tz

from django.core.cache import cache
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

from . import news_history as nh
from .news_live import pip_size

logger = logging.getLogger(__name__)

# What each release is, in one sentence (shown on the hub page and used as the Google description).
ABOUT = {
    'NFP': 'Non-farm payrolls counts the jobs the US economy added last month. It comes out on the first Friday of '
           'the month at 8:30 New York time and is the most traded release of the month for gold, the dollar and indices.',
    'CPI': 'The consumer price index measures US inflation month on month. A hotter number usually means higher '
           'rates for longer: a stronger dollar and pressure on gold.',
    'CORE_CPI': 'Core CPI is inflation without food and energy, the number the Federal Reserve watches most closely.',
    'FOMC': 'The Federal Reserve decides the US interest rate eight times a year. The decision comes at 14:00 New '
            'York time and the press conference 30 minutes later often brings a second move.',
    'PPI': 'The producer price index measures inflation at the factory gate, often a hint for the next CPI.',
    'CORE_PCE': 'Core PCE is the Federal Reserve\'s preferred inflation measure.',
    'GDP': 'Gross domestic product is the broadest measure of how fast the US economy is growing.',
    'RETAIL': 'Retail sales show how much US consumers spent last month, a big part of economic growth.',
    'ECB': 'The European Central Bank sets the euro-area interest rate; the press conference moves EURUSD as much '
           'as the decision.',
    'BOE': 'The Bank of England\'s Monetary Policy Committee sets the UK interest rate.',
    'BOJ': 'The Bank of Japan sets Japan\'s interest rate; any change moves USDJPY sharply.',
}
RATE_KEYS = ('FOMC', 'ECB', 'BOE', 'BOJ')                 # a rate decision is hawkish / dovish, not a beat / miss
RATE_WORDS = {1: 'Hawkish', -1: 'Dovish', 0: 'As expected'}
SHORT = {'NFP': 'NFP', 'FOMC': 'FOMC', 'CPI': 'CPI', 'CORE_CPI': 'core CPI', 'PPI': 'PPI', 'CORE_PCE': 'core PCE',
         'GDP': 'GDP', 'RETAIL': 'retail sales', 'ECB': 'ECB', 'BOE': 'BoE', 'BOJ': 'BoJ'}
HEADLINE_ORDER = ('NFP', 'FOMC', 'CPI', 'ECB', 'BOE', 'BOJ', 'CORE_PCE', 'GDP', 'PPI', 'RETAIL', 'CORE_CPI')
NAMES = {'XAUUSD': 'Gold', 'EURUSD': 'EUR/USD', 'GBPUSD': 'GBP/USD', 'USDJPY': 'USD/JPY', 'BTCUSD': 'Bitcoin',
         'EURGBP': 'EUR/GBP'}


def slug_of(key):
    return key.lower().replace('_', '-')


def key_of(slug):
    key = str(slug or '').upper().replace('-', '_')
    if key not in nh.BIG:
        raise Http404
    return key


def _site():
    from .seo import site
    return site()


def _ld(data):
    """JSON-LD for a <script> tag (no '</' can close it early)."""
    return mark_safe(json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))


def _fmt(v, unit=''):
    if v is None:
        return '—'
    s = f'{v:,.2f}'.rstrip('0').rstrip('.') if abs(v) < 1000 else f'{v:,.0f}'
    return f'{s}{unit}'


def _pips(symbol, pct, price):
    if pct is None or not price:
        return None
    return round(price * pct / 100 / pip_size(symbol))


def _symbol_order(ev):
    order = nh.SYMBOLS.get(ev.currency, [])
    return lambda r: order.index(r.symbol) if r.symbol in order else 9


def release_dict(ev):
    """One release with its reactions, ready for the page."""
    sign = nh.surprise_sign(ev)
    unit = ev.unit or ''
    label, currency, _, good = nh.EVENTS[ev.key]
    surprise = (ev.actual - ev.forecast) if ev.actual is not None and ev.forecast is not None else None
    rows = []
    for r in sorted(ev.reactions.all(), key=_symbol_order(ev)):
        rows.append({'symbol': r.symbol, 'name': NAMES.get(r.symbol, r.symbol), 'price0': r.price0,
                     'm15': r.m15, 'h1': r.h1, 'h4': r.h4,
                     'pips15': _pips(r.symbol, r.m15, r.price0), 'pips1h': _pips(r.symbol, r.h1, r.price0),
                     'pips4h': _pips(r.symbol, r.h4, r.price0),
                     'high1h': _pips(r.symbol, r.up1h, r.price0), 'low1h': _pips(r.symbol, r.down1h, r.price0),
                     'path': r.path or []})
    return {'ev': ev, 'key': ev.key, 'slug': slug_of(ev.key), 'label': label, 'currency': currency,
            'date': ev.when.date(), 'url': f'/news/{slug_of(ev.key)}/{ev.when:%Y-%m-%d}/',
            'actual': _fmt(ev.actual, unit), 'forecast': _fmt(ev.forecast, unit), 'previous': _fmt(ev.previous, unit),
            'sign': sign, 'result': ((RATE_WORDS if ev.key in RATE_KEYS else {1: 'Beat', -1: 'Miss', 0: 'In line'})[sign]
                                     if ev.actual is not None else 'Pending'),
            'surprise': surprise, 'surprise_txt': (f'{surprise:+,.2f}'.rstrip('0').rstrip('.') + unit) if surprise else '',
            'higher_is_good': good, 'rows': rows}


def story(rel):
    """A short write-up from the numbers alone (no guessing about why)."""
    ev, label, cur = rel['ev'], rel['label'], rel['currency']
    if ev.actual is None:
        return [f"{label} is due on {ev.when:%A %d %B %Y} at {ev.when:%H:%M} GMT. "
                f"Forecast {rel['forecast']}, previous {rel['previous']}."]
    out = []
    if rel['sign'] == 0:
        out.append(f"{label} came out at {rel['actual']} on {ev.when:%A %d %B %Y}, in line with the forecast of "
                   f"{rel['forecast']} (previous {rel['previous']}). With no surprise, markets usually move less "
                   f"and in both directions.")
    else:
        word = 'better' if rel['sign'] > 0 else 'worse'
        if ev.key in RATE_KEYS:
            out.append(f"{label}: the rate was set at {rel['actual']} on {ev.when:%A %d %B %Y} against an expected "
                       f"{rel['forecast']} (previous {rel['previous']}), a {'hawkish' if rel['sign'] > 0 else 'dovish'} "
                       f"surprise, usually {'supportive' if rel['sign'] > 0 else 'negative'} for the {cur}.")
        else:
            out.append(f"{label} came out at {rel['actual']} on {ev.when:%A %d %B %Y} at {ev.when:%H:%M} GMT against a "
                       f"forecast of {rel['forecast']} (previous {rel['previous']}): {word} than expected for the "
                       f"{cur}, a {rel['surprise_txt']} surprise.")
    moves = []
    for r in rel['rows']:
        if r['h1'] is None:
            continue
        way = 'rose' if r['h1'] > 0 else 'fell'
        p = f" ({abs(r['pips1h']):,} pips)" if r['pips1h'] is not None else ''
        moves.append(f"{r['name']} {way} {abs(r['h1']):.2f}%{p}")
    if moves:
        out.append('In the first hour ' + ', '.join(moves[:-1]) + (' and ' if len(moves) > 1 else '') + moves[-1] + '.')
    big = max((r for r in rel['rows'] if r['h1'] is not None and r['m15'] is not None), key=lambda r: abs(r['h1']),
              default=None)
    if big and big['m15'] and big['h1']:
        if big['m15'] * big['h1'] > 0 and abs(big['h1']) >= abs(big['m15']):
            out.append(f"The first 15-minute move in {big['name']} kept going for the rest of the hour.")
        elif big['m15'] * big['h1'] < 0:
            out.append(f"The first spike in {big['name']} reversed within the hour: the first candle was a trap.")
    return out


def hub_stats(key):
    """Beat rate and the usual 1-hour move per market, from every stored release."""
    from .news_live import beat_odds
    p_beat, _, n = beat_odds(key)
    markets = []
    for sym in nh.SYMBOLS.get(nh.EVENTS[key][1], []):
        st = nh.symbol_stats(key, sym)
        if not st:
            continue
        price = st.get('price_now')
        row = {'symbol': sym, 'name': NAMES.get(sym, sym), 'n': st['n'], 'abs_h1': st['abs_h1'],
               'pips_h1': abs(_pips(sym, st['abs_h1'], price) or 0) or None, 'up_share': round(st['up_share'] * 100)}
        for side in ('beat', 'miss'):
            s = st.get(side)
            row[side] = ({'n': s['n'], 'avg': s['avg_h1'], 'pips': _pips(sym, s['avg_h1'], price),
                          'same': round(s['same_way'] * 100)} if s and s['n'] >= 2 else None)
        markets.append(row)
    return {'p_beat': round(p_beat * 100), 'n': n, 'markets': markets}


def next_release(key):
    """The next date of this release if it is in the calendar feed (about a week ahead)."""
    from .calendar_feed import get_calendar
    now = datetime.now(dt_tz.utc)
    try:
        evs = [e for e in get_calendar(days_ahead=7).get('events') or []
               if isinstance(e.get('date'), datetime) and e['date'] > now
               and nh.classify(e.get('event'), e.get('currency')) == key]
    except Exception:
        return None
    return min(evs, key=lambda e: e['date']) if evs else None


def _releases(key=None, limit=40):
    from .models import EconEvent
    qs = EconEvent.objects.filter(key__in=[key] if key else nh.BIG, when__lte=timezone.now())
    seen, out = set(), []
    for ev in qs.prefetch_related('reactions').order_by('-when')[:limit * 3]:
        k = (ev.key, ev.when.date())
        if k in seen:
            continue
        seen.add(k)
        out.append(release_dict(ev))
        if len(out) >= limit:
            break
    return out


# ═══════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════
def news_index(request):
    releases = _releases(limit=30)
    hubs = [{'key': k, 'slug': slug_of(k), 'label': nh.EVENTS[k][0], 'currency': nh.EVENTS[k][1]} for k in nh.BIG]
    site = _site()
    ld = {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': 'Economic news results and market reactions',
          'url': f'{site}/news/', 'publisher': {'@type': 'Organization', 'name': 'PATREOAIX'}}
    return render(request, 'store/news_index.html', {'releases': releases, 'hubs': hubs, 'ld': _ld(ld)})


def news_event(request, event):
    key = key_of(event)
    releases = _releases(key, limit=36)
    label, currency = nh.EVENTS[key][0], nh.EVENTS[key][1]
    try:
        stats = hub_stats(key)
    except Exception as e:
        logger.warning(f'news hub stats {key}: {e}')
        stats = {'p_beat': 50, 'n': 0, 'markets': []}
    nxt = next_release(key)
    site = _site()
    crumbs = {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': [
        {'@type': 'ListItem', 'position': 1, 'name': 'News', 'item': f'{site}/news/'},
        {'@type': 'ListItem', 'position': 2, 'name': label, 'item': f'{site}/news/{slug_of(key)}/'}]}
    return render(request, 'store/news_event.html', {
        'key': key, 'slug': slug_of(key), 'label': label, 'currency': currency, 'about': ABOUT.get(key, ''),
        'releases': releases, 'stats': stats, 'next': nxt, 'ld': _ld(crumbs),
        'beats': sum(1 for r in releases if r['sign'] > 0), 'misses': sum(1 for r in releases if r['sign'] < 0)})


def news_release(request, event, day):
    from .models import EconEvent
    key = key_of(event)
    try:
        d = datetime.strptime(day, '%Y-%m-%d').date()
    except ValueError:
        raise Http404
    ev = (EconEvent.objects.filter(key=key, when__date=d).prefetch_related('reactions').order_by('-actual')
          .first())
    if not ev:
        raise Http404
    rel = release_dict(ev)
    text = story(rel)
    older = EconEvent.objects.filter(key=key, when__lt=ev.when.replace(hour=0, minute=0)).order_by('-when').first()
    newer = EconEvent.objects.filter(key=key, when__date__gt=d, when__lte=timezone.now()).order_by('when').first()
    site = _site()
    title = f"{rel['label']} {ev.when:%B %Y}: {rel['actual']} vs {rel['forecast']} forecast"
    ld = [{'@context': 'https://schema.org', '@type': 'NewsArticle', 'headline': title[:110],
           'description': ' '.join(text)[:300], 'datePublished': ev.when.isoformat(),
           'dateModified': ev.updated_at.isoformat(), 'image': [f'{site}/static/store/social-card.png'],
           'mainEntityOfPage': f"{site}{rel['url']}",
           'author': {'@type': 'Organization', 'name': 'PATREOAIX', 'url': site},
           'publisher': {'@type': 'Organization', 'name': 'PATREOAIX',
                         'logo': {'@type': 'ImageObject', 'url': f'{site}/static/store/icons/icon-512.png'}}},
          {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': [
              {'@type': 'ListItem', 'position': 1, 'name': 'News', 'item': f'{site}/news/'},
              {'@type': 'ListItem', 'position': 2, 'name': rel['label'], 'item': f"{site}/news/{rel['slug']}/"},
              {'@type': 'ListItem', 'position': 3, 'name': f'{ev.when:%d %b %Y}', 'item': f"{site}{rel['url']}"}]}]
    return render(request, 'store/news_release.html', {
        'rel': rel, 'story': text, 'title': title, 'ld': _ld(ld), 'about': ABOUT.get(key, ''),
        'older': release_dict(older) if older else None, 'newer': release_dict(newer) if newer else None,
        'path_json': json.dumps([{'s': r['name'], 'p': r['path']} for r in rel['rows'] if r['path']])})


def blog_index(request):
    from .models import BlogPost
    posts = BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())
    kind = request.GET.get('kind')
    if kind in dict(BlogPost.KINDS):
        posts = posts.filter(kind=kind)
    return render(request, 'store/blog_index.html', {'posts': posts[:60], 'kind': kind, 'kinds': BlogPost.KINDS})


def blog_post(request, slug):
    from .models import BlogPost
    post = get_object_or_404(BlogPost, slug=slug, is_published=True)
    site = _site()
    ld = {'@context': 'https://schema.org', '@type': 'BlogPosting', 'headline': post.title[:110],
          'description': post.summary, 'datePublished': post.published_at.isoformat(),
          'dateModified': post.updated_at.isoformat(), 'image': [f'{site}/static/store/social-card.png'],
          'mainEntityOfPage': f'{site}{post.get_absolute_url()}',
          'author': {'@type': 'Organization', 'name': 'PATREOAIX', 'url': site},
          'publisher': {'@type': 'Organization', 'name': 'PATREOAIX',
                        'logo': {'@type': 'ImageObject', 'url': f'{site}/static/store/icons/icon-512.png'}}}
    more = BlogPost.objects.filter(is_published=True).exclude(pk=post.pk)[:4]
    return render(request, 'store/blog_post.html', {'post': post, 'ld': _ld(ld), 'more': more})


# ═══════════════════════════════════════════════════════════════
# AUTOMATIC POSTS: weekly outlook + weekly recap
# ═══════════════════════════════════════════════════════════════
def _week_start(d):
    return d - timedelta(days=d.weekday())


def _a(href, text):
    return f'<a href="{escape(href)}">{escape(text)}</a>'


def _and(xs):
    return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' and ' + xs[-1]


def write_outlook(monday, force=False):
    """The week ahead: every high-impact release by day, what each one usually does, and links."""
    from .models import BlogPost
    from .calendar_feed import get_calendar
    slug = f'weekly-outlook-{monday:%Y-%m-%d}'
    if not force and BlogPost.objects.filter(slug=slug).exists():
        return None
    start = datetime(monday.year, monday.month, monday.day, tzinfo=dt_tz.utc)
    end = start + timedelta(days=7)
    events = [e for e in (get_calendar(days_ahead=8).get('events') or [])
              if isinstance(e.get('date'), datetime) and start <= e['date'] < end and e.get('impact') == 'high']
    if not events:
        return None
    events.sort(key=lambda e: e['date'])
    keys = []
    for e in events:
        k = nh.classify(e.get('event'), e.get('currency'))
        if k in nh.BIG and k not in keys:
            keys.append(k)
    top = sorted(keys, key=HEADLINE_ORDER.index)
    if 'CPI' in top and 'CORE_CPI' in top:
        top.remove('CORE_CPI')
    names = [SHORT[k] for k in top[:3]]
    headline = _and(names) if names else f'{len(events)} high-impact releases'
    title = f'Week ahead {monday.day} {monday:%b %Y}: {headline}'
    summary = (f'The high-impact economic releases for the week of {monday:%d %B %Y}, by day and time (GMT), and how '
               f'gold, EUR/USD, USD/JPY and Bitcoin usually react{": " + headline if names else ""}.')

    body = [f'<p>{escape(len(events))} high-impact releases are on the calendar this week. Times are GMT; the '
            f'{_a("/calendar/", "live economic calendar")} shows them in your own time zone and fills in the actual '
            f'numbers the moment they are out.</p>']
    day = None
    for e in events[:40]:
        if e['date'].date() != day:
            if day is not None:
                body.append('</ul>')
            day = e['date'].date()
            body.append(f'<h2>{e["date"]:%A %d %B}</h2><ul>')
        k = nh.classify(e.get('event'), e.get('currency'))
        name = escape(e.get('event') or '')
        if k in nh.BIG:
            name = _a(f'/news/{slug_of(k)}/', e.get('event') or '')
        fc = f' · forecast {escape(e["forecast"])}' if e.get('forecast') else ''
        pv = f' · previous {escape(e["previous"])}' if e.get('previous') else ''
        body.append(f'<li><b>{e["date"]:%H:%M}</b> {escape(e.get("currency") or "")} {name}{fc}{pv}</li>')
    body.append('</ul>')

    for k in keys[:5]:
        try:
            st = hub_stats(k)
        except Exception:
            continue
        label = nh.EVENTS[k][0]
        body.append(f'<h2>What {escape(label)} usually does</h2>')
        if ABOUT.get(k):
            body.append(f'<p>{escape(ABOUT[k])}</p>')
        if st['n'] >= 4:
            body.append(f'<p>Over the last {st["n"]} releases it beat the forecast {st["p_beat"]}% of the time.</p>')
        items = []
        for m in st['markets']:
            if m['pips_h1']:
                items.append(f'<li>{escape(m["name"])}: a typical first-hour move of {m["abs_h1"]:.2f}% '
                             f'(about {m["pips_h1"]:,} pips), measured over {m["n"]} releases</li>')
        if items:
            body.append('<ul>' + ''.join(items) + '</ul>')
        body.append(f'<p>Every past result: {_a(f"/news/{slug_of(k)}/", label + " history")}.</p>')

    body.append('<h2>How to trade a news week</h2><ul>'
                '<li>Stay flat or small in the minutes before a big number: spreads widen and the first spike often reverses.</li>'
                '<li>Let the first 15-minute candle close, then trade in the direction it holds, with the stop beyond it.</li>'
                '<li>Risk 1% or less per trade, more so on a news day.</li></ul>'
                f'<p>The {_a("/news-impact/", "live news scanner")} gives the odds before each release and a buy or '
                f'sell call with the expected pips the moment the number is out. New to this? Start with the free '
                f'{_a("/academy/c/fundamentals/", "fundamentals course")} in the academy.</p>')
    post, _ = BlogPost.objects.update_or_create(slug=slug, defaults=dict(
        title=title[:160], summary=summary[:300], body='\n'.join(body), kind='outlook', auto=True,
        published_at=max(start - timedelta(hours=6), timezone.now() - timedelta(days=6)), is_published=True))
    return post


def write_recap(monday, force=False):
    """The week just gone: each big release with its result and the moves, plus the signal results."""
    from .models import BlogPost, EconEvent, ScannerSignal
    slug = f'weekly-recap-{monday:%Y-%m-%d}'
    if not force and BlogPost.objects.filter(slug=slug).exists():
        return None
    start = datetime(monday.year, monday.month, monday.day, tzinfo=dt_tz.utc)
    end = start + timedelta(days=7)
    evs = list(EconEvent.objects.filter(key__in=nh.BIG, when__gte=start, when__lt=end, actual__isnull=False)
               .prefetch_related('reactions').order_by('when'))
    sigs = list(ScannerSignal.objects.filter(closed_at__gte=start, closed_at__lt=end).exclude(outcome='OPEN'))
    if not evs and not sigs:
        return None
    seen, rels = set(), []
    for ev in evs:
        if (ev.key, ev.when.date()) not in seen:
            seen.add((ev.key, ev.when.date()))
            rels.append(release_dict(ev))
    wins = sum(1 for s in sigs if s.outcome == 'TP')
    losses = sum(1 for s in sigs if s.outcome == 'SL')
    total_r = sum(s.result_r or 0 for s in sigs)
    top = sorted(rels, key=lambda r: HEADLINE_ORDER.index(r['key']))
    names = [f"{SHORT[r['key']]} {r['result'].lower()}" for r in top[:3]]
    title = f'Week in review {monday.day} {monday:%b %Y}' + (': ' + _and(names) if names else '')
    summary = (f'What happened in the week of {monday:%d %B %Y}: '
               + (', '.join(f"{SHORT[r['key']]} {r['actual']} vs {r['forecast']} forecast" for r in top[:3]) + '. '
                  if rels else '')
               + (f'AI signals: {wins} wins, {losses} losses, {total_r:+.1f}R.' if sigs else ''))
    body = []
    for r in rels:
        body.append(f'<h2>{_a(r["url"], r["label"] + " · " + r["date"].strftime("%A %d %B"))}</h2>')
        body.append(''.join(f'<p>{escape(p)}</p>' for p in story(r)))
    if sigs:
        body.append('<h2>AI signal results</h2>')
        body.append(f'<p>{len(sigs)} signals closed this week: {wins} hit the target, {losses} hit the stop, '
                    f'{len(sigs) - wins - losses} expired. Net result {total_r:+.1f}R (1R = the amount risked on one trade).</p>')
        best = sorted([s for s in sigs if s.result_r], key=lambda s: -(s.result_r or 0))[:5]
        if best:
            body.append('<ul>' + ''.join(f'<li>{escape(s.direction)} {escape(s.symbol)} {escape(s.timeframe)}: '
                                         f'{s.result_r:+.2f}R</li>' for s in best) + '</ul>')
        body.append(f'<p>Every signal, win or lose, is on the {_a("/results/", "public results page")}.</p>')
    body.append(f'<p>Next week\'s releases: the {_a("/calendar/", "economic calendar")} and the '
                f'{_a("/blog/?kind=outlook", "weekly outlook")}.</p>')
    post, _ = BlogPost.objects.update_or_create(slug=slug, defaults=dict(
        title=title[:160], summary=summary[:300], body='\n'.join(body), kind='recap', auto=True,
        published_at=min(end - timedelta(days=1, hours=12), timezone.now()), is_published=True))
    return post


def weekly(now=None):
    """Outlook from Sunday for the coming week (or Monday–Wednesday for the current one); recap from Saturday."""
    now = now or datetime.now(dt_tz.utc)
    today = now.date()
    made = []
    this_monday = _week_start(today)
    if today.weekday() == 6:
        made.append(write_outlook(this_monday + timedelta(days=7)))
    elif today.weekday() <= 2:
        made.append(write_outlook(this_monday))
    if today.weekday() >= 5:
        made.append(write_recap(this_monday))
    elif today.weekday() == 0:
        made.append(write_recap(this_monday - timedelta(days=7)))
    return [p for p in made if p]


def safe_weekly():
    """Called by the background scanner; tries at most every 3 hours."""
    if not cache.add('blog:weekly:tick', 1, 3 * 3600):
        return []
    try:
        return weekly()
    except Exception as e:
        logger.warning(f'weekly blog post failed: {e}')
        return []


def blog_feed(request):
    """RSS for the blog (feed readers, Google News-style aggregators)."""
    from django.http import HttpResponse
    from django.utils.feedgenerator import Rss201rev2Feed
    from .models import BlogPost
    site = _site()
    feed = Rss201rev2Feed(title='PATREOAIX trading blog', link=f'{site}/blog/', language='en',
                          description='Weekly forex, gold and crypto outlook and recap.')
    for p in BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())[:30]:
        feed.add_item(title=p.title, link=f'{site}{p.get_absolute_url()}', description=p.summary,
                      pubdate=p.published_at, unique_id=f'{site}{p.get_absolute_url()}')
    return HttpResponse(feed.writeString('utf-8'), content_type='application/rss+xml; charset=utf-8')


def sitemap_urls():
    """(path, priority, changefreq, lastmod) for every public page this module serves."""
    from .models import BlogPost, EconEvent
    out = [('/news/', '0.8', 'daily', None), ('/calendar/', '0.8', 'daily', None), ('/blog/', '0.7', 'weekly', None)]
    out += [(f'/news/{slug_of(k)}/', '0.7', 'weekly', None) for k in nh.BIG]
    seen = set()
    for key, when, upd in (EconEvent.objects.filter(key__in=nh.BIG, when__lte=timezone.now(), actual__isnull=False)
                           .order_by('-when').values_list('key', 'when', 'updated_at')[:1500]):
        if (key, when.date()) not in seen:
            seen.add((key, when.date()))
            out.append((f'/news/{slug_of(key)}/{when:%Y-%m-%d}/', '0.6', 'monthly', upd))
    for slug, upd in BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now()).values_list('slug', 'updated_at')[:2000]:
        out.append((f'/blog/{slug}/', '0.6', 'monthly', upd))
    return out
