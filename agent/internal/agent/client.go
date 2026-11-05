package agent

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"net/http"
	"runtime"
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
		if err := serveCommand(cfg, cmd, func(msg any) error { return conn.WriteJSON(msg) }); err != nil {
			return fmt.Errorf("write: %w", err)
		}
	}
}

// serveCommand answers one command through send, which writes a message
// to the connection. A fetch the backend asked to stream goes out as
// fetch_chunk messages and a closing FetchEnd (#205); everything else is a
// single message from handleCommand. Only an error from send, meaning the
// connection is broken, is returned; a failed command is answered with an
// ErrorResult instead.
func serveCommand(cfg Config, cmd Command, send func(any) error) error {
	if cmd.Type != "fetch" || cmd.ChunkSize <= 0 {
		return send(handleCommand(cfg, cmd))
	}

	var sendErr error
	seq := 0
	chunks, size, err := StreamFetch(cfg.BasePath, cmd.Path, cmd.ChunkSize, cmd.MaxBytes,
		func(data []byte) error {
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
