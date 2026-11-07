import { expect, test } from '@playwright/test'

// #272, #273, #282: files show their time, the order follows the System
// default (no sort control in the explorer), and an open source can be
// refreshed in place.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('explorer: files show their time, and an open source refreshes in place', async ({ page }) => {
  await page.goto('/#/viewer')
  await expect(page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME })).toBeVisible()
  await expect(page.getByLabel('Sort files')).toHaveCount(0)

  // Open the built-in source: its log file shows with a time next to it.
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  const logFile = page.locator('.tree-body').getByRole('button', { name: /perchtail\.log/ }).first()
  await expect(logFile).toBeVisible()
  await expect(logFile.locator('.time')).not.toBeEmpty()

  // Refreshing lists it again without collapsing it.
  const listed = page.waitForResponse((r) => r.url().includes('/browse?path=') && r.status() === 200)
  // The row's buttons show on hover.
  await page.locator('.explorer .source-row', { hasText: SYSTEM_SOURCE_NAME }).hover()
  await page.getByRole('button', { name: `Refresh ${SYSTEM_SOURCE_NAME}` }).click()
  await listed
  await expect(logFile).toBeVisible()
})
