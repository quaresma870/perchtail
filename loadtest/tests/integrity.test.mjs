// Unit tests of the checks the growing-file scenarios make (k6/lib/integrity.js)
// and of the ZIP reader (k6/lib/zip.js). Run with:
//   node --test loadtest/tests/integrity.test.mjs
// (test_kit.py runs it too when node is on the PATH).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import zlib from 'node:zlib'
import { ascii, checkLines, checkWhole, formatLine, LINE_BYTES, Stream } from '../k6/lib/integrity.js'
import { crc32, inflateRaw, listZip, readMember } from '../k6/lib/zip.js'

const T = 1763150000000
const RATE = 262144

function file(gen, from, to, name = 'growing-1') {
  let s = ''
  for (let seq = from; seq <= to; seq++) s += formatLine(gen, seq, T + seq, RATE, name)
  return ascii(s)
}

function join(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.length
  }
  return out
}

test('the line layout is the one loadtest/seed/growing.py writes', () => {
  // The same line as test_kit.py's GrowingTests.test_line_layout.
  assert.equal(
    formatLine(1, 1, T, RATE, 'growing-1'),
    'G00001 S0000000001 T1763150000000 R000262144 INFO  growing-1            ' + 'b'.repeat(55) + '\n',
  )
  assert.equal(formatLine(12, 345, T, RATE, 'x').length, LINE_BYTES)
})

test('a whole file from its first line passes, with a last line cut short', () => {
  const whole = file(1, 1, 50)
  const r = checkWhole(whole, whole.length)
  assert.ok(r.ok, r.error)
  assert.equal(r.lines, 50)
  assert.equal(r.lastSeq, 50)
  const cut = whole.slice(0, whole.length - 70)
  const c = checkWhole(cut, cut.length)
  assert.ok(c.ok, c.error)
  assert.equal(c.lines, 49)
  assert.equal(c.tail, LINE_BYTES - 70)
})

test('an empty file passes', () => {
  assert.ok(checkWhole(new Uint8Array(0), 0).ok)
})

test('a gap is found', () => {
  const r = checkWhole(join(file(1, 1, 10), file(1, 12, 20)), null)
  assert.equal(r.ok, false)
  assert.match(r.error, /missing lines \(1\) at byte 1280: expected G00001 S0000000011/)
})

test('repeated lines are found', () => {
  const r = checkWhole(join(file(1, 1, 10), file(1, 9, 20)), null)
  assert.equal(r.ok, false)
  assert.match(r.error, /repeated lines at byte 1280/)
})

test('a line cut in the middle is found', () => {
  const whole = file(1, 1, 10)
  const broken = join(whole.slice(0, 5 * LINE_BYTES + 40), whole.slice(6 * LINE_BYTES))
  const r = checkWhole(broken, null)
  assert.equal(r.ok, false)
  assert.match(r.error, /at byte 640/)
})

test('a file that does not start at its first line is found', () => {
  const r = checkWhole(file(1, 5, 10), null)
  assert.equal(r.ok, false)
  assert.match(r.error, /missing lines \(4\) at byte 0/)
})

test('an answer shorter than announced is found', () => {
  const whole = file(1, 1, 10)
  const r = checkWhole(whole.slice(0, 1000), whole.length)
  assert.equal(r.ok, false)
  assert.match(r.error, /shorter than announced: 1000 of 1280 bytes/)
})

test('a damaged line is found', () => {
  const whole = file(1, 1, 10)
  whole[3 * LINE_BYTES + 100] = 0x5a
  const r = checkWhole(whole, null)
  assert.equal(r.ok, false)
  assert.match(r.error, /line damaged at byte 384/)
})

