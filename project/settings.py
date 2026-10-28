import os  #
from pathlib import Path  #
from dotenv import load_dotenv  #

# Build paths inside the project like this: BASE_DIR / 'subdir'.  #
BASE_DIR = Path(__file__).resolve().parent.parent  #
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'  #
# Load environment variables from .env file  #
load_dotenv(os.path.join(BASE_DIR, '.env'))  #

# =============================================
# DERIV API CONFIGURATION
# ==========================================
# Deriv OAuth Configuration
# ==========================================
import logging


import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Access your OANDA credentials
OANDA_API_TOKEN = os.environ.get('OANDA_API_TOKEN')
OANDA_ACCOUNT_ID = os.environ.get('OANDA_ACCOUNT_ID')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        # Silence the noisy static/dev-server access logs
        'django.server': {
            'handlers': ['console'],
            'level': 'WARNING',  # ← Only show WARNING and above (hides 200s)
            'propagate': False,
        },
    },
}


DERIV_APP_ID = os.environ.get('DERIV_APP_ID', '34mjct1dBCmYao65TsMoD')
# App ID for Deriv's public market-data websocket (ticks, candles, digit settlement). 1089 is Deriv's
# shared test app; put your own app's numeric ID here if Deriv stops serving 1089.
DERIV_WS_APP_ID = os.environ.get('DERIV_WS_APP_ID', '1089')
# Force a market-data socket URL (default: Deriv's new public endpoint, then the legacy one)
DERIV_PUBLIC_WS_URL = os.environ.get('DERIV_PUBLIC_WS_URL', '')
# Deriv login return address. Empty = this site's own /deriv-callback/ on the address the visitor uses
# (https://www.patreoaix.com/deriv-callback/ live, http://127.0.0.1:8000/deriv-callback/ on the laptop).
# Register the live one in your Deriv app.
DERIV_OAUTH_REDIRECT = os.environ.get('DERIV_OAUTH_REDIRECT', '')
DERIV_OAUTH_SCOPES = 'read trade'
# =============================================
# NOWCRYPTO API CONFIGURATION
# =============================================

TWELVEDATA_API_KEY = os.environ.get('TWELVEDATA_API_KEY', '')          # in .env (never in the code: the repo is public)


# ==========================================
# BINANCE API CONFIGURATION
# ==========================================
import os

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
BINANCE_TESTNET = os.getenv('BINANCE_TESTNET', 'False') == 'True'
import os
# ==========================================
# SMS / TWILIO CONFIGURATION
# ==========================================
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '')

# ⚠️ Set to True AFTER you upgrade your Twilio account
# While False, SMS will use Twilio's predefined trial templates
TWILIO_UPGRADED = os.environ.get('TWILIO_UPGRADED', 'False') == 'True'

# Master switch
SMS_ENABLED = os.environ.get('SMS_ENABLED', 'True') == 'True'

# Per-category flags
SMS_OTP_ENABLED = True
SMS_DEPOSIT_ENABLED = True
SMS_WITHDRAWAL_ENABLED = True
SMS_TRADE_ENABLED = True
SMS_SECURITY_ENABLED = True
SMS_REFERRAL_ENABLED = True

# OTP settings
SMS_OTP_EXPIRY_SECONDS = 300  # 5 minutes
SMS_OTP_LENGTH = 6

# Rate limiting (per user, per template, per hour)
SMS_RATE_LIMIT_PER_HOUR = 10
# settings.py
NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY', '')
NOWPAYMENTS_IPN_SECRET = os.getenv('NOWPAYMENTS_IPN_SECRET', '')
NOWPAYMENTS_SANDBOX = os.getenv('NOWPAYMENTS_SANDBOX', 'True') == 'True'   # False when going live

