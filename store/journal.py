# store/journal.py
"""
Trading journal + the weekly AI coach review.

  • Closed trades from the PATREOAIX terminal and from AI Copy Trading are added to the journal automatically;
    trades on other brokers are added by hand. Each entry can carry the setup, the emotion, whether the plan was
    followed and a note.
  • Every Monday the worker (signal_scanner → safe_weekly) reviews last week for each trader who traded: results,
    what worked, the habits that cost money (revenge trading, no stop, oversizing, holding losers, bad sessions,
    emotional trades), and one focus for the coming week. The trader gets a notification (and a push).
"""
import logging
import statistics
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SESSIONS = [(0, 7, 'Asian session'), (7, 12, 'London morning'), (12, 16, 'London–New York overlap'),
            (16, 21, 'New York afternoon'), (21, 24, 'late evening')]          # UTC hours


def _tz():
    from .telegram_schedule import _tz as tz
    return tz()


def week_start(day=None):
    """Monday (local date) of the week containing `day`."""
    day = day or timezone.localtime(timezone.now(), _tz()).date()
    return day - timedelta(days=day.weekday())


def _bounds(start):
    tz = _tz()
    a = datetime.combine(start, dtime.min).replace(tzinfo=tz)
    return a, a + timedelta(days=7)


# ═══════════════════════════════════════════════════════════════
# ENTRIES
# ═══════════════════════════════════════════════════════════════
def sync(user):
    """Add the user's closed terminal trades and copy trades that are not in the journal yet. Returns how many."""
    from .models import Trade, CopyTrade, JournalEntry
    n = 0
    done = set(JournalEntry.objects.filter(user=user, trade__isnull=False).values_list('trade_id', flat=True))
    for t in Trade.objects.filter(user=user, status__in=('WON', 'LOST'), date_closed__isnull=False) \
            .exclude(pk__in=done).select_related('asset').order_by('date_closed')[:500]:
        if t.target_digit is not None:                      # Deriv digit contracts are not journal trades
            continue
        JournalEntry.objects.create(
            user=user, source='site', trade=t, symbol=(t.asset.symbol if t.asset else '?')[:20], direction=t.direction[:4],
            lots=float(t.stake or 0) or None, entry_price=float(t.entry_price) if t.entry_price else None,
            exit_price=float(t.exit_price) if t.exit_price else None, had_stop=bool(t.stop_loss),
            pnl=round(float(t.pnl or 0), 2), close_reason=t.close_reason or '', opened_at=t.date_opened, closed_at=t.date_closed)
        n += 1
    done = set(JournalEntry.objects.filter(user=user, copy_trade__isnull=False).values_list('copy_trade_id', flat=True))
    for c in CopyTrade.objects.filter(user=user, status='CLOSED', closed_at__isnull=False).exclude(pk__in=done)[:500]:
        JournalEntry.objects.create(
            user=user, source='copy', copy_trade=c, symbol=c.symbol, direction=c.direction, had_stop=True,
            pnl=round(float(c.profit or 0), 2), opened_at=c.created_at, closed_at=c.closed_at, setup='ai_signal',
            followed_plan=True)
        n += 1
    return n


def add_manual(user, data):
    from .models import JournalEntry
    symbol = (data.get('symbol') or '').strip().upper()[:20]
    direction = 'SELL' if str(data.get('direction', '')).upper() == 'SELL' else 'BUY'
    if not symbol:
        raise ValueError('Enter the market, e.g. XAUUSD.')
    try:
        pnl = float(str(data.get('pnl') or '').replace(',', ''))
    except ValueError:
        raise ValueError('Enter the profit or loss in USD (use - for a loss).')
    def num(k):
        try:
            return float(str(data.get(k)).replace(',', '')) if data.get(k) not in (None, '') else None
        except ValueError:
            return None
    closed = timezone.now()
    if data.get('date'):
        try:
            closed = datetime.strptime(data['date'], '%Y-%m-%dT%H:%M').replace(tzinfo=_tz())
        except ValueError:
            pass
    return JournalEntry.objects.create(
        user=user, source='manual', symbol=symbol, direction=direction, lots=num('lots'), entry_price=num('entry'),
        exit_price=num('exit'), had_stop=data.get('had_stop') in ('on', 'yes', True, '1'), pnl=round(pnl, 2), closed_at=closed,
        **_tags(data))


def _tags(data):
    from .models import JournalEntry
    out = {}
    if 'setup' in data:
        out['setup'] = data['setup'] if data['setup'] in dict(JournalEntry.SETUPS) else ''
    if 'emotion' in data:
        out['emotion'] = data['emotion'] if data['emotion'] in dict(JournalEntry.EMOTIONS) else ''
    if 'followed_plan' in data:
        out['followed_plan'] = {'yes': True, 'no': False}.get(data['followed_plan'])
    if 'note' in data:
        out['note'] = (data['note'] or '')[:2000]
    return out


