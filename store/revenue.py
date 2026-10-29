# store/revenue.py
"""
Admin revenue dashboard (/admin/revenue/, staff only): subscriptions, deposits, withdrawals, bonuses, active
users and the growth features (trials, referrals, academy, copy trading, push, price alerts), for a chosen period
compared with the period before it.
"""
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

PERIODS = [(7, '7 days'), (30, '30 days'), (90, '90 days'), (365, '12 months')]
ZERO = Decimal('0')


def _sum(qs, field='amount'):
    return qs.aggregate(s=Sum(field))['s'] or ZERO


def _delta(now, before):
    now, before = float(now or 0), float(before or 0)
    if not before:
        return None
    return round((now - before) / abs(before) * 100)


def _money_block(a, b):
    from .models import Transaction, DepositBonus
    from django.contrib.auth.models import User
    tx = Transaction.objects.filter(created_at__gte=a, created_at__lt=b, status='completed')
    dep = tx.filter(transaction_type='DEPOSIT')
    subs = _sum(tx.filter(transaction_type='SUBSCRIPTION')) - _sum(tx.filter(transaction_type='SUBSCRIPTION_REFUND'))
    wd = _sum(tx.filter(transaction_type='WITHDRAWAL')).copy_abs()
    first_ids = set()
    for uid in dep.values_list('user_id', flat=True).distinct():
        if not Transaction.objects.filter(user_id=uid, transaction_type='DEPOSIT', status='completed', created_at__lt=a).exists():
            first_ids.add(uid)
    deposits = _sum(dep)
    return {
        'subs': subs, 'subs_n': tx.filter(transaction_type='SUBSCRIPTION').count(),
        'deposits': deposits, 'deposits_n': dep.count(), 'depositors': dep.values('user_id').distinct().count(),
        'first_depositors': len(first_ids), 'withdrawals': wd, 'net_deposits': deposits - wd,
        'bonus': _sum(DepositBonus.objects.filter(created_at__gte=a, created_at__lt=b)),
        'bonus_n': DepositBonus.objects.filter(created_at__gte=a, created_at__lt=b).count(),
        'signups': User.objects.filter(date_joined__gte=a, date_joined__lt=b).count(),
        'active': User.objects.filter(last_login__gte=a, last_login__lt=b).count(),
    }


