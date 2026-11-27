# Contributing to PerchTail

Thanks for considering it. Contributions of any size help: a typo fix, a bug report, a test, a connector.

## License and CLA

PerchTail is licensed under the [AGPL-3.0](LICENSE). Before a pull request can be merged, its author signs the [Contributor License Agreement](CLA.md), once: it lets the maintainer distribute your contribution under the project's license and under other terms, while you keep the copyright in it. Say so in the pull request if your employer has rights in what you write.

## Before you start

For anything beyond a small fix, please open an issue or discussion first. This project touches production credentials and RBAC, so design changes in those areas need a conversation before a PR, not after.

[CLAUDE.md](CLAUDE.md) is a short architecture brief: what PerchTail stores, its components and scheduled jobs, the data model, the security design and the repository layout. Read it before a larger change. If your change makes a statement in it untrue, update it in the same pull request.

## Development setup

You need Python 3.12, Node.js 22 and, for the push agent, Go (see "Push agent (Go)" below). The commands below are for a Linux or macOS shell.

### Backend (FastAPI)

Set up a virtual environment in `backend/` and install the development dependencies:

```bash
git clone https://github.com/quaresma870/perchtail.git
cd perchtail/backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

The backend reads its settings from environment variables and from a `.env` file in the directory it starts in. Create one from the example:

```bash
cp ../.env.example .env
```

In `backend/.env`, set `CREDENTIAL_ENCRYPTION_KEY` to a random value (for example the output of `python -c "import secrets; print(secrets.token_urlsafe(32))"`): the backend refuses to start with the default `changeme`. For plain HTTP on your machine, also set `SESSION_COOKIE_SECURE=false`. Everything else can keep its default. The database, logs and scratch files then go to `backend/data/` (`DATA_DIR`, `LOG_DIR` and `SCRATCH_DIR`), which git ignores.

Apply the database migrations, then start the backend, both from `backend/` (the Docker image runs the same two commands in `backend/docker-entrypoint.sh`):

```bash
python -m alembic upgrade head
uvicorn app.main:app --port 8000
```

On the first start with an empty database, the backend creates the `admin` super admin with a temporary password in `DATA_DIR/initial-admin-password` (`backend/data/db/initial-admin-password` with the defaults); the `initial_super_admin.seeded` warning names the file. Sign in with it, choose a new password, and set up two-factor authentication, which super admins must use.

Run the checks CI runs from the repository root:

```bash
cd ..             # the repository root
ruff check backend
black --check backend
pytest
ruff check loadtest                            # the load-test kit
python -m unittest discover -s loadtest/tests
```

A change to the models needs an Alembic migration. Generate one from `backend/`, then review and edit it before committing:

```bash
python -m alembic revision --autogenerate -m "short description"
```

A migration must be safe to run again after an interruption: SQLite doesn't roll back schema changes, so a stopped upgrade can leave part of its work done. Before an `op.batch_alter_table` on a table, deal with the `_alembic_tmp_<table>` that an interrupted rebuild leaves behind, and never drop it without checking: while `<table>` exists, the temporary table is a leftover and is dropped; when only the temporary table exists, the rebuild stopped after dropping `<table>` and the temporary table holds the only copy of the rows, so rename it back to `<table>` and log a warning; when neither exists, stop with a message pointing at the `pre-upgrade` backup (the `_recover_interrupted_rebuild` helper does all three); create a table, or add a column, only if it isn't there yet; and when the rebuilt table uses `NO_ID_REUSE`, pass `table_kwargs={"sqlite_autoincrement": True}` and put its `sqlite_sequence` value back afterwards. `c7f2a9e4d136` and `e5c1b7a93d24` show how, and `backend/tests/test_migrations_keep_autoincrement.py` runs them over each of the three cases.

The revisions before the PostgreSQL baseline `c4e7a1d9b302` run on SQLite only (`sqlite_only()` from `backend/migrations/engines.py`); every revision after it runs on both SQLite and PostgreSQL. Test a new one on both: `pytest` runs on SQLite, and with `TEST_DATABASE_URL` set to an empty PostgreSQL 18 database (`postgresql://...`) the same suite, including `tests/test_migrations_postgresql.py` and the tests marked `postgres_only`, runs on PostgreSQL.

