import { expect, test } from '@playwright/test'

test('system settings: alert interval, monitoring token generation', async ({ page }) => {
  await page.goto('/#/settings/system')

  // Alerts no longer hang off a Search toggle (#303): the nav link is always there.
  await expect(page.locator('nav').getByRole('link', { name: 'Search' })).toHaveCount(0)
  await expect(page.locator('nav').getByRole('link', { name: 'Alerts' })).toBeVisible()

  const interval = page.getByLabel('Alert check interval in seconds')
  const intervalRow = page.locator('.setting-row', { has: interval })
  // alerts_interval_seconds defaults to 60 (app/system_settings.py); out of
  // range is refused before saving, and the default is restored at the end.
  await expect(interval).toHaveValue('60')
  await interval.fill('5')
  await intervalRow.getByRole('button', { name: 'Save' }).click()
  await expect(intervalRow.locator('.error')).toContainText('15 to 3600')

  await interval.fill('30')
  await intervalRow.getByRole('button', { name: 'Save' }).click()
  await expect(intervalRow.getByRole('button', { name: 'Save' })).toBeDisabled()
  await page.reload()
  await expect(interval).toHaveValue('30')
  await interval.fill('60')
  await intervalRow.getByRole('button', { name: 'Save' }).click()
  await expect(intervalRow.getByRole('button', { name: 'Save' })).toBeDisabled()

  // Monitoring token: shown once, right after generation.
  await page.getByRole('button', { name: /Generate token|Regenerate token/ }).click()
  await expect(page.locator('.token-box')).toBeVisible()
  await expect(page.locator('.token-box')).not.toBeEmpty()
})

test('system settings: audit log toggle hides the nav link and redirects the route', async ({
  page,
}) => {
  await page.goto('/#/settings/system')

  const auditRow = page.locator('.setting-row', { hasText: 'Audit log' }).first()
  const auditCheckbox = auditRow.locator('input[type=checkbox]')
  // audit_view_enabled defaults to true (app/system_settings.py) -- restore
  // it before finishing regardless of how the assertions below go.
  await expect(auditCheckbox).toBeChecked()

  await auditRow.locator('label.switch').click()
  await expect(auditCheckbox).not.toBeChecked()
  await expect(page.locator('.settings-nav').getByRole('link', { name: 'Audit log' })).toHaveCount(
    0,
  )

  // The route itself is guarded, not just the link (see App.svelte) -- a
  // bookmarked/typed URL should bounce away, not render a page whose own
  // nav tab no longer exists. The guard pushes to bare /settings, which
  // SettingsIndex.svelte immediately replaces with /settings/sources -- that
  // second hop is the actually-settled URL this waits for.
  await page.goto('/#/settings/audit-log')
  await expect(page).toHaveURL(/#\/settings\/sources$/)

  await page.goto('/#/settings/system')
  await page.locator('.setting-row', { hasText: 'Audit log' }).first().locator('label.switch').click()
  await expect(auditCheckbox).toBeChecked()
  await expect(
    page.locator('.settings-nav').getByRole('link', { name: 'Audit log' }),
  ).toBeVisible()
})
