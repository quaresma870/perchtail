package agent

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// FeatureLinks: a command that reads content through a link (the path
// itself or a folder above it) is refused with where the link leads, before
// anything of the content is sent, until the server asks for that target.

func linkedBase(t *testing.T) string {
	t.Helper()
	base := t.TempDir()
	if err := os.MkdirAll(filepath.Join(base, "real"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(base, "real"), "app-1.log", []byte(secretLog))
	writeFile(t, filepath.Join(base, "real"), "logs.zip", zipOf(t, []zipMember{{name: "a.log", content: secretLog}}))
	if err := os.Symlink("real", filepath.Join(base, "linked")); err != nil {
		t.Skip("symlinks not available:", err)
	}
	if err := os.Symlink(filepath.Join("real", "app-1.log"), filepath.Join(base, "current.log")); err != nil {
		t.Fatal(err)
	}
	return base
}

func linkCommands(t *testing.T) []Command {
	t.Helper()
	block := redactJSON(t, testRules, "")
	return []Command{
		{Type: "fetch", Path: "current.log"},
		{Type: "fetch", Path: "current.log", ChunkSize: minChunkSize},
		{Type: "fetch", Path: "linked/app-1.log", ChunkSize: minChunkSize, Redact: block},
		{Type: "fetch", Path: "linked/logs.zip", ChunkSize: minChunkSize, Member: "a.log", Redact: block},
		{Type: "fetch_archive", Path: "linked/logs.zip", ChunkSize: minChunkSize, Members: []string{"a.log"}, Redact: block},
		{Type: "read_range", Path: "current.log", Length: 100, HeadLength: 10},
		{Type: "read_range", Path: "linked/app-1.log", Length: 100, Redact: block},
		{Type: "list_archive", Path: "linked/logs.zip"},
	}
}

func realOf(cmd Command) string {
	if cmd.Path == "current.log" {
		return "real/app-1.log"
	}
	return "real/" + strings.TrimPrefix(cmd.Path, "linked/")
}

func TestEveryContentCommandRefusesALinkBeforeSendingAnything(t *testing.T) {
	base := linkedBase(t)
	for i, cmd := range linkCommands(t) {
		cmd.ID = "c"
		sent := collect(t, base, cmd)
		if len(sent) != 1 {
			t.Fatalf("%d %s %s: sent %d messages, want only the refusal: %+v", i, cmd.Type, cmd.Path, len(sent), sent)
		}
		result, ok := sent[0].(ErrorResult)
		if !ok || result.Code != "link_target" || result.Outside || result.Real == nil || *result.Real != realOf(cmd) {
			t.Fatalf("%d %s %s: answer = %+v", i, cmd.Type, cmd.Path, sent[0])
		}
		encoded, _ := json.Marshal(sent)
		assertNoSecret(t, cmd.Type, encoded)
	}
}

func TestEveryContentCommandReadsOnceTheTargetIsExpected(t *testing.T) {
	base := linkedBase(t)
	for i, cmd := range linkCommands(t) {
		cmd.ID, cmd.ExpectReal = "c", realOf(cmd)
		sent := collect(t, base, cmd)
		if result, ok := sent[len(sent)-1].(ErrorResult); ok {
			t.Fatalf("%d %s %s: refused: %+v", i, cmd.Type, cmd.Path, result)
		}
	}
}

func TestAContentCommandRefusesALinkOutOfTheBase(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	writeFile(t, outside, "secret.log", []byte(secretLog))
	if err := os.Symlink(filepath.Join(outside, "secret.log"), filepath.Join(base, "escape.log")); err != nil {
		t.Skip("symlinks not available:", err)
	}
	for _, expect := range []string{"", "escape.log", "secret.log"} {
		sent := collect(t, base, Command{Type: "fetch", ID: "c", Path: "escape.log", ChunkSize: minChunkSize, ExpectReal: expect})
		result, ok := sent[0].(ErrorResult)
		if len(sent) != 1 || !ok || result.Code != "link_target" || !result.Outside || result.Real != nil {
			t.Fatalf("expect %q: %+v", expect, sent)
		}
	}
}

func TestAPathInAnotherCaseIsNotTakenForALinkOnlyWhereCaseIsIgnored(t *testing.T) {
	if !pathsIgnoreCase {
		if samePath("App.log", "app.log") {
			t.Fatal("paths compared ignoring case on a case-sensitive system")
		}
		return
	}
	if !samePath("App.log", "app.log") {
		t.Fatal("paths compared with case on Windows")
	}
}

func TestAnUnsafeExpectedTargetIsRefused(t *testing.T) {
	base := linkedBase(t)
	sent := collect(t, base, Command{Type: "fetch", ID: "c", Path: "current.log", ChunkSize: minChunkSize, ExpectReal: "../real/app-1.log"})
	if result, ok := sent[0].(ErrorResult); len(sent) != 1 || !ok || result.Code == "link_target" {
		t.Fatalf("answer = %+v", sent)
	}
}

// resolveSame, where finalPath isn't available: a name out of the base is
// only reported as such, never looked at; one inside must be the opened file.
func TestResolveSameLooksOnlyAtNamesInsideTheBase(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "base")
	outside := filepath.Join(root, "outside.log")
	inside := filepath.Join(base, "inside.log")
	if err := os.MkdirAll(base, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{outside, inside, filepath.Join(base, "other.log")} {
		if err := os.WriteFile(name, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(outside, filepath.Join(base, "out.log")); err != nil {
		t.Skip("symlinks not available:", err)
	}
	resolvedBase, err := filepath.EvalSymlinks(base)
	if err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(inside)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	got, err := resolveSame(f, inside, resolvedBase)
	if err != nil || got != filepath.Join(resolvedBase, "inside.log") {
		t.Fatalf("inside: got %q, %v", got, err)
	}
	got, err = resolveSame(f, filepath.Join(base, "out.log"), resolvedBase)
	if err != nil || strings.HasPrefix(got, resolvedBase+string(filepath.Separator)) {
		t.Fatalf("out of the base: got %q, %v", got, err)
	}
	if _, err := resolveSame(f, filepath.Join(base, "other.log"), resolvedBase); err == nil {
		t.Fatal("another file inside the base was taken for the opened one")
	}
}
