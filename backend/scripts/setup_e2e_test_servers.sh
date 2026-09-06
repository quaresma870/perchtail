#!/usr/bin/env bash
# Stands up real, throwaway SSH and SMB test servers for the Playwright E2E
# suite (see frontend/e2e/sources-ssh.spec.ts, sources-smb.spec.ts) --
# actual sshd/smbd processes, not a mocked client. WinRM is the one
# exception (see backend/app/testing/fake_winrm.py's docstring for why).
#
# Deliberately plain system packages (openssh-server, samba) run as
# foreground processes on non-privileged ports, not Docker -- no daemon
# assumed to be available on the box running this (CI runner or a
# contributor's machine), and this needs no more privilege than sudo already
# grants a CI runner user. Everything it creates lives under
# backend/data/e2e/ and is wiped by run_e2e_server.sh on every run; the one
# exception is the "e2euser" OS account and its Samba password, which
# persist across runs (recreating a Linux user is real system state, not
# something to redo every time) but are inert outside this script's own
# sshd/smbd instances -- nothing else on the box authenticates against them.
#
# Usage: called by run_e2e_server.sh, not meant to be run standalone.
# Requires sudo (passwordless, as on a GitHub Actions runner) when not
# already root.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
E2E_DIR="$(pwd)/data/e2e"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# --- packages -----------------------------------------------------------
if ! command -v sshd >/dev/null || ! command -v smbd >/dev/null; then
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    openssh-server samba samba-common-bin
fi

SFTP_SERVER_PATH="$(dpkg -L openssh-sftp-server 2>/dev/null | grep -m1 'sftp-server$' || true)"
SFTP_SERVER_PATH="${SFTP_SERVER_PATH:-/usr/lib/openssh/sftp-server}"

# --- throwaway OS account -------------------------------------------------
# Real, persisted across runs (unlike everything under data/e2e/) since
# creating/deleting a Linux user on every test run is unnecessary churn --
# only this script's own sshd/smbd instances, on non-standard ports this
# script alone binds, ever authenticate against it.
E2E_SSH_USER="${E2E_SSH_USER:-e2euser}"
E2E_SSH_PASSWORD="${E2E_SSH_PASSWORD:-e2e-test-password!}"
if ! id -u "$E2E_SSH_USER" >/dev/null 2>&1; then
  $SUDO useradd -m -s /usr/sbin/nologin "$E2E_SSH_USER"
fi
printf '%s:%s\n' "$E2E_SSH_USER" "$E2E_SSH_PASSWORD" | $SUDO chpasswd

# --- fixture files ---------------------------------------------------------
SSH_ROOT="$E2E_DIR/ssh-root"
SMB_ROOT="$E2E_DIR/smb-root"
rm -rf "$SSH_ROOT" "$SMB_ROOT"
mkdir -p "$SSH_ROOT" "$SMB_ROOT"
printf 'ssh hello world log line 1\nssh hello world log line 2\n' > "$SSH_ROOT/hello.log"
printf 'ssh other log\n' > "$SSH_ROOT/other.log"
printf 'smb hello world log line 1\nsmb hello world log line 2\n' > "$SMB_ROOT/hello.log"
printf 'smb other log\n' > "$SMB_ROOT/other.log"
$SUDO chown -R "$E2E_SSH_USER:$E2E_SSH_USER" "$SSH_ROOT" "$SMB_ROOT"
chmod 755 "$SSH_ROOT" "$SMB_ROOT"

# --- sshd -----------------------------------------------------------------
SSH_ETC="$E2E_DIR/ssh-etc"
# run_e2e_server.sh kills a previous run's sshd/smbd by pidfile *before*
# wiping data/e2e/ (this script always finds a clean slate here) -- doing
# that check here instead would be too late, since this script only ever
# runs after that wipe has already deleted the pidfiles it would look for.
rm -rf "$SSH_ETC"
mkdir -p "$SSH_ETC"
ssh-keygen -q -t rsa -b 2048 -f "$SSH_ETC/host_rsa_key" -N ''
# sshd's privilege-separation directory -- not part of this script's own
# data/e2e/ sandbox since it's a fixed, well-known path sshd itself expects.
$SUDO mkdir -p /run/sshd
$SUDO chmod 755 /run/sshd