# =============================================
# SECURITY & DEPLOYMENT
# =============================================
# Same code on the laptop and on the server: everything that differs comes from .env
#   laptop: DEBUG=True (default)        server: DEBUG=False, SITE_DOMAIN=patreoaix.com
DEBUG = os.getenv('DEBUG', 'True') == 'True'
SECRET_KEY = os.getenv('SECRET_KEY') or ('django-insecure-local-dev-only' if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError('Set SECRET_KEY in .env on the server (python -c "import secrets; print(secrets.token_urlsafe(50))")')

SITE_DOMAIN = os.getenv('SITE_DOMAIN', 'patreoaix.com')
# links in posts, emails and callbacks use the www address: PythonAnywhere serves www.<domain>; the bare domain
# only works once the registrar forwards it to www (set SITE_URL=https://patreoaix.com to change this)
SITE_URL = os.getenv('SITE_URL', f'https://www.{SITE_DOMAIN}').rstrip('/')
PUBLIC_HOST = SITE_URL.split('://', 1)[-1]                       # what posters and captions print
ON_PYTHONANYWHERE = bool(os.getenv('PYTHONANYWHERE_DOMAIN'))      # set by PythonAnywhere itself

ALLOWED_HOSTS = [SITE_DOMAIN, f'www.{SITE_DOMAIN}', '.pythonanywhere.com', 'localhost', '127.0.0.1',
                 '192.168.100.74', '.loca.lt', '.trycloudflare.com']
ALLOWED_HOSTS += [h.strip() for h in os.getenv('EXTRA_ALLOWED_HOSTS', '').split(',') if h.strip()]
if DEBUG:
    ALLOWED_HOSTS.append('*')

# Django refuses https form posts (login, signup, trades …) from origins not listed here
CSRF_TRUSTED_ORIGINS = [
    f'https://{SITE_DOMAIN}', f'https://www.{SITE_DOMAIN}', 'https://*.pythonanywhere.com',
    'https://*.loca.lt', 'https://*.trycloudflare.com',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = not DEBUG          # cookies only over https on the live site
SECURE_SSL_REDIRECT = (not DEBUG) and os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'   # http:// → https://
CSRF_COOKIE_SECURE = not DEBUG

# PythonAnywhere (and plain WSGI hosts) have no websockets: pages poll the REST endpoints instead
WEBSOCKETS_ENABLED = os.getenv('WEBSOCKETS_ENABLED', 'False' if ON_PYTHONANYWHERE else 'True') == 'True'

# =============================================
# APPLICATION DEFINITION
# =============================================
INSTALLED_APPS = [
    'daphne',  #
    'hide_admin.apps.HideAdminConfig',
   # 'django.contrib.admin',  #
    'django.contrib.auth',  #
    'django.contrib.contenttypes',  #
    'django.contrib.sessions',  #
    'django.contrib.messages',  #
    'django.contrib.staticfiles',  #
    'store.apps.StoreConfig',  #
    'channels',  #
    'django_daraja',  #
    'django.contrib.sites',  #
    'allauth',  #
    'allauth.account',  #
    'allauth.socialaccount',  #
    'allauth.socialaccount.providers.google',  #
    'allauth.socialaccount.providers.apple',  #
    'rest_framework',
    'rest_framework_simplejwt',
]
SITE_ID = 1  #



ASGI_APPLICATION = 'project.asgi.application'  #
WSGI_APPLICATION = 'project.wsgi.application'  #

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',  #
    'django.contrib.sessions.middleware.SessionMiddleware',  #
    'django.middleware.common.CommonMiddleware',  #
    'django.middleware.csrf.CsrfViewMiddleware',  #
    'django.contrib.auth.middleware.AuthenticationMiddleware',  #
    'allauth.account.middleware.AccountMiddleware',  #
    'django.contrib.messages.middleware.MessageMiddleware',  #
    'django.middleware.clickjacking.XFrameOptionsMiddleware',  #
]

ROOT_URLCONF = 'project.urls'  #

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',  #
        'DIRS': [],  #
        'APP_DIRS': True,  #
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',  #
                'django.contrib.auth.context_processors.auth',  #
                'django.contrib.messages.context_processors.messages',
                'store.context_processors.kyc_status',
                'store.context_processors.site_flags',
                'store.context_processors.membership',
  #
            ],
        },
    },
]

