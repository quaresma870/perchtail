<p align="center">
  <img src="frontend/public/favicon.svg" alt="PerchTail logo" width="96" height="96">
</p>

<h1 align="center">PerchTail</h1>

> Live, rule-scoped log browsing across Linux and Windows servers — agentless by default, nothing mirrored, nothing left behind.

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-v0.7.1%20%28pre--1.0%29-blue.svg)](#status)
[![CI](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml/badge.svg)](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml)

**Contents:** [Status](#status) · [What it is](#what-it-is) ·
[Why not just use X](#why-not-just-use-x) ·
[Feature comparison](#feature-comparison) · [Screenshots](#screenshots) ·
[Quick start](#quick-start) ·
[Deployment: reverse proxy + TLS](#deployment-reverse-proxy-nginx--tls) ·
[Documentation](#documentation) · [License](#license)

## Status

🟢 **v0.7.1** is the current release: usable today, still pre-1.0.

- **Sources:** SSH/SFTP, SMB and WinRM without installing anything on the server (WinRM also through a read-only JEA endpoint that needs no administrator account), and a Go push agent for hosts that can't be reached inbound. A source can read several folders. Every open is a fresh fetch; nothing is mirrored.
- **Viewer:** one explorer across every source, as a list or a tree, with each file's size, a status dot per source, folders that refresh themselves and an advanced filter across sources and folders. A CodeMirror editor with tabs from several servers, Tail that fetches only the new lines, one find bar with Find all over files of any size, compare, beautify, highlights and bookmarks. One search box finds sources and file names across every source, from a file-name index that stores no content. Live alerts read only what was appended and call a webhook. Redaction hides secrets, card numbers and personal data in what users open, Tail and download, on the server or on the push agent's host.
- **Access:** roles scoped by customer, folder and source; local accounts with a password policy (NIST defaults), TOTP, required for administrators and optionally for everyone, and optional password recovery by email; OIDC single sign-on with group-to-role mapping; an access report of who holds what, where, with flags for privileged, inactive and MFA-less accounts, on its own Settings page and as JSON or CSV, for access reviews.
- **Operations:** settings in a sidebar in five sections (Sources, Access, Content, Operations, Audit), with an icon for each page, edited in popups, an update check, liveness and readiness probes, a detailed health endpoint with Prometheus metrics and Zabbix templates, an audit log that is hash-chained, verified daily and records where each action came from, including refused requests, bulk YAML import and export, server-side limits on listings and downloads running at once (Settings → Explorer and limits), and a system SMTP server for email, always over verified TLS (Settings → Email).
- **Security work so far:** login lockout, CSP and security headers, credential encryption with key rotation, SSH host-key pinning, session revocation, a DNS-rebind-safe webhook fetcher, encrypted online backups, push agents upgraded from the portal only to binaries signed with the release key, signed images and release files, CodeQL, gosec, Bandit, zizmor, fuzz and property tests in CI.

**What's next.** 0.7 is the last feature release before 1.0. 1.0 adds PostgreSQL support and otherwise validates what is there: load tests and a sizing guide, security controls in CI and releases, and an internal pentest. High availability, user groups, temporary access, notifications, API tokens and an external pentest come after 1.0. See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

**Support.** Which releases get fixes, and for how long, is in [SECURITY.md](SECURITY.md#support-and-end-of-life).

Tested with `pytest` (backend), `vitest` (frontend units) and a Playwright suite that drives the real UI against real SSH and SMB servers, and every release candidate is retested in a lab with Linux, Windows and push-agent sources before the release. SAML isn't built (OIDC covers Entra ID, Okta, Google Workspace and Keycloak), and PerchTail hasn't seen production traffic beyond the maintainer's own use.

## What it is

PerchTail gives support and ops engineers one place to browse, open, and download
log files live from a mix of Linux and Windows servers — without installing
anything on those servers, and without permanently copying their content anywhere.

You configure **sources** (a host and how to reach it — SSH/SFTP, SMB, WinRM, or a push agent), attach **rules** (which files and folders are visible, glob or regex, evaluated in order), and PerchTail lists and fetches matching files live, opened in a Notepad++/VS-Code-style viewer and downloadable as single files or zipped folders. A `.zip`, `.tar.gz` or `.tgz` archive opens as a folder whose files go through the same rules; an archive with a file whose last matching rule is an exclude can't be downloaded whole (its other files still open and download one by one; with [redaction](docs/redaction.md) applied to downloads, it is rebuilt without them instead), and a folder's ZIP leaves it out and lists it in `SKIPPED.txt`. Nothing is proactively mirrored or cached across views — every open is a fresh fetch, because the logs behind it are still being written.

RBAC is built in from day one: access is scoped per customer/environment, not just
per user, so a support engineer working one account can't accidentally browse
another's production logs.

## Why not just use X

- **Graylog / OpenSearch / Wazuh** — built around search over an ingested,
  indexed copy of your data. Great if you want that; overkill if you just want to
  look at what's actually on the box right now, and archiving-to-disk is often
  gated behind enterprise tiers.
- **rclone / rsync** — excellent at rule-based selective access to remote files,
  but no viewer, and built to copy rather than to browse-and-discard.
- **Filebrowser / Filestash / code-server** — good viewers, but no rule-scoped,
  cross-protocol, multi-source access with RBAC on top.

PerchTail is the missing middle: rule-scoped live access plus a real viewer, with
nothing sitting around afterward for someone to leak or for disk to fill up with.

## Feature comparison

| | PerchTail | Graylog | Wazuh | rclone + a file browser | Loki |
|---|---|---|---|---|---|
| Live view, nothing stored | ✅ | ❌ ingests a copy | ❌ ingests a copy | ❌ mirrors to disk | ❌ ingests a copy |
| Agentless Linux + Windows | ✅ SSH/SFTP, SMB, WinRM | needs Beats/NXLog agents | needs an agent | rclone remotes | needs Promtail agent |
| Customer/environment-scoped RBAC | ✅ built in | limited / enterprise | role-based, not scoped this way | ❌ | limited |
| SSO | ✅ OIDC (SAML 🚧 if needed) | enterprise tier | ✅ | ❌ | via Grafana |
| Code-editor-style viewer | ✅ CodeMirror | search UI, not a file viewer | search UI | depends which browser | Grafana Explore |
| Setup | single docker-compose | multi-service | multi-service | multiple tools glued together | multi-service |

This table reflects the design as of this writing and each project evolves —
verify anything that matters to your decision against current docs.

## Screenshots

Sources, with their customer and folder, protocol, status and credential age, and the actions on each row:

![Settings, Sources: the sidebar of Settings sections on the left, and a table of sources for the customers Northwind, Contoso and Fabrikam over SSH, SMB, WinRM and a push agent, each reachable or, for the push agent, never connected, with the age of its credential, its rules and Edit and Delete buttons](docs/images/screenshot-sources.png)

The viewer: the explorer with file sizes and a status dot per source, a log open with its error line tinted, the find bar ("1 of 65") and Find all listing every matching line below:

![Viewer: the explorer lists the sources with a status dot each and the files of web-01 with their sizes; app.log is open with its ERROR line tinted, the find bar searching ERROR, and the Find all panel below listing the 65 matching lines with their line numbers](docs/images/screenshot-viewer.png)

A role in its editor, opened over the roles list: global capabilities and access grants, the most specific scope winning, from customer down to a single source:

![Role editor in a popup over the roles list: the name Support engineers, switches for the global capabilities with View audit log and alerts turned on, and access grants for the customers Northwind and Contoso, the folder Production and the source files-01, with their capabilities](docs/images/screenshot-role-editor.png)

Settings, in five sections in the sidebar, each page with its own icon, here Explorer and limits:

![Settings, Explorer and limits: the sidebar with the Sources, Access, Content, Operations and Audit sections and an icon for each page, the Explorer card with the connections view, file order, folder refresh and Tail interval, and the Limits card with the concurrency limits and entries per listing](docs/images/screenshot-settings.png)

A grant gives, in its customer, folder or source, any of `view` (browse and open files), `download` (whole files and folder ZIPs), `manage_rules` (edit the visibility rules) and `manage_source` (shown as "Manage sources": change the sources and folders there). The `create_source` global capability adds sources and folders where the role has `manage_source`; changing, moving or deleting an existing source (its settings, agent token, saved credential and agent upgrades), or moving or deleting a folder, also needs `manage_source` on it (and, to move it, where it goes), and a move can't give the person moving it more access than they had; renaming a folder needs `manage_source` on it, and renaming or deleting a customer needs `manage_source` in a grant on that customer. Only a super admin creates customers, and everyone else sees only the customers and folders their grants reach. `manage_rules` only governs the rules. Short of super admin, someone with `manage_users` or `manage_roles` can't change their own role or edit the role they hold, and can only hand out roles and grants within their own: no global capability they lack, and no capability in a customer, folder or source that they don't hold there themselves.

## Quick start

```bash
git clone https://github.com/quaresma870/perchtail.git
cd perchtail
cp .env.example .env
```

Edit `.env` and set a real `CREDENTIAL_ENCRYPTION_KEY` — this encrypts SSH/SMB/
WinRM credentials at rest, so don't ship the placeholder:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

This walkthrough browses PerchTail over plain `http://localhost:8080` to get
you looking at the UI quickly — for that to work, also set
`SESSION_COOKIE_SECURE=false` in `.env` first. Leave it at its default
(`true`) for any real deployment: browsers silently drop a `Secure` session
cookie over plain HTTP, so with the default left on, login would appear to
just bounce back to the login page with no error. See
[Deployment: reverse proxy + TLS](#deployment-reverse-proxy-nginx--tls)
below for how to run this for real, with `SESSION_COOKIE_SECURE` left at its
secure default.

Then bring it up:

```bash
docker compose up -d
```

**Who can reach it.** `docker-compose.yml` publishes port 8080 on `127.0.0.1` only, so PerchTail answers on this host and nowhere else. Other machines reach it through a reverse proxy on this host that terminates TLS ([Deployment: reverse proxy + TLS](#deployment-reverse-proxy-nginx--tls)). To publish it on every address instead, for a test without a proxy, change the port in `docker-compose.yml` to `"8080:8000"`.

**How it runs.** The container's root filesystem is read-only (PerchTail writes only under `/data` and to a `/tmp` in memory), it can't gain privileges (`no-new-privileges`), and it starts with every Linux capability dropped except the few its entrypoint needs for one step: handing `/data` over to PerchTail's user (uid 10001) when the container starts as root, before it drops them all and starts the app. The preferred way skips that step and runs as PerchTail's user from the start: uncomment `user: "10001:10001"` in `docker-compose.yml` and remove its `cap_add` list. A new volume already belongs to that user. A volume made by an older release may hold files that belong to root: fix them once, before the change, while `cap_add` is still there:

```bash
docker compose run --rm --user root --entrypoint chown perchtail -R 10001:10001 /data
```

With `docker run`, the same is `--user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp`, with the volumes owned by uid 10001.

To run PerchTail on Kubernetes instead, with the Helm chart in `charts/perchtail/`, see [docs/kubernetes.md](docs/kubernetes.md).

This builds the image from the checkout. To run a published release
instead, replace `build: .` in `docker-compose.yml` with the image of that
version (amd64 and arm64), for example:

```yaml
    image: ghcr.io/quaresma870/perchtail:0.7.1
```

Each [GitHub release](https://github.com/quaresma870/perchtail/releases) also carries the push-agent binaries, their `SHA256SUMS`, `SHA256SUMS.sig` (its Ed25519 signature with the release key), the image's SBOM, and `perchtail-image.json` with `perchtail-image.json.sig`, which name the release's image by digest and are signed with the same key, all attached before the release is published. The image carries the same signed agent binaries, for upgrading agents from the portal. `latest` and `X.Y` tags only move when a full release is published, never with a pre-release, and `latest` only to the newest full release: a fix for an older line (for example a 0.6.x after 0.7.0) moves its own `X.Y` tag and leaves `latest` where it is.

On first startup (only when the database has zero users), PerchTail creates a break-glass super-admin account with a randomly generated password. The password is never logged: it is written to `initial-admin-password` in `DATA_DIR` (`/data/db` in the container), readable only by PerchTail's user, and the log says where:

```bash
docker compose exec perchtail cat /data/db/initial-admin-password
```

Open `http://localhost:8080`, sign in with that username/password (`admin` by default — override with `INITIAL_ADMIN_USERNAME` in `.env` before first startup), and you'll be forced to set your own password immediately; the file is deleted when you do. Super admins and every local account whose role has a global capability must then set up two-factor authentication (TOTP) before they can do anything else. From there: **Settings → Sources → + Add source** to point PerchTail at a server (see [docs/source-setup.md](docs/source-setup.md) for what the source side needs configured first), attach a rule so something is actually visible (a source with zero rules shows nothing, by design), and open **Viewer** to browse it.

State (the SQLite database, rotated application logs, and the ephemeral
scratch cache) lives in the `perchtail-data` Docker volume, so it survives
`docker compose down`/`up` — only `docker compose down -v` discards it.

**Backups and upgrades.** Everything stateful lives on the `perchtail-data`
volume (`/data` in the container), one directory per kind of data:

| Directory | Setting | Contents | Back up? |
|---|---|---|---|
| `/data/db` | `DATA_DIR` | SQLite database, credential-encryption salt (`credential_salt`), SSH `known_hosts`, the first super admin's temporary password (`initial-admin-password`, until it is changed) | yes |
| `/data/logs` | `LOG_DIR` | the app's own logs, rotated by size and day | optional |
| `/data/scratch` | `SCRATCH_DIR` | short-lived fetches, purged on close and emptied at startup | no |
| `/data/backups` | `BACKUP_DIR` | encrypted backups made by `python -m app.backup` | copy them off the host |

Each can be mounted as its own volume, and in production they should be:
`docker-compose.yml` has the three volumes ready to uncomment. Then a burst
of downloads filling scratch, or of DEBUG logging, can't fill the
database's disk. PerchTail also refuses a new fetch (HTTP 507) when scratch
is full or its disk is down to `SCRATCH_MIN_FREE_GB` / `SCRATCH_MIN_FREE_PERCENT`
free (1 GiB or 5%, whichever is larger). Keep `/data/db`
on a local disk, not a network share: the database runs in SQLite's WAL
mode, which needs one. Upgrading from a release before 0.5.0 moves the database, salt and
`known_hosts` from `/data` into `/data/db` on first start; see the
CHANGELOG's upgrade notes.

SQLite is the default. PerchTail can also use an empty PostgreSQL 18 database, with `DATABASE_URL=postgresql://...` (or `DATABASE_URL_FILE` for a secret) and the server's certificate verified over a network; `/data/db` then still holds the salt and `known_hosts`. See [docs/configuration.md](docs/configuration.md#postgresql). `python -m app.migrate_database` moves an existing SQLite installation there with its data ([docs/upgrade.md](docs/upgrade.md#moving-from-sqlite-to-postgresql)). `python -m app.backup` supports a SQLite database only.

Back up with `python -m app.backup` while PerchTail runs: it puts a consistent copy of the database, the salt and `known_hosts` into one file, always encrypted (with a key derived from `CREDENTIAL_ENCRYPTION_KEY`, or from an optional `BACKUP_ENCRYPTION_KEY`), checks it, and restores it with PerchTail stopped. Settings → Backups and updates also makes one on a schedule: every 24 hours on a new install. The database and `credential_salt` are useless without each other, and so is the salt without your `CREDENTIAL_ENCRYPTION_KEY`: keep the keys somewhere you can reach without this host. Before an upgrade, make a `pre-upgrade` backup; restoring it, with the previous image, rolls the upgrade back:

```bash
docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade
docker compose stop perchtail
# set the new image tag in docker-compose.yml, then:
docker compose pull && docker compose up -d
```

Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before.

[docs/backup-and-restore.md](docs/backup-and-restore.md) covers scheduled backups, the keys, checking and restoring a backup, rolling back an upgrade, copies off the host and testing a restore.

Only one PerchTail may run per `DATA_DIR`: a second instance (or an `alembic upgrade` while one runs) refuses to start, and so does an older image on a database a newer version has already migrated (see [docs/monitoring.md](docs/monitoring.md#log-events-worth-alerting-on)).

**Update check.** Once a day, PerchTail asks the GitHub releases API which
release is the latest (one unauthenticated request; nothing about your
installation is sent, and pre-releases are never offered). Super admins see
the result under **Settings → Backups and updates**, card **Updates**, with the upgrade steps for
how this instance was installed. Turn it off there, or set
`UPDATE_CHECK=false` to make sure PerchTail never makes that request (for
example on a network without internet access). `HTTPS_PROXY` is honoured.
The source overview also shows each push agent's version, and flags agents
older than the server.

**Push-agent upgrades.** **Settings → Agents** (for roles with **Create new sources**) lists the push agents of the sources where the role holds **Manage sources**, which every action on an agent also needs, with their version, and upgrades them to the server's version: off by default, then manually (one agent, or every outdated one) or automatically inside a weekly window in the server's time zone, a few at a time. Each agent can follow that policy or have its own; after a number of failed upgrades in a row (3 by default) automatic upgrades skip an agent until it's resumed, and every attempt is kept with who asked, the versions and the result. The agent downloads the release from the server (the Docker image carries the signed agent binaries) or from GitHub, installs it only if the release signature and checksum verify and the version is newer than its own, and restarts into it; anything that fails leaves the current version running. Each host can refuse with `PERCHTAIL_REMOTE_UPGRADE=false`. Agents older than 0.7 are upgraded by hand once ([docs/agent-service.md](docs/agent-service.md#upgrading-by-hand)); until then their folders can still be browsed, but none of their files can be opened, followed or downloaded, and Settings → Agents marks them "Update required". See [agent/README.md](agent/README.md#upgrades-from-the-portal) for what the host needs (a writable binary folder, a service that restarts the agent).

## Deployment: reverse proxy (nginx) + TLS

**Never expose PerchTail's admin/viewer UI to the public internet without TLS termination and something in front of it** — it's the entry point to every source's credentials and every source's log content across every customer configured, gated only by the session cookie. `docker-compose.yml` publishes the app on `127.0.0.1:8080` only; for anything beyond this host, put a reverse proxy in front of it.

1. Keep the published port on loopback, as `docker-compose.yml` has it (`"127.0.0.1:8080:8000"`), so it's reachable through nginx but not directly from outside the host.

2. Set `PUBLIC_BASE_URL` in `.env` to the real external `https://` URL
   you're about to configure below (used to build the OIDC `redirect_uri`
   if you set up [SSO](docs/sso-setup.md), and by the agent protocol's
   `wss://` endpoint if you use [push-agent sources](docs/source-setup.md#agent-push-agent-for-hosts-not-reachable-inbound)).
   Leave `SESSION_COOKIE_SECURE` at its default (`true`) — nginx is about
   to terminate real TLS, so the browser will actually have an `https://`
   origin to set that cookie on.

3. An nginx server block terminating TLS and proxying everything to the
   app:

   ```nginx
   server {
       listen 80;
       server_name perchtail.example.com;
       return 301 https://$host$request_uri;
   }

   server {
       listen 443 ssl http2;
       server_name perchtail.example.com;

       ssl_certificate     /etc/letsencrypt/live/perchtail.example.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/perchtail.example.com/privkey.pem;

       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;

           # Needed for the agent protocol's persistent WebSocket connections
           # (wss://.../agent/connect) -- harmless no-ops for ordinary HTTP
           # requests, which never carry an Upgrade header in the first
           # place. Without proxy_read_timeout raised, nginx's 60s default
           # would silently drop an agent's otherwise-idle connection every
           # minute (it reconnects on its own, but there's no reason to make
           # it); skip this whole block if you don't use the Agent protocol.
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
           proxy_read_timeout 3600s;
       }
   }
   ```

   PerchTail doesn't need `X-Forwarded-Proto` trusted to behave correctly —
   its CSRF Origin-check only ever compares `Host`, deliberately never
   scheme, precisely so it works the same whether or not a proxy sits in
   front (see the CSRF notes in [docs/design-decisions.md](docs/design-decisions.md#sessions-and-csrf) if you're curious why). It's
   sent above anyway since it's a generally useful signal for nginx's own
   access logs.

4. `docker compose up -d` to pick up the port-binding change, then reload
   nginx. Sign in at `https://perchtail.example.com` — the break-glass
   account created on first startup still works exactly as in the Quick
   start above.

Running nginx in its own container instead of on the host works the same
way — attach it to a shared Docker network with the `perchtail` service and
`proxy_pass` to `http://perchtail:8000` directly, and drop the host port
publish in `docker-compose.yml` entirely rather than binding it to loopback.

The audit log records each request's client address. PerchTail only believes `X-Forwarded-For` from a proxy listed in `TRUSTED_PROXIES` (loopback only by default): with nginx in its own container or on another host, set `TRUSTED_PROXIES` to its address in `.env`, or every entry records the proxy's address instead of the client's (see [docs/audit-events.md](docs/audit-events.md#client-address)).

## Documentation

Installing and running PerchTail:

- [docs/install-from-source.md](docs/install-from-source.md) — running PerchTail on a Linux host without Docker, with a systemd unit
- [docs/kubernetes.md](docs/kubernetes.md) — running PerchTail on Kubernetes with the Helm chart: the encryption key's Secret, the main values, ingress and TLS, backups, upgrading
- [docs/upgrade.md](docs/upgrade.md) — upgrading PerchTail
- [docs/verifying-releases.md](docs/verifying-releases.md) — checking a release before you run it: the image's cosign signature, the build provenance of the image and the agent binaries, the signed checksums and the SBOMs
- [docs/configuration.md](docs/configuration.md) — configuration reference: environment variables and the system settings in each Settings section
- [docs/backup-and-restore.md](docs/backup-and-restore.md) — encrypted backups, scheduled backups, restoring, rolling back an upgrade and testing a restore
- [docs/monitoring.md](docs/monitoring.md) — the liveness and readiness probes and who may call them, the detailed health endpoint and its token, Prometheus `/metrics`, the Zabbix 7.0 and 8.0 templates, and the log events worth alerting on
- [docs/credential-key-rotation.md](docs/credential-key-rotation.md) — rotating `CREDENTIAL_ENCRYPTION_KEY` without losing access to what's already encrypted
- [docs/high-availability.md](docs/high-availability.md) — whether and how to run PerchTail behind a load balancer or on Kubernetes, and what lives in the process
- [docs/sizing.md](docs/sizing.md) — sizing guide: the hardware profiles and how they are measured with the load-test kit in `loadtest/` (its figures are still to be measured)
- [docs/troubleshooting.md](docs/troubleshooting.md) — troubleshooting and FAQ

Security:

- [docs/security-model.md](docs/security-model.md) — the trust boundaries, who can do what (capabilities, grants and the rule that administrators never widen access), sign-in, sessions and browser protections, keys and encryption, connections to sources, and what PerchTail expects from its environment
- [docs/hardening.md](docs/hardening.md) — a production checklist: keys, reverse proxy and TLS, `TRUSTED_PROXIES`, outbound connections, file permissions, NTP, sources, emergency accounts, MFA, monitoring and backups
- [docs/data-and-retention.md](docs/data-and-retention.md) — everything PerchTail keeps (database, files, scratch, logs, backups, the browser), how long and how it is removed, users' personal data, and every connection that leaves the installation and how to turn it off
- [docs/audit-events.md](docs/audit-events.md) — every event the audit log records, the fields of an entry, the client address behind a proxy, retention and integrity checks

Connecting sources:

- [docs/source-setup.md](docs/source-setup.md) — preparing a Linux or Windows server for PerchTail over SSH/SFTP, SMB or WinRM, the push agent, and local-disk sources
- [docs/examples/jea](docs/examples/jea/README.md) — the read-only JEA endpoint for WinRM sources without a remote shell or administrator account
- [agent/README.md](agent/README.md) — the push agent: configuration, download, upgrades from the portal and building it
- [docs/agent-service.md](docs/agent-service.md) — running the push agent as a service
- [docs/bulk-import.md](docs/bulk-import.md) — creating and updating customers, folders, sources and rules from a YAML file, and exporting them

Administration and use:

- [docs/admin-guide.md](docs/admin-guide.md) — administering PerchTail
- [docs/redaction.md](docs/redaction.md) — redaction of secrets, card numbers and personal data in file content: the built-in and custom rules, their false positives, where content is redacted and what redaction doesn't cover
- [docs/sso-setup.md](docs/sso-setup.md) — registering PerchTail as an OIDC client and mapping groups to roles, with notes for Entra ID, Okta, Google Workspace and Keycloak/Authentik
- [docs/user-guide.md](docs/user-guide.md) — using the explorer and the viewer

Project:

- [CONTRIBUTING.md](CONTRIBUTING.md) — how to get a dev environment running and submit changes
- [CLAUDE.md](CLAUDE.md) — architecture brief for contributors
- [docs/design-decisions.md](docs/design-decisions.md) — why PerchTail is built the way it is, including decisions that were later replaced, and the open items
- [frontend/README.md](frontend/README.md) — developing the web interface: dev server, checks, tests and build
- [ROADMAP.md](ROADMAP.md) — milestones and what's next
- [SECURITY.md](SECURITY.md) — how to report a vulnerability, and how fast fixes ship
- [CHANGELOG.md](CHANGELOG.md) — what's shipped, release by release

## License

PerchTail is free software under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only), from 0.7.0 on. You can use, change and share it, including running it for others; if you change it and let people use your changed version over a network, you have to offer them its source under the same license. Releases up to 0.6.4 were published under the MIT license and stay under it.

Contributions are accepted under the [Contributor License Agreement](CLA.md) (see [CONTRIBUTING.md](CONTRIBUTING.md)).

The PerchTail name and logo are trademarks; [TRADEMARKS.md](TRADEMARKS.md) says how they can be used, including by forks and modified versions.
