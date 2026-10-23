from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db.models import Count
from store.models import Account


class Command(BaseCommand):
    help = 'Fix duplicate user accounts and merge balances'

    def handle(self, *args, **options):
        duplicates = Account.objects.values('user', 'account_type').annotate(
            count=Count('id')
        ).filter(count__gt=1)
        
        if not duplicates:
            self.stdout.write(self.style.SUCCESS("✅ No duplicate accounts found!"))
            return
        
        fixed_count = 0
        
        for dup in duplicates:
            user = User.objects.get(id=dup['user'])
            accounts = Account.objects.filter(
                user=user, 
                account_type=dup['account_type']
            ).order_by('id')
            
            total_balance = sum(acc.balance for acc in accounts)
            
            first_account = accounts.first()
            first_account.balance = total_balance
            first_account.save()
            
            duplicate_ids = [acc.id for acc in accounts[1:]]
            Account.objects.filter(id__in=duplicate_ids).delete()
            
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Fixed {user.username} - {dup['account_type']}: "
                    f"merged {len(accounts)} accounts, balance ${total_balance}"
                )
            )
            fixed_count += 1
        
        self.stdout.write(
            self.style.SUCCESS(f"\n✅ Fixed {fixed_count} users with duplicate accounts")
        )