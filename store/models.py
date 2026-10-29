# ============================================================
# store/models.py  — CLEAN, DEDUPLICATED, PRODUCTION-READY
# ============================================================

import json
import secrets
import string

from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .fields import EncryptedTokenField


# ============================================================
# USER PROFILE
# ============================================================

class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    subscription_tier = models.CharField(max_length=50, default='free')
    is_active_subscription = models.BooleanField(default=False)
    trial_used = models.BooleanField(default=False)          # the free 3-day Pro trial (store/membership.py)
    is_kyc_verified = models.BooleanField(default=False)
    kyc_submitted_at = models.DateTimeField(null=True, blank=True)

    # ✅ SMS / Contact fields
    phone_number = models.CharField(
        max_length=20, blank=True, null=True,
        help_text="E.164 format (e.g. +254712345678)"
    )
    sms_opt_in = models.BooleanField(
        default=True,
        help_text="User consents to receive transactional SMS"
    )

    # ═══════════════════════════════════════════════════════════
    # ✅ NEW: Subscription lifecycle (for pro-rated refunds)
    # ═══════════════════════════════════════════════════════════
    subscription_started_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the current billing cycle started."
    )
    subscription_expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the current billing cycle ends (start + 30 days)."
    )
    last_renewal_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Amount paid for the current billing cycle (USD)."
    )

    def __str__(self):
        return self.user.username

    @property
    def sms_ready(self):
        """True if this user can receive SMS."""
        return bool(self.phone_number and self.sms_opt_in)

    # ═══════════════════════════════════════════════════════════
    # ✅ Subscription helper methods
    # ═══════════════════════════════════════════════════════════
    def total_billing_days(self):
        """Total days in the current billing cycle (defaults to 30)."""
        if not self.subscription_started_at or not self.subscription_expires_at:
            return 30
        delta = self.subscription_expires_at - self.subscription_started_at
        return max(1, delta.days)

    def days_remaining(self):
        """Unused days left in the current billing cycle."""
        if not self.subscription_expires_at:
            return 0
        delta = self.subscription_expires_at - timezone.now()
        return max(0, delta.days)

    def days_used(self):
        """Days already consumed in the current billing cycle."""
        total = self.total_billing_days()
        remaining = self.days_remaining()
        return max(0, total - remaining)

    def calculate_refund(self):
        """
        Pro-rated refund = (days_remaining / total_days) × amount_paid.
        Returns a Decimal rounded to 2 decimal places.
        """
        from decimal import ROUND_HALF_UP

        total = self.total_billing_days()
        remaining = self.days_remaining()
        paid = self.last_renewal_amount or Decimal('0.00')

        if total <= 0 or paid <= 0 or remaining <= 0:
            return Decimal('0.00')

        return (paid * Decimal(remaining) / Decimal(total)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
# ============================================================
# BOTS & STRATEGIES
# ============================================================

class UserBot(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='user_bots')
    name = models.CharField(max_length=100)
    config_json = models.JSONField(default=dict)
    state_json = models.JSONField(default=dict)
    is_running = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.user.username}) - {'Running' if self.is_running else 'Stopped'}"


class TradingStrategy(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='trading_strategies')
    name = models.CharField(max_length=200, default='Untitled Strategy')
    blocks_json = models.TextField(default='[]')
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_deployed = models.BooleanField(default=False)
    total_trades = models.IntegerField(default=0)
    win_rate = models.FloatField(default=0.0)
    total_return = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.user.username} - {self.name}"


# ============================================================
# ASSETS & MARKET DATA
# ============================================================

class Asset(models.Model):
    ASSET_TYPES = [
        ('crypto', 'Crypto'),
        ('forex', 'Forex'),
        ('stock', 'Stock'),
        ('synthetic', 'Synthetic Index')
    ]
    symbol = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    asset_type = models.CharField(max_length=20, choices=ASSET_TYPES, default='crypto')
    price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    change_percent = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    digit_precision = models.IntegerField(default=2)

    def __str__(self):
        return f"{self.symbol} ({self.get_asset_type_display()})"

    @property
    def last_digit(self):
        price_str = f"{self.price:.{self.digit_precision}f}".replace(".", "")
        return int(price_str[-1]) if price_str else 0


class PriceTick(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="ticks")
    price = models.DecimalField(max_digits=18, decimal_places=4)
    last_digit = models.IntegerField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def save(self, *args, **kwargs):
        if self.asset and self.price is not None:
            price_str = f"{self.price:.{self.asset.digit_precision}f}".replace(".", "")
            if price_str:
                self.last_digit = int(price_str[-1])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.asset.symbol} @ {self.price} (Digit: {self.last_digit})"


class AITradingStrategy(models.Model):
    STRATEGY_TYPES = (
        ('EVEN_ODD', 'Even / Odd Probability'),
        ('OVER_UNDER', 'Over / Under Threshold'),
        ('MATCH_DIFF', 'Match / Differ Pattern'),
        ('SENTIMENT_NLP', 'News Sentiment NLP (FinBERT)'),
    )
    name = models.CharField(max_length=100)
    strategy_type = models.CharField(max_length=20, choices=STRATEGY_TYPES)
    min_confidence = models.FloatField(default=70.0)
    is_enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.name


# ============================================================
# TRADING
# ============================================================

class Trade(models.Model):
    DIRECTION_CHOICES = [('BUY', 'Buy'), ('SELL', 'Sell')]
    STATUS_CHOICES = [('OPEN', 'Open'), ('WON', 'Won'), ('LOST', 'Lost'), ('PENDING', 'Pending'), ('CANCELLED', 'Cancelled')]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    asset = models.ForeignKey('Asset', on_delete=models.SET_NULL, null=True, blank=True)
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES, default='BUY')
    stake = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    entry_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    exit_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    target_digit = models.IntegerField(null=True, blank=True)
    stop_loss = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)    # 5-digit forex needs > 4 places
    take_profit = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    payout = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='OPEN')
    date_opened = models.DateTimeField(auto_now_add=True)
    date_closed = models.DateTimeField(null=True, blank=True)

    current_price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    pnl = models.DecimalField(max_digits=20, decimal_places=2, default=0.00)
    last_price_update = models.DateTimeField(null=True, blank=True)

    # ═══════════════════════════════════════════════════════════
    # ✅ MT5 TERMINAL — margin & leverage tracking
    # ═══════════════════════════════════════════════════════════
    margin_used = models.DecimalField(
        max_digits=20, decimal_places=8, default=Decimal('0.00'),
        help_text="Margin locked when this trade was opened."
    )
    leverage = models.PositiveIntegerField(
        default=100,
        help_text="Leverage used at open time (e.g., 100 for 100:1)."
    )
    contract_size = models.DecimalField(
        max_digits=20, decimal_places=4, default=Decimal('1.0'),
        help_text="Contract size for this asset (e.g., 100 oz for XAUUSD)."
    )

    # ═══════════════════════════════════════════════════════════
    # ✅ MT5 TRADE SERVER — swap, commission, partial closes
    # ═══════════════════════════════════════════════════════════
    swap = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('0.00'),
        help_text="Accumulated swap (USD). Goes to the balance when the position closes."
    )
    swap_charged_at = models.DateTimeField(
        null=True, blank=True, help_text="Last rollover the swap was charged for."
    )
    commission = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('0.00'),
        help_text="Entry commission (USD, negative = charged)."
    )
    close_commission = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal('0.00'),
        help_text="Exit commission (USD, negative = charged)."
    )
    close_reason = models.CharField(
        max_length=10, blank=True, default='',
        help_text="CLIENT, SL, TP, SO (stop out) or CLOSE_BY."
    )
    trailing_points = models.PositiveIntegerField(
        null=True, blank=True, help_text="Trailing stop distance in points."
    )
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.CASCADE, related_name='partial_closes',
        help_text="Set on the closed part of a partially closed position."
    )

    def __str__(self):
        return f"{self.user.username} | {self.direction} {self.asset.symbol if self.asset else 'N/A'} | {self.status}"

