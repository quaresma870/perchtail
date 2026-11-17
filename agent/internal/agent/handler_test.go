package agent

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
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

	got, err := HandleFetch(base, "app.log", "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(got) != string(content) {
		t.Errorf("got %q, want %q", got, content)
	}
}

func TestHandleFetchRejectsUnsafePath(t *testing.T) {
	base := t.TempDir()
	if _, err := HandleFetch(base, "/etc/passwd", ""); err == nil {
		t.Fatal("expected an error for an absolute path")
	}
}

func TestHandleFetchRefusesSymlinkOutOfBase(t *testing.T) {
	outside := t.TempDir()
	secret := filepath.Join(outside, "secret.txt")
	if err := os.WriteFile(secret, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	base := t.TempDir()
	if err := os.Symlink(secret, filepath.Join(base, "link.log")); err != nil {
		t.Fatal(err)
	}

	content, err := HandleFetch(base, "link.log", "")
	var link *LinkTargetError
	if !errors.As(err, &link) || !link.Outside {
		t.Fatalf("expected a link_target error (outside), got %q, %v", content, err)
	}
	// Whatever the server says it expects.
	if content, err := HandleFetch(base, "link.log", "link.log"); err == nil {
		t.Fatalf("followed a symlink out of base: %q", content)
	}
}

func TestHandleListRefusesSymlinkedDirOutOfBase(t *testing.T) {
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret.txt"), []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	base := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(base, "escape")); err != nil {
		t.Fatal(err)
	}

	if entries, err := HandleList(base, "escape"); err == nil {
		t.Fatalf("expected an error listing a symlinked dir out of base, got %+v", entries)
	}
}

func TestHandleFetchFollowsSymlinkInsideBaseOnlyToTheTargetExpected(t *testing.T) {
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "real.log"), []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("real.log", filepath.Join(base, "current.log")); err != nil {
		t.Fatal(err)
	}

	// Asked for as itself: refused with where it leads, nothing read.
	content, err := HandleFetch(base, "current.log", "")
	var link *LinkTargetError
	if !errors.As(err, &link) || link.Outside || link.Real != "real.log" || content != nil {
		t.Fatalf("content=%q err=%v", content, err)
	}
	// Asked for again with the target the server checked: read.
	content, err = HandleFetch(base, "current.log", "real.log")
	if err != nil || string(content) != "ok" {
		t.Fatalf("content=%q err=%v", content, err)
	}
	// Another target than where it leads: refused.
	if _, err := HandleFetch(base, "current.log", "other.log"); !errors.As(err, &link) {
		t.Fatalf("err=%v", err)
	}
}