def tag(user, entry_id, data):
    from .models import JournalEntry
    e = JournalEntry.objects.get(user=user, pk=entry_id)
    for k, v in _tags(data).items():
        setattr(e, k, v)
    e.save()
    return e


# ═══════════════════════════════════════════════════════════════
# THE WEEKLY REVIEW
# ═══════════════════════════════════════════════════════════════
def _money(v):
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def stats(entries):
    wins = [e.pnl for e in entries if e.pnl > 0]
    losses = [e.pnl for e in entries if e.pnl < 0]
    net = round(sum(e.pnl for e in entries), 2)
    gross_l = -sum(losses)
    return {
        'trades': len(entries), 'wins': len(wins), 'losses': len(losses), 'net': net,
        'win_rate': round(len(wins) / len(entries) * 100) if entries else None,
        'avg_win': round(statistics.mean(wins), 2) if wins else 0, 'avg_loss': round(-statistics.mean(losses), 2) if losses else 0,
        'profit_factor': round(sum(wins) / gross_l, 2) if gross_l else (None if not wins else 99),
        'best': round(max((e.pnl for e in entries), default=0), 2), 'worst': round(min((e.pnl for e in entries), default=0), 2),
    }


def _group(entries, key):
    g = defaultdict(list)
    for e in entries:
        k = key(e)
        if k:
            g[k].append(e)
    return {k: (round(sum(x.pnl for x in v), 2), len(v)) for k, v in g.items()}


def _session(e):
    h = (e.opened_at or e.closed_at).astimezone(timezone.utc).hour
    return next(name for a, b, name in SESSIONS if a <= h < b)


