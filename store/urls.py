from django.urls import path
from . import views
from . import mt5_views
from . import site_views
from . import pwa
from .membership import members_only, auto_trading_only   # signals: Pro / VIP only · Starter trades manually
from store.views import cryptomus_verify , landing_page_view , connect_ctrader
from rest_framework_simplejwt.views import TokenObtainPairView
from .serializers import CustomTokenObtainPairSerializer
from django.views.generic import RedirectView

#app_name = 'store'

urlpatterns = [
    # Main Dashboard & Home
    path('landing/', views.landing_page_view, name='landing'),
    path('', views.dashboard_view, name='dashboard'),
    # installable web app (Add to Home Screen) — root paths so the service worker controls every page
    path('manifest.webmanifest', pwa.manifest, name='pwa_manifest'),
    path('sw.js', pwa.service_worker, name='pwa_sw'),
    path('offline/', pwa.offline, name='pwa_offline'),
    path('dashboard/', views.landing_page_view, name='landing'),
    path('index/', views.dashboard_view, name='index'),
    path('market-overview/', members_only(views.market_overview_view), name='market_overview'),
    path('posters/', views.signal_posters, name='signal_posters'),
    path('results/', views.public_results, name='public_results'),
    path('results/card.png', views.public_results_png, name='public_results_png'),
    path('api/push/subscribe/', views.api_push_subscribe, name='api_push_subscribe'),
    path('price-alerts/', views.price_alerts_page, name='price_alerts'),
    path('api/price-alert/', views.api_price_alert, name='api_price_alert'),
    path('academy/', views.academy_page, name='academy'),
    path('academy/<slug:key>/', views.academy_lesson, name='academy_lesson'),
    path('copy-trading/', views.copy_trading_page, name='copy_trading'),
    path('api/copy-trading/sync/', views.api_copy_trading_sync, name='api_copy_trading_sync'),
    path('api/copy-trading/close/', views.api_copy_trading_close, name='api_copy_trading_close'),
    path('news-impact/', members_only(views.news_impact, feature='News impact'), name='news_impact'),
    path('subscribe/', views.subscribe_link, name='subscribe'),
    path('pricing/', views.subscribe_link),
    path('privacy/', views.privacy_view, name='privacy'),
    path('terms/', views.terms_view, name='terms'),
    path('tiktok/connect/', views.tiktok_connect, name='tiktok_connect'),
    path('tiktok/callback/', views.tiktok_callback, name='tiktok_callback'),
    path('tiktok/disconnect/', views.tiktok_disconnect, name='tiktok_disconnect'),
    path('posters/tiktok/', views.tiktok_post_poster, name='tiktok_post_poster'),
    path('markets/', views.market_overview, name='markets'),
    path('trade/close/<int:trade_id>/', mt5_views.close_trade_api, name='close_trade_api'),
    path('trade/close-all/', mt5_views.close_all_trades_api, name='close_all_trades_api'),
    path('api/open-positions/', mt5_views.api_open_positions, name='api_open_positions'),
    path('get-market-scan-data/', members_only(views.get_market_scan_data), name='get_market_scan_data'),
    path('api/mt5/metrics/', mt5_views.api_mt5_metrics, name='api_mt5_metrics'),
    path('terminal/', mt5_views.mt5_terminal_view, name='mt5_terminal'),

    path('api/mt5/history/', mt5_views.api_mt5_history, name='api_mt5_history'),

    # MT5 terminal: positions, pending orders, quotes
    path('trade/close-partial/<int:trade_id>/', mt5_views.close_partial_trade_api, name='close_partial_trade_api'),
    path('trade/close-by/', mt5_views.close_by_api, name='close_by_api'),
    path('trade/modify/<int:trade_id>/', mt5_views.modify_trade_api, name='modify_trade_api'),
    path('trade/pending/place/', mt5_views.api_place_pending_order, name='place_pending_order'),
    path('trade/pending/modify/<int:order_id>/', mt5_views.api_modify_pending_order, name='modify_pending_order'),
    path('trade/pending/cancel/<int:order_id>/', mt5_views.api_cancel_pending_order, name='cancel_pending_order'),
    path('trade/pending/cancel-all/', mt5_views.api_cancel_all_pending_orders, name='cancel_all_pending_orders'),
    path('api/mt5/orders/', mt5_views.api_mt5_orders, name='api_mt5_orders'),
    path('api/mt5/quotes/', mt5_views.api_mt5_quotes, name='api_mt5_quotes'),


# ==========================================
# FUNDED ACCOUNT PAYOUT URLS
# ==========================================
    path('funded/payout/', views.funded_payout_view, name='funded_payout'),
    path('funded/payout/request/', views.request_funded_payout, name='request_funded_payout'),
    path('funded/payout/history/', views.payout_history_view, name='payout_history'),

# Admin
    path('admin/payouts/', views.admin_payout_queue_view, name='admin_payout_queue'),



    path('strategy-builder/', views.ai_strategy_builder, name='ai_strategy_builder'),
    path('api/strategy/save/', views.save_strategy_api, name='save_strategy_api'),
    path('api/strategy/load/', views.load_strategy_api, name='load_strategy_api'),
    path('api/strategy/list/', views.list_strategies_api, name='list_strategies_api'),
    path('api/strategy/deploy/', views.deploy_strategy_api, name='deploy_strategy_api'),
    path('api/strategy/backtest/', views.backtest_strategy_api, name='backtest_strategy_api'),
    path('api/pending-orders/', views.api_pending_orders, name='api_pending_orders'),
    path('api/trade-history/', views.api_trade_history, name='api_trade_history'),


    path('copy-trade/', members_only(views.copy_trade_view), name='copy_trade'),
    path('copy-trade/redeem/', members_only(views.redeem_copy_code), name='redeem_copy_code'),
    path('admin/copy-signals/', views.admin_copy_signals_view, name='admin_copy_signals'),


# In urls.py
    path('get-live-prices/', views.get_live_prices, name='get_live_prices'),
    path('api/deriv/ticks/', views.api_deriv_ticks, name='api_deriv_ticks'),
    path('api/deriv/demo/account/', views.api_deriv_demo_account, name='api_deriv_demo_account'),
    path('api/deriv/demo/trade/', views.api_deriv_demo_trade, name='api_deriv_demo_trade'),
    path('api/deriv/demo/multipliers/', views.api_deriv_demo_multipliers, name='api_deriv_demo_multipliers'),
    path('api/deriv/demo/positions/', views.api_deriv_demo_positions, name='api_deriv_demo_positions'),
    path('api/deriv/demo/open/', views.api_deriv_demo_open, name='api_deriv_demo_open'),
    path('api/deriv/demo/close/', views.api_deriv_demo_close, name='api_deriv_demo_close'),
    path('api/deriv/real/enable/', views.api_deriv_real_enable, name='api_deriv_real_enable'),

    path('api/market-status/', views.api_market_status, name='api_market_status'),
    # MetaTrader & Trading Accounts (New Broker Suite)
    path('accounts/', views.trading_accounts_view, name='trading_accounts'),
    path('accounts/leverage/', views.account_leverage_view, name='account_leverage'),
    path('platforms/download/', views.platforms_download_view, name='platforms_download'),
    path('trading-accounts/', views.trading_accounts_view, name='trading_accounts'),

    # KYC & Compliance Suite (New)
    path('kyc/submit/', views.kyc_submit_view, name='kyc_submit'),
    path('kyc/status/', views.kyc_status_view, name='kyc_status'),
    path('kyc/start/', views.kyc_auto_start, name='kyc_auto_start'),
    path('kyc/return/', views.kyc_auto_return, name='kyc_auto_return'),
    path('kyc/webhook/', views.kyc_webhook, name='kyc_webhook'),
# urls.py snippet
    path('admin/messages/', views.admin_message_center_view, name='admin_message_center'),
    path('support/', views.ai_support_view, name='ai_support'),
    
    path('support/ticket/submit/', views.submit_support_ticket, name='submit_support_ticket'),
    # Crypto Wallet, Deposits, Withdrawals & Gateways (Expanded)
    path('wallet/crypto/', views.crypto_wallet_view, name='crypto_wallet'),
    path('wallet/crypto/deposit/', views.crypto_deposit_view, name='crypto_deposit'),
    path('wallet/crypto/withdrawal/', views.crypto_withdrawal_view, name='crypto_withdrawal'),
    path('gateway/callback/', views.gateway_callback_view, name='gateway_callback'),
    path('deposit/', views.request_deposit, name='request_deposit'),
    path('deposit/create/', views.create_nowpayments_payment, name='create_nowpayments_payment'),
    path('deposit/request/', views.request_deposit, name='request_deposit'),
    path('deposit/status/<str:payment_id>/', views.payment_status_view, name='funding_status'),
    path('deposit/crypto/submit/', views.submit_crypto_deposit, name='submit_crypto_deposit'),
    path('funding/withdrawal/', views.request_withdrawal, name='request_withdrawal'),
    path('funding/transfer/', views.internal_transfer, name='internal_transfer'),
    path('transfer-accounts/', views.transfer_between_accounts, name='transfer_between_accounts'),
    path('payments/status/', views.payment_status_view, name='payment_status'),
    path('api/payment-status/<str:checkout_id>/', views.check_payment_status, name='check_payment_status'),
    path('account-operations/funding/payment-methods/deposit', views.funding_deposit, name='funding_deposit'),
    path('account-operations/funding/payment-methods/deposit/mpesa-callback/', views.mpesa_callback, name='mpesa_callback'),

    # Payment Gateways & Webhooks IPN
    path('api/nowpayments/ipn/', views.nowpayments_ipn_webhook, name='nowpayments_ipn_webhook'),
    path('api/webhook/binance/', views.binance_webhook, name='binance_webhook'),
    path('cryptomus_6fcb4880.html', cryptomus_verify),
    # ==========================================
    # Broker Balance API
    # ==========================================
    path('api/broker-balances/', views.api_broker_balances, name='api_broker_balances'),
    path('api/broker/<int:account_id>/sync/', views.sync_single_broker, name='sync_single_broker'),
    # Developer & API Portal (New)
    #path('developer/api/', views.api_dashboard_view, name='api_dashboard'),
   # path('developer/webhooks/', views.webhook_manager_view, name='webhook_manager'),
   # path('developer/docs/', views.api_docs_view, name='api_docs'),
    path('api-integrations/', views.api_integrations, name='api_integrations'),
    path('api/live-breakout-predictions/', members_only(views.get_live_breakout_predictions_api), name='live_breakout_predictions_api'),
    # AI, Signals & Trading Tools
    path('trade/', views.trade_view, name='trade'),
    path('funded/', views.funded_view, name='funded'),
    path('funded/ai-gold/toggle/', views.execute_funded_ai_gold, name='execute_funded_ai_gold'),
    path('api/verify-crypto-txid/', views.verify_crypto_txid, name='verify_crypto_txid'),
    path('api/candles/', views.api_candles, name='api_candles'),
    path('api/candles/', views.api_candles, name='api_candles'),

    # ==========================================
    # Frontend polled endpoints (chart + UI)
    # ==========================================
    path('api/live-prices/', views.api_live_prices, name='api_live_prices'),
    path('api/copy-signals/count/', views.api_copy_signals_count, name='api_copy_signals_count'),
    path('api/public/ticker/', site_views.api_public_ticker, name='api_public_ticker'),
    path('api/notifications/', site_views.api_notifications, name='api_notifications'),
    path('api/notifications/<int:notification_id>/read/', site_views.api_notification_read, name='api_notification_read'),
    path('api/notifications/read-all/', site_views.api_notifications_read_all, name='api_notifications_read_all'),
    path('api/market/overview/', site_views.api_market_overview, name='api_market_overview'),
    path('api/ai-scan/', members_only(site_views.api_ai_scan), name='api_ai_scan'),
    path('api/ai-scan/<str:symbol>/', members_only(site_views.api_ai_scan_detail), name='api_ai_scan_detail'),
    path('api/signals/confluence/', members_only(site_views.api_signal_confluence), name='api_signal_confluence'),
    path('api/signals/alerts/', members_only(site_views.api_signal_alerts), name='api_signal_alerts'),
    path('api/signals/history/', members_only(site_views.api_signal_history), name='api_signal_history'),
    # ═══════════════════════════════════════════════════════════
    # LIVE CHART
    # ═══════════════════════════════════════════════════════════
    path('live-chart/', views.live_chart_view, name='live_chart'),
    path('api/chart/trade/place/', views.place_chart_trade, name='place_chart_trade'),
    path('api/chart/trades/open/', views.list_chart_open_trades, name='list_chart_open_trades'),





    path('digits_trading/', views.digits_trading, name='digits_trading'),
    path('open-trade/', views.open_trade, name='open_trade'),
    path('trade/execute/', mt5_views.execute_trade, name='execute_trade'),
    path('api/analysis/<int:asset_id>/', members_only(views.ai_analysis_api), name='ai_analysis_api'),
    path('live-signals/', members_only(views.live_signals), name='live_signals'),
    path('ai-generator/', members_only(views.ai_signal_generator), name='ai_signal_generator'),
    path('ai-summary/', views.ai_market_summary, name='ai_market_summary'),
    path('prediction-models/', members_only(views.ai_prediction_models), name='ai_prediction_models'),
    path('risk-analyzer/', views.ai_risk_analyzer, name='ai_risk_analyzer'),
    path('trade-journal/', views.journal_page, name='ai_trade_journal'),
    path('trade-journal/old/', views.ai_trade_journal, name='ai_trade_journal_old'),
    path('strategy-builder/', views.ai_strategy_builder, name='ai_strategy_builder'),
    path('run-sentiment/', views.run_sentiment_analysis_view, name='run_sentiment_analysis'),

    # Charts & Analytics
    path('forex-heatmap/', views.forex_heatmap, name='forex_heatmap'),
    path('crypto-heatmap/', views.crypto_heatmap, name='crypto_heatmap'),
    path('trend-scanner/', views.trend_scanner, name='trend_scanner'),
    path('pattern-detection/', views.pattern_detection_view, name='pattern_detection'),
    path('volatility-index/', views.volatility_index, name='volatility_index'),
    path('economic-calendar/', views.economic_calendar, name='economic_calendar'),
    path('news-sentiment/', views.news_sentiment, name='news_sentiment'),
    path('correlation-matrix/', views.correlation_matrix, name='correlation_matrix'),
    path('forex-analytics/', views.forex_analytics, name='forex_analytics'),
    path('crypto-analytics/', views.crypto_analytics, name='crypto_analytics'),
    path('top-gainers-losers/', views.top_gainers_losers, name='top_gainers_losers'),
    path('market-sentiment/', views.market_sentiment, name='market_sentiment'),
    path('liquidity-zones/', views.liquidity_zones, name='liquidity_zones'),
    path('price-action/', views.price_action_monitor, name='price_action_monitor'),

    # Portfolio & Trade History
    path('portfolio/', views.user_trades, name='portfolio'),
    path('portfolio-overview/', views.user_trades, name='my_trades'),
    path('open-positions/', views.open_positions, name='open_positions'),
    path('trade-history/', views.trade_history, name='trade_history'),
    path('history/', views.trade_history, name='trade_history_alt'),
    path('performance/', views.performance_analytics, name='performance_analytics'),
    path('risk-metrics/', views.risk_metrics, name='risk_metrics'),
    path('pl-overview/', views.pl_overview, name='pl_overview'),
    path('forex-pairs/', views.forex_pairs, name='forex_pairs'),
    path('crypto-pairs/', views.crypto_pairs, name='crypto_pairs'),
    
    path('connect-binance/', views.connect_binance, name='connect_binance'),
    path('profile/connect-ctrader/', views.connect_ctrader, name='connect_ctrader'),
    


    # Add to urls.py

    # ... existing URLs ...
    
    # Market Scanner
    path('market-scanner/', members_only(views.market_scanner), name='market_scanner'),
    path('api/market-scan/', members_only(views.get_market_scan_data), name='get_market_scan_data'),
    path('signal/<str:symbol>/', members_only(views.signal_detail_view), name='signal_detail'),

    
    # Automation, Bots & Webhooks
    path('api/token/', TokenObtainPairView.as_view(serializer_class=CustomTokenObtainPairSerializer), name='token_obtain_pair'),

    path('bot/', views.bot_dashboard_view, name='bot'),
    path('bot/control/', auto_trading_only(views.bot_control_view), name='bot_control'),
    path('bot/create/', auto_trading_only(views.create_bot_view), name='create_bot'),
    path('bot-center/', views.bot_center, name='bot_center'),
    path('webhooks/', views.webhook_automations, name='webhooks'),
    path('webhook-automations/', views.webhook_automations, name='webhook_automations'),
    path('webhook-automations/create/', views.create_webhook, name='create_webhook'),
    path('automations/webhooks/', views.webhook_automations_view, name='webhook_automations_view'),
    path('automations/webhooks/<int:webhook_id>/toggle/', views.toggle_webhook_status, name='toggle_webhook'),
    path('api/webhooks/<str:source_name>/', views.handle_incoming_webhook, name='incoming_webhook'),
    path('auto-signal-forwarder/', members_only(views.auto_signal_forwarder), name='auto_signal_forwarder'),
    path('custom-alerts/', views.custom_alerts, name='custom_alerts'),
    path('saved-signals/', members_only(views.saved_signals), name='saved_signals'),

    # Broker & OAuth Connection
    path('connect-deriv/', views.connect_deriv, name='connect_deriv'),
    path('connect-broker/', views.connect_broker, name='connect_broker'),
    path('deriv-callback/', views.deriv_callback, name='deriv_callback'),
    path('connect-deriv/token/', views.connect_deriv_token, name='connect_deriv_token'),
    path('api/broker/<int:account_id>/disconnect/', views.disconnect_broker, name='disconnect_broker'),

    # Rewards & Subscription
    path('rewards/', views.survey_rewards_view, name='survey_rewards'),
    path('rewards/claim/<int:reward_id>/', views.claim_survey_reward, name='claim_survey_reward'),
    path('subscription/', views.subscription_billing, name='subscription_billing'),
    path('subscription/trial/', views.start_trial_view, name='start_trial'),
    path('subscription/cancel/', views.cancel_subscription, name='cancel_subscription'),
    path('subscription/switch/<str:tier>/', views.switch_tier, name='switch_tier'),
    path('referrals/', views.referral_dashboard, name='referral_dashboard'),
    path('referrals/withdraw/', views.withdraw_referral_earnings, name='withdraw_referral_earnings'),
    path('api/referrals/stats/', views.api_referral_stats, name='api_referral_stats'),
    # Settings, Profile & Support
    path('profile/', views.profile, name='profile'),
    path('profile-security/', views.profile_security, name='profile_security'),
    path('notifications/', views.notification_settings, name='notification_settings'),
    path('api/check-notifications/', views.check_notifications, name='check_notifications'),
    path('connected-exchanges/', views.connected_exchanges, name='connected_exchanges'),
    path('help-center/', views.help_center, name='help_center'),


    path('api/change-password/', views.change_password_api, name='change_password_api'),
# urls.py
    # ... existing
    path('api/sessions/', views.api_list_sessions, name='api_list_sessions'),
    path('api/sessions/<int:session_id>/revoke/', views.api_revoke_session, name='api_revoke_session'),
    path('api/sessions/revoke-all/', views.api_revoke_all_other_sessions, name='api_revoke_all_sessions'),
    path('api/change-password/', views.change_password_api, name='change_password_api'),

    
    path('auth/otp/request/', views.request_email_otp_view, name='request_email_otp'),
    path('auth/otp/verify/', views.verify_email_otp_view, name='verify_email_otp'),
    # Admin & Back-Office Extensions
    path('admin/hub/', views.admin_dashboard_hub_view, name='admin_dashboard_hub'),
    path('admin/revenue/', views.admin_revenue, name='admin_revenue'),
    path('admin/hub/counts/', views.api_admin_hub_counts, name='api_admin_hub_counts'),
    path('admin/kyc/', views.admin_kyc_queue_view, name='admin_kyc_queue'),
    path('admin/risk/', views.admin_risk_dashboard_view, name='admin_risk_dashboard'),
    path('admin/gateways/', views.admin_gateway_logs_view, name='admin_gateway_logs'),
   # path('admin-payments/', views.admin_payment_dashboard, name='admin_payment_dashboard'),
    path('admin/kyc/update/<int:pk>/', views.update_kyc_status, name='update_kyc_status'),

   # path('admin/crypto-deposits/verify/', views.api_verify_crypto_tx, name='api_verify_crypto_tx'),
    path('admin-payments/', RedirectView.as_view(url='/admin/hub/', permanent=False), name='admin_payment_dashboard'),

   # path('admin/crypto-deposits/', views.admin_crypto_deposits_view, name='admin_crypto_deposits'),
    # Authentication
    path('login/', views.login_view, name='login'),
    # the login / signup pages post to allauth's addresses: serve them with our own views (phone login,
    # referral codes, wallets created at signup). Listed before allauth, so these win. Google login stays on allauth.
    path('accounts/login/', views.login_view, name='account_login'),
    path('accounts/signup/', views.signup_view, name='account_signup'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('api/ai-scrutiny/<str:symbol>/', views.api_ai_scrutiny, name='api_ai_scrutiny'),
    path('api/news-risk/', views.api_news_risk, name='api_news_risk'),   # if not added yet
    path('api/ai-chat/', views.ai_chat_api, name='ai_chat_api'),
    path('support/', views.ai_support_view, name='ai_support'),
    path('support/ticket/submit/', views.submit_support_ticket, name='submit_support_ticket'),

]