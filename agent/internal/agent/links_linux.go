//go:build linux

package agent

import (
	"os"
	"strconv"
)

// finalPath is where the open file f really is, every link resolved: the
// kernel's own answer for the descriptor, so it is the file that was
// opened. Without /proc, errNoFinalPath.
func finalPath(f *os.File) (string, error) {
	real, err := os.Readlink("/proc/self/fd/" + strconv.FormatUint(uint64(f.Fd()), 10))
	if err != nil {
		if _, statErr := os.Stat("/proc/self/fd"); statErr != nil {
			return "", errNoFinalPath
		}
		return "", err
	}
	return real, nil
}