cat > "$SSH_ETC/sshd_config" <<EOF
Port 2222
ListenAddress 127.0.0.1
HostKey $SSH_ETC/host_rsa_key
PidFile $SSH_ETC/sshd.pid
UsePAM no
PasswordAuthentication yes
PermitRootLogin no
AllowUsers $E2E_SSH_USER
Subsystem sftp $SFTP_SERVER_PATH
LogLevel ERROR
EOF

$SUDO /usr/sbin/sshd -f "$SSH_ETC/sshd_config" -E "$SSH_ETC/sshd.log"

# --- smbd -------------------------------------------------------------------
SMB_ETC="$E2E_DIR/smb-etc"
rm -rf "$SMB_ETC"
mkdir -p "$SMB_ETC/private" "$SMB_ETC/lock" "$SMB_ETC/state" "$SMB_ETC/cache" "$SMB_ETC/run"

cat > "$SMB_ETC/smb.conf" <<EOF
[global]
workgroup = WORKGROUP
server role = standalone server
security = user
map to guest = never
smb ports = 1445
bind interfaces only = yes
interfaces = lo
private dir = $SMB_ETC/private
lock directory = $SMB_ETC/lock
state directory = $SMB_ETC/state
cache directory = $SMB_ETC/cache
pid directory = $SMB_ETC/run
log file = $SMB_ETC/log.smbd
max log size = 1000
load printers = no
printcap name = /dev/null
disable spoolss = yes

[e2efixtures]
path = $SMB_ROOT
read only = yes
browsable = yes
guest ok = no
valid users = $E2E_SSH_USER
EOF

printf '%s\n%s\n' "$E2E_SSH_PASSWORD" "$E2E_SSH_PASSWORD" | \
  $SUDO smbpasswd -c "$SMB_ETC/smb.conf" -s -a "$E2E_SSH_USER"
$SUDO smbpasswd -c "$SMB_ETC/smb.conf" -e "$E2E_SSH_USER"

$SUDO /usr/sbin/smbd --configfile="$SMB_ETC/smb.conf" --daemon --no-process-group

# --- wait for both to actually accept connections ------------------------
# Both self-daemonize before their listener is necessarily up -- fail loudly
# here rather than let the first e2e test hit an unexplained connection
# refused a few seconds into the run.
_wait_for_port() {
  local host="$1" port="$2" label="$3"
  for _ in $(seq 1 50); do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      exec 3>&- 3<&-
      return 0
    fi
    sleep 0.2
  done
  echo "ERROR: $label never came up on $host:$port -- see its log under $E2E_DIR" >&2
  return 1
}
_wait_for_port 127.0.0.1 2222 sshd
_wait_for_port 127.0.0.1 1445 smbd

# --- hand off connection details to the Playwright specs -------------------
# SSH_ROOT/SMB_ROOT are absolute paths under this checkout, so there's no
# fixed literal frontend/e2e/*.spec.ts could hardcode -- written here, once,
# as the one place that actually knows them, and read directly off disk by
# the specs (sources-ssh.spec.ts, sources-smb.spec.ts) before this server is
# even up. Not needed for WinRM: its fixture root is a fixed constant
# (app/testing/fake_winrm.py's FIXTURE_ROOT), not a real filesystem path.
cat > "$E2E_DIR/fixture-paths.json" <<EOF
{
  "ssh": {
    "host": "127.0.0.1",
    "port": 2222,
    "base_path": "$SSH_ROOT",
    "username": "$E2E_SSH_USER",
    "password": "$E2E_SSH_PASSWORD"
  },
  "smb": {
    "host": "127.0.0.1",
    "port": 1445,
    "base_path": "e2efixtures",
    "username": "$E2E_SSH_USER",
    "password": "$E2E_SSH_PASSWORD"
  }
}
EOF

echo "e2e test servers up: sshd on 127.0.0.1:2222, smbd on 127.0.0.1:1445 (user $E2E_SSH_USER)"
