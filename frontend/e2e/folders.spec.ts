import { expect, test } from '@playwright/test'
import { confirmPopup, createFolder } from './popups'

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
  // Scoped to `.name` (only ever this row's own folder name), so "EU"
  // doesn't also match a row whose name merely contains it.
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

  await createFolder(page, 'EU')
  await expect(folderRow(page, 'EU')).toBeVisible()

  await createFolder(page, 'Production', folderRow(page, 'EU'))
  await expect(folderRow(page, 'Production')).toBeVisible()

  // Rename, in a popup (#301).
  await folderRow(page, 'Production').getByRole('button', { name: 'Rename' }).click()
  const renameDialog = page.getByRole('dialog', { name: 'Rename folder' })
  await renameDialog.getByLabel('Name').fill('Prod')
  await renameDialog.getByRole('button', { name: 'Save' }).click()
  await expect(renameDialog).toHaveCount(0)
  await expect(folderRow(page, 'Prod')).toBeVisible()
  await expect(folderRow(page, 'Production')).toHaveCount(0)

  // Drag and drop (#355): the source onto a folder, then back to the top
  // level; the move goes through the API, so it survives a reload.
  const sourceRow = page.locator('.row.source, .loose-source').filter({ hasText: sourceName })
  await sourceRow.dragTo(folderRow(page, 'EU'))
  await page.reload()
  await page.locator('#customer-select').selectOption({ label: customerName })
  await expect(page.locator('.row.source').filter({ hasText: sourceName })).toBeVisible()
  await page.locator('.row.source').filter({ hasText: sourceName }).dragTo(page.locator('.top-level'))
  await expect(page.locator('.loose-source').filter({ hasText: sourceName })).toBeVisible()

  // Delete-protection: a folder with a child folder can't be deleted.
  await folderRow(page, 'EU').getByRole('button', { name: 'Delete' }).click()
  await confirmPopup(page, 'Delete')
  await expect(page.locator('.row-error')).toContainText(/sub-folders or sources/)
  await expect(folderRow(page, 'EU')).toBeVisible()

  // Move: re-parent "Prod" back to top level so "EU" becomes deletable too.
  await folderRow(page, 'Prod').getByRole('button', { name: 'Move' }).click()
  const moveDialog = page.getByRole('dialog', { name: 'Move "Prod"' })
  await moveDialog.getByLabel('Move under').selectOption({ label: '(top level)' })
  await moveDialog.getByRole('button', { name: 'Move' }).click()
  await expect(moveDialog).toHaveCount(0)
  await expect(folderRow(page, 'Prod').locator('.indent')).toHaveAttribute('style', /width: 0px/)

  await folderRow(page, 'Prod').getByRole('button', { name: 'Delete' }).click()
  await confirmPopup(page, 'Delete')
  await expect(folderRow(page, 'Prod')).toHaveCount(0)
  await folderRow(page, 'EU').getByRole('button', { name: 'Delete' }).click()
  await confirmPopup(page, 'Delete')
  await expect(folderRow(page, 'EU')).toHaveCount(0)

  // Cleanup: source via the Sources admin UI, customer via a direct API
  // call (no admin UI exposes customer deletion yet).
  await page.goto('/#/settings/sources')
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'Delete' })
    .click()
  await confirmPopup(page, 'Delete')
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)

  const customers = await page.request.get('/customers').then((r) => r.json())
  const customer = customers.find((c: { name: string }) => c.name === customerName)
  expect(customer).toBeTruthy()
  const deleteResponse = await page.request.delete(`/customers/${customer.id}`)
  expect(deleteResponse.ok()).toBe(true)
})
