package redact

import (
	"bytes"
	"fmt"
	"math/big"
	"math/rand"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// Properties over generated content, as backend/tests/
// test_redaction_properties.py: values generated for every detector,
// planted in noise, fed in random blocks with long lines cut at a random
// limit, are never seen in clear; a window gives the same lines as the
// whole file; no rules means no change.

func allRules(t testing.TB) *Rules {
	t.Helper()
	var specs []RuleSpec
	for i, name := range DetectorNames() {
		specs = append(specs, RuleSpec{"id": i, "name": name, "detector": name})
	}
	rules, err := Compile(specs)
	if err != nil {
		t.Fatal(err)
	}
	return rules
}

const (
	alnum  = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
	upper  = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	b64url = alnum + "_-"
	digits = "0123456789"
)

func text(rng *rand.Rand, alphabet string, min, max int) string {
	n := min + rng.Intn(max-min+1)
	var b strings.Builder
	for i := 0; i < n; i++ {
		b.WriteByte(alphabet[rng.Intn(len(alphabet))])
	}
	return b.String()
}

func luhnDigit(payload string) string {
	total := 0
	for position := 0; position < len(payload); position++ {
		value := int(payload[len(payload)-1-position] - '0')
		if position%2 == 0 {
			value *= 2
			if value > 9 {
				value -= 9
			}
		}
		total += value
	}
	return strconv.Itoa((10 - total%10) % 10)
}

func genCard(rng *rand.Rand) string {
	brand := detectors.CardBrands[rng.Intn(len(detectors.CardBrands))]
	r := brand.Ranges[rng.Intn(len(brand.Ranges))]
	low, _ := strconv.Atoi(r[0])
	high, _ := strconv.Atoi(r[1])
	prefix := fmt.Sprintf("%0*d", len(r[0]), low+rng.Intn(high-low+1))
	length := brand.Lengths[rng.Intn(len(brand.Lengths))]
	payload := (prefix + text(rng, digits, length, length))[:length-1]
	number := payload + luhnDigit(payload)
	separator := []string{"", " ", "-"}[rng.Intn(3)]
	group := []int{4, 1}[rng.Intn(2)]
	var parts []string
	for i := 0; i < len(number); i += group {
		end := i + group
		if end > len(number) {
			end = len(number)
		}
		parts = append(parts, number[i:end])
	}
	return strings.Join(parts, separator)
}

func genIBAN(rng *rand.Rand) string {
	countries := make([]string, 0, len(detectors.IbanLengths))
	for c := range detectors.IbanLengths {
		countries = append(countries, c)
	}
	sort.Strings(countries)
	country := countries[rng.Intn(len(countries))]
	bban := text(rng, upper, detectors.IbanLengths[country]-4, detectors.IbanLengths[country]-4)
	var digitsText strings.Builder
	for _, c := range bban + country + "00" {
		value, _ := strconv.ParseInt(string(c), 36, 64)
		digitsText.WriteString(strconv.FormatInt(value, 10))
	}
	number, _ := new(big.Int).SetString(digitsText.String(), 10)
	check := 98 - new(big.Int).Mod(number, big.NewInt(97)).Int64()
	iban := fmt.Sprintf("%s%02d%s", country, check, bban)
	if rng.Intn(2) == 0 {
		var parts []string
		for i := 0; i < len(iban); i += 4 {
			end := i + 4
			if end > len(iban) {
				end = len(iban)
			}
			parts = append(parts, iban[i:end])
		}
		return strings.Join(parts, " ")
	}
	return iban
}

// genIPv6 is a random address in Python's compressed form: groups without
// leading zeros, the longest run of two or more zero groups as "::".
func genIPv6(rng *rand.Rand) string {
	var groups [8]uint16
	for i := range groups {
		if rng.Intn(3) == 0 {
			groups[i] = 0
		} else {
			groups[i] = uint16(rng.Intn(1 << 16))
		}
	}
	if groups == [8]uint16{} {
		groups[7] = 1
	}
	bestStart, bestLen := -1, 0
	for i := 0; i < 8; {
		if groups[i] != 0 {
			i++
			continue
		}
		j := i
		for j < 8 && groups[j] == 0 {
			j++
		}
		if j-i > bestLen {
			bestStart, bestLen = i, j-i
		}
		i = j
	}
	hex := func(from, to int) string {
		var parts []string
		for i := from; i < to; i++ {
			parts = append(parts, strconv.FormatUint(uint64(groups[i]), 16))
		}
		return strings.Join(parts, ":")
	}
	if bestLen < 2 {
		return hex(0, 8)
	}
	return hex(0, bestStart) + "::" + hex(bestStart+bestLen, 8)
}

// generated gives (text written to the log, what must not be seen in clear
// with enough context, the hidden part alone) for a detector.
func generated(rng *rand.Rand, detector string) (string, string, string) {
	switch detector {
	case "card":
		v := genCard(rng)
		return v, v, v
	case "iban":
		v := genIBAN(rng)
		return v, v, v
	case "email":
		v := text(rng, alnum, 1, 12) + "@" + text(rng, "abcdefghijklmnopqrstuvwxyz0123456789", 1, 10) + "." + text(rng, "abcdefghijklmnopqrstuvwxyz", 2, 6)
		return v, v, v
	case "jwt":
		v := "eyJ" + text(rng, b64url, 5, 20) + ".eyJ" + text(rng, b64url, 5, 20) + "." + text(rng, b64url, 0, 20)
		return v, v, v
	case "ipv4":
		v := fmt.Sprintf("%d.%d.%d.%d", rng.Intn(256), rng.Intn(256), rng.Intn(256), rng.Intn(256))
		return v, v, v
	case "ipv6":
		v := genIPv6(rng)
		return v, v, v
	case "bearer":
		token := text(rng, upper, 8, 30)
		return "Bearer " + token, "Bearer " + token, token
	case "authorization":
		value := text(rng, upper, 4, 30)
		return "Authorization: Basic " + value, "Basic " + value, value
	case "secret_kv":
		key := []string{"password", "db_password", "api_key", "token", "client_secret"}[rng.Intn(5)]
		var value string
		for value == "" || strings.ToLower(value) == "null" || strings.ToLower(value) == "none" {
			value = text(rng, upper, 1, 20)
		}
		return key + "=" + value, "=" + value, value
	case "url_password":
		secret := text(rng, upper, 1, 16)
		return "https://u:" + secret + "@host", ":" + secret + "@", secret
	case "private_key":
		body := "MII" + text(rng, alnum+"+/", 8, 60)
		return "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----", body, body
	}
	panic(detector)
}

var (
	noise      = []string{" ", "  ", "ok", "x=1", "12:30:45", "v1.2", "id 42", "é", "\t", "\xff"}
	separators = []string{" ", "  ", "\t", "\n", "\r\n", ", ", "; ", " | ", " - "}
)

func genNoise(rng *rand.Rand) string {
	switch rng.Intn(4) {
	case 0:
		return noise[rng.Intn(len(noise))]
	case 1:
		return text(rng, digits, 1, 8)
	case 2:
		var parts []string
		for i := 0; i < 1+rng.Intn(40); i++ {
			parts = append(parts, text(rng, digits, 1, 8))
		}
		return strings.Join(parts, " ")
	}
	return []string{"AB12", "DE12", "-", strings.Repeat("x", 70), "ok\n"}[rng.Intn(5)]
}

func planted(rng *rand.Rand) ([]byte, []string) {
	names := DetectorNames()
	var parts, secrets []string
	for i := 0; i < rng.Intn(26); i++ {
		if len(parts) > 0 {
			parts = append(parts, separators[rng.Intn(len(separators))])
		}
		if rng.Intn(2) == 0 {
			written, clear, _ := generated(rng, names[rng.Intn(len(names))])
			parts = append(parts, written)
			secrets = append(secrets, clear)
		} else {
			parts = append(parts, genNoise(rng))
		}
	}
	return []byte(strings.Join(parts, "")), secrets
}

func randomBlocks(rng *rand.Rand, data []byte) [][]byte {
	points := map[int]bool{}
	for i := 0; i < rng.Intn(6); i++ {
		points[rng.Intn(len(data)+1)] = true
	}
	cuts := []int{0}
	for p := range points {
		cuts = append(cuts, p)
	}
	sort.Ints(cuts)
	cuts = append(cuts, len(data))
	var blocks [][]byte
	for i := 0; i+1 < len(cuts); i++ {
		blocks = append(blocks, data[cuts[i]:cuts[i+1]])
	}
	return blocks
}

func TestNoGeneratedValueSurvives(t *testing.T) {
	rules := allRules(t)
	rng := rand.New(rand.NewSource(277))
	for n := 0; n < 3000; n++ {
		data, secrets := planted(rng)
		maxLine := 64 + rng.Intn(537)
		out, _ := run(t, rules, Options{MaxLineBytes: maxLine}, randomBlocks(rng, data), true)
		for _, secret := range secrets {
			if bytes.Contains(out, []byte(secret)) {
				t.Fatalf("%q survives in %q (input %q, max line %d)", secret, out, data, maxLine)
			}
		}
	}
}

var markerPattern = regexp.MustCompile(`\[redacted: [^\]]*\]`)

func TestNoPieceOfAValueSurvivesALongLineCut(t *testing.T) {
	rules := allRules(t)
	rng := rand.New(rand.NewSource(278))
	var names []string
	for _, name := range DetectorNames() {
		if name != "private_key" {
			names = append(names, name)
		}
	}
	for n := 0; n < 2000; n++ {
		written, _, secret := generated(rng, names[rng.Intn(len(names))])
		line := []byte(text(rng, "~#/| ", 0, 300) + " " + written + " " + text(rng, "~#/| ", 0, 40) + "\n")
		maxLine := 64 + rng.Intn(337)
		out, _, err := RedactBytes(line, rules, true, Options{MaxLineBytes: maxLine})
		if err != nil {
			t.Fatal(err)
		}
		rest := markerPattern.ReplaceAll(out, nil)
		size := 4
		if len(secret) < size {
			size = len(secret)
		}
		for i := 0; i+size <= len(secret); i++ {
			piece := secret[i : i+size]
			if !strings.ContainsAny(piece, alnum) {
				continue
			}
			if bytes.Contains(rest, []byte(piece)) {
				t.Fatalf("%q of %q left in %q (max line %d)", piece, secret, out, maxLine)
			}
		}
	}
}

func TestAWindowMatchesTheWholeFile(t *testing.T) {
	rules := allRules(t)
	rng := rand.New(rand.NewSource(279))
	for n := 0; n < 1000; n++ {
		data, _ := planted(rng)
		starts := []int{0}
		for i, c := range data {
			if c == '\n' {
				starts = append(starts, i+1)
			}
		}
		start := starts[rng.Intn(len(starts))]
		whole, _, _ := RedactBytes(data, rules, true, Options{})
		head, _, _ := RedactBytes(data[:start], rules, true, Options{})
		window, err := RedactWindow(data, start, rules, 0, true, Options{})
		if err != nil {
			t.Fatal(err)
		}
		if window.Aligned || !bytes.Equal(append(head, window.Data...), whole) {
			t.Fatalf("start %d of %q: head %q + window %q != %q", start, data, head, window.Data, whole)
		}
	}
}

func TestNoRulesChangeNothing(t *testing.T) {
	rules, err := Compile(nil)
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(280))
	for n := 0; n < 500; n++ {
		data := []byte(text(rng, "ab \n\xff\x00", 0, 300))
		out, result, _ := RedactBytes(data, rules, rng.Intn(2) == 0, Options{})
		if !bytes.Equal(out, data) || result.SourceOffset != int64(len(data)) {
			t.Fatalf("%q changed to %q", data, out)
		}
	}
}
