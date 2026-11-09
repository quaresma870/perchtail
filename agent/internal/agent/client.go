package agent

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"runtime"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	initialBackoff = time.Second
	maxBackoff     = 30 * time.Second
	// A connection that stayed up this long was a good one (#333): the next
	// reconnect starts again from initialBackoff.
	stableConnection = time.Minute
)

// Keepalive (#333). The agent pings the server every pingInterval and
// gives up on a connection that has been silent (no message, no pong) for
// pongWait, so a half-open connection -- a NAT or firewall that dropped it
// without a word -- is noticed and replaced instead of waiting forever.
// Variables so tests can shorten them.
var (
	pingInterval = 25 * time.Second
	pongWait     = 75 * time.Second
	writeWait    = 10 * time.Second
)

// Run holds the agent's persistent connection open, reconnecting with
// exponential backoff and jitter whenever it drops, until ctx is
// cancelled. Per ROADMAP.md's Phase 2 notes, the agent only ever dials out
// and waits -- the backend drives every list/fetch command live down this
// connection, and nothing is proactively synced ahead of time. It returns
// ErrRestart once an upgrade from the portal was installed and the agent
// must exit for its service manager to start the new binary (Windows).
func Run(ctx context.Context, cfg Config) error {
	return run(ctx, cfg, newUpgrader(cfg), rand.Float64)
}

func run(ctx context.Context, cfg Config, upgrades *upgrader, random func() float64) error {
	backoff := initialBackoff
	for {
		connected, err := connectAndServe(ctx, cfg, upgrades)
		if upgrades.restartRequested() {
			return ErrRestart
		}
		if ctx.Err() != nil {
			return nil
		}
		backoff = backoffAfter(backoff, connected)
		delay := reconnectDelay(backoff, random)
		log.Printf("agent: connection lost (%v), reconnecting in %s", err, delay.Round(time.Millisecond))
		select {
		case <-ctx.Done():
			return nil
		case <-upgrades.restartCh:
			return ErrRestart
		case <-time.After(delay):
		}
		backoff = nextBackoff(backoff)
	}
}

// backoffAfter starts the backoff over after a connection that stayed up
// (#333): one drop after hours connected isn't a server that keeps failing.
func backoffAfter(backoff, connected time.Duration) time.Duration {
	if connected >= stableConnection {
		return initialBackoff
	}
	return backoff
}

func nextBackoff(backoff time.Duration) time.Duration {
	backoff *= 2
	if backoff > maxBackoff {
		return maxBackoff
	}
	return backoff
}

// reconnectDelay spreads reconnects out (#333): a random delay between half
// and one and a half times the backoff, kept within initialBackoff and
// maxBackoff, so agents that lost the server at the same moment (a
// restart) don't all come back at the same instant. random returns a value
// in [0, 1).
func reconnectDelay(backoff time.Duration, random func() float64) time.Duration {
	low := backoff / 2
	if low < initialBackoff {
		low = initialBackoff
	}
	high := backoff + backoff/2
	if high > maxBackoff {
		high = maxBackoff
	}
	if high <= low {
		return low
	}
	return low + time.Duration(random()*float64(high-low))
}

