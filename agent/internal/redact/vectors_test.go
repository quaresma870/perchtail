package redact

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"unicode/utf16"
)

// The vectors shared with the backend's engine (backend/tests/
// test_redaction_vectors.py runs the same file): both engines must give the
// same output, counts and offsets, whatever block sizes the input comes in.

type vectorFile struct {
	Engine       int              `json:"engine"`
	Vectors      []map[string]any `json:"vectors"`
	LongLineCuts []map[string]any `json:"long_line_cuts"`
	HeadDigests  []struct {
		KeyB64   string `json:"key_b64"`
		Data     string `json:"data"`
		Offset   int64  `json:"offset"`
		Expected string `json:"expected"`
	} `json:"head_digests"`
	RuleDigests []struct {
		Rules    []map[string]any `json:"rules"`
		Expected string           `json:"expected"`
	} `json:"rule_digests"`
	RuleErrors []map[string]any `json:"rule_errors"`
	Detect     []struct {
		HeadB64  string  `json:"head_b64"`
		Detected *string `json:"detected"`
	} `json:"detect"`
}

func loadVectors(t testing.TB) vectorFile {
	t.Helper()
	raw, err := os.ReadFile("testdata/vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var file vectorFile
	if err := decoder.Decode(&file); err != nil {
		t.Fatal(err)
	}
	return file
}

// encodeText is Python's str.encode for the encodings the vectors use.
func encodeText(t testing.TB, text, encoding string) []byte {
	switch encoding {
	case "", "utf-8":
		return []byte(text)
	case "utf-16-le", "utf-16-be":
		var out []byte
		for _, u := range utf16.Encode([]rune(text)) {
			if encoding == "utf-16-be" {
				out = append(out, byte(u>>8), byte(u))
			} else {
				out = append(out, byte(u), byte(u>>8))
			}
		}
		return out
	case "latin-1":
		var out []byte
		for _, r := range text {
			out = append(out, byte(r))
		}
		return out
	}
	t.Fatalf("encoding %q", encoding)
	return nil
}

func vectorBytes(t testing.TB, v map[string]any, key string) []byte {
	if b64, ok := v[key+"_b64"].(string); ok {
		data, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	encoding, _ := v[key+"_encoding"].(string)
	text, _ := v[key].(string)
	return encodeText(t, text, encoding)
}

func ruleSpecs(v map[string]any) []RuleSpec {
	raw, _ := v["rules"].([]any)
	specs := make([]RuleSpec, len(raw))
	for i, r := range raw {
		specs[i] = RuleSpec(r.(map[string]any))
	}
	return specs
}

func number(v map[string]any, key string) (int, bool) {
	n, ok := v[key].(json.Number)
	if !ok {
		return 0, false
	}
	i, err := n.Int64()
	return int(i), err == nil
}

func vectorOptions(t testing.TB, v map[string]any, data []byte) Options {
	var opts Options
	encoding, _ := v["source_encoding"].(string)
	if encoding == "auto" {
		head := data
		if len(head) > 64 {
			head = head[:64]
		}
		detected, err := DetectEncoding(head)
		if err != nil {
			t.Fatal(err)
		}
		encoding = detected
	}
	opts.SourceEncoding = encoding
	opts.OutputEncoding, _ = v["output_encoding"].(string)
	if n, ok := number(v, "max_line_bytes"); ok {
		opts.MaxLineBytes = n
	}
	if n, ok := number(v, "pem_max_lines"); ok {
		opts.PemMaxLines = n
	}
	return opts
}

func counts(v map[string]any) map[string]int {
	out := map[string]int{}
	raw, _ := v["counts"].(map[string]any)
	for name, n := range raw {
		i, _ := n.(json.Number).Int64()
		out[name] = int(i)
	}
	return out
}

func splits(data []byte) [][][]byte {
	out := [][][]byte{{data}}
	for cut := 0; cut <= len(data); cut++ {
		out = append(out, [][]byte{data[:cut], data[cut:]})
	}
	for _, size := range []int{1, 7, 4096} {
		var blocks [][]byte
		for i := 0; i < len(data); i += size {
			end := i + size
			if end > len(data) {
				end = len(data)
			}
			blocks = append(blocks, data[i:end])
		}
		if blocks == nil {
			blocks = [][]byte{{}}
		}
		out = append(out, blocks)
	}
	return out
}

func final(v map[string]any) bool {
	f, ok := v["final"].(bool)
	return !ok || f
}

func run(t testing.TB, rules *Rules, opts Options, blocks [][]byte, isFinal bool) ([]byte, *Redactor) {
	r, err := New(rules, opts)
	if err != nil {
		t.Fatal(err)
	}
	var out []byte
	for _, block := range blocks {
		out = append(out, r.Feed(block)...)
	}
	return append(out, r.Finish(isFinal)...), r
}

func sameCounts(got, want map[string]int) bool {
	if len(got) == 0 && len(want) == 0 {
		return true
	}
	return reflect.DeepEqual(got, want)
}

func TestVectors(t *testing.T) {
	file := loadVectors(t)
	if file.Engine != EngineVersion {
		t.Fatalf("vectors are for engine %d, this is %d", file.Engine, EngineVersion)
	}
	for _, v := range file.Vectors {
		v := v
		name, _ := v["name"].(string)
		if _, ok := v["window"]; ok {
			continue
		}
		t.Run(name, func(t *testing.T) {
			data := vectorBytes(t, v, "input")
			rules, err := Compile(ruleSpecs(v))
			if err != nil {
				t.Fatal(err)
			}
			if code, ok := v["error"].(string); ok {
				// The refusal comes from detecting the encoding (source_encoding
				// auto) or from the encoding given.
				var err error
				if v["source_encoding"] == "auto" {
					_, err = DetectEncoding(data)
				} else {
					_, err = New(rules, vectorOptions(t, v, data))
				}
				if !errors.Is(err, ErrUnsupportedEncoding) || code != "unsupported_encoding" {
					t.Fatalf("want error %s, got %v", code, err)
				}
				return
			}
			expected := vectorBytes(t, v, "expected")
			opts := vectorOptions(t, v, data)
			for _, blocks := range splits(data) {
				out, r := run(t, rules, opts, blocks, final(v))
				sizes := make([]int, len(blocks))
				for i, b := range blocks {
					sizes[i] = len(b)
				}
				if !bytes.Equal(out, expected) {
					t.Fatalf("blocks %v:\n got %q\nwant %q", sizes, out, expected)
				}
				if !sameCounts(r.Counts, counts(v)) {
					t.Fatalf("blocks %v: counts %v, want %v", sizes, r.Counts, counts(v))
				}
				if r.SourceSize != int64(len(data)) {
					t.Fatalf("source size %d", r.SourceSize)
				}
				if n, ok := number(v, "source_offset"); ok && r.SourceOffset != int64(n) {
					t.Fatalf("blocks %v: source offset %d, want %d", sizes, r.SourceOffset, n)
				}
				if n, ok := number(v, "trailing_partial"); ok && r.TrailingPartial != n {
					t.Fatalf("blocks %v: trailing partial %d, want %d", sizes, r.TrailingPartial, n)
				}
			}
		})
	}
}

func TestWindowVectors(t *testing.T) {
	for _, v := range loadVectors(t).Vectors {
		v := v
		window, ok := v["window"].(map[string]any)
		if !ok {
			continue
		}
		t.Run(v["name"].(string), func(t *testing.T) {
			data := vectorBytes(t, v, "input")
			rules, err := Compile(ruleSpecs(v))
			if err != nil {
				t.Fatal(err)
			}
			start, _ := number(window, "start")
			base, _ := number(window, "base_offset")
			isFinal, _ := v["final"].(bool)
			got, err := RedactWindow(data, start, rules, int64(base), isFinal, vectorOptions(t, v, data))
			if err != nil {
				t.Fatal(err)
			}
			if want := vectorBytes(t, v, "expected"); !bytes.Equal(got.Data, want) {
				t.Fatalf("got %q want %q", got.Data, want)
			}
			if !sameCounts(got.Counts, counts(v)) {
				t.Fatalf("counts %v want %v", got.Counts, counts(v))
			}
			begin, _ := number(v, "begin")
			end, _ := number(v, "end")
			trailing, _ := number(v, "trailing_partial")
			aligned, _ := v["aligned"].(bool)
			if got.Begin != begin || got.End != end || got.Aligned != aligned || got.TrailingPartial != trailing {
				t.Fatalf("begin %d end %d aligned %v trailing %d; want %d %d %v %d",
					got.Begin, got.End, got.Aligned, got.TrailingPartial, begin, end, aligned, trailing)
			}
		})
	}
}

var markerRE = regexp.MustCompile(`(\[redacted: [^\]]*\])`)

// encodeKept is kept text as a UTF16Unsure redaction gives it: the source's
// own UTF-16 bytes, with the markers in ASCII.
func encodeKept(t testing.TB, text, encoding string) []byte {
	var out []byte
	last := 0
	for _, m := range markerRE.FindAllStringIndex(text, -1) {
		out = append(out, encodeText(t, text[last:m[0]], encoding)...)
		out = append(out, text[m[0]:m[1]]...)
		last = m[1]
	}
	return append(out, encodeText(t, text[last:], encoding)...)
}

func TestLongLineCutVectors(t *testing.T) {
	for _, v := range loadVectors(t).LongLineCuts {
		v := v
		t.Run(v["name"].(string), func(t *testing.T) {
			rules, err := Compile(ruleSpecs(v))
			if err != nil {
				t.Fatal(err)
			}
			maxLine, _ := number(v, "max_line_bytes")
			kept, value := v["kept"].(string), v["value"].(string)
			guard := rules.cutGuard()
			unsure := v["source_encoding"] == "utf-16-unsure"
			encoding, _ := v["input_encoding"].(string)
			big := encoding == "utf-16-be"
			for k := 0; k <= len(value); k++ {
				var line, expected []byte
				opts := Options{MaxLineBytes: maxLine}
				if unsure {
					filler := maxLine/2 - len(kept) - 2 - k
					text := fmt.Sprintf("%s %s %s end\n", kept, strings.Repeat("~", filler), value)
					line = encodeText(t, text, encoding)
					blank := 2*(len(kept)+1+filler) + map[bool]int{true: 1, false: 0}[big]
					expected = encodeKept(t, v["expected_kept"].(string), encoding)
					if blank <= maxLine-2*guard {
						expected = append(expected, encodeText(t, " "+strings.Repeat("~", filler), encoding)...)
					}
					if big {
						expected = append(expected, 0)
					}
					expected = append(expected, Marker(LongLineLabel(maxLine))...)
					expected = append(expected, '\n')
					if !big {
						expected = append(expected, 0)
					}
					opts.SourceEncoding = UTF16Unsure
				} else {
					filler := maxLine - len(kept) - 2 - k
					line = []byte(kept + " " + strings.Repeat("~", filler) + " " + value + " end\n")
					expected = []byte(v["expected_kept"].(string))
					if maxLine-k-1 <= maxLine-guard {
						expected = append(expected, []byte(" "+strings.Repeat("~", filler))...)
					}
					expected = append(expected, Marker(LongLineLabel(maxLine))...)
					expected = append(expected, '\n')
				}
				single := make([][]byte, len(line))
				for i := range line {
					single[i] = line[i : i+1]
				}
				for _, blocks := range [][][]byte{{line}, single} {
					out, r := run(t, rules, opts, blocks, true)
					if !bytes.Equal(out, expected) {
						t.Fatalf("k=%d blocks=%d:\n got %q\nwant %q", k, len(blocks), out, expected)
					}
					if !sameCounts(r.Counts, counts(v)) {
						t.Fatalf("k=%d counts %v", k, r.Counts)
					}
				}
			}
		})
	}
}

func TestRuleErrorVectors(t *testing.T) {
	// Rule lists both engines refuse to compile.
	for _, v := range loadVectors(t).RuleErrors {
		v := v
		t.Run(v["name"].(string), func(t *testing.T) {
			_, err := Compile(ruleSpecs(v))
			var ruleErr *RuleError
			if !errors.As(err, &ruleErr) {
				t.Fatalf("want a RuleError, got %v", err)
			}
		})
	}
}

func TestMaxLengthWidensTheCutGuard(t *testing.T) {
	guard := func(specs []RuleSpec) int {
		rules, err := Compile(specs)
		if err != nil {
			t.Fatal(err)
		}
		return rules.cutGuard()
	}
	custom := func(maxLength any) RuleSpec {
		spec := RuleSpec{"name": "acct", "pattern": `ACCT \d+`}
		if maxLength != nil {
			spec["max_length"] = maxLength
		}
		return spec
	}
	if got := guard(nil); got != detectors.CutGuardBytes {
		t.Fatalf("no rules: %d", got)
	}
	if got := guard([]RuleSpec{custom(nil)}); got != detectors.CutGuardBytes {
		t.Fatalf("no max_length: %d", got)
	}
	if got := guard([]RuleSpec{custom(json.Number("10"))}); got != detectors.CutGuardBytes {
		t.Fatalf("max_length under the built-in guard: %d", got)
	}
	if got := guard([]RuleSpec{custom(json.Number("300")), custom(json.Number("120"))}); got != 300 {
		t.Fatalf("largest max_length: %d", got)
	}
	// The digest covers max_length, so a changed value never reuses the
	// rules compiled for the old one.
	a, _ := RulesDigest([]RuleSpec{custom(json.Number("120"))})
	b, _ := RulesDigest([]RuleSpec{custom(json.Number("300"))})
	if a == b {
		t.Fatal("max_length isn't part of the digest")
	}
}

func TestHeadDigestVectors(t *testing.T) {
	for _, v := range loadVectors(t).HeadDigests {
		key, _ := base64.StdEncoding.DecodeString(v.KeyB64)
		if got := HeadDigest(key, []byte(v.Data), v.Offset); got != v.Expected {
			t.Fatalf("%q offset %d: %s want %s", v.Data, v.Offset, got, v.Expected)
		}
	}
}

func TestDetectVectors(t *testing.T) {
	for _, v := range loadVectors(t).Detect {
		head, _ := base64.StdEncoding.DecodeString(v.HeadB64)
		got, err := DetectEncoding(head)
		switch {
		case v.Detected != nil && *v.Detected == "error":
			if !errors.Is(err, ErrUnsupportedEncoding) {
				t.Fatalf("%x: want an error, got %q", head, got)
			}
		case v.Detected == nil:
			if err != nil || got != Bytes {
				t.Fatalf("%x: got %q %v, want bytes", head, got, err)
			}
		default:
			if err != nil || got != *v.Detected {
				t.Fatalf("%x: got %q %v, want %s", head, got, err, *v.Detected)
			}
		}
	}
}

func TestEveryDetectorHasAVector(t *testing.T) {
	used := map[string]bool{}
	for _, v := range loadVectors(t).Vectors {
		for _, r := range ruleSpecs(v) {
			if d, ok := r["detector"].(string); ok {
				used[d] = true
			}
		}
	}
	for _, name := range DetectorNames() {
		if !used[name] {
			t.Errorf("no vector uses detector %s", name)
		}
	}
}

func TestEveryDetectorCompiles(t *testing.T) {
	// The iban and private-key detectors run some of their patterns on the
	// bytes as they are, which a decode pattern can't (compile refuses it).
	for _, name := range DetectorNames() {
		if _, err := Compile([]RuleSpec{{"name": name, "detector": name}}); err != nil {
			t.Errorf("detector %s: %v", name, err)
		}
	}
}

func TestDecodeOnlyForPatternsThatNameHighBytes(t *testing.T) {
	cases := []struct {
		text   string
		decode bool
	}{
		{`id=.{3};`, false},
		{`[^a]+`, false},
		{`\S+\W`, false},
		{`(?i)token`, false},
		{`\Q\xE9\E`, false},
		{`\xE9`, true},
		{`(?i)\xC9`, true},
		{`\351`, true},
		{`\pL`, true},
		{`\p{So}`, true},
		{`[\x80-\xff]`, true},
		{`[\x00-\xff]`, true},
		{`[^\xe9]`, true},
	}
	for _, c := range cases {
		p, err := compilePattern(c.text)
		if err != nil {
			t.Fatalf("%s: %v", c.text, err)
		}
		if !p.latin1 || p.decode != c.decode {
			t.Errorf("%s: latin1 %v decode %v, want decode %v", c.text, p.latin1, p.decode, c.decode)
		}
	}
}

func TestLatin1PatternsRefuseEscapesAboveFF(t *testing.T) {
	// RE2 refuses these in Latin-1 mode, so the backend refuses the rule.
	for _, text := range []string{`\x{100}`, `a\x{fffd}b`, `[\x00-\x{10ffff}]`, `[^\x{fffd}]`, `\400`, `[\0-\777]`, `\p{NoSuchClass}`} {
		if _, err := compilePattern(text); err == nil {
			t.Errorf("%s compiled", text)
		}
	}
	for _, text := range []string{`\Q\x{100}\E`, `\\x{100}`, `[\\x{100}]`, `\377`, `\x{ff}`} {
		if _, err := compilePattern(text); err != nil {
			t.Errorf("%s: %v", text, err)
		}
	}
}

func TestLatin1Rewrite(t *testing.T) {
	cases := []struct{ in, out string }{
		{`a\xE9b`, `a\x{E0E9}b`},
		{`\x41\x{e9}\351\0`, `\x41\x{E0E9}\x{E0E9}\0`},
		{`[\x80-\xff]`, `[\x{E080}-\x{E0FF}]`},
		{`[~-\xff]`, `[\x{7E}-\x{7F}\x{E080}-\x{E0FF}]`},
		{`[^a-z\xe9]`, `[^a-z\x{E0E9}]`},
		{`[]\xe9-]`, `[]\x{E0E9}-]`},
		{`[[:alpha:]\d]`, `[[:alpha:]\d]`},
		{`\p{Sc}`, `[\x{24}\x{E0A2}-\x{E0A5}]`},
		{`[\p{Greek}x]`, `[\x{10FFFF}x]`},
		{`\Q[\xe9\E\xe9`, `\Q[\xe9\E\x{E0E9}`},
		{`\(\xe9\)`, `\(\x{E0E9}\)`},
	}
	for _, c := range cases {
		got, err := latin1Rewrite(c.in)
		if err != nil {
			t.Fatalf("%s: %v", c.in, err)
		}
		if got != c.out {
			t.Errorf("%s: %s want %s", c.in, got, c.out)
		}
	}
}

func TestRuleDigestVectors(t *testing.T) {
	for _, v := range loadVectors(t).RuleDigests {
		specs := make([]RuleSpec, len(v.Rules))
		for i, r := range v.Rules {
			specs[i] = RuleSpec(r)
		}
		got, err := RulesDigest(specs)
		if err != nil {
			t.Fatal(err)
		}
		if got != v.Expected {
			t.Fatalf("%v: %s want %s", v.Rules, got, v.Expected)
		}
		rules, err := Compile(specs)
		if err != nil {
			t.Fatal(err)
		}
		if rules.Digest() != v.Expected {
			t.Fatalf("compiled digest %s", rules.Digest())
		}
	}
}
