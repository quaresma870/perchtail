package agent

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// #205: a fetch the backend asks to stream goes out in chunks, so the
// agent never holds the whole file.

func writeFile(t *testing.T, dir, name string, content []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), content, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestStreamFetchSendsChunksInOrder(t *testing.T) {
	base := t.TempDir()
	content := bytes.Repeat([]byte("0123456789"), 1000) // 10,000 bytes
	writeFile(t, base, "app.log", content)

	var got [][]byte
	chunks, size, err := StreamFetch(base, "app.log", minChunkSize, 0, func(data []byte) error {
		got = append(got, append([]byte(nil), data...))
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if chunks != 3 || size != int64(len(content)) || len(got) != 3 {
		t.Fatalf("chunks=%d size=%d sent=%d, want 3, %d, 3", chunks, size, len(got), len(content))
	}
	if len(got[0]) != minChunkSize || !bytes.Equal(bytes.Join(got, nil), content) {
		t.Errorf("chunks don't add up to the file")
	}
}

func TestStreamFetchOfAnEmptyFileSendsNothing(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "empty.log", nil)

	chunks, size, err := StreamFetch(base, "empty.log", minChunkSize, 0, func([]byte) error {
		t.Fatal("send called for an empty file")
		return nil
	})
	if err != nil || chunks != 0 || size != 0 {
		t.Errorf("chunks=%d size=%d err=%v", chunks, size, err)
	}
}

func TestStreamFetchRefusesAFileOverTheLimitBeforeReading(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "big.log", make([]byte, 5000))

	_, _, err := StreamFetch(base, "big.log", minChunkSize, 4000, func([]byte) error {
		t.Fatal("send called for a file over the limit")
		return nil
	})
	var tooLarge *TooLargeError
	if !errors.As(err, &tooLarge) || tooLarge.Size != 5000 || tooLarge.Limit != 4000 {
		t.Errorf("err = %v, want TooLargeError{5000, 4000}", err)
	}
}

func TestStreamFetchRefusesSymlinkOutOfBase(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	writeFile(t, outside, "secret.log", []byte("secret"))
	if err := os.Symlink(filepath.Join(outside, "secret.log"), filepath.Join(base, "link.log")); err != nil {
		t.Skip("symlinks not available:", err)
	}
	if _, _, err := StreamFetch(base, "link.log", minChunkSize, 0, func([]byte) error { return nil }); err == nil {
		t.Error("expected an error for a symlink leading out of the base path")
	}
}

func TestStreamFetchReturnsTheSendError(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log", make([]byte, 3*minChunkSize))
	broken := errors.New("connection broken")

	calls := 0
	_, _, err := StreamFetch(base, "app.log", minChunkSize, 0, func([]byte) error {
		calls++
		return broken
	})
	if !errors.Is(err, broken) || calls != 1 {
		t.Errorf("err=%v calls=%d, want the send error after one call", err, calls)
	}
}

