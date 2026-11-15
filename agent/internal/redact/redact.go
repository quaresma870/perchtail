// Package redact replaces secrets, card numbers and personal data in file
// content with `[redacted: <label>]`, with exactly the behaviour of the
// backend's engine (backend/app/redaction/engine.py), so the agent can
// redact on its own host before content leaves it. The vectors both engines
// run are in testdata/vectors.json.
//
// Content is redacted as bytes, a line at a time, in streaming: Feed takes
// blocks of any size and returns the redacted complete lines, keeping the
// unfinished last line for the next block. Every rule matches within one
// line, so where blocks are cut never changes the result. See engine.py for
// the rules of long lines, private keys, unfinished lines and UTF-16.
package redact

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
)

const (
	// MaxLineBytes is the longest line kept whole; the rest of a longer
	// line becomes the long-line marker.
	MaxLineBytes = 1024 * 1024
	// PemMaxLines is how many lines a private-key BEGIN without an END
	// hides at most.
	PemMaxLines = 200
	// IncompleteLineLabel labels an unfinished last line of a read that
	// the file may continue past.
	IncompleteLineLabel = "incomplete line"
	// pemCarryBytes is how much of the content before a dropped piece of a
	// long line is read again with it, so a BEGIN or END split between two
	// pieces is still found.
	pemCarryBytes = 512
)

var blanks = []byte(" \t\r\v\f")

// Marker is the replacement text for a label.
func Marker(label string) []byte { return []byte("[redacted: " + label + "]") }

func sizeLabel(size int) string {
	switch {
	case size%(1024*1024) == 0:
		return fmt.Sprintf("%d MiB", size/(1024*1024))
	case size%1024 == 0:
		return fmt.Sprintf("%d KiB", size/1024)
	}
	return fmt.Sprintf("%d bytes", size)
}

// LongLineLabel labels the dropped rest of a line over maxLineBytes.
func LongLineLabel(maxLineBytes int) string { return "line over " + sizeLabel(maxLineBytes) }

// Options of a Redactor; the zero value is the backend's defaults.
type Options struct {
	// SourceEncoding is Bytes, UTF16LE, UTF16BE or UTF16Unsure (see
	// DetectEncoding).
	SourceEncoding string
	// OutputEncoding is "utf-8" (the default, what the viewer reads) or
	// "source" (UTF-16 encoded back, for downloads).
	OutputEncoding string
	// MaxLineBytes is MaxLineBytes when 0.
	MaxLineBytes int
	// PemMaxLines is PemMaxLines when 0.
	PemMaxLines int
	// Origin is the file offset of the first byte fed (it only matters for
	// UTF16Unsure, whose UTF-16 readings are aligned on even offsets).
	Origin int64
}

type pemState struct {
	open  bool
	lines int
}

// pemFound is, for one reading, where the last BEGIN of a dropped long
// line ends and where its last END starts (file offsets).
type pemFound struct {
	begin, end       int64
	hasBegin, hasEnd bool
}

// A span is a replacement candidate. Private-key spans have priority 0 and
// win any overlap; regex matches have priority 1, and the rule listed first
// wins among them.
type span struct {
	start, end int
	priority   int
	rule       int
	repl       []byte
	counted    bool
}

// merged is a replacement after overlaps are merged: rule is the index of
// the rule to count, or -1.
type merged struct {
	start, end int
	repl       []byte
	rule       int
}

