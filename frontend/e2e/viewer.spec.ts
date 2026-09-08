import { expect, test } from '@playwright/test'

// The built-in system source (app/bootstrap.py's SYSTEM_SOURCE_NAME) --
// visible to this suite's seeded user because it's a super-admin, per
// CLAUDE.md's "gated purely by is_super_admin" rule.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('opening the built-in log source lists and displays its own log file', async ({ page }) => {
  await page.goto('/#/viewer')

  await page.getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await expect(page).toHaveURL(/#\/viewer\/\d+$/)

  // TimedRotatingFileHandler (app/logging_config.py) opens perchtail.log
  // immediately on startup, so it's already there and non-empty by the
  // time the e2e server answers /healthz.
  const logFile = page.getByRole('button', { name: 'perchtail.log' })
  await expect(logFile).toBeVisible()
  await logFile.click()

  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')
  await expect(page.locator('.cm-content')).not.toBeEmpty()
})
