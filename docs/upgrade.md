# Upgrading PerchTail

This guide explains, for the administrators who run PerchTail, how to upgrade the server (Docker or an install from source) and its push agents, what happens to the database, and how to roll an upgrade back.

Related guides: [admin-guide.md](admin-guide.md), [configuration.md](configuration.md), [backup-and-restore.md](backup-and-restore.md), [agent-service.md](agent-service.md) (the push agent as a service), [troubleshooting.md](troubleshooting.md), [monitoring.md](monitoring.md), [install-from-source.md](install-from-source.md) and [user-guide.md](user-guide.md).

**Contents:** [Before you upgrade](#before-you-upgrade) · [Upgrading with Docker](#upgrading-with-docker) · [Upgrading an install from source](#upgrading-an-install-from-source) · [Database migrations](#database-migrations) · [Upgrading push agents](#upgrading-push-agents) · [The Updates card](#the-updates-card) · [Rolling back](#rolling-back)

## Before you upgrade

1. **Read the release notes.** Each release in [CHANGELOG.md](../CHANGELOG.md) (also on the [GitHub release page](https://github.com/quaresma870/perchtail/releases)) has an `### Upgrading` section when an upgrade needs more than replacing the version: a setting to add, a behaviour that changed, an agent to upgrade by hand. Read it for every release between yours and the target. For example, 0.7.0's notes say that `/healthz` and `/readyz` now need authorization, and that push agents older than 0.7 must be upgraded by hand once.
2. **Check which version runs.** **Settings → System**, card **Updates** (super admins), shows it under **Running**; so does the `version` field of `/monitoring/health`.
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
- **Migrations change data.** Some drop tables or rewrite rows (0.7.0 drops the content index's tables, and gives `manage_source` to existing grants); read the release's `### Upgrading` notes.

## Upgrading push agents

The server never upgrades itself, and push agents aren't upgraded with it. An agent older than the server keeps connecting; **Settings → Agents** and the source's page show its version and flag it as outdated. An agent is only ever upgraded to the server's own version, so upgrade the server first.

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

**Settings → System**, card **Updates**, is shown to super admins. PerchTail can't upgrade itself: the card tells you that a release is out and how to install it.

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

The steps shown for an install from source are those of [Upgrading an install from source](#upgrading-an-install-from-source), with the paths of [install-from-source.md](install-from-source.md); adjust them if your paths differ.

## Rolling back

Migrations aren't undone with their data: a downgrade can't bring back a table or a value an upgrade dropped. Don't use `alembic downgrade` to roll back. Restore the backup made before the upgrade with the previous version instead:

1. Stop PerchTail.
2. Put the previous version back: the previous image tag in `docker-compose.yml`, or the previous tag checked out and installed.
3. Restore the `pre-upgrade` backup ([backup-and-restore.md](backup-and-restore.md#rolling-back-an-upgrade)).
4. Start PerchTail.

Everything changed since the upgrade (sources, rules, users, audit entries) is lost; it stays in the `before-restore` copy the restore leaves next to `DATA_DIR`.

Going back to a release before 0.7.0, which has no restore command: stop PerchTail, put the copy of `DATA_DIR` made before the upgrade back in place, and start the previous version.

Push agents upgraded in between stay on the newer version: an agent never downgrades itself. To put an older agent back, install its binary by hand.
