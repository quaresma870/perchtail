package agent

// Fuzz tests for path containment (security scanning, level 2). The earlier
// advisories (P1–P3) were all about reaching files outside a source's base,
// so these check that invariant directly, against arbitrary paths from the
// backend: a path the agent accepts never leaves the base, and nothing the
// agent reads or lists comes from outside it, symlinks included.
//
// `go test` runs the seed corpus as ordinary tests; CI also fuzzes each
// target for a short while (see .github/workflows/ci.yml).

import (
	"bytes"
	"os"
	"path"
	"path/filepath"
	"strings"
	"testing"
)

func FuzzIsSafeRelativePath(f *testing.F) {
	for _, seed := range []string{
		"", "app.log", "nested/app.log", "a..b/c", "./a", "a/./b",
		"..", "../x", "a/../../b", "a/../b", "....//x", "/etc/passwd",
		"\\Windows", "C:\\Windows", "C:x", "a\\..\\..\\b", "%2e%2e/x", "\x00", "a//..//..//b",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, p string) {
		if !IsSafeRelativePath(p) {
			return
		}
		// Accepted: with either separator, it must stay under the base.
		cleaned := path.Clean(strings.ReplaceAll(p, "\\", "/"))
		if cleaned == ".." || strings.HasPrefix(cleaned, "../") || strings.HasPrefix(cleaned, "/") {
			t.Fatalf("IsSafeRelativePath(%q) accepted a path that leaves the base (%q)", p, cleaned)
		}
		if strings.Contains(p, ":") {
			t.Fatalf("IsSafeRelativePath(%q) accepted a drive letter or stream name", p)
		}
	})
}

const outsideMarker = "OUTSIDE-THE-BASE-SECRET"

// containmentFixture builds a base with ordinary files, a symlinked folder
// and a symlinked file that both lead outside it, next to a folder holding
// a file whose content must never be returned.
func containmentFixture(f *testing.F) (base, outsideName string) {
	root := f.TempDir()
	base = filepath.Join(root, "base")
	outside := filepath.Join(root, "outside")
	for _, dir := range []string{filepath.Join(base, "nested"), outside} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			f.Fatal(err)
		}
	}
	files := map[string]string{
		filepath.Join(base, "app.log"):         "inside\n",
		filepath.Join(base, "nested", "x.log"): "inside nested\n",
		filepath.Join(outside, "secret.log"):   outsideMarker + "\n",
	}
	for name, content := range files {
		if err := os.WriteFile(name, []byte(content), 0o644); err != nil {
			f.Fatal(err)
		}
	}
	if err := os.Symlink(outside, filepath.Join(base, "escape")); err != nil {
		f.Skip("symlinks not available:", err)
	}
	if err := os.Symlink(filepath.Join(outside, "secret.log"), filepath.Join(base, "link.log")); err != nil {
		f.Skip("symlinks not available:", err)
	}
	return base, "outside"
}

func FuzzFetchStaysInBase(f *testing.F) {
	base, outsideName := containmentFixture(f)
	for _, seed := range []string{
		"", "app.log", "nested/x.log", "nested", "escape", "escape/secret.log", "link.log",
		"nested/../link.log", "nested/../escape/secret.log", "../" + outsideName + "/secret.log",
		"escape/../escape/secret.log", "./link.log", "nested//..//link.log",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, p string) {
		var streamed bytes.Buffer
		_, _, _ = StreamFetch(base, p, minChunkSize, 0, func(data []byte) error {
			streamed.Write(data)
			return nil
		})
		if bytes.Contains(streamed.Bytes(), []byte(outsideMarker)) {
			t.Fatalf("StreamFetch(%q) returned content from outside the base", p)
		}
		if content, err := HandleFetch(base, p); err == nil && bytes.Contains(content, []byte(outsideMarker)) {
			t.Fatalf("HandleFetch(%q) returned content from outside the base", p)
		}
		if entries, err := HandleList(base, p); err == nil {
			for _, entry := range entries {
				if entry.Name == "secret.log" {
					t.Fatalf("HandleList(%q) listed a file from outside the base", p)
				}
			}
		}
	})
}
