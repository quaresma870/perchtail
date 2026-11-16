# Roadmap

What comes next for PerchTail, without dates: progress is measured by what is merged. What has shipped is in [CHANGELOG.md](CHANGELOG.md); why things are built the way they are, including decisions that were later replaced, is in [docs/design-decisions.md](docs/design-decisions.md). Each item links its GitHub issue where it has one; the plan is also tracked as the milestones [0.7](https://github.com/quaresma870/perchtail/milestone/1) and [1.0](https://github.com/quaresma870/perchtail/milestone/2).

Which releases get fixes, and for how long, is in [SECURITY.md](SECURITY.md#support-and-end-of-life).

## Principles that limit scope

- PerchTail only reads from sources. It never writes, deletes or changes anything on them.
- Nothing from the logs is kept. Content is fetched fresh into scratch and deleted; only file names, sizes and modification times are indexed.
- Access is denied unless a grant allows it, and every grant is scoped to a customer, a folder or a source.
- One process per `DATA_DIR`, on SQLite. SQLite stays the default database even once PostgreSQL is supported.
- 0.7 is the last feature release before 1.0. 1.0 adds no features: it validates and hardens what is there.
- Every release goes through a release candidate retested in a lab with Linux, Windows and push-agent sources first.
- The HTTP API is internal to the web UI and may change before 1.0.
- Bulk import is additive: it never deletes anything and never exports credentials.
- Work without an issue (SAML, maintained Kubernetes manifests, a further high-availability tier) waits for a real need; see the open items in [docs/design-decisions.md](docs/design-decisions.md).

## 0.7.0

The last feature release before 1.0. Done means merged on `dev` and listed under `[Unreleased]` in the CHANGELOG.

Done:

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
- Secure defaults: the published port on the loopback address, container hardening, a warning for a weak credential encryption key, `Cache-Control: no-store` on file content, the API docs off by default, a daily backup on new installs, no test code in the image, private scratch and log folders, encrypted webhook URLs, SMB signing and encryption per source, and a warning for WinRM sources without JEA.
- Security documentation: the response times and the support policy in [SECURITY.md](SECURITY.md), and the audited events in [docs/audit-events.md](docs/audit-events.md).
- Property-based API tests and accessibility checks in the end-to-end tests (#376, #377).

Started:

- Redaction of secrets, payment card numbers and personal data in viewed and downloaded content (#382), and masking by role (#249): the viewer, Tail and downloads get redacted content, and the push agent can redact on its host; alerts, asking agents to redact and a settings screen are still to do.
- An access report of who holds what, where, for access reviews (#384): the report, as JSON or CSV, and its capability exist; a page to view it is still to do.

## 1.0.0

Validation only, no new features:

- Load tests in a lab and a sizing guide, on SQLite (#277). The load-test kit is in `loadtest/`.
- Security controls in CI and releases: OWASP ZAP, OpenSSF Scorecard, and a signed image with provenance and an SBOM. These are in place.
- An internal penetration test.
- Fixes for what those find, and documentation.

## After 1.0

For 1.1 and later releases:

- One-click server upgrade for Docker installs, opt-in (#320).
- PostgreSQL support (#306).
- High availability on PostgreSQL (#336).
- Notifications by email, Slack and Microsoft Teams (#251).
- User groups with their own grants, and grants directly on a user (#246).
- Temporary access to a customer, folder or source, with a reason and an expiry (#247).
- Audit log export and forwarding (#252), and logs in Elastic Common Schema fields (#383).
- Session policies for admins: idle timeout, lifetime, sessions per user and required MFA (#349).
- Personal API tokens (#253).
- Viewer: context menus (#287), split view with synchronized scrolling (#288), bookmark and tab shortcuts (#289), and several files merged into one timeline (#250).
- An external penetration test (#124).