// connectAndServe holds one connection and returns how long it was up (0
// if it never connected) and why it ended.
func connectAndServe(ctx context.Context, cfg Config, upgrades *upgrader) (time.Duration, error) {
	header := http.Header{}
	header.Set("Authorization", "Bearer "+cfg.Token)
	// The backend matches rules case-insensitively for Windows agents, as
	// NTFS does (#189); it can only know which OS this is if told.
	header.Set(OSHeader, runtime.GOOS)
	header.Set(VersionHeader, Version)

	conn, _, err := websocket.DefaultDialer.DialContext(ctx, cfg.ServerURL, header)
	if err != nil {
		return 0, fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()
	connectedAt := time.Now()
	log.Printf("agent: connected to %s", cfg.ServerURL)

	// A silent server is a dead one (#333): every message and every pong
	// pushes the read deadline back; when it passes, ReadJSON fails and
	// Run reconnects. Pings are WebSocket control frames, which any server
	// answers, older PerchTail servers included.
	waitFor, every := pongWait, pingInterval
	_ = conn.SetReadDeadline(time.Now().Add(waitFor))
	conn.SetPongHandler(func(string) error {
		return conn.SetReadDeadline(time.Now().Add(waitFor))
	})

	// ReadJSON blocks until the backend sends something, so a stop signal
	// (ctx cancelled) wouldn't be noticed until then and `systemctl stop`
	// waited out its timeout (#205). Closing the connection unblocks it.
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(every)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				_ = conn.WriteControl(websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, "agent stopping"),
					time.Now().Add(time.Second))
				_ = conn.Close()
				return
			case <-ticker.C:
				// WriteControl may be called alongside the other writes.
				if err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(writeWait)); err != nil {
					_ = conn.Close()
					return
				}
			case <-done:
				return
			}
		}
	}()

	// Each command runs on its own goroutine, so a long streamed fetch
	// doesn't hold up a browse or another open on the same agent; writes
	// are serialized, since a websocket.Conn allows one writer at a time.
	// The backend sends "cancel" for a fetch it gave up on (timeout, size
	// limit), so the file isn't read to the end for nothing.
	connCtx, cancelAll := context.WithCancel(ctx)
	defer cancelAll()
	var writeMu sync.Mutex
	send := func(msg any) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		return conn.WriteJSON(msg)
	}
	// After an upgrade is installed on Windows: a clean close for the
	// server, which also ends the read loop below.
	closeConn := func() {
		_ = conn.WriteControl(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, "agent restarting after an upgrade"),
			time.Now().Add(time.Second))
		_ = conn.Close()
	}
	running := newInflight()
	slots := make(chan struct{}, maxParallelCommands)

	for {
		if ctx.Err() != nil {
			return time.Since(connectedAt), nil
		}
		var cmd Command
		if err := conn.ReadJSON(&cmd); err != nil {
			if ctx.Err() != nil {
				return time.Since(connectedAt), nil
			}
			return time.Since(connectedAt), fmt.Errorf("read: %w", err)
		}
		_ = conn.SetReadDeadline(time.Now().Add(waitFor))
		switch cmd.Type {
		case "cancel":
			running.cancel(cmd.ID)
			continue
		case "ping":
			// The server's own check that this connection is alive (#333).
			go func(id string) { _ = send(Pong{Type: "pong", ID: id}) }(cmd.ID)
			continue
		case "upgrade":
			// Not a file command: it takes no slot, and it goes on after
			// its first answer (#319).
			go upgrades.handle(connCtx, cmd, send, closeConn)
			continue
		}
		cmdCtx := running.start(connCtx, cmd.ID)
		go func(cmd Command) {
			defer running.finish(cmd.ID)
			slots <- struct{}{}
			defer func() { <-slots }()
			if err := serveCommand(cmdCtx, cfg, cmd, send); err != nil {
				// The connection is broken: closing it ends the read loop,
				// and Run reconnects.
				log.Printf("agent: write failed (%v), dropping the connection", err)
				_ = conn.Close()
			}
		}(cmd)
	}
}

// maxParallelCommands bounds how many commands read files at once.
const maxParallelCommands = 8

// errCancelled stops a streamed fetch the backend cancelled.
var errCancelled = errors.New("cancelled by the backend")

// inflight tracks the running commands by id, so "cancel" can stop one.
type inflight struct {
	mu      sync.Mutex
	cancels map[string]context.CancelFunc
}

func newInflight() *inflight { return &inflight{cancels: map[string]context.CancelFunc{}} }

func (r *inflight) start(parent context.Context, id string) context.Context {
	ctx, cancel := context.WithCancel(parent)
	r.mu.Lock()
	r.cancels[id] = cancel
	r.mu.Unlock()
	return ctx
}

