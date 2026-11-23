import { expect, test } from '@playwright/test'
import { createUser } from './popups'
import { setUpMfa } from './totp'

// Mandatory MFA: with "Require MFA for local accounts" on, a local account
// without MFA can only set it up after signing in. (Super admins and roles
// with a global capability always need it: the seeded admin has it on.)

const username = `e2e-mfa-required-${Date.now()}`
// Within the default password policy (#245): nothing from the username.
const tempPassword = 'Lantern-Orbit-temp-58!'
const newPassword = 'Tagus-River-new-73!'

test('mfa required: a covered account must set up MFA before anything else', async ({
  page,
  browser,
}) => {
  await page.goto('/#/settings/sign-in-security')
  const toggle = page.getByLabel('Require MFA for local accounts')
  const row = page.locator('.setting-row', { has: toggle })
  await expect(toggle).not.toBeChecked()
  await row.locator('label.switch').click()
  await expect(toggle).toBeChecked()

  try {
    await createUser(page, { username, password: tempPassword, role: 'No Access' })

    // In its own context: the shared page keeps the super admin's session.
    const userContext = await browser.newContext({ storageState: { cookies: [], origins: [] } })
    const userPage = await userContext.newPage()
    await userPage.goto('/#/login')
    await userPage.getByLabel('Username').fill(username)
    await userPage.getByLabel('Password').fill(tempPassword)
    await userPage.getByRole('button', { name: 'Sign in' }).click()
    await expect(userPage).toHaveURL(/#\/change-password$/)
    await userPage.getByLabel('Current (temporary) password').fill(tempPassword)
    await userPage.getByLabel('New password').fill(newPassword)
    await userPage.getByRole('button', { name: 'Save' }).click()

    // Only the MFA setup is reachable: other pages lead back to it, and the
    // API refuses everything else.
    await expect(userPage).toHaveURL(/#\/setup-mfa$/)
    await userPage.goto('/#/viewer')
    await expect(userPage).toHaveURL(/#\/setup-mfa$/)
    const refused = await userPage.request.get('/system-settings')
    expect(refused.status()).toBe(403)
    expect((await refused.json()).detail.error_code).toBe('mfa_setup_required')

    await setUpMfa(userPage, newPassword)
    await expect(userPage).toHaveURL(/#\/viewer$/)
    expect((await userPage.request.get('/system-settings')).ok()).toBeTruthy()
    await userContext.close()
  } finally {
    const off = await page.request.patch('/system-settings', {
      data: { require_mfa_local_accounts: false },
    })
    expect(off.ok()).toBeTruthy()
    const users = await (await page.request.get('/users')).json()
    const created = users.find((u: { username: string }) => u.username === username)
    if (created) expect((await page.request.delete(`/users/${created.id}`)).ok()).toBeTruthy()
  }
})