// Redactor redacts one stream of content: Feed blocks in order, then call
// Finish once; Counts, SourceSize, SourceOffset and TrailingPartial are set
// by then. With no rules the content passes through unchanged.
type Redactor struct {
	rules       *Rules
	Passthrough bool
	union       bool
	origin      int64
	encoding    string
	unit        int
	newline     []byte
	reencode    bool
	maxLine     int
	longLine    []byte
	pemMax      int
	buffer      []byte
	consumed    int64 // source offset of buffer[0]
	lineStart   int64 // source offset of the line being read
	overflow    bool  // the current line is over maxLine
	lineOutput  int   // bytes already sent for the current line
	lineCounts  map[string]int
	pem         [3]pemState
	pemCarry    []byte
	pemFound    []pemFound
	// Counts is the number of replacements by rule name.
	Counts map[string]int
	// SourceSize is the number of bytes fed.
	SourceSize int64
	// SourceOffset is where the next read should start (an unfinished
	// last line is read again).
	SourceOffset int64
	// TrailingPartial is how many bytes at the end of the output stand for
	// that unfinished line.
	TrailingPartial int
}

// New makes a Redactor for compiled rules.
func New(rules *Rules, opts Options) (*Redactor, error) {
	if rules == nil {
		rules = &Rules{pemRule: -1}
	}
	newlines := map[string][]byte{
		Bytes:       []byte("\n"),
		UTF16LE:     []byte("\n\x00"),
		UTF16BE:     []byte("\x00\n"),
		UTF16Unsure: []byte("\n"),
	}
	if _, ok := newlines[opts.SourceEncoding]; !ok {
		return nil, fmt.Errorf("%w: can't redact %q content", ErrUnsupportedEncoding, opts.SourceEncoding)
	}
	output := opts.OutputEncoding
	if output == "" {
		output = "utf-8"
	}
	if output != "utf-8" && output != "source" {
		return nil, fmt.Errorf("output encoding must be utf-8 or source, not %q", output)
	}
	maxLine := opts.MaxLineBytes
	if maxLine == 0 {
		maxLine = MaxLineBytes
	}
	pemMax := opts.PemMaxLines
	if pemMax == 0 {
		pemMax = PemMaxLines
	}
	r := &Redactor{
		rules:       rules,
		Passthrough: len(rules.rules) == 0,
		union:       opts.SourceEncoding == UTF16Unsure,
		origin:      opts.Origin,
		lineCounts:  map[string]int{},
		Counts:      map[string]int{},
		pemMax:      pemMax,
	}
	encoding := opts.SourceEncoding
	if r.union {
		encoding = Bytes
	}
	r.encoding = encoding
	r.unit = 1
	if encoding != Bytes {
		r.unit = 2
	}
	r.newline = newlines[encoding]
	r.reencode = encoding != Bytes && output == "source"
	if maxLine < r.unit || pemMax < 1 {
		return nil, fmt.Errorf("max line bytes and pem max lines must be positive")
	}
	r.maxLine = maxLine - maxLine%r.unit
	r.longLine = Marker(LongLineLabel(maxLine))
	return r, nil
}

// Feed redacts the next block and returns the complete lines it finished.
func (r *Redactor) Feed(data []byte) []byte {
	r.SourceSize += int64(len(data))
	if r.Passthrough {
		return append([]byte(nil), data...)
	}
	r.buffer = append(r.buffer, data...)
	var out bytes.Buffer
	for {
		if r.overflow {
			index := r.findNewline(r.buffer, 0, len(r.buffer))
			at := r.origin + r.consumed
			if index < 0 {
				size := len(r.buffer) - len(r.buffer)%r.unit
				r.pemTailFeed(clone(r.buffer[:size]), at)
				r.drop(size)
				break
			}
			r.pemTailFeed(clone(r.buffer[:index]), at)
			r.pemTailEnd()
			r.drop(index + len(r.newline))
			out.Write(r.encodeMarker([]byte("\n")))
			r.overflow = false
			r.lineStart = r.consumed
			r.lineOutput = 0
			r.lineCounts = map[string]int{}
			continue
		}
		if index := r.rfindNewline(r.buffer); index >= 0 {
			end := index + len(r.newline)
			block := clone(r.buffer[:end])
			at := r.origin + r.consumed
			r.drop(end)
			out.Write(r.lines(block, at, true))
			r.lineStart = r.consumed
		}
		if len(r.buffer) > r.maxLine {
			r.lineCounts = map[string]int{}
			at := r.origin + r.consumed
			source := clone(r.buffer[:r.maxLine])
			piece := r.segment(source, at, true, true)
			piece = append(piece, r.encodeMarker(r.longLine)...)
			out.Write(piece)
			r.lineOutput = len(piece)
			r.overflow = true
			size := len(r.buffer) - len(r.buffer)%r.unit
			r.pemTailStart(source, at)
			r.pemTailFeed(clone(r.buffer[r.maxLine:size]), at+int64(r.maxLine))
			r.drop(size)
			continue
		}
		break
	}
	return out.Bytes()
}

