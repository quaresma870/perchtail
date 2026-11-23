#!/bin/sh
set -e

# The test hooks (app/main.py) are for the end-to-end run from a source
# checkout; the image ships no test code, and a deployment never sets them.
if [ -n "${PERCHTAIL_TEST_PATCH_MODULE:-}" ] || [ -n "${PERCHTAIL_TEST_MODE:-}" ]; then
  echo "PERCHTAIL_TEST_PATCH_MODULE and PERCHTAIL_TEST_MODE are for the tests only; remove them from the environment." >&2
  exit 1
fi

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

# Moves a pre-0.5.0 /data layout into DATA_DIR before anything opens the
# database (app/data_layout.py).
python -m app.data_layout
python -m alembic upgrade head
# --no-proxy-headers: the app's own ClientAddressMiddleware (TRUSTED_PROXIES,
# app/request_context.py) is the only thing that reads X-Forwarded-For.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
