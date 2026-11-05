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

Python dependencies: `backend/requirements.in` lists what the backend needs,
with version ranges; `backend/requirements.txt` pins every package
(transitive ones included) and is what the Docker image and a from-source
install use. To add or upgrade a package, edit `requirements.in`, then
regenerate the pins with [uv](https://docs.astral.sh/uv/) and commit both:

```bash
cd backend
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
```

To bump everything within the ranges, add `--upgrade`.

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

## Releasing (maintainers)

Releases are immutable once published: a published release accepts no new
assets and its tag can't be reused. `.github/workflows/release.yml`
therefore prepares a **draft**, and a maintainer publishes it.

1. Merge a PR into `dev` that sets the version in `backend/app/version.py`
   and `frontend/package.json` (`cd frontend && npm version --no-git-tag-version X.Y.Z`)
   and adds a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`.
   Use a suffix such as `X.Y.Z-rc.1` for a pre-release.
2. Start the Release workflow, either way:
   - Push the tag: `git tag vX.Y.Z <commit> && git push origin vX.Y.Z`.
   - Or Actions → Release → Run workflow, choosing `dev` under "Use
     workflow from", with the tag (`vX.Y.Z`) and the branch or commit to
     tag (default `dev`). The workflow creates the tag. GitHub only shows
     this button once `release.yml` is on the default branch (`main`); until
     then, push the tag, or dispatch the workflow through the API with
     `ref: dev`.

   It refuses a tag whose code carries another version or has no
   CHANGELOG section, and a tag whose release is already published.
3. It builds the agent binaries and `SHA256SUMS`, pushes
   `ghcr.io/quaresma870/perchtail:X.Y.Z` with its SBOM, and creates a
   draft release with all of them attached and the CHANGELOG section as
   notes. A tag with a suffix becomes a pre-release.
4. Review the draft (notes, assets), edit the notes if needed, and
   publish it. Publishing a full release moves the image's `X.Y` and
   `latest` tags to it; a pre-release never does.

To fix a draft, push the fix, delete the draft and its tag, and run the
workflow again. Once a release is published, a fix needs a new version.

## Security-sensitive areas

Changes touching credential storage, RBAC/grant resolution, or auth providers get
extra scrutiny by design — see [SECURITY.md](SECURITY.md) for what's in scope and
how to report a vulnerability privately rather than via a public PR or issue.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Participation
means agreeing to keep discussion respectful and on-topic.