// Finish ends the content. final is true at the end of the file or of what
// a download sends: the last line is redacted as it is. For a read the file
// may continue past (the viewer, a Tail poll), an unfinished last line is
// sent as `[redacted: incomplete line]`, SourceOffset is where it starts
// and TrailingPartial how many bytes of the output it took.
func (r *Redactor) Finish(final bool) []byte {
	r.TrailingPartial = 0
	if r.Passthrough {
		r.SourceOffset = r.SourceSize
		return nil
	}
	var out []byte
	switch {
	case r.overflow:
		if final {
			r.SourceOffset = r.SourceSize
		} else {
			r.TrailingPartial = r.lineOutput
			r.SourceOffset = r.lineStart
			for name, count := range r.lineCounts {
				r.Counts[name] -= count
				if r.Counts[name] == 0 {
					delete(r.Counts, name)
				}
			}
		}
	case len(r.buffer) > 0:
		if final {
			out = r.lines(clone(r.buffer), r.origin+r.consumed, true)
			r.SourceOffset = r.SourceSize
		} else {
			out = r.encodeMarker(Marker(IncompleteLineLabel))
			r.TrailingPartial = len(out)
			r.SourceOffset = r.lineStart
		}
	default:
		r.SourceOffset = r.SourceSize
	}
	r.buffer = nil
	return out
}

// SkipContext reads whole lines that come before the content to redact (at
// is the file offset of data[0]), only to know whether it starts inside a
// private-key block. Nothing is returned or counted.
func (r *Redactor) SkipContext(data []byte, at int64) {
	if !r.Passthrough && r.rules.pemBegin != nil && len(data) > 0 {
		r.lines(data, at, false)
	}
}

func clone(b []byte) []byte { return append([]byte(nil), b...) }

func (r *Redactor) drop(size int) {
	r.buffer = r.buffer[size:]
	r.consumed += int64(size)
	if cap(r.buffer) > 4*MaxLineBytes && len(r.buffer) < cap(r.buffer)/4 {
		r.buffer = clone(r.buffer)
	}
}

// findNewline is the first newline in data[start:end] at an offset that is
// a multiple of the unit (relative to data).
func (r *Redactor) findNewline(data []byte, start, end int) int {
	for start <= end {
		i := bytes.Index(data[start:end], r.newline)
		if i < 0 {
			return -1
		}
		i += start
		if i%r.unit == 0 {
			return i
		}
		start = i + 1
	}
	return -1
}

func (r *Redactor) rfindNewline(data []byte) int {
	index := bytes.LastIndex(data, r.newline)
	for index >= 0 && index%r.unit != 0 {
		index = bytes.LastIndex(data[:index+1], r.newline)
	}
	return index
}

func longestLine(block []byte) int {
	longest := 0
	for _, line := range bytes.Split(block, []byte("\n")) {
		if len(line) > longest {
			longest = len(line)
		}
	}
	return longest
}

