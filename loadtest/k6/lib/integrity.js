// Checks that what the server sent of a growing file is an unbroken piece
// of it. The files are written by loadtest/seed/growing.py: every line is
// LINE_BYTES long and carries its generation (G), its sequence number in
// that generation (S, from 1, no gaps), when it was written (T, ms since
// the epoch) and the writer's rate (R, bytes/s), then a filler letter
// chosen by the sequence number, then a newline.
//
// Plain JavaScript without k6 imports, so the same code runs in k6 and in
// the unit tests (node --test loadtest/tests/integrity.test.mjs).

export const LINE_BYTES = 128
const NAME_WIDTH = 20
const OFF_GEN = 1
const LEN_GEN = 5
const OFF_SEQ = 8
const LEN_SEQ = 10
const OFF_TS = 20
const LEN_TS = 13
const OFF_RATE = 35
const LEN_RATE = 9
const OFF_FILL_CHECK = 100
const LOW_AT = OFF_SEQ + LEN_SEQ - 4
const FULL_EVERY = 256
const NL = 10
const G = 71
const S = 83

function pad(n, width) {
  const s = String(n)
  return s.length >= width ? s : '0'.repeat(width - s.length) + s
}

/** One line, as growing.py writes it (for the tests). */
export function formatLine(gen, seq, tsMs, rate, name) {
  let head = `G${pad(gen, LEN_GEN)} S${pad(seq, LEN_SEQ)} T${pad(tsMs, LEN_TS)} R${pad(rate, LEN_RATE)} INFO  `
  const shown = name.slice(0, NAME_WIDTH)
  head += shown + ' '.repeat(NAME_WIDTH - shown.length) + ' '
  return head + String.fromCharCode(97 + (seq % 26)).repeat(LINE_BYTES - 1 - head.length) + '\n'
}

/** Bytes of an ASCII string (for the tests). */
export function ascii(text) {
  const out = new Uint8Array(text.length)
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i)
  return out
}

function digits(bytes, at, len) {
  let n = 0
  for (let i = at; i < at + len; i++) {
    const d = bytes[i] - 48
    if (d < 0 || d > 9) return -1
    n = n * 10 + d
  }
  return n
}

function shown(bytes, at, len) {
  let s = ''
  for (let i = at; i < Math.min(bytes.length, at + len); i++) {
    const c = bytes[i]
    s += c >= 32 && c < 127 ? String.fromCharCode(c) : `\\x${c.toString(16).padStart(2, '0')}`
  }
  return s
}

function expected(gen, seq) {
  return `G${gen == null ? '?????' : pad(gen, LEN_GEN)} S${seq == null ? '??????????' : pad(seq, LEN_SEQ)}`
}

/**
 * Checks `bytes` (a Uint8Array) as lines that start at a line boundary.
 * opts.gen: the generation expected (null: whatever the first line says);
 * opts.seq: the sequence number of the first line (null: whatever it says);
 * opts.base: the offset of bytes[0] in the file, for the messages.
 * A last line cut short is allowed (the server read the file while a line
 * was being written) and returned as `tail`, its length in bytes.
 * Returns {ok, lines, gen, firstSeq, lastSeq, firstTs, lastTs, lastRate,
 * tail, error}; `error` describes the first deviation.
 */