func TestServeCommandStreamsWhenAskedTo(t *testing.T) {
	base := t.TempDir()
	content := bytes.Repeat([]byte("x"), minChunkSize+10)
	writeFile(t, base, "app.log", content)

	var sent []any
	err := serveCommand(context.Background(), Config{BasePath: base},
		Command{Type: "fetch", ID: "req-7", Path: "app.log", ChunkSize: minChunkSize},
		func(msg any) error { sent = append(sent, msg); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if len(sent) != 3 {
		t.Fatalf("sent %d messages, want 2 chunks and an end", len(sent))
	}
	var joined []byte
	for i, msg := range sent[:2] {
		chunk, ok := msg.(FetchChunk)
		if !ok || chunk.Type != "fetch_chunk" || chunk.ID != "req-7" || chunk.Seq != i {
			t.Fatalf("message %d = %+v", i, msg)
		}
		data, _ := base64.StdEncoding.DecodeString(chunk.DataB64)
		joined = append(joined, data...)
	}
	end, ok := sent[2].(FetchEnd)
	if !ok || end.Type != "fetch_result" || !end.Streamed || end.Chunks != 2 || end.Size != int64(len(content)) {
		t.Errorf("end = %+v", sent[2])
	}
	if !bytes.Equal(joined, content) {
		t.Error("chunks don't add up to the file")
	}
}

func TestServeCommandReportsTooLargeWithACode(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "big.log", make([]byte, 5000))

	var sent []any
	_ = serveCommand(context.Background(), Config{BasePath: base},
		Command{Type: "fetch", ID: "req-8", Path: "big.log", ChunkSize: minChunkSize, MaxBytes: 4000},
		func(msg any) error { sent = append(sent, msg); return nil })
	if len(sent) != 1 {
		t.Fatalf("sent %d messages, want one error", len(sent))
	}
	result, ok := sent[0].(ErrorResult)
	if !ok || result.Type != "fetch_error" || result.Code != "too_large" || result.Size != 5000 {
		t.Errorf("result = %+v", sent[0])
	}
}

func TestServeCommandWithoutChunkSizeKeepsTheSingleMessage(t *testing.T) {
	// A backend older than 0.5.3 sends no chunk_size and expects one
	// fetch_result with the whole file.
	base := t.TempDir()
	writeFile(t, base, "app.log", []byte("hello"))

	var sent []any
	_ = serveCommand(context.Background(), Config{BasePath: base}, Command{Type: "fetch", ID: "req-9", Path: "app.log"},
		func(msg any) error { sent = append(sent, msg); return nil })
	if len(sent) != 1 {
		t.Fatalf("sent %d messages, want 1", len(sent))
	}
	if result, ok := sent[0].(FetchResult); !ok || result.ContentB64 != base64.StdEncoding.EncodeToString([]byte("hello")) {
		t.Errorf("result = %+v", sent[0])
	}
}

func TestServeCommandSendsNothingOnceCancelled(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log", make([]byte, 3*minChunkSize))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	var sent []any
	err := serveCommand(ctx, Config{BasePath: base},
		Command{Type: "fetch", ID: "req-10", Path: "app.log", ChunkSize: minChunkSize},
		func(msg any) error { sent = append(sent, msg); return nil })
	if err != nil || len(sent) != 0 {
		t.Errorf("err=%v sent=%d, want nothing sent for a cancelled fetch", err, len(sent))
	}
}

// fakeBackend runs a WebSocket server that hands the connection to script,
// and the agent's connectAndServe against it.
func fakeBackend(t *testing.T, base string, script func(conn *websocket.Conn)) {
	t.Helper()
	upgrader := websocket.Upgrader{}
	finished := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		script(conn)
		close(finished)
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := Config{ServerURL: "ws" + strings.TrimPrefix(server.URL, "http"), Token: "t", BasePath: base}
	go func() { _, _ = connectAndServe(ctx, cfg, newUpgrader(cfg)) }()
	select {
	case <-finished:
	case <-time.After(10 * time.Second):
		t.Fatal("fake backend didn't finish")
	}
}

func readType(t *testing.T, conn *websocket.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	var msg map[string]any
	if err := conn.ReadJSON(&msg); err != nil {
		t.Fatalf("read: %v", err)
	}
	return msg
}

func TestAListIsAnsweredWhileALargeFetchStreams(t *testing.T) {
	// Review of #205: commands ran one at a time, so a long fetch held up
	// every browse on the same agent.
	base := t.TempDir()
	writeFile(t, base, "big.log", make([]byte, 1024*minChunkSize))
	fakeBackend(t, base, func(conn *websocket.Conn) {
		_ = conn.WriteJSON(Command{Type: "fetch", ID: "f", Path: "big.log", ChunkSize: minChunkSize})
		first := readType(t, conn) // the stream has started
		if first["type"] != "fetch_chunk" {
			t.Errorf("first message = %v", first["type"])
		}
		_ = conn.WriteJSON(Command{Type: "list", ID: "l", Path: ""})
		for {
			msg := readType(t, conn)
			if msg["type"] == "list_result" {
				return
			}
			if msg["type"] == "fetch_result" {
				t.Error("the list was answered only after the whole fetch")
				return
			}
		}
	})
}

func TestACancelledFetchStopsStreaming(t *testing.T) {
	base := t.TempDir()
	total := 2048
	writeFile(t, base, "big.log", make([]byte, total*minChunkSize))
	fakeBackend(t, base, func(conn *websocket.Conn) {
		_ = conn.WriteJSON(Command{Type: "fetch", ID: "f", Path: "big.log", ChunkSize: minChunkSize})
		readType(t, conn)
		_ = conn.WriteJSON(Command{Type: "cancel", ID: "f"})
		chunks := 1
		for {
			_ = conn.SetReadDeadline(time.Now().Add(time.Second))
			var msg map[string]any
			if err := conn.ReadJSON(&msg); err != nil {
				break // quiet for a second: the stream stopped
			}
			if msg["type"] == "fetch_result" {
				t.Error("a cancelled fetch still ended normally")
				return
			}
			chunks++
		}
		if chunks >= total {
			t.Errorf("sent all %d chunks despite the cancel", chunks)
		}
	})
}

func TestStreamFetchCapsTheChunkSizeTheBackendAsksFor(t *testing.T) {
	// The chunk size comes from the backend's message: a huge value must
	// not make the agent allocate or send more than maxChunkSize at once.
	base := t.TempDir()
	writeFile(t, base, "app.log", make([]byte, 2*maxChunkSize+10))

	var sizes []int
	_, _, err := StreamFetch(base, "app.log", 1<<40, 0, func(data []byte) error {
		sizes = append(sizes, len(data))
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(sizes) != 3 || sizes[0] != maxChunkSize || sizes[2] != 10 {
		t.Errorf("chunk sizes = %v, want [%d %d 10]", sizes, maxChunkSize, maxChunkSize)
	}
}
