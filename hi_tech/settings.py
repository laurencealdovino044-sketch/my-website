import os
import socket
from pathlib import Path
from datetime import timedelta


def load_env_file(path):
    """Read KEY=VALUE lines from a .env file into os.environ.

    Every setting below is read from the environment, which on Windows means
    re-exporting them in each new terminal. A .env file next to manage.py keeps
    them in one place instead. Real environment variables always win, so a shell
    export can still override the file for one run.

    No dependency: python-dotenv would do the same for a package install.
    """
    if not path.exists():
        return

    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue

        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()

        # Strip one layer of matching quotes, so spaces survive: KEY="a b"
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        # A blank value means "not configured", so leave it out of the environment
        # entirely: setting it to '' would override the defaults below, and an empty
        # DJANGO_SECRET_KEY stops Django from starting at all.
        if key and value and key not in os.environ:
            os.environ[key] = value


load_env_file(Path(__file__).resolve().parent.parent / '.env')



def get_default_host_ip():
    """Return a non-loopback IPv4 address for this machine, if available."""
    candidates = []
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM):
            ip = sockaddr[0]
            if family == socket.AF_INET and not ip.startswith('127.'):
                candidates.append(ip)
    except Exception:
        candidates = []

    if not candidates:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith('127.'):
                candidates.append(ip)
        except Exception:
            candidates = []

    if not candidates:
        return '127.0.0.1'
    return candidates[0]


# MediaMTX listens for RTMP on 1935 and serves HLS on 8888 (see deployment/mediamtx.yml).
# The port has to be explicit in the RTMP base: DJI Fly publishes to exactly the URL it is
# given, and "rtmp://host/live" would leave the drone talking to port 1935 only by luck of
# the client's default. Keep these in sync with the listen addresses in mediamtx.yml.
DEFAULT_RTMP_PORT = os.environ.get("DRONE_RTMP_PORT", "1935").strip() or "1935"
DEFAULT_HLS_PORT = os.environ.get("MEDIAMTX_HLS_PORT", "8888").strip() or "8888"


def get_default_rtmp_base_url():
    return f"rtmp://{get_default_host_ip()}:{DEFAULT_RTMP_PORT}/live"


def get_default_hls_base_url():
    return f"http://{get_default_host_ip()}:{DEFAULT_HLS_PORT}/live"

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-this-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if host.strip()]

LOGIN_REDIRECT_URL = '/'  # Redirect to the home page after login
LOGOUT_REDIRECT_URL = '/'  # Redirect to the home page after logout
LOGIN_URL = "/streams/auth/login/"

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'predictions.apps.PredictionsConfig',
    'streams.apps.StreamsConfig',
    'widget_tweaks',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'hi_tech.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'hi_tech.wsgi.application'

DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite").lower()
if DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "dji_streams"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
elif DB_ENGINE in {"mysql", "mariadb"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("DB_NAME", "dji_streams"),
            "USER": os.environ.get("DB_USER", "root"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "3306"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DRONE_RTMP_URL = os.environ.get('DRONE_RTMP_URL', '').strip()
DRONE_RTMP_PUBLISH_URL = os.environ.get('DRONE_RTMP_PUBLISH_URL', '').strip()
DRONE_RTMP_STREAM_KEY = os.environ.get('DRONE_RTMP_STREAM_KEY', 'eggplant-live').strip() or 'eggplant-live'
DRONE_RTMP_PORT = os.environ.get('DRONE_RTMP_PORT', '1935').strip() or '1935'

STREAM_RTMP_BASE_URL = (os.environ.get("STREAM_RTMP_BASE_URL") or get_default_rtmp_base_url()).strip()
STREAM_HLS_BASE_URL = (os.environ.get("STREAM_HLS_BASE_URL") or get_default_hls_base_url()).strip()
MEDIAMTX_API_BASE_URL = os.environ.get("MEDIAMTX_API_BASE_URL", "http://127.0.0.1:9997").strip()

# HLS host without the "/live" application segment. A drone that joined the RTMP URL and key
# without a "/" publishes to the path "livedji_<key>", and its playlist then lives at
# /livedji_<key>/index.m3u8 - outside STREAM_HLS_BASE_URL - so playback URLs are built from
# this root plus whatever path MediaMTX reports.
STREAM_HLS_ROOT_URL = (
    os.environ.get("STREAM_HLS_ROOT_URL")
    or f"http://{get_default_host_ip()}:{DEFAULT_HLS_PORT}"
).strip()

# LIVE_CAPTURE_SOURCE controls the ffmpeg input used when starting a live publish from the web UI.
# Examples:
#  - RTSP camera:   "rtsp://192.168.1.10:554/stream"
#  - Local video device on Linux: "/dev/video0"
#  - Windows DirectShow device (example): "video=Integrated Camera"
# You can set this via environment variable LIVE_CAPTURE_SOURCE or in Django settings.

def detect_default_capture_source():
    """Heuristically choose a sensible local camera/RTSP source for this host."""
    if os.name == 'nt':
        # Common Windows webcam names. Prefer the built-in camera if present.
        for candidate in ('video=Integrated Camera', 'video=USB Camera', 'video=FaceTime HD Camera'):
            return candidate
        return 'video=Integrated Camera'

    for candidate in ('/dev/video0', '/dev/video1', '/dev/video2'):
        if os.path.exists(candidate):
            return candidate

    return 'rtsp://127.0.0.1:8554/stream'


LIVE_CAPTURE_SOURCE = (os.environ.get('LIVE_CAPTURE_SOURCE') or detect_default_capture_source()).strip() or None

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ---------------------------------------------------------------------------
# Notifications (Gmail e-mail + SMS)
# ---------------------------------------------------------------------------
# Alerts are sent from predictions/notifications.py. Both channels are optional:
# whatever is not configured is simply skipped, so the app still runs with no
# credentials set. Configure through environment variables, never in this file.

NOTIFICATIONS_ENABLED = os.environ.get("NOTIFICATIONS_ENABLED", "1") == "1"

# Gmail SMTP. Gmail rejects account passwords for SMTP, so GMAIL_APP_PASSWORD must be a
# 16-character App Password generated at https://myaccount.google.com/apppasswords
# (requires 2-Step Verification on the account).
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "0") == "1"
EMAIL_HOST_USER = (os.environ.get("GMAIL_ADDRESS") or os.environ.get("EMAIL_HOST_USER", "")).strip()
EMAIL_HOST_PASSWORD = (
    os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("EMAIL_HOST_PASSWORD", "")
).replace(" ", "")
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "15"))
DEFAULT_FROM_EMAIL = (
    os.environ.get("DEFAULT_FROM_EMAIL")
    or (f"Hi-Tech Eggplant <{EMAIL_HOST_USER}>" if EMAIL_HOST_USER else "webmaster@localhost")
)

# Without credentials, SMTP would raise on every send. Print the message to the console
# instead so local development still shows exactly what would have been mailed.
if os.environ.get("DJANGO_EMAIL_BACKEND"):
    EMAIL_BACKEND = os.environ["DJANGO_EMAIL_BACKEND"]
elif EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# SMS gateway. "twilio" works worldwide; "semaphore" is a Philippine gateway that is
# cheaper for local 09xx numbers. Both are called over plain HTTPS with `requests`,
# so neither one adds a dependency. Leave unset to log messages instead of sending.
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "").strip().lower()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()

SEMAPHORE_API_KEY = os.environ.get("SEMAPHORE_API_KEY", "").strip()
SEMAPHORE_SENDER_NAME = os.environ.get("SEMAPHORE_SENDER_NAME", "").strip()

if not SMS_PROVIDER:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER:
        SMS_PROVIDER = "twilio"
    elif SEMAPHORE_API_KEY:
        SMS_PROVIDER = "semaphore"
    else:
        SMS_PROVIDER = "console"

# Local numbers are typed as 09171234567; gateways want E.164 (+639171234567).
SMS_DEFAULT_COUNTRY_CODE = os.environ.get("SMS_DEFAULT_COUNTRY_CODE", "+63").strip()
SMS_TIMEOUT = int(os.environ.get("SMS_TIMEOUT", "15"))


# ---------------------------------------------------------------------------
# Sign in with Google
# ---------------------------------------------------------------------------
# Uses Google Identity Services: the browser gets an ID token, predictions.views
# .google_login verifies it server-side with google-auth (already a dependency of
# firebase-admin), and signs the matching user in. Create the client ID at
# https://console.cloud.google.com/apis/credentials -> OAuth client ID -> Web
# application, and list the site's origin under "Authorized JavaScript origins"
# (e.g. http://localhost:8000). No client secret is needed for this flow.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()

# Google reports whether it has verified the address on the account. Linking an
# unverified one to an existing account would let anybody who can claim that
# address take it over, so those sign-ins are refused.
GOOGLE_REQUIRE_VERIFIED_EMAIL = os.environ.get("GOOGLE_REQUIRE_VERIFIED_EMAIL", "1") == "1"

# Only the local system account may authenticate.
AUTHENTICATION_BACKENDS = ["predictions.auth.SingleUserBackend"]
