#!/bin/sh
set -eu
python manage.py migrate --noinput
python manage.py bootstrap_accounts
exec python manage.py runserver "0.0.0.0:${APP_PORT:-8080}"
