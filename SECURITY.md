# Security policy

PerchTail is designed to hold credentials for production systems (SSH keys, SMB and WinRM passwords) and to gate access to potentially sensitive log content via RBAC. Security issues here have real consequences — please report them responsibly rather than as a public issue or PR.

## Support and end of life

A release line is in one of three states:

- **Active support:** it gets bug fixes and security fixes.
- **End of support:** it gets security fixes only.
- **End of life:** it gets no fixes. Its release, container image, signatures and SBOM stay available, so it can still be installed, rolled back to and verified.

No release is ever deleted. Fixes ship as a new patch release of a supported line, within the times in [Response times](#response-times), so an install stays supported by running the latest patch release of its line.

**Until 1.0**, only the latest release is supported. Each earlier release reaches end of life when the next one is published.

**From 1.0 on**, the latest minor release is always in active support. When the next minor is published, the previous one keeps active support for 6 more months, gets security fixes only until 12 months, and then reaches end of life. Both periods count from the release of the minor that replaced it. For example, if 1.1 is released on day D, 1.0 is in active support until D + 6 months, gets security fixes only until D + 12 months, and reaches end of life after that.

| Release line | State |
|---|---|
| 0.7 | The next release, not published yet. Once released, it is the only supported line until the release after it (0.8.0 or 1.0.0) is published. |
| 0.6 | Active support, as the latest release. It reaches end of life when 0.7.0 is released. |
| 0.5 and earlier | End of life since 0.6.0 was released on 2026-11-07. |

Releases up to 0.6.4 are under the MIT license; later releases are under the AGPL-3.0-only.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports. Instead:

1. Use [GitHub Security Advisories](../../security/advisories/new) on this repo (private by default), or
2. Email [security@perchtail.com](mailto:security@perchtail.com).

Please include what you found, how to reproduce it, and its potential impact. Reports are acknowledged as soon as possible.

## Response times

A confirmed vulnerability in anything PerchTail ships is fixed in a published release within **3 days**, whatever its severity. Severity decides how urgently it's handled within those days, not a longer deadline.

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
- Auth provider implementations (local and OIDC) — session handling, token validation, SSO callback handling
- Redaction: anything that lets content a redaction rule should replace reach a browser, a download or a webhook in clear
- Path traversal or injection via rule patterns, source configuration, or the archive browse/download endpoints, including a symlink or junction that reaches a file the rules hide
- Anything that could turn a read-only viewing feature into a write path back to a source (this project's core invariant is that it never writes to sources)

## Disclosure

Coordinated disclosure is appreciated — please allow time for a fix before any public write-up. Credit is happily given in the release notes unless you'd prefer to stay anonymous.
