import { expect, test } from '@playwright/test'
import { confirmPopup } from './popups'

const roleName = `E2E Role ${Date.now()}`
const customerName = `E2E Role Test Customer ${Date.now()}`

// Roles has no dedicated "create a customer" affordance of its own -- reuse
// SourceEditor's inline "+ Create new customer..." flow to get a grant
// target to work with, without saving an actual source (nothing is
// submitted here, so no source row is created).
async function createCustomerViaSourceEditor(
  page: import('@playwright/test').Page,
  name: string = customerName,
) {
  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Customer').selectOption({ label: '+ Create new customer…' })
  await page.getByPlaceholder('New customer name').fill(name)
  await page.getByRole('button', { name: 'Create' }).click()
  await expect(page.getByPlaceholder('New customer name')).toHaveCount(0)
}

test('roles: create, global capabilities, grants (add + remove), duplicate, delete', async ({
  page,
}) => {
  await createCustomerViaSourceEditor(page)

  await page.goto('/#/settings/roles')
  await page.getByRole('button', { name: '+ New role' }).click()
  await page.getByLabel('Name').fill(roleName)

  // These render as a custom "switch" (label wrapping a visually-hidden
  // checkbox plus a sibling track span) -- clicking the <input> directly
  // gets blocked ("intercepts pointer events") since the track sits on top
  // of it; clicking the wrapping <label> toggles the checkbox natively,
  // same as a user actually would.
  const createSourcesRow = page.locator('.switch-row', { hasText: 'Create new sources' })
  const manageUsersRow = page.locator('.switch-row', { hasText: 'Manage users' })
  await createSourcesRow.locator('label.switch').click()
  await manageUsersRow.locator('label.switch').click()
  await expect(createSourcesRow.locator('input[type=checkbox]')).toBeChecked()
  await expect(manageUsersRow.locator('input[type=checkbox]')).toBeChecked()
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page).toHaveURL(/#\/settings\/roles\/\d+$/)

  // Capability toggles persisted through the real API.
  await page.reload()
  await expect(
    page.locator('.switch-row', { hasText: 'Create new sources' }).locator('input[type=checkbox]'),
  ).toBeChecked()
  await expect(
    page.locator('.switch-row', { hasText: 'Manage users' }).locator('input[type=checkbox]'),
  ).toBeChecked()

  // Grants: starts empty.
  await expect(page.getByText("No grants yet — this role can't see anything.")).toBeVisible()

  // Add a customer-scoped grant.
  const addGrantForm = page.locator('.add-grant')
  await addGrantForm.locator('select').first().selectOption('customer')
  await addGrantForm.locator('select').nth(1).selectOption({ label: customerName })
  await addGrantForm.getByLabel('Grant View', { exact: true }).check()
  await addGrantForm.getByLabel('Grant Download', { exact: true }).check()
  await addGrantForm.getByRole('button', { name: 'Add grant' }).click()

  const grantRow = page.getByRole('row', { name: new RegExp(customerName) })
  await expect(grantRow).toBeVisible()
  await expect(grantRow.getByText('Customer', { exact: true })).toBeVisible()
  await expect(grantRow.getByText('View', { exact: true })).toBeVisible()
  await expect(grantRow.getByText('Download', { exact: true })).toBeVisible()

  // Remove it -- back to the empty state.
  await grantRow.getByRole('button', { name: 'remove' }).click()
  await expect(page.getByText("No grants yet — this role can't see anything.")).toBeVisible()

  // Duplicate, from the Roles list.
  await page.goto('/#/settings/roles')
  const roleRow = page.getByRole('row', { name: new RegExp(roleName) })
  await expect(roleRow).toBeVisible()
  // The list shows readable global capabilities, not the API keys.
  await expect(roleRow.locator('.caps')).toHaveText('Create sources, Manage users')
  await page
    .getByRole('row', { name: new RegExp(roleName) })
    .getByRole('button', { name: 'duplicate' })
    .click()
  // Duplicating navigates to the new role's own editor -- confirms it,
  // whatever the duplicate's name turns out to be.
  await expect(page).toHaveURL(/#\/settings\/roles\/\d+$/)

  // Cleanup: delete both the original and the duplicate. Whatever naming
  // scheme the duplicate uses, it's expected to contain the original's name
  // (e.g. "<name> (copy)") -- two rows here confirms that assumption rather
  // than silently deleting the wrong count if it doesn't.
  await page.goto('/#/settings/roles')
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(2)
  await page
    .getByRole('row', { name: new RegExp(roleName) })
    .first()
    .getByRole('button', { name: 'Delete' })
    .click()
  await confirmPopup(page, 'Delete')
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(1)
  await page
    .getByRole('row', { name: new RegExp(roleName) })
    .getByRole('button', { name: 'Delete' })
    .click()
  await confirmPopup(page, 'Delete')
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(0)
})

test('roles: a "Manage sources" grant sends manage_source and survives duplicating the role', async ({
  page,
}) => {
  const name = `E2E Manage Sources ${Date.now()}`
  const customer = `E2E Manage Sources Customer ${Date.now()}`
  await createCustomerViaSourceEditor(page, customer)

  await page.goto('/#/settings/roles')
  await page.getByRole('button', { name: '+ New role' }).click()
  await page.getByLabel('Name').fill(name)
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page).toHaveURL(/#\/settings\/roles\/\d+$/)

  const addGrantForm = page.locator('.add-grant')
  const manageSource = addGrantForm.getByLabel('Grant Manage sources', { exact: true })
  await expect(addGrantForm.locator('label.chip', { hasText: 'Manage sources' })).toHaveAttribute(
    'title',
    /^Manage sources:/,
  )
  await expect(addGrantForm.locator('label.chip', { hasText: 'manage_' })).toHaveCount(0)
  await addGrantForm.locator('select').first().selectOption('customer')
  await addGrantForm.locator('select').nth(1).selectOption({ label: customer })
  await addGrantForm.getByLabel('Grant View', { exact: true }).check()
  await manageSource.check()
  // The API still gets the keys, not the labels.
  const grantRequest = page.waitForRequest(
    (req) => req.method() === 'POST' && /\/roles\/\d+\/grants$/.test(req.url()),
  )
  await addGrantForm.getByRole('button', { name: 'Add grant' }).click()
  const grantBody = (await grantRequest).postDataJSON()
  expect(grantBody).toMatchObject({
    scope_type: 'customer',
    capabilities: ['view', 'manage_source'],
  })

  const grantRow = page.getByRole('row', { name: new RegExp(customer) })
  await expect(grantRow.getByText('Manage sources', { exact: true })).toBeVisible()
  await expect(grantRow.getByText('Manage rules', { exact: true })).toHaveCount(0)
  await expect(grantRow.getByText('manage_source', { exact: true })).toHaveCount(0)

  // A target the editor can't list (deleted, or out of reach of whoever
  // edits) shows the readable scope label and the id, not the raw key.
  await page.route(/\/customers(\?|$)/, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await page.reload()
  const fallbackTarget = page.locator('td.target', {
    hasText: new RegExp(`^Customer #${grantBody.scope_id}$`),
  })
  await expect(fallbackTarget).toBeVisible()
  await expect(page.locator('td.target', { hasText: /^customer #/ })).toHaveCount(0)
  await page.unroute(/\/customers(\?|$)/)

  // The duplicate carries the same grant.
  await page.goto('/#/settings/roles')
  await page
    .getByRole('row', { name: new RegExp(name) })
    .getByRole('button', { name: 'duplicate' })
    .click()
  await expect(page).toHaveURL(/#\/settings\/roles\/\d+$/)
  const copiedRow = page.getByRole('row', { name: new RegExp(customer) })
  await expect(copiedRow.getByText('Manage sources', { exact: true })).toBeVisible()

  // Cleanup: the original and the duplicate.
  await page.goto('/#/settings/roles')
  for (let left = 1; left >= 0; left--) {
    await page
      .getByRole('row', { name: new RegExp(name) })
      .first()
      .getByRole('button', { name: 'Delete' })
      .click()
    await confirmPopup(page, 'Delete')
    await expect(page.getByRole('row', { name: new RegExp(name) })).toHaveCount(left)
  }
})

test('roles: built-in roles open read-only in the editor', async ({ page }) => {
  await page.goto('/#/settings/roles')
  const row = page.locator('tr', { has: page.locator('.role-name', { hasText: /^No Access$/ }) })
  await expect(row.getByText('Built-in', { exact: true })).toBeVisible()
  await row.getByRole('button', { name: 'edit' }).click()

  await expect(
    page.getByText("Built-in role — its name, capabilities and grants can't be changed."),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Add grant' })).toHaveCount(0)
})

test('roles: the editor opens in a popup over the list, keeps its address, and asks before dropping changes', async ({
  page,
}) => {
  // #302.
  await page.goto('/#/settings/roles')
  await page.getByRole('button', { name: '+ New role' }).click()
  await expect(page).toHaveURL(/#\/settings\/roles\/new$/)
  const editor = page.getByRole('dialog', { name: 'New role' })
  await expect(editor).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Roles', exact: true })).toBeVisible()

  await editor.getByLabel('Name', { exact: true }).fill('not saved')
  await page.keyboard.press('Escape')
  const discard = page.getByRole('dialog', { name: 'Discard changes' })
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: 'Cancel' }).click()
  await expect(editor).toBeVisible()
  await editor.getByRole('button', { name: 'Cancel', exact: true }).click()
  await confirmPopup(page, 'Discard')
  await expect(page).toHaveURL(/#\/settings\/roles$/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('row', { name: /not saved/ })).toHaveCount(0)
})

test('roles: Escape on a new, untouched role closes it even while its lists still load', async ({ page }) => {
  // Seen in the lab (0.6.0-rc.3): with slow lists, Escape asked "Discard changes".
  await page.route(/\/(customers|folders|sources)(\?|$)/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    await route.continue()
  })
  await page.goto('/#/settings/roles')
  await page.getByRole('button', { name: '+ New role' }).click()
  await expect(page.getByRole('dialog', { name: 'New role' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page).toHaveURL(/#\/settings\/roles$/)
})
