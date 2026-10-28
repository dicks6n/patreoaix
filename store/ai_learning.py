# store/ai_learning.py
"""
Daily learning from the scanner's own results.

Every closed signal (target or stop hit) is a lesson: which scanners agreed with it, and was it right? Once a day
the scanner compares, for each of its strategies, how often signals it agreed with reached the target, against the
overall hit rate. Strategies that were right more often get more say in the next grades, ones that were wrong get
less. The change is shrunk toward "no change" while there are few examples and kept within 0.6×–1.5×, so a short
lucky or unlucky run can't take over. (The machine-learning model inside the scanner already retrains on fresh
candles for every market; this adds learning from real outcomes.)

    python manage.py ai_learn            # learn now and print what changed
"""
import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

KEY = 'scanner_weights'
MIN_SIGNALS = 20          # below this the scanner keeps its default weights
DAYS = 120
LO, HI = 0.6, 1.5
CACHE = 'ai:learned:weights'


def learn(days=DAYS):
    """→ {'multipliers': {key: x}, 'base': hit rate, 'n': signals, 'detail': {...}} and stores it."""
    from datetime import timedelta
    from .models import AILearning, ScannerSignal
    from .signal_engine import SCANNERS
    since = timezone.now() - timedelta(days=days)
    closed = list(ScannerSignal.objects.filter(outcome__in=('TP', 'SL'), closed_at__gte=since).values('agree', 'outcome', 'style'))
    n = len(closed)
    result = {'n': n, 'days': days, 'base': None, 'multipliers': {}, 'detail': {}, 'learned_at': timezone.now().isoformat()}
    if n:
        wins = sum(1 for s in closed if s['outcome'] == 'TP')
        base = wins / n
        result['base'] = round(base, 3)
        for key, label, weight, _ in SCANNERS:
            if not weight:
                continue
            sub = [s for s in closed if key in (s['agree'] or [])]
            k = len(sub)
            if not k:
                continue
            rate = sum(1 for s in sub if s['outcome'] == 'TP') / k
            shrink = k / (k + 15)                                    # few examples → small change
            mult = 1 + 1.5 * (rate - base) * shrink if n >= MIN_SIGNALS else 1.0
            mult = round(min(HI, max(LO, mult)), 3)
            result['multipliers'][key] = mult
            result['detail'][key] = {'label': label, 'signals': k, 'hit_rate': round(rate, 3), 'multiplier': mult}
    AILearning.objects.update_or_create(key=KEY, defaults={'data': result})
    cache.delete(CACHE)
    return result


def multipliers():
    """{scanner key: weight multiplier}; empty until enough signals have closed."""
    hit = cache.get(CACHE)
    if hit is not None:
        return hit
    from .models import AILearning
    try:
        row = AILearning.objects.filter(key=KEY).first()
        data = row.data if row else {}
    except Exception:
        data = {}
    mult = data.get('multipliers') or {} if (data.get('n') or 0) >= MIN_SIGNALS else {}
    cache.set(CACHE, mult, 600)
    return mult


def safe_daily():
    """Called by the background scanner: learns once a day."""
    if not cache.add(f"ai:learn:{timezone.now():%Y%m%d}", 1, 2 * 86400):
        return None
    try:
        res = learn()
        logger.info(f"AI learning: {res['n']} closed signals, base hit rate {res['base']}, multipliers {res['multipliers']}")
        return res
    except Exception as e:
        logger.warning(f'AI learning failed: {e}')
        return None