# =============================================
# CHANNELS & WEBSOCKET LAYER
# =============================================
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',  #
    },
}

# =============================================
# DATABASE
# =============================================
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',  #
        'NAME': BASE_DIR / 'db.sqlite3',  #
        # the website and the background task write at the same time: wait instead of "database is locked"
        'OPTIONS': {'timeout': 20},
    }
}

# =============================================
# CACHE — shared by every process
# =============================================
# Live prices, candles, AI results and provider pauses live here. The default in-memory cache is private
# to each process: fine for one runserver on the laptop, but on a host the website runs several worker
# processes plus the background task, and they must all see the same prices. A file cache is shared.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': os.getenv('CACHE_DIR', str(BASE_DIR / '.cache')),
        'TIMEOUT': 300,
        'OPTIONS': {'MAX_ENTRIES': 20000, 'CULL_FREQUENCY': 4},
    }
}


SIMPLE_JWT = {
    'TOKEN_OBTAIN_SERIALIZER': 'store.serializers.CustomTokenObtainPairSerializer',
}
# =============================================
# PASSWORD VALIDATION
# =============================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},  #
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},  #
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},  #
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},  #
]

# =============================================
# INTERNATIONALIZATION
# =============================================
LANGUAGE_CODE = 'en-us'  #
TIME_ZONE = 'Africa/Nairobi'  #
USE_I18N = True  #
USE_TZ = True  #

# =============================================
# STATIC & MEDIA FILES
# =============================================
STATIC_URL = '/static/'  #
STATIC_ROOT = BASE_DIR / 'staticfiles'      # python manage.py collectstatic → served by the host at /static/
STATICFILES_DIRS = [
    BASE_DIR / "store" / "static",  #
]

MEDIA_URL = '/media/'  #
MEDIA_ROOT = BASE_DIR / 'media'  #

# =============================================
# EMAIL BACKEND (FOR FORGOT PASSWORD)
# =============================================
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# =============================================
# REST FRAMEWORK & JWT CONFIGURATION
# =============================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    )
}

# =============================================
# M-PESA CONFIGURATION
# =============================================
MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY', '')  #  in .env
MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET', '')  #  in .env
MPESA_PASSKEY = os.getenv('MPESA_PASSKEY', 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919')  #
MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE', '174379')  #
MPESA_ENVIRONMENT = os.getenv('MPESA_ENVIRONMENT', 'sandbox')  #
MPESA_EXPRESS_SHORTCODE = '174379'  #
if MPESA_ENVIRONMENT == 'production':  #
    MPESA_BASE_URL = 'https://api.safaricom.co.ke'  #
else:  #
    MPESA_BASE_URL = 'https://sandbox.safaricom.co.ke'  #

MPESA_CALLBACK_URL = os.getenv('MPESA_CALLBACK_URL', f'{SITE_URL}/account-operations/funding/payment-methods/deposit/mpesa-callback/')  #

# =============================================
# LOGGING CONFIGURATION
# =============================================
LOGGING = {
    'version': 1,  #
    'disable_existing_loggers': False,  #
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',  #
        },
        'file': {
            'level': 'INFO',  #
            'class': 'logging.FileHandler',  #
            'filename': str(BASE_DIR / 'mpesa_debug.log'),  #
        },
    },
    'loggers': {
        'store': {
            'handlers': ['console', 'file'],  #
            'level': 'DEBUG' if DEBUG else 'INFO',  #
            'propagate': True,  #
        },
    },
}


EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', 'dikkymccria@gmail.com')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')      # Gmail App Password, in .env only
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', EMAIL_HOST_USER)

