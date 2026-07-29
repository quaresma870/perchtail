# PerchTail

> Live, rule-scoped log browsing across Linux and Windows servers — no agents,
> nothing mirrored, nothing left behind.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-early%20development-orange.svg)](#status)
[![CI](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml/badge.svg)](https://github.com/quaresma870/perchtail/actions/workflows/ci.yml)

## Status

🚧 **Early development.** PerchTail isn't released yet — this repo currently holds
the design and a working plan. Watch/star to follow progress; see
[CHANGELOG.md](CHANGELOG.md) for what's actually shipped versus planned.

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
| SSO (OIDC/SAML) | 🚧 roadmap | enterprise tier | ✅ | ❌ | via Grafana |
| Code-editor-style viewer | ✅ CodeMirror | search UI, not a file viewer | search UI | depends which browser | Grafana Explore |
| Setup | single docker-compose | multi-service | multi-service | multiple tools glued together | multi-service |

This table reflects the design as of this writing and each project evolves —
verify anything that matters to your decision against current docs.

## Quick start

```bash
git clone https://github.com/<your-org>/perchtail.git
cd perchtail
cp .env.example .env   # set LOG_RETENTION_DAYS, credential encryption key, etc.
docker compose up -d
```

Then open `http://localhost:8080`, sign in with the seeded local super-admin
account, and add your first source.

*(Quick start will be filled in with real steps once phase 1 ships — see
[CLAUDE.md](CLAUDE.md) for the build plan.)*

## Documentation

- [CLAUDE.md](CLAUDE.md) — full design/architecture reference and build plan
- [ROADMAP.md](ROADMAP.md) — phased milestones and what's next
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to get a dev environment running and
  submit changes
- [SECURITY.md](SECURITY.md) — how to report a vulnerability
- [CHANGELOG.md](CHANGELOG.md) — what's shipped, release by release

## License

MIT — see [LICENSE](LICENSE).
