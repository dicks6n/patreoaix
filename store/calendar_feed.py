# store/calendar_feed.py
"""
Economic calendar events, tried in order:
  1. TradingView  economic-calendar.tradingview.com/events   (actual / forecast / previous)
  2. ForexFactory nfs.faireconomy.media weekly JSON           (forecast / previous, no actual)
  3. The last good result (kept 24 h), marked stale
Results are cached for 15 minutes so page views don't hit the feeds.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_tz

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = 'calendar:events:v2'
LAST_GOOD_KEY = 'calendar:last_good:v2'
CACHE_TTL = 15 * 60
LAST_GOOD_TTL = 24 * 3600
COUNTRIES = 'US,EU,GB,JP,DE,FR,IT,AU,NZ,CA,CH,CN'

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/124.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}
CURRENCY_COUNTRY = {'USD': 'US', 'EUR': 'EU', 'GBP': 'GB', 'JPY': 'JP', 'AUD': 'AU', 'NZD': 'NZ',
                    'CAD': 'CA', 'CHF': 'CH', 'CNY': 'CN'}
COUNTRY_CURRENCY = {'US': 'USD', 'EU': 'EUR', 'DE': 'EUR', 'FR': 'EUR', 'IT': 'EUR', 'ES': 'EUR', 'GB': 'GBP',
                    'UK': 'GBP', 'JP': 'JPY', 'AU': 'AUD', 'NZ': 'NZD', 'CA': 'CAD', 'CH': 'CHF', 'CN': 'CNY'}


def flag(country):
    code = (country or '').upper()
    if code == 'UK':
        code = 'GB'
    if len(code) != 2 or not code.isalpha():
        return '🏳️'
    return ''.join(chr(0x1F1E6 + ord(ch) - ord('A')) for ch in code)


def _parse_date(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


# ── 1. TradingView ──────────────────────────────────────────────
def fetch_tradingview(days_ahead=7):
    now = datetime.now(dt_tz.utc)
    params = {
        'from': (now - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00.000Z'),
        'to': (now + timedelta(days=days_ahead)).strftime('%Y-%m-%dT23:59:59.000Z'),
        'countries': COUNTRIES,
    }
    headers = dict(BROWSER_HEADERS, Origin='https://www.tradingview.com', Referer='https://www.tradingview.com/')
    r = requests.get('https://economic-calendar.tradingview.com/events', params=params, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    rows = data.get('result') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"unexpected TradingView payload: {str(data)[:120]}")
    events = []
    for item in rows:
        date = _parse_date(item.get('date'))
        if not date:
            continue
        imp = item.get('importance')
        # TradingView: -1 low, 0 medium, 1 high
        impact = {-1: 'low', 0: 'medium', 1: 'high'}.get(imp, 'low')
        country = (item.get('country') or '').upper()
        events.append({
            'id': f"tv-{item.get('id') or len(events)}",
            'date': date,
            'country': country,
            'currency': item.get('currency') or COUNTRY_CURRENCY.get(country, ''),
            'flag': flag(country),
            'event': item.get('title') or item.get('indicator') or 'Event',
            'impact': impact,
            'actual': _clean(item.get('actual')),
            'forecast': _clean(item.get('forecast')),
            'previous': _clean(item.get('previous')),
            'unit': item.get('unit') or '',
            'description': item.get('comment') or '',
        })
    return events


# ── 2. ForexFactory weekly JSON ─────────────────────────────────
def fetch_forexfactory():
    events = []
    for name in ('thisweek', 'nextweek'):
        try:
            r = requests.get(f'https://nfs.faireconomy.media/ff_calendar_{name}.json', headers=BROWSER_HEADERS, timeout=10)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            if name == 'thisweek':
                raise
            logger.info(f"ForexFactory {name} unavailable: {e}")
            continue
        for i, item in enumerate(rows if isinstance(rows, list) else []):
            date = _parse_date(item.get('date'))
            if not date:
                continue
            cur = (item.get('country') or '').upper()      # ForexFactory puts the currency here
            impact = (item.get('impact') or '').lower()
            country = CURRENCY_COUNTRY.get(cur, cur[:2])
            events.append({
                'id': f"ff-{name}-{i}",
                'date': date,
                'country': country,
                'currency': cur,
                'flag': flag(country),
                'event': item.get('title') or 'Event',
                'impact': impact if impact in ('high', 'medium', 'low') else 'low',
                'actual': _clean(item.get('actual')),
                'forecast': _clean(item.get('forecast')),
                'previous': _clean(item.get('previous')),
                'unit': '',
                'description': 'Bank holiday — expect thin liquidity.' if impact == 'holiday' else '',
            })
    return events


SOURCES = (('TradingView', fetch_tradingview), ('ForexFactory', fetch_forexfactory))


def get_calendar(days_ahead=7):
    """→ {'events': [...], 'source': str|None, 'stale': bool, 'updated': datetime|None, 'errors': [...]}"""
    hit = cache.get(CACHE_KEY)
    if hit is not None:
        return hit
    errors = []
    now = datetime.now(dt_tz.utc)
    horizon = now + timedelta(days=days_ahead + 1)
    for name, fn in SOURCES:
        try:
            events = [e for e in fn() if e['date'] <= horizon]
        except Exception as e:
            logger.warning(f"Economic calendar: {name} failed: {e}")
            errors.append(f"{name}: {e}")
            continue
        if events:
            events.sort(key=lambda e: e['date'])
            res = {'events': events, 'source': name, 'stale': False, 'updated': now, 'errors': errors}
            cache.set(CACHE_KEY, res, CACHE_TTL)
            cache.set(LAST_GOOD_KEY, res, LAST_GOOD_TTL)
            return res
        errors.append(f"{name}: no events returned")
    last = cache.get(LAST_GOOD_KEY)
    if last:
        res = dict(last, stale=True, errors=errors)
    else:
        res = {'events': [], 'source': None, 'stale': False, 'updated': None, 'errors': errors}
    cache.set(CACHE_KEY, res, 120)          # retry the feeds in 2 minutes
    return res
