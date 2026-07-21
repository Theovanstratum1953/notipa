#!/bin/sh
set -e

if [ "$DJANGO_ENV" = "production" ]; then
  echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
  while ! nc -z "$DB_HOST" "$DB_PORT"; do
    sleep 0.5
  done
  echo "PostgreSQL is up."
fi

python manage.py migrate --noinput

if [ "$DJANGO_ENV" = "production" ]; then
  python manage.py collectstatic --noinput
  exec gunicorn notipa.wsgi:application --bind 0.0.0.0:8000 --workers 3
else
  exec python manage.py runserver 0.0.0.0:8000
fi
