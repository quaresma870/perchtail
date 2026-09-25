<p align="center">
  <img src="frontend/public/favicon.svg" alt="PerchTail logo" width="96" height="96">
</p>

<h1 align="center">PerchTail</h1>

> Live, rule-scoped log browsing across Linux and Windows servers — no agents,
> nothing mirrored, nothing left behind.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-v0.2.0%20%28pre--1.0%29-blue.svg)](#status)
[![CI](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml/badge.svg)](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml)

**Contents:** [Status](#status) · [What it is](#what-it-is) ·
[Why not just use X](#why-not-just-use-x) ·
[Feature comparison](#feature-comparison) · [Screenshots](#screenshots) ·
[Quick start](#quick-start) ·
[Deployment: reverse proxy + TLS](#deployment-reverse-proxy-nginx--tls) ·
[Documentation](#documentation) · [License](#license)

## Status

🟢 **v0.2.0 — Phase 1 (MVP), Phase 1b (SSO), Phase 2 (push-agent), and
Phase 3 (full-text search, saved-search webhook alerts, IdP
group-claim-to-role auto-mapping, a detailed health endpoint) are all
complete** (see [ROADMAP.md](ROADMAP.md) for the full phase breakdown).
Agentless SSH/SFTP, SMB, and WinRM browsing, a Go push-agent for hosts
that can't be reached inbound, ephemeral fetch (nothing mirrored),
rule-scoped RBAC, OIDC single sign-on (local accounts still work
alongside it), and opt-in full-text search with alerting over indexed
sources all work end-to-end. The Viewer's home page is a two-column
"recent connections / all connections" dashboard with a search box
(folder/customer/host), and deployment-wide feature toggles live under
Settings → System, including a full admin audit log (every login and
source/rule/role/user/customer/folder/SSO/system-settings change, with
type/action/date filters and an admin-configurable retention window).

Most of a pre-1.0 security-hardening pass is done too — login lockout,
CSP/security headers, CI-blocking dependency and container-image
vulnerability scanning, `CREDENTIAL_ENCRYPTION_KEY` rotation with a
tooled migration path, salted PBKDF2 key derivation, persisted SSH
host-key pinning, a Sessions page for revoking a login remotely, a
DNS-rebind-safe outbound webhook fetcher, optional TOTP/MFA for local
accounts, and audit-log tamper-evidence (HMAC hash-chained, periodically
verified) — see ROADMAP.md's "Security hardening" section for what's
still open (mainly a formal third-party review). Covered
end-to-end by `pytest` (backend), `vitest` (frontend units), and a
Playwright suite that drives the real browser UI against real SSH/SMB
test servers, not just mocks. Still pre-1.0 otherwise: SAML isn't built
(OIDC already covers Azure AD/Entra ID, Okta, Google Workspace,
Keycloak/Authentik, so it's only getting built if a real need shows up),
and it hasn't seen production traffic beyond the maintainer's own use.
See [CHANGELOG.md](CHANGELOG.md) for what's actually shipped versus
planned.

## What it is

PerchTail gives support and ops engineers one place to browse, open, and download
log files live from a mix of Linux and Windows servers — without installing
anything on those servers, and without permanently copying their content anywhere.

You configure **sources** (a host and how to reach it — SSH/SFTP, SMB, or WinRM),
attach **rules** (which files and folders are visible, glob or regex, evaluated in
order), and PerchTail lists and fetches matching files live, opened in a
Notepad++/VS-Code-style viewer and downloadable as single files or zipped folders.
Nothing is proactively mirrored or cached across views — every open is a fresh
fetch, because the logs behind it are still being written.

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

Sources, grouped by customer, with a protocol badge and connection status per row:

![Sources list, showing SSH/SMB/WinRM sources grouped under two customers with protocol badges and status](docs/images/screenshot-sources.png)

The viewer: a lazy-loaded folder tree feeding a CodeMirror pane, with `[error]`/`[warn]`
tokens colored and error lines tinted:

![Viewer open on an app.log file, showing colored log-level tokens and a highlighted error line](docs/images/screenshot-viewer.png)

A role's access grants — most-specific-scope-wins, resolved from customer down to a
single source:

![Role editor showing toggle switches for global capabilities and a table of customer/source access grants](docs/images/screenshot-role-editor.png)

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

On first startup (only when the database has zero users), PerchTail creates a
break-glass super-admin account with a randomly generated password and prints
it once to the container logs — grab it before it scrolls away:

```bash
docker compose logs perchtail | grep initial_super_admin
```

Open `http://localhost:8080`, sign in with that username/password (`admin` by
default — override with `INITIAL_ADMIN_USERNAME` in `.env` before first
startup), and you'll be forced to set your own password immediately. From
there: **Settings → Sources → + Add source** to point PerchTail at a server
(see [docs/source-setup.md](docs/source-setup.md) for what the source side
needs configured first), attach a rule so something is actually visible (a
source with zero rules shows nothing, by design), and open **Viewer** to
browse it.

State (the SQLite database, rotated application logs, and the ephemeral
scratch cache) lives in the `perchtail-data` Docker volume, so it survives
`docker compose down`/`up` — only `docker compose down -v` discards it.

## Deployment: reverse proxy (nginx) + TLS

**Never expose PerchTail's admin/viewer UI to the public internet without
TLS termination and something in front of it** — it's the entry point to
every source's credentials and every source's log content across every
customer configured, gated only by the session cookie. `docker-compose.yml`
publishes the app directly on `8080` for the [Quick start](#quick-start)
above; for anything beyond your own machine, put a reverse proxy in front
of it and stop publishing that port to anything but `localhost`.

1. Bind the published port to loopback only, so it's reachable through
   nginx but not directly from outside the host — edit `docker-compose.yml`:

   ```diff
   -      - "8080:8000"
   +      - "127.0.0.1:8080:8000"
   ```

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
   front (see ROADMAP.md's CSRF-review notes if you're curious why). It's
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

## Documentation

- [CLAUDE.md](CLAUDE.md) — full design/architecture reference and build plan
- [ROADMAP.md](ROADMAP.md) — phased milestones and what's next
- [docs/source-setup.md](docs/source-setup.md) — how to prepare a Linux or
  Windows server so PerchTail can reach it over SSH/SFTP, SMB, or WinRM
- [docs/sso-setup.md](docs/sso-setup.md) — registering PerchTail as an OIDC
  client and setting up group-to-role mapping, with per-provider notes for
  Azure AD/Entra ID, Okta, Google Workspace, and Keycloak/Authentik
- [docs/monitoring.md](docs/monitoring.md) — the detailed health endpoint for
  external monitoring (Zabbix, Prometheus), and how to generate its token
- [docs/credential-key-rotation.md](docs/credential-key-rotation.md) — how to
  rotate `CREDENTIAL_ENCRYPTION_KEY` without losing access to already-
  encrypted credentials
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to get a dev environment running and
  submit changes
- [SECURITY.md](SECURITY.md) — how to report a vulnerability
- [CHANGELOG.md](CHANGELOG.md) — what's shipped, release by release

## License

MIT — see [LICENSE](LICENSE).
