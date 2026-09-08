import { expect, test } from '@playwright/test'

const username = `e2e-user-${Date.now()}`
const password = 'e2e-throwaway-password-123!'

test('users: create, reset password, change role, deactivate/reactivate', async ({ page }) => {
  await page.goto('/#/settings/users')

  await page.getByPlaceholder('username or email').fill(username)
  await page.getByPlaceholder('temporary password').fill(password)
  // The seeded builtin "No Access" role (app/bootstrap.py's
  // seed_no_access_role) always exists and grants nothing -- a safe default
  // for a throwaway test account.
  await page.locator('.add-user select').selectOption({ label: 'No Access' })
  await page.getByRole('button', { name: 'Create user' }).click()

  const row = page.getByRole('row', { name: new RegExp(username) })
  await expect(row).toBeVisible()
  await expect(row.getByText('active', { exact: true })).toBeVisible()

  // Role change persists through the real API, not just local state.
  await row.locator('select').selectOption({ label: 'Super Admin' })
  await page.reload()
  const roleSelectAfterReload = page.getByRole('row', { name: new RegExp(username) }).locator('select')
  await expect(roleSelectAfterReload.locator('option:checked')).toHaveText('Super Admin')
  await roleSelectAfterReload.selectOption({ label: 'No Access' })

  // Reset password shows a one-time temporary password row.
  await page.getByRole('row', { name: new RegExp(username) }).getByRole('button', { name: 'reset password' }).click()
  await expect(page.getByText('Temporary password (shown once):')).toBeVisible()

  // Deactivate flips the status badge and the action's own label; reactivate
  // flips both back.
  await page.getByRole('row', { name: new RegExp(username) }).getByRole('button', { name: 'deactivate' }).click()
  const rowAfterDeactivate = page.getByRole('row', { name: new RegExp(username) })
  await expect(rowAfterDeactivate.getByText('inactive', { exact: true })).toBeVisible()
  await expect(rowAfterDeactivate.getByRole('button', { name: 'reactivate' })).toBeVisible()

  await rowAfterDeactivate.getByRole('button', { name: 'reactivate' }).click()
  const rowAfterReactivate = page.getByRole('row', { name: new RegExp(username) })
  await expect(rowAfterReactivate.getByText('active', { exact: true })).toBeVisible()

  // Cleanup: deactivate again (soft-delete is the only affordance this page
  // gives -- see CLAUDE.md's "Deactivate (soft, keeps audit history) is the
  // default way to remove access").
  await rowAfterReactivate.getByRole('button', { name: 'deactivate' }).click()
})
