# Frontend build stage — produces the static SPA bundle only; this stage's
# node toolchain never ships in the final image.
FROM node:22-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Backend + runtime stage
FROM python:3.12-slim

WORKDIR /app/backend

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini .
COPY backend/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Sibling of backend/, matching the repo layout — app/main.py locates it via
# a path relative to itself, so this placement isn't arbitrary.
COPY --from=frontend-build /frontend/dist /app/frontend/dist

ENV LOG_DIR=/data/logs \
    DATABASE_URL=sqlite:////data/perchtail.db \
    SCRATCH_DIR=/data/scratch

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
