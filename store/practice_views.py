# store/practice_views.py
"""Academy Practice simulator pages (logic in store/practice.py). Open to everyone; signed-in students keep
their score and earn reward points, visitors keep it for the browser session."""
import json
import logging

from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import practice as pr

logger = logging.getLogger(__name__)
SESSION_KEY = 'practice_rows'
DAILY_POINTS = 15          # practice trades a day that earn points
DAILY_WIN_POINTS = 10


def _anon_rows(request):
    return [tuple(x) for x in request.session.get(SESSION_KEY, [])]


def _stats(request):
    if request.user.is_authenticated:
        return pr.user_stats(request.user)
    return pr.summarize(_anon_rows(request))


def practice_page(request):
    from .models import PracticeTrade
    recent = (PracticeTrade.objects.filter(user=request.user)[:8] if request.user.is_authenticated else [])
    stats = _stats(request)
    return render(request, 'store/academy_practice.html', {
        'stats': stats, 'stats_json': json.dumps(stats), 'recent': recent, 'setups': [(k, v[0], v[1]) for k, v in pr.SETUPS.items()],
        'has_real': bool(pr._real_keys())})


def api_next(request):
    prefer = request.GET.get('src') if request.GET.get('src') in ('real', 'sim') else None
    try:
        return JsonResponse(pr.new_round(prefer=prefer))
    except Exception as e:
        logger.exception(f'practice next: {e}')
        return JsonResponse({'error': 'Could not build a chart. Try again.'}, status=500)


@require_POST
def api_answer(request):
    try:
        res = pr.answer(request.POST.get('token'), request.POST.get('choice'), request.POST.get('rr'))
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    row = (res['choice'], res['r'], res['correct'], res['setup'])
    points = 0
    if request.user.is_authenticated:
        from .models import PracticeTrade
        try:
            PracticeTrade.objects.create(user=request.user, digest=res['digest'], source=res['source'], setup=res['setup'][:30],
                                         symbol=res['symbol'], timeframe=res['timeframe'][:5], choice=res['choice'],
                                         rr=res['rr'], outcome=res['outcome'], result_r=res['r'], correct=res['correct'])
            points = _award(request.user, res)
        except IntegrityError:
            res['repeat'] = True                                     # the same chart answered twice: not counted
    else:
        rows = request.session.get(SESSION_KEY, [])
        digests = request.session.get(SESSION_KEY + '_d', [])
        if res['digest'] not in digests:
            rows.append(list(row))
            digests.append(res['digest'])
            request.session[SESSION_KEY] = rows[-300:]
            request.session[SESSION_KEY + '_d'] = digests[-300:]
    res.pop('digest', None)
    res['points'] = points
    res['stats'] = _stats(request)
    return JsonResponse(res)


def _award(user, res):
    from .models import PracticeTrade
    from . import rewards
    today = timezone.localdate()
    n = PracticeTrade.objects.filter(user=user, created_at__date=today).count()
    pts = 0
    if n <= DAILY_POINTS:
        pts += rewards.safe_award(user, 'practice', f'{today}:{n}')
    if res['correct']:
        w = PracticeTrade.objects.filter(user=user, created_at__date=today, correct=True).count()
        if w <= DAILY_WIN_POINTS:
            pts += rewards.safe_award(user, 'practice_win', f'{today}:{w}')
    return pts
