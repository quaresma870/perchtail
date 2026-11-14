import { expect, test, type Page } from '@playwright/test'

/** Resolves once a settings save has been answered. */
const saved = (page: Page) =>
  page.waitForResponse(
    (response) => response.url().endsWith('/system-settings') && response.request().method() === 'PATCH',
  )

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
  // The button is disabled while the request runs too: wait for the save
  // itself, or the reload below can cut it short.
  await Promise.all([saved(page), intervalRow.getByRole('button', { name: 'Save' }).click()])
  await expect(intervalRow.getByRole('button', { name: 'Save' })).toBeDisabled()
  await page.reload()
  await expect(interval).toHaveValue('30')
  await interval.fill('60')
  await Promise.all([saved(page), intervalRow.getByRole('button', { name: 'Save' }).click()])
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

test('system settings: the audit folder listings switch stays on when turning it off is refused', async ({
  page,
}) => {
  await page.goto('/#/settings/system')
  const checkbox = page.getByLabel('Audit folder listings')
  const row = page.locator('.setting-row', { has: checkbox })
  // audit_folder_listings defaults to true (app/system_settings.py).
  await expect(checkbox).toBeChecked()
  await expect(checkbox).toBeEnabled()

  // A refusal (the server's answer to anyone but a super admin) puts the
  // switch back. The request never reaches the server, so nothing changes.
  const refused = /\/system-settings$/
  await page.route(refused, (route) =>
    route.request().method() === 'PATCH'
      ? route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Not allowed' }),
        })
      : route.continue(),
  )
  await row.locator('label.switch').click()
  await expect(page.locator('.error', { hasText: 'Not allowed' })).toBeVisible()
  await expect(checkbox).toBeChecked()
  await page.unroute(refused)

  // Whoever manages system settings without being a super admin can't turn
  // it off: the switch is disabled while it's on, and says why.
  await page.route(/\/auth\/me$/, async (route) => {
    const response = await route.fetch()
    const user = await response.json()
    await route.fulfill({
      response,
      json: {
        ...user,
        is_super_admin: false,
        global_capabilities: [...new Set([...user.global_capabilities, 'manage_system_settings'])],
      },
    })
  })
  await page.reload()
  await expect(checkbox).toBeChecked()
  await expect(checkbox).toBeDisabled()
  await expect(row.locator('label.switch')).toHaveAttribute(
    'title',
    'Only a super admin can turn it off.',
  )
})

test('system settings: server limits are saved and kept within range (#344)', async ({ page }) => {
  await page.goto('/#/settings/system')

  const perUser = page.getByLabel('Downloads per user')
  const perSource = page.getByLabel('At once on one source')
  const save = page.getByRole('button', { name: 'Save limits' })
  const card = page.locator('.card', { has: save })
  // Defaults from app/system_settings.py; restored at the end.
  await expect(perUser).toHaveValue('2')
  await expect(save).toBeDisabled()

  await perUser.fill('99')
  await save.click()
  await expect(card.locator('.error')).toContainText('1 to 32')

  await perUser.fill('3')
  await Promise.all([saved(page), save.click()])
  await expect(save).toBeDisabled()
  await page.reload()
  await expect(perUser).toHaveValue('3')

  // More at once on one source than in all is refused by the server.
  await perSource.fill('64')
  await save.click()
  await expect(card.locator('.error')).toContainText("can't be above")

  await perSource.fill('4')
  await perUser.fill('2')
  await Promise.all([saved(page), save.click()])
  await expect(save).toBeDisabled()
})
