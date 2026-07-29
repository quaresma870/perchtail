# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/) once a first
release ships (0.x releases may include breaking changes between minors).

## [Unreleased]

### Added
- Phase 2 (push-agent) backend infrastructure: a new `agent` protocol for
  sources that can't be reached inbound over SSH/SMB/WinRM. The agent
  (a future Go binary — see ROADMAP.md) dials out and holds a persistent
  WebSocket open at `/agent/connect`, authenticated with a bearer
  enrollment token (`POST /sources/{id}/agent-token`, hashed at rest,
  returned in plaintext once); the backend then relays live `list`/`fetch`
  commands down that connection on demand, exactly as it would call any
  other connector directly — nothing is proactively synced or mirrored, so
  the always-fresh, nothing-persisted rule holds for agent-mode sources too.
  `AgentRegistry` (`app/agent_registry.py`) tracks live connections
  in-memory and bridges FastAPI's synchronous connector calls to the async
  WebSocket via `asyncio.run_coroutine_threadsafe`. `SourcePublic` now
  reports `agent_connected`/`agent_last_seen_at` for agent-protocol sources.

## [0.1.1] - 2026-07-30

Phase 1b (SSO) complete: OIDC single sign-on alongside local auth, with
auto-provisioned no-access accounts for first-time SSO logins and an SSO
settings admin page.

### Added
- Phase 1b (SSO): `OIDCProvider` (`app/auth/providers/oidc.py`) — the second
  `AuthProvider` alongside `LocalPasswordProvider`, covering the OIDC
  authorization-code flow (discovery, code exchange, ID-token signature
  verification against the IdP's live JWKS, and `iss`/`aud`/`exp`/`nonce`
  claim checks). Admin CRUD for `SSOProviderConfig` at `/sso/...` (gated by
  `manage_sso`), with a test-connection action mirroring sources' `/check`
  endpoint; `GET /auth/sso/status`, `GET /auth/sso/login`, and
  `GET /auth/sso/callback` drive the actual sign-in flow, using a
  Fernet-encrypted, self-contained `state` param rather than a server-side
  state table. A first-time SSO sign-in auto-provisions a user with a new
  builtin "No Access" role (`app.bootstrap.seed_no_access_role`) — an admin
  assigns the real role afterward, per CLAUDE.md. Local login is unaffected
  and keeps working alongside an enabled provider. Frontend: an SSO
  settings admin page (gated by `manage_sso`, nav-linked) to configure the
  provider and test its connection, and a "Sign in with {name}" button on
  the Login page when one is enabled.