// lines redacts a block of whole lines (the last may lack its newline only
// at the end of the content) that starts at file offset at, giving the
// lines over maxLine the long-line marker.
func (r *Redactor) lines(block []byte, at int64, emit bool) []byte {
	newline := r.newline
	if len(block) <= r.maxLine+len(newline) || (r.unit == 1 && longestLine(block) <= r.maxLine) {
		return r.redactSource(block, at, emit)
	}
	var out bytes.Buffer
	start, pending := 0, 0
	for start < len(block) {
		index := r.findNewline(block, start, len(block))
		end, after := len(block), len(block)
		if index >= 0 {
			end, after = index, index+len(newline)
		}
		if end-start > r.maxLine {
			if pending < start {
				out.Write(r.redactSource(block[pending:start], at+int64(pending), emit))
			}
			piece := block[start : start+r.maxLine]
			out.Write(r.segment(piece, at+int64(start), false, emit))
			r.pemTailStart(piece, at+int64(start))
			r.pemTailFeed(block[start+r.maxLine:end], at+int64(start+r.maxLine))
			r.pemTailEnd()
			if emit {
				out.Write(r.encodeMarker(r.longLine))
				if index >= 0 {
					out.Write(r.encodeMarker([]byte("\n")))
				}
			}
			pending = after
		}
		start = after
	}
	if pending < len(block) {
		out.Write(r.redactSource(block[pending:], at+int64(pending), emit))
	}
	return out.Bytes()
}

// segment redacts the first maxLine bytes of a long line: only the part
// before the cut point is kept. With line set it is counted in the line's
// counts and in the total.
func (r *Redactor) segment(piece []byte, at int64, line bool, emit bool) []byte {
	if !emit {
		r.scan(piece, at, false)
		return nil
	}
	base, spans := r.scan(piece, at, true)
	guard := r.rules.cutGuard()
	if r.union {
		guard *= 2
	}
	cut := cutPoint(base, spans, guard)
	var kept []merged
	for _, m := range spans {
		if m.end <= cut {
			kept = append(kept, m)
		}
	}
	counts := r.Counts
	if line {
		counts = r.lineCounts
	}
	out := r.fromWork(r.apply(base[:cut], kept, counts))
	if line {
		for name, count := range counts {
			r.Counts[name] += count
		}
	}
	return out
}

// cutPoint is where the kept start of a long line ends: at least guard
// bytes before the limit, then back to the last blank (or to the start of
// the line when there is none), then back to the start of a replacement the
// point would cut (the backend's _cut_point).
func cutPoint(base []byte, spans []merged, guard int) int {
	cut := len(base) - guard
	if cut <= 0 {
		return 0
	}
	best := -1
	for _, blank := range blanks {
		if i := bytes.LastIndexByte(base[:cut+1], blank); i > best {
			best = i
		}
	}
	cut = best
	if cut <= 0 {
		return 0
	}
	for _, m := range spans {
		if m.start < cut && cut < m.end {
			return m.start
		}
		if m.start >= cut {
			break
		}
	}
	return cut
}

// readings are the readings of a piece of content the private-key state is
// kept for, in the order of r.pem.
func (r *Redactor) readings(source []byte, at int64) []view {
	skip := int(at % 2)
	switch {
	case r.union:
		return []view{&bytesView{source}, newUTF16View(source, false, skip), newUTF16View(source, true, skip)}
	case r.encoding == Bytes:
		return []view{&bytesView{source}}
	}
	return []view{newUTF16View(source, r.encoding == UTF16BE, skip)}
}

// pemTailStart: a line went over maxLine after piece (its kept part). The
// rest of the line is dropped unseen, but a private-key BEGIN in it still
// has to hide the lines that follow, so it is read for BEGIN and END.
func (r *Redactor) pemTailStart(piece []byte, at int64) {
	if r.rules.pemBegin == nil {
		return
	}
	if len(piece) > pemCarryBytes {
		piece = piece[len(piece)-pemCarryBytes:]
	}
	r.pemCarry = clone(piece)
	r.pemFound = make([]pemFound, len(r.readings(nil, at)))
}

