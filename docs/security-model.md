# Security model

This page describes how PerchTail protects the logs it gives access to: what it trusts, what it stores and how, who can do what, and what it expects from the environment it runs in. It is written for whoever installs PerchTail, approves it or answers questions about it. How to set it up securely is in [hardening.md](hardening.md); what it keeps and for how long is in [data-and-retention.md](data-and-retention.md); every event it records is in [audit-events.md](audit-events.md).

## What PerchTail does and doesn't do

- **It only reads from sources.** No connector writes, deletes, renames or changes anything on a source. The service accounts it uses on the sources only need read access ([source-setup.md](source-setup.md)).
- **It keeps nothing of the logs.** Opening, following or downloading a file always fetches it fresh into a private scratch folder, which is emptied when the file is closed, after 5 minutes without use and at every start. The database never holds file content: only the names, sizes and modification times of the files the rules make visible (the file-name index), and how far each alert has read each file.
- **Rules decide what exists.** A path no rule makes visible can't be listed, opened, downloaded, searched or watched by an alert, by anyone, super admins included. A source with no rules shows nothing.
- **Access is denied unless a grant allows it.** Every request is checked against the user's role on the server; the web interface only hides what the server would refuse anyway.
- **Redaction**, when a super admin turns it on, replaces secrets, payment card numbers and personal data in what the viewer, Tail and downloads send, for every user, super admins included; for a push-agent source whose agent is from 0.7.0 on, the agent redacts on its own host, so the original never reaches PerchTail. Alerts run their query on the redacted lines and send only redacted lines to their webhook. The settings are in [configuration.md](configuration.md#redaction).

## Trust boundaries

| Boundary | What crosses it | What protects it |
|---|---|---|
| Browser → reverse proxy | Users' requests and the log content sent back | TLS at the reverse proxy, HSTS, the session cookie's `Secure` and `SameSite=Strict` flags ([hardening.md](hardening.md)) |
| Reverse proxy → PerchTail | The same, in plain HTTP on the host or a private network | PerchTail listening on loopback or a private network only; `X-Forwarded-For` believed only from `TRUSTED_PROXIES` |
| PerchTail → sources | Credentials and read commands out, file listings and content in | Each protocol's own authentication and encryption (see [Connections to sources](#connections-to-sources)); credentials encrypted at rest |
| Push agent → PerchTail | An outbound WebSocket from the agent's host; PerchTail never connects to the agent | `wss://` with the server's certificate checked, an enrollment token per agent, the agent confined to its base path |
| PerchTail → outside services | Only what an administrator configures, plus the daily release check ([data-and-retention.md](data-and-retention.md#what-leaves-the-installation)) | TLS; webhook targets limited to public addresses; email only over TLS |
| PerchTail host and `/data` | The database, the salt, pinned host keys, backups, logs and scratch | Folders readable by PerchTail's user only; secrets encrypted with a key kept outside `/data` |

Anyone who controls the PerchTail host, or holds both the database and `CREDENTIAL_ENCRYPTION_KEY`, can read every stored credential and re-sign the audit log. Protect the host and the key accordingly.

## Who can do what

### Roles and grants

Each user has exactly one role. A role carries **global capabilities**, which apply everywhere, and **grants**, which give **scoped capabilities** on one customer, folder or source and everything below it, including sources added later.

Global capabilities:

| Capability | What it allows |
|---|---|
| `manage_users` | Create, edit and deactivate users and reset local passwords, only for roles within the admin's own authority. |
| `manage_roles` | Create and edit roles and their grants, within the admin's own authority, never the role the admin holds. |
| `manage_sso` | Configure the SSO provider and the group-to-role mappings. |
| `create_source` | Together with `manage_source` where it goes, add sources and folders. On its own it reaches nothing. |
| `manage_system_settings` | Change the settings under Settings → System. Some only a super admin may change: turning folder-listing audit off, shortening the audit retention, requiring MFA for every local account, password recovery and redaction. |
| `view_audit_log` | Read the audit log. Implied by nothing else. |
| `manage_alerts` | Create and edit alerts, on sources the user can view. An alert sends matching lines to a webhook, so viewing a source doesn't imply it. |
| `view_access_report` | Generate the access report. Read-only and implied by nothing else; short of a super admin, limited to the sources where the reviewer holds a capability. |

Scoped capabilities, given by a grant:

| Capability | What it allows on the grant's scope |
|---|---|
| `view` | Browse, open and follow files. |
| `download` | Download files and folders. |
| `manage_rules` | Change the visibility rules, nothing else. |
| `run_now` | Start a file-name index pass. |
| `manage_source` | Edit, move and delete sources; rename, move and delete folders; rename and delete a customer (with a grant on the customer itself); agent tokens, saved credentials and agent upgrades. With `create_source`, add sources and folders. |

A **super admin** role bypasses grants and is the only one that reaches the built-in system log source, creates customers, and configures redaction, email and the import and export of configuration.

### How access is decided

- One function decides every capability check, and the access report uses the same one: the most specific grant wins (the source, then the nearest folder up the tree, then the customer). With no grant, access is denied.
- A role holds at most one grant per scope, so there is never a choice between two grants on the same scope.
- Customers and folders are only listed for a role whose grants reach them. Asking for one outside its grants answers 404, as if it didn't exist.
- Every request a signed-in user makes without the permission it needs gives them a neutral message and is recorded in the audit log as `access.denied` with what was attempted and why. Failed sign-ins and lockouts are recorded too ([audit-events.md](audit-events.md)). These refusals are not recorded: a request without a valid session (401), a state-changing request blocked by the cross-site check (403), and a request from an account that must first change its password or set up two-factor authentication (403).

### Access is never widened by whoever administers it

A delegated administrator (a role with `manage_users`, `manage_roles` or `manage_sso`, short of a super admin) can only give what they hold themselves:

- They can't assign, edit or act on a role more powerful than their own: a super-admin role, one with a global capability they don't hold, or one with a grant reaching further than their own grant at that scope.
- They can't change the role they hold or move themselves to another role. Only a super admin can.
- A role's grants are checked as a whole after each change, so removing one grant can't make another reach further than the administrator's authority.
- The last active super admin can't be deactivated or demoted, and the built-in roles ("Super Admin" and "No Access") can't be changed.
- An SSO user whose groups match no mapping gets the "No Access" role at sign-in, also when they had another role before; the last active super admin keeps theirs. A delegated administrator only creates, changes or deletes a group mapping whose role is within their authority, placed before every mapping whose role isn't (the last matching mapping wins), and only a super admin deletes or turns off the SSO provider.

### Signing in

- **Local accounts** sign in with a username and password. Passwords are hashed with argon2id and follow the password policy under Settings → System (12 to 128 characters by default, simple and personal passwords rejected, optional history, character classes and maximum age). Accounts an administrator creates must change their password at first sign-in.
- **SSO** uses OpenID Connect with one provider at a time: the authorization code flow with PKCE, an `https://` issuer that must match its discovery document, the identity provider reached only at public addresses, and the ID token's signature (only asymmetric algorithms the provider announces), issuer, audience (with `azp` when there are several), expiry and nonce checked. SSO accounts can't sign in with a password, and an administrator can't give them one.
- **Two-factor authentication** (TOTP, with 10 single-use backup codes) is required of super admins and of every role with a global capability, and of every local account when **Require MFA for local accounts** is on. Until a covered account sets it up, it can do nothing else. SSO accounts rely on the identity provider's MFA.
- **Failed sign-ins** lock the username for 5 minutes after 5 failures, and lock a client address that fails as often, whatever the usernames. An active local super admin with MFA is locked only from the address that failed, so an attacker can't lock the emergency account out from elsewhere.
- **Password recovery by email** is off by default. When on, it never serves super admins, roles with a global capability, SSO accounts or accounts without an email.

### The emergency account

The first start creates a local super admin (`admin` by default) with a random password, written to `DATA_DIR/initial-admin-password`, readable by PerchTail's user only and deleted when the password is changed. Local super admins keep working while SSO is on or the identity provider is down. Keep at least two local super admins with MFA, with their passwords and backup codes stored where your organisation keeps emergency credentials, and check the audit log for their sign-ins (`user.login`). An administrator can't turn off another user's MFA: an account whose authenticator and backup codes are both lost can't sign in again, so an administrator deactivates it and creates a new one.

### Sessions and the browser

- A session is a random 256-bit token in the `perchtail_session` cookie (`HttpOnly`, `SameSite=Strict`, `Secure` unless `SESSION_COOKIE_SECURE=false`). Only its SHA-256 is stored. It lasts `SESSION_TTL_HOURS` (12) and is checked on the server at every request.
- Signing out deletes the session. Changing one's password ends the user's other sessions; an administrator's password reset, a password reset by email and deactivating the account end all of them. Users see and end their own sessions in My profile.
- **CSRF**: the `SameSite=Strict` cookie is never sent with a cross-site request; on top of it, a state-changing request whose `Origin` (or `Referer`) names another host than the request's is refused.
- **Response headers**: a Content-Security-Policy that allows only PerchTail's own scripts (`script-src 'self'`, no inline script; inline styles are allowed because the viewer's editor injects them), `frame-ancestors 'none'` and `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a Permissions-Policy turning off camera, microphone, location and other unused features, and `Cross-Origin-Opener-Policy: same-origin`. HSTS is set by the reverse proxy that terminates TLS.
- File content and the password-recovery pages are sent with `Cache-Control: no-store`, so neither the browser nor a proxy keeps a copy.
- The web interface loads nothing from other sites: no CDN, web font, analytics or telemetry.
- The interactive API documentation (`/docs`, `/redoc`, `/openapi.json`) answers 404 unless `API_DOCS=true`.

## Keys and encryption

| Secret | Protects | Where it lives |
|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` with the per-install salt | Source credentials, the SSO provider's configuration, users' TOTP secrets, the SMTP password and alert webhook URLs (Fernet: AES-128-CBC with HMAC-SHA256, key derived with PBKDF2-HMAC-SHA256, 600,000 iterations); and, through a second key derived separately, the audit log's hash chain (HMAC-SHA256) | The key in the environment (`.env`), never in the database; the salt in `DATA_DIR/credential_salt` |
| `BACKUP_ENCRYPTION_KEY` (optional) | Backups (AES-256-GCM in authenticated blocks, key derived with HKDF-SHA256). Without it, backups use `CREDENTIAL_ENCRYPTION_KEY`. | The environment |
| Push-agent release key | Agent binaries: an agent installs an upgrade only if `SHA256SUMS` carries a valid Ed25519 signature from the key built into it | The public key in the agent; the private key with the release process |

What is never stored in a form that can be read back: local passwords, previous passwords and MFA backup codes (argon2id hashes), and session tokens, password-reset tokens, agent enrollment tokens and the monitoring token (SHA-256 hashes).

- PerchTail refuses to start with the shipped `changeme` key, warns about a weak key (shorter than 32 bytes once decoded, or less than 128 bits of estimated entropy) and, with `CREDENTIAL_KEY_REFUSE_WEAK=true`, refuses to start with one.
- It refuses to generate a new salt while the database holds data encrypted under the old one, so a lost salt is noticed rather than silently making every secret unreadable.
- `python -m app.rotate_credential_key` re-encrypts every secret under a new key and re-signs the audit chain in one transaction ([credential-key-rotation.md](credential-key-rotation.md)).
- The key and the salt are useless without each other. Keep the key outside the host, where you can reach it if the host is lost ([backup-and-restore.md](backup-and-restore.md)).

## The audit log

Every administrative change, every sign-in and sign-out, every file opened, followed or downloaded, every search and every request refused for lack of permission is recorded with who, when, from which address, with which session, and on what. Entries are chained with HMAC-SHA256 under a key that can't be computed from the database alone, so changing, deleting or reordering entries, or moving the chain's anchor, is detected by the daily integrity check. Entries are kept 365 days by default (at least 90, or forever), and only a super admin can shorten that. The fields, every event, the retention and how integrity is checked are in [audit-events.md](audit-events.md).

## Connections to sources

| Protocol | Encryption and identity | The safer setting |
|---|---|---|
| SSH/SFTP | SSH; the server's host key is pinned in `DATA_DIR/ssh_known_hosts`, and a changed key is refused | `SSH_HOST_KEY_POLICY=strict` refuses a host whose key wasn't added by hand; the default `tofu` pins it on first connection |
| SMB | NTLM authentication; signed messages required by default (**Require signing**, per source) | **Encrypt** asks for SMB 3 encryption; a source with signing off shows a warning |
| WinRM | HTTPS (port 5986) with the server's certificate checked, optionally against the source's own CA; HTTP only when chosen explicitly | A JEA endpoint with the read-only role in [examples/jea](examples/jea/README.md), so the account needs no remote shell or administrator rights; a WinRM source without JEA shows a warning |
| Push agent | The agent dials out over `wss://` and refuses `ws://` unless `PERCHTAIL_ALLOW_INSECURE=true`; it authenticates with its enrollment token | Keep the agent's base path to the log folders; set `PERCHTAIL_REMOTE_UPGRADE=false` on hosts that must never be upgraded from the portal |
| Local | Files on the PerchTail host, only under `LOCAL_SOURCE_ROOTS` (empty by default, which disables them) | Mount host logs read-only |

On every protocol, paths are checked to stay inside the source's folders, and reads are bounded in size, time and concurrency (`MAX_FETCH_MB`, `MAX_DECOMPRESSED_MB`, `REMOTE_READ_TIMEOUT_SECONDS` and the limits under Settings → System). Regular expressions from users (rules, alerts, redaction patterns) run in linear time (RE2).

## What PerchTail expects from its environment

- A reverse proxy in front of it that terminates TLS and sets HSTS. PerchTail itself speaks plain HTTP and must not be reachable from users' networks except through the proxy.
- A host whose clock is kept right by NTP: sessions, lockouts, password-reset links, TOTP codes and audit timestamps depend on it.
- `CREDENTIAL_ENCRYPTION_KEY` (and `BACKUP_ENCRYPTION_KEY`) generated randomly and kept outside the host.
- `DATA_DIR` on a local disk, readable by PerchTail's user only, and backups copied off the host.

[hardening.md](hardening.md) goes through each of these.

## Releases and vulnerabilities

How to report a vulnerability, how fast fixes ship and which releases get them is in [SECURITY.md](../SECURITY.md). How to check that a release's image, agent binaries and SBOMs are the ones the project published is in [verifying-releases.md](verifying-releases.md).
