package agent

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// A release signed with a key made for the test, served by a fake
// PerchTail server and a fake GitHub (#319).

const (
	testOldVersion = "0.7.0"
	testNewVersion = "0.7.1"
	testToken      = "agent-token"
)

type testRelease struct {
	priv    ed25519.PrivateKey
	pubPEM  []byte
	name    string
	binary  []byte
	sums    []byte
	sig     []byte
	goos    string
	goarch  string
	version string
}

func newTestRelease(t *testing.T, version string) *testRelease {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	r := &testRelease{
		priv:    priv,
		pubPEM:  pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}),
		goos:    "linux",
		goarch:  "amd64",
		version: version,
		binary:  []byte("#!/bin/sh\necho new agent " + version + "\n"),
	}
	r.name = binaryName(version, r.goos, r.goarch)
	r.resign(r.binary)
	return r
}

// resign writes SHA256SUMS for binary (plus another platform's line) and
// signs it.
func (r *testRelease) resign(binary []byte) {
	sum := sha256.Sum256(binary)
	other := sha256.Sum256([]byte("another platform's build"))
	r.sums = []byte(fmt.Sprintf("%s  %s\n%s  %s\n",
		hex.EncodeToString(other[:]), binaryName(r.version, "freebsd", "amd64"),
		hex.EncodeToString(sum[:]), r.name))
	r.sig = ed25519.Sign(r.priv, r.sums)
}

// fileServer serves the release's files under prefix; tamper can change
// what a file looks like on the way out. With token, requests without it
// are refused.
type fileServer struct {
	*httptest.Server
	mu       sync.Mutex
	files    map[string][]byte
	requests []string
}

func newFileServer(t *testing.T, prefix, token string, files map[string][]byte) *fileServer {
	t.Helper()
	fs := &fileServer{files: files}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fs.mu.Lock()
		fs.requests = append(fs.requests, r.URL.Path)
		data, ok := fs.files[strings.TrimPrefix(r.URL.Path, prefix)]
		fs.mu.Unlock()
		if token != "" && r.Header.Get("Authorization") != "Bearer "+token {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if !strings.HasPrefix(r.URL.Path, prefix) || !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(data)
	}))
	t.Cleanup(fs.Close)
	return fs
}

func (fs *fileServer) set(name string, data []byte) {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	fs.files[name] = data
}

func (fs *fileServer) setAll(files map[string][]byte) {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	fs.files = files
}

func (r *testRelease) files() map[string][]byte {
	return map[string][]byte{sumsFile: r.sums, sigFile: r.sig, r.name: r.binary}
}

type upgradeFixture struct {
	release *testRelease
	server  *fileServer
	github  *fileServer
	exe     string
	execs   []string
	execErr error
	u       *upgrader
}

