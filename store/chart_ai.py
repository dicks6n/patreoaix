# store/chart_ai.py
"""
"What does this chart mean?" — upload a chart screenshot, get a structured read:

  1. The image is checked and re-encoded with Pillow (only real images, max 8 MB, resized to 1600 px, metadata dropped).
     Nothing is stored.
  2. Gemini (GEMINI_API_KEY, model CHART_AI_MODEL) reads it as a trading coach and answers in JSON: trend, market
     structure, candlestick / chart patterns, levels, bias with confidence, bull and bear scenarios, a strategy with
     entry / stop / target, mistakes to avoid, and which academy lessons explain what it saw.
  3. When the chart is a market the PATREOAIX scanner follows (gold, forex, indices, crypto), the live Market Overview
     verdict for that market is added, from real price history: the "data" view next to the "picture" view.

Daily limits: CHART_AI_FREE_DAILY (3) for Starter, CHART_AI_MEMBER_DAILY (30) for Pro / VIP, staff unlimited.
"""
import io
import json
import logging
import os
import re

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1600


class ChartAIError(Exception):
    pass


def _cfg(name, default):
    return getattr(settings, name, None) or os.getenv(name) or default


def enabled():
    return bool(_cfg('GEMINI_API_KEY', ''))


def daily_limit(user):
    if user.is_staff:
        return None
    from .membership import is_member
    return int(_cfg('CHART_AI_MEMBER_DAILY', 30) if is_member(user) else _cfg('CHART_AI_FREE_DAILY', 3))


def _count_key(user):
    return f'chartai:n:{user.id}:{timezone.localdate():%Y%m%d}'


def used_today(user):
    return cache.get(_count_key(user)) or 0


def left_today(user):
    lim = daily_limit(user)
    return None if lim is None else max(0, lim - used_today(user))


def _spend(user):
    k = _count_key(user)
    cache.add(k, 0, 2 * 86400)
    try:
        cache.incr(k)
    except ValueError:
        cache.set(k, 1, 2 * 86400)


def clean_image(f):
    """Uploaded file → JPEG bytes. Raises ChartAIError for anything that is not a real image."""
    if not f:
        raise ChartAIError('Choose a chart screenshot first.')
    if getattr(f, 'size', 0) > MAX_BYTES:
        raise ChartAIError('The image is larger than 8 MB. Take a smaller screenshot.')
    from PIL import Image, UnidentifiedImageError
    raw = f.read()
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ChartAIError('That file is not an image. Upload a PNG or JPG screenshot of a chart.')
    if img.width < 200 or img.height < 150:
        raise ChartAIError('The image is too small to read. Take a bigger screenshot of the chart.')
    img = img.convert('RGB')
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=88)                  # re-encoded: no metadata, no hidden payloads
    return out.getvalue()


def _lesson_keys():
    from .academy_library import TOPICS
    return [f'{c}/{t}' for c, t in TOPICS]


PROMPT = """You are the PATREOAIX trading coach. A trader uploaded a screenshot of a price chart and asks what it means.
Read the chart carefully (candles, wicks, trend, swings, drawn lines, indicators, the symbol and timeframe if visible).
Teach as you explain: simple, clear English for a beginner, specific to THIS chart (refer to where things are: "the last
three candles", "the left side", "around 2,350"). Never promise profits; talk in probabilities.

The trader's note (may be empty): {note}

Answer ONLY with JSON in exactly this shape:
{{
 "is_chart": true or false (false if the image is not a price chart),
 "symbol": "the market if visible, e.g. XAUUSD, EURUSD, BTCUSD, US30, else empty",
 "timeframe": "e.g. 1h, 4h, 1d, 15m if visible, else empty",
 "summary": "2-3 sentences: what this chart is showing right now",
 "trend": {{"direction": "up|down|sideways", "strength": "strong|moderate|weak", "explanation": "..."}},
 "structure": "market structure in plain words: higher highs/lows, break of structure, change of character, ranges",
 "patterns": [{{"name": "e.g. Bullish engulfing", "type": "candlestick|chart|smc|indicator", "where": "where on the chart",
               "meaning": "what it usually means here", "learn": "one lesson key from the list, or empty"}}],
 "levels": [{{"price": "the price or zone", "kind": "support|resistance|demand|supply|liquidity|trendline", "note": "..."}}],
 "indicators": "what any visible indicators say, or empty",
 "bias": "bullish|bearish|neutral",
 "confidence": integer 0-100 (how clear the picture is; rarely above 75),
 "scenarios": [{{"name": "Bullish", "probability": integer, "trigger": "what must happen", "target": "...", "invalidation": "..."}},
               {{"name": "Bearish", "probability": integer, "trigger": "...", "target": "...", "invalidation": "..."}}],
 "strategy": {{"setup": "name of the strategy that fits", "entry": "where/when to enter", "stop_loss": "where and why",
              "take_profit": "where and why", "risk_reward": "e.g. 1:2", "explanation": "why this plan, step by step"}},
 "wait_for": "what a disciplined trader waits for before entering",
 "mistakes_to_avoid": ["...", "..."],
 "learn_more": ["up to 4 lesson keys from the list"]
}}
Scenario probabilities must add up to 100. Lesson keys must come from this list: {lessons}
"""


def _parse(text):
    text = (text or '').strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise ChartAIError('The AI answer could not be read. Try again.')
    try:
        return json.loads(m.group(0))
    except ValueError:
        raise ChartAIError('The AI answer could not be read. Try again.')


