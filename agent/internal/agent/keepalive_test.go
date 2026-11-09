package agent

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// #333: reconnect timing and dead-connection detection.

func TestReconnectDelayStaysWithinBoundsAndJitters(t *testing.T) {
	backoff := initialBackoff
	for step := 0; step < 10; step++ {
		low := reconnectDelay(backoff, func() float64 { return 0 })
		high := reconnectDelay(backoff, func() float64 { return 0.999999 })
		for _, d := range []time.Duration{low, high} {
			if d < initialBackoff || d > maxBackoff {
				t.Fatalf("backoff %s: delay %s is outside [%s, %s]", backoff, d, initialBackoff, maxBackoff)
			}
		}
		if high <= low {
			t.Fatalf("backoff %s: no jitter (%s to %s)", backoff, low, high)
		}
		// Within half to one and a half times the backoff, clamped.
		if low > backoff || high < backoff/2 {
			t.Fatalf("backoff %s: delay range %s to %s doesn't surround it", backoff, low, high)
		}
		backoff = nextBackoff(backoff)
	}
	if backoff != maxBackoff {
		t.Fatalf("backoff after 10 doublings = %s, want the %s cap", backoff, maxBackoff)
	}
}

func TestTheFirstReconnectIsSpreadOut(t *testing.T) {
	// After a server restart every agent is at the first step: their
	// delays must differ, not all be exactly one second.
	seen := map[time.Duration]bool{}
	for _, r := range []float64{0, 0.25, 0.5, 0.75, 0.99} {
		seen[reconnectDelay(initialBackoff, func() float64 { return r })] = true
	}
	if len(seen) < 5 {
		t.Fatalf("first reconnect delays = %v, want them spread out", seen)
	}
}

func TestBackoffResetsAfterAStableConnection(t *testing.T) {
	if got := backoffAfter(16*time.Second, 2*time.Minute); got != initialBackoff {
		t.Fatalf("after a 2-minute connection: backoff %s, want %s", got, initialBackoff)
	}
	if got := backoffAfter(16*time.Second, 5*time.Second); got != 16*time.Second {
		t.Fatalf("after a 5-second connection: backoff %s, want it kept", got)
	}
	if got := backoffAfter(8*time.Second, 0); got != 8*time.Second {
		t.Fatalf("after a failed dial: backoff %s, want it kept", got)
	}
}

func shortKeepalive(t *testing.T, interval, wait time.Duration) {
	t.Helper()
	oldInterval, oldWait := pingInterval, pongWait
	pingInterval, pongWait = interval, wait
	t.Cleanup(func() { pingInterval, pongWait = oldInterval, oldWait })
}

func wsURL(server *httptest.Server) string {
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func TestASilentServerIsDetectedAsDead(t *testing.T) {
	// A server that accepts and then goes quiet -- never reads, so it never
	// answers a ping -- as a connection dropped by a NAT looks.
	shortKeepalive(t, 50*time.Millisecond, 300*time.Millisecond)
	release := make(chan struct{})
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		<-release
	}))
	defer server.Close()
	defer close(release)

	cfg := Config{ServerURL: wsURL(server), Token: "t"}
	result := make(chan error, 1)
	start := time.Now()
	go func() {
		_, err := connectAndServe(context.Background(), cfg, newUpgrader(cfg))
		result <- err
	}()
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("connectAndServe returned no error for a dead connection")
		}
		if elapsed := time.Since(start); elapsed < 250*time.Millisecond {
			t.Fatalf("gave up after %s, before the %s read deadline", elapsed, pongWait)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("a silent server wasn't noticed")
	}
}

func TestAServerThatAnswersPingsKeepsTheConnection(t *testing.T) {
	shortKeepalive(t, 50*time.Millisecond, 200*time.Millisecond)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		// Reading is what answers pings (gorilla's default ping handler),
		// as uvicorn does on the real server; nothing else is sent.
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cfg := Config{ServerURL: wsURL(server), Token: "t"}
	result := make(chan error, 1)
	go func() {
		_, err := connectAndServe(ctx, cfg, newUpgrader(cfg))
		result <- err
	}()
	select {
	case err := <-result:
		t.Fatalf("the connection ended (%v) although the server answered every ping", err)
	case <-time.After(800 * time.Millisecond):
	}
	cancel()
	select {
	case <-result:
	case <-time.After(3 * time.Second):
		t.Fatal("connectAndServe didn't stop")
	}
}

func TestTheAgentAnswersTheServersPing(t *testing.T) {
	upgrader := websocket.Upgrader{}
	got := make(chan Pong, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		if err := conn.WriteJSON(Command{Type: "ping", ID: "p1"}); err != nil {
			return
		}
		var pong Pong
		if err := conn.ReadJSON(&pong); err == nil {
			got <- pong
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := Config{ServerURL: wsURL(server), Token: "t"}
	go func() { _, _ = connectAndServe(ctx, cfg, newUpgrader(cfg)) }()
	select {
	case pong := <-got:
		if pong.Type != "pong" || pong.ID != "p1" {
			t.Fatalf("answer = %+v, want pong p1", pong)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no pong")
	}
}

func TestAnUpgradeCommandIsAnsweredOverTheConnection(t *testing.T) {
	// The real dispatch path (#319): a "dev" build refuses, with a reason
	// the server can show.
	upgrader := websocket.Upgrader{}
	got := make(chan ErrorResult, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		if err := conn.WriteJSON(Command{Type: "upgrade", ID: "u1", Version: "0.7.1"}); err != nil {
			return
		}
		var answer ErrorResult
		if err := conn.ReadJSON(&answer); err == nil {
			got <- answer
		}
	}))
	defer server.Close()

	previous := Version
	Version = "dev"
	defer func() { Version = previous }()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := Config{ServerURL: wsURL(server), Token: "t", RemoteUpgrade: true}
	go func() { _, _ = connectAndServe(ctx, cfg, newUpgrader(cfg)) }()
	select {
	case answer := <-got:
		if answer.Type != "upgrade_error" || answer.ID != "u1" || answer.Code != "refused" {
			t.Fatalf("answer = %+v, want a refusal", answer)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no answer")
	}
}

func TestRunReturnsErrRestartOnceAnUpgradeAsksForIt(t *testing.T) {
	// Windows: the upgrade is installed and the agent must exit for its
	// service manager to start the new binary.
	server := httptest.NewServer(http.NotFoundHandler()) // every dial fails
	defer server.Close()
	cfg := Config{ServerURL: wsURL(server), Token: "t"}
	u := newUpgrader(cfg)
	result := make(chan error, 1)
	go func() { result <- run(context.Background(), cfg, u, func() float64 { return 0 }) }()
	time.Sleep(100 * time.Millisecond) // waiting out its first backoff
	u.requestRestart()
	select {
	case err := <-result:
		if err != ErrRestart {
			t.Fatalf("run returned %v, want ErrRestart", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("run didn't return")
	}
}
