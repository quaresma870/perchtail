# perchtail-agent

The PerchTail push agent: a small binary for hosts PerchTail can't reach inbound over SSH/SMB/WinRM. It dials **out** to the server and holds a single persistent WebSocket connection open; the server then relays live `list`/`fetch` commands down that connection exactly as it would call any other connector directly. The agent never pushes files on its own — nothing is synced or mirrored ahead of time, so PerchTail's always-fresh, nothing-persisted rule holds for agent-mode sources too.

## Configuration

The agent reads five environment variables, and has no configuration file. The first three are required:

| Variable | Example | Meaning |
|---|---|---|
| `PERCHTAIL_SERVER_URL` | `wss://perchtail.example.com/agent/connect` | The server's agent endpoint |
| `PERCHTAIL_AGENT_TOKEN` | (from the source's page) | Enrollment token: **Generate token** under **Agent enrollment** on the source's page; shown once |
| `PERCHTAIL_BASE_PATH` | `/var/log/myapp` | Local root the agent is allowed to read from. Checked at startup: the agent stops with a message naming the problem if it doesn't exist, isn't a folder or can't be read by the agent's account. |
| `PERCHTAIL_ALLOW_INSECURE` | unset | Set to `true` (or `1`, `yes`, `on`) to allow a plain `ws://` URL. Without it the agent refuses to start unless the URL is `wss://`, because the token and every fetched file travel over this connection. Only for testing or a fully trusted network. |
| `PERCHTAIL_REMOTE_UPGRADE` | unset (allowed) | Set to `false` to refuse every upgrade from the portal, whatever the server asks (see below). Anything but `true`/`false` (or `1`/`0`, `yes`/`no`, `on`/`off`) stops the agent at startup, so a typo can't leave upgrades on. |

A configuration error stops the agent at startup with exit code 1 and the reason in its log. [docs/agent-service.md](../docs/agent-service.md#2-configuration) lists the messages.

## Connection

The agent pings the server every 25 seconds and reconnects when it has heard nothing from it for 75 seconds, so a connection a NAT or firewall dropped without a word is replaced instead of hanging. The server does the same the other way (a ping every 30 seconds, the agent dropped after 90 silent seconds). Reconnects back off exponentially from 1 to 30 seconds, start again from 1 second after a connection that stayed up for a minute, and each delay is random between half and one and a half times the backoff, so agents don't all come back at the same moment after a server restart.

## What it tells the server

On each connection the agent sends, besides its token, its operating system, its version and the features it supports, comma-separated. The server decides from the features, not from the version, what it asks:

- `links/1` (agents from 0.7): a listing says where each entry really is when a symbolic link (or, on Windows, a junction) leads elsewhere inside the base path, and a command that reads content is answered only when the path really is where the server expects, after the server has checked its rules on that target; otherwise it is refused before anything is read. The check and the read use the same open file, so a link swapped in between isn't followed. **The server reads no file content from an agent without it**: such an agent stays connected and can be browsed, but opening, Tail, downloads, folder ZIPs and archives are refused, and alerts skip its files, until it is upgraded, and Settings → Agents says "Update required".
- `redaction/1` (agents from 0.7): when redaction applies to the source, the server sends the rules with each request and the agent redacts what it sends on its host, with a Go port of the server's engine (checked against the same test vectors), and lists and reads archives and decompresses `.gz` files there, so the original content never leaves the host. With or without redaction, the server has such an agent list a `.zip`, `.tar.gz` or `.tgz` on its host when it is expanded, and extract there the file asked for when one inside it is opened or downloaded, so only that file is sent, not the whole archive; the server still checks its rules on each file inside before asking for it. A whole archive downloaded, a folder ZIP and a plain `.gz` file are still sent whole. An agent older than 0.7 has neither feature, so the server reads none of its files until it is upgraded (see `links/1` above).
- `progress/1` (agents from 0.7.0): when the server asks for it with a request that reads content (a file, an archive member, a decompressed `.gz` file, a Tail or alert read, an archive's members, the listing of an archive), the agent says every 10 seconds that it is still working, while it has read more of the file (or of the archive, up to the member asked for or as far as it has listed it) and hasn't sent anything else in that time; a read that hangs sends nothing, so it still fails after 30 seconds. The server counts that as an answer to its 30-second wait, so a member near the end of a large `.tar.gz`, which the agent must read up to before it sends anything (always the case when it extracts it on its host), or a large `.tar.gz` it must read through to list (when it is expanded, or downloaded whole), is no longer given up as unanswered. The size limits below still bound how much the agent reads. A server that doesn't ask gets no such message, and an agent without the feature is never asked.

## Limits

- A listing (a folder, or a `.tar.gz` archive's members) stops at 50,000 entries, or earlier once the answer nears 8 MiB, and says it was cut short; a server from 0.7 asks for its own cap, never above that. A `.zip` with more than 50,000 entries isn't listed at all: it is refused with `archive_unreadable`.
- At most 8 commands run at once, of which at most 2 redact.
- Content is sent in pieces of at most 1 MiB, and a ranged read (Tail) reads at most 1 MiB. The largest file and the most decompression may produce are set by the server in each request (its `MAX_FETCH_MB` and `MAX_DECOMPRESSED_MB`); a refusal says which of the two was passed, so the server names the right one even when they are the same number.
- Upgrade downloads are capped (256 MiB for the binary) and each may take at most 10 minutes; the agent must accept or refuse an upgrade within 15 seconds.

## Upgrades from the portal

From 0.7, agents are upgraded from **Settings → Agents** in PerchTail. The page lists the agents of the sources a user can manage (the **Create new sources** global capability and `manage_source` on the source), and those users can upgrade them; changing **Remote upgrades**, the window and the failure limit needs **Manage system settings**. Agents older than 0.7 don't know the command: upgrade them by hand once ([docs/agent-service.md](../docs/agent-service.md#upgrading-by-hand)); until then, the server reads none of their files.

- **Remote upgrades** applies to every agent: **Off** (the default), **Manual** (the page's **Upgrade** button on an agent, or **Upgrade all outdated agents**) or **Automatic** (also upgraded by the server inside a window). Each agent can **Follow the system** (the default) or have its own policy, either way: an agent can be upgraded with the system Off, or stay Off with the system Automatic.
- **The automatic window** is a set of days of the week, a start time and a length in hours (1 to 24), in the server's time zone (shown on the page); a window may run past midnight. Inside it the server upgrades connected outdated agents whose policy is Automatic, a few at a time (5 by default, 1 to 50), and doesn't ask an agent again in the same window once its upgrade didn't work.
- **The failure limit** (3 by default, 1 to 10): after that many failed upgrades in a row (failed, refused or timed out), automatic upgrades skip the agent until someone clicks **Resume**, which clears the count. A new server version doesn't resume it. **Upgrade** still works on a suspended agent; a success clears the count, a failure adds to it. An agent too old for the command isn't counted, and isn't asked again until it reports another version.
- Every attempt is kept: who asked (a user or the schedule), the from and to versions, when it started and ended, and how it ended. Clicking an agent's last result shows its history. The page also summarises the last automatic window (upgraded, failed, suspended).
- `PERCHTAIL_REMOTE_UPGRADE=false` on the host still refuses every upgrade, whatever the policy.

The server only says which version to upgrade to: its own. The agent then:

1. refuses straight away if `PERCHTAIL_REMOTE_UPGRADE=false`, if it was built without the release public key, if its own version isn't a release (a `dev` build), or if the target isn't newer than its own version (no downgrades, no reinstalling the same version);
2. downloads `SHA256SUMS`, `SHA256SUMS.sig` and `perchtail-agent-<version>-<os>-<arch>[.exe]` from the PerchTail server it's connected to (`/agent/binaries/` next to `PERCHTAIL_SERVER_URL`, with its own token), or, if the server has no valid copy, from `https://github.com/quaresma870/perchtail/releases/download/v<version>/` — the only two places, never a URL the server names;
3. checks the Ed25519 signature over `SHA256SUMS` against the release public key built into the agent (`internal/agent/upgrade_signing_key.pub`), then the binary's SHA-256 against its line, with downloads capped in size;
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
# and, with the release public key (internal/agent/upgrade_signing_key.pub):
openssl pkeyutl -verify -pubin -inkey upgrade_signing_key.pub -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig
```

On Windows: `Get-FileHash .\perchtail-agent-<version>-windows-amd64.exe` and compare with the line in `SHA256SUMS`.

## Build

Build with Go 1.27 or later (`go.mod`), from this folder, setting the version the way the release workflow does (`.github/workflows/release.yml`): the agent reports it when it connects, and upgrades from the portal compare it with the server's.

```sh
VERSION=X.Y.Z   # the release tag you checked out, without the "v"
CGO_ENABLED=0 go build -trimpath \
  -ldflags "-s -w -X github.com/quaresma870/perchtail/agent/internal/agent.Version=${VERSION}" \
  -o perchtail-agent .
```

Cross-compile for another platform with `GOOS` and `GOARCH`, as the release does:

```sh
for target in linux/amd64 linux/arm64 windows/amd64; do
  os="${target%/*}"; arch="${target#*/}"
  ext=""; [ "$os" = windows ] && ext=".exe"
  CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" go build -trimpath \
    -ldflags "-s -w -X github.com/quaresma870/perchtail/agent/internal/agent.Version=${VERSION}" \
    -o "perchtail-agent-${VERSION}-${os}-${arch}${ext}" .
done
```

The version must be a release version: `X.Y.Z`, `X.Y.Z-rc.N` or `X.Y.Z-beta.N` (a leading `v` is accepted). Built without `-X ...Version=`, as with a plain `go build .`, the agent reports version `dev` and refuses upgrades from the portal: it can't tell whether a release is newer than itself. A build from a checkout whose `internal/agent/upgrade_signing_key.pub` is empty refuses them too. Set the version only to the release your checkout really is (`git checkout vX.Y.Z`): an agent built from other code but labelled with a release version would be treated as that release.

## Test

```sh
go test ./...
```
