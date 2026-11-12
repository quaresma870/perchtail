# Backup and restore

PerchTail keeps three things that can't be rebuilt: the SQLite database, the credential salt (`credential_salt`) and SSH `known_hosts`, all in `DATA_DIR` (`/data/db` in the Docker image). The database holds the sources, rules, users, roles, settings and audit log; the salt and your `CREDENTIAL_ENCRYPTION_KEY` together decrypt every stored credential, SSO secret and MFA secret, so each is useless without the other. Logs and scratch don't need a backup.

`python -m app.backup` backs these up while PerchTail runs, into one encrypted file, checks a backup, and restores one. The sections below cover:

- [Making a backup](#making-a-backup): by hand, on a schedule, and before an upgrade.
- [The key](#the-key): which key encrypts a backup, and what a key rotation means for older backups.
- [Checking a backup](#checking-a-backup).
- [Restoring](#restoring), step by step.
- [Rolling back an upgrade](#rolling-back-an-upgrade).
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

The backup is taken online: SQLite's backup API copies the database in one consistent snapshot while PerchTail keeps writing, and the copy is checked with `PRAGMA quick_check` before it's packed. The file lands in `BACKUP_DIR` (`/data/backups` in the image; by default `backups` next to `DATA_DIR`, so `/var/lib/perchtail/backups` with the install guide's paths), named `perchtail-<version>-<UTC time>-<reason>.ptbackup`, readable by PerchTail's user only. `python -m app.backup list` lists them.

PerchTail keeps the last `BACKUP_KEEP` (7) manual and scheduled backups and the last 3 pre-upgrade ones, and deletes only the oldest of the same kind when a new one is made.

### On a schedule

Settings → System → Backups → **Back up every** sets an interval from 1 hour to a week; 0 (the default) turns scheduled backups off. The same section lists the backups on the server, with their date, size and reason, for whoever manages the system settings. Backups are never downloadable from the web interface or the API: copy them off the host yourself (see below).

A scheduled backup that fails logs `backup.create_failed` and fails the `backup` job in `/monitoring/health` ([monitoring.md](monitoring.md)). After a failed attempt, the next one waits an hour (or the interval, if that's shorter), so a full disk doesn't fill the audit log with an entry every few minutes; a restart of PerchTail tries again straight away, and a backup that works brings back the normal interval.

### Before an upgrade

The upgrade steps (the README, [install-from-source.md](install-from-source.md#upgrading) and the Updates card under Settings → System) start with a backup made with `--reason pre-upgrade`, while the old version still runs. Those are kept apart from the others, so a burst of scheduled backups never deletes the one you need to roll back.

Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before.

## The key

Every backup is encrypted, always: AES-256-GCM in 1 MiB blocks, with a key derived (HKDF-SHA256, a random salt per file) from:

- `BACKUP_ENCRYPTION_KEY`, when it's set; or
- `CREDENTIAL_ENCRYPTION_KEY` otherwise.

The file's header says which of the two was used. The key itself never enters the file or the logs. Each block is authenticated, together with the header, its position and whether it's the last one, so a changed byte, a missing or reordered block, a truncated file or an edited header all fail the check.

Without `BACKUP_ENCRYPTION_KEY`, the key you already guard for the credentials also opens the backups. Setting `BACKUP_ENCRYPTION_KEY` gives backups a key of their own: keep it apart from the backups and from `CREDENTIAL_ENCRYPTION_KEY` (another vault entry, another person), so that a copied backup, or a leaked credential key, isn't enough to open one. A backup made with `BACKUP_ENCRYPTION_KEY` only decrypts with it. Restoring needs both keys anyway: the backup key to open the file, and the `CREDENTIAL_ENCRYPTION_KEY` the credentials inside were encrypted with.

**After a key rotation** ([credential-key-rotation.md](credential-key-rotation.md)), backups made before it still hold credentials encrypted under the old `CREDENTIAL_ENCRYPTION_KEY`, and, without `BACKUP_ENCRYPTION_KEY`, are themselves encrypted under it. Keep the old key for as long as you keep those backups, and make a new backup right after the rotation.

## Checking a backup

```bash
docker compose exec -u perchtail perchtail python -m app.backup verify /data/backups/<file>
```

`verify` decrypts the whole file and checks every block, the manifest (format, app version, Alembic revision, time, reason), each file's SHA-256 and the database's `quick_check`, without restoring anything. It works while PerchTail runs. Each `create`, `verify` and `restore` is recorded in the audit log as `backup.create`, `backup.verify` and `backup.restore`, with the file name, the reason and the result.

## Restoring

A restore replaces the database, the salt and `known_hosts` with the backup's. It needs PerchTail stopped, and refuses, changing nothing, when:

- PerchTail is running on that `DATA_DIR` (it takes the same lock a second instance would);
- the file doesn't decrypt with the current key, or fails any check `verify` makes;
- the backup's database is at an Alembic revision this version doesn't know (it was made by a newer PerchTail: restore it with that version);
- the current `CREDENTIAL_ENCRYPTION_KEY` doesn't decrypt the credentials in the backup. Set the key the backup was made with. `--force` restores anyway, and every source credential, SSO secret and MFA secret then has to be entered again; it never skips the file's own decryption and checks.

Docker:

```bash
docker compose stop perchtail
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.backup restore /data/backups/<file>
docker compose up -d
```

`--entrypoint python` keeps the container from starting PerchTail itself; `docker compose run` reads the same `.env`.

Install from source:

```bash
sudo systemctl stop perchtail
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup restore /var/lib/perchtail/backups/<file>
sudo systemctl start perchtail
```

The current `DATA_DIR` is moved, whole, to `<DATA_DIR>.before-restore-<UTC time>` (for example `/data/db.before-restore-20261112T101500Z`), and the backup's files are written to a fresh `DATA_DIR`, without the old `-wal` and `-shm` files. When `DATA_DIR` is its own volume, which can't be renamed, its contents are moved instead. A database, salt or `known_hosts` that `DATABASE_URL`, `CREDENTIAL_SALT_PATH` or `SSH_KNOWN_HOSTS_PATH` puts outside `DATA_DIR` is renamed to `<file>.before-restore-<UTC time>` next to itself. Delete the `before-restore` copies once the restored instance works.

The restored database can't hold the record of its own restore, so the restore is logged (`backup.restore`, [monitoring.md](monitoring.md#log-events-worth-alerting-on)) and the first start on it writes the `backup.restore` audit entry. On start, PerchTail migrates the restored database to its own version, as after any upgrade.

## Rolling back an upgrade

An upgraded PerchTail migrates the database on its first start, and the previous version refuses to start on it (`startup.database_newer_than_app`). To go back:

1. Stop PerchTail.
2. Put the previous version back: the previous image tag in `docker-compose.yml`, or the previous tag checked out and installed as in [install-from-source.md](install-from-source.md#upgrading).
3. Restore the `pre-upgrade` backup made before the upgrade (`python -m app.backup list` shows it), as in [Restoring](#restoring).
4. Start PerchTail.

Anything changed between the upgrade and the rollback (new sources, rules, users, audit entries) is lost with it; it stays in the `before-restore` copy.

## Keeping a copy off the host

A backup on the same disk as the database doesn't survive the loss of that disk or host. Copy the files in `BACKUP_DIR` somewhere else on a schedule: another server, object storage, your existing backup system. They're encrypted, so the copy doesn't need to be trusted with their content, but keep the keys out of that place: a backup is only as safe as the key stored next to it. Keep a copy of the keys themselves somewhere you can reach when the host is gone: without `CREDENTIAL_ENCRYPTION_KEY` (and `BACKUP_ENCRYPTION_KEY`, when it's set), no backup can be restored.

## Testing a restore

A backup that has never been restored is a guess. Every few months, and after changing keys, restore a recent backup on a separate test instance (its own host or containers, its own `DATA_DIR`, the same keys) and check that it starts, that you can sign in, and that a source opens: that last step proves the credentials decrypt. `verify` proves the file is intact and readable with the current keys; only a restore proves the whole procedure, keys included, works.