test('Follow: pieces cut anywhere chain, through a rotation', () => {
  const stream = new Stream()
  const first = file(1, 1, 20)
  assert.ok(stream.restart(first.slice(0, 1000), 1000), stream.error)
  // What was appended, read in pieces that cut lines anywhere.
  const rest = join(first.slice(1000), file(1, 21, 40))
  for (let at = 0; at < rest.length; at += 333) {
    assert.ok(stream.push(rest.slice(at, Math.min(rest.length, at + 333))), stream.error)
  }
  assert.equal(stream.seq, 41)
  assert.equal(stream.gen, 1)
  // X-Follow-Reset: the file was rotated, the viewer reloads the new one.
  assert.ok(stream.restart(file(2, 1, 5), null), stream.error)
  assert.ok(stream.push(file(2, 6, 9)), stream.error)
  assert.equal(stream.gen, 2)
  assert.equal(stream.seq, 10)
})

test('Follow: a reset to an empty file, then the next generation', () => {
  const stream = new Stream()
  assert.ok(stream.restart(file(3, 1, 4), null))
  assert.ok(stream.restart(new Uint8Array(0), 0))
  assert.equal(stream.push(file(3, 1, 2)), false)
  assert.match(stream.error, /older generation after a reset/)
  const again = new Stream()
  assert.ok(again.restart(file(3, 1, 4), null))
  assert.ok(again.restart(new Uint8Array(0), 0))
  assert.ok(again.push(file(4, 1, 2)), again.error)
})

test('Follow: a gap or a repeat between reads is found', () => {
  const gap = new Stream()
  assert.ok(gap.restart(file(1, 1, 10), null))
  assert.equal(gap.push(file(1, 12, 13)), false)
  assert.match(gap.error, /missing lines \(1\) at byte 1280/)
  const repeat = new Stream()
  assert.ok(repeat.restart(file(1, 1, 10), null))
  assert.equal(repeat.push(file(1, 10, 11)), false)
  assert.match(repeat.error, /repeated lines/)
})

test('Follow: a reload of an older generation is found', () => {
  const stream = new Stream()
  assert.ok(stream.restart(file(2, 1, 10), null))
  assert.equal(stream.restart(file(1, 1, 10), null), false)
  assert.match(stream.error, /older generation after a reset: G00001 after G00002/)
})

test("Follow from a file's end: the part line is dropped, then pieces chain", () => {
  // The viewer's "Tail the end": read from a byte in the middle of a line.
  const whole = file(1, 1, 100)
  const at = 30 * LINE_BYTES + 57
  const stream = new Stream()
  assert.ok(stream.restartAt(whole.slice(at, 60 * LINE_BYTES + 10), at), stream.error)
  assert.equal(stream.gen, 1)
  assert.equal(stream.seq, 61)
  assert.equal(stream.lines, 29)
  assert.ok(stream.push(whole.slice(60 * LINE_BYTES + 10)), stream.error)
  assert.equal(stream.seq, 101)
  // A gap after the end read is found.
  assert.equal(stream.push(file(1, 102, 103)), false)
  assert.match(stream.error, /missing lines \(1\)/)
})

test("Follow from a file's end: a read shorter than the part line, then the rest", () => {
  const whole = file(4, 1, 20)
  const at = 5 * LINE_BYTES + 10
  const stream = new Stream()
  assert.ok(stream.restartAt(whole.slice(at, at + 50), at), stream.error)
  assert.equal(stream.lines, 0)
  assert.ok(stream.push(whole.slice(at + 50)), stream.error)
  assert.equal(stream.gen, 4)
  assert.equal(stream.seq, 21)
  assert.equal(stream.lines, 14)
})

test("Follow from a file's end, through a rotation", () => {
  const stream = new Stream()
  const first = file(2, 1, 50)
  assert.ok(stream.restartAt(first.slice(1000), 1000), stream.error)
  // After X-Follow-Reset, the end of the new file: a later generation.
  const next = file(3, 1, 30)
  assert.ok(stream.restartAt(next.slice(700), 700), stream.error)
  assert.equal(stream.gen, 3)
  assert.ok(stream.push(file(3, 31, 32)), stream.error)
  // An older generation is found.
  assert.equal(stream.restartAt(file(1, 1, 30).slice(700), 700), false)
  assert.match(stream.error, /older generation after a reset: G00001 after G00003/)
})

