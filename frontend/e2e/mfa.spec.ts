import { expect, test } from '@playwright/test'
import { totp } from './totp'

const username = `e2e-mfa-user-${Date.now()}`
const tempPassword = 'e2e-mfa-temp-password-123!'
const newPassword = 'e2e-mfa-new-password-456!'

test('mfa: enroll, login with a TOTP code, login with a backup code, disable', async ({
  page,
  browser,
}) => {
  // --- create a throwaway local user (as the seeded super-admin) ---
  await page.goto('/#/settings/users')
  await page.getByPlaceholder('username or email').fill(username)
  await page.getByPlaceholder('temporary password').fill(tempPassword)
  await page.locator('.add-user select').selectOption({ label: 'No Access' })
  await page.getByRole('button', { name: 'Create user' }).click()
  await expect(page.getByRole('row', { name: new RegExp(username) })).toBeVisible()

  // Everything below runs in a separate, logged-out browser context rather
  // than reusing `page` -- `page` carries the shared super-admin session
  // from e2e/.auth/admin.json that every other spec's storageState is
  // seeded from. Clicking "Log out" on it would delete that session
  // server-side (see auth/sessions.py's delete_session), permanently
  // breaking every spec that runs after this one in the same worker.
  const userContext = await browser.newContext()
  const userPage = await userContext.newPage()

  // --- sign in as the throwaway user, clear the forced password change ---
  await userPage.goto('/#/login')
  await userPage.getByLabel('Username').fill(username)
  await userPage.getByLabel('Password').fill(tempPassword)
  await userPage.getByRole('button', { name: 'Sign in' }).click()
  await expect(userPage).toHaveURL(/#\/change-password$/)
  await userPage.getByLabel('Current (temporary) password').fill(tempPassword)
  await userPage.getByLabel('New password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Save' }).click()
  await expect(userPage).toHaveURL(/#\/viewer$/)

  // --- enroll: QR + secret, confirm with a live TOTP code ---
  await userPage.goto('/#/settings/security')
  await userPage.getByRole('button', { name: 'Enable two-factor authentication' }).click()
  await userPage.getByLabel('Current password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Continue' }).click()
  await expect(userPage.locator('img.qr')).toBeVisible()
  const secret = await userPage.locator('.secret code').innerText()

  await userPage.getByLabel('Confirmation code').fill(totp(secret))
  await userPage.getByRole('button', { name: 'Confirm' }).click()

  await expect(userPage.locator('.backup-codes')).toBeVisible()
  const backupCodes = await userPage.locator('.code-grid li').allInnerTexts()
  expect(backupCodes).toHaveLength(10)
  await userPage.getByRole('button', { name: 'Done' }).click()
  await expect(userPage.getByText('Two-factor authentication is on')).toBeVisible()

  // --- login now requires a second factor ---
  await userPage.getByRole('button', { name: 'Log out' }).click()
  await expect(userPage).toHaveURL(/#\/login$/)
  await userPage.getByLabel('Username').fill(username)
  await userPage.getByLabel('Password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Sign in' }).click()
  await expect(userPage.getByText('Enter the 6-digit code')).toBeVisible()

  // A wrong code is rejected without granting a session.
  await userPage.getByLabel('Authentication code').fill('000000')
  await userPage.getByRole('button', { name: 'Verify' }).click()
  await expect(userPage.locator('.error')).toContainText('Invalid authentication code')
  await expect(userPage).not.toHaveURL(/#\/viewer$/)

  // The right code signs in. Confirming enrollment above already consumed
  // "now"'s time-step (TOTP codes are single-use, see backend/app/auth/
  // mfa.py's replay protection), so this needs the *next* step's code
  // rather than colliding with the one already spent.
  await userPage.getByLabel('Authentication code').fill(totp(secret, undefined, 1))
  await userPage.getByRole('button', { name: 'Verify' }).click()
  await expect(userPage).toHaveURL(/#\/viewer$/)

  // --- a backup code works as a one-time substitute for the TOTP code ---
  await userPage.getByRole('button', { name: 'Log out' }).click()
  await expect(userPage).toHaveURL(/#\/login$/)
  await userPage.getByLabel('Username').fill(username)
  await userPage.getByLabel('Password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Sign in' }).click()
  await expect(userPage.getByText('Enter the 6-digit code')).toBeVisible()
  await userPage.getByLabel('Authentication code').fill(backupCodes[0])
  await userPage.getByRole('button', { name: 'Verify' }).click()
  await expect(userPage).toHaveURL(/#\/viewer$/)

  // --- disable MFA, then confirm login no longer asks for a code ---
  await userPage.goto('/#/settings/security')
  await userPage.getByRole('button', { name: 'Disable MFA' }).click()
  await userPage.getByLabel('Current password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Disable MFA' }).click()
  await expect(userPage.getByText('Two-factor authentication is off')).toBeVisible()

  await userPage.getByRole('button', { name: 'Log out' }).click()
  await expect(userPage).toHaveURL(/#\/login$/)
  await userPage.getByLabel('Username').fill(username)
  await userPage.getByLabel('Password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Sign in' }).click()
  await expect(userPage).toHaveURL(/#\/viewer$/)

  await userContext.close()

  // --- cleanup: back on the untouched admin session, deactivate the user ---
  await page.goto('/#/settings/users')
  await page
    .getByRole('row', { name: new RegExp(username) })
    .getByRole('button', { name: 'deactivate' })
    .click()
  await expect(
    page.getByRole('row', { name: new RegExp(username) }).getByText('inactive', { exact: true }),
  ).toBeVisible()
})