Python dependencies: `backend/requirements.in` lists what the backend needs, with version ranges; `backend/requirements.txt` pins every package (transitive ones included) and is what the Docker image and a from-source install use. Every pin carries its hashes, and the Docker image installs with `pip --require-hashes`. The development and test tools are in `backend/requirements-dev.in`, compiled the same way into `requirements-dev.txt` on top of the backend's pins. To add or upgrade a package, edit the `.in` file, then regenerate both files with [uv](https://docs.astral.sh/uv/) and commit all of them:

```bash
cd backend
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 --generate-hashes -o requirements-dev.txt
```

To bump everything within the ranges, add `--upgrade` to the first command (the second follows the first's pins).

### Frontend (Vite + Svelte)

In a separate terminal, from the repository root:

```bash
cd frontend
npm ci
npm run dev      # dev server, proxies API calls to the backend on 127.0.0.1:8000
npm run check    # svelte-check and the TypeScript projects
npm test         # vitest
```

`npm run dev` prints the address to open. It forwards the API paths listed in `frontend/vite.config.ts` to the backend started above.

### End-to-end tests (Playwright)

From `frontend/`:

```bash
npx playwright install --with-deps chromium   # one-time, per machine
npm run test:e2e
```

This builds the frontend and starts its own isolated backend on port 8001 with a throwaway SQLite DB (`backend/scripts/run_e2e_server.sh`), so it won't touch a backend you already have running on 8000. The script activates `backend/.venv` when it exists. It also provisions real, throwaway sshd/smbd test servers on ports 2222/1445 (`backend/scripts/setup_e2e_test_servers.sh`, plain `apt-get install openssh-server samba`) so the ssh/smb source specs run against a real protocol implementation; the first run may prompt for `sudo` if those aren't already installed. Set `SKIP_E2E_TEST_SERVERS=1` to skip that step for a quick run of everything else without `sudo` or those packages (`sources-ssh.spec.ts`, `sources-smb.spec.ts` and `import-export.spec.ts`, which reads the fixture paths those servers write, will then fail; everything else is unaffected). See `frontend/e2e/` and the testing notes in [docs/design-decisions.md](docs/design-decisions.md) for how it's wired together.

`frontend/e2e/accessibility.spec.ts` runs axe-core on each main page and fails on serious or critical WCAG 2.0/2.1 A and AA violations, listing each rule, its impact and the selectors of the offending nodes. A new page, or a new popup on an existing one, gets its own test in that spec: open it, wait for it to finish loading, then check it. Fix what it finds in the component; only a third-party node that can't be fixed by configuration (such as CodeMirror's scroller) is excluded, with a comment saying why.

#### The README's screenshots

`frontend/e2e/readme-screenshots.spec.ts` remakes the images in `docs/images/` from demo data it creates (fictional customers and hosts) and deletes again. It doesn't run in the normal suite: it is skipped unless `README_SCREENSHOTS` is set. From `frontend/`, with the e2e backend and its sshd/smbd test servers:

```bash
README_SCREENSHOTS=1 npx playwright test e2e/readme-screenshots.spec.ts
```

The SSH and SMB sources point at the test servers by address; with `README_HOST_NAMES=1` they use their own names (`web-01`, `web-02`, `files-01`, `files-02`, `edge-01`) as hosts instead, for a backend whose machine resolves those names to the test servers (in its `/etc/hosts`). Optimize the PNGs afterwards (for example `optipng -o5 -strip all docs/images/*.png`) and check each one before committing it.

### Push agent (Go)

The agent in `agent/` is built with Go 1.27 (`go 1.27.0` in `agent/go.mod`; CI uses the latest 1.27 release). Run the checks CI runs from `agent/`:

