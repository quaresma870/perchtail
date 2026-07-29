# PerchTail

Name settled after checking several rounds of alternatives for collisions —
"logmirror" no longer fit once the design moved away from persistent mirroring
(see "Live browsing & ephemeral fetch behavior" below). PerchTail plays on
"fish for logs": a perch sits and watches without disturbing the water, which
matches the always-fresh, nothing-persisted model well, and it gives a natural
mascot/logo direction — a fish — for later branding work.

## What this is

A self-hosted, open-source tool that gives you one place to browse, open, and
download log files live from a mix of Linux and Windows servers — without
installing anything on the sources, and without permanently copying their content.

The person configures **sources** (a host + how to reach it), attaches **rules**
(which files/folders are visible, via glob or regex, evaluated in order), and the
tool lists and fetches matching files live through a web UI, opened in a
Notepad++/VS-Code-style viewer and downloadable as single files or zipped folders.
Nothing is proactively mirrored or cached across views — see "Live browsing &
ephemeral fetch behavior" below for why.

Built by a support/DevOps engineer who does forensic log analysis across many customer
production environments (mixed Linux + Windows, telecom/contact-center stack) and
wanted a lightweight, always-fresh viewing layer rather than a full SIEM. Intended to
be released publicly on GitHub under MIT.

## Why it exists

No existing open-source tool does exactly this combination:
- SIEM/log platforms (Graylog, OpenSearch, Wazuh, Loki) are built around *search*
  over an ingested/indexed copy, not live, rule-scoped, read-only browsing of the
  sources as they actually are right now.
- Sync tools (rclone, rsync) do selective, rule-based access to remote files well
  but have no viewer and are built to copy, not to browse-and-discard.
- File browsers (Filebrowser, Filestash, code-server) do the viewing well but don't
  do rule-scoped, cross-protocol, multi-source access with RBAC on top.

This project is the missing middle: rule-scoped live access + a real viewer, in one
small self-hosted app, with nothing sitting around afterward for someone to leak or
for disk to fill up with.

## Architecture

```
Linux sources (SSH/SFTP)  ─┐
                           ├─▶ Connector service ─▶ Ephemeral scratch ─▶ Web UI ─▶ browser
Windows sources (SMB/WinRM)┘     (rule engine,        (fetch-on-open,       (admin config
                                   protocol clients)    purge-on-close)       + CodeMirror-based
                                                                              viewer/download)
```

Core principle: **this tool only ever reads from sources, never writes to them, and
never keeps a persistent copy of their content.** Every browse is a live listing;
every open or download is a fresh fetch. Agentless in the MVP; a push-agent mode is
a later phase for sources that can't be reached inbound (a firewall/reachability
concern, independent of the always-fresh rule above).

## Tech stack (decided)

