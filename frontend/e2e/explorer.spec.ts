import { expect, test } from '@playwright/test'

// #272, #273: the file order is each user's choice and is kept on the
// server; an open source can be refreshed in place.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('explorer: the file order persists, and an open source refreshes in place', async ({ page }) => {
  await page.goto('/#/viewer')
  const sort = page.getByLabel('Sort files')
  await expect(sort).toHaveValue('name_asc')

  const saved = page.waitForResponse((r) => r.url().includes('/auth/preferences') && r.ok())
  await sort.selectOption('mtime_desc')
  await saved
  await page.reload()
  await expect(page.getByLabel('Sort files')).toHaveValue('mtime_desc')

  // Open the built-in source: its log file shows with a time next to it.
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  const logFile = page.locator('.tree-body').getByRole('button', { name: /perchtail\.log/ }).first()
  await expect(logFile).toBeVisible()
  await expect(logFile.locator('.time')).not.toBeEmpty()

  // Refreshing lists it again without collapsing it.
  const listed = page.waitForResponse((r) => r.url().includes('/browse?path=') && r.status() === 200)
  await page.getByRole('button', { name: `Refresh ${SYSTEM_SOURCE_NAME}` }).click({ force: true })
  await listed
  await expect(logFile).toBeVisible()

  // Back to the default for the specs that follow.
  const reset = page.waitForResponse((r) => r.url().includes('/auth/preferences') && r.ok())
  await page.getByLabel('Sort files').selectOption('name_asc')
  await reset
  await expect(page.getByLabel('Sort files')).toHaveValue('name_asc')
})
