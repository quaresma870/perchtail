# Backup and restore

This guide explains, for the administrators who run PerchTail, how to back it up, check a backup, restore it on the same or a new machine, and what the keys protect.

Related guides: [admin-guide.md](admin-guide.md), [configuration.md](configuration.md) (every setting, including `BACKUP_DIR`, `BACKUP_KEEP` and `BACKUP_ENCRYPTION_KEY`), [upgrade.md](upgrade.md), [troubleshooting.md](troubleshooting.md), [monitoring.md](monitoring.md), [credential-key-rotation.md](credential-key-rotation.md), [install-from-source.md](install-from-source.md) and [agent-service.md](agent-service.md).

PerchTail keeps three things that can't be rebuilt: the database, the credential salt (`credential_salt`) and SSH `known_hosts`. The salt and `known_hosts` are in `DATA_DIR` (`/data/db` in the Docker image), and so is the database on SQLite; on PostgreSQL ([configuration.md](configuration.md#postgresql)) it is on the database server. The database holds the sources, rules, users, roles, settings and audit log; the salt and your `CREDENTIAL_ENCRYPTION_KEY` together decrypt every stored credential, SSO secret, MFA secret, alert webhook URL and the SMTP password, and key the audit log's hash chain, so each is useless without the other. Logs and scratch don't need a backup.

`python -m app.backup` backs these up while PerchTail runs, into one encrypted file, checks a backup, and restores one, on SQLite and on PostgreSQL alike. The sections below cover:

