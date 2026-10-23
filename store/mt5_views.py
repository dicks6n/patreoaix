# store/mt5_views.py
"""
HTTP endpoints of the MT5 terminal. All pricing and accounting is done by
mt5_service — these views only parse the request and shape the response.
Prices sent by the browser are ignored: the server fills at its own bid/ask.
"""
import json
import logging
from datetime import timedelta, datetime
from decimal import Decimal
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import mt5_service as mt5
from . import price_feed
from .models import Account, PendingOrder, Trade, Transaction
from .mt5_service import TradeError

logger = logging.getLogger(__name__)

TRADE_DIRECTIONS = ('BUY', 'SELL')


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        raise TradeError("Invalid request")


def trade_api(view):
    """Turn TradeError into a 400 JSON answer and anything unexpected into a 500."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except TradeError as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
        except Exception:
            logger.exception(f"{view.__name__} failed")
            return JsonResponse({'success': False, 'error': 'Trade server error, please try again'}, status=500)
    return wrapper


def _require_kyc(user):
    from .views import user_has_approved_kyc
    if not user_has_approved_kyc(user):
        raise TradeError("KYC verification required to trade.")


def _fmt_time(dt):
    return timezone.localtime(dt).strftime('%Y.%m.%d %H:%M:%S') if dt else ''


def _f(v):
    return float(v) if v is not None else None


# ═══════════════════════════════════════════════════════════════
# TERMINAL PAGE
# ═══════════════════════════════════════════════════════════════
def is_mobile_phone(request):
    """
    True for phones (tablets and desktops are not phones). Detects the device, not just the browser text:
      - user-agent says mobile / iPhone
      - Chrome's client hint Sec-CH-UA-Mobile: ?1 (sent even when the UA string is reduced)
      - px_phone cookie: set by mt5_mobile_only.html after checking the real screen + touch, so an
        iPhone with "Request Desktop Website" or an Android phone in desktop mode still gets the terminal
    """
    from .middleware import parse_user_agent
    if parse_user_agent(request.META.get('HTTP_USER_AGENT', ''))['device_type'] == 'mobile':
        return True
    if request.META.get('HTTP_SEC_CH_UA_MOBILE') == '?1':
        return True
    return request.COOKIES.get('px_phone') == '1'


@login_required
def mt5_terminal_view(request):
    # Phone-only page. Staff can preview on a computer with ?preview=1.
    # launched from the home-screen app (manifest start_url / cookie set by the installed app) → allowed on tablets too
    installed_app = request.GET.get('source') == 'pwa' or request.COOKIES.get('px_pwa') == '1'
    if not is_mobile_phone(request) and not installed_app and not (request.user.is_staff and request.GET.get('preview') == '1'):
        return render(request, 'store/mt5_mobile_only.html', {
            'terminal_url': request.build_absolute_uri(request.path),
        })

    symbols = list(mt5.MARKET_WATCH)
    for s in Trade.objects.filter(user=request.user, status='OPEN').values_list('asset__symbol', flat=True):
        if s and s in mt5.SPECS and s not in symbols:
            symbols.append(s)
    for s in PendingOrder.objects.filter(user=request.user, status='PENDING').values_list('symbol', flat=True):
        if s in mt5.SPECS and s not in symbols:
            symbols.append(s)
    # Every symbol the Symbols panel can add (synthetics need the Deriv feed, which the terminal doesn't stream)
    tradable = [s for s, spec in mt5.SPECS.items() if spec.kind != 'synthetic' or s in symbols]
    price_feed.refresh_all_async()
    return render(request, 'store/mt5_terminal.html', {
        'metrics': mt5.get_metrics(request.user),
        'mt5_config': {
            'specs': mt5.symbol_specs_json(tradable),
            'market_watch': symbols,
            'margin_call_level': float(mt5.MARGIN_CALL_LEVEL),
            'stop_out_level': float(mt5.STOP_OUT_LEVEL),
            'ticket_offset': mt5.TICKET_OFFSET,
            'currency': mt5.ACCOUNT_CURRENCY,
        },
    })


# ═══════════════════════════════════════════════════════════════
# QUOTES & METRICS
# ═══════════════════════════════════════════════════════════════
@login_required
@require_GET
def api_mt5_quotes(request):
    """Bid/ask for Market Watch — REST fallback for the WebSocket quote stream."""
    symbols = [s for s in (request.GET.get('symbols') or '').upper().split(',') if s in mt5.SPECS]
    price_feed.refresh_all_async()
    return JsonResponse({'success': True, 'quotes': mt5.quotes_payload(symbols or mt5.MARKET_WATCH)})


@login_required
def api_mt5_metrics(request):
    """Account figures. Also runs the order engine for this user (pending fills, S/L, T/P, stop out)."""
    mt5.process_user_orders(request.user)
    m = mt5.get_metrics(request.user)
    return JsonResponse({
        'success': True,
        'balance': float(m['balance']),
        'profit': float(m['profit']),
        'swap': float(m['swap']),
        'floating_pnl': float(m['floating_pnl']),
        'equity': float(m['equity']),
        'margin_used': float(m['margin_used']),
        'free_margin': float(m['free_margin']),
        'margin_level': _f(m['margin_level']),
        'margin_call_level': float(m['margin_call_level']),
        'stop_out_level': float(m['stop_out_level']),
        'margin_call': m['margin_call'],
        'position_count': m['position_count'],
        'can_open_new': m['can_open_new'],
    })


# ═══════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════
def _notional_usd(symbol, volume, price):
    """Position size in USD (contract size × lots × price), used by the risk pages."""
    spec = mt5.SPECS.get(symbol)
    if not spec:
        return None
    try:
        return round(float(mt5.to_usd(spec.contract_size * Decimal(str(volume or 0)) * Decimal(str(price)), spec.quote)), 2)
    except Exception:
        return None


def _position_dict(t):
    s = mt5.position_snapshot(t)
    pnl = float(s['profit'])
    return {
        'id': t.id,
        'ticket': mt5.ticket_for(t),
        'symbol': s['symbol'],
        'direction': t.direction,
        'volume': float(t.stake or 0),
        'entry': float(t.entry_price or 0),
        'current_price': float(s['current']),
        'pnl': pnl,
        'swap': float(s['swap']),
        'commission': float(s['commission']),
        'margin': float(s['margin']),
        'sl': _f(t.stop_loss),
        'tp': _f(t.take_profit),
        'trailing': t.trailing_points or 0,
        'opened': t.date_opened.isoformat() if t.date_opened else None,
        'opened_at': _fmt_time(t.date_opened),
        'notional_usd': _notional_usd(s['symbol'], t.stake, s['current']),
        # kept for older pages
        'pnl_display': f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}",
        'is_positive': pnl >= 0,
    }


@login_required
def api_open_positions(request):
    trades = Trade.objects.filter(user=request.user, status='OPEN').select_related('asset').order_by('date_opened')
    positions = [_position_dict(t) for t in trades]
    m = mt5.get_metrics(request.user)
    return JsonResponse({
        'success': True,
        'positions': positions,
        'total_pnl': float(m['floating_pnl']),
        'balance': float(m['balance']),
        'equity': float(m['equity']),
        'count': len(positions),
    })


@login_required
@csrf_exempt          # older pages post here without a CSRF token
@require_POST
@trade_api
def execute_trade(request):
    """
    Market Execution for BUY / SELL. Body: {symbol, contract_type|order_type, volume, sl?, tp?}
    Digit contracts (DIGIT*, CALL, PUT …) are passed on to the original handler.
    """
    data = _json_body(request)
    contract = (data.get('contract_type') or data.get('order_type') or '').upper()
    if contract not in TRADE_DIRECTIONS:
        from .views import execute_trade as legacy_execute
        return legacy_execute(request)

    _require_kyc(request.user)
    trade = mt5.open_position(
        request.user, data.get('symbol'), contract,
        data.get('volume', data.get('amount')), data.get('sl'), data.get('tp'),
    )
    m = mt5.get_metrics(request.user)
    return JsonResponse({
        'success': True,
        'trade_id': trade.id,
        'ticket': mt5.ticket_for(trade),
        'ticket_id': f"#{mt5.ticket_for(trade)}",
        'symbol': trade.asset.symbol,
        'order_type': trade.direction,
        'volume': float(trade.stake),
        'entry': float(trade.entry_price),
        'sl': _f(trade.stop_loss),
        'tp': _f(trade.take_profit),
        'margin_used': float(trade.margin_used),
        'leverage': trade.leverage,
        'contract_size': float(trade.contract_size),
        'commission': float(trade.commission),
        'new_balance': float(m['balance']),
        'equity': float(m['equity']),
        'free_margin': float(m['free_margin']),
    })


def _close_response(res):
    closed, net = res['closed'], res['net']
    return {
        'success': True,
        'ticket': mt5.ticket_for(res['trade']),
        'status': closed.status,
        'price': float(res['price']),
        'closed_volume': float(res['volume']),
        'remaining_volume': float(res['remaining']),
        'pnl': float(net),
        'profit': float(closed.pnl),
        'swap': float(closed.swap),
        'commission': float((closed.commission or 0) + (closed.close_commission or 0)),
        'payout': float(closed.payout or 0),
        'new_balance': float(res['balance']),
    }


def _send_close_sms(user, trade, net, status):
    try:
        from .sms_service import sms_service
        if abs(net) >= Decimal('0.10'):
            sms_service.send_trade_closed(user, symbol=trade.asset.symbol, direction=trade.direction,
                                          pnl=float(net), result=status)
    except Exception as e:
        logger.warning(f"Trade close SMS failed: {e}")


@login_required
@csrf_exempt          # older pages post here without a CSRF token
@require_POST
@trade_api
def close_trade_api(request, trade_id):
    """Close a position at market. Body: {volume?} — less than the position volume is a partial close."""
    data = _json_body(request)
    res = mt5.close_position(request.user, trade_id, data.get('volume'))
    _send_close_sms(request.user, res['trade'], res['net'], res['closed'].status)
    return JsonResponse(_close_response(res))


@login_required
@require_POST
@trade_api
def close_partial_trade_api(request, trade_id):
    data = _json_body(request)
    if data.get('volume') in (None, ''):
        raise TradeError("Set the volume to close")
    res = mt5.close_position(request.user, trade_id, data.get('volume'))
    return JsonResponse(_close_response(res))


@login_required
@csrf_exempt          # older pages post here without a CSRF token
@require_POST
@trade_api
def close_all_trades_api(request):
    """
    Bulk operations. Body: {mode: all|losing|profitable|buy|sell, symbol?}
    or the older form {trades: {id: …}} to close exactly those positions.
    """
    data = _json_body(request)
    qs = Trade.objects.filter(user=request.user, status='OPEN',
                              direction__in=TRADE_DIRECTIONS).select_related('asset')
    if data.get('symbol'):
        qs = qs.filter(asset__symbol=str(data['symbol']).upper())
    ids = [int(i) for i in (data.get('trades') or {}).keys() if str(i).isdigit()]
    mode = (data.get('mode') or 'all').lower()

    targets = []
    for t in qs:
        if ids and t.id not in ids:
            continue
        if mode in ('buy', 'sell') and t.direction != mode.upper():
            continue
        if mode in ('losing', 'profitable'):
            s = mt5.position_snapshot(t)
            total = s['profit'] + s['swap']
            if (mode == 'losing' and total >= 0) or (mode == 'profitable' and total <= 0):
                continue
        targets.append(t.id)
    if not targets:
        raise TradeError("No positions to close")

    total_pnl, won, lost, errors, balance = Decimal('0'), 0, 0, [], None
    for tid in targets:
        try:
            res = mt5.close_position(request.user, tid)
            total_pnl += res['net']
            balance = res['balance']
            if res['closed'].status == 'WON':
                won += 1
            else:
                lost += 1
        except TradeError as e:
            errors.append({'id': tid, 'error': str(e)})
    if balance is None:
        balance = mt5.get_metrics(request.user)['balance']
    return JsonResponse({
        'success': won + lost > 0,
        'closed_count': won + lost,
        'total_pnl': float(total_pnl),
        'won': won,
        'lost': lost,
        'errors': errors,
        'error': errors[0]['error'] if errors and not (won + lost) else None,
        'new_balance': float(balance),
    })


@login_required
@require_POST
@trade_api
def close_by_api(request):
    """Close By. Body: {position_id, by_id}"""
    data = _json_body(request)
    res = mt5.close_by(request.user, data.get('position_id'), data.get('by_id'))
    return JsonResponse({'success': True, 'volume': float(res['volume']), 'pnl': float(res['net']),
                         'new_balance': float(res['balance'])})


@login_required
@require_POST
@trade_api
def modify_trade_api(request, trade_id):
    """Modify position. Body: {sl?, tp?, trailing?} — null removes, an omitted key is left unchanged."""
    data = _json_body(request)
    t = mt5.modify_position(
        request.user, trade_id,
        sl=data['sl'] if 'sl' in data else 'keep',
        tp=data['tp'] if 'tp' in data else 'keep',
        trailing=data['trailing'] if 'trailing' in data else 'keep',
    )
    return JsonResponse({'success': True, 'id': t.id, 'sl': _f(t.stop_loss), 'tp': _f(t.take_profit),
                         'trailing': t.trailing_points or 0})


# ═══════════════════════════════════════════════════════════════
# PENDING ORDERS
# ═══════════════════════════════════════════════════════════════
def _order_dict(o):
    return {
        'id': o.id,
        'ticket': mt5.PENDING_TICKET_OFFSET + o.id,
        'symbol': o.symbol,
        'order_type': o.order_type,
        'type': o.get_order_type_display().lower(),
        'is_buy': o.order_type.startswith('BUY'),
        'volume': float(o.volume),
        'price': float(o.price),
        'stoplimit': _f(o.stoplimit_price),
        'sl': _f(o.stop_loss),
        'tp': _f(o.take_profit),
        'expiration': o.expiration_type,
        'expires_at': o.expires_at.isoformat() if o.expires_at else None,
        'created_at': o.created_at.isoformat() if o.created_at else None,
        'created': _fmt_time(o.created_at),
    }


@login_required
def api_mt5_orders(request):
    qs = PendingOrder.objects.filter(user=request.user, status='PENDING').order_by('-created_at')
    return JsonResponse({'success': True, 'orders': [_order_dict(o) for o in qs]})


def _pending_args(data):
    return dict(price=data.get('price'), stoplimit=data.get('stoplimit'), sl=data.get('sl'), tp=data.get('tp'),
                expiration=data.get('expiration') or 'GTC', expires_at=data.get('expires_at'))


@login_required
@require_POST
@trade_api
def api_place_pending_order(request):
    """Body: {symbol, order_type, volume, price, stoplimit?, sl?, tp?, expiration: GTC|DAY|SPECIFIED, expires_at?}"""
    data = _json_body(request)
    _require_kyc(request.user)
    o = mt5.place_pending(request.user, data.get('symbol'), data.get('order_type'), data.get('volume'),
                          **_pending_args(data))
    return JsonResponse({'success': True, 'order': _order_dict(o)})


@login_required
@require_POST
@trade_api
def api_modify_pending_order(request, order_id):
    data = _json_body(request)
    o = mt5.modify_pending(request.user, order_id, **_pending_args(data))
    return JsonResponse({'success': True, 'order': _order_dict(o)})


@login_required
@require_POST
@trade_api
def api_cancel_pending_order(request, order_id):
    mt5.cancel_pending(request.user, order_id)
    return JsonResponse({'success': True})


@login_required
@require_POST
@trade_api
def api_cancel_all_pending_orders(request):
    n = 0
    for oid in PendingOrder.objects.filter(user=request.user, status='PENDING').values_list('id', flat=True):
        try:
            mt5.cancel_pending(request.user, oid)
            n += 1
        except TradeError:
            pass
    return JsonResponse({'success': True, 'cancelled': n})


# ═══════════════════════════════════════════════════════════════
# HISTORY  (Positions / Orders / Deals + summary)
# ═══════════════════════════════════════════════════════════════
def _history_range(request):
    now = timezone.now()
    period = (request.GET.get('period') or 'month').lower()
    start, end = None, now
    if period == 'today':
        start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'week':
        start = now - timedelta(days=7)
    elif period == 'month':
        start = now - timedelta(days=30)
    elif period == '3months':
        start = now - timedelta(days=90)
    elif period == 'custom':
        def day(key):
            try:
                return timezone.make_aware(datetime.strptime(request.GET.get(key, ''), '%Y-%m-%d'))
            except ValueError:
                return None
        start = day('from')
        to = day('to')
        end = (to + timedelta(days=1)) if to else now
    return start, end


def _in_range(dt, start, end):
    return dt is not None and (start is None or dt >= start) and dt <= end


@login_required
def api_mt5_history(request):
    user = request.user
    start, end = _history_range(request)

    rows = list(
        Trade.objects.filter(user=user, direction__in=TRADE_DIRECTIONS)
        .exclude(status__in=('PENDING', 'CANCELLED'))
        .select_related('asset')
    )
    roots = {t.id: t for t in rows if t.parent_id is None}
    slices = {}
    for t in rows:
        if t.parent_id:
            slices.setdefault(t.parent_id, []).append(t)
    filled_from = {o.trade_id: o for o in PendingOrder.objects.filter(user=user, status='FILLED', trade__isnull=False)}

    positions, deals, orders = [], [], []
    for root in roots.values():
        parts = slices.get(root.id, [])
        sym = root.asset.symbol if root.asset else ''
        ticket = mt5.TICKET_OFFSET + root.id
        is_buy = root.direction == 'BUY'
        volume_in = Decimal(str(root.stake)) + sum((Decimal(str(p.stake)) for p in parts), Decimal('0'))
        comm_in = Decimal(str(root.commission or 0)) + sum((Decimal(str(p.commission or 0)) for p in parts), Decimal('0'))

        # IN deal + the order that opened it
        if _in_range(root.date_opened, start, end):
            deals.append({
                'ticket': ticket * 2, 'order': ticket, 'position': ticket, 'symbol': sym,
                'direction': 'buy' if is_buy else 'sell', 'entry': 'in', 'volume': float(volume_in),
                'price': float(root.entry_price or 0), 'time': _fmt_time(root.date_opened),
                'profit': 0.0, 'swap': 0.0, 'commission': float(comm_in), 'reason': '',
                '_ts': root.date_opened,
            })
            po = filled_from.get(root.id)
            orders.append({
                'ticket': (mt5.PENDING_TICKET_OFFSET + po.id) if po else ticket, 'symbol': sym,
                'type': po.get_order_type_display().lower() if po else ('buy' if is_buy else 'sell'),
                'volume': float(volume_in), 'price': float(po.price if po else root.entry_price or 0),
                'state': 'Filled', 'time_setup': _fmt_time(po.created_at if po else root.date_opened),
                'time_done': _fmt_time(root.date_opened), '_ts': root.date_opened,
            })

        # OUT deals (each partial close + the final close)
        closes = parts + ([root] if root.status != 'OPEN' else [])
        for c in closes:
            if not _in_range(c.date_closed, start, end):
                continue
            c_ticket = mt5.TICKET_OFFSET + c.id
            deals.append({
                'ticket': c_ticket * 2 + 1, 'order': c_ticket, 'position': ticket, 'symbol': sym,
                'direction': 'sell' if is_buy else 'buy', 'entry': 'out', 'volume': float(c.stake),
                'price': float(c.exit_price or 0), 'time': _fmt_time(c.date_closed),
                'profit': float(c.pnl or 0), 'swap': float(c.swap or 0),
                'commission': float(c.close_commission or 0),
                'reason': mt5.REASON_LABELS.get(c.close_reason or 'CLIENT', c.close_reason),
                '_ts': c.date_closed,
            })
            orders.append({
                'ticket': c_ticket, 'symbol': sym, 'type': 'sell' if is_buy else 'buy',
                'volume': float(c.stake), 'price': float(c.exit_price or 0), 'state': 'Filled',
                'time_setup': _fmt_time(c.date_closed), 'time_done': _fmt_time(c.date_closed),
                '_ts': c.date_closed,
            })

        # Closed position (aggregated over partial closes)
        if root.status != 'OPEN' and _in_range(root.date_closed, start, end):
            exits = [(Decimal(str(c.stake)), Decimal(str(c.exit_price or 0))) for c in closes]
            vol_out = sum((v for v, _ in exits), Decimal('0')) or Decimal('1')
            avg_exit = sum((v * p for v, p in exits), Decimal('0')) / vol_out
            profit = sum((Decimal(str(c.pnl or 0)) for c in closes), Decimal('0'))
            swap = sum((Decimal(str(c.swap or 0)) for c in closes), Decimal('0'))
            comm = comm_in + sum((Decimal(str(c.close_commission or 0)) for c in closes), Decimal('0'))
            positions.append({
                'id': root.id, 'ticket': ticket, 'symbol': sym, 'direction': root.direction,
                'is_buy': is_buy, 'volume': float(volume_in), 'entry': float(root.entry_price or 0),
                'exit': float(round(avg_exit, mt5.SPECS[sym].digits if sym in mt5.SPECS else 5)),
                'pnl': float(profit), 'swap': float(swap), 'commission': float(comm),
                'sl': _f(root.stop_loss), 'tp': _f(root.take_profit), 'status': root.status,
                'reason': mt5.REASON_LABELS.get(root.close_reason or 'CLIENT', root.close_reason),
                'opened_at': _fmt_time(root.date_opened), 'closed_at': _fmt_time(root.date_closed),
                '_ts': root.date_closed,
            })

    # Cancelled / expired / rejected pending orders
    for o in PendingOrder.objects.filter(user=user).exclude(status__in=('PENDING', 'FILLED')):
        ts = o.created_at
        if not _in_range(ts, start, end):
            continue
        orders.append({
            'ticket': mt5.PENDING_TICKET_OFFSET + o.id, 'symbol': o.symbol,
            'type': o.get_order_type_display().lower(), 'volume': float(o.volume), 'price': float(o.price),
            'state': o.get_status_display(), 'time_setup': _fmt_time(o.created_at),
            'time_done': _fmt_time(o.expires_at if o.status == 'EXPIRED' else o.created_at), '_ts': ts,
        })

    # Balance operations on the trading account (transfers in / out)
    deposit = withdrawal = Decimal('0')
    for tx in Transaction.objects.filter(user=user, transaction_type='INTERNAL_TRANSFER'):
        if not _in_range(tx.created_at, start, end):
            continue
        d = (tx.details or '').lower()
        if 'to trading account' in d:
            amount = Decimal(str(tx.amount))
            deposit += amount
        elif 'from trading account' in d:
            amount = -Decimal(str(tx.amount))
            withdrawal += -amount
        else:
            continue
        deals.append({
            'ticket': 0, 'symbol': '', 'direction': 'balance', 'entry': '', 'volume': 0, 'price': 0,
            'time': _fmt_time(tx.created_at), 'profit': float(amount), 'swap': 0.0, 'commission': 0.0,
            'reason': tx.details, '_ts': tx.created_at,
        })

    for lst in (positions, deals, orders):
        lst.sort(key=lambda r: r['_ts'], reverse=True)
        for r in lst:
            r.pop('_ts', None)

    trade_deals = [d for d in deals if d['direction'] != 'balance']
    acc = Account.objects.filter(user=user, account_type='TRADING').first()
    return JsonResponse({
        'success': True,
        'positions': positions[:500],
        'orders': orders[:1000],
        'deals': deals[:1000],
        'summary': {
            'deposit': float(deposit),
            'withdrawal': float(withdrawal),
            'profit': round(sum(d['profit'] for d in trade_deals), 2),
            'swap': round(sum(d['swap'] for d in trade_deals), 2),
            'commission': round(sum(d['commission'] for d in trade_deals), 2),
            'credit': 0.0,
            'balance': float(acc.balance) if acc else 0.0,
        },
    })
