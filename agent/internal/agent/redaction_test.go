package agent

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/quaresma870/perchtail/agent/internal/redact"
)

// Redaction on the host (#382, #249): whatever path a command with a
// `redact` block takes, nothing of the original values leaves the agent,
// and what it sends is what the shared engine gives (the engine's parity
// with the server's is pinned by agent/internal/redact/testdata).

var testHeadKey = bytes.Repeat([]byte{7}, headKeyBytes)

var testRules = []redact.RuleSpec{
	{"id": 1, "name": "card", "detector": "card"},
	{"id": 2, "name": "secret_kv", "detector": "secret_kv"},
	{"id": 3, "name": "email", "detector": "email"},
	{"id": 4, "name": "private_key", "detector": "private_key"},
}

const secretLog = "start\n" +
	"user alice@example.com logged in\n" +
	"password=hunter2 ok\n" +
	"card 4111 1111 1111 1111 paid\n" +
	"-----BEGIN RSA PRIVATE KEY-----\n" +
	"MIIEsecretkeybody\n" +
	"-----END RSA PRIVATE KEY-----\n" +
	"end\n"

// The originals that must never be sent.
var secretValues = []string{"alice@example.com", "hunter2", "4111 1111", "MIIEsecretkeybody"}

func assertNoSecret(t *testing.T, what string, data []byte) {
	t.Helper()
	for _, secret := range secretValues {
		if bytes.Contains(data, []byte(secret)) {
			t.Fatalf("%s carries %q in clear: %q", what, secret, data)
		}
	}
}

func redactJSON(t *testing.T, rules []redact.RuleSpec, output string) json.RawMessage {
	t.Helper()
	digest, err := redact.RulesDigest(rules)
	if err != nil {
		t.Fatal(err)
	}
	block := map[string]any{
		"engine": redact.EngineVersion, "digest": digest, "rules": rules,
		"head_key": base64.StdEncoding.EncodeToString(testHeadKey),
	}
	if output != "" {
		block["output_encoding"] = output
	}
	raw, err := json.Marshal(block)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func compiled(t *testing.T, rules []redact.RuleSpec) *redact.Rules {
	t.Helper()
	r, err := redact.Compile(rules)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

// collect runs serveCommand and returns what it sent.
func collect(t *testing.T, base string, cmd Command) []any {
	t.Helper()
	var sent []any
	if err := serveCommand(context.Background(), Config{BasePath: base}, cmd,
		func(msg any) error { sent = append(sent, msg); return nil }); err != nil {
		t.Fatal(err)
	}
	return sent
}

// streamed splits what a fetch sent into its chunks' content (checking
// they are numbered in order with the other messages, and no larger than
// limit) and its last message.
func streamed(t *testing.T, sent []any, limit int) ([]byte, any) {
	t.Helper()
	if len(sent) == 0 {
		t.Fatal("nothing sent")
	}
	var data []byte
	for i, msg := range sent[:len(sent)-1] {
		chunk, ok := msg.(FetchChunk)
		if !ok {
			continue
		}
		if chunk.Seq != i {
			t.Fatalf("message %d has seq %d", i, chunk.Seq)
		}
		piece, _ := base64.StdEncoding.DecodeString(chunk.DataB64)
		if len(piece) > limit {
			t.Fatalf("chunk of %d bytes, over %d", len(piece), limit)
		}
		data = append(data, piece...)
	}
	return data, sent[len(sent)-1]
}

func refusal(t *testing.T, sent []any, code string) ErrorResult {
	t.Helper()
	if len(sent) != 1 {
		t.Fatalf("sent %d messages, want only the refusal: %+v", len(sent), sent)
	}
	result, ok := sent[0].(ErrorResult)
	if !ok || result.Type != "redaction_error" || result.Code != code {
		t.Fatalf("answer = %+v, want a redaction_error %s", sent[0], code)
	}
	return result
}

func TestConnectAnnouncesRedaction(t *testing.T) {
	got := make(chan string, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- r.Header.Get(FeaturesHeader)
		if conn, err := upgrader.Upgrade(w, r, nil); err == nil {
			conn.Close()
		}
	}))
	defer server.Close()
	cfg := Config{ServerURL: "ws" + strings.TrimPrefix(server.URL, "http"), Token: "t"}
	_, _ = connectAndServe(context.Background(), cfg, newUpgrader(cfg))
	if features := <-got; features != "redaction/1" {
		t.Fatalf("features header = %q, want redaction/1", features)
	}
}

func TestARedactedFetchOverTheConnection(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log", []byte(secretLog))
	fakeBackend(t, base, func(conn *websocket.Conn) {
		_ = conn.WriteJSON(Command{
			Type: "fetch", ID: "f", Path: "app.log", ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, ""),
		})
		var data []byte
		for {
			msg := readType(t, conn)
			if msg["type"] == "fetch_chunk" {
				piece, _ := base64.StdEncoding.DecodeString(msg["data_b64"].(string))
				data = append(data, piece...)
				continue
			}
			assertNoSecret(t, "the fetch", data)
			if msg["type"] != "fetch_result" || msg["redacted"] != true || msg["digest"] != compiled(t, testRules).Digest() {
				t.Errorf("end = %v", msg)
			}
			return
		}
	})
}

func TestParseRedactionWithoutABlock(t *testing.T) {
	for _, raw := range []string{"", "null", " null "} {
		if red, err := parseRedaction(json.RawMessage(raw)); red != nil || err != nil {
			t.Errorf("%q: %v %v, want nothing", raw, red, err)
		}
	}
}

