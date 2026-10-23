# store/admin_hub.py
"""
Extra powers for the admin hub (admin_dashboard_hub_view):

  context(request, ctx)   → platform overview, system health, user lookup, support tickets,
                            copy signals, gateway discrepancies, AI scanner, recent activity,
                            funded payouts and the user's balance next to every withdrawal
  handle(request)         → new hub actions (returns a redirect, or None to let the hub handle it):
                            ticket       — change a ticket's status and reply to the user
                            adjust       — credit / debit a user's exchange wallet (ADJUSTMENT
                                           transaction; the Transaction signal moves the balance)
                            notify       — message one user (notification, optional SMS)
                            broadcast    — message every user / a subscription tier
                            copy_toggle  — pause / activate a copy-trade signal
                            user_active  — suspend / reactivate a login
  counts()                → pending counts for the live badges (api_admin_hub_counts)

The existing approve / reject logic for deposits, crypto, withdrawals, KYC and funded
payouts stays in admin_dashboard_hub_view.
"""
import logging
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import DatabaseError, transaction
from django.db.models import Q, Sum, Count
from django.shortcuts import redirect
from django.utils import timezone

from .models import (Account, CopyTradeSignal, FundedPayoutRequest, KYCProfile, Notification, PaymentRequest,
                     SupportTicket, Trade, Transaction, TransactionLog, UserBrokerAccount, UserProfile)

logger = logging.getLogger(__name__)
HUB = 'admin_dashboard_hub'


def _back(request, **params):
    from urllib.parse import urlencode
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get('next')
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)              # e.g. the message center posting a ticket reply
    url = redirect(HUB).url
    keep = {k: v for k, v in params.items() if v}
    tab = request.POST.get('tab')
    if tab:
        keep['tab'] = tab
    return redirect(url + ('?' + urlencode(keep) if keep else ''))


def _sum(qs, field='amount'):
    return qs.aggregate(t=Sum(field))['t'] or Decimal('0.00')


def _sms(user, text):
    try:
        from .sms_service import sms_service
        sms_service.send_to_user(user, text, template='general')
        return True
    except Exception as e:
        logger.warning(f'Admin SMS failed for {user}: {e}')
        return False


