package redact

import "bytes"

var pemBeginLiteral = []byte("-----BEGIN")

// pemSpans is the private-key state machine over whole lines of work: it
// adds the spans to replace (when spans isn't nil) and carries the
// open/closed state to the next block. `-----BEGIN ... PRIVATE KEY-----`
// up to the matching END becomes one `[redacted: private key]`, the lines
// inside it become empty (their line breaks stay, so line numbers don't
// move), and a BEGIN without an END hides at most pemMax lines.
func (r *Redactor) pemSpans(work []byte, spans *[]span, state *pemState) {
	if r.rules.pemBegin == nil {
		return
	}
	if !state.open && !bytes.Contains(work, pemBeginLiteral) {
		return
	}
	sub := subject{work: work}
	position, size := 0, len(work)
	for position < size {
		if !state.open {
			begin := bytes.Index(work[position:], pemBeginLiteral)
			if begin < 0 {
				return
			}
			begin += position
			if nl := bytes.LastIndexByte(work[position:begin], '\n'); nl >= 0 {
				position += nl + 1
			}
		}
		end := bytes.IndexByte(work[position:], '\n')
		if end < 0 {
			end = size
		} else {
			end += position
		}
		r.pemLine(&sub, position, end, spans, state)
		position = end + 1
	}
}

func (r *Redactor) pemLine(sub *subject, start, end int, spans *[]span, state *pemState) {
	work := sub.work
	contentEnd := end
	if end > start && work[end-1] == '\r' {
		contentEnd = end - 1
	}
	add := func(spanStart, spanEnd int, repl []byte, counted bool) {
		if spans != nil && spanStart < spanEnd {
			*spans = append(*spans, span{spanStart, spanEnd, 0, r.rules.pemRule, repl, counted})
		}
	}
	beginText := sub.text(r.rules.pemBegin)
	endText := sub.text(r.rules.pemEnd)
	cursor := start
	if state.open {
		found := r.rules.pemEnd.re.FindIndex(endText[start:contentEnd])
		if found == nil {
			add(start, contentEnd, nil, false)
			state.lines++
			if state.lines >= r.pemMax {
				state.open = false
			}
			return
		}
		add(start, start+found[1], nil, false)
		state.open = false
		cursor = start + found[1]
	}
	lastBegin := []int(nil)
	for _, m := range r.rules.pemBegin.re.FindAllIndex(beginText[cursor:contentEnd], -1) {
		lastBegin = []int{cursor + m[0], cursor + m[1]}
	}
	if lastBegin == nil || r.rules.pemEnd.re.FindIndex(endText[lastBegin[1]:contentEnd]) != nil {
		return
	}
	add(lastBegin[0], contentEnd, Marker(r.rules.rules[r.rules.pemRule].label), true)
	state.open = true
	state.lines = 1
	if state.lines >= r.pemMax {
		state.open = false
	}
}