func (r *Redactor) pemTailFeed(source []byte, at int64) {
	if r.rules.pemBegin == nil || len(source) == 0 {
		return
	}
	text := append(clone(r.pemCarry), source...)
	textAt := at - int64(len(r.pemCarry))
	for i, v := range r.readings(text, textAt) {
		found := &r.pemFound[i]
		work := subject{work: v.text()}
		r.rules.pemBegin.findAll(work.text(r.rules.pemBegin), func(m []int) {
			_, e := v.sourceRange(m[0], m[1])
			end := textAt + int64(e)
			if !found.hasBegin || end > found.begin {
				found.begin, found.hasBegin = end, true
			}
		})
		r.rules.pemEnd.findAll(work.text(r.rules.pemEnd), func(m []int) {
			s, _ := v.sourceRange(m[0], m[1])
			start := textAt + int64(s)
			if !found.hasEnd || start > found.end {
				found.end, found.hasEnd = start, true
			}
		})
	}
	if len(text) > pemCarryBytes {
		text = text[len(text)-pemCarryBytes:]
	}
	r.pemCarry = text
}

// pemTailEnd: the long line ended. A BEGIN in it with no END after it opens
// a private-key block over the next lines; an END after the last BEGIN
// closes one.
func (r *Redactor) pemTailEnd() {
	if r.rules.pemBegin == nil {
		return
	}
	for i := 0; i < len(r.pem) && i < len(r.pemFound); i++ {
		found, state := r.pemFound[i], &r.pem[i]
		if !found.hasBegin && !found.hasEnd {
			continue
		}
		if found.hasBegin && (!found.hasEnd || found.end < found.begin) {
			state.open = r.pemMax > 1
			state.lines = 1
		} else {
			state.open = false
		}
	}
	r.pemFound = nil
	r.pemCarry = nil
}

func (r *Redactor) toWork(source []byte) []byte {
	if r.encoding == Bytes {
		return source
	}
	return decodeUTF16(source, r.encoding == UTF16BE)
}

func (r *Redactor) fromWork(work []byte) []byte {
	if !r.reencode {
		return work
	}
	return encodeUTF16(work, r.encoding == UTF16BE)
}

func (r *Redactor) encodeMarker(text []byte) []byte {
	if r.reencode {
		return encodeUTF16(text, r.encoding == UTF16BE)
	}
	return clone(text)
}

func (r *Redactor) redactSource(source []byte, at int64, emit bool) []byte {
	if !emit {
		r.scan(source, at, false)
		return nil
	}
	base, spans := r.scan(source, at, true)
	return r.fromWork(r.apply(base, spans, r.Counts))
}

// scan runs the rules on whole lines of source content that starts at file
// offset at: the text the output is built from (the content, or its UTF-8
// for UTF-16) and the merged replacements in it. For UTF16Unsure content
// the replacements found in its UTF-16 LE and BE readings are added, in
// source offsets. With collect false only the private-key state moves on.
func (r *Redactor) scan(source []byte, at int64, collect bool) ([]byte, []merged) {
	work := r.toWork(source)
	type reading struct {
		v     *utf16View
		state *pemState
	}
	var views []reading
	if r.union {
		skip := int(at % 2)
		views = []reading{{newUTF16View(source, false, skip), &r.pem[1]}, {newUTF16View(source, true, skip), &r.pem[2]}}
	}
	if !collect {
		r.pemSpans(work, nil, &r.pem[0])
		for _, v := range views {
			r.pemSpans(v.v.work, nil, v.state)
		}
		return work, nil
	}
	spans := r.collect(work, &r.pem[0])
	for _, v := range views {
		for _, s := range r.collect(v.v.work, v.state) {
			s.start, s.end = v.v.sourceRange(s.start, s.end)
			spans = append(spans, s)
		}
	}
	return work, mergeSpans(spans)
}

