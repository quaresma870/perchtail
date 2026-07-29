package agent

import "errors"

// Config holds everything the agent needs to dial out to a PerchTail
// connector and answer live list/fetch commands against a local directory
// tree. Per ROADMAP.md's Phase 2 design, the agent only ever dials out and
// waits — nothing is synced ahead of time, so there is nothing here beyond
// where to connect, how to authenticate, and which local root to serve.
type Config struct {
	ServerURL string // e.g. wss://perchtail.example.com/agent/connect
	Token     string // enrollment token from POST /sources/{id}/agent-token
	BasePath  string // local root the agent is allowed to read from
}

// LoadConfig reads the three required settings via getenv (os.Getenv in
// production; a fake lookup in tests, so no env-var pollution is needed to
// exercise the validation paths).
func LoadConfig(getenv func(string) string) (Config, error) {
	cfg := Config{
		ServerURL: getenv("PERCHTAIL_SERVER_URL"),
		Token:     getenv("PERCHTAIL_AGENT_TOKEN"),
		BasePath:  getenv("PERCHTAIL_BASE_PATH"),
	}
	switch {
	case cfg.ServerURL == "":
		return cfg, errors.New("PERCHTAIL_SERVER_URL is required")
	case cfg.Token == "":
		return cfg, errors.New("PERCHTAIL_AGENT_TOKEN is required")
	case cfg.BasePath == "":
		return cfg, errors.New("PERCHTAIL_BASE_PATH is required")
	}
	return cfg, nil
}
