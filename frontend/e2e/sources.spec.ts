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