func (r *inflight) cancel(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if cancel, ok := r.cancels[id]; ok {
		cancel()
	}
}

func (r *inflight) finish(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if cancel, ok := r.cancels[id]; ok {
		cancel()
		delete(r.cancels, id)
	}
}

// serveCommand answers one command through send, which writes a message
// to the connection. A fetch the backend asked to stream goes out as
// fetch_chunk messages and a closing FetchEnd (#205); everything else is a
// single message from handleCommand. Only an error from send, meaning the
// connection is broken, is returned; a failed command is answered with an
// ErrorResult instead. When ctx is cancelled (the backend sent "cancel"),
// a streamed fetch stops at the next chunk and sends nothing more.
func serveCommand(ctx context.Context, cfg Config, cmd Command, send func(any) error) error {
	if cmd.Type != "fetch" || cmd.ChunkSize <= 0 {
		return send(handleCommand(cfg, cmd))
	}

	var sendErr error
	seq := 0
	chunks, size, err := StreamFetch(cfg.BasePath, cmd.Path, cmd.ChunkSize, cmd.MaxBytes,
		func(data []byte) error {
			if ctx.Err() != nil {
				return errCancelled
			}
			sendErr = send(FetchChunk{
				Type:    "fetch_chunk",
				ID:      cmd.ID,
				Seq:     seq,
				DataB64: base64.StdEncoding.EncodeToString(data),
			})
			seq++
			return sendErr
		})
	if sendErr != nil {
		return sendErr
	}
	if errors.Is(err, errCancelled) {
		return nil
	}
	if err != nil {
		result := ErrorResult{Type: "fetch_error", ID: cmd.ID, Error: err.Error()}
		var tooLarge *TooLargeError
		if errors.As(err, &tooLarge) {
			result.Code, result.Size = "too_large", tooLarge.Size
		}
		return send(result)
	}
	return send(FetchEnd{Type: "fetch_result", ID: cmd.ID, Streamed: true, Chunks: chunks, Size: size})
}

// handleCommand dispatches a single command to the matching handler and
// shapes its result (or error) into the wire response. Kept separate from
// connectAndServe's I/O loop so it's directly unit-testable without a real
// (or even fake) WebSocket connection.
func handleCommand(cfg Config, cmd Command) any {
	switch cmd.Type {
	case "list":
		entries, truncated, err := ListDirectory(cfg.BasePath, cmd.Path, cmd.MaxEntries)
		if err != nil {
			return ErrorResult{Type: "list_error", ID: cmd.ID, Error: err.Error()}
		}
		return ListResult{Type: "list_result", ID: cmd.ID, Entries: entries, Truncated: truncated}
	case "fetch":
		content, err := HandleFetch(cfg.BasePath, cmd.Path)
		if err != nil {
			return ErrorResult{Type: "fetch_error", ID: cmd.ID, Error: err.Error()}
		}
		return FetchResult{
			Type:       "fetch_result",
			ID:         cmd.ID,
			ContentB64: base64.StdEncoding.EncodeToString(content),
		}
	case "read_range":
		size, head, data, err := HandleReadRange(cfg.BasePath, cmd.Path, cmd.Offset, cmd.Length, cmd.HeadLength)
		if err != nil {
			return ErrorResult{Type: "read_range_error", ID: cmd.ID, Error: err.Error()}
		}
		return ReadRangeResult{
			Type:    "read_range_result",
			ID:      cmd.ID,
			Size:    size,
			HeadB64: base64.StdEncoding.EncodeToString(head),
			DataB64: base64.StdEncoding.EncodeToString(data),
		}
	default:
		return ErrorResult{
			Type:  "unknown_error",
			ID:    cmd.ID,
			Error: fmt.Sprintf("unknown command type %q", cmd.Type),
		}
	}
}
