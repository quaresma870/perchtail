# Audit log events

PerchTail records who did what, when and from where in its audit log (Settings → Audit log, for super admins and roles with the `view_audit_log` capability). This page lists every event it records, the fields each entry has, how long entries are kept and how their integrity is checked.

## Fields

Every entry has:

| Field | What it holds |
|---|---|
| `timestamp` | When it happened, in UTC. The API returns it with its zone (`2026-11-14T10:00:00Z`); the Audit log page shows it in the browser's time zone and names the zone. |
| `user_id` / `username` | Who did it. Empty for events no user caused (background jobs, failed sign-ins of unknown names). |
| `action` | The event, from the lists below. |
| `target_type`, `target_id` | What it was about (a source, a role, a user...), with the target's name as it was when the event was recorded. |
| `metadata` | Event details: a path, the values before and after a change, a reason. Never a password, an MFA code, a token or file content. |
| `client_ip` | The address the request came from. Behind a reverse proxy, see [Client address](#client-address). Empty for background jobs and for entries recorded before 0.7.0. |
| `user_agent` | The request's `User-Agent`, cut to 256 characters. |
| `session_id` | A short reference to the session that made the request: the first 16 characters of the SHA-256 of its token, which is what the database holds for the session. Never the token. |

Every entry is also written to the application log as a structured `INFO` line with the same action, user, target, address and session, so it can be found with `grep` without querying the database. Refused requests are logged at `WARNING` too.

## Client address

The address recorded is the TCP peer, unless that peer is a trusted proxy: then it is the first address in `X-Forwarded-For`, read from the right, that isn't a trusted proxy. Addresses further left were written by the client and are never believed. A request from anywhere else keeps its own address, and its `X-Forwarded-For` is ignored.

Trusted proxies are set with `TRUSTED_PROXIES`, a comma-separated list of addresses or CIDR blocks; the default is loopback only (`127.0.0.0/8, ::1`). Behind a reverse proxy in another container or on another host, set it to the proxy's address, never to a range that holds clients. The same address is what the monitoring allowlists (`PROBE_ALLOWED_NETWORKS`, `MONITORING_ALLOWED_NETWORKS`, see [monitoring.md](monitoring.md)) check. A malformed entry stops PerchTail at startup.

## Events

### Sign-in and sessions

| Action | When |
|---|---|
| `user.login` | A successful sign-in (local or SSO). |
| `user.login_failed` | A failed local sign-in: wrong password or wrong MFA code (`reason`). Repeated identical failures within 60 seconds are folded into the next entry (`similar_events_folded`). |
| `user.locked_out` | The failure that locked an account or a client address out, with the `scope`: `account`, `account_and_address` (an emergency account: an active local super admin with MFA on, locked only for that address) or `address` (every sign-in from that address). See [troubleshooting.md](troubleshooting.md#too-many-failed-login-attempts-try-again-later). |
| `user.login_throttled` | A sign-in attempt while the account or the address was locked out. |
| `user.sso_login_failed` | A sign-in through the identity provider that didn't end in a session, with the `reason` (`idp_error`, `state_cookie_mismatch`, `invalid_state`, `sso_disabled`, `issuer_mismatch`, `login_failed`...) and, for `idp_error`, the provider's error code. Folded like failed logins. |
| `user.logout` | A sign-out. |
| `session.revoke` | Sessions ended before they expired: one the user ended from their sessions list (`revoked_session_id`), all the others after a password change or an admin's password reset, or all of them when an admin deactivated the account (`count`, `reason`: `password_change`, `password_reset`, `deactivated`). |
| `user.sso_provision` | An account created on a first SSO sign-in. |
| `user.sso_role_synced` | A role set from the identity provider's groups on sign-in. |
| `user.change_password` | A user changed their own password. |
| `user.mfa_enable`, `user.mfa_disable`, `user.mfa_backup_codes_regenerated` | Changes to a user's own second factor. `user.mfa_enable` says whether the account had to set it up (`required`). |
| `user.mfa_required` | A sign-in by an account that must use MFA and hasn't set it up: until it does, it can only set it up. The `reason` is `admin_account` (a super admin or a role with a global capability) or `setting` (**Require MFA for local accounts**). |
| `user.mfa_secret_unreadable` | A sign-in whose TOTP code couldn't be checked because the account's MFA secret can't be decrypted (the encryption key or salt changed). The sign-in is refused like a wrong code; backup codes still work. |
| `user.reauth_failed` | A wrong current password, or a wrong authentication code when turning MFA off, given to confirm a sensitive change to the user's own account, with the `action` it was meant to allow (`change_password`, `mfa_enroll`, `mfa_disable`, `mfa_regenerate_backup_codes`). |
| `user.profile_update`, `user.email_change` | Changes to a user's own name or email. |

### Access to sources and files

| Action | When |
|---|---|
| `source.open` | A source opened in the explorer (its root listed). |
| `source.browse` | A folder below a source's root (or inside an archive) listed. On by default; turned off with the **Audit folder listings** System setting, which only a super admin can turn off. The explorer's periodic refresh of folders already open isn't recorded again. |
| `file.open`, `file.follow` | A file opened, or followed live. When redaction applied to what was sent, `"redaction": "applied"` and `"redaction_at": "server"`. |
| `file.download`, `folder.download` | A file downloaded, or a folder downloaded as a ZIP. When the download was redacted, `"redaction": "applied"` and `"redaction_at": "server"`; a whole archive and a folder ZIP also have `skipped_count`, the files left out because they can't be redacted and listed in `REDACTION-SKIPPED.txt`. In a folder ZIP it counts what its archives and `.gz` files leave out, known before the ZIP starts; a plain file only found to be left out while the ZIP is sent (UTF-32, or compressed in a format redaction doesn't support) is listed in the file and in the application log (`archive.redaction_skipped`), and recorded in `file.download_redaction_skipped`. |
| `file.download_redaction_skipped` | Written once a redacted folder ZIP ends, only when files were found to be left out while it was sent, after its `folder.download` entry: the folder (`path`), how many (`skipped_count`) and their paths on the source (`skipped`). |
| `file.open_denied`, `file.download_denied` | A whole archive refused because it holds files the rules hide (`"reason": "archive_hidden_members"`), or content redaction can't serve: a whole archive in the viewer, or one a download can't read (`"reason": "redaction_whole_archive"`), a UTF-32 file (`"reason": "redaction_unsupported_encoding"`, with `"follow": true` when it came from Tail), content compressed in a format PerchTail doesn't decompress, such as bzip2, xz, zstd, 7z or rar, or gzip or zip in a file not named `.gz` or as an archive (`"reason": "redaction_unsupported_format"`, with `"follow": true` when it came from Tail), or a `.gz` archive member that can't be read (`"reason": "redaction_unreadable"`). |
| `files.search` | A search of the file-name index: the `query`, whether it was a regex, and how many results the page had. Each page fetched is its own entry. |

### Refused requests

| Action | When |
|---|---|
| `access.denied` | Any request refused for lack of permission by a signed-in user, anywhere in the API (HTTP 403, or 404 where the answer mustn't reveal that the target exists: a customer or folder outside the role's grants, a role beyond the reader's authority, the audit log while its page is off): what was `attempted` (method and route), the target when the route names one, and the `reason` (for example `missing_capability:download`, `missing_global_capability:manage_roles`, `role_beyond_authority`). The person refused only gets a neutral message; the reason is only in the audit log. Refusals that already have their own event (`file.*_denied`) aren't recorded twice. |

### Administration

| Action | When |
|---|---|
| `source.create`, `source.update`, `source.delete` | Sources added, changed (with the values before and after) or deleted. When an import sets a source's redaction rules, `source.create` lists each rule with its values, and `source.update` lists each rule added, changed or removed with its values before (`from`) and after (`to`). |
| `source.pins_removed` | Right after a `source.delete`, or a `source.update` that changed the host, port or CA certificate: the source's old SSH host key (`host_key`) or WinRM CA certificate file (`ca_certificate`) was removed, because no other source used it. |
| `source.credential_update`, `source.agent_token_regenerate`, `source.pick_folder` | A source's saved credential replaced, its agent token regenerated, or its folders browsed with the saved credential. |
| `rule.create`, `rule.update`, `rule.delete`, `rule.reorder`, `rule.replace_raw` | Visibility rules changed. |
| `customer.*`, `folder.*` | Customers and folders created, changed or deleted. |
| `role.*`, `role_grant.*` | Roles and their grants created, changed or deleted. A `role_grant.delete` with no user and reason `duplicate_scope` is a duplicate grant removed by the 0.7.0 migration, with the grant kept and the capabilities deleted. |
| `user.create`, `user.update`, `user.deactivate`, `user.reset_password` | User accounts managed by an admin. Resetting the password of an SSO account is refused and recorded as `access.denied` with reason `sso_account`. |
| `user.password_cleared` | With no user and reason `sso_account`: a password an admin had set on an SSO account before 0.7.0, removed by the 0.7.0 migration. |
| `sso_provider.*`, `sso_group_mapping.*` | SSO configuration and group-to-role mappings. `sso_provider.issuer_mismatch` is a **Test** whose discovery document names an issuer other than the configured one. |
| `severity_pattern.*` | Severity highlighting patterns. |
| `redaction_rule.create`, `redaction_rule.update`, `redaction_rule.delete` | Redaction rules added, changed (with the values before and after) or deleted, system-wide (`"scope": "system"`) or on a source (`"scope": "source"`). |
| `redaction_override.set` | A source turning a system redaction rule off (`"disabled": true`) or back on. |
| `alert.create`, `alert.update`, `alert.delete` | Live alerts. |
| `alert.delivery_failed` | An alert notification the webhook never accepted, given up after retrying for an hour (`attempts`, `error`). Each retry before it is only in the application log. |
| `smtp_settings.create`, `smtp_settings.update`, `smtp_settings.delete` | The SMTP server under **Settings → Email** set up, changed or removed: the values before and after (`{"from": ..., "to": ...}`) of the server, port, security, username, sender and **Enabled**. The password is never recorded: a new one is recorded as `"password": "changed"`, a removed one as `"removed"`; a CA certificate as `"replaced"` or `"removed"`. |
| `smtp_settings.test` | A test message: the `recipient` and the `result` (`sent`, `failed` with the `error`, `not_configured`, or `rate_limited`). Never the message. |
| `system_settings.update` | System settings changed: for each setting, its value before and after (`{"old": ..., "new": ...}`); a secret value is recorded only as `"changed"`. |
| `monitoring_token.regenerate` | The monitoring token generated or replaced. |
| `config.export`, `config.import` | Configuration exported or imported. |
| `access_report.generate` | An access report generated, shown or downloaded: the `view` (`users` or `sources`), the `format` (`json` or `csv`), the `scope` (`full` for a super admin, `delegated` otherwise), `inactive_days`, and how many `users` and access `rows` it held. Never the report's content. |
| `credential_key.rotate` | The credential encryption key rotated. |
| `backup.create`, `backup.verify`, `backup.restore` | Backups made, checked and restored. |
| `agent.upgrade`, `agent.upgrade_resume` | Push agents upgraded from the portal. |

### The audit log itself

| Action | When |
|---|---|
| `audit.view` | Someone read the audit log, with the filters they used. Recorded after the page is read, so it isn't on the page it records. |
| `audit.purge` | Entries deleted under the retention setting: how many, which rows, the retention, and the chain anchor they left (see [Integrity](#integrity)). Written by the purge itself, in the same transaction. |
| `audit.anchor_recorded` | Written once, on the first start after upgrading to 0.7.0, for an anchor left by a purge from an older version. |
| `audit.outbox_rejected` | At startup, an event left for PerchTail to record that isn't one it accepts (see [Integrity](#integrity)): the outbox entry's id, its action and when it was written. The event itself isn't recorded, and PerchTail logs `audit_outbox.rejected` at `ERROR`. |
| `audit.integrity_check_failed` | An integrity check, scheduled, run by hand or at startup, that found a break: the first problem row and why (`unsigned_row`, `hash_mismatch`, `unsigned_anchor`, `chain_reset` or `race_breaks`). |
| `audit.chain.known_breaks_recorded` | A super admin recorded breaks from the concurrent-write race fixed in 0.5.2 as known. |

## Retention

Entries are kept for the number of days set under Settings → System → Audit log retention (365 by default) and deleted by a daily purge. The retention is at least 90 days, or `0` to keep every entry forever. Anyone with `manage_system_settings` can lengthen it; only a super admin can shorten it. Each purge is itself recorded (`audit.purge`), and shortening the retention is recorded as a `system_settings.update` with the old and new values. Upgrading to 0.7.0 raises a retention between 1 and 89 days to 90, recorded the same way with no user.

Entry ids are never reused: after a purge, new entries keep counting from the highest id ever written.

## Integrity

Entries form a hash chain: each one's HMAC-SHA256 covers its own fields and the previous entry's hash, keyed from `CREDENTIAL_ENCRYPTION_KEY`. Changing or deleting an entry breaks every hash after it. Entries recorded from 0.7.0 also cover `client_ip`, `user_agent` and `session_id`; each entry records which fields its hash covers, so entries from older versions keep verifying as they were written.

When the purge deletes the oldest entries, the newest deleted one becomes the chain's anchor, and the `audit.purge` entry records that anchor inside the chain. Verification checks that the anchor in use is the one the newest `audit.purge` entry names, so moving the anchor by hand to hide deleted entries is reported as tampering.

Every entry is signed when it is written. An entry without a signature is never signed afterwards, except entries from before the chain existed, once, on the first start that builds it. Any other unsigned entry was written outside PerchTail: it is reported as tampering (`unsigned_row`), and when one is found at startup PerchTail logs it at `ERROR` and checks the chain straight away.

A database migration can't sign entries, so an event it has to record (today the retention raised to 90 days, the duplicate grants removed and the passwords removed from SSO accounts on upgrading to 0.7.0) waits in an outbox table. On the next start, after the chain is checked, PerchTail records each one as an ordinary signed entry at the end of the chain, with no user, and removes it from the outbox. Only the events PerchTail's own migrations write are accepted, in the exact form they write them; anything else is refused and recorded as `audit.outbox_rejected`. Writing to the outbox can only add entries at the end of the chain, never change the ones already there.

Whether the chain has started is also kept outside the database, in `DATA_DIR/.audit-chain-started` (readable by PerchTail's user only, with the time it was written and the first signed entry's id). PerchTail writes it on the first start of a version that has it, once any older entries are signed, and never removes it; a restore puts it back when the backup has a chain ([backup-and-restore.md](backup-and-restore.md#restoring)). With the marker present, an audit log with no signed entry and no anchor, or whose first signed entry comes after the one the marker names, had its signatures taken away or its entries deleted: nothing is signed again, the check reports it as tampering (`chain_reset`), and at startup PerchTail logs `audit_hash_chain.chain_reset` at `ERROR`.

The chain is verified once a day (`AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS`, default `1`), and on demand with **Verify now** on the Audit log page. A failed check is shown on the page, reported by `/monitoring/health`, logged at `ERROR` and recorded as `audit.integrity_check_failed`.
