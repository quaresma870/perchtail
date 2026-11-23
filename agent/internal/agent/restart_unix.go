//go:build !windows

package agent

import (
	"os"
	"syscall"
)

// execSelf replaces this process with the upgraded binary (#319): same PID,
// same arguments and environment, so systemd (or any supervisor) keeps
// tracking it as the same service. It only returns on failure.
func execSelf(exe string) error {
	// exe is the agent's own path, where a binary whose signature and hash
	// were just verified was installed (upgrade.go).
	return syscall.Exec(exe, os.Args, os.Environ()) // #nosec G204 G702 -- see above
}

// restartByExec: on Linux the agent restarts itself in place.
const restartByExec = true
