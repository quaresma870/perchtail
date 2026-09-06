import { expect, test } from '@playwright/test'
import { loadFixturePaths } from './fixture-paths'

// Real sshd (backend/scripts/setup_e2e_test_servers.sh), not a mocked
// client -- this exercises SourceEditor's create/edit form, RuleEditor's
// row-based AND raw-paste modes (including last-match-wins), the viewer's
// live browse/open over real SFTP, and source deletion, all in one flow.
const sourceName = `E2E SSH Source ${Date.now()}`

test('SSH source: create, rule editor (rows + raw paste), browse, delete', async ({ page }) => {
  const fixtures = loadFixturePaths()
  const ssh = fixtures.ssh

  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(sourceName)
  await page.getByLabel('Protocol').selectOption('ssh')
  await page.getByLabel('Host').fill(ssh.host)
  await page.getByLabel('Port').fill(String(ssh.port))
  await page.getByLabel('Base path').fill(ssh.base_path)
  await page.getByLabel('Username').fill(ssh.username)
  await page.getByLabel('Password', { exact: true }).fill(ssh.password)
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
  const sourceId = Number(page.url().split('/').pop())

  // Row-based rule: include every .log file.
  const addRow = page.locator('.rule-editor .add-row')
  await addRow.getByRole('combobox').selectOption('include')
  await addRow.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('**/*.log')
  await addRow.getByRole('button', { name: 'Add rule' }).click()
  await expect(page.locator('.rule-row')).toHaveCount(1)

  // Browse over real SFTP and confirm both fixture files are visible/openable.
  // Generous timeout -- this is a real network round trip (connect,
  // host-key TOFU, SFTP init, listdir), not instant like the mocked/local
  // specs, and the first connection to a fresh host pays extra one-time
  // cost saving its host key.
  await page.goto(`/#/viewer/${sourceId}`)
  await expect(page.getByRole('button', { name: 'hello.log' })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: 'other.log' })).toBeVisible({ timeout: 20_000 })
  await page.getByRole('button', { name: 'hello.log' }).click()
  await expect(page.locator('.cm-content')).toContainText('ssh hello world log line 1')

  // Raw-paste mode: last-match-wins should hide hello.log while other.log
  // stays visible.
  await page.goto(`/#/settings/sources/${sourceId}`)
  await page.getByRole('button', { name: 'Raw text' }).click()
  await page.locator('textarea.input.mono').fill('**/*.log\n!hello.log')
  await page.getByRole('button', { name: 'Apply' }).click()
  await expect(page.locator('.rule-row')).toHaveCount(2)

  await page.goto(`/#/viewer/${sourceId}`)
  await expect(page.getByRole('button', { name: 'other.log' })).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: 'hello.log' })).toHaveCount(0)

  // Cleanup.
  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
})
