# store/public_results.py
"""
The public track record (/results/, no login).

Every signal the scanner records is counted, wins and losses alike; open signals are listed without their levels
(those are for members).
"""
from collections import OrderedDict
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


def _host():
    return getattr(settings, 'PUBLIC_HOST', '') or 'www.patreoaix.com'


def _site():
    return (getattr(settings, 'SITE_URL', '') or f'https://{_host()}').rstrip('/')


def stats(qs):
    closed = [s for s in qs if s.outcome in ('TP', 'SL', 'EXPIRED')]
    wins = sum(1 for s in closed if s.outcome == 'TP')
    losses = sum(1 for s in closed if s.outcome == 'SL')
    decided = wins + losses
    total_r = round(sum(s.result_r or 0 for s in closed), 2)
    return {'signals': len(qs), 'closed': len(closed), 'open': sum(1 for s in qs if s.outcome == 'OPEN'),
            'wins': wins, 'losses': losses, 'expired': len(closed) - decided,
            'win_rate': round(wins / decided * 100) if decided else None,
            'total_r': total_r, 'avg_r': round(total_r / len(closed), 2) if closed else None}


def build(style=None, days=None):
    """Everything the results page shows (cached 5 minutes)."""
    key = f'public:results:{style or "all"}:{days or "all"}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    from .models import ScannerSignal
    qs = ScannerSignal.objects.all()
    if style:
        qs = qs.filter(style=style)
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    sigs = list(qs.order_by('created_at'))
    # cumulative R over time (the "equity curve" of following every signal with the same risk)
    curve, run = [], 0.0
    for s in sorted((s for s in sigs if s.closed_at and s.outcome != 'OPEN'), key=lambda s: s.closed_at):
        run += s.result_r or 0
        curve.append({'t': int(s.closed_at.timestamp()), 'r': round(run, 2)})
    months = OrderedDict()
    for s in sigs:
        m = s.created_at.strftime('%Y-%m')
        months.setdefault(m, []).append(s)
    monthly = [dict(stats(v), month=timezone.datetime.strptime(k, '%Y-%m').strftime('%B %Y')) for k, v in reversed(months.items())]
    by_market = {}
    for s in sigs:
        by_market.setdefault(s.symbol, []).append(s)
    markets = sorted(({'symbol': k, **stats(v)} for k, v in by_market.items()), key=lambda m: -m['closed'])[:12]
    recent = sorted(sigs, key=lambda s: s.created_at, reverse=True)[:50]
    out = {'all': stats(sigs), 'last30': stats([s for s in sigs if s.created_at >= timezone.now() - timedelta(days=30)]),
           'curve': curve, 'monthly': monthly, 'markets': markets, 'recent': recent,
           'first': sigs[0].created_at if sigs else None}
    cache.set(key, out, 300)
    return out


def results_png(days=30):
    """The results card image (link preview when the page is shared), cached 30 minutes."""
    key = f'public:results:png:{days}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    from .models import ScannerSignal
    from .telegram_schedule import results_card
    since = timezone.now() - timedelta(days=days)
    closed = list(ScannerSignal.objects.filter(closed_at__gte=since).exclude(outcome='OPEN').order_by('closed_at'))
    opened = ScannerSignal.objects.filter(created_at__gte=since).count()
    still = ScannerSignal.objects.filter(outcome='OPEN').count()
    title = "TODAY'S RESULTS" if days == 1 else f'LAST {days} DAYS'
    sub = timezone.now().strftime('%d %b %Y')
    png = results_card(title, sub, closed, opened, still)[0]
    cache.set(key, png, 1800)
    return png
