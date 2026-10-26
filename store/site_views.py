# store/site_views.py
"""
Site-wide JSON endpoints used by the base layout and the dashboard:
  • market overview — 24h change, high/low and a sparkline per symbol
      crypto            → Binance 24h ticker when this server may use it (it refuses US hosts)
      everything else   → the last 24 hourly candles from views.get_candles (Deriv / Kraken / Coinbase /
                          TwelveData …, whatever this server can reach), cached 10 minutes
      gaps              → Yahoo chart meta, then TwelveData /quote — both skipped while rate-limited
  • notifications    — list, mark one read, mark all read
Live bid/ask comes from /api/mt5/quotes/ (same prices the trade server fills at).
"""
import json
import logging
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.timesince import timesince
from django.views.decorators.http import require_GET, require_POST

from . import price_feed
from .models import Notification

logger = logging.getLogger(__name__)

OVERVIEW_SYMBOLS = [
    'XAUUSD', 'XAGUSD', 'USOIL', 'UKOIL',
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY',
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'BNBUSD', 'XRPUSD',
]
OVERVIEW_TTL = 60          # seconds — 24h stats
_UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/120.0 Safari/537.36', 'Accept': 'application/json'}


def _get_json(url, timeout=6):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _downsample(values, n=48):
    values = [v for v in values if v is not None]
    if len(values) <= n:
        return values
    step = len(values) / n
    return [values[int(i * step)] for i in range(n - 1)] + [values[-1]]


def _binance_stats(symbols):
    """24h change / high / low for crypto in one request, sparkline from 1h klines."""
    pairs = {s: price_feed.BINANCE_MAP[s] for s in symbols if s in price_feed.BINANCE_MAP}
    if not pairs or price_feed.cooling('Binance'):
        return {}
    out = {}
    try:
        q = urllib.parse.quote(json.dumps(list(pairs.values()), separators=(',', ':')))
        rows = _get_json(f'https://api.binance.com/api/v3/ticker/24hr?symbols={q}')
        by_pair = {r['symbol']: r for r in rows}
        for sym, pair in pairs.items():
            r = by_pair.get(pair)
            if r:
                out[sym] = {'change_pct': float(r['priceChangePercent']), 'high': float(r['highPrice']),
                            'low': float(r['lowPrice']), 'prev_close': float(r['openPrice']), 'source': 'Binance'}
    except Exception as e:
        price_feed.feed_failed('Binance', e, rate_pause=60)     # 451 on US servers → paused for an hour
        return {}

    def spark(item):
        sym, pair = item
        try:
            kl = _get_json(f'https://api.binance.com/api/v3/klines?symbol={pair}&interval=1h&limit=24')
            return sym, [float(k[4]) for k in kl]
        except Exception:
            return sym, []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for sym, closes in pool.map(spark, pairs.items()):
            if sym in out:
                out[sym]['spark'] = closes
    return out


def _yahoo_stats(symbol):
    ticker = price_feed.YAHOO_MAP.get(symbol)
    if not ticker or price_feed.cooling('Yahoo'):
        return None
    try:
        d = _get_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=15m&range=1d')
        r = d['chart']['result'][0]
        meta = r['meta']
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        last = meta.get('regularMarketPrice')
        closes = r.get('indicators', {}).get('quote', [{}])[0].get('close', []) or []
        if not prev or not last:
            return None
        return {'change_pct': (last - prev) / prev * 100, 'high': meta.get('regularMarketDayHigh'),
                'low': meta.get('regularMarketDayLow'), 'prev_close': prev,
                'spark': _downsample(closes), 'source': 'Yahoo'}
    except Exception as e:
        price_feed.feed_failed('Yahoo', e, rate_pause=300)      # one 429 pauses Yahoo for every caller
        return None


def _twelvedata_stats(symbols):
    if not price_feed.TWELVEDATA_API_KEY or price_feed.cooling('TwelveData'):
        return {}
    td = {s: price_feed.TWELVEDATA_MAP[s] for s in symbols if s in price_feed.TWELVEDATA_MAP}
    if not td:
        return {}
    try:
        q = urllib.parse.urlencode({'symbol': ','.join(td.values()), 'apikey': price_feed.TWELVEDATA_API_KEY})
        data = _get_json(f'https://api.twelvedata.com/quote?{q}', timeout=8)
        if len(td) == 1:
            data = {list(td.values())[0]: data}
        if isinstance(data, dict) and data.get('status') == 'error':
            price_feed.feed_failed('TwelveData', data.get('message', 'error'))
            return {}
        out = {}
        for sym, tds in td.items():
            r = data.get(tds) or {}
            if 'percent_change' in r:
                out[sym] = {'change_pct': float(r['percent_change']), 'high': float(r['high']), 'low': float(r['low']),
                            'prev_close': float(r['previous_close']), 'source': 'TwelveData'}
        return out
    except Exception as e:
        price_feed.feed_failed('TwelveData', e)
        return {}


