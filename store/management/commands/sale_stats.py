"""
Real platform numbers for the sale pack, straight from the live database (sample P2P traders are left out).

    python manage.py sale_stats
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.utils import timezone


def _safe(fn, default='n/a'):
    try:
        return fn()
    except Exception:
        return default


class Command(BaseCommand):
    help = 'Real platform numbers for the sale pack (sample P2P traders excluded)'

    def handle(self, *a, **o):
        from store import models as m
        from store.p2p import SAMPLE_EMAIL
        now = timezone.now()
        real = User.objects.exclude(email__endswith=SAMPLE_EMAIL).filter(is_staff=False)
        ids = real.values_list('id', flat=True)
        d30, d7 = now - timedelta(days=30), now - timedelta(days=7)
        p2p = m.P2POrder.objects.filter(demo=False, buyer_id__in=ids, seller_id__in=ids)   # both sides real
        p2p_staff = m.P2POrder.objects.filter(demo=False).exclude(
            Q(buyer__email__endswith=SAMPLE_EMAIL) | Q(seller__email__endswith=SAMPLE_EMAIL))
        dep = m.Deposit.objects.filter(user_id__in=ids, status='approved')
        wd = m.Withdrawal.objects.filter(user_id__in=ids, status='approved')
        rows = [
            ('Registered users (excl. staff)', real.count()),
            ('New users, last 30 days', real.filter(date_joined__gte=d30).count()),
            ('Users active, last 7 days (logged in)', real.filter(last_login__gte=d7).count()),
            ('Users active, last 30 days (logged in)', real.filter(last_login__gte=d30).count()),
            ('KYC approved', _safe(lambda: m.KYCProfile.objects.filter(user_id__in=ids, status__iexact='APPROVED').count())),
            ('Paying members now (Pro / VIP)', _safe(lambda: m.UserProfile.objects.filter(
                user_id__in=ids, is_active_subscription=True, subscription_tier__in=['PRO', 'VIP']).count())),
            ('Free trials started (ever)', _safe(lambda: m.UserProfile.objects.filter(user_id__in=ids, trial_used=True).count())),
            ('Approved deposits (count)', dep.count()),
            ('Approved deposits (USD total)', f"{dep.aggregate(s=Sum('amount'))['s'] or 0:,.2f}"),
            ('Approved withdrawals (USD total)', f"{wd.aggregate(s=Sum('amount'))['s'] or 0:,.2f}"),
            ('P2P orders completed (real traders incl. desk)', p2p_staff.filter(status='COMPLETED').count()),
            ('P2P orders completed, both sides customers', p2p.filter(status='COMPLETED').count()),
            ('P2P volume completed (USDT)', f"{p2p_staff.filter(status='COMPLETED', asset='USDT').aggregate(s=Sum('amount'))['s'] or 0:,.2f}"),
            ('Terminal trades placed', _safe(lambda: m.Trade.objects.filter(user_id__in=ids).count())),
            ('Copy trades', _safe(lambda: m.CopyTrade.objects.filter(user_id__in=ids).count())),
            ('AI scanner signals generated, last 30 days', _safe(lambda: m.ScannerSignal.objects.filter(created_at__gte=d30).count())),
            ('Phones with push notifications on', _safe(lambda: m.PushSubscription.objects.filter(user_id__in=ids).count())),
            ('Android app installs registered', _safe(lambda: m.FcmDevice.objects.filter(user_id__in=ids).count())),
            ('Support tickets (ever)', _safe(lambda: m.SupportTicket.objects.filter(user_id__in=ids).count())),
        ]
        self.stdout.write(f'PATREOAIX real numbers, {timezone.localtime(now):%d %b %Y %H:%M}')
        w = max(len(r[0]) for r in rows)
        for k, v in rows:
            self.stdout.write(f'  {k.ljust(w)}  {v}')
        top = real.values('date_joined__year', 'date_joined__month').annotate(c=Count('id')) \
            .order_by('date_joined__year', 'date_joined__month')
        self.stdout.write('\nSign-ups by month:')
        for r in top:
            self.stdout.write(f"  {r['date_joined__year']}-{r['date_joined__month']:02d}  {r['c']}")
