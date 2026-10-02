import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import environ

# Define the base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize environment variables
env = environ.Env()
env.read_env(env_file=str(BASE_DIR) + "/.env")

# Production environment flag
PRODUCTION = env("PRODUCTION", cast=bool, default=False)

# Security settings
SECRET_KEY = env(
    "SECRET_KEY",
    default=secrets.token_urlsafe(64),  # Generate a strong secret key if not provided
)

DEBUG = not PRODUCTION

if DEBUG:
    ALLOWED_HOSTS = ["*"]
else:
    allowed_hosts_env = env("ALLOWED_HOSTS", default="*")
    if allowed_hosts_env.strip() == "*":
        ALLOWED_HOSTS = ["*"]
    else:
        ALLOWED_HOSTS = [
            host.strip() for host in allowed_hosts_env.split(",") if host.strip()
        ]
    # Force SSL redirect in production
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Custom user model
AUTH_USER_MODEL = "authorization.CustomUserModel"


# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party apps
    "sorl.thumbnail",
    "corsheaders",
    "rest_framework",
    "drf_yasg",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_userforeignkey",
    # Custom apps
    "authentication",
    "authorization",
    "app",
    "filesystem",
]

# Middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_userforeignkey.middleware.UserForeignKeyMiddleware",
]

ROOT_URLCONF = "autograde.urls"

# Templates configuration
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "autograde.wsgi.application"


# Database configuration
DATABASE_URL = env("POSTGRESQL_DATABASE_URL", default=None)
if PRODUCTION and DATABASE_URL:
    tmpPostgres = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": tmpPostgres.path.replace("/", ""),
            "USER": tmpPostgres.username,
            "PASSWORD": tmpPostgres.password,
            "HOST": tmpPostgres.hostname,
            "PORT": 5432,
            "OPTIONS": dict(parse_qsl(tmpPostgres.query)),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRESQL_DATABASE"),
            "USER": env("POSTGRESQL_USER"),
            "PASSWORD": env("POSTGRESQL_ROOT_PASSWORD"),
            "HOST": env("POSTGRESQL_HOST"),
            "PORT": env("POSTGRESQL_PORT"),
        }
    }


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization settings
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_L10N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Local file storage
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Authentication backends
AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)


# Django REST Framework configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
}


# Default token lifetimes
ACCESS_TOKEN_LIFETIME_MINUTES = 6
REFRESH_TOKEN_LIFETIME_DAYS = 30


# JWT settings
SIMPLE_JWT = {
    "TOKEN_OBTAIN_SERIALIZER": "authentication.serializers.serializers.TokenObtainPairSerializer",
    "TOKEN_REFRESH_SERIALIZER": "authentication.serializers.serializers.RefreshTokenSerializer",
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "id",
}


# OTP settings (in minutes)
OTP_VERIFICATION_TIME = env("OTP_VERIFICATION_TIME", cast=int, default=30)
OTP_MAX_ATTEMPTS = env("OTP_MAX_ATTEMPTS", cast=int, default=5)
OTP_RESEND_COOLDOWN_TIME = env("OTP_RESEND_COOLDOWN_TIME", cast=int, default=1)
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")

REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "otp_request": "10/hour",
    "otp_verify": "30/hour",
    "google_auth": "30/hour",
}

# Email settings
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="smtp.your-email-provider.com")
EMAIL_USE_TLS = env("EMAIL_USE_TLS", cast=bool, default=True)
EMAIL_PORT = env("EMAIL_PORT", cast=int, default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="test@example.com")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="testpassword")


# Swagger settings
SWAGGER_SETTINGS = {
    "DEFAULT_AUTO_SCHEMA_CLASS": "autograde.swagger.CustomAutoSchema",
    "SECURITY_DEFINITIONS": {
        "basic": {"type": "basic"},
        "Bearer": {"type": "apiKey", "name": "Authorization", "in": "header"},
    },
    "USE_SESSION_AUTH": True,
    "LOGIN_URL": "/api-auth/login/",
    "LOGOUT_URL": "/api-auth/logout/",
}


# CORS settings
if DEBUG:
    # Allow all origins in development
    CORS_ALLOW_ALL_ORIGINS = True
else:
    # Restrict CORS in production
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in env("CORS_ALLOWED_ORIGINS", default="").split(",")
        if origin.strip()
    ]
    # Fallback to allow all if no specific origins are configured
    if not CORS_ALLOWED_ORIGINS:
        CORS_ALLOW_ALL_ORIGINS = True

    # Security settings for production
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_HSTS_SECONDS = 31536000
    X_FRAME_OPTIONS = "DENY"
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True

# Additional CORS settings
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

CORS_ALLOWED_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]
