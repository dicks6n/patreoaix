import random
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from store.models import Asset, PriceTick, UserProfile


class Command(BaseCommand):
    help = 'Seeds initial asset data and sample ticks'

    def handle(self, *args, **kwargs):
        self.stdout.write("Seeding database...")

        # Create Superuser if missing
        if not User.objects.filter(username='admin').exists():
            admin_user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
            UserProfile.objects.create(user=admin_user, subscription_tier='pro')
            self.stdout.write(self.style.SUCCESS("Admin user created (username: admin, password: admin123)"))

        sample_assets = [
            ("BTC", "Bitcoin", "crypto", 64500.25, 2.45, 2),
            ("ETH", "Ethereum", "crypto", 3450.80, -1.20, 2),
            ("EURUSD", "EUR/USD Forex", "forex", 1.0854, 0.15, 4),
            ("R_100", "Volatility 100 Index", "synthetic", 1250.75, 0.85, 2),
            ("R_75", "Volatility 75 Index", "synthetic", 8920.40, -0.45, 2),
        ]

        for symbol, name, asset_type, price, change, precision in sample_assets:
            asset, _ = Asset.objects.get_or_create(
                symbol=symbol,
                defaults={
                    'name': name,
                    'asset_type': asset_type,
                    'price': price,
                    'change_percent': change,
                    'digit_precision': precision
                }
            )

            # Generate initial tick history
            curr_price = float(price)
            for _ in range(50):
                curr_price += random.choice([-0.25, 0.25, -0.10, 0.10, 0.05])
                PriceTick.objects.create(
                    asset=asset,
                    price=round(curr_price, precision)
                )

        self.stdout.write(self.style.SUCCESS("Data seeded successfully!"))