# PerchTail

Architecture brief for contributors and AI coding assistants working in this repository. It describes the code on this branch; when the two disagree, the code wins and this file gets fixed. Operator documentation lives in `README.md` and `docs/`; the backlog lives in `ROADMAP.md` and GitHub issues.

## What PerchTail is

PerchTail is a self-hosted web app for browsing, opening and downloading log files live from many Linux and Windows servers. An admin configures **sources** (a host and how to reach it) and attaches **rules** (which files and folders are visible). Users browse a source's tree, open files in a CodeMirror-based viewer, follow them as they grow, and download single files or folders as a ZIP. Access is controlled by roles scoped to customers, folders and sources.

PerchTail only reads from sources. It never writes, deletes or changes anything on them.

Stack: Python 3.12, FastAPI, SQLModel/SQLAlchemy, Alembic and SQLite on the backend; Svelte 5, TypeScript, Vite and CodeMirror 6 on the frontend; Go for the push agent. Licensed under the AGPL-3.0 (`LICENSE`); contributions are accepted under the CLA (`CLA.md`).

## What it stores and what it doesn't

The SQLite database (`DATA_DIR/perchtail.db` unless `DATABASE_URL` is set) holds:

- configuration: customers, folders, sources and their connection settings, rules, severity patterns, system settings (`system_setting`), the SSO provider and the SSO group-to-role mappings;
- encrypted secrets: source credentials (`Source.credential_ref`), the SSO provider configuration (`SSOProviderConfig.config`) and users' TOTP secrets (`User.mfa_secret_encrypted`);
- hashes only, never the plain value: local passwords, previous passwords and MFA backup codes (argon2id), session tokens, agent enrollment tokens and the monitoring token (SHA-256);
- users, roles and grants;
- the audit log with its HMAC hash chain, and the chain's state (anchor after purges, last verification result);
- the file-name index: path, size and modification time of each rule-visible file, never content (`file_name_entry`, searched through the `file_name_fts` FTS5 table);
- alert state: alerts, how far each alert has read each file (byte offset and a hash of the file's first bytes), and webhook deliveries waiting to be sent or retried. A pending delivery holds the matching lines and is dropped once sent or after an hour (`app/alerts.py`, `RETRY_FOR`);
- job runs (last start, finish, result and error of each scheduled job), agent upgrade runs and the last update-check result.

Next to the database, `DATA_DIR` holds the credential salt (`credential_salt`), the SSH host keys PerchTail has pinned (`ssh_known_hosts`) and the instance lock file (`.perchtail.lock`). `LOG_DIR` holds the app's own logs. `BACKUP_DIR` holds encrypted backups made by `python -m app.backup`.

File content is never kept. Opening or downloading a file always fetches it fresh from the source into an ephemeral scratch directory (`SCRATCH_DIR`), which is purged (see "Live browsing & ephemeral fetch behavior"). The audit log records which paths were opened or downloaded, not their content.

Some state is in memory only and is lost on restart: scratch bookkeeping, live agent connections (`app/agent_registry.py`), login lockouts (`app/login_throttle.py`) and process health (`app/health.py`).

## Components

### Backend (`backend/app/`)

A FastAPI app (`app/main.py`). The lifespan, in order:

1. refuses to start while `CREDENTIAL_ENCRYPTION_KEY` is the shipped default `changeme`;
2. takes an exclusive lock on `DATA_DIR/.perchtail.lock` (`app/instance_lock.py`), so only one process runs per `DATA_DIR`;
3. moves a pre-0.5.0 data layout into `DATA_DIR` (`app/data_layout.py`) and refuses a database migrated by a newer PerchTail (`app/schema_version.py`);
4. creates missing tables and the FTS5 index (`app/db.py`, `init_db`);
5. refuses to generate a new credential salt while encrypted data exists (`app/bootstrap.py`, `assert_credential_salt_present`), backfills the audit hash chain, and records a restore if one is pending;
6. seeds the system log source, the first super admin (only when there are no users; its temporary password is logged once at WARNING), the "No Access" role and the default severity patterns;
7. clears leftovers in `SCRATCH_DIR`, registers the scheduled jobs and starts the scheduler.

On shutdown it stops the scheduler and drains the worker pools within `SHUTDOWN_TIMEOUT_SECONDS` (`app/jobs.py`, `shutdown`).

Routers in `app/api/`, by area: auth (login, logout, profile, password change, MFA, own sessions, SSO login), users, roles, SSO configuration, customers, folders, sources, rules, archive (browse, open, follow, close, download and folder ZIP under `/sources/{id}`), file-name search, alerts, audit log, YAML import and export, system settings, update check, severity patterns, the agent WebSocket and binaries, agent upgrades, and monitoring. `/healthz` and `/readyz` are defined in `app/main.py`. The HTTP API is internal to the frontend and may change before 1.0; don't document it as a public interface.

The built frontend (`frontend/dist`) is served by the backend at `/` when it exists.

Command-line tools run from `backend/`: `python -m alembic upgrade head` (migrations), `python -m app.backup create|verify|restore|list`, `python -m app.rotate_credential_key` and `python -m app.data_layout`.

### Frontend (`frontend/src/`)

A Svelte 5 single-page app in TypeScript, built with Vite. Routing is hash-based (`svelte-spa-router`), so the backend serves it as plain static files. Pages are in `routes/`; shared code and its vitest unit tests (`*.test.ts`) are in `lib/`; components are in `lib/components/`. The viewer uses CodeMirror 6 (`lib/components/CodeMirrorPane.svelte`). In development, `npm run dev` proxies the API paths listed in `vite.config.ts` to a backend on `127.0.0.1:8000`.

### Push agent (`agent/`)

A Go program (module `github.com/quaresma870/perchtail/agent`, one dependency: `gorilla/websocket`) for hosts PerchTail can't reach inbound. It dials out to `/agent/connect` over a WebSocket, authenticates with its source's enrollment token, and answers list, fetch and ranged-read commands for files under `PERCHTAIL_BASE_PATH`. It never pushes files on its own. It refuses a plain `ws://` URL unless `PERCHTAIL_ALLOW_INSECURE=true`. It can be upgraded from Settings → Agents: it installs a new binary only if `SHA256SUMS` carries a valid Ed25519 signature from the release key built into it (`internal/agent/upgrade_signing_key.pub`) and the version is newer than its own. Configuration and service setup: `agent/README.md`.

### Scheduler and jobs

One in-process APScheduler `BackgroundScheduler` (`app/scheduler.py`; coalesce, `misfire_grace_time` 300 s, one instance per job). Every job is added through `app/jobs.py`'s `register`, which gives it a stable id and records each run in `job_run`. Jobs registered with `catch_up=True` run 30 seconds after startup when their interval has passed since their last success.

| Job id | Interval | What it does |
|---|---|---|
| `scratch_idle_sweep` | `SCRATCH_SWEEP_INTERVAL_SECONDS` (60 s) | Deletes scratch entries untouched for `SCRATCH_IDLE_SECONDS` (300 s). |
| `scratch_size_guard` | same | Evicts the oldest unreferenced scratch entries when scratch exceeds `SCRATCH_MAX_GB`. |
| `alert_pass` | 15 s tick | Once the alerts interval (Settings → System, 60 s) has passed, reads what was appended to watched files, queues webhook calls and sends or retries due ones. |
| `name_index_sweep` | 1 min | Queues the sources whose file-name index is older than its interval (15 min) for the index worker pool. |
| `source_checks` | 1 min | Checks the reachability of sources that are due (every 10 min, with backoff after failures) on the source-check worker pool. |
| `agent_upgrades` | 1 min | Ends upgrade attempts that timed out and, inside the window, upgrades agents set to automatic. |
| `audit_purge` | `AUDIT_PURGE_INTERVAL_SECONDS` (1 day), catch-up | Deletes audit rows older than the retention (Settings → System, 365 days; 0 keeps everything). |
| `session_purge` | 1 h, catch-up | Deletes expired sessions. |
| `update_check` | 1 h, catch-up | Asks GitHub for the latest release, at most once a day. Off with `UPDATE_CHECK=false` or the switch in Settings → System. |
| `audit_integrity_check` | `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` (365), catch-up | Verifies the audit hash chain. |
| `backup` | 5 min tick | Makes an encrypted backup once the backup interval (Settings → System) has passed; 0, the default, is off. |
| `wal_checkpoint` | 1 h | Checkpoints and truncates the SQLite WAL (`app/db.py`). |

### Connectors (`backend/app/collectors/`)

`registry.py` maps each `Protocol` to its module; every caller goes through it, and `errors.py` turns each library's exceptions into the `SourceError` types in `base.py`.

- `ssh.py`: SSH/SFTP with `paramiko`. Host keys are pinned in `SSH_KNOWN_HOSTS_PATH`; `SSH_HOST_KEY_POLICY` is `tofu` (pin on first connect) or `strict` (refuse unknown hosts).
- `smb.py`: SMB with `smbprotocol`. `smb_shares.py` lists a server's disk shares for the share picker.
- `winrm.py`: WinRM with `pywinrm`, running scripts in a WinRS shell. When the source names a session configuration (`winrm_configuration`), `winrm_jea.py` speaks PSRP (`pypsrp`) to a JEA endpoint instead and only calls the read-only functions of the reference role in `docs/examples/jea`.
- `local.py`: files on the PerchTail host. Used by the system log source and by admin-created local sources, which must sit under `LOCAL_SOURCE_ROOTS` (empty by default, which disables them). Local files are served in place, without scratch.
- `agent.py`: relays commands to a connected push agent through `app/agent_registry.py`.

A source can read several folders (`Source.roots`, `app/source_roots.py`). Remote reads are bounded by `MAX_FETCH_MB`, `MAX_DECOMPRESSED_MB` and `REMOTE_READ_TIMEOUT_SECONDS`, and concurrent connector work is limited globally and per source, and downloads and folder ZIPs per user (`app/limits.py`); a request over a limit gets 503 with `Retry-After`.

## Rule matching semantics

Implemented in `app/rules.py`.

- Rules decide which files and folders are visible and reachable on a source. A path no rule makes visible can't be listed, opened or downloaded.
- Rules are evaluated in `order`, and the last match wins, as in `.gitignore`.
- Patterns are globs (`**`, `*`, `?`) by default; a `re:` prefix makes a rule a regular expression. Rule regexes are compiled with RE2 (`google-re2`), so they run in linear time; backreferences and lookaround are rejected.
- A source with no rules shows nothing.
- Matching is case-insensitive for SMB and WinRM sources and for agents that report Windows.

## Live browsing & ephemeral fetch behavior

- Browsing lists a folder live through the source's connector, one folder at a time, filtered through the rules. Listings are capped by the `listing_max_entries` setting.
- Opening or downloading always fetches fresh from the source. Nothing is cached or reused between opens.
- `.zip`, `.tar.gz` and `.tgz` files appear as folders; opening a member extracts only that member (`app/archives.py`). A plain `.gz` file is decompressed and opens like any other file.
- Fetched files go to `SCRATCH_DIR`, tracked in memory by `app/scratch.py`'s `ScratchStore`. Each open gets its own file, reference-counted by key. A file is deleted when the viewer closes it and no other viewer holds it, when the idle sweep finds it untouched past `SCRATCH_IDLE_SECONDS`, or when the size guard evicts it (oldest unreferenced first). A new fetch is refused with 507 when scratch is full or its filesystem is below `SCRATCH_MIN_FREE_GB` / `SCRATCH_MIN_FREE_PERCENT`. Scratch is emptied at startup.
- Follow (tail) polls with ranged reads of what was appended and detects rotation by the file's first bytes; it doesn't fetch the whole file again.

## Access control (RBAC + SSO)

Implemented in `app/auth/rbac.py`.

- Each user has exactly one role (`User.role_id`).
- A role has global capabilities: `manage_users`, `manage_roles`, `manage_sso`, `create_source`, `manage_system_settings`, `view_audit_log` and `manage_alerts`.
- A role's grants (`RoleGrant`) give scoped capabilities on a customer, a folder or a source: `view`, `download`, `manage_rules`, `run_now` and `manage_source`. `manage_rules` covers only the visibility rules; `manage_source` covers editing, moving and deleting sources and folders, agent tokens, saved credentials and agent upgrades, and, together with `create_source`, adding sources and folders. `run_now` starts a file-name index pass.
- A grant applies to everything below its scope, including sources added later. The most specific grant wins: source, then the nearest folder up the tree, then the customer. With no grant, access is denied.
- A super-admin role bypasses grants. The system log source (`is_system`) is reachable by super admins only.
- Delegated admins can only grant or assign what they hold themselves (`grant_within_authority`, `role_within_authority`).
- Sign-in is local (username and password) or SSO. SSO is OIDC (`app/auth/providers/oidc.py`: `joserfc` verifies the ID token against the provider's JWKS, `httpx` does the code exchange), with one provider enabled at a time. SSO users are provisioned on first login. Group-to-role mappings (`SSOGroupRoleMapping`, last match wins) set their role on every login when one matches; with no matching mapping, a new user gets the "No Access" role and an existing user keeps the role they have. The `saml` enum values in `app/auth/models.py` have no provider behind them.

## Security notes

- **Sessions** (`app/auth/sessions.py`, `app/api/auth.py`): a random token (`secrets.token_urlsafe(32)`) whose SHA-256 hash is stored in `AuthSession`. The `perchtail_session` cookie is `HttpOnly`, `SameSite=Strict`, `Secure` unless `SESSION_COOKIE_SECURE=false`, and lasts `SESSION_TTL_HOURS` (12). Expiry is also checked server-side, a deactivated user's sessions stop working, logout deletes the session, and a password change or admin reset ends the user's other sessions. Users can list and end their own sessions from My profile. The SSO flow uses a separate `perchtail_sso_state` cookie (`HttpOnly`, `SameSite=Lax`, 10 minutes) to bind the OAuth state to the browser.
- **CSRF**: `SameSite=Strict` on the session cookie, plus `OriginCheckMiddleware` in `app/main.py`: on POST, PUT, PATCH and DELETE, an `Origin` (or `Referer`) header whose host and port differ from the request's `Host` gets 403. A request with neither header relies on SameSite alone.
- **Headers** (`SecurityHeadersMiddleware`): a strict Content-Security-Policy (`script-src 'self'`; `style-src` also allows `'unsafe-inline'` because CodeMirror injects styles at runtime; `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`. HSTS is left to the TLS-terminating reverse proxy. Every response carries an `x-request-id`.
- **Credential encryption** (`app/crypto.py`): Fernet, with the key derived by PBKDF2-HMAC-SHA256 (600,000 iterations) from `CREDENTIAL_ENCRYPTION_KEY` and a random per-install salt stored at `CREDENTIAL_SALT_PATH` (default `DATA_DIR/credential_salt`, mode 0600). Losing the salt makes every stored secret unreadable. `python -m app.rotate_credential_key` re-encrypts everything under a new key and re-signs the audit chain in one transaction.
- **Audit log** (`app/audit.py`, `app/audit_hash_chain.py`, `app/audit_integrity.py`): every row carries an HMAC-SHA256 over its content and the previous row's hash. The HMAC key is derived from the same secret and salt, separately from the Fernet key, so the chain can't be recomputed from the database alone. Every audit write also emits an INFO log line. Failed logins and lockouts are audited, with repeats aggregated (`app/auth_events.py`).
- **Login throttle** (`app/login_throttle.py`): per username, in memory; `LOGIN_MAX_ATTEMPTS` (5) failures lock the account for `LOGIN_LOCKOUT_SECONDS` (300). Failed logins for unknown accounts cost the same argon2 work as real ones.
- **MFA** (`app/auth/mfa.py`): optional TOTP for local accounts, with 10 one-time backup codes and replay protection (`mfa_last_used_step`). SSO accounts rely on the identity provider.
- **Password policy** (`app/password_policy.py`, Settings → System): minimum and maximum length (12 and 128 by default), rejection of simple and personal passwords, and optional history, character classes and maximum age. Passwords are hashed with argon2id. Admin-created local accounts must change their password at first login.
- **Monitoring token** (`app/monitoring_access.py`, `MonitoringToken`): one deployment-wide bearer token, stored as a SHA-256 hash. `/monitoring/health` and `/metrics` always need it, plus an address in `MONITORING_ALLOWED_NETWORKS` when that is set. `/healthz` and `/readyz` answer a loopback caller without proxy headers, a caller in `PROBE_ALLOWED_NETWORKS`, or a caller with the token.
- **Agent token**: one enrollment token per agent source, shown once and stored as a SHA-256 hash (`Source.agent_token_hash`). The agent sends it as a bearer token on the WebSocket handshake and when downloading binaries.
- **Other guards**: alert webhook URLs must resolve to public addresses, checked again at send time (`app/webhook_safety.py`); archive extraction and fetches are size-limited; paths are checked for containment in every connector and in the agent (`internal/agent/pathsafety.go`).

## Application logging

`app/logging_config.py`: `structlog` with JSON output, to stdout and to `LOG_DIR/perchtail.log`, at `LOG_LEVEL` (default `INFO`). A `request_id` is bound per HTTP request. The file rotates at `LOG_MAX_FILE_MB` or at midnight, rotated files are gzipped, and they are deleted after `LOG_RETENTION_DAYS` or when all logs exceed `LOG_MAX_TOTAL_MB` (`app/log_rotation.py`). Never log credentials, tokens or request bodies.

## Built-in log viewer

The app's own logs are a system source, seeded at startup (`app/bootstrap.py`, `seed_system_log_source`): protocol `local`, `is_system = true`, no customer, base path `LOG_DIR`. Only super admins can open it. It can't be edited or deleted, and its rules can't be changed.

## Web UI

Main areas: the sources explorer with the file-name search, the viewer (tabs, find, follow, compare, download), alerts, My profile (display name, email, password, MFA, sessions), and Settings: sources, folders, users, roles, SSO, system, severity indicators, audit log, agents, and import/export.

## Data model

Core tables (`backend/app/models.py`):

- `Customer`: top-level grouping; every non-system source and every folder belongs to one.
- `Folder`: nested grouping of a customer's sources, for browsing and grant scopes.
- `Source`: a host and how to reach it (protocol, host, port, base path or roots, encrypted credential, WinRM and TLS options), plus agent, reachability and file-name index state.
- `Rule`: an ordered include or exclude pattern on a source.
- `SeverityPattern`: a line-highlighting pattern for the viewer, global or per source; matching runs in the browser.
- `FileNameEntry`: one rule-visible file of a source in the file-name index (path, size, modification time).
- `Alert`: a query on the files in a scope, with a webhook URL and its owner.
- `AlertFileState`: how far an alert has read a file.
- `AlertDelivery`: a webhook call waiting to be sent or retried.
- `UpdateCheckState`: the last release-check result (single row).
- `JobRun`: the last run of each scheduled job.
- `AgentUpgradeRun`: one attempt to upgrade an agent, from request to outcome.
- `SystemSetting`: key-value settings from Settings → System; defaults live in code (`app/system_settings.py`).
- `MonitoringToken`: the hashed monitoring token (single row).

Auth tables (`backend/app/auth/models.py`):

- `Role`: name, built-in and super-admin flags, global capabilities.
- `RoleGrant`: scoped capabilities of a role on a customer, folder or source.
- `User`: username, profile, preferences, role, provider and SSO identity, password and MFA state.
- `MfaBackupCode`: a hashed one-time recovery code.
- `PasswordHistory`: a previous password hash, kept only while the history rule needs it.
- `SSOProviderConfig`: the OIDC provider, configuration encrypted.
- `SSOGroupRoleMapping`: an ordered identity-provider group to role mapping.
- `AuthSession`: a hashed session token with expiry, last use and user agent.
- `AuditLog`: who did what to which target, with metadata and hash-chain fields.
- `AuditChainState`: the hash chain's anchor and last verification result (single row).

Tables that other rows point at by id use `NO_ID_REUSE` (`sqlite_autoincrement`), so a deleted id is never reused. Timestamps are naive UTC (`app/timeutils.py`, `utcnow`). The schema is managed by Alembic (`backend/migrations/`); a schema change needs a migration.

## Repository structure

```
.github/
  workflows/          ci.yml, release.yml, security-scan.yml, codeql.yml
  dependabot.yml, FUNDING.yml
agent/                Go push agent
  main.go
  internal/agent/     client, handler, config, path safety, upgrade, protocol, version
  README.md           configuration and service setup
agent-dist/           release agent binaries, copied into the Docker image (empty in git)
backend/
  app/
    main.py           app, middleware, lifespan, job registration
    config.py         settings from the environment and .env
    db.py             engine, SQLite pragmas, WAL checkpoint
    models.py         core tables
    auth/             models.py, rbac.py, sessions.py, mfa.py, providers/ (local.py, oidc.py)
    api/              routers by area
    collectors/       connectors and their registry
    testing/          fake WinRM connector for the e2e suite
    jobs.py, scheduler.py
    rules.py, scratch.py, archives.py, source_roots.py, limits.py
    alerts.py, name_index.py, name_index_store.py, source_status.py
    audit.py, audit_hash_chain.py, audit_integrity.py, audit_purge.py, auth_events.py
    crypto.py, rotate_credential_key.py, backup.py, config_transfer.py
    agent_registry.py, agent_upgrades.py, agent_upgrade_policy.py
    monitoring_access.py, health.py, metrics.py, update_check.py
  migrations/         Alembic env.py and versions/
  scripts/            run_e2e_server.sh, setup_e2e_test_servers.sh
  tests/              pytest suite
  alembic.ini, docker-entrypoint.sh, requirements.in, requirements.txt, requirements-dev.in, requirements-dev.txt (pinned with hashes)
docs/                 operator guides; examples/ (JEA role, Zabbix templates, bulk import); images/
frontend/
  src/                App.svelte, main.ts, routes/, lib/, lib/components/
  e2e/                Playwright specs
  vite.config.ts, playwright.config.ts, package.json
Dockerfile, docker-compose.yml, .env.example
pyproject.toml        ruff, black and pytest configuration
README.md, CHANGELOG.md, ROADMAP.md, CONTRIBUTING.md, SECURITY.md, CLA.md, CODE_OF_CONDUCT.md, TRADEMARKS.md, LICENSE
```

## Deployment shape

The Docker image builds the frontend with Node 22 and runs the backend on `python:3.12-slim` as an unprivileged user (uid 10001). `backend/docker-entrypoint.sh` runs `python -m app.data_layout`, `python -m alembic upgrade head` and `uvicorn app.main:app --host 0.0.0.0 --port 8000`. `docker-compose.yml` publishes port 8080 and keeps state in the `/data` volume. PerchTail is meant to sit behind a TLS-terminating reverse proxy. It runs as a single process per `DATA_DIR`; SQLite runs in WAL mode, so `DATA_DIR` must be on a local disk.

## How to test

CI (`.github/workflows/ci.yml`) runs these on every push and pull request:

- Backend, from the repository root: `ruff check backend`, `black --check backend`, `python -m alembic upgrade head` (from `backend/`), `pytest -v`, then `pip-audit` and `bandit -q -r backend/app`.
- Frontend, from `frontend/`: `npm run check` (svelte-check and the TypeScript projects), `npm test` (vitest), `npm run build`, `npm audit --audit-level=high`.
- End-to-end, from `frontend/`: `npm run test:e2e` (Playwright). It starts its own backend on port 8001 with a throwaway database and real local sshd and smbd test servers; WinRM uses the fake in `app/testing/fake_winrm.py`. `e2e/accessibility.spec.ts` runs axe-core on every main page.
- Agent, from `agent/`: `go vet ./...`, `gofmt -l .` (must print nothing), `go test ./... -v`, 30 seconds of fuzzing for each of `FuzzIsSafeRelativePath` and `FuzzFetchStaysInBase`, cross-compilation for linux/amd64, linux/arm64 and windows/amd64, `gosec` and `govulncheck`.
- The Docker image is built and scanned with Trivy, and the workflows are audited with zizmor.

Two backend tests cover every endpoint: `tests/test_every_endpoint_needs_auth.py` (each operation needs authentication unless it is listed in `PUBLIC_OPERATIONS`) and `tests/test_api_properties.py` (Schemathesis, no 5xx, no outbound connections).

## Conventions

- Python: type hints everywhere; Pydantic models for API request and response bodies; `ruff` and `black` with a line length of 100 (`pyproject.toml`).
- Unit tests mock the SSH, SMB, WinRM and agent clients; they never reach a real remote host.
- A new endpoint requires authentication, or goes into `PUBLIC_OPERATIONS` with a comment saying why.
- Regular expressions that come from users and run on the backend are compiled with RE2 (`app/rules.py`, `compile_linear`).
- `record_audit_event` doesn't commit: the caller commits the change and its audit row in one transaction.
- Store timestamps with `app.timeutils.utcnow()`.
- A new page or popup gets a test in `frontend/e2e/accessibility.spec.ts`.
- Commits follow Conventional Commits, with a scope where it helps (`feat(rbac): ...`, `fix: ...`, `docs: ...`, `ci(release): ...`). Pull requests target `dev`.
- User-visible changes get an entry under `[Unreleased]` in `CHANGELOG.md`.
- Markdown: one paragraph or list item per line, no hard wraps.
