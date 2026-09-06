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
# See app/testing/fake_winrm.py -- there's no real WinRM target available in
# CI or a dev sandbox, unlike ssh/smb below, which run against real local
# test servers.
export PERCHTAIL_TEST_PATCH_MODULE="app.testing.fake_winrm"
# The default (300s) would leave frontend/e2e/search.spec.ts and
# alerts.spec.ts waiting minutes for the background indexer to pick up a
# freshly-created source -- short enough here to keep those specs fast
# without being so short it starves other requests on a single-process
# uvicorn.
export SEARCH_INDEX_INTERVAL_SECONDS="2"

rm -rf ./data/e2e
mkdir -p ./data/e2e

# Real sshd/smbd test targets for frontend/e2e/sources-ssh.spec.ts,
# sources-smb.spec.ts, and search.spec.ts (see setup_e2e_test_servers.sh for
# why these are real servers rather than mocked, unlike WinRM above). Must
# run after the rm -rf above -- it populates its own fixture files under
# data/e2e/. Skippable (SKIP_E2E_TEST_SERVERS=1) for a quick local run of
# everything else without needing sudo/openssh-server/samba installed --
# just expect those three specs to fail against nothing listening on
# 2222/1445. CI always runs it.
if [ "${SKIP_E2E_TEST_SERVERS:-}" != "1" ]; then
  bash "$(dirname "${BASH_SOURCE[0]}")/setup_e2e_test_servers.sh"
fi

python -m app.seed_e2e_admin
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
