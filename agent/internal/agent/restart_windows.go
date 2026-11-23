//go:build windows

package agent

import "errors"

// execSelf isn't possible on Windows: there the agent exits with
// ExitCodeRestart and the service manager starts the new binary (#319).
func execSelf(string) error {
	return errors.New("restarting in place is not supported on Windows")
}

// restartByExec: on Windows the service manager restarts the agent.
const restartByExec = false
