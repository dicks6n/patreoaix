# store/copy_trading.py
"""
AI Copy Trading: one switch copies every A+ signal onto the customer's Deriv account as a Multipliers contract.

  • The signal's stop and target become Deriv's own stop loss / take profit (USD amounts), so the trade is
    protected on Deriv even if this site is down.
  • The multiplier is picked from the stop distance so the stop costs at most ~80% of the stake
    (or the customer chooses one).
  • Limits per customer: open trades at once, trades per day, Pro members only.
  • When the AI closes or retires the signal, the copy is closed too.
  • Demo by default. Real money only when the owner switch DERIV_REAL_TRADING is on, the customer turned on real
    trading for their account, and the stake is within DERIV_REAL_MAX_STAKE (enforced in deriv_demo).

Runs inside the scanner worker (signal_alerts.record_signals / _close); every step is wrapped so a Deriv error
never stops the scan.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from . import deriv_demo as dd
from . import signal_engine as se

logger = logging.getLogger(__name__)

STOP_SHARE = 0.8          # the stop may cost at most this share of the stake when picking the multiplier


def settings_for(user):
    from .models import CopyTradingSettings
    return CopyTradingSettings.objects.get_or_create(user=user)[0]


def plan(sig, stake, multipliers, chosen=None):
    """→ (multiplier, stop_loss_usd, take_profit_usd, note). Profit on Deriv ≈ stake × multiplier × move / entry."""
    entry = float(sig.entry)
    dist = abs(entry - float(sig.stop_loss)) or entry * 0.001
    gain = abs(float(sig.take_profit) - entry)
    opts = sorted(multipliers or [])
    if chosen:
        mult = int(chosen) if not opts or int(chosen) in opts else min(opts, key=lambda m: abs(m - int(chosen)))
    else:
        fit = [m for m in opts if stake * m * dist / entry <= stake * STOP_SHARE]
        mult = fit[-1] if fit else (opts[0] if opts else None)
    if not mult:
        return None, None, None, 'This market is not available on Deriv Multipliers.'
    sl = stake * mult * dist / entry
    tp = round(max(0.1, stake * mult * gain / entry), 2)
    note = ''
    if sl >= stake:                                    # Deriv's stop loss can't exceed the stake: stop-out protects
        sl, note = None, "Stop is wider than this multiplier allows: Deriv's stop-out (stake) protects the trade."
    else:
        sl = round(max(0.1, sl), 2)
    return mult, sl, tp, note


def _eligible(cfg, sig):
    from .membership import is_member
    if se.GRADE_RANK.get(sig.grade, 0) < se.GRADE_RANK.get(cfg.min_grade or 'A+', 3):
        return False
    if sig.style not in (cfg.styles or ['intraday']):
        return False
    if cfg.symbols and sig.symbol not in cfg.symbols:
        return False
    return cfg.user.is_active and is_member(cfg.user)


def _notify(user, title, message, ok=True):
    try:
        from .notifications import notify
        notify(user, title=title, message=message, category='TRADE', priority='success' if ok else 'warning',
               action_url='/copy-trading/', action_label='Copy Trading')
    except Exception:
        pass


def copy_for(cfg, sig):
    """Copy one signal for one customer. Returns the CopyTrade row (OPEN, FAILED or SKIPPED) or None."""
    from .models import CopyTrade
    if CopyTrade.objects.filter(user=cfg.user, signal=sig).exists():
        return None
    row = CopyTrade(user=cfg.user, signal=sig, account=cfg.account, symbol=sig.symbol, direction=sig.direction,
                    stake=cfg.stake)
    mine = CopyTrade.objects.filter(user=cfg.user)
    day_ago = timezone.now() - timedelta(days=1)
    if mine.filter(status='OPEN').count() >= cfg.max_open:
        row.status, row.note = 'SKIPPED', f'Already {cfg.max_open} copied trades open (your limit).'
    elif mine.filter(status__in=('OPEN', 'CLOSED'), created_at__gte=day_ago).count() >= cfg.max_per_day:
        row.status, row.note = 'SKIPPED', f'{cfg.max_per_day} trades in the last 24 hours (your daily limit).'
    if row.status == 'SKIPPED':
        row.save()
        return row
    acc = dd.account_for(cfg.user, cfg.account)
    if not acc:
        row.status, row.note = 'FAILED', f'No Deriv {cfg.account} account connected.'
        row.save()
        return row
    stake = float(cfg.stake)
    mult, sl, tp, note = plan(sig, stake, dd.multiplier_options(sig.symbol), cfg.multiplier)
    row.multiplier, row.stop_loss, row.take_profit, row.note = mult, sl, tp, note
    if not mult:
        row.status = 'SKIPPED'
        row.save()
        return row
    try:
        res = dd.open_multiplier(acc, sig.symbol, sig.direction, stake, mult, stop_loss=sl, take_profit=tp)
        row.contract_id, row.status = res['contract_id'], 'OPEN'
        row.save()
        _notify(cfg.user, f'🤖 Copied: {sig.direction} {sig.symbol}',
                f"{sig.grade} signal copied to your Deriv {cfg.account} account: stake ${stake:.2f} × {mult}"
                + (f', stop ${sl:.2f}' if sl else '') + f', target ${tp:.2f}.')
    except Exception as e:
        row.status, row.note = 'FAILED', str(e)[:255]
        row.save()
        _notify(cfg.user, f'Copy failed: {sig.symbol}', f'Deriv did not open the trade: {row.note}', ok=False)
    return row


def on_new_signal(sig):
    """Called for every new ScannerSignal: copy it for every customer whose switch is on."""
    from .models import CopyTradingSettings
    rows = []
    for cfg in CopyTradingSettings.objects.filter(enabled=True).select_related('user'):
        try:
            if _eligible(cfg, sig):
                r = copy_for(cfg, sig)
                if r:
                    rows.append(r)
        except Exception as e:
            logger.warning(f'copy trade {cfg.user} {sig}: {e}')
    return rows


def _finish(row, pos):
    row.status, row.closed_at = 'CLOSED', timezone.now()
    row.profit = round(float(pos.get('profit') or 0), 2) if pos else row.profit
    row.save(update_fields=['status', 'closed_at', 'profit'])


def close_row(row, why=''):
    """Close a copied trade on Deriv (or record it if Deriv's own stop / target already closed it)."""
    acc = dd.account_for(row.user, row.account)
    if not acc or not row.contract_id:
        row.status, row.closed_at = 'CLOSED', timezone.now()
        row.save(update_fields=['status', 'closed_at'])
        return row
    pos = dd.contract_status(acc, row.contract_id)
    if not pos.get('is_sold') and pos.get('can_sell', True):
        res = dd.close_multiplier(acc, row.contract_id)
        pos = {'profit': res.get('profit')}
    _finish(row, pos)
    sign = '+' if (row.profit or 0) >= 0 else '−'
    _notify(row.user, f'Copy closed: {row.direction} {row.symbol} {sign}${abs(row.profit or 0):.2f}',
            f'{why or "The AI closed this signal."} Result on your Deriv {row.account} account: {sign}${abs(row.profit or 0):.2f}.',
            ok=(row.profit or 0) >= 0)
    return row


def on_signal_closed(sig):
    """The AI closed or retired a signal: close every copy of it."""
    from .models import CopyTrade
    why = {'TP': 'The signal reached its target.', 'SL': 'The signal hit its stop.',
           'EXPIRED': 'The AI retired this signal.'}.get(sig.outcome, '')
    for row in CopyTrade.objects.filter(signal=sig, status='OPEN').select_related('user'):
        try:
            close_row(row, why)
        except Exception as e:
            logger.warning(f'copy close {row}: {e}')


def sync(user):
    """Refresh the customer's open copies from Deriv (live profit; closed by Deriv's own stop / target)."""
    from .models import CopyTrade
    out = {}
    for row in CopyTrade.objects.filter(user=user, status='OPEN'):
        acc = dd.account_for(user, row.account)
        if not acc or not row.contract_id:
            continue
        try:
            pos = dd.contract_status(acc, row.contract_id)
        except Exception as e:
            out[row.id] = {'error': str(e)}
            continue
        if pos.get('is_sold'):
            _finish(row, pos)
        else:
            row.profit = round(float(pos.get('profit') or 0), 2)
            row.save(update_fields=['profit'])
        out[row.id] = {'status': row.status, 'profit': row.profit}
    return out


def stats(user):
    from .models import CopyTrade
    rows = list(CopyTrade.objects.filter(user=user, status__in=('OPEN', 'CLOSED')))
    closed = [r for r in rows if r.status == 'CLOSED']
    wins = sum(1 for r in closed if (r.profit or 0) > 0)
    return {'trades': len(rows), 'open': len(rows) - len(closed), 'closed': len(closed), 'wins': wins,
            'win_rate': round(wins / len(closed) * 100) if closed else None,
            'profit': round(sum(r.profit or 0 for r in rows), 2)}


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        logger.warning(f'copy trading {fn.__name__}: {e}')
        return None
