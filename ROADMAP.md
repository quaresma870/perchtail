# Roadmap

What comes next for PerchTail, without dates: progress is measured by what is merged. What has shipped is in [CHANGELOG.md](CHANGELOG.md); why things are built the way they are, including decisions that were later replaced, is in [docs/design-decisions.md](docs/design-decisions.md). Each item links its GitHub issue where it has one; the plan is also tracked as the milestones [0.7](https://github.com/quaresma870/perchtail/milestone/1) and [1.0](https://github.com/quaresma870/perchtail/milestone/2).

Which releases get fixes, and for how long, is in [SECURITY.md](SECURITY.md#support-and-end-of-life).

## Principles that limit scope

- PerchTail only reads from sources. It never writes, deletes or changes anything on them.
- Nothing from the logs is kept. Content is fetched fresh into scratch and deleted; only file names, sizes and modification times are indexed.
- Access is denied unless a grant allows it, and every grant is scoped to a customer, a folder or a source.
- One process per `DATA_DIR` and per database. SQLite stays the default database.
- 0.7 is the last feature release before 1.0. 1.0 validates and hardens what is there, with PostgreSQL support, a Helm chart and the few viewer additions listed under 1.0.0.
- Every release goes through a release candidate retested in a lab with Linux, Windows and push-agent sources first.
- The HTTP API is internal to the web UI and may change before 1.0.
- Bulk import is additive: it never deletes anything and never exports credentials.
- Work without an issue (SAML, a further high-availability tier) waits for a real need; see the open items in [docs/design-decisions.md](docs/design-decisions.md).

## 0.7.0 (released)

The last feature release before 1.0, released as 0.7.0 ([CHANGELOG.md](CHANGELOG.md)). It shipped:

- The content index and the full-text Search page are removed, so nothing from the logs is stored (#303).
- Alerts read new lines live, with webhook retries (#305).
- A file-name index, on by default (#304), and one search in the explorer for sources, folders and files (#300).
- Bulk credential update and credential age (#248).
- Push-agent upgrades from the portal, with signed binaries, an upgrade window and a failure limit (#319).
- Limits on connector work, a free-space check before a fetch and capped listings (#344, #345, #347).
- One instance per `DATA_DIR` and encrypted online backups (#334).
- Health, readiness and metrics endpoints with Zabbix templates (#330, #331, #332, #366).
- Audit hardening: where each action came from, more events, a minimum retention and a daily chain check (part of #252, #383).
- Access hardening: customers and folders only within a role's grants, one grant per scope, delegated admins limited to their own authority, and a separate manage-sources capability.
- Sign-in hardening: MFA required for administrators (and optionally for every local account), failed sign-ins limited per username and per address, PKCE and issuer checks for SSO, and no local passwords on SSO accounts.
- Forgotten password, reset by email for local accounts (#244), off by default.
- An access report of who holds what, where, for access reviews (#384), on its own Settings page and as JSON or CSV.
- Secure defaults: the published port on the loopback address, container hardening, a weak credential encryption key refused at startup, `Cache-Control: no-store` on file content, the API docs off by default, a daily backup on new installs, no test code in the image, private scratch and log folders, encrypted webhook URLs, SMB signing and encryption per source, and a warning for WinRM sources without JEA.
- Security documentation: the response times and the support policy in [SECURITY.md](SECURITY.md), the audited events in [docs/audit-events.md](docs/audit-events.md), the security model in [docs/security-model.md](docs/security-model.md), a hardening guide in [docs/hardening.md](docs/hardening.md), and what is kept and what leaves the installation in [docs/data-and-retention.md](docs/data-and-retention.md).
- Property-based API tests and accessibility checks in the end-to-end tests (#376, #377).
- Redaction of secrets, payment card numbers and personal data in viewed and downloaded content (#382): the viewer, Tail, downloads and alerts get redacted content, for every user, redacted on the push agent's host when the agent can, and configured by super admins in Settings → Redaction and each source's editor.

## 1.0.0

Mostly validation and hardening of what 0.7.x shipped. Merged so far:

- PostgreSQL 18 as a database engine (#306), with SQLite still the default: `DATABASE_URL` (or `DATABASE_URL_FILE`) points at an empty database, `python -m app.migrate_database` moves a SQLite installation there with its data, and `docker-compose.postgres.yml` runs PerchTail with a PostgreSQL server of its own ([docs/configuration.md](docs/configuration.md#postgresql), [docs/upgrade.md](docs/upgrade.md#moving-from-sqlite-to-postgresql)).
- A Helm chart for Kubernetes, published with every release to `oci://ghcr.io/quaresma870/charts/perchtail` and as a signed release asset ([docs/kubernetes.md](docs/kubernetes.md)).
- Sign-in and sessions: `__Host-` cookie names, an idle session timeout (60 minutes by default), MFA required of every local account on new installs, common and leaked passwords refused, and admins ending a user's sessions or everyone else's.
- Other security changes: `Cache-Control: no-store` on every API answer, the encryption key readable from a file (`CREDENTIAL_ENCRYPTION_KEY_FILE`), a weak key refused at startup by default, alert webhooks only to `https://` URLs, at most 50,000 entries in a `.zip` and 100 alerts per account, and downloads always sent as `application/octet-stream`.
- Viewer: large files that open at their end, with progress and Cancel when one opens whole; right-click menus on log lines, tabs, files, folders, sources and groups (#287); `.zip` files on SSH, SMB and WinRM sources read in place instead of copied whole; one answer for every path that can't be read; and icons in the Settings sidebar.
- The load-test kit in `loadtest/` and the sizing guide in [docs/sizing.md](docs/sizing.md) (#277), whose figures are still to be measured.
- Security controls in CI and releases: OWASP ZAP, OpenSSF Scorecard, and a signed image with provenance and an SBOM.
- Security documentation: every way to authenticate, input limits and the keys and algorithms in [docs/security-model.md](docs/security-model.md), and TLS, rate limiting and logs in [docs/hardening.md](docs/hardening.md).

## After 1.0

For 1.1 and later releases:

- One-click server upgrade for Docker installs, opt-in (#320).
- High availability on PostgreSQL (#336).
- Notifications by email, Slack and Microsoft Teams (#251).
- User groups with their own grants, and grants directly on a user (#246).
- Temporary access to a customer, folder or source, with a reason and an expiry (#247).
- Audit log export and forwarding (#252), and logs in Elastic Common Schema fields (#383).
- Session policies for admins: session lifetime, sessions per user and a sign-in banner (#349).
- Personal API tokens (#253).
- Masking that depends on the user's role, on top of the redaction every user gets (#249).
- Viewer: split view with synchronized scrolling (#288), bookmark and tab shortcuts (#289), and several files merged into one timeline (#250).
- An external penetration test (#124).
