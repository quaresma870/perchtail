package agent

import (
	"crypto/rand"
	"strings"
	"testing"
)

// incompressible returns n random bytes, which gzip can't make smaller.
func incompressible(t *testing.T, n int) string {
	t.Helper()
	data := make([]byte, n)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// tooLargeLimit runs cmd and returns the `limit` of the too_large error it
// ends with.
func tooLargeLimit(t *testing.T, base string, cmd Command) string {
	t.Helper()
	sent := collect(t, base, cmd)
	result, ok := sent[len(sent)-1].(ErrorResult)
	if !ok || result.Code != "too_large" {
		t.Fatalf("%s %s: answer = %+v, want a too_large error", cmd.Type, cmd.Path, sent[len(sent)-1])
	}
	return result.Limit
}

// With max_bytes and max_decompressed the same number (the server's
// default), the too_large error says which one was passed, so the server
// doesn't have to guess it from the number in the message (F4).
func TestTooLargeSaysWhichLimitWasPassed(t *testing.T) {
	const limit = 10_000
	base := t.TempDir()
	// Over max_bytes as stored: random content stays as large compressed.
	writeFile(t, base, "stored.log.gz", gzipped(t, incompressible(t, 2*limit)))
	writeFile(t, base, "stored.zip", zipOf(t, []zipMember{{name: "a.log", content: incompressible(t, 2*limit)}}))
	writeFile(t, base, "plain.log", []byte(strings.Repeat("x", 2*limit)))
	// Under max_bytes as stored, over max_decompressed once expanded.
	writeFile(t, base, "expands.log.gz", gzipped(t, strings.Repeat("a", 2*limit)))
	writeFile(t, base, "expands.zip", zipOf(t, []zipMember{{name: "a.log", content: strings.Repeat("a", 2*limit)}}))
	writeFile(t, base, "expands.tgz", tarGzOf(t, []tarMember{{name: "a.log", content: strings.Repeat("a", 2*limit)}}))

	redacted := redactJSON(t, testRules, "")
	cases := []struct {
		name string
		cmd  Command
		want string
	}{
		{"plain fetch", Command{Type: "fetch", Path: "plain.log"}, limitFetch},
		{"redacted fetch", Command{Type: "fetch", Path: "plain.log", Redact: redacted}, limitFetch},
		{".gz over max_bytes", Command{Type: "fetch", Path: "stored.log.gz", Decompress: "gzip", Redact: redacted}, limitFetch},
		{"archive over max_bytes", Command{Type: "fetch", Path: "stored.zip", Member: "a.log", Redact: redacted}, limitFetch},
		{"fetch_archive over max_bytes", Command{Type: "fetch_archive", Path: "stored.zip", Members: []string{"a.log"}, Redact: redacted}, limitFetch},
		{".gz expanded", Command{Type: "fetch", Path: "expands.log.gz", Decompress: "gzip", Redact: redacted}, limitDecompressed},
		{"zip member expanded", Command{Type: "fetch", Path: "expands.zip", Member: "a.log", Redact: redacted}, limitDecompressed},
		{"tar member expanded", Command{Type: "fetch", Path: "expands.tgz", Member: "a.log", Redact: redacted}, limitDecompressed},
		{"fetch_archive member expanded, not redacted", Command{Type: "fetch_archive", Path: "expands.zip", Members: []string{"a.log"}}, limitDecompressed},
	}
	for _, c := range cases {
		cmd := c.cmd
		cmd.ID, cmd.ChunkSize, cmd.MaxBytes, cmd.MaxDecompressed = "f", minChunkSize, limit, limit
		if got := tooLargeLimit(t, base, cmd); got != c.want {
			t.Errorf("%s: limit = %q, want %q", c.name, got, c.want)
		}
	}
}
