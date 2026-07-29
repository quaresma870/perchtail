# Security policy

PerchTail is designed to hold credentials for production systems (SSH keys, SMB
and WinRM passwords) and to gate access to potentially sensitive log content via
RBAC. Security issues here have real consequences — please report them
responsibly rather than as a public issue or PR.

## Supported versions

| Version | Supported |
|---|---|
| 0.x (pre-release) | ✅ during active development |

This table will be updated once a 1.0 is tagged and a support window is defined.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports. Instead:

1. Use [GitHub Security Advisories](../../security/advisories/new) on this repo
   (private by default), or
2. Email the maintainer directly (see the repo's profile for current contact
   details).

Please include what you found, how to reproduce it, and its potential impact.
This is currently a small, part-time-maintained project, so response times are
best-effort rather than a guaranteed SLA — but reports are taken seriously and
acknowledged as soon as possible.

## Scope

Particularly interested in reports involving:

- Credential storage or the encryption-at-rest mechanism for source credentials
- RBAC/grant-resolution logic — anything that lets a role reach a customer or
  source it wasn't granted
- Auth provider implementations (local, OIDC, SAML) — session handling, token
  validation, SSO callback handling
- Path traversal or injection via rule patterns, source configuration, or the
  archive browse/download endpoints
- Anything that could turn a read-only viewing feature into a write path back to
  a source (this project's core invariant is that it never writes to sources)

## Disclosure

Coordinated disclosure is appreciated — please allow time for a fix before any
public write-up. Credit is happily given in the release notes unless you'd
prefer to stay anonymous.