def _candle_stats(symbol):
    """24h change / high / low / sparkline from the last 24 hourly candles (the same feeds as the charts)."""
    key = f'site:c24:{symbol}'
    rows = cache.get(key)
    if rows is None:
        from .views import get_candles
        try:
            rows, _src = get_candles(symbol, '1h', 26)
        except Exception as e:
            logger.warning(f'overview candles {symbol}: {e}')
            rows = []
        rows = [{'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close'])}
                for c in (rows or [])[-26:]]
        cache.set(key, rows, 600 if rows else 120)       # hourly bars: 10 minutes is fresh enough
    if len(rows) < 6:
        return None
    live = (price_feed.get_cached_prices() or {}).get(symbol)
    last = float(live) if live else rows[-1]['close']
    window = rows[-25:]
    prev = window[0]['close'] if len(window) >= 25 else window[0]['open']    # price 24 hours ago
    if not prev:
        return None
    return {'change_pct': (last - prev) / prev * 100,
            'high': max([r['high'] for r in window[1:]] + [last]), 'low': min([r['low'] for r in window[1:]] + [last]),
            'prev_close': prev, 'spark': [r['close'] for r in window[1:]] + [last], 'source': 'Candles'}


def market_overview(symbols):
    key = 'site:overview:' + ','.join(symbols)
    data = cache.get(key)
    if data is not None:
        return data
    data = _binance_stats(symbols)
    others = [s for s in symbols if s not in data]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for sym, st in zip(others, pool.map(_candle_stats, others)):
            if st:
                data[sym] = st
    missing = [s for s in others if s not in data]
    if missing and not price_feed.cooling('Yahoo'):
        with ThreadPoolExecutor(max_workers=4) as pool:
            for sym, st in zip(missing, pool.map(_yahoo_stats, missing)):
                if st:
                    data[sym] = st
    missing = [s for s in missing if s not in data]
    if missing:
        data.update(_twelvedata_stats(missing))
    cache.set(key, data, OVERVIEW_TTL)
    return data


PUBLIC_TICKER = ['XAUUSD', 'BTCUSD', 'ETHUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'USOIL']


@require_GET
def api_public_ticker(request):
    """
    Live prices for the public landing page (no login; same cached feeds as the dashboard).
    ?candles=XAUUSD also returns the last 90 one-hour candles of that symbol for the hero chart.
    """
    sym_c = (request.GET.get('candles') or '').upper()
    if sym_c:
        if sym_c not in PUBLIC_TICKER:
            return JsonResponse({'success': False, 'error': 'Unknown symbol'}, status=400)
        key = f'public:candles:{sym_c}'
        candles = cache.get(key)
        if candles is None:
            from .views import get_candles
            try:
                rows, _src = get_candles(sym_c, '1h', 120)
            except Exception as e:
                logger.warning(f'public candles {sym_c}: {e}')
                rows = []
            candles = [{'time': int(c['time']), 'open': float(c['open']), 'high': float(c['high']),
                        'low': float(c['low']), 'close': float(c['close'])} for c in (rows or [])[-90:]]
            cache.set(key, candles, 300 if candles else 30)
        return JsonResponse({'success': bool(candles), 'symbol': sym_c, 'candles': candles})

    overview = market_overview(PUBLIC_TICKER)
    cached = price_feed.get_cached_prices() or {}
    quotes = []
    for sym in PUBLIC_TICKER:
        st = overview.get(sym) or {}
        price = cached.get(sym) or (st.get('spark') or [None])[-1]
        if not price:
            continue
        quotes.append({'symbol': sym, 'price': float(price), 'change_pct': st.get('change_pct')})
    return JsonResponse({'success': True, 'quotes': quotes})


@login_required
@require_GET
def api_market_overview(request):
    symbols = [s for s in (request.GET.get('symbols') or '').upper().split(',') if s in OVERVIEW_SYMBOLS] or OVERVIEW_SYMBOLS
    return JsonResponse({'success': True, 'symbols': market_overview(symbols)})


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS (bell in the header)
# ═══════════════════════════════════════════════════════════════
@login_required
@require_GET
def api_notifications(request):
    qs = Notification.objects.filter(user=request.user, is_archived=False)
    items = [{
        'id': n.id, 'title': n.title, 'message': n.message, 'category': n.category,
        'priority': n.priority, 'icon': n.icon, 'color': n.color, 'is_read': n.is_read,
        'action_url': n.action_url or '', 'time_ago': timesince(n.created_at).split(',')[0] + ' ago',
        'created_at': n.created_at.isoformat(),
    } for n in qs[:20]]
    unread = qs.filter(is_read=False).count()
    return JsonResponse({'success': True, 'unread_count': unread, 'count': unread, 'notifications': items})


@login_required
@require_POST
def api_notification_read(request, notification_id):
    Notification.objects.filter(user=request.user, id=notification_id).update(is_read=True)
    return JsonResponse({'success': True})


@login_required
@require_POST
def api_notifications_read_all(request):
    n = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'success': True, 'updated': n})


