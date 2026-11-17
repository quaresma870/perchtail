package redact

import "bytes"

// Content can change encoding part way (the backend's
// encodings.find_reading_change): every line is looked at in the reading it
// is read in, for windows that change how it is read from the start of that
// line. None holds a newline byte, so each lies within one line of bytes.

// UTF32 is what FindReadingChange gives for a window that looks UTF-32:
// the rest of the read is refused.
const UTF32 = "utf-32"

const (
	utf32WindowBytes = 16
	utf16WindowBytes = 8
	asciiWindowBytes = 4
	// changeCarryBytes is how much of what came before a piece of content is
	// looked at again with it, so a window across the two is still found.
	changeCarryBytes = utf32WindowBytes - 1
)

// utf32At: the 16 bytes at i, in groups of 4, each have exactly one byte
// that isn't NUL (nor a newline), at the same place in every group.
func utf32At(data []byte, i int) bool {
	if i+utf32WindowBytes > len(data) {
		return false
	}
	for j := 0; j < 4; j++ {
		ok := true
		for k := 0; k < 4 && ok; k++ {
			group := data[i+4*k : i+4*k+4]
			for p, b := range group {
				if p == j {
					ok = ok && b != 0 && b != '\n'
				} else {
					ok = ok && b == 0
				}
			}
		}
		if ok {
			return true
		}
	}
	return false
}

// firstUTF32 is the first UTF-32 window in data (at file offset at, read as
// reading). In clear UTF-16 LE content none starts on the NUL of a newline
// (0A 00 on an even offset), which ends the line before.
func firstUTF32(data []byte, at int64, reading string) int {
	// Every UTF-32 window holds three NULs in a row.
	first := bytes.Index(data, []byte{0, 0, 0})
	if first < 0 {
		return -1
	}
	for i := max(0, first-(utf32WindowBytes-3)); i+utf32WindowBytes <= len(data); i++ {
		if reading == UTF16LE && i > 0 && data[i-1] == '\n' && data[i] == 0 && (at+int64(i))%2 == 1 {
			continue
		}
		if utf32At(data, i) {
			return i
		}
	}
	return -1
}

// utf16At: the 8 bytes at i are 4 units of 2 bytes, each with exactly one
// NUL, on the same side (the other byte not a newline).
func utf16At(data []byte, i int) bool {
	if i+utf16WindowBytes > len(data) {
		return false
	}
	little, big := true, true
	for k := 0; k < 4; k++ {
		a, b := data[i+2*k], data[i+2*k+1]
		little = little && a != 0 && a != '\n' && b == 0
		big = big && a == 0 && b != 0 && b != '\n'
	}
	return little || big
}

// firstAlignedUTF16 is the first UTF-16 window at an even file offset (data
// starts at file offset at) that ends by end.
func firstAlignedUTF16(data []byte, at int64, end int) int {
	first := bytes.IndexByte(data, 0)
	if first < 0 {
		return -1
	}
	i := max(0, first-(utf16WindowBytes-1))
	if (at+int64(i))%2 != 0 {
		i++
	}
	for ; i+utf16WindowBytes <= end; i += 2 {
		if utf16At(data, i) {
			return i
		}
	}
	return -1
}

func printable(b byte) bool { return b >= 0x20 && b <= 0x7e }

func firstASCII(data []byte, end int) int {
	run := 0
	for i := 0; i < end; i++ {
		if printable(data[i]) {
			run++
			if run == asciiWindowBytes {
				return i - asciiWindowBytes + 1
			}
		} else {
			run = 0
		}
	}
	return -1
}

// FindReadingChange is where in data (content that starts at file offset
// at, read as reading: Bytes, UTF16LE, UTF16BE or UTF16Unsure) the first
// window that changes how it is read starts, and what it changes to: UTF32
// for a UTF-32 window (in any reading), UTF16Unsure for a UTF-16 window in
// content read as bytes or an ASCII window in clear UTF-16 content. When
// two start at the same byte, UTF32 wins. -1 when there is none.
func FindReadingChange(data []byte, at int64, reading string) (int, string) {
	index, change := -1, ""
	if i := firstUTF32(data, at, reading); i >= 0 {
		index, change = i, UTF32
	}
	switch reading {
	case Bytes:
		// Only a window that starts before the UTF-32 one counts.
		end := len(data)
		if index >= 0 {
			end = min(end, index+utf16WindowBytes-1)
		}
		if i := firstAlignedUTF16(data, at, end); i >= 0 {
			index, change = i, UTF16Unsure
		}
	case UTF16LE, UTF16BE:
		end := len(data)
		if index >= 0 {
			end = min(end, index+asciiWindowBytes-1)
		}
		if i := firstASCII(data, end); i >= 0 {
			index, change = i, UTF16Unsure
		}
	}
	return index, change
}

// strongestReadingChange is what the windows anywhere in data change the
// reading to: UTF32 when there is a UTF-32 window, otherwise UTF16Unsure
// when there is a window that changes it to that, otherwise "".
func strongestReadingChange(data []byte, at int64, reading string) string {
	if firstUTF32(data, at, reading) >= 0 {
		return UTF32
	}
	_, change := FindReadingChange(data, at, reading)
	return change
}
