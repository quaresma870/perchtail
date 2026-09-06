package agent

import "strings"

// IsSafeRelativePath mirrors backend/app/rules.py's is_safe_relative_path —
// the same independent guard against path traversal, absolute paths, and
// Windows drive letters, checked here too before a backend-supplied path
// ever touches this agent's local filesystem. The backend already validates
// paths on its side, but a compromised or buggy connector is exactly the
// scenario this second, independent check exists for.
func IsSafeRelativePath(path string) bool {
	if strings.HasPrefix(path, "/") || strings.HasPrefix(path, "\\") || strings.Contains(path, ":") {
		return false
	}
	for _, segment := range strings.FieldsFunc(path, func(r rune) bool { return r == '/' || r == '\\' }) {
		if segment == ".." {
			return false
		}
	}
	return true
}
