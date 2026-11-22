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
	listing, err := ListDirectory(basePath, relPath, 0)
	return listing.Entries, err
}

// Listing is what ListDirectory found: the entries, whether they were cut
// at a cap, and, when a link leads to the folder listed, where it really
// is, relative to the base path (FeatureLinks).
type Listing struct {
	Entries   []DirEntryWire
	Truncated bool
	RealDir   *string
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
//
// Where a link leads is reported, not judged (FeatureLinks): the folder's
// own real path when a link leads to it (Listing.RealDir), and each entry's
// (DirEntryWire.Target) when it isn't where its path says, so the server
// checks its rules on both.
func ListDirectory(basePath, relPath string, maxEntries int) (Listing, error) {
	if !IsSafeRelativePath(relPath) {
		return Listing{}, fmt.Errorf("unsafe path: %q", relPath)
	}
	if maxEntries <= 0 || maxEntries > MaxListEntries {
		maxEntries = MaxListEntries
	}
	reals, err := newRealPaths(basePath)
	if err != nil {
		return Listing{}, err
	}
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return Listing{}, err
	}
	defer root.Close()
	dir, err := root.Open(rootName(relPath))
	if err != nil {
		return Listing{}, err
	}
	defer dir.Close()
	if st, err := dir.Stat(); err != nil {
		return Listing{}, err
	} else if !st.IsDir() {
		return Listing{}, ErrNotADirectory
	}
	realDir, inside, err := reals.of(dir, relPath)
	if err != nil {
		return Listing{}, err
	}
	if !inside {
		return Listing{}, &LinkTargetError{Path: relPath, Outside: true}
	}
	listing := Listing{Entries: make([]DirEntryWire, 0)}
	moved := !samePath(realDir, cleanRel(relPath))
	if moved {
		listing.RealDir = &realDir
	}

	encoded := 0
	for {
		batch, readErr := dir.ReadDir(listBatch)
		for _, entry := range batch {
			wire, ok, err := listEntry(root, reals, relPath, realDir, moved, entry)
			if err != nil {
				return Listing{}, err
			}
			if !ok {
				continue
			}
			size, err := wireSize(wire)
			if err != nil {
				return Listing{}, err
			}
			if len(listing.Entries) >= maxEntries || encoded+size > maxListBytes {
				listing.Truncated = true
				return listing, nil
			}
			encoded += size
			listing.Entries = append(listing.Entries, wire)
		}
		if errors.Is(readErr, io.EOF) || (readErr == nil && len(batch) == 0) {
			return listing, nil
		}
		if readErr != nil {
			return Listing{}, readErr
		}
	}
}

// wireSize is how many bytes the entry takes in the list_result (or
// list_archive_result) message.
func wireSize(wire any) (int, error) {
	encoded, err := json.Marshal(wire)
	if err != nil {
		return 0, err
	}
	return len(encoded) + 1, nil // and a comma
}

// listEntry turns one directory entry into its wire form; ok is false for
// one that is left out. realDir is where the folder listed really is, and
// moved whether that isn't relPath (a link leads to it).
func listEntry(root *os.Root, reals *realPaths, relPath, realDir string, moved bool, entry fs.DirEntry) (DirEntryWire, bool, error) {
	info, err := entry.Info()
	if err != nil {
		return DirEntryWire{}, false, err
	}
	isDir := entry.IsDir()
	var target *string
	if moved {
		real := path.Join(realDir, entry.Name())
		target = &real
	}
	// Symlinks, and junctions on Windows (reported as irregular), are
	// listed only when they resolve inside basePath, with their target's
	// type and size and where it really is (Target). One that leads out
	// (escape -> /etc), is broken, can't be read or leads to something that
	// is neither a file nor a folder is left out: opening it would be
	// refused by os.Root anyway (#206), as SSH and local sources already do.
	if entry.Type()&(fs.ModeSymlink|fs.ModeIrregular) != 0 {
		name := path.Join(rootName(relPath), entry.Name())
		resolved, err := root.Stat(name)
		if err != nil || !(resolved.Mode().IsRegular() || resolved.IsDir()) {
			return DirEntryWire{}, false, nil
		}
		info, isDir = resolved, resolved.IsDir()
		real, ok := linkTarget(root, reals, name)
		if !ok {
			return DirEntryWire{}, false, nil
		}
		target = nil
		if !samePath(real, cleanRel(name)) {
			target = &real
		}
	}
	size := info.Size()
	if isDir {
		size = 0
	}
	return DirEntryWire{
		Name: entry.Name(), IsDir: isDir, Size: size, MTimeMs: info.ModTime().UnixMilli(), Target: target,
	}, true, nil
}