def analyse(entries, last_week=None):
    """→ (stats, feedback items, focus, grade). Each item: {kind: good|warn|tip, title, text}."""
    from .models import JournalEntry
    s = stats(entries)
    items = []
    if not entries:
        return s, items, '', ''
    add = lambda kind, title, text, weight=1: items.append({'kind': kind, 'title': title, 'text': text, 'w': weight})

    # results
    if s['net'] > 0:
        add('good', f"Profitable week: {_money(s['net'])}", f"{s['wins']} wins, {s['losses']} losses ({s['win_rate']}% win rate)"
            + (f", profit factor {s['profit_factor']}." if s['profit_factor'] and s['profit_factor'] < 99 else '.'))
    else:
        add('warn', f"Losing week: {_money(s['net'])}", f"{s['wins']} wins, {s['losses']} losses ({s['win_rate']}% win rate). "
            'A losing week is normal; what matters is that each loss was planned and small.', 2)
    if last_week and last_week.get('trades'):
        diff = s['net'] - last_week.get('net', 0)
        add('good' if diff >= 0 else 'tip', 'Compared with last week',
            f"{'Up' if diff >= 0 else 'Down'} {_money(abs(diff))} on last week ({_money(last_week.get('net', 0))}).")

    # reward vs risk
    if s['wins'] and s['losses'] and s['avg_loss'] > 1.5 * s['avg_win']:
        add('warn', 'Your losses are bigger than your wins',
            f"Average loss {_money(s['avg_loss'])} vs average win {_money(s['avg_win'])}. With this ratio you need over "
            f"{round(s['avg_loss'] / (s['avg_loss'] + s['avg_win']) * 100)}% winners just to break even. "
            'Keep the stop where the plan says and let winners reach target 1.', 3)
    elif s['wins'] and s['losses'] and s['avg_win'] >= 1.5 * s['avg_loss']:
        add('good', 'Winners bigger than losers', f"Average win {_money(s['avg_win'])} vs average loss {_money(s['avg_loss'])}. That is how traders stay profitable.")

    # holding losers longer than winners
    held = lambda xs: statistics.median([(e.closed_at - e.opened_at).total_seconds() / 60 for e in xs]) if xs else None
    timed = [e for e in entries if e.opened_at and e.closed_at]
    hw, hl = held([e for e in timed if e.pnl > 0]), held([e for e in timed if e.pnl < 0])
    if hw and hl and hl > 2 * hw and hl > 20:
        add('warn', 'You hold losers longer than winners',
            f"Losing trades stayed open about {round(hl)} min, winners only {round(hw)} min. Cutting winners early and hoping on losers is the most common leak.", 2)

    # revenge trading: a new trade within 30 minutes of a loss
    ordered = sorted(timed, key=lambda e: e.opened_at)
    closes = sorted(((e.closed_at, e.pnl) for e in timed), key=lambda x: x[0])
    revenge = [e for e in ordered if any(c < e.opened_at <= c + timedelta(minutes=30) and p < 0 for c, p in closes)]
    rev_net = round(sum(e.pnl for e in revenge), 2)
    if len(revenge) >= 2 and rev_net < 0:
        add('warn', 'Revenge trading', f"{len(revenge)} trades were opened within 30 minutes of a loss and lost {_money(-rev_net)} together. "
            'After a loss, step away for 30 minutes before the next trade.', 3)

    # overtrading
    per_day = defaultdict(list)
    for e in entries:
        per_day[timezone.localtime(e.closed_at, _tz()).date()].append(e)
    busiest_day, busiest = max(per_day.items(), key=lambda kv: len(kv[1]))
    if len(busiest) >= 10:
        bn = round(sum(e.pnl for e in busiest), 2)
        add('warn' if bn < 0 else 'tip', 'Overtrading',
            f"{len(busiest)} trades on {busiest_day:%A} ({_money(bn)}). More trades rarely means more profit: set a limit of 3–5 a day.", 2 if bn < 0 else 1)

    # stops
    no_stop = [e for e in entries if not e.had_stop]
    if no_stop:
        ns = round(sum(e.pnl for e in no_stop), 2)
        add('warn', 'Trades without a stop loss', f"{len(no_stop)} trade(s) had no stop ({_money(ns)}). One bad move without a stop can wipe out weeks of gains.", 3 if ns < 0 else 2)
    so = [e for e in entries if e.close_reason == 'SO']
    if so:
        add('warn', 'Stop out', f"{len(so)} position(s) closed by stop out: the account ran out of margin. Use smaller lots or less leverage.", 4)

    # position size consistency
    lots = [e.lots for e in entries if e.lots]
    if len(lots) >= 4:
        med = statistics.median(lots)
        big = [e for e in entries if e.lots and e.lots > 3 * med]
        if big:
            bn = round(sum(e.pnl for e in big), 2)
            add('warn' if bn < 0 else 'tip', 'Inconsistent position size',
                f"{len(big)} trade(s) used more than 3× your usual {med:g} lots ({_money(bn)}). Use the risk calculator in every signal to size each trade the same way.", 2 if bn < 0 else 1)

    # markets
    by_sym = _group(entries, lambda e: e.symbol)
    if len(by_sym) >= 2:
        best = max(by_sym.items(), key=lambda kv: kv[1][0])
        worst = min(by_sym.items(), key=lambda kv: kv[1][0])
        if best[1][0] > 0:
            add('good', f'Best market: {best[0]}', f"{_money(best[1][0])} from {best[1][1]} trade(s).")
        if worst[1][0] < 0 and worst[0] != best[0]:
            add('tip', f'Weakest market: {worst[0]}', f"{_money(worst[1][0])} from {worst[1][1]} trade(s). Consider pausing it or trading it only on A+ signals.", 1)

    # sessions
    by_sess = _group(timed, _session)
    if len(by_sess) >= 2:
        worst = min(by_sess.items(), key=lambda kv: kv[1][0])
        best = max(by_sess.items(), key=lambda kv: kv[1][0])
        if worst[1][0] < 0 and worst[1][1] >= 2:
            add('tip', f'Hard session: {worst[0]}', f"{_money(worst[1][0])} from {worst[1][1]} trade(s). Your best was the {best[0]} ({_money(best[1][0])}).", 1)

    # direction
    by_dir = _group(entries, lambda e: e.direction)
    if len(by_dir) == 2 and min(v[1] for v in by_dir.values()) >= 3:
        b, sl = by_dir.get('BUY', (0, 0)), by_dir.get('SELL', (0, 0))
        if (b[0] > 0) != (sl[0] > 0):
            good, bad = ('buys', 'sells') if b[0] > sl[0] else ('sells', 'buys')
            add('tip', f'Your {good} work better than your {bad}', f"Buys {_money(b[0])} ({b[1]}), sells {_money(sl[0])} ({sl[1]}).")

    # tags: emotions, plan, AI signals
    emo = _group(entries, lambda e: e.emotion)
    bad_emo = {k: v for k, v in emo.items() if k in ('fearful', 'greedy', 'revenge', 'fomo', 'bored') and v[0] < 0}
    if bad_emo:
        names = dict(JournalEntry.EMOTIONS)
        txt = ', '.join(f"{names[k].lower()} {_money(v[0])} ({v[1]})" for k, v in sorted(bad_emo.items(), key=lambda kv: kv[1][0]))
        add('warn', 'Emotions cost money', f"Trades you tagged as {txt}. When you feel this way, close the chart.", 2)
    plan = _group(entries, lambda e: {True: 'yes', False: 'no'}.get(e.followed_plan))
    if 'no' in plan:
        add('warn' if plan['no'][0] < 0 else 'tip', 'Trades off the plan',
            f"Off-plan trades: {_money(plan['no'][0])} ({plan['no'][1]}). On-plan: {_money(plan.get('yes', (0, 0))[0])} ({plan.get('yes', (0, 0))[1]}).", 2 if plan['no'][0] < 0 else 1)
    ai = [e for e in entries if e.setup == 'ai_signal' or e.source == 'copy']
    other = [e for e in entries if e not in ai]
    if ai and other:
        an, on = round(sum(e.pnl for e in ai), 2), round(sum(e.pnl for e in other), 2)
        add('good' if an >= on else 'tip', 'AI signals vs your own trades',
            f"AI signal trades {_money(an)} ({len(ai)}), your own ideas {_money(on)} ({len(other)}).")
    untagged = sum(1 for e in entries if not e.emotion and e.source != 'copy')
    if untagged >= 3:
        add('tip', 'Tag your trades', f"{untagged} trades have no emotion or setup. Tagging takes 5 seconds and makes next week's review much sharper.")

    # grade and focus
    score = 60
    if s['profit_factor']:
        score += min(20, (s['profit_factor'] - 1) * 20)
    score += (s['win_rate'] - 50) / 3 if s['win_rate'] is not None else 0
    score -= sum(i['w'] * 4 for i in items if i['kind'] == 'warn')
    grade = 'A' if score >= 80 else 'B' if score >= 65 else 'C' if score >= 50 else 'D' if score >= 35 else 'E'
    warns = sorted((i for i in items if i['kind'] == 'warn'), key=lambda i: -i['w'])
    focus = (f"Fix one thing next week: {warns[0]['title'].lower()}." if warns
             else 'Keep doing what works: same size, same rules, only A and A+ setups.')
    for i in items:
        i.pop('w', None)
    order = {'warn': 0, 'tip': 1, 'good': 2}
    items.sort(key=lambda i: order[i['kind']])
    return s, items, focus, grade


