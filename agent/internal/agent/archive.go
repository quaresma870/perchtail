package agent

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"strings"
	"time"
)

// Archives on the host (#382, #249): with redaction, the agent lists an
// archive's members and extracts and decompresses them itself, so the
// original never leaves the host. The rules are the server's
// (backend/app/archives.py): .zip, .tar.gz and .tgz files are archives,
// matched whatever the case; any other .gz file is one compressed file. A
// member is read as a stream and never written anywhere; its name is only
// ever reported and compared, never used as a path.

// ErrMemberNotFound is a member that isn't in the archive, or that a tar
// holds as a link, a device or a FIFO (only regular files are opened, as
// on the server: a link would open another member under its own name).
var ErrMemberNotFound = errors.New("member not found")

// errMemberEncrypted is a zip member stored encrypted: the agent has no
// password and never sends its encrypted bytes as if they were content.
var errMemberEncrypted = errors.New("member is encrypted")

func isZipName(name string) bool { return strings.HasSuffix(strings.ToLower(name), ".zip") }

func isTarGzName(name string) bool {
	lower := strings.ToLower(name)
	return strings.HasSuffix(lower, ".tar.gz") || strings.HasSuffix(lower, ".tgz")
}

// isArchiveName is the server's is_archive.
func isArchiveName(name string) bool { return isZipName(name) || isTarGzName(name) }

// isTransparentGzipName is the server's is_transparent_gzip.
func isTransparentGzipName(name string) bool {
	return strings.HasSuffix(strings.ToLower(name), ".gz") && !isTarGzName(name)
}

// lastSegment is the last /-separated part of a path or member name.
func lastSegment(name string) string { return name[strings.LastIndex(name, "/")+1:] }

