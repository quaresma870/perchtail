import { expect, test, type Page } from '@playwright/test'

/** Resolves once a settings save has been answered. */
const saved = (page: Page) =>
  page.waitForResponse(
    (response) => response.url().endsWith('/system-settings') && response.request().method() === 'PATCH',
  )

/** Serves /auth/me as this browser's user with only these global capabilities. */
async function actAs(page: Page, capabilities: string[]) {
  await page.route(/\/auth\/me$/, async (route) => {
    const response = await route.fetch()
    const user = await response.json()
    await route.fulfill({
      response,
      json: { ...user, is_super_admin: false, global_capabilities: capabilities },
    })
  })
}

test('settings sidebar: grouped sections, the current page marked, the old System address redirected', async ({
  page,
}) => {
  await page.goto('/#/settings/system')
  await expect(page).toHaveURL(/#\/settings\/explorer$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Explorer and limits' })).toBeVisible()

  const sidebar = page.getByRole('navigation', { name: 'Settings' })
  for (const group of ['Sources', 'Access', 'Content', 'Operations', 'Audit']) {
    await expect(sidebar.getByRole('list', { name: group, exact: true })).toBeVisible()
  }
  // A line between sections, none before the first or after the last; each
  // link has its decorative icon, the name stays the page's.
  await expect(sidebar.getByRole('separator')).toHaveCount(4)
  const groupsBox = sidebar.locator('.nav-groups')
  await expect(groupsBox.locator(':scope > :first-child')).toHaveClass(/nav-group/)
  await expect(groupsBox.locator(':scope > :last-child')).toHaveClass(/nav-group/)
  await expect(groupsBox.locator(':scope > .nav-group + .nav-group')).toHaveCount(0)
  const current = sidebar.getByRole('link', { name: 'Explorer and limits', exact: true })
  await expect(current.locator('svg[aria-hidden="true"]')).toHaveCount(1)
  await expect(current).toHaveAttribute('aria-current', 'page')
  await expect(sidebar.locator('[aria-current="page"]')).toHaveCount(1)

  await sidebar.getByRole('link', { name: 'Sign-in security' }).click()
  await expect(page).toHaveURL(/#\/settings\/sign-in-security$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Sign-in security' })).toBeVisible()
  await expect(sidebar.getByRole('link', { name: 'Sign-in security' })).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(current).not.toHaveAttribute('aria-current', 'page')

  // A page below a sidebar entry keeps the entry marked.
  await page.goto('/#/settings/sources')
  await expect(sidebar.getByRole('link', { name: 'Sources', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  )
})

test('settings sidebar: below 768 px a select per section takes its place and navigates', async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 800 })
  await page.goto('/#/settings/explorer')
  await expect(page.getByRole('heading', { level: 1, name: 'Explorer and limits' })).toBeVisible()

  const select = page.getByLabel('Settings page')
  await expect(select).toBeVisible()
  await expect(select).toHaveValue('/settings/explorer')
  await expect(page.locator('.settings-nav').getByRole('link', { name: 'Sources', exact: true })).toBeHidden()
  await expect(select.locator('optgroup')).toHaveCount(5)

  await select.selectOption({ label: 'Alerts' })
  await expect(page).toHaveURL(/#\/settings\/alerts$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Alerts' })).toBeVisible()

  // Nothing wider than the phone's screen.
  const overflow = await page.evaluate(() => {
    const content = document.querySelector('.content') as HTMLElement
    return {
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      content: content.scrollWidth - content.clientWidth,
    }
  })
  expect(overflow).toEqual({ page: 0, content: 0 })

  // From 768 px the sidebar is back.
  await page.setViewportSize({ width: 768, height: 800 })
  await expect(select).toBeHidden()
  await expect(page.locator('.settings-nav').getByRole('link', { name: 'Alerts' })).toBeVisible()
})

test('settings sidebar: a user with create_source only sees the Sources section', async ({ page }) => {
  await actAs(page, ['create_source'])
  await page.goto('/#/settings/sources')
  const sidebar = page.getByRole('navigation', { name: 'Settings' })
  await expect(sidebar.getByRole('link')).toHaveText(['Sources', 'Folders', 'Agents'])
  await expect(sidebar.getByRole('list')).toHaveCount(1)
  // A single section has no line.
  await expect(sidebar.getByRole('separator')).toHaveCount(0)
})

test('system settings: alert interval, monitoring token generation', async ({ page }) => {
  await page.goto('/#/settings/alerts')
  await expect(page.getByRole('heading', { level: 1, name: 'Alerts' })).toBeVisible()

  // Alerts no longer hang off a Search toggle (#303): the nav link is always there.
  const mainNav = page.locator('main > nav')
  await expect(mainNav.getByRole('link', { name: 'Search' })).toHaveCount(0)
  await expect(mainNav.getByRole('link', { name: 'Alerts' })).toBeVisible()

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
  await page.goto('/#/settings/monitoring')
  await expect(page.getByRole('heading', { level: 1, name: 'Monitoring' })).toBeVisible()
  await page.getByRole('button', { name: /Generate token|Regenerate token/ }).click()
  await expect(page.locator('.token-box')).toBeVisible()
  await expect(page.locator('.token-box')).not.toBeEmpty()
})

test('system settings: each page shows only its own blocks', async ({ page }) => {
  const pages: [string, string, string[], string[]][] = [
    [
      'source-defaults',
      'Source defaults',
      ['Background check interval in minutes', 'Index file names', 'Credential age warning in days', 'Folders per source'],
      ['Alert check interval in seconds', 'Tail interval'],
    ],
    [
      'sign-in-security',
      'Sign-in security',
      ['Require MFA for local accounts', 'Password recovery by email', 'Minimum length'],
      ['Folders per source', 'Backup interval in hours'],
    ],
    [
      'explorer',
      'Explorer and limits',
      ['Connections view', 'Tail interval', 'Downloads per user'],
      ['Alert check interval in seconds', 'Audit log retention in days'],
    ],
    ['alerts', 'Alerts', ['Alert check interval in seconds'], ['Tail interval', 'Backup interval in hours']],
    [
      'backups',
      'Backups and updates',
      ['Backup interval in hours', 'Check for new releases'],
      ['Alert check interval in seconds'],
    ],
    ['monitoring', 'Monitoring', [], ['Alert check interval in seconds', 'Connections view']],
  ]
  for (const [path, title, shown, hidden] of pages) {
    await page.goto(`/#/settings/${path}`)
    await expect(page.getByRole('heading', { level: 1, name: title })).toBeVisible()
    await expect(page.getByText('Loading…', { exact: true })).toHaveCount(0)
    for (const label of shown) await expect(page.getByLabel(label, { exact: true }).first()).toBeVisible()
    for (const label of hidden) await expect(page.getByLabel(label, { exact: true })).toHaveCount(0)
  }
})

test('audit log page: turning the viewer off keeps the settings for whoever manages them', async ({
  page,
}) => {
  await page.goto('/#/settings/audit-log')

  const auditCheckbox = page.getByLabel('Audit log', { exact: true })
  const auditRow = page.locator('.setting-row', { has: auditCheckbox })
  const verify = page.getByRole('button', { name: 'Verify now' })
  const navLink = page.locator('.settings-nav').getByRole('link', { name: 'Audit log' })
  // audit_view_enabled defaults to true (app/system_settings.py) -- restored
  // at the end however the assertions below go.
  await expect(auditCheckbox).toBeChecked()
  await expect(verify).toBeVisible()
  // The switch's help says who keeps the page once it is off.
  await expect(auditRow.locator('.hint')).toContainText(
    'hides the log and redirects away from the page for everyone except those who manage system settings, who keep its settings',
  )

  try {
    await auditRow.locator('label.switch').click()
    await expect(auditCheckbox).not.toBeChecked()
    // The viewer goes; the settings and the nav link stay, so it can be
    // turned on again.
    await expect(verify).toHaveCount(0)
    await expect(navLink).toBeVisible()
    await page.reload()
    await expect(page).toHaveURL(/#\/settings\/audit-log$/)
    await expect(page.getByLabel('Audit log retention in days')).toBeVisible()
    await expect(verify).toHaveCount(0)

    // Someone who only reads the audit log loses the link, and the route
    // itself is guarded, not just the link (see App.svelte): the guard
    // pushes to bare /settings, which SettingsIndex.svelte replaces with
    // /settings/sources -- the settled URL this waits for.
    await actAs(page, ['view_audit_log'])
    await page.goto('/#/settings/sources')
    await page.reload()
    await expect(page.getByRole('heading', { level: 1, name: 'Sources' })).toBeVisible()
    await expect(navLink).toHaveCount(0)
    await page.goto('/#/settings/audit-log')
    await expect(page).toHaveURL(/#\/settings\/sources$/)
    await page.unroute(/\/auth\/me$/)
    await page.reload()
    await page.goto('/#/settings/audit-log')
    await expect(page.getByRole('heading', { level: 1, name: 'Audit log' })).toBeVisible()
    await page.locator('.setting-row', { has: auditCheckbox }).locator('label.switch').click()
    await expect(auditCheckbox).toBeChecked()
    await expect(verify).toBeVisible()
  } finally {
    const restored = await page.request.patch('/system-settings', { data: { audit_view_enabled: true } })
    expect(restored.ok()).toBeTruthy()
  }
})

test('audit log page: a reader sees only the viewer, a settings manager only the settings', async ({
  page,
}) => {
  await actAs(page, ['view_audit_log'])
  await page.goto('/#/settings/audit-log')
  await expect(page.getByRole('heading', { level: 1, name: 'Audit log' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Verify now' })).toBeVisible()
  await expect(page.getByLabel('Audit log retention in days')).toHaveCount(0)
  await page.unroute(/\/auth\/me$/)

  await actAs(page, ['manage_system_settings'])
  await page.reload()
  await expect(page.getByLabel('Audit log retention in days')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Verify now' })).toHaveCount(0)
})

test('system settings: the audit folder listings switch stays on when turning it off is refused', async ({
  page,
}) => {
  await page.goto('/#/settings/audit-log')
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
  await page.goto('/#/settings/explorer')

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
