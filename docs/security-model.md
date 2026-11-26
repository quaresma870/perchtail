# Security model

This page describes how PerchTail protects the logs it gives access to: what it trusts, what it stores and how, who can do what, and what it expects from the environment it runs in. It is written for whoever installs PerchTail, approves it or answers questions about it. How to set it up securely is in [hardening.md](hardening.md); what it keeps and for how long is in [data-and-retention.md](data-and-retention.md); every event it records is in [audit-events.md](audit-events.md).

## What PerchTail does and doesn't do

- **It only reads from sources.** No connector writes, deletes, renames or changes anything on a source. The service accounts it uses on the sources only need read access ([source-setup.md](source-setup.md)).
- **It keeps nothing of the logs.** Opening or downloading a file always fetches it fresh from the source into a private scratch folder (a plain local file is read in place), which is emptied when the file is closed, after 5 minutes without use and at every start; following a file reads only what was appended. The database never holds file content: only the names, sizes and modification times of the files the rules make visible (the file-name index), and how far each alert has read each file. The one exception is a webhook call waiting to be sent or retried, which holds its matching lines for at most an hour ([data-and-retention.md](data-and-retention.md#the-database)).
- **Rules decide what exists.** A path no rule makes visible can't be listed, opened, downloaded, searched or watched by an alert, by anyone, super admins included. A link is judged by where it leads: on local, SSH and push-agent sources, a symlink (or a junction on a Windows agent) to a file or folder the rules hide, or out of the source's folders, is neither listed nor read, even when its own name is visible; SMB and WinRM sources follow no link at all. A source with no rules shows nothing.
- **Access is denied unless a grant allows it.** Every request is checked against the user's role on the server; the web interface only hides what the server would refuse anyway.
- **Redaction**, when a super admin turns it on, replaces secrets, payment card numbers and personal data in what the viewer and Tail send and, while **Redact downloads too** is on (the default), in downloads, for every user, super admins included; for a push-agent source whose agent is from 0.7.0 on, the agent redacts on its own host, so the original never reaches PerchTail. Alerts run their query on the redacted lines and send only redacted lines to their webhook. Redaction is best effort ([redaction.md](redaction.md)); the settings are in [configuration.md](configuration.md#redaction).

## Trust boundaries

| Boundary | What crosses it | What protects it |
|---|---|---|
| Browser → reverse proxy | Users' requests and the log content sent back | TLS at the reverse proxy, HSTS, the session cookie's `Secure` and `SameSite=Strict` flags ([hardening.md](hardening.md)) |
| Reverse proxy → PerchTail | The same, in plain HTTP on the host or a private network | PerchTail listening on loopback or a private network only; `X-Forwarded-For` believed only from `TRUSTED_PROXIES` |
| PerchTail → sources | Credentials and read commands out, file listings and content in | Each protocol's own authentication and encryption (see [Connections to sources](#connections-to-sources)); credentials encrypted at rest |
| Push agent → PerchTail | An outbound WebSocket from the agent's host; PerchTail never connects to the agent | `wss://` with the server's certificate checked, an enrollment token per agent (a second connection with the same token replaces the first and is audited), the agent confined to its base path |
| PerchTail → outside services | Only what an administrator configures, plus the daily release check ([data-and-retention.md](data-and-retention.md#what-leaves-the-installation)) | `https://` for the release check and the identity provider; webhook targets and the identity provider limited to public addresses, connected to at the address checked, without following redirects; email only over TLS. A new or changed webhook URL must be `https://`; an alert saved with `http://` before keeps sending unencrypted, flagged in the alerts list and logged as `alert.webhook_insecure` at each call |
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
| `create_source` | Needed, together with `manage_source` on the scope, to add, edit, move and delete sources and folders, rename and delete customers, and manage agent tokens, saved credentials and agent upgrades. On its own it reaches nothing. |
| `manage_system_settings` | Change the system settings: Settings → Source defaults, Sign-in security, Severity indicators, Explorer and limits, Alerts, Backups and updates, Monitoring, and the settings on the Audit log page. Some only a super admin may change: turning folder-listing audit off, shortening the audit retention, requiring MFA for every local account, password recovery and redaction. |
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
| `manage_source` | Together with the global `create_source`: edit, move and delete sources; rename, move and delete folders; rename and delete a customer (with a grant on the customer itself); agent tokens, saved credentials and agent upgrades; and add sources and folders. On its own it only allows checking a source's connection (**Test connection** and its status). |

A **super admin** role bypasses grants and is the only one that reaches the built-in system log source, creates customers, and configures redaction, email and the import and export of configuration.

### How access is decided

- One function decides every capability check, and the access report uses the same one: the most specific grant wins (the source, then the nearest folder up the tree, then the customer). With no grant, access is denied.
- A role holds at most one grant per scope, so there is never a choice between two grants on the same scope.
- Customers and folders are only listed for a role whose grants reach them. Asking for one outside its grants answers 404, as if it didn't exist. So does a source the user holds no capability on, and, for a delegated administrator, reading a role beyond their authority; a source they reach without the capability needed, or an attempt to edit or assign a role beyond their authority, answers 403.
- Every request a signed-in user makes without the permission it needs gives them a neutral message and is recorded in the audit log as `access.denied` with what was attempted and why. Failed sign-ins and lockouts are recorded too ([audit-events.md](audit-events.md)). These refusals are not recorded: a request without a valid session (401), a state-changing request blocked by the cross-site check (403), and a request from an account that must first change its password or set up two-factor authentication (403).

### Access is never widened by whoever administers it

A delegated administrator (a role with `manage_users`, `manage_roles` or `manage_sso`, short of a super admin) can only give what they hold themselves:

- They can't assign, edit or act on a role more powerful than their own: a super-admin role, one with a global capability they don't hold, or one with a grant reaching further than their own grant at that scope.
- They can't change the role they hold or move themselves to another role. Only a super admin can.
- They can't reset the password of, deactivate or change the role of an account whose role has `manage_users` or `manage_roles`, even one within their authority, so one delegated administrator can't take over another. Only a super admin can.
- A role's grants are checked as a whole after each change, so removing one grant can't make another reach further than the administrator's authority.
- The last active super admin can't be deactivated or demoted, and the built-in roles ("Super Admin" and "No Access") can't be changed.
- An SSO user whose groups match no mapping gets the "No Access" role at sign-in, also when they had another role before; the last active super admin keeps theirs. A delegated administrator only creates, changes or deletes a group mapping whose role is within their authority, placed before every mapping whose role isn't (the last matching mapping wins), and the mappings to a role beyond their authority don't show them its name; only a super admin deletes or turns off the SSO provider.
- Customer names are unique across the installation, so renaming a customer to a name already in use is refused (409) even when that other customer is outside the admin's reach: the refusal tells a delegated admin that the name exists, and nothing else about it.

### Rules on individual fields

Some fields follow rules of their own, whoever may otherwise read or change the object they belong to:

- **Secrets are written, never read back.** A source's saved credential, the SSO provider's client secret, the SMTP password and an alert's webhook URL are stored encrypted, and no answer of the API ever carries them: it says only whether one is saved (and, for a webhook, its scheme, host and port). Password hashes, TOTP secrets and token hashes are never returned either.
- **An SSO account's name and email come from the identity provider.** They are set at every sign-in, shown read-only in My profile, and an administrator can't change them (only a local account's). Its role is set by the group mappings at every sign-in, so a role given in Settings → Users lasts until its next sign-in.
- **A username never changes** once the account exists, so audit entries keep naming the same account.
- **A user changes only their own profile fields**: display name, email (with the current password, since password-reset links go there), password and MFA. Their role, and whether the account is active, are an administrator's to change.
- **Roles and grants stay within the administrator's authority**, as described above: a role, a grant or a group mapping beyond it can't be assigned, created or edited by a delegated administrator.
- **A few settings are a super admin's only**, even for someone with `manage_system_settings`: those listed under [Roles and grants](#roles-and-grants), and lengthening or turning off **End unused sessions after**.

### Signing in

- **Local accounts** sign in with a username and password. Passwords are hashed with argon2id and follow the password policy under Settings → Sign-in security (12 to 128 characters by default, simple, common or leaked and personal passwords rejected, optional history, character classes and maximum age). A password never contains "perchtail" or any word of the **Words passwords can't contain** list. Accounts an administrator creates must change their password at first sign-in.
- **SSO** uses OpenID Connect with one provider at a time: the authorization code flow with PKCE (S256), an `https://` issuer (and `https://` endpoints in its discovery document) that must match its discovery document's `issuer`, the identity provider reached only at public addresses, at the address checked and without following redirects (`SSO_ALLOW_HTTP_LOCALHOST=true` lets through `http://` on a loopback address, for local development only), and the ID token's signature (only asymmetric algorithms the provider announces, or RS256 when it announces none), issuer, audience (with `azp` when there are several), expiry, issue time and nonce checked. Starting an SSO sign-in is limited to 20 a minute per client address. A new SSO account takes its username from `email` only when the ID token says `email_verified: true`. SSO accounts can't sign in with a password, and an administrator can't give them one.
- **Two-factor authentication** (TOTP, with 10 single-use backup codes) is required of super admins and of every role with a global capability, and of every local account when **Require MFA for local accounts** is on (the default on a new install). Until a covered account sets it up, it can do nothing else. Turning one's own MFA off needs the current password and a code; confirming MFA only completes an enrollment in progress. A TOTP code is accepted in its own 30-second step and in the step before or after it, so a phone or host clock off by up to about 30 seconds still works; each code is accepted once, and never after a later code has been used. SSO accounts rely on the identity provider's MFA.
- **Failed sign-ins** lock the username for `LOGIN_LOCKOUT_SECONDS` (5 minutes) after `LOGIN_MAX_ATTEMPTS` (5) failures, and lock a client address that fails as often, whatever the usernames (an IPv6 address counts with its /64). A wrong current password or code when changing one's password, email or MFA counts the same way. An active local super admin with MFA is locked only from the address that failed, so an attacker can't lock the emergency account out from elsewhere.
- **Password recovery by email** is off by default. When on, it never serves super admins, roles with a global capability, SSO accounts or accounts without an email.

### Ways to authenticate

Every way into PerchTail, by people and by machines:

| Way | Who uses it | Factors | Limits | Where it is recorded |
|---|---|---|---|---|
| Local username and password, with TOTP | Local accounts, including the emergency super admins | Password, plus a TOTP or backup code when the account has MFA (required of administrators, and of every local account when **Require MFA for local accounts** is on) | 5 failures lock the username, and as many from one address lock the address, for 5 minutes (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS`); the password policy | Audit log: `user.login`, `user.login_failed`, `user.login_throttled`, `user.mfa_required` |
| SSO (OpenID Connect) | SSO accounts | Whatever the identity provider asks for; PerchTail checks the ID token, not how the user proved who they are | 20 sign-in starts a minute per client address; the sign-in must finish within 10 minutes | Audit log: `user.login`, `user.sso_login_failed`, `user.sso_role_synced`; application log: `sso.callback.*`, `sso.login.*` |
| Password recovery by email | Local accounts without a global capability, with an email, while a super admin has turned it on | Access to the account's mailbox, through a single-use link; it sets a new password and signs nobody in, so the next sign-in still asks for the password and, when the account has MFA, a code | 10 requests per client address in 15 minutes, 3 emails per account an hour, 20 link checks per address in 15 minutes; the link lasts 5 to 60 minutes (30 by default) | Audit log: `user.password_reset_requested`, `user.password_reset_completed`, `user.password_reset_failed` |
| Agent enrollment token | Push agents, on their WebSocket connection and when downloading an upgrade | One random 256-bit token per agent source, shown once, stored as a SHA-256 hash | No lockout: a token that random can't be guessed. A wrong token closes the connection without saying why; a second connection with the same token replaces the first | Audit log: `source.agent_token_regenerate`, `agent.connection_replaced`; application log: `agent.connected`, `agent.disconnected` |
| Monitoring token | The monitoring system, on `/monitoring/health` and `/metrics`, and on `/healthz` and `/readyz` for a caller that isn't on loopback or in `PROBE_ALLOWED_NETWORKS` | One random 256-bit token for the installation, stored as a SHA-256 hash | `MONITORING_ALLOWED_NETWORKS` limits the addresses `/monitoring/health` and `/metrics` answer; a wrong token gets a bare 401 | Audit log: `monitoring_token.regenerate` |

There are no API keys or other long-lived user tokens: people use the web interface with a session.

### The emergency account

The first start creates a local super admin (`admin` by default) with a random password, written to `DATA_DIR/initial-admin-password`, readable by PerchTail's user only and deleted when the password is changed. Local super admins keep working while SSO is on or the identity provider is down. Keep at least two local super admins with MFA, with their passwords and backup codes stored where your organisation keeps emergency credentials, and check the audit log for their sign-ins (`user.login`). An administrator can't turn off another user's MFA: an account whose authenticator and backup codes are both lost can't sign in again, so an administrator deactivates it and creates a new one.

### Sessions and the browser

- A session is a random 256-bit token in the `__Host-perchtail_session` cookie (`HttpOnly`, `SameSite=Strict`, `Secure`, `Path=/`, no `Domain`). The `__Host-` prefix makes a browser accept the cookie only from PerchTail's own host over HTTPS, so a site on a sibling subdomain or a page over plain HTTP can't plant or replace it. With `SESSION_COOKIE_SECURE=false` (local tests over HTTP only) the cookie is `perchtail_session`, without `Secure`. Only its SHA-256 is stored. It lasts `SESSION_TTL_HOURS` (12), ends sooner when unused for **End unused sessions after** (60 minutes by default, recorded to the minute; what the web interface asks on its own doesn't count as use), and is checked on the server at every request. Signing in ends the session the browser had before.
- An account may hold any number of sessions at once, one for each browser or device it signs in from. There is no limit, because each user sees all of theirs in My profile (when each started, was last used and expires, and its browser) and can end any of them there. Ending one of one's own sessions asks for no password or code: it only takes access away.
- An SSO sign-in starts a PerchTail session like a local one, with the same lifetime and idle timeout, independent of the session at the identity provider: signing out of PerchTail doesn't sign the user out of the identity provider, and signing out of the identity provider, or being disabled there, doesn't end a PerchTail session already open (deactivate the account in Settings → Users to end it at once). PerchTail doesn't read the `acr` or `amr` claims of the ID token, so it assumes nothing about how the user signed in at the identity provider: whether a second factor was asked is the identity provider's policy ([sso-setup.md](sso-setup.md#sessions-and-the-identity-provider)).
- Signing out deletes the session and sends `Clear-Site-Data: "cache"`, so the browser drops what it kept of PerchTail's pages. Changing one's password ends the user's other sessions and gives the current one a new token; turning one's own MFA on or off ends the other sessions; an administrator's password reset, a password reset by email and deactivating the account end all of them. An administrator with Manage users ends all of a user's sessions (only a super admin for an account that manages users or roles), and a super admin can end every session but their own.
- **CSRF**: the `SameSite=Strict` cookie is never sent with a cross-site request; on top of it, a state-changing request whose `Origin` (or `Referer`) names another host than the request's is refused.
- **Response headers**: a Content-Security-Policy that allows only PerchTail's own scripts (`script-src 'self'`, no inline script; inline styles are allowed because the viewer's editor injects them; `form-action 'self'`), `frame-ancestors 'none'` and `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a Permissions-Policy turning off camera, microphone, location and other unused features, and `Cross-Origin-Opener-Policy: same-origin`. HSTS is set by the reverse proxy that terminates TLS, preferably with `includeSubDomains`, which also makes browsers use HTTPS on every subdomain of PerchTail's host name ([hardening.md](hardening.md#network)).
- Every API answer, file content included, is sent with `Cache-Control: no-store` (unless the answer sets its own), so neither the browser nor a proxy keeps a copy. The web interface's static files keep their caching.
- A download is always `application/octet-stream` with `Content-Disposition: attachment`, so a downloaded `.html` or `.svg` file is saved, never shown or run in PerchTail's origin.
- Files from sources aren't scanned for malware. PerchTail shows them as text in the viewer, never runs or renders them, and sends downloads as attachments with `nosniff`, as above; a downloaded file is the workstation's to check, with its own antivirus, like any other file saved from a server.
- A request refused as invalid (422) is logged as `request.validation_failed` with the names of the fields at fault, never their values.
- The web interface loads nothing from other sites: no CDN, web font, analytics or telemetry.
- The interactive API documentation (`/docs`, `/redoc`, `/openapi.json`) answers 404 unless `API_DOCS=true`.

## Cryptography

Every key and algorithm PerchTail uses:

| Key or secret | Algorithm | What it is for | Where it lives | How it is rotated | If it is lost |
|---|---|---|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` and the per-install salt | Fernet (AES-128-CBC with HMAC-SHA256), under a key derived with PBKDF2-HMAC-SHA256 (600,000 iterations) from the key and a random 16-byte salt | Encrypts source credentials, the SSO provider's configuration, users' TOTP secrets, the SMTP password and alert webhook URLs | The key in the environment (`.env`) or in the file `CREDENTIAL_ENCRYPTION_KEY_FILE` names, never in the database; the salt in `DATA_DIR/credential_salt` | `python -m app.rotate_credential_key` re-encrypts everything under a new key ([credential-key-rotation.md](credential-key-rotation.md)); the salt stays for the life of the installation | Every stored secret must be entered again, and users with MFA finish signing in with a backup code ([backup-and-restore.md](backup-and-restore.md#what-the-keys-and-the-salt-protect)) |
| Audit chain key | HMAC-SHA256, under a key derived from `CREDENTIAL_ENCRYPTION_KEY` and the salt with PBKDF2-HMAC-SHA256 and a label of its own | Signs each audit entry together with the previous entry's hash | Never stored: derived at every start | With `CREDENTIAL_ENCRYPTION_KEY`; the rotation re-signs every entry in the same transaction | Entries signed before fail the integrity check; their content stays readable |
| File-start keys | HMAC-SHA256, under one key per source, derived with HKDF-SHA256 from a root derived like the audit chain key | Lets Tail and alerts recognise that a file was replaced, without keeping its bytes | Never stored: derived when needed | With `CREDENTIAL_ENCRYPTION_KEY` | Alerts read the files they watch again from the top, once, and may send lines they had already sent |
| `BACKUP_ENCRYPTION_KEY` (optional; `CREDENTIAL_ENCRYPTION_KEY` when it isn't set) | AES-256-GCM in 1 MiB authenticated blocks, under a key derived for each file with HKDF-SHA256 and a random 16-byte salt | Encrypts backups | The environment | Set a new value: new backups use it, and older ones still need the old value, so keep it as long as those backups | Backups made with it can't be opened; the running installation doesn't use it |
| Release signing key | Ed25519 | Signs the agent binaries' `SHA256SUMS` and the image's digest (`perchtail-image.json`): an agent installs an upgrade from the portal only with a valid signature from the public key built into it | The public key in every agent and in the repository at each release's tag; the private key with the project's release process, never in the repository or the image | A release whose agents carry a new public key, signed with the old key, moves agents over to it; an agent that only knows a key no longer used is upgraded by hand | Agents already installed can't be upgraded from the portal; installing by hand, after checking the release ([verifying-releases.md](verifying-releases.md)), still works |
| Local passwords, previous passwords and MFA backup codes | argon2id (the `argon2-cffi` defaults), with a random salt in each hash | Checks a password or code without keeping it | The database, as hashes only | No key. A password hash made with older parameters is replaced at the next successful sign-in | Nothing can be lost: a forgotten password is reset |
| Session, password-reset, agent enrollment and monitoring tokens | 256 random bits from the operating system's generator, stored as SHA-256 | Recognises a token without keeping it | The database, as hashes only; the token itself only with its holder | Sessions and reset links expire; agent and monitoring tokens are regenerated in Settings, which ends the old one | A lost token is generated again; the database alone signs nobody in |
| TLS | TLS 1.2 or later | Users to the reverse proxy; PerchTail's own connections to the identity provider, `https://` webhooks, the SMTP server, WinRM over HTTPS, a PostgreSQL server off its host (unless `DATABASE_REQUIRE_VERIFIED_TLS=false`) and the release check, and push agents' connections to PerchTail, all with the certificate checked | The proxy's certificate and key on the proxy; CA certificates given to WinRM sources in `DATA_DIR/winrm_ca/`, the SMTP server's in the database | As your certificate authority requires; the proxy's settings are in [hardening.md](hardening.md#network) | A lapsed or lost certificate stops connections until it is replaced |

SSH, SMB and WinRM connections to sources use their own protocols' encryption, described under [Connections to sources](#connections-to-sources).

Every algorithm above comes from a maintained library (`cryptography`, `argon2-cffi`, Python's `hashlib`, `hmac` and `secrets`, Go's `crypto/ed25519`); PerchTail implements none itself. What it encrypts carries a version (a Fernet token its version byte, a backup the format version in its header), so a change of algorithm or of its parameters is never a setting: it comes in a PerchTail release with a migration that re-encrypts or re-signs what is stored, as the key rotation does, and the release notes say so. argon2id is the exception: each hash carries its parameters, and hashes move to new ones as users sign in.

What is never stored in a form that can be read back: local passwords, previous passwords and MFA backup codes (argon2id hashes), and session tokens, password-reset tokens, agent enrollment tokens and the monitoring token (SHA-256 hashes).

- PerchTail refuses to start with the shipped `changeme` key or with a weak key (shorter than 32 bytes once decoded, or less than 128 bits of estimated entropy). `CREDENTIAL_KEY_REFUSE_WEAK=false` lets it start with a weak key, with warnings, while the key is rotated.
- It refuses to generate a new salt while the database holds data encrypted under the old one, so a lost salt is noticed rather than silently making every secret unreadable.
- `python -m app.rotate_credential_key` re-encrypts every secret under a new key and re-signs the audit chain in one transaction ([credential-key-rotation.md](credential-key-rotation.md)).
- The key and the salt are useless without each other. Keep the key outside the host, where you can reach it if the host is lost ([backup-and-restore.md](backup-and-restore.md)).

## The audit log

Every administrative change, every sign-in and sign-out, every file opened, followed or downloaded, every search and every request refused for lack of permission is recorded with who, when, from which address, with which session, and on what. Entries are chained with HMAC-SHA256 under a key that can't be computed from the database alone, so changing, deleting or reordering entries, or moving the chain's anchor, is detected by the daily integrity check. Entries are kept 365 days by default (at least 90, or forever), and only a super admin can shorten that. The fields, every event, the retention and how integrity is checked are in [audit-events.md](audit-events.md).

## Connections to sources

| Protocol | Encryption and identity | The safer setting |
|---|---|---|
| SSH/SFTP | SSH; the server's host key is pinned in `DATA_DIR/ssh_known_hosts`, and a changed key is refused | `SSH_HOST_KEY_POLICY=strict` refuses a host whose key wasn't added by hand; the default `tofu` pins it on first connection |
| SMB | NTLM authentication; signed messages required by default (**Require signing**, per source) | **Encrypt** asks for SMB 3 encryption, ticked for a new source in the source editor; a source with signing off shows a warning |
| WinRM | HTTPS (port 5986) with the server's certificate checked, optionally against the source's own CA; HTTP when chosen, or, for a source with no scheme set (saved before 0.5.0, or imported without `winrm_scheme`), on a port other than 5986; the source editor warns when HTTP is chosen | A JEA endpoint with the read-only role in [examples/jea](examples/jea/README.md), so the account needs no remote shell or administrator rights; a WinRM source without JEA shows a warning |
| Push agent | The agent dials out over `wss://` and refuses `ws://` unless `PERCHTAIL_ALLOW_INSECURE=true`; it authenticates with its enrollment token. Files are read only from an agent that says where links lead (0.7.0 or later) | Keep the agent's base path to the log folders; set `PERCHTAIL_REMOTE_UPGRADE=false` on hosts that must never be upgraded from the portal |
| Local | Files on the PerchTail host, only under `LOCAL_SOURCE_ROOTS` (empty by default, which disables them) | Mount host logs read-only |

On every protocol, paths are checked to stay inside the source's folders, and reads are bounded in size, time and concurrency (`MAX_FETCH_MB`, `MAX_DECOMPRESSED_MB`, `REMOTE_READ_TIMEOUT_SECONDS` and the limits under Settings → Explorer and limits). An archive is read at most 50,000 entries deep: a `.tar.gz` lists its first 50,000, and a `.zip` stating or holding more is refused before its index is loaded, on the server and on push agents. On local, SSH and push-agent sources, the rules see both the path asked for and where it really leads, and a push agent reads a file only once the server has checked the rules on its target; SMB and WinRM sources refuse junctions and symlinks, and a WinRM source reads only paths on a drive of its host. Regular expressions and globs from users that run on the server (rules, the file-name search, alerts, redaction patterns) run in linear time (RE2). Severity indicators are saved in the same syntax and run in the browser, in a Web Worker, where one that takes too long is turned off.

## Input limits

Everything a request sends is checked on the server against a typed model of that request, whatever the web interface already checked: a value of the wrong type, out of range or too long answers 422 (logged as `request.validation_failed`, with the names of the fields at fault and never their values), and fields the request doesn't define are ignored. On top of that, these limits apply:

| What | Limit |
|---|---|
| Display name, email | 100 and 254 characters |
| Password | Between the policy's minimum and maximum (12 and 128 by default; the maximum can be set from 64 to 1,024); a longer one is refused before it is hashed or checked |
| Words passwords can't contain | 50 words |
| Visibility rules | 500 per source, 1,000 characters each, globs or RE2 regular expressions |
| Severity indicators | 1,000 characters each, globs or RE2 regular expressions |
| Redaction rules | 50 per scope (the system's, or a source's own), a name of 48 characters, a pattern of 1,000 characters in RE2 with a bounded compiled size and memory |
| Alerts | 100 per account; the query is plain text or an RE2 regular expression |
| File-name search | 500 characters |
| Folders a source reads | 20 by default (`source_max_roots`), never more than 100 |
| CA certificates (WinRM sources, the SMTP server) | 64 KiB |
| YAML import | 1,000,000 bytes and 5,000 sources per file |
| Folder listing | `listing_max_entries` entries (5,000 by default, 100 to 20,000); a push agent lists at most 50,000 entries of a folder |
| Archives | A `.tar.gz` is listed up to its first 50,000 entries; a `.zip` stating or holding more than 50,000 is refused |
| File size | `MAX_FETCH_MB` fetched and `MAX_DECOMPRESSED_MB` decompressed (2,048 each); over either, 413 |
| Concurrent work | Connector slots in all, per source and per user, and downloads per user (Settings → Explorer and limits); over a limit, 503 with `Retry-After` |

PerchTail sets no overall size on a request body: the reverse proxy's applies (nginx's `client_max_body_size` is 1 MB by default; 2 MB leaves room for a YAML import at its limit).

Some checks look at several values together:

- A source in a folder belongs to that folder's customer, and a folder nests only under a folder of the same customer, never under itself or one of its own subfolders.
- A role holds at most one grant per scope, and a grant at least one capability.
- An alert watches one source, folder or customer at most, and its webhook URL must be `https://` and resolve only to public addresses.
- A WinRM source reads only paths on a drive of its host, and a local source only folders under `LOCAL_SOURCE_ROOTS`.
- A regular expression RE2 can't compile (backreferences, lookaround) is refused when it is saved, so none reaches the server's matching.

## What PerchTail expects from its environment

- A reverse proxy in front of it that terminates TLS and sets HSTS. PerchTail itself speaks plain HTTP and must not be reachable from users' networks except through the proxy.
- A host whose clock is kept right by NTP: sessions, lockouts, password-reset links, TOTP codes and audit timestamps depend on it.
- `CREDENTIAL_ENCRYPTION_KEY` (and `BACKUP_ENCRYPTION_KEY`) generated randomly and kept outside the host. `CREDENTIAL_ENCRYPTION_KEY_FILE` reads the key from a mounted secret instead of the environment.
- `DATA_DIR` on a local disk, readable by PerchTail's user only, and backups copied off the host.

[hardening.md](hardening.md) goes through each of these.

## Releases and vulnerabilities

How to report a vulnerability, how fast fixes ship and which releases get them is in [SECURITY.md](../SECURITY.md). How to check that a release's image, agent binaries and SBOMs are the ones the project published is in [verifying-releases.md](verifying-releases.md).
