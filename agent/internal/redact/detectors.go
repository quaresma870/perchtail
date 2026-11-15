package redact

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"regexp/syntax"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"
)

// detectorsJSON is a byte-for-byte copy of backend/app/redaction/detectors.json
// (a backend test fails when the two differ): the agent can't read the
// backend's copy, and the backend image doesn't carry agent/.
//
//go:embed detectors.json
var detectorsJSON []byte

type detectorSpec struct {
	Name        string  `json:"name"`
	Pattern     string  `json:"pattern"`
	SecretGroup int     `json:"secret_group"`
	Label       string  `json:"label"`
	Check       *string `json:"check"`
	KeyGroup    *int    `json:"key_group"`
	PemBegin    string  `json:"pem_begin"`
	PemEnd      string  `json:"pem_end"`
}

type keyLabelSpec struct {
	Contains []string `json:"contains"`
	Label    string   `json:"label"`
}

type detectorFile struct {
	Engine        int            `json:"engine"`
	CutGuardBytes int            `json:"cut_guard_bytes"`
	Detectors     []detectorSpec `json:"detectors"`
	KvIgnore      []string       `json:"kv_ignore"`
	KeyLabels     []keyLabelSpec `json:"key_labels"`
	CardBrands    []cardBrand    `json:"card_brands"`
	IbanLengths   map[string]int `json:"iban_lengths"`
}

var detectors = func() detectorFile {
	var file detectorFile
	if err := json.Unmarshal(detectorsJSON, &file); err != nil {
		panic(fmt.Sprintf("redact: detectors.json: %v", err))
	}
	return file
}()

// EngineVersion is the version of the redaction rules' behaviour, the
// same as the backend's ENGINE_VERSION (detectors.json's "engine").
var EngineVersion = detectors.Engine

// DetectorNames lists the built-in detectors, in the order of detectors.json.
func DetectorNames() []string {
	names := make([]string, len(detectors.Detectors))
	for i, d := range detectors.Detectors {
		names[i] = d.Name
	}
	return names
}

func detectorByName(name string) (detectorSpec, bool) {
	for _, d := range detectors.Detectors {
		if d.Name == name {
			return d, true
		}
	}
	return detectorSpec{}, false
}

// RuleError is a rule the engine can't run: an unknown detector, a pattern
// that doesn't compile, or a secret_group the pattern doesn't have.
type RuleError struct{ Message string }

func (e *RuleError) Error() string { return e.Message }

