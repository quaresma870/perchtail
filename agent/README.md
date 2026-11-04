# perchtail-agent

The Phase 2 push-agent (see the repo root `ROADMAP.md`): a small binary for
hosts PerchTail can't reach inbound over SSH/SMB/WinRM. It dials **out** to
the server and holds a single persistent WebSocket connection open; the
server then relays live `list`/`fetch` commands down that connection exactly
as it would call any other connector directly. The agent never pushes files
on its own — nothing is synced or mirrored ahead of time, so PerchTail's
always-fresh, nothing-persisted rule holds for agent-mode sources too.

## Configuration

Three environment variables, all required:

| Variable | Example | Meaning |
|---|---|---|
| `PERCHTAIL_SERVER_URL` | `wss://perchtail.example.com/agent/connect` | The server's agent endpoint |
| `PERCHTAIL_AGENT_TOKEN` | (from the admin UI) | Enrollment token, issued once via `POST /sources/{id}/agent-token` |
| `PERCHTAIL_BASE_PATH` | `/var/log/myapp` | Local root the agent is allowed to read from |
| `PERCHTAIL_ALLOW_INSECURE` | unset | Set to `true` to allow a plain `ws://` URL. Without it the agent refuses to start unless the URL is `wss://`, because the token and every fetched file travel over this connection. Only for testing or a fully trusted network. |

## Download

Every [GitHub release](https://github.com/quaresma870/perchtail/releases)
has prebuilt binaries for Linux (amd64, arm64) and Windows (amd64), named
`perchtail-agent-<version>-<os>-<arch>`, and a `SHA256SUMS` file. Check the
download before installing it:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

On Windows: `Get-FileHash .\perchtail-agent-<version>-windows-amd64.exe` and
compare with the line in `SHA256SUMS`.

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

## Test

```sh
go test ./...
```
