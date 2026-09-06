import { expect, test } from '@playwright/test'

test('system settings: search toggle affects nav, monitoring token generation', async ({
  page,
}) => {
  await page.goto('/#/settings/system')

  const searchRow = page.locator('.setting-row', { hasText: 'Search' })
  const searchCheckbox = searchRow.locator('input[type=checkbox]')
  // search_view_enabled defaults to true (app/system_settings.py) -- other
  // specs (search.spec.ts, alerts.spec.ts) depend on that default, so this
  // restores it before finishing regardless of how the toggle assertions go.
  await expect(searchCheckbox).toBeChecked()

  // Clicking the wrapping <label class="switch"> (not the <input> directly,
  // which the sibling track span visually covers) -- see roles.spec.ts.
  await searchRow.locator('label.switch').click()
  await expect(searchCheckbox).not.toBeChecked()
  await expect(page.locator('nav').getByRole('link', { name: 'Search' })).toHaveCount(0)
  await expect(page.locator('nav').getByRole('link', { name: 'Alerts' })).toHaveCount(0)

  await searchRow.locator('label.switch').click()
  await expect(searchCheckbox).toBeChecked()
  await expect(page.locator('nav').getByRole('link', { name: 'Search' })).toBeVisible()
  await expect(page.locator('nav').getByRole('link', { name: 'Alerts' })).toBeVisible()

  // Monitoring token: shown once, right after generation.
  await page.getByRole('button', { name: /Generate token|Regenerate token/ }).click()
  await expect(page.locator('.token-box')).toBeVisible()
  await expect(page.locator('.token-box')).not.toBeEmpty()
})
