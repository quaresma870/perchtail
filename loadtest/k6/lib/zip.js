// Reads the folder ZIPs PerchTail streams (GET /download-zip): the central
// directory, and each member inflated piece by piece with its CRC-32
// checked, so a member of hundreds of MB is checked without being held
// whole. k6 has no inflate of its own; this is RFC 1951 in plain
// JavaScript (no k6 imports, so the unit tests run it in node too).

const CRC_TABLE = (() => {
  const t = new Int32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c
  }
  return t
})()

/** Updates a CRC-32 (start with 0) with `bytes`. */
export function crc32(crc, bytes) {
  let c = ~crc
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8)
  return ~c >>> 0
}

// --- inflate ----------------------------------------------------------------

const LENGTH_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258]
const LENGTH_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
const DIST_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577]
const DIST_EXTRA = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
const CLEN_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]

/** A lookup table for a canonical Huffman code: indexed by the next
 * `bits` bits of input (least significant first), each entry is
 * symbol << 4 | code length. */
function huffman(lengths) {
  let max = 0
  for (const l of lengths) if (l > max) max = l
  if (max === 0) return { bits: 1, table: new Int32Array(2).fill(-1) }
  const count = new Int32Array(16)
  for (const l of lengths) if (l) count[l]++
  const next = new Int32Array(16)
  let code = 0
  for (let len = 1; len <= 15; len++) {
    code = (code + count[len - 1]) << 1
    next[len] = code
  }
  const size = 1 << max
  const table = new Int32Array(size).fill(-1)
  for (let sym = 0; sym < lengths.length; sym++) {
    const len = lengths[sym]
    if (!len) continue
    let c = next[len]++
    let rev = 0
    for (let i = 0; i < len; i++) {
      rev = (rev << 1) | (c & 1)
      c >>= 1
    }
    for (let i = rev; i < size; i += 1 << len) table[i] = (sym << 4) | len
  }
  return { bits: max, table }
}

let FIXED = null
function fixedCodes() {
  if (!FIXED) {
    const lit = new Array(288)
    for (let i = 0; i < 144; i++) lit[i] = 8
    for (let i = 144; i < 256; i++) lit[i] = 9
    for (let i = 256; i < 280; i++) lit[i] = 7
    for (let i = 280; i < 288; i++) lit[i] = 8
    FIXED = { lit: huffman(lit), dist: huffman(new Array(30).fill(5)) }
  }
  return FIXED
}

const WINDOW = 32768
const OUT_SIZE = 1 << 18
const LIMIT = OUT_SIZE - 300

/**
 * Inflates raw DEFLATE data `src[start, end)`, handing the output to
 * `onData(piece)` in pieces (a piece is only valid during the call).
 * Returns the number of bytes produced. Throws on bad data.
 */
