# store/ai_gold.py
"""
AI Gold Auto-Trader on real XAUUSD prices.

  Entry   The chart scanner reads the real 15-minute gold chart (scalp style:
          5m / 15m / 1h). A trade is opened only when it gives a BUY or SELL
          setup, at the live ask (buy) or bid (sell), with the setup's stop
          loss and take profit. No setup → no trade.
  Size    1 % of the funded balance at risk at the stop, 0.01 – 1.00 lots.
  Exit    The trade stays open until the real market touches the stop loss or
          take profit (checked against 1-minute candles since entry, so touches
          between checks are not missed), or until MAX_HOLD has passed, when it
          closes at the market price.
  Payout  Wins are credited at the funded account's retention rate (88 %);
          losses are charged in full. The balance only changes when a trade closes.

settle_user() / settle_all() close trades that have hit a level; the price
tracker calls settle_all() so trades close even when nobody has the page open.
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.db import transaction
from django.utils import timezone

from . import mt5_service as mt5

logger = logging.getLogger(__name__)

SYMBOL = 'XAUUSD'
STYLE = 'scalp'                       # 15-minute chart, 1-hour trend filter
RISK_PER_TRADE = Decimal('0.01')      # of funded balance
MIN_LOTS, MAX_LOTS = Decimal('0.01'), Decimal('1.00')
MAX_HOLD = timedelta(hours=5)         # 20 bars of the 15-minute chart
RETENTION = Decimal('0.88')           # share of a winning trade's profit the trader keeps


def _models():
    from .models import ClientAIToggle, FundedAccount, FundedTrade, Transaction
    return ClientAIToggle, FundedAccount, FundedTrade, Transaction


def _d(v):
    return Decimal(str(v)).quantize(Decimal('0.01'))


def trade_dict(t, quote=None):
    out = {
        'trade_id': t.id,
        'direction': t.direction,
        'entry': float(t.entry_price),
        'exit': float(t.exit_price) if t.exit_price is not None else None,
        'sl': float(t.stop_loss) if t.stop_loss is not None else None,
        'tp': float(t.take_profit) if t.take_profit is not None else None,
        'lot_size': float(t.lot_size),
        'profit': float(t.profit_amount),
        'status': t.status,
        'reason': t.close_reason,
        'note': t.signal_note,
        'opened_at': t.created_at.isoformat() if t.created_at else None,
        'closed_at': t.closed_at.isoformat() if t.closed_at else None,
    }
    if t.status == 'OPEN' and quote is not None:
        close = quote.bid if t.direction == 'BUY' else quote.ask
        out['current'] = float(close)
        out['floating'] = round(float(mt5.calc_profit(SYMBOL, t.direction, t.lot_size, t.entry_price, close)), 2)
    return out


# ═══════════════════════════════════════════════════════════════
# SETTLEMENT
# ═══════════════════════════════════════════════════════════════
def _first_touch(t, candles, spread):
    """Walk 1-minute candles after entry; return (price, 'SL'|'TP') for the first level touched."""
    sl = float(t.stop_loss) if t.stop_loss is not None else None
    tp = float(t.take_profit) if t.take_profit is not None else None
    opened = int(t.created_at.timestamp())
    for c in candles:
        if c['time'] + 60 <= opened:          # candle finished before the trade existed
            continue
        lo, hi = float(c['low']), float(c['high'])
        if t.direction == 'BUY':              # a buy closes at the bid
            hit_sl = sl is not None and lo <= sl
            hit_tp = tp is not None and hi >= tp
        else:                                 # a sell closes at the ask
            hit_sl = sl is not None and hi + spread >= sl
            hit_tp = tp is not None and lo + spread <= tp
        if hit_sl:                            # both in one candle: assume the stop came first
            return sl, 'SL'
        if hit_tp:
            return tp, 'TP'
    return None, None


def _close(t, price, reason):
    ClientAIToggle, FundedAccount, FundedTrade, Transaction = _models()
    with transaction.atomic():
        t = FundedTrade.objects.select_for_update().get(pk=t.pk)
        if t.status != 'OPEN':
            return None
        acct = FundedAccount.objects.select_for_update().get(pk=t.funded_account_id)
        gross = mt5.calc_profit(SYMBOL, t.direction, t.lot_size, t.entry_price, price)
        net = gross * RETENTION if gross > 0 else gross
        t.exit_price = _d(price)
        t.profit_percent = RETENTION if gross > 0 else Decimal('1.00')
        t.profit_amount = _d(net)
        t.status = 'WON' if net > 0 else 'LOST'
        t.close_reason = reason
        t.closed_at = timezone.now()
        t.save(update_fields=['exit_price', 'profit_percent', 'profit_amount', 'status', 'close_reason', 'closed_at'])
        acct.balance = _d(acct.balance + t.profit_amount)
        acct.save(update_fields=['balance'])
        Transaction.objects.create(
            user=t.user, amount=t.profit_amount, transaction_type='AI_GOLD_TRADE',
            details=(f"AI Gold {t.direction} {t.lot_size} XAUUSD {t.entry_price} → {t.exit_price} "
                     f"({ {'SL': 'stop loss', 'TP': 'take profit', 'TIME': 'time exit'}.get(reason, reason) }). "
                     f"Result {t.profit_amount:+}. Balance ${acct.balance}"),
        )
    return t


def settle_trades(trades):
    """Close any of these OPEN trades whose stop / target was touched or whose time is up."""
    trades = list(trades)
    if not trades:
        return []
    from . import chart_scanner
    quote = mt5.cached_quote(SYMBOL)
    spec = mt5.SPECS[SYMBOL]
    spread = float(spec.spread * spec.point)
    candles = chart_scanner.candles_for(SYMBOL, '1m')['candles']
    closed = []
    for t in trades:
        price, reason = _first_touch(t, candles, spread)
        if price is None and quote is not None:
            # live quote may be newer than the last candle
            cur = float(quote.bid if t.direction == 'BUY' else quote.ask)
            sl, tp = t.stop_loss, t.take_profit
            if t.direction == 'BUY':
                if sl is not None and cur <= float(sl): price, reason = float(sl), 'SL'
                elif tp is not None and cur >= float(tp): price, reason = float(tp), 'TP'
            else:
                if sl is not None and cur >= float(sl): price, reason = float(sl), 'SL'
                elif tp is not None and cur <= float(tp): price, reason = float(tp), 'TP'
            if price is None and timezone.now() - t.created_at >= MAX_HOLD:
                price, reason = cur, 'TIME'
        if price is not None:
            done = _close(t, price, reason)
            if done:
                closed.append(done)
    return closed


def settle_user(user):
    _, _, FundedTrade, _ = _models()
    return settle_trades(FundedTrade.objects.filter(user=user, asset=SYMBOL, status='OPEN'))


def settle_all():
    _, _, FundedTrade, _ = _models()
    try:
        return settle_trades(FundedTrade.objects.filter(asset=SYMBOL, status='OPEN').select_related('funded_account'))
    except Exception as e:
        logger.warning(f"AI Gold settlement failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# ONE AUTO-TRADER CYCLE
# ═══════════════════════════════════════════════════════════════
def lots_for(balance, risk_per_lot):
    if risk_per_lot <= 0:
        return MIN_LOTS
    lots = (Decimal(str(balance)) * RISK_PER_TRADE / Decimal(str(risk_per_lot))).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    return max(MIN_LOTS, min(MAX_LOTS, lots))


def run_cycle(user, funded_account, ai_toggle):
    """
    Settle → (if nothing is open and the daily allowance allows) look for a setup and open one.
    Returns {'trades': closed trades, 'opened': new trade or None, 'open_trade': running trade or None,
             'message': what the AI did, 'live_price': float|None}
    """
    ClientAIToggle, FundedAccount, FundedTrade, _ = _models()
    closed = [trade_dict(t) for t in settle_user(user)]
    quote = mt5.cached_quote(SYMBOL)
    live = float(quote.bid) if quote else None
    open_t = FundedTrade.objects.filter(user=user, asset=SYMBOL, status='OPEN').order_by('-created_at').first()
    res = {'trades': closed, 'opened': None, 'open_trade': trade_dict(open_t, quote) if open_t else None,
           'message': '', 'live_price': live}
    if open_t:
        res['message'] = 'Managing the open trade until its stop loss or take profit is reached.'
        return res
    if not ai_toggle.can_trade_today():
        res['message'] = 'Daily trade limit reached.'
        return res
    funded_account.refresh_from_db()
    if funded_account.balance <= 0:
        res['message'] = 'Funded balance is $0.'
        return res

    try:
        mt5.require_market_open(SYMBOL)          # say "market closed" plainly instead of "no price"
    except mt5.TradeError as e:
        res['message'] = f"{e}. No trade opened."
        return res

    q = mt5.live_quote(SYMBOL)
    if q is None:
        res['message'] = 'No live gold price right now (market closed or feed offline). No trade opened.'
        return res

    from . import chart_scanner
    a = chart_scanner.analyse(SYMBOL, STYLE)
    if not a.get('ok'):
        res['message'] = f"Chart analysis unavailable: {a.get('error') or 'no data'}. No trade opened."
        return res
    if a['signal'] == 'WAIT' or not a.get('plan'):
        lean = (a.get('lean') or '').lower()
        res['message'] = (f"No clear setup on the 15-minute chart (strength {a['confidence']}, leaning {lean}). "
                          f"Waiting.")
        return res

    direction = a['signal']
    entry = q.ask if direction == 'BUY' else q.bid
    sl, tp = Decimal(str(a['plan']['sl'])), Decimal(str(a['plan']['tp1']))
    if (direction == 'BUY' and not (sl < entry < tp)) or (direction == 'SELL' and not (tp < entry < sl)):
        res['message'] = 'Price moved past the setup levels since the analysis. Waiting for the next one.'
        return res
    risk_per_lot = abs(mt5.calc_profit(SYMBOL, direction, 1, entry, sl))
    lots = lots_for(funded_account.balance, risk_per_lot)
    note = '; '.join(r['text'] for r in a.get('reasons', [])[:2])[:255]

    with transaction.atomic():
        toggle = ClientAIToggle.objects.select_for_update().get(pk=ai_toggle.pk)
        if not toggle.can_trade_today() or FundedTrade.objects.filter(user=user, asset=SYMBOL, status='OPEN').exists():
            res['message'] = 'Another cycle already opened a trade.'
            return res
        t = FundedTrade.objects.create(
            funded_account=funded_account, user=user, asset=SYMBOL, direction=direction,
            entry_price=_d(entry), lot_size=lots, stop_loss=_d(sl), take_profit=_d(tp),
            profit_percent=Decimal('0'), profit_amount=Decimal('0'), status='OPEN', signal_note=note,
        )
        toggle.daily_trades_count += 1
        toggle.last_trade_date = timezone.now().date()
        toggle.save(update_fields=['daily_trades_count', 'last_trade_date'])
        ai_toggle.daily_trades_count = toggle.daily_trades_count
    res['opened'] = res['open_trade'] = trade_dict(t, q)
    res['message'] = (f"Opened {direction} {lots} lots at {_d(entry)} · SL {_d(sl)} · TP {_d(tp)} "
                      f"(risk ≈ ${_d(risk_per_lot * lots)}, strength {a['confidence']}).")
    return res


# ═══════════════════════════════════════════════════════════════
# RECORD ON THE FUNDED ACCOUNT
# ═══════════════════════════════════════════════════════════════
def stats(user):
    """Lifetime AI Gold results on this user's funded account (closed trades, from the database)."""
    _, _, FundedTrade, _ = _models()
    from django.db.models import Count, Q, Sum
    agg = FundedTrade.objects.filter(user=user, asset=SYMBOL, status__in=('WON', 'LOST')).aggregate(
        trades=Count('id'), wins=Count('id', filter=Q(status='WON')),
        pnl=Sum('profit_amount'), won=Sum('profit_amount', filter=Q(status='WON')),
        lost=Sum('profit_amount', filter=Q(status='LOST')))
    n, w = agg['trades'] or 0, agg['wins'] or 0
    return {
        'trades': n, 'wins': w, 'losses': n - w,
        'win_rate': round(w * 100 / n) if n else 0,
        'pnl': float(agg['pnl'] or 0), 'won': float(agg['won'] or 0), 'lost': float(agg['lost'] or 0),
        'open': FundedTrade.objects.filter(user=user, asset=SYMBOL, status='OPEN').count(),
    }


def history(user, limit=50):
    _, _, FundedTrade, _ = _models()
    return list(FundedTrade.objects.filter(user=user, asset=SYMBOL).order_by('-created_at')[:limit])
