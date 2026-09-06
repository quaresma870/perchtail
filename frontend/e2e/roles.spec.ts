import { expect, test } from '@playwright/test'

const roleName = `E2E Role ${Date.now()}`
const customerName = `E2E Role Test Customer ${Date.now()}`

// Roles has no dedicated "create a customer" affordance of its own -- reuse
// SourceEditor's inline "+ Create new customer..." flow to get a grant
// target to work with, without saving an actual source (nothing is
// submitted here, so no source row is created).
async function createCustomerViaSourceEditor(page: import('@playwright/test').Page) {
  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Customer').selectOption({ label: '+ Create new customer…' })
  await page.getByPlaceholder('New customer name').fill(customerName)
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
  await addGrantForm.getByLabel('view', { exact: true }).check()
  await addGrantForm.getByLabel('download', { exact: true }).check()
  await addGrantForm.getByRole('button', { name: 'Add grant' }).click()

  const grantRow = page.getByRole('row', { name: new RegExp(customerName) })
  await expect(grantRow).toBeVisible()
  await expect(grantRow.getByText('customer', { exact: true })).toBeVisible()
  await expect(grantRow.getByText('view', { exact: true })).toBeVisible()
  await expect(grantRow.getByText('download', { exact: true })).toBeVisible()

  // Remove it -- back to the empty state.
  await grantRow.getByRole('button', { name: 'remove' }).click()
  await expect(page.getByText("No grants yet — this role can't see anything.")).toBeVisible()

  // Duplicate, from the Roles list.
  await page.goto('/#/settings/roles')
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toBeVisible()
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
  page.on('dialog', (dialog) => dialog.accept())
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(2)
  await page
    .getByRole('row', { name: new RegExp(roleName) })
    .first()
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(1)
  await page
    .getByRole('row', { name: new RegExp(roleName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(0)
})
