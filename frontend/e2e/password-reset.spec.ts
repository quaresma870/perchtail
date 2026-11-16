import { expect, test } from '@playwright/test'

// Password recovery by email (#244), logged out. The recovery API is
// stubbed, so no SMTP server is needed: the link from the email is opened
// directly. The server side (tokens, limits, audit) is covered by
// backend/tests/test_password_reset.py.

const TOKEN = 'e2e-token-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'
const POLICY = {
  min_length: 12,
  max_length: 128,
  reject_simple: true,
  reject_personal: true,
  history: 0,
  required_classes: 0,
  max_age_days: 0,
}

test.describe('logged out', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  test('password reset: no link while recovery is off', async ({ page }) => {
    await page.route('**/auth/login-options', (route) =>
      route.fulfill({ json: { password_reset: false } }),
    )
    await page.goto('/#/login')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Forgot password?' })).toHaveCount(0)
  })

  test('password reset: ask for a link, set the password, back to sign in', async ({ page }) => {
    const requests: unknown[] = []
    const completions: Record<string, unknown>[] = []
    await page.route('**/auth/login-options', (route) =>
      route.fulfill({ json: { password_reset: true } }),
    )
    await page.route('**/auth/password-reset/request', async (route) => {
      requests.push(route.request().postDataJSON())
      await route.fulfill({ status: 202, json: { status: 'accepted' } })
    })
    await page.route('**/auth/password-reset/check', (route) =>
      route.fulfill({ json: { valid: true, policy: POLICY } }),
    )
    await page.route('**/auth/password-reset/complete', async (route) => {
      completions.push(route.request().postDataJSON())
      await route.fulfill({ status: 204 })
    })

    await page.goto('/#/login')
    await page.getByRole('link', { name: 'Forgot password?' }).click()
    await expect(page).toHaveURL(/#\/forgot-password$/)
    await page.getByLabel('Username or email').fill('someone@example.com')
    await page.getByRole('button', { name: 'Send the link' }).click()
    await expect(page.getByRole('status')).toContainText('If an account matches')
    expect(requests).toEqual([{ identifier: 'someone@example.com' }])

    // The link from the email.
    await page.goto(`/#/reset-password/${TOKEN}`)
    await expect(page.getByRole('heading', { name: 'Choose a new password' })).toBeVisible()
    await expect(page.getByLabel('New password')).toBeVisible()
    // The token leaves the address bar at once.
    await expect(page).toHaveURL(/#\/reset-password$/)
    await expect(page.getByRole('list', { name: 'Password rules' })).toBeVisible()

    await page.getByLabel('New password').fill('Fresh-Kettle-Harbor-2031')
    await page.getByRole('button', { name: 'Set the password' }).click()
    await expect(page).toHaveURL(/#\/login$/)
    await expect(page.getByRole('status')).toHaveText(
      'Your password was changed. Sign in with the new one.',
    )
    expect(completions).toEqual([{ token: TOKEN, new_password: 'Fresh-Kettle-Harbor-2031' }])
  })

  test('password reset: an invalid link offers a new one', async ({ page }) => {
    await page.route('**/auth/password-reset/check', (route) =>
      route.fulfill({ json: { valid: false, policy: null } }),
    )
    await page.goto(`/#/reset-password/${TOKEN}`)
    await expect(page.getByText('This link is invalid or has expired.')).toBeVisible()
    await expect(page.getByLabel('New password')).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Ask for a new link' })).toBeVisible()
  })
})