# ── Telegram channel for AI signals (python manage.py telegram_test) ──
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')        # from @BotFather
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID', '')      # @yourchannel or -100… (bot must be an admin)
TELEGRAM_MIN_GRADE = os.getenv('TELEGRAM_MIN_GRADE', 'A')       # A or A+
TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', 'True')
TELEGRAM_INVITE_URL = os.getenv('TELEGRAM_INVITE_URL', '')     # t.me link shown on the landing page and in posts
TELEGRAM_SCAN_POSTS = os.getenv('TELEGRAM_SCAN_POSTS', 'True')                 # a list of strong setups after each scan
TELEGRAM_SCAN_REPEAT_MINUTES = os.getenv('TELEGRAM_SCAN_REPEAT_MINUTES', '60')  # same list again after; 0 = every scan
# daily content plan (store/telegram_schedule.py); hours in TELEGRAM_TIMEZONE (default TIME_ZONE)
TELEGRAM_SCHEDULE = os.getenv('TELEGRAM_SCHEDULE', 'True')
TELEGRAM_TIMEZONE = os.getenv('TELEGRAM_TIMEZONE', '')
TELEGRAM_BRIEF_HOUR = os.getenv('TELEGRAM_BRIEF_HOUR', '8')        # morning brief
TELEGRAM_RECAP_HOUR = os.getenv('TELEGRAM_RECAP_HOUR', '22')       # daily results card
TELEGRAM_WEEKLY_DAY = os.getenv('TELEGRAM_WEEKLY_DAY', '6')        # weekly report: 0 = Monday … 6 = Sunday
TELEGRAM_PULSE_HOURS = os.getenv('TELEGRAM_PULSE_HOURS', '3')      # market pulse when quiet this long; 0 = off
TELEGRAM_NEWS_ALERTS = os.getenv('TELEGRAM_NEWS_ALERTS', 'True')   # 30 min before high-impact news
TELEGRAM_COACH_UPDATES = os.getenv('TELEGRAM_COACH_UPDATES', 'True')  # WAIT / GET READY / ENTRY CONFIRMED posts
TELEGRAM_COACH_SYMBOLS = os.getenv('TELEGRAM_COACH_SYMBOLS', 'XAUUSD,EURUSD,GBPUSD,USDJPY,BTCUSD,USOIL')
TELEGRAM_COACH_MINUTES = os.getenv('TELEGRAM_COACH_MINUTES', '60')   # min gap per market (entries post at once)
TELEGRAM_COACH_DAILY = os.getenv('TELEGRAM_COACH_DAILY', '12')       # coach posts per day

# ── Automatic KYC (store/kyc_auto.py): Didit ID + selfie check; empty = manual upload reviewed by staff ──
DIDIT_API_KEY = os.getenv('DIDIT_API_KEY', '')
DIDIT_WORKFLOW_ID = os.getenv('DIDIT_WORKFLOW_ID', '')
DIDIT_WEBHOOK_SECRET = os.getenv('DIDIT_WEBHOOK_SECRET', '')

# ── TikTok: signal posters as photo posts (store/tiktok.py has the setup steps) ──
TIKTOK_CLIENT_KEY = os.getenv('TIKTOK_CLIENT_KEY', '')
TIKTOK_CLIENT_SECRET = os.getenv('TIKTOK_CLIENT_SECRET', '')
TIKTOK_ENABLED = os.getenv('TIKTOK_ENABLED', 'True')
TIKTOK_MIN_GRADE = os.getenv('TIKTOK_MIN_GRADE', 'A')            # A or A+
TIKTOK_DAILY_LIMIT = os.getenv('TIKTOK_DAILY_LIMIT', '10')       # posts per day (TikTok limits posting too)
TIKTOK_POST_RESULTS = os.getenv('TIKTOK_POST_RESULTS', 'True')   # also post the TARGET HIT / STOPPED OUT poster

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'