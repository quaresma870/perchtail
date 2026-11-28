# Preparing a source server for PerchTail

PerchTail reaches a host through the push agent, a small Go binary that runs on the host and dials *out* to PerchTail, or without installing anything on it, over SSH/SFTP, SMB or WinRM (deprecated), each with a **dedicated, least-privilege account**. This guide helps you choose, and covers what to configure on the *source* side before you add it under **Settings → Sources**.

## Choosing how to reach a host

A source reaches its host in one of five ways. All of them read the same files under the same rules and send nothing ahead of time; they differ in what the host needs, which way the connection goes, how fast large files arrive and where redaction runs.

| | Push agent | SSH / SFTP | SMB | WinRM (deprecated) | Local disk |
|---|---|---|---|---|---|
| Hosts | Linux (amd64, arm64) and Windows (amd64) | Linux, and Windows with its OpenSSH server | Windows shares (and Samba) | Windows | the PerchTail host |
| Connection | outbound from the host to PerchTail over `wss://`: no inbound port on the host | inbound to the host, TCP 22 | inbound to the host, TCP 445 | inbound to the host, TCP 5986 (5985 over HTTP) | none |
| What the host needs | the agent binary, run as a service | an SSH server and an account | a share of the log folder and an account | a WinRM HTTPS listener, an account and, recommended, the read-only JEA endpoint | nothing |
| What PerchTail stores | the SHA-256 hash of the source's enrollment token: no login credential | the account's key or password, encrypted | the account's password, encrypted | the account's password, encrypted | nothing |
| Large files | fastest: the agent reads its own disk and streams over its connection | fastest | fast | much slower: PowerShell sends each file as base64 text inside WS-Management responses | read in place |
| Redaction | on the host (`redaction/1`): content leaves the host already redacted | on the PerchTail server, after the original arrives | on the server | on the server | on the server |
| Archives | `.zip`, `.tar.gz` and `.tgz` listed and extracted on the host: only the file asked for travels | a `.zip` read in place (its index, then only the file asked for); a `.tar.gz` copied whole | as SSH | as SSH | read on the PerchTail host |
| Links | symlinks, and junctions on Windows, followed only inside the base path to what the rules show (`links/1`) | symlinks followed only inside the base path to what the rules show | never followed | never followed | as SSH |
| What it can read | only under `PERCHTAIL_BASE_PATH`, checked by the agent on every path | what the account can read (an SFTP chroot narrows it) | the share and its NTFS permissions | the NTFS permissions, and the JEA role's folders | under `LOCAL_SOURCE_ROOTS` |
| Updates | from Settings → Agents, only to a newer binary signed with the release key | the host's own | the host's own | the host's own | none |

### Why the push agent

Wherever you can install it, the push agent is the best way to reach a host, on Linux and on Windows:

- **No inbound port and no login account.** The agent dials out to PerchTail over `wss://` (it refuses a plain `ws://` URL unless told otherwise), so the host needs no listening service and no firewall rule for PerchTail. PerchTail keeps no password or key for the host, only the hash of a per-source enrollment token, shown once; regenerating it disconnects the agent that used the old one.
- **It reads only its folder.** Every path is checked against `PERCHTAIL_BASE_PATH` on the host, whatever the server asks, and the agent only answers commands: it never sends files on its own.
- **Content leaves the host redacted.** When redaction applies to the source, the agent redacts on its host with the source's rules, using a port of the server's engine checked against the same test vectors (`redaction/1`), and the server discards any answer that doesn't prove it was redacted with the rules it sent. Over SSH, SMB and WinRM the original crosses the network and is redacted on the server.
- **Only what is asked for travels.** The agent lists `.zip`, `.tar.gz` and `.tgz` archives on its host and extracts there the one file asked for, so a large archive isn't sent to read one log inside it.
- **Long reads keep going, and stop when they should.** While the agent reads far into a large archive before it can send anything, it reports its progress every 10 seconds (`progress/1`), so the read isn't given up as unanswered; a read that hangs still fails after 30 seconds. When the server gives up on a read, the agent stops reading the file.
- **Links are checked where they lead.** The agent reports where each symlink or junction really leads, and reads a file only when it is where the server checked its rules (`links/1`), with the same open file, so a link swapped in between isn't followed.
- **Upgrades from the portal, signed.** Settings → Agents upgrades an agent when asked, or by itself within an upgrade window; the agent installs a new binary only when `SHA256SUMS` carries a valid Ed25519 signature from the release key built into it and the version is newer than its own. `PERCHTAIL_REMOTE_UPGRADE=false` turns this off on a host.
- **As fast as SSH.** The agent reads its own disk and streams the file over its one connection, so large files arrive as fast as over SSH, and far faster than over WinRM.

