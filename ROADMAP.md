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
- [ ] Full-text search: match on file path and source host/name too, not just
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

- **Working plan, not yet built**: make `file_path` an indexed FTS5 column
  (it's currently `UNINDEXED`, storage-only) so a single MATCH query can hit
  line content and file path together. FTS5's default `unicode61` tokenizer
  already folds case for matching, so this is case-insensitive by
  construction, same as content search already is today.
- **Host/source-name matching is the harder half**, since neither lives
  per-line in `search_index_fts` — a source's host is metadata on the
  `Source` row, not something naturally indexed alongside its lines. Two
  reasonable designs, not yet decided between:
  1. Denormalize `host` (and the source's display name) into every FTS row
     at index time, so one unified MATCH query and one ranked result list
     covers content + path + host together — simplest UX, but means an
     admin renaming a source's host doesn't reflect in search until that
     source's files are next re-indexed (same accepted-staleness spirit as
     the size-only change-detection already in place).
  2. Resolve host/name matches separately (a plain case-insensitive `LIKE`
     against `Source.host`/`Source.name`, no FTS involved) and surface them
     as their own small "sources matching" section in the UI, distinct from
     line-content hits — always current, no re-index lag, but a second
     results list instead of one ranked one.
  Leaning toward (1) for a simpler single-search-box experience, but this
  is worth a real look before building rather than assuming.

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

## Viewer: find in document

A Notepad++-style "Find All in Current Document" results panel — a list of
every match in the currently open file (line number + snippet), click to
jump — alongside, not instead of, the existing Ctrl+F inline highlight and
next/previous navigation (`@codemirror/search`, see the earlier Ctrl+F fix
in CHANGELOG.md).

- [ ] Results panel component listing every match with line number + a
      short snippet of surrounding text, most-natural-order (top to
      bottom of the document)
- [ ] Click a result to jump to it — reuses `CodeMirrorPane`'s existing
      `scrollToLine`-style jump, same mechanism the cross-file Search
      page's results list already uses today
- [ ] Existing inline Ctrl+F panel (highlight + next/previous) stays as
      is — this is an addition, not a replacement
- [ ] Some entry point to open the new panel (e.g. a "Find All" button
      inside the existing search panel, or its own shortcut) — exact UX
      still to be decided when this is built

### Notes on decisions made — find in document

- **No new dependency needed.** Neither CodeMirror's own search package
  nor any other editor library (Monaco, Ace) ships a results-list panel
  like this out of the box — Notepad++, VS Code, etc. all build it as
  custom UI on top of their editor's basic search primitives, same as
  we'd be doing. `@codemirror/search`'s `getSearchCursor` (or a plain
  regex scan over `view.state.doc`, mapping offsets to line numbers via
  `doc.lineAt`) is enough to collect every match; the panel itself is a
  new Svelte component.
- **Coexists with the current inline search, doesn't replace it** — per
  explicit direction. Two complementary tools: quick highlight-and-step-
  through for a single term, versus "show me everywhere this appears at
  once" for scanning a large log.

## Viewer: toward an advanced editor (not yet triaged into a phase)

Raised while discussing the find-in-document work — further steps toward
a fuller, Notepad++-like *viewing* experience. Explicitly **not** going
there: editing or saving changes back to a source. Raised and considered,
but dropped deliberately — it would contradict CLAUDE.md's core "read-only,
always" principle for no strong enough reason, and every item below stays
entirely display-only: none of it ever mutates the file being viewed or
writes anything back to the source, the same guarantee the existing
line-level highlighting and Ctrl+F search already hold to.

- [ ] **Per-file-type syntax highlighting.** Currently only log-level
      tokens are colored (`codemirror-theme.ts`'s `logLevelHighlighting`);
      there's no language-aware highlighting for e.g. `.json`, `.xml`,
      `.js` config/log files. `@codemirror/lang-javascript`,
      `@codemirror/lang-json`, and `@codemirror/lang-xml` are already
      installed dependencies but currently unused anywhere in the
      codebase — likely added in anticipation of this and never wired
      up. `@codemirror/language-data` (not yet installed) adds broader
      extension-based auto-detection beyond those three.
- [ ] **Compare files (diff view).** Read-only side-by-side or unified
      diff between two open files or two versions of the same rotated
      log (`app.log` vs `app.log.1`, etc.) — a genuinely useful forensic
      feature for this audience. `@codemirror/merge` (the official
      CodeMirror 6 diff/merge extension, not yet installed) is the
      natural fit given the project is already all-in on CodeMirror.
- [ ] **More severity indicators + jump-to-next-problem navigation.**
      Today's `logLevelHighlighting` only tints bracketed `[error]`/
      `[fatal]` tokens and only line-tints on the bare words "error"/
      "fatal" (`codemirror-theme.ts`'s `LEVEL_TOKEN`/`ERROR_LINE`) — no
      "jump to next one" command exists, and coverage is narrow (no
      `warn`/`warning` line-tint, no `critical`/`severe`/`panic`/
      `exception`/`traceback`-style markers common across other
      ecosystems' log formats). Two tiers:
      1. Broaden the built-in pattern set and add a next/previous-problem
         step command (conceptually like an IDE's "next diagnostic"),
         stepping through everything at warn-or-worse severity — the
         existing color-coding still shows which severity each stop is.
      2. Later, and only if the built-in set proves too narrow in
         practice: user-configurable custom indicator patterns, glob/regex
         the same way the `Rule` engine already works, rather than a
         second, differently-shaped pattern mechanism.
- [ ] **Line-wrap toggle.** Log lines are often very long (embedded JSON,
      long messages); a simple wrap/no-wrap switch
      (`EditorView.lineWrapping`) is cheap and directly useful.
- [ ] **Beautify / minify for embedded JSON, XML, and (lower priority) JS.**
      A display-only reformat of the currently-open content or a
      selection — never touches the file on disk, purely a client-side
      re-render, same guarantee as line-wrap. Two distinct shapes worth
      keeping separate when this gets designed: reformatting a whole file
      that already *is* JSON/XML (a minified config file, say) versus
      reformatting just one embedded JSON blob inside a bigger log line
      (structured-logging lines are common; a whole-line beautify-in-place
      is probably the more-used case day to day). JSON needs no library
      (`JSON.parse`/`stringify(obj, null, 2)` natively); XML has no native
      browser formatter, so either a small zero-dependency formatter
      function or a tiny package (e.g. `xml-formatter`) — pick when this
      gets built, not speculatively now. JS beautify is listed but lower
      priority: logs rarely carry embedded minified JS the way they carry
      JSON/XML, so it's worth confirming there's a real use case before
      building it rather than assuming parity with the other two.

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
- Audit log retention policy
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
