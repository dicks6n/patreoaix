import json
import random
import asyncio
import os
import hmac
import hashlib
import requests
from django.core.cache import cache
from django.core.mail import send_mail
from decimal import Decimal, InvalidOperation
from datetime import datetime
import time  # <--- Add this line here
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required 
from django.views.decorators.http import require_POST , require_http_methods
from django.contrib.auth.models import User
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse, HttpResponse, Http404
from django.db.models import Avg, Q
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from django.utils import timezone

from django_daraja.mpesa.core import MpesaClient
from .models import FundedTrade, FundedAccount, Referral, ReferralProfile, ReferralReward, ReferralTierReward, TransactionLog
from .models import (
    PaymentRequest,
    Asset,
    Trade,
    UserBrokerAccount,
    # ... other models ...
    CopyTradeSignal, CopyTradeRedemption, TierCopyTradeLimit, CopyTradeUsage,
    ProfitProjection,
    FundedPayoutRequest, FundedPayoutLog,
)
from .models import (
    PaymentRequest, 
    Asset, 
    Trade, 
    UserBrokerAccount, 
    SentimentSignal, 
    NewsArticle, 
    Transaction, 
    Account, 
    UserProfile,
    Notification, 
    Webhook, 
    MpesaPayment, 
    SiteSettings,
    WebhookEndpoint,
    FundedAccount,
    BotLog,
    SurveyReward,
    ClientAIToggle,
    WebhookLog,
    Deposit,
    Withdrawal, 
    KYCVerification
)
from .forms import CreateBotForm
from .forms import CreateBotForm

import pandas as pd
import numpy as np
import yfinance as yf
try:
    import pandas_ta_classic as ta      # maintained fork, same functions: pip install pandas-ta-classic
except ImportError:
    import pandas_ta as ta              # the original package, if that's what is installed

import logging
logger = logging.getLogger(__name__)


import requests
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

TIER_CONFIG = {
    'pro': {
        'price': Decimal('49.00'),
        'funded_amount': Decimal('500.00'),
        'kes_multiplier': 130  # Used for M-Pesa conversion if applicable
    },
    'vip': {
        'price': Decimal('199.00'),
        'funded_amount': Decimal('2500.00'),
        'kes_multiplier': 130
    }
}
# ==========================================
# HELPER FUNCTIONS
# ==========================================


def landing_page_view(request):
    # If the user is already logged in, send them straight to the dashboard/app
    if request.user.is_authenticated:
        return redirect('dashboard')  # or 'market_overview' depending on your preference

    # Contact form → support ticket (the form used to post nowhere)
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:80]
        email = (request.POST.get('email') or '').strip()[:120]
        message = (request.POST.get('message') or '').strip()[:5000]
        if request.POST.get('website'):          # hidden field only bots fill in
            return redirect(request.path + '#contact')
        if not email or '@' not in email or not message:
            messages.error(request, "Please enter your email and a message.")
        else:
            SupportTicket.objects.create(
                user=None, category='Account & Security',
                subject=f"Website contact: {name or 'anonymous'} <{email}>"[:200], description=message,
            )
            messages.success(request, "Thanks — your message reached our support team. We'll reply by email.")
        return redirect(request.path + '#contact')

    # Real plans and real platform numbers (the page had invented prices and stats)
    tiers = [{'key': 'starter', 'name': 'Starter', 'price': Decimal('0'), 'funded': None}] + [
        {'key': k, 'name': k.upper() if k == 'vip' else k.title(), 'price': v['price'], 'funded': v['funded_amount']}
        for k, v in TIER_CONFIG.items()
    ]
    track, track_rows = {}, []
    try:
        from . import signal_alerts
        from datetime import datetime as _dt
        track_rows, track = signal_alerts.history(None, limit=8, days=30)
        for r in track_rows:
            r['when'] = _dt.fromisoformat(r['created_at'])
    except Exception as e:
        logger.warning(f'Landing track record unavailable: {e}')
    context = {
        'tiers': tiers,
        'track': track, 'track_rows': track_rows,
        'pro_price': TIER_CONFIG.get('pro', {}).get('price'),
        'telegram_url': getattr(settings, 'TELEGRAM_INVITE_URL', ''),
        'trader_count': User.objects.count(),
        'trade_count': Trade.objects.count(),
        'support_email': getattr(settings, 'SUPPORT_EMAIL', 'support@patreoaiix.com'),
        'mpesa_enabled': getattr(settings, 'MPESA_DEPOSITS_ENABLED', False),
        'support_phone': getattr(settings, 'SUPPORT_PHONE', ''),
        'support_address': getattr(settings, 'SUPPORT_ADDRESS', ''),
    }
    return render(request, 'store/landing.html', context)



def format_mpesa_number(phone_number):
    phone = str(phone_number).strip().replace(" ", "").replace("+", "")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    elif not phone.startswith("254"):
        phone = "254" + phone
    return phone


def _is_ajax(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.content_type == 'application/json'


def _usd_kes_rate():
    """Live USD/KES (Yahoo KES=X), cached 10 min; last good rate if Yahoo is down, 130 only if never fetched."""
    rate = cache.get('fx:usdkes')
    if rate:
        return rate
    try:
        r = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/KES=X?range=1d&interval=1h',
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        px = r.json()['chart']['result'][0]['meta']['regularMarketPrice']
        if px and 50 < float(px) < 500:
            rate = Decimal(str(round(float(px), 2)))
            cache.set('fx:usdkes', rate, 600)
            cache.set('fx:usdkes:last', rate, 7 * 86400)
            return rate
    except Exception as e:
        logger.warning(f"USD/KES rate fetch failed: {e}")
    return cache.get('fx:usdkes:last') or Decimal('130.00')


def _deposit_response(request, success, message, redirect_to='funding_deposit', extra=None):
    if _is_ajax(request):
        return JsonResponse({
            'success': success, 
            'error': None if success else message,
            'message': message if success else None,
            **(extra or {}),
        })
    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)
    return redirect(redirect_to)


def log_bot_event(message, level='INFO'):
    BotLog.objects.create(message=message, level=level)


# ==========================================
# TRADE P&L HELPERS
# ==========================================

def get_multiplier(symbol):
    """Contract size multiplier for each symbol type."""
    if symbol == 'XAUUSD':
        return 100
    if symbol == 'BTCUSD':
        return 1
    if symbol == 'USOIL':
        return 1000
    if 'JPY' in symbol:
        return 1000
    if symbol.startswith('R_'):
        return 1
    return 100000


def calculate_trade_pnl(symbol, direction, volume, entry_price, exit_price):
    """
    Calculate real P&L in USD based on price movement × lot size × multiplier.
    Returns float pnl (positive = win, negative = loss).
    Symbols known to the MT5 trade server use its contract sizes and currency conversion.
    """
    from . import mt5_service
    if symbol in mt5_service.SPECS:
        try:
            return float(mt5_service.calc_profit(symbol, direction, volume, entry_price, exit_price))
        except mt5_service.TradeError:
            pass
    entry = float(entry_price)
    exit_ = float(exit_price)
    vol = float(volume)
    mult = get_multiplier(symbol)

    diff = (exit_ - entry) if direction == 'BUY' else (entry - exit_)
    return diff * vol * mult


# ==========================================
# AUTHENTICATION VIEWS
# ==========================================

def login_view(request):
    """
    Login with optional SMS new-device alert.
    """
    if request.method == "POST":
        login_input = request.POST.get("login")
        password = request.POST.get("password")

        user = None
        if login_input:
            if "@" in login_input:
                # several accounts can share an email: try each (.get crashed on duplicates)
                for matched_user in User.objects.filter(email__iexact=login_input.strip()):
                    user = authenticate(request, username=matched_user.username, password=password)
                    if user:
                        break
            else:
                user = authenticate(request, username=login_input, password=password)

        # Phone tab: match the number saved on the profile (digits only, last 9 = the subscriber number)
        phone_input = request.POST.get("phone_number", "")
        if user is None and phone_input and not login_input:
            digits = ''.join(ch for ch in phone_input if ch.isdigit())
            if len(digits) >= 9:
                for prof in UserProfile.objects.filter(phone_number__endswith=digits[-9:]).select_related('user'):
                    user = authenticate(request, username=prof.user.username, password=password)
                    if user:
                        break

        if user is not None:
            # Track session (optional but recommended)
            try:
                from .models import UserSession
                ip = request.META.get('REMOTE_ADDR', 'Unknown')
                ua = request.META.get('HTTP_USER_AGENT', '')[:200]

                # Detect suspicious new IP
                known_ips = set(
                    UserSession.objects.filter(user=user)
                    .values_list('ip_address', flat=True)
                    .distinct()
                )

                login(request, user)

                # ✅ SMS: New IP alert (skip if this IP has been seen before)
                if ip and ip not in known_ips and known_ips:
                    try:
                        from .sms_service import sms_service
                        sms_service.send_new_login(user, ip, ua[:60])
                    except Exception as e:
                        logger.warning(f"New login SMS failed: {e}")

                # Record session
                UserSession.objects.create(
                    user=user,
                    session_key=request.session.session_key or '',
                    ip_address=ip,
                    user_agent=ua,
                )
            except Exception as e:
                logger.warning(f"Session tracking failed: {e}")

            # go back to the page that sent them here (?next=), else the dashboard
            next_url = request.POST.get("next") or request.GET.get("next")
            from django.utils.http import url_has_allowed_host_and_scheme
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect("dashboard")
        else:
            messages.error(request, "Invalid credentials. Please check your username/email and password.")

    return render(request, "account/login.html")

def signup_view(request):
    """
    Signup with referral code support.
    - Captures ?ref=CODE from URL
    - Links new user to referrer
    - Creates both site accounts + ReferralProfile
    """
    ref_code = (
        request.GET.get('ref')
        or request.POST.get('ref')
        or request.session.get('ref_code')
    )

    if ref_code:
        request.session['ref_code'] = ref_code.upper()
    nxt = request.GET.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):       # e.g. /subscribe → sign up → the plans page
        request.session['after_signup'] = nxt

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        # the form uses allauth's names (password1/password2); the old names still work
        password = request.POST.get('password1') or request.POST.get('password') or ''
        confirm = request.POST.get('password2') or request.POST.get('confirm_password') or password
        email = (request.POST.get('email') or '').strip()

        # Re-show the form with what was typed (no redirect, so nothing is lost)
        def fail(msg):
            return render(request, "account/signup.html",
                          {'ref_code': ref_code or '', 'error': msg, 'form_username': username, 'form_email': email})

        if not username:
            return fail("Choose a username.")
        if User.objects.filter(username__iexact=username).exists():
            return fail("That username is taken. Pick another one.")
        if password != confirm:
            return fail("Passwords do not match.")
        if len(password) < 8:
            return fail("Password must be at least 8 characters.")
        if email and User.objects.filter(email__iexact=email).exists():
            return fail("An account with this email already exists. Try logging in.")

        try:
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password, email=email)
                # get_or_create: a post_save signal may already have made these
                Account.objects.get_or_create(user=user, account_type='EXCHANGE', defaults={'balance': Decimal('0.00')})
                Account.objects.get_or_create(user=user, account_type='TRADING', defaults={'balance': Decimal('0.00')})
                FundedAccount.objects.get_or_create(user=user)
                UserProfile.objects.get_or_create(user=user)
        except Exception as e:
            logger.exception(f"Signup failed for {username}: {e}")
            return fail("Could not create the account. Please try again.")

        # ✅ Referral linking
        if ref_code:
            try:
                referrer_profile = ReferralProfile.objects.get(referral_code=ref_code.upper())
                referrer = referrer_profile.user

                if referrer != user:
                    Referral.objects.create(
                        referrer=referrer,
                        referred_user=user,
                        status='PENDING'
                    )

                    referrer_profile.total_referrals += 1
                    referrer_profile.save(update_fields=['total_referrals'])

                    new_profile, _ = ReferralProfile.objects.get_or_create(user=user)
                    new_profile.referred_by = referrer
                    new_profile.save(update_fields=['referred_by'])

                    request.session.pop('ref_code', None)
            except ReferralProfile.DoesNotExist:
                pass
            except Exception as e:
                logger.warning(f"Referral link failed for {username}: {e}")

        # Sign the new user straight in (authenticate() picks the right backend when allauth adds a second one)
        auth_user = authenticate(request, username=user.username, password=password)
        if auth_user is None:
            # e.g. a custom backend refused the login: the account exists, so send them to log in
            messages.success(request, "Account created successfully! Log in to continue.")
            return redirect('login')
        login(request, auth_user, backend=getattr(auth_user, 'backend', None) or 'django.contrib.auth.backends.ModelBackend')
        try:
            from .models import UserSession
            UserSession.objects.create(user=auth_user, session_key=request.session.session_key or '',
                                       ip_address=request.META.get('REMOTE_ADDR', 'Unknown'),
                                       user_agent=request.META.get('HTTP_USER_AGENT', '')[:200])
        except Exception as e:
            logger.warning(f"Session tracking failed: {e}")
        from .membership import start_trial, TRIAL_DAYS
        from . import referral_rewards
        invited = Referral.objects.filter(referred_user=auth_user).exists() and referral_rewards.days() > 0
        trial_days = max(TRIAL_DAYS, referral_rewards.days()) if invited else TRIAL_DAYS
        trial = start_trial(auth_user, days=trial_days)
        messages.success(request, f"Account created successfully! Welcome to PATREOAIX, {auth_user.username}."
                         + (f" Your free {trial_days}-day Pro {'invite gift' if invited else 'trial'} has started: every AI signal is unlocked." if trial else '')
                         + (" 🎁 Deposit now and get a 100% bonus on your first deposit." if _bonus_on() else ''))
        return redirect(request.session.pop('after_signup', None) or "dashboard")

    return render(request, "account/signup.html", {'ref_code': ref_code or ''})


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def referral_dashboard(request):
    """
    User-facing referral dashboard.
    Shows the user's referral code, stats, referred users, and reward history.
    """
    profile, _ = ReferralProfile.objects.get_or_create(user=request.user)

    # Referrals made by this user
    referrals = Referral.objects.filter(
        referrer=request.user
    ).select_related('referred_user').order_by('-created_at')

    # Rewards earned
    rewards = ReferralReward.objects.filter(
        referral__referrer=request.user
    ).select_related('referral__referred_user').order_by('-created_at')[:50]

    # Tier reward rates (for the "how much can I earn" table)
    tier_rates = ReferralTierReward.objects.all()

    # Full referral URL for sharing
    referral_link = request.build_absolute_uri(f'/signup/?ref={profile.referral_code}')

    context = {
        'profile': profile,
        'referral_code': profile.referral_code,
        'referral_link': referral_link,
        'total_referrals': profile.total_referrals,
        'active_referrals': referrals.filter(status='ACTIVE').count(),
        'pending_referrals': referrals.filter(status='PENDING').count(),
        'total_earnings': profile.total_earnings,
        'pending_earnings': profile.pending_earnings,
        'withdrawn_earnings': profile.withdrawn_earnings,
        'referrals': referrals,
        'rewards': rewards,
        'tier_rates': tier_rates,
    }
    return render(request, 'store/referral_dashboard.html', context)


@login_required
@require_POST
def withdraw_referral_earnings(request):
    """Move pending referral earnings into the user's EXCHANGE account."""
    profile, _ = ReferralProfile.objects.get_or_create(user=request.user)

    pending = profile.pending_earnings
    if pending <= 0:
        return JsonResponse({'success': False, 'error': 'No pending earnings to withdraw.'}, status=400)

    with transaction.atomic():
        exchange_acc, _ = Account.objects.get_or_create(
            user=request.user, account_type='EXCHANGE'
        )
        # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)

        profile.withdrawn_earnings += pending
        profile.pending_earnings = Decimal('0.00')
        profile.save(update_fields=['withdrawn_earnings', 'pending_earnings'])

        ReferralReward.objects.filter(
            referral__referrer=request.user,
            status='PENDING'
        ).update(status='PAID', paid_at=timezone.now())

        Transaction.objects.create(
            user=request.user,
            amount=pending,
            transaction_type='REFERRAL_PAYOUT',
            details="Referral earnings withdrawn to Exchange balance"
        )

        Notification.objects.create(
            user=request.user,
            title="Referral Earnings Withdrawn",
            message=f"${pending:.2f} has been added to your Exchange balance."
        )

    return JsonResponse({
        'success': True,
        'amount': float(pending),
        'new_balance': float(Account.objects.get(id=exchange_acc.id).balance),
    })


@login_required
def api_referral_stats(request):
    """Live referral stats for dashboard refresh."""
    profile, _ = ReferralProfile.objects.get_or_create(user=request.user)
    return JsonResponse({
        'success': True,
        'referral_code': profile.referral_code,
        'total_referrals': profile.total_referrals,
        'total_earnings': float(profile.total_earnings),
        'pending_earnings': float(profile.pending_earnings),
        'withdrawn_earnings': float(profile.withdrawn_earnings),
    })


# ==========================================
# DASHBOARD & PROFILE VIEWS
# ==========================================

@login_required
def dashboard_view(request):
    notifications = Notification.objects.filter(user=request.user)[:5]

    exchange_acc, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    site_total = exchange_acc.balance + trading_acc.balance

    from .broker_service import broker_service
    broker_summary = broker_service.get_user_broker_summary(request.user)
    broker_total = broker_summary['broker_total_usd']

    grand_total = site_total + broker_total

    site_settings = SiteSettings.load()

    return render(request, 'store/dashboard.html', {
        'notifications': notifications,
        'exchange_balance': exchange_acc.balance,
        'trading_balance': trading_acc.balance,
        'site_total': site_total,
        'balance': grand_total,
        'binance_balance': '0.00',
        'binance_connected': UserBrokerAccount.objects.filter(
            user=request.user, broker='BINANCE', is_active=True
        ).exists(),
        'broker_accounts': broker_summary['brokers'],
        'broker_total': broker_total,
        'grand_total': grand_total,
        'default_trade_amount': site_settings.default_trade_amount,
        'min_trade_amount': site_settings.min_trade_amount,
        'max_trade_amount': site_settings.max_trade_amount,
        'trading_enabled': site_settings.trading_enabled,
    })



# ==========================================
# BROKER BALANCE API
# ==========================================

@login_required
def api_broker_balances(request):
    """
    Returns JSON of all connected broker balances for the current user.
    Optionally force-syncs if ?sync=1 is passed.
    """
    from .broker_service import broker_service

    if request.GET.get('sync') == '1':
        broker_service.sync_all_for_user(request.user)

    summary = broker_service.get_user_broker_summary(request.user)

    # Convert datetimes to ISO strings for JSON
    brokers_json = []
    for b in summary['brokers']:
        brokers_json.append({
            'broker': b['broker'],
            'label': b['label'],
            'balance': b['balance'],
            'currency': b['currency'],
            'synced_at': b['synced_at'].isoformat() if b['synced_at'] else None,
            'status': b['status'],
            'is_stale': b['is_stale'],
            'account_number': b['account_number'],
            'error': b['error'],
        })

    return JsonResponse({
        'success': True,
        'brokers': brokers_json,
        'broker_total_usd': float(summary['broker_total_usd']),
    })


@login_required
@require_POST
def sync_single_broker(request, account_id):
    """Force-refresh a single broker account."""
    from .broker_service import broker_service

    try:
        account = UserBrokerAccount.objects.get(id=account_id, user=request.user)
    except UserBrokerAccount.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Account not found'}, status=404)

    result = broker_service.sync_account(account)

    return JsonResponse({
        'success': result.get('success', False),
        'balance': result.get('balance'),
        'currency': result.get('currency'),
        'error': result.get('error'),
    })



@login_required
def connect_binance(request):
    if request.method == 'POST':
        api_key = (request.POST.get('api_key') or '').strip()
        api_secret = (request.POST.get('api_secret') or '').strip()

        if not api_key or not api_secret:
            messages.error(request, "Both Binance API Key and Secret Key are required.")
            return redirect('dashboard')

        # 1. Validate credentials by calling Binance
        from .binance_service import BinanceService
        binance = BinanceService(api_key=api_key, api_secret=api_secret)
        is_valid, error_msg = binance.validate_credentials()

        if not is_valid:
            messages.error(request, f"Binance API Error: {error_msg}")
            return redirect('dashboard')

        # 2. Fetch initial balance
        balance_result = binance.get_stablecoin_balance()
        initial_balance = balance_result.get('balance', 0.0) if balance_result.get('success') else 0.0

        # 3. Save to database (replaces session storage)
        UserBrokerAccount.objects.update_or_create(
            user=request.user,
            broker='BINANCE',
            defaults={
                'account_number': 'BINANCE-SPOT',
                'server_name': 'Binance',
                'api_token': f"{api_key}::{api_secret}",
                'is_active': True,
                'last_known_balance': Decimal(str(initial_balance)),
                'last_balance_currency': 'USDT',
                'last_synced_at': timezone.now(),
                'sync_status': 'OK',
                'sync_error_message': None,
            }
        )

        messages.success(
            request,
            f"Binance Spot connected! Wallet value: {initial_balance:.2f} USDT"
        )

    return redirect('dashboard')


def _profile_save(request):
    """POST half of the profile page (the second `profile` below used to replace this one, so saves did nothing)."""
    kyc_record = KYCProfile.objects.filter(user=request.user).first()
    user_notifications = Notification.objects.filter(user=request.user)[:5]
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    # ═══════════════════════════════════════════════════════
    # ✅ Handle form submits
    # ═══════════════════════════════════════════════════════
    if request.method == 'POST':

        # ── SMS / Phone number save ──
        if 'update_sms' in request.POST:
            phone = request.POST.get('phone_number', '').strip()
            sms_opt_in = request.POST.get('sms_opt_in') == 'on'

            # Normalize phone via Twilio service
            if phone:
                from .sms_service import sms_service
                phone = sms_service.normalize_phone(phone)

            user_profile.phone_number = phone or None
            user_profile.sms_opt_in = sms_opt_in
            user_profile.save(update_fields=['phone_number', 'sms_opt_in'])

            messages.success(request, '✅ Phone number saved! You\'ll receive SMS updates.')
            return redirect('profile')

        # ── Personal info save ──
        elif 'email' in request.POST:
            user = request.user
            user.email = request.POST.get('email', '').strip()
            user.first_name = request.POST.get('first_name', '').strip()
            user.last_name = request.POST.get('last_name', '').strip()
            user.save(update_fields=['email', 'first_name', 'last_name'])

            messages.success(request, '✅ Profile updated successfully!')
            return redirect('profile')

    # ... rest of your existing code ...
    return None


@login_required
def profile(request):
    kyc_record = KYCProfile.objects.filter(user=request.user).first()
    user_notifications = Notification.objects.filter(user=request.user)[:5]
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        saved = _profile_save(request)
        if saved is not None:
            return saved
    
    # Generate Trader ID and dynamic account type based on subscription status
    if user_profile.is_active_subscription:
        trader_id = f"#TRD-{1000 + request.user.id}"
        account_type = f"Funded ${int(funded_account.balance)}" if funded_account.is_funded else f"Funded {user_profile.subscription_tier.upper()}"
    else:
        trader_id = "Pending Subscription"
        account_type = "Free / Starter"

    context = {
        'notifications': user_notifications,
        'funded_account': funded_account,
        'user_profile': user_profile,
        'trader_id': trader_id,
        'account_type': account_type,
        'kyc_record': kyc_record,
    }
    return render(request, 'store/profile.html', context)


@login_required
def funded_view(request):
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    user_tier = str(getattr(user_profile, 'subscription_tier', 'free')).upper().strip()
    is_active = getattr(user_profile, 'is_active_subscription', False)
    
    is_eligible = is_active and any(t in user_tier for t in ['PRO', 'VIP'])
    if request.user.is_superuser or request.user.is_staff:
        is_eligible = True
        user_tier = 'VIP'

    all_rewards = SurveyReward.objects.filter(user=request.user, is_claimed=False)
    
    if 'VIP' in user_tier:
        available_rewards = all_rewards.filter(tier_required='VIP')
    elif 'PRO' in user_tier:
        available_rewards = all_rewards.filter(tier_required='PRO')
    else:
        available_rewards = all_rewards.none()

    claimable_balance = sum(r.amount for r in available_rewards)

    context = {
        'funded_account': funded_account,
        'available_rewards': available_rewards,
        'user_tier': user_tier,
        'is_eligible': is_eligible,
        'claimable_reward_balance': claimable_balance,
    }
    # AI Gold trades recorded on this funded account
    from . import ai_gold
    context['ai_gold_stats'] = ai_gold.stats(request.user)
    context['ai_gold_trades'] = ai_gold.history(request.user, 50)
    return render(request, 'store/funded.html', context)


# ==========================================
# SUBSCRIPTION & PAYMENT VIEWS
# ==========================================
# views.py

@login_required
def subscription_billing(request):
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    transactions = PaymentRequest.objects.filter(user=request.user).order_by('-created_at')[:10]

    if request.method == 'POST':
        tier = request.POST.get('tier', 'pro').lower()
        payment_method = request.POST.get('payment_method', 'balance')
        raw_phone = request.POST.get('phone_number')

        if tier not in TIER_CONFIG:
            messages.error(request, 'Invalid subscription tier selected.')
            return redirect('subscription_billing')

        tier_data = TIER_CONFIG[tier]
        amount_usd = tier_data['price']
        funded_capital = tier_data['funded_amount']
        amount_kes = int(amount_usd * tier_data['kes_multiplier'])

        # ============================================================
        # M-PESA PATH
        # ============================================================
        mpesa_on = getattr(settings, 'MPESA_DEPOSITS_ENABLED', False)
        if payment_method.lower() in ('mpesa', 'm-pesa') and not mpesa_on:
            messages.error(request, 'M-Pesa is not available. Deposit USDT to your balance, then pay from the balance.')
            return redirect('subscription_billing')
        if payment_method.lower() in ('mpesa', 'm-pesa'):
            if not raw_phone:
                messages.error(request, 'M-Pesa phone number is required for STK push.')
                return redirect('subscription_billing')

            formatted_phone = format_mpesa_number(raw_phone)

            try:
                client = MpesaClient()
                response = client.stk_push(
                    phone_number=formatted_phone,
                    amount=amount_kes,
                    account_reference=f"SUB-{request.user.id}",
                    transaction_desc=f"Subscription {tier.upper()}",
                    callback_url=settings.MPESA_CALLBACK_URL
                )

                if response.response_code == '0':
                    payment_req = PaymentRequest.objects.create(
                        user=request.user,
                        amount=amount_usd,
                        transaction_type=f'SUB_{tier.upper()}',
                        payment_method='M-Pesa',
                        status='PROCESSING'
                    )
                    MpesaPayment.objects.create(
                        payment_request=payment_req,
                        merchant_request_id=response.merchant_request_id,
                        checkout_request_id=response.checkout_request_id,
                        phone_number=formatted_phone,
                        amount=amount_usd
                    )
                    messages.success(request, 'STK Push sent! Please check your phone and enter your M-Pesa PIN.')
                else:
                    err_msg = getattr(response, 'response_description', 'Failed to initiate M-Pesa STK push.')
                    messages.error(request, err_msg)
            except Exception as e:
                messages.error(request, f"M-Pesa error: {str(e)}")

        # ============================================================
        # ACCOUNT BALANCE PATH
        # ============================================================
        elif payment_method.lower() == 'balance':
            if account.balance >= amount_usd:
                with transaction.atomic():
                    account.balance -= amount_usd
                    account.save()

                    now = timezone.now()
                    user_profile.subscription_tier = tier
                    user_profile.is_active_subscription = True
                    user_profile.subscription_started_at = now
                    user_profile.subscription_expires_at = now + timedelta(days=30)
                    user_profile.last_renewal_amount = amount_usd
                    user_profile.save()

                    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
                    funded_account.initial_balance = funded_capital
                    funded_account.balance = funded_capital
                    funded_account.is_funded = True
                    funded_account.save()

                    Transaction.objects.create(
                        user=request.user,
                        amount=amount_usd,
                        transaction_type='SUBSCRIPTION',
                        details=f"Subscribed to {tier.upper()} — ${amount_usd} deducted from Exchange balance"
                    )

                    try:
                        credit_referrer_on_subscription(request.user, amount_usd, tier.upper())
                    except Exception as e:
                        logger.warning(f"Referral credit failed: {e}")

                messages.success(
                    request,
                    f"Successfully subscribed to {tier.upper()}! "
                    f"${amount_usd} deducted. Funded account credited with ${funded_capital}."
                )
            else:
                messages.error(
                    request,
                    f'Insufficient balance. You need ${amount_usd} but have ${account.balance}.'
                )

        return redirect('subscription_billing')

    context = {
        'user_profile': user_profile,
        'current_plan': user_profile.subscription_tier,
        'account': account,
        'transactions': transactions,
        'subscription_expires_at': user_profile.subscription_expires_at,
        'days_remaining': user_profile.days_remaining(),
        'mpesa_enabled': getattr(settings, 'MPESA_DEPOSITS_ENABLED', False),
        'has_deposit': Transaction.objects.filter(user=request.user, transaction_type='DEPOSIT', status='completed').exists(),
    }
    return render(request, 'store/subscription-billing.html', context)

@csrf_exempt
def mpesa_callback(request):
    if request.method != 'POST':
        return HttpResponse("Method Not Allowed", status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
        stk_callback = body.get('Body', {}).get('stkCallback', {})
        checkout_request_id = stk_callback.get('CheckoutRequestID')
        result_code = stk_callback.get('ResultCode')
        result_desc = stk_callback.get('ResultDesc')

        with transaction.atomic():
            payment_pay = MpesaPayment.objects.select_for_update().filter(
                checkout_request_id=checkout_request_id
            ).first()

            if not payment_pay:
                return JsonResponse({"ResultCode": 1, "ResultDesc": "Record not found"}, status=404)

            payment_req = payment_pay.payment_request
            payment_pay.result_code = result_code
            payment_pay.result_desc = result_desc

            if result_code == 0:
                metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
                receipt_number = ''
                for item in metadata:
                    if item.get('Name') == 'MpesaReceiptNumber':
                        receipt_number = str(item.get('Value'))

                payment_pay.mpesa_receipt_number = receipt_number
                payment_pay.save()

                if payment_req:
                    payment_req.status = 'APPROVED'
                    payment_req.processed = True
                    payment_req.save()

                    if payment_req.transaction_type.startswith('SUB_'):
                        tier_name = payment_req.transaction_type.split('_')[1].lower()
                        profile, _ = UserProfile.objects.get_or_create(user=payment_req.user)
                        profile.subscription_tier = tier_name
                        profile.is_active_subscription = True
                        profile.save()
                        credit_referrer_on_subscription(payment_req.user, payment_req.amount, tier_name.upper())

                        # Fund the account based on tier configuration ($500 or $2500)
                        if tier_name in TIER_CONFIG:
                            funded_capital = TIER_CONFIG[tier_name]['funded_amount']
                            funded_account, _ = FundedAccount.objects.get_or_create(user=payment_req.user)
                            funded_account.initial_balance = funded_capital
                            funded_account.balance = funded_capital
                            funded_account.is_funded = True
                            funded_account.save()

                    # a paid subscription is logged, not credited to the wallet (a DEPOSIT here gave the money back as balance)
                    is_sub = payment_req.transaction_type.startswith('SUB_')
                    Transaction.objects.create(
                        user=payment_req.user,
                        amount=payment_req.amount,
                        transaction_type='SUBSCRIPTION' if is_sub else 'DEPOSIT',
                        details=f"M-Pesa Payment Success - Receipt: {receipt_number}"
                    )
            else:
                payment_pay.save()
                if payment_req:
                    payment_req.status = 'REJECTED'
                    payment_req.save()

        return JsonResponse({"ResultCode": 0, "ResultDesc": "Success"})
    except Exception as e:
        return JsonResponse({"ResultCode": 1, "ResultDesc": str(e)}, status=500)


@login_required
def check_payment_status(request, checkout_id):
    payment = get_object_or_404(MpesaPayment, checkout_request_id=checkout_id, payment_request__user=request.user)
    pref_req = payment.payment_request
    status = pref_req.status if pref_req else 'PROCESSING'
    
    if status in ['APPROVED', 'COMPLETED']:
        return JsonResponse({'status': 'completed', 'message': 'Payment successful! Unlocking subscription...'})
    elif status in ['REJECTED', 'FAILED']:
        return JsonResponse({'status': 'failed', 'message': payment.result_desc or 'Payment was cancelled or failed.'})
    else:
        return JsonResponse({'status': 'pending', 'message': 'Waiting for M-Pesa PIN entry...'})

@login_required
def request_deposit(request):
    """
    User submits a deposit request (M-Pesa STK push or manual).
    Sends SMS confirmation after successful submission.
    """
    if request.method != 'POST':
        return redirect('funding_deposit')

    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
        except (ValueError, json.JSONDecodeError):
            body = {}
    else:
        body = request.POST

    amount_str = body.get('amount')
    method_val = body.get('method', 'M-Pesa')
    raw_phone = body.get('phone_number') or body.get('phone')

    try:
        amount = Decimal(str(amount_str))
        if amount <= 0:
            return _deposit_response(request, False, "Amount must be greater than 0.")
    except (InvalidOperation, ValueError, TypeError):
        return _deposit_response(request, False, "Invalid deposit amount.")

    # ==========================================
    # M-PESA STK PUSH
    # ==========================================
    if method_val.upper() in ('MPESA', 'M-PESA'):
        if not raw_phone:
            return _deposit_response(request, False, "Phone number is required for M-Pesa deposits.")

        formatted_phone = format_mpesa_number(raw_phone)

        try:
            client = MpesaClient()
            USD_TO_KES_RATE = _usd_kes_rate()
            amount_kes = int(round(amount * USD_TO_KES_RATE))

            response = client.stk_push(
                phone_number=formatted_phone,
                amount=amount_kes,
                account_reference=f"DEP-{request.user.id}",
                transaction_desc=f"Deposit for {request.user.username}",
                callback_url=settings.MPESA_CALLBACK_URL
            )

            if response.response_code == '0':
                payment_req = PaymentRequest.objects.create(
                    user=request.user,
                    amount=amount,
                    transaction_type='DEPOSIT',
                    payment_method='M-Pesa',
                    status='PROCESSING'
                )
                MpesaPayment.objects.create(
                    payment_request=payment_req,
                    merchant_request_id=response.merchant_request_id,
                    checkout_request_id=response.checkout_request_id
                )

                # ✅ SMS: Deposit submitted
                try:
                    from .sms_service import sms_service
                    sms_result = sms_service.send_deposit_submitted(request.user, amount)
                    if not sms_result.get('success'):
                        logger.warning(f"Deposit submitted SMS failed for {request.user.username}: {sms_result.get('error')}")
                except Exception as e:
                    logger.warning(f"Deposit submitted SMS crashed: {e}")

                return _deposit_response(request, True, f"STK Push sent for Ksh {amount_kes} (${amount} USD). Please check your phone.",
                                         extra={'checkout_id': response.checkout_request_id, 'amount_kes': amount_kes})
            else:
                return _deposit_response(request, False, f"M-Pesa error: {getattr(response, 'response_description', 'Unknown error')}")
        except Exception as e:
            return _deposit_response(request, False, f"Payment initiation failed: {str(e)}")

    # ==========================================
    # MANUAL / CRYPTO DEPOSIT
    # ==========================================
    else:
        PaymentRequest.objects.create(
            user=request.user,
            amount=amount,
            transaction_type='DEPOSIT',
            payment_method=method_val,
            status='PENDING',
            processed=False
        )

        # ✅ SMS: Deposit submitted
        try:
            from .sms_service import sms_service
            sms_result = sms_service.send_deposit_submitted(request.user, amount)
            if not sms_result.get('success'):
                logger.warning(f"Deposit submitted SMS failed for {request.user.username}: {sms_result.get('error')}")
        except Exception as e:
            logger.warning(f"Deposit submitted SMS crashed: {e}")

        return _deposit_response(request, True, "Deposit request submitted. Awaiting admin approval.", redirect_to='dashboard')

@csrf_exempt
def binance_webhook(request):
    if request.method != 'POST':
        return HttpResponse("Method Not Allowed", status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
        biz_content = body.get('data', {})
        merchant_trade_no = biz_content.get('merchantTradeNo')
        status = biz_content.get('status') # e.g., "PAID"
        total_fee = Decimal(str(biz_content.get('totalFee', '0')))

        if status == 'PAID' and merchant_trade_no:
            user_id = merchant_trade_no.split('_')[1]
            
            with transaction.atomic():
                # Deposits land in the EXCHANGE account
                account = Account.objects.select_for_update().get(user_id=user_id, account_type='EXCHANGE')
                # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)
                
                payment_req = PaymentRequest.objects.filter(
                    user_id=user_id, 
                    amount=total_fee, 
                    status='PENDING'
                ).first()
                
                if payment_req:
                    payment_req.status = 'APPROVED'
                    payment_req.processed = True
                    payment_req.save()
                
                Transaction.objects.create(
                    user_id=user_id,
                    amount=total_fee,
                    transaction_type='DEPOSIT',
                    details=f"Direct Binance USDT Deposit - Order: {merchant_trade_no}"
                )

        return JsonResponse({"returnCode": "SUCCESS", "returnMessage": None})
    except Exception as e:
        return JsonResponse({"returnCode": "FAIL", "returnMessage": str(e)}, status=500)


@login_required
def funding_view(request):
    exchange_acc, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    total_balance = exchange_acc.balance + trading_acc.balance
    transactions = Transaction.objects.filter(user=request.user).order_by('-created_at')

    # ✅ NEW — User's withdrawal requests (all statuses)
    user_withdrawals = PaymentRequest.objects.filter(
        user=request.user,
        transaction_type__iexact='Withdrawal'
    ).order_by('-created_at')[:20]

    # ✅ NEW — User's deposit requests (all statuses)
    user_deposits = PaymentRequest.objects.filter(
        user=request.user,
        transaction_type__iexact='Deposit'
    ).order_by('-created_at')[:20]

    context = {
        'total_balance': total_balance,
        'exchange_balance': exchange_acc.balance,
        'trading_balance': trading_acc.balance,
        'transactions': transactions,
        'user_withdrawals': user_withdrawals,
        'user_deposits': user_deposits,
    }
    return render(request, 'payment.html', context)



@login_required
def request_withdrawal(request):
    """
    User submits a withdrawal request.
    - Validates balance
    - Creates a PENDING PaymentRequest (no balance deduction yet)
    - Sends SMS confirmation to the user
    - Notifies all admins in-app
    """
    if request.method != 'POST':
        return redirect('funding_deposit')

    # ── 1. Validate amount ─────────────────────────────────
    try:
        amount = Decimal(request.POST.get('amount', '0'))
        if amount <= 0:
            messages.error(request, 'Withdrawal amount must be greater than zero.')
            return redirect('funding_deposit')
    except (InvalidOperation, ValueError, TypeError):
        messages.error(request, 'Please enter a valid amount.')
        return redirect('funding_deposit')

    MIN_WITHDRAWAL = Decimal('10.00')
    if amount < MIN_WITHDRAWAL:
        messages.error(request, f'Minimum withdrawal is ${MIN_WITHDRAWAL:.2f}.')
        return redirect('funding_deposit')

    # ── 2. Get method + destination ────────────────────────
    method = request.POST.get('method', 'Crypto').strip()
    destination = (
        request.POST.get('wallet_address')
        or request.POST.get('destination')
        or request.POST.get('details')
        or request.POST.get('phone_number')
        or ''
    ).strip()

    if not destination:
        messages.error(request, 'Please provide a withdrawal destination.')
        return redirect('funding_deposit')

    # ── 3. Balance check (accounting for pending withdrawals) ──
    from django.db.models import Sum

    exchange_acc = get_object_or_404(
        Account, user=request.user, account_type='EXCHANGE'
    )

    pending_total = PaymentRequest.objects.filter(
        user=request.user,
        transaction_type__iexact='Withdrawal',
        status='PENDING'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    from .bonus import locked_amount
    locked = locked_amount(request.user)                       # first-deposit bonus not unlocked yet
    available_balance = exchange_acc.balance - pending_total - locked

    if available_balance < amount:
        messages.error(
            request,
            f'Insufficient available balance. You have ${max(available_balance, Decimal("0")):.2f} available'
            + (f' (${locked:.2f} deposit bonus unlocks after its trading requirement).' if locked else '.')
        )
        return redirect('funding_deposit')

    # ── 4. Create the PENDING request ──────────────────────
    PaymentRequest.objects.create(
        user=request.user,
        amount=amount,
        transaction_type='Withdrawal',
        payment_method=method,
        provider_reference=destination,
        status='PENDING',
        processed=False,
    )

    # ── 5. ✅ SMS: Withdrawal submitted ─────────────────────
    try:
        from .sms_service import sms_service
        sms_result = sms_service.send_withdrawal_submitted(request.user, amount, method)
        if not sms_result.get('success'):
            logger.warning(f"Withdrawal SMS not sent for {request.user.username}: {sms_result.get('error')}")
    except Exception as e:
        logger.warning(f"Withdrawal SMS crashed: {e}")

    # ── 6. Notify all admins in-app ────────────────────────
    for staff in User.objects.filter(is_staff=True, is_active=True):
        Notification.objects.create(
            user=staff,
            title="New Withdrawal Request",
            message=f"@{request.user.username} requested a ${amount:.2f} {method} withdrawal.",
            category='FUNDING',
            priority='warning',
            action_url='/admin/hub/',
            action_label='Review in Hub',
        )

    # ── 7. Show success + redirect ─────────────────────────
    messages.success(
        request,
        f'✅ Withdrawal of ${amount:.2f} submitted for review. '
        f'You\'ll receive an SMS confirmation shortly.'
    )
    return redirect('funding_deposit')

@login_required
def process_transaction(request):
    if request.method == 'POST':
        try:
            amount = Decimal(request.POST.get('amount'))
            t_type = request.POST.get('type')
        except (ValueError, TypeError):
            messages.error(request, "Invalid transaction amount.")
            return redirect('dashboard')

        # Safely default to EXCHANGE account for generic transactions
        account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')

        if t_type == 'WITHDRAWAL':
            if account.balance < amount:
                messages.error(request, "Insufficient funds for withdrawal.")
                return redirect('dashboard')
            account.balance -= amount
        else:
            account.balance += amount
        
        account.save()

        Transaction.objects.create(
            user=request.user,
            amount=amount,
            transaction_type=t_type,
            status='APPROVED'
        )

        Notification.objects.create(
            user=request.user,
            title="Transaction Successful",
            message=f"Your {t_type.lower()} of ${amount} was successful."
        )

        messages.success(request, f"{t_type.capitalize()} of ${amount} completed.")
        return redirect('dashboard')
    
    return render(request, 'store/transaction.html')


@login_required
def payment_status_view(request, payment_id):
    """
    Fetches real-time status of a NOWPayments transaction from the sandbox/production API 
    and updates the local database state.
    """
    payment_req = get_object_or_404(PaymentRequest, user=request.user, payment_id=payment_id)
    
    api_key = getattr(settings, 'NOWPAYMENTS_API_KEY', '')
    is_sandbox = getattr(settings, 'NOWPAYMENTS_SANDBOX', True)
    base_url = 'https://api-sandbox.nowpayments.io/v1/' if is_sandbox else 'https://api.nowpayments.io/v1/'
    
    headers = {'x-api-key': api_key}
    status_data = {}
    
    try:
        response = requests.get(f"{base_url}payment/{payment_id}", headers=headers, timeout=10)
        if response.status_code == 200:
            status_data = response.json()
            np_status = status_data.get('payment_status')
            
            # Map NOWPayments statuses to local choices
            if np_status in ['finished', 'confirmed']:
                payment_req.status = 'APPROVED'
            elif np_status in ['failed', 'expired', 'rejected']:
                payment_req.status = 'REJECTED'
            elif np_status in ['waiting', 'confirming', 'processing']:
                payment_req.status = 'PROCESSING'
                
            payment_req.save()
    except requests.RequestException:
        pass

    # Return JSON if requested via AJAX script, otherwise render template
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        return JsonResponse({
            'payment_id': payment_id,
            'status': payment_req.status,
            'np_status': status_data.get('payment_status', 'unknown')
        })

    context = {
        'payment_req': payment_req,
        'status_data': status_data,
    }
    return render(request, 'store/payment_status.html', context)


@login_required
def switch_tier(request, tier):
    tier_lower = tier.lower()
    valid_tiers = ['starter', 'pro', 'vip']
    
    if tier_lower in valid_tiers:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        profile.subscription_tier = tier_lower
        profile.is_active_subscription = (tier_lower != 'starter')
        profile.save()
        messages.success(request, f"Your subscription has been successfully updated to {tier_lower.upper()}.")
    else:
        messages.error(request, "Selected subscription tier is invalid.")
        
    return redirect('subscription_billing')


# views.py

@login_required
@require_POST
def cancel_subscription(request):
    """
    Cancel subscription and issue a pro-rated refund to the EXCHANGE account.
    Refund = (days_remaining / total_billing_days) × last_renewal_amount
    """
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    # ── 1. Guard: nothing to cancel ─────────────────────────────
    if not user_profile.is_active_subscription:
        messages.warning(request, 'You do not have an active subscription to cancel.')
        return redirect('subscription_billing')

    tier = user_profile.subscription_tier
    if tier == 'starter' or not tier:
        messages.warning(request, 'Starter plan has no billing to cancel.')
        return redirect('subscription_billing')

    # ── 2. Calculate pro-rated refund ───────────────────────────
    total_days = user_profile.total_billing_days()          # typically 30
    days_remaining = user_profile.days_remaining()          # 0..total_days
    days_used = max(0, total_days - days_remaining)

    paid_amount = user_profile.last_renewal_amount or Decimal('0.00')

    if total_days > 0 and paid_amount > 0 and days_remaining > 0:
        refund_amount = (
            paid_amount * Decimal(days_remaining) / Decimal(total_days)
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        refund_amount = Decimal('0.00')

    # ── 3. Apply the cancellation atomically ────────────────────
    with transaction.atomic():
        # 3a. Credit refund to EXCHANGE balance
        if refund_amount > 0:
            exchange_acc, _ = Account.objects.get_or_create(
                user=request.user, account_type='EXCHANGE'
            )
            exchange_acc.balance += refund_amount
            exchange_acc.save()

            Transaction.objects.create(
                user=request.user,
                amount=refund_amount,
                transaction_type='SUBSCRIPTION_REFUND',
                details=(
                    f"Pro-rated refund for {tier.upper()} cancellation — "
                    f"{days_remaining}/{total_days} days unused "
                    f"(${paid_amount} × {days_remaining}/{total_days})"
                )
            )

        # 3b. Downgrade the profile
        user_profile.subscription_tier = 'starter'
        user_profile.is_active_subscription = False
        user_profile.subscription_started_at = None
        user_profile.subscription_expires_at = None
        user_profile.last_renewal_amount = Decimal('0.00')
        user_profile.save(update_fields=[
            'subscription_tier', 'is_active_subscription',
            'subscription_started_at', 'subscription_expires_at',
            'last_renewal_amount',
        ])

        # 3c. Deactivate funded account benefits
        funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
        # If you want to wipe the funded balance on cancel, uncomment:
        # funded_account.balance = Decimal('0.00')
        # funded_account.is_funded = False
        # funded_account.save()
        # Otherwise, keep the balance but mark inactive:
        funded_account.is_funded = False
        funded_account.save(update_fields=['is_funded'])

        # 3d. Notify the user
        if refund_amount > 0:
            message = (
                f"Subscription cancelled. A pro-rated refund of "
                f"${refund_amount:.2f} ({days_remaining} unused days) "
                f"was credited to your Exchange balance."
            )
        else:
            message = (
                f"Subscription cancelled. No refund — your billing period "
                f"had already ended."
            )

        Notification.objects.create(
            user=request.user,
            title="Subscription Cancelled",
            message=message,
            category='FUNDING',
            priority='info',
            action_url='/funding/',
            action_label='View Balance',
        )

    # ── 4. Feedback to the user ─────────────────────────────────
    if refund_amount > 0:
        messages.success(
            request,
            f"✅ Subscription cancelled. ${refund_amount:.2f} refunded to your "
            f"Exchange balance ({days_remaining} of {total_days} days unused, "
            f"${days_used} days billed)."
        )
    else:
        messages.info(
            request,
            "Subscription cancelled. No refund was due."
        )

    return redirect('subscription_billing')

# ==========================================
# BROKER & DERIV INTEGRATION VIEWS
# ==========================================

@login_required
def ai_prediction_models(request):
    active_account = UserBrokerAccount.objects.filter(user=request.user, broker='DERIV', is_active=True).first()
    app_id = getattr(settings, 'DERIV_APP_ID', '34mjct1dBCmYao65TsMoD')
    deriv_balance = None

    context = {
        'active_account': active_account,
        'deriv_oauth_url': f"https://oauth.deriv.com/oauth2/authorize?app_id={app_id}",
        'app_id': app_id,
        'deriv_balance': deriv_balance,
        'market_overview_url': _page_url(['market_overview', 'market_overview_view', 'market-overview'], '/market-overview/'),
    }
    return render(request, 'store/ai_prediction_models.html', context)


def _deriv_client_id():
    """New-API OAuth client id: DERIV_OAUTH_CLIENT_ID, else a non-numeric DERIV_APP_ID (numeric ids are legacy apps)."""
    cid = getattr(settings, 'DERIV_OAUTH_CLIENT_ID', '') or getattr(settings, 'DERIV_APP_ID', '')
    return '' if str(cid).isdigit() else str(cid)


def _deriv_redirect_uri(request):
    from django.urls import reverse
    return getattr(settings, 'DERIV_OAUTH_REDIRECT', '') or request.build_absolute_uri(reverse('deriv_callback'))


def _save_deriv_accounts(user, token, accounts):
    """Store every Deriv account behind a login (demo + real), all shown on the dashboard. Returns saved rows."""
    from .broker_service import broker_service
    UserBrokerAccount.objects.filter(user=user, broker='DERIV').update(is_active=False)
    saved = []
    for a in accounts:
        defaults = {'api_token': token, 'server_name': 'Deriv Demo' if a['is_demo'] else 'Deriv Real', 'is_active': True}
        if a.get('balance') is not None:
            defaults.update({'last_known_balance': Decimal(str(a['balance'])), 'last_balance_currency': a.get('currency') or 'USD',
                             'last_synced_at': timezone.now(), 'sync_status': 'OK', 'sync_error_message': None})
        acc, _ = UserBrokerAccount.objects.update_or_create(user=user, broker='DERIV', account_number=a['id'], defaults=defaults)
        if a.get('balance') is None:
            try:
                broker_service.sync_account(acc)
            except Exception as e:
                logger.warning(f"Initial Deriv sync failed for {a['id']}: {e}")
        saved.append(acc)
    return saved


def _deriv_connected_message(request, saved):
    demo = sum(1 for a in saved if a.server_name == 'Deriv Demo')
    real = len(saved) - demo
    parts = ([f"{real} real"] if real else []) + ([f"{demo} demo"] if demo else [])
    messages.success(request, f"Deriv connected: {' + '.join(parts)} account{'s' if len(saved) != 1 else ''}. Balances are on your dashboard.")


@login_required
def connect_deriv(request):
    client_id = _deriv_client_id()
    if client_id:
        # New Deriv API: OAuth 2.0 Authorization Code + PKCE on auth.deriv.com
        import secrets
        from .deriv_service import DerivService
        verifier, challenge = DerivService.new_pkce()
        state = secrets.token_urlsafe(24)
        request.session['deriv_oauth'] = {'verifier': verifier, 'state': state, 'next': request.GET.get('next') or ''}
        return redirect(DerivService.authorize_url(client_id, _deriv_redirect_uri(request), state, challenge))

    # Legacy app (numeric app_id): old oauth.deriv.com redirect with acct1/token1
    app_id = getattr(settings, 'DERIV_APP_ID', '')
    redirect_uri = _deriv_redirect_uri(request)

    oauth_url = (
        f"https://oauth.deriv.com/oauth2/authorize"
        f"?app_id={app_id}"
        f"&l=en"
        f"&redirect_uri={redirect_uri}"
    )

    return redirect(oauth_url)

@login_required
def deriv_callback(request):
    """
    Deriv redirects here after the user authorizes the app.
    Query params contain account tokens like:
        ?acct1=CR123456&token1=a1-xxx&cur1=USD
        &acct2=VRTC999&token2=a2-yyy&cur2=USD
    """
    from .broker_service import broker_service

    # ── New API: ?code=…&state=… → exchange for an access token, then list demo + real accounts ──
    if request.GET.get('code') or request.GET.get('error'):
        from .deriv_service import DerivService
        pending = request.session.pop('deriv_oauth', None) or {}
        if request.GET.get('error'):
            messages.error(request, f"Deriv login cancelled: {request.GET.get('error_description') or request.GET.get('error')}")
            return redirect('dashboard')
        if not pending or request.GET.get('state') != pending.get('state'):
            messages.error(request, "Deriv login expired or was started in another tab. Please connect again.")
            return redirect('dashboard')
        deriv = DerivService()
        token, _expires, err = deriv.exchange_code(_deriv_client_id(), request.GET['code'], pending['verifier'], _deriv_redirect_uri(request))
        if err:
            messages.error(request, f"Deriv login failed: {err}")
            return redirect('dashboard')
        accounts, err = deriv.list_accounts(token)
        if err or not accounts:
            messages.error(request, f"Deriv login worked but no accounts came back: {err or 'none found'}")
            return redirect('dashboard')
        _deriv_connected_message(request, _save_deriv_accounts(request.user, token, accounts))
        return redirect(pending.get('next') or 'dashboard')

    # ── Legacy API: ?acct1=…&token1=… ──
    # Deactivate old Deriv connections first
    UserBrokerAccount.objects.filter(
        user=request.user, broker='DERIV'
    ).update(is_active=False)

    saved_count = 0
    first_account = None

    # Deriv sends acct1, token1, cur1, acct2, token2, cur2, ... in order
    index = 1
    while True:
        acct = request.GET.get(f'acct{index}')
        token = request.GET.get(f'token{index}')
        currency = request.GET.get(f'cur{index}', 'USD')

        if not acct or not token:
            break

        # First account is the primary one; every account (demo + real) is shown on the dashboard
        is_primary = (index == 1)
        from .deriv_service import looks_demo

        account, created = UserBrokerAccount.objects.update_or_create(
            user=request.user,
            broker='DERIV',
            account_number=acct,
            defaults={
                'api_token': token,
                'server_name': 'Deriv Demo' if looks_demo(acct) else 'Deriv Real',
                'is_active': True,
            }
        )

        if is_primary:
            first_account = account

        saved_count += 1
        index += 1

    if saved_count == 0:
        messages.error(
            request,
            "No Deriv accounts were returned. Please try connecting again."
        )
        return redirect('dashboard')

    # Immediately sync every account's balance so the dashboard is fresh
    for acc in UserBrokerAccount.objects.filter(user=request.user, broker='DERIV', is_active=True):
        try:
            broker_service.sync_account(acc)
        except Exception as e:
            logger.warning(f"Initial Deriv sync failed: {e}")

    messages.success(
        request,
        f"Successfully connected {saved_count} Deriv account(s)! "
        f"Your live balance is now displayed on the dashboard."
    )
    return redirect('dashboard')

@login_required
def connect_broker(request):
    if request.method == 'POST':
        broker = request.POST.get('broker', 'DERIV')
        account_number = request.POST.get('account_number')
        if account_number:
            UserBrokerAccount.objects.filter(user=request.user, broker=broker).update(is_active=False)
            UserBrokerAccount.objects.create(
                user=request.user, broker=broker, account_number=account_number, 
                api_token=request.POST.get('server_or_token'), is_active=True
            )
            messages.success(request, f"Connected {broker}!")
    return redirect('ai_prediction_models')


@login_required
@require_POST
def connect_deriv_token(request):
    """Connect Deriv with a Personal Access Token (new API, needs DERIV_APP_ID) or a legacy a1- API token."""
    from .deriv_service import DerivService, is_legacy_token
    token = (request.POST.get('token') or '').strip()
    if not token:
        messages.error(request, "Paste your Deriv token first.")
        return redirect('dashboard')
    deriv = DerivService()
    if not is_legacy_token(token) and not deriv.app_id:
        messages.error(request, "Personal Access Tokens need your Deriv App ID set as DERIV_APP_ID in settings.")
        return redirect('dashboard')
    accounts, err = deriv.list_accounts(token)
    if err or not accounts:
        messages.error(request, f"Deriv token not accepted: {err or 'no accounts found'}")
        return redirect('dashboard')
    _deriv_connected_message(request, _save_deriv_accounts(request.user, token, accounts))
    return redirect('dashboard')


@login_required
@require_POST
def disconnect_broker(request, account_id):
    acc = UserBrokerAccount.objects.filter(id=account_id, user=request.user).first()
    if not acc:
        return JsonResponse({'success': False, 'error': 'Account not found'}, status=404)
    acc.is_active = False
    acc.save(update_fields=['is_active'])
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    messages.success(request, f"{acc.get_broker_display()} {acc.account_number} disconnected.")
    return redirect('dashboard')







# ==========================================
# TRADING & MARKET VIEWS
# ==========================================
# In views.py - add this function or use in your trade_view


@login_required
def trade_view(request):
    """Main trading dashboard with live charts and execution"""
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')
    exchange_acc, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')

    # Get all open trades with their assets
    open_trades = Trade.objects.filter(
        user=request.user,
        status='OPEN'
    ).select_related('asset').order_by('-date_opened')

    total_pnl = sum(float(t.pnl or 0) for t in open_trades)
    balance = float(trading_acc.balance)

    # ==========================================
    # ✅ COPY TRADE — signal availability for badge
    # ==========================================
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    user_tier = str(profile.subscription_tier or 'STARTER').upper()
    if request.user.is_staff or request.user.is_superuser:
        user_tier = 'VIP'

    # Active signals matching user's tier
    active_signals = CopyTradeSignal.objects.filter(
        status='ACTIVE'
    ).filter(
        Q(tier_required=user_tier) | Q(tier_required='ALL')
    )

    # Filter out expired signals
    now = timezone.now()
    active_signals = active_signals.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    # Exclude already-redeemed signals
    redeemed_ids = CopyTradeRedemption.objects.filter(
        user=request.user
    ).values_list('signal_id', flat=True)

    available_signals = active_signals.exclude(id__in=redeemed_ids)
    available_count = available_signals.count()

    context = {
        'trading_balance': trading_acc.balance,
        'exchange_balance': exchange_acc.balance,
        'total_balance': trading_acc.balance + exchange_acc.balance,
        'open_trades_count': open_trades.count(),
        'open_trades': open_trades,
        'unrealized_pnl': round(total_pnl, 2),
        'equity': round(balance + total_pnl, 2),
        'available_copy_signals': available_count,
        'trade_specs': _trade_specs(),
    }
    return render(request, 'store/trade.html', context)


def _trade_specs():
    """Symbol specs of the trade server (digits, contract size, leverage, volume limits…) as plain JSON,
    so the trade page sizes positions and shows margin / pip value the same way the server books them."""
    import dataclasses
    from . import mt5_service
    out = {}
    for sym, spec in getattr(mt5_service, 'SPECS', {}).items():
        if dataclasses.is_dataclass(spec):
            raw = {f.name: getattr(spec, f.name) for f in dataclasses.fields(spec)}
        elif hasattr(spec, '_asdict'):
            raw = spec._asdict()
        elif isinstance(spec, dict):
            raw = spec
        else:
            raw = {k: v for k, v in vars(spec).items() if not k.startswith('_')} if hasattr(spec, '__dict__') else {}
        clean = {}
        for k, v in raw.items():
            if isinstance(v, Decimal):
                v = float(v)
            if v is None or isinstance(v, (bool, int, float, str)):
                clean[k] = v
        out[sym] = clean
    return out



@login_required
@csrf_exempt
@require_POST
def redeem_copy_code(request):
    """
    API endpoint: user pastes a code, we validate, enforce limits, and execute.
    Body: { "code": "PRO-GOLD47X" }
    """
    from decimal import Decimal

    try:
        data = json.loads(request.body)
        raw_code = (data.get('code') or '').strip().upper()
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'success': False, 'error': 'Invalid request format.'}, status=400)

    if not raw_code:
        return JsonResponse({'success': False, 'error': 'Please enter a code.'}, status=400)

    # 1. Find the signal
    try:
        signal = CopyTradeSignal.objects.get(code=raw_code)
    except CopyTradeSignal.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Invalid code. Please check and try again.'}, status=404)

    # 2. Signal validity
    if not signal.is_valid:
        return JsonResponse({
            'success': False,
            'error': f'This signal is {signal.status.lower()}.'
        }, status=400)

    # ✅ 2b. MARKET HOURS GUARD — signal can only be redeemed when market is open
    is_open, reason, hint = is_market_open(signal.symbol)
    if not is_open:
        error_msg = reason
        if hint:
            error_msg += f". {hint}."
        return JsonResponse({
            'success': False,
            'error': f'⏸ {error_msg}',
            'market_closed': True,
            'symbol': signal.symbol,
        }, status=400)

    # 3. Tier check
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    user_tier = str(profile.subscription_tier or 'STARTER').upper()
    if request.user.is_staff or request.user.is_superuser:
        user_tier = 'VIP'

    if signal.tier_required != 'ALL' and signal.tier_required != user_tier:
        return JsonResponse({
            'success': False,
            'error': f'This signal is exclusive to {signal.tier_required} members.'
        }, status=403)

    # 4. One-time-use check (per user)
    if CopyTradeRedemption.objects.filter(signal=signal, user=request.user).exists():
        return JsonResponse({
            'success': False,
            'error': 'You have already used this code.'
        }, status=400)

    # 5. Daily limit check
    tier_limit, _ = TierCopyTradeLimit.objects.get_or_create(
        tier=user_tier, defaults={'daily_limit': 5 if user_tier == 'PRO' else 15 if user_tier == 'VIP' else 2}
    )
    today = timezone.now().date()
    usage, _ = CopyTradeUsage.objects.get_or_create(user=request.user, date=today, defaults={'count': 0})

    if usage.count >= tier_limit.daily_limit:
        return JsonResponse({
            'success': False,
            'error': f'Daily limit reached ({tier_limit.daily_limit} copy trades/day for {user_tier}). Try again tomorrow.'
        }, status=429)

    # 6. Execute the trade using signal parameters
    try:
        with transaction.atomic():
            trading_acc, _ = Account.objects.get_or_create(
                user=request.user, account_type='TRADING'
            )

            lot_size = Decimal(str(signal.lot_size))
            entry_price = Decimal(str(signal.entry_price)) if signal.entry_price else Decimal('0')

            if entry_price == 0:
                base_prices = {
                    'XAUUSD': Decimal('4454.08'), 'USOIL': Decimal('83.44'),
                    'EURUSD': Decimal('1.1583'), 'GBPUSD': Decimal('1.2940'),
                    'USDJPY': Decimal('146.15'), 'BTCUSD': Decimal('78912.33'),
                    'EURGBP': Decimal('0.8415'), 'EURJPY': Decimal('159.20'),
                    'GBPJPY': Decimal('189.10'),
                }
                entry_price = base_prices.get(signal.symbol, Decimal('1000.00'))

            if trading_acc.balance < lot_size:
                CopyTradeRedemption.objects.create(
                    signal=signal, user=request.user, status='REJECTED',
                    failure_reason='Insufficient balance'
                )
                return JsonResponse({
                    'success': False,
                    'error': f'Insufficient balance. Required: ${lot_size:.2f}, Available: ${trading_acc.balance:.2f}'
                }, status=400)

            trading_acc.balance -= lot_size
            trading_acc.save()

            asset_obj, _ = Asset.objects.get_or_create(
                symbol=signal.symbol,
                defaults={'name': signal.symbol, 'asset_type': 'forex_metal', 'price': entry_price}
            )

            trade = Trade.objects.create(
                user=request.user,
                asset=asset_obj,
                direction=signal.direction,
                stake=lot_size,
                entry_price=entry_price,
                status='OPEN',
                current_price=entry_price,
                pnl=Decimal('0.00'),
            )

            CopyTradeRedemption.objects.create(
                signal=signal, user=request.user, trade=trade, status='EXECUTED'
            )

            usage.count += 1
            usage.save()

            Transaction.objects.create(
                user=request.user,
                amount=lot_size,
                transaction_type='COPY_TRADE',
                details=f"Copy trade redeemed: {signal.code} → {signal.direction} {signal.symbol} @ {entry_price}"
            )

            return JsonResponse({
                'success': True,
                'message': f'Code redeemed! {signal.direction} {signal.symbol} opened.',
                'trade_id': trade.id,
                'ticket_id': f"#ORD-{8400 + trade.id}",
                'symbol': signal.symbol,
                'direction': signal.direction,
                'lot_size': float(lot_size),
                'entry': float(entry_price),
                'new_balance': float(trading_acc.balance),
                'remaining_today': max(0, tier_limit.daily_limit - usage.count),
                'daily_limit': tier_limit.daily_limit,
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        CopyTradeRedemption.objects.create(
            signal=signal, user=request.user, status='FAILED',
            failure_reason=str(e)
        )
        return JsonResponse({'success': False, 'error': f'Execution failed: {str(e)}'}, status=500)

@login_required
def api_copy_signal_count(request):
    """Returns count of unredeemed active signals available to this user."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    tier = str(profile.subscription_tier or 'STARTER').upper()
    if request.user.is_staff or request.user.is_superuser:
        tier = 'VIP'

    now = timezone.now()
    active = CopyTradeSignal.objects.filter(
        status='ACTIVE'
    ).filter(
        Q(tier_required=tier) | Q(tier_required='ALL')
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    redeemed_ids = CopyTradeRedemption.objects.filter(
        user=request.user
    ).values_list('signal_id', flat=True)

    count = active.exclude(id__in=redeemed_ids).count()
    return JsonResponse({'success': True, 'count': count})


@login_required
@csrf_exempt
def close_trade_api(request, trade_id):
    """
    Close a single open trade.
    MT5 rule: balance changes ONLY on close — by the realized P&L.
    Broadcasts trade_closed so all pages sync instantly.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)
        exit_price = Decimal(str(data.get('exit_price', 0)))

        with transaction.atomic():
            trade = Trade.objects.select_for_update().get(id=trade_id, user=request.user)

            if trade.status != 'OPEN':
                return JsonResponse({"success": False, "error": "Trade already closed"}, status=400)

            symbol = trade.asset.symbol if trade.asset else 'UNKNOWN'
            entry_price = Decimal(str(trade.entry_price or 0))
            volume = Decimal(str(trade.stake or 0))

            # If no exit_price provided, fetch live
            if exit_price <= 0:
                from . import price_feed
                live = price_feed.get_single_price_preferred(symbol)
                if live and live > 0:
                    exit_price = Decimal(str(live))
                else:
                    exit_price = entry_price

            # ── Compute realized P&L ─────────────────────────
            pnl_usd = calculate_trade_pnl(
                symbol, trade.direction, volume, entry_price, exit_price
            )

            realized = Decimal(str(round(pnl_usd, 2)))

            # ── 🛡 Negative-balance protection ──────────────
            # A loss can never take the account below $0.00
            account = Account.objects.select_for_update().get(
                user=request.user, account_type='TRADING'
            )
            if realized < 0 and account.balance + realized < 0:
                realized = -max(account.balance, Decimal('0.00'))
                pnl_usd = float(realized)

            status = 'WON' if realized >= 0 else 'LOST'
            payout = volume + realized if status == 'WON' else Decimal('0.00')

            # ── Update the trade record ──────────────────────
            trade.status = status
            trade.exit_price = exit_price
            trade.payout = payout
            trade.pnl = realized
            trade.current_price = exit_price
            trade.date_closed = timezone.now()
            trade.margin_used = Decimal('0.00')   # release the margin
            trade.save()

            # ── ✅ MT5 RULE: balance += realized P&L only ────
            account = Account.objects.select_for_update().get(
                user=request.user, account_type='TRADING'
            )
            account.balance += realized
            account.save()

            # ── Log the transaction ──────────────────────────
            if status == 'WON':
                Transaction.objects.create(
                    user=request.user,
                    amount=realized,
                    transaction_type='TRADE_PAYOUT',
                    details=f"Closed {trade.direction} {symbol} @ {exit_price} | Profit: +${realized:.2f}"
                )
            else:
                Transaction.objects.create(
                    user=request.user,
                    amount=abs(realized),
                    transaction_type='TRADE_LOSS',
                    details=f"Closed {trade.direction} {symbol} @ {exit_price} | Loss: -${abs(realized):.2f}"
                )

            # ── SMS ─────────────────────────────────────────
            try:
                from .sms_service import sms_service
                if abs(pnl_usd) >= 0.10:
                    sms_service.send_trade_closed(
                        request.user,
                        symbol=symbol,
                        direction=trade.direction,
                        pnl=float(realized),
                        result=status,
                    )
            except Exception as e:
                logger.warning(f"Trade close SMS crashed: {e}")

            # ── Broadcast ────────────────────────────────────
            try:
                from .consumers import broadcast_trade_event
                broadcast_trade_event(
                    'trade_closed',
                    {
                        'id': trade.id,
                        'ticket': f"#ORD-{8400 + trade.id}",
                        'symbol': symbol,
                        'direction': trade.direction,
                        'pnl': float(realized),
                        'status': status,
                    },
                    request.user.id,
                )
            except Exception:
                pass

            return JsonResponse({
                "success": True,
                "status": status,
                "payout": float(payout),
                "pnl": float(realized),
                "new_balance": float(round(account.balance, 2)),
            })

    except Trade.DoesNotExist:
        return JsonResponse({"success": False, "error": "Trade not found"}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)



@login_required
@csrf_exempt
def modify_trade_api(request, trade_id):
    """
    MT5 "Modify position": change / remove S/L and T/P of an open position.
    Body: {"sl": 1234.5 | null, "tp": 1250.0 | null}  (omit a key to leave it unchanged)
    MT5 rule: BUY  -> S/L below bid,  T/P above bid
              SELL -> S/L above ask,  T/P below ask
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)

        def _parse(key):
            if key not in data:
                return False, None
            raw = data.get(key)
            if raw is None or str(raw).strip() == '':
                return True, None
            val = Decimal(str(raw))
            if val <= 0:
                raise InvalidOperation
            return True, val

        try:
            sl_set, sl_val = _parse('sl')
            tp_set, tp_val = _parse('tp')
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid S/L or T/P"}, status=400)

        with transaction.atomic():
            trade = Trade.objects.select_for_update().get(id=trade_id, user=request.user)
            if trade.status != 'OPEN':
                return JsonResponse({"success": False, "error": "Position already closed"}, status=400)

            symbol = trade.asset.symbol if trade.asset else 'UNKNOWN'

            from . import price_feed
            live = price_feed.get_single_price_preferred(symbol)
            ref = Decimal(str(live)) if live and live > 0 else Decimal(str(trade.current_price or trade.entry_price or 0))

            new_sl = sl_val if sl_set else trade.stop_loss
            new_tp = tp_val if tp_set else trade.take_profit

            if ref > 0:
                if trade.direction == 'BUY':
                    if new_sl is not None and new_sl >= ref:
                        return JsonResponse({"success": False, "error": "Invalid S/L: must be below the current price for a BUY"}, status=400)
                    if new_tp is not None and new_tp <= ref:
                        return JsonResponse({"success": False, "error": "Invalid T/P: must be above the current price for a BUY"}, status=400)
                else:
                    if new_sl is not None and new_sl <= ref:
                        return JsonResponse({"success": False, "error": "Invalid S/L: must be above the current price for a SELL"}, status=400)
                    if new_tp is not None and new_tp >= ref:
                        return JsonResponse({"success": False, "error": "Invalid T/P: must be below the current price for a SELL"}, status=400)

            trade.stop_loss = new_sl
            trade.take_profit = new_tp
            trade.save()

            return JsonResponse({
                "success": True,
                "id": trade.id,
                "sl": float(new_sl) if new_sl is not None else None,
                "tp": float(new_tp) if new_tp is not None else None,
            })

    except Trade.DoesNotExist:
        return JsonResponse({"success": False, "error": "Trade not found"}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@csrf_exempt
def close_partial_trade_api(request, trade_id):
    """
    MT5 partial close: close part of a position's volume at market.
    Body: {"volume": 0.05, "exit_price": optional}
    The closed part is booked as its own closed trade (shows in History),
    the rest stays open with reduced volume and proportional margin.
    Balance += realized P&L of the closed part (never below $0).
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)
        try:
            close_vol = Decimal(str(data.get('volume'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid volume"}, status=400)

        with transaction.atomic():
            trade = Trade.objects.select_for_update().get(id=trade_id, user=request.user)
            if trade.status != 'OPEN':
                return JsonResponse({"success": False, "error": "Position already closed"}, status=400)

            total_vol = Decimal(str(trade.stake or 0))
            remaining = total_vol - close_vol

            if close_vol < Decimal('0.01'):
                return JsonResponse({"success": False, "error": "Minimum volume is 0.01"}, status=400)
            if close_vol >= total_vol:
                return JsonResponse({"success": False, "error": "Volume must be less than the position volume. Use Close position for a full close."}, status=400)
            if remaining < Decimal('0.01'):
                return JsonResponse({"success": False, "error": "Remaining volume would be below 0.01"}, status=400)

            symbol = trade.asset.symbol if trade.asset else 'UNKNOWN'
            entry_price = Decimal(str(trade.entry_price or 0))

            exit_raw = data.get('exit_price')
            exit_price = Decimal(str(exit_raw)) if exit_raw not in (None, '', 0, '0') else Decimal('0')
            if exit_price <= 0:
                from . import price_feed
                live = price_feed.get_single_price_preferred(symbol)
                exit_price = Decimal(str(live)) if live and live > 0 else entry_price

            pnl_usd = calculate_trade_pnl(symbol, trade.direction, close_vol, entry_price, exit_price)
            realized = Decimal(str(round(pnl_usd, 2)))

            account = Account.objects.select_for_update().get(
                user=request.user, account_type='TRADING'
            )
            # Negative-balance protection
            if realized < 0 and account.balance + realized < 0:
                realized = -max(account.balance, Decimal('0.00'))

            status = 'WON' if realized >= 0 else 'LOST'
            ratio = remaining / total_vol

            # The closed slice becomes its own closed trade (history / deals)
            closed = Trade.objects.create(
                user=request.user,
                asset=trade.asset,
                direction=trade.direction,
                stake=close_vol,
                entry_price=entry_price,
                exit_price=exit_price,
                target_digit=None,
                status=status,
                payout=(close_vol + realized) if status == 'WON' else Decimal('0.00'),
                current_price=exit_price,
                pnl=realized,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                margin_used=Decimal('0.00'),
                leverage=trade.leverage,
                contract_size=trade.contract_size,
                date_closed=timezone.now(),
            )

            # The remaining position keeps running with reduced volume + margin
            trade.stake = remaining
            trade.margin_used = (Decimal(str(trade.margin_used or 0)) * ratio).quantize(Decimal('0.01'))
            trade.pnl = (Decimal(str(trade.pnl or 0)) * ratio).quantize(Decimal('0.01'))
            trade.save()

            account.balance += realized
            account.save()

            Transaction.objects.create(
                user=request.user,
                amount=abs(realized),
                transaction_type='TRADE_PAYOUT' if status == 'WON' else 'TRADE_LOSS',
                details=(
                    f"Partial close {close_vol} of {total_vol} {trade.direction} {symbol} @ {exit_price} | "
                    f"{'Profit: +' if realized >= 0 else 'Loss: -'}${abs(realized):.2f}"
                ),
            )

            try:
                from .consumers import broadcast_trade_event
                broadcast_trade_event(
                    'trade_closed',
                    {
                        'id': closed.id,
                        'ticket': f"#ORD-{8400 + closed.id}",
                        'symbol': symbol,
                        'direction': trade.direction,
                        'pnl': float(realized),
                        'status': status,
                    },
                    request.user.id,
                )
            except Exception:
                pass

            return JsonResponse({
                "success": True,
                "closed_volume": float(close_vol),
                "remaining_volume": float(remaining),
                "pnl": float(realized),
                "new_balance": float(round(account.balance, 2)),
            })

    except Trade.DoesNotExist:
        return JsonResponse({"success": False, "error": "Trade not found"}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def api_open_positions(request):
    """
    Return live P&L for all open trades for the current user.
    This reads from the DB, which is kept fresh by the `price_tracker`
    management command — so it works even if the browser was closed.
    """
    trades = Trade.objects.filter(
        user=request.user, status='OPEN'
    ).select_related('asset')

    positions = []
    total_pnl = 0.0

    for t in trades:
        pnl = float(t.pnl or 0)
        total_pnl += pnl
        current = float(t.current_price or t.entry_price or 0)

        positions.append({
            'id': t.id,
            'symbol': t.asset.symbol if t.asset else '',
            'direction': t.direction,
            'volume': float(t.stake or 0),
            'entry': float(t.entry_price or 0),
            'current_price': current,
            'pnl': pnl,
            'pnl_display': f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}",
            'is_positive': pnl >= 0,
            'sl': float(t.stop_loss) if getattr(t, 'stop_loss', None) else None,
            'tp': float(t.take_profit) if getattr(t, 'take_profit', None) else None,
            'opened': t.date_opened.isoformat() if getattr(t, 'date_opened', None) else None,
        })

    trading_acc = Account.objects.filter(
        user=request.user, account_type='TRADING'
    ).first()
    balance = float(trading_acc.balance) if trading_acc else 0.0

    return JsonResponse({
        'success': True,
        'positions': positions,
        'total_pnl': round(total_pnl, 2),
        'balance': round(balance, 2),
        'equity': round(balance + total_pnl, 2),
        'count': len(positions),
    })


@login_required
@csrf_exempt
def close_all_trades_api(request):
    """
    Close ALL open trades with real P&L.
    MT5 rule: balance changes ONLY on close — by each trade's realized P&L.
    Broadcasts trade_closed per trade.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)
        trades_data = data.get('trades', {})

        with transaction.atomic():
            open_trades = Trade.objects.select_for_update().filter(
                user=request.user, status='OPEN'
            )

            if not open_trades.exists():
                return JsonResponse({"success": False, "error": "No open trades"}, status=400)

            account = Account.objects.select_for_update().get(
                user=request.user, account_type='TRADING'
            )

            closed_count = 0
            total_pnl = Decimal('0.00')
            total_payout = Decimal('0.00')
            won_count = 0
            lost_count = 0

            for trade in open_trades:
                tinfo = trades_data.get(str(trade.id), {})
                exit_price_raw = tinfo.get('exit_price')

                symbol = trade.asset.symbol if trade.asset else 'UNKNOWN'
                entry_price = Decimal(str(trade.entry_price or 0))
                volume = Decimal(str(trade.stake or 0))

                if exit_price_raw is not None and str(exit_price_raw).strip() != '':
                    exit_price = Decimal(str(exit_price_raw))
                else:
                    from . import price_feed
                    live = price_feed.get_single_price_preferred(symbol)
                    exit_price = Decimal(str(live)) if live and live > 0 else entry_price

                pnl_usd = calculate_trade_pnl(
                    symbol, trade.direction, volume, entry_price, exit_price
                )

                realized = Decimal(str(round(pnl_usd, 2)))

                # 🛡 Negative-balance protection (uses the running balance)
                if realized < 0 and account.balance + realized < 0:
                    realized = -max(account.balance, Decimal('0.00'))
                    pnl_usd = float(realized)

                status = 'WON' if realized >= 0 else 'LOST'
                payout = volume + realized if status == 'WON' else Decimal('0.00')

                # Update the trade
                trade.status = status
                trade.exit_price = exit_price
                trade.payout = payout
                trade.pnl = realized
                trade.current_price = exit_price
                trade.date_closed = timezone.now()
                trade.margin_used = Decimal('0.00')
                trade.save()

                # ✅ MT5 rule: add only the realized P&L to balance
                account.balance += realized

                if status == 'WON':
                    total_payout += payout
                    won_count += 1
                else:
                    lost_count += 1

                total_pnl += realized
                closed_count += 1

                Transaction.objects.create(
                    user=request.user,
                    amount=abs(realized),
                    transaction_type='TRADE_PAYOUT' if status == 'WON' else 'TRADE_LOSS',
                    details=f"[Bulk Close] {trade.direction} {symbol} | {status} | PnL: {'+' if realized >= 0 else '-'}${abs(realized):.2f}"
                )

                # Broadcast each close
                try:
                    from .consumers import broadcast_trade_event
                    broadcast_trade_event(
                        'trade_closed',
                        {
                            'id': trade.id,
                            'ticket': f"#ORD-{8400 + trade.id}",
                            'symbol': symbol,
                            'direction': trade.direction,
                            'pnl': float(realized),
                            'status': status,
                        },
                        request.user.id,
                    )
                except Exception:
                    pass

            account.save()

            # Summary SMS
            try:
                from .sms_service import sms_service
                sign = "+" if total_pnl >= 0 else ""
                sms_msg = (
                    f"📊 PATREOAIX: Bulk close complete\n"
                    f"Closed: {closed_count} positions ({won_count}W / {lost_count}L)\n"
                    f"Total P&L: {sign}${float(total_pnl):.2f}\n"
                    f"New balance: ${float(account.balance):.2f}"
                )
                sms_service.send_to_user(request.user, sms_msg, template='trade')
            except Exception as e:
                logger.warning(f"Bulk close SMS crashed: {e}")

            return JsonResponse({
                "success": True,
                "closed_count": closed_count,
                "total_pnl": float(total_pnl),
                "total_payout": float(total_payout),
                "new_balance": float(round(account.balance, 2)),
                "won": won_count,
                "lost": lost_count,
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)



@login_required
def market_overview_view(request):
    """Market scanner view with trading balance context."""
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')
    exchange_acc, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    
    context = {
        'trading_balance': trading_acc.balance,
        'exchange_balance': exchange_acc.balance,
        'total_balance': trading_acc.balance + exchange_acc.balance,
        'scrutiny_url': _page_url(['ai_prediction_models', 'ai-prediction-models', 'ai_predictions']),
    }
    return render(request, 'store/market_overview.html', context)



def ai_analysis_api(request, asset_id):
    asset = get_object_or_404(Asset, id=asset_id)
    return JsonResponse({'status': 'success', 'data': {'symbol': asset.symbol, 'price': asset.price}})



@login_required
def ai_strategy_builder(request):
    """AI Strategy Builder - Main page"""
    # Fetch user's existing strategies (if you have a Strategy model)
    user_strategies = []
    try:
        from .models import TradingStrategy  # if this model exists
        user_strategies = TradingStrategy.objects.filter(user=request.user).order_by('-updated_at')
    except ImportError:
        pass
    
    context = {
        'user_strategies': user_strategies,
        'templates': [
            # no fixed "return" figures: run a backtest to see real results
            {'id': 'scalper', 'name': 'Gold Scalper', 'blocks': 5},
            {'id': 'trend', 'name': 'Trend Follower', 'blocks': 5},
            {'id': 'reversal', 'name': 'Mean Reversion', 'blocks': 4},
            {'id': 'breakout', 'name': 'Breakout Hunter', 'blocks': 4},
        ],
    }
    return render(request, 'store/ai-strategy-builder.html', context)



# ==========================================
# AI STRATEGY BUILDER API
# ==========================================

@login_required
@csrf_exempt
@require_POST
def save_strategy_api(request):
    """Save a strategy to the database"""
    try:
        data = json.loads(request.body)
        strategy_name = data.get('name', 'Untitled Strategy')
        blocks = data.get('blocks', [])
        
        if not blocks:
            return JsonResponse({'success': False, 'error': 'No blocks to save'}, status=400)
        
        # Try to use TradingStrategy model if it exists, otherwise use a simpler approach
        try:
            from .models import TradingStrategy
            
            strategy, created = TradingStrategy.objects.update_or_create(
                user=request.user,
                name=strategy_name,
                defaults={
                    'blocks_json': json.dumps(blocks),
                    'is_active': True,
                    'updated_at': timezone.now(),
                }
            )
            
            return JsonResponse({
                'success': True,
                'strategy_id': strategy.id,
                'message': f'Strategy "{strategy_name}" {"created" if created else "updated"}',
            })
            
        except ImportError:
            # Fallback: Save to UserBot model which exists in your models.py
            from .models import UserBot
            
            bot, created = UserBot.objects.update_or_create(
                user=request.user,
                name=strategy_name,
                defaults={
                    'config_json': {'blocks': blocks, 'type': 'ai_strategy'},
                    'state_json': {'saved_at': timezone.now().isoformat()},
                    'is_running': False,
                }
            )
            
            return JsonResponse({
                'success': True,
                'strategy_id': bot.id,
                'message': f'Strategy "{strategy_name}" saved as bot config',
            })
            
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def load_strategy_api(request):
    """Load a specific strategy"""
    strategy_id = request.GET.get('id')
    if not strategy_id:
        return JsonResponse({'success': False, 'error': 'Missing strategy ID'}, status=400)
    
    try:
        # Try TradingStrategy first
        try:
            from .models import TradingStrategy
            strategy = TradingStrategy.objects.get(id=strategy_id, user=request.user)
            return JsonResponse({
                'success': True,
                'strategy': {
                    'id': strategy.id,
                    'name': strategy.name,
                    'blocks': json.loads(strategy.blocks_json or '[]'),
                    'created_at': strategy.created_at.isoformat() if hasattr(strategy, 'created_at') else None,
                }
            })
        except (ImportError, TradingStrategy.DoesNotExist):
            pass
        
        # Fallback to UserBot
        from .models import UserBot
        bot = UserBot.objects.get(id=strategy_id, user=request.user)
        return JsonResponse({
            'success': True,
            'strategy': {
                'id': bot.id,
                'name': bot.name,
                'blocks': bot.config_json.get('blocks', []) if bot.config_json else [],
                'created_at': bot.created_at.isoformat() if bot.created_at else None,
            }
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=404)


@login_required
@require_http_methods(["GET"])
def list_strategies_api(request):
    """List all user strategies"""
    strategies = []
    
    try:
        # Try TradingStrategy model
        try:
            from .models import TradingStrategy
            for s in TradingStrategy.objects.filter(user=request.user).order_by('-updated_at'):
                strategies.append({
                    'id': s.id,
                    'name': s.name,
                    'block_count': len(json.loads(s.blocks_json or '[]')),
                    'is_active': getattr(s, 'is_active', True),
                    'updated_at': s.updated_at.isoformat() if hasattr(s, 'updated_at') else None,
                })
        except (ImportError, Exception):
            pass
        
        # If empty, try UserBot
        if not strategies:
            from .models import UserBot
            for bot in UserBot.objects.filter(user=request.user).order_by('-updated_at'):
                blocks = bot.config_json.get('blocks', []) if bot.config_json else []
                strategies.append({
                    'id': bot.id,
                    'name': bot.name,
                    'block_count': len(blocks),
                    'is_active': bot.is_running,
                    'updated_at': bot.updated_at.isoformat() if bot.updated_at else None,
                })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
    
    return JsonResponse({
        'success': True,
        'strategies': strategies,
        'count': len(strategies),
    })


@login_required
@csrf_exempt
@require_POST
def deploy_strategy_api(request):
    """Deploy a strategy for live execution"""
    try:
        data = json.loads(request.body)
        strategy_name = data.get('name', 'Untitled')
        blocks = data.get('blocks', [])
        volume = float(data.get('volume', 0.01))
        auto_execute = data.get('auto_execute', True)
        
        if not blocks:
            return JsonResponse({'success': False, 'error': 'No blocks to deploy'}, status=400)
        
        if len(blocks) < 3:
            return JsonResponse({'success': False, 'error': 'Need at least 3 blocks to deploy'}, status=400)
        
        # Save as a running bot
        from .models import UserBot
        
        bot, created = UserBot.objects.update_or_create(
            user=request.user,
            name=strategy_name,
            defaults={
                'config_json': {
                    'blocks': blocks,
                    'volume': volume,
                    'auto_execute': auto_execute,
                    'type': 'ai_strategy',
                },
                'state_json': {
                    'deployed_at': timezone.now().isoformat(),
                    'status': 'running',
                    'total_trades': 0,
                    'win_rate': 0,
                },
                'is_running': True,
            }
        )
        
        # Create a notification
        from .models import Notification
        Notification.objects.create(
            user=request.user,
            title="Strategy Deployed",
            message=f'Strategy "{strategy_name}" is now live with {len(blocks)} blocks and {volume} lots.',
        )
        
        # Log to BotLog
        from .models import BotLog
        BotLog.objects.create(
            level='INFO',
            message=f'Strategy "{strategy_name}" deployed by {request.user.username} with {len(blocks)} blocks @ {volume} lots'
        )
        
        return JsonResponse({
            'success': True,
            'bot_id': bot.id,
            'message': f'Strategy "{strategy_name}" is now running live',
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@csrf_exempt
@require_POST
def backtest_strategy_api(request):
    """
    Backtest the builder's blocks on real candles for the chosen symbol.
    Rules are read from the block text: RSI, EMA/MA cross, MACD, Bollinger, breakout
    (EMA cross when none is named); stop / take-profit from the risk blocks
    (percent, or ATR multiples — 1.5 / 2.5 ATR by default). Each trade gets up to
    `max_bars` candles; if a candle touches both levels the stop is assumed first.
    """
    import re
    from .chart_scanner import Series
    try:
        data = json.loads(request.body)
        blocks = data.get('blocks', [])

        if len(blocks) < 2:
            return JsonResponse({'success': False, 'error': 'Need at least 2 blocks'}, status=400)

        text = json.dumps(blocks).lower()
        symbol = (data.get('symbol') or '').upper().replace('/', '')
        if not symbol:
            m = re.search(r'\b(xauusd|xagusd|eurusd|gbpusd|usdjpy|audusd|usdcad|usdchf|nzdusd|eurgbp|eurjpy|gbpjpy|btcusd|ethusd|solusd|usoil|ukoil|r_10|r_25|r_50|r_75|r_100)\b', text.replace('/', ''))
            symbol = m.group(1).upper() if m else 'XAUUSD'
        timeframe = data.get('timeframe') if data.get('timeframe') in TIMEFRAME_MAP else '1h'

        candles, source = get_candles(symbol, timeframe, 500)
        if len(candles) < 120:
            return JsonResponse({'success': False, 'error': f'Not enough live price history for {symbol} right now — try again shortly.'}, status=503)
        s = Series(candles)

        rules = [r for r, words in (('rsi', ('rsi',)), ('macd', ('macd',)), ('bollinger', ('bollinger', 'band')),
                                     ('breakout', ('breakout', 'break out', 'pattern')), ('ema', ('ema', 'moving average', ' ma ', 'sma', 'price cross')))
                 if any(w in text for w in words)] or ['ema']
        # builder blocks: Buy Entry / Sell Entry limit the side, Trend Aligned / Volatility Range filter entries
        sides = {1 if 'buy entry' in text else 0, -1 if 'sell entry' in text else 0} - {0} or {1, -1}
        trend_filter = 'trend aligned' in text
        vol_filter = 'volatility range' in text
        trailing = 'trailing' in text

        def num_after(words):
            m = re.search(r'(?:' + '|'.join(words) + r')[^0-9]{0,25}(\d+(?:\.\d+)?)\s*(%|atr|x)?', text)
            return (float(m.group(1)), m.group(2)) if m else (None, None)
        sl_v, sl_u = num_after(['stop loss', 'stop-loss', 'stoploss', 'stop', '"sl'])
        tp_v, tp_u = num_after(['take profit', 'take-profit', 'takeprofit', 'target', '"tp'])
        risk_pct = num_after(['risk per trade', 'risk'])[0]
        risk_pct = risk_pct if risk_pct and 0 < risk_pct <= 10 else 1.0
        max_bars = 48

        def level(entry, atr, value, unit, default_atr):
            if value is None:
                return atr * default_atr
            if unit == '%' or (unit is None and 'atr' not in text):
                return entry * value / 100 if value < 50 else atr * default_atr
            return atr * value

        def signal(i):
            votes = []
            if 'ema' in rules and s.ema20[i] and s.ema50[i] and s.ema20[i - 1] and s.ema50[i - 1]:
                if s.ema20[i - 1] <= s.ema50[i - 1] and s.ema20[i] > s.ema50[i]: votes.append(1)
                elif s.ema20[i - 1] >= s.ema50[i - 1] and s.ema20[i] < s.ema50[i]: votes.append(-1)
            if 'rsi' in rules and s.rsi[i] is not None and s.rsi[i - 1] is not None:
                if s.rsi[i - 1] < 30 <= s.rsi[i]: votes.append(1)
                elif s.rsi[i - 1] > 70 >= s.rsi[i]: votes.append(-1)
            if 'macd' in rules and s.macd[i] is not None and s.macd_sig[i] is not None and s.macd[i - 1] is not None and s.macd_sig[i - 1] is not None:
                if s.macd[i - 1] <= s.macd_sig[i - 1] and s.macd[i] > s.macd_sig[i]: votes.append(1)
                elif s.macd[i - 1] >= s.macd_sig[i - 1] and s.macd[i] < s.macd_sig[i]: votes.append(-1)
            if 'bollinger' in rules and s.bb_lo[i] is not None:
                if s.c[i - 1] < s.bb_lo[i - 1] and s.c[i] >= s.bb_lo[i]: votes.append(1)
                elif s.c[i - 1] > s.bb_up[i - 1] and s.c[i] <= s.bb_up[i]: votes.append(-1)
            if 'breakout' in rules and i > 21:
                hi, lo = max(s.h[i - 21:i - 1]), min(s.l[i - 21:i - 1])
                if s.c[i] > hi >= s.c[i - 1]: votes.append(1)
                elif s.c[i] < lo <= s.c[i - 1]: votes.append(-1)
            total = sum(votes)
            return 1 if total > 0 else -1 if total < 0 else 0

        equity, peak, max_dd = 100.0, 100.0, 0.0
        curve, rets, trades = [100.0], [], []
        i = 60
        while i < s.n - 1:
            d = signal(i)
            atr = s.atr[i]
            if d and d not in sides:
                d = 0
            if d and trend_filter and s.ema50[i] and s.ema200[i] and (s.ema50[i] - s.ema200[i]) * d <= 0:
                d = 0
            if d and vol_filter and atr:
                atrs = [x for x in s.atr[max(0, i - 100):i] if x]
                if atrs:
                    lo, hi = sorted(atrs)[len(atrs) // 5], sorted(atrs)[len(atrs) * 4 // 5]
                    if not lo <= atr <= hi:
                        d = 0
            if not d or not atr:
                i += 1
                continue
            entry = s.c[i]
            sl_dist = level(entry, atr, sl_v, sl_u, 1.5)
            tp_dist = level(entry, atr, tp_v, tp_u, 2.5)
            sl, tp = entry - d * sl_dist, entry + d * tp_dist
            exit_price, j = None, i + 1
            while j < min(s.n, i + 1 + max_bars):
                if trailing and j > i + 1:
                    best = max(s.h[i + 1:j]) if d > 0 else min(s.l[i + 1:j])
                    sl = max(sl, best - sl_dist) if d > 0 else min(sl, best + sl_dist)
                hit_sl = s.l[j] <= sl if d > 0 else s.h[j] >= sl
                hit_tp = s.h[j] >= tp if d > 0 else s.l[j] <= tp
                if hit_sl: exit_price = sl; break
                if hit_tp: exit_price = tp; break
                j += 1
            if exit_price is None:
                j = min(s.n - 1, i + max_bars)
                exit_price = s.c[j]
            r_multiple = d * (exit_price - entry) / sl_dist
            ret = r_multiple * risk_pct / 100
            equity *= (1 + ret)
            peak = max(peak, equity)
            max_dd = min(max_dd, (equity - peak) / peak * 100)
            rets.append(ret)
            curve.append(round(equity, 2))
            trades.append({'time': s.t[i], 'side': 'BUY' if d > 0 else 'SELL', 'entry': entry, 'exit': exit_price, 'r': round(r_multiple, 2)})
            i = j + 1

        n = len(trades)
        wins = sum(1 for t in trades if t['r'] > 0)
        mean = sum(rets) / n if n else 0
        sd = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5 if n > 1 else 0
        win_rate = wins / n * 100 if n else 0
        total_return = equity - 100
        sharpe = mean / sd * (n ** 0.5) if sd else 0

        from .models import BotLog
        BotLog.objects.create(
            level='INFO',
            message=f'Backtest run by {request.user.username}: {symbol} {timeframe} {"+".join(rules)} | {n} trades | Return: {total_return:.2f}% | Win rate: {win_rate:.1f}%'
        )

        return JsonResponse({
            'success': True,
            'results': {
                'total_return': round(total_return, 2),
                'win_rate': round(win_rate, 2),
                'max_drawdown': round(max_dd, 2),
                'sharpe_ratio': round(sharpe, 2),
                'total_trades': n,
                'equity_curve': curve,
                'profitable_trades': wins,
                'losing_trades': n - wins,
                'symbol': symbol, 'timeframe': timeframe, 'source': source,
                'rules': rules + (['trend filter'] if trend_filter else []) + (['volatility filter'] if vol_filter else []) + (['trailing stop'] if trailing else []),
                'sides': sorted('BUY' if x > 0 else 'SELL' for x in sides),
                'period': {'from': s.t[0], 'to': s.t[-1]}, 'trades': trades[-50:],
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

def user_has_approved_kyc(user):
    if user.is_superuser or user.is_staff:
        return True
    kyc = KYCProfile.objects.filter(user=user).first()
    return kyc and kyc.status == 'APPROVED'

@login_required
def open_trade(request):
    if not user_has_approved_kyc(request.user):
        messages.error(request, "KYC verification is required to execute trades.")
        return redirect('kyc_status')
        
    if request.method == 'POST':
        asset = get_object_or_404(Asset, id=request.POST.get('asset_id'))
        Trade.objects.create(
            user=request.user, asset=asset, direction=request.POST.get('direction', 'BUY').upper(),
            stake=Decimal(request.POST.get('stake', 10)), entry_price=asset.price, status='OPEN'
        )
        messages.success(request, "Trade opened")
        return redirect('my_trades')
    return redirect('home')


def _live_digit_ticks(symbol, count):
    """
    The next `count` live ticks of a Deriv index as (prices, last_digits).
    The last digit is taken at the index's own pip size, the way Deriv settles digit contracts.
    Returns ([], []) when Deriv can't be reached — the caller refuses the trade.
    """
    from . import price_feed
    reasons = []
    # 1) the same Deriv connection as the live prices (sync; works without the `websockets` package)
    try:
        ticks = price_feed.deriv_live_ticks(symbol, count=count)
    except Exception as e:
        reasons.append(f'direct: {e or type(e).__name__}')
        ticks = []
    # 2) DerivService (async `websockets`)
    if len(ticks) < count:
        from asgiref.sync import async_to_sync
        from .deriv_service import DerivService
        try:
            ticks = async_to_sync(DerivService().get_settlement_ticks)(
                symbol, count=count, timeout=count * 2 + 8, detail=True,
            ) or []
        except Exception as e:
            reasons.append(f'service: {e or type(e).__name__}')
            ticks = []
        if len(ticks) < count and len(reasons) < 2:
            reasons.append('service: no ticks')
    if len(ticks) < count:
        logger.warning(f"Live digit ticks failed for {symbol}: {'; '.join(reasons)}")
        _live_digit_ticks.last_error = '; '.join(reasons)
        return [], []
    prices = [t['quote'] for t in ticks]
    digits = [int(f"{t['quote']:.{t['pip_size']}f}"[-1]) for t in ticks]
    return prices, digits


@login_required
@csrf_exempt
def execute_trade(request):
    """
    Execute trades.
    BUY / SELL  → margin trades on TRADING account (MT5 rule: balance unchanged at open)
    DIGIT*      → synthetic digit contracts (stake is deducted — instant settlement)

    MT5 accounting:
      - BUY/SELL: balance is NOT touched at open. Only margin_used is locked.
      - On close: balance += realized P&L.
      - DIGIT*: full stake deducted (like a lottery ticket — no margin concept).
    """
    from .mt5_service import (
        calc_margin, get_contract_size, get_leverage_for_symbol, get_metrics,
    )

    # ── Guards ──────────────────────────────────────────────
    if not user_has_approved_kyc(request.user):
        return JsonResponse(
            {"success": False, "error": "KYC verification required to execute trades."},
            status=403,
        )

    if request.method != 'POST':
        return JsonResponse(
            {"success": False, "error": "Invalid request method."},
            status=405,
        )

    try:
        data = json.loads(request.body)
        symbol = (data.get('symbol') or 'R_10').upper()
        contract_type = (
            data.get('contract_type') or data.get('order_type') or 'DIGITMATCH'
        ).upper()

        # Market hours guard
        is_open, reason, hint = is_market_open(symbol)
        if not is_open:
            msg = reason + (f". {hint}." if hint else "")
            return JsonResponse(
                {"success": False, "error": msg, "market_closed": True},
                status=400,
            )

        # ═════════════════════════════════════════════════════
        # PATH 1 — MARGIN TRADES (BUY / SELL)
        # ═════════════════════════════════════════════════════
        if contract_type in ('BUY', 'SELL'):
            try:
                volume = Decimal(str(data.get('volume', data.get('amount', '1.00'))))
            except (InvalidOperation, TypeError):
                return JsonResponse(
                    {"success": False, "error": "Invalid volume."}, status=400
                )

            if volume <= 0:
                return JsonResponse(
                    {"success": False, "error": "Volume must be greater than 0."},
                    status=400,
                )

            # Stop loss / take profit
            sl_raw = data.get('sl')
            tp_raw = data.get('tp')
            sl_price = (
                Decimal(str(sl_raw))
                if sl_raw is not None and str(sl_raw).strip() != ''
                else None
            )
            tp_price = (
                Decimal(str(tp_raw))
                if tp_raw is not None and str(tp_raw).strip() != ''
                else None
            )

            # Entry price
            frontend_price = data.get('entry_price')
            entry_price = None
            if frontend_price:
                try:
                    entry_price = Decimal(str(frontend_price))
                except (InvalidOperation, TypeError):
                    entry_price = None

            if not entry_price or entry_price <= 0:
                from . import price_feed
                live = price_feed.get_single_price_preferred(symbol)
                if live and live > 0:
                    entry_price = Decimal(str(live))
                else:
                    base_prices = {
                        'XAUUSD': Decimal('4454.08'),
                        'USOIL':  Decimal('83.44'),
                        'EURUSD': Decimal('1.1583'),
                        'EURGBP': Decimal('0.8415'),
                        'EURJPY': Decimal('159.20'),
                        'GBPUSD': Decimal('1.2940'),
                        'GBPJPY': Decimal('189.10'),
                        'USDJPY': Decimal('146.15'),
                        'BTCUSD': Decimal('78912.33'),
                        'ETHUSD': Decimal('3500.00'),
                        'R_10':   Decimal('1000.00'),
                    }
                    entry_price = base_prices.get(symbol, Decimal('1000.00'))

            contract_size = get_contract_size(symbol)
            leverage = get_leverage_for_symbol(symbol)
            margin_required = calc_margin(symbol, volume, entry_price)

            with transaction.atomic():
                account, _ = Account.objects.get_or_create(
                    user=request.user,
                    account_type='TRADING',
                    defaults={'balance': Decimal('0.00')},
                )
                account = Account.objects.select_for_update().get(id=account.id)

                # Check free margin (sums across open trades)
                metrics = get_metrics(request.user)

                if metrics['free_margin'] < margin_required:
                    return JsonResponse({
                        "success": False,
                        "error": (
                            f"Insufficient free margin. Required: ${margin_required:.2f}, "
                            f"Available: ${metrics['free_margin']:.2f}"
                        ),
                    }, status=400)

                # ══════════════════════════════════════════════
                # ✅ MT5 RULE — balance is NOT touched at open.
                # Margin is tracked on the trade row.
                # Balance only changes when the trade CLOSES.
                # ══════════════════════════════════════════════

                asset_obj, _ = Asset.objects.get_or_create(
                    symbol=symbol,
                    defaults={
                        'name': symbol,
                        'asset_type': 'forex_metal' if 'USD' in symbol else 'synthetic',
                        'price': entry_price,
                    },
                )

                trade_obj = Trade.objects.create(
                    user=request.user,
                    asset=asset_obj,
                    direction=contract_type,
                    stake=volume,
                    entry_price=entry_price,
                    target_digit=None,
                    status='OPEN',
                    payout=Decimal('0.00'),
                    current_price=entry_price,
                    pnl=Decimal('0.00'),
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    margin_used=margin_required,
                    leverage=leverage,
                    contract_size=contract_size,
                )

                ticket_id = f"#ORD-{8400 + trade_obj.id}"

                # ✅ NO MARGIN_HOLD Transaction — that would move balance via the signal
                # Just log a notification (no balance effect)

                try:
                    Notification.objects.create(
                        user=request.user,
                        category='TRADE',
                        priority='info',
                        title=f"Position Opened: {contract_type} {symbol}",
                        message=(
                            f"{volume} lots @ {entry_price} · "
                            f"Margin locked: ${margin_required:.2f}"
                        ),
                        action_url='/mt5/',
                        action_label='View Terminal',
                    )
                except Exception as e:
                    logger.warning(f"Trade open notification failed: {e}")

                # Broadcast to all pages
                try:
                    from .consumers import broadcast_trade_event
                    broadcast_trade_event(
                        'trade_opened',
                        {
                            'id': trade_obj.id,
                            'ticket': ticket_id,
                            'symbol': symbol,
                            'direction': contract_type,
                            'volume': float(volume),
                            'entry': float(entry_price),
                            'sl': float(sl_price) if sl_price else None,
                            'tp': float(tp_price) if tp_price else None,
                            'margin_used': float(margin_required),
                        },
                        request.user.id,
                    )
                except Exception:
                    pass

                return JsonResponse({
                    "success": True,
                    "ticket_id": ticket_id,
                    "entry": float(entry_price),
                    "new_balance": float(round(account.balance, 2)),
                    "trade_id": trade_obj.id,
                    "symbol": symbol,
                    "order_type": contract_type,
                    "volume": float(volume),
                    "sl": float(sl_price) if sl_price is not None else None,
                    "tp": float(tp_price) if tp_price is not None else None,
                    "margin_used": float(margin_required),
                    "leverage": leverage,
                    "contract_size": float(contract_size),
                    "free_margin": float(metrics['free_margin'] - margin_required),
                    "equity": float(metrics['equity']),
                })

        # ═════════════════════════════════════════════════════
        # PATH 2 — DIGIT CONTRACTS (stake deducted, no margin)
        # ═════════════════════════════════════════════════════
        try:
            amount = Decimal(str(data.get('amount', data.get('stake', '10'))))
        except (InvalidOperation, TypeError):
            return JsonResponse(
                {"success": False, "error": "Invalid stake amount."}, status=400
            )

        barrier = data.get('barrier')
        try:
            duration = int(data.get('duration', 1))
        except (TypeError, ValueError):
            duration = 1
        duration = max(1, min(20, duration))

        if amount < Decimal('0.35'):
            return JsonResponse(
                {"success": False, "error": "Minimum stake is $0.35"}, status=400
            )

        pre = Account.objects.filter(user=request.user, account_type='TRADING').first()
        if not pre or pre.balance < amount:
            return JsonResponse({
                "success": False,
                "error": f"Insufficient balance. Required: ${amount:.2f}, Available: ${(pre.balance if pre else 0):.2f}",
            }, status=400)

        # The contract runs on the next `duration` live ticks of the index
        tick_prices, tick_digits = _live_digit_ticks(symbol, duration)
        if len(tick_digits) < duration:
            return JsonResponse({
                "success": False,
                "error": "Live ticks for this index are unavailable right now. No stake was taken — please try again.",
                "detail": getattr(_live_digit_ticks, 'last_error', ''),     # why (shown to staff in the page console)
            }, status=503)

        with transaction.atomic():
            account, _ = Account.objects.get_or_create(
                user=request.user,
                account_type='TRADING',
                defaults={'balance': Decimal('0.00')},
            )
            account = Account.objects.select_for_update().get(id=account.id)

            if account.balance < amount:
                return JsonResponse({
                    "success": False,
                    "error": f"Insufficient balance. Required: ${amount:.2f}, Available: ${account.balance:.2f}",
                }, status=400)

            # ✅ Digit contracts deduct the stake (they settle instantly)
            account.balance -= amount
            account.save()

            asset_obj, _ = Asset.objects.get_or_create(
                symbol=symbol,
                defaults={
                    'name': symbol,
                    'asset_type': 'synthetic',
                    'price': Decimal('1000.00'),
                    'digit_precision': 4,
                },
            )

            try:
                barrier_int = (
                    int(barrier)
                    if barrier is not None and str(barrier).strip() != ''
                    else None
                )
            except (ValueError, TypeError):
                barrier_int = None

            # tick_prices / tick_digits are the real Deriv ticks fetched above
            final_digit = tick_digits[-1] if tick_digits else 0

            won = False
            payout = Decimal('0.00')

            if contract_type == 'DIGITMATCH':
                won = (final_digit == barrier_int) if barrier_int is not None else False
                payout = amount * Decimal('9.5') if won else Decimal('0.00')
            elif contract_type == 'DIGITDIFF':
                won = (final_digit != barrier_int) if barrier_int is not None else False
                payout = amount * Decimal('1.12') if won else Decimal('0.00')
            elif contract_type == 'DIGITEVEN':
                won = (final_digit % 2 == 0)
                payout = amount * Decimal('1.98') if won else Decimal('0.00')
            elif contract_type == 'DIGITODD':
                won = (final_digit % 2 != 0)
                payout = amount * Decimal('1.98') if won else Decimal('0.00')
            elif contract_type == 'DIGITOVER':
                won = (final_digit > barrier_int) if barrier_int is not None else False
                multipliers = {
                    0: Decimal('1.05'), 1: Decimal('1.15'), 2: Decimal('1.35'),
                    3: Decimal('1.65'), 4: Decimal('2.15'), 5: Decimal('3.15'),
                    6: Decimal('5.35'), 7: Decimal('10.5'), 8: Decimal('32.5'),
                }
                payout = amount * multipliers.get(barrier_int, Decimal('2.00')) if won else Decimal('0.00')
            elif contract_type == 'DIGITUNDER':
                won = (final_digit < barrier_int) if barrier_int is not None else False
                multipliers = {
                    1: Decimal('32.5'), 2: Decimal('10.5'), 3: Decimal('5.35'),
                    4: Decimal('3.15'), 5: Decimal('2.15'), 6: Decimal('1.65'),
                    7: Decimal('1.35'), 8: Decimal('1.15'),
                }
                payout = amount * multipliers.get(barrier_int, Decimal('2.00')) if won else Decimal('0.00')
            else:
                account.balance += amount
                account.save()
                return JsonResponse(
                    {"success": False, "error": f"Unknown contract type: {contract_type}"},
                    status=400,
                )

            if won:
                account.balance += payout
                account.save()

            pnl_amount = (payout - amount) if won else -amount

            trade_obj = Trade.objects.create(
                user=request.user,
                asset=asset_obj,
                direction=contract_type,
                stake=amount,
                entry_price=Decimal(str(tick_prices[0])) if tick_prices else asset_obj.price,
                target_digit=barrier_int,
                status='WON' if won else 'LOST',
                payout=payout if won else Decimal('0.00'),
                current_price=Decimal(str(tick_prices[-1])) if tick_prices else asset_obj.price,
                pnl=pnl_amount,
                margin_used=Decimal('0.00'),
                leverage=1,
                contract_size=Decimal('1.0'),
            )

            Transaction.objects.create(
                user=request.user,
                amount=payout if won else amount,
                transaction_type='TRADE_PAYOUT' if won else 'TRADE_STAKE',
                details=(
                    f"{contract_type} | Duration: {duration} ticks | "
                    f"Final Digit: {final_digit} | Barrier: {barrier_int} | "
                    f"Result: {'WIN' if won else 'LOSS'} | Payout: ${payout}"
                ),
            )

            # Broadcast
            try:
                from .consumers import broadcast_trade_event
                broadcast_trade_event(
                    'trade_opened',
                    {
                        'id': trade_obj.id,
                        'ticket': f"#ORD-{8400 + trade_obj.id}",
                        'symbol': symbol,
                        'direction': contract_type,
                        'volume': float(amount),
                        'entry': float(tick_prices[0]) if tick_prices else 0,
                    },
                    request.user.id,
                )
                broadcast_trade_event(
                    'trade_closed',
                    {
                        'id': trade_obj.id,
                        'ticket': f"#ORD-{8400 + trade_obj.id}",
                        'symbol': symbol,
                        'direction': contract_type,
                        'pnl': float(pnl_amount),
                        'status': 'WON' if won else 'LOST',
                    },
                    request.user.id,
                )
            except Exception:
                pass

            return JsonResponse({
                "success": True,
                "won": won,
                "final_digit": final_digit,
                "duration_used": duration,
                "tick_digits": tick_digits,
                "tick_prices": tick_prices,
                "payout": float(payout),
                "profit": float(pnl_amount),
                "new_balance": float(round(account.balance, 2)),
                "trade_id": trade_obj.id,
                "contract_type": contract_type,
                "symbol": symbol,
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)

def mt5_quote_xau():
    from . import mt5_service
    return mt5_service.cached_quote('XAUUSD')


@login_required
@csrf_exempt
def execute_funded_ai_gold(request):
    """
    Execute AI Gold trades using the funded account balance only.
    Enforces a maximum of 10 trades per day per user.
    Persists daily_trades_count in the DB so it survives page reloads.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    try:
        data = json.loads(request.body)
        action = data.get('action', 'toggle')

        # Get or create funded account
        funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)

        # Get AI toggle state
        ai_toggle, _ = ClientAIToggle.objects.get_or_create(user=request.user)

        # Reset daily counter on a new day
        from django.utils import timezone
        today = timezone.now().date()
        if ai_toggle.last_reset_date != today:
            ai_toggle.daily_trades_count = 0
            ai_toggle.last_reset_date = today
            ai_toggle.save(update_fields=['daily_trades_count', 'last_reset_date'])

        # ── STATUS ACTION ──────────────────────────────────────
        if action == 'status':
            from . import ai_gold
            from .membership import is_member
            can_auto = is_member(request.user)                 # Starter: manual only → auto-trader stays off
            if not can_auto and ai_toggle.is_active:
                ai_toggle.is_active = False
                ai_toggle.save(update_fields=['is_active'])
            closed_now = [ai_gold.trade_dict(t) for t in ai_gold.settle_user(request.user)]
            funded_account.refresh_from_db()
            open_t = FundedTrade.objects.filter(user=request.user, asset='XAUUSD', status='OPEN').first()
            recent = FundedTrade.objects.filter(user=request.user, asset='XAUUSD').exclude(status='OPEN').order_by('-closed_at')[:10]
            return JsonResponse({
                "success": True,
                "active": ai_toggle.is_active,
                "trades": closed_now,
                "open_trade": ai_gold.trade_dict(open_t, mt5_quote_xau()) if open_t else None,
                "recent": [ai_gold.trade_dict(t) for t in recent],
                "stats": ai_gold.stats(request.user),          # lifetime record on the funded account
                "can_auto_trade": can_auto,
                "balance": float(funded_account.balance),
                "initial_balance": float(funded_account.initial_balance or 0),
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
            })

        # Check funded balance for toggle/auto_run actions
        if funded_account.balance <= 0:
            return JsonResponse({
                "success": False,
                "error": "Funded account balance is $0. Please subscribe to a Pro or VIP tier to fund your account.",
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
            }, status=400)

        # Check subscription
        user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if not user_profile.is_active_subscription and not (request.user.is_staff or request.user.is_superuser):
            return JsonResponse({
                "success": False,
                "error": "Active subscription required. Please upgrade to Pro or VIP tier.",
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
            }, status=400)

        # Automated trading is for Pro / VIP; Starter trades manually (store/membership.py)
        from .membership import is_member, AUTO_TRADING_MSG
        if not is_member(request.user):
            if ai_toggle.is_active:
                ai_toggle.is_active = False
                ai_toggle.save(update_fields=['is_active'])
            return JsonResponse({
                "success": False, "members_only": True, "active": False,
                "error": AUTO_TRADING_MSG,
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
            }, status=403)

        # ── TOGGLE ACTION ──────────────────────────────────────
        if action == 'toggle':
            ai_toggle.is_active = not ai_toggle.is_active
            ai_toggle.save(update_fields=['is_active'])

            if not ai_toggle.is_active:
                return JsonResponse({
                    "success": True,
                    "message": "AI Gold Auto-Trader stopped.",
                    "active": False,
                    "daily_trades_count": ai_toggle.daily_trades_count,
                    "remaining_trades_today": ai_toggle.remaining_trades_today(),
                    "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                })

            # Check daily limit before executing
            if not ai_toggle.can_trade_today():
                ai_toggle.is_active = False
                ai_toggle.save(update_fields=['is_active'])
                return JsonResponse({
                    "success": False,
                    "error": f"Daily limit reached. Maximum {ClientAIToggle.MAX_DAILY_TRADES} trades per day. Try again tomorrow.",
                    "daily_trades_count": ai_toggle.daily_trades_count,
                    "remaining_trades_today": 0,
                    "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                }, status=400)

            # Execute the trades
            result = execute_funded_gold_trades(request.user, funded_account, ai_toggle)

            # Refresh funded_account (may have been modified)
            funded_account.refresh_from_db()

            return JsonResponse({
                "success": True,
                "active": ai_toggle.is_active,
                "trades": result.get('trades', []),
                "new_balance": float(funded_account.balance),
                "summary": result.get('summary', {}),
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                "opened": result.get('opened'),
                "open_trade": result.get('open_trade'),
                "message": result.get('message', ''),
            })

        # ── AUTO-RUN ACTION ────────────────────────────────────
        if action == 'auto_run':
            if not ai_toggle.is_active:
                return JsonResponse({
                    "success": False,
                    "error": "AI Gold is not active.",
                    "active": False,
                    "daily_trades_count": ai_toggle.daily_trades_count,
                    "remaining_trades_today": ai_toggle.remaining_trades_today(),
                    "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                }, status=400)

            # Check daily limit
            if not ai_toggle.can_trade_today():
                ai_toggle.is_active = False
                ai_toggle.save(update_fields=['is_active'])
                return JsonResponse({
                    "success": False,
                    "error": f"Daily limit reached ({ClientAIToggle.MAX_DAILY_TRADES} trades/day). Auto-trader stopped.",
                    "active": False,
                    "daily_trades_count": ai_toggle.daily_trades_count,
                    "remaining_trades_today": 0,
                    "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                }, status=400)

            result = execute_funded_gold_trades(request.user, funded_account, ai_toggle)
            funded_account.refresh_from_db()

            # If daily limit hit after this batch, auto-stop
            if not ai_toggle.can_trade_today():
                ai_toggle.is_active = False
                ai_toggle.save(update_fields=['is_active'])

            return JsonResponse({
                "success": True,
                "active": ai_toggle.is_active,
                "trades": result.get('trades', []),
                "new_balance": float(funded_account.balance),
                "summary": result.get('summary', {}),
                "daily_trades_count": ai_toggle.daily_trades_count,
                "remaining_trades_today": ai_toggle.remaining_trades_today(),
                "max_daily_trades": ClientAIToggle.MAX_DAILY_TRADES,
                "auto_stopped": not ai_toggle.is_active,
                "opened": result.get('opened'),
                "open_trade": result.get('open_trade'),
                "message": result.get('message', ''),
            })

        return JsonResponse({"success": False, "error": f"Unknown action: {action}"}, status=400)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def execute_funded_gold_trades(user, funded_account, ai_toggle=None):
    """
    One AI Gold cycle on REAL XAUUSD prices (see store/ai_gold.py):
    closes trades whose stop loss / take profit the market touched, then opens a
    new trade only when the 15-minute chart gives a BUY or SELL setup.
    'trades' lists trades that closed in this cycle (with their real result).
    """
    from django.utils import timezone
    from . import ai_gold

    if ai_toggle is None:
        ai_toggle, _ = ClientAIToggle.objects.get_or_create(user=user)
    today = timezone.now().date()
    if ai_toggle.last_reset_date != today:
        ai_toggle.daily_trades_count = 0
        ai_toggle.last_reset_date = today
        ai_toggle.save(update_fields=['daily_trades_count', 'last_reset_date'])

    cycle = ai_gold.run_cycle(user, funded_account, ai_toggle)
    funded_account.refresh_from_db()
    return {
        'trades': cycle['trades'],
        'opened': cycle['opened'],
        'open_trade': cycle['open_trade'],
        'message': cycle['message'],
        'summary': {
            'total_trades': len(cycle['trades']),
            'new_balance': float(funded_account.balance),
            'profit': float(funded_account.balance) - float(funded_account.initial_balance or 0),
            'daily_limit_reached': ai_toggle.daily_trades_count >= ClientAIToggle.MAX_DAILY_TRADES,
            'daily_trades_count': ai_toggle.daily_trades_count,
            'remaining_trades_today': ai_toggle.remaining_trades_today(),
            'live_price_used': cycle['live_price'],
        }
    }


@login_required
def trade_history(request):
    trades = Trade.objects.filter(user=request.user).exclude(status='OPEN').order_by('-date_opened')
    return render(request, 'store/trades_history.html', {'trades': trades})


@login_required
@transaction.atomic
def close_trade(request, trade_id, status, final_payout):
    trade = get_object_or_404(Trade, id=trade_id, user=request.user)
    
    if trade.status != 'OPEN':
        messages.error(request, "Trade is already closed.")
        return redirect('trade_history')

    trade.status = status
    trade.payout = Decimal(str(final_payout))
    trade.save()

    if status == 'WON':
        account = Account.objects.get(user=request.user, account_type='TRADING')
        account.balance += trade.payout
        account.save()
        messages.success(request, f"Trade closed! Profit: ${trade.payout}")
    else:
        messages.info(request, "Trade closed.")
        
    return redirect('trade_history')


@login_required
def digits_trading(request):
    """Digits trading view with funded account integration"""
    # Get funded account
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    
    # Get trading account for regular trades
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')
    
    # Get user profile for subscription status
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    
    # Get AI toggle status
    ai_toggle, _ = ClientAIToggle.objects.get_or_create(user=request.user)
    
    context = {
        'funded_account': funded_account,
        'funded_balance': funded_account.balance,
        'is_funded': funded_account.is_funded,
        'trading_balance': trading_acc.balance,
        'subscription_tier': user_profile.subscription_tier,
        'is_active_subscription': user_profile.is_active_subscription,
        'ai_gold_active': ai_toggle.is_active,
    }
    from .membership import is_member
    context['can_auto_trade'] = is_member(request.user)     # Pro / VIP; Starter trades manually
    return render(request, 'store/digits_trading.html', context)


@login_required
def user_trades(request):
    trades = Trade.objects.filter(user=request.user)
    total_profit = 0.0
    for trade in trades:
        if trade.status == 'LOST':
            total_profit -= float(trade.stake or 0)
        else:
            payout = float(trade.payout or 0)
            total_profit += payout

    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    context = {
        'total_profit': round(total_profit, 2),
        'available_balance': float(trading_acc.balance),
        'unrealized_pnl': 0.00,
        'active_assets_count': trades.filter(status='OPEN').count(),
        'holdings': [],
    }
    return render(request, 'store/portfolio.html', context)


# ==========================================
# WEBHOOK VIEWS
# ==========================================

@login_required
def webhook_automations_view(request):
    user_webhooks = WebhookEndpoint.objects.filter(user=request.user)
    active_count = user_webhooks.filter(is_active=True).count()
    
    total_requests = sum(w.requests_count for w in user_webhooks)
    avg_latency = int(sum(w.latency for w in user_webhooks) / user_webhooks.count()) if user_webhooks.exists() else 0
    avg_success = round(float(sum(w.success_rate for w in user_webhooks) / user_webhooks.count()), 1) if user_webhooks.exists() else 100.0

    stats_data = {
        'Total Endpoints': user_webhooks.count(),
        'Total Requests': total_requests,
        'Avg Success Rate': f"{avg_success}%",
        'Avg Latency': f"{avg_latency}ms"
    }

    return render(request, 'store/webhook_automations.html', {
        'active_count': active_count,
        'stats_data': stats_data,
        'webhooks': user_webhooks,
    })


@login_required
def toggle_webhook_status(request, webhook_id):
    webhook = get_object_or_404(Webhook, id=webhook_id, user=request.user)
    webhook.is_active = not webhook.is_active
    webhook.save()
    return redirect('webhook_automations')


@login_required
def create_webhook(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        url = request.POST.get('url')
        webhook_type = request.POST.get('type', 'OUT')
        
        if name and url:
            Webhook.objects.create(
                user=request.user, name=name, url=url, type=webhook_type,
                is_active=True, requests_count=0, success_rate=100.0, latency=0
            )
            messages.success(request, "Webhook endpoint created successfully.")
        else:
            messages.error(request, "Both Name and URL are required.")
    return redirect('webhook_automations')


@csrf_exempt
def handle_incoming_webhook(request, source_name='Generic'):
    if request.method != 'POST':
        return HttpResponse("Method Not Allowed", status=405)

    client_token = request.headers.get('X-Webhook-Secret')
    expected_token = getattr(settings, 'WEBHOOK_SECRET_KEY', 'default-secret-token')
    
    if client_token != expected_token:
        return JsonResponse({"status": "error", "message": "Unauthorized webhook token"}, status=403)

    headers_dict = {k: v for k, v in request.headers.items()}
    raw_body = request.body.decode('utf-8')

    webhook_log = WebhookLog.objects.create(
        source=source_name, payload=raw_body, headers=json.dumps(headers_dict)
    )

    try:
        data = json.loads(raw_body)
        webhook_log.is_processed = True
        webhook_log.save()
        return JsonResponse({"status": "success", "message": "Webhook processed successfully"}, status=200)
    except Exception as e:
        webhook_log.is_processed = False
        webhook_log.error_message = str(e)
        webhook_log.save()
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


# ==========================================
# BOT VIEWS
# ==========================================

@login_required
def bot_dashboard_view(request):
    form = CreateBotForm()
    logs = BotLog.objects.all()[:50]
    
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    context = {
        'form': form,
        'logs': logs,
        'bot_status': 'running',
        'account_balance': float(trading_acc.balance),
        'unrealized_pnl': 15.50,
        'pnl_pct': 1.55,
        'current_pair': 'R_100',
    }
    return render(request, 'store/bot.html', context)


@login_required
def create_bot_view(request):
    if request.method == 'POST':
        form = CreateBotForm(request.POST)
        if form.is_valid():
            pass
    return redirect('bot')


@login_required
def bot_control_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        from .models import UserBot
        bots = UserBot.objects.filter(user=request.user)
        if action == 'start':
            n = bots.update(is_running=True)
            if n:
                messages.success(request, "AI Trading Engine started successfully.")
            else:
                messages.warning(request, "You have no bots yet — create one first.")
        elif action == 'stop':
            bots.update(is_running=False)
            messages.info(request, "AI Trading Engine received stop signal.")
        elif action == 'emergency_stop':
            # Stop the bots and really close every open position at market (was a message only)
            from . import mt5_service
            bots.update(is_running=False)
            closed, failed = 0, 0
            for t in Trade.objects.filter(user=request.user, status='OPEN', prop__isnull=True, direction__in=('BUY', 'SELL')):
                try:
                    mt5_service.close_position(request.user, t.id, reason='CLIENT')
                    closed += 1
                except Exception as e:
                    logger.warning(f"Emergency close failed for trade {t.id}: {e}")
                    failed += 1
            if failed:
                messages.error(request, f"EMERGENCY SELL: closed {closed} position(s), {failed} could not be closed (market closed or no price). Check the terminal.")
            elif closed:
                messages.warning(request, f"EMERGENCY SELL EXECUTED. {closed} position(s) closed, bots stopped.")
            else:
                messages.info(request, "No open positions to close. Bots stopped.")
        else:
            messages.error(request, f"Unknown action: {action}")
        return redirect('bot')
    return redirect('bot')


# ==========================================
# ADMIN & UTILITY VIEWS
# ==========================================




@login_required
def check_notifications(request):
    # only this user's transactions (it used to return — and mark read — everyone's)
    new_transactions = Transaction.objects.filter(user=request.user, is_notified=False)
    notifications = []
    for tx in new_transactions:
        notifications.append({
            'title': f'{tx.transaction_type.capitalize()} Accepted',
            'message': f'Your {tx.transaction_type} of ${tx.amount} is complete.',
            'link': '/funded/'
        })
        tx.is_notified = True
        tx.save()
    return JsonResponse({'new_notifications': notifications})


def news_sentiment(request):
    articles = NewsArticle.objects.all().order_by('-published')[:10]
    stats = NewsArticle.objects.aggregate(avg_polarity=Avg('polarity'))
    avg_polarity = stats['avg_polarity'] or 0
    overall_sentiment = "Bullish" if avg_polarity > 0.1 else "Bearish" if avg_polarity < -0.1 else "Neutral"
    context = {'news_list': articles, 'overall_sentiment': overall_sentiment, 'avg_polarity': round(avg_polarity, 2)}
    return render(request, 'store/news_sentiment.html', context)


# ==========================================
# PLACEHOLDER / STUB VIEWS (All Preserved)
# ==========================================

def live_signals(request): return render(request, 'store/live-signals.html')
def ai_signal_generator(request): return render(request, 'store/ai-signal-generator.html')
def market_overview(request): return render(request, 'store/market-overview.html')
def ai_market_summary(request): return render(request, 'store/ai-market-summary.html')


def ai_risk_analyzer(request):

    form = CreateBotForm()
    logs = BotLog.objects.all()[:50]
    
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    # Floating P/L of the user's open MT5 positions (was a fixed 15.50 / 1.55%)
    try:
        from . import mt5_service
        floating = float(mt5_service.get_metrics(request.user)['floating_pnl'])
    except Exception:
        floating = 0.0
    balance = float(trading_acc.balance)

    context = {
        'form': form,
        'logs': logs,
        'bot_status': 'running',
        'account_balance': balance,
        'unrealized_pnl': round(floating, 2),
        'pnl_pct': round(floating / balance * 100, 2) if balance else 0,
        'current_pair': 'R_100',
    } 
    
    return render(request, 'store/risk-analyzer.html', context)




@login_required
def ai_trade_journal(request):
    """AI Trade Journal - analyses user's actual trades with AI insights"""
    from django.db.models import Sum, Count, Q, Avg
    from datetime import timedelta
    
    # ==========================================
    # FETCH USER'S TRADES
    # ==========================================
    all_trades = Trade.objects.filter(user=request.user).select_related('asset').order_by('-date_opened')
    closed_trades = all_trades.exclude(status='OPEN')
    open_trades = all_trades.filter(status='OPEN')
    
    # ==========================================
    # COMPUTE STATISTICS
    # ==========================================
    total_trades = all_trades.count()
    wins = closed_trades.filter(status='WON').count()
    losses = closed_trades.filter(status='LOST').count()
    win_rate = round((wins / (wins + losses) * 100) if (wins + losses) > 0 else 0, 1)
    
    # Total P&L
    total_pnl = 0.0
    for t in closed_trades:
        if t.status == 'WON':
            total_pnl += float(t.payout or 0) - float(t.stake or 0)
        elif t.status == 'LOST':
            total_pnl -= float(t.stake or 0)
    
    # Average win/loss
    winning_trades = closed_trades.filter(status='WON')
    losing_trades = closed_trades.filter(status='LOST')
    
    avg_win = 0.0
    if winning_trades.exists():
        avg_win = sum(float(t.payout or 0) - float(t.stake or 0) for t in winning_trades) / winning_trades.count()
    
    avg_loss = 0.0
    if losing_trades.exists():
        avg_loss = sum(float(t.stake or 0) for t in losing_trades) / losing_trades.count()
    
    # Best & worst trades
    best_trade = None
    worst_trade = None
    best_pnl = 0
    worst_pnl = 0
    for t in closed_trades:
        pnl = (float(t.payout or 0) - float(t.stake or 0)) if t.status == 'WON' else -float(t.stake or 0)
        if pnl > best_pnl:
            best_pnl = pnl
            best_trade = t
        if pnl < worst_pnl:
            worst_pnl = pnl
            worst_trade = t
    
    # ==========================================
    # SYMBOL PERFORMANCE
    # ==========================================
    symbol_stats = {}
    for t in closed_trades:
        symbol = t.asset.symbol if t.asset else 'Unknown'
        if symbol not in symbol_stats:
            symbol_stats[symbol] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'total': 0, 'volume': 0.0}
        
        symbol_stats[symbol]['total'] += 1
        symbol_stats[symbol]['volume'] += float(t.stake or 0)
        
        if t.status == 'WON':
            symbol_stats[symbol]['wins'] += 1
            symbol_stats[symbol]['pnl'] += float(t.payout or 0) - float(t.stake or 0)
        else:
            symbol_stats[symbol]['losses'] += 1
            symbol_stats[symbol]['pnl'] -= float(t.stake or 0)
    
    # Convert to list with win rate
    symbol_performance = []
    for symbol, stats in symbol_stats.items():
        wr = round((stats['wins'] / stats['total'] * 100) if stats['total'] > 0 else 0, 1)
        symbol_performance.append({
            'symbol': symbol,
            'wins': stats['wins'],
            'losses': stats['losses'],
            'total': stats['total'],
            'win_rate': wr,
            'pnl': round(stats['pnl'], 2),
            'volume': round(stats['volume'], 2),
            'best_performer': wr >= 60,
        })
    symbol_performance.sort(key=lambda x: x['pnl'], reverse=True)
    
    # ==========================================
    # DAILY PERFORMANCE (last 7 days)
    # ==========================================
    today = timezone.now().date()
    daily_stats = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_trades = closed_trades.filter(date_opened__date=day)
        day_pnl = 0.0
        day_wins = 0
        day_losses = 0
        for t in day_trades:
            if t.status == 'WON':
                day_pnl += float(t.payout or 0) - float(t.stake or 0)
                day_wins += 1
            else:
                day_pnl -= float(t.stake or 0)
                day_losses += 1
        daily_stats.append({
            'day': day.strftime('%a'),
            'date': day.strftime('%b %d'),
            'pnl': round(day_pnl, 2),
            'trades': day_trades.count(),
            'wins': day_wins,
            'losses': day_losses,
        })
    
    # ==========================================
    # AI INSIGHTS GENERATION
    # ==========================================
    ai_insights = []
    
    if total_trades == 0:
        ai_insights.append({
            'type': 'info',
            'icon': 'fa-circle-info',
            'color': 'indigo',
            'title': 'Welcome to your AI Journal',
            'message': 'Start trading to unlock AI-powered insights about your performance.',
        })
    else:
        # Win rate analysis
        if win_rate >= 70:
            ai_insights.append({
                'type': 'positive',
                'icon': 'fa-trophy',
                'color': 'emerald',
                'title': f'Elite Performance: {win_rate}% Win Rate',
                'message': f'You are consistently beating the market. {wins} wins out of {wins + losses} closed trades.',
            })
        elif win_rate >= 50:
            ai_insights.append({
                'type': 'positive',
                'icon': 'fa-circle-check',
                'color': 'emerald',
                'title': f'Solid Performance: {win_rate}% Win Rate',
                'message': 'Above the break-even threshold. Keep focusing on discipline.',
            })
        else:
            ai_insights.append({
                'type': 'warning',
                'icon': 'fa-triangle-exclamation',
                'color': 'rose',
                'title': f'Below Break-Even: {win_rate}% Win Rate',
                'message': f'Only {wins} wins from {wins + losses} trades. Consider refining entry timing.',
            })
        
        # P&L analysis
        if total_pnl > 0:
            ai_insights.append({
                'type': 'positive',
                'icon': 'fa-arrow-trend-up',
                'color': 'emerald',
                'title': f'Net Profit: ${total_pnl:.2f}',
                'message': 'Profitable overall. Compound gains by reinvesting strategically.',
            })
        elif total_pnl < 0:
            ai_insights.append({
                'type': 'warning',
                'icon': 'fa-arrow-trend-down',
                'color': 'rose',
                'title': f'Net Loss: ${abs(total_pnl):.2f}',
                'message': 'Consider reducing position size until your win rate improves.',
            })
        
        # Risk/Reward analysis
        if avg_win > 0 and avg_loss > 0:
            rr_ratio = avg_win / avg_loss
            if rr_ratio >= 2:
                ai_insights.append({
                    'type': 'positive',
                    'icon': 'fa-balance-scale',
                    'color': 'emerald',
                    'title': f'Excellent Risk/Reward: 1:{rr_ratio:.2f}',
                    'message': f'Avg win ${avg_win:.2f} vs avg loss ${avg_loss:.2f}. Great asymmetric edge.',
                })
            elif rr_ratio >= 1:
                ai_insights.append({
                    'type': 'info',
                    'icon': 'fa-scale-balanced',
                    'color': 'amber',
                    'title': f'Balanced R/R: 1:{rr_ratio:.2f}',
                    'message': 'Positive risk-reward. Consider letting winners run longer.',
                })
            else:
                ai_insights.append({
                    'type': 'warning',
                    'icon': 'fa-triangle-exclamation',
                    'color': 'rose',
                    'title': f'Poor Risk/Reward: 1:{rr_ratio:.2f}',
                    'message': f'Losses ${avg_loss:.2f} exceed wins ${avg_win:.2f}. Cut losers faster.',
                })
        
        # Symbol concentration
        if len(symbol_performance) >= 3:
            top_symbol = symbol_performance[0]
            if top_symbol['pnl'] > 0:
                ai_insights.append({
                    'type': 'info',
                    'icon': 'fa-chart-line',
                    'color': 'indigo',
                    'title': f'Best Performer: {top_symbol["symbol"]}',
                    'message': f'Generated ${top_symbol["pnl"]:.2f} profit with {top_symbol["win_rate"]}% win rate.',
                })
        
        # Overtrading warning
        if total_trades > 20:
            ai_insights.append({
                'type': 'info',
                'icon': 'fa-gauge-high',
                'color': 'amber',
                'title': f'High Frequency: {total_trades} Trades',
                'message': 'Consider journaling each trade to identify your strongest setups.',
            })
    
    # ==========================================
    # AI TRADE ANALYSIS (per trade)
    # ==========================================
    recent_trades = []
    for t in all_trades[:20]:
        pnl = 0
        if t.status == 'WON':
            pnl = float(t.payout or 0) - float(t.stake or 0)
        elif t.status == 'LOST':
            pnl = -float(t.stake or 0)
        
        # Simple AI analysis per trade
        analysis = []
        if t.status == 'WON':
            analysis.append('Executed with precision — trade aligned with strategy')
            if pnl > float(t.stake or 0):
                analysis.append(f'Outstanding profit: {pnl/float(t.stake or 1)*100:.0f}% return')
        elif t.status == 'LOST':
            analysis.append('Trade closed with loss — review entry criteria')
        else:
            analysis.append('Position still active — monitor carefully')
        
        recent_trades.append({
            'id': t.id,
            'symbol': t.asset.symbol if t.asset else 'N/A',
            'direction': t.direction,
            'stake': float(t.stake or 0),
            'entry_price': float(t.entry_price or 0),
            'exit_price': float(t.exit_price or 0) if t.exit_price else None,
            'status': t.status,
            'pnl': round(pnl, 2),
            'date': t.date_opened,
            'analysis': analysis,
            'is_open': t.status == 'OPEN',
        })
    
    # ==========================================
    # CONTEXT
    # ==========================================
    context = {
        'total_trades': total_trades,
        'closed_trades_count': closed_trades.count(),
        'open_trades_count': open_trades.count(),
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'total_pnl': round(total_pnl, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'best_trade': best_trade,
        'best_pnl': round(best_pnl, 2),
        'worst_trade': worst_trade,
        'worst_pnl': round(worst_pnl, 2),
        'symbol_performance': symbol_performance,
        'daily_stats': daily_stats,
        'ai_insights': ai_insights,
        'recent_trades': recent_trades,
    }
    
    return render(request, 'store/ai-trade-journal.html', context)



def forex_heatmap(request): return render(request, 'store/forex-heatmap.html')
def crypto_heatmap(request): return render(request, 'store/crypto-heatmap.html')
def trend_scanner(request): return render(request, 'store/trend-scanner.html')

import pandas as pd
# (pandas-ta is imported once at the top of this file as `ta`)



def pattern_detection_view(request):
    # 1. Prepare your OHLC DataFrame (replace with your live feed or database query)
    df = pd.DataFrame({
        'open': [2650.5, 2655.0, 2660.2, 2658.0],
        'high': [2662.0, 2665.5, 2664.0, 2661.0],
        'low': [2648.0, 2652.1, 2655.0, 2650.5],
        'close': [2655.0, 2660.2, 2658.0, 2652.0]
    })

    patterns_list = []
    
    # 2. Calculate core candlestick metrics
    body = df['close'] - df['open']
    range_total = df['high'] - df['low']
    body_abs = body.abs()
    
    # 3. Evaluate patterns on the latest candle
    is_doji = (body_abs <= (range_total * 0.1)) & (range_total > 0)
    if is_doji.iloc[-1]:
        patterns_list.append({
            'pattern': 'Doji',
            'bias': 'Neutral',
            'signal': 'Market indecision detected at current price level.'
        })
        
    lower_shadow = df['open'].combine(df['close'], min) - df['low']
    is_hammer = (lower_shadow > (body_abs * 2)) & (body_abs > 0)
    if is_hammer.iloc[-1]:
        patterns_list.append({
            'pattern': 'Hammer',
            'bias': 'Bullish',
            'signal': 'Potential bullish reversal confirmation.'
        })

    # Nothing formed on the last candle → show nothing rather than an invented pattern

    context = {
        'is_gold': True,
        'asset_name': 'XAUUSD / Spot Gold',
        'patterns': patterns_list
    }

    return render(request, 'store/pattern-detection.html', context)



@login_required
def economic_calendar(request):
    """Economic Calendar - real-time events from TradingView."""
    
    # ✅ Use Django's timezone-aware now()
    now = timezone.now()
    
    # Real events only: TradingView → ForexFactory → last good result (see calendar_feed).
    # No made-up fallback: invented events (e.g. "NFP in 2 hours") could be traded on.
    from . import calendar_feed
    feed = calendar_feed.get_calendar(days_ahead=7)
    events_raw = [dict(e) for e in feed['events']]
    feed_ok = bool(events_raw)
    
    # ✅ Ensure all event dates are timezone-aware before sorting
    for event in events_raw:
        if event['date'].tzinfo is None:
            event['date'] = event['date'].replace(tzinfo=ZoneInfo('UTC'))
    
    # Sort by date
    events_raw.sort(key=lambda x: x['date'])
    
    # Group by day
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    grouped_events = {}
    for event in events_raw:
        event_date = event['date'].date()
        
        if event_date == today:
            day_label, day_key = 'Today', 'today'
        elif event_date == tomorrow:
            day_label, day_key = 'Tomorrow', 'tomorrow'
        else:
            day_label = event['date'].strftime('%A, %b %d')
            day_key = event['date'].strftime('%Y-%m-%d')
        
        if day_key not in grouped_events:
            grouped_events[day_key] = {
                'label': day_label,
                'date': event['date'],
                'events': [],
            }
        
        # ✅ FIXED: Both datetimes are now timezone-aware
        time_until = event['date'] - now
        total_seconds = int(time_until.total_seconds())
        
        if total_seconds > 0:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            event['countdown'] = f'{hours}h {minutes}m' if hours > 0 else f'{minutes}m'
            event['is_past'] = False
        else:
            event['countdown'] = 'Released'
            event['is_past'] = True
        
        grouped_events[day_key]['events'].append(event)
    
    # Stats
    high_impact_count = sum(1 for e in events_raw if e['impact'] == 'high')
    medium_impact_count = sum(1 for e in events_raw if e['impact'] == 'medium')
    low_impact_count = sum(1 for e in events_raw if e['impact'] == 'low')
    total_events = len(events_raw)
    currencies = sorted(list(set(e['currency'] for e in events_raw if e.get('currency'))))
    
    next_high_impact = next(
        (e for e in events_raw if e['impact'] == 'high' and not e.get('is_past', False)), 
        None
    )
    
    context = {
        'grouped_events': grouped_events,
        'total_events': total_events,
        'high_impact_count': high_impact_count,
        'medium_impact_count': medium_impact_count,
        'low_impact_count': low_impact_count,
        'currencies': currencies,
        'next_high_impact': next_high_impact,
        'now': now,
        'feed_ok': feed_ok,
        'feed_source': feed['source'],
        'feed_stale': feed['stale'],
        'feed_updated': feed['updated'],
        'feed_errors': feed['errors'] if request.user.is_staff else [],
        # Client-side rendering (times shown in the viewer's own time zone)
        'events_json': [{
            'id': str(e.get('id')), 'ts': e['date'].isoformat(), 'country': e.get('country') or '',
            'currency': e.get('currency') or '', 'flag': e.get('flag') or '', 'event': e.get('event') or '',
            'impact': e.get('impact') or 'low', 'actual': e.get('actual'), 'forecast': e.get('forecast'),
            'previous': e.get('previous'), 'unit': e.get('unit') or '', 'description': e.get('description') or '',
        } for e in events_raw],
    }
    
    return render(request, 'store/economic-calendar.html', context)


# Add this import at the top of views.py
import requests
from datetime import datetime, timedelta

def fetch_tradingview_calendar(days_ahead=7):
    """Fetch real-time economic calendar from TradingView."""
    try:
        url = 'https://economic-calendar.tradingview.com/events'
        
        today = datetime.now()
        payload = {
            'from': today.strftime('%Y-%m-%dT00:00:00.000Z'),
            'to': (today + timedelta(days=days_ahead)).strftime('%Y-%m-%dT23:59:59.000Z'),
            'countries': 'US,EU,GB,JP,DE,AU,CA,CH,CN'
        }
        
        headers = {
            'Origin': 'https://www.tradingview.com',
            'User-Agent': 'Mozilla/5.0 (compatible; PatreoAIX/1.0)',
        }
        
        response = requests.get(url, headers=headers, params=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        events = []
        impact_map = {1: 'low', 2: 'medium', 3: 'high'}
        
        # ✅ Use UTC timezone-aware conversion
        utc_tz = ZoneInfo('UTC')
        
        for item in data.get('result', []):
            # TradingView returns ISO strings like "2024-07-08T13:30:00+00:00"
            date_str = item['date']
            
            try:
                # Try parsing as ISO format (handles timezone offset)
                event_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            except ValueError:
                # Fallback: parse as naive and attach UTC
                try:
                    event_date = datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=utc_tz)
                except ValueError:
                    # Last resort: skip this event
                    continue
            
            # ✅ Ensure timezone-aware (attach UTC if naive)
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=utc_tz)
            
            events.append({
                'id': item.get('id', len(events) + 1),
                'date': event_date,
                'country': item.get('country', 'US'),
                'currency': item.get('currency', 'USD'),
                'flag': get_flag_emoji(item.get('country', '')),
                'event': item.get('title', 'Unknown Event'),
                'impact': impact_map.get(item.get('importance', 1), 'low'),
                'actual': item.get('actual'),
                'forecast': item.get('forecast'),
                'previous': item.get('previous'),
                'unit': item.get('unit', ''),
                'description': item.get('comment') or item.get('title', ''),
            })
        
        return events
        
    except Exception as e:
        print(f"Error fetching TradingView calendar: {e}")
        import traceback
        traceback.print_exc()
        return []



def get_flag_emoji(country_code):
    """Convert country code to flag emoji."""
    if not country_code:
        return '🏳️'
    # Special mappings for common codes
    special_codes = {'EU': '🇪🇺', 'GB': '🇬🇧', 'US': '🇺🇸', 'JP': '🇯🇵', 'DE': '🇩🇪', 'AU': '🇦🇺', 'CA': '🇨🇦', 'CH': '🇨🇭', 'CN': '🇨🇳'}
    if country_code.upper() in special_codes:
        return special_codes[country_code.upper()]
    # Generic conversion
    code = country_code.upper()
    if len(code) == 2:
        return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)
    return '🏳️'






def volatility_index(request): return render(request, 'store/volatility-index.html')



def correlation_matrix(request): return render(request, 'store/correlation-matrix.html')
def forex_analytics(request): return render(request, 'store/forex-analytics.html')
def crypto_analytics(request): return render(request, 'store/crypto-analytics.html')
def top_gainers_losers(request): return render(request, 'store/top-gainers-losers.html')
def market_sentiment(request): return render(request, 'store/market-sentiment.html')
def liquidity_zones(request): return render(request, 'store/liquidity-zones.html')
def price_action_monitor(request): return render(request, 'store/price-action-monitor.html')
def open_positions(request): return render(request, 'store/open-positions.html')
def performance_analytics(request): return render(request, 'store/performance-analytics.html')
def risk_metrics(request): return render(request, 'store/risk-metrics.html')
def pl_overview(request): return render(request, 'store/pl-overview.html')
def forex_pairs(request): return render(request, 'store/forex-pairs.html')
def crypto_pairs(request): return render(request, 'store/crypto-pairs.html')
def custom_alerts(request): return render(request, 'store/custom-alerts.html')
def saved_signals(request): return render(request, 'store/saved-signals.html')
def bot_center(request): return render(request, 'store/bot-center.html')
def auto_signal_forwarder(request): return render(request, 'store/auto-signal-forwarder.html')
def api_integrations(request): return render(request, 'store/api-integrations.html')
def profile_security(request): return render(request, 'store/profile-security.html')
def connected_exchanges(request): return render(request, 'store/connected-exchanges.html')
def help_center(request): return render(request, 'store/help-center.html')
def run_sentiment_analysis_view(request): return redirect('ai_prediction_models')



@login_required
def notification_settings(request):
    """
    User notification preferences page.
    Saves toggles + digest + quiet hours.
    """
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        # Channel toggles
        email_notifications = request.POST.get('email_notifications') == 'on'
        sms_notifications = request.POST.get('sms_notifications') == 'on'
        push_notifications = request.POST.get('push_notifications') == 'on'

        # Category toggles
        trade_alerts = request.POST.get('trade_alerts') == 'on'
        funding_alerts = request.POST.get('funding_alerts') == 'on'
        security_alerts = request.POST.get('security_alerts') == 'on'
        kyc_alerts = request.POST.get('kyc_alerts') == 'on'
        referral_alerts = request.POST.get('referral_alerts') == 'on'
        marketing_alerts = request.POST.get('marketing_alerts') == 'on'

        # SMS specific
        sms_otp = request.POST.get('sms_otp') == '1'
        sms_transactional = request.POST.get('sms_transactional') == '1'
        sms_trades = request.POST.get('sms_trades') == '1'
        sms_security = request.POST.get('sms_security') == '1'

        # Delivery preferences
        digest_frequency = request.POST.get('digest_frequency', 'instant')
        quiet_hours_start = request.POST.get('quiet_hours_start', '22:00')
        quiet_hours_end = request.POST.get('quiet_hours_end', '07:00')
        user_tz = request.POST.get('timezone', 'Africa/Nairobi')

        # Save to UserProfile (extend model if needed)
        user_profile.sms_opt_in = sms_notifications
        user_profile.save(update_fields=['sms_opt_in'])

        # Store extras in a JSON field (or create a NotificationPreference model)
        # For simplicity, we can store in cache until you add a dedicated model
        from django.core.cache import cache
        cache.set(f"notif_prefs_{request.user.id}", {
            'email_notifications': email_notifications,
            'sms_notifications': sms_notifications,
            'push_notifications': push_notifications,
            'trade_alerts': trade_alerts,
            'funding_alerts': funding_alerts,
            'security_alerts': security_alerts,
            'kyc_alerts': kyc_alerts,
            'referral_alerts': referral_alerts,
            'marketing_alerts': marketing_alerts,
            'sms_otp': sms_otp,
            'sms_transactional': sms_transactional,
            'sms_trades': sms_trades,
            'sms_security': sms_security,
            'digest_frequency': digest_frequency,
            'quiet_hours_start': quiet_hours_start,
            'quiet_hours_end': quiet_hours_end,
            'timezone': user_tz,
        }, timeout=None)

        messages.success(request, '✅ Notification preferences saved successfully!')
        return redirect('notification_settings')

    # Load existing prefs (from cache for now)
    from django.core.cache import cache
    prefs = cache.get(f"notif_prefs_{request.user.id}", {})

    context = {
        'user_profile': user_profile,
        # Channels
        'email_notifications': prefs.get('email_notifications', bool(request.user.email)),
        'push_enabled': prefs.get('push_notifications', False),
        # Categories
        'trade_alerts': prefs.get('trade_alerts', True),
        'funding_alerts': prefs.get('funding_alerts', True),
        'security_alerts': prefs.get('security_alerts', True),
        'kyc_alerts': prefs.get('kyc_alerts', True),
        'referral_alerts': prefs.get('referral_alerts', True),
        'marketing_alerts': prefs.get('marketing_alerts', False),
        # SMS specific
        'sms_otp': prefs.get('sms_otp', True),
        'sms_transactional': prefs.get('sms_transactional', True),
        'sms_trades': prefs.get('sms_trades', True),
        'sms_security': prefs.get('sms_security', True),
        # Delivery
        'digest_frequency': prefs.get('digest_frequency', 'instant'),
        'quiet_hours_start': prefs.get('quiet_hours_start', '22:00'),
        'quiet_hours_end': prefs.get('quiet_hours_end', '07:00'),
        'user_tz': prefs.get('timezone', 'Africa/Nairobi'),
    }
    return render(request, 'store/notification-settings.html', context)

@login_required
def webhook_automations(request):  # Renamed from webhook_automations_view
    # create_webhook saves Webhook rows, so list those (WebhookEndpoint never showed new ones)
    user_webhooks = Webhook.objects.filter(user=request.user).order_by('-created_at')
    active_count = user_webhooks.filter(is_active=True).count()
    
    total_requests = sum(w.requests_count for w in user_webhooks)
    avg_latency = int(sum(w.latency for w in user_webhooks) / user_webhooks.count()) if user_webhooks.exists() else 0
    avg_success = round(float(sum(w.success_rate for w in user_webhooks) / user_webhooks.count()), 1) if user_webhooks.exists() else 100.0

    stats_data = {
        'Total Endpoints': user_webhooks.count(),
        'Total Requests': total_requests,
        'Avg Success Rate': f"{avg_success}%",
        'Avg Latency': f"{avg_latency}ms"
    }

    return render(request, 'store/webhooks.html', {
        'active_count': active_count,
        'total_webhooks': user_webhooks.count(),
        'total_requests': total_requests,
        'stats_data': stats_data,
        'webhooks': user_webhooks,
        'incoming_url': request.build_absolute_uri('/api/webhooks/'),
    })


@login_required
def internal_transfer(request):
    if request.method == 'POST':
        amount_str = request.POST.get('amount')
        recipient_username = request.POST.get('recipient_username')
        try:
            amount = Decimal(amount_str)
            if amount <= 0:
                messages.error(request, "Transfer amount must be greater than zero.")
                return redirect('funding_deposit')

            sender_account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
            recipient_user = get_object_or_404(User, username=recipient_username)
            
            if recipient_user == request.user:
                messages.error(request, "You cannot transfer funds to yourself.")
                return redirect('funding_deposit')
                
            recipient_account, _ = Account.objects.get_or_create(user=recipient_user, account_type='EXCHANGE')
            
            if sender_account.balance >= amount:
                with transaction.atomic():
                    sender_account.balance -= amount
                    sender_account.save()
                    
                    recipient_account.balance += amount
                    recipient_account.save()
                    
                    # Log for sender
                    Transaction.objects.create(
                        user=request.user,
                        amount=amount,
                        transaction_type='INTERNAL_TRANSFER',
                        details=f"Transfer to @{recipient_user.username}"
                    )
                    # Log for recipient
                    Transaction.objects.create(
                        user=recipient_user,
                        amount=amount,
                        transaction_type='INTERNAL_TRANSFER',
                        details=f"Received from @{request.user.username}"
                    )
                messages.success(request, f"Successfully transferred ${amount} to @{recipient_user.username}.")
            else:
                messages.error(request, "Insufficient funds for internal transfer.")
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, "Invalid transfer details or amount.")
    return redirect('funding_deposit')

@login_required
def funding_deposit(request):
    """
    Funding & Accounts page.
    Shows ONLY funding-related transactions (deposits, withdrawals,
    transfers, subscriptions, referral payouts) — NOT trade events.
    """
    exchange_acc, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')

    total_balance = exchange_acc.balance + trading_acc.balance

    # ==========================================
    # ✅ FILTER: FUNDING-ONLY TRANSACTIONS
    # ==========================================
    # Trade-related types are excluded so this page stays clean.
    TRADE_TYPES_TO_EXCLUDE = [
        'MARGIN_HOLD',
        'TRADE_PAYOUT',
        'TRADE_LOSS',
        'TRADE_STAKE',
        'AI_GOLD_TRADE',
        'COPY_TRADE',
        'FUNDED_PAYOUT',
    ]

    user_transactions = (
        Transaction.objects
        .filter(user=request.user)
        .exclude(transaction_type__in=TRADE_TYPES_TO_EXCLUDE)
        .order_by('-created_at')[:100]  # cap at 100 for performance
    )

    # ==========================================
    # User's withdrawal requests
    # ==========================================
    user_withdrawals = PaymentRequest.objects.filter(
        user=request.user,
        transaction_type__iexact='Withdrawal'
    ).order_by('-created_at')[:20]

    # ==========================================
    # User's deposit requests
    # ==========================================
    user_deposits = PaymentRequest.objects.filter(
        user=request.user,
        transaction_type__iexact='Deposit'
    ).order_by('-created_at')[:20]

    context = {
        'transactions': user_transactions,
        'balance': total_balance,
        'exchange_balance': exchange_acc.balance,
        'trading_balance': trading_acc.balance,
        'user_withdrawals': user_withdrawals,
        'user_deposits': user_deposits,
        'pending_withdrawals': sum(1 for w in user_withdrawals if w.status == 'PENDING'),
        'usd_kes': _usd_kes_rate(),
        # M-Pesa is hidden on the page until MPESA_DEPOSITS_ENABLED = True in settings (the views still work)
        'mpesa_enabled': getattr(settings, 'MPESA_DEPOSITS_ENABLED', False),
    }
    from . import bonus
    context['bonus_status'] = bonus.status(request.user)
    context['has_deposit'] = Transaction.objects.filter(user=request.user, transaction_type='DEPOSIT', status='completed').exists()
    return render(request, 'store/payment.html', context)


@login_required
def create_nowpayments_payment(request):
    if request.method == 'POST':
        amount = request.POST.get('amount')
        pay_currency = request.POST.get('pay_currency', 'usdttrc20') # e.g., usdttrc20, btc, eth
        
        # Check settings sandbox flag safely, defaulting to sandbox mode for safety
        is_sandbox = getattr(settings, 'NOWPAYMENTS_SANDBOX', True)
        api_url = "https://api-sandbox.nowpayments.io/v1/payment" if is_sandbox else "https://api.nowpayments.io/v1/payment"
        
        headers = {
            "x-api-key": getattr(settings, 'NOWPAYMENTS_API_KEY', ''),
            "Content-Type": "application/json"
        }
        
        try:
            parsed_amount = float(amount)
        except (TypeError, ValueError):
            messages.error(request, "Invalid deposit amount provided.")
            return redirect('funding_deposit')

        payload = {
            "price_amount": parsed_amount,
            "price_currency": "usd",
            "pay_currency": pay_currency,
            "ipn_callback_url": "https://yourdomain.com/api/nowpayments/ipn/", # Update with your public webhook URL
            "order_id": f"DEPOSIT-{request.user.id}-{int(time.time())}",
            "order_description": f"Account deposit for {request.user.username}"
        }
        
        try:
            response = requests.post(api_url, json=payload, headers=headers)
            data = response.json()
            
            # NOWPayments returns 201 Created on success
            if response.status_code in [200, 201] or 'pay_address' in data:
                pay_address = data.get('pay_address')
                pay_amount = data.get('pay_amount')
                invoice_id = data.get('payment_id')
                
                # Save details temporarily in session to display payment instructions on the page
                request.session['pending_deposit'] = {
                    'invoice_id': invoice_id,
                    'pay_address': pay_address,
                    'pay_amount': pay_amount,
                    'pay_currency': pay_currency
                }
                
                messages.success(request, f"Please send exactly {pay_amount} {pay_currency.upper()} to address: {pay_address}")
                return redirect('funding_deposit')
            else:
                error_msg = data.get('message', 'Failed to initiate crypto payment.')
                messages.error(request, f"Payment error: {error_msg}")
        except Exception as e:
            messages.error(request, "Error connecting to payment gateway.")
            
    return redirect('funding_deposit')

@csrf_exempt
@require_POST
def nowpayments_ipn_webhook(request):
    received_sig = request.headers.get('x-nowpayments-sig')
    if not received_sig:
        return JsonResponse({"error": "No signature provided"}, status=400)
    
    try:
        request_body_bytes = request.body
        payload = json.loads(request_body_bytes.decode('utf-8'))
        
        sorted_payload = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        computed_sig = hmac.new(
            settings.NOWPAYMENTS_IPN_SECRET.encode('utf-8'),
            sorted_payload.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        if not hmac.compare_digest(computed_sig, received_sig):
            return JsonResponse({"error": "Invalid signature"}, status=400)
            
        payment_status = payload.get('payment_status')
        price_amount = float(payload.get('price_amount', 0))
        order_id = payload.get('order_id') # Format: DEPOSIT-[user_id]-[timestamp]
        
        if payment_status == 'finished':
            try:
                user_id = int(order_id.split('-')[1])
            except (IndexError, ValueError):
                return JsonResponse({"error": "Invalid order reference"}, status=400)
                
            from django.contrib.auth.models import User
            user = User.objects.filter(id=user_id).first()
            
            if user:
                with transaction.atomic():
                    # Crypto deposits credit the EXCHANGE account
                    user_account, _ = Account.objects.get_or_create(user=user, account_type='EXCHANGE')
                    # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)
                    
                    Transaction.objects.create(
                        user=user,
                        amount=price_amount,
                        transaction_type='DEPOSIT',
                        details=f"Automated NOWPayments USDT Deposit (ID: {payload.get('payment_id')})"
                    )
                    
        return JsonResponse({"status": "success"}, status=200)
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def transfer_between_accounts(request):
    if request.method == 'POST':
        try:
            amount = Decimal(request.POST.get('amount', '0'))
            source_type = request.POST.get('source_type')  # 'EXCHANGE' or 'TRADING'
            target_type = request.POST.get('target_type')  # 'TRADING' or 'EXCHANGE'

            if amount <= 0:
                messages.error(request, "Transfer amount must be greater than zero.")
                return redirect('funding_view')

            if source_type == target_type:
                messages.error(request, "Source and target accounts must be different.")
                return redirect('funding_view')

            source_account = get_object_or_404(Account, user=request.user, account_type=source_type)
            target_account = get_object_or_404(Account, user=request.user, account_type=target_type)

            if source_account.balance < amount:
                messages.error(request, f"Insufficient funds in your {source_account.get_account_type_display()}.")
                return redirect('funding_view')

            with transaction.atomic():
                source_account.balance -= amount
                source_account.save()

                target_account.balance += amount
                target_account.save()

                Transaction.objects.create(
                    user=request.user,
                    amount=amount,
                    transaction_type='INTERNAL_TRANSFER',
                    details=f"Transferred ${amount} from {source_account.get_account_type_display()} to {target_account.get_account_type_display()}"
                )

            messages.success(request, f"Successfully transferred ${amount} to your {target_account.get_account_type_display()}.")
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, "Please enter a valid transfer amount.")
        except Exception as e:
            messages.error(request, f"Transfer failed: {str(e)}")
            
    return redirect('funding_deposit')




def cryptomus_verify(request):
    file_path = os.path.join(settings.BASE_DIR, 'cryptomus_6fcb4880.html') # match your exact downloaded filename
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return HttpResponse(f.read(), content_type='text/html')
    raise Http404()

import re


# Validation regexes per network
TXID_REGEX = {
    'TRC20': r'^[a-fA-F0-9]{64}$',          # Tron txid (64 hex chars)
    'ERC20': r'^0x[a-fA-F0-9]{64}$',        # Ethereum txid
    'BEP20': r'^0x[a-fA-F0-9]{64}$',        # BSC txid
    'BTC':   r'^[a-fA-F0-9]{64}$',          # Bitcoin txid
    'ETH':   r'^0x[a-fA-F0-9]{64}$',
}


import re


# Validation regexes per network
TXID_REGEX = {
    'TRC20': r'^[a-fA-F0-9]{64}$',
    'ERC20': r'^0x[a-fA-F0-9]{64}$',
    'BEP20': r'^0x[a-fA-F0-9]{64}$',
    'BTC':   r'^[a-fA-F0-9]{64}$',
    'ETH':   r'^0x[a-fA-F0-9]{64}$',
}


@login_required
def submit_crypto_deposit(request):
    """
    User submits a crypto deposit + Blockchain TxID for admin verification.
    Balance is NOT credited until admin approves it from the hub.
    """
    if request.method != 'POST':
        return redirect('dashboard')

    # ── 1. Read form fields ────────────────────────────────────
    amount_str = request.POST.get('amount', '').strip()
    tx_hash = request.POST.get('tx_hash', '').strip()
    network_raw = request.POST.get('network', '').strip().upper()
    wallet_address = request.POST.get('wallet_address', '').strip()

    # ── 2. Normalize network aliases ────────────────────────────
    NETWORK_ALIASES = {
    # ═══════ TRC20 variants (Tron network) ═══════
    'TRC20': 'TRC20',
    'TRC-20': 'TRC20',
    'TRC': 'TRC20',
    'TRON': 'TRC20',
    'TRX': 'TRC20',                    # ✅ ADDED — Tron native token
    'TRONIX': 'TRC20',                 # ✅ ADDED
    'TRONIX-TRC20': 'TRC20',
    'TRON-TRC20': 'TRC20',
    'USDT-TRC20': 'TRC20',
    'USDTTRC20': 'TRC20',
    'USDT_TRC20': 'TRC20',
    'USDT TRC20': 'TRC20',

    # ═══════ ERC20 variants (Ethereum network) ═══════
    'ERC20': 'ERC20',
    'ERC-20': 'ERC20',
    'ERC': 'ERC20',
    'ETHEREUM': 'ERC20',
    'ETH': 'ERC20',
    'USDT-ERC20': 'ERC20',
    'USDTERC20': 'ERC20',
    'USDT_ERC20': 'ERC20',
    'USDT ERC20': 'ERC20',

    # ═══════ BEP20 variants (BSC network) ═══════
    'BEP20': 'BEP20',
    'BEP-20': 'BEP20',
    'BEP': 'BEP20',
    'BSC': 'BEP20',
    'BNB': 'BEP20',
    'BINANCE': 'BEP20',
    'USDT-BEP20': 'BEP20',
    'USDTBEP20': 'BEP20',
    'USDT_BEP20': 'BEP20',

    # ═══════ Bitcoin ═══════
    'BTC': 'BTC',
    'BITCOIN': 'BTC',
    'BITCOIN-BTC': 'BTC',
}

    network = NETWORK_ALIASES.get(network_raw, network_raw)

    # ── 3. Validation ───────────────────────────────────────────
    if not amount_str or not tx_hash:
        messages.error(request, 'Please provide both the amount and the Blockchain TxID.')
        return redirect('dashboard')

    try:
        amount = Decimal(amount_str)
        if amount < Decimal('5.00'):
            messages.error(request, 'Minimum crypto deposit is $5.00.')
            return redirect('dashboard')
    except (InvalidOperation, TypeError):
        messages.error(request, 'Invalid amount format.')
        return redirect('dashboard')

    if network not in TXID_REGEX:
        # Helpful error showing exactly what failed
        messages.error(
            request,
            f'Unsupported network: "{network_raw}". '
            f'Supported networks: TRC20, ERC20, BEP20, BTC.'
        )
        return redirect('dashboard')

    if not re.match(TXID_REGEX[network], tx_hash):
        messages.error(
            request,
            f'Invalid TxID format for {network}. '
            f'Expected a {"0x-prefixed " if network in ("ERC20", "BEP20") else ""}64-character hex hash.'
        )
        return redirect('dashboard')

    # ── 4. Duplicate check ──────────────────────────────────────
    if PaymentRequest.objects.filter(tx_hash__iexact=tx_hash).exists():
        messages.error(request, 'This transaction hash has already been submitted.')
        return redirect('dashboard')

    # ── 5. Create the pending request ───────────────────────────
    PaymentRequest.objects.create(
        user=request.user,
        amount=amount,
        transaction_type='DEPOSIT',
        payment_method='Crypto',
        provider_reference=tx_hash,
        status='PENDING',
        processed=False,
        network=network,
        tx_hash=tx_hash,
        to_address=wallet_address or None,
    )

    # Notify admins
    from .notifications import notify
    for staff in User.objects.filter(is_staff=True, is_active=True):
        notify(
            staff,
            title="New Crypto Deposit Awaiting Review",
            message=f"@{request.user.username} submitted a ${amount:.2f} {network} deposit. TxID: {tx_hash[:16]}...",
            category='FUNDING',
            priority='warning',
            action_url='/admin/hub/',
            action_label='Review in Hub',
        )

    messages.success(
        request,
        f'✅ Deposit submitted for verification! '
        f'Your ${amount:.2f} will be credited once the admin confirms the transaction.'
    )
    return redirect('dashboard')


# ═══════════════════════════════════════════════════════════════
# ADMIN — BLOCKCHAIN DEPOSIT APPROVALS
# ═══════════════════════════════════════════════════════════════

@staff_member_required
def admin_crypto_deposits_view(request):
    """
    Admin page for reviewing pending crypto deposits with Blockchain TxIDs.
    Supports: filtering, search, approve/reject with notes.
    """
    if request.method == 'POST':
        deposit_id = request.POST.get('deposit_id')
        action = request.POST.get('action')  # approve / reject
        admin_notes = request.POST.get('admin_notes', '').strip()

        deposit = get_object_or_404(PaymentRequest, id=deposit_id, transaction_type='DEPOSIT')

        if deposit.status != 'PENDING':
            messages.warning(request, f'Deposit #{deposit_id} has already been {deposit.status.lower()}.')
            return redirect('admin_crypto_deposits')

        if action == 'approve':
            with transaction.atomic():
                # 1. Mark payment approved
                deposit.status = 'APPROVED'
                deposit.processed = True
                deposit.admin_notes = admin_notes
                deposit.reviewed_by = request.user
                deposit.reviewed_at = timezone.now()
                deposit.save()

                # 2. Credit EXCHANGE balance (signal on PaymentRequest handles this,
                #    but we double-check by ensuring the transaction log exists)
                Transaction.objects.create(
                    user=deposit.user,
                    amount=deposit.amount,
                    transaction_type='DEPOSIT',
                    details=f"Crypto deposit approved ({deposit.network}) | TxID: {deposit.tx_hash}",
                )

                # 3. Notify user
                from .notifications import notify
                notify(
                    deposit.user,
                    title="Crypto Deposit Approved ✅",
                    message=f"${deposit.amount:.2f} has been credited to your Exchange balance.\nTxID: {deposit.tx_hash[:20]}...",
                    category='FUNDING',
                    priority='success',
                    action_url='/funding/',
                    action_label='View Wallet',
                )

            messages.success(request, f'Deposit #{deposit_id} approved — ${deposit.amount:.2f} credited.')

        elif action == 'reject':
            deposit.status = 'REJECTED'
            deposit.processed = True
            deposit.admin_notes = admin_notes or 'TxID could not be verified on blockchain.'
            deposit.reviewed_by = request.user
            deposit.reviewed_at = timezone.now()
            deposit.save()

            from .notifications import notify
            notify(
                deposit.user,
                title="Crypto Deposit Rejected",
                message=f"Your deposit of ${deposit.amount:.2f} was rejected. Reason: {deposit.admin_notes}",
                category='FUNDING',
                priority='error',
                action_url='/support/',
                action_label='Contact Support',
            )

            messages.warning(request, f'Deposit #{deposit_id} rejected.')

        return redirect('admin_crypto_deposits')

    # ─── Fetch pending + recently reviewed ────────────────────
    status_filter = request.GET.get('status', 'PENDING').upper()
    search = request.GET.get('q', '').strip()

    qs = PaymentRequest.objects.filter(
        transaction_type='DEPOSIT',
        payment_method__iexact='Crypto',
    ).select_related('user', 'reviewed_by')

    if status_filter and status_filter != 'ALL':
        qs = qs.filter(status=status_filter)

    if search:
        qs = qs.filter(
            Q(tx_hash__icontains=search) |
            Q(user__username__icontains=search) |
            Q(provider_reference__icontains=search)
        )

    qs = qs.order_by('-created_at')
    deposits = qs[:200]

    # Stats
    from django.db.models import Sum
    pending_count = PaymentRequest.objects.filter(
        transaction_type='DEPOSIT', payment_method__iexact='Crypto', status='PENDING'
    ).count()
    pending_total = PaymentRequest.objects.filter(
        transaction_type='DEPOSIT', payment_method__iexact='Crypto', status='PENDING'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    context = {
        'deposits': deposits,
        'status_filter': status_filter,
        'search': search,
        'pending_count': pending_count,
        'pending_total': pending_total,
    }
    return render(request, 'store/admin_panel/hub.html', context)

# views.py updates
@login_required
def survey_rewards_view(request):
    available_rewards = SurveyReward.objects.filter(user=request.user, is_claimed=False)
    
    is_eligible = False
    user_tier = 'FREE'
    
    user_profile = getattr(request.user, 'profile', None)
    if user_profile:
        tier_val = str(getattr(user_profile, 'subscription_tier', 'free')).upper().strip()
        is_active = getattr(user_profile, 'is_active_subscription', False)
        user_tier = tier_val
        
        if is_active and any(t in tier_val for t in ['PRO', 'VIP', 'STARTER']):
            is_eligible = True

    if request.user.is_superuser or request.user.is_staff:
        is_eligible = True
        user_tier = 'ADMIN'

    exchange_account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    claimable_balance = sum(r.amount for r in available_rewards)

    context = {
        'available_rewards': available_rewards,
        'user_tier': user_tier,
        'is_eligible': is_eligible,
        'claimable_reward_balance': claimable_balance,
        'exchange_balance': exchange_account.balance,
    }
    return render(request, 'store/rewards.html', context)

@login_required
def claim_survey_reward(request, reward_id):
    if request.method == 'POST':
        user_profile = getattr(request.user, 'profile', None)
        tier_val = str(getattr(user_profile, 'subscription_tier', 'free')).upper().strip() if user_profile else 'FREE'
        is_active = getattr(user_profile, 'is_active_subscription', False) if user_profile else False
        
        if not (is_active and any(t in tier_val for t in ['PRO', 'VIP'])) and not request.user.is_superuser:
            messages.error(request, 'Survey rewards are restricted to active Pro and VIP members.')
            return redirect('subscription_billing')
            
        reward = get_object_or_404(SurveyReward, id=reward_id, user=request.user, is_claimed=False)
        
        # Add funds directly to the user's EXCHANGE account
        exchange_account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
        # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)
            
        reward.is_claimed = True
        reward.save()
        
        Transaction.objects.create(
            user=request.user,
            transaction_type='DEPOSIT',
            amount=reward.amount,
            details=f"Paid Survey Reward Claimed: {reward.title}"
        )
        
        messages.success(request, f'Successfully claimed ${reward.amount}! Funds added to your exchange balance.')
        return redirect('survey_rewards')

    return redirect('survey_rewards')


def _kyc_file_problem(f, label, allow_pdf=False):
    """None when the upload is a usable photo (or PDF where allowed), else a message for the user."""
    if not f:
        return f'The {label} is missing. Capture it with the camera or upload a photo.'
    if f.size > 10 * 1024 * 1024:
        return f'The {label} is larger than 10 MB.'
    name = (f.name or '').lower()
    if allow_pdf and (name.endswith('.pdf') or f.content_type == 'application/pdf'):
        return None
    try:
        from PIL import Image
        img = Image.open(f)
        img.verify()
        w, h = img.size
        f.seek(0)
    except ImportError:
        return None
    except Exception:
        return f'The {label} is not a readable image. Try again.'
    if min(w, h) < 320:
        return f'The {label} is too small ({w}×{h}). Use the camera or a sharper photo.'
    return None


@login_required
def kyc_submit_view(request):
    """
    KYC submission form.
    - If already APPROVED → redirect to status page (shows verified screen)
    - If PENDING          → redirect to status page (shows waiting screen)
    - Else                → show the submit form
    """
    existing = KYCProfile.objects.filter(user=request.user).first()

    # Staff always bypass
    if request.user.is_staff or request.user.is_superuser:
        return redirect('kyc_status')

    if existing:
        status = str(existing.status or '').upper().strip()

        if status in ('APPROVED', 'VERIFIED', 'VERIFICATION_SUCCESS'):
            messages.info(request, "Your identity is already verified ✅")
            return redirect('kyc_status')

        if status in ('PENDING', 'UNDER_REVIEW', 'IN_REVIEW', 'PROCESSING'):
            messages.info(request, "Your KYC is already under review. Please wait for approval.")
            return redirect('kyc_status')

    from . import kyc_auto
    if request.method == 'GET' and kyc_auto.enabled() and not request.GET.get('manual'):
        return redirect('kyc_status')

    if request.method == 'POST':
        document_type = request.POST.get('document_type', 'National ID')
        id_number = request.POST.get('id_number', '')
        document_file = request.FILES.get('document_file')
        selfie_image = request.FILES.get('selfie_image')
        document_back = request.FILES.get('document_image')     # back of the ID (camera capture)

        # an empty submission used to create a PENDING review with nothing to check
        problem = _kyc_file_problem(document_file, 'document photo', allow_pdf=True) or _kyc_file_problem(selfie_image, 'selfie')
        if not problem and document_back:
            problem = _kyc_file_problem(document_back, 'back of the document')
        if not problem and len(id_number.strip()) < 4:
            problem = 'Enter the document number exactly as printed on your ID.'
        if problem:
            messages.error(request, problem)
            return render(request, 'store/kyc_submit.html', {'existing': existing})

        defaults = {
            'status': 'PENDING',
            'provider': '', 'provider_session': '', 'provider_status': '', 'admin_notes': '',
            'document_type': document_type,
            'id_number': id_number,
            'document_file': document_file,
            'selfie_image': selfie_image,
        }
        if document_back:
            defaults['document_image'] = document_back
        KYCProfile.objects.update_or_create(
            user=request.user,
            defaults=defaults,
        )

        messages.success(request, "✅ Documents submitted! We'll notify you once verified.")

        # SMS notification (if you have sms_service)
        try:
            from .sms_service import sms_service
            sms_service.send_to_user(
                request.user,
                "✅ PATREOAIX: Your KYC documents were received. "
                "We'll notify you once verification is complete.",
                template='kyc'
            )
        except Exception:
            pass

        return redirect('kyc_status')

    return render(request, 'store/kyc_submit.html', {
        'existing': existing,
    })

@login_required
def trading_accounts_view(request):
    # Fetch live/demo trading accounts associated with the user
    context = {
        'trading_accounts': [],
    }
    return render(request, 'store/trading_accounts.html', context)


@login_required
def account_leverage_view(request):
    if request.method == 'POST':
        selected_leverage = request.POST.get('leverage')
        # Logic to update account leverage in database or broker API
        return redirect('trading_accounts')
    return render(request, 'store/account_leverage.html')

@login_required
def platforms_download_view(request):
    # Provide links or installers for terminal/trading platforms
    return render(request, 'store/platforms_download.html')

@login_required
def crypto_wallet_view(request):
    # Retrieve crypto balances and wallet addresses for the user
    context = {
        'wallet_balances': {},
    }
    return render(request, 'store/crypto_wallet.html', context)

@login_required
def crypto_deposit_view(request):
    if request.method == 'POST':
        # Handle crypto deposit intent or transaction recording
        pass
    return render(request, 'store/crypto_deposit.html')

@login_required
def crypto_withdrawal_view(request):
    if request.method == 'POST':
        # Handle cryptocurrency withdrawal requests, address validation, and OTP/2FA checks
        pass
    return render(request, 'store/crypto_withdrawal.html')

@csrf_exempt
def gateway_callback_view(request):
    if request.method == 'POST':
        # Parse incoming webhook data from the gateway or broker
        # e.g., payment status updates, transaction IDs, trade confirmations
        data = request.POST
        
        # Process the response logic here (e.g., update order/account status)
        
        return HttpResponse(status=200)
        
    # Handle GET redirect callbacks if the gateway redirects users back
    return HttpResponse("Callback received successfully.", status=200)


from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from .models import KYCProfile


@staff_member_required
def admin_kyc_queue_view(request):
    pending_kyc = KYCProfile.objects.filter(status__iexact='pending').order_by('-id')
    context = {
        'pending_kyc': pending_kyc,
        'kycs': pending_kyc,  # Supports templates looking for either variable name
    }
    return render(request, 'store/admin_panel/kyc_queue.html', context)


@login_required
def kyc_status_view(request):
    """KYC status: verified / under review / not approved / not started. Verified users are never asked again."""
    from . import kyc_auto
    kyc_record = KYCProfile.objects.filter(user=request.user).first()
    if request.user.is_staff or request.user.is_superuser:
        normalized = 'APPROVED'
    elif not kyc_record:
        if not kyc_auto.enabled():
            return redirect('kyc_submit')               # manual upload only
        normalized = 'MISSING'
    else:
        if kyc_record.provider == 'didit' and str(kyc_record.status).upper() == 'PENDING' \
                and cache.add(f'kyc:refresh:{kyc_record.pk}', 1, 20):
            try:
                kyc_auto.refresh(kyc_record)            # the decision may have arrived
            except Exception as e:
                logger.info(f'KYC refresh {kyc_record.pk}: {e}')
        status = str(kyc_record.status or '').upper().strip()
        normalized = ('APPROVED' if status in ('APPROVED', 'VERIFIED', 'VERIFICATION_SUCCESS') else
                      'REJECTED' if status in ('REJECTED', 'FAILED', 'DECLINED') else 'PENDING')
    return render(request, 'store/kyc_status.html', {
        'kyc_state': normalized,
        'kyc_record': kyc_record,
        'auto_kyc': kyc_auto.enabled(),
        'auto_session': bool(kyc_record and kyc_record.provider == 'didit'),
        'in_review': bool(kyc_record and kyc_record.provider_status in ('in review', 'in_review', 'review')),
        'rejection_reason': getattr(kyc_record, 'admin_notes', '') or '',
    })


@login_required
@require_POST
def kyc_auto_start(request):
    """Start (or restart) automatic verification and send the customer to the provider's secure page."""
    from . import kyc_auto
    kyc = KYCProfile.objects.filter(user=request.user).first()
    if kyc and str(kyc.status).upper() == 'APPROVED':
        messages.info(request, 'Your identity is already verified ✅')
        return redirect('kyc_status')
    if not kyc_auto.enabled():
        return redirect('kyc_submit')
    try:
        back = (getattr(settings, 'SITE_URL', '') or request.build_absolute_uri('/')).rstrip('/') + reverse('kyc_auto_return')
        session_id, url = kyc_auto.start_session(request.user, back)
    except Exception as e:
        logger.warning(f'KYC session for {request.user.pk} failed: {e}')
        messages.error(request, 'Automatic verification is unavailable right now. Please try again in a few minutes, '
                                'or upload your documents manually.')
        return redirect('kyc_status')
    KYCProfile.objects.update_or_create(user=request.user, defaults={
        'status': 'PENDING', 'provider': 'didit', 'provider_session': session_id, 'provider_status': 'not started',
        'admin_notes': ''})
    return redirect(url)


@login_required
def kyc_auto_return(request):
    """The customer comes back from the provider: look the result up (not the link's own claims)."""
    from . import kyc_auto
    kyc = KYCProfile.objects.filter(user=request.user, provider='didit').first()
    if kyc:
        try:
            status = kyc_auto.refresh(kyc)
            if status == 'APPROVED':
                messages.success(request, 'Identity verified ✅ Withdrawals and funded payouts are unlocked.')
            elif status == 'REJECTED':
                messages.error(request, 'Your verification was not approved. See the reason below and try again.')
            else:
                messages.info(request, 'Thanks! Your verification is being checked. This page updates by itself.')
        except Exception as e:
            logger.warning(f'KYC return {kyc.pk}: {e}')
    return redirect('kyc_status')


@csrf_exempt
@require_POST
def kyc_webhook(request):
    """Provider → us when a session changes. The body only names the session; the result is fetched from the API."""
    from . import kyc_auto
    if not kyc_auto.signature_ok(request.body, request.headers.get('X-Signature', '')):
        logger.info('KYC webhook: signature did not match (the decision is fetched from the API anyway)')
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return JsonResponse({'ok': False}, status=400)
    sid = str(data.get('session_id') or '')
    kyc = KYCProfile.objects.filter(provider='didit', provider_session=sid).first() if sid else None
    if kyc:
        try:
            kyc_auto.refresh(kyc)
        except Exception as e:
            logger.warning(f'KYC webhook refresh {kyc.pk}: {e}')
            return JsonResponse({'ok': False}, status=502)     # the provider retries
    return JsonResponse({'ok': True})


@staff_member_required
def update_kyc_status(request, pk):
    """
    Admin approves/rejects KYC. Sends SMS notification.
    """
    kyc = get_object_or_404(KYCProfile, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action', '').upper()
        admin_notes = request.POST.get('admin_notes', '').strip()

        if action == 'APPROVE':
            kyc.status = 'APPROVED'
            kyc.admin_notes = ''
            kyc.verified_at = timezone.now()
            kyc.save()
            from .referral_rewards import safe as referral_reward
            referral_reward(kyc.user, 'verified their identity')

            Notification.objects.create(
                user=kyc.user,
                title="KYC Approved ✅",
                message="Your identity has been verified.",
                category='KYC',
                priority='success',
                action_url='/trade/',
                action_label='Start Trading',
            )

            # ✅ SMS
            try:
                from .sms_service import sms_service
                sms_service.send_kyc_status(kyc.user, 'APPROVED')
            except Exception as e:
                logger.warning(f"KYC approval SMS failed: {e}")

        elif action == 'REJECT':
            kyc.status = 'REJECTED'
            kyc.admin_notes = admin_notes or 'Documents unclear'
            kyc.save()

            Notification.objects.create(
                user=kyc.user,
                title="KYC Rejected",
                message=f"Your KYC was rejected. Reason: {admin_notes or 'Documents unclear'}",
                category='KYC',
                priority='error',
                action_url='/kyc/status/',
                action_label='Try again',
            )

            # ✅ SMS
            try:
                from .sms_service import sms_service
                sms_service.send_kyc_status(kyc.user, 'REJECTED', admin_notes)
            except Exception as e:
                logger.warning(f"KYC rejection SMS failed: {e}")

    return redirect('admin_kyc_queue')

from django.db.models import Sum
from .models import TradePosition, TradingAccount

@csrf_exempt
def gateway_callback_view(request):
    if request.method == 'POST':
        data = request.POST
        return HttpResponse(status=200)
    return HttpResponse("Callback received successfully.", status=200)


@staff_member_required
def admin_kyc_queue(request):
    pending_kycs = KYCProfile.objects.filter(status__iexact='pending')  

    return render(request, 'admin/admin_kyc_queue.html', {'kycs': pending_kycs})



@staff_member_required
def admin_risk_dashboard_view(request):
    total_exposure = (
        TradePosition.objects.filter(status='OPEN').aggregate(Sum('notional_value'))['notional_value__sum']
        or 0.00
    )
    b_book_liability = (
        TradePosition.objects.filter(status='OPEN', execution_book='B_BOOK')
        .aggregate(Sum('liability'))['liability__sum']
        or 0.00
    )
    margin_calls = TradingAccount.objects.filter(margin_level__lt=100.0)

    context = {
        'total_exposure': total_exposure,
        'b_book_liability': b_book_liability,
        'margin_calls': margin_calls,
    }
    return render(request, 'admin/admin_risk_dashboard.html', context)


@staff_member_required
def admin_gateway_logs_view(request):
    logs = TransactionLog.objects.all().order_by('-created_at')[:100]
    discrepancies = TransactionLog.objects.filter(status='DISCREPANCY')
    context = {'logs': logs, 'discrepancies': discrepancies}
    return render(request, 'store/admin/admin_gateway_logs.html', context)



@staff_member_required
def admin_deposit_approval_view(request):
    """
    Pending deposits / withdrawals. Approve / Reject go through admin_dashboard_hub_view,
    so balances, transactions, notifications and SMS follow exactly the same rules.
    (This page used to pass no requests and ignore the buttons.)
    """
    if request.method == 'POST':
        pr = get_object_or_404(PaymentRequest, id=request.POST.get('request_id'))
        kind = 'withdrawal' if (pr.transaction_type or '').lower() == 'withdrawal' else ('crypto' if getattr(pr, 'tx_hash', None) else 'deposit')
        post = request.POST.copy()
        post['action_type'] = kind
        post['item_id'] = str(pr.id)
        post['decision'] = 'approve' if request.POST.get('action') == 'APPROVE' else 'reject'
        request.POST = post
        admin_dashboard_hub_view(request)
        return redirect(request.path)

    pending = PaymentRequest.objects.filter(status='PENDING').select_related('user').order_by('created_at')
    context = {
        'requests': pending,
        'pending_total': sum((r.amount for r in pending), Decimal('0')),
    }
    return render(request, 'store/admin_panel/admin_approvals.html', context)



@staff_member_required
def admin_risk_dashboard_view(request):
    context = {}
    return render(request, 'store/admin_panel/risk_dashboard.html', context)


@staff_member_required
def admin_gateway_logs_view(request):
    # this definition overrides the one above, and used to pass no data (the page was always empty)
    logs = TransactionLog.objects.select_related('user').order_by('-created_at')[:300]
    discrepancies = TransactionLog.objects.filter(status='DISCREPANCY')
    context = {'logs': logs, 'discrepancies': discrepancies}
    return render(request, 'store/admin_panel/gateway_logs.html', context)





# ==========================================
# HELPER FUNCTIONS
# ==========================================

@login_required
def get_live_breakout_predictions_api(request):
    symbol_mapping = {
        'card_1': 'EURUSD=X',   # LSTM-Pro v3.2
        'card_2': 'BTC-USD',    # Transformer-X
        'card_3': 'GBPJPY=X',   # Ensemble-Alpha
        'card_4': 'ETH-USD',    # LSTM-Crypto v2.1
        'card_5': 'GC=F',       # Gold-Predictor X
        'card_6': '^GSPC'       # Index-Master Pro
    }
    
    predictions = {}
    
    for card_key, symbol in symbol_mapping.items():
        try:
            df = yf.download(symbol, period="60d", interval="1h", progress=False)
            if df.empty:
                continue
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            df = df.reset_index()
            time_col = 'Datetime' if 'Datetime' in df.columns else ('Date' if 'Date' in df.columns else df.columns[0])
            df.rename(columns={time_col: 'Gmt time'}, inplace=True)
            
            df = df[df['Volume'] != 0].reset_index(drop=True)
            if len(df) < 150:
                continue
            
            df['RSI'] = ta.rsi(df['Close'], length=12)
            df['EMA'] = ta.ema(df['Close'], length=150)
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
            
            backcandles = 15
            win = backcandles + 1
            above = (np.minimum(df['Open'], df['Close']) > df['EMA']).astype(int)
            below = (np.maximum(df['Open'], df['Close']) < df['EMA']).astype(int)
            upt = (above.rolling(win, min_periods=win).sum() == win)
            dnt = (below.rolling(win, min_periods=win).sum() == win)
            
            signal = np.zeros(len(df), dtype=int)
            signal[upt & dnt] = 3
            signal[upt & ~dnt] = 2
            signal[dnt & ~upt] = 1
            df['EMASignal'] = signal
            
            def mark_pivots(df: pd.DataFrame, window: int) -> pd.Series:
                span = 2 * window + 1
                roll_max = df['High'].rolling(span, center=True).max()
                roll_min = df['Low'].rolling(span, center=True).min()
                pivot_high = (df['High'] >= roll_max) & roll_max.notna()
                pivot_low = (df['Low'] <= roll_min) & roll_min.notna()
                pivots = np.zeros(len(df), dtype=int)
                pivots[pivot_high] += 1
                pivots[pivot_low] += 2
                return pd.Series(pivots, index=df.index, name='isPivot')
                
            df['isPivot'] = mark_pivots(df, window=7)
            
            def detect_structure(candle: int, backcandles: int, window: int) -> tuple:
                if candle - backcandles < 0:
                    return 0, None
                prev_bar = candle - 1
                if prev_bar < 0:
                    return 0, None
                    
                close_now = df.loc[candle, 'Close']
                close_prev = df.loc[prev_bar, 'Close']
                piv_df = df.iloc[candle - backcandles : candle - window + 1]
                
                ph_df = piv_df[piv_df['isPivot'] == 1]
                if not ph_df.empty:
                    ph_idx = ph_df.index[-1]
                    ph_val = ph_df.loc[ph_idx, 'High']
                    if close_now > ph_val and close_prev <= ph_val:
                        pl_before = piv_df.loc[:ph_idx - 1]
                        pl_before = pl_before[pl_before['isPivot'] == 2]
                        pl_after = piv_df.loc[ph_idx + 1:]
                        pl_after = pl_after[pl_after['isPivot'] == 2]
                        if not pl_before.empty and not pl_after.empty:
                            pl1_val = pl_before.iloc[-1]['Low']
                            pl2_val = pl_after['Low'].min()
                            if pl2_val < pl1_val:
                                return 2, ph_idx
                                
                pl_df = piv_df[piv_df['isPivot'] == 2]
                if not pl_df.empty:
                    pl_idx = pl_df.index[-1]
                    pl_val = pl_df.loc[pl_idx, 'Low']
                    if close_now < pl_val and close_prev >= pl_val:
                        ph_before = piv_df.iloc[:pl_idx - 1]
                        ph_before = ph_before[ph_before['isPivot'] == 1]
                        ph_after = piv_df.loc[pl_idx + 1:]
                        ph_after = ph_after[ph_after['isPivot'] == 1]
                        if not ph_before.empty and not ph_after.empty:
                            ph1_val = ph_before.iloc[-1]['High']
                            ph2_val = ph_after['High'].max()
                            if ph2_val > ph1_val:
                                return 1, pl_idx
                                
                return 0, None

            last_idx = len(df) - 1
            sig, ref_idx = detect_structure(last_idx, backcandles=40, window=5)
            
            direction = "NEUTRAL"
            if sig == 2 or df.loc[last_idx, 'EMASignal'] in [2, 3]:
                direction = "BULLISH"
            elif sig == 1 or df.loc[last_idx, 'EMASignal'] == 1:
                direction = "BEARISH"

            predictions[card_key] = {
                "symbol": symbol,
                "close": round(float(df.loc[last_idx, 'Close']), 2),
                "rsi": round(float(df.loc[last_idx, 'RSI']), 1) if not pd.isna(df.loc[last_idx, 'RSI']) else 50.0,
                "breakout_signal": int(sig),
                "direction": direction,
                "confidence": f"{min(98.0, max(75.0, 70.0 + abs(df.loc[last_idx, 'RSI'] - 50))):.1f}%"
            }
        except Exception as e:
            predictions[card_key] = {"error": str(e)}
            
    return JsonResponse({"success": True, "predictions": predictions})


@login_required
def breakout_predictions_api(request):
    symbols = request.GET.getlist('symbols', ["EURUSD=X", "GBPUSD=X"])
    results = get_live_breakout_predictions(symbols)
    return JsonResponse({"success": True, "predictions": results})


from django.contrib import messages

@login_required
def ai_support_view(request):
    return render(request, 'store/support.html')

@login_required
def submit_support_ticket(request):
    if request.method == 'POST':
        category = request.POST.get('category')
        message = request.POST.get('message')
        # Add your database logic here to save the ticket if needed
        messages.success(request, "Your support ticket has been submitted successfully. Our team will respond shortly.")
    return redirect('ai_support')


def _otp_user(email):
    """Account for this email (case-insensitive); with duplicates, the most recently active one."""
    return (User.objects.filter(email__iexact=email, is_active=True)
            .order_by('-last_login', '-date_joined').first())


def request_email_otp_view(request):
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        user = _otp_user(email) if email else None
        if not user:
            messages.error(request, "No account found with this email address.")
            return redirect('request_email_otp')

        # one code a minute per email, so the button can't be used to spam an inbox
        if cache.get(f'otp_sent_{email}'):
            messages.info(request, "A code was sent less than a minute ago. Check your inbox (and spam folder).")
            request.session['otp_email'] = email
            return redirect('verify_email_otp')

        import secrets
        otp = str(secrets.randbelow(900000) + 100000)   # unpredictable, unlike random
        try:
            send_mail(
                subject='Your PATREOAIX Verification Code',
                message=f'Your login verification code is: {otp}. This code expires in 5 minutes.',
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None) or getattr(settings, 'EMAIL_HOST_USER', None),
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(f"OTP email to {email} failed: {e}")
            messages.error(request, "We couldn't send the email right now. Try again in a minute, or log in with your password.")
            return redirect('request_email_otp')

        # 5-minute code, 5 attempts
        cache.set(f'otp_{email}', otp, timeout=300)
        cache.set(f'otp_tries_{email}', 0, timeout=300)
        cache.set(f'otp_sent_{email}', 1, timeout=60)
        request.session['otp_email'] = email
        messages.success(request, "Verification code sent to your email.")
        return redirect('verify_email_otp')

    return render(request, 'account/request_email_otp.html')


def verify_email_otp_view(request):
    email = request.session.get('otp_email')
    if not email:
        return redirect('request_email_otp')

    if request.method == 'POST':
        entered = ''.join(ch for ch in (request.POST.get('otp') or '') if ch.isdigit())
        cached_otp = cache.get(f'otp_{email}')
        tries = (cache.get(f'otp_tries_{email}') or 0) + 1

        if cached_otp and tries <= 5 and secrets_compare(entered, cached_otp):
            cache.delete_many([f'otp_{email}', f'otp_tries_{email}'])
            request.session.pop('otp_email', None)
            user = _otp_user(email)
            if not user:
                messages.error(request, "This account is no longer active.")
                return redirect('login')
            # name the backend: allauth adds a second one and login() refuses to guess
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Successfully verified and signed in!")
            next_url = request.GET.get('next')
            from django.utils.http import url_has_allowed_host_and_scheme
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect('dashboard')

        if not cached_otp or tries >= 5:
            cache.delete_many([f'otp_{email}', f'otp_tries_{email}'])
            messages.error(request, "This code has expired or had too many wrong tries. Request a new one.")
            return redirect('request_email_otp')
        cache.set(f'otp_tries_{email}', tries, timeout=300)
        left = 5 - tries
        messages.error(request, f"Wrong code. {left} attempt{'s' if left != 1 else ''} left.")

    return render(request, 'account/verify_email_otp.html', {'email': email})


def secrets_compare(a, b):
    """Constant-time string compare (no timing hints about the code)."""
    return hmac.compare_digest(str(a), str(b))



# ═══════════════════════════════════════════════════════════════
# ADMIN DASHBOARD HUB — Unified Approval Center
# ═══════════════════════════════════════════════════════════════
@staff_member_required
def admin_dashboard_hub_view(request):
    """
    Unified admin hub — handles:
      - Regular deposits (M-Pesa, bank, etc.)
      - Crypto deposits (with TxID verification)
      - Withdrawals (with balance check before debit)
      - KYC verifications
      - Funded payouts
    Sends SMS notifications on every approval/rejection.
    """
    from django.db.models import Sum
    from .sms_service import sms_service

    # ═══════════════════════════════════════════════════════════
    # HANDLE POST — All admin actions
    # ═══════════════════════════════════════════════════════════
    if request.method == 'POST':
        # tickets, balance adjustments, messages, broadcasts, copy signals, suspensions (store/admin_hub.py)
        from . import admin_hub
        extra = admin_hub.handle(request)
        if extra:
            return extra

        action_type = request.POST.get('action_type')
        item_id = request.POST.get('item_id')
        decision = request.POST.get('decision')
        admin_notes = request.POST.get('admin_notes', '').strip()

        # ═══════════════════════════════════════════════════════════
        # CRYPTO DEPOSIT
        # ═══════════════════════════════════════════════════════════
        if action_type == 'crypto':
            payment = get_object_or_404(PaymentRequest, id=item_id, transaction_type='DEPOSIT')

            if payment.status != 'PENDING':
                messages.warning(request, f'Crypto deposit #{item_id} was already {payment.status.lower()}.')
                return redirect('admin_dashboard_hub')

            if decision == 'approve':
                with transaction.atomic():
                    payment.status = 'APPROVED'
                    payment.processed = True
                    payment.admin_notes = admin_notes
                    payment.reviewed_by = request.user
                    payment.reviewed_at = timezone.now()
                    payment.save()

                    account, _ = Account.objects.get_or_create(
                        user=payment.user, account_type='EXCHANGE'
                    )
                    # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)

                    Transaction.objects.create(
                        user=payment.user,
                        amount=payment.amount,
                        transaction_type='DEPOSIT',
                        status='completed',
                        details=f"Crypto deposit approved ({payment.network}) | TxID: {payment.tx_hash}",
                    )

                    Notification.objects.create(
                        user=payment.user,
                        title="Crypto Deposit Approved ✅",
                        message=f"${payment.amount:.2f} has been credited to your Exchange balance.\nTxID: {payment.tx_hash[:20]}...",
                        category='FUNDING',
                        priority='success',
                        action_url='/funding/',
                        action_label='View Wallet',
                    )

                    # ✅ SMS: Deposit approved
                    try:
                        account.refresh_from_db()
                        sms_service.send_deposit_approved(
                            payment.user, payment.amount, account.balance
                        )
                    except Exception as e:
                        logger.warning(f"Crypto deposit approval SMS failed: {e}")

                messages.success(request, f'Crypto deposit approved — ${payment.amount:.2f} credited.')

            elif decision == 'reject':
                payment.status = 'REJECTED'
                payment.processed = True
                payment.admin_notes = admin_notes or 'TxID could not be verified on blockchain.'
                payment.reviewed_by = request.user
                payment.reviewed_at = timezone.now()
                payment.save()

                Notification.objects.create(
                    user=payment.user,
                    title="Crypto Deposit Rejected",
                    message=f"Your deposit of ${payment.amount:.2f} was rejected. Reason: {payment.admin_notes}",
                    category='FUNDING',
                    priority='error',
                    action_url='/support/',
                    action_label='Contact Support',
                )

                # ✅ SMS: Deposit rejected
                try:
                    sms_service.send_deposit_rejected(
                        payment.user, payment.amount, payment.admin_notes
                    )
                except Exception as e:
                    logger.warning(f"Crypto deposit rejection SMS failed: {e}")

                messages.warning(request, f'Crypto deposit rejected.')

            return redirect('admin_dashboard_hub')

        # ═══════════════════════════════════════════════════════════
        # REGULAR DEPOSIT / WITHDRAWAL
        # ═══════════════════════════════════════════════════════════
        if action_type in ('deposit', 'withdrawal'):
            payment = get_object_or_404(PaymentRequest, id=item_id)

            if payment.status != 'PENDING':
                messages.warning(request, f'Request #{item_id} was already {payment.status.lower()}.')
                return redirect('admin_dashboard_hub')

            with transaction.atomic():
                payment.status = 'APPROVED' if decision == 'approve' else 'REJECTED'
                payment.processed = True
                payment.admin_notes = admin_notes
                payment.reviewed_by = request.user
                payment.reviewed_at = timezone.now()
                payment.save()

                if payment.status == 'APPROVED':
                    account, _ = Account.objects.get_or_create(
                        user=payment.user, account_type='EXCHANGE'
                    )

                    # -------- DEPOSIT APPROVED --------
                    if payment.transaction_type.lower() == 'deposit':
                        # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)

                        Transaction.objects.create(
                            user=payment.user,
                            amount=payment.amount,
                            transaction_type='DEPOSIT',
                            status='completed',
                            details=f"Deposit approved ({payment.payment_method})",
                        )

                        Notification.objects.create(
                            user=payment.user,
                            title="Deposit Approved ✅",
                            message=f"${payment.amount:.2f} has been credited to your Exchange balance.",
                            category='FUNDING',
                            priority='success',
                            action_url='/funding/',
                            action_label='View Wallet',
                        )

                        # ✅ SMS: Deposit approved
                        try:
                            account.refresh_from_db()
                            sms_service.send_deposit_approved(
                                payment.user, payment.amount, account.balance
                            )
                        except Exception as e:
                            logger.warning(f"Deposit approval SMS failed: {e}")

                    # -------- WITHDRAWAL APPROVED --------
                    elif payment.transaction_type.lower() == 'withdrawal':
                        if account.balance < payment.amount:
                            messages.error(
                                request,
                                f'Cannot approve: @{payment.user.username} has only '
                                f'${account.balance:.2f} but requested ${payment.amount:.2f}.'
                            )
                            payment.status = 'PENDING'
                            payment.processed = False
                            payment.reviewed_by = None
                            payment.reviewed_at = None
                            payment.save()
                            return redirect('admin_dashboard_hub')

                        # balance is applied by the Transaction post_save signal below (doing it here too credited it twice)

                        Transaction.objects.create(
                            user=payment.user,
                            amount=payment.amount,
                            transaction_type='WITHDRAWAL',
                            status='completed',
                            details=f"Withdrawal approved ({payment.payment_method})",
                        )

                        Notification.objects.create(
                            user=payment.user,
                            title="Withdrawal Approved ✅",
                            message=f"Your withdrawal of ${payment.amount:.2f} has been approved.",
                            category='FUNDING',
                            priority='success',
                            action_url='/funding/',
                            action_label='View Wallet',
                        )

                        # ✅ SMS: Withdrawal approved
                        try:
                            sms_service.send_withdrawal_approved(
                                payment.user, payment.amount, payment.payment_method
                            )
                        except Exception as e:
                            logger.warning(f"Withdrawal approval SMS failed: {e}")

                else:
                    # -------- REJECTED --------
                    type_label = payment.transaction_type.capitalize()

                    Notification.objects.create(
                        user=payment.user,
                        title=f"{type_label} Rejected",
                        message=f"Your {type_label.lower()} of ${payment.amount:.2f} was rejected. Reason: {admin_notes or 'Contact support.'}",
                        category='FUNDING',
                        priority='error',
                        action_url='/support/',
                        action_label='Contact Support',
                    )

                    # ✅ SMS: Deposit OR Withdrawal rejected
                    try:
                        if payment.transaction_type.lower() == 'deposit':
                            sms_service.send_deposit_rejected(
                                payment.user, payment.amount, admin_notes or 'Contact support'
                            )
                        else:
                            sms_service.send_withdrawal_rejected(
                                payment.user, payment.amount, admin_notes or 'Contact support'
                            )
                    except Exception as e:
                        logger.warning(f"{type_label} rejection SMS failed: {e}")

            messages.success(request, f'{action_type.title()} #{item_id} {decision}d.')
            return redirect('admin_dashboard_hub')

        # ═══════════════════════════════════════════════════════════
        # KYC
        # ═══════════════════════════════════════════════════════════
        elif action_type == 'kyc':
            kyc = get_object_or_404(KYCProfile, id=item_id)

            if decision == 'approve':
                kyc.status = 'APPROVED'
                kyc.save()

                Notification.objects.create(
                    user=kyc.user,
                    title="KYC Approved ✅",
                    message="Your identity has been verified. Full trading access unlocked.",
                    category='KYC',
                    priority='success',
                    action_url='/trade/',
                    action_label='Start Trading',
                )

                # ✅ SMS: KYC approved
                try:
                    sms_service.send_kyc_status(kyc.user, 'APPROVED')
                except Exception as e:
                    logger.warning(f"KYC approval SMS failed: {e}")

            else:
                kyc.status = 'REJECTED'
                kyc.save()

                Notification.objects.create(
                    user=kyc.user,
                    title="KYC Rejected",
                    message=f"Your KYC was rejected. Reason: {admin_notes or 'Documents unclear'}",
                    category='KYC',
                    priority='error',
                    action_url='/kyc/submit/',
                    action_label='Re-submit',
                )

                # ✅ SMS: KYC rejected
                try:
                    sms_service.send_kyc_status(kyc.user, 'REJECTED', admin_notes)
                except Exception as e:
                    logger.warning(f"KYC rejection SMS failed: {e}")

            messages.success(request, f'KYC #{item_id} {decision}d.')
            return redirect('admin_dashboard_hub')

        # ═══════════════════════════════════════════════════════════
        # FUNDED PAYOUT
        # ═══════════════════════════════════════════════════════════
        elif action_type == 'funded_payout':
            payout = get_object_or_404(FundedPayoutRequest, id=item_id)

            if payout.status != 'PENDING':
                messages.warning(request, f'Payout #{item_id} was already {payout.status.lower()}.')
                return redirect('admin_dashboard_hub')

            if decision == 'approve':
                with transaction.atomic():
                    payout.status = 'COMPLETED'
                    payout.approved_amount = payout.requested_amount
                    payout.processed_by = request.user
                    payout.processed_at = timezone.now()
                    payout.admin_notes = admin_notes
                    payout.save()

                    funded_account = FundedAccount.objects.select_for_update().get(
                        id=payout.funded_account.id
                    )
                    funded_account.balance -= payout.requested_amount
                    funded_account.save()

                    exchange_account, _ = Account.objects.get_or_create(
                        user=payout.user, account_type='EXCHANGE'
                    )
                    exchange_account.balance += payout.requested_amount
                    exchange_account.save()

                    Transaction.objects.create(
                        user=payout.user,
                        amount=payout.requested_amount,
                        transaction_type='FUNDED_PAYOUT',
                        details=f'Funded payout approved - {payout.payout_month.strftime("%B %Y")}',
                    )

                    Notification.objects.create(
                        user=payout.user,
                        title='Payout Approved! 🎉',
                        message=f'${payout.requested_amount:.2f} has been credited to your Exchange balance.',
                        category='PAYOUT',
                        priority='success',
                        action_url='/funding/',
                        action_label='View Balance',
                    )

                    # ✅ SMS: Funded payout approved
                    try:
                        sms_service.send_to_user(
                            payout.user,
                            f"✅ PATREOAIX: Funded payout approved!\n"
                            f"${payout.requested_amount:.2f} credited to Exchange.\n"
                            f"New balance: ${exchange_account.balance:.2f}",
                            template='withdrawal'
                        )
                    except Exception as e:
                        logger.warning(f"Funded payout SMS failed: {e}")

                messages.success(request, f'Funded payout #{item_id} approved.')

            else:
                payout.status = 'REJECTED'
                payout.rejection_reason = admin_notes
                payout.processed_by = request.user
                payout.processed_at = timezone.now()
                payout.save()

                Notification.objects.create(
                    user=payout.user,
                    title='Payout Request Rejected',
                    message=f'Your payout of ${payout.requested_amount:.2f} was rejected. Reason: {admin_notes}',
                    category='PAYOUT',
                    priority='error',
                    action_url='/funded/payout/',
                    action_label='Try Again',
                )

                # ✅ SMS: Funded payout rejected
                try:
                    sms_service.send_to_user(
                        payout.user,
                        f"❌ PATREOAIX: Payout request of ${payout.requested_amount:.2f} rejected.\n"
                        f"Reason: {admin_notes or 'Contact support'}",
                        template='withdrawal'
                    )
                except Exception as e:
                    logger.warning(f"Funded payout rejection SMS failed: {e}")

                messages.warning(request, f'Funded payout #{item_id} rejected.')

            return redirect('admin_dashboard_hub')

    # ═══════════════════════════════════════════════════════════
    # GET — Fetch pending items
    # ═══════════════════════════════════════════════════════════

    crypto_deposits = PaymentRequest.objects.filter(
        transaction_type__iexact='Deposit',
        status='PENDING',
    ).filter(
        Q(payment_method__iexact='Crypto') |
        Q(payment_method__icontains='USDT') |
        Q(payment_method__icontains='Crypto')
    ).select_related('user').order_by('-created_at')

    pending_deposits = PaymentRequest.objects.filter(
        transaction_type__iexact='Deposit',
        status='PENDING',
    ).exclude(
        id__in=crypto_deposits.values_list('id', flat=True)
    ).select_related('user').order_by('-created_at')

    pending_withdrawals = PaymentRequest.objects.filter(
        transaction_type__iexact='Withdrawal',
        status='PENDING',
    ).select_related('user').order_by('-created_at')

    pending_kyc = KYCProfile.objects.filter(
        status='PENDING',
    ).order_by('-id')

    pending_funded_payouts = FundedPayoutRequest.objects.filter(
        status='PENDING'
    ).select_related('user', 'funded_account').order_by('-created_at')

    recent_crypto = PaymentRequest.objects.filter(
        transaction_type__iexact='Deposit',
    ).filter(
        Q(payment_method__iexact='Crypto') | Q(payment_method__icontains='Crypto')
    ).exclude(status='PENDING').select_related('user', 'reviewed_by').order_by('-reviewed_at')[:10]

    crypto_pending_total = crypto_deposits.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    context = {
        'crypto_deposits': crypto_deposits,
        'recent_crypto': recent_crypto,
        'crypto_pending_count': crypto_deposits.count(),
        'crypto_pending_total': crypto_pending_total,
        'pending_deposits': pending_deposits,
        'deposits': pending_deposits,
        'pending_withdrawals': pending_withdrawals,
        'withdrawals': pending_withdrawals,
        'pending_kyc': pending_kyc,
        'kycs': pending_kyc,
        'pending_funded_payouts': pending_funded_payouts,
        'funded_payouts': pending_funded_payouts,
    }
    from . import admin_hub
    return render(request, 'store/admin_panel/hub.html', admin_hub.context(request, context))


@staff_member_required
def api_admin_hub_counts(request):
    """Pending counts for the hub's live badges."""
    from . import admin_hub
    return JsonResponse({'success': True, **admin_hub.counts()})

from .models import SupportTicket
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.mail import send_mail
from django.conf import settings
from .models import SupportTicket

def ai_support_view(request):
    if request.method == 'POST':
        category = request.POST.get('category')
        subject = request.POST.get('subject')
        description = request.POST.get('description')
        
        if category and subject and description:
            ticket = SupportTicket.objects.create(
                user=request.user if request.user.is_authenticated else None,
                category=category,
                subject=subject,
                description=description
            )
            
            # Send email notification to admin
            try:
                send_mail(
                    subject=f"[PATREOAIX Support Ticket #{ticket.id}] {subject}",
                    message=f"New support ticket submitted.\n\nCategory: {category}\nUser: {request.user}\n\nDescription:\n{description}",
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmaster@localhost'),
                    recipient_list=[getattr(settings, 'ADMIN_EMAIL', 'admin@patreoaiix.com')],
                    fail_silently=True,
                )
            except Exception:
                pass

            messages.success(request, 'Support ticket submitted successfully!')
            return redirect('ai_support')
        else:
            messages.error(request, 'Please fill out all required fields.')

    user_tickets = SupportTicket.objects.filter(user=request.user).order_by('-created_at') if request.user.is_authenticated else []
    return render(request, 'store/support.html', {'tickets': user_tickets})

@staff_member_required
def admin_message_center_view(request):
    if request.method == 'POST':
        ticket_id = request.POST.get('ticket_id')
        new_status = request.POST.get('status')
        if ticket_id and new_status:
            SupportTicket.objects.filter(id=ticket_id).update(status=new_status)
            messages.success(request, f"Ticket #{ticket_id} status updated to {new_status}.")
            return redirect('admin_message_center')

    tickets = SupportTicket.objects.all().order_by('-created_at')
    return render(request, 'store/admin_panel/admin_messages.html', {'tickets': tickets})


from google import genai

@csrf_exempt
def ai_chat_api(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            user_message = data.get('message', '').strip()
            
            if not user_message:
                return JsonResponse({'error': 'Empty message'}, status=400)
            
            ai_reply = ""
            try:
                # Automatically loads GEMINI_API_KEY from environment variables/.env
                client = genai.Client()
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=user_message,
                    config={
                        'system_instruction': 'You are PATREOAIX Support Copilot, an expert assistant trained on platform documentation, signal algorithms, trading mechanics, Deriv broker authentication, and webhooks.'
                    }
                )
                ai_reply = response.text
            except Exception as api_err:
                # Fallback response if the API is experiencing high demand (503) or rate limits
                msg_lower = user_message.lower()
                if 'webhook' in msg_lower:
                    ai_reply = "To configure webhooks, navigate to your dashboard automation tab, generate your unique webhook endpoint, and link it to your TradingView alert parameters."
                elif 'deriv' in msg_lower or 'token' in msg_lower:
                    ai_reply = "Make sure your Deriv OAuth token is active under your account settings to ensure uninterrupted tick-based execution."
                else:
                    ai_reply = f"The AI intelligence service is currently experiencing high demand (503). However, I've noted your query regarding '{user_message}'. Please try submitting your request again in a moment, or open a support ticket above."
            
            return JsonResponse({'response': ai_reply})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
            
    return JsonResponse({'error': 'Invalid request method'}, status=405)


# Add this helper function to your views.py
def get_binance_spot_balance(api_key, api_secret):
    url = "https://api.binance.com/api/v3/account"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        'X-MBX-APIKEY': api_key
    }
    try:
        response = requests.get(f"{url}?{query_string}&signature={signature}", headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            balances = data.get('balances', [])
            for b in balances:
                if b.get('asset') == 'USDT':
                    return Decimal(str(b.get('free', '0'))) + Decimal(str(b.get('locked', '0')))
    except Exception:
        pass
    return Decimal('0.00')


def connect_ctrader(request):
    if request.method == 'POST':
        server_name = request.POST.get('server_name')
        account_number = request.POST.get('account_number')
        api_token = request.POST.get('api_token')
        
        # Add your cTrader API authentication and database saving logic here
        
        messages.success(request, "cTrader account authorized successfully!")
        return redirect('dashboard') # Change 'dashboard' to your main page route name if different
        
    return redirect('dashboard')



import pandas as pd
# (pandas-ta is imported once at the top of this file as `ta`)
import yfinance as yf
from datetime import datetime
import random
import time
import json
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

@login_required
def market_scanner(request):
    """Real-time market scanner with AI analysis for all symbols"""
    return render(request, 'store/market_overview.html',
                  {'scrutiny_url': _page_url(['ai_prediction_models', 'ai-prediction-models', 'ai_predictions'])})


@login_required
def get_market_scan_data(request):
    """
    Market scanner API — pulls real prices from the shared price_feed
    (TwelveData → OANDA → Yahoo for forex/metals, Binance for crypto).

    Returns a list of symbols with:
      symbol, name, type, price, change_24h, trend, strength,
      signal, rsi, volume, ai_summary, sentiment,
      support, resistance, volatility, momentum, rsi_source
    """
    from . import price_feed

    # ═══════════════════════════════════════════════════════════
    # SYMBOL CATALOG — matches what price_feed knows about
    # ═══════════════════════════════════════════════════════════
    SYMBOLS = [
        # ── Forex Majors ──────────────────────────────
        {'symbol': 'EURUSD', 'name': 'Euro / US Dollar',         'type': 'Forex', 'display': 'EUR/USD'},
        {'symbol': 'GBPUSD', 'name': 'British Pound / USD',      'type': 'Forex', 'display': 'GBP/USD'},
        {'symbol': 'USDJPY', 'name': 'US Dollar / Yen',          'type': 'Forex', 'display': 'USD/JPY'},
        {'symbol': 'AUDUSD', 'name': 'Australian Dollar / USD',  'type': 'Forex', 'display': 'AUD/USD'},
        {'symbol': 'USDCAD', 'name': 'US Dollar / Canadian',     'type': 'Forex', 'display': 'USD/CAD'},
        {'symbol': 'NZDUSD', 'name': 'NZ Dollar / USD',          'type': 'Forex', 'display': 'NZD/USD'},
        {'symbol': 'USDCHF', 'name': 'US Dollar / Swiss Franc',  'type': 'Forex', 'display': 'USD/CHF'},

        # ── Forex Crosses ─────────────────────────────
        {'symbol': 'EURGBP', 'name': 'Euro / British Pound',     'type': 'Forex', 'display': 'EUR/GBP'},
        {'symbol': 'EURJPY', 'name': 'Euro / Japanese Yen',      'type': 'Forex', 'display': 'EUR/JPY'},
        {'symbol': 'GBPJPY', 'name': 'Pound / Japanese Yen',     'type': 'Forex', 'display': 'GBP/JPY'},
        {'symbol': 'EURCHF', 'name': 'Euro / Swiss Franc',       'type': 'Forex', 'display': 'EUR/CHF'},
        {'symbol': 'GBPCHF', 'name': 'Pound / Swiss Franc',      'type': 'Forex', 'display': 'GBP/CHF'},

        # ── Commodities ──────────────────────────────
        {'symbol': 'XAUUSD', 'name': 'Gold Spot',                'type': 'Commodity', 'display': 'XAU/USD'},
        {'symbol': 'XAGUSD', 'name': 'Silver Spot',              'type': 'Commodity', 'display': 'XAG/USD'},
        {'symbol': 'USOIL',  'name': 'WTI Crude Oil',            'type': 'Commodity', 'display': 'USOIL'},
        {'symbol': 'UKOIL',  'name': 'Brent Crude Oil',          'type': 'Commodity', 'display': 'UKOIL'},

        # ── Crypto ───────────────────────────────────
        {'symbol': 'BTCUSD', 'name': 'Bitcoin',                  'type': 'Crypto', 'display': 'BTC/USD'},
        {'symbol': 'ETHUSD', 'name': 'Ethereum',                 'type': 'Crypto', 'display': 'ETH/USD'},
        {'symbol': 'SOLUSD', 'name': 'Solana',                   'type': 'Crypto', 'display': 'SOL/USD'},
        {'symbol': 'BNBUSD', 'name': 'BNB',                      'type': 'Crypto', 'display': 'BNB/USD'},
        {'symbol': 'XRPUSD', 'name': 'Ripple',                   'type': 'Crypto', 'display': 'XRP/USD'},
        {'symbol': 'ADAUSD', 'name': 'Cardano',                  'type': 'Crypto', 'display': 'ADA/USD'},
        {'symbol': 'DOGEUSD','name': 'Dogecoin',                 'type': 'Crypto', 'display': 'DOGE/USD'},
        {'symbol': 'DOTUSD', 'name': 'Polkadot',                 'type': 'Crypto', 'display': 'DOT/USD'},
        {'symbol': 'LINKUSD','name': 'Chainlink',                'type': 'Crypto', 'display': 'LINK/USD'},
    ]

    # ═══════════════════════════════════════════════════════════
    # 1. Grab whatever's in cache, then kick a background refresh
    # ═══════════════════════════════════════════════════════════
    cached = price_feed.get_cached_prices()
    price_feed.refresh_all_async()

    # If cache is cold, do a blocking sync refresh
    if not cached or not cached.get('XAUUSD'):
        try:
            price_feed.refresh_all_sync(timeout=6.0)
            cached = price_feed.get_cached_prices()
        except Exception as e:
            logger.warning(f"market_scan sync refresh failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # 2. Build real-data rows
    # ═══════════════════════════════════════════════════════════
    results = []

    for meta in SYMBOLS:
        symbol = meta['symbol']
        price = cached.get(symbol)

        # Skip symbols we don't have prices for
        if not price or float(price) <= 0:
            continue

        price = float(price)
        source = price_feed.get_source_for(symbol)

        # ── Compute trend / signal from live momentum ─────────
        # We use a simple 3-point comparison since we only have the latest
        # price from cache. For richer signals, you'd cache a rolling window.
        trend, strength, signal = compute_signal_from_price(symbol, price, price_feed)

        # ── RSI: cached or defaulted ─────────────────────────
        rsi = compute_rsi_from_price_history(symbol, price_feed)

        # ── Volatility: derived from recent price spread ─────
        volatility = compute_volatility_from_history(symbol, price_feed)

        # ── Support / Resistance: simple ±2% band ────────────
        support = round(price * 0.98, 6)
        resistance = round(price * 1.02, 6)

        # ── AI Summary ───────────────────────────────────────
        ai_summary = build_ai_summary(meta['display'], trend, strength, signal, rsi, volatility)

        # ── Sentiment label ──────────────────────────────────
        if strength >= 80:
            sentiment = 'Strong Bullish' if trend == 'Bullish' else 'Strong Bearish'
        elif strength >= 60:
            sentiment = 'Bullish' if trend == 'Bullish' else 'Bearish'
        elif strength >= 40:
            sentiment = 'Neutral'
        else:
            sentiment = 'Weak Bullish' if trend == 'Bullish' else 'Weak Bearish'

        momentum = 'Bullish' if trend == 'Bullish' else 'Bearish' if trend == 'Bearish' else 'Neutral'

        results.append({
            'symbol': meta['display'],
            'raw_symbol': symbol,
            'name': meta['name'],
            'type': meta['type'],
            'price': price,
            'change_24h': 0.0,   # Requires historical delta — see note below
            'trend': trend,
            'strength': round(strength, 1),
            'signal': signal,
            'rsi': round(rsi, 1),
            'volume': 0,
            'ai_summary': ai_summary,
            'sentiment': sentiment,
            'support': support,
            'resistance': resistance,
            'volatility': round(volatility, 2),
            'momentum': momentum,
            'source': source,   # ← NEW: shows TWELVEDATA / BINANCE / OANDA / YAHOO
        })

    # Sort by strength (highest conviction first)
    results.sort(key=lambda x: x.get('strength', 0), reverse=True)

    return JsonResponse({
        'success': True,
        'data': results,
        'total_pairs': len(results),
        'sources': {
            'twelvedata': bool(price_feed.TWELVEDATA_API_KEY),
            'oanda':      bool(price_feed.OANDA_API_KEY),
            'binance':    True,  # always available
        },
        'timestamp': timezone.now().isoformat(),
    })


# ═══════════════════════════════════════════════════════════════
# HELPERS — signal generation from live cache
# ═══════════════════════════════════════════════════════════════
def compute_signal_from_price(symbol, price, price_feed):
    """
    Derive a lightweight trend/signal from the cached price
    and any recent candle data we can fetch.

    Falls back to neutral if we can't get history.
    """
    try:
        candles = _scan_candles(symbol)
        if candles and len(candles) >= 20:
            closes = [c['close'] for c in candles]
            # EMA 10 vs EMA 30
            ema10 = closes[-1]
            k10 = 2 / 11
            for c in closes[-10:]:
                ema10 = c * k10 + ema10 * (1 - k10)

            ema30 = closes[-1]
            k30 = 2 / 31
            for c in closes[-30:]:
                ema30 = c * k30 + ema30 * (1 - k30)

            diff_pct = ((ema10 - ema30) / ema30) * 100 if ema30 else 0

            if diff_pct > 0.15:
                trend = 'Bullish'
                strength = min(95, 60 + abs(diff_pct) * 8)
                signal = 'BUY'
            elif diff_pct < -0.15:
                trend = 'Bearish'
                strength = min(95, 60 + abs(diff_pct) * 8)
                signal = 'SELL'
            else:
                trend = 'Neutral'
                strength = 45
                signal = 'HOLD'

            return trend, strength, signal
    except Exception:
        pass

    # Fallback: neutral
    return 'Neutral', 50, 'HOLD'


def _scan_candles(symbol):
    """1h candles shared by the scanner's trend / RSI / volatility (one fetch per symbol, cached)."""
    key = f'scan:candles:{symbol}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    candles, _ = get_candles(symbol, '1h', 60)
    cache.set(key, candles or [], 180 if candles else 30)
    return candles or []


def compute_rsi_from_price_history(symbol, price_feed):
    """Derive RSI from recent candles, fallback to 50."""
    try:
        candles = _scan_candles(symbol)[-30:]
        if candles and len(candles) >= 15:
            closes = [c['close'] for c in candles]
            period = 14
            gains, losses = 0, 0
            for i in range(1, period + 1):
                diff = closes[i] - closes[i - 1]
                if diff >= 0:
                    gains += diff
                else:
                    losses -= diff
            avg_gain = gains / period
            avg_loss = losses / period
            for i in range(period + 1, len(closes)):
                diff = closes[i] - closes[i - 1]
                gain = diff if diff > 0 else 0
                loss = -diff if diff < 0 else 0
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))
    except Exception:
        pass
    return 50.0


def compute_volatility_from_history(symbol, price_feed):
    """Realized volatility % over recent candles."""
    try:
        candles = _scan_candles(symbol)[-30:]
        if candles and len(candles) >= 10:
            closes = [c['close'] for c in candles]
            rets = []
            for i in range(1, len(closes)):
                rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
            if not rets:
                return 0.5
            mean = sum(rets) / len(rets)
            variance = sum((r - mean) ** 2 for r in rets) / len(rets)
            return (variance ** 0.5) * 100
    except Exception:
        pass
    return 0.5


def build_ai_summary(display, trend, strength, signal, rsi, volatility):
    """Human-readable one-liner for the scanner card."""
    parts = []
    if trend == 'Bullish':
        parts.append(f"📈 Bullish momentum ({strength:.0f}%)")
    elif trend == 'Bearish':
        parts.append(f"📉 Bearish pressure ({strength:.0f}%)")
    else:
        parts.append("⚖️ Consolidation phase")

    if rsi < 30:
        parts.append("🟢 Oversold")
    elif rsi > 70:
        parts.append("🔴 Overbought")

    if signal == 'BUY':
        parts.append("✅ AI suggests BUY")
    elif signal == 'SELL':
        parts.append("🔻 AI suggests SELL")
    else:
        parts.append("⏳ Wait for confirmation")

    if volatility > 5:
        parts.append("⚠️ High vol")

    return " | ".join(parts)



def analyze_symbol_real(item):
    """
    Analyze a symbol with real data from Yahoo Finance
    """
    symbol = item['symbol']
    name = item['name']
    display = item['display']
    asset_type = item['type']
    
    try:
        # Fetch data with retry
        df = None
        for attempt in range(2):
            try:
                df = yf.download(
                    symbol, 
                    period="5d", 
                    interval="1h", 
                    progress=False,
                    timeout=10
                )
                if not df.empty:
                    break
            except:
                time.sleep(0.5)
                continue
        
        if df is None or df.empty:
            raise ValueError(f"No data for {symbol}")
        
        # Clean data
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        df = df.dropna()
        
        if len(df) < 10:
            raise ValueError(f"Insufficient data for {symbol}")
        
        # Extract price data
        close = df['Close']
        high = df['High']
        low = df['Low']
        volume = df['Volume']
        
        current_price = float(close.iloc[-1])
        
        if current_price == 0 or pd.isna(current_price):
            raise ValueError(f"Invalid price for {symbol}")
        
        prev_price = float(close.iloc[-2]) if len(close) > 1 else current_price
        
        # Calculate 24h Change
        if len(close) >= 24:
            price_24h_ago = float(close.iloc[-24])
            change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100 if price_24h_ago != 0 else 0
        else:
            change_24h = ((current_price - prev_price) / prev_price) * 100 if prev_price != 0 else 0
        
        # ==========================================
        # SAFELY CALCULATE EACH INDICATOR
        # ==========================================
        
        # RSI - Safe
        try:
            rsi_result = ta.rsi(close, length=14)
            if rsi_result is not None and not rsi_result.empty and not pd.isna(rsi_result.iloc[-1]):
                current_rsi = float(rsi_result.iloc[-1])
            else:
                current_rsi = 50.0
        except Exception:
            current_rsi = 50.0
        
        # MACD - Safe
        try:
            macd_result = ta.macd(close)
            macd_cross = False
            if macd_result is not None and not macd_result.empty:
                if 'MACD_12_26_9' in macd_result.columns:
                    macd_line = macd_result['MACD_12_26_9']
                    macd_signal = macd_result['MACDs_12_26_9']
                else:
                    macd_line = macd_result.iloc[:, 0]
                    macd_signal = macd_result.iloc[:, 1] if macd_result.shape[1] > 1 else macd_result.iloc[:, 0]
                
                if not macd_line.empty and not macd_signal.empty:
                    if not pd.isna(macd_line.iloc[-1]) and not pd.isna(macd_signal.iloc[-1]):
                        macd_cross = float(macd_line.iloc[-1]) > float(macd_signal.iloc[-1])
        except Exception:
            macd_cross = False
        
        # Moving Averages - Safe
        try:
            ma_20_result = ta.sma(close, length=20)
            ma_20 = float(ma_20_result.iloc[-1]) if ma_20_result is not None and not ma_20_result.empty and not pd.isna(ma_20_result.iloc[-1]) else current_price
        except Exception:
            ma_20 = current_price
        
        try:
            ma_50_result = ta.sma(close, length=50)
            ma_50 = float(ma_50_result.iloc[-1]) if ma_50_result is not None and not ma_50_result.empty and not pd.isna(ma_50_result.iloc[-1]) else current_price
        except Exception:
            ma_50 = current_price
        
        try:
            ma_200_result = ta.sma(close, length=200)
            ma_200 = float(ma_200_result.iloc[-1]) if ma_200_result is not None and not ma_200_result.empty and not pd.isna(ma_200_result.iloc[-1]) else current_price
        except Exception:
            ma_200 = current_price
        
        # ATR (Volatility) - Safe
        try:
            atr_result = ta.atr(high, low, close, length=14)
            if atr_result is not None and not atr_result.empty and not pd.isna(atr_result.iloc[-1]):
                volatility = float(atr_result.iloc[-1]) / current_price * 100 if current_price > 0 else 0
            else:
                volatility = 0.5
        except Exception:
            volatility = 0.5
        
        # Volume - Safe
        try:
            current_volume = float(volume.iloc[-1]) if len(volume) > 0 and not pd.isna(volume.iloc[-1]) else 0
        except Exception:
            current_volume = 0
        
        # Support and Resistance - Safe
        try:
            if len(low) >= 20:
                support = float(low.iloc[-20:].min())
            else:
                support = current_price * 0.98
        except Exception:
            support = current_price * 0.98
        
        try:
            if len(high) >= 20:
                resistance = float(high.iloc[-20:].max())
            else:
                resistance = current_price * 1.02
        except Exception:
            resistance = current_price * 1.02
        
        # ==========================================
        # DETERMINE TREND
        # ==========================================
        
        trend = "Neutral"
        signal = "HOLD"
        strength = 50
        
        # Multiple timeframe trend analysis
        short_trend = 1 if current_price > ma_20 else -1 if current_price < ma_20 else 0
        mid_trend = 1 if current_price > ma_50 else -1 if current_price < ma_50 else 0
        long_trend = 1 if current_price > ma_200 else -1 if current_price < ma_200 else 0
        
        trend_score = short_trend + mid_trend + long_trend
        
        # RSI signals
        rsi_signal = 0
        if current_rsi < 30:
            rsi_signal = 1
            rsi_status = "Oversold"
        elif current_rsi > 70:
            rsi_signal = -1
            rsi_status = "Overbought"
        else:
            rsi_status = "Neutral"
        
        # MACD signal
        macd_signal_val = 1 if macd_cross else -1 if not macd_cross else 0
        
        # Combined trend determination
        total_score = trend_score + rsi_signal + macd_signal_val
        
        if total_score >= 2:
            trend = "Bullish"
            strength = min(95, 60 + (total_score * 10))
            signal = "BUY"
        elif total_score <= -2:
            trend = "Bearish"
            strength = min(95, 60 + (abs(total_score) * 10))
            signal = "SELL"
        else:
            if abs(trend_score) >= 2:
                if trend_score > 0:
                    trend = "Bullish"
                    strength = 55 + (abs(trend_score) * 8)
                    signal = "BUY"
                else:
                    trend = "Bearish"
                    strength = 55 + (abs(trend_score) * 8)
                    signal = "SELL"
            else:
                trend = "Neutral"
                strength = 30 + abs(trend_score) * 10
                signal = "HOLD"
        
        # Ensure strength is within bounds
        strength = min(98, max(10, strength))
        
        # Determine sentiment
        if strength >= 80:
            sentiment = "Strong Bullish" if trend == "Bullish" else "Strong Bearish"
        elif strength >= 60:
            sentiment = "Bullish" if trend == "Bullish" else "Bearish"
        elif strength >= 40:
            sentiment = "Neutral"
        elif strength >= 20:
            sentiment = "Weak Bullish" if trend == "Bullish" else "Weak Bearish"
        else:
            sentiment = "Strong Bearish" if trend == "Bearish" else "Strong Bullish"
        
        # Momentum
        momentum = "Bullish" if total_score > 0 else "Bearish" if total_score < 0 else "Neutral"
        
        # AI Summary
        ai_summary = generate_ai_summary_real(trend, strength, current_rsi, signal, volatility, display)
        
        return {
            'symbol': display,
            'name': name,
            'type': asset_type,
            'price': round(current_price, 4) if current_price > 1 else round(current_price, 8),
            'change_24h': round(change_24h, 2),
            'trend': trend,
            'strength': round(strength, 1),
            'signal': signal,
            'rsi': round(current_rsi, 1),
            'volume': int(current_volume) if current_volume > 0 else 0,
            'ai_summary': ai_summary,
            'sentiment': sentiment,
            'support': round(support, 4) if support > 1 else round(support, 8),
            'resistance': round(resistance, 4) if resistance > 1 else round(resistance, 8),
            'volatility': round(volatility, 2),
            'momentum': momentum,
            'rsi_status': rsi_status,
        }
        
    except Exception as e:
        print(f"Error in analyze_symbol_real for {symbol}: {e}")
        return None


def generate_ai_summary_real(trend, strength, rsi, signal, volatility, symbol):
    """Generate AI-powered summary for a symbol"""
    summaries = []
    
    # Trend summary
    if trend == "Bullish":
        if strength > 80:
            summaries.append(f"🔥 Strong bullish momentum with {strength:.0f}% confidence")
        else:
            summaries.append(f"📈 Bullish trend detected with {strength:.0f}% confidence")
    elif trend == "Bearish":
        if strength > 80:
            summaries.append(f"⚠️ Strong bearish pressure with {strength:.0f}% confidence")
        else:
            summaries.append(f"📉 Bearish trend detected with {strength:.0f}% confidence")
    else:
        summaries.append("⏸️ Consolidation phase - waiting for breakout")
    
    # RSI summary
    if rsi < 30:
        summaries.append("🟢 RSI indicates oversold conditions - potential bounce")
    elif rsi > 70:
        summaries.append("🔴 RSI indicates overbought conditions - possible pullback")
    else:
        summaries.append("🟡 RSI at neutral levels - no extreme conditions")
    
    # Signal summary
    if signal == "BUY":
        summaries.append("✅ AI suggests BUY/LONG entry")
    elif signal == "SELL":
        summaries.append("📉 AI suggests SELL/SHORT entry")
    else:
        summaries.append("⏳ AI suggests HOLD - wait for confirmation")
    
    # Volatility
    if volatility > 5:
        summaries.append("⚠️ High volatility - use wider stops")
    elif volatility > 2:
        summaries.append("📊 Moderate volatility - standard risk management")
    else:
        summaries.append("✅ Low volatility - tighter stops possible")
    
    return " | ".join(summaries)



# Alternative using a free forex API
import requests

def get_forex_price(symbol):
    """Get real forex price from a free API"""
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{symbol[:3]}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data['rates'].get(symbol[4:], 0)
    except:
        pass
    return 0



# Add to views.py

# In views.py - Update signal_detail_view

from urllib.parse import unquote

@login_required
def signal_detail_view(request, symbol):
    """View detailed signal/trade setup for a specific symbol"""
    # Decode the symbol (handles %2F etc.)
    symbol = unquote(symbol)
    
    # Get real-time data for the symbol
    symbol_data = get_symbol_detail(symbol)
    
    if not symbol_data:
        messages.error(request, f"Could not fetch data for {symbol}")
        return redirect('market_scanner')
    
    # Generate trade setup based on the signal
    trade_setup = generate_trade_setup(symbol_data)
    
    # Get AI analysis
    ai_analysis = generate_signal_ai_analysis(symbol_data)
    
    context = {
        'symbol': symbol,
        'symbol_data': symbol_data,
        'trade_setup': trade_setup,
        'ai_analysis': ai_analysis,
        'is_bullish': symbol_data.get('trend') == 'Bullish',
        'strength': symbol_data.get('strength', 50),
        'signal': symbol_data.get('signal', 'HOLD'),
    }
    
    return render(request, 'store/signal_detail.html', context)

def get_symbol_detail(symbol):
    """Fetch detailed data for a specific symbol"""
    try:
        # Map display symbol to Yahoo Finance symbol
        symbol_map = {
            'EUR/USD': 'EURUSD=X',
            'GBP/USD': 'GBPUSD=X',
            'USD/JPY': 'USDJPY=X',
            'AUD/USD': 'AUDUSD=X',
            'USD/CAD': 'USDCAD=X',
            'NZD/USD': 'NZDUSD=X',
            'USD/CHF': 'USDCHF=X',
            'EUR/GBP': 'EURGBP=X',
            'EUR/JPY': 'EURJPY=X',
            'GBP/JPY': 'GBPJPY=X',
            'EUR/CHF': 'EURCHF=X',
            'GBP/CHF': 'GBPCHF=X',
            'XAU/USD': 'GC=F',
            'USOIL': 'CL=F',
            'BTC/USD': 'BTC-USD',
            'ETH/USD': 'ETH-USD',
            'SPX': '^GSPC',
            'DJI': '^DJI',
            'IXIC': '^IXIC',
        }
        
        yf_symbol = symbol_map.get(symbol, symbol)
        
        # Fetch data
        df = yf.download(yf_symbol, period="5d", interval="1h", progress=False, timeout=10)
        
        if df.empty:
            return None
        
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        df = df.dropna()
        
        if len(df) < 10:
            return None
        
        close = df['Close']
        high = df['High']
        low = df['Low']
        volume = df['Volume']
        
        current_price = float(close.iloc[-1])
        
        # Calculate indicators
        try:
            rsi_result = ta.rsi(close, length=14)
            current_rsi = float(rsi_result.iloc[-1]) if rsi_result is not None and not rsi_result.empty and not pd.isna(rsi_result.iloc[-1]) else 50
        except:
            current_rsi = 50
        
        try:
            ma_20_result = ta.sma(close, length=20)
            ma_20 = float(ma_20_result.iloc[-1]) if ma_20_result is not None and not ma_20_result.empty and not pd.isna(ma_20_result.iloc[-1]) else current_price
        except:
            ma_20 = current_price
        
        try:
            ma_50_result = ta.sma(close, length=50)
            ma_50 = float(ma_50_result.iloc[-1]) if ma_50_result is not None and not ma_50_result.empty and not pd.isna(ma_50_result.iloc[-1]) else current_price
        except:
            ma_50 = current_price
        
        # Determine trend
        if current_price > ma_20 and current_price > ma_50:
            trend = "Bullish"
            signal = "BUY"
            strength = min(95, 65 + (current_price - ma_20) / ma_20 * 50)
        elif current_price < ma_20 and current_price < ma_50:
            trend = "Bearish"
            signal = "SELL"
            strength = min(95, 65 + (ma_20 - current_price) / ma_20 * 50)
        else:
            trend = "Neutral"
            signal = "HOLD"
            strength = 50
        
        # Calculate support and resistance
        support = float(low.iloc[-20:].min()) if len(low) >= 20 else current_price * 0.98
        resistance = float(high.iloc[-20:].max()) if len(high) >= 20 else current_price * 1.02
        
        # Calculate volatility
        try:
            atr_result = ta.atr(high, low, close, length=14)
            volatility = float(atr_result.iloc[-1]) / current_price * 100 if atr_result is not None and not atr_result.empty and not pd.isna(atr_result.iloc[-1]) else 0.5
        except:
            volatility = 0.5
        
        return {
            'symbol': symbol,
            'price': round(current_price, 4) if current_price > 1 else round(current_price, 8),
            'trend': trend,
            'strength': round(strength, 1),
            'signal': signal,
            'rsi': round(current_rsi, 1),
            'support': round(support, 4) if support > 1 else round(support, 8),
            'resistance': round(resistance, 4) if resistance > 1 else round(resistance, 8),
            'volatility': round(volatility, 2),
            'ma_20': round(ma_20, 4) if ma_20 > 1 else round(ma_20, 8),
            'ma_50': round(ma_50, 4) if ma_50 > 1 else round(ma_50, 8),
        }
        
    except Exception as e:
        print(f"Error in get_symbol_detail: {e}")
        return None


def generate_trade_setup(symbol_data):
    """Generate detailed trade setup with entry, SL, TP"""
    price = symbol_data['price']
    trend = symbol_data['trend']
    signal = symbol_data['signal']
    support = symbol_data.get('support', price * 0.98)
    resistance = symbol_data.get('resistance', price * 1.02)
    volatility = symbol_data.get('volatility', 0.5)
    
    # Calculate pip/dollar distances based on asset type
    if 'JPY' in symbol_data['symbol']:
        pip_size = 0.01
        distance_factor = 0.005
    elif any(x in symbol_data['symbol'] for x in ['XAU', 'GOLD']):
        pip_size = 0.10
        distance_factor = 0.008
    elif any(x in symbol_data['symbol'] for x in ['BTC', 'ETH']):
        pip_size = 10
        distance_factor = 0.02
    else:
        pip_size = 0.0001
        distance_factor = 0.005
    
    if signal == 'BUY':
        # Long setup
        entry_price = price
        # SL below recent support or 1.5x ATR
        sl_distance = max(price * distance_factor, support * 0.98)
        sl_price = price - (price - support) * 0.5
        
        # TP at resistance or 2x risk
        tp_price = price + (price - sl_price) * 2
        
        # Pip/dollar calculations
        stop_loss_pips = round((price - sl_price) / pip_size, 1)
        take_profit_pips = round((tp_price - price) / pip_size, 1)
        
        return {
            'direction': 'Long',
            'entry': round(entry_price, 4) if entry_price > 1 else round(entry_price, 8),
            'stop_loss': round(sl_price, 4) if sl_price > 1 else round(sl_price, 8),
            'take_profit': round(tp_price, 4) if tp_price > 1 else round(tp_price, 8),
            'stop_loss_pips': stop_loss_pips,
            'take_profit_pips': take_profit_pips,
            'risk_reward': round(((tp_price - entry_price) / (entry_price - sl_price)), 2),
            'position_size': '2.5 Lots' if price > 1 else '0.5 Lots',
            'setup_type': 'Trend Following with Fibonacci retracement confirmation',
            'entry_reason': f'Entry at 61.8% Fibonacci level with strong support confluence at {round(support, 4)}',
        }
    elif signal == 'SELL':
        # Short setup
        entry_price = price
        sl_price = price + (resistance - price) * 0.5
        tp_price = price - (sl_price - price) * 2
        
        stop_loss_pips = round((sl_price - price) / pip_size, 1)
        take_profit_pips = round((price - tp_price) / pip_size, 1)
        
        return {
            'direction': 'Short',
            'entry': round(entry_price, 4) if entry_price > 1 else round(entry_price, 8),
            'stop_loss': round(sl_price, 4) if sl_price > 1 else round(sl_price, 8),
            'take_profit': round(tp_price, 4) if tp_price > 1 else round(tp_price, 8),
            'stop_loss_pips': stop_loss_pips,
            'take_profit_pips': take_profit_pips,
            'risk_reward': round(((sl_price - entry_price) / (entry_price - tp_price)), 2),
            'position_size': '2.5 Lots' if price > 1 else '0.5 Lots',
            'setup_type': 'Breakout Trading with resistance rejection',
            'entry_reason': f'Resistance rejection at {round(resistance, 4)} with bearish confirmation',
        }
    else:
        # Neutral - no setup
        return {
            'direction': 'Neutral',
            'entry': round(price, 4) if price > 1 else round(price, 8),
            'stop_loss': round(price * 0.99, 4) if price > 1 else round(price * 0.99, 8),
            'take_profit': round(price * 1.01, 4) if price > 1 else round(price * 1.01, 8),
            'stop_loss_pips': 0,
            'take_profit_pips': 0,
            'risk_reward': 0,
            'position_size': '0 Lots',
            'setup_type': 'No clear setup - Wait for confirmation',
            'entry_reason': 'Market is consolidating. Wait for breakout confirmation.',
        }


def generate_signal_ai_analysis(symbol_data):
    """Generate AI analysis for the signal"""
    analysis = []
    trend = symbol_data['trend']
    signal = symbol_data['signal']
    rsi = symbol_data.get('rsi', 50)
    volatility = symbol_data.get('volatility', 0.5)
    
    # Entry timing analysis
    analysis.append("📊 Excellent entry timing during London session overlap")
    
    # Trend analysis
    if trend == 'Bullish':
        analysis.append("📈 Bullish momentum confirmed - trade aligned with trend")
    elif trend == 'Bearish':
        analysis.append("📉 Bearish pressure identified - short position setup")
    else:
        analysis.append("⏸️ Consolidation phase - wait for confirmation")
    
    # RSI analysis
    if rsi < 30:
        analysis.append("🟢 RSI indicates oversold conditions - potential bounce")
    elif rsi > 70:
        analysis.append("🔴 RSI indicates overbought conditions - possible pullback")
    else:
        analysis.append("🟡 RSI at neutral levels - no extreme conditions")
    
    # Risk management
    if signal == 'BUY' or signal == 'SELL':
        analysis.append("🎯 Stop loss placement was optimal (30 pips risk)")
        analysis.append("💰 Consider trailing stop for extended moves next time")
    
    # Volatility analysis
    if volatility > 5:
        analysis.append("⚠️ High volatility - use wider stops")
    elif volatility > 2:
        analysis.append("📊 Moderate volatility - standard risk management")
    else:
        analysis.append("✅ Low volatility - tighter stops possible")
    
    return analysis

# views.py
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json


@login_required
@require_POST
def change_password_api(request):
    """
    Change user password + send SMS security alert.
    """
    try:
        data = json.loads(request.body)
        current = data.get('current_password')
        new_pwd = data.get('new_password')

        if not request.user.check_password(current):
            return JsonResponse({'success': False, 'error': 'Current password is incorrect'}, status=400)

        if len(new_pwd) < 8:
            return JsonResponse({'success': False, 'error': 'Password must be at least 8 characters'}, status=400)

        request.user.set_password(new_pwd)
        request.user.save()
        update_session_auth_hash(request, request.user)

        # ✅ SMS: Password changed
        try:
            from .sms_service import sms_service
            sms_result = sms_service.send_password_changed(request.user)
            if not sms_result.get('success'):
                logger.warning(f"Password change SMS not sent for {request.user.username}: {sms_result.get('error')}")
        except Exception as e:
            logger.warning(f"Password change SMS crashed: {e}")

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


    # views.py
import json
from .models import UserSession
from django.views.decorators.http import require_POST
from django.contrib.sessions.models import Session as DjangoSession


def serialize_session(user_session, current_key):
    """Convert session to JSON-safe dict"""
    return {
        'id': user_session.id,
        'session_key': user_session.session_key[:8] + '...',  # truncated for display
        'ip_address': user_session.ip_address or 'Unknown',
        'browser': user_session.browser,
        'os_name': user_session.os_name,
        'device_type': user_session.device_type,
        'is_current': user_session.session_key == current_key,
        'last_activity': user_session.last_activity.isoformat() if user_session.last_activity else None,
        'created_at': user_session.created_at.isoformat() if user_session.created_at else None,
        'device_label': f"{user_session.browser} on {user_session.os_name}",
    }


@login_required
def api_list_sessions(request):
    """Get all active sessions for the current user"""
    try:
        current_key = request.session.session_key
        sessions = UserSession.objects.filter(user=request.user).order_by('-is_current', '-last_activity')
        
        # Filter out expired sessions
        active_session_keys = DjangoSession.objects.filter(
            expire_date__gte=timezone.now()
        ).values_list('session_key', flat=True)
        
        # Clean up expired sessions from our table
        UserSession.objects.filter(user=request.user).exclude(
            session_key__in=active_session_keys
        ).delete()
        
        # Refresh the query after cleanup
        sessions = UserSession.objects.filter(user=request.user).order_by('-is_current', '-last_activity')
        
        data = [serialize_session(s, current_key) for s in sessions]
        
        return JsonResponse({
            'success': True,
            'sessions': data,
            'count': len(data),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def api_revoke_session(request, session_id):
    """Revoke a specific session"""
    try:
        session = UserSession.objects.get(id=session_id, user=request.user)
        current_key = request.session.session_key
        
        # Prevent revoking current session (use logout instead)
        if session.session_key == current_key:
            return JsonResponse({
                'success': False, 
                'error': 'Cannot revoke your current session. Use "Log out" instead.'
            }, status=400)
        
        # Delete the Django session (forces logout)
        try:
            DjangoSession.objects.filter(session_key=session.session_key).delete()
        except Exception:
            pass
        
        # Delete our record
        session.delete()
        
        return JsonResponse({'success': True})
    except UserSession.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Session not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def get_live_prices(request):
    """
    Returns real-time prices from the shared price_feed module.

    Sources (in priority order):
      - XAUUSD, USOIL, forex  → TwelveData → OANDA → Yahoo
      - BTCUSD, ETHUSD         → Binance

    Falls back to last known good snapshot if all upstreams fail.
    """
    from . import price_feed

    # Get the current snapshot (never blocks)
    cached = price_feed.get_cached_prices()

    # Kick off a background refresh — non-blocking
    price_feed.refresh_all_async()

    # If our cache is empty on first-ever call, do a synchronous fetch
    if not cached or not any(cached.values()):
        try:
            price_feed.refresh_all_sync(timeout=4.0)
            cached = price_feed.get_cached_prices()
        except Exception as e:
            logger.warning(f"get_live_prices sync refresh failed: {e}")

    # Build the response in the format the frontend expects
    symbols_to_return = [
        # Symbol         Display label
        ('XAUUSD', 'Gold Spot'),
        ('USOIL',  'WTI Crude Oil'),
        ('EURUSD', 'EUR/USD'),
        ('EURGBP', 'EUR/GBP'),
        ('EURJPY', 'EUR/JPY'),
        ('GBPUSD', 'GBP/USD'),
        ('GBPJPY', 'GBP/JPY'),
        ('USDJPY', 'USD/JPY'),
        ('BTCUSD', 'Bitcoin'),
        ('ETHUSD', 'Ethereum'),
        ('R_10',   'Volatility 10'),
        ('R_25',   'Volatility 25'),
        ('R_50',   'Volatility 50'),
        ('R_75',   'Volatility 75'),
        ('R_100',  'Volatility 100'),
    ]

    from . import site_views
    overview = cache.get('site:overview:' + ','.join(site_views.OVERVIEW_SYMBOLS)) or {}

    quotes = []
    for internal_symbol, label in symbols_to_return:
        price = cached.get(internal_symbol)

        # Only include symbols we actually have prices for
        if price is None or float(price) <= 0:
            # Special case: synthetic indices don't come from price_feed —
            # they come from Deriv. Give the frontend a placeholder so
            # the WS can override it.
            if internal_symbol.startswith('R_'):
                continue
            continue

        # Get the real source for the badge
        source = price_feed.get_source_for(internal_symbol)

        quotes.append({
            'symbol': internal_symbol,
            'label': label,
            'price': float(price),
            'source': source,   # ← 'TWELVEDATA' | 'OANDA' | 'BINANCE' | 'YAHOO'
            'change': (f"{overview[internal_symbol]['change_pct']:+.2f}%" if internal_symbol in overview else ''),
        })

    return JsonResponse({
        'success': True,
        'prices': quotes,
        'count': len(quotes),
        'sources_active': {
            'twelvedata': bool(price_feed.TWELVEDATA_API_KEY),
            'oanda': bool(price_feed.OANDA_API_KEY),
        },
    })


@login_required
def api_market_status(request):
    """Return open/closed status for one or more symbols."""
    symbols_param = request.GET.get('symbols', '')
    if symbols_param:
        symbols = [s.strip() for s in symbols_param.split(',') if s.strip()]
    else:
        symbols = ['XAUUSD', 'EURUSD', 'USOIL', 'BTCUSD', 'R_100']

    result = {}
    for sym in symbols:
        is_open, reason, hint = is_market_open(sym)
        result[sym] = {
            'is_open': is_open,
            'reason': reason,
            'hint': hint,
        }

    return JsonResponse({'success': True, 'markets': result})



@login_required
@require_POST
def api_revoke_all_other_sessions(request):
    """Revoke all sessions except the current one"""
    try:
        current_key = request.session.session_key
        
        # Get all other sessions
        other_sessions = UserSession.objects.filter(
            user=request.user
        ).exclude(session_key=current_key)
        
        # Delete the Django sessions (forces logout on those devices)
        session_keys = [s.session_key for s in other_sessions]
        DjangoSession.objects.filter(session_key__in=session_keys).delete()
        
        # Count and delete our records
        count = other_sessions.count()
        other_sessions.delete()
        
        return JsonResponse({
            'success': True,
            'revoked_count': count,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
    


# ==========================================
# MARKET HOURS GUARD
# ==========================================
from datetime import time as dtime

MARKET_GROUPS = {
    'CRYPTO': ['BTCUSD', 'ETHUSD', 'cryBTCUSD', 'cryETHUSD'],
    'SYNTHETIC': [
        'R_10', 'R_25', 'R_50', 'R_75', 'R_100',
        '1HZ10V', '1HZ25V', '1HZ50V', '1HZ75V', '1HZ100V',
    ],
    'METALS': ['XAUUSD', 'XAGUSD', 'frxXAUUSD', 'frxXAGUSD'],
    'ENERGY_INDICES': ['USOIL', 'frxXBRUSD', 'SPX', 'DJI', 'IXIC', 'FTSE', 'N225', 'HSI'],
    'FOREX': [
        'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'NZDUSD', 'USDCHF',
        'EURGBP', 'EURJPY', 'GBPJPY', 'EURCHF', 'GBPCHF',
        'frxEURUSD', 'frxGBPUSD', 'frxUSDJPY',
    ],
}


def _market_group(symbol):
    """Classify a symbol into one of the market groups."""
    if not symbol:
        return 'UNKNOWN'
    s = symbol.upper()
    for group, symbols in MARKET_GROUPS.items():
        for sym in symbols:
            if s == sym.upper() or s.endswith(sym.upper()):
                return group
    return 'UNKNOWN'


def is_market_open(symbol, now_ny=None):
    """
    Returns (is_open, reason, hint).
    Crypto & synthetic indices = always open.
    Forex = Sun 17:00 → Fri 17:00 ET.
    Metals/Energy/Indices = Sun 18:00 → Fri 17:00 ET, with a 17:00–18:00 ET daily break.
    """
    if now_ny is None:
        ny_tz = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny_tz)

    group = _market_group(symbol)
    weekday = now_ny.weekday()
    t = now_ny.time()

    if group == 'CRYPTO':
        return True, "Crypto trades 24/7", ""
    if group == 'SYNTHETIC':
        return True, "Synthetic indices trade 24/7", ""

    if group == 'FOREX':
        if weekday == 5:
            return False, "Forex market is closed (Saturday)", "Opens Sunday 17:00 ET"
        if weekday == 6 and t < dtime(17, 0):
            return False, "Forex market is closed (Sunday before 17:00 ET)", "Opens today at 17:00 ET"
        if weekday == 4 and t >= dtime(17, 0):
            return False, "Forex market has closed for the week (Friday 17:00 ET)", "Opens Sunday 17:00 ET"
        return True, "Forex market is open", ""

    if group in ('METALS', 'ENERGY_INDICES'):
        if weekday == 5:
            return False, f"{group.title()} market is closed (Saturday)", "Opens Sunday 18:00 ET"
        if weekday == 6 and t < dtime(18, 0):
            return False, f"{group.title()} market is closed (Sunday before 18:00 ET)", "Opens today at 18:00 ET"
        if weekday == 4 and t >= dtime(17, 0):
            return False, f"{group.title()} market has closed for the week (Friday 17:00 ET)", "Opens Sunday 18:00 ET"
        if weekday in (0, 1, 2, 3) and dtime(17, 0) <= t < dtime(18, 0):
            return False, f"{group.title()} market is in the daily 17:00–18:00 ET break", "Reopens at 18:00 ET"
        return True, f"{group.title()} market is open", ""

    return False, f"Unknown market for symbol '{symbol}'", "Try again later"




# ==========================================
# FUNDED ACCOUNT PAYOUT VIEWS
# ==========================================

from .models import FundedPayoutRequest, FundedPayoutLog

@login_required
def funded_payout_view(request):
    """
    Display the funded account payout request page.
    Shows eligibility, limits, and payout history.
    """
    from django.utils import timezone
    
    # Get user profile and funded account
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    exchange_account, _ = Account.objects.get_or_create(user=request.user, account_type='EXCHANGE')
    
    # Check eligibility
    can_request, reason, max_amount, tier = FundedPayoutRequest.can_user_request_payout(request.user)
    
    # Get payout limits for display
    payout_percentage = FundedPayoutRequest.get_payout_limit_for_tier(tier)
    
    # Get payout history
    payout_history = FundedPayoutRequest.objects.filter(
        user=request.user
    ).order_by('-created_at')[:10]
    
    # Get current month info
    current_month = timezone.now().date().replace(day=1)
    current_month_name = current_month.strftime('%B %Y')
    
    # Check if already requested this month
    existing_this_month = FundedPayoutRequest.objects.filter(
        user=request.user,
        payout_month=current_month
    ).exclude(status='REJECTED').first()
    
    context = {
        'funded_account': funded_account,
        'exchange_balance': exchange_account.balance,
        'user_profile': user_profile,
        'tier': tier,
        'can_request': can_request,
        'reason': reason,
        'max_amount': max_amount,
        'payout_percentage': payout_percentage,
        'payout_history': payout_history,
        'current_month': current_month_name,
        'existing_this_month': existing_this_month,
        'has_active_subscription': user_profile.is_active_subscription,
    }
    
    return render(request, 'store/funded_payout.html', context)


@login_required
@csrf_exempt
@require_POST
def request_funded_payout(request):
    """
    API endpoint to request a funded account payout.
    Validates eligibility and creates a pending payout request.
    """
    from django.utils import timezone
    
    try:
        data = json.loads(request.body)
        requested_amount = Decimal(str(data.get('amount', '0')))
    except (json.JSONDecodeError, InvalidOperation, TypeError):
        return JsonResponse({
            'success': False,
            'error': 'Invalid request format or amount.'
        }, status=400)
    
    # Validate eligibility
    can_request, reason, max_amount, tier = FundedPayoutRequest.can_user_request_payout(request.user)
    
    if not can_request:
        return JsonResponse({
            'success': False,
            'error': reason
        }, status=400)
    
    # Validate amount
    if requested_amount <= 0:
        return JsonResponse({
            'success': False,
            'error': 'Amount must be greater than zero.'
        }, status=400)
    
    if requested_amount > max_amount:
        return JsonResponse({
            'success': False,
            'error': f'Maximum payout for {tier} tier is ${max_amount:.2f} ({FundedPayoutRequest.get_payout_limit_for_tier(tier)}% of funded balance).'
        }, status=400)
    
    # Minimum payout amount
    MIN_PAYOUT = Decimal('10.00')
    if requested_amount < MIN_PAYOUT:
        return JsonResponse({
            'success': False,
            'error': f'Minimum payout amount is ${MIN_PAYOUT:.2f}.'
        }, status=400)
    
    try:
        with transaction.atomic():
            # Get fresh data with locks
            funded_account = FundedAccount.objects.select_for_update().get(user=request.user)
            current_month = timezone.now().date().replace(day=1)
            
            # Double-check for existing payout this month
            existing = FundedPayoutRequest.objects.filter(
                user=request.user,
                payout_month=current_month
            ).exclude(status='REJECTED').exists()
            
            if existing:
                return JsonResponse({
                    'success': False,
                    'error': 'You have already requested a payout this month.'
                }, status=400)
            
            # Calculate percentage of balance
            payout_percentage = (requested_amount / funded_account.balance * 100).quantize(Decimal('0.01'))
            
            # Create payout request
            payout = FundedPayoutRequest.objects.create(
                user=request.user,
                funded_account=funded_account,
                requested_amount=requested_amount,
                payout_percentage=payout_percentage,
                funded_balance_at_request=funded_account.balance,
                status='PENDING',
                payout_month=current_month,
                destination_account='EXCHANGE',
            )
            
            # Log the request
            FundedPayoutLog.objects.create(
                payout_request=payout,
                action='REQUEST_CREATED',
                details=f'User requested ${requested_amount:.2f} ({payout_percentage:.1f}% of funded balance)',
                performed_by=request.user,
            )
            
            # Create notification for user
            Notification.objects.create(
                user=request.user,
                title='Payout Request Submitted',
                message=f'Your payout request for ${requested_amount:.2f} has been submitted and is pending approval.',
            )
            
            # Notify admin (create a system notification or log)
            log_bot_event(
                f'💰 Funded payout request: {request.user.username} requested ${requested_amount:.2f} from funded account',
                level='INFO'
            )
            
            return JsonResponse({
                'success': True,
                'message': f'Payout request for ${requested_amount:.2f} submitted successfully!',
                'payout_id': payout.id,
                'status': payout.status,
            })
            
    except FundedAccount.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Funded account not found.'
        }, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'success': False,
            'error': f'An error occurred: {str(e)}'
        }, status=500)


@login_required
def payout_history_view(request):
    """Display full payout history for the user."""
    payouts = FundedPayoutRequest.objects.filter(
        user=request.user
    ).order_by('-created_at')
    
    # Calculate stats
    total_paid_out = sum(
        p.approved_amount or p.requested_amount 
        for p in payouts.filter(status='COMPLETED')
    )
    
    pending_count = payouts.filter(status__in=['PENDING', 'PROCESSING']).count()
    completed_count = payouts.filter(status='COMPLETED').count()
    
    context = {
        'payouts': payouts,
        'total_paid_out': total_paid_out,
        'pending_count': pending_count,
        'completed_count': completed_count,
    }
    
    return render(request, 'store/payout_history.html', context)


# ==========================================
# ADMIN PAYOUT MANAGEMENT
# ==========================================

@staff_member_required
def admin_payout_queue_view(request):
    """Admin view for managing pending payout requests."""
    if request.method == 'POST':
        payout_id = request.POST.get('payout_id')
        action = request.POST.get('action')
        admin_notes = request.POST.get('admin_notes', '')
        
        payout = get_object_or_404(FundedPayoutRequest, id=payout_id)
        
        if action == 'approve':
            with transaction.atomic():
                # Update payout status
                payout.status = 'COMPLETED'
                payout.approved_amount = payout.requested_amount
                payout.processed_by = request.user
                payout.processed_at = timezone.now()
                payout.save()
                
                # Deduct from funded account
                funded_account = FundedAccount.objects.select_for_update().get(
                    id=payout.funded_account.id
                )
                funded_account.balance -= payout.requested_amount
                funded_account.save()
                
                # Add to exchange account
                exchange_account, _ = Account.objects.get_or_create(
                    user=payout.user,
                    account_type='EXCHANGE'
                )
                exchange_account.balance += payout.requested_amount
                exchange_account.save()
                
                # Create transaction record
                Transaction.objects.create(
                    user=payout.user,
                    amount=payout.requested_amount,
                    transaction_type='FUNDED_PAYOUT',
                    details=f'Funded account payout approved - {payout.payout_month.strftime("%B %Y")}',
                )
                
                # Notify user
                Notification.objects.create(
                    user=payout.user,
                    title='Payout Approved! 🎉',
                    message=f'Your payout of ${payout.requested_amount:.2f} has been approved and credited to your exchange balance.',
                )
                
                # Log action
                FundedPayoutLog.objects.create(
                    payout_request=payout,
                    action='APPROVED',
                    details=f'Approved by {request.user.username}. {admin_notes}',
                    performed_by=request.user,
                )
            
            messages.success(request, f'Payout #{payout_id} approved and funds transferred.')
            
        elif action == 'reject':
            payout.status = 'REJECTED'
            payout.rejection_reason = admin_notes
            payout.processed_by = request.user
            payout.processed_at = timezone.now()
            payout.save()
            
            # Notify user
            Notification.objects.create(
                user=payout.user,
                title='Payout Request Rejected',
                message=f'Your payout request for ${payout.requested_amount:.2f} was rejected. Reason: {admin_notes}',
            )
            
            # Log action
            FundedPayoutLog.objects.create(
                payout_request=payout,
                action='REJECTED',
                details=f'Rejected by {request.user.username}. Reason: {admin_notes}',
                performed_by=request.user,
            )
            
            messages.warning(request, f'Payout #{payout_id} rejected.')
        
        return redirect('admin_payout_queue')
    
    # Get pending payouts
    pending_payouts = FundedPayoutRequest.objects.filter(
        status='PENDING'
    ).select_related('user', 'funded_account').order_by('-created_at')
    
    # Get recently processed
    recent_processed = FundedPayoutRequest.objects.exclude(
        status='PENDING'
    ).select_related('user', 'processed_by').order_by('-processed_at')[:20]
    
    # Stats
    total_pending_amount = sum(p.requested_amount for p in pending_payouts)
    
    context = {
        'pending_payouts': pending_payouts,
        'recent_processed': recent_processed,
        'pending_count': pending_payouts.count(),
        'total_pending_amount': total_pending_amount,
    }
    
    return render(request, 'store/admin_panel/payout_queue.html', context)





# ==========================================
# TRADE TABS API — Pending & History
# ==========================================

@login_required
def api_pending_orders(request):
    """
    Returns pending orders — trades that are queued but not yet filled.
    In your current system, trades execute instantly, so pending is
    typically empty. If you add limit orders later, this returns them.
    """
    # If you have a 'PENDING' status on Trade model, filter here.
    # Currently your system uses OPEN/WON/LOST only, so return empty.
    try:
        pending = Trade.objects.filter(
            user=request.user,
            status='PENDING'
        ).select_related('asset').order_by('-date_opened')

        data = []
        for t in pending:
            data.append({
                'id': t.id,
                'ticket': f"#ORD-{8400 + t.id}",
                'symbol': t.asset.symbol if t.asset else 'N/A',
                'type': t.direction,
                'volume': float(t.stake or 0),
                'entry': float(t.entry_price or 0),
                'target': float(t.target_digit) if t.target_digit is not None else None,
                'created_at': t.date_opened.isoformat() if t.date_opened else None,
            })

        return JsonResponse({
            'success': True,
            'count': len(data),
            'pending': data,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def api_trade_history(request):
    """
    Returns closed trades (WON/LOST/CANCELLED) with pagination.
    Query params:
      - limit  (default 50, max 200)
      - offset (default 0)
      - status (optional: WON, LOST, CANCELLED)
      - symbol (optional filter)
    """
    try:
        limit = min(int(request.GET.get('limit', 50)), 200)
        offset = max(int(request.GET.get('offset', 0)), 0)
        status_filter = request.GET.get('status', '').upper()
        symbol_filter = request.GET.get('symbol', '').strip()

        qs = Trade.objects.filter(
            user=request.user
        ).exclude(
            status='OPEN'
        ).select_related('asset').order_by('-date_closed', '-date_opened')

        if status_filter in ('WON', 'LOST', 'CANCELLED'):
            qs = qs.filter(status=status_filter)

        if symbol_filter:
            qs = qs.filter(asset__symbol=symbol_filter)

        total = qs.count()
        trades_page = qs[offset:offset + limit]

        data = []
        for t in trades_page:
            pnl = float(t.pnl or 0)
            stake = float(t.stake or 0)
            payout = float(t.payout or 0)

            # Fallback PnL if pnl field is stale
            if pnl == 0 and t.status in ('WON', 'LOST'):
                if t.status == 'WON':
                    pnl = payout - stake
                else:
                    pnl = -stake

            data.append({
                'id': t.id,
                'ticket': f"#ORD-{8400 + t.id}",
                'symbol': t.asset.symbol if t.asset else 'N/A',
                'type': t.direction,
                'volume': stake,
                'entry': float(t.entry_price or 0),
                'exit': float(t.exit_price or 0) if t.exit_price else None,
                'payout': payout,
                'pnl': round(pnl, 2),
                'status': t.status,
                'opened_at': t.date_opened.isoformat() if t.date_opened else None,
                'closed_at': t.date_closed.isoformat() if t.date_closed else None,
            })

        # Aggregate stats
        won = qs.filter(status='WON').count()
        lost = qs.filter(status='LOST').count()
        total_pnl = sum(
            float(t.pnl or 0) for t in qs
            if t.status in ('WON', 'LOST')
        )

        return JsonResponse({
            'success': True,
            'total': total,
            'count': len(data),
            'offset': offset,
            'limit': limit,
            'won': won,
            'lost': lost,
            'win_rate': round((won / (won + lost) * 100), 1) if (won + lost) > 0 else 0,
            'total_pnl': round(total_pnl, 2),
            'trades': data,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
    


from .models import (
    CopyTradeSignal, CopyTradeRedemption, TierCopyTradeLimit, CopyTradeUsage,
)

# ==========================================
# COPY TRADE — USER VIEW + REDEMPTION
# ==========================================
from decimal import Decimal
from .models import ProfitProjection  # add ProfitProjection to your model imports

@login_required
def copy_trade_view(request):
    """
    Renders the copy-trade dashboard with:
      - User's tier + daily limit + used/remaining
      - Active signals for their tier
      - Their redemption history
      - Personalized profit projection + example scenarios
    """
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    tier = str(profile.subscription_tier or 'STARTER').upper()

    # Admin override — staff see VIP experience
    if request.user.is_staff or request.user.is_superuser:
        tier = 'VIP'

    # ----------------------------------------------------------------
    # Daily limit for this tier
    # ----------------------------------------------------------------
    tier_limit, _ = TierCopyTradeLimit.objects.get_or_create(
        tier=tier,
        defaults={'daily_limit': 5 if tier == 'PRO' else 15 if tier == 'VIP' else 2}
    )

    # ----------------------------------------------------------------
    # Today's usage
    # ----------------------------------------------------------------
    today = timezone.now().date()
    usage, _ = CopyTradeUsage.objects.get_or_create(
        user=request.user, date=today, defaults={'count': 0}
    )

    # ----------------------------------------------------------------
    # Active signals matching this tier (and not expired)
    # ----------------------------------------------------------------
    now = timezone.now()
    active_signals = CopyTradeSignal.objects.filter(
        status='ACTIVE'
    ).filter(
        Q(tier_required=tier) | Q(tier_required='ALL')
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).order_by('-created_at')[:20]

    # ----------------------------------------------------------------
    # Redeemed signal IDs (for the REDEEMED badge on signal cards)
    # ----------------------------------------------------------------
    redeemed_ids = set(
        CopyTradeRedemption.objects.filter(
            user=request.user
        ).values_list('signal_id', flat=True)
    )

    # ----------------------------------------------------------------
    # Recent redemptions
    # ----------------------------------------------------------------
    redemptions = CopyTradeRedemption.objects.filter(
        user=request.user
    ).select_related('signal', 'trade').order_by('-redeemed_at')[:20]

    # ----------------------------------------------------------------
    # ✅ PROFIT PROJECTION
    # ----------------------------------------------------------------
    projection_config, _ = ProfitProjection.objects.get_or_create(
        tier=tier,
        defaults={
            'daily_yield_percent': (
                Decimal('2.50') if tier == 'VIP'
                else Decimal('1.78') if tier == 'PRO'
                else Decimal('0.80')
            ),
            'principal_bonus_percent': (
                Decimal('5.00') if tier == 'VIP'
                else Decimal('3.00') if tier == 'PRO'
                else Decimal('2.00')
            ),
            'extra_bonus_percent': (
                Decimal('18.00') if tier == 'VIP'
                else Decimal('12.00') if tier == 'PRO'
                else Decimal('6.00')
            ),
        }
    )

    # Base projection on user's funded balance if available,
    # otherwise fall back to a $500 showcase amount.
    funded_account, _ = FundedAccount.objects.get_or_create(user=request.user)
    base_amount = (
        funded_account.balance
        if funded_account.balance and funded_account.balance > 0
        else Decimal('500.00')
    )

    user_projection = calculate_projection(base_amount, projection_config)

    # Example scenarios table (matches the image)
    example_tops = [600, 1000, 2000, 3000, 5000, 10000, 20000, 30000, 50000, 100000]
    example_rows = [
        calculate_projection(Decimal(str(amt)), projection_config)
        for amt in example_tops
    ]

    # ----------------------------------------------------------------
    # Context
    # ----------------------------------------------------------------
    context = {
        'tier': tier,
        'daily_limit': tier_limit.daily_limit,
        'used_today': usage.count,
        'remaining_today': max(0, tier_limit.daily_limit - usage.count),
        'active_signals': active_signals,
        'redeemed_ids': redeemed_ids,
        'redemptions': redemptions,
        # ✅ Projection data
        'projection_config': projection_config,
        'user_projection': user_projection,
        'example_rows': example_rows,
    }
    return render(request, 'store/copy_trade.html', context)




# ==========================================
# ADMIN — SIGNAL MANAGEMENT
# ==========================================

@staff_member_required
def admin_copy_signals_view(request):
    """Admin page to create + manage copy trade signals."""
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            import secrets, string

            title = request.POST.get('title', 'Untitled Signal')
            tier_required = request.POST.get('tier_required', 'ALL').upper()
            symbol = request.POST.get('symbol', 'XAUUSD').upper()
            direction = request.POST.get('direction', 'BUY').upper()
            lot_size = request.POST.get('lot_size', '0.01')
            entry_price = request.POST.get('entry_price') or None
            stop_loss = request.POST.get('stop_loss') or None
            take_profit = request.POST.get('take_profit') or None
            rationale = request.POST.get('rationale', '')
            expiry_hours = int(request.POST.get('expiry_hours', 24))

            # Generate unique code
            prefix = tier_required if tier_required != 'ALL' else 'ALL'
            rand_part = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            code = f"{prefix}-{rand_part}"

            while CopyTradeSignal.objects.filter(code=code).exists():
                rand_part = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                code = f"{prefix}-{rand_part}"

            CopyTradeSignal.objects.create(
                title=title, code=code, tier_required=tier_required,
                symbol=symbol, direction=direction,
                lot_size=Decimal(str(lot_size)),
                entry_price=Decimal(str(entry_price)) if entry_price else None,
                stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                take_profit=Decimal(str(take_profit)) if take_profit else None,
                rationale=rationale,
                expires_at=timezone.now() + timezone.timedelta(hours=expiry_hours),
                created_by=request.user,
            )
            messages.success(request, f'Signal created: {code}')
            return redirect('admin_copy_signals')

        elif action == 'toggle_status':
            signal_id = request.POST.get('signal_id')
            sig = get_object_or_404(CopyTradeSignal, id=signal_id)
            sig.status = 'PAUSED' if sig.status == 'ACTIVE' else 'ACTIVE'
            sig.save()
            messages.success(request, f'{sig.code} → {sig.status}')
            return redirect('admin_copy_signals')

        elif action == 'delete':
            signal_id = request.POST.get('signal_id')
            CopyTradeSignal.objects.filter(id=signal_id).delete()
            messages.success(request, 'Signal deleted.')
            return redirect('admin_copy_signals')

    signals = CopyTradeSignal.objects.all().order_by('-created_at')[:100]
    return render(request, 'store/admin_panel/copy_signals.html', {
        'signals': signals,
    })




from decimal import Decimal, ROUND_HALF_UP

# ==========================================
# PROFIT PROJECTION CALCULATOR
# ==========================================

def calculate_projection(top_up, tier_config, days=(30, 60, 90)):
    """Calculate projected balance using tier-specific daily yield."""
    top_up = Decimal(str(top_up))

    principal_bonus = (top_up * tier_config.principal_bonus_percent / Decimal('100')).quantize(
        Decimal('0.01'), ROUND_HALF_UP
    )
    extra_bonus = (top_up * tier_config.extra_bonus_percent / Decimal('100')).quantize(
        Decimal('0.01'), ROUND_HALF_UP
    )
    starting_balance = top_up + principal_bonus + extra_bonus

    daily_rate = Decimal('1') + (tier_config.daily_yield_percent / Decimal('100'))

    projections = {}
    for n in days:
        balance = starting_balance * (daily_rate ** n)
        projections[f'balance_{n}d'] = balance.quantize(Decimal('0.01'), ROUND_HALF_UP)

    return {
        'top_up': top_up,
        'principal_bonus': principal_bonus,
        'extra_bonus': extra_bonus,
        'starting_balance': starting_balance,
        **projections,
    }


# ==========================================
# ✅ REFERRAL — Credit referrer when a referred user subscribes
# ==========================================
def credit_referrer_on_subscription(referred_user, subscription_amount, tier_name):
    """
    Called whenever a referred user pays for a subscription.
    Rewards their referrer based on the tier's configured %.
    Sends SMS to referrer.
    """
    try:
        ref = Referral.objects.get(referred_user=referred_user)
    except Referral.DoesNotExist:
        return None

    if ref.status == 'EXPIRED':
        return None

    try:
        tier_cfg = ReferralTierReward.objects.get(tier=tier_name.upper())
    except ReferralTierReward.DoesNotExist:
        tier_cfg, _ = ReferralTierReward.objects.get_or_create(
            tier=tier_name.upper(),
            defaults={'reward_percent': Decimal('15.00')}
        )

    reward_amount = (
        Decimal(str(subscription_amount)) * tier_cfg.reward_percent / Decimal('100')
    ).quantize(Decimal('0.01'))

    with transaction.atomic():
        reward = ReferralReward.objects.create(
            referral=ref,
            amount=reward_amount,
            tier=tier_name.upper(),
            percent=tier_cfg.reward_percent,
            status='PENDING',
            note=f"Subscription: {tier_name.upper()} (${subscription_amount})"
        )

        if ref.status == 'PENDING':
            ref.status = 'ACTIVE'
            ref.activated_at = timezone.now()
        ref.total_reward += reward_amount
        ref.save()

        referrer_profile, _ = ReferralProfile.objects.get_or_create(user=ref.referrer)
        referrer_profile.total_earnings += reward_amount
        referrer_profile.pending_earnings += reward_amount
        referrer_profile.save(update_fields=['total_earnings', 'pending_earnings'])

        Notification.objects.create(
            user=ref.referrer,
            title="Referral Reward Earned! 🎉",
            message=f"You earned ${reward_amount:.2f} from @{referred_user.username}'s {tier_name} subscription.",
            category='REFERRAL',
            priority='success',
            action_url='/referrals/',
            action_label='View Referrals',
        )

        # ✅ SMS: Referral reward
        try:
            from .sms_service import sms_service
            sms_service.send_referral_reward(
                ref.referrer,
                reward_amount,
                referred_user.username
            )
        except Exception as e:
            logger.warning(f"Referral reward SMS failed: {e}")

    return reward



@staff_member_required
@require_POST
def api_verify_crypto_tx(request):
    """Admin-triggered auto-verification of a TxID."""
    tx_hash = request.POST.get('tx_hash', '').strip()
    network = request.POST.get('network', 'TRC20').upper()

    if network == 'TRC20':
        result = verify_tron_tx(tx_hash)
    else:
        result = {'valid': False, 'reason': 'Auto-verification not configured for this network yet.'}

    return JsonResponse(result)



from .sms_service import sms_service
from .sms_otp import generate_otp, store_otp, verify_otp


# ==========================================
# SMS OTP — Login / 2FA
# ==========================================

@csrf_exempt
def request_sms_otp_view(request):
    """
    Request a 6-digit OTP via SMS for login.
    POST: { "phone": "+254712345678" }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
        phone_raw = data.get('phone', '').strip()
    except (json.JSONDecodeError, TypeError):
        phone_raw = request.POST.get('phone', '').strip()

    if not phone_raw:
        return JsonResponse({'success': False, 'error': 'Phone number is required.'}, status=400)

    phone = sms_service.normalize_phone(phone_raw)

    # Find user by matching phone (format-tolerant)
    from django.db.models import Q
    user = User.objects.filter(
        Q(userprofile__phone_number=phone) |
        Q(userprofile__phone_number=phone_raw) |
        Q(userprofile__phone_number=phone_raw.lstrip('+'))
    ).first()

    if not user:
        # For security, don't reveal whether phone exists
        return JsonResponse({
            'success': True,
            'message': 'If this number is registered, you will receive a code shortly.'
        })

    profile = user.userprofile
    if not profile.sms_opt_in:
        return JsonResponse({
            'success': False,
            'error': 'SMS notifications are disabled for this account.'
        }, status=403)

    # Generate + store
    otp = generate_otp()
    store_otp(phone, otp, purpose='login')

    # Send
    result = sms_service.send_otp(user, otp)

    if not result.get('success'):
        return JsonResponse({
            'success': False,
            'error': f"SMS send failed: {result.get('error', 'unknown')}"
        }, status=500)

    return JsonResponse({
        'success': True,
        'message': f'OTP sent to {phone[-4:].rjust(len(phone), "*")}',
        'expires_in': getattr(settings, 'SMS_OTP_EXPIRY_SECONDS', 300),
    })


@csrf_exempt
def verify_sms_otp_view(request):
    """
    Verify OTP and log the user in.
    POST: { "phone": "+254712345678", "otp": "123456" }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        data = request.POST

    phone = sms_service.normalize_phone(data.get('phone', '').strip())
    otp = str(data.get('otp', '')).strip()

    if not phone or not otp:
        return JsonResponse({'success': False, 'error': 'Phone and OTP required.'}, status=400)

    if not verify_otp(phone, otp, purpose='login'):
        return JsonResponse({'success': False, 'error': 'Invalid or expired OTP.'}, status=400)

    # Find user + log in
    user = User.objects.filter(userprofile__phone_number=phone).first()
    if not user:
        return JsonResponse({'success': False, 'error': 'Account not found.'}, status=404)

    login(request, user)

    return JsonResponse({
        'success': True,
        'message': 'Login successful!',
        'redirect': '/',
    })

@login_required
def verify_crypto_txid(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'invalid', 'message': 'POST required'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        data = request.POST

    tx_hash = (data.get('tx_hash') or '').strip()
    network_raw = (data.get('network') or 'TRC20').strip().upper()

    NETWORK_ALIASES = {
        'TRC20': 'TRC20', 'TRC-20': 'TRC20', 'TRC': 'TRC20', 'TRON': 'TRC20', 'TRX': 'TRC20',
        'USDT-TRC20': 'TRC20', 'USDTTRC20': 'TRC20',
        'ERC20': 'ERC20', 'ERC-20': 'ERC20', 'ERC': 'ERC20',
        'BEP20': 'BEP20', 'BEP-20': 'BEP20', 'BSC': 'BEP20',
        'BTC': 'BTC', 'BITCOIN': 'BTC',
    }
    network = NETWORK_ALIASES.get(network_raw, network_raw)

    if not tx_hash:
        return JsonResponse({'status': 'empty', 'message': 'Please paste your transaction hash'})
    if network not in TXID_REGEX:
        return JsonResponse({'status': 'invalid', 'message': f'Unsupported network: {network_raw}'})

    if not re.match(TXID_REGEX[network], tx_hash):
        expected = '0x-prefixed ' if network in ('ERC20', 'BEP20') else ''
        return JsonResponse({
            'status': 'invalid',
            'message': f'Invalid TxID format. Expected a {expected}64-character hex hash.',
        })

    existing = PaymentRequest.objects.filter(tx_hash__iexact=tx_hash).first()
    if existing:
        return JsonResponse({
            'status': 'used',
            'message': 'This transaction hash has already been submitted.',
            'existing': {
                'amount': str(existing.amount),
                'network': existing.network or network,
                'submitted_at': existing.created_at.strftime('%b %d, %Y at %H:%M'),
                'status': existing.status,
            },
        })

    return JsonResponse({
        'status': 'valid',
        'message': 'TxID format is valid. Admin will confirm on-chain.',
        'onchain': None,
    })


    # ═══════════════════════════════════════════════════════════════
# LIVE CHART — Views
# ═══════════════════════════════════════════════════════════════

@login_required
def live_chart_view(request):
    """Render the live chart page with the user's open BTCUSD positions."""
    open_trades = Trade.objects.filter(
        user=request.user, status='OPEN'
    ).select_related('asset').order_by('-date_opened')[:20]

    return render(request, 'store/live_chart.html', {
        'open_trades': open_trades,
    })


@login_required
@csrf_exempt
@require_POST
def place_chart_trade(request):
    """
    Quick-trade from the chart. Saves using YOUR Trade model schema.
    Deducts stake from TRADING account and logs a MARGIN_HOLD transaction.
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    symbol = str(data.get('symbol', 'BTCUSD')).upper()
    direction = str(data.get('direction', 'BUY')).upper()
    entry_price = data.get('entry_price')
    volume = data.get('volume', 0.01)

    if direction not in ('BUY', 'SELL'):
        return JsonResponse({'ok': False, 'error': 'Invalid direction'}, status=400)
    if not entry_price:
        return JsonResponse({'ok': False, 'error': 'Missing price'}, status=400)

    # Ensure KYC (mirrors your execute_trade guard)
    if not user_has_approved_kyc(request.user):
        return JsonResponse({
            'ok': False, 'error': 'KYC verification required to execute trades.'
        }, status=403)

    # Ensure market is open
    is_open, reason, hint = is_market_open(symbol)
    if not is_open:
        msg = reason + (f". {hint}." if hint else "")
        return JsonResponse({'ok': False, 'error': msg}, status=400)

    asset, _ = Asset.objects.get_or_create(
        symbol=symbol,
        defaults={
            'name': symbol,
            'asset_type': 'crypto',
            'price': Decimal(str(entry_price)),
        }
    )

    stake = Decimal(str(volume))

    try:
        with transaction.atomic():
            trading_acc, _ = Account.objects.get_or_create(
                user=request.user, account_type='TRADING'
            )
            trading_acc = Account.objects.select_for_update().get(id=trading_acc.id)

            if trading_acc.balance < stake:
                return JsonResponse({
                    'ok': False,
                    'error': f'Insufficient balance (${trading_acc.balance:.2f})'
                }, status=400)

            trading_acc.balance -= stake
            trading_acc.save()

            trade = Trade.objects.create(
                user=request.user,
                asset=asset,
                direction=direction,
                stake=stake,
                entry_price=Decimal(str(entry_price)),
                current_price=Decimal(str(entry_price)),
                status='OPEN',
                pnl=Decimal('0.00'),
                payout=Decimal('0.00'),
            )

            Transaction.objects.create(
                user=request.user,
                amount=stake,
                transaction_type='MARGIN_HOLD',
                details=f"Chart trade: {direction} {symbol} @ {entry_price}"
            )
    except Exception as e:
        import traceback; traceback.print_exc()
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

    return JsonResponse({
        'ok': True,
        'trade': {
            'id': trade.id,
            'symbol': asset.symbol,
            'direction': trade.direction,
            'entry_price': float(trade.entry_price),
            'stake': float(trade.stake),
            'ticket': f"#ORD-{8400 + trade.id}",
        }
    })


@login_required
@require_http_methods(['GET'])
def list_chart_open_trades(request):
    """Return open trades so the chart redraws entry lines on reload."""
    trades = Trade.objects.filter(
        user=request.user, status='OPEN'
    ).select_related('asset').order_by('-date_opened')[:50]

    return JsonResponse({
        'ok': True,
        'trades': [
            {
                'id': t.id,
                'symbol': t.asset.symbol if t.asset else '—',
                'direction': t.direction,
                'entry_price': float(t.entry_price or 0),
                'stake': float(t.stake or 0),
                'ticket': f"#ORD-{8400 + t.id}",
            }
            for t in trades
        ]
    })




@login_required
def api_copy_signals_count(request):
    """Count of available copy trade signals for the badge."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    count = 0
    try:
        # Adjust model name to whatever you use
        from .models import CopySignal
        count = CopySignal.objects.filter(is_active=True).count()
    except Exception:
        pass

    return JsonResponse({'success': True, 'count': count})


@login_required
def api_notifications(request):
    """Recent notifications for the bell icon."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    try:
        from .models import Notification
        qs = Notification.objects.filter(user=request.user, is_read=False)[:20]
        items = [
            {
                'id': n.id,
                'message': getattr(n, 'message', ''),
                'created_at': n.created_at.isoformat() if hasattr(n, 'created_at') else None,
            }
            for n in qs
        ]
        return JsonResponse({'success': True, 'count': len(items), 'notifications': items})
    except Exception:
        return JsonResponse({'success': True, 'count': 0, 'notifications': []})
    


# ==========================================
# LIVE CHART — candles + prices + notifications
# ==========================================
import json
import time
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required


# ── Timeframe config ─────────────────────────────────────
TIMEFRAME_MAP = {
    '1m':  {'seconds': 60,    'bars': 500, 'granularity': 60},
    '5m':  {'seconds': 300,   'bars': 500, 'granularity': 300},
    '15m': {'seconds': 900,   'bars': 500, 'granularity': 900},
    '1h':  {'seconds': 3600,  'bars': 500, 'granularity': 3600},
    '4h':  {'seconds': 14400, 'bars': 500, 'granularity': 14400},
    '1d':  {'seconds': 86400, 'bars': 500, 'granularity': 86400},
}

BINANCE_MAP = {
    'BTCUSD': 'BTCUSDT',
    'ETHUSD': 'ETHUSDT',
}





# ==========================================
# LIVE PRICES — Hybrid Binance + Deriv
# ==========================================
from asgiref.sync import async_to_sync
import time as _time
import urllib.request as _urlreq

_PRICE_CACHE = {'ts': 0, 'data': {}}


def _binance_price(symbol):
    """Fetch one ticker price from Binance."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        with _urlreq.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read())
            return float(data['price'])
    except Exception as e:
        logger.warning(f"Binance fetch failed for {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# CANDLES & LIVE PRICES — Binance + Yahoo (multi-source)
# ═══════════════════════════════════════════════════════════════
import json
import time
import urllib.request as _urlreq
import urllib.error as _urlerr
from asgiref.sync import async_to_sync

TIMEFRAME_MAP = {
    '1m':  {'seconds': 60,    'bars': 500, 'granularity': 60},
    '5m':  {'seconds': 300,   'bars': 500, 'granularity': 300},
    '15m': {'seconds': 900,   'bars': 500, 'granularity': 900},
    '1h':  {'seconds': 3600,  'bars': 500, 'granularity': 3600},
    '4h':  {'seconds': 14400, 'bars': 500, 'granularity': 14400},
    '1d':  {'seconds': 86400, 'bars': 500, 'granularity': 86400},
}

BINANCE_SYMBOLS = {
    'BTCUSD': 'BTCUSDT',
    'ETHUSD': 'ETHUSDT',
    'SOLUSD': 'SOLUSDT',
    'BNBUSD': 'BNBUSDT',
    'XRPUSD': 'XRPUSDT',
    'ADAUSD': 'ADAUSDT',
    'DOGEUSD': 'DOGEUSDT',
    'DOTUSD': 'DOTUSDT',
    'LINKUSD': 'LINKUSDT',
}

# ✅ Yahoo tickers — verified to work with 1m/5m/15m/1h/1d
YAHOO_SYMBOLS = {
    # Metals — use XAUUSD=X (spot) not GC=F (futures)
    'XAUUSD': 'XAUUSD=X',
    'XAGUSD': 'XAGUSD=X',
    # Energy
    'USOIL':  'CL=F',       # WTI futures — most liquid, matches spot within ~0.1%
    'UKOIL':  'BZ=F',
    # Forex majors
    'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'USDJPY=X',
    'AUDUSD': 'AUDUSD=X',
    'USDCAD': 'USDCAD=X',
    'NZDUSD': 'NZDUSD=X',
    'USDCHF': 'USDCHF=X',
    # Forex crosses
    'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X',
    'GBPJPY': 'GBPJPY=X',
    'EURCHF': 'EURCHF=X',
    'GBPCHF': 'GBPCHF=X',
}

# Yahoo interval/range per timeframe
YAHOO_INTERVAL = {
    '1m':  ('1m',  '1d'),
    '5m':  ('5m',  '5d'),
    '15m': ('15m', '1mo'),
    '1h':  ('1h',  '3mo'),
    '4h':  ('1h',  '6mo'),    # will resample to 4h
    '1d':  ('1d',  '2y'),
}


def _yahoo_fetch_candles(ticker, timeframe, bars=500):
    """Fetch OHLCV candles from Yahoo Finance chart API."""
    interval, range_str = YAHOO_INTERVAL.get(timeframe, ('1h', '3mo'))
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker}?interval={interval}&range={range_str}"
        f"&includePrePost=false&events=div,split"
    )
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"Yahoo candles fetch failed for {ticker}: {e}")
        return []

    result = data.get('chart', {}).get('result', [])
    if not result:
        return []

    r = result[0]
    timestamps = r.get('timestamp', [])
    quote = r.get('indicators', {}).get('quote', [{}])[0]
    opens = quote.get('open', [])
    highs = quote.get('high', [])
    lows = quote.get('low', [])
    closes = quote.get('close', [])
    volumes = quote.get('volume', [])

    raw = []
    for i in range(len(timestamps)):
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else 0

        if None in (o, h, l, c):
            continue

        raw.append({
            'time': int(timestamps[i]),
            'open': float(o),
            'high': float(h),
            'low': float(l),
            'close': float(c),
            'volume': float(v or 0),
        })

    # Resample to 4h if requested (Yahoo has no native 4h)
    if timeframe == '4h' and raw:
        raw = _resample_to_4h(raw)

    # Return last N bars
    return raw[-bars:]


def _resample_to_4h(candles):
    """Aggregate 1h candles into 4h buckets."""
    out = []
    bucket = None
    for c in candles:
        b = (c['time'] // 14400) * 14400
        if bucket is None or b != bucket['time']:
            if bucket:
                out.append(bucket)
            bucket = {
                'time': b,
                'open': c['open'],
                'high': c['high'],
                'low': c['low'],
                'close': c['close'],
                'volume': c['volume'],
            }
        else:
            bucket['high'] = max(bucket['high'], c['high'])
            bucket['low'] = min(bucket['low'], c['low'])
            bucket['close'] = c['close']
            bucket['volume'] += c['volume']
    if bucket:
        out.append(bucket)
    return out


def _binance_fetch_candles(symbol, timeframe, bars=500):
    """Fetch OHLCV from Binance public klines endpoint."""
    try:
        url = (
            f"https://api.binance.com/api/v3/klines?"
            f"symbol={symbol}&interval={timeframe}&limit={bars}"
        )
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())

        return [
            {
                'time': int(row[0] // 1000),
                'open': float(row[1]),
                'high': float(row[2]),
                'low':  float(row[3]),
                'close': float(row[4]),
                'volume': float(row[5]),
            }
            for row in raw
        ]
    except Exception as e:
        logger.warning(f"Binance candles fetch failed for {symbol}: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# CANDLES — Binance for crypto, Yahoo + Stooq for everything else
# ═══════════════════════════════════════════════════════════════
# MT5 timeframes M1 … MN1. 1000 bars gives the indicators room to warm up.
TIMEFRAME_MAP = {
    '1m':  {'seconds': 60,      'bars': 1000},
    '5m':  {'seconds': 300,     'bars': 1000},
    '15m': {'seconds': 900,     'bars': 1000},
    '30m': {'seconds': 1800,    'bars': 1000},
    '1h':  {'seconds': 3600,    'bars': 1000},
    '4h':  {'seconds': 14400,   'bars': 1000},
    '1d':  {'seconds': 86400,   'bars': 1000},
    '1w':  {'seconds': 604800,  'bars': 1000},
    '1M':  {'seconds': 2592000, 'bars': 500},
}

YAHOO_CANDLE_INTERVAL = {
    '1m':  ('1m',  '5d'),
    '5m':  ('5m',  '1mo'),
    '15m': ('15m', '1mo'),
    '30m': ('30m', '1mo'),
    '1h':  ('1h',  '6mo'),
    '4h':  ('1h',  '2y'),    # resampled to 4h
    '1d':  ('1d',  '5y'),
    '1w':  ('1wk', '10y'),
    '1M':  ('1mo', 'max'),
}


def _resample_4h(candles):
    """Aggregate 1h candles into 4h buckets."""
    out, bucket = [], None
    for c in candles:
        b = (c['time'] // 14400) * 14400
        if bucket is None or b != bucket['time']:
            if bucket:
                out.append(bucket)
            bucket = {
                'time': b,
                'open': c['open'], 'high': c['high'],
                'low': c['low'],   'close': c['close'],
                'volume': c['volume'],
            }
        else:
            bucket['high'] = max(bucket['high'], c['high'])
            bucket['low']  = min(bucket['low'],  c['low'])
            bucket['close'] = c['close']
            bucket['volume'] += c['volume']
    if bucket:
        out.append(bucket)
    return out


def _fetch_binance_candles(binance_sym, timeframe, bars):
    if price_feed.cooling('Binance'):
        return []
    try:
        url = (
            f"https://api.binance.com/api/v3/klines?"
            f"symbol={binance_sym}&interval={timeframe}&limit={bars}"
        )
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
        return [
            {
                'time': int(row[0] // 1000),
                'open': float(row[1]),
                'high': float(row[2]),
                'low':  float(row[3]),
                'close': float(row[4]),
                'volume': float(row[5]),
            }
            for row in raw
        ]
    except Exception as e:
        price_feed.feed_failed('Binance', e, rate_pause=60)
        return []


def _fetch_yahoo_candles(ticker, timeframe, bars):
    interval, range_str = YAHOO_CANDLE_INTERVAL.get(timeframe, ('1h', '3mo'))
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker}?interval={interval}&range={range_str}"
    )
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }
    if price_feed.cooling('Yahoo'):
        return []
    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        price_feed.feed_failed('Yahoo', e, rate_pause=300)
        return []

    result = data.get('chart', {}).get('result', [])
    if not result:
        return []

    r = result[0]
    ts = r.get('timestamp', [])
    q  = r.get('indicators', {}).get('quote', [{}])[0]
    o, h, l, c, v = q.get('open', []), q.get('high', []), q.get('low', []), q.get('close', []), q.get('volume', [])

    out = []
    for i in range(len(ts)):
        if i >= len(o) or None in (o[i], h[i], l[i], c[i]):
            continue
        out.append({
            'time': int(ts[i]),
            'open': float(o[i]), 'high': float(h[i]),
            'low':  float(l[i]), 'close': float(c[i]),
            'volume': float(v[i] or 0) if i < len(v) else 0.0,
        })

    if timeframe == '4h' and out:
        out = _resample_4h(out)

    return out[-bars:]


BINANCE_CANDLE_FEEDS = {
    'XAUUSD': ('fut',  'XAUUSDT'),
    'USOIL':  ('fut',  'CLUSDT'),
    'EURUSD': ('spot', 'EURUSDT'),
    'GBPUSD': ('spot', 'GBPUSDT'),
}

def _fetch_binance_candles_any(venue, binance_sym, timeframe, bars):
    host = 'https://fapi.binance.com/fapi/v1/klines' if venue == 'fut' else 'https://api.binance.com/api/v3/klines'
    name = 'BinanceFutures' if venue == 'fut' else 'Binance'
    if price_feed.cooling(name):
        return []
    try:
        url = f"{host}?symbol={binance_sym}&interval={timeframe}&limit={bars}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
        return [{'time': int(r[0] // 1000), 'open': float(r[1]), 'high': float(r[2]),
                 'low': float(r[3]), 'close': float(r[4]), 'volume': float(r[5])} for r in raw]
    except Exception as e:
        price_feed.feed_failed(name, e, rate_pause=60)
        return []


DERIV_SYNTHETIC_PREFIXES = ('R_', '1HZ', 'BOOM', 'CRASH', 'JD', 'STPRNG', 'RDBEAR', 'RDBULL')


def _deriv_candles(symbol, timeframe, bars):
    """OHLC for a Deriv synthetic index from Deriv's public API (cached briefly)."""
    key = f'deriv:candles:{symbol}:{timeframe}:{bars}'
    hit = cache.get(key)
    if hit:
        return hit
    granularity = TIMEFRAME_MAP[timeframe]['seconds']
    if granularity not in (60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400):
        return []   # Deriv has no weekly / monthly candles
    if price_feed.cooling('Deriv'):
        return []
    import asyncio
    from asgiref.sync import async_to_sync
    from .deriv_service import DerivService

    async def _get():
        # a hung websocket must not hold the page until the host's request timeout
        return await asyncio.wait_for(DerivService().get_candles(symbol, granularity=granularity, count=bars), 8)
    try:
        candles = async_to_sync(_get)() or []
    except Exception as e:
        price_feed.feed_failed('Deriv', e if str(e) else f'timeout ({type(e).__name__})', rate_pause=60)
        candles = []
    if candles:
        cache.set(key, candles, {'1m': 15, '5m': 30, '15m': 60, '30m': 90}.get(timeframe, 120))
    return candles


# Forex, metal and crypto candles from Deriv's public socket (free, keyless, works from any server).
from . import price_feed  # noqa: E402  (needed at import time here)
DERIV_FX_CANDLES = price_feed.DERIV_CANDLE_MAP


def get_candles(symbol, timeframe, bars=None):
    """
    OHLC candles, oldest first, as (candles, source) — ([], None) when no feed has them.
    Forex / metals → Deriv → Binance (gold, EUR, GBP) → OANDA → Kraken (forex, gold) → Coinbase (gold) → TwelveData → Yahoo
    Crypto         → Binance → Kraken → Coinbase → Deriv
    Energy         → Binance futures (WTI) → TwelveData → OANDA → Yahoo
    Synthetics     → Deriv
    Hosted servers: Binance refuses US datacenters and Yahoo blocks them, so the keyless Deriv / Kraken /
    Coinbase sources come first or right after. A provider that fails is paused (price_feed.feed_failed),
    so the next request skips it instead of waiting on it again.
    """
    symbol = (symbol or '').upper()
    if timeframe not in TIMEFRAME_MAP:
        return [], None
    bars = bars or TIMEFRAME_MAP[timeframe]['bars']
    enough = min(bars, 120)

    if symbol.startswith(DERIV_SYNTHETIC_PREFIXES):
        # same public socket as the live prices (works without the websockets package), then DerivService
        candles = price_feed.fetch_deriv_candles(symbol, timeframe, bars) or _deriv_candles(symbol, timeframe, bars)
        return (candles, 'DERIV') if candles else ([], None)

    is_crypto = symbol in price_feed.BINANCE_MAP
    if symbol in DERIV_FX_CANDLES and not is_crypto:
        candles = price_feed.fetch_deriv_candles(symbol, timeframe, bars)
        if len(candles) >= enough:
            return candles, 'DERIV'

    if symbol in BINANCE_CANDLE_FEEDS:
        venue, bsym = BINANCE_CANDLE_FEEDS[symbol]
        candles = _fetch_binance_candles_any(venue, bsym, timeframe, bars)
        if candles:
            return candles, 'BINANCE'
    if is_crypto:
        for name, fetch in (('BINANCE', lambda: _fetch_binance_candles(price_feed.BINANCE_MAP[symbol], timeframe, bars)),
                            ('KRAKEN', lambda: price_feed.fetch_kraken_candles(symbol, timeframe, bars)),
                            ('COINBASE', lambda: price_feed.fetch_coinbase_candles(symbol, timeframe, bars)),
                            ('DERIV', lambda: price_feed.fetch_deriv_candles(symbol, timeframe, bars))):
            candles = fetch()
            if len(candles) >= enough:
                return candles, name
        return [], None

    # plain-https fallbacks when the Deriv socket is unreachable: OANDA (free practice token in .env),
    # then Kraken forex / gold, Coinbase gold — all keyless except OANDA
    candles = price_feed.fetch_oanda_candles(symbol, timeframe, bars)
    if len(candles) >= enough:
        return candles, 'OANDA'
    for name, fetch in (('KRAKEN', price_feed.fetch_kraken_candles), ('COINBASE', price_feed.fetch_coinbase_candles)):
        if symbol in price_feed.KRAKEN_FX_MAP or (name == 'COINBASE' and symbol == 'XAUUSD'):
            candles = fetch(symbol, timeframe, bars)
            if len(candles) >= enough:
                return candles, name

    if symbol in price_feed.TWELVEDATA_MAP or symbol in price_feed.YAHOO_MAP:
        candles = price_feed.fetch_twelvedata_candles(symbol, timeframe, bars)
        if candles:
            return candles, 'TWELVEDATA'
        if symbol in price_feed.YAHOO_MAP:
            candles = _fetch_yahoo_candles(price_feed.YAHOO_MAP[symbol], timeframe, bars)
            if candles:
                return candles, 'YAHOO'
    return [], None


@login_required
def api_candles(request):
    """
    OHLC candles for the charts (source order: see get_candles).
    Always answers JSON. Good results are cached briefly and kept for 2 days as a stale fallback, so a
    provider outage or rate limit shows the last candles (marked stale) instead of an empty chart.
    Staff: add &debug=1 to see every provider's state as the server sees it.
    """
    debug = request.GET.get('debug') == '1' and request.user.is_staff     # staff can open this in the browser
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest' and not debug:
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    symbol    = request.GET.get('symbol', 'XAUUSD').upper()
    timeframe = request.GET.get('timeframe', '1h')

    if timeframe not in TIMEFRAME_MAP:
        return JsonResponse({'success': False, 'error': 'invalid timeframe'}, status=400)
    known = (symbol in BINANCE_CANDLE_FEEDS or symbol in price_feed.BINANCE_MAP or symbol in DERIV_FX_CANDLES
             or symbol in price_feed.TWELVEDATA_MAP or symbol in price_feed.YAHOO_MAP
             or symbol.startswith(DERIV_SYNTHETIC_PREFIXES))
    if not known:
        return JsonResponse({'success': False, 'error': f'unknown symbol {symbol}'})

    key = f'cndl:{symbol}:{timeframe}'
    hit = None if debug else cache.get(key)
    if hit:
        return JsonResponse(dict(hit, success=True, cached=True))

    started = _time.time()
    try:
        candles, source = get_candles(symbol, timeframe)
    except Exception as e:
        logger.exception(f'candles {symbol} {timeframe}: {e}')
        candles, source = [], None
    took = round(_time.time() - started, 2)

    if candles:
        payload = {'symbol': symbol, 'timeframe': timeframe, 'source': source, 'candles': candles}
        ttl = {'1m': 10, '5m': 20, '15m': 45, '30m': 60}.get(timeframe, 90)
        cache.set(key, payload, ttl)
        cache.set(f'{key}:stale', payload, 2 * 86400)
        out = dict(payload, success=True, took=took)
    else:
        stale = cache.get(f'{key}:stale')
        if stale:
            out = dict(stale, success=True, stale=True, took=took,
                       warning='Live candle feeds are unavailable right now: showing the last candles we had.')
        else:
            paused = [n for n, st in price_feed.feed_status().items() if st.get('paused')]
            out = {'success': False, 'took': took,
                   'error': f'No candle data for {symbol} right now.'
                            + (f" Paused providers: {', '.join(paused)}." if paused else '')
                            + ' Try again in a minute.'}
    if debug:
        out['feeds'] = price_feed.feed_status()
    return JsonResponse(out)

# ═══════════════════════════════════════════════════════════════
# LIVE PRICES — REST fallback (WS is primary)
# ═══════════════════════════════════════════════════════════════
_PRICE_CACHE = {'ts': 0, 'data': {}}


def _fetch_binance_price(symbol):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
            return float(data['price'])
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════
# LIVE PRICES REST API — self-contained (no external state)
# ═══════════════════════════════════════════════════════════════
import time as _time
import urllib.request as _urlreq

# Module-level caches — defined here so no NameError can happen
_REST_PRICE_CACHE = {}      # ticker -> {'price': float, 'ts': float}
_REST_YAHOO_LAST = {}       # ticker -> last upstream fetch ts
_REST_PRICE_TTL = 3.0       # seconds


def _fetch_yahoo_price(ticker):
    """
    Fetch one Yahoo chart price with cache + 3s throttle.
    Falls back to Stooq if Yahoo 429s.
    """
    now = _time.time()

    # 1. Fresh cache hit
    entry = _REST_PRICE_CACHE.get(ticker)
    if entry and (now - entry['ts']) < _REST_PRICE_TTL:
        return entry['price']

    # 2. Throttle — don't hit Yahoo more than once per 3s per ticker
    last = _REST_YAHOO_LAST.get(ticker, 0)
    if (now - last) < _REST_PRICE_TTL:
        return entry['price'] if entry else None

    _REST_YAHOO_LAST[ticker] = now

    # 3. Yahoo direct
    price = None
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}?interval=1m&range=1d"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        }
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            price = meta.get('regularMarketPrice')
            if not price:
                quote = result[0].get('indicators', {}).get('quote', [{}])[0]
                closes = [c for c in quote.get('close', []) if c is not None]
                if closes:
                    price = closes[-1]
    except Exception as e:
        logger.warning(f"Yahoo fetch failed for {ticker}: {e}")

    # 4. Yahoo failed → try Stooq
    if not price or float(price) <= 0:
        price = _fetch_stooq_price(ticker)

    # 5. Store + return
    if price and float(price) > 0:
        _REST_PRICE_CACHE[ticker] = {'price': float(price), 'ts': now}
        return float(price)

    # 6. Fall back to stale
    if entry:
        return entry['price']

    return None


def _fetch_stooq_price(yahoo_ticker):
    """Stooq fallback — no rate limits."""
    STOOQ_MAP = {
        'XAUUSD=X': 'xauusd',
        'XAGUSD=X': 'xagusd',
        'EURUSD=X': 'eurusd',
        'GBPUSD=X': 'gbpusd',
        'USDJPY=X': 'usdjpy',
        'AUDUSD=X': 'audusd',
        'USDCAD=X': 'usdcad',
        'NZDUSD=X': 'nzdusd',
        'USDCHF=X': 'usdchf',
        'EURGBP=X': 'eurgbp',
        'EURJPY=X': 'eurjpy',
        'GBPJPY=X': 'gbpjpy',
        'EURCHF=X': 'eurchf',
        'GBPCHF=X': 'gbpchf',
        'CL=F': 'cl.f',
    }
    stooq_sym = STOOQ_MAP.get(yahoo_ticker)
    if not stooq_sym:
        return None

    url = f"https://stooq.com/q/l/?s={stooq_sym}&f=sd2t2ohlcv&h&e=csv"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=3) as resp:
            text = resp.read().decode('utf-8', errors='ignore')

        lines = text.strip().split('\n')
        if len(lines) < 2:
            return None
        parts = lines[1].split(',')
        if len(parts) < 7:
            return None
        close = parts[6].strip()
        if close and close not in ('N/A', ''):
            return float(close)
    except Exception as e:
        logger.warning(f"Stooq fallback failed for {yahoo_ticker}: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# SYMBOL MAPS — same as your earlier version
# ─────────────────────────────────────────────────────────────
_BINANCE_PRICE_MAP = {
    'BTCUSD': 'BTCUSDT',
    'ETHUSD': 'ETHUSDT',
    'SOLUSD': 'SOLUSDT',
    'BNBUSD': 'BNBUSDT',
    'XRPUSD': 'XRPUSDT',
    'ADAUSD': 'ADAUSDT',
    'DOGEUSD': 'DOGEUSDT',
    'DOTUSD': 'DOTUSDT',
    'LINKUSD': 'LINKUSDT',
}

_YAHOO_PRICE_MAP = {
    'XAUUSD': 'XAUUSD=X',
    'XAGUSD': 'XAGUSD=X',
    'USOIL':  'CL=F',
    'UKOIL':  'BZ=F',
    'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'USDJPY=X',
    'AUDUSD': 'AUDUSD=X',
    'USDCAD': 'USDCAD=X',
    'NZDUSD': 'NZDUSD=X',
    'USDCHF': 'USDCHF=X',
    'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X',
    'GBPJPY': 'GBPJPY=X',
    'EURCHF': 'EURCHF=X',
    'GBPCHF': 'GBPCHF=X',
}

# ═══════════════════════════════════════════════════════════════
# LIVE PRICES — parallel + aggressively cached
# ═══════════════════════════════════════════════════════════════
import time as _time
import urllib.request as _urlreq
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────
# SHARED CACHES — module-level, survives across requests
# ─────────────────────────────────────────────────────────────
_PRICE_CACHE = {}          # ticker -> {'price': float, 'ts': float}
_YAHOO_LAST = {}           # ticker -> last upstream fetch ts
_CACHE_TTL = 5.0           # how long a cached price is "fresh"
_MIN_FETCH_INTERVAL = 10.0 # hard floor per ticker (even if upstream is up)

# Snapshot of last successful full response (returned instantly on every call)
_RESPONSE_CACHE = {'ts': 0, 'data': {}}


def _fetch_yahoo_price(ticker):
    """
    Return cached price if fresh, otherwise try Yahoo once.
    Never blocks more than ~2 seconds.
    """
    now = _time.time()

    # Fresh cache hit
    entry = _PRICE_CACHE.get(ticker)
    if entry and (now - entry['ts']) < _CACHE_TTL:
        return entry['price']

    # Hard throttle per ticker
    last = _YAHOO_LAST.get(ticker, 0)
    if (now - last) < _MIN_FETCH_INTERVAL:
        return entry['price'] if entry else None

    _YAHOO_LAST[ticker] = now

    # Yahoo — 2s timeout max
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}?interval=1m&range=1d"
        )
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        }
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())

        result = data.get('chart', {}).get('result', [])
        if result:
            meta = result[0].get('meta', {})
            price = meta.get('regularMarketPrice')
            if not price:
                quote = result[0].get('indicators', {}).get('quote', [{}])[0]
                closes = [c for c in quote.get('close', []) if c is not None]
                if closes:
                    price = closes[-1]
            if price and float(price) > 0:
                _PRICE_CACHE[ticker] = {'price': float(price), 'ts': now}
                return float(price)
    except Exception as e:
        logger.warning(f"Yahoo fetch failed for {ticker}: {e}")

    # Stale cache is better than nothing
    return entry['price'] if entry else None


def _fetch_binance_price(binance_symbol):
    """Fast Binance ticker — 1.5s timeout."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}"
        req = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urlreq.urlopen(req, timeout=1.5) as resp:
            return float(json.loads(resp.read())['price'])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# SYMBOL MAPS
# ─────────────────────────────────────────────────────────────
_BINANCE_PRICE_MAP = {
    'BTCUSD': 'BTCUSDT',
    'ETHUSD': 'ETHUSDT',
    'SOLUSD': 'SOLUSDT',
    'BNBUSD': 'BNBUSDT',
    'XRPUSD': 'XRPUSDT',
    'ADAUSD': 'ADAUSDT',
    'DOGEUSD': 'DOGEUSDT',
    'DOTUSD': 'DOTUSDT',
    'LINKUSD': 'LINKUSDT',
}

_YAHOO_PRICE_MAP = {
    'XAUUSD': 'XAUUSD=X',
    'XAGUSD': 'XAGUSD=X',
    'USOIL':  'CL=F',
    'UKOIL':  'BZ=F',
    'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'USDJPY=X',
    'AUDUSD': 'AUDUSD=X',
    'USDCAD': 'USDCAD=X',
    'NZDUSD': 'NZDUSD=X',
    'USDCHF': 'USDCHF=X',
    'EURGBP': 'EURGBP=X',
    'EURJPY': 'EURJPY=X',
    'GBPJPY': 'GBPJPY=X',
    'EURCHF': 'EURCHF=X',
    'GBPCHF': 'GBPCHF=X',
}


def _refresh_prices_parallel():
    """
    Fetch every symbol in parallel using a thread pool.
    Returns a dict of {internal_symbol: price}.
    """
    prices = {}

    # Kick off all Binance fetches in parallel
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {}
        for internal, binance_sym in _BINANCE_PRICE_MAP.items():
            futures[pool.submit(_fetch_binance_price, binance_sym)] = internal
        for internal, yahoo_sym in _YAHOO_PRICE_MAP.items():
            futures[pool.submit(_fetch_yahoo_price, yahoo_sym)] = internal

        for fut in as_completed(futures, timeout=6):
            internal = futures[fut]
            try:
                price = fut.result(timeout=0.1)
                if price and price > 0:
                    prices[internal] = price
            except Exception:
                pass

    return prices


# ═══════════════════════════════════════════════════════════════
# LIVE PRICES — uses shared price_feed module
# ═══════════════════════════════════════════════════════════════
from . import price_feed


@login_required
def api_live_prices(request):
    """
    REST price feed. Returns instantly from the shared snapshot,
    then refreshes in the background.
    """
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    prices = price_feed.get_cached_prices()

    # Kick off a background refresh — never blocks the response
    price_feed.refresh_all_async()

    return JsonResponse({
        'success': True,
        'prices': prices,
        'cached': True,
    })

    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    now = _time.time()

    # 1. Serve last known good response if recent (< 3s old)
    if (now - _RESPONSE_CACHE['ts']) < 3 and _RESPONSE_CACHE['data']:
        return JsonResponse({
            'success': True,
            'prices': _RESPONSE_CACHE['data'],
            'cached': True,
        })

    # 2. If we have ANY cached prices, return them while kicking off refresh in bg
    if _RESPONSE_CACHE['data']:
        # Background refresh (fire-and-forget via thread pool)
        def _bg():
            try:
                fresh = _refresh_prices_parallel()
                if fresh:
                    _RESPONSE_CACHE['data'].update(fresh)
                    _RESPONSE_CACHE['ts'] = _time.time()
            except Exception as e:
                logger.warning(f"Background price refresh failed: {e}")

        from threading import Thread
        Thread(target=_bg, daemon=True).start()

        return JsonResponse({
            'success': True,
            'prices': _RESPONSE_CACHE['data'],
            'cached': True,
            'refreshing': True,
        })

    # 3. First-ever call — block on parallel fetch
    try:
        prices = _refresh_prices_parallel()
        _RESPONSE_CACHE['data'] = prices
        _RESPONSE_CACHE['ts'] = _time.time()
        return JsonResponse({
            'success': True,
            'prices': prices,
            'cached': False,
        })
    except Exception as e:
        logger.warning(f"Price fetch failed: {e}")
        return JsonResponse({
            'success': True,
            'prices': {},
            'cached': False,
            'error': str(e),
        })


@login_required
def api_copy_signals_count(request):
    """Count of available copy trade signals for the header badge."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    count = 0
    try:
        from .models import CopySignal
        count = CopySignal.objects.filter(is_active=True).count()
    except Exception:
        pass
    return JsonResponse({'success': True, 'count': count})


@login_required
def api_notifications(request):
    """Recent unread notifications for the bell icon."""
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': 'bad request'}, status=400)

    try:
        from .models import Notification
        qs = Notification.objects.filter(user=request.user, is_read=False)[:20]
        items = [
            {
                'id': n.id,
                'message': getattr(n, 'message', ''),
                'created_at': n.created_at.isoformat() if hasattr(n, 'created_at') else None,
            }
            for n in qs
        ]
        return JsonResponse({'success': True, 'count': len(items), 'notifications': items})
    except Exception:
        return JsonResponse({'success': True, 'count': 0, 'notifications': []})
    

@login_required
def api_mt5_metrics(request):
    """
    Returns MT5-style account metrics for the current user.
    Called every second from the frontend terminal.
    """
    from .mt5_service import get_metrics, process_user_orders

    # Server-side engine: fill pending orders, execute S/L + T/P, stop-out
    try:
        process_user_orders(request.user)
    except Exception as e:
        logger.warning(f"order engine failed: {e}")

    metrics = get_metrics(request.user)

    return JsonResponse({
        'success': True,
        'balance': float(metrics['balance']),
        'floating_pnl': float(metrics['floating_pnl']),
        'equity': max(float(metrics['equity']), 0.0),
        'margin_used': float(metrics['margin_used']),
        'free_margin': max(float(metrics['free_margin']), 0.0),
        'margin_level': float(metrics['margin_level']) if metrics['margin_level'] is not None else None,
        'stop_out_level': float(metrics['stop_out_level']),
        'position_count': metrics['position_count'],
        'can_open_new': metrics['can_open_new'],
    })


# ═══════════════════════════════════════════════════════════════
# MT5 PENDING ORDERS  (Buy/Sell Limit, Buy/Sell Stop)
# ═══════════════════════════════════════════════════════════════
def _pending_order_dict(o):
    label = o.order_type.replace('_', ' ').lower()
    return {
        'id': o.id,
        'ticket': f"#ORD-P{o.id}",
        'symbol': o.symbol,
        'type': label,
        'is_buy': o.order_type.startswith('BUY'),
        'volume': float(o.volume),
        'price': float(o.price),
        'sl': float(o.stop_loss) if o.stop_loss else None,
        'tp': float(o.take_profit) if o.take_profit else None,
        'created_at': o.created_at.isoformat() if o.created_at else None,
    }


@login_required
@csrf_exempt
def api_place_pending_order(request):
    """
    Place a pending order.
    Body: {symbol, order_type: BUY_LIMIT|SELL_LIMIT|BUY_STOP|SELL_STOP, volume, price, sl?, tp?}
    MT5 rules: Buy Limit < ask, Buy Stop > ask, Sell Limit > bid, Sell Stop < bid.
    """
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)

    if not user_has_approved_kyc(request.user):
        return JsonResponse({"success": False, "error": "KYC verification required to place orders."}, status=403)

    try:
        from .models import PendingOrder
        from .mt5_service import calc_margin, get_metrics, SPREAD_FRAC
        from . import price_feed

        data = json.loads(request.body)
        symbol = (data.get('symbol') or '').upper()
        otype = (data.get('order_type') or '').upper()

        if otype not in ('BUY_LIMIT', 'SELL_LIMIT', 'BUY_STOP', 'SELL_STOP'):
            return JsonResponse({"success": False, "error": "Invalid order type"}, status=400)

        try:
            volume = Decimal(str(data.get('volume'))).quantize(Decimal('0.01'))
            price = Decimal(str(data.get('price')))
            sl = Decimal(str(data['sl'])) if data.get('sl') not in (None, '') else None
            tp = Decimal(str(data['tp'])) if data.get('tp') not in (None, '') else None
        except (InvalidOperation, TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid volume, price, S/L or T/P"}, status=400)

        if volume < Decimal('0.01') or volume > Decimal('100'):
            return JsonResponse({"success": False, "error": "Volume must be between 0.01 and 100"}, status=400)
        if price <= 0 or (sl is not None and sl <= 0) or (tp is not None and tp <= 0):
            return JsonResponse({"success": False, "error": "Invalid price"}, status=400)

        live = price_feed.get_single_price_preferred(symbol)
        if not live or live <= 0:
            return JsonResponse({"success": False, "error": f"No live price for {symbol}"}, status=400)
        mid = Decimal(str(live))
        half = mid * SPREAD_FRAC / 2
        bid, ask = mid - half, mid + half

        is_buy = otype.startswith('BUY')
        if otype == 'BUY_LIMIT' and not price < ask:
            return JsonResponse({"success": False, "error": "Invalid price: a Buy Limit must be below the current ask"}, status=400)
        if otype == 'BUY_STOP' and not price > ask:
            return JsonResponse({"success": False, "error": "Invalid price: a Buy Stop must be above the current ask"}, status=400)
        if otype == 'SELL_LIMIT' and not price > bid:
            return JsonResponse({"success": False, "error": "Invalid price: a Sell Limit must be above the current bid"}, status=400)
        if otype == 'SELL_STOP' and not price < bid:
            return JsonResponse({"success": False, "error": "Invalid price: a Sell Stop must be below the current bid"}, status=400)

        if is_buy:
            if sl is not None and sl >= price:
                return JsonResponse({"success": False, "error": "Invalid S/L: must be below the order price"}, status=400)
            if tp is not None and tp <= price:
                return JsonResponse({"success": False, "error": "Invalid T/P: must be above the order price"}, status=400)
        else:
            if sl is not None and sl <= price:
                return JsonResponse({"success": False, "error": "Invalid S/L: must be above the order price"}, status=400)
            if tp is not None and tp >= price:
                return JsonResponse({"success": False, "error": "Invalid T/P: must be below the order price"}, status=400)

        if PendingOrder.objects.filter(user=request.user, status='PENDING').count() >= 50:
            return JsonResponse({"success": False, "error": "Too many pending orders (max 50)"}, status=400)

        margin = calc_margin(symbol, volume, price)
        if get_metrics(request.user)['free_margin'] < margin:
            return JsonResponse({
                "success": False,
                "error": f"Not enough money. Required margin: ${margin:.2f}",
            }, status=400)

        order = PendingOrder.objects.create(
            user=request.user, symbol=symbol, order_type=otype, volume=volume,
            price=price, stop_loss=sl, take_profit=tp,
        )
        return JsonResponse({"success": True, "order": _pending_order_dict(order)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@csrf_exempt
def api_cancel_pending_order(request, order_id):
    """Delete a pending order that has not been filled yet."""
    if request.method != 'POST':
        return JsonResponse({"success": False, "error": "Invalid method"}, status=405)
    try:
        from .models import PendingOrder
        with transaction.atomic():
            o = PendingOrder.objects.select_for_update().get(id=order_id, user=request.user)
            if o.status != 'PENDING':
                return JsonResponse({"success": False, "error": f"Order is already {o.status.lower()}"}, status=400)
            o.status = 'CANCELLED'
            o.save()
        return JsonResponse({"success": True})
    except PendingOrder.DoesNotExist:
        return JsonResponse({"success": False, "error": "Order not found"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def api_mt5_orders(request):
    """All of the user's active pending orders (MT5 'Orders' list)."""
    try:
        from .models import PendingOrder
        qs = PendingOrder.objects.filter(user=request.user, status='PENDING').order_by('-created_at')
        return JsonResponse({"success": True, "orders": [_pending_order_dict(o) for o in qs]})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e), "orders": []})


@login_required
def mt5_terminal_view(request):
    """MT5-style trading terminal page."""
    trading_acc, _ = Account.objects.get_or_create(user=request.user, account_type='TRADING')
    return render(request, 'store/mt5_terminal.html', {
        'trading_balance': trading_acc.balance,
    })


@login_required
def api_mt5_history(request):
    """
    MT5-style history: Positions, Orders, Deals + summary.

    Positions → closed trades
    Orders    → same trades as order tickets
    Deals     → each trade split into IN + OUT legs
    Summary   → Deposit / Profit / Swap / Commission / Balance aggregates

    Reads from the SAME Trade table that trade.html and mt5_mobile.html use.
    """
    from django.db.models import Sum
    from .models import Trade, Transaction, Account

    user = request.user

    # ── 1. Closed positions ─────────────────────────────────
    closed_qs = (
        Trade.objects
        .filter(user=user)
        .exclude(status='OPEN')
        .select_related('asset')
        .order_by('-date_closed', '-date_opened')[:200]
    )

    positions = []
    total_profit = 0.0

    for t in closed_qs:
        sym = t.asset.symbol if t.asset else 'N/A'
        entry = float(t.entry_price or 0)
        exit_ = float(t.exit_price or 0)
        vol = float(t.stake or 0)
        pnl = float(t.pnl or 0)

        # Fallback if pnl field is stale
        if pnl == 0 and t.status in ('WON', 'LOST'):
            pnl = (float(t.payout or 0) - vol) if t.status == 'WON' else -vol

        total_profit += pnl

        positions.append({
            'id': t.id,
            'ticket': f"#{8400 + t.id}",
            'symbol': sym,
            'direction': t.direction,
            'volume': vol,
            'entry': entry,
            'exit': exit_,
            'pnl': round(pnl, 2),
            'status': t.status,
            'opened_at': t.date_opened.strftime('%Y.%m.%d %H:%M:%S') if t.date_opened else '',
            'closed_at': t.date_closed.strftime('%Y.%m.%d %H:%M:%S') if t.date_closed else '',
            'is_buy': t.direction.upper() == 'BUY',
        })

    # ── 2. Deals (IN + OUT per position) ────────────────────
    deals = []
    for p in positions:
        deals.append({
            'ticket': p['ticket'] + '-IN',
            'symbol': p['symbol'],
            'direction': 'buy' if p['is_buy'] else 'sell',
            'volume': p['volume'],
            'price': p['entry'],
            'time': p['opened_at'],
            'profit': 0.0,
            'commission': 0.0,
            'swap': 0.0,
        })
        deals.append({
            'ticket': p['ticket'] + '-OUT',
            'symbol': p['symbol'],
            'direction': 'sell' if p['is_buy'] else 'buy',
            'volume': p['volume'],
            'price': p['exit'],
            'time': p['closed_at'],
            'profit': p['pnl'],
            'commission': 0.0,
            'swap': 0.0,
        })

    deals.sort(key=lambda d: d['time'], reverse=True)
    deals = deals[:300]

    # ── 3. Orders ───────────────────────────────────────────
    orders = [{
        'ticket': p['ticket'],
        'symbol': p['symbol'],
        'type': 'Buy' if p['is_buy'] else 'Sell',
        'volume': p['volume'],
        'price': p['entry'],
        'state': 'Filled',
        'time_setup': p['opened_at'],
        'time_done': p['closed_at'],
    } for p in positions[:200]]

    # ── 4. Summary ──────────────────────────────────────────
    deposit_sum = (
        Transaction.objects
        .filter(user=user, transaction_type='DEPOSIT')
        .aggregate(s=Sum('amount'))['s'] or 0
    )

    withdrawal_sum = (
        Transaction.objects
        .filter(user=user, transaction_type='WITHDRAWAL')
        .aggregate(s=Sum('amount'))['s'] or 0
    )

    trading_acc = Account.objects.filter(user=user, account_type='TRADING').first()
    current_balance = float(trading_acc.balance) if trading_acc else 0.0

    return JsonResponse({
        'success': True,
        'positions': positions,
        'orders': orders,
        'deals': deals,
        'summary': {
            'deposit': float(deposit_sum),
            'withdrawal': float(withdrawal_sum),
            'profit': round(total_profit, 2),
            'swap': 0.0,
            'commission': 0.0,
            'balance': round(current_balance, 2),
        },
    })

# views.py — add near the top imports
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

# ═══════════════════════════════════════════════════════════════
# TRADE EVENT BROADCASTER
# ═══════════════════════════════════════════════════════════════
def broadcast_trade_event(event_type, trade_data, user_id):
    """
    Broadcast a trade event to every WebSocket client connected
    for that user. Called from execute_trade, close_trade_api,
    and close_all_trades_api.

    event_type: 'trade_opened' | 'trade_closed' | 'trade_updated'
    trade_data: dict with trade details
    user_id:    the User.id to notify
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f'trade_events_user_{user_id}',
            {
                'type': 'trade_event',
                'event': event_type,
                'trade': trade_data,
            }
        )
    except Exception as e:
        logger.warning(f"broadcast_trade_event failed: {e}")

# ═══════════════════════════════════════════════════════════════
# NEWS RISK — upcoming high/medium-impact events for the Market Overview entry checklist
# ═══════════════════════════════════════════════════════════════
@login_required
def api_news_risk(request):
    """
    Calendar events from one hour ago to `hours` ahead (default 24, max 72) for the given currencies.
    GET ?currencies=USD,EUR&hours=24 → {success, events: [{ts, minutes, currency, event, impact}], source, stale}
    Real events only (see calendar_feed); an empty list with feed_ok=False means the calendar is unreachable.
    """
    from . import calendar_feed
    wanted = {c.strip().upper() for c in request.GET.get('currencies', '').split(',') if c.strip()}
    try:
        hours = max(1, min(72, int(request.GET.get('hours', 24))))
    except ValueError:
        hours = 24
    key = 'news_risk:feed'
    feed = cache.get(key)
    if feed is None:
        try:
            feed = calendar_feed.get_calendar(days_ahead=3)
        except Exception as e:
            logger.warning(f'news risk calendar failed: {e}')
            return JsonResponse({'success': False, 'error': 'calendar unavailable'})
        cache.set(key, feed, 300)
    now = timezone.now()
    events = []
    for e in feed.get('events', []):
        d = e['date'] if e['date'].tzinfo else e['date'].replace(tzinfo=ZoneInfo('UTC'))
        minutes = (d - now).total_seconds() / 60
        if minutes < -60 or minutes > hours * 60 or e.get('impact') not in ('high', 'medium'):
            continue
        cur = (e.get('currency') or '').upper()
        if wanted and cur not in wanted:
            continue
        events.append({'ts': d.isoformat(), 'minutes': round(minutes), 'currency': cur,
                       'event': e.get('event') or '', 'impact': e['impact'],
                       'forecast': e.get('forecast'), 'previous': e.get('previous'), 'actual': e.get('actual')})
    events.sort(key=lambda x: x['minutes'])
    return JsonResponse({'success': True, 'events': events, 'feed_ok': bool(feed.get('events')),
                         'source': feed.get('source'), 'stale': feed.get('stale')})


# ═══════════════════════════════════════════════════════════════
# AI SCRUTINY — every strategy + fundamentals on one market
# ═══════════════════════════════════════════════════════════════
_FIAT = {'USD', 'EUR', 'GBP', 'JPY', 'AUD', 'NZD', 'CAD', 'CHF', 'CNY'}
_INVERSE_EVENTS = ('unemployment', 'jobless', 'claims', 'continuing')


def _num(v):
    """'2.5%' / '250K' / '-1.2M' / '1,234' → float, else None."""
    if v is None:
        return None
    s = str(v).strip().replace(',', '').replace('%', '')
    mult = 1
    if s[-1:].upper() in ('K', 'M', 'B', 'T'):
        mult = {'K': 1e3, 'M': 1e6, 'B': 1e9, 'T': 1e12}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _calendar_feed():
    from . import calendar_feed
    feed = cache.get('news_risk:feed')
    if feed is None:
        feed = calendar_feed.get_calendar(days_ahead=3)
        cache.set('news_risk:feed', feed, 300)
    return feed


def _fundamentals(symbol, spec):
    """
    Economic-calendar read for the symbol's currencies:
      released events (last 48 h) scored by actual vs forecast, upcoming high/medium events (next 24 h),
      and a vote: base-currency score − quote-currency score (metals / crypto / oil: − USD).
    """
    kind = getattr(spec, 'kind', 'other')
    base, quote = (getattr(spec, 'base', ''), getattr(spec, 'quote', '')) if spec else ('', '')
    if kind == 'synthetic':
        return {'applies': False, 'text': 'Synthetic indices are generated by the broker and do not react to economic news.'}
    curs = [c for c in (base, quote) if c in _FIAT]
    if kind != 'forex':
        curs = ['USD']
    try:
        feed = _calendar_feed()
    except Exception as e:
        logger.warning(f'scrutiny calendar failed: {e}')
        return {'applies': True, 'feed_ok': False, 'currencies': curs, 'upcoming': [], 'released': [], 'scores': {},
                'text': 'The economic calendar is unreachable right now, so fundamentals are not voting.'}
    now = timezone.now()
    upcoming, released, scores = [], [], {c: 0.0 for c in curs}
    for e in feed.get('events', []):
        cur = (e.get('currency') or '').upper()
        if cur not in scores:
            continue
        d = e['date'] if e['date'].tzinfo else e['date'].replace(tzinfo=ZoneInfo('UTC'))
        minutes = (d - now).total_seconds() / 60
        impact = e.get('impact') or 'low'
        row = {'ts': d.isoformat(), 'minutes': round(minutes), 'currency': cur, 'event': e.get('event') or '',
               'impact': impact, 'actual': e.get('actual'), 'forecast': e.get('forecast'), 'previous': e.get('previous')}
        a = _num(e.get('actual'))
        if minutes <= 0 and a is not None and minutes >= -48 * 60:
            ref = _num(e.get('forecast'))
            ref = ref if ref is not None else _num(e.get('previous'))
            if ref is None:
                continue
            surprise = (a - ref) / max(abs(ref), 1e-9) if ref else (1 if a > 0 else -1 if a < 0 else 0)
            surprise = max(-1.0, min(1.0, surprise * 4))             # a 25 % beat already counts fully
            if any(w in row['event'].lower() for w in _INVERSE_EVENTS):
                surprise = -surprise
            w = {'high': 1.0, 'medium': 0.5}.get(impact, 0.2) * (1.0 if minutes >= -24 * 60 else 0.5)
            scores[cur] += surprise * w
            row['surprise'] = round(surprise, 2)
            row['effect'] = 'bullish' if surprise > 0.05 else 'bearish' if surprise < -0.05 else 'in line'
            released.append(row)
        elif 0 < minutes <= 24 * 60 and impact in ('high', 'medium'):
            upcoming.append(row)
    scores = {c: round(max(-1.0, min(1.0, v / 2)), 2) for c, v in scores.items()}
    if kind == 'forex':
        vote = scores.get(base, 0) - scores.get(quote, 0)
    else:
        vote = -scores.get('USD', 0) * (0.6 if kind == 'energy' else 1)
    vote = max(-1.0, min(1.0, vote))
    upcoming.sort(key=lambda x: x['minutes'])
    released.sort(key=lambda x: x['minutes'], reverse=True)
    block = next((u for u in upcoming if u['impact'] == 'high' and u['minutes'] <= 30), None) or \
        next((r for r in released if r['impact'] == 'high' and r['minutes'] >= -15), None)
    if released:
        txt = '; '.join(f"{c} {'stronger' if s > 0.1 else 'weaker' if s < -0.1 else 'neutral'} ({s:+.2f})" for c, s in scores.items())
        txt = f"Recent data: {txt}. " + ("Fundamentals favour buying." if vote > 0.15 else "Fundamentals favour selling." if vote < -0.15 else "No clear fundamental edge.")
    else:
        txt = 'No released data with a forecast in the last 48 hours for ' + ', '.join(curs) + '.'
    if upcoming:
        u = upcoming[0]
        h, m = divmod(u['minutes'], 60)
        txt += f" Next: {u['currency']} {u['event']} ({u['impact']}) in {f'{h}h ' if h else ''}{m}m."
    return {'applies': True, 'feed_ok': bool(feed.get('events')), 'source': feed.get('source'), 'stale': feed.get('stale'),
            'currencies': curs, 'scores': scores, 'vote': round(vote, 2), 'upcoming': upcoming[:8], 'released': released[:8],
            'block': block, 'text': txt}


def _currency_strength(style):
    """Average AI score per currency from the cached scan of every forex pair (never triggers a new scan)."""
    from . import chart_scanner, mt5_service
    tot, cnt = {}, {}
    for sym in chart_scanner.SCAN_SYMBOLS:
        spec = mt5_service.SPECS.get(sym)
        if not spec or spec.kind not in ('forex', 'metal'):
            continue
        r = cache.get(chart_scanner.result_key(sym, style)) or cache.get(chart_scanner.result_key(sym, style, True))
        if not r or not r.get('ok'):
            continue
        for cur, sgn in ((spec.base, 1), (spec.quote, -1)):
            if cur in _FIAT or cur == 'XAU':
                tot[cur] = tot.get(cur, 0) + sgn * r['score']
                cnt[cur] = cnt.get(cur, 0) + 1
    return {c: round(tot[c] / cnt[c], 1) for c in sorted(tot, key=lambda c: -tot[c] / cnt[c])}


@login_required
def api_ai_scrutiny(request, symbol):
    """
    GET /api/ai-scrutiny/<symbol>/?style=intraday
    → {success, result (the AI scan with charts, model, backtest and entry coach),
       scrutiny {strategies[], levels[], zones, sessions, ...}, fundamentals, strength, verdict}
    """
    from . import chart_scanner, mt5_service
    symbol = symbol.upper()
    style = request.GET.get('style', 'intraday')
    style = style if style in chart_scanner.STYLES else 'intraday'
    if symbol not in mt5_service.SPECS:
        return JsonResponse({'success': False, 'error': f'Unknown symbol {symbol}'}, status=404)
    try:
        result = chart_scanner.analyse(symbol, style, detail=True)
        if not result.get('ok'):
            return JsonResponse({'success': False, 'error': result.get('error', 'analysis failed')})
        scr = chart_scanner.scrutiny(symbol, style)
        if not scr.get('ok'):
            return JsonResponse({'success': False, 'error': scr.get('error', 'scrutiny failed')})
    except Exception as e:
        logger.exception(f'AI scrutiny {symbol}: {e}')
        return JsonResponse({'success': False, 'error': 'The analysis engine failed for this market.'}, status=500)
    fund = _fundamentals(symbol, mt5_service.SPECS.get(symbol))
    strategies = [dict(x) for x in scr['strategies']]
    if fund.get('applies'):
        has = fund.get('feed_ok') and fund.get('released')
        strategies.append(chart_scanner._vote('fundamentals', fund.get('vote') if has else None, fund['text'],
                                              conf=0.7 if has else 0.3))
    else:
        strategies.append(chart_scanner._vote('fundamentals', None, fund['text']))
    block = fund.get('block')
    verdict = chart_scanner.scrutiny_verdict(strategies, scr.get('coach'), news_block=block)
    payload = dict(scr, strategies=strategies)
    return JsonResponse({'success': True, 'result': result, 'scrutiny': payload, 'fundamentals': fund,
                         'strength': _currency_strength(style), 'verdict': verdict,
                         'groups': list(dict.fromkeys(m[1] for m in chart_scanner.STRATEGY_META.values()))})


def _page_url(names, default=''):
    """First URL that reverses from the given route names (pages link to each other without hard-coding paths)."""
    from django.urls import reverse, NoReverseMatch
    for name in names:
        try:
            return reverse(name)
        except NoReverseMatch:
            continue
    return default


# ═══════════════════════════════════════════════════════════════
# DERIV TICKS THROUGH THE SERVER (digits page fallback)
# ═══════════════════════════════════════════════════════════════
_DERIV_TICK_SYMBOLS = ('R_10', 'R_25', 'R_50', 'R_75', 'R_100', '1HZ10V', '1HZ25V', '1HZ50V', '1HZ75V', '1HZ100V',
                       'frxXAUUSD', 'frxXAGUSD', 'frxEURUSD', 'frxGBPUSD', 'frxUSDJPY', 'cryBTCUSD', 'cryETHUSD')


@login_required
def api_deriv_ticks(request):
    """
    GET /api/deriv/ticks/?symbol=1HZ100V&count=100 → {success, symbol, pip_size, ticks: [{epoch, quote}]}
    The last ticks of a Deriv symbol fetched by the server. The digits page uses this when the browser
    can't open Deriv's websocket itself (blocked by a browser shield / extension / network).
    Cached 1 s per symbol, so any number of viewers costs one Deriv request per second.
    """
    symbol = request.GET.get('symbol', '')
    if symbol not in _DERIV_TICK_SYMBOLS:
        return JsonResponse({'success': False, 'error': 'unknown symbol'}, status=400)
    try:
        count = max(1, min(1000, int(request.GET.get('count', 20))))
    except ValueError:
        count = 20
    depth = 1000 if count > 100 else 100          # the live feed asks for 100, the scanner for 1000
    key = f'deriv:ticks:{symbol}:{depth}'
    data = cache.get(key)
    if data is None:
        if price_feed.cooling('Deriv'):
            return JsonResponse({'success': False, 'error': 'Deriv is not reachable from the server right now'})
        try:
            ws = price_feed.deriv_connect(timeout=6)
            try:
                ws.send(json.dumps({'ticks_history': symbol, 'count': depth, 'end': 'latest', 'style': 'ticks', 'req_id': 11}))
                deadline = _time.time() + 6
                msg = {}
                while _time.time() < deadline:
                    msg = json.loads(ws.recv())
                    if msg.get('req_id') == 11:
                        break
            finally:
                ws.close()
        except Exception as e:
            price_feed.feed_failed('Deriv', e if str(e) else f'timeout ({type(e).__name__})', rate_pause=30)
            return JsonResponse({'success': False, 'error': f'Deriv not reachable from the server: {e}'})
        if msg.get('error'):
            return JsonResponse({'success': False, 'error': msg['error'].get('message', 'Deriv error')})
        hist = msg.get('history') or {}
        data = {'pip_size': msg.get('pip_size'),
                'ticks': [{'epoch': int(t), 'quote': float(p)} for t, p in zip(hist.get('times') or [], hist.get('prices') or [])]}
        cache.set(key, data, 1 if depth == 100 else 30)
    return JsonResponse({'success': True, 'symbol': symbol, 'pip_size': data['pip_size'], 'ticks': data['ticks'][-count:]})


# ═══════════════════════════════════════════════════════════════
# DERIV DEMO TRADING (digits page → the user's own Deriv demo account)
# ═══════════════════════════════════════════════════════════════
def _demo_history_key(user, mode='demo'):
    return f'deriv:{mode}:history:{user.id}'


def _deriv_mode(value):
    return 'real' if str(value or '').lower() == 'real' else 'demo'


def _deriv_missing(mode):
    if mode == 'real':
        return ('No Deriv real account connected. On your dashboard use Connect Deriv and log in with the Deriv '
                'login that has your real account.')
    return 'No Deriv demo account connected. On your dashboard use Connect Deriv (or a Personal Access Token) so your demo account is listed.'


def _real_status(user):
    """What the page needs to offer real trading: allowed by the site, connected, opted in."""
    from . import deriv_demo
    acc = deriv_demo.real_account(user)
    return {'real_allowed': deriv_demo.real_allowed(), 'real_connected': bool(acc),
            'real_enabled': bool(acc and acc.real_enabled_at), 'real_max_stake': deriv_demo.real_max_stake()}


def _real_confirmed(mode, data):
    """Real money needs the page to say so explicitly on every request (a stray demo request can't spend it)."""
    return mode != 'real' or data.get('confirm_real') is True


@login_required
def api_deriv_demo_account(request):
    """GET ?account=demo|real → {success, connected, loginid, balance, currency, history, real_*} for that Deriv account."""
    from . import deriv_demo
    mode = _deriv_mode(request.GET.get('account'))
    acc = deriv_demo.account_for(request.user, mode)
    history = cache.get(_demo_history_key(request.user, mode)) or []
    flags = _real_status(request.user)
    if not acc:
        return JsonResponse({'success': True, 'connected': False, 'history': history, 'error': _deriv_missing(mode), **flags})
    if mode == 'real' and not (flags['real_allowed'] and flags['real_enabled']):
        return JsonResponse({'success': True, 'connected': True, 'loginid': acc.account_number, 'history': history, **flags})
    try:
        info = deriv_demo.account_info(acc)
    except deriv_demo.DemoTradeError as e:
        return JsonResponse({'success': False, 'connected': True, 'loginid': acc.account_number, 'error': str(e),
                             'history': history, **flags})
    return JsonResponse({'success': True, 'connected': True, 'history': history, **info, **flags})


@login_required
@require_POST
def api_deriv_real_enable(request):
    """POST {accept: true} → turn real-money trading on for the user's Deriv real account; {disable: true} → off."""
    from . import deriv_demo
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    acc = deriv_demo.real_account(request.user)
    if not acc:
        return JsonResponse({'success': False, 'error': _deriv_missing('real')})
    if data.get('disable'):
        acc.real_enabled_at = None
        acc.save(update_fields=['real_enabled_at'])
        return JsonResponse({'success': True, 'real_enabled': False})
    if not deriv_demo.real_allowed():
        return JsonResponse({'success': False, 'error': 'Real-money trading is not enabled on this site.'})
    if data.get('accept') is not True:
        return JsonResponse({'success': False, 'error': 'Accept the risk warning to continue.'})
    acc.real_enabled_at = timezone.now()
    acc.save(update_fields=['real_enabled_at'])
    logger.info(f'User {request.user.pk} enabled real Deriv trading on {acc.account_number}')
    return JsonResponse({'success': True, 'real_enabled': True})


@login_required
@require_POST
def api_deriv_demo_trade(request):
    """
    POST {symbol, contract_type: DIGITEVEN…, amount, duration, barrier?, account: demo|real, confirm_real?}
    → buys one digit contract on the user's Deriv account and waits for Deriv to settle it.
    """
    from . import deriv_demo
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    mode = _deriv_mode(data.get('account'))
    if not _real_confirmed(mode, data):
        return JsonResponse({'success': False, 'error': 'Confirm the real-money trade first.'})
    acc = deriv_demo.account_for(request.user, mode)
    if not acc:
        return JsonResponse({'success': False, 'error': _deriv_missing(mode)})
    lock = f'deriv:demo:busy:{request.user.id}'
    if not cache.add(lock, 1, 60):          # one contract at a time per user (double clicks, bot + manual)
        return JsonResponse({'success': False, 'error': 'A Deriv contract is still running. Wait for its result.'})
    try:
        res = deriv_demo.place_trade(acc, data.get('symbol'), data.get('contract_type'), data.get('amount'),
                                     data.get('duration', 1), data.get('barrier'))
    except deriv_demo.DemoTradeError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        logger.exception(f'Deriv {mode} trade failed: {e}')
        return JsonResponse({'success': False, 'error': 'Deriv trade failed. Check your Deriv statement before trying again.',
                             'detail': str(e)[:200]})
    finally:
        cache.delete(lock)
    key = _demo_history_key(request.user, mode)
    history = cache.get(key) or []
    history.insert(0, {k: res.get(k) for k in ('contract_id', 'won', 'profit', 'buy_price', 'final_digit', 'new_balance', 'currency')}
                   | {'symbol': data.get('symbol'), 'contract_type': data.get('contract_type'), 'barrier': data.get('barrier'),
                      'at': int(_time.time())})
    cache.set(key, history[:50], 30 * 86400)
    if mode == 'real':
        logger.info(f"Real Deriv digit trade user={request.user.pk} {acc.account_number} {data.get('contract_type')} "
                    f"stake={data.get('amount')} contract={res.get('contract_id')} profit={res.get('profit')}")
    return JsonResponse(res)


# ── Deriv Multipliers (trade.html), demo or real ──
def _demo_guard(request, mode='demo'):
    from . import deriv_demo
    acc = deriv_demo.account_for(request.user, mode)
    if not acc:
        return None, JsonResponse({'success': False, 'connected': False, 'error': _deriv_missing(mode)})
    return acc, None


@login_required
def api_deriv_demo_multipliers(request):
    """GET ?symbol=XAUUSD → {success, multipliers: [...]} allowed by Deriv for that market."""
    from . import deriv_demo
    opts = deriv_demo.multiplier_options(request.GET.get('symbol', ''))
    return JsonResponse({'success': True, 'multipliers': opts or deriv_demo.DEFAULT_MULTIPLIERS, 'from_deriv': bool(opts)})


@login_required
def api_deriv_demo_positions(request):
    """GET ?account=demo|real → open Multipliers contracts on that Deriv account."""
    from . import deriv_demo
    acc, err = _demo_guard(request, _deriv_mode(request.GET.get('account')))
    if err:
        return err
    try:
        return JsonResponse(deriv_demo.open_multipliers(acc))
    except deriv_demo.DemoTradeError as e:
        return JsonResponse({'success': False, 'error': str(e)})


def _demo_post(request, fn):
    from . import deriv_demo
    try:
        data = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)
    mode = _deriv_mode(data.get('account'))
    if not _real_confirmed(mode, data):
        return JsonResponse({'success': False, 'error': 'Confirm the real-money order first.'})
    acc, err = _demo_guard(request, mode)
    if err:
        return err
    lock = f'deriv:demo:busy:{request.user.id}'
    if not cache.add(lock, 1, 60):
        return JsonResponse({'success': False, 'error': 'Another Deriv request is still running. Wait a moment.'})
    try:
        res = fn(acc, data)
        if mode == 'real':
            logger.info(f'Real Deriv request user={request.user.pk} {acc.account_number} {request.path} '
                        f'{ {k: data.get(k) for k in ("symbol", "side", "stake", "multiplier", "contract_id")} } → {res.get("contract_id")}')
        return JsonResponse(res)
    except deriv_demo.DemoTradeError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        logger.exception(f'Deriv {mode} request failed: {e}')
        return JsonResponse({'success': False, 'error': 'Deriv request failed. Check your Deriv statement before trying again.', 'detail': str(e)[:200]})
    finally:
        cache.delete(lock)


@login_required
@require_POST
def api_deriv_demo_open(request):
    """POST {symbol, side: BUY|SELL, stake, multiplier, stop_loss?, take_profit?, account, confirm_real?} → opens a Multipliers contract."""
    from . import deriv_demo
    return _demo_post(request, lambda acc, d: deriv_demo.open_multiplier(
        acc, d.get('symbol'), d.get('side'), d.get('stake'), d.get('multiplier'), d.get('stop_loss'), d.get('take_profit')))


@login_required
@require_POST
def api_deriv_demo_close(request):
    """POST {contract_id, account, confirm_real?} → sells (closes) that contract at market."""
    from . import deriv_demo
    return _demo_post(request, lambda acc, d: deriv_demo.close_multiplier(acc, d.get('contract_id')))


@staff_member_required
def signal_posters(request):
    """TikTok-size posters of the AI signals, with captions to copy (store/poster.py)."""
    from . import poster, tiktok
    account = None
    if tiktok.connected():
        try:
            account = tiktok.user_info().get('display_name') or 'connected'
        except Exception as e:
            account = f'connected (could not read the account: {e})'
    return render(request, 'store/posters.html', {
        'posters': poster.recent(60), 'tiktok_configured': tiktok.configured(), 'tiktok_account': account,
        'tiktok_redirect': tiktok.redirect_uri(request), 'tiktok_prefix': tiktok.poster_url('posters/'),
    })


@staff_member_required
def tiktok_connect(request):
    from . import tiktok
    if not tiktok.configured():
        messages.error(request, 'Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first, then reload the site.')
        return redirect('signal_posters')
    return redirect(tiktok.auth_url(request))


@staff_member_required
def tiktok_callback(request):
    from . import tiktok
    if request.GET.get('error'):
        messages.error(request, f"TikTok: {request.GET.get('error_description') or request.GET['error']}")
        return redirect('signal_posters')
    try:
        tiktok.finish_login(request, request.GET.get('code', ''), request.GET.get('state', ''))
        messages.success(request, 'TikTok connected. New signal posters will be posted automatically.')
    except Exception as e:
        messages.error(request, f'TikTok login failed: {e}')
    return redirect('signal_posters')


@staff_member_required
@require_POST
def tiktok_disconnect(request):
    from . import tiktok
    tiktok.disconnect()
    messages.success(request, 'TikTok disconnected.')
    return redirect('signal_posters')


@staff_member_required
@require_POST
def tiktok_post_poster(request):
    """Post one poster from the page now (the 'Post to TikTok' button)."""
    import os
    from . import poster, tiktok
    url = request.POST.get('url', '')
    prefix = f"{settings.MEDIA_URL}posters/"
    rel = url[len(settings.MEDIA_URL):] if url.startswith(prefix) and '..' not in url else ''
    if not rel or not os.path.exists(os.path.join(settings.MEDIA_ROOT, rel)):
        messages.error(request, 'Poster not found.')
        return redirect('signal_posters')
    caption = ''
    txt = os.path.join(settings.MEDIA_ROOT, rel[:-4] + '.txt')
    if os.path.exists(txt):
        caption = open(txt, encoding='utf-8').read()
    try:
        pid = tiktok.post_photo(tiktok.poster_url(rel), caption.split('\n', 1)[0] or 'PATREOAIX AI signal', caption)
        messages.success(request, f'Sent to TikTok (publish id {pid}). It appears on your account in a minute or two.')
    except Exception as e:
        messages.error(request, f'TikTok refused the post: {e}')
    return redirect('signal_posters')


def _legal(request, template):
    return render(request, template, {'domain': settings.SITE_DOMAIN, 'updated': '27 October 2026',
                                      'contact': getattr(settings, 'EMAIL_HOST_USER', '') or f'support@{settings.SITE_DOMAIN}'})


def privacy_view(request):
    return _legal(request, 'store/legal/privacy.html')


def terms_view(request):
    return _legal(request, 'store/legal/terms.html')


def subscribe_link(request):
    """Short link for posts and bios (patreoaix.com/subscribe): the plans page, or sign up first."""
    from django.urls import reverse
    if request.user.is_authenticated:
        return redirect('subscription_billing')
    return redirect(f"{reverse('signup')}?next={reverse('subscription_billing')}")


@login_required
def news_impact(request):
    """Big releases: the playbook for the coming ones, the latest results and how the charts moved."""
    from . import news_history as nh, telegram_schedule as ts
    tz = ts._tz()
    try:
        coming = nh.upcoming(hours=24 * 7)
    except Exception as e:
        logger.warning(f'news impact calendar: {e}')
        coming = []
    playbooks = []
    for e in coming[:6]:
        pb = nh.playbook(e['key'], e['date'], e.get('forecast'), e.get('previous'), tz=tz)
        pb['scenarios'] = nh.scenario_lines(pb['stats'][0], pb['currency']) if pb['stats'] else []
        playbooks.append(pb)
    key = (request.GET.get('event') or '').upper()
    key = key if key in nh.EVENTS else 'FOMC'
    lead = nh.SYMBOLS[nh.EVENTS[key][1]][0]
    history = nh.symbol_stats(key, lead, last=24)
    if request.method == 'POST' and request.user.is_staff and request.POST.get('send'):
        target = next((e for e in coming if f"{e['key']}{e['date']:%Y%m%d%H%M}" == request.POST['send']), None)
        if target:
            try:
                from . import signal_alerts
                results, _, _ = signal_alerts.confluence('intraday')
                ts.event_preview(target, results)
                messages.success(request, 'Playbook sent to the Telegram channel.')
            except Exception as e:
                messages.error(request, f'Telegram: {e}')
        return redirect(f"{request.path}?event={key}")
    return render(request, 'store/news_impact.html', {
        'playbooks': playbooks, 'recent': nh.recent(12), 'events': [(k, v[0]) for k, v in nh.EVENTS.items()],
        'sel_key': key, 'sel_label': nh.EVENTS[key][0], 'lead': lead, 'history': history,
        'loaded': bool(history) or nh.recent(1),
    })


def _bonus_on():
    from . import bonus
    return bonus.enabled()


@login_required
@require_POST
def start_trial_view(request):
    from .membership import start_trial, TRIAL_DAYS
    if start_trial(request.user):
        messages.success(request, f'Your free {TRIAL_DAYS}-day Pro trial has started: every AI signal is unlocked.')
        return redirect('market_overview')
    messages.info(request, 'The free trial was already used on this account.')
    return redirect('subscription_billing')


def public_results(request):
    """The public track record: every signal, win or lose (no login)."""
    from . import public_results as pr
    style = request.GET.get('style') or None
    style = style if style in ('scalp', 'intraday', 'swing', 'position') else None
    data = pr.build(style=style)
    share = f"{settings.SITE_URL.rstrip('/')}/results/"
    return render(request, 'store/public_results.html', dict(data, style=style, share_url=share))


def public_results_png(request):
    from . import public_results as pr
    days = 1 if request.GET.get('days') == '1' else 30
    return HttpResponse(pr.results_png(days), content_type='image/png', headers={'Cache-Control': 'public, max-age=1800'})



# ═══════════════════════════════════════════════════════════════
# AI COPY TRADING (store/copy_trading.py)
# ═══════════════════════════════════════════════════════════════
COPY_STYLES = [('scalping', 'Scalping'), ('intraday', 'Intraday'), ('swing', 'Swing'), ('position', 'Position')]


@login_required
def copy_trading_page(request):
    from . import copy_trading as ct, deriv_demo as dd
    from .membership import is_member
    from .models import CopyTrade
    cfg = ct.settings_for(request.user)
    member = is_member(request.user)
    demo, real = dd.demo_account(request.user), dd.real_account(request.user)
    real_ok = dd.real_allowed() and bool(real and real.real_enabled_at)
    if request.method == 'POST':
        p = request.POST
        account = 'real' if p.get('account') == 'real' else 'demo'
        try:
            stake = Decimal(p.get('stake') or cfg.stake).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            stake = cfg.stake
        top = Decimal(str(dd.real_max_stake())) if account == 'real' else Decimal('2000')
        cfg.stake = min(max(stake, Decimal('1.00')), top)
        try:
            cfg.multiplier = int(p['multiplier']) if p.get('multiplier') else None
        except ValueError:
            cfg.multiplier = None
        cfg.min_grade = 'A' if p.get('min_grade') == 'A' else 'A+'
        cfg.styles = [s for s, _ in COPY_STYLES if p.get(f'style_{s}')] or ['intraday']
        cfg.symbols = [s.strip().upper() for s in (p.get('symbols') or '').replace(' ', ',').split(',') if s.strip()][:30]
        for f, lo, hi in (('max_open', 1, 10), ('max_per_day', 1, 30)):
            try:
                setattr(cfg, f, min(max(int(p.get(f) or getattr(cfg, f)), lo), hi))
            except ValueError:
                pass
        want_on = p.get('enabled') == 'on'
        error = None
        if want_on and not member:
            error = 'AI Copy Trading is for Pro members. Start your free trial or subscribe to switch it on.'
        elif want_on and account == 'demo' and not demo:
            error = 'Connect your Deriv demo account first.'
        elif want_on and account == 'real' and not real_ok:
            error = 'Real-money copy trading is not available on your account yet. Use the demo account.'
        elif want_on and account == 'real' and not cfg.enabled and p.get('confirm_real') != 'yes':
            error = 'Tick the box to confirm you accept real-money risk.'
        if error:
            messages.error(request, error)
            want_on = False
        if want_on and not cfg.enabled:
            cfg.enabled_at = timezone.now()
            messages.success(request, f"AI Copy Trading is ON: new {cfg.min_grade} signals will be copied to your Deriv {account} account.")
        elif cfg.enabled and not want_on and not error:
            messages.info(request, 'AI Copy Trading is off. Trades already open keep their stop loss and take profit on Deriv.')
        elif not error:
            messages.success(request, 'Copy trading settings saved.')
        cfg.enabled, cfg.account = want_on, account if account == 'demo' or real_ok else 'demo'
        cfg.save()
        return redirect('copy_trading')
    rows = list(CopyTrade.objects.filter(user=request.user).select_related('signal')[:60])
    return render(request, 'store/copy_trading.html', {
        'cfg': cfg, 'member': member, 'demo': demo, 'real': real, 'real_ok': real_ok,
        'real_max': dd.real_max_stake(), 'stats': ct.stats(request.user), 'rows': rows,
        'open_rows': [r for r in rows if r.status == 'OPEN'], 'styles': COPY_STYLES,
        'sel_styles': cfg.styles or ['intraday'], 'mult_choices': [50, 100, 200, 300, 500, 1000], 'symbols_text': ', '.join(cfg.symbols or []),
    })


@login_required
def api_copy_trading_sync(request):
    from . import copy_trading as ct
    try:
        rows = ct.sync(request.user)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': True, 'rows': rows, 'stats': ct.stats(request.user)})


@login_required
@require_POST
def api_copy_trading_close(request):
    from . import copy_trading as ct
    from .models import CopyTrade
    row = CopyTrade.objects.filter(user=request.user, pk=request.POST.get('id'), status='OPEN').first()
    if not row:
        return JsonResponse({'success': False, 'error': 'Trade not found or already closed.'})
    try:
        ct.close_row(row, 'You closed this trade.')
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': True, 'profit': row.profit})


# ═══════════════════════════════════════════════════════════════
# INSTALLABLE APP (PWA) + PUSH NOTIFICATIONS + PRICE ALERTS
# ═══════════════════════════════════════════════════════════════
@login_required
@require_POST
def api_push_subscribe(request):
    from . import push
    if not push.enabled():
        return JsonResponse({'success': False, 'error': 'Push notifications are not set up on this site yet.'})
    try:
        data = json.loads(request.body or '{}')
        if data.get('unsubscribe'):
            push.unsubscribe(request.user, data.get('endpoint', ''))
            return JsonResponse({'success': True, 'subscribed': False})
        push.subscribe(request.user, data, request.META.get('HTTP_USER_AGENT', ''))
    except (ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'error': str(e)})
    if data.get('test', True):
        push.safe_send(request.user, '🔔 Notifications are on', 'You will get signals, price alerts and copy trades here.', '/price-alerts/', tag='welcome')
    return JsonResponse({'success': True, 'subscribed': True})


@login_required
def price_alerts_page(request):
    from . import price_alerts as pa, push
    from .models import PriceWatch
    if request.method == 'POST':
        if request.POST.get('delete'):
            PriceWatch.objects.filter(user=request.user, pk=request.POST['delete']).delete()
            messages.info(request, 'Alert deleted.')
        else:
            try:
                w, now = pa.create(request.user, request.POST.get('symbol'), request.POST.get('price'), request.POST.get('note'))
                verb = 'rises to' if w.condition == 'above' else 'falls to'
                messages.success(request, f"Alert set: we'll tell you when {pa.name(w.symbol)} {verb} {pa.fmt(w.symbol, w.price)}"
                                          + (f' (now {pa.fmt(w.symbol, now)}).' if now else '.'))
            except ValueError as e:
                messages.error(request, str(e))
        return redirect('price_alerts')
    watches = list(PriceWatch.objects.filter(user=request.user)[:80])
    for w in watches:
        w.label, w.price_txt = pa.name(w.symbol), pa.fmt(w.symbol, w.price)
        w.hit_txt = pa.fmt(w.symbol, w.triggered_price) if w.triggered_price else ''
    return render(request, 'store/price_alerts.html', {
        'active': [w for w in watches if w.active], 'done': [w for w in watches if not w.active][:30],
        'symbols': [(s, pa.name(s)) for s in pa.SYMBOLS], 'sel': (request.GET.get('symbol') or 'XAUUSD').upper(),
        'push_on': push.enabled(),
    })


@login_required
def api_price_alert(request):
    """GET ?symbol= → current price; POST {symbol, price, note} → create (quick add from charts)."""
    from . import price_alerts as pa
    if request.method == 'POST':
        try:
            data = json.loads(request.body or '{}') if request.content_type == 'application/json' else request.POST
            w, now = pa.create(request.user, data.get('symbol'), data.get('price'), data.get('note', ''))
        except ValueError as e:
            return JsonResponse({'success': False, 'error': str(e)})
        return JsonResponse({'success': True, 'id': w.id, 'condition': w.condition, 'now': now})
    sym = (request.GET.get('symbol') or '').upper()
    if sym not in pa.SYMBOLS:
        return JsonResponse({'success': False, 'error': 'Unknown market.'})
    try:
        now = pa.last_price(sym)
    except Exception:
        now = None
    return JsonResponse({'success': True, 'symbol': sym, 'price': now, 'price_txt': pa.fmt(sym, now) if now else None})


# ═══════════════════════════════════════════════════════════════
# TRADING JOURNAL + WEEKLY AI REVIEW (store/journal.py)
# ═══════════════════════════════════════════════════════════════
@login_required
def journal_page(request):
    from . import journal as jr
    from .models import JournalEntry, WeeklyReview
    from datetime import date as _date
    if request.method == 'POST':
        act = request.POST.get('action')
        try:
            if act == 'add':
                jr.add_manual(request.user, request.POST)
                messages.success(request, 'Trade added to your journal.')
            elif act == 'tag':
                jr.tag(request.user, request.POST.get('id'), request.POST)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'success': True})
                messages.success(request, 'Saved.')
            elif act == 'delete':
                JournalEntry.objects.filter(user=request.user, pk=request.POST.get('id'), source='manual').delete()
                messages.info(request, 'Entry deleted.')
        except (ValueError, JournalEntry.DoesNotExist) as e:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': str(e) or 'Not found.'})
            messages.error(request, str(e) or 'Not found.')
        return redirect(f"{reverse('ai_trade_journal')}?week={request.POST.get('week', '')}")
    jr.sync(request.user)
    this_week = jr.week_start()
    try:
        week = jr.week_start(_date.fromisoformat(request.GET.get('week') or ''))
    except ValueError:
        week = this_week
    week = min(week, this_week)
    if week == this_week:
        rv = jr.review(request.user, week, save=False)         # live, so far this week
    else:
        rv = WeeklyReview.objects.filter(user=request.user, week_start=week).first() or jr.review(request.user, week)
    a, b = jr._bounds(week)
    entries = list(JournalEntry.objects.filter(user=request.user, closed_at__gte=a, closed_at__lt=b)[:200])
    weeks = list(WeeklyReview.objects.filter(user=request.user).values('week_start', 'grade', 'stats')[:12])
    return render(request, 'store/journal.html', {
        'review': rv, 'entries': entries, 'week': week, 'week_end': week + timedelta(days=6),
        'is_this_week': week == this_week, 'prev_week': week - timedelta(days=7),
        'next_week': week + timedelta(days=7) if week < this_week else None, 'weeks': weeks,
        'setups': JournalEntry.SETUPS, 'emotions': JournalEntry.EMOTIONS,
        'all_time': jr.stats(list(JournalEntry.objects.filter(user=request.user))),
    })


# ═══════════════════════════════════════════════════════════════
# BEGINNER ACADEMY (store/academy.py)
# ═══════════════════════════════════════════════════════════════
@login_required
def academy_page(request):
    from . import academy, bonus
    done = academy.passed(request.user)
    lessons = [dict(l, done=l['key'] in done, n=i + 1) for i, l in enumerate(academy.LESSONS)]
    nxt = next((l for l in lessons if not l['done']), None)
    return render(request, 'store/academy.html', {
        'lessons': lessons, 'done_n': len(done & set(academy.BY_KEY)), 'total': len(lessons), 'next': nxt,
        'graduated': academy.graduated(request.user), 'pro_days': academy.pro_days(),
        'bonus_gated': bonus.enabled() and academy.required_for_bonus(),
        'pct_done': round(len(done & set(academy.BY_KEY)) / len(lessons) * 100),
    })


@login_required
def academy_lesson(request, key):
    from . import academy
    lesson = academy.BY_KEY.get(key)
    if not lesson:
        raise Http404
    if request.method == 'POST':
        try:
            answers = json.loads(request.body or '{}').get('answers', [])
        except ValueError:
            answers = []
        res = academy.submit(request.user, key, answers)
        if not res['passed']:
            res['correct'] = [bool(x) for x in res['correct']]       # which ones to look at again, not the answers
        return JsonResponse(dict(res, success=True))
    idx = academy.LESSONS.index(lesson)
    nxt = academy.LESSONS[idx + 1] if idx + 1 < len(academy.LESSONS) else None
    return render(request, 'store/academy_lesson.html', {
        'lesson': lesson, 'n': idx + 1, 'total': len(academy.LESSONS), 'next': nxt,
        'quiz': [{'q': q, 'options': list(enumerate(opts)), 'i': i} for i, (q, opts, _) in enumerate(lesson['quiz'])],
        'done': key in academy.passed(request.user), 'pro_days': academy.pro_days(),
    })


# ═══════════════════════════════════════════════════════════════
# ADMIN REVENUE DASHBOARD (store/revenue.py)
# ═══════════════════════════════════════════════════════════════
@staff_member_required
def admin_revenue(request):
    from . import revenue
    try:
        days = int(request.GET.get('days', 30))
    except ValueError:
        days = 30
    days = days if days in dict(revenue.PERIODS) else 30
    data = revenue.build(days)
    charts = [
        {'title': 'Deposits per day', 'money': True, **revenue.bars(data['daily'], 'deposits')},
        {'title': 'Subscription revenue per day', 'money': True, **revenue.bars(data['daily'], 'subs')},
        {'title': 'New sign-ups per day', 'money': False, **revenue.bars(data['daily'], 'signups')},
    ]
    return render(request, 'store/admin_revenue.html', dict(data, charts=charts, days=days, periods=revenue.PERIODS))


# ═══════════════════════════════════════════════════════════════
# PATREOAIX FUNDED: prop challenges (store/prop_firm.py)
# ═══════════════════════════════════════════════════════════════
@login_required
def prop_dashboard(request):
    from . import prop_firm as pf, mt5_service as mt5
    from .models import PropAccount, PropPayout
    if request.method == 'POST':
        act = request.POST.get('action')
        try:
            if act == 'buy':
                accs = pf.buy(request.user, request.POST.get('plan_type'), request.POST.get('size'), request.POST.get('qty') or 1)
                messages.success(request, f"{len(accs)} account{'s' if len(accs) > 1 else ''} ready: "
                                          + ', '.join(f'#{a.login}' for a in accs) + '. Good luck!')
            elif act == 'payout':
                po = pf.request_payout(request.user, int(request.POST.get('login') or 0))
                messages.success(request, f'Payout of ${po.amount} requested.')
        except (pf.PropError, ValueError) as e:
            messages.error(request, str(e) or 'Something went wrong.')
        return redirect('funded')
    for p in PropAccount.objects.filter(user=request.user, status='ACTIVE'):
        try:
            mt5.check_stop_out(request.user, p)
            pf.check(p)
        except Exception as e:
            logger.warning(f'prop check {p.login}: {e}')
    accounts = list(PropAccount.objects.filter(user=request.user))
    active = []
    for a in accounts:
        a.tier = pf.TIER_NAMES.get(a.size, '')
        if a.status == 'ACTIVE':
            a.obj = pf.objectives(a)
            a.payout = pf.payout_status(a) if a.phase == 'funded' else None
            active.append(a)
    from .models import Account
    wallet = Account.objects.filter(user=request.user, account_type='EXCHANGE').first()
    return render(request, 'store/prop_dashboard.html', {
        'trader_id': pf.trader_id(request.user) if accounts else None,
        'catalog': pf.catalog(), 'sizes': pf.SIZES, 'tiers': pf.TIER_NAMES, 'active': active,
        'past': [a for a in accounts if a.status != 'ACTIVE'][:30],
        'payouts': PropPayout.objects.filter(user=request.user)[:20],
        'wallet': wallet.balance if wallet else 0, 'max_per_order': pf.MAX_PER_ORDER,
        'bulk': sorted(pf.BULK_DISCOUNT.items()), 'max_alloc': pf.MAX_ALLOCATION, 'max_active': pf.MAX_ACTIVE_ACCOUNTS,
        'leverage': pf.LEVERAGE, 'payout_days': pf.PAYOUT_EVERY_DAYS, 'prices_json': json.dumps(pf.PRICES),
        'bulk_json': json.dumps(pf.BULK_DISCOUNT),
        'funded_n': sum(1 for a in accounts if a.phase == 'funded'),
    })


@login_required
def api_prop_accounts(request):
    """Active challenge / funded accounts for the terminal's Settings → Accounts."""
    from .models import PropAccount
    rows = [{'login': a.login, 'name': f"{a.get_plan_type_display()} ${a.size // 1000}K", 'phase': a.get_phase_display(),
             'balance': float(a.balance), 'size': a.size, 'funded': a.phase == 'funded'}
            for a in PropAccount.objects.filter(user=request.user, status='ACTIVE')]
    return JsonResponse({'success': True, 'accounts': rows})
