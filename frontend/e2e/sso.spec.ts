import { expect, test } from '@playwright/test'
import { confirmPopup } from './popups'

// Only the admin settings CRUD is covered here, not a real SSO login round
// trip -- that would need a real OIDC IdP stood up, which is out of scope
// (see ROADMAP.md's "Frontend E2E testing" notes). The issuer URL below
// uses the reserved, guaranteed-nonexistent ".invalid" TLD (RFC 2606) so
// "Test connection" fails fast and predictably instead of timing out
// against a real network address.
const providerName = `E2E SSO ${Date.now()}`
const groupName = `e2e-group-${Date.now()}`

test('SSO settings: create provider, test connection (expected failure), group mapping, remove', async ({
  page,
}) => {
  await page.goto('/#/settings/sso')
  await page.getByLabel('Display name').fill(providerName)
  await page.getByLabel('Issuer URL').fill('https://issuer.invalid.example/tenant')
  await page.getByLabel('Client ID').fill('e2e-client-id')
  await page.getByLabel('Client secret').fill('e2e-client-secret')
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page.getByRole('button', { name: 'Test connection' })).toBeVisible()

  await page.getByRole('button', { name: 'Test connection' }).click()
  await expect(page.locator('.test-result')).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('.test-result')).toContainText('✕')

  // Group -> role mapping.
  await page.getByRole('button', { name: '+ Add mapping' }).click()
  const mappingDialog = page.getByRole('dialog', { name: 'New group mapping' })
  await mappingDialog.getByLabel('IdP group name').fill(groupName)
  await mappingDialog.getByLabel('Role').selectOption({ label: 'No Access' })
  await mappingDialog.getByRole('button', { name: 'Add mapping' }).click()
  await expect(mappingDialog).toHaveCount(0)

  const mappingRow = page.locator('.mapping-row', { hasText: groupName })
  await expect(mappingRow).toBeVisible()
  await expect(mappingRow.getByText('No Access')).toBeVisible()
  await mappingRow.getByRole('button', { name: 'Remove' }).click()
  await confirmPopup(page, 'Remove')
  await expect(mappingRow).toHaveCount(0)

  // Cleanup: remove the provider itself.
  await page.locator('.actions').getByRole('button', { name: 'Remove' }).click()
  await confirmPopup(page, 'Remove')
  await expect(page.getByRole('button', { name: 'Test connection' })).toHaveCount(0)
})