// normalizeMemberName is the server's normalize_member_name: /-separated,
// without empty, "." or ".." segments.
func normalizeMemberName(name string) string {
	var parts []string
	for _, part := range strings.Split(strings.ReplaceAll(name, "\\", "/"), "/") {
		if part != "" && part != "." && part != ".." {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, "/")
}

// archiveEntry is one entry of an archive, in the archive's order.
type archiveEntry struct {
	name    string
	size    int64
	isDir   bool
	mtimeMs int64
	// A regular file (a zip's files, a tar's regular files); a tar's
	// links, devices and FIFOs are neither files nor folders.
	regular   bool
	encrypted bool
	// open reads the entry's content; for a tar, only until the walk moves
	// on to the next entry.
	open func() (io.Reader, error)
}

// listed says whether the server's list_members lists the entry.
func (e archiveEntry) listed() bool { return e.regular || e.isDir }

// walkArchive calls fn for each entry of the archive f (named name), in
// order, until fn returns false or an error.
func walkArchive(f *os.File, name string, fn func(archiveEntry) (bool, error)) error {
	switch {
	case isZipName(name):
		st, err := f.Stat()
		if err != nil {
			return err
		}
		zr, err := zip.NewReader(f, st.Size())
		// A member named ../x or /x is reported (ErrInsecurePath) with a
		// reader that works: names are only shown, never used as paths.
		if err != nil && !errors.Is(err, zip.ErrInsecurePath) {
			return err
		}
		for _, zf := range zr.File {
			entryName := zipName(zf)
			zf := zf
			more, err := fn(archiveEntry{
				name:      entryName,
				size:      clampSize(zf.UncompressedSize64),
				isDir:     strings.HasSuffix(entryName, "/"),
				mtimeMs:   zipMTimeMs(zf),
				regular:   !strings.HasSuffix(entryName, "/"),
				encrypted: zf.Flags&0x1 != 0,
				open:      func() (io.Reader, error) { return zf.Open() },
			})
			if err != nil || !more {
				return err
			}
		}
		return nil
	case isTarGzName(name):
		if _, err := f.Seek(0, io.SeekStart); err != nil {
			return err
		}
		gz, err := gzip.NewReader(f)
		if err != nil {
			return err
		}
		defer gz.Close()
		tr := tar.NewReader(gz)
		for {
			header, err := tr.Next()
			if errors.Is(err, io.EOF) {
				return nil
			}
			if err != nil && !errors.Is(err, tar.ErrInsecurePath) {
				return err
			}
			entry := archiveEntry{
				name:    tarName(header),
				size:    header.Size,
				isDir:   header.Typeflag == tar.TypeDir,
				regular: tarRegular(header.Typeflag),
				open:    func() (io.Reader, error) { return tr, nil },
			}
			if entry.isDir {
				entry.size = 0
			}
			if !header.ModTime.IsZero() && header.ModTime.Unix() != 0 {
				entry.mtimeMs = header.ModTime.UnixMilli()
			}
			more, err := fn(entry)
			if err != nil || !more {
				return err
			}
		}
	}
	return fmt.Errorf("not a supported archive: %s", lastSegment(name))
}

// clampSize is a zip header's size as an int64 (a crafted header can claim
// more than that).
func clampSize(n uint64) int64 {
	if n > math.MaxInt64 {
		return math.MaxInt64
	}
	return int64(n)
}

// tarRegular is Python's TarInfo.isfile: regular files, contiguous files
// and GNU sparse files (Go's reader turns the old regular type and a
// trailing / into TypeReg and TypeDir already).
func tarRegular(flag byte) bool {
	return flag == tar.TypeReg || flag == tar.TypeCont || flag == tar.TypeGNUSparse
}

// tarName is the member's name as Python's tarfile gives it: a folder's
// without its trailing slashes.
func tarName(header *tar.Header) string {
	if header.Typeflag == tar.TypeDir {
		return strings.TrimRight(header.Name, "/")
	}
	return header.Name
}

// zipName is the member's name as Python's zipfile gives it: cut at a NUL,
// and decoded as UTF-8 when the archive says so (flag bit 11), otherwise
// as code page 437.
func zipName(zf *zip.File) string {
	name := zf.Name
	if i := strings.IndexByte(name, 0); i >= 0 {
		name = name[:i]
	}
	if zf.Flags&0x800 != 0 {
		return name
	}
	var b strings.Builder
	for i := 0; i < len(name); i++ {
		c := name[i]
		if c < 0x80 {
			b.WriteByte(c)
		} else {
			b.WriteRune(cp437High[c-0x80])
		}
	}
	return b.String()
}

// cp437High maps the bytes 0x80-0xFF of code page 437 to Unicode.
var cp437High = []rune("ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»" +
	"░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀" +
	"αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ")

// zipMTimeMs is the member's MS-DOS date and time, taken as UTC, as the
// server reads it (it ignores the extended timestamp); 0 when invalid.
func zipMTimeMs(zf *zip.File) int64 {
	// The legacy fields (deprecated in favour of Modified, which prefers
	// the extended timestamp) are what the server reads.
	date, clock := zf.ModifiedDate, zf.ModifiedTime
	year, month, day := int(date>>9)+1980, int(date>>5&0xF), int(date&0x1F)
	hour, minute, second := int(clock>>11), int(clock>>5&0x3F), int(clock&0x1F)*2
	if month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59 {
		return 0
	}
	t := time.Date(year, time.Month(month), day, hour, minute, second, 0, time.UTC)
	if t.Day() != day {
		return 0 // 31 February
	}
	return t.UnixMilli()
}

// ListArchive lists the members of the archive at relPath under basePath,
// as the server's list_members (a tar's links, devices and FIFOs left
// out), with the caps of ListDirectory: at most maxEntries members (0, or
// anything above MaxListEntries, means MaxListEntries) and maxListBytes of
// them, truncated past either.
func ListArchive(basePath, relPath string, maxEntries int) ([]ArchiveMemberWire, bool, error) {
	if maxEntries <= 0 || maxEntries > MaxListEntries {
		maxEntries = MaxListEntries
	}
	f, closeFile, err := openFile(basePath, relPath)
	if err != nil {
		return nil, false, err
	}
	defer closeFile()
	members := make([]ArchiveMemberWire, 0)
	truncated := false
	encoded := 0
	err = walkArchive(f, relPath, func(e archiveEntry) (bool, error) {
		if !e.listed() {
			return true, nil
		}
		wire := ArchiveMemberWire{Name: e.name, IsDir: e.isDir, Size: e.size, MTimeMs: e.mtimeMs}
		size, err := wireSize(wire)
		if err != nil {
			return false, err
		}
		if len(members) >= maxEntries || encoded+size > maxListBytes {
			truncated = true
			return false, nil
		}
		encoded += size
		members = append(members, wire)
		return true, nil
	})
	if err != nil {
		return nil, false, err
	}
	return members, truncated, nil
}

// openFile opens relPath under basePath through os.Root, so neither the
// path nor a symlink on it can lead out of basePath; it must be a file.
func openFile(basePath, relPath string) (*os.File, func(), error) {
	if !IsSafeRelativePath(relPath) {
		return nil, nil, fmt.Errorf("unsafe path: %q", relPath)
	}
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return nil, nil, err
	}
	f, err := root.Open(rootName(relPath))
	if err != nil {
		_ = root.Close()
		return nil, nil, err
	}
	closeAll := func() {
		_ = f.Close()
		_ = root.Close()
	}
	st, err := f.Stat()
	if err != nil {
		closeAll()
		return nil, nil, err
	}
	if st.IsDir() {
		closeAll()
		return nil, nil, fmt.Errorf("%s: is a directory", relPath)
	}
	return f, closeAll, nil
}

