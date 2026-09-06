import { expect, test } from '@playwright/test'

// example.com resolves to a real, public, non-loopback/non-private address
// so app/webhook_safety.py's SSRF guard (checked at alert-create time)
// accepts it -- a reserved-TLD host like ".invalid" would fail DNS
// resolution and get rejected right at creation, before there'd be
// anything to click "Test" on.
const alertName = `E2E Alert ${Date.now()}`

test('alerts: create, test webhook, toggle enabled, delete', async ({ page }) => {
  await page.goto('/#/alerts')
  await page.getByRole('button', { name: '+ New alert' }).click()

  await page.getByLabel('Name').fill(alertName)
  await page.getByLabel('Query').fill('connection refused')
  // Leave Source as "All sources you can view" -- doesn't depend on any
  // other spec's transient sources existing.
  await page.getByLabel('Webhook URL').fill('https://example.com/e2e-webhook-test')
  await page.getByRole('button', { name: 'Create alert' }).click()

  const row = page.locator('.alert-row', { hasText: alertName })
  await expect(row).toBeVisible()
  await expect(row.locator('.query')).toHaveText('connection refused')

  await row.getByRole('button', { name: 'Test' }).click()
  await expect(row.getByText(/Sent ✓|Failed/)).toBeVisible({ timeout: 15_000 })

  const enabledCheckbox = row.locator('input[type=checkbox]')
  await expect(enabledCheckbox).toBeChecked()
  // Clicking the wrapping <label class="switch"> (not the <input> directly,
  // which the sibling track span visually covers) -- see roles.spec.ts.
  await row.locator('label.switch').click()
  await expect(page.locator('.alert-row', { hasText: alertName }).locator('input[type=checkbox]')).not.toBeChecked()

  page.once('dialog', (dialog) => dialog.accept())
  await page.locator('.alert-row', { hasText: alertName }).getByRole('button', { name: 'Delete' }).click()
  await expect(page.locator('.alert-row', { hasText: alertName })).toHaveCount(0)
})