def build(days=30):
    from django.contrib.auth.models import User
    from .models import (Transaction, DepositBonus, UserProfile, Referral, AcademyProgress, CopyTradingSettings,
                         CopyTrade, PushSubscription, PriceWatch, PaymentRequest)
    from .membership import MEMBER_TIERS, TRIAL_TIER
    now = timezone.now()
    a = now - timedelta(days=days)
    cur, prev = _money_block(a, now), _money_block(a - timedelta(days=days), a)
    kpis = [
        {'label': 'Subscription revenue', 'value': cur['subs'], 'money': True, 'delta': _delta(cur['subs'], prev['subs']), 'sub': f"{cur['subs_n']} payments"},
        {'label': 'Deposits', 'value': cur['deposits'], 'money': True, 'delta': _delta(cur['deposits'], prev['deposits']),
         'sub': f"{cur['deposits_n']} from {cur['depositors']} users · {cur['first_depositors']} first-time"},
        {'label': 'Withdrawals', 'value': cur['withdrawals'], 'money': True, 'delta': _delta(cur['withdrawals'], prev['withdrawals']), 'sub': 'completed', 'invert': True},
        {'label': 'Net deposits', 'value': cur['net_deposits'], 'money': True, 'delta': _delta(cur['net_deposits'], prev['net_deposits']), 'sub': 'deposits − withdrawals'},
        {'label': 'Bonuses given', 'value': cur['bonus'], 'money': True, 'delta': _delta(cur['bonus'], prev['bonus']), 'sub': f"{cur['bonus_n']} first-deposit bonuses", 'invert': True},
        {'label': 'New sign-ups', 'value': cur['signups'], 'delta': _delta(cur['signups'], prev['signups']), 'sub': 'accounts created'},
        {'label': 'Active users', 'value': cur['active'], 'delta': _delta(cur['active'], prev['active']), 'sub': 'logged in during the period'},
    ]

    # subscribers right now
    live = UserProfile.objects.filter(is_active_subscription=True).filter(Q(subscription_expires_at__isnull=True) | Q(subscription_expires_at__gt=now))
    tiers = OrderedDict()
    for t, n in live.values_list('subscription_tier').annotate(n=Count('id')).order_by('-n'):
        tiers[str(t or 'free')] = n
    paid = [p for p in live.only('subscription_tier', 'last_renewal_amount') if str(p.subscription_tier).lower() != TRIAL_TIER
            and any(m in str(p.subscription_tier).upper() for m in MEMBER_TIERS)]
    trial_now = live.filter(subscription_tier=TRIAL_TIER).count()
    trial_users = UserProfile.objects.filter(trial_used=True).values_list('user_id', flat=True)
    converted = Transaction.objects.filter(user_id__in=trial_users, transaction_type='SUBSCRIPTION', status='completed').values('user_id').distinct().count()
    tried = UserProfile.objects.filter(trial_used=True).count()
    subs_now = {
        'paid': len(paid), 'trial': trial_now, 'tiers': list(tiers.items()),
        'mrr': sum((p.last_renewal_amount or ZERO) for p in paid),
        'trial_conv': round(converted / tried * 100) if tried else None, 'tried': tried, 'converted': converted,
    }

    # users
    users = {
        'total': User.objects.count(),
        'dau': User.objects.filter(last_login__gte=now - timedelta(days=1)).count(),
        'wau': User.objects.filter(last_login__gte=now - timedelta(days=7)).count(),
        'mau': User.objects.filter(last_login__gte=now - timedelta(days=30)).count(),
    }

    # money owed / waiting
    pending_wd = PaymentRequest.objects.filter(status='PENDING', transaction_type__icontains='WITHDRAW')
    liabilities = {
        'bonus_locked': _sum(DepositBonus.objects.filter(released_at__isnull=True)),
        'bonus_locked_n': DepositBonus.objects.filter(released_at__isnull=True).count(),
        'pending_wd': _sum(pending_wd), 'pending_wd_n': pending_wd.count(),
    }

    # growth features
    month = now - timedelta(days=days)
    growth = [
        ('Referral sign-ups', Referral.objects.filter(created_at__gte=month).count(), 'friends who joined with a link'),
        ('Referral Pro rewards', Referral.objects.filter(pro_days_given=True, pro_days_at__gte=month).count(), 'inviters rewarded'),
        ('Academy graduates', AcademyProgress.objects.filter(lesson='_graduated', passed_at__gte=month).count(), 'finished every lesson'),
        ('Copy trading on', CopyTradingSettings.objects.filter(enabled=True).count(), 'customers right now'),
        ('Copied trades', CopyTrade.objects.filter(created_at__gte=month, status__in=('OPEN', 'CLOSED')).count(), 'opened on Deriv'),
        ('Push devices', PushSubscription.objects.count(), 'phones / browsers'),
        ('Price alerts active', PriceWatch.objects.filter(active=True).count(), 'waiting to fire'),
    ]

    # daily series
    span = min(days, 90)
    start = (now - timedelta(days=span - 1)).date()
    series = OrderedDict((start + timedelta(days=i), {'deposits': 0.0, 'subs': 0.0, 'withdrawals': 0.0, 'signups': 0})
                         for i in range(span))
    tx = Transaction.objects.filter(created_at__date__gte=start, status='completed',
                                    transaction_type__in=('DEPOSIT', 'SUBSCRIPTION', 'WITHDRAWAL'))
    key = {'DEPOSIT': 'deposits', 'SUBSCRIPTION': 'subs', 'WITHDRAWAL': 'withdrawals'}
    for t in tx.only('created_at', 'amount', 'transaction_type'):
        d = timezone.localtime(t.created_at).date()
        if d in series:
            series[d][key[t.transaction_type]] += abs(float(t.amount))
    for j in User.objects.filter(date_joined__date__gte=start).values_list('date_joined', flat=True):
        d = timezone.localtime(j).date()
        if d in series:
            series[d]['signups'] += 1
    daily = [dict(v, day=k) for k, v in series.items()]

    top = list(Transaction.objects.filter(created_at__gte=a, status='completed', transaction_type='DEPOSIT')
               .values('user__username').annotate(total=Sum('amount'), n=Count('id')).order_by('-total')[:10])
    recent = list(Transaction.objects.filter(transaction_type__in=('DEPOSIT', 'SUBSCRIPTION', 'WITHDRAWAL', 'BONUS', 'SUBSCRIPTION_REFUND'))
                  .select_related('user').order_by('-created_at')[:20])
    return {'kpis': kpis, 'subs_now': subs_now, 'users': users, 'liabilities': liabilities, 'growth': growth,
            'daily': daily, 'top': top, 'recent': recent, 'span': span}


def bars(daily, field):
    """Single-series daily bars (rendered as HTML columns): height as % of the period's highest day."""
    vals = [d[field] for d in daily]
    top = max(vals) if vals and max(vals) > 0 else 0
    return {'bars': [{'pct': round(d[field] / top * 100, 1) if top and d[field] else 0, 'v': d[field], 'day': d['day']} for d in daily],
            'max': top, 'total': sum(vals)}