class PriceAlert(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE)
    target_price = models.DecimalField(max_digits=18, decimal_places=8)
    condition = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} Alert: {self.asset.symbol} @ {self.target_price}"


# ============================================================
# SENTIMENT
# ============================================================

class SentimentSignal(models.Model):
    SIGNAL_CHOICES = [('BUY', 'Bullish / Buy'), ('SELL', 'Bearish / Sell'), ('NEUTRAL', 'Neutral / Hold')]
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="sentiment_signals", null=True, blank=True)
    asset_symbol = models.CharField(max_length=20, default="XAU/USD")
    query = models.CharField(max_length=255)
    composite_score = models.FloatField()
    signal = models.CharField(max_length=10, choices=SIGNAL_CHOICES)
    confidence = models.FloatField(default=0.0)
    articles_analyzed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.asset_symbol} - {self.signal} ({self.composite_score:.2f})"


class NewsArticle(models.Model):
    sentiment_signal = models.ForeignKey(SentimentSignal, on_delete=models.CASCADE, related_name='articles')
    title = models.CharField(max_length=500)
    link = models.URLField(max_length=1000)
    published = models.CharField(max_length=100, blank=True, null=True)
    sentiment = models.CharField(max_length=20)
    polarity = models.FloatField()

    def __str__(self):
        return self.title


# ============================================================
# BROKER
# ============================================================

class UserBrokerAccount(models.Model):
    BROKER_CHOICES = [
        ('DERIV', 'Deriv Account'),
        ('PEPPERSTONE_MT5', 'Pepperstone MetaTrader 5'),
        ('PEPPERSTONE_CTRADER', 'Pepperstone cTrader'),
        ('BINANCE', 'Binance Spot'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='broker_accounts')
    broker = models.CharField(max_length=50, choices=BROKER_CHOICES, default='DERIV')
    account_number = models.CharField(max_length=100)
    server_name = models.CharField(max_length=100, blank=True, null=True, default='Deriv-Server')
    # new-API OAuth access tokens are JWTs, longer than 255 characters; stored encrypted (store/fields.py)
    api_token = EncryptedTokenField(max_length=8192, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    # the customer accepted the real-money risk warning for this (real) account; None = real trading not enabled
    real_enabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    last_known_balance = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    last_balance_currency = models.CharField(max_length=10, default='USD')
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(
        max_length=20, default='PENDING',
        choices=[('OK', 'OK'), ('ERROR', 'Error'), ('PENDING', 'Pending')]
    )
    sync_error_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} - {self.get_broker_display()} ({self.account_number})"

    @property
    def is_stale(self):
        if not self.last_synced_at:
            return True
        from datetime import timedelta
        return (timezone.now() - self.last_synced_at) > timedelta(seconds=60)


class BrokerBalanceSnapshot(models.Model):
    broker_account = models.ForeignKey(UserBrokerAccount, on_delete=models.CASCADE, related_name='balance_snapshots')
    balance = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=10, default='USD')
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']


# ============================================================
# ADMIN APPROVAL MODELS
# ============================================================

class Deposit(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deposits')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=255, unique=True, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Deposit #{self.id} - {self.user.username} (${self.amount})"


class Withdrawal(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawals')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    destination = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Withdrawal #{self.id} - {self.user.username} (${self.amount})"


class KYCVerification(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('verified', 'Verified'), ('rejected', 'Rejected')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='kyc_verifications')
    document_type = models.CharField(max_length=100, default='National ID')
    document_file = models.FileField(upload_to='kyc_documents/', blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"KYC - {self.user.username} ({self.document_type})"


class KYCProfile(models.Model):
    """Primary KYC record — one per user."""
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='kyc_profile')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    document_type = models.CharField(max_length=50, default='National ID')
    id_number = models.CharField(max_length=50, blank=True, null=True)
    document_number = models.CharField(max_length=100, blank=True, null=True)
    document_file = models.FileField(upload_to='kyc_documents/', blank=True, null=True)
    document_image = models.ImageField(upload_to='kyc_documents/', blank=True, null=True)
    selfie_image = models.ImageField(upload_to='kyc_selfies/', blank=True, null=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    # automatic verification (store/kyc_auto.py) and the reviewer's note
    provider = models.CharField(max_length=20, blank=True, default='')          # '' = manual upload, 'didit'
    provider_session = models.CharField(max_length=100, blank=True, default='', db_index=True)
    provider_status = models.CharField(max_length=40, blank=True, default='')
    admin_notes = models.TextField(blank=True, default='')                      # rejection reason shown to the user
    verified_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} - {self.status}"


# ============================================================
# ACCOUNTS & TRANSACTIONS
# ============================================================

class Account(models.Model):
    ACCOUNT_TYPES = (('EXCHANGE', 'Exchange Account'), ('TRADING', 'Trading Account'))
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='accounts')
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES, default='EXCHANGE')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'account_type')

    def __str__(self):
        return f"@{self.user.username} - {self.get_account_type_display()} (${self.balance})"


class Transaction(models.Model):
    TRANSACTION_TYPES = (
        ('DEPOSIT', 'Deposit'),
        ('WITHDRAWAL', 'Withdrawal'),
        ('INTERNAL_TRANSFER', 'Internal Transfer'),
    )

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='completed')
    details = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_notified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} - {self.transaction_type} of {self.amount} ({self.status})"

class FundedAccount(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='funded_accounts')
    initial_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    is_funded = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Funded Account for {self.user.username} - Balance: ${self.balance}"


