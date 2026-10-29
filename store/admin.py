from django.contrib import admin
from .models import UserProfile, Asset, Trade, PriceAlert, PriceTick, AITradingStrategy , SurveyReward ,Account




@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'subscription_tier', 'is_active_subscription')
    search_fields = ('user__username', 'user__email', 'subscription_tier')

@admin.register(SurveyReward)
class SurveyRewardAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'amount', 'tier_required', 'is_claimed', 'created_at')
    list_filter = ('tier_required', 'is_claimed')
    search_fields = ('title', 'user__username')

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'account_type', 'balance', 'updated_at')
    list_filter = ('account_type',)
    search_fields = ('user__username',)

@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'name', 'asset_type', 'price', 'change_percent', 'digit_precision')
    list_filter = ('asset_type',)
    search_fields = ('symbol', 'name')


@admin.register(PriceTick)
class PriceTickAdmin(admin.ModelAdmin):
    list_display = ('asset', 'price', 'last_digit', 'timestamp')
    list_filter = ('asset', 'last_digit')
    date_hierarchy = 'timestamp'


@admin.register(AITradingStrategy)
class AITradingStrategyAdmin(admin.ModelAdmin):
    list_display = ('name', 'strategy_type', 'min_confidence', 'is_enabled')


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ('user', 'asset', 'direction', 'stake', 'entry_price', 'status', 'date_opened')
    list_filter = ('status', 'direction')


@admin.register(PriceAlert)
class PriceAlertAdmin(admin.ModelAdmin):
    list_display = ('user', 'asset', 'target_price', 'is_active')

from .models import Account, Transaction  # Ensure these are imported


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'created_at')

# In admin.py
from django.contrib import admin
from .models import PaymentRequest

@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'status', 'processed', 'created_at')
    list_editable = ('status',) # Allows admin to change status directly from the list view

# store/admin.py

from django.contrib import admin
from .models import WebhookEndpoint, WebhookLog

@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'url', 'is_active', 'requests_count', 'success_rate', 'latency')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'url', 'user__username')

@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = ('source', 'is_processed', 'received_at')
    list_filter = ('is_processed', 'source', 'received_at')
    search_fields = ('source', 'payload')

from .models import SiteSettings

@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()  # enforce singleton in the UI too
    def has_delete_permission(self, request, obj=None):
        return False


from .models import KYCProfile

@admin.register(KYCProfile)
class KYCProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'document_type', 'id_number', 'status', 'submitted_at')
    list_filter = ('status', 'document_type')
    search_fields = ('user__username', 'id_number')



from .models import CopyTradeSignal, CopyTradeRedemption, TierCopyTradeLimit, CopyTradeUsage

@admin.register(CopyTradeSignal)
class CopyTradeSignalAdmin(admin.ModelAdmin):
    list_display = ('code', 'title', 'tier_required', 'symbol', 'direction',
                    'lot_size', 'status', 'redemption_count', 'created_at')
    list_filter = ('status', 'tier_required', 'symbol', 'direction')
    search_fields = ('code', 'title', 'symbol')
    readonly_fields = ('created_at',)


@admin.register(CopyTradeRedemption)
class CopyTradeRedemptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'signal', 'status', 'redeemed_at')
    list_filter = ('status', 'redeemed_at')
    search_fields = ('user__username', 'signal__code')


@admin.register(TierCopyTradeLimit)
class TierCopyTradeLimitAdmin(admin.ModelAdmin):
    list_display = ('tier', 'daily_limit')


@admin.register(CopyTradeUsage)
class CopyTradeUsageAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'count')
    list_filter = ('date',)


from .models import ProfitProjection, ProfitProjectionSnapshot

@admin.register(ProfitProjection)
class ProfitProjectionAdmin(admin.ModelAdmin):
    list_display = ('tier', 'daily_yield_percent', 'principal_bonus_percent',
                    'extra_bonus_percent', 'updated_at')
    list_editable = ('daily_yield_percent', 'principal_bonus_percent', 'extra_bonus_percent')


@admin.register(ProfitProjectionSnapshot)
class ProfitProjectionSnapshotAdmin(admin.ModelAdmin):
    list_display = ('user', 'tier', 'top_up', 'starting_balance',
                    'balance_30d', 'balance_90d', 'created_at')
    list_filter = ('tier', 'created_at')
    search_fields = ('user__username',)



from .models import ReferralProfile, Referral, ReferralReward, ReferralTierReward


@admin.register(ReferralProfile)
class ReferralProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'referral_code', 'referred_by', 'total_referrals',
                    'total_earnings', 'pending_earnings', 'withdrawn_earnings', 'created_at')
    search_fields = ('user__username', 'user__email', 'referral_code')
    readonly_fields = ('referral_code', 'created_at')
    list_filter = ('created_at',)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ('referrer', 'referred_user', 'status', 'total_reward', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('referrer__username', 'referred_user__username')


@admin.register(ReferralReward)
class ReferralRewardAdmin(admin.ModelAdmin):
    list_display = ('referral', 'amount', 'tier', 'percent', 'status', 'created_at')
    list_filter = ('status', 'tier', 'created_at')
    list_editable = ('status',)
    search_fields = ('referral__referrer__username', 'referral__referred_user__username')


@admin.register(ReferralTierReward)
class ReferralTierRewardAdmin(admin.ModelAdmin):
    list_display = ('tier', 'reward_percent', 'updated_at')
    list_editable = ('reward_percent',)

# ── PATREOAIX Funded (store/prop_firm.py) ──
from .models import PropAccount, PropPayout, PropTrader


@admin.register(PropTrader)
class PropTraderAdmin(admin.ModelAdmin):
    list_display = ('code', 'user', 'created_at')
    search_fields = ('code', 'user__username', 'user__email')


@admin.register(PropAccount)
class PropAccountAdmin(admin.ModelAdmin):
    list_display = ('login', 'user', 'plan_type', 'size', 'phase', 'status', 'balance', 'price_paid', 'created_at')
    list_filter = ('status', 'phase', 'plan_type', 'size')
    search_fields = ('login', 'user__username', 'order_ref')
    readonly_fields = ('login', 'previous', 'order_ref', 'created_at', 'ended_at')


@admin.register(PropPayout)
class PropPayoutAdmin(admin.ModelAdmin):
    list_display = ('account', 'user', 'profit', 'split', 'amount', 'status', 'created_at')
    list_filter = ('status',)
    actions = ['approve_and_pay', 'reject_payout']

    @admin.action(description='Approve and pay to wallet')
    def approve_and_pay(self, request, queryset):
        from . import prop_firm
        for po in queryset.filter(status='PENDING'):
            prop_firm.pay(po)

    @admin.action(description='Reject (profit goes back to the account)')
    def reject_payout(self, request, queryset):
        from . import prop_firm
        for po in queryset.filter(status='PENDING'):
            prop_firm.reject(po, 'Rejected by staff')
