# Installing from source (without Docker)

Docker is the supported way to run PerchTail (see the README's Quick start).
Use this guide to run it straight on a Linux host instead, for example a VM
where you'd rather not run Docker. The result is the same app: the same
pinned Python packages as the image and the same frontend bundle.

## Requirements

- Python 3.12 or later
- Node.js 22, only to build the frontend; it isn't needed at run time
- Git

## 1. Get the code and build the frontend

```bash
sudo useradd --system --home-dir /opt/perchtail --shell /usr/sbin/nologin perchtail
sudo git clone https://github.com/quaresma870/perchtail.git /opt/perchtail
cd /opt/perchtail
git checkout v0.5.1          # the release you want

cd frontend
npm ci
npm run build                # writes frontend/dist, which the backend serves
cd ..
```

## 2. Install the backend

```bash
python3.12 -m venv /opt/perchtail/venv
/opt/perchtail/venv/bin/pip install -r backend/requirements.txt
```

`backend/requirements.txt` pins every package to an exact version, the
same set the Docker image installs. `backend/requirements.in` holds the
version ranges it is generated from.

## 3. Choose where state lives

The defaults (`./data/db`, `./data/logs`, `./data/scratch`) are relative to
the directory PerchTail starts in, which suits development. On a server,
set absolute paths:

| Setting | Suggested path | Contents |
|---|---|---|
| `DATA_DIR` | `/var/lib/perchtail/db` | database, credential salt, SSH `known_hosts` (back this up) |
| `LOG_DIR` | `/var/log/perchtail` | the app's own logs, rotated by size and day |
| `SCRATCH_DIR` | `/var/lib/perchtail/scratch` | short-lived fetches, purged on close |

```bash
sudo install -d -o perchtail -g perchtail -m 0700 /var/lib/perchtail /var/lib/perchtail/db /var/lib/perchtail/scratch
sudo install -d -o perchtail -g perchtail -m 0750 /var/log/perchtail
```

Keep `DATA_DIR` on a local disk: the database runs in SQLite's WAL mode,
which doesn't work over a network share. On startup PerchTail makes
`DATA_DIR` readable by its own user only (directory `0700`, files `0600`).

## 4. Configure

PerchTail reads `.env` from the directory it starts in, here `backend/`:

```bash
sudo cp .env.example backend/.env
sudo chown perchtail:perchtail backend/.env
sudo chmod 0600 backend/.env
```

Edit `backend/.env`. At minimum:

```ini
CREDENTIAL_ENCRYPTION_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(32))">
DATA_DIR=/var/lib/perchtail/db
LOG_DIR=/var/log/perchtail
SCRATCH_DIR=/var/lib/perchtail/scratch
PUBLIC_BASE_URL=https://perchtail.example.com
```

`.env.example` documents every other setting.

## 5. First start

Run these as the `perchtail` user, from `backend/`. The systemd unit below
runs the same steps on every start.

```bash
cd /opt/perchtail/backend
sudo -u perchtail /opt/perchtail/venv/bin/python -m app.data_layout
sudo -u perchtail /opt/perchtail/venv/bin/python -m alembic upgrade head
sudo -u perchtail /opt/perchtail/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On the very first start PerchTail creates the break-glass super-admin and
logs its generated password once (`initial_super_admin.seeded`, field
`temporary_password`). Note it down, then stop uvicorn with Ctrl+C.

`python -m app.data_layout` moves a layout from before 0.5.0 into
`DATA_DIR` and tightens its permissions; on a fresh install it has nothing
to move. `alembic upgrade head` creates or migrates the database.

## 6. Run it as a service

`/etc/systemd/system/perchtail.service`:

```ini
[Unit]
Description=PerchTail log viewer
After=network-online.target
Wants=network-online.target

[Service]
User=perchtail
Group=perchtail
WorkingDirectory=/opt/perchtail/backend
ExecStartPre=/opt/perchtail/venv/bin/python -m app.data_layout
ExecStartPre=/opt/perchtail/venv/bin/python -m alembic upgrade head
ExecStart=/opt/perchtail/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
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

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now perchtail
curl -fsS http://127.0.0.1:8000/healthz
```

Run a single uvicorn worker, as the Docker image does: the scheduler,
login throttling and agent connections live in the process (see
[high-availability.md](high-availability.md)). Put a reverse proxy with TLS
in front of it, as described in the README ("Deployment: reverse proxy
(nginx) + TLS"), and never expose it to the internet without
authentication in front.

## Upgrading

```bash
cd /opt/perchtail
sudo systemctl stop perchtail
sudo cp -a /var/lib/perchtail/db /var/lib/perchtail/db.backup-$(date +%F)
sudo git fetch --tags && sudo git checkout v0.5.2      # the new release
(cd frontend && sudo npm ci && sudo npm run build)
sudo /opt/perchtail/venv/bin/pip install -r backend/requirements.txt
sudo systemctl start perchtail                          # migrates on start
```

Read the release's CHANGELOG entry first: it says when an upgrade needs
anything beyond this.