func newUpgradeFixture(t *testing.T) *upgradeFixture {
	t.Helper()
	f := &upgradeFixture{release: newTestRelease(t, testNewVersion)}
	f.server = newFileServer(t, "/agent/binaries/", testToken, f.release.files())
	f.github = newFileServer(t, "/download/v"+testNewVersion+"/", "", f.release.files())
	dir := t.TempDir()
	f.exe = filepath.Join(dir, "perchtail-agent")
	if err := os.WriteFile(f.exe, []byte("old agent binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	f.u = &upgrader{
		current:       testOldVersion,
		goos:          f.release.goos,
		goarch:        f.release.goarch,
		allowed:       true,
		signingKeyPEM: f.release.pubPEM,
		client:        http.DefaultClient,
		maxBinary:     1 << 20,
		executable:    func() (string, error) { return f.exe, nil },
		restartCh:     make(chan struct{}),
	}
	f.u.exec = func(exe string) error {
		f.execs = append(f.execs, exe)
		return f.execErr
	}
	f.u.sources = func(version string) []binarySource {
		return []binarySource{
			{label: "the PerchTail server", base: f.server.URL + "/agent/binaries/", token: testToken},
			{label: "GitHub", base: f.github.URL + "/download/v" + version + "/"},
		}
	}
	return f
}

func (f *upgradeFixture) perform(t *testing.T) ([]string, error) {
	t.Helper()
	var stages []string
	err := f.u.perform(context.Background(), testNewVersion, func(stage, detail string) {
		stages = append(stages, strings.TrimSpace(stage+" "+detail))
	})
	return stages, err
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// leftovers lists what the upgrade left next to the binary besides the
// binary itself and its backup.
func leftovers(t *testing.T, exe string) []string {
	t.Helper()
	entries, err := os.ReadDir(filepath.Dir(exe))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		if e.Name() != filepath.Base(exe) && e.Name() != filepath.Base(exe)+".old" {
			names = append(names, e.Name())
		}
	}
	return names
}

func TestUpgradeInstallsAVerifiedBinaryFromTheServer(t *testing.T) {
	f := newUpgradeFixture(t)
	stages, err := f.perform(t)
	if err != nil {
		t.Fatalf("perform: %v", err)
	}
	if got := readFile(t, f.exe); got != string(f.release.binary) {
		t.Fatalf("binary = %q, want the new one", got)
	}
	if got := readFile(t, f.exe+".old"); got != "old agent binary" {
		t.Fatalf("backup = %q, want the old binary", got)
	}
	if len(f.execs) != 1 || f.execs[0] != f.exe {
		t.Fatalf("execs = %v, want one restart into %s", f.execs, f.exe)
	}
	want := []string{"downloading from the PerchTail server", "installing", "restarting"}
	if strings.Join(stages, "|") != strings.Join(want, "|") {
		t.Fatalf("stages = %v, want %v", stages, want)
	}
	f.github.mu.Lock()
	defer f.github.mu.Unlock()
	if len(f.github.requests) != 0 {
		t.Fatalf("GitHub was asked although the server had the release: %v", f.github.requests)
	}
	if extra := leftovers(t, f.exe); len(extra) != 0 {
		t.Fatalf("left behind: %v", extra)
	}
}

func TestUpgradeFallsBackToGitHub(t *testing.T) {
	f := newUpgradeFixture(t)
	f.server.setAll(map[string][]byte{}) // a server image without bundled binaries
	stages, err := f.perform(t)
	if err != nil {
		t.Fatalf("perform: %v", err)
	}
	if got := readFile(t, f.exe); got != string(f.release.binary) {
		t.Fatalf("binary = %q, want the new one", got)
	}
	if stages[1] != "downloading from GitHub" {
		t.Fatalf("stages = %v", stages)
	}
}

func assertUpgradeRefused(t *testing.T, f *upgradeFixture, err error, wantInError string) {
	t.Helper()
	if err == nil {
		t.Fatal("the upgrade went through")
	}
	if !strings.Contains(err.Error(), wantInError) {
		t.Fatalf("error = %q, want it to mention %q", err, wantInError)
	}
	if got := readFile(t, f.exe); got != "old agent binary" {
		t.Fatalf("binary = %q, the old one must stay in place", got)
	}
	if len(f.execs) != 0 {
		t.Fatalf("restarted although the upgrade failed: %v", f.execs)
	}
	if extra := leftovers(t, f.exe); len(extra) != 0 {
		t.Fatalf("left behind: %v", extra)
	}
}

func TestUpgradeRefusesATamperedBinary(t *testing.T) {
	f := newUpgradeFixture(t)
	for _, srv := range []*fileServer{f.server, f.github} {
		srv.set(f.release.name, append([]byte("evil"), f.release.binary...))
	}
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "doesn't match its SHA-256")
}