export function checkLines(bytes, opts = {}) {
  const base = opts.base || 0
  let gen = opts.gen == null ? null : opts.gen
  let want = opts.seq == null ? null : opts.seq
  const n = Math.floor(bytes.length / LINE_BYTES)
  const out = { ok: true, lines: 0, gen, firstSeq: null, lastSeq: null, firstTs: null, lastTs: null, lastRate: null, tail: 0, error: null }
  const fail = (at, what) => {
    out.ok = false
    out.error = `${what} at byte ${base + at}: expected ${expected(gen, want)}, found "${shown(bytes, at, 18)}"`
    return out
  }
  // Every line's frame (G, S, newline), filler and the last 4 digits of
  // its sequence number; its whole header (generation and the full
  // number) on the first line, every FULL_EVERY lines and on the last
  // one. A gap, a repeat or a line cut short changes the last digits of
  // the next line or shifts the frame; a jump of a multiple of 10 000
  // lines is caught by the next whole header.
  for (let i = 0; i < n; i++) {
    const o = i * LINE_BYTES
    if (bytes[o] !== G || bytes[o + OFF_SEQ - 1] !== S || bytes[o + LINE_BYTES - 1] !== NL) {
      return fail(o, 'not a whole line')
    }
    if (want != null && i > 0 && i % FULL_EVERY !== 0 && i !== n - 1) {
      const low =
        (bytes[o + LOW_AT] - 48) * 1000 +
        (bytes[o + LOW_AT + 1] - 48) * 100 +
        (bytes[o + LOW_AT + 2] - 48) * 10 +
        (bytes[o + LOW_AT + 3] - 48)
      if (low !== want % 10000) {
        const seq = digits(bytes, o + OFF_SEQ, LEN_SEQ)
        if (seq < 0) return fail(o, 'not a whole line')
        return fail(o, seq < want ? 'repeated lines' : `missing lines (${seq - want})`)
      }
      if (bytes[o + OFF_FILL_CHECK] !== 97 + (want % 26)) return fail(o, 'line damaged')
      out.lastSeq = want
      want++
      out.lines++
      continue
    }
    const g = digits(bytes, o + OFF_GEN, LEN_GEN)
    const seq = digits(bytes, o + OFF_SEQ, LEN_SEQ)
    if (g < 0 || seq < 0) return fail(o, 'not a whole line')
    if (bytes[o + OFF_FILL_CHECK] !== 97 + (seq % 26)) return fail(o, 'line damaged')
    if (gen == null) gen = g
    if (g !== gen) return fail(o, g < gen ? 'older generation' : 'generation changed')
    if (want != null && seq !== want) {
      return fail(o, seq < want ? 'repeated lines' : `missing lines (${seq - want})`)
    }
    if (out.firstSeq == null) {
      out.firstSeq = seq
      out.firstTs = digits(bytes, o + OFF_TS, LEN_TS)
    }
    out.lastSeq = seq
    want = seq + 1
    out.lines++
  }
  out.gen = gen
  if (n > 0) {
    const o = (n - 1) * LINE_BYTES
    out.lastTs = digits(bytes, o + OFF_TS, LEN_TS)
    out.lastRate = digits(bytes, o + OFF_RATE, LEN_RATE)
  }
  const rest = bytes.length - n * LINE_BYTES
  if (rest > 0) {
    const o = n * LINE_BYTES
    for (let i = o; i < bytes.length; i++) {
      if (bytes[i] === NL) return fail(o, 'line cut short')
    }
    if (bytes[o] !== G) return fail(o, 'not a whole line')
    if (rest >= OFF_SEQ + LEN_SEQ) {
      const g = digits(bytes, o + OFF_GEN, LEN_GEN)
      const seq = digits(bytes, o + OFF_SEQ, LEN_SEQ)
      if ((gen != null && g !== gen) || (want != null && seq !== want)) {
        return fail(o, 'the last line is out of order')
      }
    }
    out.tail = rest
  }
  return out
}

/**
 * A whole file as /open or /download sent it: from its first line, and
 * as long as the server announced. `declared` is the Content-Length (or
 * null when there was none).
 */
export function checkWhole(bytes, declared, opts = {}) {
  if (declared != null && bytes.length < declared) {
    return { ok: false, error: `shorter than announced: ${bytes.length} of ${declared} bytes`, lines: 0, tail: 0 }
  }
  if (bytes.length === 0) {
    return { ok: true, lines: 0, gen: null, firstSeq: null, lastSeq: null, tail: 0, error: null }
  }
  return checkLines(bytes, { seq: 1, gen: opts.gen == null ? null : opts.gen })
}

function concat(a, b) {
  if (!a || a.length === 0) return b
  const out = new Uint8Array(a.length + b.length)
  out.set(a, 0)
  out.set(b, a.length)
  return out
}

/**
 * A file read piece after piece: a ZIP member as it is inflated, or Follow
 * reading what was appended. Each piece must carry on exactly where the
 * last one stopped, across a line cut in two between pieces.
 */
