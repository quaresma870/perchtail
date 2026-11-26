# Upgrading PerchTail

This guide explains, for the administrators who run PerchTail, how to upgrade the server (Docker or an install from source) and its push agents, what happens to the database, and how to roll an upgrade back.

Related guides: [admin-guide.md](admin-guide.md), [configuration.md](configuration.md), [backup-and-restore.md](backup-and-restore.md), [agent-service.md](agent-service.md) (the push agent as a service), [troubleshooting.md](troubleshooting.md), [monitoring.md](monitoring.md), [install-from-source.md](install-from-source.md) and [user-guide.md](user-guide.md).

**Contents:** [Before you upgrade](#before-you-upgrade) · [Upgrading with Docker](#upgrading-with-docker) · [Upgrading an install from source](#upgrading-an-install-from-source) · [Database migrations](#database-migrations) · [Upgrading push agents](#upgrading-push-agents) · [The Updates card](#the-updates-card) · [Rolling back](#rolling-back) · [Upgrading to 0.7.0](#upgrading-to-070)

Which releases still get fixes, and for how long, is in [Support and end of life](../SECURITY.md#support-and-end-of-life).

## Before you upgrade

1. **Read the release notes.** Each release in [CHANGELOG.md](../CHANGELOG.md) (also on the [GitHub release page](https://github.com/quaresma870/perchtail/releases)) has an `### Upgrading` section when an upgrade needs more than replacing the version: a setting to add, a behaviour that changed, an agent to upgrade by hand. Read it for every release between yours and the target. For example, 0.7.0's notes say that `/healthz` and `/readyz` now need authorization, and that push agents older than 0.7 must be upgraded by hand once; [Upgrading to 0.7.0](#upgrading-to-070) goes through each of them.
2. **Check which version runs.** **Settings → Backups and updates**, card **Updates** (super admins), shows it under **Running**; so does the `version` field of `/monitoring/health`.
3. **Make a backup**, while the old version still runs. From 0.7.0 on:

   ```bash
   # Docker, in the folder with docker-compose.yml:
   docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade

   # Install from source:
   sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
     -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
     /opt/perchtail-venv/bin/python -m app.backup create --reason pre-upgrade
   ```

   `pre-upgrade` backups are kept apart from the others (the last 3), so scheduled backups never push out the one you need to roll back. The command refuses to run as root, hence `-u perchtail`. See [backup-and-restore.md](backup-and-restore.md).

   Upgrading from a release before 0.7.0: the command doesn't exist there yet. Stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) instead, for example `sudo cp -a /var/lib/perchtail/db "/var/lib/perchtail/db.backup-$(date +%F)"`.

You can upgrade across several releases in one step: the migrations run in order, from the database's revision to the new version's.

## Upgrading with Docker

With a published image (`image: ghcr.io/quaresma870/perchtail:<version>` in `docker-compose.yml`):

```bash
# In the folder with docker-compose.yml:
docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade
docker compose stop perchtail
# Set the image to ghcr.io/quaresma870/perchtail:<new version> in docker-compose.yml, then:
docker compose pull && docker compose up -d
docker compose logs -f perchtail
```

With an image built from a checkout (`build: .`, the default in `docker-compose.yml`):

```bash
docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade
git fetch --tags && git checkout v<new version>
docker compose build
docker compose up -d
```

An image built from a checkout doesn't carry the signed push-agent binaries, so agents upgraded from the portal download them from GitHub ([AGENT_BINARIES_DIR](configuration.md#updates-and-agent-upgrades)).

On start, the container's entrypoint (`backend/docker-entrypoint.sh`):

1. as root, gives everything under `/data` to the unprivileged `perchtail` user, then drops root;
2. runs `python -m app.data_layout`, which moves a layout from before 0.5.0 (database, salt and `known_hosts` directly in `/data`) into `/data/db`;
3. runs `python -m alembic upgrade head`, which migrates the database;
4. starts PerchTail.

The upgrade is done when the log shows `startup.complete` and the **Updates** card shows the new version under **Running**. If the container stops instead, the log says why; [troubleshooting.md](troubleshooting.md) covers the startup refusals.

## Upgrading an install from source

With the paths of [install-from-source.md](install-from-source.md):

```bash
PERCHTAIL_TAG=v<new version>
cd ~/perchtail-build
git fetch --tags && git checkout "$PERCHTAIL_TAG"
(cd frontend && npm ci && npm run build)

sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup create --reason pre-upgrade
sudo systemctl stop perchtail
sudo rsync -a --chown=root:root --delete --exclude .git --exclude frontend/node_modules ~/perchtail-build/ /opt/perchtail/
sudo /opt/perchtail-venv/bin/pip install --require-hashes -r /opt/perchtail/backend/requirements.txt
sudo systemctl start perchtail
```

The unit's `ExecStartPre` lines run `python -m app.data_layout` and `python -m alembic upgrade head` before PerchTail starts, so `systemctl start` migrates the database. To migrate by hand, with PerchTail stopped:

```bash
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m alembic upgrade head
```

In a development checkout, run `python -m alembic upgrade head` in `backend/`, with the same environment PerchTail uses.

Check `sudo journalctl -u perchtail` for `startup.complete`.

## Database migrations

- **PerchTail doesn't migrate by itself.** The Docker entrypoint and the systemd unit run `alembic upgrade head` before starting it. If you start `uvicorn` another way, run the migration first.
- **One process per `DATA_DIR`.** `alembic upgrade` takes the same lock as PerchTail, `DATA_DIR/.perchtail.lock`, and refuses to run while PerchTail runs on that `DATA_DIR` (`startup.instance_lock_held`, naming the host, PID, version and start time of the one running). Stop PerchTail first.
- **A database migrated by a newer version is refused by an older one.** PerchTail and `alembic upgrade` check the database's revision first; at a revision this version doesn't know, both stop with `startup.database_newer_than_app` and change nothing. That's what happens when you put the previous image back after an upgrade: run the newer version again, or restore the backup made before the upgrade ([Rolling back](#rolling-back)).
- **An interrupted upgrade is fixed by running it again.** If the migration stops half way (the container is killed, the host restarts), start the new version again, or run `alembic upgrade head` again. A table rebuild that stopped half way is finished or cleaned up on the next run, without losing rows (the log then says `An interrupted rebuild of table ... was finished`). If a table is gone and can't be recovered, the migration stops with `Table ... is missing` and changes nothing more. If it still fails, restore the `pre-upgrade` backup with the previous version ([Rolling back](#rolling-back)) and report the error.
- **Migrations change data.** Some drop tables or rewrite rows (0.7.0 drops the content index's tables, and gives `manage_source` to existing grants); read the release's `### Upgrading` notes.
- **Some migrations rebuild a table.** 0.7.0 rebuilds the audit log table: on a large audit log it takes a while, and needs free disk for a second copy of the table while it runs.

## Moving from SQLite to PostgreSQL

`python -m app.migrate_database` copies a SQLite installation into an empty PostgreSQL database: users, roles and grants, customers, folders, sources and their encrypted credentials, rules, settings, alerts, the file-name index and the audit log with its hash chain, each row with its own id. Everything happens in one transaction on PostgreSQL, checked before it is committed; the SQLite file is only read, never changed.

### Before you start

- **Upgrade first.** The command copies a database at this version's revision only: upgrade PerchTail as usual and let it start once on SQLite, which migrates the file.
- **Make a backup** while PerchTail still runs, and `verify` it ([backup-and-restore.md](backup-and-restore.md)): `docker compose exec -u perchtail perchtail python -m app.backup create`. `python -m app.backup` works on SQLite only for now, so this is the last backup it makes until it supports PostgreSQL.
- **Create the database**: empty, on PostgreSQL 18 or later, in UTF8, owned by the role PerchTail will connect as, ideally with the `C.UTF-8` locale of the `builtin` provider, and where that role may create the `pg_trgm` extension (see [configuration.md](configuration.md#postgresql)): `CREATE DATABASE perchtail OWNER perchtail ENCODING 'UTF8' LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8' TEMPLATE template0;`. The command creates the schema itself. Plan for room for the whole database on the server; a large file-name index takes a while to copy.
- **Check the audit chain.** The command refuses a database whose audit hash chain doesn't verify. In **Settings → Audit log**, click **Verify now** on the SQLite installation; if it reports breaks a super admin can record as known (**Mark as known**), record them first. A chain with any other break can't be migrated.
- **Keep `DATA_DIR`.** PerchTail on PostgreSQL still needs it: the credential salt, the SSH host keys and the audit chain marker stay there, and the command refuses to run without the salt. Keep the same `CREDENTIAL_ENCRYPTION_KEY`.

### Running it

Stop PerchTail, then point `DATABASE_URL` (or `DATABASE_URL_FILE`) at the PostgreSQL database in `.env`, with the TLS options it needs ([configuration.md](configuration.md#postgresql)). Run the command as PerchTail's own user, with the same environment as PerchTail; it refuses to run as root. `--from` names the SQLite file PerchTail used, `/data/db/perchtail.db` in the Docker image.

Docker, in the folder with `docker-compose.yml`:

```bash
docker compose stop perchtail
# Set DATABASE_URL in .env, then:

# Does everything, checks included, then rolls back: writes nothing.
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.migrate_database --from sqlite:////data/db/perchtail.db --dry-run

# The migration.
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.migrate_database --from sqlite:////data/db/perchtail.db
```

Install from source ([install-from-source.md](install-from-source.md)), with `DATABASE_URL` set in `/etc/perchtail/perchtail.env` and `<DATA_DIR>` the folder the SQLite file is in:

```bash
sudo systemctl stop perchtail
sudo systemd-run --pty --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.migrate_database --from sqlite:///<DATA_DIR>/perchtail.db --dry-run
# then the same command without --dry-run
```

The command refuses, and writes nothing, when:

- PerchTail, `alembic upgrade` or another migration runs on this `DATA_DIR` or on the PostgreSQL database;
- `--from` isn't a SQLite database file, or the file is at another revision than this version's ("start this version of PerchTail on it once");
- `DATABASE_URL` isn't a PostgreSQL database, or one PerchTail wouldn't start on (older than 18, not UTF8, or reached over the network without a verified certificate while `DATABASE_REQUIRE_VERIFIED_TLS` is `true`);
- the credential salt isn't where `CREDENTIAL_SALT_PATH` says;
- any of PerchTail's tables in the PostgreSQL database already holds a row: the message names them. There is no option to overwrite; drop the database and create it again.

Then, in one transaction, it creates the schema in an empty database, copies every table with its ids, and moves the id sequences past them. Before committing, it checks that every table has as many rows as in the SQLite file, that the current `CREDENTIAL_ENCRYPTION_KEY` decrypts every stored secret (source credentials, SSO provider settings, MFA secrets, the SMTP password, webhook URLs), that the audit hash chain verifies, and that the schema's revision is the file's. Only then does it record `database.migrate` in the audit log, chained after the copied entries ([audit-events.md](audit-events.md)), verify the chain again and commit. If anything fails, it rolls back, says what failed, and the PostgreSQL database is as it was. Secrets are copied encrypted, as stored; neither they nor the database's password are printed or logged. A row PostgreSQL refuses (one pointing at a row that is gone, which SQLite doesn't check) is reported by its table and the kind of error, never with its values. The log has a `database.migrate` event with the result (`ok`, `dry_run`, `refused` with the `reason`, or `failed`).

### Checking it

Start PerchTail. It runs on PostgreSQL from now on (the log's `startup.complete`; `GET /readyz`). Then:

- **Settings → Audit log** shows the `database.migrate` entry with the rows copied per table, and no integrity problem at the top;
- sign in with a local account and with SSO if you use it, open a file on a source of each protocol (its credential decrypts), and search for a file name;
- make a backup once `python -m app.backup` supports PostgreSQL, and keep the SQLite file and its backup until then.

### Going back

The SQLite file wasn't changed: stop PerchTail, remove `DATABASE_URL` from `.env` (or point it at the SQLite file again), and start PerchTail. Everything done on PostgreSQL in between (sources, rules, users, audit entries) isn't in the SQLite file and is lost.

## Upgrading push agents

The server never upgrades itself, and push agents aren't upgraded with it. An agent older than the server keeps connecting; **Settings → Agents** and the source's page show its version and flag it as outdated. An agent is only ever upgraded to the server's own version, so upgrade the server first.

From 0.7, the server reads files only from an agent that says where links lead (the `links/1` feature, which agents announce from 0.7 on; see [agent-service.md](agent-service.md#what-the-agent-tells-the-server)). After the server's upgrade, an older agent stays connected and its folders can still be browsed, but opening, Tail, downloads, folder ZIPs and archives are refused (409, "This source's agent must be updated before its files can be read…", audited with the reason `agent_update_required`) until it is upgraded; alerts on its files wait where they are. **Settings → Agents** shows "Update required" under such an agent's version. Agents older than 0.7 can only be upgraded [by hand](#by-hand), so plan it right after the server's upgrade.

### By hand

Agents older than 0.7 don't know the upgrade command: upgrade each of them by hand once. Any agent can be upgraded by hand.

1. Download `perchtail-agent-<version>-<os>-<arch>` (Linux amd64 or arm64, Windows amd64), `SHA256SUMS` and `SHA256SUMS.sig` from the [GitHub release](https://github.com/quaresma870/perchtail/releases), and check them as in [agent/README.md](../agent/README.md#download).
2. Stop the agent's service, replace its binary with the new one (keep the same file name and location), and start the service again. On Linux, make the new file executable (`chmod 755`).
3. Check in **Settings → Agents** that the agent reconnected with the new version.

[agent-service.md](agent-service.md) describes the service on each system.

### From the portal

From 0.7, **Settings → Agents** upgrades agents to the server's version. The page is shown to users with the `create_source` global capability; upgrading an agent also needs `manage_source` on its source. The settings at the top of the page (described in [configuration.md](configuration.md#settings--agents)) can be changed only by those who may manage system settings.

- **Remote upgrades**: **Off** (the default) allows no upgrade from the portal; **Manual** shows an **Upgrade** button on each outdated agent and **Upgrade all outdated agents**; **Automatic** also lets the server upgrade connected outdated agents inside the window. Each agent can **Follow the system** (the default) or have its own policy, in its row.
- **Automatic window**: days of the week, a start time and a length (1 to 24 hours), in the server's time zone, shown on the page (`TZ`, or UTC in the Docker image). A window may run past midnight. Inside it the server upgrades agents a few at a time (**At once**, 5 by default) and doesn't ask an agent again in the same window once its upgrade didn't work.
- **Failure limit** (3 by default): after that many failed, refused or timed-out upgrades in a row, automatic upgrades skip the agent until someone clicks **Resume**. A manual **Upgrade** still works on it.
- **History**: every attempt is kept with who asked (a user or the schedule), the versions, the times and the result; click an agent's last result to see it. Attempts are audited as `agent.upgrade`.

When asked, the agent downloads the release files from the server (`AGENT_BINARIES_DIR`, which release images fill) or, if the server has no valid copy, from the GitHub release. It installs the binary only if the release signature and the checksum verify and the version is newer than its own, keeps the previous binary as `<binary>.old`, and restarts into the new one. Anything that fails leaves the current version running, and the page shows why. An upgrade counts as done only when the agent reconnects with the new version, and as failed after 10 minutes without news.

On the agent's host:

- The agent's account must be able to write to the folder its binary is in.
- The service must restart the agent: `Restart=always` with systemd; on Windows, a wrapper that restarts it when it exits with code 3.
- `PERCHTAIL_REMOTE_UPGRADE=false` refuses every upgrade from the portal on that host.

See [agent/README.md](../agent/README.md#upgrades-from-the-portal) for the details and examples.

## The Updates card

**Settings → Backups and updates**, card **Updates**, is shown to super admins. PerchTail can't upgrade itself: the card tells you that a release is out and how to install it.

Once a day (when the switch is on and `UPDATE_CHECK` isn't `false`), PerchTail asks the GitHub releases API for the latest release. Nothing about the installation is sent, and pre-releases are never offered. The card shows:

- **Running**: the running version, and "Docker image" or "installed from source", from `PERCHTAIL_INSTALL` (the image sets it to `docker`).
- **Latest release**: its version, publication date and a **release notes** link, or "not known yet".
- **Last check**: when the last check ran, "couldn't check" with the error (`update_check.failed` in the log), or "off".
- When a newer release exists: "Version X is available", a reminder to read its release notes first, and the upgrade steps for the install method. Otherwise: "This is the latest release."
- **Check now** runs the check at once.

The steps shown for Docker:

```text
# In the folder with docker-compose.yml:
docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade
docker compose stop perchtail
# Set the image to:
#   ghcr.io/quaresma870/perchtail:<version>
docker compose pull && docker compose up -d
```

They assume `docker-compose.yml` uses the published image. If yours builds from a checkout (`build: .`), follow [Upgrading with Docker](#upgrading-with-docker) instead.

The steps shown for an install from source follow [Upgrading an install from source](#upgrading-an-install-from-source), with the paths of [install-from-source.md](install-from-source.md) and `pip install --require-hashes`; adjust them if your paths differ.

## Rolling back

Migrations aren't undone with their data: a downgrade can't bring back a table or a value an upgrade dropped. Don't use `alembic downgrade` to roll back. Restore the backup made before the upgrade with the previous version instead:

1. Stop PerchTail.
2. Put the previous version back: the previous image tag in `docker-compose.yml`, or the previous tag checked out and installed.
3. Restore the `pre-upgrade` backup ([backup-and-restore.md](backup-and-restore.md#rolling-back-an-upgrade)).
4. Start PerchTail.

Everything changed since the upgrade (sources, rules, users, audit entries) is lost; it stays in the `before-restore` copy the restore leaves next to `DATA_DIR`.

Going back to a release before 0.7.0, which has no restore command: stop PerchTail, put the copy of `DATA_DIR` made before the upgrade back in place, and start the previous version.

Push agents upgraded in between stay on the newer version: an agent never downgrades itself. To put an older agent back, install its binary by hand.

## Upgrading to 1.0.0

- **A weak `CREDENTIAL_ENCRYPTION_KEY` stops PerchTail from starting.** `CREDENTIAL_KEY_REFUSE_WEAK` now defaults to `true`. If 0.7 warned about your key (`startup.weak_credential_encryption_key` in the log, or "CREDENTIAL_ENCRYPTION_KEY is weaker than recommended." at the top of the system settings pages), rotate it with `python -m app.rotate_credential_key` before upgrading ([credential-key-rotation.md](credential-key-rotation.md)), or set `CREDENTIAL_KEY_REFUSE_WEAK=false` for the time it takes and remove it afterwards. The commands are in [troubleshooting.md](troubleshooting.md#perchtail-doesnt-start-weak-credential-key).
- **A YAML import that sets `search_indexing_enabled` is refused.** The setting no longer exists since 0.7; remove the key from your import files ([bulk-import.md](bulk-import.md#format)).

## Upgrading to 0.7.1

Upgrade the server, then the push agents (see "Upgrading push agents" above).

An `.env` (or `perchtail.env`) copied from an earlier `.env.example` sets `LOG_QUIET_THIRD_PARTY=false`: delete the line, or set it to `true`. From 0.7.1 on, `true` is the default, and with `LOG_LEVEL=DEBUG` it keeps the protocol libraries (SMB, SSH, WinRM, HTTP) at `INFO`, whose `DEBUG` messages can include what is read from sources, before redaction. While it is `false`, PerchTail logs `logging.third_party_debug` at startup. See [configuration.md](configuration.md).

## Upgrading to 0.7.0

0.7.0 changes more than a usual release. Each item below, except the closing note on redaction, is one entry of its `### Upgrading` notes in [CHANGELOG.md](../CHANGELOG.md), with what to do and where it is explained.

### Before and during the upgrade

- **Back up first** with `python -m app.backup create --reason pre-upgrade` once you run 0.7.0 or later. Coming from an earlier release, the command doesn't exist yet: copy `DATA_DIR` instead ([Before you upgrade](#before-you-upgrade)).
- **The migration rebuilds the audit log table**, so entry ids are never reused after a purge. On a large audit log it takes a while and needs free disk for a second copy of the table; the hash chain is unchanged. If it is interrupted, run it again ([Database migrations](#database-migrations)).
- **Alert webhook URLs are encrypted by the migration**, so it must run with the same `CREDENTIAL_ENCRYPTION_KEY` and salt as PerchTail (the Docker entrypoint and the systemd unit do). From then on an alert shows only the URL's scheme, host and port, and editing an alert without typing a new URL keeps the stored one.
- **The test hooks are refused.** `PERCHTAIL_TEST_PATCH_MODULE` is honoured only with `PERCHTAIL_TEST_MODE=true`, which only the end-to-end test server sets; set without it, PerchTail refuses to start, and the Docker image refuses both. A deployment sets neither ([configuration.md](configuration.md#other-variables)).

### Network, reverse proxy and monitoring

- **`/healthz` and `/readyz` need authorization**: a local caller without proxy headers (the Docker `HEALTHCHECK`), the monitoring token, or an address in `PROBE_ALLOWED_NETWORKS`. A load balancer or Kubernetes probe that called them without the token stops working until it sends the token or its network is listed ([monitoring.md](monitoring.md#who-may-call-them)).
- **Set `TRUSTED_PROXIES` behind a reverse proxy in another container or on another host.** It decides whose `X-Forwarded-For` is believed, for the address in the audit log, the monitoring allowlists and the sign-in lockout, which now also counts failures per client address: without it, every client has the proxy's address, and five failures by anyone lock everyone out. The default is loopback only. A forwarded header from a peer outside it logs `client_address.untrusted_forwarded_header` once, and `/monitoring/health` then reports `trusted_proxies_suspect: true` until the next restart ([configuration.md](configuration.md#monitoring-and-reverse-proxy), [troubleshooting.md](troubleshooting.md#too-many-failed-login-attempts-try-again-later)).
- **uvicorn runs with `--no-proxy-headers`**, so PerchTail alone reads `X-Forwarded-For`. The Docker image does it already; an install from source adds it to the `ExecStart` line of its systemd unit ([install-from-source.md](install-from-source.md#6-run-it-as-a-service)).
- **`docker-compose.yml` publishes PerchTail on `127.0.0.1` only**, with a read-only root filesystem, `no-new-privileges` and every capability dropped but the few the entrypoint needs. Other machines reach it through a reverse proxy that terminates TLS; to publish it on every address again, change the port to `"8080:8000"`. If you run your own compose file or `docker run`, the README lists the settings ([README](../README.md#quick-start)).
- **`/docs`, `/redoc` and `/openapi.json` answer 404** unless `API_DOCS=true` ([configuration.md](configuration.md#keys-security-and-sessions)).

### Sign-in and accounts

- **Administrators must use MFA.** Local accounts of super admins and of roles with any global capability must set up two-factor authentication: at their first sign-in after the upgrade they are taken to **Set up two-factor authentication** and can do nothing else until it is done. Make sure each of them has an authenticator app at hand. SSO accounts aren't affected ([admin-guide.md](admin-guide.md#users)).
- **The first super admin's temporary password is no longer logged.** A fresh install writes it to `DATA_DIR/initial-admin-password`. Existing installs aren't affected, but a temporary password logged by an earlier version stays in the rotated logs until they expire ([README](../README.md#quick-start)).
- **SSO accounts lose any PerchTail password** an admin's reset may have left on them, recorded in the audit log as `user.password_cleared` with reason `sso_account` ([audit-events.md](audit-events.md#administration)).
- **The SSO issuer must be `https://`**, as must the endpoints its discovery document names, and it must match the document's `issuer` exactly (a trailing slash aside). A provider saved with an `http://` issuer stops signing in until it is changed; **Test connection** checks it ([sso-setup.md](sso-setup.md#1-register-perchtail-as-an-oidc-client-at-your-idp)).
- **The identity provider must be on a public address**: an IdP reached at a private, loopback or link-local address stops signing in (`sso.login.discovery_failed` in the log) ([sso-setup.md](sso-setup.md#1-register-perchtail-as-an-oidc-client-at-your-idp)).
- **An SSO account that no group mapping matches drops to the no-access role** at its next sign-in. Before upgrading, check that every SSO user who should keep access is in a group with a mapping ([sso-setup.md](sso-setup.md#2-group-claim-to-role-mapping-optional)).
- **Only a super admin deletes or turns off the SSO provider.** An admin with **Manage SSO settings** short of super admin creates, changes and deletes only the group mappings whose role is within their own authority ([sso-setup.md](sso-setup.md#2-group-claim-to-role-mapping-optional)).
- **A new SSO account's username comes from `email` only when the ID token says `email_verified: true`**; existing accounts keep theirs ([sso-setup.md](sso-setup.md#how-a-sign-in-maps-to-an-account)).

### Roles, customers and grants

- **Manage sources is a capability of its own.** The migration gives `manage_source` to every grant with `manage_rules` in roles with **Create new sources**. Afterwards, take it off where a role should only edit rules, and give it where a role should add sources ([admin-guide.md](admin-guide.md#grant-capabilities)).
- **Only a super admin creates customers**, and a role with **Create new sources** sees and changes only the customers and folders its grants reach ([admin-guide.md](admin-guide.md#customers-and-folders)).
- **Duplicate grants are merged**: where a role had several grants on the same customer, folder or source, the migration keeps the one with the lowest id and records each deleted one as a `role_grant.delete` with reason `duplicate_scope`. Check those roles' grants ([audit-events.md](audit-events.md#administration)).
- **Sources take their folder's customer.** The migration gives a source saved in a folder with another customer, or none, the folder's customer, lists its id in the migration log and records it as a `source.update` with no user and reason `folder_customer`. Check those sources' grants ([admin-guide.md](admin-guide.md#customers-and-folders)).
- **Test connection and checking a source's status need Manage sources** on the source: a role with only **View** no longer sees **Test connection** or can click a status to check it again. Give `manage_source` to the grants that should keep it ([admin-guide.md](admin-guide.md#grant-capabilities)).
- **Only a super admin acts on other user or role admins**: resetting the password of, deactivating or changing the role of an account whose role has `manage_users` or `manage_roles` is refused to anyone else, audited as `access.denied` with reason `peer_admin` ([admin-guide.md](admin-guide.md#delegated-administrators)).

### Audit log

- **Audit retention is at least 90 days**, or `0` to keep entries forever: the migration raises a shorter retention to 90 and records it as a `system_settings.update`. Only a super admin can shorten it from now on ([admin-guide.md](admin-guide.md#retention)).
- **The audit chain is checked every day**: `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` now defaults to 1 ([admin-guide.md](admin-guide.md#integrity)).

### Keys and backups

- **A weak `CREDENTIAL_ENCRYPTION_KEY` is warned about** in the log and at the top of the system settings pages (for example **Settings → Explorer and limits**): change it with `python -m app.rotate_credential_key` ([credential-key-rotation.md](credential-key-rotation.md)). `CREDENTIAL_KEY_REFUSE_WEAK=true` refuses to start with one ([configuration.md](configuration.md#keys-security-and-sessions)); from 1.0.0 that is the default.
- **The scheduled backup is on by default for new installs**, every 24 hours. An existing install keeps it off: turn it on in **Settings → Backups and updates**, card **Backups** ([configuration.md](configuration.md#backups-card)).

### Sources, rules and archives

- **Push agents must be upgraded before their files can be read**, by hand for agents older than 0.7 ([Upgrading push agents](#upgrading-push-agents)).
- **Push agents can be upgraded from the portal from now on**, once they run 0.7. For that, the agent's account must be able to write to the folder its binary is in, and its service must restart it: `Restart=always` with systemd, and a restart on exit for a Windows service ([agent/README.md](../agent/README.md#upgrades-from-the-portal)).
- **A push agent checks `PERCHTAIL_BASE_PATH` at startup**: one whose folder doesn't exist, isn't a folder or can't be read by its account stops with exit code 1 and a message naming the problem, and a service manager set to restart it restarts it in a loop. Check the folder before upgrading an agent ([agent/README.md](../agent/README.md#configuration)).
- **Links are judged by where they lead** on local, SSH and agent sources: a symbolic link (or a junction on a Windows agent) to a file or folder the rules hide is no longer listed or read, even when its own name is visible. Check the rules of sources whose logs are reached through links ([source-setup.md](source-setup.md#principles-that-apply-to-every-protocol)).
- **Exclude rules ignore letter case on every source**, glob and `re:`; include rules are unchanged ([admin-guide.md](admin-guide.md#rules)).
- **SMB signing is a setting of each SMB source.** Every source keeps requiring signed messages; one whose server can't sign can turn **Require signing** off ([source-setup.md](source-setup.md#4-protocol-version-and-auth)).
- **A WinRM source reads only paths on a drive.** One saved with a UNC (`\\server\share`) or device (`\\?\`, `\\.\`) path stops being read: change it to a drive path of the WinRM host, or read the share through an SMB source ([source-setup.md](source-setup.md#base-path)).
- **A `.tar.gz` with more than 50,000 entries** lists only its first 50,000, and its members past them can't be opened: split it into smaller archives ([user-guide.md](user-guide.md#archives)).
- **Severity regexes use the rules' syntax** (RE2, at most 1000 characters). One saved before with lookaround or a backreference, or longer, keeps working, and can still be switched off, given another level or switches, or deleted; only a change to its pattern must follow the new rules ([admin-guide.md](admin-guide.md#severity-indicators)).

### Search and alerts

- **The content index and the Search page are gone.** The migration drops their tables; nothing can be kept. The `SEARCH_INDEX_*` variables are ignored ([configuration.md](configuration.md#other-variables)), and a YAML import that still sets `search_indexing_enabled` applies with a warning in 0.7 and is refused from 1.0.0 ([bulk-import.md](bulk-import.md#format)). File names are searched with the file-name index ([admin-guide.md](admin-guide.md#file-name-index)).
- **Alerts read new lines live.** Existing alerts start again at the end of each file they watch, and their query is now plain text or a regular expression: rewrite a query that used full-text operators such as `AND` or quotes ([admin-guide.md](admin-guide.md#creating-an-alert)).
- **Webhook calls changed shape**: each hit has `source_id`, `source_name`, `file_path`, `offset` and `line` instead of `line_number` and `snippet_html`. Update receivers that read `snippet_html` ([admin-guide.md](admin-guide.md#webhook-calls)).

### Redaction

Redaction is new in 0.7.0 and off by default, so the upgrade changes nothing until a super admin turns it on in **Settings → Redaction**. Once on, it applies to every user, super admins included; downloads are redacted too while **Redact downloads too** is on (the default); and on a push-agent source, an agent from 0.7 redacts on its own host ([redaction.md](redaction.md), [configuration.md](configuration.md#redaction)).
