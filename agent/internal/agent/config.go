package agent

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// allowInsecureEnv opts in to a plain ws:// server URL. Off by default: the
// enrollment token travels in the connection's Authorization header and
// every fetched file comes back over this connection, so an unencrypted
// link would expose both to anyone on the path.
const allowInsecureEnv = "PERCHTAIL_ALLOW_INSECURE"

// remoteUpgradeEnv is the host's say over upgrades from the portal (#319):
// "false" refuses every upgrade command, whatever the server asks. Unset
// allows them (the server has its own switch, off by default).
const remoteUpgradeEnv = "PERCHTAIL_REMOTE_UPGRADE"

// Config holds everything the agent needs to dial out to a PerchTail
// connector and answer live list/fetch commands against a local directory
// tree. Per ROADMAP.md's Phase 2 design, the agent only ever dials out and
// waits — nothing is synced ahead of time, so there is nothing here beyond
// where to connect, how to authenticate, and which local root to serve.
type Config struct {
	ServerURL string // e.g. wss://perchtail.example.com/agent/connect
	Token     string // enrollment token from POST /sources/{id}/agent-token
	BasePath  string // local root the agent is allowed to read from
	// Whether the host allows upgrades from the portal (#319); see
	// remoteUpgradeEnv.
	RemoteUpgrade bool
}

// LoadConfig reads the three required settings and the optional ones via getenv (os.Getenv in
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
	if err := checkServerURL(cfg.ServerURL, isTruthy(getenv(allowInsecureEnv))); err != nil {
		return cfg, err
	}
	// A typo must not silently leave upgrades on for a host that meant to
	// turn them off, so anything but a clear yes or no stops the agent.
	switch raw := getenv(remoteUpgradeEnv); {
	case strings.TrimSpace(raw) == "" || isTruthy(raw):
		cfg.RemoteUpgrade = true
	case isFalsy(raw):
		cfg.RemoteUpgrade = false
	default:
		return cfg, fmt.Errorf("%s must be true or false (got %q)", remoteUpgradeEnv, raw)
	}
	return cfg, nil
}

func checkServerURL(raw string, allowInsecure bool) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("PERCHTAIL_SERVER_URL %q is not a valid URL", raw)
	}
	switch strings.ToLower(u.Scheme) {
	case "wss":
		return nil
	case "ws":
		if allowInsecure {
			return nil
		}
		return fmt.Errorf(
			"PERCHTAIL_SERVER_URL uses unencrypted ws://, which would send the agent token "+
				"and every fetched file in clear text; use wss://, or set %s=true "+
				"for a trusted network (e.g. testing)", allowInsecureEnv)
	default:
		return fmt.Errorf("PERCHTAIL_SERVER_URL must start with wss:// (got %q)", u.Scheme)
	}
}

func isTruthy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func isFalsy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "0", "false", "no", "off":
		return true
	}
	return false
}