// pattern is a compiled rule pattern. The backend compiles patterns with
// RE2 and the never_nl option, so a match never crosses a newline, and runs
// pure-ASCII patterns in Latin-1, byte for byte. Here a pattern is run on
// each line on its own; for a pure-ASCII pattern every byte of 0x80 or over
// is first turned into 0x80, an invalid UTF-8 byte that Go's regexp reads
// as one character (U+FFFD): it is matched by `.` and negated classes as a
// Latin-1 character is, counts as one character in bounded repetitions,
// and is never folded into an ASCII letter by (?i) (Go would otherwise
// match U+212A KELVIN SIGN for k and U+017F LONG S for s, which Latin-1
// can't hold). That stand-in only works while the pattern can't tell one
// byte of 0x80 or over from another: a pure-ASCII pattern that names some
// of them (`\xE9`, `[\x80-\xff]`, `\pL`, `[^\xe9]`) is marked decode and
// runs in the private-use form described in latin1.go. A pattern with
// non-ASCII literals runs on the UTF-8 bytes as they are.
type pattern struct {
	re     *regexp.Regexp
	latin1 bool
	decode bool
	// Variants of a UTF-8 pattern for stretches between invalid bytes
	// (see anchored), built when first needed.
	mu       sync.Mutex
	variants [4]*regexp.Regexp
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

// compilePattern compiles a rule pattern the way the engine runs it.
func compilePattern(text string) (*pattern, error) {
	full := "(?m)" + text
	if !utf8.ValidString(full) {
		return nil, fmt.Errorf("the pattern isn't valid UTF-8")
	}
	if strings.Contains(text, `\C`) {
		return nil, fmt.Errorf(`\C isn't supported`)
	}
	re, err := regexp.Compile(full)
	if err != nil {
		return nil, err
	}
	p := &pattern{re: re, latin1: isASCII(full)}
	if !p.latin1 {
		return p, nil
	}
	rewritten, err := latin1Rewrite(full)
	if err != nil {
		return nil, err
	}
	parsed, err := syntax.Parse(full, syntax.Perl)
	if err != nil {
		return nil, err
	}
	if p.decode = namesHighBytes(parsed); p.decode {
		if p.re, err = regexp.Compile(rewritten); err != nil {
			return nil, err
		}
	}
	return p, nil
}

// latinized is the text a Latin-1 pattern runs on (see pattern).
func latinized(work []byte) []byte {
	out := make([]byte, len(work))
	for i, c := range work {
		if c >= 0x80 {
			c = 0x80
		}
		out[i] = c
	}
	return out
}

// subject holds a piece of work text and, built once, its Latin-1 form.
type subject struct {
	work []byte
	lat  []byte
}

func (s *subject) text(p *pattern) []byte {
	if !p.latin1 || p.decode {
		return s.work
	}
	if s.lat == nil {
		s.lat = latinized(s.work)
	}
	return s.lat
}

// lines calls fn with the [start, end) of each line of text (without its
// newline).
func lines(text []byte, fn func(start, end int)) {
	start := 0
	for {
		i := bytes.IndexByte(text[start:], '\n')
		if i < 0 {
			fn(start, len(text))
			return
		}
		fn(start, start+i)
		start += i + 1
	}
}

// findAll gives the submatch indexes of every match in text, line by line,
// as offsets into text. A UTF-8 pattern (one with non-ASCII literals) runs
// on each stretch of valid UTF-8 of the line on its own: RE2 in UTF-8 mode
// never matches an invalid byte (not with `.`, `\S` or a negated class),
// where Go's regexp reads it as U+FFFD and matches it.
func (p *pattern) findAll(text []byte, fn func(match []int)) {
	run := func(start, end int) {
		for _, m := range p.re.FindAllSubmatchIndex(text[start:end], -1) {
			for i := range m {
				if m[i] >= 0 {
					m[i] += start
				}
			}
			fn(m)
		}
	}
	lines(text, func(start, end int) {
		if p.decode && hasHighByte(text[start:end]) {
			decoded, offsets := latin1Decode(text[start:end])
			for _, m := range p.re.FindAllSubmatchIndex(decoded, -1) {
				for i := range m {
					if m[i] >= 0 {
						m[i] = offsets[m[i]] + start
					}
				}
				fn(m)
			}
			return
		}
		if p.latin1 || utf8.Valid(text[start:end]) {
			run(start, end)
			return
		}
		lineStart, lineEnd := start, end
		validRuns(text, start, end, func(s, e int) {
			re := p.anchored(s == lineStart, e == lineEnd)
			for _, m := range re.FindAllSubmatchIndex(text[s:e], -1) {
				for i := range m {
					if m[i] >= 0 {
						m[i] += s
					}
				}
				fn(m)
			}
		})
	})
}

// anchored is the pattern for a stretch of valid UTF-8 that starts (atStart)
// or ends (atEnd) at an invalid byte rather than at the line's edge: there
// RE2 sees no start or end of line, so ^ and \A, or $ and \z, must not match
// at the stretch's edge, which Go's regexp takes for the text's.
func (p *pattern) anchored(atStart, atEnd bool) *regexp.Regexp {
	if atStart && atEnd {
		return p.re
	}
	key := 0
	if !atStart {
		key |= 1
	}
	if !atEnd {
		key |= 2
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if re := p.variants[key]; re != nil {
		return re
	}
	parsed, err := syntax.Parse(p.re.String(), syntax.Perl)
	if err != nil {
		return p.re
	}
	noMatch(parsed, !atStart, !atEnd)
	re, err := regexp.Compile(parsed.String())
	if err != nil {
		return p.re
	}
	p.variants[key] = re
	return re
}

func noMatch(re *syntax.Regexp, begin, end bool) {
	switch {
	case begin && (re.Op == syntax.OpBeginLine || re.Op == syntax.OpBeginText),
		end && (re.Op == syntax.OpEndLine || re.Op == syntax.OpEndText):
		*re = syntax.Regexp{Op: syntax.OpNoMatch, Flags: re.Flags}
		return
	}
	for _, sub := range re.Sub {
		noMatch(sub, begin, end)
	}
}

// validRuns calls fn with each maximal stretch of valid UTF-8 in
// text[start:end].
func validRuns(text []byte, start, end int, fn func(start, end int)) {
	runStart := start
	for i := start; i < end; {
		r, size := utf8.DecodeRune(text[i:end])
		if r == utf8.RuneError && size <= 1 {
			if runStart < i {
				fn(runStart, i)
			}
			i++
			runStart = i
			continue
		}
		i += size
	}
	if runStart < end {
		fn(runStart, end)
	}
}

// searchFrom is RE2's search(text, pos) on one line: the leftmost match
// that starts at or after pos, with the byte before pos as context. Only
// patterns whose context-dependent assertion is a leading \b use it (the
// iban detector): a match found at pos itself is dropped when the byte
// before pos is a word character, as RE2 would not have found it there.
func (p *pattern) searchFrom(line []byte, pos int) []int {
	for pos <= len(line) {
		m := p.re.FindSubmatchIndex(line[pos:])
		if m == nil {
			return nil
		}
		if m[0] == 0 && pos > 0 && isWordByte(line[pos-1]) && pos < len(line) && isWordByte(line[pos]) {
			pos++
			continue
		}
		for i := range m {
			if m[i] >= 0 {
				m[i] += pos
			}
		}
		return m
	}
	return nil
}

func isWordByte(c byte) bool { return isASCIIAlnum(c) || c == '_' }

type rule struct {
	name     string
	re       *pattern
	group    int
	label    string
	check    string
	keyGroup int
}

// Rules is a compiled rule list.
type Rules struct {
	rules []rule
	// The private-key detector's BEGIN and END patterns and the index of
	// its rule, when it is on.
	pemBegin, pemEnd *pattern
	pemRule          int
	digest           string
	// The largest max_length of the custom rules (the longest match each
	// can make, in bytes), 0 when none sets one.
	maxLength int
}

// cutGuard is how far before the line limit the kept start of a long line
// ends at least: the longest value any rule can match with a blank in it,
// the largest of CutGuardBytes (the built-ins) and every custom rule's
// max_length, as the backend's CompiledRules.cut_guard.
func (r *Rules) cutGuard() int {
	return max(detectors.CutGuardBytes, r.maxLength)
}

// Digest identifies the rule list: the SHA-256 of its canonical JSON, the
// same as the backend's rules_digest.
func (r *Rules) Digest() string { return r.digest }

// RuleSpec is one rule in the agent's `redact` block format: a built-in
// {"name", "detector"} or a custom {"name", "pattern", "secret_group",
// "max_length"}, where max_length is the longest match the pattern can
// make, in bytes.
// Other keys (an id) are kept for the digest.
type RuleSpec map[string]any

// RulesDigest is the SHA-256 of the canonical JSON of a rule list: keys
// sorted, no spaces, non-ASCII characters as they are (Python's json.dumps
// with sort_keys, separators=(",", ":") and ensure_ascii=False).
func RulesDigest(specs []RuleSpec) (string, error) {
	var buf bytes.Buffer
	buf.WriteByte('[')
	for i, spec := range specs {
		if i > 0 {
			buf.WriteByte(',')
		}
		if err := canonicalJSON(&buf, map[string]any(spec)); err != nil {
			return "", err
		}
	}
	buf.WriteByte(']')
	sum := sha256.Sum256(buf.Bytes())
	return hex.EncodeToString(sum[:]), nil
}

func canonicalJSON(buf *bytes.Buffer, value any) error {
	switch v := value.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if v {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case string:
		canonicalString(buf, v)
	case json.Number:
		buf.WriteString(v.String())
	case int:
		buf.WriteString(strconv.Itoa(v))
	case int64:
		buf.WriteString(strconv.FormatInt(v, 10))
	case float64:
		if v == float64(int64(v)) {
			buf.WriteString(strconv.FormatInt(int64(v), 10))
		} else {
			buf.WriteString(strconv.FormatFloat(v, 'g', -1, 64))
		}
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			canonicalString(buf, k)
			buf.WriteByte(':')
			if err := canonicalJSON(buf, v[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	case RuleSpec:
		return canonicalJSON(buf, map[string]any(v))
	case []any:
		buf.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := canonicalJSON(buf, item); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	default:
		return fmt.Errorf("redaction rule value of type %T", value)
	}
	return nil
}

func canonicalString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}

var (
	cacheMu sync.Mutex
	cache   = map[string]*Rules{}
	order   []string
)

const cacheSize = 32

// Compile compiles a rule list, cached by its digest so the same rules
// compile once. It returns a *RuleError for a rule that can't run.
func Compile(specs []RuleSpec) (*Rules, error) {
	digest, err := RulesDigest(specs)
	if err != nil {
		return nil, &RuleError{err.Error()}
	}
	cacheMu.Lock()
	if cached, ok := cache[digest]; ok {
		cacheMu.Unlock()
		return cached, nil
	}
	cacheMu.Unlock()
	compiled, err := compile(specs)
	if err != nil {
		return nil, err
	}
	compiled.digest = digest
	cacheMu.Lock()
	defer cacheMu.Unlock()
	if _, ok := cache[digest]; !ok {
		cache[digest] = compiled
		order = append(order, digest)
		if len(order) > cacheSize {
			delete(cache, order[0])
			order = order[1:]
		}
	}
	return compiled, nil
}

func intValue(value any) (int, bool) {
	switch v := value.(type) {
	case json.Number:
		n, err := strconv.Atoi(v.String())
		return n, err == nil
	case int:
		return v, true
	case int64:
		return int(v), true
	case float64:
		if v == float64(int(v)) {
			return int(v), true
		}
	}
	return 0, false
}

func compile(specs []RuleSpec) (*Rules, error) {
	out := &Rules{pemRule: -1}
	for index, spec := range specs {
		name, _ := spec["name"].(string)
		if name == "" {
			return nil, &RuleError{"every redaction rule needs a name"}
		}
		if raw, ok := spec["detector"]; ok && raw != nil {
			detector, _ := raw.(string)
			d, found := detectorByName(detector)
			if !found {
				return nil, &RuleError{fmt.Sprintf("unknown detector %q", fmt.Sprint(raw))}
			}
			re, err := compilePattern(d.Pattern)
			if err != nil {
				return nil, &RuleError{fmt.Sprintf("detector %s: %v", d.Name, err)}
			}
			r := rule{name: name, re: re, group: d.SecretGroup, label: d.Label, keyGroup: -1}
			if d.Check != nil {
				r.check = *d.Check
			}
			if re.decode && r.check == "iban" {
				// searchFrom runs the pattern on the bytes as they are.
				return nil, &RuleError{fmt.Sprintf("detector %s: unsupported pattern", d.Name)}
			}
			if d.KeyGroup != nil {
				r.keyGroup = *d.KeyGroup
			}
			out.rules = append(out.rules, r)
			if d.PemBegin != "" && out.pemBegin == nil {
				begin, err := compilePattern(d.PemBegin)
				if err != nil {
					return nil, &RuleError{err.Error()}
				}
				end, err := compilePattern(d.PemEnd)
				if err != nil {
					return nil, &RuleError{err.Error()}
				}
				if begin.decode || end.decode {
					// pemLine runs these patterns on the bytes as they are.
					return nil, &RuleError{fmt.Sprintf("detector %s: unsupported pattern", d.Name)}
				}
				out.pemBegin, out.pemEnd, out.pemRule = begin, end, index
			}
			continue
		}
		text, _ := spec["pattern"].(string)
		if text == "" {
			return nil, &RuleError{fmt.Sprintf("redaction rule %q needs a detector or a pattern", name)}
		}
		re, err := compilePattern(text)
		if err != nil {
			return nil, &RuleError{fmt.Sprintf("redaction rule %q: %v", name, err)}
		}
		group := 0
		if raw, ok := spec["secret_group"]; ok && raw != nil {
			if b, isBool := raw.(bool); isBool && !b {
				group = 0
			} else if n, isInt := intValue(raw); isInt {
				group = n
			} else {
				return nil, &RuleError{fmt.Sprintf("redaction rule %q: secret_group %v isn't a group of the pattern", name, raw)}
			}
		}
		if group < 0 || group > re.re.NumSubexp() {
			return nil, &RuleError{fmt.Sprintf("redaction rule %q: secret_group %d isn't a group of the pattern", name, group)}
		}
		if raw, ok := spec["max_length"]; ok && raw != nil {
			maxLength, isInt := intValue(raw)
			if !isInt || maxLength < 1 {
				return nil, &RuleError{fmt.Sprintf("redaction rule %q: max_length must be a positive number of bytes", name)}
			}
			out.maxLength = max(out.maxLength, maxLength)
		}
		out.rules = append(out.rules, rule{name: name, re: re, group: group, label: name, keyGroup: -1})
	}
	return out, nil
}
