# perchtail-agent

The Phase 2 push-agent (see the repo root `ROADMAP.md`): a small binary for hosts PerchTail can't reach inbound over SSH/SMB/WinRM. It dials **out** to the server and holds a single persistent WebSocket connection open; the server then relays live `list`/`fetch` commands down that connection exactly as it would call any other connector directly. The agent never pushes files on its own — nothing is synced or mirrored ahead of time, so PerchTail's always-fresh, nothing-persisted rule holds for agent-mode sources too.

## Configuration

Environment variables; the first three are required:

| Variable | Example | Meaning |
|---|---|---|
| `PERCHTAIL_SERVER_URL` | `wss://perchtail.example.com/agent/connect` | The server's agent endpoint |
| `PERCHTAIL_AGENT_TOKEN` | (from the admin UI) | Enrollment token, issued once via `POST /sources/{id}/agent-token` |
| `PERCHTAIL_BASE_PATH` | `/var/log/myapp` | Local root the agent is allowed to read from |
| `PERCHTAIL_ALLOW_INSECURE` | unset | Set to `true` to allow a plain `ws://` URL. Without it the agent refuses to start unless the URL is `wss://`, because the token and every fetched file travel over this connection. Only for testing or a fully trusted network. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | Set to `false` to refuse every upgrade from the portal, whatever the server asks (see below). Anything but `true`/`false` (or `1`/`0`, `yes`/`no`, `on`/`off`) stops the agent at startup, so a typo can't leave upgrades on. |

## Connection

The agent pings the server every 25 seconds and reconnects when it has heard nothing from it for 75 seconds, so a connection a NAT or firewall dropped without a word is replaced instead of hanging. The server does the same the other way (a ping every 30 seconds, the agent dropped after 90 silent seconds). Reconnects back off exponentially from 1 to 30 seconds, start again from 1 second after a connection that stayed up for a minute, and each delay is random between half and one and a half times the backoff, so agents don't all come back at the same moment after a server restart.

## Upgrades from the portal

From 0.7, agents are upgraded from **Settings → Agents** in PerchTail, by users who can manage sources. Agents older than 0.7 don't know the command: upgrade them by hand once.