# ═══════════════════════════════════════════════════════════════
# AI CHART SCANNER (market overview page)
# ═══════════════════════════════════════════════════════════════
@login_required
@require_GET
def api_ai_scan(request):
    from . import chart_scanner
    style = request.GET.get('style', 'intraday')
    symbols = [s for s in (request.GET.get('symbols') or '').upper().split(',') if s]
    results = chart_scanner.scan(symbols, style)
    return JsonResponse({'success': True, 'style': style, 'timeframes': chart_scanner.STYLES.get(style, chart_scanner.STYLES['intraday']),
                         'results': results})


@login_required
@require_GET
def api_ai_scan_detail(request, symbol):
    from . import chart_scanner
    symbol = symbol.upper()
    if symbol not in chart_scanner.SCAN_SYMBOLS:
        return JsonResponse({'success': False, 'error': 'Unknown symbol'}, status=404)
    res = chart_scanner.analyse(symbol, request.GET.get('style', 'intraday'), detail=True)
    return JsonResponse({'success': bool(res.get('ok')), 'result': res, 'error': res.get('error')})


# ═══════════════════════════════════════════════════════════════
# AI CONFLUENCE SIGNALS (market overview: every scanner + the model)
# ═══════════════════════════════════════════════════════════════
@login_required
@require_GET
def api_signal_confluence(request):
    from . import chart_scanner, signal_alerts, signal_engine
    style = request.GET.get('style', 'intraday')
    style = style if style in chart_scanner.STYLES else 'intraday'
    symbol = (request.GET.get('symbol') or '').upper()
    results, strength, when = signal_alerts.confluence(style, [symbol] if symbol else None, fresh=request.GET.get('fresh') == '1')
    from django.db import DatabaseError
    from .models import ScannerSignal
    try:
        live = {(x.symbol, x.direction): x.id for x in ScannerSignal.objects.filter(style=style, outcome='OPEN')}
    except DatabaseError:            # signals table not migrated yet: the readings still work
        live = {}
    results = [dict(r, recorded=live.get((r['symbol'], r['direction']))) for r in results]
    return JsonResponse({'success': True, 'style': style, 'results': results, 'strength': strength,
                         'scanners': signal_engine.scanner_meta(), 'updated_at': when.isoformat(),
                         'worker': signal_alerts.worker_status()})


@login_required
def api_signal_alerts(request):
    from .models import SignalAlertSubscription
    from . import chart_scanner
    sub = SignalAlertSubscription.objects.filter(user=request.user).first()
    if request.method == 'POST':
        try:
            data = json.loads(request.body or '{}')
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Bad JSON'}, status=400)
        sub = sub or SignalAlertSubscription(user=request.user)
        if 'enabled' in data:
            sub.enabled = bool(data['enabled'])
        if data.get('style') in chart_scanner.STYLES:
            sub.style = data['style']
        if data.get('min_grade') in ('A', 'A+'):
            sub.min_grade = data['min_grade']
        if isinstance(data.get('symbols'), list):
            sub.symbols = [s for s in (str(x).upper() for x in data['symbols']) if s in chart_scanner.SCAN_SYMBOLS]
        sub.save()
    elif request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    return JsonResponse({'success': True, 'alerts': {
        'enabled': bool(sub and sub.enabled), 'style': sub.style if sub else 'intraday',
        'min_grade': sub.min_grade if sub else 'A', 'symbols': sub.symbols if sub else [],
    }})


@login_required
@require_GET
def api_signal_history(request):
    from . import signal_alerts
    style = request.GET.get('style') or None
    rows, stats = signal_alerts.history(style)
    return JsonResponse({'success': True, 'signals': rows, 'stats': stats, 'worker': signal_alerts.worker_status()})
