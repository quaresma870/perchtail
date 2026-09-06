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
