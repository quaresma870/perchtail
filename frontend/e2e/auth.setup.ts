import { expect, test as setup } from '@playwright/test'

// Must match backend/scripts/run_e2e_server.sh's defaults (and whatever it
// was invoked with, if overridden).
const ADMIN_USERNAME = process.env.E2E_ADMIN_USERNAME ?? 'e2e-admin'
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'e2e-test-password-123!'

const authFile = 'e2e/.auth/admin.json'

setup('authenticate as the seeded e2e super-admin', async ({ page }) => {
  await page.goto('/#/login')
  await page.getByLabel('Username').fill(ADMIN_USERNAME)
  await page.getByLabel('Password').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/#\/viewer$/)

  await page.context().storageState({ path: authFile })
})
