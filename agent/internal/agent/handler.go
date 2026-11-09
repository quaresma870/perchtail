package agent

import (
	"encoding/json"
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

// Caps on one listing (#347), so a huge folder never makes a message over
// the backend's WebSocket limit (16 MiB in uvicorn by default), which
// failed every time: at most MaxListEntries entries, and no more than
// maxListBytes of them once JSON-encoded. A backend from 0.7 asks for its
// own maximum (Command.MaxEntries), never above MaxListEntries.
const (
	MaxListEntries = 50000
	// Entries read from the directory at a time, so a folder with millions
	// of files isn't read whole into memory first.
	listBatch = 1000
)

// A variable so tests can lower it.
var maxListBytes = 8 << 20

// HandleList lists every immediate child of relPath under basePath, up to
// the caps above. See ListDirectory.
func HandleList(basePath, relPath string) ([]DirEntryWire, error) {
	entries, _, err := ListDirectory(basePath, relPath, 0)
	return entries, err
}

// ListDirectory lists the immediate children of relPath under basePath.
// Rule filtering (which files are actually visible per the source's rule
// chain) happens on the backend side — collectors/agent.py's
// list_directory applies is_visible to what this returns — so this just
// reports everything under the requested directory, unfiltered, same
// division of labor as every other connector's live-listing call.
//
// It stops at maxEntries entries (0, or anything above MaxListEntries,
// means MaxListEntries) or once they would take more than maxListBytes,
// and then reports truncated, in the directory's own order (#347).
func ListDirectory(basePath, relPath string, maxEntries int) ([]DirEntryWire, bool, error) {
	if !IsSafeRelativePath(relPath) {
		return nil, false, fmt.Errorf("unsafe path: %q", relPath)
	}
	if maxEntries <= 0 || maxEntries > MaxListEntries {
		maxEntries = MaxListEntries
	}
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return nil, false, err
	}
	defer root.Close()
	dir, err := root.Open(rootName(relPath))
	if err != nil {
		return nil, false, err
	}
	defer dir.Close()
	if st, err := dir.Stat(); err != nil {
		return nil, false, err
	} else if !st.IsDir() {
		return nil, false, ErrNotADirectory
	}

	result := make([]DirEntryWire, 0)
	encoded := 0
	for {
		batch, readErr := dir.ReadDir(listBatch)
		for _, entry := range batch {
			wire, ok, err := listEntry(root, relPath, entry)
			if err != nil {
				return nil, false, err
			}
			if !ok {
				continue
			}
			size, err := wireSize(wire)
			if err != nil {
				return nil, false, err
			}
			if len(result) >= maxEntries || encoded+size > maxListBytes {
				return result, true, nil
			}
			encoded += size
			result = append(result, wire)
		}
		if errors.Is(readErr, io.EOF) || (readErr == nil && len(batch) == 0) {
			return result, false, nil
		}
		if readErr != nil {
			return nil, false, readErr
		}
	}
}

// wireSize is how many bytes the entry takes in the list_result message.
func wireSize(wire DirEntryWire) (int, error) {
	encoded, err := json.Marshal(wire)
	if err != nil {
		return 0, err
	}
	return len(encoded) + 1, nil // and a comma
}

// listEntry turns one directory entry into its wire form; ok is false for
// one that is left out.
func listEntry(root *os.Root, relPath string, entry fs.DirEntry) (DirEntryWire, bool, error) {
	info, err := entry.Info()
	if err != nil {
		return DirEntryWire{}, false, err
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
			return DirEntryWire{}, false, nil
		}
		info, isDir = target, target.IsDir()
	}
	size := info.Size()
	if isDir {
		size = 0
	}
	return DirEntryWire{
		Name: entry.Name(), IsDir: isDir, Size: size, MTimeMs: info.ModTime().UnixMilli(),
	}, true, nil
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

// maxRangeLength caps one read_range (#254), whatever the backend asks:
// the same 1 MiB as a fetch chunk (RANGE_MAX_BYTES on the backend), and
// maxHeadLength the start of the file read along with it.
const (
	maxRangeLength = maxChunkSize
	maxHeadLength  = 4 << 10
)

// HandleReadRange reads up to length bytes of relPath from offset, and its
// first headLength bytes, for the viewer's incremental follow (#254): only
// what was appended since the viewer's last read crosses the connection,
// plus the start of the file and its size, which tell the backend a file
// that was rotated or truncated. Reading at or past the end returns no
// bytes.
func HandleReadRange(basePath, relPath string, offset, length, headLength int64) (int64, []byte, []byte, error) {
	if !IsSafeRelativePath(relPath) {
		return 0, nil, nil, fmt.Errorf("unsafe path: %q", relPath)
	}
	if offset < 0 || length < 0 || headLength < 0 {
		return 0, nil, nil, fmt.Errorf("invalid range: offset %d, length %d, head %d", offset, length, headLength)
	}
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return 0, nil, nil, err
	}
	defer root.Close()
	f, err := root.Open(rootName(relPath))
	if err != nil {
		return 0, nil, nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return 0, nil, nil, err
	}
	if st.IsDir() {
		return 0, nil, nil, fmt.Errorf("%s: is a directory", relPath)
	}
	size := st.Size()
	// Fixed-size allocations, as in StreamFetch: the backend's numbers only
	// pick how much of each buffer is used.
	head, err := readAt(f, 0, make([]byte, maxHeadLength)[:min(headLength, size, maxHeadLength)])
	if err != nil {
		return size, nil, nil, err
	}
	data := []byte{}
	if offset < size {
		data, err = readAt(f, offset, make([]byte, maxRangeLength)[:min(length, size-offset, maxRangeLength)])
		if err != nil {
			return size, nil, nil, err
		}
	}
	return size, head, data, nil
}

// readAt fills as much of buf as the file has from offset.
func readAt(f *os.File, offset int64, buf []byte) ([]byte, error) {
	if len(buf) == 0 {
		return []byte{}, nil
	}
	n, err := f.ReadAt(buf, offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:n], nil
}