export function inflateRaw(src, start, end, onData) {
  let pos = start
  let bitBuf = 0
  let bitCnt = 0
  const out = new Uint8Array(OUT_SIZE)
  let op = 0
  let total = 0

  function need(n) {
    while (bitCnt < n) {
      const byte = pos < end ? src[pos] : 0
      if (pos >= end + 4) throw new Error('deflate data ends early')
      pos++
      bitBuf |= byte << bitCnt
      bitCnt += 8
    }
  }
  function bits(n) {
    if (n === 0) return 0
    need(n)
    const v = bitBuf & ((1 << n) - 1)
    bitBuf >>>= n
    bitCnt -= n
    return v
  }
  function decode(h) {
    need(h.bits)
    const e = h.table[bitBuf & ((1 << h.bits) - 1)]
    if (e < 0) throw new Error('bad Huffman code')
    const len = e & 15
    bitBuf >>>= len
    bitCnt -= len
    return e >> 4
  }
  function flush(keep) {
    const upto = op - keep
    if (upto <= 0) return
    onData(out.subarray(0, upto))
    total += upto
    out.copyWithin(0, upto, op)
    op = keep
  }
  function room() {
    if (op >= LIMIT) flush(WINDOW)
  }

  let last = 0
  while (!last) {
    last = bits(1)
    const type = bits(2)
    if (type === 0) {
      // Whole bytes already in the bit buffer belong to the block.
      pos -= bitCnt >> 3
      bitBuf = 0
      bitCnt = 0
      if (pos + 4 > end) throw new Error('stored block header cut short')
      const len = src[pos] | (src[pos + 1] << 8)
      const nlen = src[pos + 2] | (src[pos + 3] << 8)
      if ((len ^ 0xffff) !== nlen) throw new Error('bad stored block length')
      pos += 4
      if (pos + len > end) throw new Error('stored block cut short')
      let done = 0
      while (done < len) {
        room()
        const n = Math.min(len - done, LIMIT - op)
        out.set(src.subarray(pos + done, pos + done + n), op)
        op += n
        done += n
      }
      pos += len
      continue
    }
    let lit
    let dist
    if (type === 1) {
      const f = fixedCodes()
      lit = f.lit
      dist = f.dist
    } else if (type === 2) {
      const hlit = bits(5) + 257
      const hdist = bits(5) + 1
      const hclen = bits(4) + 4
      const clen = new Array(19).fill(0)
      for (let i = 0; i < hclen; i++) clen[CLEN_ORDER[i]] = bits(3)
      const ch = huffman(clen)
      const lengths = []
      while (lengths.length < hlit + hdist) {
        const sym = decode(ch)
        if (sym < 16) lengths.push(sym)
        else if (sym === 16) {
          if (!lengths.length) throw new Error('repeat with no previous length')
          const prev = lengths[lengths.length - 1]
          for (let r = 3 + bits(2); r > 0; r--) lengths.push(prev)
        } else if (sym === 17) for (let r = 3 + bits(3); r > 0; r--) lengths.push(0)
        else for (let r = 11 + bits(7); r > 0; r--) lengths.push(0)
      }
      lit = huffman(lengths.slice(0, hlit))
      dist = huffman(lengths.slice(hlit, hlit + hdist))
    } else {
      throw new Error('bad block type')
    }
    // The hot loop, with the bit reading written out (function calls cost
    // a lot in k6's JavaScript engine).
    const litTable = lit.table
    const litBits = lit.bits
    const litMask = (1 << litBits) - 1
    const distTable = dist.table
    const distBits = dist.bits
    const distMask = (1 << distBits) - 1
    for (;;) {
      while (bitCnt < litBits) {
        if (pos >= end + 4) throw new Error('deflate data ends early')
        bitBuf |= (pos < end ? src[pos] : 0) << bitCnt
        pos++
        bitCnt += 8
      }
      let e = litTable[bitBuf & litMask]
      if (e < 0) throw new Error('bad Huffman code')
      bitBuf >>>= e & 15
      bitCnt -= e & 15
      const sym = e >> 4
      if (sym < 256) {
        if (op >= LIMIT) flush(WINDOW)
        out[op++] = sym
        continue
      }
      if (sym === 256) break
      const li = sym - 257
      if (li >= 29) throw new Error('bad length code')
      let len = LENGTH_BASE[li]
      const le = LENGTH_EXTRA[li]
      if (le) {
        while (bitCnt < le) {
          bitBuf |= (pos < end ? src[pos] : 0) << bitCnt
          pos++
          bitCnt += 8
        }
        len += bitBuf & ((1 << le) - 1)
        bitBuf >>>= le
        bitCnt -= le
      }
      while (bitCnt < distBits) {
        if (pos >= end + 4) throw new Error('deflate data ends early')
        bitBuf |= (pos < end ? src[pos] : 0) << bitCnt
        pos++
        bitCnt += 8
      }
      e = distTable[bitBuf & distMask]
      if (e < 0) throw new Error('bad Huffman code')
      bitBuf >>>= e & 15
      bitCnt -= e & 15
      const di = e >> 4
      if (di >= 30) throw new Error('bad distance code')
      let d = DIST_BASE[di]
      const de = DIST_EXTRA[di]
      if (de) {
        while (bitCnt < de) {
          bitBuf |= (pos < end ? src[pos] : 0) << bitCnt
          pos++
          bitCnt += 8
        }
        d += bitBuf & ((1 << de) - 1)
        bitBuf >>>= de
        bitCnt -= de
      }
      if (d > op) throw new Error('distance too far back')
      if (op >= LIMIT) flush(WINDOW)
      if (d >= len) {
        out.copyWithin(op, op - d, op - d + len)
        op += len
      } else if (d === 1) {
        out.fill(out[op - 1], op, op + len)
        op += len
      } else {
        let from = op - d
        for (let i = 0; i < len; i++) out[op++] = out[from++]
      }
    }
  }
  flush(0)
  return total
}