class FundedTrade(models.Model):
    funded_account = models.ForeignKey('FundedAccount', on_delete=models.CASCADE, related_name='trades')
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    asset = models.CharField(max_length=20, default='XAUUSD')
    direction = models.CharField(max_length=10)
    entry_price = models.DecimalField(max_digits=18, decimal_places=2)
    exit_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    lot_size = models.DecimalField(max_digits=10, decimal_places=2, default=1.00)
    profit_percent = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    profit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, default='PENDING')   # OPEN while running, then WON / LOST
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # AI Gold trades run on real prices: they close when the market touches these levels
    stop_loss = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    close_reason = models.CharField(max_length=20, blank=True, default='')    # SL / TP / TIME / MANUAL
    signal_note = models.CharField(max_length=255, blank=True, default='')    # why the AI took the trade

    def __str__(self):
        return f"{self.user.username if self.user else 'N/A'} - {self.asset} ({self.direction}) - {self.status}"


# ============================================================
# PAYMENTS
# ============================================================

class PaymentRequest(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    NETWORK_CHOICES = (
        ('TRC20', 'USDT TRC-20 (Tron)'),
        ('ERC20', 'USDT ERC-20 (Ethereum)'),
        ('BEP20', 'USDT BEP-20 (BSC)'),
        ('BTC', 'Bitcoin'),
        ('ETH', 'Ethereum'),
        ('OTHER', 'Other'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payment_requests')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=50)
    payment_method = models.CharField(max_length=50)
    provider_reference = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='PENDING')
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # ✅ Blockchain fields
    network = models.CharField(max_length=10, choices=NETWORK_CHOICES, blank=True, null=True)
    tx_hash = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    from_address = models.CharField(max_length=128, blank=True, null=True)
    to_address = models.CharField(max_length=128, blank=True, null=True)

    # ✅ Admin review tracking
    admin_notes = models.TextField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_payments'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    @property
    def details(self):
        if self.tx_hash:
            return f"{self.transaction_type} via {self.payment_method} | TxID: {self.tx_hash[:16]}... | Network: {self.network or 'N/A'}"
        if self.provider_reference:
            return f"{self.transaction_type} via {self.payment_method} (Ref: {self.provider_reference})"
        return f"{self.transaction_type} via {self.payment_method}"

    @property
    def explorer_url(self):
        """Return a block explorer link for the TxID based on the network."""
        if not self.tx_hash:
            return None
        base = {
            'TRC20': 'https://tronscan.org/#/transaction/',
            'ERC20': 'https://etherscan.io/tx/',
            'BEP20': 'https://bscscan.com/tx/',
            'BTC':   'https://blockstream.info/tx/',
            'ETH':   'https://etherscan.io/tx/',
        }.get(self.network)
        return f"{base}{self.tx_hash}" if base else None

    def __str__(self):
        return f"{self.user.username} - {self.transaction_type} ${self.amount} ({self.payment_method})"

class MpesaPayment(models.Model):
    payment_request = models.OneToOneField(PaymentRequest, on_delete=models.CASCADE, related_name='mpesa_details', null=True, blank=True)
    merchant_request_id = models.CharField(max_length=100)
    checkout_request_id = models.CharField(max_length=100)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.TextField(null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    mpesa_receipt_number = models.CharField(max_length=100, blank=True)
    transaction_date = models.DateTimeField(null=True, blank=True)
    phone_number = models.CharField(max_length=15, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Mpesa: {self.mpesa_receipt_number or 'Pending'} ({self.checkout_request_id})"


# ==========================================
# PROFESSIONAL NOTIFICATION SYSTEM
# ==========================================

class Notification(models.Model):
    """
    Rich notification model supporting categories, priority, actions, and read state.
    """
    CATEGORY_CHOICES = [
        ('TRADE', 'Trade'),
        ('FUNDING', 'Funding'),
        ('REFERRAL', 'Referral'),
        ('ACCOUNT', 'Account'),
        ('KYC', 'KYC'),
        ('COPY_TRADE', 'Copy Trade'),
        ('PAYOUT', 'Payout'),
        ('SYSTEM', 'System'),
        ('SECURITY', 'Security'),
    ]

    PRIORITY_CHOICES = [
        ('info', 'Info'),           # Blue — neutral updates
        ('success', 'Success'),     # Green — positive outcome
        ('warning', 'Warning'),     # Amber — needs attention
        ('error', 'Error'),         # Red — critical
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='notifications'
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='SYSTEM')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='info')

    title = models.CharField(max_length=200)
    message = models.TextField()

    # Optional deep-link (e.g., '/trade/', '/funding/', '/referrals/')
    action_url = models.CharField(max_length=255, blank=True, null=True)
    action_label = models.CharField(max_length=50, blank=True, null=True, default='View')

    # Optional metadata (JSON for extra context)
    metadata = models.JSONField(blank=True, null=True)

    # State
    is_read = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)

    # Optional auto-expire (for temporary alerts)
    expires_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read', '-created_at']),
            models.Index(fields=['user', 'category']),
        ]

    def __str__(self):
        return f"[{self.category}] {self.title} → {self.user.username}"

    @property
    def icon(self):
        """FontAwesome icon class based on category."""
        icons = {
            'TRADE': 'fa-chart-line',
            'FUNDING': 'fa-wallet',
            'REFERRAL': 'fa-user-group',
            'ACCOUNT': 'fa-user-circle',
            'KYC': 'fa-id-card',
            'COPY_TRADE': 'fa-bolt',
            'PAYOUT': 'fa-hand-holding-dollar',
            'SYSTEM': 'fa-bell',
            'SECURITY': 'fa-shield-halved',
        }
        return icons.get(self.category, 'fa-bell')

    @property
    def color(self):
        """Tailwind color classes based on priority."""
        colors = {
            'info': 'text-indigo-400 bg-indigo-500/10 border-indigo-500/20',
            'success': 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
            'warning': 'text-amber-400 bg-amber-500/10 border-amber-500/20',
            'error': 'text-rose-400 bg-rose-500/10 border-rose-500/20',
        }
        return colors.get(self.priority, colors['info'])

# ============================================================
# WEBHOOKS
# ============================================================

class Webhook(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=10, choices=[('IN', 'Incoming'), ('OUT', 'Outgoing')])
    url = models.URLField()
    method = models.CharField(max_length=10, default='POST')
    is_active = models.BooleanField(default=True)
    requests_count = models.IntegerField(default=0)
    success_rate = models.FloatField(default=100.0)
    latency = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class WebhookLog(models.Model):
    source = models.CharField(max_length=50, default='Generic')
    payload = models.TextField()
    headers = models.TextField(blank=True, null=True)
    is_processed = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, null=True)
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Webhook [{self.source}] at {self.received_at} - {'Success' if self.is_processed else 'Failed'}"

    def get_json_payload(self):
        try:
            return json.loads(self.payload)
        except json.JSONDecodeError:
            return {}


