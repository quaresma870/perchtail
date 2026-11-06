package agent

import (
	"bytes"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

// Incremental follow (#254).

func writeLog(t *testing.T, content []byte) string {
	t.Helper()
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app.log"), content, 0o644); err != nil {
		t.Fatal(err)
	}
	return base
}

func TestHandleReadRangeReadsFromTheOffset(t *testing.T) {
	base := writeLog(t, []byte("line one\nline two\n"))
	size, data, err := HandleReadRange(base, "app.log", 9, 100)
	if err != nil {
		t.Fatal(err)
	}
	if size != 18 || string(data) != "line two\n" {
		t.Fatalf("got size %d, data %q", size, data)
	}
}

func TestHandleReadRangeAtOrPastTheEndIsEmpty(t *testing.T) {
	base := writeLog(t, []byte("abc"))
	for _, offset := range []int64{3, 10} {
		size, data, err := HandleReadRange(base, "app.log", offset, 100)
		if err != nil || size != 3 || len(data) != 0 {
			t.Fatalf("offset %d: size %d, data %q, err %v", offset, size, data, err)
		}
	}
}

func TestHandleReadRangeCapsTheLength(t *testing.T) {
	base := writeLog(t, bytes.Repeat([]byte("x"), maxRangeLength+10))
	size, data, err := HandleReadRange(base, "app.log", 0, 1<<40)
	if err != nil {
		t.Fatal(err)
	}
	if size != maxRangeLength+10 || len(data) != maxRangeLength {
		t.Fatalf("size %d, read %d", size, len(data))
	}
}

func TestHandleReadRangeRefusesBadInput(t *testing.T) {
	base := writeLog(t, []byte("abc"))
	for _, tc := range []struct {
		path           string
		offset, length int64
	}{
		{"../app.log", 0, 1},
		{"app.log", -1, 1},
		{"app.log", 0, -1},
		{".", 0, 1},
	} {
		if _, _, err := HandleReadRange(base, tc.path, tc.offset, tc.length); err == nil {
			t.Errorf("%+v: expected an error", tc)
		}
	}
}

func TestHandleReadRangeDoesNotFollowALinkOutOfTheBase(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "base")
	if err := os.Mkdir(base, 0o755); err != nil {
		t.Fatal(err)
	}
	secret := filepath.Join(root, "secret.log")
	if err := os.WriteFile(secret, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(base, "link.log")); err != nil {
		t.Skip("symlinks not available:", err)
	}
	if _, data, err := HandleReadRange(base, "link.log", 0, 100); err == nil {
		t.Fatalf("read %q through a link leaving the base", data)
	}
}

func TestHandleCommandAnswersReadRange(t *testing.T) {
	base := writeLog(t, []byte("hello world"))
	reply := handleCommand(Config{BasePath: base}, Command{
		Type: "read_range", ID: "r1", Path: "app.log", Offset: 6, Length: 5,
	})
	result, ok := reply.(ReadRangeResult)
	if !ok {
		t.Fatalf("reply = %#v", reply)
	}
	data, _ := base64.StdEncoding.DecodeString(result.DataB64)
	if result.Type != "read_range_result" || result.ID != "r1" || result.Size != 11 || string(data) != "world" {
		t.Fatalf("result = %+v (%q)", result, data)
	}

	reply = handleCommand(Config{BasePath: base}, Command{Type: "read_range", ID: "r2", Path: "nope.log", Length: 1})
	if failure, ok := reply.(ErrorResult); !ok || failure.Type != "read_range_error" {
		t.Fatalf("missing file: reply = %#v", reply)
	}
}