func TestParseRedactionRefusesWhatItCantApply(t *testing.T) {
	key := base64.StdEncoding.EncodeToString(testHeadKey)
	digest, _ := redact.RulesDigest(testRules[:1])
	good := func(change string) string {
		block := `{"engine":1,"digest":"` + digest + `","rules":[{"id":1,"name":"card","detector":"card"}],"head_key":"` + key + `"` + change + `}`
		return block
	}
	custom := func(maxLength string) string {
		return `{"engine":1,"digest":"x","head_key":"` + key + `","rules":[{"id":1,"name":"card","detector":"card"},` +
			`{"id":9,"name":"acct","pattern":"ACCT \\d+","max_length":` + maxLength + `}]}`
	}
	cases := []struct {
		name, raw, code string
		rule            any
	}{
		{"another engine", strings.Replace(good(""), `"engine":1`, `"engine":2`, 1), codeInvalidRedact, nil},
		{"an unknown field", good(`,"keep_originals":true`), codeInvalidRedact, nil},
		{"no rules", `{"engine":1,"digest":"","head_key":"` + key + `"}`, codeInvalidRedact, nil},
		{"a short head key", strings.Replace(good(""), key, "c2hvcnQ=", 1), codeInvalidRedact, nil},
		{"a digest of other rules", strings.Replace(good(""), digest, strings.Repeat("0", 64), 1), codeInvalidRedact, nil},
		{"another output encoding", good(`,"output_encoding":"latin-1"`), codeInvalidRedact, nil},
		{"lines over the engine's limit", good(`,"max_line_bytes":2097152`), codeInvalidRedact, nil},
		{"not JSON", `{"engine":`, codeInvalidRedact, nil},
		// The server's engine refuses these, so the agent must as well:
		// numbers are read as written (UseNumber).
		{"max_length 1e2", custom("1e2"), codeRuleUnsupported, json.Number("9")},
		{"max_length 100.0", custom("100.0"), codeRuleUnsupported, json.Number("9")},
		{"max_length over 4096", custom("4097"), codeRuleUnsupported, json.Number("9")},
		{"a pattern RE2 can't run", strings.Replace(custom("10"), `ACCT \\d+`, `\\C`, 1), codeRuleUnsupported, json.Number("9")},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := parseRedaction(json.RawMessage(c.raw))
			result := errorResult(Command{ID: "r"}, "fetch_error", err)
			if result.Type != "redaction_error" || result.Code != c.code || result.Rule != c.rule {
				t.Fatalf("got %+v (%v), want %s for rule %v", result, err, c.code, c.rule)
			}
		})
	}
}

func TestParseRedactionAcceptsMaxLengthAtTheLimit(t *testing.T) {
	rules := []redact.RuleSpec{{"id": 9, "name": "acct", "pattern": `ACCT \d+`, "max_length": 4096}}
	red, err := parseRedaction(redactJSON(t, rules, ""))
	if err != nil || red == nil {
		t.Fatalf("%v", err)
	}
	if red.rules.Digest() != compiled(t, rules).Digest() {
		t.Error("the digest differs from the rules'")
	}
}

func TestRedactedFetchForTheViewer(t *testing.T) {
	base := t.TempDir()
	// Over a few chunks, and ending with a line still being written.
	content := []byte(strings.Repeat(secretLog, 40) + "card 4111 1111 11")
	writeFile(t, base, "app.log", content)

	sent := collect(t, base, Command{
		Type: "fetch", ID: "f", Path: "app.log", ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, ""),
	})
	data, last := streamed(t, sent, minChunkSize)
	assertNoSecret(t, "the fetch", data)
	want, result, err := redact.RedactBytes(content, compiled(t, testRules), false, redact.Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(data, want) {
		t.Fatal("the content differs from the engine's redaction")
	}
	if !bytes.HasSuffix(data, []byte("[redacted: incomplete line]")) {
		t.Errorf("the unfinished line isn't the marker: %q", data[len(data)-40:])
	}
	end, ok := last.(RedactedFetchEnd)
	if !ok {
		t.Fatalf("end = %#v", last)
	}
	if end.Type != "fetch_result" || !end.Streamed || !end.Redacted || end.Digest != compiled(t, testRules).Digest() ||
		end.Size != int64(len(data)) || end.Chunks != len(sent)-1 || end.SourceSize != int64(len(content)) ||
		end.SourceOffset != result.SourceOffset || end.SourceOffset != int64(len(content)-len("card 4111 1111 11")) ||
		end.TrailingPartial != result.TrailingPartial || end.Counts["card"] != 40 || end.Counts["private_key"] != 40 {
		t.Errorf("end = %+v", end)
	}
	if end.HeadDigest != redact.HeadDigest(testHeadKey, content, end.SourceOffset) {
		t.Error("head digest")
	}
}

func TestRedactedFetchForADownloadEndsTheLastLine(t *testing.T) {
	base := t.TempDir()
	content := []byte(secretLog + "card 4111 1111 1111 1111")
	writeFile(t, base, "app.log", content)

	sent := collect(t, base, Command{
		Type: "fetch", ID: "f", Path: "app.log", ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, "source"),
	})
	data, last := streamed(t, sent, minChunkSize)
	assertNoSecret(t, "the download", data)
	want, _, _ := redact.RedactBytes(content, compiled(t, testRules), true, redact.Options{OutputEncoding: "source"})
	end := last.(RedactedFetchEnd)
	if !bytes.Equal(data, want) || end.TrailingPartial != 0 || end.SourceOffset != int64(len(content)) {
		t.Errorf("data %q, end %+v", data, end)
	}
}

func TestRedactedFetchEncodesUTF16BackForADownload(t *testing.T) {
	base := t.TempDir()
	content := append([]byte{0xff, 0xfe}, utf16LE(secretLog)...) // with a BOM
	writeFile(t, base, "app.log", content)

	viewer, _ := streamed(t, collect(t, base, Command{
		Type: "fetch", ID: "v", Path: "app.log", ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, ""),
	}), minChunkSize)
	download, _ := streamed(t, collect(t, base, Command{
		Type: "fetch", ID: "d", Path: "app.log", ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, "source"),
	}), minChunkSize)
	assertNoSecret(t, "the viewer's UTF-8", viewer)
	if !bytes.Contains(viewer, []byte("[redacted: email]")) || bytes.Contains(viewer, []byte{0}) {
		t.Errorf("viewer = %q, want UTF-8", viewer)
	}
	if !bytes.HasPrefix(download, []byte{0xff, 0xfe}) || !bytes.Contains(download, utf16LE("ok\n")) {
		t.Errorf("download = %q, want UTF-16 LE", download)
	}
	for _, secret := range secretValues {
		if bytes.Contains(download, utf16LE(secret)) {
			t.Fatalf("the download carries %q", secret)
		}
	}
}

func utf16LE(s string) []byte {
	var out []byte
	for _, r := range s {
		out = append(out, byte(r), byte(r>>8))
	}
	return out
}

