import { expect, test } from '@playwright/test'

// Settings -> Email (#244): "Remove the saved password" clears and disables
// the password field, and saving sends the removal, never what was typed.
// The SMTP settings API is stubbed, so no SMTP server or DNS is needed.

const SAVED = {
  host: 'smtp.example.test',
  port: 587,
  security: 'starttls',
  username: 'relay',
  password_set: true,
  from_address: 'perchtail@example.test',
  from_name: 'PerchTail',
  tls_ca_pem: null,
  enabled: true,
  updated_at: '2026-11-14T00:00:00',
}

test('email settings: removing the saved password clears the field and sends the removal', async ({
  page,
}) => {
  const puts: Record<string, unknown>[] = []
  await page.route(/\/smtp-settings$/, async (route) => {
    const request = route.request()
    if (request.method() === 'PUT') {
      puts.push(request.postDataJSON())
      await route.fulfill({ json: { ...SAVED, password_set: false } })
    } else {
      await route.fulfill({ json: SAVED })
    }
  })

  await page.goto('/#/settings/email')
  await expect(page.getByRole('heading', { level: 1, name: 'Email settings' })).toBeVisible()
  const password = page.locator('input[type="password"]')
  const remove = page.getByLabel('Remove the saved password')

  await password.fill('typed-but-not-wanted')
  await remove.check()
  await expect(password).toHaveValue('')
  await expect(password).toBeDisabled()

  // Unticking gives the field back, empty.
  await remove.uncheck()
  await expect(password).toBeEnabled()
  await expect(password).toHaveValue('')

  await password.fill('typed-again')
  await remove.check()
  await expect(password).toHaveValue('')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Saved' })).toBeVisible()

  expect(puts).toHaveLength(1)
  expect(puts[0].password).toBe('')
  await expect(remove).toHaveCount(0)
})
