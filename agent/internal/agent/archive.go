package agent

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"strconv"
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
// Answered with the code "member_encrypted" (errorResult), which the server
// refuses as it refuses an encrypted member it finds itself.
var errMemberEncrypted = errors.New("member is encrypted")

// archiveUnreadableError is an archive that can't be read as one: damaged,
// cut short, or another kind of file under a .zip or .tar.gz name. Its
// listing or a member's content fails with it, answered with the code
// "archive_unreadable" (errorResult), which the server refuses as it
// refuses an archive it can't read itself.
type archiveUnreadableError struct{ err error }

func (e *archiveUnreadableError) Error() string { return e.err.Error() }
func (e *archiveUnreadableError) Unwrap() error { return e.err }

// gzipUnreadableError is a .gz file or member that can't be decompressed:
// damaged, cut short, or not gzip at all. Answered with the code
// "gzip_unreadable".
type gzipUnreadableError struct{ err error }

func (e *gzipUnreadableError) Error() string { return e.err.Error() }
func (e *gzipUnreadableError) Unwrap() error { return e.err }

// unreadable marks err, from reading an archive or decompressing a .gz,
// with wrap, unless it isn't about the content: the end of it, a limit, an
// I/O error from the file itself, a cancelled command, or an error marked
// already (the archive under a .gz member failing first).
func unreadable(err error, wrap func(error) error) error {
	var tooLarge *TooLargeError
	var ioErr *fs.PathError
	var archiveErr *archiveUnreadableError
	var gzipErr *gzipUnreadableError
	switch {
	case err == nil, errors.Is(err, io.EOF), errors.Is(err, errCancelled),
		errors.As(err, &tooLarge), errors.As(err, &ioErr),
		errors.As(err, &archiveErr), errors.As(err, &gzipErr):
		return err
	}
	return wrap(err)
}

var errEmptyArchive = errors.New("the archive is empty")

// MaxZipEntries is the most entries a .zip may hold, as on the server
// (backend/app/archives.py, MAX_ZIP_ENTRIES): archive/zip reads the whole
// index into memory before anything else, so the count the archive states
// is read first (zipEntryCount) and a .zip stating more is unreadable.
const MaxZipEntries = 50000

var errTooManyZipEntries = errors.New("the archive has more than " + strconv.Itoa(MaxZipEntries) + " entries")

var (
	zipEndSignature      = []byte("PK\x05\x06")
	zip64LocatorSig      = []byte("PK\x06\x07")
	zip64EndSignature    = []byte("PK\x06\x06")
	zipEndLength         = int64(22)
	zip64LocatorLength   = int64(20)
	zip64EndRecordLength = int64(56)
)

// zipEntryCount is how many entries the .zip r (of size bytes) says it
// holds: from its end-of-central-directory record, or from the ZIP64 record
// right before the ZIP64 locator when there is one, found as the server's
// zipfile finds them (the last 22 bytes first, then the last 64 KiB). false
// when there is no such record; the zip reader then refuses the file
// itself.
func zipEntryCount(r io.ReaderAt, size int64) (uint64, bool) {
	if size < zipEndLength {
		return 0, false
	}
	tailLength := zipEndLength
	tail := make([]byte, tailLength)
	if _, err := r.ReadAt(tail, size-tailLength); err != nil {
		return 0, false
	}
	if !bytes.Equal(tail[:4], zipEndSignature) || tail[20] != 0 || tail[21] != 0 {
		tailLength = min(size, zipEndLength+0xFFFF)
		tail = make([]byte, tailLength)
		if _, err := r.ReadAt(tail, size-tailLength); err != nil {
			return 0, false
		}
	}
	at := bytes.LastIndex(tail, zipEndSignature)
	if at < 0 || int64(len(tail)-at) < zipEndLength {
		return 0, false
	}
	entries := uint64(binary.LittleEndian.Uint16(tail[at+10:]))
	locatorOffset := size - tailLength + int64(at) - zip64LocatorLength
	if locatorOffset < 0 {
		return entries, true
	}
	locator := make([]byte, zip64LocatorLength)
	if _, err := r.ReadAt(locator, locatorOffset); err != nil || !bytes.Equal(locator[:4], zip64LocatorSig) {
		return entries, true
	}
	recordOffset := locatorOffset - zip64EndRecordLength
	if recordOffset < 0 {
		return 0, false
	}
	record := make([]byte, zip64EndRecordLength)
	if _, err := r.ReadAt(record, recordOffset); err != nil || !bytes.Equal(record[:4], zip64EndSignature) {
		return 0, false
	}
	return binary.LittleEndian.Uint64(record[32:]), true
}

// errNoTarHeader is a .tar.gz whose content, once decompressed, ends before
// its first whole entry.
var errNoTarHeader = errors.New("the archive ends before its first entry")

// tarBlockSize is the size of a tar header block.
const tarBlockSize = 512

// headReader keeps the first tar block read through it.
type headReader struct {
	r    io.Reader
	head []byte
}

func (h *headReader) Read(p []byte) (int, error) {
	n, err := h.r.Read(p)
	if room := tarBlockSize - len(h.head); room > 0 {
		h.head = append(h.head, p[:min(n, room)]...)
	}
	return n, err
}

// startsWithZeros says the first block was read whole and is all zeros:
// the end of an archive with no members.
func (h *headReader) startsWithZeros() bool {
	return len(h.head) == tarBlockSize && bytes.Count(h.head, []byte{0}) == tarBlockSize
}

func archiveUnreadable(err error) error {
	return unreadable(err, func(err error) error { return &archiveUnreadableError{err} })
}