func TestRedactedFetchRefusesBeforeSendingAnything(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipArchive(t, map[string]string{"a.log": secretLog}))
	writeFile(t, base, "app.log.gz", gzipped(t, secretLog))
	writeFile(t, base, "wide.log", append([]byte{0xff, 0xfe, 0, 0}, []byte("p\x00\x00\x00")...))
	writeFile(t, base, "app.log", []byte(secretLog))
	block := redactJSON(t, testRules, "")
	fetch := func(path string) Command {
		return Command{Type: "fetch", ID: "f", Path: path, ChunkSize: minChunkSize, Redact: block}
	}

	refusal(t, collect(t, base, fetch("logs.zip")), codeNotRedactable)
	refusal(t, collect(t, base, fetch("app.log.gz")), codeNotRedactable)
	refusal(t, collect(t, base, fetch("wide.log")), codeUnsupportedEncoding)
	bad := fetch("app.log")
	bad.Redact = json.RawMessage(strings.Replace(string(block), `"detector":"email"`, `"detector":"phone"`, 1))
	if result := refusal(t, collect(t, base, bad), codeRuleUnsupported); result.Rule != json.Number("3") {
		t.Errorf("rule = %v, want 3", result.Rule)
	}
	// Never as one message with the content, even if asked so.
	unstreamed := fetch("app.log")
	unstreamed.ChunkSize = 0
	data, _ := streamed(t, collect(t, base, unstreamed), minChunkSize)
	assertNoSecret(t, "a fetch without chunk_size", data)
	if result, ok := handleCommand(Config{BasePath: base}, unstreamed).(ErrorResult); !ok || result.Type != "fetch_error" {
		t.Errorf("handleCommand answered %+v", result)
	}
}

func gzipped(t *testing.T, content string) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write([]byte(content)); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestRedactedFetchOfAGzipFile(t *testing.T) {
	base := t.TempDir()
	content := secretLog + "last 4111 1111 1111 1111"
	writeFile(t, base, "app.log.gz", gzipped(t, content))
	cmd := Command{
		Type: "fetch", ID: "f", Path: "app.log.gz", ChunkSize: minChunkSize, Decompress: "gzip",
		MaxDecompressed: 1 << 20, Redact: redactJSON(t, testRules, ""),
	}
	data, last := streamed(t, collect(t, base, cmd), minChunkSize)
	assertNoSecret(t, "the .gz", data)
	// Complete content: the last line is redacted as it is.
	want, _, _ := redact.RedactBytes([]byte(content), compiled(t, testRules), true, redact.Options{})
	if !bytes.Equal(data, want) || last.(RedactedFetchEnd).TrailingPartial != 0 {
		t.Errorf("data = %q", data)
	}

	cmd.MaxDecompressed = 50
	sent := collect(t, base, cmd)
	if result, ok := sent[len(sent)-1].(ErrorResult); !ok || result.Type != "fetch_error" || result.Code != "too_large" {
		t.Errorf("over the limit: %+v", sent[len(sent)-1])
	}
	cmd.MaxDecompressed, cmd.Decompress = 0, "zstd"
	if result := collect(t, base, cmd)[0].(ErrorResult); result.Type != "fetch_error" {
		t.Errorf("unknown decompression: %+v", result)
	}
}

func TestADecompressedFetchWithoutRedaction(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log.gz", gzipped(t, "plain\n"))
	data, last := streamed(t, collect(t, base, Command{
		Type: "fetch", ID: "f", Path: "app.log.gz", ChunkSize: minChunkSize, Decompress: "gzip",
	}), minChunkSize)
	if string(data) != "plain\n" {
		t.Errorf("data = %q", data)
	}
	if _, ok := last.(FetchEnd); !ok {
		t.Errorf("end = %#v, want a plain FetchEnd", last)
	}
}

type zipMember struct {
	name      string
	content   string
	encrypted bool
	nonUTF8   bool
	modified  time.Time
}

func zipArchive(t *testing.T, members map[string]string) []byte {
	t.Helper()
	var list []zipMember
	for name, content := range members {
		list = append(list, zipMember{name: name, content: content})
	}
	return zipOf(t, list)
}

