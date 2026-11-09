# Preparing a source server for PerchTail

PerchTail is agentless by default — for SSH/SFTP, SMB, and WinRM sources, it
never installs anything on the machines it reads from. Each of those needs a
**dedicated, least-privilege account** reachable over the relevant protocol.
For a host that can't be reached inbound at all, the push-agent (a small Go
binary that dials *out* instead) covers that case without needing an inbound
account or firewall rule on the source. This guide covers what to configure
on the *source* side before adding it as a source in PerchTail's admin UI.
See [CLAUDE.md](../CLAUDE.md) for how the `Source` model (`protocol`, `host`,
`port`, `credential_ref`, `base_path`) maps to this configuration.

## At a glance

| Protocol | Used for | Default port | Account type | Key restriction |
|---|---|---|---|---|
| SSH/SFTP | Linux | 22 | dedicated OS user, key-based | SFTP-only chroot jail |
| SMB | Windows shares | 445 | dedicated local/domain user | share + NTFS ACL scoped to one folder |
| WinRM | Windows (fallback when SMB isn't open) | 5986 (HTTPS) / 5985 | dedicated account behind the read-only JEA endpoint (or, without it, remote-shell rights) | NTFS read ACLs; the JEA role's allowed folders |
| Agent | Any host not reachable inbound | outbound only, no listener | enrollment token, not a login account | `PERCHTAIL_BASE_PATH` scopes what it can read |
| Local disk | Directories on the PerchTail host (e.g. a read-only bind mount) | n/a | none | off unless the operator sets `LOCAL_SOURCE_ROOTS` |

## Principles that apply to every protocol

- **One dedicated service account per source.** Never reuse a personal or
  admin account, and never share one account across multiple customers or
  sources — credential compromise should be contained to a single source.
- **OS-level least privilege is the real boundary.** PerchTail's rule engine
  (include/exclude, glob or regex) decides what's *visible in the UI* — it is
  not a substitute for the source itself only granting read access to what
  should be reachable in the first place. Scope both.
- **Rule matching follows the source's filesystem.** Rules are matched
  case-insensitively for SMB and WinRM sources and for agents running on
  Windows, since Windows treats `Logs` and `logs` as the same folder, and
  case-sensitively everywhere else.
  Requests for a non-canonical path (`./x`, `a//b`, backslashes, and on
  Windows trailing dots/spaces or 8.3 short names like `PROGRA~1`) are
  rejected outright, so an exclude rule can't be sidestepped by spelling
  the same file differently. An agent reports its OS each time it
  connects (shown as **Agent OS** on the source's page); agents older than
  0.5.1 don't, and their rules stay case-sensitive until the agent is
  upgraded.
- **Rules decide which folders show up, too.** A folder appears (and can be
  browsed) only if some file below it could be visible under the source's
  rules. `exclude secret/**` hides the `secret` folder entirely, sub-folder
  names included; with only `include app/**`, other top-level folders don't
  appear; with no rules at all, nothing does. A later include can still
  re-open part of an excluded folder, and then only the folders leading to
  it are shown. This analysis works on **glob** rules: regex rules are
  treated conservatively (a regex include keeps folders visible, a regex
  exclude isn't assumed to cover a whole folder), so to hide a folder
  reliably, use a glob exclude like `folder/**`. On SSH sources, rules are
  case-sensitive: `secret/**` doesn't hide `SECRET/`; use
  `re:(?i)^secret/` or a second glob rule for that.
- **Links don't lead out of the base path.** Symlinks (SSH, local, agent)
  and junctions or symlinks (SMB, WinRM) that point outside the base path
  are left out of listings, and any path that goes through one answers
  404. On Windows a junction needs no special privilege, so anyone who can
  write to the log folder could otherwise point it at any folder the
  service account can read.
- **Read-only, always.** The account only ever needs to list directories and
  read file contents. Never grant write, delete, or execute rights beyond
  what the protocol inherently requires to do that.
- **Reachability is inbound, from the connector, for SSH/SMB/WinRM.** Those
  three need the connector host to have inbound network access to the source
  on the relevant port. If a source can't allow inbound (isolated network,
  strict firewall), use the [Agent protocol](#agent-push-agent-for-hosts-not-reachable-inbound)
  instead — it dials out, so nothing needs to accept an inbound connection.
- **The credential only ever lives in PerchTail's source config**, where it's
  encrypted at rest (see CLAUDE.md's "Security notes"). Don't reuse it
  anywhere else, and rotate it the same way you'd rotate any service account
  credential.

## SSH / SFTP (Linux sources)

### 1. Create a dedicated account

```bash
sudo useradd -m -s /usr/sbin/nologin perchtail
```

Use `nologin` (or equivalent) as the shell — this account should never get an
interactive shell, only SFTP.

### 2. Generate and install a key pair

Generate a key for PerchTail — ideally one key per source, so a leaked key
only affects that source:

```bash
ssh-keygen -t ed25519 -N "" -f perchtail_<source-name> -C "perchtail"
```

PerchTail accepts ed25519, ECDSA and RSA private keys, in OpenSSH or PEM
format. Leave the passphrase empty (`-N ""`): a passphrase-protected key is
rejected, since PerchTail would have to store the passphrase right next to
the key anyway. The key itself is encrypted at rest like every other
credential.

Install only the **public** key on the source:

```bash
sudo mkdir -p /home/perchtail/.ssh
sudo cp perchtail_<source-name>.pub /home/perchtail/.ssh/authorized_keys
sudo chown -R perchtail:perchtail /home/perchtail/.ssh
sudo chmod 700 /home/perchtail/.ssh
sudo chmod 600 /home/perchtail/.ssh/authorized_keys
```

Paste the matching **private** key into PerchTail's source credential field.
Disable password auth for this account (or globally, per your policy).

### 3. Restrict it to SFTP (required), and optionally chroot it

With a `nologin` shell, SFTP only works through sshd's **internal**
SFTP server. The default `Subsystem sftp …/sftp-server` is started through
the user's shell, so `nologin` prints "This account is currently not
available." and the client fails with `Received message too long
1416128883` (that number is "This" read as a length). So this block is
**required**, not optional:

```
Match User perchtail
    ForceCommand internal-sftp
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
```

**Optional hardening: chroot the account** to the log tree, so the SSH
account itself can't see anything else on the host. sshd requires every
component of the chroot path to be owned by root and not writable by group
or others. On Debian/Ubuntu `/var/log` is `root:syslog 775`, so any chroot
under `/var/log` is refused ("bad ownership or modes for chroot directory
component"). Use a read-only bind mount instead:

```
# /etc/fstab
/var/log/appname  /srv/sftp/appname  none  bind,ro  0 0
```

```
Match User perchtail
    ForceCommand internal-sftp
    ChrootDirectory /srv/sftp          # root:root 755
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
```

**With a chroot, the source's Base path is relative to the chroot**, not the
real path on disk: `/appname` in this example (or `/` if the chroot is the
log folder itself). Using the real path (`/var/log/appname`) fails with
"Not found on the source", and the connection check says so.

On RHEL/Rocky with SELinux enforcing, SFTP reads of `var_log_t` files work
without extra policy when the account isn't chrooted. A chroot under
SELinux hasn't been tested yet.

Restart `sshd` after editing.

### 4. Grant read access to the log directories

Whatever produces the logs (the app, syslog, logrotate) needs to leave them
readable by this account — e.g. add `perchtail` to the log-owning group
(`adm` is the common convention for `/var/log` on Debian/Ubuntu), or set
ACLs directly:

```bash
sudo setfacl -R -m u:perchtail:rX /var/log/appname
sudo setfacl -R -d -m u:perchtail:rX /var/log/appname   # default ACL for new/rotated files
```

The default ACL matters: rotated files are new inodes, and PerchTail assumes
these directories keep gaining members over time (see CLAUDE.md's "Live
browsing & ephemeral fetch behavior") — a one-time `chmod` won't cover what
logrotate creates tomorrow.

PerchTail resolves every path on the server before listing or reading
it, and refuses anything that resolves outside the source's base path,
symlinks included. A link such as `escape -> /etc` inside the log folder
is hidden from listings and can't be opened. A chroot is still
recommended as a second, OS-level boundary.

### 5. Firewall

Allow TCP 22 (or your custom SSH port) inbound from the PerchTail connector's
IP only.

### 6. Host-key pinning

PerchTail keeps every SSH server's host key in `SSH_KNOWN_HOSTS_PATH`
(`/data/db/ssh_known_hosts` in Docker). If a pinned key ever changes, the
connection fails. That is the man-in-the-middle check.

What happens the *first* time PerchTail sees a host depends on
`SSH_HOST_KEY_POLICY`:

- `tofu` (default) pins the key on first connect. The OpenSSH equivalent
  is `StrictHostKeyChecking=accept-new`. Each pin is logged at WARNING as
  `ssh.host_key_pinned`, with an OpenSSH-style `SHA256:` fingerprint.
  Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on
  the server.
- `strict` refuses any host that isn't already in the file. Add each
  server first, from a machine you trust, and check the fingerprint
  before you append:

  ```bash
  ssh-keyscan -p 22 app01.example.com >> /data/db/ssh_known_hosts
  ```

## SMB (Windows shares)

### 1. Create a dedicated account

Local or domain account, e.g. `svc-perchtail`. No admin rights. If your
policy allows that granularity, disable interactive/"log on locally" rights
for it — this account only ever needs network (SMB) logon.

### 2. Scope the share

Share only the specific folder tree PerchTail needs — this applies
regardless of which drive the logs happen to live on. Logs sitting under
`C:\ProgramData\...`, `C:\inetpub\logs\...`, or another system-drive path
are completely normal; that's not a reason to reach for the built-in `C$`
admin share (see below for why). Instead, create an ordinary custom share
pointed at the exact folder, wherever it is:

```powershell
New-SmbShare -Name "AppLogs" -Path "C:\ProgramData\AppName\Logs" -ReadAccess "DOMAIN\svc-perchtail"
```

**Avoid the built-in `C$` (or any `<drive>$`) admin share.** It's not that
logs can't live on `C:` — it's that `C$` shares the entire drive, and by
default Windows only lets members of the local **Administrators** group
connect to it at all (a non-admin account gets denied even with correct
NTFS permissions on the target folder, due to UAC remote restrictions on
local accounts). So using `C$` doesn't just widen scope to the whole drive,
it also forces `svc-perchtail` to become a local admin to connect —
defeating the point of a dedicated least-privilege account. A folder-scoped
custom share avoids both problems at once, no matter which drive it's on.

If logs are genuinely scattered across many unpredictable paths on a host
and a single custom share can't cover them, create one share per log
folder and add them as [several folders of one source](#several-folders-in-one-source),
or use the [push agent](#agent-push-agent-for-hosts-not-reachable-inbound).
Don't fall back to `C$`.

### 3. Match the NTFS ACL to the share

Share permissions and NTFS permissions are evaluated independently on
Windows — set both to read-only for this account on the same folder:

```powershell
icacls "C:\ProgramData\AppName\Logs" /grant "DOMAIN\svc-perchtail:(OI)(CI)RX"
```

### 4. Protocol version and auth

- Require SMB3+; disable SMBv1 on the host entirely if it's still enabled
  anywhere — it's deprecated and insecure.
- PerchTail's connector authenticates over NTLM regardless of whether the
  source is domain-joined — `credential_ref` is only ever a bare
  username/password (see CLAUDE.md's data model), with no way to supply a
  realm or keytab for Kerberos. Require NTLMv2 as the minimum on the share
  side.
- Enable SMB signing.

### 5. Firewall

Allow TCP 445 inbound from the PerchTail connector's IP only.

## WinRM (Windows, fallback when SMB isn't open)

> **Read this before choosing WinRM.** There are two ways to connect, and
> they differ a lot in what the stored credential can do:
>
> - **Through PerchTail's read-only JEA endpoint (recommended, 0.6+).** The
>   source names the endpoint in **JEA endpoint**, and PerchTail speaks PSRP
>   to it. The endpoint runs in `NoLanguage` mode and only exposes
>   PerchTail's own functions, which list and read files under the folders
>   you allow and refuse junctions and links. The account needs no
>   administrator rights and no remote shell: if the credential leaks, it can
>   read those logs and nothing else. See step 2, option A.
> - **Through a WinRS remote shell** (JEA endpoint left empty). Windows only
>   lets an account open that shell if it is a local **Administrator**, or if
>   the WinRM service's `RootSDDL` is widened to grant it (`GXGR`). Either way
>   the credential has a **full remote shell** on the host. See step 2,
>   option B.
>
> An [SMB share](#smb-windows-shares) of the log folder, or the
> [push agent](#agent-push-agent-for-hosts-not-reachable-inbound), are still
> simpler where either is possible.

### 1. Enable WinRM over HTTPS

```powershell
winrm quickconfig
```

Then add an HTTPS listener (port 5986) with a certificate the PerchTail host
can verify. In the source editor, set **Scheme** to HTTPS (the default for
new sources). If the certificate comes from an internal CA or is
self-signed, paste that CA certificate (PEM) into **CA certificate**. It is
trusted for that source only, without changing the server-wide trust store
or setting `REQUESTS_CA_BUNDLE`.

Plain HTTP (port 5985) still has NTLM encrypt the WinRM messages, but the
server isn't authenticated, so a machine in the middle can impersonate it.
Use it only on a network you control.

Sources created before 0.5.0 have no scheme stored and keep the old rule
until edited: HTTPS on port 5986 or with no port, HTTP on any other port.
Saving the source in the editor stores the scheme explicitly.

### 2. Create a dedicated account

A dedicated local or domain account, e.g. `svc-perchtail`, used for nothing
else and for no other host, with NTFS read access to the log folders only.

**Option A -- the read-only JEA endpoint (recommended).** Put the account in
a group of its own (e.g. `PerchTail Readers`), copy
[`docs/examples/jea`](examples/jea) to the host, and run, as an administrator:

```powershell
.Register-PerchTailJea.ps1 -Group 'CONTOSOPerchTail Readers' -Roots 'D:ogs', 'c:inetpubogs'
```

it installs the role, records the folders it may read
(`%programdata%perchtailjea-roots.txt`, changeable by administrators only)
and registers the endpoint `perchtail`. in the source editor, set **jea
endpoint** to `perchtail`; the **base path** must be one of those folders or
inside one. the account needs nothing else: not administrators, not
**remote management users**, not `rootsddl`. details in
[`docs/examples/jea/readme.md`](examples/jea/readme.md).

**option b -- a winrs shell.** leave **jea endpoint** empty. for the reasons
above the account then needs one of:

- membership of the local **Administrators** group; or
- an entry in the WinRM `RootSDDL` granting it execute and read (`GXGR`):

  ```powershell
  winrm configSDDL default
  ```

  Add the account with read and execute rights in the dialog. This still
  gives it a remote shell, but keeps it out of Administrators, so it can
  only read what its NTFS permissions allow.

Adding the account to **Remote Management Users** alone is not enough for
option B: it fails with `Access is denied`.

### 3. Firewall

Allow TCP 5986 (HTTPS), or 5985 only if HTTPS genuinely isn't an option,
inbound from the PerchTail connector's IP only.

## Agent (push-agent, for hosts not reachable inbound)

Unlike the other three protocols, the agent doesn't need a dedicated login
account, an open inbound port, or any credential PerchTail stores and uses to
connect *to* the host — the host connects *to* PerchTail instead, over a
single outbound WebSocket connection, and PerchTail relays live `list`/`fetch`
commands down it exactly as it would call any other connector directly.
Nothing is synced or pushed ahead of time; the always-fresh, nothing-persisted
rule applies to agent-mode sources the same as any other. See
[`agent/README.md`](../agent/README.md) for the full build/config reference.

### 1. Add the source and generate an enrollment token

In PerchTail's admin UI, create a source with protocol `Agent`, then use its
"Generate token" action (Settings → Sources → the source → Agent enrollment).
The token is shown once — copy it before navigating away; regenerating it
invalidates whatever the agent's config file currently has.

### 2. Install the agent binary on the source host

Either build it from `agent/` (`go build .`, or cross-compile — see
`agent/README.md`) or use a prebuilt binary for the host's OS/architecture,
and place it wherever you'd normally put a small system service binary
(e.g. `/usr/local/bin/perchtail-agent` on Linux).

### 3. Configure and run it

Environment variables; the first three are required:

| Variable | Example | Meaning |
|---|---|---|
| `PERCHTAIL_SERVER_URL` | `wss://perchtail.example.com/agent/connect` | PerchTail's agent endpoint |
| `PERCHTAIL_AGENT_TOKEN` | (from step 1) | The enrollment token |
| `PERCHTAIL_BASE_PATH` | `/var/log/myapp` | The only local root the agent will read from |
| `PERCHTAIL_ALLOW_INSECURE` | unset | `true` allows a plain `ws://` URL. By default the agent refuses anything but `wss://`. Testing or trusted networks only. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | `false` refuses every upgrade from the portal, whatever the server asks. |

Run it under whatever process supervisor the host already uses (systemd, a Windows service wrapper such as NSSM, etc.) so it restarts on failure or reboot — it reconnects with exponential backoff and jitter on its own if the connection drops, and notices a connection that died silently, but something still needs to restart the process itself if it exits. Run it as a least-privilege account that only has read access under `PERCHTAIL_BASE_PATH` — same read-only, scope-to-what's-needed principle as every other protocol here, just enforced by the OS account the agent runs as instead of a protocol-level ACL.

To upgrade the agent from the portal later (Settings → System turns it on), that account also needs write access to the folder holding the agent's binary, and the supervisor must restart the agent when it exits: see [Upgrades from the portal](../agent/README.md#upgrades-from-the-portal) for a systemd unit and the Windows service settings.

### 4. Firewall

None needed on the source host for inbound traffic — the agent only ever
initiates the connection outbound to `PERCHTAIL_SERVER_URL` over HTTPS/WSS.
Allow that outbound destination through the source's firewall/proxy if
outbound traffic is otherwise restricted.

## Local disk (directories on the PerchTail host itself)

For logs that are already on the machine running PerchTail, typically a
host directory bind-mounted read-only into the container:

```yaml
# docker-compose.yml
services:
  perchtail:
    volumes:
      - perchtail-data:/data
      - /var/log/myapp:/mnt/host-logs/myapp:ro
```

Local-disk sources are **off by default**. The server operator enables them
by listing the allowed parent directories in `LOCAL_SOURCE_ROOTS`
(comma-separated, absolute) in `.env`, for example
`LOCAL_SOURCE_ROOTS=/mnt/host-logs`. A source's base path must sit at or
under one of those directories, and reads never follow a symlink out of the
source's base path. This is deliberately an environment setting rather than
something an admin can change from the web UI: otherwise anyone allowed to
create sources could point one at PerchTail's own database or secrets.
Mount only what you mean to expose, and mount it read-only.

## Base path

What goes in **Base path** depends on the protocol, and PerchTail refuses
the kinds of path that can never work when you save the source:

| Protocol | Base path | Example |
|---|---|---|
| SSH/SFTP | absolute path on the host (relative to the chroot, if the account has one) | `/var/log/myapp` |
| SMB | the **share name**, optionally followed by a folder inside it. Not a drive path, and not an administrative share (`C$`, `ADMIN$`) | `AppLogs` or `AppLogs\iis` |
| WinRM | absolute Windows path, on any drive | `D:\Logs\MyApp` |
| Local disk | a directory under `LOCAL_SOURCE_ROOTS` | `/mnt/logs/app` |
| Agent | documentation only: the agent's `PERCHTAIL_BASE_PATH` is what counts | |

Instead of typing it, use **Browse…** next to the field. It connects with the
host and credential in the form and lists folders only, never file names:
`/` and down over SSH, the drives and then folders over WinRM, and the
host's shares (the ones the account can see, without `C$`/`ADMIN$`/`IPC$`)
and then folders over SMB. Browsing is limited to users who can manage
sources, and each listing is written to the audit log. A saved source's stored credential is
only ever sent to the host and port it was saved for: to browse another
host, or to save the source with a new host or port, type the credential
again.

Rules are relative to the base path. Changing the base path (or the host or
port) means the same rules now apply to whatever is at the new location, so
check they still match; the source's file-name index and alert positions are
cleared, and the audit log records the old and new values.

### Several folders in one source

A source can read several folders on the same host with the same
credential, for example IIS logs on `D:` and application logs on `E:`. How
many is set in **Settings → System → Folders per source**: 20 by default,
1 turns the feature off, and 100 at most. Lowering the limit doesn't
affect sources that already have more folders, but they can't add any. Use
**+ Add another folder** in the source editor and give each folder a
label. In the viewer each folder appears at the top level under its label,
and rules see the same paths, so `**/*.log` applies to every folder and
`IIS/**` to one. The connection test checks each folder and names the ones
that fail. Push-agent sources serve a single folder and can't have several.

## Testing a source

Once configured, add the source in PerchTail's admin UI with the matching
protocol, host, port, credential, and `base_path`, then use the
test-connection action to confirm reachability and permissions before
attaching rules. To add many sources at once, see
[bulk import](bulk-import.md).
