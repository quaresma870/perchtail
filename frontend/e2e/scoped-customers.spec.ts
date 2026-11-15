import { expect, test, type Browser, type Page } from '@playwright/test'

// Customers and folders only within a role's grants: the source editor and
// a delegated admin's role editor list only what the role's grants reach.

// Within the default password policy (#245): nothing from the username.
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

/** Two customers with a folder each (as the seeded super admin), and a
 * role reaching only the first one, with `globalCapabilities`. */
async function setUp(page: Page, label: string, globalCapabilities: string[]) {
  const stamp = `${label} ${Date.now()}`
  const inside = await post(page, '/customers', { name: `E2E In ${stamp}` })
  const outside = await post(page, '/customers', { name: `E2E Out ${stamp}` })
  const inFolder = await post(page, '/folders', { name: `In folder ${stamp}`, customer_id: inside.id })
  const outFolder = await post(page, '/folders', { name: `Out folder ${stamp}`, customer_id: outside.id })
  const role = await post(page, '/roles', {
    name: `E2E Reach ${stamp}`,
    global_capabilities: globalCapabilities,
  })
  await post(page, `/roles/${role.id}/grants`, {
    scope_type: 'customer',
    scope_id: inside.id,
    capabilities: ['view', 'manage_source'],
  })
  const username = `e2e-reach-${label}-${Date.now()}`
  const user = await post(page, '/users', { username, password: tempPassword, role_id: role.id })
  return { stamp, inside, outside, inFolder, outFolder, role, user, username }
}

/** Deactivates the user (users are never deleted, so their role stays) and
 * removes the rest; deleting the customers also deletes the grants on them. */
async function tearDown(page: Page, made: Awaited<ReturnType<typeof setUp>>, extraRoles: number[] = []) {
  await remove(page, `/users/${made.user.id}`)
  for (const id of extraRoles) await remove(page, `/roles/${id}`)
  await remove(page, `/folders/${made.inFolder.id}`)
  await remove(page, `/folders/${made.outFolder.id}`)
  await remove(page, `/customers/${made.inside.id}`)
  await remove(page, `/customers/${made.outside.id}`)
}

/** Signs in as `username` in its own browser context (the shared page keeps
 * the super admin's session) and clears the forced password change. */
async function signIn(browser: Browser, username: string): Promise<Page> {
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
  await expect(page).toHaveURL(/#\/viewer$/)
  return page
}

async function optionLabels(select: import('@playwright/test').Locator): Promise<string[]> {
  return (await select.locator('option').allInnerTexts()).map((label) => label.trim())
}

test('scoped customers: the source editor lists only the customers and folders the role reaches', async ({
  page,
  browser,
}) => {
  const made = await setUp(page, 'creator', ['create_source'])
  const userPage = await signIn(browser, made.username)

  await userPage.goto('/#/settings/sources/new')
  const customer = userPage.getByLabel('Customer')
  await expect(customer.locator('option', { hasText: `E2E In ${made.stamp}` })).toHaveCount(1)
  const customers = await optionLabels(customer)
  expect(customers).not.toContain(`E2E Out ${made.stamp}`)
  // Only a super admin adds customers.
  expect(customers).not.toContain('+ Create new customer…')

  await customer.selectOption({ label: `E2E In ${made.stamp}` })
  const folder = userPage.getByLabel('Folder')
  await expect(folder.locator('option', { hasText: `In folder ${made.stamp}` })).toHaveCount(1)
  expect(await optionLabels(folder)).not.toContain(`Out folder ${made.stamp}`)

  // The super admin still adds customers from the same editor.
  await page.goto('/#/settings/sources/new')
  await expect(
    page.getByLabel('Customer').locator('option', { hasText: '+ Create new customer…' }),
  ).toHaveCount(1)

  await userPage.context().close()
  await tearDown(page, made)
})

test('scoped customers: a delegated role admin without create_source picks customers and folders in reach', async ({
  page,
  browser,
}) => {
  const made = await setUp(page, 'roles', ['manage_roles'])
  const userPage = await signIn(browser, made.username)

  await userPage.goto('/#/settings/roles')
  await userPage.getByRole('button', { name: '+ New role' }).click()
  const roleName = `E2E Delegated ${made.stamp}`
  await userPage.getByLabel('Name').fill(roleName)
  await userPage.getByRole('button', { name: 'Save' }).click()
  await expect(userPage).toHaveURL(/#\/settings\/roles\/\d+$/)
  const newRoleId = Number(userPage.url().match(/roles\/(\d+)$/)![1])

  const addGrantForm = userPage.locator('.add-grant')
  const scopeType = addGrantForm.locator('select').first()
  const target = addGrantForm.locator('select').nth(1)

  await scopeType.selectOption('customer')
  await expect(target.locator('option', { hasText: `E2E In ${made.stamp}` })).toHaveCount(1)
  expect(await optionLabels(target)).not.toContain(`E2E Out ${made.stamp}`)

  await scopeType.selectOption('folder')
  await expect(target.locator('option', { hasText: `In folder ${made.stamp}` })).toHaveCount(1)
  expect(await optionLabels(target)).not.toContain(`Out folder ${made.stamp}`)

  await scopeType.selectOption('customer')
  await target.selectOption({ label: `E2E In ${made.stamp}` })
  await addGrantForm.getByLabel('Grant View', { exact: true }).check()
  await addGrantForm.getByRole('button', { name: 'Add grant' }).click()
  await expect(userPage.getByRole('row', { name: new RegExp(`E2E In ${made.stamp}`) })).toBeVisible()

  await userPage.context().close()
  await tearDown(page, made, [newRoleId])
})