func zipOf(t *testing.T, members []zipMember) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for _, m := range members {
		header := &zip.FileHeader{Name: m.name, Method: zip.Deflate, NonUTF8: m.nonUTF8, Modified: m.modified}
		if m.encrypted {
			header.Flags |= 0x1
		}
		f, err := w.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.Write([]byte(m.content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

type tarMember struct {
	name, content, link string
	flag                byte
	modified            time.Time
}

func tarGzOf(t *testing.T, members []tarMember) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	w := tar.NewWriter(gz)
	for _, m := range members {
		flag := m.flag
		if flag == 0 {
			flag = tar.TypeReg
		}
		modified := m.modified
		if modified.IsZero() {
			modified = time.Unix(0, 0)
		}
		header := &tar.Header{Name: m.name, Typeflag: flag, Linkname: m.link, Mode: 0o644, ModTime: modified}
		if flag == tar.TypeReg {
			header.Size = int64(len(m.content))
		}
		if err := w.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if flag == tar.TypeReg {
			if _, err := w.Write([]byte(m.content)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestRedactedFetchOfArchiveMembers(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipOf(t, []zipMember{
		{name: "app/a.log", content: secretLog + "tail 4111 1111 1111 1111"},
		{name: "./odd//a.log", content: secretLog + "tail 4111 1111 1111 1111"},
		{name: "inner.zip", content: "PK"},
		{name: "locked.log", content: "x", encrypted: true},
	}))
	writeFile(t, base, "logs.tgz", tarGzOf(t, []tarMember{
		{name: "app/", flag: tar.TypeDir},
		{name: "app/a.log", content: secretLog + "tail 4111 1111 1111 1111"},
		{name: "./odd//a.log", content: secretLog + "tail 4111 1111 1111 1111"},
		{name: "link.log", flag: tar.TypeSymlink, link: "app/a.log"},
	}))
	want, _, _ := redact.RedactBytes([]byte(secretLog+"tail 4111 1111 1111 1111"), compiled(t, testRules), true, redact.Options{})
	member := func(path, name string) Command {
		return Command{
			Type: "fetch", ID: "m", Path: path, Member: name, ChunkSize: minChunkSize,
			MaxDecompressed: 1 << 20, Redact: redactJSON(t, testRules, ""),
		}
	}
	for _, archive := range []string{"logs.zip", "logs.tgz"} {
		// The stored name, or the normalized name the tree shows.
		for _, name := range []string{"app/a.log", "./odd//a.log", "odd/a.log"} {
			data, last := streamed(t, collect(t, base, member(archive, name)), minChunkSize)
			assertNoSecret(t, archive+" "+name, data)
			if !bytes.Equal(data, want) {
				t.Errorf("%s %s: %q", archive, name, data)
			}
			if end, ok := last.(RedactedFetchEnd); !ok || end.TrailingPartial != 0 {
				t.Errorf("%s: end = %#v", archive, last)
			}
		}
		for _, name := range []string{"missing.log", "app"} {
			if result := collect(t, base, member(archive, name))[0].(ErrorResult); result.Code != "not_found" {
				t.Errorf("%s %s: %+v", archive, name, result)
			}
		}
		tooLarge := member(archive, "app/a.log")
		tooLarge.MaxDecompressed = 10
		sent := collect(t, base, tooLarge)
		if result := sent[len(sent)-1].(ErrorResult); result.Code != "too_large" {
			t.Errorf("%s over the limit: %+v", archive, result)
		}
	}
	// A link would open another member under its own name.
	if result := collect(t, base, member("logs.tgz", "link.log"))[0].(ErrorResult); result.Code != "not_found" {
		t.Errorf("link: %+v", result)
	}
	refusal(t, collect(t, base, member("logs.zip", "inner.zip")), codeNotRedactable)
	if result := collect(t, base, member("logs.zip", "locked.log"))[0].(ErrorResult); result.Type != "fetch_error" {
		t.Errorf("encrypted: %+v", result)
	}
	if result := collect(t, base, member("app.log", "x"))[0].(ErrorResult); result.Type != "fetch_error" {
		t.Errorf("not an archive: %+v", result)
	}
}

func TestListArchive(t *testing.T) {
	base := t.TempDir()
	when := time.Date(2024, 5, 6, 7, 8, 10, 0, time.UTC)
	writeFile(t, base, "logs.ZIP", zipOf(t, []zipMember{
		{name: "app/", modified: when},
		{name: "app/a.log", content: "hello", modified: when},
		{name: "\x81ber.log", content: "x", nonUTF8: true}, // code page 437, as Python reads it
	}))
	writeFile(t, base, "logs.tar.gz", tarGzOf(t, []tarMember{
		{name: "app/", flag: tar.TypeDir, modified: when},
		{name: "app/a.log", content: "hello", modified: when},
		{name: "link.log", flag: tar.TypeSymlink, link: "app/a.log"},
		{name: "fifo", flag: tar.TypeFifo},
	}))

	zipped, truncated, err := ListArchive(base, "logs.ZIP", 0)
	if err != nil || truncated {
		t.Fatal(err, truncated)
	}
	wantZip := []ArchiveMemberWire{
		{Name: "app/", IsDir: true, MTimeMs: when.UnixMilli()},
		{Name: "app/a.log", Size: 5, MTimeMs: when.UnixMilli()},
		{Name: "über.log", Size: 1, MTimeMs: zipped[2].MTimeMs},
	}
	if len(zipped) != 3 || zipped[0] != wantZip[0] || zipped[1] != wantZip[1] || zipped[2].Name != wantZip[2].Name {
		t.Errorf("zip members = %+v", zipped)
	}

	tarred, _, err := ListArchive(base, "logs.tar.gz", 0)
	if err != nil {
		t.Fatal(err)
	}
	wantTar := []ArchiveMemberWire{
		{Name: "app", IsDir: true, MTimeMs: when.UnixMilli()},
		{Name: "app/a.log", Size: 5, MTimeMs: when.UnixMilli()},
	}
	if len(tarred) != 2 || tarred[0] != wantTar[0] || tarred[1] != wantTar[1] {
		t.Errorf("tar members = %+v (links and FIFOs aren't listed)", tarred)
	}

	first, truncated, err := ListArchive(base, "logs.tar.gz", 1)
	if err != nil || !truncated || len(first) != 1 {
		t.Errorf("capped: %+v %v %v", first, truncated, err)
	}
	if _, _, err := ListArchive(base, "../logs.zip", 0); err == nil {
		t.Error("an unsafe path was listed")
	}
	writeFile(t, base, "app.log", []byte("x"))
	if result := handleCommand(Config{BasePath: base}, Command{Type: "list_archive", ID: "l", Path: "app.log"}); result.(ErrorResult).Type != "list_archive_error" {
		t.Errorf("not an archive: %+v", result)
	}
	if result := handleCommand(Config{BasePath: base}, Command{Type: "list_archive", ID: "l", Path: "logs.ZIP"}); len(result.(ListArchiveResult).Members) != 3 {
		t.Errorf("list_archive: %+v", result)
	}
}

func TestFetchArchiveSendsTheMembersAskedForRedacted(t *testing.T) {
	when := time.Date(2024, 5, 6, 7, 8, 10, 0, time.UTC)
	content := secretLog + "tail 4111 1111 1111 1111"
	wide := string([]byte{0xff, 0xfe, 0, 0, 'p', 0, 0, 0})
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipOf(t, []zipMember{
		{name: "a.log", content: content, modified: when},
		{name: "hidden.log", content: "password=hunter2\n"},
		{name: "b.log.gz", content: string(gzipped(t, content))},
		{name: "inner.tgz", content: "x"},
		{name: "locked.log", content: "x", encrypted: true},
		{name: "wide.log", content: wide},
	}))
	writeFile(t, base, "logs.tgz", tarGzOf(t, []tarMember{
		{name: "a.log", content: content, modified: when},
		{name: "hidden.log", content: "password=hunter2\n"},
		{name: "b.log.gz", content: string(gzipped(t, content))},
		{name: "inner.tgz", content: "x"},
		{name: "link.log", flag: tar.TypeSymlink, link: "a.log"},
		{name: "wide.log", content: wide},
	}))
	want, _, _ := redact.RedactBytes([]byte(content), compiled(t, testRules), true, redact.Options{})

	for _, archive := range []string{"logs.zip", "logs.tgz"} {
		sent := collect(t, base, Command{
			Type: "fetch_archive", ID: "z", Path: archive, ChunkSize: minChunkSize, MaxDecompressed: 1 << 20,
			Members: []string{"a.log", "b.log.gz", "inner.tgz", "locked.log", "link.log", "wide.log", "gone.log", "a.log"},
			Redact:  redactJSON(t, testRules, "source"),
		})
		members := map[string][]byte{}
		var order []ArchiveMember
		current := ""
		for i, msg := range sent[:len(sent)-1] {
			switch m := msg.(type) {
			case ArchiveMember:
				if m.Seq != i || m.Type != "archive_member" || m.ID != "z" {
					t.Fatalf("%s: message %d = %+v", archive, i, m)
				}
				order = append(order, m)
				current = m.Name
			case FetchChunk:
				if m.Seq != i {
					t.Fatalf("%s: chunk %d has seq %d", archive, i, m.Seq)
				}
				piece, _ := base64.StdEncoding.DecodeString(m.DataB64)
				members[current] = append(members[current], piece...)
			default:
				t.Fatalf("%s: message %d = %#v", archive, i, msg)
			}
		}
		skipped := map[string]string{}
		for _, m := range order {
			skipped[m.Name] = m.Skipped
		}
		wantSkipped := map[string]string{
			"a.log": "", "b.log.gz": "", "inner.tgz": "nested_archive", "wide.log": "unsupported_encoding",
			"gone.log": "not_found", "locked.log": "encrypted", "link.log": "not_found",
		}
		if archive == "logs.tgz" {
			wantSkipped["locked.log"] = "not_found"
		}
		if len(order) != len(wantSkipped) {
			t.Errorf("%s: members = %+v", archive, order)
		}
		for name, reason := range wantSkipped {
			if got, ok := skipped[name]; !ok || got != reason {
				t.Errorf("%s: %s skipped %q, want %q", archive, name, got, reason)
			}
		}
		if order[0].Name != "a.log" || order[0].MTimeMs != when.UnixMilli() || order[1].Name != "b.log.gz" || !order[1].Gunzipped {
			t.Errorf("%s: %+v", archive, order[:2])
		}
		for name, data := range members {
			assertNoSecret(t, archive+" "+name, data)
		}
		if !bytes.Equal(members["a.log"], want) || !bytes.Equal(members["b.log.gz"], want) || len(members) != 2 {
			t.Errorf("%s: contents %q", archive, members)
		}
		end, ok := sent[len(sent)-1].(RedactedFetchEnd)
		if !ok || !end.Redacted || end.Counts["card"] != 4 || end.SourceSize != int64(2*len(content)) || end.Size != int64(2*len(want)) {
			t.Errorf("%s: end = %#v", archive, sent[len(sent)-1])
		}
	}
}

// As the server's _redact_content: a member over max_decompressed once
// decompressed, or a corrupt .gz member, is left out (announced again as
// skipped when part of it was already sent) and the other members go on.
func TestFetchArchiveLeavesOutAMemberItCantRead(t *testing.T) {
	content := secretLog + "tail 4111 1111 1111 1111"
	long := strings.Repeat("a line of the log\n", 300) // 5400 bytes: two chunks
	corrupt := gzipped(t, long)
	corrupt[len(corrupt)-8] ^= 0xff // CRC-32
	truncated := gzipped(t, long)
	truncated = truncated[:len(truncated)-4]
	members := []struct{ name, content string }{
		{"a.log", content},
		{"big.log", strings.Repeat("a line of the log\n", 1000)},
		{"crc.log.gz", string(corrupt)},
		{"cut.log.gz", string(truncated)},
		{"header.log.gz", "not gzip at all\n"},
		{"c.log", content},
	}
	var zipped []zipMember
	var tarred []tarMember
	var names []string
	for _, m := range members {
		zipped = append(zipped, zipMember{name: m.name, content: m.content})
		tarred = append(tarred, tarMember{name: m.name, content: m.content})
		names = append(names, m.name)
	}
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipOf(t, zipped))
	writeFile(t, base, "logs.tgz", tarGzOf(t, tarred))
	want, _, _ := redact.RedactBytes([]byte(content), compiled(t, testRules), true, redact.Options{})

	for _, archive := range []string{"logs.zip", "logs.tgz"} {
		sent := collect(t, base, Command{
			Type: "fetch_archive", ID: "z", Path: archive, ChunkSize: minChunkSize, MaxDecompressed: 6000,
			Members: names, Redact: redactJSON(t, testRules, "source"),
		})
		end, ok := sent[len(sent)-1].(RedactedFetchEnd)
		if !ok {
			t.Fatalf("%s: ended with %#v", archive, sent[len(sent)-1])
		}
		// The content of each member, from its last announcement on, and
		// the reason it was left out, if it was.
		got := map[string][]byte{}
		skipped := map[string]string{}
		withdrawn := map[string]bool{}
		current := ""
		for i, msg := range sent[:len(sent)-1] {
			switch m := msg.(type) {
			case ArchiveMember:
				if m.Seq != i {
					t.Fatalf("%s: message %d has seq %d", archive, i, m.Seq)
				}
				if _, seen := skipped[m.Name]; seen {
					if current != m.Name || m.Skipped == "" {
						t.Fatalf("%s: %s announced again out of place: %+v", archive, m.Name, m)
					}
					withdrawn[m.Name] = true
				}
				current = m.Name
				skipped[m.Name] = m.Skipped
				delete(got, m.Name)
			case FetchChunk:
				if m.Seq != i || skipped[current] != "" {
					t.Fatalf("%s: chunk %d (seq %d) for %s", archive, i, m.Seq, current)
				}
				piece, _ := base64.StdEncoding.DecodeString(m.DataB64)
				got[current] = append(got[current], piece...)
			}
		}
		wantSkipped := map[string]string{
			"a.log": "", "big.log": "too_large", "crc.log.gz": "unreadable",
			"cut.log.gz": "unreadable", "header.log.gz": "unreadable", "c.log": "",
		}
		for name, reason := range wantSkipped {
			if r, ok := skipped[name]; !ok || r != reason {
				t.Errorf("%s: %s skipped %q, want %q", archive, name, r, reason)
			}
		}
		if !withdrawn["big.log"] || !withdrawn["crc.log.gz"] {
			t.Errorf("%s: withdrawn = %v, want big.log and crc.log.gz", archive, withdrawn)
		}
		if len(got) != 2 || !bytes.Equal(got["a.log"], want) || !bytes.Equal(got["c.log"], want) {
			t.Errorf("%s: contents %q", archive, got)
		}
		if end.SourceSize != int64(2*len(content)) || end.Counts["card"] != 4 {
			t.Errorf("%s: end = %#v", archive, end)
		}
	}
}

func TestFetchArchiveWithoutRedaction(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipArchive(t, map[string]string{"a.log": "one\n", "b.log": "two\n"}))
	sent := collect(t, base, Command{Type: "fetch_archive", ID: "z", Path: "logs.zip", Members: []string{"b.log"}})
	if len(sent) != 3 || sent[0].(ArchiveMember).Name != "b.log" {
		t.Fatalf("sent %+v", sent)
	}
	if data, _ := base64.StdEncoding.DecodeString(sent[1].(FetchChunk).DataB64); string(data) != "two\n" {
		t.Errorf("content %q", data)
	}
	if end, ok := sent[2].(FetchEnd); !ok || end.Size != 4 {
		t.Errorf("end %#v", sent[2])
	}
	if result := collect(t, base, Command{Type: "fetch_archive", ID: "z", Path: "logs.zip", MaxBytes: 10})[0].(ErrorResult); result.Code != "too_large" {
		t.Errorf("over max_bytes: %+v", result)
	}
}

// readRange runs a redacted read_range and decodes its content.
func readRange(t *testing.T, base, path string, offset int64) (RedactedReadRangeResult, []byte) {
	t.Helper()
	answer := handleCommand(Config{BasePath: base}, Command{
		Type: "read_range", ID: "r", Path: path, Offset: offset, Length: maxRangeLength,
		HeadLength: 64, Redact: redactJSON(t, testRules, ""),
	})
	result, ok := answer.(RedactedReadRangeResult)
	if !ok {
		t.Fatalf("answer = %#v", answer)
	}
	data, _ := base64.StdEncoding.DecodeString(result.DataB64)
	if len(data) > maxRangeLength {
		t.Fatalf("%d bytes, over %d", len(data), maxRangeLength)
	}
	return result, data
}

func TestRedactedReadRangeNeverSendsTheStartOfTheFile(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log", []byte(secretLog))
	result, data := readRange(t, base, "app.log", 0)
	encoded, _ := json.Marshal(result)
	if strings.Contains(string(encoded), "head_b64") {
		t.Errorf("the answer carries the file's first bytes: %s", encoded)
	}
	assertNoSecret(t, "the read", data)
	if result.Type != "read_range_result" || !result.Redacted || result.Digest != compiled(t, testRules).Digest() ||
		result.NextOffset != int64(len(secretLog)) || result.ReadEnd != int64(len(secretLog)) || result.Aligned ||
		result.HeadDigest != redact.HeadDigest(testHeadKey, []byte(secretLog), 0) ||
		result.NextHeadDigest != redact.HeadDigest(testHeadKey, []byte(secretLog), int64(len(secretLog))) {
		t.Errorf("result = %+v", result)
	}
}

func TestRedactedReadRangeFollowsAGrowingFile(t *testing.T) {
	// A Tail of a file written in pieces, values cut between them, gives
	// what redacting the whole file gives.
	base := t.TempDir()
	parts := []string{
		"start\ncard 4111 1111", " 1111 1111 paid\n-----BEGIN RSA PRIVATE KEY-----\nMIIEsecret",
		"keybody\n", "-----END RSA PRIVATE KEY-----\nuser alice@exa", "mple.com in\npassword=hunter2\n",
	}
	var written, shown []byte
	offset, partial := int64(0), 0
	for _, part := range parts {
		written = append(written, part...)
		writeFile(t, base, "app.log", written)
		result, data := readRange(t, base, "app.log", offset)
		assertNoSecret(t, "a poll", data)
		// As the viewer: the marker of the unfinished line is replaced by
		// what the next poll sends.
		shown = append(shown[:len(shown)-partial], data...)
		offset, partial = result.NextOffset, result.TrailingPartial
	}
	want, _, _ := redact.RedactBytes(written, compiled(t, testRules), true, redact.Options{})
	if partial != 0 || !bytes.Equal(shown, want) {
		t.Errorf("shown %q\nwant  %q", shown, want)
	}
}

func TestRedactedReadRangeInsideAPrivateKey(t *testing.T) {
	base := t.TempDir()
	content := "start\n-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretkeybody\nMIIEsecretkeybody\n"
	writeFile(t, base, "app.log", []byte(content))
	// From the middle of a line inside the key: aligned to the next line,
	// which the context before the offset shows is still the key.
	offset := int64(strings.Index(content, "MIIE") + 4)
	result, data := readRange(t, base, "app.log", offset)
	assertNoSecret(t, "a read inside a key", data)
	// The key's BEGIN, in the context, took the marker; its lines keep
	// their line breaks only.
	if !result.Aligned || result.NextOffset != int64(len(content)) || string(data) != "\n" {
		t.Errorf("result %+v, data %q", result, data)
	}
}

func TestRedactedReadRangeReadsLessWhenRedactingGrowsTheContent(t *testing.T) {
	// Short emails become longer markers: the answer still fits, and the
	// following polls go on from where it stopped.
	base := t.TempDir()
	content := bytes.Repeat([]byte("a@b.cd\n"), maxRangeLength/7)
	writeFile(t, base, "app.log", content)
	var shown []byte
	offset, partial, polls := int64(0), 0, 0
	for offset < int64(len(content)) {
		result, data := readRange(t, base, "app.log", offset)
		if result.NextOffset <= offset && result.TrailingPartial == 0 {
			t.Fatalf("poll %d: no progress", polls)
		}
		shown = append(shown[:len(shown)-partial], data...)
		offset, partial = result.NextOffset, result.TrailingPartial
		polls++
	}
	want, _, _ := redact.RedactBytes(content, compiled(t, testRules), true, redact.Options{})
	if polls < 2 || partial != 0 || !bytes.Equal(shown, want) {
		t.Errorf("%d polls, %d bytes shown, want %d", polls, len(shown), len(want))
	}
}

func TestRedactedReadRangeSkipsALineWithNoEnd(t *testing.T) {
	base := t.TempDir()
	content := append(bytes.Repeat([]byte("x"), 2*maxRangeLength), "\nok\n"...)
	writeFile(t, base, "app.log", content)
	result, data := readRange(t, base, "app.log", 0)
	if string(data) != "[redacted: line over 1008 KiB]\n" || result.NextOffset != maxRangeLength || result.ReadEnd != maxRangeLength {
		t.Errorf("result %+v, data %q", result, data)
	}
}

func TestRedactedReadRangeRefusesUTF32(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "wide.log", []byte{0xff, 0xfe, 0, 0, 'p', 0, 0, 0})
	answer := handleCommand(Config{BasePath: base}, Command{
		Type: "read_range", ID: "r", Path: "wide.log", Length: 100, Redact: redactJSON(t, testRules, ""),
	})
	if result, ok := answer.(ErrorResult); !ok || result.Type != "redaction_error" || result.Code != codeUnsupportedEncoding {
		t.Errorf("answer %+v", answer)
	}
}

func TestRedactedReadRangePastTheEnd(t *testing.T) {
	base := t.TempDir()
	writeFile(t, base, "app.log", []byte("short\n"))
	result, data := readRange(t, base, "app.log", 100)
	if len(data) != 0 || result.Size != 6 || result.NextOffset != 100 {
		t.Errorf("result %+v", result)
	}
}

func TestAtMostTwoCommandsRedactAtOnce(t *testing.T) {
	slots := newCommandSlots()
	redacting := Command{Redact: json.RawMessage(`{}`)}
	release := []func(){slots.acquire(redacting), slots.acquire(redacting)}

	third := make(chan struct{})
	go func() {
		defer slots.acquire(redacting)()
		close(third)
	}()
	select {
	case <-third:
		t.Fatal("a third redacting command ran")
	case <-time.After(100 * time.Millisecond):
	}
	// A command that doesn't redact isn't held up.
	done := make(chan struct{})
	go func() {
		defer slots.acquire(Command{})()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("a plain command waited for the redaction slots")
	}
	release[0]()
	select {
	case <-third:
	case <-time.After(5 * time.Second):
		t.Fatal("the third redacting command never ran")
	}
	release[1]()
}

func TestRedactedFetchRefusesACompressedMemberNotAskedDecompressed(t *testing.T) {
	// A .gz member sent as it is would carry the original, compressed.
	base := t.TempDir()
	writeFile(t, base, "logs.zip", zipOf(t, []zipMember{{name: "app.log.gz", content: string(gzipped(t, secretLog))}}))
	writeFile(t, base, "logs.tar.gz", tarGzOf(t, []tarMember{{name: "app.log.gz", content: string(gzipped(t, secretLog))}}))
	want, _, _ := redact.RedactBytes([]byte(secretLog), compiled(t, testRules), true, redact.Options{})
	for _, archive := range []string{"logs.zip", "logs.tar.gz"} {
		cmd := Command{
			Type: "fetch", ID: "m", Path: archive, Member: "app.log.gz", ChunkSize: minChunkSize,
			MaxDecompressed: 1 << 20, Redact: redactJSON(t, testRules, ""),
		}
		refusal(t, collect(t, base, cmd), codeNotRedactable)
		// Asked for decompressed, it is redacted.
		cmd.Decompress = "gzip"
		data, last := streamed(t, collect(t, base, cmd), minChunkSize)
		assertNoSecret(t, archive+" app.log.gz", data)
		if _, ok := last.(RedactedFetchEnd); !ok || !bytes.Equal(data, want) {
			t.Errorf("%s: data %q, end %#v", archive, data, last)
		}
	}
}

// As the server's decompress_gzip: a .gz cut short, asked for
// decompressed (the file or an archive member), is refused, redacted or
// not, instead of passing as content up to the cut.
func TestFetchRefusesATruncatedGzAskedDecompressed(t *testing.T) {
	cut := gzipped(t, secretLog)
	cut = cut[:len(cut)-4]
	base := t.TempDir()
	writeFile(t, base, "app.log.gz", cut)
	writeFile(t, base, "logs.zip", zipOf(t, []zipMember{{name: "app.log.gz", content: string(cut)}}))
	writeFile(t, base, "logs.tar.gz", tarGzOf(t, []tarMember{{name: "app.log.gz", content: string(cut)}}))
	for _, redacting := range []bool{false, true} {
		for _, target := range []struct{ path, member string }{
			{"app.log.gz", ""}, {"logs.zip", "app.log.gz"}, {"logs.tar.gz", "app.log.gz"},
		} {
			cmd := Command{
				Type: "fetch", ID: "g", Path: target.path, Member: target.member, ChunkSize: minChunkSize,
				Decompress: "gzip", MaxDecompressed: 1 << 20,
			}
			if redacting {
				cmd.Redact = redactJSON(t, testRules, "")
			}
			sent := collect(t, base, cmd)
			result, ok := sent[len(sent)-1].(ErrorResult)
			if !ok || result.Type != "fetch_error" || !strings.Contains(result.Error, "end-of-stream") {
				t.Errorf("%s %s (redacting %v): ended with %#v", target.path, target.member, redacting, sent[len(sent)-1])
			}
		}
	}
}

func TestRedactedFetchRefusesAnArchiveAskedDecompressed(t *testing.T) {
	// A .tar.gz decompressed is a tar stream that starts with a member's
	// name, so it would pass as text and carry its .gz members' originals.
	base := t.TempDir()
	tarGz := tarGzOf(t, []tarMember{{name: "app.log.gz", content: string(gzipped(t, secretLog))}})
	writeFile(t, base, "logs.tar.gz", tarGz)
	writeFile(t, base, "logs.tgz", tarGz)
	writeFile(t, base, "outer.zip", zipOf(t, []zipMember{
		{name: "inner.tar.gz", content: string(tarGz)},
		{name: "inner.tgz", content: string(tarGz)},
	}))
	for _, decompress := range []string{"", "gzip"} {
		for _, path := range []string{"logs.tar.gz", "logs.tgz"} {
			cmd := Command{
				Type: "fetch", ID: "w", Path: path, ChunkSize: minChunkSize, Decompress: decompress,
				MaxDecompressed: 1 << 20, Redact: redactJSON(t, testRules, ""),
			}
			refusal(t, collect(t, base, cmd), codeNotRedactable)
		}
		for _, member := range []string{"inner.tar.gz", "inner.tgz"} {
			cmd := Command{
				Type: "fetch", ID: "m", Path: "outer.zip", Member: member, ChunkSize: minChunkSize,
				Decompress: decompress, MaxDecompressed: 1 << 20, Redact: redactJSON(t, testRules, ""),
			}
			refusal(t, collect(t, base, cmd), codeNotRedactable)
		}
	}
}

// compressedFormats are content in each compressed format redaction
// refuses, told by its first bytes, each holding the secrets.
func compressedFormats(t *testing.T) map[string][]byte {
	t.Helper()
	return map[string][]byte{
		"bzip2":       []byte("BZh9\x31\x41\x59\x26\x53\x59" + secretLog),
		"bzip2-empty": []byte("BZh1\x17\x72\x45\x38\x50\x90" + secretLog),
		"xz":          []byte("\xfd7zXZ\x00" + secretLog),
		"zstd":        []byte("\x28\xb5\x2f\xfd" + secretLog),
		"7z":          []byte("7z\xbc\xaf\x27\x1c" + secretLog),
		"rar":         []byte("Rar!\x1a\x07\x01\x00" + secretLog),
		"gzip":        gzipped(t, secretLog),
		"zip":         zipArchive(t, map[string]string{"a.log": secretLog}),
		"zip-empty":   []byte("PK\x05\x06" + secretLog),
		"zip-split":   []byte("PK\x07\x08" + secretLog),
	}
}

func TestUnsupportedCompressedFormat(t *testing.T) {
	for name, content := range compressedFormats(t) {
		if !unsupportedCompressedFormat(content[:min(len(content), redact.HeadBytes)]) {
			t.Errorf("%s isn't detected", name)
		}
	}
	// Text that only looks like the start of one.
	for _, text := range []string{"BZh9 is a name\n", "BZh0\x31\x41\x59\x26\x53\x59", "PK is a name\n", "Rar\n", "7z\n", ""} {
		if unsupportedCompressedFormat([]byte(text)) {
			t.Errorf("%q is taken for compressed content", text)
		}
	}
}

func TestRedactedFetchRefusesUnsupportedCompressedFormats(t *testing.T) {
	base := t.TempDir()
	formats := compressedFormats(t)
	for name, content := range formats {
		// A name that doesn't say it is compressed (app.log.1), or one that
		// does but isn't decompressed (.bz2).
		writeFile(t, base, name+".log.1", content)
		writeFile(t, base, name+".bz2", content)
		writeFile(t, base, name+".in.gz", gzipped(t, string(content)))
	}
	writeFile(t, base, "members.zip", zipOf(t, []zipMember{
		{name: "xz.log", content: string(formats["xz"])},
		{name: "gzip.log.1", content: string(formats["gzip"])},
		{name: "bzip2.log.gz", content: string(gzipped(t, string(formats["bzip2"])))},
	}))
	writeFile(t, base, "members.tgz", tarGzOf(t, []tarMember{
		{name: "xz.log", content: string(formats["xz"])},
		{name: "gzip.log.1", content: string(formats["gzip"])},
		{name: "bzip2.log.gz", content: string(gzipped(t, string(formats["bzip2"])))},
	}))
	block := redactJSON(t, testRules, "")
	for name := range formats {
		for _, path := range []string{name + ".log.1", name + ".bz2"} {
			for _, output := range []string{"", "source"} {
				cmd := Command{Type: "fetch", ID: "f", Path: path, ChunkSize: minChunkSize, Redact: redactJSON(t, testRules, output)}
				refusal(t, collect(t, base, cmd), codeUnsupportedFormat)
			}
		}
		// The content of a .gz file.
		refusal(t, collect(t, base, Command{
			Type: "fetch", ID: "f", Path: name + ".in.gz", ChunkSize: minChunkSize, Decompress: "gzip",
			MaxDecompressed: 1 << 20, Redact: block,
		}), codeUnsupportedFormat)
		// A redacted Tail.
		answer := handleCommand(Config{BasePath: base}, Command{
			Type: "read_range", ID: "r", Path: name + ".log.1", Offset: 0, Length: maxRangeLength, Redact: block,
		})
		if result, ok := answer.(ErrorResult); !ok || result.Type != "redaction_error" || result.Code != codeUnsupportedFormat {
			t.Errorf("read_range of %s: %+v", name, answer)
		}
		// Without redaction, nothing changes.
		data, _ := streamed(t, collect(t, base, Command{Type: "fetch", ID: "f", Path: name + ".log.1", ChunkSize: minChunkSize}), minChunkSize)
		if !bytes.Equal(data, formats[name]) {
			t.Errorf("%s without redaction: %q", name, data)
		}
	}
	for _, archive := range []string{"members.zip", "members.tgz"} {
		for _, member := range []string{"xz.log", "gzip.log.1"} {
			refusal(t, collect(t, base, Command{
				Type: "fetch", ID: "m", Path: archive, Member: member, ChunkSize: minChunkSize,
				MaxDecompressed: 1 << 20, Redact: block,
			}), codeUnsupportedFormat)
		}
		refusal(t, collect(t, base, Command{
			Type: "fetch", ID: "m", Path: archive, Member: "bzip2.log.gz", ChunkSize: minChunkSize, Decompress: "gzip",
			MaxDecompressed: 1 << 20, Redact: block,
		}), codeUnsupportedFormat)
		// In a fetch_archive, each is skipped with no content.
		sent := collect(t, base, Command{
			Type: "fetch_archive", ID: "z", Path: archive, ChunkSize: minChunkSize, MaxDecompressed: 1 << 20,
			Members: []string{"xz.log", "gzip.log.1", "bzip2.log.gz"}, Redact: block,
		})
		if len(sent) != 4 {
			t.Fatalf("%s: sent %+v", archive, sent)
		}
		for _, msg := range sent[:3] {
			if m, ok := msg.(ArchiveMember); !ok || m.Skipped != "unsupported_format" {
				t.Errorf("%s: %#v, want skipped unsupported_format", archive, msg)
			}
		}
		if end, ok := sent[3].(RedactedFetchEnd); !ok || end.Chunks != 0 || end.Size != 0 {
			t.Errorf("%s: end %#v", archive, sent[3])
		}
	}
}

func TestRedactedReadRangeWithASmallLengthNeverGoesBack(t *testing.T) {
	base := t.TempDir()
	line := "password=hunter2 line\n"
	content := strings.Repeat(line, 10000)
	writeFile(t, base, "app.log", []byte(content))
	read := func(offset, length int64) (RedactedReadRangeResult, []byte) {
		t.Helper()
		answer := handleCommand(Config{BasePath: base}, Command{
			Type: "read_range", ID: "r", Path: "app.log", Offset: offset, Length: length, Redact: redactJSON(t, testRules, ""),
		})
		result, ok := answer.(RedactedReadRangeResult)
		if !ok {
			t.Fatalf("answer = %#v", answer)
		}
		data, _ := base64.StdEncoding.DecodeString(result.DataB64)
		return result, data
	}
	// The offset is well past the context read before it (16 KiB), the
	// length well under it.
	offset := int64(len(line) * 4545)
	for _, start := range []int64{offset, offset + 5} {
		result, data := read(start, 4096)
		assertNoSecret(t, "a short read", data)
		if result.NextOffset <= start || result.NextOffset > start+4096 || result.NextOffset%int64(len(line)) != 0 {
			t.Errorf("from %d: next %d", start, result.NextOffset)
		}
		// The read ends inside a line, sent as the incomplete-line marker.
		if result.TrailingPartial == 0 || !bytes.HasSuffix(data, []byte("[redacted: incomplete line]")) {
			t.Fatalf("from %d: partial %d", start, result.TrailingPartial)
		}
		from := (start + int64(len(line)) - 1) / int64(len(line)) * int64(len(line))
		want, _, _ := redact.RedactBytes([]byte(content[from:result.NextOffset]), compiled(t, testRules), true, redact.Options{})
		if whole := data[:len(data)-result.TrailingPartial]; !bytes.Equal(whole, want) {
			t.Errorf("from %d: %q", start, whole)
		}
	}
	if result, data := read(offset, 0); result.NextOffset != offset || len(data) != 0 {
		t.Errorf("length 0: %+v %q", result, data)
	}

	// A line with no end within the short read is skipped, forward.
	long := strings.Repeat("ok\n", 40000) + strings.Repeat("x", 200000) + "\nok\n"
	writeFile(t, base, "app.log", []byte(long))
	result, data := read(120000, 4096)
	if result.NextOffset != 120000+4096 || string(data) != "[redacted: line over 4 KiB]\n" {
		t.Errorf("long line: next %d, data %q", result.NextOffset, data)
	}
}

func TestOldCommandsAreUnchanged(t *testing.T) {
	// Without a redact block, read_range still sends the head and data.
	base := t.TempDir()
	writeFile(t, base, "app.log", []byte(secretLog))
	answer := handleCommand(Config{BasePath: base}, Command{Type: "read_range", ID: "r", Path: "app.log", Length: 10, HeadLength: 5})
	if result, ok := answer.(ReadRangeResult); !ok || result.HeadB64 == "" {
		t.Errorf("answer %#v", answer)
	}
}