func TestUpgradeRefusesTamperedSums(t *testing.T) {
	// SHA256SUMS rewritten for a malicious binary, keeping the release's
	// signature: the signature no longer verifies.
	f := newUpgradeFixture(t)
	evil := []byte("evil binary")
	sum := sha256.Sum256(evil)
	tampered := []byte(hex.EncodeToString(sum[:]) + "  " + f.release.name + "\n")
	for _, srv := range []*fileServer{f.server, f.github} {
		srv.set(sumsFile, tampered)
		srv.set(f.release.name, evil)
	}
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "signature over SHA256SUMS doesn't verify")
}

func TestUpgradeRefusesAnotherKeysSignature(t *testing.T) {
	// A whole release signed by someone else's key.
	f := newUpgradeFixture(t)
	other := newTestRelease(t, testNewVersion)
	for _, srv := range []*fileServer{f.server, f.github} {
		srv.setAll(other.files())
	}
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "signature over SHA256SUMS doesn't verify")
}

func TestUpgradeRefusesAMalformedSignature(t *testing.T) {
	f := newUpgradeFixture(t)
	for _, srv := range []*fileServer{f.server, f.github} {
		srv.set(sigFile, []byte("not a signature"))
	}
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "not an Ed25519 signature")
}

func TestUpgradeRefusesABinaryOverTheSizeCap(t *testing.T) {
	f := newUpgradeFixture(t)
	f.u.maxBinary = int64(len(f.release.binary) - 1)
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "larger than")
}

func TestUpgradeRefusesASumsFileWithoutThisPlatform(t *testing.T) {
	f := newUpgradeFixture(t)
	f.u.goarch = "arm64"
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "no line for perchtail-agent-0.7.1-linux-arm64")
}

func TestTheServerSourceSendsTheAgentToken(t *testing.T) {
	f := newUpgradeFixture(t)
	f.u.sources = func(string) []binarySource {
		return []binarySource{{label: "the PerchTail server", base: f.server.URL + "/agent/binaries/", token: "wrong"}}
	}
	_, err := f.perform(t)
	assertUpgradeRefused(t, f, err, "401")
}

func TestAFailedRestartPutsTheOldBinaryBack(t *testing.T) {
	f := newUpgradeFixture(t)
	f.execErr = errors.New("exec format error")
	_, err := f.perform(t)
	if err == nil || !strings.Contains(err.Error(), "previous binary is back") {
		t.Fatalf("error = %v", err)
	}
	if got := readFile(t, f.exe); got != "old agent binary" {
		t.Fatalf("binary = %q, want the old one restored", got)
	}
}

func TestOnWindowsTheUpgradeAsksRunToExit(t *testing.T) {
	f := newUpgradeFixture(t)
	f.u.exec = nil
	f.u.goos = "windows"
	f.release.goos = "windows"
	f.release.name = binaryName(testNewVersion, "windows", "amd64")
	f.release.resign(f.release.binary)
	f.server.setAll(f.release.files())
	if _, err := f.perform(t); err != nil {
		t.Fatalf("perform: %v", err)
	}
	if !f.u.restartRequested() {
		t.Fatal("no restart was requested")
	}
	if got := readFile(t, f.exe); got != string(f.release.binary) {
		t.Fatalf("binary = %q, want the new one", got)
	}
	if got := readFile(t, f.exe+".old"); got != "old agent binary" {
		t.Fatalf("backup = %q", got)
	}
}

func TestInstallBinaryReplacesAndKeepsABackup(t *testing.T) {
	for _, goos := range []string{"linux", "windows"} {
		t.Run(goos, func(t *testing.T) {
			dir := t.TempDir()
			exe := filepath.Join(dir, "agent")
			newPath := filepath.Join(dir, "agent.new")
			_ = os.WriteFile(exe, []byte("old"), 0o755)
			_ = os.WriteFile(exe+".old", []byte("older"), 0o755) // a previous upgrade's backup
			_ = os.WriteFile(newPath, []byte("new"), 0o755)

			backup, err := installBinary(exe, newPath, goos)
			if err != nil {
				t.Fatalf("installBinary: %v", err)
			}
			if readFile(t, exe) != "new" || readFile(t, backup) != "old" {
				t.Fatalf("exe = %q, backup = %q", readFile(t, exe), readFile(t, backup))
			}
			if _, err := os.Stat(newPath); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("the new file is still there: %v", err)
			}
			if err := restoreBinary(exe, backup); err != nil {
				t.Fatalf("restoreBinary: %v", err)
			}
			if readFile(t, exe) != "old" {
				t.Fatalf("exe = %q after the rollback", readFile(t, exe))
			}
		})
	}
}