```bash
cd agent
go vet ./...
gofmt -l .        # must print nothing; fix with gofmt -w .
go test ./...
go test ./internal/agent -run '^$' -fuzz '^FuzzIsSafeRelativePath$' -fuzztime 30s
go test ./internal/agent -run '^$' -fuzz '^FuzzFetchStaysInBase$' -fuzztime 30s
for target in FuzzRedactBlockCuts FuzzRedactUTF16 FuzzRedactWindow FuzzCustomRule; do
  go test ./internal/redact -run '^$' -fuzz "^${target}\$" -fuzztime 30s
done
```

`go test` runs only the fuzz targets' seed corpus; the fuzz runs explore new inputs for the path checks in `internal/agent/fuzz_test.go` and for the redaction engine in `internal/redact/fuzz_test.go`. CI also cross-compiles for linux/amd64, linux/arm64 and windows/amd64, and runs `gosec` (v2.30.0) and `govulncheck`:

```bash
go install github.com/securego/gosec/v2/cmd/gosec@v2.30.0
"$(go env GOPATH)/bin/gosec" -quiet ./...
go install golang.org/x/vuln/cmd/govulncheck@latest
"$(go env GOPATH)/bin/govulncheck" ./...
```

A plain `go build` produces an agent that reports its version as `dev`: the server doesn't compare it, and the agent refuses upgrades from the portal. To build with a version, set `internal/agent.Version` the way `.github/workflows/release.yml` does:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -ldflags "-s -w -X github.com/quaresma870/perchtail/agent/internal/agent.Version=X.Y.Z" \
  -o dist/perchtail-agent-X.Y.Z-linux-amd64 .
```

The version must be `X.Y.Z`, `X.Y.Z-rc.N` or `X.Y.Z-beta.N` (`internal/agent/version.go`); anything else counts as unknown. Configuration and service setup are in `agent/README.md`.

## Conventions

- Type hints everywhere; Pydantic models for API request/response bodies.
- `ruff` + `black` for lint/format; run both before opening a PR.
- Tests via `pytest`. Unit tests mock the SSH/SMB/WinRM and agent clients and never reach a real remote host; the e2e suite uses the throwaway local test servers described above.
- Two backend tests cover every endpoint at once. `tests/test_every_endpoint_needs_auth.py` calls each operation without signing in and expects 401 or 403, except those in its `PUBLIC_OPERATIONS` list: a new endpoint that must be public goes in that list, with a one-line comment saying why. `tests/test_api_properties.py` runs Schemathesis against the OpenAPI schema as a super admin and fails on any 5xx; operations that would reach a source, agent, webhook, SSO provider or GitHub are excluded, and any other outbound connection fails the test. It runs 25 examples per operation with a fixed seed; set `SCHEMATHESIS_MAX_EXAMPLES` for a longer run by hand.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`), with a scope where it helps (`feat(rbac): ...`).
- Markdown: one paragraph or list item per line, no hard wraps.
- Frontend dependency advisories: CI fails on any high or critical `npm audit` advisory, and the daily security scan on any at all, through `.github/scripts/npm_audit.py`. An advisory can be accepted in `.github/npm-audit-exceptions.json` only when it has no fixed version and doesn't reach what PerchTail ships, with the reason and an expiry date at most about a month away. CI fails again once the date passes, and once the advisory is gone, so the exception is removed when Dependabot brings the fix.
- Image advisories: the daily security scan runs Grype on the published image and fails on any advisory with a fix, except those listed in `.grype.yaml`, each with its reason and a "Review by" date; `.github/scripts/grype_exceptions.py` fails the scan once a date passes. List only an advisory with no fix for the Python line the image uses, or in a part of the image PerchTail never runs.

## Pull requests

1. Fork, branch from `dev` (the integration branch everything lands on between releases; `main` only moves forward at a tagged release), keep the PR focused on one change, and open it against `dev`.
2. Add or update tests for anything behavior-affecting.
3. Add an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) in the same pull request, for anything a user or operator would notice. The changelog is written by hand; follow the sections already there (`Upgrading`, `Added`, and so on).
4. Describe what changed and why in the PR description, and link the issue if there is one.

