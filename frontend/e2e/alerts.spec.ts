import { expect, test } from '@playwright/test'
import { confirmPopup } from './popups'

// example.com resolves to a real, public, non-loopback/non-private address
// so app/webhook_safety.py's SSRF guard (checked at alert-create time)
// accepts it -- a reserved-TLD host like ".invalid" would fail DNS
// resolution and get rejected right at creation, before there'd be
// anything to click "Test" on.
const alertName = `E2E Alert ${Date.now()}`

test('alerts: create, edit, test webhook, toggle enabled, delete', async ({ page }) => {
  await page.goto('/#/alerts')
  await page.getByRole('button', { name: '+ New alert' }).click()

  await page.getByLabel('Name').fill(alertName)
  // An invalid RE2 regex is refused with the backend's reason.
  await page.getByLabel('Query').fill('(unclosed')
  await page.getByLabel('Regular expression').check()
  // Leave Watch as "Every source I can view" -- doesn't depend on any
  // other spec's transient sources existing.
  await expect(page.getByLabel('Watch')).toHaveValue('')
  await expect(page.getByLabel('Files')).toHaveValue('**/*.log')
  await page.getByLabel('Webhook URL').fill('https://example.com/e2e-webhook-test')
  await page.getByRole('button', { name: 'Create alert' }).click()
  await expect(page.locator('.alert-form .error')).toContainText('invalid regex')

  await page.getByLabel('Regular expression').uncheck()
  await page.getByLabel('Query').fill('connection refused')
  await page.getByRole('button', { name: 'Create alert' }).click()

  const row = page.locator('.alert-row', { hasText: alertName })
  await expect(row).toBeVisible()
  await expect(row.locator('.query')).toHaveText('connection refused')
  await expect(row).toContainText('Every source you can view')
  await expect(row).toContainText('**/*.log')

  await row.getByRole('button', { name: 'Edit' }).click()
  await page.getByLabel('Files').fill('**/*.txt')
  await page.getByRole('button', { name: 'Save changes' }).click()
  await expect(page.locator('.alert-row', { hasText: alertName })).toContainText('**/*.txt')

  await row.getByRole('button', { name: 'Test' }).click()
  await expect(row.getByText(/Sent ✓|Failed/)).toBeVisible({ timeout: 15_000 })

  const enabledCheckbox = row.locator('input[type=checkbox]')
  await expect(enabledCheckbox).toBeChecked()
  // Clicking the wrapping <label class="switch"> (not the <input> directly,
  // which the sibling track span visually covers) -- see roles.spec.ts.
  await row.locator('label.switch').click()
  await expect(page.locator('.alert-row', { hasText: alertName }).locator('input[type=checkbox]')).not.toBeChecked()

  await page.locator('.alert-row', { hasText: alertName }).getByRole('button', { name: 'Delete' }).click()
  await confirmPopup(page, 'Delete')
  await expect(page.locator('.alert-row', { hasText: alertName })).toHaveCount(0)
})