def generate(contents, config=None):
    """Gemini generate_content with the model fallback (any feature can use it). Returns the response."""
    from google import genai
    client = genai.Client(api_key=_cfg('GEMINI_API_KEY', ''))
    last = None
    for model in [m for m in dict.fromkeys([_cfg('CHART_AI_MODEL', ''), *FALLBACK_MODELS]) if m]:
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:
            last = e
            if not any(code in str(e) for code in ('NOT_FOUND', '404', 'UNAVAILABLE', '503', 'RESOURCE_EXHAUSTED', '429')):
                raise
            logger.info(f'Gemini model {model} unavailable or busy ({str(e)[:60]}), trying the next one')
    raise last


def _ask_gemini(jpeg, note):
    from google.genai import types
    prompt = PROMPT.format(note=(note or '').strip()[:300] or '(none)', lessons=', '.join(_lesson_keys()))
    resp = generate([types.Part.from_bytes(data=jpeg, mime_type='image/jpeg'), prompt],
                    types.GenerateContentConfig(response_mime_type='application/json', temperature=0.3))
    return _parse(resp.text)


FALLBACK_MODELS = ['gemini-flash-latest', 'gemini-3.8-flash', 'gemini-3.5-flash', 'gemini-2.5-flash']


SYMBOL_ALIASES = {'GOLD': 'XAUUSD', 'XAU/USD': 'XAUUSD', 'SILVER': 'XAGUSD', 'BITCOIN': 'BTCUSD', 'BTC/USD': 'BTCUSD',
                  'BTCUSDT': 'BTCUSD', 'ETHUSDT': 'ETHUSD', 'ETHEREUM': 'ETHUSD', 'DJI': 'US30', 'DOW': 'US30', 'US30USD': 'US30',
                  'NASDAQ': 'NAS100', 'NDX': 'NAS100', 'USTEC': 'NAS100', 'WTI': 'USOIL', 'CRUDE': 'USOIL', 'BRENT': 'UKOIL'}


def normalize_symbol(s):
    s = re.sub(r'[^A-Z0-9/]', '', str(s or '').upper())
    s = SYMBOL_ALIASES.get(s, s).replace('/', '')
    return SYMBOL_ALIASES.get(s, s)


def live_view(symbol):
    """The Market Overview verdict for this market, from real price history, or None."""
    from . import chart_scanner
    if symbol not in chart_scanner.SCAN_SYMBOLS:
        return None
    try:
        from . import coach_card
        v = coach_card.build_view(symbol, 'intraday')
    except Exception as e:
        logger.info(f'chart AI live view {symbol}: {e}')
        return None
    if not v:
        return None
    vd = v.get('verdict') or {}
    return {'symbol': symbol, 'tf': (v.get('tf') or '').upper(), 'price': v.get('price'), 'action': vd.get('action'),
            'side': vd.get('side'), 'conviction': vd.get('conviction'), 'headline': vd.get('headline'),
            'agree': len(vd.get('agree') or []), 'voting': vd.get('voting'), 'news': v.get('news')}


def _clean_result(r):
    from .academy_library import TOPICS
    ok = {f'{c}/{t}' for c, t in TOPICS}
    r = r if isinstance(r, dict) else {}
    r['bias'] = r.get('bias') if r.get('bias') in ('bullish', 'bearish', 'neutral') else 'neutral'
    try:
        r['confidence'] = max(0, min(100, int(r.get('confidence') or 0)))
    except (TypeError, ValueError):
        r['confidence'] = 0
    r['patterns'] = [p for p in (r.get('patterns') or []) if isinstance(p, dict)][:8]
    for p in r['patterns']:
        p['learn'] = p.get('learn') if p.get('learn') in ok else ''
    r['levels'] = [x for x in (r.get('levels') or []) if isinstance(x, dict)][:8]
    r['scenarios'] = [x for x in (r.get('scenarios') or []) if isinstance(x, dict)][:3]
    r['mistakes_to_avoid'] = [str(x) for x in (r.get('mistakes_to_avoid') or [])][:5]
    learn = [k for k in (r.get('learn_more') or []) if k in ok]
    learn += [p['learn'] for p in r['patterns'] if p['learn'] and p['learn'] not in learn]
    r['learn'] = [{'key': k, 'course': k.split('/')[0], 'topic': k.split('/')[1], 'title': TOPICS[tuple(k.split('/'))]['title']}
                  for k in learn[:6]]
    r['trend'] = r.get('trend') if isinstance(r.get('trend'), dict) else {}
    r['strategy'] = r.get('strategy') if isinstance(r.get('strategy'), dict) else {}
    return r


def analyze(user, upload, note=''):
    """→ result dict for the page. Raises ChartAIError with a message for the user."""
    if not enabled():
        raise ChartAIError('Chart reading is not switched on yet (the site needs its GEMINI_API_KEY).')
    left = left_today(user)
    if left is not None and left <= 0:
        raise ChartAIError('You have used today\'s chart readings. Pro and VIP members get '
                           f'{_cfg("CHART_AI_MEMBER_DAILY", 30)} a day: upgrade at /subscription/.')
    jpeg = clean_image(upload)
    try:
        raw = _ask_gemini(jpeg, note)
    except ChartAIError:
        raise
    except Exception as e:
        logger.warning(f'chart AI: {e}')
        raise ChartAIError('The AI is busy right now. Try again in a minute.')
    _spend(user)
    r = _clean_result(raw)
    if r.get('is_chart') is False:
        r['not_chart'] = True
        return r
    r['symbol_norm'] = normalize_symbol(r.get('symbol'))
    r['live'] = live_view(r['symbol_norm']) if r['symbol_norm'] else None
    return r
