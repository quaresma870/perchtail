# Configuration reference

This guide lists every setting of a PerchTail server and its push agents, with its default, accepted values, effect and where to change it, for the administrators who deploy and run PerchTail.

Related guides: [admin-guide.md](admin-guide.md) (day-to-day administration), [user-guide.md](user-guide.md), [agent-service.md](agent-service.md) (running the push agent as a service), [troubleshooting.md](troubleshooting.md), [upgrade.md](upgrade.md), [backup-and-restore.md](backup-and-restore.md), [monitoring.md](monitoring.md), [install-from-source.md](install-from-source.md), [source-setup.md](source-setup.md), [sso-setup.md](sso-setup.md), [credential-key-rotation.md](credential-key-rotation.md) and [high-availability.md](high-availability.md).

PerchTail has two kinds of settings:

- **Environment variables**, read once when the process starts. They cover what the operator of the host decides: paths, keys, limits that protect the host, network trust. Changing one needs a restart.
- **System settings**, stored in the database and changed in the web interface under **Settings → System** and **Settings → Agents**. They take effect without a restart.

**Contents:** [How environment variables are read](#how-environment-variables-are-read) · [Storage and paths](#storage-and-paths) · [Keys, security and sessions](#keys-security-and-sessions) · [Sources and connectors](#sources-and-connectors) · [Scratch and limits](#scratch-and-limits) · [Logging](#logging) · [Audit log](#audit-log) · [Monitoring and reverse proxy](#monitoring-and-reverse-proxy) · [Backups](#backups) · [Updates and agent upgrades](#updates-and-agent-upgrades) · [Other variables](#other-variables) · [Push agent](#push-agent) · [System settings](#system-settings) · [Settings → Agents](#settings--agents)

## How environment variables are read

PerchTail reads its settings with pydantic-settings (`backend/app/config.py`, class `Settings`). Each setting's variable is the field name in upper case, with no prefix.

- **Where they come from.** The process environment, then a `.env` file in the working directory; a variable in the environment wins over the same one in `.env`. With Docker Compose, `docker-compose.yml` loads the `.env` next to it into the container's environment (`env_file`). For an install from source, the systemd unit loads `/etc/perchtail/perchtail.env` ([install-from-source.md](install-from-source.md)). [`.env.example`](../.env.example) is the template for both.
- **Names** are matched without regard to case. A variable PerchTail doesn't know is ignored without a warning, so check the spelling of anything you set.
- **Values.** Booleans accept `true` or `false` (also `1`/`0`, `yes`/`no`, `on`/`off`). Sizes in MB or GB accept decimals. A value of the wrong type, or outside a fixed list, stops PerchTail at startup with an error that names the variable.
- **Applying a change.** Restart PerchTail. With Docker Compose, run `docker compose up -d`: it recreates the container with the new `.env`, while `docker compose restart` keeps the old environment. For an install from source, `sudo systemctl restart perchtail`.
- **The Docker image sets** `DATA_DIR=/data/db`, `LOG_DIR=/data/logs`, `SCRATCH_DIR=/data/scratch`, `BACKUP_DIR=/data/backups`, `AGENT_BINARIES_DIR=/app/agent-binaries` and `PERCHTAIL_INSTALL=docker` (`Dockerfile`). Leave these unset in `.env` unless you mount a volume somewhere else.
- **Variables read by other programs** (`HTTPS_PROXY`, `TZ`, `SMB_EXPERIMENTAL_TRANSPORT_RECEIVE_TIMEOUT`) must be in the process environment. Docker Compose and the systemd unit put them there; a `.env` file read only by PerchTail's own settings, as when you start `uvicorn` by hand in `backend/`, doesn't.

The defaults below are the code's defaults. Relative paths are relative to the working directory: `backend/` for a checkout, `/opt/perchtail/backend` with the install guide's paths.

## Storage and paths

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `DATA_DIR` | `./data/db` (`/data/db` in the image) | A directory path | Holds what must be backed up: the database, the credential salt and SSH `known_hosts`, unless the three variables below put one elsewhere. Also holds the instance lock `.perchtail.lock`: only one PerchTail process, or one `alembic upgrade`, may use a `DATA_DIR` at a time. PerchTail limits it to its own user (`0700`). Keep it on a local disk: SQLite's WAL mode doesn't work over a network share. |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/perchtail.db` | A SQLite file URL: `sqlite:///` followed by the path (four slashes for an absolute path, `sqlite:////var/lib/perchtail/perchtail.db`) | The database. Set it only to keep the database outside `DATA_DIR`. `python -m app.backup` supports a SQLite file only. |
| `CREDENTIAL_SALT_PATH` | `<DATA_DIR>/credential_salt` | A file path | The per-install random salt (16 bytes) that, with `CREDENTIAL_ENCRYPTION_KEY`, derives the keys for stored credentials, SSO secrets, MFA secrets and the audit log's hash chain. Created on first use, readable by PerchTail's user only. Losing it makes all of those unreadable: see [backup-and-restore.md](backup-and-restore.md#what-the-keys-and-the-salt-protect). |
| `SSH_KNOWN_HOSTS_PATH` | `<DATA_DIR>/ssh_known_hosts` | A file path | The SSH host keys PerchTail has pinned. Losing it resets host-key pinning: with `SSH_HOST_KEY_POLICY=tofu` every server's key is trusted again on its next connection. |
| `LOG_DIR` | `./data/logs` (`/data/logs` in the image) | A directory path | Where PerchTail writes its own log, `perchtail.log`, and its rotated archives (see [Logging](#logging)). The built-in application-log source shows this directory; it is created at the first start with the `LOG_DIR` of that start, and keeps that path if `LOG_DIR` changes later. |
| `SCRATCH_DIR` | `./data/scratch` (`/data/scratch` in the image) | A directory path | Short-lived copies of files fetched from remote sources, folder ZIPs being built and remote copies of archives. Emptied at startup. Never a filesystem root, and never a directory that holds the database or the logs: PerchTail then refuses to clear it (`scratch.leftovers_not_cleared`). Put it on its own disk where you can. |
| `BACKUP_DIR` | `backups` next to `DATA_DIR` (`./data/backups`; `/data/backups` in the image) | A directory path | Where `python -m app.backup` and the scheduled backup write encrypted backups. See [Backups](#backups). |
| `DERIVED_FROM_DATA_DIR` | (computed) | Not a setting | Internal: PerchTail records which of `DATABASE_URL`, `CREDENTIAL_SALT_PATH` and `SSH_KNOWN_HOSTS_PATH` were left to default, so the move of a layout from before 0.5.0 never touches a path you set. A value set in the environment is replaced at startup. |

## Keys, security and sessions

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | `changeme` | Any string; generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` | The root secret. With the salt, it encrypts source credentials, SSO provider settings and MFA secrets, keys the audit log's hash chain, and, unless `BACKUP_ENCRYPTION_KEY` is set, encrypts backups. PerchTail refuses to start while it is `changeme` (`startup.insecure_credential_encryption_key`). Changing it on an existing install needs a rotation, not just a new value: see [credential-key-rotation.md](credential-key-rotation.md). A key shorter than 32 bytes, or with less than 128 bits of estimated entropy, is a warning at startup (`startup.weak_credential_encryption_key`) and at the top of Settings → System; see `CREDENTIAL_KEY_REFUSE_WEAK`. |
| `CREDENTIAL_KEY_REFUSE_WEAK` | `false` | `true`, `false` | `true` refuses to start with a weak `CREDENTIAL_ENCRYPTION_KEY` instead of only warning. A key is weak when it is shorter than 32 bytes once decoded (a hex or base64 key counts the bytes it decodes to, any other key its UTF-8 bytes) or has less than 128 bits of estimated entropy (its length times the Shannon entropy of its characters, capped by its size compressed with zlib, so a repeated word or phrase doesn't pass). The keys the generator above makes always pass. Planned to become the default in 1.0; change a weak key with a rotation ([credential-key-rotation.md](credential-key-rotation.md)). |
| `API_DOCS` | `false` | `true`, `false` | `true` serves the interactive API documentation at `/docs` and `/redoc` and the OpenAPI schema at `/openapi.json`. Off, they answer 404: the HTTP API is internal to the web UI and may change before 1.0, and the schema lists every endpoint for whoever can reach the server. |
| `CREDENTIAL_SALT_ALLOW_REGENERATE` | `false` | `true`, `false` | Leave it `false`. When the salt file is missing and the database already holds encrypted data, PerchTail refuses to start (`startup.credential_salt_missing`) so that you can restore the salt. `true` lets it start with a new salt (`startup.credential_salt_regenerated`): every source credential, SSO secret and MFA secret must then be entered again, and older audit rows fail the integrity check. Set it back to `false` afterwards. |
| `SESSION_TTL_HOURS` | `12` | A whole number of hours | How long a sign-in lasts. The session expires that long after sign-in, whatever the activity, and the cookie has the same lifetime. |
| `SESSION_COOKIE_SECURE` | `true` | `true`, `false` | The `Secure` flag on the session cookie and the SSO sign-in cookie. Keep `true` behind HTTPS. Browsers drop a `Secure` cookie over plain HTTP, so sign-in fails without an error on `http://`; set `false` only for local tests over HTTP. |
| `LOGIN_MAX_ATTEMPTS` | `5` | A whole number | Failed sign-ins in a row against one username before that username is locked out. One client address is locked after the same number of failures, whatever the usernames: behind a NAT or a shared proxy, that many failures by anyone there lock that address for everyone for `LOGIN_LOCKOUT_SECONDS`, so behind a reverse proxy set `TRUSTED_PROXIES` to it. An active local super admin with MFA on is locked per username and address, so it can't be locked out from every address; without MFA on yet, it's locked per username like any other account. See [troubleshooting.md](troubleshooting.md#too-many-failed-login-attempts-try-again-later). |
| `LOGIN_LOCKOUT_SECONDS` | `300` | A whole number of seconds | How long the lockout lasts. The counts are kept in memory: a restart clears them. |
| `INITIAL_ADMIN_USERNAME` | `admin` | A username | The break-glass super admin PerchTail creates on its first start, only when the database has no users. Its password is generated and written to `DATA_DIR/initial-admin-password`, readable only by PerchTail's user; the log only says where the file is (`initial_super_admin.seeded`, field `password_file`). The password must be changed at first sign-in, and the file is deleted when it is. Changing the variable later has no effect. |
| `SSO_ALLOW_HTTP_LOCALHOST` | `false` | `true`, `false` | The OIDC issuer and the endpoints its discovery document names must be `https://`. `true` also accepts `http://` on `localhost`, `127.0.0.1` and `::1`, for local development with a test identity provider. Never on another host. |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | The external URL users reach PerchTail at, without a trailing slash | Builds the OIDC redirect URI, `<PUBLIC_BASE_URL>/auth/sso/callback`, and the address the browser returns to after an SSO sign-in. Must match the redirect URI registered at the identity provider ([sso-setup.md](sso-setup.md)). Only used for SSO. |

## Sources and connectors

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `LOCAL_SOURCE_ROOTS` | empty | Comma-separated absolute directories, for example `/mnt/host-logs` | The directories "Local disk" sources may read, with everything beneath them. Empty turns local-disk sources off; only the built-in application-log source reads local disk then. Checked on every access, after following symlinks, so narrowing it also stops existing sources outside it. Set only here, never from the web interface, so that a web admin can't point a source at the server's own database or keys. See [source-setup.md](source-setup.md#local-disk-directories-on-the-perchtail-host-itself). |
| `SSH_HOST_KEY_POLICY` | `tofu` | `tofu`, `strict` | `tofu` pins a server's SSH host key on first connection and logs its fingerprint; `strict` refuses a server whose key isn't already in `SSH_KNOWN_HOSTS_PATH`. A changed key is refused either way. See [source-setup.md](source-setup.md#6-host-key-pinning). |
| `REMOTE_READ_TIMEOUT_SECONDS` | `60` | A whole number of seconds | How long a connected SSH/SFTP or SMB source may stay silent before the call fails, so a server that stalls after login can't hold a worker. For SSH it bounds the banner, authentication, channel and SFTP reads; for SMB the connection and the server's answers. A background source check is given up after twice this plus 10 seconds. |
| `MAX_FETCH_MB` | `2048` | A number of MB | The largest single file a remote source fetches, and the largest total a folder ZIP may collect. Over it, the request is refused (HTTP 413). |
| `MAX_DECOMPRESSED_MB` | `2048` | A number of MB | The largest size one `.gz` file, or one member of a `.zip` or `.tar.gz`, may decompress to before opening it is refused (HTTP 413). A guard against archive bombs. |

## Scratch and limits

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `SCRATCH_MAX_GB` | `5.0` | A number of GiB | The most scratch may hold. Over it, the files nobody has open are evicted (`scratch.size_guard_eviction`); when that isn't enough, a new open, download, folder ZIP or archive listing is refused (HTTP 507, `scratch.full`). |
| `SCRATCH_IDLE_SECONDS` | `300` | A whole number of seconds | A file in scratch untouched for this long is deleted, even if a client never closed it. |
| `SCRATCH_SWEEP_INTERVAL_SECONDS` | `60` | A whole number of seconds | How often the idle sweep and the size guard run. |
| `SCRATCH_MIN_FREE_GB` | `1.0` | A number of GiB; `0` with `SCRATCH_MIN_FREE_PERCENT=0` turns the check off | Free space to keep on the filesystem that holds `SCRATCH_DIR`. A new fetch is refused (HTTP 507, `scratch.disk_low`) when free space would drop below the larger of this and `SCRATCH_MIN_FREE_PERCENT`. |
| `SCRATCH_MIN_FREE_PERCENT` | `5.0` | A percentage of the filesystem | The same floor, as a share of the filesystem's size. |
| `SHUTDOWN_TIMEOUT_SECONDS` | `20` | A whole number of seconds | How long a stop waits for running background jobs and the file-name index and source-check workers to finish. Keep the container's stop grace period above it: `docker-compose.yml` sets `stop_grace_period: 30s`. |

How much work runs at once (listings, downloads, folder ZIPs) is a system setting, in the **Limits** card: see [System settings](#system-settings).

## Logging

PerchTail logs JSON lines to standard output and to `LOG_DIR/perchtail.log`. The active file rotates at `LOG_MAX_FILE_MB` or at midnight, whichever comes first; each rotated file is compressed as `perchtail.log.<date>.<n>.gz`.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (any case); anything else counts as `INFO` | The lowest level logged. At `INFO` and above, the SMB and SSH libraries (`smbprotocol`, `spnego`, `paramiko`) log only warnings and errors, since at `INFO` they log every SMB message and SSH session. `DEBUG` can write gigabytes a day, mostly SMB and SSH packet dumps. |
| `LOG_RETENTION_DAYS` | `30` | A whole number of days; `0` keeps archives regardless of age | Rotated archives older than this are deleted. |
| `LOG_MAX_FILE_MB` | `100` | A number of MB; `0` rotates at midnight only | The size at which the active file rotates. |
| `LOG_MAX_TOTAL_MB` | `2048` | A number of MB; `0` for no limit | The most all logs together may take. The oldest archives are deleted first; when this deletes archives `LOG_RETENTION_DAYS` would still have kept, PerchTail logs `logs.retention.size_limit` and reports it in `/monitoring/health`. |
| `LOG_QUIET_THIRD_PARTY` | `false` | `true`, `false` | With `LOG_LEVEL=DEBUG`, `true` keeps PerchTail's own DEBUG lines and caps the protocol libraries (SMB, SSH and the like) at `INFO`. No effect at `INFO` and above. |

## Audit log

What the audit log keeps, and whether its page is shown, are system settings (the **Audit log** card). These two variables set how often its background jobs run.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `AUDIT_PURGE_INTERVAL_SECONDS` | `86400` (a day) | A whole number of seconds | How often the purge job deletes entries older than `audit_retention_days`. A job that missed its time while PerchTail was stopped runs after the next start. |
| `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` | `1` | A whole number of days | How often the job that verifies the audit log's hash chain runs. A break logs `audit_integrity.chain_broken` and is recorded as `audit.integrity_check_failed`. It also catches up after a restart. |

## Monitoring and reverse proxy

Who may call `/healthz`, `/readyz`, `/monitoring/health` and `/metrics` is explained in [monitoring.md](monitoring.md#who-may-call-them). The monitoring token is generated in **Settings → System**, card **Monitoring**.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `PROBE_ALLOWED_NETWORKS` | empty | Comma-separated IP addresses or CIDR blocks | Callers that may use `/healthz` and `/readyz` without the monitoring token, such as a load balancer or a kubelet that can't send a header. A local caller without proxy headers (the Docker `HEALTHCHECK`) is always allowed. A malformed entry stops PerchTail at startup, naming it. |
| `MONITORING_ALLOWED_NETWORKS` | empty | Comma-separated IP addresses or CIDR blocks | When set, `/monitoring/health` and `/metrics` also require the caller to be in one of these networks, on top of the token; others get `403`. A malformed entry stops PerchTail at startup. |
| `TRUSTED_PROXIES` | `127.0.0.0/8, ::1` (loopback only) | Comma-separated IP addresses or CIDR blocks | The reverse proxies whose `X-Forwarded-For` PerchTail believes. On a request from one of them, the client address is the first address in `X-Forwarded-For`, read from the right, that isn't a trusted proxy; on a request from anywhere else, it is the TCP peer and `X-Forwarded-For` is ignored. That address is what the audit log records and what `PROBE_ALLOWED_NETWORKS` and `MONITORING_ALLOWED_NETWORKS` check. Behind a reverse proxy in another container or on another machine, set it to the proxy's address; never a range that holds clients, which could then name their own address. A malformed entry stops PerchTail at startup. See [audit-events.md](audit-events.md#client-address). |
| `FORWARDED_ALLOW_IPS` | none | Not used | Has no effect on PerchTail. The Docker image and the systemd unit in [install-from-source.md](install-from-source.md) run uvicorn with `--no-proxy-headers`, so uvicorn never reads proxy headers and PerchTail resolves the client address itself, from `TRUSTED_PROXIES`. An old `.env` that sets it still starts; move its addresses to `TRUSTED_PROXIES`. |

## Backups

See [backup-and-restore.md](backup-and-restore.md) for how backups are made, checked and restored. The interval of scheduled backups is a system setting (`backup_interval_hours`).

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `BACKUP_DIR` | `backups` next to `DATA_DIR` (`/data/backups` in the image) | A directory path | Where backups are written, readable by PerchTail's user only. Listed (not downloadable) in **Settings → System**, card **Backups**. Copy them off the host. |
| `BACKUP_KEEP` | `7` | A whole number; below 1 counts as 1 | How many manual and scheduled backups are kept; the oldest is deleted when a new one is made. The last 3 `pre-upgrade` backups are kept on their own, whatever this says. |
| `BACKUP_ENCRYPTION_KEY` | empty | Any string; generate it like `CREDENTIAL_ENCRYPTION_KEY` | When set, new backups are encrypted with a key derived from it instead of from `CREDENTIAL_ENCRYPTION_KEY`. A backup made with it opens only with it. Keep it apart from the backups and from `CREDENTIAL_ENCRYPTION_KEY`. |

## Updates and agent upgrades

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `UPDATE_CHECK` | `true` | `true`, `false` | `true` allows the daily release check (one request to `api.github.com`, nothing about the install sent), which the **Updates** switch can still turn off. `false` means PerchTail never makes that request, and the switch is disabled. Use `false` on a network without internet access. |
| `PERCHTAIL_INSTALL` | empty (`docker` in the image) | `docker`, or anything else | Which upgrade steps the **Updates** card shows: `docker` (any case) shows the Docker steps, anything else the steps for an install from source. See [upgrade.md](upgrade.md#the-updates-card). |
| `AGENT_BINARIES_DIR` | empty (`/app/agent-binaries` in the image) | A directory path | The folder holding the signed push-agent binaries of this server's version (`perchtail-agent-<version>-<os>-<arch>`, `SHA256SUMS`, `SHA256SUMS.sig`), served to connected agents that are upgraded from the portal. Empty, or a folder without them for this version, means agents download the release from GitHub instead; **Settings → Agents** says so. Release images carry the binaries; an image built from a checkout doesn't. |

## Other variables

These aren't fields of `Settings`, but PerchTail, its commands or its libraries read them.

| Variable | Read by | Effect |
|---|---|---|
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | The release check (httpx) | The update check goes through the proxy these name. A proxy password in the URL is masked in the error the **Updates** card shows. |
| `TZ` | The agent upgrade window | The time zone the automatic agent upgrade window uses; without it, the system's (UTC in the Docker image). **Settings → Agents** shows the zone in use. |
| `SMB_EXPERIMENTAL_TRANSPORT_RECEIVE_TIMEOUT` | smbprotocol | Seconds without a word from an SMB server before the library checks the connection and then fails pending requests. PerchTail sets it to `REMOTE_READ_TIMEOUT_SECONDS` unless you set it yourself. |
| `NEW_CREDENTIAL_ENCRYPTION_KEY` | `python -m app.rotate_credential_key` | The key to rotate to. Without it, the command asks for it. Pass it to that command only, never in `.env`. See [credential-key-rotation.md](credential-key-rotation.md). |
| `PERCHTAIL_IMPORT_*` | YAML import (**Settings → Import / export**) | A credential in an import file may be written as `${env:PERCHTAIL_IMPORT_<NAME>}`; the server reads that variable at import time. Only names starting with `PERCHTAIL_IMPORT_` are read. See [bulk-import.md](bulk-import.md#credentials). |
| `SEARCH_INDEX_*` | Nothing | The content index was removed in 0.7.0; these variables are ignored, and an old `.env` that sets them still starts. |

**Test only.** `PERCHTAIL_TEST_PATCH_MODULE` (loads a test double at startup, used by the end-to-end test server, and only together with `PERCHTAIL_TEST_MODE=true`; set without it, PerchTail refuses to start, and the Docker image refuses either one), `E2E_ADMIN_USERNAME` and `E2E_ADMIN_PASSWORD` (the account `app.seed_e2e_admin` creates for the end-to-end tests) and `SCHEMATHESIS_MAX_EXAMPLES` (the property-based API tests) are for the test suites. Never set them on a deployment.

The HTTP API behind the web interface is internal and may change before 1.0; only the operator endpoints `/healthz`, `/readyz`, `/monitoring/health` and `/metrics` are meant for other tools.

## Push agent

The push agent (`agent/`) reads these from its own environment, on the host it runs on ([agent/README.md](../agent/README.md), [agent-service.md](agent-service.md)). They don't belong in the server's `.env`.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `PERCHTAIL_SERVER_URL` | required | `wss://<host>/agent/connect` | The server's agent endpoint. The agent refuses a `ws://` URL unless `PERCHTAIL_ALLOW_INSECURE` is true, and any other scheme. |
| `PERCHTAIL_AGENT_TOKEN` | required | The enrollment token shown once when the source's agent token is generated | Authenticates the agent. Keep the file that holds it readable by root only. |
| `PERCHTAIL_BASE_PATH` | required | A local directory | The root the agent may read; nothing outside it is served. |
| `PERCHTAIL_ALLOW_INSECURE` | unset (false) | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` | Allows a plain `ws://` server URL. The token and every fetched file then travel in clear text: for tests or a fully trusted network only. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` | `false` refuses every upgrade from the portal, whatever the server's policy. Any other value stops the agent at startup, so a typo can't leave upgrades on. |

The agent uses Go's standard proxy variables (`HTTPS_PROXY`, `NO_PROXY`) for its connection to the server and for downloads from GitHub.

## System settings

**Settings → System** is shown to super admins and to roles with the `manage_system_settings` global capability. The settings are grouped in cards, named below as on screen. Each change is saved at once (cards with several fields have their own save button), recorded in the audit log as `system_settings.update`, and applies without a restart. A setting nobody changed has its default from the code; the values are stored in the database (table `system_setting`), so they are part of every backup, but not of the YAML export.

The key is the setting's name in the code (`backend/app/system_settings.py`), as it appears in the `system_settings.update` audit entries.

### Explorer card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `connections_view_default` | **Connections view** | `list` | `list` (List), `tree` (Tree) | How the viewer's explorer lists sources for everyone who hasn't chosen: a flat list, or a tree by customer and folder. Open browsers pick it up on their next page change, within 30 seconds. |
| `connections_view_switchable` | **Let users switch between list and tree** | on | on, off | On: each user can switch, and the choice follows them. Off: the switch is hidden and everyone sees the default view. |
| `explorer_sort_default` | **File order** | `name_asc` (Name A→Z) | `name_asc` (Name A→Z), `name_desc` (Name Z→A), `mtime_desc` (Newest first), `mtime_asc` (Oldest first), `size_desc` (Largest first) | How the explorer orders files in a folder, for everyone. Folders always come first. |
| `explorer_refresh_seconds` | **Refresh open folders** | `30` | `0` (off), or 5 to 3600 seconds | How often the explorer lists the folders people have open again, so rotated files show up. Only the list of files, never their content, and not while the browser tab is hidden. |
| `tail_interval_seconds` | **Tail interval** | `2` | `1`, `2`, `5`, `10` seconds | How often Tail checks an open file for new lines, for everyone. A shorter interval is more live and asks the source more often. |

### Monitoring card

No stored setting: **Generate token** (or **Regenerate token**) creates the bearer token for `/monitoring/health` and `/metrics`, shown once. Regenerating it invalidates the previous one. See [monitoring.md](monitoring.md#generating-a-monitoring-token).

### Sources card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `source_check_minutes` | **Check sources in the background** | `10` | `0` (off), or up to 1440 minutes | How often PerchTail checks that each source answers, one connection per source, so the explorer shows which ones are down. Push-agent sources show their live connection instead. A source that fails three times in a row backs off for up to 30 minutes. |
| `name_index_enabled` | **Index file names** | on | on, off | The file-name index: PerchTail lists every source's rule-visible files in the background and keeps their names, sizes and times, never their content. The explorer's search across sources and live alerts use it. Off stops the listings; what was indexed stays until a source is changed. Each source can also be left out in its own settings. |
| `name_index_minutes` | **List each source's files every** | `15` | 5 to 1440 minutes | A source is listed again once its index is older than this, a few sources at a time. |
| `credential_max_age_days` | **Warn when a credential is older than** | `90` | `0` (off), or up to 3650 days | **Settings → Sources** marks the sources whose credential was set longer ago than this. |
| `source_max_roots` | **Folders per source** | `20` | 1 to 100 | How many folders one source may read. `1` turns several folders per source off. Sources that already have more keep them, but can't add more. |

### Alerts card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `alerts_interval_seconds` | **Check alerts every** | `60` | 15 to 3600 seconds | How often a pass reads the lines appended to the files each alert watches. |

### Limits card

The limits are saved together with **Save limits**. A request over a limit is answered "busy" (HTTP 503 with `Retry-After: 5`) instead of waiting, and logged as `limits.busy`.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `concurrency_global` | **Listings and downloads at once** | `16` | 1 to 256 | Folder listings, file opens, downloads, folder ZIPs and archive listings running at the same time across the server. |
| `concurrency_per_source` | **At once on one source** | `4` | 1 to 64, and not above `concurrency_global` | The same, on any one source, so one busy server can't take every slot. |
| `concurrency_user_transfers` | **Downloads per user** | `2` | 1 to 32 | Downloads and folder ZIPs one person may have in progress. |
| `listing_max_entries` | **Entries per listing** | `5000` | 100 to 20000 | The most entries a folder or archive listing shows. A bigger folder shows the first ones and says so; the search finds the rest. |

### Updates card

Shown to super admins only. See [upgrade.md](upgrade.md#the-updates-card) for what it shows.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `update_check_enabled` | The switch next to **Updates** | on | on, off | Whether PerchTail asks GitHub once a day for the latest release. `UPDATE_CHECK=false` overrides it and disables the switch. |

### Backups card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `backup_interval_hours` | **Back up every** | `24` on a new install; an install upgraded from before 0.7.0 keeps its value (`0`, off, unless it was set) | `0` (off), or 1 to 168 hours | Makes a `scheduled` encrypted backup in `BACKUP_DIR` once this long has passed since the last one. After a failed attempt, the next one waits an hour, or the interval if that's shorter. The card also lists the backups on the server (**Backups on the server**). |

### Audit log card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `audit_view_enabled` | **Audit log** | on | on, off | Shows the **Settings → Audit log** page to the roles allowed to see it. Off hides the page for everyone, and the audit log API answers 404 to everyone but a super admin; events are still recorded. |
| `audit_folder_listings` | **Audit folder listings** | on | on, off | Records every folder someone lists below a source's root (`source.browse`), not only the sources they open and the files they open or download. Turning it on needs `manage_system_settings`; only a super admin can turn it off. |
| `audit_retention_days` | **Audit log retention** | `365` | `0` (keep forever), or at least 90 days | Entries older than this are deleted by the purge job (see `AUDIT_PURGE_INTERVAL_SECONDS`), and each purge is recorded as `audit.purge`. Only a super admin can shorten it. See [audit-events.md](audit-events.md). |

### Redaction

No screen yet: in this version a super admin changes these through the API (`PATCH /system-settings`); anyone else, `manage_system_settings` included, is refused. In this version redaction applies to what the viewer and Tail show and to downloads; alerts and push agents don't apply it yet (an agent's content is redacted on the server).

A redacted download carries the `X-PerchTail-Redacted: 1` header. A single file, a `.gz` file (downloaded decompressed) or an archive member is redacted as it is sent, its last line included, and a UTF-16 file stays UTF-16. A whole `.zip` or `.tar.gz` (or an archive inside one, downloaded as a member) is rebuilt in the same format: the members the rules hide are left out without a trace, each other file is redacted, a `.gz` file inside is decompressed, redacted and compressed again under the same name, and folders, a `.tar.gz` file's links and devices aren't kept. A folder ZIP holds every file redacted, its `.gz` files and archives treated the same way. What can't be redacted is left out and listed, with the reason, in `REDACTION-SKIPPED.txt` inside the archive or ZIP: encrypted zip members, symbolic links in a `.zip`, zip members compressed with a method PerchTail can't read (such as Deflate64), archives inside an archive, UTF-32 files, files compressed in a format PerchTail doesn't decompress, files over `MAX_DECOMPRESSED_MB` once decompressed, and compressed files that can't be read. A single UTF-32 file, an archive that can't be read, or a `.gz` member that can't be read is refused instead. `MAX_FETCH_MB` counts the bytes fetched from the source, not the redacted size.

While redaction applies, content compressed in a format PerchTail doesn't decompress (bzip2, xz, zstd, 7z and rar, and gzip or zip content in a file whose name doesn't end in `.gz`, `.zip`, `.tar.gz` or `.tgz`, such as a rotated `app.log.1` that is really gzip) can't be redacted, so it isn't shown or sent: opening, following or downloading it answers 409 with the error code `redaction_unsupported_format`, and the refusal is audited (`file.open_denied` or `file.download_denied`, `"reason": "redaction_unsupported_format"`). The format is told by the first bytes of the content, not by the file name, and that includes the content of a `.gz` file and of an archive member. In an archive or folder ZIP such a file is left out and listed in `REDACTION-SKIPPED.txt`. A `.gz` file inside an archive opens in the viewer decompressed and redacted, like a `.gz` file, within `MAX_DECOMPRESSED_MB`; one that can't be read is refused. With redaction off, nothing of this changes.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `redaction_enabled` | none yet | off | on, off | Redaction of file content with the enabled redaction rules, for everyone, super admins included. On with no enabled rule changes nothing. |
| `redaction_downloads` | none yet | on | on, off | While redaction is on, whether downloads (single files, folder ZIPs, archives and their members) are redacted too. Off, they carry the original content; the viewer, Tail and alerts stay redacted. |
| `redaction_download_suffix` | none yet | on | on, off | Whether a redacted download's file name says so (`app.redacted.log`). Only counts while downloads are redacted. |

### Two-factor authentication card

Local accounts of super admins and of roles with any global capability must always use two-factor authentication (TOTP); SSO accounts never, since their second factor belongs to the identity provider. An account that must use it and hasn't set it up can sign in, but can only set it up until it does (`user.mfa_required` in the audit log).

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `require_mfa_local_accounts` | **Require MFA for local accounts** | off | on, off | Extends the requirement to every local account. Applied at once: anyone covered without MFA is asked to set it up on their next request. Only a super admin can change it, either way. |

### Password policy card

Applies to local accounts whenever a password is set: a user's own change, the change at first sign-in, and temporary passwords an admin sets. Existing passwords are checked at their next change. Saved together with **Save policy**.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `password_min_length` | **Minimum length** | `12` | 8 to 1024, and not above the maximum | The shortest password accepted. |
| `password_max_length` | **Maximum length** | `128` | 64 to 1024 | The longest password accepted. |
| `password_required_classes` | **Character classes required** | `0` (off) | `0` (off) to `4` (all 4) | How many of lowercase, uppercase, digit and symbol a password must contain. |
| `password_history` | **Don't reuse the last N passwords (0 = off)** | `0` | 0 to 24 | Refuses the current password and the ones before it, N in all. Lowering it deletes the stored hashes it no longer needs at once. |
| `password_max_age_days` | **Expire after N days (0 = never)** | `0` | 0 to 3650 | After this many days, a password must be changed. |
| `password_reject_simple` | **Reject passwords that are too simple (repeats, sequences, keyboard runs, the username)** | on | on, off | Refuses repeated characters, sequences, keyboard runs and the username. |
| `password_reject_personal` | **Reject passwords containing the username, email or name** | on | on, off | Refuses passwords that contain the user's username, email or name. |

## Settings → Agents

**Settings → Agents** lists every push agent with its version and upgrades them from the portal ([upgrade.md](upgrade.md#from-the-portal)). The page is shown to users with the `create_source` global capability; the settings below, at the top of the page, can be changed only by those who may manage system settings, and are saved with **Save**. Changes are audited as `system_settings.update`. Each agent can also have its own policy, in its row: **Follow the system** (the default), **Off**, **Manual** or **Automatic**.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `agent_upgrade_policy` | **Remote upgrades** | `off` (Off) | `off` (Off), `manual` (Manual), `automatic` (Automatic) | Off: no upgrades from the portal. Manual: users who can manage an agent's source upgrade it from the page (**Upgrade**, **Upgrade all outdated agents**). Automatic: the server also upgrades connected outdated agents inside the window. |
| `agent_upgrade_window_days` | **Automatic window**, the day boxes (Mon to Sun) | `127` (every day) | At least one day; stored as a bit per day, Monday first | The days the automatic window opens. |
| `agent_upgrade_window_start_minutes` | **Start** | `120` (02:00) | 00:00 to 23:59; stored as minutes after midnight | When the window opens, in the server's time zone (see `TZ`). A window may run past midnight into the next day. |
| `agent_upgrade_window_hours` | **for … hours** | `4` | 1 to 24 | How long the window stays open. |
| `agent_upgrade_failure_limit` | **Failure limit** | `3` | 1 to 10 | After this many failed, refused or timed-out upgrades in a row, automatic upgrades skip the agent until someone clicks **Resume**. A manual **Upgrade** still works. |
| `agent_upgrade_concurrency` | **At once** | `5` | 1 to 50 | How many agents the schedule upgrades at the same time. |

Two more rows of the `system_setting` table aren't settings: `agent_upgrade_last_window` keeps the bounds of the last automatic window for the page's summary, and `agent_remote_upgrade_enabled`, the on/off switch from before the policy existed, is read as **Manual** when it is on and no policy was saved, and is removed when the policy is next saved.

Other settings pages hold configuration that isn't a system setting: **Settings → SSO** ([sso-setup.md](sso-setup.md)), **Settings → Email** (the SMTP server, super admins only, [admin-guide.md](admin-guide.md#email)), **Settings → Severity indicators** and **Settings → Import / export** ([bulk-import.md](bulk-import.md)).
