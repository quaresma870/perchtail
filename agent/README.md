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
