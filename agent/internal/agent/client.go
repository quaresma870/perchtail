package agent

import (
	"context"
	"encoding/base64"
	"fmt"
	"log"
	"net/http"
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

	conn, _, err := websocket.DefaultDialer.DialContext(ctx, cfg.ServerURL, header)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()
	log.Printf("agent: connected to %s", cfg.ServerURL)

	for {
		if ctx.Err() != nil {
			return nil
		}
		var cmd Command
		if err := conn.ReadJSON(&cmd); err != nil {
			return fmt.Errorf("read: %w", err)
		}
		if err := conn.WriteJSON(handleCommand(cfg, cmd)); err != nil {
			return fmt.Errorf("write: %w", err)
		}
	}
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