See [Agent](#agent-push-agent-for-hosts-not-reachable-inbound) below and [agent-service.md](agent-service.md) to install it.

### Which to use

- **The push agent** wherever you can install it, on Linux and on Windows.
- Where you can't: **SSH / SFTP** on Linux; on Windows, an **SMB** share of the log folder, or the **OpenSSH server** that comes with Windows (see [Moving a WinRM source](#moving-a-winrm-source) for its setup).
- **Local disk** for logs already on the PerchTail host.
- **WinRM** only for sources that already use it, until they are moved. WinRM is deprecated in PerchTail 1.0: it keeps working through 1.x, gets no further performance work, and will be removed in a later major release. The source editor says so when WinRM is chosen, the sources list labels WinRM sources **deprecated**, and the system settings pages show how many there are.

## At a glance

| Protocol | Used for | Default port | Account type | Key restriction |
|---|---|---|---|---|
| SSH/SFTP | Linux | 22 | dedicated OS user, key-based | SFTP-only chroot jail |
| SMB | Windows shares | 445 | dedicated local/domain user | share + NTFS ACL scoped to one folder |
| WinRM (deprecated) | Windows, existing sources until they are [moved](#moving-a-winrm-source) | 5986 (HTTPS) / 5985 | dedicated account behind the read-only JEA endpoint (or, without it, remote-shell rights) | NTFS read ACLs; the JEA role's allowed folders |
| Agent | Any host not reachable inbound | outbound only, no listener | enrollment token, not a login account | `PERCHTAIL_BASE_PATH` scopes what it can read |
| Local disk | Directories on the PerchTail host (e.g. a read-only bind mount) | n/a | none | off unless the operator sets `LOCAL_SOURCE_ROOTS` |

## Principles that apply to every protocol

- **One dedicated service account per source.** Never reuse a personal or admin account, and never share one account across multiple customers or sources — credential compromise should be contained to a single source.
- **OS-level least privilege is the real boundary.** PerchTail's rule engine (include/exclude, glob or regex) decides what's *visible in the UI* — it is not a substitute for the source itself only granting read access to what should be reachable in the first place. Scope both.
- **Include rule matching follows the source's filesystem.** Include rules are matched case-insensitively for SMB and WinRM sources and for agents running on Windows, since Windows treats `Logs` and `logs` as the same folder, and case-sensitively everywhere else; exclude rules ignore case on every source (below). Requests for a non-canonical path (`./x`, `a//b`, backslashes, and on Windows trailing dots/spaces or 8.3 short names like `PROGRA~1`) are rejected outright, so an exclude rule can't be sidestepped by spelling the same file differently. An agent reports its OS each time it connects (shown as **Agent OS** on the source's page); agents older than 0.5.1 don't, and their include rules stay case-sensitive until the agent is upgraded.
- **Rules decide which folders show up, too.** A folder appears (and can be browsed) only if some file below it could be visible under the source's rules. `exclude secret/**` hides the `secret` folder entirely, sub-folder names included; with only `include app/**`, other top-level folders don't appear; with no rules at all, nothing does. A later include can still re-open part of an excluded folder, and then only the folders leading to it are shown. This analysis works on **glob** rules: regex rules are treated conservatively (a regex include keeps folders visible, a regex exclude isn't assumed to cover a whole folder), so to hide a folder reliably, use a glob exclude like `folder/**`.
- **Rules reach inside archives.** A `.zip` or `.tar.gz` browses as a folder, and each file in it is matched by its path through the archive (`logs/old.zip/app.log`). An archive in which some file's last matching rule is an exclude can't be downloaded or opened whole, since that would hand over that file; its other files can still be opened and downloaded one by one, and a folder's ZIP leaves the archive out and lists it in `SKIPPED.txt`. The refusal and `SKIPPED.txt` don't say why; the audit log records each refused archive (`file.open_denied` and `file.download_denied`, for one a folder's ZIP left out once the ZIP ends, with the folder in `folder`). Files no rule matches don't count: with only `include **/*.zip`, the archive's files aren't listed but the archive still downloads whole. An archive that can't be read as one is refused whole whenever the source has an exclude rule, and so is an archive that holds another archive (the inner archive's own files aren't matched against the rules), whether it's the outer archive or the inner one taken on its own.
- **Links are judged by where they lead.** On SSH, local and agent sources (symlinks, and on a Windows agent also junctions), a link is listed and read only when it leads inside the base path to a file or folder the rules show: the rules are checked on the link's own path and on its target, and on every folder a path goes through. `current.log -> app-2026-11-16.log` keeps working when both are visible; a link to a file or folder the rules hide, or out of the base path, is left out of listings, and opening, following or downloading it answers 404 and is audited with the reason `link_target_hidden`. SMB and WinRM sources never follow a junction or a symlink at all: they are left out of listings and any path that goes through one answers 404. On Windows a junction needs no special privilege, so anyone who can write to the log folder could otherwise point it at any folder the service account can read.
- **Exclude rules ignore letter case on every source.** An exclude rule, glob or `re:`, also hides the same path spelled in another case (`secret/**` hides `SECRET/`), even on a source whose own file names are case-sensitive. Include rules keep the source's case sensitivity.
- **Read-only, always.** The account only ever needs to list directories and read file contents. Never grant write, delete, or execute rights beyond what the protocol inherently requires to do that.
- **Reachability is inbound, from the connector, for SSH/SMB/WinRM.** Those three need the connector host to have inbound network access to the source on the relevant port. If a source can't allow inbound (isolated network, strict firewall), use the [Agent protocol](#agent-push-agent-for-hosts-not-reachable-inbound) instead — it dials out, so nothing needs to accept an inbound connection.
- **The credential only ever lives in PerchTail's source config**, where it's encrypted at rest with a key derived from `CREDENTIAL_ENCRYPTION_KEY` (see [credential-key-rotation.md](credential-key-rotation.md)). Don't reuse it anywhere else, and rotate it the same way you'd rotate any service account credential.

## SSH / SFTP (Linux sources)

### 1. Create a dedicated account

```bash
sudo useradd -m -s /usr/sbin/nologin perchtail
```

Use `nologin` (or equivalent) as the shell — this account should never get an interactive shell, only SFTP.

### 2. Generate and install a key pair

Generate a key for PerchTail — ideally one key per source, so a leaked key only affects that source:

```bash
ssh-keygen -t ed25519 -N "" -f perchtail_<source-name> -C "perchtail"
```

PerchTail accepts ed25519, ECDSA and RSA private keys, in OpenSSH or PEM format. Leave the passphrase empty (`-N ""`): a passphrase-protected key is rejected, since PerchTail would have to store the passphrase right next to the key anyway. The key itself is encrypted at rest like every other credential.

Install only the **public** key on the source:

```bash
sudo mkdir -p /home/perchtail/.ssh
sudo cp perchtail_<source-name>.pub /home/perchtail/.ssh/authorized_keys
sudo chown -R perchtail:perchtail /home/perchtail/.ssh
sudo chmod 700 /home/perchtail/.ssh
sudo chmod 600 /home/perchtail/.ssh/authorized_keys
```

Paste the matching **private** key into PerchTail's source credential field. Disable password auth for this account (or globally, per your policy).

### 3. Restrict it to SFTP (required), and optionally chroot it

With a `nologin` shell, SFTP only works through sshd's **internal** SFTP server. The default `Subsystem sftp …/sftp-server` is started through the user's shell, so `nologin` prints "This account is currently not available." and the client fails with `Received message too long 1416128883` (that number is "This" read as a length). So this block is **required**, not optional:

```
Match User perchtail
    ForceCommand internal-sftp
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
```

**Optional hardening: chroot the account** to the log tree, so the SSH account itself can't see anything else on the host. sshd requires every component of the chroot path to be owned by root and not writable by group or others. On Debian/Ubuntu `/var/log` is `root:syslog 775`, so any chroot under `/var/log` is refused ("bad ownership or modes for chroot directory component"). Use a read-only bind mount instead:

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

**With a chroot, the source's Base path is relative to the chroot**, not the real path on disk: `/appname` in this example (or `/` if the chroot is the log folder itself). Using the real path (`/var/log/appname`) fails with "Not found on the source", and the connection check says so.

On RHEL/Rocky with SELinux enforcing, SFTP reads of `var_log_t` files work without extra policy when the account isn't chrooted. A chroot under SELinux hasn't been tested yet.

Restart `sshd` after editing.

### 4. Grant read access to the log directories

Whatever produces the logs (the app, syslog, logrotate) needs to leave them readable by this account — e.g. add `perchtail` to the log-owning group (`adm` is the common convention for `/var/log` on Debian/Ubuntu), or set ACLs directly:

```bash
sudo setfacl -R -m u:perchtail:rX /var/log/appname
sudo setfacl -R -d -m u:perchtail:rX /var/log/appname   # default ACL for new/rotated files
```

The default ACL matters: rotated files are new inodes, and PerchTail lists the folder live each time, so new files must be readable too — a one-time `chmod` won't cover what logrotate creates tomorrow.

PerchTail resolves every path on the server before listing or reading it, and refuses anything that resolves outside the source's base path, symlinks included. A link such as `escape -> /etc` inside the log folder is hidden from listings and can't be opened, and so is a link to a file the source's rules hide (the rules are checked on where it leads). A chroot is still recommended as a second, OS-level boundary.

### 5. Firewall

Allow TCP 22 (or your custom SSH port) inbound from the PerchTail connector's IP only.

### 6. Host-key pinning

PerchTail keeps every SSH server's host key in `SSH_KNOWN_HOSTS_PATH` (`/data/db/ssh_known_hosts` in Docker). If a pinned key ever changes, the connection fails. That is the man-in-the-middle check. Deleting a source, or changing its host or port, removes the old host's key from the file when no other source uses that host and port, audited as `source.pins_removed`.

What happens the *first* time PerchTail sees a host depends on `SSH_HOST_KEY_POLICY`:

- `tofu` (default) pins the key on first connect. The OpenSSH equivalent is `StrictHostKeyChecking=accept-new`. Each pin is logged at WARNING as `ssh.host_key_pinned`, with an OpenSSH-style `SHA256:` fingerprint. Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server.
- `strict` refuses any host that isn't already in the file. Add each server first, from a machine you trust, and check the fingerprint before you append:

  ```bash
  ssh-keyscan -p 22 app01.example.com >> /data/db/ssh_known_hosts
  ```

## SMB (Windows shares)

### 1. Create a dedicated account

Local or domain account, e.g. `svc-perchtail`. No admin rights. If your policy allows that granularity, disable interactive/"log on locally" rights for it — this account only ever needs network (SMB) logon.

### 2. Scope the share

Share only the specific folder tree PerchTail needs — this applies regardless of which drive the logs happen to live on. Logs sitting under `C:\ProgramData\...`, `C:\inetpub\logs\...`, or another system-drive path are completely normal; that's not a reason to reach for the built-in `C$` admin share (see below for why). Instead, create an ordinary custom share pointed at the exact folder, wherever it is:

```powershell
New-SmbShare -Name "AppLogs" -Path "C:\ProgramData\AppName\Logs" -ReadAccess "DOMAIN\svc-perchtail"
```

**Avoid the built-in `C$` (or any `<drive>$`) admin share.** It's not that logs can't live on `C:` — it's that `C$` shares the entire drive, and by default Windows only lets members of the local **Administrators** group connect to it at all (a non-admin account gets denied even with correct NTFS permissions on the target folder, due to UAC remote restrictions on local accounts). So using `C$` doesn't just widen scope to the whole drive, it also forces `svc-perchtail` to become a local admin to connect — defeating the point of a dedicated least-privilege account. A folder-scoped custom share avoids both problems at once, no matter which drive it's on.

If logs are genuinely scattered across many unpredictable paths on a host and a single custom share can't cover them, create one share per log folder and add them as [several folders of one source](#several-folders-in-one-source), or use the [push agent](#agent-push-agent-for-hosts-not-reachable-inbound). Don't fall back to `C$`.

### 3. Match the NTFS ACL to the share

Share permissions and NTFS permissions are evaluated independently on Windows — set both to read-only for this account on the same folder:

```powershell
icacls "C:\ProgramData\AppName\Logs" /grant "DOMAIN\svc-perchtail:(OI)(CI)RX"
```

### 4. Protocol version and auth

- Require SMB3+; disable SMBv1 on the host entirely if it's still enabled anywhere — it's deprecated and insecure.
- PerchTail's connector authenticates over NTLM regardless of whether the source is domain-joined — the stored credential is only a username and password, with no way to supply a realm or keytab for Kerberos. Require NTLMv2 as the minimum on the share side.
- Enable SMB signing. PerchTail requires signed messages by default (**Require signing** under **SMB security** in the source editor, `smb_require_signing` in a bulk import) and refuses a server that doesn't sign; turn it off only for a server that can't sign, and the editor and the source's page then warn about it. **Encrypt (SMB 3)** (`smb_encrypt`) also asks for SMB 3 encryption of the whole session and refuses a server that can't encrypt; without it the server decides. It starts ticked for a new source in the source editor; untick it only for a server that can't encrypt. The API and a bulk import leave it off when it isn't given.

### 5. Firewall

Allow TCP 445 inbound from the PerchTail connector's IP only.

## WinRM (Windows, fallback when SMB isn't open)

> **WinRM is deprecated.** It keeps working through PerchTail 1.x and will be removed in a later major release. Reads over WinRM are much slower than over the push agent, SSH or SMB (see [Large files](#4-large-files) below), and the push agent needs no inbound port or account. For a new Windows source use the agent, an SMB share or the OpenSSH server included with Windows ([Choosing how to reach a host](#choosing-how-to-reach-a-host)), and move existing WinRM sources when you can ([Moving a WinRM source](#moving-a-winrm-source)).

> **Read this before choosing WinRM.** There are two ways to connect, and they differ a lot in what the stored credential can do:
>
> - **Through PerchTail's read-only JEA endpoint (recommended, 0.6+).** The source names the endpoint in **JEA endpoint (optional)**, and PerchTail speaks PSRP to it. The endpoint runs in `NoLanguage` mode and only exposes PerchTail's own functions, which list and read files under the folders you allow and refuse junctions and links. The account needs no administrator rights and no remote shell: if the credential leaks, it can read those logs and nothing else. See step 2, option A.
> - **Through a WinRS remote shell** (JEA endpoint left empty). Windows only lets an account open that shell if it is a local **Administrator**, or if the WinRM service's `RootSDDL` is widened to grant it (`GXGR`). Either way the credential has a **full remote shell** on the host. See step 2, option B.
>
> An [SMB share](#smb-windows-shares) of the log folder, or the [push agent](#agent-push-agent-for-hosts-not-reachable-inbound), are still simpler where either is possible.

### 1. Enable WinRM over HTTPS

```powershell
winrm quickconfig
```

Then add an HTTPS listener (port 5986) with a certificate the PerchTail host can verify. In the source editor, set **Scheme** to HTTPS (the default for new sources). If the certificate comes from an internal CA or is self-signed, paste that CA certificate (PEM) into **CA certificate (PEM, optional)**. It is trusted for that source only, without changing the server-wide trust store or setting `REQUESTS_CA_BUNDLE`. Deleting the source, or changing its host, port or CA certificate, removes the old certificate's file when no other source uses it, audited as `source.pins_removed`.

Plain HTTP (port 5985) still has NTLM encrypt the WinRM messages, but the server isn't authenticated (the source editor warns when **Scheme** is HTTP), so a machine in the middle can impersonate it. Use it only on a network you control.

Sources created before 0.5.0 have no scheme stored and keep the old rule until edited: HTTPS on port 5986 or with no port, HTTP on any other port. Saving the source in the editor stores the scheme explicitly.

### 2. Create a dedicated account

A dedicated local or domain account, e.g. `svc-perchtail`, used for nothing else and for no other host, with NTFS read access to the log folders only.

**Option A -- the read-only JEA endpoint (recommended).** Put the account in a group of its own (e.g. `PerchTail Readers`), copy [`docs/examples/jea`](examples/jea) to the host, and run, as an administrator:

```powershell
.\Register-PerchTailJea.ps1 -Group 'CONTOSO\PerchTail Readers' -Roots 'D:\Logs', 'C:\inetpub\logs'
```

It installs the role, records the folders it may read (`%ProgramData%\PerchTail\jea-roots.txt`, changeable by administrators only) and registers the endpoint `PerchTail`. In the source editor, set **JEA endpoint** to `PerchTail`; the **Base path** must be one of those folders or inside one. The account needs nothing else: not Administrators, not **Remote Management Users**, not `RootSDDL`. Details in [`docs/examples/jea/README.md`](examples/jea/README.md).

**Option B -- a WinRS shell.** Leave **JEA endpoint** empty; the source editor and the source's page then warn that the account needs remote-shell rights. For the reasons above the account then needs one of:

- membership of the local **Administrators** group; or
- an entry in the WinRM `RootSDDL` granting it execute and read (`GXGR`):

  ```powershell
  winrm configSDDL default
  ```

  Add the account with read and execute rights in the dialog. This still gives it a remote shell, but keeps it out of Administrators, so it can only read what its NTFS permissions allow.

Adding the account to **Remote Management Users** alone is not enough for option B: it fails with `Access is denied`.

### 3. Firewall

Allow TCP 5986 (HTTPS), or 5985 only if HTTPS genuinely isn't an option, inbound from the PerchTail connector's IP only.

### 4. Large files

WinRM has no file transfer of its own: PerchTail reads a file through PowerShell, which sends it as base64 text inside WS-Management responses, one response at a time. Opening or downloading a large file is therefore much slower than over SMB or the agent: minutes rather than seconds for a file of a few hundred MB. Use an SMB source or the agent for hosts with large logs. A file bigger than **Open from the end above** (Settings → Explorer and limits, 50 MB by default) opens at its end by itself, reading only its last 1 MB, with **Load earlier** and **Load whole file** in the viewer; with that setting at 0, **Tail the end** in the warning for files over 200 MB opens only the last part of a file.

A `.zip` is read in place: expanding it reads only its index at its end, and opening one of its files only that file's compressed bytes, a few PowerShell requests rather than the whole archive. A large `.zip` is therefore quick to expand and its small files quick to open; a large file inside it still takes as long as its compressed size. A `.tar.gz` is still read whole.

Each response carries up to 500 KB (PerchTail asks for envelopes of 512000 bytes). A server whose `MaxEnvelopeSizekb` is below 500 refuses that size; PerchTail then asks again with 150 KB envelopes, which works but takes about three times as many responses, so reads are slower, and logs `winrm.envelope_fallback` each time. `MaxEnvelopeSizekb` is 500 by default since Windows Server 2012; to check it, run `winrm get winrm/config` as an administrator, and to restore it, `winrm set winrm/config '@{MaxEnvelopeSizekb="500"}'`.

## Moving a WinRM source

WinRM is deprecated (see [Which to use](#which-to-use)). A source's protocol can't be changed, so moving a WinRM source means adding a source over another protocol for the same folder, checking it, and then deleting the WinRM source. Nothing has to happen on the same day: both sources can exist side by side while you check the new one.

### 1. Choose what replaces it

- **The push agent (recommended).** Install it on the Windows host as a service, as in [agent-service.md](agent-service.md#5-install-on-windows-with-a-service-wrapper), with `PERCHTAIL_BASE_PATH` set to the WinRM source's base path (for example `D:\Logs\MyApp`), and run it as an account that can only read that folder. The host then needs no WinRM listener, no inbound port and no account in PerchTail. An agent serves one folder: for a WinRM source with several folders, add one agent source per folder, or set `PERCHTAIL_BASE_PATH` to a folder above them and let the rules show only what the WinRM source showed.
- **SMB.** Share the log folder read-only to a dedicated account, as in [SMB](#smb-windows-shares). The source's base path is the share name, so share exactly the folder the WinRM source read, and the rules keep matching the same paths. A folder on another drive is another share, added as another folder of the same source.
- **The OpenSSH server included with Windows.** On Windows Server 2019 and later, and Windows 10 and 11, install the **OpenSSH Server** optional feature (`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`), start the `sshd` service and set it to start automatically. Give a dedicated local account read access to the log folders only, install its public key (for a member of Administrators, OpenSSH reads `administrators_authorized_keys` instead, so use an account that isn't one), and restrict it to SFTP in `%ProgramData%\ssh\sshd_config` with a `Match User` block holding `ForceCommand internal-sftp`. Allow TCP 22 inbound from PerchTail only. In PerchTail, add an **SSH / SFTP** source whose base path is the folder as the SFTP server names it, with forward slashes and a leading slash: `/D:/Logs/MyApp`.

An SSH source doesn't know its host runs Windows, which matters for the rules: its include rules are case-sensitive, and PerchTail doesn't refuse the other spellings Windows accepts for a name (8.3 short names like `SECRET~1`, trailing dots or spaces), so an exclude rule can be sidestepped by spelling a path that way. Exclude rules still ignore case. On an SSH source to Windows, keep files that must stay hidden out of the account's reach with NTFS permissions rather than with exclude rules alone, or choose the agent or SMB, which PerchTail treats as Windows: the agent reports its operating system, and SMB and Windows agents get case-insensitive include rules and refuse those spellings, as WinRM did.

### 2. Add the new source

Add it under **Settings → Sources** in the same customer and folder as the WinRM source, so the grants on that customer or folder cover it too. Give it a name other than the WinRM source's, since a YAML import finds a source by its name within its customer. For an agent source, generate its enrollment token and start the agent with it.

### 3. Copy the rules

Rules are relative to the base path, so they carry over unchanged when the new source reads the same folder.

- In the rule editor: open the WinRM source's rules in **Raw text**, copy them, and paste them into the new source's rule editor in **Raw text**.
- With a YAML file (super admins): under **Settings → Import / export**, export the configuration, keep the WinRM source's entry, and change its `name`, `protocol`, `host`, `port` and `base_path` (or `roots`), remove `winrm_scheme` and `tls_ca_pem`, and add a `credential` for SMB or SSH, or set it in the source editor afterwards. Importing it creates the new source with the same `rules`, `redaction_rules` and `redaction_disable`, and leaves the WinRM source as it is. See [bulk-import.md](bulk-import.md).

### 4. Grants and alerts

Grants on the customer or on a folder above the source apply to the new source by themselves. Grants on the WinRM source itself, and alerts scoped to it, don't move: add them again on the new source (Settings → Roles, Alerts). Deleting the WinRM source deletes its own grants and the alerts scoped to it.

### 5. Check it, then delete the WinRM source

Use **Test connection** on the new source's page, browse it, and open a few of the files the WinRM source showed, including one the rules hide, which must stay hidden. The new source's file-name index fills on its next pass, and alerts read its files from where they are when they first see them. Then delete the WinRM source under **Settings → Sources**; its entries in the audit log stay.

On the Windows host, remove what only PerchTail used: the JEA endpoint (see [the JEA README](examples/jea/README.md)), the WinRM HTTPS listener and its firewall rule if nothing else needs them, and the WinRM source's account.

## Agent (push-agent, for hosts not reachable inbound)

Unlike SSH, SMB and WinRM sources, the agent doesn't need a dedicated login account, an open inbound port, or any credential PerchTail stores and uses to connect *to* the host — the host connects *to* PerchTail instead, over a single outbound WebSocket connection, and PerchTail relays live `list`/`fetch` commands down it exactly as it would call any other connector directly. Nothing is synced or pushed ahead of time; the always-fresh, nothing-persisted rule applies to agent-mode sources the same as any other. See [`agent/README.md`](../agent/README.md) for the full build and configuration reference.

### 1. Add the source and generate an enrollment token

Under **Settings → Sources**, create a source with protocol **Agent (push)** and save it. Then, on the source's page or in its editor, click **Generate token** under **Agent enrollment**. The token is shown once — copy it before you leave the page. **Regenerate token** invalidates the previous token and disconnects an agent that is using it.

### 2. Install the agent binary on the source host

Download the prebuilt binary for the host's OS and architecture from the release, and check it against `SHA256SUMS` (see [Download](../agent/README.md#download)), or build it from `agent/` with the version set (see [Build](../agent/README.md#build)). Put it in a folder of its own, for example `/opt/perchtail-agent/` on Linux; upgrades from the portal need that folder to be writable by the agent's account (see below).

Use an agent of the same release as the server. The server reads files only from an agent that tells it where links lead (agents from 0.7 on): an older agent stays connected and its folders can still be browsed, but opening, Tail, downloads, folder ZIPs and archives are refused with "This source's agent must be updated…", and alerts skip its files, until it is updated, and **Settings → Agents** marks it **Update required**.

### 3. Configure and run it

The agent reads five environment variables, and has no configuration file. The first three are required:

| Variable | Example | Meaning |
|---|---|---|
| `PERCHTAIL_SERVER_URL` | `wss://perchtail.example.com/agent/connect` | PerchTail's agent endpoint |
| `PERCHTAIL_AGENT_TOKEN` | (from step 1) | The enrollment token |
| `PERCHTAIL_BASE_PATH` | `/var/log/myapp` | The only local root the agent will read from |
| `PERCHTAIL_ALLOW_INSECURE` | unset | `true` allows a plain `ws://` URL. By default the agent refuses anything but `wss://`. Testing or trusted networks only. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | `false` refuses every upgrade from the portal, whatever the server asks. |

The agent stops at startup, with a non-zero exit code and a message naming the problem, when `PERCHTAIL_BASE_PATH` doesn't exist, isn't a folder or can't be read by its account.

Run it under a process supervisor so it restarts on failure or reboot: systemd on Linux. On Windows the agent is a console program, not a Windows service: run it as a service through a wrapper such as NSSM or WinSW (see [agent/README.md](../agent/README.md#what-the-host-needs), and [agent-service.md](agent-service.md) for a full setup on Linux and Windows). It reconnects with exponential backoff and jitter on its own if the connection drops, and notices a connection that died silently, but something still needs to restart the process itself if it exits. Run it as a least-privilege account that only has read access under `PERCHTAIL_BASE_PATH` — same read-only, scope-to-what's-needed principle as every other protocol here, just enforced by the OS account the agent runs as instead of a protocol-level ACL.

To upgrade the agent from the portal later (Settings → Agents, off by default), that account also needs write access to the folder holding the agent's binary, and the supervisor must restart the agent when it exits: see [Upgrades from the portal](../agent/README.md#upgrades-from-the-portal) for a systemd unit and the Windows service settings.

### 4. Firewall

None needed on the source host for inbound traffic — the agent only ever initiates the connection outbound to `PERCHTAIL_SERVER_URL` over WSS. Allow that outbound destination through the source's firewall or proxy if outbound traffic is otherwise restricted. For upgrades from the portal, the agent also downloads over HTTPS from the same PerchTail server and, when the server has no copy of the release, from GitHub (`github.com`, which redirects the download to GitHub's own download host).

## Local disk (directories on the PerchTail host itself)

For logs that are already on the machine running PerchTail, typically a host directory bind-mounted read-only into the container:

```yaml
# docker-compose.yml
services:
  perchtail:
    volumes:
      - perchtail-data:/data
      - /var/log/myapp:/mnt/host-logs/myapp:ro
```

Local-disk sources are **off by default**. The server operator enables them by listing the allowed parent directories in `LOCAL_SOURCE_ROOTS` (comma-separated, absolute) in `.env`, for example `LOCAL_SOURCE_ROOTS=/mnt/host-logs`. A source's base path must sit at or under one of those directories, and reads never follow a symlink out of the source's base path. This is deliberately an environment setting rather than something an admin can change from the web UI: otherwise anyone allowed to create sources could point one at PerchTail's own database or secrets. Mount only what you mean to expose, and mount it read-only.

## Base path

What goes in **Base path** depends on the protocol, and PerchTail refuses the kinds of path that can never work when you save the source:

| Protocol | Base path | Example |
|---|---|---|
| SSH/SFTP | absolute path on the host (relative to the chroot, if the account has one) | `/var/log/myapp` |
| SMB | the **share name**, optionally followed by a folder inside it. Not a drive path, and not an administrative share (`C$`, `ADMIN$`) | `AppLogs` or `AppLogs\iis` |
| WinRM | absolute Windows path on a drive of the WinRM host. Not a UNC path (`\\server\share`) or a device path (`\\?\`, `\\.\`): read a share through an SMB source instead | `D:\Logs\MyApp` |
| Local disk | a directory under `LOCAL_SOURCE_ROOTS` | `/mnt/logs/app` |
| Agent | documentation only: the agent's `PERCHTAIL_BASE_PATH` is what counts. Spaces around it are trimmed, and one with control characters is refused | |

A WinRM source saved by an earlier version with a UNC or device path is no longer read: it answers "This source's Base path isn't a path on a drive (e.g. D:\Logs): edit the source to correct it", and the log has `winrm.base_path_not_on_drive`. Change it to a path on a drive of the WinRM host, or read the share through an SMB source.

Instead of typing it, use **Browse…** next to the field. It connects with the host and credential in the form and lists folders only, never file names: `/` and down over SSH, the drives and then folders over WinRM (with a JEA endpoint, the folders the endpoint allows), and the host's shares (the ones the account can see, without `C$`/`ADMIN$`/`IPC$`) and then folders over SMB. Browsing is limited to users who can manage sources (for a saved source, **Manage sources** on that source), and each listing is written to the audit log. A connection that can't be made gets the same message whatever the reason; the reason is in the server log. A saved source's stored credential is only ever sent to the host and port it was saved for, and browsing a saved source always reaches its saved host and port: to browse another host, or to save the source with a new host or port, type the credential again.

Rules are relative to the base path. Changing the base path or a folder (or the host or port) means the same rules now apply to whatever is at the new location, so check they still match; the source's file-name index and alert positions are cleared, and the audit log records the old and new values.

### Several folders in one source

A source can read several folders on the same host with the same credential, for example IIS logs on `D:` and application logs on `E:`. How many is set in **Settings → Source defaults → Folders per source**: 20 by default, 1 turns the feature off, and 100 at most. Lowering the limit doesn't affect sources that already have more folders, but they can't add any. Use **+ Add another folder** in the source editor and give each folder a label. In the viewer each folder appears at the top level under its label, and rules see the same paths, so `**/*.log` applies to every folder and `IIS/**` to one. The connection test checks each folder and names the ones that fail. Push-agent sources serve a single folder and can't have several.

## Testing a source

Once configured, add the source under **Settings → Sources** with the matching protocol, host, port, credential and **Base path**, then click **Test connection** on the source's page (it needs **Manage sources** on the source) to confirm reachability and permissions before attaching rules. To add many sources at once, see [bulk import](bulk-import.md).
