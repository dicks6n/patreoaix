# store/ledger.py
"""
Race-proof balance and status changes.

The site runs on SQLite, where select_for_update() does nothing: two requests at the same moment could both read
the old balance and both spend it. Every money movement therefore goes through ONE conditional UPDATE
("subtract X only if the balance is still ≥ X"), which the database applies atomically on any engine.
Use inside transaction.atomic() so a failure later in the same operation rolls the change back.
"""
from decimal import Decimal

from django.db.models import F
from django.utils import timezone


def debit(model, pk, amount, keep=Decimal('0')):
    """Take `amount` from model(pk).balance if at least amount + keep is there. → True if taken."""
    return model.objects.filter(pk=pk, balance__gte=Decimal(amount) + Decimal(keep)) \
        .update(balance=F('balance') - Decimal(amount), updated_at=timezone.now()) == 1


def credit(model, pk, amount):
    model.objects.filter(pk=pk).update(balance=F('balance') + Decimal(amount), updated_at=timezone.now())


def retry_busy(fn):
    """
    SQLite answers "database is locked" when two writers collide. Every money operation here is all-or-nothing
    (and transfers have an idempotency key), so trying again a moment later is safe and saves the customer an
    error page. Not for use inside an outer transaction.
    """
    import functools
    import random
    import time
    from django.db import OperationalError, connection

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except OperationalError as e:
                if 'locked' not in str(e).lower() or attempt == 5 or connection.in_atomic_block:
                    raise
                time.sleep(0.15 * (attempt + 1) + random.random() * 0.2)
    return wrapper
