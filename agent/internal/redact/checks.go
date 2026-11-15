package redact

import (
	"bytes"
	"strings"
)

// ipv6OK tells whether a string is an IPv6 address the way Python's
// ipaddress.IPv6Address (CPython 3.12) accepts it, so both engines decide
// alike: net/netip differs on zones and embedded IPv4. An optional
// non-empty %zone without a second %, at most 45 characters before it, at
// least 3 and at most 9 colon-separated parts, at most one "::" (which must
// stand for at least one group), a leading or trailing ":" only as part of
// "::", and groups of 1 to 4 hex digits; an IPv4 suffix (a "." in the last
// part) as four dotted decimal octets.
func ipv6OK(text string) bool {
	if text == "" || strings.Contains(text, "/") {
		return false
	}
	if i := strings.IndexByte(text, '%'); i >= 0 {
		scope := text[i+1:]
		if scope == "" || strings.Contains(scope, "%") {
			return false
		}
		text = text[:i]
	}
	if text == "" || len(text) > 45 {
		return false
	}
	const hextets = 8
	parts := strings.SplitN(text, ":", hextets+2)
	if len(parts) < 3 {
		return false
	}
	if last := parts[len(parts)-1]; strings.Contains(last, ".") {
		high, low, ok := ipv4Hextets(last)
		if !ok {
			return false
		}
		parts = append(parts[:len(parts)-1], high, low)
	}
	if len(parts) > hextets+1 {
		return false
	}
	skip := -1
	for i := 1; i < len(parts)-1; i++ {
		if parts[i] == "" {
			if skip >= 0 {
				return false
			}
			skip = i
		}
	}
	var hi, lo []string
	if skip >= 0 {
		partsHi := skip
		partsLo := len(parts) - skip - 1
		if parts[0] == "" {
			partsHi--
			if partsHi != 0 {
				return false
			}
		}
		if parts[len(parts)-1] == "" {
			partsLo--
			if partsLo != 0 {
				return false
			}
		}
		if hextets-(partsHi+partsLo) < 1 {
			return false
		}
		hi = parts[:partsHi]
		lo = parts[len(parts)-partsLo:]
	} else {
		if len(parts) != hextets || parts[0] == "" || parts[len(parts)-1] == "" {
			return false
		}
		hi = parts
	}
	for _, group := range append(append([]string{}, hi...), lo...) {
		if !hextetOK(group) {
			return false
		}
	}
	return true
}

func hextetOK(group string) bool {
	if group == "" || len(group) > 4 {
		return false
	}
	for i := 0; i < len(group); i++ {
		c := group[i]
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F') {
			return false
		}
	}
	return true
}

// ipv4Hextets parses a dotted IPv4 suffix as Python's IPv4Address does
// (four decimal octets of at most 3 ASCII digits, no leading zero, each at
// most 255) and gives its two hextets.
func ipv4Hextets(text string) (string, string, bool) {
	octets := strings.Split(text, ".")
	if len(octets) != 4 {
		return "", "", false
	}
	var value [4]int
	for i, octet := range octets {
		if octet == "" || len(octet) > 3 {
			return "", "", false
		}
		if len(octet) > 1 && octet[0] == '0' {
			return "", "", false
		}
		n := 0
		for j := 0; j < len(octet); j++ {
			if octet[j] < '0' || octet[j] > '9' {
				return "", "", false
			}
			n = n*10 + int(octet[j]-'0')
		}
		if n > 255 {
			return "", "", false
		}
		value[i] = n
	}
	const hex = "0123456789abcdef"
	format := func(n int) string {
		if n == 0 {
			return "0"
		}
		out := ""
		for n > 0 {
			out = string(hex[n%16]) + out
			n /= 16
		}
		return out
	}
	return format(value[0]<<8 | value[1]), format(value[2]<<8 | value[3]), true
}

func isASCIIAlnum(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// ipv6Length is how many bytes of an `ipv6` match are the address, or -1
// (checks.ipv6_length): it has to be valid and must not run on into a
// word; a single colon at its end is punctuation. following is the byte
// after the match, or -1 at the end of the line.
func ipv6Length(value []byte, following int) int {
	if !ipv6OK(string(value)) {
		if !bytes.HasSuffix(value, []byte(":")) || bytes.HasSuffix(value, []byte("::")) {
			return -1
		}
		following = ':'
		value = value[:len(value)-1]
		if !ipv6OK(string(value)) {
			return -1
		}
	}
	if following >= 0 && (isASCIIAlnum(byte(following)) || following == '_') {
		return -1
	}
	return len(value)
}

func asciiLower(b []byte) []byte {
	out := make([]byte, len(b))
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		out[i] = c
	}
	return out
}

// kvValueOK tells whether a secret_kv value is worth redacting: not a
// placeholder (null, none, only asterisks) and not already redacted.
func kvValueOK(value []byte) bool {
	lowered := asciiLower(value)
	for _, ignore := range detectors.KvIgnore {
		if string(lowered) == ignore {
			return false
		}
	}
	if bytes.HasPrefix(lowered, []byte("[redacted")) {
		return false
	}
	return len(bytes.Trim(value, "*")) > 0
}

// keyLabel is the label of a secret_kv replacement, from its key.
func keyLabel(key []byte, fallback string) string {
	lowered := string(asciiLower(key))
	for _, entry := range detectors.KeyLabels {
		for _, fragment := range entry.Contains {
			if strings.Contains(lowered, fragment) {
				return entry.Label
			}
		}
	}
	return fallback
}