func TestInstallBinaryLeavesTheOldOneWhenTheNewOneIsMissing(t *testing.T) {
	for _, goos := range []string{"linux", "windows"} {
		t.Run(goos, func(t *testing.T) {
			dir := t.TempDir()
			exe := filepath.Join(dir, "agent")
			_ = os.WriteFile(exe, []byte("old"), 0o755)
			if _, err := installBinary(exe, filepath.Join(dir, "missing"), goos); err == nil {
				t.Fatal("installBinary succeeded without a new binary")
			}
			if readFile(t, exe) != "old" {
				t.Fatalf("exe = %q", readFile(t, exe))
			}
		})
	}
}

func TestPrecheck(t *testing.T) {
	release := newTestRelease(t, testNewVersion)
	cases := []struct {
		name    string
		current string
		target  string
		allowed bool
		key     []byte
		want    string
	}{
		{"newer release", "0.7.0", "0.7.1", true, release.pubPEM, ""},
		{"newer release candidate", "0.7.0", "0.8.0-rc.1", true, release.pubPEM, ""},
		{"final after its candidate", "0.7.1-rc.2", "0.7.1", true, release.pubPEM, ""},
		{"host opted out", "0.7.0", "0.7.1", false, release.pubPEM, "PERCHTAIL_REMOTE_UPGRADE=false"},
		{"no embedded key", "0.7.0", "0.7.1", true, nil, "built without the release signing key"},
		{"blank embedded key", "0.7.0", "0.7.1", true, []byte("\n"), "built without the release signing key"},
		{"garbage key", "0.7.0", "0.7.1", true, []byte("not pem"), "not a PEM public key"},
		{"same version", "0.7.1", "0.7.1", true, release.pubPEM, "not newer"},
		{"older version", "0.7.1", "0.6.0", true, release.pubPEM, "not newer"},
		{"candidate of the same release", "0.7.1", "0.7.1-rc.3", true, release.pubPEM, "not newer"},
		{"dev build", "dev", "0.7.1", true, release.pubPEM, "is not a release"},
		{"not a version", "0.7.0", "../../evil", true, release.pubPEM, "not a PerchTail release version"},
		{"tag form", "0.7.0", "v0.7.1", true, release.pubPEM, "not a PerchTail release version"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u := &upgrader{current: tc.current, allowed: tc.allowed, signingKeyPEM: tc.key}
			err := u.precheck(tc.target)
			if tc.want == "" {
				if err != nil {
					t.Fatalf("precheck: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("precheck error = %v, want it to mention %q", err, tc.want)
			}
		})
	}
}

func TestTheCommittedSigningKeyIsEmptyOrEd25519(t *testing.T) {
	// The repository ships the release key's public half, or nothing until
	// the maintainer adds it; anything else would break every upgrade.
	_, err := parseSigningKey(upgradeSigningKeyPEM)
	if err != nil && !errors.Is(err, errNoSigningKey) {
		t.Fatalf("upgrade_signing_key.pem: %v", err)
	}
}

func TestExpectedHash(t *testing.T) {
	sum := strings.Repeat("ab", 32)
	if _, err := expectedHash([]byte(sum+"  a\n"+sum+" *a\n"), "a"); err == nil {
		t.Fatal("a file listed twice must be refused")
	}
	if _, err := expectedHash([]byte("xyz  a\n"), "a"); err == nil {
		t.Fatal("a malformed hash must be refused")
	}
	got, err := expectedHash([]byte(sum+" *a\r\n"), "a")
	if err != nil || hex.EncodeToString(got) != sum {
		t.Fatalf("expectedHash = %x, %v", got, err)
	}
}

func TestServerBinariesURL(t *testing.T) {
	cases := map[string]string{
		"wss://perchtail.example.com/agent/connect":          "https://perchtail.example.com/agent/binaries/",
		"wss://example.com:8443/perchtail/agent/connect?x=1": "https://example.com:8443/perchtail/agent/binaries/",
		"ws://10.0.0.5:8080/agent/connect":                   "http://10.0.0.5:8080/agent/binaries/",
		"wss://example.com":                                  "https://example.com/binaries/",
		"https://example.com/agent/connect":                  "",
		"not a url":                                          "",
	}
	for in, want := range cases {
		if got := serverBinariesURL(in); got != want {
			t.Errorf("serverBinariesURL(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestVersionComparison(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"0.7.1", "0.7.0", true},
		{"0.10.0", "0.9.9", true},
		{"1.0.0", "0.99.99", true},
		{"0.7.0", "0.7.0-rc.4", true},
		{"0.7.0-rc.2", "0.7.0-rc.1", true},
		{"0.7.0-rc.1", "0.7.0", false},
		{"0.7.0", "0.7.0", false},
		{"0.6.0", "0.7.0", false},
		{"0.7.1", "dev", false},
		{"dev", "0.7.0", false},
		{"0.7.1-beta", "0.7.0", false},
	}
	for _, tc := range cases {
		if got := isNewerVersion(tc.a, tc.b); got != tc.want {
			t.Errorf("isNewerVersion(%q, %q) = %v, want %v", tc.a, tc.b, got, tc.want)
		}
	}
}

func TestHandleAnswersRefusalsAndFailures(t *testing.T) {
	f := newUpgradeFixture(t)
	var mu sync.Mutex
	var sent []any
	send := func(msg any) error {
		mu.Lock()
		defer mu.Unlock()
		sent = append(sent, msg)
		return nil
	}

	f.u.allowed = false
	f.u.handle(context.Background(), Command{Type: "upgrade", ID: "u1", Version: testNewVersion}, send, func() {})
	refusal, ok := sent[0].(ErrorResult)
	if !ok || refusal.Type != "upgrade_error" || refusal.Code != "refused" || refusal.ID != "u1" {
		t.Fatalf("sent = %#v, want a refusal", sent)
	}

	f.u.allowed = true
	sent = nil
	for _, srv := range []*fileServer{f.server, f.github} {
		srv.set(f.release.name, []byte("tampered"))
	}
	f.u.handle(context.Background(), Command{Type: "upgrade", ID: "u2", Version: testNewVersion}, send, func() {})
	if accepted, ok := sent[0].(UpgradeAccepted); !ok || accepted.From != testOldVersion || accepted.To != testNewVersion {
		t.Fatalf("first message = %#v, want upgrade_accepted", sent[0])
	}
	failed, ok := sent[len(sent)-1].(UpgradeFailed)
	if !ok || failed.Type != "upgrade_failed" || !strings.Contains(failed.Error, "SHA-256") {
		t.Fatalf("last message = %#v, want upgrade_failed", sent[len(sent)-1])
	}
	if readFile(t, f.exe) != "old agent binary" {
		t.Fatal("the old binary must stay in place")
	}
}

func TestHandleRefusesASecondUpgradeWhileOneRuns(t *testing.T) {
	f := newUpgradeFixture(t)
	f.u.running.Store(true)
	var sent []any
	f.u.handle(context.Background(), Command{Type: "upgrade", ID: "u3", Version: testNewVersion},
		func(msg any) error { sent = append(sent, msg); return nil }, func() {})
	if r, ok := sent[0].(ErrorResult); !ok || r.Code != "busy" {
		t.Fatalf("sent = %#v, want busy", sent)
	}
}
