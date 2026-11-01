"""P2P test mode was removed: close any ads / orders that were placed with demo coins (no real money involved)."""
from django.db import migrations
from django.utils import timezone


def close_demo(apps, schema_editor):
    P2PAd = apps.get_model('store', 'P2PAd')
    P2POrder = apps.get_model('store', 'P2POrder')
    P2POrder.objects.filter(demo=True, status__in=('PENDING', 'PAID', 'APPEAL')).update(
        status='CANCELLED', cancelled_at=timezone.now(), cancel_reason='Test mode removed')
    P2PAd.objects.filter(demo=True).exclude(status='CLOSED').update(status='CLOSED', remaining=0)


class Migration(migrations.Migration):
    dependencies = [('store', '0066_wallet_transfers')]
    operations = [migrations.RunPython(close_demo, migrations.RunPython.noop)]
