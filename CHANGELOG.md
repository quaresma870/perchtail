# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/) once a first
release ships (0.x releases may include breaking changes between minors).

## [Unreleased]

### Changed
- **Settings have a sidebar with sections**: Sources (Sources, Folders, Agents, Source defaults), Access (Users, Roles, Single sign-on, Sign-in security), Content (Redaction, Severity indicators, Explorer and limits), Operations (Alerts, Email, Backups and updates, Monitoring, Import / export) and Audit (Audit log, Access review), each page shown only to those who may use it. On a phone-sized screen the sidebar is a list at the top of the page. The settings of the former System page are now on the pages of their section: Source defaults, Sign-in security, Explorer and limits, Alerts, Backups and updates, Monitoring, and the audit log's settings below the log on the Audit log page, which whoever manages system settings keeps seeing while the log is turned off. The old System address opens Explorer and limits. Nothing about the settings themselves, who may change them or how they are audited changes.
- A folder ZIP starts at once, with and without redaction. Its `.zip`, `.tar.gz` and (when downloads are redacted) `.gz` files were all fetched, checked and redacted or rebuilt before the first byte, so a folder with large archives could send nothing for a long time; now the ZIP is sent in order, one file at a time, each archive or `.gz` file fetched, checked on the copy that is zipped and redacted or rebuilt only when its turn comes, and deleted from scratch once it is in the ZIP. Scratch needs room for the folder's largest file (twice that when redacted) instead of all its archives. What the ZIP leaves out is still listed in `SKIPPED.txt` or `REDACTION-SKIPPED.txt`, and the checks that refuse a ZIP before it starts are unchanged.
- The audit of a folder ZIP changed with it: `folder.download` is still written before the ZIP starts, but its `file_count` is now every file listed for the ZIP, and `skipped_archives` and `skipped_count` are gone from it. What the ZIP leaves out is recorded once it ends, however it ends, one entry per file or archive member, with the folder of the ZIP in `folder`: `file.download_denied` (`"reason": "archive_hidden_members"`) without redaction, `file.download_redaction_skipped` with it, which now also covers archives and `.gz` files (reasons `archive_unreadable`, `too_large`, `nested_archive` and, for a member, `encrypted`, `link` or `unsupported_compression`, besides `redaction_unsupported_encoding` and `redaction_unsupported_format`) and no longer groups files per reason (`skipped_count` and `skipped` are gone from it). If the audit entries can't be saved, the log has `archive.late_audit_failed`.
- On a push agent, expanding a `.zip`, `.tar.gz` or `.tgz` in the explorer and opening, reloading or downloading a file inside one are now done on the agent's host also when redaction doesn't apply to the source: the agent lists the archive and sends only the file asked for, instead of sending the whole archive to the server first. The rules are still checked on the server for each file inside, before it is asked for. A whole archive downloaded, a folder ZIP and a plain `.gz` file are still sent whole. As with redaction on, a file inside a `.zip` compressed with a method the agent can't read now answers "The source returned an error" instead of the 409 `archive_member_unreadable`.

### Fixed
- Switching tabs, coming back to the Viewer from another page, or highlighting patterns arriving no longer rebuilds the open file, which froze the page for seconds with large files; each tab also keeps its place and selection.
- A push agent's old connection, closed when the agent reconnects (for example after an upgrade from the portal), no longer logs an error with a traceback.

### Security
- The push agent uses a `gorilla/websocket` commit that fixes GO-2026-6278: the mask key of each WebSocket frame the agent sent came from a weak random generator. Over `wss://`, which the agent requires unless `PERCHTAIL_ALLOW_INSECURE=true`, TLS already protects the frames. The agent now also depends on `golang.org/x/net`, through `gorilla/websocket`.
- Expanding a `.zip` on an SSH, SMB or WinRM source, and opening, following or downloading a file inside it, now read only the bytes they need instead of copying the whole archive to scratch first, every time: listing reads the archive's end (its index), and a file inside it only its own header and compressed data. A large `.zip` over WinRM took as long to list, and as long again to open each file, as copying it whole; it now takes a few requests. `MAX_FETCH_MB` counts what such an operation transfers, so a `.zip` larger than it can be expanded and its files opened, and an operation that would transfer more than it is refused with the same 413. A `.zip` that changes size while it is read, or whose file fails its CRC check, is refused as unreadable (409 `archive_unreadable`, audited). `.tar.gz` files, whole-archive downloads, folder ZIPs and local sources read archives as before, and push agents on their host (above). Each such read logs `archive.ranged_read` (DEBUG) with its requests, bytes and seconds.

## [0.7.0] - 2026-11-23

The release after 0.6.4. It carries everything from the pre-releases 0.7.0-rc.1 to rc.4, whose sections in [CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.7.0/CHANGELOG.md) list every change; the code is the one tested in the lab as rc.4, on Linux, Windows (SMB, WinRM, JEA) and push agent sources. PerchTail is licensed under the AGPL-3.0 from this release on (see Changed in rc.2).

### Upgrading from 0.6.x
Read this list before upgrading: push agents must be updated before their files can be read, administrators must use MFA, and several defaults and permissions change.