export class Stream {
  constructor(opts = {}) {
    this.gen = opts.gen == null ? null : opts.gen
    this.seq = opts.seq == null ? 1 : opts.seq
    this.carry = null
    this.offset = opts.offset || 0
    this.lines = 0
    this.error = null
    this.lastTs = null
    this.firstTs = null
    this.lastRate = null
  }

  /** Feeds the next piece; false (and `error`) at the first deviation. */
  push(piece) {
    if (this.error) return false
    if (this.drop) {
      // Still inside the line cut by the start of an end-only read.
      const skipped = Math.min(this.drop, piece.length)
      this.drop -= skipped
      this.offset += skipped
      piece = piece.subarray(skipped)
      if (piece.length === 0) return true
    }
    const bytes = concat(this.carry, piece)
    const base = this.offset - (this.carry ? this.carry.length : 0)
    const r = checkLines(bytes, { gen: this.gen, seq: this.seq, base })
    this.offset += piece.length
    if (!r.ok) {
      this.error = r.error
      return false
    }
    if (r.lines) {
      if (this.gen == null && this.after != null && r.gen <= this.after) {
        this.error = `older generation after a reset: G${pad(r.gen, LEN_GEN)} after G${pad(this.after, LEN_GEN)}`
        return false
      }
      this.gen = r.gen
      this.seq = r.lastSeq + 1
      this.lastTs = r.lastTs
      this.lastRate = r.lastRate
      if (this.firstTs == null && r.firstSeq === 1) this.firstTs = r.firstTs
      this.lines += r.lines
    }
    this.carry = r.tail ? bytes.slice(bytes.length - r.tail) : null
    return true
  }

  /** Starts over from a whole file (Follow after X-Follow-Reset); the new
   * file must be a later generation, or the same one read again. */
  restart(bytes, declared) {
    const r = checkWhole(bytes, declared)
    if (!r.ok) {
      this.error = r.error
      return false
    }
    if (r.lines && this.gen != null && r.gen < this.gen) {
      this.error = `older generation after a reset: G${pad(r.gen, LEN_GEN)} after G${pad(this.gen, LEN_GEN)}`
      return false
    }
    // An empty file (just rotated): the next lines must be a later
    // generation than the one followed so far.
    this.after = r.lines ? null : this.gen
    this.drop = 0
    this.gen = r.lines ? r.gen : null
    this.seq = r.lines ? r.lastSeq + 1 : 1
    this.offset = bytes.length
    this.carry = r.tail ? bytes.slice(bytes.length - r.tail) : null
    if (r.lines) {
      this.lastTs = r.lastTs
      this.lastRate = r.lastRate
      if (r.firstSeq === 1) this.firstTs = r.firstTs
    }
    this.lines = r.lines
    return true
  }

  /**
   * Starts over from the end of a file, as the viewer's "Tail the end" of
   * a large file reads it: `bytes` were read from byte `at`, which may be
   * in the middle of a line. The viewer drops the part line before the
   * first newline; so does this, and what follows must be whole lines in
   * sequence, of the generation followed so far or a later one (after a
   * rotation). The sequence number of the first line isn't known, so the
   * lines are checked against each other from there on.
   */
  restartAt(bytes, at) {
    const skip = (LINE_BYTES - (at % LINE_BYTES)) % LINE_BYTES
    const rest = bytes.length > skip ? bytes.subarray(skip) : new Uint8Array(0)
    const r = checkLines(rest, { base: at + skip })
    if (!r.ok) {
      this.error = r.error
      return false
    }
    if (r.lines && this.gen != null && r.gen < this.gen) {
      this.error = `older generation after a reset: G${pad(r.gen, LEN_GEN)} after G${pad(this.gen, LEN_GEN)}`
      return false
    }
    // No whole line yet: the first one may be of the generation followed
    // so far or a later one.
    this.after = r.lines || this.gen == null ? null : this.gen - 1
    this.drop = bytes.length < skip ? skip - bytes.length : 0
    this.gen = r.lines ? r.gen : null
    this.seq = r.lines ? r.lastSeq + 1 : null
    this.offset = at + bytes.length
    this.carry = r.tail ? rest.slice(rest.length - r.tail) : null
    if (r.lines) {
      this.lastTs = r.lastTs
      this.lastRate = r.lastRate
    }
    this.lines = r.lines
    return true
  }
}
