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

### M5 — Remaining connectors
- See [docs/source-setup.md](docs/source-setup.md) for the SMB/WinRM-side
  prerequisites (scoped share + ACLs, JEA-constrained WinRM account) these
  connectors assume.
- [ ] `collectors/smb.py` (`smbprotocol` or `impacket`)
- [ ] `collectors/winrm.py` (`pywinrm`)
- [ ] `collectors/local.py` — no scratch needed, reads directly off disk

### M6 — Built-in log viewer (dogfooding)
- [ ] System source seeded on first startup, pointed at `LOG_DIR`,
      `is_system = true`, `customer_id = null`
- [ ] Access gated purely by `is_super_admin`, no grant can reach it
- [ ] "system" badge in the admin sources list; non-editable, non-deletable

### M7 — Admin & viewer UI
- [ ] Sources list (status, protocol, last run, rule count, run-now)
- [ ] Rule editor: row-based UI + raw-text/gitignore-style paste mode
- [ ] Run history with errors
- [ ] Lazy-loaded folder tree (fetch children on expand only)
- [ ] CodeMirror-based viewer pane: tabs, in-file search, download (single file
      or zipped folder)
- [ ] Roles UI: list, editor (global-capability toggles + customer/source
      access tree with search/filter, collapsed by default), duplicate-role
      action
- [ ] Users UI: list with active/inactive, create, reset password,
      deactivate/delete, assigned role

### M8 — Phase 1 exit
- [ ] docker-compose deployment documented and tested end-to-end
- [ ] README "Quick start" filled in with real steps (replacing the current
      placeholder)
- [ ] Security pass on credential storage (encryption at rest) and archive
      endpoints (path traversal)
- [ ] Tag `v0.1.0`

## Phase 1b — SSO

- [ ] `OIDCProvider` (`authlib`) behind the existing `AuthProvider` interface
- [ ] SSO settings admin page: configure active provider, test-connection
      action
- [ ] Auto-provision SSO users with the no-access default role
- [ ] Local break-glass account confirmed to keep working alongside SSO

## Phase 2

- [ ] Go push-agent (single static binary, cross-compiled) for sources not
      reachable inbound
- [ ] `SAMLProvider` (`python3-saml`) — **only if** a real need shows up; see
      the open decision in CLAUDE.md (OIDC already covers Azure AD/Entra ID,
      Okta, Google Workspace, Keycloak/Authentik)

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

## Community, once Phase 1 is real

From CLAUDE.md's "Community & discoverability" — not blocking Phase 1 code,
but worth doing deliberately once there's something to show:
- [ ] Screenshots/demo GIF in the README
- [ ] Submit to `awesome-selfhosted` and similar lists
- [ ] GitHub topics: `self-hosted`, `log-viewer`, `rbac`, `devops`
- [ ] Launch: r/selfhosted, r/devops, Hacker News "Show HN"