class WebhookEndpoint(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='webhook_endpoints')
    name = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    is_active = models.BooleanField(default=True)
    requests_count = models.PositiveIntegerField(default=0)
    success_rate = models.DecimalField(max_digits=5, decimal_places=2, default=100.00)
    latency = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({'Active' if self.is_active else 'Inactive'})"


# ============================================================
# SITE SETTINGS
# ============================================================

class SiteSettings(models.Model):
    default_trade_amount = models.DecimalField(max_digits=12, decimal_places=2, default=10.00)
    min_trade_amount = models.DecimalField(max_digits=12, decimal_places=2, default=1.00)
    max_trade_amount = models.DecimalField(max_digits=12, decimal_places=2, default=1000.00)
    payout_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=90.00)
    trading_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return "Site Trading Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BotLog(models.Model):
    LOG_LEVELS = [
        ('INFO', 'Info'),
        ('BUY', 'Buy Signal'),
        ('SELL', 'Sell Signal'),
        ('WARN', 'Warning'),
        ('ERROR', 'Error'),
    ]
    timestamp = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10, choices=LOG_LEVELS, default='INFO')
    message = models.TextField()

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.timestamp.strftime('%H:%M:%S')}] {self.level}: {self.message}"


# ============================================================
# AI TOGGLE & REWARDS
# ============================================================

class ClientAIToggle(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    is_active = models.BooleanField(default=False)
    assigned_lot_size = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    last_trade_date = models.DateField(null=True, blank=True)
    daily_trades_count = models.IntegerField(default=0)
    last_reset_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} - Active: {self.is_active}"

    MAX_DAILY_TRADES = 10

    def can_trade_today(self):
        today = timezone.now().date()
        if self.last_reset_date != today:
            self.daily_trades_count = 0
            self.last_reset_date = today
            self.save()
        return self.daily_trades_count < self.MAX_DAILY_TRADES

    def remaining_trades_today(self):
        today = timezone.now().date()
        if self.last_reset_date != today:
            return self.MAX_DAILY_TRADES
        return max(0, self.MAX_DAILY_TRADES - self.daily_trades_count)

    def increment_trade_count(self):
        today = timezone.now().date()
        if self.last_reset_date != today:
            self.daily_trades_count = 0
            self.last_reset_date = today
        self.daily_trades_count += 1
        self.save()


class SurveyReward(models.Model):
    TIER_CHOICES = [('PRO', 'Pro'), ('VIP', 'VIP')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='survey_rewards')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tier_required = models.CharField(max_length=10, choices=TIER_CHOICES, default='PRO')
    is_claimed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - ${self.amount} ({self.user.username})"


# ============================================================
# RISK / TRADING POSITIONS
# ============================================================

class TradingAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    account_id = models.CharField(max_length=50, unique=True, blank=True, null=True)
    type = models.CharField(max_length=50, default='Live')
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    leverage = models.IntegerField(default=100)
    margin_level = models.FloatField(default=150.0)

    def __str__(self):
        return f'{self.user.username} - {self.account_id or self.id} (${self.balance})'


class TradePosition(models.Model):
    BOOK_CHOICES = [('A_BOOK', 'A-Book'), ('B_BOOK', 'B-Book')]
    STATUS_CHOICES = [('OPEN', 'Open'), ('CLOSED', 'Closed')]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    notional_value = models.DecimalField(max_digits=12, decimal_places=2)
    liability = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    execution_book = models.CharField(max_length=20, choices=BOOK_CHOICES, default='B_BOOK')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    created_at = models.DateTimeField(auto_now_add=True)


class TransactionLog(models.Model):
    STATUS_CHOICES = [('SUCCESS', 'Success'), ('PENDING', 'Pending'), ('DISCREPANCY', 'Discrepancy')]
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    reference_id = models.CharField(max_length=100, unique=True)
    gateway_name = models.CharField(max_length=50)
    transaction_type = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)


# ============================================================
# SUPPORT TICKETS
# ============================================================

class SupportTicket(models.Model):
    CATEGORY_CHOICES = [
        ('AI Signals & Execution', 'AI Signals & Execution'),
        ('Subscription & Billing', 'Subscription & Billing'),
        ('Webhook & Bot Integration', 'Webhook & Bot Integration'),
        ('Account & Security', 'Account & Security'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    status = models.CharField(max_length=50, default='Open')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.category}] {self.subject} ({self.status})"


# ============================================================
# SESSIONS
# ============================================================

class UserSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='user_sessions')
    session_key = models.CharField(max_length=40, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    device_type = models.CharField(max_length=20, default='desktop')
    browser = models.CharField(max_length=50, default='Unknown')
    os_name = models.CharField(max_length=50, default='Unknown')
    location = models.CharField(max_length=100, blank=True, null=True)
    is_current = models.BooleanField(default=False)
    last_activity = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-last_activity']

    def __str__(self):
        return f"{self.user.username} - {self.browser} on {self.os_name}"


# ============================================================
# FUNDED ACCOUNT PAYOUT SYSTEM
# ============================================================

class FundedPayoutRequest(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('REJECTED', 'Rejected'),
    ]

    TIER_PAYOUT_LIMITS = {
        'PRO': Decimal('25.00'),
        'VIP': Decimal('40.00'),
    }

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='funded_payouts')
    funded_account = models.ForeignKey('FundedAccount', on_delete=models.CASCADE, related_name='payout_requests')

    requested_amount = models.DecimalField(max_digits=12, decimal_places=2)
    approved_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payout_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    funded_balance_at_request = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    rejection_reason = models.TextField(blank=True, null=True)

    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='processed_payouts'
    )
    processed_at = models.DateTimeField(null=True, blank=True)

    destination_account = models.CharField(max_length=20, default='EXCHANGE')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    payout_month = models.DateField(help_text="Month this payout is for (first day of month)")

    class Meta:
        ordering = ['-created_at']
        unique_together = ['user', 'payout_month']

    def __str__(self):
        return f"{self.user.username} - ${self.requested_amount} ({self.status}) - {self.payout_month.strftime('%B %Y')}"

    @classmethod
    def get_payout_limit_for_tier(cls, tier):
        return cls.TIER_PAYOUT_LIMITS.get(str(tier).upper(), Decimal('0.00'))

    @classmethod
    def can_user_request_payout(cls, user):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        tier = str(profile.subscription_tier).upper()

        if tier not in ['PRO', 'VIP']:
            return False, "Only Pro and VIP members can request funded account payouts.", Decimal('0.00'), tier

        if not profile.is_active_subscription:
            return False, "Your subscription must be active to request payouts.", Decimal('0.00'), tier

        funded_account, _ = FundedAccount.objects.get_or_create(user=user)

        if not funded_account.is_funded or funded_account.balance <= 0:
            return False, "Your funded account has no balance to withdraw from.", Decimal('0.00'), tier

        current_month = timezone.now().date().replace(day=1)
        existing = cls.objects.filter(user=user, payout_month=current_month).exclude(status='REJECTED').first()

        if existing:
            return False, f"You have already requested a payout for {current_month.strftime('%B %Y')}. Next payout available next month.", Decimal('0.00'), tier

        payout_percentage = cls.get_payout_limit_for_tier(tier)
        max_amount = (funded_account.balance * payout_percentage / 100).quantize(Decimal('0.01'))
        return True, "", max_amount, tier

    def save(self, *args, **kwargs):
        if not self.payout_month:
            self.payout_month = timezone.now().date().replace(day=1)
        super().save(*args, **kwargs)


