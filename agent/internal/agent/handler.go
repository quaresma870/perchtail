package agent

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
)

// ErrNotADirectory is returned when a list targets a file; the backend
// answers 404 for it, as for SMB and WinRM (#206).
var ErrNotADirectory = errors.New("not a directory")

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
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	dir, err := root.Open(rootName(relPath))
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	if st, err := dir.Stat(); err != nil {
		return nil, err
	} else if !st.IsDir() {
		return nil, ErrNotADirectory
	}
	entries, err := dir.ReadDir(-1)
	if err != nil {
		return nil, err
	}

	result := make([]DirEntryWire, 0, len(entries))
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			return nil, err
		}
		isDir := entry.IsDir()
		// Symlinks, and junctions on Windows (reported as irregular), are
		// listed only when they resolve inside basePath, with their
		// target's type and size. One that leads out (escape -> /etc), is
		// broken or can't be read is left out: opening it would be refused
		// by os.Root anyway (#206), as SSH and local sources already do.
		if entry.Type()&(fs.ModeSymlink|fs.ModeIrregular) != 0 {
			target, err := root.Stat(path.Join(rootName(relPath), entry.Name()))
			if err != nil {
				continue
			}
			info, isDir = target, target.IsDir()
		}
		size := info.Size()
		if isDir {
			size = 0
		}
		result = append(result, DirEntryWire{Name: entry.Name(), IsDir: isDir, Size: size})
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
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	f, err := root.Open(rootName(relPath))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(f)
}

// rootName turns a backend-supplied relative path into a name for os.Root,
// which resolves it -- symlinks included -- without ever leaving basePath.
// IsSafeRelativePath stays as the first, lexical check; os.Root is what
// stops a symlink under basePath from pointing a read anywhere else.
func rootName(relPath string) string {
	if relPath == "" {
		return "."
	}
	return path.Clean(relPath)
}
