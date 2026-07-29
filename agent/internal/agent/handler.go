package agent

import (
	"fmt"
	"os"
	"path/filepath"
)

// HandleList lists the immediate children of relPath under basePath. Rule
// filtering (which files are actually visible per the source's rule chain)
// happens on the backend side — collectors/agent.py's list_directory
// applies is_visible to what this returns — so this just reports everything
// under the requested directory, unfiltered, same division of labor as
// every other connector's live-listing call.
func HandleList(basePath, relPath string) ([]DirEntryWire, error) {
	if !IsSafeRelativePath(relPath) {
		return nil, fmt.Errorf("unsafe path: %q", relPath)
	}
	dir := filepath.Join(basePath, filepath.FromSlash(relPath))
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}

	result := make([]DirEntryWire, 0, len(entries))
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			return nil, err
		}
		size := info.Size()
		if entry.IsDir() {
			size = 0
		}
		result = append(result, DirEntryWire{Name: entry.Name(), IsDir: entry.IsDir(), Size: size})
	}
	return result, nil
}

// HandleFetch reads relPath's full content off local disk under basePath —
// the agent's half of collectors/agent.py's fetch_file. Base64-encoding
// happens in the caller (handleCommand), not here, so this stays a plain
// byte-reading function to unit test.
func HandleFetch(basePath, relPath string) ([]byte, error) {
	if !IsSafeRelativePath(relPath) {
		return nil, fmt.Errorf("unsafe path: %q", relPath)
	}
	path := filepath.Join(basePath, filepath.FromSlash(relPath))
	return os.ReadFile(path)
}
