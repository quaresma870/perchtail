# Data and retention

This page lists everything PerchTail keeps, where, how it is protected, how long it stays and how it is removed, and everything that leaves the installation. Nothing from the logs PerchTail gives access to is kept: file content only passes through a short-lived scratch file (see [Scratch](#scratch)). How the stored secrets are encrypted is in [security-model.md](security-model.md#keys-and-encryption).

## The database

The database is `DATA_DIR/perchtail.db` (SQLite) unless `DATABASE_URL` points elsewhere. `DATA_DIR` and the files in it are readable by PerchTail's user only.

| What | Contents | Protection | How long | How it is removed |
|---|---|---|---|---|
| Configuration | Customers, folders, sources (host, port, folders, protocol options), rules, severity patterns, redaction rules, system settings, the SSO provider and group mappings, the SMTP server | Secrets in it are encrypted (below) | Until changed or deleted | Deleting it in Settings removes it at once |
| Source credentials | Usernames, passwords and SSH keys | Encrypted (Fernet) | Until replaced, or the source is deleted | With the source |
| SSO provider and SMTP secrets | The OIDC client secret, the SMTP password | Encrypted (Fernet) | Until replaced or removed | Removing the provider or the SMTP server |
| User accounts | Username, display name, email, role, sign-in method, the identity provider's subject and issuer for SSO accounts, the IdP group whose mapping gave the role, last sign-in, MFA state, display preferences | Passwords and backup codes only as argon2id hashes; TOTP secrets encrypted | As long as the account exists: accounts are deactivated, never deleted (see [Users](#users)) | Not removed; see [Users](#users) |
| Previous passwords | argon2id hashes | Hash | Only while the password-history rule needs them, and only as many as it needs | Dropped as the history moves on, or when the rule is turned off |
| MFA backup codes | argon2id hashes, with when each was used | Hash | Until the user regenerates them or turns MFA off | Replaced as a set |
| Sessions | SHA-256 of the session token, the browser's User-Agent, created, last seen and expiry times | Hash | `SESSION_TTL_HOURS` (12) at most | Deleted on sign-out, when the user ends them, after a password change or reset (the session that changed the password goes on under a new token), when the user turns MFA on or off (their other sessions), when the account is deactivated, and by an hourly purge once expired |
| Password-reset links | SHA-256 of the token, fingerprints of the password and email they were issued for, expiry | Hash | 5 to 60 minutes (30 by default) | Deleted when used or when a new one is asked for, and by the hourly purge once expired |
| Agent and monitoring tokens | SHA-256 of each push agent's enrollment token and of the monitoring token | Hash | Until regenerated | Regenerating replaces it; an agent's goes with its source |
| Audit log | Who did what, when, from which address, User-Agent and session reference, on which target, with details such as file paths, search queries, the username entered at a failed sign-in, and the old and new values of a change (an email change keeps both addresses) | HMAC hash chain; never passwords, tokens or file content | 365 days by default (Settings → Audit log, at least 90, or `0` for ever) | Deleted by the daily purge, which is itself recorded ([audit-events.md](audit-events.md#retention)) |
| File-name index | Path, size and modification time of each file the rules make visible | Not encrypted; filtered by grants and current rules at every search | While the file exists and stays visible; refreshed every 15 minutes by default | A rule change drops the names it now hides at once; turning the index off for a source, disabling it, changing where it reads from or deleting it drops all its names. Turning the index off in Settings → Source defaults stops passes and searches but keeps what was indexed. |
| Alerts | Name, query, scope, file glob, owner, last run and errors, and the webhook URL | The webhook URL is encrypted and never returned by the API | Until deleted | Deleting the alert, or the source, folder or customer it watches |
| Alert reading position | Per alert and file: path, byte offset, and a keyed hash of the file's first 64 bytes | Not encrypted; never content | While the alert exists | With the alert, or when its source changes host or folders |
| Pending webhook calls | Up to 50 matching lines of up to 2,000 characters each, with their source name and file path | Not encrypted; redacted when redaction applies to the source (a call queued before redaction was turned on is redacted as it is sent) | Until sent, or one hour of retries | Deleted when sent or given up, when the alert is turned off or deleted, when its owner may no longer create alerts, and when its owner can no longer view the source of its lines (audited as `alert.delivery_dropped`) |
| Agent upgrade history | Each upgrade attempt: who asked, versions, times, result | Not encrypted | While the source exists | With the source |
| Operational state | The last run of each job (errors never hold URLs or credentials), the last release check, the audit chain's anchor and last verification | Not encrypted | Overwritten at each run | Replaced in place |

## Files next to the database

| Where | What | How long | How it is removed |
|---|---|---|---|
| `DATA_DIR/credential_salt` | The per-install salt the encryption keys are derived from | For the life of the installation; it must never be lost | Never; it goes with the database |
| `DATA_DIR/ssh_known_hosts` | Pinned SSH host public keys | While a source uses the host | Removed when the last source using it is deleted or changes host or port (`source.pins_removed`) |
| `DATA_DIR/winrm_ca/` | CA certificates given to WinRM sources | While a source uses it | Removed when the last source using it is deleted or changes it |
| `DATA_DIR/initial-admin-password` | The first super admin's temporary password, on a fresh install | Until that password is changed | Deleted when it is |
| `DATA_DIR/.audit-chain-started` | When the audit hash chain started, with the first signed entry's id | For the life of the installation | Never; a restore puts it back |
| `DATA_DIR/.perchtail.lock` | The running instance's host, PID, version and start time | While PerchTail runs | Released when the process ends |

## Scratch

`SCRATCH_DIR` holds the content of the files users open, follow or download, while they do. Its folder is readable by PerchTail's user only, and each file in it is created readable by that user only. When redaction applies, only the redacted copy stays there: the fetched original is deleted before the answer is sent, and a push agent of 0.7.0 or later sends only redacted content.

A scratch file is deleted when the viewer closes it and no one else holds it open, when it has gone unused for `SCRATCH_IDLE_SECONDS` (5 minutes), when scratch is over `SCRATCH_MAX_GB` (oldest unused first), and at every start. A file still being fetched, redacted or sent is never deleted by the idle time or the size limit: its idle time counts from when that ends. Plain files of local sources are read in place and not copied to scratch, except their redacted copy when redaction applies; their `.gz` files and archive members are extracted there like any other source's. Scratch isn't encrypted: keep `SCRATCH_DIR` on a disk only the host can read, or in memory.

## Application logs

`LOG_DIR` holds PerchTail's own logs: requests, background jobs, errors, and one line for each audit event (action, user, target, address and session). They never hold passwords, tokens, request bodies or file content; they do hold file paths, source names and error messages. The folder is readable by PerchTail's user only, and in the web interface by super admins only, through the built-in application-log source.

The active file rotates at `LOG_MAX_FILE_MB` (100) or at midnight; rotated files are compressed and deleted after `LOG_RETENTION_DAYS` (30) or once all logs exceed `LOG_MAX_TOTAL_MB` (2048), oldest first. Logs also go to standard output, where the container runtime or the system journal keeps them under its own retention.

## Backups

`BACKUP_DIR` holds encrypted backups of the database, the salt and the pinned SSH host keys (a restore writes the chain marker again from the database), made by `python -m app.backup create` or on a schedule (every 24 hours on a new install). They contain everything the database does, so their retention is that of the data in them: the last `BACKUP_KEEP` (7) manual and scheduled backups and the last 3 `pre-upgrade` ones are kept, older ones are deleted. Copies taken off the host follow whatever retention you give them. A restore keeps the data it replaced in `<DATA_DIR>.before-restore-<time>` until you delete it ([backup-and-restore.md](backup-and-restore.md)).

## The browser

PerchTail keeps no token and no file content in the browser's storage. The session cookie is `HttpOnly`, so scripts can't read it. Local storage holds only the height of the viewer's panel; session storage, emptied when the tab is closed, holds the sort order and filters of the Settings tables. The explorer's sort order and the connections view are display preferences kept with the user's account. File content and the password-recovery pages are sent with `Cache-Control: no-store`, so the browser doesn't keep them in its cache; what a user downloads is theirs to keep or delete.

## Users

PerchTail keeps the personal data listed above about its users: username, display name, email, the identity provider's identifiers for SSO accounts, sign-in times, the User-Agent of their sessions, and in the audit log what they did, from which address. It keeps nothing about the people whose data may appear in the logs it gives access to, beyond the file paths in the index and in the audit log.

Accounts are deactivated, not deleted, so every audit entry keeps pointing at the account that made it. Deactivating an account (Settings → Users) ends all its sessions at once, stops it signing in, stops its alerts, and makes any password-reset link it had unusable. Its row stays, with its username, name and email; an administrator can edit a local account's name and email, and an SSO account's come from the identity provider. Its audit entries are deleted by the retention purge like everyone else's. The access report flags accounts that haven't signed in for a chosen number of days, for periodic review ([admin-guide.md](admin-guide.md#access-review)).

## What leaves the installation

PerchTail sends no telemetry, usage statistics or crash reports, and the web interface loads nothing from other sites. These are the only connections it makes:

| Connection | When | What is sent | How to turn it off |
|---|---|---|---|
| Release check: `https://api.github.com/repos/quaresma870/perchtail/releases/latest` | At most once a day, on by default | One unauthenticated GET, with nothing about the installation | **Settings → Backups and updates**, card **Updates**, or `UPDATE_CHECK=false`, which makes sure the request is never made |
| Sources | When users browse and open files, every 10 minutes for the background reachability check, and every 15 minutes for the file-name index | The source's credentials, over its protocol, and read commands | Disable or delete the source; the check and the index have their own switches in Settings → Source defaults and on each source |
| Alert webhooks | When an alert matches | The matching lines (up to 50 per call), redacted when redaction applies to the source, source name and file path, to the URL the alert's owner set, which must be `https://` when it is added or changed (an alert saved earlier with `http://` keeps sending without encryption, with a warning in the alerts list and `alert.webhook_insecure` in the log); only public addresses are allowed, checked again before each call, which goes to the address checked and doesn't follow redirects | Disable or delete the alert, or take `manage_alerts` away |
| SSO identity provider | At each SSO sign-in (the discovery document is reused for 10 minutes) and **Test connection** | The discovery and key requests, and the authorization code exchange with the client secret, over `https://` to public addresses only (unless `SSO_ALLOW_HTTP_LOCALHOST=true`, for local development) | Remove or disable the provider |
| SMTP server | Password-reset links, the notice to a user's previous email address, and test messages | The message to its recipient, always over TLS | Turn email off under **Settings → Email** |

`HTTPS_PROXY` is honoured for the release check. A push agent opens its own connection to PerchTail; when PerchTail has no copy of a newer agent release, an agent that is asked to upgrade downloads it from the project's GitHub releases from its own host, which `PERCHTAIL_REMOTE_UPGRADE=false` on that host prevents.
