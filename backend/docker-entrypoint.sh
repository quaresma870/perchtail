#!/bin/sh
set -e

# Started as root only long enough to make sure /data belongs to the
# unprivileged perchtail user -- volumes created by images before 0.4.0 are
# root-owned -- then re-exec this script without root and without any
# capabilities. Started as non-root (`docker run --user ...`), skip straight
# to the app.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  find /data ! -user perchtail -exec chown perchtail:perchtail {} +
  exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all \
    --bounding-set=-all "$0" "$@"
fi

python -m alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