- **Backend**: Python 3.12+, FastAPI. Chosen specifically because the three protocol
  libraries needed (SSH/SFTP, SMB, WinRM) are more mature in Python than in Node or
  .NET — this is a protocol-heavy tool, so the ecosystem with the best clients wins.
  - `paramiko` — SSH/SFTP
  - `smbprotocol` or `impacket` — SMB (Windows shares)
  - `pywinrm` — WinRM/PowerShell remoting (fallback when SMB isn't open)
- **Scheduler**: APScheduler, in-process — used only for the ephemeral scratch
  idle-sweep (see below), not for any proactive collection since nothing is
  pulled on a schedule anymore.
- **Database**: SQLite. Single file, zero config, ships trivially in the Docker image.
- **Frontend**: embed CodeMirror 6 (MIT-licensed) directly into the app's own
  viewer pages — don't run a separate code-server container. Chosen over Monaco
  specifically because this is a log *viewer*, not a code IDE: CodeMirror reads
  as a plain text editor by default (no autocomplete widgets, code lens, or
  other IDE chrome to strip out), is lighter weight, and its virtualized
  rendering handles large rotated log files better than Monaco's, which starts
  degrading well before the sizes these logs actually reach. Syntax
  highlighting, tabs, in-file search/regex all still available, just requiring
  a bit more manual setup than Monaco's batteries-included languages — a
  worthwhile trade for a read-only log viewer that doesn't need language
  intelligence.
- **Future push-agent**: Go. Single static binary, cross-compiles for Windows and
  Linux from one codebase, tiny footprint — same reasoning as Filebeat/Promtail.
- **Packaging**: Docker + docker-compose. Should sit comfortably behind an nginx
  reverse proxy alongside other self-hosted internal tools.
- **Logging**: `structlog`, JSON output, request-scoped context (see "Application
  logging" section below).
- **License**: MIT.

## Data model (v1)

- **Customer** — id, name (e.g. Vodacom Tanzania, Fidelidade, GermanCloud). Every
  customer-owned source belongs to exactly one customer — this is the unit RBAC
  grants scope to. Not used by system sources (see "Built-in log viewer" below).
- **Source** — id, name, customer_id (FK, nullable — null for system sources),
  protocol (`ssh` | `smb` | `winrm` | `local`), host, port, credential_ref
  (encrypted, nullable — not needed for `local`), base_path, enabled,
  schedule_cron, is_system (bool, default false)
- **Rule** — id, source_id, order, type (`include` | `exclude`), pattern,
  pattern_kind (`glob` | `regex`), notes

## Rule matching semantics (important — document this clearly in the README)

- Rules decide which files and folders are **visible when browsing a source live**
  — not what gets copied, since nothing is copied ahead of time anymore.
- Rules are evaluated **in order, last match wins** — same mental model as
  `.gitignore`. This is the one thing that confuses users in comparable tools
  (rsync/rclone) if left ambiguous, so be explicit about it everywhere: in the UI,
  in the docs, in error messages.
- Support both glob (`**`, `*`, `?`) and regex patterns. Default to glob; prefix a
  pattern with `re:` to switch that rule to regex.
- A source with zero rules matches nothing (explicit opt-in, not "show everything by
  default" — this tool touches production systems, default should be conservative).

## Live browsing & ephemeral fetch behavior

- **Browsing** a source lists directories/files live via the source's configured
  protocol, filtered through that source's rule chain — nothing outside a matching
  rule is ever listed or reachable.
- **Opening or downloading a file always fetches fresh from the source.** No
  caching or reuse, ever — not even for rotated/compressed files (`.gz`, `.zip`).
  The assumption that rotated archives are immutable once written turned out not
  to hold here: these directories keep gaining new members over time, so nothing
  is safe to treat as "done" and reused. One uniform rule, no exceptions: every
  open is a fresh fetch.
- **Compressed containers are virtual folders in the tree** — expanding a `.zip` or
  `.tar.gz` lists its members (via the standard-library `zipfile`/`tarfile`); opening
  one decompresses and fetches just that member into scratch. A plain `.gz` decompresses
  transparently and opens like any other file. Same always-fresh, purge-on-close rules
  apply to what comes out of an archive as to anything else.
- **Fetched content lives in a per-session scratch location, not a persistent
  archive.** It's purged:
  - on an explicit close signal from the UI (tab closed / file closed), and
  - via reference counting when the same file is open in more than one session at
    once — delete only when the last viewer closes it, and
  - via a periodic idle-sweep (e.g. every few minutes) that deletes anything
    untouched past a short threshold, as a backstop for crashed or disconnected
    clients that never send a close signal.
- **A size-guard on total scratch usage** (e.g. cap at N GB) evicts the oldest
  zero-reference entries first if concurrent load pushes past it. This is a safety
  valve for load, not a caching strategy — it never causes a file to be served from
  a prior fetch instead of a fresh one.
- Never delete, modify, or write anything back to a source. Read-only, always.
- Later: push-agent mode (Go binary) for sources where inbound SSH/SMB/WinRM isn't
  reachable — it watches folders locally and pushes matching files outbound over
  HTTPS to the connector, avoiding firewall/NAT problems entirely. This is about
  network reachability, not about the always-fresh rule above, which still applies.

## Access control (RBAC + SSO)

This is core to the project, not a phase-3 bolt-on — nearly every API endpoint needs
a permission check, so the model has to exist before most endpoints are written.

**Customer grouping is mandatory at this scale.** With dozens of customers and dozens
of sources each, per-source-only grants don't work — every role edit would mean
scrolling hundreds of toggles, and every new source added to an existing customer
would need manual re-granting across every affected role. Instead:

- A role grants access at the **customer** level by default (view / download / manage
  rules / run now).
- A customer-level grant applies **dynamically** to every source under that customer,
  including ones added later — this is the entire point of grouping.
- Per-source rows in the permission tree exist only for **exceptions** — an override
  that differs from the customer's default for one specific source.

**Roles are single, not multiple, per user.** One `role_id` on the `User` row. Since a
role is the *entire* definition of someone's access with no composition across roles,
expect the number of roles to grow with real access patterns (e.g. "Tier 2 — Vodacom +
Fidelidade, view only" as its own named role). Give the role editor a **duplicate role**
action from day one — cloning and tweaking a diff is much less painful than rebuilding
a grant tree from scratch each time. Also give the source-access tree in the role
editor a **search/filter box** — with dozens of customer groups, scrolling to find one
gets tedious fast; default everything collapsed.

**Grant resolution logic** (pseudocode):
```
if source.is_system: return user.role.is_super_admin  # no grant can reach a system source
if user.role.is_super_admin: allow everything
grant = RoleGrant.find(role=user.role, scope_type="source", scope_id=source.id)
if not grant: grant = RoleGrant.find(role=user.role, scope_type="customer", scope_id=source.customer_id)
if not grant: deny
return requested_capability in grant.capabilities
```

**Sign-in supports local accounts and SSO only** — no other auth methods. Build behind
an abstracted provider interface so SAML can be added later without reworking auth:
```
AuthProvider (interface)
  ├── LocalPasswordProvider   (always present — the break-glass super-admin account)
  ├── OIDCProvider            (build first — authlib; simpler protocol, covers Azure
  │                            AD/Entra ID, Okta, Google Workspace, Keycloak/Authentik)
  └── SAMLProvider            (later — python3-saml; more setup per IdP, XML signing,
                                metadata exchange)
```
- SSO always coexists with local auth — never remove the local break-glass account.
- One OIDC/SAML provider configured at a time for v1. Multi-IdP federation is a real
  feature some tools need, but only worth building if actually needed.
- SSO login auto-provisions a user with the no-access default role; an admin assigns
  the real role afterward. Auto-mapping IdP group claims to roles is a good phase-2
  automation, not required for v1.

**Data model additions for this section:**
- **Role** — id, name, is_builtin, is_super_admin, global_capabilities (manage_users,
  manage_roles, manage_sso, create_source)
- **RoleGrant** — id, role_id, scope_type (`customer` | `source`), scope_id,
  capabilities (`view`, `download`, `manage_rules`, `run_now`)
- **User** — id, username/email, password_hash (nullable for SSO-only accounts),
  role_id (FK, single), active, auth_provider, external_id, last_login_at
- **SSOProviderConfig** — id, protocol (`oidc` | `saml`), name, config
  (client id/secret or SAML metadata, encrypted), enabled
- **AuditLog** — id, user_id, action, target_type, target_id, timestamp, metadata —
  cheap to add now, painful to bolt on after this tool already holds production
  credentials. Log at minimum: login, source/rule create-edit-delete, file download,
  role/grant changes.

## Security notes

- Credentials (SSH keys, passwords, WinRM creds) must be encrypted at rest — key
  from an env var or mounted secrets file, never stored plaintext in SQLite.
- Password hashing via argon2id (`argon2-cffi`). Force password change on first
  login for admin-created local accounts.
- Deactivate (soft, keeps audit history) is the default way to remove access; hard
  delete should be gated behind confirmation and probably disallowed for any account
  with prior activity.
- Viewer access is read-only; only roles with the relevant capability can edit
  sources/rules or trigger runs — enforced via the grant resolution logic above.
- Never expose the archive or admin UI to the public internet without auth in
  front of it — call this out explicitly in the README.

## Application logging (distinct from AuditLog)

`AuditLog` (above) is a durable, queryable record of business actions, read via an
admin UI page. Application logging is the separate, much higher-volume operational
trail — connector timing, protocol errors, rule-evaluation detail, scratch
purge/sweep activity, stack traces — and needs levels because most of it is noise
outside of active troubleshooting. Every `AuditLog` write should also emit a
structured INFO-level log line, so ops can grep for an action without querying SQLite.

- **Library**: `structlog`, JSON output. Bind a `request_id` per HTTP request via
  middleware so every log line from that request correlates automatically —
  necessary once multiple users are hitting this concurrently.
- **Levels** (`LOG_LEVEL` env var, default `INFO`):
  - `DEBUG` — full protocol chatter, per-file rule-match evaluation detail
  - `INFO` — lifecycle events, successful fetch/open/download, successful logins
  - `WARNING` — permission denials, retried connections, fetches over a slow-fetch
    threshold, scratch size-guard evictions
  - `ERROR` — failed source connections, failed fetches, unhandled exceptions
  - `CRITICAL` — startup failures, DB unreachable, credential-store key missing
- **Destinations**: stdout/stderr always (`docker compose logs` works out of the
  box), plus a rotating file under a mounted volume so logs survive container
  recreation.
- **Rotation & retention are handled by the app, not the host's `logrotate`.**
  `logging.handlers.TimedRotatingFileHandler` (daily), a custom rotator hook that
  gzips on rotation, and `backupCount` driven by `LOG_RETENTION_DAYS` (default 30)
  to auto-delete anything older. This keeps the Docker image self-contained —
  no dependency on host-level `logrotate` or a cron sidecar to get working rotation.
- Never log credentials or full request/response bodies — same rule as everywhere
  else in this project.

## Built-in log viewer (dogfooding)

The app's own rotated logs (see "Application logging" above) are exposed through
the exact same viewer as customer sources, via a **system source**:

- Seeded automatically on first startup, pointed at the same `LOG_DIR` the
  rotating file handler writes to, protocol `local`, `is_system = true`,
  `customer_id = null`.
- The `local` connector reads directly from disk — no ephemeral scratch needed,
  since a file already on the same machine as the app is inherently zero-latency
  and always fresh. Only the remote connectors (ssh/smb/winrm) use scratch.
- Access is gated purely by `is_super_admin` (see the updated grant resolution
  above) — no role or grant, however permissive, can reach it short of that.
- Shown in the admin sources list with a "system" badge: non-editable,
  non-deletable, rules not user-configurable.
- Net-new code is small: one protocol enum value, one simple connector, one
  boolean flag, one bypass check in grant resolution — cheap enough to fold into
  phase 1 rather than defer, and a good validation that the connector
  abstraction is clean if a fourth protocol slots in this easily.

## Community & discoverability

Being technically solid doesn't get an open-source project used — it has to be
easy to find, easy to trust, and easy to contribute to. Baseline files (already
drafted, see repo root): `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`,
`LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/FUNDING.yml`.

A few things worth doing deliberately once phase 1 is real, not just present:

- **README honesty over hype.** The current README explicitly says "early
  development" and marks unbuilt features (SSO) as roadmap rather than done —
  keep that discipline. Overclaiming maturity is the fastest way to lose trust
  with exactly the security-conscious audience this project needs.
- **SECURITY.md matters more than usual here** — this tool holds production
  credentials by design, and a visible, serious vulnerability disclosure policy
  is a real trust signal for that audience, not boilerplate.
- **Screenshots/demo GIF in the README** once the viewer exists — a "show, don't
  tell" of the CodeMirror-based viewer and the role/permission tree is worth more
  than another paragraph of description.
- **Submit to `awesome-selfhosted` and similar curated lists** once there's a
  working release — this is how a lot of self-hosted tooling actually gets
  discovered, more than search traffic.
- **Launch venues worth considering at 1.0**: r/selfhosted, r/devops, Hacker
  News "Show HN." Skip Product Hunt — wrong audience for an ops tool.
- **GitHub topics/tags** on the repo (`self-hosted`, `log-viewer`, `rbac`,
  `devops`) — cheap, and meaningfully affects GitHub's own search/discovery.
- **Conventional commits from day one** (already in Conventions below) — this
  is what makes an automated CHANGELOG generation tool worth adding later
  instead of maintaining it by hand forever.

## Web UI

- **Admin**: Sources list (status, protocol, last run, rule count, run-now action),
  per-source rule editor — both a row-based UI and a raw-text/gitignore-style paste
  mode for power users — and run history with errors.
- **Viewer**: lazy-loaded folder tree (fetch a folder's children live, only on
  expand — never eagerly list an entire remote tree upfront, which would be slow
  over SSH/SMB for deep hierarchies) + CodeMirror-based pane with tabs, in-file search,
  and download (single file or a zipped folder), all fetched fresh per the rules
  above.
- **Roles**: role list, editor with global-capability toggles + customer/source
  access tree (search/filter box, collapsed by default), duplicate-role action.
- **Users**: list with active/inactive status, create user, reset password,
  deactivate/delete, assigned role.
- **SSO settings**: configure the active OIDC or SAML provider (client id/secret
  or metadata), test-connection action.

## Phased roadmap

1. **Phase 1 (MVP)** — agentless live browsing & on-demand fetch over SSH/SFTP,
   SMB, and WinRM; ephemeral scratch with refcounted purge + idle-sweep; SQLite;
   RBAC (customers, roles, grants, local auth, audit log); structured application
   logging (levels, rotation, retention) from the first commit, not bolted on
   later; built-in super-admin-only log viewer via the `local` protocol; admin
   CRUD for sources/rules; CodeMirror-based viewer; docker-compose deployment.
2. **Phase 1b** — SSO (OIDC first, via the abstracted provider interface).
3. **Phase 2** — Go push-agent for sources that can't be reached inbound; SAML
   provider added behind the same interface if actually needed.
4. **Phase 3** — full-text search, alerting, IdP group-claim-to-role auto-mapping.
   Note: full-text search needs its own answer, since there's no persistent copy
   of anything to grep anymore — likely a separate, lightweight background indexer
   that extracts and stores only text/metadata for search, distinct from (and not
   reusing) the viewing scratch space. Design this properly when phase 3 starts
   rather than assuming it falls out of the existing model for free.

## Suggested repo structure

```
perchtail/
  README.md
  CONTRIBUTING.md
  CHANGELOG.md
  SECURITY.md
  CODE_OF_CONDUCT.md
  .github/
    FUNDING.yml
  backend/
    app/
      main.py
      logging_config.py    # structlog setup, TimedRotatingFileHandler + gzip rotator
      models.py          # Customer, Source, Rule (SQLModel/SQLAlchemy)
      rules.py            # pattern matching + last-match-wins precedence engine
      scratch.py           # ephemeral fetch: refcounted purge, idle-sweep, size-guard
      collectors/          # per-protocol live listing + fetch, not proactive sync
        ssh.py
        smb.py
        winrm.py
        local.py            # built-in log viewer's connector, no scratch needed
      auth/
        models.py          # Role, RoleGrant, User, SSOProviderConfig, AuditLog
        rbac.py             # grant resolution logic + FastAPI permission dependency
        providers/
          local.py
          oidc.py
          saml.py           # phase 2
      api/
        sources.py
        rules.py
        archive.py        # browse/open/download endpoints
        users.py
        roles.py
        auth.py
    tests/
  frontend/
    (admin pages + CodeMirror-embedded viewer)
  agent/                  # phase 2 — Go push-agent
  docker-compose.yml
  Dockerfile
  LICENSE                 # MIT
  CLAUDE.md               # this file
```

## Conventions

- Python: type hints everywhere, Pydantic models for API request/response bodies,
  `ruff` + `black` for lint/format.
- Commits: conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`) since this
  is a public repo from day one.
- Tests: `pytest`; mock the SSH/SMB/WinRM clients in unit tests rather than hitting
  real remote hosts in CI.

## Open decisions (revisit as the project develops)

- Exact UX for the raw-text rule paste mode.
- Where the ephemeral scratch space should live — plain local disk (simplest) vs a
  tmpfs/ramdisk for speed, given it's fully transient either way.
- Audit log retention policy — keep forever, or expire after N months?
- Whether SAML gets built in phase 2 alongside the push-agent, or only if a real
  need for it shows up (OIDC covers Azure AD/Entra ID, Okta, Google Workspace,
  Keycloak/Authentik — SAML may simply never be needed).
- How phase 3 full-text search gets its content, since nothing persists — needs a
  dedicated indexing design, not a reuse of the viewing scratch space.
- Whether the built-in log viewer should filter out DEBUG-level rotated files by
  default (often noisy) or show everything, since only super admins see it anyway.

## First things to do in a new session

1. Scaffold the FastAPI project under `backend/` matching the structure above.
2. Set up SQLModel (or SQLAlchemy) models for `Customer`, `Source`, `Rule`,
   `SyncState`, `Run`, and the auth models (`Role`, `RoleGrant`, `User`,
   `SSOProviderConfig`, `AuditLog`) together — auth touches every endpoint, so it
   needs to exist before endpoints are written, not retrofitted after.
3. Build the grant-resolution permission dependency (`auth/rbac.py`) and the local
   auth provider first, with unit tests — this and the rule engine are the two
   pieces of logic in the whole project that must be correct before anything else
   is built on top of them.
4. Build the rule-matching engine (`rules.py`) with unit tests.
5. Wire up one connector (start with SSH/SFTP via `paramiko`, it's the simplest)
   end-to-end: live directory listing filtered by rules, fetch-on-open into
   `scratch.py`'s ephemeral store, purge on close — no incremental/staleness
   logic needed anywhere, since every open is always a fresh fetch by design.
   Apply the permission dependency to its API endpoints from the start.
