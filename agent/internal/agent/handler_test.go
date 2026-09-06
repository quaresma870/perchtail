package agent

import (
	"os"
	"path/filepath"
	"testing"
)

func TestHandleListReturnsFilesAndDirs(t *testing.T) {
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app.log"), []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(base, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}

	entries, err := HandleList(base, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	byName := map[string]DirEntryWire{}
	for _, e := range entries {
		byName[e.Name] = e
	}
	if byName["app.log"].IsDir || byName["app.log"].Size != 5 {
		t.Errorf("app.log entry = %+v", byName["app.log"])
	}
	if !byName["nested"].IsDir {
		t.Errorf("nested entry = %+v", byName["nested"])
	}
}

func TestHandleListRejectsUnsafePath(t *testing.T) {
	base := t.TempDir()
	if _, err := HandleList(base, "../../etc"); err == nil {
		t.Fatal("expected an error for a path-traversal attempt")
	}
}

func TestHandleListDescendsIntoNestedDir(t *testing.T) {
	base := t.TempDir()
	if err := os.Mkdir(filepath.Join(base, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "nested", "debug.log"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	entries, err := HandleList(base, "nested")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(entries) != 1 || entries[0].Name != "debug.log" {
		t.Errorf("entries = %+v", entries)
	}
}

func TestHandleFetchReturnsFileContent(t *testing.T) {
	base := t.TempDir()
	content := []byte("hello from the agent")
	if err := os.WriteFile(filepath.Join(base, "app.log"), content, 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := HandleFetch(base, "app.log")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(got) != string(content) {
		t.Errorf("got %q, want %q", got, content)
	}
}

func TestHandleFetchRejectsUnsafePath(t *testing.T) {
	base := t.TempDir()
	if _, err := HandleFetch(base, "/etc/passwd"); err == nil {
		t.Fatal("expected an error for an absolute path")
	}
}
