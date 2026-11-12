package agent

import (
	"fmt"
	"regexp"
	"strconv"
)

// A PerchTail version: X.Y.Z for a final release, X.Y.Z-rc.N for a release
// candidate or X.Y.Z-beta.N for an early pre-release taken from dev (a
// leading "v", as in tags, is accepted) -- the three forms the release
// workflow produces, and the same rule as the server's app/versions.py.
// They sort as X.Y.Z-beta.N < X.Y.Z-rc.N < X.Y.Z, numerically within each
// stage. Anything else ("dev", a local build) is unknown, and no upgrade is
// made on a guess.
var versionPattern = regexp.MustCompile(`^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-(beta|rc)\.(\d{1,4}))?$`)

// The stage of a version: a beta sorts before a release candidate, which
// sorts before the final release.
const (
	stageBeta = iota
	stageRC
	stageFinal
)

// parseVersion returns (major, minor, patch, stage, n) for a PerchTail
// version, with n 0 for a final release, or false when v isn't one.
func parseVersion(v string) ([5]int, bool) {
	m := versionPattern.FindStringSubmatch(v)
	if m == nil {
		return [5]int{}, false
	}
	var parts [5]int
	for i := 0; i < 3; i++ {
		parts[i], _ = strconv.Atoi(m[i+1])
	}
	switch m[4] {
	case "beta":
		parts[3] = stageBeta
	case "rc":
		parts[3] = stageRC
	default:
		parts[3] = stageFinal
		return parts, true
	}
	parts[4], _ = strconv.Atoi(m[5])
	return parts, true
}

// canonicalVersion rebuilds a PerchTail version from its parsed numbers, so
// what goes into a download URL, a file name or a log line is made here,
// not copied from the server's message. False when v isn't one.
func canonicalVersion(v string) (string, bool) {
	p, ok := parseVersion(v)
	if !ok {
		return "", false
	}
	switch p[3] {
	case stageBeta:
		return fmt.Sprintf("%d.%d.%d-beta.%d", p[0], p[1], p[2], p[4]), true
	case stageRC:
		return fmt.Sprintf("%d.%d.%d-rc.%d", p[0], p[1], p[2], p[4]), true
	default:
		return fmt.Sprintf("%d.%d.%d", p[0], p[1], p[2]), true
	}
}

// isNewerVersion reports whether candidate is a later release than current.
// False when either isn't a PerchTail version.
func isNewerVersion(candidate, current string) bool {
	a, okA := parseVersion(candidate)
	b, okB := parseVersion(current)
	if !okA || !okB {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return a[i] > b[i]
		}
	}
	return false
}
