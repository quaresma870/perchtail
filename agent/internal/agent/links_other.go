//go:build !linux && !windows

package agent

import "os"

// finalPath isn't available here: realPaths resolves the path itself and
// checks it is the file that was opened.
func finalPath(*os.File) (string, error) {
	return "", errNoFinalPath
}
