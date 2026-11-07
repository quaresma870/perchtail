import { expect, test } from '@playwright/test'

// #276: each source shows its reachability, and "Check now" updates it.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('source status: check now marks the built-in source reachable', async ({ page }) => {
  await page.goto('/#/viewer')
  const row = page.locator('.explorer .source-row', { hasText: SYSTEM_SOURCE_NAME })
  await expect(row.locator('.status-dot')).toBeVisible()

  const checked = page.waitForResponse((r) => r.url().includes('/check') && r.ok())
  // The row's buttons show on hover.
  await row.hover()
  await row.getByRole('button', { name: `Check ${SYSTEM_SOURCE_NAME} now` }).click()
  await checked
  await expect(row.locator('.status-dot')).toHaveClass(/status-ok/)
  await expect(row.locator('.status-dot')).toHaveAttribute('title', /Reachable \(checked/)
})