class FundedPayoutLog(models.Model):
    payout_request = models.ForeignKey(FundedPayoutRequest, on_delete=models.CASCADE, related_name='logs')
    action = models.CharField(max_length=50)
    details = models.TextField(blank=True, null=True)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# ============================================================
# COPY TRADE SYSTEM
# ============================================================

class CopyTradeSignal(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('PAUSED', 'Paused'),
        ('CLOSED', 'Closed'),
        ('EXPIRED', 'Expired'),
    ]
    TIER_CHOICES = [('PRO', 'Pro'), ('VIP', 'VIP'), ('ALL', 'All Tiers')]

    title = models.CharField(max_length=200)
    code = models.CharField(max_length=32, unique=True, db_index=True)
    tier_required = models.CharField(max_length=10, choices=TIER_CHOICES, default='ALL')

    symbol = models.CharField(max_length=20, default='XAUUSD')
    direction = models.CharField(max_length=10, default='BUY')
    lot_size = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.01'))
    entry_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    stop_loss = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    rationale = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ACTIVE')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_copy_signals'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.code} — {self.title} ({self.tier_required})"

    @property
    def is_valid(self):
        if self.status != 'ACTIVE':
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    @property
    def redemption_count(self):
        return self.redemptions.count()


class CopyTradeRedemption(models.Model):
    STATUS_CHOICES = [
        ('EXECUTED', 'Executed'),
        ('FAILED', 'Failed'),
        ('REJECTED', 'Rejected'),
    ]
    signal = models.ForeignKey(CopyTradeSignal, on_delete=models.CASCADE, related_name='redemptions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copy_redemptions')
    trade = models.ForeignKey('Trade', on_delete=models.SET_NULL, null=True, blank=True, related_name='copy_redemptions')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='EXECUTED')
    failure_reason = models.TextField(blank=True, null=True)

    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-redeemed_at']
        unique_together = ['signal', 'user']

    def __str__(self):
        return f"{self.user.username} → {self.signal.code} ({self.status})"


class TierCopyTradeLimit(models.Model):
    tier = models.CharField(max_length=10, unique=True)
    daily_limit = models.PositiveIntegerField(default=5)

    def __str__(self):
        return f"{self.tier} → {self.daily_limit}/day"


class CopyTradeUsage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copy_usage')
    date = models.DateField(default=timezone.now)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ['user', 'date']

    def __str__(self):
        return f"{self.user.username} — {self.date} — {self.count}"


# ============================================================
# PROFIT PROJECTION SYSTEM
# ============================================================

class ProfitProjection(models.Model):
    tier = models.CharField(max_length=10, unique=True)
    daily_yield_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1.78'))
    principal_bonus_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    extra_bonus_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('12.00'))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tier} → {self.daily_yield_percent}%/day"


class ProfitProjectionSnapshot(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profit_projections')
    tier = models.CharField(max_length=10)
    top_up = models.DecimalField(max_digits=18, decimal_places=2)
    principal_bonus = models.DecimalField(max_digits=18, decimal_places=2)
    extra_bonus = models.DecimalField(max_digits=18, decimal_places=2)
    starting_balance = models.DecimalField(max_digits=18, decimal_places=2)

    balance_30d = models.DecimalField(max_digits=18, decimal_places=2)
    balance_60d = models.DecimalField(max_digits=18, decimal_places=2)
    balance_90d = models.DecimalField(max_digits=18, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} ({self.tier}) — ${self.top_up} → ${self.balance_90d}"


# ============================================================
# REFERRAL SYSTEM
# ============================================================

def generate_referral_code():
    """Generate a unique 8-char referral code."""
    while True:
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        if not ReferralProfile.objects.filter(referral_code=code).exists():
            return code


class ReferralProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='referral_profile'
    )
    referral_code = models.CharField(max_length=12, unique=True, db_index=True)
    referred_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='referred_users'
    )

    total_referrals = models.PositiveIntegerField(default=0)
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    pending_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    withdrawn_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} → {self.referral_code}"

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = generate_referral_code()
        super().save(*args, **kwargs)

    @property
    def referral_link(self):
        base = getattr(settings, 'SITE_URL', 'https://patreoaix.com')
        return f"{base}/signup/?ref={self.referral_code}"


class Referral(models.Model):
    STATUS_CHOICES = [('PENDING', 'Pending'), ('ACTIVE', 'Active'), ('EXPIRED', 'Expired')]

    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='referrals_made'
    )
    referred_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='referral_source'
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')

    total_reward = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    reward_paid = models.BooleanField(default=False)
    pro_days_given = models.BooleanField(default=False)    # the inviter's "+7 days of Pro" (store/referral_rewards.py)
    pro_days_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['referrer', 'referred_user']

    def __str__(self):
        return f"{self.referrer.username} → {self.referred_user.username} ({self.status})"


class ReferralReward(models.Model):
    STATUS_CHOICES = [('PENDING', 'Pending'), ('PAID', 'Paid'), ('CANCELLED', 'Cancelled')]

    referral = models.ForeignKey(Referral, on_delete=models.CASCADE, related_name='rewards')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tier = models.CharField(max_length=10)
    percent = models.DecimalField(max_digits=5, decimal_places=2)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"${self.amount} → {self.referral.referrer.username} ({self.status})"


class ReferralTierReward(models.Model):
    tier = models.CharField(max_length=10, unique=True)
    reward_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tier} → {self.reward_percent}%"


# ============================================================
# MODEL SIGNALS  (all signals at the bottom, safe zone)
# ============================================================

@receiver(post_save, sender=User)
def create_referral_profile_on_signup(sender, instance, created, **kwargs):
    """Auto-create ReferralProfile the moment a User is created."""
    if created:
        ReferralProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Transaction)
