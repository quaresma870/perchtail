package agent

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/quaresma870/perchtail/agent/internal/redact"
)

// Redaction on the host (#382, #249). When redaction applies to a source,
// the server sends the rules in a `redact` block with every command that
// returns content (rules per command: a rule saved on the server applies to
// the next request, and there is no state to rebuild after a reconnect),
// and the agent redacts what it read before any of it is sent, with the
// engine in agent/internal/redact, which behaves exactly as the server's.
// It fails closed: a block, a rule or content it can't apply the rules to
// is answered with a redaction_error before anything else is sent, never
// with the content as it is. What it sends says it was redacted and with
// which rules (Redacted, Digest), which the server checks.

// Codes of a redaction_error.
const (
	// A rule the engine can't run; ErrorResult.Rule is its id.
	codeRuleUnsupported = "rule_unsupported"
	// Content in an encoding the engine can't redact (UTF-32).
	codeUnsupportedEncoding = "unsupported_encoding"
	// A redact block the agent can't read, for an engine it doesn't run,
	// or whose rules don't match its digest.
	codeInvalidRedact = "invalid_redact"
	// Content that can't be redacted as asked: a whole archive, or a .gz
	// file not asked for decompressed, which would carry the original
	// compressed.
	codeNotRedactable = "not_redactable"
	// Content compressed in a format the agent doesn't decompress there,
	// told by its first bytes (unsupportedCompressedFormat).
	codeUnsupportedFormat = "unsupported_format"
)

// The first bytes of the compressed formats the agent doesn't decompress,
// as the server's unsupported_compressed_format. bzip2 is "BZh", the block
// size ('1' to '9'), then the magic of its first block or of the end of an
// empty stream. gzip and zip are only decompressed when the name says so
// (.gz, .zip) and the command asks for it; by the time content reaches this
// check that has happened, so gzip or zip content here is in a file whose
// name doesn't say so (app.log.1 holding gzip) and is refused too.
var (
	bzip2BlockMagics           = [][]byte{[]byte("\x31\x41\x59\x26\x53\x59"), []byte("\x17\x72\x45\x38\x50\x90")}
	unsupportedCompressedMagic = [][]byte{
		[]byte("\xfd7zXZ\x00"),       // xz
		[]byte("\x28\xb5\x2f\xfd"),   // zstd
		[]byte("7z\xbc\xaf\x27\x1c"), // 7z
		[]byte("Rar!\x1a\x07"),       // rar (1.5 and 5)
		[]byte("\x1f\x8b"),           // gzip
		[]byte("PK\x03\x04"),         // zip
		[]byte("PK\x05\x06"),         // zip with no members
		[]byte("PK\x07\x08"),         // zip split or spanned
	}
)

// unsupportedCompressedFormat says whether content starting with head is
// compressed in a format the agent doesn't decompress there, told by its
// first bytes rather than by its name.
func unsupportedCompressedFormat(head []byte) bool {
	if len(head) >= 10 && bytes.HasPrefix(head, []byte("BZh")) && head[3] >= '1' && head[3] <= '9' {
		for _, magic := range bzip2BlockMagics {
			if bytes.Equal(head[4:10], magic) {
				return true
			}
		}
	}
	for _, magic := range unsupportedCompressedMagic {
		if bytes.HasPrefix(head, magic) {
			return true
		}
	}
	return false
}

func unsupportedFormatError() error {
	return &redactionError{
		code:    codeUnsupportedFormat,
		message: "the content is in a compressed format redaction doesn't support",
	}
}

const (
	// maxParallelRedactions bounds how many commands with a `redact` block
	// run at once, since redacting takes CPU on the host (a production
	// server); the others wait.
	maxParallelRedactions = 2
	// followContextBytes is how much a redacted read_range reads before its
	// offset, only to know whether the offset falls inside a private key
	// (the server's FOLLOW_CONTEXT_BYTES), and followWindowBytes what is
	// left of a read for the content after it (FOLLOW_WINDOW_BYTES).
	followContextBytes = 16 << 10
	followWindowBytes  = maxRangeLength - followContextBytes
	headKeyBytes       = 32
	maxPemLines        = 100 * redact.PemMaxLines
)

