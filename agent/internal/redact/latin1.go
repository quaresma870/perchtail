package redact

import (
	"fmt"
	"regexp/syntax"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// A pure-ASCII pattern runs in RE2's Latin-1 mode on the backend, where
// each byte is one character, U+0000 to U+00FF, and where:
//
//   - an escape above \xff (\x{100}, \x{fffd}, \400) is refused;
//   - (?i) folds only the ASCII letters: (?i)\xE9 matches é but not É,
//     (?i)[^\xc9] still matches é, (?i)\p{Lu} adds a-z but not à-þ.
//
// A decode pattern (see pattern) runs here on each line with every byte of
// 0x80 or over turned into the private-use code point U+E000 plus the byte
// (U+E080 to U+E0FF), which no Unicode class holds and which (?i) never
// folds, and with the pattern rewritten to match (latin1Rewrite): escapes
// of 0x80 to 0xFF become those code points, \p and \P classes become the
// explicit list of the ASCII and Latin-1 characters they hold, and a class
// range from ASCII into 0x80 to 0xFF is split at 0x7F. The decoded line
// holds only ASCII and U+E080 to U+E0FF, so `.`, negated classes, \W and
// \S match a high byte as RE2 does, \w, \d, \s and \b stay ASCII, and
// (?i) folds the ASCII letters only (KELVIN SIGN and LONG S, which Go adds
// for k and s, can't occur).

const highBase = 0xE000

// noRune stands in for an empty Unicode class: the decoded text never holds it.
const noRune = 0x10FFFF

// namesHighBytes reports whether a Latin-1 pattern can tell one byte of
// 0x80 or over from another, or matches U+FFFD (see pattern): it has a
// literal, or a character class, that holds some of U+0080 to U+00FF but
// not all of them, or holds U+FFFD without all of them, or all of them
// without U+FFFD.
func namesHighBytes(re *syntax.Regexp) bool {
	switch re.Op {
	case syntax.OpLiteral:
		for _, r := range re.Rune {
			if highRune(r) {
				return true
			}
			if re.Flags&syntax.FoldCase != 0 {
				for f := unicode.SimpleFold(r); f != r; f = unicode.SimpleFold(f) {
					if highRune(f) {
						return true
					}
				}
			}
		}
	case syntax.OpCharClass:
		high, fffd := 0, false
		for i := 0; i+1 < len(re.Rune); i += 2 {
			lo, hi := re.Rune[i], re.Rune[i+1]
			if lo <= utf8.RuneError && utf8.RuneError <= hi {
				fffd = true
			}
			if lo < 0x80 {
				lo = 0x80
			}
			if hi > 0xFF {
				hi = 0xFF
			}
			if lo <= hi {
				high += int(hi-lo) + 1
			}
		}
		all := high == 0x80
		if !(all && fffd) && !(high == 0 && !fffd) {
			return true
		}
	}
	for _, sub := range re.Sub {
		if namesHighBytes(sub) {
			return true
		}
	}
	return false
}

// highRune: a code point the 0x80 stand-in byte can't stand for exactly.
func highRune(r rune) bool { return (r >= 0x80 && r <= 0xFF) || r == utf8.RuneError }

// latin1Decode is line with every byte of 0x80 or over turned into U+E000
// plus the byte, in UTF-8, and for each byte of it (and its end) the offset
// in line it came from.
func latin1Decode(line []byte) ([]byte, []int) {
	out := make([]byte, 0, 3*len(line))
	offsets := make([]int, 0, 3*len(line)+1)
	for i, c := range line {
		if c < 0x80 {
			out = append(out, c)
			offsets = append(offsets, i)
			continue
		}
		out = utf8.AppendRune(out, highBase+rune(c))
		offsets = append(offsets, i, i, i)
	}
	return out, append(offsets, len(line))
}

func hasHighByte(b []byte) bool {
	for _, c := range b {
		if c >= 0x80 {
			return true
		}
	}
	return false
}

func escapeRune(b *strings.Builder, r rune) {
	fmt.Fprintf(b, `\x{%X}`, r)
}

// mapped is the code point a Latin-1 character r runs as in a decode pattern.
func mapped(r rune) rune {
	if r >= 0x80 {
		return highBase + r
	}
	return r
}

// latin1Rewrite is a pure-ASCII pattern rewritten for the decoded form (see
// above). It refuses an escape above \xff, as RE2 does in Latin-1, and a \p
// class it can't list. What it doesn't understand it copies as it is, for
// regexp.Compile to refuse.
func latin1Rewrite(s string) (string, error) {
	var b strings.Builder
	for i := 0; i < len(s); {
		switch {
		case s[i] == '[':
			end, err := rewriteClass(&b, s, i)
			if err != nil {
				return "", err
			}
			i = end
		case s[i] == '\\' && i+1 < len(s) && s[i+1] == 'Q':
			end := strings.Index(s[i+2:], `\E`)
			if end < 0 {
				b.WriteString(s[i:])
				return b.String(), nil
			}
			b.WriteString(s[i : i+2+end+2])
			i += 2 + end + 2
		case s[i] == '\\' && i+1 < len(s) && (s[i+1] == 'p' || s[i+1] == 'P'):
			set, end, ok, err := unicodeClass(s, i)
			if err != nil {
				return "", err
			}
			if !ok {
				b.WriteString(s[i:])
				return b.String(), nil
			}
			b.WriteByte('[')
			if !writeSet(&b, set) {
				escapeRune(&b, noRune)
			}
			b.WriteByte(']')
			i = end
		case s[i] == '\\':
			r, end, ok := valueEscape(s, i)
			if !ok {
				n := 2
				if i+1 >= len(s) {
					n = 1
				}
				b.WriteString(s[i : i+n])
				i += n
				continue
			}
			if r > 0xFF {
				return "", errHighEscape
			}
			if r >= 0x80 {
				escapeRune(&b, mapped(r))
			} else {
				b.WriteString(s[i:end])
			}
			i = end
		default:
			b.WriteByte(s[i])
			i++
		}
	}
	return b.String(), nil
}

var errHighEscape = fmt.Errorf(`an escape above \xff isn't supported in a pure-ASCII pattern`)

// valueEscape reads the character escape at s[i] (a backslash) the way Go's
// regexp/syntax does: \x hex, octal, \a \f \n \r \t \v, or an escaped
// punctuation character. ok is false for anything else.
func valueEscape(s string, i int) (r rune, end int, ok bool) {
	if i+1 >= len(s) {
		return 0, 0, false
	}
	c, j := s[i+1], i+2
	switch {
	case c == 'x':
		if j < len(s) && s[j] == '{' {
			close := strings.IndexByte(s[j:], '}')
			if close < 2 {
				return 0, 0, false
			}
			v, err := strconv.ParseUint(s[j+1:j+close], 16, 32)
			if err != nil || v > unicode.MaxRune {
				return 0, 0, false
			}
			return rune(v), j + close + 1, true
		}
		if j+2 > len(s) {
			return 0, 0, false
		}
		v, err := strconv.ParseUint(s[j:j+2], 16, 8)
		if err != nil {
			return 0, 0, false
		}
		return rune(v), j + 2, true
	case c >= '1' && c <= '7' && (j >= len(s) || s[j] < '0' || s[j] > '7'):
		return 0, 0, false
	case c >= '0' && c <= '7':
		v := rune(c - '0')
		for n := 1; n < 3 && j < len(s) && s[j] >= '0' && s[j] <= '7'; n++ {
			v = v*8 + rune(s[j]-'0')
			j++
		}
		return v, j, true
	}
	switch c {
	case 'a':
		return '\a', j, true
	case 'f':
		return '\f', j, true
	case 'n':
		return '\n', j, true
	case 'r':
		return '\r', j, true
	case 't':
		return '\t', j, true
	case 'v':
		return '\v', j, true
	}
	if c < utf8.RuneSelf && !isASCIIAlnum(c) {
		return rune(c), j, true
	}
	return 0, 0, false
}

// unicodeClass reads the \p or \P class at s[i]: the Latin-1 characters it
// holds. ok is false when it is malformed (regexp.Compile refuses it).
func unicodeClass(s string, i int) (set [0x100]bool, end int, ok bool, err error) {
	negate := s[i+1] == 'P'
	j := i + 2
	if j >= len(s) {
		return set, 0, false, nil
	}
	name := s[j : j+1]
	end = j + 1
	if s[j] == '{' {
		close := strings.IndexByte(s[j:], '}')
		if close < 0 {
			return set, 0, false, nil
		}
		name, end = s[j+1:j+close], j+close+1
	}
	if strings.HasPrefix(name, "^") {
		negate, name = !negate, name[1:]
	}
	var table *unicode.RangeTable
	if name != "Any" {
		if table = unicode.Categories[name]; table == nil {
			if table = unicode.Scripts[name]; table == nil {
				return set, 0, false, fmt.Errorf("the Unicode class %q isn't supported", name)
			}
		}
	}
	for r := range set {
		set[r] = (table == nil || unicode.Is(table, rune(r))) != negate
	}
	return set, end, true, nil
}

// writeSet writes the ranges of set in the decoded form; false when empty.
func writeSet(b *strings.Builder, set [0x100]bool) bool {
	wrote := false
	for r := rune(0); r < 0x100; {
		if !set[r] {
			r++
			continue
		}
		start := r
		for r < 0x100 && set[r] {
			r++
		}
		writeRange(b, start, r-1)
		wrote = true
	}
	return wrote
}

// writeRange writes the Latin-1 range lo-hi in the decoded form, split at
// 0x7F so it holds no other code point.
func writeRange(b *strings.Builder, lo, hi rune) {
	if lo < 0x80 && hi >= 0x80 {
		writeRange(b, lo, 0x7F)
		lo = 0x80
	}
	escapeRune(b, mapped(lo))
	if hi != lo {
		b.WriteByte('-')
		escapeRune(b, mapped(hi))
	}
}

// classChar reads one character of a class at s[i]: a plain character or a
// character escape.
func classChar(s string, i int) (rune, int, bool) {
	if s[i] == '\\' {
		return valueEscape(s, i)
	}
	return rune(s[i]), i + 1, true
}

// rewriteClass rewrites the class that starts at s[i] ('[') and returns the
// offset after it.
func rewriteClass(b *strings.Builder, s string, i int) (int, error) {
	b.WriteByte('[')
	i++
	if i < len(s) && s[i] == '^' {
		b.WriteByte('^')
		i++
	}
	for first := true; i < len(s) && (s[i] != ']' || first); first = false {
		if strings.HasPrefix(s[i:], "[:") {
			if close := strings.Index(s[i+2:], ":]"); close >= 0 {
				b.WriteString(s[i : i+2+close+2])
				i += 2 + close + 2
				continue
			}
		}
		if s[i] == '\\' && i+1 < len(s) {
			switch s[i+1] {
			case 'p', 'P':
				set, end, ok, err := unicodeClass(s, i)
				if err != nil {
					return 0, err
				}
				if !ok {
					b.WriteString(s[i:])
					return len(s), nil
				}
				if !writeSet(b, set) {
					escapeRune(b, noRune)
				}
				i = end
				continue
			case 'd', 'D', 's', 'S', 'w', 'W':
				b.WriteString(s[i : i+2])
				i += 2
				continue
			}
		}
		lo, j, ok := classChar(s, i)
		if !ok {
			b.WriteString(s[i:])
			return len(s), nil
		}
		hi, end := lo, j
		if j+1 < len(s) && s[j] == '-' && s[j+1] != ']' {
			if hi, end, ok = classChar(s, j+1); !ok {
				b.WriteString(s[i:])
				return len(s), nil
			}
		}
		switch {
		case lo > 0xFF || hi > 0xFF:
			return 0, errHighEscape
		case hi < 0x80 || lo > hi:
			b.WriteString(s[i:end])
		default:
			writeRange(b, lo, hi)
		}
		i = end
	}
	if i < len(s) {
		b.WriteByte(']')
		i++
	}
	return i, nil
}
