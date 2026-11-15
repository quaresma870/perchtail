package redact

// ibanLength is how many bytes of an `iban` match are the IBAN (the match
// can run on into a word that follows it), or -1 when it isn't one: a known
// country, that country's length and the mod-97 check (checks.iban_length).
func ibanLength(value []byte) int {
	if len(value) < 2 {
		return -1
	}
	expected, ok := detectors.IbanLengths[string(value[:2])]
	if !ok {
		return -1
	}
	compact := make([]byte, 0, expected)
	end := 0
	for index, c := range value {
		if c == ' ' {
			continue
		}
		compact = append(compact, c)
		end = index + 1
		if len(compact) == expected {
			break
		}
	}
	if len(compact) != expected {
		return -1
	}
	if end < len(value) && value[end] != ' ' {
		return -1
	}
	rearranged := append(append([]byte{}, compact[4:]...), compact[:4]...)
	remainder := 0
	for _, c := range rearranged {
		var digit int
		switch {
		case c >= '0' && c <= '9':
			digit = int(c - '0')
		case c >= 'A' && c <= 'Z':
			digit = int(c-'A') + 10
		case c >= 'a' && c <= 'z':
			digit = int(c-'a') + 10
		default:
			return -1
		}
		if digit >= 10 {
			remainder = (remainder*100 + digit) % 97
		} else {
			remainder = (remainder*10 + digit) % 97
		}
	}
	if remainder != 1 {
		return -1
	}
	return end
}
