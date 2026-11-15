package redact

import (
	"bytes"
	"encoding/binary"
	"errors"
	"sort"
	"unicode/utf16"
	"unicode/utf8"
)

// The encodings content is redacted in (backend/app/redaction/encodings.py).
const (
	// Bytes is content redacted as bytes (any ASCII-compatible encoding).
	Bytes = ""
	// UTF16LE and UTF16BE are clearly UTF-16 content: decoded, redacted as
	// UTF-8, served as UTF-8 or encoded back.
	UTF16LE = "utf-16-le"
	UTF16BE = "utf-16-be"
	// UTF16Unsure is content that may be UTF-16 without being clearly so:
	// served as bytes, the detectors run on the bytes and on its UTF-16 LE
	// and BE readings, and the union is replaced.
	UTF16Unsure = "utf-16-unsure"
)

// HeadBytes is how much of the start of a file decides its encoding (and
// identifies it for a Tail, see HeadDigest).
const HeadBytes = 64

// ErrUnsupportedEncoding is content that can't be redacted (UTF-32): it is
// refused rather than served as is. Its code is "unsupported_encoding".
var ErrUnsupportedEncoding = errors.New("unsupported_encoding")

// DetectEncoding decides how content is redacted from its first HeadBytes,
// as encodings.detect_encoding does.
func DetectEncoding(head []byte) (string, error) {
	if len(head) > HeadBytes {
		head = head[:HeadBytes]
	}
	if bytes.HasPrefix(head, []byte{0xff, 0xfe, 0, 0}) || bytes.HasPrefix(head, []byte{0, 0, 0xfe, 0xff}) {
		return "", ErrUnsupportedEncoding
	}
	if bytes.HasPrefix(head, []byte{0xff, 0xfe}) {
		return UTF16LE, nil
	}
	if bytes.HasPrefix(head, []byte{0xfe, 0xff}) {
		return UTF16BE, nil
	}
	quads := len(head) / 4
	if quads >= 2 {
		allLE, allBE := true, true
		for i := 0; i < quads; i++ {
			q := head[4*i : 4*i+4]
			if !(q[0] != 0 && q[1] == 0 && q[2] == 0 && q[3] == 0) {
				allLE = false
			}
			if !(q[0] == 0 && q[1] == 0 && q[2] == 0 && q[3] != 0) {
				allBE = false
			}
		}
		if allLE || allBE {
			return "", ErrUnsupportedEncoding
		}
	}
	pairs := len(head) / 2
	if pairs < 2 {
		return Bytes, nil
	}
	little, big := 0, 0
	for i := 0; i < pairs; i++ {
		p := head[2*i : 2*i+2]
		if p[0] != 0 && p[1] == 0 {
			little++
		}
		if p[0] == 0 && p[1] != 0 {
			big++
		}
	}
	switch {
	case little*4 >= pairs*3 && big == 0:
		return UTF16LE, nil
	case big*4 >= pairs*3 && little == 0:
		return UTF16BE, nil
	case little*4 >= pairs || big*4 >= pairs:
		return UTF16Unsure, nil
	}
	return Bytes, nil
}

func units(source []byte, big bool) []uint16 {
	out := make([]uint16, len(source)/2)
	for i := range out {
		a, b := uint16(source[2*i]), uint16(source[2*i+1])
		if big {
			out[i] = a<<8 | b
		} else {
			out[i] = b<<8 | a
		}
	}
	return out
}

// decodeUTF16 is Python's bytes.decode("utf-16-le"/"utf-16-be", "replace")
// to UTF-8: surrogate pairs joined, a lone surrogate and a last odd byte
// each become U+FFFD.
func decodeUTF16(source []byte, big bool) []byte {
	runes := utf16.Decode(units(source, big))
	out := make([]byte, 0, len(source))
	for _, r := range runes {
		out = utf8.AppendRune(out, r)
	}
	if len(source)%2 == 1 {
		out = utf8.AppendRune(out, utf8.RuneError)
	}
	return out
}

// encodeUTF16 is Python's work.decode("utf-8", "replace").encode(...): each
// maximal invalid subsequence of the UTF-8 becomes one U+FFFD.
func encodeUTF16(work []byte, big bool) []byte {
	runes := decodeUTF8Replace(work)
	encoded := utf16.Encode(runes)
	out := make([]byte, 0, 2*len(encoded))
	for _, u := range encoded {
		if big {
			out = binary.BigEndian.AppendUint16(out, u)
		} else {
			out = binary.LittleEndian.AppendUint16(out, u)
		}
	}
	return out
}