def review(user, start, save=True):
    from .models import JournalEntry, WeeklyReview
    sync(user)
    a, b = _bounds(start)
    entries = list(JournalEntry.objects.filter(user=user, closed_at__gte=a, closed_at__lt=b))
    prev = WeeklyReview.objects.filter(user=user, week_start=start - timedelta(days=7)).first()
    if prev is None:
        pa, pb = _bounds(start - timedelta(days=7))
        prev_stats = stats(list(JournalEntry.objects.filter(user=user, closed_at__gte=pa, closed_at__lt=pb)))
    else:
        prev_stats = prev.stats
    s, items, focus, grade = analyse(entries, prev_stats)
    if not save:
        return WeeklyReview(user=user, week_start=start, stats=s, feedback=items, focus=focus, grade=grade)
    r, _ = WeeklyReview.objects.update_or_create(user=user, week_start=start, defaults={
        'stats': s, 'feedback': items, 'focus': focus, 'grade': grade})
    return r


def run_weekly(now=None):
    """Monday from 06:00 local: review last week for everyone who traded. Once per week."""
    from django.contrib.auth.models import User
    from .models import Trade, CopyTrade, JournalEntry
    from .notifications import notify
    now = timezone.localtime(now or timezone.now(), _tz())
    if now.weekday() != 0 or now.hour < 6:
        return 0
    start = week_start(now.date()) - timedelta(days=7)
    key = f'journal:weekly:{start}'
    if cache.get(key):
        return 0
    cache.set(key, 1, 8 * 86400)
    a, b = _bounds(start)
    ids = set(Trade.objects.filter(status__in=('WON', 'LOST'), date_closed__gte=a, date_closed__lt=b).values_list('user_id', flat=True))
    ids |= set(CopyTrade.objects.filter(status='CLOSED', closed_at__gte=a, closed_at__lt=b).values_list('user_id', flat=True))
    ids |= set(JournalEntry.objects.filter(closed_at__gte=a, closed_at__lt=b).values_list('user_id', flat=True))
    n = 0
    for user in User.objects.filter(pk__in=ids, is_active=True):
        try:
            r = review(user, start)
            if not r.stats.get('trades'):
                continue
            notify(user, title=f"📒 Your weekly trading review · grade {r.grade}",
                   message=f"{r.stats['trades']} trades, {_money(r.stats['net'])}. {r.focus}",
                   category='TRADE', priority='info', action_url=f'/trade-journal/?week={start}', action_label='Read review')
            n += 1
        except Exception as e:
            logger.warning(f'weekly review {user}: {e}')
    return n


def safe_weekly():
    try:
        return run_weekly()
    except Exception as e:
        logger.warning(f'weekly reviews: {e}')
        return 0