// redactionError refuses a command with a `redact` block; see the codes
// above. Its message never holds content.
type redactionError struct {
	code    string
	message string
	rule    any
}

func (e *redactionError) Error() string { return e.message }

// redactBlock is the `redact` block of a command.
type redactBlock struct {
	// The engine version the rules are for (redact.EngineVersion).
	Engine int `json:"engine"`
	// The server's rules_digest of Rules.
	Digest string            `json:"digest"`
	Rules  []redact.RuleSpec `json:"rules"`
	// The source's key for HeadDigest, 32 bytes, base64-encoded.
	HeadKey string `json:"head_key"`
	// 0 means the engine's default.
	MaxLineBytes int `json:"max_line_bytes"`
	PemMaxLines  int `json:"pem_max_lines"`
	// "utf-8" (the viewer; the default) or "source" (a download: UTF-16
	// content is encoded back, and the end of the content ends its last
	// line).
	OutputEncoding string `json:"output_encoding"`
}

// redaction is a `redact` block ready to use.
type redaction struct {
	rules   *redact.Rules
	headKey []byte
	opts    redact.Options
}

// hasRedact says whether a command carries a `redact` block.
func hasRedact(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && !bytes.Equal(trimmed, []byte("null"))
}

// parseRedaction reads a command's `redact` block: nil when there is none.
// Numbers are read as written (UseNumber), so a rule is refused exactly as
// the server's engine refuses it (1e2 or 100.0 aren't whole numbers there),
// and a field the agent doesn't know is refused rather than ignored.
func parseRedaction(raw json.RawMessage) (*redaction, error) {
	if !hasRedact(raw) {
		return nil, nil
	}
	invalid := func(format string, args ...any) error {
		return &redactionError{code: codeInvalidRedact, message: fmt.Sprintf(format, args...)}
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	var block redactBlock
	if err := decoder.Decode(&block); err != nil {
		return nil, invalid("the redact block can't be read: %v", err)
	}
	if block.Engine != redact.EngineVersion {
		return nil, invalid("redaction engine %d isn't supported (this agent runs engine %d)", block.Engine, redact.EngineVersion)
	}
	if block.Rules == nil {
		return nil, invalid("the redact block has no rules")
	}
	key, err := base64.StdEncoding.DecodeString(block.HeadKey)
	if err != nil || len(key) != headKeyBytes {
		return nil, invalid("head_key must be %d bytes, base64-encoded", headKeyBytes)
	}
	if block.MaxLineBytes < 0 || block.MaxLineBytes > redact.MaxLineBytes {
		return nil, invalid("max_line_bytes must be from 1 to %d", redact.MaxLineBytes)
	}
	if block.PemMaxLines < 0 || block.PemMaxLines > maxPemLines {
		return nil, invalid("pem_max_lines must be from 1 to %d", maxPemLines)
	}
	switch block.OutputEncoding {
	case "", "utf-8", "source":
	default:
		return nil, invalid("output_encoding must be utf-8 or source")
	}
	rules, err := redact.Compile(block.Rules)
	if err != nil {
		var ruleErr *redact.RuleError
		if errors.As(err, &ruleErr) {
			return nil, &redactionError{code: codeRuleUnsupported, message: ruleErr.Message, rule: failingRule(block.Rules)}
		}
		return nil, invalid("the rules can't be compiled: %v", err)
	}
	if block.Digest != rules.Digest() {
		return nil, invalid("the rules don't match their digest")
	}
	return &redaction{
		rules:   rules,
		headKey: key,
		opts: redact.Options{
			OutputEncoding: block.OutputEncoding,
			MaxLineBytes:   block.MaxLineBytes,
			PemMaxLines:    block.PemMaxLines,
		},
	}, nil
}

// failingRule is the id of the first rule that can't be compiled on its
// own, for the server to name it.
func failingRule(specs []redact.RuleSpec) any {
	for _, spec := range specs {
		if _, err := redact.Compile([]redact.RuleSpec{spec}); err != nil {
			return spec["id"]
		}
	}
	return nil
}

// forDownload says whether the block is for a download (output in the
// source's encoding), whose content is complete: the end of the file ends
// its last line.
func (r *redaction) forDownload() bool { return r.opts.OutputEncoding == "source" }

// errorResult answers a failed command: a redaction_error for a
// redactionError, otherwise errType with the error's code.
func errorResult(cmd Command, errType string, err error) ErrorResult {
	var refused *redactionError
	if errors.As(err, &refused) {
		return ErrorResult{Type: "redaction_error", ID: cmd.ID, Error: refused.message, Code: refused.code, Rule: refused.rule}
	}
	result := ErrorResult{Type: errType, ID: cmd.ID, Error: err.Error()}
	var tooLarge *TooLargeError
	var link *LinkTargetError
	switch {
	case errors.As(err, &tooLarge):
		result.Code, result.Size = "too_large", tooLarge.Size
	case errors.Is(err, ErrMemberNotFound):
		result.Code = "not_found"
	case errors.As(err, &link):
		// Where it really is goes to the server only, which checks its
		// rules on it (FeatureLinks); nothing of the content was read.
		result.Code, result.Outside = "link_target", link.Outside
		if !link.Outside {
			real := link.Real
			result.Real = &real
		}
	}
	return result
}

// chunkSender sends content as fetch_chunk messages of at most size bytes.
// Seq numbers them, and the archive_member messages of a fetch_archive,
// in order.
type chunkSender struct {
	ctx    context.Context
	id     string
	size   int
	send   func(any) error
	seq    int
	chunks int
	bytes  int64
	// sendErr is set when the connection failed, which ends it.
	sendErr error
}

func (c *chunkSender) message(msg any) error {
	if c.ctx.Err() != nil {
		return errCancelled
	}
	if err := c.send(msg); err != nil {
		c.sendErr = err
		return err
	}
	c.seq++
	return nil
}

func (c *chunkSender) write(data []byte) error {
	for len(data) > 0 {
		n := min(len(data), c.size)
		chunk := FetchChunk{Type: "fetch_chunk", ID: c.id, Seq: c.seq, DataB64: base64.StdEncoding.EncodeToString(data[:n])}
		if err := c.message(chunk); err != nil {
			return err
		}
		c.chunks++
		c.bytes += int64(n)
		data = data[n:]
	}
	return nil
}

// copyChunks sends what src holds as it is.
func (c *chunkSender) copy(src io.Reader) error {
	buf := make([]byte, maxChunkSize)[:c.size]
	for {
		n, err := io.ReadFull(src, buf)
		if n > 0 {
			if err := c.write(buf[:n]); err != nil {
				return err
			}
		}
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

func clampChunkSize(size int64) int { return int(min(max(size, minChunkSize), maxChunkSize)) }

// redactStream redacts what src holds, a block at a time, and passes the
// result to write, as the server's _redact_file: in the encoding detected
// from the first redact.HeadBytes bytes (UTF-32 is refused before anything
// is written). start, when set, is called once the encoding is known and
// before anything is written. final is true for complete content (a
// download, an archive member, a .gz file): its last line is redacted as
// it is; otherwise an unfinished last line becomes the incomplete-line
// marker and SourceOffset its start.
func redactStream(src io.Reader, red *redaction, final bool, blockSize int, start func() error, write func([]byte) error) (RedactionInfo, error) {
	buf := make([]byte, maxChunkSize)[:blockSize]
	n, err := io.ReadFull(src, buf)
	eof := errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
	if err != nil && !eof {
		return RedactionInfo{}, err
	}
	head := bytes.Clone(buf[:min(n, redact.HeadBytes)])
	if unsupportedCompressedFormat(head) {
		return RedactionInfo{}, unsupportedFormatError()
	}
	encoding, err := redact.DetectEncoding(head)
	if err != nil {
		return RedactionInfo{}, &redactionError{
			code:    codeUnsupportedEncoding,
			message: "the content is in an encoding redaction doesn't support (UTF-32)",
		}
	}
	opts := red.opts
	opts.SourceEncoding = encoding
	redactor, err := redact.New(red.rules, opts)
	if err != nil {
		return RedactionInfo{}, &redactionError{code: codeInvalidRedact, message: err.Error()}
	}
	if start != nil {
		if err := start(); err != nil {
			return RedactionInfo{}, err
		}
	}
	for {
		if n > 0 {
			if err := write(redactor.Feed(buf[:n])); err != nil {
				return RedactionInfo{}, err
			}
		}
		if eof {
			// Refused part way (UTF-32), the rest is still read, without
			// being sent, so SourceOffset is the end of the content, as on
			// the server.
			break
		}
		n, err = io.ReadFull(src, buf)
		eof = errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
		if err != nil && !eof {
			return RedactionInfo{}, err
		}
	}
	if err := write(redactor.Finish(final)); err != nil {
		return RedactionInfo{}, err
	}
	info := RedactionInfo{
		Redacted:        true,
		Digest:          red.rules.Digest(),
		SourceSize:      redactor.SourceSize,
		SourceOffset:    redactor.SourceOffset,
		TrailingPartial: redactor.TrailingPartial,
		Counts:          redactor.Counts,
		HeadDigest:      redact.HeadDigest(red.headKey, head, redactor.SourceOffset),
	}
	if redactor.RefusedAt >= 0 {
		refused := redactor.RefusedAt
		info.RefusedEncodingAt = &refused
	}
	return info, nil
}

// withContent opens what a fetch asks for and passes it to fn: the file
// at cmd.Path, the archive member cmd.Member, and/or the content
// decompressed (cmd.Decompress "gzip"). MaxBytes caps the file as stored,
// MaxDecompressed what extracting and decompressing produce. Redacting, an
// archive (whole or as a member, whatever cmd.Decompress says), or a .gz
// file not decompressed, is refused: it would carry the original,
// compressed.
func withContent(basePath string, cmd Command, redacting bool, fn func(io.Reader) error) error {
	switch cmd.Decompress {
	case "", "gzip":
	default:
		return fmt.Errorf("unsupported decompression %q", cmd.Decompress)
	}
	f, closeFile, err := openFile(basePath, cmd.Path, cmd.ExpectReal)
	if err != nil {
		return err
	}
	defer closeFile()
	if cmd.MaxBytes > 0 {
		st, err := f.Stat()
		if err != nil {
			return err
		}
		if st.Size() > cmd.MaxBytes {
			return &TooLargeError{Size: st.Size(), Limit: cmd.MaxBytes}
		}
	}
	name := lastSegment(cmd.Path)
	content := func(r io.Reader) error {
		if cmd.Decompress == "" {
			return fn(r)
		}
		gz, err := gunzip(r)
		if err != nil {
			return err
		}
		defer gz.Close()
		// Strict, as the server's decompress_gzip: a .gz cut short is
		// refused, not passed on up to the cut.
		return fn(&limitedReader{r: &strictGzipReader{r: gz}, limit: cmd.MaxDecompressed})
	}
	if cmd.Member != "" {
		if !isArchiveName(name) {
			return fmt.Errorf("%s: not an archive", name)
		}
		if redacting && isArchiveName(lastSegment(cmd.Member)) {
			// Whatever cmd.Decompress says: a .tar.gz decompressed is
			// still an archive, whose compressed members would pass as text.
			return &redactionError{code: codeNotRedactable, message: "an archive inside an archive can't be redacted"}
		}
		if redacting && isTransparentGzipName(lastSegment(cmd.Member)) && cmd.Decompress == "" {
			// It would carry the original, compressed.
			return &redactionError{code: codeNotRedactable, message: "a compressed member can't be redacted unless it is asked for decompressed"}
		}
		return withMember(f, name, cmd.Member, func(_ archiveEntry, r io.Reader) error {
			return content(&limitedReader{r: r, limit: cmd.MaxDecompressed})
		})
	}
	if redacting && (isArchiveName(name) || (cmd.Decompress == "" && isTransparentGzipName(name))) {
		return &redactionError{code: codeNotRedactable, message: "a whole archive or compressed file can't be redacted"}
	}
	return content(&limitedReader{r: f, limit: cmd.MaxBytes})
}

// serveContentFetch answers a streamed fetch with a `redact` block, a
// member or a decompression: fetch_chunk messages, then a FetchEnd, or a
// RedactedFetchEnd when redacted.
func serveContentFetch(ctx context.Context, cfg Config, cmd Command, send func(any) error) error {
	red, err := parseRedaction(cmd.Redact)
	if err != nil {
		return send(errorResult(cmd, "fetch_error", err))
	}
	out := &chunkSender{ctx: ctx, id: cmd.ID, size: clampChunkSize(cmd.ChunkSize), send: send}
	var info RedactionInfo
	err = withContent(cfg.BasePath, cmd, red != nil, func(src io.Reader) error {
		if red == nil {
			return out.copy(src)
		}
		final := cmd.Member != "" || cmd.Decompress != "" || red.forDownload()
		var err error
		info, err = redactStream(src, red, final, out.size, nil, out.write)
		return err
	})
	if out.sendErr != nil {
		return out.sendErr
	}
	if errors.Is(err, errCancelled) {
		return nil
	}
	if err != nil {
		return send(errorResult(cmd, "fetch_error", err))
	}
	end := FetchEnd{Type: "fetch_result", ID: cmd.ID, Streamed: true, Chunks: out.chunks, Size: out.bytes}
	if red == nil {
		return send(end)
	}
	return send(RedactedFetchEnd{FetchEnd: end, RedactionInfo: info})
}

// serveFetchArchive answers a fetch_archive: the members asked for, in the
// archive's order, each an ArchiveMember and its fetch_chunk messages, in
// one pass over the archive's content, then a FetchEnd (a
// RedactedFetchEnd when redacted, with the totals). With redaction,
// members are complete content (the end ends their last line), a .gz
// member is sent decompressed, and a member that can't be redacted is
// announced as skipped, with no content.
func serveFetchArchive(ctx context.Context, cfg Config, cmd Command, send func(any) error) error {
	red, err := parseRedaction(cmd.Redact)
	if err != nil {
		return send(errorResult(cmd, "fetch_error", err))
	}
	out := &chunkSender{ctx: ctx, id: cmd.ID, size: clampChunkSize(cmd.ChunkSize), send: send}
	total := RedactionInfo{Counts: map[string]int{}}
	err = fetchArchive(cfg.BasePath, cmd, red, out, &total)
	if out.sendErr != nil {
		return out.sendErr
	}
	if errors.Is(err, errCancelled) {
		return nil
	}
	if err != nil {
		return send(errorResult(cmd, "fetch_error", err))
	}
	end := FetchEnd{Type: "fetch_result", ID: cmd.ID, Streamed: true, Chunks: out.chunks, Size: out.bytes}
	if red == nil {
		return send(end)
	}
	total.Redacted, total.Digest, total.SourceOffset = true, red.rules.Digest(), total.SourceSize
	return send(RedactedFetchEnd{FetchEnd: end, RedactionInfo: total})
}

func fetchArchive(basePath string, cmd Command, red *redaction, out *chunkSender, total *RedactionInfo) error {
	f, closeFile, err := openFile(basePath, cmd.Path, cmd.ExpectReal)
	if err != nil {
		return err
	}
	defer closeFile()
	name := lastSegment(cmd.Path)
	if !isArchiveName(name) {
		return fmt.Errorf("%s: not an archive", name)
	}
	if cmd.MaxBytes > 0 {
		st, err := f.Stat()
		if err != nil {
			return err
		}
		if st.Size() > cmd.MaxBytes {
			return &TooLargeError{Size: st.Size(), Limit: cmd.MaxBytes}
		}
	}
	entries, err := archiveHeads(f, name)
	if err != nil {
		return err
	}
	// The members asked for, by the index of the entry sent for each (the
	// last with that name, as when one is opened), and those that aren't
	// there.
	last := lastIndexes(entries)
	wanted := map[int]bool{}
	var missing []string
	seen := map[string]bool{}
	for _, member := range cmd.Members {
		if seen[member] {
			continue
		}
		seen[member] = true
		if index, ok := last[member]; ok && entries[index].regular {
			wanted[index] = true
		} else {
			missing = append(missing, member)
		}
	}
	announce := func(e archiveEntry, gunzipped bool, skipped string) error {
		return out.message(ArchiveMember{
			Type: "archive_member", ID: out.id, Seq: out.seq, Name: e.name,
			MTimeMs: e.mtimeMs, Gunzipped: gunzipped, Skipped: skipped,
		})
	}
	index := -1
	err = walkArchive(f, name, func(e archiveEntry) (bool, error) {
		index++
		if !wanted[index] {
			return true, nil
		}
		return true, sendArchiveMember(e, cmd.MaxDecompressed, red, out, total, announce)
	})
	if err != nil {
		return err
	}
	for _, member := range missing {
		if err := announce(archiveEntry{name: member}, false, "not_found"); err != nil {
			return err
		}
	}
	return nil
}

// Why a member of a redacted fetch_archive that couldn't be read is left
// out: over max_decompressed once decompressed, or unreadable.
const (
	skipTooLarge   = "too_large"
	skipUnreadable = "unreadable"
)

func memberSkip(err error) string {
	var tooLarge *TooLargeError
	if errors.As(err, &tooLarge) {
		return skipTooLarge
	}
	return skipUnreadable
}

// errTruncatedGzip is a .gz file or member that ends before its
// end-of-stream marker, which gzip.Reader reports as io.ErrUnexpectedEOF:
// redactStream and chunkSender.copy would take that for the end of the
// content.
var errTruncatedGzip = errors.New("compressed content ended before the end-of-stream marker")

type strictGzipReader struct{ r io.Reader }

func (s *strictGzipReader) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	if errors.Is(err, io.ErrUnexpectedEOF) {
		err = errTruncatedGzip
	}
	return n, err
}

func sendArchiveMember(
	e archiveEntry, maxDecompressed int64, red *redaction, out *chunkSender, total *RedactionInfo,
	announce func(archiveEntry, bool, string) error,
) error {
	if e.encrypted {
		return announce(e, false, "encrypted")
	}
	if red != nil && isArchiveName(lastSegment(e.name)) {
		return announce(e, false, "nested_archive")
	}
	r, err := e.open()
	if err != nil {
		if red != nil {
			return announce(e, false, skipUnreadable)
		}
		return err
	}
	var src io.Reader = &limitedReader{r: r, limit: maxDecompressed}
	gunzipped := false
	if red != nil && isTransparentGzipName(lastSegment(e.name)) {
		gz, err := gunzip(src)
		if err != nil {
			return announce(e, false, memberSkip(err))
		}
		defer gz.Close()
		src, gunzipped = &limitedReader{r: &strictGzipReader{r: gz}, limit: maxDecompressed}, true
	}
	if red == nil {
		if err := announce(e, false, ""); err != nil {
			return err
		}
		return out.copy(src)
	}
	info, err := redactStream(src, red, true, out.size, func() error { return announce(e, gunzipped, "") }, out.write)
	var refused *redactionError
	if errors.As(err, &refused) {
		switch refused.code {
		case codeUnsupportedEncoding:
			return announce(e, gunzipped, "unsupported_encoding")
		case codeUnsupportedFormat:
			return announce(e, gunzipped, "unsupported_format")
		}
		return err
	}
	if err != nil {
		if out.sendErr != nil || errors.Is(err, errCancelled) {
			return err
		}
		// Reading the member failed (over max_decompressed, or a corrupt
		// member or .gz member), maybe after part of it was sent: it is
		// announced again as skipped, which withdraws what was sent of it,
		// and the other members go on, as the server leaves out a member
		// it can't read.
		return announce(e, gunzipped, memberSkip(err))
	}
	if info.RefusedEncodingAt != nil {
		// UTF-32 part way: withdrawn and left out whole, as the server
		// leaves out a member that turns UTF-32 part way.
		return announce(e, gunzipped, "unsupported_encoding")
	}
	total.SourceSize += info.SourceSize
	for rule, count := range info.Counts {
		total.Counts[rule] += count
	}
	return nil
}

// HandleReadRangeRedacted answers a read_range with a `redact` block, as
// the server's redacted Tail (_follow_redacted): it reads up to
// followContextBytes before offset, for context only, and length bytes from
// offset (at most maxRangeLength in all), refuses content compressed in a
// format it doesn't decompress (unsupportedCompressedFormat), redacts the whole lines from offset (from the next line when offset
// isn't the start of one), sends an unfinished last line as the
// incomplete-line marker, and replaces a line with no end within the read
// by a marker, going on after it. When the redacted lines would take more
// than maxRangeLength bytes, it reads less, so the answer always fits.
// The file's first bytes are only used for the digests, never sent.
// withLineOffsets (the alerts) adds ChangedLines, so the server knows where
// each redacted line starts in the original.
//
// The file must really be at expectReal (openFile).
func HandleReadRangeRedacted(basePath, relPath, expectReal string, offset, length int64, red *redaction, withLineOffsets bool) (RedactedReadRangeResult, error) {
	if offset < 0 || length < 0 {
		return RedactedReadRangeResult{}, fmt.Errorf("invalid range: offset %d, length %d", offset, length)
	}
	f, closeFile, err := openFile(basePath, relPath, expectReal)
	if err != nil {
		return RedactedReadRangeResult{}, err
	}
	defer closeFile()
	st, err := f.Stat()
	if err != nil {
		return RedactedReadRangeResult{}, err
	}
	size := st.Size()
	head, err := readAt(f, 0, make([]byte, redact.HeadBytes)[:min(int64(redact.HeadBytes), size)])
	if err != nil {
		return RedactedReadRangeResult{}, err
	}
	encoding, err := redact.DetectEncoding(head)
	if err != nil {
		return RedactedReadRangeResult{}, &redactionError{
			code:    codeUnsupportedEncoding,
			message: "the content is in an encoding redaction doesn't support (UTF-32)",
		}
	}
	result := RedactedReadRangeResult{
		Size:       size,
		DataB64:    "",
		Redacted:   true,
		Digest:     red.rules.Digest(),
		NextOffset: offset,
		ReadEnd:    offset,
		HeadDigest: redact.HeadDigest(red.headKey, head, offset),
		Counts:     map[string]int{},
	}
	if withLineOffsets {
		none := [][2]int64{}
		result.ChangedLines = &none
	}
	if offset > size {
		// Rotated or truncated: the server starts over.
		result.NextHeadDigest = result.HeadDigest
		return result, nil
	}
	if unsupportedCompressedFormat(head) {
		return RedactedReadRangeResult{}, unsupportedFormatError()
	}
	if length == 0 {
		// Nothing asked for after the offset: nothing to send.
		result.NextHeadDigest = result.HeadDigest
		return result, nil
	}
	opts := red.opts
	opts.SourceEncoding = encoding
	readFrom := max(0, offset-followContextBytes)
	before := offset - readFrom
	// The context before the offset, then length bytes from it, at most
	// maxRangeLength in all, so something after the offset is always read
	// (before is at most followContextBytes, well under maxRangeLength).
	window := min(before+length, maxRangeLength)
	buf := make([]byte, maxRangeLength)
	for {
		data := []byte{}
		if readFrom < size {
			data, err = readAt(f, readFrom, buf[:min(window, size-readFrom)])
			if err != nil {
				return RedactedReadRangeResult{}, err
			}
		}
		read, err := redact.RedactWindow(data, int(min(before, int64(len(data)))), red.rules, readFrom, false, opts)
		if err != nil {
			return RedactedReadRangeResult{}, &redactionError{code: codeInvalidRedact, message: err.Error()}
		}
		body, counts, partial := read.Data, read.Counts, read.TrailingPartial
		readEnd := readFrom + int64(len(data))
		next := readFrom + int64(read.End)
		skipped := next <= offset && readEnd < size
		if skipped {
			// No line ends within what was read: skip it, fail closed. The
			// label is what was skipped, as the server's when it reads a
			// whole window.
			label := int(min(readEnd-offset, followWindowBytes))
			body = append(redact.Marker(redact.LongLineLabel(label)), '\n')
			counts, partial, next = map[string]int{}, 0, readEnd
		}
		// The Tail never goes back.
		next = max(next, offset)
		if len(body) > maxRangeLength && window-before <= 1 {
			// Not reachable (a marker fits), but never send more.
			return RedactedReadRangeResult{}, errors.New("the redacted read doesn't fit in one answer")
		}
		if len(body) <= maxRangeLength {
			if read.RefusedAt >= 0 && !skipped {
				// UTF-32 part way: the rest of what was read is refused.
				refused := readFrom + int64(read.RefusedAt)
				result.RefusedEncodingAt = &refused
			}
			if withLineOffsets && result.RefusedEncodingAt == nil {
				// The original lines the redacted ones stand for: those read,
				// or the line with no end that was skipped.
				originals := []int64{next - offset}
				if !skipped {
					// Read as UTF16Unsure from UnionAt on, the lines there end
					// at every newline byte.
					union := read.End
					if read.UnionAt >= 0 {
						union = read.UnionAt
					}
					originals = append(
						lineLengths(data[read.Begin:union], encoding, readFrom+int64(read.Begin)),
						lineLengths(data[union:read.End], redact.Bytes, readFrom+int64(union))...,
					)
				}
				changed, err := changedLines(body[:len(body)-partial], originals)
				if err != nil {
					return RedactedReadRangeResult{}, err
				}
				result.ChangedLines = &changed
			}
			result.DataB64 = base64.StdEncoding.EncodeToString(body)
			result.NextOffset = next
			result.ReadEnd = readEnd
			result.NextHeadDigest = redact.HeadDigest(red.headKey, head, next)
			result.Counts = counts
			result.TrailingPartial = partial
			result.Aligned = read.Aligned
			return result, nil
		}
		// Redacted, the lines take more room than the original (short
		// values, longer markers): read half as much after the offset.
		window = before + (window-before)/2
	}
}

// lineLengths splits block, whole lines of the original in encoding that
// start at file offset at, into the length of each line, its newline
// included (a UTF-16 newline counts only on an even offset, as the engine
// reads it).
func lineLengths(block []byte, encoding string, at int64) []int64 {
	newline, unit := []byte("\n"), int64(1)
	switch encoding {
	case redact.UTF16LE:
		newline, unit = []byte("\n\x00"), 2
	case redact.UTF16BE:
		newline, unit = []byte("\x00\n"), 2
	}
	var lengths []int64
	start, from := 0, 0
	for {
		i := bytes.Index(block[from:], newline)
		if i < 0 {
			return lengths
		}
		i += from
		if (at+int64(i))%unit != 0 {
			from = i + 1
			continue
		}
		end := i + len(newline)
		lengths = append(lengths, int64(end-start))
		start, from = end, end
	}
}

// changedLines pairs the redacted whole lines of output with the original
// lines they stand for (one for one: redaction never adds or removes a line
// break) and lists those whose length changed. Anything else is refused
// rather than sent with positions that would be wrong.
func changedLines(output []byte, originals []int64) ([][2]int64, error) {
	changed := [][2]int64{}
	index := 0
	for len(output) > 0 {
		i := bytes.IndexByte(output, '\n')
		if i < 0 || index >= len(originals) {
			return nil, errors.New("the redacted lines don't match the lines read")
		}
		if int64(i+1) != originals[index] {
			changed = append(changed, [2]int64{int64(index), originals[index]})
		}
		output = output[i+1:]
		index++
	}
	if index != len(originals) {
		return nil, errors.New("the redacted lines don't match the lines read")
	}
	return changed, nil
}