## Releasing (maintainers)

Releases are immutable once published: a published release accepts no new assets and its tag can't be reused. `.github/workflows/release.yml` therefore prepares a **draft**, and a maintainer publishes it.

1. Merge a PR into `dev` that sets the version in `backend/app/version.py`, `frontend/package.json` (`cd frontend && npm version --no-git-tag-version X.Y.Z`) and `charts/perchtail/Chart.yaml` (both `version` and `appVersion`, so the chart installs that release's image) and adds a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`. For a pre-release, `X.Y.Z-beta.N` is an early pre-release taken from `dev` and `X.Y.Z-rc.N` a release candidate; both sort before `X.Y.Z`, and a beta before a release candidate.
2. Start the Release workflow, either way:
   - Push the tag: `git tag vX.Y.Z <commit> && git push origin vX.Y.Z`.
   - Or Actions → Release → Run workflow, choosing `dev` under "Use workflow from", with the tag (`vX.Y.Z`) and the branch or commit to tag (default `dev`). The workflow creates the tag. GitHub only shows this button once `release.yml` is on the default branch (`main`); until then, push the tag, or dispatch the workflow through the API with `ref: dev`.

   It refuses a tag whose code carries another version or has no CHANGELOG section, and a tag whose release is already published.
3. It packages the Helm chart (`perchtail-X.Y.Z.tgz`), builds the agent binaries and `SHA256SUMS` (which lists the chart too), signs `SHA256SUMS` into `SHA256SUMS.sig` with the agent release signing key (below), pushes `ghcr.io/quaresma870/perchtail:X.Y.Z` with the signed agent binaries inside and its SBOM, then pushes the chart to `oci://ghcr.io/quaresma870/charts/perchtail` with version `X.Y.Z`, each with GitHub build provenance, and creates a draft release with all of them attached and the CHANGELOG section as notes. A tag with a suffix becomes a pre-release; its chart is published too, with that version, and Helm installs it only when `--version` names it. The tag must also match `version` and `appVersion` in `charts/perchtail/Chart.yaml`; a commit without the chart (a hotfix for a line before 1.0.0) is released without one.
4. Review the draft (notes, assets), edit the notes if needed, and publish it. Publishing a full release moves the image's `X.Y` and `latest` tags to it; a pre-release never does. The chart has only its version tag. The first time the chart is pushed, its package (`charts/perchtail` under the repository owner's packages on GitHub) is private: make it public in the package's settings, as the image's is, or nobody can install it.
5. Once a full release is published, set `PREVIOUS_RELEASE_IMAGE` in `.github/workflows/ci.yml` to its image pinned by digest (`docker buildx imagetools inspect ghcr.io/quaresma870/perchtail:X.Y.Z` prints it), in a PR into `dev`: CI's upgrade test then upgrades from that release.

To fix a draft, push the fix, delete the draft and its tag, and run the workflow again. Once a release is published, a fix needs a new version.

A fix for a released version that can't wait for the next one goes on a `hotfix/X.Y.Z` branch, which has the same rules as `dev` (changes by PR with CI green, no force push). Run the workflow from that branch ("Use workflow from" and the branch to tag both set to it); the workflow accepts a commit on the hotfix branch it runs from. For the current line, cut the branch from `main`; once the full release is published, merge it into `main` and `main` back into `dev`. For an older line (for example 0.6.2 once 0.7.0 is out), cut the branch from that line's last release tag and never merge it into `main`; bring the fix to `dev` in its own pull request if `dev` needs it. Such a release moves its own `X.Y` image tag but not `latest`; when publishing it, untick "Set as the latest release" so the newest version stays GitHub's latest release, which the portal's update check reads.

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

Changes touching credential storage, RBAC/grant resolution, or auth providers get extra scrutiny by design. See [SECURITY.md](SECURITY.md) for what's in scope and how to report a vulnerability privately rather than via a public PR or issue.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Participation means agreeing to keep discussion respectful and on-topic.
