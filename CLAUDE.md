# PerchTail

Architecture brief for contributors working in this repository. It describes the code on this branch; when the two disagree, the code wins and this file gets fixed. Operator documentation lives in `README.md` and `docs/`; the backlog lives in `ROADMAP.md` and GitHub issues.

## What PerchTail is

PerchTail is a self-hosted web app for browsing, opening and downloading log files live from many Linux and Windows servers. An admin configures **sources** (a host and how to reach it) and attaches **rules** (which files and folders are visible). Users browse a source's tree, open files in a CodeMirror-based viewer, follow them as they grow, and download single files or folders as a ZIP. Access is controlled by roles scoped to customers, folders and sources.

PerchTail only reads from sources. It never writes, deletes or changes anything on them.

Stack: Python 3.12, FastAPI, SQLModel/SQLAlchemy, Alembic and SQLite on the backend; Svelte 5, TypeScript, Vite and CodeMirror 6 on the frontend; Go for the push agent. Licensed under the AGPL-3.0 (`LICENSE`); contributions are accepted under the CLA (`CLA.md`).

## What it stores and what it doesn't

The SQLite database (`DATA_DIR/perchtail.db` unless `DATABASE_URL` is set) holds:

- configuration: customers, folders, sources and their connection settings, rules, severity patterns, redaction rules, system settings (`system_setting`), the system SMTP server (`smtp_settings`), the SSO provider and the SSO group-to-role mappings;
- encrypted secrets: source credentials (`Source.credential_ref`), the SSO provider configuration (`SSOProviderConfig.config`), users' TOTP secrets (`User.mfa_secret_encrypted`), the SMTP password (`SmtpSettings.password_encrypted`) and alert webhook URLs (`Alert.webhook_url_encrypted`, never returned by the API);
- hashes only, never the plain value: local passwords, previous passwords and MFA backup codes (argon2id), session tokens, password-reset tokens, agent enrollment tokens and the monitoring token (SHA-256);
- users, roles and grants;
- the audit log with its HMAC hash chain, the chain's state (anchor after purges, last verification result), and the audit outbox: events a migration recorded without the chain key, signed into the chain by the next start (`app/audit_outbox.py`);
- the file-name index: path, size and modification time of each rule-visible file, never content (`file_name_entry`, searched through the `file_name_fts` FTS5 table);
- alert state: alerts, how far each alert has read each file (byte offset and a hash of the file's first bytes), and webhook deliveries waiting to be sent or retried. A pending delivery holds the matching lines and is dropped once sent or after an hour (`app/alerts.py`, `RETRY_FOR`);
- job runs (last start, finish, result and error of each scheduled job), agent upgrade runs and the last update-check result.

Next to the database, `DATA_DIR` holds the credential salt (`credential_salt`), the SSH host keys PerchTail has pinned (`ssh_known_hosts`), the instance lock file (`.perchtail.lock`), the marker saying the audit chain has started (`.audit-chain-started`, `app/audit_hash_chain.py`) and, on a fresh install until it is changed, the first super admin's temporary password (`initial-admin-password`). `LOG_DIR` holds the app's own logs. `BACKUP_DIR` holds encrypted backups made by `python -m app.backup`.

File content is never kept. Opening or downloading a file always fetches it fresh from the source into an ephemeral scratch directory (`SCRATCH_DIR`), which is purged (see "Live browsing & ephemeral fetch behavior"). The audit log records which paths were opened or downloaded, not their content.

Some state is in memory only and is lost on restart: scratch bookkeeping, live agent connections (`app/agent_registry.py`), connector and transfer slots (`app/limits.py`), login lockouts and SSO sign-in starts (`app/login_throttle.py`), password-recovery and webhook-test rate limits (`app/password_reset_limits.py`, `app/api/alerts.py`), which Tails were already audited (`app/api/archive.py`, `_FollowAudits`) and process health (`app/health.py`).

## Components

### Backend (`backend/app/`)

A FastAPI app (`app/main.py`). The lifespan, in order:

1. refuses to start while `CREDENTIAL_ENCRYPTION_KEY` is the shipped default `changeme`, or is weak with `CREDENTIAL_KEY_REFUSE_WEAK=true` (otherwise a weak key is only a warning);
2. takes an exclusive lock on `DATA_DIR/.perchtail.lock` (`app/instance_lock.py`), so only one process runs per `DATA_DIR`;
3. moves a pre-0.5.0 data layout into `DATA_DIR` (`app/data_layout.py`) and refuses a database migrated by a newer PerchTail (`app/schema_version.py`);
4. creates missing tables and the FTS5 index (`app/db.py`, `init_db`);
5. refuses to generate a new credential salt while encrypted data exists (`app/bootstrap.py`, `assert_credential_salt_present`), backfills the audit hash chain, signs into it the anchor of a purge made before 0.7.0, records the events waiting in the audit outbox, and records a restore if one is pending;
6. seeds the system log source (moving it to a changed `LOG_DIR`), the first super admin (only when there are no users; its temporary password goes to `DATA_DIR/initial-admin-password`, mode 0600, deleted when it is changed, and the log only names the file), the "No Access" role, the default severity patterns and the built-in redaction rules (off), binds older SSO accounts to their issuer, and writes the audit chain marker once the chain has started;
7. clears leftovers in `SCRATCH_DIR`, registers the scheduled jobs and starts the scheduler.

On shutdown it stops the scheduler and drains the worker pools within `SHUTDOWN_TIMEOUT_SECONDS` (`app/jobs.py`, `shutdown`).

Routers in `app/api/`, by area: auth (login, logout, profile, password change, password recovery by email, MFA, own sessions, SSO login), users, roles, SSO configuration, the system SMTP server, customers, folders, sources, rules, archive (browse, open, follow, close, download and folder ZIP under `/sources/{id}`), file-name search, alerts, audit log, YAML import and export, the access report, system settings, update check, severity patterns, redaction rules, the agent WebSocket and binaries, agent upgrades, and monitoring. `/healthz` and `/readyz` are defined in `app/main.py`. The HTTP API is internal to the frontend and may change before 1.0; don't document it as a public interface.

The built frontend (`frontend/dist`) is served by the backend at `/` when it exists.

Command-line tools run from `backend/`: `python -m alembic upgrade head` (migrations), `python -m app.backup create|verify|restore|list`, `python -m app.rotate_credential_key` and `python -m app.data_layout`.

### Frontend (`frontend/src/`)

A Svelte 5 single-page app in TypeScript, built with Vite. Routing is hash-based (`svelte-spa-router`), so the backend serves it as plain static files. Pages are in `routes/`; shared code and its vitest unit tests (`*.test.ts`) are in `lib/`; components are in `lib/components/`. The viewer uses CodeMirror 6 (`lib/components/CodeMirrorPane.svelte`). In development, `npm run dev` proxies the API paths listed in `vite.config.ts` to a backend on `127.0.0.1:8000`.

### Push agent (`agent/`)

A Go program (module `github.com/quaresma870/perchtail/agent`, one dependency: `gorilla/websocket`) for hosts PerchTail can't reach inbound. It dials out to `/agent/connect` over a WebSocket, authenticates with its source's enrollment token, and answers list, fetch, ranged-read and archive (`list_archive`, `fetch_archive`) commands for files under `PERCHTAIL_BASE_PATH`, listing at most 50,000 entries of a folder. Its redaction engine (`internal/redact`) is a Go port of the server's (`app/redaction/`), and both are checked against the same test vectors (`agent/internal/redact/testdata/vectors.json`). It never pushes files on its own. It refuses a plain `ws://` URL unless `PERCHTAIL_ALLOW_INSECURE=true`. With the `links/1` feature it reports where each listed path really is and reads a file only when it really is where the command's `expect_real` says (`internal/agent/links.go`), answering a `link_target` error with the real path otherwise; the server checks its rules on that target and asks again (`app/collectors/agent.py`, `_following_links`). With `progress/1`, a content command (`fetch`, `read_range`, `fetch_archive`) sent with `progress` gets a `progress` message every 10 seconds during which the agent read more of the file (or of the archive, up to and through the member asked for) and sent nothing else (`internal/agent/progress.go`), so a read that hangs still times out; and the server counts it as activity for that command's 30-second wait (`app/agent_registry.py`, `receive_progress`); `progress` is only sent to an agent that announced it. A `too_large` error says which limit was passed (`limit`: `fetch` or `decompressed`); without it, the server guesses from the number in the message (`app/collectors/agent.py`, `_raise_command_error`). The server never reads content from an agent without `links/1` (`AgentUpdateRequiredError`, 409, audited with the reason `agent_update_required`); listing goes on, and Settings → Agents shows it needs updating. It can be upgraded from Settings → Agents: it installs a new binary only if `SHA256SUMS` carries a valid Ed25519 signature from the release key built into it (`internal/agent/upgrade_signing_key.pub`) and the version is newer than its own. Configuration and service setup: `agent/README.md`.

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
| `audit_purge` | `AUDIT_PURGE_INTERVAL_SECONDS` (1 day), catch-up | Deletes audit rows older than the retention (Settings → System, 365 days, at least 90; 0 keeps everything). |
| `session_purge` | 1 h, catch-up | Deletes expired sessions and expired password-reset links. |
| `update_check` | 1 h, catch-up | Asks GitHub for the latest release, at most once a day. Off with `UPDATE_CHECK=false` or the switch in Settings → System. |
| `audit_integrity_check` | `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` (1 day), catch-up | Verifies the audit hash chain. |
| `backup` | 5 min tick | Makes an encrypted backup once the backup interval (Settings → System) has passed: 24 hours on a new install, 0 (off) kept on an install upgraded from before 0.7.0. |
| `wal_checkpoint` | 1 h | Checkpoints and truncates the SQLite WAL (`app/db.py`). |

### Connectors (`backend/app/collectors/`)

`registry.py` maps each `Protocol` to its module; every caller goes through it, and `errors.py` turns each library's exceptions into the `SourceError` types in `base.py`.

- `ssh.py`: SSH/SFTP with `paramiko`. Host keys are pinned in `SSH_KNOWN_HOSTS_PATH`; `SSH_HOST_KEY_POLICY` is `tofu` (pin on first connect) or `strict` (refuse unknown hosts).
- `smb.py`: SMB with `smbprotocol`, with signing required unless the source turns it off (`smb_require_signing`) and SMB 3 encryption when it asks for it (`smb_encrypt`). `smb_shares.py` lists a server's disk shares for the share picker.
- `winrm.py`: WinRM with `pywinrm`, running scripts in a WinRS shell, on drive paths only (UNC and device paths are refused). When the source names a session configuration (`winrm_configuration`), `winrm_jea.py` speaks PSRP (`pypsrp`) to a JEA endpoint instead and only calls the read-only functions of the reference role in `docs/examples/jea`.
- `local.py`: files on the PerchTail host. Used by the system log source and by admin-created local sources, which must sit under `LOCAL_SOURCE_ROOTS` (empty by default, which disables them). Local plain files are served in place, without scratch; a local `.gz` file, an archive member and anything redacted go through scratch like remote files (`app/api/archive.py`, `_resolve_content`).
- `agent.py`: relays commands to a connected push agent through `app/agent_registry.py`.

A source can read several folders (`Source.roots`, `app/source_roots.py`). Remote reads are bounded by `MAX_FETCH_MB`, `MAX_DECOMPRESSED_MB` and `REMOTE_READ_TIMEOUT_SECONDS`; a read over either size limit answers 413 (`app/api/archive.py`, `_fetch_too_large` and `_too_large`), when an archive is browsed as when it is opened or downloaded, and `app/api/errors.py` gives the same 413 for one an endpoint doesn't refuse itself. Concurrent connector work is limited globally and per source, and downloads and folder ZIPs per user (`app/limits.py`); a request over a limit gets 503 with `Retry-After`.

## Rule matching semantics

Implemented in `app/rules.py`.

- Rules decide which files and folders are visible and reachable on a source. A path no rule makes visible can't be listed, opened or downloaded.
- Rules are evaluated in `order`, and the last match wins, as in `.gitignore`.
- Patterns are globs (`**`, `*`, `?`) by default; a `re:` prefix makes a rule a regular expression. Rule regexes are compiled with RE2 (`google-re2`), so they run in linear time; backreferences and lookaround are rejected.
- A source with no rules shows nothing.
- A source holds at most 500 rules and a pattern at most 1000 characters (`MAX_RULES_PER_SOURCE`, `MAX_PATTERN_CHARS`), in the rule editor, pasted text and YAML import alike.
- Exclude rules match case-insensitively on every source. Include rules are case-insensitive for SMB and WinRM sources and for agents that report Windows, case-sensitive elsewhere.
- Links are judged by where they lead: on local, SSH and agent sources, the rules are checked on the path asked for and on its real path (where a symlink or junction, the path itself or a folder above it, leads), in listings (`DirEntry.target`, `DirListing.real_dir`) and in every read (`collectors/base.py`, `require_target`; the connectors get a `target_check` built from the rules by `collectors/errors.py`, and without one follow no link). A link out of the base path or to something hidden answers 404, audited with the reason `link_target_hidden` (`access.denied`, or `file.open_denied` for open and follow and `file.download_denied` for downloads; `app/api/errors.py`). SMB and WinRM refuse every link.

## Live browsing & ephemeral fetch behavior

- Browsing lists a folder live through the source's connector, one folder at a time, filtered through the rules. Listings are capped by the `listing_max_entries` setting.
- Opening or downloading always fetches fresh from the source. Nothing is cached or reused between opens.
- `.zip`, `.tar.gz` and `.tgz` files appear as folders; opening a member extracts only that member (`app/archives.py`). A `.tar.gz` is read in one pass, at most 50,000 entries (`MAX_TAR_ENTRIES`); past that its listing is cut short. A plain `.gz` file is decompressed and opens like any other file. An archive that can't be read as one, or a `.gz` file that can't be decompressed, is refused with 409 `archive_unreadable`, whether it is browsed or a member is opened (`ArchiveUnreadableRefused`), also on an agent that lists and reads archives on its host, which answers with its own `archive_unreadable` and `gzip_unreadable` codes (`SourceArchiveUnreadableError`). An encrypted zip member is refused with 409 `archive_member_unreadable` (`MemberUnreadableRefused`), on an agent through its `member_encrypted` code (`SourceMemberUnreadableError`).
- Fetched files go to `SCRATCH_DIR`, tracked in memory by `app/scratch.py`'s `ScratchStore`. Each open gets its own file, reference-counted by a key of the user's session, source, path and member (`app/api/archive.py`, `_scratch_key`), so closing a file releases only the copy that session opened. A file is deleted when its last reference is released, when the idle sweep finds it untouched past `SCRATCH_IDLE_SECONDS`, or when the size guard evicts it (oldest unreferenced first). A new fetch is refused with 507 when scratch is full or its filesystem is below `SCRATCH_MIN_FREE_GB` / `SCRATCH_MIN_FREE_PERCENT`. Scratch is emptied at startup.
- Follow (tail) polls with ranged reads of what was appended and detects rotation by the file's first bytes (an HMAC of them with a per-source key, `app/redaction/engine.py`, `head_digest`, shared with the alerts); it doesn't fetch the whole file again. `.gz` files and archive members answer 409 `follow_unsupported` and are reloaded whole; the viewer shows any other 409 as the server sends it and stops Tail when it carries an `error_code` (`frontend/src/lib/fetch-errors.ts`). Each poll takes a connector slot, and each Tail of a file in a session is audited once (`file.follow`) with the offset it starts from, and again after 10 minutes without a poll or when a poll reads from before that offset (`app/api/archive.py`, `_FollowAudits`).
- When redaction applies to a source (`redaction_enabled` and an effective redaction rule, `app/redaction_rules.py`), `/open` and `/follow` send content redacted on the server (`app/redaction/`), for every user: `/open` fetches into scratch, redacts into the scratch file it serves and deletes the fetched original before answering; `/follow` reads a little before the offset for context and sends only redacted whole lines. Offsets stay in bytes of the original (`X-Source-Offset`, `X-Follow-Offset`), an unfinished last line is sent as `[redacted: incomplete line]` (`X-Follow-Partial`), a `.gz` archive member is decompressed before it is redacted, and whole archives, UTF-32 files and content compressed in a format PerchTail doesn't decompress (bzip2, xz, zstd, 7z, rar, and gzip or zip in a file not named as one, told by its first bytes) are refused. The encoding is decided by the first bytes and checked again on every line (`app/redaction/encodings.py`, `find_reading_change`): a line that looks UTF-16 in bytes content, or ASCII text in clear UTF-16, is read as UTF-16-unsure from there, and a line that looks UTF-32 refuses the rest of the read (`[redacted: unsupported encoding]`, audited with `redaction_refused` and `redaction_refused_offset`). While the `redaction_downloads` setting is on, `/download` and `/download-zip` are redacted too: a file, `.gz` file or member is redacted as it is sent (UTF-16 kept), a whole `.zip`/`.tar.gz` is rebuilt in the same format without the members the rules hide (`app/archives.py`, `rebuild_archive`), a folder ZIP holds every file redacted, what can't be redacted is listed in `REDACTION-SKIPPED.txt`, and the response says so in `X-PerchTail-Redacted` (and in the file name with `redaction_download_suffix`). On an agent source whose connection announced `redaction/1` (`X-PerchTail-Agent-Features`, kept in `app/agent_registry.py` per connection and in `Source.agent_features`), the agent redacts instead: every content command (`fetch`, `read_range`, `fetch_archive`) carries a `redact` block with the rules, archives are listed and read on the host (`list_archive`, `fetch_archive`, `fetch` with `member`/`decompress`), and the server only rebuilds archives and ZIPs (`app/archives.py`, `write_archive`) from what arrived redacted; `app/collectors/agent.py` discards any answer that doesn't say it was redacted with the digest of the rules sent. An agent with `progress/1` keeps a command alive while it reads up to a member far into a large archive before its first chunk (see "Push agent"). An agent with `links/1` but without the feature falls back to redaction on the server (`agent.redaction_fallback`); one without `links/1` is refused first (`agent_update_required`), without that log. Alerts (`app/alerts.py`) run their query on the lines redacted as the Tail's and queue only those; on an agent that redacts, `read_range` with `with_line_offsets` answers `changed_lines`, from which the server works out where each line starts in the original.

## Access control (RBAC + SSO)

Implemented in `app/auth/rbac.py`.

- Each user has exactly one role (`User.role_id`).
- A role has global capabilities: `manage_users`, `manage_roles`, `manage_sso`, `create_source`, `manage_system_settings`, `view_audit_log`, `manage_alerts` and `view_access_report`.
- A role's grants (`RoleGrant`) give scoped capabilities on a customer, a folder or a source: `view`, `download`, `manage_rules`, `run_now` and `manage_source`. `manage_rules` covers only the visibility rules; `manage_source`, together with the global `create_source` (the routers' `require_manage`), covers adding, editing, moving and deleting sources, adding, renaming, moving and deleting folders, renaming and deleting customers (with a grant on the customer itself), agent tokens, saved credentials and agent upgrades; on its own it only allows the connection check (`check_connection`). `run_now` starts a file-name index pass.
- A grant applies to everything below its scope, including sources added later. The most specific grant wins: source, then the nearest folder up the tree, then the customer. With no grant, access is denied.
- One function decides: `resolve_access` returns the capabilities and the grant that decided them (`via`), reading grants and folders through a `GrantLookup` (the database, or the access report's in-memory snapshot). `resolve_capability`, `holds_capability_at` and the access report (`app/access_report.py`) all go through it.
- A role holds at most one grant per scope (`uq_rolegrant_scope`).
- Customers and folders are listed and reachable only within a role's grants (`org_reach`); one outside answers 404 (`concealed`), audited as `access.denied`. A source on which the role holds no capability answers the same 404 (`get_source_in_reach`). Only a super admin creates customers. A source in a folder always belongs to that folder's customer (422 otherwise).
- A grant holds at least one capability (422 otherwise).
- A super-admin role bypasses grants. The system log source (`is_system`) is reachable by super admins only.
- Delegated admins can only grant or assign what they hold themselves (`grant_within_authority`, `role_within_authority`). Only a super admin resets the password of, deactivates or changes the role of an account whose role has `manage_users` or `manage_roles` (`app/api/users.py`, `_require_not_peer_admin`, audited with the reason `peer_admin`).
- Sign-in is local (username and password) or SSO. SSO is OIDC (`app/auth/providers/oidc.py`: `joserfc` verifies the ID token against the provider's JWKS, `httpx` does the code exchange), with one provider enabled at a time. SSO users are provisioned on first login. Group-to-role mappings (`SSOGroupRoleMapping`, last match wins) set their role on every login; with no matching mapping, the user gets the "No Access" role, new or existing, except that the last active super admin is never demoted by a sign-in (the role is kept and a warning logged). Only a super admin deletes or turns off the provider; a delegated admin with `manage_sso` creates, changes and deletes only mappings to roles within their authority (`role_within_authority`), and changes the issuer, client or group claim only while every mapping is within it. Requests to the IdP (discovery, JWKS, token) follow the webhook address policy (`app/webhook_safety.py`), the discovery document is cached for 10 minutes for sign-in starts (cleared when the provider is saved), starting a sign-in is limited per client address (`app/login_throttle.py`), and ID tokens are accepted only with the asymmetric algorithms the provider announces, with `azp` checked when there are several audiences; a new account's username comes from `email` only when `email_verified` is true. The `saml` enum values in `app/auth/models.py` have no provider behind them.

## Security notes

- **Sessions** (`app/auth/sessions.py`, `app/api/auth.py`): a random token (`secrets.token_urlsafe(32)`) whose SHA-256 hash is stored in `AuthSession`. The `perchtail_session` cookie is `HttpOnly`, `SameSite=Strict`, `Secure` unless `SESSION_COOKIE_SECURE=false`, and lasts `SESSION_TTL_HOURS` (12). Expiry is also checked server-side, a deactivated user's sessions stop working, logout deletes the session, a password change ends the user's other sessions and gives the current session a new token, an admin's password reset ends all of the account's sessions (`app/api/users.py`, `reset_password`), and turning MFA on or off ends the other sessions. Users can list and end their own sessions from My profile. The SSO flow uses a separate `perchtail_sso_state` cookie (`HttpOnly`, `SameSite=Lax`, 10 minutes) to bind the OAuth state to the browser.
- **CSRF**: `SameSite=Strict` on the session cookie, plus `OriginCheckMiddleware` in `app/main.py`: on POST, PUT, PATCH and DELETE, an `Origin` (or `Referer`) header whose host and port differ from the request's `Host` gets 403. A request with neither header relies on SameSite alone.
- **Headers** (`SecurityHeadersMiddleware`): a strict Content-Security-Policy (`script-src 'self'`; `style-src` also allows `'unsafe-inline'` because CodeMirror injects styles at runtime; `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a `Permissions-Policy` turning off browser features PerchTail never uses, `Cross-Origin-Opener-Policy: same-origin` and `X-PerchTail: 1`. HSTS is left to the TLS-terminating reverse proxy. Every response carries an `x-request-id`: the caller's when it is at most 64 characters of `[A-Za-z0-9._-]`, a new one otherwise.
- **Credential encryption** (`app/crypto.py`): Fernet, with the key derived by PBKDF2-HMAC-SHA256 (600,000 iterations) from `CREDENTIAL_ENCRYPTION_KEY` and a random per-install salt stored at `CREDENTIAL_SALT_PATH` (default `DATA_DIR/credential_salt`, mode 0600). Losing the salt makes every stored secret unreadable. `python -m app.rotate_credential_key` re-encrypts everything under a new key and re-signs the audit chain in one transaction.
- **Audit log** (`app/audit.py`, `app/audit_hash_chain.py`, `app/audit_integrity.py`): every row carries an HMAC-SHA256 over its content and the previous row's hash. The HMAC key is derived from the same secret and salt, separately from the Fernet key, so the chain can't be recomputed from the database alone. Every audit write also emits an INFO log line. Failed logins and lockouts are audited, with repeats aggregated (`app/auth_events.py`).
- **Login throttle** (`app/login_throttle.py`): in memory, per username and per client address; `LOGIN_MAX_ATTEMPTS` (5) failures lock the account for `LOGIN_LOCKOUT_SECONDS` (300), and as many from one address, whatever the usernames, lock that address. An active local super admin with MFA on is locked per username and address. Failed logins for unknown accounts cost the same argon2 work as real ones.
- **MFA** (`app/auth/mfa.py`): TOTP for local accounts, with 10 one-time backup codes and replay protection (`mfa_last_used_step`). Required of super admins and roles with any global capability, and of every local account with the `require_mfa_local_accounts` setting (`app/auth/mfa_policy.py`); until a covered account sets it up, `get_current_active_user` answers 403 `mfa_setup_required`. `/auth/mfa/confirm` only confirms a pending enrollment, and its wrong codes count toward the login lockout; new backup codes come only from `/auth/mfa/backup-codes/regenerate`, with the password; turning one's own MFA off needs the password and a TOTP or backup code. SSO accounts rely on the identity provider and can't sign in locally.
- **Password policy** (`app/password_policy.py`, Settings → System): minimum and maximum length (12 and 128 by default), rejection of simple and personal passwords, and optional history, character classes and maximum age. Passwords are hashed with argon2id; a password longer than the policy maximum is refused in sign-in, change, re-confirmation and reset requests before it is hashed or checked. Admin-created local accounts must change their password at first login.
- **Password recovery by email** (`app/auth/password_reset.py`, `app/password_reset_limits.py`): off by default; a super admin turns it on (`password_reset_enabled`), and it works only while email is on and `PUBLIC_BASE_URL` is https. Local accounts only, never super admins or roles with a global capability, checked when the link is asked for and when it's used. `/auth/password-reset/request` always answers 202 at once and does the lookup and the email in the background. The token (32 random bytes) travels in the link's fragment, is stored as its SHA-256, works once, expires, and dies when the password or email changes. Using it revokes every session and opens none. Changing one's own email needs the current password.
- **Monitoring token** (`app/monitoring_access.py`, `MonitoringToken`): one deployment-wide bearer token, stored as a SHA-256 hash. `/monitoring/health` and `/metrics` always need it, plus an address in `MONITORING_ALLOWED_NETWORKS` when that is set. `/healthz` and `/readyz` answer a loopback caller without proxy headers, a caller in `PROBE_ALLOWED_NETWORKS`, or a caller with the token.
- **Agent token**: one enrollment token per agent source, shown once and stored as a SHA-256 hash (`Source.agent_token_hash`). The agent sends it as a bearer token on the WebSocket handshake and when downloading binaries.
- **Other guards**: alert webhook URLs and the SSO identity provider's endpoints must resolve to public addresses, checked again at send time and pinned to the checked address (`app/webhook_safety.py`), and the SMTP server may be on a private address but not on a loopback, link-local, multicast, unspecified or reserved one, checked when saved and before every message (`resolve_validated_smtp_ips`, `app/email_delivery.py`, always TLS with the certificate verified); archive extraction and fetches are size-limited; paths are checked for containment in every connector and in the agent (`internal/agent/pathsafety.go`).
- **Secure defaults**: `CREDENTIAL_ENCRYPTION_KEY` is checked at startup (`app/key_strength.py`: at least 32 bytes once decoded and 128 bits of estimated entropy); a weak key is a warning in the log and Settings → System, and a refusal only with `CREDENTIAL_KEY_REFUSE_WEAK=true`. `/docs`, `/redoc` and `/openapi.json` answer 404 unless `API_DOCS=true`. File content (open, follow, download, folder ZIP) is sent with `Cache-Control: no-store`. `DATA_DIR`, `LOG_DIR`, `SCRATCH_DIR` and `BACKUP_DIR` are made `0700` at startup (`app/data_layout.py`), and scratch files are created `0600`. Deleting a source removes its SSH host key and WinRM CA file when no other source uses them (`app/source_pins.py`).

## Application logging

`app/logging_config.py`: `structlog` with JSON output, to stdout and to `LOG_DIR/perchtail.log`, at `LOG_LEVEL` (default `INFO`). A `request_id` is bound per HTTP request. The file rotates at `LOG_MAX_FILE_MB` or at midnight, rotated files are gzipped, and they are deleted after `LOG_RETENTION_DAYS` or when all logs exceed `LOG_MAX_TOTAL_MB` (`app/log_rotation.py`). The SMB and SSH libraries' loggers (`smbprotocol`, `spnego`, `paramiko`) are capped at WARNING from the INFO level up (`QUIET_AT_INFO_LOGGERS`); at DEBUG, `LOG_QUIET_THIRD_PARTY=true` caps the connector and HTTP libraries (`NOISY_THIRD_PARTY_LOGGERS` in `app/log_rotation.py`) at INFO (`third_party_level`). Never log credentials, tokens or request bodies.

## Built-in log viewer

The app's own logs are a system source, seeded at startup (`app/bootstrap.py`, `seed_system_log_source`): protocol `local`, `is_system = true`, no customer, base path `LOG_DIR` (updated at startup when `LOG_DIR` changes, audited as `source.update`). Only super admins can open it. It can't be edited or deleted, and its rules can't be changed.

## Web UI

Main areas: the sign-in page (with "Forgot password?" while password recovery is on), the sources explorer with the file-name search, the viewer (tabs, find, follow, compare, download), alerts, My profile (display name, email, password, MFA, sessions), and Settings: sources, folders, agents, roles, users, SSO, system, severity indicators, redaction, email, import/export, audit log and access review (`lib/components/SettingsNav.svelte`).

## Data model

Core tables (`backend/app/models.py`):

- `Customer`: top-level grouping; every non-system source and every folder belongs to one.
- `Folder`: nested grouping of a customer's sources, for browsing and grant scopes.
- `Source`: a host and how to reach it (protocol, host, port, base path or roots, encrypted credential, WinRM and TLS options), plus agent (OS, version, announced features), reachability and file-name index state.
- `Rule`: an ordered include or exclude pattern on a source.
- `SeverityPattern`: a line-highlighting pattern for the viewer, global or per source, a glob or (with `re:`) a regex in RE2 syntax, at most 1000 characters; matching runs in the browser, in a Web Worker with a time budget per pattern (`lib/severity-worker.ts`, `lib/severity-matching.ts`, `lib/severity-highlighting.ts`).
- `RedactionRule`: a redaction rule, system-wide or a source's own: a built-in detector or a custom RE2 pattern (`app/redaction_rules.py`); configured by super admins only.
- `SourceRedactionOverride`: a source turning a system redaction rule off.
- `FileNameEntry`: one rule-visible file of a source in the file-name index (path, size, modification time).
- `Alert`: a query on the files in a scope, with a webhook URL and its owner.
- `AlertFileState`: how far an alert has read a file.
- `AlertDelivery`: a webhook call waiting to be sent or retried.
- `UpdateCheckState`: the last release-check result (single row).
- `JobRun`: the last run of each scheduled job.
- `AgentUpgradeRun`: one attempt to upgrade an agent, from request to outcome.
- `SystemSetting`: key-value settings from Settings → System; defaults live in code (`app/system_settings.py`).
- `SmtpSettings`: the system SMTP server from Settings → Email (single row), password encrypted.
- `MonitoringToken`: the hashed monitoring token (single row).

Auth tables (`backend/app/auth/models.py`):

- `Role`: name, built-in and super-admin flags, global capabilities.
- `RoleGrant`: scoped capabilities of a role on a customer, folder or source.
- `User`: username, profile, preferences, role, provider and SSO identity (with the IdP group whose mapping gave the role, `sso_role_group`), password and MFA state.
- `MfaBackupCode`: a hashed one-time recovery code.
- `PasswordHistory`: a previous password hash, kept only while the history rule needs it.
- `SSOProviderConfig`: the OIDC provider, configuration encrypted.
- `SSOGroupRoleMapping`: an ordered identity-provider group to role mapping.
- `AuthSession`: a hashed session token with expiry, last use and user agent.
- `PasswordResetToken`: a pending password-reset link (`app/auth/password_reset.py`): the token's hash, expiry, and fingerprints of the password hash and email it was issued for.
- `AuditLog`: who did what to which target, with metadata and hash-chain fields.
- `AuditChainState`: the hash chain's anchor and last verification result (single row).
- `AuditOutbox`: an audit event a migration recorded without the chain key, signed into the chain and deleted by the next start (`app/audit_outbox.py`).

Tables that other rows point at by id use `NO_ID_REUSE` (`sqlite_autoincrement`), so a deleted id is never reused. Timestamps are naive UTC (`app/timeutils.py`, `utcnow`). The schema is managed by Alembic (`backend/migrations/`); a schema change needs a migration.

## Repository structure

```
.github/
  workflows/          ci.yml, release.yml, security-scan.yml, codeql.yml, scorecard.yml
  upgrade-test/       seed and verify scripts of CI's upgrade test
  dependabot.yml, FUNDING.yml
.zap/                 rules.tsv for CI's OWASP ZAP baseline scan
agent/                Go push agent
  main.go
  internal/agent/     client, handler, config, path safety, links, archives, redaction, upgrade, protocol, version
  internal/redact/    redaction engine, with the test vectors shared with the server
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
    redaction/, redaction_rules.py   redaction engine and its rules
    alerts.py, name_index.py, name_index_store.py, source_status.py, email_delivery.py
    audit.py, audit_hash_chain.py, audit_integrity.py, audit_purge.py, audit_outbox.py, auth_events.py, access_report.py
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
loadtest/             load-test kit (k6 scenarios, seeding, reports) behind docs/sizing.md
Dockerfile, docker-compose.yml, .env.example
pyproject.toml        ruff, black and pytest configuration
README.md, CHANGELOG.md, ROADMAP.md, CONTRIBUTING.md, SECURITY.md, CLA.md, CODE_OF_CONDUCT.md, TRADEMARKS.md, LICENSE
```

## Deployment shape

The Docker image builds the frontend with Node 22 and runs the backend on `python:3.12-slim` as an unprivileged user (uid 10001), with pip removed once the requirements are installed. `backend/docker-entrypoint.sh` runs `python -m app.data_layout`, `python -m alembic upgrade head` and `uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers` (the client address comes from `TRUSTED_PROXIES`, `app/request_context.py`). `docker-compose.yml` keeps state in the `/data` volume. PerchTail is meant to sit behind a TLS-terminating reverse proxy. It runs as a single process per `DATA_DIR`; SQLite runs in WAL mode, so `DATA_DIR` must be on a local disk. `docker-compose.yml` publishes port 8080 on `127.0.0.1` only, with a read-only root filesystem, `no-new-privileges` and every capability dropped except the few the entrypoint's root step needs; running with `user: "10001:10001"` needs none. The image ships no test code (`.dockerignore`), and its entrypoint refuses the test hooks (`PERCHTAIL_TEST_PATCH_MODULE`, `PERCHTAIL_TEST_MODE`).

## How to test

CI (`.github/workflows/ci.yml`) runs these on every push and pull request:

- Backend, from the repository root: `ruff check backend`, `black --check backend`, `python -m alembic upgrade head` (from `backend/`), `pytest -v`, the load-test kit's checks (`ruff check loadtest`, `python -m unittest discover -s loadtest/tests`), then `pip-audit` (on `requirements.txt` and `requirements-dev.txt`) and `bandit -q -r backend/app`.
- Frontend, from `frontend/`: `npm run check` (svelte-check and the TypeScript projects), `npm test` (vitest), `npm run build`, and `npm audit` through `.github/scripts/npm_audit.py --level high`, which fails on any high or critical advisory except those accepted, each with a reason and an expiry date, in `.github/npm-audit-exceptions.json`.
- End-to-end, from `frontend/`: `npm run test:e2e` (Playwright). It starts its own backend on port 8001 with a throwaway database and real local sshd and smbd test servers; WinRM uses the fake in `app/testing/fake_winrm.py`. `e2e/accessibility.spec.ts` runs axe-core on every main page.
- Agent, from `agent/`: `go vet ./...`, `gofmt -l .` (must print nothing), `go test ./... -v`, 30 seconds of fuzzing for each of `FuzzIsSafeRelativePath` and `FuzzFetchStaysInBase` (path checks, `./internal/agent`) and of `FuzzRedactBlockCuts`, `FuzzRedactUTF16`, `FuzzRedactWindow` and `FuzzCustomRule` (redaction engine, `./internal/redact`), cross-compilation for linux/amd64, linux/arm64 and windows/amd64, `gosec` and `govulncheck`.
- The Docker image is built and scanned with Trivy, then started for an OWASP ZAP baseline scan (`.zap/rules.tsv`). The upgrade test starts the last release (`PREVIOUS_RELEASE_IMAGE`, pinned by digest), fills it through its API, upgrades the same volume to the new image and checks that every object, secret and audit entry came through, and that the previous release refuses the upgraded database (`.github/upgrade-test/`). A CycloneDX SBOM of the image is uploaded.
- The workflows are audited with zizmor, and the CI scripts' tests run (`python3 -m unittest discover -s .github/scripts/tests`).

Other workflows: `codeql.yml` (CodeQL, on pushes and pull requests to `dev`, `main` and `hotfix/**`, and weekly), `scorecard.yml` (OpenSSF Scorecard, on `main` and weekly) and `security-scan.yml` (the dependency and image vulnerability scans, every day; the image scan accepts the advisories listed in `.grype.yaml`, each with a reason and a review date checked by `.github/scripts/grype_exceptions.py`).

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