# ═══════════════════════════════════════════════════════════════
# ACTIONS
# ═══════════════════════════════════════════════════════════════
def handle(request):
    a = request.POST.get('action_type')
    if a not in ('ticket', 'adjust', 'notify', 'broadcast', 'copy_toggle', 'user_active'):
        return None

    if a == 'ticket':
        t = SupportTicket.objects.filter(id=request.POST.get('item_id')).select_related('user').first()
        if not t:
            messages.error(request, 'Ticket not found.')
            return _back(request)
        status = request.POST.get('status') or t.status
        reply = (request.POST.get('reply') or '').strip()
        t.status = status
        t.save(update_fields=['status'])
        if t.user and (reply or status == 'Resolved'):
            Notification.objects.create(
                user=t.user, title=f"Support: {t.subject[:150]}" + (' — resolved ✅' if status == 'Resolved' else ''),
                message=reply or 'Your ticket has been marked as resolved. Reply on the support page if you still need help.',
                category='SYSTEM', priority='success' if status == 'Resolved' else 'info',
                action_url='/support/', action_label='View ticket')
        messages.success(request, f"Ticket #{t.id} → {status}" + (' and reply sent.' if reply and t.user else '.'))
        return _back(request)

    if a == 'adjust':
        user = User.objects.filter(id=request.POST.get('user_id')).first()
        reason = (request.POST.get('reason') or '').strip()
        try:
            amount = Decimal(str(request.POST.get('amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            amount = None
        if not user or not amount or amount <= 0:
            messages.error(request, 'Pick a user and a positive amount.')
            return _back(request, q=request.POST.get('q'))
        if not reason:
            messages.error(request, 'A reason is required for every balance adjustment (it goes in the audit trail).')
            return _back(request, q=request.POST.get('q'))
        direction = request.POST.get('direction')
        signed = amount if direction == 'credit' else -amount
        with transaction.atomic():
            acc, _ = Account.objects.select_for_update().get_or_create(user=user, account_type='EXCHANGE')
            if signed < 0 and acc.balance < amount:
                messages.error(request, f'@{user.username} has only ${acc.balance:.2f} — cannot debit ${amount:.2f}.')
                return _back(request, q=request.POST.get('q'))
            # The Transaction post_save signal applies ADJUSTMENT to the exchange balance (credit, or debit when negative)
            Transaction.objects.create(user=user, amount=signed, transaction_type='ADJUSTMENT', status='completed',
                                       details=f"Admin {direction} by {request.user.username}: {reason}"[:255])
        acc.refresh_from_db()
        Notification.objects.create(
            user=user, title=f"Balance {'credited' if signed > 0 else 'debited'}: ${amount:.2f}",
            message=f"{reason}\nNew exchange balance: ${acc.balance:.2f}", category='FUNDING',
            priority='success' if signed > 0 else 'warning', action_url='/funding/', action_label='View wallet')
        messages.success(request, f"{'Credited' if signed > 0 else 'Debited'} ${amount:.2f} {'to' if signed > 0 else 'from'} @{user.username}. New balance ${acc.balance:.2f}.")
        return _back(request, q=request.POST.get('q') or user.username)

    if a == 'notify':
        user = User.objects.filter(id=request.POST.get('user_id')).first()
        title, body = (request.POST.get('title') or '').strip(), (request.POST.get('message') or '').strip()
        if not user or not title or not body:
            messages.error(request, 'User, title and message are required.')
            return _back(request, q=request.POST.get('q'))
        Notification.objects.create(user=user, title=title[:200], message=body, category='SYSTEM', priority='info')
        sms = request.POST.get('sms') == 'on' and _sms(user, f"PATREOAIX: {title}\n{body}"[:480])
        messages.success(request, f"Message sent to @{user.username}" + (' (and by SMS).' if sms else '.'))
        return _back(request, q=request.POST.get('q') or user.username)

    if a == 'broadcast':
        title, body = (request.POST.get('title') or '').strip(), (request.POST.get('message') or '').strip()
        audience = request.POST.get('audience', 'all')
        if not title or not body:
            messages.error(request, 'Title and message are required.')
            return _back(request)
        users = User.objects.filter(is_active=True)
        if audience in ('free', 'pro', 'vip', 'starter'):
            users = users.filter(userprofile__subscription_tier__iexact=audience)
        elif audience == 'kyc_pending':
            users = users.filter(kyc_profile__status='PENDING')
        elif audience == 'staff':
            users = users.filter(is_staff=True)
        priority = request.POST.get('priority') if request.POST.get('priority') in ('info', 'success', 'warning', 'error') else 'info'
        n = Notification.objects.bulk_create([Notification(user=u, title=title[:200], message=body, category='SYSTEM', priority=priority,
                                                           action_url=request.POST.get('link') or None) for u in users.only('id')])
        messages.success(request, f'Broadcast sent to {len(n)} user{"s" if len(n) != 1 else ""}.')
        return _back(request)

    if a == 'copy_toggle':
        sig = CopyTradeSignal.objects.filter(id=request.POST.get('item_id')).first()
        if sig:
            sig.status = 'PAUSED' if sig.status == 'ACTIVE' else 'ACTIVE'
            sig.save(update_fields=['status'])
            messages.success(request, f'{sig.code} → {sig.status}')
        return _back(request)

    if a == 'user_active':
        user = User.objects.filter(id=request.POST.get('user_id')).first()
        if not user or user == request.user or user.is_superuser:
            messages.error(request, "You can't suspend yourself or a superuser.")
            return _back(request, q=request.POST.get('q'))
        user.is_active = not user.is_active
        user.save(update_fields=['is_active'])
        messages.success(request, f"@{user.username} {'reactivated' if user.is_active else 'suspended — they can no longer log in'}.")
        return _back(request, q=request.POST.get('q') or user.username)
    return None


# ═══════════════════════════════════════════════════════════════
# READ SIDE
# ═══════════════════════════════════════════════════════════════
def counts():
    return {
        'deposits': PaymentRequest.objects.filter(transaction_type__iexact='Deposit', status='PENDING').count(),
        'withdrawals': PaymentRequest.objects.filter(transaction_type__iexact='Withdrawal', status='PENDING').count(),
        'kyc': KYCProfile.objects.filter(status='PENDING').count(),
        'payouts': FundedPayoutRequest.objects.filter(status='PENDING').count(),
        'tickets': SupportTicket.objects.exclude(status='Resolved').count(),
        'discrepancies': TransactionLog.objects.filter(status='DISCREPANCY').count(),
    }


def overview():
    now = timezone.now()
    d1, d7 = now - timedelta(days=1), now - timedelta(days=7)
    approved = PaymentRequest.objects.filter(status='APPROVED')
    dep, wd = approved.filter(transaction_type__iexact='Deposit'), approved.filter(transaction_type__iexact='Withdrawal')
    pending = PaymentRequest.objects.filter(status='PENDING')
    open_trades = Trade.objects.filter(status='OPEN')
    o = {
        'users_total': User.objects.count(),
        'users_new_24h': User.objects.filter(date_joined__gte=d1).count(),
        'users_new_7d': User.objects.filter(date_joined__gte=d7).count(),
        'users_suspended': User.objects.filter(is_active=False).count(),
        'exchange_total': _sum(Account.objects.filter(account_type='EXCHANGE'), 'balance'),
        'trading_total': _sum(Account.objects.filter(account_type='TRADING'), 'balance'),
        'dep_24h': _sum(dep.filter(reviewed_at__gte=d1)), 'dep_7d': _sum(dep.filter(reviewed_at__gte=d7)),
        'wd_24h': _sum(wd.filter(reviewed_at__gte=d1)), 'wd_7d': _sum(wd.filter(reviewed_at__gte=d7)),
        'pending_in': _sum(pending.filter(transaction_type__iexact='Deposit')),
        'pending_out': _sum(pending.filter(transaction_type__iexact='Withdrawal')),
        'open_trades': open_trades.count(),
        'trades_24h': Trade.objects.filter(date_opened__gte=d1).count(),
        'kyc_approved': KYCProfile.objects.filter(status='APPROVED').count(),
        'copy_active': CopyTradeSignal.objects.filter(status='ACTIVE').count(),
        'brokers_linked': UserBrokerAccount.objects.filter(is_active=True).count(),
        'brokers_errors': UserBrokerAccount.objects.filter(is_active=True, sync_status='ERROR').count(),
    }
    try:
        o['floating_pnl'] = _sum(open_trades, 'pnl')
    except Exception:
        o['floating_pnl'] = None
    o['net_flow_7d'] = o['dep_7d'] - o['wd_7d']
    o.update(counts())
    o['queue_total'] = o['deposits'] + o['withdrawals'] + o['kyc'] + o['payouts'] + o['tickets']
    return o


def health():
    """Plain pass / warn / fail checks an admin can act on."""
    checks = []

    def add(name, state, detail, fix=''):
        checks.append({'name': name, 'state': state, 'detail': detail, 'fix': fix})

    # live prices
    try:
        from . import price_feed
        ages = [time.time() - e['ts'] for e in (price_feed.get_price_entry(s) for s in ('XAUUSD', 'EURUSD', 'BTCUSD')) if e and e.get('ts')]
        if ages and min(ages) < 120:
            add('Live prices', 'ok', f'Freshest quote {int(min(ages))}s old')
        else:
            add('Live prices', 'warn', 'No fresh quote in this web process', 'Start "python manage.py price_tracker" and check the data-feed API keys')
    except Exception as e:
        add('Live prices', 'warn', f'Could not read the price cache ({e.__class__.__name__})')
    # AI scanner worker
    try:
        from .signal_alerts import worker_status
        w = worker_status()
        add('AI signal scanner', 'ok' if w['running'] else 'warn', f"Last pass {w.get('age', '—')}s ago" if w.get('last_run') else 'Never ran',
            '' if w['running'] else 'Start "python manage.py signal_scanner"')
    except Exception:
        add('AI signal scanner', 'warn', 'Signals module not installed or not migrated', 'Install the AI signals update and run migrate')
    # integrations
    add('Deriv app', 'ok' if getattr(settings, 'DERIV_APP_ID', '') else 'fail',
        'DERIV_APP_ID set' if getattr(settings, 'DERIV_APP_ID', '') else 'DERIV_APP_ID missing', 'Add DERIV_APP_ID to settings.py')
    add('Deriv login redirect', 'ok' if getattr(settings, 'DERIV_OAUTH_REDIRECT', '') else 'warn',
        getattr(settings, 'DERIV_OAUTH_REDIRECT', '') or 'Not set (uses this site\'s /deriv-callback/)', 'Register the same URL in your Deriv app')
    feeds = [n for n in ('TWELVEDATA_API_KEY', 'OANDA_API_KEY') if getattr(settings, n, '')]
    add('Forex / gold data keys', 'ok' if feeds else 'warn', ', '.join(feeds) if feeds else 'No TwelveData / OANDA key — forex falls back to Yahoo', 'Add TWELVEDATA_API_KEY')
    add('Email', 'ok' if getattr(settings, 'EMAIL_HOST', '') else 'warn', getattr(settings, 'EMAIL_HOST', '') or 'EMAIL_HOST not set — emails are not delivered', 'Configure EMAIL_HOST / EMAIL_HOST_USER')
    add('Debug mode', 'warn' if settings.DEBUG else 'ok', 'DEBUG is ON — never run production like this' if settings.DEBUG else 'DEBUG off', 'Set DEBUG = False on the live server')
    # data problems
    disc = TransactionLog.objects.filter(status='DISCREPANCY').count()
    add('Gateway ledger', 'fail' if disc else 'ok', f'{disc} discrepanc{"y" if disc == 1 else "ies"}' if disc else 'No discrepancies', 'Open Gateway logs')
    neg = Account.objects.filter(balance__lt=0).count()
    add('Wallet balances', 'fail' if neg else 'ok', f'{neg} account{"s" if neg != 1 else ""} below zero' if neg else 'No negative balances', 'Search the user and adjust')
    stale = PaymentRequest.objects.filter(status='PENDING', created_at__lt=timezone.now() - timedelta(hours=24)).count()
    add('Request backlog', 'warn' if stale else 'ok', f'{stale} request{"s" if stale != 1 else ""} waiting over 24h' if stale else 'Nothing older than 24h')
    return checks


def lookup(q):
    """User 360°: balances, KYC, requests, transactions, trades, brokers, tickets."""
    q = (q or '').strip()
    if not q:
        return None, []
    qs = User.objects.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    digits = ''.join(ch for ch in q if ch.isdigit())
    if len(digits) >= 6:
        qs = qs | User.objects.filter(userprofile__phone_number__endswith=digits[-9:])
    if q.isdigit():
        qs = qs | User.objects.filter(id=int(q))
    matches = list(qs.distinct().order_by('-date_joined')[:8])
    exact = next((u for u in matches if q.lower() in (u.username.lower(), (u.email or '').lower())), None)
    user = exact or (matches[0] if len(matches) == 1 else None)
    if not user:
        return None, matches
    accs = {a.account_type: a.balance for a in Account.objects.filter(user=user)}
    prof = UserProfile.objects.filter(user=user).first()
    kyc = KYCProfile.objects.filter(user=user).first()
    card = {
        'user': user, 'profile': prof, 'kyc': kyc,
        'exchange': accs.get('EXCHANGE', Decimal('0.00')), 'trading': accs.get('TRADING', Decimal('0.00')),
        'requests': PaymentRequest.objects.filter(user=user).order_by('-created_at')[:8],
        'transactions': Transaction.objects.filter(user=user).order_by('-created_at')[:10],
        'open_trades': Trade.objects.filter(user=user, status='OPEN').select_related('asset')[:10],
        'brokers': UserBrokerAccount.objects.filter(user=user, is_active=True),
        'tickets': SupportTicket.objects.filter(user=user).order_by('-created_at')[:5],
        'deposited': _sum(PaymentRequest.objects.filter(user=user, status='APPROVED', transaction_type__iexact='Deposit')),
        'withdrawn': _sum(PaymentRequest.objects.filter(user=user, status='APPROVED', transaction_type__iexact='Withdrawal')),
    }
    return card, matches


def context(request, ctx):
    """Everything the powered-up hub template needs, added to the hub's own context."""
    # balance + affordability next to each pending withdrawal (same check the approve button enforces)
    wds = ctx.get('pending_withdrawals')
    if wds is not None:
        bal = {a.user_id: a.balance for a in Account.objects.filter(account_type='EXCHANGE', user_id__in=[w.user_id for w in wds])}
        for w in wds:
            w.user_balance = bal.get(w.user_id, Decimal('0.00'))
            w.can_pay = w.user_balance >= w.amount
    extra = {
        'ov': overview(),
        'health': health(),
        'tickets_open': SupportTicket.objects.exclude(status='Resolved').select_related('user').order_by('-created_at')[:25],
        'copy_signals': CopyTradeSignal.objects.order_by('-created_at')[:8],
        'discrepancy_logs': TransactionLog.objects.filter(status='DISCREPANCY').select_related('user').order_by('-created_at')[:10],
        'activity': Transaction.objects.select_related('user').order_by('-created_at')[:15],
        'tab': request.GET.get('tab') or 'queues',
        'q': request.GET.get('q', ''),
    }
    extra['health_bad'] = sum(1 for c in extra['health'] if c['state'] != 'ok')
    if extra['q']:
        extra['lookup'], extra['lookup_matches'] = lookup(extra['q'])
    try:
        from .models import ScannerSignal
        from .signal_alerts import history
        rows, stats = history(limit=8)
        extra['ai_signals'], extra['ai_stats'] = rows, stats
    except (ImportError, DatabaseError):
        extra['ai_signals'], extra['ai_stats'] = [], None
    ctx.update(extra)
    return ctx
