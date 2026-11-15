package redact

import (
	"bytes"
	"reflect"
	"regexp"
	"testing"
)

// Fuzz targets: no input makes the engine panic, and where blocks are cut
// never changes the result. `go test` runs the seeds; CI explores for a
// while with -fuzz.

func seedRedact(f *testing.F) {
	for _, s := range []string{
		"password=hunter22 4111 1111 1111 1111\n",
		"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
		"a fe80::1%eth0 b GB82 WEST 1234 5698 7654 32\npartial",
		"\xff\xfe\x00\x00t\x00o\x00k\x00e\x00n\x00=\x00x\x00\n\x00",
		"token=\xc3\xa9\xffx " + string(bytes.Repeat([]byte("~"), 80)) + " Bearer abcdefgh12345678\n",
	} {
		f.Add([]byte(s), uint16(len(s)/2), uint8(40), uint8(0))
	}
}

// FuzzRedactBlockCuts: content fed whole and in two blocks gives the same
// output and counts, as bytes and as UTF-16 content served as bytes
// (UTF16Unsure), with any long-line limit; final and not final.
func FuzzRedactBlockCuts(f *testing.F) {
	seedRedact(f)
	rules := allRules(f)
	f.Fuzz(func(t *testing.T, data []byte, cut uint16, maxLine uint8, mode uint8) {
		opts := Options{MaxLineBytes: 16 + int(maxLine)}
		if mode&1 == 1 {
			opts.SourceEncoding = UTF16Unsure
			opts.Origin = int64(mode>>1) & 1
		}
		final := mode&4 == 0
		at := int(cut) % (len(data) + 1)
		whole, r1 := run(t, rules, opts, [][]byte{data}, final)
		split, r2 := run(t, rules, opts, [][]byte{data[:at], data[at:]}, final)
		if !bytes.Equal(whole, split) || !sameCounts(r1.Counts, r2.Counts) ||
			r1.SourceOffset != r2.SourceOffset || r1.TrailingPartial != r2.TrailingPartial {
			t.Fatalf("cut %d changes the result:\n%q %v\n%q %v", at, whole, r1.Counts, split, r2.Counts)
		}
	})
}

// FuzzRedactUTF16: clearly UTF-16 content, decoded, redacted and served as
// UTF-8 or encoded back, never panics, whole or in two blocks. Block cuts
// aren't compared here: like the backend's engine, a line of exactly the
// long-line limit whose two-byte newline is split between blocks is taken
// for a long line (see the report of this change).
func FuzzRedactUTF16(f *testing.F) {
	seedRedact(f)
	rules := allRules(f)
	f.Fuzz(func(t *testing.T, data []byte, cut uint16, maxLine uint8, mode uint8) {
		opts := Options{MaxLineBytes: 16 + int(maxLine), SourceEncoding: UTF16LE}
		if mode&1 == 1 {
			opts.SourceEncoding = UTF16BE
		}
		if mode&2 == 2 {
			opts.OutputEncoding = "source"
		}
		at := int(cut) % (len(data) + 1)
		run(t, rules, opts, [][]byte{data}, mode&4 == 0)
		run(t, rules, opts, [][]byte{data[:at], data[at:]}, mode&4 == 0)
	})
}

// FuzzRedactWindow: a window anywhere in any content never panics and its
// offsets stay inside the data.
func FuzzRedactWindow(f *testing.F) {
	seedRedact(f)
	rules := allRules(f)
	f.Fuzz(func(t *testing.T, data []byte, start uint16, base uint8, mode uint8) {
		at := int(start) % (len(data) + 1)
		opts := Options{}
		if mode&1 == 1 {
			opts.SourceEncoding = UTF16Unsure
		}
		w, err := RedactWindow(data, at, rules, int64(base), mode&2 == 0, opts)
		if err != nil {
			t.Fatal(err)
		}
		if w.Begin < at || w.Begin > len(data) || w.End < w.Begin || w.End > len(data) {
			t.Fatalf("window [%d, %d) for start %d in %d bytes", w.Begin, w.End, at, len(data))
		}
	})
}

// FuzzCustomRule: any custom pattern either compiles or is refused with a
// RuleError, and redacting with it never panics. A pure-ASCII pattern
// rewritten for Latin-1 (latin1Rewrite) finds the same matches as the
// pattern itself in ASCII text.
func FuzzCustomRule(f *testing.F) {
	f.Add(`id=(\d+)`, []byte("id=42 x\n"), uint8(1))
	f.Add("senhá=(\\S+)", []byte("senh\xc3\xa1=a\xffb\n"), uint8(1))
	f.Add(`^(a|b)*$`, []byte("abab\n\xff"), uint8(0))
	f.Add(`(?i)x[^\xc9]y`, []byte("x\xe9y x\xc9y XAY\n"), uint8(0))
	f.Add(`Nome: (\p{L}+)`, []byte("Nome: Jo\xe3o\n"), uint8(1))
	f.Add(`[~-\xff]+|\PL\Q[\E`, []byte("~k\xe9[ \xff\n"), uint8(0))
	f.Fuzz(func(t *testing.T, pattern string, data []byte, group uint8) {
		rules, err := Compile([]RuleSpec{{"name": "custom", "pattern": pattern, "secret_group": int(group % 4)}})
		if err != nil {
			if _, ok := err.(*RuleError); !ok {
				t.Fatalf("%T %v", err, err)
			}
			return
		}
		run(t, rules, Options{MaxLineBytes: 64}, [][]byte{data}, true)
		if p := rules.rules[0].re; p.decode {
			ascii := make([]byte, len(data))
			for i, c := range data {
				ascii[i] = c & 0x7F
			}
			original := regexp.MustCompile("(?m)" + pattern)
			for _, line := range bytes.Split(ascii, []byte("\n")) {
				want := original.FindAllSubmatchIndex(line, -1)
				got := p.re.FindAllSubmatchIndex(line, -1)
				if !reflect.DeepEqual(want, got) {
					t.Fatalf("%q on %q: rewritten %v, original %v", pattern, line, got, want)
				}
			}
		}
	})
}