- **Remote upgrades** applies to every agent: **Off** (the default), **Manual** (the page's **Upgrade** button on an agent, or **Upgrade all outdated agents**) or **Automatic** (also upgraded by the server inside a window). Each agent can **Follow the system** (the default) or have its own policy, either way: an agent can be upgraded with the system Off, or stay Off with the system Automatic.
- **The automatic window** is a set of days of the week, a start time and a length in hours (1 to 24), in the server's time zone (shown on the page); a window may run past midnight. Inside it the server upgrades connected outdated agents whose policy is Automatic, a few at a time (5 by default, 1 to 50), and doesn't ask an agent again in the same window once its upgrade didn't work.
- **The failure limit** (3 by default, 1 to 10): after that many failed upgrades in a row (failed, refused or timed out), automatic upgrades skip the agent until someone clicks **Resume**, which clears the count. A new server version doesn't resume it. **Upgrade** still works on a suspended agent; a success clears the count, a failure adds to it. An agent too old for the command isn't counted, and isn't asked again until it reports another version.
- Every attempt is kept: who asked (a user or the schedule), the from and to versions, when it started and ended, and how it ended. Clicking an agent's last result shows its history. The page also summarises the last automatic window (upgraded, failed, suspended).
- `PERCHTAIL_REMOTE_UPGRADE=false` on the host still refuses every upgrade, whatever the policy.

The server only says which version to upgrade to: its own. The agent then:

1. refuses straight away if `PERCHTAIL_REMOTE_UPGRADE=false`, if it was built without the release public key, if its own version isn't a release (a `dev` build), or if the target isn't newer than its own version (no downgrades, no reinstalling the same version);
2. downloads `SHA256SUMS`, `SHA256SUMS.sig` and `perchtail-agent-<version>-<os>-<arch>[.exe]` from the PerchTail server it's connected to (`/agent/binaries/` next to `PERCHTAIL_SERVER_URL`, with its own token), or, if the server has no valid copy, from `https://github.com/quaresma870/perchtail/releases/download/v<version>/` — the only two places, never a URL the server names;
3. checks the Ed25519 signature over `SHA256SUMS` against the release public key built into the agent (`internal/agent/upgrade_signing_key.pem`), then the binary's SHA-256 against its line, with downloads capped in size;
4. writes the new binary next to the current one, keeps the current one as `<binary>.old`, and moves the new one in place;
5. restarts into it: on Linux it replaces its own process (same PID, so systemd keeps tracking it); on Windows it exits with code **3** for the service manager to start the new binary.

A failed download or check leaves the current binary untouched and running; a failed restart puts the previous binary back. Either way the agent reports why, and Settings → Agents shows it. The upgrade has worked only once the agent reconnects with the new version; with no news from the agent for 10 minutes it counts as failed ("the agent didn't come back").

So that a compromised server can't make the agent run anything else, everything that matters is fixed in the agent: where the files come from, the key that must have signed them, and that the version must be newer.

### What the host needs

- **The agent's account must be able to write to the folder its binary is in**, to place the new binary and the `.old` backup next to it. Put the agent in a folder of its own owned by that account, for example `/opt/perchtail-agent/` on Linux or `C:\Program Files\PerchTail Agent\` with Modify rights for the service account on Windows. If it can't write there, the upgrade fails before anything is changed and says so. If your change control doesn't allow that, leave the folder read-only for the agent or set `PERCHTAIL_REMOTE_UPGRADE=false`, and upgrade by hand.
- **The service must restart the agent.**

Linux, systemd — for example `/etc/systemd/system/perchtail-agent.service`:

```ini
[Unit]
Description=PerchTail push agent
After=network-online.target
Wants=network-online.target

[Service]
User=perchtail-agent
# PERCHTAIL_SERVER_URL, PERCHTAIL_AGENT_TOKEN, PERCHTAIL_BASE_PATH, ...
EnvironmentFile=/etc/perchtail-agent.env
ExecStart=/opt/perchtail-agent/perchtail-agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The agent restarts in place after an upgrade, so systemd doesn't even see it stop; `Restart=always` brings it back after any other exit. Keep `/etc/perchtail-agent.env` readable only by root (`chmod 600`): it holds the token.

Windows — the agent is a console program, run as a service by a wrapper such as [NSSM](https://nssm.cc/) or [WinSW](https://github.com/winsw/winsw). After an upgrade it exits with code 3, and the wrapper must start it again:

- NSSM restarts the program whenever it exits by default; to be explicit: `nssm set PerchTailAgent AppExit Default Restart` (or only for the upgrade: `nssm set PerchTailAgent AppExit 3 Restart`), and `nssm set PerchTailAgent AppRestartDelay 5000`.
- WinSW: `<onfailure action="restart" delay="5 sec"/>` in the service's XML.
- For a wrapper that stops the service when the program exits, set the service's recovery actions so Windows starts it again, and make them apply to a non-zero exit as well as a crash:

  ```powershell
  sc.exe failure PerchTailAgent reset= 86400 actions= restart/5000/restart/5000/restart/5000
  sc.exe failureflag PerchTailAgent 1
  ```

On Windows the running binary is renamed to `perchtail-agent.exe.old` during the upgrade (a running program can't be replaced, only renamed); it is deleted at the next upgrade.

## Download

Every [GitHub release](https://github.com/quaresma870/perchtail/releases) has prebuilt binaries for Linux (amd64, arm64) and Windows (amd64), named `perchtail-agent-<version>-<os>-<arch>`, a `SHA256SUMS` file and `SHA256SUMS.sig`, its Ed25519 signature with the release key. Check the download before installing it:

```sh
sha256sum --check --ignore-missing SHA256SUMS
# and, with the release public key (internal/agent/upgrade_signing_key.pem):
openssl pkeyutl -verify -pubin -inkey upgrade_signing_key.pem -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig
```

On Windows: `Get-FileHash .\perchtail-agent-<version>-windows-amd64.exe` and compare with the line in `SHA256SUMS`.

## Build

```sh
go build .
```

Cross-compile for another platform:

```sh
GOOS=linux   GOARCH=amd64 go build -o perchtail-agent-linux-amd64   .
GOOS=linux   GOARCH=arm64 go build -o perchtail-agent-linux-arm64   .
GOOS=windows GOARCH=amd64 go build -o perchtail-agent-windows-amd64.exe .
```

A local build reports version `dev` and refuses upgrades from the portal: it can't tell whether a release is newer than itself. A build from a checkout whose `internal/agent/upgrade_signing_key.pem` is empty refuses them too.

## Test

```sh
go test ./...
```
