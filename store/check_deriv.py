"""
check_deriv.py — Show which Deriv account is connected + live balance.
Run: python check_deriv.py
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')
django.setup()

from store.models import UserBrokerAccount
from store.broker_service import broker_service


def main():
    print("=" * 70)
    print("DERIV ACCOUNTS CHECK")
    print("=" * 70)

    accounts = UserBrokerAccount.objects.filter(broker='DERIV').order_by('user__username')

    if not accounts.exists():
        print("\n❌ No Deriv accounts connected.")
        print("\nTo connect one:")
        print("  1. Make sure Django + tunnel are running")
        print("  2. Visit http://127.0.0.1:8000/connect-deriv/")
        print("  3. Log in and authorize")
        return

    for acc in accounts:
        is_demo = acc.account_number.upper().startswith('VRT')
        badge = '🎮 DEMO' if is_demo else '💰 LIVE'

        print(f"\n👤 User:        {acc.user.username}")
        print(f"   Broker:      {acc.get_broker_display()}")
        print(f"   Account:     {acc.account_number}")
        print(f"   Type:        {badge}")
        print(f"   Active:      {'✅ YES' if acc.is_active else '❌ NO'}")
        print(f"   Balance:     {acc.last_known_balance} {acc.last_balance_currency}")
        print(f"   Last sync:   {acc.last_synced_at}")
        print(f"   Sync status: {acc.sync_status}")
        if acc.sync_error_message:
            print(f"   Error:       {acc.sync_error_message[:80]}")

        # Try to force-sync for live balance
        if acc.is_active and acc.api_token:
            print(f"\n   🔄 Force-syncing live balance...")
            try:
                result = broker_service.sync_account(acc)
                if result.get('success'):
                    acc.refresh_from_db()
                    print(f"   ✅ Live balance: {acc.last_known_balance} {acc.last_balance_currency}")
                else:
                    print(f"   ⚠️ Sync failed: {result.get('error')}")
            except Exception as e:
                print(f"   ⚠️ Sync error: {str(e)[:100]}")

        print("-" * 70)

    print("\n✅ Done")


if __name__ == '__main__':
    main()
    