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
- [ ] Go push-agent (single static binary, cross-compiled) for sources not
      reachable inbound (in review — see the Go push-agent binary PR)
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

## Phase 3

- [ ] Full-text search — needs its own indexing design since nothing persists
      from the viewing scratch space; design before building, per CLAUDE.md
- [ ] Alerting
- [ ] IdP group-claim-to-role auto-mapping

## Open decisions

Carried over from CLAUDE.md — revisit as the relevant phase approaches rather
than deciding speculatively now:
- Raw-text rule paste mode UX
- Ephemeral scratch location: plain disk vs tmpfs/ramdisk
- Audit log retention policy
- Whether SAML is needed at all
- Full-text search's content source, given nothing persists
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
