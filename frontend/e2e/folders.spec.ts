import { expect, test } from '@playwright/test'

// Folders are always scoped to a customer, and there's no dedicated
// customer-creation UI (SourceEditor's inline "+ Create new customer…" is
// the only path today) -- so this spec creates one throwaway source purely
// to get a fresh customer to organize folders under, then tears down
// source, folders, and (via a direct API call -- no admin UI exposes
// customer deletion) the customer itself at the end.
const stamp = Date.now()
const customerName = `E2E Folders Corp ${stamp}`
const sourceName = `e2e-folders-source-${stamp}`

function folderRow(page: import('@playwright/test').Page, name: string) {
  // A folder row's "move to" <select> lists every OTHER folder's name as
  // an <option>, which counts toward the row's own text content even
  // though it's not visible -- scope to `.name` specifically (only ever
  // this row's own folder name) so e.g. matching "EU" doesn't also catch
  // "Prod"'s row (which lists EU as a valid move target).
  return page.locator('.row').filter({ has: page.locator('.name', { hasText: new RegExp(`^${name}$`) }) })
}

test('folders: create nested folders, rename, move, delete-protection, then delete', async ({ page }) => {
  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(sourceName)
  await page.locator('form select').first().selectOption('__new__')
  await page.getByPlaceholder('New customer name').fill(customerName)
  await page.locator('.inline-create').getByRole('button', { name: 'Create' }).click()
  await expect(page.getByPlaceholder('New customer name')).toHaveCount(0)
  await page.getByLabel('Host').fill('h1')
  await page.getByLabel('Base path').fill('/var/log')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)

  await page.goto('/#/settings/folders')
  await page.locator('#customer-select').selectOption({ label: customerName })

  await page.getByPlaceholder('New folder name').fill('EU')
  await page.getByRole('button', { name: '+ New folder' }).click()
  await expect(folderRow(page, 'EU')).toBeVisible()

  await folderRow(page, 'EU').getByRole('button', { name: '+ Subfolder' }).click()
  await page.getByPlaceholder('New folder name').fill('Production')
  await page.getByRole('button', { name: '+ New folder' }).click()
  await expect(folderRow(page, 'Production')).toBeVisible()

  // Rename.
  await folderRow(page, 'Production').getByRole('button', { name: 'Rename' }).click()
  await page.locator('.rename-form input').fill('Prod')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(folderRow(page, 'Prod')).toBeVisible()
  await expect(folderRow(page, 'Production')).toHaveCount(0)

  // Delete-protection: a folder with a child folder can't be deleted.
  await folderRow(page, 'EU').getByRole('button', { name: 'Delete' }).click()
  await expect(page.locator('.row-error')).toContainText(/sub-folders or sources/)
  await expect(folderRow(page, 'EU')).toBeVisible()

  // Move: re-parent "Prod" back to top level so "EU" becomes deletable too.
  await folderRow(page, 'Prod').locator('.move-select').selectOption('')
  await expect(folderRow(page, 'Prod').locator('.move-select')).toHaveValue('')

  await folderRow(page, 'Prod').getByRole('button', { name: 'Delete' }).click()
  await expect(folderRow(page, 'Prod')).toHaveCount(0)
  await folderRow(page, 'EU').getByRole('button', { name: 'Delete' }).click()
  await expect(folderRow(page, 'EU')).toHaveCount(0)

  // Cleanup: source via the Sources admin UI, customer via a direct API
  // call (no admin UI exposes customer deletion yet).
  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)

  const customers = await page.request.get('/customers').then((r) => r.json())
  const customer = customers.find((c: { name: string }) => c.name === customerName)
  expect(customer).toBeTruthy()
  const deleteResponse = await page.request.delete(`/customers/${customer.id}`)
  expect(deleteResponse.ok()).toBe(true)
})
