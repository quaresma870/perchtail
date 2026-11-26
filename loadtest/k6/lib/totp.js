// TOTP (RFC 6238, SHA-1, 6 digits, 30-second steps) for the kit's users
// that must use MFA (a role with a global capability, app/auth/mfa_policy.py).
// PerchTail takes each step's code once per account, so a virtual user that
// signs in again within the same step waits for the next one.
import crypto from 'k6/crypto'
import { sleep } from 'k6'

const STEP_SECONDS = 30
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
const lastStep = {}

/** The bytes of a base32 secret, without padding. */
export function base32Bytes(secret) {
  const clean = String(secret).toUpperCase().replace(/=+$/, '')
  const out = []
  let bits = 0
  let value = 0
  for (const ch of clean) {
    const index = ALPHABET.indexOf(ch)
    if (index < 0) throw new Error('not a base32 secret')
    value = (value << 5) | index
    bits += 5
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff)
      bits -= 8
    }
  }
  return new Uint8Array(out)
}

/** The 6-digit code of `secret` for time step `counter`. */
export function totp(secret, counter) {
  const message = new Uint8Array(8)
  let rest = counter
  for (let i = 7; i >= 0; i--) {
    message[i] = rest % 256
    rest = Math.floor(rest / 256)
  }
  const digest = new Uint8Array(
    crypto.hmac('sha1', base32Bytes(secret).buffer, message.buffer, 'binary'),
  )
  const offset = digest[digest.length - 1] & 0x0f
  const code =
    (((digest[offset] & 0x7f) << 24) |
      (digest[offset + 1] << 16) |
      (digest[offset + 2] << 8) |
      digest[offset + 3]) %
    1000000
  return String(code).padStart(6, '0')
}

/** A code of a step `username` hasn't used yet in this virtual user. */
export function freshCode(username, secret) {
  let step = Math.floor(Date.now() / 1000 / STEP_SECONDS)
  const last = lastStep[username]
  if (last !== undefined && step <= last) {
    sleep((last + 1) * STEP_SECONDS - Date.now() / 1000 + 0.5)
    step = last + 1
  }
  lastStep[username] = step
  return totp(secret, step)
}
