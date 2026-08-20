# Roadmap

This turns the phased plan and "first things to do" list in [CLAUDE.md](CLAUDE.md)
into trackable milestones. No calendar dates — this is a part-time project, so
progress is tracked by what's actually merged, not a schedule. Check
[CHANGELOG.md](CHANGELOG.md) for what's shipped; this file is for what's next.

Each Phase 1 milestone below is meant to become a GitHub Milestone with these
items as issues, once the repo is up. Checkboxes here track the plan; issue
links can be added as they're created.

## Phase 1 — MVP

Order matters: auth/RBAC and the rule engine are load-bearing for everything
built after them (per CLAUDE.md, "First things to do in a new session"), so
they come before any connector or UI work, not after.

### M0 — Project scaffold ✅
- [x] `backend/` FastAPI project structure matching the "Suggested repo
      structure" in CLAUDE.md
- [x] `structlog` logging setup (`logging_config.py`): JSON output, `request_id`
      middleware, level thresholds, `TimedRotatingFileHandler` + gzip rotator,
      `LOG_RETENTION_DAYS` env var (default 30)
- [x] SQLite + SQLModel/SQLAlchemy wired up, migrations tooling chosen (Alembic)
- [x] `ruff` + `black` + `pytest` configured; CI workflow running them on PRs
- [x] `docker-compose.yml` + `Dockerfile` skeleton (even before there's much to
      run, so packaging isn't a phase-1-exit surprise)

### M1 — Data model ✅
- [x] Core models: `Customer`, `Source`, `Rule`
- [x] Auth models: `Role`, `RoleGrant`, `User`, `SSOProviderConfig`, `AuditLog`
- [x] Built together, not staged — auth touches every endpoint written after

### M2 — RBAC & local auth ✅
- [x] Grant-resolution logic (`auth/rbac.py`) implementing the pseudocode in
      CLAUDE.md, including the `is_system` short-circuit
- [x] FastAPI permission dependency wrapping grant resolution
- [x] `LocalPasswordProvider` (argon2id hashing, forced password change on
      first login for admin-created accounts)
- [x] Unit tests for grant resolution — this is one of the two pieces of logic
      in the project that must be correct before anything is built on top
- [x] `AuditLog` writes wired into login + role/grant changes, each also
      emitting a structured INFO log line
- [x] `api/auth.py`: HTTP login/logout/me/change-password endpoints and
      session issuance. Went with server-side sessions (opaque token in an
      httpOnly cookie, hashed and stored in SQLite via a new `AuthSession`
      table) rather than a stateless JWT — this tool holds production
      credentials, so being able to revoke a session immediately (logout, or
      deactivating a user) mattered more than avoiding a DB lookup per
      request. `get_current_active_user` blocks every other endpoint until
      an admin-created account's forced password change is done.

### M3 — Rule engine ✅
- [x] `rules.py`: glob (default) + regex (`re:` prefix) pattern matching
- [x] Last-match-wins precedence, evaluated in rule order
- [x] Zero rules → matches nothing (explicit opt-in)
- [x] Unit tests covering ordering, glob/regex mix, and the empty-ruleset case

### M4 — First connector: SSH/SFTP + ephemeral scratch ✅
- See [docs/source-setup.md](docs/source-setup.md) for the SSH/SFTP-side
  prerequisites (dedicated account, chroot jail, ACLs) this connector assumes.
- [x] `collectors/ssh.py` via `paramiko`: live directory listing filtered
      through the rule engine, fetch-on-open
- [x] `scratch.py`: per-session scratch store, refcounted purge, idle-sweep
      backstop, size-guard eviction (zero-ref oldest first)
- [x] `.gz` transparent decompression; `.zip`/`.tar.gz` as virtual folders
      (`zipfile`/`tarfile`) with the same fresh-fetch/purge rules applied to
      extracted members
- [x] Permission dependency applied to every archive endpoint from the start
- [x] Tests mock the SSH client — no real remote host in CI

  Notes on decisions made building this:
  - `Source.credential_ref` decrypts to a JSON blob (`{"username":...,
    "private_key" or "password":...}`), not a bare secret — CLAUDE.md's data
    model didn't spell out how SSH auth fields map onto one encrypted
    string field. Added `app.crypto.encrypt_credential`/`decrypt_credential`
    for this.
  - Directories are always listed (never filtered by the rule engine) so
    the tree stays navigable toward deeper matches like `**/*.log`; only
    files are subject to the rule chain. An exclude rule targeting a
    directory still hides everything under it (its files fail the rule
    check) even though the directory itself may still appear, possibly
    empty — a UX wrinkle, not a security gap.
  - `api/archive.py`'s `open`/`download` take `path` and an optional
    `member` (for archive contents) as separate query params rather than
    a combined virtual path string, to avoid ambiguous prefix-parsing for
    nested archives. Only one level of archive nesting is supported.
  - Added a hard path-traversal guard (`rules.is_safe_relative_path`)
    independent of the rule engine's own verdict — a permissive rule could
    coincidentally match a `../..` payload, so this can't be the only
    defense (CLAUDE.md's security scope explicitly calls this out).
  - SSH host-key verification uses trust-on-first-use (`AutoAddPolicy`);
    sources are admin-configured, not arbitrary input, so this is an
    acceptable default for now, not a hardened one.

### M4.5 — Nested folders for source organization ✅
- [x] `Folder` model: self-referential (`parent_folder_id`), belongs to a
      `Customer`; a `Source` can optionally sit inside one (`Source.folder_id`,
      nullable — null means directly under the customer, no folder)
- [x] `RoleGrant.scope_type` gains `folder`; grant resolution walks
      source → folder chain (innermost to outermost) → customer, most specific
      grant wins — generalizes the existing source-beats-customer logic to N
      levels instead of 2 (see CLAUDE.md's updated grant-resolution pseudocode)
- [x] Migration for the new table + `Source.folder_id` column (SQLite needs
      Alembic's batch mode to add a FK constraint via ALTER TABLE — the
      autogenerated migration doesn't do this by default and would have
      failed on first run; fixed by hand)
- [x] Unit tests: a grant on a mid-level folder applies to sources several
      levels deeper; a source-level grant still beats a folder-level one; a
      folder-level grant still beats the customer-level one; the nearest
      folder in the chain beats a more distant ancestor folder
- [ ] Note for M7: role editor's access tree becomes customer/folder/.../
      source; source create/edit gets an optional folder picker; folder
      management (create/rename/move/delete) is its own small admin surface —
      not built here, just flagged so M7 doesn't miss it

  Deliberately out of scope: permission scoping *within* a single source's own
  directory tree (different capabilities for different sub-paths of the same
  source). Considered and declined — Folders group sources, not paths inside
  one, and Rules already control content visibility uniformly for whoever can
  access a source, which is sufficient for now.

### M5 — Remaining connectors ✅
- See [docs/source-setup.md](docs/source-setup.md) for the SMB/WinRM-side
  prerequisites (scoped share + ACLs, JEA-constrained WinRM account) these
  connectors assume.
- [x] `collectors/smb.py` (`smbprotocol`)
- [x] `collectors/winrm.py` (`pywinrm`)
- [x] `collectors/local.py` — no scratch needed, reads directly off disk

  Notes on decisions made building this:
  - Added `collectors/base.py` with a shared `DirEntry` dataclass (was
    duplicated per connector since M4) and a `local_copy(source, path)`
    context manager on every connector — remote protocols fetch into a
    temp file and yield that; `local.py` just yields the real path
    directly, no copy. `api/archive.py`'s archive-listing and
    gzip/archive-member extraction now use this uniformly instead of each
    hand-rolling a `tempfile.TemporaryDirectory()`.
  - `api/archive.py`'s `open`/`download` now skip the scratch store
    entirely for local, plain files (served straight from `resolve_path()`
    — no `X-Scratch-Key` header, nothing to `/close`), matching CLAUDE.md's
    "no ephemeral scratch needed" for local sources literally. A local
    `.gz` or archive member still goes through scratch, since decompressing
    or extracting produces new derived bytes that have to live somewhere.
  - WinRM has no native bulk file-transfer primitive, so `fetch_file` reads
    via `[IO.File]::ReadAllBytes` + base64 through PowerShell — fine for
    log files, not efficient for very large ones. CLAUDE.md documents
    WinRM as the SMB fallback, not the primary path, so this is an
    accepted tradeoff, not an oversight.
  - `smbclient`'s (from `smbprotocol`) high-level `smbclient` module API
    is used instead of raw SMB2 primitives — much closer to `os`/`pathlib`
    semantics (`scandir`, `open_file`), same reasoning as choosing
    `paramiko`'s SFTP layer over raw SSH channels for M4.

### M6 — Built-in log viewer (dogfooding) ✅
- [x] System source seeded on first startup, pointed at `LOG_DIR`,
      `is_system = true`, `customer_id = null`
- [x] Access gated purely by `is_super_admin`, no grant can reach it
      (already true structurally since M2 — `resolve_capability`'s
      `is_system` short-circuit — this milestone was really about seeding)
- [ ] "system" badge in the admin sources list; non-editable, non-deletable
      — UI concern, deferred to M7 (no source CRUD API/UI exists yet at all,
      for any source, so "non-editable" isn't meaningfully testable before
      M7 builds source CRUD in the first place)

  Note: seeded with one broad include rule (`**/*`) rather than zero rules —
  zero rules would make the viewer show nothing at all (rules.py's explicit
  opt-in default). `LOG_DIR` is a dedicated directory that should only ever
  contain this app's own rotated logs, and the source is already
  super-admin-only, so a broad rule here doesn't weaken the security model.
  Verified live end-to-end: seeding is idempotent across restarts, a
  super-admin can browse and open the app's own live log (and literally see
  its own audit-log entries in the response), a regular user gets 403.

### M7 — Admin & viewer UI ✅

  Note on scope vs. CLAUDE.md's original wording: "last run"/"run-now"/"run
  history with errors" are pre-pivot leftovers from when this project was
  designed around scheduled mirroring (see CLAUDE.md's own opening section on
  why "logmirror" stopped fitting). There is no `Run`/`SyncState` model and
  nothing is fetched on a schedule anymore — every browse is live and every
  open is a fresh fetch, so there is nothing to "run" and no history to keep.
  Replaced with a lightweight on-demand connection check (`POST
  /sources/{id}/check`) that opens the connector and lists the base path,
  surfacing reachability in the sources list without inventing a persistent
  run concept the architecture no longer has.

  Frontend stack: CLAUDE.md never named one beyond "CodeMirror 6 for the
  editor component." Asked the user to pick (Vite+Svelte / Vite+Vue 3 /
  Vite+React / server-rendered+htmx); the question went unanswered, so,
  consistent with this project's practice of making documented judgment calls
  on undecided points rather than blocking, proceeding with the recommended
  default: **Vite + Svelte + TypeScript**. Reasoning: smallest runtime and
  build output of the SPA options (matters for a self-hosted single-container
  app), no JSX/virtual-DOM overhead for what's mostly CRUD forms and a tree +
  editor pane, and first-class, low-ceremony support for the CodeMirror 6
  integration this milestone needs anyway. Easily revisited before 1.0 if it
  turns out to be the wrong call — nothing else in the architecture depends
  on this choice.

  Backend CRUD API this UI needs (`api/customers.py`, `api/folders.py`,
  `api/sources.py`, `api/rules.py`, `api/roles.py`, `api/users.py`) is done
  and tested. `api/archive.py` also gained a `GET .../download-zip` endpoint
  (zips an entire folder, fetching each contained file fresh) since CLAUDE.md's
  Viewer spec calls for downloading "a single file or a zipped folder" and
  only the single-file path existed before this milestone. See CHANGELOG.md
  for what each surface does.

  Access-tree simplification: CLAUDE.md's Roles UI spec asks for a
  "customer/folder/source access tree with search/filter, collapsed by
  default." Built instead: a flat scope-type + scope-picker dropdown pair
  (customer → folder → source) plus a table of existing grants. This proves
  the grant model end-to-end (create/update/delete a grant at any scope,
  most-specific-wins resolution, duplicate-role cloning grants) without the
  extra weeks a real nested/collapsible/searchable tree widget would take —
  worth revisiting once there are enough real customers/folders that a flat
  dropdown gets unwieldy, not before.

  Archive-browsing limit: expanding a `.zip`/`.tar.gz` lists its members
  flat (whatever the archive's own namelist gives), one level deep — the
  backend's `/browse` can't recurse into a sub-path *inside* an archive (see
  `api/archive.py`), so a directory-flagged member inside an archive is a
  dead end in the tree, not further expandable. Matches what M4's archive
  handling actually supports; nested-archive UX wasn't asked for.

- [x] Sources list (status via connection check, protocol, rule count,
      system badge — non-editable/non-deletable)
- [x] Rule editor: row-based UI + raw-text/gitignore-style paste mode
- [x] Lazy-loaded folder tree (fetch children on expand only)
- [x] CodeMirror-based viewer pane: tabs, in-file search, download (single file
      or zipped folder)
- [x] Roles UI: list, editor (global-capability toggles + customer/folder/
      source access grants, duplicate-role action) — see access-tree
      simplification note above
- [x] Users UI: list with active/inactive, create, reset password,
      deactivate/delete, assigned role

### M8 — Phase 1 exit ✅
- [x] docker-compose deployment documented and tested end-to-end
- [x] README "Quick start" filled in with real steps (replacing the current
      placeholder)
- [x] Security pass on credential storage (encryption at rest) and archive
      endpoints (path traversal)
- [x] Tag `v0.1.0`

  Notes on decisions made building this:
  - **No way to create the first user.** Every prior milestone assumed a
    `User` already existed, but nothing ever seeded one — a fresh deployment
    had zero users and zero way to log in. Added
    `bootstrap.seed_initial_super_admin`: a no-op once any user exists,
    otherwise creates a built-in super-admin role and a user with a randomly
    generated password (never read from config), logged once at WARNING —
    same pattern as the admin-driven reset-password endpoint, not a
    long-lived secret sitting in `.env`.
  - **Frontend serving.** CLAUDE.md's packaging section says "Docker +
    docker-compose... sit comfortably behind an nginx reverse proxy," which
    reads as PerchTail being one thing behind someone else's existing proxy,
    not shipping its own. So: a multi-stage Dockerfile builds the SPA and
    the FastAPI app mounts the built `dist/` as static files at `/` (mounted
    last, after every API router, so it can't shadow a route). This works
    unmodified with the frontend's hash-based routing — the browser only
    ever requests `/`, so no catch-all/rewrite rule is needed for
    client-side routes.
  - **Two real security bugs found and fixed during the pass, not just
    reviewed:**
    1. Browsing into a `.zip`/`.tar.gz` listed every member unconditionally,
       and `/open`/`/download` didn't check a requested member against the
       rule chain at all — a rule scoped to show only the archive itself
       would leak everything packed inside it, and a client calling
       `/open` directly with an arbitrary `member` bypassed the browse
       listing's filtering entirely. Fixed: both paths now check
       `is_visible` on the member's combined virtual path
       (`{archive_path}/{member_name}`), same convention already used
       elsewhere.
    2. `is_safe_relative_path` only split on `/`, so a `..\\`-style
       (backslash) traversal segment passed straight through — and
       `collectors/smb.py`/`collectors/winrm.py` join a relative path onto a
       Windows `base_path` with backslashes, so this was a real,
       exploitable gap for SMB/WinRM sources specifically (SSH/local were
       incidentally safe since POSIX doesn't treat backslash as a
       separator). Fixed: splits on both `/` and `\\`, and also rejects a
       bare `:` to rule out Windows drive-letter absolute paths.
  - **Known limitation, not fixed here:** `CREDENTIAL_ENCRYPTION_KEY` is a
    single static key with no rotation story — rotating it requires
    re-entering every source's credentials. Acceptable for v1 (see Open
    decisions below); revisit if this becomes a real operational pain point.

## Phase 1b — SSO ✅

- [x] `OIDCProvider` behind the existing `AuthProvider` interface — see
      "Notes on decisions made" below for why this uses `joserfc` directly
      rather than `authlib`'s own (now-deprecated) JOSE module
- [x] SSO settings admin page: configure active provider, test-connection
      action
- [x] Auto-provision SSO users with the no-access default role
- [x] Local break-glass account confirmed to keep working alongside SSO

### Notes on decisions made

- **`joserfc` instead of `authlib`'s `authlib.jose`.** CLAUDE.md named
  authlib for JOSE/JWT handling; as of authlib 1.7, its own maintainers
  deprecated `authlib.jose` in favor of `joserfc` ("compatible before version
  2.0.0"). Using the actively-maintained module directly, rather than a
  deprecated shim, better serves the actual intent (a solid, maintained JWT
  toolkit) than a literal reading of the library name.
- **Plain `httpx` calls instead of `authlib`'s `OAuth2Client`** for the
  authorization-code exchange. The three-step flow (build authorize URL →
  exchange code → fetch/verify ID token) is a handful of HTTP calls; keeping
  each as its own small, named function (`fetch_discovery_document`,
  `exchange_code_for_tokens`, `fetch_jwks`) means tests can monkeypatch each
  step directly, the same "mock the client, don't hit a real remote"
  convention the SSH/SMB/WinRM connector tests already use — routing
  everything through `OAuth2Client` would mean faking that client's
  internals instead.
- **Stateless `state` param instead of a server-side state table.** The
  OIDC `state`/nonce is Fernet-encrypted (via the same `app.crypto` helper
  Source credentials already use) rather than stored in a new DB table —
  Fernet tokens already embed a creation timestamp, which doubles as the
  expiry check (10 minutes). One fewer table, same CSRF protection.
- **ID token signature verification against the IdP's live JWKS** (not just
  trusting the userinfo endpoint) — `verify_id_token` checks the RS256
  signature, `iss`, `aud`, `exp`, and `nonce` claims before anything derived
  from the token is trusted. This and the OIDC callback's state/nonce
  handling are the two places in this milestone where "must be correct
  before anything else is built on top of it" applies most.
- **No account linking.** An SSO login is matched purely by
  `(auth_provider=oidc, external_id=sub)`; a username collision with an
  existing local account raises rather than silently linking. Deliberately
  out of scope for v1 — CLAUDE.md doesn't ask for it, and account linking
  is its own security-sensitive decision (which side's identity wins?)
  better made when a real need for it shows up.
- **`PUBLIC_BASE_URL` setting instead of deriving the redirect_uri from the
  incoming request.** The alternative means trusting `X-Forwarded-*`
  headers from whatever reverse proxy sits in front of the app (see
  CLAUDE.md's packaging note) — an explicit setting sidesteps that trust
  question, at the cost of one more thing to configure.
- **One enabled provider at a time, enforced server-side** (409 on trying
  to enable a second), matching CLAUDE.md's "one OIDC/SAML provider
  configured at a time for v1" — this is the thing the login flow actually
  depends on (unambiguously picking "the" active provider), so it's a real
  constraint, not just documentation.

## Phase 2

- [x] Backend agent-link infrastructure: `Protocol.agent`, `AgentRegistry`,
      the agent's WebSocket endpoint, and the `agent` connector
- [x] Go push-agent (single static binary, cross-compiled) for sources not
      reachable inbound
- [x] Frontend: agent source UI (enrollment-token generation, connection
      status)
- [ ] `SAMLProvider` (`python3-saml`) — **only if** a real need shows up; see
      the open decision in CLAUDE.md (OIDC already covers Azure AD/Entra ID,
      Okta, Google Workspace, Keycloak/Authentik)

### Notes on decisions made — frontend agent source UI

- **`SourceEditor.svelte`'s `agent` protocol hides Port and the SSH/SMB/WinRM
  credential fieldset** in favor of an "Agent enrollment" section — there's
  no host to reach or password to store, only an enrollment token. Host and
  Base path stay as free-text fields for the admin's own documentation
  (which physical host, which directory it's supposed to be watching), even
  though `collectors/agent.py` never actually reads `Source.base_path` for
  this protocol — the real root is enforced by the agent's own
  `PERCHTAIL_BASE_PATH` config, not by anything this app sends it.
- **`has_agent_token` added to `SourcePublic`**, mirroring the existing
  `has_credential` field — needed so the UI can label the button
  "Generate token" vs. "Regenerate token" without the backend ever
  re-exposing the token itself after its one-time display.
- **Sources.svelte shows agent-protocol sources' live status
  (`agent_connected`/`agent_last_seen_at`) instead of the manual "check"
  button** the other protocols use — a `/check` call against an
  agent-protocol source can only succeed while an agent happens to be
  connected anyway (it just calls the same `list_directory`), so the
  already-live registry state the source list returns is a strictly better
  answer than a manual, one-shot check would be.

### Notes on decisions made — backend agent-link infrastructure

- **The agent dials out; the backend still drives every command live.**
  CLAUDE.md frames the push-agent purely as a reachability fix ("this is
  about network reachability, not about the always-fresh rule above, which
  still applies"), not license for a proactive mirror/sync design. So the
  agent opens a persistent WebSocket to the connector and then just waits —
  the connector sends `list`/`fetch` commands down that connection on
  demand, exactly when a user browses or opens a file, and the agent reads
  its local disk and replies. Nothing is pushed ahead of time; an agent-mode
  source is exactly as "always-fresh" as an SSH or SMB one, just reached
  over a connection the far end initiated instead of one this app dials
  directly.
- **WebSocket over HTTP long-polling or gRPC** for that persistent
  connection — asked the user to weigh in on transport and got no response,
  so made the call directly: plain `FastAPI`/Starlette WebSocket support
  needs no extra dependency, cross-compiles trivially from Go
  (`gorilla/websocket` or `nhooyr.io/websocket`), and a single long-lived
  duplex connection is a more natural fit for "server pushes a command,
  agent pushes back a result" than polling or a heavier RPC framework would
  be for what is, at bottom, a two-message-type protocol.
- **`AgentRegistry` bridges FastAPI's sync path operations to the one
  async WebSocket.** Every existing connector (`collectors/ssh.py` etc.) is
  plain sync code, run in FastAPI's thread pool; the agent's connection,
  like any WebSocket, only exists on the main asyncio event loop.
  `agent_registry.py`'s `send_command_sync` bridges the two via
  `asyncio.run_coroutine_threadsafe` (schedule the send+await onto the
  bound loop) plus a blocking `Future.result(timeout=...)` on the calling
  thread — so `collectors/agent.py`'s `list_directory`/`fetch_file` read as
  plain synchronous calls, same shape as every other connector, with the
  cross-thread bridging fully contained in one module.
- **In-memory registry, not a DB table**, for which sources currently have
  a live connection — same reasoning as `scratch.py`'s `ScratchStore`: a
  live connection is inherently per-process and can't survive a restart, so
  persisting it would just be a stale value waiting to be read.
- **Enrollment token is bearer-auth over the WebSocket handshake**, hashed
  with SHA-256 before storage (`Source.agent_token_hash`) — same pattern as
  `auth/sessions.py`'s session tokens. `POST /sources/{id}/agent-token`
  returns the plaintext exactly once, at generation time, mirroring the
  existing admin reset-password UX; regenerating invalidates whatever token
  the agent's config file was using.
- **`Source.agent_last_seen_at`** is informational only (surfaced in the
  admin UI), updated on every successful handshake — the actual "is it
  connected right now" answer always comes from `AgentRegistry.is_connected`,
  never from this column, so it can't drift out of sync with reality.

### Notes on decisions made — Go push-agent binary

- **`agent/` is its own Go module** (`agent/go.mod`), not folded into the
  Python backend's structure — CLAUDE.md's suggested repo layout already
  calls this out as a phase-2 concern with its own tree. Internal packages
  live under `agent/internal/agent` so `main.go` stays a thin
  config-load-and-run wrapper, same separation `backend/app/main.py` keeps
  from the rest of the backend.
- **`gorilla/websocket`** for the client side of the persistent connection —
  the de facto standard Go WebSocket client, pairs with the plain
  Starlette/FastAPI WebSocket server the backend already uses (see the
  backend agent-link infrastructure notes above), no protocol translation
  needed on either end.
- **The wire protocol is a flat JSON envelope** (`{"type", "id", "path"}` in,
  `{"type", "id", ...}` or `{"type": "..._error", "id", "error"}` out) that
  mirrors `agent_registry.py`/`collectors/agent.py` exactly — a `list`
  command returns `{"name", "is_dir", "size"}` entries, a `fetch` command
  returns base64-encoded content (`content_b64`), matching the existing
  `collectors/winrm.py` convention for wire-transferring file bytes.
- **`IsSafeRelativePath` re-implements `rules.is_safe_relative_path`
  independently in Go**, rather than trusting the backend's own validation —
  the agent is the last line of defense against a `..`/absolute-path/
  drive-letter payload actually reaching its local filesystem, so it can't
  rely solely on a check made on the other end of the wire.
- **Reconnect with exponential backoff** (1s → 30s cap) rather than a fixed
  retry interval or giving up — an agent is meant to run unattended for long
  periods on a host that may itself restart or lose connectivity
  intermittently.
- **No file-system watching, no local queue, no persistence of any kind.**
  The agent answers exactly the command it's given and nothing else —
  reinforcing, on the agent's own side, the same always-fresh design the
  backend enforces on its side.

## Phase 3

- [x] Full-text search — needs its own indexing design since nothing persists
      from the viewing scratch space; design before building, per CLAUDE.md
- [x] Full-text search: match on file path and source host/name too, not just
      line content, case-insensitive — a source or file whose name matches
      the query should surface even if none of its lines happen to contain
      that text (e.g. searching "win-app-02" should find the source, not just
      lines that literally say "win-app-02")
- [ ] Alerting — notify on new content matching a saved search (see the
      Alerting design notes below for the working scope decision)
- [ ] IdP group-claim-to-role auto-mapping
- [ ] System/operational health endpoint(s) for external monitoring
      (Zabbix, and ideally Prometheus too) — see notes below
- [ ] Security hardening pass — see the dedicated section below; called out
      explicitly rather than left implicit, since this audience holds
      production credentials and treats security posture as a first-class
      requirement, not a nice-to-have

### Notes on decisions made — full-text search

- **A genuinely separate index, not a reuse of the viewing scratch space** —
  exactly as CLAUDE.md flagged this would need. `app/search_index.py`'s
  background indexer (an APScheduler job, same shape as `scratch.py`'s
  sweeps) walks each opted-in source's rule-visible files and stores short
  per-line snippets in a SQLite FTS5 virtual table
  (`app.db.ensure_search_schema`). This is a deliberately lagging,
  approximate secondary structure — the live viewer's fetch-fresh behavior
  is completely unaffected by anything here.
- **Opt-in per source (`Source.search_indexing_enabled`, off by default)** —
  the design questions asked about this went unanswered, so the call was
  made directly: indexing is the one place in this project that stores a
  form of log content at rest, even reduced to short snippets, so it gets
  the same conservative "explicit opt-in, not on by default" treatment the
  rule engine already uses for visibility (a source with zero rules matches
  nothing).
- **Short per-line snippets stored, not full extracted text** — same
  unanswered-question judgment call, made toward the smaller footprint:
  one FTS5 row per non-empty line (path, line number, snippet ≤500 chars),
  not the complete text of every indexed file. Search results show the
  matching line with FTS5's own highlighting; opening a result still
  re-fetches the live file for the full view, same as clicking it in the
  tree.
- **Staleness tracked by file size alone, not size+mtime** —
  `SearchIndexState` per (source, file_path). None of the five connector
  protocols report a file's modification time (`collectors/base.py`'s
  `DirEntry` only has name/path/is_dir/size), so size is the only signal
  available uniformly across all of them. This under-detects a same-size
  content edit, an accepted tradeoff for log files that are typically
  append-only (grow) or rotated (renamed), not edited in place.
- **The user's search-box input is wrapped as one quoted FTS5 phrase**,
  not passed through as FTS5's own query syntax — predictable, grep-like
  substring matching beats exposing AND/OR/NOT/prefix* to a plain search
  box, and avoids a MATCH syntax error on input like an unbalanced quote.
- **FTS5's `snippet()` output is HTML-escaped before its `<mark>` highlight
  tags are spliced back in** (`_escape_snippet`, using control-character
  placeholders round-tripped through `html.escape`) — `snippet()` inserts
  its highlight markers into the *raw* stored line with no escaping of its
  own, and a log line is arbitrary content, so rendering it unescaped via
  the frontend's `{@html}` would be a stored-XSS hole (a line containing
  `<script>...</script>` would execute as-is). Caught and fixed during this
  same pass, with a regression test.
- **Plain files and transparent `.gz` are indexed; `.zip`/`.tar.gz`
  containers are not** — decompressing `.gz` first (same as the viewer does
  on open) is cheap and rotated logs spend most of their life gzipped, but
  indexing every member of a bulk archive (how deep? every nested archive
  too?) is a real design question of its own, left for a future pass rather
  than answered speculatively here.
- **Binary content is sniffed and skipped** (a null byte in the first 8KB),
  and files over a configurable size cap (`search_index_max_file_size_mb`,
  default 20MB) are skipped too — the indexer reads a whole file into memory
  to index it, so both guards exist for the same reason the scratch store
  has a size guard: a safety valve for load, not a design goal.
- **Search UI click-through** (`Search.svelte` → `Viewer.svelte`) passes
  the target path and line number as a query string
  (`#/viewer/:id?path=...&line=...`); `CodeMirrorPane` gained an imperative
  `scrollToLine()` method (called via `bind:this` after the tab opens,
  rather than a reactive prop) so a search result opens the file and jumps
  straight to the matched line, not just the source's root.

## UI reorganization: unified Settings navigation

- [x] Collapse Sources/Roles/Users/SSO into a single "Settings" top-nav entry
      with a shared sub-nav, so the top nav reads Viewer | Search | Settings
      instead of listing every admin surface individually

### Notes on decisions made

- **Sources moved into Settings wholesale**, even though it was never
  capability-gated the way Roles/Users/SSO are — any authenticated user
  could always reach it, mainly to check connection status/rule counts.
  The Viewer already has its own lighter source picker for "just browse",
  so Sources' unique remaining value is the admin actions (create/edit/
  delete); `SettingsNav`'s "Sources" tab stays unconditionally visible
  (matching its old unconditional nav link) so that read-only use isn't
  lost, while the "Settings" top-nav entry itself is likewise always shown
  rather than capability-gated, for the same reason.
- **A shared `SettingsNav.svelte` component embedded at the top of each
  page**, not a nested `svelte-spa-router` with a wrapping layout — the
  simpler option given `svelte-spa-router`'s flat routing model has no
  built-in layout/outlet concept; each settings page already renders its
  own `<div class="page">`, so adding one shared sub-nav component above it
  costs one import + one line per file rather than restructuring routing.
- **Every route gained a `/settings` prefix**
  (`/settings/sources`, `/settings/sources/:id`, `/settings/roles`, etc.);
  a bare `/settings` renders `SettingsIndex.svelte`, which redirects
  (via `replace`, not `push`, so it doesn't add a spurious history entry)
  to `/settings/sources` — always reachable, so it's a safe unconditional
  landing tab for a bookmarked or typed `/settings` URL.
- **Found and fixed a real bug while verifying this in a browser**: `/sso`
  was missing from `vite.config.ts`'s dev-proxy prefix list (`/search` had
  the same gap, caught and fixed during the Phase 3 work). It only affected
  local `npm run dev` — production serves the API and the built SPA from
  the same FastAPI process with no path-based reverse-proxy split, so
  `/sso` always reached its router there regardless.

### Notes on decisions made — full-text search: path/host matching

- **Built, but landed on (2) for host/name, not the (1) it was leaning
  toward** — actually building both options out surfaced a problem with
  (1) that wasn't obvious on paper: `search_index_fts` is one row per
  *line*, so denormalizing a source's host/name into every row means a
  host/name match is technically true of every single indexed line of that
  source. For a source with thousands of indexed lines, that's not "the
  source surfaces in results", it's "every line of that source floods the
  top 50 results" — the opposite of the intended UX. (2) doesn't have this
  problem since it's resolved outside the line-granularity index entirely.
- **File path**: `file_path` is now an indexed FTS5 column (was
  `UNINDEXED`, storage-only), so one unified MATCH query covers content and
  path together, ranked by the same `rank`. This has the identical
  every-line-of-a-matching-file problem host/name would have had under (1)
  — solved in `search_index.search()` by asking FTS5's `snippet()` against
  the content and path columns separately per row: a row is a genuine
  content hit if the content snippet actually got highlighted; otherwise,
  if the path snippet did, it's a path-only hit, deduplicated down to one
  representative row per `(source_id, file_path)` before the result list is
  built. `matched_field` (`"content"` | `"path"`) rides along on
  `SearchHit`/the API response so the frontend can label a path-only hit
  distinctly ("filename match") instead of showing a misleadingly
  unhighlighted line.
- **Host/source name**: resolved as (2), and it turned out to need *no
  backend endpoint at all* — `Search.svelte` already fetches every
  RBAC-visible source (`GET /sources`) to resolve a content hit's
  `source_id` to a display name, so matching by name/host is just a
  client-side filter over that same already-fetched list
  (`lib/source-match.ts`'s `filterSourcesByNameOrHost`), rendered as its
  own "Sources matching" section above the content-hit results. Always
  current (it's live source metadata, not an index), no re-index lag, and
  zero new round-trips.
- **Schema upgrade for existing deployments**: `app.db.ensure_search_schema`
  now detects a `search_index_fts` table still carrying the old `file_path
  UNINDEXED` declaration (FTS5 can't ALTER a column's indexed-ness in
  place), drops and recreates it, and clears `SearchIndexState` alongside
  it so previously-indexed files look "new" again and rebuild into the new
  schema on the next sweep — otherwise they'd stay permanently unsearchable
  post-upgrade, skipped forever as "unchanged by size" despite the FTS
  table under them having just been wiped. Runs automatically on startup,
  same as the table's original creation; no separate Alembic migration,
  consistent with FTS5 schema already living outside Alembic's management.

### Notes on decisions made — alerting

- **Scope, as currently understood: content-match alerts, not operational
  health alerts.** "Alerting" in CLAUDE.md's phase list is one word with no
  further spec; read in context (immediately after full-text search in the
  same sentence) as "save a search, get notified when new indexed content
  matches it" — extending the Phase 3 index rather than a separate
  system-health-alerting concern (which the new monitoring-endpoint item
  below covers instead). Flagged here explicitly since this is a judgment
  call on an underspecified word, not a confirmed requirement.
- **Rides on the existing FTS5 index and indexer, no parallel structure**:
  a new `Alert` row (owner, saved query, optional source scope, webhook
  config, `last_checked_at`) and an `evaluate_alerts()` sweep (same
  APScheduler shape as `run_indexing_sweep`) that only looks at files whose
  `SearchIndexState.indexed_at` advanced since the alert's last check —
  reusing the timestamp signal already in place rather than depending on
  FTS5 rowid stability (rowids aren't stable across re-indexes, since a
  changed file's rows are deleted and reinserted).
- **Webhook-only notification channel for v1, not email** — no SMTP
  sending exists anywhere in the project today (temporary passwords are
  displayed once in the UI, never emailed), so email would be new
  infrastructure; a generic JSON webhook covers Slack/Teams/PagerDuty/
  generic consumers with zero new dependencies, matching the project's
  minimal-infra ethos.
- **An alert can only ever fire on sources with `search_indexing_enabled`
  already on** — a hard consequence of riding on the FTS5 index, not a
  separate opt-in decision to design.
- **A webhook is a new "content leaves the system" path**, same category of
  decision as full-text search's own opt-in indexing (CLAUDE.md's "nothing
  sitting around afterward for someone to leak" ethos) — enabling an alert
  is a deliberate export choice, worth calling out explicitly in the UI
  copy when this gets built, not just in this doc.
- **RBAC is re-checked at evaluation time, not just at alert-creation
  time** — an alert only ever evaluates sources the owning user can
  currently view via `visible_source_ids`, so revoking a grant silently
  stops that alert's scope from firing again, without needing to remember
  to also edit or delete the alert itself.

### Notes on decisions made — system/operational health endpoint(s)

- **A separate, richer endpoint alongside the existing plain `/healthz`**,
  not a replacement for it — `/healthz` stays a fast, unauthenticated
  liveness check for the Docker healthcheck/orchestrator; a new endpoint
  (`/health/detailed` or similar) carries the structured data an external
  monitoring system like Zabbix (or Prometheus, if that gets added too)
  actually wants to poll and alert on.
- **Candidate contents**: overall status (ok/degraded/error), DB
  reachability + latency, scratch usage vs `scratch_max_gb`, count of
  enabled sources by protocol, count of currently-connected vs configured
  agent-protocol sources, last successful search-indexing sweep time (and
  whether any opted-in source is overdue), whether the APScheduler jobs are
  still actually running (next-run time not stuck in the past), app
  version, uptime.
- **Needs its own auth, separate from user sessions** — a monitoring
  system can't do an interactive cookie-session login. Leaning toward a
  single long-lived, admin-generated bearer token (hashed at rest, shown
  once at generation) scoped only to this endpoint — the same "hash at
  rest, plaintext shown once" pattern the agent enrollment token
  (`Source.agent_token_hash`) and session tokens already use, rather than
  inventing a new credential-handling convention. IP-allowlisting the
  endpoint is a reasonable *additional* deployment-level measure (nginx
  `allow`/`deny`) but not a substitute for real auth on the app's side.
- **Zabbix specifically favors an HTTP agent item + JSONPath preprocessing
  per metric** (modern Zabbix, ≥5.0) against one JSON endpoint, rather than
  one endpoint per scalar metric — plan the response shape and document
  the JSONPath expressions for the common items (a short `docs/
  monitoring.md`, mirroring `docs/source-setup.md`'s per-integration style)
  rather than requiring Zabbix-specific endpoint variants.
- **Worth designing so a Prometheus `/metrics` endpoint is a thin second
  wrapper over the same underlying health-check internals later**, even if
  only the Zabbix-oriented JSON endpoint ships first — several self-hosted
  shops standardize on Prometheus+Grafana instead of (or alongside) Zabbix.

## Connections home redesign

Inspired by Apache Guacamole's dashboard-style landing page — kept in
PerchTail's existing theme/design system, no visual-language changes.

- [x] Viewer home page (`#/viewer` with no source selected) becomes a
      two-column layout: recent connections on the left, all connections
      on the right — replacing the current flat single list of source cards
- [x] "All connections" gets a search box matching folder, customer, or
      host, case-insensitive (`lib/connection-filter.ts`) — not the
      source's own display name, per spec
- [x] `Source` list responses carry `customer_name`/`folder_name` so cards
      can show "Customer / Folder" as subtext without a separate lookup
- [ ] Folder-tree navigation for browsing sources by customer/folder — the
      current "All connections" list shows customer/folder as flat subtext
      per card (enough to search/scan), not an actual expandable tree.
      `Folder` is fully modeled and RBAC-scoped (unlimited nesting via
      `parent_folder_id`), but nothing in the frontend renders it as a
      tree yet; still open.
- [ ] Dedicated folder/host management admin page (create/rename/move/
      delete folders, move sources between them) — CLAUDE.md flags this as
      its own admin surface and it was never built; only inline folder
      creation from the source editor exists today (both the original gap
      and this redesign's search box work off that same inline-create
      flow, not a standalone page)

### Notes on decisions made — connections home redesign

- **"Recent connections" needed new tracking, not just a reorder — built
  as planned.** `GET /sources/{id}/browse` (root path only, i.e. first
  hop into a source, not every sub-directory expand) now records a
  `source.open` `AuditLog` row; `GET /sources/recent` reads the current
  user's own most-recent-per-source events back, re-checking live
  visibility so a revoked grant can't leak a source through history.
  `GET /sources/{id}/download` also now logs `file.download` — this was
  CLAUDE.md's own stated minimum audit bar ("file download") that had
  never actually been wired up anywhere.
- **Recent connections and the full audit log viewer (below) share a data
  source but stayed two separate features, per explicit direction.** This
  redesign only writes the new events and reads back the current user's
  own recent ones (`GET /sources/recent`) — no general-purpose audit
  filtering/viewing endpoint, which is scoped on its own below.
- **Deployment-wide feature toggles, needed for both this and the audit
  viewer, got a small shared mechanism now rather than one bespoke flag
  each.** A new `SystemSetting` key-value table + `GET`/`PATCH
  /system-settings` (gated by a new `manage_system_settings` global
  capability, a new "System" tab under Settings) backs a `search_view_enabled`
  toggle — off hides the Search nav entry *and* redirects away from the
  `/search` route itself, not just the link, so it's actually off for a
  bookmarked/typed URL too. The audit-log toggle described below reuses
  this same mechanism once that page exists; no dead UI was added for it
  ahead of time.
- **Folder-tree navigation and the standalone management page are still
  open**, deliberately deferred out of this pass — the shipped "flat list
  with Customer / Folder subtext + search" covers the same real need
  (find a source by where it's organized) without the added scope of a
  real expand/collapse tree component or drag-and-drop-style folder
  management UI. Revisit if the flat-list-with-search approach turns out
  not to be enough at real scale.

## Full audit log viewer (admin-only)

Flagged as its own feature, separate from the connections home redesign
above, even though it shares the same `AuditLog` writes that redesign work
added. `AuditLog` has been write-only since Phase 1 — every write site
(login, source/rule/role/customer/folder CRUD, and now `source.open`/
`file.download` from the redesign work) already exists, but there's still
no read endpoint and no admin page, even though CLAUDE.md's "Application
logging" section always specced it as "a durable, queryable record ...
read via an admin UI page."

- [ ] `GET /audit` endpoint: paginated, filterable by action/type, user,
      target type, and date range
- [ ] Gated by a dedicated capability (e.g. `view_audit_log`), admin-only
      per explicit direction — not opened up via the existing customer/
      folder/source grant tree, since audit visibility is a global concern,
      not scoped to what a role can browse
- [ ] Frontend: new "Audit Log" page under Settings
  - [ ] Filter controls for action/type (multi-select against the known
        action namespace: `login`, `source.*`, `rule.*`, `role.*`,
        `user.*`, `customer.*`, `folder.*`, `sso.*`, `source.open`,
        `file.download`)
  - [ ] A retention control — admin-configurable from the frontend, not
        just an env var
- [ ] Backend retention enforcement: a scheduled purge job (APScheduler,
      same shape as the scratch idle-sweep and search-index sweep) driven
      by that configurable setting
- [ ] Deployment-wide on/off toggle for this page, reusing the
      `SystemSetting` mechanism the connections-home redesign already
      built for the Search view toggle (`app/system_settings.py`,
      `GET`/`PATCH /system-settings`) — add an `audit_view_enabled` key
      and a second row on the System settings page once this page exists;
      not built ahead of time since a toggle with nothing to gate yet
      would just be dead UI (see that section's notes)

### Notes on decisions made — full audit log viewer

- **Retention becomes a real decision here, not a deferred one.** This
  roadmap's own "Open decisions" list has carried "Audit log retention
  policy — keep forever, or expire after N months?" as unresolved since
  Phase 1. Explicit direction: make it admin-configurable from the
  frontend rather than picking a number now — the UI needs a setting
  (e.g., days), not just a filter on the display.
- **This is a second, independent retention knob from `LOG_RETENTION_DAYS`.**
  That setting governs the rotated structured *application* log files
  (`logging_config.py`, gzip + `TimedRotatingFileHandler`); `AuditLog` is a
  separate SQLite table with its own lifecycle, so its retention setting
  needs its own storage and its own purge job — the two shouldn't be
  conflated just because they sound similar.
- **Type/action filtering is a first-class frontend requirement, not just
  a nice-to-have** — per explicit direction, the audit page's parameters
  need to let an admin narrow by what kind of action happened, not just
  scroll a flat chronological feed.

## Viewer: find in document

A Notepad++-style "Find All in Current Document" results panel — a list of
every match in the currently open file (line number + snippet), click to
jump — alongside, not instead of, the existing Ctrl+F inline highlight and
next/previous navigation (`@codemirror/search`, see the earlier Ctrl+F fix
in CHANGELOG.md).

- [x] Results panel component listing every match with line number + a
      short snippet of surrounding text, most-natural-order (top to
      bottom of the document)
- [x] Click a result to jump to it — reuses `CodeMirrorPane`'s existing
      `scrollToLine`-style jump, same mechanism the cross-file Search
      page's results list already uses today
- [x] Existing inline Ctrl+F panel (highlight + next/previous) stays as
      is — this is an addition, not a replacement
- [x] Entry point: a "Find All" toggle button in the pane toolbar (the
      same stateful-toggle-button pattern used throughout the "toward an
      advanced editor" section below) — click to open a bottom-docked
      panel with its own query/case/regex controls, click again to close

### Notes on decisions made — find in document

- **No new dependency needed.** Neither CodeMirror's own search package
  nor any other editor library (Monaco, Ace) ships a results-list panel
  like this out of the box — Notepad++, VS Code, etc. all build it as
  custom UI on top of their editor's basic search primitives, same as
  we'd be doing.
- **Built as a pure, unit-tested scan over the tab's own content string
  (`lib/find-in-document.ts`), not `@codemirror/search`'s `getSearchCursor`
  / `view.state.doc`.** The tab's fetched content is the same data either
  way; scanning it directly keeps the matching logic framework-independent
  and trivially testable without spinning up a CodeMirror view (same
  "extract the pure function" pattern as `connection-filter.ts`/
  `tab-key.ts`). A deliberate, separate consequence: the results panel has
  its own query/case/regex controls, not synced with whatever's currently
  typed into the inline Ctrl+F panel — two independent inputs rather than
  coupling their state.
- **Coexists with the current inline search, doesn't replace it** — per
  explicit direction. Two complementary tools: quick highlight-and-step-
  through for a single term, versus "show me everywhere this appears at
  once" for scanning a large log.
- **Capped at 5000 matches** (`maxResults`), reporting `truncated: true`
  rather than silently stopping — a safety valve for a pathological
  query (e.g. a single common character) against a huge file, not a
  design goal, same spirit as the scratch store's size guard.

## Viewer: toward an advanced editor (not yet triaged into a phase)

Further steps toward a fuller, Notepad++-like *viewing* experience for the
CodeMirror-based Viewer. Explicitly **not** going there: editing or saving
changes back to a source — dropped deliberately, it would contradict
CLAUDE.md's core "read-only, always" principle for no strong enough
reason. Everything below stays entirely display-only: none of it ever
mutates the file being viewed or writes anything back to the source.

**Interaction pattern, decided:** every toggle-able view mode below (wrap,
show-all-characters, mark-highlighting, tail -f, compare) is a stateful
toolbar button, not a modal or a settings-page trip — click to turn a mode
on, click again to turn it off. For anything that needs a second input
(compare needs a second file), arming the button puts the pane in "pick a
target" mode; the next file opened/selected from the tree completes the
action, and clicking the button again clears it and returns to normal
viewing.

- [x] **Severity indicators become admin-configurable, both globally and
      per-source, with a dedicated Settings section, plus jump-to-
      next-problem navigation.**
  - [x] New backend model, `SeverityPattern` (`level` [error/warning/info/
        debug], `pattern`, `pattern_kind` [glob|regex, `re:` prefix — same
        convention as `Rule`], `enabled`, `highlight_line`,
        `include_in_navigation`, `source_id` nullable — null means the
        global default, set means a per-source override), seeded with
        sensible global defaults on first startup so it isn't empty
        (`app/severity_patterns.py`'s `DEFAULT_GLOBAL_PATTERNS`)
  - [x] `GET`/`POST`/`PATCH`/`DELETE` endpoints for admin CRUD on the
        pattern set (global: `/severity-patterns`; per-source:
        `/sources/{id}/severity-patterns`), plus `GET
        /sources/{id}/severity-patterns/effective` the Viewer calls to
        fetch the *effective* set for whatever source is open — a
        source's own patterns where it has any, falling back to the
        global set otherwise (override, not merge — same "most specific
        wins" shape as grant resolution, just at the pattern level)
  - [x] New "Settings → Severity Indicators" page for the global default
        set: row-based editor (level, pattern, line-tint toggle,
        nav-eligible toggle, enabled toggle) — raw-text/gitignore-style
        paste mode deliberately skipped for this feature (see notes below)
  - [x] A new section on the source editor (`SourceEditor.svelte`,
        alongside the existing "Include in full-text search" toggle) to
        override severity indicators for that specific source — same
        row-based editor, scoped to just that source
  - [x] `CodeMirrorPane`/`codemirror-theme.ts` fetch and highlight against
        this configured (effective) set instead of hardcoded regexes —
        matching logic lives client-side in
        `lib/severity-highlighting.ts` (pure, unit-tested), CodeMirror
        glue (`ViewPlugin`s/decorations) stays in `codemirror-theme.ts`
  - [x] Next/previous-problem step command, wired to toolbar buttons in
        the Viewer — steps through lines with a match from a
        navigation-eligible pattern, wrapping at either end. Resolves the
        earlier open question in favor of a **per-pattern
        `include_in_navigation` flag**, not a fixed warn-or-worse
        severity floor — an admin decides what counts as a "step to"
        problem instead of it being hardcoded; the seeded defaults enable
        it for error/warning but not info/debug (routine noise, not
        "problems")
  - Gating: global CRUD uses `manage_system_settings` (from the
    connections-home redesign work); per-source overrides use
    `manage_rules`, same as `Rule` — consistent with how the rest of the
    grant model already splits "deployment-wide" from "per-source"
    concerns
- [x] **Per-file-type syntax highlighting.** Language picked from the open
      file's own extension (`lib/file-language.ts`'s `languageForFilename`,
      pure and unit-tested) — `.json` → `@codemirror/lang-json`, `.xml`/
      `.html`/`.htm`/`.svg` → `@codemirror/lang-xml`, `.js`/`.mjs`/`.cjs`/
      `.jsx`/`.ts`/`.tsx` → `@codemirror/lang-javascript` (all three were
      already-installed dependencies, unused anywhere in the codebase
      until now). Coexists with severity-pattern highlighting on the same
      pane; anything unrecognized (most log files) gets no language
      extension and displays exactly as before — additive, never
      required. Uses the archive member's own name for files opened
      inside a `.zip`/`.tar.gz` (same field `FolderTree` already emits on
      open), not the archive's name.
- [ ] **Compare files (diff view), as a toggle button.** Arm the "Compare"
      button, pick a second file from the tree (or another open tab), and
      it renders a read-only diff against the currently active file in
      place. `@codemirror/merge` (not yet installed) is the natural fit.
- [x] **Line-wrap toggle.** `EditorView.lineWrapping`, cheap and directly
      useful for long log lines.
- [ ] **Beautify / minify for embedded JSON, XML, and (lower priority) JS.**
      Display-only reformat, never touches the file on disk.
- [ ] **Live-follow / "tail -f" mode, as a toggle button.** The
      architecturally biggest item here — deferred. Two candidate
      mechanisms: client-side polling (works uniformly but adds
      round-trips over SSH/SMB/WinRM), or extending the agent-mode
      WebSocket with a "watch" command (cheaper, agent-only).
- [x] **Reload/refresh button.** A manual re-fetch of the currently open
      file's content in place, without closing and reopening the tab.
- [x] **Copy selected lines (with line numbers).**
- [x] **"Show all characters" toggle** (whitespace/CRLF-vs-LF), relevant
      given this tool spans both Linux and Windows sources.
- [x] **Go-to-line** (Ctrl+G).
- [x] **Bookmarks.** Pure client-side/session state.
- [ ] **Multi-pattern "mark" highlighting** (Notepad++'s Mark feature, not
      to be confused with severity indicators above). Persistently
      highlight all occurrences of one or more ad hoc patterns at once,
      each in its own color.

### Notes on decisions made — small toolbar toggles

- **CRLF can't be detected from a live CodeMirror `Line.text`.** CodeMirror's
  default line-separator matching (`/\r\n?|\n/`) treats a `\r\n` pair as a
  single separator and consumes the `\r` while splitting the document into
  lines — by the time a line can be inspected, the `\r` is already gone.
  `lib/whitespace-highlighting.ts`'s `findCrlfLineNumbers` instead scans the
  *raw fetched content string* before CodeMirror ever ingests it (same line
  numbering either way, since a `\r\n` pair collapses to one line break on
  both sides), and the CRLF glyph is rendered as a zero-width
  `Decoration.widget` appended after the line's last character rather than
  a `Decoration.replace` over a character that no longer exists in the
  document.
- **"Next/previous problem" and "next/previous bookmark" share one
  generic stepper** (`lib/line-cycle.ts`'s `nextLine`/`previousLine`) —
  wrap-around cycling through a sorted list of line numbers relative to a
  current position is the exact same operation either way; severity
  navigation's `nextProblemLine`/`previousProblemLine` are now thin
  aliases over it rather than a second implementation.
- **Reload releases the old scratch reference, not just fetches fresh.**
  `archive.py`'s scratch key is deterministic (hash of source id + path +
  member), so a reload is: call `/open` again (a fresh fetch, acquiring a
  new reference under the same key), then call `/close` once for the old
  reference — same "path/member, not the literal scratch key" release
  call `closeTab` already made, just triggered without closing the tab.
- **Bookmarks and wrap/show-whitespace live on the `Tab` object in
  Viewer.svelte, not inside `CodeMirrorPane`.** The pane is a singleton
  reused across tab switches (`content` changes, the component doesn't
  remount) — per-tab state has to live above it and flow down as props,
  the same shape `severityPatterns` already established, or switching
  tabs would show one tab's bookmarks/toggles against another's content.
- **Copy-with-line-numbers is additive, not a Ctrl+C replacement.** The
  browser's native copy already handles a plain-text copy of a selection;
  this is specifically for the "prefixed with line numbers" case, exposed
  as its own toolbar action (`CodeMirrorPane.copySelectedLines`) rather
  than intercepting Ctrl+C.

### Notes on decisions made — severity indicators

- **Rule's path-oriented glob compiler (`app/rules.py`'s `_compile_glob`)
  is not reused for pattern matching.** It's anchored (`^...$`) and
  segment-aware (`**/` = path segments, `*` doesn't cross `/`) — built for
  matching file *paths*, not scanning arbitrary text within a line. A
  "glob"-kind severity pattern is instead matched as a literal substring
  anywhere in the line, case-insensitive; a "regex"-kind pattern uses the
  pattern as-is, also case-insensitive. Both still reuse the same
  `PatternKind` enum and `re:` prefix admin convention as `Rule`, purely
  for UI/API consistency — the underlying matching semantics differ.
- **Raw-text paste mode, deliberately skipped for this feature.** `Rule`'s
  raw mode earns its keep because a source can have dozens of path rules;
  severity patterns are typically a handful per level, where a row-based
  editor is enough and a bulk-paste format would just add parsing
  complexity (per-line level tagging) for little benefit. Revisit if real
  usage shows otherwise.
- **Override, not merge, for per-source patterns** — same simplicity
  tradeoff as choosing not to invent new fallback rules: a source with any
  patterns of its own uses only those, full stop, rather than layering on
  top of the global set.

## Security hardening (pre-1.0)

Called out as its own section, not folded silently into other phases —
this project holds production credentials by design (CLAUDE.md's own
framing), and the explicit ask is to treat security posture as a
first-class, tracked requirement rather than an implicit assumption.
Everything below is a candidate, not yet triaged into "must-have before
1.0" versus "nice-to-have" — that pass still needs doing.

- [ ] Login rate limiting / brute-force lockout on `/auth/login` — no
      throttling exists today beyond argon2id's own hashing cost
- [ ] Security response headers on every response (CSP, X-Frame-Options,
      X-Content-Type-Options, Referrer-Policy; HSTS is a deployment-level
      concern wherever TLS actually terminates, per CLAUDE.md's "sit behind
      a reverse proxy" packaging note)
- [ ] Dependency vulnerability scanning in CI, blocking or at minimum
      reporting: `pip-audit`/`safety` (backend), `npm audit` (frontend),
      `govulncheck` (the Go agent) — three ecosystems, three tools
- [ ] Container image scanning (e.g. Trivy/Grype) for the published Docker
      image, plus an SBOM published alongside releases
- [ ] Automated dependency-update PRs (Dependabot/Renovate) across all
      three ecosystems — keeping current, not just detecting known-bad
- [ ] `CREDENTIAL_ENCRYPTION_KEY` rotation path — currently no documented
      or tooled way to rotate this key without losing access to every
      already-encrypted `Source.credential_ref`/`SSOProviderConfig.config`
- [ ] Session management UI: list a user's own active sessions
      (`AuthSession` already exists at the data layer) with the ability to
      revoke one remotely — useful on its own, and a prerequisite for any
      "someone else is logged in as me" incident response
- [ ] Optional TOTP/MFA for local accounts — SSO already delegates this to
      the IdP, but the local break-glass account (and any org that doesn't
      enable SSO) has no second factor today
- [ ] Audit log tamper-evidence (e.g. hash-chaining `AuditLog` rows) so a
      compromised admin account can't quietly edit history without it
      being detectable
- [ ] CSRF review across every state-changing endpoint — confirm the
      existing `SameSite=strict` session cookie is sufficient on its own,
      or add explicit CSRF tokens where it isn't
- [ ] A formal third-party security review or pentest before declaring 1.0
      — SECURITY.md's disclosure policy covers *reporting* a vulnerability;
      this is about actively looking for one before external users show up

### Notes on decisions made — audit findings fixed

A manual code-level security pass (SAST + live black-box testing against a
local instance, no external exploitation tools run against anything but
this project's own local instance) found and fixed three issues, filed as
GitHub issues (#49, #50, #51). The SSRF fix (#49, in `Alert.webhook_url`)
shipped alongside the alerting feature itself, since that's the branch the
vulnerable code lives on — see that feature's own notes for it. The other
two:

- **#50 — SSH connector trusted any host key, every connection, with no
  persistence.** `AutoAddPolicy()` alone only governs what happens for a
  host paramiko has *never* seen; paramiko itself already raises
  `BadHostKeyException` on a mismatch for a host it *has* on file —
  independent of policy. The actual bug was that nothing ever loaded or
  saved a known_hosts file, so every host looked "never seen" on every
  single connection. Fixed by persisting host keys across connections
  (`ssh_known_hosts_path`); `AutoAddPolicy()` itself was correct to keep.
- **#51 — Credential encryption used a single unsalted SHA-256 round (no
  KDF work factor) and the `"changeme"` default had no startup guard.**
  Fixed with PBKDF2-HMAC-SHA256 (600k iterations, OWASP's 2023 floor) and
  a persisted per-install salt (`credential_salt_path`), plus a `lifespan`
  check that now refuses to start at all if
  `CREDENTIAL_ENCRYPTION_KEY` is still `"changeme"`. **This changes the
  derived key for every existing deployment** — anything encrypted under
  the old derivation (source credentials, SSO client secrets) becomes
  undecryptable after upgrading. Acceptable pre-1.0 (CHANGELOG.md's own
  stated policy: "0.x releases may include breaking changes between
  minors") given this project "hasn't seen production traffic beyond the
  maintainer's own use" per the README, but this sharpens the existing
  `CREDENTIAL_ENCRYPTION_KEY` rotation-path item above from "nice to have"
  toward "actually needed soon" — the next deployment to actually hold
  real credentials at any scale will want a rotation/re-encrypt tool
  before its next KDF change, not after.

## Ideas worth considering (not yet triaged into a phase)

Raised while discussing what else belongs on this roadmap — real candidates,
not commitments, and not yet placed into a specific phase:

- **Bulk source import/export** (CSV or YAML) — CLAUDE.md's own framing
  ("dozens of customers and dozens of sources each") implies a scale where
  creating every source one-by-one through the UI becomes the bottleneck;
  an import/export path (and maybe a documented config-as-code story) is
  high-value at that scale.

### Notes on decisions made — bulk source import

- **Format: YAML as primary, CSV as a secondary/simple option.** YAML
  handles the nested rule-list-per-source shape naturally; CSV works fine
  for the common case of "many sources, same rule set" but gets awkward
  once rules vary per row. No third (JSON) format aimed at humans — it's
  the same tree as YAML with worse ergonomics for hand-editing.
- **Ship a downloadable template** pre-filled with one example source (all
  fields, comments explaining each), plus a **dry-run/preview** step that
  validates and shows what would be created/changed before committing —
  no import applies blind.
- **Create-vs-update semantics**: keyed on source name within a customer;
  re-importing the same name updates that source rather than duplicating
  it, so the same file can be re-run idempotently as config-as-code.
- **Credentials: split structure from secrets, two separate uploads.**
  The structural import (protocol, host, base_path, rules, customer/folder
  placement — no credentials) is safe to commit to a repo, paste into a
  ticket, or hand to a teammate; it produces source shells plus an import
  batch id. A **separate, credentials-only upload** then keys credentials
  to those sources by name and funnels every value through the exact same
  `encrypt_credential` (Fernet) path the manual per-source UI already
  uses — no parallel credential-writing code path. The uploaded bytes are
  discarded immediately after the DB write completes (scratch, not
  storage, same as fetched log content); the endpoint is excluded from
  request-body logging; the preview step shows presence only ("SSH key:
  provided"), never values; `AuditLog` records a count summary ("imported
  12 sources, 9 with credentials"), never payload. Treat the accept as
  one-time, same discipline as temporary passwords and agent enrollment
  tokens.
- **The gold-standard option: external secret-manager references, not
  inline values.** Deferred past v1, but worth designing properly since
  it's the strongest answer to "bulk-import credentials without them ever
  being typed/pasted into a file a human handles." Row-level credential
  fields become a reference string instead of a value — e.g.
  `secretref:kv/data/<customer-slug>/<source-name>#ssh_key` — resolved
  through a new `SecretResolver` interface, mirroring how `AuthProvider`
  already abstracts local vs. OIDC vs. SAML in this codebase (concrete
  implementations: Vault KV first, since it's the common self-hosted
  choice for this audience; cloud secret managers later if requested).
  Two tiers, increasing in how much this actually removes from PerchTail's
  own at-rest footprint:
  - **Tier 1 — resolve-at-import.** The resolver fetches the referenced
    value once, at import time, then feeds it straight into the existing
    `encrypt_credential` (Fernet) path — same storage model as today,
    the only change is that no human ever handles the raw secret; the
    import file only ever contains references, safe to commit/share like
    the structural file.
  - **Tier 2 — resolve-at-use (live passthrough), the actual gold
    standard.** PerchTail stores only the reference, never a Fernet blob,
    for that source; the relevant connector (ssh/smb/winrm) resolves the
    live value from the external secret manager at connection time and
    discards it immediately after, same as everything else in the
    always-fresh model. This shrinks PerchTail's own stored-secret
    surface to effectively nothing for sources onboarded this way — the
    one thing still stored is PerchTail's own credential to reach the
    secret manager, which is a single shared secret rather than one per
    source, a much smaller blast radius if it were ever compromised.
    Heavier to build (needs the secret manager reachable at connection
    time, not just import time, and its own connection-failure handling
    distinct from a source being unreachable) — worth it only once this
    audience is actually asking for it.

- **A lightweight, scheduled connectivity-check sweep**, distinct from the
  existing on-demand manual `/check` — writing a short history of
  reachability per source. This would double as real data behind the
  monitoring-endpoint item above ("which sources have been flaky in the
  last 24h"), not just a point-in-time poke.
- **API-first admin tooling** (a thin CLI, or simply excellent API docs)
  for scripting source/rule management — the same target audience
  (support/DevOps engineers managing many customers) is likely to want
  GitOps-style, scripted control over sources/rules at some point.

## Open decisions

Carried over from CLAUDE.md — revisit as the relevant phase approaches rather
than deciding speculatively now:
- Raw-text rule paste mode UX
- Ephemeral scratch location: plain disk vs tmpfs/ramdisk
- Audit log retention *number* (the mechanism is decided — admin-configurable
  from the frontend, its own purge job — see "Full audit log viewer" above;
  what default/range to offer is still open)
- Whether SAML is needed at all
- Whether to index `.zip`/`.tar.gz` archive members for full-text search,
  and how deep (see the Phase 3 full-text search notes above) — deferred,
  not needed for the initial opt-in, plain-files-and-gz version
- Whether the built-in log viewer filters DEBUG-level files by default
- `CREDENTIAL_ENCRYPTION_KEY` rotation story (currently: none — rotating it
  means re-entering every source's credentials)

## Community, once Phase 1 is real

From CLAUDE.md's "Community & discoverability" — not blocking Phase 1 code,
but worth doing deliberately once there's something to show:
- [ ] Screenshots/demo GIF in the README
- [ ] Submit to `awesome-selfhosted` and similar lists
- [ ] GitHub topics: `self-hosted`, `log-viewer`, `rbac`, `devops`
- [ ] Launch: r/selfhosted, r/devops, Hacker News "Show HN"