func (r *Redactor) collect(work []byte, pem *pemState) []span {
	var spans []span
	r.pemSpans(work, &spans, pem)
	sub := subject{work: work}
	for index, ru := range r.rules.rules {
		text := sub.text(ru.re)
		if ru.check == "iban" {
			// With overlap: a failed match is searched again from the byte
			// after its start, so a code that looks like the start of an
			// IBAN doesn't hide the IBAN.
			lines(text, func(lineStart, lineEnd int) {
				line := text[lineStart:lineEnd]
				position := 0
				for {
					m := ru.re.searchFrom(line, position)
					if m == nil {
						return
					}
					start, end := m[2*ru.group]+lineStart, m[2*ru.group+1]+lineStart
					parts := r.check(ru, m, lineStart, work, start, end)
					for _, p := range parts {
						spans = append(spans, span{start + p.start, start + p.end, 1, index, Marker(p.label), true})
					}
					if len(parts) > 0 {
						position = start + parts[len(parts)-1].end - lineStart
					} else {
						position = m[0] + 1
					}
				}
			})
			continue
		}
		ru.re.findAll(text, func(m []int) {
			start, end := m[2*ru.group], m[2*ru.group+1]
			if start < 0 || start == end {
				return
			}
			if ru.check == "" {
				spans = append(spans, span{start, end, 1, index, Marker(ru.label), true})
				return
			}
			for _, p := range r.check(ru, m, 0, work, start, end) {
				spans = append(spans, span{start + p.start, start + p.end, 1, index, Marker(p.label), true})
			}
		})
	}
	return spans
}

type part struct {
	start, end int
	label      string
}

// check runs a detector's verification on work[start:end]: the parts to
// replace (relative to start) with their labels, none to leave the match
// alone. m holds the match's submatch indexes, offset by base.
func (r *Redactor) check(ru rule, m []int, base int, work []byte, start, end int) []part {
	value := work[start:end]
	switch ru.check {
	case "luhn_iin":
		var out []part
		for _, c := range cardSpans(value) {
			out = append(out, part{c.start, c.end, replaceLast4(ru.label, c.last4)})
		}
		return out
	case "iban":
		if n := ibanLength(value); n >= 0 {
			return []part{{0, n, ru.label}}
		}
		return nil
	case "ipv6":
		following := -1
		if end < len(work) {
			following = int(work[end])
		}
		if n := ipv6Length(value, following); n >= 0 {
			return []part{{0, n, ru.label}}
		}
		return nil
	case "kv_value":
		if !kvValueOK(value) {
			return nil
		}
		var key []byte
		if ru.keyGroup >= 0 && m[2*ru.keyGroup] >= 0 {
			key = work[m[2*ru.keyGroup]+base : m[2*ru.keyGroup+1]+base]
		}
		return []part{{0, len(value), keyLabel(key, ru.label)}}
	}
	panic(fmt.Sprintf("redact: unknown check %q", ru.check))
}

func replaceLast4(label, last4 string) string {
	return string(bytes.ReplaceAll([]byte(label), []byte("{last4}"), []byte(last4)))
}

// mergeSpans makes overlapping spans one replacement: a private key wins,
// then the rule listed first, then the span that starts first.
func mergeSpans(spans []span) []merged {
	sort.SliceStable(spans, func(i, j int) bool {
		if spans[i].start != spans[j].start {
			return spans[i].start < spans[j].start
		}
		return spans[i].end < spans[j].end
	})
	var out []merged
	for index := 0; index < len(spans); {
		best := spans[index]
		start, end := best.start, best.end
		index++
		for index < len(spans) && spans[index].start < end {
			other := spans[index]
			if other.end > end {
				end = other.end
			}
			if other.priority < best.priority ||
				other.priority == best.priority && (other.rule < best.rule ||
					other.rule == best.rule && other.start < best.start) {
				best = other
			}
			index++
		}
		rule := -1
		if best.counted {
			rule = best.rule
		}
		out = append(out, merged{start, end, best.repl, rule})
	}
	return out
}

