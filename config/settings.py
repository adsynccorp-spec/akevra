"""
Django settings for AKEVRA Sprint 0.
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if os.getenv("DEBUG", "").lower() in {"1", "true", "yes", "on"}:
        SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError("SECRET_KEY environment variable must be set in production.")

DEBUG = _env_bool("DEBUG", False)
if DEBUG is False and os.getenv("DEBUG") is None:
    pass  # safe default: DEBUG off unless explicitly enabled
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "apps.core",
    "apps.organizations",
    "apps.accounts.apps.AccountsConfig",
    "apps.rbac",
    "apps.supervision",
    "apps.audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.TenantIsolationMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

def _database_config():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        parsed = urlparse(database_url)
        sslmode = (parse_qs(parsed.query).get("sslmode") or ["require"])[0]
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote((parsed.path or "/").lstrip("/") or "neondb"),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or 5432),
            "OPTIONS": {"sslmode": sslmode},
            "ATOMIC_REQUESTS": True,
            # Reuse the connection between requests: a fresh TLS handshake to a remote
            # database (Neon) costs ~3 s, which made every API call slow.
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "600")),
            "CONN_HEALTH_CHECKS": True,
        }
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "akevra"),
        "USER": os.getenv("DB_USER", "akevra"),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
        "ATOMIC_REQUESTS": True,
    }
    if os.getenv("DB_SSL", "").lower() in {"1", "true", "require"}:
        config["OPTIONS"] = {"sslmode": "require"}
    return config


DATABASES = {"default": _database_config()}

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

AUTH_USER_MODEL = "accounts.Identity"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.SessionTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AKEVRA API",
    "DESCRIPTION": (
        "Sprint 0 foundation API. Authenticate with POST /api/v1/auth/login, "
        "copy the token, then click Authorize and paste it as a Bearer token.\n\n"
        "Demo password for every account: `Sprint0!Akevra`\n\n"
        "- `alex@akevra.test` — one organization, goes straight in\n"
        "- `admin@akevra.test` — two organizations, must select a workspace\n"
        "- `jordan@akevra.test` — Supervisor in one relationship, Supervisee in another\n"
        "- `mfa@akevra.test` — MFA; TOTP secret `JBSWY3DPEHPK3PXP`"
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SERVE_AUTHENTICATION": [],
    "COMPONENT_SPLIT_REQUEST": True,
    "SWAGGER_UI_SETTINGS": {
        "persistAuthorization": True,
        "displayRequestDuration": True,
    },
    "DISABLE_ERRORS_AND_WARNINGS": True,
}

AUTH_LOCKOUT_THRESHOLD = int(os.getenv("AUTH_LOCKOUT_THRESHOLD", "5"))
AUTH_LOCKOUT_SECONDS = int(os.getenv("AUTH_LOCKOUT_SECONDS", "900"))
AUTH_SESSION_IDLE_SECONDS = int(os.getenv("AUTH_SESSION_IDLE_SECONDS", "900"))
AUTH_SESSION_ABSOLUTE_SECONDS = int(os.getenv("AUTH_SESSION_ABSOLUTE_SECONDS", "28800"))
AUTH_MFA_ISSUER = os.getenv("AUTH_MFA_ISSUER", "AKEVRA")

# Invitations (RBAC-002). Set EMAIL_* for real delivery; the console backend prints
# mail to the server log, and the admin always gets the activation link in the API.
INVITATION_TTL_HOURS = int(os.getenv("INVITATION_TTL_HOURS", "72"))
PASSWORD_RESET_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "10"))
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))  # fail fast if the host blocks SMTP
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")  # for EMAIL_BACKEND=apps.core.email.BrevoEmailBackend
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "AKEVRA <no-reply@akevra.app>")
