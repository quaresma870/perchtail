package agent

import (
	"bytes"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"testing"
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
	err := serveCommand(Config{BasePath: base},
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
	_ = serveCommand(Config{BasePath: base},
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
	_ = serveCommand(Config{BasePath: base}, Command{Type: "fetch", ID: "req-9", Path: "app.log"},
		func(msg any) error { sent = append(sent, msg); return nil })
	if len(sent) != 1 {
		t.Fatalf("sent %d messages, want 1", len(sent))
	}
	if result, ok := sent[0].(FetchResult); !ok || result.ContentB64 != base64.StdEncoding.EncodeToString([]byte("hello")) {
		t.Errorf("result = %+v", sent[0])
	}
}
