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
  await expect(page.locator('.alert-form .error')).toContainText(/invalid regex/i)

  await page.getByLabel('Regular expression').uncheck()
  await page.getByLabel('Query').fill('connection refused')
  await page.getByRole('button', { name: 'Create alert' }).click()

  const row = page.locator('.alert-row', { hasText: alertName })
  await expect(row).toBeVisible()
  await expect(row.locator('.query')).toHaveText('connection refused')
  await expect(row).toContainText('Every source you can view')
  await expect(row).toContainText('**/*.log')
  // The webhook URL is stored encrypted and only its host is shown.
  await expect(row).toContainText('https://example.com/…')
  await expect(row).not.toContainText('e2e-webhook-test')

  await row.getByRole('button', { name: 'Edit' }).click()
  // Left empty, the stored URL is kept.
  await expect(page.getByLabel('Webhook URL')).toHaveValue('')
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

test('alerts: an http:// webhook is refused, and an old one is flagged in the list', async ({
  page,
}) => {
  await page.goto('/#/alerts')
  await page.getByRole('button', { name: '+ New alert' }).click()
  await page.getByLabel('Name').fill(`${alertName} plain`)
  await page.getByLabel('Query').fill('connection refused')
  await page.getByLabel('Webhook URL').fill('http://example.com/e2e-webhook-test')
  await page.getByRole('button', { name: 'Create alert' }).click()
  await expect(page.locator('.alert-form .error')).toContainText('must start with https://')

  // An alert saved with http:// before https:// was required: no API makes
  // one any more, so the list answer is stood in for.
  await page.route('**/alerts', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      json: [
        {
          id: 999999,
          name: 'Old plain alert',
          query: 'refused',
          regex: false,
          source_id: null,
          folder_id: null,
          customer_id: null,
          file_glob: '**/*.log',
          webhook_url_masked: 'http://example.com/…',
          webhook_insecure: true,
          enabled: true,
          last_checked_at: null,
          last_fired_at: null,
          failed_deliveries: 0,
          last_error: null,
        },
      ],
    })
  })
  await page.reload()
  const row = page.locator('.alert-row', { hasText: 'Old plain alert' })
  await expect(row.locator('.insecure-warning')).toContainText("aren't encrypted")
})