def update_account_balance(sender, instance, created, **kwargs):
    """
    Route transaction types to the correct account:
    - Trade types (MARGIN_HOLD, TRADE_PAYOUT, TRADE_LOSS, COPY_TRADE, AI_GOLD_TRADE, FUNDED_PAYOUT)
      are ALREADY handled inside views.py on the TRADING account — SKIP them here.
    - Everything else (DEPOSIT, WITHDRAWAL, SUB_*, REFERRAL_PAYOUT, etc.)
      affects the EXCHANGE account.
    """
    if not created:
        return

    # Skip internal transfers — handled explicitly in views
    if instance.transaction_type == 'INTERNAL_TRANSFER':
        return

    # Skip pending transactions (only final ones affect balance)
    if getattr(instance, 'status', 'completed') == 'pending':
        return

    # ✅ Trade-related types are handled in the trading views — do NOT touch balances here
    TRADE_HANDLED_TYPES = {
        'MARGIN_HOLD',
        'TRADE_PAYOUT',
        'TRADE_LOSS',
        'TRADE_STAKE',
        'TRADE_COMMISSION',
        'COPY_TRADE',
        'AI_GOLD_TRADE',
        'FUNDED_PAYOUT',
    }
    if instance.transaction_type in TRADE_HANDLED_TYPES:
        return

    # ═══════════════════════════════════════════════════════════
    # EXCHANGE ACCOUNT OPERATIONS
    # ═══════════════════════════════════════════════════════════
    account, _ = Account.objects.get_or_create(
        user=instance.user,
        account_type='EXCHANGE'
    )

    credit_types = ['DEPOSIT', 'BONUS', 'ADJUSTMENT', 'REFERRAL_PAYOUT']
    debit_types = ['WITHDRAWAL']

    if instance.transaction_type in credit_types or instance.transaction_type.startswith('SUB_'):
        account.balance += instance.amount
    elif instance.transaction_type in debit_types:
        account.balance -= instance.amount
    else:
        return  # Unknown type — do nothing

    account.save()

@receiver(post_save, sender=PaymentRequest)
def handle_payment_approval(sender, instance, **kwargs):
    if instance.status == 'APPROVED' and not instance.processed:
        tier_capital_mapping = {
            'STARTER': 0.00,
            'PRO': 50000.00,
            'VIP': 200000.00,
        }

        purchased_tier = 'PRO'
        details_upper = str(instance.details).upper()
        type_upper = str(instance.transaction_type).upper()

        if 'VIP' in details_upper or 'VIP' in type_upper:
            purchased_tier = 'VIP'
        elif 'PRO' in details_upper or 'PRO' in type_upper:
            purchased_tier = 'PRO'
        elif 'STARTER' in details_upper or 'STARTER' in type_upper:
            purchased_tier = 'STARTER'

        assigned_capital = tier_capital_mapping.get(purchased_tier, 50000.00)

        profile, _ = UserProfile.objects.get_or_create(user=instance.user)
        profile.subscription_tier = purchased_tier.lower()
        profile.is_active_subscription = (purchased_tier != 'STARTER')
        profile.save()

        funded_account, _ = FundedAccount.objects.get_or_create(user=instance.user)
        funded_account.is_funded = (assigned_capital > 0)
        funded_account.initial_balance = assigned_capital
        funded_account.balance = assigned_capital
        funded_account.save()

        # Create subscription transaction log
        Transaction.objects.create(
            user=instance.user,
            amount=instance.amount,
            transaction_type=f'SUB_{purchased_tier}',
            details=f"Subscription Payment - {purchased_tier} Tier (Funded: ${assigned_capital:,.2f})"
        )

        # ✅ Credit referrer
        try:
            from .views import credit_referrer_on_subscription
            credit_referrer_on_subscription(instance.user, instance.amount, purchased_tier)
        except Exception as e:
            print(f"⚠️ Referral credit failed: {e}")

        instance.processed = True
        instance.save()


@receiver(post_save, sender=Transaction)
def first_deposit_bonus(sender, instance, created, **kwargs):
    if created and instance.transaction_type == 'DEPOSIT' and getattr(instance, 'status', 'completed') == 'completed':
        try:
            from .bonus import on_deposit
            on_deposit(instance)
            from .referral_rewards import safe as referral_reward
            referral_reward(instance.user, 'made their first deposit')
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'first-deposit bonus failed: {e}')


@receiver(post_save, sender=Transaction)
def send_transaction_notification(sender, instance, created, **kwargs):
    if created and instance.transaction_type == 'DEPOSIT':
        Notification.objects.create(
            user=instance.user,
            title="Transaction Approved",
            message=f"Your deposit of ${instance.amount} was successful."
        )


# ============================================================
# AI BROKER HELPERS (module-level utilities)
# ============================================================

import logging
import pandas as pd
try:
    import pandas_ta_classic as ta      # maintained fork, same functions: pip install pandas-ta-classic
except ImportError:
    import pandas_ta as ta

logger = logging.getLogger(__name__)

ASSET_UNIVERSE = {
    "commodities": ["XAUUSD", "USOIL"],
    "forex_pairs": ["EURUSD", "GBPUSD", "USDJPY", "EURGBP", "EURJPY", "GBPJPY"],
    "crypto": ["BTCUSD"],
}


def fetch_market_data(symbol, timeframe="1h", limit=100):
    """Placeholder — integrate with your broker API."""
    pass


def calculate_broker_parameters(symbol, current_price):
    if "XAU" in symbol:
        volume, sl_distance, tp_distance, spread = 1.00, 15.00, 30.00, "1.2 pips"
    elif "BTC" in symbol:
        volume, sl_distance, tp_distance, spread = 0.10, 500.00, 1200.00, "12.0 pts"
    elif "OIL" in symbol:
        volume, sl_distance, tp_distance, spread = 1.00, 1.20, 2.50, "0.03 pts"
    else:
        volume, sl_distance, tp_distance, spread = 0.50, 0.0035, 0.0070, "1.0 pip"

    return {
        "volume": volume,
        "sl": round(current_price - sl_distance, 5),
        "tp": round(current_price + tp_distance, 5),
        "spread": spread,
    }


def analyze_market_conditions(df, symbol):
    if df is None or len(df) < 200:
        return "HOLD"

    df["rsi"] = ta.rsi(df["close"], length=14)
    df["sma_50"] = ta.sma(df["close"], length=50)
    df["sma_200"] = ta.sma(df["close"], length=200)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    is_bullish = last["close"] > last["sma_50"] > last["sma_200"]
    is_bearish = last["close"] < last["sma_50"] < last["sma_200"]
    rsi_bullish = prev["rsi"] <= 45 and last["rsi"] > 45
    rsi_bearish = prev["rsi"] >= 55 and last["rsi"] < 55

    if is_bullish and rsi_bullish:
        return "BUY"
    elif is_bearish and rsi_bearish:
        return "SELL"
    return "HOLD"


