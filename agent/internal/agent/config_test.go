package agent

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func fakeGetenv(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

// validEnv is a complete configuration whose base path is a real, readable
// folder; each test overrides what it's about.
func validEnv(t *testing.T, overrides map[string]string) func(string) string {
	t.Helper()
	values := map[string]string{
		"PERCHTAIL_SERVER_URL":  "wss://example.com/agent/connect",
		"PERCHTAIL_AGENT_TOKEN": "s3cret",
		"PERCHTAIL_BASE_PATH":   t.TempDir(),
	}
	for key, value := range overrides {
		values[key] = value
	}
	return fakeGetenv(values)
}

func TestLoadConfigSucceedsWhenAllSet(t *testing.T) {
	base := t.TempDir()
	getenv := fakeGetenv(map[string]string{
		"PERCHTAIL_SERVER_URL":  "wss://example.com/agent/connect",
		"PERCHTAIL_AGENT_TOKEN": "s3cret",
		"PERCHTAIL_BASE_PATH":   base,
	})

	cfg, err := LoadConfig(getenv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.ServerURL != "wss://example.com/agent/connect" {
		t.Errorf("ServerURL = %q", cfg.ServerURL)
	}
	if cfg.Token != "s3cret" {
		t.Errorf("Token = %q", cfg.Token)
	}
	if cfg.BasePath != base {
		t.Errorf("BasePath = %q", cfg.BasePath)
	}
}

func TestLoadConfigRequiresServerURL(t *testing.T) {
	getenv := validEnv(t, map[string]string{"PERCHTAIL_SERVER_URL": ""})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_SERVER_URL is missing")
	}
}

func TestLoadConfigRequiresToken(t *testing.T) {
	getenv := validEnv(t, map[string]string{"PERCHTAIL_AGENT_TOKEN": ""})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_AGENT_TOKEN is missing")
	}
}

func TestLoadConfigRequiresBasePath(t *testing.T) {
	getenv := validEnv(t, map[string]string{"PERCHTAIL_BASE_PATH": ""})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_BASE_PATH is missing")
	}
}

func TestLoadConfigRefusesABasePathThatCantBeServed(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "app.log")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name string
		path string
		want string
	}{
		{"missing", filepath.Join(dir, "no-such-folder"), "doesn't exist"},
		{"a file", file, "is not a folder"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := LoadConfig(validEnv(t, map[string]string{"PERCHTAIL_BASE_PATH": tc.path}))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("LoadConfig(%q) error = %v, want one containing %q", tc.path, err, tc.want)
			}
		})
	}
}

func TestLoadConfigRefusesAnUnreadableBasePath(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("folder permissions are ACLs on Windows")
	}
	if os.Geteuid() == 0 {
		t.Skip("root reads any folder")
	}
	dir := filepath.Join(t.TempDir(), "locked")
	if err := os.Mkdir(dir, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })

	_, err := LoadConfig(validEnv(t, map[string]string{"PERCHTAIL_BASE_PATH": dir}))
	if err == nil || !strings.Contains(err.Error(), "not readable") {
		t.Fatalf("LoadConfig(%q) error = %v, want a not-readable error", dir, err)
	}
}

func TestLoadConfigAcceptsAnEmptyBasePath(t *testing.T) {
	if _, err := LoadConfig(validEnv(t, nil)); err != nil {
		t.Fatalf("an empty folder is a valid base path: %v", err)
	}
}

func TestLoadConfigServerURLScheme(t *testing.T) {
	cases := []struct {
		name     string
		url      string
		insecure string
		wantErr  bool
	}{
		{"wss accepted", "wss://example.com/agent/connect", "", false},
		{"ws rejected by default", "ws://example.com/agent/connect", "", true},
		{"ws rejected when opt-in is false", "ws://example.com/agent/connect", "false", true},
		{"ws accepted with explicit opt-in", "ws://example.com/agent/connect", "true", false},
		{"https rejected", "https://example.com/agent/connect", "true", true},
		{"no host rejected", "wss:///agent/connect", "", true},
		{"garbage rejected", "not a url", "true", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			getenv := validEnv(t, map[string]string{
				"PERCHTAIL_SERVER_URL":     tc.url,
				"PERCHTAIL_ALLOW_INSECURE": tc.insecure,
			})
			_, err := LoadConfig(getenv)
			if (err != nil) != tc.wantErr {
				t.Fatalf("LoadConfig(%q, insecure=%q) error = %v, wantErr %v", tc.url, tc.insecure, err, tc.wantErr)
			}
		})
	}
}

func TestLoadConfigRemoteUpgrade(t *testing.T) {
	// #319: the host can always refuse upgrades from the portal.
	cases := []struct {
		value   string
		want    bool
		wantErr bool
	}{
		{"", true, false},
		{"true", true, false},
		{"1", true, false},
		{"false", false, false},
		{"FALSE", false, false},
		{"0", false, false},
		{"off", false, false},
		{"flase", false, true},
	}
	for _, tc := range cases {
		getenv := validEnv(t, map[string]string{"PERCHTAIL_REMOTE_UPGRADE": tc.value})
		cfg, err := LoadConfig(getenv)
		if (err != nil) != tc.wantErr {
			t.Fatalf("PERCHTAIL_REMOTE_UPGRADE=%q: error = %v, wantErr %v", tc.value, err, tc.wantErr)
		}
		if err == nil && cfg.RemoteUpgrade != tc.want {
			t.Fatalf("PERCHTAIL_REMOTE_UPGRADE=%q: RemoteUpgrade = %v, want %v", tc.value, cfg.RemoteUpgrade, tc.want)
		}
	}
}
