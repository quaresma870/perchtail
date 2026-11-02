import crypto from 'node:crypto'

// A minimal RFC 6238 TOTP generator, just enough to drive the MFA e2e spec
// against a real secret returned by /auth/mfa/enroll -- deliberately
// hand-rolled instead of adding an otplib-style dependency for one test
// file (mirrors run_e2e_server.sh's own "no new dependency for a single
// test helper" bias).
const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

function base32Decode(input: string): Buffer {
  const clean = input.toUpperCase().replace(/=+$/, '')
  let bits = ''
  for (const char of clean) {
    const val = BASE32_ALPHABET.indexOf(char)
    if (val === -1) continue
    bits += val.toString(2).padStart(5, '0')
  }
  const bytes: number[] = []
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2))
  }
  return Buffer.from(bytes)
}

export function totp(secret: string, atMs: number = Date.now(), stepOffset = 0): string {
  const timeStepSeconds = 30
  const digits = 6
  const key = base32Decode(secret)
  const counter = Math.floor(atMs / 1000 / timeStepSeconds) + stepOffset
  const counterBuf = Buffer.alloc(8)
  counterBuf.writeBigUInt64BE(BigInt(counter))
  const hmac = crypto.createHmac('sha1', key).update(counterBuf).digest()
  const offset = hmac[hmac.length - 1] & 0x0f
  const binary =
    ((hmac[offset] & 0x7f) << 24) |
    ((hmac[offset + 1] & 0xff) << 16) |
    ((hmac[offset + 2] & 0xff) << 8) |
    (hmac[offset + 3] & 0xff)
  const otp = binary % 10 ** digits
  return otp.toString().padStart(digits, '0')
}
