import { readFileSync } from 'node:fs'
import { expect, test, type Browser, type Page } from '@playwright/test'
import { setUpMfa } from './totp'

// Settings -> Access review (#384): the report as the server computes it,
// its exports downloaded from the server, its audit entry, and a delegated
// admin's report limited to their own scope.

// Within the default password policy (#245): nothing from the usernames.
const tempPassword = 'Lantern-Orbit-temp-58!'
const newPassword = 'Tagus-River-new-73!'

type Created = { id: number }

async function post<T = Created>(page: Page, path: string, data: unknown): Promise<T> {
  const response = await page.request.post(path, { data })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}

async function remove(page: Page, path: string) {
  const response = await page.request.delete(path)
  expect(response.ok(), await response.text()).toBeTruthy()
}

/** Two customers with a source each, and a role with a View grant on the
 * first customer and `globalCapabilities`, held by a new local user (as
 * the seeded super admin). */
async function setUp(page: Page, label: string, globalCapabilities: string[]) {
  const stamp = `${label} ${Date.now()}`
  const inside = await post(page, '/customers', { name: `E2E AR In ${stamp}` })
  const outside = await post(page, '/customers', { name: `E2E AR Out ${stamp}` })
  const source = (name: string, customerId: number) =>
    post(page, '/sources', {
      name,
      protocol: 'ssh',
      host: 'access-report.invalid',
      port: 22,
      base_path: '/var/log',
      customer_id: customerId,
    })
  const inName = `ar-in-${label}-${Date.now()}`
  const outName = `ar-out-${label}-${Date.now()}`
  const inSource = await source(inName, inside.id)
  const outSource = await source(outName, outside.id)
  const role = await post(page, '/roles', {
    name: `E2E AR ${stamp}`,
    global_capabilities: globalCapabilities,
  })
  await post(page, `/roles/${role.id}/grants`, {
    scope_type: 'customer',
    scope_id: inside.id,
    capabilities: ['view'],
  })
  const username = `e2e-ar-${label}-${Date.now()}`
  const user = await post(page, '/users', { username, password: tempPassword, role_id: role.id })
  return { stamp, inside, outside, inSource, outSource, inName, outName, role, user, username }
}

/** Deactivates the user (users are never deleted, so their role stays) and
 * removes the sources and customers. */
async function tearDown(page: Page, made: Awaited<ReturnType<typeof setUp>>) {
  await remove(page, `/users/${made.user.id}`)
  await remove(page, `/sources/${made.inSource.id}`)
  await remove(page, `/sources/${made.outSource.id}`)
  await remove(page, `/customers/${made.inside.id}`)
  await remove(page, `/customers/${made.outside.id}`)
}

/** Signs in as `username` in its own browser context (the shared page keeps
 * the super admin's session) and clears the forced password change; with
 * `mfa`, also sets up two-factor authentication. */