// --- ZIP --------------------------------------------------------------------

function u16(b, at) {
  return b[at] | (b[at + 1] << 8)
}
function u32(b, at) {
  return (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16) | (b[at + 3] << 24)) >>> 0
}
function u64(b, at) {
  return u32(b, at) + u32(b, at + 4) * 4294967296
}

function utf8(bytes) {
  let s = ''
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i])
  try {
    return decodeURIComponent(escape(s))
  } catch (e) {
    return s
  }
}

/** The members listed in a ZIP's central directory: {name, method, crc,
 * compressedSize, size, dataStart}. Throws when it isn't a whole ZIP. */
export function listZip(b) {
  let eocd = -1
  for (let i = b.length - 22; i >= Math.max(0, b.length - 22 - 65535); i--) {
    if (u32(b, i) === 0x06054b50) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new Error('no end of central directory: the ZIP is cut short')
  let count = u16(b, eocd + 10)
  let cdOffset = u32(b, eocd + 16)
  if ((count === 0xffff || cdOffset === 0xffffffff) && eocd >= 20 && u32(b, eocd - 20) === 0x07064b50) {
    const z64 = u64(b, eocd - 12)
    if (u32(b, z64) !== 0x06064b50) throw new Error('bad ZIP64 end of central directory')
    count = u64(b, z64 + 32)
    cdOffset = u64(b, z64 + 48)
  }
  const entries = []
  let p = cdOffset
  for (let n = 0; n < count; n++) {
    if (u32(b, p) !== 0x02014b50) throw new Error(`bad central directory entry ${n}`)
    const method = u16(b, p + 10)
    const crc = u32(b, p + 16)
    let compressedSize = u32(b, p + 20)
    let size = u32(b, p + 24)
    const nameLen = u16(b, p + 28)
    const extraLen = u16(b, p + 30)
    const commentLen = u16(b, p + 32)
    let local = u32(b, p + 42)
    const name = utf8(b.subarray(p + 46, p + 46 + nameLen))
    let x = p + 46 + nameLen
    const xEnd = x + extraLen
    while (x + 4 <= xEnd) {
      const id = u16(b, x)
      const len = u16(b, x + 2)
      if (id === 0x0001) {
        let f = x + 4
        if (size === 0xffffffff) {
          size = u64(b, f)
          f += 8
        }
        if (compressedSize === 0xffffffff) {
          compressedSize = u64(b, f)
          f += 8
        }
        if (local === 0xffffffff) local = u64(b, f)
      }
      x += 4 + len
    }
    if (u32(b, local) !== 0x04034b50) throw new Error(`bad local header for ${name}`)
    const dataStart = local + 30 + u16(b, local + 26) + u16(b, local + 28)
    if (dataStart + compressedSize > b.length) throw new Error(`${name} is cut short`)
    entries.push({ name, method, crc, compressedSize, size, dataStart })
    p += 46 + nameLen + extraLen + commentLen
  }
  return entries
}

/** Hands one member's content to `onData` in pieces; checks its size and
 * CRC-32. Returns null when it is whole, or what is wrong. */
export function readMember(b, entry, onData) {
  let crc = 0
  let size = 0
  const take = (piece) => {
    crc = crc32(crc, piece)
    size += piece.length
    onData(piece)
  }
  try {
    if (entry.method === 0) take(b.subarray(entry.dataStart, entry.dataStart + entry.compressedSize))
    else if (entry.method === 8) inflateRaw(b, entry.dataStart, entry.dataStart + entry.compressedSize, take)
    else return `compression method ${entry.method} not supported`
  } catch (e) {
    return `can't inflate: ${e.message}`
  }
  if (size !== entry.size) return `size ${size}, the directory says ${entry.size}`
  if (crc !== entry.crc) return 'CRC-32 mismatch'
  return null
}