test("Follow from a file's end: a bad line after the part line is found", () => {
  const whole = file(1, 1, 40)
  const broken = join(whole.slice(0, 20 * LINE_BYTES + 40), whole.slice(21 * LINE_BYTES))
  const stream = new Stream()
  assert.equal(stream.restartAt(broken.slice(10 * LINE_BYTES + 3), 10 * LINE_BYTES + 3), false)
  assert.match(stream.error, /at byte 2560/)
})

test('checkLines reports the offset in the file', () => {
  const r = checkLines(file(1, 3, 3), { gen: 1, seq: 2, base: 4096 })
  assert.match(r.error, /missing lines \(1\) at byte 4096/)
})

// --- ZIP ---------------------------------------------------------------------

function u16(n) {
  return [n & 0xff, (n >>> 8) & 0xff]
}
function u32(n) {
  return [n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff]
}

/** A ZIP as zipfile writes it to an unseekable stream: each member's
 * sizes in a data descriptor after its data, then the central directory. */
function makeZip(members) {
  const parts = []
  const central = []
  let offset = 0
  for (const { name, data } of members) {
    const packed = new Uint8Array(zlib.deflateRawSync(data, { level: 6 }))
    const crc = crc32(0, data)
    const nameBytes = ascii(name)
    const local = Uint8Array.from([...u32(0x04034b50), ...u16(20), ...u16(8), ...u16(8), ...u32(0), ...u32(0), ...u32(0), ...u32(0), ...u16(nameBytes.length), ...u16(0)])
    const descriptor = Uint8Array.from([...u32(0x08074b50), ...u32(crc), ...u32(packed.length), ...u32(data.length)])
    parts.push(local, nameBytes, packed, descriptor)
    central.push(
      join(
        Uint8Array.from([...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(8), ...u16(8), ...u32(0), ...u32(crc), ...u32(packed.length), ...u32(data.length), ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0), ...u32(offset)]),
        nameBytes,
      ),
    )
    offset += local.length + nameBytes.length + packed.length + descriptor.length
  }
  const cd = join(...central)
  const end = Uint8Array.from([...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(members.length), ...u16(members.length), ...u32(cd.length), ...u32(offset), ...u16(0)])
  return join(...parts, cd, end)
}

function checkZip(zip) {
  const problems = []
  for (const entry of listZip(zip)) {
    const stream = new Stream()
    const problem = readMember(zip, entry, (piece) => stream.push(piece))
    if (problem || stream.error) problems.push(`${entry.name}: ${problem || stream.error}`)
  }
  return problems
}

test('inflate matches zlib: stored, fixed and dynamic blocks', () => {
  const random = new Uint8Array(300000)
  for (let i = 0; i < random.length; i++) random[i] = (i * 2654435761) >>> 24
  const lines = file(1, 1, 5000)
  for (const data of [random, lines, ascii('hello'), new Uint8Array(0)]) {
    for (const level of [0, 1, 9]) {
      const packed = new Uint8Array(zlib.deflateRawSync(data, { level }))
      const pieces = []
      inflateRaw(packed, 0, packed.length, (piece) => pieces.push(piece.slice()))
      assert.deepEqual(join(...pieces), data, `level ${level}, ${data.length} bytes`)
    }
  }
})

test('a ZIP of growing files passes, member by member', () => {
  const zip = makeZip([
    { name: 'growing-1.log', data: file(2, 1, 3000) },
    { name: 'growing-1.log.1', data: file(1, 1, 4000) },
  ])
  assert.deepEqual(checkZip(zip), [])
})

test('a ZIP member with a gap, or damaged, is found', () => {
  const gap = makeZip([{ name: 'growing-1.log', data: join(file(1, 1, 100), file(1, 102, 200)) }])
  assert.match(checkZip(gap)[0], /growing-1.log: missing lines \(1\)/)
  const zip = makeZip([{ name: 'growing-1.log', data: file(1, 1, 3000) }])
  zip[200] ^= 0xff
  assert.equal(checkZip(zip).length, 1)
})

test('a ZIP cut short is found', () => {
  const zip = makeZip([{ name: 'growing-1.log', data: file(1, 1, 3000) }])
  assert.throws(() => listZip(zip.slice(0, zip.length - 30)), /cut short/)
})
