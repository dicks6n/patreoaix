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
DERIV_OAUTH_REDIRECT = os.environ.get(
    'DERIV_OAUTH_REDIRECT',
    'http://127.0.0.1:8000/deriv-callback/'  # change in production
)
DERIV_OAUTH_SCOPES = 'read trade'
# =============================================
# NOWCRYPTO API CONFIGURATION
# =============================================

TWELVEDATA_API_KEY = os.environ.get('TWELVEDATA_API_KEY', 'cdd0d6f92a5948288c3d102a874b7ff6')


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
NOWPAYMENTS_API_KEY = '3PKABWF-B65MP44-K6DR5HH-RB6XGH3'
NOWPAYMENTS_IPN_SECRET = 'cLoO44Lqh4PIGxVbhNcvyFyEHV/8E+97'
NOWPAYMENTS_SANDBOX = True  # Set to False when going live

# =============================================
# SECURITY & DEPLOYMENT
# =============================================
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-ahw@(v2zyav9rjx$yqhgjer+qfa$xiv7k1d37!q$0w8ylwba3k')  #
DEBUG = True  #
ALLOWED_HOSTS = ['*' , '192.168.100.74' , '.loca.lt', '.trycloudflare.com']  #


CSRF_TRUSTED_ORIGINS = [
    'https://*.loca.lt', 'https://*.trycloudflare.com',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

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
MPESA_CONSUMER_KEY = os.getenv('MPESA_CONSUMER_KEY', 'LFuAPkR8BWp3uQ4NAUdtRPi0umQVYT5sS8XTEbWOgNsgept2')  #
MPESA_CONSUMER_SECRET = os.getenv('MPESA_CONSUMER_SECRET', 'UCf5kdz3nAe6Gakk2IJb69gBznmX6knNe1eA7MeQD1pzgmoI7PxaxXGWNLiLawtN')  #
MPESA_PASSKEY = os.getenv('MPESA_PASSKEY', 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919')  #
MPESA_SHORTCODE = os.getenv('MPESA_SHORTCODE', '174379')  #
MPESA_ENVIRONMENT = os.getenv('MPESA_ENVIRONMENT', 'sandbox')  #
MPESA_EXPRESS_SHORTCODE = '174379'  #
if MPESA_ENVIRONMENT == 'production':  #
    MPESA_BASE_URL = 'https://api.safaricom.co.ke'  #
else:  #
    MPESA_BASE_URL = 'https://sandbox.safaricom.co.ke'  #

MPESA_CALLBACK_URL = os.getenv('MPESA_CALLBACK_URL', 'https://your-domain.com/mpesa/callback/')  #

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
            'level': 'DEBUG',  #
            'class': 'logging.FileHandler',  #
            'filename': 'mpesa_debug.log',  #
        },
    },
    'loggers': {
        'store': {
            'handlers': ['console', 'file'],  #
            'level': 'DEBUG',  #
            'propagate': True,  #
        },
    },
}


EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'dikkymccria@gmail.com'  # Replace with your Gmail address
EMAIL_HOST_PASSWORD = 'iqhl anfs hdkr igxi'     # Replace with your 16-digit Gmail App Password
DEFAULT_FROM_EMAIL = 'dikkymccria@gmail.com'

ADMIN_EMAIL = 'dikkymccria@gmail.com'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'