import { expect, test } from '@playwright/test'
import { ADMIN_TOTP_SECRET, totp } from './totp'

// Runs logged out regardless of the "chromium" project's default storageState
// (see playwright.config.ts) -- login itself can't be tested from an
// already-authenticated context.
test.use({ storageState: { cookies: [], origins: [] } })

const ADMIN_USERNAME = process.env.E2E_ADMIN_USERNAME ?? 'e2e-admin'
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'e2e-test-password-123!'

test('visiting a protected route while logged out redirects to login', async ({ page }) => {
  await page.goto('/#/viewer')
  await expect(page).toHaveURL(/#\/login$/)
})

test('wrong password shows an error and does not navigate away', async ({ page }) => {
  await page.goto('/#/login')
  await page.getByLabel('Username').fill(ADMIN_USERNAME)
  await page.getByLabel('Password').fill('definitely-not-the-password')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page.locator('.error')).toBeVisible()
  await expect(page).toHaveURL(/#\/login$/)
})

test('correct credentials sign in and land on the viewer', async ({ page }) => {
  await page.goto('/#/login')
  await page.getByLabel('Username').fill(ADMIN_USERNAME)
  await page.getByLabel('Password').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  // Super admins must use MFA. The setup project may have spent this time
  // step's code already (codes are single-use), so take the next one.
  await page.getByLabel('Authentication code').fill(totp(ADMIN_TOTP_SECRET, undefined, 1))
  await page.getByRole('button', { name: 'Verify' }).click()

  await expect(page).toHaveURL(/#\/viewer$/)
})
