import { expect, test } from '@playwright/test'

// Connections-home's "Tree" view (ROADMAP.md's folder-tree navigation
// item) groups RBAC-visible sources by customer -> nested folder path,
// built entirely from GET /sources's own folder_path field. This spec
// builds a small nested structure (customer -> EU -> Prod -> a source,
// plus a second source directly under the customer) via the existing
// SourceEditor + Folders admin page flows, then exercises the tree.
const stamp = Date.now()
const customerName = `E2E Tree Corp ${stamp}`
const rootSourceName = `e2e-tree-root-${stamp}`
const nestedSourceName = `e2e-tree-nested-${stamp}`

function folderRow(page: import('@playwright/test').Page, name: string) {
  return page.locator('.row').filter({ has: page.locator('.name', { hasText: new RegExp(`^${name}$`) }) })
}

async function createSource(
  page: import('@playwright/test').Page,
  name: string,
  opts: { newCustomer?: boolean; folderLabel?: string } = {},
) {
  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(name)
  if (opts.newCustomer) {
    await page.locator('form select').first().selectOption('__new__')
    await page.getByPlaceholder('New customer name').fill(customerName)
    await page.locator('.inline-create').getByRole('button', { name: 'Create' }).click()
    await expect(page.getByPlaceholder('New customer name')).toHaveCount(0)
  } else {
    await page.locator('form select').first().selectOption({ label: customerName })
  }
  if (opts.folderLabel) {
    await page.locator('form select').nth(1).selectOption({ label: opts.folderLabel })
  }
  await page.getByLabel('Host').fill('h1')
  await page.getByLabel('Base path').fill('/var/log')
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
}

test('connections-home tree: nests sources by customer/folder and search auto-expands', async ({
  page,
}) => {
  await createSource(page, rootSourceName, { newCustomer: true })

  await page.goto('/#/settings/folders')
  await page.locator('#customer-select').selectOption({ label: customerName })
  await page.getByPlaceholder('New folder name').fill('EU')
  await page.getByRole('button', { name: '+ New folder' }).click()
  await expect(folderRow(page, 'EU')).toBeVisible()
  await folderRow(page, 'EU').getByRole('button', { name: '+ Subfolder' }).click()
  await page.getByPlaceholder('New folder name').fill('Prod')
  await page.getByRole('button', { name: '+ New folder' }).click()
  await expect(folderRow(page, 'Prod')).toBeVisible()

  await createSource(page, nestedSourceName, { folderLabel: '— Prod' })

  await page.goto('/#/viewer')
  await expect(page.locator('.picker-column.all')).toBeVisible()
  await page.getByRole('button', { name: 'Tree', exact: true }).click()

  const tree = page.locator('.connections-tree-body')
  await tree.getByText(customerName, { exact: true }).click()
  await tree.getByText('EU', { exact: true }).click()
  await tree.getByText('Prod', { exact: true }).click()

  await expect(tree.getByText(rootSourceName, { exact: true })).toBeVisible()
  await expect(tree.getByText(nestedSourceName, { exact: true })).toBeVisible()

  // Switching back to List keeps the existing flat-list behavior intact.
  await page.getByRole('button', { name: 'List', exact: true }).click()
  await expect(page.locator('.connections-tree-body')).toHaveCount(0)
  await expect(page.getByRole('button', { name: rootSourceName })).toBeVisible()

  // Search auto-expands the tree even when nothing was manually expanded.
  // filterConnections matches folder/customer/host, not a source's own
  // name (see connection-filter.ts), so search by the folder name here --
  // it should surface the nested source (whose path includes "Prod") and
  // filter out the root-level one (which has no folder at all).
  await page.getByRole('button', { name: 'Tree', exact: true }).click()
  await page.getByPlaceholder('Search by folder, customer, or host…').fill('Prod')
  await expect(page.locator('.connections-tree-body').getByText(nestedSourceName, { exact: true })).toBeVisible()
  await expect(page.locator('.connections-tree-body').getByText(rootSourceName, { exact: true })).toHaveCount(0)

  // Cleanup: both sources via the Sources admin UI first (folders.py
  // refuses to delete a non-empty folder), then the now-childless
  // folders, then the customer via a direct API call (no admin UI exposes
  // customer deletion yet).
  await page.goto('/#/settings/sources')
  for (const name of [rootSourceName, nestedSourceName]) {
    page.once('dialog', (dialog) => dialog.accept())
    await page.getByRole('row', { name: new RegExp(name) }).getByRole('button', { name: 'delete' }).click()
    await expect(page.getByRole('row', { name: new RegExp(name) })).toHaveCount(0)
  }

  await page.goto('/#/settings/folders')
  await page.locator('#customer-select').selectOption({ label: customerName })
  await folderRow(page, 'Prod').locator('.move-select').selectOption('')
  await folderRow(page, 'Prod').getByRole('button', { name: 'Delete' }).click()
  await expect(folderRow(page, 'Prod')).toHaveCount(0)
  await folderRow(page, 'EU').getByRole('button', { name: 'Delete' }).click()
  await expect(folderRow(page, 'EU')).toHaveCount(0)

  const customers = await page.request.get('/customers').then((r) => r.json())
  const customer = customers.find((c: { name: string }) => c.name === customerName)
  expect(customer).toBeTruthy()
  const deleteResponse = await page.request.delete(`/customers/${customer.id}`)
  expect(deleteResponse.ok()).toBe(true)
})
