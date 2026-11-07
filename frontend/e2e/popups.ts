import { expect, type Locator, type Page } from '@playwright/test'

// Settings forms and confirmations open in popups (#301), not the browser's
// confirm() or inline forms.

/** Clicks `label` in the open popup and waits for that popup to close
 * (another one may open next, e.g. the temporary password after a reset). */
export async function confirmPopup(page: Page, label: string) {
  const button = page.getByRole('button', { name: label, exact: true })
  const dialog = page.getByRole('dialog').filter({ has: button })
  await dialog.getByRole('button', { name: label, exact: true }).click()
  await expect(dialog).toHaveCount(0)
}

/** Creates a folder in Settings -> Folders (customer already picked):
 * from the page's "+ New folder", or a row's "+ Subfolder" when `under`
 * names the parent row. */
export async function createFolder(page: Page, name: string, underRow?: Locator) {
  if (underRow) await underRow.getByRole('button', { name: '+ Subfolder' }).click()
  else await page.getByRole('button', { name: '+ New folder' }).click()
  const dialog = page.getByRole('dialog', { name: 'New folder' })
  await dialog.getByLabel('Name').fill(name)
  await dialog.getByRole('button', { name: 'Create folder' }).click()
  await expect(dialog).toHaveCount(0)
}

/** Creates a local user through Settings -> Users -> + New user. */
export async function createUser(
  page: Page,
  user: { username: string; password: string; role: string; displayName?: string; email?: string },
) {
  await page.goto('/#/settings/users')
  await page.getByRole('button', { name: '+ New user' }).click()
  const dialog = page.getByRole('dialog', { name: 'New user' })
  await dialog.getByLabel('Username').fill(user.username)
  if (user.displayName) await dialog.getByLabel('Display name (optional)').fill(user.displayName)
  if (user.email) await dialog.getByLabel('Email (optional)').fill(user.email)
  await dialog.getByLabel('Temporary password').fill(user.password)
  await dialog.getByLabel('Role').selectOption({ label: user.role })
  await dialog.getByRole('button', { name: 'Create user' }).click()
  await expect(dialog).toHaveCount(0)
}