// resolveMemberName is the server's resolve_member_name: the member's name
// as stored, when the name asked for is one, otherwise the first file
// whose normalized name it is, otherwise the name as asked.
func resolveMemberName(entries []archiveEntry, requested string) string {
	for _, e := range entries {
		if e.listed() && e.name == requested {
			return e.name
		}
	}
	for _, e := range entries {
		if e.listed() && !e.isDir && normalizeMemberName(e.name) == requested {
			return e.name
		}
	}
	return requested
}

// archiveHeads reads every entry of the archive without its content.
func archiveHeads(f *os.File, name string) ([]archiveEntry, error) {
	var entries []archiveEntry
	err := walkArchive(f, name, func(e archiveEntry) (bool, error) {
		e.open = nil
		entries = append(entries, e)
		return true, nil
	})
	return entries, err
}

// lastIndexes maps each name to the index of the last entry with that
// name: like the server (zipfile, tarfile), the last of members that share
// a name is the one opened.
func lastIndexes(entries []archiveEntry) map[string]int {
	last := make(map[string]int, len(entries))
	for i, e := range entries {
		last[e.name] = i
	}
	return last
}

// withMember calls fn with a reader of the member requested of the archive
// f (named name), as the server's extract_member opens it.
func withMember(f *os.File, name, requested string, fn func(archiveEntry, io.Reader) error) error {
	entries, err := archiveHeads(f, name)
	if err != nil {
		return err
	}
	raw := resolveMemberName(entries, requested)
	index, ok := lastIndexes(entries)[raw]
	if !ok || !entries[index].regular {
		return ErrMemberNotFound
	}
	if entries[index].encrypted {
		return errMemberEncrypted
	}
	i := 0
	found := false
	err = walkArchive(f, name, func(e archiveEntry) (bool, error) {
		if i != index {
			i++
			return true, nil
		}
		found = true
		r, err := e.open()
		if err != nil {
			return false, err
		}
		return false, fn(e, r)
	})
	if err == nil && !found {
		return ErrMemberNotFound // the archive changed between the two reads
	}
	return err
}

// limitedReader fails with a TooLargeError once more than limit bytes were
// read (no limit when limit <= 0): the size a header claims can lie, so
// what is produced is counted.
type limitedReader struct {
	r     io.Reader
	limit int64
	n     int64
}

func (l *limitedReader) Read(p []byte) (int, error) {
	n, err := l.r.Read(p)
	l.n += int64(n)
	if l.limit > 0 && l.n > l.limit {
		return 0, &TooLargeError{Size: l.n, Limit: l.limit}
	}
	return n, err
}

// gunzip decompresses r, as the server's decompress_gzip (concatenated
// members included).
func gunzip(r io.Reader) (io.ReadCloser, error) {
	return gzip.NewReader(r)
}
