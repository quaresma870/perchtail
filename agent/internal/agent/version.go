package agent

import (
	"regexp"
	"strconv"
)

// A PerchTail version: X.Y.Z, or X.Y.Z-rc.N for a release candidate -- the
// only two forms the release workflow produces, and the same rule as the
// server's app/versions.py. Anything else ("dev", a local build) is
// unknown, and no upgrade is made on a guess.
var versionPattern = regexp.MustCompile(`^(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-rc\.(\d{1,4}))?$`)

// A final release sorts after all of its release candidates.
const finalRelease = 1 << 30

// parseVersion returns (major, minor, patch, rc) for a PerchTail version,
// or false when v isn't one.
func parseVersion(v string) ([4]int, bool) {
	m := versionPattern.FindStringSubmatch(v)
	if m == nil {
		return [4]int{}, false
	}
	var parts [4]int
	for i := 0; i < 3; i++ {
		parts[i], _ = strconv.Atoi(m[i+1])
	}
	parts[3] = finalRelease
	if m[4] != "" {
		parts[3], _ = strconv.Atoi(m[4])
	}
	return parts, true
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
