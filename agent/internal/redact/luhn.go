package redact

import (
	"fmt"
	"sort"
	"strconv"
	"sync"
)

// cardBrand is one entry of detectors.json's card_brands.
type cardBrand struct {
	Brand   string      `json:"brand"`
	Ranges  [][2]string `json:"ranges"`
	Lengths []int       `json:"lengths"`
}

var (
	brandTableOnce sync.Once
	brandTable     map[string][]int
)

// cardLengths gives the card lengths a number may have, by its first four
// digits: every brand prefix in the table is at most four digits long.
func cardLengths(prefix string) []int {
	brandTableOnce.Do(func() {
		brandTable = map[string][]int{}
		for number := 0; number < 10000; number++ {
			p := fmt.Sprintf("%04d", number)
			set := map[int]bool{}
			for _, brand := range detectors.CardBrands {
				for _, r := range brand.Ranges {
					low, _ := strconv.Atoi(r[0])
					high, _ := strconv.Atoi(r[1])
					value, _ := strconv.Atoi(p[:len(r[0])])
					if low <= value && value <= high {
						for _, length := range brand.Lengths {
							set[length] = true
						}
					}
				}
			}
			if len(set) == 0 {
				continue
			}
			lengths := []int{}
			for length := range set {
				if length >= 12 && length <= 19 {
					lengths = append(lengths, length)
				}
			}
			sort.Ints(lengths)
			brandTable[p] = lengths
		}
	})
	return brandTable[prefix]
}

// cardSpan is a card number inside a `card` match: [start, end) relative to
// the match, and its last four digits.
type cardSpan struct {
	start, end int
	last4      string
}

// cardSpans finds the card numbers inside a `card` match, as
// checks.card_spans does: every window of 12 to 19 digits that starts and
// ends at a separator or at an end of the sequence, whose separators are
// all of one kind, whose check digit is valid (Luhn) and that has the
// prefix and length of a known brand. Windows that overlap are replaced
// together, labelled with the last four digits of the replaced stretch.
func cardSpans(value []byte) []cardSpan {
	positions := make([]int, 0, len(value))
	for i, c := range value {
		if c >= '0' && c <= '9' {
			positions = append(positions, i)
		}
	}
	count := len(positions)
	if count < 12 {
		return nil
	}
	digits := make([]byte, count)
	for k, p := range positions {
		digits[k] = value[p]
	}
	spaces := make([]int, count)
	hyphens := make([]int, count)
	for k := 1; k < count; k++ {
		gap := positions[k] - positions[k-1]
		var separator byte
		if gap == 2 {
			separator = value[positions[k]-1]
		}
		spaces[k] = spaces[k-1]
		hyphens[k] = hyphens[k-1]
		if gap == 2 && separator == ' ' {
			spaces[k]++
		}
		if gap == 2 && separator == '-' {
			hyphens[k]++
		}
	}
	var plain, doubled [2][]int
	for q := 0; q < 2; q++ {
		plain[q] = make([]int, count+1)
		doubled[q] = make([]int, count+1)
	}
	for k := 0; k < count; k++ {
		digit := int(digits[k] - '0')
		twice := digit * 2
		if digit > 4 {
			twice = digit*2 - 9
		}
		for q := 0; q < 2; q++ {
			plain[q][k+1] = plain[q][k]
			doubled[q][k+1] = doubled[q][k]
			if k%2 == q {
				plain[q][k+1] += digit
				doubled[q][k+1] += twice
			}
		}
	}
	startsRun := make([]bool, count)
	for k := 0; k < count; k++ {
		startsRun[k] = k == 0 || positions[k]-positions[k-1] > 1
	}
	endsRun := func(k int) bool { return k == count-1 || startsRun[k+1] }
	type window struct{ first, last int }
	var windows []window
	for first := 0; first < count-11; first++ {
		if !startsRun[first] {
			continue
		}
		lengths := cardLengths(string(digits[first : first+4]))
		for _, length := range lengths {
			end := first + length
			if end > count {
				break
			}
			last := end - 1
			if !endsRun(last) {
				continue
			}
			if spaces[last]-spaces[first] != 0 && hyphens[last]-hyphens[first] != 0 {
				continue
			}
			q := last % 2
			total := plain[q][end] - plain[q][first] + doubled[1-q][end] - doubled[1-q][first]
			if total%10 == 0 {
				windows = append(windows, window{first, last})
			}
		}
	}
	var found []cardSpan
	for _, w := range windows {
		if len(found) > 0 && positions[w.first] < found[len(found)-1].end {
			if positions[w.last]+1 > found[len(found)-1].end {
				found[len(found)-1].end = positions[w.last] + 1
				found[len(found)-1].last4 = string(digits[w.last-3 : w.last+1])
			}
			continue
		}
		found = append(found, cardSpan{positions[w.first], positions[w.last] + 1, string(digits[w.last-3 : w.last+1])})
	}
	return found
}
