package agent

import (
	"context"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestHandleCommandList(t *testing.T) {
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app.log"), []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := Config{BasePath: base}

	result := handleCommand(cfg, Command{Type: "list", ID: "req-1", Path: ""})
	listResult, ok := result.(ListResult)
	if !ok {
		t.Fatalf("got %T, want ListResult", result)
	}
	if listResult.Type != "list_result" || listResult.ID != "req-1" {
		t.Errorf("listResult = %+v", listResult)
	}
	if len(listResult.Entries) != 1 || listResult.Entries[0].Name != "app.log" {
		t.Errorf("entries = %+v", listResult.Entries)
	}
}

func TestHandleCommandListErrorOnUnsafePath(t *testing.T) {
	cfg := Config{BasePath: t.TempDir()}
	result := handleCommand(cfg, Command{Type: "list", ID: "req-2", Path: "../../etc"})
	errResult, ok := result.(ErrorResult)
	if !ok {
		t.Fatalf("got %T, want ErrorResult", result)
	}
	if errResult.Type != "list_error" || errResult.ID != "req-2" {
		t.Errorf("errResult = %+v", errResult)
	}
}

func TestHandleCommandFetch(t *testing.T) {
	base := t.TempDir()
	content := []byte("hello from the agent")
	if err := os.WriteFile(filepath.Join(base, "app.log"), content, 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := Config{BasePath: base}

	result := handleCommand(cfg, Command{Type: "fetch", ID: "req-3", Path: "app.log"})
	fetchResult, ok := result.(FetchResult)
	if !ok {
		t.Fatalf("got %T, want FetchResult", result)
	}
	if fetchResult.Type != "fetch_result" || fetchResult.ID != "req-3" {
		t.Errorf("fetchResult = %+v", fetchResult)
	}
	decoded, err := base64.StdEncoding.DecodeString(fetchResult.ContentB64)
	if err != nil {
		t.Fatalf("failed to decode content_b64: %v", err)
	}
	if string(decoded) != string(content) {
		t.Errorf("decoded = %q, want %q", decoded, content)
	}
}

func TestHandleCommandFetchErrorOnMissingFile(t *testing.T) {
	cfg := Config{BasePath: t.TempDir()}
	result := handleCommand(cfg, Command{Type: "fetch", ID: "req-4", Path: "missing.log"})
	errResult, ok := result.(ErrorResult)
	if !ok {
		t.Fatalf("got %T, want ErrorResult", result)
	}
	if errResult.Type != "fetch_error" || errResult.ID != "req-4" {
		t.Errorf("errResult = %+v", errResult)
	}
}

func TestHandleCommandUnknownType(t *testing.T) {
	cfg := Config{BasePath: t.TempDir()}
	result := handleCommand(cfg, Command{Type: "bogus", ID: "req-5"})
	errResult, ok := result.(ErrorResult)
	if !ok {
		t.Fatalf("got %T, want ErrorResult", result)
	}
	if !strings.HasSuffix(errResult.Type, "_error") {
		t.Errorf("errResult.Type = %q, must end in _error so resolve_response treats it as a failure", errResult.Type)
	}
}

func TestConnectSendsOSHeader(t *testing.T) {
	got := make(chan string, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- r.Header.Get(OSHeader)
		conn, err := upgrader.Upgrade(w, r, nil)
		if err == nil {
			conn.Close()
		}
	}))
	defer server.Close()

	cfg := Config{ServerURL: "ws" + strings.TrimPrefix(server.URL, "http"), Token: "t"}
	_ = connectAndServe(context.Background(), cfg)

	if reported := <-got; reported != runtime.GOOS {
		t.Fatalf("OS header = %q, want %q", reported, runtime.GOOS)
	}
}

func TestRunStopsPromptlyWhileConnected(t *testing.T) {
	// #205: a connected agent blocked in ReadJSON ignored the stop signal
	// until the backend sent a message.
	upgrader := websocket.Upgrader{}
	connected := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		// One full round trip first, so the agent is surely back in
		// ReadJSON, waiting, when the context is cancelled.
		if err := conn.WriteJSON(Command{Type: "noop", ID: "1"}); err != nil {
			return
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
		close(connected)
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cfg := Config{ServerURL: "ws" + strings.TrimPrefix(server.URL, "http"), Token: "t"}
	result := make(chan error, 1)
	go func() { result <- Run(ctx, cfg) }()

	select {
	case <-connected:
	case <-time.After(5 * time.Second):
		t.Fatal("agent never connected")
	}
	cancel()
	select {
	case err := <-result:
		if err != nil {
			t.Fatalf("Run returned %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run didn't return after the context was cancelled")
	}
}
