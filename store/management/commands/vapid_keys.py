"""python manage.py vapid_keys → the VAPID key pair for Web Push (paste into .env once, then reload the site)."""
import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generate VAPID keys for push notifications'

    def handle(self, *args, **opts):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
        private = b64(key.private_numbers().private_value.to_bytes(32, 'big'))
        public = b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        self.stdout.write('Add these lines to .env (keep the private key secret), then reload the web app and restart run_background:\n')
        self.stdout.write(f'VAPID_PUBLIC_KEY={public}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={private}')
        self.stdout.write('VAPID_EMAIL=support@patreoaix.com')
