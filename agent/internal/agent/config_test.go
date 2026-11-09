package agent

import "testing"

func fakeGetenv(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func TestLoadConfigSucceedsWhenAllSet(t *testing.T) {
	getenv := fakeGetenv(map[string]string{
		"PERCHTAIL_SERVER_URL":  "wss://example.com/agent/connect",
		"PERCHTAIL_AGENT_TOKEN": "s3cret",
		"PERCHTAIL_BASE_PATH":   "/var/log/app",
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
	if cfg.BasePath != "/var/log/app" {
		t.Errorf("BasePath = %q", cfg.BasePath)
	}
}

func TestLoadConfigRequiresServerURL(t *testing.T) {
	getenv := fakeGetenv(map[string]string{
		"PERCHTAIL_AGENT_TOKEN": "s3cret",
		"PERCHTAIL_BASE_PATH":   "/var/log/app",
	})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_SERVER_URL is missing")
	}
}

func TestLoadConfigRequiresToken(t *testing.T) {
	getenv := fakeGetenv(map[string]string{
		"PERCHTAIL_SERVER_URL": "wss://example.com/agent/connect",
		"PERCHTAIL_BASE_PATH":  "/var/log/app",
	})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_AGENT_TOKEN is missing")
	}
}

func TestLoadConfigRequiresBasePath(t *testing.T) {
	getenv := fakeGetenv(map[string]string{
		"PERCHTAIL_SERVER_URL":  "wss://example.com/agent/connect",
		"PERCHTAIL_AGENT_TOKEN": "s3cret",
	})
	if _, err := LoadConfig(getenv); err == nil {
		t.Fatal("expected an error when PERCHTAIL_BASE_PATH is missing")
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
			getenv := fakeGetenv(map[string]string{
				"PERCHTAIL_SERVER_URL":     tc.url,
				"PERCHTAIL_AGENT_TOKEN":    "s3cret",
				"PERCHTAIL_BASE_PATH":      "/var/log/app",
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
		getenv := fakeGetenv(map[string]string{
			"PERCHTAIL_SERVER_URL":     "wss://example.com/agent/connect",
			"PERCHTAIL_AGENT_TOKEN":    "s3cret",
			"PERCHTAIL_BASE_PATH":      "/var/log/app",
			"PERCHTAIL_REMOTE_UPGRADE": tc.value,
		})
		cfg, err := LoadConfig(getenv)
		if (err != nil) != tc.wantErr {
			t.Fatalf("PERCHTAIL_REMOTE_UPGRADE=%q: error = %v, wantErr %v", tc.value, err, tc.wantErr)
		}
		if err == nil && cfg.RemoteUpgrade != tc.want {
			t.Fatalf("PERCHTAIL_REMOTE_UPGRADE=%q: RemoteUpgrade = %v, want %v", tc.value, cfg.RemoteUpgrade, tc.want)
		}
	}
}
