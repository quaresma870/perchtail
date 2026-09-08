# Contributing to PerchTail

Thanks for considering it — this project is early-stage, so contributions of any
size (a typo fix, a bug report, a whole connector) genuinely help.

## Before you start

For anything beyond a small fix, please open an issue or discussion first. This
project touches production credentials and RBAC, so design changes in those areas
need a conversation before a PR, not after.

## Development setup

Backend (FastAPI):

```bash
git clone https://github.com/quaresma870/perchtail.git
cd perchtail/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Frontend (Vite + Svelte), in a separate terminal from the repo root:

```bash
cd frontend
npm install
npm run dev      # dev server, proxies API calls to a backend on :8000
npm run check    # svelte-check
npm test         # vitest
```

End-to-end tests (Playwright), from `frontend/`:

```bash
npx playwright install --with-deps chromium   # one-time, per machine
npm run test:e2e
```

This builds the frontend and starts its own isolated backend on port 8001
with a throwaway SQLite DB (`backend/scripts/run_e2e_server.sh`) — it won't
touch a `npm run dev` backend you already have running on :8000. It also
provisions real, throwaway sshd/smbd test servers on ports 2222/1445
(`backend/scripts/setup_e2e_test_servers.sh`, plain `apt-get install
openssh-server samba`) so the ssh/smb source specs run against a real
protocol implementation — the first run may prompt for `sudo` if those
aren't already installed. Set `SKIP_E2E_TEST_SERVERS=1` to skip that step
for a quick run of everything else without needing `sudo`/those packages
locally (`sources-ssh.spec.ts`, `sources-smb.spec.ts`, and `search.spec.ts`
will fail with nothing listening on those ports, everything else is
unaffected). See `frontend/e2e/` and ROADMAP.md's "Frontend E2E testing
(Playwright)" section for how it's wired together.

See [CLAUDE.md](CLAUDE.md) for the full architecture and repo layout — it's kept
up to date as the source of truth for design decisions.

## Conventions

- Type hints everywhere; Pydantic models for API request/response bodies.
- `ruff` + `black` for lint/format — run both before opening a PR.
- Tests via `pytest`. Mock the SSH/SMB/WinRM clients in unit tests; never hit a
  real remote host in CI.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`) — this drives the changelog.

## Pull requests

1. Fork, branch from `dev` (the integration branch everything lands on between
   releases — `main` only moves forward at a tagged release), keep the PR
   focused on one change, and open it against `dev`.
2. Add or update tests for anything behavior-affecting.
3. Update [CHANGELOG.md](CHANGELOG.md) under `Unreleased`.
4. Describe what changed and why in the PR description — link the issue if there
   is one.

## Security-sensitive areas

Changes touching credential storage, RBAC/grant resolution, or auth providers get
extra scrutiny by design — see [SECURITY.md](SECURITY.md) for what's in scope and
how to report a vulnerability privately rather than via a public PR or issue.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Participation
means agreeing to keep discussion respectful and on-topic.
