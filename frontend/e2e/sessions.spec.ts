import { expect, test } from '@playwright/test'

test('sessions page lists the current session and offers to log it out', async ({ page }) => {
  await page.goto('/#/settings/sessions')

  const currentSessionRow = page.locator('.session-row', { hasText: 'This device' })
  await expect(currentSessionRow).toBeVisible()
  await expect(currentSessionRow.getByRole('button', { name: 'Log out' })).toBeVisible()
})