// linkTarget is where the link name (relative to the base path) really
// leads, relative to the base path; ok is false when that can't be told
// or is out of the base path.
func linkTarget(root *os.Root, reals *realPaths, name string) (string, bool) {
	f, err := root.Open(name)
	if err != nil {
		return "", false
	}
	defer f.Close()
	real, inside, err := reals.of(f, name)
	if err != nil || !inside {
		return "", false
	}
	return real, true
}

// HandleFetch reads relPath's full content off local disk under basePath —
// the agent's half of collectors/agent.py's fetch_file. Base64-encoding
// happens in the caller (handleCommand), not here, so this stays a plain
// byte-reading function to unit test. The file must really be at
// expectReal (openFile).
func HandleFetch(basePath, relPath, expectReal string) ([]byte, error) {
	return handleFetch(nil, basePath, relPath, expectReal)
}

// handleFetch is HandleFetch with its reads counted by meter (withProgress).
func handleFetch(meter *readMeter, basePath, relPath, expectReal string) ([]byte, error) {
	f, closeFile, err := openFile(basePath, relPath, expectReal)
	if err != nil {
		return nil, err
	}
	defer closeFile()
	return io.ReadAll(meter.meter(f))
}

// Chunk sizes the agent accepts from the backend: small enough to keep
// every message well under the backend's WebSocket message limit (16 MiB
// in uvicorn by default), large enough not to waste time on overhead. The
// maximum is what the backend asks for (FETCH_CHUNK_BYTES).
const (
	minChunkSize = 4 << 10
	maxChunkSize = 1 << 20
)

// Which of the backend's limits a TooLargeError passed, sent to it as the
// error's `limit` so it names the right one even when both are the same
// number: the file as stored (MaxBytes, the server's MAX_FETCH_MB) or what
// extracting or decompressing it produced (MaxDecompressed, its
// MAX_DECOMPRESSED_MB).
const (
	limitFetch        = "fetch"
	limitDecompressed = "decompressed"
)

// TooLargeError refuses a streamed fetch over the backend's limit. Kind is
// limitFetch or limitDecompressed.
type TooLargeError struct {
	Size  int64
	Limit int64
	Kind  string
}

func (e *TooLargeError) Error() string {
	return fmt.Sprintf("file is %d bytes, over the %d-byte limit", e.Size, e.Limit)
}

// StreamFetch reads relPath under basePath in chunks of at most chunkSize
// bytes and passes each one to send, in order (#205). Only one chunk is
// held at a time, however large the file. maxBytes > 0 refuses a file
// larger than that: up front from its size, and again while reading,
// since a log can grow while it's being read. It returns the number of
// chunks and bytes sent. An error from send is returned as is. The file
// must really be at expectReal (openFile).
func StreamFetch(
	basePath, relPath, expectReal string, chunkSize, maxBytes int64, send func([]byte) error,
) (chunks int, size int64, err error) {
	return streamFetch(nil, basePath, relPath, expectReal, chunkSize, maxBytes, send)
}

// streamFetch is StreamFetch with its reads counted by meter (withProgress).
func streamFetch(
	meter *readMeter, basePath, relPath, expectReal string, chunkSize, maxBytes int64, send func([]byte) error,
) (chunks int, size int64, err error) {
	chunkSize = min(max(chunkSize, minChunkSize), maxChunkSize)
	opened, closeFile, err := openFile(basePath, relPath, expectReal)
	if err != nil {
		return 0, 0, err
	}
	defer closeFile()
	f := meter.meter(opened)
	st, err := f.Stat()
	if err != nil {
		return 0, 0, err
	}
	if maxBytes > 0 && st.Size() > maxBytes {
		return 0, 0, &TooLargeError{Size: st.Size(), Limit: maxBytes, Kind: limitFetch}
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
				return chunks, size, &TooLargeError{Size: size, Limit: maxBytes, Kind: limitFetch}
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
// bytes. The file must really be at expectReal (openFile).
func HandleReadRange(basePath, relPath, expectReal string, offset, length, headLength int64) (int64, []byte, []byte, error) {
	return meteredReadRange(nil, basePath, relPath, expectReal, offset, length, headLength)
}

// meteredReadRange is HandleReadRange with its reads counted by meter
// (withProgress).
func meteredReadRange(meter *readMeter, basePath, relPath, expectReal string, offset, length, headLength int64) (int64, []byte, []byte, error) {
	if offset < 0 || length < 0 || headLength < 0 {
		return 0, nil, nil, fmt.Errorf("invalid range: offset %d, length %d, head %d", offset, length, headLength)
	}
	opened, closeFile, err := openFile(basePath, relPath, expectReal)
	if err != nil {
		return 0, nil, nil, err
	}
	defer closeFile()
	f := meter.meter(opened)
	st, err := f.Stat()
	if err != nil {
		return 0, nil, nil, err
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
func readAt(f io.ReaderAt, offset int64, buf []byte) ([]byte, error) {
	if len(buf) == 0 {
		return []byte{}, nil
	}
	n, err := f.ReadAt(buf, offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:n], nil
}
