import { expect, test } from '@playwright/test'

// #274, #300: one search box; with sources ticked (Filter sources), it searches their files live.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('search: files of a ticked source, filtered by name or glob', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.getByRole('button', { name: 'Filter sources', exact: true }).click()
  await expect(page.getByText('Tick the sources and folders to search their files live.')).toBeVisible()

  // Ticking keeps the tree, so several can be ticked; "Show the files"
  // (or typing a filter) shows the matches.
  await page.getByLabel(`Filter files in ${SYSTEM_SOURCE_NAME}`).check()
  await expect(page.getByLabel(`Filter files in ${SYSTEM_SOURCE_NAME}`)).toBeChecked()
  await page.getByRole('button', { name: 'Show the files (1)' }).click()
  const results = page.locator('.scoped-results')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()

  await page.getByLabel('Search sources and files').fill('*.no-such-extension')
  await expect(results.getByText('No file names match.')).toBeVisible()
  await page.getByLabel('Search sources and files').fill('perchtail.l?g')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()

  // A regex is written between slashes (#300); an invalid one says so.
  await page.getByLabel('Search sources and files').fill('/^perchtail\\.l.g$/')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()
  await page.getByLabel('Search sources and files').fill('/perchtail(/')
  await expect(page.getByText('Invalid regex')).toBeVisible()
  await page.getByLabel('Search sources and files').fill('perchtail.l?g')
  await expect(results.getByRole('button', { name: 'perchtail.log', exact: true })).toBeVisible()

  // Opening a match opens it in the viewer.
  await results.getByRole('button', { name: 'perchtail.log', exact: true }).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')

  await page.getByRole('button', { name: 'Clear all' }).click()
  await expect(page.getByText('Tick the sources and folders to search their files live.')).toBeVisible()
  await page.getByRole('button', { name: 'Filter sources', exact: true }).click()
  await expect(page.getByLabel(`Filter files in ${SYSTEM_SOURCE_NAME}`)).toHaveCount(0)
})

test('search: with nothing ticked, finds sources by name and reveals them', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.getByLabel('Search sources and files').fill('PerchTail application')
  const results = page.locator('.search-results')
  const row = results.locator('.row.source').filter({ hasText: SYSTEM_SOURCE_NAME })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page.getByLabel('Search sources and files')).toHaveValue('')
  await expect(results).toHaveCount(0)
})
