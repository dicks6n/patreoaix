# store/fields.py
"""
Encrypted model fields.

EncryptedTokenField stores broker / API tokens encrypted (Fernet, key = TOKEN_ENCRYPTION_KEY in .env) so a
leaked database or backup does not leak the tokens that can trade customers' money. Values are stored as
"enc1:<fernet token>"; older plain values still read normally and are encrypted the next time they are saved
(python manage.py encrypt_tokens does it for every row at once).

Without TOKEN_ENCRYPTION_KEY values are stored as they are (laptop / old setups keep working), and real-money
trading stays off (deriv_demo.real_allowed()).

Make a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Keep it safe: if the key is lost, stored tokens can't be read and customers have to reconnect.
"""
import logging

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)
PREFIX = 'enc1:'


def _fernet():
    key = str(getattr(settings, 'TOKEN_ENCRYPTION_KEY', '') or '').strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def encryption_on():
    try:
        return _fernet() is not None
    except Exception as e:                      # a malformed key
        logger.error(f'TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {e}')
        return False


def encrypt(value):
    if not value or str(value).startswith(PREFIX):
        return value
    f = _fernet()
    return PREFIX + f.encrypt(str(value).encode()).decode() if f else value


def decrypt(value):
    if not value or not str(value).startswith(PREFIX):
        return value
    f = _fernet()
    if not f:
        logger.error('An encrypted token was found but TOKEN_ENCRYPTION_KEY is not set')
        return ''
    try:
        return f.decrypt(str(value)[len(PREFIX):].encode()).decode()
    except Exception:
        logger.error('A stored token could not be decrypted (wrong TOKEN_ENCRYPTION_KEY?)')
        return ''


class EncryptedTokenField(models.CharField):
    """CharField whose value is encrypted in the database and plain in Python."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))
