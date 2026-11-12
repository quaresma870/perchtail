# Security policy

PerchTail is designed to hold credentials for production systems (SSH keys, SMB and WinRM passwords) and to gate access to potentially sensitive log content via RBAC. Security issues here have real consequences — please report them responsibly rather than as a public issue or PR.

## Supported versions

| Version | Supported |
|---|---|
| 0.x (pre-release) | ✅ during active development |

This table will be updated once a 1.0 is tagged and a support window is defined.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports. Instead:

1. Use [GitHub Security Advisories](../../security/advisories/new) on this repo (private by default), or
2. Email the maintainer directly (see the repo's profile for current contact details).

Please include what you found, how to reproduce it, and its potential impact. Reports are acknowledged as soon as possible.

## Response times

A confirmed vulnerability in anything PerchTail ships is fixed in a published release within:

| Severity | Release with the fix |
|---|---|
| Critical, High | 3 days |
| Medium, Low | 7 days |

The clock starts when the report is confirmed or the advisory for a dependency is published. A critical issue with a workaround gets an advisory describing it as soon as it's confirmed, ahead of the release. A vulnerability that only affects development tooling (tests, linters, the build) is fixed on `dev` within the same times, without a release of its own.

"Anything PerchTail ships" is the same whichever way it's deployed (Docker Compose, k3s or any other Kubernetes, or from source):

- the server's Python packages;
- the frontend's npm packages, built into the bundle the server serves;
- the push agent: its Go modules and the Go standard library it's built with;
- the container image: its base image and OS packages;
- the GitHub Actions that build and sign releases.

These are checked on every push and pull request, and again every day against `main`, `dev`, the latest release's agent binaries and the published image, so a new advisory is caught even when nothing in PerchTail changes. Each release also carries an SBOM, so you can match its components against your own scanner's findings.

## Scope

Particularly interested in reports involving:

- Credential storage or the encryption-at-rest mechanism for source credentials
- RBAC/grant-resolution logic — anything that lets a role reach a customer or source it wasn't granted
- Auth provider implementations (local, OIDC, SAML) — session handling, token validation, SSO callback handling
- Path traversal or injection via rule patterns, source configuration, or the archive browse/download endpoints
- Anything that could turn a read-only viewing feature into a write path back to a source (this project's core invariant is that it never writes to sources)

## Disclosure

Coordinated disclosure is appreciated — please allow time for a fix before any public write-up. Credit is happily given in the release notes unless you'd prefer to stay anonymous.
