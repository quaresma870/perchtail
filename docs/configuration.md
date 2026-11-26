# Configuration reference

This guide lists every setting of a PerchTail server and its push agents, with its default, accepted values, effect and where to change it, for the administrators who deploy and run PerchTail.

Related guides: [admin-guide.md](admin-guide.md) (day-to-day administration), [user-guide.md](user-guide.md), [agent-service.md](agent-service.md) (running the push agent as a service), [troubleshooting.md](troubleshooting.md), [upgrade.md](upgrade.md), [backup-and-restore.md](backup-and-restore.md), [monitoring.md](monitoring.md), [install-from-source.md](install-from-source.md), [source-setup.md](source-setup.md), [sso-setup.md](sso-setup.md), [credential-key-rotation.md](credential-key-rotation.md) and [high-availability.md](high-availability.md).

PerchTail has two kinds of settings:

- **Environment variables**, read once when the process starts. They cover what the operator of the host decides: paths, keys, limits that protect the host, network trust. Changing one needs a restart.
- **System settings**, stored in the database and changed in the web interface on the pages of each Settings section (**Settings → Source defaults**, **Sign-in security**, **Redaction**, **Explorer and limits**, **Alerts**, **Backups and updates**, **Audit log** and **Agents**). They take effect without a restart.

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
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/perchtail.db` | A SQLite file URL: `sqlite:///` followed by the path (four slashes for an absolute path, `sqlite:////var/lib/perchtail/perchtail.db`); or a PostgreSQL URL, `postgresql://user:password@host:5432/database` (also `postgres://` or `postgresql+psycopg://`), with libpq options such as `sslmode` and `sslrootcert` in its query | The database. Set it to keep a SQLite database outside `DATA_DIR`, or to use PostgreSQL 18 or later (see [PostgreSQL](#postgresql)). PerchTail never shows it with its password: the log and its error messages hide it. `python -m app.backup` supports a SQLite file only. |
| `DATABASE_URL_FILE` | empty | A file path | Reads `DATABASE_URL` from this file instead, for a Docker or Kubernetes secret: its content, without the line break at its end. Setting both `DATABASE_URL` and `DATABASE_URL_FILE`, or naming a file that doesn't exist or is empty, stops PerchTail at startup with an error that names the variable, never its value. |
| `DATABASE_POOL_SIZE` | `10` | A whole number, at least 1 | PostgreSQL only: connections PerchTail keeps open to the database. |
| `DATABASE_MAX_OVERFLOW` | `20` | A whole number, at least 0 | PostgreSQL only: connections PerchTail may open on top of `DATABASE_POOL_SIZE` during a burst, closed again once it is over. With the defaults PerchTail uses at most 30 connections, one of which holds the instance lock; keep the server's `max_connections` above that. |
| `DATABASE_REQUIRE_VERIFIED_TLS` | `true` | `true`, `false` | PostgreSQL only: `true` refuses to start when the database isn't on this host (a Unix socket, `localhost` or a loopback address) and the connection doesn't verify the server's certificate (`sslmode` other than `verify-ca` or `verify-full`), with `startup.database_tls_not_verified` at `CRITICAL`. `false` lets PerchTail start with that event as a warning at every start, and the system settings pages (for example Settings → Explorer and limits) say at the top that the connection to the database doesn't verify its certificate. Use `false` only on a network nobody else can reach, such as the private network of a Docker Compose project. |
| `CREDENTIAL_SALT_PATH` | `<DATA_DIR>/credential_salt` | A file path | The per-install random salt (16 bytes) that, with `CREDENTIAL_ENCRYPTION_KEY`, derives the keys for stored credentials, SSO secrets, MFA secrets, alert webhook URLs, the SMTP password, the audit log's hash chain and the hash Tail and alerts use to recognise a file's start. Created on first use, readable by PerchTail's user only. Losing it makes all of those unreadable: see [backup-and-restore.md](backup-and-restore.md#what-the-keys-and-the-salt-protect). |
| `SSH_KNOWN_HOSTS_PATH` | `<DATA_DIR>/ssh_known_hosts` | A file path | The SSH host keys PerchTail has pinned. Losing it resets host-key pinning: with `SSH_HOST_KEY_POLICY=tofu` every server's key is trusted again on its next connection. |
| `LOG_DIR` | `./data/logs` (`/data/logs` in the image) | A directory path | Where PerchTail writes its own log, `perchtail.log`, and its rotated archives (see [Logging](#logging)). The built-in application-log source shows this directory; when `LOG_DIR` changes, the source follows it at the next start, recorded in the audit log as a `source.update` with no user and reason `log_dir_changed`. |
| `SCRATCH_DIR` | `./data/scratch` (`/data/scratch` in the image) | A directory path | Short-lived copies of files fetched from remote sources, folder ZIPs being built and remote copies of archives. Emptied at startup. Never a filesystem root, and never a directory that holds the database or the logs: PerchTail then refuses to clear it (`scratch.leftovers_not_cleared`). Put it on its own disk where you can. |
| `BACKUP_DIR` | `backups` next to `DATA_DIR` (`./data/backups`; `/data/backups` in the image) | A directory path | Where `python -m app.backup` and the scheduled backup write encrypted backups. See [Backups](#backups). |
| `DERIVED_FROM_DATA_DIR` | (computed) | Not a setting | Internal: PerchTail records which of `DATABASE_URL`, `CREDENTIAL_SALT_PATH` and `SSH_KNOWN_HOSTS_PATH` were left to default, so the move of a layout from before 0.5.0 never touches a path you set. A value set in the environment is replaced at startup. |

### PostgreSQL

SQLite in `DATA_DIR` is the default and needs nothing. To use PostgreSQL instead, give PerchTail an empty database on PostgreSQL 18 or later and point `DATABASE_URL` (or `DATABASE_URL_FILE`) at it; `alembic upgrade head`, which the Docker image runs before every start, creates the schema. To move an existing SQLite installation there, with its data, use `python -m app.migrate_database` ([upgrade.md](upgrade.md#moving-from-sqlite-to-postgresql)).

- **The database.** Create it with the UTF8 encoding, owned by the role PerchTail connects as. PerchTail refuses to start on a server older than 18 (`startup.database_version_unsupported`) or a database in another encoding (`startup.database_encoding_unsupported`), both at `CRITICAL`. The `C.UTF-8` locale of the `builtin` provider is recommended, so that lists are sorted the same as on SQLite: `CREATE DATABASE perchtail OWNER perchtail ENCODING 'UTF8' LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8' TEMPLATE template0;`.
- **The `pg_trgm` extension.** The migration that creates the schema also creates it in the database (`CREATE EXTENSION IF NOT EXISTS pg_trgm`), with a trigram index on the file-name index's paths. It is a trusted extension, so the database's owner may create it; on a server where extensions are restricted, have an administrator create it first.
- **TLS.** libpq's options go in the URL's query. Over a network, verify the server's certificate: `postgresql://perchtail:<password>@db.example.com:5432/perchtail?sslmode=verify-full&sslrootcert=/run/secrets/pg-ca.pem` (add `sslcert` and `sslkey` for a client certificate). libpq's default, `prefer`, encrypts when it can but checks nothing, so by default PerchTail refuses to start on it (see `DATABASE_REQUIRE_VERIFIED_TLS`). A database on the same host (a Unix socket, `localhost`, `127.0.0.1` or `::1`) needs no TLS.
- **The password.** Keep it out of `.env` where you can: put the whole URL in a secret and name it with `DATABASE_URL_FILE`, or leave the password out of the URL and give it to libpq with `PGPASSWORD` or a password file (`PGPASSFILE`). A password with `@`, `:`, `/` or `%` in it must be percent-encoded in the URL.
- **Connections.** PerchTail keeps a pool of `DATABASE_POOL_SIZE` connections (up to `DATABASE_MAX_OVERFLOW` more), checks each one before using it, and replaces one after 30 minutes. It connects with `application_name=perchtail` (shown in `pg_stat_activity`), gives up connecting after 10 seconds and sends TCP keepalives, so a dead server or a cut connection is noticed.
- **One instance per database.** Besides the lock file in `DATA_DIR`, PerchTail and `alembic upgrade` take an advisory lock in the database (`pg_try_advisory_lock`) while they run, so a second instance with another `DATA_DIR` on the same database refuses to start (`startup.instance_lock_held`, with `lock` `database`). The lock goes with the connection holding it, however the process ends. A server restart, a failover or a firewall cutting the idle connection also ends it: connections send TCP keepalives (after 30 seconds idle, every 10 seconds, 3 misses), and the `database_lock_check` job checks the lock every minute and takes it again on a new connection (`database.instance_lock_reacquired`, a warning). If another instance took it meanwhile, it logs `database.instance_lock_lost` (critical, naming that instance) and `/readyz` fails with `database_lock` until PerchTail is restarted; stop one of the two.
- **`DATA_DIR` is still needed.** It holds the credential salt, the SSH host keys, the audit chain marker, the instance lock and the first super admin's password: keep it on a persistent volume and back it up as with SQLite.

## Keys, security and sessions

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | `changeme` | Any string; generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` | The root secret. With the salt, it encrypts source credentials, SSO provider settings, MFA secrets, alert webhook URLs and the SMTP password, keys the audit log's hash chain and the hash Tail and alerts use to recognise a file's start, and, unless `BACKUP_ENCRYPTION_KEY` is set, encrypts backups. PerchTail refuses to start while it is `changeme` (`startup.insecure_credential_encryption_key`). Changing it on an existing install needs a rotation, not just a new value: see [credential-key-rotation.md](credential-key-rotation.md). A key shorter than 32 bytes, or with less than 128 bits of estimated entropy, stops PerchTail from starting (`startup.weak_credential_encryption_key`); see `CREDENTIAL_KEY_REFUSE_WEAK`. |
| `CREDENTIAL_ENCRYPTION_KEY_FILE` | empty | The path of a readable file | Reads `CREDENTIAL_ENCRYPTION_KEY` from this file, for a Docker or Kubernetes secret mounted as a file, so the key isn't in the environment. The file's content is the key; one final line break (`\n` or `\r\n`) is ignored. Set it or `CREDENTIAL_ENCRYPTION_KEY`, not both: with both, or with a file that is missing, unreadable or empty, PerchTail doesn't start and the message names `CREDENTIAL_ENCRYPTION_KEY_FILE`. The key it reads is checked like the variable (`changeme` and weak keys). `python -m app.backup` and `python -m app.rotate_credential_key` read it too. |
| `CREDENTIAL_KEY_REFUSE_WEAK` | `true` | `true`, `false` | `true` refuses to start with a weak `CREDENTIAL_ENCRYPTION_KEY`. `false` lets PerchTail start with one, as a temporary way out while the key is rotated: the log then has `startup.weak_credential_encryption_key` and `startup.weak_credential_key_allowed` as warnings at every start, and the system settings pages (for example Settings → Explorer and limits) say at the top that the key is weaker than recommended. A key is weak when it is shorter than 32 bytes once decoded (a hex or base64 key counts the bytes it decodes to, any other key its UTF-8 bytes) or has less than 128 bits of estimated entropy (its length times the Shannon entropy of its characters, capped by its size compressed with zlib, so a repeated word or phrase doesn't pass). The keys the generator above makes always pass. Change a weak key with a rotation ([credential-key-rotation.md](credential-key-rotation.md)), which runs with the weak key, and remove `CREDENTIAL_KEY_REFUSE_WEAK=false` afterwards. |
| `API_DOCS` | `false` | `true`, `false` | `true` serves the interactive API documentation at `/docs` and `/redoc` and the OpenAPI schema at `/openapi.json`. Off, they answer 404: the HTTP API is internal to the web UI and may change before 1.0, and the schema lists every endpoint for whoever can reach the server. |
| `CREDENTIAL_SALT_ALLOW_REGENERATE` | `false` | `true`, `false` | Leave it `false`. When the salt file is missing and the database already holds encrypted data, PerchTail refuses to start (`startup.credential_salt_missing`) so that you can restore the salt. `true` lets it start with a new salt (`startup.credential_salt_regenerated`): every source credential, SSO secret, MFA secret, alert webhook URL and the SMTP password must then be entered again, and older audit rows fail the integrity check. Set it back to `false` afterwards. |
| `SESSION_TTL_HOURS` | `12` | A whole number of hours | How long a sign-in lasts. The session expires that long after sign-in, whatever the activity, and the cookie has the same lifetime. |
| `SESSION_COOKIE_SECURE` | `true` | `true`, `false` | The `Secure` flag on the session cookie and the SSO sign-in cookie. Keep `true` behind HTTPS. Browsers drop a `Secure` cookie over plain HTTP, so sign-in fails without an error on `http://`; set `false` only for local tests over HTTP. With `true` the cookies are named `__Host-perchtail_session` and `__Host-perchtail_sso_state` (only PerchTail's own host, over HTTPS, can set them); with `false`, `perchtail_session` and `perchtail_sso_state`. Changing it signs everyone out. |
| `LOGIN_MAX_ATTEMPTS` | `5` | A whole number | Failed sign-ins in a row against one username before that username is locked out. One client address (an IPv6 address counts as its `/64`) is locked after the same number of failures, whatever the usernames: behind a NAT or a shared proxy, that many failures by anyone there lock that address for everyone for `LOGIN_LOCKOUT_SECONDS`, so behind a reverse proxy set `TRUSTED_PROXIES` to it. An active local super admin with MFA on is locked per username and address, so it can't be locked out from every address; without MFA on yet, it's locked per username like any other account. See [troubleshooting.md](troubleshooting.md#too-many-failed-login-attempts-try-again-later). |
| `LOGIN_LOCKOUT_SECONDS` | `300` | A whole number of seconds | How long the lockout lasts. The counts are kept in memory: a restart clears them. |
| `INITIAL_ADMIN_USERNAME` | `admin` | A username | The break-glass super admin PerchTail creates on its first start, only when the database has no users. Its password is generated and written to `DATA_DIR/initial-admin-password`, readable only by PerchTail's user; the log only says where the file is (`initial_super_admin.seeded`, field `password_file`). The password must be changed at first sign-in, and the file is deleted when it is. Changing the variable later has no effect. |
| `SSO_ALLOW_HTTP_LOCALHOST` | `false` | `true`, `false` | The OIDC issuer and the endpoints its discovery document names must be `https://`. `true` also accepts `http://` on `localhost`, `127.0.0.1` and `::1`, for local development with a test identity provider, and lets requests to those loopback hosts through the address policy that otherwise refuses any IdP at a private, loopback or link-local address (docs/sso-setup.md). Never on another host. |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | The external URL users reach PerchTail at, without a trailing slash | Builds the OIDC redirect URI, `<PUBLIC_BASE_URL>/auth/sso/callback`, and the address the browser returns to after an SSO sign-in. Must match the redirect URI registered at the identity provider ([sso-setup.md](sso-setup.md)). Also the start of the links in password-reset emails, which can only be turned on while it starts with `https://`. |

## Sources and connectors

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `LOCAL_SOURCE_ROOTS` | empty | Comma-separated absolute directories, for example `/mnt/host-logs` | The directories "Local disk" sources may read, with everything beneath them. Empty turns local-disk sources off; only the built-in application-log source reads local disk then. Checked on every access, after following symlinks, so narrowing it also stops existing sources outside it. Set only here, never from the web interface, so that a web admin can't point a source at the server's own database or keys. See [source-setup.md](source-setup.md#local-disk-directories-on-the-perchtail-host-itself). |
| `SSH_HOST_KEY_POLICY` | `tofu` | `tofu`, `strict` | `tofu` pins a server's SSH host key on first connection and logs its fingerprint; `strict` refuses a server whose key isn't already in `SSH_KNOWN_HOSTS_PATH`. A changed key is refused either way. See [source-setup.md](source-setup.md#6-host-key-pinning). |
| `REMOTE_READ_TIMEOUT_SECONDS` | `60` | A whole number of seconds | How long a connected SSH/SFTP or SMB source may stay silent before the call fails, so a server that stalls after login can't hold a worker. For SSH it bounds the banner, authentication, channel and SFTP reads; for SMB the connection and the server's answers. A background source check is given up after twice this plus 10 seconds. |
| `MAX_FETCH_MB` | `2048` | A number of MB | The largest single file a remote source fetches, and the largest total a folder ZIP may collect. Over it, the request is refused (HTTP 413). A `.zip` on an SSH, SMB or WinRM source is read in place, so for it the limit counts the bytes each operation transfers: expanding it (the archive's end: its index and end records), and opening, following or downloading one of its files (that file's header and compressed data, plus the index to find it). A `.zip` larger than the limit can then be expanded and its files opened; an operation that would transfer more than the limit (a huge index, or a file whose compressed size passes it) is refused with the same 413. Downloading the whole archive, a `.tar.gz` and a `.gz` file still count their size as stored. |
| `MAX_DECOMPRESSED_MB` | `2048` | A number of MB | The largest size one `.gz` file, or one member of a `.zip` or `.tar.gz`, may decompress to before opening it is refused (HTTP 413). A guard against archive bombs. |

## Scratch and limits

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `SCRATCH_MAX_GB` | `5.0` | A number of GiB | The most scratch may hold. Over it, the files nobody has open are evicted (`scratch.size_guard_eviction`); when that isn't enough, a new open, download, folder ZIP or archive listing is refused (HTTP 507, `scratch.full`). |
| `SCRATCH_IDLE_SECONDS` | `300` | A whole number of seconds | A file in scratch untouched for this long is deleted, even if a client never closed it. A file still being fetched, redacted or sent isn't; its time counts from when that ends. |
| `SCRATCH_SWEEP_INTERVAL_SECONDS` | `60` | A whole number of seconds | How often the idle sweep and the size guard run. |
| `SCRATCH_MIN_FREE_GB` | `1.0` | A number of GiB; `0` with `SCRATCH_MIN_FREE_PERCENT=0` turns the check off | Free space to keep on the filesystem that holds `SCRATCH_DIR`. A new fetch is refused (HTTP 507, `scratch.disk_low`) when free space would drop below the larger of this and `SCRATCH_MIN_FREE_PERCENT`. |
| `SCRATCH_MIN_FREE_PERCENT` | `5.0` | A percentage of the filesystem | The same floor, as a share of the filesystem's size. |
| `SHUTDOWN_TIMEOUT_SECONDS` | `20` | A whole number of seconds | How long a stop waits for running background jobs and the file-name index and source-check workers to finish. Keep the container's stop grace period above it: `docker-compose.yml` sets `stop_grace_period: 30s`. |

How much work runs at once (listings, downloads, folder ZIPs) is a system setting, in the **Limits** card of **Settings → Explorer and limits**: see [System settings](#system-settings).

## Logging

PerchTail logs JSON lines to standard output and to `LOG_DIR/perchtail.log`. The active file rotates at `LOG_MAX_FILE_MB` or at midnight, whichever comes first; each rotated file is compressed as `perchtail.log.<date>.<n>.gz`.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (any case); anything else counts as `INFO` | The lowest level logged. At `INFO` and above, the SMB and SSH libraries (`smbprotocol`, `spnego`, `paramiko`) log only warnings and errors, since at `INFO` they log every SMB message and SSH session. `DEBUG` can write gigabytes a day, mostly SMB and SSH packet dumps. |
| `LOG_RETENTION_DAYS` | `30` | A whole number of days; `0` keeps archives regardless of age | Rotated archives older than this are deleted. |
| `LOG_MAX_FILE_MB` | `100` | A number of MB; `0` rotates at midnight only | The size at which the active file rotates. |
| `LOG_MAX_TOTAL_MB` | `2048` | A number of MB; `0` for no limit | The most all logs together may take. The oldest archives are deleted first; when this deletes archives `LOG_RETENTION_DAYS` would still have kept, PerchTail logs `logs.retention.size_limit` and reports it in `/monitoring/health`. |
| `LOG_QUIET_THIRD_PARTY` | `true` | `true`, `false` | With `LOG_LEVEL=DEBUG`, `true` keeps PerchTail's own DEBUG lines and caps the protocol libraries (SMB, SSH, WinRM, HTTP) at `INFO`. `false` lets them log at `DEBUG`: their messages can include what is read from sources, before redaction, and they make reads slow and the logs large. Use `false` only to debug a connection, for a short time; PerchTail logs a `logging.third_party_debug` warning at startup while it is set. No effect at `INFO` and above. |

## Audit log

What the audit log keeps, and whether its page is shown, are system settings (the **Audit log** card of **Settings → Audit log**). These two variables set how often its background jobs run.

| Variable | Default | Accepted values | Effect |
|---|---|---|---|
| `AUDIT_PURGE_INTERVAL_SECONDS` | `86400` (a day) | A whole number of seconds | How often the purge job deletes entries older than `audit_retention_days`. A job that missed its time while PerchTail was stopped runs after the next start. |
| `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` | `1` | A whole number of days | How often the job that verifies the audit log's hash chain runs. A break logs `audit_integrity.chain_broken` and is recorded as `audit.integrity_check_failed`. It also catches up after a restart. |

## Monitoring and reverse proxy

Who may call `/healthz`, `/readyz`, `/monitoring/health` and `/metrics` is explained in [monitoring.md](monitoring.md#who-may-call-them). The monitoring token is generated in **Settings → Monitoring**.

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
| `BACKUP_DIR` | `backups` next to `DATA_DIR` (`/data/backups` in the image) | A directory path | Where backups are written, readable by PerchTail's user only. Listed (not downloadable) in **Settings → Backups and updates**, card **Backups**. Copy them off the host. |
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
| `PERCHTAIL_BASE_PATH` | required | A local directory | The root the agent may read; nothing outside it is served. The agent checks it at startup and stops if it is missing, not a folder or not readable. |
| `PERCHTAIL_ALLOW_INSECURE` | unset (false) | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` | Allows a plain `ws://` server URL. The token and every fetched file then travel in clear text: for tests or a fully trusted network only. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` | `false` refuses every upgrade from the portal, whatever the server's policy. Any other value stops the agent at startup, so a typo can't leave upgrades on. |

The agent uses Go's standard proxy variables (`HTTPS_PROXY`, `NO_PROXY`) for its connection to the server and for downloads from GitHub.

## System settings

The system settings are on the pages of each Settings section, shown to super admins and to roles with the `manage_system_settings` global capability: **Source defaults** (section Sources), **Sign-in security** (Access), **Redaction** (Content, super admins only), **Explorer and limits** (Content), **Alerts**, **Backups and updates** and **Monitoring** (Operations), and the settings on the **Audit log** page (Audit). Each page groups its settings in cards, named below as on screen. The address of the former System page (`#/settings/system`) opens **Settings → Explorer and limits**. A switch or a list is saved as soon as it changes, a number with the **Save** button next to it, and the **Limits** and **Password policy** cards with one button for the whole card. Each change is recorded in the audit log as `system_settings.update`, and applies without a restart. A setting nobody changed has its default from the code; the values are stored in the database (table `system_setting`), so they are part of every backup, but not of the YAML export.

The key is the setting's name in the code (`backend/app/system_settings.py`), as it appears in the `system_settings.update` audit entries.

### Settings → Explorer and limits

#### Explorer card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `connections_view_default` | **Connections view** | `list` | `list` (List), `tree` (Tree) | How the viewer's explorer lists sources for everyone who hasn't chosen: a flat list, or a tree by customer and folder. Open browsers pick it up on their next page change, within 30 seconds. |
| `connections_view_switchable` | **Let users switch between list and tree** | on | on, off | On: each user can switch, and the choice follows them. Off: the switch is hidden and everyone sees the default view. |
| `explorer_sort_default` | **File order** | `name_asc` (Name A→Z) | `name_asc` (Name A→Z), `name_desc` (Name Z→A), `mtime_desc` (Newest first), `mtime_asc` (Oldest first), `size_desc` (Largest first) | How the explorer orders files in a folder, for everyone. Folders always come first. |
| `explorer_refresh_seconds` | **Refresh open folders** | `30` | `0` (off), or 5 to 3600 seconds | How often the explorer lists the folders people have open again, so rotated files show up. Only the list of files, never their content, and not while the browser tab is hidden. |
| `tail_interval_seconds` | **Tail interval** | `2` | `1`, `2`, `5`, `10` seconds | How often Tail checks an open file for new lines, for everyone. A shorter interval is more live and asks the source more often. |

#### Limits card

The limits are saved together with **Save limits**. A request over a limit is answered "busy" (HTTP 503 with `Retry-After: 5`) instead of waiting, and logged as `limits.busy`.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `concurrency_global` | **Listings and downloads at once** | `16` | 1 to 256 | Folder listings, file opens, Tail polls, downloads, folder ZIPs and archive listings running at the same time across the server. |
| `concurrency_per_source` | **At once on one source** | `4` | 1 to 64, and not above `concurrency_global` | The same, on any one source, so one busy server can't take every slot. |
| `concurrency_user_transfers` | **Downloads per user** | `2` | 1 to 32 | Downloads and folder ZIPs one person may have in progress. |
| `listing_max_entries` | **Entries per listing** | `5000` | 100 to 20000 | The most entries a folder or archive listing shows. A bigger folder shows the first ones and says so; the search finds the rest. |
| `open_from_end_mb` | **Open from the end above** | `50` | `0` (off), or 1 to 100000 MB | A file bigger than this, as listed, opens in the viewer at its last 1 MB, read through Tail without copying the file to scratch, with **Load earlier** and **Load whole file**. `.gz` files and files inside archives always open whole. `0` opens every file whole, with the warning above 200 MB. |

### Settings → Monitoring

#### Monitoring card

No stored setting: **Generate token** (or **Regenerate token**) creates the bearer token for `/monitoring/health` and `/metrics`, shown once. Regenerating it invalidates the previous one. See [monitoring.md](monitoring.md#generating-a-monitoring-token).

### Settings → Source defaults

#### Sources card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `source_check_minutes` | **Check sources in the background** | `10` | `0` (off), or up to 1440 minutes | How often PerchTail checks that each source answers, one connection per source, so the explorer shows which ones are down. Push-agent sources show their live connection instead. A source that fails three times in a row backs off for up to 30 minutes. |
| `name_index_enabled` | **Index file names** | on | on, off | The file-name index: PerchTail lists every source's rule-visible files in the background and keeps their names, sizes and times, never their content. The explorer's search across sources and live alerts use it. Off stops the listings; what was indexed stays until a source is changed. Each source can also be left out in its own settings. |
| `name_index_minutes` | **List each source's files every** | `15` | 5 to 1440 minutes | A source is listed again once its index is older than this, a few sources at a time. |
| `credential_max_age_days` | **Warn when a credential is older than** | `90` | `0` (off), or up to 3650 days | **Settings → Sources** marks the sources whose credential was set longer ago than this. |
| `source_max_roots` | **Folders per source** | `20` | 1 to 100 | How many folders one source may read. `1` turns several folders per source off. Sources that already have more keep them, but can't add more. |

### Settings → Alerts

#### Alerts card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `alerts_interval_seconds` | **Check alerts every** | `60` | 15 to 3600 seconds | How often a pass reads the lines appended to the files each alert watches. |

### Settings → Backups and updates

#### Updates card

Shown to super admins only. See [upgrade.md](upgrade.md#the-updates-card) for what it shows.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `update_check_enabled` | The switch next to **Updates** | on | on, off | Whether PerchTail asks GitHub once a day for the latest release. `UPDATE_CHECK=false` overrides it and disables the switch. |

#### Backups card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `backup_interval_hours` | **Back up every** | `24` on a new install; an install upgraded from before 0.7.0 keeps its value (`0`, off, unless it was set) | `0` (off), or 1 to 168 hours | Makes a `scheduled` encrypted backup in `BACKUP_DIR` once this long has passed since the last scheduled one. After a failed attempt, the next one waits an hour, or the interval if that's shorter. The card also lists the backups on the server (**Backups on the server**). |

### Settings → Audit log

The audit log's settings are on the **Settings → Audit log** page, below its viewer, for whoever manages system settings. They stay there while the viewer is off, so it can be turned on again.

#### Audit log card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `audit_view_enabled` | **Audit log** | on | on, off | Shows the **Settings → Audit log** page to the roles allowed to see it. Off hides the log for everyone except those who manage system settings, who keep the page with its settings, and the audit log API answers 404 to everyone but a super admin; events are still recorded. |
| `audit_folder_listings` | **Audit folder listings** | on | on, off | Records every folder someone lists below a source's root (`source.browse`), not only the sources they open and the files they open or download. Turning it on needs `manage_system_settings`; only a super admin can turn it off. |
| `audit_retention_days` | **Audit log retention** | `365` | `0` (keep forever), or at least 90 days | Entries older than this are deleted by the purge job (see `AUDIT_PURGE_INTERVAL_SECONDS`), and each purge is recorded as `audit.purge`. Only a super admin can shorten it. See [audit-events.md](audit-events.md). |

### Redaction

Shown on **Settings → Redaction**, to super admins only; anyone else, `manage_system_settings` included, is refused. The rules themselves, and what each source turns off or adds, are set there and in each source's editor: [redaction.md](redaction.md) describes the rules, where content is redacted and what redaction doesn't cover. Redaction applies to what the viewer and Tail show, to downloads and to alerts: an alert's query runs on the redacted lines, so a value that was replaced can't be matched, and only redacted lines are sent to its webhook, including calls queued before redaction was turned on, which are redacted as they are sent.

On a push-agent source, an agent of this version or later redacts on its own host, with the same rules and the same results, so the original content never reaches PerchTail: the file, the archive member or the decompressed `.gz` file is read and redacted there, an archive is listed and its members read there, and the server only rebuilds the archive or the ZIP from what arrived redacted. The server only uses an answer that says it was redacted with the rules it sent. If the agent can't run a custom rule, the request fails with the rule's name instead of falling back to the server. An archive inside an archive can't be downloaded from an agent while downloads are redacted (each of its files still opens one by one in the viewer). An agent older than 0.7 can't redact on its host, and the server reads none of its files anyway until it is upgraded, because it doesn't say where links lead (`links/1`, see [agent-service.md](agent-service.md#what-the-agent-tells-the-server)): its content requests are refused.

A redacted download carries the `X-PerchTail-Redacted: 1` header. A single file, a `.gz` file (downloaded decompressed) or an archive member is redacted as it is sent, its last line included, and a UTF-16 file stays UTF-16. A whole `.zip` or `.tar.gz` (or an archive inside one, downloaded as a member) is rebuilt in the same format: the members the rules hide are left out without a trace, each other file is redacted, a `.gz` file inside is decompressed, redacted and compressed again under the same name, and folders, a `.tar.gz` file's links and devices aren't kept. A folder ZIP holds every file redacted, its `.gz` files and archives treated the same way, each fetched and redacted (an archive rebuilt) only when its turn comes, so the ZIP starts at once. What can't be redacted is left out and listed, with the reason, in `REDACTION-SKIPPED.txt` inside the archive or ZIP: encrypted zip members, symbolic links in a `.zip`, zip members compressed with a method PerchTail can't read (such as Deflate64), archives inside an archive, UTF-32 files, files compressed in a format PerchTail doesn't decompress, files over `MAX_DECOMPRESSED_MB` once decompressed, and compressed files that can't be read. A single UTF-32 file, an archive that can't be read, or a `.gz` member that can't be read is refused instead. `MAX_FETCH_MB` counts the bytes fetched from the source, not the redacted size.

While redaction applies, content compressed in a format PerchTail doesn't decompress (bzip2, xz, zstd, 7z and rar, and gzip or zip content in a file whose name doesn't end in `.gz`, `.zip`, `.tar.gz` or `.tgz`, such as a rotated `app.log.1` that is really gzip) can't be redacted, so it isn't shown or sent: opening, following or downloading it answers 409 with the error code `redaction_unsupported_format`, and the refusal is audited (`file.open_denied` or `file.download_denied`, `"reason": "redaction_unsupported_format"`). The format is told by the first bytes of the content, not by the file name, and that includes the content of a `.gz` file and of an archive member. In an archive or folder ZIP such a file is left out and listed in `REDACTION-SKIPPED.txt`. A `.gz` file inside an archive opens in the viewer decompressed and redacted, like a `.gz` file, within `MAX_DECOMPRESSED_MB`; one that can't be read is refused. With redaction off, nothing of this changes.

How content is read for redaction is decided by its first bytes (plain text, UTF-16, or text that may be UTF-16) and checked again on every line, as content can change encoding part way. From a line of plain text that looks UTF-16 (four characters in a row with a NUL byte each), or a line of a UTF-16 file with plain ASCII text in it, to the end of what is read, the content is redacted as text that may be UTF-16: as bytes and as both UTF-16 readings, and shown as bytes. From a line that looks UTF-32, the rest of what is read is refused: the viewer, Tail and a single-file download show what came before it and `[redacted: unsupported encoding]` in its place, and the refusal is audited with where it started (`redaction_refused` and `redaction_refused_offset` in `file.open` and `file.follow`, a `file.download_redaction_skipped` entry for a download). In an archive or folder ZIP such a file is left out and listed in `REDACTION-SKIPPED.txt`, a `.gz` member downloaded alone is refused (409, `redaction_unsupported_encoding`), and an alert doesn't read past the file's last read until it changes, as for a UTF-32 file. Each Tail poll and alert pass checks the lines it reads again. Binary content can look UTF-16 or UTF-32 to these checks: it is then redacted more, or refused, never sent less redacted. Content compressed part way through a plain text file (a gzip or xz stream after text lines) is redacted as bytes, so values inside the compressed stream aren't found.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `redaction_enabled` | **Redact file content** | off | on, off | Redaction of file content with the enabled redaction rules, for everyone, super admins included. On with no enabled rule changes nothing. |
| `redaction_downloads` | **Redact downloads too** | on | on, off | While redaction is on, whether downloads (single files, folder ZIPs, archives and their members) are redacted too. Off, they carry the original content; the viewer, Tail and alerts stay redacted. |
| `redaction_download_suffix` | **Say so in the file name** | on | on, off | Whether a redacted download's file name says so (`app.redacted.log`). Only counts while downloads are redacted. |

### Settings → Sign-in security

#### Two-factor authentication card

Local accounts of super admins and of roles with any global capability must always use two-factor authentication (TOTP); SSO accounts never, since their second factor belongs to the identity provider. An account that must use it and hasn't set it up can sign in, but can only set it up until it does (`user.mfa_required` in the audit log).

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `require_mfa_local_accounts` | **Require MFA for local accounts** | on (off on an install upgraded from 0.7 or earlier) | on, off | Extends the requirement to every local account. Applied at once: anyone covered without MFA is asked to set it up on their next request. Only a super admin can change it, either way. |

#### Sessions card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `session_idle_minutes` | **End unused sessions after** | `60` | `0` (no limit), or 5 to 720 minutes | A session nobody used for this long ends, and its user signs in again; `SESSION_TTL_HOURS` still ends every session at the latest. Any authenticated request counts as use, a running Tail included. What the web interface asks on its own (the Explorer's source list and status, refreshing open folders, the progress of a file being opened) doesn't count, so a browser left open on the Explorer still signs out. Only a super admin can make it longer or turn it off; whoever manages system settings can make it shorter. The last use is recorded at most once a minute, so a session ends between this many minutes and one more after it was last used. |

A super admin also has **End every other session**, which signs out every account, on every device, except their own session (audited as `session.revoke`, `reason` `admin_all`).

While **Require MFA for local accounts** is off, the system settings pages show "Two-factor authentication isn't required of every local account." at the top.

#### Password recovery card

Lets users of local accounts reset a forgotten password through a link sent to their email ([user-guide.md](user-guide.md#forgotten-password), [admin-guide.md](admin-guide.md#password-recovery)). Off by default. It can be turned on only while email is set up and on under **Settings → Email** and `PUBLIC_BASE_URL` starts with `https://`; otherwise the switch is unavailable and the card says why. Accounts of super admins and of roles with any global capability, SSO accounts, and accounts without an email can't use it. Only a super admin can change these settings.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `password_reset_enabled` | **Password recovery by email** | off | on, off | Shows **Forgot password?** on the sign-in page and sends reset links. It stops working, without changing the setting, while email is off or removed or `PUBLIC_BASE_URL` isn't https. |
| `password_reset_minutes` | **Links work for** | `30` | 5 to 60 minutes | How long a reset link works after it's sent. |

#### Password policy card

Applies to local accounts whenever a password is set: a user's own change, the change at first sign-in, a reset through an email link, and temporary passwords an admin sets. Existing passwords are checked at their next change. Saved together with **Save policy**.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `password_min_length` | **Minimum length** | `12` | 8 to 1024, and not above the maximum | The shortest password accepted. |
| `password_max_length` | **Maximum length** | `128` | 64 to 1024 | The longest password accepted. A longer one is refused with "Use at most N characters." before it is hashed or checked, when signing in, changing a password, confirming one's password for a sensitive action and resetting it from an email link. |
| `password_required_classes` | **Character classes required** | `0` (off) | `0` (off) to `4` (all 4) | How many of lowercase, uppercase, digit and symbol a password must contain. |
| `password_history` | **Don't reuse the last N passwords (0 = off)** | `0` | 0 to 24 | Refuses the current password and the ones before it, N in all. Lowering it deletes the stored hashes it no longer needs at once. |
| `password_max_age_days` | **Expire after N days (0 = never)** | `0` | 0 to 3650 | After this many days, a password must be changed. |
| `password_reject_simple` | **Reject passwords that are too simple (repeats, sequences, keyboard runs, the username)** | on | on, off | Refuses repeated characters, sequences, keyboard runs, the username, and passwords on the list of common and leaked passwords shipped with PerchTail (the 8-character-or-longer entries of SecLists' 100,000 most common passwords, compared without case; no network request). |
| `password_reject_personal` | **Reject passwords containing the username, email or name** | on | on, off | Refuses passwords that contain the user's username, email or name. |

#### Words passwords can't contain card

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `password_context_words` | **Words passwords can't contain** | none | up to 50 words of 3 to 64 characters, one per line | A new password containing one of them, in any case, is refused, wherever the password policy applies; your organization's name, for example. A password containing "perchtail" is always refused, whatever this list and the other settings say. |

## Settings → Agents

**Settings → Agents** lists every push agent with its version and upgrades them from the portal ([upgrade.md](upgrade.md#from-the-portal)). The page is shown to users with the `create_source` global capability; the settings below, at the top of the page, can be changed only by those who may manage system settings, and are saved with **Save**. Changes are audited as `system_settings.update`, under the keys below except that the window's start is recorded as `agent_upgrade_window_start` in `HH:MM` and its days as a list of numbers (0 for Monday to 6 for Sunday). Each agent can also have its own policy, in its row: **Follow the system** (the default), **Off**, **Manual** or **Automatic**.

| Key | On screen | Default | Accepted values | Effect |
|---|---|---|---|---|
| `agent_upgrade_policy` | **Remote upgrades** | `off` (Off) | `off` (Off), `manual` (Manual), `automatic` (Automatic) | Off: no upgrades from the portal. Manual: users who can manage an agent's source upgrade it from the page (**Upgrade**, **Upgrade all outdated agents**). Automatic: the server also upgrades connected outdated agents inside the window. |
| `agent_upgrade_window_days` | **Automatic window**, the day boxes (Mon to Sun) | `127` (every day) | At least one day; stored as a bit per day, Monday first | The days the automatic window opens. |
| `agent_upgrade_window_start_minutes` | **Start** | `120` (02:00) | 00:00 to 23:59; stored as minutes after midnight | When the window opens, in the server's time zone (see `TZ`). A window may run past midnight into the next day. |
| `agent_upgrade_window_hours` | **for … hours** | `4` | 1 to 24 | How long the window stays open. |
| `agent_upgrade_failure_limit` | **Failure limit** | `3` | 1 to 10 | After this many failed, refused or timed-out upgrades in a row, automatic upgrades skip the agent until someone clicks **Resume**. A manual **Upgrade** still works. |
| `agent_upgrade_concurrency` | **At once** | `5` | 1 to 50 | How many agents the schedule upgrades at the same time. |

Two more rows of the `system_setting` table aren't settings: `agent_upgrade_last_window` keeps the bounds of the last automatic window for the page's summary, and `agent_remote_upgrade_enabled`, the on/off switch from before the policy existed, is read as **Manual** when it is on and no policy was saved, and is removed when the policy is next saved.

Other settings pages hold configuration that isn't a system setting: **Settings → Single sign-on** ([sso-setup.md](sso-setup.md)), **Settings → Email** (the SMTP server, super admins only, [admin-guide.md](admin-guide.md#email)), **Settings → Severity indicators** and **Settings → Import / export** ([bulk-import.md](bulk-import.md)).
