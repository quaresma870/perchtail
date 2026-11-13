# Installing from source (without Docker)

Docker is the supported way to run PerchTail (see the README's Quick start).
This guide is for running it directly on a Linux host instead, for example
on a VM where you'd rather not run Docker. You get the same pinned Python
packages and the same frontend as the Docker image.

The guide was tested on Ubuntu 24.04. Run it as an ordinary admin account
with `sudo`. Commands without `sudo` run as that account; only installing
and the service itself need root.

## 1. Prerequisites

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv curl xz-utils rsync
```

Node.js 22 is needed only to build the frontend. Ubuntu's own package is
older (18.x), so take the official build from nodejs.org and check it
against the published checksums:

```bash
cd ~
NODE_DIST=https://nodejs.org/dist/latest-v22.x
curl -fsSLO "$NODE_DIST/SHASUMS256.txt"
NODE_TARBALL=$(grep -o 'node-v22[^ ]*-linux-x64.tar.xz' SHASUMS256.txt)   # linux-arm64 on ARM
curl -fsSLO "$NODE_DIST/$NODE_TARBALL"
grep " $NODE_TARBALL\$" SHASUMS256.txt | sha256sum -c -      # must print "OK"
tar -xJf "$NODE_TARBALL"
export PATH="$HOME/${NODE_TARBALL%.tar.xz}/bin:$PATH"
node --version                                                # v22.x
```

NodeSource's `nodejs` 22 package works too, if you prefer apt.

## 2. Build, as your own user

Pick the **latest release** from the
[Releases page](https://github.com/quaresma870/perchtail/releases). Use a
full release, not a pre-release, unless you are testing one.

```bash
PERCHTAIL_TAG=v0.5.2          # the latest release tag
git clone https://github.com/quaresma870/perchtail.git ~/perchtail-build
cd ~/perchtail-build
git checkout "$PERCHTAIL_TAG"
grep APP_VERSION backend/app/version.py   # must match the tag, without the "v"

cd frontend
npm ci
npm run build                 # writes frontend/dist, which the backend serves
cd ..
```

## 3. Install

```bash
sudo useradd --system --home-dir /opt/perchtail --no-create-home --shell /usr/sbin/nologin perchtail

sudo mkdir -p /opt/perchtail
sudo rsync -a --chown=root:root --delete --exclude .git --exclude frontend/node_modules ~/perchtail-build/ /opt/perchtail/

sudo python3.12 -m venv /opt/perchtail-venv
sudo /opt/perchtail-venv/bin/pip install -r /opt/perchtail/backend/requirements.txt
```

The code and the venv belong to root, and the service only reads them.
`--chown=root:root` matters: `rsync -a` would otherwise keep the build
account as the owner, and that account could then change the code the
service runs. Check that nothing else owns a file there:

```bash
sudo find /opt/perchtail /opt/perchtail-venv ! -user root   # must print nothing
```

`backend/requirements.txt` pins every package to the same versions the
Docker image installs. The version ranges they're generated from are in
`backend/requirements.in`.

## 4. Where state lives

The defaults (`./data/db`, `./data/logs`, `./data/scratch`) are relative to
the directory PerchTail starts in, which suits development. On a server,
use absolute paths:

| Setting | Path | Contents |
|---|---|---|
| `DATA_DIR` | `/var/lib/perchtail/db` | database, credential salt, SSH `known_hosts` (back this up) |
| `LOG_DIR` | `/var/log/perchtail` | the app's own logs, rotated by size and day |
| `SCRATCH_DIR` | `/var/lib/perchtail/scratch` | short-lived fetches, purged on close |
| `BACKUP_DIR` | `/var/lib/perchtail/backups` (the default next to `DATA_DIR`) | encrypted backups made by `python -m app.backup` |

```bash
sudo install -d -o perchtail -g perchtail -m 0700 /var/lib/perchtail /var/lib/perchtail/db /var/lib/perchtail/scratch
sudo install -d -o perchtail -g perchtail -m 0750 /var/log/perchtail
```

Put the three on separate filesystems where you can (at least `SCRATCH_DIR`
apart from `DATA_DIR`): scratch holds every file being opened or zipped, and
a full disk under it would also stop the database and the logs. PerchTail
refuses a new fetch (HTTP 507) when scratch reaches `SCRATCH_MAX_GB`, or when
its filesystem is down to `SCRATCH_MIN_FREE_GB` / `SCRATCH_MIN_FREE_PERCENT`
free (1 GiB or 5% by default, whichever is larger), but that guard only
covers scratch. A folder ZIP fetches and checks the folder's `.zip` and
`.tar.gz` files before the ZIP starts, and they wait in scratch for their
turn, so size scratch for all the archives in the largest folder you zip.

Keep `DATA_DIR` on a local disk. The database runs in SQLite's WAL mode,
which doesn't work over a network share. On every start, PerchTail sets
`DATA_DIR` so that only its own user can read it (directory `0700`, files
`0600`), and `BACKUP_DIR` too.

Back up `DATA_DIR` with `python -m app.backup`, run as the `perchtail` user with the service's environment file, while PerchTail runs:

```bash
sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup create
```

[backup-and-restore.md](backup-and-restore.md) covers scheduled backups, the keys, checking and restoring a backup, and copying backups off the host.

## 5. Configure

Settings go in an environment file that only root can read. systemd loads
it into the service.

```bash
sudo install -d -m 0755 /etc/perchtail
sudo install -m 0600 /opt/perchtail/.env.example /etc/perchtail/perchtail.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # a new CREDENTIAL_ENCRYPTION_KEY
sudoedit /etc/perchtail/perchtail.env
```

At a minimum, set:

```ini
CREDENTIAL_ENCRYPTION_KEY=<the value printed above>
DATA_DIR=/var/lib/perchtail/db
LOG_DIR=/var/log/perchtail
SCRATCH_DIR=/var/lib/perchtail/scratch
PUBLIC_BASE_URL=https://perchtail.example.com
```

The file documents every other setting.

## 6. Run it as a service

Create `/etc/systemd/system/perchtail.service`:

```ini
[Unit]
Description=PerchTail log viewer
After=network-online.target
Wants=network-online.target

[Service]
User=perchtail
Group=perchtail
EnvironmentFile=/etc/perchtail/perchtail.env
WorkingDirectory=/opt/perchtail/backend
ExecStartPre=/opt/perchtail-venv/bin/python -m app.data_layout
ExecStartPre=/opt/perchtail-venv/bin/python -m alembic upgrade head
ExecStart=/opt/perchtail-venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/perchtail /var/log/perchtail

[Install]
WantedBy=multi-user.target
```

Then start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now perchtail
curl -fsS http://127.0.0.1:8000/healthz
sudo journalctl -u perchtail | grep initial_super_admin.seeded
```

On the first start, PerchTail creates the break-glass super-admin and logs
its generated password once, in the `temporary_password` field of
`initial_super_admin.seeded`. Note it down; you change it at first login.

On every start, `python -m app.data_layout` moves a layout from before
0.5.0 into `DATA_DIR` (a fresh install has nothing to move) and tightens
its permissions. `alembic upgrade head` then creates or migrates the
database.

Run a single uvicorn worker, as the Docker image does. The scheduler,
login throttling and agent connections live in the process (see
[high-availability.md](high-availability.md)). Put a reverse proxy with TLS
in front, as described in the README under "Deployment: reverse proxy
(nginx) + TLS". Never expose PerchTail to the internet without
authentication in front of it.

## Upgrading

```bash
PERCHTAIL_TAG=v0.5.3          # the new release
cd ~/perchtail-build
git fetch --tags && git checkout "$PERCHTAIL_TAG"
(cd frontend && npm ci && npm run build)

sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.backup create --reason pre-upgrade
sudo systemctl stop perchtail
sudo rsync -a --chown=root:root --delete --exclude .git --exclude frontend/node_modules ~/perchtail-build/ /opt/perchtail/
sudo /opt/perchtail-venv/bin/pip install -r /opt/perchtail/backend/requirements.txt
sudo systemctl start perchtail          # migrates on start
```

Read the release's CHANGELOG entry first; it says when an upgrade needs
anything more than this.

The first command makes an encrypted `pre-upgrade` backup while the old version still runs.

Upgrading from a release before 0.7.0: the command doesn't exist there yet, so stop PerchTail and copy `DATA_DIR` (and, for a source install, the database and salt files if they live outside it) as before. In place of the `systemd-run` command, after `sudo systemctl stop perchtail`:

```bash
sudo cp -a /var/lib/perchtail/db "/var/lib/perchtail/db.backup-$(date +%F)"
```

To roll the upgrade back, check out and install the previous tag the same way, restore that backup with PerchTail stopped, and start it again ([backup-and-restore.md](backup-and-restore.md#rolling-back-an-upgrade)).

If you installed with this guide before 0.5.2, `/opt/perchtail` belongs
to your build account. Fix it once with
`sudo chown -R root:root /opt/perchtail`.
