# store/management/commands/check_margin_levels.py
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from store.mt5_service import check_stop_out


class Command(BaseCommand):
    help = "Check every user's margin level and force-close positions if needed."

    def handle(self, *args, **options):
        users_with_trades = User.objects.filter(
            trade__status='OPEN'
        ).distinct()

        total_closed = 0
        for user in users_with_trades:
            closed = check_stop_out(user)
            if closed:
                total_closed += len(closed)
                self.stdout.write(
                    self.style.WARNING(
                        f"Stop-out: closed {len(closed)} position(s) for {user.username}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(f"Margin check complete. Total closed: {total_closed}")
        )