func gzipUnreadable(err error) error {
	return unreadable(err, func(err error) error { return &gzipUnreadableError{err} })
}

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
func walkArchive(f contentFile, name string, fn func(archiveEntry) (bool, error)) error {
	switch {
	case isZipName(name):
		st, err := f.Stat()
		if err != nil {
			return err
		}
		if entries, ok := zipEntryCount(f, st.Size()); ok && entries > MaxZipEntries {
			return archiveUnreadable(errTooManyZipEntries)
		}
		zr, err := zip.NewReader(f, st.Size())
		// A member named ../x or /x is reported (ErrInsecurePath) with a
		// reader that works: names are only shown, never used as paths.
		if err != nil && !errors.Is(err, zip.ErrInsecurePath) {
			return archiveUnreadable(err)
		}
		// An archive whose record states fewer entries than its index holds.
		if len(zr.File) > MaxZipEntries {
			return archiveUnreadable(errTooManyZipEntries)
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
				open: func() (io.Reader, error) {
					r, err := zf.Open()
					// A member whose local header is damaged or cut short,
					// though the index lists it, can't be read, as the
					// server's zipfile says (BadZipFile). io.EOF here is the
					// header cut short, never the end of the content, so it
					// is marked too. An I/O error from the file itself and
					// a compression method the agent can't read keep their
					// errors.
					var ioErr *fs.PathError
					if err != nil && !errors.Is(err, zip.ErrAlgorithm) && !errors.As(err, &ioErr) {
						return nil, &archiveUnreadableError{err}
					}
					if err != nil {
						return nil, err
					}
					return &strictMemberReader{r: r}, nil
				},
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
		if errors.Is(err, io.EOF) {
			// An empty file: not a .tar.gz, as the server's tarfile says.
			err = errEmptyArchive
		}
		if err != nil {
			return archiveUnreadable(err)
		}
		defer gz.Close()
		read := &headReader{r: gz}
		tr := tar.NewReader(read)
		for sawEntry := false; ; {
			header, err := tr.Next()
			// As the server's tarfile: an archive that ends before its first
			// whole entry lists empty only when its first block is all zeros
			// (the end of an archive with no members). Less than a block,
			// nothing at all, or a pax or GNU long-name header followed by
			// nothing or by part of the entry's header is not a tar. A global
			// pax header (git archive writes one), which archive/tar gives as
			// an entry of its own, isn't one: tarfile reads it with the
			// header after it.
			if !sawEntry && (errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)) && !read.startsWithZeros() {
				return archiveUnreadable(errNoTarHeader)
			}
			// io.ErrUnexpectedEOF: the archive ends inside the last member,
			// whose reads then fail with errTruncatedMember (unreadable, as a
			// truncated zip member); the members before it are whole.
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				return nil
			}
			if err != nil && !errors.Is(err, tar.ErrInsecurePath) {
				return archiveUnreadable(err)
			}
			if header.Typeflag != tar.TypeXGlobalHeader {
				sawEntry = true
			}
			entry := archiveEntry{
				name:    tarName(header),
				size:    header.Size,
				isDir:   header.Typeflag == tar.TypeDir,
				regular: tarRegular(header.Typeflag),
				open:    func() (io.Reader, error) { return &strictMemberReader{r: tr}, nil },
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

// errTruncatedMember is a zip or tar member whose content ends before the
// size its header declares, which archive/zip and archive/tar report as
// io.ErrUnexpectedEOF:
// redactStream and chunkSender.copy would take that for the end of the
// content. It is unreadable, as on the server: a fetch of it is refused and
// a fetch_archive leaves it out.
var errTruncatedMember = errors.New("archive member ended before its declared size")

type strictMemberReader struct{ r io.Reader }

func (s *strictMemberReader) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	if errors.Is(err, io.ErrUnexpectedEOF) {
		err = errTruncatedMember
	}
	return n, archiveUnreadable(err)
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
// them, truncated past either. The archive must really be at expectReal
// (openFile).
func ListArchive(basePath, relPath, expectReal string, maxEntries int) ([]ArchiveMemberWire, bool, error) {
	return listArchive(nil, basePath, relPath, expectReal, maxEntries)
}

// listArchive is ListArchive with its reads of the archive counted by meter
// (withProgress), so listing a large archive says it is still reading.
func listArchive(meter *readMeter, basePath, relPath, expectReal string, maxEntries int) ([]ArchiveMemberWire, bool, error) {
	if maxEntries <= 0 || maxEntries > MaxListEntries {
		maxEntries = MaxListEntries
	}
	opened, closeFile, err := openFile(basePath, relPath, expectReal)
	if err != nil {
		return nil, false, err
	}
	defer closeFile()
	f := meter.meter(opened)
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
func archiveHeads(f contentFile, name string) ([]archiveEntry, error) {
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
func withMember(f contentFile, name, requested string, fn func(archiveEntry, io.Reader) error) error {
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
// what is produced is counted. kind is the limit's (TooLargeError.Kind).
type limitedReader struct {
	r     io.Reader
	limit int64
	kind  string
	n     int64
}

func (l *limitedReader) Read(p []byte) (int, error) {
	n, err := l.r.Read(p)
	l.n += int64(n)
	if l.limit > 0 && l.n > l.limit {
		return 0, &TooLargeError{Size: l.n, Limit: l.limit, Kind: l.kind}
	}
	return n, err
}

// gunzip decompresses r, as the server's decompress_gzip (concatenated
// members included).
func gunzip(r io.Reader) (io.ReadCloser, error) {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return nil, gzipUnreadable(err)
	}
	return gz, nil
}