def execute_broker_order(symbol, order_type, current_price, params):
    logger.info(f"[BROKER] {symbol} {order_type} @ {current_price}")
    return {
        "ticket_id": "#ORD-" + str(pd.Timestamp.now().timestamp())[-6:],
        "symbol": symbol,
        "type": order_type,
        "volume": params["volume"],
        "entry": current_price,
        "sl": params["sl"],
        "tp": params["tp"],
        "status": "FILLED",
    }


def run_daily_ai_gold_engine():
    """Placeholder for scheduled engine run."""
    logger.info("Multi-asset trading engine run started")
    report = {}
    for symbol in list(set(ASSET_UNIVERSE["commodities"] + ASSET_UNIVERSE["forex_pairs"] + ASSET_UNIVERSE["crypto"])):
        try:
            df = fetch_market_data(symbol)
            signal = analyze_market_conditions(df, symbol)
            report[symbol] = signal
        except Exception as e:
            logger.error(f"{symbol}: {e}")
    return report

# ============================================================
# MT5 PENDING ORDERS
# ============================================================

class PendingOrder(models.Model):
    ORDER_TYPES = [
        ('BUY_LIMIT', 'Buy Limit'),
        ('SELL_LIMIT', 'Sell Limit'),
        ('BUY_STOP', 'Buy Stop'),
        ('SELL_STOP', 'Sell Stop'),
        ('BUY_STOP_LIMIT', 'Buy Stop Limit'),
        ('SELL_STOP_LIMIT', 'Sell Stop Limit'),
    ]
    STATUSES = [
        ('PENDING', 'Pending'),
        ('FILLED', 'Filled'),
        ('CANCELLED', 'Cancelled'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
    ]
    EXPIRATION_TYPES = [
        ('GTC', 'Good till cancelled'),
        ('DAY', 'Today'),
        ('SPECIFIED', 'Specified'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='pending_orders')
    symbol = models.CharField(max_length=20)
    order_type = models.CharField(max_length=16, choices=ORDER_TYPES)
    volume = models.DecimalField(max_digits=12, decimal_places=2)
    price = models.DecimalField(max_digits=20, decimal_places=8)
    stoplimit_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True,
        help_text="Limit price a Stop Limit order is placed at once its stop price is reached."
    )
    expiration_type = models.CharField(max_length=10, choices=EXPIRATION_TYPES, default='GTC')
    expires_at = models.DateTimeField(null=True, blank=True)
    stop_loss = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default='PENDING', db_index=True)
    reason = models.CharField(max_length=200, blank=True, default='')
    trade = models.ForeignKey('Trade', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    filled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.order_type} {self.volume} {self.symbol} @ {self.price} [{self.status}]"

# ═══════════════════════════════════════════════════════════════
# AI CONFLUENCE SIGNALS (store/signal_engine.py, manage.py signal_scanner)
# ═══════════════════════════════════════════════════════════════
class SignalAlertSubscription(models.Model):
    """A user's choice to be notified when the background scanner finds a strong setup."""
    STYLES = [('scalp', 'Scalp'), ('intraday', 'Intraday'), ('swing', 'Swing'), ('position', 'Position')]
    GRADES = [('A', 'A and A+'), ('A+', 'A+ only')]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='signal_alerts')
    enabled = models.BooleanField(default=True)
    style = models.CharField(max_length=10, choices=STYLES, default='intraday')
    min_grade = models.CharField(max_length=2, choices=GRADES, default='A')
    symbols = models.JSONField(default=list, blank=True, help_text="Empty = every scanned market")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} alerts {'on' if self.enabled else 'off'} ({self.style}, {self.min_grade}+)"


class ScannerSignal(models.Model):
    """A strong setup the background scanner found, and how it played out on real candles."""
    OUTCOMES = [('OPEN', 'Open'), ('TP', 'Target hit'), ('SL', 'Stop hit'), ('EXPIRED', 'Expired')]

    symbol = models.CharField(max_length=20, db_index=True)
    style = models.CharField(max_length=10)
    timeframe = models.CharField(max_length=5)
    direction = models.CharField(max_length=4)
    grade = models.CharField(max_length=2)
    confluence = models.FloatField()
    quality = models.FloatField()
    entry = models.FloatField()
    stop_loss = models.FloatField()
    take_profit = models.FloatField()
    take_profit_2 = models.FloatField(null=True, blank=True)
    rr = models.FloatField(null=True, blank=True)
    bar_time = models.BigIntegerField(help_text="Open time (epoch) of the closed candle the signal was read on")
    agree = models.JSONField(default=list)
    scanners = models.JSONField(default=list)
    outcome = models.CharField(max_length=8, choices=OUTCOMES, default='OPEN', db_index=True)
    result_r = models.FloatField(null=True, blank=True)
    notified = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.grade} {self.direction} {self.symbol} {self.timeframe} [{self.outcome}]"


class ScannerSnapshot(models.Model):
    """Latest full confluence scan per style, shared by the worker and the web pages."""
    style = models.CharField(max_length=10, unique=True)
    results = models.JSONField(default=list)
    strength = models.JSONField(default=dict)
    source = models.CharField(max_length=10, default='web')      # 'worker' or 'web'
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.style} snapshot ({self.source}, {self.updated_at:%H:%M:%S})"


# ============================================================
# NEWS HISTORY: big economic releases and how the markets moved after them (store/news_history.py)
# ============================================================
class EconEvent(models.Model):
    source_id = models.CharField(max_length=60, unique=True)          # e.g. tv-123456
    key = models.CharField(max_length=20, db_index=True)              # NFP, CPI, FOMC … (news_history.EVENTS)
    title = models.CharField(max_length=160)
    currency = models.CharField(max_length=5, db_index=True)
    when = models.DateTimeField(db_index=True)
    actual = models.FloatField(null=True, blank=True)
    forecast = models.FloatField(null=True, blank=True)
    previous = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=12, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-when']

    def __str__(self):
        return f'{self.key} {self.when:%Y-%m-%d %H:%M} {self.title}'


class NewsReaction(models.Model):
    """How one market moved after one release: % change from the last price before it."""
    event = models.ForeignKey(EconEvent, on_delete=models.CASCADE, related_name='reactions')
    symbol = models.CharField(max_length=12)
    price0 = models.FloatField()
    m15 = models.FloatField(null=True, blank=True)
    h1 = models.FloatField(null=True, blank=True)
    h4 = models.FloatField(null=True, blank=True)
    d1 = models.FloatField(null=True, blank=True)
    up1h = models.FloatField(null=True, blank=True)       # highest point in the first hour, % (≥ 0)
    down1h = models.FloatField(null=True, blank=True)     # lowest point in the first hour, % (≤ 0)
    path = models.JSONField(default=list, blank=True)     # 15-minute closes for 4 h, % from price0
    measured_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('event', 'symbol')


