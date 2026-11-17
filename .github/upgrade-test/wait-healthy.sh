#!/usr/bin/env bash
# Waits until PerchTail in the container answers /healthz, asked from inside
# the container: from 0.7.0 on the probe answers a local caller only.
set -euo pipefail
container="$1"
for _ in $(seq 1 90); do
  if docker exec "$container" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" 2>/dev/null; then
    echo "$container is up"
    exit 0
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
    echo "::error title=Upgrade test::$container stopped while starting"
    docker logs "$container" 2>&1 | tail -50
    exit 1
  fi
  sleep 2
done
echo "::error title=Upgrade test::$container did not answer /healthz within 3 minutes"
docker logs "$container" 2>&1 | tail -50
exit 1