// apply builds the output from base and the replacements. It always returns
// a new slice: callers append markers to it, which must never write into
// the content base shares memory with (a long line's kept part is read
// again for private keys).
func (r *Redactor) apply(base []byte, spans []merged, counts map[string]int) []byte {
	if len(spans) == 0 {
		return clone(base)
	}
	out := make([]byte, 0, len(base))
	position := 0
	for _, m := range spans {
		out = append(out, base[position:m.start]...)
		out = append(out, m.repl...)
		position = m.end
		if m.rule >= 0 {
			counts[r.rules.rules[m.rule].name]++
		}
	}
	return append(out, base[position:]...)
}

// HeadDigest identifies a file by its first min(HeadBytes, offset) bytes
// for the Tail and the alerts: HMAC-SHA256 with a per-source key, truncated
// to 128 bits, so a short, guessable start of a file can't be confirmed
// offline from the digest.
func HeadDigest(key, start []byte, offset int64) string {
	n := int64(HeadBytes)
	if offset < n {
		n = offset
	}
	if int64(len(start)) < n {
		n = int64(len(start))
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(start[:n])
	return hex.EncodeToString(mac.Sum(nil))[:32]
}

// Result is what a whole redaction gave.
type Result struct {
	Counts          map[string]int
	SourceSize      int64
	SourceOffset    int64
	TrailingPartial int
	OutputSize      int
}

// RedactBytes redacts content held in memory in one go.
func RedactBytes(data []byte, rules *Rules, final bool, opts Options) ([]byte, Result, error) {
	r, err := New(rules, opts)
	if err != nil {
		return nil, Result{}, err
	}
	out := append(r.Feed(data), r.Finish(final)...)
	return out, Result{r.Counts, r.SourceSize, r.SourceOffset, r.TrailingPartial, len(out)}, nil
}

// WindowResult is a redacted read from the middle of a file. Offsets index
// the data passed in: the output is the redaction of data[Begin:End] (plus
// the incomplete-line marker when TrailingPartial > 0), and the next read
// should start at End.
type WindowResult struct {
	Data            []byte
	Counts          map[string]int
	Begin, End      int
	Aligned         bool
	TrailingPartial int
}

// RedactWindow redacts data[start:] of a read that began baseOffset bytes
// into the file, for a Tail poll or "open only the end". data[:start] is
// context read before the offset: never returned, only read to know whether
// start falls inside a private-key block. When start isn't the start of a
// line, the output starts at the next line instead and Aligned is true, so
// a value cut by the offset is never shown in part.
func RedactWindow(data []byte, start int, rules *Rules, baseOffset int64, final bool, opts Options) (WindowResult, error) {
	r, err := New(rules, opts)
	if err != nil {
		return WindowResult{}, err
	}
	if r.Passthrough {
		r.Feed(data[start:])
		return WindowResult{clone(data[start:]), map[string]int{}, start, len(data), false, 0}, nil
	}
	unit := int64(r.unit)
	newline := r.newline
	find := func(begin, end int) int {
		for begin <= end {
			i := bytes.Index(data[begin:end], newline)
			if i < 0 {
				return -1
			}
			i += begin
			if (baseOffset+int64(i))%unit == 0 {
				return i
			}
			begin = i + 1
		}
		return -1
	}
	begin := start
	atLineStart := baseOffset+int64(start) == 0 ||
		(start >= len(newline) && (baseOffset+int64(start))%unit == 0 &&
			bytes.Equal(data[start-len(newline):start], newline))
	if !atLineStart {
		if i := find(start, len(data)); i < 0 {
			begin = len(data)
		} else {
			begin = i + len(newline)
		}
	}
	context := 0
	if baseOffset != 0 {
		if i := find(0, begin); i < 0 {
			context = begin
		} else {
			context = i + len(newline)
		}
	}
	r.origin = baseOffset + int64(begin)
	r.SkipContext(clone(data[context:begin]), baseOffset+int64(context))
	out := append(r.Feed(data[begin:]), r.Finish(final)...)
	return WindowResult{
		Data:            out,
		Counts:          r.Counts,
		Begin:           begin,
		End:             begin + int(r.SourceOffset),
		Aligned:         begin != start,
		TrailingPartial: r.TrailingPartial,
	}, nil
}
