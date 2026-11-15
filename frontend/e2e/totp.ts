import crypto from 'node:crypto'
import { expect, type Page } from '@playwright/test'

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

// The seeded e2e super admin's TOTP secret: super admins must use MFA. Must
// match backend/scripts/run_e2e_server.sh (and whatever it was run with).
export const ADMIN_TOTP_SECRET = process.env.E2E_ADMIN_TOTP_SECRET ?? 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'

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

/** On the forced "Set up two-factor authentication" page: enrolls with the
 * account's `password`, confirms with a live code, dismisses the backup
 * codes. Returns the secret, for codes at later sign-ins. */
export async function setUpMfa(page: Page, password: string): Promise<string> {
  await expect(page.getByRole('heading', { name: 'Set up two-factor authentication' })).toBeVisible()
  await page.getByRole('button', { name: 'Enable two-factor authentication' }).click()
  await page.getByLabel('Current password').fill(password)
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.locator('img.qr')).toBeVisible()
  const secret = await page.locator('.secret code').innerText()
  await page.getByLabel('Confirmation code').fill(totp(secret))
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.locator('.backup-codes')).toBeVisible()
  await page.getByRole('button', { name: 'Done' }).click()
  return secret
}
