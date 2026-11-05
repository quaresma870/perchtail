package agent

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"net/http"
	"runtime"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	initialBackoff = time.Second
	maxBackoff     = 30 * time.Second
)

// Run holds the agent's persistent connection open, reconnecting with
// exponential backoff whenever it drops, until ctx is cancelled. Per
// ROADMAP.md's Phase 2 notes, the agent only ever dials out and waits — the
// backend drives every list/fetch command live down this connection, and
// nothing is proactively synced ahead of time.
func Run(ctx context.Context, cfg Config) error {
	backoff := initialBackoff
	for {
		err := connectAndServe(ctx, cfg)
		if ctx.Err() != nil {
			return nil
		}
		log.Printf("agent: connection lost (%v), reconnecting in %s", err, backoff)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

func connectAndServe(ctx context.Context, cfg Config) error {
	header := http.Header{}
	header.Set("Authorization", "Bearer "+cfg.Token)
	// The backend matches rules case-insensitively for Windows agents, as
	// NTFS does (#189); it can only know which OS this is if told.
	header.Set(OSHeader, runtime.GOOS)

	conn, _, err := websocket.DefaultDialer.DialContext(ctx, cfg.ServerURL, header)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()
	log.Printf("agent: connected to %s", cfg.ServerURL)

	// ReadJSON blocks until the backend sends something, so a stop signal
	// (ctx cancelled) wouldn't be noticed until then and `systemctl stop`
	// waited out its timeout (#205). Closing the connection unblocks it.
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			_ = conn.WriteControl(websocket.CloseMessage,
				websocket.FormatCloseMessage(websocket.CloseNormalClosure, "agent stopping"),
				time.Now().Add(time.Second))
			conn.Close()
		case <-done:
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
	running := newInflight()
	slots := make(chan struct{}, maxParallelCommands)

	for {
		if ctx.Err() != nil {
			return nil
		}
		var cmd Command
		if err := conn.ReadJSON(&cmd); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("read: %w", err)
		}
		if cmd.Type == "cancel" {
			running.cancel(cmd.ID)
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
				conn.Close()
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
		entries, err := HandleList(cfg.BasePath, cmd.Path)
		if err != nil {
			return ErrorResult{Type: "list_error", ID: cmd.ID, Error: err.Error()}
		}
		return ListResult{Type: "list_result", ID: cmd.ID, Entries: entries}
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
	default:
		return ErrorResult{
			Type:  "unknown_error",
			ID:    cmd.ID,
			Error: fmt.Sprintf("unknown command type %q", cmd.Type),
		}
	}
}
