release: python manage.py migrate --noinput
web: gunicorn config.wsgi:application
worker: celery -A config worker -l info --concurrency=2