class AILearning(models.Model):
    """What the scanner learned from its own closed signals (store/ai_learning.py), one row per kind."""
    key = models.CharField(max_length=40, unique=True)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class DepositBonus(models.Model):
    """The first-deposit bonus (store/bonus.py). Tradable at once; withdrawable after the trading requirement."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='deposit_bonus')
    deposit = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    required_turnover = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.user} bonus ${self.amount} ({"released" if self.released_at else "locked"})'


# ============================================================
# AI COPY TRADING (store/copy_trading.py)
# ============================================================
class CopyTradingSettings(models.Model):
    """One switch: copy every A+ AI signal onto the customer's Deriv account (demo for now)."""
    ACCOUNTS = [('demo', 'Deriv Demo'), ('real', 'Deriv Real')]
    GRADES = [('A+', 'A+ only'), ('A', 'A and A+')]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copy_trading')
    enabled = models.BooleanField(default=False)
    account = models.CharField(max_length=5, choices=ACCOUNTS, default='demo')
    stake = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('10.00'))
    multiplier = models.PositiveIntegerField(null=True, blank=True, help_text='Empty = picked from the stop distance')
    min_grade = models.CharField(max_length=2, choices=GRADES, default='A+')
    styles = models.JSONField(default=list, blank=True)         # empty = intraday
    symbols = models.JSONField(default=list, blank=True)        # empty = every market Deriv offers
    max_open = models.PositiveIntegerField(default=3)
    max_per_day = models.PositiveIntegerField(default=5)
    enabled_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Copy trading {self.user} ({'on' if self.enabled else 'off'}, {self.account})"


class CopyTrade(models.Model):
    """One AI signal copied (or skipped) onto a customer's Deriv account."""
    STATUSES = [('OPEN', 'Open'), ('CLOSED', 'Closed'), ('FAILED', 'Failed'), ('SKIPPED', 'Skipped')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copy_trades')
    signal = models.ForeignKey('ScannerSignal', on_delete=models.SET_NULL, null=True, blank=True, related_name='copies')
    account = models.CharField(max_length=5, default='demo')
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=4)
    status = models.CharField(max_length=8, choices=STATUSES, default='OPEN', db_index=True)
    contract_id = models.BigIntegerField(null=True, blank=True)
    stake = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    multiplier = models.PositiveIntegerField(null=True, blank=True)
    stop_loss = models.FloatField(null=True, blank=True)        # USD amounts sent to Deriv
    take_profit = models.FloatField(null=True, blank=True)
    profit = models.FloatField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['user', 'signal']

    def __str__(self):
        return f"{self.user} {self.direction} {self.symbol} [{self.status}]"


# ============================================================
# PUSH NOTIFICATIONS (store/push.py) + PRICE ALERTS (store/price_alerts.py)
# ============================================================
class PushSubscription(models.Model):
    """A browser / installed-app push endpoint (Web Push) for one device of a user."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Push {self.user} ({self.user_agent[:40]})"


class PriceWatch(models.Model):
    """'Tell me when gold reaches 4,050': fires once, by push + in-app notification."""
    CONDITIONS = [('above', 'Rises to'), ('below', 'Falls to')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='price_watches')
    symbol = models.CharField(max_length=20, db_index=True)
    condition = models.CharField(max_length=5, choices=CONDITIONS)
    price = models.FloatField()
    note = models.CharField(max_length=140, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    triggered_at = models.DateTimeField(null=True, blank=True)
    triggered_price = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} {self.symbol} {self.condition} {self.price}"


@receiver(post_save, sender=Notification)
def push_new_notification(sender, instance, created, **kwargs):
    """Every in-app notification also goes to the user's phone / desktop (store/push.py; off without VAPID keys)."""
    if not created:
        return
    from .push import enabled, safe_send
    if enabled():
        safe_send(instance.user, instance.title, instance.message, instance.action_url or '/',
                  tag=f'n{instance.pk}')


# ============================================================
# TRADING JOURNAL + WEEKLY AI REVIEW (store/journal.py)
# ============================================================
class JournalEntry(models.Model):
    """One closed trade in the journal: imported from the terminal / copy trading, or added by hand."""
    SOURCES = [('site', 'PATREOAIX terminal'), ('copy', 'AI Copy Trading'), ('manual', 'Other broker')]
    SETUPS = [('ai_signal', 'AI signal'), ('trend', 'Trend follow'), ('pullback', 'Pullback'), ('breakout', 'Breakout'),
              ('reversal', 'Reversal'), ('news', 'News'), ('gut', 'Gut feeling'), ('other', 'Other')]
    EMOTIONS = [('calm', 'Calm'), ('confident', 'Confident'), ('fearful', 'Fearful'), ('greedy', 'Greedy'),
                ('revenge', 'Revenge / angry'), ('bored', 'Bored'), ('fomo', 'FOMO')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='journal_entries')
    source = models.CharField(max_length=6, choices=SOURCES, default='manual')
    trade = models.OneToOneField('Trade', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal')
    copy_trade = models.OneToOneField('CopyTrade', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal')
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=4)
    lots = models.FloatField(null=True, blank=True)
    entry_price = models.FloatField(null=True, blank=True)
    exit_price = models.FloatField(null=True, blank=True)
    had_stop = models.BooleanField(default=True)
    pnl = models.FloatField(default=0)
    close_reason = models.CharField(max_length=10, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(db_index=True)
    setup = models.CharField(max_length=10, choices=SETUPS, blank=True)
    emotion = models.CharField(max_length=10, choices=EMOTIONS, blank=True)
    followed_plan = models.BooleanField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-closed_at']

    def __str__(self):
        return f"{self.user} {self.direction} {self.symbol} {self.pnl:+.2f}"


class WeeklyReview(models.Model):
    """The AI coach's review of one trading week (Monday to Sunday)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='weekly_reviews')
    week_start = models.DateField()
    stats = models.JSONField(default=dict)
    feedback = models.JSONField(default=list)          # [{kind, title, text}]
    focus = models.CharField(max_length=255, blank=True)
    grade = models.CharField(max_length=2, blank=True)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-week_start']
        unique_together = ['user', 'week_start']

    def __str__(self):
        return f"Review {self.user} {self.week_start}"


class AcademyProgress(models.Model):
    """A passed academy lesson (store/academy.py); lesson '_graduated' marks the rewards as given."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='academy_progress')
    lesson = models.CharField(max_length=20)
    score = models.PositiveSmallIntegerField(default=0)
    passed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'lesson']

    def __str__(self):
        return f"{self.user} {self.lesson}"
