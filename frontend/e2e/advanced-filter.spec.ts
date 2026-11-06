import { expect, test } from '@playwright/test'

// #274: tick a source, then filter its files by name.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('advanced filter: files of a ticked source, filtered by name or glob', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.getByRole('button', { name: 'Advanced filter' }).click()
  await expect(page.getByText('Tick the sources and folders to filter files in.')).toBeVisible()

  await page.getByLabel(`Filter files in ${SYSTEM_SOURCE_NAME}`).check()
  const results = page.locator('.scoped-results')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()

  await page.getByLabel('Filter files', { exact: true }).fill('*.no-such-extension')
  await expect(results.getByText('No file names match.')).toBeVisible()
  await page.getByLabel('Filter files', { exact: true }).fill('perchtail.l?g')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()

  // Opening a match opens it in the viewer.
  await results.getByRole('button', { name: 'perchtail.log', exact: true }).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')

  await page.getByRole('button', { name: 'Clear all' }).click()
  await expect(page.getByText('Tick the sources and folders to filter files in.')).toBeVisible()
  await page.getByRole('button', { name: 'Advanced filter' }).click()
  await expect(page.getByPlaceholder('Search by folder, customer, or host…')).toBeVisible()
})
