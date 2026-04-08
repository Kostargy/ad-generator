# ruff: noqa: E501
from .base import *  # noqa: F403
from .base import APPS_DIR
from .base import DATABASES
from .base import REDIS_SSL
from .base import REDIS_URL
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[".herokuapp.com"])

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES
# ------------------------------------------------------------------------------
_redis_options: dict = {
    "CLIENT_CLASS": "django_redis.client.DefaultClient",
    # Mimicking memcache behavior.
    # https://github.com/jazzband/django-redis#memcached-exceptions-behavior
    "IGNORE_EXCEPTIONS": True,
}
if REDIS_SSL:
    # Heroku Redis uses self-signed certs.
    import ssl

    _redis_options["CONNECTION_POOL_KWARGS"] = {"ssl_cert_reqs": ssl.CERT_NONE}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": _redis_options,
    },
}

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-proxy-ssl-header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-ssl-redirect
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-secure
SESSION_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-name
SESSION_COOKIE_NAME = "__Secure-sessionid"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-secure
CSRF_COOKIE_SECURE = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-name
CSRF_COOKIE_NAME = "__Secure-csrftoken"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-trusted-origins
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=["https://*.herokuapp.com"],
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-seconds
# TODO: set this to 60 seconds first and then to 518400 once you prove the former works
SECURE_HSTS_SECONDS = 60
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-include-subdomains
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-preload
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
# https://docs.djangoproject.com/en/dev/ref/middleware/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)

# STATIC & MEDIA
# ------------------------------------------------------------------------------
# Static files always go through WhiteNoise (works on Heroku, no S3 round-trip).
# Media files: if a Bucketeer addon is provisioned (BUCKETEER_BUCKET_NAME is set),
# upload to its S3 bucket. Otherwise fall back to local filesystem (ephemeral).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
STATIC_URL = "/static/"
MEDIA_URL = "/media/"
MEDIA_ROOT = str(APPS_DIR / "media")

if env("BUCKETEER_BUCKET_NAME", default=""):
    AWS_ACCESS_KEY_ID = env("BUCKETEER_AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("BUCKETEER_AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("BUCKETEER_BUCKET_NAME")
    AWS_S3_REGION_NAME = env("BUCKETEER_AWS_REGION", default="us-east-1")
    # Bucketeer buckets are private. Use presigned URLs (querystring auth) so
    # media files load from S3 without needing public-read ACLs.
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = 60 * 60 * 24  # 24h signed URLs
    AWS_S3_FILE_OVERWRITE = False
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "location": "media",
            "file_overwrite": False,
        },
    }

# EMAIL
# ------------------------------------------------------------------------------
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL",
    default="Everdries_ad_generator <noreply@example.com>",
)
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env(
    "DJANGO_EMAIL_SUBJECT_PREFIX",
    default="[Everdries_ad_generator] ",
)
ACCOUNT_EMAIL_SUBJECT_PREFIX = EMAIL_SUBJECT_PREFIX
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = env("DJANGO_ADMIN_URL", default="admin/")

# LOGGING
# ------------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"require_debug_false": {"()": "django.utils.log.RequireDebugFalse"}},
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
    },
    "handlers": {
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "django.request": {
            "handlers": ["mail_admins"],
            "level": "ERROR",
            "propagate": True,
        },
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": ["console", "mail_admins"],
            "propagate": True,
        },
    },
}


# Your stuff...
# ------------------------------------------------------------------------------
