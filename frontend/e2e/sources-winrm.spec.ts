import { expect, test } from '@playwright/test'

// No real WinRM target is available in CI or this dev sandbox (that needs
// an actual Windows host) -- run_e2e_server.sh wires in
// backend/app/testing/fake_winrm.py instead, which serves a small
// in-memory fixture "filesystem" from behind the real winrm.py collector
// code. FIXTURE_ROOT here must match that module's FIXTURE_ROOT exactly.
// sources-ssh.spec.ts and sources-smb.spec.ts already cover raw-paste mode
// and last-match-wins against real protocol servers, so this one just
// confirms WinRM sources work end to end through the same UI.
const sourceName = `E2E WinRM Source ${Date.now()}`
const FIXTURE_ROOT = 'C:\\Logs\\e2e-fixture'

test('WinRM source: create, add rule, browse over the mocked connector, delete', async ({
  page,
}) => {
  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(sourceName)
  await page.getByLabel('Protocol').selectOption('winrm')
  await page.getByLabel('Host').fill('e2e-fake-winrm-host')
  await page.getByLabel('Base path').fill(FIXTURE_ROOT)
  await page.getByLabel('Username').fill('svc')
  await page.getByLabel('Password', { exact: true }).fill('unused-fake-connector-ignores-this')
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
  const sourceId = Number(page.url().split('/').pop())

  const addRow = page.locator('.rule-editor .add-row')
  await addRow.getByRole('combobox').selectOption('include')
  await addRow.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('**/*.log')
  await addRow.getByRole('button', { name: 'Add rule' }).click()
  await expect(page.locator('.rule-row')).toHaveCount(1)

  await page.goto(`/#/viewer/${sourceId}`)
  await expect(page.getByRole('button', { name: 'hello.log' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'other.log' })).toBeVisible()
  await page.getByRole('button', { name: 'hello.log' }).click()
  await expect(page.locator('.cm-content')).toContainText('winrm hello world log line 1')

  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
})