- [Making a backup](#making-a-backup): by hand, on a schedule, and before an upgrade.
- [What a backup holds](#what-a-backup-holds), and what you keep yourself.
- [The key](#the-key): which key encrypts a backup, and what a key rotation means for older backups.
- [What the keys and the salt protect](#what-the-keys-and-the-salt-protect): what is lost without each of them.
- [Checking a backup](#checking-a-backup).
- [Restoring](#restoring), step by step.
- [Restoring on a new machine](#restoring-on-a-new-machine).
- [Rolling back an upgrade](#rolling-back-an-upgrade).
- [PostgreSQL](#postgresql): how a backup is made and restored there, the time and space it takes, and PostgreSQL's own backups.
- [Keeping a copy off the host](#keeping-a-copy-off-the-host).
- [Testing a restore](#testing-a-restore).

Run every command as PerchTail's own user, never as root: the command refuses root, because the files it writes must stay readable by PerchTail. In Docker that means `-u perchtail`; for an install from source ([install-from-source.md](install-from-source.md)), `systemd-run --uid=perchtail` with the service's environment file, as shown below.

## Making a backup

### By hand

With PerchTail running:

```bash
# Docker, in the folder with docker-compose.yml:
docker compose exec -u perchtail perchtail python -m app.backup create

# Install from source:
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup create
```

The backup is taken online, from one consistent snapshot while PerchTail keeps writing: on SQLite, SQLite's backup API copies the database; on PostgreSQL, PerchTail copies its tables into a SQLite file ([PostgreSQL](#postgresql)). The copy is checked with `PRAGMA quick_check` before it's packed. The file lands in `BACKUP_DIR` (`/data/backups` in the image; by default `backups` next to `DATA_DIR`, so `/var/lib/perchtail/backups` with the install guide's paths), named `perchtail-<version>-<UTC time>-<reason>.ptbackup`, readable by PerchTail's user only. The command prints the path of the file it wrote.

`python -m app.backup list` lists the backups in `BACKUP_DIR`, newest first, with their name, UTC time, size and reason (`manual`, `scheduled` or `pre-upgrade`).

PerchTail keeps the last `BACKUP_KEEP` (7) manual and scheduled backups and the last 3 pre-upgrade ones, and deletes only the oldest of the same kind when a new one is made.

### On a schedule

**Settings → Backups and updates**, card **Backups**, **Back up every** sets an interval from 1 hour to a week; 0 turns scheduled backups off. A new install backs up every 24 hours; an install upgraded from before 0.7.0 keeps what it had, which is off unless it was set. The same card lists the backups on the server, with their date, size and reason, for whoever manages the system settings. Backups are never downloadable from the web interface or the API: copy them off the host yourself (see below).

A scheduled backup that fails logs `backup.create_failed` and fails the `backup` job in `/monitoring/health` ([monitoring.md](monitoring.md)). After a failed attempt, the next one waits an hour (or the interval, if that's shorter), so a full disk doesn't fill the audit log with an entry every few minutes. That wait is kept in memory: after a restart, the job tries again at its first run, within 5 minutes, and a backup that works brings back the normal interval.

### Before an upgrade

The upgrade steps (the README, [upgrade.md](upgrade.md), [install-from-source.md](install-from-source.md#upgrading) and the **Updates** card under **Settings → Backups and updates**) start with a backup made with `--reason pre-upgrade`, while the old version still runs. Those are kept apart from the others, so a burst of scheduled backups never deletes the one you need to roll back.

Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before.

## What a backup holds

A backup holds the database, the credential salt and, when it exists, SSH `known_hosts`, with a manifest: the backup format, the database engine (`sqlite` or `postgresql`), the PerchTail version and Alembic revision that made it, the UTC time, the reason and each file's SHA-256. The database carries everything set in the web interface, system settings included, and the hashes of the push-agent tokens and of the monitoring token, so those keep working after a restore.

A backup doesn't hold, and you keep yourself:

- the keys: `CREDENTIAL_ENCRYPTION_KEY` and, if you set it, `BACKUP_ENCRYPTION_KEY`;
- the rest of the environment: `.env` (Docker) or `/etc/perchtail/perchtail.env` (install from source), with `PUBLIC_BASE_URL`, `LOCAL_SOURCE_ROOTS`, the paths and the limits ([configuration.md](configuration.md));
- `docker-compose.yml` or the systemd unit, the reverse proxy and its TLS certificates;
- PerchTail's own logs, and each push agent's environment file on its host.

## The key

Every backup is encrypted, always: AES-256-GCM in 1 MiB blocks, with a key derived (HKDF-SHA256, a random salt per file) from:

- `BACKUP_ENCRYPTION_KEY`, when it's set; or
- `CREDENTIAL_ENCRYPTION_KEY` otherwise.

The file's header says which of the two was used. The key itself never enters the file or the logs. Each block is authenticated, together with the header, its position and whether it's the last one, so a changed byte, a missing or reordered block, a truncated file or an edited header all fail the check.

Without `BACKUP_ENCRYPTION_KEY`, the key you already guard for the credentials also opens the backups. Setting `BACKUP_ENCRYPTION_KEY` gives backups a key of their own: keep it apart from the backups and from `CREDENTIAL_ENCRYPTION_KEY` (another vault entry, another person), so that a copied backup, or a leaked credential key, isn't enough to open one. A backup made with `BACKUP_ENCRYPTION_KEY` only decrypts with it. Restoring needs both keys anyway: the backup key to open the file, and the `CREDENTIAL_ENCRYPTION_KEY` the credentials inside were encrypted with.

**After a key rotation** ([credential-key-rotation.md](credential-key-rotation.md)), backups made before it still hold credentials encrypted under the old `CREDENTIAL_ENCRYPTION_KEY`, and, without `BACKUP_ENCRYPTION_KEY`, are themselves encrypted under it. Keep the old key for as long as you keep those backups, and make a new backup right after the rotation.

## What the keys and the salt protect

`CREDENTIAL_ENCRYPTION_KEY` and the salt are combined (PBKDF2-HMAC-SHA256) into the key that encrypts source credentials, SSO provider settings, MFA secrets, alert webhook URLs and the SMTP password, into a second key that signs the audit log's hash chain, and into a third one that alerts and Tail use to recognise the start of a file. None of them can be recovered from the database, and neither the key nor the salt from the other. Local passwords, MFA backup codes, push-agent tokens, the monitoring token and sessions are stored as hashes and don't depend on them.

| What is lost | What stops working | What still works |
|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | Every source that uses a stored credential (SSH, SMB, WinRM) fails until its credential is entered again in **Settings → Sources** (one by one, or with **Update credentials** on several). SSO sign-in fails until the provider is entered again in **Settings → Single sign-on**: its issuer, client ID and client secret are stored encrypted together, so all three must be typed again, and the scopes and group claim go back to their defaults. Users with MFA turned on can finish signing in only with one of their backup codes. Alerts can't call their webhooks until each webhook URL is entered again, and read the files they watch again from the top; email can't be sent until the SMTP password is entered again in **Settings → Email**. Every audit entry from before fails the integrity check. Backups made without `BACKUP_ENCRYPTION_KEY` can't be opened at all. | Local accounts without MFA, roles, customers, folders, sources and their rules, push-agent sources (they use their own token), local-disk sources, system settings and the content of the audit log. |
| The salt (`credential_salt`) | The same as losing the key: everything derived from the key and the salt can't be read. PerchTail refuses to start while the database holds encrypted data (`startup.credential_salt_missing`), so that you can put the salt back. | Backups: each one contains the salt, and its own encryption doesn't depend on it. Restore the latest backup, or take `credential_salt` from it, to get everything back. |
| `BACKUP_ENCRYPTION_KEY` | Backups made with it can't be opened. | The running installation, which doesn't use this key. Set a new `BACKUP_ENCRYPTION_KEY` and make a new backup at once. |
| SSH `known_hosts` | Host-key pinning starts over: with `SSH_HOST_KEY_POLICY=tofu` each server's key is trusted again on its next connection; with `strict`, SSH sources fail until their keys are added. | Everything else. |

`CREDENTIAL_SALT_ALLOW_REGENERATE=true` lets PerchTail start with a new salt when the old one is lost for good, with the losses in the first row. Don't start PerchTail with a guessed or different `CREDENTIAL_ENCRYPTION_KEY` to try it: it starts, but every audit entry written meanwhile is signed with the wrong key and fails the integrity check later.

## Checking a backup

```bash
# Docker:
docker compose exec -u perchtail perchtail python -m app.backup verify /data/backups/<file>

# Install from source:
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup verify /var/lib/perchtail/backups/<file>
```

`verify` decrypts the whole file and checks every block, the manifest's format and file list, each file's SHA-256 against the manifest, that the database and the salt are there, and the database's `quick_check`, without restoring anything. It refuses a backup made on the other database engine (SQLite or PostgreSQL), which this installation couldn't restore. On success it prints the version, Alembic revision, time and reason from the manifest. It works while PerchTail runs. It decrypts into a temporary folder in `BACKUP_DIR`, removed afterwards, so `BACKUP_DIR` needs free space for about the size of the database.

`verify` proves the file is intact and opens with the backup key you have. Two checks only `restore` makes: that the current `CREDENTIAL_ENCRYPTION_KEY` decrypts the credentials inside, and that this PerchTail version knows the database's revision. To check those without touching production, restore the backup on a test instance ([Testing a restore](#testing-a-restore)).

Each `create`, `verify` and `restore` is recorded in the audit log as `backup.create`, `backup.verify` and `backup.restore`, with the file name, the reason and the result.

## Restoring

A restore replaces the database, the salt and `known_hosts` with the backup's. It needs PerchTail stopped, and refuses, changing nothing, when:

- PerchTail is running on that `DATA_DIR` (it takes the same lock a second instance would) or, on PostgreSQL, on that database;
- the backup was made on the other database engine: a SQLite backup is restored on SQLite, a PostgreSQL one on PostgreSQL;
- the file doesn't decrypt with the current key, or fails any check `verify` makes;
- the backup's database is at an Alembic revision this version doesn't know (it was made by a newer PerchTail: restore it with that version);
- on PostgreSQL, the backup or the database is at another revision than this version's ([PostgreSQL](#postgresql));
- the current `CREDENTIAL_ENCRYPTION_KEY` doesn't decrypt the credentials in the backup. Set the key the backup was made with. `--force` restores anyway, and every source credential, SSO secret, MFA secret, alert webhook URL and the SMTP password then has to be entered again; it never skips the file's own decryption and checks.

Docker:

```bash
docker compose stop perchtail
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.backup restore /data/backups/<file>
docker compose up -d
```

`--entrypoint python` keeps the container from starting PerchTail itself; `docker compose run` reads the same `.env`.

Kubernetes: stop the Deployment and run the restore in a pod of its own on the same data volume, as in [kubernetes.md](kubernetes.md#restoring-a-backup).

Install from source:

```bash
sudo systemctl stop perchtail
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup restore /var/lib/perchtail/backups/<file>
sudo systemctl start perchtail
```

The current `DATA_DIR` is moved, whole, to `<DATA_DIR>.before-restore-<UTC time>` (for example `/data/db.before-restore-20261112T101500Z`), and the backup's files are written to a fresh `DATA_DIR`, without the old `-wal` and `-shm` files. When `DATA_DIR` is its own volume, which can't be renamed, its contents are moved instead. A database, salt or `known_hosts` that `DATABASE_URL`, `CREDENTIAL_SALT_PATH` or `SSH_KNOWN_HOSTS_PATH` puts outside `DATA_DIR` is renamed to `<file>.before-restore-<UTC time>` next to itself. Delete the `before-restore` copies once the restored instance works. On PostgreSQL the database itself is replaced in place, with no `before-restore` copy of it ([PostgreSQL](#postgresql)).

The restored database can't hold the record of its own restore, so the restore is logged (`backup.restore`, [monitoring.md](monitoring.md#log-events-worth-alerting-on)) and the first start on it writes the `backup.restore` audit entry. On start, the container's entrypoint (or the unit's `ExecStartPre` for an install from source) runs `alembic upgrade head`, which migrates the restored database to the running version, as after any upgrade.

### The before-restore copy

`<DATA_DIR>.before-restore-<UTC time>` is the instance as it was just before the restore: its database (on SQLite), its credential salt, its SSH `known_hosts`, its audit chain marker and anything else that was in `DATA_DIR`. It is kept so a restore can be undone, or so something changed after the backup was made can be looked up, and PerchTail never reads it, backs it up or deletes it. Files that lived outside `DATA_DIR` get the same suffix next to themselves.

It holds the same secrets as `DATA_DIR`: the stored credentials, SSO settings and MFA secrets (encrypted, and readable with its salt and `CREDENTIAL_ENCRYPTION_KEY`), and the audit log. Keep it readable by PerchTail's user only (it is created `0700`, like `DATA_DIR`).

Delete it once the restored instance works: you can sign in, the sources connect, and the audit log's integrity check passes (Settings → Audit log, **Verify now**). Until then it is the only way back to the state before the restore. A second restore makes a copy of its own, with its own time, and leaves the earlier ones in place.

## Restoring on a new machine

To move PerchTail to another host, or to rebuild one that was lost:

1. **Install the same PerchTail version as the backup, or a newer one.** The version is in the backup's name. An older version refuses the backup; on SQLite a newer one restores it and migrates it at start. On PostgreSQL, restore it with a version at the same Alembic revision into an empty database, then upgrade ([PostgreSQL](#postgresql)).
2. **Set the environment** as on the old host: the same `CREDENTIAL_ENCRYPTION_KEY`, `BACKUP_ENCRYPTION_KEY` if the backup was made with it, and the rest of `.env` or `perchtail.env` ([What a backup holds](#what-a-backup-holds)). A different `LOG_DIR` is fine: the built-in application-log source follows it at start, with a `source.update` audit entry. The directories listed in `LOCAL_SOURCE_ROOTS` must exist on the new host.
3. **Put the backup where PerchTail's user can read it**, and restore it with PerchTail stopped (commands below).
4. **Check the new instance**: `startup.complete` in its log, sign in with an account from the backup, and open a file on a source that uses a stored credential, which proves the key and the salt match.
5. **If the address changed**: set `PUBLIC_BASE_URL` and update the redirect URI at the identity provider ([sso-setup.md](sso-setup.md)); set each push agent's `PERCHTAIL_SERVER_URL` to the new address and restart the agent ([agent-service.md](agent-service.md)). Agent tokens and the monitoring token are in the database and keep working.

Docker, in the folder with `docker-compose.yml` and `.env`:

```bash
docker compose up -d && docker compose stop perchtail   # creates the volume and the container
docker compose run --rm --no-deps --entrypoint install perchtail -d -o perchtail -g perchtail -m 0700 /data/backups
docker compose cp ./<file> perchtail:/data/backups/<file>
docker compose run --rm --no-deps --entrypoint chown perchtail perchtail:perchtail /data/backups/<file>
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.backup restore /data/backups/<file>
docker compose up -d
```

The first start creates an empty database and a new salt; the restore moves them aside to `/data/db.before-restore-<UTC time>`, which you can delete once the restored instance works. A new volume has no `/data/backups` until the first backup, hence the `install -d`; `docker compose cp` writes the file as root, hence the `chown`.

Install from source, after following [install-from-source.md](install-from-source.md) through the service unit:

```bash
sudo install -d -o perchtail -g perchtail -m 0700 /var/lib/perchtail/backups
sudo install -o perchtail -g perchtail -m 0600 ./<file> /var/lib/perchtail/backups/<file>
sudo systemctl stop perchtail      # if it was started
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup restore /var/lib/perchtail/backups/<file>
sudo systemctl start perchtail
```

The audit chain marker, `DATA_DIR/.audit-chain-started`, records outside the database that the audit log's hash chain has started (see [audit-events.md](audit-events.md#integrity)); it isn't part of the backup. The restore writes it again in the new `DATA_DIR` when the backup's database has a chain, so stripping the restored log's signatures is still reported; when the backup has none (made before the chain existed), it's left out and the first start signs those entries once, as on an upgrade. The previous marker stays in the `before-restore` copy with the rest of the old `DATA_DIR`. Don't delete it by hand: without it, an audit log whose signatures were all removed would be signed again on the next start.

## Rolling back an upgrade

An upgraded PerchTail migrates the database on its first start, and the previous version refuses to start on it (`startup.database_newer_than_app`). Migrations aren't undone with their data: go back by restoring the backup made before the upgrade ([upgrade.md](upgrade.md#rolling-back)).

1. Stop PerchTail.
2. Put the previous version back: the previous image tag in `docker-compose.yml`, or the previous tag checked out and installed as in [install-from-source.md](install-from-source.md#upgrading). On Kubernetes, follow [kubernetes.md](kubernetes.md#rolling-back-an-upgrade).
3. Restore the `pre-upgrade` backup made before the upgrade (`python -m app.backup list` shows it), as in [Restoring](#restoring). On PostgreSQL, drop the database and create it again, empty, first: it is at the newer version's revision, which the previous version refuses.
4. Start PerchTail.

Anything changed between the upgrade and the rollback (new sources, rules, users, audit entries) is lost with it; on SQLite it stays in the `before-restore` copy, on PostgreSQL only in a backup made before you dropped the database.

Going back to a release before 0.7.0, which has no `app.backup` command: stop PerchTail, put the copy of `DATA_DIR` made before the upgrade back in place of the current one, and start the previous version.

## PostgreSQL

On PostgreSQL ([configuration.md](configuration.md#postgresql)) the commands, the file, its encryption and its checks are the same as on SQLite. What differs is how the database gets in and out of the file.

**Making a backup.** PerchTail reads every one of its tables, with each row's id, from one `REPEATABLE READ READ ONLY` transaction, so the backup is a single consistent snapshot while PerchTail keeps writing, and writes them into a SQLite file with the database's Alembic revision. That file is what the backup holds as its database, checked with `quick_check` like a SQLite one; `pg_dump` isn't needed or used. The manifest says `"database": "postgresql"` and has format 2, which a PerchTail release without PostgreSQL support refuses as an unknown format. The command connects as `DATABASE_URL` says, with the same TLS options and checks as PerchTail itself, and never logs the URL's password. It backs up a database at this version's revision only, which is always the case for the scheduled backup; run `python -m app.backup create` with the version PerchTail runs.

**What is copied.** Every table of PerchTail: configuration, users and roles, encrypted secrets, the audit log with its hash chain, alerts and the whole file-name index. Not copied, and not needed: the trigram search index (rebuilt from the file-name index as rows are written back) and the `alembic_version` table (the restore writes the backup's revision). Nothing else on the server is: other databases or tables, roles and their passwords, extensions, server settings, the TLS certificates. Those belong to the PostgreSQL server's own backups.

**Time and space.** The copy goes through PerchTail row by row, so it is slower than `pg_dump`, which matters only for a large file-name index: it holds one row per rule-visible file, usually most of the database. While it runs, `BACKUP_DIR` holds the uncompressed SQLite copy, about the size of PerchTail's data in the database, and then the encrypted backup, about as large again; `verify` and `restore` need room for one copy. The snapshot keeps a transaction open on the server for as long as the copy takes, which holds back the cleanup of rows changed meanwhile (VACUUM) until it ends; schedule large backups at a quiet time. As an order of magnitude: on a laptop with PostgreSQL on the same host, a database of 157 MiB holding 500,000 file-name index entries took 11 seconds to back up, into a 73 MiB file, and 35 seconds to restore, most of it rebuilding the trigram search index. Time a backup and a restore once on a copy of your data, and check that the scheduled backup ends well within its interval.

**Restoring.** As on SQLite, PerchTail must be stopped: the restore takes the `DATA_DIR` lock and the database's own lock, and refuses while another PerchTail holds either. The database must be the one `DATABASE_URL` names, either empty (a new database: the restore creates the schema first) or at this version's revision, and the backup must have been made at this version's revision, since rows are written through this version's tables. Then, in one transaction, it empties every PerchTail table, writes the backup's rows with their ids, moves the id sequences past them and checks that every table has the backup's number of rows; it swaps `DATA_DIR` as on SQLite, and only then commits. If anything fails, before or after the swap, the transaction is rolled back and `DATA_DIR` is put back: the database and `DATA_DIR` are as they were. A table locked by another session (a `psql` left in a transaction) fails the restore after 30 seconds instead of holding it.

The database's content before the restore isn't kept: there is no `before-restore` copy of the database, only of `DATA_DIR`. Make a backup first if you may want it back. To restore a backup made by an older version (after an upgrade, to roll it back), drop the database and create it again, empty, restore the backup with the version that made it, and start the version you want, which migrates it ([Rolling back an upgrade](#rolling-back-an-upgrade)).

**Another engine.** A backup is restored on the engine it was made on: `verify` and `restore` refuse a SQLite backup on PostgreSQL and a PostgreSQL backup on SQLite. To move a SQLite installation to PostgreSQL, use `python -m app.migrate_database` ([upgrade.md](upgrade.md#moving-from-sqlite-to-postgresql)); there is no way back from PostgreSQL to SQLite.

**PostgreSQL's own backups.** Back up the PostgreSQL server as well, with its own tools or your provider's: `pg_dump`, base backups with WAL archiving, point-in-time recovery, snapshots. They cover the server, which PerchTail's backup doesn't, and can restore to any moment. They don't hold what is in `DATA_DIR`: keep PerchTail's backups too, or `DATA_DIR` (at least the credential salt) and the key alongside them, since the secrets in the database can't be decrypted without the salt and `CREDENTIAL_ENCRYPTION_KEY`.

## Keeping a copy off the host

A backup on the same disk as the database doesn't survive the loss of that disk or host. Copy the files in `BACKUP_DIR` somewhere else on a schedule: another server, object storage, your existing backup system. They're encrypted, so the copy doesn't need to be trusted with their content, but keep the keys out of that place: a backup is only as safe as the key stored next to it. Keep a copy of the keys themselves somewhere you can reach when the host is gone: without `CREDENTIAL_ENCRYPTION_KEY` (and `BACKUP_ENCRYPTION_KEY`, when it's set), no backup can be restored.

## Testing a restore

A backup that has never been restored is a guess. Every few months, and after changing keys, restore a recent backup on a separate test instance (its own host or containers, its own `DATA_DIR`, the same keys) as in [Restoring on a new machine](#restoring-on-a-new-machine), and check that it starts, that you can sign in, and that a source opens: that last step proves the credentials decrypt. `verify` proves the file is intact and readable with the current keys; only a restore proves the whole procedure, keys included, works.

A test instance holds the same sources and alerts as production: while it runs, it checks those sources in the background and its alerts call the same webhooks. Stop it once the test is done.
