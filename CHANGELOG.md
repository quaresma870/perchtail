# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/) once a first
release ships (0.x releases may include breaking changes between minors).

## [Unreleased]

### Added
- Project scaffold (M0): FastAPI app under `backend/`, `structlog` JSON logging
  with request-id middleware and gzip-rotated daily log files, SQLite +
  SQLModel wired up with Alembic migrations, `ruff`/`black`/`pytest` tooling,
  CI workflow, and a `docker-compose`/`Dockerfile` skeleton.
- Data model (M1): `Customer`, `Source`, `Rule`, and the auth models `Role`,
  `RoleGrant`, `User`, `SSOProviderConfig`, `AuditLog`, plus an
  `app/crypto.py` helper (Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`) so
  `Source.credential_ref` and `SSOProviderConfig.config` have a real path to
  encryption-at-rest from day one.
- RBAC & local auth (M2): grant-resolution logic and a FastAPI permission
  dependency (`auth/rbac.py`), `LocalPasswordProvider` (argon2id, forced
  password change on admin-created accounts), and `AuditLog` writes wired
  into login and role/grant creation, each paired with a structured INFO log
  line.
- `api/auth.py`: login/logout/me/change-password endpoints backed by
  server-side sessions (hashed opaque token in an httpOnly, SameSite=strict
  cookie, stored in a new `AuthSession` table) rather than a stateless JWT,
  so revoking access is immediate.
- Rule engine (M3): `rules.py` implements CLAUDE.md's matching semantics —
  glob (default, path-separator-aware `**`/`*`/`?`, not stdlib fnmatch) or
  regex (`re:` prefix) patterns, last-match-wins evaluated by rule order,
  and zero rules matching nothing.
- SSH/SFTP connector and ephemeral scratch (M4): `collectors/ssh.py`
  (paramiko-based live listing and fetch-on-open), `scratch.py`
  (refcounted purge, idle-sweep backstop, size-guard eviction, both wired
  as APScheduler jobs), `archives.py` (`.zip`/`.tar.gz` as virtual folders,
  transparent `.gz` decompression), and `api/archive.py`
  (browse/open/close/download, gated by the M2 permission dependency and
  independently by the rule engine on every specific path).
- Remaining connectors (M5): `collectors/smb.py` (`smbprotocol`),
  `collectors/winrm.py` (`pywinrm`, base64-through-PowerShell for file
  content since WinRM has no native bulk transfer), and `collectors/
  local.py` (reads directly off disk). `api/archive.py` now skips the
  scratch store entirely for local, plain files — served straight from
  their real path — while still using it for a local `.gz` or archive
  member, since decompressing/extracting produces new derived bytes that
  have to live somewhere.
- Nested folders for source organization (M4.5): a `Folder` model
  (self-referential, nested under a `Customer`) and `RoleGrant.scope_type`
  gains `folder`, generalizing the existing source-beats-customer grant
  resolution to N levels — source → folder chain (nearest first) →
  customer, most specific wins.
- Admin CRUD API (M7, part 1): `api/customers.py`, `api/folders.py`,
  `api/sources.py`, `api/rules.py`, `api/roles.py`, `api/users.py` — the
  full backend surface the admin/viewer UI needs. Sources gain a
  `POST /sources/{id}/check` on-demand connection check, replacing the
  pre-pivot "run-now"/"run history" wording in CLAUDE.md's original Web UI
  section (there's no `Run` model and nothing runs on a schedule anymore —
  see ROADMAP.md's M7 notes). Rules gain a gitignore-style raw-text
  paste-mode endpoint (`PUT .../rules/raw`, leading `!` negates a line) in
  addition to the row-based CRUD + reorder. Roles gain a duplicate-role
  action (clones grants too) and grant CRUD, with a privilege-escalation
  guard: only an existing super-admin can create or edit a super-admin
  role. Users gain admin-driven create/update/reset-password/deactivate
  (deactivate is a soft delete, per CLAUDE.md's Security notes). Customer/
  folder management is treated as an admin surface gated by the
  `create_source` global capability rather than per-scope grants, per
  CLAUDE.md's "its own small admin surface" wording. See
  [ROADMAP.md](ROADMAP.md) for what's next.