func TestHandleFetchRefusesAFileUnderALinkedFolder(t *testing.T) {
	base := t.TempDir()
	if err := os.MkdirAll(filepath.Join(base, "secret"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "secret", "key.log"), []byte("k"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("secret", filepath.Join(base, "logs")); err != nil {
		t.Fatal(err)
	}

	_, err := HandleFetch(base, "logs/key.log", "")
	var link *LinkTargetError
	if !errors.As(err, &link) || link.Real != "secret/key.log" {
		t.Fatalf("err=%v", err)
	}
}

func TestListDirectorySaysWhereLinksLead(t *testing.T) {
	base := t.TempDir()
	for _, dir := range []string{"archive/2026", "logs"} {
		if err := os.MkdirAll(filepath.Join(base, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []string{"archive/2026/x.log", "app-1.log"} {
		if err := os.WriteFile(filepath.Join(base, file), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("app-1.log", filepath.Join(base, "current.log")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("../archive/2026", filepath.Join(base, "logs", "year")); err != nil {
		t.Fatal(err)
	}

	listing, err := ListDirectory(base, "", 0)
	if err != nil || listing.RealDir != nil {
		t.Fatalf("listing %+v, err %v", listing, err)
	}
	targets := map[string]string{}
	for _, e := range listing.Entries {
		if e.Target != nil {
			targets[e.Name] = *e.Target
		}
	}
	if len(targets) != 1 || targets["current.log"] != "app-1.log" {
		t.Fatalf("targets = %v", targets)
	}

	// A folder reached through a link: where it is, and each entry's too.
	listing, err = ListDirectory(base, "logs/year", 0)
	if err != nil || listing.RealDir == nil || *listing.RealDir != "archive/2026" {
		t.Fatalf("listing %+v, err %v", listing, err)
	}
	if len(listing.Entries) != 1 || listing.Entries[0].Target == nil || *listing.Entries[0].Target != "archive/2026/x.log" {
		t.Fatalf("entries = %+v", listing.Entries)
	}
	encoded, _ := json.Marshal(handleCommand(Config{BasePath: base}, Command{Type: "list", ID: "l", Path: "logs/year"}))
	if !strings.Contains(string(encoded), `"real_dir":"archive/2026"`) || !strings.Contains(string(encoded), `"target":"archive/2026/x.log"`) {
		t.Fatalf("wire = %s", encoded)
	}
	// A folder where its path says: no target on the wire.
	encoded, _ = json.Marshal(handleCommand(Config{BasePath: base}, Command{Type: "list", ID: "m", Path: "archive"}))
	if strings.Contains(string(encoded), "target") || strings.Contains(string(encoded), "real_dir") {
		t.Fatalf("wire = %s", encoded)
	}
}

func TestHandleCommandAnswersALinkWithItsTarget(t *testing.T) {
	base := t.TempDir()
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app-1.log"), []byte("line\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outside, "secret.log"), []byte("s"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("app-1.log", filepath.Join(base, "current.log")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "secret.log"), filepath.Join(base, "escape.log")); err != nil {
		t.Fatal(err)
	}
	cfg := Config{BasePath: base}

	for _, cmd := range []Command{
		{Type: "fetch", ID: "a", Path: "current.log"},
		{Type: "read_range", ID: "b", Path: "current.log", Length: 10, HeadLength: 4},
	} {
		result, ok := handleCommand(cfg, cmd).(ErrorResult)
		if !ok || result.Code != "link_target" || result.Real == nil || *result.Real != "app-1.log" || result.Outside {
			t.Fatalf("%s: %+v", cmd.Type, result)
		}
		encoded, _ := json.Marshal(result)
		if strings.Contains(string(encoded), "line") {
			t.Fatalf("%s sent content: %s", cmd.Type, encoded)
		}
	}
	result, ok := handleCommand(cfg, Command{Type: "fetch", ID: "c", Path: "escape.log"}).(ErrorResult)
	if !ok || result.Code != "link_target" || !result.Outside || result.Real != nil {
		t.Fatalf("outside: %+v", result)
	}
	got, ok := handleCommand(cfg, Command{Type: "read_range", ID: "d", Path: "current.log", ExpectReal: "app-1.log", Length: 10}).(ReadRangeResult)
	if !ok || got.DataB64 == "" {
		t.Fatalf("expected the content once the target is expected: %+v", got)
	}
}

func TestHandleListLeavesOutLinksThatLeaveTheBase(t *testing.T) {
	// #206: `escape -> /etc` was listed, though opening it is refused.
	outside := t.TempDir()
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app.log"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(base, "logs"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(base, "escape")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if err := os.Symlink("logs", filepath.Join(base, "inside")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("missing", filepath.Join(base, "broken")); err != nil {
		t.Fatal(err)
	}

	entries, err := HandleList(base, "")
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, e := range entries {
		got[e.Name] = e.IsDir
	}
	if _, ok := got["escape"]; ok {
		t.Error("escape (link out of the base) was listed")
	}
	if _, ok := got["broken"]; ok {
		t.Error("broken link was listed")
	}
	if isDir, ok := got["inside"]; !ok || !isDir {
		t.Errorf("inside (link within the base) should be listed as a directory, got %v %v", isDir, ok)
	}
	if _, ok := got["app.log"]; !ok {
		t.Error("app.log missing")
	}
}

func TestHandleListOfAFileIsNotADirectory(t *testing.T) {
	base := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, "app.log"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := HandleList(base, "app.log"); !errors.Is(err, ErrNotADirectory) {
		t.Fatalf("got %v, want ErrNotADirectory", err)
	}
}

func TestHandleListReportsModificationTimes(t *testing.T) {
	// #273: the explorer sorts by time.
	base := t.TempDir()
	file := filepath.Join(base, "app.log")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	when := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	if err := os.Chtimes(file, when, when); err != nil {
		t.Fatal(err)
	}
	entries, err := HandleList(base, "")
	if err != nil || len(entries) != 1 {
		t.Fatalf("entries %+v, err %v", entries, err)
	}
	if entries[0].MTimeMs != when.UnixMilli() {
		t.Fatalf("mtime = %d, want %d", entries[0].MTimeMs, when.UnixMilli())
	}
}

func writeFiles(t *testing.T, dir string, count int) {
	t.Helper()
	for i := range count {
		name := filepath.Join(dir, "file-"+strconv.Itoa(i)+".log")
		if err := os.WriteFile(name, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestListDirectoryStopsAtTheEntryCap(t *testing.T) {
	base := t.TempDir()
	writeFiles(t, base, 25)

	listing, err := ListDirectory(base, "", 10)
	if err != nil || !listing.Truncated || len(listing.Entries) != 10 {
		t.Fatalf("entries %d, truncated %v, err %v", len(listing.Entries), listing.Truncated, err)
	}
	listing, err = ListDirectory(base, "", 25)
	if err != nil || listing.Truncated || len(listing.Entries) != 25 {
		t.Fatalf("entries %d, truncated %v, err %v", len(listing.Entries), listing.Truncated, err)
	}
}

func TestListDirectoryNeverAsksForMoreThanItsOwnCap(t *testing.T) {
	base := t.TempDir()
	writeFiles(t, base, 3)
	listing, err := ListDirectory(base, "", MaxListEntries*10)
	if err != nil || listing.Truncated || len(listing.Entries) != 3 {
		t.Fatalf("entries %d, truncated %v, err %v", len(listing.Entries), listing.Truncated, err)
	}
}

func TestListDirectoryStopsBeforeTheMessageGetsTooBig(t *testing.T) {
	base := t.TempDir()
	writeFiles(t, base, 50)
	saved := maxListBytes
	maxListBytes = 1000
	defer func() { maxListBytes = saved }()

	listing, err := ListDirectory(base, "", 0)
	entries := listing.Entries
	if err != nil || !listing.Truncated || len(entries) == 0 || len(entries) >= 50 {
		t.Fatalf("entries %d, truncated %v, err %v", len(entries), listing.Truncated, err)
	}
	encoded, _ := json.Marshal(entries)
	if len(encoded) > maxListBytes {
		t.Fatalf("%d bytes, over %d", len(encoded), maxListBytes)
	}
}

func TestHandleCommandListPassesTheCapAndSaysWhenItCut(t *testing.T) {
	base := t.TempDir()
	writeFiles(t, base, 5)
	result := handleCommand(Config{BasePath: base}, Command{Type: "list", ID: "l", MaxEntries: 2})
	list, ok := result.(ListResult)
	if !ok || len(list.Entries) != 2 || !list.Truncated {
		t.Fatalf("result = %+v", result)
	}
	encoded, _ := json.Marshal(list)
	if !strings.Contains(string(encoded), `"truncated":true`) {
		t.Fatalf("wire = %s", encoded)
	}
	whole, _ := handleCommand(Config{BasePath: base}, Command{Type: "list", ID: "m"}).(ListResult)
	encoded, _ = json.Marshal(whole)
	if whole.Truncated || strings.Contains(string(encoded), "truncated") {
		t.Fatalf("wire = %s", encoded)
	}
}