- **The content index and the Search page are gone** (#303). The migration drops their tables, so the space they took comes back; nothing can be kept. The `SEARCH_INDEX_*` environment variables are ignored.
- **Alerts read new lines live** (#305). Existing alerts keep their name, query, source and webhook, and start again at the end of each file they watch. Their query is now plain text (anywhere in a line, any letter case) or a regular expression, not full-text search syntax: an alert that used operators like `AND` or quotes needs its query rewritten.
- **Webhook calls changed shape**: each hit has `source_name`, `file_path`, `offset` (byte position in the file) and `line` (the matching line, plain text), in place of `line_number` and `snippet_html`. Receivers that read `snippet_html` need updating.
- A YAML import that still sets `search_indexing_enabled` applies with a warning; from 1.0 it's refused.
- **The health probes need authorization** (#366). `/healthz` and `/readyz` answer only a local caller without proxy headers (the Docker `HEALTHCHECK`), the monitoring token, or an address in the new `PROBE_ALLOWED_NETWORKS`. A load balancer or Kubernetes probe that called them without the token stops working: send the token as `Authorization: Bearer <token>`, or list its network in `PROBE_ALLOWED_NETWORKS` (see docs/monitoring.md, "Who may call them").
- **Back up before upgrading with `python -m app.backup create --reason pre-upgrade`** (#334), from 0.7.0 on: the upgrade steps in the README and docs/install-from-source.md now start with it. Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before.
- **Push agents can be upgraded from the portal from now on** (#319), but agents older than 0.7 don't know the command: upgrade each of them by hand once. For the upgrades that follow, the agent's account must be able to write to the folder its binary is in, and its service must restart it: `Restart=always` with systemd, and a restart on exit for a Windows service (agent/README.md, "Upgrades from the portal").
- **Manage sources is a capability of its own.** The migration keeps what every role could do: in each role with the `create_source` global capability, every grant that has `manage_rules` also gets `manage_source`. Roles without `create_source` couldn't change sources, so their grants are left as they are. After the upgrade, take `manage_source` off the grants where a role should only edit rules, and give it where a role should add sources: adding a source or a folder now needs it where it goes. Downgrading the migration removes `manage_source` from every grant.
- **Audit retention is at least 90 days** (#252, #383), or `0` to keep entries forever. The migration raises a retention set between 1 and 89 days to 90, and the first start after it records that in the audit log as a `system_settings.update` with no user, with the old and new values. Only a super admin can shorten the retention from now on.
- **`TRUSTED_PROXIES` sets which reverse proxies are believed** (#252, #383), for the address recorded in the audit log and the one the monitoring allowlists check. It replaces uvicorn's `FORWARDED_ALLOW_IPS` advice in docs/monitoring.md: behind a proxy in another container or on another host, set `TRUSTED_PROXIES` to its address. The default is loopback only, as before. The Docker image now starts uvicorn with `--no-proxy-headers`, so PerchTail alone reads `X-Forwarded-For`; an install from source should add `--no-proxy-headers` to the `ExecStart` line of its systemd unit (docs/install-from-source.md).
- The migration rebuilds the audit log table so entry ids are never reused after a purge; on a large audit log it takes a while and needs free disk for a second copy of the table while it runs. The hash chain is unchanged.
- **Only a super admin creates customers.** A role with `create_source` sees and changes only the customers and folders its grants reach: give a delegated admin `manage_source` in a grant on a customer for them to rename or delete it, and on a folder for them to rename it.
- **Duplicate grants are merged.** Where a role had more than one grant for the same customer, folder or source, the migration keeps the one with the lowest id (the one access was decided by), deletes the others, and the first start after it records each deleted grant in the audit log as a `role_grant.delete` with no user, reason `duplicate_scope` and the capabilities it had. Check those roles' grants after upgrading. Downgrading the migration doesn't bring the deleted grants back.
- **The audit chain is checked every day** instead of once a year: `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` now defaults to 1.
- **Administrators must use MFA.** Local accounts of super admins and of roles with any global capability must set up two-factor authentication: at their first sign-in after upgrading, they can only set it up. Make sure each admin has an authenticator app at hand. SSO accounts aren't affected.
- **SSO accounts lose any PerchTail password.** The migration removes the password an admin's reset may have left on an SSO account (and its previous password hashes), and the first start after it records each one in the audit log as a `user.password_cleared` with no user and reason `sso_account`.
- **The SSO issuer must be `https://`**, and so must the endpoints its discovery document names: a provider saved with an `http://` issuer stops signing in (the log has `sso.login.insecure_idp_url`) until its issuer is changed. The issuer must also match the discovery document's `issuer` exactly, a trailing slash aside (the log has `sso.login.issuer_mismatch` otherwise); **Test connection** checks it. `SSO_ALLOW_HTTP_LOCALHOST=true` allows `http://` on a loopback address, for local development only.
- **The first super admin's temporary password is no longer logged**: on a fresh install it is in `DATA_DIR/initial-admin-password` (readable only by PerchTail's user), and the log says where. Upgrading installs aren't affected; a temporary password logged by an earlier version stays in the rotated logs until they expire.
- **Behind a reverse proxy, set `TRUSTED_PROXIES`** if you haven't: failed sign-ins are now also counted per client address, and without it every client has the proxy's address. The first request since startup that carries `X-Forwarded-For` or `Forwarded` from a peer outside `TRUSTED_PROXIES` logs a warning (`client_address.untrusted_forwarded_header`, with the peer's address), and `/monitoring/health` then reports `trusted_proxies_suspect: true` until the next restart.
- **`docker-compose.yml` publishes PerchTail on `127.0.0.1` only.** Other machines reach it through a reverse proxy that terminates TLS (README, "Quick start"); to publish it on every address again, change the port to `"8080:8000"`. The compose file also runs the container with a read-only root filesystem, `no-new-privileges` and every capability dropped but the few the entrypoint's root step needs. If you run your own compose file or `docker run`, the README lists the settings, and the preferred way to run as PerchTail's user (`user: "10001:10001"`, with no capability at all).
- **The scheduled backup is on by default for new installs**, every 24 hours. An existing install keeps what it had: the migration stores the old default (off) where the interval was never set. Turn it on in Settings → System → Backups.
- **SMB signing is a setting of each SMB source.** PerchTail always required signed SMB messages, and every source, new or existing, keeps requiring them. A source whose server can't sign can turn **Require signing** off, and its page then warns about it; **Encrypt** asks for SMB 3 encryption.
- **`/docs`, `/redoc` and `/openapi.json` answer 404** unless `API_DOCS=true`. The API is internal to the web UI.
- **Alert webhook URLs are encrypted and never returned.** The migration encrypts the stored URLs, so it needs the same `CREDENTIAL_ENCRYPTION_KEY` and salt as the app. The alerts API returns `webhook_url_masked` (scheme, host and port) instead of `webhook_url`, and an update without `webhook_url` keeps the stored one.
- **A weak `CREDENTIAL_ENCRYPTION_KEY` is warned about** at startup (`startup.weak_credential_encryption_key`) and in Settings → System: shorter than 32 bytes once decoded, or less than 128 bits of estimated entropy. Change it with `python -m app.rotate_credential_key`. From 1.0 a weak key is meant to stop PerchTail from starting; `CREDENTIAL_KEY_REFUSE_WEAK=true` does that already.
- **A WinRM source reads only paths on a drive.** A source saved with a UNC (`\\server\share`) or device (`\\?\`, `\\.\`) Base path or folder stops being read, with a message to correct it (the log has `winrm.base_path_not_on_drive`): change it to a path on a drive of the WinRM host, or read the share through an SMB source.
- `PERCHTAIL_TEST_PATCH_MODULE` is honoured only with `PERCHTAIL_TEST_MODE=true`, which only the end-to-end test server sets; set without it, PerchTail refuses to start, and the Docker image refuses both.
- **An SSO account no group mapping matches drops to the no-access role** at its next sign-in, audited as `user.sso_role_synced` with reason `no_matching_mapping`. Before, it kept the role it had, so leaving an IdP group didn't take away the role the group gave, and a role assigned by hand in Settings → Users stayed. Before upgrading, check that every SSO user who should keep access is in a group with a mapping. The last active super admin is never demoted this way: the role is kept and the log has `sso.role_sync_kept_last_super_admin`.
- **The identity provider must be on a public address.** The discovery document, the JWKS and the token request follow the same address policy as alert webhooks: the host must resolve only to global addresses, PerchTail connects to the address it checked, and redirects aren't followed. An IdP reached at a private, loopback or link-local address stops signing in (the log has `sso.login.discovery_failed`). `SSO_ALLOW_HTTP_LOCALHOST=true` also lets through a loopback host, for local development only.
- **Only a super admin deletes or turns off the SSO provider**, and an admin with **Manage SSO settings** short of super admin deletes only the group mappings whose role is within their own authority, as for creating and changing them. Since the last matching mapping wins, they also can't create, change or delete a mapping at or after the position of a mapping whose role is beyond their authority.
- **The username of a new SSO account comes from `email` only when the ID token says `email_verified: true`**; otherwise from `preferred_username`, or `sub`. Existing accounts keep their username.
- **Push agents must be updated before their files can be read.** The server reads files only from an agent that tells it where links lead (`links/1`, agents from 0.7 on). An older agent stays connected and its folders can still be browsed, but opening, Tail, downloads, folder ZIPs, archives and alerts are refused (409, "This source's agent must be updated…", audited with the reason `agent_update_required`) until it is updated; alerts on its files wait where they are. **Settings → Agents** marks such agents with **Update required**. Agents older than 0.7 must be upgraded by hand once (docs/agent-service.md, "Upgrading by hand").
- **A push agent checks `PERCHTAIL_BASE_PATH` at startup**: one whose folder doesn't exist, isn't a folder or can't be read by its account stops with exit code 1 and a message naming the problem, and a service manager set to restart it restarts it in a loop, instead of connecting and failing every listing. Check the folder before upgrading an agent.
- **Test connection and checking a source's status need Manage sources** on the source: a role with only View no longer sees **Test connection** or can click a status to check it again. Give `manage_source` to the grants that should keep it.
- **Only a super admin acts on other user or role admins**: resetting the password of, deactivating or changing the role of an account whose role has `manage_users` or `manage_roles` is refused to anyone else (`access.denied`, `peer_admin`).
- **Sources take their folder's customer.** The migration gives a source saved in a folder with another customer, or none, the folder's customer, lists its id in the migration log and records it as a `source.update` with no user and reason `folder_customer`. Check those sources' grants.
- **Links are judged by where they lead** on local, SSH and agent sources: a symlink (or, on a Windows agent, a junction) that leads to a file or folder the rules hide is no longer listed or read, even when its own name is visible. A link to a visible file, such as `current.log` to today's log, keeps working. Check the rules of sources whose logs are reached through links into folders the rules exclude.
- **Exclude rules ignore letter case on every source**, glob and `re:`: on SSH, local and Linux-agent sources, `secret/**` now also hides `SECRET/` and `Secret/`. Include rules are unchanged.
- **Severity regexes use the rules' syntax.** A severity indicator saved before with lookaround or a backreference, or longer than 1000 characters, keeps working, and can still be switched off, given another level or switches, or deleted in Settings; only a new pattern for it must be in the rules' syntax.
- **A `.tar.gz` with more than 50,000 entries** (files, folders and links together) lists only its first 50,000, and its members past them can't be opened: split it into smaller archives.

### Highlights
- **Redaction** of secrets, card numbers and personal data in what users open, Tail and (optionally) download, on the server or on the push agent's host, so the original never leaves it.
- **Push agents upgraded from the portal** (Settings → Agents): manual or inside an automatic window, with binaries checked against the release's Ed25519 signature by the agent itself.
- **Links judged by where they lead**: the rules are checked on a symlink's or junction's target, in listings and in every read, on local, SSH and agent sources.
- **Live alerts** that read only what was appended, like Tail, and a **file-name index** behind one search box in the explorer; the content index and the Search page are gone.
- **Access report** for access reviews, a **Manage sources** capability of its own, customers and folders shown only within a role's grants, and MFA required of administrators.
- **Password recovery by email** (off by default) and a system SMTP server in Settings → Email.
- **Encrypted online backups** (`python -m app.backup`), scheduled every 24 hours on new installs, and one instance per `DATA_DIR`.
- **Operations**: `/readyz`, Prometheus `/metrics`, Zabbix templates, limits on connector work, a free-space check before fetches, capped listings, and a per-job history in `/monitoring/health`.
- **Large files and archives**: WinRM reads a file through one shell with larger envelopes, push agents report progress on long reads and archive listings, and folders asked for as files are refused alike on every connector.
- **Verifiable releases**: the Docker image is signed with cosign, and each release carries a signed `SHA256SUMS` and `perchtail-image.json`.

### Known issues
- WinRM is still much slower than SMB or a push agent for large files and archives (docs/source-setup.md, WinRM, "Large files"); prefer SMB or an agent for big logs on Windows.

## [0.7.0-rc.4] - 2026-11-23

The code of 0.7.0-rc.3 with the fixes below: large files and archives read through push agents and WinRM, scratch files in use during long reads, folders and links to folders read as files, and missing files on local sources. Upgrade notes are the same as for rc.2; upgrade the push agents to get the agent fixes.

### Changed
- Each archive rebuilt for a redacted download (downloaded whole or inside a folder ZIP, on the server or from what a push agent redacted) now logs `archive.rebuild_done` (INFO) with the source, the archive's path, the size of the rebuilt archive, how many files went in redacted and how many were left out, and how long it took.
- Opening or downloading a file over WinRM takes far fewer round trips. Through a WinRS shell, a read used a new shell and a new PowerShell process for every 4 MiB; it now uses one shell and one PowerShell process for the whole file, which is opened once after the junction check and written to scratch as it arrives. Every WinRM request, through a WinRS shell or the JEA endpoint, asks for WS-Management envelopes of 512000 bytes instead of 153600, so each response carries over three times as much; a server whose `MaxEnvelopeSizekb` is below 500 is asked again with 153600 and logs `winrm.envelope_fallback`. WinRM is still much slower than SMB or the push agent for large files (docs/source-setup.md, WinRM, "Large files"). At `LOG_LEVEL=DEBUG`, each read through a WinRS shell logs `winrm.fetch_done`.

### Fixed
- Expanding a `.zip`, `.tar.gz` or `.tgz` in the explorer that is over `MAX_FETCH_MB` (or, listed on a push agent, over `MAX_DECOMPRESSED_MB` once read) answered HTTP 500; it now gets the same HTTP 413 and message as opening or downloading it, with and without the flat listing of all members. Any read over these limits that an endpoint doesn't refuse itself also gets that 413 rather than a 500.
- On a push-agent source with redaction, an archive or `.gz` file over `MAX_FETCH_MB` was refused with the `MAX_DECOMPRESSED_MB` message when both limits had the same value (the default). The agent now says which limit was passed, and the server names that one; with an agent older than 0.7.0 the server still tells them apart by their size. Upgrade the agents for the right message.
- On a push-agent source with redaction, opening or downloading a file near the end of a large `.tar.gz` failed after 30 seconds with "The agent didn't answer in time", because the agent must read the archive up to that file before it sends anything. An agent from 0.7.0 tells the server every 10 seconds that it is still reading for a request that returns content (`progress/1`), only while its read moves forward, and the server keeps waiting while it does; a read that hangs still fails after 30 seconds, and the size limits still bound what it reads. Older agents and servers behave as before. Upgrade the agents to get it.
- On a push-agent source with redaction, expanding a large `.tar.gz` in the explorer, or downloading it whole, no longer fails after 30 seconds with "The agent didn't answer in time" while the agent reads it through to list its members: listing an archive now sends the same `progress/1` messages, and an agent that announced `progress/1` without them behaves as before.
- Opening or downloading a file that took longer than `SCRATCH_IDLE_SECONDS` (300 seconds by default) to fetch, redact or send could fail with HTTP 500, or leave copies in scratch that counted towards `SCRATCH_MAX_GB` until the next restart: the idle sweep deleted the scratch file while it was still in use. A file being fetched, redacted or sent is now never deleted by the idle sweep or the size guard, its idle time counts from when that ends, and a scratch file no longer known to PerchTail is deleted when it is closed.
- A source whose background checks had failed a few dozen times in a row lost its backoff: working out the next delay overflowed, so it was checked again at every check interval (10 minutes by default) without backing off and logged `source.backoff_not_saved` each time. The delay now stops at its 30-minute cap, and a source already in that state backs off again at its next failed check.
- Opening, following (Tail) or downloading a folder, or a link to one, through the API answered HTTP 500 on SSH and SMB sources and 502 on push agents, and wasn't audited. On every source it now answers exactly as a path that isn't there (HTTP 404) and is audited as `file.open_denied` or `file.download_denied` with the reason `not_a_file`. A link to a folder the rules hide (such as a link to `secret` with an `exclude secret/**` rule) has the rules checked on that folder, as browsing it does, and is refused as `link_target_hidden`. A push agent from this version answers a folder with its own `is_directory` code; older agents keep working, recognized by their message.
- On a local source (including the built-in log viewer), opening or downloading a file that isn't there, or that is deleted (a log rotated away) just as it is opened, answered HTTP 500. It now answers HTTP 404 with the same answer as on every other connector, and a file deleted while it is being sent is still sent whole.

## [0.7.0-rc.3] - 2026-11-21

The code of 0.7.0-rc.2 with a smaller image. Upgrade notes are the same as for rc.2.

### Security
- The Docker image no longer contains pip: it is removed once the backend's requirements are installed, since nothing runs it afterwards. Its own advisories no longer apply to the image.

## [0.7.0-rc.2] - 2026-11-18

A feature and security release: redaction, password recovery by email, the access review, encrypted backups, links judged by where they lead, and hardening across authentication, authorization, connectors and the push agent. Read Upgrading before upgrading: push agents must be updated before their files can be read, administrators must use MFA, and several defaults and permissions change.

### Upgrading
- **The content index and the Search page are gone** (#303). The migration drops their tables, so the space they took comes back; nothing can be kept. The `SEARCH_INDEX_*` environment variables are ignored.
- **Alerts read new lines live** (#305). Existing alerts keep their name, query, source and webhook, and start again at the end of each file they watch. Their query is now plain text (anywhere in a line, any letter case) or a regular expression, not full-text search syntax: an alert that used operators like `AND` or quotes needs its query rewritten.
- **Webhook calls changed shape**: each hit has `source_name`, `file_path`, `offset` (byte position in the file) and `line` (the matching line, plain text), in place of `line_number` and `snippet_html`. Receivers that read `snippet_html` need updating.
- A YAML import that still sets `search_indexing_enabled` applies with a warning; from 1.0 it's refused.
- **The health probes need authorization** (#366). `/healthz` and `/readyz` answer only a local caller without proxy headers (the Docker `HEALTHCHECK`), the monitoring token, or an address in the new `PROBE_ALLOWED_NETWORKS`. A load balancer or Kubernetes probe that called them without the token stops working: send the token as `Authorization: Bearer <token>`, or list its network in `PROBE_ALLOWED_NETWORKS` (see docs/monitoring.md, "Who may call them").
- **Back up before upgrading with `python -m app.backup create --reason pre-upgrade`** (#334), from 0.7.0 on: the upgrade steps in the README and docs/install-from-source.md now start with it. Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before.
- **Push agents can be upgraded from the portal from now on** (#319), but agents older than 0.7 don't know the command: upgrade each of them by hand once. For the upgrades that follow, the agent's account must be able to write to the folder its binary is in, and its service must restart it: `Restart=always` with systemd, and a restart on exit for a Windows service (agent/README.md, "Upgrades from the portal").
- **Manage sources is a capability of its own.** The migration keeps what every role could do: in each role with the `create_source` global capability, every grant that has `manage_rules` also gets `manage_source`. Roles without `create_source` couldn't change sources, so their grants are left as they are. After the upgrade, take `manage_source` off the grants where a role should only edit rules, and give it where a role should add sources: adding a source or a folder now needs it where it goes. Downgrading the migration removes `manage_source` from every grant.
- **Audit retention is at least 90 days** (#252, #383), or `0` to keep entries forever. The migration raises a retention set between 1 and 89 days to 90, and the first start after it records that in the audit log as a `system_settings.update` with no user, with the old and new values. Only a super admin can shorten the retention from now on.
- **`TRUSTED_PROXIES` sets which reverse proxies are believed** (#252, #383), for the address recorded in the audit log and the one the monitoring allowlists check. It replaces uvicorn's `FORWARDED_ALLOW_IPS` advice in docs/monitoring.md: behind a proxy in another container or on another host, set `TRUSTED_PROXIES` to its address. The default is loopback only, as before. The Docker image now starts uvicorn with `--no-proxy-headers`, so PerchTail alone reads `X-Forwarded-For`; an install from source should add `--no-proxy-headers` to the `ExecStart` line of its systemd unit (docs/install-from-source.md).
- The migration rebuilds the audit log table so entry ids are never reused after a purge; on a large audit log it takes a while and needs free disk for a second copy of the table while it runs. The hash chain is unchanged.
- **Only a super admin creates customers.** A role with `create_source` sees and changes only the customers and folders its grants reach: give a delegated admin `manage_source` in a grant on a customer for them to rename or delete it, and on a folder for them to rename it.
- **Duplicate grants are merged.** Where a role had more than one grant for the same customer, folder or source, the migration keeps the one with the lowest id (the one access was decided by), deletes the others, and the first start after it records each deleted grant in the audit log as a `role_grant.delete` with no user, reason `duplicate_scope` and the capabilities it had. Check those roles' grants after upgrading. Downgrading the migration doesn't bring the deleted grants back.
- **The audit chain is checked every day** instead of once a year: `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` now defaults to 1.
- **Administrators must use MFA.** Local accounts of super admins and of roles with any global capability must set up two-factor authentication: at their first sign-in after upgrading, they can only set it up. Make sure each admin has an authenticator app at hand. SSO accounts aren't affected.
- **SSO accounts lose any PerchTail password.** The migration removes the password an admin's reset may have left on an SSO account (and its previous password hashes), and the first start after it records each one in the audit log as a `user.password_cleared` with no user and reason `sso_account`.
- **The SSO issuer must be `https://`**, and so must the endpoints its discovery document names: a provider saved with an `http://` issuer stops signing in (the log has `sso.login.insecure_idp_url`) until its issuer is changed. The issuer must also match the discovery document's `issuer` exactly, a trailing slash aside (the log has `sso.login.issuer_mismatch` otherwise); **Test connection** checks it. `SSO_ALLOW_HTTP_LOCALHOST=true` allows `http://` on a loopback address, for local development only.
- **The first super admin's temporary password is no longer logged**: on a fresh install it is in `DATA_DIR/initial-admin-password` (readable only by PerchTail's user), and the log says where. Upgrading installs aren't affected; a temporary password logged by an earlier version stays in the rotated logs until they expire.
- **Behind a reverse proxy, set `TRUSTED_PROXIES`** if you haven't: failed sign-ins are now also counted per client address, and without it every client has the proxy's address. The first request since startup that carries `X-Forwarded-For` or `Forwarded` from a peer outside `TRUSTED_PROXIES` logs a warning (`client_address.untrusted_forwarded_header`, with the peer's address), and `/monitoring/health` then reports `trusted_proxies_suspect: true` until the next restart.
- **`docker-compose.yml` publishes PerchTail on `127.0.0.1` only.** Other machines reach it through a reverse proxy that terminates TLS (README, "Quick start"); to publish it on every address again, change the port to `"8080:8000"`. The compose file also runs the container with a read-only root filesystem, `no-new-privileges` and every capability dropped but the few the entrypoint's root step needs. If you run your own compose file or `docker run`, the README lists the settings, and the preferred way to run as PerchTail's user (`user: "10001:10001"`, with no capability at all).
- **The scheduled backup is on by default for new installs**, every 24 hours. An existing install keeps what it had: the migration stores the old default (off) where the interval was never set. Turn it on in Settings → System → Backups.
- **SMB signing is a setting of each SMB source.** PerchTail always required signed SMB messages, and every source, new or existing, keeps requiring them. A source whose server can't sign can turn **Require signing** off, and its page then warns about it; **Encrypt** asks for SMB 3 encryption.
- **`/docs`, `/redoc` and `/openapi.json` answer 404** unless `API_DOCS=true`. The API is internal to the web UI.
- **Alert webhook URLs are encrypted and never returned.** The migration encrypts the stored URLs, so it needs the same `CREDENTIAL_ENCRYPTION_KEY` and salt as the app. The alerts API returns `webhook_url_masked` (scheme, host and port) instead of `webhook_url`, and an update without `webhook_url` keeps the stored one.
- **A weak `CREDENTIAL_ENCRYPTION_KEY` is warned about** at startup (`startup.weak_credential_encryption_key`) and in Settings → System: shorter than 32 bytes once decoded, or less than 128 bits of estimated entropy. Change it with `python -m app.rotate_credential_key`. From 1.0 a weak key is meant to stop PerchTail from starting; `CREDENTIAL_KEY_REFUSE_WEAK=true` does that already.
- **A WinRM source reads only paths on a drive.** A source saved with a UNC (`\\server\share`) or device (`\\?\`, `\\.\`) Base path or folder stops being read, with a message to correct it (the log has `winrm.base_path_not_on_drive`): change it to a path on a drive of the WinRM host, or read the share through an SMB source.
- `PERCHTAIL_TEST_PATCH_MODULE` is honoured only with `PERCHTAIL_TEST_MODE=true`, which only the end-to-end test server sets; set without it, PerchTail refuses to start, and the Docker image refuses both.
- **An SSO account no group mapping matches drops to the no-access role** at its next sign-in, audited as `user.sso_role_synced` with reason `no_matching_mapping`. Before, it kept the role it had, so leaving an IdP group didn't take away the role the group gave, and a role assigned by hand in Settings → Users stayed. Before upgrading, check that every SSO user who should keep access is in a group with a mapping. The last active super admin is never demoted this way: the role is kept and the log has `sso.role_sync_kept_last_super_admin`.
- **The identity provider must be on a public address.** The discovery document, the JWKS and the token request follow the same address policy as alert webhooks: the host must resolve only to global addresses, PerchTail connects to the address it checked, and redirects aren't followed. An IdP reached at a private, loopback or link-local address stops signing in (the log has `sso.login.discovery_failed`). `SSO_ALLOW_HTTP_LOCALHOST=true` also lets through a loopback host, for local development only.
- **Only a super admin deletes or turns off the SSO provider**, and an admin with **Manage SSO settings** short of super admin deletes only the group mappings whose role is within their own authority, as for creating and changing them. Since the last matching mapping wins, they also can't create, change or delete a mapping at or after the position of a mapping whose role is beyond their authority.
- **The username of a new SSO account comes from `email` only when the ID token says `email_verified: true`**; otherwise from `preferred_username`, or `sub`. Existing accounts keep their username.
- **Push agents must be updated before their files can be read.** The server reads files only from an agent that tells it where links lead (`links/1`, agents from 0.7 on). An older agent stays connected and its folders can still be browsed, but opening, Tail, downloads, folder ZIPs, archives and alerts are refused (409, "This source's agent must be updated…", audited with the reason `agent_update_required`) until it is updated; alerts on its files wait where they are. **Settings → Agents** marks such agents with **Update required**. Agents older than 0.7 must be upgraded by hand once (docs/agent-service.md, "Upgrading by hand").
- **A push agent checks `PERCHTAIL_BASE_PATH` at startup**: one whose folder doesn't exist, isn't a folder or can't be read by its account stops with exit code 1 and a message naming the problem, and a service manager set to restart it restarts it in a loop, instead of connecting and failing every listing. Check the folder before upgrading an agent.
- **Test connection and checking a source's status need Manage sources** on the source: a role with only View no longer sees **Test connection** or can click a status to check it again. Give `manage_source` to the grants that should keep it.
- **Only a super admin acts on other user or role admins**: resetting the password of, deactivating or changing the role of an account whose role has `manage_users` or `manage_roles` is refused to anyone else (`access.denied`, `peer_admin`).
- **Sources take their folder's customer.** The migration gives a source saved in a folder with another customer, or none, the folder's customer, lists its id in the migration log and records it as a `source.update` with no user and reason `folder_customer`. Check those sources' grants.
- **Links are judged by where they lead** on local, SSH and agent sources: a symlink (or, on a Windows agent, a junction) that leads to a file or folder the rules hide is no longer listed or read, even when its own name is visible. A link to a visible file, such as `current.log` to today's log, keeps working. Check the rules of sources whose logs are reached through links into folders the rules exclude.
- **Exclude rules ignore letter case on every source**, glob and `re:`: on SSH, local and Linux-agent sources, `secret/**` now also hides `SECRET/` and `Secret/`. Include rules are unchanged.
- **Severity regexes use the rules' syntax.** A severity indicator saved before with lookaround or a backreference, or longer than 1000 characters, keeps working, and can still be switched off, given another level or switches, or deleted in Settings; only a new pattern for it must be in the rules' syntax.
- **A `.tar.gz` with more than 50,000 entries** (files, folders and links together) lists only its first 50,000, and its members past them can't be opened: split it into smaller archives.

### Added
- **Access report** (#384): who holds what, where, for access reviews, from the same grant resolution that decides every request. `GET /access-report` lists every active user with their role, sign-in method, the identity-provider group whose mapping gave the role, last sign-in, MFA state, global capabilities, flags (`super_admin`, `global_capabilities`, `local_without_mfa`, `must_change_password`, `inactive`, `reaches_all_sources`) and their capabilities on each source with the grant that decided them (`view=users`), or each source with who holds what on it (`view=sources`), as JSON or as a CSV download (`format=csv`) stamped with when and by whom it was generated. It needs the new `view_access_report` global capability (shown as "View access report"), which no other capability implies; a role with it short of super admin gets a report limited to the sources where it holds a capability itself, without folder names or grant scopes above its own grant. Every report generated is audited as `access_report.generate`, without its content. SSO users' role group is recorded from their next sign-in on (new column `user.sso_role_group`), and cleared when an admin changes their role by hand. **Settings → Access review** shows it to roles with "View access report": by user, with filters by flag and each user's access grouped by customer and folder, or by source, with the users holding each capability and the grant behind it; a field for the days without sign-in that make an account inactive; the time and author of the report shown, and a note when it is limited to the reviewer's scope; and **Export CSV** and **Export JSON**, which download the report from the server for the same view and days. See docs/admin-guide.md, "Access review".
- **The audit log says where each action came from** (#252, #383): the client's address, its User-Agent and a short reference to the session (the first 16 characters of the SHA-256 of its token, never the token), in new columns on the Audit log page and in the API. Behind a reverse proxy listed in `TRUSTED_PROXIES`, the address is the client's, read from `X-Forwarded-For` from the right; an `X-Forwarded-For` from anywhere else is ignored.
- **More events in the audit log** (#252, #383): sign-outs (`user.logout`), sessions ended early (`session.revoke`), failed SSO sign-ins (`user.sso_login_failed`, folded like failed logins), a wrong current password when changing one's password or MFA (`user.reauth_failed`), every request refused for lack of permission anywhere in the API (`access.denied`, with what was attempted, on what and why; the person refused still only gets a neutral message), file-name searches with their query (`files.search`), folders listed below a source's root (`source.browse`), reads of the audit log with their filters (`audit.view`), each audit purge (`audit.purge`), failed integrity checks (`audit.integrity_check_failed`) and alert notifications given up (`alert.delivery_failed`). `system_settings.update` now records each setting's value before and after.
- **Audit folder listings**, a System setting (Settings → System → Audit log), on by default: turned off, folders listed below a source's root are no longer recorded. Only a super admin can turn it off; anyone who manages system settings can turn it on.
- `.env.example` documents `TRUSTED_PROXIES`.
- **Require MFA for local accounts**, a System setting (Settings → System → Two-factor authentication), off by default: on, every local account must use two-factor authentication, not only administrators. Only a super admin can change it. An account that must use MFA and hasn't set it up is taken to **Set up two-factor authentication** after signing in, and the API refuses everything else (403 `mfa_setup_required`); audited as `user.mfa_required`, and `user.mfa_enable` records whether it was required.
- `SSO_ALLOW_HTTP_LOCALHOST` (false by default), for a local test identity provider over `http://` on a loopback address.
- docs/audit-events.md lists every audited event, the fields of an entry, retention and how integrity is checked. A test fails when the code records an event the page doesn't list, or the page lists one nothing records.
- Security documentation: docs/security-model.md (trust boundaries, capabilities and grants, sign-in, sessions, keys and encryption, connections to sources), docs/hardening.md (a production checklist) and docs/data-and-retention.md (what is kept, for how long, how it is removed, and every connection that leaves the installation), linked from the README.
- **Bulk credential update** (#248). In Settings → Sources, tick sources and **Update credentials** to set one new username and password (or SSH key) on all of them; each source keeps its own encrypted copy. By default the new credential is tried on each source first and applied only where it works, with a summary of what was updated, failed and skipped; every update is audited per source, without the secret. Sources show how old their credential is (sortable), with a warning and a filter past an age set in Settings → System (90 days by default, 0 turns it off).
- **Live alerts** (#305): a pass every 60 seconds (Settings → System) reads only what was appended to the files each alert watches, the way Tail does, and keeps nothing of their content. An alert watches a source, a folder and its subfolders, a customer, or every source its owner can view, and picks files with a glob (`**/*.log` by default) among those the file-name index lists; compressed files and archives are never read. Queries are text or RE2 regular expressions. A rotated or rewritten file is read from the top. Each source gets at most 20 files and 15 seconds per alert per pass; files that don't fit are read in the next passes and counted in `/monitoring/health`.
- **File-name index** (#304), on by default. PerchTail lists each source's rule-visible files in the background (a few sources at a time, each again once its index is older than 15 minutes) and keeps their names, sizes and modification times, never their content. `GET /files` searches them across every source the user can view, as text, glob or regex, with no cap on results (paged); the explorer's search box uses it to find files (#300). Settings → System turns it on or off and sets the interval (5 minutes to a day); each source can be left out, and its page shows how many files were found, when, why a pass stopped short, and an **Index now** button. Folders that can't be listed, or that are deeper than 32 levels, are skipped without dropping what was indexed below them. Search checks each source's current rules, and a rule change drops the names it now hides straight away.
- Settings → Folders shows each customer's sources inside their folders, and folders and sources move by **drag and drop**: onto a folder, or onto the customer's top level. A folder can't be dropped into itself or below itself; the Move button still does the same from the keyboard (#355).
- Settings → Users has an **Edit** popup per user (click the username or Edit): display name and email for local accounts, role and active, with the username read-only (#356).
- Every background job has a stable id and name, and its last start, finish, result, duration and error are stored in the database and shown per job in `/monitoring/health` (#327). Stored errors never include URLs or credential-looking values.
- `REMOTE_READ_TIMEOUT_SECONDS` (60 by default): how long a connected SSH/SFTP or SMB source may stay silent before the call fails (#343).
- `SHUTDOWN_TIMEOUT_SECONDS` (20 by default): how long a stop waits for running background jobs and the file-name index and source-check workers to finish (#335). `docker-compose.yml` now sets `stop_grace_period: 30s`; see docs/high-availability.md for Kubernetes.
- `GET /readyz` for readiness probes (#330): ready only when a database write works, `DATA_DIR` is writable and the scheduler runs, 503 naming what failed otherwise. `/healthz` stays liveness-only; the Kubernetes example in docs/high-availability.md now uses `/readyz` for its readiness probe.
- `/monitoring/health` reports more (#330): free and total disk for `DATA_DIR`, `SCRATCH_DIR` and `LOG_DIR`, database and WAL file size, the file-name index (sources taking part, never indexed, oldest pass age, passes that failed or stopped short), failing and backing-off sources, the last audit integrity check, the answering instance (hostname and PID), the age of the last alert pass and of each job's last success, and the number of failed jobs. docs/monitoring.md documents every field and lists the log events worth alerting on.
- `GET /metrics` in the Prometheus text format, behind the monitoring token, built from the same computation as `/monitoring/health` (#331). docs/monitoring.md has a scrape config and example alert rules.
- Zabbix templates for Zabbix 7.0 LTS and 8.0 LTS in `docs/examples/zabbix/` (#332): one HTTP agent item on `/monitoring/health`, dependent items, triggers with macro thresholds, and discovery of the background jobs.
- An hourly `wal_checkpoint` job checkpoints SQLite's write-ahead log and truncates it, and connections set `journal_size_limit` to 64 MiB, so the WAL file no longer keeps the size of its biggest burst (#346).
- **Server-side limits on connector work** (#344). Folder listings, file opens, downloads, folder ZIPs and archive listings take a slot while they talk to a source: 16 at once in all and 4 on any one source by default, and each user may have 2 downloads or folder ZIPs in progress. When a slot isn't free the request is answered 503 with `Retry-After: 5` and a clear message instead of waiting; the explorer and the viewer show it with a **Try again**. The limits are in Settings → System (Limits), and what's in use, the limits and the requests refused show in `/monitoring/health` (`limits`) and `/metrics`. The defaults will be tuned by the load tests (#277).
- **Free-space check before a fetch** (#345): a new open, download, folder ZIP or archive listing is refused with HTTP 507 and a message saying why when scratch is at `SCRATCH_MAX_GB` (after evicting what nobody has open) or when the filesystem under `SCRATCH_DIR` is down to `SCRATCH_MIN_FREE_GB` / `SCRATCH_MIN_FREE_PERCENT` free (1 GiB or 5%, whichever is larger; 0 for both turns it off). `/monitoring/health` reports the floor as `scratch.min_free_bytes`.
- **Capped listings** (#347): a folder or an archive lists at most 5000 entries (Settings → System → Limits, 100 to 20000), folders first; the response's `X-Listing-Truncated` header says when there were more, and the explorer shows "Showing the first N entries; narrow with the search".
- The viewer keeps at most 30 tabs open, says so when a 31st is opened, and warns before opening a file over 200 MB, offering **Tail the end** (the last 1 MiB, then Tail as usual), **Download** or **Open anyway** (#347). Tail keeps the last 200,000 lines of a tab and says when older ones were dropped.
- **Upgrade push agents from the portal** (#319), in the new **Settings → Agents** page, for users who can manage sources. The server sends an agent nothing but its own version; the agent downloads the release from the server it is connected to, or from the GitHub release if the server has no copy, installs it only after checking it (see Security), and restarts into it: in place on Linux (same PID, so systemd keeps tracking it), by exiting with code 3 for the service manager on Windows. Anything that fails leaves the current version running and shows why. Agents older than 0.7 are reported as too old to upgrade remotely.
- Settings → Agents sets **Remote upgrades** for every agent: Off (the default), Manual (an **Upgrade** button per agent and **Upgrade all outdated agents**) or Automatic. Each agent's row has its own policy, **Follow the system** by default, or Off, Manual or Automatic whatever the system's (#319).
- **Automatic agent upgrades** (#319): inside a window set by days of the week, start time and length (1 to 24 hours) in the server's time zone, shown on the page, the server upgrades connected outdated agents whose policy is Automatic, 5 at a time by default (1 to 50), and never asks an agent again in the same window once its upgrade didn't work. The page summarises the last window: upgraded, failed, suspended.
- **Failure limit for agent upgrades** (#319): after 3 failed, refused or timed-out upgrades in a row (1 to 10), automatic upgrades skip the agent until someone clicks **Resume** (audited as `agent.upgrade_resume`); a new server version doesn't resume it. A manual **Upgrade** still works on a suspended agent: a success clears the count, a failure adds to it.
- **Every agent upgrade attempt is kept** (#319): who asked (a user or the schedule), the from and to versions, start, end, status and message. An upgrade only succeeds once the agent reconnects reporting the new version, and fails as timed out after 10 minutes without news from the agent. The Agents table shows each agent's last result; clicking it shows the history, most recent first. The source's page shows the last result and links to the Agents page. Each attempt is audited as `agent.upgrade` with the versions, the trigger and the result, when the agent answers and when it ends; policy changes are audited as `source.update` and `system_settings.update`.
- YAML import and export carry a push-agent source's `agent_upgrade_policy` (`inherit`, `off`, `manual` or `automatic`) (#319).
- The Docker image carries the signed agent binaries of its version in `/app/agent-binaries` (`AGENT_BINARIES_DIR`), served to connected agents at `/agent/binaries/` with their own token, so hosts without internet access can upgrade. Settings → Agents says when a server has none (an image built from source or in CI) and agents will use GitHub instead.
- `PERCHTAIL_REMOTE_UPGRADE=false` on an agent's host refuses every upgrade from the portal, whatever the server asks (#319).
- Releases carry `SHA256SUMS.sig`, an Ed25519 signature of `SHA256SUMS` with the release key; the release workflow fails if the `AGENT_SIGNING_KEY` secret is missing or doesn't match the public key in the agent's source (#319).
- Property-based API tests (#376): Schemathesis generates requests from the app's OpenAPI schema and checks that none gets a server error, and a test checks that every endpoint refuses callers who aren't signed in, except a short list of public ones.
- Accessibility checks in the end-to-end tests (#377): axe-core runs on the main pages (login, viewer, Settings, Alerts, My profile) and fails on serious or critical WCAG 2.0/2.1 A and AA violations.
- **One instance per `DATA_DIR`** (#334). PerchTail and `alembic upgrade` take an operating-system lock on `DATA_DIR/.perchtail.lock` before opening the database; a second instance on the same `DATA_DIR` refuses to start, and a migration refuses to run while PerchTail does, with `startup.instance_lock_held` naming the host, PID, version and start time of the one running. The lock ends with its process, so a crash never leaves a stale one. An older image started on a database a newer version already migrated refuses with `startup.database_newer_than_app` instead of an Alembic error (docs/monitoring.md, docs/high-availability.md).
- **Encrypted online backups** (#334). `python -m app.backup create` backs up the database (SQLite's backup API, one consistent snapshot while PerchTail keeps running, checked with `quick_check`), the credential salt and SSH `known_hosts` into one file in the new `BACKUP_DIR` (`/data/backups` in the image), with a manifest of the version, Alembic revision, time, reason and each file's SHA-256. Backups are always encrypted with AES-256-GCM in authenticated 1 MiB blocks, under a key derived from `CREDENTIAL_ENCRYPTION_KEY` or from the optional `BACKUP_ENCRYPTION_KEY`, so a changed, truncated or reordered file is detected. `verify` checks a backup without restoring it; `restore` (PerchTail stopped) refuses, changing nothing, while PerchTail runs, for a database newer than this version, or when the keys don't match (`--force` only skips the check that the current `CREDENTIAL_ENCRYPTION_KEY` decrypts the backup's credentials), and keeps the previous data in `<DATA_DIR>.before-restore-<time>`. The last `BACKUP_KEEP` (7) manual and scheduled backups and the last 3 `pre-upgrade` ones are kept. Settings → System sets a scheduled backup (1 hour to a week, or 0 for off; every 24 hours on a new install, see Upgrading; after a failed attempt it waits an hour before trying again) and lists the backups; they can't be downloaded. Each action is audited (`backup.create`, `backup.verify`, `backup.restore`). The command refuses to run as root. docs/backup-and-restore.md covers backups, keys, restoring, rolling back an upgrade, copies off the host and testing a restore, and the upgrade steps now start with a `pre-upgrade` backup.
- **Redaction** of secrets, card numbers and personal data in file content (#382, #249): the server's redaction engine, with built-in detectors for card numbers (Luhn and card brand), bearer tokens, `Authorization` headers, `password=`-style values, passwords in URLs, private keys, JWTs, email addresses, IPv4/IPv6 addresses and IBANs, plus custom RE2 patterns with the longest match they can make; the push agent has the same engine, checked against the server's with shared test vectors, and can now redact on its host the content, archive members and decompressed `.gz` files it sends, so the original never leaves the host; system-wide and per-source redaction rules (the built-in detectors, off by default, and custom patterns), with a source able to turn a system rule off; and the redaction settings (off by default). Rules and settings are configured by super admins only, audited with their values before and after, and kept in the configuration import and export (`redaction_rules`, `redaction_disable`). With redaction on, the viewer and Tail get the content redacted by the server, for every user, super admins included: the server keeps only the redacted copy in scratch, the viewer shows how many values were replaced and highlights each `[redacted: ...]`, a line still being written shows as `[redacted: incomplete line]` until it is complete, a file opened at its end starts at the next whole line, UTF-16 files are redacted and shown as UTF-8, a `.gz` file inside an archive is decompressed and redacted, UTF-32 files and whole archives aren't shown (their files open one by one), content compressed in a format PerchTail doesn't decompress (bzip2, xz, zstd, 7z, rar, and gzip or zip content in a file not named as one, such as a rotated `app.log.1`, told by its first bytes) isn't shown or downloaded and the refusal is audited, and each open and Tail start is audited with `"redaction": "applied"`. Downloads are redacted too while the "Redact downloads too" setting is on (the default): single files, `.gz` files and archive members are redacted as they are sent (UTF-16 files stay UTF-16), whole `.zip` and `.tar.gz` archives are rebuilt in the same format with their files redacted and without the files the rules hide, folder ZIPs hold every file redacted, what can't be redacted (encrypted members, symbolic links in a `.zip`, zip members compressed with a method PerchTail can't read, archives inside archives, UTF-32 files, files in those compressed formats) is left out and listed in `REDACTION-SKIPPED.txt`, a file only found to be left out while a folder ZIP is sent gets its own audit entry once the ZIP ends, every redacted download carries an `X-PerchTail-Redacted` header and, by default, `.redacted` in its name (`app.redacted.log`), the viewer's download button says "Download (redacted)", and each download is audited with `"redaction": "applied"`. For a push-agent source whose agent says it can redact (agents from this version on), everything is redacted on the agent's host instead, and the original never reaches the server: opening a file, an archive member or a `.gz` file, the Tail, downloads, whole archives (listed and read on the host, rebuilt on the server from the redacted members) and folder ZIPs; the server only uses an answer that says it was redacted with the rules it sent, a rule the agent can't run is named in the error instead of falling back, an archive inside an archive isn't downloaded from an agent, and the audit says where the content was redacted (`"redaction_at": "agent"` or `"server"`). An agent that says where links lead (`links/1`) but can't redact on its host gets the requests of before and the server redacts what it sends, logged once per connection as `agent.redaction_fallback`; agents older than 0.7 do neither, get no such log line, and their files can't be read until they are updated (see Upgrading); the features an agent announces are kept with its source (`agent_features`). Alerts run their query on the redacted lines, for every alert, so a value that was replaced can't be matched, and only redacted lines are queued and sent to webhooks; they are redacted as the Tail's (on the agent's host for an agent that can), each hit's `offset` is still where its line starts in the original file, and turning redaction on or off neither restarts a file nor repeats a match. A webhook call still waiting to be sent or retried is redacted as it goes out, with the rules that apply then, so one queued before redaction was turned on doesn't send its lines in clear, and it isn't sent while the rules can't run. A file redaction can't read waits where it is and is logged once (`alert.file_read_failed`) until it changes. Super admins configure redaction on the new **Settings → Redaction** page (the switches, with a confirmation that redaction applies to everyone; the built-in rules, each with what it finds and its false positives; custom rules for every source, with a box to try a pattern on sample text) and in the new **Redaction** section of each source's editor (which system rules apply there, turning one off for that source, and the source's own rules), with a warning on a push-agent source whose agent can't redact on its host, or must be updated before its files can be read; nobody else sees either. In Tail, the count of redacted values follows what the tab still holds once old lines leave it. [docs/redaction.md](docs/redaction.md) describes the rules, where content is redacted and what redaction doesn't cover; redaction is best effort. Tail and alerts now identify a file's start with a keyed hash instead of a plain SHA-256.
- **System SMTP server** (part of #244), in the new **Settings → Email** page for super admins: server, port, STARTTLS or implicit TLS, an optional username and password (encrypted at rest, never shown again), sender address and name, an optional CA certificate for an internal CA, and **Enabled**. Nothing is configured or sent by default. The connection always uses TLS with the certificate verified against the server's name; a server that doesn't offer STARTTLS is refused rather than used in plain text, and there is no mode without TLS. The server may be on a private network but not on loopback, link-local, multicast, unspecified or reserved addresses; its name is checked when saved and again before every message, and PerchTail connects to the address it checked. **Send a test message** sends a fixed plain-text message (at most 5 a minute for the installation). Saving, removing and testing are audited (`smtp_settings.*`), never with the password or the message; the password is covered by key rotation, the missing-salt check at startup and the backup key check. docs/admin-guide.md describes the page.
- **Password recovery by email** (#244), off by default: a super admin turns it on in Settings → System (Password recovery) once email is set up and on and `PUBLIC_BASE_URL` is `https://`, and sets how long links work (5 to 60 minutes, 30 by default). The sign-in page then shows **Forgot password?**: a user of a local account enters their username or email and gets a link to choose a new password. The answer is the same, and as fast, whether or not an account matches; super admins, roles with any global capability, SSO accounts and accounts without an email never get a link, checked again when it's used. The link carries a 32-byte token after `#`, so it never reaches the server's or a proxy's logs; only its SHA-256 is stored, it works once, and it stops working when it expires, when the password or the email changes or when another link is asked for. The new password follows the password policy, every session of the account ends, and no session is opened, so the next sign-in still asks for the second factor. Requests and token uses are rate-limited per address and per account, and audited (`user.password_reset_requested`, `user.password_reset_completed`, `user.password_reset_failed`), never with the token, the link or the password; a request that matches no account keeps the username or email entered, as failed sign-ins keep the username.
- **Load-test kit** in `loadtest/` (part of #277): it seeds a test instance through the API (customers, folders, sources of every protocol, rules, users and push agents, with a ledger so `clean` removes only what it created), generates and grows the served log files, runs k6 scenarios (browsing, Follow, downloads, file-name search, agents, large files and a ramp to the breaking point), records the server's CPU, memory, scratch, database and health while they run, and writes a summary, with `tiny`, `small`, `medium` and `large` profiles. It also runs files that grow while they are read (opened, downloaded, zipped and followed with every answer checked line by line, at rising write rates, and a busy log's day), a `production` profile of an installation in use (170 sources, five kinds of people through a working day, an incident in the middle), and a `protocol_matrix` profile that measures what each protocol and feature costs, one thing at a time. It only runs against hosts listed in `LOADTEST_ALLOWED_HOSTS`. docs/sizing.md is the skeleton of the sizing guide; its numbers come from measurements still to be made.
- **Key strength check** for `CREDENTIAL_ENCRYPTION_KEY` at startup (`app/key_strength.py`): a weak key is a warning in the log and at the top of Settings → System (`GET /system-settings/warnings`), with what to do; `CREDENTIAL_KEY_REFUSE_WEAK=true` refuses to start with it.
- **SMB signing and encryption per source**: **Require signing** (on by default) and **Encrypt** (SMB 3, off by default) in the source editor, passed to the connector, the folder picker and the bulk credential test, and in the YAML import and export (`smb_require_signing`, `smb_encrypt`; left out of a file, an existing source keeps its setting). A source with signing off shows a warning on its page.
- A WinRM source without a JEA endpoint shows a warning in the source editor and on its page: the account then needs remote-shell rights, in practice an administrator; the warning links to the guide for the read-only JEA endpoint.
- `API_DOCS=true` serves `/docs`, `/redoc` and `/openapi.json`, which are off by default.
- Deleting a source, or changing its host, port or CA certificate, removes its old SSH host key from `known_hosts` and its old WinRM CA certificate file when no other source uses them, audited as `source.pins_removed`.
- docs/backup-and-restore.md explains the `before-restore` copy a restore leaves next to `DATA_DIR`: what it holds and when to delete it.

### Changed
- **Changing your own email needs your current password** (#244), in My profile, throttled like the other password re-confirmations (`user.reauth_failed` with `action: email_change`), and when email is on, the previous address is told of the change (`user.email_change_notice`).
- Audit times are unambiguous: the audit API returns them in UTC with their zone (`Z`), and the Audit log page shows them in the browser's time zone, naming the zone in each time and in the Time header. Before, the page read UTC times as local ones.
- `SECURITY.md` has a support and end-of-life policy: active support (bug and security fixes), end of support (security fixes only) and end of life (no fixes; the release, image, signatures and SBOM stay available). Until 1.0, only the latest release is supported. From 1.0, the latest minor has active support, and the previous minor keeps active support for 6 months after the next minor is released and security fixes until 12 months. Security fixes now ship within 3 days whatever their severity, instead of 7 days for medium and low. `TRADEMARKS.md` is stricter about the name and the logo, and says it doesn't limit what the AGPL-3.0 allows with the code. The ROADMAP is short again, with the plan to 1.0 and what comes after it, and its design notes moved to `docs/design-decisions.md` (#339).
- **License:** PerchTail is now licensed under the GNU Affero General Public License v3.0 (AGPL-3.0-only) instead of MIT. You can still use, change and share it, including running it for others; whoever changes it and lets people use the changed version over a network has to offer them its source. Releases up to 0.6.4 stay under MIT. Contributions are accepted under a Contributor License Agreement (`CLA.md`), signed once.
- **One search in the explorer** (#300). The box finds sources (by name, host, customer or folder) and file names in every source the user can view, from the file-name index, without picking a mode; results replace the tree while there is text in the box, each with its source and folder, and say when the index last listed the files. Text matches anywhere, `*` and `?` make a glob, and a regex goes between slashes (`/app\.log\.\d/`); the Regex and Match case options and the Advanced filter button are gone (always case-insensitive). **Filter sources**, next to List/Tree, shows the checkboxes: with sources or folders ticked, the search runs live in those, as before, including sources left out of the index.
- Settings → System groups its settings into cards (Explorer, Monitoring, Sources, Alerts, Limits, Updates, Backups, Audit log, Two-factor authentication, Password recovery, Password policy) laid out in columns across the page width, one column on narrow screens (#357).
- The audit log uses the full width of the browser. Its filters are dropdowns with a search box on top: Action, and one per target type (customer, folder, role, source, user...) to pick single targets by name or every target of the type, including ones deleted since. Clicking the Time, User, Action or Target header sorts it; the server sorts, so the order covers every matching entry and not only the page on screen (#358).
- The image's `latest` tag only moves to a release that is the newest full release, so a fix for an older line (for example a 0.6.x after 0.7.0) moves its own `X.Y` tag and leaves `latest` on the newest version.
- Background source checks run four sources at a time, each with a deadline, so a source that stalls only holds its own worker and is shown as failing (#343).
- A source whose background check fails three times in a row backs off: its background checks and file-name index passes are skipped for a growing, jittered time (up to 30 minutes), and it stays failing until any operation on it works. The count survives restarts; browsing the source is never blocked by it (#343).
- Folder ZIP downloads and remote copies of archives are built under `SCRATCH_DIR` instead of the system temp directory, and scratch usage in `/monitoring/health` and the size guard is measured on disk, so they count too (#329).
- A disabled source is off: it keeps its configuration in Settings → Sources, but it no longer appears in the Viewer, open tabs from it close, and nothing can be listed, opened, followed, downloaded or checked on it (the API answers 409), whatever the user's grants. Before, only background work skipped it and it could still be browsed.
- `/monitoring/health` turns `degraded` when a background job's last run failed, when the data, scratch or log filesystem has less than 5% free, or when the audit hash chain is broken (#330).
- The audit log purge deletes in chunks of 5000 rows, each in its own short transaction, instead of one large delete holding the database's write lock (#346).
- Audit integrity verification reads the log in blocks of 2000 rows and keeps only the hashes it can still need, instead of loading the whole table; a purge running at the same time waits for it, so it never reports a break that isn't there (#346).
- **Folder ZIPs are streamed** as they're built (#345): each file is fetched fresh into scratch, written to the download and deleted before the next, so a ZIP is never held whole on disk or in memory. The folder's `.zip` and `.tar.gz` files are the exception: they're fetched and checked before the ZIP starts, and wait in scratch for their turn. The size limit (`MAX_FETCH_MB` on the listed total, checked before anything is fetched, and on what is actually fetched) and the rule filtering are unchanged. The response has no `Content-Length` any more, and a failure after it has started (a source error, a file that grew past the limit) cuts the download short instead of answering an error; the audit entry's `file_count` is the number of files in the ZIP.
- An archive's members are listed once when it's expanded in the explorer (`GET /sources/{id}/browse?path=<archive>&all=true`) and kept while it's open, instead of fetching the archive again for every folder clicked inside it (#347).
- Glob patterns, in rules and in the file-name search, are compiled with RE2 like regular expressions, so a pattern with many `*` can't make matching slow down (#347). A file or folder whose name contains a control character (a newline, a tab) is no longer listed, as it already couldn't be opened.
- A push agent lists at most 50,000 entries of a folder, or fewer once the message nears 8 MiB, and says when it stopped (#347); the server asks for that cap and enforces it for older agents too. Rules are applied on the server afterwards, so a huge folder may show fewer matches than it holds.
- The folder ZIP walk, like the file-name index, stops 32 folders down and lists each folder only once by its real path (SSH) or device and inode (local disk), so a symlink loop ends at once (#347).
- `docker-compose.yml` and the install docs recommend separate volumes for the database, scratch and logs (#345).
- **Dead push-agent connections are noticed** (#333). The server pings each agent every 30 seconds and drops a connection it has heard nothing from for 90 seconds, so a connection a NAT or firewall dropped silently no longer shows the agent as connected; agents older than 0.7 answer too. The agent pings the server every 25 seconds and reconnects when it has heard nothing for 75 seconds, older servers included.
- A push agent's reconnect delay starts again from 1 second after a connection that stayed up for a minute, and every delay is random between half and one and a half times the backoff (within 1 to 30 seconds), so agents don't all reconnect at the same moment after a server restart (#333).
- Versions like `0.7.0-beta.1` are recognised alongside release candidates, ordered before them, so the server and push agents compare and upgrade between beta pre-releases. The update check never offers a pre-release to an install, even if GitHub's latest release were one.
- **Manage sources** (`manage_source`), a new capability in a role's grants, replaces manage rules for changing sources and folders: editing, moving and deleting sources, their agent token, browsing with their saved credential, and moving and deleting folders. It also governs what only this version has: changing credentials in bulk (#248) skips the sources without it; upgrading a push agent from the portal, setting its upgrade policy or resuming it (#319) needs it, and upgrade all leaves out those agents; the Agents list and an agent's upgrade history show only those sources; dragging a source or a folder to another place (#355) follows the move rules. Moves still can't widen the mover's access. Adding a source or a folder needs create source and manage sources where it goes (the customer, or the parent folder); with neither a customer nor a folder, only a super admin adds a source. Manage rules goes back to governing only the visibility rules, and manage sources gives no access to them. The role editor offers it in each grant, duplicating a role keeps it, and a delegated admin can only grant it where they hold it.
- A super admin role whose active users are the only active super admins left can no longer lose its super admin flag (409), as the last active super admin already couldn't be deactivated.
- The role editor and the roles list show readable names for capabilities and scopes ("Manage rules", "Manage sources", "Customer"...) instead of their keys; the API, the import/export YAML and the audit log keep the keys.
- `docker-compose.yml`: the port on `127.0.0.1`, a read-only root filesystem with `/tmp` in memory, `no-new-privileges`, and `cap_drop: [ALL]` with only `CHOWN`, `DAC_READ_SEARCH`, `SETUID`, `SETGID` and `SETPCAP` for the entrypoint's root step, which drops them all before the app starts.
- The scheduled backup defaults to every 24 hours on a new install.

### Removed
- The full-text content index, the Search page, `GET /search`, the per-source "indexed for search" option and the Search setting (#303). Searching file names across sources is the file-name index (#304); watching for lines is live alerts (#305).

### Security
- Every response now carries a `Permissions-Policy` that turns off browser features PerchTail never uses (camera, microphone, location, USB, payments and others) and `Cross-Origin-Opener-Policy: same-origin`, and the Content Security Policy adds `form-action 'self'`. Every change is now also scanned with OWASP ZAP against the running image, and the repository's supply-chain practices with OpenSSF Scorecard.
- **Releases can be verified** (#350). The Docker image is signed with cosign, keyless through GitHub's OIDC identity and recorded in Sigstore's transparency log, and the image and the push-agent binaries carry GitHub build provenance (`gh attestation verify`); the agents' `SHA256SUMS` keeps its Ed25519 signature. Each release also has CycloneDX SBOMs for the backend, the frontend and the agent, next to the image's. docs/verifying-releases.md shows how to check each of them. Releases before 0.7.0 carry only the agents' `SHA256SUMS`, the image's SBOM and the image's BuildKit provenance in the registry.
- The backend's `requirements.txt` (and `requirements-dev.txt`) pin every package with its hashes, and the Docker image installs them with `pip --require-hashes`, so a package that changed on the index fails the build instead of reaching the image. A from-source install does the same (docs/install-from-source.md).
- Each release carries `perchtail-image.json`, naming its Docker image by digest, and `perchtail-image.json.sig`, an Ed25519 signature over it with the same release key as the agents' `SHA256SUMS` (#320). It lets a client check that an image comes from a genuine release, whatever the registry's tags say.
- `/healthz` and `/readyz` no longer answer the world: a caller needs to be local, in `PROBE_ALLOWED_NETWORKS`, or hold the monitoring token, and a refused probe learns nothing about the instance's health. The new `MONITORING_ALLOWED_NETWORKS` can also restrict `/monitoring/health` and `/metrics` to the monitoring system's addresses, on top of the token (#366).
- A push agent upgraded from the portal (#319) installs a binary only when the Ed25519 signature over the release's `SHA256SUMS` verifies against the release public key built into the agent, the binary matches its SHA-256 line, and the version is newer than its own; wherever the files came from, a server that is compromised can't make it run anything but a genuine, newer PerchTail release, nor downgrade it. The server never sends a URL, only the version. An agent built without the public key refuses every remote upgrade. Downloads are capped in size, and the binaries the server serves are limited to its own version's release files.
- The audit chain's anchor, which marks where purged entries ended, is now recorded inside the chain by each purge (#252, #383), so moving it by hand to hide deleted entries is reported as tampering. The first start after upgrading records an anchor left by an older purge once.
- Audit entries recorded from 0.7.0 cover their address, User-Agent and session in their hash; each entry records which fields its hash covers, so entries from older versions keep verifying as they were written.
- An audit entry without a signature is reported as tampering instead of being signed at the next start (#252, #383): only entries from before the chain existed are signed late, once, on the first start that builds it. Such an entry fails the integrity check, turns `/monitoring/health` `degraded` and is logged at `ERROR` when PerchTail starts.
- **Customers and folders only within a role's grants.** Customer and folder lists show a role only the customers its grants reach (on the customer, one of its folders or one of its sources) and the folders below a folder or customer grant, plus the folders on the path to a source grant; a super admin still sees all of them. A customer or folder outside that reach answers 404, as if it didn't exist, also when adding or moving a source there or granting it to a role, and the refusal is audited as `access.denied`. Only a super admin creates customers; renaming or deleting a customer needs `manage_source` in a grant on that customer, and renaming a folder needs `manage_source` on it, like moving and deleting it. Before, any role with `create_source` listed every customer and folder, created customers, and renamed any customer or folder and deleted any empty customer.
- A role can hold only one grant per customer, folder or source: a second one is refused with 409, and the database enforces it. With two, which one access was decided by was undefined.
- A delegated admin (`manage_roles`, `manage_users` or `manage_sso`) sees only the roles within their own authority, the same rule that decides who may assign or edit them: the others are left out of the roles list and pickers, and reading one, or its grants, answers 404. In Settings → Users, a user holding such a role shows it by id and can't be changed there. Before, they could read every role and its grants, and with them which customers, folders and sources other roles reach.
- With the Audit log page turned off (`audit_view_enabled`), the audit log API now answers 404 to everyone but a super admin, instead of still answering anyone with `view_audit_log`.
- **SSO accounts can't sign in with a local password.** An admin could reset an SSO account's password, and the local sign-in didn't check the account's provider, so the account could then sign in without the identity provider or its MFA. Resetting an SSO account's password is now refused (409, audited as `access.denied` with reason `sso_account`), and the local sign-in refuses any account that isn't local, with the same answer and the same work as a wrong password.
- **Sign-in throttling also counts failures per client address**: `LOGIN_MAX_ATTEMPTS` failures from one address (the same limit as per username, 5 by default), whatever the usernames, lock that address for `LOGIN_LOCKOUT_SECONDS`, and other addresses keep signing in. Everyone behind one NAT or shared proxy shares that address, so five failures by anyone there lock it for all of them; behind a reverse proxy, set `TRUSTED_PROXIES`. Before, a flood of made-up usernames from one address filled the tracker and refused every new username from everywhere. An active local super admin with MFA on is now locked per username and address, so an anonymous caller can't lock the break-glass account out from every address; until its MFA is on, it's locked per username like any other account. `user.locked_out` records the `scope` that was locked.
- **Deactivating a user ends their sessions at once** (audited as `session.revoke` with reason `deactivated`); reactivating the account no longer brings old sessions back.
- **The first super admin's temporary password goes to a file, not the log**: `DATA_DIR/initial-admin-password`, mode 0600, deleted when that password is changed (by the admin at first sign-in, or by another admin's reset).
- **OIDC sign-in uses PKCE** (S256) on top of the client secret, and the ID token must carry `exp` and `iat`. The issuer and the endpoints from its discovery document must be `https://`, checked when the provider is saved and before every request to the identity provider. The discovery document's `issuer` must be the configured one (a trailing slash aside); otherwise the sign-in and **Test connection** are refused and audited (`user.sso_login_failed` with reason `issuer_mismatch`, `sso_provider.issuer_mismatch`).
- **Turning your own MFA off needs the second factor too**: besides the current password, a code from the authenticator app or a backup code. A wrong code is refused, audited as `user.reauth_failed` and counted toward the sign-in lockout like a wrong password.
- A sign-in with an MFA backup code works even when the account's TOTP secret can't be decrypted (the encryption key or salt changed); a TOTP code is then refused like a wrong one and audited as `user.mfa_secret_unreadable`. Before, both answered 500.
- An event a database migration has to audit waits in an outbox and is recorded by the next start as a new signed entry at the end of the chain (#252, #383). Only the events PerchTail's migrations write are accepted; anything else in the outbox is refused and audited as `audit.outbox_rejected`.
- Whether the audit chain has started is also recorded outside the database, in `DATA_DIR/.audit-chain-started` (#252, #383), written on the first start of this version and never removed by PerchTail. With it present, an audit log whose signatures were all taken away is reported as tampering (`chain_reset`) instead of being signed again as if the chain had never started. A restore puts it back when the backup has a chain (docs/backup-and-restore.md).
- Saving a source, updating credentials in bulk or importing YAML with a passphrase-protected SSH private key failed with a server error; it's now refused with 400 "Encrypted SSH private keys aren't supported; provide the key without a passphrase". A key the SSH connector can't load gives the usual credential error instead of a 500.
- A folder ZIP over a listing the source cut short (a push agent's listing limit) left files out without saying so. It's now refused before it starts with 409 "This folder has more entries than a ZIP can list; narrow it to a subfolder", and audited as `file.download_denied` with the reason `listing_truncated`.
- A folder ZIP left out, with only a log line, the files more than 32 folders down. It's now refused before it starts with 409 "This folder is nested too deeply for a ZIP; narrow it to a subfolder", and audited as `file.download_denied` with the reason `too_deep`.
- File content (open, Tail, download and folder ZIP) is sent with `Cache-Control: no-store`, so neither the browser nor a proxy keeps a copy.
- Alert webhook URLs, whose path or query often carries the receiver's secret, are encrypted at rest like the other secrets, never returned by the API, covered by `python -m app.rotate_credential_key`, and left out of the errors a blocked URL gives.
- `LOG_DIR` and `SCRATCH_DIR` are made `0700` at startup, like `DATA_DIR`, and scratch files are created `0600`.
- **Redaction checks the encoding on every line, not only at the start of a file.** Content that turns UTF-16 part way (a UTF-16 line in a plain text log), or plain ASCII text inside a UTF-16 file, is redacted from that line on as bytes and as both UTF-16 readings, so a value in it is found. Content that turns UTF-32 part way is refused from that line on: the viewer, Tail and a single-file download show `[redacted: unsupported encoding]` in place of the rest, the audit entry says so with where it started (`redaction_refused` and `redaction_refused_offset` in `file.open` and `file.follow`, a `file.download_redaction_skipped` entry once a download is sent), an archive member or a folder ZIP's file is left out and listed in `REDACTION-SKIPPED.txt`, and alerts don't read past it, as for a UTF-32 file. The same holds when a push agent redacts on its host. Binary content can look UTF-16 or UTF-32 to these checks: it is then redacted more, or refused, never sent less redacted (docs/configuration.md, "Redaction").
- The Docker image ships no test code (the backend's tests, the fake WinRM connector, the end-to-end admin seeder), and its entrypoint refuses the test hooks.
- **Confirming MFA only confirms a pending enrollment.** With MFA already on, `/auth/mfa/confirm` is refused, so a session alone can't issue new backup codes (they come only from regenerating them, with the password); a wrong code counts toward the sign-in lockout and is audited as `user.reauth_failed` with action `mfa_confirm`.
- **Requests to the identity provider can't reach internal addresses**: the discovery document, the `jwks_uri` named in it, the token endpoint and **Test connection** follow the alert webhooks' address policy, with the connection pinned to the checked address and no redirects. **Test connection** answers with a neutral message; the reason is in the log (`sso.test.failed`, `sso.test.discovery_incomplete`, `sso.test.issuer_mismatch`).
- **Starting an SSO sign-in is limited per client address** (20 a minute; over it, 429 with `Retry-After`, audited as `user.sso_login_failed` with reason `rate_limited`), and the identity provider's discovery document is reused for 10 minutes, until the provider is saved again.
- **ID tokens are checked more strictly**: only the asymmetric signature algorithms the provider announces (`id_token_signing_alg_values_supported`, or RS256 when it announces none) are accepted, never `none` or HS*, and a token with several audiences must name PerchTail's client ID in `azp`.
- **Passwords longer than the policy maximum are refused before they are checked** (422 `password_too_long`), at sign-in, password change, re-confirmation and reset by email, so a huge password costs no hashing.
- **Changing your own email to one another account has is refused with a neutral message**, not a 409 saying the address is used.
- **Changing your own password gives your session a new token**, besides ending the other sessions, and **turning MFA on or off ends your other sessions** (`session.revoke` with reason `mfa_enable` or `mfa_disable`).
- **A request's `x-request-id` is kept only when it is at most 64 letters, digits, `.`, `_` or `-`**; otherwise PerchTail gives the request a new one. The identity provider's `error` and `error_description` on the SSO callback are cut to 200 characters in the log.
- **Every Tail is audited**: `file.follow` is written once per Tail of a file in a session, whatever byte it starts from, with that byte in `offset`, and again when it reads from before that byte. Before, a Tail that didn't start at the beginning of the file and didn't say it was starting read the file without a record.
- A Tail poll takes a connector slot while it reads, like opening a file, so the concurrency limits in Settings → System cover it: past them it gets 503 with `Retry-After`.
- Closing a file in the viewer releases only the copy that session opened; another viewer's copy of the same file stays until that viewer closes it.
- An SSH fetch stops copying as soon as the file passes `MAX_FETCH_MB`, instead of transferring all of a file that grew after its size was checked.
- The access report's CSV also neutralises cells that start with a tab, a carriage return or a line feed, with spaces before `=`, `+`, `-` or `@`, or with their full-width forms (`＝`, `＋`, `－`, `＠`), which spreadsheets read as formulas too.
- Listing an SMB server's shares ends with an error when the server answers with a malformed fragment length or more than 1 MiB, instead of waiting forever.
- **Sources outside a role's reach answer like missing ones.** A source the user holds no capability on answers 404 "Source not found", as one that doesn't exist, and the refusal is audited as `access.denied` (`source_out_of_reach`); a source they reach without the capability needed is still a 403. Alerts, bulk credential updates and bulk agent upgrades answer a scope or source the user can't act on exactly like one that doesn't exist, and each refused item of a batch is audited as `access.denied`.
- The folder picker and **Test connection** no longer tell a closed port, a refused connection and a timeout apart: every connection that can't be made gets the same message, and the reason goes only to the server log. **Test connection** needs `manage_source` on the source (it needed `view`). The folder picker, given a saved source, needs `manage_source` on it whatever credential is typed, and always browses the saved source's host and port; it browses a typed host only for a new source.
- A grant must hold at least one capability: one holding none is refused with 422. One saved before no longer widens which customers and folders the role reaches.
- A delegated admin who isn't a super admin can no longer reset the password of, deactivate or change the role of an account whose role has `manage_users` or `manage_roles`; only a super admin can. The refusal is audited as `access.denied` (`peer_admin`).
- A source in a folder always belongs to that folder's customer: adding or moving one with another customer, or none, is refused with 422 (it was 400, and a missing customer was accepted). The upgrade gives sources saved before with another customer, or none, their folder's customer, names them in the migration log and records each one in the audit log as a `source.update` with no user, reason `folder_customer` and the customer before and after.
- A webhook call waiting to be sent or retried is dropped when its alert's owner can no longer view the source of its lines (or the source is gone), and that is audited as `alert.delivery_dropped`.
- Pasting rules as text and importing them from YAML have the same limits as the rule editor, which now has them too: at most 500 rules per source and 1000 characters per pattern. The audit entry of a pasted rule set keeps the rules before and after.
- An import preview no longer says whether a credential in the file or typed for an existing source is the one already saved: it always says **credential replaced**, and applying it replaces the saved one.
- A WinRM source's base path and the folder picker accept only drive paths: UNC paths (`\\server\share`) and device paths (`\\?\`, `\\.\`) are refused, as is a UNC path in the SMB picker. A WinRM source saved before with such a path is no longer read.
- Alert webhooks refuse IPv6 addresses that carry an IPv4 one for a translator or tunnel (NAT64 `64:ff9b::/96` and `64:ff9b:1::/48`, 6to4, Teredo, IPv4-translated and IPv4-compatible), whatever the embedded address. A webhook call that can't connect is reported as "connection failed", whatever the reason, and the reason goes to the server log. A user can send at most 5 webhook tests a minute (429 with `Retry-After` past that).
- A push agent connecting with the token of an agent already connected still replaces the older connection, which is usually the same agent reconnecting, but this is now audited as `agent.connection_replaced` with both addresses. When the addresses differ, the source's page warns those who manage it that two agents may share the token, until an agent next connects without replacing another connection.
- **The rules are checked on where a link leads.** On local, SSH and push-agent sources, a symlink (the file itself or a folder above it) inside the base path could show and serve a file the rules hide, under the link's own name. The rules now see both the path asked for and its real path, in listings, open, Tail, downloads, folder ZIPs, archives, the file-name index and alerts; a link out of the base path or to a hidden file or folder is left out of listings, and asking for it answers 404, audited as `file.open_denied`, `file.download_denied` or `access.denied` with the reason `link_target_hidden`. Without the source's rules, no link is followed. The push agent (`links/1`) reports where each path really is, symlinks and Windows junctions alike, and reads a file only once the server has checked its rules on that target, with the same open file, so a link swapped in between isn't followed; an 8.3 short name on a Windows agent reaches the rules as the file's real name. SMB and WinRM sources keep refusing every link.
- **Exclude rules match without regard to letter case on every protocol**, glob and `re:`, so a file name spelled in another case can't slip past an exclude on a filesystem PerchTail can't tell ignores case.
- **Severity indicators can't stall the viewer.** A severity regex is saved only in the same syntax as rule regexes (RE2: no backreferences or lookaround) and, like a rule pattern, at most 1000 characters. In the viewer, indicators are matched in a Web Worker, apart from the page, and only on the first 2000 characters of each line. One that takes more than a second in all over the lines in view, or over the whole file for the previous and next problem buttons, is turned off until the page is reloaded, with a warning naming it; when it is stuck in a single match, the worker is stopped and replaced, and the other indicators keep working.
- A `.tar.gz` is read in one pass when it is listed or a member is opened, instead of being decompressed again for each step, and without keeping every entry in memory: at most 50,000 entries and 8 MiB of names are read, and more than 1 MiB of headers for one entry (long names, pax headers and sparse blocks) or of global pax headers in the whole archive makes it unreadable, as do headers chained deeper than PerchTail can follow. A bigger archive lists its first entries and says the listing was cut short; a member past them isn't found, the whole archive is refused for download when the source has an exclude rule, and a redacted download of it is refused.
- In **Settings → SSO**, an admin with **Manage SSO settings** short of super admin saw the names of the roles beyond their own access that group mappings give, which the roles list already hides from them. Such a mapping now shows "A role outside your access", without the role's name (`role_name` is null and `role_outside_authority` true), and **Remove** is offered only on the mappings the server lets them remove.

### Fixed
- The viewer's count of redacted values counted `[redacted: unsupported encoding]` once Tail lines left the top of the tab; it now leaves it out, as the server's count does.
- Opening or downloading a zip member that is encrypted, or compressed with a method PerchTail can't read, answered 500. It's now refused with 409, with or without redaction, and audited as `file.open_denied` or `file.download_denied` with the reason `member_encrypted` or `member_unsupported_compression`. On a push agent that redacts on its host, only an encrypted member is refused this way (see below); one compressed with a method the agent can't read answers the source's generic error.
- A redacted folder ZIP over a listing the source cut short, or nested too deep, answered 500. It's now refused with 403 and audited as `file.download_denied` with the reason `listing_truncated` or `too_deep`.
- In the role editor, the **Target** list of a new grant now follows the scope type picked; it kept listing the targets of the first one.
- The release workflow's check that the CHANGELOG has a section for the version could fail at random once the CHANGELOG grew large; it now reads the whole file before looking.
- Dragging the corner of a text box no longer pushes it out of its card. The YAML import box can be made wider and taller, and its card grows with it; the other text boxes (rule paste mode, private keys, certificates) only grow downwards.
- Rotating `CREDENTIAL_ENCRYPTION_KEY` left users' MFA secrets under the old key, so MFA users could no longer sign in, and broke the audit log's hash chain, whose key comes from the same secret. The rotation now re-encrypts MFA secrets and re-signs the audit chain in the same transaction, after checking the chain under the old key (a broken chain stops the rotation), and records itself in the audit log (#326).
- An alert whose webhook failed lost those matches: it moved on and only logged a warning. A failed call is now retried with backoff for up to an hour, then counted on the alert with its error, shown on the Alerts page and in `/monitoring/health` (#328). While a call is retried, its matching lines (at most 50) wait in the database; they're dropped when it succeeds, after the hour, or as soon as the alert is turned off or its owner may no longer alert, and the stored error never includes the webhook URL.
- Long-interval background jobs (audit purge, audit integrity check, update check, expired-session purge) counted their interval from each start, so a container restarted more often than that never ran them. One that's overdue by its stored record now runs 30 seconds after startup (#327).
- Opening the bare address while signed in (`http://perchtail.example/` rather than `#/viewer`) showed an empty page under the menu bar. It now opens the Viewer.
- An SSH/SFTP source that stalled after login blocked a worker thread forever (no read timeout or keepalive), and a stalled SMB server held a request for about 20 minutes. Both now fail after `REMOTE_READ_TIMEOUT_SECONDS` (#343).
- Shutdown cut running background jobs and file-name index passes off mid-run; it now lets them finish within `SHUTDOWN_TIMEOUT_SECONDS` (#335).
- Scratch files left by a crash or a killed container were never deleted or counted; `SCRATCH_DIR` is now emptied at startup (refused if it holds the database or the logs) (#329).
- Each connected push agent kept a database connection until it disconnected, so with about 15 agents that had answered a command every other request waited 30 seconds and failed (#341).
- Escape on a new source or role popup, before its lists finished loading, asked to discard changes although nothing was typed (#315, a known issue in 0.6.0).
- A push agent's listing of a very large folder went over the 16 MiB WebSocket message limit and failed every time (#347).
- A folder ZIP over SSH with a symlink that loops back up logged in again for every level until the depth limit, about a thousand logins (#347).
- A download cut short by the client could keep its scratch file until the idle sweep: the release ran only when the response finished normally. It now runs however the response ends (#344).
- An integer above 9223372036854775807 in a request (an id in the path or body, a port) crashed with a 500; it is now refused with 422 and "A number in the request is out of range" (#376).
- Accessibility (#377): faint text, accent links and badges, and primary buttons now meet the WCAG AA contrast ratio; switches, selects and date fields in Settings, the user role selects and the log viewer's content have accessible names; the user menu and explorer file names now include their visible text (initials, file size) in their accessible names.
- After `CREDENTIAL_ENCRYPTION_KEY` or the credential salt was lost or changed outside the rotation tool, Settings → SSO failed to load. It now lists the provider, says "The saved client secret can't be decrypted with the current key; enter it again", and saves new settings with a new secret; an SSO sign-in fails with the usual message and is audited as `user.sso_login_failed` with the reason `secret_unreadable`.
- The system log source kept the `LOG_DIR` of the first start. It now follows a changed `LOG_DIR` on startup, audited as a `source.update` with no user and the old and new paths.
- A user with **View** but not **Manage rules** on a source saw its rules as editable, and saving failed. The rules are now read-only for them, with "You can't change this source's rules".
- The role editor said **Run now** wasn't used by any feature; it now says it starts **Index now** on the source's page. The Audit log page no longer points to a Settings → System option for the integrity check schedule, which runs daily (`AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS`).
- `python -m app.rotate_credential_key` ran while PerchTail was running on the same `DATA_DIR`, and as root. Like `python -m app.backup`, it now refuses both.
- A push agent started with a `PERCHTAIL_BASE_PATH` that didn't exist, wasn't a folder or couldn't be read connected anyway and failed every listing. It now stops at startup with a non-zero exit code and a message naming the problem.
- An image built from a Windows checkout with `core.autocrlf=true` didn't start (`exec /app/docker-entrypoint.sh: no such file or directory`): the entrypoint had CRLF line endings. A `.gitattributes` keeps shell scripts and the Dockerfile LF, and the image build strips carriage returns from the entrypoint.
- Opening or downloading a local file (the system log source or a local source) that grew while it was being sent cut the response off with a network error. The file is now sent as it was when the request started.
- At `LOG_LEVEL=INFO`, the SMB and SSH libraries logged every SMB message and every closed SSH session, a large share of the log. `smbprotocol`, `spnego` and `paramiko` now log only warnings and errors unless `LOG_LEVEL=DEBUG`.
- An upgrade interrupted while a migration rebuilt a table left SQLite's temporary table behind, and every later upgrade failed with `table _alembic_tmp_auditlog already exists`, so PerchTail didn't start. The migrations now recover from it and can be run again: the temporary table is dropped while the original exists, renamed back when the rebuild stopped after dropping the original, and the migration stops without changing anything when neither is there; docs/upgrade.md says to run an interrupted upgrade again, or restore the `pre-upgrade` backup.
- `python -m app.backup restore` didn't check the current `CREDENTIAL_ENCRYPTION_KEY` against alert webhook URLs, so a backup whose only encrypted data was webhook URLs was restored with a key that can't read them. Such a backup is now refused with the wrong key, unless `--force`.
- For a push agent older than 0.7, whose files can't be read until it is updated, `agent.redaction_fallback` was still logged, and the source editor's **Redaction** section said PerchTail redacts what the agent sends. The log line is now only for an agent with `links/1` that falls back to redaction on the server, and the source editor says the agent must be updated before its files can be read.
- Tail: every 409 from the server made the tab reload the whole file at each check, and **Tail the end** blamed "an agent older than 0.6". Now only a file that can't be followed from where Tail left off (`.gz` files and files inside archives) reloads whole; any other refusal shows the server's message, such as an agent to update or content redaction can't read, and Tail stops when the refusal won't change at the next check. A push-agent error that mentions an unknown command is no longer taken for an agent older than 0.6.
- The viewer offered **Try again** on every 409, including refusals that won't change on a retry: an agent to update, an encrypted or unreadable archive member, an archive or a compressed file that can't be read, and content redaction can't read. It now offers it only for the others, such as a file locked on the source.
- A `.tar.gz` listed only in part, because PerchTail reads at most 50,000 of its entries, said "Showing the first N entries" with the explorer's listing limit as N, a number it hadn't reached. It now says "This archive has more entries than were read; showing the N read."
- Expanding a `.zip` or `.tar.gz` that can't be read as one (damaged, or another kind of file), or opening or downloading a file inside it, answered 500. It's now refused with 409 "This archive can't be read: it is damaged, or it isn't really a .zip or .tar.gz file." (`archive_unreadable`), logged as `archive.unreadable`, and audited for a file inside it as `file.open_denied` or `file.download_denied` with the reason `archive_unreadable`.
- Settings → Severity indicators sent an indicator's pattern again with every change, so one saved before the RE2 and length checks (lookaround, a backreference, over 1000 characters) couldn't be switched off or given another level or switch. The page now sends the pattern only when it was edited.
- Settings → SSO said to give a new SSO account its role on the Users page, that removing a mapping stops its role sync, and that the users of a removed mapping keep their role. An SSO account no mapping matches drops to No Access at its next sign-in, and the page now says so.
- When an SSO sign-in couldn't start (the identity provider couldn't be reached or its address was refused, its issuer didn't match, or it wasn't `https://`), the browser was left on a JSON 502 "Could not reach the identity provider". It's now sent back to the sign-in page with "SSO sign-in failed", as when a sign-in fails after the identity provider; the log still has the reason (`sso.login.discovery_failed`, `sso.login.issuer_mismatch`, `sso.login.insecure_idp_url`).
- A YAML import accepted a push-agent source's `base_path` with control characters; it's now refused with "Base path contains control characters", as for the other protocols.
- The upgrade steps in **Settings → System → Updates** for an install from source ran `pip install` without `--require-hashes`, unlike docs/install-from-source.md and docs/upgrade.md; they now check the pinned hashes.
- The source editor saved a push-agent source's base path with control characters, which a YAML export then carried into an import that refused it. The editor now checks it as the import does: spaces around it are trimmed, and control characters are refused with "Base path contains control characters".
- A disabled source answered its 409 without a code, so the viewer offered **Try again** and Tail kept checking. It now answers with the code `source_disabled`: no **Try again**, and Tail stops.
- A folder a push agent listed only in part (at 50,000 entries) said "Showing the first N entries" with the explorer's listing limit as N. The server now says who cut a listing (`X-Listing-Cut-By`: `archive` or `source`), and the explorer says "The source listed only part of this folder; showing the N entries it listed." for such a folder, as it already did for an archive.
- On a push agent that redacts on its host, an archive that can't be read, when expanded or when a file inside it was opened or downloaded, answered 502 "The source returned an error" with **Try again**. The agent now says it can't read the archive (`archive_unreadable`) or decompress a `.gz` file (`gzip_unreadable`), and the server refuses them as when it reads them itself: 409 `archive_unreadable`, logged as `archive.unreadable` and audited, and a `.gz` file inside an archive with `redaction_unreadable`. This includes a `.tar.gz` that ends before its first whole file once decompressed (a few bytes, nothing at all, or only headers such as a long file name or a global header), which listed as empty, and a file of a `.zip` whose own header is damaged or cut short though the zip's index lists it. An encrypted file of a `.zip` is now refused as on the server too: 409 `archive_member_unreadable`, audited with the reason `member_encrypted` (the agent's code `member_encrypted`).
- Opening or downloading a `.gz` file that can't be decompressed (damaged, or not gzip at all) answered 500. It's now refused with 409 "This compressed file can't be read: it is damaged, or it isn't really a .gz file." (`archive_unreadable`), logged as `archive.unreadable` and audited with the reason `archive_unreadable`.
- The **Enabled** switch in Settings → SSO had no accessible name, so a screen reader announced an unnamed checkbox. It is now named "Enabled".
- When Tail stopped on a refusal that won't change, the message didn't say so. It now ends with "The tab keeps what it had; Tail stopped.", as when the file is gone.

## [0.6.4] - 2026-11-13

A security release, the code retested in the lab as 0.6.4-rc.1. Upgrade the server; push agents don't need to change. Check the Changed section first: admins who move folders may need manage rules on them.

### Security
- Moving a folder or a source can no longer widen what the person moving it can do. Moving or deleting a folder now needs manage rules on it and, to move it, where it goes; and moving a folder or a source that has no grant of its own for the role is refused when the role holds more where it would go than where it is now. Before, create source alone was enough to move a folder, with its sources, under a folder the role could reach, and manage rules in both places was enough to move a source or folder to where the role had view or download, so the role could read sources it couldn't see.

### Changed
- Moving or deleting a folder now needs manage rules on it (and, to move it, where it goes) as well as create source, and a folder or source with no grant of its own for the role can't be moved to where the role holds more than where it is.

## [0.6.4-rc.1] - 2026-11-13

A security release. Upgrade the server; push agents don't need to change. Check the Changed section first: admins who move folders may need manage rules on them.

### Security
- Moving a folder or a source can no longer widen what the person moving it can do. Moving or deleting a folder now needs manage rules on it and, to move it, where it goes; and moving a folder or a source that has no grant of its own for the role is refused when the role holds more where it would go than where it is now. Before, create source alone was enough to move a folder, with its sources, under a folder the role could reach, and manage rules in both places was enough to move a source or folder to where the role had view or download, so the role could read sources it couldn't see.

### Changed
- Moving or deleting a folder now needs manage rules on it (and, to move it, where it goes) as well as create source, and a folder or source with no grant of its own for the role can't be moved to where the role holds more than where it is.

## [0.6.3] - 2026-11-13

A security release, the code retested in the lab as 0.6.3-rc.1. Upgrade the server; push agents don't need to change. Check the Changed section first: delegated admins (create source, manage users, manage roles, manage SSO) may need manage rules on the sources they look after.

### Security
- Changing, moving or deleting an existing source, regenerating its push agent's token or browsing for a folder with its saved credential now needs manage rules on that source, and moving it also needs manage rules where it goes. Before, create source alone was enough to do all of this to any source, including ones the role couldn't see.
- A delegated admin (manage users, manage roles or manage SSO, without super admin) can no longer change their own role or edit or delete the role they hold, and can only assign, create, edit, duplicate or map to an SSO group a role whose grants they hold themselves: each capability in each customer, folder or source has to be theirs there too, and in any folder or source beneath it where they hold less. Before, only the global capabilities were compared, so a delegated admin could give themselves or others access to customers they couldn't see.
- Symbolic and hard links, devices and FIFOs inside a `.tar.gz` archive are no longer listed, and opening or downloading one answers "not found", like a member that doesn't exist. Before, a link with a visible name opened the member it pointed at, even one the source's rules hide. `.zip` archives were not affected.

### Changed
- Changing, moving, deleting an existing source, regenerating its agent token or picking folders with its saved credential now needs manage rules on that source (and, to move it, where it goes) as well as create source; give manage rules where people need it.
- A delegated admin can no longer change their own role, edit the role they hold, or grant capabilities in a customer, folder or source that they don't hold there themselves.
- A delegated admin can no longer reset the password of, deactivate or edit users whose role reaches beyond their own access.

## [0.6.3-rc.1] - 2026-11-13

A security release. Upgrade the server; push agents don't need to change. Check the Changed section first: delegated admins (create source, manage users, manage roles, manage SSO) may need manage rules on the sources they look after.

### Security
- Changing, moving or deleting an existing source, regenerating its push agent's token or browsing for a folder with its saved credential now needs manage rules on that source, and moving it also needs manage rules where it goes. Before, create source alone was enough to do all of this to any source, including ones the role couldn't see.
- A delegated admin (manage users, manage roles or manage SSO, without super admin) can no longer change their own role or edit or delete the role they hold, and can only assign, create, edit, duplicate or map to an SSO group a role whose grants they hold themselves: each capability in each customer, folder or source has to be theirs there too, and in any folder or source beneath it where they hold less. Before, only the global capabilities were compared, so a delegated admin could give themselves or others access to customers they couldn't see.
- Symbolic and hard links, devices and FIFOs inside a `.tar.gz` archive are no longer listed, and opening or downloading one answers "not found", like a member that doesn't exist. Before, a link with a visible name opened the member it pointed at, even one the source's rules hide. `.zip` archives were not affected.

### Changed
- Changing, moving, deleting an existing source, regenerating its agent token or picking folders with its saved credential now needs manage rules on that source (and, to move it, where it goes) as well as create source; give manage rules where people need it.
- A delegated admin can no longer change their own role, edit the role they hold, or grant capabilities in a customer, folder or source that they don't hold there themselves.
- A delegated admin can no longer reset the password of, deactivate or edit users whose role reaches beyond their own access.

## [0.6.2] - 2026-11-13

A security release, the code retested in the lab as 0.6.2-rc.1. Upgrade the server; push agents don't need to change.

### Security
- Downloading or opening a whole `.zip` or `.tar.gz` archive, or the ZIP of a folder that contains one, now follows the source's rules for the files inside the archive. An archive with a file whose last matching rule is an exclude can't be downloaded or opened whole (its other files can still be opened and downloaded one by one), and a folder's ZIP leaves it out and lists it in `SKIPPED.txt`. Each refused archive is recorded in the audit log.
- Regenerating a push agent's token, or disabling or deleting its source, now closes the agent's open connection. Before, an agent that had connected with the old token kept answering for the source until it disconnected on its own, so regenerating a leaked token didn't cut off whoever was using it.

### Fixed
- .zip, .tar.gz and .gz files are recognised whatever the case of their extension (OLD.ZIP, app.log.GZ).

## [0.6.2-rc.1] - 2026-11-13

A security release. Upgrade the server; push agents don't need to change.

### Security
- Downloading or opening a whole `.zip` or `.tar.gz` archive, or the ZIP of a folder that contains one, now follows the source's rules for the files inside the archive. An archive with a file whose last matching rule is an exclude can't be downloaded or opened whole (its other files can still be opened and downloaded one by one), and a folder's ZIP leaves it out and lists it in `SKIPPED.txt`. Each refused archive is recorded in the audit log.
- Regenerating a push agent's token, or disabling or deleting its source, now closes the agent's open connection. Before, an agent that had connected with the old token kept answering for the source until it disconnected on its own, so regenerating a leaked token didn't cut off whoever was using it.

### Fixed
- .zip, .tar.gz and .gz files are recognised whatever the case of their extension (OLD.ZIP, app.log.GZ).

## [0.6.1] - 2026-11-12

A security release, the code retested in the lab as 0.6.1-rc.1. 0.6.0's push-agent binaries were built with Go 1.24, which no longer receives security fixes, and govulncheck finds 43 known vulnerabilities in the Go standard library they contain. Nothing else in the server or the agent changes. Upgrade the server and then upgrade each push agent to 0.6.1 by hand (download the binary for its platform from the release and replace the old one); from 0.7.0 on, agents can be upgraded from the portal.

### Security
- The push agent is built with Go 1.27, at its latest patch release, instead of Go 1.24. The vulnerability scan checks the same Go release the binaries are built with.
- The image is rebuilt on current base images and OS packages. The base images in the `Dockerfile` are pinned by digest, and Dependabot proposes their updates like any other dependency.
- A daily scan checks the backend's Python packages, the frontend's npm packages and the agent's Go modules on `main` and `dev`, the latest release's agent binaries, and the published image with its OS packages, so a new advisory is caught even when no code changes. SECURITY.md now states how fast fixes ship and what that covers.

## [0.6.1-rc.1] - 2026-11-12

A security release: 0.6.0's push-agent binaries were built with Go 1.24, which no longer receives security fixes, and govulncheck finds 43 known vulnerabilities in the Go standard library they contain. Nothing else in the server or the agent changes. Upgrade the server and then upgrade each push agent to 0.6.1 by hand (download the binary for its platform from the release and replace the old one); from 0.7.0 on, agents can be upgraded from the portal.

### Security
- The push agent is built with Go 1.27, at its latest patch release, instead of Go 1.24. The vulnerability scan checks the same Go release the binaries are built with.
- The image is rebuilt on current base images and OS packages. The base images in the `Dockerfile` are pinned by digest, and Dependabot proposes their updates like any other dependency.
- A daily scan checks the backend's Python packages, the frontend's npm packages and the agent's Go modules on `main` and `dev`, the latest release's agent binaries, and the published image with its OS packages, so a new advisory is caught even when no code changes. SECURITY.md now states how fast fixes ship and what that covers.

## [0.6.0] - 2026-11-07

The release after 0.5.3. It carries everything from the pre-releases
0.6.0-rc.1, rc.2 and rc.3, whose sections in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.6.0/CHANGELOG.md)
list every change; the code is the one retested in the lab as rc.3, on
Linux, Windows (SMB, WinRM, JEA) and push agent sources.

Highlights:
- **Viewer:** Tail fetches only the new lines (like `tail -f`), at an interval set under Settings → System. One find bar for Find and Find all, with Match case, Whole word, Regex and "3 of 120"; Find all lists every matching line, with no limit, even in logs of 100 MB. A quieter toolbar.
- **Explorer:** each file shows its size (the time on hover); open folders refresh themselves; a status dot on each source says whether it answers; an advanced filter searches file names across ticked sources, folders of sources and whole customers, recursively, as text, glob or regex.
- **WinRM without a remote shell:** a read-only JEA endpoint (`docs/examples/jea`) that lists and reads only the allowed folders, as an account with no administrator rights.
- **Update check:** super admins see the running and the latest version, with the upgrade steps, under Settings → System → Updates.
- **Accounts:** a password policy (NIST SP 800-63B defaults); My profile with name, email, password, two-factor authentication and sessions; a user menu in the top bar.
- **Settings:** row actions are buttons; creating, editing and deleting open in popups, the source and role editors included.

### Upgrading
- **Back up `DATA_DIR` first**, with PerchTail stopped. Six migrations
  run on start; they only add columns and tables (update check state,
  agent version, password history, display name and email, user
  preferences, source status, the WinRM JEA endpoint).
- **Upgrade the push agents** to get incremental Tail, file times and
  the agent version in the source overview. Older agents keep working:
  Tail reloads the whole file, and their files show no time.
- **The update check makes one outbound request a day** to the GitHub
  releases API (nothing about the installation is sent). Set
  `UPDATE_CHECK=false` to turn it off entirely, for example on an
  air-gapped install.
- **A background check connects to every source every 10 minutes** (one
  connection, not audited) to show its status. Change the interval or
  turn it off (0) under Settings → System.
- From source: reinstall `backend/requirements.txt`, which adds `pypsrp`
  for JEA endpoints.
- Settings → Security and Settings → Sessions moved to **My profile**, in
  the user menu at the top right; the old links lead there.
- New under Settings → System: password policy (NIST defaults, stricter
  rules off), Tail interval (2 s), open-folder refresh (30 s), default
  file order, source check interval, update check.

### Deprecated
- **Content indexing, the Search page and the current Alerts go away in
  0.7** (#303). The content index is the only place PerchTail keeps text
  from the logs, against its promise that nothing from the sources is
  kept. In 0.7 the upgrade deletes the indexed text; searching inside a
  file stays (Find and Find all), file names get an index of their own,
  on by default and with no content (#304), and Alerts watch new lines
  live instead of the index (#305). PostgreSQL support comes in 0.7 too
  (#306).

### Known issues
- Pressing Escape on a new, untouched source or role popup while its
  lists are still loading asks to discard changes; choose Discard, or
  wait a moment. Fixed in 0.7 (#315).

## [0.6.0-rc.3] - 2026-11-07

Third release candidate for 0.6.0, from the rc.2 lab review. Upgrade
notes, from 0.6.0-rc.2: no migrations, no new settings, and push agents
don't need upgrading. From earlier versions, follow the rc.2 and rc.1
notes below as well.

### Changed
- **Advanced filter: no limit on folders or time** (#284). A ticked
  source or folder is searched to the end, still a few listings at a
  time per source, with Stop. When it's done it says how many folders it
  searched; it says so as well when it was stopped, when folders couldn't
  be listed, and when it didn't go below 32 levels (a guard against links
  that loop back to a folder above). The refresh of the results no longer
  stops at 50 folders.
- **A user menu in the top bar** (#298): your initials, name and an arrow,
  opening My profile and Log out (the separate Log out button goes). It
  works from the keyboard too.
- **The explorer shows each file's size** ("3.5 MB") instead of the
  time it was last modified; hovering a file shows the exact size and
  the time. Also in the advanced filter's results.
- **One find bar for Find and Find all** (#299), above the editor:
  text, Match case, Whole word and Regex, previous/next with "3 of 120"
  (every match counted, in the background), and Find all, which lists
  the matching lines in the panel below. Enter / Shift+Enter move between
  matches, Alt+Enter runs Find all, Escape closes the bar, Ctrl/Cmd+F
  opens it with the selected text. The toolbar keeps one Find button.
- **Settings: buttons and popups** (#301). Row actions in the settings
  tables are buttons; creating and editing users, folders and SSO group
  mappings opens a popup; deleting, revoking, deactivating and resetting
  a password ask in a popup instead of the browser's confirmation. A
  password reset shows the temporary password in a popup with Copy.
- **Source and role editors in a popup** (#302) over the list (or the
  source's own page) they were opened from, keeping their address, so
  links, back and reload still work. Closing with unsaved changes asks
  first.

### Deprecated
- **Content indexing, the Search page and the current Alerts go away in
  0.7** (#303). The content index is the only place PerchTail keeps text
  from the logs, against its promise that nothing from the sources is
  kept. In 0.7 the upgrade deletes the indexed text; searching inside a
  file stays (Find and Find all), file names get an index of their own,
  on by default and with no content (#304), and Alerts watch new lines
  live instead of the index (#305). PostgreSQL support comes in 0.7 too
  (#306).

### Fixed
- The Tail notice ("N new lines — jump to end") stays in the log's
  corner instead of covering the Find all results (#298).
- **Find all: no limit, and no missing rows.** Results stopped at 5,000
  matches, and a long list of long lines (debug logs) drew blank or
  missing rows while scrolling. Every matching line is now listed; only
  the rows in view are drawn, each showing at most 10,000 characters of
  its line (cut around the first match, marked with "…"). Copy and go to
  line still use the whole line. A search for a common letter in a
  100 MB log no longer runs the tab out of memory.

## [0.6.0-rc.2] - 2026-11-07

Second release candidate for 0.6.0, with the changes from the rc.1 lab
review. Upgrade notes, from 0.6.0-rc.1:
- No migrations.
- New System setting **Tail interval** (Settings → System): 1, 2, 5 or
  10 seconds, 2 by default. The interval each browser remembered in rc.1
  is no longer used.
- The per-user file order chosen in rc.1 is no longer used; the order
  set under Settings → System applies to everyone.
- Push agents don't need upgrading from rc.1.

From 0.5.3, follow the 0.6.0-rc.1 notes below as well.

### Changed
- **Follow is now called Tail** (#283). How often it checks for new
  lines is a System setting (Settings → System → Tail interval: 1, 2, 5
  or 10 seconds, 2 by default) instead of each user's choice, so sources
  see a known load; the viewer shows it next to the button while Tail is
  on. 30 seconds is no longer offered.
- **No sort control in the explorer** (#282): folders are listed in the
  order set under Settings → System. The per-user choice made in
  0.6.0-rc.1 is kept but not used for now; it comes back as a "Sort by"
  action on folders.
- **Advanced filter searches subfolders and whole customers, with regex**
  (#284). Ticking a source or folder now searches the folders below it
  right away (up to 6 levels, 300 folders or 90 seconds per source, two
  listings at a time per source and six overall), with progress and a
  Stop button. In the Tree view, customers and folders of sources can be
  ticked too: every source under them is searched. Regex and Match case
  options, as in Find all; a pattern with `/` matches the path below the
  ticked folder (`app/*.log`, `**/2026-11/*.gz`). An invalid regex says
  so. Results refresh by listing again the ticked folder and the folders
  that had files, not every folder searched.
- **Quieter viewer toolbar** (#286): buttons without borders, grouped
  (find, view, Tail) with thin dividers; only what's on gets a fill, and
  Tail shows a green dot while it runs. Copy lines, bookmarks, problems,
  Reload and Download are icons on the right, with tooltips.
- **Find all can be resized** (#285): drag the line on top of the panel,
  or focus it and use the arrow keys. This browser remembers the height.

### Fixed
- **Files that disappear, on Windows** (#272). Over WinRM (without a JEA
  endpoint), a file that was gone opened as an empty file, and Tail
  on it said "The source sent an unexpected answer"; a Windows push agent
  answered "The source returned an error". Both now say the file is no
  longer on the source, as SSH, SMB and Linux agents do.

## [0.6.0-rc.1] - 2026-11-07

First release candidate for 0.6.0. Upgrade notes:
- **Back up `DATA_DIR` first**, as for every release. Six migrations run
  on start; they only add columns and tables (update check state, agent
  version, password history, display name and email, user preferences,
  source status, the WinRM JEA endpoint).
- **Upgrade the push agents** to get incremental Follow, file times and
  the agent version in the source overview. Older agents keep working:
  Follow reloads the whole file, and their files show no time.
- **The update check makes one outbound request a day** to the GitHub
  releases API (nothing about the installation is sent). Set
  `UPDATE_CHECK=false` to turn it off entirely, for example on an
  air-gapped install.
- **A background check now connects to every source every 10 minutes**
  (one connection, not audited) to show its status. Change the interval
  or turn it off (0) under Settings → System.
- From source: reinstall `backend/requirements.txt`, which adds `pypsrp`
  for JEA endpoints.
- Settings → Security and Settings → Sessions moved to **My profile**;
  the old links lead there.
- New under Settings → System: password policy (NIST defaults, stricter
  rules off), open-folder refresh (30 s), default file order, source
  check interval, update check.

### Added
- **Update check** (#215). Once a day PerchTail asks the GitHub releases
  API for the latest release, and super admins see under
  **Settings → System → Updates** the running version, the latest one,
  a link to its release notes and the upgrade steps for a Docker or a
  from-source install. A "Check now" button checks on demand. On by
  default: turn it off with the switch, or with `UPDATE_CHECK=false`,
  which wins over the switch and means no outbound request at all. Only
  full releases are offered, never release candidates, and nothing about
  the installation is sent.
- **Push agent version.** Agents from this release report their version
  when they connect, and the source overview shows it, flagging an agent
  older than the server. Older agents show as "not reported yet".
- **Password policy** (#245), under **Settings → System**, for local
  accounts. Checked whenever a password is set: a user's own change, the
  change at first login, temporary passwords an admin picks, and resets
  (whose random password now meets the policy too). The rules show next
  to the password fields, and an error names the rule that failed. The
  defaults follow NIST SP 800-63B: 12 to 128 characters, no passwords
  that are too simple (repeats, sequences, keyboard runs, the username
  with small changes) and none containing the username or name.
  Optional, off by default: character classes, not reusing the last N
  passwords (only previous argon2id hashes are kept, and only while the
  rule is on) and expiry after N days, which makes the user change it at
  the next sign-in. Existing passwords are checked at their next change;
  with expiry on, they count from the upgrade.
- **My profile** (#243), opened by clicking your name in the top bar: your
  display name and email, password change, two-factor authentication and
  sessions on one page. Settings → Security and Settings → Sessions moved
  there (their old links still lead to it). The email is checked for
  format and must be unique; changing it is audited
  (`user.email_change`). SSO accounts get their name and email from the
  IdP's claims on each sign-in, read-only, and have no password section.
  Admins see name and email in the Users list, can set them when
  creating a user and edit them for local accounts. The password policy
  also refuses passwords containing the name or email.
- **Explorer: open folders refresh themselves** (#272). The folders you
  have open are listed again every 30 seconds (Settings → System, 0 to
  turn it off): only the list of files, never their content, one folder
  at a time per source, not while the browser tab is hidden, and less
  often for a source that just failed. New files are highlighted for a
  moment; files that are gone disappear; open subfolders stay open. A ⟳
  button on each open source and folder lists it right away, and
  re-opening a folder lists it again.
- **Files that disappear** (#272). Opening a file that's no longer on the
  source says so (no longer "wasn't found, or is hidden by the rules")
  and drops it from its folder. A file you're following that disappears
  stops Follow; its tab stays open with what it had, marked, with a note.
- **Explorer: file times and order** (#273). Each file shows when it was
  last modified, and a sort button orders folders by name (A→Z, Z→A),
  newest or oldest first, or size; folders come first. The default is
  set under Settings → System; each user's own choice is kept on the
  server and follows them, as does the list/tree choice for connections
  (a choice this browser remembered moves there on first use). Push
  agents older than this release don't report times: their files show
  none and sort last by time.
- **Source status** (#276). A dot before each source in the explorer
  says whether it answers: green reachable, red failing (with the reason
  and how long ago), amber for a disconnected push agent, grey not
  checked yet. The status is kept on the server and shared by everyone:
  a background check runs every 10 minutes (Settings → System, 0 = off,
  one connection per source, not audited), and every real operation
  (browse, open, Follow, search indexing) updates it too; an error about
  one file doesn't count. "Check now" on the source re-checks it, and the
  Sources list shows the same status. When PerchTail itself stops
  answering, a banner says so and retries until it's back.
- **Advanced filter in the explorer** (#274). An "Advanced filter" button
  next to the search box: tick sources and folders, on any servers, and
  the box then filters their files by name (shown with "Show the files"
  or as soon as you type), as text or as a glob
  (`app-2026-11*`, `*.log`). Each ticked one shows its matching files,
  in your sort order and with their times, and opens them on a click;
  chips list the selection, with "Clear all". It filters what's listed
  in those folders; "Search subfolders too" lists the folders below on
  request (up to 3 levels and 50 folders per selection). The selection
  lasts while the app is open, like the tabs.
- **WinRM without a remote shell: a read-only JEA endpoint** (#169). A
  WinRM source can name a PowerShell endpoint in **JEA endpoint**;
  PerchTail then speaks PSRP to it and only calls the functions of
  PerchTail's reference role (`docs/examples/jea`), by name and with
  typed parameters. The role runs in `NoLanguage` mode, lists and reads
  only under the folders its administrator allows, refuses junctions and
  links, and runs as the connecting account, so that account needs no
  administrator rights, no Remote Management Users membership and no
  `RootSDDL` change. `Register-PerchTailJea.ps1` installs it. Sources
  without an endpoint keep the WinRS shell as before.

### Changed
- **Follow fetches only the new lines** (#254). Instead of re-fetching
  the whole file every 2 seconds, Follow asks the source for what was
  appended after the bytes the viewer holds (SFTP and SMB seek, WinRM
  reads a range, the push agent has a new `read_range` command), and
  appends it to the editor, which keeps your place and selection. While
  you're at the end it scrolls with the new lines; scrolled up, a
  "N new lines — jump to end" button appears instead. A rotated or
  truncated file is detected (it shrank, or starts differently) and
  reloaded from the start, with a note. The interval can be set from
  1 to 30 seconds, and Follow pauses while the browser tab is hidden.
  Every poll still reads fresh from the source; following is audited
  once (`file.follow`), not on every poll. `.gz` files, archive members
  and push agents older than this release still reload the whole file.

## [0.5.3] - 2026-11-06

The release after 0.5.2. It carries everything from the pre-release
0.5.3-rc.1, whose section in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.5.3/CHANGELOG.md)
lists every change. Since the rc, only the push agent changed: its read
buffer is now a fixed 1 MiB, so the chunk size PerchTail asks for can't
make it allocate more. The rc was retested in the lab on Linux and
Windows sources, including an audit log with old race breaks.

Highlights:
- **Audit log:** if "Verify now" reported "Tampering detected" because of the race fixed in 0.5.2, it now shows those breaks apart, and a super admin can mark them as known (Settings → Audit log). Any other break is still tampering. Entries now name the source, user or object they're about, and keep that name after a rename or a delete.
- **Push agent:** files come in 1 MiB chunks, written as they arrive, so large logs open without the agent holding them in memory. A file over `MAX_FETCH_MB` is refused before it's read. The agent answers commands in parallel, so a long download doesn't hold up browsing, and stops reading when PerchTail gives up on a file.
- **Search indexing:** each source gets a time budget per sweep, so one large source can't hold up the others; files that can't be indexed are retried only when their size changes.
- **Settings:** the Sources and Users lists sort by any column and filter.
- **Viewer:** "⌕ Find" is a button that opens the search panel.

### Upgrading
- **Back up `DATA_DIR` first**, with PerchTail stopped.
- One migration runs on start (`audit known breaks`). It records the newest audit row at the time of the upgrade, which is how 0.5.3 tells the old race breaks apart (see above).
- **Upgrade the push agents.** Agents older than 0.5.3 keep working with this version, but send each file in one message, which fails above about 12 MiB.
- Optional new setting: `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default 120).

## [0.5.3-rc.1] - 2026-11-05

First release candidate for 0.5.3. Upgrade notes:
- **Back up `DATA_DIR` first**, as for every release. One migration
  runs on start (`audit known breaks`): it adds columns to the audit
  chain state and records the newest audit row at the time of the
  upgrade, which is how 0.5.3 tells old race breaks apart (see below).
- **Upgrade the push agents** to get chunked fetches, parallel commands
  and cancel. Agents older than 0.5.3 keep working with this backend, but
  send whole files in one message, which fails above about 12 MiB.
- Optional new setting: `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default 120).
- If "Verify now" reports "Tampering detected" because of the race fixed
  in 0.5.2, it now shows those breaks apart, and a super admin can mark
  them as known (see the 0.5.2 known issue).

### Added
- Audit log: entries name what they're about (#239). Each event now
  records the target's name and where it sits (a source's "customer /
  folder", a rule's source, a grant's role), and the names next to ids it
  already held (`source_name`, `role_name`, `customer_name`,
  `scope_name`), so an entry still makes sense after the object is
  renamed or deleted. The table shows a readable Target column, linked
  when the object still exists, and the details as key/value pairs.
  Entries from before this release can't be changed (the hash chain
  covers them), so they show the object's current name, marked as such.
- Audit log: breaks in the hash chain left by the race of concurrent writes before 0.5.2 (#213) are no longer reported as tampering forever (#217). "Verify now" reports them apart, as "N breaks match the #213 race", when each row is valid under the chain's key, chains from an existing earlier row, and was written before the upgrade to 0.5.3. A super admin can "Mark as known": this writes a chained `audit.chain.known_breaks_recorded` entry listing those rows and the fork's other rows, and verification then reports the chain as intact, with N known breaks. Any other break is still tampering, and so is a recorded row that later goes missing or changes.

### Fixed
- Push agent: files are fetched in 1 MiB chunks, written to the scratch
  file as they arrive (#205). Before, the agent read the whole file and
  sent it base64-encoded in one WebSocket message: a ~100 MB log took
  hundreds of MB of agent memory, and anything over about 12 MiB was
  larger than the backend's 16 MiB message limit, so the open failed and
  the agent's connection dropped. A file over `MAX_FETCH_MB` is now
  refused by the agent before it reads anything (HTTP 413, as for the
  other connectors). The fetch only times out when no chunk arrives for
  30 seconds, not after 30 seconds in all. **Upgrade the agents** to get
  this: an agent older than 0.5.3 still works with this backend, but
  sends whole files as before, and a 0.5.3 agent works with an older
  backend the old way. The agent also answers commands in parallel, so a
  long download no longer holds up browsing on the same agent, and stops
  reading a file when the backend gives up on it.
- Search indexing: one large source no longer holds the whole sweep (#218).
  A first index of a source with thousands of files took minutes, and
  meanwhile every other source's index, and the alerts that ride on it,
  went stale. Each source now gets at most
  `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default 120) of reading per sweep
  and carries on where it left off in the next one. New files are read
  first, then the least recently indexed, so a source with many growing
  logs still works through all of them. Files that can't be indexed
  (binary, unreadable) are retried only when their size changes.
- Viewer: "⌕ Find" in the toolbar is now a button that opens the search
  panel. It looked clickable before but was only a hint for Ctrl/Cmd+F
  (#218).

### Changed
- Settings → Sources and Settings → Users can be sorted by any column,
  both ways, and filtered (#238): by text (name, host, customer or folder;
  username or role) and by protocol and status, or role, status and
  sign-in method. "N of M" shows while a filter is on. The sort and
  filters are kept while you open a row and come back. The Sources list
  gains a Customer column and shows each source's host; the Users list
  shows the sign-in method and the last login.
- Dependencies: docker/setup-qemu-action and setup-buildx-action 4,
  docker/metadata-action 6, docker/build-push-action 7, docker/login-action
  4, actions/upload-artifact 7 and download-artifact 8 (all actions in the
  CI and Release workflows now run on Node 24), @codemirror/commands
  6.11.1, @codemirror/autocomplete 6.20.4, @sveltejs/vite-plugin-svelte
  7.3.1, @playwright/test 1.64.1 and black 26.10.1.
- Dependabot no longer proposes sqlmodel 0.0.45 or later: those versions
  reject the naive datetimes PerchTail stores, so `requirements.in` keeps
  it below 0.0.45.

## [0.5.2] - 2026-11-05

The release after 0.5.0. There is no 0.5.1: its tag was used by mistake
for a pre-release of older code, and releases here are immutable. This
release carries everything from the pre-releases 0.5.1-rc.1 and
0.5.2-rc.2 (0.5.2-rc.1 was tagged but never released). Their sections in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.5.2/CHANGELOG.md)
list every change. Both were retested in the lab on Linux and Windows
sources.

Highlights:
- **Security:** SMB and WinRM no longer follow junctions or symlinks out of the base path; SMB sessions are no longer shared between sources; changing a source's host or port requires its credential again; rules for Windows agents ignore case; `DATA_DIR` is readable by PerchTail's user only.
- **Search indexing** no longer locks the database, and skips a file it can't read instead of failing the whole source.
- **Audit log:** two requests writing at the same moment can no longer break the hash chain.
- **Push agent:** stops at once on SIGTERM, and leaves out links that lead outside its folder.
- **Releases** are built as a draft with the agent binaries, `SHA256SUMS` and an SBOM attached, then published.

### Upgrading
- **Back up `DATA_DIR` first**, with PerchTail stopped: copy the whole directory.
- A migration adds `source.agent_os`; it runs on start.
- The database switches to SQLite's WAL mode: `perchtail.db-wal` and `perchtail.db-shm` appear next to it. Keep `DATA_DIR` on a local disk, not a network share.
- **Upgrade the push agents** too: Windows agents need this version for their rules to ignore case, and all agents get the SIGTERM and link fixes.
- Installed from source? `/opt/perchtail` was owned by your build account; run `sudo chown -R root:root /opt/perchtail` once (see `docs/install-from-source.md`).

### Known issues
**Audit chain breaks from before this release.** Before 0.5.2, two requests writing audit entries at the same moment could both chain from the same previous entry (#213). An install where that happened keeps showing "Tampering detected" in Settings → Audit log → "Verify now", at the first such row. A break of this kind has a recognisable signature in the database (`auditlog` table): the reported row and the row before it have the **same `prev_hash`**, timestamps **within the same second**, and are dated **before your upgrade to 0.5.2**. Treat any other break, or one dated after the upgrade, as tampering. 0.5.3 will let a super admin record these breaks as known (#217).

### Fixed
Since 0.5.2-rc.2:
- Installing from source left `/opt/perchtail` owned by the account that
  built it, not root, so that account could change the code the service
  runs. Both `rsync` commands in `docs/install-from-source.md` now pass
  `--chown=root:root`, and the guide shows how to check and how to fix an
  existing install.
- A release's target commit is the tagged one; before, GitHub recorded the
  default branch for a tag on `dev`.

## [0.5.2-rc.2] - 2026-11-04

Second release candidate for 0.5.2. `v0.5.2-rc.1` was tagged but never
released: its Release workflow run failed before creating the draft, and
the e2e tests then found the audit log race below. Everything listed
under 0.5.2-rc.1 is in this release too; the upgrade notes there apply
(back up `DATA_DIR`, upgrade push agents).

### Fixed
- The audit log could report "Tampering detected" when two requests wrote
  audit entries at the same moment: both could chain from the same
  previous entry. Each writer now takes the database's write lock before
  reading the last entry, so the second waits for the first. More likely
  since the database moved to WAL mode in 0.5.2-rc.1.
- The Release workflow creates the SBOM's directory before writing it,
  and the draft job downloads only the agent binaries and the SBOM, not
  the build record that `docker/build-push-action` uploads.

## [0.5.2-rc.1] - 2026-11-04

Release candidate for 0.5.2, from the lab retest of 0.5.1-rc.1. There is
no 0.5.1 release: the `v0.5.1` tag was used by mistake for an earlier
pre-release of older code, and releases here are immutable, so 0.5.1-rc.1
is followed by 0.5.2. This is also the first release built by the new
draft-first Release workflow. Back up `DATA_DIR` before upgrading, and
upgrade push agents too (SIGTERM, link handling).

### Fixed
- Releases get their assets again. Releases in this repository are
  immutable, and the Release workflow attached the agent binaries,
  `SHA256SUMS` and SBOM after publication, which an immutable release
  refuses, so `v0.5.1-rc.1` has none. The workflow now builds everything
  into a **draft** release, from a tag push or a manual run, and a
  maintainer publishes it; `X.Y` and `latest` move when a full release is
  published. It also refuses a tag whose code carries another version
  (see CONTRIBUTING.md, "Releasing").
- WinRM TLS errors are decided by OpenSSL's verify code. A certificate
  issued for another IP address ("IP address mismatch") is now reported
  as a name mismatch, not as an unknown issuer.
- Search indexing skips a file it can't read (no permission, rotated away
  since the listing, grown past the limit) and indexes the rest. Before,
  one such file failed the whole source's sweep, every sweep.
- The push agent exits right away on SIGTERM. A connected agent used to
  wait for the next command, so `systemctl stop` took 90 s and ended in
  SIGKILL.
- The push agent leaves out links that lead outside its folder (or are
  broken), as SSH and local sources do, and browsing a file answers 404
  instead of 502.
- "Test connection" and browsing a source with no credential say so
  instead of showing a Python error.
- WinRM logs `winrm.path_outside_base` when a later block of a read is
  refused, not only the first check.
- Import points a port-only change without a credential at `.port`.
- The source editor shows a save error next to the Save button.
- The SSH key field on the import page wraps in narrow windows.
- System settings say that open sessions pick up a Connections view
  change within 30 seconds.
- The source page writes agent OS names properly ("Linux", "Windows").
- `DATA_DIR/winrm_ca` is `0700`, like the rest of `DATA_DIR`.

### Changed
- Dependencies: actions/checkout, setup-go, setup-node and setup-python 7,
  trivy-action 0.36.0, @codemirror/language 6.13.1, @codemirror/merge
  6.12.3, svelte 5.57.3, svelte-check 4.7.8, ruff 0.17.3 and pytest 9.2.0.
- CI installs the e2e SSH/SMB test servers in their own step, before
  Playwright starts.
- `docs/install-from-source.md` installs the latest release tag rather than a fixed one, builds as an ordinary user and installs with `sudo`, lists `python3.12-venv`, gets Node.js 22 from nodejs.org with a checksum check, and keeps settings in `/etc/perchtail/perchtail.env`.

## [0.5.1-rc.1] - 2026-11-04

Release candidate for 0.5.1: fixes for what the Hyper-V lab test of 0.5.0
found, for the lab to retest before the final release. Back up `DATA_DIR`
before upgrading; the database switches to WAL mode on first start (see
"Fixed"). Upgrade Windows push agents so their rules ignore case.

### Security
- SMB and WinRM sources no longer follow junctions or symlinks out of the
  base path. Before, anyone able to write to the log folder on the Windows
  host could create a junction (no special privilege needed) and expose
  any folder the service account can read to users with `view` on the
  source. Junctions and symlinks are now left out of listings, and a path
  through one answers 404 (`smb.path_outside_base` /
  `winrm.path_outside_base`). WinRM re-checks with every block it reads.
- SMB sessions are no longer shared between sources. smbclient's
  process-wide pool is keyed by host and username, so a source with the
  wrong password could read with another source's authenticated session,
  and a passing connection test didn't prove its credential was right.
  Every SMB request now authenticates with its own connection, which is
  closed afterwards.
- Changing a source's host or port now requires entering the credential
  again (in the editor, the API and bulk import), as the folder picker
  already did. Before, editing could send the saved credential (for SMB
  and WinRM, an NTLM exchange) to any new address.
- Rules for an agent running on Windows are now matched case-insensitively,
  as for SMB and WinRM sources. Before, `exclude secret/**` didn't hide a
  folder named `SECRET` on disk, though NTFS treats both as the same
  folder. The agent now reports its OS when it connects (shown as **Agent
  OS** on the source's page). Upgrade Windows agents to get this: older
  agents don't report it and keep case-sensitive rules.
- `DATA_DIR` is now readable by PerchTail's user only: on startup the
  directory is set to `0700`, and the database (with its WAL/SHM files),
  the credential salt and `known_hosts` to `0600`. Earlier versions left
  them `0644`, readable by any local user on a host install.

### Fixed
- SMB: a directory symlink in a folder no longer makes the whole listing
  fail with `SMBLinkRedirectionError`.
- WinRM: browsing a path that is a file answers 404 instead of listing the
  file under a doubled path.
- Search indexing no longer locks the database. The first index of a
  large source kept one write transaction open while it fetched every
  file, and for those minutes logins and anything else that writes failed
  with `database is locked` (HTTP 500). Each file is now fetched first and
  committed on its own, the database uses SQLite's WAL mode so reads don't
  wait for writes, and a write waits up to 30 s for another to finish
  instead of failing. Indexing also stops when the source is disabled or
  taken out of search mid-sweep.
- Scheduled jobs that start late (a busy worker) now run instead of being
  skipped, and runs that piled up collapse into one. A skipped indexing
  sweep also delayed alerts.
- Import refuses a rule whose pattern alone is in quotes. In an inline
  list (`rules: [include **/*, exclude "*/secret/**"]`) the quotes became
  part of the pattern, so the rule never matched.
- A folder ZIP over `MAX_FETCH_MB` is refused before anything is
  downloaded, using the sizes in the listing, and the 413 now gives the
  folder's size. Before, an SMB folder downloaded up to the limit first.
- The source page reloads when the source id in the URL changes.
- Paths the push-agent refuses (a link out of its folder) answer 404, as
  over SSH, instead of 502.
- TLS errors say whether the certificate is for another name, has expired
  or comes from an unknown issuer, instead of always "isn't trusted".
- Import keeps offering to set a credential for sources that have none,
  also after they've been imported, and takes an SSH private key as well
  as a password.
- Compare shows which source each side comes from.
- Machines and folders expanded in the explorer stay expanded after
  visiting Search or Settings, like the tabs.
- A change to the Connections view (System settings) reaches users who
  already have PerchTail open on their next navigation, without a reload.
- The source page shows the plain-HTTP warning for WinRM sources, as the
  editor does.
- `startup.data_layout.moved` is logged as a structured event in Docker
  too, and `logs.retention.size_limit` has the numbers as fields instead
  of in the event name.

### Added
- [docs/install-from-source.md](docs/install-from-source.md): running
  PerchTail on a Linux host without Docker, with a systemd unit.
- Releases now carry what's needed to install them (`.github/workflows/release.yml`):
  - push-agent binaries for Linux (amd64, arm64) and Windows (amd64), with `SHA256SUMS`;
  - the Docker image on `ghcr.io/quaresma870/perchtail`, for amd64 and arm64, with provenance and an SBOM;
  - the SBOM, also attached to the release.
  `latest` and `X.Y` image tags move only with full releases. The workflow can also be run by hand for an existing tag, to add these to a release published earlier.

### Changed
- `backend/requirements.txt` now pins every Python package, transitive
  ones included, so the image and a source install get exactly the same
  versions. The version ranges moved to `backend/requirements.in`; see
  CONTRIBUTING.md for how to regenerate the pins.

## [0.5.0] - 2026-11-04

Fixes and features from the Hyper-V lab test of 0.4.0. The viewer
becomes one explorer across every source. Sources can read several
folders, and their Base path can be picked by browsing the host. Searches
can be narrowed by customer, folder, source and path. The whole
configuration can be bulk imported and exported as YAML. Logs are capped
by size, and the database moves to its own directory. Folder visibility
now follows the rules (a security fix), and the WinRM docs match what
the connector really needs.

### Upgrading

Database migrations run automatically on startup. Back up the `/data`
volume first.

- **The database, credential salt and SSH `known_hosts` move to
  `/data/db`** (#172). On the first start of 0.5.0 they are moved from
  `/data` automatically, salt first and database last, and the move is
  logged (`startup.data_layout.moved`). Nothing to do with the stock
  `docker-compose.yml`. If you set `DATABASE_URL`, `CREDENTIAL_SALT_PATH`
  or `SSH_KNOWN_HOSTS_PATH` yourself, those paths are kept as they are.
  If a database is still found at `/data/perchtail.db` while another one is
  in use in `/data/db`, startup logs `startup.data_layout.legacy_database_left`:
  stop PerchTail and move the right one into place. Update your backups to
  include `/data/db`.
- **Logs** now rotate at `LOG_MAX_FILE_MB` (100) as well as at midnight, and
  all logs together are capped at `LOG_MAX_TOTAL_MB` (2048). Archives are
  named `perchtail.log.<day>.<n>.gz`; existing `perchtail.log.<day>.gz`
  archives keep expiring under `LOG_RETENTION_DAYS`.
- **WinRM sources:** the documented JEA setup could never work with this
  connector, which needs an account with remote-shell rights. The docs now
  say so; prefer SMB or the push agent where possible (#169). Existing
  WinRM sources keep inferring HTTPS or HTTP from the port until they are
  saved in the editor, which stores the scheme explicitly.
- **SMB and WinRM base paths** are now validated when they change: an SMB
  source pointing at `C$` or a drive path, or a WinRM source with a
  relative path, keeps working but must be corrected the next time its
  path is edited (#159).

### Added
- **One explorer across every source** (#162):
  - machines expand in place in the viewer's sidebar, in list or tree
    view, and their folders load lazily;
  - tabs from several sources stay open side by side, each marked with
    its source, so the same `app.log` on two servers is two tabs;
  - Compare works across sources;
  - open tabs survive visiting Search or Settings and coming back (in
    memory only, closed on logout);
  - a source that fails to load shows its error on its own node;
  - `/viewer/<id>` now expands and reveals that source.
- **Find all, Notepad++-style** (#155):
  - one row per matching line, showing the whole line with every match
    highlighted and a hit count;
  - rows can be selected with click, Ctrl/Cmd+click, Shift+click or
    Ctrl/Cmd+A;
  - a right-click menu copies the selected lines (with or without line
    numbers), searches within the results, excludes lines matching a
    pattern (both chainable, with breadcrumbs to go back), and jumps to
    the line.
- **Source overview** (#157): creating a source lands on a read-only
  overview instead of back on its edit form. It shows the details, a
  prominent "No rules yet" callout, the rules, a connection test and, for
  agent sources, the enrollment token. Editing moved to
  `/settings/sources/<id>/edit`.
- **Settings → System → Connections view** (#156): a default list or tree
  view for everyone, and whether users may switch between them. When they
  may, each browser remembers its choice.
- **Pick the Base path by browsing the host** (#159): **Browse…** next to
  the field lists folders over SSH, drives then folders over WinRM, and the
  host's shares then folders over SMB (share listing via SRVSVC, with no
  new dependency). Folders only, never file names; limited to users who
  can manage sources and audited. A saved credential is only reused for
  the host and port it was saved for.
- **Several folders in one source** (#161): labelled folders on the same
  host, each shown at the top level of the source's tree. Rules see the
  labelled paths (`**/*.log` for all, `IIS/**` for one), and the connection
  test checks each folder. Push-agent sources stay single-folder. How many
  folders a source may have is set in Settings → System → Folders per
  source: 20 by default, 1 turns the feature off, 100 at most. Lowering it
  never locks an existing source out: it keeps its folders but can't add
  more.
- **Search scope** (#163): narrow a search to any mix of customers,
  folders (with their sub-folders) and sources, and to file paths
  containing some text or matching a glob. The scope is applied before the
  result limit, kept in the URL and remembered per browser. The API takes
  `customer_id`, `folder_id`, `source_id` (repeatable) and `path`.
- **WinRM scheme and CA certificate per source** (#169): an explicit
  HTTPS/HTTP choice instead of guessing from the port, with each scheme's
  default port, and an optional CA certificate (PEM) trusted for that
  source only, for internal PKI or self-signed listeners, without a
  server-wide `REQUESTS_CA_BUNDLE`.
- **Log size limits** (#172): `LOG_MAX_FILE_MB` rotates the log before
  midnight when it grows past it, and `LOG_MAX_TOTAL_MB` caps all logs,
  deleting the oldest archives first. When that cap deletes logs that
  `LOG_RETENTION_DAYS` would have kept, a warning is logged and
  `/monitoring/health` reports `logs.size_limit_deleted_early`.
  `LOG_QUIET_THIRD_PARTY=true` keeps PerchTail's own DEBUG logging while
  capping protocol libraries (smbprotocol, spnego, paramiko, ...) at INFO.
- **Bulk import and export** (#179): Settings → Import / export (super
  admins) creates and updates customers, folders, sources and rules from
  one YAML file, with a preview of every change, credentials from the
  file, `${env:PERCHTAIL_IMPORT_*}` or typed in, and an all-or-nothing
  apply. Importing the same file again changes nothing; nothing is ever
  deleted. Export writes the same format, without credentials. See
  `docs/bulk-import.md` and `docs/examples/perchtail-import.example.yaml`.

### Changed
- State is split by kind (#172): `DATA_DIR` (`/data/db` in the image)
  holds the database, credential salt and SSH `known_hosts`, apart from
  `LOG_DIR` (`/data/logs`) and `SCRATCH_DIR` (`/data/scratch`), so each can
  be its own volume and a log burst can't fill the database's disk. See
  the upgrade notes above.
- Base path is checked per protocol when a source is created or its path
  changes (#159): SMB takes a share name (no drive paths like `C:\Logs`,
  no `C$`/`ADMIN$`), WinRM an absolute Windows path on any drive. The
  editor shows a protocol-specific placeholder and hint. Existing sources
  aren't re-checked until their path is edited.
- Changing a source's host, port, base path or folders clears its search
  index and alert positions, since the files are no longer the same, and
  the `source.update` audit entry now records old and new values (the
  credential only as "replaced").
- The window itself no longer scrolls (#154). The navigation stays in
  place, and in the viewer only the log pane, the explorer and the Find
  all panel scroll. Before, a large log stretched the whole page.
- A failed open, reload or compare now shows a banner above the editor
  with the server's reason, e.g. "This file is 100.0 MB, over this
  server's 64 MB limit (MAX_FETCH_MB)", plus a Try again action when
  useful. The explorer is no longer wiped. The 413 body now includes
  `size_bytes` and `limit_bytes` (#160).
- `/open` is served inline as text instead of as an attachment.
  Download icons and actions are hidden from users without the
  `download` capability on that source, using the new
  `my_capabilities` field of each source (#174).
- WinRM setup now states what the connector actually needs: an account with remote-shell rights (Administrators, or `GXGR` in the WinRM `RootSDDL`). The JEA instructions, which couldn't work, are gone; moving to PSRP so a JEA endpoint can constrain the account is tracked (#169). SMB or the push agent is recommended where possible.
- New: base path per protocol, the folder picker and several folders per source (`docs/source-setup.md`), bulk import (`docs/bulk-import.md`), the data directories (README), and the `logs` health fields (`docs/monitoring.md`).
- SSH/SFTP setup: `ForceCommand internal-sftp` is required with a `nologin` shell; chroot is optional hardening, with the sshd ownership rule and a bind-mount example for Debian/Ubuntu; with a chroot, Base path is relative to it (#173). Folder visibility and SSH rule case-sensitivity are documented with the rule principles.

### Fixed
- WinRM sources whose base path is a drive root (`D:\`) listed the
  current directory on that drive instead of its root.
- Source editor and role editor form controls: checkboxes and toggles
  sit next to their labels again (#158).
- Secret fields (SSH private key, passwords, SSO client secret) opt out
  of browser spellcheck and grammar extensions such as Grammarly, which
  could otherwise send their contents to third-party servers. The
  private key field is monospace without soft wrapping (#158).
- Push-agent sources can be browsed and opened again. Browse, open and
  download answered 501 "protocol agent not yet supported" while the
  connection check passed. All code paths now share one connector
  registry (#165).
- SMB: the live log of a Windows service, held open for writing by
  another process, can now be opened instead of failing with
  `STATUS_SHARING_VIOLATION` (#166). A file over `MAX_FETCH_MB` is refused
  before transferring anything, instead of after downloading up to the
  limit, which took minutes (#167).
- WinRM: a folder containing a file with an accented or other non-ASCII
  name can now be listed. Before, the whole listing failed with a decode
  error (#168).
- Predictable source-side failures get their own status and a clear
  message instead of a bare 500:
  - no read permission: 403;
  - not found: 404;
  - file locked: 409;
  - unreachable, authentication, SSH host key mismatch or untrusted TLS
    certificate: 502.

  An SMB Base path that isn't a share now says so (#170).
- Archives (`.zip`, `.tar.gz`) are browsed as a folder tree instead of a
  flat list of every member. Members with odd names (`../`, absolute) are
  shown normalized and still open (#175).

### Security
- Rules now decide which **folders** are visible, not only which files. Before, every folder was always listed. An excluded folder, and through direct API calls its whole tree of sub-folder names, stayed visible, and a source with no rules showed its complete directory skeleton. A folder is now shown and browsable only if some file below it could be visible under the same ordered, last-match-wins rules. Hidden folders answer 404 on browse and folder ZIP, and the search indexer no longer walks them (#164). **Upgrade note:** folders that only contain files your rules don't show now disappear from the tree. Root-level patterns such as `*.log` never cross `/`, so they show no folders at all: use `**/*.log` to include matching files at any depth. Regex rules are analysed conservatively; to hide a folder reliably, use a glob exclude such as `folder/**`.
- Failed logins, account lockouts and login attempts while locked out are now recorded in the audit log and the app log (`user.login_failed`, `user.locked_out`, `user.login_throttled`), with the attempted username, client IP and reason, never the password. Repeated identical failures within 60 s are folded into one row (#171).
- Connection checks no longer show raw library errors (UNC paths, host keys, internals) to users who can only view a source. They get an actionable message instead, and users who can manage sources also see the underlying error (#170).

## [0.4.1] - 2026-11-03

A security fix release. SSH/SFTP sources no longer follow symlinks out
of the base path. **Upgrade now if you use SSH sources without a
`ChrootDirectory`.** No migrations and no configuration changes: replace
the container.

### Security
- SSH/SFTP sources no longer follow symlinks out of the base path. A
  symlink inside the base path (for example `escape -> /etc`) let anyone
  with `view` on an SSH source that isn't chrooted browse the link target
  and read any file the SSH account could read, such as `/etc/passwd`.
  Paths are now resolved on the server (SFTP `realpath`) before every
  listing and read, and anything that resolves outside the resolved base
  path gets the same 404 as a path hidden by the rules. Outward and
  dangling symlinks are left out of listings. Symlinks that stay inside
  the base path still work, and are shown as the file or folder they
  point to. Refusals are logged as `ssh.path_outside_base`. Found in the
  Hyper-V lab tests. The push agent got the equivalent fix in 0.3.0;
  local sources were already contained.

## [0.4.0] - 2026-11-03

A security-hardening release: fixes from the internal review that
prepares the external pentest (#124). It includes one database migration
(applied automatically on start) and several changes that need action
when upgrading. **Before upgrading, check:**

1. **Container user:** the container now runs as uid/gid 10001, not root.
   Host directories mounted for local-disk sources (`LOCAL_SOURCE_ROOTS`)
   must be readable by uid 10001. `/data` is fixed up automatically.
2. **Alerts:** creating or editing alerts needs the new `manage_alerts`
   global capability, and an alert only fires while its owner holds it.
   Grant it to the roles that use alerts.
3. **Regex rules:** `re:` rules now use RE2. A rule that uses lookaround
   or backreferences fails closed (logged as `rules.invalid_regex`) and
   must be rewritten.
4. **Push agent:** a `ws://` server URL is refused unless
   `PERCHTAIL_ALLOW_INSECURE=true`. Use `wss://`.
5. **SSO:** accounts are now bound to the IdP's issuer. Existing accounts
   are bound on first start when exactly one OIDC provider is configured
   or enabled. Otherwise watch for a `sso.issuer_backfill_skipped`
   warning.
6. **Delegated SSO admins** (`manage_sso` without super admin) can no
   longer change the trusted IdP or group claim while a group mapping
   grants a role above their own.

New setting: `MAX_FETCH_MB` (default 2048) caps each remote fetch and
each folder ZIP.

### Security
- The push agent now refuses to start with a plain `ws://` server URL,
  because its enrollment token and every file it serves travel over that
  connection. Use `wss://`, or set `PERCHTAIL_ALLOW_INSECURE=true` to
  opt in for testing or a fully trusted network. **Upgrade note:** an
  agent configured with `ws://` and no opt-in exits on start with an
  error saying so.
- A delegated SSO admin (`manage_sso` without super admin) could escalate
  to any role a group mapping grants, including Super Admin, by pointing
  the provider at an IdP or group claim they control and signing in as a
  member of the mapped group. Creating a provider, enabling one, or
  changing its issuer, client id or group claim now returns 403 unless
  every group mapping's role is within the admin's own authority, the
  same bar as creating such a mapping directly. Renaming, rotating the
  client secret and disabling stay open to them.
- Regex rules (`re:`) are now compiled with RE2 (`google-re2`), which
  always matches in linear time. A catastrophic pattern such as `(a+)+$`
  can no longer stall every browse of a source (ReDoS). RE2 has no
  backreferences or lookaround, so a rule using them is rejected with a
  400 that says why. **Upgrade note:** an existing rule that uses them
  fails closed (a broken include matches nothing, a broken exclude hides
  everything) and is logged once as `rules.invalid_regex`; rewrite it.
  Severity patterns run in the browser, not on the server, so they keep
  full regex syntax.
- SSO accounts are now identified by the IdP's issuer and subject
  (`iss` + `sub`), not by `sub` alone. `sub` is only unique within one
  IdP, so after the SSO provider was pointed at a different issuer, a user
  there whose `sub` matched an existing account's would have signed in as
  that account, with its role. **Upgrade note:** on first start, existing
  SSO accounts are bound to the enabled OIDC provider's issuer (or the
  only one configured). If that is ambiguous, they are left unbound and a
  `sso.issuer_backfill_skipped` warning is logged: those users then get a
  new account at their next login, which clashes on username until an
  admin renames or deletes the old one.
- Turning off full-text search for a source, or disabling the source, now
  deletes the search snippets already stored for it, along with its
  alerts' per-file progress. Before, they stayed in the database
  indefinitely. Each indexing sweep also purges leftovers from sources
  that are no longer active, which covers anything left by earlier
  versions.
- Remote fetches are capped by a new `MAX_FETCH_MB` setting (default
  2048). The cap applies per file and to a folder ZIP's total. Over the
  limit, the request gets HTTP 413 and no partial file is left behind. SSH
  checks the size before transferring anything; SMB streams in chunks and
  stops at the cap. WinRM no longer reads a whole file into memory in one
  PowerShell call: it checks the size first, then reads byte ranges, and
  opens the file with shared read/write access, so a log that is still
  being written can be read. Folder ZIPs stop as soon as their total
  passes the cap, instead of growing outside the scratch size guard.
- Opening a file (`file.open`) and downloading a folder as ZIP
  (`folder.download`, with its file count) are now recorded in the audit
  log, like single-file downloads already were. Viewing a file necessarily
  sends its full content to the browser, so `view` can read whole files.
  `download` gates bulk export on top of that, and the role editor now
  says so on each capability.
- Alerts are now treated as the export channel they are. Creating,
  editing, enabling or testing an alert needs a new global capability,
  `manage_alerts`. Anyone can still list and delete their own alerts.
  An alert only fires while its owner holds `manage_alerts`, re-checked at
  every evaluation like source access. Webhook targets must be publicly
  routable (an `is_global` allowlist), which also rules out carrier-grade
  NAT, documentation and benchmarking ranges and IPv4-mapped IPv6.
  Creating, editing and deleting an alert is audited; the audit log
  records only the webhook's host, never its path or query, which often
  carry a token. **Upgrade note:** grant `manage_alerts` to the roles that
  should keep using alerts. Until you do, their existing alerts stop
  firing.
- The Docker image now runs PerchTail as an unprivileged `perchtail` user
  (uid/gid 10001) with no Linux capabilities, instead of as root. The
  entrypoint starts as root only long enough to give `/data` to that user,
  so existing root-owned volumes keep working, then drops privileges.
  **Upgrade note:** host directories mounted for local-disk sources
  (`LOCAL_SOURCE_ROOTS`) must be readable by uid 10001, for example
  `setfacl -R -m u:10001:rX /var/log/app` on the host.
- CI: `aquasecurity/trivy-action` is pinned to `v0.35.0` instead of
  `@master`. That action's tags were force-pushed to credential-stealing
  code in March 2026, and a branch reference would have run whatever was
  pushed there. `actions/checkout` and `actions/setup-python` move to their
  Node 24 majors, and Dependabot now also keeps the workflow's actions up
  to date.
- Login lockout can no longer be reset by flooding the throttle with other
  usernames. An entry only leaves the tracker once it's idle (unlocked,
  with no failure inside the lockout window). If the tracker fills with
  live entries, new usernames are refused with 429 instead of evicting
  someone else's count.
- A login with an unknown, inactive or SSO-only username now costs the
  same argon2 verify as a wrong password, so response timing no longer
  reveals which usernames exist.
- `/auth/change-password` is throttled like login. Changing your password
  signs out every other session. An admin password reset signs the user
  out everywhere.

### Fixed
- An agent that reconnected before the server noticed its old connection
  had dropped showed as disconnected, and every browse of its source
  failed, until it reconnected again. The old connection's cleanup was
  removing the new one. Now a new connection replaces the old one
  cleanly: the old socket is closed, commands still waiting on it fail at
  once instead of timing out, and late replies from it are ignored.
- Expired login sessions are now deleted hourly instead of piling up
  forever, and a session's `last_seen_at` is written at most once a
  minute instead of on every authenticated request.

## [0.3.1] - 2026-11-03

A bug-fix release: SSH sources now accept ed25519 and ECDSA private keys,
not only RSA. No migrations and no configuration changes; upgrade by
replacing the container.

### Fixed
- SSH sources only accepted RSA private keys, even though
  `docs/source-setup.md` tells you to generate an ed25519 one. Pasting an
  ed25519 or ECDSA key failed on every connection. All three key types
  now work, in OpenSSH or PEM format. A passphrase-protected key, or text
  that isn't a key, now fails with a clear message instead of a parser
  error.

## [0.3.0] - 2026-11-02

A security release on top of new viewer and navigation features. A full
security and bug review of the codebase, plus a CodeQL code-scanning pass,
found and fixed the following:
- delegated admins could escalate themselves to super-admin;
- admin-created local-disk sources could read anywhere on the PerchTail
  host;
- exclude rules could be bypassed with a non-canonical path;
- deleted ids could be reused and inherit their old access;
- WinRM path quoting missed the Unicode quote characters;
- archives had no decompression limit (zip bombs);
- the Go push-agent could follow a symlink out of its folder;
- first-use SSH host-key trust was silent: every pin is now logged with
  its fingerprint, and a strict mode refuses unknown hosts.

Also new since 0.2.0:
- optional TOTP/MFA for local accounts;
- tamper-evident (HMAC hash-chained) audit log;
- viewer Compare, Beautify/Minify, Follow and multi-pattern Highlight;
- a tree view of all connections, with a Folders admin page.

Bugs fixed: concurrent opens of one file corrupting each other, invalid
regex rules breaking a whole source, and alerts that missed new matches
and repeated old ones.

### Upgrading

Database migrations run automatically on startup. Back up the `/data`
volume first. Before you restart, check the following:

- **Local-disk sources** (other than the built-in application-log source)
  are now off by default. Set `LOCAL_SOURCE_ROOTS` to the directories they
  read from, comma-separated. Until you do, those sources answer 403.
- **Docker deployments, do this before you recreate the container:** the
  credential salt and SSH `known_hosts` now live on the `/data` volume.
  While the *old* container is still running, copy them there:

  ```sh
  docker compose exec perchtail sh -c 'cp -n /app/backend/data/credential_salt /data/ 2>/dev/null; cp -n /app/backend/data/ssh_known_hosts /data/ 2>/dev/null; ls -l /data/credential_salt'
  ```

  If you skip this step, the new version refuses to start rather than
  silently losing every stored credential, SSO secret and MFA secret.
- **SMB/WinRM rules** now match case-insensitively, as Windows does.
- **Regex rules** that don't compile are now rejected when saved. Any
  already stored fail closed: a broken include matches nothing, and a
  broken exclude hides everything. Check the ERROR log for
  `rules.invalid_regex` after upgrading.
- New optional settings: `MAX_DECOMPRESSED_MB` (default 2048) and
  `SSH_HOST_KEY_POLICY` (`tofu`, the default, or `strict`).

### Added
- Viewer: Compare (a read-only side-by-side diff against a second file
  picked from the tree or another open tab, via `@codemirror/merge`),
  Beautify/Minify for JSON and XML tabs (display-only, never touches the
  file on disk), Follow (a `tail -f`-style toggle that polls and re-fetches
  the open file every few seconds), and multi-pattern "Highlight" (ad hoc,
  session-only pattern highlighting, Notepad++'s Mark feature, each
  pattern in its own color). See ROADMAP.md's notes for design decisions,
  including two real bugs (a scroll-to-top regression on Follow, a
  double-add on Enter in the highlight input) found and fixed while
  building these.
- Connections home: a "List"/"Tree" toggle for "All connections", showing
  the real nested customer → folder → … → source hierarchy (built
  client-side from a new `folder_path` field on each source), alongside
  the existing flat list. A new "Folders" admin page (Settings → Folders)
  covers create/rename/move/delete for folders. See ROADMAP.md's notes,
  including a real RBAC information-disclosure issue (a narrowly-scoped
  folder/source grant could leak ancestor folder names above its own
  scope) found via code review and fixed before this shipped.

### Security
- SSH host keys are now pinned through PerchTail's own trust-on-first-use
  policy instead of paramiko's `AutoAddPolicy`. The behaviour is the
  same, but every newly pinned key is now logged at WARNING
  (`ssh.host_key_pinned`) with its OpenSSH-style `SHA256:` fingerprint,
  so you can check it against the server. The new
  `SSH_HOST_KEY_POLICY=strict` setting refuses any server that isn't
  already in `SSH_KNOWN_HOSTS_PATH`. Resolves the CodeQL alert
  `py/paramiko-missing-host-key-validation`.
- The Go push-agent now reads through `os.Root`, so a symlink inside its
  watched folder can no longer point a list or fetch at any other file on
  that machine. The lexical `..` check stays as the first line of
  defence. Found by CodeQL (`go/path-injection`).
- The CI workflow's `GITHUB_TOKEN` is limited to `contents: read`
  (CodeQL `actions/missing-workflow-permissions`).
- Local-disk path containment uses `os.path.realpath` and a
  separator-terminated prefix check, which static analysis recognises.
  The behaviour is the same, and an allowlist entry of `/` still works.
  Opening or downloading with an empty path is now a 400. Before, an
  empty path named the source's base directory itself and failed with a
  500.
- Opening a `.gz` file or a `.zip`/`.tar.gz` member is now capped at
  `MAX_DECOMPRESSED_MB` of decompressed output (default 2048). Previously a
  small crafted archive (a "zip bomb") could expand without limit and
  fill the scratch disk. The cap counts the bytes actually produced, not
  the size the archive header claims. Going over it returns HTTP 413 and
  deletes the partial file. The search indexer now stops decompressing at
  its own per-file limit instead of decompressing in full first.
- Delegated admin capabilities can no longer be used to reach
  super-admin. A non-super-admin can now only assign, edit, grant on, or
  map an SSO group to a role that isn't super-admin and carries no global
  capability they don't hold themselves, and can't reset the password of,
  deactivate, or edit a user more privileged than themselves. Built-in
  roles ("Super Admin", "No Access") are now immutable, the last active
  super-admin can't be deactivated or demoted, and role/grant updates and
  deletions are now recorded in the audit log.
- Admin-created "Local disk" sources are now off by default and can only
  point under directories the server operator lists in the new
  `LOCAL_SOURCE_ROOTS` environment setting. Previously anyone with
  `create_source` could choose any base path on the PerchTail host itself.
  Local reads are also confined to the source's base path, so a symlink
  can't lead outside it. **Upgrade note:** existing local-disk sources stop
  working (403) until `LOCAL_SOURCE_ROOTS` covers their base path. The
  built-in application-log source is unaffected.
- Exclude rules can no longer be sidestepped by requesting a
  non-canonical spelling of the same path. Browse/open/download/close now
  reject `.` and empty path segments, backslashes, `:` and control
  characters. SMB and WinRM sources also reject trailing dots/spaces and
  8.3 short names, and match rules case-insensitively, as Windows does.
  **Behavior change** for SMB/WinRM sources: rules that previously relied
  on case to tell files apart now match regardless of case.
- Deleting a customer, folder, source or role can no longer hand its access
  to whatever gets created next. SQLite reused the highest deleted id, and
  the role grants, search-index snippets, alerts and severity patterns left
  pointing at the old row silently attached to the new one, potentially
  under a different customer. Those tables now use `AUTOINCREMENT`,
  deletes remove everything that pointed at the deleted row, a role can't
  be deleted while an SSO group mapping still targets it, and a migration
  cleans up rows already orphaned this way and starts every id sequence
  past any id the audit log has ever recorded.
- WinRM sources now escape every character PowerShell accepts as a
  single-quote string delimiter (the ASCII apostrophe and the Unicode
  curly-quote variants U+2018–U+201B) when embedding a path in a command.
  Previously only the ASCII apostrophe was escaped, so a crafted path could
  end the quoted string early on the Windows host.
- Optional TOTP/MFA for local accounts (`pyotp`). Self-service only, via a
  new Settings → Security page: enroll by scanning a QR code (or entering
  the secret manually), confirm with a live code before it's actually
  turned on, and get 10 one-time backup codes shown exactly once. Once
  enabled, `/auth/login` requires a second factor (a live TOTP code or an
  unused backup code) after the password; a wrong code counts against the
  existing login-throttle lockout, a missing one doesn't. A live TOTP code
  is single-use, not just time-limited — replaying an already-accepted code
  within its normal validity window is rejected. Enrolling, disabling, and
  regenerating backup codes all require re-confirming the current password,
  throttled the same way as login itself, so a stolen session token alone
  isn't enough to take over an account's second factor. The TOTP secret
  is Fernet-encrypted at rest (like `Source.credential_ref`); backup codes
  are one-way argon2 hashes. See ROADMAP.md's security-hardening notes for
  the full design, including why login finalization (`last_login_at` /
  the `user.login` audit event) had to move behind the MFA check rather
  than firing on password-correct alone.
- Audit log tamper-evidence: `AuditLog` rows are now HMAC-SHA256
  hash-chained (`app/audit_hash_chain.py`), keyed by a second key derived
  from `CREDENTIAL_ENCRYPTION_KEY` (domain-separated from the credential
  Fernet key, `app.crypto.audit_chain_key`) so the chain can't be silently
  recomputed by anyone who only has the SQLite file itself. Altering or
  deleting a row anywhere changes every hash after it. A new periodic
  verification job (`app/audit_integrity.py`) walks the chain on its own
  configurable cadence (`audit_integrity_check_interval_days`, default 365
  — explicitly independent of the retention window and purge cadence), and
  a manual, cooldown-throttled "Verify now" action on the Audit Log page
  runs the same check on demand. Existing history is backfilled
  automatically on first startup after upgrading; the retention purge
  sweep now deletes only a genuine id-prefix of the table (not just
  whatever matches the timestamp filter, which could rarely diverge from
  id order under concurrent writes) and advances a persisted chain anchor,
  so routine purging doesn't itself look like tampering to the next
  verification pass. See ROADMAP.md's security-hardening notes for the
  full design, including its one documented, accepted limitation around a
  narrow concurrent-write race.

### Fixed
- Alerts missed new matches and repeated old ones. A new matching line
  could be missed when more than 200 older matches ranked above it,
  because only one top-200 search across the whole index was checked.
  Every append to a file also re-sent all of that file's old matching
  lines. Each changed file is now searched on its own, starting after the
  last line that alert already reported. A rotated or rewritten file
  reports from the top. Adds the `alert_file_state` table (migration
  `c5e1a7d93b20`).
- Two viewers opening the same file at the same moment could corrupt each
  other's view. Both fetches wrote into one shared scratch file, so the
  second fetch truncated and rewrote it while the first response was
  still streaming. Each fetch now gets its own scratch file, and the
  reference-counted purge removes them all once the last viewer closes.
- A rule (or severity pattern) with an invalid regex, such as an
  unbalanced `(`, used to save fine and then make every browse, open and
  download of that source fail with a server error. Invalid regexes are
  now rejected with a 400 when saved, and a raw-text paste names the bad
  line and leaves the existing rule chain untouched. Rules saved before
  this change fail closed: a broken include rule matches nothing, and a
  broken exclude rule hides everything rather than exposing what it was
  meant to hide. Each broken pattern is logged once at ERROR.
- The Docker image now keeps the credential-encryption salt and SSH
  `known_hosts` on the `/data` volume (`CREDENTIAL_SALT_PATH`,
  `SSH_KNOWN_HOSTS_PATH`). Previously both defaulted to a path inside the
  container's own filesystem, so recreating or upgrading the container
  silently generated a new salt, which made every stored credential, SSO
  client secret and MFA secret undecryptable, made the audit-log integrity
  check report the whole chain as broken, and reset SSH host-key pinning.
  Startup now also refuses to generate a fresh salt while the database
  already holds data encrypted under the old one, instead of failing later
  (`CREDENTIAL_SALT_ALLOW_REGENERATE=true` overrides this if the original
  is permanently lost). **Upgrade step for Docker deployments:** while the
  *old* container is still running, copy both files onto the volume first,
  then upgrade as usual:

  ```sh
  docker compose exec perchtail sh -c 'cp -n /app/backend/data/credential_salt /data/ 2>/dev/null; cp -n /app/backend/data/ssh_known_hosts /data/ 2>/dev/null; ls -l /data/credential_salt'
  ```
- `OriginCheckMiddleware`'s CSRF defense-in-depth check was blocking every
  state-changing request — including login — when deployed behind a
  TLS-terminating reverse proxy (the standard, documented setup): uvicorn
  runs with no proxy-header trust configured, so it always saw the
  proxied connection's scheme as `http`, while a real browser's `Origin`
  header correctly said `https`, and the check compared both. It now
  compares only `host[:port]`, matching what the middleware's own
  docstring already claimed it did. Found and verified while writing the
  reverse-proxy deployment docs, before anyone hit it in production — see
  ROADMAP.md's CSRF-review notes for the full reproduction and rationale.
- CI: the `docker-image` job's Trivy scan was failing on every build (not
  just feature PRs) because `python:3.12-slim`'s baked-in OS packages had
  fallen behind Debian's own published security patches between upstream
  rebuilds. The `Dockerfile`'s backend stage now runs `apt-get update &&
  apt-get upgrade -y` before installing Python dependencies, pulling
  current packages from the live Debian security mirror at build time.

## [0.2.0] - 2026-09-08

Phase 2 (push-agent) and Phase 3 (full-text search, alerting, IdP
group-claim-to-role auto-mapping, external monitoring) complete, plus a
full admin audit log viewer, a redesigned connections home page, a set of
viewer toolbar/editor improvements, and an extensive pre-1.0
security-hardening pass — login lockout, security response headers,
CI-blocking dependency and container-image vulnerability scanning,
`CREDENTIAL_ENCRYPTION_KEY` rotation with a tooled migration path, salted
PBKDF2 credential-key derivation, persisted SSH host-key pinning, a
Sessions page for revoking a login remotely, a CSRF review, and two real
vulnerabilities found and fixed via a full security-review pass: a
DNS-rebind SSRF gap in outbound alert webhooks and a login-CSRF gap in
OIDC SSO. Covered end-to-end by a new Playwright e2e suite that drives
the real browser UI against real SSH/SMB test servers, not just mocks.
**Breaking for existing deployments**: the credential-encryption key
derivation change means previously-encrypted credentials need
`python -m app.rotate_credential_key` (or reconfiguring sources/SSO from
scratch) after upgrading — see
[docs/credential-key-rotation.md](docs/credential-key-rotation.md).

### Security
- Fixed a residual SSRF gap in the alert webhook safety check (found via a
  full security-review pass over everything merged since v0.1.1, follow-up
  to #49): the pre-send re-check re-validated the plain hostname and then
  handed it straight to `httpx.post`, which resolves DNS independently at
  connect time — an attacker's own authoritative DNS server could answer
  the validation query with a public address and the connection query,
  moments later, with an internal one (e.g. cloud metadata), since those
  were always two separate lookups no matter how close together. `send_webhook`
  now resolves and validates the hostname exactly once
  (`webhook_safety.resolve_pinned_webhook_target`) and connects directly to
  that specific already-validated IP, with the original hostname preserved
  as the `Host` header and TLS SNI/certificate-verification name — nothing
  re-resolves the hostname between validation and connection.
- Fixed a login-CSRF gap in OIDC SSO (#61): the OAuth `state` param
  was never bound to the browser that started the flow, so a code+state
  pair captured from an attacker's own successful login could be replayed
  by a victim's browser, silently logging the victim into the attacker's
  PerchTail account. `/auth/sso/login` now also sets a short-lived,
  `httponly`, `samesite=lax` cookie carrying the same `state` value;
  `/auth/sso/callback` rejects any request whose `state` doesn't match
  that cookie, closing the gap without needing a server-side flow table.
- New "Sessions" page under Settings (visible to every user, like Sources)
  lists every device/browser currently signed in as you and lets you
  revoke one remotely — a prerequisite for "someone else is logged in as
  me" incident response. Sessions now record the browser's `User-Agent` at
  login so rows in the list are actually distinguishable from each other.
  Self-service only — a user can see and revoke only their own sessions.
- `CREDENTIAL_ENCRYPTION_KEY` can now be rotated without losing access to
  already-encrypted credentials: `python -m app.rotate_credential_key`
  (with `--dry-run` to preview first) re-encrypts every `Source.credential_ref`
  and `SSOProviderConfig.config` row from the old key to a new one in a
  single all-or-nothing transaction. See
  [docs/credential-key-rotation.md](docs/credential-key-rotation.md).
- CSRF review: confirmed the existing `SameSite=strict` session cookie is
  sufficient protection on its own across all 44 state-changing endpoints
  (every one requires that cookie; no `CORSMiddleware` exists; the agent
  WebSocket endpoint uses a bearer token, not a cookie). Added
  `OriginCheckMiddleware` anyway as defense-in-depth: for every
  `POST`/`PUT`/`PATCH`/`DELETE`, `Origin` (or `Referer`) must match the
  request's own `Host` header when either is present, closing the residual
  gap of a browser bug or non-standard client not honoring `SameSite`. No
  frontend changes needed for this — `vite.config.ts`'s dev proxy was
  updated to strip these headers on proxied requests so local dev (which
  legitimately runs frontend and backend on different ports) isn't
  affected.
- CI now blocks on dependency and container vulnerabilities: `pip-audit`
  (backend), `npm audit --audit-level=high` (frontend), and `govulncheck`
  (the Go agent) run in their respective CI jobs; a new `docker-image` job
  builds the published `Dockerfile` and scans it with Trivy
  (HIGH/CRITICAL, fixable only), and generates a CycloneDX SBOM uploaded as
  a build artifact on every run. `.github/dependabot.yml` opens weekly
  update PRs against `dev` for all three ecosystems (pip, npm, gomod).
  Fixed one pre-existing high-severity transitive vulnerability (`nanoid`,
  via vite→postcss) as part of turning this on.
- Security response headers (`Content-Security-Policy`, `X-Frame-Options`,
  `X-Content-Type-Options`, `Referrer-Policy`) are now sent on every
  response, API and frontend alike. CSP allows only same-origin scripts and
  styles (`'unsafe-inline'` for `style-src` only, to accommodate CodeMirror
  6's runtime-injected editor styles); `frame-ancestors 'none'` backs up
  `X-Frame-Options: DENY` for clickjacking protection.
- Login now locks out after repeated failed attempts against one username
  (`login_max_attempts`, default 5) for a configurable duration
  (`login_lockout_seconds`, default 300s), returning `429` with a
  `Retry-After` header — previously nothing throttled `/auth/login` beyond
  argon2id's own hashing cost.
- SSH connector now persists known host keys across connections
  (`ssh_known_hosts_path`) instead of trusting whatever key each fresh
  connection presents with no memory of previous ones — a connection to a
  host whose key changed since it was last seen now fails loudly instead
  of silently succeeding, closing a man-in-the-middle window on every SSH
  source.
- Credential encryption now derives its key via PBKDF2-HMAC-SHA256 (600k
  iterations) with a persisted per-install salt, replacing a single
  unsalted SHA-256 round that had no work factor at all. The app also now
  refuses to start if `CREDENTIAL_ENCRYPTION_KEY` is still the shipped
  `"changeme"` default, rather than silently encrypting every stored
  credential under a key derivable from this project's own public source.
  **Breaking for existing deployments**: previously-encrypted credentials
  (source SSH/SMB/WinRM secrets, SSO client secrets) can't be decrypted
  under the new derivation — reconfigure them after upgrading.
- Fixed an SSRF vulnerability (#49) in `Alert.webhook_url`: any authenticated
  user — including a freshly auto-provisioned no-access account — could point
  a webhook at an internal address (loopback, link-local/cloud-metadata,
  RFC1918 private ranges) and use `POST /alerts/{id}/test` as a reachability
  oracle against the server's own network. `webhook_url` is now resolved and
  checked against disallowed address ranges both when an alert is created or
  updated and again immediately before the webhook is actually sent, closing
  the DNS-rebinding gap between those two points in time.

### Added
- Full audit log viewer: a new "Audit log" page under Settings, gated by a
  dedicated `view_audit_log` global capability (never implied by any other
  capability — a role needs it explicitly, same as everything else on this
  page being a global, not customer/folder/source-scoped, concern). Lists
  every `AuditLog` entry (login, and every source/rule/role/user/customer/
  folder/SSO/system-settings/severity-pattern change) paginated newest-first,
  filterable by target type and action (both built from the distinct values
  actually in the table right now via `GET /audit/filters`, so a new action
  namespace shows up automatically instead of the list going stale) and by
  date range. `GET /audit` also accepts `user_id` and a trailing `.*` on any
  `action` for a prefix match (e.g. `source.*`), for API consumers, even
  though the shipped UI only exposes exact-action and target-type controls.
  A new `audit_retention_days` system setting (default 365, `0` = keep
  forever), admin-configurable from Settings → System, drives a daily
  `app/audit_purge.py` sweep that permanently deletes anything older —
  resolving this roadmap's long-open "keep audit logs forever, or expire
  after N months?" question. The page itself has its own deployment-wide
  on/off toggle (`audit_view_enabled`, reusing the same `SystemSetting`
  mechanism as the Search toggle), separate from who can see it.
- A Playwright end-to-end test suite (`frontend/e2e/`, `npm run test:e2e`)
  covering login (success, failure, and the logged-out redirect); the
  built-in system log source's viewer and its non-editable sources-list
  row; the sessions page; source create/edit/delete against all three
  remote protocols (SSH, SMB, WinRM) including both rule-editor modes
  (row-based and raw-paste) and last-match-wins, browsing and opening a
  real file over each; roles (global capabilities, grant add/remove,
  duplicate) and users (create, reset password, role change,
  deactivate/reactivate); SSO provider settings, a test-connection
  failure, and group→role mappings; the deployment-wide search/alerts
  toggle and monitoring token generation; global severity-indicator
  patterns; and full-text search (indexing a real source and deep-linking
  a hit into the viewer) plus alerts (create, test webhook, toggle,
  delete). SSH and SMB run against real local test servers
  (`backend/scripts/setup_e2e_test_servers.sh`, plain OS packages, not
  Docker); WinRM is the one protocol mocked at the connector's session
  seam (`backend/app/testing/fake_winrm.py`), since a real target needs an
  actual Windows host. Runs against an isolated backend
  (`backend/scripts/run_e2e_server.sh`) seeded with a deterministic
  super-admin account (`backend/app/seed_e2e_admin.py`), on its own port
  and its own throwaway SQLite DB so it never touches a developer's real
  dev environment. Wired into CI as a new `e2e` job. See ROADMAP.md's
  "Frontend E2E testing (Playwright)" section for the design notes and
  what's deliberately still out of scope.
- SSO: IdP group-claim-to-role auto-mapping. Configure a "group claim" name
  on the OIDC provider and an ordered list of group → role mappings
  (evaluated last-match-wins, same rule as source Rules); a user's role is
  resolved from their IdP groups and applied on every SSO login, not just
  first provisioning, so it stays in sync as group membership changes.
  Applies immediately on first sign-in too — a new user whose groups match
  a mapping is provisioned straight into that role instead of always
  landing on the no-access default. Note: this means a role changed
  directly in PerchTail can be overwritten on that user's next login if
  their IdP groups still match a configured mapping — remove the mapping
  (or the user from that group) to stop the sync.
- Alerting: a new "Alerts" page for saving a search query and getting a
  webhook notification when new matching content shows up in a source's
  index. Rides entirely on the existing full-text search index/indexer —
  evaluated right after every indexing sweep, only re-checking files whose
  index entry has changed since the alert's last check, and only ever
  matching newly-appeared line content (never a filename match, which
  can't meaningfully be "new"). RBAC is re-checked on every evaluation, not
  just at creation, so losing access to a source silently stops that
  alert's scope without needing to edit or delete it. A per-alert **Test**
  button sends a synthetic webhook so the receiver can be verified without
  waiting for real content. Alerts are owned by the creating user (simple
  list/create/enable/delete, not part of the customer/folder/source grant
  tree), and the nav entry is gated by the same deployment-wide toggle as
  Search, since there's nothing for an alert to watch with it off.
- Detailed health endpoint for external monitoring: `GET /monitoring/health`,
  gated by its own deployment-wide bearer token (generated from Settings →
  System, hashed at rest, shown once) rather than user-session auth — a
  monitoring system like Zabbix or Prometheus can't do an interactive login.
  Reports overall status (ok/degraded/error), DB reachability + latency,
  scratch usage, source counts by protocol, agent connected-vs-configured
  counts, last search-indexing sweep time + overdue flag, and APScheduler job
  health, alongside app version and uptime. Stays separate from the existing
  fast, unauthenticated `GET /healthz` liveness check. See
  `docs/monitoring.md` for the response shape and a Zabbix HTTP-agent-item +
  JSONPath integration guide.
- Search now matches file paths and source names/hosts, not just line
  content. A query matching a file's path (but none of its lines) surfaces
  that file as a "filename match" hit; a query matching a source's name or
  host surfaces the source itself in a new "Sources matching" section,
  distinct from content hits — always current since it's a live filter over
  RBAC-visible source metadata, not part of the lagging background index.
  `search_index_fts`'s `file_path` column is now indexed (previously
  storage-only); existing deployments self-migrate their FTS schema and
  re-index on the next sweep, handled automatically at startup.
- Viewer: a "Find All" results panel (`lib/find-in-document.ts`,
  `FindInDocumentPanel.svelte`) alongside the existing Ctrl+F inline
  search — lists every match in the currently open file (line number +
  highlighted snippet), click a result to jump straight to it. Supports
  match-case and regex modes, and caps at 5000 results (reporting
  "truncated") as a safety valve for pathologically match-heavy queries
  rather than a design goal. Pure client-side scan of the tab's own
  content, no new dependency.
- Phase 2 (push-agent) backend infrastructure: a new `agent` protocol for
  sources that can't be reached inbound over SSH/SMB/WinRM. The agent
  (a future Go binary — see ROADMAP.md) dials out and holds a persistent
  WebSocket open at `/agent/connect`, authenticated with a bearer
  enrollment token (`POST /sources/{id}/agent-token`, hashed at rest,
  returned in plaintext once); the backend then relays live `list`/`fetch`
  commands down that connection on demand, exactly as it would call any
  other connector directly — nothing is proactively synced or mirrored, so
  the always-fresh, nothing-persisted rule holds for agent-mode sources too.
  `AgentRegistry` (`app/agent_registry.py`) tracks live connections
  in-memory and bridges FastAPI's synchronous connector calls to the async
  WebSocket via `asyncio.run_coroutine_threadsafe`. `SourcePublic` now
  reports `agent_connected`/`agent_last_seen_at` for agent-protocol sources.
- Phase 2 (push-agent): `agent/`, a standalone Go binary for sources that
  can't be reached inbound over SSH/SMB/WinRM. It dials out to the server
  and holds a persistent WebSocket connection open (`gorilla/websocket`),
  authenticated with the bearer enrollment token issued by the backend;
  the server then relays live `list`/`fetch` commands down that connection
  on demand — nothing is proactively synced. Cross-compiles for
  linux/amd64, linux/arm64, and windows/amd64; a dedicated CI job builds,
  vets, tests, and cross-compiles it on every push.
- Phase 2 (push-agent) frontend: `SourceEditor.svelte` gains an `agent`
  protocol option — no host/credential fields, instead a one-time
  enrollment-token generator ("Generate token" / "Regenerate token", with
  `has_agent_token` newly reported by `SourcePublic`) for pasting into the
  agent's `PERCHTAIL_AGENT_TOKEN` config. `Sources.svelte` shows agent
  sources' live connection state (connected / never connected / last seen)
  instead of the manual reachability check the other protocols use.
- Phase 3: full-text search. A new opt-in per-source flag
  (`Source.search_indexing_enabled`, off by default) enables a background
  indexer (`app/search_index.py`, an APScheduler sweep) that walks a
  source's rule-visible files and stores short per-line snippets in a
  SQLite FTS5 index — a genuinely separate, lagging, approximate structure,
  not a reuse of the ephemeral viewing scratch space or a departure from
  the always-fresh live-browsing model. `GET /search?q=...` searches across
  every source the current user can view, highlighting matches (safely
  HTML-escaped before its `<mark>` tags are added — log content is
  untrusted, arbitrary text). Frontend: a new Search page with a
  query box and a ranked, clickable results list; clicking a result opens
  the file in the Viewer and jumps straight to the matched line
  (`CodeMirrorPane` gained a `scrollToLine` method for this). The source
  editor gains an "Include in full-text search" toggle.
- Source editor: the Customer and Folder pickers gain an inline "+ Create
  new…" option, so a new customer or folder no longer needs a detour
  through an admin page that doesn't exist yet — pick it, type a name, and
  it's created (`POST /customers` / `POST /folders`) and selected in
  place. The Folder picker shows the existing nested tree indented by
  depth, and a new folder defaults its parent to whatever folder was
  already selected, so nesting one inside another is a single click.
  Folders remain purely organizational — name + optional parent, never a
  host or protocol — the backend already supported unlimited nesting via
  `Folder.parent_folder_id`; this was a frontend-only gap.
- Connections home redesign (Guacamole-inspired dashboard layout, see
  ROADMAP.md). The Viewer's landing page (`#/viewer` with no source
  selected) is now a two-column layout — recent connections on the left,
  all connections on the right — replacing the previous flat list.
  "Recent" is powered by new `source.open` `AuditLog` events (logged on
  the first browse into a source, not every sub-directory expand) read
  back via `GET /sources/recent`, RBAC-rechecked at read time so a
  revoked grant can't leak a source through history. `GET /sources/{id}/download`
  now also logs `file.download` — CLAUDE.md's own stated audit minimum
  that had never actually been wired up. "All connections" gains a search
  box matching folder, customer, or host, case-insensitive
  (`lib/connection-filter.ts`, not the source's own name); `SourcePublic`
  now reports `customer_name`/`folder_name` so cards can show this without
  a separate lookup.
- System settings: a new `SystemSetting` key-value table backs
  deployment-wide feature toggles, admin-configurable from a new
  "System" tab under Settings (`GET`/`PATCH /system-settings`, gated by a
  new `manage_system_settings` global capability). Ships with one toggle,
  "Search" — disabling it hides the Search nav entry for every user and
  redirects away from the `/search` route itself, not just the link. Built
  generically enough that the planned audit-log viewer's own toggle
  (ROADMAP.md) can reuse the same mechanism once that page exists.
- Viewer: admin-configurable severity indicators, both globally and
  per-source, replacing the old hardcoded `[error]`/`[warn]`-token and
  bare-word regexes. A new `SeverityPattern` model (level, pattern,
  glob/regex + `re:` prefix convention same as `Rule`, enabled,
  highlight-line, include-in-navigation, nullable `source_id`) backs
  `GET`/`POST`/`PATCH`/`DELETE /severity-patterns` (global set, gated by
  `manage_system_settings`) and the same under
  `/sources/{id}/severity-patterns` (per-source override, gated by
  `manage_rules`), plus `GET /sources/{id}/severity-patterns/effective`
  the Viewer reads — a source's own patterns win outright when it has any,
  else the global default set, same "most specific wins" shape as grant
  resolution. Seeded with sensible defaults on first startup (error/
  fatal/critical/panic/exception/traceback, warn, info, debug). New
  "Settings → Severity Indicators" page for the global set and a matching
  section on the source editor for per-source overrides, both row-based.
  The Viewer gained "‹ problem" / "problem ›" toolbar buttons that step
  through lines matching a navigation-eligible pattern (configurable per
  pattern, not a fixed severity floor), wrapping at either end. Matching
  logic lives client-side (`lib/severity-highlighting.ts`, pure and
  unit-tested); `codemirror-theme.ts` now builds its highlighting
  `ViewPlugin`s from whatever pattern set is effective instead of a fixed
  regex.
- Viewer: a set of small, per-tab toolbar toggles/actions, all display-only
  (never write anything back to the file or source) — line-wrap, "show all
  characters" (reveals whitespace as `·`/`→` glyphs and flags CRLF line
  endings, detected from the raw fetched content since CodeMirror's own
  line-separator matching consumes the `\r` before it can be inspected),
  go-to-line (Ctrl/Cmd+G, same page-level interception as the existing
  Ctrl/Cmd+F), copy-selected-lines-with-line-numbers, a manual reload
  button (re-fetches the open tab's content in place and releases the
  previous scratch reference), and bookmarks (pure client-side per-tab
  state, next/previous navigation reusing the same wrap-around stepper
  severity "next/previous problem" navigation already used — extracted to
  `lib/line-cycle.ts` so both share one implementation).
- Viewer: per-file-type syntax highlighting. Language is picked from the
  open file's own extension (`lib/file-language.ts`) — `.json`, `.xml`/
  `.html`/`.htm`/`.svg`, and `.js`/`.mjs`/`.cjs`/`.jsx`/`.ts`/`.tsx` now
  render with real syntax coloring via `@codemirror/lang-json`/`lang-xml`/
  `lang-javascript`, previously installed but unused dependencies.
  Anything unrecognized (most log files) is unaffected.

### Changed
- Frontend: Sources, Roles, Users, and SSO settings are now consolidated
  under a single "Settings" top-nav entry (previously four separate top-nav
  links), with a shared sub-nav across all four pages. Routes gained a
  `/settings` prefix (e.g. `/settings/sources`, `/settings/roles/:id`); a
  bare `/settings` redirects to `/settings/sources`. No capability gating
  changed — Sources stays visible to any authenticated user, Roles/Users/SSO
  stay gated behind their existing `manage_roles`/`manage_users`/`manage_sso`
  checks, same as before.
- README: status section now reflects Phase 2 (push-agent) and Phase 3
  (full-text search) as complete, not just Phase 1/1b; the three
  `docs/images/` screenshots (Sources, Viewer, Role editor) are
  regenerated against the new Settings nav; the Quick start walkthrough's
  "Sources → New source" now reads "Settings → Sources → + Add source".
- README: status section was stale in the other direction — it still
  listed alerting and IdP group-claim-to-role auto-mapping as not built,
  when both shipped earlier in this same Unreleased section. Corrected,
  and added a line on the pre-1.0 security-hardening work that's actually
  done (login lockout, security headers, CI vulnerability scanning,
  credential-key rotation, the Sessions page) versus what's still open,
  and a mention of the new Playwright e2e suite alongside the existing
  `pytest`/`vitest` coverage.

### Fixed
- Viewer: Ctrl/Cmd+F opened the browser's own find bar instead of
  CodeMirror's in-file search. Root cause was twofold — clicking into the
  read-only pane never actually moved DOM focus there (it isn't
  `contentEditable`, so a plain click doesn't focus it, and CodeMirror's
  `basicSetup` only wires the search *keymap*, not the `search()`
  extension the panel needs), so the keystroke had nothing to catch it
  and fell through to the browser. Fixed by intercepting Ctrl/Cmd+F at
  the window level in `Viewer.svelte` while a tab is open and opening
  the panel directly via `openSearchPanel`, regardless of focus state.
- `vite.config.ts`'s dev-proxy prefix list was missing `/sso`, so the SSO
  settings page silently failed to load under `npm run dev` (production is
  unaffected — the API and built SPA are served from the same FastAPI
  process there, with no path-based proxy split). Found while verifying the
  Settings reorganization in a real browser.
- SMB connector, two compounding bugs, both only reachable with a real
  `smbclient`/`smbprotocol` connection (found via the new Playwright e2e
  suite's real SMB server, not the mocked unit tests): `register_session`
  was left at `smbclient`'s default `auth_protocol` (`"negotiate"`, which
  tries Kerberos before falling back to NTLM) — `credential_ref` only ever
  decrypts to a bare username/password, with no realm/domain/KDC field
  anywhere in the `Source` model, so a client with no Kerberos
  configuration at all could fail the entire SPNEGO negotiation outright
  (`pyspnego.exceptions.BadMechanismError: Unable to negotiate common
  mechanism`) instead of ever reaching the NTLM this app actually
  authenticates with; and, once that was fixed, a source on a non-default
  SMB port still failed every `scandir`/`open_file` call — `smbclient`
  resolves its *own* session for those via `get_smb_tree(path, port=445,
  ...)`, defaulting to 445 independently of whatever port
  `register_session` was called with, so it silently attempted an
  unauthenticated connection on the wrong port instead of reusing the
  already-authenticated session. Both `auth_protocol="ntlm"` and the
  actual configured port are now passed to every `smbclient` call, not
  just the initial `register_session`; and a third, same-shape bug behind
  those two — `list_directory`'s per-entry `info.stat()` call (to read a
  file's size) triggers its own fresh `smbclient` connection lookup that
  forwards none of `_connect_kwargs`' `port`/`auth_protocol`, so it lands
  on a different, uncredentialed session and fails the same SPNEGO
  negotiation all over again. Fixed by reading the size straight off
  `info.smb_info.end_of_file` — data `scandir()` already returned in the
  original listing — instead of making a second network call at all.

## [0.1.1] - 2026-07-30

Phase 1b (SSO) complete: OIDC single sign-on alongside local auth, with
auto-provisioned no-access accounts for first-time SSO logins and an SSO
settings admin page.

### Added
- Phase 1b (SSO): `OIDCProvider` (`app/auth/providers/oidc.py`) — the second
  `AuthProvider` alongside `LocalPasswordProvider`, covering the OIDC
  authorization-code flow (discovery, code exchange, ID-token signature
  verification against the IdP's live JWKS, and `iss`/`aud`/`exp`/`nonce`
  claim checks). Admin CRUD for `SSOProviderConfig` at `/sso/...` (gated by
  `manage_sso`), with a test-connection action mirroring sources' `/check`
  endpoint; `GET /auth/sso/status`, `GET /auth/sso/login`, and
  `GET /auth/sso/callback` drive the actual sign-in flow, using a
  Fernet-encrypted, self-contained `state` param rather than a server-side
  state table. A first-time SSO sign-in auto-provisions a user with a new
  builtin "No Access" role (`app.bootstrap.seed_no_access_role`) — an admin
  assigns the real role afterward, per CLAUDE.md. Local login is unaffected
  and keeps working alongside an enabled provider. Frontend: an SSO
  settings admin page (gated by `manage_sso`, nav-linked) to configure the
  provider and test its connection, and a "Sign in with {name}" button on
  the Login page when one is enabled.
- More unit tests: backend `app/audit.py` (`record_audit_event`'s field
  defaults and its documented "doesn't commit" contract) and
  `app/timeutils.py` (`utcnow`'s naive-datetime guarantee) were the only two
  app modules without a dedicated test file — both now have one. Frontend:
  extracted `memberPath` (the archive-member-path slicing logic that had been
  duplicated inline in `FolderTree.svelte` twice, plus once more inside
  `download-href.ts`) into `lib/tab-key.ts`, with tests, so the three call
  sites can't silently drift apart.
- README: real screenshots (Sources, Viewer, Role editor) under `docs/images/`,
  a short table of contents, and a fixed `git clone` command (was still the
  `<your-org>` placeholder). CONTRIBUTING.md gets the same clone-URL fix plus
  frontend dev setup (`npm install`/`npm run dev`/`npm run check`/`npm test`),
  which had been missing entirely even though the frontend has been a core
  part of the stack since M7.

### Fixed
- `backend/requirements.txt` was missing `httpx`, which
  `app/auth/providers/oidc.py` imports unconditionally at module level
  (via `app/api/auth.py` → `app/main.py`) regardless of whether SSO is
  configured. It was only listed in `requirements-dev.txt`, so tests never
  caught it — a container built from `requirements.txt` alone (the
  production Docker image) crashed on every boot with
  `ModuleNotFoundError: No module named 'httpx'` right after migrations
  ran. Moved `httpx` into `requirements.txt` as a real runtime dependency.
- `RoleEditor.svelte`'s bare `form { flex-direction: column }` rule (meant for
  the role-details form) also matched the page's second `<form>` — the
  "add grant" row — flipping its flex main axis from row to column. Combined
  with `.add-grant select { flex: 0 0 160px }` (a width in the intended row
  layout), the flipped axis turned that into a *height*, rendering both scope
  dropdowns as ~160px-tall boxes instead of a normal single-line row. Found
  while capturing README screenshots. Scoped the rule to `form.card` (the
  role-details form's actual class) so it can't leak onto other forms in the
  same component.

## [0.1.0] - 2026-07-29

Phase 1 (MVP) complete: agentless live browsing over SSH/SFTP, SMB, and WinRM;
ephemeral fetch (nothing mirrored); rule-scoped RBAC (customers, folders,
roles, grants, local auth, audit log); the built-in log viewer; and the full
admin/viewer UI (Vite + Svelte), packaged as a single Docker image.

### Added
- Project scaffold (M0): FastAPI app under `backend/`, `structlog` JSON logging
  with request-id middleware and gzip-rotated daily log files, SQLite +
  SQLModel wired up with Alembic migrations, `ruff`/`black`/`pytest` tooling,
  CI workflow, and a `docker-compose`/`Dockerfile` skeleton.
- Data model (M1): `Customer`, `Source`, `Rule`, and the auth models `Role`,
  `RoleGrant`, `User`, `SSOProviderConfig`, `AuditLog`, plus an
  `app/crypto.py` helper (Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`) so
  `Source.credential_ref` and `SSOProviderConfig.config` have a real path to
  encryption-at-rest from day one.
- RBAC & local auth (M2): grant-resolution logic and a FastAPI permission
  dependency (`auth/rbac.py`), `LocalPasswordProvider` (argon2id, forced
  password change on admin-created accounts), and `AuditLog` writes wired
  into login and role/grant creation, each paired with a structured INFO log
  line.
- `api/auth.py`: login/logout/me/change-password endpoints backed by
  server-side sessions (hashed opaque token in an httpOnly, SameSite=strict
  cookie, stored in a new `AuthSession` table) rather than a stateless JWT,
  so revoking access is immediate.
- Rule engine (M3): `rules.py` implements CLAUDE.md's matching semantics —
  glob (default, path-separator-aware `**`/`*`/`?`, not stdlib fnmatch) or
  regex (`re:` prefix) patterns, last-match-wins evaluated by rule order,
  and zero rules matching nothing.
- SSH/SFTP connector and ephemeral scratch (M4): `collectors/ssh.py`
  (paramiko-based live listing and fetch-on-open), `scratch.py`
  (refcounted purge, idle-sweep backstop, size-guard eviction, both wired
  as APScheduler jobs), `archives.py` (`.zip`/`.tar.gz` as virtual folders,
  transparent `.gz` decompression), and `api/archive.py`
  (browse/open/close/download, gated by the M2 permission dependency and
  independently by the rule engine on every specific path).
- Remaining connectors (M5): `collectors/smb.py` (`smbprotocol`),
  `collectors/winrm.py` (`pywinrm`, base64-through-PowerShell for file
  content since WinRM has no native bulk transfer), and `collectors/
  local.py` (reads directly off disk). `api/archive.py` now skips the
  scratch store entirely for local, plain files — served straight from
  their real path — while still using it for a local `.gz` or archive
  member, since decompressing/extracting produces new derived bytes that
  have to live somewhere.
- Nested folders for source organization (M4.5): a `Folder` model
  (self-referential, nested under a `Customer`) and `RoleGrant.scope_type`
  gains `folder`, generalizing the existing source-beats-customer grant
  resolution to N levels — source → folder chain (nearest first) →
  customer, most specific wins.
- Built-in log viewer (M6): `app/bootstrap.py` seeds a system source on
  first startup pointed at `LOG_DIR`, so the app's own rotated logs are
  browsable through the exact same live-browsing path as customer sources
  — gated purely by `is_super_admin` (already enforced since M2).
- Admin CRUD API (M7, part 1): `api/customers.py`, `api/folders.py`,
  `api/sources.py`, `api/rules.py`, `api/roles.py`, `api/users.py` — the
  full backend surface the admin/viewer UI needs. Sources gain a
  `POST /sources/{id}/check` on-demand connection check, replacing the
  pre-pivot "run-now"/"run history" wording in CLAUDE.md's original Web UI
  section (there's no `Run` model and nothing runs on a schedule anymore —
  see ROADMAP.md's M7 notes). Rules gain a gitignore-style raw-text
  paste-mode endpoint (`PUT .../rules/raw`, leading `!` negates a line) in
  addition to the row-based CRUD + reorder. Roles gain a duplicate-role
  action (clones grants too) and grant CRUD, with a privilege-escalation
  guard: only an existing super-admin can create or edit a super-admin
  role. Users gain admin-driven create/update/reset-password/deactivate
  (deactivate is a soft delete, per CLAUDE.md's Security notes). Customer/
  folder management is treated as an admin surface gated by the
  `create_source` global capability rather than per-scope grants, per
  CLAUDE.md's "its own small admin surface" wording. `api/archive.py` also
  gains `GET .../download-zip` (zips an entire folder, fetching each
  contained file fresh) so the Viewer can offer "download a zipped folder,"
  not just a single file. `auth/me` (and login/change-password) now also
  returns `is_super_admin`/`global_capabilities` so the frontend can gate
  nav without a second round-trip.
- Admin & viewer UI (M7, part 2): a Vite + Svelte + TypeScript SPA under
  `frontend/` — the frontend stack CLAUDE.md never named (see ROADMAP.md's
  M7 notes for why Svelte). Login/change-password, a sources list with
  inline connection-check and a create/edit form, a rule editor (row-based
  + gitignore-style raw-text paste mode, toggled per source), a lazy-loaded
  folder tree (children fetched only on expand, one level of `.zip`/
  `.tar.gz` expansion) feeding a CodeMirror 6 tabbed viewer pane (in-file
  search via CodeMirror's built-in search panel, single-file and
  zipped-folder download), and Roles/Users admin pages (grant CRUD,
  duplicate-role, reset-password, deactivate). CI gains a `frontend` job
  (`svelte-check` + `vite build`) alongside the existing backend job.
- Phase 1 exit (M8): multi-stage `Dockerfile` (Node stage builds the SPA,
  copied into the Python runtime image), `docker-entrypoint.sh` (runs
  `alembic upgrade head` before `uvicorn`), and `app.main` mounts the built
  frontend as static files at `/` — works unmodified with the frontend's
  hash-based routing, since the browser only ever requests `/`.
  `app.bootstrap.seed_initial_super_admin` creates a break-glass super-admin
  with a randomly generated, once-logged password on first startup (a fresh
  deployment previously had no way to create its first user at all).
  README's Quick start is now real, tested steps rather than a placeholder.
- Visual redesign and logo: a dark theme (CSS custom properties in
  `app.css` — background/border/text/accent tokens, per-protocol and
  per-status badge colors) applied across every page, plus a fish/perch mark
  (`frontend/public/favicon.svg`) per CLAUDE.md's mascot note — used as the
  favicon, the nav-bar brand, and the README header. The full brand asset
  pack (icon at multiple raster sizes, mark-only light/dark variants, and an
  icon+wordmark lockup) lives under `frontend/public/brand/` for later
  branding work. The Viewer's CodeMirror pane gains a matching dark theme
  and lightweight log-level highlighting (`[info]`/`[warn]`/`[error]` tokens
  colored, error lines tinted) so a log reads the same way `grep -i error`
  would highlight it.
- Frontend unit testing (`vitest`, added alongside this redesign): pulled
  the non-CSS logic that redesign touched or introduced out into small,
  pure, testable modules — `lib/codemirror-theme.ts`'s log-level token/line
  detection, `lib/tab-key.ts` (the tab/tree-highlight key format shared
  between Viewer and FolderTree, previously duplicated inline in both),
  `lib/download-href.ts` (FolderTree's per-entry download-URL branching),
  and `lib/rule-format.ts` (RuleEditor's raw-text line rendering/parsing) —
  plus tests for the existing `lib/api.ts` request wrapper, `lib/auth.ts`'s
  capability check and login/logout/session-refresh flows, and `lib/hash.ts`'s
  hashchange-driven route store. CI's `frontend` job runs `npm test` before
  the build step.

### Fixed
- Archive member listing/opening didn't check the rule chain against the
  specific member requested — browsing a `.zip`/`.tar.gz` listed every
  member unconditionally, and `/open`/`/download` didn't re-check a
  requested `member` against the rules at all, so a rule scoped to show
  only the archive itself leaked its full contents. Both paths now check
  `is_visible` on the member's combined virtual path.
- `is_safe_relative_path` only split on `/`, so a backslash (`..\\`) path
  traversal segment passed straight through — a real, exploitable gap for
  SMB/WinRM sources specifically, since those connectors join a relative
  path onto a Windows `base_path` with backslashes. Now splits on both
  `/` and `\\`, and rejects a bare `:` to rule out Windows drive-letter
  absolute paths.

What comes next is in [ROADMAP.md](ROADMAP.md).

[Unreleased]: https://github.com/quaresma870/perchtail/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/quaresma870/perchtail/compare/v0.6.4...v0.7.0
[0.7.0-rc.4]: https://github.com/quaresma870/perchtail/compare/v0.7.0-rc.3...v0.7.0-rc.4
[0.7.0-rc.3]: https://github.com/quaresma870/perchtail/compare/v0.7.0-rc.2...v0.7.0-rc.3
[0.7.0-rc.2]: https://github.com/quaresma870/perchtail/compare/v0.6.4...v0.7.0-rc.2
[0.6.4]: https://github.com/quaresma870/perchtail/compare/v0.6.3...v0.6.4
[0.6.4-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.6.3...v0.6.4-rc.1
[0.6.3]: https://github.com/quaresma870/perchtail/compare/v0.6.2...v0.6.3
[0.6.3-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.6.2...v0.6.3-rc.1
[0.6.2]: https://github.com/quaresma870/perchtail/compare/v0.6.1...v0.6.2
[0.6.2-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.6.1...v0.6.2-rc.1
[0.6.1]: https://github.com/quaresma870/perchtail/compare/v0.6.0...v0.6.1
[0.6.1-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.6.0...v0.6.1-rc.1
[0.6.0]: https://github.com/quaresma870/perchtail/compare/v0.5.3...v0.6.0
[0.6.0-rc.3]: https://github.com/quaresma870/perchtail/compare/v0.6.0-rc.2...v0.6.0-rc.3
[0.6.0-rc.2]: https://github.com/quaresma870/perchtail/compare/v0.6.0-rc.1...v0.6.0-rc.2
[0.6.0-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.5.3...v0.6.0-rc.1
[0.5.3]: https://github.com/quaresma870/perchtail/compare/v0.5.2...v0.5.3
[0.5.3-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.5.2...v0.5.3-rc.1
[0.5.2]: https://github.com/quaresma870/perchtail/compare/v0.5.0...v0.5.2
[0.5.2-rc.2]: https://github.com/quaresma870/perchtail/compare/v0.5.2-rc.1...v0.5.2-rc.2
[0.5.2-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.5.1-rc.1...v0.5.2-rc.1
[0.5.1-rc.1]: https://github.com/quaresma870/perchtail/compare/v0.5.0...v0.5.1-rc.1
[0.5.0]: https://github.com/quaresma870/perchtail/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/quaresma870/perchtail/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/quaresma870/perchtail/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/quaresma870/perchtail/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/quaresma870/perchtail/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/quaresma870/perchtail/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/quaresma870/perchtail/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/quaresma870/perchtail/releases/tag/v0.1.0
