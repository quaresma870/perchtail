// Command perchtail-agent is the Phase 2 push-agent (see ROADMAP.md): a
// small binary that dials out to a PerchTail server and holds a persistent
// connection open, answering live list/fetch commands against a local
// directory tree, for hosts that can't be reached inbound over
// SSH/SMB/WinRM. It never pushes files on its own initiative.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/quaresma870/perchtail/agent/internal/agent"
)

func main() {
	cfg, err := agent.LoadConfig(os.Getenv)
	if err != nil {
		log.Fatalf("agent: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := agent.Run(ctx, cfg); err != nil {
		log.Fatalf("agent: %v", err)
	}
}