async function signIn(browser: Browser, username: string, mfa: boolean): Promise<Page> {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } })
  const page = await context.newPage()
  await page.goto('/#/login')
  await page.getByLabel('Username').fill(username)
  await page.getByLabel('Password').fill(tempPassword)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/#\/change-password$/)
  await page.getByLabel('Current (temporary) password').fill(tempPassword)
  await page.getByLabel('New password').fill(newPassword)
  await page.getByRole('button', { name: 'Save' }).click()
  if (mfa) {
    await expect(page).toHaveURL(/#\/setup-mfa$/)
    await setUpMfa(page, newPassword)
  }
  await expect(page).toHaveURL(/#\/viewer$/)
  return page
}

async function openReport(page: Page) {
  await page.goto('/#/settings/sources')
  await page.locator('.settings-nav').getByRole('link', { name: 'Access review' }).click()
  await expect(page).toHaveURL(/#\/settings\/access-review$/)
  await expect(page.getByRole('heading', { name: 'Access review' })).toBeVisible()
  await expect(page.getByText(/^Generated at .+ by /)).toBeVisible()
}

test('access review: a super admin sees a local user without MFA flagged and exports CSV and JSON', async ({
  page,
}) => {
  const made = await setUp(page, 'super', [])
  const me: { username: string } = await (await page.request.get('/auth/me')).json()
  try {
    await openReport(page)
    await expect(page.getByText(/limited to your scope/)).toHaveCount(0)
    await expect(page.getByText(new RegExp(`^Generated at .+ by ${me.username}$`))).toBeVisible()

    await page.getByLabel('Filter users').fill(made.username)
    const row = page.locator('tr.user-row', { hasText: made.username })
    await expect(row).toHaveCount(1)
    await expect(row).toContainText(`E2E AR ${made.stamp}`)
    await expect(row).toContainText('Local')
    await expect(row).toContainText('Never')
    await expect(row.locator('.flag', { hasText: 'Local account without MFA' })).toBeVisible()
    await expect(row.locator('.flag', { hasText: 'Must change password' })).toBeVisible()
    await expect(row.locator('.flag', { hasText: 'Never signed in' })).toBeVisible()

    // The flag filters keep the user while its flag is picked.
    await page.getByRole('checkbox', { name: 'Super admin' }).check()
    await expect(row).toHaveCount(0)
    await page.getByRole('checkbox', { name: 'Local account without MFA' }).check()
    await expect(row).toHaveCount(1)
    await page.getByRole('checkbox', { name: 'Super admin' }).uncheck()
    await page.getByRole('checkbox', { name: 'Local account without MFA' }).uncheck()

    // The access, by customer, collapsed until opened, with a search box.
    await row.getByRole('button', { name: `Show access of ${made.username}` }).click()
    const access = page.locator('tr.access-row')
    await expect(access.getByText(`E2E AR In ${made.stamp}`, { exact: true })).toBeVisible()
    const sourceName = access.getByText(made.inName, { exact: true })
    await expect(sourceName).toBeHidden()
    await access.getByLabel(`Search the access of ${made.username}`).fill(made.inName)
    await expect(sourceName).toBeVisible()
    await expect(access.getByText('Role E2E AR').first()).toBeVisible()

    // The days without sign-in recalculate, and the exports carry them.
    const days = page.getByLabel('Days without sign-in')
    await days.fill('0')
    await days.dispatchEvent('change')
    await expect(page.getByText('Enter a whole number of days between 1 and 3650.')).toBeVisible()
    await days.fill('30')
    await days.dispatchEvent('change')
    await expect(page.getByText('Enter a whole number of days between 1 and 3650.')).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Export CSV' })).toHaveAttribute(
      'href',
      '/access-report?view=users&format=csv&inactive_days=30',
    )

    const [csv] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('link', { name: 'Export CSV' }).click(),
    ])
    expect(csv.suggestedFilename()).toMatch(/^perchtail-access-report-users-\d{4}-\d{2}-\d{2}\.csv$/)
    const csvText = readFileSync((await csv.path())!, 'utf-8')
    expect(csvText.split('\r\n')[0]).toContain('generated_at,generated_by,username')
    expect(csvText).toContain(made.username)
    expect(csvText).toContain('local_without_mfa')

    const [json] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('link', { name: 'Export JSON' }).click(),
    ])
    expect(json.suggestedFilename()).toMatch(/^perchtail-access-report-users-\d{4}-\d{2}-\d{2}\.json$/)
    const body = JSON.parse(readFileSync((await json.path())!, 'utf-8'))
    expect(body.generated_by.username).toBe(me.username)
    expect(body.parameters.inactive_days).toBe(30)
    const exported = body.users.find((u: { username: string }) => u.username === made.username)
    expect(exported.flags).toContain('local_without_mfa')

    // By source: the users per capability and the grant behind each.
    await page.getByRole('button', { name: 'By source' }).click()
    await page.getByLabel('Search sources').fill(made.inName)
    const inCard = page.locator('section.source', { hasText: made.inName })
    await expect(inCard).toHaveCount(1)
    await expect(page.locator('section.source', { hasText: made.outName })).toHaveCount(0)
    await expect(inCard).toContainText(made.username)
    await expect(inCard).toContainText('View')
    await expect(inCard).toContainText(`Role E2E AR ${made.stamp}: Customer E2E AR In ${made.stamp}`)

    // Each report generated is in the audit log.
    await page.locator('.settings-nav').getByRole('link', { name: 'Audit log' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Audit log' })).toBeVisible()
    await expect(page.getByRole('cell', { name: 'access_report.generate' }).first()).toBeVisible()
  } finally {
    await tearDown(page, made)
  }
})

test('access review: a delegated admin of one customer sees only that customer', async ({ page, browser }) => {
  const made = await setUp(page, 'delegated', ['view_access_report'])
  const userPage = await signIn(browser, made.username, true)
  try {
    await openReport(userPage)
    await expect(userPage.getByRole('note')).toContainText('This report is limited to your scope')

    await userPage.getByRole('button', { name: 'By source' }).click()
    await expect(userPage.getByRole('heading', { name: made.inName })).toBeVisible()
    await expect(userPage.getByText(made.outName)).toHaveCount(0)
    await expect(userPage.getByText(`E2E AR Out ${made.stamp}`)).toHaveCount(0)

    // Its users view has its own access, inside its customer only.
    await userPage.getByRole('button', { name: 'By user' }).click()
    await userPage.getByLabel('Filter users').fill(made.username)
    await userPage.getByRole('button', { name: `Show access of ${made.username}` }).click()
    await expect(userPage.locator('tr.access-row')).toContainText(`E2E AR In ${made.stamp}`)
    await expect(userPage.locator('tr.access-row')).not.toContainText(`E2E AR Out ${made.stamp}`)

    // The super admin's report has both.
    await openReport(page)
    await page.getByRole('button', { name: 'By source' }).click()
    await expect(page.getByRole('heading', { name: made.outName })).toBeVisible()
    await expect(page.getByRole('heading', { name: made.inName })).toBeVisible()
  } finally {
    await userPage.context().close()
    await tearDown(page, made)
  }
})

test('access review: without the capability there is no tab and the page is refused', async ({
  page,
  browser,
}) => {
  const made = await setUp(page, 'none', [])
  const userPage = await signIn(browser, made.username, false)
  try {
    await userPage.goto('/#/settings/sources')
    await expect(userPage.locator('.settings-nav')).toBeVisible()
    await expect(userPage.locator('.settings-nav').getByRole('link', { name: 'Access review' })).toHaveCount(0)
    await userPage.goto('/#/settings/access-review')
    await expect(userPage.getByText('Not permitted')).toBeVisible()
    await expect(userPage.getByText(/^Generated at/)).toHaveCount(0)
  } finally {
    await userPage.context().close()
    await tearDown(page, made)
  }
})
