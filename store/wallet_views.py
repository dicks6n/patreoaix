# store/wallet_views.py
"""Wallet hub (/wallet/): every balance and every money action in one place, plus user-to-user transfers."""
import secrets
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render

from . import p2p, transfers
from .models import Account, PropAccount, Transaction

WALLET_TYPES = ('DEPOSIT', 'WITHDRAWAL', 'BONUS', 'SUBSCRIPTION', 'SUBSCRIPTION_REFUND', 'CHALLENGE_FEE', 'ADJUSTMENT',
                'REFERRAL_PAYOUT', 'TRANSFER_IN', 'TRANSFER_OUT', 'P2P_BUY', 'P2P_SELL', 'P2P_ESCROW', 'P2P_REFUND',
                'P2P_FEE', 'INTERNAL_TRANSFER')
CREDIT = ('DEPOSIT', 'BONUS', 'SUBSCRIPTION_REFUND', 'ADJUSTMENT', 'REFERRAL_PAYOUT', 'TRANSFER_IN', 'P2P_BUY', 'P2P_REFUND')
LABELS = {'TRANSFER_IN': 'Received', 'TRANSFER_OUT': 'Sent', 'P2P_BUY': 'P2P buy', 'P2P_SELL': 'P2P sell',
          'P2P_ESCROW': 'P2P escrow', 'P2P_REFUND': 'P2P returned', 'P2P_FEE': 'P2P fee', 'CHALLENGE_FEE': 'Challenge',
          'INTERNAL_TRANSFER': 'Account transfer', 'SUBSCRIPTION_REFUND': 'Refund', 'REFERRAL_PAYOUT': 'Referral'}


def _bal(user, kind):
    a = Account.objects.filter(user=user, account_type=kind).first()
    return a.balance if a else Decimal('0')


@login_required
def wallet(request):
    u = request.user
    from .models import P2POrder
    from django.db.models import Q
    rows = list(Transaction.objects.filter(user=u, transaction_type__in=WALLET_TYPES).order_by('-created_at')[:25])
    for t in rows:
        t.label = LABELS.get(t.transaction_type, t.transaction_type.replace('_', ' ').title())
        t.credit = t.transaction_type in CREDIT or (t.transaction_type == 'INTERNAL_TRANSFER' and 'from trading' in (t.details or '').lower())
    live = p2p.live()
    p2p_bals = [(a, p2p.fmt(a, b)) for a, b in p2p.balances(u).items()]
    return render(request, 'store/wallet.html', {
        'wallet_bal': _bal(u, 'EXCHANGE'), 'trading_bal': _bal(u, 'TRADING'), 'available': transfers.available(u),
        'bonus_locked': transfers._bonus_locked(u), 'p2p_bals': p2p_bals, 'p2p_live': live,
        'props': PropAccount.objects.filter(user=u, status='ACTIVE').count(),
        'p2p_open': P2POrder.objects.filter(Q(buyer=u) | Q(seller=u), status__in=('PENDING', 'PAID', 'APPEAL')).count(),
        'rows': rows, 'sent': transfers.history(u, 8), 'trader_id': _trader_id(u),
    })


def _trader_id(user):
    from .models import PropTrader
    t = PropTrader.objects.filter(user=user).first()
    return t.code if t else ''


@login_required
def send(request):
    u = request.user
    if request.method == 'POST':
        try:
            t = transfers.send(u, request.POST.get('to'), request.POST.get('amount'), request.POST.get('password'),
                               request.POST.get('note', ''), request.POST.get('idem', ''))
            messages.success(request, f'${t.amount:,.2f} sent to @{t.recipient.username} · {t.ref}')
            return redirect('wallet')
        except transfers.TransferError as e:
            messages.error(request, str(e))
            return render(request, 'store/wallet_send.html', _send_ctx(request, request.POST))
    return render(request, 'store/wallet_send.html', _send_ctx(request, {'to': request.GET.get('to', '')}))


def _send_ctx(request, data):
    u = request.user
    lim = transfers.limits()
    kyc = transfers.kyc_ok(u)
    daily = lim['daily'] if kyc else lim['no_kyc_daily']
    return {'available': transfers.available(u), 'bonus_locked': transfers._bonus_locked(u), 'lim': lim, 'kyc': kyc,
            'daily': daily, 'left_today': max(Decimal('0'), daily - transfers.sent_today(u)),
            'idem': secrets.token_hex(12), 'data': data, 'recent': transfers.history(u, 6)}


@login_required
def lookup(request):
    """Recipient preview while typing (masked name + verified badge), never the balance or email."""
    try:
        return JsonResponse({'success': True, **transfers.preview(request.user, request.GET.get('q', ''))})
    except transfers.TransferError as e:
        return JsonResponse({'success': False, 'error': str(e)})
