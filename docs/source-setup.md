# Preparing a source server for PerchTail

PerchTail is agentless in v1 — it never installs anything on the machines it
reads from. Instead, each source server needs a **dedicated, least-privilege
account** reachable over one of three protocols (SSH/SFTP, SMB, or WinRM).
This guide covers what to configure on the *source* side before adding it as a
source in PerchTail's admin UI. See [CLAUDE.md](../CLAUDE.md) for how the
`Source` model (`protocol`, `host`, `port`, `credential_ref`, `base_path`)
maps to this configuration.

## At a glance

| Protocol | Used for | Default port | Account type | Key restriction |
|---|---|---|---|---|
| SSH/SFTP | Linux | 22 | dedicated OS user, key-based | SFTP-only chroot jail |
| SMB | Windows shares | 445 | dedicated local/domain user | share + NTFS ACL scoped to one folder |
| WinRM | Windows (fallback when SMB isn't open) | 5985/5986 | dedicated local/domain user | JEA-constrained, read-only cmdlets |

## Principles that apply to every protocol

- **One dedicated service account per source.** Never reuse a personal or
  admin account, and never share one account across multiple customers or
  sources — credential compromise should be contained to a single source.
- **OS-level least privilege is the real boundary.** PerchTail's rule engine
  (include/exclude, glob or regex) decides what's *visible in the UI* — it is
  not a substitute for the source itself only granting read access to what
  should be reachable in the first place. Scope both.
- **Read-only, always.** The account only ever needs to list directories and
  read file contents. Never grant write, delete, or execute rights beyond
  what the protocol inherently requires to do that.
- **Reachability is inbound, from the connector.** PerchTail is agentless in
  v1, so the connector host needs inbound network access to the source on
  the relevant port. If a source can't allow inbound (isolated network,
  strict firewall), it's out of scope for v1 — the Phase 2 push-agent (see
  [ROADMAP.md](../ROADMAP.md#phase-2)) is being built specifically for that
  case.
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
ssh-keygen -t ed25519 -f perchtail_<source-name> -C "perchtail"
```

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

### 3. Jail it to SFTP and the folder it needs

In `/etc/ssh/sshd_config`:

```
Match User perchtail
    ForceCommand internal-sftp
    ChrootDirectory /var/log/appname   # match the source's base_path
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
```

Restart `sshd`. This account can no longer get a shell or forward ports, and
is chrooted to exactly the tree the source's `base_path` should point at —
belt-and-suspenders alongside the rule engine.

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

### 5. Firewall

Allow TCP 22 (or your custom SSH port) inbound from the PerchTail connector's
IP only.

## SMB (Windows shares)

### 1. Create a dedicated account

Local or domain account, e.g. `svc-perchtail`. No admin rights. If your
policy allows that granularity, disable interactive/"log on locally" rights
for it — this account only ever needs network (SMB) logon.

### 2. Scope the share

Share only the specific folder tree PerchTail needs — never `C$`, never a
whole drive:

```powershell
New-SmbShare -Name "AppLogs" -Path "D:\Logs\AppName" -ReadAccess "DOMAIN\svc-perchtail"
```

### 3. Match the NTFS ACL to the share

Share permissions and NTFS permissions are evaluated independently on
Windows — set both to read-only for this account on the same folder:

```powershell
icacls "D:\Logs\AppName" /grant "DOMAIN\svc-perchtail:(OI)(CI)RX"
```

### 4. Protocol version and auth

- Require SMB3+; disable SMBv1 on the host entirely if it's still enabled
  anywhere — it's deprecated and insecure.
- Prefer Kerberos over NTLM if the source is domain-joined; if NTLM is
  unavoidable, require NTLMv2 as the minimum.
- Enable SMB signing.

### 5. Firewall

Allow TCP 445 inbound from the PerchTail connector's IP only.

## WinRM (Windows, fallback when SMB isn't open)

WinRM is remote PowerShell, not a file-listing protocol — its default
surface is much broader than "read files," so constraining it well matters
more here than for SSH or SMB.

### 1. Enable WinRM, prefer HTTPS

```powershell
winrm quickconfig
```

Then configure an HTTPS listener with a real certificate rather than leaving
it on plain HTTP — otherwise traffic crosses the wire without transport
encryption beyond whatever NTLM itself provides.

### 2. Create a dedicated account, constrained with JEA

Do **not** add the PerchTail account to local Administrators, which is
WinRM's default requirement for remote access. Instead, set up a **Just
Enough Administration (JEA)** endpoint that exposes only the read-only
cmdlets PerchTail actually needs (`Get-ChildItem`, `Get-Content`,
`Get-Item`), and grant the account access to that endpoint only:

```powershell
Register-PSSessionConfiguration -Name "PerchTailReadOnly" -Path .\PerchTailJEA.pssc
```

This is the single most important control on this protocol — without it, a
compromised PerchTail credential on a WinRM source has far more reach than
"browse some log files."

### 3. Firewall

Allow TCP 5985 (HTTP, only if HTTPS genuinely isn't an option) or 5986
(HTTPS) inbound from the PerchTail connector's IP only.

## Testing a source

Once configured, add the source in PerchTail's admin UI with the matching
protocol, host, port, credential, and `base_path`, then use the run-now /
test-connection action to confirm reachability and permissions before
attaching rules.
