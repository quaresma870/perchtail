# Contributing to PerchTail

Thanks for considering it — this project is early-stage, so contributions of any
size (a typo fix, a bug report, a whole connector) genuinely help.

## License and CLA

PerchTail is licensed under the [AGPL-3.0](LICENSE). Before a pull request can be merged, its author signs the [Contributor License Agreement](CLA.md), once: it lets the maintainer distribute your contribution under the project's license and under other terms, while you keep the copyright in it. Say so in the pull request if your employer has rights in what you write.

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
locally (`sources-ssh.spec.ts` and `sources-smb.spec.ts`
will fail with nothing listening on those ports, everything else is
unaffected). See `frontend/e2e/` and ROADMAP.md's "Frontend E2E testing
(Playwright)" section for how it's wired together.

`frontend/e2e/accessibility.spec.ts` runs axe-core on each main page and fails on serious or critical WCAG 2.0/2.1 A and AA violations, listing each rule, its impact and the selectors of the offending nodes. A new page, or a new popup on an existing one, gets its own test in that spec: open it, wait for it to finish loading, then check it. Fix what it finds in the component; only a third-party node that can't be fixed by configuration (such as CodeMirror's scroller) is excluded, with a comment saying why.

See [CLAUDE.md](CLAUDE.md) for the full architecture and repo layout — it's kept
up to date as the source of truth for design decisions.

## Conventions

- Type hints everywhere; Pydantic models for API request/response bodies.
- `ruff` + `black` for lint/format — run both before opening a PR.
- Tests via `pytest`. Mock the SSH/SMB/WinRM clients in unit tests; never hit a
  real remote host in CI.
- Two backend tests cover every endpoint at once. `tests/test_every_endpoint_needs_auth.py` calls each operation without signing in and expects 401 or 403, except those in its `PUBLIC_OPERATIONS` list: a new endpoint that must be public goes in that list, with a one-line comment saying why. `tests/test_api_properties.py` runs Schemathesis against the OpenAPI schema as a super admin and fails on any 5xx; operations that would reach a source, agent, webhook, SSO provider or GitHub are excluded, and any other outbound connection fails the test. It runs 25 examples per operation with a fixed seed; set `SCHEMATHESIS_MAX_EXAMPLES` for a longer run by hand.
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
   For a pre-release, `X.Y.Z-beta.N` is an early pre-release taken from `dev` and `X.Y.Z-rc.N` a release candidate; both sort before `X.Y.Z`, and a beta before a release candidate.
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
3. It builds the agent binaries and `SHA256SUMS`, signs `SHA256SUMS` into `SHA256SUMS.sig` with the agent release signing key (below), pushes `ghcr.io/quaresma870/perchtail:X.Y.Z` with the signed agent binaries inside and its SBOM, and creates a draft release with all of them attached and the CHANGELOG section as notes. A tag with a suffix becomes a pre-release.
4. Review the draft (notes, assets), edit the notes if needed, and
   publish it. Publishing a full release moves the image's `X.Y` and
   `latest` tags to it; a pre-release never does.

To fix a draft, push the fix, delete the draft and its tag, and run the
workflow again. Once a release is published, a fix needs a new version.

A fix for a released version that can't wait for the next one goes on a `hotfix/X.Y.Z` branch cut from `main`, which has the same rules as `dev` (changes by PR with CI green, no force push). Run the workflow from that branch ("Use workflow from" and the branch to tag both set to it); the workflow accepts a commit on the hotfix branch it runs from. Once the full release is published, merge the hotfix branch into `main` and `main` back into `dev`.

### Agent release signing key

Push agents upgraded from the portal (#319) install a binary only if `SHA256SUMS` carries a valid Ed25519 signature from the release key, whose public half is built into the agent. The release workflow fails without the private key, and when it doesn't match the public key in the source. The maintainer's public key is already committed as `agent/internal/agent/upgrade_signing_key.pub`; the steps below set up a key pair from scratch, and are the same for a rotation:

1. On a trusted machine, generate the key pair and derive the public key:

   ```sh
   openssl genpkey -algorithm ed25519 -out agent-signing.pem
   openssl pkey -in agent-signing.pem -pubout -out upgrade_signing_key.pub
   ```

2. Replace `agent/internal/agent/upgrade_signing_key.pub` with the new `upgrade_signing_key.pub` (the public key, `-----BEGIN PUBLIC KEY-----`) and merge it through a PR like any other change. Agents built from then on verify upgrades with the new key.
3. Create an environment named `release` (Settings → Environments), limit its deployment branches to `dev`, `main` and `hotfix/*`, and add the environment secret `AGENT_SIGNING_KEY` there with the whole content of `agent-signing.pem`, the private key (`-----BEGIN PRIVATE KEY-----` lines included). An environment secret, not a repository secret: only the release workflow's `agent` job names that environment, so a workflow pushed on another branch can't read the key.
4. Keep `agent-signing.pem` offline (an encrypted backup, a password manager), and never commit it. Losing it means new agents need a new public key and the agents already installed can't verify upgrades signed with another one: they would have to be upgraded by hand once.

Rotating the key works the same way, and has the same cost: agents built with the old public key refuse releases signed with the new one, so each of them is upgraded by hand once.

## Security-sensitive areas

Changes touching credential storage, RBAC/grant resolution, or auth providers get
extra scrutiny by design — see [SECURITY.md](SECURITY.md) for what's in scope and
how to report a vulnerability privately rather than via a public PR or issue.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Participation
means agreeing to keep discussion respectful and on-topic.
