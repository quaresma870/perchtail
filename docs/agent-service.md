# Running the push agent as a service

This guide is for administrators who install the PerchTail push agent on a host and keep it running and up to date as a service on Linux or Windows.

Related guides: [configuration.md](configuration.md) (server settings), [upgrade.md](upgrade.md) (upgrading the server), [admin-guide.md](admin-guide.md) (sources, rules and roles), [user-guide.md](user-guide.md), [troubleshooting.md](troubleshooting.md#push-agent), [source-setup.md](source-setup.md#agent-push-agent-for-hosts-not-reachable-inbound) and the agent's own reference, [agent/README.md](../agent/README.md).

## How the agent works

The agent is a small program for hosts that PerchTail can't reach over SSH, SMB or WinRM.

It dials out to the PerchTail server and keeps one WebSocket connection open. The server sends list and read commands down that connection when a user browses or opens a file. The agent never sends files on its own, and nothing is copied ahead of time.

The agent reads only under one local folder, `PERCHTAIL_BASE_PATH`. The source's rules in PerchTail then decide which of those files users see.

One agent serves one source. A second agent that connects with the same token replaces the first one, and the two keep replacing each other: give each host its own source and token.

## Before you start

- You need the **Create new sources** global capability and the `manage_source` capability where the source goes (its customer or folder). A super admin has both.
- The host needs outbound access to the PerchTail server on HTTPS (port 443 behind the usual reverse proxy). Nothing needs to reach the host inbound.
- For upgrades from the portal, the host also needs to reach the PerchTail server's `/agent/binaries/` path, or `github.com` when the server doesn't carry the agent binaries (see [Upgrading from the portal](#upgrading-from-the-portal)).

## 1. Create the source and its token

1. Open **Settings → Sources** and click **+ Add source**.
2. Set **Protocol** to **Agent (push)**.
3. Fill in **Name**, and in **Host** a friendly name for the agent's host. PerchTail doesn't connect to it; it is a label.
4. **Base path** is optional for an agent. It is only a note: the agent's own `PERCHTAIL_BASE_PATH` decides what it can read. A push-agent source serves one folder and can't have several.
5. Click **Save**. The source page opens.
6. Under **Agent enrollment**, click **Generate token**. Copy the token straight away: it is shown once. PerchTail keeps only a hash of it.
7. Add at least one include rule, such as `**/*.log`. A source with no rules shows nothing.

The token is also on the edit page, under **Agent enrollment**. Once a source has a token, the button reads **Regenerate token**. A new token invalidates the old one at once: PerchTail closes the agent's connection, and the agent can't come back until its configuration has the new token.

Disabling or deleting the source also closes the agent's connection and refuses it from then on.

## 2. Configuration

The agent reads environment variables only. It has no configuration file and no command-line options.

| Variable | Required | Example | Meaning |
|---|---|---|---|
| `PERCHTAIL_SERVER_URL` | yes | `wss://perchtail.example.com/agent/connect` | The server's agent endpoint. The path is always `/agent/connect`. |
| `PERCHTAIL_AGENT_TOKEN` | yes | the token from step 1 | The source's enrollment token. |
| `PERCHTAIL_BASE_PATH` | yes | `/var/log/myapp` or `D:\Logs\MyApp` | The only local folder the agent reads from. |
| `PERCHTAIL_ALLOW_INSECURE` | no | unset | `true` allows a plain `ws://` server URL. |
| `PERCHTAIL_REMOTE_UPGRADE` | no | unset (upgrades allowed) | `false` refuses every upgrade from the portal. |

The agent checks its configuration once, at startup. When a check fails, it logs the reason and exits with code 1:

- A missing required variable stops it with `PERCHTAIL_SERVER_URL is required`, `PERCHTAIL_AGENT_TOKEN is required` or `PERCHTAIL_BASE_PATH is required`.
- `PERCHTAIL_SERVER_URL` must parse as a URL with a host. Otherwise: `PERCHTAIL_SERVER_URL "<value>" is not a valid URL`.
- The URL must start with `wss://`. A `ws://` URL is refused unless `PERCHTAIL_ALLOW_INSECURE` is true. Any other scheme (`https://`, for example) is refused with `PERCHTAIL_SERVER_URL must start with wss:// (got "https")`.
- `PERCHTAIL_ALLOW_INSECURE` counts as true for `1`, `true`, `yes` or `on`, in any letter case. Any other value, a typo included, counts as false, so the agent stays on `wss://`.
- `PERCHTAIL_REMOTE_UPGRADE` accepts `true`, `1`, `yes`, `on` (allowed) and `false`, `0`, `no`, `off` (refused), in any letter case. Unset or empty allows upgrades. Any other value stops the agent with `PERCHTAIL_REMOTE_UPGRADE must be true or false (got "<value>")`, so a typo can't leave upgrades on.

The agent doesn't check `PERCHTAIL_BASE_PATH` at startup. If the folder doesn't exist or the agent's account can't read it, the agent still connects, and browsing the source fails with "Not found on the source" or "The source account has no read permission for this path". Use **Test connection** on the source page to check it.

The agent logs to standard error, one line per event, for example `agent: connected to wss://perchtail.example.com/agent/connect`. Its first line gives its version: `perchtail-agent 0.7.0`.

## 3. Connection, TLS and the reverse proxy

### wss:// and ws://

Use `wss://`. The token travels in the connection's `Authorization` header, and every file the agent sends goes over the same connection.

`ws://` sends both in clear text. The agent refuses it unless `PERCHTAIL_ALLOW_INSECURE=true`, with this message:

```text
PERCHTAIL_SERVER_URL uses unencrypted ws://, which would send the agent token and every fetched file in clear text; use wss://, or set PERCHTAIL_ALLOW_INSECURE=true for a trusted network (e.g. testing)
```

Set it only for a test, or on a network you fully trust.

The agent checks the server's certificate against the host's trust store. If the certificate comes from an internal CA, add that CA to the host's trust store. On Linux, Go programs also read `SSL_CERT_FILE` and `SSL_CERT_DIR`.

The agent honours `HTTPS_PROXY` and `NO_PROXY` for the connection and for upgrade downloads.

### Reverse proxy

The reverse proxy in front of PerchTail must pass WebSocket upgrades on `/agent/connect`. The nginx server block in the [README](../README.md#deployment-reverse-proxy-nginx--tls) already does this for every path.

If you proxy the agent path on its own, a minimal nginx location is:

```nginx
location = /agent/connect {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

For upgrades from the portal, also forward `/agent/binaries/` (a plain HTTPS download, no WebSocket). The agent builds that address from `PERCHTAIL_SERVER_URL`: the same host, with `binaries/` in place of `connect`.

Without the WebSocket headers, the handshake fails and the agent logs `dial: websocket: bad handshake`.

### Keepalive and reconnects

- The agent pings the server every 25 seconds. When it has heard nothing from the server for 75 seconds, it drops the connection and reconnects.
- The server pings each agent every 30 seconds and drops an agent it has heard nothing from for 90 seconds.
- So a connection that a NAT device or firewall dropped silently is replaced within about a minute and a half, and the source shows the agent as not connected in the meantime.
- After a lost connection, the agent waits and tries again. The wait doubles from 1 second up to 30 seconds. After a connection that stayed up for at least a minute, it starts again from 1 second. Each wait is random, between half and one and a half times the current step, so agents don't all reconnect at the same moment after a server restart.
- The agent keeps trying for as long as it runs, also when the server refuses it (wrong token, disabled source). Check its log if it doesn't come back.
- On a stop signal (`systemctl stop`, Ctrl+C) the agent closes the connection cleanly and exits with code 0.

## 4. Install on Linux with systemd

Prebuilt binaries for Linux (amd64, arm64) are attached to every [GitHub release](https://github.com/quaresma870/perchtail/releases). Check the download first: see [Upgrading by hand](#upgrading-by-hand).

1. Create a dedicated system account with no login shell:

   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin perchtail-agent
   ```

2. Give that account read access to the log folder, and nothing more. For example, with an ACL that also covers files created later:

   ```bash
   sudo setfacl -R -m u:perchtail-agent:rX /var/log/myapp
   sudo setfacl -R -d -m u:perchtail-agent:rX /var/log/myapp
   ```

3. Put the binary in a folder of its own. If you want upgrades from the portal, the agent's account must own that folder (see [Permissions](#6-permissions-of-the-agents-account)):

   ```bash
   sudo install -d -o perchtail-agent -g perchtail-agent -m 0755 /opt/perchtail-agent
   sudo install -o perchtail-agent -g perchtail-agent -m 0755 perchtail-agent-0.7.0-linux-amd64 /opt/perchtail-agent/perchtail-agent
   ```

   Without upgrades from the portal, keep both owned by root, and set `PERCHTAIL_REMOTE_UPGRADE=false`.

4. Write the environment file, readable only by root. systemd reads it before it starts the agent as `perchtail-agent`:

   ```bash
   sudo install -m 0600 -o root -g root /dev/null /etc/perchtail-agent.env
   sudoedit /etc/perchtail-agent.env
   ```

   ```ini
   PERCHTAIL_SERVER_URL=wss://perchtail.example.com/agent/connect
   PERCHTAIL_AGENT_TOKEN=<the token>
   PERCHTAIL_BASE_PATH=/var/log/myapp
   ```

5. Create `/etc/systemd/system/perchtail-agent.service`:

   ```ini
   [Unit]
   Description=PerchTail push agent
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=perchtail-agent
   EnvironmentFile=/etc/perchtail-agent.env
   ExecStart=/opt/perchtail-agent/perchtail-agent
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   `Restart=always` brings the agent back after any exit, a configuration error included (it then retries every 5 seconds and logs why). After an upgrade from the portal, the agent restarts in place with the same process ID, so systemd doesn't see it stop.

6. Start it and check its log:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now perchtail-agent
   journalctl -u perchtail-agent -f
   ```

   Look for `agent: connected to wss://...`.

## 5. Install on Windows with a service wrapper

The agent is a console program. It doesn't register itself as a Windows service. Run it under a service wrapper such as [NSSM](https://nssm.cc/) or [WinSW](https://github.com/winsw/winsw).

The wrapper must start the agent again whenever it exits. After an upgrade from the portal, the agent exits with code **3** so that the wrapper starts the new binary. A running program can't be replaced on Windows, so this is the only way the new version starts. If the wrapper doesn't restart it, the agent stays stopped and the upgrade ends as "Didn't come back" after 10 minutes.

The prebuilt Windows binary (amd64) is `perchtail-agent-<version>-windows-amd64.exe`. Put it in a folder of its own and rename it, for example to `C:\Program Files\PerchTail Agent\perchtail-agent.exe`.

### With NSSM

```powershell
nssm install PerchTailAgent "C:\Program Files\PerchTail Agent\perchtail-agent.exe"
nssm set PerchTailAgent AppEnvironmentExtra "PERCHTAIL_SERVER_URL=wss://perchtail.example.com/agent/connect" "PERCHTAIL_AGENT_TOKEN=<the token>" "PERCHTAIL_BASE_PATH=D:\Logs\MyApp"
nssm set PerchTailAgent AppExit Default Restart
nssm set PerchTailAgent AppRestartDelay 5000
nssm set PerchTailAgent AppStderr "C:\ProgramData\PerchTail Agent\agent.log"
nssm start PerchTailAgent
```

NSSM restarts a program that exits by default. `AppExit Default Restart` makes it explicit. To restart only after an upgrade, use `nssm set PerchTailAgent AppExit 3 Restart` instead. `AppStderr` keeps the agent's log in a file; the service account needs write access to that folder.

### With WinSW

Put the WinSW executable and its XML file next to each other, for example in `C:\Program Files\PerchTail Agent\`:

```xml
<service>
  <id>PerchTailAgent</id>
  <name>PerchTail Agent</name>
  <description>PerchTail push agent</description>
  <executable>C:\Program Files\PerchTail Agent\perchtail-agent.exe</executable>
  <env name="PERCHTAIL_SERVER_URL" value="wss://perchtail.example.com/agent/connect"/>
  <env name="PERCHTAIL_AGENT_TOKEN" value="the token"/>
  <env name="PERCHTAIL_BASE_PATH" value="D:\Logs\MyApp"/>
  <onfailure action="restart" delay="5 sec"/>
</service>
```

The XML file holds the token: let only Administrators and the service account read it.

### Recovery actions

If your wrapper stops the service when the program exits instead of restarting the program, set the service's recovery actions so that Windows starts it again. `failureflag 1` makes them apply to a non-zero exit code as well as to a crash:

```powershell
sc.exe failure PerchTailAgent reset= 86400 actions= restart/5000/restart/5000/restart/5000
sc.exe failureflag PerchTailAgent 1
```

Setting them is a good safety net with NSSM and WinSW too.

### The service account

Don't run the agent as LocalSystem. Use a dedicated account, or the service's virtual account, which needs no password:

```powershell
sc.exe config PerchTailAgent obj= "NT SERVICE\PerchTailAgent"
icacls "D:\Logs\MyApp" /grant "NT SERVICE\PerchTailAgent:(OI)(CI)RX"
```

For upgrades from the portal, also give it Modify rights on the agent's folder:

```powershell
icacls "C:\Program Files\PerchTail Agent" /grant "NT SERVICE\PerchTailAgent:(OI)(CI)M"
```

On Windows, PerchTail matches the source's rules without regard to letter case, as NTFS does. The agent reports its OS each time it connects, and the source page shows it as **Agent OS**.

## 6. Permissions of the agent's account

Give the agent's account the least it needs:

- **Read** on `PERCHTAIL_BASE_PATH` and below: list folders and read files. Never write.
- **Write on its own binary folder**, only if you want upgrades from the portal. The agent writes the new binary next to the running one, keeps the previous one as `<binary>.old`, and moves the new one in place. If it can't write there, the upgrade fails before anything changes and says so.
- Nothing else: no administrator rights, no login shell, no access to other folders.

If your change control doesn't allow the agent to replace its own binary, keep the folder read-only for the agent's account, set `PERCHTAIL_REMOTE_UPGRADE=false`, and upgrade by hand.

The agent refuses paths that leave `PERCHTAIL_BASE_PATH`, including through a symbolic link or a junction that points outside it. That doesn't replace the operating system's permissions: an account that can read only the log folder is still the real boundary.

A link inside `PERCHTAIL_BASE_PATH` (a symbolic link, or on Windows also a junction) is followed only to a file or folder the source's rules show: the agent tells the server where each path really is, and reads it only once the server, having checked its rules on that target, asks for it. `current.log -> app-2026-11-16.log` keeps working; a link to a file the rules hide is left out of the listing and can't be opened.

## 7. Upgrading

The agent never upgrades itself on its own. It upgrades when you replace the binary, or when the server asks it to and the host allows it.

From 0.7, the server reads files only from an agent that tells it where links lead (an agent from 0.7 on). An older agent stays connected and its folders can still be browsed, but opening, following, downloading (files, folder ZIPs and archives) and alerts are refused with a message asking for the agent to be updated, each refusal recorded in the audit log (reason `agent_update_required`). **Settings → Agents** marks such an agent with **Update required**. Agents older than 0.7 must be upgraded by hand once, as below.

### Upgrading by hand

Agents older than 0.7 can only be upgraded this way, once. So can any agent with `PERCHTAIL_REMOTE_UPGRADE=false`, and local builds (they report version `dev`).

1. Download from the [GitHub release](https://github.com/quaresma870/perchtail/releases) the binary for the host, `SHA256SUMS` and `SHA256SUMS.sig`:

   ```bash
   VERSION=0.7.0
   BASE=https://github.com/quaresma870/perchtail/releases/download/v$VERSION
   curl -fLO "$BASE/perchtail-agent-$VERSION-linux-amd64"
   curl -fLO "$BASE/SHA256SUMS"
   curl -fLO "$BASE/SHA256SUMS.sig"
   ```

2. Check the binary's SHA-256 against `SHA256SUMS`:

   ```bash
   sha256sum --check --ignore-missing SHA256SUMS
   ```

   On Windows, run `Get-FileHash .\perchtail-agent-<version>-windows-amd64.exe` and compare the hash with the binary's line in `SHA256SUMS`.

3. Check the Ed25519 signature over `SHA256SUMS` with the release public key, `agent/internal/agent/upgrade_signing_key.pub` in the repository at the release's tag. This needs OpenSSL 3.0 or later:

   ```bash
   openssl pkeyutl -verify -pubin -inkey upgrade_signing_key.pub -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig
   ```

   It prints `Signature Verified Successfully`. Windows has no built-in tool for this: check the files on a machine with OpenSSL, then copy them over.

4. Replace the binary and restart the service.

   Linux:

   ```bash
   sudo systemctl stop perchtail-agent
   sudo install -o perchtail-agent -g perchtail-agent -m 0755 perchtail-agent-0.7.0-linux-amd64 /opt/perchtail-agent/perchtail-agent
   sudo systemctl start perchtail-agent
   ```

   Windows:

   ```powershell
   Stop-Service PerchTailAgent
   Copy-Item .\perchtail-agent-0.7.0-windows-amd64.exe "C:\Program Files\PerchTail Agent\perchtail-agent.exe" -Force
   Start-Service PerchTailAgent
   ```

5. Check the source page: **Agent version** shows the new version once the agent has reconnected.

### Upgrading from the portal

From 0.7, agents can be upgraded from **Settings → Agents**. The page lists every push-agent source with its **Connection**, the **Agent** version, the **Server** version, its **Policy** and its **Last result**. An agent older than the server is marked as outdated.

Who can do what:

- The **Agents** page is in the Settings menu for users with **Create new sources**. It lists the agents of the sources where they also have `manage_source`, and they can upgrade those agents, change their policy and resume them.
- The system-wide settings at the top of the page are System settings: changing them needs **Manage system settings**.

The settings:

- **Remote upgrades**, for every agent: **Off** (the default), **Manual** (the **Upgrade** button on an agent's row, and **Upgrade all outdated agents**) or **Automatic** (also upgraded by the server inside the window).
- Each agent's **Policy**: **Follow the system** (the default), or its own **Off**, **Manual** or **Automatic**. Either way works: an agent can be upgraded with the system Off, or stay Off with the system Automatic.
- **Automatic window**: days of the week, a **Start** time and a length of 1 to 24 hours, in the server's time zone, which the page shows. A window may run past midnight. The default is every day from 02:00 for 4 hours. In the Docker image the server's time zone is UTC unless you set `TZ` in `.env`.
- **Failure limit** (1 to 10, default 3): after this many failed, refused or timed-out upgrades in a row, automatic upgrades skip the agent. Its row shows "suspended after N failures" and a **Resume** button. Resume clears the count. A new server version doesn't resume an agent on its own. **Upgrade** still works on a suspended agent: a success clears the count, a failure adds to it.
- **At once** (1 to 50, default 5): how many agents the schedule upgrades at the same time.

Inside the window, the server upgrades connected, outdated agents whose policy is Automatic and that aren't suspended. It doesn't ask an agent again in the same window once its upgrade didn't work. The page summarises the last window: upgraded, failed, suspended.

**Upgrade** can't start, and its tooltip says why, when remote upgrades are off for the agent, the source is disabled, the agent isn't connected, the agent reports a version that isn't a release, or an upgrade is already running.

What happens during an upgrade:

1. The server sends the agent its own version, and nothing else. The agent has 15 seconds to accept or refuse.
2. The agent refuses at once if `PERCHTAIL_REMOTE_UPGRADE=false`, if it was built without the release public key, if its own version isn't a release, or if the server's version isn't newer than its own. Agents never downgrade.
3. The agent downloads `SHA256SUMS`, `SHA256SUMS.sig` and its binary from the PerchTail server (`/agent/binaries/`, with its own token). If the server has no valid copy, it downloads them from the GitHub release. It never uses an address the server names.
4. It checks the Ed25519 signature over `SHA256SUMS` against the release key built into the agent, then the binary's SHA-256 against its line.
5. It writes the new binary next to the running one, keeps the previous one as `<binary>.old`, and moves the new one in place. On Windows the running binary is renamed to `perchtail-agent.exe.old`; the next upgrade deletes it.
6. It restarts into the new binary: in place on Linux (same process ID), by exiting with code 3 on Windows.

A failed download or check leaves the current binary in place and running. A failed restart on Linux puts the previous binary back. The agent reports why, and the **Last result** shows it.

The upgrade counts as **Upgraded** only once the agent reconnects with the new version. With no news from the agent for 10 minutes, it ends as **Didn't come back**.

Click an agent's **Last result** to see every attempt: when it started, who asked (a user, or **Schedule**), the versions, the result and how long it took. The source page shows the last result too.

The Docker image carries the signed agent binaries of its own version, so hosts without internet access can upgrade. A server installed from source doesn't, and the page says so ("This server doesn't carry the agent binaries"); agents then download from GitHub, so their hosts need to reach `github.com`. To serve the binaries from a source install, put the release's `SHA256SUMS`, `SHA256SUMS.sig` and `perchtail-agent-*` files of the server's version in a folder and set `AGENT_BINARIES_DIR` to it (see [configuration.md](configuration.md)).

A host can always refuse with `PERCHTAIL_REMOTE_UPGRADE=false`, whatever the policy.

### Agents too old for the upgrade command

Agents older than 0.7 don't know the upgrade command:

- An agent from 0.6 reports its version, so it shows as outdated. If it's asked to upgrade, it doesn't understand the command, and the attempt ends as **Too old to upgrade remotely**, with "This agent is too old to upgrade remotely: upgrade it once by hand, and later versions can be upgraded from here". This doesn't count toward the failure limit, and automatic upgrades don't ask it again until it reports another version.
- An agent older than 0.6 doesn't report its version. Its version shows as unknown, and it can't be upgraded from the portal.

Upgrade these agents [by hand](#upgrading-by-hand) once. Check at the same time that the service restarts the agent on exit and that the account can write to the binary's folder, so the next upgrades can come from the portal.

## Checking that it works

- The source page shows **Agent** as connected, the **Agent OS** and the **Agent version**.
- In the Viewer, the status dot before the source has the tooltip "Agent connected". When the agent is away, it reads "Agent not connected (last seen ...)".
- **Test connection** on the source page lists the base path through the agent.

If something doesn't work, see [Push agent](troubleshooting.md#push-agent) in the troubleshooting guide.