// decodeUTF8Replace decodes UTF-8 the way Python's "replace" error handler
// does: a maximal subpart of an ill-formed sequence (Unicode 3.9, the
// W3C/WHATWG practice) becomes one U+FFFD, where Go's decoder gives one per
// byte.
func decodeUTF8Replace(b []byte) []rune {
	out := make([]rune, 0, len(b))
	for i := 0; i < len(b); {
		r, size := utf8.DecodeRune(b[i:])
		if r != utf8.RuneError || size > 1 {
			out = append(out, r)
			i += size
			continue
		}
		out = append(out, utf8.RuneError)
		i += maximalSubpart(b[i:])
	}
	return out
}

// maximalSubpart is the length of the ill-formed sequence at the start of
// b to replace with one U+FFFD (at least 1).
func maximalSubpart(b []byte) int {
	lead := b[0]
	var need int
	low, high := byte(0x80), byte(0xBF)
	switch {
	case lead >= 0xC2 && lead <= 0xDF:
		need = 1
	case lead == 0xE0:
		need, low = 2, 0xA0
	case lead >= 0xE1 && lead <= 0xEC, lead == 0xEE, lead == 0xEF:
		need = 2
	case lead == 0xED:
		need, high = 2, 0x9F
	case lead == 0xF0:
		need, low = 3, 0x90
	case lead >= 0xF1 && lead <= 0xF3:
		need = 3
	case lead == 0xF4:
		need, high = 3, 0x8F
	default:
		return 1
	}
	n := 1
	for k := 0; k < need && n < len(b); k++ {
		c := b[n]
		if k == 0 {
			if c < low || c > high {
				return n
			}
		} else if c < 0x80 || c > 0xBF {
			return n
		}
		n++
	}
	return n
}

// utf16View is one UTF-16 reading (LE or BE) of UTF16Unsure content, as
// UTF-8 to run the rules on, with the way back to source offsets (the
// backend's _Utf16View). skip is 1 when the piece starts at an odd offset
// of the file. Surrogates aren't joined: every unit is one character of two
// source bytes (a last odd byte is one), a surrogate becomes U+FFFD, and a
// unit with a newline byte on either side is a newline.
type utf16View struct {
	work   []byte
	skip   int
	size   int
	starts []int // UTF-8 offset of each character, when not all ASCII
}

func newUTF16View(source []byte, big bool, skip int) *utf16View {
	if skip > len(source) {
		skip = len(source)
	}
	end := skip + (len(source)-skip)/2*2
	text := make([]rune, 0, (end-skip)/2+1)
	for _, u := range units(source[skip:end], big) {
		r := rune(u)
		// The newline rule wins over the surrogate one (0xD80A is a
		// newline), as in the backend's translation table.
		switch {
		case u&0xFF00 == 0x0A00 || u&0x00FF == 0x0A:
			r = '\n'
		case u >= 0xD800 && u < 0xE000:
			r = utf8.RuneError
		}
		text = append(text, r)
	}
	if end < len(source) {
		if source[end] == '\n' {
			text = append(text, '\n')
		} else {
			text = append(text, utf8.RuneError)
		}
	}
	v := &utf16View{skip: skip, size: len(source)}
	work := make([]byte, 0, len(text))
	ascii := true
	for _, r := range text {
		if r >= 0x80 {
			ascii = false
		}
		work = utf8.AppendRune(work, r)
	}
	v.work = work
	if !ascii {
		v.starts = make([]int, len(text)+1)
		offset := 0
		for i, r := range text {
			v.starts[i] = offset
			offset += utf8.RuneLen(r)
		}
		v.starts[len(text)] = offset
	}
	return v
}

func (v *utf16View) char(offset int) int {
	if v.starts == nil {
		return offset
	}
	// bisect_right(starts, offset) - 1
	return sort.Search(len(v.starts), func(i int) bool { return v.starts[i] > offset }) - 1
}

// sourceRange maps a [start, end) of the reading's work text back to source
// offsets of the piece.
func (v *utf16View) sourceRange(start, end int) (int, int) {
	first := v.char(start)
	last := v.char(end - 1)
	s := v.skip + 2*first
	e := v.skip + 2*last + 2
	if e > v.size {
		e = v.size
	}
	return s, e
}

func (v *utf16View) text() []byte { return v.work }

// bytesView is the bytes reading of a piece of content.
type bytesView struct{ work []byte }

func (v *bytesView) sourceRange(start, end int) (int, int) { return start, end }
func (v *bytesView) text() []byte                          { return v.work }

type view interface {
	sourceRange(start, end int) (int, int)
	text() []byte
}