- More unit tests: backend `app/audit.py` (`record_audit_event`'s field
  defaults and its documented "doesn't commit" contract) and
  `app/timeutils.py` (`utcnow`'s naive-datetime guarantee) were the only two
  app modules without a dedicated test file — both now have one. Frontend:
  extracted `memberPath` (the archive-member-path slicing logic that had been
  duplicated inline in `FolderTree.svelte` twice, plus once more inside
  `download-href.ts`) into `lib/tab-key.ts`, with tests, so the three call
  sites can't silently drift apart.
- README: real screenshots (Sources, Viewer, Role editor) under `docs/images/`,
  a short table of contents, and a fixed `git clone` command (was still the
  `<your-org>` placeholder). CONTRIBUTING.md gets the same clone-URL fix plus
  frontend dev setup (`npm install`/`npm run dev`/`npm run check`/`npm test`),
  which had been missing entirely even though the frontend has been a core
  part of the stack since M7.

### Fixed
- `RoleEditor.svelte`'s bare `form { flex-direction: column }` rule (meant for
  the role-details form) also matched the page's second `<form>` — the
  "add grant" row — flipping its flex main axis from row to column. Combined
  with `.add-grant select { flex: 0 0 160px }` (a width in the intended row
  layout), the flipped axis turned that into a *height*, rendering both scope
  dropdowns as ~160px-tall boxes instead of a normal single-line row. Found
  while capturing README screenshots. Scoped the rule to `form.card` (the
  role-details form's actual class) so it can't leak onto other forms in the
  same component.

## [0.1.0] - 2026-07-29

Phase 1 (MVP) complete: agentless live browsing over SSH/SFTP, SMB, and WinRM;
ephemeral fetch (nothing mirrored); rule-scoped RBAC (customers, folders,
roles, grants, local auth, audit log); the built-in log viewer; and the full
admin/viewer UI (Vite + Svelte), packaged as a single Docker image.

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
- Built-in log viewer (M6): `app/bootstrap.py` seeds a system source on
  first startup pointed at `LOG_DIR`, so the app's own rotated logs are
  browsable through the exact same live-browsing path as customer sources
  — gated purely by `is_super_admin` (already enforced since M2).
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
  CLAUDE.md's "its own small admin surface" wording. `api/archive.py` also
  gains `GET .../download-zip` (zips an entire folder, fetching each
  contained file fresh) so the Viewer can offer "download a zipped folder,"
  not just a single file. `auth/me` (and login/change-password) now also
  returns `is_super_admin`/`global_capabilities` so the frontend can gate
  nav without a second round-trip.
- Admin & viewer UI (M7, part 2): a Vite + Svelte + TypeScript SPA under
  `frontend/` — the frontend stack CLAUDE.md never named (see ROADMAP.md's
  M7 notes for why Svelte). Login/change-password, a sources list with
  inline connection-check and a create/edit form, a rule editor (row-based
  + gitignore-style raw-text paste mode, toggled per source), a lazy-loaded
  folder tree (children fetched only on expand, one level of `.zip`/
  `.tar.gz` expansion) feeding a CodeMirror 6 tabbed viewer pane (in-file
  search via CodeMirror's built-in search panel, single-file and
  zipped-folder download), and Roles/Users admin pages (grant CRUD,
  duplicate-role, reset-password, deactivate). CI gains a `frontend` job
  (`svelte-check` + `vite build`) alongside the existing backend job.
- Phase 1 exit (M8): multi-stage `Dockerfile` (Node stage builds the SPA,
  copied into the Python runtime image), `docker-entrypoint.sh` (runs
  `alembic upgrade head` before `uvicorn`), and `app.main` mounts the built
  frontend as static files at `/` — works unmodified with the frontend's
  hash-based routing, since the browser only ever requests `/`.
  `app.bootstrap.seed_initial_super_admin` creates a break-glass super-admin
  with a randomly generated, once-logged password on first startup (a fresh
  deployment previously had no way to create its first user at all).
  README's Quick start is now real, tested steps rather than a placeholder.
- Visual redesign and logo: a dark theme (CSS custom properties in
  `app.css` — background/border/text/accent tokens, per-protocol and
  per-status badge colors) applied across every page, plus a fish/perch mark
  (`frontend/public/favicon.svg`) per CLAUDE.md's mascot note — used as the
  favicon, the nav-bar brand, and the README header. The full brand asset
  pack (icon at multiple raster sizes, mark-only light/dark variants, and an
  icon+wordmark lockup) lives under `frontend/public/brand/` for later
  branding work. The Viewer's CodeMirror pane gains a matching dark theme
  and lightweight log-level highlighting (`[info]`/`[warn]`/`[error]` tokens
  colored, error lines tinted) so a log reads the same way `grep -i error`
  would highlight it.
- Frontend unit testing (`vitest`, added alongside this redesign): pulled
  the non-CSS logic that redesign touched or introduced out into small,
  pure, testable modules — `lib/codemirror-theme.ts`'s log-level token/line
  detection, `lib/tab-key.ts` (the tab/tree-highlight key format shared
  between Viewer and FolderTree, previously duplicated inline in both),
  `lib/download-href.ts` (FolderTree's per-entry download-URL branching),
  and `lib/rule-format.ts` (RuleEditor's raw-text line rendering/parsing) —
  plus tests for the existing `lib/api.ts` request wrapper, `lib/auth.ts`'s
  capability check and login/logout/session-refresh flows, and `lib/hash.ts`'s
  hashchange-driven route store. CI's `frontend` job runs `npm test` before
  the build step.

### Fixed
- Archive member listing/opening didn't check the rule chain against the
  specific member requested — browsing a `.zip`/`.tar.gz` listed every
  member unconditionally, and `/open`/`/download` didn't re-check a
  requested `member` against the rules at all, so a rule scoped to show
  only the archive itself leaked its full contents. Both paths now check
  `is_visible` on the member's combined virtual path.
- `is_safe_relative_path` only split on `/`, so a backslash (`..\\`) path
  traversal segment passed straight through — a real, exploitable gap for
  SMB/WinRM sources specifically, since those connectors join a relative
  path onto a Windows `base_path` with backslashes. Now splits on both
  `/` and `\\`, and rejects a bare `:` to rule out Windows drive-letter
  absolute paths.

See [ROADMAP.md](ROADMAP.md) for what's next (Phase 1b: SSO).
