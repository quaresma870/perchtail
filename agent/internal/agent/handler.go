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

// Chunk sizes the agent accepts from the backend: small enough to keep
// every message well under the backend's WebSocket message limit (16 MiB
// in uvicorn by default), large enough not to waste time on overhead. The
// maximum is what the backend asks for (FETCH_CHUNK_BYTES).
const (
	minChunkSize = 4 << 10
	maxChunkSize = 1 << 20
)

// TooLargeError refuses a streamed fetch over the backend's limit.
type TooLargeError struct {
	Size  int64
	Limit int64
}

func (e *TooLargeError) Error() string {
	return fmt.Sprintf("file is %d bytes, over the %d-byte limit", e.Size, e.Limit)
}

// StreamFetch reads relPath under basePath in chunks of at most chunkSize
// bytes and passes each one to send, in order (#205). Only one chunk is
// held at a time, however large the file. maxBytes > 0 refuses a file
// larger than that: up front from its size, and again while reading,
// since a log can grow while it's being read. It returns the number of
// chunks and bytes sent. An error from send is returned as is.
func StreamFetch(
	basePath, relPath string, chunkSize, maxBytes int64, send func([]byte) error,
) (chunks int, size int64, err error) {
	if !IsSafeRelativePath(relPath) {
		return 0, 0, fmt.Errorf("unsafe path: %q", relPath)
	}
	chunkSize = min(max(chunkSize, minChunkSize), maxChunkSize)
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return 0, 0, err
	}
	defer root.Close()
	f, err := root.Open(rootName(relPath))
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return 0, 0, err
	}
	if st.IsDir() {
		return 0, 0, fmt.Errorf("%s: is a directory", relPath)
	}
	if maxBytes > 0 && st.Size() > maxBytes {
		return 0, 0, &TooLargeError{Size: st.Size(), Limit: maxBytes}
	}

	// A fixed-size allocation: chunkSize comes from the backend's message
	// and only picks how much of the buffer each read uses, so no value it
	// sends can make the agent allocate more.
	buf := make([]byte, maxChunkSize)[:chunkSize]
	for {
		n, readErr := io.ReadFull(f, buf)
		if n > 0 {
			size += int64(n)
			if maxBytes > 0 && size > maxBytes {
				return chunks, size, &TooLargeError{Size: size, Limit: maxBytes}
			}
			if err := send(buf[:n]); err != nil {
				return chunks, size, err
			}
			chunks++
		}
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			return chunks, size, nil
		}
		if readErr != nil {
			return chunks, size, readErr
		}
	}
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
