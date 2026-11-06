import { expect, test } from '@playwright/test'

const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('sources list shows the built-in system source as non-editable', async ({ page }) => {
  await page.goto('/#/settings/sources')

  const row = page.getByRole('row', { name: new RegExp(SYSTEM_SOURCE_NAME) })
  await expect(row).toBeVisible()
  await expect(row.getByText('system')).toBeVisible()

  // CLAUDE.md's "Built-in log viewer" section: shown with a system badge,
  // non-editable, non-deletable -- unlike a normal source's row.
  await expect(row.getByRole('button', { name: 'edit' })).toHaveCount(0)
  await expect(row.getByRole('button', { name: 'delete' })).toHaveCount(0)
})

test('source overview follows the id in the URL (#192)', async ({ page }) => {
  const created = await page.request.post('/sources', {
    data: {
      name: 'E2E Overview Other',
      protocol: 'ssh',
      host: 'overview.invalid',
      port: 22,
      base_path: '/var/log',
    },
  })
  expect(created.ok()).toBeTruthy()
  const other = await created.json()
  const sources = await page.request.get('/sources').then((r) => r.json())
  const system = sources.find((s: { name: string }) => s.name === SYSTEM_SOURCE_NAME)

  try {
    await page.goto(`/#/settings/sources/${system.id}`)
    await expect(page.getByRole('heading', { level: 1, name: SYSTEM_SOURCE_NAME })).toBeVisible()

    await page.evaluate((id) => (window.location.hash = `#/settings/sources/${id}`), other.id)
    await expect(page.getByRole('heading', { level: 1, name: 'E2E Overview Other' })).toBeVisible()
  } finally {
    await page.request.delete(`/sources/${other.id}`)
  }
})

test('sources list sorts and filters, and keeps both after opening a source (#238)', async ({
  page,
}) => {
  await page.goto('/#/settings/sources')

  const nameHeader = page.getByRole('columnheader', { name: /Source/ })
  await nameHeader.getByRole('button').click()
  await expect(nameHeader).toHaveAttribute('aria-sort', 'ascending')
  await nameHeader.getByRole('button').click()
  await expect(nameHeader).toHaveAttribute('aria-sort', 'descending')

  await page.getByLabel('Filter sources').fill('no-such-source-zzz')
  await expect(page.getByText('No sources match.')).toBeVisible()

  await page.getByLabel('Filter sources').fill('application logs')
  await page.getByLabel('Protocol').selectOption({ label: 'Local' })
  const row = page.getByRole('row', { name: new RegExp(SYSTEM_SOURCE_NAME) })
  await expect(row).toBeVisible()
  await expect(page.locator('.filter-count')).toContainText('1 of')

  await row.getByRole('link', { name: SYSTEM_SOURCE_NAME }).click()
  await page.goBack()
  await expect(page.getByLabel('Filter sources')).toHaveValue('application logs')
  await expect(page.getByRole('columnheader', { name: /Source/ })).toHaveAttribute(
    'aria-sort',
    'descending',
  )

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await expect(page.getByLabel('Filter sources')).toHaveValue('')
})
