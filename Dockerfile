# Frontend build stage — produces the static SPA bundle only; this stage's
# node toolchain never ships in the final image. The bundle is the same on
# every architecture, so it is built on the builder's own platform rather
# than under emulation in multi-arch release builds.
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Backend + runtime stage
FROM python:3.12-slim

WORKDIR /app/backend

# python:3.12-slim's own OS packages lag behind Debian's security patches
# between upstream rebuilds (see issue #87) — pull current packages from the
# live Debian security mirror at build time instead of trusting whatever the
# base image tag happened to bake in.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# The app runs as this unprivileged user, never as root -- see
# docker-entrypoint.sh for how /data ownership is handed over on startup.
RUN groupadd --system --gid 10001 perchtail \
    && useradd --system --uid 10001 --gid perchtail --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin perchtail \
    && mkdir -p /data && chown perchtail:perchtail /data

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini .
COPY backend/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# The signed push-agent binaries of this version (#319), which agents
# upgraded from the portal download from the server (app/agent_upgrades.py).
# The release workflow puts them in agent-dist/; in any other build the
# folder only holds its .gitkeep, and agents use the GitHub release instead.
COPY agent-dist/ /app/agent-binaries/

# Sibling of backend/, matching the repo layout — app/main.py locates it via
# a path relative to itself, so this placement isn't arbitrary.
COPY --from=frontend-build /frontend/dist /app/frontend/dist

# Everything stateful lives under /data, the volume docker-compose.yml
# mounts, in one directory per kind of data (#172) so each can be mounted
# on its own: /data/db holds what must be backed up (database, credential
# salt, SSH known_hosts -- losing the salt makes every stored credential
# undecryptable, and losing known_hosts silently resets SSH host-key
# pinning), /data/logs and /data/scratch what can be lost. Images before
# 0.5.0 kept the database and its siblings at /data itself; the entrypoint
# moves them (app/data_layout.py). PERCHTAIL_INSTALL tells the Updates card
# under Settings -> System which upgrade steps to show (#215).
ENV DATA_DIR=/data/db \
    LOG_DIR=/data/logs \
    SCRATCH_DIR=/data/scratch \
    AGENT_BINARIES_DIR=/app/agent-binaries \
    PERCHTAIL_INSTALL=docker

EXPOSE 8000

# Liveness only: /readyz (database, DATA_DIR, scheduler) is for an orchestrator's
# readiness probe, see docs/high-availability.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
