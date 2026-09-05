#!/usr/bin/env bash
# Starts a from-scratch PerchTail backend for the Playwright E2E suite (see
# frontend/playwright.config.ts, which invokes this via `npm run e2e:server`
# after building the frontend). Isolated from a developer's normal dev
# backend on every axis that matters:
#   - port 8001, not the documented dev port 8000 (CONTRIBUTING.md), so this
#     can run alongside `npm run dev` / a real local backend without conflict.
#   - its own SQLite DB, credential salt, known_hosts, log dir, and scratch
#     dir under backend/data/e2e/ -- wiped and recreated on every run, so
#     each test run starts from a clean, deterministic state.
# Not meant to be run any other way -- it deletes backend/data/e2e/ on every
# invocation.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Best-effort venv activation: works locally where CONTRIBUTING.md's
# `python -m venv .venv` setup exists; in CI, dependencies are typically
# installed into the runner's own Python and there's no .venv to find.
source .venv/bin/activate 2>/dev/null || true

export DATABASE_URL="sqlite:///./data/e2e/perchtail.db"
# Anything non-empty and not the insecure "changeme" default main.py refuses
# to start with -- this DB is thrown away after every run, so there's no
# real secret to protect here.
export CREDENTIAL_ENCRYPTION_KEY="${CREDENTIAL_ENCRYPTION_KEY:-e2e-test-key-not-for-production-use}"
export CREDENTIAL_SALT_PATH="./data/e2e/credential_salt"
export SSH_KNOWN_HOSTS_PATH="./data/e2e/ssh_known_hosts"
export LOG_DIR="./data/e2e/logs"
export SCRATCH_DIR="./data/e2e/scratch"
# No HTTPS in the e2e run -- a Secure cookie would be silently dropped by
# the browser and every authenticated request would look logged-out.
export SESSION_COOKIE_SECURE="false"
export E2E_ADMIN_USERNAME="${E2E_ADMIN_USERNAME:-e2e-admin}"
export E2E_ADMIN_PASSWORD="${E2E_ADMIN_PASSWORD:-e2e-test-password-123!}"

rm -rf ./data/e2e
mkdir -p ./data/e2e

python -m app.seed_e2e_admin
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
